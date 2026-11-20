"""Guoyi's public website JSON endpoints, as called by its own announcement UI."""
import json,re
from .sources import Listing,ListingPage,LayoutError,html_tree,node_text,parse_date,_attachments
BASE='https://www.gmgitc.com'
ENTRY=BASE+'/Bid/BidList.aspx?curNav=02'

def list_url(page):
    return BASE+f'/Handlers/Default/Notice_BidInfo_List.ashx?categoryA=&categoryB=&categoryC=&wd=&thisPage={page}&rowsEachPage=15'

def listing(raw,page):
    from .extra_sources import KEYWORDS
    try:d=json.loads(raw)
    except ValueError:raise LayoutError('Invalid public listing JSON')
    if d.get('status')!='success' or not isinstance(d.get('data'),list):raise LayoutError('Public listing API failed')
    items=[]
    for row in d['data']:
        if not re.search(KEYWORDS,row.get('Title','')):continue
        identifier=str(row['SNID'])
        if not identifier.isdigit():raise LayoutError('Invalid public notice ID')
        items.append(Listing(identifier,BASE+'/Handlers/Bid/Notice_BidInfo_Detail.ashx?SNID='+identifier,row['Title'],published_date=parse_date(row.get('ShowDate','')),metadata={'discovery_url':ENTRY,'list_page':page,'public_api':list_url(page)}))
    return ListingPage(items,page+1 if page<int(d['TotalPages']) else None,explicit_empty=not items)

def detail(raw,item):
    try:d=json.loads(raw)
    except ValueError:raise LayoutError('Invalid public detail JSON')
    if d.get('status')!='success' or not isinstance(d.get('data'),dict):raise LayoutError('Public detail API failed')
    row=d['data'];body=row.get('HTMContent') or row.get('TxtContent') or ''
    if len(body)<40:raise LayoutError('Public notice body unavailable')
    tree=html_tree(body);text=node_text(tree)
    url=BASE+'/Bid/BidInfoDetail.aspx?SNID='+item.external_id
    return {'external_id':item.external_id,'url':url,'title':row['Title'],'text':text,'html':raw,'published_date':parse_date(row.get('ShowDate','')),'kind':'procurement','access_level':'public','attachments':_attachments([tree],url),'metadata':{**item.metadata,'format':'public_json','public_detail_api':BASE+'/Handlers/Bid/Notice_BidInfo_Detail.ashx?SNID='+item.external_id}}
