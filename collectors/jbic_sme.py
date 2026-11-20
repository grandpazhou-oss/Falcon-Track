"""JBIC SME research material parser; remote commercial copying is blocked.

This module does not perform network requests or grant source-use rights.
It parses only bytes supplied by the caller, and preserves source-language
quotations so the human can distinguish financing, ownership and operations.
"""
import hashlib
import re
from datetime import date
from urllib.parse import urljoin,urlsplit
from lxml import html
from .sources import node_text,parse_date
from radar.china_places import project_city

ENTRY='https://www.jbic.go.jp/ja/information/press/smes.html'
TERMS='https://www.jbic.go.jp/ja/terms.html'
_PATH=re.compile(r'^/ja/information/press/(press-\d{4}/press_\d+\.html)$')


def _detail_identity(url):
    try:
        parts=urlsplit(url)
        match=_PATH.fullmatch(parts.path)
        if parts.scheme!='https' or parts.hostname!='www.jbic.go.jp' or parts.port not in {None,443} or parts.username or parts.password or not match:
            raise ValueError('非 JBIC 官方公告地址')
        return match.group(1)[:-5]
    except ValueError as exc:
        raise ValueError('非 JBIC 官方公告地址') from exc


def _two_year_cutoff(today):
    try:return today.replace(year=today.year-2)
    except ValueError:return today.replace(year=today.year-2,day=28)


def parse_jbic_index(body,*,as_of=None):
    """Read every dated index row in the bounded 24-month window, not a head slice."""
    today=as_of or date.today();cutoff=_two_year_cutoff(today)
    tree=html.fromstring(body);rows=[];seen=set()
    tables=tree.xpath('//table[contains(concat(" ",normalize-space(@class)," ")," block ")]')
    if not tables:raise ValueError('JBIC 列表结构未匹配')
    for tr in tree.xpath('//table[contains(concat(" ",normalize-space(@class)," ")," block ")]//tr'):
        anchors=tr.xpath('.//a[@href]')
        for a in anchors:
            url=urljoin(ENTRY,a.get('href',''))
            try:identity=_detail_identity(url)
            except ValueError:continue
            title=node_text(a);published=parse_date(node_text(tr))
            if not published or not cutoff<=date.fromisoformat(published)<=today:continue
            if not re.search(r'(?:中国|中華人民共和国)(?:において|で|法人|における)',title):continue
            if re.search(r'香港|台湾|澳門|マカオ|中国地方',title):continue
            if identity not in seen:
                rows.append({'id':identity,'url':url,'title':title,'published_date':published});seen.add(identity)
    return rows


def _fact(field,value,quote,text):
    if not value or not quote or quote not in text:return None
    line=next((i for i,v in enumerate(text.splitlines(),1) if quote in v),None)
    return {'field':field,'value':value,'quote':quote,'location':f'article:line:{line}' if line else 'article'}


