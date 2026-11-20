"""Synthetic cases: no source-site content or external calls."""
from datetime import date
from types import SimpleNamespace
import pytest
from collectors.jbic_sme import parse_jbic_index,parse_jbic_material,collect_jbic


def page(*,date_text='2026年2月10日',contract='本契約は2025年6月25日に締結済みです。',year='1996年',market='中国'):
    return (f'''<html><head><meta charset="utf-8"></head><main><div id="h1_title"><h1>{market}における製造事業に対する融資</h1></div>
    <div class="main"><section><article><address class="header-address">{date_text}</address><ol>
    <li>株式会社国際協力銀行（JBIC）は、株式会社架空衣料（本社：東京都、以下「架空衣料」）の中華人民共和国（以下「中国」）法人SUZHOU SAMPLE GARMENT CO., LTD.（以下「SSG」）との間で貸付契約を締結しました。</li>
    <li>本件は、SSGが{market}の蘇州市において実施する衣料品の製造・販売事業に必要な資金を融資するものです。</li>
    <li>架空衣料は衣料品製造を行う中小企業です。同社は{year}にSSGを設立しました。</li>
    </ol><div class="footnotes">{contract}</div></article></section></div></main></html>''').encode()


def test_jbic_research_extracts_independent_dates_and_names():
    data=parse_jbic_material(page(),'https://www.jbic.go.jp/ja/information/press/press-2025/press_00149.html')
    signal=data['metadata']['business_signal'];entities=signal['entities'];dates=signal['dates']
    assert entities[0]['name']=='株式会社架空衣料' and entities[0]['role']=='japanese_parent'
    assert entities[1]['name']=='SUZHOU SAMPLE GARMENT CO., LTD.' and entities[1]['role']=='china_entity'
    assert entities[1]['relationship_type']=='unknown'
    assert dates['published_at']['value']=='2026-02-10' and dates['contract_date']['value']=='2025-06-25'
    assert dates['china_established']['value']=='1996' and dates['china_established']['precision']=='year'
    assert signal['company_scale']=='sme' and signal['event_type']=='finance_support'
    assert all(f['quote'] in data['text'] for f in signal['facts'])
    assert all('国際協力銀行' not in x['name'] for x in entities)
    assert signal['is_procurement'] is False


def test_relative_contract_is_not_manufactured_from_publication_date():
    data=parse_jbic_material(page(contract='本契約は1日に締結しました。'),'https://www.jbic.go.jp/ja/information/press/press-2026/press_00013.html')
    assert data['metadata']['business_signal']['dates']['contract_date']['value'] is None
    assert data['metadata']['business_signal']['dates']['contract_date']['precision']=='unknown'


def test_explicit_today_contract_may_use_document_date_with_derivation():
    content=page(contract='').replace('（JBIC）は、'.encode(),'（JBIC）は、本日、'.encode())
    data=parse_jbic_material(content,'https://www.jbic.go.jp/ja/information/press/press-2025/press_00195.html')
    contract=data['metadata']['business_signal']['dates']['contract_date']
    assert contract['value']=='2026-02-10' and contract['status']=='derived_relative_today'


def test_financing_amount_is_not_treated_as_logistics_budget():
    content=page().replace('との間で貸付契約を締結しました。'.encode(),'との間で、融資金額35百万円を限度とする貸付契約を締結しました。'.encode())
    data=parse_jbic_material(content,'https://www.jbic.go.jp/ja/information/press/press-2025/press_00149.html')
    signal=data['metadata']['business_signal'];amount=next(x for x in signal['facts'] if x['field']=='financing_amount')
    assert amount['value']=='35百万円' and signal['is_procurement'] is False


def test_no_china_entity_does_not_turn_hiroshima_into_mainland():
    content=b'<html><main><div id="h1_title"><h1>test</h1></div><div class="main"><section><article><address>2026\xe5\xb9\xb42\xe6\x9c\x8810\xe6\x97\xa5</address><p>test</p></article></section></div></main></html>'
    with pytest.raises(ValueError):parse_jbic_material(content,'https://www.jbic.go.jp/ja/information/press/press-2025/press_00149.html')


def test_jbic_automated_copy_requires_external_consultation(monkeypatch):
    monkeypatch.setattr('radar.public_fetch.fetch',lambda *a,**k:pytest.fail('no network'))
    monkeypatch.setattr('socket.create_connection',lambda *a,**k:pytest.fail('no network'))
    result=collect_jbic(SimpleNamespace(options={'authorized':True},checkpoint={}))
    assert result['status']=='blocked' and not result['items']
    assert '事先' in result['error'] and result['stats']['policy_status']=='consultation_required'


def test_jbic_material_rejects_lookalike_or_nonstandard_port():
    for url in ['https://www.jbic.go.jp.evil.test/ja/information/press/press-2025/press_00149.html',
                'https://www.jbic.go.jp:444/ja/information/press/press-2025/press_00149.html']:
        with pytest.raises(ValueError):parse_jbic_material(page(),url)


def test_index_uses_entire_24_month_window_and_stable_ids():
    rows=''.join(f'<tr><td>{d}</td><td><a href="/ja/information/press/press-2025/press_{i}.html">{title}</a></td></tr>' for i,d,title in [
        ('00001','2026年8月1日','タイにおいて事業'),('00002','2026年3月27日','中国において衣料製造'),
        ('00003','2025年2月10日','中国において部品製造'),('00004','2023年1月1日','中国において部品製造')])
    records=parse_jbic_index(('<html><meta charset="utf-8"><table class="block">'+rows+'</table></html>').encode(),as_of=date(2026,11,14))
    assert [r['id'] for r in records]==['press-2025/press_00002','press-2025/press_00003']


def test_missing_index_layout_is_not_a_successful_empty_scan():
    with pytest.raises(ValueError,match='列表结构'):
        parse_jbic_index(b'<html><body>temporary or redesigned</body></html>',as_of=date(2026,11,14))


def test_jbic_uses_explicit_note_date_and_scoped_financing_reason():
    body = page(contract='契約締結日：2025年6月25日').replace('事業に必要な資金'.encode(), '事業の設備増強に必要な資金'.encode())
    data = parse_jbic_material(body, 'https://www.jbic.go.jp/ja/information/press/press-2025/press_00149.html')
    signal = data['metadata']['business_signal']
    assert signal['dates']['contract_date']['value'] == '2025-06-25'
    assert data['published_date'] == '2026-02-10'
    assert signal['project_city'] == '苏州' and '設備増強' in signal['finance_purpose']
