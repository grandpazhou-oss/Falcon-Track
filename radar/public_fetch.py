"""Public-only bounded reads: per-hop SSRF and robots checks, shared rate lease."""
import re
import os
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit, urljoin, parse_qsl, urlencode, urlunsplit
import httpx
from filelock import Timeout
from protego import Protego
from .request_rate import public_request
from .network_security import is_public_address

class FetchError(ValueError):
    pass


_request_budget = ContextVar('public_request_budget', default=None)


@contextmanager
def request_budget(max_requests, max_seconds, *, journal=None):
    """Count all actual public requests, including robots and redirects."""
    budget={'limit':max(1,int(max_requests)),'used':0,'deadline':time.monotonic()+max(1,float(max_seconds))}
    if journal is not None:
        budget['journal']=journal
    token=_request_budget.set(budget)
    try:yield budget
    finally:_request_budget.reset(token)


def check_budget():
    budget=_request_budget.get()
    if budget:
        if time.monotonic()>=budget['deadline']:raise FetchError('time_limit')
        if budget['used']>=budget['limit']:raise FetchError('total_request_limit')


def bounded_deadline(seconds):
    deadline=time.monotonic()+seconds
    budget=_request_budget.get()
    return min(deadline,budget['deadline']) if budget else deadline


def _consume_budget(url=None):
    check_budget()
    budget=_request_budget.get()
    if budget:
        budget['used']+=1
        if url and 'journal' in budget:
            p=urlsplit(url)
            query=[(k,'[REDACTED]' if k.lower()=='subscription-key' else v) for k,v in parse_qsl(p.query,keep_blank_values=True)]
            row={'url':urlunsplit((*p[:3],urlencode(query),'')),'status':'no_response'}
            budget['journal'].append(row)
            return row


def validate_url(url):
    p = urlsplit(url)
    try:
        valid = p.scheme in {'http', 'https'} and p.hostname and not p.username and not p.password and p.port in {None, 80, 443}
    except ValueError:
        valid = False
    if not valid:
        raise FetchError('unsafe_url')
    if p.hostname.lower() in {'localhost', 'metadata.google.internal'}:
        raise FetchError('non_public_host')
    try:
        addresses = sorted({r[4][0] for r in socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)})
    except OSError:
        raise FetchError('dns_failed')
    if not addresses or any(not is_public_address(a) for a in addresses):
        raise FetchError('non_public_address')
    return p, addresses[0]


def _request_once(url, max_bytes, deadline, *, method="GET", data=None, conditions=None):
    parsed, address = validate_url(url)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FetchError('time_limit')
    target = httpx.URL(url).copy_with(host=address)
    headers = {'Host': parsed.netloc, 'User-Agent': 'LogisticsLeadRadar/0.2 (public evidence research)'}
    if conditions:
        headers.update({k:v for k,v in conditions.items() if k in {"If-None-Match","If-Modified-Since"}})
    try:
        with public_request(timeout=remaining):
            attempt=_consume_budget(url)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchError('time_limit')
            with httpx.Client(timeout=remaining, follow_redirects=False, trust_env=False) as client:
                with client.stream(method, target, data=data, headers=headers, extensions={'sni_hostname': parsed.hostname}) as r:
                    status = r.status_code
                    if attempt is not None:
                        attempt['status']=status
                    result = {'status': status, 'location': r.headers.get('location', ''), 'content_type': r.headers.get('content-type', '')}
                    result.update(etag=r.headers.get('etag',''), last_modified=r.headers.get('last-modified',''))
                    data = bytearray()
                    if status == 200:
                        length=r.headers.get('content-length','')
                        if length.isdigit() and int(length)>max_bytes:raise FetchError('size_limit')
                        for chunk in r.iter_bytes():
                            if time.monotonic() > deadline:
                                raise FetchError('time_limit')
                            data.extend(chunk)
                            if len(data) > max_bytes or r.num_bytes_downloaded > max_bytes:
                                raise FetchError('size_limit')
                    result['body'] = bytes(data)
                    return result
    except Timeout as exc:
        raise FetchError('rate_wait_timeout') from exc
    except httpx.TimeoutException as exc:
        raise FetchError('timeout') from exc
    except httpx.ConnectError as exc:
        cause=exc
        while cause and not isinstance(cause,ssl.SSLError):cause=cause.__cause__
        raise FetchError('tls_failed' if cause or 'certificate' in str(exc).lower() else 'connect_failed') from exc
    except httpx.HTTPError as exc:
        raise FetchError('network_error') from exc


# Cache is keyed by both scheme and authority, not by host alone.
# Only successful 200 and explicit 404/410 responses are cached.
_robots_cache = {}
_robots_lock = threading.Lock()
_ROBOTS_TTL = 3600.0
_USER_AGENT = 'LogisticsLeadRadar'


def clear_robots_cache():
    with _robots_lock:
        _robots_cache.clear()


