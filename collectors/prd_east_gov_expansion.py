"""Public project notices, screened before intake for project-side Japanese links."""
import re
from urllib.parse import urlsplit

from collectors.china_entry_evidence import ACTION, region, article, counters, failure, links, read

DONGGUAN='https://im.dg.gov.cn/gkmlpt/'
HUANGPU='http://www.hp.gov.cn/zwgk/gsgb/phgb/'
TARGETS=(HUANGPU,)
QUERIES=(
 'site:gov.cn 日资 OR 日本 增资扩产 OR 签约 OR 投产 上海 OR 苏州 OR 昆山 OR 太仓 OR 无锡 OR 常州 OR 南通',
 'site:gov.cn 日资 OR 日本 增资扩产 OR 签约 OR 投产 广州 OR 东莞 OR 深圳 OR 佛山 OR 珠海',
)
JP_OWNER=re.compile(r'日资(?:企业|项目|制造|公司)|日企|日系(?:企业|项目|公司)|日本(?:[\w]{0,8}企业|母公司|[\w（）()]{1,26}株式会社)|株式会社')
NON_OWNER=re.compile(r'采购|引进设备|购买|设备供应|对标|日资企业参观|与日本交流|韩国|韓国|韩国籍|韩国企业|德资|法国企业|瑞典企业')
GOV_URL_PATH=re.compile(r'/content/(?:[\d/]+/)?post_\d+\.html$|/(?:art_|content_)\d+(?:\.html?)?$')
PROJECT=re.compile(r'增资|扩产|扩建|投产|技改|提质|新建|新设|环评|环境影响|开工|签约|竣工')


def official(url):
    host=urlsplit(url).hostname or ''
    return host.endswith('.gov.cn') and urlsplit(url).scheme in {'http','https'}


def screened(title, text):
    if not PROJECT.search(title + ' ' + text):
        return ''
    for line in re.split(r'[。！？\n]',title + '\n' + text):
        if ACTION.search(line) and JP_OWNER.search(line) and not NON_OWNER.search(line):
            return line.strip()[:500]
    return ''

def disclosed_company(quote):
    legal = re.search(r'(?:日本)?[A-Za-z\u4e00-\u9fff&（）()]{2,28}株式会社', quote)
    if legal:
        return legal.group()
    named = re.search(r'日本[^，。]{0,10}?(?:企业|厂商)([A-Za-z\u4e00-\u9fff&]{2,20}?)(?:在|于|的)', quote)
    return named.group(1) if named else ''



def collect(stats, extra_urls=(), max_details=8, targets=TARGETS):
    out=[]
    seen=set()
    discovered={}
    for index in targets:
        try:
            r,_=read(index,stats)
            raw=[(url,title) for url,title in links(r['body'],r['url'])
                 if GOV_URL_PATH.search(urlsplit(url).path)]
            if not raw: raise ValueError('no_static_article_links')
            rows=sorted(raw,key=lambda row:not bool(JP_OWNER.search(row[1])))[:max_details]
            for url,title in rows:
                discovered.setdefault(url,(title,index))
            counters(stats,index,len(raw),0)
        except (ValueError,OSError) as exc: failure(stats,index,exc)
    for url,title in extra_urls:
        if official(url):
            discovered.setdefault(url,(title,'tavily:gov.cn'))
    for url,(title,index) in discovered.items():
        if url in seen:continue
        seen.add(url)
        try:
            r,doc=read(url,stats)
            if not official(r['url']):raise ValueError('non_government_final_url')
            item=article(r,doc,title,min_chars=50)
            evidence=screened(item['title'],item['text'])
            counters(stats,url,1,int(bool(evidence)))
            if not evidence:continue
            item.update(evidence_quote=evidence,region=region(evidence+' '+item['title']),
                        company=disclosed_company(evidence),
                        source_scope='government_project',japan_evidence=evidence)
            for counted in stats['endpoints']:
                if counted['url']==index:
                    counted['retained']+=1
            out.append(item)
        except (ValueError,OSError) as exc: failure(stats,url,exc)
    return out
