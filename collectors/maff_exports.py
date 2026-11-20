"""Pure parsing of MAFF's public export-plan index and extracted PDF pages.

No requests, credentials, model calls, or database writes occur here.  A plan
is an early research signal; it is never evidence of an active logistics RFQ.
"""
import hashlib
import re
import unicodedata
from urllib.parse import urljoin,urlsplit
from lxml import html

ENTRY='https://www.maff.go.jp/j/shokusan/export/gfp/yusyutsu_keikaku_kohyo.html'
_PDF_PATH=re.compile(r'^/j/shokusan/export/gfp/attach/pdf/yusyutsu_keikaku_kohyo-(\d+)\.pdf$',re.I)
_FISCAL=re.compile(r'[（(]((?:(?:令和|平成)[0-9０-９元]+|20\d{2})年度)[）)]')
_NAME=re.compile(r'(?:※\s*)?申請者名\s*[:：]\s*([^、，\n\r]{2,120})')
_PRODUCT=re.compile(r'品目\s*[:：]\s*([^\n\r]{1,160})')
_IMPORTER_SCOPE=r'(?:中国本土|中国大陸|中国大陆|中国側|香港|台湾)(?:側|の)?'
_IMPORTER=re.compile(r'(?P<label>(?:'+_IMPORTER_SCOPE+r')?(?:輸入業者|輸入者|インポーター|輸入代理店)|'+_IMPORTER_SCOPE+r'代理店)\s*[:：]\s*(?P<entity>[^、，;；。\n]{2,100}(?:有限責任公司|有限责任公司|有限公司|株式会社|公司))')


def _clean(value):
    return re.sub(r'\s+',' ',value or '').strip()


def parse_maff_index(body,entry_url=ENTRY):
    """Group repeated category links by canonical PDF URL, not company name."""
    if len(body)>3_000_000:raise ValueError('maff_index_size_limit')
    tree=html.fromstring(body);records={};index_hash=hashlib.sha256(body).hexdigest()
    for anchor in tree.xpath('//a[@href]'):
        url=urljoin(entry_url,anchor.get('href',''));parts=urlsplit(url)
        if parts.scheme!='https' or parts.hostname!='www.maff.go.jp':continue
        match=_PDF_PATH.fullmatch(parts.path)
        if not match:continue
        url='https://www.maff.go.jp'+parts.path
        title=_clean(anchor.text_content());product_match=re.search(r'【([^】]+)】',title)
        fiscal_match=_FISCAL.search(title)
        if not title:continue
        product=product_match.group(1).strip() if product_match else ''
        end=fiscal_match.start() if fiscal_match else len(title)
        start=title.rfind('：',0,end)
        if start<0:start=title.rfind(':',0,end)
        applicant=title[start+1:end].strip() if start>=0 else ''
        applicant=re.sub(r'\s*\(?PDF\s*:.*$','',applicant,flags=re.I).strip()
        quote={'url':entry_url,'document_hash':index_hash,'quote':title}
        if url in records:
            if quote not in records[url]['list_quotes']:records[url]['list_quotes'].append(quote)
            continue
        records[url]={'id':match.group(1),'url':url,'title':title,'applicant':applicant,'product':product,
                      'source_fiscal_year':fiscal_match.group(1) if fiscal_match else '',
                      'list_url':entry_url,'list_hash':index_hash,'list_quote':title,'list_quotes':[quote]}
    return list(records.values())


def _lines(pages):
    for page in pages:
        number=page.get('page')
        if not isinstance(number,int) or number<1:continue
        text=page.get('text') or ''
        if not isinstance(text,str):continue
        for line in text.splitlines():
            if line.strip():yield number,line.strip()


def _china_market(line):
    """Require local text to connect mainland China to sales/export activity."""
    return any(_china_market_clause(clause) for clause in re.split(r'[。；;]',line))


