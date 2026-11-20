"""Bounded discovery of source-language bank disclosures."""
import re
from datetime import date
from unicodedata import normalize
from urllib.parse import urlencode, urljoin, urlsplit

from collectors.china_entry_evidence import article, counters, failure, read
from collectors.issuer_ir_pdf import retain_original
from collectors.jbic_sme import ENTRY, TERMS, parse_jbic_index
from collectors.sources import node_text, parse_date
from radar.china_places import project_city
from radar.search_provider import canonical_url
from radar.website_check import public_html_tree

BANK = 'https://www.chiginkyo.or.jp/regional_banks/news/index.php'
SHOKO_NEWS = 'https://www.shokochukin.co.jp/newsrelease/'
SHOKO_ARCHIVE = urljoin(SHOKO_NEWS, f'{date.today().year - 1}/')
SHOKO_SUPPORT = 'https://www.shokochukin.co.jp/corporation/service/support/overseas/'
SITES = {
    'chiginkyo_news': ('日本地方银行协会新闻', (BANK,)),
    'shokochukin_news': ('商工中金新闻与支持案例', (SHOKO_NEWS, SHOKO_ARCHIVE, SHOKO_SUPPORT)),
    'jbic_sme': ('日本国际协力银行中小企业公告', (ENTRY,)),
}
PURPOSE = re.compile(r'運転資金|設備(?:増強|投資|資金)|新工場|仕入(?:体制を強化|資金)|集荷|生産能力|現地法人(?:の)?設立')
TOPIC = re.compile(PURPOSE.pattern + r'|現地法人|海外展開|スタンドバイ|保証|融資|商談会')
JAPAN_REGION = re.compile(r'中国(?:地方|地区|経済産業局)|鳥取|島根|岡山|広島|山口')
MAINLAND = re.compile(r'中華人民共和国|中国(?:大陸|本土|現地|(?:向け|産|法人|市場|進出)|における)')
COMPANY = r'(?:株式会社|有限会社|合同会社|合資会社|合名会社)'
PARENT = re.compile(rf'({COMPANY}[^()（）\n、。]{{1,55}}|[^()（）\s、。は]{{1,45}}{COMPANY})[（(]\s*本社[：:]')
CHILD = re.compile(r'(?:中国現地法人|中国法人)\s*[「『]?([^\n、。]{2,95}?(?:有限公司|(?:CO\.?,?\s*)?LTD\.?))', re.I)
CONTRACT = re.compile(r'本契約は[\s\S]{0,25}?締結|契約締結日\s*[:：]?\s*20\d\d\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日')
EVENT = re.compile(r'(?:合同|共同)?(?:ビジネス)?商談会')


def mainland_location(text):
    clean = JAPAN_REGION.sub('', text)
    return project_city(clean) or ('中国大陆' if MAINLAND.search(clean) else '')


def event_identity(text):
    """Use the meeting date, city and edition across the issuing banks."""
    text = normalize('NFKC', text)
    city = mainland_location(text)
    if not city or city == '中国大陆' or not EVENT.search(text):
        return ''
    for line in re.split(r'\n|。', text):
        if not re.search(r'開催\s*日|日\s*時|開催\s*[:：]|商談会', line):
            continue
        match = re.search(r'20\d\d\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日', line)
        if not match:
            continue
        day = parse_date(re.sub(r'\s+', '', match[0]))
        if not day:
            continue
        return f'joint-business-meeting:{city}:{day}'
    return ''


def parse_finance(text, published, as_of):
    parent = PARENT.search(text)
    child = CHILD.search(text, parent.end()) if parent else None
    event = event_identity(text)
    if not parent or not child:
        if event:
            return {'region': mainland_location(text), 'event_key': event, 'historical_case': True,
                    'source_scope': 'finance_meeting', 'evidence_quote': text[:450]}
        return None
    city = mainland_location(child[0])
    paragraph = re.sub(r'\s+', '', text[parent.start():child.end()+290])
    purpose = PURPOSE.search(paragraph)
    if not city or not purpose or not published:
        return None
    company = parent[1].strip()
    section = re.search(r'【\s*'+re.escape(company)+r'の概要\s*】([^【]+)', text)
    employees = re.search(r'従業員数\s*([\d,]+)\s*名', section[1]) if section else None
    contract = CONTRACT.search(text)
    contract_date = parse_date(re.sub(r'\s+', '', contract[0])) if contract else None
    purposes = list(dict.fromkeys(PURPOSE.findall(paragraph)))
    procurement = re.search(r'中国産[^。\n]{0,24}仕入体制を強化', text)
    if procurement:
        purposes.append(procurement[0])
    return {
        'company': company, 'china_entity': child[1], 'region': city,
        'employees': employees[1] if employees else '', 'finance_purpose': '／'.join(purposes),
        'contract_date': contract_date, 'source_scope': 'finance_support',
        'evidence_quote': text[parent.start():child.end()+150].strip(),
        'historical_case': (as_of-date.fromisoformat(contract_date or published)).days > 365,
    }


