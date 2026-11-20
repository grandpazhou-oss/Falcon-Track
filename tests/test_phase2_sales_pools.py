"""Offline cases for source evidence, pool routing and exported cards."""
from unittest.mock import patch
from django.test import TestCase
from django.urls import reverse

from collectors.keyword_radar import collect
from radar.keyword_extraction import validate_result
from radar.models import KeywordCandidate, Source, Lead, Document, ResearchEvent
from radar.sales_pools import build_sales_pools, clean_company_name
from radar.search_provider import DirectJapanFeedProvider, SearchResult
from radar.website_check import discover_official_site, probe_official_site


def extraction(text, url, company, event, location='上海', size='unknown', year='2027'):
    return {'company_name':company,'is_japanese_affiliated':True,'japanese_evidence':company,
        'size_tier':size,'event_type':event,'china_location':location,'target_period':year,
        'evidence_quote':text,'source_url':url}


def make_candidate(text, url, event, company='キリン株式会社', size='unknown', status='model_needs_review'):
    return KeywordCandidate.objects.create(canonical_url=url,title=company,provider='prtimes',query_id='q',
        text=text,raw_path='raw/research/synthetic.html',status=status,result={'extraction':extraction(text,url,company,event,size=size)})


class SalesPoolTests(TestCase):
    def test_sme_discovery_official_two_hop_and_pool(self):
        xml='''<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/">
        <item><title>株式会社マコホ 中国進出計画</title><link>https://prtimes.jp/macoho</link>
        <description>日本の中小企業が中国蘇州で新工場を設立</description></item></rdf:RDF>'''.encode()
        response={'body':xml,'url':'https://prtimes.jp/index.rdf'}
        evidence={'raw_path':'raw/research/feed.html','sha256':'a'*64,'redirect_chain':[],'robots_redirect_chains':[]}
        with patch('collectors.research_common.read_original',return_value=(response,evidence)):
            self.assertEqual(collect(DirectJapanFeedProvider('prtimes'),'feed','q',3)['added'],1)
        candidate=KeywordCandidate.objects.get()
        html='<html><body>株式会社マコホ</body></html>'.encode()
        web_response={'url':'https://macoho.example/','body':html}
        web_evidence={'url':web_response['url'],'raw_path':'raw/research/site.html','sha256':'b'*64}
        results=[SearchResult('官网','https://macoho.example/about','','','mock','q')]
        with patch('radar.website_check.read_original',return_value=(web_response,web_evidence)):
            site=discover_official_site('株式会社マコホ',results)
        self.assertEqual(site['status'],'name_matched_ownership_unverified')
        def read(url,**kwargs):
            if url.endswith('/news'):
                body='<html><a href="/news/china">中国工場記事</a></html>'.encode()
            elif url.endswith('/news/china'):
                body='<html><body>2026年11月17日 日本企業が中国蘇州に新工場を設立。</body></html>'.encode()
            else:
                raise ValueError('not available')
            return {'url':url,'body':body},{'url':url,'raw_path':'raw/research/a.html','sha256':'c'*64}
        with patch('radar.website_check.read_original',side_effect=read):
            findings=probe_official_site(site,max_pages=8)
        self.assertTrue(any(x['url'].endswith('/news/china') for x in findings))
        text='株式会社マコホ计划は日本の中小企業として中国蘇州に2027年新工場を設立します。'
        payload=extraction(text,candidate.canonical_url,'株式会社マコホ计划','plant_expansion','蘇州','micro_sme')
        candidate.text=text;candidate.raw_path='raw/research/synthetic.html';candidate.status='model_needs_review'
        candidate.result={'extraction':validate_result(payload,text,candidate.canonical_url)}
        candidate.save(update_fields=['text','raw_path','status','result'])
        card=build_sales_pools()[0]['cards'][0]
        self.assertEqual(card['company_name'],'株式会社マコホ')
        self.assertEqual(card['size_tier'],'micro_sme')
        self.assertEqual(card['pool'],'establishment')
        self.assertEqual(card['evidence_quote'],text)

    def test_existing_logistics_change_and_historical_and_ad_noise(self):
        current='キリン株式会社は日本の大手企業で、中国上海の物流センターを2027年に移転します。'
        make_candidate(current,'https://example.org/current','logistics_change',size='mid_large')
        old='キリン株式会社は2003年に中国上海の工場で稼働しました。'
        make_candidate(old,'https://example.org/old','historical_only')
        marketing='キリン株式会社の新しい消費者向け商品を中国上海で販売。'
        make_candidate(marketing,'https://example.org/ad','irrelevant')
        pools=build_sales_pools()
        self.assertEqual([c['company_name'] for c in pools[2]['cards']],['キリン株式会社'])
        self.assertEqual(pools[2]['cards'][0]['size_tier'],'mid_large')
        self.assertEqual(sum(p['count'] for p in pools),1)
        self.assertIn('物流外包需求未确认',pools[2]['cards'][0]['pending'])

    def test_explicit_china_market_entry_is_in_sme_pool_and_sme_first(self):
        text='株式会社マコホは日本の中小企業として2027年に中国上海の市場開拓を計画します。'
        make_candidate(text,'https://example.org/entry','market_entry_plan',company='株式会社マコホ',size='micro_sme')
        general='株式会社大規模は中国上海に2027年工場を新設します。'
        small='株式会社小規模は日本の中小企業として中国上海に2027年工場を新設します。'
        make_candidate(general,'https://prtimes.jp/large','new_china_entity',company='株式会社大規模')
        make_candidate(small,'https://prtimes.jp/small','new_china_entity',company='株式会社小規模',size='micro_sme')
        pools=build_sales_pools()
        self.assertEqual(pools[1]['cards'][0]['company_name'],'株式会社マコホ')
        self.assertEqual(pools[0]['cards'][0]['size_tier'],'micro_sme')

    def test_mismatched_evidence_url_does_not_become_card(self):
        text='キリン株式会社は中国上海の物流センターを2027年に移転します。'
        item=make_candidate(text,'https://example.org/original','logistics_change')
        item.result['extraction']['source_url']='https://example.org/other'
        item.save(update_fields=['result'])
        self.assertEqual(build_sales_pools()[2]['count'],0)

    def test_old_relative_year_plan_is_retained_but_excluded_from_active_pools(self):
        quote='日资企业东莞新荣精密汽车配件有限公司在中国东莞今年计划扩大工厂。'
        item=make_candidate(quote,'https://www.gz.gov.cn/a','plant_expansion',
                            company='日资企业东莞新荣精密汽车配件有限公司')
        item.result['source_publication']={'date':'2025-06-13','field':'meta:PubDate'}
        item.save(update_fields=['result'])
        with patch('radar.sales_pools.timezone.localdate',return_value=__import__('datetime').date(2025,11,18)):
            self.assertEqual(build_sales_pools()[0]['count'],1)
        with patch('radar.sales_pools.timezone.localdate',return_value=__import__('datetime').date(2026,11,18)):
            self.assertEqual(build_sales_pools()[0]['count'],0)
        self.assertTrue(KeywordCandidate.objects.filter(pk=item.pk).exists())
        self.assertEqual(clean_company_name('日资企业东莞新荣精密汽车配件有限公司'),'东莞新荣精密汽车配件有限公司')

    def test_later_model_output_does_not_erase_verified_research(self):
        text='キリン株式会社は中国上海に2027年工場を新設します。'
        item=make_candidate(text,'https://prtimes.jp/verified','new_china_entity')
        item.status='verified_research_signal'
        item.result['verification']={'status':'verified_research_signal',
            'sources':[{'url':'https://example.org/proof','quote':'来源证明'}]}
        item.result['phase2_model_history']=[{'status':'succeeded','output':{
            **item.result['extraction'],'event_type':'irrelevant'}}]
        item.save(update_fields=['status','result'])
        self.assertEqual(build_sales_pools()[0]['cards'][0]['company_name'],'キリン株式会社')

    def test_highchem_source_quote_year_and_normalization(self):
        url='https://prtimes.jp/highchem'
        text='（株）ハイケムは中国・江蘇省南通市の工場に新たな触媒製造プラントを設置します。2027年春の稼働を予定しています。'
        data=extraction(text,url,'株式会社ハイケム','plant_expansion','南通')
        data['japanese_evidence']='株式会社ハイケム'
        value=validate_result(data,text,url)
        self.assertEqual(value['evidence_quote'],text)
        self.assertEqual(value['japanese_evidence'],'（株）ハイケム')
        self.assertEqual(value['target_period'],'2027')
        KeywordCandidate.objects.create(canonical_url=url,title='南通工場',provider='prtimes',query_id='q',
            text=text,raw_path='raw/research/synthetic.html',status='model_needs_review',result={'extraction':value})
        card=build_sales_pools()[0]['cards'][0]
        self.assertEqual((card['china_location'],card['target_period']),('南通','2027'))
        self.assertEqual(card['size_tier'],'unknown')

    def test_export_and_dashboard_show_same_read_only_cards(self):
        text='株式会社マコホは日本の中小企業として中国上海で2027年に工場を新設します。'
        make_candidate(text,'https://prtimes.jp/a','new_china_entity',company='株式会社マコホ',size='micro_sme')
        before=KeywordCandidate.objects.count()
        response=self.client.get(reverse('radar:sales_pools_export'))
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(response.json()['pools']),3)
        card=response.json()['pools'][0]['cards'][0]
        for key in ('company_name','size_tier','event_type','china_location','target_period',
                    'japanese_identity_basis','pending','evidence_quote','source_url','sales_directions'):
            self.assertIn(key,card)
        self.assertNotIn('key_ref',str(response.json()))
        dashboard=self.client.get(reverse('radar:dashboard'))
        self.assertContains(dashboard,'A 级优先商机')
        self.assertContains(self.client.get(reverse('radar:sales_pools')+'?grade=B'),'株式会社マコホ')
        self.assertContains(dashboard,'物流外包需求未确认')
        self.assertEqual(KeywordCandidate.objects.count(),before)

    def test_export_marks_missing_location_and_target_period(self):
        text='キリン株式会社は中国市場の物流倉庫を移転します。'
        item=make_candidate(text,'https://example.org/move','logistics_change')
        item.result['extraction']['china_location']=''
        item.result['extraction']['target_period']=''
        item.save(update_fields=['result'])
        payload=self.client.get(reverse('radar:sales_pools_export')).json()
        card=payload['pools'][2]['cards'][0]
        self.assertEqual(card['china_location'],'待核')
        self.assertEqual(card['target_period'],'待核')

    def test_old_directory_and_expired_plan_are_not_current_entry(self):
        self.assertEqual(clean_company_name('株式会社マコホ计划'),'株式会社マコホ')
        source=Source.objects.create(key='maff_test',name='公开来源',url='https://www.maff.go.jp')
        lead=Lead.objects.create(source=source,title='旧计划',text='株式会社青木は中国市場の開拓を計画',content_hash='a'*64,
                                 external_id='one',url='https://www.maff.go.jp/p.html')
        doc=Document.objects.create(lead=lead,content_hash='b'*64,text=lead.text)
        p={'name':'株式会社青木','role':'japan_company','quote':'株式会社青木','document_id':doc.pk}
        china={'quote':'株式会社青木は中国市場の開拓を計画','document_id':doc.pk,'url':lead.url}
        size={'value':'sme','quote':'株式会社青木','document_id':doc.pk}
        ResearchEvent.objects.create(lead=lead,event_type='export_plan',china_scope='mainland',participants=[p],
          china_evidence=china,size=size,dates=[{'kind':'plan_fiscal_year','value':'令和5年度'}],source_document_hash='b'*64)
        self.assertEqual(build_sales_pools()[1]['count'],0)
