import pytest
from radar.discovery import discover
from radar.models import Company, Source
from radar.entities import classify_japanese_link


def html(s):return ('<html><head><meta charset="utf-8"></head><body>'+s+'</body></html>').encode()


def fake_sources(mismatch=False):
    roster=html('<ul class="CompanyList"><li>苏州样本电子有限公司</li><li>产机样本（苏州）有限公司</li><li>佳能样本（苏州）有限公司</li><li>无法核对有限公司</li></ul>')
    chamber=html('<table class="product_info_table"><a href="https://sznissho.org/2026/10/14/a/?lang=zh">苏州样本电子有限公司</a><a href="https://sznissho.org/2026/10/14/b/?lang=zh">产机样本（苏州）有限公司</a></table>')
    canon=html('<table><tr><td class="company_td"><a href="http://www.canon-suzhou.com.cn/">佳能样本（苏州）有限公司<span>»</span></a></td></tr></table>')
    result={
        'https://snd-jp.com/wp/snd/about-html/about-jp/':roster,
        'https://sznissho.org/trade_meeting/product_info_zh/':chamber,
        'https://www.canon.com.cn/about/china/introduction/':canon,
        'https://panasonic.cn/about/panasonic-china/':html('其他集团资料'),
        'https://www.shimadzu.com.cn/aboutus/company/chinacompany.html':html('其他集团资料'),
        'https://sznissho.org/2026/10/14/a/?lang=zh':html('<article>苏州样本电子有限公司'+('事业介绍与制造产品。'*30)+'</article>'),
        'https://sznissho.org/2026/10/14/b/?lang=zh':html('<article>'+('不匹配企业有限公司' if mismatch else '产机样本（苏州）有限公司')+('事业介绍与制造产品。'*30)+'</article>'),
        'https://www.canon-suzhou.com.cn/':html('佳能样本（苏州）有限公司'+('事业介绍与制造产品。'*30)),
    }
    return lambda url,**kw:{'body':result[url],'url':url}


@pytest.mark.django_db
def test_suzhou_crosscheck_requires_exact_list_and_two_real_evidence_records(tmp_path,settings,monkeypatch):
    settings.DATA_DIR=tmp_path
    source=Source.objects.create(key='japan_directory_suzhou_checked',name='苏州交叉核验',url='https://snd-jp.com/wp/snd/about-html/about-jp/',kind='enterprise_discovery')
    existing=Company.objects.create(name='苏州样本电子有限公司',profile={'industry':'人工记录'},ownership_evidence='人工依据',discovery_evidence=[{'source':'manual','quote':'历史'}])
    monkeypatch.setattr('radar.suzhou_crosscheck.fetch',fake_sources())
    a=discover(source);b=discover(source)
    assert a['stats']['companies_new']==3 and b['stats']['companies_new']==0
    assert a['stats']['coverage_complete'] is True
    assert Company.objects.count()==4 and Company.objects.filter(name='无法核对有限公司').exists()
    existing.refresh_from_db();assert existing.profile['industry']=='人工记录' and existing.ownership_evidence=='人工依据'
    assert existing.discovery_evidence[0]=={'source':'manual','quote':'历史'} and len(existing.discovery_evidence)==2
    assert (tmp_path/existing.discovery_evidence[1]['raw_path']).exists()
    assert classify_japanese_link(existing.name,existing.discovery_evidence)['level']=='directory'
    assert existing.ownership_status=='unknown'


@pytest.mark.django_db
def test_suzhou_website_mismatch_does_not_block_roster(tmp_path,settings,monkeypatch):
    settings.DATA_DIR=tmp_path
    source=Source.objects.create(key='japan_directory_suzhou_checked',name='苏州名录',url='https://snd-jp.com/wp/snd/about-html/about-jp/',kind='enterprise_discovery')
    original=fake_sources(mismatch=True)
    requested=[]
    def read(url,**kw):
        requested.append(url);return original(url,**kw)
    monkeypatch.setattr('radar.suzhou_crosscheck.fetch',read)
    discover(source)
    assert Company.objects.count()==4 and requested==[source.url]



def test_group_indices_match_exact_roster_and_company_detail_urls():
    from radar.suzhou_crosscheck import _candidates
    roster=html('<ul class="CompanyList"><li>样本松下（苏州）有限公司</li><li>样本岛津（苏州）有限公司</li><li>相似但不相同有限公司</li></ul>')
    panasonic=html('<a href="/about/panasonic_china/a/">样本松下（苏州）有限公司</a><a href="https://bad.example/x">相似但不相同有限公司</a>')
    shimadzu=html('<div class="card"><div class="card-header">样本岛津（苏州）有限公司</div><a href="/SSM/index.html">详情</a></div>')
    _,found=_candidates(roster,html(''),html(''),panasonic,shimadzu)
    assert found==[('样本松下（苏州）有限公司','https://panasonic.cn/about/panasonic_china/a/','panasonic.cn'),
                   ('样本岛津（苏州）有限公司','https://www.shimadzu.com.cn/SSM/index.html','www.shimadzu.com.cn')]


@pytest.mark.django_db
def test_unavailable_roster_does_not_advance_checkpoint(tmp_path,settings,monkeypatch):
    import httpx
    settings.DATA_DIR=tmp_path
    source=Source.objects.create(key='japan_directory_suzhou_checked',name='苏州名录',url='https://snd-jp.com/wp/snd/about-html/about-jp/',kind='enterprise_discovery')
    def blocked(*args,**kwargs):raise httpx.ConnectError('TLS failure')
    monkeypatch.setattr('radar.suzhou_crosscheck.fetch',blocked)
    with pytest.raises(ValueError,match='ConnectError'):discover(source)
    source.refresh_from_db();assert source.checkpoint=={} and Company.objects.count()==0
