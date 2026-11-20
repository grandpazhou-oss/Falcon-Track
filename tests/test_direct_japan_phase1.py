from datetime import date
from unittest.mock import patch
from django.test import TestCase

from collectors.keyword_radar import collect, coarse_screen
from radar.keyword_extraction import validate_result
from radar.models import KeywordCandidate, SearchCall
from radar.search_provider import DirectJapanFeedProvider, parse_feed

RDF = '''<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<item><title>上海に新工場を設立</title><link>https://example.org/china</link>
<description>日本企業の工場拡張</description><dc:date>2026-11-17T09:00:00+09:00</dc:date></item>
<item><title>日本国内の新商品</title><link>https://example.org/domestic</link><description>食品</description></item>
</rdf:RDF>'''.encode()

ATOM = '''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<title>蘇州に新設</title><link rel="alternate" href="https://example.org/atom"/>
<summary>工場</summary><updated>2026-11-17T09:00:00Z</updated></entry></feed>'''.encode()

RSS = b'''<rss><channel><item><title>Shanghai</title><link>https://example.org/rss</link>
<description>warehouse</description><pubDate>Mon, 16 Nov 2026 02:00:00 GMT</pubDate></item></channel></rss>'''


class FeedTests(TestCase):
    def test_rdf_atom_and_rss_namespaces(self):
        for body, expected in [(RDF,'/china'), (ATOM,'/atom'), (RSS,'/rss')]:
            with self.subTest(expected=expected):
                rows, count = parse_feed(body, 'test', 'q')
                self.assertGreater(count, 0)
                self.assertTrue(rows[0].url.endswith(expected))
                self.assertTrue(rows[0].published_at.startswith('2026-11-'))
        self.assertEqual(parse_feed(RDF, 'test', 'q')[1], 2)

    def test_local_filter_keeps_low_signal_in_intake(self):
        response = {'body':RDF, 'url':'https://prtimes.jp/index.rdf'}
        evidence = {'raw_path':'raw/research/feed.html', 'sha256':'a'*64,
                    'redirect_chain':[], 'robots_redirect_chains':[]}
        with patch('collectors.research_common.read_original', return_value=(response,evidence)):
            result = collect(DirectJapanFeedProvider('prtimes'), 'feed', 'q', 5)
        self.assertEqual(result['raw_items'], 2)
        self.assertEqual(result['local_matches'], 1)
        self.assertEqual(result['added'], 2)
        self.assertTrue(KeywordCandidate.objects.filter(canonical_url='https://example.org/domestic').exists())
        self.assertEqual(SearchCall.objects.count(), 1)

    def test_listing_filters_before_database(self):
        body = '''<html><body><h3><a href="/main/html/rd/p/1.html">上海との業務提携</a></h3>
          <h3><a href="/main/html/rd/p/2.html">北海道の飲食店</a></h3></body></html>'''.encode()
        response = {'body':body,'url':'https://prtimes.jp/main/html/searchrlp/release_type_id/04'}
        evidence = {'raw_path':'raw/research/feed.html','sha256':'b'*64,
                    'redirect_chain':[],'robots_redirect_chains':[]}
        with patch('collectors.research_common.read_original', return_value=(response,evidence)):
            result = collect(DirectJapanFeedProvider('prtimes_management'), 'feed', 'q', 5)
        self.assertEqual(result['raw_items'], 2)
        self.assertEqual(result['local_matches'], 1)
        self.assertEqual(coarse_screen('上海との業務提携', '上海への投資を検討'), '')

    def test_unsafe_xml_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_feed(b'<!DOCTYPE x [<!ENTITY e "x">]><rss/>','test','q')

    def test_shenzhen_and_product_noise_are_distinguished(self):
        self.assertEqual(coarse_screen('深圳新设仓库', '有限公司在深圳建设物流仓库。'), '')
        self.assertEqual(coarse_screen('SSD拡張ハブの新製品', '深圳市有限公司发布存储产品。'), 'consumer_product')


