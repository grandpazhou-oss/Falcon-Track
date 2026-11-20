"""Route saved, noisy-title evidence to research without changing sales candidates."""
import calendar
from collections import defaultdict
from datetime import date, timedelta
from hashlib import sha256
import re
import unicodedata

from django.core.management.base import CommandError
from django.utils import timezone

from collectors.keyword_radar import LOCALITY, NON_ACTION, NOISE_MARKERS, OWN_ACTION
from .management.commands.backup_data import raw_source_file
from .models import KeywordCandidate, ResearchEvent, Source
from .research import _mainland_evidence, ingest_research_record
from .search_provider import canonical_url

LEGAL = r'株式会社|有限会社|合同会社|有限公司'
NAME = re.compile(rf'(?:{LEGAL})[\w・]{{2,26}}?|[\w・（）()]{{2,26}}(?:{LEGAL})')
SUBJECT = r'(?:[（(][^）)\n]{1,24}[）)]\s*)?(?:は|が|では|宣布|已|将|在|计划|擬|拟|于)'
NAME_WITH_SUBJECT = re.compile(rf'(?:{NAME.pattern})(?=\s*{SUBJECT})')
OPERATION = re.compile(OWN_ACTION.pattern + r'|事業を開始|開設した|再出店|操業|営業を開始|供貨|發貨|发货|扩产|扩建|仓库.{0,14}启用|生産能力.{0,15}増強|筹建|(?:上海|中国|蘇州|苏州|天津|深圳)へ進出')
COMPLETED = re.compile(r'した|してい[るま]|しました|を開始|が稼働|を操業|を開設|を設立|追加発注を(?:受|頂|いただ)|'
                       r'已|正式|投产|竣工|开业|运营|完成|してきた|を増強し|へ進出[。！]$')
PLAN = re.compile(r'予定|計画(?![・、]\s*設計)|方針|预计|计划|拟|将于')
NOT_EVIDENCE = re.compile(NON_ACTION.pattern + r'|(?:を|お)すすめ|方法|手続|ガイド|手順|例として|例えば|どうすれば|'
                          r'という回答|アンケート回答|討論|调查显示|業種分布|事業者向け|未(?:完成|投产|开业|签约)|尚未|没有|不再|を取りやめ')
OUTSIDE = re.compile(r'(?:日本国内|渋谷|東京都|韓国|韩国|タイ|ベトナム|越南|米国|美国|台湾|香港|澳门|澳門)'
                     r'(?:に|で|向けに|へ|当地|开设|建厂)')
DATE = re.compile(r'(?<!\d)((?:19|20)\d{2})\s*(?:[年./-]\s*(\d{1,2})(?:\s*[月./-]\s*(\d{1,2}))?(?:月|日)?|年(?!度))')


def _date_fields(quote):
    found = DATE.search(unicodedata.normalize('NFKC', quote))
    if found:
        year, month, day = (int(v) if v else None for v in found.groups())
        try:
            if day:
                return date(year, month, day), f'{year:04d}-{month:02d}-{day:02d}', 'day'
            if month:
                return date(year, month, calendar.monthrange(year, month)[1]), f'{year:04d}-{month:02d}', 'month'
            return date(year, 12, 31), str(year), 'year'
        except ValueError:
            pass
    return None


def _archived_material(candidate):
    if (candidate.text and candidate.raw_path and candidate.result.get('source_sha256')
            and candidate.result.get('content_mode') != 'snippet_only'):
        return {'text':candidate.text,'raw_path':candidate.raw_path,
                'sha256':candidate.result['source_sha256'],'url':candidate.canonical_url}
    recovered = candidate.result.get('research_recovery') or {}
    if all(recovered.get(k) for k in ('text','raw_path','sha256')) and canonical_url(recovered.get('url') or ''):
        return recovered
    return {}


