"""Evidence acquisition precedes entity suggestions; no model memory as a search engine."""
import hashlib,json,re,subprocess,sys,tempfile,time
from pathlib import Path
from urllib.parse import quote_plus,urljoin
import httpx
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from filelock import FileLock
from lxml import html
from .models import Document, EntityCandidate, SearchCall
from .evidence import reextract
from .entities import organizations
from .public_fetch import fetch,robots_allowed,FetchError
from .ai_config import secret
from collectors.sources import node_text

def save_pdf(lead,body,url='',parent_hash=''):
    if len(body)>10_000_000 or not body.startswith(b'%PDF-'): raise FetchError('invalid_or_oversize_pdf')
    digest=hashlib.sha256(body).hexdigest(); path=settings.DATA_DIR/'raw'/'attachments'/f'{digest}.pdf'
    path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists(): path.write_bytes(body)
    with tempfile.TemporaryDirectory() as tmp:
        result=Path(tmp)/'pdf.json'
        try:
            subprocess.run([sys.executable,'-m','radar.pdf_extract',str(path),str(result)],cwd=settings.BASE_DIR,timeout=20,check=True,capture_output=True)
            parsed=json.loads(result.read_text(encoding='utf-8'))
        except (subprocess.TimeoutExpired,subprocess.CalledProcessError):
            parsed={'status':'processing_limit','pages':[]}
    doc,_=Document.objects.get_or_create(lead=lead,content_hash=digest,defaults={'text':''})
    doc.text='\n'.join(p['text'] for p in parsed['pages'])
    doc.raw_path=path.relative_to(settings.DATA_DIR).as_posix()
    doc.metadata={**doc.metadata,'kind':'pdf','url':url or doc.metadata.get('url',''),
                  'parent_hash':parent_hash or doc.metadata.get('parent_hash',''),**parsed}
    doc.save(update_fields=['text','raw_path','metadata'])
    return doc


def attachment_identity(lead, text):
    """An attachment link is provenance, not proof of project identity."""
    compact=lambda value:re.sub(r'[\s（）()【】\[\]《》:：—_-]','',value or '')
    title=re.sub(r'^关于','',lead.title)
    title=re.sub(r'(?:生态环境影响评价文件审批|环境影响评价文件审批|生态环境影响报告表审批)的公告$','',title)
    project=compact(title);body=compact(text)
    company=compact(lead.company.name) if lead.company_id else ''
    if len(project)>=10 and project in body and company and company in body:
        return 'matched','project_title_and_named_company_in_attachment'
    return 'conflict_or_unverified','project_or_company_not_independently_confirmed'


def attachment_facts(doc):
    """Conservative rule candidates, each tied to a verbatim page excerpt."""
    facts=[]
    for page in doc.metadata.get('pages',[]):
        text=page.get('text','')
        for piece in re.split(r'(?<=[。；])',text):
            flat=re.sub(r'\s+','',piece)
            if not re.search(r'新增|扩建后|全厂|现有|原有|仓库|原辅料',flat):continue
            scope='added' if re.search(r'新增(?:生产|两条|产线|生产线)',flat) else ('total' if re.search(r'扩建后|全厂',flat) else ('existing' if re.search(r'现有|原有',flat) else 'unknown'))
            values=[]
            for m in re.finditer(r'(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>万?台|万?件|万?吨|条)(?P<period>\s*/\s*年|每年)?',piece):
                values.append({'value':m.group('value'),'unit':m.group('unit'),'period':re.sub(r'\s+','',m.group('period') or ''),'context':re.sub(r'\s+','',piece[max(0,m.start()-18):m.start()])})
            if not values and not re.search(r'仓库|原辅料',flat):continue
            facts.append({'scope':scope,'values':values,'quote':piece.strip(), 'page':page['page'],
                          'block_id':f'd{doc.pk}:p{page["page"]}'})
    return facts[:100]


