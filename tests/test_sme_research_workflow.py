"""Synthetic evidence tests; these records never enter the user's business database."""
import hashlib
import pytest
from django.utils import timezone
from radar.models import Source,Lead,Company,Document,Review,ModelCall


@pytest.fixture
def record(tmp_path,settings,db):
    settings.DATA_DIR=tmp_path
    text='株式会社青空精密は中国・上海に青空精密（上海）有限公司を設立。2024年3月に新工場の稼働を計画。中小企業。'
    body=text.encode();sha=hashlib.sha256(body).hexdigest()
    path=tmp_path/'raw'/'research'/f'{sha}.html';path.parent.mkdir(parents=True);path.write_bytes(body)
    return {'external_id':'doc-123','title':'青空精密の上海事業計画','url':'https://source.example/events/123',
        'text':text,'document':{'raw_path':path.relative_to(tmp_path).as_posix(),'sha256':sha,'type':'html'},
        'research':{'event_type':'entity_planned','workstream':'planned_china','china_scope':'mainland',
          'china_evidence':{'quote':'中国・上海','location':'paragraph:1'},
          'participants':[{'name':'株式会社青空精密','role':'japan_parent','relationship':'unknown','quote':'株式会社青空精密','location':'paragraph:1'},
                          {'name':'青空精密（上海）有限公司','role':'china_entity','relationship':'unknown','quote':'青空精密（上海）有限公司','location':'paragraph:1'}],
          'dates':[{'kind':'planned','value':'2024-03','precision':'month','status':'planned','quote':'2024年3月','location':'paragraph:1'}],
          'facts':[{'field':'事業','value':'新工場','quote':'新工場の稼働を計画','location':'paragraph:1'}],
          'size':{'value':'sme','basis':'source_statement','quote':'中小企業','location':'paragraph:1'},
          'questions':['是否已投产？'],'inferences':['可核实物流安排。'],'next_step':'核实项目进度。'}}


@pytest.mark.django_db
def test_research_event_is_idempotent_preserves_human_review_and_keeps_dates(record):
    from radar.research import ingest_research_record
    source=Source.objects.create(key='synthetic-research',name='合成来源',kind='early_signal')
    lead,created,updated=ingest_research_record(source,record)
    assert created and not updated and lead.company.name=='株式会社青空精密'
    assert lead.event_type=='early_signal' and lead.processing_status=='early_signal' and lead.deadline is None
    assert lead.research_event.event_type=='entity_planned' and lead.research_event.progress=='overdue_unconfirmed'
    assert lead.research_event.dates[0]['precision']=='month' and lead.research_event.dates[0]['value']=='2024-03'
    assert len(lead.research_event.participants)==2 and lead.research_event.size['value']=='sme'
    lead.review_status='contacted';lead.metadata={'manual_buyer':True,'human_note':'保留'};lead.save()
    Review.objects.create(lead=lead,status='contacted',note='真实人工动作的合成测试')
    again,created,updated=ingest_research_record(source,record);again.refresh_from_db()
    assert not created and not updated and again.pk==lead.pk and again.review_status=='contacted'
    assert again.metadata['human_note']=='保留' and again.reviews.count()==1 and ModelCall.objects.count()==0


@pytest.mark.django_db
def test_research_evidence_must_be_local_and_verbatim_and_cannot_guess_identity(record):
    from radar.research import ingest_research_record
    source=Source.objects.create(key='synthetic-guard',name='合成来源',kind='early_signal')
    record['research']['participants'][0]['name']='猜测日本企业'
    with pytest.raises(ValueError):ingest_research_record(source,record)
    assert Lead.objects.count()==0 and Company.objects.count()==0


@pytest.mark.django_db
def test_research_rejects_hong_kong_and_japanese_china_region_as_mainland(record):
    from radar.research import ingest_research_record
    source=Source.objects.create(key='synthetic-region',name='合成来源',kind='early_signal')
    record['research']['china_evidence']['quote']='岡山県中国地方'
    with pytest.raises(ValueError):ingest_research_record(source,record)
    assert Lead.objects.count()==0


