"""Read-only sales cards from evidence-backed research and keyword candidates."""
import re
import unicodedata
from datetime import date,timedelta
from urllib.parse import urlsplit

from django.urls import reverse
from django.utils import timezone

from .keyword_extraction import source_excerpt
from .china_places import CITIES, project_city_spelling, region_weight
from .keyword_dates import recent_publication
from .models import Company, KeywordCandidate, Lead, ResearchEvent
from .company_enrichment import fields as company_fields, named_case, site_root
from .research import _mainland_evidence
from .project_grade import original_matches, qualify, promote
from .search_provider import canonical_url

POOLS = (
    ('establishment', '高确定性在华新设／扩产池'),
    ('sme_entry', '中小型日企高潜出海池'),
    ('logistics_change', '存量日企物流异动／再评估池'),
)
ACTION_LABELS = {
    'new_china_entity':'新设在华法人／据点',
    'plant_expansion':'扩产／扩建',
    'market_entry_plan':'对华市场进入计划',
    'logistics_change':'物流据点／业务变化',
}
HISTORICAL = {'historical_only', 'irrelevant', 'cancelled_or_withdrawn', 'existing_presence'}
EDITORIAL_SOURCES = {'kanto_verified_cases', 'sh_market_disclosures'}
RETAIL = re.compile(r'出店|開店|オープン|店舗開設|开店|门店')
TRADE = re.compile(r'輸出|輸入|出荷|納品|販売|越境EC|貿易|进出口|供货|补货')



def clean_company_name(value):
    name = re.sub(r'\s+', ' ', str(value or '')).strip(' \t\n：:、，,;；')
    original = name
    prefix = re.match(r'^(?:日资企业|日資企業|日系企业|日系企業|日本企业|日本企業)\s*', name)
    if prefix and re.search(r'有限公司|有限会社|株式会社|股份公司', name[prefix.end():]):
        name = name[prefix.end():]
    name = re.sub(r'(?:は|が)?(?:中国|上海|蘇州|苏州|深圳|南通)(?:への|に|で)?(?:進出|設立|新設|进出|设立|扩产)(?:を|の)?(?:計画|予定|计划|拟)$', '', name)
    name = re.sub(r'(?:计划在|計画|计划|予定|拟在|擬|拟)$', '', name).strip(' \t\n：:、，,;；')
    return original if not name or name in {'株式会社','有限会社','有限公司'} else name


def _city(text):
    return project_city_spelling(text, municipal_suffix=True)


def _period_evidence(text, period, quote, *, document=False):
    parts = period.split('-')
    pattern = (r'(?<!\d)' + re.escape(parts[0]) +
               (r'(?:年|[-/.])0?' + str(int(parts[1])) + r'(?:月|[-/.]|$)' if len(parts) == 2 else r'(?!\d)'))
    def matches(value):
        return bool(re.search(pattern, re.sub(r'\s+', '', unicodedata.normalize('NFKC', value or ''))))
    if matches(quote):
        return quote
    if not document:
        return ''
    for line in (text or '').splitlines():
        compact = re.sub(r'\s+', '', unicodedata.normalize('NFKC', line))
        if matches(line) and re.search(r'予定|計画|计划|安排|投产|稼働|完成|開業|開設|設立|开业|操業|开工|着工', compact):
            return line.strip()
    return ''


def _directions(text):
    result = []
    if re.search(r'輸出|輸入|进出口|跨境|貿易|贸易|海外向け', text, re.I):
        result.append('跨境空运／海运：业务量及路线待核')
    if re.search(r'工場|工厂|工場|プラント|生産|生产|倉庫|仓库|物流センター|物流中心', text, re.I):
        result.append('国内3PL仓配：仓储与配送需求待核')
    if re.search(r'化学|化工|危険物|危化|触媒|催化', text, re.I):
        result.append('化工／危险品物流：物料属性与资质待核')
    return result or ['空运、海运、3PL及危险品方向均需先核实需求与我方能力']


def _base_card(*, pool, company, size, event_type, action, location, period, basis,
               quote, source_url, text, detail_url='', source_kind='', identity_url=''):
    return {'pool':pool, 'company_name':company or '待核', 'size_tier':size,
            'event_type':event_type, 'action_label':action or '待核', 'china_location':location or '待核',
            'target_period':period or '待核', 'japanese_identity_basis':basis or '待核',
            'identity_evidence_url':identity_url, 'evidence_quote':quote or '待核', 'source_url':source_url,
            'sales_directions':_directions(text),
            'pending':['最终股东国籍待核', '物流外包需求未确认'],
            'detail_url':detail_url, 'source_kind':source_kind}


