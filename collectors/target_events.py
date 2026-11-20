"""Conservative evidence selection for the manual issuer watchlist."""
import re
import unicodedata
from datetime import date, timedelta

from collectors.research_common import pdf_pages
from radar.china_places import ALIASES, CITY
from radar.website_check import public_html_tree


def compact(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value))


ACTIONS = {
    '生产迁移或据点整合': r'移管|移転|集約|再編|統廃合|統合|閉鎖',
    '仓配或供应链变化': r'(?:倉庫|物流センター|配送拠点|在庫|調達|供給網).{0,15}(?:新設|増設|開設|拡充|切替|見直し|変更|増強|削減)',
    '扩产或设备投资': r'増産|増設|増強|設備投資|生産能力.{0,8}(?:拡大|拡充)',
    '新设、建设或投产': r'新設|設立|開設|起工|着工|竣工|稼働(?:を)?開始|操業開始|新工場.{0,10}建設',
}
ACTION = re.compile('|'.join(ACTIONS.values()))
ASSET = re.compile(r'工場|生産|製造|設備|拠点|倉庫|物流|配送|在庫|供給|調達|法人|子会社')
FOREIGN = re.compile(r'中国地方|中国地区|中国四国|中国経済産業局|中国電力|四国地方|鳥取|島根|岡山|広島|山口|台湾|香港|マカオ|ベトナム|タイ国|タイ工場|米国|メキシコ')
DATE = re.compile(r'(?<!\d)((?:19|20)\d\d)年(?!度)(?:(\d{1,2})月(?:(\d{1,2})日)?)?')
PLAN = re.compile(r'予定|計画(?:して|です|中)|見込|目指|する方針|までに.{0,35}(?:します|いたします)')
DONE = re.compile(r'しました|いたしました|行いました|完了|開始した|移管した|設立した|竣工した|してきました')
TITLE_REV = re.compile(r'^[【（(](?:訂正|一部訂正|修正|更新)[】）)]\s*')
RESTRICTED = re.compile(r'captcha|verify you are human|ログインが必要|会員限定記事|購読者限定|アクセスが集中|access denied', re.I)


def event_stage(text):
    if re.search(r'検討(?:中|して|を開始)|可能性', text):
        return '待核'
    if re.search(r'することを(?:決議|決定)|計画を(?:発表|決議)', text):
        return '计划'
    planned, done = bool(PLAN.search(text)), bool(DONE.search(text))
    if planned or not done:
        return '计划' if planned and not done else '待核'
    for pattern, stage in ((r'起工|着工','已开工'), (r'竣工','已竣工'),
                           (r'(?:稼働|操業|生産)(?:を)?開始','已开始运营')):
        if re.search(pattern,text):
            return stage
    return '已披露发生'


def check_access(tree):
    title = tree.xpath('string(//title)')
    body = ' '.join(tree.xpath('//body//text()[not(ancestor::script or ancestor::style)]'))
    if (tree.xpath('//input[translate(@type,"PASSWORD","password")="password"]')
            or RESTRICTED.search(title) or RESTRICTED.search(body[:3500])):
        raise ValueError('access_restricted')


def publication(blocks):
    for loc, raw in blocks:
        value = compact(raw)
        value = re.sub(r'^(?:公表日|発表日|公開日|掲載日)[：:]?', '', value)
        found = re.fullmatch(r'((?:19|20)\d\d)[年./-](\d{1,2})[月./-](\d{1,2})日?(?:T[\d:+.Z-]+)?', value)
        if found:
            try:
                day = date(*map(int, found.groups()))
            except ValueError:
                continue
            return {'value': day.isoformat(), 'quote': raw, 'locator': loc}
    return None


