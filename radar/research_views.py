import re
import uuid

from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404,redirect,render
from django.views.decorators.http import require_GET,require_http_methods,require_POST
from .models import ResearchEvent,Source,SalesFeedback
from .sales_feedback import detail_feedback
from .views import _base
from .development import development
from .sales_pools import research_card
from .company_enrichment import fields as company_fields

EVENTS={'historical_only':'历史经营事实','research_pending':'经营证据待核','existing_presence':'在华据点','market_research':'市场调研','exhibitor_confirmed':'主办方列名参展',
 'export_plan':'对华出口计划','market_access_registration':'市场准入登记','entity_planned':'计划设立',
 'entity_established':'已披露设立','project_signed':'项目签约','construction_started':'项目开工',
 'operation_started':'开始运营','expansion':'扩产','relocation':'搬迁','supplier_recruitment':'供应商招募',
 'rfq':'询价','cancelled_or_withdrawn':'取消或撤回','finance_support':'融资支持'}
WORKSTREAMS={'shanghai_market':'上海市场开拓','existing_in_china':'已在华企业','planned_china':'在华项目及计划','export_to_china':'对华供货计划'}
ROLES={'listed_exhibitor':'日本地区展商（注册地待核）','japan_parent':'日本母公司','exporter':'日本出口方','japan_company':'日本企业','china_entity':'中国关联主体',
       'china_site':'中国据点','announced_counterpart':'公告列示的商谈对象（未证实参加）','distributor':'经销代理','importer':'进口商','import_agent':'进口代理','agent':'代理','unknown':'待核实'}
RELATIONS={'owned_subsidiary':'原表自报独资','joint_venture':'原表自报合资','branch':'分支机构',
 'representative_office':'代表处','distributor':'经销代理','contract_manufacturer':'委托生产',
 'importer':'进口商','mentioned_organization':'提及的机构','unknown':'关系待核实'}
DATES={'published_at':'发布日期','event_date':'事件日期','planned':'计划日期','planned_date':'计划日期',
       'contract_date':'合同日期','china_established':'披露的在华设立年','source_updated':'来源更新日期（非企业设立日期）',
       'case_year':'案例年度（非企业动作日期）','plan_fiscal_year':'资料标注的计划年度（非执行日期）'}
FIELDS={'china_action':'案例中的对华动作','site_operator':'在华门店运营主体','employees':'来源披露人数','booth':'名录展位','fair_dates':'会期','parent_address':'日本总部地址','site_address':'在华据点地址','site_function':'据点业务',
 'relation_description':'关系原表','products':'产品','product':'产品','business_description':'业务描述',
 'website':'名录列示的网站（关联待核）','operational_roles':'自报业务角色','source_note':'原表维护日期（非设立日期）',
 'market':'市场','market_stage':'来源描述的市场阶段','announced_counterpart':'另一来源列示的商谈对象',
 'conflicting_counterpart':'来源间存在冲突的其他对象','export_amount':'出口金额','export_quantity':'出口量','business':'业务','china_location':'地区',
 'financing_amount':'融资金额（非物流预算）','parent_china_relation':'来源称述的关联'}
SIZES={'sme':'中小企业（仅按所列证据口径）','micro':'微型企业','mid_large':'中大型企业','unknown':'规模待核实'}
SCOPES={'china_mainland':'中国大陆','china_unspecified':'中国范围未细分','hong_kong':'香港','taiwan':'台湾',
        'unknown':'未注明市场','multiple_markets_unallocated':'多个市场合计，未分配中国份额','unassigned':'未分配市场'}
DATE_STATUS={'planned':'计划，实际进度待核','stated':'原文明示','actual':'原文明示实际发生','source_metadata':'来源资料日期',
             'derived_relative_today':'依据公告中的“本日”与发布日期推算，待核实',
             'derived_same_month':'来源仅写月日，年份依据临近发布日期推算，待核实',
             'year_unknown':'原文未写年份', 'source_metadata':'来源资料日期'}

@require_GET
def research_list(request):
    events=ResearchEvent.objects.filter(lead__duplicate_of__isnull=True).select_related('lead','lead__source','lead__company').prefetch_related('lead__documents').order_by('-observed_at','-pk')
    stream=request.GET.get('stream','')
    if stream == 'japan_channels':
        from collectors.japan_channels import CHANNELS
        events = events.filter(lead__source__key__in=(*CHANNELS,'kanto_verified_cases','idec_overseas_cases'))
    elif stream == 'sales_shanghai':
        events = events.filter(lead__source__key__in=[
            'sh_market_disclosures', 'fbc_ptc_manufacturers', 'osaka_ciie_roster', 'fhc_shanghai_japan'])
    elif stream == 'historical_review':
        events = events.filter(event_type__in=('historical_only','research_pending'))
    elif stream in WORKSTREAMS:events=events.filter(workstream=stream)
    page=Paginator(events,25).get_page(request.GET.get('page'))
    for event in page.object_list:
        event.development = development(event)
        event.ui_event=_event_label(event)
        event.ui_stream=WORKSTREAMS.get(event.workstream,'其他研究')
        event.ui_historical=_historical_source(event) or event.lead.source.key == 'idec_overseas_cases'
    ctx=_base('research');ctx.update(page=page,streams=WORKSTREAMS.items(),stream=stream)
    return render(request,'radar/research_list.html',ctx)

