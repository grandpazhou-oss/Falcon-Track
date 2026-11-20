"""Public logistics-provider procurement, retained as subcontract opportunities."""
import copy
import re
from datetime import datetime,timedelta
from urllib.parse import urljoin,urlsplit
from django.utils import timezone
from lxml import html
from .candidate_state import CandidateLedger,Limits,LimitReached,bounded
from .japanese_public import read
from .sources import node_text,parse_date

BASE='https://www.dfwlg.com'
SERVICE=r'短驳|运输|仓储|仓库|仓配|配送|货运|货代|发运|集装箱|冷链|危化|危险品|海运|空运|物流(?:服务|作业|配送|外包|承运|业务外包)'


def listing(body,page):
    tree=html.fromstring(body);rows=[]
    anchors=tree.xpath('//ul[contains(concat(" ",normalize-space(@class)," ")," h-6 ")]/li/a[@href]')
    for anchor in anchors:
        url=urljoin(BASE,anchor.get('href'));parts=urlsplit(url)
        match=re.fullmatch(r'/info/(\d+)',parts.path)
        if parts.scheme!='https' or parts.hostname!='www.dfwlg.com' or not match:continue
        titles=anchor.xpath('.//div[contains(concat(" ",normalize-space(@class)," ")," h-13 ")]')
        if not titles:continue
        title=' '.join(titles[0].xpath('./text()')).strip()
        published=parse_date(node_text(titles[0]))
        descriptions=anchor.xpath('.//div[contains(concat(" ",normalize-space(@class)," ")," h-14 ")]')
        summary=node_text(descriptions[0]) if descriptions else ''
        if title and re.search(SERVICE,title+'\n'+summary,re.I):
            rows.append({'id':match.group(1),'url':url,'title':title,'published':published})
    if not tree.xpath('//ul[contains(concat(" ",normalize-space(@class)," ")," h-6 ")]'):
        raise ValueError('dfwlg_listing_layout_missing')
    next_href=f'/node/330_{page+1}'
    more=bool(tree.xpath('//a[@href=$href]',href=next_href))
    return rows,more,len(anchors)


def detail(body,candidate):
    tree=html.fromstring(body)
    nodes=tree.xpath('//div[contains(concat(" ",normalize-space(@class)," ")," newsnr ")]')
    if not nodes:raise ValueError('dfwlg_detail_layout_missing')
    node=copy.deepcopy(nodes[0]);heads=node.xpath('./h1')
    title=node_text(heads[0]) if heads else candidate['title']
    raw_time=node_text(node.xpath('./p[contains(concat(" ",normalize-space(@class)," ")," source ")]')[0]) if node.xpath('./p[contains(concat(" ",normalize-space(@class)," ")," source ")]') else ''
    for child in node.xpath('./h1|./p[contains(concat(" ",normalize-space(@class)," ")," source ")]'):
        child.drop_tree()
    text=node_text(node)
    if len(text)<80:raise ValueError('dfwlg_detail_too_short')
    timestamp=re.search(r'发布时间\s*[:：]\s*(20\d{2}-\d{2}-\d{2}\s+\d{1,2}:\d{2})',raw_time)
    compact=re.sub(r'\s+','',text)
    period=re.search(r'(?:公示期限|公示期)[:：]?自.{0,25}?起(\d{1,3})小时',compact)
    end=''
    if timestamp and period:
        start=timezone.make_aware(datetime.strptime(timestamp.group(1),'%Y-%m-%d %H:%M'),timezone.get_default_timezone())
        end=(start+timedelta(hours=int(period.group(1)))).isoformat()
    return {'external_id':candidate['id'],'url':candidate['url'],'title':title,'text':text,
            'html':body.decode('utf-8','replace'),'published_date':candidate.get('published'),
            'access_level':'public','kind':'procurement','metadata':{'schema_version':1,'discovery_url':candidate.get('listing',''),
            'source_role':'logistics_provider_subcontract','source_role_note':'物流服务商公开采购；正文提及的客户并不自动是采购方。',
            'notice_period_end':end,'notice_period_quote':period.group() if period else '',
            'notice_time_note':'依据原文发布时间与公示时长计算，仅表示公示期，不等于响应截止。' if end else ''}}


def collect_dfwlg(source):
    options=source.options or {};plan=Limits(options);ledger=CandidateLedger(source.checkpoint,'dfwlg_candidates_v1')
    pages=bounded(options.get('head_pages'),3,1,10);deep=max(2,min(pages,int(ledger.state.get('next_page',2)))) if pages>1 else 1
    items=[];logs=[];errors=[];checked=0;stopped='';next_page=deep
    for page in ([1,deep] if deep>1 else [1]):
        url=BASE+('/node/330' if page==1 else f'/node/330_{page}')
        try:
            response,evidence=plan.read(read,url);logs.append(evidence)
            rows,more,count=listing(response['body'],page);checked+=count
            for row in rows:
                ledger.add(row['id'],row['url'],'物流服务',{'url':url,'sha256':evidence.get('sha256'),'raw_path':evidence.get('raw_path')},
                           title=row['title'],published=row['published'],listing=url)
            if page>1:next_page=page+1 if more and page<pages else 2
        except LimitReached as exc:stopped=str(exc);break
        except Exception as exc:errors.append({'url':url,'error':type(exc).__name__+': '+str(exc)[:160]});break
    for candidate in ledger.pending():
        if len(items)>=plan.accepted:stopped=stopped or 'accepted_limit';break
        try:
            response,evidence=plan.read(read,candidate['url'],detail=True);logs.append(evidence)
            item=detail(response['body'],candidate)
            if re.search(SERVICE,item['title']+'\n'+item['text'],re.I):items.append(item);ledger.done(candidate,'ingested')
            else:ledger.done(candidate,'excluded')
        except LimitReached as exc:stopped=str(exc);break
        except Exception as exc:
            if str(exc) in {'time_limit','total_request_limit'}:stopped=str(exc);break
            ledger.failed(candidate,exc);errors.append({'url':candidate['url'],'error':type(exc).__name__+': '+str(exc)[:160]})
    pending=sum(x.get('status') in {'pending','retry'} for x in ledger.entries.values())
    return {'items':items,'status':'partial' if errors or stopped or pending else 'success','next_cursor':1,
            'collector_checkpoint':ledger.save(next_page=next_page),
            'stats':{'requests':logs,'error_count':len(errors),'errors':errors,'list_rows_checked':checked,
                     'details_attempted':plan.detail_calls,'details_ok':len(items),'pending':pending,
                     'limit_reason':stopped,'request_calls':plan.calls,'coverage_complete':False,
                     'scope':f'首页及轮转前{pages}页，仅物流关键词；采购人和服务客户须分别核实'}}
