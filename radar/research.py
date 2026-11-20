"""Evidence-preserving business research, separate from procurement and AI."""
import calendar
import copy
import hashlib
import json
import re
from datetime import date
from pathlib import Path
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from filelock import FileLock
from .models import Company,Document,Lead,ResearchEvent

EVENT_TYPES={'existing_presence','market_research','exhibitor_confirmed','export_plan',
 'market_access_registration','entity_planned','entity_established','project_signed',
 'construction_started','operation_started','expansion','relocation','supplier_recruitment',
 'rfq','cancelled_or_withdrawn','finance_support','historical_only','research_pending'}
RELATIONS={'owned_subsidiary','joint_venture','branch','representative_office','distributor',
 'contract_manufacturer','importer','mentioned_organization','unknown'}


def _evidence(entry,text,sources=None,default_hash=''):
    if not isinstance(entry,dict):raise ValueError('证据格式无效')
    quote=entry.get('quote');location=entry.get('location')
    evidence_hash=entry.get('document_hash') or default_hash
    source_text=(sources or {}).get(evidence_hash,{}).get('text',text)
    if sources is not None and evidence_hash not in sources:raise ValueError('证据文件版本不存在')
    if not isinstance(quote,str) or not quote.strip() or quote not in source_text or not isinstance(location,str) or not location.strip():
        raise ValueError('证据缺少逐字原文或具体位置')


def _mainland_evidence(evidence):
    quote=evidence.get('quote','')
    from .china_places import project_city

    if project_city(quote) or re.search(r'广东|広東|廣東|江苏|江蘇|浙江|山东|山東|辽宁|遼寧|中国本土|中国大陆|中国大陸|中华人民共和国', quote):
        return True
    if re.search(r'\b(?:mainland china|china|shanghai|suzhou|tianjin|beijing|guangzhou|shenzhen|wuxi|dalian|qingdao|changzhou|jiangsu|zhejiang|ningbo|dongguan)\b',quote,re.I) and not re.search(r'hong\s*kong|macao|macau|taiwan|japan',quote,re.I):return True
    return bool('中国' in quote and not re.search(r'中国地方|岡山|広島|広岛|鳥取|島根|山口|香港|澳门|澳門|台湾|台灣',quote))


def _semantic(value):
    """Ignore source-document formatting and locations when business fields are unchanged."""
    if isinstance(value,dict):
        return {k:_semantic(v) for k,v in value.items() if k not in {'quote','location','document_id','document_hash','url','observed_at'}}
    if isinstance(value,list):return [_semantic(x) for x in value if not (isinstance(x,dict) and (x.get('field')=='source_note' or x.get('kind')=='source_updated'))]
    return value


def research_progress(dates):
    today=timezone.localdate()
    for item in dates or []:
        if item.get('status')!='planned' or not item.get('value'):continue
        value=str(item['value']);precision=item.get('precision')
        try:
            if precision in {'day','day_or_before'}:end=date.fromisoformat(value)
            elif precision in {'month','month_or_before'}:
                year,month=map(int,value.split('-'));end=date(year,month,calendar.monthrange(year,month)[1])
            elif precision=='year':end=date(int(value),12,31)
            else:continue
        except (ValueError,TypeError):continue
        if end<today:
            completed=any(x.get('kind') in {'completed','operation_started'} and x.get('status')=='actual' for x in dates)
            return 'completed_evidenced' if completed else 'overdue_unconfirmed'
    return 'unknown_or_current'


def _first_factory_fingerprint(text,event_type,participants,dates):
    """Only link reports describing the same explicitly unique first factory."""
    if event_type!='construction_started':return None
    event_date=next((d.get('value') for d in dates if d.get('kind')=='event_date'
                     and d.get('precision')=='day' and d.get('status') in {'stated','derived_same_month','actual'}),None)
    names=[p.get('name','') for p in participants if p.get('role') in {'japan_parent','japan_company'}]
    if not event_date or not names:return None
    for sentence in re.split(r'(?<=。)|\n',text):
        if not re.search(r'(?:中国|国内|在华)(?:的)?(?:首个|首座|第一(?:个|座))[^。\n]{0,18}(?:生产基地|生产工厂|工厂)',sentence):continue
        from .china_places import project_city
        location=project_city(sentence)
        name=next((name for name in names if name and name in sentence),None)
        if name and location:return (name,event_type,event_date,location)
    return None


