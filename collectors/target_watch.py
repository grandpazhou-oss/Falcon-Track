"""Manual target-company pilot. No production ORM reads or writes."""
import hashlib
import json
import re
from datetime import date
from pathlib import Path
from uuid import uuid4
from urllib.parse import urljoin, urlsplit
from xml.etree.ElementTree import ParseError

from django.utils import timezone
from filelock import FileLock
from lxml.etree import ParserError

from collectors.cli import atomic_json
from collectors.research_common import read_original
from collectors.target_events import ACTION, ASSET, TITLE_REV, candidate, check_access, compact, document
from radar.public_fetch import check_budget, request_budget
from radar.search_provider import canonical_url, parse_feed
from radar.website_check import public_html_tree


def load_targets(path):
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    companies = config['companies']
    if not 1 <= len(companies) <= 30:
        raise ValueError('配置需要 1–30 家企业')
    ids, names, owners = set(), set(), {}
    for c in companies:
        origin = canonical_url(c['website'])
        def official(url):
            return bool(origin and canonical_url(url) and urlsplit(canonical_url(url))[:2] == urlsplit(origin)[:2])
        if not origin.startswith('https://') or not re.fullmatch('[a-z0-9_-]+', c['id']) or c['id'] in ids:
            raise ValueError('企业编号或官网无效')
        ids.add(c['id'])
        aliases = [compact(name) for name in [c['name'], *c['aliases']]]
        if not all(aliases) or names.intersection(aliases):
            raise ValueError('母公司名称或别名冲突')
        names.update(aliases)
        date.fromisoformat(c['checked_on'])
        if not (official(c['mapping_url']) and official(c['terms_url']) and c['terms_note'] and c['access'] in {'reviewed','hold'}):
            raise ValueError('缺少官网映射或使用条件核查')
        local = set()
        for r in c['relations']:
            key = compact(r['name'])
            if not key or key in local or r['status'] not in {'verified','unknown','conflict'}:
                raise ValueError('法人重复或关系状态无效')
            local.add(key)
            if r['status'] == 'verified':
                if not r['basis'] or not official(r['url']) or key in owners:
                    raise ValueError('已核法人缺依据或存在跨母公司冲突，请标待核')
                owners[key] = c['id']
        if not c['entries']:
            raise ValueError('企业没有公开入口')
        for e in c['entries']:
            if e['kind'] not in {'rss','list','detail','pdf'} or not official(e['url']):
                raise ValueError('入口类型或同源范围无效')
    return companies


def discover(response, kind):
    body = response['body']
    if kind == 'rss':
        rows, total = parse_feed(body, 'issuer_rss', '')
        if not total:
            raise ValueError('rss_empty_or_unrecognized')
        links = [(r.url, r.title+' '+r.snippet) for r in rows]
    else:
        if 'html' not in response['content_type'].lower():
            raise ValueError('unexpected_index_type')
        tree = public_html_tree(body)
        check_access(tree)
        for n in tree.xpath('//header|//nav|//footer|//aside|//script|//style'):
            n.drop_tree()
        links = [(n.get('href'), ' '.join(n.itertext()).strip()) for n in tree.xpath('//a[@href]')]
        if not links:
            raise ValueError('index_empty_or_unrecognized')
    selected = {}
    for href, label in links:
        url = canonical_url(urljoin(response['url'], href))
        if not url or urlsplit(url)[:2] != urlsplit(response['url'])[:2] or url == canonical_url(response['url']):
            continue
        if re.search(r'年報|統合報告書|有価証券報告書|ログイン|会員限定', label):
            continue
        if re.search(r'/(?:login|signin|members?)(?:/|$)|\.(?:zip|exe|mp4)$', urlsplit(url).path, re.I):
            continue
        if not (ACTION.search(label) or (ASSET.search(label) and re.search(r'中国|上海|蘇州|天津|広州|安徽|湖北',label))):
            continue
        selected.setdefault(url, label[:400])
    return list(selected.items())


