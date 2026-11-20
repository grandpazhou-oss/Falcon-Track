"""Workflow regressions: freshness, backoff, operator controls and expiry."""
import json
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from radar.models import Lead, RuntimeState, Source, SourceRun
from radar.services import _execute_collector, enqueue_source, process_pending, process_run


class WorkflowTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        settings = override_settings(DATA_DIR=Path(self.temp.name), LEAD_HEAD_PAGES=3,
                                     LEAD_MAX_PAGES=1, LEAD_MAX_ITEMS=8, LEAD_AUTOMATIC_ENABLED=False)
        settings.enable()
        self.addCleanup(settings.disable)
        self.source = Source.objects.create(key="ysczw", name="合成测试来源", enabled=True, interval_minutes=240)

    def collector_result(self, cursor):
        def fake(args, **kwargs):
            result = {"source_key": "ysczw", "status": "partial", "items": [], "next_cursor": cursor,
                      "stats": {"error_count": 0, "coverage_complete": False}, "error": ""}
            Path(args[args.index("--output") + 1]).write_text(json.dumps(result))
            return subprocess.CompletedProcess(args, 0, "", "")
        return fake

    def test_old_cycle_refreshes_head_instead_of_waiting_for_archive(self):
        self.source.checkpoint = {"cursor": 2, "cycle_seen": ["old"],
                                  "cycle_started_at": (timezone.now() - timedelta(hours=5)).isoformat()}
        with patch("radar.services.subprocess.run", side_effect=self.collector_result(1)):
            result, cursor, seen = _execute_collector(self.source)
        self.assertEqual((cursor, seen), (1, []))
        self.assertEqual(result["stats"]["head_refresh_reason"], "head_refresh_due")

    def test_old_archive_cursor_restarts_with_bounded_coverage(self):
        self.source.checkpoint = {"cursor": 1200, "cycle_seen": ["old"]}
        with patch("radar.services.subprocess.run", side_effect=self.collector_result(1)):
            result, cursor, seen = _execute_collector(self.source)
        self.assertEqual((cursor, seen), (1, []))
        self.assertEqual(result["stats"]["head_pages_limit"], 3)

    def test_finishing_head_window_is_not_reported_as_full_website(self):
        self.source.checkpoint = {"cursor": 3, "cycle_seen": ["done"]}
        self.source.save()
        run = enqueue_source(self.source.pk)
        result = {"status": "partial", "items": [], "next_cursor": 4,
                  "stats": {"error_count": 0, "coverage_complete": False}, "error": ""}
        with patch("radar.services._execute_collector", return_value=(result, 3, ["done"])):
            run = process_run(run.pk)
        self.source.refresh_from_db()
        self.assertEqual(self.source.checkpoint["cursor"], 1)
        self.assertEqual(self.source.checkpoint["cycle_seen"], [])
        self.assertTrue(run.stats["head_window_complete"])
        self.assertFalse(run.stats["coverage_complete"])
        self.assertEqual(run.status, "success_no_new")
        self.assertGreater(self.source.next_run_at, timezone.now() + timedelta(hours=3))

    def test_partial_access_error_preserves_progress_and_backs_off(self):
        run = enqueue_source(self.source.pk)
        result = {"status": "partial", "items": [], "next_cursor": 2,
                  "stats": {"error_count": 1}, "error": "HTTP 429"}
        with patch("radar.services._execute_collector", return_value=(result, 2, ["saved"])):
            run = process_run(run.pk)
        self.source.refresh_from_db()
        self.assertEqual(run.status, "partial")
        self.assertEqual(run.stats["partial_reason"], "errors")
        self.assertEqual(self.source.checkpoint["cycle_seen"], ["saved"])
        self.assertIsNone(self.source.last_success_at)
        self.assertGreater(self.source.next_run_at, timezone.now() + timedelta(minutes=55))

    def test_repeated_clicks_keep_only_one_active_job(self):
        first = enqueue_source(self.source.pk)
        second = enqueue_source(self.source.pk)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SourceRun.objects.count(), 1)

    def test_global_pause_stops_auto_enqueue_but_manual_queue_still_runs(self):
        RuntimeState.objects.create(key="scheduler_control", value={"enabled": False})
        with patch("radar.services.process_run") as process:
            self.assertEqual(process_pending(), [])
            process.assert_not_called()
        queued = enqueue_source(self.source.pk)
        completed = SourceRun(pk=queued.pk, status="success_no_new", new_count=0, updated_count=0)
        with patch("radar.services.process_run", return_value=completed) as process:
            output = process_pending()
        process.assert_called_once_with(queued.pk)
        self.assertEqual(output[0]["status"], "success_no_new")

    def test_expiry_respects_explicit_time_without_inventing_end_of_day(self):
        now = timezone.now()
        common = {"source": self.source, "title": "合成时效测试", "content_hash": "synthetic",
                  "processing_status": "actionable", "deadline": timezone.localdate()}
        exact = Lead.objects.create(external_id="exact", deadline_at=now - timedelta(minutes=1),
                                    deadline_has_time=True, **common)
        later = Lead.objects.create(external_id="later", deadline_at=now + timedelta(hours=1),
                                    deadline_has_time=True, **common)
        date_only = Lead.objects.create(external_id="date", **{**common, "processing_status": "needs_review"})
        process_pending()
        for lead in [exact, later, date_only]:
            lead.refresh_from_db()
        self.assertEqual(exact.processing_status, "closed")
        self.assertEqual(later.processing_status, "actionable")
        self.assertEqual(date_only.processing_status, "needs_review")
