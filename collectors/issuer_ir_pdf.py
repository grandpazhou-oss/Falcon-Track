"""Bounded issuer disclosure discovery. Search results supply URLs only."""
import re
import unicodedata
from urllib.parse import urljoin, urlsplit

from datetime import timedelta

from django.utils import timezone
from collectors.keyword_radar import publication_date
from collectors.china_entry_evidence import CHINA, article, counters, failure, passes_noise, read, region
from collectors.sources import node_text
from collectors.research_common import pdf_pages
from radar.company_enrichment import classify, same_name, site_root
from radar.website_check import public_html_tree

QUERIES = (
    'filetype:pdf (中国工場 OR 上海工場 OR 蘇州工場 OR 華東) (新設 OR 移転 OR 増強 OR 統合 OR 稼働) (2026 OR 2027 OR 2028)',
    'filetype:pdf (広東 OR 広州 OR 東莞 OR 深圳 OR 珠三角) (工場 OR 子会社 OR 拠点) (新設 OR 移転 OR 増強 OR 設立) (2026 OR 2027 OR 2028)',
    '(子会社の設立 OR 工場新設 OR 生産拠点移転) (上海 OR 蘇州 OR 張家港 OR 広州 OR 東莞 OR 深圳) (2026 OR 2027) -site:prtimes.jp -site:finance.biggo.jp',
)
REPORT_EXCLUSIONS = '-統合報告書 -アニュアルレポート -有価証券報告書 -サステナビリティ'
EXPANSION_QUERIES = (
    ('S1','広州 東莞 深圳 新工場 生産 2026 2027'),
    ('S2','広東省 工場 移転 増産 生産移管 2026 2027'),
    ('E3','上海 蘇州 新工場 生産拠点 稼働 2026 2027'),
    ('C4','中国子会社 設立 工場 物流 2026 2027 プレスリリース'),
)
TDNET_PATH = re.compile(r'/markets/ir/irftp/data/tdnr/[A-Za-z0-9_/-]+\.pdf\Z',re.I)
ISSUER_HTML = re.compile(r'/(?:news(?:room|release|_release|-release)?|press|release|ir(?:news)?|investors?|topics|corporate/news|all-news)(?:/|[._-])',re.I)


def nikkei_tdnet(url):
    address=urlsplit(url)
    return address.scheme=='https' and address.netloc.lower()=='www.nikkei.com' and bool(TDNET_PATH.fullmatch(address.path))


def discovery_kind(url):
    return 'pdf' if urlsplit(url).path.lower().endswith(('.pdf','.ashx')) else 'html'


REPORT_TITLE = re.compile(r'統合報告書|アニュアルレポート|有価証券報告書|サステナビリティ(?:報告|レポート)|integrated.report|annual.report|sustainability.report', re.I)
ACTION_TYPES = (
    ('移管或产能整合', r'移管|集約|再編|統合|閉鎖'),
    ('工厂或据点搬迁', r'移転|搬迁'),
    ('扩产', r'増産|増強|増設|拡張|扩产'),
    ('开始运营', r'稼働|操業|投产|出荷開始'),
    ('新建或设立', r'新設|新设|開設|設立|落户|竣工|着工|整備'),
)
MEDIA_QUERIES = (
    '("日资" OR "日企") ("增资扩产" OR "新工厂" OR "投产" OR "落户") ("广州" OR "东莞" OR "深圳" OR "佛山" OR "珠海") (2026 OR 2027)',
    'site:cn.nikkei.com (中国工厂 OR 广东 OR 广州 OR 东莞 OR 深圳 OR 苏州 OR 上海) (新厂 OR 增产 OR 扩建 OR 设立) (2026 OR 2027)',
)
ACTION = re.compile(r'移転|新設|新设|開設|再編|移管|増強|増設|統合|稼働|操業|設立|出荷開始|増産|拡張|整備|供給開始|閉鎖|集約|投产|扩产|搬迁|落户|竣工|着工')
PUBLISHER = re.compile(r'(?:会社名|上場会社名)[：:\s]+([^\n]{3,75})')
NAME = re.compile(r'(?:株式会社|有限会社)[\w・＆&-]{2,36}|[\w・＆&-]{2,36}株式会社')
CODE = re.compile(r'(?:証券コード|コード番号|銘柄コード|コード)\s*[：:]?\s*([0-9][0-9A-Z]{3})(?![0-9A-Z])')
EXCLUDED = ('biggo.jp','jpx.co.jp','im.dg.gov.cn','kabuyoho-sls-disclose.ifis.co.jp','storage.googleapis.com')


