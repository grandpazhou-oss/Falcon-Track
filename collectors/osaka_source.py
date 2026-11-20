"""Convert Osaka's verified self-reported workbook into research tasks."""
import re
from pathlib import Path
from lxml import html
from django.conf import settings
from collectors.candidate_state import bounded
from collectors.osaka_sme import collect_directory
from collectors.sources import node_text
from radar.research import _mainland_evidence


def record_from_row(row,evidence,index_text):
    digest=evidence['file_hash'];facts=row['facts'];sheet=row['sheet']
    def cell(key):
        part=facts[key]
        return {'quote':part['quote'],'location':f'worksheet:{sheet}!{part["cell"]}','document_hash':digest}
    text='\n'.join([sheet]+[f['quote'] for f in facts.values()]+[n['quote'] for n in row.get('source_notes',[])])
    region=cell('site_address') if 'site_address' in facts else None
    if not region or not _mainland_evidence(region):
        region={'quote':sheet,'location':f'worksheet:{sheet}','document_hash':digest}
    if not _mainland_evidence(region):raise ValueError('mainland_evidence_missing')
    relationship=row['relation_type']
    participants=[{'name':row['parent_name'],'role':'japan_parent','relationship':'unknown',**cell('parent_name')},
                  {'name':row['site_name'],'role': 'distributor' if relationship=='distributor' else 'china_site',
                   'relationship':relationship,**cell('site_name')}]
    other=[]
    for key in ('parent_address','site_address','site_function','relation_description','products','business_description','website'):
        if key in facts:other.append({'field':key,'value':facts[key]['quote'],**cell(key)})
    if row.get('operational_roles') and 'site_function' in facts:
        other.append({'field':'operational_roles','value':', '.join(row['operational_roles']),**cell('site_function')})
    for note in row.get('source_notes',[]):
        other.append({'field':'source_note','value':note['quote'],'quote':note['quote'],
                      'location':f'worksheet:{sheet}!{note["cell"]}','document_hash':digest})
    scale={'value':'unknown'};support=[]
    if index_text:
        para=next((s.strip() for s in index_text.splitlines() if '中小企業' in s and '海外拠点' in s and len(s.strip())<1200),'')
        if para:
            scale={'value':'sme','basis':'official_directory_self_report','quote':para,
                   'location':'official_index:paragraph','document_hash':evidence['index_hash']}
            support=[{'raw_path':evidence['index_raw_path'],'sha256':evidence['index_hash'],
                      'type':'html','url':evidence['index_url'],'text':index_text}]
    dates=[]
    if support and evidence.get('index_updated_on') and evidence.get('index_updated_quote') in index_text:
        dates.append({'kind':'source_updated','value':evidence['index_updated_on'],'precision':'day','status':'source_metadata',
                      'quote':evidence['index_updated_quote'],'location':'official_index:updated','document_hash':evidence['index_hash']})
    return {'external_id':'osaka:'+row['record_key'],'title':row['parent_name']+' · '+row['site_name'],
       'url':evidence['url'],'text':text,
       'document':{'raw_path':evidence['raw_path'],'sha256':digest,'type':'xlsx'},'supporting_documents':support,
       'research':{'event_type':'existing_presence','workstream':'existing_in_china','china_scope':'mainland',
          'china_evidence':region,'participants':participants,'dates':dates,'facts':other,'size':scale,
          'inferences':['可核实产品、当地据点及进出口或仓配安排；名录不证明当前采购。'],
          'questions':['名录为企业自报；请复核据点当前状态及实际关系。'],
          'next_step':'核实企业官网与据点状态，随后跟踪扩建、搬迁、渠道及仓网变化。'}}


def collect_osaka(source):
    checkpoint=(source.checkpoint or {}).get('osaka_directory_v1',{})
    try:
        data=collect_directory(checkpoint)
        evidence=data['evidence'];logs=evidence['requests']
        if data['status']=='unchanged':
            return {'items':[],'status':'success','next_cursor':1,'collector_checkpoint':{'osaka_directory_v1':checkpoint},
                    'stats':{'requests':logs,'coverage_complete':True,'note':'源文件未变化'}}
        body=(settings.DATA_DIR/evidence['index_raw_path']).read_bytes()
        index_text=node_text(html.fromstring(body))
        rows=data['records'];eligible=[r for r in rows if r['region_class']=='mainland_china']
        offset=checkpoint.get('next_offset',0) if checkpoint.get('file_hash')==evidence['file_hash'] else 0
        offset=max(0,min(len(eligible),int(offset)))
        limit=bounded((source.options or {}).get('max_accepted_items',(source.options or {}).get('max_items')),8,1,50)
        batch=eligible[offset:offset+limit];items=[];excluded=[]
        for row in batch:
            try:items.append(record_from_row(row,evidence,index_text))
            except ValueError as exc:excluded.append({'sheet':row['sheet'],'reason':str(exc)})
        next_offset=offset+len(batch);completed=next_offset>=len(eligible)
        state={'file_hash':evidence['file_hash'],'next_offset':next_offset,'completed':completed}
        return {'items':items,'status':'success' if completed else 'partial','next_cursor':1,
                'collector_checkpoint':{'osaka_directory_v1':state},'stats':{'requests':logs,
                  'rows_total':len(rows),'mainland_rows':len(eligible),'offset':offset,'next_offset':next_offset,
                  'other_regions':[{'sheet':r['sheet'],'reason':r['region_class']} for r in rows if r['region_class']!='mainland_china'],
                  'excluded':excluded,'diagnostics':data['diagnostics'],'coverage_complete':completed}}
    except Exception as exc:
        return {'items':[],'status':'blocked','next_cursor':1,'error':f'{type(exc).__name__}: {str(exc)[:350]}',
                'stats':{'requests':[],'error_count':1,'coverage_complete':False}}