def _robots_allowed(url, deadline, same_origin=False, *, robots_path_only=False):
    parsed, _ = validate_url(url)
    key = (parsed.scheme.lower(), parsed.netloc.lower(), same_origin, robots_path_only)
    with _robots_lock:
        cached = _robots_cache.get(key)
        if not cached or cached[0] <= time.monotonic():
            robot_url = f'{parsed.scheme}://{parsed.netloc}/robots.txt'
            chain=[];seen=set();target=robot_url
            for _ in range(6):
                if target in seen:raise FetchError('robots_or_robots_unavailable:redirect_loop')
                seen.add(target);validate_url(target)
                try:response = _request_once(target, 1_000_000, deadline)
                except FetchError as exc:
                    raise FetchError('robots_or_robots_unavailable:'+str(exc)) from exc
                chain.append({'url':target,'status':response['status']})
                if response['status'] not in {301,302,303,307,308}:break
                if not response.get('location'):raise FetchError('robots_or_robots_unavailable:invalid_redirect')
                target=urljoin(target,response['location'])
                if robots_path_only and urlsplit(target).path!='/robots.txt':
                    raise FetchError('robots_redirect_not_followed')
                if same_origin and urlsplit(target)[:2] != parsed[:2]:
                    raise FetchError('off_origin_robots_redirect')
            else:raise FetchError('robots_or_robots_unavailable:redirect_limit')
            if response['status'] in {404, 410}:
                rules = None
            elif response['status'] == 200:
                body = response['body'].decode('utf-8-sig', 'replace')
                # An error or HTML login page is not an empty robots file.
                # Empty/comment-only and Sitemap-only files are valid.
                lines = [line.split('#', 1)[0].strip() for line in body.splitlines()]
                lines = [line for line in lines if line]
                has_agent = bool(re.search(r'(?im)^\s*user-agent\s*:\s*\S+', body))
                sitemap_only = bool(lines) and all(re.match(r'^sitemap\s*:\s*https?://\S+', line, re.I) for line in lines)
                if ('html' in response['content_type'].lower() or re.search(r'<\s*(?:html|!doctype|body)\b', body, re.I)
                        or (lines and not (has_agent or sitemap_only))):
                    raise FetchError('robots_or_robots_unavailable:invalid_format')
                rules = Protego.parse(body)
            else:
                # 403, 429, 5xx and unexpected replies are distinguishable and fail closed.
                raise FetchError(f"robots_or_robots_unavailable:http_{response['status']}")
            cached = (time.monotonic() + _ROBOTS_TTL, rules, chain)
            _robots_cache[key] = cached
        rules = cached[1]
    extra=[x.strip() for x in os.environ.get('LEAD_ROBOTS_EXTRA_AGENTS','').split(',') if x.strip()]
    agents=(_USER_AGENT,*extra)
    return rules is None or all(rules.can_fetch(url, agent) for agent in agents)


def robots_allowed(url):
    try:
        return _robots_allowed(url, time.monotonic() + 12)
    except (FetchError, httpx.HTTPError, Timeout, OSError):
        return False


def fetch(url, max_bytes=3_000_000, timeout=12, *, form=None, same_origin=False, conditions=None, no_redirect=False):
    original = url
    method = "POST" if form is not None else "GET"
    deadline = bounded_deadline(timeout)
    redirects=[];robots_chains=[]
    for _ in range(4):
        check_budget()
        # Do not request either robots or content for a non-public target.
        validate_url(url)
        try:
            allowed = _robots_allowed(url, deadline, same_origin, **({"robots_path_only":True} if no_redirect else {}))
        except (httpx.HTTPError, OSError) as exc:
            raise FetchError('robots_or_robots_unavailable') from exc
        if not allowed:
            raise FetchError('robots_or_robots_unavailable:disallow')
        parsed=urlsplit(url);cached=_robots_cache.get((parsed.scheme.lower(),parsed.netloc.lower(),same_origin,no_redirect))
        if cached:robots_chains.append(cached[2])
        response = (_request_once(url, max_bytes, deadline, method=method, data=form)
                    if method == "POST" else _request_once(url, max_bytes, deadline, **({"conditions":conditions} if conditions else {})))
        if response['status'] in {301, 302, 303, 307, 308}:
            if no_redirect:
                raise FetchError('index_redirect_not_followed')
            if not response['location']:
                raise FetchError('invalid_redirect')
            if response['status'] in {301, 302, 303}:
                method, form = 'GET', None
            target = urljoin(url, response['location'])
            if same_origin and urlsplit(target)[:2] != parsed[:2]:
                raise FetchError('off_origin_redirect')
            redirects.append({'url':url,'status':response['status'],'target':target})
            if method == 'POST' and urlsplit(target).netloc != urlsplit(url).netloc:
                raise FetchError('cross_host_post_redirect')
            url = target
            continue
        if response['status'] != 200 and not (conditions and response['status'] == 304):
            raise FetchError(f"http_{response['status']}")
        return {'url': url, 'status':response['status'], 'etag':response.get('etag',''), 'last_modified':response.get('last_modified',''), 'original_url': original, 'body': response['body'], 'content_type': response['content_type'],
                'redirect_chain':redirects,'robots_redirect_chains':robots_chains}
    raise FetchError('redirect_limit')
