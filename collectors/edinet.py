"""Manual EDINET v2 reads. The credential only enters the TLS request target."""
import csv
import hashlib
import http.client
import io
import json
import os
import re
import socket
import time
import zipfile
from datetime import date
from urllib.parse import urlencode, urlsplit

from filelock import Timeout
from lxml import html, etree

from radar import public_fetch
from collectors.research_common import pdf_pages
from collectors.target_events import compact

BASE = 'https://api.edinet-fsa.go.jp/api/v2/documents'
TYPES = {'120','160','180'}
SECTION = re.compile(r'設備の新設[、，・,]?除却等の計画|関係会社の状況|PlannedAdditions|MajorSubsidiariesAndAssociates|OverviewOfAffiliatedEntities', re.I)


def api_key():
    key = os.environ.get('EDINET_API_KEY')
    if key:
        return key
    from radar.ai_config import SERVICE, secure_keyring
    from keyring.errors import KeyringError
    try:
        return secure_keyring().get_password(SERVICE,'edinet-api-v2') or ''
    except (KeyringError,ValueError,OSError):
        return ''


def request(url, key, limit):
    """Reject redirects. Never hand a credential-bearing URL to HTTP loggers."""
    parsed = urlsplit(url)
    if (parsed[:2] != ('https','api.edinet-fsa.go.jp') or
            not re.fullmatch(r'/api/v2/documents(?:\.json|/S[A-Z0-9]{7})',parsed.path)):
        raise ValueError('invalid_edinet_endpoint')
    deadline = public_fetch.bounded_deadline(20)
    public_fetch.check_budget()
    if not public_fetch._robots_allowed(url, deadline, same_origin=True):
        raise ValueError('blocked_by_robots')
    _, ip = public_fetch.validate_url(url)
    try:
        with public_fetch.public_request(timeout=max(.01,deadline-time.monotonic())):
            attempt=public_fetch._consume_budget(url)
            conn = http.client.HTTPSConnection(parsed.hostname,timeout=max(.01,deadline-time.monotonic()))
            # Pin the vetted IP while keeping the original TLS hostname and certificate checks.
            conn._create_connection = lambda addr,*args: socket.create_connection((ip,443),*args)
            try:
                conn.request('GET',parsed.path+'?'+parsed.query+'&'+urlencode({'Subscription-Key':key}),
                             headers={'User-Agent':'LogisticsLeadRadar/0.2 (public evidence research)'})
                response = conn.getresponse()
                if attempt is not None:
                    attempt['status']=response.status
                if response.status!=200:
                    raise ValueError(f'edinet_http_{response.status}')
                body=bytearray()
                while True:
                    remaining=deadline-time.monotonic()
                    if remaining<=0:
                        raise ValueError('edinet_timeout')
                    if conn.sock:
                        conn.sock.settimeout(remaining)
                    part=response.read1(min(65536,limit+1-len(body)))
                    body.extend(part)
                    if len(body)>limit:
                        raise ValueError('edinet_size_limit')
                    if not part:
                        return checked_body(bytes(body))
            finally:
                conn.close()
    except (http.client.HTTPException,OSError,Timeout):
        raise ValueError('edinet_transport_error') from None


def checked_body(body):
    # EDINET also returns HTTP 200 with an error status in a JSON envelope.
    if body.lstrip().startswith(b'{'):
        obj=json.loads(body)
        if isinstance(obj,dict):
            meta=obj.get('metadata')
            status=str(meta.get('status','') if isinstance(meta,dict) else obj.get('StatusCode',obj.get('statusCode','')))
            if status in {'400','401','403','404','429','500','503'}:
                raise ValueError('edinet_http_'+status)
    return body


def filings(day, codes, key):
    url=BASE+'.json?'+urlencode({'date':day.isoformat(),'type':2})
    payload=json.loads(request(url,key,12_000_000))
    status=str(payload.get('metadata',{}).get('status','')) if isinstance(payload,dict) and isinstance(payload.get('metadata'),dict) else ''
    if status in {'401','403','429'}:
        raise ValueError('edinet_http_'+status)
    if status!='200' or not isinstance(payload.get('results'),list):
        raise ValueError('edinet_list_invalid')
    return [{k:str(row.get(k) or '') for k in ('docID','docTypeCode','edinetCode','docDescription','submitDateTime','csvFlag','pdfFlag')}
            for row in payload['results'] if isinstance(row,dict) and row.get('edinetCode') in codes and row.get('docTypeCode') in TYPES
            and re.fullmatch('S[A-Z0-9]{7}',row.get('docID') or '')
            and row.get('withdrawalStatus') not in ('1','2') and row.get('disclosureStatus') not in ('1','2')]


