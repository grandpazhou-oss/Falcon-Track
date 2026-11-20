"""Osaka's self-reported overseas manufacturing sites: bounded XLSX reader.

This is a company directory, never proof of a new entity or procurement.
The workbook is data only: formulas, macros and remote workbook links are
never evaluated or followed. The caller persists checkpoint only after all
returned records and evidence have been committed successfully.
"""
import hashlib
import io
import json
import os
import posixpath
import re
import stat
import tempfile
import unicodedata
import zipfile
from datetime import date
from pathlib import Path
from urllib.parse import urljoin,urlsplit

from defusedxml import ElementTree as ET
from lxml import html
from django.conf import settings
from radar.public_fetch import fetch

INDEX_URL='https://www.pref.osaka.lg.jp/o110070/keizaikoryu/mono_kaigaikyoten/index.html'
HOST='www.pref.osaka.lg.jp'
MAX_DOWNLOAD_BYTES=12_000_000
MAX_UNCOMPRESSED_BYTES=64_000_000
MAX_MEMBER_BYTES=12_000_000
MAX_MEMBERS=1500
MAIN='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKG='http://schemas.openxmlformats.org/package/2006/relationships'
N={'m':MAIN,'r':REL,'p':PKG}


def discover_workbook(body,base=INDEX_URL):
    tree=html.fromstring(body)
    urls=[]
    for href in tree.xpath('//a[@href]/@href'):
        url=urljoin(base,href);p=urlsplit(url)
        if (p.scheme=='https' and p.hostname==HOST and not p.username and not p.password
                and re.fullmatch(r'/documents/7030/kaigaikyoten_china_[0-9]+\.xlsx',p.path,re.I)
                and not p.query and not p.fragment):urls.append(url)
    urls=list(dict.fromkeys(urls))
    if len(urls)!=1:raise ValueError('osaka_china_workbook_link_missing_or_ambiguous')
    return urls[0]


def _index_update(body):
    tree=html.fromstring(body);nodes=tree.xpath('//*[@id="tmp_update"]')
    quote=' '.join(nodes[0].text_content().split()) if nodes else ''
    match=re.search(r'更新日\s*[:：]\s*(20\d{2})年(\d{1,2})月(\d{1,2})日',quote)
    if not match:return None,quote
    try:return date(*map(int,match.groups())).isoformat(),quote
    except ValueError:return None,quote


def _safe_archive(body,max_uncompressed_bytes):
    if len(body)>MAX_DOWNLOAD_BYTES:raise ValueError('xlsx_download_size_limit')
    try:z=zipfile.ZipFile(io.BytesIO(body))
    except (zipfile.BadZipFile,OSError) as exc:raise ValueError('invalid_xlsx') from exc
    names=set();total=0
    for info in z.infolist():
        name=info.filename;parts=name.split('/')
        if (not name or name.startswith('/') or '\\' in name or any(x in {'.','..'} for x in parts)
                or name in names or info.flag_bits&1 or stat.S_ISLNK(info.external_attr>>16)):
            z.close();raise ValueError('unsafe_xlsx_member')
        names.add(name);total+=info.file_size
        if info.file_size>MAX_MEMBER_BYTES or total>max_uncompressed_bytes or len(names)>MAX_MEMBERS:
            z.close();raise ValueError('xlsx_uncompressed_size_limit')
    if any(re.search(r'vbaProject\.bin$',x,re.I) for x in names):
        z.close();raise ValueError('xlsx_macro_forbidden')
    return z,names,total


def _string(element):
    """Read rich text but exclude Excel ruby/phonetic runs (rPh)."""
    result=[]
    for child in element:
        if child.tag==f'{{{MAIN}}}t':result.append(child.text or '')
        elif child.tag==f'{{{MAIN}}}r':
            result.extend(x.text or '' for x in child if x.tag==f'{{{MAIN}}}t')
    return ''.join(result).strip()


def _cells(sheet,shared):
    values={};formulas=0
    for cell in sheet.findall('.//m:sheetData/m:row/m:c',N):
        address=cell.get('r','')
        if not re.fullmatch(r'[A-Z]{1,3}[1-9][0-9]{0,5}',address):continue
        if cell.find('m:f',N) is not None:
            formulas+=1;continue
        value=cell.find('m:v',N);inline=cell.find('m:is',N);kind=cell.get('t')
        if kind=='s' and value is not None and value.text is not None:
            try:text=shared[int(value.text)]
            except (ValueError,IndexError) as exc:raise ValueError('invalid_xlsx_string_index') from exc
        elif kind=='inlineStr' and inline is not None:text=_string(inline)
        else:text=(value.text or '').strip() if value is not None else ''
        if text:values[address]=text
    return values,formulas


