from datetime import timedelta

import pytest
from django.utils import timezone

from collectors.issuer_ir_pdf import parse_pdf_disclosure
from collectors.sme_sources import company_scale, project_quote, rss_index
from radar.china_places import SEARCH_CITIES, project_city, region_weight
from radar.models import KeywordCandidate, SalesFeedback


@pytest.mark.parametrize('code', ['', '証券コード：1234'])
def test_issuer_pdf_does_not_require_listing_code(code):
    today=timezone.localdate()-timedelta(days=2)
    text=(f'{today.year}年{today.month}月{today.day}日\n'
          f'会社名：試験精密株式会社\n{code}\n'
          '中国蘇州の新工場を2027年4月に稼働開始予定です。')
    parsed=parse_pdf_disclosure('新工場の稼働について', [{'page':3,'text':text}])
    assert parsed and parsed['company']=='試験精密株式会社'
    assert parsed['region']=='蘇州' and parsed['target_period']=='2027-04'
    assert parsed['pdf_page']==3 and parsed['security_code']==('1234' if code else '')


@pytest.mark.parametrize('alias,city,points',[
    ('寧波','宁波',10),('紹興','绍兴',10),('南京','南京',10),('広徳','广德',10),
    ('深セン','深圳',6),('江門','江门',6),('肇慶','肇庆',6),('惠州','惠州',6),
])
def test_shared_city_aliases(alias,city,points):
    text=f'本社：北京\n{alias}工場開設'
    assert project_city(text)==city and region_weight(text)==points and city in SEARCH_CITIES


def test_hq_does_not_count_as_project_city():
    assert not project_city('本社：上海市\n国内販売を継続')
    assert region_weight('本社：上海市\n国内販売を継続')==0


def test_rdf_only_returns_official_article_urls():
    xml=b'''<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/">
    <item><title>x</title><link>https://www.atpress.ne.jp/news/7</link><corp>issuer</corp></item>
    <item><link>https://other.example/news/7</link></item></rdf:RDF>'''
    assert [r['company'] for r in rss_index(xml)]==['issuer']
    assert not project_quote('中国進出支援サービスのセミナーを開設します。')
    assert project_quote('中国広州に新工場を開設する計画です。')


@pytest.mark.django_db
def test_sales_feedback_is_separate_from_evidence_and_reason_is_required(client):
    case=KeywordCandidate.objects.create(canonical_url='https://evidence.example/test',
        title='日系企業が上海で工場を新設します',snippet='日系企業が上海で工場を新設します',
        provider='test',query_id='test')
    card=f'/sales-pools/candidate/{case.pk}/'
    path='/sales-pools/feedback/'
    assert client.post(path,{'card':card,'status':'rejected'}).status_code==400
    assert client.post(path,{'card':card,'status':'rejected','reason':'window_missed'}).status_code==302
    result=client.get('/sales-pools/export.json').json()
    assert result['grades']['B'][0]['feedback_reason']=='window_missed'
    assert result['sales_feedback']['reasons']['window_missed']==1
    assert client.post(path,{'card':card,'status':'accepted'}).status_code==302
    assert SalesFeedback.objects.get().reason==''
    case.refresh_from_db()
    assert case.result=={}


def test_declaration_scale_records_current_revenue_and_keeps_project_column():
    assert company_scale('売上高100億円を目指す。従業員：88名\n〇現在の売上高：56.3億円') == {
        'employees':'88名','revenue':'56.3億円'}
    quote=project_quote('日本工場を新設します     中国・上海の販売網を拡充します。',declarations=True)
    assert quote=='中国・上海の販売網を拡充します'


def test_city_expansion_does_not_admit_market_survey():
    from collectors.keyword_radar import intake_noise_reason

    assert intake_noise_reason('日本企業の「中国進出」動向調査', '上海や杭州から撤退する企業も調査した。') == 'noise_industry_survey'


def test_batch_selects_unread_overseas_with_mainland_first():
    from collectors.sme_sources import unread_overseas

    rows=[{'url':url,'priority':priority} for url,priority in [('seen',2),('china',2),('other',1),('domestic',0)]]
    assert [row['url'] for row in unread_overseas(rows, {'seen'})]==['china','other']
