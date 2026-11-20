from .ui_language import language_for
"""Read-only sales pool display and evidence-only JSON export."""
from django.contrib import messages
from django.core.paginator import Paginator
import csv
import uuid

from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .development import homepage_contacts
from .analysis_audit import audit_runs
from .company_enrichment import FIELDS, named_case
from .models import AnalysisRun, Company, KeywordCandidate, SalesFeedback
from .sales_feedback import FeedbackForm, annotate, detail_feedback, distribution
from .sales_pools import build_sales_pools, candidate_fields, intake_card, intake_grades
from .views import _base
from .sales_ui import display_card, sales_data


@require_GET
def sales_pools(request):
    context = _base('sales_pools')
    pools,groups=sales_data(request)
    grade=request.GET.get('grade','')
    if grade not in {'A','B','C','AB'}:
        grade=''
    filters={key:request.GET.get(key,'').strip()[:160] for key in ('region','industry','feedback','q')}
    rows=[card for group in (grade or 'AB') for card in groups[group]]
    filtered=[card for card in rows if
              (not filters['region'] or card['ui_region']==filters['region']) and
              (not filters['industry'] or card.get('company_industry')==filters['industry']) and
              (not filters['feedback'] or card['feedback_status']==filters['feedback']) and
              (not filters['q'] or any(filters['q'].casefold() in str(card.get(key,'')).casefold()
               for key in ('company_name','title','china_location','evidence_quote')))]
    query=request.GET.copy()
    query.pop('page',None)
    context.update(sales_pools=pools, grade=grade, grade_a=len(groups['A']), grade_b=len(groups['B']),
                   grade_c=len(groups['C']), filters=filters, query=query.urlencode(),
                   show_list=bool(grade or any(filters.values())),
                   industries=sorted({c.get('company_industry') for rs in groups.values() for c in rs if c.get('company_industry')}),
                   intake_page=Paginator(filtered,25).get_page(request.GET.get('page')),
                   feedback_statuses=SalesFeedback.Status.choices,feedback_reasons=SalesFeedback.Reason.choices)
    return render(request, 'radar/sales_pools.html', context)


@require_GET
def export_sales_pools(request):
    pools = build_sales_pools()
    grades = annotate(intake_grades(pools))
    payload = {'generated_at':timezone.now().isoformat(),
               'notice':'公开资料研究信号，不代表已确认物流采购或实际跟进。',
               'homepage_contacts':homepage_contacts(pools),
               'grades':grades, 'sales_feedback':distribution(grades),
               'runs':audit_runs(),
               'pools':[{'key':p['key'], 'label':p['label'], 'count':p['count'], 'cards':p['cards']} for p in pools]}
    response = JsonResponse(payload, json_dumps_params={'ensure_ascii':False, 'indent':2})
    response['Content-Disposition'] = 'attachment; filename="sales-pools.json"'
    return response



def grade_export_rows(grades):
    totals = distribution(grades)
    for grade, rows in grades.items():
        for row in rows:
            yield {'id':row.get('candidate_id') or row.get('detail_url','').rstrip('/').rsplit('/',1)[-1], 'grade':grade,
                   'company_name':row.get('company_name',''),
                   'region':row.get('china_location',''), 'company_headquarters':row.get('company_headquarters',''),
                   'target_period':row.get('target_period',''),
                   'source':row.get('provider_key',row['source_kind']), 'source_url':row['source_url'],
                   **{name:row.get(name,'') for name in FIELDS},
                   **{name:row.get(name,'') for name in FEEDBACK_FIELDS},
                   'feedback_status_count_ab':totals['statuses'][row['feedback_status']],
                   'feedback_reason_count_ab':totals['reasons'].get(row['feedback_reason'],0)}


FEEDBACK_FIELDS = ('feedback_status','feedback_reason','feedback_note','feedback_at')


@require_GET
def export_sales_csv(request):
    response=HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition']='attachment; filename="sales-pools.csv"'
    response.write('\ufeff')
    rows=list(grade_export_rows(annotate(intake_grades(build_sales_pools()))))
    cols=['id','grade','company_name',*FIELDS,'region','company_headquarters','target_period','source','source_url',*FEEDBACK_FIELDS,'feedback_status_count_ab','feedback_reason_count_ab']
    writer=csv.DictWriter(response,fieldnames=cols)
    writer.writeheader()
    for row in rows:
        writer.writerow({k:"'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v
                         for k,v in row.items()})
    return response


@require_GET
def candidate_detail(request,pk):
    candidate=get_object_or_404(KeywordCandidate,pk=pk)
    card=intake_card(candidate)
    card['company_name'] = card['company_name'] or named_case(candidate)
    company=Company.objects.filter(name=card['company_name']).first()
    card.update(candidate_fields(candidate,company))
    from .analysis import review_historical_for_display

    runs=[review_historical_for_display(run) for run in AnalysisRun.objects.filter(input_snapshot__candidate_id=candidate.pk)[:5]]
    display_card(card)
    card['detail_url']=reverse('radar:sales_candidate_detail',args=[pk])
    return render(request,'radar/sales_candidate_detail.html',{
        **_base('sales_pools'), 'card':card, 'candidate':candidate,
        'feedback_card':detail_feedback(card), 'feedback_reasons':SalesFeedback.Reason.choices, 'project_runs':runs, 'request_key':str(uuid.uuid4())})


@require_POST
def analyze_candidate(request,pk):
    from django.http import HttpResponseBadRequest
    from .analysis import enqueue

    candidate=get_object_or_404(KeywordCandidate,pk=pk)
    tier=request.POST.get('tier','')
    if tier=='pro' and not any(c.get('detail_url')==reverse('radar:sales_candidate_detail',args=[pk]) for g in 'AB' for c in sales_data(request)[1][g]):
        return HttpResponseBadRequest('深度研判仅用于 A/B 级线索。')
    try:
        run,_=enqueue(None,'analysis',request.POST.get('request_key',''),
                      force=request.POST.get('force')=='true',candidate_id=candidate.pk,tier=tier,language=language_for(request))
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    return redirect(reverse('radar:analyses')+'?batch='+run.batch_key)


@require_GET
def feedback_stats(request):
    totals = distribution(annotate(intake_grades(build_sales_pools())))
    return render(request, 'radar/sales_feedback_stats.html', {
        **_base('feedback'),
        'statuses':[(label, totals['statuses'][key]) for key, label in SalesFeedback.Status.choices],
        'reasons':[(label, totals['reasons'][key]) for key, label in SalesFeedback.Reason.choices],
    })


@require_POST
def submit_feedback(request):
    form = FeedbackForm(request.POST)
    if not form.is_valid():
        return HttpResponse('营业反馈未保存。请填写有效状态，退回时选择原因。', status=400)
    groups = intake_grades(build_sales_pools())
    card = next((card for grade in 'AB' for card in groups[grade]
                 if card['detail_url'] == request.POST.get('card')), None)
    if card is None:
        return HttpResponse('未找到 A/B 级商机。', status=404)
    updates=form.cleaned_data.copy()
    if 'note' not in request.POST:
        updates.pop('note',None)
    SalesFeedback.objects.update_or_create(source_url=card['source_url'], defaults=updates)
    messages.success(request, '营业反馈已保存。')
    return redirect(card['detail_url'])
