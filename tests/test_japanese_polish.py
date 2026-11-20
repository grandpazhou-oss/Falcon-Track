import pytest
from django.template.loader import render_to_string
from lxml.html import fromstring

from radar.brief_translation import normalize_brief, validate_translation
from radar.japanese_brief import language_issue
from radar.pro_brief import validate_brief
from radar.sales_ui import display_card
from tests.test_pro_brief_r3 import sample_brief, sample_fact


@pytest.mark.parametrize('text',[
    '入場日を確認します。', '危険品特種物流を提案します。', '中国区の担当者',
    '２０２７年春、１５分', '状況を伺いできれば幸いです。',
    '記載済みの対応力で支えられる範囲', '業務範囲で未記入の部分は確認に留める',
    '業務範囲の未確認部分は確認事項として残す。',
])
def test_japanese_quality_check_rejects_unpolished_text(text):
    assert language_issue(text)
    with pytest.raises(ValueError,match='japanese_wording'):
        validate_translation('原文',text,'ja')


def test_translation_repairs_unambiguous_words_and_discards_form_sentence():
    text = ('中国区での入場ウィンドウは２０２７年春です。危険品特種物流、３ＰＬと入場配送に対応します。'
            '１５分ほど現状を伺いできれば幸いです。業務範囲で未記入の部分は確認に留める。')
    final=normalize_brief(text,'ja')
    assert final==('中国拠点での提案の好機は2027年春です。危険物物流、3PLと工場への納入配送に対応します。'
                   '15分ほど現状をお伺いできれば幸いです。')
    validate_translation(text,final,'ja')


def test_pro_rejects_ambiguous_entry_words_without_erasing_valid_questions():
    data=sample_brief()
    data['discovery_questions']=['入場番号を確認できますか？','工場への納入はいつですか？','１５分ほど現状を伺いできれば幸いです。']
    data['entry_strategy']['pitch_angle']='記載済みの対応力で支えられる範囲です。工場納入の体制をご相談します。'
    result,issues=validate_brief(data,[sample_fact()],language='ja')
    assert result['discovery_questions']==['工場への納入はいつですか？','15分ほど現状をお伺いできれば幸いです。']
    assert result['entry_strategy']['pitch_angle']=='工場納入の体制をご相談します。'
    assert {item['reason'] for item in issues}=={'日语术语、敬语或数字格式需要修改','营业文案含表单状态'}


def test_three_pools_fallback_labels_and_jetro_display():
    page=render_to_string('radar/sales_pool_cards.html',{'ui_lang':'ja','cards':[{}]})
    text=fromstring(page).text_content()
    assert '待核' not in text and text.count('要確認')>=6
    assert display_card({'provider_key':'jetro_biznews'})['ui_source']=='JETRO 商务短信'


@pytest.mark.django_db
def test_settings_table_wrappers_render_in_both_languages(client):
    for lang in ('zh','ja'):
        client.post('/language/',{'language':lang})
        response=client.get('/ai/settings/')
        assert response.status_code==200
        page=fromstring(response.content)
        tables=page.xpath('//table[contains(concat(" ",normalize-space(@class)," ")," stats-table ")]')
        assert len(tables)==6
        assert all(t.xpath('parent::div[contains(concat(" ",normalize-space(@class)," ")," table-wrap ")]') for t in tables)
        if lang=='ja':
            assert '有望率' in page.text_content()
