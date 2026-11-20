"""Revalidate a small, editorially selected set of first-party announcements."""
from lxml import html
from collectors.research_common import read_original

# Each marker must occur in the same original version before its fact is used.
DISCLOSURES = [
    ('https://www.malymoon-costume.com/view/news/20260304185905', '株式会社Willbermoon',
     '中国1号店となる上海店をオープン', '2026-04-17', 'Malymoon 上海首店及杭州二店计划',
     '请日方品牌网店运营法人确认华东两店是否已营业、在华负责方，以及服装跨店补货和退换货安排。'),
    ('https://vsw.co.jp/shanghai-shop/', 'Vsw 株式会社', '上海 1号店、2026年7月オープンに向けて計画進行中。',
     '2026-07', '上海食品专门店计划', '官网同时提到常设店；核实具体门店现状，以及日方或当地伙伴谁负责进货、储温和配送。'),
    ('https://www.onward-hd.co.jp/release/2026/20260722.html', '株式会社ウィゴー', '中国・上海における実店舗１店舗目',
     '2026-09-30', '上海首家实体店计划', '核实门店营业状态，以及线上订单和门店是否共用库存、谁负责补货及退货。'),
    ('https://totachi.co.jp/news/detail.php?id=57', 'TOTACHI工業株式会社', '次回のAutomechanika Shanghaiは、今年の12月2日から5日の日程で開催予定です。',
     '2026-12-02', '上海汽配展再参展计划', '核实参展安排、由哪一方运送样品及展后分销补货计划；液体分类以具体产品资料为准。'),
]


def collect_disclosures(source):
    items, errors = [], []
    for url, name, marker, date, title, question in DISCLOSURES:
        try:
            result, doc = read_original(url)
            tree = html.fromstring(result['body'].decode('utf-8'))
            lines = [s.strip() for s in tree.xpath('//text()[not(ancestor::script or ancestor::style)]') if s.strip()]
            def evidence(fragment):
                quote = next(s for s in lines if fragment in s)
                return {'location': '企业原页正文或页标题', 'quote': quote}
            date_marker = {'2026-04-17':'2026年4月17日', '2026-07': '2026年7月', '2026-09-30': '2026年9月30日', '2026-12-02': '12月2日から5日'}[date]
            if name == 'TOTACHI工業株式会社':
                evidence('2026/02/20')
            participant = {'role':'japan_company','name':name}
            supporting, extra_facts = [], []
            size = {'value':'unknown'}
            if name == '株式会社Willbermoon':
                contract, legal = read_original('https://www.malymoon-costume.com/view/contract')
                legal_text = html.fromstring(contract['body'].decode('utf-8')).text_content()
                if name not in legal_text or 'Malymoon' not in legal_text:
                    raise ValueError('品牌网店法人尚未核实')
                supporting.append({**legal,'type':'html','text':legal_text})
                participant.update(quote=name,location='品牌官网特商法经营者公示',document_hash=legal['sha256'])
                from collectors.kanto_cases import material, evidence as proof
                profile = material('https://www.willbermoon.com/company')
                proof(profile, name)
                staff = proof(profile, '55名 従業員数')
                supporting.append(profile)
                extra_facts.append({'field':'employees','value':'55',**staff})
                size = {'value':'sme',**staff}
                extra_facts.append({'field':'relation_description','value':'日本品牌网店运营法人，在华门店运营方待核',
                                    **{k:participant[k] for k in ('quote','location','document_hash')}})
            else:
                participant.update(evidence(name))
            items.append({'external_id': url, 'title': name + ' · ' + title, 'url': url,
                          'text': '\n'.join(lines), 'supporting_documents':supporting, 'document': {**doc, 'type': 'html'},
                          'research': {'event_type': 'market_research', 'workstream': 'planned_china',
                          'china_scope': 'mainland', 'china_evidence': evidence('上海で4日間' if name == 'TOTACHI工業株式会社' else marker),
                          'participants': [participant], 'size':size,
                          'facts': [*extra_facts, {'field': 'market_stage', 'value': title, **evidence(marker)}],
                          'dates': [{'kind':'planned_date', 'value': date, 'status':'planned',
                                     'precision':'month' if len(date)==7 else 'day', **evidence(date_marker)}],
                          'next_step': question}})
        except Exception as exc:
            errors.append({'url': url, 'error': str(exc) or type(exc).__name__})
    return {'items': items, 'status': ('partial' if items else 'blocked') if errors else 'success',
            'error': str(errors) if errors else '', 'stats': {'discovery_method':'editorial_recheck','editorial_recheck':len(items),'errors': errors, 'error_count': len(errors),
            'coverage_complete': not errors, 'scope':'人工发现的企业原发，重新核验证据，不覆盖全网'}}
