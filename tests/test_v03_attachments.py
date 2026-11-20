import uuid
import pytest
from radar.analysis import enqueue,process_analysis
from radar.enrichment import attachment_facts
from radar.models import Company,Document,Lead,ModelCall,Source


@pytest.mark.django_db
def test_public_attachment_is_idempotent_and_never_calls_model(tmp_path,settings,monkeypatch):
    settings.DATA_DIR=tmp_path
    company=Company.objects.create(name='测试制造有限公司')
    source=Source.objects.create(key='fixture-pdf',name='测试')
    url='https://public.example/approval.pdf'
    lead=Lead.objects.create(source=source,company=company,external_id='pdf',title='关于测试制造有限公司扩建项目生态环境影响评价文件审批的公告',
                             content_hash='a'*64,metadata={'attachments':[{'url':url}]})
    monkeypatch.setattr('radar.enrichment.fetch',lambda *a,**kw:{'url':url,'body':b'%PDF-synthetic'})
    def fake_save(lead,body,url,parent_hash):
        text='测试制造有限公司扩建项目。新增生产变压器18万台/年。扩建后全厂产量50万台/年。'
        return Document.objects.create(lead=lead,content_hash='b'*64,text=text,metadata={'kind':'pdf','parent_hash':parent_hash,'url':url,'pages':[{'page':1,'text':text,'status':'text'}]})
    monkeypatch.setattr('radar.enrichment.save_pdf',fake_save)
    monkeypatch.setattr('radar.analysis.call_provider',lambda *a:pytest.fail('must not call a model'))
    key=str(uuid.uuid4());run,created=enqueue(lead.pk,'attachment',key,attachment_url=url)
    again,new=enqueue(lead.pk,'attachment',key,attachment_url=url)
    assert created and not new and run.pk==again.pk
    process_analysis(run.pk);run.refresh_from_db()
    assert run.status=='succeeded' and ModelCall.objects.count()==0
    doc=Document.objects.get(pk=run.result['document_id'])
    assert doc.metadata['identity']=='matched' and doc.metadata['parent_hash']=='a'*64
    facts=doc.metadata['fact_candidates']
    assert [f['scope'] for f in facts]==['added','total']
    assert [f['values'][0]['value'] for f in facts]==['18','50']
    assert all(f['block_id']==f'd{doc.pk}:p1' for f in facts)


@pytest.mark.django_db
def test_attachment_rejects_unknown_url_and_changed_parent(tmp_path,settings,monkeypatch):
    settings.DATA_DIR=tmp_path
    source=Source.objects.create(key='fixture-reject',name='测试')
    url='https://public.example/approved.pdf'
    lead=Lead.objects.create(source=source,external_id='1',title='项目',content_hash='a'*64,metadata={'attachments':[{'url':url}]})
    with pytest.raises(ValueError):enqueue(lead.pk,'attachment',str(uuid.uuid4()),attachment_url='http://127.0.0.1/private.pdf')
    run,_=enqueue(lead.pk,'attachment',str(uuid.uuid4()),attachment_url=url)
    Lead.objects.filter(pk=lead.pk).update(content_hash='b'*64)
    monkeypatch.setattr('radar.enrichment.fetch',lambda *a,**kw:pytest.fail('changed source must not fetch'))
    process_analysis(run.pk);run.refresh_from_db()
    assert run.status=='failed' and run.error_code=='source_changed'


def test_numeric_candidates_keep_page_and_explicit_scope():
    from types import SimpleNamespace
    doc=SimpleNamespace(pk=9,metadata={'pages':[{'page':1,'text':'新增生产变压器18万台/年、逆变器46.8万台/年。扩建后全厂年产50万台/年。'}]})
    facts=attachment_facts(doc)
    assert [(f['scope'],[v['value'] for v in f['values']]) for f in facts]==[('added',['18','46.8']),('total',['50'])]
    assert all(f['quote'] in doc.metadata['pages'][0]['text'] for f in facts)


