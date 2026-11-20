"""Persistent business lifecycle. A successful job is not a qualified opportunity."""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from filelock import FileLock, Timeout

from radar.models import Company, Document, Lead, ModelCall, RuntimeState, Source, SourceRun

SOURCE_DEFINITIONS = [
    {"key": "ysczw", "name": "运输采招网 · 物流招标", "url": "https://www.ysczw.com/?a=index&classify_id=6&m=goods", "kind": "procurement", "note": "公开栏目与摘要。部分采购方、正文或附件需要会员；缺失项保留待核实。"},
    {"key": "suzhou_epia", "name": "苏州环保产业协会 · 项目公示", "url": "https://www.sz-epia.cn/xmgs.asp?classid=29", "kind": "early_signal", "note": "协会项目公示，仅作为企业变化信号；不等于已确认物流外包。"},
]

from collectors.sources import CATALOG
SOURCE_DEFINITIONS.extend({"key":k,"name":v["name"],"url":v["entry"],"kind":v["kind"],"note":v["note"]} for k,v in CATALOG.items())

from radar.discovery import DIRECTORIES
SOURCE_DEFINITIONS.extend(DIRECTORIES.values())
from collectors.japanese_public import REGISTRY as PUBLIC_SOURCES
SOURCE_DEFINITIONS.extend(PUBLIC_SOURCES.values())
from radar.research_sources import SOURCES as RESEARCH_SOURCES, EXECUTABLE as RESEARCH_EXECUTABLE, definitions as research_definitions
from .source_modes import FREE_GROUPS, ON_DEMAND, schedulable, source_mode
SOURCE_DEFINITIONS.extend(research_definitions())

def seed_sources():
    for record in SOURCE_DEFINITIONS:
        source, _ = Source.objects.get_or_create(key=record["key"], defaults={**record, "enabled": False})
        if source.key in RESEARCH_SOURCES:
            from radar.models import SourceManifest
            item = RESEARCH_SOURCES[source.key]
            manifest_writer = (SourceManifest.objects.update_or_create if source.key in {
                'kanto_verified_cases', 'sh_market_disclosures', 'japan_china_store_news'} else SourceManifest.objects.get_or_create)
            manifest_writer(source=source, defaults={'format':item[2], 'coverage':item[3],
                'access_policy':item[4], 'verification_status':item[5]})

def automatic_enabled():
    state = RuntimeState.objects.filter(key="scheduler_control").first()
    return bool(state.value.get("enabled", False)) if state else settings.LEAD_AUTOMATIC_ENABLED

def enqueue_source(source_id):
    source = Source.objects.get(pk=source_id)
    mode,reason=source_mode(source.key)
    if mode!='A':
        raise ValueError(reason)
    try:
        # If the worker owns the lock, its running job is healthy as far as the UI can know.
        with FileLock(str(settings.DATA_DIR / "worker.lock"), timeout=0):
            _recover_running_sources(SourceRun.objects.filter(source=source,status="running"))
            old=SourceRun.objects.filter(source=source,status="queued",
                                         requested_at__lt=timezone.now()-timedelta(hours=2))
            old.update(status="cancelled",finished_at=timezone.now(),error="长期未开始，已用本次提交替换。")
    except Timeout:
        pass
    existing = SourceRun.objects.filter(source=source, status__in=["queued", "running"]).first()
    if existing:
        return existing
    try:
        with transaction.atomic():
            return SourceRun.objects.create(source=source)
    except IntegrityError:
        return SourceRun.objects.get(source=source, status__in=["queued", "running"])

def _iso_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            return timezone.localtime(parsed).date() if timezone.is_aware(parsed) else parsed.date()
        except (TypeError, ValueError):
            return None

