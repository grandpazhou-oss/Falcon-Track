"""Synthetic test issuers only. The scale test serves real HTTP on loopback."""
import csv
import hashlib
import http.client
import io
import json
import threading
import time
import zipfile
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from collectors import cli, edinet, target_batch as batch
from collectors.target_watch import discover
from collectors.title_prescore import title_prescore
from radar import public_fetch

DAY=date(2026,11,20)
KEY='synthetic-private-fixture!'
COMPANY='架空機械株式会社'
ENTITY='架空（上海）有限公司'
QUOTE=ENTITY+'は2027年3月に上海工場を増設する予定です。'


def company(i=0, mode='full_archive', kind='list'):
    host=f'https://issuer{i}.example/'
    return {'id':f'c{i}','name':COMPANY,'aliases':[],'relations':[
        {'name':ENTITY,'place':'上海','status':'verified','url':host,'basis':'合成测试'}],
        'edinet_code':'E00001','parent_edinet_code':'E00002','china_entities':[ENTITY],
        'entries':[{'url':edinet.BASE+'.json' if kind=='edinet' else host+'news',
                    'role':'news','kind':kind,'entry_id':'news','compliance_mode':mode}]}


def response(url,body):
    return {'url':url,'content_type':'text/html','body':body.encode('utf-8'),'status':200}


@pytest.mark.parametrize('title,band',[
 ('芝浦機械 中国安徽省に新工場を建設','A'),('西川ゴム 湖北省孝感市で生産開始','A'),
 ('タイ製造現地法人の閉鎖に関するお知らせ','skip'),('中国支社人事異動','skip'),
 ('海外生産体制強化に向けた新工場建設','A'),('海外展開','skip'),('設備投資','B'),
 ('中国営業所・中山町の人事異動','skip'),('タイから上海に生産移管','A')])
def test_prescore(title,band):
    score,_=title_prescore(title,[])
    assert ('A' if score>=45 else 'B' if score>=25 else 'skip')==band


def test_pointer_and_manual_do_not_fetch_details_or_archive(tmp_path,monkeypatch):
    calls=[]
    def read(url,**kw):
        assert kw['no_redirect']
        calls.append(url)
        return response(url,'<main><a href="a">上海工場増設</a><a href="b">タイ工場閉鎖</a></main>')
    monkeypatch.setattr(batch,'fetch',read)
    report=batch.run([company(mode='metadata_pointer'),company(1,'manual_watch')],tmp_path,limit=10)
    assert calls==['https://issuer0.example/news'] and not (tmp_path/'raw').exists()
    assert len(report['pointers'])==1 and set(report['pointers'][0])==set(
        'company_id title published_date source_url prescore matched_terms compliance_mode archive_status'.split())
    assert report['pointers'][0]['archive_status']=='pointer_only'
    skipped=json.loads((tmp_path/'prescore_skipped.jsonl').read_text(encoding='utf-8'))
    assert skipped['score']<25 and skipped['url'].endswith('/b')


def filing(kind='180',code='E00002'):
    return dict(docID='S100AAAA',docTypeCode=kind,edinetCode=code,docDescription='法定開示',
                submitDateTime='2026-11-20 10:00',csvFlag='1',pdfFlag='1')


def csv_zip(rows):
    text=io.StringIO()
    wr=csv.writer(text,delimiter='\t')
    wr.writerow(['要素ID','項目名','値'])
    wr.writerows(rows)
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:
        z.writestr('XBRL_TO_CSV/jpcrp-test.csv',text.getvalue().encode('utf-16'))
    return b.getvalue()


