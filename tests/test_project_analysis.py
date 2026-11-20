import json
import uuid

import httpx

from radar.analysis import enqueue, process_pending_analyses
from radar.models import AnalysisRun, Document, ResearchEvent
from tests.test_v02 import provider, setup_data


def test_project_analysis_checks_timeline_and_reuses_manual_post(setup_data, monkeypatch, client):
    lead, cfg = setup_data
    doc = lead.documents.get()
    doc.metadata |= {'kind': 'research_material', 'source_document_hash': 'b'*64}
    doc.save()
    event = ResearchEvent.objects.create(lead=lead, event_type='relocation', workstream='existing_in_china')
    section = '2026年5月，上海工厂已投产。\n预计2027年3月末广州仓库开始运营。'
    original = Document.objects.create(lead=lead, content_hash='b'*64, text=section,
        metadata={'kind':'research_source','parent_hash':lead.content_hash,'pages':[{'page':2,'text':section}]})
    unrelated = Document.objects.create(lead=lead, content_hash='c'*64, text='2026年10月8日 日本開催',
        metadata={'kind':'research_source','parent_hash':lead.content_hash})
    ref = f'd{original.pk}:p2:s1'
    other = f'd{unrelated.pk}:s1'
    done = {'field':'投产','block_id':ref,'quote':'2026年5月，上海工厂已投产。',
        'event_status':'occurred','date_kind':'event','date_text':'2026年5月','date_block_id':ref,'date_quote':'2026年5月'}
    plan = {**done,'field':'仓库运营','quote':'预计2027年3月末广州仓库开始运营。',
        'event_status':'planned','date_text':'2027年3月末','date_quote':'2027年3月末'}
    step = {'check':'核对新仓启用','contact_role':'仓库运营部','question':'补货由谁安排？','evidence_ids':[ref]}

    def handler(request):
        payload=json.loads(json.loads(request.content)['messages'][1]['content'])
        assert set(payload['evidence']['documents'])=={str(original.pk),str(unrelated.pk)}
        assert payload['evidence']['documents'][str(original.pk)]['sections'][0]['text']==section
        result={'facts':[done, plan, {**plan,'event_status':'occurred'},
            {**done,'event_status':'unverified','date_kind':'disclosure','date_block_id':other,
             'date_text':'2026年10月8日','date_quote':'2026年10月8日'}],
            'verification':[step,{**step,'evidence_ids':[other]},{**step,'question':'整机由谁运输？'}]}
        return httpx.Response(200,json={'usage':{'total_tokens':123},'choices':[{'message':{'content':json.dumps(result)}}]})

    provider(monkeypatch,handler)
    key=str(uuid.uuid4())
    url=f'/leads/{lead.pk}/analyze/'
    for _ in range(2):
        assert client.post(url, {'request_key':key, 'task_type':'analysis'}).status_code==302
    run,=process_pending_analyses()
    assert AnalysisRun.objects.count()==1 and run.calls.count()==1
    assert run.status=='needs_review' and run.prompt_version=='sales-v02.11-project'
    assert [f['event_status'] for f in run.result['analysis']['facts']]==['occurred','planned']
    assert run.result['analysis']['verification']==[step]
    assert len(run.result['validation_errors'])==4
    assert run.result['analysis']['facts'][0]['page']==2
    assert original.content_hash in run.input_snapshot['evidence']['document_hashes']
    response=client.get(f'/leads/{lead.pk}/')
    for label in ('已发生（原文披露）','动作日期 2026年5月','查什么','找谁','问什么'):
        assert label in response.content.decode()
    event.refresh_from_db()
    assert event.event_type=='relocation' and event.facts==[]


def test_project_with_no_saved_text_keeps_failure(setup_data, monkeypatch):
    lead, cfg = setup_data
    ResearchEvent.objects.create(lead=lead, event_type='historical_only')
    lead.documents.all().update(text='')
    monkeypatch.setattr('radar.analysis.secret',lambda cfg:'synthetic-secret')
    run,_=enqueue(lead.pk,'analysis',str(uuid.uuid4()))
    run,=process_pending_analyses()
    assert (run.status,run.error_code,run.calls.count())==('failed','missing_project_evidence',0)


def test_keyword_source_hash_gate_and_retry(setup_data, client, settings, monkeypatch):
    import hashlib
    from radar.models import KeywordCandidate

    raw=settings.DATA_DIR/'raw'/'example.html'
    raw.parent.mkdir()
    raw.write_bytes('2026年2月中国工場始動'.encode())
    candidate=KeywordCandidate.objects.create(canonical_url='https://example.com/issuer',title='工場始動',
        text=raw.read_text(),raw_path='raw/example.html',provider='test',
        result={'content_mode':'full_text','source_sha256':hashlib.sha256(raw.read_bytes()).hexdigest()})
    action=f'/sales-pools/candidate/{candidate.pk}/analyze/'
    assert client.post(action, {'request_key':str(uuid.uuid4())}).status_code==302
    first=AnalysisRun.objects.get()
    assert first.lead_id is None and first.input_snapshot['candidate_id']==candidate.pk
    assert first.input_snapshot['evidence']['processed_blocks']==1
    provider(monkeypatch, lambda req: httpx.Response(429))
    run,=process_pending_analyses()
    assert run.error_code=='rate_limited' and run.calls.count()==1
    raw.write_text('tampered')
    assert client.post(f'/ai/tasks/{first.pk}/action/', {'action':'retry','request_key':str(uuid.uuid4())}).status_code==302
    run,=process_pending_analyses()
    assert run.error_code=='missing_project_evidence' and run.calls.count()==0
    assert AnalysisRun.objects.get(pk=first.pk).error_code=='rate_limited'
    assert len(KeywordCandidate.objects.get(pk=candidate.pk).result)==2