def _digest(title, text, *, url="", source_kind="procurement", published_date=None,
            listed_deadline=None, access_level="public", attachments=None, metadata=None):
    """Hash source facts that affect a lead, excluding traversal and fetch bookkeeping."""
    volatile = {
        "page", "list_page", "page_no", "page_number", "cursor", "next_cursor",
        "fetched_at", "fetched_at_utc", "fetch_time", "fetched_time", "scraped_at",
        "collected_at", "observed_at", "last_seen_at", "retrieved_at", "request_id",
        "run_id", "task_id", "fetch_duration", "fetch_duration_ms", "download_latency",
        "elapsed", "elapsed_ms", "retry_count", "attempt",
    }

    def stable(value):
        if isinstance(value, dict):
            return {
                str(key): stable(item) for key, item in value.items()
                if str(key).lower() not in volatile
                and not str(key).startswith(("manual_", "_"))
            }
        if isinstance(value, (list, tuple)):
            return [stable(item) for item in value]
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return value

    # Attachment order is presentation detail; changed URLs, labels or access are facts.
    files = sorted(
        (stable(item) for item in (attachments or [])),
        key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),
    )
    normalized_text = "\n".join(
        re.sub(r"[ \t]+", " ", line).strip()
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ).strip()
    payload = {
        "title": re.sub(r"\s+", " ", title).strip(), "text": normalized_text,
        "url": url, "source_kind": source_kind, "published_date": published_date,
        "listed_deadline": listed_deadline, "access_level": access_level,
        "attachments": files, "metadata": stable(metadata or {}),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def _reserve_model_call():
    if not settings.LEAD_MODEL_ENABLED:
        return None
    if not all(os.environ.get(x) for x in ["LEAD_MODEL_BASE_URL", "LEAD_MODEL_NAME", "LEAD_MODEL_API_KEY"]):
        return None
    if settings.LEAD_MODEL_DAILY_BUDGET <= 0:
        return None
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        # A short OS lock protects all local web/worker processes, including SQLite.
        # Release it after the reservation commits, before any paid HTTP request.
        with FileLock(str(settings.DATA_DIR / "model-budget.lock"), timeout=5):
            with transaction.atomic():
                requested_at = timezone.now()
                today = timezone.localdate(requested_at)
                if ModelCall.objects.filter(requested_at__date=today).count() >= settings.LEAD_MODEL_DAILY_BUDGET:
                    return None
                return ModelCall.objects.create(requested_at=requested_at)
    except Timeout:
        return None

def ingest_item(source, item):
    from radar.extraction import extract_event, parse_explicit_dates, qualify_event, recruitment_signal_event
    from radar.llm import enrich_with_model

    title = str(item.get("title", "")).strip()[:600]
    text = str(item.get("text", "")).strip()[:350000]
    external_id = str(item.get("external_id", "")).strip()[:160]
    if not title or not text or not external_id:
        raise ValueError("来源条目缺少标题、正文或稳定编号，不能记为有效采集。")
    url = str(item.get("url", ""))[:2000]
    if url and urlparse(url).scheme not in {"http", "https"}:
        raise ValueError("原文链接必须是 HTTP(S)。")
    # Only user actions may establish manual_* overrides. Collector metadata is data.
    metadata = {
        str(key): value for key, value in dict(item.get("metadata") or {}).items()
        if not str(key).startswith(("manual_", "_"))
    }
    employer = None
    is_recruitment = source.key in {"japan_directory_yingchuang", "japan_directory_mol_recruitment"} or source.kind == "recruitment"
    if source.key in {"japan_directory_yingchuang", "japan_directory_mol_recruitment"}:
        name = str(metadata.get("employer_name") or "")
        employer = Company.objects.filter(name=name, subscribed=True).first()
        quote = metadata.get("position_name") if source.key == "japan_directory_mol_recruitment" else name
        valid = employer and metadata.get("signal_type") == "job" and any(
            isinstance(e, dict) and e.get("source") == source.key and e.get("url") == url
            and e.get("quote") == quote and e.get("document_hash") == metadata.get("directory_detail_hash")
            and e.get("status") == "japan_affiliation_unverified"
            for e in employer.discovery_evidence or []
        )
        if valid and source.key == "japan_directory_mol_recruitment":
            from .mol_recruitment import ABOUT
            valid = any(isinstance(e, dict) and e.get("source") == source.key and e.get("url") == ABOUT
                        and e.get("quote") == name and e.get("status") == "japan_affiliation_unverified"
                        for e in employer.discovery_evidence or [])
        if not valid:
            raise ValueError("unverified_recruitment_employer")
    if source.key in PUBLIC_SOURCES and is_recruitment:
        name = str(metadata.get("employer_name") or "")
        if name and name in text and not re.search(r"某|匿名|保密|知名", name):
            employer = Company.objects.get_or_create(name=name[:240])[0]
    source_kind = "early_signal" if is_recruitment else source.kind
    access = item.get("access_level", "public")
    attachments = list(item.get("attachments") or [])
    # A site's unrelated page chrome can change while an individual job does
    # not. Keep the full-page hash as evidence, but not as a job-content version.
    digest_metadata = dict(metadata)
    if is_recruitment:
        digest_metadata.pop("matched_queries", None)
    if source.key == "japan_directory_mol_recruitment":
        digest_metadata.pop("directory_detail_hash", None)
    digest = _digest(
        title, text, url=url, source_kind=source_kind, published_date=item.get("published_date"),
        listed_deadline=item.get("listed_deadline"), access_level=access,
        attachments=attachments, metadata=digest_metadata,
    )
    now = timezone.now()
    existing = Lead.objects.filter(source=source, external_id=external_id).first()
    if existing and existing.content_hash == digest:
        if is_recruitment:
            old = dict(existing.metadata or {})
            old["matched_queries"] = sorted(set(old.get("matched_queries",[]) + metadata.get("matched_queries",[])))
            existing.metadata = old
            existing.save(update_fields=["metadata"])
        existing.last_seen_at = now
        existing.save(update_fields=["last_seen_at"])
        return existing, False, False

    # Production extraction uses the actual clock; a date-only as_of loses same-day expiry.
    event = extract_event(title, text, source_kind=source_kind, listed_deadline=item.get("listed_deadline"))

    def normalize_deadline(value):
        """Return an ISO fact and an aware instant only when a time was actually stated."""
        if not value:
            return None, None
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            return value.isoformat(), None
        else:
            raw = str(value).strip()
            try:
                return date.fromisoformat(raw).isoformat(), None
            except (TypeError, ValueError):
                pass
            try:
                # Require explicit hour/minute before accepting ISO datetime precision.
                if not re.search(r"[T ]\d{1,2}:\d{2}", raw):
                    raise ValueError("no explicit time")
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                choices = parse_explicit_dates(raw)
                if len(choices) != 1:
                    return None, None
                value, has_time, _ = choices[0]
                if not has_time:
                    return value, None
                parsed = datetime.fromisoformat(value)
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.get_default_timezone())
        parsed = timezone.localtime(parsed, timezone.get_default_timezone())
        return parsed.isoformat(), parsed

    original_status = event.get("processing_status")
    original_summary = event.get("summary", "")
    value, deadline_at = normalize_deadline(event.get("deadline"))
    event["deadline"] = value
    event["deadline_has_time"] = deadline_at is not None
    deadline_conflict = False
    registration_override = False
    if metadata.get("deadline_kind") == "registration":
        declared, declared_at = normalize_deadline(item.get("listed_deadline"))
        raw_declared, raw_at = normalize_deadline(metadata.get("deadline_raw"))
        evidence_quote = str(item.get("listed_deadline") or "")
        if not declared and raw_declared:
            declared, declared_at = raw_declared, raw_at
            evidence_quote = str(metadata["deadline_raw"])
        elif declared and raw_declared:
            if _iso_date(declared) != _iso_date(raw_declared):
                deadline_conflict = True
            elif raw_at is not None:
                # The list parser may have normalized its date while preserving raw time.
                declared, declared_at = raw_declared, raw_at
                evidence_quote = str(metadata["deadline_raw"])
        if declared:
            if event.get("deadline_kind") == "registration" and value:
                if _iso_date(value) != _iso_date(declared):
                    deadline_conflict = True
                elif declared_at is None and deadline_at is not None:
                    # A date-only listing must not erase a same-day time from the notice.
                    declared, declared_at = value, deadline_at
                elif declared_at is not None and deadline_at is not None and declared_at != deadline_at:
                    deadline_conflict = True
            event.update(deadline=declared, deadline_kind="registration", deadline_has_time=declared_at is not None)
            deadline_at = declared_at
            registration_override = True
            # These fields are explicitly labelled collector metadata, never fake body quotes.
            for evidence in [
                {"field": "listed_deadline", "quote": evidence_quote[:160]},
                {"field": "listed_deadline_kind", "quote": "registration"},
            ]:
                if evidence not in event["evidence"]:
                    event["evidence"].append(evidence)
    event = qualify_event(event)
    # Retain the model adapter's conservative historical-state guard unless an explicit
    # collector registration fact changed the relevant deadline.
    if original_status == "closed" and not registration_override and event["event_type"] not in {"early_signal", "irrelevant"}:
        event["processing_status"] = "closed"
        event["summary"] = original_summary

    missing = list(event.get("missing_fields", []))
    if deadline_conflict:
        missing.append("deadline_conflict")
        if event["processing_status"] == "actionable":
            event["processing_status"] = "needs_review"
        event["summary"] += " 正文与列表的报名截止不一致，需核对原公告。"
    if access != "public":
        if "full_text_or_attachment_access" not in missing:
            missing.append("full_text_or_attachment_access")
        if event.get("processing_status") == "actionable":
            event["processing_status"] = "needs_review"
        event["summary"] += " 当前全文或附件访问不完整，参与条件仍待核实。"

    if metadata.get("invitation_required"):
        if event.get("processing_status") == "actionable":event["processing_status"] = "needs_review"
        missing.append("invitation_required")
    if is_recruitment:
        recruitment_signal_event(event, published=metadata.get("posting_date_status") == "source_stated")
        deadline_at = None
    buyer = event.get("buyer_name")
    if buyer:
        buyer = re.sub(r"\s+", " ", str(buyer)).strip()[:240]

    raw_rel = ""
    html = item.get("html")
    if html:
        folder = settings.DATA_DIR / "raw" / source.key
        folder.mkdir(parents=True, exist_ok=True)
        raw_file = folder / f"{digest}.html"
        if not raw_file.exists():
            raw_file.write_text(str(html), encoding="utf-8")
        raw_rel = raw_file.relative_to(settings.DATA_DIR).as_posix()
    from radar.entities import subject_reason
    fields = {
        "subject_reason": ("source_redacted" if metadata.get("employer_display") else "parser_missed") if is_recruitment and not employer else subject_reason(event, text, access),
        "deadline_semantics": event.get("deadline_semantics", "unknown"),
        "title": title, "url": url, "summary": event.get("summary", ""),
        "buyer_role": event.get("buyer_role", "unknown"), "event_type": event.get("event_type", "unknown"),
        "processing_status": event.get("processing_status", "needs_review"),
        "service_types": event.get("service_types", []), "regions": event.get("regions", []),
        "deadline": _iso_date(event.get("deadline")), "deadline_at": deadline_at,
        "deadline_kind": event.get("deadline_kind", "unknown"),
        "deadline_has_time": deadline_at is not None,
        "published_date": _iso_date(item.get("published_date")), "access_level": access,
        "missing_fields": sorted(set(missing)), "evidence": event.get("evidence", []),
        "qualifications": event.get("qualifications", []), "participation_path": event.get("participation_path") or "",
        "service_period": event.get("service_period") or "", "text": text, "content_hash": digest,
        "extraction_method": event.get("extraction_method", "rules"), "model_warning": event.get("model_warning") or "",
        "last_seen_at": now,
    }
    # Serialize only local ingestion writes. The HTTP call is already complete. Row locking
    # also coordinates with PostgreSQL writers; explicit update_fields excludes sales reviews.
    with FileLock(str(settings.DATA_DIR / "ingest-write.lock"), timeout=30), transaction.atomic():
        current = Lead.objects.select_for_update().filter(source=source, external_id=external_id).first()
        if current and current.content_hash == digest:
            current.last_seen_at = now
            current.save(update_fields=["last_seen_at"])
            return current, False, False
        current_metadata = dict(current.metadata or {}) if current else {}
        # Remove prior source-owned keys so a fact removed upstream does not linger. Other
        # metadata, including the latest human additions, is merged from the fresh row.
        merged_metadata = {
            key: value for key, value in current_metadata.items()
            if key not in current_metadata.get("_source_metadata_keys", [])
            and key not in {"attachments", "_source_metadata_keys"}
        }
        if is_recruitment:
            metadata["matched_queries"] = sorted(set(metadata.get("matched_queries", []) + current_metadata.get("matched_queries", [])))
        merged_metadata.update(metadata)
        merged_metadata.update({key: value for key, value in current_metadata.items() if key.startswith("manual_")})
        merged_metadata["attachments"] = attachments
        merged_metadata["_source_metadata_keys"] = sorted(metadata)
        fields["metadata"] = merged_metadata
        if current and current_metadata.get("manual_buyer"):
            fields["company"] = current.company
        elif is_recruitment:
            fields["company"] = employer
        else:
            fields["company"] = Company.objects.get_or_create(name=buyer)[0] if buyer else None
        if current:
            for key, value in fields.items():
                setattr(current, key, value)
            current.version += 1
            current.save(update_fields=[*fields, "version"])
            lead, created = current, False
        else:
            # The semantic hash contains source metadata/URL; exact text copies may still
            # arrive through a different source and should remain visibly linked.
            duplicate = Lead.objects.filter(title=title, text=text, duplicate_of__isnull=True,
                event_type=fields['event_type'],published_date=fields['published_date'],deadline=fields['deadline']).first()
            lead = Lead.objects.create(source=source, external_id=external_id, duplicate_of=duplicate, **fields)
            created = True
        document_metadata = {
            **metadata, "attachments": attachments, "listed_deadline": item.get("listed_deadline"),
            "access_level": access, "published_date": item.get("published_date"), "url": url,
        }
        doc,_=Document.objects.get_or_create(lead=lead, content_hash=digest, defaults={"text": text, "raw_path": raw_rel, "metadata": document_metadata})
        if is_recruitment:
            from .models import EntityCandidate
            for entity in metadata.get('mentioned_organizations',[]):
                if not isinstance(entity,dict):continue
                name=str(entity.get('name') or '')[:240];quote=str(entity.get('quote') or '')
                if not name or not quote or quote not in text:continue
                line=next((i for i,value in enumerate(text.splitlines(),1) if quote in value),None)
                EntityCandidate.objects.get_or_create(lead=lead,name=name,role='mentioned_research',document_hash=digest,
                    defaults={'source_url':url,'status':'research_only','evidence':{'quote':quote,'document_id':doc.pk,
                        'block_id':f'd{doc.pk}:b{line}' if line else '',
                        'note':'正文提及的关联组织，供研究；并非已确认的招聘方或采购人。'}})
    from radar.ai_config import config
    cfg = config()
    if created and cfg.enabled and cfg.auto_new and not lead.is_test and not source.key.startswith("manual"):
        import uuid
        from radar.analysis import enqueue
        enqueue(lead.pk,"analysis",str(uuid.uuid4()))
    return lead, created, not created

