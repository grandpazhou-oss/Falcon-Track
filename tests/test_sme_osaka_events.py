from datetime import date

import pytest

from collectors.osaka_events import parse_news_index, parse_osaka_event, parse_ageless_event, compare_event_claims, to_research_record

OSAKA='https://osaka-sh.com.cn/news/business-matching2026/'
ATC='https://www.ageless.gr.jp/seminar/6839/'


def osaka(title,body,published='2026-10-27'):
    return f'<meta charset="utf-8"><div id="single_news"><div class="title">{title}</div><div class="stitle">{published}</div><div class="main">{body}</div></div>'.encode()


def atc(title,body):
    return f'<meta charset="utf-8"><title>{title}</title><article><div class="ArticleBody">{body}</div></article>'.encode()


EVENT='中国展開・介護用品ビジネスマッチング'
DATE='■ 開催日時：2026年12月4日(金) 日本時間 10:00'
SOURCE='<p>2025年に甲社が中国で「山下為楽帆（上海）実業発展有限公司」を設立したほか、福岡県を拠点とする長潤国際貿易株式会社も中国各地にフランチャイズ網を広げる。</p><p>今回は、上記の山下為楽帆（上海）実業発展有限公司、長潤国際貿易株式会社との商談機会をご提供します。</p>'
SECONDARY='<p>今回の商談は、販売会社である山下福至（上海）健康管理有限公司、長潤国際貿易株式会社との商談機会をご提供します。</p>'


def test_index_is_same_origin_and_deduplicated():
    body=b'<a href="/news/a/">first</a><a href="/news/a/#x">dup</a><a href="https://evil.test/news/x/">evil</a>'
    assert [x['url'] for x in parse_news_index(body)]==['https://osaka-sh.com.cn/news/a/']


def test_future_recruitment_does_not_mean_confirmed_attendance():
    event=parse_osaka_event(osaka('12/4 '+EVENT+'を開催します', SOURCE+'<p>'+DATE+'</p>'),OSAKA,as_of=date(2026,11,14))
    assert event['source_stage']=='announced' and event['current_state']=='upcoming_as_announced'
    assert event['event_start']=='2026-12-04' and not event['is_procurement']
    assert all(x['role']=='announced_counterpart' for x in event['counterparts'])


def test_past_recruitment_is_not_reported_as_held():
    raw=osaka('FBC上海2026の出展者を募集します','<p>会期：2026年11月３日（火）～６日（金）<br>対象：50社</p>')
    event=parse_osaka_event(raw,'https://osaka-sh.com.cn/news/fbcshanghai2026inptc/',as_of=date(2026,11,14))
    assert event['source_stage']=='recruitment' and event['event_end']=='2026-11-06'
    assert event['current_state']=='planned_date_passed_unverified' and event['counterparts']==[]


@pytest.mark.parametrize(('title','stage'), [('イベントを中止します','cancelled'),('イベントを延期します','postponed'),('イベントを開催しました','reported_held')])
def test_stage_follows_report_not_elapsed_time(title,stage):
    event=parse_osaka_event(osaka(title,'<p>'+DATE+'</p>'),OSAKA,as_of=date(2026,11,14))
    assert event['source_stage']==stage and event['current_state']==stage


def test_two_publishers_disagree_and_corroborated_name_is_separate():
    primary=parse_osaka_event(osaka('12/4 '+EVENT+'を開催します',SOURCE+'<p>'+DATE+'</p>'),OSAKA,as_of=date(2026,11,14))
    secondary=parse_ageless_event(atc(EVENT+'｜ATC','<p>'+SECONDARY+'</p><p>'+DATE+'</p>'),ATC)
    compared=compare_event_claims(primary,secondary)
    assert compared['status']=='different_lists_needs_review'
    assert compared['common_names']==['長潤国際貿易株式会社']
    assert {x['name'] for x in compared['qualified_research_targets']}=={'長潤国際貿易株式会社'}
    assert {x['name'] for x in compared['disputed_claims']}=={'山下為楽帆（上海）実業発展有限公司','山下福至（上海）健康管理有限公司'}


