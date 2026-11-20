from django.core.management.base import BaseCommand
from radar.analysis import process_pending_analyses
class Command(BaseCommand):
    help='Process queued analysis jobs once, using the same safe worker as Prefect.'
    def add_arguments(self,parser):parser.add_argument('--limit',type=int,default=1)
    def handle(self,*args,**options):
        for run in process_pending_analyses(max(1,min(30,options['limit']))):
            if run:self.stdout.write(f'{run.pk}: {run.status} {run.error_code}')