def parse_jbic_material(body,url):
    """Parse a user-supplied official article; this is not a license check."""
    identity=_detail_identity(url)
    if not isinstance(body,bytes) or not body or len(body)>3_000_000:raise ValueError('材料格式无效或超过上限')
    tree=html.fromstring(body)
    article=tree.xpath('//main/div[contains(concat(" ",normalize-space(@class)," ")," main ")]/section/article')
    heading=tree.xpath('//main//h1')
    if not article or not heading:raise ValueError('JBIC 公告正文结构未匹配')
    article=article[0];text=node_text(article)
    title=node_text(heading[0]).splitlines()[0].strip()
    if len(text)<100:raise ValueError('公告正文不完整')
    p=re.search(r'(株式会社(?!国際協力銀行)[^\n、。()（）]{1,60})[（(]本社[：:]',text)
    c=re.search(r'(?:中華人民共和国|中国)(?:[（(][^）)]{0,30}[）)])?法人\s*([A-Z][A-Z0-9 ,.\-&()]{4,140}?(?:CO\.,?\s*LTD\.?|COMPANY\s+LIMITED|LTD\.?))(?=[（(]|、|と)',text,re.I)
    if not p or not c:raise ValueError('未能从同一原文确认日本企业及中国法人关系')
    parent=p.group(1).strip();child=c.group(1).strip()
    if re.search(r'銀行|ファイナンス|信金',parent):raise ValueError('主体可能为金融机构，待人工核对')
    alias_match=re.match(r'[（(]以下[「『]([A-Za-z0-9]{2,20})[」』][）)]',text[c.end():])
    child_alias=alias_match.group(1) if alias_match else ''
    address=article.xpath('.//address[contains(concat(" ",normalize-space(@class)," ")," header-address ")]')
    if not address:address=article.xpath('.//address')
    published_quote=node_text(address[0]) if address else ''
    published=parse_date(published_quote)
    if not published:raise ValueError('公告日期未明示')
    facts=[]
    def add(field,value,quote):
        item=_fact(field,value,quote,text)
        if item:facts.append(item)
    add('japanese_parent',parent,p.group(0));add('china_entity',child,c.group(0))
    add('parent_china_relation','官网称为该日本企业的中国法人',text[p.start():c.end()])
    add('published_at',published,published_quote)
    parent_alias=parent.removeprefix('株式会社').strip()
    scale_quote=next((s for s in re.split(r'(?<=。)|\n',text) if parent_alias in s and '中小企業' in s and '中堅・中小企業' not in s),'')
    scale='sme' if scale_quote else 'unknown'
    if scale_quote:add('company_scale','sme',scale_quote.strip())
    product_match=re.search(r'(?:中国|中華人民共和国)の([^。\n]{1,25}?)において実施する([^。\n]{2,80}?)事業に必要な資金',text)
    if product_match:
        location=product_match.group(1);business=product_match.group(2)+'事業'
        add('china_location',location,product_match.group(0));add('business',business,product_match.group(0))
    else:location='';business=''
    finance_quote=product_match.group(0) if product_match else ''
    purpose_match=re.search(r'[^。\n]{0,95}(?:設備増強|設備投資|設備資金|運転資金|現地法人(?:の)?設立)[^。\n]{0,60}',text)
    if purpose_match:
        finance_quote=purpose_match[0].strip()
    finance_purpose=finance_quote
    if finance_purpose:add('finance_purpose',finance_purpose,finance_quote)
    city=project_city(finance_quote) or project_city(location)
    if city:
        add('project_city',city,finance_quote if project_city(finance_quote) else product_match[0])
    amount=re.search(r'融資金額(?:約)?([\d,]+(?:千|百万|百|万)?(?:人民元|円|米ドル))',text)
    if amount:add('financing_amount',amount.group(1),amount.group())
    established=None
    if child_alias:
        established=re.search(r'((?:19|20)\d{2})年(?:には|に)[^。\n]{0,120}?'+re.escape(child_alias)+r'(?:を|の)?設立',text)
    contract=re.search(r'(?:本契約は\s*((?:19|20)\d{2}年\s*\d{1,2}月\s*\d{1,2}日)[^。\n]{0,30}?締結|契約締結日\s*[:：]?\s*((?:19|20)\d{2}年\s*\d{1,2}月\s*\d{1,2}日))',text)
    contract_date=None;contract_status='unknown';contract_quote=''
    if contract:
        contract_date=parse_date(re.sub(r'\s+', '', contract.group(1) or contract.group(2)))
        if contract_date:contract_status='stated';contract_quote=contract.group()
        else:contract=None
    if not contract:
        today=re.search(r'本日[^。\n]{0,300}?貸付契約を締結',text)
        if today:
            contract_date=published;contract_status='derived_relative_today';contract_quote=today.group()
    if established:add('china_established_year',established.group(1),established.group())
    if contract_date:add('contract_date',contract_date,contract_quote)
    dates={'published_at':{'value':published,'precision':'day','status':'stated','quote':published_quote},
           'contract_date':{'value':contract_date,'precision':'day' if contract_date else 'unknown',
                            'status':contract_status,'quote':contract_quote},
           'china_established':{'value':established.group(1) if established else None,'precision':'year' if established else 'unknown',
                               'status':'stated' if established else 'unknown','quote':established.group() if established else ''}}
    signal={'event_type':'finance_support','market_region':'CN-mainland' if product_match else 'unknown','company_scale':scale,
            'entities':[{'name':parent,'role':'japanese_parent','relationship_type':'unknown'},
                        {'name':child,'role':'china_entity','relationship_type':'unknown'}],
            'dates':dates,'facts':facts,'china_location':location,'project_city':city,'finance_purpose':finance_purpose,'business':business,'is_procurement':False,
            'source_evidence':{'publisher':'JBIC','url':url,'document_sha256':hashlib.sha256(body).hexdigest(),
                               'source_type':'official_finance','rights_status':'not_checked_by_parser'},
            'next_step':'人工核实企业当前经营、在华关系与物流安排；融资不等于物流采购。'}
    return {'external_id':identity,'url':url,'title':title,'text':text,'html':body.decode('utf-8','replace'),
            'published_date':published,'kind':'early_signal','access_level':'public',
            'metadata':{'business_signal':signal,'source_policy':'用户提供材料，来源权利及事实待核实'}}


def collect_jbic(source):
    """Fail closed: a boolean cannot replace prior consultation with the source."""
    return {'items':[],'status':'blocked','next_cursor':1,
            'error':'JBIC 条款对商业目的复制要求事先咨询；未经确认，自动保存网页已暂停。',
            'stats':{'error_count':1,'policy_status':'consultation_required','source_terms':TERMS,
                     'requests':[],'coverage_complete':False}}