def blocked(url):
    host=urlsplit(url).hostname or ''
    if host=='nikkei.com' or host.endswith('.nikkei.com'):
        return not nikkei_tdnet(url)
    return any(host==x or host.endswith('.'+x) for x in EXCLUDED)


def parse_disclosure(title,text):
    normalized=unicodedata.normalize('NFKC',text).replace('(株)','株式会社')
    explicit=PUBLISHER.search(normalized[:5000])
    issuer_text=re.split(r'上場取引所|代表者|コード番号|\s{2,}',explicit[1])[0] if explicit else normalized[:2000]
    issuer=NAME.search(issuer_text.strip())
    code=CODE.search(normalized[:6000])
    evidence_lines=[line.strip() for line in re.split(r'[。\n]',text) if CHINA.search(line)
                   and ACTION.search(line) and re.search(r'工場|工厂|生産|生产|子会社|法人|設備|出荷|拠点|倉庫|仓库|物流|供給',line)
                   and not re.search(r'中国地方|撤回|中止',line)
                   and (len(line.strip())>=17 or re.search(r'稼働|操業|設立|供給',line))]
    evidence=max(evidence_lines,key=lambda line:(region(line) not in ('','中国'),
                 bool(re.search(r'移管|稼働|操業|開設|出荷開始',line))),default='')
    if not issuer or not evidence or not passes_noise(title,text):
        return None
    period=re.search(r'(20\d{2})年\s*(\d{1,2})?月?',unicodedata.normalize('NFKC',evidence)) if '取締役会' not in evidence else None
    target=(period[1]+(f'-{int(period[2]):02d}' if period[2] and 1<=int(period[2])<=12 else '')) if period else ''
    return {'company':issuer[0],'security_code':code[1] if code else '',
            'evidence_quote':evidence,'target_period':target,'region':region(evidence),
            'company_industry':classify(evidence) or classify(next((x for x in text.splitlines() if '主要事業分野' in x),'')),'source_scope':'issuer_disclosure',
            'action_type':next((label for label, terms in ACTION_TYPES if re.search(terms,evidence)), '')}


