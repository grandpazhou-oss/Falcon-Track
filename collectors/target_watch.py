"""Manual target-company pilot. No production ORM reads or writes."""
import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from collectors.target_events import TITLE_REV, check_access, compact
from radar.search_provider import canonical_url, parse_feed
from radar.website_check import public_html_tree


def load_targets(path):
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    companies = config['companies']
    if not 1 <= len(companies) <= 30:
        raise ValueError('配置需要 1–30 家企业')
    ids, names, owners = set(), set(), {}
    for c in companies:
        modern = 'company_id' in c
        if modern:
            c['id'], c['name'] = c['company_id'], c['name_ja']
            for field in ('name_zh','industry','owner_sales','china_entities','edinet_code','parent_edinet_code'):
                if field not in c:
                    raise ValueError('企业配置缺少 '+field)
            if any(v is not None and not re.fullmatch(r'E[0-9]{5}',v)
                   for v in (c['edinet_code'],c['parent_edinet_code'])):
                raise ValueError('EDINET 提交者代码无效')
            if not isinstance(c['china_entities'],list) or not all(isinstance(n,str) and n.strip() for n in c['china_entities']):
                raise ValueError('在华法人或别名无效')
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
        if not (official(c['mapping_url']) and official(c['terms_url']) and c['terms_note'] and (modern or c['access'] in {'reviewed','hold'})):
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
        entry_ids = set()
        for index, e in enumerate(c['entries']):
            if not modern:
                e.update(entry_id=str(index),source_type=e['kind'],
                         compliance_mode='full_archive' if c['access']=='reviewed' else 'manual_watch',
                         terms_reviewed_at=c['checked_on'],terms_note=c['terms_note'])
            mode, kind = e['compliance_mode'], e['source_type']
            if mode not in {'full_archive','statutory_api','metadata_pointer','manual_watch'}:
                raise ValueError('未知合规档位')
            date.fromisoformat(e['terms_reviewed_at'])
            if not e['terms_note'] or not re.fullmatch('[a-zA-Z0-9_-]+',e['entry_id']) or e['entry_id'] in entry_ids:
                raise ValueError('入口编号或条款记录无效')
            entry_ids.add(e['entry_id'])
            u=urlsplit(e['url'])
            # Only this government company profile can be an off-site pointer index.
            gbiz = (u[:3]==('https','info.gbiz.go.jp','/hojin/ichiran') and
                    re.fullmatch(r'hojinBango=[0-9]{13}',u.query) and kind=='list' and mode=='metadata_pointer')
            if mode=='statutory_api':
                if kind!='edinet' or e['url']!='https://api.edinet-fsa.go.jp/api/v2/documents.json' or not (c.get('edinet_code') or c.get('parent_edinet_code')):
                    raise ValueError('法定披露入口或代码无效')
            elif (not official(e['url']) and not gbiz or kind not in {'rss','list','detail','pdf'}
                  or mode=='metadata_pointer' and kind not in {'rss','list'}
                  or 'tdnet' in u.netloc.lower() or u.netloc.lower().endswith('jpx.co.jp') and 'tdnet' in e['url'].lower()):
                raise ValueError('入口类型或同源范围无效')
            e['kind']=kind
        c.setdefault('china_entities',[r['name'] for r in c['relations'] if r['status']=='verified'])
    return companies


def discover(response, kind, entry=None):
    """Yield title, same-origin link and a separately evidenced publication date."""
    if kind == 'rss':
        rows, total = parse_feed(response['body'], 'issuer_rss', '')
        if not total:
            raise ValueError('rss_empty_or_unrecognized')
        links = [(r.url,r.title,r.published_at[:10]) for r in rows]
    else:
        if 'html' not in response['content_type'].lower():
            raise ValueError('unexpected_index_type')
        tree = public_html_tree(response['body'])
        check_access(tree)
        for node in tree.xpath('//header|//nav|//footer|//aside|//script|//style'):
            node.drop_tree()
        links = []
        entry = entry or {}
        for node in tree.xpath(entry.get('links_xpath','//a[@href]')):
            href = node.get('href').strip()
            if not href or href.startswith('#'):
                continue
            headings = node.xpath(entry['title_xpath']) if entry.get('title_xpath') else []
            title = ' '.join((headings or [node])[0].text_content().split())
            container = node.xpath('ancestor::*[self::li or self::tr or self::dd][1]')
            context = container[0] if container else node
            dates = context.xpath('.//time/@datetime|preceding-sibling::dt[1]//time/@datetime|preceding-sibling::dt[1]//text()')
            dates.append(' '.join(context.text_content().split())[:45])
            stamp = next((m.groups() for value in dates if (m:=re.search(r'(20\d{2})[./年-](\d{1,2})[./月-](\d{1,2})',value))), None)
            day = '-'.join(v.zfill(2) for v in stamp) if stamp else ''
            links.append((href,title,day))
        if not links:
            raise ValueError('index_empty_or_unrecognized')
    selected = {}
    for href, title, day in links:
        url=canonical_url(urljoin(response['url'],href))
        if not title or not url or urlsplit(url)[:2]!=urlsplit(response['url'])[:2] or url==canonical_url(response['url']):
            continue
        if re.search(r'/(?:login|signin|members?)(?:/|$)|\.(?:zip|exe|mp4)$',urlsplit(url).path,re.I):
            continue
        try:
            day=date.fromisoformat(day).isoformat()
        except ValueError:
            day=''
        selected.setdefault(url,{'url':url,'title':title[:600],'published_date':day})
    return list(selected.values())


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