def _region(sheet_name,address):
    text=unicodedata.normalize('NFKC',sheet_name+' '+address).casefold()
    if re.search(r'香港|hong\s*kong',text):return 'hong_kong'
    if re.search(r'澳門|澳门|macao|macau',text):return 'macao'
    if re.search(r'台湾|台灣|taiwan',text):return 'taiwan'
    addr=unicodedata.normalize('NFKC',address).casefold()
    if '中国地方' in sheet_name or re.search(r'日本国|日本国内|japan|岡山県|岡山市|広島県|広島市|鳥取県|島根県|山口県',addr):return 'japan'
    from radar.china_places import MAINLAND
    if MAINLAND.search(text) or re.search(r'中國|china|江蘇|江苏',text,re.I):return 'mainland_china'
    return 'unknown'


def _relation(shape,activity):
    checked=re.findall(r'[■☑✓]\s*(合弁|独資|駐在員事務所|その他\s*[（(][^）)]*[）)]|独占販売代理店|販売代理店|委託生産先)',shape)
    if len(checked)>1:return 'unknown'
    if checked:
        value=checked[0]
        if value=='合弁':return 'joint_venture'
        if value=='独資':return 'owned_subsidiary'
        if value=='駐在員事務所':return 'representative_office'
        if re.search(r'代理店',value):return 'distributor'
        if re.search(r'委託生産',value):return 'contract_manufacturer'
        return 'unknown'
    checked=re.findall(r'[■☑✓]\s*(駐在員\s*[（(]連絡[）)]\s*事務所|独占販売代理店|販売代理店|委託生産先)',activity)
    if len(checked)!=1:return 'unknown'
    if '代理店' in checked[0]:return 'distributor'
    if '委託生産' in checked[0]:return 'contract_manufacturer'
    return 'representative_office'


def _activities(activity):
    roles=[]
    for selected in re.findall(r'[■☑✓]\s*(製造及び販売|独占販売代理店|販売代理店|委託生産先|製造|販売)',activity):
        values={'製造及び販売':['manufacturing','sales'],'製造':['manufacturing'],'販売':['sales'],
                '独占販売代理店':['distributor'],'販売代理店':['distributor'],
                '委託生産先':['contract_manufacturer']}[selected]
        for value in values:
            if value not in roles:roles.append(value)
    return roles


def _website(value):
    for candidate in re.findall(r'https?://[^\s\u3000<>\"\'（）()]+',value or ''):
        candidate=candidate.rstrip('、，。.,;；')
        try:p=urlsplit(candidate)
        except ValueError:continue
        if p.hostname and not p.username and not p.password:return candidate
    return ''


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()


