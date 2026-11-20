"""Two-pass manual watch. Discovery commits its pending work before saving validators."""
import hashlib
import json
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from xml.etree.ElementTree import ParseError

from django.utils import timezone
from filelock import FileLock
from lxml.etree import LxmlError

from collectors import edinet
from collectors.cli import atomic_json
from collectors.research_common import archive_original
from collectors.target_events import candidate, compact, document
from collectors.target_watch import discover, expanded_entries, store_event
from collectors.title_prescore import title_prescore
from radar.public_fetch import check_budget, fetch, request_budget
from radar.search_provider import canonical_url

ERRORS = (ValueError, OSError, ParseError, LxmlError)


def fingerprint(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def read_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def run(companies, output, *, limit, details=3, references=False, configured=None,
        mode='incremental', edinet_days=7, edinet_date=None, resume=False):
    output=Path(output).resolve()
    output.mkdir(parents=True,exist_ok=True)
    event_file=output/'target-events.json'
    if any(output.iterdir()) and not event_file.is_file() and set(p.name for p in output.iterdir())!={'.target-watch.lock'}:
        raise ValueError('请指定空目录或已有的本试点输出目录')
    with FileLock(str(output/'.target-watch.lock'),timeout=0):
        archive=read_json(event_file,{'format':'target-watch-v1','events':[]})
        if archive.get('format')!='target-watch-v1':
            raise ValueError('输出目录格式不符')
        atomic_json(event_file,archive)
        state_file=output/'watch_state.json'
        state=read_json(state_file,{'format':2,'sources':{}})
        if state.get('format')!=2:
            raise ValueError('水位线格式不符')
        today=timezone.localdate()
        signature=fingerprint(json.dumps([companies,mode,references,edinet_days,str(edinet_date),today.isoformat()],sort_keys=True))
        if not resume or state.get('checkpoint',{}).get('signature')!=signature:
            state['checkpoint']={'signature':signature,'indexes':[]}
        checkpoint=state['checkpoint']['indexes']
        now=timezone.now()
        report={'id':now.strftime('%Y%m%dT%H%M%S%fZ')+'-'+uuid4().hex[:6],'started_at':now.isoformat(),
                'mode':mode,'is_baseline':mode=='baseline','entries':[],'reads':[],'pointers':[],'edinet_days':[],'http_requests':[]}
        stats=Counter(dict.fromkeys(('requests','attempted_sources','successful_reads','documents','candidate_events',
                  'new_events','duplicates','failures','unknown','skipped_titles','pointer_only','not_modified',
                  'manual_watch','skipped_missing_api_key','budget_skips','deferred','seen_or_old','suspected'),0))
        stats.update(configured_companies=configured or len(companies),selected_companies=len(companies))
        periods=Counter()
        sources={}
        statutory=[]
        queue=[]

        def save():
            atomic_json(state_file,state)

        def queue_titles(rows,c,e,s,row):
            previous_date=s.get('latest_date','')
            for item in rows:
                url=item['url']
                key=fingerprint(url)
                title=item['title']
                stamp=item['published_date']
                version=fingerprint(title+'|'+stamp)
                if mode=='incremental' and (s['seen'].get(key)==version or
                        (stamp and stamp<previous_date and key not in s['pending'] and key not in s['seen'])):
                    stats['seen_or_old']+=1
                    continue
                score,rules=title_prescore(title,c['china_entities'])
                s['latest_date']=max(stamp,s.get('latest_date',''))
                if score<25:
                    with (output/'prescore_skipped.jsonl').open('a',encoding='utf-8') as log:
                        log.write(json.dumps({'company_id':c['id'],'entry_id':e['entry_id'],'title':title,'url':url,
                                  'score':score,'matched_rules':rules,'timestamp':now.isoformat()},ensure_ascii=False)+'\n')
                    stats['skipped_titles']+=1
                elif e['compliance_mode']=='metadata_pointer':
                    if score>=45:
                        report['pointers'].append({'company_id':c['id'],'title':title,'published_date':stamp or None,
                            'source_url':url,'prescore':score,'matched_terms':[t for terms in rules.values() for t in terms],
                            'compliance_mode':'metadata_pointer','archive_status':'pointer_only'})
                        stats['pointer_only']+=1
                else:
                    s['pending'][key]=dict(item,version=version,score=score)
                    continue
                s['pending'].pop(key,None)
                s['seen'][key]=version
            row['titles']=len(rows)

        with request_budget(limit,min(900,limit*22+5),journal=report['http_requests']) as budget:
            for c in companies:
                for e in expanded_entries(c['entries'],today):
                    level=e['compliance_mode']
                    row={'company_id':c['id'],'entry_id':e['entry_id'],'url':e['url'],'compliance_mode':level}
                    report['entries'].append(row)
                    if level=='manual_watch':
                        row['status']='manual_watch'
                        stats['manual_watch']+=1
                        continue
                    # A scope/terms change cannot resurrect work authorized under an older policy.
                    scope=c['id']+'/'+e['entry_id']+'/'+fingerprint(level+'|'+e['url'])
                    s=state['sources'].setdefault(scope,{'seen':{},'pending':{}})
                    if e['role']=='historical_reference' and not references:
                        row['status']='reference_not_requested'
                        continue
                    sources[scope]=(c,e,s,row)
                    if level=='statutory_api':
                        if e.get('source_type')!=edinet.SOURCE_TYPE or e['url']!=edinet.BASE+'.json':
                            row.update(status='failed',reason='invalid_edinet_source')
                            stats['failures']+=1
                            continue
                        report['source_attribution']=row['source_attribution']=edinet.ATTRIBUTION
                        statutory.append(scope)
                        continue
                    if scope in checkpoint:
                        row['status']='resumed'
                        continue
                    try:
                        check_budget()
                        if e['kind'] in ('rss','list'):
                            stats['attempted_sources']+=1
                            conditions=s.get('conditions') if mode=='incremental' else None
                            response=fetch(e['url'],max_bytes=5_000_000,timeout=15,same_origin=True,
                                           conditions=conditions,no_redirect=True)
                            if response.get('status')==304:
                                stats['not_modified']+=1
                                row['status']='not_modified'
                            else:
                                rows=discover(response,e['kind'],e)
                                if level=='full_archive':
                                    row['document']=archive_original(response,output)
                                queue_titles(rows,c,e,s,row)
                                stats['successful_reads']+=1
                                row['status']='index'
                                s['conditions']={k:response[v] for k,v in (('If-None-Match','etag'),('If-Modified-Since','last_modified')) if response.get(v)}
                        else:
                            queue_titles([{'url':e['url'],'title':e.get('title',''),'published_date':''}],c,e,s,row)
                            row['status']='reference'
                        checkpoint.append(scope)
                        # A pointer must be durable before its URL is marked seen, even if interrupted.
                        if report['pointers']:
                            atomic_json(output/'runs'/(report['id']+'.json'),report)
                        save()
                    except ERRORS as exc:
                        row.update(status='failed',reason=str(exc)[:180])
                        stats['budget_skips' if str(exc)=='total_request_limit' else 'failures']+=1

            blocked_scopes=set()
            key=edinet.api_key() if statutory else ''
            if statutory and not key:
                for scope in statutory:
                    sources[scope][3]['status']='skipped_missing_api_key'
                stats['skipped_missing_api_key']=len(statutory)
            elif statutory:
                codes={code for scope in statutory for code in (sources[scope][0]['edinet_code'],sources[scope][0]['parent_edinet_code']) if code}
                stats['attempted_sources']+=len(statutory)
                for scope in statutory:
                    sources[scope][3]['status']='statutory_index'
                days=(edinet_date,) if edinet_date else (today-timedelta(days=d) for d in range(edinet_days))
                for day in days:
                    label='edinet:'+day.isoformat()
                    if label in checkpoint:
                        continue
                    daylog={'date':day.isoformat(),'source_attribution':edinet.ATTRIBUTION}
                    report['edinet_days'].append(daylog)
                    try:
                        check_budget()
                        filings=edinet.filings(day,codes,key)
                        daylog.update(status='ok',matched=len(filings))
                        stats['successful_reads']+=1
                        for scope in statutory:
                            c,e,s,row=sources[scope]
                            for f in filings:
                                if f['edinetCode'] not in (c['edinet_code'],c['parent_edinet_code']):
                                    continue
                                url=edinet.BASE+'/'+f['docID']
                                hashed=fingerprint(url)
                                if mode=='baseline' or hashed not in s['seen']:
                                    s['pending'][hashed]={'url':url,'title':f['docDescription'],'version':f['docID'],
                                                         'score':45,'filing':f,'published_date':f['submitDateTime'][:10]}
                        checkpoint.append(label)
                        save()
                    except ERRORS as exc:
                        daylog.update(status='failed',reason=str(exc)[:150])
                        stats['failures']+=1
                        if str(exc) in {'edinet_http_401','edinet_http_403','edinet_http_429','total_request_limit','blocked_by_robots'} or 'robots' in str(exc):
                            blocked_scopes.update(statutory)
                            for target in statutory:
                                sources[target][3].update(status='stopped',reason=str(exc)[:150])
                            break

            for scope,(c,e,s,row) in sources.items():
                if row['status'] in ('failed','skipped_missing_api_key') or e['compliance_mode'] not in ('full_archive','statutory_api'):
                    continue
                queue.extend((item['score'],scope,hashed,item) for hashed,item in s['pending'].items())
            used=Counter()
            completed={}
            outstanding_a=False
            for score,scope,hashed,item in sorted(queue,key=lambda q:-q[0]):
                c,e,s,row=sources[scope]
                identity=(c['id'],item['url'])
                if identity in completed:
                    s['seen'][hashed]=item['version']
                    del s['pending'][hashed]
                    save()
                    continue
                if scope in blocked_scopes or used[c['id']]>=details or (score<45 and outstanding_a) or budget['used']>=limit:
                    stats['deferred']+=1
                    outstanding_a |= score>=45
                    continue
                read={'company_id':c['id'],'url':item['url'],'prescore':score,'title':item['title']}
                report['reads'].append(read)
                used[c['id']]+=1
                try:
                    if e['compliance_mode']=='statutory_api':
                        read['source_attribution']=edinet.ATTRIBUTION
                        f=item['filing']
                        blocks,pub,doc=edinet.report_document(f,key,output)
                        read['section_blocks']=len(blocks)
                        if f['edinetCode']!=c['edinet_code']:
                            # Group disclosures must identify this operating company in the same passage.
                            names=[c['name'],*c['china_entities']]
                            blocks=[(loc,sentence) for loc,t in blocks for sentence in re.split(r'[。！？；]',t)
                                    if any(compact(n) in compact(sentence) for n in names)]
                        else:
                            blocks=[('EDINET edinetCode',c['name']),*blocks]
                        title=item['title']
                    elif e['compliance_mode']=='full_archive' and urlsplit(item['url'])[:2]==urlsplit(e['url'])[:2]:
                        response=fetch(item['url'],max_bytes=5_000_000,timeout=15,same_origin=True)
                        doc=archive_original(response,output)
                        title,blocks,pub,read['partial']=document(response,doc,output,'pdf' if urlsplit(item['url']).path.lower().endswith('.pdf') else 'detail')
                    else:
                        raise ValueError('scope_changed')
                    if hashlib.sha256((output/doc['raw_path']).read_bytes()).hexdigest()!=doc['sha256']:
                        raise ValueError('archive_hash_mismatch')
                    doc.update(fetched_at=timezone.now().isoformat(),is_baseline=mode=='baseline')
                    read['document']=doc
                    stats['documents']+=1
                    stats['successful_reads']+=1
                    fact,reason=candidate(c,title,blocks,pub,today,entity_site=e['compliance_mode']=='statutory_api')
                    if fact:
                        status,event_id=store_event(archive['events'],c['id'],fact,title or item['title'],doc)
                        read.update(status=status,event_id=event_id)
                        periods[fact['time_group']]+=1
                        stats['candidate_events']+=1
                        stats['duplicates' if status=='duplicate' else 'new_events']+=1
                        stats['suspected']+=int(status=='suspected')
                        read['unknown_fields']=[k for k in ('china_entity','publication_date') if not fact[k]]
                        field={'计划':'planned_date','待核':'stage'}.get(fact['stage'],'actual_date')
                        if field=='stage' or not fact[field]:
                            read['unknown_fields'].append(field)
                        stats['unknown']+=bool(read['unknown_fields'])
                        atomic_json(event_file,archive)
                    else:
                        read.update(status='no_match',reason=reason)
                    completed[identity]=True
                    s['seen'][hashed]=item['version']
                    del s['pending'][hashed]
                    save()
                except ERRORS as exc:
                    reason=str(exc)[:180]
                    read.update(status='failed',reason=reason)
                    if reason.endswith(('http_401','http_403','http_429','access_restricted')) or 'robots' in reason:
                        blocked_scopes.update(statutory if e['compliance_mode']=='statutory_api' else [scope])
                        if e['compliance_mode']=='statutory_api':
                            for target in statutory:
                                sources[target][3].update(status='stopped',reason=reason)
                    stats['failures']+=1
            stats['requests']=budget['used']
        stats['pending']=sum(len(s['pending']) for _,_,s,_ in sources.values())
        report.update(stats=dict(stats),time_groups=dict(periods))
        atomic_json(output/'runs'/(report['id']+'.json'),report)
        if not (stats['deferred'] or stats['budget_skips'] or stats['failures']):
            state.pop('checkpoint',None)
        save()
        return report