def acquire_attachment(run):
    from .analysis import AnalysisFailure
    from .models import Lead
    from .public_fetch import request_budget
    lead=Lead.objects.select_related('company').get(pk=run.lead_id)
    if lead.content_hash!=run.input_snapshot['source_hash']:raise AnalysisFailure('source_changed')
    url=run.input_snapshot['attachment_url']
    allowed={a.get('url') for a in lead.metadata.get('attachments',[]) if isinstance(a,dict)}
    if url not in allowed:raise AnalysisFailure('attachment_not_in_current_source')
    try:
        with request_budget(8,60):response=fetch(url,max_bytes=10_000_000,timeout=45)
        if Lead.objects.get(pk=lead.pk).content_hash!=run.input_snapshot['source_hash']:raise AnalysisFailure('source_changed')
        doc=save_pdf(lead,response['body'],response['url'],lead.content_hash)
    except FetchError as exc:raise AnalysisFailure(str(exc)[:64]) from exc
    identity,reason=attachment_identity(lead,doc.text)
    doc.metadata={**doc.metadata,'identity':identity,'identity_reason':reason,
                  'requested_url':url,'downloaded_at':timezone.now().isoformat(),
                  'redirect_chain':response.get('redirect_chain',[]),'robots_redirect_chains':response.get('robots_redirect_chains',[])}
    doc.metadata['fact_candidates']=attachment_facts(doc)
    doc.save(update_fields=['metadata'])
    return {'origin':'public_attachment','document_id':doc.pk,'document_hash':doc.content_hash,
            'identity':identity,'identity_reason':reason,'facts_count':len(doc.metadata['fact_candidates']),
            'note':'附件为项目材料，审批不等于投产或物流采购；数值候选须结合原文核对。'}

def project_match(title,text,candidate_title,candidate_text):
    """Same title alone is insufficient if explicit year, lot or phase conflicts."""
    def years(v): return set(re.findall(r'20\d{2}',v))
    a,b=years(title),years(candidate_title)
    if a and b and a!=b:return False,'year_conflict'
    phase=lambda v: 'award' if re.search('中标|成交|结果',v) else ('change' if re.search('变更|更正|延期',v) else 'invitation')
    if phase(title)!=phase(candidate_title):return False,'phase_conflict'
    lots=lambda v:set(re.findall(r'第[一二三四五六七八九十\d]+(?:标段|包)|标段[一二三四五六七八九十\d]+',v))
    if lots(title)!=lots(candidate_title):return False,'lot_conflict'
    norm=lambda v:re.sub(r'[\s【】\[\]（）()：:—_-]','',v)
    original=norm(title); candidate=norm(candidate_title)
    exact=original==candidate or (len(original)>15 and original in candidate)
    nums=lambda v:set(re.findall(r'(?:项目编号|招标编号|采购编号)\s*[:：]\s*([A-Za-z0-9-]{6,60})',v))
    identifiers=nums(text); other=nums(candidate_text)
    if identifiers and other and not identifiers.intersection(other):return False,'project_number_conflict'
    if exact and (not identifiers or bool(identifiers.intersection(other))):return True,'title_year_lot_phase_match'
    return False,'insufficient_identity'

def search(run,cfg,query):
    if not cfg.search_enabled or not secret(cfg,search=True):return [],'search_not_configured'
    with FileLock(str(settings.DATA_DIR/'search-budget.lock'),timeout=5),transaction.atomic():
        if SearchCall.objects.filter(requested_at__date=timezone.localdate()).count()>=cfg.search_daily_limit:return [],'search_daily_limit'
        call=SearchCall.objects.create(analysis=run,query=query[:600])
    try:
        with httpx.Client(timeout=12,follow_redirects=False) as c:
            response=c.get('https://api.search.brave.com/res/v1/web/search',params={'q':query,'count':3},headers={'X-Subscription-Token':secret(cfg,search=True),'Accept':'application/json'})
        response.raise_for_status()
        urls=[x['url'] for x in response.json().get('web',{}).get('results',[])][:3]
        call.urls=urls;call.status='succeeded';return urls,'searched'
    except Exception:
        call.status='failed';return [],'search_failed'
    finally:call.save(update_fields=['urls','status'])

