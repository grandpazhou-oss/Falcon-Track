from datetime import date
import unittest

from radar.extraction import extract_event, parse_explicit_dates, usable_participation


class ExtractionTests(unittest.TestCase):
    def test_anonymous_gated_logistics_is_not_actionable_or_foreign(self):
        event = extract_event("2026—2028年度仓配服务招标", "截止日期：2026-11-19\n您暂无下载权限", as_of=date(2026, 11, 13))
        self.assertIsNone(event["buyer_name"])
        self.assertEqual(event["processing_status"], "needs_review")
        self.assertEqual(event["ownership_status"], "unknown")
        self.assertEqual(event["deadline"], "2026-11-19")
        self.assertEqual(event["deadline_kind"], "unknown")
        self.assertFalse(event["deadline_has_time"])
        self.assertEqual(event["extraction_method"], "rules")

    def test_registration_has_priority_over_later_quote_deadline(self):
        event = extract_event("运输服务招标公告", "采购人：南方材料有限公司\n报名截止时间：2026年11月12日 18:00\n报价截止时间：2026年11月20日 18:00\n报名方式：https://example.com/tender", as_of=date(2026, 11, 13))
        self.assertEqual(event["deadline_kind"], "registration")
        self.assertEqual(event["deadline"], "2026-11-12T18:00:00+08:00")
        self.assertEqual(event["processing_status"], "needs_review")
        self.assertIn("registration_closed", event["missing_fields"])
        self.assertGreaterEqual(len([e for e in event["evidence"] if e["field"] == "deadline"]), 2)

    def test_no_year_is_not_invented(self):
        event = extract_event("冷链运输询价", "采购人：东方食品有限公司\n报价截止：11月19日18时\n报价方式：发送至rfq@example.com", as_of=date(2026, 11, 13))
        self.assertIsNone(event["deadline"])
        self.assertEqual(event["processing_status"], "needs_review")

    def test_agent_is_not_substituted_for_buyer(self):
        event = extract_event("仓储服务招标", "招标代理机构：东城采购咨询有限公司\n报名截止：2030-01-01\n报名方式：https://example.com")
        self.assertIsNone(event["buyer_name"])
        self.assertEqual(event["processing_status"], "needs_review")

    def test_explicit_buyer_and_agent_separate(self):
        event = extract_event("运输服务询价", "招标代理机构：东城咨询有限公司\n采购人：南方材料有限公司\n报价截止：2030-01-01\n报价方式：发送至rfq@example.com")
        self.assertEqual(event["buyer_name"], "南方材料有限公司")
        self.assertEqual(event["processing_status"], "actionable")
        self.assertEqual(event["ownership_status"], "unknown")

    def test_same_day_date_only_is_pending(self):
        event = extract_event("运输服务询价", "采购人：南方材料有限公司\n报价截止：2026-11-13\n报价方式：发送至rfq@example.com", as_of=date(2026, 11, 13))
        self.assertEqual(event["processing_status"], "needs_review")
        self.assertIn("deadline_time_verification", event["missing_fields"])

    def test_equipment_construction_software_goods_are_not_services(self):
        for title in ("物流园建设工程招标", "叉车采购招标", "物流管理系统采购公告", "饮用水采购（含运输配送）招标"):
            with self.subTest(title=title):
                event = extract_event(title, "采购人：南方材料有限公司\n含配送和安装。")
                self.assertEqual(event["processing_status"], "irrelevant")

    def test_drinking_water_transport_service_is_kept(self):
        event = extract_event("饮用水运输服务招标", "采购人：南方饮料有限公司")
        self.assertEqual(event["event_type"], "tender")
        self.assertIn("transport", event["service_types"])

    def test_early_signal_never_becomes_confirmed_outsourcing(self):
        event = extract_event("新建仓库及生产线项目公示", "建设单位：横岛材料有限公司\n年产值将增加。", source_kind="early_signal")
        self.assertEqual(event["processing_status"], "early_signal")
        self.assertEqual(event["ownership_status"], "unknown")
        self.assertEqual(event["buyer_name"], "横岛材料有限公司")

    def test_award_terminated_and_direct_procurement_closed(self):
        for title in ("运输服务中标结果公告", "仓储服务终止公告", "物流服务单一来源采购公示"):
            with self.subTest(title=title):
                event = extract_event(title, "采购人：南方材料有限公司\n截止日期：2030-01-01")
                self.assertEqual(event["processing_status"], "closed")

    def test_body_talking_about_future_winner_does_not_close(self):
        event = extract_event("运输服务招标公告", "采购人：南方材料有限公司\n中标人应提供车辆清单。")
        self.assertEqual(event["event_type"], "tender")

    def test_rfi_not_actionable_rfq(self):
        event = extract_event("仓储服务 RFI 信息征询", "采购人：南方材料有限公司\n报价截止：2030-01-01\n报价方式：发送至rfq@example.com")
        self.assertEqual(event["event_type"], "rfi")
        self.assertEqual(event["processing_status"], "needs_review")

    def test_bid_deadline_does_not_establish_registration_open(self):
        event = extract_event("运输服务招标", "采购人：南方材料有限公司\n投标截止：2030-01-01\n参与方式：先在https://example.com报名后报价")
        self.assertEqual(event["processing_status"], "needs_review")
        self.assertIn("registration_deadline", event["missing_fields"])

    def test_supplier_recruitment_kept_distinct(self):
        event = extract_event("承运商招募公告", "采购人：南方材料有限公司\n报名截止：2030-01-01\n报名方式：https://example.com")
        self.assertEqual(event["event_type"], "supplier_recruitment")
        self.assertEqual(event["processing_status"], "actionable")

    def test_generic_platform_is_not_participation_route(self):
        self.assertFalse(usable_participation("请登录平台报名"))
        self.assertFalse(usable_participation("详见附件"))
        self.assertTrue(usable_participation("登录中招联合招标采购平台报名"))

    def test_invalid_date_and_precision(self):
        self.assertEqual(parse_explicit_dates("2026年2月30日"), [])
        self.assertEqual(parse_explicit_dates("2026年11月19日")[0][:2], ("2026-11-19", False))
        self.assertEqual(parse_explicit_dates("2026年11月19日18时")[0][:2], ("2026-11-19T18:00:00+08:00", True))

    def test_listing_date_has_unknown_semantics(self):
        event = extract_event("运输服务公告", "需要运输服务。", listed_deadline="2030-01-01")
        self.assertEqual(event["deadline_kind"], "unknown")
        self.assertEqual(event["processing_status"], "needs_review")

    def test_unknown_registration_cannot_borrow_the_bid_date(self):
        event = extract_event("运输服务招标", "采购人：南方材料有限公司\n报名截止时间：另行通知\n报价截止时间：2030-01-01\n报名方式：https://example.com")
        self.assertEqual(event["deadline_kind"], "bid")
        self.assertEqual(event["processing_status"], "needs_review")
        self.assertIn("registration_deadline", event["missing_fields"])

    def test_evidence_is_verbatim_except_labelled_collector_metadata(self):
        title = "运输服务招标"
        text = "采购人：南方材料有限公司\n报名截止：2030-01-01\n报名方式：https://example.com\n资质要求：须具备道路运输经营许可证。"
        event = extract_event(title, text)
        for proof in event["evidence"]:
            self.assertIn(proof["quote"], title + "\n" + text)


if __name__ == "__main__":
    unittest.main()
