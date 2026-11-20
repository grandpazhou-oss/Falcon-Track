"""Explicit static-site adapters, registered only after inspecting real markup.
Registration is not a quality certification: reports record actual two-round results.
"""
import hashlib,re
from urllib.parse import urljoin,urlsplit
from .sources import Listing,ListingPage,LayoutError,node_text,first_text,parse_date,_attachments

KEYWORDS=r'运输|仓储|物流|配送|仓配|快递|邮寄|派送|货代|报关|承运|零担|整车|冷链|装卸|搬运'
CATALOG={
 'chinapost':dict(name='中国邮政 · 采购公告',host='www.chinapost.com.cn',entry='https://www.chinapost.com.cn/cn/category/1813/137331-1.html',list='https://www.chinapost.com.cn/cn/category/1813/137331-{page}.html',links="//a[contains(@href,'/cn/report/')]",body="//*[@id='ReportIDtext']",title="//*[@id='ReportIDname']",kind='procurement',note='官方物流同行采购/分包；参加采购可能需平台注册。'),
 'airchina':dict(name='中国航空集团 · 采购公告',host='pur.airchina.com.cn',entry='https://pur.airchina.com.cn/001/001001/moreinfo.html',list='https://pur.airchina.com.cn/001/001001/{page}.html',links="//a[contains(@class,'ewb-info-name')]",body="//div[contains(@class,'ewb-notice-copy')]",title="//*[contains(@class,'ewb-notice-title')]",kind='procurement',note='集团多法人公告；运输还需区分客运、货运和地面操作。'),
 'wutong':dict(name='物通网 · 物流招标',host='m.chinawutong.com',entry='https://m.chinawutong.com/zb/',list='https://m.chinawutong.com/zb/',links="//a[contains(@href,'/zb/211/') and contains(@href,'.html')]",body="//*[@id='content_main']",title="//h1",kind='procurement',note='行业发布；发布公司与采购法人需核对，电话受登录限制，含广告与历史记录。'),
 'xinhua':dict(name='浙江新华 · 招标公告',host='www.zxhsd.com.cn',entry='https://www.zxhsd.com/qydt/zbxx/',list='https://www.zxhsd.com/qydt/zbxx/page_{page}.shtml',links="//*[@id='word']//a[contains(@href,'.shtml')]",body="//*[@id='contentmain']",title="//h1",kind='procurement',note='官方图书运输、仓配与快递采购；需排除设备和非物流服务。'),
 'yunshu':dict(name='物流招标网 · 辅助发现',host='www.yunshuzhaobiao.com',entry='https://www.yunshuzhaobiao.com/ysgg-2703/010/1/',list='https://www.yunshuzhaobiao.com/ysgg-2703/010/{page}/',links="//a[contains(@href,'/news-')]",body="//div[contains(concat(' ',normalize-space(@class),' '),' detail-text ')]",title="//h1",kind='procurement',note='部分字段被匿名化，辅助发现；不按栏目存在即计为合格需求来源。'),
 'ccgp':dict(name='中国政府采购网 · 地方公开招标',host='www.ccgp.gov.cn',entry='https://www.ccgp.gov.cn/cggg/dfgg/gkzb/',list='https://www.ccgp.gov.cn/cggg/dfgg/gkzb/index_{zero}.htm',links="//ul[contains(@class,'c_list_bid')]//a",body="//div[contains(@class,'vF_detail_content')]",title="//h2",kind='procurement',note='跨独立采购机构；物流相关标题筛选，其他采购不入池。'),
}
CATALOG['xinhua']['host']='www.zxhsd.com'
CATALOG.update({
 'gmg':dict(name='国义招标 · 公告',host='www.gmgitc.com',entry='https://www.gmgitc.com/Bid/BidList.aspx?curNav=02',kind='procurement',note='官网使用的公开 JSON 列表与正文接口；多行业多需求企业，投标另需注册。'),
 'hubei':dict(name='湖北省招标 · 招标公告',host='www.hbbidding.com.cn',entry='https://www.hbbidding.com.cn/list/16.html',list='https://www.hbbidding.com.cn/list/16.html?page={page}',links="//a[contains(@href,'/view/') and contains(@href,'.html')]",body="//div[@class='psgCont']",title="//*[contains(@class,'psgTit')]",kind='procurement',note='跨企业代理公告；历史样本较多，物资供应与运输外包分开核验。'),
 'gdebidding':dict(name='广东招标中心 · 招标采购',host='www.gdebidding.com',entry='https://www.gdebidding.com/zbxxgg/index.jhtml',list='https://www.gdebidding.com/zbxxgg/index_{page}.jhtml',links="//a[contains(@href,'/zbxxgg/') and not(contains(@href,'index'))]",body="//div[@class='padding-big']",title="//h1",kind='procurement',note='公开代理采购公告，跨独立货主与物流企业；平台投标须另行注册。'),
 'cntcitc':dict(name='中招联合 · 服务采购',host='www.365trade.com.cn',entry='https://www.365trade.com.cn/zfwzb/index.jhtml',list='https://www.365trade.com.cn/zfwzb/index_{page}.jhtml',links="//ul[@class='searchList']//a[contains(@href,'/zfwzb/') and not(contains(@href,'index'))]",body="//*[@id='content']",title="//h1",kind='procurement',note='跨企业招采服务平台，公开公告全文；投标与文件领取可能需注册/收费。'),
 'genertec':dict(name='中国通用招标网 · 服务采购',host='www.china-tender.com.cn',entry='https://www.china-tender.com.cn/zbfw/index.jhtml',list='https://www.china-tender.com.cn/zbfw/index_{page}.jhtml',links="//a[contains(@href,'/zbfw/') and not(contains(@href,'index'))]",body="//div[contains(@class,'Jknr')]",title="//h1",kind='procurement',note='跨企业招采集合；公开公告全文，实际参与须遵循公告。'),
 'gz_environment':dict(name='广州生态环境局 · 建设项目拟审批公示',host='sthjj.gz.gov.cn',entry='https://sthjj.gz.gov.cn/hjgl/jsxm/hpspqgs/index.html',list='https://sthjj.gz.gov.cn/hjgl/jsxm/hpspqgs/index_{page}.html',links="//a[contains(@href,'/hpspqgs/content/post_')]",body="//*[@id='logPanel']",title="//h1",kind='early_signal',note='广州市公开环评拟审批；只作为建设变化信号，覆盖含广州开发区，非采购需求，日资背景须另核。仅刷新栏目首页。'),
 'teda_projects':dict(name='天津经开区 · 重大项目公示',host='www.tjbh.gov.cn',entry='https://www.tjbh.gov.cn/kfq/channels/11009.html',list='https://www.tjbh.gov.cn/kfq/channels/11009.html',links="//a[contains(@href,'/contents/11018/') and @title]",body="//*[@id='zoomcon']",title="//h1",kind='early_signal',note='天津经开区重大项目及规划公示，仅作为建设变化信号；日资背景及项目主体须另核，不证明物流采购。仅刷新栏目首页。'),
})