SALES = {
    '生产迁移或据点整合': ('生产转移可能同时改变设备搬运和零部件交货路线。','哪些线体要搬、何时停线，迁出与接收工厂分别由谁对接？'),
    '仓配或供应链变化': ('仓储或采购网络变化可能影响库存位置、配送半径和补货频率。','哪些货物调整库位或补货频次，新旧仓之间由谁组织调拨？'),
    '扩产或设备投资': ('产能调整可能改变来料批量、仓容和成品发运频次。','哪条产线先放量，来料与成品的高峰周预计在什么时候？'),
    '新设、建设或投产': ('新据点可能先有设备与物料进场，投产后才形成常态收发货。','现场目前到哪个阶段，首批物料和常态出货分别由谁安排？'),
}


def store_event(events, company_id, fact, title, doc):
    # Match versions of the same named project conservatively; possible matches keep separate records.
    url = canonical_url(doc['url'])
    exact = compact(fact['quote'])
    heading = compact(TITLE_REV.sub('', title))
    scope = [e for e in events if e['company_id']==company_id]
    matches, related = [], []
    for event in scope:
        for old in event['evidence']:
            same_url = canonical_url(old['url']) == url
            if same_url and old['sha256'] == doc['sha256']:
                return 'duplicate', event['id']
            same_subject = all(old[k] == fact[k] for k in ('china_entity','place','action'))
            same_title = bool(heading and heading == compact(TITLE_REV.sub('', old['title'])))
            old_date = (old['publication_date'] or {}).get('value')
            new_date = (fact['publication_date'] or {}).get('value')
            copied = (same_title and same_subject and len(exact) >= 24 and old_date and old_date == new_date)
            corrected = same_url and same_title and same_subject and fact['china_entity'] and TITLE_REV.match(title)
            if (old['sha256'] == doc['sha256'] or corrected
                    or (compact(old['quote']) == exact and (same_url or copied))):
                matches.append(event)
                break
            if same_url or same_title or same_subject:
                related.append(event['id'])
    if len(matches) == 1:
        event = matches[0]
        result = 'duplicate'
    else:
        event = {'id': uuid4().hex, 'company_id': company_id, 'sales_status':'待营业核实',
                 'manual_note':'', 'evidence_status':'有原文，待复核',
                 'logistics_inference': SALES[fact['action']][0],
                 'sales_question': SALES[fact['action']][1],
                 'possible_related': sorted(set(related + [e['id'] for e in matches])), 'evidence': []}
        events.append(event)
        result = 'suspected' if event['possible_related'] else 'new'
    if not any(v['sha256']==doc['sha256'] and canonical_url(v['url'])==url for v in event['evidence']):
        event['evidence'].append({**doc, 'title':title, **fact})
    return result, event['id']


