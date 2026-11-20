"""Offline regressions for facts, human review, time precision and paid-call limits."""
import copy
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone as datetime_timezone
from pathlib import Path
from unittest.mock import patch

from django.db import close_old_connections, connections
from django.db.models.query import QuerySet
from django.test import TestCase, TransactionTestCase, override_settings

from radar.extraction import extract_event
from radar.models import Company, Document, Lead, ModelCall, Review, Source
from radar.services import _reserve_model_call, ingest_item


CHINA = datetime_timezone(timedelta(hours=8))
NOW = datetime(2030, 1, 20, 12, 0, tzinfo=CHINA)
MODEL_ENV = {
    "LEAD_MODEL_ENABLED": "true", "LEAD_MODEL_BASE_URL": "https://model.example/v1",
    "LEAD_MODEL_NAME": "offline-test", "LEAD_MODEL_API_KEY": "test-key-never-sent",
}


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


def notice(identifier="notice-1", deadline="2030-01-25 15:00"):
    return {
        "external_id": identifier,
        "title": "运输服务询价公告",
        "text": (
            "采购人：示例材料有限公司\n"
            f"报价截止：{deadline}\n"
            "报价方式：发送至rfq@example.com\n"
            "资格要求：具有道路运输经营许可证。"
        ),
        "url": "https://source.example/notices/" + identifier,
        "access_level": "public", "metadata": {}, "attachments": [],
    }


class IngestionTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="ingest-regression-")
        self.addCleanup(directory.cleanup)
        config = override_settings(DATA_DIR=Path(directory.name), LEAD_MODEL_ENABLED=False)
        config.enable()
        self.addCleanup(config.disable)
        for freezer in [patch("radar.extraction.datetime", FrozenDateTime), patch("radar.services.timezone.now", return_value=NOW)]:
            freezer.start()
            self.addCleanup(freezer.stop)
        self.source = Source.objects.create(key="offline", name="离线测试来源", url="https://source.example/", kind="procurement")

    def test_same_day_time_is_preserved_and_expired_immediately(self):
        with patch("radar.extraction.extract_event", wraps=extract_event) as extractor:
            lead, created, updated = ingest_item(self.source, notice(deadline="2030-01-20 10:00"))
        lead.refresh_from_db()
        self.assertEqual((created, updated), (True, False))
        self.assertNotIn("as_of", extractor.call_args.kwargs)
        self.assertEqual(lead.processing_status, "closed")
        self.assertEqual(lead.deadline.isoformat(), "2030-01-20")
        self.assertEqual(lead.deadline_at, datetime(2030, 1, 20, 10, 0, tzinfo=CHINA))
        self.assertTrue(lead.deadline_has_time)

    def test_date_only_and_future_time_keep_distinct_precision(self):
        date_only, _, _ = ingest_item(self.source, notice("date-only", "2030-01-20"))
        future, _, _ = ingest_item(self.source, notice("future-time", "2030-01-20 14:30"))
        self.assertEqual(date_only.processing_status, "needs_review")
        self.assertIsNone(date_only.deadline_at)
        self.assertFalse(date_only.deadline_has_time)
        self.assertIn("deadline_time_verification", date_only.missing_fields)
        self.assertEqual(future.processing_status, "actionable")
        self.assertEqual(future.deadline_at, datetime(2030, 1, 20, 14, 30, tzinfo=CHINA))

    def test_registration_override_requalifies_instead_of_reusing_bid_time(self):
        item = notice()
        item.update(listed_deadline="2030-01-20", metadata={"deadline_kind": "registration"})
        lead, _, _ = ingest_item(self.source, item)
        self.assertEqual(lead.deadline_kind, "registration")
        self.assertEqual(lead.deadline.isoformat(), "2030-01-20")
        self.assertIsNone(lead.deadline_at)
        self.assertFalse(lead.deadline_has_time)
        self.assertEqual(lead.processing_status, "needs_review")
        self.assertIn("deadline_time_verification", lead.missing_fields)
        self.assertIn({"field": "listed_deadline", "quote": "2030-01-20"}, lead.evidence)
        self.assertIn({"field": "listed_deadline_kind", "quote": "registration"}, lead.evidence)

    def test_registration_time_in_raw_list_cell_is_not_lost(self):
        item = notice()
        item.update(listed_deadline="2030-01-20", metadata={
            "deadline_kind": "registration", "deadline_raw": "2030年1月20日 10:15",
        })
        lead, _, _ = ingest_item(self.source, item)
        self.assertEqual(lead.processing_status, "needs_review")
        self.assertIn("registration_closed", lead.missing_fields)
        self.assertEqual(lead.deadline_at, datetime(2030, 1, 20, 10, 15, tzinfo=CHINA))
        self.assertTrue(lead.deadline_has_time)

    def test_date_listing_preserves_same_day_registration_time_from_body(self):
        item = notice()
        item["text"] += "\n报名截止：2030-01-21 09:30"
        item.update(listed_deadline="2030-01-21", metadata={"deadline_kind": "registration"})
        lead, _, _ = ingest_item(self.source, item)
        self.assertEqual(lead.deadline_at, datetime(2030, 1, 21, 9, 30, tzinfo=CHINA))
        self.assertTrue(lead.deadline_has_time)
        self.assertNotIn("deadline_conflict", lead.missing_fields)

    def test_conflicting_registration_dates_stay_pending_with_both_evidence(self):
        item = notice()
        item["text"] += "\n报名截止：2030-01-21 09:30"
        item.update(listed_deadline="2030-01-22", metadata={"deadline_kind": "registration"})
        lead, _, _ = ingest_item(self.source, item)
        self.assertEqual(lead.deadline.isoformat(), "2030-01-22")
        self.assertIsNone(lead.deadline_at)
        self.assertEqual(lead.processing_status, "needs_review")
        self.assertIn("deadline_conflict", lead.missing_fields)
        self.assertTrue(any("2030-01-21" in x["quote"] for x in lead.evidence))
        self.assertTrue(any("2030-01-22" in x["quote"] for x in lead.evidence))

    def test_early_signal_cannot_be_promoted_by_registration_metadata(self):
        self.source.kind = "early_signal"
        self.source.save(update_fields=["kind"])
        item = notice()
        item.update(listed_deadline="2030-01-19 11:00", metadata={"deadline_kind": "registration"})
        lead, _, _ = ingest_item(self.source, item)
        self.assertEqual(lead.processing_status, "early_signal")
        self.assertEqual(lead.event_type, "early_signal")

    def test_access_only_change_updates_without_changing_body(self):
        item = notice()
        item["access_level"] = "partial"
        first, _, _ = ingest_item(self.source, item)
        self.assertEqual(first.processing_status, "needs_review")
        item["access_level"] = "public"
        changed, created, updated = ingest_item(self.source, item)
        self.assertEqual((created, updated), (False, True))
        self.assertEqual(changed.version, 2)
        self.assertEqual(changed.processing_status, "actionable")
        self.assertNotIn("full_text_or_attachment_access", changed.missing_fields)
        self.assertEqual(Document.objects.filter(lead=changed).count(), 2)

    def test_listing_deadline_only_change_reclassifies_same_body(self):
        item = notice()
        item.update(listed_deadline="2030-01-21", metadata={"deadline_kind": "registration"})
        first, _, _ = ingest_item(self.source, item)
        self.assertEqual(first.processing_status, "actionable")
        item["listed_deadline"] = "2030-01-19"
        changed, _, updated = ingest_item(self.source, item)
        self.assertTrue(updated)
        self.assertEqual(changed.version, 2)
        self.assertEqual(changed.processing_status, "needs_review")
        self.assertIn("registration_closed", changed.missing_fields)
        self.assertEqual(changed.deadline.isoformat(), "2030-01-19")

    def test_attachment_and_stable_metadata_changes_create_versions(self):
        item = notice()
        item["metadata"] = {"industry": "材料", "region": "苏州"}
        lead, _, _ = ingest_item(self.source, item)
        item["attachments"] = [{"url": "https://source.example/spec.pdf", "label": "运输要求"}]
        lead, _, updated = ingest_item(self.source, item)
        self.assertTrue(updated)
        self.assertEqual(lead.version, 2)
        self.assertEqual(lead.metadata["attachments"], item["attachments"])
        item["metadata"] = {"industry": "化工"}
        lead, _, updated = ingest_item(self.source, item)
        self.assertTrue(updated)
        self.assertEqual(lead.version, 3)
        self.assertEqual(lead.metadata["industry"], "化工")
        self.assertNotIn("region", lead.metadata)

    def test_page_fetch_time_and_attachment_order_do_not_manufacture_versions(self):
        item = notice()
        item["metadata"] = {"list_page": 1, "fetched_at": "2030-01-20T01:00:00Z", "industry": "材料", "trace": {"page": 1, "request_id": "first"}}
        item["attachments"] = [
            {"url": "https://source.example/a.pdf", "label": "甲"},
            {"url": "https://source.example/b.pdf", "label": "乙"},
        ]
        first, _, _ = ingest_item(self.source, item)
        item["metadata"].update(list_page=9, fetched_at="2030-01-21T01:00:00Z", trace={"page": 9, "request_id": "second"})
        item["attachments"].reverse()
        with patch("radar.services._reserve_model_call") as reserve:
            repeated, created, updated = ingest_item(self.source, item)
        self.assertEqual((created, updated), (False, False))
        self.assertEqual(repeated.content_hash, first.content_hash)
        self.assertEqual(repeated.version, 1)
        self.assertEqual(Document.objects.filter(lead=first).count(), 1)
        reserve.assert_not_called()

    def test_human_changes_during_rule_extraction_survive_automatic_update(self):
        item = notice()
        lead, _, _ = ingest_item(self.source, item)
        manual_company = Company.objects.create(name="人工确认的实际采购企业")
        reservation = ModelCall.objects.create()
        changed = copy.deepcopy(item)
        changed["text"] = changed["text"].replace("示例材料有限公司", "网页新显示的名称")
        changed["metadata"] = {"industry": "材料更新"}

        from radar.extraction import extract_event as original_extract
        def human_action_then_model_result(title, text, **kwargs):
            event = original_extract(title, text, **kwargs)
            current = Lead.objects.get(pk=lead.pk)
            metadata = {**current.metadata, "manual_buyer": True, "manual_note": "原文企业身份已人工核实", "human_annotation": "保留此备注"}
            Lead.objects.filter(pk=lead.pk).update(review_status="contacted", company=manual_company, metadata=metadata)
            Review.objects.create(lead=lead, status="contacted", note="已经按公告途径联系采购方。")
            return event

        with patch("radar.extraction.extract_event", side_effect=human_action_then_model_result):
            updated, _, _ = ingest_item(self.source, changed)
        updated.refresh_from_db()
        self.assertEqual(updated.review_status, "contacted")
        self.assertEqual(updated.company_id, manual_company.pk)
        self.assertTrue(updated.metadata["manual_buyer"])
        self.assertEqual(updated.metadata["human_annotation"], "保留此备注")
        self.assertEqual(updated.metadata["industry"], "材料更新")
        self.assertEqual(updated.version, 2)
        self.assertEqual(updated.reviews.count(), 1)
        self.assertFalse(Company.objects.filter(name="网页新显示的名称").exists())

    def test_automatic_save_never_writes_a_later_human_review_field(self):
        item = notice()
        lead, _, _ = ingest_item(self.source, item)
        original_save = Lead.save

        def review_immediately_before_save(instance, *args, **kwargs):
            if instance.pk == lead.pk and "version" in (kwargs.get("update_fields") or []):
                Lead.objects.filter(pk=lead.pk).update(review_status="accepted")
            return original_save(instance, *args, **kwargs)

        item["text"] += "\n补充要求：需提供车辆清单。"
        with patch.object(Lead, "save", review_immediately_before_save):
            ingest_item(self.source, item)
        lead.refresh_from_db()
        self.assertEqual(lead.review_status, "accepted")

    def test_text_copy_from_another_url_still_links_to_first_notice(self):
        first, _, _ = ingest_item(self.source, notice("first"))
        copied, _, _ = ingest_item(self.source, notice("second"))
        self.assertNotEqual(first.content_hash, copied.content_hash)
        self.assertEqual(copied.duplicate_of_id, first.pk)

    @override_settings(LEAD_MODEL_ENABLED=True, LEAD_MODEL_DAILY_BUDGET=1)
    def test_collection_does_not_call_provider_even_when_legacy_env_enabled(self):
        with patch.dict(os.environ, MODEL_ENV), patch("radar.llm.enrich_with_model") as model:
            first, _, _ = ingest_item(self.source, notice("rule-only"))
        model.assert_not_called()
        self.assertEqual(ModelCall.objects.count(), 0)
        self.assertEqual(first.extraction_method, "rules")

    def test_rule_datetime_stays_precise_after_collection_analysis_split(self):
        item=notice()
        item["text"]="采购人：示例材料有限公司\n报价截止：2030年1月20日14:30\n报价方式：发送至test@example.com"
        lead,_,_=ingest_item(self.source,item)
        self.assertEqual(lead.deadline_at,datetime(2030,1,20,14,30,tzinfo=CHINA))
        self.assertEqual(lead.extraction_method,"rules")


class ModelBudgetConcurrencyTests(TransactionTestCase):
    @override_settings(LEAD_MODEL_ENABLED=True, LEAD_MODEL_DAILY_BUDGET=1)
    def test_parallel_connections_cannot_reserve_the_same_last_budget_slot(self):
        barrier = threading.Barrier(2)
        original_count = QuerySet.count

        def delayed_count(query):
            result = original_count(query)
            if query.model is ModelCall:
                # Widen the old count/create race. The OS lock must span both operations.
                time.sleep(0.08)
            return result

        def reserve():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                record = _reserve_model_call()
                return record.pk if record else None
            finally:
                connections.close_all()

        with tempfile.TemporaryDirectory(prefix="model-budget-") as directory, override_settings(DATA_DIR=Path(directory)), patch.dict(os.environ, MODEL_ENV), patch.object(QuerySet, "count", delayed_count):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(reserve) for _ in range(2)]
                results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(ModelCall.objects.count(), 1)