@pytest.mark.django_db
def test_changed_event_preserves_previous_fact_snapshot(record):
    from radar.research import ingest_research_record
    source=Source.objects.create(key='synthetic-versions',name='合成来源',kind='early_signal')
    lead,_,_=ingest_research_record(source,record)
    old_hash=lead.content_hash
    record['research']['event_type']='construction_started'
    revised,created,updated=ingest_research_record(source,record)
    assert updated and not created and revised.pk==lead.pk
    assert revised.research_event.event_type=='construction_started'
    assert revised.documents.get(content_hash=old_hash).metadata['research_snapshot']['event_type']=='entity_planned'

@pytest.mark.django_db
def test_research_rejects_symlink_raw_evidence(record,tmp_path):
    from radar.research import ingest_research_record
    source=Source.objects.create(key='synthetic-symlink',name='合成来源')
    path=tmp_path/record['document']['raw_path'];body=path.read_bytes()
    outside=tmp_path/'outside.html';outside.write_bytes(body);path.unlink();path.symlink_to(outside)
    with pytest.raises(ValueError,match='路径不安全'):ingest_research_record(source,record)
    assert Lead.objects.count()==0


@pytest.mark.django_db
def test_research_format_change_keeps_business_version_and_new_original(record,tmp_path):
    from radar.research import ingest_research_record
    source=Source.objects.create(key='synthetic-format',name='合成来源')
    lead,_,_=ingest_research_record(source,record)
    body=(record['text']+'\n格式变化，不改变业务字段。').encode();digest=hashlib.sha256(body).hexdigest()
    path=tmp_path/'raw'/'research'/f'{digest}.html';path.write_bytes(body)
    record['text']=body.decode();record['document']['sha256']=digest;record['document']['raw_path']=path.relative_to(tmp_path).as_posix()
    again,created,updated=ingest_research_record(source,record)
    assert not created and not updated and again.version==lead.version
    assert again.research_event.source_document_hash==digest
    assert again.documents.filter(content_hash=digest).exists()


@pytest.mark.django_db
def test_source_research_run_keeps_checkpoint_and_does_not_repeat(record,monkeypatch):
    from radar.services import process_run
    from radar.models import SourceRun
    source=Source.objects.create(key='osaka_sme_china_sites',name='合成来源',kind='early_signal',checkpoint={'retained':'yes'})
    result={'items':[record],'status':'success','next_cursor':1,'stats':{},'collector_checkpoint':{'batch':{'completed':True}}}
    monkeypatch.setattr('radar.services._execute_collector',lambda s:(result,1,[]))
    first=process_run(SourceRun.objects.create(source=source).pk)
    second=process_run(SourceRun.objects.create(source=source).pk)
    source.refresh_from_db()
    assert (first.new_count,second.new_count)==(1,0) and second.status=='success_no_new'
    assert source.checkpoint['retained']=='yes' and source.checkpoint['batch']['completed']
    assert Lead.objects.count()==1 and ModelCall.objects.count()==0


