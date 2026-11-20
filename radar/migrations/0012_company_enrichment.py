from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('radar', '0011_keywordcandidate_error_detail_and_more')]

    operations = [
        migrations.AddField('company','company_industry',models.CharField(blank=True,max_length=40)),
        migrations.AddField('company','official_website',models.URLField(blank=True,max_length=2000)),
        migrations.AddField('company','official_website_status',models.CharField(
            choices=[('verified','已核实'),('unverified','待核实'),('missing','缺少依据')],
            default='missing',max_length=12)),
    ]
