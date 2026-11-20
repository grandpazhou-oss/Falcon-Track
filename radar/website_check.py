"""Check one directory-listed website with public-fetch protections, no crawl."""
import hashlib,re,unicodedata
from urllib.parse import urlsplit
from django.conf import settings
from django.utils import timezone
from lxml import html
from collectors.research_common import read_original
from collectors.sources import node_text
from .models import Document
from .public_fetch import request_budget


def _candidate(event):
    fact=next((f for f in event.facts if f.get('field')=='website'),None)
    if not fact:raise ValueError('该记录没有名录列示的网站。')
    for value in re.findall(r'https?://[^\s<>"\'（）()]+',str(fact.get('value',''))):
        candidate=value.rstrip('、，。.,;；')
        try:
            parsed=urlsplit(candidate)
            if parsed.hostname and not parsed.username and not parsed.password:return candidate
        except ValueError:continue
    raise ValueError('名录列示的网站地址无法识别。')


def _explicit_name(text,participants):
    names=[p['name'] for p in participants if p.get('role') in {'japan_parent','japan_company'} and p.get('name')]
    def normalized(v):return re.sub(r'\s+','',unicodedata.normalize('NFKC',v)).casefold()
    for line in text.splitlines():
        for name in names:
            if len(normalized(name))>=4 and normalized(name) in normalized(line):return name,line.strip()
    return '',''


def check_listed_website(event):
    url=_candidate(event);now=timezone.now().isoformat();attempts=[]
    try:
        with request_budget(10,30):
            try:
                response,evidence=read_original(url,max_bytes=2_000_000,timeout=18)
                attempts.append({'url':url,'status':'read'})
            except Exception as exc:
                attempts.append({'url':url,'status':'failed','reason':type(exc).__name__+': '+str(exc)[:150]})
                # Try the same host over HTTPS only after a network/HTTP failure;
                # never retry to get around an explicit or unavailable robots file.
                if not url.startswith('http://') or 'robots' in str(exc).lower():raise
                secure='https://'+url[len('http://'):]
                try:response,evidence=read_original(secure,max_bytes=2_000_000,timeout=18)
                except Exception as next_exc:
                    attempts.append({'url':secure,'status':'failed','reason':type(next_exc).__name__+': '+str(next_exc)[:150]})
                    raise
                attempts.append({'url':secure,'status':'read'})
        if response['body'].startswith((b'%PDF-',b'PK\x03\x04')):raise ValueError('官网首页不是可读 HTML')
        try:
            response['body'].decode('utf-8')
            parser=html.HTMLParser(encoding='utf-8',no_network=True)
        except UnicodeDecodeError:parser=html.HTMLParser(no_network=True)
        tree=html.fromstring(response['body'],parser=parser)
        for unwanted in tree.xpath('//script|//style|//noscript'):unwanted.drop_tree()
        footer=[' '.join(x.itertext()) for x in tree.xpath('//footer')]
        text=(node_text(tree)+'\n'+'\n'.join(footer))[:350000]
        name,quote=_explicit_name(text,event.participants)
        metadata={'kind':'website_check','url':evidence['url'],'candidate_url':url,'observed_at':now,
                  'status':'explicit_name_match' if name else 'identity_unverified',
                  'name':name,'quote':quote,'attempts':attempts,'notice':'仅核对网页是否明确出现目录列示的日本企业名；不证明法律所有权或采购。'}
        doc,created=Document.objects.get_or_create(lead=event.lead,content_hash=evidence['sha256'],
                         defaults={'raw_path':evidence['raw_path'],'text':text,'metadata':metadata})
        if not created and doc.metadata.get('kind')=='website_check':
            doc.metadata=metadata;doc.save(update_fields=['metadata'])
        return doc
    except Exception as exc:
        # Keep a trace without inventing source content or treating failure as a negative identity match.
        digest=hashlib.sha256(('website-check-error:'+url+':'+now).encode()).hexdigest()
        return Document.objects.create(lead=event.lead,content_hash=digest,text='',metadata={'kind':'website_check',
            'url':url,'observed_at':now,'attempts':attempts,'status':'blocked_or_failed','error':type(exc).__name__+': '+str(exc)[:200]})

