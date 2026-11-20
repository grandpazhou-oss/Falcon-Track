"""Live directories discover companies; directory membership is not logistics demand or equity proof."""
import hashlib
from lxml import html
from urllib.parse import urljoin
from django.utils import timezone
from django.conf import settings
from django.db import transaction
from filelock import FileLock
from .models import Company
from .public_fetch import fetch,robots_allowed,FetchError
from collectors.sources import node_text
import re

DIRECTORIES={
 'shfia_members':{'key':'shfia_members','name':'上海外商投资协会 · 2023公开会员名录','url':'https://www.shfia.cn/cms/xinwen/1590.html','kind':'enterprise_discovery','note':'公开历史会员名单每批50家；包含多国与本土机构，非日资证明，不计日本种子数量。'},
 'japan_directory_mol_recruitment':{'key':'japan_directory_mol_recruitment','name':'商船三井物流中国 · 公开招聘','url':'https://www.mol-logistics.cn/TalentAcquisition/list.aspx?page=1','kind':'enterprise_discovery','note':'日系物流同行招聘信号；发布时间不代表仍开放，不等于采购。'},
 'japan_directory_suzhou_checked':{'key':'japan_directory_suzhou_checked','name':'苏州高新区 · 日企公开名录','url':'https://snd-jp.com/wp/snd/about-html/about-jp/','kind':'enterprise_discovery','note':'公开名录每批50家直接入库；官网核验仅增强，法人现状及股权仍须核实。'},
 'japan_directory_yingchuang':{'key':'japan_directory_yingchuang','name':'英创人才 · 日企招聘专题','url':'https://www.yingchuang.com/special/','kind':'enterprise_discovery','note':'日企招聘专题中的企业发现；岗位发布时间及有效性须另核，不代表物流采购。'},
 'japan_directory_chongqing':{'key':'japan_directory_chongqing','name':'重庆日本商工俱乐部 · 企业介绍','url':'https://cq-jpclub.com/company','kind':'enterprise_discovery','note':'企业介绍仅反映关联，日资背景和需求须另核；首页分批读取独立详情。'},
 'jetro_directory':{'key':'jetro_directory','name':'JETRO · 在华日资企业公开链接','url':'https://www.jetro.go.jp/china/contribution.html','kind':'enterprise_discovery','note':'官方公开关联名录；仅为日资关联线索，不等于股权核实，不计物流需求。'},
 'shfia_directory':{'key':'shfia_directory','name':'上海外商投资协会 · 日资企业分会','url':'https://www.shfia.cn/cms/japanese-enterprise.html','kind':'enterprise_discovery','note':'分会公开组织与会员列表；关联线索须针对当前法人另核股权。'},
}

def discover(source,limit=50):
    if source.key == "shfia_members":
        from .association_rosters import discover_association
        return discover_association(source, limit)
    if source.key == 'japan_directory_mol_recruitment':
        from .mol_recruitment import discover_mol
        return discover_mol(source)
    if source.key == 'japan_directory_suzhou_checked':
        from .suzhou_crosscheck import discover_suzhou
        return discover_suzhou(source, limit)
    if source.key in {'japan_directory_chongqing','japan_directory_yingchuang'}:
        from .verified_japan_directories import discover_verified
        return discover_verified(source, limit)
    url=source.url
    if not robots_allowed(url):raise FetchError('robots_or_robots_unavailable')
    response=fetch(url);tree=html.fromstring(response['body']);digest=hashlib.sha256(response['body']).hexdigest()
    path=settings.DATA_DIR/'raw'/'directories'/f'{digest}.html';path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists():path.write_bytes(response['body'])
    candidates=[]
    if source.key=='jetro_directory':
        for a in tree.xpath('//a[@href]'):
            name=node_text(a).strip()
            if re.fullmatch(r'[\u4e00-\u9fffA-Za-z（）()·\s]{3,100}(?:有限公司|企業集団|企业集团)',name):
                candidates.append((name,urljoin(url,a.get('href')),name))
    else:
        for row in tree.xpath('//tr'):
            cells=row.xpath('./td')
            for cell in cells:
                value=node_text(cell)
                if re.fullmatch(r'[\u4e00-\u9fffA-Za-z（）()·\s]{3,100}(?:有限公司|有限责任公司|分公司)',value):candidates.append((value,'',node_text(row)[:500]))
    unique=list({x[0]:x for x in candidates}.values())
    if not unique:raise ValueError('directory_layout_no_entities')
    created=0;checked=[]
    with FileLock(str(settings.DATA_DIR/'directory-write.lock'),timeout=30), transaction.atomic():
        locked=type(source).objects.select_for_update().get(pk=source.pk)
        offset=int(locked.checkpoint.get('directory_offset',0)) % len(unique)
        batch=unique[offset:offset+limit]
        for name,website,quote in batch:
            company,new=Company.objects.get_or_create(name=name);created+=int(new)
            company=Company.objects.select_for_update().get(pk=company.pk)
            if not company.subscribed:continue
            evidence={'source':source.key,'url':url,'document_hash':digest,'raw_path':path.relative_to(settings.DATA_DIR).as_posix(),'quote':quote,'status':'japan_affiliation_unverified','checked_at':timezone.now().isoformat()}
            previous=list(company.discovery_evidence or [])
            identity=('source','url','document_hash','quote','status')
            if not any(isinstance(e,dict) and all(e.get(k)==evidence.get(k) for k in identity) for e in previous):
                previous.append(evidence)
            company.discovery_evidence=previous
            company.profile=dict(company.profile or {})
            company.profile.setdefault('last_discovered_at',timezone.now().isoformat())
            if website and not company.profile.get('website'):company.profile['website']=website
            company.save(update_fields=['discovery_evidence','profile','updated_at']);checked.append(name)
        locked.checkpoint={**locked.checkpoint,'directory_offset':(offset+len(batch))%len(unique)}
        locked.save(update_fields=['checkpoint'])
    source.checkpoint=locked.checkpoint
    return {'items':[], 'status':'success','next_cursor':1, 'stats':{'directory':True,'companies_found':len(unique),'companies_processed':len(checked),'companies_new':created,'limit':limit,'document_hash':digest,'discovery_url':url,'names':checked,'error_count':0,'coverage_complete':len(unique)<=limit,'offset':offset,'scope':'公开名录分批滚动；非物流需求'}}
