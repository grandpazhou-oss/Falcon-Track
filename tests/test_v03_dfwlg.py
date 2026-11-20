import pytest
from collectors.dfwlg import detail,listing


def test_peer_source_separates_customer_from_buyer_and_marks_notice_period():
    body=('''<html><head><meta charset="utf-8"></head><div class="newsnr"><h1>备件仓储运输项目</h1>
    <p class="source">发布时间：2026-01-01 10:00</p><p>项目需求：为东风日产提供备件仓储运输服务。
    采购方式：询比采购。供应商应当提供合法营业执照和道路运输经营许可证，具备长期稳定物流服务经验。
    公示期限：自发布本公示起48小时。响应文件递交方式另行通知。</p></div></html>''').encode()
    item=detail(body,{'id':'123','url':'https://www.dfwlg.com/info/123','title':'备件运输','published':'2026-01-01'})
    assert item['metadata']['notice_period_end']=='2026-01-03T10:00:00+08:00'
    assert item['metadata']['source_role']=='logistics_provider_subcontract'
    assert '客户并不自动是采购方' in item['metadata']['source_role_note']
    assert item.get('listed_deadline') is None and '东风日产' in item['text']


def test_listing_does_not_treat_logistics_department_equipment_as_service():
    body='''<html><head><meta charset="utf-8"></head><ul class="h-6"><li><a href="/info/1"><div class="h-13">生产物流三部焊装业务外包<span>2026-11-01</span></div><div class="h-14">焊装服务</div></a></li>
    <li><a href="/info/2"><div class="h-13">备件仓储运输服务<span>2026-11-02</span></div><div class="h-14">公开询比</div></a></li></ul><a href="/node/330_2">2</a></html>'''.encode()
    rows,more,count=listing(body,1)
    assert [x['id'] for x in rows]==['2'] and more and count==2


def test_peer_source_keeps_backlog_across_runs(monkeypatch):
    from types import SimpleNamespace
    import collectors.dfwlg as c
    body='''<html><head><meta charset="utf-8"></head><ul class="h-6">'''+''.join(f'<li><a href="/info/{i}"><div class="h-13">仓储运输项目{i}<span>2026-11-01</span></div></a></li>' for i in range(1,4))+'''</ul></html>'''
    def read(url,**kwargs):
        if '/node/' in url:content=body
        else:content='<html><head><meta charset="utf-8"></head><div class="newsnr"><h1>仓储运输项目</h1><p class="source">发布时间：2026-11-01 10:00</p><p>仓储运输服务采购，供应商需具有道路运输资质。'+('物流业务说明'*20)+'</p></div></html>'
        return {'body':content.encode()}, {'url':url,'sha256':'a'*64,'raw_path':'raw/list.html'}
    monkeypatch.setattr(c,'read',read)
    source=SimpleNamespace(options={'head_pages':1,'max_accepted_items':1},checkpoint={})
    batches=[]
    for _ in range(4):
        result=c.collect_dfwlg(source);source.checkpoint.update(result['collector_checkpoint']);batches.append([i['external_id'] for i in result['items']])
    assert batches==[['1'],['2'],['3'],[]]


@pytest.mark.django_db
def test_expired_publication_period_is_archived_without_inventing_bid_deadline():
    from radar.models import Source,Lead
    from radar.queues import select_queue,business_leads
    source=Source.objects.create(key='dfwlg_procurement',name='同行公开采购')
    lead=Lead.objects.create(source=source,external_id='1',title='日产仓储运输',event_type='rfq',processing_status='needs_review',
        metadata={'notice_period_end':'2020-01-03T10:00:00+08:00','source_role':'logistics_provider_subcontract'})
    assert lead.deadline is None and not lead.company_id
    assert select_queue(business_leads(),'archive').filter(pk=lead.pk).exists()
    assert not select_queue(business_leads(),'subject').filter(pk=lead.pk).exists()