def score_card(card, today, *, independently_verified=False, identity_verified=True, publication='', question=''):
    host = urlsplit(card['source_url']).hostname or ''
    trusted = host.endswith(('.gov.cn','.go.jp','.lg.jp')) or host == 'www.idec.or.jp'
    credibility = 17 if trusted else 13 if independently_verified or _official(card['source_url']) else 9
    planned = bool(re.search(r'計画|予定|目指|検討|计划|拟', card['action_label'] + card['evidence_quote']))
    action = 15 if planned else 23
    period = card['target_period']
    timing = 0
    if re.fullmatch(r'20\d{2}(?:-\d{2}(?:-\d{2})?)?', period):
        if period < today.isoformat()[:len(period)]:
            card['pending'].append('原计划时间已过，进度待核')
        else:
            timing = 15
    elif recent_publication(publication, today) or (
            re.fullmatch(r'20\d{2}-(?:0[1-9]|1[0-2])', publication)
            and (0 <= (today.year - int(publication[:4])) * 12 + today.month - int(publication[5:]) <= 5)):
        timing = 10
    if timing == 0:
        card['pending'].append('近期进度／货期待核')
    size = 10 if card['size_tier'] == 'micro_sme' else 0
    if card['size_tier'] == 'unknown':
        card['pending'].append('规模待核')
    weight = region_weight(card['china_location'] + ' ' + card['evidence_quote'])
    score = 18 + credibility + action + timing + size + weight
    qualified = bool(identity_verified and timing and score >= 65)
    if not qualified:
        score = min(score, 64)
    grade = 'A' if qualified else 'B' if score >= 45 else 'C'
    return {**card, 'score':score, 'pool_label':dict(POOLS)[card['pool']],
            'label':'优先联系' if grade == 'A' else '高潜待核' if grade == 'B' else '观察',
            'grade':grade, 'content_mode':'full_text', 'region_weight':weight,
            'parts':[credibility,action,timing,size], 'reason':card['action_label'],
            'question':question or '最近一批货何时发运，由哪一方安排运输？'}


def _size(value, source_text='', evidence=''):
    if value in {'micro_sme','micro','sme'}:
        if evidence or re.search(r'中小企業|中小企业|小規模事業|小微企业', source_text):
            return 'micro_sme'
    if value == 'mid_large' and (evidence or re.search(r'大企業|大手企業|大手メーカー|大型企业|大型企業', source_text)):
        return 'mid_large'
    return 'unknown'


def _official(url):
    host = (urlsplit(url).hostname or '').lower()
    return host == 'prtimes.jp' or host.endswith('.prtimes.jp') or host.endswith(('.gov.cn','.go.jp','.lg.jp'))


def _media_host(url):
    host = (urlsplit(url).hostname or '').lower()
    return any(host == domain or host.endswith('.' + domain)
               for domain in ('jetro.go.jp','newswitch.jp','nikkan.co.jp','zaobao.com.sg','yahoo.co.jp'))


def _corporate_source(candidate, extraction):
    site = (candidate.result or {}).get('website_candidate') or {}
    name = extraction.get('company_name', '')
    normalize = lambda value: re.sub(r'\s+', '', unicodedata.normalize('NFKC', value or '')).casefold()
    return bool(site.get('status') == 'name_matched_ownership_unverified' and name
        and normalize(name) == normalize(site.get('company', '')) and source_excerpt(site.get('quote', ''), name)
        and (urlsplit(site.get('url', '')).hostname or '').lower() == (urlsplit(candidate.canonical_url).hostname or '').lower()
        and site.get('raw_path') and re.fullmatch(r'[0-9a-f]{64}', site.get('sha256', '')))


def _recent_media(candidate, extraction):
    if not (_media_host(candidate.canonical_url) or _official(candidate.canonical_url) or _corporate_source(candidate, extraction)):
        return False
    source_publication = (candidate.result or {}).get('source_publication', {})
    field = source_publication.get('field', '')
    if not (field.startswith(('meta:', 'text:')) or field == 'time:published_datetime'
            or (field == 'pdf:first_page' and source_publication.get('page') == 1
                and source_excerpt(candidate.text, source_publication.get('quote', '')))):
        return False
    today = timezone.localdate()
    if not recent_publication(source_publication.get('date', ''), today):
        return False
    period = str(extraction.get('target_period') or '')
    if period and (not re.fullmatch(r'20\d{2}(?:-(?:0[1-9]|1[0-2]))?', period)
                   or int(period[:4]) < today.year):
        return False
    return True


