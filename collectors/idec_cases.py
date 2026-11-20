"""Parse the first page of IDEC's public overseas support archive."""
import re
from urllib.parse import urljoin, urlsplit

from lxml import html

from collectors.kanto_cases import INDEX
from collectors.japan_channels import clean
from collectors.research_common import read_original
from collectors.research_pages import max_pages, next_page
from radar.research import _mainland_evidence

KEY = 'idec_overseas_cases'
ACTION = re.compile(r'進出|設立|発注|委託|輸出|輸入|出荷|納品|販路|販売|商談|出展|展示会|取引|調達|生産|拠点|倉庫')


def parse_detail(tree, doc):
    names = tree.xpath('//*[@class="support_info_header"]//*[@class="name"]')
    paragraphs = [clean(node) for node in tree.xpath('//*[@class="section_body"]')]
    if not names or not paragraphs:
        raise ValueError('IDEC 详情缺少公司或正文')
    name = clean(names[0]).split('【')[0].strip()
    sentences = [s.strip() for p in paragraphs for s in p.split('。')]
    action = next((s for s in sentences if ACTION.search(s) and not re.search(r'輸入規制|輸入禁止|禁輸', s)
                   and _mainland_evidence({'quote':s})), '')
    if not name or not action:
        return None
    def proof(quote):
        return {'quote':quote, 'location':'IDEC 企业栏或案例正文'}
    addresses = tree.xpath('//*[@class="entry_info support"]//*[@class="address"]')
    address = clean(addresses[0]) if addresses else ''
    text = '\n'.join([name, address, *paragraphs])
    year = re.search(r'(20\d{2})年度支援事例|支援事例集\s*(20\d{2})\s*掲載', text)
    staff = re.search(r'(?:従業員(?:数)?|社員数)\s*[：:]?\s*(\d[\d,]*)\s*[人名]', text)
    facts = [{'field':'china_action','value':action,**proof(action)}]
    dates = []
    label = '案例年度待核'
    if year:
        value = year[1] or year[2]
        dates.append({'kind':'case_year','value':value,'status':'stated','precision':'year',**proof(year[0])})
        label = value + (' 年度支援案例' if year[1] else ' 年案例集')
    if address:
        facts.append({'field':'parent_address','value':address,**proof(address)})
    size = {'value':'unknown'}
    if staff:
        count = int(staff[1].replace(',', ''))
        facts.append({'field':'employees','value':str(count),**proof(staff[0])})
        if 0 < count <= 300:
            size = {'value':'sme',**proof(staff[0])}
    return {'url':doc['url'],'external_id':doc['url'],'title':f'{name} · {label}',
        'text':text,'document':{**doc,'type':'html'},'research':{
            'event_type':'existing_presence','workstream':'existing_in_china','china_scope':'mainland',
            'china_evidence':proof(action),'dates':dates,'facts':facts,'size':size,
            'participants':[{'role':'japan_company','name':name,**proof(name)}],
            'next_step':'先核实案例之后对华交易是否延续，以及近期有无发货或补货。'}}


def collect_idec(source):
    requests, errors, checked, items, intake = [], [], [], [], []
    def fetch(url):
        result, doc = read_original(url)
        requests.append(doc)
        return html.fromstring(result['body'].decode('utf-8')), doc
    current = (getattr(source,'checkpoint',{}) or {}).get('idec_next') or INDEX
    pages = []
    discovered = set()
    for _ in range(max_pages(source)):
        if not current or current in pages:
            break
        try:
            tree, _ = fetch(current)
            urls = {}
            for href in tree.xpath('//a[contains(@href,"supportcase_info.html")]/@href'):
                url = urlsplit(urljoin(INDEX, href))
                if (url.scheme, url.netloc, url.path) == ('https', 'www.idec.or.jp', '/support/supportcase_info.html') and re.fullmatch(r'id=\d+', url.query):
                    urls[url._replace(fragment='').geturl()] = None
            if not urls:
                raise ValueError('IDEC 栏目缺少详情链接')
            pages.append(current)
        except Exception as exc:
            errors.append({'url':current, 'error':str(exc)})
            break
        detail_errors = len(errors)
        for url in list(urls)[:20]:
            if url in discovered:
                continue
            discovered.add(url)
            try:
                detail, doc = fetch(url)
                item = parse_detail(detail, doc)
                checked.append({'url':url, 'qualified':bool(item)})
                if item:
                    items.append(item)
                    intake.append(item)
                else:
                    body = '\n'.join(clean(node) for node in detail.xpath('//*[@class="section_body"]'))
                    title = detail.xpath('string(//*[@class="support_info_header"]//*[@class="name"])').strip()
                    intake.append({'url':url,'title':title,'text':body,'document':doc})
            except Exception as exc:
                errors.append({'url':url, 'error':str(exc)})
        if len(errors) != detail_errors:
            break
        current = next_page(tree, current)
    return {'items':items,'intake':intake,'status':('partial' if pages else 'blocked') if errors else 'success',
        'error':str(errors) if errors else '', 'collector_checkpoint':{'idec_next':current},
        'stats':{'discovery_method':'automatic_rolling','discovered':len(discovered),
        'checked':checked,'qualified':len(items),'requests':requests,'pages':pages,'errors':errors,
        'error_count':len(errors),'coverage_complete':not current and not errors,'scope':'IDEC 栏目游标与每页最多20篇详情'}}
