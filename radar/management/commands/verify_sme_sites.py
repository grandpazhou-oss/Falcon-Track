"""Verify declared company sites for SMRJ opportunities using the public fetcher."""
import json
from pathlib import Path

from django.core.management.base import BaseCommand
from filelock import FileLock
from django.conf import settings

from collectors.issuer_ir_pdf import retain_original, verify_root
from radar.models import KeywordCandidate
from radar.public_fetch import request_budget
from radar.sales_pools import intake_card


class Command(BaseCommand):
    help = 'Verify the declared official sites of current SMRJ B leads; no provider calls.'

    def add_arguments(self, parser):
        parser.add_argument('--online', action='store_true', required=True)
        parser.add_argument('--report', type=Path, required=True)
        parser.add_argument('--after-id', type=int, default=0, help='Resume with candidates created after this ID.')

    def handle(self, *args, **options):
        rows = []
        with FileLock(str(settings.DATA_DIR/'sme-site-verify.lock'), timeout=0), request_budget(150, 850) as budget:
            for case in KeywordCandidate.objects.filter(provider='smrj_100oku',pk__gt=options['after_id']).order_by('pk'):
                if intake_card(case)['grade'] != 'B':
                    continue
                data = case.result
                url = data.get('official_website','')
                company = data.get('extraction',{}).get('company_name','')
                row = {'id':case.pk, 'company':company, 'declared_url':url, 'requests':[]}
                rows.append(row)
                try:
                    profile = verify_root(url,company,row,probe=True) if url and company else None
                    if not profile:
                        row['status'] = 'identity_unconfirmed' if url else 'missing_declared_website'
                        continue
                    root, _, doc = profile
                    retain_original(doc)
                    case.result = {**data, 'official_website':root, 'official_website_status':'verified', 'website_evidence':doc}
                    case.save(update_fields=['result'])
                    row.update(status='verified', website=root, proof=doc)
                except (ValueError, OSError) as exc:
                    row.update(status='failed', reason=str(exc))
                finally:
                    options['report'].write_text(json.dumps({'rows':rows,'physical_requests':budget['used']},ensure_ascii=False,indent=2))
        options['report'].write_text(json.dumps({'rows':rows,'physical_requests':budget['used']},ensure_ascii=False,indent=2))
        self.stdout.write(f'{len(rows)} 个 B 级官网核验结果已保存。')