@require_GET
def research_detail(request,pk):
    event=get_object_or_404(ResearchEvent.objects.select_related('lead','lead__source'),pk=pk)
    checks=[d for d in event.lead.documents.all() if d.metadata.get('kind')=='website_check'][:8]
    for doc in checks:
        doc.ui_status={'explicit_name_match':'网页出现企业全名，仍需核对归属','identity_unverified':'未在页面找到企业全名',
                       'blocked_or_failed':'访问受阻或失败'}.get(doc.metadata.get('status'),'待核实')
    from .analysis import review_historical_for_display

    runs=[review_historical_for_display(run) for run in event.lead.analyses.filter(input_snapshot__project_analysis=True)[:5]]
    from .sales_ui import display_card
    card=research_card(event) or {}
    dossier={**company_fields(event.lead.company), **display_card(card)}
    page=re.search(r'第\s*(\d+)\s*[页頁]',event.china_evidence.get('location',''))
    ctx=_base('research');ctx.update(card=card, dossier=dossier, evidence_page=page[1] if page else '', project_runs=runs, promotion_basis=card.get('promotion_basis'), feedback_card=detail_feedback(card), feedback_reasons=SalesFeedback.Reason.choices, request_key=str(uuid.uuid4()), development=development(event),company_info=company_fields(event.lead.company),event=event,event_label=_event_label(event),
        stream_label=WORKSTREAMS.get(event.workstream,'其他研究'),historical_source=_historical_source(event),
        size_label=SIZES.get(event.size.get('value'),'规模待核实'),
        has_website=any(f.get('field')=='website' for f in event.facts),
        website_checks=checks,
        related_reports=event.lead.possible_copies.select_related('source').all(),
        participants=[{**p,'ui_role':ROLES.get(p.get('role'),'角色待核实'),
            'ui_relationship':RELATIONS.get(p.get('relationship'),'关系待核实'),
            'ui_scope':SCOPES.get(p.get('market_scope'),p.get('market_scope',''))} for p in event.participants],
        dates=[{**d,'ui_kind':DATES.get(d.get('kind'),d.get('kind')),'ui_status':DATE_STATUS.get(d.get('status'),d.get('status'))} for d in event.dates],
        facts=[{**f,'ui_field':FIELDS.get(f.get('field'),f.get('field')),'ui_scope':SCOPES.get(f.get('market_scope'),f.get('market_scope',''))} for f in event.facts])
    return render(request,'radar/research_detail.html',ctx)

def _event_label(event):
    if event.event_type in {'historical_only','research_pending'} and event.progress == 'overdue_unconfirmed':
        return '过期计划 · 完成待核'
    return EVENTS.get(event.event_type,event.event_type)


@require_POST
def recheck_archived(request):
    from .archived_recheck import scan_archived_research

    rows = scan_archived_research(apply=True)
    added = sum(row['created'] for row in rows)
    existing = sum(bool(row.get('research_id')) for row in rows) - added
    messages.success(request,f'存量复核完成。标题命中 {len(rows)} 条，新增研究 {added} 条，已在研究 {existing} 条。')
    return redirect('/research/?stream=historical_review')


def _historical_source(event):
    """Classify old source material, without treating its fiscal label as a due date."""
    import re
    from django.utils import timezone
    if event.event_type == 'historical_only':
        return True
    fiscal=next((str(d.get('value','')) for d in event.dates if d.get('kind')=='plan_fiscal_year'),'')
    m=re.search(r'令和(\d+)',fiscal)
    if not m:return False
    today=timezone.localdate();current_fiscal=today.year if today.month>=4 else today.year-1
    return 2018+int(m.group(1))<current_fiscal

@require_http_methods(['GET','POST'])
def jbic_import(request):
    if request.method=='POST':
        upload=request.FILES.get('material');url=request.POST.get('url','').strip()
        if request.POST.get('rights')!='yes':messages.error(request,'请先确认有权提供和保存该材料。')
        elif not upload or upload.size>3_000_000:messages.error(request,'请选择不超过 3 MB 的 JBIC 公告 HTML。')
        else:
            try:
                from .research_import import import_jbic
                source=Source.objects.get(key='jbic_sme_china')
                lead,created,updated=import_jbic(source,upload.read(),url)
            except (Source.DoesNotExist,ValueError,OSError) as exc:messages.error(request,'未导入：'+str(exc)[:250])
            else:
                messages.success(request,'材料已保存并建立研究任务。' if created else '已核对该材料；重复提交没有产生重复任务。')
                return redirect('radar:research_detail',pk=lead.research_event.pk)
    return render(request,'radar/research_import.html',_base('research'))

@require_http_methods(['POST'])
def verify_website(request,pk):
    event=get_object_or_404(ResearchEvent.objects.select_related('lead'),pk=pk)
    try:
        from .website_check import check_listed_website
        doc=check_listed_website(event)
        if doc.metadata.get('status')=='explicit_name_match':messages.success(request,'页面出现名录中的企业全名；关联仍需人工核对。')
        elif doc.metadata.get('status')=='identity_unverified':messages.warning(request,'已读取页面，但未找到明确企业全名，关联待核。')
        else:messages.warning(request,'网站读取受阻或失败，具体原因已记录。')
    except ValueError as exc:messages.error(request,str(exc))
    return redirect('radar:research_detail',pk=event.pk)
