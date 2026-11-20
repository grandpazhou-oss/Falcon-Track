import pytest
from radar.models import Company, Source
from radar.discovery import discover
from radar.entities import classify_japanese_link


def listing_cq():
    return '''<meta charset="utf-8"><div id="companyList">
    <div class="company-card"><h2 class="company-title">重庆丸全物流有限公司</h2><button onclick="toggleReadMore('11')"></button></div>
    <div class="company-card"><h2 class="company-title">本田测试有限公司</h2><button onclick="toggleReadMore('7')"></button></div>
    <div class="company-card"><h2 class="company-title">学校</h2><button onclick="toggleReadMore('9')"></button></div>
    </div>'''.encode()


def detail(name):
    return ('<html><meta charset="utf-8"><div class="content"><h1 class="main-heading">'+name+'</h1><h2 class="section-heading">企业概要</h2><p>'+'物流业务说明及背景资料。'*15+'</p></div></html>').encode()


@pytest.mark.django_db
def test_chongqing_details_are_preserved_and_directory_match_is_not_ownership(tmp_path,settings,monkeypatch):
    settings.DATA_DIR=tmp_path
    source=Source.objects.create(key='japan_directory_chongqing',name='重庆',url='https://cq-jpclub.com/company',kind='enterprise_discovery')
    existing=Company.objects.create(name='重庆丸全物流有限公司',ownership_status='japan_confirmed',ownership_evidence='人工证明',profile={'industry':'人工'},discovery_evidence=[{'source':'manual','quote':'原依据'}])
    def fake_fetch(url,**kwargs):
        if url==source.url:body=listing_cq()
        elif url.endswith('id=11'):body=detail('重庆丸全物流有限公司')
        elif url.endswith('id=7'):body=detail('本田测试有限公司')
        else:raise AssertionError(url)
        return {'body':body,'url':url}
    monkeypatch.setattr('radar.verified_japan_directories.fetch',fake_fetch)
    first=discover(source,limit=1);second=discover(source,limit=1);third=discover(source,limit=1)
    existing.refresh_from_db(); source.refresh_from_db()
    assert first['stats']['details_ok']==1 and second['stats']['companies_new']==1 and third['stats']['companies_new']==0
    assert existing.profile['industry']=='人工' and existing.ownership_evidence=='人工证明' and existing.discovery_evidence[0]=={'source':'manual','quote':'原依据'}
    evidence=existing.discovery_evidence[-1]
    assert evidence['url'].endswith('id=11') and evidence['quote']=='重庆丸全物流有限公司'
    assert (tmp_path/evidence['raw_path']).is_file()
    assert source.checkpoint['directory_offset']==1
    other=Company.objects.get(name='本田测试有限公司')
    match=classify_japanese_link(other.name,other.discovery_evidence)
    assert match['level']=='directory' and not match['ownership_proven'] and other.ownership_status=='unknown'
    assert not Company.objects.filter(name='学校').exists()


@pytest.mark.django_db
def test_chongqing_mismatch_or_network_failure_does_not_advance_or_save(tmp_path,settings,monkeypatch):
    settings.DATA_DIR=tmp_path
    source=Source.objects.create(key='japan_directory_chongqing',name='重庆',url='https://cq-jpclub.com/company',kind='enterprise_discovery',checkpoint={'directory_offset':0,'keep':1})
    def fake_fetch(url,**kwargs):return {'body':listing_cq() if url==source.url else detail('另一个企业有限公司'),'url':url}
    monkeypatch.setattr('radar.verified_japan_directories.fetch',fake_fetch)
    with pytest.raises(ValueError,match='detail_mismatch'):
        discover(source,limit=1)
    source.refresh_from_db()
    assert source.checkpoint=={'directory_offset':0,'keep':1}
    assert Company.objects.count()==0

@pytest.mark.django_db
def test_yingchuang_recruiter_profiles_require_matching_company_and_real_jd(tmp_path,settings,monkeypatch):
    settings.DATA_DIR=tmp_path
    source=Source.objects.create(key='japan_directory_yingchuang',name='招聘专题',url='https://www.yingchuang.com/special/',kind='enterprise_discovery')
    names=['出光测试有限公司','岗位为空有限公司','错误名称有限公司']
    page='<meta charset="utf-8">'+''.join('<a class="company-title" href="/html/zh/special/'+str(i)+'/index.html">'+n+'</a>' for i,n in enumerate(names))
    def fake_fetch(url,**kwargs):
        if url==source.url:body=page
        elif '/0/' in url:body='<meta charset="utf-8"><div class="nav">出光测试有限公司</div><div class="jobdetail">物流岗位职责 '+('仓储运输工作'*30)+'</div>'
        elif '/1/' in url:body='<meta charset="utf-8"><div class="nav">岗位为空有限公司</div>'
        else:body='<meta charset="utf-8"><div class="nav">其他公司</div><div class="jobdetail">'+('物流岗位'*60)+'</div>'
        return {'body':body.encode(),'url':url}
    monkeypatch.setattr('radar.verified_japan_directories.fetch',fake_fetch)
    monkeypatch.setattr('radar.discovery.robots_allowed',lambda url: (_ for _ in ()).throw(AssertionError('unexpected network path')))
    result=discover(source,limit=3)
    assert result['stats']['details_ok']==1
    assert [x['reason'] for x in result['stats']['skipped_details']]==['no_public_job_description','detail_mismatch']
    assert list(Company.objects.values_list('name',flat=True))==['出光测试有限公司']
    company=Company.objects.get()
    assert classify_japanese_link(company.name,company.discovery_evidence)['level']=='directory'
    assert company.ownership_status=='unknown' and source.checkpoint['directory_offset']==0

