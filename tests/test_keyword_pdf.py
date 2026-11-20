import hashlib
from datetime import date
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from radar.keyword_dates import recent_publication
from radar.keyword_pdf import inspect_keyword_pdf, pdf_publication
from radar.models import AIConfig, KeywordCandidate
from radar.sales_pools import keyword_card
from radar.search_provider import SearchResult


def make_pdf(path, text=True):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    if text:
        font = DictionaryObject({NameObject('/Type'):NameObject('/Font'),
                                 NameObject('/Subtype'):NameObject('/Type1'),
                                 NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
            DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 72 720 Td (China factory expansion 2027) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(stream)
    with path.open('wb') as out:
        writer.write(out)


class KeywordPDFTests(TestCase):
    def test_saved_pdf_text_layer_and_missing_text(self):
        with TemporaryDirectory() as temp, override_settings(DATA_DIR=Path(temp)):
            raw = Path(temp,'raw','research');raw.mkdir(parents=True)
            for with_text in (True, False):
                temporary=raw/'input.pdf';make_pdf(temporary,with_text)
                digest=hashlib.sha256(temporary.read_bytes()).hexdigest()
                path=raw/(digest+'.pdf');temporary.rename(path)
                item=KeywordCandidate(canonical_url='https://example.org/test.pdf',title='x',
                                      provider='test',query_id='q',result={'source_sha256':digest})
                parsed=inspect_keyword_pdf(item)
                self.assertEqual(parsed['raw_path'],'raw/research/'+digest+'.pdf')
                self.assertEqual(parsed['status'], 'text' if with_text else 'no_text_layer')
                if with_text:self.assertIn('factory expansion',parsed['text'])

    def test_pdf_header_and_publication_window(self):
        info=pdf_publication(' 2026年２月12日\n株式会社青木')
        self.assertEqual(info['date'],'2026-02-12')
        self.assertEqual(info['quote'],'2026年２月12日')
        self.assertTrue(recent_publication(info['date'],date(2026,11,18)))
        self.assertFalse(recent_publication('2025-02-12',date(2026,11,18)))
        self.assertFalse(recent_publication('2026-12-12',date(2026,11,18)))
        self.assertEqual(pdf_publication('2016年 3月'),{})

    def test_saved_pdf_can_be_reviewed_without_network_or_model(self):
        with TemporaryDirectory() as temp, override_settings(DATA_DIR=Path(temp)):
            raw=Path(temp,'raw','research');raw.mkdir(parents=True)
            temporary=raw/'a.pdf';make_pdf(temporary,False)
            digest=hashlib.sha256(temporary.read_bytes()).hexdigest();temporary.rename(raw/(digest+'.pdf'))
            item=KeywordCandidate.objects.create(canonical_url='https://example.org/a.pdf',title='PDF',
                provider='tavily',query_id='q',status='needs_document_review',result={'source_sha256':digest})
            AIConfig.objects.create()
            with patch('radar.management.commands.process_keyword_pdfs.extract') as model, \
                 patch('radar.management.commands.process_keyword_pdfs.discover_official_site') as site:
                call_command('process_keyword_pdfs',output_dir=temp,stdout=StringIO())
            item.refresh_from_db()
            self.assertEqual(item.raw_path,'raw/research/'+digest+'.pdf')
            self.assertEqual(item.result['pdf_review']['skip_reason'],'no_text_layer')
            model.assert_not_called();site.assert_not_called()

    def test_pdf_discovery_keeps_the_raw_file_link(self):
        AIConfig.objects.create(search_daily_limit=5)
        class Provider:
            def search(self, query, query_id):
                return [SearchResult('日本企业中国新工厂建设','https://example.org/a.pdf',
                                     '株式会社青木新工場','','tavily',query_id)]
        with TemporaryDirectory() as temp, patch('radar.management.commands.keyword_radar.provider_for',return_value=Provider()), \
             patch('radar.management.commands.keyword_radar.read_original',return_value=(
                 {'url':'https://example.org/a.pdf','body':b'%PDF-1.7'},
                 {'raw_path':'raw/research/a.pdf','sha256':'a'*64})):
            call_command('keyword_radar',online=True,provider='tavily',max_search=1,
                         search_daily_limit=5,max_details=1,output_dir=temp,stdout=StringIO())
        item=KeywordCandidate.objects.get()
        self.assertEqual(item.status,'needs_document_review')
        self.assertEqual(item.raw_path,'raw/research/a.pdf')

    def test_company_pdf_uses_supported_future_date_and_city(self):
        url='https://company.example/a.pdf'
        quote='株式会社青木は中国北京市に新工場を設立します。'
        text='2026年２月12日\n'+quote+'\n(５) 操 業 開 始 予 定 2027年６月'
        output={'company_name':'株式会社青木','is_japanese_affiliated':True,'japanese_evidence':quote,
                'size_tier':'unknown','event_type':'plant_expansion','china_location':'',
                'target_period':'2027-06','evidence_quote':quote,'source_url':url}
        item=KeywordCandidate(canonical_url=url,title='中国工場',provider='tavily',query_id='q',text=text,
            raw_path='raw/research/document.pdf',status='model_needs_review',result={'extraction':output,'pdf_review':{'status':'text'},
                'source_publication':{'date':'2026-02-12','field':'pdf:first_page','page':1,'quote':'2026年２月12日'},
                'website_candidate':{'status':'name_matched_ownership_unverified','company':'株式会社青木',
                    'url':'https://company.example/','quote':'株式会社青木','raw_path':'raw/research/site.html','sha256':'a'*64}})
        with patch('radar.sales_pools.timezone.localdate',return_value=date(2026,11,18)):
            card=keyword_card(item)
            self.assertEqual((card['china_location'],card['target_period']),('北京市','2027-06'))
            self.assertEqual(card['source_publication_date'],'2026-02-12')
            self.assertIn('2027年６月',card['target_period_evidence'])
            item.result['website_candidate']['url']='https://other.example/'
            self.assertIsNone(keyword_card(item))
