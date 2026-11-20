"""Versioned normalization and bounded selection across all paragraphs/pages."""
import hashlib
import re
from datetime import datetime
from pathlib import Path
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from filelock import FileLock
from collectors.sources import SOURCES, Listing, parse_detail
from .models import Lead, Document, Company, ExtractionRun, EntityCandidate
from .extraction import extract_event, recruitment_signal_event
from .entities import subject_reason

EXTRACTOR_VERSION = 'rules-v02.11'

def document_blocks(doc):
    pages = doc.metadata.get('pages')
    if pages:
        return [{'id': f'd{doc.pk}:p{x["page"]}', 'document_id': doc.pk, 'hash': doc.content_hash,
                 'url': doc.metadata.get('url', ''), 'page': x['page'], 'text': x['text']} for x in pages if x['text'].strip()]
    return [{'id': f'd{doc.pk}:b{i+1}', 'document_id': doc.pk, 'hash': doc.content_hash,
             'url': doc.metadata.get('url', ''), 'paragraph': i+1, 'text': line.strip()}
            for i, line in enumerate(doc.text.splitlines()) if line.strip()]

def current_documents(lead):
    docs=list(lead.documents.filter(content_hash=lead.content_hash))
    docs += [d for d in lead.documents.filter(metadata__kind__in=['pdf','linked_html']) if (d.metadata.get('kind')=='pdf' and d.metadata.get('identity')!='conflict_or_unverified') or d.metadata.get('identity')=='matched']
    return list({d.pk:d for d in docs}.values())

def select_evidence(lead, limit=24000):
    blocks = []
    normalized = lead.extractions.filter(source_hash=lead.content_hash).order_by('-id').first()
    for doc in current_documents(lead):
        if normalized and doc.content_hash==lead.content_hash:
            doc.text=normalized.result.get('normalized_text',doc.text)
        blocks.extend(document_blocks(doc))
    # Split oversized pages instead of silently clipping the beginning of a PDF.
    chunks = []
    for block in blocks:
        for i in range(0, len(block['text']), 1800):
            chunks.append({**block, 'id': block['id']+f':c{i//1800+1}', 'text': block['text'][i:i+1800]})
    pattern = r'采购人|采购方|招标人|招募|寻源|建设单位|投标|截止|报名|资格|运输|仓储|配送|物流|报关'
    ranked = sorted(enumerate(chunks), key=lambda p: (-len(re.findall(pattern,p[1]['text'])), p[0]))
    chosen, used = [], 0
    for i, block in ranked:
        if used+len(block['text']) <= limit:
            chosen.append((i,block)); used += len(block['text'])
    selected=[b for _,b in sorted(chosen)]
    return {'blocks':selected, 'total_blocks':len(chunks), 'processed_blocks':len(selected),
            'unprocessed_ids':[b['id'] for b in chunks if b['id'] not in {x['id'] for x in selected}],
            'full_coverage':len(selected)==len(chunks), 'selected_chars':used,
            'document_hashes':sorted({b['hash'] for b in blocks})}

def reextract(lead_id):
    lead = Lead.objects.select_related('source').get(pk=lead_id)
    original_hash = lead.content_hash
    doc = lead.documents.filter(content_hash=original_hash).first()
    text = lead.text
    if doc and doc.raw_path and lead.source.key in SOURCES:
        path = (settings.DATA_DIR/doc.raw_path).resolve()
        if path.is_relative_to((settings.DATA_DIR/'raw').resolve()) and path.suffix=='.html' and path.is_file():
            item = parse_detail(lead.source.key, path.read_text(encoding='utf-8'), Listing(lead.external_id,lead.url,lead.title,metadata=doc.metadata))
            text = item['text']
    is_recruitment = (lead.source.kind == 'recruitment' or lead.source.key in {'japan_directory_yingchuang','japan_directory_mol_recruitment'}) and lead.metadata.get('signal_type') == 'job'
    event = extract_event(lead.title,text,'early_signal' if is_recruitment else lead.source.kind,listed_deadline=doc.metadata.get('listed_deadline') if doc else None)
    if is_recruitment:
        recruitment_signal_event(event, published=lead.metadata.get('posting_date_status') == 'source_stated')
    if lead.metadata.get('invitation_required'):
        if event['processing_status']=='actionable':event['processing_status']='needs_review'
        event['missing_fields'].append('invitation_required')
    event['subject_reason'] = ('source_redacted' if lead.metadata.get('employer_display') else 'parser_missed') if is_recruitment and not lead.company_id else subject_reason(event,text,lead.access_level)
    if lead.access_level != 'public' and event['processing_status']=='actionable':
        event['processing_status']='needs_review'
    event['normalized_text'] = text
    docs = [{'id':d.pk,'hash':d.content_hash} for d in current_documents(lead)]
    with FileLock(str(settings.DATA_DIR/'ingest-write.lock'),timeout=30), transaction.atomic():
        current = Lead.objects.select_for_update().get(pk=lead_id)
        run = ExtractionRun.objects.create(lead=current,source_hash=original_hash,extractor_version=EXTRACTOR_VERSION,result=event,documents=docs)
        if current.content_hash != original_hash:
            return run
        if not current.metadata.get('manual_buyer') and not is_recruitment:
            current.company = Company.objects.get_or_create(name=event['buyer_name'])[0] if event.get('buyer_name') else None
        if current.metadata.get('manual_buyer') and current.company_id:
            event['subject_reason']=''
            event['missing_fields']=[x for x in event['missing_fields'] if x not in ['buyer_name','related_company']]
        for field in ['summary','buyer_role','event_type','processing_status','service_types','regions','missing_fields','evidence','qualifications','deadline_kind','deadline_has_time','subject_reason','deadline_semantics']:
            setattr(current,field,event.get(field,getattr(current,field)))
        current.participation_path=event.get('participation_path') or ''
        current.deadline_at=datetime.fromisoformat(event['deadline']) if event.get('deadline') and 'T' in event['deadline'] else None
        current.deadline=current.deadline_at.date() if current.deadline_at else (event.get('deadline') or None)
        current.extraction_version += 1
        current.extraction_method='rules'
        current.save(update_fields=['company','summary','buyer_role','event_type','processing_status','service_types','regions','missing_fields','evidence','qualifications','deadline_kind','deadline_has_time','subject_reason','deadline_semantics','participation_path','deadline','deadline_at','extraction_version','extraction_method'])
        for e in event.get('entities',[]):
            EntityCandidate.objects.get_or_create(lead=current,name=e['name'],role=e['role'],document_hash=original_hash,defaults={'source_url':lead.url,'evidence':e})
    return run
