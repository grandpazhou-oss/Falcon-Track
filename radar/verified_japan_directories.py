"""Public directory adapters that verify each company against its own detail page."""
import hashlib
import re
import unicodedata
from urllib.parse import urljoin, urlsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from filelock import FileLock
from lxml import html

from collectors.sources import node_text
from .models import Company
from .public_fetch import fetch


def _normal(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value or ''))


def _chongqing_listing(body):
    tree = html.fromstring(body)
    cards = tree.xpath('//*[@id="companyList"]//*[contains(concat(" ", normalize-space(@class), " "), " company-card ")]')
    if not cards:
        raise ValueError('directory_layout_no_cards')
    found = []
    for card in cards:
        names = card.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), " company-title ")]')
        buttons = card.xpath('.//button[@onclick]')
        if not names or not buttons:
            continue
        name = ' '.join(node_text(names[0]).split())
        match = re.fullmatch(r"\s*toggleReadMore\(['\"](\d+)['\"]\)\s*;?\s*", buttons[0].get('onclick', ''))
        if match and re.search(r'(?:有限公司|有限会社)(?:[^\s]{0,20}(?:支店|分公司|分行))?$', name):
            found.append((name, 'https://cq-jpclub.com/company-detail?id=' + match[1]))
    return list(dict.fromkeys(found))


def _chongqing_detail(body, name):
    tree = html.fromstring(body)
    nodes = tree.xpath('//div[contains(concat(" ", normalize-space(@class), " "), " content ")]')
    heads = tree.xpath('//h1[contains(concat(" ", normalize-space(@class), " "), " main-heading ")]')
    if not nodes or not heads or _normal(node_text(heads[0])) != _normal(name):
        raise ValueError('detail_mismatch')
    text = node_text(nodes[0])
    if len(text) < 100 or not nodes[0].xpath('.//h2'):
        raise ValueError('detail_incomplete')



def _yingchuang_listing(body, base):
    tree = html.fromstring(body)
    anchors = tree.xpath('//a[contains(concat(" ", normalize-space(@class), " "), " company-title ")][@href]')
    if not anchors:
        raise ValueError('directory_layout_no_cards')
    found = []
    for anchor in anchors:
        name = ' '.join(node_text(anchor).split())
        url = urljoin(base, anchor.get('href', ''))
        parsed = urlsplit(url)
        if (parsed.scheme == 'https' and parsed.hostname == 'www.yingchuang.com'
                and not parsed.username and not parsed.password
                and parsed.path.startswith('/html/zh/special/')
                and re.search(r'有限公司$', name)):
            found.append((name, url))
    return list(dict.fromkeys(found))


def _yingchuang_detail(body, name):
    tree = html.fromstring(body)
    nav = tree.xpath('//*[contains(concat(" ", normalize-space(@class), " "), " nav ")]')
    if not nav or _normal(name) not in _normal(node_text(nav[0])):
        raise ValueError('detail_mismatch')
    jobs = tree.xpath('//*[contains(concat(" ", normalize-space(@class), " "), " jobdetail ")]')
    texts = [node_text(job) for job in jobs]
    if not any(len(text) >= 60 for text in texts):
        raise ValueError('no_public_job_description')
    return texts