def complete_subject(run,cfg):
    lead=run.lead
    for doc in lead.documents.filter(metadata__kind='pdf',metadata__status='queued'):
        path=(settings.DATA_DIR/doc.raw_path).resolve()
        if path.is_relative_to((settings.DATA_DIR/'raw').resolve()):
            save_pdf(lead,path.read_bytes(),doc.metadata.get('url',''),doc.metadata.get('parent_hash',''))
    extracted=reextract(lead.pk)
    trace=[{'step':'reparse','status':'completed','extraction_id':extracted.pk}]
    candidates=[]
    for e in extracted.result.get('entities',[]):
        candidates.append({'name':e['name'],'role':e['role'],'quote':e['quote'],'url':lead.url,'status':'existing_document'})
    for doc in lead.documents.filter(metadata__kind='pdf'):
        for page in doc.metadata.get('pages',[]):
            for e in organizations('',page['text']):
                EntityCandidate.objects.get_or_create(lead=lead,analysis=run,name=e['name'],role=e['role'],document_hash=doc.content_hash,defaults={'source_url':doc.metadata.get('url',''),'evidence':{**e,'page':page['page'],'document_id':doc.pk},'status':'attachment_candidate'})
                candidates.append({**e,'page':page['page'],'document_id':doc.pk,'status':'attachment_candidate'})
    original=lead.documents.filter(content_hash=lead.content_hash).first()
    links=[]
    attachments=lead.metadata.get('attachments',[])
    links.extend(x['url'] for x in attachments if isinstance(x,dict) and str(x.get('url','')).lower().split('?')[0].endswith('.pdf'))
    if original and original.raw_path:
        path=(settings.DATA_DIR/original.raw_path).resolve()
        if path.is_relative_to((settings.DATA_DIR/'raw').resolve()) and path.suffix=='.html':
            tree=html.fromstring(path.read_text(encoding='utf-8'))
            for a in tree.xpath('//a[@href]'):
                if re.search('原文|原始|采购公告|项目文件|招标公告',a.text_content()):
                    links.append(urljoin(lead.url,a.get('href')))
    query=lead.title[:250]
    found,search_status=search(run,cfg,query)
    trace.append({'step':'search','status':search_status,'query':query,'urls':found,
                  'manual_query':'https://www.bing.com/search?q='+quote_plus(query)})
    links=list(dict.fromkeys(links+found))[:3]
    start=time.monotonic()
    for url in links:
        if time.monotonic()-start>45:
            trace.append({'step':'read','url':url,'status':'time_budget'});break
        try:
            if not robots_allowed(url):raise FetchError('robots_or_robots_unavailable')
            response=fetch(url,max_bytes=10_000_000)
            if response['body'].startswith(b'%PDF-'):
                doc=save_pdf(lead,response['body'],response['url'],lead.content_hash)
                # An attachment link proves provenance, not project identity. Never
                # manufacture its title from the parent announcement to pass matching.
                text=doc.text; candidate_title=text[:600]
            else:
                tree=html.fromstring(response['body']); candidate_title=tree.xpath('string(//h1)') or tree.xpath('string(//title)')
                for n in tree.xpath('//nav|//footer|//script|//style'):n.drop_tree()
                text=node_text(tree)
                digest=hashlib.sha256(response['body']).hexdigest()
                path=settings.DATA_DIR/'raw'/'linked'/f'{digest}.html';path.parent.mkdir(parents=True,exist_ok=True)
                if not path.exists():path.write_bytes(response['body'])
                doc,_=Document.objects.get_or_create(lead=lead,content_hash=digest,defaults={'text':text,'raw_path':path.relative_to(settings.DATA_DIR).as_posix(),'metadata':{'kind':'linked_html','url':response['url'],'parent_hash':lead.content_hash}})
            matched,reason=project_match(lead.title,lead.text,candidate_title,text)
            doc.metadata={**doc.metadata,'identity':'matched' if matched else 'conflict_or_unverified','identity_reason':reason}
            doc.save(update_fields=['metadata'])
            trace.append({'step':'read','url':response['url'],'status':reason,'hash':doc.content_hash,'document_id':doc.pk})
            for e in organizations(candidate_title,text):
                status='identity_matched' if matched else 'conflict_or_unverified'
                EntityCandidate.objects.get_or_create(lead=lead,analysis=run,name=e['name'],role=e['role'],document_hash=doc.content_hash,defaults={'source_url':response['url'],'evidence':{**e,'identity':reason},'status':status})
                candidates.append({**e,'url':response['url'],'status':status})
        except Exception as exc:
            trace.append({'step':'read','url':url,'status':str(exc) if isinstance(exc,FetchError) else type(exc).__name__})
    return {'origin':'evidence_workflow','trace':trace,'candidates':candidates,'search_status':search_status,
            'note':'候选供人工确认。已重解析本地事实；跨站候选尚未自动覆盖人工字段。'}
