"""Risk-based parsing and bounded-pagination checks; fixtures are synthetic.

Live source responses, when checked, are kept outside the distributable fixtures.
"""

from pathlib import Path
import unittest

from collectors.sources import AccessBlocked, LayoutError, SOURCES, parse_date, parse_detail, parse_listing, select_batch


FIXTURES = Path(__file__).parent / "fixtures"

def test_source_spider_never_randomizes_below_one_second():
    from collectors.spider import PublicSourceSpider
    assert PublicSourceSpider.custom_settings['DOWNLOAD_DELAY'] >= 1
    assert PublicSourceSpider.custom_settings['RANDOMIZE_DOWNLOAD_DELAY'] is False

def test_guangzhou_environment_list_and_detail_stay_https_and_non_procurement():
    from collectors.sources import parse_listing,parse_detail
    from collectors.spider import RegisteredHostMiddleware
    from scrapy import Request
    from scrapy.exceptions import IgnoreRequest
    from types import SimpleNamespace
    import pytest
    listing='<html><body><a href="http://sthjj.gz.gov.cn/hjgl/jsxm/hpspqgs/content/post_123.html">某制造企业新建项目环境影响评价文件拟审查公示</a><a href="http://evil.example/hjgl/jsxm/hpspqgs/content/post_456.html">环境影响公示</a></body></html>'
    page=parse_listing('gz_environment',listing)
    assert len(page.items)==1 and page.items[0].url.startswith('https://sthjj.gz.gov.cn/')
    detail='<html><div id="logPanel"><p>项目名称：某制造企业新建项目</p><p>建设单位：测试制造有限公司</p><p>建设内容：新建生产线以及配套仓储设施。本公告为环境影响评价公示。</p></div></html>'
    item=parse_detail('gz_environment',detail,page.items[0])
    assert item['kind']=='early_signal' and item['metadata']['signal_type']=='environmental_project'
    assert '建设单位' in item['text'] and '不证明存在物流采购' in item['metadata']['signal_note']
    crawler=SimpleNamespace(spider=SimpleNamespace(source_key='gz_environment',source=SOURCES['gz_environment']))
    middleware=RegisteredHostMiddleware(crawler)
    with pytest.raises(IgnoreRequest):middleware.process_request(Request('http://sthjj.gz.gov.cn/hjgl/jsxm/hpspqgs/index.html'))
    assert middleware.process_request(Request('https://sthjj.gz.gov.cn/hjgl/jsxm/hpspqgs/index.html')) is None


def fixture(name):
    return (FIXTURES / f"collector_{name}.html").read_text(encoding="utf-8")


