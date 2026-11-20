"""Rotating cross-company research queries. No work runs on import."""
from dataclasses import asdict
from collections import deque
from datetime import date
from email.utils import parsedate_to_datetime
import re
import unicodedata
from urllib.parse import urlsplit
from django.db import transaction
from filelock import FileLock
from django.conf import settings

from radar.china_places import MAINLAND as LOCALITY, SEARCH_CITIES as REGIONS
from radar.models import KeywordCandidate, KeywordCursor
from radar.search_provider import CHINA_TERMS, TAVILY_EXCLUDE_DOMAINS, budgeted_search, canonical_url

QUERY_BANK = (
    ('pr_east', 'site:prtimes.jp 中国 工場 OR 倉庫 OR 現地法人 OR 進出 上海 OR 蘇州 OR 常州'),
    ('park_east', 'site:gov.cn 日资 项目 签约 OR 投产 OR 环评 苏州 OR 昆山 OR 无锡 OR 常州 OR 南通'),
    ('park_south', 'site:gov.cn 日资 环评 OR 拿地 OR 扩产 广州 OR 东莞 OR 深圳 OR 佛山'),
    ('pr_south', '日本企業 工場 OR 物流センター OR 現地法人 広州 OR 東莞 OR 深圳 OR 仏山'),

    ('new_entity', '日本企業 中国現地法人 設立 発表'),
    ('expansion', '日本企業 中国 新工場 建設 稼働 発表'),
    ('sme_entry', '日本 中小企業 中国 進出 事業 発表'),
    ('park', '日资企业 {region} 项目 签约 投产 扩产 公告'),
    ('new_entity', '日本企業 {region} 子会社 新設 発表'),
    ('expansion', '日本企業 中国 物流センター 倉庫 新設 移転 発表'),
    ('sme_entry', '大阪 中小企業 中国 {industry} 進出 発表'),
    ('park', '日本独资 {region} 新工厂 环评 建设 项目'),
    ('new_entity', '日资企业 {region} 新设 子公司 项目 签约'),
    ('expansion', '日企 {region} 工厂 扩建 扩产 物流 项目'),
    ('sme_entry', '日本中小企业 中国 落地 投资 项目 企业'),
    ('park', '日本企業 中国 工業団地 工場 建設 発表'),
)
INDUSTRIES = ('製造', '食品', '化学', '機械', '電子部品')
GUIDE_TERMS = r'手順|まとめ|とは|メリット|教程|指南|步骤|如何|怎么|解説|留意点|政策解读|法律法规|法律事務所|企業法務|法人設立の流れ|注册成立手续|セミナー|ウェビナー|市場規模|企業リスト|企業名錄|企业名录|年度版|歴史|沿革|企业历史|公司历史|企業情報|会社四季報|投資信託|グローバルバランス|Integrated Report|統合報告書|成功事例|エキスパート|上市公司重大事项|发行人及保荐机构|^ニュース\s*[|｜]|物流センター運営'