def _pool_for(event_type):
    return {'new_china_entity':'establishment', 'plant_expansion':'establishment',
            'market_entry_plan':'sme_entry', 'logistics_change':'logistics_change'}.get(event_type)


def _past_plan(candidate, extraction):
    quote = extraction.get('evidence_quote','')
    if not re.search(r'计划|計画|予定|拟|擬|将于|目指',quote):
        return False
    period = str(extraction.get('target_period') or '')
    today = timezone.localdate()
    if re.match(r'^20\d{2}',period) and period[:4] in quote:
        return int(period[:4]) < today.year
    publication = (candidate.result or {}).get('source_publication',{}).get('date','')
    try:
        published = date.fromisoformat(publication)
    except (TypeError,ValueError):
        return False
    if re.search(r'今年|本年|本年度|年内|当年|this year',quote,re.I) and published.year < today.year:
        return True
    return published < today - timedelta(days=366)


def keyword_card(candidate):
    from collectors.keyword_radar import intake_noise_reason
    if intake_noise_reason(candidate.title, candidate.text):
        return None
    if not candidate.raw_path or candidate.result.get('content_mode') == 'snippet_only':
        return None
    if candidate.status not in {'verified_research_signal', 'model_needs_review'}:
        return None
    result = candidate.result or {}
    verified = result.get('verification') or {}
    verification_sources = verified.get('sources', [])
    is_verified = verified.get('status') == 'verified_research_signal' and bool(verification_sources)
    history = result.get('phase2_model_history') or []
    recent = next((x.get('output') for x in reversed(history) if x.get('status') == 'succeeded' and x.get('output')), None)
    extraction = (result.get('extraction') if is_verified else recent) or result.get('extraction') or {}
    event_type = extraction.get('event_type', '')
    pool = _pool_for(event_type)
    if not pool or event_type in HISTORICAL or not extraction.get('is_japanese_affiliated'):
        return None
    plan_extraction = dict(extraction)
    if (is_verified and verified.get('target_period') and
            source_excerpt(candidate.text,verified.get('target_period_evidence',''))):
        plan_extraction['target_period'] = verified['target_period']
        plan_extraction['evidence_quote'] += ' ' + verified.get('target_period_evidence','')
    if _past_plan(candidate,plan_extraction):
        return None
    quote = extraction.get('evidence_quote', '')
    source_url = canonical_url(candidate.canonical_url)
    company = clean_company_name(extraction.get('company_name', ''))
    if foreign_owner(company, candidate.text):
        return None
    if (not source_url or canonical_url(extraction.get('source_url','')) != source_url or not company
            or not quote or quote not in candidate.text or not source_excerpt(candidate.text, company)):
        return None
    if not _mainland_evidence({'quote':quote}):
        return None
    identity = extraction.get('japanese_evidence', '')
    if not identity or identity not in candidate.text:
        return None
    media_recent = _recent_media(candidate, extraction)
    if candidate.provider == 'tavily' and not is_verified and not media_recent:
        return None
    if pool == 'establishment' and not (_official(source_url) or is_verified or media_recent):
        return None
    if _media_host(source_url) and not (is_verified or media_recent):
        return None
    identity_url = ''
    if is_verified:
        japan_source = next((x for x in verification_sources if x.get('role') == 'company_profile'
                             or re.search(r'日本|日资|日系|日資',x.get('quote',''))), None)
        identity_url = canonical_url((japan_source or verification_sources[0]).get('url',''))
        basis = '独立来源核验日本法人／企业；最终股东国籍待核'
    else:
        basis = '模型依据原文识别日企关联，仍需人工核验：' + identity
    size_evidence = source_excerpt(candidate.text, verified.get('size_evidence',''))
    size = _size(extraction.get('size_tier'), quote, size_evidence)
    period = extraction.get('target_period') or ''
    period_evidence = ''
    if period and re.fullmatch(r'20\d{2}(?:-(?:0[1-9]|1[0-2]))?', period):
        period_evidence = _period_evidence(candidate.text, period, quote,
            document=bool(result.get('pdf_review')) and candidate.raw_path.lower().endswith('.pdf'))
    if not period_evidence:
        period = ''
    if (re.fullmatch(r'20\d{2}(?:-(?:0[1-9]|1[0-2]))?', str(verified.get('target_period','')))
            and verified['target_period'][:4] in verified.get('target_period_evidence','')
            and source_excerpt(candidate.text, verified.get('target_period_evidence',''))):
        period = verified['target_period']
        period_evidence = verified['target_period_evidence']
    location = extraction.get('china_location','')
    if location not in CITIES or not source_excerpt(candidate.text, location):
        location = _city(quote)
    card = _base_card(pool=pool, company=company, size=size, event_type=event_type,
        action=ACTION_LABELS[event_type], location=location,
        period=period, basis=basis, quote=quote, source_url=source_url,
        text=candidate.title+'\n'+candidate.text, source_kind='公开发现候选', identity_url=identity_url)
    if not is_verified:
        card['pending'].append('待官网/最终股权复核')
    if re.search(r'计划|計画|予定|拟|擬|決議|目指|検討', quote):
        card['pending'].append('项目实施进度待核')
        if event_type == 'new_china_entity':
            card['action_label'] = '计划新设在华法人／据点，进度待核'
    publication = result.get('source_publication', {})
    case_month = re.fullmatch(r'(20\d{2})年(\d{1,2})月', publication.get('quote',''))
    if _recent_media(candidate, extraction) or (
            is_verified and publication.get('field') == 'text:case_month' and case_month
            and publication.get('date') == f'{case_month[1]}-{int(case_month[2]):02d}'
            and case_month[0] in candidate.text.splitlines()):
        card['source_publication_date'] = publication['date']
    activity_quote = verified.get('completed_activity_evidence','') if is_verified else ''
    activity = re.search(r'(?:令和\s*(\d+)|(20\d{2}))\s*年\s*(\d{1,2})月', activity_quote)
    if activity and activity_quote in candidate.text and 1 <= int(activity[3]) <= 12:
        year = 2018 + int(activity[1]) if activity[1] else int(activity[2])
        card['completed_activity_month'] = f'{year:04d}-{int(activity[3]):02d}'
        card['completed_activity_evidence'] = activity_quote
    if is_verified and verified.get('action_label'):
        card['action_label'] = verified['action_label']
    if period_evidence and period_evidence != quote:
        card['target_period_evidence'] = period_evidence
    card['provider_key'] = candidate.provider
    explicit_japan = bool(re.search(r'日本企業|日本企业|日本法人|日系|日资|日資|日本独資|日本独资|日本.{0,12}本社', identity))
    return score_card(card, timezone.localdate(), independently_verified=is_verified,
                      identity_verified=is_verified or explicit_japan,
                      publication=card.get('source_publication_date') or card.get('completed_activity_month',''),
                      question=verified.get('next_step','') if is_verified else '')


