import json
from django.core.management.base import BaseCommand, CommandError
from radar.source_modes import RUNNABLE, MANUAL
from radar.models import Source
from radar.services import enqueue_source, process_pending, seed_sources

class Command(BaseCommand):
    help = "Run one queued collection job, optionally enqueueing a known source."
    def add_arguments(self, parser):
        parser.add_argument("--source", choices=sorted(RUNNABLE - MANUAL.keys()))
        parser.add_argument("--max-pages", type=int, choices=range(1, 11))
    def handle(self, *args, **options):
        seed_sources()
        if options['max_pages'] and not options['source']:
            raise CommandError('--max-pages 需要指定来源')
        if options['source'] and options['max_pages']:
            run = enqueue_source(Source.objects.get(key=options['source']).pk)
            result = process_pending(target_run_id=run.pk, max_pages=options['max_pages'])
            if not result or result[0].get('status') == 'worker_busy':
                raise CommandError('工作进程正在处理其他任务；采集仍在队列中。')
        else:
            if options['source']:
                enqueue_source(Source.objects.get(key=options['source']).pk)
            result = process_pending()
        self.stdout.write(json.dumps(result, ensure_ascii=False))
        if any(x.get("status") in {"failed", "blocked"} for x in result):
            raise CommandError("来源未成功采集，请查看运行记录。")