def import_text(title, text, url="", source_kind="procurement"):
    if source_kind not in {"procurement", "early_signal"}:
        raise ValueError("不支持的来源类型。")
    source, _ = Source.objects.get_or_create(key="manual_" + source_kind, defaults={
        "name": "原文导入" if source_kind == "procurement" else "早期信号导入", "url": "", "kind": source_kind,
        "enabled": False, "status": "manual", "note": "由使用者粘贴的原文；保留链接供人工核实。",
    })
    key = hashlib.sha256((url.strip() if url else title.strip() + "\n" + text.strip()).encode("utf-8")).hexdigest()
    lead, _, _ = ingest_item(source, {"external_id": key, "title": title, "text": text, "url": url,
        "access_level": "public", "kind": source_kind, "metadata": {"input_method": "user_pasted_text"}})
    return lead

def _collect_free_group(source):
    from collectors import source_cards
    if source.key in ON_DEMAND:
        return source_cards.collect(source)
    from collectors.china_entry_sources import collect_group
    from collectors.candidate_state import bounded

    maximum=bounded(source.options.get('max_items'),5,1,12)
    collected=collect_group(source.key,max_details=maximum,record_run=False)
    errors=collected['errors']
    read_any=bool(collected['requests'])
    skipped = {'mainland_company_and_finance_evidence_missing', 'no_relevant_action', 'noise_or_promotion'}
    failures = [e for e in errors if e.get('status') != 'duplicate_event' and e['reason'] not in skipped]
    blocked=failures and all(e.get('status')=='blocked_by_robots' for e in failures)
    status='partial' if failures and read_any else 'blocked' if blocked else 'failed' if failures else 'success'
    return {'items':[],'status':status,'next_cursor':1,'collector_checkpoint':collected['checkpoint'],
            'error': '；'.join(dict.fromkeys(e['reason'] for e in failures))[:1400],
            'stats':{'error_count':len(failures),'skips_and_errors':errors,'details_admitted':collected['retained'],
                     'candidates_new':len(collected['created']),'confirmed_public_read':read_any,
                     'endpoints':collected['endpoints'],'requests':collected['requests'],'details_read':collected.get('details_read',[])}}


