from datetime import date
from types import SimpleNamespace

from collectors import japan_finance as finance, smrj_subsidy_global as subsidy
from radar.analysis import generation_parameters
from radar.mimo_tiers import FLASH, PRO


def test_award_batches_use_dated_mapping_and_resume_oldest_after_bound(monkeypatch):
    index = b'''<table>
    <tr><td>2026&#24180;8&#26376;7&#26085;</td><td><a href="/ippan/23th/saitaku23ji_g.pdf">23</a></td></tr>
    <tr><td>2026&#24180;4&#26376;30&#26085;</td><td><a href="/ippan/22th/saitaku22ji_g.pdf">22</a></td></tr>
    <tr><td>2023&#24180;4&#26376;30&#26085;</td><td><a href="/ippan/13th/saitaku13ji_g.pdf">13</a></td></tr>
    </table>'''
    page = {'page': 8, 'text': (
        '事業者受付番号  都道府県    商号又は名称       法人番号         事業計画名        認定支援機関名\n'
        '361 R124220006 中部ブロック   三重県     合資会社水谷商店      8190003001647'
        '  ペタライト不使用中国向け土鍋開発と量産体制強化による輸出拡大     四日市商工会議所\n'
        '362 R124220007 中部ブロック   三重県     合名会社架空     9999999999999'
        '  国内の在日中国人向け飲食店     架空支援')}

    def read(url, *_args, **_kwargs):
        return {'url': url, 'body': index}, {'url': url, 'raw_path': 'raw/sample.pdf'}

    monkeypatch.setattr(subsidy, 'read', read)
    def pdf_pages(_path, _timeout, *, mode='layout'):
        if mode == 'table_layout':
            return {'pages': [page], 'backend': 'pypdf'}
        return {'pages': [{'page': 8, 'text': '事業計画名'}], 'backend': 'pymupdf'}

    monkeypatch.setattr(subsidy, 'pdf_pages', pdf_pages)
    monkeypatch.setattr('django.utils.timezone.localdate', lambda: date(2026, 11, 20))
    stats = {'errors': [], 'endpoints': []}
    seen = {}
    for publication in ('2026-08-07', '2026-04-30'):
        [row] = subsidy.collect(stats, 1, targets=(subsidy.MONO,), seen=seen)
        assert row['company'] == '合資会社水谷商店'
        assert row['pdf_page'] == 8 and row['award_publish_date'] == publication
        assert 'target_period' not in row
    assert subsidy.collect(stats, 1, targets=(subsidy.MONO,), seen=seen) == []
    assert subsidy.award_rows([page], 'mono')[1]['company'] == '合名会社架空'


def test_japanese_chugoku_is_never_mainland_by_itself():
    assert not finance.mainland_location('中国地方の広島・岡山・鳥取・島根・山口で中小企業に融資')
    assert finance.mainland_location('広島銀行が上海現地法人の設備投資を支援') == '上海'
    assert finance.mainland_location('恵州の現地法人') == '惠州'


def test_joint_event_deduplicates_across_bank_publishers(monkeypatch):
    index = ('<meta charset="UTF-8"><a href="https://east.example/event">上海合同商談会</a>'
             '<a href="https://west.example/notice">上海商談会</a>').encode()

    def read(url, *_args, **_kwargs):
        return {'url': url, 'body': index}, {'url': url, 'raw_path': 'raw/event.html'}

    def article(_response, document, title):
        return {'document': document, 'title': title, 'url': document['url'], 'published': '',
                'pages': [], 'text': title + '\n開催日：2026年11月30日'}

    monkeypatch.setattr(finance, 'read', read)
    monkeypatch.setattr(finance, 'article', article)
    stats = {'errors': [], 'endpoints': []}
    [event] = finance.collect('chiginkyo_news', stats, (finance.BANK,), 5, date(2026, 11, 20), ())
    assert event['source_scope'] == 'finance_meeting'
    assert stats['errors'][0]['status'] == 'duplicate_event'
    assert finance.collect('chiginkyo_news', stats, (finance.BANK,), 5, date(2026, 11, 20), [event['event_key']]) == []


def test_shoko_keeps_parent_employee_count_and_does_not_use_release_day_as_contract():
    text = ('商工中金は、例食品株式会社（本社：愛知県）の中国現地法人例食品（上海）貿易有限公司に'
            '運転資金を保証しました。\n【例食品株式会社の概要】\n従業員数 75 名\n'
            '【例食品（上海）貿易有限公司の概要】\n従業員数 26 名')
    row = finance.parse_finance(text, '2025-10-15', date(2026, 11, 20))
    assert row['company'] == '例食品株式会社' and row['employees'] == '75'
    assert row['china_entity'] == '例食品（上海）貿易有限公司'
    assert row['region'] == '上海' and row['finance_purpose'] == '運転資金'
    assert row['contract_date'] is None and row['historical_case']


def test_pro_reasoning_is_configurable_and_flash_always_off():
    config = SimpleNamespace(base_url='https://api.xiaomimimo.com/v1', model=PRO, max_tokens=1280, pro_thinking=True)
    params = generation_parameters(config, 'project_evidence')
    assert params['thinking'] == {'type': 'enabled'} and 'temperature' not in params
    config.pro_thinking = False
    assert generation_parameters(config, 'project_evidence')['thinking'] == {'type': 'disabled'}
    config.pro_thinking = True
    config.model = FLASH
    assert generation_parameters(config, 'semantic_prescreen')['thinking'] == {'type': 'disabled'}


