"""Only named award rows with an overseas project or an official global category."""
import re
import unicodedata
from datetime import date, timedelta
from urllib.parse import quote, unquote, urljoin, urlsplit

from collectors.china_entry_evidence import CHINA, region, counters, failure, links, read, pdf_pages

from collectors.issuer_ir_pdf import retain_original

SMRJ = 'https://shinjigyou-shinshutsu.smrj.go.jp/results'
MONO = 'https://portal.monodukuri-hojo.jp/saitaku.html'
TARGETS = (SMRJ, MONO)


def award_links(body, base):
    if urlsplit(base).hostname == urlsplit(SMRJ).hostname:
        pattern = r'/docs/shinjigyou_saitakukekka_\d+\.pdf'
        return [url for url,_ in links(body,base) if re.fullmatch(pattern,unquote(urlsplit(url).path))]
    paths = [u for u,_ in links(body,base)
             if re.search(r'/ippan/\d+th/(?:saitaku\w+(?:_g)?|採択者リスト_[^/]+)\.pdf$',unquote(urlsplit(u).path))]
    return list(dict.fromkeys(paths))


DOMESTIC = re.compile(r'在日中国人|国内製造|日本国内|中国茶ドリンク専門店|留学生.*受け入れ|留学.*受け入れ|中国食文化.*ギフト')
DATED = re.compile(r'(?:令和(\d{1,2})|((?:19|20)\d{2}))年\s*(\d{1,2})月\s*(\d{1,2})日')


def active_awards(body, base, today):
    from radar.website_check import public_html_tree
    from radar.search_provider import canonical_url

    tree=public_html_tree(body)
    allowed={quote(url, safe=":/%?=&") for url in award_links(body,base)}
    found={}
    for link in tree.xpath('//a[@href]'):
        url=quote(canonical_url(urljoin(base,link.get('href'))), safe=':/%?=&')
        if url not in allowed:
            continue
        if base==MONO:
            rows=link.xpath('ancestor::tr[1]')
            evidence=' '.join(rows[0].itertext()) if rows else ''
            if not DATED.search(unicodedata.normalize('NFKC',evidence)) and rows:
                prior=rows[0].xpath('preceding-sibling::tr[1]')
                if prior and prior[0].xpath('./td[@rowspan="2"]'):
                    evidence=' '.join(prior[0].itertext())
        else:
            blocks=list(link.iterancestors())[:5]
            evidence=next((' '.join(x.itertext()) for x in blocks
                           if DATED.search(unicodedata.normalize('NFKC',' '.join(x.itertext())))), '')
        match=DATED.search(unicodedata.normalize('NFKC',evidence))
        if not match:
            continue
        try:
            selected=date(2018+int(match[1]) if match[1] else int(match[2]),int(match[3]),int(match[4]))
        except ValueError:
            continue
        if today-timedelta(days=365)<=selected<=today:
            found[url]=selected
    return found


def award_rows(pages, layout):
    """Layout-mode PDF lines. Column boundaries come from printed table headings."""
    rows = []
    plan_column = None
    for page in pages:
        for line in page['text'].splitlines():
            if '法人番号' in line and '事業計画名' in line:
                start = line.find('事業者名') if layout == 'smrj' else line.find('商号又は名称')
                plan_column = line.find('事業計画名')
                end = line.find('認定支援機関名')
                if start < 0 or end <= plan_column:
                    plan_column = None
                continue
            if plan_column is None:
                continue
            number = re.search(r'(?<!\d)\d{13}(?!\d)', unicodedata.normalize('NFKC',line))
            if not number:
                continue
            left = line[:number.start()].strip()
            chunks = re.split(r'\s{2,}', left)
            name = chunks[-1].strip()
            if (len(chunks) < 3 or not name or not re.search(r'株式会社|有限会社|合同会社|合資会社|合名会社|事業組合|法人', name)):
                continue
            rest=line[number.end():].strip()
            columns = re.split(r'\s{2,}', rest)
            industry = columns[0] if layout == 'smrj' else ''
            if layout == 'smrj':
                columns = [value for value in columns[1:] if value not in {'○','〇','●'}]
            project = columns[0] if columns else ''
            if project:
                rows.append({'company':name,'project':project,'id':number[0],
                             'page':page['page'],'quote':line.strip(),'industry':industry})
    return rows


def collect(stats, max_details=8, *, targets=TARGETS, seen=None):
    from django.utils import timezone
    from radar.company_enrichment import classify
    out=[]
    seen = seen if seen is not None else {}
    for index in targets:
        try:
            response,_=read(index,stats,probe=True)
            documents=active_awards(response['body'],response['url'],timezone.localdate())
            if not documents: raise ValueError('award_list_layout_missing')
            has_filter=0
            pending=sorted(((u,published) for u,published in documents.items()
                            if seen.get(u)!=published.isoformat()),key=lambda item:(item[1],item[0]),reverse=True)
            for u,published in pending[:max_details]:
                try:
                    response,doc=read(u,stats,limit=15_000_000,probe=True)
                    parsed=pdf_pages(doc['raw_path'],55)
                    layout='smrj' if index==SMRJ else 'mono'
                    rows=award_rows(parsed['pages'],layout)
                    if not rows and parsed.get('backend') == 'pymupdf':
                        parsed=pdf_pages(doc['raw_path'],55,mode='table_layout')
                        rows=award_rows(parsed['pages'],layout)
                    if parsed.get('unprocessed_pages'):
                        failure(stats,u,ValueError('pdf_page_budget'))
                    if not rows: raise ValueError('award_rows_layout_missing')
                    kept=[r for r in rows if CHINA.search(r['project']) and not DOMESTIC.search(r['project'])]
                    counters(stats,u,len(rows),len(kept))
                    if not parsed.get('unprocessed_pages'):
                        seen[u]=published.isoformat()
                    if kept:
                        retain_original(doc)
                    has_filter+=len(kept)
                    for r in kept:
                        address=doc['url']+f"#page={r['page']}&radar-row={r['id']}"
                        out.append({'url':address,'title':r['company']+'｜'+r['project'],'text':r['quote'],
                            'company':r['company'],'document':doc,'region':region(r['project']),
                            'source_scope':'grant_selection','evidence_quote':r['quote'],'published':published.isoformat(),
                            'award_publish_date':published.isoformat(),'pdf_page':r['page'],
                            'company_industry':classify(r['project']) or classify(r['industry']),
                            'unique_document_row':True,'historical_case':False})
                except (ValueError,OSError) as exc: failure(stats,u,exc)
            counters(stats,index,len(documents),has_filter)
        except (ValueError,OSError) as exc: failure(stats,index,exc)
    return out
