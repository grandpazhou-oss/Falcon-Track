"""Parse public event notices without treating a recruitment target as an attendee.

No network, database, or model calls.  Cross-source discrepancies are retained
as claims for review; no corporate alias or deal is inferred.
"""
import hashlib
import re
import unicodedata
from datetime import date
from urllib.parse import urljoin, urlsplit

from lxml import html

ENTRY='https://osaka-sh.com.cn/news/'


def _url(url,host,path):
    parts=urlsplit(url)
    try:
        return (parts.scheme=='https' and parts.hostname==host and parts.port in (None,443)
                and not parts.username and not parts.password and not parts.query
                and not parts.fragment and bool(re.fullmatch(path,parts.path)))
    except ValueError:
        return False


def _text(node):
    for element in node.xpath('.//br|.//p'):
        element.tail='\n'+(element.tail or '')
    return '\n'.join(' '.join(line.split()) for line in node.text_content().splitlines() if line.strip())


def parse_news_index(body):
    if len(body)>3_000_000:raise ValueError('index_size_limit')
    tree=html.fromstring(body);rows=[];seen=set()
    for anchor in tree.xpath('//a[@href]'):
        parts=urlsplit(urljoin(ENTRY,anchor.get('href','')))
        url=parts._replace(fragment='').geturl()
        if not _url(url,'osaka-sh.com.cn',r'/news/[^/]+/') or url in seen:continue
        title=' '.join(anchor.text_content().split())
        if title:rows.append({'url':url,'title':title});seen.add(url)
    return rows


def _times(text):
    match=re.search(r'(?:開催日時|会期)\s*[:：]\s*([^\n]+)',text)
    if not match:return None,None,''
    quote=match.group();normalized=unicodedata.normalize('NFKC',quote)
    parsed=re.search(r'(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日(?:\([^)]*\))?(?:\s*[~〜～-]\s*(?:(\d{1,2})月)?\s*(\d{1,2})日)?',normalized)
    if not parsed:return None,None,quote
    year,month,day,end_month,end_day=parsed.groups()
    try:
        start=date(int(year),int(month),int(day))
        end=date(int(year),int(end_month or month),int(end_day or day))
        if end<start:return None,None,quote
        return start.isoformat(),end.isoformat(),quote
    except ValueError:
        return None,None,quote


def _claims(text,url,digest):
    claims=[]
    pattern=r'(?:上記の|販売会社である)([^。\n]{2,180}?)との商談機会'
    for match in re.finditer(pattern,text):
        for name in match.group(1).split('、'):
            name=name.strip()
            if not re.fullmatch(r'[\w\u3400-\u9fff・（）()＆& .-]{2,90}(?:有限公司|株式会社)',name):continue
            china=''
            for sentence in re.split(r'(?<=。)|\n',text):
                if name not in sentence:continue
                escaped=re.escape(name)
                founding=re.search(r'中国で[^。\n]{0,100}「'+escaped+r'」を設立',sentence)
                franchise=re.search(escaped+r'.{0,12}中国(?:各地|国内).{0,40}フランチャイズ網を広げ',sentence)
                if founding or franchise:
                    china=sentence.strip();break
            claim={'name':name,'role':'announced_counterpart','quote':match.group(),
                   'url':url,'document_hash':digest,'china_evidence':china}
            if claim not in claims:claims.append(claim)
    return claims


def _event(body,url,host,path,selector):
    if not _url(url,host,path):raise ValueError('untrusted_event_url')
    if len(body)>3_000_000:raise ValueError('event_size_limit')
    tree=html.fromstring(body);nodes=tree.xpath(selector)
    if len(nodes)!=1:raise ValueError('event_layout_missing')
    text=_text(nodes[0]);digest=hashlib.sha256(body).hexdigest()
    if not text.strip():raise ValueError('event_text_missing')
    start,end,quote=_times(text)
    return {'url':url,'document_hash':digest,'text':text,'event_start':start,'event_end':end,
            'date_quote':quote,'counterparts':_claims(text,url,digest),'is_procurement':False}


def parse_osaka_event(body,url,*,as_of=None):
    event=_event(body,url,'osaka-sh.com.cn',r'/news/[^/]+/',
                 '//*[@id="single_news"]//*[contains(concat(" ",normalize-space(@class)," ")," main ")]')
    tree=html.fromstring(body)
    title=tree.xpath('//*[@id="single_news"]/*[contains(concat(" ",normalize-space(@class)," ")," title ")]')
    published=tree.xpath('//*[@id="single_news"]/*[contains(concat(" ",normalize-space(@class)," ")," stitle ")]')
    if len(title)!=1 or len(published)!=1:raise ValueError('event_metadata_missing')
    event['title']=' '.join(title[0].text_content().split())
    event['published']=' '.join(published[0].text_content().split())
    heading=event['title']
    if re.search(r'中止',heading):stage='cancelled'
    elif re.search(r'延期',heading):stage='postponed'
    elif re.search(r'開催(?:しました|されました)',heading):stage='reported_held'
    elif re.search(r'募集',heading):stage='recruitment'
    elif re.search(r'開催(?:します|予定)',heading):stage='announced'
    else:stage='unknown'
    today=as_of or date.today()
    if stage in {'cancelled','postponed','reported_held'}:state=stage
    elif event['event_end'] and date.fromisoformat(event['event_end'])<today:state='planned_date_passed_unverified'
    elif not event['event_start']:state='date_unknown'
    else:state='future_date_unverified' if stage=='unknown' else 'upcoming_as_announced'
    event['source_stage']=stage;event['current_state']=state
    event['event_name']=re.sub(r'(?:を開催します|の出展者を募集します).*$', '',re.sub(r'^\d{1,2}/\d{1,2}\s*','',heading)).strip()
    return event


