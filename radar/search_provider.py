"""Explicit, quota-controlled search providers. Importing this module never contacts a service."""
from dataclasses import dataclass
from datetime import timedelta
from email.utils import parsedate_to_datetime
from html import unescape
import json
import os
import re
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
from xml.etree import ElementTree

import httpx
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from filelock import FileLock

from .models import SearchCall
from .public_fetch import FetchError, fetch
from .request_rate import public_request


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    published_at: str
    provider: str
    query_id: str


def canonical_url(url):
    try:
        parts = urlsplit(unescape(url.strip()))
        if parts.scheme.lower() not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
            return ''
        port = parts.port
        if port not in (None, 80, 443):
            return ''
        host = parts.hostname.lower().rstrip('.')
        if ':' in host:
            host = '[' + host + ']'
        authority = host + ((':' + str(port)) if port and (parts.scheme.lower(), port) not in {('http', 80), ('https', 443)} else '')
        params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                  if not k.lower().startswith('utm_') and k.lower() not in {'fbclid', 'gclid'}]
        return urlunsplit((parts.scheme.lower(), authority, parts.path or '/', urlencode(sorted(params)), ''))
    except (ValueError, TypeError):
        return ''


def clean_text(text):
    return re.sub(r'\s+', ' ', unescape(re.sub(r'<[^>]+>', ' ', text or ''))).strip()


def parse_feed(body, provider, query_id):
    """Parse RSS 2.0, RSS 1.0/RDF and Atom without resolving external entities."""
    if re.match(br'\s*(?:<!doctype\s+html|<html)', body, re.I):
        raise ValueError('provider_returned_html_not_rss')
    if b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
        raise ValueError('unsafe XML declaration')
    root = ElementTree.fromstring(body)
    rss = '{http://purl.org/rss/1.0/}'
    atom = '{http://www.w3.org/2005/Atom}'
    dc = '{http://purl.org/dc/elements/1.1/}'
    nodes = root.findall('.//item') + root.findall('.//' + rss + 'item') + root.findall('.//' + atom + 'entry')
    results, seen = [], set()
    for item in nodes[:500]:
        def value(*tags):
            for tag in tags:
                child = item.find(tag)
                if child is not None:
                    text = ''.join(child.itertext()).strip()
                    if text:
                        return text
            return ''
        link = value('link', rss + 'link')
        if not link:
            links = item.findall(atom + 'link')
            link = next((node.get('href', '') for node in links
                         if node.get('rel', 'alternate') == 'alternate' and node.get('href')), '')
        url = canonical_url(link)
        if not url or url in seen:
            continue
        seen.add(url)
        published = value('pubDate', dc + 'date', atom + 'published', atom + 'updated')
        if published and not re.match(r'^\d{4}-\d\d-\d\d', published):
            try:
                published = parsedate_to_datetime(published).isoformat()
            except (ValueError, TypeError, OverflowError):
                published = ''
        results.append(SearchResult(clean_text(value('title', rss + 'title', atom + 'title'))[:600], url,
            clean_text(value('description', rss + 'description', atom + 'summary', atom + 'content'))[:4000],
            published[:80], provider, query_id))
    return results, len(nodes)


class RssNewsSearchProvider:
    def __init__(self, engine='bing', locale='ja-JP'):
        if engine not in {'bing', 'google'} or locale not in {'ja-JP', 'zh-CN'}:
            raise ValueError('unsupported RSS engine or locale')
        self.engine, self.locale = engine, locale

    def search(self, query, query_id):
        if self.engine == 'bing':
            domain = 'cn.bing.com' if self.locale == 'zh-CN' else 'www.bing.com'
            url = 'https://' + domain + '/news/search?' + urlencode({'q': query, 'format': 'rss', 'setlang': self.locale, 'mkt': self.locale})
        else:
            lang, country = self.locale.split('-')
            url = 'https://news.google.com/rss/search?' + urlencode({'q': query, 'hl': lang, 'gl': country, 'ceid': country + ':' + lang})
        # Retry transient failures only; the outer logical reservation is reused.
        for attempt in range(2):
            try:
                body = fetch(url, max_bytes=1_000_000, timeout=20)['body']
                return self.parse(body, query_id)
            except FetchError as exc:
                if attempt or str(exc) not in {'timeout', 'connect_failed', 'network_error'}:
                    raise

    def parse(self, body, query_id):
        return parse_feed(body, self.engine + '_rss', query_id)[0]