def _evidence_is_saved(lead, item):
    if not isinstance(item, dict) or not item.get('quote') or not item.get('document_id'):
        return False
    document = next((d for d in lead.documents.all() if d.pk == item['document_id']), None)
    return bool(document and item['quote'] in document.text)


def _target_period(lead, dates):
    for d in dates:
        if (d.get('kind') in {'planned','planned_date','target_period'} and d.get('value')
                and d.get('status') in {'planned','stated'} and _evidence_is_saved(lead,d)):
            return str(d['value'])
    return ''


def _candidate_promotion(candidate, card):
    if card['grade'] != 'B':
        return card
    data=candidate.result
    extraction=data.get('extraction') or {}
    action=data.get('source_evidence_quote') or extraction.get('evidence_quote','')
    period=data.get('disclosure_target_period') or card.get('target_period') or extraction.get('target_period','')
    date_quote=_period_evidence(candidate.text,period,action,document=True) if re.fullmatch(
        r'20\d{2}(?:-\d{2})?',period or '') else action
    verification=data.get('verification') or {}
    identified=(data.get('publisher_verified') or (data.get('source_scope')=='issuer_disclosure'
                and data.get('official_website_status')=='verified')
                or (verification.get('status')=='verified_research_signal' and bool(verification.get('sources')))
                or (data.get('pdf_review') and re.search(r'上場会社名|証券コード|コード番号',candidate.text[:2500])
                    and (urlsplit(candidate.canonical_url).hostname or '').endswith('.co.jp')))
    from .project_analysis import normalize_excerpt
    action_saved=(action and normalize_excerpt(action) in normalize_excerpt(candidate.text))
    evidence=qualify(card.get('china_location',''),period,action,date_quote or action,
                     source_verified=bool(identified and action_saved and
                                          original_matches(candidate.raw_path,data.get('source_sha256'))))
    return promote(card,evidence)


