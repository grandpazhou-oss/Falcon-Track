"""Run the explicitly enabled keyword discovery loop in a bounded local batch."""
import json
import hashlib
import re
import httpx
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from filelock import FileLock, Timeout

from collectors.keyword_radar import candidate_priority, collect, coarse_screen, next_query, publication_date, publication_from_text, publication_from_tree, round_robin_candidates
from collectors.research_common import read_original
from collectors.sources import node_text
from radar.ai_config import config
from radar.keyword_dates import recent_publication
from radar.keyword_extraction import KEYWORD_PROMPT_VERSION, extract
from radar.management.commands.backup_data import raw_source_file
from radar.models import Company, KeywordCandidate, ModelCall, SearchCall
from radar.public_fetch import request_budget
from radar.search_provider import CHINA_TERMS, DIRECT_URLS, EVENT_TERMS, budgeted_search, canonical_url, provider_for
from radar.website_check import discover_official_site, probe_official_site, public_html_tree


class Command(BaseCommand):
    help = 'Explicitly run a bounded keyword research batch. Network and models are off by default.'

    def add_arguments(self, parser):
        parser.add_argument('--online', action='store_true')
        parser.add_argument('--existing-only', action='store_true')
        parser.add_argument('--review-intake', action='store_true', help='Review top A/B intake with saved originals only.')
        parser.add_argument('--allow-model', action='store_true')
        parser.add_argument('--provider', choices=['bing','google','brave','tavily','rss','direct',*DIRECT_URLS], default='direct')
        parser.add_argument('--locale', choices=['ja-JP','zh-CN'], default='ja-JP')
        parser.add_argument('--max-search', type=int, default=0)
        parser.add_argument('--max-model', type=int, default=0)
        parser.add_argument('--max-details', type=int, default=10)
        parser.add_argument('--search-daily-limit', type=int, default=0)
        parser.add_argument('--model-daily-limit', type=int, default=0)
        parser.add_argument('--company', default='')
        parser.add_argument('--output-dir', default='')

    def handle(self, *args, **o):
        if not o['online'] or (o['existing_only'] and (o['max_search'] != 0 or o['company'])) or (not o['existing_only'] and (not 1 <= o['max_search'] <= 15 or not 1 <= o['search_daily_limit'] <= 30)):
            raise CommandError('需显式指定 --online、--max-search 1..15 和 --search-daily-limit 1..30。')
        if not 0 <= o['max_model'] <= 10 or not 0 <= o['max_details'] <= 50:
            raise CommandError('模型最多 10 次，详情最多 50 条。')
        if o['max_model'] and (not o['allow_model'] or not 1 <= o['model_daily_limit'] <= 30):
            raise CommandError('模型需显式指定 --allow-model 和 --model-daily-limit 1..30；单批最多 10 次。')
        if o['company'] and (o['provider'] == 'direct' or o['provider'] in DIRECT_URLS):
            raise CommandError('官网搜索需显式选择 bing、google、brave 或 tavily。')
        if o['review_intake'] and not (o['existing_only'] and o['allow_model'] and o['provider'] == 'tavily'):
            raise CommandError('--review-intake 需配合 --existing-only --allow-model --provider tavily')
        cfg = config()
        search_limit = min(cfg.search_daily_limit, o['search_daily_limit'])
        providers = (['bing','google'] if o['provider'] == 'rss' else
                     list(DIRECT_URLS) if o['provider'] == 'direct' else [o['provider']])
        report = {'started_at':timezone.now().isoformat(), 'queries':[], 'details':[],
                  'max_search':o['max_search'], 'max_model':o['max_model'], 'model_calls':0,
                  'valid_new_verified_leads':0, 'note':'候选和模型输出均须人工核验，非采购事实。'}
        before_search = SearchCall.objects.count()
        before_model = ModelCall.objects.count()
        try:
            with FileLock(str(settings.DATA_DIR / 'keyword-radar-run.lock'), timeout=0):
                # Physical requests, including robots and redirects, have a separate hard cap.
                with request_budget(max(10, o['max_search'] * 6 + o['max_details'] * 5), 480) as budget:
                    if o['existing_only']:
                        self.run_candidates(o, cfg, providers, search_limit, report)
                    else:
                        self.run_batch(o, cfg, providers, search_limit, report)
                    report['physical_requests'] = budget['used']
        except Timeout as exc:
            raise CommandError('另一个关键词任务正在运行。') from exc
        report['search_calls'] = SearchCall.objects.count() - before_search
        report['model_calls'] = ModelCall.objects.count() - before_model
        report['finished_at'] = timezone.now().isoformat()
        report['candidate_status_counts'] = {}
        for status in KeywordCandidate.objects.values_list('status', flat=True):
            report['candidate_status_counts'][status] = report['candidate_status_counts'].get(status, 0) + 1
        out = Path(o['output_dir'] or (settings.DATA_DIR / 'verification' / 'phase1')).resolve()
        out.mkdir(parents=True, exist_ok=True)
        (out/'PHASE1_RADAR_SMOKE.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        funnel = {'queries':report['queries'], 'details':report['details'], 'pending_scored':report.get('pending_scored', []),
                  'search_calls':report['search_calls'], 'model_calls':report['model_calls'],
                  'candidate_status_counts':report['candidate_status_counts'],
                  'valid_new_verified_leads':report['valid_new_verified_leads']}
        (out/'source-funnel.json').write_text(json.dumps(funnel, ensure_ascii=False, indent=2), encoding='utf-8')
        self.stdout.write(json.dumps(report, ensure_ascii=False))

    def run_batch(self, o, cfg, providers, search_limit, report):
        if o['company']:
            provider = provider_for(providers[0], o['locale'])
            query = o['company'] + ' 会社概要 公式'
            try:
                results = budgeted_search(provider, query, 'official_site', search_limit)
                site = discover_official_site(o['company'], results)
                report['queries'].append({'query_id':'official_site', 'query':query, 'status':'succeeded', 'returned':len(results), **getattr(provider, 'last_stats', {})})
                report['company_site'] = site
                if site:
                    report['company_site_findings'] = probe_official_site(site)
                    company = Company.objects.filter(name=o['company']).first()
                    if company:
                        profile = dict(company.profile or {})
                        records = list(profile.get('keyword_website_candidates', []))
                        if not any(row.get('url') == site['url'] for row in records):
                            records.append({**site, 'findings':report['company_site_findings']})
                            profile['keyword_website_candidates'] = records
                            company.profile = profile
                            company.save(update_fields=['profile'])
            except Exception as exc:
                report['queries'].append({'query_id':'official_site', 'status':'failed', 'error':str(exc)[:150]})
        remaining = o['max_search'] - len(report['queries'])
        direct = all(name in DIRECT_URLS for name in providers)
        if direct:
            remaining = min(remaining, len(providers))
        for index in range(remaining):
            name = providers[index % len(providers)]
            query_id, query = ('direct:' + name, DIRECT_URLS[name]) if direct else next_query()
            provider = None
            try:
                provider = provider_for(name, o['locale'])
                result = collect(provider, query, query_id, search_limit)
                report['queries'].append({'query_id':query_id, 'query':query, 'provider':name, 'status':'succeeded', **result})
            except Exception as exc:
                report['queries'].append({'query_id':query_id, 'query':query, 'provider':name,
                                          'status':'failed', 'error':str(exc)[:150],
                                          **getattr(provider, 'last_stats', {})})
                if str(exc) in {'search_daily_limit','total_request_limit','time_limit'}:
                    break
                if isinstance(exc,httpx.HTTPStatusError) and exc.response.status_code in {401,403,429}:
                    break
        if not any(item.get('status') == 'succeeded' for item in report['queries']):
            return
        self.run_candidates(o, cfg, providers, search_limit, report)

    def run_candidates(self, o, cfg, providers, search_limit, report):
        direct = all(name in DIRECT_URLS for name in providers)
        calls = 0
        pending = list(KeywordCandidate.objects.filter(status__in=['discovered','ready_for_review']).order_by('id'))
        pending.sort(key=lambda item: (-candidate_priority(item), item.pk))
        report['pending_scored'] = [{'id':item.pk, 'status':item.status, 'score':candidate_priority(item)} for item in pending]
        selected = ([item for item in pending if candidate_priority(item) >= 3][:o['max_details']]
                    if o['provider'] == 'tavily' else
                    round_robin_candidates((item for item in pending if item.status == 'discovered'), o['max_details']))
        if o.get('review_intake'):
            from radar.sales_pools import intake_card
            eligible = [(intake_card(item),item) for item in pending if item.raw_path and item.text
                        and not item.result.get('model_call_id')]
            eligible.sort(key=lambda pair: (-pair[0]['score'], -candidate_priority(pair[1]), pair[1].pk))
            selected = [item for card,item in eligible if card['grade'] == 'B'][:o['max_details']]
        for candidate in selected:
            record = {'id':candidate.pk, 'url':candidate.canonical_url, 'score':candidate_priority(candidate),
                      'previous_status':candidate.status, 'fetch_attempted':candidate.status == 'discovered'}
            try:
                if candidate.status == 'discovered' and not candidate.raw_path:
                    response, evidence = read_original(candidate.canonical_url, max_bytes=2_000_000, timeout=20)
                    final = canonical_url(response['url'])
                    if not final:
                        raise ValueError('invalid_final_url')
                    other = KeywordCandidate.objects.filter(canonical_url=final).exclude(pk=candidate.pk).first()
                    if other:
                        candidate.status = 'duplicate_redirect'
                        candidate.result = {'duplicate_of': other.pk}
                        candidate.save(update_fields=['status','result'])
                        record['status'] = candidate.status
                        report['details'].append(record)
                        continue
                    candidate.canonical_url = final
                    if response['body'].startswith(b'%PDF-'):
                        candidate.raw_path = evidence['raw_path']
                        candidate.result.update({'source_sha256':evidence['sha256'], 'source_url':final,
                                                 'rule_reason':'pdf_not_html'})
                        candidate.status = 'needs_document_review'
                        candidate.save(update_fields=['canonical_url','raw_path','status','result'])
                        record.update(status=candidate.status, result=candidate.result)
                        report['details'].append(record)
                        continue
                    tree = public_html_tree(response['body'])
                    publication = publication_from_tree(tree)
                    for node in tree.xpath('//script|//style|//noscript'):
                        node.drop_tree()
                    candidate.text = node_text(tree)[:200000]
                    candidate.raw_path = evidence['raw_path']
                    candidate.result.update({'source_sha256':evidence['sha256'], 'source_url':final})
                    if publication:
                        candidate.result['source_publication'] = publication
                        if not candidate.published_at:
                            candidate.published_at = publication['date']
                if candidate.text.startswith('%PDF-'):
                    candidate.status = 'needs_document_review'
                    candidate.result['rule_reason'] = 'pdf_not_html'
                    candidate.save(update_fields=['canonical_url','raw_path','status','result'])
                    record.update(status=candidate.status, result=candidate.result)
                    report['details'].append(record)
                    continue
                if not candidate.result.get('source_publication'):
                    publication = publication_from_text(candidate.text)
                    if publication:
                        candidate.result['source_publication'] = publication
                reason = coarse_screen(candidate.title, candidate.text, require_company=candidate.provider in DIRECT_URLS)
                candidate.status = ('needs_company_review' if reason == 'no_named_company'
                                    else 'rule_rejected' if reason else 'ready_for_review')
                candidate.result['rule_reason'] = reason
                candidate.result.pop('model_skip_reason', None)
                candidate.save(update_fields=['canonical_url','text','raw_path','status','result','published_at'])
                skip = ''
                if not reason and o['allow_model'] and o['provider'] == 'tavily':
                    from radar.sales_pools import intake_card
                    if intake_card(candidate)['grade'] != 'B' or not candidate.raw_path:
                        skip = 'outside_model_review_scope'
                    elif ModelCall.objects.filter(requested_at__date=timezone.localdate()).count() >= min(cfg.daily_limit, o['model_daily_limit']):
                        skip = 'model_daily_limit'
                if not reason and o['allow_model'] and calls < o['max_model'] and not skip:
                    raw = raw_source_file(candidate.raw_path).read_bytes()
                    if hashlib.sha256(raw).hexdigest() != candidate.result.get('source_sha256'):
                        raise ValueError('saved_source_hash_mismatch')
                    # Save this state before the request so a crash cannot replay a paid call.
                    candidate.status = 'model_started'
                    candidate.save(update_fields=['status'])
                    calls += 1
                    try:
                        value = extract(candidate, cfg, o['model_daily_limit'])
                        candidate.result['extraction'] = value
                        candidate.result['prompt_version'] = KEYWORD_PROMPT_VERSION
                        candidate.status = 'model_needs_review'
                    except Exception as exc:
                        candidate.result['model_error'] = type(exc).__name__
                        candidate.status = 'model_failed'
                    if hasattr(candidate, '_keyword_call_id'):
                        candidate.result['model_call_id'] = candidate._keyword_call_id
                    candidate.save(update_fields=['status','result'])
                if skip:
                    candidate.result['model_skip_reason'] = skip
                    candidate.save(update_fields=['result'])
                value = candidate.result.get('extraction', {})
                if (not direct and value.get('company_name') and value.get('is_japanese_affiliated')
                        and len(report['queries']) < o['max_search']):
                    query = value['company_name'] + ' 会社概要 公式'
                    try:
                        provider = provider_for(providers[0], o['locale'])
                        matches = budgeted_search(provider, query, 'official_site', search_limit)
                        site = discover_official_site(value['company_name'], matches)
                        report['queries'].append({'query_id':'official_site', 'query':query,
                                                  'status':'succeeded', 'returned':len(matches),
                                                  **getattr(provider, 'last_stats', {})})
                        if site:
                            candidate.result['website_candidate'] = site
                            candidate.result['website_findings'] = probe_official_site(site)
                            candidate.save(update_fields=['result'])
                    except Exception as exc:
                        report['queries'].append({'query_id':'official_site', 'status':'failed',
                                                  'error':type(exc).__name__ + ': ' + str(exc)[:100]})
                record['status'] = candidate.status
                record['raw_path'] = candidate.raw_path
                record['result'] = candidate.result
                if candidate.error_detail:
                    record['error_detail'] = candidate.error_detail
            except Exception as exc:
                fallback = (candidate.provider == 'tavily' and bool(candidate.snippet.strip()) and
                            str(exc) in {'http_403','timeout','robots_or_robots_unavailable:http_403',
                                         'robots_or_robots_unavailable:invalid_format'})
                candidate.status = 'snippet_review' if fallback else 'fetch_failed'
                if fallback:
                    candidate.result['content_mode'] = 'snippet_only'
                candidate.result['error'] = type(exc).__name__ + ': ' + str(exc)[:150]
                candidate.save(update_fields=['status','result'])
                record.update(status=candidate.status, error=candidate.result['error'])
            report['details'].append(record)
