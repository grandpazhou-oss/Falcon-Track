"""Synthetic companies and documents throughout; no network, database, or model required."""
import copy
import json
from datetime import date

from django.conf import settings
from django.core.management import call_command, CommandError
import pytest

from collectors import target_watch as watch, target_batch as batch
from collectors import target_events as events
from radar import public_fetch

ROOT = 'https://issuer.example/'
DAY = date(2026, 11, 20)
QUOTE = '当社の子会社である上海架空機器有限公司は2027年3月に上海工場を増設する予定です。'
C = {'id':'test', 'name':'架空機器株式会社', 'aliases':[], 'website':ROOT,
     'relations':[{'name':'上海架空機器有限公司','place':'上海','status':'verified','basis':'子会社','url':ROOT+'profile'}],
     'checked_on':'2026-11-20','mapping_url':ROOT+'profile','terms_url':ROOT+'terms','terms_note':'合成测试','access':'reviewed',
     'entries':[{'role':'news','kind':'list','url':ROOT+'news'}]}


def source(s=QUOTE, title='上海工場第二棟の増設について'):
    return ('<main><h1>'+title+'</h1><time itemprop="datePublished" datetime="2026-10-01">発表</time>'
            '<p>架空機器株式会社のニュースリリース。本件について次の通り発表します。</p><p>'+s+'</p></main>').encode()


def parse(s):
    response = {'body':source(s),'content_type':'text/html'}
    title, blocks, pub, partial = events.document(response, {}, None, 'detail')
    return events.candidate(C,title,blocks,pub,DAY)[0]


def test_configuration(tmp_path):
    config = tmp_path/'list.json'
    rows = [copy.deepcopy(C)]
    def validate():
        config.write_text(json.dumps({'companies':rows}))
        return watch.load_targets(config)
    rows[0]['relations'][0]['status'] = 'unknown'
    assert validate()[0]['relations'][0]['status'] == 'unknown'
    rows[0]['relations'][0]['status'] = 'verified'
    rows.append(dict(copy.deepcopy(C), id='other',name='別法人'))
    with pytest.raises(ValueError, match='冲突'):
        validate()
    rows.pop()
    rows[0]['entries'][0]['url'] = 'https://different.example/'
    with pytest.raises(ValueError, match='同源'):
        validate()
    assert len(watch.load_targets(settings.BASE_DIR/'config'/'target_companies.json')) == 5


@pytest.mark.parametrize('s',[
    '当社の子会社である上海架空機器有限公司は名古屋に新工場を新設する予定です。',
    '当社が上海に新工場を建設する予定はございません。',
    '当社は日本の中国地方に2027年3月に新工場を新設する予定です。',
    '当社は広島県の中山地区に倉庫を新設しました。',
    '取引先の別法人は上海に工場を新設しました。',
    '当社はベトナムに新工場を新設し上海へ販売する予定です。',
])
def test_no_false_china_or_subject(s):
    assert parse(s) is None


def test_dates_and_phases():
    row = parse(QUOTE)
    decision = parse('当社は上海に新工場を新設することを決議しました。')
    assert decision['stage']=='计划' and decision['actual_date'] is None
    assert row['planned_date']['value'] == '2027-03' and row['actual_date'] is None
    assert row['publication_date']['value'] == '2026-10-01'
    assert row['stage']=='计划' and row['time_group']=='upcoming' and row['locator'].endswith('/p[2]')
    old = parse('当社は上海に2018年5月に新倉庫を開設しました。')
    assert old['actual_date']['value']=='2018-05' and old['time_group']=='historical'
    assert old['china_entity'] is None and old['planned_date'] is None
    assert parse('当社は上海工場の第3棟について増設を検討しています。')['time_group']=='unknown'
    assert parse('当社は天津に2025年5月に新工場を新設する予定です。')['time_group']=='elapsed_plan'
    assert parse('当社は上海に2026年5月に新工場を着工しました。')['stage']=='已开工'


def test_link_duplicates_versions_and_manual_fields():
    fact = parse(QUOTE)
    items = []
    title = '上海工場第二棟の増設について'
    def put(url, sha, f=fact, t=title):
        return watch.store_event(items,'test',f,t,{'url':ROOT+url,'sha256':sha})
    assert put('one','a')[0]=='new'
    items[0].update(sales_status='暂缓',manual_note='本人已核')
    assert put('one','a')[0]=='duplicate'
    assert put('copy','a')[0]=='duplicate'
    revised = parse(QUOTE.replace('3月','5月'))
    assert put('one','b',revised,'（訂正）'+title)[0]=='duplicate'
    assert len(items)==1 and len(items[0]['evidence'])==3 and items[0]['manual_note']=='本人已核'
    assert items[0]['sales_status']=='暂缓' and items[0]['evidence'][0]['planned_date']['value']=='2027-03'
    assert put('other','c',revised,'別工場の建設について')[0]=='suspected'
    assert items[1]['possible_related']==[items[0]['id']]


def test_rss_scope_and_unsupported_query():
    feed = ('<rss><channel><item><title>蘇州工場を増設</title><link>'+ROOT+'a</link></item>'
            '<item><title>上海工場を増設</title><link>https://another.example/x</link></item></channel></rss>')
    assert watch.discover({'url':ROOT+'rss','body':feed.encode()},'rss')==[{'url':ROOT+'a','title':'蘇州工場を増設','published_date':''}]
    with pytest.raises(CommandError, match='不支持 query'):
        call_command('target_watch',query='上海',online=True)
    with pytest.raises(ValueError,match='access_restricted'):
        watch.discover({'url':ROOT,'body':b'<title>Access denied</title>', 'content_type':'text/html'},'list')