def ingest_research_record(source,record):
    """Persist one named, source-supported China research event idempotently."""
    text=record.get('text');research=copy.deepcopy(record.get('research') or {})
    if not isinstance(text,str) or not text.strip() or not record.get('title') or not record.get('external_id'):
        raise ValueError('研究记录缺少稳定编号、标题或原文')
    if research.get('event_type') not in EVENT_TYPES:raise ValueError('研究事件类型无效')
    if research.get('china_scope')!='mainland':raise ValueError('未证实中国大陆关系')
    document=record.get('document') or {};rel=document.get('raw_path','')
    supplied=[{**document,'text':text,'url':record.get('url','')},*(record.get('supporting_documents') or [])]
    from django.core.management.base import CommandError
    from .management.commands.backup_data import raw_source_file
    sources={}
    for material in supplied:
        try:path=raw_source_file(material.get('raw_path',''))
        except CommandError as exc:raise ValueError('原始证据文件不可用或路径不安全') from exc
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if digest!=material.get('sha256'):raise ValueError('原始证据哈希不一致')
        if not isinstance(material.get('text'),str):raise ValueError('来源正文缺失')
        sources[digest]=material
    actual=document['sha256']
    _evidence(research.get('china_evidence'),text,sources,actual)
    if not _mainland_evidence(research['china_evidence']):raise ValueError('中国大陆证据不足')
    participants=research.get('participants') or []
    if not participants:raise ValueError('研究任务必须具名')
    for person in participants:
        _evidence(person,text,sources,actual)
        name=person.get('name')
        if not isinstance(name,str) or not name.strip() or len(name)>240 or name not in person['quote']:
            raise ValueError('主体名称必须由原文逐字支持')
        if person.get('relationship','unknown') not in RELATIONS:raise ValueError('实体关系类型无效')
    for item in (research.get('dates') or [])+(research.get('facts') or []):_evidence(item,text,sources,actual)
    size=research.get('size') or {'value':'unknown'}
    if size.get('value','unknown') not in {'micro','sme','mid_large','unknown'}:raise ValueError('企业规模分类无效')
    if size.get('value','unknown')!='unknown':_evidence(size,text,sources,actual)
    semantic=hashlib.sha256(json.dumps(_semantic({'title':record['title'],'research':research}),sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    now=timezone.now();external_id=str(record['external_id'])[:160]
    with FileLock(str(settings.DATA_DIR/'ingest-write.lock'),timeout=30),transaction.atomic():
        lead=Lead.objects.select_for_update().filter(source=source,external_id=external_id).first()
        created=lead is None;updated=bool(lead and lead.content_hash!=semantic)
        company_refs=[]
        for participant in participants:
            company,_=Company.objects.get_or_create(name=participant['name'])
            company_refs.append((participant,company))
        primary=next((company for item,company in company_refs if item.get('role') in {'japan_parent','exporter','japan_company','china_entity'}),company_refs[0][1])
        if created:
            lead=Lead.objects.create(source=source,external_id=external_id,title=str(record['title'])[:600],url=str(record.get('url') or '')[:2000],
                text=text[:350000],content_hash=semantic,company=primary,event_type='early_signal',processing_status='early_signal',
                buyer_role='unknown',summary='具名企业经营研究；不代表物流采购。',metadata={'signal_type':'business_research'})
        else:
            lead.last_seen_at=now
            if updated:
                lead.title=str(record['title'])[:600];lead.url=str(record.get('url') or '')[:2000]
                lead.text=text[:350000];lead.content_hash=semantic;lead.version+=1
                if not lead.metadata.get('manual_buyer'):lead.company=primary
                lead.save(update_fields=['last_seen_at','title','url','text','content_hash','version','company'])
            else:lead.save(update_fields=['last_seen_at'])
        doc,_=Document.objects.get_or_create(lead=lead,content_hash=semantic,defaults={'text':text[:350000],
             'raw_path':rel,'metadata':{'kind':'research_material','url':record.get('url',''),
             'source_document_hash':actual,'media_type':document.get('type',''),
             'source_location':document.get('location','')}})
        source_docs={}
        for digest,material in sources.items():
            source_docs[digest],_=Document.objects.get_or_create(lead=lead,content_hash=digest,defaults={
                'text':material['text'][:350000],'raw_path':material['raw_path'],
                'metadata':{'kind':'research_source','url':material.get('url',''),'source_document_hash':digest,
                            'media_type':material.get('type',''),'parent_hash':semantic,'pages':material.get('pages',[])}})
        for item,company in company_refs:item['company_id']=company.pk
        def attach(item):
            digest=item.get('document_hash') or actual
            material=sources[digest]
            return {**item,'document_id':source_docs[digest].pk,'document_hash':digest,'url':material.get('url',''),
                    'publisher':material.get('publisher',source.name),
                    'source_level':material.get('source_level','source_reported_official')}
        payload={'event_type':research['event_type'],'workstream':research.get('workstream','unknown')[:32],
            'china_scope':'mainland','china_evidence':attach(research['china_evidence']),
            'participants':[attach(item) for item in participants],
            'dates':[attach(item) for item in research.get('dates',[])],
            'facts':[attach(item) for item in research.get('facts',[])],
            'size':attach(size) if size.get('value','unknown')!='unknown' else size,
            'inferences':research.get('inferences',[]),'questions':research.get('questions',[]),
            'next_step':str(research.get('next_step',''))[:4000],'source_document_hash':actual}
        if 'research_snapshot' not in doc.metadata:
            doc.metadata={**doc.metadata,'research_snapshot':payload}
            doc.save(update_fields=['metadata'])
        event,event_created=ResearchEvent.objects.get_or_create(lead=lead,defaults=payload)
        if not event_created and any(getattr(event,name)!=value for name,value in payload.items()):
            for name,value in payload.items():setattr(event,name,value)
            event.save(update_fields=list(payload))
        if created:
            fingerprint=_first_factory_fingerprint(text,event.event_type,event.participants,event.dates)
            if fingerprint:
                candidates=ResearchEvent.objects.select_related('lead').filter(event_type=event.event_type,
                    lead__company=lead.company,lead__duplicate_of__isnull=True).exclude(pk=event.pk)
                for candidate in candidates:
                    if _first_factory_fingerprint(candidate.lead.text,candidate.event_type,candidate.participants,candidate.dates)==fingerprint:
                        lead.duplicate_of=candidate.lead;lead.save(update_fields=['duplicate_of']);break
    return lead,created,updated