def research_card(event):
    lead = event.lead
    if (event.event_type in HISTORICAL | {'research_pending'} or lead.is_test or lead.duplicate_of_id
            or lead.review_status in {'contacted','not_fit'} or event.china_scope != 'mainland'):
        return None
    china = event.china_evidence or {}
    quote, url = china.get('quote', ''), canonical_url(china.get('url', '') or lead.url)
    if not url or not _mainland_evidence({'quote':quote}) or not _evidence_is_saved(lead, china):
        return None
    person = next((p for p in event.participants if p.get('role') in {'japan_parent','japan_company','exporter'} and
                   p.get('name') and p.get('name') in p.get('quote','') and _evidence_is_saved(lead,p)), None)
    if not person:
        return None
    company = clean_company_name(person['name'])
    size_info = event.size or {}
    size_evidence = size_info.get('quote','') if _evidence_is_saved(lead,size_info) else ''
    size = _size(size_info.get('value'), evidence=size_evidence)
    stage = next((f for f in event.facts if f.get('field') == 'market_stage' and _evidence_is_saved(lead,f)), {})
    event_type, action = '', ''
    if event.event_type == 'entity_established':
        event_type, action = 'new_china_entity', '披露在华法人设立'
    elif event.event_type == 'expansion':
        event_type, action = 'plant_expansion', '扩产／扩建'
    elif event.event_type == 'construction_started' and re.search(r'工厂|工場|生产基地|生産拠点', quote):
        event_type, action = 'plant_expansion', '新建工厂／建设中'
    elif event.event_type == 'project_signed' and re.search(r'二期|扩建|扩产|増設|拡張', quote):
        event_type, action = 'plant_expansion', '扩建项目已签约，进度待核'
    elif event.event_type in {'relocation','operation_started'}:
        event_type, action = 'logistics_change', '搬迁信号，物流影响待核'
    elif event.event_type == 'entity_planned':
        event_type, action = 'new_china_entity', '计划新设在华法人／据点，进度待核'
    elif event.event_type == 'market_research' and RETAIL.search(quote):
        event_type, action = 'logistics_change', '在华门店动作，备货安排待核'
    elif event.event_type == 'market_research' and TRADE.search(quote + ' ' + stage.get('quote','')) and size == 'micro_sme':
        event_type, action = 'market_entry_plan', '来源记录对华供货或贸易，近期进度待核'
    elif event.event_type in {'market_research','export_plan'} and size == 'micro_sme':
        if re.search(r'中国.{0,30}(?:计划|拟|開拓|進出|予定|目指|深耕|拡大)|(?:计划|拟|開拓|進出|予定).{0,30}中国', quote):
            event_type, action = 'market_entry_plan', '对华市场计划，执行情况待核'
    pool = _pool_for(event_type)
    if not pool:
        return None
    if event.event_type == 'export_plan':
        fiscal = next((str(x.get('value','')) for x in event.dates if x.get('kind') == 'plan_fiscal_year'), '')
        match = re.search(r'令和(\d+)', fiscal)
        today = timezone.localdate()
        current_fiscal = today.year if today.month >= 4 else today.year - 1
        if match and 2018 + int(match.group(1)) < current_fiscal:
            return None
    affiliation = next((f for f in event.facts if f.get('field') == 'japanese_affiliation' and _evidence_is_saved(lead,f)), None)
    basis = ('来源称述日企关联：' + affiliation['quote']) if affiliation else ('来源列示日本企业：' + person['quote'])
    loc = next((str(f.get('value','')) for f in event.facts if f.get('field') in {'china_location','project_location'} and _evidence_is_saved(lead,f)), '')
    card = _base_card(pool=pool, company=company, size=size, event_type=event_type, action=action,
        location=loc or _city(quote), period=_target_period(lead,event.dates), basis=basis,
        quote=quote, source_url=url, text=lead.title+'\n'+quote,
        detail_url=reverse('radar:research_detail', args=[event.pk]), source_kind='来源研究事件')
    employees = next((f.get('value') for f in event.facts if f.get('field') == 'employees' and _evidence_is_saved(lead,f)), None)
    dates = [d for d in event.dates if _evidence_is_saved(lead,d)]
    publication = next((d['value'] for d in dates if d.get('kind') in {'published','published_at','publication_date','event_date'}
                        and d.get('precision') == 'day'), '')
    if any(d.get('kind') == 'case_year' and str(d['value']) < str(timezone.localdate().year) for d in dates):
        return None
    if re.search(r'历史案例|历史资料', str(stage.get('value',''))):
        return None
    card['provider_key'] = lead.source.key
    if lead.source.key in EDITORIAL_SOURCES:
        card['source_kind'] = 'editorial_recheck'
    score = score_card(card, timezone.localdate(), publication=publication, question=event.next_step)
    if stage:
        score['action_evidence_quote'] = stage['quote']
        score['action_evidence_url'] = stage.get('url', url)
        score['reason'] = str(stage['value'])
    if score['grade']=='B' and event.event_type in {'expansion','relocation','operation_started','construction_started','entity_established','entity_planned','project_signed'}:
        docs={d.pk:d for d in lead.documents.all()}
        action_doc=docs.get(china.get('document_id'))
        loc_fact=next((f for f in event.facts if f.get('field') in {'china_location','project_location'}
                       and _evidence_is_saved(lead,f)),{})
        loc_doc=docs.get(loc_fact.get('document_id'))
        location_verified=bool(loc_doc and original_matches(loc_doc.raw_path,loc_fact.get('document_hash')))
        verified=bool(action_doc and china.get('source_level') in {'source_reported_company','source_reported_official'}
                      and original_matches(action_doc.raw_path,china.get('document_hash')))
        for line in dates:
            if line.get('kind') not in {'event_date','operation_started','planned','planned_date','target_period','china_established'}:
                continue
            if line.get('document_hash')!=china.get('document_hash'):
                continue
            evidence=qualify(loc or _city(quote),line.get('value',''),quote,line.get('quote',''),
                             source_verified=verified,location_verified=location_verified)
            if evidence:
                score=promote(score,evidence)
                break
    if employees:
        score['reason'] += f'（来源披露员工 {employees} 人）'
    return score


