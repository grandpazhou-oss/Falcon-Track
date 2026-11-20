"""Synthetic boundaries for the v0.3 acquisition repair."""
import json
import pytest
from collectors.japanese_public import job_detail, expansion_hits


def job_page(extra=''):
    return ('''<html><head><meta charset="utf-8"></head><body><main><header><div>普通制造企业</div>
    <h1>物流主管</h1><p>工作地点：长沙</p><p>发布日期：2026-11-01</p></header>
    <section><h2>职位描述</h2><p>负责仓储和物流，要求日语。</p></section></main>
    <footer>上海办公室<section><h2>推荐职位</h2><p>全日资企业招聘仓库主管</p></section></footer>'''+extra+'</body></html>').encode()


def test_job_fields_use_current_job_not_footer_or_recommendation():
    item=job_detail(job_page(),'https://example.org/jobs/x-12','12',['日语 物流'])
    assert item['metadata']['job_locations']==['长沙']
    assert not item['metadata']['japanese_employer_claim']
    assert item['published_date']=='2026-11-01'
    assert '上海办公室' not in item['text'] and '推荐职位' not in item['text']


@pytest.mark.django_db
def test_local_job_reparse_keeps_manual_company_and_creates_research_candidates(tmp_path,settings,client):
    from django.urls import reverse
    from radar.models import Company,Document,Lead,Source
    settings.DATA_DIR=tmp_path
    source=Source.objects.create(key='persol_jobs',name='招聘',kind='recruitment')
    company=Company.objects.create(name='人工确认制造有限公司')
    lead=Lead.objects.create(source=source,company=company,external_id='12',url='https://example.org/jobs/x-12',
        title='旧职位',text='旧正文',content_hash='a'*64,review_status='contacted',metadata={'manual_buyer':True,'signal_type':'job'})
    body=job_page().replace('负责仓储和物流，要求日语。'.encode(),'负责住友電気工業（株）物流支持，要求日语。'.encode())
    path=tmp_path/'raw'/'job.html';path.parent.mkdir(parents=True);path.write_bytes(body)
    Document.objects.create(lead=lead,content_hash=lead.content_hash,text=lead.text,raw_path='raw/job.html')
    response=client.post(reverse('radar:reparse_job',args=[lead.pk]));lead.refresh_from_db()
    assert response.status_code==302 and lead.company==company and lead.review_status=='contacted'
    candidate=lead.entity_candidates.get(name='住友電気工業（株）')
    assert candidate.status=='research_only' and candidate.role=='mentioned_research'
    assert candidate.evidence['quote'] in lead.text and candidate.evidence['block_id'].startswith('d')


@pytest.mark.django_db
def test_unknown_japanese_owned_manufacturer_expansion_is_not_whitelist_blocked(tmp_path,settings):
    settings.DATA_DIR=tmp_path
    hits=expansion_hits('合成精密制造有限公司新建项目','建设单位为日本独资企业，计划新增仓库和生产线。')
    assert any(x.get('basis')=='explicit_japan_ownership' for x in hits)


@pytest.mark.django_db
def test_equipment_brand_and_language_do_not_identify_owner(tmp_path,settings):
    from radar.models import Lead,Source
    from radar.entities import japanese_link_for_lead
    settings.DATA_DIR=tmp_path
    title='某制造企业扩建项目'
    text='建设单位：华南合成制造有限公司。采用丰田汽车设备，项目新建生产线。岗位要求日语。'
    source=Source.objects.create(key='synthetic-eia',name='测试',kind='early_signal')
    lead=Lead.objects.create(source=source,external_id='1',title=title,text=text)
    assert not expansion_hits(title,text)
    assert japanese_link_for_lead(lead)['level']=='none'


def test_robots_follows_safe_redirect_and_uses_actual_crawler(monkeypatch):
    import radar.public_fetch as f
    f.clear_robots_cache()
    monkeypatch.setattr(f,'validate_url',lambda url:(__import__('urllib.parse',fromlist=['urlsplit']).urlsplit(url),'8.8.8.8'))
    def request(url,*args,**kwargs):
        if url.endswith('/robots.txt'):return {'status':301,'location':'/rules.txt'}
        if url.endswith('/rules.txt'):return {'status':200,'body':b'User-agent: GPTBot\nDisallow: /\n\nUser-agent: LogisticsLeadRadar\nAllow: /\n','content_type':'text/plain'}
        raise AssertionError(url)
    monkeypatch.setattr(f,'_request_once',request)
    assert f.robots_allowed('https://example.org/public')


@pytest.mark.parametrize('status',[429,500,503])
def test_robots_server_failure_is_reported_and_never_allows_page(monkeypatch,status):
    from urllib.parse import urlsplit
    import radar.public_fetch as f
    f.clear_robots_cache()
    monkeypatch.setattr(f,'validate_url',lambda u:(urlsplit(u),'8.8.8.8'))
    monkeypatch.setattr(f,'_request_once',lambda *a,**k:{'status':status,'location':'','body':b'','content_type':'text/plain'})
    with pytest.raises(f.FetchError,match=f'robots_or_robots_unavailable:http_{status}'):
        f.fetch('https://example.org/document')


