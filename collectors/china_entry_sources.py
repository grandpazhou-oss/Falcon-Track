"""Manual four-source runs. Keep admission and evidence separate from existing sales scores."""
import hashlib
import re
from collections import defaultdict
from urllib.parse import urlsplit

from django.utils import timezone

from collectors import aichi_kyoto_overseas as regional, prd_east_gov_expansion as government
from collectors import smrj_subsidy_global as subsidies, issuer_ir_pdf as issuer, sme_sources, japan_finance
from collectors.china_entry_evidence import article, counters, failure, passes_noise, read
from radar.models import KeywordCandidate, Source, SourceRun
from radar.search_provider import canonical_url, budgeted_search, provider_for

DEFINITIONS = {
    'issuer_ir_pdf': ('日本发行人官网 · IR 与公告', ()),
    'prd_media_discovery': ('珠三角中文媒体 · 原件发现', ()),
    'smrj_subsidy_global': ('日本公开补贴采择 · 海外项目', subsidies.TARGETS),
    'aichi_kyoto_overseas': ('爱知／京都 · 海外经营动态', regional.TARGETS),
    'prd_east_gov_expansion': ('长三角／珠三角 · 日资项目及环评', government.TARGETS),
    **sme_sources.SITES,
    **japan_finance.SITES,
}


def admit(source_key, row):
    url=canonical_url(row['url'])
    if row.get('unique_document_row'):
        fragment=urlsplit(row['url']).fragment
        if not re.fullmatch(r'page=[1-9]\d*&radar-row=\d{13}',fragment):
            raise ValueError('invalid_document_row')
        if KeywordCandidate.objects.filter(provider=source_key, result__source_sha256=row['document']['sha256'],
                                           canonical_url__endswith='#'+fragment).exists():
            return None
        url += '#' + fragment
    if not url or len(url)>2000 or not row['title'] or not row['text']:
        return None
    doc=row['document']
    item,created=KeywordCandidate.objects.get_or_create(canonical_url=url, defaults={
        'title':row['title'][:600],'snippet':row['text'][:4000],'text':row['text'],
        'provider':source_key,'query_id':'china-entry:'+source_key,
        'published_at':row.get('published',''),'raw_path':doc['raw_path'],
        'status':'ready_for_review' if passes_noise(row['title'],row['text']) else 'rule_rejected',
        'result':{
            'source_sha256':doc['sha256'],'source_url':doc['url'],'content_mode':'full_text',
            'historical_case':bool(row.get('historical_case')),
            'source_scope':row.get('source_scope',''),'source_pre_screen':True,
            **({'publisher_verified':True,'publication_channel':row.get('publication_channel','issuer_site'),
                'action_type':row.get('action_type','')} if row.get('publisher_verified') else {}),
            'china_region':row.get('region',''),'disclosure_target_period':row.get('target_period',''),
            'security_code':row.get('security_code',''),
            'company_industry':row.get('company_industry',''),
            'official_website':row.get('official_website',''),
            'official_website_status':row.get('official_website_status') or ('verified' if row.get('official_website') else 'missing'),
            **{k:row[k] for k in ('headquarters','company_scale','award_publish_date','pdf_page','finance_purpose','contract_date','china_entity','event_key','employees') if row.get(k)},
            'website_evidence':row.get('website_document',{}),
            'source_evidence_quote':row.get('evidence_quote',''),
            **({'pdf_pages':row['pages'],'pdf_page':row.get('pdf_page'),'action_type':row.get('action_type','')}
               if row.get('pages') and source_key in {'issuer_ir_pdf','prd_media_discovery','smrj_100oku'} else {}),
            'extraction':{'company_name':row.get('company','')} if row.get('company') else {},
        },
    })
    return item if created else None


def search_groups(probes, groups, recorded=(), *, expansion=False):
    selected=defaultdict(list)
    if 'issuer_ir_pdf' in groups:
        selected['issuer_ir_pdf']=issuer.EXPANSION_QUERIES if expansion else tuple(('',q) for q in issuer.QUERIES)
    if 'prd_media_discovery' in groups:
        selected['prd_media_discovery']=tuple(('',q) for q in issuer.MEDIA_QUERIES)
    search_rows=[]
    found=defaultdict(list)
    for group,queries in selected.items():
        try:
            provider=provider_for('tavily','ja-JP' if group=='issuer_ir_pdf' else 'zh-CN')
        except ValueError:
            search_rows.extend({'group':group,'pack':pack,'query':query,'status':'not_run','reason':'tavily_key_unavailable'} for pack,query in queries)
            continue
        if group=='issuer_ir_pdf':
            provider.days=365
        for pack,query in queries:
            row={'group':group,'pack':pack,'query':query}
            if query in recorded:
                continue
            try:
                from radar.ai_config import config
                results=budgeted_search(provider,query,group,config().search_daily_limit)
            except (ValueError,OSError) as exc:
                search_rows.append({**row,'status':'failed','reason':str(exc)[:100]})
                return found,search_rows
            except Exception as exc:
                search_rows.append({**row,'status':'failed','reason':type(exc).__name__})
                return found,search_rows
            credits=provider.last_stats.get('credits')
            search_rows.append({**row,'status':'succeeded','count':len(results),'credits':credits,'urls':[r.url for r in results], 'hits':[{'url':r.url,'title':r.title,'kind_hint':issuer.discovery_kind(r.url)} for r in results]})
            found[group].extend((r.url,r.title) for r in results)
            if type(credits) is not int or sum(r.get('credits',0) or 0 for r in search_rows) >= (12 if expansion else 20):
                return found,search_rows
    return found,search_rows


