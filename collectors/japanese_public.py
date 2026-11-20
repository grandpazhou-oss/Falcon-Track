"""Registered public list-driven trials; no login, CAPTCHA or model calls."""
import hashlib
import json
import re
from urllib.parse import urlencode, urljoin, urlsplit
from lxml import html
from radar.public_fetch import fetch
from .sources import node_text, parse_date, _attachments

REGISTRY = {
 'dfmc_nissan': {'key':'dfmc_nissan','name':'东风采购 · 日系合资企业采购（试用）','url':'https://etp.dfmc.com.cn/jyxx/004002/trade_info_newf.html','kind':'procurement','note':'公开招标与邀请询比均保留；日产品牌不替代采购法人，邀请项目不等于公开可报名。每轮有界搜索，默认关闭。'},
 'persol_jobs': {'key':'persol_jobs','name':'PERSOL（英创新站）· 日企物流岗位搜索（试用）','url':'https://persolchina.career.gllue.com/jobs','kind':'recruitment','note':'公开搜索与独立JD；匿名雇主保留为空、日期未知不补猜。每次轮转一组关键词，最多3页/50条。'},
 'dfwlg_procurement': {'key':'dfwlg_procurement','name':'东风物流 · 公开采购与分包（试用）','url':'https://www.dfwlg.com/node/330','kind':'procurement','note':'物流服务商及关联单位公开信息；业务客户不等于采购方，历史公示期不代表当前可报名。默认关闭。',
     'validation':{'conclusion':'2026-11-14 已核三条历史公开详情及两轮采集；采购主体与当前参与状态仍须逐条核实。'}},
}
JOB_QUERIES = ('日企 物流','日企 关务','日企 仓库','日语 供应链','日语 进出口')
CITIES = ('上海','苏州','无锡','广州','东莞','深圳','天津','大连','青岛')
LOGISTICS = r'物流|运输|仓储|仓库|包装|集装箱|调拨|货代|供应链|关务|进出口|冷链|危化|危险品|海运|空运|SCM'


def read(url, *, form=None):
    response=fetch(url,timeout=35,form=form)
    from django.conf import settings
    digest=hashlib.sha256(response['body']).hexdigest()
    path=settings.DATA_DIR/'raw'/'source_lists'/f'{digest}.html';path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists():path.write_bytes(response['body'])
    return response, {'url':url,'sha256':digest,'raw_path':path.relative_to(settings.DATA_DIR).as_posix(),
                      'redirect_chain':response.get('redirect_chain',[]),'robots_redirect_chains':response.get('robots_redirect_chains',[])}


def dfmc_detail(body, row, discovery):
    from radar.extraction import _deadline_candidates
    tree=html.fromstring(body)
    nodes=tree.xpath('//div[@class="article-info"]')
    if not nodes:raise ValueError('dfmc_detail_layout_missing')
    text=node_text(nodes[0])
    if len(text)<80:raise ValueError('dfmc_detail_too_short')
    headers=tree.xpath('//div[@class="article"]/h3')
    title=node_text(headers[0]) if headers else row['title']
    method='邀请询比' if re.search(r'邀请询比|被邀请单位名称',text) else row.get('zbfs','待核实')
    deadlines=_deadline_candidates(text)
    metadata={'schema_version':1,'discovery_url':discovery,'procurement_method':method,
              'invitation_required':'邀请' in method or bool(re.search(r'仅限.{0,12}邀请',text)),
              'listed_buyer':row.get('jianshedanwei',''),'listed_end_time':row.get('bmjzsj',''),
              'project_number':row.get('zbbh',''),'announcement_stage':row.get('categoryname',''),
              'project_title':re.sub(r'(?:变更公告|更正公告|成交候选人公示|成交结果公告|采购公告).*$', '', title),
              'deadlines':deadlines,'signal_type':'procurement',
              'participation_note':'公开公告；文件获取与响应可能需要注册/CA，邀请采购须确认已受邀。'}
    return {'external_id':row['infoid'],'url':urljoin('https://etp.dfmc.com.cn',row['infourl']),
            'title':title,'text':text,'html':body.decode('utf-8','replace'),
            'published_date':parse_date(row.get('infodate','')),'access_level':'public',
            'attachments':_attachments(nodes,urljoin('https://etp.dfmc.com.cn',row['infourl'])),'metadata':metadata}


