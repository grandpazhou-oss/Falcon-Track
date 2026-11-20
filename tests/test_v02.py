import copy,json,uuid,ipaddress
from functools import partial
from unittest.mock import patch
from datetime import date
import httpx,pytest
from django.utils import timezone
from django.core.management import call_command
from collectors.sources import node_text,html_tree
from radar.extraction import extract_event
from radar.entities import subject_reason
from radar.models import *
from radar.analysis import enqueue,process_analysis,process_pending_analyses
from radar.ai_config import config
from radar.evidence import select_evidence,reextract
from radar.enrichment import project_match,save_pdf
from radar.public_fetch import validate_url,FetchError

@pytest.fixture
def setup_data(tmp_path,settings,db):
    settings.DATA_DIR=tmp_path
    s=Source.objects.create(key='test',name='合成测试')
    text='采购方：测试制造有限公司\n报价截止：2030年1月1日\n报价方式：https://example.com/quote\n运输服务：整车与零担'
    lead=Lead.objects.create(source=s,title='测试制造有限公司运输询比采购公告',external_id='1',text=text,content_hash='a'*64)
    Document.objects.create(lead=lead,content_hash=lead.content_hash,text=text,metadata={'url':'https://example.com/notice'})
    cfg=config();cfg.enabled=True;cfg.base_url='https://model.example/v1';cfg.model='contract-model';cfg.save()
    return lead,cfg

def provider(monkeypatch,handler):
    original=httpx.Client
    monkeypatch.setattr('radar.analysis.httpx.Client',partial(original,transport=httpx.MockTransport(handler)))
    monkeypatch.setattr('radar.analysis.secret',lambda cfg:'synthetic-secret')

def valid_response(request):
    body=json.loads(request.content)
    assert body['model']=='contract-model'
    assert request.url.path=='/v1/chat/completions'
    assert 'synthetic-secret' not in request.content.decode()
    return httpx.Response(200,json={'model':'contract-model-returned','usage':{'prompt_tokens':9,'completion_tokens':5,'total_tokens':14},'choices':[{'message':{'content':json.dumps({'facts':[],'inferences':[],'capability_match':[],'questions':['尚需核对资质'],'next_steps':['核对正式采购入口']},ensure_ascii=False)}}]})

@pytest.mark.django_db
def test_japanese_sales_prompt_preserves_unknown_capability_and_signal_boundaries(setup_data,monkeypatch):
    lead,cfg=setup_data
    def handler(req):
        body=json.loads(req.content);prompt=body['messages'][0]['content']
        assert all(word in prompt for word in ['跨境空运','跨境海运','国内3PL仓配','危险品物流','招聘或扩产','破冰话术'])
        assert '不得把招聘或扩产当成已经外包采购' in prompt
        assert '未有证据不得断言具体人员、国籍、职务或授权' in prompt
        return valid_response(req)
    provider(monkeypatch,handler)
    run,_=enqueue(lead.pk,'analysis',str(uuid.uuid4()));process_analysis(run.pk);run.refresh_from_db()
    assert run.status=='succeeded' and not run.capability_snapshot['services']

def test_inline_and_table_roles():
    text=node_text(html_tree('<article><p>采购方：<b>测试</b><span>制造</span>有限公司（中国）</p><table><tr><td>招标代理机构</td><td>测试咨询有限公司</td></tr></table></article>'))
    assert '测试制造有限公司' in text
    event=extract_event('运输询比采购公告',text)
    assert event['event_type']=='rfq'
    assert event['buyer_name']=='测试制造有限公司'
    assert any(e['role']=='agent' for e in event['entities'])

def test_signal_does_not_require_bid_or_use_environment_contact():
    e=extract_event('测试工厂扩建生产线公示','建设单位：测试制造有限公司\n依据《江苏省环境条例》\n意见反馈截止：2030-01-01\n意见反馈邮箱：environment@example.com',source_kind='early_signal')
    assert e['processing_status']=='early_signal'
    assert not e['regions'] and e['deadline'] is None and e['participation_path'] is None
    assert 'qualifications' not in e['missing_fields']
    assert extract_event('学校宿舍建设公示','建设单位：测试学校',source_kind='early_signal')['processing_status']=='irrelevant'
    assert extract_event('学校配送服务招标','采购人：测试大学')['event_type']=='tender'