@pytest.mark.django_db
def test_user_material_upload_stays_local_and_preserves_email_child(tmp_path,settings,client,monkeypatch):
    from email.message import EmailMessage
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.urls import reverse
    from radar.models import AnalysisRun
    settings.DATA_DIR=tmp_path
    source=Source.objects.create(key='user-material',name='测试')
    lead=Lead.objects.create(source=source,external_id='1',title='测试项目',content_hash='a'*64)
    message=EmailMessage();message['Subject']='测试';message.set_content('用户提供的线索，不自动读取 https://example.invalid/')
    message.add_attachment('<html><p>测试物流公告</p><script>bad()</script></html>'.encode(),maintype='text',subtype='html',filename='notice.html')
    def no_network(*args,**kwargs):pytest.fail('user material must not fetch remote URLs')
    monkeypatch.setattr('radar.enrichment.fetch',no_network)
    response=client.post(reverse('radar:upload_pdf',args=[lead.pk]),{
        'request_key':str(uuid.uuid4()),'material':SimpleUploadedFile('test.eml',message.as_bytes())})
    assert response.status_code==302
    run=AnalysisRun.objects.get(task_type='material');process_analysis(run.pk);run.refresh_from_db()
    assert run.status=='succeeded' and ModelCall.objects.count()==0
    original=Document.objects.get(pk=run.result['document_id'])
    child=Document.objects.get(pk=run.result['children'][0])
    assert original.metadata['identity']=='conflict_or_unverified'
    assert child.metadata['parent_material_hash']==original.content_hash
    assert '测试物流公告' in child.text and 'bad()' not in child.text
    assert (tmp_path/original.raw_path).read_bytes()==message.as_bytes()


@pytest.mark.django_db
def test_user_html_never_claims_verified_identity(tmp_path,settings,client):
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.urls import reverse
    from radar.models import AnalysisRun
    settings.DATA_DIR=tmp_path
    lead=Lead.objects.create(source=Source.objects.create(key='user-html',name='测试'),external_id='2',title='项目',content_hash='a'*64)
    response=client.post(reverse('radar:upload_pdf',args=[lead.pk]),{'request_key':str(uuid.uuid4()),
        'material':SimpleUploadedFile('材料.html','<html><p>网页材料</p></html>'.encode())})
    assert response.status_code==302
    run=AnalysisRun.objects.get(task_type='material');process_analysis(run.pk);run.refresh_from_db()
    doc=Document.objects.get(pk=run.result['document_id'])
    assert run.status=='succeeded' and doc.metadata['identity']=='conflict_or_unverified'
    assert '网页材料' in doc.text and ModelCall.objects.count()==0


@pytest.mark.django_db(transaction=True)
def test_backup_includes_original_and_parsed_user_pdf_paths(tmp_path,settings):
    import zipfile
    from django.core.management import call_command
    from radar.management.commands.restore_data import validate_archive
    settings.DATA_DIR=tmp_path/'data'
    original=settings.DATA_DIR/'raw'/'uploads'/'sample.pdf'
    parsed=settings.DATA_DIR/'raw'/'attachments'/'sample.pdf'
    for path in (original,parsed):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'%PDF-example')
    lead=Lead.objects.create(source=Source.objects.create(key='backup-material',name='测试'),external_id='1',title='项目',content_hash='a'*64)
    Document.objects.create(lead=lead,content_hash='b'*64,text='材料',raw_path='raw/attachments/sample.pdf',
        metadata={'kind':'pdf','user_import':True,'upload_path':'raw/uploads/sample.pdf'})
    archive=tmp_path/'backup.zip';call_command('backup_data',output=str(archive),verbosity=0)
    with zipfile.ZipFile(archive) as z:
        validate_archive(z)
        assert {'raw/uploads/sample.pdf','raw/attachments/sample.pdf'}<=set(z.namelist())
