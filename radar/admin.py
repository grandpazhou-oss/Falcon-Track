from django.contrib import admin
from .models import Company, Document, Lead, Review, Source, SourceRun

admin.site.site_header = "物流商机 · 维护后台"
admin.site.register([Source, SourceRun, Company, Lead, Review, Document])

