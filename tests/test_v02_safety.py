import io,json,uuid,zipfile
from unittest.mock import patch
import pytest,httpx
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject,DictionaryObject,NameObject
from django.core.management import call_command
from django.core.management.base import CommandError
from radar.models import *
from radar.enrichment import save_pdf
from radar.evidence import current_documents
from radar.public_fetch import fetch,FetchError
from radar.analysis import enqueue,process_pending_analyses
from radar.ai_config import config
from radar.management.commands.backup_data import snapshot_payload
from tests.test_v02 import setup_data

@pytest.mark.django_db
def test_pdf_real_text_and_ocr_status(setup_data):
    lead,_=setup_data
    writer=PdfWriter();page=writer.add_blank_page(width=400,height=300)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
    stream=DecodedStreamObject();stream.set_data(b'BT /F1 12 Tf 20 250 Td (Buyer: Contract Factory Ltd.) Tj ET');page[NameObject('/Contents')]=stream
    data=io.BytesIO();writer.write(data);doc=save_pdf(lead,data.getvalue())
    assert 'Contract Factory Ltd.' in doc.text and doc.metadata['pages'][0]['page']==1
    blank=PdfWriter();blank.add_blank_page(width=400,height=300);data=io.BytesIO();blank.write(data)
    doc=save_pdf(lead,data.getvalue());assert doc.metadata['status']=='ocr_required' and not doc.text

@pytest.mark.django_db
def test_conflicting_evidence_excluded(setup_data):
    lead,_=setup_data
    doc=Document.objects.create(lead=lead,content_hash='x'*64,text='wrong company',metadata={'kind':'linked_html','identity':'conflict_or_unverified'})
    assert doc not in current_documents(lead)

@pytest.mark.django_db(transaction=True)
def test_restore_cancels_pending_model_jobs_and_preserves_usage(tmp_path,settings):
    from radar.management.commands.backup_data import BUSINESS_MODELS
    settings.DATA_DIR=tmp_path
    s=Source.objects.create(key='test',name='synthetic');lead=Lead.objects.create(source=s,title='test',external_id='1',content_hash='a'*64)
    cfg=config();cfg.enabled=True;cfg.auto_new=True;cfg.key_ref='synthetic-ref';cfg.save()
    job,_=enqueue(lead.pk,'analysis',str(uuid.uuid4()));ModelCall.objects.create(analysis=job,status='uncertain',error_code='interrupted_billing_unknown')
    archive=tmp_path/'backup.zip';call_command('backup_data',output=str(archive))
    for model in reversed(BUSINESS_MODELS):model.objects.all().delete()
    call_command('restore_data',str(archive));job=AnalysisRun.objects.get()
    assert job.status=='cancelled' and job.error_code=='restored_no_replay'
    assert ModelCall.objects.count()==1 and not config().enabled and not config().key_ref
    with patch('radar.analysis.call_provider',side_effect=AssertionError('no replay')):assert process_pending_analyses()==[]

@pytest.mark.django_db
def test_worker_never_replays_interrupted_task(setup_data):
    lead,_=setup_data;job,_=enqueue(lead.pk,'analysis',str(uuid.uuid4()));job.status='running';job.save()
    with patch('radar.analysis.call_provider',side_effect=AssertionError('no replay')):process_pending_analyses()
    job.refresh_from_db();assert job.error_code=='interrupted_billing_unknown'

def test_public_redirect_to_private_address_blocked(monkeypatch,tmp_path):
    original=httpx.Client
    monkeypatch.setenv('LEAD_PUBLIC_RATE_DIR',str(tmp_path))
    from radar.public_fetch import clear_robots_cache
    clear_robots_cache()
    monkeypatch.setattr('radar.public_fetch.socket.getaddrinfo',lambda *a,**k:[(2,1,6,'',('93.184.216.34',443))] if a[0]=='public.example' else [(2,1,6,'',('127.0.0.1',80))])
    monkeypatch.setattr('radar.public_fetch.httpx.Client',lambda **kwargs:original(**kwargs,transport=httpx.MockTransport(lambda r:httpx.Response(404) if r.url.path=='/robots.txt' else httpx.Response(302,headers={'Location':'http://internal.example'}))))
    with pytest.raises(FetchError,match='non_public_address'):fetch('https://public.example')

