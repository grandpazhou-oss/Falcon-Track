"""Backfill from archived documents, then optionally verify a bounded number of A/B gaps."""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from collectors.issuer_ir_pdf import blocked, verify_root
from radar.ai_config import config
from radar.company_enrichment import extract_profile, named_case, same_name, site_root
from radar.models import Company, Document, KeywordCandidate
from radar.public_fetch import request_budget
from radar.sales_pools import build_sales_pools, intake_grades
from radar.search_provider import budgeted_search, provider_for


def fill(company, industry, website, *, verified=False, evidence=None):
    changed=[]
    if industry and not company.company_industry:
        company.company_industry=industry
        changed.append('company_industry')
    if website and (not company.official_website or
                    (verified and company.official_website_status!='verified')):
        company.official_website=website if verified else ''
        company.official_website_status='verified' if verified else 'unverified'
        changed.extend(['official_website','official_website_status'])
        if not verified and website:
            company.profile={**company.profile,'official_website_candidate':website}
            changed.append('profile')
        if verified and evidence:
            company.profile={**company.profile,'official_website_evidence':evidence}
            changed.append('profile')
    if changed:
        company.save(update_fields=changed)


def offline():
    companies={row.pk:row for row in Company.objects.all()}
    for doc in Document.objects.exclude(raw_path='').exclude(lead__company_id=None).select_related('lead').iterator():
        company=companies[doc.lead.company_id]
        metadata=doc.metadata
        url=metadata.get('url','')
        archived_verified=metadata.get('kind')=='website_check' and metadata.get('status')=='explicit_name_match'
        industry,website=extract_profile(company.name, doc.text)
        verified = archived_verified and same_name(company.name,doc.text)
        if verified:
            website=site_root(url)
        evidence={'raw_path':doc.raw_path,'url':url,
                  'sha256':metadata.get('source_document_hash',doc.content_hash)}
        fill(company,industry,website,verified=bool(website and verified),evidence=evidence)
    for candidate in KeywordCandidate.objects.exclude(text='').exclude(raw_path='').iterator():
        result=candidate.result
        name=named_case(candidate)
        industry,website=extract_profile(name,candidate.text,project=result.get('source_scope')=='grant_selection')
        if industry and not result.get('company_industry'):
            result['company_industry']=industry
        if website and not result.get('official_website'):
            result.update(official_website_candidate=website,official_website_status='unverified')
        if industry or website:
            candidate.save(update_fields=['result'])


def verify_candidates(urls,name,company,stats):
    for url in dict.fromkeys(filter(None,urls)):
        if not site_root(url) or blocked(url):
            continue
        try:
            verified=verify_root(url,name,stats)
        except (ValueError,OSError) as exc:
            stats['errors'].append({'url':url,'company':name,'reason':str(exc)[:160]})
            continue
        if verified:
            site,text,doc=verified
            industry,_=extract_profile(name,text)
            fill(company,industry,site,verified=True,evidence=doc)
            stats['verified'].append(name)
            return


def enrich_ab(online,search_limit):
    grades=intake_grades(build_sales_pools())
    companies={company.name:company for company in Company.objects.all()}
    stats={'requests':[],'errors':[],'search_credits':0,'search_calls':0,'verified':[],'missing':[]}
    provider=provider_for('tavily','ja-JP') if search_limit else None
    with request_budget(100,420):
        for row in grades['A']+grades['B']:
            candidate=KeywordCandidate.objects.filter(canonical_url=row['source_url']).first()
            name=row.get('company_name') or (named_case(candidate) if candidate else '')
            if not name or name=='待核':
                continue
            company=companies.get(name)
            if not company:
                if not candidate or not candidate.raw_path or not same_name(name,candidate.text):
                    continue
                company,_=Company.objects.get_or_create(name=name)
                companies[name]=company
            result=candidate.result if candidate else {}
            if candidate:
                fill(company,result.get('company_industry',''),result.get('official_website',''),
                     verified=result.get('official_website_status')=='verified',evidence=result.get('website_evidence'))
            if company.company_industry and company.official_website_status=='verified':
                continue
            if not online:
                continue
            urls=[company.official_website,company.profile.get('official_website_candidate',''),
                  result.get('official_website_candidate',''),row.get('identity_evidence_url',''),row['source_url']]
            verify_candidates(urls,name,company,stats)
            if company.official_website_status != 'verified' and provider and stats['search_calls']<search_limit:
                if stats['search_credits'] is not None and stats['search_credits']<10:
                    stats['search_calls']+=1
                    try:
                        found=budgeted_search(provider,name+' 会社概要 事業内容 公式','company-profile',config().search_daily_limit)
                        credits=provider.last_stats.get('credits')
                        stats['search_credits']=stats['search_credits']+credits if type(credits) is int else None
                    except Exception as exc:
                        stats['search_credits']=None
                        stats['errors'].append({'company':name,'reason':type(exc).__name__})
                    else:
                        verify_candidates([x.url for x in found[:3]],name,company,stats)
            if not company.company_industry or company.official_website_status!='verified':
                stats['missing'].append(name)
    return stats


class Command(BaseCommand):
    help=__doc__

    def add_arguments(self,parser):
        parser.add_argument('--online',action='store_true')
        parser.add_argument('--search-limit',type=int,default=0)
        parser.add_argument('--report',type=Path,required=True)

    def handle(self,*args,**options):
        limit=options['search_limit']
        if not 0<=limit<=10 or (limit and not options['online']):
            raise CommandError('在线补查搜索上限 0–10。')
        offline()
        try:
            report=enrich_ab(options['online'],limit)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        options['report'].write_text(json.dumps(report,ensure_ascii=False,indent=2))