def inspect_archived_candidate(candidate, today):
    """Return exact archived excerpts or a reason why the candidate stays in observation."""
    title = unicodedata.normalize('NFKC', candidate.title)
    hits = [kind for kind, pattern in NOISE_MARKERS if re.search(pattern, title, re.I)]
    if not hits:
        return {'status':'title_clear'}
    output = {'status':'no_named_operation','title_hits':hits}
    material = _archived_material(candidate)
    if not material:
        return {**output, 'status':'no_original'}
    try:
        if sha256(raw_source_file(material['raw_path']).read_bytes()).hexdigest() != material['sha256']:
            return {**output,'status':'original_unverified'}
    except (OSError, CommandError):
        return {**output,'status':'original_unverified'}

    text = material['text'][:30000]
    names = {line.strip() for line in text.splitlines() if NAME.fullmatch(line.strip())}
    names.update(re.sub(r'^.*?(?:グループ会社|現地法人|子会社)である', '', match[0])
                 for match in NAME_WITH_SUBJECT.finditer(text))
    extracted = str((candidate.result.get('extraction') or {}).get('company_name') or '').strip()
    if 2 <= len(extracted) <= 50 and extracted not in {'中国企業','日本企業','中国企业','日本企业','当社','本公司','未公表'}:
        names.add(extracted)
    for quote in re.split(r'(?<=[。！？])|\n', text):
        quote = quote.strip()
        normalized = unicodedata.normalize('NFKC', quote)
        if (not quote or len(quote)>420 or normalized == title or NOT_EVIDENCE.search(normalized)
                or OUTSIDE.search(normalized) or not LOCALITY.search(normalized)
                or not _mainland_evidence({'quote':normalized}) or not OPERATION.search(normalized)):
            continue
        planned = bool(PLAN.search(normalized))
        if not planned and not COMPLETED.search(normalized):
            continue
        name = next((n for n in sorted(names,key=lambda n:(-len(n),n)) if n in text and
                     any(re.search(re.escape(alias)+r'\s*'+SUBJECT, quote)
                         for alias in (n,re.sub(LEGAL,'',n)) if len(alias)>=2)), None)
        if not name:
            continue
        evidence = {'quote':quote,'location':'存量正文经营句'}
        dates = []
        when = _date_fields(quote)
        status = 'pending'
        if when:
            end,value,precision = when
            dates = [{**evidence,'kind':'planned' if planned else 'event_date',
                      'status':'planned' if planned else 'actual','value':value,'precision':precision}]
            if planned and end<today:
                status = 'overdue_plan'
            elif not planned and end<today-timedelta(days=365):
                status = 'historical_fact'
        return {**output,'status':status,'company':name,'evidence':evidence,'dates':dates}
    return output


def _company_key(name):
    return re.sub(rf'{LEGAL}|\s+', '', unicodedata.normalize('NFKC', name)).casefold()


def scan_archived_research(*, apply=False):
    today = timezone.localdate()
    existing = defaultdict(dict)
    for pk,url,people in ResearchEvent.objects.filter(lead__duplicate_of__isnull=True,
            lead__is_test=False).order_by('pk').values_list('pk','lead__url','participants'):
        for person in people:
            existing[canonical_url(url)].setdefault(_company_key(person.get('name') or ''),pk)
    rows = []
    source = None
    for candidate in KeywordCandidate.objects.order_by('pk').iterator(chunk_size=200):
        result = inspect_archived_candidate(candidate, today)
        if result['status']=='title_clear':
            continue
        url = canonical_url(candidate.canonical_url)
        row = {**result,'candidate_id':candidate.pk,'title':candidate.title,'url':url,'created':False}
        rows.append(row)
        if not url or 'evidence' not in result:
            continue
        material = _archived_material(candidate)
        evidence_url = canonical_url(material['url'])
        if evidence_url != url:
            row['evidence_url'] = evidence_url
        name = result['company']
        company_key = _company_key(name)
        prior = existing[evidence_url].get(company_key) or existing[url].get(company_key)
        if prior:
            row['research_id'] = prior
            continue
        if not apply:
            continue
        if source is None:
            source,_ = Source.objects.get_or_create(key='archived-evidence-review',defaults={
                'name':'存量原文人工复核','kind':'early_signal','enabled':False,
                'note':'存量公开原文复核；无自动采集适配器。'})
        evidence = result['evidence']
        lead,new,_ = ingest_research_record(source,{
            'external_id':f'candidate-{candidate.pk}','title':candidate.title,'url':evidence_url,'text':material['text'],
            'document':{'raw_path':material['raw_path'],'sha256':material['sha256'],
                        'source_level':'archived_text_pending_review'},
            'research':{
                'event_type':'research_pending' if result['status']=='pending' else 'historical_only',
                'workstream':'existing_in_china','china_scope':'mainland','china_evidence':evidence,
                'participants':[{'name':name,'role':'unknown','quote':name,'location':'存量正文主体'}],
                'facts':[{**evidence,'field':'china_action','value':'存量原文中的经营动作，待人工核对'}],
                'dates':result['dates'],
                'next_step':'核对公告主体、项目阶段和当前物流安排。'}})
        row.update(research_id=lead.research_event.pk,created=new)
        existing[evidence_url][company_key] = lead.research_event.pk
    return rows