def test_robots_redirect_to_private_address_and_loop_are_blocked(monkeypatch):
    from urllib.parse import urlsplit
    import radar.public_fetch as f
    f.clear_robots_cache()
    def validate(url):
        if urlsplit(url).hostname=='127.0.0.1':raise f.FetchError('non_public_address')
        return urlsplit(url),'8.8.8.8'
    monkeypatch.setattr(f,'validate_url',validate)
    monkeypatch.setattr(f,'_request_once',lambda *a,**k:{'status':301,'location':'http://127.0.0.1/private'})
    with pytest.raises(f.FetchError,match='non_public_address'):f.fetch('https://example.org/document')
    f.clear_robots_cache()
    monkeypatch.setattr(f,'_request_once',lambda *a,**k:{'status':301,'location':'/robots.txt'})
    with pytest.raises(f.FetchError,match='redirect_loop'):f.fetch('https://example.org/document')


def test_oversized_decompressed_response_is_rejected(monkeypatch):
    import gzip,httpx
    from contextlib import nullcontext
    from urllib.parse import urlsplit
    import radar.public_fetch as f
    f.clear_robots_cache();original=httpx.Client
    monkeypatch.setattr(f,'validate_url',lambda u:(urlsplit(u),'8.8.8.8'))
    monkeypatch.setattr(f,'public_request',lambda **k:nullcontext())
    def handle(req):
        if req.url.path=='/robots.txt':return httpx.Response(404)
        return httpx.Response(200,headers={'Content-Encoding':'gzip'},content=gzip.compress(b'a'*10000))
    monkeypatch.setattr(f.httpx,'Client',lambda **kw:original(**kw,transport=httpx.MockTransport(handle)))
    with pytest.raises(f.FetchError,match='size_limit'):f.fetch('https://example.org/document',max_bytes=1000)


def test_failed_candidate_has_retry_backoff():
    from collectors.candidate_state import CandidateLedger
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime
    ledger=CandidateLedger({},'v1');item=ledger.add('1','https://example.org/1','q',{})
    ledger.failed(item,ValueError('http_429'))
    assert item['status']=='retry' and parse_datetime(item['retry_at'])>timezone.now()
    assert ledger.pending()==[]


def test_generic_title_does_not_merge_year_lot_or_announcement_phase():
    from types import SimpleNamespace
    from radar.dedup import identity
    row=lambda title,text:identity(SimpleNamespace(title=title),text)
    original=row('物流服务采购公告','项目编号：ABC-123456；服务期2026年；第一标段。')
    assert original!=row('物流服务采购公告','项目编号：ABC-123456；服务期2027年；第一标段。')
    assert original!=row('物流服务采购公告','项目编号：ABC-123456；服务期2026年；第二标段。')
    assert original!=row('物流服务采购变更公告','项目编号：ABC-123456；服务期2026年；第一标段。')


def _dfmc_stub(monkeypatch, *, logistic=True, fail_once=False):
    from collectors import japanese_public as c
    calls=[];state={'failed':False}
    rows=[{'infoid':str(n),'title':('物流运输' if logistic else '咨询服务')+str(n)+'项目',
           'infourl':f'/jyxx/{n}.html','infodate':'2026-11-01'} for n in (1,2,3)]
    def read(url,form=None):
        calls.append(url)
        evidence={'url':url,'sha256':'a'*64,'raw_path':'raw/source_lists/a.html'}
        if url.endswith('webBuilderCommon.js'):
            return {'body':b'var siteInfo = {"projectName":"/EpointWebBuilder","siteGuid":"site"}'},evidence
        if form is not None:
            data=rows if form['categoryNum']=='004002' and form['pageindex']=='0' else []
            return {'body':json.dumps({'custom':{'infodata':data}}).encode()},evidence
        if fail_once and not state['failed']:
            state['failed']=True;raise TimeoutError('synthetic timeout')
        content=('物流运输服务项目。' if logistic else '咨询与研究服务项目。')*20
        body=('<html><head><meta charset="utf-8"></head><div class="article"><h3>合成服务项目</h3><div class="article-info">'+content+'</div></div></html>').encode()
        return {'body':body},evidence
    monkeypatch.setattr(c,'read',read)
    return c,calls


def test_dfmc_candidate_backlog_survives_acceptance_budget(monkeypatch):
    from types import SimpleNamespace
    c,calls=_dfmc_stub(monkeypatch)
    source=SimpleNamespace(options={'head_pages':1,'max_accepted_items':1,'max_detail_requests':5},checkpoint={},url='https://etp.dfmc.com.cn')
    collected=[]
    for _ in range(4):
        result=c.collect_dfmc(source);source.checkpoint.update(result['collector_checkpoint'])
        collected.append([x['external_id'] for x in result['items']])
    assert collected==[['1'],['2'],['3'],[]]
    assert len([x for x in calls if x.endswith('.html')])==3


