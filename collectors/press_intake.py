"""Bounded free press and industrial-land listings routed to intake."""
import re
from urllib.parse import urlencode, urljoin, urlsplit

from lxml import html

from collectors.japan_channels import clean
from collectors.keyword_radar import publication_from_text, publication_from_tree
from collectors.research_common import read_original
from collectors.research_pages import max_pages, next_page

SITES = {
    'atpress_public': ('アットプレス · 公开企业发稿', 'https://www.atpress.ne.jp/', r'/news/\d+'),
    'kyodo_public': ('共同通信 PR ワイヤー · 公开企业发稿', 'https://kyodonewsprwire.jp/release', r'/release/\d+'),
    'logi_today_public': ('LOGISTICS TODAY · 公开物流动态', 'https://www.logi-today.com/', r'/\d{5,}'),
    'huangpu_industrial_land': ('广州黄埔 · 工业用地公告与成交', 'https://www.hp.gov.cn/tzcy/tdjy/', r'/tzcy/tdjy/content/post_\d+\.html'),
}
SEARCH_INDEX = {
    'atpress_public':'https://www.atpress.ne.jp/news/search?' + urlencode({'q':'中国'}),
}
from radar.china_places import MAINLAND as GEOGRAPHY
BUSINESS = re.compile(r'製造|工場|倉庫|出荷|物流|増設|進出|現地法人|子会社|輸出|移転|生産|製造業|\b3PL\b')
LAND = re.compile(r'工业用地|工业地块|制造业|交易结果|竞得人')


def collect_press(source):
    _, base, path = SITES[source.key]
    host = urlsplit(base).hostname
    government = source.key == 'huangpu_industrial_land'
    requests, errors, intake, checked, seen = [], [], [], [], set()
    index = SEARCH_INDEX.get(source.key,base)
    checkpoint = source.checkpoint or {}
    page = (checkpoint.get('press_next') if checkpoint.get('press_index') == index else '') or index
    def read(url):
        result, doc = read_original(url, timeout=30)
        requests.append(doc)
        return html.fromstring(result['body']), doc
    pages = []
    for _ in range(max_pages(source)):
        if not page or page in pages:
            break
        try:
            tree, _ = read(page)
            links = {}
            for node in tree.xpath('//a[@href]'):
                title = clean(node)
                target = urlsplit(urljoin(page, node.get('href')))
                if title and target.hostname == host and re.fullmatch(path, target.path):
                    links.setdefault(target._replace(scheme='https',query='',fragment='').geturl(), title)
            if not links:
                raise ValueError('listing_layout_missing')
            pages.append(page)
            rows = list(links.items())
            rows.sort(key=lambda row: not (GEOGRAPHY.search(row[1]) and BUSINESS.search(row[1])))
        except Exception as exc:
            errors.append({'url':page,'error':str(exc)})
            break
        before = len(errors)
        for url,title in rows[:12]:
            if url in seen:
                continue
            seen.add(url)
            try:
                detail, doc = read(url)
                for entry in detail.xpath('//script|//style|//noscript|//nav|//footer|//header'):
                    entry.drop_tree()
                sections = detail.xpath('//article|//main|//*[@id="zoom" or @id="content"]')
                text = clean(sections[0] if sections else detail.xpath('//body')[0])
                if len(text) < 80:
                    raise ValueError('detail_text_missing')
                match = bool(LAND.search(title+' '+text) if government else
                             GEOGRAPHY.search(title+' '+text[:10000]) and BUSINESS.search(title+' '+text[:10000]))
                checked.append({'url':url,'matched':match})
                if match:
                    publication = publication_from_tree(detail) or publication_from_text(text)
                    if government:
                        from collectors.japanese_public import expansion_hits
                        from radar.seed_dictionary import matches
                        owner = re.search(r'竞得人\s*(?:[:：]|为)\s*([^，。；]{2,80})',text)
                        hits = expansion_hits(title,text) or matches(title + ' ' + (owner[1] if owner else ''))
                    else:
                        hits = []
                    intake.append({'url':url,'title':title,'text':text,'document':doc,
                        'intake_metadata':{'source_publication':publication,'seed_hits':hits}})
            except Exception as exc:
                errors.append({'url':url,'error':str(exc)})
        if len(errors) != before:
            break
        page = next_page(tree,page)
    return {'items':[], 'intake':intake, 'status':('partial' if pages else 'blocked') if errors else 'success',
            'error':str(errors)[:1800] if errors else '', 'collector_checkpoint':{'press_next':page,'press_index':index},
            'stats':{'requests':requests,'pages':pages,'details':checked,'discovered':len(seen),
                     'error_count':len(errors),'errors':errors,'coverage_complete':False}}
