from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from radar.models import KeywordCandidate
from radar.public_fetch import FetchError
from radar.sales_pools import intake_card, region_weight


@pytest.mark.django_db
@pytest.mark.parametrize('reason', ['http_403','timeout','robots_or_robots_unavailable:http_403',
                                     'robots_or_robots_unavailable:invalid_format'])
def test_tavily_failure_keeps_reviewable_snippet(tmp_path, monkeypatch, settings, reason):
    settings.DATA_DIR = Path(tmp_path)
    candidate = KeywordCandidate.objects.create(canonical_url='https://example.org/factory', provider='tavily',
        title='新产品动态', snippet='日本企业株式会社样例宣布在苏州建设新工厂', published_at='')
    def blocked(*args, **kwargs):
        raise FetchError(reason)
    monkeypatch.setattr('radar.management.commands.keyword_radar.read_original', blocked)
    call_command('keyword_radar', online=True, existing_only=True, provider='tavily', max_details=1,
                 output_dir=str(tmp_path), stdout=StringIO())
    candidate.refresh_from_db()
    card = intake_card(candidate)
    assert candidate.status == 'snippet_review'
    assert card['content_mode'] == 'snippet_only'
    assert card['grade'] == 'B' and card['score'] < 65
    assert not candidate.raw_path
    assert region_weight('苏州') > region_weight('东莞') > region_weight('沈阳')


def test_next_page_keeps_same_origin_and_column():
    from collectors.research_pages import next_page
    from lxml import html
    page = html.fromstring('<a rel="next" href="https://example.net/news/page/3">次へ</a>'
                           '<a rel="next" href="/news/page/3">次へ</a>')
    assert next_page(page, 'https://example.org/news/page/2') == 'https://example.org/news/page/3'


def test_named_korean_company_does_not_inherit_japanese_mentions():
    from types import SimpleNamespace
    candidate = SimpleNamespace(pk=1, title='苏州新工厂奠基', canonical_url='https://example.org/a',
        provider='tavily', status='model_needs_review', raw_path='raw/research/a.html',
        published_at='', snippet='', text='韩国示例公司在苏州新设工厂。日本企业提供设备。',
        result={'source_sha256':'test','extraction':{'company_name':'示例公司'}})
    assert intake_card(candidate)['grade'] == 'C'


def test_fhc_resumes_after_page_three(monkeypatch):
    from types import SimpleNamespace
    from collectors.shanghai_exhibitors import collect_exhibitors
    base = 'https://www.fhcchina.com/roster'
    seen = []
    def read(url):
        seen.append(url)
        body = ('<html><head><title>FHC上海</title></head><body><script>var lastPage = 5</script>'
                '<div class="fhc-exhibitor-list"><div><div class="_name">样例公司</div>'
                '<div><div>A1</div></div><div><div>日本</div></div></div></div></body></html>')
        return {'body':body.encode()}, {'url':url}
    monkeypatch.setattr('collectors.shanghai_exhibitors.read_original', read)
    source = SimpleNamespace(key='fhc_shanghai_japan', url=base, checkpoint={'fhc_page':4}, _max_pages=2)
    result = collect_exhibitors(source)
    assert seen == [base, base+'/page/4', base+'/page/5']
    assert len(result['items']) == 2
    assert result['collector_checkpoint']['fhc_page'] == 1


@pytest.mark.django_db
def test_sales_recheck_keeps_downgrade_in_observation():
    from radar.sales_pools import build_sales_pools, intake_grades
    candidate = KeywordCandidate.objects.create(canonical_url='https://example.org/move',
        provider='tavily', title='日本企业上海物流中心新设', snippet='日本企业上海物流中心新设',
        result={'sales_recheck':{'decision':'downgraded','note':'原发正文仅为办公室搬迁'}})
    row, = intake_grades(build_sales_pools())['C']
    assert row['candidate_id'] == candidate.pk
    assert '原发正文仅为办公室搬迁' in row['pending']
    assert row['sales_recheck']['decision'] == 'downgraded'


@pytest.mark.parametrize(('kind','title','body'), [
    ('study_tour', '上海工場の視察ツアー参加者募集', '株式会社例社は中国の設備工場を訪問します。'),
    ('office_move', '上海現地法人のオフィス移転', '株式会社例社は上海現地法人のオフィスを移転しました。'),
    ('anniversary', '株式会社例社 中国展開20周年記念式典', '中国蘇州工場の開業を振り返りました。'),
    ('industry_survey', '中国・日系製造業の業種分布ランキング', '株式会社例社が上海の日系企業の実態調査を実施しました。'),
    ('reverse_expansion', '中国ブランドが日本初上陸、渋谷でPOP-UP', '株式会社例社の協力で中国企業が日本に会社設立。'),
])
def test_common_intake_noise_moves_to_c(kind, title, body):
    from types import SimpleNamespace
    from collectors.keyword_radar import coarse_screen, score_candidate
    candidate = SimpleNamespace(pk=1,title=title,canonical_url='https://example.org/noise',
        provider='tavily',status='discovered',raw_path='raw/research/example.html',
        published_at='',snippet=body,text=body,result={'source_sha256':'test'})
    card = intake_card(candidate)
    assert card['grade'] == 'C' and card['score'] < 45
    assert card['noise_reason'] == coarse_screen(title,body) == 'noise_' + kind
    assert score_candidate(title,body) < 0


