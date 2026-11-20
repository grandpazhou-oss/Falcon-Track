import uuid

import pytest

from radar.analysis import enqueue
from radar.ai_config import config
from radar.mimo_tiers import FLASH, PRO, FlashScreen, estimated_rmb
from radar.models import KeywordCandidate, SalesFeedback
from radar.sales_ui import display_card


def test_labels_and_chinese_region():
    card=display_card({'source_kind':'smrj_100oku','size_tier':'micro_sme','china_location':'蘇州'})
    assert (card['ui_source'],card['ui_size'],card['ui_region'])==('100 亿宣言','中小微','east')


def test_cost_and_strict_boolean():
    assert estimated_rmb(PRO,{'prompt_tokens':1000,'completion_tokens':100,'cached_tokens':100})=='0.0033025'
    assert estimated_rmb(FLASH,{'prompt_tokens':1000,'completion_tokens':100})=='0.0012'
    with pytest.raises(ValueError):
        FlashScreen.model_validate({'is_china_mainland_logistics_relevant':'false','company_name':'',
                                   'china_city':'','action_type':'无关噪音','one_line_reason':'海外活动'})


@pytest.mark.django_db
def test_filter_and_same_tier_queue_identity(client):
    case=KeywordCandidate.objects.create(canonical_url='https://example.com/1',provider='smrj_100oku',
        title='日本企業 上海工場新設',snippet='日本企業 上海工場新設',text='日本企業 上海工場新設'+'文'*1000,
        raw_path='raw/example.html',result={'source_sha256':'test','china_region':'上海'})
    c=config()
    c.base_url='https://api.xiaomimimo.com/v1'
    c.save(update_fields=['base_url'])
    flash,_=enqueue(None,'analysis',str(uuid.uuid4()),candidate_id=case.pk,tier='flash')
    pro,_=enqueue(None,'analysis',str(uuid.uuid4()),candidate_id=case.pk,tier='pro')
    same,new=enqueue(None,'analysis',str(uuid.uuid4()),candidate_id=case.pk,tier='pro')
    assert len(flash.input_snapshot['flash_excerpt'])==800 and not flash.input_snapshot['evidence']
    assert flash.config_snapshot['model']==FLASH and pro.config_snapshot['model']==PRO and same.pk==pro.pk and not new
    page=client.get('/sales-pools/?grade=B&region=east&q=上海')
    assert '100 亿宣言' in page.content.decode() and 'smrj_100oku' not in page.content.decode()
    assert '100 亿宣言' not in client.get('/sales-pools/?grade=B&region=south&q=上海').content.decode()

    SalesFeedback.objects.create(source_url=case.canonical_url,status='hold',note='保留已有跟进备注')
    reply=client.post('/sales-pools/feedback/', {'card':f'/sales-pools/candidate/{case.pk}/','status':'accepted'})
    assert reply.status_code==302
    assert SalesFeedback.objects.get(source_url=case.canonical_url).note=='保留已有跟进备注'