def lead_card(lead):
    if (lead.is_test or lead.duplicate_of_id or lead.processing_status in {'closed','irrelevant'} or
            lead.review_status in {'contacted','not_fit'} or not lead.company or lead.company.ownership_status != 'japan_confirmed' or not lead.company.ownership_evidence):
        return None
    company = clean_company_name(lead.company.name)
    url = canonical_url(lead.url)
    if not url or not source_excerpt(lead.text, company):
        return None
    quote = next((x.strip() for x in re.split(r'(?<=[。！？])|\n', lead.text)
                  if _mainland_evidence({'quote':x}) and re.search(r'物流|倉庫|仓库|物流中心',x) and
                  re.search(r'新設|新设|増設|扩建|搬迁|移転|迁建',x)), '')
    if not quote:
        return None
    card = _base_card(pool='logistics_change',company=company,size='unknown',
        event_type='logistics_change',action='物流据点变化，需求待核',location=_city(quote),period='',
        basis='人工确认日资背景：'+lead.company.ownership_evidence,
        quote=quote,source_url=url,text=lead.title+'\n'+quote,
        detail_url=reverse('radar:lead_detail',args=[lead.pk]),source_kind='原有线索')
    card['provider_key'] = lead.source.key
    return score_card(card, timezone.localdate())


