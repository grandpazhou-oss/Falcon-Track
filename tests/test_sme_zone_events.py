from types import SimpleNamespace

from collectors.zone_events import listing, detail, collect_changzhou_zone, collect_zone


def make_list(rows, *, more=True):
    links = ''.join(
        f'<li><a href="/html/cznd/2026/EFBJILJD_0626/{i}.html" title="{title}">{title}</a>2026-06-26</li>'
        for i, title in rows
    )
    next_link = '<a href="/class/EFBJILJD/2">2</a>' if more else ''
    return f'<html><body><ul>{links}</ul>{next_link}</body></html>'.encode()


def make_detail(content, *, published='2026-06-26', title='企业项目启动建设'):
    return (f'<html><body><table><tr><td class="NewsTitle">{title}</td></tr>'
            f'<tr><td>发布日期：{published} 来源：部门</td></tr>'
            f'<tr><td id="czfxcontent">{content}</td></tr></table></body></html>').encode()


def candidate(i='101', title='企业项目启动建设'):
    return {'id': i, 'url': f'https://www.cznd.gov.cn/html/cznd/2026/EFBJILJD_0626/{i}.html',
            'title': title, 'listing': 'https://www.cznd.gov.cn/class/EFBJILJD'}


def test_listing_keeps_nonbrand_project_and_checks_path():
    body = make_list([('101', '材料生产基地落子园区'), ('102', '某项目签约')])
    rows, more, count = listing(body, 1)
    assert [r['id'] for r in rows] == ['101', '102']
    assert rows[0]['published'] == '2026-06-26'
    assert more and count == 2


def test_japanese_parent_china_entity_and_distinct_publication_and_event_dates():
    body = make_detail('<p>6月25日，日本株式会社晴峰材料国内首个生产基地——常州晴峰材料有限公司'
                       '于园区正式开工建设。项目总投资1000万元。预计2027年6月投产。</p>')
    item = detail(body, candidate())
    signal = item['metadata']['business_signal']
    assert signal['event_type'] == 'construction_started'
    assert signal['company_scale'] == 'unknown'
    assert signal['is_procurement'] is False
    assert [(e['name'], e['role']) for e in signal['entities']] == [
        ('株式会社晴峰材料', 'japanese_parent'), ('常州晴峰材料有限公司', 'china_entity')]
    assert all(e['relationship_type'] == 'unknown' for e in signal['entities'])
    assert signal['dates']['published_at']['value'] == '2026-06-26'
    assert signal['dates']['event_date']['value'] == '2026-06-25'
    assert signal['dates']['event_date']['status'] == 'derived_same_month'
    assert all(f['quote'] in item['text'] and f['location'].startswith('article:line:') for f in signal['facts'])
    assert '采购' in signal['next_step']


def test_project_place_business_and_investment_keep_separate_evidence():
    body = make_detail('<p>6月25日，日本株式会社晴峰材料国内首个生产基地——常州晴峰材料有限公司'
                       '于滨开区中瑞产业园二期正式开工建设。项目总投资超1000万美元，'
                       '主营高端特殊钢零部件加工。</p>')
    item = detail(body, candidate())
    facts = {f['field']: f for f in item['metadata']['business_signal']['facts']}
    assert facts['project_location']['value'] == '滨开区中瑞产业园二期'
    assert '高端特殊钢零部件加工' in facts['business_description']['value']
    assert facts['project_investment']['value'] == '项目总投资超1000万美元'
    assert all(f['quote'] in item['text'] for f in facts.values())
    assert 'logistics_budget' not in facts


def test_no_china_entity_or_first_presence_is_fabricated():
    body = make_detail('<p>6月25日，日本株式会社光山制造的中国首个生产基地，'
                       '在常州高新区正式启动建设。产品将用于精密设备。</p>')
    signal = detail(body, candidate())['metadata']['business_signal']
    assert len(signal['entities']) == 1
    assert signal['entities'][0]['name'] == '株式会社光山制造'
    assert not signal.get('first_china_presence')
    assert not signal.get('china_entity')


def test_unrelated_japanese_machinery_does_not_identify_chinese_project_as_japanese():
    body = make_detail('<p>6月25日，常州本地机械有限公司新厂正式开工建设，拟采购日本株式会社'
                       '高技公司的加工设备。项目位于高新区。</p>')
    assert detail(body, candidate()) is None


def test_planned_operation_is_not_reported_as_completed():
    body = make_detail('<p>6月25日，日本株式会社晴峰材料在常州正式签约新厂项目，'
                       '预计2027年6月建成投产。</p>')
    signal = detail(body, candidate())['metadata']['business_signal']
    assert signal['event_type'] == 'project_signed'
    assert signal['dates']['published_at']['value'] == '2026-06-26'
    assert signal['dates']['event_date']['value'] == '2026-06-25'
    assert signal['dates']['planned_date']['value'] == '2027-06'
    assert signal['dates']['planned_date']['precision'] == 'month'
    assert signal['dates']['planned_date']['status'] == 'planned'


