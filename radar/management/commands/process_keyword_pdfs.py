"""Reprocess saved PDFs; model and direct website access require explicit permission."""
import hashlib
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from filelock import FileLock, Timeout

from collectors.keyword_radar import coarse_screen
from radar.ai_config import config
from radar.keyword_dates import recent_publication
from radar.keyword_extraction import KEYWORD_PROMPT_VERSION, extract
from radar.keyword_pdf import inspect_keyword_pdf
from radar.models import KeywordCandidate, ModelCall
from radar.public_fetch import request_budget
from radar.search_provider import SearchResult
from radar.website_check import discover_official_site


class Command(BaseCommand):
    help = 'Inspect saved keyword PDFs without search; paid analysis is explicit and bounded.'

    def add_arguments(self, parser):
        parser.add_argument('--allow-model', action='store_true')
        parser.add_argument('--max-calls', type=int, default=0)
        parser.add_argument('--model-daily-limit', type=int, default=0)
        parser.add_argument('--output-dir', default='')

    def handle(self, *args, **o):
        if not 0 <= o['max_calls'] <= 4 or (o['max_calls'] and
                (not o['allow_model'] or not 1 <= o['model_daily_limit'] <= 30)):
            raise CommandError('模型需显式允许，单批最多 4 次，并提供每日上限。')
        cfg = config()
        before = ModelCall.objects.count()
        rows = []
        try:
            with FileLock(str(settings.DATA_DIR/'keyword-radar-run.lock'), timeout=0):
                for candidate in KeywordCandidate.objects.filter(status='needs_document_review').order_by('pk'):
                    row = {'id':candidate.pk, 'url':candidate.canonical_url}
                    try:
                        pdf = inspect_keyword_pdf(candidate)
                        candidate.raw_path = pdf['raw_path']
                        previous = hashlib.sha256(candidate.text.encode('utf-8')).hexdigest()
                        review = {'status':pdf['status'], 'pages':pdf['pages'], 'characters':len(pdf['text']),
                                  'first_page_year':pdf.get('first_page_year',''), 'page_lengths':pdf.get('page_lengths',[]),
                                  'source_sha256':candidate.result.get('source_sha256',''),
                                  'previous_text_sha256':previous, 'version':'pdf-text-v2', 'backend':pdf.get('backend','')}
                        candidate.result['pdf_review'] = review
                        reason = pdf['status'] if pdf['status'] not in {'text','partial'} else ''
                        if not reason:
                            candidate.text = pdf['text'][:200000]
                            if pdf.get('publication'):
                                candidate.result['source_publication'] = pdf['publication']
                            published = (pdf.get('publication') or {}).get('date', '')
                            if not published:
                                reason = ('old_publication_year' if pdf.get('first_page_year') and
                                          int(pdf['first_page_year']) < timezone.localdate().year else 'publication_unverified')
                            elif not recent_publication(published, timezone.localdate()):
                                reason = 'publication_not_recent'
                            else:
                                reason = coarse_screen(candidate.title, candidate.text)
                        review['skip_reason'] = reason
                        candidate.status = 'needs_document_review' if reason else 'ready_for_review'
                        candidate.save(update_fields=['raw_path','text','result','status'])
                        if not reason and o['allow_model'] and ModelCall.objects.count() - before < o['max_calls']:
                            if ModelCall.objects.filter(requested_at__date=timezone.localdate()).count() >= min(cfg.daily_limit,o['model_daily_limit']):
                                review['skip_reason'] = 'model_daily_limit'
                                candidate.save(update_fields=['result'])
                            else:
                                candidate.status = 'model_started'
                                candidate.save(update_fields=['status'])
                                try:
                                    value = extract(candidate,cfg,o['model_daily_limit'])
                                    candidate.result['extraction'] = value
                                    candidate.result['prompt_version'] = KEYWORD_PROMPT_VERSION
                                    candidate.status = 'model_needs_review'
                                except Exception as exc:
                                    candidate.result['model_error'] = type(exc).__name__
                                    candidate.status = 'model_failed'
                                if hasattr(candidate, '_keyword_call_id'):
                                    candidate.result['model_call_id'] = candidate._keyword_call_id
                                candidate.save(update_fields=['result','status'])
                                value = candidate.result.get('extraction', {})
                                if value.get('company_name') and value.get('is_japanese_affiliated'):
                                    item = SearchResult(value['company_name'],candidate.canonical_url,'','','direct','official_site')
                                    with request_budget(6,60):
                                        site = discover_official_site(value['company_name'],[item])
                                    if site:
                                        candidate.result['website_candidate'] = site
                                        candidate.save(update_fields=['result'])
                        row.update(status=candidate.status, pdf_review=review, raw_path=candidate.raw_path,
                                   source_publication=candidate.result.get('source_publication',{}),
                                   model_call_id=candidate.result.get('model_call_id'),
                                   error_detail=candidate.error_detail,
                                   website_candidate=candidate.result.get('website_candidate'))
                    except Exception as exc:
                        row.update(status='inspection_failed', error=type(exc).__name__+': '+str(exc)[:150])
                    rows.append(row)
        except Timeout as exc:
            raise CommandError('另一个关键词任务正在运行。') from exc
        report = {'pdfs':rows,'model_calls':ModelCall.objects.count()-before,'search_calls':0,
                  'finished_at':timezone.now().isoformat()}
        out = Path(o['output_dir'] or settings.DATA_DIR/'verification'/'keyword-pdfs')
        out.mkdir(parents=True,exist_ok=True)
        (out/'pdf-review.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        self.stdout.write(json.dumps(report,ensure_ascii=False))
