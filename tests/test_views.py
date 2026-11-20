from datetime import date, datetime, timezone as datetime_timezone
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse

from radar.forms import CompanyForm
from radar.models import Company, Lead, Review, RuntimeState, Source, SourceRun


class ReviewWorkspaceTests(TestCase):
    def setUp(self):
        self.source = Source.objects.create(key="ysczw", name="公开物流栏目", url="https://www.ysczw.com/", kind="procurement")
        self.lead = Lead.objects.create(
            source=self.source, external_id="test-1", title="公开仓储运输采购公告",
            text="采购方和报名方式尚待核实。", service_types=["warehousing", "transport"],
            content_hash="a" * 64, missing_fields=["buyer_name"], metadata={"keep": "original"},
        )

    def test_empty_and_populated_pages_render(self):
        for name in ["dashboard", "leads", "sources", "runs", "import"]:
            with self.subTest(page=name):
                response = self.client.get(reverse("radar:" + name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "AI 未启用")
        self.assertContains(self.client.get(reverse("radar:leads")), "公开仓储运输采购公告")

    def test_review_requires_post_and_preserves_evidence(self):
        url = reverse("radar:review", args=[self.lead.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        response = self.client.post(url, {"status": "accepted", "note": "线路匹配，需核实报名。"})
        self.assertEqual(response.status_code, 302)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.review_status, "accepted")
        self.assertEqual(self.lead.text, "采购方和报名方式尚待核实。")
        self.assertEqual(self.lead.processing_status, "needs_review")
        self.assertEqual(Review.objects.get(lead=self.lead).note, "线路匹配，需核实报名。")

    def test_contacted_status_requires_actual_action_note(self):
        response = self.client.post(reverse("radar:review", args=[self.lead.pk]), {"status": "contacted", "note": ""})
        self.assertEqual(response.status_code, 400)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.review_status, "new")

    def test_source_writes_have_csrf_and_queue_only(self):
        client = Client(enforce_csrf_checks=True)
        url = reverse("radar:run_source", args=[self.source.pk])
        self.assertEqual(client.post(url).status_code, 403)
        run = SourceRun.objects.create(source=self.source)
        with patch("radar.services.enqueue_source", return_value=run) as enqueue:
            response = self.client.post(url)
        enqueue.assert_called_once_with(self.source.pk)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(run.status, "queued")

    def test_manual_company_change_requires_ownership_evidence(self):
        url = reverse("radar:company", args=[self.lead.pk])
        response = self.client.post(url, {"buyer_name": "示例材料有限公司", "ownership_status": "japan_confirmed", "ownership_evidence": ""})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Company.objects.exists())
        response = self.client.post(url, {"buyer_name": "示例材料有限公司", "ownership_status": "unknown", "ownership_evidence": ""})
        self.assertEqual(response.status_code, 302)
        self.lead.refresh_from_db()
        self.assertTrue(self.lead.metadata["manual_buyer"])
        self.assertEqual(self.lead.metadata["keep"], "original")
        self.assertEqual(self.lead.company.ownership_status, "unknown")

    def test_filter_service_and_unknown_ownership(self):
        url = reverse("radar:leads")
        self.assertContains(self.client.get(url, {"service": "transport", "ownership": "unknown"}), self.lead.title)
        self.assertNotContains(self.client.get(url, {"service": "cold_chain"}), self.lead.title)

    def test_evidence_is_escaped_and_unsafe_links_not_rendered(self):
        self.lead.text = "<script>alert('raw')</script> 原文"
        self.lead.url = "javascript:alert(1)"
        self.lead.save()
        response = self.client.get(reverse("radar:lead_detail", args=[self.lead.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert")
        self.assertNotContains(response, 'href="javascript:')

    def test_early_signal_is_not_presented_as_confirmed_buyer(self):
        self.lead.processing_status = "early_signal"
        self.lead.event_type = "early_signal"
        self.lead.save()
        response = self.client.get(reverse("radar:lead_detail", args=[self.lead.pk]))
        self.assertContains(response, "关联企业 / 建设单位")
        self.assertContains(response, "不代表已经确认物流外包需求")

    def test_scheduler_control_is_explicit_and_boolean(self):
        url = reverse("radar:scheduler_settings")
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url, {"enabled": "anything"}).status_code, 400)
        self.assertEqual(self.client.post(url, {"enabled": "true"}).status_code, 302)
        self.assertEqual(RuntimeState.objects.get(key="scheduler_control").value, {"enabled": True})
        self.assertEqual(self.client.post(url, {"enabled": "false"}).status_code, 302)
        self.assertEqual(RuntimeState.objects.get(key="scheduler_control").value, {"enabled": False})

    def test_import_does_not_fetch_arbitrary_url(self):
        with patch("radar.services.import_text", return_value=self.lead) as importer:
            response = self.client.post(reverse("radar:import"), {
                "title": "手工提供的公告", "url": "https://example.com/source", "source_kind": "procurement",
                "text": "这是一段手工提供的公告正文，用于验证标题、正文和来源路径的传递。",
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(importer.call_args.kwargs["url"], "https://example.com/source")

    def test_manual_import_sources_cannot_be_scheduled(self):
        source = Source.objects.create(key="manual_procurement", name="原文导入", url="", kind="procurement")
        self.assertNotContains(self.client.get(reverse("radar:sources")), "原文导入")
        self.assertEqual(self.client.post(reverse("radar:run_source", args=[source.pk])).status_code, 400)
        self.assertEqual(self.client.post(reverse("radar:source_settings", args=[source.pk]), {"enabled": "on", "interval_minutes": 60}).status_code, 400)

    def test_partial_error_is_not_presented_as_successful_budget_limit(self):
        SourceRun.objects.create(source=self.source, status="partial", error="详情页读取失败。",
                                 stats={"partial_reason": "errors", "head_pages_limit": 3})
        response = self.client.get(reverse("radar:runs"))
        self.assertContains(response, "出错待重试")
        self.assertContains(response, "部分读取出现错误")
        self.assertContains(response, "来源最新前 3 页")
        self.assertNotContains(response, "本批工作量达到上限")

    def test_exact_deadline_is_shown_in_china_time(self):
        self.lead.deadline = date(2099, 12, 1)
        self.lead.deadline_at = datetime(2099, 12, 1, 10, 0, tzinfo=datetime_timezone.utc)
        self.lead.deadline_has_time = True
        self.lead.save()
        response = self.client.get(reverse("radar:lead_detail", args=[self.lead.pk]))
        self.assertContains(response, "2099 年 12 月 01 日 18:00")
        self.assertContains(response, "北京时间")
        self.assertNotContains(response, "具体时刻待核实")
        self.lead.deadline_at = None
        self.lead.deadline_has_time = False
        self.lead.save()
        response = self.client.get(reverse("radar:lead_detail", args=[self.lead.pk]))
        self.assertContains(response, "具体时刻待核实")

    def test_listing_metadata_is_labelled_separately_from_body_quotes(self):
        self.lead.evidence = [
            {"field": "listed_deadline", "quote": "2099-12-01"},
            {"field": "listed_deadline_kind", "quote": "registration"},
            {"field": "deadline", "quote": "正文中的报名截止时间需要核实。"},
        ]
        self.lead.missing_fields = ["deadline_conflict"]
        self.lead.save()
        response = self.client.get(reverse("radar:lead_detail", args=[self.lead.pk]))
        self.assertContains(response, "正文与列表截止时间冲突")
        self.assertContains(response, "列表期限类别")
        self.assertContains(response, "列表采集字段 · 非正文引文", count=2)
        self.assertContains(response, "<p>报名截止</p>", html=True)
        self.assertContains(response, "<blockquote>正文中的报名截止时间需要核实。</blockquote>", html=True)
        self.assertNotContains(response, "<blockquote>2099-12-01</blockquote>")
        self.assertNotContains(response, "registration")

    def test_company_update_merges_metadata_saved_after_initial_read(self):
        original_is_valid = CompanyForm.is_valid

        def validate_after_collection(form):
            result = original_is_valid(form)
            Lead.objects.filter(pk=self.lead.pk).update(metadata={"keep": "original", "collector_stamp": "new-source-evidence"})
            return result

        with patch("radar.views.CompanyForm.is_valid", autospec=True, side_effect=validate_after_collection):
            response = self.client.post(reverse("radar:company", args=[self.lead.pk]), {
                "buyer_name": "示例资料核实有限公司", "ownership_status": "unknown", "ownership_evidence": "",
            })
        self.assertEqual(response.status_code, 302)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.metadata, {"keep": "original", "collector_stamp": "new-source-evidence", "manual_buyer": True})

