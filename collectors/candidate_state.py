"""Small checkpoint-backed discovery ledger and cooperative run limits."""
from datetime import timedelta
import copy
import time
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from radar.public_fetch import check_budget, FetchError


class LimitReached(ValueError):
    pass


def bounded(value, default, low, high):
    try:return max(low,min(high,int(value)))
    except (ValueError,TypeError):return default


class Limits:
    def __init__(self,options):
        self.accepted=bounded(options.get('max_accepted_items',options.get('max_items')),8,1,50)
        self.details=bounded(options.get('max_detail_requests'),30,1,200)
        self.total=bounded(options.get('max_total_requests'),80,1,500)
        self.seconds=bounded(options.get('max_elapsed_seconds'),180,1,600)
        self.started=time.monotonic();self.calls=0;self.detail_calls=0

    def read(self,reader,url,*,detail=False,**kwargs):
        if time.monotonic()-self.started>=self.seconds:raise LimitReached('elapsed_limit')
        if self.calls>=self.total:raise LimitReached('total_request_limit')
        if detail and self.detail_calls>=self.details:raise LimitReached('detail_request_limit')
        try:check_budget()
        except FetchError as exc:raise LimitReached(str(exc)) from exc
        self.calls+=1
        if detail:self.detail_calls+=1
        return reader(url,**kwargs)


class CandidateLedger:
    def __init__(self,checkpoint,key):
        self.key=key
        self.state=copy.deepcopy((checkpoint or {}).get(key) or {})
        self.entries={str(k):v for k,v in self.state.get('entries',{}).items()}

    def add(self,identity,url,query,evidence,**facts):
        now=timezone.now().isoformat();identity=str(identity)
        item=self.entries.get(identity)
        if not item:
            item={'id':identity,'url':url,'discovered_at':now,'status':'pending','attempts':0,'queries':[]}
            self.entries[identity]=item
        item.update(url=url,last_seen_at=now,listing_evidence=evidence,**facts)
        if query and query not in item['queries']:item['queries'].append(query)
        return item

    def pending(self):
        now=timezone.now();result=[]
        for item in self.entries.values():
            due=parse_datetime(item.get('retry_at') or '')
            if item.get('status') in {'pending','retry'} and (not due or due<=now):result.append(item)
            elif item.get('status') in {'ingested','excluded'}:
                due=parse_datetime(item.get('recheck_at') or '')
                if due and due<=now:result.append(item)
        return sorted(result,key=lambda x:(x.get('discovered_at',''),x['id']))

    def done(self,item,status):
        item.update(status=status,processed_at=timezone.now().isoformat(),retry_at='',last_error='',
                    recheck_at=(timezone.now()+timedelta(days=7)).isoformat())

    def failed(self,item,exc):
        item.update(status='retry',attempts=item.get('attempts',0)+1,last_error=type(exc).__name__+': '+str(exc)[:160],
                    retry_at=(timezone.now()+timedelta(minutes=5)).isoformat())

    def save(self,**extra):
        return {self.key:{**self.state,**extra,'entries':self.entries}}
