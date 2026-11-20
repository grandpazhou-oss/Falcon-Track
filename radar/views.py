import uuid
import re
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Max, Q
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .forms import CompanyForm, ImportForm, OWNERSHIP_CHOICES, REVIEW_CHOICES, ReviewForm, SourceSettingsForm
from .models import Company, Document, Lead, Review, Source, SourceRun, RuntimeState


STATUS_LABELS = {
    "actionable": "具备跟进条件", "needs_review": "需要核实", "early_signal": "早期信号",
    "closed": "已结束", "irrelevant": "不相关", "raw": "待处理", "parsed": "已解析",
    "needs_enrichment": "待补齐",
}
EVENT_LABELS = {
    "tender": "招标公告", "rfq": "询价", "rfi": "信息征询", "supplier_recruitment": "供应商招募",
    "change": "项目变更", "award": "结果公告", "early_signal": "早期信号", "irrelevant": "不相关",
    "unknown": "类型待核实",
}
SERVICE_LABELS = {
    "transport": "运输", "warehousing": "仓储", "distribution": "配送", "freight_forwarding": "国际货代",
    "customs": "报关", "cold_chain": "冷链", "inplant": "厂内物流", "handling": "装卸搬运",
}
RUN_LABELS = {
    "queued": "等待执行", "running": "采集中", "success": "采集完成", "success_no_new": "正常扫描 · 无新增",
    "partial": "部分完成 · 待续采", "blocked": "访问受阻", "failed": "采集失败", "cancelled": "已停止",
    "idle": "尚未运行", "candidate": "待验证", "pending": "待运行",
}
DEADLINE_LABELS = {"registration": "报名截止", "bid": "报价截止", "unknown": "来源截止日期 · 类型待核实"}
ACCESS_LABELS = {"public": "公开正文", "partial": "内容不完整", "blocked": "访问受限", "manual": "手工提供"}
REVIEW_LABELS = dict(REVIEW_CHOICES)
OWNERSHIP_LABELS = dict(OWNERSHIP_CHOICES)
MISSING_LABELS = {
    "buyer_name": "采购方", "buyer": "采购方", "buyer_legal_name": "采购方", "deadline": "有效期限",
    "deadline_kind": "期限类型", "participation_path": "参加方式", "service_types": "服务范围",
    "ownership": "企业背景", "ownership_status": "企业背景", "ownership_evidence": "企业背景依据", "qualifications": "资格条件",
    "full_text_or_attachment_access": "完整正文 / 附件权限",
    "deadline_time_verification": "当日截止时刻", "registration_deadline": "报名前置期限",
    "registration_closed": "报名或文件获取已截止，已报名者能否继续投标需核实",
    "deadline_conflict": "正文与列表截止时间冲突",
}
EVIDENCE_LABELS = {**MISSING_LABELS, "event_type": "公告类型", "regions": "地区 / 线路", "buyer_role": "主体角色", "service_period": "服务期间", "published_date": "发布日期", "listed_deadline": "列表截止日期", "listed_deadline_kind": "列表期限类别"}


def _base(active):
    from .ai_config import config
    cfg = config()
    model_enabled = cfg.enabled
    return {
        "active_nav": active,
        "today": timezone.localdate(),
        "model_enabled": model_enabled,
        "model_mode": "AI 可用" if model_enabled else "AI 未启用",
        "app_version": "0.4-sme-r1（隔离）", "request_key": str(uuid.uuid4()),
    }


def _safe_url(value):
    try:
        parsed = urlsplit(value or "")
        return value if parsed.scheme in ("http", "https") and parsed.netloc else ""
    except ValueError:
        return ""