def list_url(key,page):
    if key=='gmg':
        from .gmg_source import list_url as api_url
        return api_url(page)
    spec=CATALOG[key]
    if page==1:return spec['entry']
    return spec['list'].format(page=page,zero=page-1)

def listing(key,tree,page,raw):
    spec=CATALOG[key];anchors=tree.xpath(spec['links'])
    if not anchors:raise LayoutError('Registered listing anchors missing')
    items=[];seen=set()
    for a in anchors:
        title=a.xpath('string(.//@title)') or first_text([a]);title=re.sub(r'^20\d{2}-\d{2}-\d{2}\s*','',title)
        title=re.sub(r'\s*(?:20\d{2}-)?\d{2}-\d{2}$','',title).strip()
        url=urljoin(spec['entry'],a.get('href',''))
        if key=='gz_environment':
            parsed=urlsplit(url)
            if parsed.hostname!=spec['host'] or parsed.username or parsed.password:continue
            url='https://'+spec['host']+parsed.path
        if urlsplit(url).hostname!=spec['host'] or url in seen:continue
        seen.add(url)
        if key=='gz_environment':
            if not re.search(r'环境影响|环评',title):continue
        elif key=='teda_projects':
            if not re.search(r'项目|建设',title):continue
        elif not re.search(KEYWORDS,title):continue
        identifier=hashlib.sha256(url.encode()).hexdigest()[:40]
        pub=parse_date(node_text(a.getparent()))
        items.append(Listing(identifier,url,title,published_date=pub,metadata={'list_page':page,'discovery_url':list_url(key,page)}))
    # Verified site-specific pagination metadata, never inferred from absence of items.
    if key=='chinapost':
        total=re.search(r'total:(\d+)',raw);next_page=page+1 if total and page*10<int(total[1]) else None
    elif key=='airchina':
        total=re.search(r'var total\s*=\s*(\d+)',raw);next_page=page+1 if total and page*10<int(total[1]) else None
    elif key=='gz_environment':next_page=page+1 if any(f'index_{page+1}.html' in a.get('href','') for a in tree.xpath('//a')) else None
    elif key in {'wutong','teda_projects'}:next_page=None # bounded head-refresh feed; no archive coverage claim
    elif key=='xinhua':
        total=re.search(r'(?:共|totalPage\s*=\s*)\s*(\d+)\s*(?:页|;)',raw);next_page=page+1 if total and page<int(total[1]) else None
        if not total:
            next_page=page+1 if re.search(r'page_.*?\.shtml',raw) and page<20 else None
    elif key in {'cntcitc','genertec','gdebidding'}:
        next_page=page+1 if any(f'index_{page+1}.jhtml' in a.get('href','') for a in tree.xpath('//a')) else None
    elif key=='hubei':
        next_page=page+1 if any(f'page={page+1}' in a.get('href','') for a in tree.xpath('//a')) else None
    elif key=='ccgp':
        total=re.search(r'Pager\(\{size:(\d+)',raw);next_page=page+1 if total and page<int(total[1]) else None
    else:
        next_page=page+1 if any('下一页' in node_text(a) for a in tree.xpath('//a')) else None
    return ListingPage(items,next_page,explicit_empty=not items)