@pytest.mark.parametrize('title,text,expected',[
 ('关于测试制造有限公司运输服务招标公告','1.招标人：测试制造有限公司','测试制造有限公司'),
 ('运输服务公告','代理单位受测试制造有限公司（以下简称“招标人”）委托\n招 标 人：测试制造有限公司','测试制造有限公司'),
 ('食材配送服务公告','1.采购人信息\n名称：天津市公安局河北分局机关','天津市公安局河北分局机关'),
])
def test_real_layout_regressions(title,text,expected):
    from radar.extraction import extract_event
    assert extract_event(title,text)['buyer_name']==expected

def test_registration_in_bid_instructions_is_not_recruitment():
    from radar.extraction import extract_event
    assert extract_event('派送项目谈判公告','供应商注册后参加采购')['event_type']=='tender'
    assert extract_event('运输服务中标候选人公示','')['event_type']=='award'


def test_deadline_mention_never_borrows_publication_start():
    from radar.extraction import extract_event
    text='采购人：测试制造有限公司\n请于公告截止时间前完成注册。\n采购公告发布时间为2026年11月12日至2030年11月18日23时59分\n请在公告截止时间前登录平台 https://example.com 报名。'
    e=extract_event('运输服务谈判公告',text)
    assert e['deadline'].startswith('2030-11-18') and e['deadline_kind']=='registration'

def test_paragraphs_inside_table_cells_stay_in_row():
    from collectors.sources import node_text,html_tree
    text=node_text(html_tree('<table><tr><td><p>采购人</p></td><td><p><b>测试</b>制造有限公司</p></td></tr></table>'))
    assert text=='采购人 | 测试制造有限公司'


def test_layout_table_keeps_article_paragraphs():
    from collectors.sources import node_text,html_tree
    text=node_text(html_tree('<table><tr><td><p>'+('正文'*250)+'</p><p>报名方式：https://example.com</p></td></tr></table>'))
    assert '\n报名方式' in text


@pytest.mark.django_db
def test_duplicate_links_preserve_reviews_and_event_identity(setup_data):
    from radar.dedup import link_duplicates
    from radar.models import Review
    lead,_=setup_data
    lead.title='2030年运输服务第一标段招标公告';lead.text='项目编号：AB-123456';lead.save()
    same=Lead.objects.create(source=lead.source,external_id='2',title=lead.title,text=lead.text,review_status='contacted')
    Review.objects.create(lead=same,status='contacted',note='合成反馈：实际联络')
    year=Lead.objects.create(source=lead.source,external_id='3',title=lead.title.replace('2030','2031'),text=lead.text)
    stage=Lead.objects.create(source=lead.source,external_id='4',title=lead.title.replace('招标公告','中标公告'),text=lead.text)
    synthetic=Lead.objects.create(source=lead.source,external_id='5',title=lead.title,text=lead.text,is_test=True)
    assert link_duplicates()==[[same.pk,lead.pk]]
    same.refresh_from_db();assert same.review_status=='contacted' and same.reviews.count()==1
    for obj in [year,stage,synthetic]:
        obj.refresh_from_db();assert obj.duplicate_of_id is None
    assert link_duplicates()==[]


def test_site_delivery_acceptance_is_not_bid_participation():
    from radar.extraction import extract_event
    e=extract_event('运输招标公告','采购人：测试制造有限公司\n供应商需在北京市测试路自有仓库送货，并经现场验收。\n报名方式：https://example.com/apply')
    assert e['participation_path']=='https://example.com/apply'

def test_file_purchase_end_precedes_bid_deadline():
    from radar.extraction import extract_event
    e=extract_event('运输招标公告','购买招标文件时间：2030年1月1日起至2030年1月10日17:00期间\n投标截止时间：2030年1月20日10:00')
    assert e['deadline'].startswith('2030-01-10') and e['deadline_kind']=='registration'