def _china_market_clause(line):
    value=unicodedata.normalize('NFKC',re.sub(r'\s+','',line))
    value=re.sub(r'中国(?:\((?:香港|台湾|澳門|澳门|マカオ)\)|地方|四国|香港|台湾|澳門|澳门|マカオ|語|产|産)', '',value)
    if '中国' not in value:return False
    if re.search(r'中国.{0,24}?(?:撤退|中止|終了|取りやめ|停止|行わない)',value):return False
    sourcing=re.search(r'中国(?:本土|大陸|大陆|市場|市场)?(?:で|から|より).{0,28}?(?:調達|采购|購入|仕入)',value)
    sales=re.search(r'中国(?:本土|大陸|大陆|市場|市场)?(?:で|へ|への|に|向け|向けに)(?:(?!調達|采购|購入|仕入)[^。；;]){0,28}?(?:輸出|販売|销售|営業|進出|販路|開拓|商談)',value)
    destination=re.search(r'(?:輸出先(?:国)?|輸出国|対象国|目标国|目的地|輸出先地域)[：:]?.{0,80}中国',value)
    if sourcing and not (sales or destination):return False
    return bool(re.search(r'中国(?:市场|市場|向け|への輸出|に輸出|向け輸出|で(?:販売|営業))',value)
                or re.search(r'中国(?:本土|大陸|大陆).{0,20}(?:市場|市场|向け|商談|輸出|販売|販路)',value)
                or re.search(r'(?:輸出先(?:国)?|輸出国|対象国|目标国|目的地|輸出先地域)[：:]?.{0,80}中国',value)
                or re.search(r'中国.{0,12}(?:市場|市场|向け|向けに|への輸出|に輸出)',value))