def detail(key,tree,item,raw):
    spec=CATALOG[key];nodes=tree.xpath(spec['body'])
    if not nodes:raise LayoutError('Registered detail body missing')
    for node in nodes:
        for child in node.xpath('.//p|.//div'):
            if node_text(child).startswith(('上一篇：','下一篇：')) and child.getparent() is not None:child.drop_tree()
    text='\n'.join(node_text(n) for n in nodes)
    title=first_text(tree.xpath(spec['title'])) or item.title
    title=title.split("\n")[0].strip()
    if len(title)<8:title=item.title
    metadata={**item.metadata,'url':item.url,'body_access':'public'}
    if key=='gz_environment':
        metadata.update(signal_type='environmental_project',signal_note='环评是建设变化信号，不证明存在物流采购。')
    elif key=='teda_projects':
        metadata.update(signal_type='construction_project',signal_note='规划公示是建设变化信号，不证明存在物流采购；政府联系人不是采购联系人。')
    access='public'
    if key=='wutong':
        heads=tree.xpath('//ul[@class="notice_head_ul"]/li')
        extra=[]
        for row in heads:
            label=first_text(row.xpath('./text()'))
            if '联系电话' in label:continue
            value=' '.join(node_text(x) for x in row.xpath('./div'))
            if value:extra.append(label+'：'+value)
        text='\n'.join(extra+[text]);metadata['role_note']='发布公司为发布者，实际采购角色待原文核实';metadata['contact_access']='login_required'
    if key=='yunshu' and re.search(r'\*\*|点击查看|会员',text):access='partial';metadata['body_access']='redacted'
    if len(text)<40:raise LayoutError('Detail body too short')
    pub=item.published_date
    if key=='chinapost':pub=parse_date(first_text(tree.xpath('//*[@id="ReportIDIssueTime"]'))) or pub
    elif key=='airchina':pub=parse_date(first_text(tree.xpath('//*[contains(@class,"ewb-notice-sources")]'))) or pub
    elif key=='teda_projects':pub=parse_date(first_text(tree.xpath('//h1'))) or pub
    attachments=_attachments(nodes,item.url)
    if key=='teda_projects':
        for anchor in nodes[0].xpath('.//a[@href]'):
            url=urljoin(item.url,anchor.get('href',''))
            parsed=urlsplit(url)
            if parsed.scheme=='https' and parsed.hostname==spec['host'] and not parsed.username and not parsed.password and re.search(r'\.(?:jpe?g|png)(?:[?#]|$)',url,re.I) and not any(x['url']==url for x in attachments):
                attachments.append({'url':url,'label':first_text([anchor])[:300] or '规划图附件'})
    return {'external_id':item.external_id,'url':item.url,'title':title,'text':text,'html':raw,'published_date':pub,
            'kind':spec['kind'],'access_level':access,'listed_deadline':None,'attachments':attachments,'metadata':metadata}
