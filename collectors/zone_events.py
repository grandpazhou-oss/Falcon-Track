"""Changzhou zone news: bounded discovery of named Japanese project events."""
import hashlib
import re
from datetime import date
from urllib.parse import urljoin, urlsplit

from lxml import html

from .candidate_state import CandidateLedger, Limits, LimitReached, bounded
from .japanese_public import read
from .sources import node_text, parse_date

BASE = 'https://www.cznd.gov.cn'
ENTRY = BASE + '/class/EFBJILJD'
_PATH = re.compile(r'/html/cznd/20\d{2}/[A-Z]+_\d{4}/(\d+)\.html')
_LIST_PATH = re.compile(r'/html/cznd/20\d{2}/EFBJILJD_\d{4}/(\d+)\.html')
_JAPAN_NAME = re.compile(
    r'日本(?P<name>株式会社[\u4e00-\u9fffA-Za-z0-9&＆・()（）._-]+?)'
    r'(?=的|国内|在|于|项目|正式|创立|将|为|、|，|。|\s)'
    r'|日本(?P<suffix>[\u4e00-\u9fffA-Za-z0-9&＆・()（）._-]{1,30}?株式会社)'
)
_EVENTS = (
    ('construction_started', re.compile(r'正式(?:开工(?:建设)?|启动建设|奠基)|(?:项目|工程|基地|厂房)[^。\n]{0,10}(?:正式)?(?:开工建设|开工|奠基)')),
    ('project_signed', re.compile(r'正式签约|签约(?:仪式)?(?:正式)?(?:举行|落户|落地)|项目(?:完成|正式)?签约|签约新厂')),
    ('operation_started', re.compile(r'正式(?:建成投产|投产|运营)|项目(?:顺利|正式)?(?:投产|投入运营)')),
    ('relocation', re.compile(r'(?:正式|已)(?:完成)?(?:搬迁|迁址)')),
    ('expansion', re.compile(r'(?:正式|已)(?:启动|实施|进行)?(?:扩产|扩建)')),
)


def _official_identity(url, pattern=_PATH):
    parts = urlsplit(url)
    try:
        valid = (parts.scheme == 'https' and parts.hostname == 'www.cznd.gov.cn'
                 and parts.port in (None, 443) and not parts.username and not parts.password
                 and not parts.query and not parts.fragment)
    except ValueError:
        valid = False
    match = pattern.fullmatch(parts.path) if valid else None
    return match.group(1) if match else ''


def listing(body, page):
    tree = html.fromstring(body.decode('utf-8', 'replace'))
    anchors = tree.xpath('//li/a[contains(@href,"/html/cznd/")]')
    if not anchors:
        raise ValueError('changzhou_list_layout_missing')
    rows, seen = [], set()
    for anchor in anchors:
        url = urljoin(ENTRY, anchor.get('href', ''))
        identity = _official_identity(url, _LIST_PATH)
        if not identity or identity in seen:
            continue
        title = (anchor.get('title') or node_text(anchor)).strip()
        if not title:
            continue
        rows.append({'id': identity, 'url': url, 'title': title,
                     'published': parse_date(node_text(anchor.getparent()))})
        seen.add(identity)
    next_path = f'/class/EFBJILJD/{page + 1}'
    return rows, bool(tree.xpath('//a[@href=$href]', href=next_path)), len(anchors)


def _fact(field, value, quote, text):
    if not value or not quote or quote not in text:
        return None
    line = next((i for i, item in enumerate(text.splitlines(), 1) if quote in item), None)
    return {'field': field, 'value': value, 'quote': quote,
            'location': f'article:line:{line}' if line else 'article'}


def _event_date(sentence, published):
    matches = list(re.finditer(r'(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日', sentence))
    if not matches:
        return {'value': None, 'precision': 'unknown', 'status': 'unknown', 'quote': ''}
    match = matches[0]
    year, month, day = match.groups()
    try:
        if year:
            value = date(int(year), int(month), int(day)).isoformat()
            return {'value': value, 'precision': 'day', 'status': 'stated', 'quote': match.group()}
        p = date.fromisoformat(published) if published else None
        if p and p.month == int(month) and 0 <= p.day-int(day) <= 7:
            value = date(p.year, int(month), int(day)).isoformat()
            return {'value': value, 'precision': 'day', 'status': 'derived_same_month', 'quote': match.group()}
        date(2000, int(month), int(day))
        return {'value': f'--{int(month):02}-{int(day):02}', 'precision': 'month_day',
                'status': 'year_unknown', 'quote': match.group()}
    except ValueError:
        return {'value': None, 'precision': 'unknown', 'status': 'invalid', 'quote': match.group()}