def test_production_change_only_in_signal_body():
    e=extract_event('测试半导体有限公司芯片封装新建项目环境公示',
                    '建设单位：测试半导体有限公司\n项目完成后预计年新增芯片封装100万颗。',source_kind='early_signal')
    assert e['processing_status']=='early_signal' and e['participation_path'] is None
    assert any('年新增芯片' in row['quote'] for row in e['evidence'])

def test_parenthesized_award_stays_in_history():
    e=extract_event('测试物流有限公司生产服务项目中标（入围）结果公告','原招标公告发布于2026年11月1日。')
    assert e['event_type']=='award' and e['processing_status']=='closed'

def test_open_ended_requires_explicit_evidence():
    text='招募人：测试制造有限公司\n报名方式：https://example.com'
    a=extract_event('运输承运商招募',text);b=extract_event('运输承运商招募',text+'\n长期开放报名')
    assert a['deadline_semantics']=='unknown' and a['processing_status']=='needs_review'
    assert b['deadline_semantics']=='open_ended' and b['processing_status']=='actionable'

def test_no_guess_and_identity_checks():
    e=extract_event('省内干线运输承运商项目询比采购公告','采购人：****\n会员全文')
    assert e['buyer_name'] is None
    assert subject_reason(e,'会员全文')=='login_or_membership_required'
    assert not project_match('2026年运输招标','','2025年运输招标','')[0]
    assert not project_match('2026年运输招标','','2026年运输中标结果','')[0]
    assert not project_match('2026年第1标段运输招标','','2026年第2标段运输招标','')[0]
    assert project_match('2026年物流运输服务招标公告','项目编号：AB-123456','2026年物流运输服务招标公告','项目编号：AB-123456')[0]

@pytest.mark.django_db
def test_old_record_reanalysis_and_double_click(setup_data,monkeypatch):
    lead,cfg=setup_data; provider(monkeypatch,valid_response)
    key=str(uuid.uuid4());a,new=enqueue(lead.pk,'analysis',key,force=True)
    b,new2=enqueue(lead.pk,'analysis',key,force=True)
    assert a.pk==b.pk and new and not new2
    process_analysis(a.pk);process_analysis(a.pk)
    assert ModelCall.objects.count()==1
    ordinary,_=enqueue(lead.pk,'analysis',str(uuid.uuid4()))
    assert ordinary.pk==a.pk
    c,_=enqueue(lead.pk,'analysis',str(uuid.uuid4()),force=True);process_analysis(c.pk)
    assert ModelCall.objects.count()==2
    lead.refresh_from_db();assert lead.version==1 and lead.content_hash=='a'*64
    a.refresh_from_db();assert a.status=='succeeded' and a.result['usage']['total_tokens']==14 and a.result['cost'] is None

@pytest.mark.django_db
def test_model_does_not_overwrite_concurrent_human_or_new_source(setup_data,monkeypatch):
    lead,cfg=setup_data
    def handler(req):
        company=Company.objects.create(name='人工确认有限公司')
        Lead.objects.filter(pk=lead.pk).update(company=company,review_status='contacted',metadata={'manual_buyer':True},content_hash='b'*64)
        return valid_response(req)
    provider(monkeypatch,handler)
    a,_=enqueue(lead.pk,'analysis',str(uuid.uuid4()));process_analysis(a.pk)
    a.refresh_from_db();lead.refresh_from_db()
    assert a.stale and lead.review_status=='contacted' and lead.company.name=='人工确认有限公司'

@pytest.mark.django_db
@pytest.mark.parametrize('mode,code',[('timeout','timeout_billing_unknown'),('429','rate_limited'),('json','invalid_response'),('fake','unsupported_fact')])
def test_failure_paths_do_not_retry(setup_data,monkeypatch,mode,code):
    lead,cfg=setup_data
    def handler(req):
        if mode=='timeout':raise httpx.ReadTimeout('synthetic-secret',request=req)
        if mode=='429':return httpx.Response(429,text='synthetic-secret')
        if mode=='json':return httpx.Response(200,json={})
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'facts':[{'field':'buyer','value':'虚构企业','quote':'虚构企业','block_id':'missing'}]})}}]})
    provider(monkeypatch,handler)
    a,_=enqueue(lead.pk,'analysis',str(uuid.uuid4()));process_pending_analyses();process_pending_analyses()
    a.refresh_from_db();assert a.error_code==code and ModelCall.objects.count()==1
    assert 'synthetic-secret' not in json.dumps(a.result)