def build_sales_pools():
    pools = {key:[] for key,_ in POOLS}
    unique = {}
    candidates = list(KeywordCandidate.objects.order_by('-created_at','-pk'))
    companies = Company.objects.in_bulk(field_name='name')
    reviews = {item.canonical_url:item.result['sales_recheck'] for item in candidates if item.result.get('sales_recheck')}
    events = ResearchEvent.objects.filter(lead__duplicate_of__isnull=True,lead__is_test=False).select_related(
        'lead','lead__source','lead__company').prefetch_related('lead__documents').order_by('-observed_at','-pk')
    leads = Lead.objects.filter(research_event__isnull=True,is_test=False,duplicate_of__isnull=True).select_related('company','source')
    for records, mapper in ((candidates,keyword_card),(events,research_card),(leads,lead_card)):
        for item in records:
            card = mapper(item)
            if isinstance(item, KeywordCandidate):
                candidate_card=intake_card(item) if not card else _candidate_promotion(item,card)
                if candidate_card.get('promotion_basis') and not card:
                    summary=candidate_card['promotion_basis']
                    original=_base_card(pool='establishment',company=candidate_card['company_name'],size='unknown',
                        event_type='plant_expansion',action='项目原文已核验',location=summary['city'],
                        period=summary['target_period'],basis='发行人原件',quote=summary['action_quote'],
                        source_url=item.canonical_url,text=item.title+'\n'+summary['action_quote'],source_kind=item.provider)
                    card={**original,**candidate_card,'pool_label':dict(POOLS)['establishment'],
                          'reason':'有具名大陆项目、计划时间和已存档原件',
                          'question':'项目备货、设备到货与发运分别由哪一方安排？'}
                elif candidate_card.get('promotion_basis'):
                    card=candidate_card
            if card:
                published_company = item.lead.company if isinstance(item, ResearchEvent) else (
                    item.company if isinstance(item, Lead) else companies.get(card['company_name']))
                card.update(company_fields(published_company))
                if isinstance(item, KeywordCandidate):
                    card.update(candidate_fields(item, published_company))
                    card['candidate_id'] = item.pk
                    card['detail_url'] = reverse('radar:sales_candidate_detail',args=[item.pk])
                review = reviews.get(card['source_url'])
                if review:
                    if review['decision'] in {'downgraded','superseded'}:
                        continue
                    card['sales_recheck'] = review
                    if review.get('note'):
                        card['pending'].append(review['note'])
                identity = (card['source_url'], card['company_name'])
                if card['score'] > unique.get(identity, {}).get('score', 0):
                    unique[identity] = card
    for card in unique.values():
        pools[card['pool']].append(card)
    for cards in pools.values():
        cards.sort(key=lambda card: (-card['score'], card['size_tier'] != 'micro_sme', card['company_name'], card['source_url']))
    return [{'key':key,'label':label,'cards':pools[key],'count':len(pools[key]),'preview':pools[key][:2]}
            for key,label in POOLS]


JAPAN = re.compile(r'日企|日资|日資|日系|日本企業|日本企业|日本法人|日本の|株式会社|有限会社|合同会社|合資会社|合名会社|Japan(?:ese)?', re.I)
MOVEMENT = re.compile(r'新設|新设|設立|设立|進出|进出|扩产|扩建|増設|増強|移管|拡張|投产|稼働|新工[場厂]|'
                      r'簽約|签约|搬迁|移転|新築|輸出|輸入|出荷|补货|供货|倉庫|仓库|物流センター|物流中心|环评|拿地')


def foreign_owner(name, text):
    return bool(name and re.search(r'(?:韩国|韓国)(?:企业|企業|公司)?[ 　]*' + re.escape(name)
                                  + r'(?![A-Za-z0-9])', text, re.I))