def run(companies, output, *, limit, details, references=False, configured=None):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    state_path = output/'target-events.json'
    if any(output.iterdir()) and not state_path.is_file():
        raise ValueError('请指定空目录或已有的本试点输出目录')
    with FileLock(str(output/'.target-watch.lock'), timeout=0):
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'format':'target-watch-v1','events':[]}
        if state.get('format') != 'target-watch-v1':
            raise ValueError('输出目录格式不符')
        atomic_json(state_path, state)
        now = timezone.now()
        run_id = now.strftime('%Y%m%dT%H%M%S%fZ')+'-'+uuid4().hex[:6]
        report = {'id':run_id, 'started_at':now.isoformat(), 'as_of':timezone.localdate().isoformat(),
                  'entries':[], 'reads':[], 'capabilities':{'rss':'固定订阅源，不执行搜索','list':'仅当前页，最多一层详情',
                  'detail':'同源 HTML','pdf':'同源 PDF 文本层','historical_pagination':False,'query_search':False}}
        stats = dict.fromkeys(('attempted_sources','requests','successful_reads','documents','candidate_events',
                               'new_events','duplicates','suspected','failures','unknown','terms_holds','budget_skips'), 0)
        stats.update(configured_companies=configured if configured is not None else len(companies), selected_companies=len(companies))
        periods = {k:0 for k in ('historical','historical_disclosure','elapsed_plan','recent','upcoming','unknown')}
        seen = {}
        errors = (ValueError, OSError, ParseError, ParserError)

        def process(c, kind, url, label, entry):
            key = (c['id'], canonical_url(url), kind in {'list','rss'})
            if key in seen:
                entry['reused_urls'].append(url)
                return seen[key]
            check_budget()
            row = {'company_id':c['id'], 'url':url, 'kind':kind, 'entry_url':entry['url']}
            report['reads'].append(row)
            try:
                response, doc = read_original(url, directory=output, same_origin=True, max_bytes=5_000_000, timeout=15)
                stats['successful_reads'] += 1
                doc['fetched_at'] = timezone.now().isoformat()
                row['document'] = doc
                if hashlib.sha256((output/doc['raw_path']).read_bytes()).hexdigest()!=doc['sha256']:
                    raise ValueError('archive_hash_mismatch')
                if kind in {'list','rss'}:
                    children = discover(response, kind)
                    row.update(status='index', eligible_links=len(children), unvisited=max(0,len(children)-details))
                    seen[key] = children[:details]
                else:
                    title, blocks, pub, partial = document(response, doc, output, kind)
                    stats['documents'] += 1
                    fact, reason = candidate(c, title, blocks, pub, date.fromisoformat(report['as_of']))
                    row.update(status='no_match', reason=reason, partial=partial)
                    if fact:
                        status, event_id = store_event(state['events'], c['id'],fact,title or label,doc)
                        row.update(status=status,event_id=event_id, time_group=fact['time_group'])
                        periods[fact['time_group']] += 1
                        stats['candidate_events'] += 1
                        stats['duplicates' if status=='duplicate' else 'new_events'] += 1
                        stats['suspected'] += int(status=='suspected')
                        expected = ['china_entity', 'publication_date']
                        if fact['stage'] == '计划':
                            expected.append('planned_date')
                        elif fact['stage'] != '待核':
                            expected.append('actual_date')
                        row['unknown_fields'] = [k for k in expected if not fact[k]]
                        if fact['stage'] == '待核':
                            row['unknown_fields'].append('stage')
                        stats['unknown'] += int(bool(row['unknown_fields']))
                    seen[key] = []
                return seen[key]
            except errors as exc:
                row.update(status='failed', reason=str(exc)[:200])
                stats['failures'] += 1
                raise

        with request_budget(limit, min(600, limit*18+5)) as budget:
            for c in companies:
                for e in c['entries']:
                    entry = dict(e, company_id=c['id'], reused_urls=[])
                    report['entries'].append(entry)
                    if e['role']=='historical_reference' and not references:
                        entry['status'] = 'reference_not_requested'
                        continue
                    if c['access'] != 'reviewed':
                        entry.update(status='terms_hold', reason=c['terms_note'])
                        stats['terms_holds'] += 1
                        continue
                    try:
                        check_budget()
                    except ValueError as exc:
                        entry.update(status='budget_skipped', reason=str(exc))
                        stats['budget_skips'] += 1
                        continue
                    stats['attempted_sources'] += 1
                    try:
                        children = process(c,e['kind'],e['url'],'',entry)
                        for url, label in children:
                            process(c,'pdf' if urlsplit(url).path.lower().endswith('.pdf') else 'detail',url,label,entry)
                        entry['status'] = 'complete'
                    except errors as exc:
                        entry.update(status='stopped', reason=str(exc)[:200])
            stats['requests'] = budget['used']
        report.update(stats=stats, time_groups=periods)
        atomic_json(state_path,state)
        runs = output/'runs'
        runs.mkdir(exist_ok=True)
        atomic_json(runs/(run_id+'.json'), report)
        return report