@pytest.mark.django_db
def test_budget_and_unconfigured_use_no_network(setup_data,monkeypatch):
    lead,cfg=setup_data;provider(monkeypatch,lambda r:pytest.fail('must not call network'))
    cfg.daily_limit=0;cfg.save();a,_=enqueue(lead.pk,'analysis',str(uuid.uuid4()));process_analysis(a.pk);a.refresh_from_db()
    assert a.error_code=='daily_limit' and ModelCall.objects.count()==0
    monkeypatch.setattr('radar.analysis.secret',lambda cfg:'')
    b,_=enqueue(lead.pk,'analysis',str(uuid.uuid4()),force=True);process_analysis(b.pk);b.refresh_from_db()
    assert b.error_code=='not_configured'

@pytest.mark.django_db
def test_web_enqueues_only_and_replays_safely(setup_data,client,monkeypatch):
    lead,cfg=setup_data;provider(monkeypatch,lambda r:pytest.fail('web must not call provider'))
    data={'task_type':'analysis','request_key':str(uuid.uuid4()),'lead_ids':[lead.pk]}
    assert client.post('/ai/enqueue/',data).status_code==302
    assert client.post('/ai/enqueue/',data).status_code==302
    assert AnalysisRun.objects.count()==1 and ModelCall.objects.count()==0
    for path in ['/ai/settings/','/ai/tasks/','/capability/',f'/leads/{lead.pk}/','/leads/','/']:
        r=client.get(path);assert r.status_code==200,path
        assert b'synthetic-secret' not in r.content

@pytest.mark.django_db
def test_late_page_selection_and_uncovered_ranges(setup_data):
    lead,cfg=setup_data
    pages=[{'page':i,'text':'background '*900} for i in range(1,5)]+[{'page':5,'text':'采购方：后页制造有限公司；报名方式：https://example.com'}]
    Document.objects.create(lead=lead,content_hash='p'*64,text='\n'.join(p['text'] for p in pages),metadata={'kind':'pdf','pages':pages})
    result=select_evidence(lead,2000)
    assert any(b.get('page')==5 for b in result['blocks'])
    assert result['unprocessed_ids'] and not result['full_coverage']

@pytest.mark.django_db
def test_reextract_preserves_acquisition_and_manual(setup_data):
    lead,cfg=setup_data;company=Company.objects.create(name='人工企业')
    lead.company=company;lead.review_status='contacted';lead.metadata={'manual_buyer':True};lead.save()
    reextract(lead.pk);lead.refresh_from_db()
    assert lead.company==company and lead.review_status=='contacted' and lead.version==1 and lead.content_hash=='a'*64
    assert ExtractionRun.objects.count()==1

@pytest.mark.parametrize('url',['http://127.0.0.1/a','http://169.254.169.254/','file:///etc/passwd','http://[::1]/','http://user:pass@example.com/'])
def test_public_fetch_rejects_local_and_credentials(url):
    with pytest.raises(FetchError):validate_url(url)


@pytest.mark.django_db
@pytest.mark.parametrize('services,quote,expected',[
    ('','冷链运输','failed'),
    ('整车运输','冷链运输','failed'),
    ('整车运输','整车运输','succeeded'),
])
def test_capability_match_requires_user_supplied_quote(setup_data,monkeypatch,services,quote,expected):
    from radar.ai_config import capability
    from radar.analysis import PROMPT_VERSION,SCHEMA_VERSION
    lead,cfg=setup_data
    cap=capability();cap.services=services;cap.save()
    def handler(req):
        body=json.loads(req.content)
        evidence=json.loads(body['messages'][1]['content'])['evidence']['blocks'][0]['id']
        result={'capability_match':[{'text':'能力匹配建议','evidence_ids':[evidence],'capability_quote':quote}]}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result)}}]})
    provider(monkeypatch,handler)
    run,_=enqueue(lead.pk,'analysis',str(uuid.uuid4()));process_analysis(run.pk);run.refresh_from_db()
    assert run.status==expected
    assert run.prompt_version==PROMPT_VERSION and run.schema_version==SCHEMA_VERSION
    if expected=='failed':assert run.error_code=='unsupported_capability' and 'analysis' not in run.result
    assert ModelCall.objects.count()==1