NOISE_MARKERS = (
    ('reverse_expansion', r'日本初上陸|日本に会社設立|中国企業による対日投資|(?:渋谷|東京)で.{0,24}POP[- ]?UP'),
    ('study_tour', r'視察ツアー|視察団|(?:参観|考察).{0,10}招募|参加者募集'),
    ('office_move', r'(?:オフィス|事務所|本社).{0,8}移転|办公室搬迁|办公地址变更'),
    ('anniversary', r'周年.{0,6}(?:記念|祝)|記念式典|(?:創立|設立|独立|展開)\s*\d+\s*周年|周年庆典'),
    ('industry_survey', r'ランキング|業種分布|アンケート|現地調査結果|実態調査|動向調査|市場シェア分析|行业排名|'
                        r'行业调查|问卷调查|市場動向.{0,18}発表|(?:割|%|以上).{0,26}(?:と回答|が回答)|'
                        r'(?:中国進出(?:の?日本)?企業|日本企業の中国拠点|日企|日系企業).{0,34}(?:社減|社純減|減少|最少|割減|社突破|突破\d+家)'
                        r'|日本企業の.{0,4}中国離れ|JETRO.{0,20}地域・分析レポート'),
    ('reference_material', r'維基百科|维基百科|支援制度紹介パンフレット|海外販路開拓支援\s*\||'
                           r'海外拠点リスト.{0,14}ご紹介|日本公庫総研レポート|稲盛のエピソード|'
                           r'日企在.{1,12}的\d+年|日系企業が集積.{0,18}高地|日资第一高地.{0,14}为什么'),
    ('trade_show_overview', r'(?:輸入博|进博会).{0,65}(?:日本企業約?\s*\d+社|\d+家日企)|'
                            r'海外ビジネスEXPO.{0,22}(?:始動|初開催)|国際見本市.{0,18}初開催へ'),
    ('third_market_investment', r'(?:対米投資|赴美投资).{0,45}(?:ニューヨーク|アメリカ|米国|美国).{0,24}(?:工場|工厂|建設)'),
)
FLOW_TERMS = re.compile(r'倉庫|仓库|物流センター|物流中心|工場|工厂|生産拠点|生产基地|配送|在庫|库存')
LEGAL_NAME = re.compile(r'株式会社|有限会社|合同会社|有限公司|日本企業|日資企業|日资企业')
OWN_ACTION = re.compile(
    r'(?:工[場厂]|生産拠点|生产基地|倉庫|仓库|物流センター|物流中心|現地法人|子会社|販売事業会社|合弁会社|現地拠点|子公司|店舗|工厂|門店|门店)'
    r'.{0,25}(?:新設|新设|設立|设立|建設|建设|建成|着工|开工|増設|拡張|拡大|扩建|扩产|稼働|投产|開設|开业|開業|移転|搬迁)'
    r'|(?:新設|新设|設立|设立|新建|建設|扩建|投产|開始|开设|開設).{0,18}(?:工[場厂]|倉庫|仓库|物流センター|物流中心|現地法人|子会社|販売事業会社|合弁会社|店舗|門店|门店)'
    r'|新工場|新工厂|出店|開店|(?:出荷|輸出|輸入|納入|供給).{0,12}(?:開始|した|を予定)|供货|补货|追加発注|リピートオーダー|初めて.{0,20}輸[出入]'
)


COMPANY_SUBJECT = re.compile(
    r'^(?:(?:株式会社|有限会社|合同会社)[\w ・-]{2,32}|[\w ・-]{2,32}(?:株式会社|有限公司))(?=\s*[:・|\n])'
)
LOCAL_SALES = re.compile(
    r'商品.{0,8}販売.{0,8}(?:開始|実施)|製造[、,]?販売して|'
    r'現地法人(?:を持[ちつ]|運営)|(?:开始|已向).{0,18}(?:供货|补货)'
)
SALE_EVENT = re.compile(r'限定商品|限定.{0,16}(?:グッズ|商品)|限量商品')
DATED_VENUE = re.compile(
    r'(?:上海|深圳|北京|蘇州|苏州|杭州|広州|广州)\s*(?:場所|会場|地点)[^・。]{0,55}'
    r'(?:期間|開始|日期)\s*[:：]?\s*20\d{2}(?:年|[/.-])'
)
NON_ACTION = re.compile(r'関連記事|関連ニュース|おすすめ記事|関連キーワード|事例一覧|企業一覧|'
                        r'予定はない|計画はない|無計画|无计划|未计划|未供货|見送|中止|取消|撤回')


def _body_operation(body):
    # A case heading identifies its subject; action and destination come from the case text.
    subject = COMPANY_SUBJECT.match(body.lstrip())
    paragraphs = body.splitlines()
    for paragraph in paragraphs:
        if not LOCALITY.search(paragraph) or NON_ACTION.search(paragraph):
            continue
        if re.search(r'子会社[、,]\s*[\w]{2,30}\([\w]{2,12}\)(?:で|が).{0,25}工場を建設', paragraph):
            return True
        for clause in paragraph.split('。'):
            if LOCALITY.search(clause) and LOCAL_SALES.search(clause) and (subject or COMPANY_SUBJECT.match(clause)):
                return True
        if subject and re.search(r'(?:から|の).{0,16}(?:引き合い|注文)', paragraph) and re.search(
                r'追加発注を(?:頂|いただ|受)|サンプルを発送|接到追加订单', paragraph):
            return True
        if (SALE_EVENT.search(paragraph) and DATED_VENUE.search(paragraph)
                and re.search(r'(?:株式会社|有限公司).{0,180}(?:開催する|開催します|開催を)', paragraph)):
            return True
    if subject and re.search(r'中国(?:への|向けの)(?:ライブコマース|越境EC)で(?:ブレイク|販売を開始|受注を獲得)', body):
        return bool(re.search(r'対象国・地域.{0,12}商品\s+中国\s*/\s*[^\n]+', body))
    return False


