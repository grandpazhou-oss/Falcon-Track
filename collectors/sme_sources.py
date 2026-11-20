"""Public SME declarations and signed @Press releases."""
import hashlib
import re
import unicodedata
from datetime import timedelta
from urllib.parse import urljoin, urlsplit

from django.core.management.base import CommandError
from django.utils import timezone

from lxml import etree, html

from collectors.china_entry_evidence import CHINA, article, counters, failure, passes_noise, read
from collectors.issuer_ir_pdf import retain_original
from collectors.research_common import pdf_pages
from collectors.sources import node_text
from radar.china_places import project_city, project_text
from radar.company_enrichment import classify, identity, same_name, site_root
from radar.models import KeywordCandidate, SourceRun
from radar.search_provider import canonical_url

SMRJ = 'https://growth-100-oku.smrj.go.jp/companies/index.html'
ATPRESS = 'https://www.atpress.ne.jp/rss/index.rdf'
SITES = {
    'smrj_100oku': ('中小機構 · 100億宣言', (SMRJ,)),
    'atpress_rss': ('@Press · 官方 RSS', (ATPRESS,)),
}
MOTION = re.compile(r'出店|開店|開業|開設|開拓|展開|新設|拡大|拡張|増強|拡充|増産|増設|進出|供給|出荷|輸出|販路|生産移管|販売開始|物流拠点|倉庫|工場|事業開始|稼働')
OVERSEAS = re.compile(r'海外(?:拠点|法人|工場|現地|展開|市場|販路|売上)|現地法人|越境EC|国際物流')
PROMOTION = re.compile(r'セミナー|ウェビナー|コンサルティング|進出支援サービス|PR支援|リサーチレポート|キャンペーン')


def class_nodes(name, relative=''):
    return f'{relative}//div[@class="{name}"]'


def declaration_index(body):
    tree = html.fromstring(body.decode('utf-8'))
    entries = {}
    for node in tree.xpath(class_nodes('c-item1__content')):
        paths = node.xpath('.//a[starts-with(@href,"/companies/pdf/")]/@href')
        names = node.xpath(class_nodes('c-item1__title','.'))
        if not paths or not names:
            continue
        path = paths[0]
        if not re.fullmatch(r'/companies/pdf/[\w-]+\.pdf',path):
            continue
        hq = node.xpath('.//div[@class="cat"]/text()')
        dates = node.xpath(class_nodes('c-item1__date','.'))
        date = re.findall(r'20\d{2}年\d{2}月\d{2}日', dates[0].text_content()) if dates else []
        linked = node.getparent().xpath('./div[@class="box2"]//div[@class="cat"]/text()')
        priority = 2 if any(CHINA.search(x) for x in linked) else int(any(not x.endswith(('都','道','府','県')) for x in linked))
        url = urljoin(SMRJ,path)
        entries[url] = {'url':url,'company':node_text(names[0]),'hq':hq[0].strip() if hq else '',
                        'date':date[-1].translate(str.maketrans({'年':'-','月':'-','日':''})) if date else '',
                        'priority':priority}
    if not entries:
        raise ValueError('declaration_index_layout_missing')
    return sorted(entries.values(),key=lambda row:(row['priority'],row['date'],row['url']),reverse=True)


