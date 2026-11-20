from django.core.management.base import BaseCommand
from radar.seed_dictionary import export_dictionary

class Command(BaseCommand):
    help = '从已有名录证据导出本机日企检索词典，不调用模型'
    def handle(self, **options):
        data = export_dictionary()
        self.stdout.write(f"companies={data['company_count']} terms={len(data['terms'])}")
