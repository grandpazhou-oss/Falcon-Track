import json
import pytest
from radar.models import Company,Source,Lead
from radar.roster_import import import_roster
from radar.entities import classify_japanese_link,organizations,japanese_link_for_lead
from radar.queues import japan_signal,select_queue
from radar.seed_dictionary import export_dictionary,matches,name_terms
from collectors.japanese_public import job_listing,job_detail,dfmc_detail,expansion_hits


def response(text='名录',url='https://example.org/roster'):
    return {'body':text.encode(),'url':url}


@pytest.mark.django_db
def test_project_change_link_requires_same_buyer_source_and_number(client):
    source=Source.objects.create(key='dfmc_nissan',name='合成采购')
    buyer=Company.objects.create(name='合成汽车有限公司')
    other=Company.objects.create(name='合成另一企业有限公司')
    original=Lead.objects.create(source=source,external_id='original',title='合成项目原公告',company=buyer,metadata={'project_number':'SYNTH-001'})
    change=Lead.objects.create(source=source,external_id='change',title='合成项目变更公告',company=buyer,metadata={'project_number':'SYNTH-001'})
    Lead.objects.create(source=source,external_id='unrelated',title='不应关联的其他采购人',company=other,metadata={'project_number':'SYNTH-001'})
    html=client.get(f'/leads/{original.pk}/').content.decode()
    assert f'/leads/{change.pk}/' in html and change.title in html
    assert '不应关联的其他采购人' not in html


@pytest.mark.django_db
def test_roster_fifty_continuation_changes_and_normalized_replay(tmp_path,settings):
    settings.DATA_DIR=tmp_path
    s=Source.objects.create(key='japan_directory_test',name='合成名单',kind='enterprise_discovery')
    rows=[{'name':f'苏州合成样本{i}有限公司'} for i in range(60)]
    a=import_roster(s,rows,response(),basis='official_zone_list')
    assert a['stats']['companies_new']==50 and a['stats']['rows_remaining']==10
    reordered=[{'name':'苏州新增样本有限公司'}]+list(reversed(rows))
    b=import_roster(s,reordered,response('第二版'),basis='official_zone_list')
    assert b['stats']['companies_new']==11 and b['stats']['coverage_complete']
    c=import_roster(s,reordered,response('第二版'),basis='official_zone_list')
    assert c['stats']['companies_new']==0 and Company.objects.count()==61


@pytest.mark.django_db
def test_mixed_roster_preserves_manual_and_excludes_non_japanese_dictionary(tmp_path,settings):
    settings.DATA_DIR=tmp_path
    s=Source.objects.create(key='shfia_members',name='合成协会',kind='enterprise_discovery')
    c=Company.objects.create(name='合成（上海）有限公司',profile={'industry':'人工'},ownership_evidence='人工证据')
    rows=[{'name':'合成(上海)有限公司'},{'name':'无锡合成幼儿园'},{'name':'合成上海有限公司无锡分公司'}]
    result=import_roster(s,rows,response(),basis='association_list',japanese=False)
    c.refresh_from_db()
    assert Company.objects.count()==2 and c.profile['industry']=='人工' and c.ownership_evidence=='人工证据'
    assert result['stats']['outcomes'][1]['outcome']=='retained_nonenterprise'
    assert classify_japanese_link(c.name,c.discovery_evidence)['level']=='none'
    assert export_dictionary()['company_count']==0


@pytest.mark.django_db
def test_dictionary_evidence_and_ambiguous_brand(tmp_path,settings):
    settings.DATA_DIR=tmp_path
    s=Source.objects.create(key='japan_directory_test',name='合成名单')
    import_roster(s,[{'name':'上海合成光学有限公司'},{'name':'佳能合成(苏州)有限公司'},{'name':'佳能合成(无锡)有限公司'}],response(),basis='official_zone_list')
    data=export_dictionary()
    assert data['company_count']==3
    assert matches('上海合成光学有限公司扩产')[0]['basis']=='full_name'
    assert next(x for x in data['terms'] if x['term']=='佳能')['ambiguous']
    assert '中国' not in name_terms('中国物流有限公司')