def collect_dfmc(source):
    from .candidate_state import CandidateLedger,Limits,LimitReached,bounded
    base='https://etp.dfmc.com.cn';items=[];logs=[];errors=[];checked=0;stopped=''
    options=source.options or {};plan=Limits(options);ledger=CandidateLedger(source.checkpoint,'dfmc_candidates_v1')
    pages=bounded(options.get('head_pages'),3,1,6)  # index 6+ requires verification
    keywords=options.get('keywords') or ['日产']
    if isinstance(keywords,str):keywords=re.split(r'[,，\n]+',keywords)
    keywords=[str(x).strip()[:50] for x in keywords if str(x).strip()][:10] or ['日产']
    cursors=dict(ledger.state.get('page_cursors',{}))
    query_index=int(ledger.state.get('query_index',0))%len(keywords)
    keywords=keywords[query_index:]+keywords[:query_index]
    listing_limit=max(2,min(21,plan.total//2));listing_limited=False
    try:
        cfg,e=plan.read(read,base+'/js/webBuilderCommon.js');logs.append(e)
        match=re.search(rb'var siteInfo\s*=\s*(\{[^\n]+?\})',cfg['body'])
        if not match:raise ValueError('dfmc_public_config_missing')
        config=json.loads(match[1]);project=config.get('projectName','')
        if project != '/EpointWebBuilder':raise ValueError('dfmc_api_path_changed')
        api=base+project+'/rest/secactionopen/getSecInfoListYzm'
        for keyword in keywords:
            for category in ('004002','004001'):
                key=keyword+'|'+category;deep=max(1,min(pages-1,int(cursors.get(key,1)))) if pages>1 else 0
                for page in ([0,deep] if deep else [0]):
                    if plan.calls>=listing_limit:
                        listing_limited=True;break
                    try:
                        r,e=plan.read(read,api,form={'siteGuid':config['siteGuid'],'categoryNum':category,'content':keyword,
                            'pageindex':str(page),'pagesize':'10','startdate':'','enddate':'','projecttype':'',
                            'bmzt':'','jylx':'','clientHost':'etp.dfmc.com.cn'})
                        e.update(category=category,page=page,keyword=keyword);logs.append(e)
                        payload=json.loads(r['body']).get('custom',{})
                        if 'infodata' not in payload:raise ValueError('dfmc_list_unavailable_or_verification')
                        rows=payload['infodata'];checked+=len(rows)
                        for row in rows:
                            title=row.get('title','')
                            if not re.search(LOGISTICS+r'|服务|采购项目',title,re.I):continue
                            url=urljoin(base,row.get('infourl',''));p=urlsplit(url)
                            if p.scheme!='https' or p.hostname!='etp.dfmc.com.cn' or not p.path.startswith('/jyxx/'):continue
                            identity=str(row.get('infoid') or hashlib.sha256(url.encode()).hexdigest())
                            ledger.add(identity,url,keyword,{'url':e.get('url'),'sha256':e.get('sha256'),'raw_path':e.get('raw_path')},row=row)
                        if page:
                            cursors[key]=1 if page+1>=pages or len(rows)<10 else page+1
                    except LimitReached as exc:stopped=str(exc);break
                    except Exception as exc:
                        errors.append({'url':api,'category':category,'page':page,'error':type(exc).__name__+': '+str(exc)[:180]});break
                if stopped or listing_limited:break
            if stopped or listing_limited:break
    except LimitReached as exc:stopped=str(exc)
    except Exception as exc:errors.append({'url':base,'error':type(exc).__name__+': '+str(exc)[:180]})
    generic_used=0;generic_limit=bounded(options.get('generic_detail_budget'),2,0,20)
    for candidate in ledger.pending():
        if len(items)>=plan.accepted:stopped=stopped or 'accepted_limit';break
        generic=not re.search(LOGISTICS,candidate.get('row',{}).get('title',''),re.I)
        if generic and generic_used>=generic_limit:continue
        try:
            if generic:generic_used+=1
            d,e=plan.read(read,candidate['url'],detail=True);logs.append(e)
            item=dfmc_detail(d['body'],candidate['row'],source.url)
            if not re.search(LOGISTICS,item['title']+'\n'+item['text'],re.I):ledger.done(candidate,'excluded');continue
            item['metadata']['matched_queries']=candidate['queries'];items.append(item);ledger.done(candidate,'ingested')
        except LimitReached as exc:stopped=str(exc);break
        except Exception as exc:
            if str(exc) in {'time_limit','total_request_limit'}:stopped=str(exc);break
            ledger.failed(candidate,exc);errors.append({'url':candidate['url'],'error':type(exc).__name__+': '+str(exc)[:180]})
    pending=sum(x.get('status') in {'pending','retry'} for x in ledger.entries.values())
    if listing_limited:stopped=stopped or 'listing_budget'
    return {'items':items,'status':'partial' if errors or stopped or pending else 'success','next_cursor':1,
            'collector_checkpoint':ledger.save(page_cursors=cursors,query_index=(query_index+1)%len(keywords)),
            'stats':{'error_count':len(errors),'errors':errors,'list_rows_checked':checked,'details_ok':len(items),
                     'details_attempted':plan.detail_calls,'request_calls':plan.calls,'pending':pending,'limit_reason':stopped,
                     'requests':logs,'coverage_complete':False,'scope':f'配置关键词，招标/非招标首页及轮转至前{pages}页，验证码区未访问'}}


def job_listing(body,base):
    tree=html.fromstring(body)
    result=[]
    for a in tree.xpath('//a[contains(@href,"/jobs/")]'):
        url=urljoin(base+'/',a.get('href',''))
        # Relative ./jobs from /jobs resolves at the domain root.
        if '/jobs/jobs/' in url:url=url.replace('/jobs/jobs/','/jobs/')
        p=urlsplit(url)
        if p.hostname!='persolchina.career.gllue.com' or p.scheme!='https':continue
        match=re.search(r'-(\d+)$',p.path)
        if match:result.append((match[1],url,node_text(a)))
    if not result and '搜索无结果' not in node_text(tree):raise ValueError('job_list_layout_missing')
    return list({x[0]:x for x in result}.values())


def job_detail(body,url,job_id,queries):
    tree=html.fromstring(body);heads=tree.xpath('//h1')
    if not heads:raise ValueError('job_detail_layout_missing')
    head=heads[0];title=node_text(head)
    scope=head
    while scope is not None and not scope.xpath('.//*[self::h2 or self::h3 or self::h4][normalize-space(.)="职位描述"]'):
        scope=scope.getparent()
    if scope is None:raise ValueError('job_description_missing')
    # Work only on the nearest container containing this job's title and
    # description. Never inherit the global footer or recommendations.
    import copy
    scope=copy.deepcopy(scope)
    for node in scope.xpath('.//footer|.//nav|.//aside|.//*[contains(@class,"recommend") or contains(@id,"recommend")]'):
        if node.getparent() is not None:node.drop_tree()
    for heading in scope.xpath('.//*[self::h2 or self::h3 or self::h4][contains(normalize-space(.),"推荐职位") or contains(normalize-space(.),"相关职位")]'):
        parent=heading.getparent()
        if parent is not scope and parent.getparent() is not None:parent.drop_tree()
    text=node_text(scope);lines=text.splitlines()
    position=next((i for i,line in enumerate(lines) if line==title),-1)
    claim=lines[position-1] if position>0 else ''
    employer=claim if re.fullmatch(r'.{2,100}(?:有限公司|有限责任公司|分公司)',claim) and not re.search(r'某|知名|大型|匿名|保密',claim) else ''
    description=[]
    for h in scope.xpath('.//*[self::h2 or self::h3 or self::h4][normalize-space(.)="职位描述" or normalize-space(.)="职位要求" or normalize-space(.)="公司介绍"]'):
        parent=h.getparent()
        if parent is not None:description.append(node_text(parent))
    job_text='\n'.join(description) if description else text
    japanese=claim if re.search(r'日企|日资|日系',claim) else ''
    if not japanese:
        ownership_claim=re.search(r'(?:全日资企业|日本独资企业|日资独资企业)',job_text)
        if ownership_claim:japanese=ownership_claim.group(0)
    if not re.search(LOGISTICS,text,re.I):return None
    # Keep language hints weaker than a platform's nationality claim.
    language='日语' if '日语' in text or '日本語' in text else ''
    if not japanese and not language:
        from radar.entities import classify_japanese_link
        if classify_japanese_link(employer)['level']=='none':return None
    cities=[]
    location=re.search(r'(?:工作地点|工作地|工作城市|职位地点)\s*[:：]\s*([^\n]{1,60})',text)
    if location:cities=[v.strip() for v in re.split(r'[、,，/]',location.group(1)) if v.strip()]
    if not cities:
        # PERSOL's header grid has industry, function, location, experience,
        # education, in that order; do not scan all page text for city names.
        grid=head.xpath('ancestor::div[.//div[contains(concat(" ",normalize-space(@class)," ")," grid ")]][1]//div[contains(concat(" ",normalize-space(@class)," ")," grid ")][1]')
        if grid:
            fields=grid[0].xpath('./div')
            if len(fields)>=5:
                value=node_text(fields[2]).strip()
                if value:cities=[v.strip() for v in re.split(r'[、,，/]',value) if v.strip()]
    published=None;valid_until=None
    label=re.search(r'(?:发布日期|发布时间|发布于)\s*[:：]?\s*(20\d{2}[-年/.]\d{1,2}[-月/.]\d{1,2}日?)',text)
    if label:published=parse_date(label.group(1))
    valid=re.search(r'(?:有效期至|截止日期|招聘截止)\s*[:：]?\s*(20\d{2}[-年/.]\d{1,2}[-月/.]\d{1,2}日?)',text)
    if valid:valid_until=parse_date(valid.group(1))
    for script in tree.xpath('//script[@type="application/ld+json"]'):
        try:payload=json.loads(script.text or '')
        except (TypeError,ValueError):continue
        entries=payload if isinstance(payload,list) else [payload]
        for obj in entries:
            if not isinstance(obj,dict) or obj.get('@type')!='JobPosting':continue
            if obj.get('title')!=title and str(job_id) not in str(obj.get('url','')):continue
            if not published and obj.get('datePosted'):published=parse_date(str(obj['datePosted'])[:10])
            if not valid_until and obj.get('validThrough'):valid_until=parse_date(str(obj['validThrough'])[:10])
    mentioned=[]
    for match in re.finditer(r'[\u3400-\u9fffA-Za-z0-9]{2,30}(?:株式会社|（株）|\(株\))',job_text):
        sentence=re.split(r'[。\n]',job_text[match.start():],maxsplit=1)[0]
        name=re.sub(r'^(?:(?:主要)?负责|支持|协助|服务于|包括|为|向|给)+','',match.group())
        if name:mentioned.append({'name':name,'role':'关联组织待核实','quote':sentence[:240],'evidence_block':'job_description'})
    body_text='\n'.join(lines[max(0,position-1):])
    body_text=re.sub(r'\n(?:立即投递|分享|立即投递邀请好友)(?:\n|$)','\n',body_text)
    return {'external_id':job_id,'url':url,'title':title+' · 招聘','text':body_text,
            'html':body.decode('utf-8','replace'),'published_date':published,'access_level':'public',
            'metadata':{'schema_version':1,'signal_type':'job','employer_name':employer,
                        'employer_display':claim,'japanese_employer_claim':japanese,'language_hint':language,
                        'position_name':title,'posting_date_status':'source_stated' if published else 'unknown','valid_until':str(valid_until) if valid_until else None,'mentioned_organizations':mentioned,'matched_queries':queries,
                        'job_locations':cities,'discovery_url':REGISTRY['persol_jobs']['url']}}


def collect_jobs(source):
    from .candidate_state import CandidateLedger,Limits,LimitReached,bounded
    base=REGISTRY['persol_jobs']['url'];ledger=CandidateLedger(source.checkpoint,'job_candidates_v1');plan=Limits(source.options or {})
    index=int(ledger.state.get('query_index',0))%len(JOB_QUERIES);combo=JOB_QUERIES[index];pages=bounded(source.options.get('head_pages'),3,1,3)
    cursors=dict(ledger.state.get('page_cursors',{}));deep=max(2,min(pages,int(cursors.get(combo,2)))) if pages>1 else 1
    items=[];logs=[];errors=[];checked=0;excluded=[];stopped='';list_ok=True
    for page in ([1,deep] if deep>1 else [1]):
        keyword=combo.split()[-1];url=base+'?'+urlencode({'gql':urlencode({'keyword':keyword}),'page':page})
        try:
            r,e=plan.read(read,url);e.update(query=combo,effective_keyword=keyword,page=page);logs.append(e)
            rows=job_listing(r['body'],base);checked+=len(rows)
            for job_id,detail_url,summary in rows:
                ledger.add(job_id,detail_url,combo,{'url':e.get('url'),'sha256':e.get('sha256'),'raw_path':e.get('raw_path')},summary=summary)
            if page>1:cursors[combo]=2 if page>=pages or not rows else page+1
        except LimitReached as exc:stopped=str(exc);list_ok=False;break
        except Exception as exc:errors.append({'url':url,'error':type(exc).__name__+': '+str(exc)[:180]});list_ok=False;break
    for candidate in ledger.pending():
        if len(items)>=plan.accepted:stopped=stopped or 'accepted_limit';break
        try:
            d,e=plan.read(read,candidate['url'],detail=True);logs.append(e)
            item=job_detail(d['body'],candidate['url'],candidate['id'],candidate['queries'])
            if item:items.append(item);ledger.done(candidate,'ingested')
            else:
                excluded.append({'id':candidate['id'],'url':candidate['url'],'reason':'当前职位正文未命中物流及日企/日语线索'})
                ledger.done(candidate,'excluded')
        except LimitReached as exc:stopped=str(exc);break
        except Exception as exc:
            if str(exc) in {'time_limit','total_request_limit'}:stopped=str(exc);break
            ledger.failed(candidate,exc);errors.append({'url':candidate['url'],'error':type(exc).__name__+': '+str(exc)[:180]})
    pending=sum(x.get('status') in {'pending','retry'} for x in ledger.entries.values())
    state=ledger.save(query_index=(index+1)%len(JOB_QUERIES) if list_ok else index,page_cursors=cursors)
    return {'items':items,'status':'partial' if errors or stopped or pending else 'success','next_cursor':1,
            'collector_checkpoint':state,'stats':{'error_count':len(errors),'errors':errors,'requests':logs,'list_rows_checked':checked,
            'details_ok':len(items),'details_attempted':plan.detail_calls,'request_calls':plan.calls,'pending':pending,'limit_reason':stopped,
            'excluded':excluded,'coverage_complete':False,'scope':'每次一组关键词，首页及轮转后页；发布日期仅取当前职位明示'}}


def expansion_hits(title,text):
    from radar.seed_dictionary import matches
    from radar.entities import classify_japanese_link
    # A Japanese equipment brand in the project body is not evidence that
    # the owner is Japanese. Restrict weak name matching to the project title
    # and the explicitly labelled owner field, excluding equipment clauses.
    owner=re.search(r'(?:建设单位|项目单位|项目业主|项目方|投资方)\s*[:：为是]\s*([^。；;\n]{1,100})',text)
    owner_text=re.split(r'使用|采用|购买|配备|引进|采购|主要建设',owner.group(1),maxsplit=1)[0] if owner else ''
    hits=matches(title+'\n'+owner_text)
    if not hits:
        # A complete legal name immediately carrying the construction action
        # is a weak project association even if there is no labelled owner.
        for hit in matches(text):
            term=hit.get('term','')
            if hit.get('basis')=='full_name' and re.search(re.escape(term)+r'(?:拟|计划|启动|投资)?(?:新建|扩建|搬迁|迁建|扩产)',text):
                hits.append(hit)
    # Static aliases also work before the first directory export.
    if not hits:
        link=classify_japanese_link(title+' '+owner_text,dynamic=False)
        if link['level']!='none':hits=[{'term':link['matched'],'basis':'known_brand','company_ids':[]}]
    if not re.search(r'新建|新设|扩产|扩建|搬迁|迁建|产线|生产线|危化|危险化学品|危险品|仓储|储存',title+'\n'+text):return []
    if not hits:
        # Require an explicit role and nationality in the same bounded clause;
        # a Japanese machine or a Japanese-language requirement is not enough.
        claim=re.search(r'(?:建设单位|项目单位|项目业主|项目方|投资方)[^。\n]{0,80}(?:日本独资|日本全资|全日资|日资企业)',text)
        if claim:hits=[{'term':claim.group(),'basis':'explicit_japan_ownership','quote':claim.group()}]
    if not hits and re.search(r'危险化学品仓储|危化品仓储|物流中心|仓储物流中心|冷链仓储|配送中心|货运枢纽',title+'\n'+text):
        hits=[{'term':'物流设施变化','basis':'logistics_unknown','quote':'物流设施相关，企业背景待核实'}]
    return hits


def collect_expansion(source):
    from .extra_sources import detail
    from .sources import Listing
    from .candidate_state import CandidateLedger,Limits,LimitReached,bounded
    from radar.models import Lead
    base='https://sthjj.gz.gov.cn';items=[];intake=[];logs=[];errors=[];excluded=[]
    options=source.options or {};plan=Limits(options);ledger=CandidateLedger(source.checkpoint,'expansion_candidates_v1')
    pages=bounded(options.get('head_pages'),3,1,10);stopped='';cursors=dict(ledger.state.get('page_cursors',{}));checked=0
    old_seen=set(source.checkpoint.get('expansion_seen',[]))
    for section in ('hpspgg','hpspqgs'):
        deep=max(2,min(pages,int(cursors.get(section,2)))) if pages>1 else 1
        for page in ([1,deep] if deep>1 else [1]):
            url=f'{base}/hjgl/jsxm/{section}/'+('index.html' if page==1 else f'index_{page}.html')
            try:
                r,e=plan.read(read,url);logs.append(e);tree=html.fromstring(r['body'])
                anchors=tree.xpath(f'//a[contains(@href,"/{section}/content/post_")]')
                if not anchors:raise ValueError('environment_list_layout_missing')
                for a in anchors:
                    title=a.get('title') or node_text(a);p=urlsplit(urljoin(url,a.get('href','')))
                    if p.hostname!='sthjj.gz.gov.cn':continue
                    u=base+p.path
                    identity=hashlib.sha256(u.encode()).hexdigest()[:40];existed=identity in ledger.entries
                    entry=ledger.add(identity,u,section,{'url':e.get('url'),'sha256':e.get('sha256'),'raw_path':e.get('raw_path')},title=title,published=parse_date(node_text(a.getparent().getparent())),listing=url)
                    if not existed and u in old_seen:
                        ledger.done(entry,'ingested' if Lead.objects.filter(source=source,url=u).exists() else 'excluded')
                        entry['legacy_checkpoint']=True
                    checked+=1
                if page>1:
                    more=any(f'index_{page+1}.html' in a.get('href','') for a in tree.xpath('//a'))
                    cursors[section]=page+1 if more and page<pages else 2
            except LimitReached as exc:stopped=str(exc);break
            except Exception as exc:errors.append({'url':url,'error':type(exc).__name__+': '+str(exc)[:180]});break
        if stopped:break
    pending=ledger.pending();pending.sort(key=lambda x:not bool(expansion_hits(x.get('title',''),'')))
    unknown=0;unknown_limit=bounded(options.get('max_unknown_research'),3,0,20)
    for candidate in pending:
        if len(items)>=plan.accepted:stopped=stopped or 'accepted_limit';break
        if unknown_limit and unknown>=unknown_limit:stopped=stopped or 'unknown_research_limit';break
        url=candidate['url'];title=candidate.get('title','')
        try:
            r,e=plan.read(read,url,detail=True);logs.append(e);tree=html.fromstring(r['body'])
            item=detail('gz_environment',tree,Listing(candidate['id'],url,title,published_date=candidate.get('published'),metadata={'discovery_url':candidate.get('listing','')}),r['body'].decode('utf-8','replace'))
            hits=expansion_hits(item['title'],item['text'])
            intake.append({**item,'document':e,'intake_metadata':{'seed_hits':hits}})
            if hits:
                if hits[0].get('basis')=='logistics_unknown':
                    if unknown_limit==0:stopped=stopped or 'unknown_research_limit';break
                    unknown+=1
                item['metadata'].update(japanese_term_matches=hits,signal_type='environmental_project',schema_version=1)
                items.append(item);ledger.done(candidate,'ingested')
            else:
                excluded.append({'url':url,'title':title,'reason':'正文未命中日企关联或高物流相关变化'});ledger.done(candidate,'excluded')
        except LimitReached as exc:stopped=str(exc);break
        except Exception as exc:
            if str(exc) in {'time_limit','total_request_limit'}:stopped=str(exc);break
            ledger.failed(candidate,exc);errors.append({'url':url,'error':type(exc).__name__+': '+str(exc)[:180]})
    remaining=sum(x.get('status') in {'pending','retry'} for x in ledger.entries.values())
    return {'items':items,'intake':intake,'status':'partial' if errors or stopped or remaining else 'success','next_cursor':1,
            'collector_checkpoint':ledger.save(page_cursors=cursors),
            'stats':{'requests':logs,'error_count':len(errors),'errors':errors,'list_rows_checked':checked,
                     'bodies_read':plan.detail_calls,'rows_remaining':remaining,'details_ok':len(items),'limit_reason':stopped,
                     'request_calls':plan.calls,'excluded':excluded,'coverage_complete':False,'scope':f'拟审批/审批首页与轮转至前{pages}页；关联不等于股权或物流采购'}}

REGISTRY['gz_japan_expansion'] = {'key':'gz_japan_expansion','name':'广州环评 · 日企扩产/特种物流信号（试用）',
 'url':'https://sthjj.gz.gov.cn/hjgl/jsxm/hpspgg/index.html','kind':'early_signal',
 'note':'同一广州生态环境官网的两个栏目，含开发区；词典+正文筛选，不另算独立网站。每次最多50正文，定时默认关。'}
