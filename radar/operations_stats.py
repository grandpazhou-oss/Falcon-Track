"""Local settings dashboard. Counts come from the ledgers and the live A/B worklist."""
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
import os

from django.conf import settings
from django.db.models import Count, Q, Sum, IntegerField
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Coalesce
from django.utils import timezone

from .china_places import project_city, MAINLAND
from .labels import ERROR_LABELS
from .mimo_tiers import FLASH, PRO
from .models import KeywordCandidate, Lead, ModelCall, SalesFeedback, SearchCall, Source, SourceRun
from .sales_ui import SOURCE_LABELS, sales_data

RANGES={'today':'今日','7':'近 7 天','30':'近 30 天','all':'累计'}
TOKEN_KEYS=('prompt_tokens','cached_tokens','completion_tokens','total_tokens')
REGIONS={'east':'华东','south':'珠三角','other':'中国其他地区','unknown':'未明确'}


def rate(n,d):
    return round(100*n/d,1) if d else None


def token_rows(scope,today):
    calls=ModelCall.objects.filter(model__in=(FLASH,PRO))
    days={'today':1,'7':7,'30':30}.get(scope)
    if days:
        calls=calls.filter(requested_at__date__gte=today-timedelta(days=days-1),requested_at__date__lte=today)
    grouped={r['model']:r for r in calls.values('model').annotate(count=Count('id'),
              **{k:Coalesce(Sum(Cast(KeyTextTransform(k,'usage'),IntegerField())),0) for k in TOKEN_KEYS})}
    rows=[]
    for name, model in (('Flash',FLASH),('Pro',PRO)):
        row=grouped.get(model,{'count':0,**dict.fromkeys(TOKEN_KEYS,0)})
        row={k:v for k,v in row.items() if k!='model'}
        rows.append({**row,'name':name,'cache_rate':rate(row['cached_tokens'],row['prompt_tokens'])})
    return rows


def search_stats(cfg,today):
    q=SearchCall.objects.all()
    start=today.replace(day=1)
    this_month=Q(requested_at__date__gte=start,requested_at__date__lte=today)
    values=q.aggregate(month=Count('pk',filter=this_month),total=Count('pk'),
                       month_success=Count('pk',filter=this_month & Q(status='succeeded')),
                       success=Count('pk',filter=Q(status='succeeded')))
    failure=q.filter(status='failed').order_by('-requested_at','-id').first()
    return {**values,'enabled':cfg.search_enabled,'monthly_rate':rate(values['month_success'],values['month']),
            'overall_rate':rate(values['success'],values['total']),
            'daily_limit':cfg.search_daily_limit,'today':q.filter(requested_at__date=today).count(),
            'last_failure':ERROR_LABELS.get(failure.error_code,'检索失败，暂无原因记录') if failure else '',
            'last_failure_at':failure.requested_at if failure else None}


def source_rows(cards):
    sources={s.key:s.name for s in Source.objects.all()}
    runs={r['source__key']:r['seen'] for r in SourceRun.objects.values('source__key').annotate(seen=Sum('discovered_count'))}
    candidates={r['provider']:r['count'] for r in KeywordCandidate.objects.values('provider').annotate(count=Count('pk'))}
    legacy=defaultdict(set)
    for key,url in Lead.objects.filter(review_status__in=['accepted','contacted']).values_list('source__key','url'):
        legacy[key].add(url)
    grouped=defaultdict(list)
    for card in cards:
        grouped[card.get('provider_key') or card.get('source_kind') or 'other'].append(card)
    recent=defaultdict(list)
    status_error={'failed','blocked'}
    for key,status,new,updated,error,reason in SourceRun.objects.filter(finished_at__gte=timezone.now()-timedelta(days=30)).exclude(
            status__in=['queued','running','cancelled']).order_by('-finished_at','-id').values_list(
                'source__key','status','new_count','updated_count','error','stats__partial_reason'):
        if len(recent[key])<3:
            recent[key].append((status,new,updated,error or reason=='errors'))
    rows=[]
    for key in sets_in_order(sources,runs,candidates,grouped):
        own=grouped[key]
        # Use cumulative discoveries where tracked. Historical/manual imports may only have a candidate count.
        seen=max(runs.get(key,0) or 0,candidates.get(key,0),len(own))
        useful=len(legacy[key] | {c.get('source_url') or c.get('detail_url') or str(i)
                    for i,c in enumerate(own) if c['feedback_status']=='accepted'})
        history=recent[key]
        alerts=[]
        if history and (history[0][0] in status_error or history[0][3]):
            alerts.append('最近采集出错')
        if len(history)>=3 and all(not row[1] for row in history):
            alerts.append('连续 3 次无新增')
        rows.append({'name':SOURCE_LABELS.get(key) if key=='jetro_biznews' else sources.get(key) or SOURCE_LABELS.get(key) or (own[0]['ui_source'] if own else key),
                     'seen':seen,'ab':len(own),'useful':useful,'effective':rate(len(own),seen),'alerts':alerts})
    return sorted(rows,key=lambda r:(-(r['effective'] if r['effective'] is not None else -1),-r['ab'],r['name']))


def sets_in_order(*maps):
    return dict.fromkeys(key for mapping in maps for key in mapping)


def distribution(cards):
    regions,industries,statuses,reasons=(Counter() for _ in range(4))
    for c in cards:
        city=c.get('china_location','')
        region=c['ui_region']
        regions[region if region in {'east','south'} else 'other' if project_city(city) or MAINLAND.search(city) else 'unknown']+=1
        industries[c.get('company_industry') or '未明确']+=1
        statuses[c['feedback_status']]+=1
        if c['feedback_status']=='rejected' and c['feedback_reason']:
            reasons[c['feedback_reason']]+=1
    n=len(cards)
    choices=dict(SalesFeedback.Reason.choices)
    return {'regions':[{'name':v,'count':regions[k],'share':rate(regions[k],n)} for k,v in REGIONS.items()],
            'industries':[{'name':k,'count':v,'share':rate(v,n)} for k,v in industries.most_common()],
            'feedback':[{'name':v,'count':statuses[k]} for k,v in {
                'new':'待处理','accepted':'跟进中','hold':'暂缓观察','rejected':'已忽略'}.items()],
            'reasons':[{'name':choices.get(k,'其他'),'count':v} for k,v in reasons.most_common(3)]}


def storage_stats():
    config=settings.DATABASES['default']
    paths=[]
    if config['ENGINE']=='django.db.backends.sqlite3':
        name=str(config['NAME'])
        paths=[Path(name+suffix) for suffix in ('','-wal','-shm')]
    def summarize(items):
        size=count=0
        for path in items:
            try:
                if path.is_file() and not path.is_symlink():
                    size+=path.stat().st_size; count+=1
            except OSError:
                continue
        return {'count':count,'bytes':size}
    root=settings.DATA_DIR/'raw'
    originals=(Path(folder)/name for folder,dirs,files in os.walk(root)
             if Path(folder).name!='probe_cache'
             for name in files if 'probe_cache' not in Path(folder).relative_to(root).parts
             and Path(name).suffix.lower() in {'.html','.htm','.pdf'})
    return {'database':summarize(paths),'archive':summarize(originals)}


def dashboard(request,cfg):
    scope=request.GET.get('range','30')
    scope=scope if scope in RANGES else '30'
    _,groups=sales_data(request)
    cards=[c for grade in 'AB' for c in groups[grade]]
    today=timezone.localdate()
    return {'scope':scope,'ranges':RANGES,'tokens':token_rows(scope,today),'search':search_stats(cfg,today),
            'sources':source_rows(cards),'storage':storage_stats(),**distribution(cards)}