def parse_pdf_disclosure(title, pages):
    """Keep geographic table sections and independent comma-separated actions apart."""
    if not pages:
        return None
    cover=unicodedata.normalize('NFKC', pages[0]['text'])
    labels=re.compile(r'(?:会社名|上場会社名|企業名)[\]】]?[：:\s]+([^\n]{3,75})')
    identity_match=labels.search(cover)
    author=identity_match[1] if identity_match else cover
    # PDF layout can insert spaces between the corporate suffix and legal name.
    author=re.sub(r'株式会社[ \t]+(?=[^\s(（])|(?<=[\w・])[ \t]+株式会社',lambda m:m[0].strip(),author)
    issuer=NAME.search(author)
    code=CODE.search(cover) or re.search(
        r'(?:東証[^\n]{0,30}?市場|企業 *ID\])[：:\s]*(\d[0-9A-Z]{3})(?![0-9A-Z])',cover)
    if not issuer:
        return None
    pub=re.search(r'(?m)^\s*(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\b',cover)
    published=publication_date('-'.join((pub[1],pub[2].zfill(2),pub[3].zfill(2)))) if pub else None
    if not published or not timezone.localdate()-timedelta(days=365)<=published<=timezone.localdate():
        return None

    choices=[]
    for page in pages:
        lines=page['text'].splitlines()
        for i,line in enumerate(lines):
            if not CHINA.search(line) or re.search(r'中国地方|撤回|中止',line):
                continue
            for clause in re.split(r'[、。；;]',line):
                if CHINA.search(clause) and ACTION.search(clause):
                    choices.append((page['page'],clause.strip()))
            # Match independent geography headings followed by same-page bullets, such as a factory plan.
            if re.fullmatch(r'[\s・]*(?:中国)?[（(]?[\w・ ]{0,20}[）)]?[\s・]*',line) and not ACTION.search(line):
                for offset,near in enumerate(lines[i+1:i+5],1):
                    if re.search(r'ベトナム|タイ工場|台湾|マレーシア|韓国|米国|メキシコ',near):
                        break
                    if ACTION.search(near) and re.search(r'工場|工厂|生産|子会社|法人|倉庫|物流|拠点|建屋',near):
                        choices.append((page['page'],'\n'.join(lines[i:i+offset+1]).strip()))
    narrowed=[]
    for page,quote in choices:
        # Utilization ratios, hypothetical Q&A, market commentary and cross-country sales do not establish a project.
        normalized=unicodedata.normalize('NFKC',quote)
        city=region(normalized)
        if (not city or len(quote)>420 or re.search(
                r'稼[働動]率|ご質問|質問させ|そういった|でしょうか|売上高|市場(?:向け|拡大)|受注|参照|中国地方',normalized)):
            continue
        if not re.search(r'工場|工厂|生産|子会社|法人|倉庫|物流|拠点|建屋|センター|設備',normalized):
            continue
        period=re.search(r'(20\d{2})年\s*(?:(\d{1,2})月|([春夏秋冬]))', normalized)
        target=''
        if period:
            target=period[1]+(f'-{int(period[2]):02d}' if period[2] and 1<=int(period[2])<=12 else '年'+(period[3] or ''))
        action=next((label for label, terms in ACTION_TYPES if re.search(terms,normalized)), '')
        if action:
            narrowed.append((bool(target), city!='中国', -len(quote),page,quote,city,target,action))
    if not narrowed or not passes_noise(title,'\n'.join(p['text'] for p in pages)):
        return None
    _,_,_,page,quote,city,target,action=max(narrowed)
    return {'company':issuer[0],'security_code':code[1] if code else '', 'evidence_quote':quote,'pdf_page':page,
            'target_period':target,'region':city,'company_industry':classify(quote),
            'published':published.isoformat(),'action_type':action,'source_scope':'issuer_disclosure'}


def verify_root(url, company, stats, *, probe=False):
    root=site_root(url)
    if not root or blocked(root):
        return None
    response,doc=read(root,stats,**({'probe':True} if probe else {}))
    actual=site_root(response['url'])
    if not actual or blocked(actual):
        return None
    tree=public_html_tree(response['body'])
    for node in tree.xpath('//script|//style'):
        node.drop_tree()
    title=tree.xpath('string(//title)')
    labels=tree.xpath('//meta[@property="og:site_name"]/@content|//header//img/@alt|//h1/text()|//footer//small/text()|//footer//img/@alt')
    ownership=' '.join([title,*labels])
    if same_name(company,ownership):
        descriptions=tree.xpath('//meta[@name="description"]/@content')
        return actual,'\n'.join([title,*descriptions,node_text(tree)]),doc
    for anchor in tree.xpath('//a[@href]'):
        text=node_text(anchor)
        link=urljoin(response['url'],anchor.get('href'))
        if site_root(link)!=actual or not re.fullmatch(r'会社概要|企業概要|会社情報|企業情報',text.strip()):
            continue
        profile,evidence=read(link,stats,**({'probe':True} if probe else {}))
        if site_root(profile['url'])!=actual:
            return None
        section=public_html_tree(profile['body'])
        headings=section.xpath('//h1|//h2|//th|//td|//dt|//dd|//footer//small')
        identity=' '.join(node_text(node) for node in headings)
        if same_name(company,identity):
            return actual,node_text(section),{**doc,'findings':[evidence]}
        break
    return None


