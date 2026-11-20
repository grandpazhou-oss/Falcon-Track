from datetime import date
from lxml import html
from collectors.japan_channels import TOKYO, JETRO, PRESS, parse, discover

def test_cases_need_direction_and_entity():
    def case(sentence):
        return html.fromstring('<main><h1>甲株式会社</h1><p>'+sentence+'</p></main>')
    today = date(2026,11,18)
    assert not parse(TOKYO,case('中国からベトナムに移転'), '甲株式会社',2024,today)
    a = parse(TOKYO,case('当社は中国サイトからの依頼で販売を継続'), '甲株式会社',2022,today)
    assert '2022' in a['title'] and not a['research']['dates']
    assert not parse(JETRO,case('中国での販売量は伸びた'), '乙株式会社',2026,today)

def test_press_requires_named_dated_operating_action():
    def news(sentence):
        return html.fromstring('<article><div id="press-release-body"><p>'+sentence+'</p></div></article>')
    today = date(2026,11,18)
    s = '株式会社甲（本社：東京都港区）は2026年11月30日、中国・海南島で店舗をオープンいたします。'
    result = parse(PRESS,news(s),'',0,today)['research']
    assert result['dates'][0]['value']=='2026-11-30'
    assert result['participants'][0]['name']=='株式会社甲'
    assert result['size']['value']=='unknown'
    assert not parse(PRESS,news(s.replace('中国・海南島','台湾')),'',0,today)

def test_jetro_country_tag_alone_does_not_qualify():
    tree=html.fromstring('<ul><li><input value="cn"><a href="/case_study/2026/a.html"><p class="title">A</p>台湾市場を開拓</a></li></ul>')
    url, name, year = next(discover(JETRO,tree,'https://www.jetro.go.jp',2026))
    assert url.endswith('/case_study/2026/a.html')
    assert parse(JETRO, html.fromstring('<main><h1>A</h1><p>他市場の記事</p></main>'), name, year, date(2026,11,19)) is None

def test_group_publisher_and_local_store_operator_are_distinct():
    sentence = ('株式会社親のグループ会社である、株式会社甲（代表取締役：乙、本社所在地：東京都渋谷区）の'
                '子会社「悠艾（上海）商貿有限公司（SHANGHAI LTD.）」は、2026年11月、中国・上海に新店舗をオープンします。')
    tree = html.fromstring('<article><div id="press-release-body"><p>'+sentence+'</p></div></article>')
    event = parse(PRESS,tree,'',0,date(2026,11,18))['research']
    assert event['participants'][0]['name'] == '株式会社甲'
    assert event['dates'][0]['precision'] == 'month'
    assert event['dates'][0]['value'] == '2026-11'
    assert next(f['value'] for f in event['facts'] if f['field']=='site_operator') == '悠艾（上海）商貿有限公司'


def test_press_classifies_non_retail_actions():
    from collectors.japan_channels import PRESS, parse
    for action, kind in [('新工場の建設を開始', 'construction_started'),
                         ('工場を増設', 'expansion'),
                         ('物流センターを新設', 'relocation'),
                         ('現地法人を設立', 'entity_established')]:
        tree = html.fromstring('<div id="press-release-body"><p>'
            f'株式会社架空（本社：東京都）は2026年11月5日に上海で{action}しました。'
            '</p></div>')
        assert parse(PRESS, tree, '', 0, date(2026, 11, 19))['research']['event_type'] == kind