def test_month_day_in_another_month_does_not_invent_year():
    body = make_detail('<p>5月28日，日本株式会社晴峰材料在常州项目正式开工建设。项目位于高新区。</p>')
    d = detail(body, candidate())['metadata']['business_signal']['dates']['event_date']
    assert d['value'] == '--05-28' and d['precision'] == 'month_day' and d['status'] == 'year_unknown'


def test_named_company_and_japan_identity_can_be_proved_in_separate_sentences():
    body = make_detail('<p>6月25日，ABC株式会社在常州高新区项目完成签约。</p>'
                       '<p>ABC创立于1959年，位列日本优质企业300强。</p>')
    item = detail(body, candidate())
    signal = item['metadata']['business_signal']
    assert signal['event_type'] == 'project_signed'
    assert signal['entities'][0]['name'] == 'ABC株式会社'
    proof = next(f for f in signal['facts'] if f['field'] == 'japanese_affiliation')
    assert '日本优质企业' in proof['quote'] and proof['quote'] in item['text']


def test_japanese_company_event_in_japan_is_not_a_china_project():
    body = make_detail('<p>6月25日，日本株式会社青山制造在大阪的项目正式开工建设，'
                       '其产品未来将出口中国。</p>')
    assert detail(body, candidate()) is None


def test_two_rounds_preserve_frontier_and_do_not_duplicate(monkeypatch):
    rows = [('101', '日本项目开工'), ('102', '日本项目签约')]
    bodies = {
        '101': make_detail('<p>6月25日，日本株式会社晴峰材料在常州项目正式开工建设。项目位于高新区。</p>'),
        '102': make_detail('<p>6月25日，日本株式会社青山制造在常州项目正式开工建设。项目位于高新区。</p>'),
    }
    def fake_read(url, **kwargs):
        body = make_list(rows, more=False) if '/class/' in url else bodies[url.rsplit('/', 1)[-1][:-5]]
        return {'body': body, 'url': url}, {'url': url, 'sha256': 'sha', 'raw_path': 'raw/synthetic'}
    monkeypatch.setattr('collectors.zone_events.read', fake_read)
    src = SimpleNamespace(options={'head_pages': 1, 'max_accepted_items': 1}, checkpoint={})
    first = collect_changzhou_zone(src)
    src.checkpoint = first['collector_checkpoint']
    second = collect_changzhou_zone(src)
    src.checkpoint = second['collector_checkpoint']
    third = collect_changzhou_zone(src)
    assert [i['external_id'] for i in first['items'] + second['items']] == ['101', '102']
    assert third['items'] == []
    assert first['stats']['pending'] == 1 and second['stats']['pending'] == 0
    assert len(second['collector_checkpoint']['changzhou_zone_candidates_v1']['entries']) == 2


def test_failed_detail_stays_retryable_and_is_not_marked_excluded(monkeypatch):
    def fake_read(url, **kwargs):
        if '/class/' in url:
            return {'body': make_list([('101', '日本项目开工')], more=False), 'url': url}, {'url': url}
        raise ValueError('synthetic network failure')
    monkeypatch.setattr('collectors.zone_events.read', fake_read)
    src = SimpleNamespace(options={'head_pages': 1}, checkpoint={})
    result = collect_changzhou_zone(src)
    assert result['status'] == 'partial'
    assert result['collector_checkpoint']['changzhou_zone_candidates_v1']['entries']['101']['status'] == 'retry'
    assert result['stats']['error_count'] == 1


def test_research_import_contract_references_verified_source(monkeypatch):
    import hashlib
    body = make_detail('<p>6月25日，日本株式会社晴峰材料在常州项目正式开工建设。'
                       '项目位于高新区。</p>')
    digest = hashlib.sha256(body).hexdigest()
    def fake_read(url, **kwargs):
        payload = make_list([('101', '项目开工')], more=False) if '/class/' in url else body
        return {'body': payload, 'url': url}, {'url': url, 'sha256': digest, 'raw_path': 'raw/source_lists/test.html'}
    monkeypatch.setattr('collectors.zone_events.read', fake_read)
    src = SimpleNamespace(options={'head_pages': 1}, checkpoint={})
    record = collect_zone(src)['items'][0]
    assert record['external_id'] == 'cznd:101'
    assert record['document']['sha256'] == digest
    # A Japanese project company is not necessarily a proven parent company.
    assert record['research']['participants'][0]['role'] == 'japan_company'
    assert record['research']['china_scope'] == 'mainland'
    evidence = record['research']['china_evidence']
    assert '常州' in evidence['quote'] and evidence['quote'] in record['text']
    assert all(f['quote'] in record['text'] for f in record['research']['facts'])
