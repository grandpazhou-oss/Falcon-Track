from io import StringIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from django.core.management import call_command
from django.test import TestCase

from radar.models import AIConfig, KeywordCandidate, SearchCall
from radar.search_provider import TavilySearchProvider
from collectors.keyword_radar import round_robin_candidates
from collectors.keyword_radar import publication_from_tree
from lxml import html


class TavilyGateTests(TestCase):
    def test_detail_budget_rotates_across_queries(self):
        items=[KeywordCandidate(query_id=q,title=q) for q in ('a','a','a','b','b','c')]
        self.assertEqual([x.query_id for x in round_robin_candidates(items,4)],['a','b','c','a'])

    def test_official_publication_metadata_is_captured(self):
        tree=html.fromstring('<html><head><meta name="PubDate" content="2025-06-13 09:30:38"></head></html>')
        self.assertEqual(publication_from_tree(tree),{'date':'2025-06-13','field':'meta:PubDate'})

    def test_basic_search_records_credits_without_saving_key(self):
        with patch('radar.search_provider.api_json',return_value={'results':[],'usage':{'credits':1}}) as api:
            provider=TavilySearchProvider('dummy-test-key')
            self.assertEqual(provider.search('日本企業 中国','q1'),[])
        request=api.call_args.kwargs['json']
        self.assertEqual(request['search_depth'],'basic')
        self.assertFalse(request['auto_parameters'])
        self.assertTrue(request['include_usage'])
        self.assertEqual(provider.last_stats,{'credits':1})
        self.assertNotIn('dummy-test-key',str(provider.last_stats))

    def test_denied_search_stops_without_model_calls(self):
        AIConfig.objects.create(search_daily_limit=10)
        candidate=KeywordCandidate.objects.create(canonical_url='https://example.org/a',title='test',
                                                    provider='tavily',query_id='previous')
        request=httpx.Request('POST','https://api.tavily.com/search')
        response=httpx.Response(429,request=request)
        class Provider:
            def search(self,query,query_id):
                raise httpx.HTTPStatusError('rate limited',request=request,response=response)
        with TemporaryDirectory() as tmp, patch('radar.management.commands.keyword_radar.provider_for',return_value=Provider()), \
             patch('radar.management.commands.keyword_radar.extract') as model, \
             patch('radar.management.commands.keyword_radar.read_original') as original:
            call_command('keyword_radar',online=True,provider='tavily',max_search=5,search_daily_limit=10,
                         allow_model=True,max_model=3,model_daily_limit=3,max_details=2,
                         output_dir=tmp,stdout=StringIO())
        self.assertEqual(SearchCall.objects.count(),1)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status,'discovered')
        model.assert_not_called()
        original.assert_not_called()