def test_bank_public_search_follows_only_verified_same_site_get_form():
    from urllib.parse import parse_qs, urlsplit
    form = b'<form action="index.php" method="GET"><input name="freeword"></form>'
    searches = finance.bank_searches(form, finance.BANK)
    assert [parse_qs(urlsplit(url).query)['freeword'][0] for url in searches] == ['上海', '中国現地法人', '深圳']
    assert not finance.bank_searches(form.replace(b'GET', b'POST'), finance.BANK)
    assert not finance.bank_searches(form.replace(b'index.php', b'https://other.example/index.php'), finance.BANK)


def test_shoko_read_uses_same_site_year_index_and_reads_mainland_first(monkeypatch):
    case = finance.SHOKO_NEWS + '2026/'
    local = 'https://www.shokochukin.co.jp/assets/pdf/local.pdf'
    other = 'https://www.shokochukin.co.jp/assets/pdf/other.pdf'
    shell = b'<meta http-equiv="refresh" content="0;URL=/newsrelease/2026/">'
    index = (f'<meta charset="utf-8"><a href="{other}">ベトナム現地法人に融資</a>'
             f'<a href="{local}">上海現地法人に融資</a>').encode()
    visited = []

    def read(url, *_args, **_kwargs):
        visited.append(url)
        return {'url': url, 'body': shell if url == finance.SHOKO_NEWS else index}, {'url': url, 'raw_path': 'raw/research/example.pdf'}

    def article(response, doc, title):
        return {'url': doc['url'], 'document': doc, 'title': title, 'pages': [],
                'text': '例食品株式会社（本社：愛知県）の中国現地法人例食品（上海）貿易有限公司に運転資金を保証',
                'published': '2026-11-19'}

    monkeypatch.setattr(finance, 'read', read)
    monkeypatch.setattr(finance, 'article', article)
    stats = {'errors': [], 'endpoints': []}
    [row] = finance.collect('shokochukin_news', stats, (finance.SHOKO_NEWS,), 1, date(2026, 11, 20), ())
    assert visited == [finance.SHOKO_NEWS, case, local]
    assert row['region'] == '上海' and stats['details_read'][0]['url'] == local
    assert not finance.shoko_news_target(shell.replace(b'/newsrelease', b'//other.example/newsrelease'), finance.SHOKO_NEWS)


def test_event_date_normalizes_pdf_spacing_and_digits_without_using_release_date():
    original = '２０２６年５月１１日\n上海地銀八行商談会２０２６\n日  時  ２０２６年７月１０日\n申込期限 ２０２６年５月２２日'
    assert finance.event_identity(original) == 'joint-business-meeting:上海:2026-07-10'
    assert not finance.event_identity(original.replace('日  時  ２０２６年７月１０日', '日時未定'))


def test_smrj_short_company_name_requires_exact_legal_heading():
    from collectors.sme_sources import declaration_company_matches
    pages = [{'text': '計画\n甲乙商事について調査。'}, {'text': '甲乙株式会社（製造業）\n企業理念'}]
    assert declaration_company_matches('甲乙 株式会社', pages)
    assert not declaration_company_matches('甲乙 株式会社', pages[:1])
    assert not declaration_company_matches('甲乙 株式会社', [{'text': '新甲乙株式会社（製造業）'}])


def test_smrj_index_cache_is_only_used_for_transient_reads(monkeypatch):
    from collectors import sme_sources as sme
    calls = []
    source = SimpleNamespace(key='smrj_100oku', checkpoint={})
    monkeypatch.setattr(sme, 'cached_smrj_index', lambda *args: calls.append(args[1]) or b'<html/>')
    monkeypatch.setattr(sme, 'declaration_index', lambda body: [])
    for reason in ('robots_or_robots_unavailable:disallow', 'time_limit'):
        def unavailable(*args, **kwargs):
            raise ValueError(reason)
        monkeypatch.setattr(sme, 'read', unavailable)
        stats = {'errors': [], 'endpoints': []}
        rows, _ = sme.collect(source, stats, 1, (sme.SMRJ,))
        assert not rows and stats['errors'][0]['reason'] == reason
        if reason.startswith('robots'):
            assert not calls
    assert calls == [sme.SMRJ]


def test_declaration_checks_pdf_reading_order_without_changing_project_quote(monkeypatch):
    from collectors import sme_sources as sme
    import pytest

    company = '甲乙株式会社'
    quote = '中国上海に新工場を開設する計画です'
    layout = [{'page': 2, 'text': '甲乙株式会社（製造業）          実施体制\n'+quote}]
    monkeypatch.setattr(sme, 'article', lambda *_: {'text':layout[0]['text'], 'pages':layout,'published':''})
    def pages(path, *, mode):
        assert mode == 'plain'
        return {'pages':[{'page':2,'text':'甲乙株式会社（製造業）'}]}
    monkeypatch.setattr(sme, 'pdf_pages', pages)
    entry = {'company':company,'date':'2026-02-02'}
    # Short legal headings still require the existing identity validator in both modes.
    result = sme.disclosure(entry, {'body':b'%PDF-'}, {'raw_path':'raw/test.pdf'}, declarations=True)
    assert result['pdf_page']==2 and result['evidence_quote']==quote
    with pytest.raises(ValueError, match='declaration_company_not_confirmed'):
        sme.disclosure({**entry,'company':'丙丁株式会社'}, {'body':b'%PDF-'}, {'raw_path':'raw/test.pdf'}, declarations=True)
