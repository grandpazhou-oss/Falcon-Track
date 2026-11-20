from django.core.management.base import BaseCommand
from radar.models import Source
from radar.suzhou_crosscheck import enrich_suzhou
import json

class Command(BaseCommand):
    help = '可选：为苏州名录增强公开官网证据；不影响名录入库，不调用模型'
    def handle(self, **options):
        source = Source.objects.get(key='japan_directory_suzhou_checked')
        self.stdout.write(json.dumps(enrich_suzhou(source),ensure_ascii=False))