def resolve_issuer_hits(previous, max_calls=2):
    """Recover a primary disclosure by the exact company/action reported in an automatic hit."""
    from radar.ai_config import config
    from radar.models import SearchCall

    if len(previous)!=len(issuer.EXPANSION_QUERIES):
        raise ValueError('issuer resolution requires the four completed automatic searches')
    queries={q for _,q in issuer.EXPANSION_QUERIES}
    if {row['query'] for row in previous}!=queries:
        raise ValueError('issuer resolution search provenance mismatch')
    provider=provider_for('tavily','ja-JP')
    provider.days=365
    followups={}
    for row in previous:
        ledger=SearchCall.objects.filter(query=row['query'],status='succeeded').order_by('-pk').first()
        if ledger is None or set(row.get('urls',[]))!=set(ledger.urls):
            continue
        for hit in row['hits']:
            domain=urlsplit(hit['url']).hostname
            if domain not in {'www.nikkei.com','response.jp'}:
                continue
            match=re.match(r'^([一-龯ぁ-んァ-ヺA-Za-z0-9・ー]{3,30})[、，､].*(子会社設立|現地法人.{0,6}移転|新工場)',hit['title'])
            if match:
                query=f'{match[1]} 中国 {match[2]} 2026 ニュースリリース -site:nikkei.com -site:response.jp'
                followups.setdefault(query,(row['pack'],hit['url']))
    hits=[]
    data=[]
    used=sum(row.get('credits') or 0 for row in previous)
    for query,(pack,url) in followups.items():
        if len(data)>=max_calls or used>=12:
            break
        if SearchCall.objects.filter(query=query,requested_at__date=timezone.localdate()).exists():
            continue
        row={'pack':pack+'-original','derived_from':url,'query':query}
        try:
            result=budgeted_search(provider,query,'issuer_original',config().search_daily_limit)
        except Exception as exc:
            data.append({**row,'status':'failed','reason':type(exc).__name__})
            break
        credits=provider.last_stats.get('credits')
        data.append({**row,'status':'succeeded','count':len(result),'credits':credits,
                     'urls':[r.url for r in result],'hits':[{'url':r.url,'title':r.title,'kind_hint':issuer.discovery_kind(r.url)} for r in result]})
        hits.extend((r.url,r.title) for r in result)
        if type(credits) is not int:
            break
        used+=credits
    return hits,data



def recover_tdnet(previous):
    """Search independently published TDnet copies of robots-blocked discovered originals."""
    from radar.ai_config import config
    from radar.models import SearchCall

    blocked_urls={e['url'] for source in previous.get('sources',[]) for e in source.get('errors',[])
                  if e['status']=='blocked_by_robots'}
    entries=[]
    rows=[]
    provider=provider_for('tavily','ja-JP')
    provider.days=365
    for row in previous.get('search',[]):
        ledger=SearchCall.objects.filter(query=row['query'],status='succeeded').order_by('-pk').first()
        if ledger is None or not set(row.get('urls',[]))&blocked_urls&set(ledger.urls):
            continue
        prefix=row['query'].split(' -site:',1)[0]
        query='site:www.nikkei.com/markets/ir/irftp/data/tdnr '+prefix
        if SearchCall.objects.filter(query=query,requested_at__date=timezone.localdate()).exists():
            continue
        result=budgeted_search(provider,query,'issuer_tdnet_relay',config().search_daily_limit)
        rows.append({'pack':row['pack']+'-tdnet','query':query,'status':'succeeded','count':len(result),
                     'credits':provider.last_stats.get('credits'),'urls':[r.url for r in result],
                     'hits':[{'url':r.url,'title':r.title,'kind_hint':issuer.discovery_kind(r.url)} for r in result]})
        entries=[(r.url,r.title) for r in result if issuer.nikkei_tdnet(r.url)]
        break
    return entries,rows