def _planned_date(text):
    match = re.search(r'(?:预计|计划|拟|力争|争取)[^。\n]{0,35}?(20\d{2})年(\d{1,2})月(\d{1,2})?(日)?(前)?[^。\n]{0,15}?(?:投产|运营|运行|建成|搬迁)', text)
    if not match:
        return {'value': None, 'precision': 'unknown', 'status': 'unknown', 'quote': ''}
    year, month, day, _, before = match.groups()
    try:
        if day:
            value = date(int(year), int(month), int(day)).isoformat()
            precision = 'day_or_before' if before else 'day'
        else:
            date(int(year), int(month), 1)
            value = f'{year}-{int(month):02}'
            precision = 'month_or_before' if before else 'month'
        return {'value': value, 'precision': precision, 'status': 'planned', 'quote': match.group()}
    except ValueError:
        return {'value': None, 'precision': 'unknown', 'status': 'invalid', 'quote': match.group()}


def detail(body, candidate):
    url = candidate['url']
    identity = _official_identity(url)
    if not identity or identity != str(candidate['id']):
        raise ValueError('changzhou_untrusted_detail_url')
    tree = html.fromstring(body.decode('utf-8', 'replace'))
    content = tree.xpath('//*[@id="czfxcontent"]')
    title_nodes = tree.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," NewsTitle ")]')
    meta = tree.xpath('//*[contains(text(),"发布日期：") or contains(text(),"发布日期:")]')
    if not content or not title_nodes or not meta:
        raise ValueError('changzhou_detail_layout_missing')
    body_text = node_text(content[0])
    if len(body_text) < 30:
        raise ValueError('changzhou_detail_too_short')
    published_match = re.search(r'发布日期\s*[:：]\s*20\d{2}-\d{1,2}-\d{1,2}', node_text(meta[0]))
    if not published_match:
        raise ValueError('changzhou_published_date_missing')
    published_quote = published_match.group()
    published = parse_date(published_quote)
    if not published:
        raise ValueError('changzhou_published_date_invalid')
    text = published_quote + '\n' + body_text
    sentences = [part.strip() for part in re.split(r'(?<=。)|\n', body_text) if part.strip()]
    event = parent_name = parent_quote = affiliation_quote = event_sentence = None
    for sentence in sentences:
        names = list(_JAPAN_NAME.finditer(sentence))
        if len(names) == 1:
            name = names[0]
            # A Japanese equipment supplier is not automatically the project owner.
            if re.search(r'(?:采购|购买|引进|采用|使用)[^，。]{0,10}$', sentence[:name.start()]):
                continue
            tentative_name = name.group('name') or name.group('suffix')
            tentative_quote = name.group()
            nationality = name.group()
        elif not names:
            # A named 株式会社 plus an explicit same-name nationality claim may
            # occur in separate paragraphs, without a brand whitelist.
            named = list(re.finditer(r'(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9&._-]{1,25}株式会社)', sentence))
            if len(named) != 1:
                continue
            tentative_name = named[0].group(1)
            tentative_quote = tentative_name
            alias = re.escape(tentative_name.removesuffix('株式会社'))
            nationality = next((s for s in sentences if re.search(r'(?<![A-Za-z0-9])'+alias+r'(?![A-Za-z0-9])', s)
                                and re.search(r'日本(?:优质)?企业|总部(?:位于|设在)日本', s)), '')
            if not nationality:
                continue
        else:
            continue
        for event_type, pattern in _EVENTS:
            match = pattern.search(sentence)
            if (match and re.search(r'常州|新北|滨开区|中国(?:大陆|境内|首个|工厂|生产基地)', sentence[:match.end()])
                    and not re.search(r'(?:预计|计划|拟|将|力争|争取)[^，。]{0,15}$', sentence[:match.start()])):
                event = (event_type, match.group())
                parent_name, parent_quote, affiliation_quote, event_sentence = tentative_name, tentative_quote, nationality, sentence
                break
        if event:
            break
    if not event:
        return None
    entities = [{'name': parent_name, 'role': 'japanese_parent', 'relationship_type': 'unknown'}]
    chinese = re.search(r'(?:生产基地|工厂|项目)(?:——|—|为|是)\s*([\u4e00-\u9fffA-Za-z0-9()（）]{2,45}?(?:有限责任公司|有限公司))', event_sentence)
    if chinese:
        entities.append({'name': chinese.group(1), 'role': 'china_entity', 'relationship_type': 'unknown'})
    dates = {'published_at': {'value': published, 'precision': 'day', 'status': 'stated', 'quote': published_quote},
             'event_date': _event_date(event_sentence, published), 'planned_date': _planned_date(body_text)}
    facts = []
    for field, value, quote in (
        ('japanese_parent', parent_name, parent_quote),
        ('japanese_affiliation', 'official_statement', affiliation_quote),
        ('china_entity', chinese.group(1) if chinese else '', chinese.group() if chinese else ''),
        ('event_type', event[0], event_sentence),
        ('published_at', published, published_quote),
        ('event_date', dates['event_date']['value'], dates['event_date']['quote']),
        ('planned_date', dates['planned_date']['value'], dates['planned_date']['quote']),
    ):
        fact = _fact(field, value, quote, text)
        if fact:
            facts.append(fact)
    paragraph = next((line for line in body_text.splitlines() if event_sentence in line), event_sentence)
    location = re.search(r'(?:于|在)((?:常州|新北|滨开区)[^，。]{1,35}?)(?=正式|开工|启动|签约|完成|落地)', event_sentence)
    business = re.search(r'(?:主营|主要产品(?:为|是))[^。\n]{2,80}', paragraph)
    investment = re.search(r'(?:项目)?(?:总|首期)投资(?:超|约)?[\d,.]+(?:亿|万)?(?:美元|人民币|元)', paragraph)
    for field, match in (('project_location', location), ('business_description', business),
                         ('project_investment', investment)):
        if match:
            value = match.group(1) if field == 'project_location' else match.group()
            fact = _fact(field, value, match.group(), text)
            if fact:
                facts.append(fact)
    signal = {
        'event_type': event[0], 'market_region': 'CN-mainland', 'company_scale': 'unknown',
        'entities': entities, 'dates': dates, 'facts': facts, 'is_procurement': False,
        'source_evidence': {'publisher': '常州国家高新区管委会（新北区人民政府）', 'url': url,
                            'document_sha256': hashlib.sha256(body).hexdigest(), 'source_type': 'official_zone'},
        'next_step': '人工核实项目进度、企业关系和物流安排；项目事件不等于物流采购。',
    }
    return {'external_id': identity, 'url': url, 'title': node_text(title_nodes[0]), 'text': text,
            'html': body.decode('utf-8', 'replace'), 'published_date': published, 'kind': 'early_signal',
            'access_level': 'public', 'metadata': {'business_signal': signal,
            'discovery_url': candidate.get('listing', ''), 'schema_version': 1}}