@pytest.mark.django_db
def test_research_pages_and_jbic_manual_import_require_explicit_rights(settings,tmp_path,client):
    from django.core.files.uploadedfile import SimpleUploadedFile
    from radar.services import seed_sources
    settings.DATA_DIR=tmp_path;seed_sources()
    body=('''<html><meta charset="utf-8"><main><div id="h1_title"><h1>中国における製造事業に対する融資</h1></div>
    <div class="main"><section><article><address class="header-address">2026年2月10日</address><ol>
    <li>株式会社国際協力銀行（JBIC）は、株式会社架空衣料（本社：東京都、以下「架空衣料」）の中華人民共和国（以下「中国」）法人SUZHOU SAMPLE GARMENT CO., LTD.（以下「SSG」）との間で貸付契約を締結しました。</li>
    <li>本件は、SSGが中国の蘇州市において実施する衣料品の製造・販売事業に必要な資金を融資するものです。</li>
    <li>架空衣料は衣料品製造を行う中小企業です。同社は1996年にSSGを設立しました。</li></ol>
    <div class="footnotes">本契約は2025年6月25日に締結済みです。</div></article></section></div></main></html>''').encode()
    url='https://www.jbic.go.jp/ja/information/press/press-2025/press_00149.html'
    def upload():return SimpleUploadedFile('article.html',body,content_type='text/html')
    response=client.post('/research/import-jbic/',{'url':url,'material':upload()})
    assert response.status_code==200 and Lead.objects.count()==0
    response=client.post('/research/import-jbic/',{'url':url,'material':upload(),'rights':'yes'})
    assert response.status_code==302 and Lead.objects.count()==1
    page=client.get(response.url);assert page.status_code==200 and '融资支持'.encode() in page.content
    assert client.get('/research/').status_code==200 and client.get('/sources/').status_code==200
    response=client.post('/research/import-jbic/',{'url':url,'material':upload(),'rights':'yes'})
    assert response.status_code==302 and Lead.objects.count()==1 and ModelCall.objects.count()==0


def test_expired_month_or_before_plan_needs_progress_check():
    from radar.research import research_progress
    assert research_progress([{'kind':'planned_date','value':'2024-03','precision':'month_or_before','status':'planned'}])=='overdue_unconfirmed'

@pytest.mark.django_db
def test_listed_site_check_requires_exact_named_company_and_preserves_failure(record,tmp_path,monkeypatch):
    from radar.research import ingest_research_record
    from radar.website_check import check_listed_website
    source=Source.objects.create(key='synthetic-website',name='合成来源')
    record['text']+='\nhttps://company.example/'
    body=record['text'].encode();digest=hashlib.sha256(body).hexdigest()
    path=tmp_path/'raw'/'research'/f'{digest}.html';path.write_bytes(body)
    record['document'].update(raw_path=path.relative_to(tmp_path).as_posix(),sha256=digest)
    record['research']['facts'].append({'field':'website','value':'https://company.example/',
         'quote':'https://company.example/','location':'worksheet:S!B7'})
    lead,_,_=ingest_research_record(source,record)
    site='<html><body>会社概要 株式会社青空精密</body></html>'.encode();h=hashlib.sha256(site).hexdigest()
    target=tmp_path/'raw'/'research'/f'{h}.html';target.write_bytes(site)
    monkeypatch.setattr('radar.website_check.read_original',lambda *a,**k:({'body':site},{'url':'https://company.example/','sha256':h,'raw_path':target.relative_to(tmp_path).as_posix()}))
    doc=check_listed_website(lead.research_event)
    assert doc.metadata['status']=='explicit_name_match' and doc.metadata['name']=='株式会社青空精密'
    def blocked(*a,**k):raise ValueError('robots_disallowed')
    monkeypatch.setattr('radar.website_check.read_original',blocked)
    failure=check_listed_website(lead.research_event)
    assert failure.metadata['status']=='blocked_or_failed' and 'robots_disallowed' in failure.metadata['error']
    assert not failure.raw_path and ModelCall.objects.count()==0

@pytest.mark.django_db
def test_unverified_and_policy_blocked_sources_cannot_be_scheduled(client):
    from radar.services import seed_sources
    seed_sources()
    for key in ('osaka_china_business_events','jbic_sme_china'):
        source=Source.objects.get(key=key)
        client.post(f'/sources/{source.pk}/settings/',{'enabled':'on','interval_minutes':60})
        source.refresh_from_db()
        assert not source.enabled

