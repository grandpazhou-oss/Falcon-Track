import pytest

from collectors import aichi_kyoto_overseas as regional, prd_east_gov_expansion as gov
from collectors.smrj_subsidy_global import SMRJ, award_links, award_rows
from collectors.tdnet_biggo import DAYS, parse_disclosure


def test_tdnet_only_uses_verified_listing_shape_and_named_details():
    url='https://finance.biggo.jp/news/jpx_tdnet_123'
    document='会社名：例工業株式会社\n証券コード：1234\n発表日：2026年10月7日\n中国上海で子会社を設立する。'
    assert DAYS==180
    result=parse_disclosure('適時開示',document,url)
    assert result['company']=='例工業株式会社' and result['security_code']=='1234'
    assert result['published']=='2026-10-07' and result['region']=='上海'
    assert parse_disclosure('適時開示',document.replace('中国上海','大阪'),url) is None
    assert parse_disclosure('適時開示',document,'https://finance.biggo.jp.example/news/jpx_tdnet_123') is None


def test_subsidy_reads_project_column_not_a_keyword_in_company_name():
    from collectors.china_entry_evidence import OVERSEAS

    page={'page':1,'text':'エリア  都道府県  市町村  事業者名   法人番号      主たる業種  連携体    事業計画名      認定支援機関名\n'
        '中部    愛知県    例市    株式会社海外企画     1111111111111  食品製造    国産店舗改装    支援機関\n'
        '中部    愛知県    例市    株式会社例食品       2222222222222  食品製造      中国向け食品輸出    支援機関'}
    continuation={'page':2,'text':'中部    愛知県    例市    株式会社例物流     3333333333333  運輸業    国内倉庫の改修    支援機関'}
    rows=award_rows([page,continuation],'smrj')
    assert len(rows)==3 and rows[-1]['page']==2
    assert [r['company'] for r in rows if OVERSEAS.search(r['project'])]==['株式会社例食品']
    raw=b'<a href="/docs/shinjigyou_saitakukekka_04.pdf">list</a><a href="https://else.example/other.pdf">other</a>'
    assert len(award_links(raw,SMRJ))==1


def test_regional_discovery_scopes_domain_and_keeps_overseas_details():
    raw='''<a href="/uploaded/attachment/1.pdf">【上海】中国市場の報告</a>
    <a href="/uploaded/attachment/2.pdf">名古屋地域報告</a>
    <a href="https://else.example/uploaded/attachment/3.pdf">中国</a>'''.encode()
    found,total=regional.discovery(raw,regional.LETTER)
    assert total==2 and len(found)==1 and found[0][0].endswith('/1.pdf')


def test_government_project_requires_project_side_japanese_evidence():
    assert gov.screened('扩产项目公示','日资企业例某正式扩产并投产。')
    assert not gov.screened('扩产项目公示','广州某企业宣布扩产。拟采购日本企业的设备。')
    assert not gov.screened('扩产项目公示','广州某企业宣布扩产。日资企业参观。')
    assert not gov.screened('扩产公示','韩国ABC株式会社正式扩产。')
    assert gov.disclosed_company('日本跨国企业例会社在东莞的工厂正式投产')=='例会社'
    assert not gov.official('https://gz.gov.cn.example/path')


@pytest.mark.django_db
def test_admit_keeps_baseline_untouched_and_uses_existing_noise_grades():
    from collectors.china_entry_sources import admit
    from radar.models import KeywordCandidate
    from radar.sales_pools import intake_card

    doc={'url':'https://example.org/source.pdf','sha256':'a'*64,'raw_path':'raw/research/demo.pdf'}
    base=KeywordCandidate.objects.create(canonical_url=doc['url'],title='legacy',snippet='review')
    data={'document':doc,'url':doc['url'],'title':'上位ランキング','text':'業種分布アンケート',
          'company':'株式会社例','published':''}
    assert admit('smrj_subsidy_global',data) is None
    base.refresh_from_db()
    assert base.title=='legacy'
    data.update(url=doc['url']+'#page=2&radar-row=2222222222222',unique_document_row=True)
    row=admit('smrj_subsidy_global',data)
    assert row and admit('smrj_subsidy_global',data) is None
    assert admit('smrj_subsidy_global',{**data,'url':data['url'].replace('source.pdf','%E5%90%8D%E7%B0%BF.pdf')}) is None
    assert intake_card(row)['grade']=='C'
    assert row.result['source_url']==doc['url']
    info=admit('aichi_kyoto_overseas',{
        'url':'https://example.org/region','title':'上海の事業レポート','text':'株式会社例は上海で工場を新設。',
        'source_scope':'regional_reference','document':doc,
    })
    assert intake_card(info)['grade']=='C'


def test_huangpu_extracts_the_original_body_alongside_attachment_list():
    from collectors.china_entry_evidence import article

    text='广州开发区行政审批局已批准某公司检测实验室项目环境影响评价文件，现将审批的具体内容进行公布，详见附件。'
    raw=('<h1>环境影响公示</h1><div id="zoomcon"><p>'+text+'</p></div>'
         '<div class="article_documents">附件下载</div>').encode()
    row=article({'body':raw,'content_type':'text/html'},{'url':'http://www.hp.gov.cn/a'},min_chars=40)
    assert text in row['text']
