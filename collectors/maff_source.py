"""Bounded source discovery for MAFF export plans, without model calls."""
import hashlib,json,re,time,unicodedata
from django.conf import settings
from radar.management.commands.backup_data import raw_source_file
from .candidate_state import CandidateLedger,Limits,LimitReached
from .maff_exports import ENTRY,parse_maff_index,parse_maff_plan
from .research_common import read_original,pdf_pages


def _fiscal_order(entry):
    label=entry.get('source_fiscal_year','')
    m=re.search(r'令和(\d+)',label)
    if m:return 2018+int(m.group(1))
    m=re.search(r'20\d{2}',label)
    return int(m.group()) if m else 0


def plan_identity(plan,pages):
    """Merge category links to the same named plan and normalized text version."""
    normalize=lambda s:re.sub(r'\s+','',unicodedata.normalize('NFKC',str(s)))
    fields=plan.get('fields',{})
    identity=[normalize(plan.get('company_name','')),normalize(fields.get('product','')),
              normalize(fields.get('source_fiscal_year','')),
              normalize('\n'.join(p.get('text','') for p in pages))]
    return 'maff:'+hashlib.sha256(json.dumps(identity,ensure_ascii=False).encode()).hexdigest()


def record_from_plan(entry,plan,pages,pdf_evidence,index_evidence,index_text):
    if not plan.get('accepted'):raise ValueError('plan_not_accepted')
    primary=pdf_evidence['sha256'];index_hash=index_evidence['sha256']
    evidence=plan.get('evidence',[])
    def find(field,*,pdf_only=False,value=None):
        return next((x for x in evidence if x.get('field')==field and (not pdf_only or x.get('document_hash')==primary)
                     and (value is None or x.get('value')==value)),None)
    name=find('applicant');china=find('market',pdf_only=True,value='china_mainland')
    if not name or not china:raise ValueError('named_applicant_or_china_evidence_missing')
    def convert(x):
        return {'quote':x['quote'],'location':x.get('block_id') or ('page:'+str(x.get('page'))),
                'document_hash':x['document_hash']}
    participants=[{'name':plan['company_name'],'role':'exporter','relationship':'unknown',**convert(name)}]
    for rel in plan['fields'].get('relationships',[]):
        item=next((x for x in evidence if x.get('field')=='relationship' and x.get('value')==rel.get('name')),None)
        if item:participants.append({'name':rel['name'],'role':rel.get('role','unknown'),
             'relationship':'importer' if rel.get('role')=='importer' else 'unknown',
             'market_scope':rel.get('market_scope','unknown'),**convert(item)})
    facts=[]
    for item in evidence:
        if item.get('field') in {'product','market'}:
            facts.append({'field':item['field'],'value':str(item.get('value','')),**convert(item)})
    for mention in plan['fields'].get('market_mentions',[]):
        item=next((x for x in evidence if x.get('field')=='market' and x.get('quote')==mention.get('quote')),None)
        if item:facts.append({'field':'market_stage','value':mention.get('role','unqualified'),**convert(item)})
    for metric in plan['fields'].get('metrics',[]):
        item=next((x for x in evidence if x.get('field')=='metric' and x.get('quote')==metric.get('quote')),None)
        if item:facts.append({'field':metric['kind'],'value':', '.join(metric.get('values',[])),
            'unit':metric.get('unit',''),'market_scope':metric.get('market_scope','unassigned'),**convert(item)})
    dates=[];fiscal=find('source_fiscal_year')
    if fiscal:dates.append({'kind':'plan_fiscal_year','value':fiscal['value'],'precision':'fiscal_year','status':'planned',**convert(fiscal)})
    text='\n'.join(p.get('text','') for p in pages)
    return {'external_id':plan_identity(plan,pages),'url':entry['url'],'title':entry['title'],'text':text,
       'document':{'raw_path':pdf_evidence['raw_path'],'sha256':primary,'type':'pdf','pages':pages},
       'supporting_documents':[{'raw_path':index_evidence['raw_path'],'sha256':index_hash,'type':'html',
                                'url':index_evidence['url'],'text':index_text}],
       'research':{'event_type':'export_plan','workstream':'export_to_china','china_scope':'mainland',
          'china_evidence':convert(china),'participants':participants,'dates':dates,'facts':facts,
          'size':{'value':'unknown'},'questions':plan.get('questions',[]),
          'inferences':['可人工核实对华进口、仓配及运输安排，原文不证明采购。'],
          'next_step':'核实出口计划现状、产品准入及对应物流安排。'}}