# Discovery yields candidates only; a name on a page does not prove legal ownership.
NOISE_DOMAINS = frozenset({
    'google.com', 'news.google.com', 'bing.com', 'baidu.com', 'baike.baidu.com',
    'wikipedia.org', 'zhihu.com', 'linkedin.com', 'facebook.com', 'x.com',
    'tianyancha.com', 'qcc.com', 'aiqicha.baidu.com', '企查查.com', '1688.com',
    'made-in-china.com', 'alibaba.com', 'jetro.go.jp', 'prtimes.jp',
})


def is_noise_domain(url):
    host = (urlsplit(url).hostname or '').lower().rstrip('.')
    return not host or any(host == domain or host.endswith('.' + domain) for domain in NOISE_DOMAINS)


def public_html_tree(body):
    try:
        body.decode('utf-8')
        parser = html.HTMLParser(encoding='utf-8', no_network=True)
    except UnicodeDecodeError:
        parser = html.HTMLParser(no_network=True)
    return html.fromstring(body, parser=parser)


def discover_official_site(company_name, results):
    """Return a name-matched home page for human review, never an ownership fact."""
    from urllib.parse import urlunsplit
    from .search_provider import canonical_url
    if len(company_name.strip()) < 4:
        return None
    seen = set()
    for item in results[:5]:
        url = canonical_url(item.url)
        if not url or is_noise_domain(url):
            continue
        parts = urlsplit(url)
        home = urlunsplit((parts.scheme, parts.netloc, '/', '', ''))
        if home in seen:
            continue
        seen.add(home)
        try:
            response, evidence = read_original(home, max_bytes=1_500_000, timeout=18)
            if is_noise_domain(response['url']):
                continue
            tree = public_html_tree(response['body'])
            text = node_text(tree) + '\n' + '\n'.join(' '.join(node.itertext()) for node in tree.xpath('//footer'))
            name, quote = _explicit_name(text, [{'role':'japan_company','name':company_name}])
            if name:
                return {'status':'name_matched_ownership_unverified', 'company':name,
                        'url':evidence['url'], 'quote':quote[:600], 'raw_path':evidence['raw_path'],
                        'sha256':evidence['sha256']}
        except Exception:
            continue
    return None


def probe_official_site(site, today=None, max_pages=8):
    """At most two links from the verified-name home; old/undated items are excluded."""
    from datetime import timedelta
    from urllib.parse import urljoin
    from django.utils.dateparse import parse_date
    from .search_provider import canonical_url
    from .research import _mainland_evidence
    if site.get('status') != 'name_matched_ownership_unverified':
        return []
    today = today or timezone.localdate()
    root = canonical_url(site['url'])
    host = urlsplit(root).hostname
    sections = ('news', 'topics', 'ir', 'company', 'cn', 'china')
    queue = [(urljoin(root, '/' + part), 1) for part in sections]
    seen, findings = {root}, []
    while queue and len(seen) <= max_pages:
        url, depth = queue.pop(0)
        url = canonical_url(url)
        if not url or url in seen or urlsplit(url).hostname != host or is_noise_domain(url):
            continue
        seen.add(url)
        try:
            response, evidence = read_original(url, max_bytes=1_500_000, timeout=15)
            if urlsplit(response['url']).hostname != host:
                continue
            tree = public_html_tree(response['body'])
            text = node_text(tree)[:100000]
            dates = re.findall(r'(?<!\d)(20\d{2})[年/.-](\d{1,2})[月/.-](\d{1,2})', text[:10000])
            recent = False
            for year, month, day in dates[:50]:
                day_value = parse_date(f'{int(year):04}-{int(month):02}-{int(day):02}')
                if day_value and today - timedelta(days=366) <= day_value <= today:
                    recent = True
                    break
            if recent and _mainland_evidence({'quote':text}) and re.search(r'設立|设立|新工場|建設|建设|扩产|拡張|物流|子会社|子公司|工厂|倉庫|仓库', text):
                findings.append({'url': evidence['url'], 'raw_path': evidence['raw_path'], 'sha256':evidence['sha256'],
                                 'date_check':'page_contains_recent_date', 'status':'needs_human_verification'})
            if depth == 1:
                for link in tree.xpath('//a[@href]')[:100]:
                    target = canonical_url(urljoin(url, link.get('href')))
                    if target and urlsplit(target).hostname == host and target not in seen:
                        queue.append((target, 2))
        except Exception:
            continue
    return findings