def issuer_industry(text, profile, quote):
    from radar.company_enrichment import BUSINESS

    # Prefer disclosed products over downstream customer-industry references.
    for line in profile.splitlines()[:2]:
        if re.search(r'放電加工機|工作機械|射出成形機|マシニングセンタ',line):
            return '精密机械与工业设备'
        value=classify(line)
        if value:
            return value
    for evidence in (quote,profile,text):
        lines=evidence.splitlines()
        for i,line in enumerate(lines):
            if BUSINESS.search(line):
                value=classify(' '.join(lines[i:i+2]))
                if value:
                    return value
        value=classify(evidence)
        if value:
            return value
    return ''


def parse_html_disclosure(item):
    parsed=parse_disclosure(item['title'],item['text'])
    if not parsed:
        return None
    visible=item['text']
    published=publication_date(item['published'])
    if not published:
        match=re.search(r'(?m)^\s*(20\d{2})[年./-]\s*(\d{1,2})[月./-]\s*(\d{1,2})(?:日|\b)',visible[:4000])
        if match:
            published=publication_date('-'.join((match[1],match[2].zfill(2),match[3].zfill(2))))
    if not published or not timezone.localdate()-timedelta(days=365)<=published<=timezone.localdate():
        return None
    # Compare actions against their local clause rather than another location elsewhere on the page.
    candidate=parsed['evidence_quote']
    if re.search(r'稼働率|市場(?:向け|拡大)|撤回|中止|現地法[\s]*人一覧',candidate):
        return None
    return {**parsed,'published':published.isoformat()}


def retain_original(document):
    from django.conf import settings
    from pathlib import Path

    for child in document.get('findings',[]):
        retain_original(child)
    path=Path(document['raw_path'])
    if path.parts[:2]!=('raw','probe_cache'):
        return
    source=settings.DATA_DIR/path
    destination=settings.DATA_DIR/'raw'/'archived'/path.name
    destination.parent.mkdir(parents=True,exist_ok=True)
    if not destination.exists():
        source.replace(destination)
    document['raw_path']=destination.relative_to(settings.DATA_DIR).as_posix()


