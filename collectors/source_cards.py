"""Free, bounded discovery behind the manual source cards."""
import re
from urllib.parse import urljoin, urlsplit

from collectors import issuer_ir_pdf as issuer
from collectors.aichi_kyoto_overseas import LETTER
from collectors.candidate_state import bounded
from collectors.china_entry_evidence import ACTION, article, failure, links, read
from radar.public_fetch import fetch
from radar.search_provider import canonical_url
from radar.website_check import public_html_tree

# Public list, detail path, link text. Empty list URL uses the source's registered URL.
LISTS = {
    'osaka_china_business_events': ('', r'/news/[^/]+/', r'出展|商談|博覧会|交易会'),
    'aichi_china_sites': (LETTER, r'/attachment/.*\.pdf$', r'【上海】'),
    'cifer_japan_registrations': ('https://www.maff.go.jp/pps/j/fac/export/index.html', r'\.pdf$', r'中国向け精米'),
    'jetro_china_pavilion': ('', r'/events/[^/]+/[^/]+\.html$', r'中国(?!地域|地方)|上海|広州|深圳'),
    'prefecture_export_awards': ('https://www.pref.hiroshima.lg.jp/soshiki/76/', r'/attachment/.*\.pdf$', r'審査結果|採択結果'),
    'jcea_monthly_reference': ('https://www.jc-web.or.jp/pages/56/', r'/files/libs/([0-9]+)/([0-9]{8})[0-9]+\.pdf$', ''),
    'prd_media_discovery': ('https://sw.gz.gov.cn/swzx/wstz/', r'/content/post_\d+\.html$', r'.'),
}
CORPORATE = {'parent_official_disclosures', 'issuer_ir_pdf'}
INDEX_ONLY = {'jetro_china_pavilion', 'jcea_monthly_reference'}
NOTES = {
    'osaka_china_business_events':'读取大阪上海事务所的商谈与出展公告，历史活动仅供研究。',
    'aichi_china_sites':'替代入口为爱知县上海驻点报告，反映市场动态。',
    'cifer_japan_registrations':'替代入口为日本农水省输华精米工厂及熏蒸仓库名录，仅覆盖大米。',
    'jetro_china_pavilion':'更新 JETRO 对华活动链接，打开原站看详情。招募公告仅供研究。',
    'prefecture_export_awards':'跟踪广岛县本期海外拓展评审结果，海外获奖不代表已供华。',
    'jcea_monthly_reference':'更新协会公开经济资料的索引链接。月报正文请在原站阅读。',
    'prd_media_discovery':'免费入口为广州市商务局外商投资报道，仅保留涉及日企的经营动态。',
    'parent_official_disclosures':'轮询已核实企业官网的新闻，核对具名在华动作。',
    'issuer_ir_pdf':'轮询已核实发行人官网的 IR 与公告，每次换一组企业。',
}
ALTERNATIVES = {
    'fna_fbc_exhibitors': ('fbc_ptc_manufacturers', '采集主办方展商专题'),
    'jgoodtech_profiles': ('smrj_100oku', '采集替代来源 · 100亿宣言'),
    'tdnet_biggo': ('issuer_ir_pdf', '采集替代来源 · 发行人官网'),
}


def listing(key, body, url):
    _, path, title = LISTS[key]
    if key == 'jcea_monthly_reference':
        records = []
        for href in public_html_tree(body).xpath('//a[.//img]/@href'):
            target = canonical_url(urljoin(url, href))
            match = re.fullmatch(path, urlsplit(target).path)
            if match and urlsplit(target).hostname == urlsplit(url).hostname:
                records.append((match[2], target, '資料日中経済 · PDF ' + match[1]))
        return [(u, t) for _, u, t in sorted(records, reverse=True)]
    entries = [(u, t) for u, t in links(body, url)
            if re.search(path, urlsplit(u).path, re.I) and re.search(title, t)]
    if key == 'osaka_china_business_events':
        dates = {}
        for li in public_html_tree(body).xpath('//ul[starts-with(@id,"new_list_")]/li'):
            href = li.xpath('string(.//a/@href)')
            dates[canonical_url(urljoin(url, href))] = li.xpath('string(span)').strip()
        entries.sort(key=lambda pair: dates.get(pair[0], ''), reverse=True)
    return entries