def test_roles_parentheses_and_fullwidth_numbering():
    entities=organizations('合成运输采购','由采购人（郑州日产汽车有限公司）签订合同。\n十五．业主单位：郑州日产汽车有限公司\n采购代理机构：测试咨询有限公司')
    assert {x['name'] for x in entities if x['role']=='buyer'}=={'郑州日产汽车有限公司'}


def test_numbered_invitation_is_not_award_and_preserves_file_window():
    from radar.extraction import extract_event
    text='询比采购公告\nXXXX：（被邀请单位名称）\n1.2采购人：合成汽车有限公司 1.3采购代理机构：合成咨询有限公司\n采购人按下列原则选择成交供应商：\n采购人依据推荐顺序确定成交供应商。\n4.2供应商在2027年11月10日23时59分至2027年11月13日23时59分，登录平台后在网上下载询比采购文件。\n5.1响应文件递交截止时间（以下简称“截止时间”）为2027-11-16 09:30。'
    raw=('<html><meta charset="utf-8"><div class="article"><h3>合成海外车辆出口空运项目</h3><div class="article-info">'+text.replace('\n','<br>')+'</div></div></html>').encode()
    item=dfmc_detail(raw,{'title':'合成海外车辆出口空运项目','infoid':'test','infourl':'/jyxx/test.html'},'https://example.org')
    event=extract_event(item['title'],item['text'],'procurement')
    assert event['event_type']=='rfq' and event['buyer_name']=='合成汽车有限公司'
    assert event['deadline']=='2027-11-13T23:59:00+08:00'
    assert event['bid_deadline']=='2027-11-16T09:30:00+08:00'
    assert item['metadata']['invitation_required']


def test_recruiter_full_text_ownership_claim_remains_a_platform_claim():
    body='<html><meta charset="utf-8"><div>某危化品制造企业</div><h1>供应链部长</h1><p>上海</p><h2>职位描述</h2><p>全日资企业，负责危险品物流。</p></html>'.encode()
    item=job_detail(body,'https://example.org/job-321','321',['日语 供应链'])
    assert item['metadata']['japanese_employer_claim']=='全日资企业'
    assert not item['metadata']['employer_name']


@pytest.mark.django_db
def test_anonymous_job_is_retained_and_reextract_does_not_invent_company(tmp_path,settings):
    from radar.services import ingest_item
    from radar.evidence import reextract
    settings.DATA_DIR=tmp_path
    s=Source.objects.create(key='persol_jobs',name='合成招聘',kind='recruitment')
    body='<html><meta charset="utf-8"><body><div>某日企</div><h1>物流主管</h1><p>上海</p><h2>职位描述</h2><p>新设仓库管理，物流与关务协调，切换3PL供应商。</p><h2>职位要求</h2><p>三年工作经验。</p></body></html>'.encode()
    item=job_detail(body,'https://persolchina.career.gllue.com/jobs/test-123','123',['日企 物流'])
    a,new,_=ingest_item(s,item);assert new and a.company_id is None
    assert japanese_link_for_lead(a)['level']=='platform_claim' and japan_signal(a)
    item['metadata']['matched_queries']=['日企 关务']
    b,new,updated=ingest_item(s,item);assert not new and not updated
    assert b.metadata['matched_queries']==['日企 关务','日企 物流'] and b.documents.count()==1
    reextract(a.pk);a.refresh_from_db();assert a.company_id is None and a.buyer_role=='unknown'


@pytest.mark.django_db
def test_invitation_multiple_deadlines_survive_reextract(tmp_path,settings):
    from radar.services import ingest_item
    from radar.evidence import reextract
    settings.DATA_DIR=tmp_path
    s=Source.objects.create(key='dfmc_nissan',name='合成采购',kind='procurement')
    text='采购人（郑州日产汽车有限公司）。本项目邀请询比采购。仓储运输服务。\n采购文件获取时间：2027年11月10日10时00分至2027年11月15日18时00分\n报价截止时间：2027年11月19日9时30分\n供应商资格：道路运输经营许可证\n报名方式：登录平台报名。'
    raw=('<html><meta charset="utf-8"><div class="article"><h3>合成运输采购</h3><div class="article-info">'+text.replace('\n','<br>')+'</div></div></html>').encode()
    item=dfmc_detail(raw,{'title':'合成运输采购','infoid':'test','infourl':'/jyxx/test.html','infodate':'2027-11-09'},s.url)
    lead,_,_=ingest_item(s,item)
    assert lead.company.name=='郑州日产汽车有限公司' and lead.processing_status=='needs_review'
    assert lead.deadline.isoformat()=='2027-11-15' and len(lead.metadata['deadlines'])==2
    assert lead.metadata['invitation_required'] and select_queue(Lead.objects.all(),'procurement').count()==0
    reextract(lead.pk);lead.refresh_from_db();assert lead.processing_status=='needs_review'


