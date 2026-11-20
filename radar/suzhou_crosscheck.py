"""Cross-check exact Suzhou roster names against publicly linked corporate profiles."""
import hashlib
import re
import unicodedata
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from filelock import FileLock
from lxml import html

from collectors.sources import node_text
from .models import Company
from .public_fetch import FetchError, fetch

ROSTER = 'https://snd-jp.com/wp/snd/about-html/about-jp/'
CHAMBER = 'https://sznissho.org/trade_meeting/product_info_zh/'
CANON_GROUP = 'https://www.canon.com.cn/about/china/introduction/'
PANASONIC_GROUP = 'https://panasonic.cn/about/panasonic-china/'
SHIMADZU_GROUP = 'https://www.shimadzu.com.cn/aboutus/company/chinacompany.html'


def _normal(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text or '')).casefold()


def _read(url, host):
    try:
        result = fetch(url, timeout=24)
    except httpx.RequestError as exc:
        raise FetchError(f'{host}: {type(exc).__name__}') from exc
    parsed = urlsplit(result['url'])
    if parsed.scheme != 'https' or parsed.hostname != host or parsed.username or parsed.password:
        raise ValueError('unexpected_redirect')
    return result


def _candidates(roster, chamber, canon, panasonic=None, shimadzu=None):
    tree = html.fromstring(roster)
    elements = tree.xpath('//ul[contains(concat(" ",normalize-space(@class)," ")," CompanyList ")]/li')
    if not elements:
        raise ValueError('roster_layout_missing')
    names = [' '.join(node_text(x).split()) for x in elements]
    index = {_normal(n): n for n in names if re.search(r'有限公司$', n)}
    matches = {}
    for a in html.fromstring(chamber).xpath('//table[contains(@class,"product_info_table")]//a[@href]'):
        name = ' '.join(node_text(a).split())
        url = urljoin(CHAMBER, a.get('href'))
        p = urlsplit(url)
        if (_normal(name) in index and p.scheme == 'https' and p.hostname == 'sznissho.org'
                and not p.username and not p.password and re.match(r'^/20\d\d/\d\d/\d\d/', p.path)):
            matches[_normal(name)] = (index[_normal(name)], url, 'sznissho.org')
    for a in html.fromstring(canon).xpath('//td[contains(@class,"company_td")]/a[@href]'):
        name = ' '.join(node_text(a).split()).rstrip('»').strip()
        url = urljoin(CANON_GROUP, a.get('href'))
        p = urlsplit(url)
        # The corporate index links to HTTP; this exact same host has separately
        # verified HTTPS/robots support. Do not generalize scheme upgrades.
        if (_normal(name) in index and p.hostname == 'www.canon-suzhou.com.cn'
                and not p.username and not p.password and p.path in {'', '/'}):
            https = urlunsplit(('https', p.netloc, p.path or '/', p.query, ''))
            matches[_normal(name)] = (index[_normal(name)], https, 'www.canon-suzhou.com.cn')
    if panasonic:
        for a in html.fromstring(panasonic).xpath('//a[@href]'):
            name = ' '.join(node_text(a).split())
            url = urljoin(PANASONIC_GROUP, a.get('href'))
            p = urlsplit(url)
            if (_normal(name) in index and p.scheme == 'https' and p.hostname == 'panasonic.cn'
                    and not p.username and not p.password and p.path.startswith('/about/panasonic_china/')):
                matches[_normal(name)] = (index[_normal(name)], url, 'panasonic.cn')
    if shimadzu:
        for card in html.fromstring(shimadzu).xpath('//div[contains(concat(" ",normalize-space(@class)," ")," card ")]'):
            headings = card.xpath('./div[contains(concat(" ",normalize-space(@class)," ")," card-header ")]')
            name = ' '.join(node_text(headings[0]).split()) if headings else ''
            for a in card.xpath('.//a[@href]'):
                url = urljoin(SHIMADZU_GROUP, a.get('href'))
                p = urlsplit(url)
                if (_normal(name) in index and p.scheme == 'https' and p.hostname == 'www.shimadzu.com.cn'
                        and not p.username and not p.password and p.path.startswith('/SSM/')):
                    matches[_normal(name)] = (index[_normal(name)], url, 'www.shimadzu.com.cn')
                    break
    return names, [matches[_normal(name)] for name in names if _normal(name) in matches]


