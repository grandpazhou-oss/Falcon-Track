"""MAFF export plan parsing boundaries; no network or business database writes."""
from collectors.maff_exports import parse_maff_index,parse_maff_plan

URL='https://www.maff.go.jp/j/shokusan/export/gfp/yusyutsu_keikaku_kohyo.html'
PDF='https://www.maff.go.jp/j/shokusan/export/gfp/attach/pdf/yusyutsu_keikaku_kohyo-540.pdf'
ENTRY={'id':'540','url':PDF,'title':'【菓子】福岡県：株式会社しんこう（令和5年度）',
       'applicant':'株式会社しんこう','product':'菓子','source_fiscal_year':'令和5年度',
       'list_url':URL,'list_quote':'【菓子】福岡県：株式会社しんこう（令和5年度）'}


def test_index_merges_duplicate_urls_without_conflating_distinct_plans():
    html='''<html><meta charset="utf-8"><a href="./attach/pdf/yusyutsu_keikaku_kohyo-540.pdf">【菓子】福岡県：株式会社しんこう（令和5年度）(PDF : 348KB)</a>
    <a href="./attach/pdf/yusyutsu_keikaku_kohyo-540.pdf">【加工食品】福岡県：株式会社しんこう（令和5年度）</a>
    <a href="./attach/pdf/yusyutsu_keikaku_kohyo-541.pdf">【菓子】福岡県：株式会社しんこう（令和4年度）</a>
    <a href="https://evil.example/yusyutsu_keikaku_kohyo-542.pdf">【菓子】株式会社悪意</a></html>'''.encode()
    records=parse_maff_index(html,URL)
    assert [x['id'] for x in records]==['540','541']
    assert records[0]['applicant']=='株式会社しんこう' and records[0]['product']=='菓子'
    assert records[0]['source_fiscal_year']=='令和5年度' and len(records[0]['list_quotes'])==2


def test_china_export_plan_retains_multi_market_values_as_aggregate():
    text='※申請者名：株式会社しんこう、品目：菓子（揚菓子、焼菓子）\n中国本土の商社との合同商談を進め、中国市場への輸出を図る。\n輸出額（千円） 23,044 180,000\n輸出量（ｔ） 22.1トン 154.5トン\n輸出先国 中国、韓国、台湾、香港'
    result=parse_maff_plan([{'page':2,'text':text,'status':'text'}],ENTRY,'a'*64)
    assert result['accepted'] and result['company_name']=='株式会社しんこう'
    assert result['event_type']=='export_plan' and result['china_scope']=='mainland'
    assert set(result['fields']['markets'])=={'china_mainland','korea','taiwan','hong_kong'}
    assert result['fields']['scale']=='unknown' and result['fields']['china_entity_name']==''
    assert all(x['market_scope']=='multiple_markets_unallocated' for x in result['fields']['metrics'])
    assert result['fields']['source_fiscal_year']=='令和5年度' and not result['fields']['event_date']
    assert all(e['quote'] in text for e in result['evidence'] if e.get('page'))


def test_hong_kong_taiwan_and_japan_chugoku_are_not_china_mainland():
    for body in ['輸出先国：香港、台湾','中国香港向けに輸出する',
                 '輸出先国：中国（香港）、台湾','中国（台湾）向けに輸出する',
                 '広島県の中国地方にある工場で生産する']:
        result=parse_maff_plan([{'page':1,'text':'申請者名：株式会社テスト\n'+body,'status':'text'}],ENTRY,'a'*64)
        assert not result['accepted'] and result['reason']=='no_mainland_market_evidence'


def test_china_supplier_origin_is_not_a_target_market():
    for body in ['申請者名：株式会社テスト\n中国産原材料を日本に輸入する。輸出先国：フランス、スイス。',
                 '申請者名：株式会社テスト\n中国本土の工場から材料を輸入し、日本で製造する。',
                 '申請者名：株式会社テスト\n中国市場で原材料を購入し、日本で製造してフランスへ輸出する。']:
        result=parse_maff_plan([{'page':1,'text':body,'status':'text'}],ENTRY,'a'*64)
        assert not result['accepted'] and result['reason']=='no_mainland_market_evidence'


def test_named_china_importer_is_a_role_not_a_china_subsidiary():
    body='申請者名：株式会社しんこう\n中国向け輸出を拡大。輸入業者：上海能隆食品有限公司\n輸出先国：中国、台湾'
    result=parse_maff_plan([{'page':1,'text':body,'status':'text'}],ENTRY,'a'*64)
    assert result['accepted']
    assert result['fields']['relationships']==[{'name':'上海能隆食品有限公司','role':'importer','market_scope':'unknown','page':1,'quote':'輸入業者：上海能隆食品有限公司'}]
    assert result['fields']['china_entity_name']==''