def test_reported_historical_entry_is_not_confused_with_entry_plan():
    from radar.project_analysis import status_issue

    actual={'event_status':'occurred','date_kind':'event','date_text':'2006年','quote':'2006年に上海へ進出。'}
    assert status_issue(actual,'2026-11-19')==''
    assert status_issue({**actual,'quote':'2006年に上海へ進出する計画。'},'2026-11-19')
    assert status_issue({**actual,'date_text':'2026年11月30日'},'2026-11-19')


def test_pdf_whitespace_is_accepted_without_changing_quotes_or_borrowing_dates():
    from radar.project_analysis import date_issue, normalize_excerpt

    raw='2026年11月\n　19日　上海工場　新設'
    quote='2026年11月19日 上海工場新設'
    assert normalize_excerpt(quote) == normalize_excerpt(raw)
    assert normalize_excerpt('上海、工場') != normalize_excerpt('上海,工場')
    fact={'block_id':'p3','quote':quote,'date_kind':'event','date_text':'2026年11月19日',
          'date_quote':'2026年11月19日','date_block_id':'p3','event_status':'planned'}
    blocks={'p3':{'document_id':None,'hash':'a','page':3,'text':raw},
            'p4':{'document_id':None,'hash':'a','page':4,'text':raw},
            'other':{'document_id':None,'hash':'b','page':3,'text':raw}}
    assert not date_issue(fact,blocks)
    assert date_issue(fact|{'date_block_id':'p4'},blocks)
    assert date_issue(fact|{'date_block_id':'other'},blocks)
    assert date_issue(fact|{'date_text':'2026年11月20日'},blocks)


def test_saved_pdf_keeps_real_page_after_blank_pages(setup_data, client, monkeypatch, settings):
    import hashlib

    from pypdf import PdfWriter
    from radar.models import KeywordCandidate
    from radar.pdf_extract import extract

    raw=settings.DATA_DIR/'raw'/'sample.pdf'
    raw.parent.mkdir()
    writer=PdfWriter()
    for _ in range(3):
        writer.add_blank_page(200,200)
    writer.write(raw)
    extracted=extract(raw)['pages']
    assert [page['page'] for page in extracted]==[1,2,3]
    assert not extracted[0]['text']
    # Synthetic visible text on the third extracted page isolates persistence from PDF fonts.
    text='2026年1月 上海工場稼働。'
    extracted[2]['text']=text
    c=KeywordCandidate.objects.create(canonical_url='https://example.co.jp/sample.pdf', title='工場稼働',
         provider='issuer_ir_pdf', raw_path='raw/sample.pdf', text=text,
         result={'content_mode':'full_text','source_sha256':hashlib.sha256(raw.read_bytes()).hexdigest(),
                 'pdf_pages':extracted})
    run,_=enqueue(None,'analysis',str(uuid.uuid4()),candidate_id=c.pk)
    block,=run.input_snapshot['evidence']['blocks']
    assert block['page']==3 and block['id']==f'k{c.pk}:p3:s1'
    result={'facts':[{'field':'开始运营','block_id':block['id'],'quote':text,
         'date_kind':'event','date_text':'2026年1月','date_block_id':block['id'],
         'date_quote':'2026年1月','event_status':'occurred'}],
         'verification':[{'check':'核实产线','contact_role':'物流部门','question':'补货由谁安排？','evidence_ids':[block['id']]}]}
    provider(monkeypatch,lambda _:httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result)}}]}))
    run,=process_pending_analyses()
    assert run.result['analysis']['facts'][0]['page']==3
    response=client.get(f'/sales-pools/candidate/{c.pk}/').content.decode()
    assert 'sample.pdf#page=3' in response and 'PDF 第 3 页' in response


def test_issuer_question_cannot_import_another_location_quarter():
    from radar.project_analysis import verification_issue

    step={'check':'核对上海项目', 'contact_role':'厂区物流',
          'question':'2027年Q4是否开工？','evidence_ids':['section']}
    focuses=[{'kind':'china_action','quote':'上海臨港 工場を拡張（2027年春より稼働開始予定）'}]
    blocks={'section':'ベトナム 2027年Q4\n'+focuses[0]['quote']}
    assert verification_issue(step,blocks,focuses)
    assert not verification_issue(step|{'question':'2027年春的设备到货由谁安排？'},blocks,focuses)


def test_candidate_uses_hash_verified_issuer_supplement_with_own_pages(setup_data, settings):
    import hashlib
    from radar.models import KeywordCandidate
    from radar.project_analysis import candidate_evidence

    raw=settings.DATA_DIR/'raw'
    raw.mkdir()
    html=raw/'issuer.html'
    pdf=raw/'issuer.pdf'
    html.write_bytes(b'issuer html')
    pdf.write_bytes(b'saved issuer pdf')
    digest=lambda file:hashlib.sha256(file.read_bytes()).hexdigest()
    candidate=KeywordCandidate.objects.create(canonical_url='https://example.com/news',title='issuer',provider='issuer_ir_pdf',
        raw_path='raw/issuer.html',text='广州工厂新线计划于2027年4月启动',
        result={'content_mode':'full_text','source_sha256':digest(html),'verification':{'sources':[
            {'project_original':True,'url':'https://example.com/issuer.pdf','raw_path':'raw/issuer.pdf',
             'sha256':digest(pdf),'pages':[{'page':9,'text':'2027年4月 广州项目'}]}
        ]}})
    blocks=candidate_evidence(candidate,6000)['blocks']
    assert [(b.get('page'),b['hash']) for b in blocks]==[(None,digest(html)),(9,digest(pdf))]
    pdf.write_bytes(b'wrong PDF')
    assert len(candidate_evidence(candidate,6000)['blocks'])==1
