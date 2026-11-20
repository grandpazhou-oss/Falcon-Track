from django.db.models import Q
from django.db.models import Case, When, IntegerField, Value
from django.utils import timezone
from .models import Lead
import re
from django.utils.dateparse import parse_datetime

LABELS={'japan':'日企主动拓客池（信号驱动）','procurement':'采购参与','registration':'供应商注册 / 招募','expansion':'日企扩产 / 特种物流信号','recruitment':'日企招聘信号','research':'企业研究','subject':'主体待补全','archive':'历史 / 低相关'}

LINK_LABELS={'confirmed':'日资背景已人工核实','directory':'日企名录关联 · 背景待核实','explicit_claim':'项目原文声称日资 · 待核实',
             'joint_venture':'合资品牌关联 · 背景待核实','seed':'日企字号关联 · 背景待核实','language_hint':'日语岗位 · 企业背景待确认','platform_claim':'平台声称日企 · 主体待确认','none':''}

def notice_period_expired(metadata):
    value=(metadata or {}).get('notice_period_end')
    try:end=parse_datetime(value) if isinstance(value,str) and value else None
    except ValueError:end=None
    return bool(end and timezone.is_aware(end) and end<=timezone.now())

def japan_signal(lead,link=None):
    """Find evidence-bearing sales signals without declaring inferred ownership or demand."""
    from .entities import japanese_link_for_lead
    link=link or japanese_link_for_lead(lead)
    if link['level']=='none' or lead.is_test or lead.duplicate_of_id:return None
    text=f'{lead.title}\n{lead.text}'
    meta=lead.metadata or {}
    if notice_period_expired(meta):return None
    is_job=(meta.get('signal_type') in {'job','recruitment','job_posting'} or
            meta.get('record_type') in {'job','recruitment','job_posting'} or
            lead.source.kind=='recruitment' or bool(re.search(r'招聘|岗位招聘|职位招聘|求人|採用',lead.title)))
    role_words=r'物流|货运|货代|空运|海运|重大件|滚装|关务|通関|仓库|仓储|倉庫|倉儲|SCM|供应链|サプライチェーン|进出口|輸出入|危险品|危化|危険物|冷链|冷蔵'
    triggers=r'扩张|扩大|扩产|新增产能|新设|新建|新仓|筹建|搬迁|迁建|供应商切换|切换.{0,8}3PL|危化|危险品|危险货物|冷链|恒温|事業拡大|業務拡大|新倉庫|倉庫.{0,3}(?:新設|増設|移転)|委託先.{0,3}(?:切替|変更)|危険物|冷蔵|定温'
    if is_job:
        from datetime import timedelta
        if lead.published_date and lead.published_date < timezone.localdate() - timedelta(days=180):return None
        if not re.search(role_words,text,re.I):return None
        if link['role']=='logistics_peer' and re.search(r'仓储|仓库|倉庫|危化|危险品|危険物|危险货物|冷链|冷蔵|恒温|定温|特种运输|特殊輸送|重大件|大件运输|滚装',text):
            return {**link,'signal':'日系物流同行招聘 · 分包切入待核实'}
        if re.search(triggers,text,re.I):
            return {**link,'signal':'物流岗位与业务变化信号 · 需求待核实'}
        return {**link,'signal':'物流岗位信号 · 时效与需求待核实','priority':5}
    if lead.event_type in {'tender','rfq','supplier_recruitment','change'}:
        if lead.processing_status in {'closed','irrelevant'}:return None
        if lead.deadline_at and lead.deadline_at<=timezone.now():return None
        if not lead.deadline_at and lead.deadline and lead.deadline<timezone.localdate():return None
        return {**link,'signal':'更正公告 · 结合原公告核对' if lead.event_type=='change' else '采购 / 供应商招募 · 条件待核实'}
    if lead.event_type=='early_signal' or lead.source.kind=='early_signal':
        if lead.processing_status=='irrelevant':return None
        if re.search(r'宿舍|住宅|学校|教学|办公楼|医院',lead.title) and not re.search(r'物流|仓储|仓库|工厂|生产线|危化|危险品',lead.title):return None
        if re.search(r'新建|新设|扩产|扩建|搬迁|迁建|新增产能|生产线|产线|危化|危险化学品|危险品|新設|増設|増産|移転|危険物',text):
            return {**link,'signal':'项目建设变化信号 · 不等于物流采购'}
    return None

def business_leads():
    return Lead.objects.filter(is_test=False,duplicate_of__isnull=True)

def select_queue(qs,key):
    if key in {'japan','expansion','recruitment'}:
        if key=='expansion':qs=qs.filter(source__kind='early_signal')
        elif key=='recruitment':qs=qs.filter(Q(source__kind='recruitment')|Q(metadata__signal_type='job'))
        # Scores are computed from source-backed company data at read time; no
        # ownership or review fields are changed by visiting the workspace.
        matched=[]
        for lead in qs.select_related('company','source').iterator():
            item=japan_signal(lead)
            if item:matched.append((lead.pk,10*{'confirmed':0,'directory':1,'explicit_claim':2,'joint_venture':2,'seed':3,'platform_claim':4,'language_hint':5}.get(item['level'],6)+item.get('priority',0)))
        if not matched:return qs.none()
        ranks=Case(*[When(pk=pk,then=Value(rank)) for pk,rank in matched],output_field=IntegerField())
        return qs.filter(pk__in=[pk for pk,_ in matched]).annotate(japan_priority=ranks).order_by('japan_priority','-first_seen_at','-id')
    expired=Q(deadline_at__lte=timezone.now())|Q(deadline_at__isnull=True,deadline__lt=timezone.localdate())
    # The notice period is kept separate from a response deadline. Past notices
    # are placed in history, while the detail explains the distinction.
    past_notice_ids=[pk for pk,meta in qs.filter(source__key='dfwlg_procurement').values_list('pk','metadata') if notice_period_expired(meta)]
    from datetime import timedelta
    archive=Q(processing_status__in=['closed','irrelevant'])|expired|Q(pk__in=past_notice_ids)|((Q(source__kind='recruitment')|Q(metadata__signal_type='job'))&Q(published_date__lt=timezone.localdate()-timedelta(days=180)))
    if key=='archive':return qs.filter(archive)
    qs=qs.exclude(archive)
    if key=='procurement':return qs.filter(processing_status='actionable').exclude(event_type='supplier_recruitment')
    if key=='registration':return qs.filter(event_type='supplier_recruitment')
    if key=='research':return qs.filter(processing_status='early_signal')
    if key=='subject':return qs.filter(company__isnull=True).exclude(event_type='early_signal')
    return qs