@pytest.mark.parametrize('kind',['180','120','160'])
def test_shared_filing_list_and_evidence(tmp_path,monkeypatch,kind):
    metadata=[filing(kind),filing(kind,'E99999'),filing('999')]
    payload=csv_zip([['one','関係会社の状況',QUOTE],['two','その他','別の会社は広州工場を移転しました。']])
    calls=[]
    def read(url,key,limit):
        assert key==KEY and KEY not in url
        calls.append(url)
        if 'documents.json?' in url:
            return json.dumps({'metadata':{'status':'200'},'results':metadata}).encode()
        return payload
    monkeypatch.setattr(edinet,'request',read)
    monkeypatch.setattr(edinet,'api_key',lambda:KEY)
    c=company(mode='statutory_api',kind='edinet')
    unrelated=company(1,mode='statutory_api',kind='edinet')
    unrelated.update(name='別会社',china_entities=[],relations=[],edinet_code='E99998',parent_edinet_code=None)
    result=batch.run([c,unrelated],tmp_path,limit=16,edinet_days=1)
    assert len(calls)==2 and len(result['reads'])==1
    assert result['stats']['new_events']==1
    ev=json.loads((tmp_path/'target-events.json').read_text(encoding='utf-8'))['events'][0]['evidence'][0]
    assert ev['place']=='上海' and 'row 2' in ev['locator'] and ev['publication_date']['value']=='2026-11-20'
    assert ev['planned_date']['value']=='2027-03' and ev['sha256']==hashlib.sha256(payload).hexdigest()
    assert all(KEY.encode() not in p.read_bytes() for p in tmp_path.rglob('*') if p.is_file())
    assert (len(edinet.csv_blocks(payload,kind))==2)==(kind=='180')


def test_parent_unrelated_missing_key_and_empty_list(tmp_path,monkeypatch):
    c=company(mode='statutory_api',kind='edinet')
    monkeypatch.setattr(edinet,'api_key',lambda:'')
    assert batch.run([c],tmp_path,limit=4)['stats']['skipped_missing_api_key']==1
    monkeypatch.setattr(edinet,'request',lambda *a:b'{"metadata":{"status":"200"},"results":[]}')
    assert edinet.filings(DAY,{'E00001'},KEY)==[]
    monkeypatch.setattr(edinet,'api_key',lambda:KEY)
    monkeypatch.setattr(edinet,'filings',lambda *a:[filing()])
    body=csv_zip([['x','臨時報告書','親会社の別の子会社は広州に新工場を建設しました。']])
    monkeypatch.setattr(edinet,'request',lambda *a:body)
    assert batch.run([c],tmp_path,limit=4,edinet_days=1)['stats']['candidate_events']==0


@pytest.mark.parametrize('status',[401,429,302])
def test_secret_stays_on_wire_only(tmp_path,monkeypatch,caplog,status):
    class FakeTLS:
        def __init__(self,*a,**kw): pass
        def request(self,method,path,headers):
            assert 'Subscription-Key=synthetic-private-fixture%21' in path
        def getresponse(self):
            return type('Response',(),{'status':status})()
        def close(self): pass
    monkeypatch.setattr(edinet.http.client,'HTTPSConnection',FakeTLS)
    monkeypatch.setattr(public_fetch,'_robots_allowed',lambda *a,**k:True)
    monkeypatch.setattr(public_fetch,'validate_url',lambda u:(urlsplit(u),'93.184.216.34'))
    monkeypatch.setattr(edinet,'api_key',lambda:KEY)
    with pytest.raises(ValueError,match=f'edinet_http_{status}') as caught:
        edinet.filings(DAY,{'E00001'},KEY)
    report=batch.run([company(mode='statutory_api',kind='edinet')],tmp_path,limit=5,edinet_days=1)
    for text in [str(caught.value),json.dumps(report),caplog.text]:
        assert KEY not in text and 'Subscription-Key' not in text


def test_edinet_pdf_fallback_keeps_true_page(tmp_path,monkeypatch):
    f=dict(filing('120'),csvFlag='0')
    calls=[]
    monkeypatch.setattr(edinet,'request',lambda url,*a:calls.append(url) or b'%PDF-synthetic')
    monkeypatch.setattr(edinet,'pdf_pages',lambda *a,**kw:{'pages':[{'page':7,'text':'設備の新設、除却等の計画\n'+QUOTE}]})
    blocks,_,doc=edinet.report_document(f,KEY,tmp_path)
    assert calls[0].endswith('type=2') and ('PDF p.7',QUOTE) in blocks and doc['raw_path'].endswith('.pdf')