def test_job_list_dedup_and_no_apply_url():
    data='<html><meta charset="utf-8"><a href="./jobs/test-12">日企 物流 上海</a><a href="./jobs/test-12">重复</a><a href="/jobs/test-12/apply">投递</a></html>'.encode()
    rows=job_listing(data,'https://persolchina.career.gllue.com/jobs')
    assert len(rows)==1 and rows[0][1]=='https://persolchina.career.gllue.com/jobs/test-12'


def test_non_enterprise_and_branches():
    from radar.roster_import import entity_type
    assert entity_type('合成幼儿园')=='other_organization'
    assert entity_type('合成有限公司苏州分公司')=='branch'


@pytest.mark.django_db
def test_expansion_unidentified_owner_retains_weak_hit(tmp_path,settings):
    settings.DATA_DIR=tmp_path
    s=Source.objects.create(key='japan_directory_test',name='合成目录')
    import_roster(s,[{'name':'苏州特制样本有限公司'}],response(),basis='official_zone_list')
    export_dictionary()
    assert expansion_hits('建设公示','苏州特制样本有限公司新建工厂')
    assert not expansion_hits('建设公示','苏州特制样本有限公司新闻活动')


def test_post_redirect_never_forwards_form_to_other_host(monkeypatch):
    import time
    import radar.public_fetch as mod
    monkeypatch.setattr(mod,'validate_url',lambda url:(None,'8.8.8.8'))
    monkeypatch.setattr(mod,'_robots_allowed',lambda *a:True)
    monkeypatch.setattr(mod,'_request_once',lambda *a,**k:{'status':307,'location':'https://elsewhere.example/path'})
    with pytest.raises(mod.FetchError,match='cross_host_post_redirect'):mod.fetch('https://example.org/list',form={'q':'test'})


@pytest.mark.django_db(transaction=True)
def test_backup_includes_collection_list_evidence(tmp_path,settings):
    from radar.models import SourceRun
    from radar.management.commands.backup_data import snapshot_payload
    settings.DATA_DIR=tmp_path
    s=Source.objects.create(key='synthetic-public-source',name='合成')
    SourceRun.objects.create(source=s,status='success',stats={'requests':[{'url':'https://example.org/list','raw_path':'raw/source_lists/test.html'}]})
    _,paths,_=snapshot_payload()
    assert 'raw/source_lists/test.html' in paths


@pytest.mark.django_db
def test_language_hints_do_not_claim_japanese_ownership(tmp_path,settings):
    settings.DATA_DIR=tmp_path
    s=Source.objects.create(key='persol_jobs',name='合成招聘',kind='recruitment')
    lead=Lead.objects.create(source=s,title='关务岗位招聘',text='需要日语，负责进出口与关务',external_id='x',content_hash='a'*64,metadata={'signal_type':'job','language_hint':'日语'})
    link=japanese_link_for_lead(lead)
    assert link['level']=='language_hint' and not link['ownership_proven']
    assert japan_signal(lead)


def test_column_table_roles_do_not_swap_consultant_and_owner():
    text='序号 | 项目名称 | 建设单位 | 环评机构\n1 | 扩建项目 | 电装（广州南沙）有限公司 | 合成环保咨询有限公司'
    entities=organizations('项目公示',text)
    assert any(e['role']=='project_owner' and e['name']=='电装（广州南沙）有限公司' for e in entities)
    assert any(e['role']=='environmental_consultant' and e['name']=='合成环保咨询有限公司' for e in entities)
    assert not any(e['role']=='buyer' for e in entities)