def test_separate_file_sale_heading_finds_expired_registration_before_future_bid():
    from datetime import date
    from radar.extraction import extract_event
    text=('采购人：测试制造有限公司\n八、招标文件的发售时间及地点等：\n'
          '1.时间：自本公告发布之日起至2030年1月12日止，上午9:00-11:30，下午14:00-17:00。\n'
          '投标截止时间：2030年1月27日14:00\n报名方式：https://example.com/apply')
    event=extract_event('测试制造有限公司物流运输招标公告',text,as_of=date(2030,1,14))
    assert event['deadline']=='2030-01-12' and event['deadline_kind']=='registration'
    assert not event['deadline_has_time']
    assert event['processing_status']=='needs_review' and 'registration_closed' in event['missing_fields']
    assert any('2030年1月27日' in x['quote'] for x in event['evidence'] if x['field']=='deadline')
    assert extract_event('测试制造有限公司物流运输招标公告',text,as_of=date(2030,1,28))['processing_status']=='closed'

def test_get_file_window_in_prose_precedes_bid():
    from datetime import date
    from radar.extraction import extract_event
    text=('采购人：测试制造有限公司\n获取招标文件\n获取文件流程：平台注册后缴费。\n'
          '凡有意参加投标者，请于2030年1月1日至2030年1月10日，每日上午9时至12时，下午13时至17时，通过平台获取招标文件。\n'
          '投标文件截止时间：2030年1月20日09:30\n报名方式：https://example.com/apply')
    event=extract_event('测试制造有限公司物流运输招标公告',text,as_of=date(2030,1,5))
    assert event['deadline']=='2030-01-10' and event['deadline_kind']=='registration'
    assert not event['deadline_has_time'] and event['bid_deadline'].startswith('2030-01-20')

def test_title_and_location_do_not_confirm_procurement_role():
    from radar.extraction import extract_event
    from radar.entities import subject_reason
    e=extract_event('测试制造有限公司纯碱产品装卸服务采购公告','项目所在地：测试制造有限公司\n物料：装卸服务')
    assert e['buyer_name'] is None
    assert any(x['method']=='title_candidate' for x in e['entities'])
    assert subject_reason(e,'项目所在地：测试制造有限公司')=='buyer_role_unconfirmed'
    assert extract_event('办公设施设备耗材配送及维修服务采购公告','')['event_type']=='irrelevant'

@pytest.mark.django_db
def test_linked_pdf_cannot_borrow_parent_title_to_pass_identity(setup_data,monkeypatch):
    from radar.enrichment import complete_subject
    lead,cfg=setup_data
    lead.title='2030年运输服务招标公告';lead.metadata={'attachments':[{'url':'https://example.com/old.pdf'}]};lead.save()
    doc=Document.objects.create(lead=lead,content_hash='z'*64,text='2029年运输服务招标公告\n采购方：其他制造有限公司',metadata={'kind':'pdf'})
    monkeypatch.setattr('radar.enrichment.robots_allowed',lambda u:True)
    monkeypatch.setattr('radar.enrichment.fetch',lambda *a,**k:{'body':b'%PDF-fake','url':'https://example.com/old.pdf'})
    monkeypatch.setattr('radar.enrichment.save_pdf',lambda *a,**k:doc)
    monkeypatch.setattr('radar.enrichment.search',lambda *a:([],'search_not_configured'))
    job,_=enqueue(lead.pk,'enrich',str(uuid.uuid4()))
    result=complete_subject(job,cfg);doc.refresh_from_db()
    assert doc.metadata['identity']=='conflict_or_unverified'
    assert any(s.get('status')=='year_conflict' for s in result['trace'])

@pytest.mark.django_db
def test_connection_test_calls_provider_with_bounded_request(setup_data,monkeypatch):
    from tests.test_v02 import provider
    from radar.analysis import process_analysis
    lead,cfg=setup_data
    cfg.enabled=False;cfg.save()
    def handler(request):
        payload=__import__('json').loads(request.content)
        assert payload['max_tokens']<=128
        return httpx.Response(200,json={'model':'contract-test','usage':{'total_tokens':8},'choices':[{'message':{'content':'{"ok":true}'}}]})
    provider(monkeypatch,handler)
    job,_=enqueue(None,'test',str(uuid.uuid4()));process_analysis(job.pk)
    cfg.refresh_from_db();assert cfg.test_status=='callable'
    assert ModelCall.objects.count()==1
