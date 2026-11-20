import uuid
import pytest
from radar.models import Source,Lead,AnalysisRun,ModelCall

@pytest.mark.django_db
def test_saved_analysis_is_visible_on_task_page_without_new_call(client,monkeypatch):
    monkeypatch.setattr('radar.analysis.call_provider',lambda *args:pytest.fail('Displaying saved results must not call a provider'))
    source=Source.objects.create(key='synthetic-display',name='合成来源')
    lead=Lead.objects.create(source=source,external_id='display',title='合成运输废标公告',content_hash='x'*64)
    run=AnalysisRun.objects.create(lead=lead,task_type='analysis',status='succeeded',request_key=str(uuid.uuid4()),result={
        'model':'synthetic-model','usage':{'prompt_tokens':3568,'completion_tokens':1374,'total_tokens':4942},'cost':None,
        'analysis':{'facts':[{'field':'阶段','value':'废标','quote':'有效投标人不足三家','block_id':'synthetic:block'}],
            'inferences':[{'text':'后续重招待确认','evidence_ids':['synthetic:block']}],
            'capability_match':[],'questions':['资质待核实'],'next_steps':['核对后续公告<script>unsafe()</script>']}})
    for _ in range(2):
        response=client.get('/ai/tasks/');assert response.status_code==200
        content=response.content.decode()
        assert '有效投标人不足三家' in content and '后续重招待确认' in content
        assert '资质待核实' in content and '下一步建议' in content
        assert '<details open><summary>查看分析结果（已生成）</summary>' in content
        assert f'/leads/{lead.pk}/#analysis-{run.pk}' in content
        assert '输入 3568 token · 输出 1374 token · 合计 4942 token' in content
        assert 'completion_tokens' not in content and '费用' not in content and 'synthetic-model' not in content
        assert '<script>unsafe()' not in content and '&lt;script&gt;' in content
    detail=client.get(f'/leads/{lead.pk}/').content.decode()
    assert f'id="analysis-{run.pk}"' in detail and '有效投标人不足三家' in detail
    run.refresh_from_db();assert run.status=='succeeded'
    assert AnalysisRun.objects.count()==1 and ModelCall.objects.count()==0

@pytest.mark.django_db
def test_zero_usage_and_missing_usage_are_distinct_on_task_page(client):
    AnalysisRun.objects.create(task_type='test',status='succeeded',request_key=str(uuid.uuid4()),result={'usage':{'prompt_tokens':0,'completion_tokens':0}})
    content=client.get('/ai/tasks/').content.decode()
    assert '输入 0 token · 输出 0 token · 合计 未知 token' in content
    AnalysisRun.objects.create(task_type='test',status='failed',error_code='disabled',request_key=str(uuid.uuid4()))
    content=client.get('/ai/tasks/').content.decode()
    assert '暂无用量记录（未调用或供应商未返回）' in content
    assert '请先在 AI 设置中启用分析' in content

@pytest.mark.django_db
def test_legacy_rejected_response_shows_actual_call_usage(client):
    run=AnalysisRun.objects.create(task_type='analysis',status='failed',error_code='unsupported_fact',request_key=str(uuid.uuid4()))
    ModelCall.objects.create(analysis=run,status='failed',model='returned-model',usage={'prompt_tokens':10,'completion_tokens':5,'total_tokens':15},error_code='unsupported_fact')
    content=client.get('/ai/tasks/').content.decode()
    assert 'returned-model' not in content
    assert '输入 10 token · 输出 5 token · 合计 15 token' in content
    assert '此历史任务未保存逐项核对明细' in content

@pytest.mark.django_db
def test_task_list_marks_analysis_stale_after_reextraction_without_changing_history(client):
    source=Source.objects.create(key='synthetic-stale',name='合成来源')
    lead=Lead.objects.create(source=source,external_id='stale',title='运输公告',content_hash='a'*64,extraction_version=2)
    run=AnalysisRun.objects.create(lead=lead,task_type='analysis',status='succeeded',request_key=str(uuid.uuid4()),
        input_snapshot={'source_hash':lead.content_hash,'extraction_version':1},capability_snapshot={'version':1})
    assert '输入已过期，请重新分析' in client.get('/ai/tasks/').content.decode()
    run.refresh_from_db();assert not run.stale

@pytest.mark.django_db
def test_historical_mixed_price_question_is_flagged_on_display_without_editing_it(client):
    source=Source.objects.create(key='synthetic-price',name='合成来源')
    lead=Lead.objects.create(source=source,external_id='price',title='运输公告',content_hash='a'*64)
    sentence='招标文件售价500元、投标保证金具体金额及提交方式未在本公告中列明，需查阅招标文件确认。'
    run=AnalysisRun.objects.create(lead=lead,task_type='analysis',status='succeeded',request_key=str(uuid.uuid4()),
        input_snapshot={'source_hash':lead.content_hash,'evidence':{'blocks':[{'id':'b1','document_id':1,'text':'招标文件售价 500 元。'}]}},
        result={'analysis':{'facts':[],'inferences':[],'capability_match':[],'questions':[sentence],'next_steps':[]}})
    for path in ['/ai/tasks/',f'/leads/{lead.pk}/']:
        content=client.get(path).content.decode()
        assert '已披露的文件售价与未披露事项混写' in content
        assert '原始任务和结果未修改' in content
        if path=='/ai/tasks/':assert '查看时发现历史条目需复核，展开结果可查看' in content
    run.refresh_from_db();assert run.status=='succeeded' and run.result['analysis']['questions']==[sentence]
    assert ModelCall.objects.count()==0

@pytest.mark.django_db
def test_no_verified_buyer_is_visible_as_unknown(client):
    source=Source.objects.create(key='synthetic-unknown-buyer',name='合成来源')
    lead=Lead.objects.create(source=source,external_id='no-buyer',title='中盐长江运输询比',content_hash='a'*64)
    run=AnalysisRun.objects.create(lead=lead,task_type='analysis',status='succeeded',request_key=str(uuid.uuid4()),
        input_snapshot={'source_hash':lead.content_hash,'evidence':{'blocks':[{'id':'b1','document_id':1,'text':'物流运输服务'}]}},
        result={'analysis':{'facts':[{'field':'服务','value':'物流运输服务','block_id':'b1','quote':'物流运输服务'}]}})
    for path in ['/ai/tasks/',f'/leads/{lead.pk}/']:
        assert '本次分析未给出可核对的采购主体' in client.get(path).content.decode()
    run.refresh_from_db();assert len(run.result['analysis']['facts'])==1
