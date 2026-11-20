import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from django.utils import timezone
from lxml.html import fromstring

from radar import brief_translation
from radar.analysis import process_pending_analyses
from radar.ai_config import config
from radar.mimo_tiers import FLASH, PRO
from radar.models import AnalysisRun, KeywordCandidate, ModelCall, SalesFeedback, SearchCall, Source, SourceRun
from radar.operations_stats import distribution, search_stats, source_rows, storage_stats, token_rows


@pytest.mark.django_db
def test_settings_stats_and_page_language(client, settings, tmp_path):
    ModelCall.objects.create(model=FLASH,status='success',usage={'prompt_tokens':100,'cached_tokens':25,'completion_tokens':30,'total_tokens':130},cost='0.002')
    ModelCall.objects.create(model=PRO,status='success',usage={'prompt_tokens':200,'cached_tokens':10,'completion_tokens':50,'total_tokens':250})
    ModelCall.objects.create(model=PRO,status='success',requested_at=timezone.now()-timedelta(days=9),usage={'total_tokens':28})
    SearchCall.objects.create(status='succeeded')
    SearchCall.objects.create(status='failed',error_code='timeout_billing_unknown')
    stats=client.get('/ai/settings/?range=7&format=json').json()
    assert [r['count'] for r in stats['tokens']]==[1,1]
    assert stats['tokens'][0]['cache_rate']==25.0 and stats['tokens'][0]['total_tokens']==130
    assert stats['search']['total']==2 and stats['search']['overall_rate']==50.0
    assert not stats['search']['enabled'] and stats['search']['last_failure']
    assert token_rows('all',timezone.localdate())[1]['total_tokens']==278
    assert 'cost' not in json.dumps(stats)

    source=Source.objects.create(key='fixture',name='公开来源')
    for _ in range(3):
        SourceRun.objects.create(source=source,status='failed',finished_at=timezone.now(),discovered_count=8)
    cards=[{'provider_key':source.key,'ui_source':'公开来源','ui_region':'east','china_location':'苏州',
            'company_industry':'物流与贸易','feedback_status':'accepted','feedback_reason':''},
           {'provider_key':source.key,'ui_source':'公开来源','ui_region':'other','china_location':'',
            'company_industry':'','feedback_status':'rejected','feedback_reason':'window_missed'}]
    row=next(r for r in source_rows(cards) if r['name']==source.name)
    assert (row['seen'],row['ab'],row['useful'],row['effective'])==(24,2,1,8.3)
    assert row['alerts']==['最近采集出错','连续 3 次无新增']
    groups=distribution(cards)
    assert groups['regions'][0]['share']==50.0 and groups['regions'][-1]['count']==1
    assert groups['reasons']==[{'name':'窗口已过','count':1}]
    settings.DATA_DIR=tmp_path
    raw=tmp_path/'raw'; (raw/'probe_cache').mkdir(parents=True)
    (raw/'a.pdf').write_bytes(b'pdf'); (raw/'probe_cache'/'b.html').write_bytes(b'blocked')
    assert storage_stats()['archive']=={'count':1,'bytes':3}

    response=client.post('/language/',{'language':'ja','next':'/ai/settings/?range=7'})
    assert response['Location']=='/ai/settings/?range=7'
    page=fromstring(client.get(response['Location']).content)
    assert page.get('lang')=='ja'
    assert '利用状況・運用集計' in page.text_content() and 'キャッシュヒット率' in page.text_content()
    assert fromstring(client.get('/').content).get('lang')=='ja'
    page.xpath('//details[@class="advanced-settings"]')[0].drop_tree()
    assert 'mimo-v2.6-' not in fromstring(client.get('/').content).text_content()
    assert not any(x in page.text_content() for x in ['¥','估算成本','预估费用'])
    assert client.post('/language/',{'language':'zh','next':'//external.example/'}).get('Location')=='/'
    assert fromstring(client.get('/').content).get('lang')=='zh-CN'


