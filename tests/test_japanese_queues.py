"""Synthetic cases for the candidate pool; never included in business counts."""
import pytest
from datetime import date
from radar.models import Source,Company,Lead
from radar.queues import japan_signal,select_queue,business_leads

@pytest.mark.django_db
def test_priority_pool_separates_real_signals_from_plain_jobs_and_unrelated_roles(client):
    source=Source.objects.create(key='synthetic-jobs',name='合成来源',kind='procurement')
    early=Source.objects.create(key='synthetic-park',name='合成公示',kind='early_signal')
    confirmed=Company.objects.create(name='测试制造有限公司',ownership_status='japan_confirmed',ownership_evidence='人工核对的合成股权依据')
    seed=Company.objects.create(name='电装测试有限公司')
    peer=Company.objects.create(name='日通物流测试有限公司')
    def row(n,company,src=source,**kw):
        return Lead.objects.create(source=src,company=company,external_id=str(n),content_hash=f'{n:064x}',**kw)
    tender=row(1,confirmed,title='供应商运输服务招标',event_type='tender',processing_status='needs_review',deadline=date(2099,1,1))
    expired=row(2,confirmed,title='历史运输招标',event_type='tender',processing_status='closed',deadline=date(2020,1,1))
    expansion=row(3,seed,src=early,title='电装测试有限公司新建生产线环境公示',text='建设单位：电装测试有限公司，新建生产线',event_type='early_signal',processing_status='early_signal')
    job=row(4,seed,title='物流经理招聘',text='物流经理负责新仓筹建及供应商切换',event_type='unknown',metadata={'signal_type':'job'})
    ordinary=row(5,seed,title='财务招聘',text='财务会计岗位，负责税务',event_type='unknown',metadata={'signal_type':'job'})
    peer_job=row(6,peer,title='仓库主管招聘',text='负责危险品仓库操作',event_type='unknown',metadata={'signal_type':'job'})
    no_buyer=row(7,None,title='电装仓储服务招标',event_type='tender',processing_status='needs_review')
    excluded=row(8,seed,src=early,title='员工宿舍新建项目',text='新建宿舍',event_type='early_signal',processing_status='early_signal')
    synthetic=row(9,seed,title='测试物流招标',event_type='tender',processing_status='needs_review',is_test=True)
    duplicate=row(10,seed,title='重复物流招标',event_type='tender',processing_status='needs_review',duplicate_of=tender)
    selected=list(select_queue(business_leads().select_related('company','source'),'japan').values_list('id',flat=True))
    assert set(selected)=={tender.pk,expansion.pk,job.pk,peer_job.pk}
    assert selected[0]==tender.pk
    assert japan_signal(peer_job)['role']=='logistics_peer'
    assert japan_signal(expansion)['level']=='seed'
    response=client.get('/leads/?queue=japan')
    assert response.status_code==200
    text=response.content.decode()
    assert '日企主动拓客池（信号驱动）' in text
    assert '日企字号关联 · 背景待核实' in text
    assert '日系物流同行（二级分包机会）· 关联待核实' in text
    assert '财务招聘' not in client.get('/leads/?queue=japan').content.decode()
    assert Company.objects.get(pk=seed.pk).ownership_status=='unknown'

@pytest.mark.django_db
def test_directory_hit_and_demand_must_both_exist():
    s=Source.objects.create(key='synthetic-dir-test',name='合成需求来源',kind='procurement')
    c=Company.objects.create(name='目录测试有限公司',discovery_evidence=[{'source':'shfia_directory','status':'japan_affiliation_unverified','url':'https://example.com','quote':'目录测试有限公司'}])
    lead=Lead.objects.create(source=s,company=c,external_id='1',title='运输服务询价采购公告',event_type='rfq',processing_status='needs_review',content_hash='a'*64)
    assert japan_signal(lead)['level']=='directory'
    lead.event_type='unknown';lead.title='普通企业介绍';lead.save()
    assert japan_signal(lead) is None
    assert c.ownership_status=='unknown'

@pytest.mark.django_db
def test_japanese_language_job_signals_need_logistics_and_real_change():
    source=Source.objects.create(key='synthetic-japanese-jobs',name='合成招聘',kind='procurement')
    company=Company.objects.create(name='电装测试有限公司')
    peer=Company.objects.create(name='日通物流测试有限公司')
    def job(n,title,text,owner=company):
        return Lead.objects.create(source=source,company=owner,external_id=str(n),content_hash=f'{n:064x}',title=title,text=text,event_type='unknown')
    expansion=job(1,'通関・輸出入担当の求人','事業拡大に伴い、輸出入の通関業務を担当します。')
    warehouse=job(2,'倉庫管理職の求人','新倉庫の立上げと物流委託先切替を担当します。')
    ordinary=job(3,'物流担当の求人','既存の出荷伝票を確認します。')
    peer_job=job(4,'危険物倉庫管理の求人','危険物の倉庫作業を担当します。',peer)
    assert japan_signal(expansion)['signal'].startswith('物流岗位')
    assert japan_signal(warehouse)['signal'].startswith('物流岗位')
    assert japan_signal(ordinary)['signal']=='物流岗位信号 · 时效与需求待核实'
    assert japan_signal(peer_job)['role']=='logistics_peer'