def parse_ageless_event(body,url):
    event=_event(body,url,'www.ageless.gr.jp',r'/seminar/\d+/',
                 '//article//*[contains(concat(" ",normalize-space(@class)," ")," ArticleBody ")]')
    tree=html.fromstring(body);title=tree.xpath('//title')
    event['title']=' '.join(title[0].text_content().split()) if title else ''
    return event


def compare_event_claims(primary,secondary):
    name=primary.get('event_name','')
    if (len(name)<8 or name not in secondary.get('title','') or not primary.get('event_start')
            or primary['event_start']!=secondary.get('event_start')):raise ValueError('different_event')
    first={x['name']:x for x in primary['counterparts']};second={x['name']:x for x in secondary['counterparts']}
    common=sorted(first.keys() & second.keys())
    disputed=[{'name':n,'claims':[source[n] for source in (first,second) if n in source],
               'status':'different_lists_needs_review'} for n in sorted(first.keys() ^ second.keys())]
    qualified=[{'name':n,'role':'announced_counterpart','china_evidence':first[n]['china_evidence'],
                'claims':[first[n],second[n]],'is_procurement':False} for n in common if first[n]['china_evidence']]
    return {'status':'different_lists_needs_review' if disputed else ('matching_lists' if common else 'no_named_list'),
            'common_names':common,'disputed_claims':disputed,'qualified_research_targets':qualified}


def to_research_record(primary,secondary,name,primary_evidence,secondary_evidence):
    """Build, but do not ingest, a record for a corroborated named research target."""
    comparison=compare_event_claims(primary,secondary)
    target=next((x for x in comparison['qualified_research_targets'] if x['name']==name),None)
    if not target:raise ValueError('qualified_target_missing')
    for event,source in ((primary,primary_evidence),(secondary,secondary_evidence)):
        if source.get('sha256')!=event['document_hash'] or not source.get('raw_path'):
            raise ValueError('source_evidence_missing')
    def evidence(event,quote):
        if not quote or quote not in event['text']:raise ValueError('source_quote_missing')
        number=next((i for i,line in enumerate(event['text'].splitlines(),1) if quote in line),None)
        return {'quote':quote,'location':f'article:line:{number}' if number else 'article:main',
                'document_hash':event['document_hash']}
    first,second=target['claims']
    china=evidence(primary,target['china_evidence'])
    person={'name':name,'role':'announced_counterpart','relationship':'unknown',**evidence(primary,first['quote'])}
    dates=[]
    if primary['event_start'] and primary['date_quote']:
        dates.append({'kind':'event_date','value':primary['event_start'],'precision':'day',
                      'status':'planned',**evidence(primary,primary['date_quote'])})
    if primary.get('published'):
        dates.append({'kind':'published_at','value':primary['published'],'precision':'day','status':'stated',
                      'quote':primary['published'],'location':'article:publication','document_hash':primary['document_hash']})
    corroboration={'field':'announced_counterpart','value':name,**evidence(secondary,second['quote'])}
    conflicts=[]
    for item in comparison['disputed_claims']:
        claim=item['claims'][0]
        event=primary if claim['url']==primary['url'] else secondary
        conflicts.append({'field':'conflicting_counterpart','value':item['name'],**evidence(event,claim['quote'])})
    identifier=hashlib.sha256((primary['url']+'\n'+name).encode()).hexdigest()[:24]
    return {'external_id':'osaka-event:'+identifier,'url':primary['url'],'title':primary['title'],
            'text':primary['published']+'\n'+primary['text'],
            'document':{'raw_path':primary_evidence['raw_path'],'sha256':primary['document_hash'],'type':'html','publisher':'大阪府上海事务所'},
            'supporting_documents':[{'raw_path':secondary_evidence['raw_path'],'sha256':secondary['document_hash'],
                                     'type':'html','url':secondary['url'],'text':secondary['text'],'publisher':'ATC エイジレスセンター'}],
            'research':{'event_type':'market_research','workstream':'existing_in_china','china_scope':'mainland',
                        'china_evidence':china,'participants':[person],'dates':dates,'facts':[corroboration,*conflicts],
                        'size':{'value':'unknown'},
                        'questions':['两站内容可能同源，企业信息及活动实际结果需另行核实。',
                                     '其他商谈对象名称在两站不同，不应合并或推断参会。'],
                        'inferences':['可研究与对华供货相关的潜在运输或仓配安排；活动公告不证明采购。'],
                        'next_step':'核实企业大陆业务现状和物流安排；报名对象未公开时不创建企业。'}}
