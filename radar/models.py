import uuid
from django.db import models
from django.utils import timezone
from django.utils.functional import cached_property

class Source(models.Model):
    key = models.SlugField(unique=True)
    name = models.CharField(max_length=160)
    url = models.URLField(max_length=1200)
    kind = models.CharField(max_length=32, default="procurement", choices=[("procurement", "物流采购"), ("early_signal", "早期信号"), ("enterprise_discovery", "企业发现"), ("recruitment", "招聘信号")])
    enabled = models.BooleanField(default=False)
    interval_minutes = models.PositiveIntegerField(default=240)
    checkpoint = models.JSONField(default=dict, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    next_run_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=32, default="unverified")
    note = models.TextField(blank=True)
    options = models.JSONField(default=dict, blank=True)
    validation = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["kind", "id"]

    def __str__(self):
        return self.name

class SourceRun(models.Model):
    STATUSES = [(x, y) for x,y in [
        ("queued","排队中"),("running","采集中"),("success","已完成"),("success_no_new","正常扫描 · 无新增"),
        ("partial","部分完成 · 待继续"),("blocked","访问受阻"),("failed","运行失败"),("cancelled","已中止"),
    ]]
    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="runs")
    status = models.CharField(max_length=32, choices=STATUSES, default="queued", db_index=True)
    requested_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    discovered_count = models.PositiveIntegerField(default=0)
    new_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    stats = models.JSONField(default=dict, blank=True)
    job_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    prefect_run_id = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ["-requested_at"]
        constraints = [models.UniqueConstraint(fields=["source"], condition=models.Q(status__in=["queued","running"]), name="one_active_run_per_source")]

class Company(models.Model):
    OWNERSHIP = [("unknown","背景待核实"),("japan_confirmed","日资 · 已核实"),("foreign_confirmed","其他外资 · 已核实"),("domestic_confirmed","内资 · 已核实")]
    name = models.CharField(max_length=240, unique=True)
    ownership_status = models.CharField(max_length=32, default="unknown", choices=OWNERSHIP)
    ownership_evidence = models.TextField(blank=True)
    first_seen_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    company_industry = models.CharField(max_length=40, blank=True)
    official_website = models.URLField(max_length=2000, blank=True)
    official_website_status = models.CharField(max_length=12, default='missing',
        choices=[('verified','已核实'),('unverified','待核实'),('missing','缺少依据')])
    profile = models.JSONField(default=dict, blank=True)
    discovery_evidence = models.JSONField(default=list, blank=True)
    subscribed = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

class Lead(models.Model):
    PROCESSING = [("actionable","具备跟进条件"),("needs_review","待核实"),("early_signal","早期信号"),("closed","已结束／历史信息"),("irrelevant","非目标服务")]
    REVIEWS = [("new","未复核"),("accepted","值得跟进"),("needs_info","需要补齐"),("not_fit","暂不匹配"),("contacted","已实际跟进")]
    EVENTS = [("tender","招标"),("rfq","询价"),("rfi","信息征询"),("supplier_recruitment","服务商招募"),("change","变更"),("award","结果公告"),("early_signal","早期信号"),("irrelevant","非目标服务"),("unknown","类型待核实")]
    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="leads")
    company = models.ForeignKey(Company, on_delete=models.SET_NULL, null=True, blank=True, related_name="leads")
    title = models.CharField(max_length=600)
    url = models.URLField(max_length=2000, blank=True)
    external_id = models.CharField(max_length=160)
    summary = models.TextField(blank=True)
    buyer_role = models.CharField(max_length=30, default="unknown")
    event_type = models.CharField(max_length=40, default="unknown", choices=EVENTS)
    processing_status = models.CharField(max_length=40, default="needs_review", choices=PROCESSING, db_index=True)
    review_status = models.CharField(max_length=32, default="new", choices=REVIEWS, db_index=True)
    service_types = models.JSONField(default=list, blank=True)
    regions = models.JSONField(default=list, blank=True)
    deadline = models.DateField(null=True, blank=True)
    # Only populated for an explicitly stated hour/minute; date-only notices stay null.
    deadline_at = models.DateTimeField(null=True, blank=True)
    deadline_kind = models.CharField(max_length=30, default="unknown")
    deadline_has_time = models.BooleanField(default=False)
    published_date = models.DateField(null=True, blank=True)
    access_level = models.CharField(max_length=24, default="public")
    missing_fields = models.JSONField(default=list, blank=True)
    evidence = models.JSONField(default=list, blank=True)
    qualifications = models.JSONField(default=list, blank=True)
    participation_path = models.TextField(blank=True)
    service_period = models.TextField(blank=True)
    text = models.TextField(blank=True)
    content_hash = models.CharField(max_length=64)
    extraction_method = models.CharField(max_length=24, default="rules")
    model_warning = models.TextField(blank=True)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    version = models.PositiveIntegerField(default=1)
    metadata = models.JSONField(default=dict, blank=True)
    subject_reason = models.CharField(max_length=48, blank=True)
    deadline_semantics = models.CharField(max_length=20, default="unknown")
    extraction_version = models.PositiveIntegerField(default=0)
    is_test = models.BooleanField(default=False, db_index=True)
    duplicate_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="possible_copies")

    class Meta:
        ordering = ["-first_seen_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["source", "external_id"], name="unique_source_item")]
        indexes = [models.Index(fields=["content_hash"])]

    def __str__(self):
        return self.title

class Review(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="reviews")
    status = models.CharField(max_length=32, choices=Lead.REVIEWS)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]