@pytest.mark.django_db
def test_rejected_fact_preserves_usage_and_diagnostic_without_adopting_it(setup_data,monkeypatch,client):
    lead,cfg=setup_data
    def handler(req):
        block=json.loads(json.loads(req.content)['messages'][1]['content'])['evidence']['blocks'][0]
        result={'facts':[{'field':'buyer','value':'无依据公司','quote':'无依据公司','block_id':block['id']}]}
        return httpx.Response(200,json={'model':'returned-model','usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15},'choices':[{'message':{'content':json.dumps(result)}}]})
    provider(monkeypatch,handler)
    run,_=enqueue(lead.pk,'analysis',str(uuid.uuid4()));process_analysis(run.pk);run.refresh_from_db()
    assert run.status=='failed' and run.error_code=='unsupported_fact'
    assert run.result['usage']['total_tokens']==15 and run.result['model']=='returned-model'
    assert 'analysis' not in run.result and run.result['validation_errors'][0]['value']=='无依据公司'
    assert '采购方：测试制造有限公司' in run.result['validation_errors'][0]['source_text']
    lead.refresh_from_db();assert lead.company_id is None
    content=client.get('/ai/tasks/').content.decode()
    assert '本次内容未作为分析事实采用' in content and '模型摘录不是所引用原文的连续原句' in content

@pytest.mark.django_db
def test_partial_analysis_quarantines_bad_fields_and_reuses_without_rebilling(setup_data,monkeypatch,client):
    lead,cfg=setup_data
    def handler(req):
        block=json.loads(json.loads(req.content)['messages'][1]['content'])['evidence']['blocks'][0]
        result={'facts':[{'field':'buyer','value':'测试制造有限公司','quote':block['text'],'block_id':block['id']},
                         {'field':'phone','value':'虚构电话','quote':'虚构电话','block_id':block['id']}],
                'inferences':[{'text':'无依据推断','evidence_ids':['missing']}]}
        return httpx.Response(200,json={'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15},'choices':[{'message':{'content':json.dumps(result)}}]})
    provider(monkeypatch,handler)
    run,_=enqueue(lead.pk,'analysis',str(uuid.uuid4()));process_analysis(run.pk);run.refresh_from_db()
    assert run.status=='needs_review' and run.error_code=='partial_validation'
    assert len(run.result['analysis']['facts'])==1 and not run.result['analysis']['inferences']
    assert len(run.result['validation_errors'])==2
    assert len(run.result['candidate_analysis']['facts'])==2
    same,new=enqueue(lead.pk,'analysis',str(uuid.uuid4()));assert same.pk==run.pk and not new
    assert ModelCall.objects.count()==1
    content=client.get('/ai/tasks/').content.decode()
    assert '已生成，部分内容待复核' in content and '其他通过校验的内容保留供复核' in content
    assert '查看分析结果（已生成）' in content and '待复核 1' in content
    assert 'buyer：测试制造有限公司' in content and 'phone：虚构电话' not in content
    lead.refresh_from_db();assert lead.company_id is None

@pytest.mark.django_db
@pytest.mark.parametrize('official',[True,False])
def test_mimo_parameters_and_complete_evidence_ids(setup_data,monkeypatch,official):
    lead,cfg=setup_data
    cfg.base_url='https://api.xiaomimimo.com/v1' if official else 'https://gateway.example/v1'
    cfg.model='mimo-v2.6-flash';cfg.save()
    def handler(req):
        body=json.loads(req.content)
        if official:
            assert body['thinking']=={'type':'disabled'} and body['max_completion_tokens']==cfg.max_tokens
            assert 'max_tokens' not in body
        else:
            assert 'thinking' not in body and body['max_tokens']==cfg.max_tokens
        assert json.loads(body['messages'][1]['content'])['analysis_date']==timezone.localdate().isoformat()
        block=json.loads(body['messages'][1]['content'])['evidence']['blocks'][0]
        assert set(block)=={'id','text'} and block['id'].endswith(':c1')
        result={'facts':[{'field':'buyer','value':'测试制造有限公司','quote':block['text'],'block_id':block['id']}]}
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(result)}}]})
    provider(monkeypatch,handler)
    run,_=enqueue(lead.pk,'analysis',str(uuid.uuid4()));process_analysis(run.pk);run.refresh_from_db()
    assert run.status=='succeeded' and run.result['finish_reason']=='stop'
    assert run.input_snapshot['evidence']['blocks'][0]['hash']==lead.content_hash

