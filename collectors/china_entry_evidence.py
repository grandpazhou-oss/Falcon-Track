"""Shared public evidence handling for the four China-entry sources."""
from urllib.parse import urljoin, urlsplit
import re

from collectors.keyword_radar import intake_noise_reason, publication_from_text, publication_from_tree
from collectors.research_common import pdf_pages, read_original
from collectors.sources import node_text
from radar.search_provider import canonical_url
from radar.website_check import public_html_tree

from radar.china_places import ALIASES as CHINA_CITIES, MAINLAND as CHINA, project_city_spelling

OVERSEAS = re.compile(CHINA.pattern + r'|アジア|海外(?:展開|市場|販路|事業|向け)|輸出|越境\s*EC|現地法人|グローバル|海外輸出', re.I)
ACTION = re.compile(r'増資|扩产|扩建|增资|投产|扩建|搬迁|环评|環境影響|厂房|移転|増強|統合|新建|新設|新设|開工|开工|稼働|操業|工場|子会社|設備投資|設立|设立|签约|事業計画|輸出|供货|出荷|物流|工厂')


def region(text):
    return project_city_spelling(text) or (match[0] if (match:=CHINA.search(text)) and match[0] not in CHINA_CITIES else '')


def read(url, stats, *, limit=5_000_000, probe=False):
    response, doc = read_original(url, timeout=28, max_bytes=limit, **({"probe":True} if probe else {}))
    doc['content_type'] = response['content_type']
    stats['requests'].append(doc)
    return response, doc


def links(body, base):
    tree = public_html_tree(body)
    seen = set()
    for link in tree.xpath('//a[@href]'):
        target = canonical_url(urljoin(base, link.get('href')))
        if not target or target in seen or urlsplit(target).hostname != urlsplit(base).hostname:
            continue
        title = re.sub(r'\s+', ' ', node_text(link)).strip()
        if title:
            seen.add(target)
            yield target, title


def article(response, doc, title='', *, min_chars=75):
    source_pages=[]
    if response['body'].startswith(b'%PDF-'):
        pages = pdf_pages(doc['raw_path'], 55)
        source_pages = pages['pages']
        text = '\n'.join(p['text'] for p in source_pages)
        meta = publication_from_text(text)
    else:
        if 'html' not in response['content_type'].lower():
            raise ValueError('unexpected_content_type')
        tree = public_html_tree(response['body'])
        meta = publication_from_tree(tree)
        nodes = tree.xpath('//h1|//*[@class="article-title"]')
        html_title = tree.xpath('string(//title)').strip()
        title = (node_text(nodes[0]) if nodes else '') or title or html_title
        for node in tree.xpath('//script|//style|//noscript|//nav|//header|//footer|//aside'):
            node.drop_tree()
        main = tree.xpath('//*[@id="zoom" or @id="content" or @id="mainContent" or @id="zoomcon" or @id="zhengwen"]|//article|//main|//*[contains(concat(" ",normalize-space(@class)," ")," article_documents ")]')
        text = node_text(max(main, key=lambda el: len(node_text(el))) if main else tree)
        meta = meta or publication_from_text(text)
    if len(text.strip()) < min_chars:
        raise ValueError('empty_or_placeholder_document')
    return {'url':doc['url'],'title':title,'text':text[:180000],'document':doc,'pages':source_pages,'published':meta.get('date','')}


def failure(stats, url, exc):
    msg = str(exc)[:160]
    status = 'blocked_by_robots' if msg.endswith((':disallow', ':invalid_format')) else 'probe_failed'
    stats['errors'].append({'url':url,'status':status,'reason':msg})


def counters(stats, url, raw, retained):
    stats['endpoints'].append({'url':url,'raw':raw,'retained':retained})


def passes_noise(title, text):
    return not intake_noise_reason(title, text)
