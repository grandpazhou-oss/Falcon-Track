from datetime import date

from radar.development import homepage_contacts
from radar.sales_pools import score_card


def test_score_changes_with_evidenced_timing_and_size():
    today = date(2026, 11, 18)
    card = {'source_url':'https://www.idec.or.jp/case','company_name':'样例',
            'action_label':'设厂计划','evidence_quote':'上海工厂计划','china_location':'上海',
            'target_period':'2026-12','size_tier':'micro_sme','pool':'establishment'}
    upcoming = score_card({**card, 'pending':[]}, today)
    assert upcoming['parts'] == [17,15,15,10]
    past = score_card({**card, 'pending':[]}, date(2027, 1, 1))
    assert past['parts'][2] == 0
    assert '原计划时间已过，进度待核' in past['pending']
    assert homepage_contacts([{'cards':[upcoming, past]}]) == [upcoming]
    editorial = score_card({**card, 'pending':[], 'source_kind':'editorial_recheck'}, today)
    assert upcoming['score'] == editorial['score']
