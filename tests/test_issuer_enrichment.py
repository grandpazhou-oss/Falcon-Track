from datetime import date

import pytest

from collectors.issuer_ir_pdf import blocked, parse_disclosure
from collectors.smrj_subsidy_global import MONO, active_awards
from radar.company_enrichment import extract_profile, fields
from radar.models import Company, KeywordCandidate
from radar.research import _mainland_evidence


def test_issuer_extracts_named_manufacturer_and_katakana_city():
    text='会社名：株式会社試験工業\n証券コード：6311\n2026年10月、深センの新工場が稼働。'
    out=parse_disclosure('深セン新工場',text)
    assert (out['company'],out['security_code'],out['region'],out['target_period'])==(
        '株式会社試験工業','6311','深セン','2026-10')
    assert parse_disclosure('新工場','会社名：株式会社試験工業\n中国地方に工場を新設。') is None
    assert blocked('https://www2.jpx.co.jp/disc/sample.pdf')
    assert blocked('https://finance.biggo.jp/news/a')
    assert not blocked('https://example.co.jp/ir/report.pdf')


@pytest.mark.parametrize('city',['深セン','仏山','アモイ','廣德','宣城','張家港'])
def test_extra_city_variants(city):
    assert _mainland_evidence({'quote':city+'に工場新設'})


def test_profile_from_company_section_preserves_website_verification_state():
    text='会社概要\n会社名：株式会社試験食品\n事業内容：食品の製造\n公式URL：https://test.example.jp/company/'
    assert extract_profile('株式会社試験食品',text)==('食品饮料与酒类','https://test.example.jp/')
    assert extract_profile('株式会社無関係',text)==('','')
    assert not fields(Company(official_website='https://test.example.jp/',official_website_status='unverified'))['official_website']


def test_old_subsidy_document_removed_before_download():
    body="""<html><table>
      <tr><td>令和8年8月1日</td><td><a href="/common/bunsho/ippan/23th/採択者リスト_23次締切.pdf">採択者一覧</a></td></tr>
      <tr><td rowspan="2">令和5年2月20日</td><td>一般型</td></tr>
      <tr><td><a href="/common/bunsho/ippan/13th/saitaku13ji_g.pdf">採択者一覧</a></td></tr>
    </table></html>""".encode()
    links=active_awards(body,MONO,date(2026,11,19))
    assert len(links)==1
    assert list(links.values())==[date(2026,8,1)]


@pytest.mark.django_db
def test_enrichment_export_and_detail(client):
    item=KeywordCandidate.objects.create(canonical_url='https://example.net/source',
        title='公开候选',provider='issuer_ir_pdf',result={'company_industry':'食品饮料与酒类',
        'official_website':'https://example.org/','official_website_status':'verified'})
    html=client.get(f'/sales-pools/candidate/{item.pk}/').content.decode()
    assert all(x in html for x in ('公司官网','线索原文','食品饮料与酒类','https://example.org/','https://example.net/source'))
    result=client.get('/sales-pools/export.json').json()['grades']['C'][0]
    assert result['official_website_status']=='verified' and result['company_industry']=='食品饮料与酒类'
    assert 'official_website_status' in client.get('/sales-pools/export.csv').content.decode()


def test_expansion_queries_allow_html_and_scoped_tdnet():
    from collectors.issuer_ir_pdf import EXPANSION_QUERIES, nikkei_tdnet

    assert [key for key,_ in EXPANSION_QUERIES]==['S1','S2','E3','C4']
    assert all(6<=len(query.split())<=10 and 'filetype:' not in query for _,query in EXPANSION_QUERIES)
    assert nikkei_tdnet('https://www.nikkei.com/markets/ir/irftp/data/tdnr/tdnetg3/20261117/140120261116581688.pdf')
    assert not nikkei_tdnet('https://www.nikkei.com/markets/ir/irftp/data/tdnr/index.html')
    assert blocked('https://www.nikkei.com/article/x')


def test_pdf_disclosure_links_each_action_to_its_own_location(monkeypatch):
    from collectors.issuer_ir_pdf import parse_pdf_disclosure

    monkeypatch.setattr('collectors.issuer_ir_pdf.timezone.localdate', lambda:date(2026,11,19))
    cover={'page':1,'text':'株式会社試験機械\n証券コード：6155\n2026年10月8日\n決算説明資料'}
    project={'page':19,'text':'ベトナム\n2027年冬 操業開始\n中国（上海臨港）\n生産増強\n近隣に新建屋を賃借、工場を拡張（2027年春より稼働開始予定）'}
    hit=parse_pdf_disclosure('決算説明資料',[cover,project])
    assert (hit['region'],hit['pdf_page'],hit['target_period'])==('上海',19,'2027年春')
    no_project={'page':12,'text':'常州工場の稼働率は85%\n蘇州の人員数、イタリア工場移転'}
    assert parse_pdf_disclosure('決算説明資料',[cover,no_project]) is None