def test_pdf_locator_and_failure(monkeypatch,tmp_path):
    response = {'body':b'%PDF-fixture'}
    doc={'raw_path':'raw/mock.pdf'}
    def pages(values):
        monkeypatch.setattr(events,'pdf_pages',lambda *a,**k:{'pages':values,'status':'partial'})
    pages([{'page':1,'text':''},{'page':2,'text':'架空機器株式会社\n'+QUOTE}])
    title, blocks, pub, partial = events.document(response,doc,tmp_path,'pdf')
    assert partial and events.candidate(C,title,blocks,pub,DAY)[0]['locator']=='PDF p.2'
    pages([{'page':1,'text':''}])
    with pytest.raises(ValueError, match='pdf_no_text_layer'):
        events.document(response,doc,tmp_path,'pdf')


@pytest.mark.parametrize('redirect_from',['/robots.txt','/news'])
def test_cross_origin_stop_before_fetch(monkeypatch,redirect_from):
    public_fetch.clear_robots_cache()
    calls=[]
    monkeypatch.setattr(public_fetch,'validate_url',lambda u:(public_fetch.urlsplit(u),'93.184.216.34'))
    def one(u,*args):
        calls.append(u)
        if u.endswith(redirect_from):
            return {'status':302,'location':'https://outside.example/path'}
        return {'status':404,'content_type':'','body':b''}
    monkeypatch.setattr(public_fetch,'_request_once',one)
    with pytest.raises(ValueError,match='off_origin'):
        public_fetch.fetch(ROOT+'news',same_origin=True)
    assert all(u.startswith(ROOT) for u in calls)
    public_fetch.clear_robots_cache()


def test_manual_batch_replay_and_failures(tmp_path, monkeypatch):
    entries = [C['entries'][0],{'kind':'pdf','role':'ir','url':ROOT+'bad.pdf'}]
    company = dict(C,entries=entries)
    def read(url,**opts):
        assert opts['same_origin']
        if url.endswith('bad.pdf'):
            raise ValueError('http_403')
        body = b'<a href="release">China</a>' if url.endswith('news') else source()
        if url.endswith('news'):
            body = '<a href="release">上海工場の増設</a>'.encode()
        return {'body':body,'content_type':'text/html','url':url}
    monkeypatch.setattr(batch,'fetch',read)
    cfg=tmp_path/'config.json'
    company['entries'][1]['title']='上海工場の増設'
    cfg.write_text(json.dumps({'companies':[company]}),encoding='utf-8')
    companies=watch.load_targets(cfg)
    out = tmp_path/'pilot'
    for i in range(2):
        report=batch.run(companies,out,limit=12,details=2,mode='baseline')
        assert report['stats']['new_events']==1-i and report['stats']['duplicates']==i
        assert report['stats']['failures']==1 and report['reads'][-1]['reason']=='http_403'
    assert len(json.loads((out/'target-events.json').read_text())['events'])==1
    assert len(list((out/'runs').glob('*.json')))==2



def test_scoped_schedule_keeps_table_date_and_title_date_separate():
    c = C['relations'][0]
    fact = parse(QUOTE.replace('2027年3月に',''))
    blocks = [('/article/ol/li[1]','会社名：'+c['name']),
              ('/article/ol/li[2]','所在地：中国上海市'),
              ('/article/ol/li[3]','営業開始日：2027年3月1日（予定）'),
              ('/article/ol[2]/li[1]','会社名：別法人'),
              ('/article/ol[2]/li[2]','所在地：ベトナム'),
              ('/article/ol[2]/li[3]','営業開始日：2026年12月1日')]
    events.schedule(fact, blocks, c, DAY)
    assert fact['planned_date']=={'value':'2027-03-01','milestone':'営業開始日','quote':blocks[2][1],'locator':blocks[2][0]}
    assert fact['actual_date'] is None
    fact = parse(QUOTE.replace('2027年3月に',''))
    events.schedule(fact, blocks[:3]+[(loc.replace('/ol/', '/ol[2]/'),t) for loc,t in blocks[:3]], c, DAY)
    assert fact['planned_date'] is None  # Two overviews with the same subject are ambiguous.


def test_unconfirmed_relationship_and_undated_disclosure():
    c = copy.deepcopy(C)
    c['relations'][0]['status']='conflict'
    row, error = events.candidate(c,'', [('PDF p.3', c['name']+'。'+QUOTE)], None, DAY)
    assert not error and row['relationship_status']=='conflict' and row['china_entity'] is None
    assert row['publication_date'] is None and row['planned_date']['value']=='2027-03'


def test_terms_hold_no_fetch_and_output_guard(tmp_path,monkeypatch,settings):
    monkeypatch.setattr(batch,'fetch',lambda *a,**k: pytest.fail('unexpected network'))
    cfg=tmp_path/'config.json'
    cfg.write_text(json.dumps({'companies':[dict(C,access='hold')]}),encoding='utf-8')
    report=batch.run(watch.load_targets(cfg), tmp_path/'isolated', limit=4, details=1)
    assert report['stats']['manual_watch']==1 and report['stats']['requests']==0
    settings.DATA_DIR=tmp_path/'business'
    with pytest.raises(CommandError,match='业务数据目录以外'):
        call_command('target_watch',online=True,output=settings.DATA_DIR/'raw')
    protected=tmp_path/'existing'
    protected.mkdir()
    (protected/'file').write_text('keep')
    with pytest.raises(ValueError, match='空目录'):
        batch.run([C],protected,limit=1,details=1)
    assert (protected/'file').read_text()=='keep'
