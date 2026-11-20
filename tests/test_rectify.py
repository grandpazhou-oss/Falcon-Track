import pytest
import pymupdf

from radar import pdf_extract
from radar.project_grade import qualify


def test_japanese_real_pdf_keeps_physical_pages(tmp_path):
    path=tmp_path/'jp.pdf'
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.new_page().insert_text((40,60),'広州工場 生産拠点',fontname='japan')
        pdf.new_page().insert_text((40,60),'2027年4月 稼働開始',fontname='japan')
        pdf.save(path)
    result=pdf_extract.extract(path)
    assert [p['page'] for p in result['pages']]==[1,2,3]
    assert result['pages'][0]['status']=='ocr_required'
    assert '広州工場' in result['pages'][1]['text']
    assert '2027年' in result['pages'][2]['text']


def test_pdf_falls_back_to_pypdf(tmp_path,monkeypatch):
    from pypdf import PdfWriter

    src=tmp_path/'backup.pdf'
    writer=PdfWriter()
    writer.add_blank_page(80,80)
    writer.write(src)
    def not_loaded(*args):
        raise ImportError('synthetic unavailable backend')
    monkeypatch.setattr(pdf_extract,'extract_pymupdf',not_loaded)
    assert pdf_extract.extract(src)['backend']=='pypdf'
    assert pdf_extract.extract(src)['pages'][0]['page']==1


@pytest.mark.parametrize('quote,timing,verified,expected',[
    ('広州工場を拡張','2027年4月 稼働予定',True,True),
    ('広州工場を拡張','2027年4月期 決算資料',True,False),
    ('広州工場を拡張','2027年4月 稼働予定',False,False),
    ('広州でN7を輸出','2027年4月 出荷',True,False),
    ('広州工場の稼働率は95%','2027年4月',True,False),
])
def test_grade_gate_requires_event_and_original(quote,timing,verified,expected):
    assert bool(qualify('広州','2027-04',quote,timing,source_verified=verified))==expected

def test_issuer_html_parses_dated_local_plant():
    from collectors.issuer_ir_pdf import parse_html_disclosure
    from django.utils import timezone

    result=parse_html_disclosure({
        'title':'広州工場生産拠点の新設のお知らせ',
        'published':timezone.localdate().isoformat(),
        'text':'会社名：例示精密株式会社\n中国広州の新工場は2027年4月に稼働予定です。',
    })
    assert result['company']=='例示精密株式会社'
    assert result['target_period']=='2027-04'
