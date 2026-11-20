from django.core.management.base import BaseCommand
from radar.models import Lead
from radar.evidence import reextract
class Command(BaseCommand):
    help='Re-extract existing evidence without modifying acquisition hashes, original documents or human feedback.'
    def add_arguments(self,parser):parser.add_argument('--lead',type=int)
    def handle(self,*args,**options):
        leads=Lead.objects.filter(pk=options['lead']) if options['lead'] else Lead.objects.all()
        for pk in leads.values_list('pk',flat=True):
            run=reextract(pk);self.stdout.write(f'{pk}: extraction {run.pk}')

        from radar.dedup import link_duplicates
        self.stdout.write('duplicate links: '+str(link_duplicates()))