def enrich_suzhou(source, limit=3):
    if source.url != ROSTER:
        raise ValueError('directory_entry_mismatch')
    limit = max(1, min(int(limit), 3))
    folder = settings.DATA_DIR / 'raw' / 'directories'
    folder.mkdir(parents=True, exist_ok=True)
    with FileLock(str(settings.DATA_DIR / 'directory-write.lock'), timeout=120):
        roster = _read(ROSTER, 'snd-jp.com')
        chamber = _read(CHAMBER, 'sznissho.org')
        canon = _read(CANON_GROUP, 'www.canon.com.cn')
        groups = []
        provider_errors = []
        for url, host in [(PANASONIC_GROUP, 'panasonic.cn'), (SHIMADZU_GROUP, 'www.shimadzu.com.cn')]:
            try:
                groups.append(_read(url, host)['body'])
            except (FetchError, ValueError) as exc:
                groups.append(None)
                provider_errors.append({'url': url, 'error': str(exc)[:300]})
        names, candidates = _candidates(roster['body'], chamber['body'], canon['body'], *groups)
        if not candidates:
            raise ValueError('no_exact_cross_source_matches')
        # Multiple listing occurrences of a name do not create repeated companies.
        candidates = list(dict.fromkeys(candidates))
        current = type(source).objects.get(pk=source.pk)
        offset = int(current.checkpoint.get('directory_offset', 0)) % len(candidates)
        batch = candidates[offset:offset + limit]
        detail_rows = []
        for name, url, host in batch:
            response = _read(url, host)
            tree = html.fromstring(response['body'])
            nodes = tree.xpath('//article') if host == 'sznissho.org' else tree.xpath('//body')
            text = node_text(nodes[0]) if nodes else ''
            if name not in text or len(text) < 150:
                raise ValueError('detail_mismatch')
            detail_rows.append((name, response['url'], response['body']))
        roster_hash = hashlib.sha256(roster['body']).hexdigest()
        roster_path = folder / f'{roster_hash}.html'
        if not roster_path.exists():
            roster_path.write_bytes(roster['body'])
        prepared = []
        for name, url, body in detail_rows:
            digest = hashlib.sha256(body).hexdigest()
            path = folder / f'{digest}.html'
            if not path.exists():
                path.write_bytes(body)
            prepared.append((name, url, digest, path))
        created = 0
        checked = []
        with transaction.atomic():
            current = type(source).objects.select_for_update().get(pk=source.pk)
            for name, url, digest, path in prepared:
                company, new = Company.objects.get_or_create(name=name)
                created += int(new)
                company = Company.objects.select_for_update().get(pk=company.pk)
                if not company.subscribed:
                    continue
                previous = list(company.discovery_evidence or [])
                for evidence_url, evidence_hash, evidence_path in [
                    (ROSTER, roster_hash, roster_path), (url, digest, path)
                ]:
                    evidence = {'source': source.key, 'url': evidence_url, 'document_hash': evidence_hash,
                                'raw_path': evidence_path.relative_to(settings.DATA_DIR).as_posix(),
                                'quote': name, 'status': 'japan_affiliation_unverified',
                                'checked_at': timezone.now().isoformat(),
                                'directory_basis':'official_zone_list',
                                'identity_verification':'list_only' if evidence_url == ROSTER else 'website_verified'}
                    identity = ('source', 'url', 'document_hash', 'quote', 'status')
                    if not any(isinstance(e, dict) and all(e.get(k) == evidence.get(k) for k in identity) for e in previous):
                        previous.append(evidence)
                company.discovery_evidence = previous
                company.profile = dict(company.profile or {})
                company.profile.setdefault('last_discovered_at', timezone.now().isoformat())
                company.save(update_fields=['discovery_evidence', 'profile', 'updated_at'])
                checked.append(name)
            # A missing group changes candidate ordering; keep the offset until
            # all indices return, so a retry cannot silently skip candidates.
            if not provider_errors:
                current.checkpoint = {**current.checkpoint, 'directory_offset': (offset + len(batch)) % len(candidates)}
                current.save(update_fields=['checkpoint'])
        source.checkpoint = current.checkpoint
    return {'items': [], 'status': 'partial' if provider_errors else 'success', 'next_cursor': None if provider_errors else 1,
            'stats': {'directory': True, 'companies_found': len(candidates), 'roster_rows': len(names),
                      'companies_processed': len(checked), 'companies_new': created, 'details_ok': len(prepared),
                      'details': [{'name': n, 'url': u, 'sha256': d} for n, u, d, _ in prepared],
                      'limit': limit, 'offset': offset, 'document_hash': roster_hash,
                      'discovery_url': ROSTER, 'error_count': len(provider_errors), 'provider_errors': provider_errors,
                      'coverage_complete': False,
                      'crosscheck_batch_complete': not provider_errors and len(candidates) <= limit,
                      'scope': '园区名单和公开企业详情精确名称交叉核验；不是采购或股权确认'}}


def discover_suzhou(source, limit=50):
    from .roster_import import import_roster
    if source.url != ROSTER:
        raise ValueError('directory_entry_mismatch')
    response = _read(ROSTER, 'snd-jp.com')
    tree = html.fromstring(response['body'])
    rows = [{'name': node_text(n)} for n in tree.xpath('//ul[contains(concat(" ",normalize-space(@class)," ")," CompanyList ")]/li')]
    return import_roster(source, rows, response, basis='official_zone_list', limit=limit)