@pytest.mark.django_db
def test_explicit_flash_translation_caches_single_call(client, monkeypatch):
    cfg=config();cfg.enabled=True;cfg.base_url='https://api.xiaomimimo.com/v1';cfg.model=FLASH
    cfg.save()
    case=KeywordCandidate.objects.create(canonical_url='https://example.org/project',title='样例',provider='issuer_ir_pdf')
    brief={'executive_brief':{'headline':'已核实项目'},'outreach_kit':{'call_script_zh':'您好','objection_reply_zh':'谢谢'}}
    run=AnalysisRun.objects.create(status='succeeded',task_type='analysis',request_key='fixture',input_snapshot={'candidate_id':case.pk},result={'analysis':{'brief':brief}})
    rendered=client.get(f'/sales-pools/candidate/{case.pk}/')
    assert rendered.status_code==200 and ModelCall.objects.count()==0
    translated={'executive_brief':{'headline':'確認済みの案件'},'outreach_kit':{'call_script_zh':'お世話になります','objection_reply_zh':'ありがとうございます'}}
    def handler(*args,**kwargs):
        payload=kwargs['json']
        assert payload['model']==FLASH and payload['thinking']=={'type':'disabled'}
        assert 'call_script_zh' in payload['messages'][0]['content']
        return httpx.Response(200,json={'usage':{'total_tokens':24},'choices':[{'message':{'content':json.dumps(translated)}}]})
    monkeypatch.setattr('radar.analysis.secret',lambda _:'local-test-secret')
    monkeypatch.setattr('httpx.Client.post',handler)
    endpoint=f'/ai/tasks/{run.pk}/translate/'
    def submit():
        return client.post(endpoint,{'target':'ja'},HTTP_ACCEPT='application/json')
    first=submit()
    assert first.status_code==202 and first.json()['created']
    assert submit().json()['created'] is False
    job,=process_pending_analyses()
    assert job.status=='succeeded' and job.result['brief']==translated and job.calls.count()==1
    client.post('/language/',{'language':'ja'})
    delivered=client.get(f'/sales-pools/candidate/{case.pk}/').content.decode()
    assert '日本語訳' in delivered and '確認済みの案件' in delivered and '原文（中国語）' in delivered
    assert '已核实项目' in client.get(f'/sales-pools/candidate/{case.pk}/?brief=original').content.decode()
    assert submit().json()=={'id':job.pk,'created':False,'status':'succeeded'}
    assert not process_pending_analyses() and ModelCall.objects.count()==1
    assert ('mimo-v2.6-' not in delivered and '¥' not in delivered)


@pytest.mark.django_db
def test_new_pro_records_selected_language(client):
    import uuid
    from radar.analysis import enqueue
    case=KeywordCandidate.objects.create(canonical_url='https://example.org/ir-pro',title='日企测试',provider='issuer_ir_pdf')
    cfg=config();cfg.base_url='https://api.xiaomimimo.com/v1';cfg.save()
    zh,_=enqueue(None,'analysis',str(uuid.uuid4()),candidate_id=case.pk,tier='pro')
    ja,_=enqueue(None,'analysis',str(uuid.uuid4()),candidate_id=case.pk,tier='pro',language='ja')
    assert zh.pk!=ja.pk and zh.input_snapshot['language']=='zh' and ja.input_snapshot['language']=='ja'


@pytest.mark.django_db
def test_japanese_brief_can_be_translated_to_chinese(client,monkeypatch):
    cfg=config();cfg.enabled=True;cfg.base_url='https://api.xiaomimimo.com/v1';cfg.model=FLASH;cfg.save()
    run=AnalysisRun.objects.create(status='succeeded',task_type='analysis',request_key='ja-fixture',
        input_snapshot={'language':'ja'},result={'analysis':{'brief':{'outreach_kit':{'email_body_ja':'ご担当者様'}}}})
    monkeypatch.setattr('radar.analysis.secret',lambda _:'local-test-secret')
    expected={'outreach_kit':{'email_body_ja':'负责人您好'}}
    def provider(*args,**kwargs):
        assert '字段名含 _ja 时也翻译成中文' in kwargs['json']['messages'][0]['content']
        return httpx.Response(200,json={'usage':{'total_tokens':20},'choices':[{'message':{'content':json.dumps(expected)}}]})
    monkeypatch.setattr('httpx.Client.post',provider)
    path=f'/ai/tasks/{run.pk}/translate/'
    assert client.post(path,{'target':'zh'},HTTP_ACCEPT='application/json').status_code==202
    job,=process_pending_analyses()
    assert job.result['brief']==expected and job.input_snapshot['language']=='zh'
    assert client.post(path,{'target':'zh'},HTTP_ACCEPT='application/json').status_code==200
    assert not process_pending_analyses() and job.calls.count()==1
