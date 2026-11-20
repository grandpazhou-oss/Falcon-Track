"""Public Osaka workbook: roles, region, evidence and safe bounded parsing."""
import io
import zipfile
from xml.sax.saxutils import escape
import pytest
from collectors.osaka_sme import parse_workbook,discover_workbook


def sample_book(*, style='', formula=False, workbook_target='worksheets/sheet2.xml',sheet_name='工具企業(天津)',note=''):
    ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rns='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    wb=f'<workbook xmlns="{ns}" xmlns:r="{rns}"><sheets><sheet name="中国編目次" sheetId="1" r:id="rId1"/><sheet name="{escape(sheet_name)}" sheetId="2" r:id="rId2"/><sheet name="工具企業(香港)" sheetId="3" r:id="rId3"/></sheets></workbook>'
    rel=f'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="x"/><Relationship Id="rId2" Target="{workbook_target}" Type="x"/><Relationship Id="rId3" Target="worksheets/sheet3.xml" Type="x"/></Relationships>'''
    def sheet(cells):
        rows=''.join(f'<row r="{i}"><c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c></row>' for i,(ref,value) in enumerate(cells,1))
        return f'<worksheet xmlns="{ns}"><sheetData>{rows}{"<row r=\"99\"><c r=\"B20\"><f>1+2</f><v>3</v></c></row>" if formula else ""}</sheetData><mergeCells count="1"><mergeCell ref="B3:G3"/></mergeCells>{style}</worksheet>'
    common=[('A3','本社企業名'),('B3','株式会社サンプル'),('A10','現地法人・拠点名'),('A11','所在地'),('A15','進出形態'),('A18','取扱製品')]
    china=common+[('A8','海外法人・海外拠点内容（□製造 ■販売代理店）'),('B10','天津样本贸易有限公司'),('B11','天津市南开区'),('B15','□合弁 □独資 ■その他（委託代理店）'),('B18','手工具')]+([('G26',note)] if note else [])
    hongkong=common+[('A8','海外法人・海外拠点内容（■製造 □販売）'),('B10','香港样本有限公司'),('B11','香港新界'),('B15','□合弁 ■独資 □その他'),('B18','精密部品')]
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('xl/workbook.xml',wb);z.writestr('xl/_rels/workbook.xml.rels',rel)
        z.writestr('xl/worksheets/sheet1.xml',f'<worksheet xmlns="{ns}"/>')
        z.writestr('xl/worksheets/sheet2.xml',sheet(china));z.writestr('xl/worksheets/sheet3.xml',sheet(hongkong))
    return out.getvalue()


def test_workbook_preserves_proxy_relation_and_hong_kong_separately():
    result=parse_workbook(sample_book())
    assert len(result['records'])==2 and result['diagnostics']['sheet_count']==3
    mainland,hongkong=result['records']
    assert mainland['parent_name']=='株式会社サンプル' and mainland['site_name']=='天津样本贸易有限公司'
    assert mainland['relation_type']=='distributor' and mainland['region_class']=='mainland_china'
    assert hongkong['relation_type']=='owned_subsidiary' and hongkong['region_class']=='hong_kong'
    assert mainland['operational_roles']==['distributor'] and hongkong['operational_roles']==['manufacturing']
    assert mainland['facts']['site_name']=={'cell':'B10','quote':'天津样本贸易有限公司'}
    assert mainland['event_date'] is None and mainland['size_evidence']=='official_list_self_report'
    assert result['diagnostics']['merge_count']==2


def test_formula_not_evaluated_and_formatting_does_not_change_business_hash():
    baseline=parse_workbook(sample_book())['records'][0]
    formatted=parse_workbook(sample_book(style='<sheetPr/>'))['records'][0]
    assert baseline['business_hash']==formatted['business_hash']
    renamed=parse_workbook(sample_book(sheet_name='工具别名(天津)'))['records'][0]
    assert baseline['record_key']==renamed['record_key'] and baseline['business_hash']==renamed['business_hash']
    noted=parse_workbook(sample_book(note='2026/03 確認'))['records'][0]
    assert baseline['business_hash']==noted['business_hash'] and noted['source_notes'][0]['cell']=='G26'
    result=parse_workbook(sample_book(formula=True))
    assert result['diagnostics']['ignored_formula_count']==2
    assert result['records'][0]['business_description']==''


