"""Discover named companies from rolling Japanese case and press indexes."""
import re
from datetime import date
from urllib.parse import quote, urljoin, urlsplit

from lxml import html
from collectors.research_common import read_original
from collectors.candidate_state import bounded
from collectors.research_pages import max_pages, next_page
from radar.research import _mainland_evidence

TOKYO = 'tokyo_ttc_cases'
JETRO = 'jetro_china_cases'
PRESS = 'japan_china_store_news'
CHANNELS = (TOKYO, JETRO, PRESS)
from radar.china_places import MAINLAND as MAINLAND_PLACES

MAINLAND = re.compile(MAINLAND_PLACES.pattern + '|海南島')
PRESS_ACTIONS = (
    (re.compile(r'(?:倉庫|物流センター).{0,24}(?:新設|開設|移転|拡張|増設|稼働)|(?:新設|新)物流センター|新倉庫'), 'relocation', '物流据点变化'),
    (re.compile(r'(?:工場|生産拠点).{0,24}(?:増設|拡張|増強|増産)|(?:増設|拡張|増強).{0,16}(?:工場|生産)'), 'expansion', '工厂扩产'),
    (re.compile(r'新工場|(?:工場|生産拠点).{0,24}(?:新設|建設|稼働)'), 'construction_started', '新工厂项目'),
    (re.compile(r'(?:現地法人|子会社).{0,24}(?:設立|新設)|(?:設立|新設).{0,16}(?:現地法人|子会社)'), 'entity_established', '法人设立'),
    (re.compile('出店|開店|オープン|店舗開設'), 'market_research', '开店'),
)

ADDRESS = re.compile(r'本社(?:所在地)?[：:、 ]+(東京都[^、。）（() ]+)')
NEXT = '最近一次对华发货是什么时候，由日方还是在华经销商安排运输和补货？'


def clean(node):
    return ' '.join(node.text_content().split())


def discover(key, tree, base, year):
    if key == TOKYO:
        links = tree.xpath('//a[.//p[contains(@class,"bl_card_ttl")]]')
    elif key == JETRO:
        links = tree.xpath('//li[input[@value="cn"]]/a')
    else:
        links = tree.xpath('//article//a[contains(@href,"/rd/p/")]')
    if not links:
        raise ValueError('栏目缺少预期案例卡片或新闻链接')
    for link in links:
        url = urljoin(base, link.get('href', ''))
        if urlsplit(url).hostname != urlsplit(base).hostname:
            continue
        title = clean(link)
        name, vintage = '', 0
        if key == TOKYO:
            card = clean(link.xpath('.//p[contains(@class,"bl_card_ttl")]')[0])
            name = card.partition('：')[2].strip()
            m = re.search(r'令和(\d+)年度', title)
            vintage = 2018 + int(m[1]) if m else 0
        elif key == JETRO:
            m = re.search(r'/case_study/(20\d\d)/',url)
            if not m:
                continue
            name = clean(link.xpath('.//p[contains(@class,"title")]')[0])
            vintage = int(m[1])
        yield url, name, vintage


