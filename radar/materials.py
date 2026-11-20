"""Bounded, local-only handling of user-provided EML, HTML and PDF evidence."""
import hashlib
from email import policy
from email.parser import BytesParser
from pathlib import Path
from lxml import html
from django.conf import settings
from .analysis import AnalysisFailure
from .models import Document,Lead
from .enrichment import attachment_facts,attachment_identity,save_pdf
from collectors.sources import node_text


def _html_text(body):
    try:
        body.decode('utf-8')
        parser=html.HTMLParser(encoding='utf-8')
    except UnicodeDecodeError:
        parser=html.HTMLParser()
    tree=html.fromstring(body,parser=parser)
    for node in tree.xpath('//script|//style|//noscript|//iframe|//object'):
        node.drop_tree()
    return node_text(tree)[:350000]


def _child(lead,body,kind,parent_hash):
    digest=hashlib.sha256(body).hexdigest()
    path=settings.DATA_DIR/'raw'/'uploads'/f'{digest}.{kind}'
    path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists():path.write_bytes(body)
    if kind=='pdf':
        doc=save_pdf(lead,body,parent_hash=lead.content_hash)
        identity,reason=attachment_identity(lead,doc.text)
        doc.metadata={**doc.metadata,'user_import':True,'parent_material_hash':parent_hash,
                      'upload_path':path.relative_to(settings.DATA_DIR).as_posix(),
                      'identity':identity,'identity_reason':reason,'fact_candidates':attachment_facts(doc)}
        doc.save(update_fields=['metadata'])
        return doc
    doc,created=Document.objects.get_or_create(lead=lead,content_hash=digest,defaults={
        'text':_html_text(body),'raw_path':path.relative_to(settings.DATA_DIR).as_posix(),
        'metadata':{'kind':'user_html','user_import':True,'parent_material_hash':parent_hash,
                    'parent_hash':lead.content_hash,'identity':'conflict_or_unverified'}})
    return doc


def process_material(run):
    lead=Lead.objects.select_related('company').get(pk=run.lead_id)
    if lead.content_hash!=run.input_snapshot['source_hash']:raise AnalysisFailure('source_changed')
    doc=Document.objects.get(pk=run.input_snapshot['material_id'],lead=lead)
    path=(settings.DATA_DIR/doc.metadata.get('upload_path',doc.raw_path)).resolve()
    if not path.is_relative_to((settings.DATA_DIR/'raw'/'uploads').resolve()) or not path.is_file():raise AnalysisFailure('material_missing')
    body=path.read_bytes()
    if len(body)>10_000_000 or hashlib.sha256(body).hexdigest()!=run.input_snapshot['material_hash']:
        raise AnalysisFailure('material_integrity_failed')
    kind=doc.metadata.get('input_type');children=[];skipped=[]
    if kind=='pdf':
        doc=save_pdf(lead,body,parent_hash=lead.content_hash)
        identity,reason=attachment_identity(lead,doc.text)
        doc.metadata={**doc.metadata,'user_import':True,'identity':identity,'identity_reason':reason,
                      'fact_candidates':attachment_facts(doc)}
        doc.save(update_fields=['metadata'])
    elif kind=='html':
        doc.text=_html_text(body);doc.metadata={**doc.metadata,'status':'text','identity':'conflict_or_unverified'}
        doc.save(update_fields=['text','metadata'])
    elif kind=='eml':
        message=BytesParser(policy=policy.default).parsebytes(body)
        parts=[]
        for index,part in enumerate(message.walk()):
            if index>=50:skipped.append('part_limit');break
            if part.is_multipart():continue
            data=part.get_payload(decode=True) or b''
            ctype=part.get_content_type();filename=part.get_filename() or ''
            attached=part.get_content_disposition()=='attachment' or bool(filename)
            if attached and (ctype=='application/pdf' or filename.lower().endswith('.pdf')) and data.startswith(b'%PDF-'):
                child=_child(lead,data,'pdf',doc.content_hash);children.append(child.pk)
            elif attached and (ctype=='text/html' or filename.lower().endswith(('.html','.htm'))):
                child=_child(lead,data,'html',doc.content_hash);children.append(child.pk)
            elif not attached and ctype in {'text/plain','text/html'}:
                if ctype=='text/html':parts.append(_html_text(data))
                else:
                    try:parts.append(data.decode(part.get_content_charset() or 'utf-8','replace'))
                    except LookupError:parts.append(data.decode('utf-8','replace'))
            elif attached:skipped.append(filename[:120] or ctype)
        doc.text='\n\n'.join(parts)[:350000]
        doc.metadata={**doc.metadata,'status':'text','identity':'conflict_or_unverified',
                      'child_document_ids':children,'unsupported_attachments':skipped}
        doc.save(update_fields=['text','metadata'])
    else:raise AnalysisFailure('unsupported_material')
    return {'origin':'user_material','document_id':doc.pk,'document_hash':doc.content_hash,'children':children,
            'note':'用户提供材料仅供核实，来源和项目身份未自动确认；未访问邮件链接，未调用模型。'}