class Document(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="documents")
    content_hash = models.CharField(max_length=64)
    text = models.TextField()
    raw_path = models.CharField(max_length=1200, blank=True)
    fetched_at = models.DateTimeField(default=timezone.now)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-fetched_at"]
        constraints = [models.UniqueConstraint(fields=["lead", "content_hash"], name="unique_document_version")]


class SourceManifest(models.Model):
    """Explicit access and verification status; a card alone is not a connection."""
    source = models.OneToOneField(Source, on_delete=models.CASCADE, related_name='manifest')
    format = models.CharField(max_length=80)
    coverage = models.TextField(blank=True)
    access_policy = models.TextField(blank=True)
    verification_status = models.CharField(max_length=40, default='candidate')
    evidence = models.JSONField(default=dict, blank=True)


class ResearchEvent(models.Model):
    """Evidence-linked business research; never a confirmed procurement by itself."""
    lead = models.OneToOneField(Lead, on_delete=models.CASCADE, related_name='research_event')
    event_type = models.CharField(max_length=40)
    workstream = models.CharField(max_length=32)
    china_scope = models.CharField(max_length=24, default='unknown')
    china_evidence = models.JSONField(default=dict)
    participants = models.JSONField(default=list)
    dates = models.JSONField(default=list)
    facts = models.JSONField(default=list)
    size = models.JSONField(default=dict)
    inferences = models.JSONField(default=list)
    questions = models.JSONField(default=list)
    next_step = models.TextField(blank=True)
    source_document_hash = models.CharField(max_length=64)
    observed_at = models.DateTimeField(default=timezone.now)

    @property
    def progress(self):
        from .research import research_progress
        return research_progress(self.dates)

class RuntimeState(models.Model):
    key = models.CharField(max_length=100, unique=True)
    value = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)

class ModelCall(models.Model):
    requested_at = models.DateTimeField(default=timezone.now, db_index=True)
    status = models.CharField(max_length=20, default="reserved")
    analysis = models.ForeignKey("AnalysisRun", null=True, blank=True, on_delete=models.SET_NULL, related_name="calls")
    provider = models.CharField(max_length=40, blank=True)
    model = models.CharField(max_length=160, blank=True)
    usage = models.JSONField(null=True, blank=True)
    cost = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)


class AIConfig(models.Model):
    """One local configuration. Credentials live only in OS keyring or explicit env."""
    enabled = models.BooleanField(default=False)
    auto_new = models.BooleanField(default=False)
    protocol = models.CharField(max_length=40, default="chat_completions")
    base_url = models.URLField(max_length=1200, blank=True)
    model = models.CharField(max_length=160, blank=True)
    pro_thinking = models.BooleanField(default=True)
    deep_model = models.CharField(max_length=32, default="mimo-v2.6-pro", blank=True, choices=[("mimo-v2.6-pro","MiMo-V2.6-Pro")])
    key_ref = models.CharField(max_length=80, blank=True)
    key_source = models.CharField(max_length=20, default="environment")
    daily_limit = models.PositiveIntegerField(default=30)
    batch_limit = models.PositiveIntegerField(default=8)
    max_tokens = models.PositiveIntegerField(default=2500)
    input_chars = models.PositiveIntegerField(default=24000)
    timeout_seconds = models.PositiveIntegerField(default=45)
    search_enabled = models.BooleanField(default=False)
    search_key_ref = models.CharField(max_length=80, blank=True)
    search_daily_limit = models.PositiveIntegerField(default=10)
    version = models.PositiveIntegerField(default=1)
    test_status = models.CharField(max_length=40, default="untested")
    tested_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class BusinessCapability(models.Model):
    regions = models.TextField(blank=True)
    services = models.TextField(blank=True)
    industries = models.TextField(blank=True)
    qualifications = models.TextField(blank=True)
    exclusions = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)


class ExtractionRun(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="extractions")
    source_hash = models.CharField(max_length=64)
    extractor_version = models.CharField(max_length=40)
    result = models.JSONField(default=dict)
    documents = models.JSONField(default=list)
    created_at = models.DateTimeField(default=timezone.now)


