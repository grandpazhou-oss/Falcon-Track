"""Named market-development signals from two organizer rosters."""
import hashlib
import re
from urllib.parse import urlsplit

from lxml import html

from collectors.research_common import read_original
from collectors.research_pages import max_pages


def record(doc, name, market, label, fields, context, role='listed_exhibitor'):
    lines = [market, name, context, *(value for _, value in fields)]
    def evidence(value):
        return {'quote': value, 'location': '主办方名单 / ' + name}

    identity = hashlib.sha256((label + '|' + name).encode()).hexdigest()
    return {'external_id': identity, 'title': name + ' · ' + label,
            'url': doc['url'], 'document': {**doc, 'type': 'html'},
            'text': '\n'.join(lines), 'research': {
                'event_type': 'exhibitor_confirmed', 'workstream': 'shanghai_market',
                'china_scope': 'mainland', 'china_evidence': evidence(market),
                'participants': [{'name': name, 'role': role, **evidence(name)}],
                'facts': [{'field': key, 'value': value, **evidence(value)} for key, value in fields],
                'next_step': '联系企业海外营业部门，确认中国进口商、样品是否转为订单、下一批货期及运费决定方。',
                'questions': ['是否已通过在华代理供货？首批或补货是否需要上海仓配？规模和物流委托情况待核。']}}


def fhc_rows(tree, doc):
    market = tree.xpath('string(//title)').strip()
    if 'FHC上海' not in market:
        raise ValueError('FHC 展商页面身份未确认')
    rows = tree.xpath('//div[@class="fhc-exhibitor-list"]/div[div[@class="_name"]]')
    if not rows:
        raise ValueError('展商表为空或结构变更')
    items = []
    for row in rows:
        name = row.xpath('string(./div[1])').strip()
        region = row.xpath('string(./div[3]/div[last()])').strip()
        if region != '日本' or not name or re.search('代表处|协议会|协会|商会|^日本.+县$', name):
            continue
        fields = [('market', region)]
        booth = row.xpath('string(./div[2]/div[last()])').strip()
        if booth:
            fields.append(('booth', booth))
        products = [v.strip() for v in row.xpath('./div[4]/div/text()') if v.strip()]
        fields.extend(('product', v) for v in products)
        items.append(record(doc, name, market, 'FHC 上海展商名录', fields, region))
    return items


def osaka_rows(tree, doc):
    paragraphs = [p.text_content().strip() for p in tree.xpath('//p')]
    marker = next((i for i,p in enumerate(paragraphs) if '大阪市ブース出展企業' in p), None)
    venue = next((p for p in paragraphs if '場所' in p and '上海' in p), '')
    date = next((p for p in paragraphs if '日時' in p and re.search(r'20\d\d年', p)), '')
    if marker is None or not venue or not date:
        raise ValueError('大阪展位名单、会期或会场缺失')
    year = re.search(r'20\d\d年', date).group()
    items = []
    for line in paragraphs[marker+1:]:
        match = re.fullmatch(r'・(.+?)（([^）]+)）', line)
        if not match:
            break
        name, product = match.groups()
        items.append(record(doc, name, venue, year + '上海进博会大阪展位',
                            [('product', product), ('fair_dates', date)], line, 'japan_company'))
    if not items:
        raise ValueError('大阪名单格式改变')
    return items


def fbc_rows(tree, doc):
    content = tree.xpath('//*[@class="active-detail-content"]')
    edition = re.search(r'FBC\s*上海\s*(20\d{2})\s+in\s+PTC', content[0].text_content(), re.I) if content else None
    if not edition:
        raise ValueError('FBC 展商介绍页改版')
    records = []
    for header in content[0].xpath('.//p[strong]'):
        name = header.text_content().strip()
        if not name.endswith('有限公司') or len(name) > 70 or '香港' in name:
            continue
        for block in header.iterancestors('section'):
            text = block.text_content().strip()
            if not text.startswith(name):
                break
            if 'SELL' not in text:
                continue
            relation = re.search(r'[^。]*日本[^。]*(?:独资设立|总公司在日本)[^。]*。|[^。]*总公司在日本。', text)
            if relation:
                product = text.split('SELL', 1)[1].split('BUY', 1)[0].strip()
                item = record(doc, name, edition[0], f'{edition[1]} 上海 FBC 制造业商谈会',
                              [('product', product), ('parent_china_relation', relation.group())],
                              name, 'china_entity')
                item['research']['workstream'] = 'existing_in_china'
                item['research']['next_step'] = '联系在华营业或物流部门，核实产品发运地区、设备或零部件交付频次及运输委托方。'
                records.append(item)
            break
    if not records:
        raise ValueError('未找到有明确日本总部或股权原文的在华制造企业')
    return records


def collect_exhibitors(source):
    logs, items = [], []
    next_fhc = None
    def read(url):
        result, doc = read_original(url)
        logs.append(doc)
        return html.fromstring(result['body'].decode('utf-8')), doc

    try:
        tree, doc = read(source.url)
        if source.key == 'fhc_shanghai_japan':
            total = re.search(r'var lastPage\s*=\s*(\d+)', tree.xpath('string(//body)'))
            if not total:
                raise ValueError('无法判定展商列表页数')
            pages = int(total[1])
            start = (source.checkpoint or {}).get('fhc_page', 1)
            if start > pages:
                start = 1
            next_fhc = start
            for page in range(start, min(pages + 1, start + max_pages(source))):
                items.extend(fhc_rows(tree, doc) if page == 1 else
                             fhc_rows(*read(source.url + '/page/' + str(page))))
                next_fhc = page + 1
            complete = next_fhc > pages
            if complete:
                next_fhc = 1
        elif source.key == 'fbc_ptc_manufacturers':
            items = fbc_rows(tree, doc)
            complete = True
        else:
            entries = []
            for li in tree.xpath('//ul[starts-with(@id,"new_list_")]/li'):
                title = li.xpath('string(.//a)').strip()
                href = li.xpath('string(.//a/@href)')
                if ('輸入博覧会' in title and '大阪市ブース' in title and '募集' not in title
                        and urlsplit(href).hostname == 'osaka-sh.com.cn'):
                    entries.append((li.xpath('string(span)'), href))
            if not entries:
                raise ValueError('没有找到已公布的大阪展位名单')
            items = osaka_rows(*read(max(entries)[1]))
            complete = True
        return {'items': items, 'status': 'success',
                'collector_checkpoint':{'fhc_page':next_fhc} if next_fhc else {}, 'stats': {
            'requests': logs, 'coverage_complete': complete, 'scope': 'FHC日本地区游标、大阪最新展位或FBC本期展商'}}
    except Exception as exc:
        return {'items': items, 'status': 'partial' if items else 'blocked', 'error': str(exc)[:400],
                'collector_checkpoint':{'fhc_page':next_fhc} if next_fhc else {},
                'stats': {'requests': logs, 'error_count': 1, 'coverage_complete': False}}