def _execute_collector(source):
    if source.key in PUBLIC_SOURCES or source.key in DIRECTORIES or source.key in RESEARCH_EXECUTABLE or source.key in FREE_GROUPS:
        from .public_fetch import request_budget
        from collectors.candidate_state import bounded
        options=source.options or {}
        with request_budget(bounded(options.get('max_total_requests'),80,1,500),
                            bounded(options.get('max_elapsed_seconds'),settings.LEAD_CRAWL_TIMEOUT,1,600)) as budget:
            if source.key in FREE_GROUPS:
                result=_collect_free_group(source)
            elif source.key in RESEARCH_EXECUTABLE:
                from importlib import import_module
                from collectors.japan_channels import CHANNELS
                from collectors.press_intake import SITES as PRESS_INTAKE
                module,name = {**dict.fromkeys(PRESS_INTAKE, ('press_intake','collect_press')), **dict.fromkeys(CHANNELS, ('japan_channels','collect_channels')), 'idec_overseas_cases':('idec_cases','collect_idec'), 'kanto_verified_cases':('kanto_cases','collect_kanto'), 'sh_market_disclosures':('market_disclosures','collect_disclosures'),
                               'fbc_ptc_manufacturers':('shanghai_exhibitors','collect_exhibitors'),
                               'fhc_shanghai_japan':('shanghai_exhibitors','collect_exhibitors'),
                               'osaka_ciie_roster':('shanghai_exhibitors','collect_exhibitors'),
                               'jbic_sme_china':('jbic_sme','collect_jbic'),
                               'osaka_sme_china_sites':('osaka_source','collect_osaka'),
                               'maff_export_plans':('maff_source','collect_maff'),
                               'china_zones_japan_projects':('zone_events','collect_zone')}[source.key]
                collector = getattr(import_module('collectors.'+module),name)
                result=collector(source)
            elif source.key in PUBLIC_SOURCES:
                from collectors.japanese_public import collect_dfmc, collect_jobs, collect_expansion
                from collectors.dfwlg import collect_dfwlg
                collector = {"dfmc_nissan":collect_dfmc,"persol_jobs":collect_jobs,"gz_japan_expansion":collect_expansion,"dfwlg_procurement":collect_dfwlg}[source.key]
                result=collector(source)
            else:
                from .discovery import discover
                result=discover(source, limit=max(1, min(50, int(source.options.get("max_items", source.options.get("directory_limit", 50))))))
            result.setdefault('stats',{})['public_requests_used']=budget['used']
            result['stats']['public_requests_limit']=budget['limit']
            return result, 1, []
    from django.utils.dateparse import parse_datetime

    head_pages = max(1,min(30,int(source.options.get("head_pages",settings.LEAD_HEAD_PAGES))))
    max_items = max(1,min(50,int(source.options.get("max_items",settings.LEAD_MAX_ITEMS))))
    checkpoint = dict(source.checkpoint or {})
    cursor = max(1, int(checkpoint.get("cursor", 1)))
    seen = list(checkpoint.get("cycle_seen", []))
    now = timezone.now()
    started = parse_datetime(str(checkpoint.get("cycle_started_at", "")))
    if started and timezone.is_naive(started):
        started = timezone.make_aware(started)
    # Fresh announcements must never wait for thousands of archive pages. Each
    # cycle covers an explicit head window, with a maximum age before refreshing.
    refresh_reason = ""
    if cursor > head_pages:
        refresh_reason = "outside_head_window"
    elif started and now - started >= timedelta(minutes=max(30, source.interval_minutes)):
        refresh_reason = "head_refresh_due"
    if refresh_reason:
        cursor, seen, started = 1, [], now
    started = started or now
    pages = min(settings.LEAD_MAX_PAGES, head_pages - cursor + 1)
    with tempfile.TemporaryDirectory(prefix="lead-collector-") as temp:
        output = Path(temp) / "result.json"
        seen_file = Path(temp) / "seen.json"
        seen_file.write_text(json.dumps(seen), encoding="utf-8")
        args = [sys.executable, "-m", "collectors.cli", "--source", source.key, "--output", str(output),
                "--cursor", str(cursor), "--max-pages", str(pages),
                "--max-items", str(max_items), "--seen-file", str(seen_file)]
        proc = subprocess.run(args, cwd=settings.BASE_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=settings.LEAD_CRAWL_TIMEOUT)
        if not output.exists():
            raise RuntimeError("采集器未返回有效结果。" + (proc.stderr[-1200:] if proc.stderr else f" exit={proc.returncode}"))
        result = json.loads(output.read_text(encoding="utf-8"))
        if result.get("status") not in {"success", "partial", "blocked", "failed"}:
            raise ValueError("采集器状态格式无效。")
        if not isinstance(result.get("items"), list):
            raise ValueError("采集器条目格式无效。")
        result.setdefault("stats", {}).update({
            "head_cycle_started_at": started.isoformat(),
            "head_refresh_reason": refresh_reason,
            "head_pages_limit": head_pages,
        })
        return result, cursor, seen