def index_links(body, base, key):
    found = {}
    for anchor in public_html_tree(body).xpath('//a[@href]'):
        url = canonical_url(urljoin(base, anchor.get('href')))
        label = node_text(anchor)
        if not url or not label:
            continue
        parts = urlsplit(url)
        if key == 'shokochukin_news' and (parts.hostname != urlsplit(SHOKO_NEWS).hostname
                                         or not parts.path.startswith(('/newsrelease/', '/assets/pdf/', '/corporation/service/support/overseas/'))):
            continue
        if TOPIC.search(label) and (mainland_location(label) or re.search(r'海外展開|現地法人', label)):
            found.setdefault(url, label)
    return found


def bank_searches(body, base):
    tree = public_html_tree(body)
    for form in tree.xpath('//form[.//input[@name="freeword"]]'):
        action = canonical_url(urljoin(base, form.get('action') or base))
        if form.get('method', 'get').lower() == 'get' and action == BANK:
            return [action + '?' + urlencode({'page': 1, 'freeword': word})
                    for word in ('上海', '中国現地法人', '深圳')]
    return []


def shoko_news_target(body, base):
    for meta in public_html_tree(body).xpath('//meta[@http-equiv and @content]'):
        if meta.get('http-equiv').lower() != 'refresh':
            continue
        match = re.fullmatch(r'\s*0\s*;\s*url\s*=\s*(.+?)\s*', meta.get('content'), re.I)
        if not match:
            continue
        target = urlsplit(urljoin(base, match[1].strip('"\'')))
        if (target.scheme == 'https' and target.netloc == urlsplit(SHOKO_NEWS).netloc
                and re.fullmatch(r'/newsrelease/20[0-9]{2}/', target.path) and not target.query):
            return target.geturl()
    return ''


def collect(key, stats, targets, limit, as_of, prior_events):
    rows = []
    attempted = set()
    events = set(prior_events)
    indexes = list(targets)
    details_read = stats.setdefault('details_read', [])
    examined_meetings = set()
    candidates = {}
    for index in indexes:
        try:
            response, _ = read(index, stats, probe=True)
            if key == 'jbic_sme':
                listed = parse_jbic_index(response['body'], as_of=as_of)
                counters(stats, index, len(listed), 0)
                stats['errors'].append({'url': index, 'status': 'consultation_required',
                                        'reason': 'detail_archiving_requires_prior_consultation', 'terms': TERMS})
                continue
            if key == 'chiginkyo_news' and index == BANK:
                indexes[1:1] = bank_searches(response['body'], response['url'])
            if key == 'shokochukin_news' and index == SHOKO_NEWS:
                target = shoko_news_target(response['body'], response['url'])
                if target and target not in indexes:
                    indexes.insert(1, target)
            entries = index_links(response['body'], response['url'], key)
            counters(stats, index, len(entries), 0)
            for url, title in entries.items():
                candidates.setdefault(url, (title, stats['endpoints'][-1]))
        except (ValueError, OSError) as exc:
            failure(stats, index, exc)

    ranked = sorted(candidates.items(), key=lambda row: not mainland_location(row[1][0]))
    for url, (title, endpoint) in ranked:
        if len(attempted) >= limit:
            break
        normalized_title = re.sub(r'[^\w]', '', normalize('NFKC', title))
        if url in indexes or url in attempted or normalized_title in examined_meetings:
            continue
        attempted.add(url)
        try:
            response, doc = read(url, stats, probe=True)
            item = article(response, doc, title)
            details_read.append({'url': doc['url'], 'sha256': doc.get('sha256', ''),
                                 'characters': len(item['text']), 'pages': len(item['pages'])})
            published = item['published'] or parse_date(re.sub(r'\s+', '', '\n'.join(item['text'].splitlines()[:10])))
            published = published or parse_date(title)
            item['published'] = published
            parsed = parse_finance(item['text'], published, as_of)
            if item['pages'] and parsed:
                parsed['pdf_page'] = next((page['page'] for page in item['pages']
                                           if parsed.get('china_entity') and parsed['china_entity'] in page['text']), None)
            if not parsed:
                failure(stats, url, ValueError('mainland_company_and_finance_evidence_missing'))
                continue
            event = parsed.get('event_key')
            if event:
                examined_meetings.add(normalized_title)
            if event and event in events:
                stats['errors'].append({'url': url, 'status': 'duplicate_event', 'reason': event})
                continue
            if event:
                events.add(event)
            retain_original(doc)
            rows.append({**item, **parsed})
            endpoint['retained'] += 1
        except (ValueError, OSError) as exc:
            failure(stats, url, exc)
    return rows
