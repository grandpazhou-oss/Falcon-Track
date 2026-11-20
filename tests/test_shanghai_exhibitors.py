import pytest
from lxml import html
from collectors.shanghai_exhibitors import fhc_rows, osaka_rows

DOC = {'url':'https://example.org/roster', 'sha256':'fake', 'raw_path':'fake'}


def test_osaka_keeps_product_with_its_company_and_requires_venue():
    page = '<p>〇日時：2026年11月5日</p><p>〇場所：上海</p><p>大阪市ブース出展企業</p><p>・例株式会社（医薬機械）</p><p>終了</p><p>・別会社（別品）</p>'
    items = osaka_rows(html.fromstring(page), DOC)
    assert len(items) == 1 and items[0]['research']['facts'][0]['value'] == '医薬機械'
    with pytest.raises(ValueError):
        osaka_rows(html.fromstring(page.replace('上海', '大阪')), DOC)


def test_fhc_requires_row_level_region_and_excludes_organizers():
    def row(name, region):
        return f'<div><div class="_name">{name}</div><div><div>B2</div></div><div><div>{region}</div></div><div><div>食品</div></div></div>'
    page = '<title>FHC上海</title><div class="fhc-exhibitor-list">' + row('示例甲','日本') + row('示例乙','中国大陆') + row('示例代表处','日本') + '</div>'
    items = fhc_rows(html.fromstring(page), DOC)
    assert [i['research']['participants'][0]['name'] for i in items] == ['示例甲']
    assert items[0]['research']['participants'][0]['role'] == 'listed_exhibitor'
    assert items[0]['external_id'] == fhc_rows(html.fromstring(page), DOC)[0]['external_id']


def test_fbc_uses_edition_from_page():
    from collectors.shanghai_exhibitors import fbc_rows
    page = ('<div class="active-detail-content">FBC上海2028 in PTC<section>'
            '<p><strong>上海样例有限公司</strong></p><p>总公司在日本。</p>'
            '<p>SELL 工业品 BUY 零部件</p></section></div>')
    item, = fbc_rows(html.fromstring(page), DOC)
    assert '2028 上海 FBC' in item['title']
    assert '2028' in item['research']['china_evidence']['quote']