def test_utf8_retry_and_binary_hash(tmp_path,monkeypatch):
    path=tmp_path/'水位線.json'
    replace=cli.os.replace
    attempts=[]
    def retry(a,b):
        attempts.append(a)
        if len(attempts)<3: raise PermissionError('simulated sharing violation')
        replace(a,b)
    monkeypatch.setattr(cli.os,'replace',retry)
    cli.atomic_json(path,{'企业':'工場'})
    assert json.loads(path.read_text(encoding='utf-8'))=={'企业':'工場'} and len(attempts)==3
    monkeypatch.setattr(cli.os,'replace',lambda *a:(_ for _ in ()).throw(PermissionError()))
    with pytest.raises(PermissionError):
        cli.atomic_json(path,{})
    assert list(tmp_path.iterdir())==[path]
    body=b'raw\r\nbytes\n'
    doc=batch.archive_original({'body':body,'url':'https://issuer.example/'},tmp_path)
    assert (tmp_path/doc['raw_path']).read_bytes()==body and doc['sha256']==hashlib.sha256(body).hexdigest()


def test_global_tier_order_resume_and_scope_downgrade(tmp_path,monkeypatch):
    cs=[company(),company(1)]
    calls=[]
    def read(url,**kw):
        calls.append(url)
        if url.endswith('news'):
            title='設備投資' if 'issuer0.' in url else '上海に新工場'
            return dict(response(url,'<a href="detail">'+title+'</a>'),etag='"one"')
        return response(url,'<article><h1>新工場</h1><p>'+QUOTE+'</p><p>'+COMPANY*8+'</p></article>')
    def counted(url,**kw):
        public_fetch._consume_budget()
        return read(url,**kw)
    monkeypatch.setattr(batch,'fetch',counted)
    report=batch.run(cs,tmp_path,limit=3)
    assert calls[-1]=='https://issuer1.example/detail' and report['stats']['deferred']==1
    calls.clear()
    batch.run(cs,tmp_path,limit=3,resume=True)
    assert calls==['https://issuer0.example/detail']
    cs[0]['entries'][0]['compliance_mode']='manual_watch'
    calls.clear()
    batch.run(cs[:1],tmp_path,limit=3,mode='baseline',resume=True)
    assert not calls


def test_loopback_30_companies_300_titles(tmp_path,monkeypatch):
    scenario={'epoch':0,'failed':None,'timeout':None}
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            _,issuer,kind=self.path.split('/')
            calls.append((issuer,kind,self.headers.get('If-None-Match'),self.headers.get('If-Modified-Since')))
            if kind=='robots.txt':
                self.send_response(404); self.end_headers(); return
            if kind=='news' and issuer==scenario['failed']:
                self.send_response(403); self.end_headers(); return
            if kind=='news' and issuer==scenario['timeout']:
                time.sleep(.25); return
            etag=f'"{scenario["epoch"]}"'
            if kind=='news' and self.headers.get('If-None-Match')==etag:
                self.send_response(304); self.end_headers(); return
            if kind=='news':
                titles=['上海新工場建設','在庫倉庫']+['取締役の人事異動']*8
                body=''.join(f'<a href="d{i}_{scenario["epoch"]}">{title}</a>' for i,title in enumerate(titles))
            else:
                body='<article><h1>上海工場の増設</h1><p>'+QUOTE+'</p><p>'+COMPANY*8+'</p></article>'
            self.send_response(200)
            self.send_header('ETag',etag); self.send_header('Last-Modified','Fri, 20 Nov 2026 00:00:00 GMT')
            self.end_headers(); self.wfile.write(body.encode())
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    def exchange(url,max_bytes,deadline,**kw):
        public_fetch._consume_budget()
        p=urlsplit(url)
        conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=.12 if p.hostname==scenario['timeout'] else 2)
        try:
            conn.request('GET','/'+p.hostname+p.path,headers=kw.get('conditions') or {})
            r=conn.getresponse()
            return {'status':r.status,'body':r.read(),'content_type':'text/html','etag':r.getheader('etag') or '',
                    'last_modified':r.getheader('last-modified') or ''}
        except TimeoutError:
            raise public_fetch.FetchError('timeout') from None
        finally:
            conn.close()
    monkeypatch.setattr(public_fetch,'_request_once',exchange)
    monkeypatch.setattr(public_fetch,'validate_url',lambda u:(urlsplit(u),'127.0.0.1'))
    public_fetch.clear_robots_cache()
    try:
        cs=[company(i) for i in range(30)]
        one=batch.run(cs,tmp_path,limit=180)
        assert sum(row['titles'] for row in one['entries'])==300
        details=sum(k.startswith('d') for _,k,_,_ in calls)
        assert details==60 and 1-details/300>=.75
        assert one['stats']['new_events']==30 and one['stats']['skipped_titles']==240
        calls.clear()
        two=batch.run(cs,tmp_path,limit=180)
        assert two['stats']['not_modified']==30 and two['stats']['new_events']==0
        assert len(calls)==30 and all(etag and lm for _,_,etag,lm in calls)
        scenario.update(failed='issuer0.example',timeout='issuer1.example')
        mixed=batch.run(cs,tmp_path,limit=180)
        assert mixed['stats']['failures']==2 and mixed['stats']['not_modified']==28
        for fault in ['failed','timeout']:
            scenario.update(failed=None,timeout=None)
            scenario[fault]='issuer0.example'
            outcome=batch.run(cs,tmp_path,limit=180)
            assert outcome['stats']['failures']==1 and outcome['stats']['not_modified']==29
            assert outcome['entries'][0]['reason']==('http_403' if fault=='failed' else 'timeout')
    finally:
        public_fetch.clear_robots_cache()
        server.shutdown(); server.server_close(); thread.join()