def collect(entries,stats,limit,*, expansion=False):
    rows=[]
    roots={}
    from radar.search_provider import canonical_url

    chosen={}
    for url,title in entries:
        chosen.setdefault(canonical_url(url),title)
    known=set()
    hashes=set()
    if expansion:
        from radar.models import KeywordCandidate, Document, Lead
        known=set(KeywordCandidate.objects.values_list('canonical_url',flat=True))
        known.update(Lead.objects.values_list('url',flat=True))
        for data in KeywordCandidate.objects.values_list('result',flat=True):
            known.update(r.get('url') for r in data.get('verification',{}).get('sources',[]))
            known.add(data.get('source_url'))
        known.update(Document.objects.values_list('metadata__url',flat=True))
        known={canonical_url(u) for u in known if u}
        hashes=set(KeywordCandidate.objects.values_list('result__source_sha256',flat=True))
        hashes.update(Document.objects.values_list('content_hash',flat=True))
    attempted=0
    for url,title in chosen.items():
        relay=nikkei_tdnet(url)
        if expansion and (url in known or REPORT_TITLE.search(title+' '+url)):
            failure(stats,url,ValueError('already_in_baseline' if url in known else 'large_report_excluded'))
            continue
        if blocked(url) or (not relay and not site_root(url)):
            failure(stats,url,ValueError('excluded_not_issuer'))
            continue
        if expansion and not relay and discovery_kind(url)=='html' and not ISSUER_HTML.search(urlsplit(url).path):
            failure(stats,url,ValueError('not_disclosure_detail'))
            continue
        if attempted >= limit:
            failure(stats,url,ValueError('discovery_limit'))
            continue
        attempted+=1
        try:
            response,doc=read(url,stats,limit=8_000_000 if expansion else 12_000_000,**({'probe':True} if expansion else {}))
            pdf=response['body'].startswith(b'%PDF-')
            doc['media_kind']='pdf' if pdf else 'html'
            if relay and (not pdf or not nikkei_tdnet(response['url'])):
                raise ValueError('tdnet_relay_not_verified_pdf')
            if expansion and not pdf and not ISSUER_HTML.search(urlsplit(response['url']).path):
                raise ValueError('not_disclosure_detail')
            if expansion and doc['sha256'] in hashes:
                raise ValueError('duplicate_original_sha256')
            if blocked(response['url']) or (not relay and site_root(response['url'])!=site_root(url)):
                raise ValueError('issuer_redirect_mismatch')
            item=article(response,doc,title)
            published=publication_date(item['published'])
            if published and not timezone.localdate()-timedelta(days=365)<=published<=timezone.localdate():
                raise ValueError('issuer_document_outside_365_days')
            parsed=(parse_pdf_disclosure(item['title'],item['pages']) if pdf else parse_html_disclosure(item)) if expansion else parse_disclosure(item['title'],item['text'])
            if not parsed and doc['raw_path'].endswith('.pdf'):
                plain=pdf_pages(doc['raw_path'],25,mode='plain')
                item['pages']=plain['pages']
                item['text']='\n'.join(p['text'] for p in plain['pages'])[:180000]
                parsed=parse_pdf_disclosure(item['title'],item['pages']) if expansion else parse_disclosure(item['title'],item['text'])
            if not parsed:
                raise ValueError('issuer_or_china_action_not_identified')
            if expansion and pdf and REPORT_TITLE.search(item['pages'][0]['text'][:200]):
                raise ValueError('large_report_excluded')
            from radar.project_analysis import normalize_excerpt
            quote=normalize_excerpt(parsed['evidence_quote'])
            parsed.setdefault('pdf_page',next((p['page'] for p in item.get('pages',[]) if quote in normalize_excerpt(p['text'])),None))
            if relay:
                if not parsed.get('security_code'):
                    raise ValueError('tdnet_issuer_code_missing')
                retain_original(doc)
                rows.append({**item,**parsed,'publisher_verified':True,'publication_channel':'nikkei_tdnet'})
                hashes.add(doc['sha256'])
                counters(stats,url,1,1)
                continue
            key=site_root(url),parsed['company']
            if key not in roots:
                roots[key]=verify_root(url,parsed['company'],stats,**({'probe':True} if expansion else {}))
            profile=roots[key]
            if not profile:
                raise ValueError('issuer_root_identity_not_verified')
            root,text,evidence=profile
            if expansion:
                retain_original(doc)
                retain_original(evidence)
            if not item['title']:
                item['title']=parsed['company']+'｜发行人披露'
            rows.append({**item,**parsed,'publisher_verified':True,'publication_channel':'issuer_site','official_website':root,
                         'website_document':evidence,'website_text':text,
                         'company_industry':parsed['company_industry'] or issuer_industry(item['text'],text,parsed['evidence_quote'])})
            hashes.add(doc['sha256'])
            counters(stats,url,1,1)
        except (ValueError,OSError) as exc:
            counters(stats,url,0,0)
            failure(stats,url,exc)
    return rows


def media_targets(entries,stats,limit):
    """Use media as a pointer; never turn media prose into issuer evidence."""
    from urllib.parse import urljoin
    links=[]
    for url,title in entries[:limit]:
        if blocked(url):
            failure(stats,url,ValueError('excluded_discovery_endpoint'))
            continue
        if not (urlsplit(url).hostname or '').endswith(('.co.jp','.jp','.com.cn','.cn','.com')):
            continue
        try:
            response,_=read(url,stats)
            if response['body'].startswith(b'%PDF-'):
                links.append((url,title))
                continue
            tree=public_html_tree(response['body'])
            main=tree.xpath('//article|//main') or [tree]
            upstream=[]
            for link in main[0].xpath('.//a[@href]'):
                target=urljoin(response['url'],link.get('href'))
                label=node_text(link)
                if site_root(target) and not blocked(target) and site_root(target)!=site_root(url):
                    if re.search(r'原文|公告|官网|プレス|\.pdf(?:\?|$)', label+' '+target,re.I):
                        upstream.append((target,label or title))
            if not upstream:
                failure(stats,url,ValueError('media_without_issuer_link'))
            links.extend(upstream[:2])
        except (ValueError,OSError) as exc:
            failure(stats,url,exc)
    return links
