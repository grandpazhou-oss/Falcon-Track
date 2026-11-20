"""Explicit, local-only ingestion of user-provided licensed JBIC material."""
import hashlib
from django.conf import settings
from collectors.jbic_sme import parse_jbic_material
from .research import ingest_research_record


def import_jbic(source,body,url):
    item=parse_jbic_material(body,url)
    signal=item['metadata']['business_signal'];facts=signal['facts'];digest=hashlib.sha256(body).hexdigest()
    def get(field):
        result=next((x for x in facts if x['field']==field),None)
        if not result:raise ValueError('必要的主体依据未取得：'+field)
        return {'quote':result['quote'],'location':result['location'],'document_hash':digest}
    participants=[{'name':e['name'],'role':'japan_parent' if e['role']=='japanese_parent' else 'china_entity',
                   'relationship':'unknown',**get('japanese_parent' if e['role']=='japanese_parent' else 'china_entity')}
                  for e in signal['entities']]
    dates=[]
    for kind,value in signal['dates'].items():
        if value.get('value'):
            dates.append({'kind':kind,'value':value['value'],'precision':value['precision'],'status':value['status'],
                          **get({'china_established':'china_established_year'}.get(kind,kind))})
    scale={'value':'unknown'}
    if signal['company_scale']=='sme':scale={'value':'sme','basis':'official_finance_statement',**get('company_scale')}
    saved=[]
    for f in facts:
        if f['field'] not in {'japanese_parent','china_entity','company_scale','published_at','contract_date','china_established_year'}:
            saved.append({'field':f['field'],'value':str(f['value']),'quote':f['quote'],
                          'location':f['location'],'document_hash':digest})
    path=settings.DATA_DIR/'raw'/'research'/f'{digest}.html';path.parent.mkdir(parents=True,exist_ok=True)
    try:
        with path.open('xb') as stream:stream.write(body)
    except FileExistsError:pass
    record={'external_id':'jbic:'+item['external_id'],'url':url,'title':item['title'],'text':item['text'],
      'document':{'raw_path':path.relative_to(settings.DATA_DIR).as_posix(),'sha256':digest,'type':'html',
                  'publisher':'JBIC（用户提供材料，未联网核验）','source_level':'user_provided_unverified'},
      'research':{'event_type':'finance_support','workstream':'planned_china','china_scope':'mainland',
        'china_evidence':get('china_entity'),'participants':participants,'dates':dates,'facts':saved,'size':scale,
        'inferences':['融资用途可作为研究物流安排的线索；不代表物流采购或已设预算。'],
        'questions':['用户提供材料，使用权及当前经营进度需分别核实。'],
        'next_step':'核实原文中融资用途、在华经营变化及物流安排。'}}
    return ingest_research_record(source,record)