def test_market_explicitly_assigned_to_china_requires_explicit_role_label():
    for label,expected in [('中国本土輸入業者','china_mainland'),('中国側輸入業者','china_unspecified'),
                           ('香港輸入業者','hong_kong'),('台湾輸入業者','taiwan')]:
        body='申請者名：株式会社しんこう\n中国向け輸出を拡大。'+label+'：上海能隆食品有限公司'
        result=parse_maff_plan([{'page':1,'text':body,'status':'text'}],ENTRY,'a'*64)
        assert result['fields']['relationships'][0]['market_scope']==expected


def test_sourcing_in_china_does_not_hide_a_separate_explicit_china_destination():
    for body in ['中国市場で原材料を購入。中国向けに菓子を輸出する。',
                 '中国市場で原材料を購入。輸出先国：中国、香港。']:
        result=parse_maff_plan([{'page':1,'text':body,'status':'text'}],ENTRY,'a'*64)
        assert result['accepted']


def test_market_exit_is_not_promoted_to_an_export_plan():
    for body in ['中国市場から撤退。輸出先国：フランス。',
                 '中国向け輸出は既に終了した。フランスへ輸出する。',
                 '中国への輸出は行わない。']:
        result=parse_maff_plan([{'page':1,'text':body,'status':'text'}],ENTRY,'a'*64)
        assert not result['accepted'] and result['reason']=='no_mainland_market_evidence'
    result=parse_maff_plan([{'page':1,'text':'中国市場から撤退。新計画は中国向けに輸出する。','status':'text'}],ENTRY,'a'*64)
    assert result['accepted']


def test_agents_are_not_promoted_to_importers():
    body=('中国向け輸出を拡大。中国側代理店：上海能隆食品有限公司\n'
          '中国本土輸入代理店：大連栄乾国際貿易有限公司\n'
          '原料代理店：株式会社テスト')
    result=parse_maff_plan([{'page':1,'text':body,'status':'text'}],ENTRY,'a'*64)
    assert [(x['role'],x['market_scope']) for x in result['fields']['relationships']]==[
        ('agent','china_unspecified'),('import_agent','china_mainland')]


def test_market_facts_separate_current_and_future_when_explicit():
    body='申請者名：株式会社しんこう\n現在、中国向けに輸出しており、今年の売上は不明。\n今後、中国市場向けの販売拡大を目指す。'
    result=parse_maff_plan([{'page':1,'text':body,'status':'text'}],ENTRY,'a'*64)
    assert result['accepted']
    assert [x['role'] for x in result['fields']['market_mentions']]==['existing','target']
    assert not result['fields']['event_date'] and not result['fields']['planned_date']


def test_unreadable_scanned_page_is_not_misreported_as_no_china():
    result=parse_maff_plan([{'page':1,'text':'','status':'ocr_required'}],ENTRY,'a'*64)
    assert not result['accepted'] and result['reason']=='text_incomplete_manual_review'


def test_unprocessed_pdf_pages_are_not_misreported_as_no_china():
    pages=[{'page':1,'text':'輸出先国：フランス','status':'text'}]
    result=parse_maff_plan(pages,ENTRY,'a'*64,total_pages=2)
    assert not result['accepted'] and result['reason']=='text_incomplete_manual_review'
    assert result['fields']['pdf_total_pages']==2


def test_index_only_company_is_retained_as_source_fact_not_employer_inferred():
    text='中国市場向けに輸出を拡大する。'
    result=parse_maff_plan([{'page':1,'text':text,'status':'text'}],ENTRY,'a'*64)
    assert result['accepted'] and result['company_name']=='株式会社しんこう'
    assert result['fields']['scale']=='unknown' and not result['fields']['china_entity_name']
    assert result['fields']['source_fiscal_year']=='令和5年度'


def test_repeated_category_link_does_not_make_a_second_plan_identity():
    from collectors.maff_source import plan_identity
    plan={'company_name':'株式会社しんこう','fields':{'product':'菓子','source_fiscal_year':'令和5年度'}}
    pages=[{'text':'中国向け 菓子\n輸出を計画。'}]
    assert plan_identity(plan,pages)==plan_identity(plan,[{'text':'中国向け  菓子 \n 輸出を計画。'}])
    assert plan_identity(plan,pages)!=plan_identity(plan,[{'text':'中国向け菓子の計画を変更。'}])


def test_bridge_preserves_historical_versus_target_market_statements():
    from collectors.maff_source import record_from_plan
    entry={**ENTRY,'list_hash':'b'*64}
    text='申請者名：株式会社しんこう\n現在、中国向けに輸出しており、今後、中国市場向けの販売拡大を目指す。'
    pages=[{'page':1,'text':text,'status':'text'}]
    outcome=parse_maff_plan(pages,entry,'a'*64,total_pages=1)
    record=record_from_plan(entry,outcome,pages,{'sha256':'a'*64,'raw_path':'raw/p.pdf'},
             {'sha256':'b'*64,'raw_path':'raw/index.html','url':URL},entry['list_quote'])
    market=[f for f in record['research']['facts'] if f['field']=='market_stage']
    assert market and market[0]['value']=='mixed_unallocated'