def test_wrong_event_or_unproven_china_relation_is_not_promoted():
    primary=parse_osaka_event(osaka('12/4 '+EVENT+'を開催します','<p>今回は、上記の長潤国際貿易株式会社との商談機会をご提供します。</p><p>'+DATE+'</p>'),OSAKA,as_of=date(2026,11,14))
    secondary=parse_ageless_event(atc(EVENT+'｜ATC','<p>販売会社である長潤国際貿易株式会社との商談機会をご提供します。</p><p>'+DATE+'</p>'),ATC)
    assert compare_event_claims(primary,secondary)['qualified_research_targets']==[]
    secondary['event_start']='2027-12-04'
    with pytest.raises(ValueError,match='different_event'):
        compare_event_claims(primary,secondary)


def test_organizers_and_approximate_counts_are_not_companies():
    raw=osaka('商談会出展企業募集','<p>共催：株式会社テスト。対象：50社。</p><p>'+DATE+'</p>')
    assert parse_osaka_event(raw,OSAKA,as_of=date(2026,11,14))['counterparts']==[]


def test_information_title_is_not_invented_as_an_event_announcement():
    raw=osaka('イベントに関する情報','<p>'+DATE+'</p>')
    event=parse_osaka_event(raw,OSAKA,as_of=date(2026,11,14))
    assert event['source_stage']=='unknown' and event['current_state']=='future_date_unverified'


def test_research_conversion_requires_both_sources_and_qualified_target():
    primary=parse_osaka_event(osaka('12/4 '+EVENT+'を開催します',SOURCE+'<p>'+DATE+'</p>'),OSAKA,as_of=date(2026,11,14))
    secondary=parse_ageless_event(atc(EVENT+'｜ATC','<p>'+SECONDARY+'</p><p>'+DATE+'</p>'),ATC)
    first={'sha256':primary['document_hash'],'raw_path':'raw/research/one.html'}
    second={'sha256':secondary['document_hash'],'raw_path':'raw/research/two.html'}
    record=to_research_record(primary,secondary,'長潤国際貿易株式会社',first,second)
    assert record['research']['event_type']=='market_research' and record['research']['participants'][0]['role']=='announced_counterpart'
    assert record['research']['dates'][0]['status']=='planned' and len(record['supporting_documents'])==1
    with pytest.raises(ValueError,match='qualified_target_missing'):
        to_research_record(primary,secondary,'山下為楽帆（上海）実業発展有限公司',first,second)
    with pytest.raises(ValueError,match='source_evidence_missing'):
        to_research_record(primary,secondary,'長潤国際貿易株式会社',{**first,'sha256':'a'*64},second)


def test_comparison_bridge_retains_conflict_without_making_extra_company():
    p={'event_name':'中国展開・介護用品ビジネスマッチング','event_start':'2026-12-04',
       'url':'https://osaka-sh.com.cn/news/business-matching2026/','title':'中国展開・介護用品ビジネスマッチングを開催します',
       'published':'2026-10-27','text':'中国本土で営業する長潤国際貿易株式会社。山下一有限公司、長潤国際貿易株式会社との商談機会。開催日時：2026年12月4日',
       'document_hash':'a'*64,'date_quote':'開催日時：2026年12月4日','counterparts':[]}
    s={'event_start':'2026-12-04','url':'https://www.ageless.gr.jp/seminar/6839/',
       'title':'中国展開・介護用品ビジネスマッチング','text':'山下二有限公司、長潤国際貿易株式会社との商談機会。',
       'document_hash':'b'*64,'counterparts':[]}
    def claim(name,quote,event,china=''):
        return {'name':name,'quote':quote,'url':event['url'],'document_hash':event['document_hash'],'china_evidence':china}
    p['counterparts']=[claim('長潤国際貿易株式会社','山下一有限公司、長潤国際貿易株式会社との商談機会',p,'中国本土で営業する長潤国際貿易株式会社。'),claim('山下一有限公司','山下一有限公司、長潤国際貿易株式会社との商談機会',p)]
    s['counterparts']=[claim('長潤国際貿易株式会社','山下二有限公司、長潤国際貿易株式会社との商談機会',s),claim('山下二有限公司','山下二有限公司、長潤国際貿易株式会社との商談機会',s)]
    record=to_research_record(p,s,'長潤国際貿易株式会社',{'sha256':'a'*64,'raw_path':'raw/a.html'}, {'sha256':'b'*64,'raw_path':'raw/b.html'})
    assert [x['name'] for x in record['research']['participants']]==['長潤国際貿易株式会社']
    assert len([x for x in record['research']['facts'] if x['field']=='conflicting_counterpart'])==2
    assert record['research']['dates'][0]['status']=='planned'