def rss_index(body):
    root = etree.fromstring(body, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
    if etree.QName(root).localname not in {'RDF','rss'} or root.getroottree().docinfo.doctype:
        raise ValueError('unexpected_rss_document')
    rows = {}
    for item in root.xpath('//*[local-name()="item"]'):
        fields = {etree.QName(e).localname: (e.text or '').strip() for e in item if isinstance(e.tag,str)}
        url = canonical_url(fields.get('link',''))
        if urlsplit(url).hostname != 'www.atpress.ne.jp' or not re.fullmatch(r'/news/\d+',urlsplit(url).path):
            continue
        summary = html.fragment_fromstring(fields.get('description') or '<p/>',create_parent=True).text_content()
        rows[url] = {'url':url,'title':fields.get('title',''),'company':fields.get('corp',''),
                     'date':fields.get('date','')[:10],'summary':summary,'priority':bool(CHINA.search(summary+' '+fields.get('title','')))}
    if not rows:
        raise ValueError('rss_items_missing')
    return list(rows.values())


def project_quote(text, *, declarations=False):
    text = project_text(text)
    if declarations:
        text = re.sub(r'〇現在の売上高[^\n]*', '', text)
    lines = [part.strip() for part in re.split(r'[。\n；]| {4,}|(?=〇(?:本社所在地|法人番号))',text)]
    lines = [line for line in lines if 12 <= len(line) <= 380 and MOTION.search(line) and not PROMOTION.search(line)]
    mainland = [line for line in lines if CHINA.search(line)]
    if mainland:
        return max(mainland, key=lambda line: (bool(project_city(line)), bool(re.search(r'増強|拡充|開設|供給|出荷|輸出',line))))
    return next((line for line in lines if OVERSEAS.search(line)), '') if declarations else ''


def company_scale(text):
    normalized = unicodedata.normalize('NFKC',text)
    return {label: re.sub(r'\s+', '', match[1]) for label, rx in (
        ('employees',r'(?:従業員数|従業員)\s*[:：]?\s*([\d,]+\s*名|[\d,]+\s*人)'),
        ('revenue',r'(?:現在の売上高|直近売上高)\s*[:：]\s*([\d,.]+\s*(?:億円|百万円|万円|円))'),
    ) if (match:=re.search(rx,normalized))}


def declaration_company_matches(company, pages):
    # The publisher's heading includes the full legal name, even for two-character brands.
    legal_name = ''.join(unicodedata.normalize('NFKC', company).split())
    if legal_name and re.search(r'株式会社|有限会社|合同会社|合資会社|合名会社', legal_name):
        header = re.compile(re.escape(legal_name) + r'(?:\([^()]+業\))?')
        if any(header.fullmatch(''.join(unicodedata.normalize('NFKC', line).split()))
               for page in pages for line in page['text'].splitlines()):
            return True
    return same_name(company, '\n'.join(page['text'] for page in pages)[:2400])


def disclosure(entry, response, doc, *, declarations):
    title = entry.get('title') or entry['company']+'｜100億宣言'
    item = article(response, doc, title)
    text = item['text']
    if declarations:
        if not response['body'].startswith(b'%PDF-'):
            raise ValueError('declaration_company_not_confirmed')
        if not declaration_company_matches(entry['company'], item['pages']):
            # Some two-column headings join unrelated columns in layout order.
            original_order = pdf_pages(doc['raw_path'], mode='plain')['pages']
            if not declaration_company_matches(entry['company'], original_order):
                raise ValueError('declaration_company_not_confirmed')
    elif not entry['company'] or not same_name(entry['company'],text[:15000]):
        raise ValueError('rss_publisher_not_confirmed')
    if not passes_noise(title,text) or PROMOTION.search(title):
        raise ValueError('noise_or_promotion')
    quote=project_quote(text, declarations=declarations)
    if not quote:
        raise ValueError('no_relevant_action')
    result={**item,'company':entry['company'],'title':title,'evidence_quote':quote,'region':project_city(quote),
            'headquarters':entry.get('hq',''),'published':entry['date'] or item['published'],
            'company_industry':classify(text),'source_scope':'sme_declaration' if declarations else 'signed_press_release'}
    if declarations:
        result['company_scale']=company_scale(text)
        links=re.findall(r'(?:https?://)[^\s<>「」]+',text[:3000])
        suffixes=[site_root(link.rstrip('.,)）')) for link in links]
        websites=list(dict.fromkeys(u for u in suffixes if u and urlsplit(u).hostname!=urlsplit(SMRJ).hostname))
        if len(websites)==1:
            result['official_website']=websites[0]
            result['official_website_status']='unverified'
        for page in item['pages']:
            if quote in project_text(page['text']):
                result['pdf_page']=page['page']
                break
    return result


def unread_overseas(entries, seen):
    return [entry for entry in entries if entry['priority'] and entry['url'] not in seen]


def cached_smrj_index(source, url, stats):
    from radar.management.commands.backup_data import raw_source_file

    recent=SourceRun.objects.filter(source=source,finished_at__gte=timezone.now()-timedelta(days=1)).order_by('-pk')
    for run in recent:
        for item in run.stats.get('requests',[]):
            if item.get('url')!=url:
                continue
            try:
                data=raw_source_file(item['raw_path']).read_bytes()
            except (OSError, CommandError):
                continue
            if hashlib.sha256(data).hexdigest()==item['sha256']:
                stats['cached_index']={'url':url,'sha256':item['sha256'],'source_run':run.pk}
                return data
    return None


def collect(source, stats, max_details, targets, *, mainland_batch=False, listed_names=()):
    if not targets:
        return [], source.checkpoint
    target=targets[0]
    declarations=source.key=='smrj_100oku'
    try:
        body=cached_smrj_index(source,target,stats) if mainland_batch else None
        if body is None:
            try:
                response,_=read(target,stats,limit=8_000_000,probe=True)
                body=response['body']
            except (ValueError, OSError) as exc:
                if not declarations or str(exc) not in {'time_limit', 'timeout', 'rate_wait_timeout'}:
                    raise
                body = cached_smrj_index(source, target, stats)
                if body is None:
                    raise
                failure(stats, target, exc)
        entries=declaration_index(body) if declarations else rss_index(body)
    except (ValueError,OSError,etree.LxmlError) as exc:
        failure(stats,target,exc)
        return [], source.checkpoint
    old=(source.checkpoint or {}).get('source_seen',{})
    seen={}
    rows=[]
    pending=[e for e in entries if e['url'] not in old or old[e['url']]!=e['date']]
    pending_urls={e['url'] for e in pending}
    candidates=[e for e in pending if declarations or e['priority']]
    if mainland_batch:
        from radar.research import _mainland_evidence

        read_urls={doc['url'] for run in SourceRun.objects.filter(source=source)
                   .values_list('stats',flat=True) for doc in run.get('requests',[])
                   if doc.get('url','').startswith(urljoin(SMRJ,'pdf/'))}
        stats['prior_read_count']=len(read_urls)
        for batch in SourceRun.objects.filter(source=source,finished_at__gte=timezone.now()-timedelta(days=1)).values_list('stats',flat=True):
            read_urls.update(batch.get('batch_attempted_urls',[]))
        candidates=unread_overseas(entries,read_urls)
        known=set(listed_names)
        stats['excluded_known_listed']=[e['company'] for e in candidates if identity(e['company']) in known]
        candidates=[e for e in candidates if identity(e['company']) not in known]
        stats['eligible_unread_mainland']=sum(e['priority']==2 for e in candidates)
        stats['eligible_unread_overseas']=sum(e['priority']==1 for e in candidates)
        stats['batch_attempted_urls']=[e['url'] for e in candidates[:max_details]]
        stats['read_batch']=[]

    # Read three feed articles on first run to verify the real detail parser even on a quiet day.
    if not declarations and not old:
        candidates.extend(e for e in pending if not e['priority'])
        candidates=candidates[:max(3, sum(bool(e['priority']) for e in pending))]
    touched={e['url'] for e in candidates[:max_details]}
    for entry in entries:
        if entry['url'] not in touched and (entry['url'] not in pending_urls or not declarations and not entry['priority']):
            seen[entry['url']]=entry['date']
    for entry in candidates[:max_details]:
        url=entry['url']
        try:
            response,doc=read(url,stats,limit=8_000_000,probe=True)
            if urlsplit(response['url']).hostname!=urlsplit(target).hostname:
                raise ValueError('detail_host_mismatch')
        except (ValueError,OSError) as exc:
            failure(stats,url,exc)
            continue
        if mainland_batch:
            stats['read_batch'].append({'company':entry['company'],'url':url,'sha256':doc['sha256'],'priority':entry['priority']})
        try:
            row=disclosure(entry,response,doc,declarations=declarations)
            if mainland_batch and not _mainland_evidence({'quote':row['evidence_quote']}):
                raise ValueError('no_mainland_action')

            if not KeywordCandidate.objects.filter(canonical_url=url).exists():
                retain_original(doc)
            rows.append(row)
            seen[url]=entry['date']
        except (ValueError,etree.LxmlError) as exc:
            failure(stats,url,exc)
            if str(exc) in {'noise_or_promotion','no_relevant_action','no_mainland_action'}:
                seen[url]=entry['date']
    stats['unseen']=len(pending)
    stats['details_attempted']=len(touched)
    counters(stats,target,len(entries),len(rows))
    return rows, {'source_seen':seen}