def intake_noise_reason(title, content):
    """Classify headline intent, allowing source-backed mainland operations through."""
    title = unicodedata.normalize('NFKC', title or '')
    body = unicodedata.normalize('NFKC', (content or '')[:12000])
    clauses = re.split(r'[。！？\n]', body)
    article = '\n'.join(clause for clause in clauses
                        if not re.search(r'関連記事|関連ニュース|おすすめ記事|関連キーワード', clause))
    lead = '\n'.join(clauses[:3])[:350]
    for kind, pattern in NOISE_MARKERS:
        scope = title if kind != 'office_move' else title + '\n' + lead
        if not re.search(pattern, scope, re.I):
            continue
        if kind == 'office_move':
            if FLOW_TERMS.search(article) or _body_operation(body):
                continue
            return 'noise_office_move'
        if kind == 'reverse_expansion' and re.search(r'方法|手続|教えて|如何|怎么|指南', title):
            if not _body_operation(body):
                return 'noise_reverse_expansion'
        for clause in [title, *clauses]:
            years = re.findall(r'(?<!\d)(20\d{2})年', clause) if kind in {'anniversary','reference_material','industry_survey'} else []
            if years and max(map(int,years)) < date.today().year - 1:
                continue
            if (LOCALITY.search(clause) and OWN_ACTION.search(clause)
                    and (LEGAL_NAME.search(clause) or
                         (LEGAL_NAME.search(title) and re.search(r'当社|弊社|本公司',clause)))
                    and not re.search(pattern + r'|調査|報告書|募集|視察|見学|紹介|統計|ランキング|%'
                                      r'|(?:日本|東京|大阪|渋谷|佐賀)に(?:進出|設立|開設|出店)'
                                      r'|を振り返|当時|創業以来|予定はない|計画はない|无计划|未计划', clause, re.I)):
                break
        else:
            if not _body_operation(body):
                return 'noise_' + kind
    return ''


