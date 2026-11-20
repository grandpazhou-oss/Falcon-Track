"""Bounded, robots-aware collection of two explicitly registered public sources."""

from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
from urllib.parse import urlsplit

import scrapy
from scrapy import signals
from scrapy.exceptions import IgnoreRequest
from filelock import Timeout
from radar.public_fetch import robots_allowed
from radar.network_security import is_public_address
from radar.request_rate import RequestLease

from .sources import AccessBlocked, LayoutError, Listing, SOURCES, parse_detail, parse_listing, select_batch


class RegisteredHostMiddleware:
    """Redirects are subject to the same host boundary as initial requests."""

    def __init__(self, crawler):
        self.crawler = crawler

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    def process_request(self, request):
        spider = self.crawler.spider
        parsed = urlsplit(request.url)
        if spider.source_key == 'gz_environment' and parsed.scheme != 'https':
            raise IgnoreRequest('This source is restricted to verified HTTPS paths')
        if parsed.scheme not in {"https", "http"} or parsed.hostname not in spider.source.allowed_hosts:
            raise IgnoreRequest("Request/redirect outside registered source hosts")
        if parsed.username or parsed.password:
            raise IgnoreRequest("Credentials in source URL are not allowed")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None and not is_public_address(address):
            raise IgnoreRequest("Non-public address is not allowed")


class StrictRobotsMiddleware:
    """Check the actual URL's scheme and authority; deny unavailable rules."""
    def process_request(self, request):
        if not robots_allowed(request.url):
            raise IgnoreRequest('robots_or_robots_unavailable')


class SharedRequestRateMiddleware:
    """Share the public website request lease with HTTPX and other processes."""
    def process_request(self, request):
        try:
            request.meta['_public_request_lease'] = RequestLease(timeout=300).acquire()
        except (Timeout, OSError) as exc:
            raise IgnoreRequest('public_request_rate_unavailable') from exc

    @staticmethod
    def _release(request):
        lease = request.meta.pop('_public_request_lease', None)
        if lease:
            lease.release()

    def process_response(self, request, response):
        self._release(request)
        return response

    def process_exception(self, request, exception):
        self._release(request)


