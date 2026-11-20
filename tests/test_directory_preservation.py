"""Directory refresh must preserve user records and older source evidence."""
import pytest
from radar.discovery import discover
from radar.models import Company,Source

@pytest.mark.django_db
def test_directory_refresh_keeps_all_evidence_and_manual_profile(tmp_path,settings,monkeypatch):
    settings.DATA_DIR=tmp_path
    old=[{'source':'shfia_directory','url':'https://example.com','document_hash':str(n),'quote':'旧依据'+str(n)} for n in range(22)]
    manual={'website':'https://manual.example','last_discovered_at':'人工记录','industry':'人工填写'}
    c=Company.objects.create(name='测试制造有限公司',ownership_status='japan_confirmed',ownership_evidence='人工股权依据',discovery_evidence=old,profile=manual)
    source=Source.objects.create(key='shfia_directory',name='合成名录',url='https://example.com',kind='enterprise_discovery')
    page='<html><head><meta charset="utf-8"></head><body><table><tr><td>测试制造有限公司</td></tr></table></body></html>'.encode()
    monkeypatch.setattr('radar.discovery.robots_allowed',lambda url:True)
    monkeypatch.setattr('radar.discovery.fetch',lambda url:{'body':page,'url':url})
    discover(source);c.refresh_from_db()
    assert c.discovery_evidence[:len(old)]==old and len(c.discovery_evidence)==len(old)+1
    assert c.profile==manual and c.ownership_status=='japan_confirmed' and c.ownership_evidence=='人工股权依据'
    discover(source);c.refresh_from_db()
    assert c.discovery_evidence[:len(old)]==old and len(c.discovery_evidence)==len(old)+1
    assert c.profile==manual
    changed=page.replace(b'</body>',b'<p>New edition</p></body>')
    monkeypatch.setattr('radar.discovery.fetch',lambda url:{'body':changed,'url':url})
    discover(source);c.refresh_from_db()
    assert c.discovery_evidence[:len(old)]==old and len(c.discovery_evidence)==len(old)+2
    assert c.profile==manual

@pytest.mark.django_db
def test_directory_failure_rolls_back_batch_and_checkpoint(tmp_path,settings,monkeypatch):
    settings.DATA_DIR=tmp_path
    old=[{'source':'manual','quote':'原有证据'}]
    company=Company.objects.create(name='甲方测试有限公司',discovery_evidence=old,profile={'industry':'人工填写'})
    source=Source.objects.create(key='shfia_directory',name='合成名录',url='https://example.com',kind='enterprise_discovery',checkpoint={'directory_offset':0,'keep':'value'})
    page='<html><head><meta charset="utf-8"></head><body><table><tr><td>甲方测试有限公司</td></tr><tr><td>乙方测试有限公司</td></tr><tr><td>丙方测试有限公司</td></tr></table></body></html>'.encode()
    monkeypatch.setattr('radar.discovery.robots_allowed',lambda url:True)
    monkeypatch.setattr('radar.discovery.fetch',lambda url:{'body':page,'url':url})
    original_save=Company.save
    def fail_second(instance,*args,**kwargs):
        if instance.name=='乙方测试有限公司':raise RuntimeError('simulated failure')
        return original_save(instance,*args,**kwargs)
    monkeypatch.setattr(Company,'save',fail_second)
    with pytest.raises(RuntimeError,match='simulated failure'):
        discover(source,limit=2)
    source.refresh_from_db();company.refresh_from_db()
    assert source.checkpoint=={'directory_offset':0,'keep':'value'}
    assert company.discovery_evidence==old and company.profile=={'industry':'人工填写'}
    assert not Company.objects.filter(name='乙方测试有限公司').exists()