def _lead_ui(lead):
    from .entities import REASONS, japanese_link_for_lead, business_role_for_company
    from .queues import japan_signal, LINK_LABELS
    japanese_link=japanese_link_for_lead(lead)
    japanese=japan_signal(lead,japanese_link)
    lead.ui_japan_link=LINK_LABELS.get(japanese_link['level'],'')
    lead.ui_japan_peer='日系物流同行（二级分包机会）· 关联待核实' if japanese_link['role']=='logistics_peer' else ''
    labels={'shipper':'货主企业候选 · 待核实','peer_logistics':'日系物流同行候选 · 二级分包机会','unknown_service':'非物流服务／业务角色待核实'}
    role=business_role_for_company(lead.company.name,lead.company.profile,japanese_link) if lead.company_id else 'unknown_service'
    lead.ui_business_role=labels[role] if lead.company_id else ''
    lead.ui_japan_signal=japanese['signal'] if japanese else ''
    lead.ui_subject_reason = REASONS.get(lead.subject_reason, "")
    lead.ui_status = STATUS_LABELS.get(lead.processing_status, "待核实")
    lead.ui_event_type = EVENT_LABELS.get(lead.event_type, "类型待核实")
    lead.ui_services = [SERVICE_LABELS.get(item, item) for item in (lead.service_types or [])]
    missing = list(lead.missing_fields or [])
    lead.ui_manual_buyer = bool((lead.metadata or {}).get("manual_buyer") and lead.company_id)
    if lead.ui_manual_buyer:
        missing = [item for item in missing if item not in ("buyer", "buyer_name", "buyer_legal_name")]
    if lead.company_id and lead.company.ownership_status != "unknown" and lead.company.ownership_evidence:
        missing = [item for item in missing if item not in ("ownership", "ownership_status", "ownership_evidence")]
    lead.ui_missing = [MISSING_LABELS.get(item, item) for item in missing]
    if lead.processing_status == "early_signal":
        lead.ui_missing = ["关联企业 / 建设单位" if item == "采购方" else item for item in lead.ui_missing]
    lead.ui_review = REVIEW_LABELS.get(lead.review_status, "尚未复核")
    lead.ui_deadline_kind = DEADLINE_LABELS.get(lead.deadline_kind, DEADLINE_LABELS["unknown"])
    lead.ui_access = ACCESS_LABELS.get(lead.access_level, "权限待确认")
    lead.ui_url = _safe_url(lead.url)
    lead.ui_ownership = OWNERSHIP_LABELS.get(lead.company.ownership_status, "背景待核实") if lead.company_id else "背景待核实"
    lead.ui_days_left = (lead.deadline - timezone.localdate()).days if lead.deadline else None
    deadline_at = getattr(lead, "deadline_at", None)
    lead.ui_deadline_expired = deadline_at <= timezone.now() if deadline_at else bool(lead.deadline and lead.deadline < timezone.localdate())
    lead.ui_evidence = []
    for item in (lead.evidence or []):
        if not isinstance(item, dict) or not item.get("quote"):
            continue
        field = item.get("field", "")
        value = item["quote"]
        if field == "listed_deadline_kind":
            value = DEADLINE_LABELS.get(value, "期限类型待核实")
        lead.ui_evidence.append({
            "field": EVIDENCE_LABELS.get(field, field or "原文"), "quote": value,
            "is_metadata": field in ("listed_deadline", "listed_deadline_kind"),
        })
    return lead


def _run_ui(run):
    run.ui_status = RUN_LABELS.get(run.status, run.status)
    run.ui_active = run.status in ("queued", "running")
    stats = run.stats or {}
    run.ui_head_pages = stats.get("head_pages_limit")
    run.ui_coverage_note = stats.get("coverage_note", "")
    run.ui_partial_reason = stats.get("partial_reason", "")
    if run.status == "partial" and run.ui_partial_reason == "errors":
        run.ui_status = "部分完成 · 出错待重试"
    elif run.status == "partial" and run.ui_partial_reason == "budget":
        run.ui_status = "部分完成 · 分批续采"
    return run


@require_GET
def dashboard(request):
    from .sales_ui import sales_data
    from .sales_feedback import distribution
    _, groups=sales_data(request)
    ctx=_base('dashboard')
    ctx.update(priority_cards=groups['A'], grade_a=len(groups['A']), grade_b=len(groups['B']),
               feedback_counts=distribution(groups)['statuses'])
    return render(request, "radar/dashboard.html", ctx)


@require_GET
def lead_list(request):
    ctx = _base("leads")
    leads = Lead.objects.select_related("company", "source").order_by("-first_seen_at")
    from .queues import select_queue, LABELS
    ctx["queue_choices"] = LABELS.items()
    leads=select_queue(leads,request.GET.get("queue", "")) if request.GET.get("queue") else leads
    filters = {key: request.GET.get(key, "").strip() for key in ("q", "status", "service", "review", "ownership", "queue")}
    if filters["q"]:
        leads = leads.filter(Q(title__icontains=filters["q"]) | Q(company__name__icontains=filters["q"]) | Q(text__icontains=filters["q"]))
    if filters["status"] in STATUS_LABELS:
        leads = leads.filter(processing_status=filters["status"])
    if filters["service"] in SERVICE_LABELS:
        leads = leads.filter(service_types__icontains='"' + filters["service"] + '"')
    if filters["review"] in REVIEW_LABELS:
        leads = leads.filter(review_status=filters["review"])
    if filters["ownership"] == "unknown":
        leads = leads.filter(Q(company__isnull=True) | Q(company__ownership_status="unknown"))
    elif filters["ownership"] in OWNERSHIP_LABELS:
        leads = leads.filter(company__ownership_status=filters["ownership"])
    page = Paginator(leads, 20).get_page(request.GET.get("page"))
    page.object_list = [_lead_ui(x) for x in page.object_list]
    ctx.update({"page": page, "filters": filters, "status_choices": STATUS_LABELS.items(),
                "service_choices": SERVICE_LABELS.items(), "review_choices": REVIEW_CHOICES,
                "ownership_choices": OWNERSHIP_CHOICES, "query_params": urlencode({k:v for k,v in filters.items() if v})})
    return render(request, "radar/leads.html", ctx)