DIRECT_URLS = {
    'prtimes': 'https://prtimes.jp/index.rdf',
    'prtimes_management': 'https://prtimes.jp/main/html/searchrlp/release_type_id/04',
    'jetro_news': 'https://www.jetro.go.jp/rss/news.xml',
    'jetro_biznews': 'https://www.jetro.go.jp/biznews/',
    'jetro_biznews_rss': 'https://www.jetro.go.jp/rss2/biznews.xml',
}
from .china_places import MAINLAND

CHINA_TERMS = re.compile(MAINLAND.pattern + r'|中國|在華|在华|江蘇|江苏|浙江|china', re.I)
EVENT_TERMS = re.compile(r'設立|设立|新設|新设|進出|进出|工場|工厂|倉庫|仓库|物流|子会社|子公司|現地法人|合弁|合资|合資|生産|生产|稼働|投产|拡張|扩张|拡充|扩建|扩产|提携|合作|輸出|出口|建設|建设', re.I)


class DirectJapanFeedProvider:
    def __init__(self, source='prtimes'):
        if source not in DIRECT_URLS:
            raise ValueError('unsupported direct feed')
        self.source = source
        self.last_stats = {}

    def search(self, query, query_id):
        from collectors.research_common import read_original
        url = DIRECT_URLS[self.source]
        self.last_stats = {'feed_url':url}
        response, evidence = read_original(url, max_bytes=3_000_000, timeout=25)
        self.last_stats.update({'final_url':response['url'], 'raw_path':evidence['raw_path'],
                                'sha256':evidence['sha256'], 'redirect_chain':evidence['redirect_chain'],
                                'robots_redirect_chains':evidence['robots_redirect_chains']})
        body = response['body']
        if self.source in {'prtimes_management', 'jetro_biznews'}:
            items, total = self.parse_listing(body, response['url'], query_id)
        else:
            items, total = parse_feed(body, self.source, query_id)
        matches = [item for item in items if CHINA_TERMS.search(item.title + ' ' + item.snippet)
                   and EVENT_TERMS.search(item.title + ' ' + item.snippet)]
        self.last_stats.update({'raw_items':total, 'parsed_items':len(items), 'local_matches':len(matches)})
        return items

    def parse_listing(self, body, page_url, query_id):
        from urllib.parse import urljoin
        from .website_check import public_html_tree
        tree = public_html_tree(body)
        xpath = ("//a[contains(@href,'/main/html/rd/p/')]" if self.source == 'prtimes_management'
                 else "//a[contains(@href,'/biznews/') and contains(@href,'.html')]")
        links = tree.xpath(xpath)
        results, seen = [], set()
        for node in links[:500]:
            url = canonical_url(urljoin(page_url, node.get('href', '')))
            title = clean_text(' '.join(node.itertext()))
            if not title or not url or url in seen:
                continue
            seen.add(url)
            results.append(SearchResult(title[:600], url, '', '', self.source, query_id))
        return results, len(links)


class BraveSearchProvider:
    def __init__(self, key):
        if not key:
            raise ValueError('missing Brave key')
        self.key = key

    def search(self, query, query_id):
        body = api_json('GET', 'https://api.search.brave.com/res/v1/web/search',
                        params={'q': query, 'count': 10},
                        headers={'X-Subscription-Token': self.key, 'Accept': 'application/json'})
        items = body.get('web', {}).get('results', [])
        return api_results(items, 'brave', query_id)