def collect_maff(source):
    options=source.options or {};plan=Limits(options);ledger=CandidateLedger(source.checkpoint,'maff_candidates_v1')
    logs=[];items=[];errors=[];excluded=[];manual_review=[];stopped=''
    try:
        index,index_evidence=plan.read(read_original,ENTRY);logs.append(index_evidence)
        records=parse_maff_index(index['body']);index_text='\n'.join(x['list_quote'] for x in records)
        for entry in sorted(records,key=_fiscal_order,reverse=True):
            entry['list_hash']=index_evidence['sha256']
            ledger.add(entry['id'],entry['url'],entry.get('source_fiscal_year',''),index_evidence,entry=entry)
    except LimitReached as exc:stopped=str(exc);records=[];index_evidence={};index_text=''
    except Exception as exc:
        return {'items':[],'status':'blocked','error':str(exc)[:500],'stats':{'error_count':1,'errors':[str(exc)[:200]],'requests':logs,'coverage_complete':False},'next_cursor':1}
    for candidate in ledger.pending():
        if len(items)>=plan.accepted:stopped=stopped or 'accepted_limit';break
        try:
            listing=candidate['listing_evidence']
            if listing['sha256']==index_evidence['sha256']:
                listing_text=index_text
            else:
                old_body=raw_source_file(listing['raw_path']).read_bytes()
                if hashlib.sha256(old_body).hexdigest()!=listing['sha256']:raise ValueError('listing_evidence_hash_mismatch')
                listing_text='\n'.join(x['list_quote'] for x in parse_maff_index(old_body))
            response,evidence=plan.read(read_original,candidate['url'],detail=True,max_bytes=10_000_000,timeout=25);logs.append(evidence)
            if not response['body'].startswith(b'%PDF-'):raise ValueError('not_pdf')
            parsed=pdf_pages(evidence['raw_path'],remaining_seconds=plan.seconds-(time.monotonic()-plan.started))
            outcome=parse_maff_plan(parsed['pages'],candidate['entry'],evidence['sha256'],total_pages=parsed.get('total_pages'))
            if not outcome['accepted']:
                row={'id':candidate['id'],'url':candidate['url'],'reason':outcome['reason']}
                candidate['last_reason']=outcome['reason']
                if outcome['reason'] in {'text_incomplete_manual_review','applicant_conflict_manual_review'}:
                    manual_review.append(row);ledger.done(candidate,'manual_review')
                else:excluded.append(row);ledger.done(candidate,'excluded')
                continue
            items.append(record_from_plan(candidate['entry'],outcome,parsed['pages'],evidence,listing,listing_text))
            ledger.done(candidate,'ingested')
        except LimitReached as exc:stopped=str(exc);break
        except Exception as exc:
            if str(exc) in {'time_limit','total_request_limit'}:stopped=str(exc);break
            ledger.failed(candidate,exc);errors.append({'url':candidate['url'],'error':type(exc).__name__+': '+str(exc)[:180]})
    pending=sum(x.get('status') in {'pending','retry'} for x in ledger.entries.values())
    return {'items':items,'status':'partial' if errors or stopped or pending else 'success','next_cursor':1,
        'collector_checkpoint':ledger.save(),
        'stats':{'requests':logs,'list_rows_checked':len(records),'details_attempted':plan.detail_calls,'details_ok':len(items),
            'pending':pending,'excluded':excluded,'manual_review':manual_review,'error_count':len(errors),'errors':errors,'limit_reason':stopped,
            'coverage_complete':False,'scope':'公开计划目录，按计划年度优先及预算续采；目标不是已发生订单'}}