def collect_group(key, probes=None, extra_urls=(), max_details=5, *, expansion=False, smrj_batch=False, listed_names=(), record_run=True):
    name,urls=DEFINITIONS[key]
    registered_key='jbic_sme_china' if key=='jbic_sme' else key
    source,_=Source.objects.get_or_create(key=registered_key, defaults={'name':name,'kind':'early_signal','url':next(iter(urls),''),
                                      'enabled':False,'note':'独立手动采集；公开原文、限速及来源预筛。'})
    stats={'requests':[],'errors':[],'endpoints':[]}
    # The queue uses the same guarded reader for the first index read and parsing.
    # The CLI can still constrain a run to its externally verified probe report.
    relevant=[p for p in (probes or ()) if p['group']==key]
    available={p['url'] for p in relevant if p['robots']=='allow' and p['http_status']==200}
    targets=urls if probes is None else tuple(u for u in urls if u in available)
    for p in relevant:
        if p['url'] not in available:
            failure(stats,p['url'],ValueError(p.get('error') or 'probe_failed'))
    checkpoint=None
    if key in sme_sources.SITES:
        if probes is not None and not relevant:
            failure(stats,urls[0],ValueError('entry_probe_missing'))
        rows,checkpoint=sme_sources.collect(source,stats,max_details,targets,mainland_batch=smrj_batch,listed_names=listed_names)
    elif key in {'issuer_ir_pdf','prd_media_discovery'}:
        entries = extra_urls
        if key == 'prd_media_discovery':
            entries = issuer.media_targets(entries,stats,max_details)
        rows=issuer.collect(entries,stats,max_details,expansion=expansion)
    elif key in japan_finance.SITES:
        events=KeywordCandidate.objects.filter(provider=key,result__has_key='event_key').values_list('result__event_key',flat=True)
        rows=japan_finance.collect(key,stats,targets,max_details,timezone.localdate(),events)
    elif key=='smrj_subsidy_global':
        checkpoint={**(source.checkpoint or {})}
        awards=dict(checkpoint.get('award_batches',{}))
        rows=subsidies.collect(stats,max_details=max_details,targets=targets,seen=awards)
        checkpoint['award_batches']=awards
    elif key=='aichi_kyoto_overseas':
        rows=regional.collect(stats,max_details=max_details,targets=targets)
    else:
        rows=government.collect(stats,extra_urls,max_details=max_details,targets=targets)
    created=[found.pk for row in rows if (found:=admit(key,row)) is not None]
    status='partial' if stats['errors'] and stats['requests'] else 'failed' if stats['errors'] else 'success'
    if record_run:
        SourceRun.objects.create(source=source,status=status,discovered_count=len(rows),new_count=len(created),
                                 finished_at=timezone.now(),stats=stats)
        source.status=('policy_blocked' if key=='jbic_sme' and any(e['status']=='consultation_required' for e in stats['errors'])
                       else 'partial' if status=='partial' else 'validated' if status=='success' else 'probe_failed')
        source.validation={'status':source.status,'last_retained':len(rows),'last_new':len(created)}
        if not expansion:
            if checkpoint is not None:
                source.checkpoint=checkpoint
            fields=['status','validation','checkpoint']
            if rows or status=='success':
                source.last_success_at=timezone.now()
                fields.append('last_success_at')
            source.save(update_fields=fields)
    return {'key':key,'status':status,'created':created,'retained':len(rows),'checkpoint':checkpoint,**stats}



def run_mimo(ids, limit):
    from radar.ai_config import config, secret
    from radar.keyword_extraction import KEYWORD_PROMPT_VERSION, extract
    from radar.management.commands.backup_data import raw_source_file
    from radar.sales_pools import intake_card
    import os
    cfg=config()
    if (os.environ.get('LEAD_MODEL_API_KEY') or os.environ.get('MIMO_API_KEY')) and not cfg.key_ref:
        cfg.key_source='environment'
    if not cfg.model and os.environ.get('LEAD_MODEL_NAME'):
        cfg.model=os.environ['LEAD_MODEL_NAME']
    if not cfg.base_url and os.environ.get('LEAD_MODEL_BASE_URL'):
        cfg.base_url=os.environ['LEAD_MODEL_BASE_URL']
    if not limit:return {'status':'not_requested','ids':[]}
    if not secret(cfg) or not cfg.model.startswith('mimo-') or not cfg.base_url:
        return {'status':'mimo_credential_unavailable','ids':[]}
    calls=[]
    for item in KeywordCandidate.objects.filter(pk__in=ids,status='ready_for_review').order_by('pk'):
        if len(calls)>=limit:break
        if not item.result.get('source_pre_screen') or not passes_noise(item.title,item.text) or intake_card(item)['grade']!='B':
            continue
        if hashlib.sha256(raw_source_file(item.raw_path).read_bytes()).hexdigest()!=item.result.get('source_sha256'):
            continue
        item.status='model_started'
        item.save(update_fields=['status'])
        try:
            value=extract(item,cfg,min(30,cfg.daily_limit))
            item.result.update(extraction=value,prompt_version=KEYWORD_PROMPT_VERSION)
            from radar.company_enrichment import classify
            evidence = value.get('industry_evidence','')
            if evidence and evidence in item.text and classify(evidence)==value.get('company_industry') and not item.result.get('company_industry'):
                item.result['company_industry']=value['company_industry']
            item.status='model_needs_review'
        except Exception as exc:
            item.result['model_error']=type(exc).__name__
            item.status='model_failed'
        call=getattr(item,'_keyword_call_id',None)
        if call:
            item.result['model_call_id']=call
            calls.append(call)
        item.save(update_fields=['status','result'])
    return {'status':'complete','ids':calls}
