import hashlib
import json
from datetime import date
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from collectors.keyword_radar import QUERY_BANK, publication_from_text, publication_from_tree, score_candidate
from lxml import html
from radar.keyword_extraction import extract, failure_detail
from radar.models import AIConfig, KeywordCandidate, ModelCall, SearchCall
from radar.sales_pools import keyword_card
from radar.search_provider import TavilySearchProvider, budgeted_search


class TavilyTunedTests(TestCase):
    def test_ninety_day_search_and_official_credits_are_recorded(self):
        provider = TavilySearchProvider('dummy-test-key')
        with patch('radar.search_provider.timezone.localdate', return_value=date(2026, 11, 18)), \
             patch('radar.search_provider.api_json', return_value={'results': [], 'usage': {'credits': 1}}) as api:
            budgeted_search(provider, '会社概要 公式', 'official_site', 5)
        request = api.call_args.kwargs['json']
        self.assertEqual(request['start_date'], '2026-08-20')
        self.assertFalse(request['filter_by_published_date'])
        self.assertTrue(request['include_published_date'])
        self.assertIn('yappango.com', request['exclude_domains'])
        for domain in ('the-shashi.com','buffett-code.com','shikiho.toyokeizai.net','job.mynavi.jp','zhihu.com'):
            self.assertIn(domain, request['exclude_domains'])
        self.assertFalse(any('-手順' in query or '-まとめ' in query for _,query in QUERY_BANK))
        self.assertEqual(SearchCall.objects.get().tavily_credits, 1)
        self.assertNotIn('dummy-test-key', str(SearchCall.objects.values().get()))

    def test_real_company_event_scores_above_guidance(self):
        strong = score_candidate('株式会社青木は中国蘇州に新工場を建設', '2026年に拡産')
        weak = score_candidate('中国法人設立の手順とメリットまとめ', '株式会社の設立方法を解説')
        self.assertGreater(strong, weak)
        self.assertLess(score_candidate('ミネベアミツミの歴史', '株式会社は中国に工場を設立'), strong)
        self.assertEqual(publication_from_text('发布时间：2023-04-07 来源：新华日报'),
                         {'date':'2023-04-07','field':'text:发布时间'})
        self.assertEqual(publication_from_tree(html.fromstring('<time datetime="2026-11-10">x</time>')), {})
        self.assertEqual(publication_from_tree(html.fromstring('<time itemprop="datePublished" datetime="2026-11-10">x</time>'))['date'], '2026-11-10')

    def test_pdf_bytes_are_not_sent_as_model_text(self):
        with TemporaryDirectory() as temp, override_settings(DATA_DIR=Path(temp)):
            item = KeywordCandidate.objects.create(canonical_url='https://example.org/a.pdf',
                title='株式会社青木 中国苏州新工厂建设', snippet='2026年新工場', provider='tavily',
                query_id='old', text='%PDF-1.7 binary', status='ready_for_review')
            AIConfig.objects.create(daily_limit=10)
            with patch('radar.management.commands.keyword_radar.extract') as model:
                call_command('keyword_radar', online=True, existing_only=True, provider='tavily',
                    allow_model=True, max_model=1, model_daily_limit=10, max_details=1,
                    output_dir=temp, stdout=StringIO())
            item.refresh_from_db()
            self.assertEqual(item.status, 'needs_document_review')
            model.assert_not_called()

    def test_existing_high_score_runs_first_without_a_search(self):
        with TemporaryDirectory() as temp, override_settings(DATA_DIR=Path(temp)):
            root = Path(temp, 'raw', 'research')
            root.mkdir(parents=True)
            raw = b'original evidence'
            (root/'article.html').write_bytes(raw)
            text = '株式会社青木は日本企業として中国蘇州に2026年新工場を建設します。'
            item = KeywordCandidate.objects.create(
                canonical_url='https://example.org/high', title='株式会社青木 中国蘇州に新工場を建設',
                snippet='2026年に工場を新設', provider='tavily', query_id='old', text=text,
                raw_path='raw/research/article.html', published_at=date.today().isoformat(),
                status='ready_for_review', result={'source_sha256':hashlib.sha256(raw).hexdigest(),
                'source_publication':{'date':date.today().isoformat(),'field':'meta:pubdate'}})
            KeywordCandidate.objects.create(canonical_url='https://example.org/low', title='中国法人設立の手順',
                snippet='メリットまとめ', provider='tavily', query_id='old', status='discovered')
            AIConfig.objects.create(daily_limit=10)
            with patch('radar.management.commands.keyword_radar.provider_for') as provider, \
                 patch('radar.management.commands.keyword_radar.read_original') as reader, \
                 patch('radar.management.commands.keyword_radar.extract', return_value={'event_type':'irrelevant'}) as model:
                call_command('keyword_radar', online=True, existing_only=True, provider='tavily',
                    allow_model=True, max_model=1, model_daily_limit=10, max_details=1,
                    output_dir=temp, stdout=StringIO())
            self.assertEqual(model.call_args.args[0].pk, item.pk)
            provider.assert_not_called()
            reader.assert_not_called()
            self.assertEqual(SearchCall.objects.count(), 0)

    def test_failed_validation_preserves_redacted_reason_and_summary(self):
        cfg = AIConfig.objects.create(base_url='https://api.xiaomimimo.com/v1', model='mimo-v2.6-flash', daily_limit=3)
        text = '株式会社青木は日本企業として中国蘇州に新工場を設立。'
        item = KeywordCandidate.objects.create(canonical_url='https://example.org/news', title='工場',
            text=text, provider='tavily', query_id='q')
        payload = {'company_name':'架空test-secret', 'is_japanese_affiliated':True, 'japanese_evidence':'日本企業',
            'size_tier':'unknown', 'event_type':'plant_expansion', 'china_location':'苏州', 'target_period':'',
            'evidence_quote':text, 'source_url':item.canonical_url}
        response = MagicMock(status_code=200, content=b'{}')
        response.json.return_value = {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(payload, ensure_ascii=False)}}]}
        client = MagicMock()
        client.__enter__.return_value.post.return_value = response
        with patch('radar.keyword_extraction.secret', return_value='test-secret'), \
             patch('radar.keyword_extraction.httpx.Client', return_value=client):
            with self.assertRaisesRegex(ValueError, 'unsupported_company'):
                extract(item, cfg, 3)
        item.refresh_from_db()
        self.assertEqual(item.error_detail['code'], 'unsupported_company')
        self.assertEqual(item.error_detail['fields'], ['company_name'])
        self.assertIn('[REDACTED_SECRET]', item.error_detail['model_output_summary'])
        self.assertNotIn('test-secret', str(item.error_detail))
        self.assertEqual(ModelCall.objects.get().status, 'failed')
        self.assertEqual(failure_detail(ValueError('unsupported_quote'), '', '')['fields'], ['evidence_quote'])
        self.assertEqual(failure_detail(ValueError('unsupported_source_url'), '', '')['fields'], ['source_url'])

    def test_recent_media_card_requires_source_date_and_event_time(self):
        url = 'https://newswitch.jp/p/example'
        quote = '株式会社青木は日本企業として2026年中国上海に新工場を設立します。'
        value = {'company_name':'株式会社青木', 'is_japanese_affiliated':True, 'japanese_evidence':'日本企業',
            'size_tier':'unknown', 'event_type':'plant_expansion', 'china_location':'上海', 'target_period':'2026',
            'evidence_quote':quote, 'source_url':url}
        item = KeywordCandidate(canonical_url=url, title='工場', provider='tavily', query_id='q',
            text=quote, raw_path='raw/research/synthetic.html', status='model_needs_review', result={'extraction':value,
            'source_publication':{'date':'2026-11-10','field':'meta:pubdate'}})
        with patch('radar.sales_pools.timezone.localdate', return_value=date(2026, 11, 18)):
            card = keyword_card(item)
            self.assertEqual(card['company_name'], '株式会社青木')
            self.assertIn('待官网/最终股权复核', card['pending'])
            item.result['extraction']['target_period'] = '2029'
            self.assertIsNotNone(keyword_card(item))
            item.result['extraction']['target_period'] = '2025'
            self.assertIsNone(keyword_card(item))
            item.result['extraction']['target_period'] = '2026'
            item.result['source_publication']['date'] = '2025-11-10'
            self.assertIsNone(keyword_card(item))
            item.result['source_publication']['date'] = '2026-11-10'
            item.result['extraction']['evidence_quote'] = quote.replace('2026年', '')
            item.text = item.text.replace('2026年', '')
            item.result['extraction']['target_period'] = ''
            self.assertEqual(keyword_card(item)['target_period'], '待核')