@pytest.mark.django_db
def test_truncated_response_keeps_usage_without_adopting_or_retrying(setup_data,monkeypatch):
    lead,cfg=setup_data
    provider(monkeypatch,lambda req:httpx.Response(200,json={'usage':{'completion_tokens':4096},
        'choices':[{'finish_reason':'length','message':{'content':'{"facts":['}}]}))
    run,_=enqueue(lead.pk,'analysis',str(uuid.uuid4()));process_pending_analyses();process_pending_analyses();run.refresh_from_db()
    assert run.status=='failed' and run.error_code=='output_truncated'
    assert run.result['usage']['completion_tokens']==4096 and run.result['finish_reason']=='length'
    assert 'analysis' not in run.result and ModelCall.objects.count()==1

@pytest.mark.django_db
@pytest.mark.parametrize('question,source,issue',[
    ('招标文件售价500元、投标保证金具体金额及提交方式未在本公告中列明，需查阅招标文件确认。',
     '招标文件售价 500 元（人民币）。\n投标人必须按招标文件规定的方式及金额提交投标保证金。','已披露的文件售价与未披露事项混写'),
    ('本项目报名截止时间已过（2026年11月20日23时59分59秒），是否仍在报名期？',
     '采购公告截止时间为2026年11月20日23时59分59秒。','截止日期晚于分析日期'),
    ('纸质投标文件仅限当面递交。',
     '选择以邮寄方式递交纸质版投标文件的，投标人需确认文件已被接收。','公告还允许邮寄'),
    ('须以自有运力履约，外协运力模式不适用。',
     '投标人投入本项目的自有车辆不得少于10台。\n中标后不得转包或分包。','外协'),
    ('本项目属于零担/整车运输需求。',
     '物流运输服务，起运地长江埠，目的地北京。','整车或零担'),
    ('当前仍处于可购标、可投标的时间窗口内。',
     '购买招标文件时间：2026年11月5日起至2026年11月20日17:00期间（办公时间内，法定节假日除外）。','办公时间'),
])
def test_contradictory_model_questions_are_quarantined(setup_data,monkeypatch,question,source,issue):
    lead,cfg=setup_data
    doc=lead.documents.first()
    if issue=='整车或零担':doc.text=doc.text.replace('运输服务：整车与零担','运输服务：普通公路运输')
    doc.text+='\n'+source;doc.save()
    monkeypatch.setattr('radar.analysis.timezone.localdate',lambda:date(2026,11,13))
    def handler(req):
        block=json.loads(json.loads(req.content)['messages'][1]['content'])['evidence']['blocks'][0]
        result={'facts':[{'field':'buyer','value':'测试制造有限公司','quote':block['text'],'block_id':block['id']}],
                'questions':[question]}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result,ensure_ascii=False)}}]})
    provider(monkeypatch,handler)
    run,_=enqueue(lead.pk,'analysis',str(uuid.uuid4()));process_analysis(run.pk);run.refresh_from_db()
    assert run.status=='needs_review'
    assert question not in run.result['analysis']['questions']
    assert any(issue in item['reason'] and item['value']==question for item in run.result['validation_errors'])

def test_semantic_filter_does_not_hide_separate_unknown_or_conditional_date():
    from radar.analysis import semantic_issue
    blocks={'b1':'招标文件售价 500 元（人民币）。'}
    assert semantic_issue('招标文件售价500元，投标保证金金额未在公告中列明。',blocks,'2026-11-13') is None
    assert semantic_issue('请核实报名截止时间是否已过（2026年11月20日）。',blocks,'2026-11-13') is None
    assert semantic_issue('报名截止时间已过（2026年11月10日）。',blocks,'2026-11-13') is None
    assert semantic_issue('纸质投标文件可当面或邮寄递交。',{'b':'选择以邮寄方式递交纸质投标文件。'},'2026-11-13') is None
    assert semantic_issue('仅限当面递交。',{'b':'不得选择以邮寄方式递交纸质投标文件。'},'2026-11-13') is None
    assert semantic_issue('外协运力模式不适用。',{'b':'本项目禁止使用外协运力。'},'2026-11-13') is None
    assert semantic_issue('是否需要零担或整车运输，需核实。',{'b':'运输服务。'},'2026-11-13') is None
    assert semantic_issue('前置获取截止已过，按公告条件不得断言当前仍可报名。',
        {'b':'购买招标文件时间：2026年11月20日，办公时间内。'},'2026-11-14') is None