TAVILY_EXCLUDE_DOMAINS = ('yappango.com', 'bs-multipay.com', 'zhihu.com', 'the-shashi.com',
                          'buffett-code.com', 'shikiho.toyokeizai.net', 'job.mynavi.jp')


class TavilySearchProvider:
    def __init__(self, key, *, days=90):
        if not key:
            raise ValueError('missing Tavily key')
        self.key = key
        self.days = days
        self.last_stats = {}

    def search(self, query, query_id):
        self.last_stats = {}
        body = api_json('POST', 'https://api.tavily.com/search',
                        headers={'Authorization': 'Bearer ' + self.key},
                        json={'query': query, 'search_depth': 'basic', 'max_results': 10,
                              'auto_parameters': False, 'include_usage': True,
                              'start_date': (timezone.localdate() - timedelta(days=self.days)).isoformat(),
                              'include_published_date': True, 'filter_by_published_date': False,
                              'exclude_domains': list(TAVILY_EXCLUDE_DOMAINS)})
        credits = (body.get('usage') or {}).get('credits')
        self.last_stats = {'credits': credits if type(credits) is int and credits >= 0 else None}
        items = body.get('results', [])
        return api_results(items, 'tavily', query_id)


def api_json(method, url, **kwargs):
    with public_request(timeout=20), httpx.Client(timeout=20, follow_redirects=False) as client:
        with client.stream(method, url, **kwargs) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 2_000_000 or response.num_bytes_downloaded > 2_000_000:
                    raise ValueError('search_response_too_large')
    return json.loads(content)


def api_results(items, provider, query_id):
    out, seen = [], set()
    for item in items[:50]:
        url = canonical_url(item.get('url', ''))
        if url and url not in seen:
            seen.add(url)
            out.append(SearchResult(clean_text(item.get('title', ''))[:600], url,
                clean_text((item.get('content') if provider == 'tavily' else item.get('description')) or item.get('description') or item.get('content') or '')[:4000],
                str(item.get('published_date') or '')[:80], provider, query_id))
    return out


def budgeted_search(provider, query, query_id, daily_limit):
    """Reserve once per query, including failed or retried network requests."""
    if daily_limit <= 0:
        raise ValueError('search_daily_limit')
    with FileLock(str(settings.DATA_DIR / 'search-budget.lock'), timeout=5), transaction.atomic():
        if SearchCall.objects.filter(requested_at__date=timezone.localdate()).count() >= daily_limit:
            raise ValueError('search_daily_limit')
        call = SearchCall.objects.create(query=query[:600])
    try:
        results = provider.search(query, query_id)
        call.status = 'succeeded'
        call.urls = [r.url for r in results]
        return results
    except Exception as exc:
        call.status = 'failed'
        code=str(exc)
        call.error_code=code if code in {'rate_limited','search_daily_limit','timeout','search_unavailable'} else type(exc).__name__
        raise
    finally:
        if isinstance(provider, TavilySearchProvider):
            call.tavily_credits = provider.last_stats.get('credits')
        call.save(update_fields=['status', 'urls', 'tavily_credits', 'error_code'])


def provider_for(name, locale):
    if name in DIRECT_URLS:
        return DirectJapanFeedProvider(name)
    if name in {'bing', 'google'}:
        return RssNewsSearchProvider(name, locale)
    if name == 'brave':
        from .ai_config import config, secret
        return BraveSearchProvider(secret(config(), search=True))
    if name == 'tavily':
        key = os.environ.get('TAVILY_API_KEY', '')
        if not key:
            try:
                from .ai_config import secure_keyring, SERVICE
                key = secure_keyring().get_password(SERVICE, 'tavily-search-api') or ''
            except Exception:
                key = ''
        return TavilySearchProvider(key)
    raise ValueError('unsupported provider')