def discover_verified(source, limit=3):
    """Advance only after the entire bounded batch and its evidence are saved."""
    if source.key not in {'japan_directory_chongqing', 'japan_directory_yingchuang'}:
        raise ValueError('unsupported_directory')
    chongqing = source.key == 'japan_directory_chongqing'
    allowed_host = 'cq-jpclub.com' if chongqing else 'www.yingchuang.com'
    entry = urlsplit(source.url)
    if entry.scheme != 'https' or entry.hostname != allowed_host or entry.username or entry.password:
        raise ValueError('directory_entry_mismatch')
    limit = max(1, min(int(limit), 3))
    folder = settings.DATA_DIR / 'raw' / 'directories'
    folder.mkdir(parents=True, exist_ok=True)
    with FileLock(str(settings.DATA_DIR / 'directory-write.lock'), timeout=120):
        listed = fetch(source.url)
        if urlsplit(listed['url']).hostname != allowed_host:
            raise ValueError('directory_redirect_mismatch')
        candidates = _chongqing_listing(listed['body']) if chongqing else _yingchuang_listing(listed['body'], source.url)
        if not candidates:
            raise ValueError('directory_layout_no_entities')
        locked = type(source).objects.get(pk=source.pk)
        offset = int(locked.checkpoint.get('directory_offset', 0)) % len(candidates)
        batch = candidates[offset:offset + limit]
        details = []
        skipped = []
        for name, url in batch:
            response = fetch(url)
            parsed = urlsplit(response['url'])
            expected = urlsplit(url)
            if parsed.hostname != allowed_host or parsed.path != expected.path or parsed.query != expected.query:
                raise ValueError('detail_redirect_mismatch')
            digest = hashlib.sha256(response['body']).hexdigest()
            try:
                jobs = (_chongqing_detail if chongqing else _yingchuang_detail)(response['body'], name)
            except ValueError as exc:
                if chongqing:
                    raise
                skipped.append({'name': name, 'url': url, 'sha256': digest, 'reason': str(exc)})
                continue
            path = folder / f'{digest}.html'
            if not path.exists():
                path.write_bytes(response['body'])
            details.append((name, url, digest, path, jobs, response['body']))
        list_hash = hashlib.sha256(listed['body']).hexdigest()
        list_path = folder / f'{list_hash}.html'
        if not list_path.exists():
            list_path.write_bytes(listed['body'])
        created = 0
        checked = []
        items = []
        with transaction.atomic():
            locked = type(source).objects.select_for_update().get(pk=source.pk)
            for name, url, digest, path, jobs, raw in details:
                company, new = Company.objects.get_or_create(name=name)
                created += int(new)
                company = Company.objects.select_for_update().get(pk=company.pk)
                if not company.subscribed:
                    continue
                evidence = {'source': source.key, 'url': url, 'document_hash': digest,
                            'raw_path': path.relative_to(settings.DATA_DIR).as_posix(), 'quote': name,
                            'status': 'japan_affiliation_unverified', 'discovery_url': source.url,
                            'checked_at': timezone.now().isoformat()}
                previous = list(company.discovery_evidence or [])
                identity = ('source', 'url', 'document_hash', 'quote', 'status')
                if not any(isinstance(e, dict) and all(e.get(k) == evidence.get(k) for k in identity) for e in previous):
                    previous.append(evidence)
                company.discovery_evidence = previous
                company.profile = dict(company.profile or {})
                company.profile.setdefault('last_discovered_at', timezone.now().isoformat())
                company.save(update_fields=['discovery_evidence', 'profile', 'updated_at'])
                checked.append(name)
                if jobs:
                    for job in jobs[:30]:
                        if not re.search(r'物流|关务|仓库|仓储|SCM|供应链|进出口|危险品|危化|冷链|运输', job, re.I):
                            continue
                        position = job.splitlines()[0].strip()[:160]
                        if not position:
                            continue
                        identifier = hashlib.sha256((url + '\n' + _normal(position)).encode()).hexdigest()
                        items.append({'external_id': identifier, 'url': url,
                                      'title': f'{name} · {position} 招聘（发布时间未知）',
                                      'text': f'招聘企业：{name}\n{job}', 'html': raw.decode('utf-8', 'replace'),
                                      'kind': 'early_signal', 'access_level': 'public', 'published_date': None,
                                      'metadata': {'signal_type': 'job', 'employer_name': name,
                                                   'employer_role': '招聘企业', 'position_name': position,
                                                   'posting_date_status': 'unknown', 'directory_detail_hash': digest,
                                                   'discovery_url': source.url}})
            next_offset = (offset + len(batch)) % len(candidates)
            if not getattr(source, '_defer_directory_checkpoint', False):
                locked.checkpoint = {**locked.checkpoint, 'directory_offset': next_offset}
                locked.save(update_fields=['checkpoint'])
        source.checkpoint = locked.checkpoint
    return {'items': items, 'status': 'success', 'next_cursor': 1,
            'stats': {'directory': True, 'companies_found': len(candidates), 'companies_processed': len(checked),
                      'companies_new': created, 'details_ok': len(details), 'details': [
                          {'name': n, 'url': u, 'sha256': d} for n, u, d, _, _, _ in details],
                      'limit': limit, 'skipped_details': skipped, 'document_hash': list_hash, 'discovery_url': source.url,
                      'error_count': 0, 'coverage_complete': len(candidates) <= limit and not skipped, 'offset': offset,
                      'next_directory_offset': next_offset,
                      'scope': '企业介绍或招聘专题，非物流采购；名录关联及岗位时效待核实'}}