def intake_card(candidate):
    from collectors.keyword_radar import coarse_screen, publication_date
    result = candidate.result or {}
    full = bool(candidate.text and candidate.raw_path and result.get('source_sha256'))
    content = candidate.text if full else candidate.snippet
    issuer_section = (result.get('source_evidence_quote','') if result.get('pdf_page')
                      and result.get('source_scope')=='issuer_disclosure'
                      and result.get('official_website_status')=='verified' else '')
    sample = candidate.title + '\n' + issuer_section + '\n' + content[:10000]
    source_scope=result.get('source_scope')
    if source_scope in {'sme_declaration','signed_press_release','issuer_disclosure'} and result.get('source_evidence_quote'):
        region = region_weight(result.get('china_region') or result['source_evidence_quote'])
    else:
        region = region_weight(sample)
    proposed_name = str((result.get('extraction') or {}).get('company_name') or '')
    outside_scope = foreign_owner(proposed_name, content)
    japan = not outside_scope and bool(JAPAN.search(sample) or any(hit.get('basis') != 'logistics_unknown' for hit in result.get('seed_hits',[])))
    action = bool(MOVEMENT.search(sample) or issuer_section and result.get('action_type'))
    direct_quote = result.get('source_evidence_quote','') if source_scope in {'sme_declaration','signed_press_release'} else ''
    direct_signal = bool(direct_quote and _mainland_evidence({'quote':direct_quote}))
    publication = publication_date((result.get('source_publication') or {}).get('date') or candidate.published_at)
    recent = bool(publication and recent_publication(publication.isoformat(), timezone.localdate()))
    # Raw candidates are reviewable even when dates are absent, but never become confirmed contacts.
    relevant = japan and (direct_signal if direct_quote else action and (region or _mainland_evidence({'quote':sample})))
    screening = coarse_screen(candidate.title, content)
    noise_reason = screening if screening.startswith('noise_') else ''
    ineligible = bool(noise_reason) or screening in {'noise','guide_or_policy','consumer_product'}
    stale = (candidate.provider == 'idec_overseas_cases' or result.get('historical_case')
             or result.get('source_scope') == 'regional_reference'
             or (result.get('extraction') or {}).get('event_type') in HISTORICAL
             or (publication and (timezone.localdate() - publication).days > 365))
    score = 45 + region + 4 * int(full) + 4 * int(recent) if relevant and not ineligible and not stale else 25 + region
    pending = ['企业身份／规模待核','经营动作与物流委托待核']
    review = result.get('sales_recheck')
    if review:
        if review['decision'] in {'downgraded','superseded'}:
            score = min(score,44)
        if review.get('note'):
            pending.append(review['note'])
    if noise_reason:
        pending.append({
            'noise_study_tour':'考察或参观招募，缺少自有经营动作',
            'noise_office_move':'办公室搬迁，缺少实体货流设施信息',
            'noise_anniversary':'周年庆典或回顾，缺少当期经营动作',
            'noise_industry_survey':'行业排名或调查，缺少具名落地动作',
            'noise_reverse_expansion':'赴日经营，缺少独立的大陆经营动作',
            'noise_reference_material':'名录、政策或历史资料，缺少当期具名经营动作',
            'noise_trade_show_overview':'展会总览或招募，缺少具名货主经营动作',
            'noise_third_market_investment':'第三国投资，缺少独立的大陆经营动作',
        }[noise_reason])
    if outside_scope:
        pending.append('原文列示该企业为韩资／韩国企业')
    if not full:
        pending.append('仅摘要，待人工复核原文')
    if not recent:
        pending.append('近期日期待核')
    card = {'candidate_id':candidate.pk,'title':candidate.title,'source_url':candidate.canonical_url,
            'source_kind':candidate.provider,'evidence_quote':issuer_section or direct_quote or content[:550], 'score':score,
            'company_name':proposed_name if source_excerpt(content,proposed_name) else '',

            'grade':'B' if score >= 45 else 'C','pending':pending, 'region_weight':region,
            'content_mode':'full_text' if full else 'snippet_only',
            'published_at':publication.isoformat() if publication else '',
            'publication_basis':'原文' if result.get('source_publication') else '来源摘要或索引',
            'china_location':result.get('china_region',''), 'target_period':result.get('disclosure_target_period',''),
            'status':candidate.status,'sales_recheck':review,'noise_reason':noise_reason}
    return _candidate_promotion(candidate,card)


def candidate_fields(candidate, company=None):
    result = candidate.result or {}
    output = company_fields(company)
    output['company_headquarters'] = result.get('headquarters','')
    output['company_scale'] = result.get('company_scale',{})
    if not output['company_industry']:
        output['company_industry'] = result.get('company_industry','')
    if not output['official_website'] and result.get('official_website_status')=='verified':
        site = site_root(result.get('official_website',''))
        if site:
            output.update(official_website=site,official_website_status='verified')
    elif not output['official_website'] and result.get('official_website_status')=='unverified':
        output['official_website_status']='unverified'
    return output


def intake_grades(pools):
    cards = [card for pool in pools for card in pool['cards']]
    groups = {'A':[], 'B':[], 'C':[]}
    represented = {card['source_url'] for card in cards}
    for card in cards:
        groups[card['grade']].append(card)
    companies = Company.objects.in_bulk(field_name='name')
    verified_b_urls = {card['source_url'] for grade in ('A','B') for card in groups[grade] if card.get('detail_url')}
    for candidate in KeywordCandidate.objects.exclude(canonical_url__in=represented):
        review = candidate.result.get('sales_recheck') or {}
        if review.get('decision') == 'promoted_b' and review.get('replacement_url') in verified_b_urls:
            continue
        card = intake_card(candidate)
        card['company_name'] = card['company_name'] or named_case(candidate)
        card.update(candidate_fields(candidate,companies.get(card['company_name'])))
        card['detail_url'] = reverse('radar:sales_candidate_detail',args=[candidate.pk])
        groups[card['grade']].append(card)
    for rows in groups.values():
        rows.sort(key=lambda card: (-card['score'], card['source_url']))
    return groups