def process_run(run_id, *, max_pages=None):
    run = SourceRun.objects.select_related("source").get(pk=run_id)
    mode, reason = source_mode(run.source.key)
    if mode != "A":
        SourceRun.objects.filter(pk=run.pk, status="queued").update(
            status="blocked", finished_at=timezone.now(), error=reason)
        run.refresh_from_db()
        return run
    if not SourceRun.objects.filter(pk=run.pk, status="queued").update(status="running", started_at=timezone.now()):
        return run
    run.refresh_from_db()
    source = run.source
    source._max_pages = max_pages
    head_pages=max(1,min(30,int(source.options.get("head_pages",settings.LEAD_HEAD_PAGES))))
    source.status = "running"
    source.save(update_fields=["status"])
    checkpoint_started = dict(source.checkpoint or {})
    try:
        if source.key == "japan_directory_yingchuang":
            # Publish the checkpoint only after every job signal has been ingested.
            # A process crash before that point replays the same idempotent batch.
            source._defer_directory_checkpoint = True
        result, cursor, seen = _execute_collector(source)
        run.stats = dict(result.get("stats", {}))
        run.stats.update({"cursor_started": cursor, "next_cursor": result.get("next_cursor"),
                          "head_pages_limit": head_pages})
        run.discovered_count = run.stats.get("details_admitted",len(result["items"]))
        run.new_count = run.stats.get("candidates_new",0)
        if result.get('intake') or result['items']:
            from collectors.wide_intake import store_candidate
            candidates = result.get('intake', result['items'])
            run.stats['intake_total'] = 0
            run.stats['intake_new'] = 0
            for candidate in candidates:
                if not candidate.get('url') or not candidate.get('title') or not candidate.get('text'):
                    continue
                run.stats['intake_total'] += 1
                run.stats['intake_new'] += store_candidate(url=candidate['url'], title=candidate['title'],
                    summary=candidate['text'], text=candidate['text'], provider=source.key,
                    query_id='direct:'+source.key, published=str(candidate.get('published_date') or ''),
                    document=candidate.get('document'), discovery={**candidate.get('intake_metadata',{}),
                        'historical_case':(candidate.get('research') or {}).get('event_type') == 'existing_presence'})
        for item in result["items"]:
            if source.key in RESEARCH_EXECUTABLE:
                from .research import ingest_research_record
                _, created, updated = ingest_research_record(source,item)
            else:
                _, created, updated = ingest_item(source, item)
            run.new_count += int(created)
            run.updated_count += int(updated)
        from .dedup import link_duplicates
        run.stats["duplicates_linked"] = [] if source.kind == "enterprise_discovery" or source.key in RESEARCH_EXECUTABLE or source.key in FREE_GROUPS else link_duplicates()
        error_count = int(run.stats.get("error_count", 0))
        status = result["status"]
        if status == "success" and not run.new_count and not run.updated_count and not run.stats.get("companies_new") and not run.stats.get("intake_new"):
            status = "success_no_new"
        run.status = status
        run.error = str(result.get("error") or "")[:2000]
        # Advance only after all items and their evidence were saved.
        if result["status"] in {"success", "partial"}:
            if source.key == "japan_directory_yingchuang":
                source.checkpoint = {**source.checkpoint, "directory_offset": run.stats["next_directory_offset"]}
            seen = list(dict.fromkeys(seen + [str(x["external_id"]) for x in result["items"]]))
            next_cursor = int(result.get("next_cursor") or 1)
            head_complete = next_cursor > head_pages
            if result["status"] == "success" or not result.get("next_cursor") or head_complete:
                source.checkpoint = {**(source.checkpoint if source.key in DIRECTORIES or source.key in PUBLIC_SOURCES or source.key in RESEARCH_EXECUTABLE or source.key in FREE_GROUPS else {}),"cursor": 1, "cycle_seen": [], "cycle_completed_at": timezone.now().isoformat()}
                source.next_run_at = timezone.now() + timedelta(minutes=source.interval_minutes)
                if head_complete:
                    run.stats.update({"head_window_complete": True, "coverage_complete": False,
                                      "coverage_note": f"已检查前 {head_pages} 页；历史页不在本轮范围内。"})
                    if not error_count:
                        run.status = "success" if run.new_count or run.updated_count else "success_no_new"
            else:
                source.checkpoint = {**(source.checkpoint if source.key in PUBLIC_SOURCES or source.key in RESEARCH_EXECUTABLE or source.key in FREE_GROUPS else {}),"cursor": next_cursor, "cycle_seen": seen,
                                     "cycle_started_at": run.stats.get("head_cycle_started_at") or source.checkpoint.get("cycle_started_at") or timezone.now().isoformat(),
                                     "updated_at": timezone.now().isoformat()}
                source.next_run_at = timezone.now() + timedelta(minutes=5)
            if result.get("collector_checkpoint"):
                source.checkpoint.update(result["collector_checkpoint"])
            if source.key in DIRECTORIES:
                from .seed_dictionary import export_dictionary
                export_dictionary()
            if run.stats.get("confirmed_public_read"):
                source.last_success_at=timezone.now()
            if error_count:
                run.stats["partial_reason"] = "errors"
                source.next_run_at = timezone.now() + timedelta(minutes=60)
            else:
                run.stats["partial_reason"] = "budget" if run.status == "partial" else ""
                source.last_success_at = timezone.now()
        else:
            source.next_run_at = timezone.now() + timedelta(minutes=60)
    except subprocess.TimeoutExpired:
        run.status, run.error = "failed", "本轮超过采集时间预算，检查点未推进；下次可继续。"
        source.next_run_at = timezone.now() + timedelta(minutes=30)
    except Exception as exc:
        run.status, run.error = "failed", f"{type(exc).__name__}: {str(exc)[:1400]}"
        source.next_run_at = timezone.now() + timedelta(minutes=30)
    if run.status in {"failed", "blocked"} and (source.key in DIRECTORIES or source.key in RESEARCH_EXECUTABLE or source.key in FREE_GROUPS):
        source.checkpoint = checkpoint_started
    run.finished_at = timezone.now()
    run.save()
    source.status = run.status
    source.save(update_fields=["checkpoint", "last_success_at", "next_run_at", "status"])
    return run

