from datetime import date
import pytest
from radar.discovery import discover
from radar.models import Source, Company, Lead, Document
from radar.services import ingest_item
from radar.entities import japanese_link_for_lead
from radar.queues import japan_signal


def body(text):return ('<html><head><meta charset="utf-8"></head><body>'+text+'</body></html>').encode()


def job(title,pub,words):
    return '<div class="list wow"><div class="name dot">'+title+'</div><time>发布时间：'+pub+'</time><span>工作地点：上海</span><div>岗位职责：'+words+'</div></div>'


def fake_fetch(url,**kwargs):
    if 'CompanyProfile/' in url:
        content=body('上海华加国际货运代理有限公司是商船三井物流株式会社在中国的投资子公司。'+('国际物流和仓储业务'*20))
    elif 'page=2' in url:
        content=body('<a href="/CompanyProfile/index.aspx">公司简介</a><div class="job-list">'+job('历史空运','2023-05-15','货物操作内容'*15)+'</div>')
    else:
        content=body('<a href="/CompanyProfile/index.aspx">公司简介</a><div class="job-list">'+job('项目货物销售经理','2026-09-02','重大件和滚装业务拓展'*12)+job('海运销售','2026-09-02','海运业务'*20)+job('货代销售','2026-07-20','货代业务'*20)+job('历史海运','2023-05-15','海运工作'*20)+'</div><a href="?page=2">2</a>')
    return {'body':content,'url':url}


@pytest.mark.django_db
def test_dated_jobs_separate_records_not_buyer_and_idempotent(tmp_path,settings,monkeypatch):
    settings.DATA_DIR=tmp_path;settings.LEAD_MODEL_ENABLED=False
    source=Source.objects.create(key='japan_directory_mol_recruitment',name='MOL招聘',url='https://www.mol-logistics.cn/TalentAcquisition/list.aspx?page=1',kind='enterprise_discovery')
    monkeypatch.setattr('radar.mol_recruitment.fetch',fake_fetch)
    monkeypatch.setattr('radar.mol_recruitment.timezone.localdate',lambda:date(2026,11,14))
    result=discover(source);assert result['stats']['details_ok']==5 and result['stats']['older_than_window']==2
    assert len(result['items'])==3 and result['stats']['list_pages']==2
    leads=[ingest_item(source,item)[0] for item in result['items']]
    assert len({l.external_id for l in leads})==3 and Lead.objects.count()==3
    lead=leads[0];assert str(lead.published_date)=='2026-09-02' and lead.buyer_role=='unknown'
    assert '上海华加国际货运代理有限公司' not in lead.text
    assert any(e['quote']=='项目货物销售经理' for e in lead.company.discovery_evidence)
    assert lead.metadata['posting_date_status']=='source_stated' and '有效性待核' in lead.title
    assert japanese_link_for_lead(lead)['role']=='logistics_peer' and japan_signal(lead)['role']=='logistics_peer'
    assert '发布时间和有效性未知' not in lead.summary
    from radar.evidence import reextract
    reextract(lead.pk);lead.refresh_from_db();assert lead.company.name=='上海华加国际货运代理有限公司' and lead.buyer_role=='unknown'
    assert '发布时间和有效性未知' not in lead.summary
    def changed_chrome(url, **kwargs):
        result=fake_fetch(url, **kwargs)
        result['body'] += b'<!-- unrelated changing page chrome -->'
        return result
    monkeypatch.setattr('radar.mol_recruitment.fetch',changed_chrome)
    again=discover(source)
    for item in again['items']:
        _,created,updated=ingest_item(source,item)
        assert not created and not updated
    assert Lead.objects.count()==3 and Company.objects.count()==1 and Document.objects.count()==3


@pytest.mark.django_db
def test_no_company_proof_stops_recruitment_source(tmp_path,settings,monkeypatch):
    settings.DATA_DIR=tmp_path
    source=Source.objects.create(key='japan_directory_mol_recruitment',name='MOL招聘',url='https://www.mol-logistics.cn/TalentAcquisition/list.aspx?page=1',kind='enterprise_discovery')
    monkeypatch.setattr('radar.mol_recruitment.fetch',lambda url,**kw:{'body':body('不是企业简介'),'url':url})
    with pytest.raises(ValueError,match='employer_proof_missing'):discover(source)
    assert Company.objects.count()==0