def test_rejected_details_still_consume_request_budget(monkeypatch):
    from types import SimpleNamespace
    c,calls=_dfmc_stub(monkeypatch,logistic=False)
    source=SimpleNamespace(options={'head_pages':1,'max_accepted_items':5,'max_detail_requests':2,'generic_detail_budget':5},checkpoint={},url='https://etp.dfmc.com.cn')
    result=c.collect_dfmc(source);entries=result['collector_checkpoint']['dfmc_candidates_v1']['entries']
    assert not result['items'] and result['stats']['details_attempted']==2
    assert sum(v['status']=='pending' for v in entries.values())==1


def test_generic_service_title_can_discover_logistics_in_bounded_body(monkeypatch):
    from types import SimpleNamespace
    from collectors import japanese_public as collector
    c,_=_dfmc_stub(monkeypatch,logistic=False)
    original=c.read
    def read(url,form=None):
        response,evidence=original(url,form=form)
        if url.endswith('.html'):
            response['body']=response['body'].replace('咨询与研究服务'.encode(),'仓储物流运输服务'.encode())
        return response,evidence
    monkeypatch.setattr(collector,'read',read)
    source=SimpleNamespace(options={'head_pages':1,'max_accepted_items':3,'generic_detail_budget':1},checkpoint={},url='https://etp.dfmc.com.cn')
    result=c.collect_dfmc(source)
    assert len(result['items'])==1 and '物流运输' in result['items'][0]['text']
    assert result['stats']['details_attempted']==1 and result['stats']['pending']==2


def test_failed_candidate_is_not_marked_finished(monkeypatch):
    from types import SimpleNamespace
    c,calls=_dfmc_stub(monkeypatch,fail_once=True)
    source=SimpleNamespace(options={'head_pages':1,'max_accepted_items':1,'max_detail_requests':1},checkpoint={},url='https://etp.dfmc.com.cn')
    result=c.collect_dfmc(source);state=result['collector_checkpoint']['dfmc_candidates_v1'];source.checkpoint.update(result['collector_checkpoint'])
    assert state['entries']['1']['status']=='retry'
    state['entries']['1']['retry_at']='2000-01-01T00:00:00+00:00'
    again=c.collect_dfmc(source)
    assert again['items'][0]['external_id']=='1'


def test_real_virtualenv_and_symlink_prefix_detection(tmp_path):
    import subprocess,sys,venv
    from pathlib import Path
    from scripts.local import in_project_venv
    actual=tmp_path/'含中文 空格'/'realenv';venv.create(actual,with_pip=False)
    python=actual/('Scripts/python.exe' if sys.platform=='win32' else 'bin/python')
    def prefixes(executable):
        out=subprocess.check_output([str(executable),'-c','import json,sys;print(json.dumps([sys.prefix,sys.base_prefix]))'],text=True)
        return json.loads(out)
    prefix,base=prefixes(python)
    assert in_project_venv(actual,prefix,base)
    link=tmp_path/'linked-venv'
    try:link.symlink_to(actual,target_is_directory=True)
    except OSError:pytest.skip('当前系统不支持测试符号链接')
    prefix,base=prefixes(link/('Scripts/python.exe' if sys.platform=='win32' else 'bin/python'))
    assert in_project_venv(link,prefix,base)
    prefix,base=prefixes(sys._base_executable)
    assert not in_project_venv(link,prefix,base)


def test_local_profile_uses_distinct_loopback_prefect_port(tmp_path,monkeypatch):
    from scripts.local import project_env
    monkeypatch.setenv('LEAD_DATA_DIR',str(tmp_path/'中文 路径'))
    monkeypatch.setenv('LEAD_PREFECT_PORT','14200')
    env,data=project_env()
    assert env['PREFECT_API_URL']=='http://127.0.0.1:14200/api'
    assert data==tmp_path/'中文 路径'


def test_analysis_worker_is_not_blocked_by_slow_collection(monkeypatch):
    import threading
    import flows
    called=threading.Event();stop=threading.Event();release=threading.Event();source_started=threading.Event()
    def slow_source():source_started.set();release.wait(3)
    source=threading.Thread(target=slow_source);source.start();assert source_started.wait(1)
    def fake_analysis(limit):called.set();stop.set();return []
    monkeypatch.setattr('radar.analysis.process_pending_analyses',fake_analysis)
    worker=threading.Thread(target=flows.analysis_loop,args=(stop,0.01));worker.start()
    assert called.wait(2) and source.is_alive()
    release.set();source.join(timeout=2)
    worker.join(timeout=2);assert not worker.is_alive()
