"""Offline collector/service boundaries, using synthetic source records only."""

import json
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

from django.test import TestCase, override_settings

from radar.models import Lead, Source, SourceRun
from radar.services import _execute_collector, process_run


def synthetic_item(identifier="900001"):
    return {
        "external_id": identifier,
        "url": f"https://www.ysczw.com/index.php?m=goods&a=details&content_id={identifier}",
        "title": "合成示例：仓储运输服务公开招标",
        "text": "合成示例：仓储运输服务公开招标\n报名开始日期：2030-01-01\n报名截止日期：2030-01-20\n公开摘要，采购主体与具体参加方式待核实。",
        "html": "<html><body>合成测试，不是网站原文。</body></html>",
        "published_date": None,
        "listed_deadline": "2030-01-20",
        "kind": "procurement",
        "access_level": "partial",
        "attachments": [],
        "metadata": {
            "registration_start_date": "2030-01-01",
            "registration_start_raw": "2030-01-01",
            "deadline_kind": "registration",
            "deadline_raw": "2030-01-20",
            "source_updated_date": "2029-12-31",
            "body_access": "member_only",
        },
    }


def synthetic_result(items=(), *, status="partial", cursor=1, errors=0):
    return {
        "source_key": "ysczw",
        "status": status,
        "items": list(items),
        "next_cursor": cursor,
        "stats": {
            "error_count": errors,
            "budget_limited": status == "partial" and not errors,
            "coverage_complete": status == "success",
            "request_count": len(items) + 2,
        },
        "error": "synthetic access failure" if errors else "",
    }


class CollectorIntegrationContractTests(TestCase):
    def setUp(self):
        self.data = tempfile.TemporaryDirectory(prefix="collector-contract-")
        self.addCleanup(self.data.cleanup)
        self.settings_override = override_settings(
            DATA_DIR=Path(self.data.name), LEAD_MODEL_ENABLED=False,
            LEAD_MAX_PAGES=1, LEAD_MAX_ITEMS=5, LEAD_CRAWL_TIMEOUT=60,
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.source = Source.objects.create(
            key="ysczw", name="合成测试来源", kind="procurement",
            url="https://www.ysczw.com/", checkpoint={"cursor": 1, "cycle_seen": ["900000"]},
        )

    def run_result(self, result):
        self.source.refresh_from_db()
        cursor = self.source.checkpoint.get("cursor", 1)
        seen = self.source.checkpoint.get("cycle_seen", [])
        run = SourceRun.objects.create(source=self.source)
        with patch("radar.services._execute_collector", return_value=(result, cursor, seen)):
            completed = process_run(run.pk)
        self.source.refresh_from_db()
        return completed

    def test_subprocess_receives_numeric_seen_ids_and_checkpoint(self):
        self.source.checkpoint = {"cursor": 2, "cycle_seen": ["900000", "900001"]}
        payload = synthetic_result(cursor=3)

        def fake_subprocess(args, **kwargs):
            self.assertEqual(args[1:3], ["-m", "collectors.cli"])
            self.assertEqual(args[args.index("--source") + 1], "ysczw")
            self.assertEqual(args[args.index("--cursor") + 1], "2")
            seen_path = Path(args[args.index("--seen-file") + 1])
            self.assertEqual(json.loads(seen_path.read_text()), ["900000", "900001"])
            output = Path(args[args.index("--output") + 1])
            output.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(kwargs["timeout"], 60)
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch("radar.services.subprocess.run", side_effect=fake_subprocess):
            result, cursor, seen = _execute_collector(self.source)
        self.assertEqual((cursor, seen), (2, ["900000", "900001"]))
        self.assertEqual(result["items"], payload["items"])
        self.assertEqual(result["next_cursor"], 3)
        self.assertEqual(result["stats"]["head_pages_limit"], 3)

    def test_budget_partial_persists_item_then_keeps_page_and_seen(self):
        run = self.run_result(synthetic_result([synthetic_item()]))
        self.assertEqual(run.status, "partial")
        self.assertEqual(run.new_count, 1)
        self.assertEqual(self.source.checkpoint["cursor"], 1)
        self.assertEqual(self.source.checkpoint["cycle_seen"], ["900000", "900001"])
        self.assertTrue(run.stats["budget_limited"])
        self.assertFalse(run.stats["coverage_complete"])
        self.assertTrue(Lead.objects.filter(source=self.source, external_id="900001").exists())

    def test_all_seen_page_can_advance_without_false_no_new_completion(self):
        run = self.run_result(synthetic_result([], cursor=2))
        self.assertEqual(run.status, "partial")
        self.assertEqual(run.discovered_count, 0)
        self.assertEqual(self.source.checkpoint["cursor"], 2)
        self.assertEqual(self.source.checkpoint["cycle_seen"], ["900000"])

    def test_blocked_run_preserves_checkpoint_for_retry(self):
        before = dict(self.source.checkpoint)
        run = self.run_result(synthetic_result([], status="blocked", cursor=1, errors=1))
        self.assertEqual(run.status, "blocked")
        self.assertEqual(self.source.checkpoint, before)
        self.assertIsNone(self.source.last_success_at)
        self.assertIn("synthetic access failure", run.error)

    def test_ingest_failure_cannot_advance_past_unsaved_item(self):
        before = dict(self.source.checkpoint)
        bad = synthetic_item("900002")
        bad["text"] = ""
        failed = self.run_result(synthetic_result([synthetic_item(), bad], cursor=2))
        self.assertEqual(failed.status, "failed")
        self.assertEqual(self.source.checkpoint, before)
        self.assertEqual(Lead.objects.filter(source=self.source).count(), 1)
        retried = self.run_result(synthetic_result([synthetic_item()], cursor=1))
        self.assertEqual(retried.status, "partial")
        self.assertEqual(retried.new_count, 0)
        self.assertEqual(Lead.objects.filter(source=self.source).count(), 1)
        self.assertIn("900001", self.source.checkpoint["cycle_seen"])

    def test_complete_scan_resets_cycle_and_can_report_no_new(self):
        run = self.run_result(synthetic_result([], status="success", cursor=1))
        self.assertEqual(run.status, "success_no_new")
        self.assertEqual(self.source.checkpoint["cursor"], 1)
        self.assertEqual(self.source.checkpoint["cycle_seen"], [])
        self.assertIn("cycle_completed_at", self.source.checkpoint)

    def test_registration_and_update_dates_never_become_publication(self):
        self.run_result(synthetic_result([synthetic_item()]))
        lead = Lead.objects.get(source=self.source, external_id="900001")
        self.assertIsNone(lead.published_date)
        self.assertEqual(lead.deadline.isoformat(), "2030-01-20")
        self.assertEqual(lead.deadline_kind, "registration")
        self.assertEqual(lead.metadata["registration_start_date"], "2030-01-01")
        self.assertEqual(lead.metadata["source_updated_date"], "2029-12-31")
        self.assertEqual(lead.access_level, "partial")
        self.assertEqual(lead.processing_status, "needs_review")
        self.assertIn("full_text_or_attachment_access", lead.missing_fields)
