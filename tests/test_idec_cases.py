from types import SimpleNamespace

from collectors.idec_cases import INDEX, collect_idec


def test_id_extracted_from_current_index_and_historical_body(monkeypatch):
    url = INDEX.replace('list.html?cat_id=6', 'info.html?id=9876')
    index = ('<a href="supportcase_info.html?id=9876">海外ケース</a>'
             '<a href="https://example.net/support/supportcase_info.html?id=4">outside</a>')
    detail = '''<div class="support_info_header"><div class="name">株式会社架空【製造業】</div></div>
        <p class="section_body">上海へ製品を輸出した。2020年度支援事例。従業員数：37名。</p>
        <div class="entry_info support"><div class="address">横浜市</div></div>'''
    pages = {INDEX:index, url:detail}
    monkeypatch.setattr('collectors.idec_cases.read_original',
        lambda key: ({'body':pages[key].encode()}, {'url':key}))
    result = collect_idec(SimpleNamespace(url=INDEX))
    assert result['stats']['discovered'] == 1
    item, = result['items']
    assert item['url'] == url
    event = item['research']
    assert event['event_type'] == 'existing_presence'
    assert event['dates'][0]['value'] == '2020'
    assert event['participants'][0]['name'] == '株式会社架空'
    assert {f['field']:f['value'] for f in event['facts']}['employees'] == '37'
    pages[url] = detail.replace('上海へ製品を輸出した','中国による輸入規制があった')
    assert not collect_idec(SimpleNamespace(url=INDEX))['items']
