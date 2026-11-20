"""Source-specific parsing, deliberately independent of the Scrapy reactor."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from lxml import html as lxml_html
from lxml.etree import ParserError


class LayoutError(ValueError):
    """A page did not match the verified listing/detail structure."""


class AccessBlocked(LayoutError):
    """The server returned an access challenge, not an empty source."""


@dataclass(frozen=True)
class SourceDefinition:
    key: str
    name: str
    host: str
    kind: str

    @property
    def allowed_hosts(self) -> set[str]:
        return {self.host, self.host.removeprefix("www.")}

    def list_url(self, page: int) -> str:
        if not 1 <= page <= 10000:
            raise ValueError("Page must be between 1 and 10000")
        from .extra_sources import CATALOG, list_url
        if self.key in CATALOG:
            return list_url(self.key, page)
        if self.key == "ysczw":
            return f"https://{self.host}/?a=index&classify_id=6&m=goods&p={page}"
        return f"https://{self.host}/xmgs.asp?classid=29&page={page}"

    def detail_url(self, external_id: str) -> str:
        if not external_id.isdigit():
            raise ValueError("Source identifiers must contain only digits")
        if self.key == "ysczw":
            return f"https://{self.host}/index.php?m=goods&a=details&content_id={external_id}"
        return f"https://{self.host}/xmgsshow.asp?id={external_id}"


SOURCES = {
    "ysczw": SourceDefinition("ysczw", "交通运输采招网 · 物流招标", "www.ysczw.com", "procurement"),
    "suzhou_epia": SourceDefinition("suzhou_epia", "苏州市环保产业协会 · 项目公示", "www.sz-epia.cn", "early_signal"),
}


@dataclass
class Listing:
    external_id: str
    url: str
    title: str
    published_date: str | None = None
    listed_deadline: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ListingPage:
    items: list[Listing]
    next_page: int | None
    explicit_empty: bool = False


def clean_text(value: str) -> str:
    return re.sub(r"[\t\r\f\v\u00a0\u3000 ]+", " ", value).strip()


def node_text(node: Any) -> str:
    """Keep block boundaries while excluding script/style/navigation machinery."""
    blocks = {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4", "br"}
    parts = []
    def walk(element):
        if not isinstance(element.tag,str):return
        tag = element.tag.lower()
        if tag in {"script", "style", "noscript", "nav", "footer"}:
            return
        if tag=="tr" and element.xpath("./td|./th") and all(len(clean_text(cell.text_content()))<=400 for cell in element.xpath("./td|./th")):
            parts.append("\n"+" | ".join(" ".join(node_text(cell).splitlines()) for cell in element.xpath("./td|./th"))+"\n")
            return
        if tag in blocks:
            parts.append("\n")
        if element.text:
            parts.append(element.text)
        for child in element:
            walk(child)
            if child.tail:
                parts.append(child.tail)
        if tag in {"td", "th"}:
            parts.append(" | ")
        elif tag in blocks:
            parts.append("\n")
    walk(node)
    return "\n".join(clean_text(x).strip(" |") for x in "".join(parts).splitlines() if clean_text(x).strip(" |"))


def first_text(nodes: list[Any]) -> str:
    if not nodes:
        return ""
    value = nodes[0]
    return clean_text(value if isinstance(value, str) else node_text(value))


def parse_date(text: str | None) -> str | None:
    if not text:
        return None
    m = re.search(r"(?<!\d)(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})(?:日)?", text)
    if not m:
        return None
    try:
        return date(*(int(p) for p in m.groups())).isoformat()
    except ValueError:
        return None


def html_tree(text: str) -> Any:
    if len(text.strip()) < 20:
        raise LayoutError("Empty or truncated HTML response")
    try:
        return lxml_html.fromstring(text)
    except (ValueError, ParserError) as exc:
        raise LayoutError("Unreadable HTML") from exc


def assert_no_challenge(tree: Any) -> None:
    title = first_text(tree.xpath("//title"))
    body = node_text(tree)
    # Login links in normal navigation are not themselves an access challenge.
    challenge = ("访问验证", "人机验证", "安全验证", "访问被拒绝", "Access Denied", "Just a moment", "Verify you are human")
    if any(x.lower() in title.lower() for x in challenge):
        raise AccessBlocked(f"Access challenge: {title[:100]}")
    if not body and not tree.xpath("//a[@href]"):
        scripts = "\n".join(tree.xpath("//script/text()"))
        if "setCookie(" in scripts and re.search(r"\bvar\s+arg1\s*=\s*['\"][0-9a-fA-F]{24,}['\"]", scripts):
            raise AccessBlocked("原站要求完成脚本 Cookie 访问验证")
    if len(body) < 800 and any(x in body for x in ("请输入验证码", "访问过于频繁", "请完成验证", "请先登录")):
        raise AccessBlocked("Access challenge or login-only response")


def _query_number(href: str, key: str) -> str | None:
    try:
        query = {k.lower(): v for k, v in parse_qs(urlsplit(href).query).items()}
        value = query.get(key.lower(), [""])[0]
        return value if value.isdigit() else None
    except ValueError:
        return None


def _next_page(tree: Any, page: int, source: SourceDefinition) -> int | None:
    if source.key == "ysczw":
        if not tree.xpath("//div[contains(concat(' ', normalize-space(@class), ' '), ' goods-page-bar ')]"):
            raise LayoutError("Verified pagination container is missing; coverage cannot be established")
        candidates = tree.xpath("//a[contains(concat(' ', normalize-space(@class), ' '), ' link_page ')]/@href")
        key = "p"
    else:
        if not tree.xpath("//div[contains(concat(' ', normalize-space(@class), ' '), ' ny_news_page ')]"):
            raise LayoutError("Verified pagination container is missing; coverage cannot be established")
        candidates = tree.xpath("//div[contains(concat(' ', normalize-space(@class), ' '), ' ny_news_page ')]//a/@href")
        key = "page"
    pages = sorted({int(p) for href in candidates if (p := _query_number(href, key)) and page < int(p) <= 10000})
    return pages[0] if pages else None


def parse_listing(source_key: str, html: str, page: int = 1) -> ListingPage:
    if source_key=='gmg':
        from .gmg_source import listing as api_listing
        return api_listing(html,page)
    source = SOURCES[source_key]
    tree = html_tree(html)
    assert_no_challenge(tree)
    from .extra_sources import CATALOG, listing
    if source_key in CATALOG:
        return listing(source_key, tree, page, html)
    items: list[Listing] = []
    ids: set[str] = set()
    if source_key == "ysczw":
        # Actual table rows, not sidebar links to a different news category.
        rows = tree.xpath("//tr[td/a[contains(@href, 'content_id=') and contains(@href, 'm=goods') and contains(@href, 'a=details')]]")
        has_container = bool(tree.xpath("//table[.//th[contains(., '报名截止时间')]]"))
        for row in rows:
            anchors = row.xpath("./td/a[contains(@href, 'content_id=')]")
            if not anchors:
                continue
            anchor = anchors[0]
            identifier = _query_number(anchor.get("href", ""), "content_id")
            title = clean_text(node_text(anchor))
            cells = row.xpath("./td")
            if not identifier or not title or identifier in ids:
                continue
            ids.add(identifier)
            start_raw = node_text(cells[2]) if len(cells) > 2 else ""
            deadline_raw = node_text(cells[3]) if len(cells) > 3 else ""
            metadata = {
                "list_page": page,
                "industry": node_text(cells[1]) if len(cells) > 1 else "",
                "registration_start_date": parse_date(start_raw),
                "registration_start_raw": start_raw,
                "deadline_kind": "registration",
                "deadline_raw": deadline_raw,
                "source_reported_status": node_text(cells[4]) if len(cells) > 4 else "",
            }
            items.append(Listing(identifier, source.detail_url(identifier), title,
                                 listed_deadline=parse_date(deadline_raw), metadata=metadata))
    else:
        has_container = bool(tree.xpath("//div[contains(concat(' ', normalize-space(@class), ' '), ' ny_a_r03 ')]"))
        anchors = tree.xpath("//div[contains(concat(' ', normalize-space(@class), ' '), ' ny_a_r03 ')]//li/a[contains(@href,'xmgsshow.asp')]")
        for anchor in anchors:
            identifier = _query_number(anchor.get("href", ""), "id")
            title = clean_text(anchor.get("title") or first_text(anchor.xpath("./text()"))).lstrip("· ")
            if not identifier or not title or identifier in ids:
                continue
            ids.add(identifier)
            date_raw = first_text(anchor.xpath("./span"))
            items.append(Listing(identifier, source.detail_url(identifier), title,
                                 published_date=parse_date(date_raw), metadata={"list_page": page, "published_raw": date_raw}))
    if not items:
        # No matching link is usually a page change; demand an explicit no-data message.
        text = node_text(tree)
        empty = has_container and any(v in text for v in ("暂无数据", "暂无信息", "没有相关记录", "没有符合条件的记录", "总数：0", "总数:0"))
        if empty:
            return ListingPage([], None, explicit_empty=True)
        raise LayoutError("Verified listing structure missing or no recognizable rows; not treated as no-new-data")
    return ListingPage(items, _next_page(tree, page, source))


def select_batch(items: list[Listing], seen: set[str], limit: int, refresh_limit: int = 0) -> tuple[list[Listing], bool, int]:
    """Return selected rows, whether eligible rows remain, and skipped seen count.

    New identifiers are always prioritized. A caller may explicitly request a
    small number of known rows for change checks; refreshes cannot starve new rows.
    """
    unseen = [x for x in items if x.external_id not in seen]
    known = [x for x in items if x.external_id in seen]
    selected = unseen[:max(0, limit)]
    room = max(0, limit - len(selected))
    selected.extend(known[:min(max(0, refresh_limit), room)])
    return selected, len(unseen) > len([x for x in selected if x.external_id not in seen]), len(known)


def _attachments(nodes: list[Any], base_url: str) -> list[dict[str, str]]:
    result = []
    seen = set()
    for root in nodes:
        for anchor in root.xpath(".//a[@href]"):
            target = urljoin(base_url, anchor.get("href", ""))
            if urlsplit(target).scheme not in {"http", "https"} or target in seen:
                continue
            if re.search(r"\.(pdf|docx?|xlsx?|zip|rar)(?:[?#]|$)", target, re.I) or anchor.get("download") is not None:
                seen.add(target)
                result.append({"url": target, "label": clean_text(node_text(anchor))[:300] or "附件"})
    return result


def parse_detail(source_key: str, html: str, listing: Listing) -> dict[str, Any]:
    if source_key=='gmg':
        from .gmg_source import detail as api_detail
        return api_detail(html,listing)
    source = SOURCES[source_key]
    tree = html_tree(html)
    assert_no_challenge(tree)
    from .extra_sources import CATALOG, detail
    if source_key in CATALOG:
        return detail(source_key, tree, listing, html)
    metadata = dict(listing.metadata)
    title = listing.title
    published_date = listing.published_date
    if source_key == "ysczw":
        summaries = tree.xpath("//*[@id='zhaiyao']")
        contents = tree.xpath("//*[@id='z_content']")
        if not summaries and not contents:
            raise LayoutError("Verified procurement detail containers are missing")
        if summaries:
            title = first_text(summaries[0].xpath("../p[1]")) or title
        container = summaries[0].getparent() if summaries else contents[0].getparent()
        container_text = node_text(container)
        title = title or first_text(tree.xpath("//title")).split("_")[0]
        summary = "\n".join(node_text(n) for n in summaries)
        full = "\n".join(node_text(n) for n in contents)
        gated = any(s in full for s in ("仅对vip会员开放", "登录后可查看全文", "仅对VIP会员开放", "会员可见", "请先登录"))
        if gated:
            full = ""
        download_gated = "暂无下载权限" in container_text
        access = "partial" if gated or download_gated else "public"
        if not summary.strip() and not full.strip():
            if gated:
                access = "blocked"
            else:
                raise LayoutError("Procurement page has empty content containers")
        rows = container.xpath("./table//tr")
        for row in rows:
            cells = row.xpath("./td")
            if len(cells) >= 2 and "更新时间" in node_text(cells[0]):
                metadata["source_updated_raw"] = node_text(cells[1])
                metadata["source_updated_date"] = parse_date(node_text(cells[1]))
        regions = container.xpath("./div[contains(@class, 'biaoqian')]/span[1]")
        metadata["region"] = first_text(regions)
        metadata["body_access"] = "member_only" if gated else "public"
        metadata["attachment_access"] = "member_only" if download_gated else "links_only"
        metadata["access_note"] = "公开摘要可读，全文或附件需要网站授权" if access != "public" else "公开网页正文"
        parts = [title]
        if metadata.get("region"):
            parts.append("地区：" + metadata["region"])
        if metadata.get("registration_start_date"):
            parts.append("报名开始日期：" + metadata["registration_start_date"])
        if listing.listed_deadline:
            parts.append("报名截止日期：" + listing.listed_deadline)
        parts.extend([summary, full])
        content_nodes = summaries + contents
    else:
        titles = tree.xpath("//div[contains(concat(' ', normalize-space(@class), ' '), ' about_nr_bt ')]")
        contents = tree.xpath("//div[contains(concat(' ', normalize-space(@class), ' '), ' about_nr_jsaa ')]")
        if not titles or not contents:
            raise LayoutError("Verified project-publicity detail containers are missing")
        title = first_text(titles)
        pub = first_text(tree.xpath("//div[contains(concat(' ', normalize-space(@class), ' '), ' about_nr_js ')]"))
        published_date = parse_date(pub) or published_date
        metadata["published_raw"] = re.split(r"阅读[：:]", pub)[0].strip()
        metadata["signal_note"] = "项目公示是早期企业信号，尚未证明物流外包或公开采购"
        access = "public"
        parts = [title, "\n".join(node_text(n) for n in contents)]
        content_nodes = contents + tree.xpath("//div[contains(concat(' ', normalize-space(@class), ' '), ' about_nr_jsaabb ')]")
    text = "\n".join(p.strip() for p in parts if p and p.strip())
    if not title or len(text) < 15:
        raise LayoutError("Detail did not contain sufficient title/content")
    return {
        "external_id": listing.external_id,
        "url": listing.url,
        "title": title,
        "text": text,
        "html": html,
        "published_date": published_date,
        "listed_deadline": listing.listed_deadline,
        "kind": source.kind,
        "access_level": access,
        "attachments": _attachments(content_nodes, listing.url),
        "metadata": metadata,
    }

from .extra_sources import CATALOG
SOURCES.update({k: SourceDefinition(k,v["name"],v["host"],v["kind"]) for k,v in CATALOG.items()})
