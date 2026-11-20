"""Manual China-entry collection with explicit public and provider budgets."""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from filelock import FileLock, Timeout

from collectors.china_entry_sources import DEFINITIONS, collect_group, recover_tdnet, resolve_issuer_hits, run_mimo, search_groups
from radar.public_fetch import request_budget


class Command(BaseCommand):
    help = 'Collect verified public China-entry sources; network requires --online.'
    def add_arguments(self, parser):
        parser.add_argument('--online', action='store_true')
        parser.add_argument('--probe-report', type=Path, required=True)
        parser.add_argument('--group', action='append', choices=DEFINITIONS)
        parser.add_argument('--details', type=int, default=5)
        parser.add_argument('--issuer-expansion', action='store_true')
        parser.add_argument('--smrj-batch', action='store_true', help='Unread overseas SMRJ declarations, mainland affiliates first; free rules only.')
        parser.add_argument('--listed-names', type=Path, help='Local audited list of normalized listed issuers for SMRJ filtering.')
        chase=parser.add_mutually_exclusive_group()
        chase.add_argument('--issuer-resolve', type=Path)
        chase.add_argument('--issuer-tdnet', type=Path)
        parser.add_argument('--tavily', action='store_true')
        parser.add_argument('--issuer-url',action='append',default=[])
        parser.add_argument('--media-url',action='append',default=[])
        parser.add_argument('--mimo', type=int, default=0)
        parser.add_argument('--report', type=Path, required=True)

    def handle(self,*args,**options):
        expansion=options['issuer_expansion']
        batch=options['smrj_batch']
        if batch and (options['group']!=['smrj_100oku'] or any(options[k] for k in ('tavily','mimo','issuer_url','media_url','issuer_expansion'))):
            raise CommandError('SMRJ 批量必须单独运行，仅使用免费规则。')
        if (options['issuer_resolve'] or options['issuer_tdnet']) and not expansion:
            raise CommandError('原件追溯要求自动发行人模式。')
        if expansion and (options['group'] != ['issuer_ir_pdf'] or options['mimo'] or options['issuer_url'] or options['media_url'] or not options['tavily']):
            raise CommandError('细分词包仅用于发行人源，高潜结果随后走项目取证队列。')
        if not options['online'] or not 1<=options['details']<=(70 if expansion else 50 if batch else 12) or not 0<=options['mimo']<=30:
            raise CommandError('请显式启用 --online，常规详情上限 12，SMRJ 批量上限 50，细分词包详情上限 70，MiMo 0–30。')
        if options['listed_names'] and not batch:
            raise CommandError('证券名单仅用于 SMRJ 批量。')
        listed_names=json.loads(options['listed_names'].read_text()) if options['listed_names'] else []
        if not isinstance(listed_names,list) or not all(isinstance(name,str) for name in listed_names):
            raise CommandError('证券名单须为字符串数组。')
        probes=json.loads(options['probe_report'].read_text())
        groups=options['group'] or list(DEFINITIONS)
        try:
            with FileLock(str(settings.DATA_DIR/'china-entry-run.lock'),timeout=0):
                with request_budget(240 if expansion else 100, 1100 if expansion else 1000 if batch else 540) as budget:
                    if options['issuer_tdnet']:
                        hits,search=recover_tdnet(json.loads(options['issuer_tdnet'].read_text()))
                        found={'issuer_ir_pdf':hits}
                    elif options['issuer_resolve']:
                        hits,search=resolve_issuer_hits(json.loads(options['issuer_resolve'].read_text())['search'])
                        found={'issuer_ir_pdf':hits}
                    else:
                        found,search=search_groups(probes,groups,expansion=expansion) if options['tavily'] else ({},[])
                    if expansion:
                        options['report'].write_text(json.dumps({'search':search},ensure_ascii=False,indent=2))
                    manual={'issuer_ir_pdf':options['issuer_url'],'prd_media_discovery':options['media_url']}
                    runs=[collect_group(k,probes,[(url,'') for url in manual.get(k,())]+list(found.get(k,())),
                                        options['details'],expansion=expansion,smrj_batch=batch,listed_names=listed_names) for k in groups]
                ids=[pk for row in runs for pk in row['created']]
                result={'sources':runs,'search':search,'mimo':run_mimo(ids,options['mimo']),
                        'physical_requests':budget['used']}
                options['report'].write_text(json.dumps(result,ensure_ascii=False,indent=2))
                self.stdout.write(json.dumps({k:v for k,v in result.items() if k!='sources'},ensure_ascii=False))
        except Timeout as exc:
            raise CommandError('另一项四源采集正在运行。') from exc
