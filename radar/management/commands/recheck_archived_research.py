import json

from django.core.management.base import BaseCommand

from radar.archived_recheck import scan_archived_research


class Command(BaseCommand):
    help = '扫描存量正文以发现研究待核；默认仅预览。'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='保存研究条目')

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(scan_archived_research(apply=options['apply']),ensure_ascii=False))