def collect_changzhou_zone(source):
    options = source.options or {}
    plan = Limits(options)
    ledger = CandidateLedger(source.checkpoint, 'changzhou_zone_candidates_v1')
    pages = bounded(options.get('head_pages'), 3, 1, 40)
    start = bounded(options.get('history_start_page'), 2, 2, max(2, pages))
    deep = max(2, min(pages, int(ledger.state.get('next_page', start)))) if pages > 1 else 1
    items, logs, errors, excluded = [], [], [], []
    checked = 0
    stopped = ''
    next_page = deep
    for page in ([1, deep] if deep > 1 else [1]):
        url = ENTRY + (f'/{page}' if page > 1 else '')
        try:
            response, evidence = plan.read(read, url)
            logs.append(evidence)
            rows, more, count = listing(response['body'], page)
            checked += count
            for row in rows:
                ledger.add(row['id'], row['url'], '园区全行业',
                           {'url': url, 'sha256': evidence.get('sha256'), 'raw_path': evidence.get('raw_path')},
                           title=row['title'], published=row['published'], listing=url)
            if page > 1:
                next_page = page + 1 if more and page < pages else 2
        except LimitReached as exc:
            stopped = str(exc)
            break
        except Exception as exc:
            errors.append({'url': url, 'error': type(exc).__name__ + ': ' + str(exc)[:160]})
            break
    candidates = ledger.pending()
    candidates.sort(key=lambda row: (not bool(re.search(r'日本|日资|日企|株式会社', row.get('title', ''))),
                                     not bool(re.search(r'项目|基地|落户|落子|开工|投产|签约|扩建|搬迁|材料|工厂|制造|产业', row.get('title', '')))))
    for candidate in candidates:
        if len(items) >= plan.accepted:
            stopped = stopped or 'accepted_limit'
            break
        try:
            response, evidence = plan.read(read, candidate['url'], detail=True)
            logs.append(evidence)
            final_url = response.get('url', candidate['url'])
            if _official_identity(final_url) != candidate['id']:
                raise ValueError('changzhou_detail_redirect_changed')
            item = detail(response['body'], candidate)
            if item:
                items.append(item)
                ledger.done(candidate, 'ingested')
            else:
                ledger.done(candidate, 'excluded')
                candidate['exclusion_reason'] = '同句未确认具名日本主体与项目事件，留待后续复核'
                excluded.append({'url': candidate['url'], 'reason': candidate['exclusion_reason']})
        except LimitReached as exc:
            stopped = str(exc)
            break
        except Exception as exc:
            if str(exc) in {'time_limit', 'total_request_limit'}:
                stopped = str(exc)
                break
            ledger.failed(candidate, exc)
            errors.append({'url': candidate['url'], 'error': type(exc).__name__ + ': ' + str(exc)[:160]})
    pending = sum(c.get('status') in {'pending', 'retry'} for c in ledger.entries.values())
    return {'items': items, 'status': 'partial' if errors or stopped or pending else 'success', 'next_cursor': 1,
            'collector_checkpoint': ledger.save(next_page=next_page),
            'stats': {'requests': logs, 'error_count': len(errors), 'errors': errors, 'list_rows_checked': checked,
                      'details_attempted': plan.detail_calls, 'details_ok': len(items), 'excluded': excluded,
                      'pending': pending, 'limit_reason': stopped, 'request_calls': plan.calls,
                      'coverage_complete': False,
                      'scope': f'区内要闻首页及轮转前{pages}页；项目新闻不等于物流采购'}}