def _detail_context(lead, review_form=None, company_form=None):
    from .company_enrichment import fields as company_fields
    lead.company_info = company_fields(lead.company)
    ctx = _base("leads")
    ctx["reanalysis_key"] = str(__import__("uuid").uuid4())
    from .ai_config import capability
    from .analysis import review_historical_for_display
    analysis_runs=list(lead.analyses.filter(input_snapshot__translation_of__isnull=True)[:15])
    cap_version=capability().version
    for run in analysis_runs:
        if run.task_type!="enrich" and (run.input_snapshot.get("extraction_version")!=lead.extraction_version or run.capability_snapshot.get("version")!=cap_version):run.stale=True
        review_historical_for_display(run)
    reviews = list(Review.objects.filter(lead=lead).order_by("-created_at"))
    for review in reviews:
        review.ui_status = REVIEW_LABELS.get(review.status, review.status)
    company_form = company_form or CompanyForm(initial={
        "buyer_name": lead.company.name if lead.company_id else "",
        "ownership_status": lead.company.ownership_status if lead.company_id else "unknown",
        "ownership_evidence": lead.company.ownership_evidence if lead.company_id else "",
    })
    if lead.processing_status == "early_signal":
        company_form.fields["buyer_name"].label = "关联企业 / 建设单位"
    project_number = lead.metadata.get("project_number")
    related_announcements = []
    if project_number and lead.company_id:
        related_announcements = Lead.objects.filter(
            source=lead.source, company_id=lead.company_id,
            metadata__project_number=project_number,
        ).exclude(pk=lead.pk).order_by("-published_date", "-id")[:10]
    ctx.update({"lead": _lead_ui(lead), "reviews": reviews,
                "related_announcements": related_announcements,
                "public_pdf_attachments": [a for a in (lead.metadata or {}).get('attachments',[]) if isinstance(a,dict) and isinstance(a.get('url'),str) and urlsplit(a['url']).path.lower().endswith('.pdf')],
                "documents": Document.objects.filter(lead=lead).order_by("-fetched_at")[:8],
                "review_form": review_form or ReviewForm(initial={"status": lead.review_status}),
                "company_form": company_form, "analysis_runs":analysis_runs, "entity_candidates":lead.entity_candidates.all().order_by("-id")[:40]})
    return ctx


@require_GET
def lead_detail(request, pk):
    lead = get_object_or_404(Lead.objects.select_related("company", "source"), pk=pk)
    from .models import SalesFeedback
    from .sales_feedback import detail_feedback
    from .sales_pools import lead_card
    ctx = _detail_context(lead)
    ctx.update(feedback_card=detail_feedback(lead_card(lead)), feedback_reasons=SalesFeedback.Reason.choices)
    return render(request, "radar/detail.html", ctx)


@require_POST
def review_lead(request, pk):
    lead = get_object_or_404(Lead.objects.select_related("company", "source"), pk=pk)
    form = ReviewForm(request.POST)
    if form.is_valid():
        with transaction.atomic():
            lead.review_status = form.cleaned_data["status"]
            lead.save(update_fields=["review_status"])
            Review.objects.create(lead=lead, status=lead.review_status, note=form.cleaned_data["note"])
        messages.success(request, "复核结果已保存。")
        return redirect("radar:lead_detail", pk=pk)
    return render(request, "radar/detail.html", _detail_context(lead, review_form=form), status=400)