def test_pointer_index_and_robots_redirect_never_fetch_target(monkeypatch):
    monkeypatch.setattr(public_fetch,'validate_url',lambda u:(urlsplit(u),'93.184.216.34'))
    for redirect_robots in (True,False):
        public_fetch.clear_robots_cache()
        calls=[]
        def once(url,*a):
            calls.append(url)
            if redirect_robots or not url.endswith('/robots.txt'):
                return {'status':302,'location':'/disclosure.pdf'}
            return {'status':404,'content_type':'text/plain','body':b''}
        monkeypatch.setattr(public_fetch,'_request_once',once)
        with pytest.raises(ValueError,match='redirect_not_followed'):
            public_fetch.fetch('https://issuer.example/news',same_origin=True,no_redirect=True)
        assert not any('disclosure.pdf' in u for u in calls)
    public_fetch.clear_robots_cache()


def test_index_selectors_and_watermark_dates(tmp_path,monkeypatch):
    entry={'links_xpath':'//dl/dd/a'}
    def index(title,day):
        return '<a href="menu">上海倉庫</a><dl><dt><time datetime="'+day+'"></time></dt><dd><a href="'+title+'">'+title+'</a></dd></dl>'
    c=company(mode='metadata_pointer')
    c['entries'][0].update(entry)
    url=c['entries'][0]['url']
    texts=[index('上海工場','2026-10-20'), index('広州工場','2026-10-01')]
    assert discover(response(url,texts[0]),'list',entry)[0]['published_date']=='2026-10-20'
    monkeypatch.setattr(batch,'fetch',lambda *a,**kw:response(url,texts.pop(0)))
    assert len(batch.run([c],tmp_path,limit=3)['pointers'])==1
    second=batch.run([c],tmp_path,limit=3)
    assert second['pointers']==[] and second['stats']['seen_or_old']==1


def test_ledger_redacts_sensitive_query():
    log=[]
    with public_fetch.request_budget(2,10,journal=log):
        public_fetch._consume_budget('https://api.edinet-fsa.go.jp/api/v2/documents.json?type=2&subscription-key='+KEY)
    assert KEY not in json.dumps(log) and 'REDACTED' in log[0]['url']


def test_blocked_filing_list_stops_queued_api_reads(tmp_path,monkeypatch):
    monkeypatch.setattr(edinet,'api_key',lambda:KEY)
    replies=iter([[filing()],ValueError('edinet_http_429')])
    def listing(*args):
        result=next(replies)
        if isinstance(result,Exception):
            raise result
        return result
    monkeypatch.setattr(edinet,'filings',listing)
    monkeypatch.setattr(edinet,'report_document',lambda *a:pytest.fail('request after rate limit'))
    report=batch.run([company(mode='statutory_api',kind='edinet')],tmp_path,limit=8,edinet_days=2)
    assert report['entries'][0]['status']=='stopped' and report['stats']['pending']==1
    assert report['reads']==[]


@pytest.mark.parametrize('envelope',[{'StatusCode':401},{'metadata':{'status':'429'}}])
def test_edinet_200_error_envelope_never_echoes_message(envelope):
    body=json.dumps(dict(envelope,message=KEY)).encode()
    with pytest.raises(ValueError,match='edinet_http_') as caught:
        edinet.checked_body(body)
    assert KEY not in str(caught.value)