class NormalizationTests(TestCase):
    def base(self):
        return {'company_name':'株式会社青木製作所','is_japanese_affiliated':True,
                'japanese_evidence':'日本企業','size_tier':'unknown','event_type':'plant_expansion',
                'china_location':'昆山','target_period':'2026',
                'evidence_quote':'株式会社青木製作所は日本企業として昆山に新工場を建設。',
                'source_url':'https://example.org/a'}

    def test_whitespace_and_legal_abbreviation_yield_original_quote(self):
        source = '（株）青木\n製作所は日本\n企業として昆山に新工場を建設。2026年'
        result = validate_result(self.base(), source, 'https://example.org/a', date(2026,11,17))
        self.assertEqual(result['evidence_quote'], '（株）青木\n製作所は日本\n企業として昆山に新工場を建設。')
        self.assertEqual(result['japanese_evidence'], '日本\n企業')
        self.assertEqual(result['china_location'], '昆山')
        self.assertIn(result['evidence_quote'], source)

    def test_expanded_city_still_requires_actual_source(self):
        data = self.base()
        source = '株式会社青木製作所は日本企業として昆山に新工場を建設。2026年'
        data['china_location'] = '南京'
        self.assertEqual(validate_result(data, source, 'https://example.org/a')['china_location'], '')
        data['china_location'] = '昆山设立'
        self.assertEqual(validate_result(data, source, 'https://example.org/a')['china_location'], '')

    def test_normalization_cannot_fabricate_a_company(self):
        data = self.base()
        source = '株式会社青木製作所は日本企業として昆山に新工場を建設。2026年'
        data['company_name'] = '株式会社別会社'
        with self.assertRaisesRegex(ValueError, 'unsupported_company'):
            validate_result(data, source, 'https://example.org/a')

class DirectGatingTests(TestCase):
    def test_200_item_rdf_is_not_truncated_at_50(self):
        items = ''.join(f'<item><title>中国に工場設立 {i}</title><link>https://example.org/{i}</link></item>'
                        for i in range(200))
        body = ('<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
                'xmlns="http://purl.org/rss/1.0/">' + items + '</rdf:RDF>').encode()
        rows, raw_count = parse_feed(body, 'test', 'q')
        self.assertEqual(raw_count, 200)
        self.assertEqual(len(rows), 200)
        self.assertTrue(rows[-1].url.endswith('/199'))

    def test_policy_without_named_company_never_reaches_model(self):
        from io import StringIO
        from tempfile import TemporaryDirectory
        from django.core.management import call_command
        from radar.models import AIConfig
        AIConfig.objects.create(search_daily_limit=5)
        item = KeywordCandidate.objects.create(canonical_url='https://example.org/policy',
            title='上海市の工場政策', provider='jetro_biznews', query_id='old')
        body='<html><body>上海市は工場の政策を発表した。</body></html>'.encode()
        response={'url':item.canonical_url,'body':body}
        evidence={'url':item.canonical_url,'sha256':'a'*64,'raw_path':'raw/research/a.html'}
        class Provider:
            def search(self, query, query_id):
                return []
        with TemporaryDirectory() as tmp, patch('radar.management.commands.keyword_radar.provider_for', return_value=Provider()), \
             patch('radar.management.commands.keyword_radar.read_original',return_value=(response,evidence)), \
             patch('radar.management.commands.keyword_radar.extract',side_effect=AssertionError('unexpected model')) as model:
            call_command('keyword_radar',online=True,provider='jetro_biznews',max_search=1,
                         search_daily_limit=5,max_details=1,allow_model=True,max_model=1,
                         model_daily_limit=1,output_dir=tmp,stdout=StringIO())
        item.refresh_from_db()
        self.assertEqual(item.status,'needs_company_review')
        model.assert_not_called()


class DirectBackupTests(__import__('django.test', fromlist=['TransactionTestCase']).TransactionTestCase):
    def test_feed_and_manual_verification_raw_are_in_portable_backup(self):
        import tempfile,zipfile
        from pathlib import Path
        from io import StringIO
        from django.core.management import call_command
        from django.test import override_settings
        from radar.management.commands.restore_data import validate_archive
        with tempfile.TemporaryDirectory() as tmp, override_settings(DATA_DIR=Path(tmp)):
            raw=Path(tmp,'raw','research')
            raw.mkdir(parents=True)
            for name in ('feed.html','proof.pdf'):
                (raw/name).write_bytes(name.encode())
            KeywordCandidate.objects.create(canonical_url='https://example.org/a',title='test',
                provider='prtimes',query_id='q',result={'discovery_feed':{'raw_path':'raw/research/feed.html'},
                    'verification':{'sources':[{'raw_path':'raw/research/proof.pdf'}]}})
            archive=Path(tmp,'backup.zip')
            call_command('backup_data',output=str(archive),stdout=StringIO())
            with zipfile.ZipFile(archive) as z:
                _, names=validate_archive(z)
            self.assertIn('raw/research/feed.html',names)
            self.assertIn('raw/research/proof.pdf',names)