def document(response, doc, output, kind):
    body = response['body']
    if body.startswith(b'%PDF-'):
        result = pdf_pages(doc['raw_path'], directory=output)
        pages = [(f"PDF p.{p['page']}", p['text']) for p in result['pages'] if p['text'].strip()]
        if not pages:
            raise ValueError('pdf_no_text_layer')
        # Only the announcement header, never a year found in the project narrative.
        header = [(pages[0][0], line.strip()) for line in pages[0][1].splitlines()[:8]]
        title = next((s.strip() for s in pages[0][1].splitlines()[:24] if 'お知らせ' in s or 'について' in s), '')
        blocks = [(loc, part.strip()) for loc, text in pages
                  for part in re.split(r'\n(?:[ \t]*\n){2,}|(?=＜[^＞\n]{1,55}の概要＞)', text) if part.strip()]
        return title, blocks, publication(header), result['status'] != 'text'
    if kind == 'pdf' or 'html' not in response['content_type'].lower():
        raise ValueError('unexpected_document_type')
    tree = public_html_tree(body)
    check_access(tree)
    for node in tree.xpath('//nav|//footer|//aside|//script|//style|//noscript|//header[not(.//h1 or .//time)]'):
        node.drop_tree()
    root = (tree.xpath('//article|//main|//*[@id="main" or @id="contents"]') or [tree])[0]
    title = ' '.join(root.xpath('.//h1//text()')).strip() or tree.xpath('string(//title)').strip()
    headers = [(tree.getroottree().getpath(n), n.get('content') or n.get('datetime'))
               for n in tree.xpath('//meta[@property="article:published_time"][@content]|//time[@pubdate or @itemprop="datePublished"][@datetime]')]
    blocks = []
    for node in root.xpath('.//p|.//li[not(.//p)]|.//tr|.//h1|.//h2|.//h3|.//time|.//div[not(*)]'):
        if node.xpath('ancestor::tr|ancestor::p') or (node.tag == 'li' and node.xpath('.//li')):
            continue
        raw = node.text_content().strip()
        if raw:
            blocks.append((tree.getroottree().getpath(node), raw))
    if len(''.join(t for _, t in blocks)) < 60:
        raise ValueError('body_incomplete')
    # An unlabeled bare date is accepted only at the top, outside project tables.
    headers.extend((loc, s) for loc, s in blocks[:8] if '/tr' not in loc)
    return title, blocks, publication(headers), False


def project_date(sentence, locator):
    """Use a single date immediately attached to this action; ambiguous dates stay null."""
    normalized = compact(sentence)
    hits = list(DATE.finditer(normalized))
    if len(hits) != 1:
        return None
    hit = hits[0]
    tail = normalized[hit.end():]
    if not re.search(ACTION, tail[:36]) and not re.search(r'(?:予定|開始|竣工|移管)[：:(（]?$', normalized[:hit.start()]):
        return None
    if re.search(r'取締役会|創業|締結|従業員|受賞|決議', tail[:25]) or re.match(r'度|時点|現在|に発表',tail):
        return None
    year, month, day = (int(x) if x else None for x in hit.groups())
    try:
        date(year, 1 if month is None else month, 1 if day is None else day)
    except ValueError:
        return None
    value = str(year) + (f'-{month:02}' if month else '') + (f'-{day:02}' if day else '')
    return {'value': value, 'quote': sentence, 'locator': locator}


def recency(actual, plan, published, today):
    event = actual or plan
    if not event:
        if published and date.fromisoformat(published['value']) < today - timedelta(days=365):
            return 'historical_disclosure'
        return 'unknown'
    value = event['value']
    # Compare intervals rather than inventing a day for a year/month.
    cutoff = (today - timedelta(days=365)).isoformat()[:len(value)]
    if plan and not actual and value < today.isoformat()[:len(value)]:
        return 'elapsed_plan'
    if value < cutoff:
        return 'historical'
    return 'upcoming' if value > today.isoformat()[:len(value)] else 'recent'


def schedule(fact, blocks, relation, today):
    """Bind labeled milestones only inside one local entity/facility overview."""
    groups = {}
    for loc, text in blocks:
        if loc.startswith('PDF'):
            # Each PDF overview has already been split at the next overview heading.
            if not re.match(r'＜[^＞]{0,35}(?:工場|倉庫)の概要＞', text):
                continue
            key = (loc, text[:40])
            lines = text.splitlines()
        elif re.search(r'/(?:ol|ul|table)(?:\[\d+\])?/', loc):
            key = (re.split(r'/(?:li|tbody|tr)\b', loc)[0], '')
            lines = [text]
        else:
            continue
        groups.setdefault(key, []).extend((loc, line.strip()) for line in lines if line.strip())
    matching = []
    for rows in groups.values():
        section = compact('\n'.join(t for _,t in rows))
        if compact(relation['name']) not in section or not re.search(r'所在地', section):
            continue
        addresses = [compact(t) for _,t in rows if compact(t).startswith('所在地')]
        if not any((relation['place'] and compact(relation['place']) in a) or fact['place'] in a for a in addresses):
            continue
        matching.append(rows)
    if len(matching) == 1:
        rows = matching[0]
        for loc, line in rows:
            norm = compact(line)
            match = re.fullmatch(r'(営業開始日?|操業開始日?|生産開始日?|稼働開始日?|竣工|着工|起工)(予定)?[:：]?'
                                 r'((?:19|20)\d{2}年(?:\d{1,2}月(?:\d{1,2}日)?)?)(?:\((予定)\))?',norm)
            if not match:
                continue
            day = DATE.fullmatch(match[3])
            y,m,d = day.groups()
            try:
                date(int(y),int(m or 1),int(d or 1))
            except ValueError:
                continue
            value = y+('-'+m.zfill(2) if m else '')+('-'+d.zfill(2) if d else '')
            is_plan = bool(match[2] or match[4])
            if not is_plan and value > today.isoformat()[:len(value)]:
                continue
            field = 'planned_date' if is_plan else 'actual_date'
            if not fact[field]:
                fact[field] = {'value':value,'milestone':match[1],'quote':line,'locator':loc}
        # A declared site address is useful when the narrative gives only a province.
        for loc, line in rows:
            if relation['place'] and compact(line).startswith('所在地') and compact(relation['place']) in compact(line):
                fact['site_address_evidence'] = {'quote':line,'locator':loc}
                if fact['place'] in relation['place']:
                    fact['place'] = relation['place']
                break


