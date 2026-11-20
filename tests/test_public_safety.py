"""Offline regressions for shared public-site rate/robots enforcement."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import httpx
import pytest
from scrapy import Request
from scrapy.exceptions import IgnoreRequest
from scrapy.http import Response
from radar.public_fetch import clear_robots_cache, fetch, robots_allowed, FetchError
from radar.request_rate import RequestLease
from collectors.spider import StrictRobotsMiddleware, SharedRequestRateMiddleware, PublicSourceSpider


@pytest.fixture(autouse=True)
def isolated_rate(monkeypatch,tmp_path):
    monkeypatch.setenv('LEAD_PUBLIC_RATE_DIR',str(tmp_path/'shared-rate'))
    clear_robots_cache()
    monkeypatch.setattr('radar.public_fetch.socket.getaddrinfo',lambda *a,**k:[(2,1,6,'',('93.184.216.34',443))])
    yield
    clear_robots_cache()


def mock_http(monkeypatch,handler):
    original=httpx.Client
    monkeypatch.setattr('radar.public_fetch.httpx.Client',lambda **kw:original(**kw,transport=httpx.MockTransport(handler)))


def test_cross_process_rate_guard_ignores_business_data_directory(tmp_path):
    root=Path(__file__).resolve().parents[1]
    script=('import time,json; from radar.request_rate import public_request; '
            'lease=public_request(); lease.__enter__(); start=time.monotonic(); time.sleep(.05); '
            'lease.__exit__(None,None,None); print(json.dumps({"start":start,"end":time.monotonic()}))')
    processes=[]
    for i in range(2):
        env={**os.environ,'LEAD_DATA_DIR':str(tmp_path/f'data{i}'),'LEAD_PUBLIC_RATE_DIR':str(tmp_path/'shared')}
        processes.append(subprocess.Popen([sys.executable,'-c',script],cwd=root,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
    values=[]
    for p in processes:
        stdout,stderr=p.communicate(timeout=15);assert p.returncode==0,stderr;values.append(json.loads(stdout))
    first,second=sorted(values,key=lambda x:x['start'])
    assert second['start']-first['end']>=1.08


def test_redirect_checks_new_protocol_robots_before_destination(monkeypatch):
    seen=[]
    def handler(req):
        host=req.headers['host'];seen.append((req.url.scheme,host,req.url.path))
        if req.url.scheme=='https' and req.url.path=='/robots.txt':return httpx.Response(404)
        if req.url.scheme=='https' and req.url.path=='/start':return httpx.Response(302,headers={'Location':'http://public.example/private'})
        if req.url.scheme=='http' and req.url.path=='/robots.txt':return httpx.Response(403)
        pytest.fail('target must not be requested when robots is unavailable')
    mock_http(monkeypatch,handler)
    with pytest.raises(FetchError,match='robots_or_robots_unavailable'):fetch('https://public.example/start',timeout=15)
    assert seen==[('https','public.example','/robots.txt'),('https','public.example','/start'),('http','public.example','/robots.txt')]


@pytest.mark.parametrize('status,content_type,body,allowed',[
    (200,'text/plain','temporary upstream failure',False),
    (200,'text/html','<html>login required</html>',False),
    (200,'text/plain','User-agent: *\nAllow: /\n<html><body>unrelated page</body></html>',False),
    (200,'text/plain','',True),
    (200,'text/plain','# comment\nSitemap: https://public.example/sitemap.xml',True),
    (200,'text/plain','User-agent: *\nDisallow: /private',False),
    (200,'text/plain','User-agent: GPTBot\nDisallow: /\nUser-agent: *\nAllow: /',True),
    (403,'text/plain','',False),(302,'text/plain','',False),(404,'text/plain','',True),
])
def test_robots_ambiguous_or_blocked_responses_fail_closed(monkeypatch,status,content_type,body,allowed):
    mock_http(monkeypatch,lambda req:httpx.Response(status,headers={'Content-Type':content_type},text=body))
    assert robots_allowed('https://public.example/private') is allowed


def test_optional_strict_robots_policy_preserves_prior_intersection(monkeypatch):
    monkeypatch.setenv('LEAD_ROBOTS_EXTRA_AGENTS','GPTBot,ChatGPT-User')
    mock_http(monkeypatch,lambda req:httpx.Response(200,headers={'Content-Type':'text/plain'},text='User-agent: GPTBot\nDisallow: /\nUser-agent: *\nAllow: /'))
    assert not robots_allowed('https://public.example/private')


def test_scrapy_enforces_robots_and_releases_rate_lease(monkeypatch):
    middleware=StrictRobotsMiddleware()
    monkeypatch.setattr('collectors.spider.robots_allowed',lambda url:False)
    with pytest.raises(IgnoreRequest,match='robots_or_robots_unavailable'):
        middleware.process_request(Request('https://public.example/private'))
    rate=SharedRequestRateMiddleware();request=Request('https://public.example/ok')
    rate.process_request(request)
    assert request.meta.get('_public_request_lease')
    assert rate.process_response(request,Response(request.url)).url==request.url
    assert '_public_request_lease' not in request.meta
    settings=PublicSourceSpider.custom_settings['DOWNLOADER_MIDDLEWARES']
    assert settings['scrapy.downloadermiddlewares.robotstxt.RobotsTxtMiddleware'] is None
    assert settings['collectors.spider.StrictRobotsMiddleware'] < settings['collectors.spider.SharedRequestRateMiddleware']


def test_scrapy_offline_requests_share_robots_and_content_guard(tmp_path):
    """Use actual downloader middleware with synthetic HTTP, never the network."""
    root=Path(__file__).resolve().parents[1]
    script=tmp_path/'scrapy_probe.py'
    script.write_text('''import json,time\nfrom urllib.parse import urlsplit\nimport httpx\nfrom scrapy.http import HtmlResponse\nfrom scrapy.crawler import CrawlerProcess\nimport radar.public_fetch as pf\nfrom collectors.spider import PublicSourceSpider\noriginal=httpx.Client\nevents=[]\npf.validate_url=lambda u:(urlsplit(u),"93.184.216.34")\ndef handler(req):\n events.append(("robots",time.monotonic()))\n return httpx.Response(404)\npf.httpx.Client=lambda **kwargs:original(**kwargs,transport=httpx.MockTransport(handler))\nclass FakeTransport:\n def process_request(self,request):\n  events.append(("page",time.monotonic()))\n  if "index" in request.url:\n   html="<a href=\\"https://sthjj.gz.gov.cn/hjgl/jsxm/hpspqgs/content/post_123.html\\">某制造企业新建项目环境影响评价文件拟审查公示</a>"\n  else:\n   html="<div id=\\"logPanel\\"><p>项目名称：某制造企业新建项目</p><p>建设单位：测试制造有限公司</p><p>建设内容：新建生产线以及配套仓储设施。本公告为环境影响评价公示。</p></div>"\n  return HtmlResponse(request.url,body=html.encode(),encoding="utf-8",request=request,status=200)\nclass FakeSpider(PublicSourceSpider):\n custom_settings={**PublicSourceSpider.custom_settings,"LOG_ENABLED":False,"DOWNLOADER_MIDDLEWARES":{**PublicSourceSpider.custom_settings["DOWNLOADER_MIDDLEWARES"],"__main__.FakeTransport":950}}\nresult={}\nprocess=CrawlerProcess()\nprocess.crawl(FakeSpider,source_key="gz_environment",result=result,max_items=1)\nprocess.start()\nprint(json.dumps({"status":result.get("status"),"items":len(result.get("items",[])),"events":events}))\n''',encoding='utf-8')
    env={**os.environ,'PYTHONPATH':str(root),'LEAD_PUBLIC_RATE_DIR':str(tmp_path/'scrapy-rate')}
    completed=subprocess.run([sys.executable,str(script)],cwd=root,env=env,text=True,capture_output=True,timeout=20)
    assert completed.returncode==0,completed.stderr
    result=json.loads(completed.stdout.strip().splitlines()[-1]);events=result['events']
    assert result['items']==1 and [x[0] for x in events]==['robots','page','page']
    assert all(events[i+1][1]-events[i][1]>=1.09 for i in range(len(events)-1))


def test_scrapy_connector_resolver_rejects_private_or_invalid_dns_results():
    from collectors.resolver import PublicOnlyResolver
    from scrapy.resolver import CachingThreadedResolver
    from twisted.internet.defer import succeed
    from twisted.internet.error import DNSLookupError
    resolver=PublicOnlyResolver.__new__(PublicOnlyResolver)
    for address,expected in [('127.0.0.1',False),('169.254.169.254',False),('224.0.0.1',False),('::ffff:127.0.0.1',False),('not-an-ip',False),('1.1.1.1',True)]:
        with patch.object(CachingThreadedResolver,'getHostByName',return_value=succeed(address)):
            outcomes=[]
            resolver.getHostByName('source.example').addCallbacks(lambda value:outcomes.append(('ok',value)),lambda error:outcomes.append(('error',error.type)))
            assert (outcomes[0][0]=='ok') is expected
            if not expected:assert outcomes[0][1] is DNSLookupError
    assert PublicSourceSpider.custom_settings['DNS_RESOLVER']=='collectors.resolver.PublicOnlyResolver'
    assert PublicSourceSpider.custom_settings['HTTPPROXY_ENABLED'] is False