def _recover_running_sources(runs):
    source_ids=list(runs.values_list("source_id",flat=True))
    runs.update(status="failed", finished_at=timezone.now(),
                error="上次采集中断，进度已保留；可再次采集。")
    Source.objects.filter(pk__in=source_ids).update(status="failed")


def process_pending(max_runs=1, prefect_run_id="", *, target_run_id=None, max_pages=None):
    lock = FileLock(str(settings.DATA_DIR / "worker.lock"), timeout=0)
    try:
        with lock:
            now = timezone.now()
            # Holding the OS lock proves there is no other project worker processing a job.
            _recover_running_sources(SourceRun.objects.filter(status="running"))
            if automatic_enabled():
                for source in Source.objects.filter(enabled=True).exclude(key__startswith="manual"):
                    if not schedulable(source.key):
                        continue
                    if not source.next_run_at or source.next_run_at <= now:
                        enqueue_source(source.pk)
            # Expiry is business state, and must update even without a fresh source article.
            eligible = Lead.objects.filter(processing_status__in=["actionable", "needs_review"])
            eligible.filter(deadline_at__lte=now).update(processing_status="closed")
            eligible.filter(deadline_at__isnull=True, deadline__lt=timezone.localdate()).update(processing_status="closed")
            queued = SourceRun.objects.filter(status="queued")
            if target_run_id is not None:
                queued = queued.filter(pk=target_run_id)
            ids = list(queued.order_by("requested_at").values_list("id", flat=True)[:max_runs])
            if ids and prefect_run_id:
                SourceRun.objects.filter(pk__in=ids, status="queued").update(prefect_run_id=prefect_run_id)
            done = [process_run(pk, max_pages=max_pages) if max_pages else process_run(pk) for pk in ids]
            RuntimeState.objects.update_or_create(key="last_dispatch", defaults={"value": {"at": timezone.now().isoformat(), "processed": len(done)}})
            return [{"run_id": r.pk, "status": r.status, "new": r.new_count, "updated": r.updated_count} for r in done]
    except Timeout:
        return [{"status": "worker_busy"}]

