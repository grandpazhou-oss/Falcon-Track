import json
import zipfile
import pytest
from django.core.management import call_command
from radar.entities import business_role_for_company, classify_japanese_link, japanese_link_for_lead
from radar.models import Company, Lead, Source
from radar.seed_dictionary import export_dictionary, matches, name_terms


@pytest.mark.django_db
def test_branch_terms_never_truncated_but_full_names_remain(tmp_path,settings):
    settings.DATA_DIR=tmp_path
    name='三菱电梯有限公司苏州分公司'
    terms=name_terms(name)
    assert name in terms and not any(t.endswith(('分','代表','办事')) for t in terms)
    c=Company.objects.create(name=name,discovery_evidence=[{'source':'japan_directory_test','status':'japan_affiliation_unverified','url':'https://example.org','listed_name':name,'quote':name,'entity_type':'branch'}])
    d=export_dictionary()
    assert d['company_count']==1 and any(x['term']==name for x in d['terms'])
    assert not any(x['term'].endswith(('分','代表','办事')) for x in d['terms'])


@pytest.mark.django_db
def test_ambiguous_and_broad_brands_need_adjacent_company_context(tmp_path,settings):
    settings.DATA_DIR=tmp_path
    data={'terms':[{'term':'三井','ambiguous':True},{'term':'本田','ambiguous':True},{'term':'青云','ambiguous':True},{'term':'村田','ambiguous':True},{'term':'NX','ambiguous':True},{'term':'Nippon Express','ambiguous':True}]}
    (tmp_path/'japanese_enterprise_seeds.json').write_text(json.dumps(data,ensure_ascii=False))
    for s in ('三井附近项目扩产','本田的故事','青云路以东','三井\n汽车配件','nx123'):
        assert not matches(s),s
    for s in ('三井汽车有限公司扩产','本田（中国）汽车有限公司','青云电子项目','NX Logistics 中国有限公司','Nippon Express Logistics 中国有限公司'):
        assert matches(s),s
    assert classify_japanese_link('三井附近物流园').get('level')=='none'
    assert classify_japanese_link('村田附近物流园',dynamic=False).get('level')=='none'
    assert classify_japanese_link('三井汽车有限公司').get('level')=='seed'
    assert classify_japanese_link('广汽丰田汽车有限公司').get('level')=='joint_venture'


@pytest.mark.django_db
@pytest.mark.parametrize('brand',['三菱','三井','住友','日立','丰田','本田','松下','佳能'])
def test_common_broad_brands_reject_non_company_prose(tmp_path,settings,brand):
    settings.DATA_DIR=tmp_path
    (tmp_path/'japanese_enterprise_seeds.json').write_text(json.dumps({'terms':[{'term':brand,'ambiguous':True}]},ensure_ascii=False))
    assert not matches(brand+'附近发生建设活动')
    assert classify_japanese_link(brand+'附近物流中心',dynamic=False)['level']=='none'
    assert matches(brand+'汽车有限公司扩产')
    assert classify_japanese_link(brand+'汽车有限公司')['level']=='seed'


@pytest.mark.django_db
def test_stale_ambiguous_metadata_cannot_bypass_context(tmp_path,settings):
    settings.DATA_DIR=tmp_path
    (tmp_path/'japanese_enterprise_seeds.json').write_text(json.dumps({'terms':[{'term':'三井','ambiguous':True}]}))
    s=Source.objects.create(key='synthetic-eia',name='合成环评',kind='early_signal')
    lead=Lead.objects.create(source=s,external_id='1',title='三井附近新建仓库',text='三井附近新建仓库',metadata={'japanese_term_matches':[{'term':'三井'}]})
    assert japanese_link_for_lead(lead)['level']=='none'


def test_business_roles_are_candidate_categories():
    assert business_role_for_company('山九物流（上海）有限公司')=='peer_logistics'
    assert business_role_for_company('上海经贸山九储运有限公司')=='peer_logistics'
    assert business_role_for_company('丰田汽车有限公司')=='shipper'
    assert business_role_for_company('山九咨询有限公司')=='unknown_service'
    name='上海合成国际货运有限公司'
    evidence=[{'source':'japan_directory_test','status':'japan_affiliation_unverified','url':'https://example.org','listed_name':name,'quote':name}]
    link=classify_japanese_link(name,evidence)
    assert business_role_for_company(name,link=link)=='peer_logistics'
    assert business_role_for_company(name)=='unknown_service'
    maker='合成物流产品制造有限公司'
    evidence=[{'source':'japan_directory_test','status':'japan_affiliation_unverified','url':'https://example.org','listed_name':maker,'quote':maker}]
    assert business_role_for_company(maker,link=classify_japanese_link(maker,evidence))=='shipper'


@pytest.mark.django_db
def test_anonymous_job_ui_does_not_identify_peer_as_employer(client):
    s=Source.objects.create(key='synthetic-job',name='合成招聘',kind='recruitment')
    lead=Lead.objects.create(source=s,external_id='1',title='物流岗位',text='某日企招聘',metadata={'signal_type':'job','employer_display':'某日企'})
    body=client.get(f'/leads/{lead.pk}/').content.decode()
    assert '同城同行业的企业只能作为潜在研究目标' in body
    lead.refresh_from_db();assert lead.company_id is None and lead.buyer_role=='unknown'


@pytest.mark.django_db(transaction=True)
def test_backup_preserves_unreferenced_directory_evidence(tmp_path,settings):
    settings.DATA_DIR=tmp_path
    folder=tmp_path/'raw'/'directories';folder.mkdir(parents=True)
    (folder/'orphan.html').write_text('orphan evidence')
    archive=tmp_path/'snapshot.zip';call_command('backup_data',output=str(archive))
    with zipfile.ZipFile(archive) as z:
        assert z.read('raw/directories/orphan.html')==b'orphan evidence'
        manifest=json.loads(z.read('manifest.json'))
        assert manifest['unreferenced_directory_evidence']==['raw/directories/orphan.html']
