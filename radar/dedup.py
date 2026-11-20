"""Merge identical source events without deleting URLs, stages, evidence or reviews."""
import re
from .models import Lead

def identity(lead,text):
    title=lead.title.split('\n')[0]
    context=title+'\n'+text
    # Different year, lot or phase stays a different event because the entire normalized title is retained.
    title=re.sub(r'[\s【】\[\]（）()：:—_\-·，,。]','',title)
    ids=sorted(set(re.findall(r'(?:项目编号|招标编号|采购编号)\s*[:：]\s*([A-Za-z0-9-]{8,60})',text)))
    # Generic titles can be reused under the same project number across years
    # or lots. A conservative false negative is safer than merging events.
    years=tuple(sorted(set(re.findall(r'(?<!\d)(20\d{2})(?=年|年度)',context))))
    lots=tuple(sorted(set(re.findall(r'第[一二三四五六七八九十\d]+(?:标段|包)|标段[一二三四五六七八九十\d]+',context))))
    return (title,tuple(ids),years,lots) if ids else None

def link_duplicates():
    seen={};linked=[]
    for lead in Lead.objects.filter(is_test=False).order_by('id'):
        extraction=lead.extractions.filter(source_hash=lead.content_hash).order_by('-id').first()
        key=identity(lead,extraction.result.get('normalized_text',lead.text) if extraction else lead.text)
        if key is None:continue
        if key in seen and lead.pk!=seen[key]:
            if lead.duplicate_of_id is None:
                lead.duplicate_of_id=seen[key];lead.save(update_fields=['duplicate_of']);linked.append([lead.pk,seen[key]])
        else:seen[key]=lead.pk
    return linked
