"""Dated, individually parsed public job descriptions from a Japanese logistics peer."""
from datetime import date, timedelta
import hashlib
from urllib.parse import urljoin, urlsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from filelock import FileLock
from lxml import html

from collectors.sources import node_text, parse_date
from .models import Company
from .public_fetch import fetch

ENTRY = 'https://www.mol-logistics.cn/TalentAcquisition/list.aspx?page=1'
ABOUT = 'https://www.mol-logistics.cn/CompanyProfile/index.aspx'
EMPLOYER = '上海华加国际货运代理有限公司'
MAX_AGE_DAYS = 180


def _read(url):
    result = fetch(url, timeout=24)
    parsed = urlsplit(result['url'])
    if parsed.scheme != 'https' or parsed.hostname != 'www.mol-logistics.cn' or parsed.username or parsed.password:
        raise ValueError('unexpected_redirect')
    return result


def _save_raw(body, folder):
    digest = hashlib.sha256(body).hexdigest()
    path = folder / f'{digest}.html'
    if not path.exists():
        path.write_bytes(body)
    return digest, path


def discover_mol(source):
    if source.url != ENTRY:
        raise ValueError('directory_entry_mismatch')
    folder = settings.DATA_DIR / 'raw' / 'directories'
    folder.mkdir(parents=True, exist_ok=True)
    with FileLock(str(settings.DATA_DIR / 'directory-write.lock'), timeout=120):
        about = _read(ABOUT)
        about_text = node_text(html.fromstring(about['body']).xpath('//body')[0])
        if EMPLOYER not in about_text or '商船三井' not in about_text or '物流' not in about_text:
            raise ValueError('employer_proof_missing')
        pages = []
        first = _read(ENTRY)
        pages.append(first)
        tree = html.fromstring(first['body'])
        next_urls = [urljoin(ENTRY, a.get('href')) for a in tree.xpath('//a[@href]')
                     if urlsplit(urljoin(ENTRY, a.get('href'))).path == '/TalentAcquisition/list.aspx'
                     and urlsplit(urljoin(ENTRY, a.get('href'))).query == 'page=2']
        if next_urls:
            pages.append(_read(next_urls[0]))
        today = timezone.localdate()
        details = []
        raw_pages = []
        for page in pages:
            parsed_page = html.fromstring(page['body'])
            # The employer name is on the company profile, not on each job
            # listing. Require the listing to link back to that profile.
            if not any(urljoin(page['url'], a.get('href')) == ABOUT for a in parsed_page.xpath('//a[@href]')):
                raise ValueError('employer_profile_link_missing')
            digest, path = _save_raw(page['body'], folder)
            raw_pages.append((page['url'], digest, path, page['body']))
            jobs = parsed_page.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," job-list ")]/div[contains(concat(" ",normalize-space(@class)," ")," list ")]')
            if not jobs:
                raise ValueError('job_layout_missing')
            for node in jobs:
                names = node.xpath('.//*[contains(concat(" ",normalize-space(@class)," ")," name ")]')
                dates = node.xpath('.//time')
                name = ' '.join(node_text(names[0]).split()) if names else ''
                raw_date = node_text(dates[0]) if dates else ''
                published = parse_date(raw_date)
                text = node_text(node)
                if not name or not published or len(text) < 80 or not ('岗位职责' in text or '工作职责' in text):
                    raise ValueError('job_detail_incomplete')
                details.append({'name': name, 'published': published, 'text': text, 'url': page['url'],
                                'digest': digest, 'raw': page['body'], 'text_hash': hashlib.sha256(text.encode()).hexdigest()})
        about_hash, about_path = _save_raw(about['body'], folder)
        with transaction.atomic():
            company, new = Company.objects.get_or_create(name=EMPLOYER)
            company = Company.objects.select_for_update().get(pk=company.pk)
            checked = 0
            if company.subscribed:
                previous = list(company.discovery_evidence or [])
                proofs = [(ABOUT, about_hash, about_path, EMPLOYER)]
                paths = {u: p for u, _, p, _ in raw_pages}
                proofs += [(d['url'], d['digest'], paths[d['url']], d['name']) for d in details]
                for url, digest, path, quote in proofs:
                    evidence = {'source': source.key, 'url': url, 'document_hash': digest,
                                'raw_path': path.relative_to(settings.DATA_DIR).as_posix(), 'quote': quote,
                                'status': 'japan_affiliation_unverified', 'checked_at': timezone.now().isoformat()}
                    identity = ('source','url','document_hash','quote','status')
                    if not any(isinstance(e, dict) and all(e.get(k) == evidence.get(k) for k in identity) for e in previous):
                        previous.append(evidence)
                company.discovery_evidence = previous
                company.profile = dict(company.profile or {})
                company.profile.setdefault('last_discovered_at', timezone.now().isoformat())
                company.save(update_fields=['discovery_evidence','profile','updated_at'])
                checked = 1
        items = []
        old = future = 0
        if checked:
            for item in details:
                published = date.fromisoformat(item['published'])
                if published > today:
                    future += 1
                    continue
                if published < today - timedelta(days=MAX_AGE_DAYS):
                    old += 1
                    continue
                ident = hashlib.sha256((item['url']+'\n'+item['name']+'\n'+item['published']).encode()).hexdigest()
                items.append({'external_id': ident, 'url': item['url'],
                              'title': f"{EMPLOYER} · {item['name']} 招聘（发布 {item['published']}，有效性待核）",
                              'text': item['text'], 'html': item['raw'].decode('utf-8','replace'),
                              'kind': 'early_signal', 'access_level': 'public', 'published_date': item['published'],
                              'metadata': {'signal_type': 'job', 'employer_name': EMPLOYER, 'employer_role': '招聘企业',
                                           'position_name': item['name'], 'posting_date_status': 'source_stated',
                                           'directory_detail_hash': item['digest'], 'discovery_url': ENTRY}})
    return {'items': items, 'status': 'success', 'next_cursor': 1,
            'stats': {'directory': True, 'companies_found': 1, 'companies_processed': checked,
                      'companies_new': int(new), 'list_pages': len(pages), 'details_ok': len(details),
                      'details': [{'position': d['name'], 'published': d['published'], 'url': d['url'],
                                   'text_sha256': d['text_hash']} for d in details],
                      'older_than_window': old, 'future_dated': future, 'max_age_days': MAX_AGE_DAYS,
                      'discovery_url': ENTRY, 'error_count': 0, 'coverage_complete': False,
                      'scope': '官网岗位独立正文块共享列表页 URL；发布时间非招聘截止；日系物流同行'}}