@pytest.mark.django_db
def test_same_first_china_factory_event_links_cross_source_duplicate(record,tmp_path):
    from copy import deepcopy
    from radar.research import ingest_research_record
    first=Source.objects.create(key='synthetic-project-a',name='合成来源 A')
    second=Source.objects.create(key='synthetic-project-b',name='合成来源 B')
    def prepare(item,word):
        item['text']+='\n2026年6月25日，株式会社青空精密的'+word+'在上海开工。'
        body=item['text'].encode();digest=hashlib.sha256(body).hexdigest()
        path=tmp_path/'raw'/'research'/f'{digest}.html';path.write_bytes(body)
        item['document'].update(raw_path=path.relative_to(tmp_path).as_posix(),sha256=digest)
        item['research']['event_type']='construction_started'
        item['research']['dates']=[{'kind':'event_date','value':'2026-06-25','precision':'day','status':'stated','quote':'2026年6月25日','location':'paragraph:2'}]
        return item
    one=prepare(deepcopy(record),'中国首个生产基地')
    two=prepare(deepcopy(record),'国内首个生产基地');two['external_id']='second-report'
    original,_,_=ingest_research_record(first,one)
    duplicate,_,_=ingest_research_record(second,two)
    assert duplicate.duplicate_of_id==original.pk
    assert duplicate.documents.exists() and original.documents.exists()


def test_first_factory_dedup_requires_same_named_company_city_and_day():
    from radar.research import _first_factory_fingerprint
    people=[{'name':'株式会社青空精密','role':'japan_company'}]
    dates=[{'kind':'event_date','value':'2026-06-25','precision':'day','status':'stated'}]
    original=_first_factory_fingerprint('株式会社青空精密的中国首个生产基地在上海开工。','construction_started',people,dates)
    assert original
    assert _first_factory_fingerprint('株式会社青空精密的中国首个生产基地在天津开工。','construction_started',people,dates)!=original
    assert _first_factory_fingerprint('株式会社青空精密的扩建工厂在上海开工。','construction_started',people,dates) is None
    assert _first_factory_fingerprint('株式会社青空精密的中国首个生产基地在上海开工。','construction_started',people,[{**dates[0],'value':'2026-06-26'}])!=original


@pytest.mark.django_db
def test_historical_recheck_keeps_evidence_in_research_without_sales_scoring(record, client):
    from radar.research import ingest_research_record
    from radar.sales_pools import research_card

    source = Source.objects.create(key='history-check', name='存量复核', enabled=False)
    record['research']['event_type'] = 'historical_only'
    lead, created, _ = ingest_research_record(source, record)
    event = lead.research_event

    assert created and research_card(event) is None
    assert event.china_evidence['quote'] in lead.documents.get(pk=event.china_evidence['document_id']).text
    assert event.progress == 'overdue_unconfirmed'
    response = client.get('/research/?stream=historical_review')
    assert response.status_code == 200
    assert record['title'].encode() in response.content


@pytest.mark.django_db
def test_noisy_archive_workflow_routes_evidence_without_changing_sales(tmp_path, settings, client):
    from datetime import date
    from radar.archived_recheck import scan_archived_research
    from radar.models import KeywordCandidate, ResearchEvent, SearchCall
    from radar.sales_pools import build_sales_pools, intake_grades

    settings.DATA_DIR = tmp_path
    raw = tmp_path / 'raw' / 'research'
    raw.mkdir(parents=True)

    def candidate(i, body, *, title='業種分布ランキング', original=True):
        digest = hashlib.sha256(body.encode()).hexdigest()
        (raw / f'{digest}.html').write_text(body, encoding='utf-8')
        return KeywordCandidate.objects.create(
            title=title, canonical_url=f'https://source.example/reports/{i}', provider='source',
            text=body, raw_path=f'raw/research/{digest}.html' if original else '',
            result={'source_sha256':digest},
        )

    old = candidate(1, '株式会社青空は2003年5月に上海で工場を新設しました。')
    plan = candidate(2, '株式会社青空は2024年3月に上海で工場を新設する予定。')
    recent = candidate(3, '株式会社青空は上海で倉庫を開設しました。')
    noise = candidate(4, '株式会社青空の上海工場を設立する手順の紹介。')
    absent = candidate(5, '株式会社青空は上海で倉庫を開設しました。', original=False)
    clean = candidate(6, '株式会社青空は上海で倉庫を開設しました。', title='個別企業の案件')
    example = candidate(7, '例えば株式会社青空は上海で倉庫を開設しました。')
    from unittest.mock import patch
    before = intake_grades(build_sales_pools())
    with patch('radar.archived_recheck.timezone.localdate', return_value=date(2026,11,19)):
        preview = {r['candidate_id']:r['status'] for r in scan_archived_research()}
        assert preview == {old.pk:'historical_fact',plan.pk:'overdue_plan',recent.pk:'pending',
                           noise.pk:'no_named_operation',absent.pk:'no_original',example.pk:'no_named_operation'}
        assert clean.pk not in preview and ResearchEvent.objects.count()==0
        response = client.post('/research/recheck-archived/')
        assert response.status_code == 302
        assert not any(r['created'] for r in scan_archived_research(apply=True))
    assert ResearchEvent.objects.count()==3
    assert intake_grades(build_sales_pools())==before
    assert SearchCall.objects.count()==ModelCall.objects.count()==0
    page = client.get('/research/?stream=historical_review')
    for label in ('历史经营事实','过期计划','经营证据待核'):
        assert label.encode() in page.content