@require_POST
def update_company(request, pk):
    lead = get_object_or_404(Lead.objects.select_related("company", "source"), pk=pk)
    form = CompanyForm(request.POST)
    if form.is_valid():
        with transaction.atomic():
            # Re-read the row under the transaction lock so a collector's newer
            # source metadata is merged rather than overwritten by the earlier read.
            lead = get_object_or_404(Lead.objects.select_for_update(), pk=pk)
            company, created = Company.objects.get_or_create(name=form.cleaned_data["buyer_name"])
            # Preserve an already verified company when a newly linked lead sends the default unknown state.
            if created or company.pk == lead.company_id or form.cleaned_data["ownership_status"] != "unknown":
                company.ownership_status = form.cleaned_data["ownership_status"]
                company.ownership_evidence = form.cleaned_data["ownership_evidence"]
                company.save()
            lead.company = company
            lead.metadata = {**(lead.metadata or {}), "manual_buyer": True}
            lead.save(update_fields=["company", "metadata"])
            entity_label = "关联企业" if lead.processing_status == "early_signal" else "采购主体"
            Review.objects.create(lead=lead, status=lead.review_status,
                                  note="人工补充" + entity_label + "：" + company.name + "。企业背景：" + OWNERSHIP_LABELS.get(company.ownership_status, "待核实") + "。")
        messages.success(request, "企业资料已保存。")
        return redirect("radar:lead_detail", pk=pk)
    return render(request, "radar/detail.html", _detail_context(lead, company_form=form), status=400)


@require_http_methods(["GET", "POST"])
def import_lead(request):
    form = ImportForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        from .services import import_text
        lead = import_text(**form.cleaned_data)
        messages.success(request, "原文已保存，可以查看整理结果。")
        return redirect("radar:lead_detail", pk=lead.pk)
    ctx = _base("import")
    ctx["form"] = form
    return render(request, "radar/import.html", ctx, status=400 if request.method == "POST" else 200)


@require_GET
def source_list(request):
    ctx = _base("sources")
    sources = list(Source.objects.exclude(key__startswith="manual").order_by("id"))
    from .source_modes import source_mode
    from .models import KeywordCandidate
    from .search_provider import canonical_url
    from .context_processors import collector_alive
    from collections import defaultdict

    from collectors.source_cards import NOTES, LISTS, CORPORATE, ALTERNATIVES
    by_key = {source.key: source for source in sources}
    by_url=defaultdict(set)
    for source_id, pk, url in Lead.objects.filter(is_test=False).values_list('source_id', 'pk', 'url'):
        by_url[source_id].add(canonical_url(url) or pk)
    candidates=defaultdict(set)
    for key,url in KeywordCandidate.objects.values_list('provider','canonical_url'):
        candidates[key].add(url)
    recent_success=dict(SourceRun.objects.filter(Q(status__in=['success','success_no_new']) |
                       Q(status='partial',discovered_count__gt=0) |
                       Q(status='partial',stats__confirmed_public_read=True))
                       .values('source_id').annotate(last=Max('finished_at')).values_list('source_id','last'))
    for source in sources:
        source.ui_mode,source.ui_reason=source_mode(source.key)
        source.latest_run = SourceRun.objects.filter(source=source).order_by("-id").first()
        if source.latest_run:
            _run_ui(source.latest_run)
        source.ui_kind = dict(Source._meta.get_field("kind").choices).get(source.kind,source.kind)
        source.lead_count = len(by_url[source.pk] | candidates[source.key])
        times=[t for t in (source.last_success_at,recent_success.get(source.pk)) if t]
        source.ui_last_success=max(times) if times else None
        source.ui_error=collection_error(source.latest_run.error) if source.latest_run else '' 
        source.settings_form = SourceSettingsForm(initial={"enabled": source.enabled, "interval_minutes": source.interval_minutes, "head_pages": source.options.get("head_pages",settings.LEAD_HEAD_PAGES), "max_items": source.options.get("max_items",50 if source.kind == "enterprise_discovery" else settings.LEAD_MAX_ITEMS),**{k:source.options.get(k) for k in ('max_accepted_items','max_detail_requests','max_total_requests','max_elapsed_seconds')},'keywords':','.join(source.options.get('keywords',[])) if isinstance(source.options.get('keywords'),list) else source.options.get('keywords','')}, auto_id=f"source_{source.pk}_%s")
        source.ui_note = NOTES.get(source.key, '')
        source.ui_url = _safe_url((LISTS.get(source.key) or ('',))[0] or source.url)
        if source.key in CORPORATE:
            source.ui_url = ''
        if source.key in ALTERNATIVES:
            target, source.alt_label = ALTERNATIVES[source.key]
            source.alt_source = by_key.get(target)

    sources.sort(key=lambda source: source.ui_mode)
    ctx.update({"sources": sources, "active_runs": SourceRun.objects.filter(status__in=["queued", "running"]).count(),
                "source_worker_alive":collector_alive(), "head_pages": getattr(settings, "LEAD_HEAD_PAGES", 3)})
    return render(request, "radar/sources.html", ctx)