def issuer_links(key, source, stats):
    from radar.models import KeywordCandidate
    query = KeywordCandidate.objects.filter(result__official_website_status='verified')
    if key == 'issuer_ir_pdf':
        query = query.filter(provider=key)
    else:
        query = query.exclude(provider='issuer_ir_pdf')
    sites = list(dict.fromkeys(u for u in query.order_by('pk').values_list('result__official_website', flat=True)
                               if u and canonical_url(u)))
    if not sites:
        raise ValueError('尚无已核实的企业官网')
    start = int(source.checkpoint.get('website_offset', 0)) % len(sites)
    selected = (sites[start:] + sites[:start])[:2]
    rows = []
    def inspect(url):
        r = fetch(url, timeout=15)
        return list(links(r['body'], r['url']))
    def detail(u, t):
        p = urlsplit(u).path
        if issuer.REPORT_TITLE.search(t) or re.search(r'/themes/|/csr/|GRIスタンダード', p + ' ' + t, re.I):
            return False
        if p.lower().endswith('.pdf'):
            return True
        return bool(issuer.ISSUER_HTML.search(p) and
                    re.search(r'\d{2}|[^/]+\.html?$', p.rstrip('/').rsplit('/', 1)[-1]) and
                    not p.endswith(('index.html', 'index.htm')))
    for site in selected:
        try:
            urls = inspect(site)
            found = [(u,t) for u,t in urls if detail(u,t)]
            if not found:
                index = next((u for u,t in urls if issuer.ISSUER_HTML.search(urlsplit(u).path)
                              and re.search(r'ニュース|お知らせ|IR|投資家|一覧', t, re.I)), None)
                if index:
                    found = [(u,t) for u,t in inspect(index) if detail(u,t)]
            rows.extend(found)
        except (ValueError,OSError) as exc:
            failure(stats,site,exc)
    return rows, {'website_offset': (start + len(selected)) % len(sites)}


def collect(source):
    from collectors.china_entry_sources import admit
    key = source.key
    stats = {'errors': [], 'requests': [], 'details_read': [], 'index_entries': [], 'endpoints': []}
    checkpoint = {}
    limit = bounded(source.options.get('max_items'), 3, 1, 12)
    saved = retained = 0
    base = LISTS.get(key, ('',))[0] or source.url
    try:
        if key in CORPORATE:
            entries, checkpoint = issuer_links(key, source, stats)
        else:
            response = fetch(base, timeout=22)
            if key == 'prefecture_export_awards':
                entries = []
                for notice, name in links(response['body'], response['url']):
                    if not re.search(r'/animal-spirits-r[0-9]+\.html$', urlsplit(notice).path):
                        continue
                    if len(stats['endpoints']) >= 3:
                        break
                    try:
                        result = fetch(notice, timeout=18)
                        entries.extend(listing(key, result['body'], result['url']))
                        stats['endpoints'].append({'url':notice, 'title':name})
                    except (ValueError, OSError) as exc:
                        failure(stats, notice, exc)
            else:
                entries = listing(key, response['body'], response['url'])
        entries = list(dict.fromkeys(entries))
        if not entries:
            raise ValueError('本次入口未找到符合栏目范围的详情链接')
        seen = source.checkpoint.get('card_urls', [])
        entries.sort(key=lambda row: row[0] in seen)
        chosen = entries[:limit]
        visited = []
        for url, title in chosen:
            try:
                if key in INDEX_ONLY:
                    stats['index_entries'].append({'url':url, 'title':title})
                else:
                    r, doc = read(url, stats, limit=8_000_000)
                    item = article(r,doc,title)
                    stats['details_read'].append({'url':doc['url'],'title':title,'sha256':doc['sha256'],'raw_path':doc['raw_path']})
                    item['title'] = title
                    if key in CORPORATE:
                        parsed = issuer.parse_pdf_disclosure(title,item['pages']) if item['pages'] else issuer.parse_html_disclosure(item)
                        if parsed:
                            profile = issuer.verify_root(doc['url'],parsed['company'],stats)
                            if profile:
                                item.update(**parsed)
                                item.update(publisher_verified=True,official_website=profile[0],
                                            website_document=profile[2],publication_channel='issuer_site')
                            else:
                                parsed = None
                        keep = bool(parsed)
                    else:
                        item['source_scope'] = 'regional_reference'
                        keep = key != 'prd_media_discovery' or bool(re.search(r'日资|日企|日本企业|日本公司|株式会社',item['text']) and ACTION.search(item['text']))
                    if keep:
                        retained += 1
                        saved += int(admit(key,item) is not None)
                visited.append(url)
            except (ValueError,OSError) as exc:
                failure(stats,url,exc)
        checkpoint['card_urls'] = list(dict.fromkeys([u for u in seen if u not in visited] + visited))[-200:]
    except (ValueError,OSError) as exc:
        failure(stats,base,exc)
    read_any = bool(stats['details_read'] or stats['index_entries'])
    errors = stats['errors']
    status = 'success' if not errors else 'partial' if read_any or (key in CORPORATE and checkpoint) else 'failed'
    stats.update(details_admitted=retained, candidates_new=saved, confirmed_public_read=read_any, error_count=len(errors))
    return {'items':[], 'status':status, 'collector_checkpoint':checkpoint,
            'error':'；'.join(dict.fromkeys(e['reason'] for e in errors)), 'stats':stats}