def next_query():
    with FileLock(str(settings.DATA_DIR / 'keyword-cursor.lock'), timeout=5), transaction.atomic():
        state, _ = KeywordCursor.objects.get_or_create(key='default')
        index = state.offset
        category, template = QUERY_BANK[index % len(QUERY_BANK)]
        cycle = index // len(QUERY_BANK)
        region = REGIONS[cycle % len(REGIONS)]
        industry = INDUSTRIES[(cycle // len(REGIONS)) % len(INDUSTRIES)]
        state.offset = index + 1
        state.save(update_fields=['offset'])
    return f'{category}:{index}', template.format(region=region, industry=industry)


def collect(provider, query, query_id, daily_limit):
    results = budgeted_search(provider, query, query_id, daily_limit)
    from collectors.wide_intake import store_candidate
    added = sum(store_candidate(url=item.url, title=item.title, summary=item.snippet,
                                published=item.published_at, provider=item.provider, query_id=item.query_id,
                                discovery={'discovery_feed':getattr(provider,'last_stats',{})})
                for item in results)
    return {'returned': len(results), 'added': added, 'urls': [item.url for item in results],
            **getattr(provider, 'last_stats', {})}


def round_robin_candidates(candidates, limit):
    groups = {}
    for candidate in candidates:
        groups.setdefault(candidate.query_id, deque()).append(candidate)
    active = deque(groups.values())
    selected = []
    while active and len(selected) < limit:
        group = active.popleft()
        selected.append(group.popleft())
        if group:
            active.append(group)
    return selected


def score_candidate(title, snippet, *, original=None):
    if intake_noise_reason(title, original if original is not None else snippet):
        return -10
    text = (title or '') + ' ' + (snippet or '')[:4000]
    score = 2 if CHINA_TERMS.search(text) else -2
    if re.search(r'(?:株式会社|有限会社|合同会社)\s*[^\s、。，]{2,20}|[\w\u4e00-\u9fffァ-ヺ]{2,20}(?:株式会社|有限公司|股份公司)', text):
        score += 4
    if re.search(r'新工場|新工厂|新建工厂|工場.*(?:建設|新設|増設)|工厂.*(?:扩产|扩建|新建)|物流センター|物流中心|倉庫.*(?:新設|移転)|仓库.*(?:新建|搬迁)|現地法人.*設立|子会社.*設立|子公司.*设立|项目.*(?:签约|投产)', text, re.I):
        score += 5
    elif re.search(r'設立|设立|新設|新设|拡張|扩产|扩建|進出|进出|物流|投产', text, re.I):
        score += 2
    if str(date.today().year) in text:
        score += 2
    if re.search(GUIDE_TERMS, title or '', re.I):
        score -= 8
    return score


def publication_date(value):
    value = str(value or '').strip()
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        try:
            return parsedate_to_datetime(value).date()
        except (ValueError, TypeError, OverflowError):
            return None


def candidate_priority(candidate):
    score = score_candidate(candidate.title, candidate.snippet, original=candidate.text or None)
    host = (urlsplit(candidate.canonical_url).hostname or '').lower()
    if any(host == domain or host.endswith('.' + domain) for domain in TAVILY_EXCLUDE_DOMAINS):
        score -= 12
    publication = publication_date((candidate.result or {}).get('source_publication', {}).get('date') or candidate.published_at)
    if publication:
        age = (date.today() - publication).days
        score += 4 if 0 <= age <= 90 else -6 if age > 365 else 0
    return score


def publication_from_tree(tree):
    names = {'pubdate','publishdate','datepublished','datepublishedutc','article:published_time','publication_date','dc.date.issued'}
    for node in tree.xpath('//meta[@content]'):
        field = node.get('name') or node.get('property') or node.get('itemprop') or ''
        if field.lower() not in names:
            continue
        match = re.match(r'\s*(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})', node.get('content',''))
        if match:
            try:
                value = date(*map(int,match.groups())).isoformat()
            except ValueError:
                continue
            return {'date':value,'field':'meta:'+field}
    for node in tree.xpath('//time[@datetime]'):
        label = ' '.join((node.get('itemprop', ''), node.get('class', ''), node.get('id', ''))).lower()
        if node.get('pubdate') is None and not re.search(r'published|publish|pubdate', label):
            continue
        value = publication_date(node.get('datetime'))
        if value:
            return {'date':value.isoformat(),'field':'time:published_datetime'}
    return {}


def publication_from_text(text):
    match = re.search(r'(发布时间|发布日期|公開日|掲載日|発表日|投稿日)\s*[:：]?\s*(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})', (text or '')[:6000])
    if match:
        try:
            return {'date':date(*map(int, match.groups()[1:])).isoformat(), 'field':'text:' + match.group(1)}
        except ValueError:
            pass
    return {}


def coarse_screen(title, text, *, require_company=False):
    reason = intake_noise_reason(title, text)
    if reason:
        return reason
    subject = title + '\n' + text[:5000]
    if re.search(r'招聘|求人|採用|転職|职位|股价|股票|株価|旅游|旅行|観光', title, re.I):
        return 'noise'
    if re.search(GUIDE_TERMS, title, re.I):
        return 'guide_or_policy'
    if re.search(r'SSD|スマートフォン|手机|イヤホン|ヘッドホン|ノートパソコン|テレビ|家電', title, re.I) and not re.search(r'工場|工厂|倉庫|仓库|物流センター', title):
        return 'consumer_product'
    if not CHINA_TERMS.search(subject):
        return 'no_china_evidence'
    if not re.search(r'設立|设立|新設|新设|進出|进出|扩建|扩产|建設|建设|投産|投产|新工場|工場|工厂|倉庫|仓库|物流|子会社|子公司|法人|事業所|生产|生産|合弁|合资|提携|合作|輸出|出口|稼働|拡張|拡充', subject, re.I):
        return 'no_event_evidence'
    if require_company and not re.search(r'株式会社|有限会社|合同会社|股份有限公司|有限公司|有限責任公司|有限责任公司|\b(?:Inc|Ltd)\.', subject, re.I):
        return 'no_named_company'
    return ''