def csv_blocks(body, kind):
    blocks=[]
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            members=[i for i in z.infolist() if i.filename.lower().endswith('.csv') and
                     re.search(r'(?:^|/)(?:jpcrp|jpigp|jpsps)',i.filename,re.I)]
            if not members or len(members)>60 or sum(i.file_size for i in members)>32_000_000:
                raise ValueError('edinet_csv_missing_or_oversize')
            for item in members:
                raw=z.read(item)
                encoding='utf-8-sig'
                if raw.startswith((b'\xff\xfe',b'\xfe\xff')):
                    encoding='utf-16'
                elif b'\t\x00' in raw[:512]:
                    encoding='utf-16-le'
                stream=io.StringIO(raw.decode(encoding))
                # EDINET text blocks can exceed the stdlib CSV default of 128 KiB.
                old_limit=csv.field_size_limit(32_000_000)
                try:
                    rows=csv.DictReader(stream,delimiter='\t')
                    if not {'項目名','値'} <= set(rows.fieldnames or []):
                        raise ValueError('edinet_csv_schema')
                    for n,row in enumerate(rows,2):
                        if kind!='180' and not SECTION.search(compact((row['項目名'] or '')+(row.get('要素ID') or ''))):
                            continue
                        value=row.get('値') or ''
                        if '<' in value:
                            tree=html.fragment_fromstring(value,create_parent='div')
                            cells=tree.xpath('.//tr|.//p[not(ancestor::tr)]')
                            if cells:
                                for i,cell in enumerate(cells,1):
                                    blocks.append((f'{item.filename} row {n} block {i}', ' '.join(cell.itertext()).strip()))
                                continue
                            value=' '.join(tree.itertext())
                        if value.strip():
                            blocks.append((f'{item.filename} row {n}',value.strip()))
                finally:
                    csv.field_size_limit(old_limit)
    except (zipfile.BadZipFile,UnicodeError,csv.Error,RuntimeError,etree.ParserError):
        raise ValueError('edinet_csv_unreadable') from None
    return blocks


def report_document(row,key,output):
    docid=row['docID']
    kind=row['docTypeCode']
    csv_available=row['csvFlag']=='1'
    if not csv_available and row['pdfFlag']!='1':
        raise ValueError('edinet_no_csv_or_pdf')
    url=BASE+'/'+docid+'?type='+('5' if csv_available else '2')
    try:
        body=request(url,key,12_000_000)
    except ValueError as exc:
        if not (csv_available and str(exc)=='edinet_http_404' and row['pdfFlag']=='1'):
            raise
        csv_available=False
        url=BASE+'/'+docid+'?type=2'
        body=request(url,key,12_000_000)
    ext='zip' if csv_available else 'pdf'
    if not body.startswith(b'PK\x03\x04' if csv_available else b'%PDF-'):
        raise ValueError('edinet_unexpected_document')
    sha=hashlib.sha256(body).hexdigest()
    rel=f'raw/research/{sha}.{ext}'
    path=output/rel
    path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=sha:
        temp=path.with_suffix('.part')
        temp.write_bytes(body)
        temp.replace(path)
    if csv_available:
        blocks=csv_blocks(body,kind)
    else:
        pages=pdf_pages(rel,directory=output)['pages']
        blocks=[]
        active = kind=='180'
        for pg in pages:
            for line in pg['text'].splitlines():
                if kind!='180':
                    if SECTION.search(compact(line)):
                        active=True
                    elif re.match(r'^\s*(?:第[0-9０-９]+|[0-9０-９]+\s*【)',line):
                        active=False
                if active:
                    blocks.append((f"PDF p.{pg['page']}",line))
    stamp=row['submitDateTime'][:10]
    try:
        pub={'value':date.fromisoformat(stamp).isoformat(),'quote':row['submitDateTime'],'locator':'EDINET submitDateTime'}
    except ValueError:
        pub=None
    doc={'url':url,'raw_path':rel,'sha256':sha,'doc_id':docid,'doc_type':kind,'edinet_code':row['edinetCode']}
    return blocks,pub,doc
