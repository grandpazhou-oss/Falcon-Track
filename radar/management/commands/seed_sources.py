from django.core.management.base import BaseCommand
from radar.services import seed_sources

class Command(BaseCommand):
    help = "Register public sources and enterprise directories, disabled until enabled by the user."
    def handle(self, *args, **options):
        seed_sources()
        self.stdout.write(self.style.SUCCESS("已登记公开来源；自动采集默认关闭。"))