@pytest.mark.django_db(transaction=True)
def test_recovered_original_is_research_evidence_without_replacing_candidate(tmp_path, settings):
    from datetime import date
    from radar.archived_recheck import inspect_archived_candidate, scan_archived_research
    from radar.management.commands.backup_data import snapshot_payload
    from radar.models import KeywordCandidate, ResearchEvent
    from radar.sales_pools import build_sales_pools, intake_grades

    settings.DATA_DIR = tmp_path
    text = '株式会社青空は2003年5月に上海で工場を新設しました。'
    digest = hashlib.sha256(text.encode()).hexdigest()
    path = tmp_path / 'raw' / 'research' / f'{digest}.html'
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding='utf-8')
    original = 'https://publisher.example/rss/123'
    recovered = 'https://publisher.example/articles/123'
    candidate = KeywordCandidate.objects.create(canonical_url=original, title='業種分布ランキング',
        provider='source', result={'content_mode':'snippet_only'})
    before = intake_grades(build_sales_pools())
    candidate.result['research_recovery'] = {'url':recovered,'text':text,'sha256':digest,
                                             'raw_path':path.relative_to(tmp_path).as_posix()}
    candidate.save(update_fields=['result'])
    assert inspect_archived_candidate(candidate, date(2026, 11, 19))['status']=='historical_fact'
    assert path.relative_to(tmp_path).as_posix() in snapshot_payload()[1]
    assert scan_archived_research(apply=True)[0]['created']
    assert not scan_archived_research(apply=True)[0]['created']
    event = ResearchEvent.objects.get()
    assert event.lead.url == event.china_evidence['url'] == recovered
    candidate.refresh_from_db()
    assert (candidate.canonical_url, candidate.text, candidate.raw_path) == (original, '', '')
    assert intake_grades(build_sales_pools()) == before

    path.write_text('changed', encoding='utf-8')
    assert inspect_archived_candidate(candidate, date(2026, 11, 19))['status']=='original_unverified'


@pytest.mark.django_db
def test_archived_company_context_and_design_are_not_an_overdue_plan(tmp_path, settings):
    from datetime import date
    from types import SimpleNamespace
    from radar.archived_recheck import inspect_archived_candidate

    settings.DATA_DIR = tmp_path
    body = ('持株株式会社は、グループ会社である上海青空電子有限公司が'
            '2005年7月に上海で設計の計画・設計業務を行う工場を開設したと発表しました。')
    p = tmp_path / 'raw' / 'research' / 'sample.html'
    p.parent.mkdir(parents=True)
    p.write_text(body, encoding='utf-8')
    item = SimpleNamespace(title='設立20周年記念式典', text=body, raw_path='raw/research/sample.html',
        canonical_url='https://publisher.example/article',result={'source_sha256':hashlib.sha256(body.encode()).hexdigest()})
    result = inspect_archived_candidate(item, date(2026,11,19))
    assert result['status']=='historical_fact'
    assert result['company']=='上海青空電子有限公司'