def _research_record(item, evidence):
    """Bridge an observed page to the evidence-validated research importer."""
    signal = item['metadata']['business_signal']
    digest = signal['source_evidence']['document_sha256']
    if evidence.get('sha256') != digest or not evidence.get('raw_path'):
        raise ValueError('changzhou_source_evidence_missing')
    by_field = {fact['field']: fact for fact in signal['facts']}
    project = by_field['event_type']
    def quote(fact):
        return {'quote': fact['quote'], 'location': fact['location'], 'document_hash': digest}
    participants = []
    for entity in signal['entities']:
        field = entity['role']
        fact = by_field[field]
        participants.append({'name': entity['name'],
                             'role': 'japan_company' if field == 'japanese_parent' else 'china_entity',
                             'relationship': 'unknown', **quote(fact)})
    dates = []
    for key, value in signal['dates'].items():
        if value.get('value') and key in by_field:
            dates.append({'kind': key, 'value': value['value'], 'precision': value['precision'],
                          'status': value['status'], **quote(by_field[key])})
    facts = [{'field': fact['field'], 'value': fact['value'], **quote(fact)} for fact in signal['facts']]
    return {'external_id': 'cznd:' + item['external_id'], 'url': item['url'], 'title': item['title'],
            'text': item['text'], 'document': {'raw_path': evidence['raw_path'], 'sha256': digest, 'type': 'html'},
            'research': {'event_type': signal['event_type'], 'workstream': 'planned_china', 'china_scope': 'mainland',
                         'china_evidence': quote(project), 'participants': participants, 'dates': dates, 'facts': facts,
                         'size': {'value': 'unknown'},
                         'inferences': ['可核实在华项目对应的运输、仓储及供应链安排；项目不代表已采购物流服务。'],
                         'questions': ['企业关系及规模待核，计划进度须以最新实际结果为准。'],
                         'next_step': signal['next_step']}}


def collect_zone(source):
    """Single official column only; other columns are not merged as projects."""
    result = collect_changzhou_zone(source)
    logs = {entry.get('url'): entry for entry in result['stats'].get('requests', [])}
    result['items'] = [_research_record(item, logs.get(item['url'], {})) for item in result['items']]
    result['stats']['project_dedup_scope'] = '仅区内要闻栏目；按公告ID增量，未执行跨栏目项目合并'
    return result