def candidate(company, title, blocks, published, today):
    parent_names = [company['name'], *company['aliases']]
    relations = company['relations']
    # Strict legal names or documented aliases, never fuzzy identity or location alone.
    body = compact('\n'.join(s for _, s in blocks))
    if not any(compact(n) in body for n in parent_names + [r['name'] for r in relations if r['status']=='verified']):
        return None, 'issuer_not_identified'
    choices = []
    unattributed = []
    for loc, block in blocks:
        for piece in re.split(r'[。！？；]', block):
            raw = piece.strip()
            text = compact(raw)
            if raw.startswith('＜') or re.search(r'取引先(?:の|である)|顧客(?:の|である)', text):
                continue
            # A Japanese HQ in parentheses is not the location of the Chinese project.
            text = re.sub(r'[(（]本社[^)）]*[)）]', '', text)
            if not 12 <= len(text) <= 380 or FOREIGN.search(text) or re.search(r'中止|撤回|稼働率|否決|見送り|ではありません|(?:計画|予定)(?:は|が)(?:ない|ありません|ございません)', text):
                continue
            if not ACTION.search(text) or not ASSET.search(text):
                continue
            own = [r for r in relations if compact(r['name']) in text]
            parent = any(compact(n) in text for n in parent_names)
            issuer_voice = bool(re.search(r'当社(?:は|が|の)|当該|弊社', text))
            geography = text
            for name in [*parent_names, *(r['name'] for r in own)]:
                geography = geography.replace(compact(name), '')
            cities = list(dict.fromkeys(ALIASES[c] for c in CITY.findall(geography) if c != '中山' or '中山市' in geography))
            regions = re.findall(r'湖北省|安徽省|広東省|浙江省|江蘇省|中華人民共和国|中国本土|中国大陸', geography)
            if not cities and not regions:
                continue
            if not (own or parent or issuer_voice):
                if cities or regions:
                    unattributed.append({'quote': raw, 'locator': loc, 'identity_status':'句内主体待核'})
                continue
            relation = own[0] if len(own) == 1 and own[0]['status'] == 'verified' else None
            place = '、'.join(cities) or regions[0]
            action = next(label for label, pattern in ACTIONS.items() if re.search(pattern, text))
            stage = event_stage(text)
            planned = stage == '计划'
            when = project_date(raw, loc) if stage != '待核' else None
            if not planned and when and when['value'] > today.isoformat()[:len(when['value'])]:
                when, stage = None, '待核'
            actual = when if not planned else None
            plan = when if planned else None
            record = {'parent': company['name'], 'china_entity': relation['name'] if relation else None,
                      'relationship_status': 'verified' if relation else 'conflict' if len(own)>1 or any(r['status']=='conflict' for r in own) else 'unknown',
                      'relationship_source': relation['url'] if relation else None, 'place': place,
                      'action': action, 'stage': stage, 'quote': raw, 'locator': loc,
                      'summary': f"{company['name']}披露{place}的{action}，阶段为{stage}。",
                      'publication_date': published, 'planned_date': plan, 'actual_date': actual,
                      'time_group': recency(actual, plan, published, today)}
            choices.append(((bool(relation), bool(when), stage!='待核', bool(cities)), record))
    if not choices:
        return None, 'no_attributable_mainland_action'
    choices.sort(key=lambda pair: pair[0], reverse=True)
    record = choices[0][1]
    relation = next((r for r in relations if r['name'] == record['china_entity']), None)
    if relation:
        schedule(record, blocks, relation, today)
    record['time_group'] = recency(record['actual_date'], record['planned_date'], published, today)
    record['passages_for_review'] = unattributed
    record['other_matching_passages'] = [r for _,r in choices[1:] if r['quote']!=record['quote']]
    return record, ''