def parse_workbook(body,*,max_uncompressed_bytes=MAX_UNCOMPRESSED_BYTES):
    z,names,total=_safe_archive(body,max_uncompressed_bytes)
    try:
        if not {'xl/workbook.xml','xl/_rels/workbook.xml.rels'}<=names:raise ValueError('invalid_xlsx_workbook')
        root=ET.fromstring(z.read('xl/workbook.xml'));rels=ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        mapping={x.get('Id'):x.get('Target','') for x in rels if x.get('TargetMode')!='External'}
        shared=[]
        if 'xl/sharedStrings.xml' in names:
            shared=[_string(x) for x in ET.fromstring(z.read('xl/sharedStrings.xml')).findall('m:si',N)]
        external=0
        for name in names:
            if name.endswith('.rels'):
                doc=ET.fromstring(z.read(name));external+=sum(x.get('TargetMode')=='External' for x in doc)
        sheets=root.findall('.//m:sheets/m:sheet',N)
        records=[];unresolved=[];formula_count=0;merge_count=0
        for sheet in sheets:
            name=sheet.get('name','');rid=sheet.get(f'{{{REL}}}id');target=mapping.get(rid,'')
            path=posixpath.normpath(posixpath.join('xl',target))
            if not re.fullmatch(r'xl/worksheets/sheet[0-9]+\.xml',path) or path not in names:
                raise ValueError('unsafe_xlsx_worksheet_target')
            document=ET.fromstring(z.read(path));values,formulas=_cells(document,shared)
            formula_count+=formulas
            if '目次' in name:continue
            merge_count+=len(document.findall('.//m:mergeCells/m:mergeCell',N))
            # These labels prove we are looking at the actual Osaka form layout.
            if not ('本社企業名' in values.get('A3','') and '拠点名' in values.get('A10','') and '所在地' in values.get('A11','')):
                unresolved.append({'sheet':name,'reason':'layout_changed_or_missing_labels'});continue
            parent=values.get('B3','').strip();site=values.get('B10','').strip()
            if not parent or not site:
                unresolved.append({'sheet':name,'reason':'parent_or_site_missing'});continue
            address=values.get('B11','').strip();shape=values.get('B15','');activity=values.get('A8','')
            region=_region(name,address);relation=_relation(shape,activity)
            city_match=re.search(r'[（(]([^()（）]{1,30})[）)]',name)
            city=city_match.group(1) if city_match else ''
            source_notes=[{'cell':key,'quote':value} for key,value in values.items() if re.fullmatch(r'G(?:2[6-9]|[3-5][0-9]|60)',key) and re.search(r'20\d{2}',value)]
            cells={'parent_name':'B3','parent_address':'B4','site_name':'B10','site_address':'B11',
                   'site_function':'A8','relation_description':'B15','products':'B18','business_description':'B20','website':'B7'}
            facts={key:{'cell':cell,'quote':values[cell]} for key,cell in cells.items() if values.get(cell)}
            record={'record_key':_digest([unicodedata.normalize('NFKC',parent),unicodedata.normalize('NFKC',site)]),
                    'sheet':name,'parent_name':parent,'parent_address':values.get('B4',''),
                    'site_name':site,'site_address':address,'city':city,'region_class':region,
                    'relation_type':relation,'relation_description':shape,'site_function':activity,'operational_roles':_activities(activity),
                    'products':values.get('B18',''),'business_description':values.get('B20',''),
                    'website':_website(values.get('B7','')),'size':'sme','size_evidence':'official_list_self_report',
                    'event_type':'existing_presence','event_date':None,'date_status':'not_stated',
                    'facts':facts,'source_notes':source_notes}
            record['business_hash']=_digest({k:v for k,v in record.items() if k not in {'facts','record_key','sheet','source_notes'}})
            records.append(record)
        counts={}
        for record in records:counts[record['record_key']]=counts.get(record['record_key'],0)+1
        for record in records:
            if counts[record['record_key']]>1:
                # Keep same-named facilities separate when the workbook lists
                # multiple sites; no legal-entity merge is inferred.
                record['record_key']=_digest([record['record_key'],record['site_address'],record['sheet']])
        return {'records':records,'diagnostics':{'sheet_count':len(sheets),'record_count':len(records),
                'unresolved_sheets':unresolved,'merge_count':merge_count,'ignored_formula_count':formula_count,
                'external_relationships_ignored':external,'uncompressed_bytes':total,
                'macro_present':False,'xlsx_sha256':hashlib.sha256(body).hexdigest()}}
    finally:z.close()


def _save_raw(body,extension):
    digest=hashlib.sha256(body).hexdigest();directory=Path(settings.DATA_DIR)/'raw'/'source_files'
    if directory.is_symlink() or directory.parent.is_symlink():raise ValueError('unsafe_raw_directory')
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/(digest+extension)
    if path.is_symlink():raise ValueError('unsafe_raw_file')
    if path.exists() and (not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest):
        raise ValueError('raw_hash_mismatch')
    if not path.exists():
        fd,name=tempfile.mkstemp(prefix='.osaka-',dir=directory)
        try:
            with os.fdopen(fd,'wb') as output:output.write(body)
            os.replace(name,path)
        finally:
            if os.path.exists(name):os.unlink(name)
    return path.relative_to(settings.DATA_DIR).as_posix()


def collect_directory(checkpoint=None,*,source_url=INDEX_URL):
    """Discover the current file; skip reparsing only after a completed commit."""
    checkpoint=checkpoint or {}
    index=fetch(source_url,max_bytes=3_000_000,timeout=35)
    link=discover_workbook(index['body'],index['url']);index_path=_save_raw(index['body'],'.html')
    workbook=fetch(link,max_bytes=MAX_DOWNLOAD_BYTES,timeout=50)
    digest=hashlib.sha256(workbook['body']).hexdigest();raw_path=_save_raw(workbook['body'],'.xlsx')
    updated_on,updated_quote=_index_update(index['body'])
    evidence={'index_url':index['url'],'index_hash':hashlib.sha256(index['body']).hexdigest(),'index_raw_path':index_path,
              'index_updated_on':updated_on,'index_updated_quote':updated_quote,
              'url':workbook['url'],'file_hash':digest,'raw_path':raw_path,'bytes':len(workbook['body']),
              'robots_redirect_chains':workbook.get('robots_redirect_chains',[]),
              'redirect_chain':workbook.get('redirect_chain',[]),
              'requests':[{'url':index['url'],'sha256':hashlib.sha256(index['body']).hexdigest(),'raw_path':index_path},
                          {'url':workbook['url'],'sha256':digest,'raw_path':raw_path}]}
    if checkpoint.get('file_hash')==digest and checkpoint.get('completed') is True:
        return {'status':'unchanged','records':[],'evidence':evidence,'diagnostics':{'reason':'same_file_hash'}}
    parsed=parse_workbook(workbook['body'])
    return {'status':'parsed','records':parsed['records'],'evidence':evidence,'diagnostics':parsed['diagnostics']}