class PublicSourceSpider(scrapy.Spider):
    name = "public_source"
    custom_settings = {
        "ROBOTSTXT_OBEY": True,
        "USER_AGENT": "LogisticsLeadRadar/0.2 (+local business research; public pages only)",
        "COOKIES_ENABLED": False,
        "HTTPPROXY_ENABLED": False,
        "DNS_RESOLVER": "collectors.resolver.PublicOnlyResolver",
        "CONCURRENT_REQUESTS": 1,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "DOWNLOAD_DELAY": 1.05,
        # Scrapy's default randomization can otherwise reduce the delay to 0.5s.
        "RANDOMIZE_DOWNLOAD_DELAY": False,
        "DOWNLOAD_TIMEOUT": 25,
        "DOWNLOAD_MAXSIZE": 3_000_000,
        "DOWNLOAD_WARNSIZE": 2_000_000,
        "RETRY_ENABLED": False,
        "REDIRECT_MAX_TIMES": 4,
        "CLOSESPIDER_TIMEOUT": 240,
        "LOG_LEVEL": "WARNING",
        "TELNETCONSOLE_ENABLED": False,
        "DOWNLOADER_MIDDLEWARES": {
            "collectors.spider.RegisteredHostMiddleware": 40,
            # Scrapy's stock middleware caches by host only and permits failures.
            "scrapy.downloadermiddlewares.robotstxt.RobotsTxtMiddleware": None,
            "collectors.spider.StrictRobotsMiddleware": 90,
            "collectors.spider.SharedRequestRateMiddleware": 900,
        },
    }
    if scrapy.version_info >= (2, 20):
        custom_settings.update({"DOWNLOAD_VERIFY_CERTIFICATES": True, "DOWNLOAD_DELAY_JITTER": 0})
    else:
        custom_settings.update({
            "DOWNLOADER_CLIENTCONTEXTFACTORY": "scrapy.core.downloader.contextfactory.BrowserLikeContextFactory",
            "RANDOMIZE_DOWNLOAD_DELAY": False,
        })

    def __init__(self, source_key, result, cursor=1, max_pages=1, max_items=5, seen=None, refresh_items=0, **kwargs):
        super().__init__(**kwargs)
        self.source_key = source_key
        self.source = SOURCES[source_key]
        self.allowed_domains = sorted(self.source.allowed_hosts)
        self.result = result
        self.start_page = int(cursor)
        self.current_page = int(cursor)
        self.max_pages = int(max_pages)
        self.max_items = int(max_items)
        self.refresh_items = int(refresh_items)
        self.seen = set(seen or [])
        self.items = []
        self.errors = []
        self.pending = 0
        self.list_pages = 0
        self.listed_rows = 0
        self.skipped_seen = 0
        self.details_requested = 0
        self.budget_limited = False
        self.has_block = False
        self.stop_now = False
        self.next_cursor = self.start_page
        self._next_page_to_fetch = None
        self._page_failed = False
        self.started_at = datetime.now(timezone.utc).isoformat()

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider._spider_error, signal=signals.spider_error)
        return spider

    async def start(self):
        # Async start is the supported API in current Scrapy releases.
        yield self._list_request(self.start_page)

    def start_requests(self):
        # Retained for compatible older Scrapy releases.
        yield self._list_request(self.start_page)

    def _list_request(self, page):
        return scrapy.Request(
            self.source.list_url(page), callback=self.parse_list, errback=self.request_error,
            cb_kwargs={"page": page}, meta={"stage": "listing", "page": page, "handle_httpstatus_all": True},
        )

    def _check_response(self, response):
        if response.status in {401, 403, 429}:
            raise AccessBlocked(f"HTTP {response.status}; source access/rate limit needs attention")
        if response.status != 200:
            raise LayoutError(f"HTTP {response.status}")
        content_type = response.headers.get(b"Content-Type", b"").decode("ascii", "replace").lower()
        if content_type and "html" not in content_type and "text/plain" not in content_type and not (self.source_key=="gmg" and "json" in content_type):
            raise LayoutError(f"Unexpected content type: {content_type[:80]}")

    def parse_list(self, response, page):
        self.current_page = page
        self.next_cursor = page
        try:
            self._check_response(response)
            parsed = parse_listing(self.source_key, response.text, page)
        except (AccessBlocked, LayoutError) as exc:
            self._record_error("listing", response.url, exc)
            return
        self.list_pages += 1
        self.listed_rows += len(parsed.items)
        room = self.max_items - self.details_requested
        selected, remaining_on_page, skipped = select_batch(parsed.items, self.seen, room, self.refresh_items)
        self.skipped_seen += skipped
        self.pending = len(selected)
        self._page_failed = False
        self._next_page_to_fetch = None
        if remaining_on_page:
            self.budget_limited = True
            self.next_cursor = page
        elif parsed.next_page is None:
            self.next_cursor = 1  # Completed source traversal; the next cycle begins at the head.
        elif self.list_pages >= self.max_pages or room <= len(selected):
            self.budget_limited = True
            self.next_cursor = parsed.next_page
        else:
            self._next_page_to_fetch = parsed.next_page
            self.next_cursor = parsed.next_page
        for index, item in enumerate(selected):
            self.details_requested += 1
            yield scrapy.Request(
                item.url, callback=self.parse_item, errback=self.request_error,
                priority=len(selected) - index,
                cb_kwargs={"listing": item},
                meta={"stage": "detail", "page": page, "external_id": item.external_id, "handle_httpstatus_all": True},
            )
        if not selected:
            yield from self._advance_page()

    def parse_item(self, response, listing: Listing):
        try:
            self._check_response(response)
            item = parse_detail(self.source_key, response.text, listing)
            self.items.append(item)
        except (AccessBlocked, LayoutError) as exc:
            self._record_error("detail", response.url, exc)
            self._page_failed = True
            self.next_cursor = response.meta.get("page", self.current_page)
        finally:
            self.pending = max(0, self.pending - 1)
        if self.pending == 0:
            yield from self._advance_page()

    def _advance_page(self):
        if self.stop_now or self._page_failed:
            return
        if self._next_page_to_fetch:
            yield self._list_request(self._next_page_to_fetch)

    def request_error(self, failure):
        request = failure.request
        stage = request.meta.get("stage", "request")
        exc = failure.value
        if isinstance(exc, IgnoreRequest):
            exc = AccessBlocked(str(exc))
        self._record_error(stage, request.url, exc)
        self.next_cursor = request.meta.get("page", self.current_page)
        if stage == "detail":
            self._page_failed = True
            self.pending = max(0, self.pending - 1)

    def _record_error(self, stage, url, exc):
        self.errors.append({"stage": stage, "url": url, "message": str(exc)[:500]})
        if isinstance(exc, AccessBlocked):
            self.has_block = True
        self.stop_now = True

    def _spider_error(self, failure, response, spider):
        self._record_error("parser", getattr(response, "url", ""), failure.value)

    def closed(self, reason):
        interrupted = reason != "finished"
        if interrupted:
            self.errors.append({"stage": "runtime", "url": "", "message": f"Stopped: {reason}; resume from saved page"})
            self.next_cursor = self.current_page
        if self.errors or interrupted:
            status = "partial" if self.items else ("blocked" if self.has_block else "failed")
        else:
            status = "partial" if self.budget_limited else "success"
        stats = self.crawler.stats.get_stats()
        self.result.update({
            "source_key": self.source_key,
            "status": status,
            "items": self.items,
            "next_cursor": self.next_cursor,
            "error": "; ".join(e["message"] for e in self.errors[:3]),
            "stats": {
                "started_at": self.started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "request_count": stats.get("downloader/request_count", 0),
                "response_count": stats.get("downloader/response_count", 0),
                "list_pages": self.list_pages,
                "listed_rows": self.listed_rows,
                "detail_requested": self.details_requested,
                "detail_succeeded": len(self.items),
                "skipped_seen": self.skipped_seen,
                "new_items": sum(x["external_id"] not in self.seen for x in self.items),
                "refreshed_items": sum(x["external_id"] in self.seen for x in self.items),
                "partial_access_items": sum(x["access_level"] == "partial" for x in self.items),
                "blocked_access_items": sum(x["access_level"] == "blocked" for x in self.items),
                "error_count": len(self.errors),
                "errors": self.errors,
                "budget_limited": self.budget_limited,
                "coverage_complete": status == "success",
                "no_new_data": status == "success" and not any(x["external_id"] not in self.seen for x in self.items),
                "robots_obeyed": True,
                "finish_reason": reason,
            },
        })
