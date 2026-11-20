import json
from django.core.management.base import BaseCommand
from radar.dedup import link_duplicates
class Command(BaseCommand):
    help='Link same-title/year/lot/stage and project-number duplicates; keep original rows and all evidence.'
    def handle(self,*args,**kwargs):self.stdout.write(json.dumps(link_duplicates()))