def parse_maff_plan(pages,entry,document_hash,*,total_pages=None):
    """Return source facts only; applicant plans and all quantities are not orders."""
    url=str(entry.get('url') or '')
    if not re.fullmatch(r'[0-9a-f]{64}',str(document_hash or '')):raise ValueError('document_hash_required')
    rows=list(_lines(pages));evidence=[];relationships=[];metrics=[];markets=[];market_mentions=[]
    page_text={p.get('page'):p.get('text','') for p in pages if isinstance(p,dict)}
    applicant_pdf='';product_pdf=''
    def add(field,value,page,quote):
        if not quote or quote not in page_text.get(page,''):return
        item={'field':field,'value':value,'url':url,'document_hash':document_hash,
              'page':page,'block_id':f'pdf:{document_hash[:16]}:p{page}','quote':quote}
        if item not in evidence:evidence.append(item)
    index_hash=entry.get('list_hash') or ''
    index_quote=entry.get('list_quote') or ''
    if re.fullmatch(r'[0-9a-f]{64}',str(index_hash)) and index_quote:
        for field in ('applicant','product','source_fiscal_year'):
            value=str(entry.get(field) or '')
            if value and value in index_quote:
                evidence.append({'field':field,'value':value,'url':entry.get('list_url') or ENTRY,
                    'document_hash':index_hash,'page':None,'block_id':f'html:{index_hash[:16]}:entry:{entry.get("id","")}',
                    'quote':index_quote,'quote_basis':'normalized_anchor_text'})
    for page,line in rows:
        name=_NAME.search(line)
        if name and not applicant_pdf:
            applicant_pdf=name.group(1).strip();add('applicant',applicant_pdf,page,name.group())
        product=_PRODUCT.search(line)
        if product and not product_pdf:
            product_pdf=product.group(1).strip();add('product',product_pdf,page,product.group())
        if _china_market(line):
            if 'china_mainland' not in markets:markets.append('china_mainland')
            add('market','china_mainland',page,line)
            current=bool(re.search(r'現状|現況|実績|既に|現在|輸出しており',line))
            target=bool(re.search(r'目標|計画|今後|予定|目指|図る|拡大を',line))
            role='mixed_unallocated' if current and target else ('existing' if current else ('target' if target else 'unqualified'))
            mention={'market':'china_mainland','role':role,'page':page,'quote':line}
            if mention not in market_mentions:market_mentions.append(mention)
        compact=re.sub(r'\s+','',line)
        if re.search(r'輸出先|輸出国|市場|営業|輸出|販路|ターゲット',compact):
            for words,code in [(('香港','ホンコン'),'hong_kong'),(('台湾','臺灣'),'taiwan'),(('韓国','韩国'),'korea')]:
                if any(word in compact for word in words) and code not in markets:
                    markets.append(code);add('market',code,page,line)
        for match in _IMPORTER.finditer(line):
            label=match.group('label')
            scope=next((code for prefix,code in [('中国本土','china_mainland'),('中国大陸','china_mainland'),
                       ('中国大陆','china_mainland'),('中国側','china_unspecified'),('香港','hong_kong'),('台湾','taiwan')]
                       if label.startswith(prefix)),'unknown')
            role='import_agent' if label.endswith('輸入代理店') else ('agent' if label.endswith('代理店') else 'importer')
            rel={'name':match.group('entity').strip(),'role':role,'market_scope':scope,
                 'page':page,'quote':match.group().strip()}
            if rel not in relationships:relationships.append(rel);add('relationship',rel['name'],page,rel['quote'])
        if re.match(r'\s*(?:輸出額|輸出量)',line) and re.search(r'\d|[０-９]',line):
            norm=unicodedata.normalize('NFKC',line)
            label='export_amount' if '輸出額' in norm else 'export_quantity'
            unit_match=re.search(r'[（(]([^）)]+)[）)]',line)
            adjacent_unit=re.search(r'\d[\d,.]*\s*(千円|百万円|万円|億円|トン|kg|t)',norm,re.I)
            metric={'kind':label,'values':re.findall(r'\d[\d,]*(?:\.\d+)?',norm),
                    'unit':unit_match.group(1) if unit_match else (adjacent_unit.group(1) if adjacent_unit else ''), 'page':page,'quote':line,
                    'market_scope':'unassigned'}
            if metric not in metrics:metrics.append(metric);add('metric',label,page,line)
    for metric in metrics:
        if len(markets)>1:metric['market_scope']='multiple_markets_unallocated'
    applicant_index=str(entry.get('applicant') or '').strip()
    applicant=applicant_pdf or applicant_index
    fields={'applicant':applicant,'product':product_pdf or str(entry.get('product') or ''),
            'source_fiscal_year':str(entry.get('source_fiscal_year') or ''),
            'markets':markets,'market_mentions':market_mentions,'scale':'unknown','china_entity_name':'','relationships':relationships,
            'metrics':metrics,'published_at':None,'event_date':None,'planned_date':None,
            'date_precision':'fiscal_year' if entry.get('source_fiscal_year') else 'unknown',
            'date_status':'plan_needs_verification','pdf_total_pages':total_pages,
            'page_status':[{'page':p.get('page'),'status':p.get('status','unknown')} for p in pages]}
    processed_pages={p.get('page') for p in pages if isinstance(p.get('page'),int) and p.get('page')>0}
    incomplete=(not rows or any(p.get('status') not in {'text',''} for p in pages)
                or (isinstance(total_pages,int) and total_pages>len(processed_pages)))
    conflict=bool(applicant_pdf and applicant_index and unicodedata.normalize('NFKC',applicant_pdf)!=unicodedata.normalize('NFKC',applicant_index))
    if 'china_mainland' not in markets:reason='text_incomplete_manual_review' if incomplete else 'no_mainland_market_evidence'
    elif not applicant:reason='named_applicant_missing'
    elif conflict:reason='applicant_conflict_manual_review'
    else:reason='matched_mainland_export_plan'
    questions=['计划不等于现有订单或实际出货，需核实当前执行状态。','原文未确认物流外包采购、运输量和我方能力匹配。']
    if len(markets)>1:questions.append('多市场合计数未分配给中国大陆，不得按中国单市场计算。')
    if incomplete:questions.append('部分页文本不可用；图表中的主体和数字需要人工查看。')
    if not relationships:questions.append('未从可读文本明确取得中国进口商身份；不得猜测。')
    return {'accepted':reason=='matched_mainland_export_plan','reason':reason,
            'company_name':applicant,'event_type':'export_plan',
            'china_scope':'mainland' if 'china_mainland' in markets else 'unknown',
            'fields':fields,'evidence':evidence,'questions':questions,
            'note':'公开出口计划；不是物流采购。资料来自日本农林水产省，企业提交内容的第三方权利需分别核实。'}