class AnalysisRun(models.Model):
    @cached_property
    def provider_call(self):
        return self.calls.order_by('-id').first()

    @property
    def provider_model(self):
        return self.result.get('model') or (self.provider_call.model if self.provider_call else '')

    @property
    def usage_summary(self):
        usage = self.result.get('usage') or (self.provider_call.usage if self.provider_call else {})
        if not usage:
            return '暂无用量记录（未调用或供应商未返回）'
        parts = []
        for key, label in [('prompt_tokens', '输入'), ('completion_tokens', '输出'), ('total_tokens', '合计')]:
            value = usage.get(key)
            value = str(value) if type(value) is int and value >= 0 else '未知'
            parts.append(f'{label} {value} token')
        return ' · '.join(parts)

    @property
    def error_label(self):
        from .labels import ERROR_LABELS
        return ERROR_LABELS.get(self.error_code, "处理未完成" if self.error_code else "")

    TYPES = [("analysis", "营业分析"), ("extract", "字段抽取/校验"), ("enrich", "补全采购主体"), ("attachment", "公开附件取证"), ("material", "用户材料解析"), ("test", "连接测试")]
    STATUSES = [("queued", "排队中"), ("running", "执行中"), ("succeeded", "已完成"), ("needs_review", "已生成，部分内容待复核"), ("failed", "失败，可手动重试"), ("cancelled", "已取消")]
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.CASCADE, related_name="analyses")
    task_type = models.CharField(max_length=24, choices=TYPES)
    status = models.CharField(max_length=24, choices=STATUSES, default="queued", db_index=True)
    request_key = models.CharField(max_length=140, unique=True)
    batch_key = models.CharField(max_length=80, blank=True, db_index=True)
    fingerprint = models.CharField(max_length=64, db_index=True)
    input_snapshot = models.JSONField(default=dict)
    config_snapshot = models.JSONField(default=dict)
    capability_snapshot = models.JSONField(default=dict)
    prompt_version = models.CharField(max_length=40, default="sales-v02.1")
    schema_version = models.CharField(max_length=40, default="evidence-v02.1")
    result = models.JSONField(default=dict)
    error_code = models.CharField(max_length=64, blank=True)
    stale = models.BooleanField(default=False)
    requested_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]


class EntityCandidate(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="entity_candidates")
    analysis = models.ForeignKey(AnalysisRun, null=True, blank=True, on_delete=models.SET_NULL)
    name = models.CharField(max_length=240)
    role = models.CharField(max_length=40)
    source_url = models.URLField(max_length=2000, blank=True)
    document_hash = models.CharField(max_length=64)
    evidence = models.JSONField(default=dict)
    status = models.CharField(max_length=40, default="suggested")
    created_at = models.DateTimeField(default=timezone.now)


class SearchCall(models.Model):
    analysis = models.ForeignKey(AnalysisRun, null=True, blank=True, on_delete=models.SET_NULL)
    query = models.CharField(max_length=600)
    status = models.CharField(max_length=24, default="reserved")
    urls = models.JSONField(default=list)
    requested_at = models.DateTimeField(default=timezone.now)
    cost = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)
    tavily_credits = models.PositiveIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)


class KeywordCandidate(models.Model):
    """Unverified search result; never silently becomes a confirmed company."""
    canonical_url = models.CharField(max_length=2000, unique=True)
    title = models.CharField(max_length=600)
    snippet = models.TextField(blank=True)
    published_at = models.CharField(max_length=80, blank=True)
    provider = models.CharField(max_length=40)
    query_id = models.CharField(max_length=100)
    status = models.CharField(max_length=40, default="discovered")
    text = models.TextField(blank=True)
    raw_path = models.CharField(max_length=1200, blank=True)
    result = models.JSONField(default=dict, blank=True)
    error_detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)


class KeywordCursor(models.Model):
    key = models.CharField(max_length=40, unique=True, default='default')
    offset = models.PositiveBigIntegerField(default=0)



class SalesFeedback(models.Model):
    class Status(models.TextChoices):
        NEW = 'new', '待营业看'
        ACCEPTED = 'accepted', '接受跟进'
        HOLD = 'hold', '暂缓观察'
        REJECTED = 'rejected', '退回'

    class Reason(models.TextChoices):
        REGION_MISMATCH = 'region_mismatch', '区域不符'
        VOLUME_TOO_SMALL = 'volume_too_small', '货量太小'
        CAPABILITY_MISMATCH = 'capability_mismatch', '服务能力不符'
        GROUP_LOCKED = 'group_locked', '集团采购封闭'
        WINDOW_MISSED = 'window_missed', '窗口已过'
        NO_CONTACT_PATH = 'no_contact_path', '无有效接触路径'
        ENTITY_OR_EVENT_ERROR = 'entity_or_event_error', '企业或事件误判'

    source_url = models.CharField(max_length=2200, unique=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.NEW)
    reason = models.CharField(max_length=24, choices=Reason.choices, blank=True)
    note = models.CharField(max_length=1000, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(status='rejected', reason__in=[
                    'region_mismatch', 'volume_too_small', 'capability_mismatch', 'group_locked',
                    'window_missed', 'no_contact_path', 'entity_or_event_error',
                ]) | models.Q(status__in=['new', 'accepted', 'hold'], reason='')),
                name='valid_sales_feedback',
            )
        ]