@pytest.mark.django_db
def test_recruitment_signal_keeps_employer_role_and_does_not_invent_current_posting(tmp_path,settings,monkeypatch):
    from radar.services import ingest_item
    from radar.queues import japan_signal
    settings.DATA_DIR=tmp_path
    settings.LEAD_MODEL_ENABLED=False
    source=Source.objects.create(key='japan_directory_yingchuang',name='招聘专题',url='https://www.yingchuang.com/special/',kind='enterprise_discovery')
    name='出光测试有限公司'
    listing=('<meta charset="utf-8"><a class="company-title" href="/html/zh/special/one/index.html">'+name+'</a>').encode()
    body=('<meta charset="utf-8"><div class="nav">'+name+'</div>'
          '<div class="jobdetail"><p>物流管理</p><p>工作内容：新设仓库筹建，仓储管理和危险品运输。负责具体现场协调和供应商管理，并跟踪实际发运、库存盘点以及跨部门业务流程。</p></div>'
          '<div class="jobdetail"><p>销售经理</p><p>工作内容：开展业务扩张及客户管理。</p></div>').encode()
    def fake_fetch(url,**kwargs):return {'body':listing if url==source.url else body,'url':url}
    monkeypatch.setattr('radar.verified_japan_directories.fetch',fake_fetch)
    result=discover(source)
    assert len(result['items'])==1
    lead,created,_=ingest_item(source,result['items'][0])
    assert created and lead.company.name==name and lead.buyer_role=='unknown'
    assert lead.event_type=='early_signal' and lead.processing_status=='early_signal'
    assert lead.published_date is None and lead.metadata['posting_date_status']=='unknown'
    assert '发布时间未知' in lead.title and '销售经理' not in lead.text
    assert japan_signal(lead)['level']=='directory'
    from radar.evidence import reextract
    reextract(lead.pk); lead.refresh_from_db()
    assert lead.company.name==name and lead.event_type=='early_signal' and lead.buyer_role=='unknown'
    lead.review_status='contacted';lead.save(update_fields=['review_status'])
    ingest_item(source,result['items'][0]);lead.refresh_from_db()
    assert lead.review_status=='contacted'


@pytest.mark.django_db
def test_recruitment_cannot_attach_unverified_employer(tmp_path,settings):
    from radar.services import ingest_item
    settings.DATA_DIR=tmp_path
    settings.LEAD_MODEL_ENABLED=False
    source=Source.objects.create(key='japan_directory_yingchuang',name='招聘专题',url='https://www.yingchuang.com/special/',kind='enterprise_discovery')
    item={'external_id':'1','url':'https://www.yingchuang.com/html/zh/special/x/index.html','title':'某企业招聘','text':'岗位：仓储物流','kind':'early_signal','metadata':{'signal_type':'job','employer_name':'某企业有限公司','directory_detail_hash':'fake'}}
    with pytest.raises(ValueError,match='unverified_recruitment_employer'):
        ingest_item(source,item)

@pytest.mark.django_db
def test_directory_job_ingestion_failure_does_not_advance_checkpoint(tmp_path,settings,monkeypatch):
    from radar.services import enqueue_source, process_run
    import radar.services as services
    settings.DATA_DIR=tmp_path
    settings.LEAD_MODEL_ENABLED=False
    source=Source.objects.create(key='japan_directory_yingchuang',name='招聘专题',url='https://www.yingchuang.com/special/',kind='enterprise_discovery',checkpoint={'directory_offset':0})
    listing=('<meta charset="utf-8"><a class="company-title" href="/html/zh/special/one/index.html">出光测试有限公司</a><a class="company-title" href="/html/zh/special/two/index.html">尼康测试有限公司</a>').encode()
    body=('<meta charset="utf-8"><div class="nav">出光测试有限公司</div><div class="jobdetail"><p>物流经理</p><p>'+('岗位工作涉及仓储运输管理。'*12)+'</p></div>').encode()
    monkeypatch.setattr('radar.verified_japan_directories.fetch',lambda url,**kw:{'body':listing if url==source.url else body,'url':url})
    def fail_after_checkpoint_check(*args):
        source.refresh_from_db()
        assert source.checkpoint == {'directory_offset':0}
        raise ValueError('synthetic ingest failure')
    monkeypatch.setattr(services,'ingest_item',fail_after_checkpoint_check)
    # Only first detail is valid; force one-item batch to demonstrate a nonzero checkpoint.
    import radar.verified_japan_directories as adapter
    original=adapter.discover_verified
    monkeypatch.setattr(adapter,'discover_verified',lambda src,limit=3:original(src,limit=1))
    run=process_run(enqueue_source(source.pk).pk)
    source.refresh_from_db()
    assert run.status=='failed' and 'synthetic ingest failure' in run.error and source.checkpoint=={'directory_offset':0}