def test_office_move_with_new_shanghai_warehouse_stays_b():
    from types import SimpleNamespace
    from collectors.keyword_radar import intake_noise_reason, score_candidate
    title = '株式会社例社 上海オフィス移転のお知らせ'
    body = '上海現地法人のオフィスを移転します。同時に上海で物流倉庫を新設します。'
    candidate = SimpleNamespace(pk=1,title=title,canonical_url='https://example.org/warehouse',
        provider='tavily',status='discovered',raw_path='raw/research/example.html',
        published_at='',snippet=body,text=body,result={'source_sha256':'test'})
    assert not intake_noise_reason(title,body)
    assert score_candidate(title,body) > 0
    assert intake_card(candidate)['grade'] == 'B'


@pytest.mark.parametrize(('title','body','reason'), [
    ('日本企業向け海外拠点リストをご紹介', '中国の工場情報を掲載。', 'reference_material'),
    ('上海ロボット国際見本市 初開催へ', '上海で出展者を募集。', 'trade_show_overview'),
    ('対米投資でニューヨークに半導体工場', '中国でも関心が高い。', 'third_market_investment'),
    ('中国進出日本企業が1万社へ減少', '業界全体の拠点数を集計した。', 'industry_survey'),
    ('上海ロボット国際見本市 初開催へ', '株式会社例社は上海に現地法人を設立し、受注を開始した。', ''),
    ('対米投資でニューヨークに半導体工場', '株式会社例社は蘇州工場を増設する。', ''),
    ('中国進出日本企業が1万社へ減少', '株式会社例社は上海向けに製品の出荷を開始した。', ''),
    ('業種分布ランキング', '株式会社例社は上海で販売事業会社「例（上海）有限公司」を設立した。', ''),
])
def test_extended_noise_rules_require_absence_of_real_mainland_action(title, body, reason):
    from collectors.keyword_radar import intake_noise_reason
    assert intake_noise_reason(title,body)==('noise_'+reason if reason else '')


def test_verified_completed_opening_uses_event_month_without_claiming_publication(monkeypatch):
    from datetime import date
    from types import SimpleNamespace
    from radar.sales_pools import keyword_card

    monkeypatch.setattr('radar.sales_pools.timezone.localdate', lambda: date(2026, 11, 19))
    opening = '令和 8年 10月\n例社 上海店 オープン'
    company, quote = '株式会社例社', '中国上海の店舗住所'
    url = 'https://example.org/company'
    verification = {
        'status': 'verified_research_signal',
        'sources': [{'url': url, 'role': 'company_profile'}],
        'completed_activity_evidence': opening,
    }
    candidate = SimpleNamespace(
        title='例社 上海店開業', text=f'{company}\n{quote}\n{opening}',
        status='verified_research_signal', canonical_url=url, raw_path='raw/research/company.html',
        provider='manual_primary_review', published_at='',
        result={'verification': verification, 'extraction': {
            'source_url': url, 'company_name': company, 'evidence_quote': quote,
            'japanese_evidence': company, 'is_japanese_affiliated': True,
            'event_type': 'new_china_entity', 'china_location': '上海',
        }},
    )
    card = keyword_card(candidate)
    assert card['grade'] == 'A'
    assert card['completed_activity_month'] == '2026-10'
    assert not card.get('source_publication_date')

    verification['completed_activity_evidence'] = opening.replace('10月', '11月')
    card = keyword_card(candidate)
    assert card['grade'] == 'B'
    assert 'completed_activity_month' not in card


@pytest.mark.parametrize(('body', 'expected'), [
    ('株式会社例社:海外事業\n中国市場向けに限定カラー商品の販売を開始した。', ''),
    ('株式会社例社:海外事業\n中国・東莞市の買い手から引き合い。有償サンプルを発送。その後、追加発注を頂いた。', ''),
    ('中国江蘇省の子会社、例香料(張家港)で香料工場を建設する。', ''),
    ('例商事株式会社:越境事業\n中国市場での現地法人運営を続けている。', ''),
    ('株式会社例社は海外で開催すると発表。限定商品を販売。上海 場所:未公開 期間:2026/12/17', ''),
    ('株式会社例社:海外事業\n対象市場は中国。韓国向け限定商品の販売を開始した。', 'noise_industry_survey'),
    ('中国市場向けに限定商品の販売を開始した。', 'noise_industry_survey'),
    ('株式会社例社:事業\n関連記事：中国向け商品の販売を開始した。', 'noise_industry_survey'),
])
def test_title_noise_defers_to_named_mainland_operations(body, expected):
    from collectors.keyword_radar import intake_noise_reason

    assert intake_noise_reason('業種分布ランキング', body) == expected


def test_reverse_investment_guide_with_legal_examples_remains_c():
    from collectors.keyword_radar import intake_noise_reason

    assert intake_noise_reason('中国企業による対日投資の方法を教えて下さい',
        '中国企業向け手続。日本での会社の種類：株式会社。中国に子会社を設立する手順を紹介。'
    ) == 'noise_reverse_expansion'


@pytest.mark.django_db
def test_official_b_recheck_hides_original_only_while_replacement_is_visible():
    from radar.sales_pools import intake_grades

    original = 'https://example.org/results.pdf#page=3&radar-row=1'
    official = 'https://example.com/factory'
    candidate = KeywordCandidate.objects.create(
        canonical_url=original, title='海外市场项目', snippet='补贴采择名单', provider='grant',
        result={'sales_recheck':{'decision':'promoted_b','replacement_url':official}},
    )
    replacement = {'source_url':official,'grade':'B','score':60,'detail_url':'/research/1/'}

    assert intake_grades([{'cards':[replacement]}]) == {'A':[],'B':[replacement],'C':[]}
    assert intake_grades([])['C'][0]['candidate_id'] == candidate.pk
    assert KeywordCandidate.objects.filter(pk=candidate.pk, canonical_url=original).exists()