def parse(key, tree, name, vintage, today):
    roots = tree.xpath('//*[@id="press-release-body"]' if key == PRESS else '//main|//*[@id="contents"]')
    if key == PRESS and not roots:
        return
    root = roots[0] if roots else tree
    lines = [clean(n) for n in root.xpath('.//h1|.//h2|.//h3|.//p|.//tr')]
    text = '\n'.join([tree.xpath('string(//title)'), *lines])
    sentences = [s.strip() for line in lines for s in re.split('[。\n]',line) if s.strip()]
    def evidence(q):
        return {'quote':q,'location':'企业公告或案例正文'}
    stage, event_type = '', 'market_research'
    dates, facts = [], []
    if key == PRESS:
        for s in sentences:
            m = re.search(r'(株式会社[^、。（()\s]{1,35}|[^。、（）()]{1,45}株式会社)[（(][^）)]{0,65}?(?:本社|本部)',s)
            d = re.search(r'(20\d\d)年(\d+)月(?:(\d+)日)?',s)
            action = next(((kind, label) for pattern, kind, label in PRESS_ACTIONS if pattern.search(s)), None)
            if not (m and d and MAINLAND.search(s) and action) or re.search('香港|台湾|訪日',s):
                continue
            try:
                when = date(int(d[1]),int(d[2]),int(d[3] or 1))
            except ValueError:
                continue
            if abs((when-today).days)>180 or '日本初' in s:
                continue
            name, q = m[1], s
            if not _mainland_evidence({'quote':q}):
                continue
            city = next((city for city in ('海南','上海','北京','深圳') if city in q), '中国大陆')
            event_type, label = action
            planned = when > today or bool(re.search(r'予定|計画|目指|検討|することを決定', q))
            if planned and event_type == 'entity_established':
                event_type = 'entity_planned'
            if event_type == 'construction_started' and (planned or not re.search(r'着工|建設を開始', q)):
                event_type = 'expansion'
            stage = f'企业披露{city}{label}' + ('计划' if planned else '')
            dates.append({'kind':'planned_date' if planned else 'event_date','status':'planned' if planned else 'stated','precision':'day' if d[3] else 'month','value':when.isoformat() if d[3] else when.strftime('%Y-%m'),**evidence(q)})
            child = re.search('子会社「([^（「」]+)（上海）([^（「」]+)',s)
            if child:
                facts.append({'field':'site_operator','value':child[1]+'（上海）'+child[2],**evidence(q)})
            break
    elif key == TOKYO:
        for s in sentences:
            if re.search('上海.{0,10}に拠点を持',s):
                stage, q = '历史案例：已披露上海据点，先核现状',s
                break
            if re.search('中国サイトからの依頼で販売を継続|中国での販売数|中国向け.{0,10}輸出',s):
                stage, q = '案例访谈时有对华销售，先核现状',s
                break
    else:
        # Prefer a concrete trading statement over the case headline.
        priority = re.compile('販売量.{0,12}伸び|中国市場.{0,30}販売.{0,8}開始|有償サンプルを発送|中国へのライブコマース')
        for s in sorted(sentences, key=lambda value: not priority.search(value)):
            if MAINLAND.search(s) and not re.search('香港|台湾|中国地方',s) and re.search('販売量|売上|成約|販売|共同購入|ライブコマース|輸出に力を入れている国',s):
                action = '对华直播销售实体商品' if 'ライブコマース' in s else '在华销量增长' if '販売量' in s and '伸び' in s else '对华销售或渠道开发'
                stage, q = 'JETRO 案例记录'+action,s
                break
    if not stage or not name or name not in text:
        return
    if vintage:
        label = '年度' if key == TOKYO else '年案例'
        stage = f'{vintage} {label} · '+stage
    facts.append({'field':'market_stage','value':stage,**evidence(q)})
    address = ADDRESS.search(q) if key == PRESS else None
    jp = address[1] if address else next((s for s in lines if re.match(r'(所在地\s*)?東京都[^。]{2,90}$',s)), '')
    if jp:
        facts.append({'field':'parent_address','value':jp.removeprefix('所在地').strip(),**evidence(jp)})
    headcount = re.search(r'(?:社員数|従業員数)[：:\s]*約?(\d+)\s*[名人]',text)
    size = {'value':'unknown'}
    if headcount:
        facts.append({'field':'employees','value':headcount[1],**evidence(headcount[0])})
        if 0 < int(headcount[1]) <= 50:
            size = {'value':'sme',**evidence(headcount[0])}
    return {'title':name+' · '+stage,'text':text,'research':{
        'event_type':event_type,'workstream':'planned_china' if key==PRESS else 'existing_in_china',
        'china_scope':'mainland','china_evidence':evidence(q),'dates':dates,'facts':facts,'size':size,
        'participants':[{'role':'japan_company','name':name,**evidence(name)}],
        'next_step': '门店首批货和后续补货由总部、当地运营方还是经销商安排？' if key==PRESS and '开店' in stage else NEXT}}


def collect_channels(source):
    today = date.today()
    indexes = [source.url]
    if source.key == PRESS:
        indexes.extend('https://prtimes.jp/topics/keywords/'+quote(tag) for tag in ('上海','中国','中国初上陸'))
    items, checked, intake, errors, requests, pages = [], [], [], [], [], []
    state = (getattr(source,'checkpoint',{}) or {}).get('japan_pages', {})
    queue = [(index, state.get(index, index)) for index in indexes if state.get(index, index)]
    if not queue:
        queue = [(index,index) for index in indexes]
        state = {}
    visited = set()
    details_per_page = bounded((getattr(source,'options',{}) or {}).get('max_details_per_page'),20,1,20)
    def fetch(url):
        result, doc = read_original(url)
        requests.append(doc)
        return html.fromstring(result['body'].decode('utf-8')), doc
    for _ in range(max_pages(source)):
        if not queue:
            break
        index, page = queue.pop(0)
        if page in pages:
            continue
        before = len(errors)
        try:
            tree, _ = fetch(page)
            candidates = {url:(name,vintage) for url,name,vintage in discover(source.key,tree,index,today.year)}
            pages.append(page)
            for url,(name,vintage) in list(candidates.items())[:details_per_page]:
                if url in visited:
                    continue
                visited.add(url)
                try:
                    detail, doc = fetch(url)
                    item = parse(source.key,detail,name,vintage,today)
                    checked.append({'url':url,'qualified':bool(item)})
                    if item:
                        record = {**item,'url':url,'external_id':url,'document':{**doc,'type':'html'}}
                        intake.append(record)
                        if not vintage or vintage >= today.year - 1 or source.key == TOKYO:
                            items.append(record)
                    else:
                        sections = detail.xpath('//*[@id="press-release-body"]|//main|//*[@id="contents"]|//article')
                        if sections:
                            intake.append({'url':url,'title':detail.xpath('string(//title)'), 
                                           'text':clean(sections[0]),'document':doc})
                except Exception as exc:
                    errors.append({'url':url,'error':str(exc)})
            if len(errors) == before:
                state[index] = next_page(tree,page)
                if state[index]:
                    queue.append((index,state[index]))
        except Exception as exc:
            errors.append({'url':page,'error':str(exc)})
    # Rotate successful indexes while keeping any page with failed details retryable.
    for index in indexes:
        state.setdefault(index,index)
    return {'items':items,'intake':intake,'status':('partial' if pages else 'blocked') if errors else 'success',
            'error':str(errors) if errors else '', 'collector_checkpoint':{'japan_pages':state},
            'stats':{'requests':requests,'checked':checked,'errors':errors,'pages':pages,
            'error_count':len(errors),'scope':'栏目游标，每页最多20篇详情','discovered':len(visited),
            'coverage_complete':False}}