class SourceParserTests(unittest.TestCase):
    def test_listing_uses_procurement_rows_and_preserves_deadline_meaning(self):
        parsed = parse_listing("ysczw", fixture("ysczw_list"))
        self.assertEqual([i.external_id for i in parsed.items], ["1001", "1002", "1003"])
        self.assertEqual(parsed.next_page, 2)
        self.assertIsNone(parsed.items[0].published_date)
        self.assertEqual(parsed.items[0].metadata["registration_start_date"], "2026-11-13")
        self.assertEqual(parsed.items[0].listed_deadline, "2026-11-20")
        self.assertEqual(parsed.items[0].metadata["deadline_kind"], "registration")

    def test_gated_body_is_partial_and_does_not_copy_navigation_or_guess_buyer(self):
        listing = parse_listing("ysczw", fixture("ysczw_list")).items[0]
        item = parse_detail("ysczw", fixture("ysczw_detail"), listing)
        self.assertEqual(item["access_level"], "partial")
        self.assertEqual(item["metadata"]["body_access"], "member_only")
        self.assertEqual(item["metadata"]["source_updated_date"], "2026-11-13")
        self.assertIsNone(item["published_date"])
        self.assertIn("常温仓储配送", item["text"])
        self.assertNotIn("999", item["text"])
        self.assertNotIn("恶意脚本文本", item["text"])
        self.assertNotIn("平台联系电话", item["text"])
        self.assertEqual(item["attachments"], [])
        self.assertNotIn("buyer_legal_name", item)

    def test_listing_without_rows_is_not_silently_successful(self):
        with self.assertRaises(LayoutError):
            parse_listing("ysczw", "<html><title>新版首页</title><body>页面结构已经改变</body></html>")
        with self.assertRaises(LayoutError):
            parse_listing("ysczw", "<html><body><table><th>报名截止时间</th></table></body></html>")

    def test_pagination_disappearance_is_not_a_false_complete_scan(self):
        page = fixture("ysczw_list").replace("goods-page-bar", "changed-layout")
        with self.assertRaises(LayoutError):
            parse_listing("ysczw", page)

    def test_explicit_empty_requires_known_container(self):
        result = parse_listing("ysczw", "<html><body><table><th>报名截止时间</th><tr><td>暂无数据</td></tr></table></body></html>")
        self.assertTrue(result.explicit_empty)
        self.assertIsNone(result.next_page)
        with self.assertRaises(LayoutError):
            parse_listing("ysczw", "<html><body>无结构的暂无数据提示</body></html>")

    def test_challenge_is_access_block_not_empty(self):
        with self.assertRaises(AccessBlocked):
            parse_listing("ysczw", "<html><title>安全验证</title><body>请输入验证码</body></html>")

    def test_script_only_cookie_challenge_is_blocked(self):
        page = "<html><script>var arg1='0123456789abcdef0123456789abcdef'; function setCookie() {}</script></html>"
        with self.assertRaisesRegex(AccessBlocked, "脚本 Cookie 访问验证"):
            parse_listing("cntcitc", page)

    def test_project_publicity_is_only_an_early_signal(self):
        parsed = parse_listing("suzhou_epia", fixture("suzhou_epia_list"))
        self.assertEqual(parsed.next_page, 2)
        self.assertIn("环境影响报告表公示", parsed.items[0].title)
        item = parse_detail("suzhou_epia", fixture("suzhou_epia_detail"), parsed.items[0])
        self.assertEqual(item["kind"], "early_signal")
        self.assertEqual(item["published_date"], "2026-11-10")
        self.assertEqual(item["access_level"], "public")
        self.assertIsNone(item["listed_deadline"])
        self.assertEqual(len(item["attachments"]), 1)
        self.assertTrue(item["attachments"][0]["url"].startswith("https://www.sz-epia.cn/"))
        self.assertNotIn("阅读", item["text"])

    def test_batch_progress_and_refresh_does_not_starve_unseen(self):
        rows = parse_listing("ysczw", fixture("ysczw_list")).items
        first, remain, _ = select_batch(rows, set(), 2)
        self.assertTrue(remain)
        second, remain, skipped = select_batch(rows, {x.external_id for x in first}, 2, refresh_limit=2)
        self.assertFalse(remain)
        self.assertEqual(second[0].external_id, "1003")
        self.assertEqual(skipped, 2)

    def test_bad_dates_do_not_receive_guessed_values(self):
        self.assertIsNone(parse_date("2026年2月30日"))
        self.assertIsNone(parse_date("待公布"))
        self.assertEqual(parse_date("2026/1/2 11:00"), "2026-01-02")

    def test_fixed_urls_cannot_inject_arbitrary_destinations(self):
        with self.assertRaises(ValueError):
            SOURCES["ysczw"].detail_url("//127.0.0.1/admin")
        with self.assertRaises(ValueError):
            SOURCES["ysczw"].list_url(0)


class PaginationStateTests(unittest.TestCase):
    def test_budget_preserves_current_page_until_unseen_rows_are_consumed(self):
        from scrapy.http import HtmlResponse
        from collectors.spider import PublicSourceSpider
        response = HtmlResponse(url=SOURCES["ysczw"].list_url(1), body=fixture("ysczw_list").encode(), encoding="utf-8")
        first = PublicSourceSpider("ysczw", {}, max_items=2)
        requests = list(first.parse_list(response, 1))
        self.assertEqual(len(requests), 2)
        self.assertEqual(first.next_cursor, 1)
        self.assertTrue(first.budget_limited)
        resumed = PublicSourceSpider("ysczw", {}, max_items=2, seen=["1001", "1002"])
        requests = list(resumed.parse_list(response, 1))
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].cb_kwargs["listing"].external_id, "1003")
        self.assertEqual(resumed.next_cursor, 2)

    def test_redirect_guard_applies_to_registered_hosts(self):
        from scrapy import Request
        from scrapy.exceptions import IgnoreRequest
        from collectors.spider import PublicSourceSpider, RegisteredHostMiddleware
        spider = PublicSourceSpider("ysczw", {})
        from types import SimpleNamespace
        guard = RegisteredHostMiddleware(SimpleNamespace(spider=spider))
        guard.process_request(Request(SOURCES["ysczw"].list_url(1)))
        with self.assertRaises(IgnoreRequest):
            guard.process_request(Request("http://127.0.0.1:8000/"))


if __name__ == "__main__":
    unittest.main()