def test_workbook_rejects_unsafe_path_and_oversized_uncompressed_payload():
    with pytest.raises(ValueError,match='unsafe'):
        parse_workbook(sample_book(workbook_target='../../../../etc/passwd'))
    with pytest.raises(ValueError,match='size'):
        parse_workbook(sample_book(),max_uncompressed_bytes=256)


def test_workbook_rejects_macro_payload():
    out=io.BytesIO(sample_book())
    with zipfile.ZipFile(out,'a') as archive:archive.writestr('xl/vbaProject.bin',b'fake-macro')
    with pytest.raises(ValueError,match='macro'):parse_workbook(out.getvalue())


def test_discovery_requires_osaka_government_https_workbook():
    assert discover_workbook(b'<a href="/documents/7030/kaigaikyoten_china_2603.xlsx">China</a>') == 'https://www.pref.osaka.lg.jp/documents/7030/kaigaikyoten_china_2603.xlsx'
    with pytest.raises(ValueError):discover_workbook(b'<a href="https://evil.example/kaigaikyoten_china_2603.xlsx">China</a>')


def test_index_update_date_is_not_an_establishment_event():
    from collectors.osaka_sme import _index_update
    body='<html><head><meta charset="utf-8"></head><p id="tmp_update">更新日：2026年3月30日</p></html>'.encode()
    assert _index_update(body)==('2026-03-30','更新日：2026年3月30日')
    assert parse_workbook(sample_book())['records'][0]['event_date'] is None


def test_japan_chugoku_region_never_counts_as_china():
    from collectors.osaka_sme import _region
    assert _region('中国地方の商品センター','日本 岡山県岡山市')=='japan'


def test_owner_relation_and_distribution_role_can_coexist():
    from collectors.osaka_sme import _relation,_activities
    shape='□合弁 ■独資 □その他';activity='□製造 ■販売代理店'
    assert _relation(shape,activity)=='owned_subsidiary' and _activities(activity)==['distributor']


def test_phonetic_ruby_is_not_appended_to_legal_name():
    from defusedxml import ElementTree as ET
    from collectors.osaka_sme import _string
    node=ET.fromstring('<si xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><t>株式会社</t><r><t>サンプル</t></r><rPh><t>カブシキガイシャ</t></rPh></si>')
    assert _string(node)=='株式会社サンプル'


def test_unchanged_workbook_does_not_reprocess_after_commit(tmp_path,settings,monkeypatch):
    from collectors.osaka_sme import collect_directory
    settings.DATA_DIR=tmp_path
    index=b'<html><a href="/documents/7030/kaigaikyoten_china_2603.xlsx">file</a></html>'
    calls=[]
    def fake_fetch(url,**kwargs):
        calls.append(url)
        return {'url':url,'body':sample_book() if url.endswith('.xlsx') else index,'robots_redirect_chains':[],'redirect_chain':[]}
    monkeypatch.setattr('collectors.osaka_sme.fetch',fake_fetch)
    first=collect_directory({})
    assert first['status']=='parsed' and len(first['records'])==2
    assert all((tmp_path/x['raw_path']).is_file() for x in first['evidence']['requests'])
    again=collect_directory({'file_hash':first['evidence']['file_hash'],'completed':True})
    assert again['status']=='unchanged' and again['records']==[] and len(calls)==4
    # Interrupted persistence is replayable even if the file hash is known.
    interrupted=collect_directory({'file_hash':first['evidence']['file_hash'],'completed':False})
    assert interrupted['status']=='parsed' and len(interrupted['records'])==2