def collection_error(error):
    if not error:
        return ''
    check=error.lower()
    if 'robots' in check and any(word in check for word in ('disallow','invalid_format','not allowed','禁止')):
        return '网站规则限制访问。'
    if 'robots' in check:
        return '暂时无法确认网站访问规则，已停止读取。'
    if any(word in check for word in ('timeout','time_limit','超时','时间预算')):
        return '网站响应超时，可稍后重试。'
    if any(word in check for word in ('connect_failed','dns_failed','network','连接失败')):
        return '暂时无法连接原站，可稍后重试。'
    if any(word in check for word in ('http_403','http_401','http_429','http 403','http 401','http 429','captcha','验证')):
        return '原站限制访问，请查看原始栏目。'
    if any(word in check for word in ('layout','anchors missing','body missing','结构','jsondecode','placeholder')):
        return '原站版式可能有变，请查看原始栏目。'
    if check in ('no_relevant_action','mainland_company_and_finance_evidence_missing'):
        return '未找到符合条件的新线索。'
    if error.startswith(('上次采集中断','长期未开始')):
        return error
    return '部分内容未取得，详情见运行记录。'


@require_POST
def run_source(request, pk):
    source = get_object_or_404(Source, pk=pk)
    if source.key.startswith("manual"):
        return HttpResponseBadRequest("手工导入来源不能执行网页采集。")
    from .services import enqueue_source
    try:
        enqueue_source(source.pk)
    except (ValueError, RuntimeError) as exc:
        messages.error(request, str(exc))
    else:
        from .context_processors import collector_alive
        body="已排队，可在运行记录查看进度。" if collector_alive() else "已加入队列，启动后台采集进程后执行。"
        messages.success(request,body)
    return redirect("radar:sources")


@require_POST
def source_settings(request, pk):
    source = get_object_or_404(Source, pk=pk)
    if source.key.startswith("manual"):
        return HttpResponseBadRequest("手工导入来源没有定时采集设置。")
    form = SourceSettingsForm(request.POST)
    if form.is_valid():
        from .source_modes import schedulable
        if form.cleaned_data["enabled"] and not schedulable(source.key):
            messages.error(request, '此来源尚未完成接入或受使用条款限制，不能启用自动采集。')
            return redirect('radar:sources')
        source.enabled = form.cleaned_data["enabled"]
        source.interval_minutes = form.cleaned_data["interval_minutes"]
        source.options = {**source.options,"head_pages": form.cleaned_data.get("head_pages") or settings.LEAD_HEAD_PAGES,"max_items":form.cleaned_data.get("max_items") or settings.LEAD_MAX_ITEMS}
        for field in ('max_accepted_items','max_detail_requests','max_total_requests','max_elapsed_seconds'):
            if form.cleaned_data.get(field) is not None:source.options[field]=form.cleaned_data[field]
        if source.key=='dfmc_nissan' and form.cleaned_data.get('keywords'):
            source.options['keywords']=[x.strip() for x in re.split(r'[,，\n]+',form.cleaned_data['keywords']) if x.strip()][:10]
        source.next_run_at = timezone.now() if source.enabled else None
        source.save(update_fields=["enabled", "interval_minutes", "next_run_at", "options"])
        messages.success(request, "来源设置已保存。")
    else:
        messages.error(request, "采集间隔必须为 60 至 10080 分钟，请检查后重新保存。")
    return redirect("radar:sources")


@require_POST
def scheduler_settings(request):
    enabled = request.POST.get("enabled")
    if enabled not in ("true", "false"):
        return HttpResponseBadRequest("无效的定时采集设置。")
    RuntimeState.objects.update_or_create(key="scheduler_control", defaults={"value": {"enabled": enabled == "true"}})
    if enabled == "true":
        messages.success(request, "定时采集已开启。")
    else:
        messages.success(request, "定时采集已暂停。")
    return redirect("radar:sources")


@require_GET
def run_list(request):
    ctx = _base("runs")
    runs = SourceRun.objects.select_related("source").order_by("-id")
    status = request.GET.get("status", "")
    if status in RUN_LABELS:
        runs = runs.filter(status=status)
    page = Paginator(runs, 25).get_page(request.GET.get("page"))
    page.object_list = [_run_ui(x) for x in page.object_list]
    ctx.update({"page": page, "selected_status": status, "run_choices": [(k,v) for k,v in RUN_LABELS.items() if k not in ("idle", "candidate", "pending")],
                "active_runs": SourceRun.objects.filter(status__in=["queued", "running"]).count(),
                "query_params": urlencode({"status":status}) if status else ""})
    return render(request, "radar/runs.html", ctx)
