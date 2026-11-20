"""Synthetic affiliation hints are not ownership proof or procurement evidence."""
import pytest
from radar.entities import classify_japanese_link, japanese_link_for_lead
from radar.extraction import extract_event
from radar.models import Company, Lead, Source


def test_seed_and_joint_brand_remain_unverified():
    a=classify_japanese_link('广汽丰田汽车有限公司')
    assert a['level']=='joint_venture' and not a['ownership_proven']
    b=classify_japanese_link('三井物流测试有限公司')
    assert b['level']=='seed' and not b['ownership_proven']
    assert classify_japanese_link('天津东丽区测试物流有限公司')['level']=='none'
    assert classify_japanese_link('示例株式会社')['level']=='none'
    assert classify_japanese_link('示例株式会社')['suffix_hint']


def test_directory_needs_exact_name_and_real_evidence():
    name='合成制造有限公司'
    row={'source':'japan_directory_dalian','status':'japan_affiliation_unverified','url':'https://example.org/','quote':'12 | 合成制造有限公司'}
    a=classify_japanese_link(name,[row]);assert a['level']=='directory' and not a['ownership_proven']
    assert classify_japanese_link(name,[{**row,'quote':'新合成制造有限公司'}])['level']=='none'
    assert classify_japanese_link(name,[{**row,'url':''}])['level']=='none'
    assert classify_japanese_link(name,[], 'japan_confirmed','')['level']=='none'
    assert classify_japanese_link(name,[], 'japan_confirmed','人工核验股权资料')['ownership_proven']


def test_peers_are_retained_as_candidates():
    assert classify_japanese_link('山九物流（上海）有限公司')['role']=='logistics_peer'
    assert classify_japanese_link('NX Logistics 中国有限公司')['role']=='logistics_peer'
    assert classify_japanese_link('近铁百货有限公司')['role']=='unknown'
    assert classify_japanese_link('next普通运输公司')['level']=='none'


def test_unrelated_buyer_not_tagged_by_broker_or_body():
    result=extract_event('运输招标公告','采购人：普通材料有限公司\n招标代理机构：三井咨询有限公司\n相关品牌：丰田')
    assert result['is_japanese_linked']=='none'
    assert result['ownership_status']=='unknown'

    matched=extract_event('汽车运输服务采购公告','采购人：广汽丰田汽车有限公司\n采购代理：普通咨询有限公司')
    assert matched['is_japanese_linked']=='joint_venture'
    assert matched['buyer_name']=='广汽丰田汽车有限公司' and matched['ownership_status']=='unknown'


@pytest.mark.django_db
def test_lead_uses_linked_organization_and_does_not_promote_agent():
    source=Source.objects.create(key='synthetic-japan',name='synthetic')
    company=Company.objects.create(name='山九物流测试有限公司')
    lead=Lead.objects.create(source=source,company=company,external_id='1',title='仓储运输公告',content_hash='a'*64)
    assert japanese_link_for_lead(lead)['role']=='logistics_peer'
    lead.buyer_role='agent';assert japanese_link_for_lead(lead)['level']=='none'
    company.refresh_from_db();assert company.ownership_status=='unknown'
