from .ui_language import language_for
from django.urls import reverse
import uuid
import hashlib
from django.contrib import messages
from django.db import transaction
from django.shortcuts import render,redirect,get_object_or_404
from django.http import HttpResponseBadRequest,JsonResponse,FileResponse
from django.views.decorators.http import require_http_methods,require_POST,require_GET
from django.core.exceptions import ObjectDoesNotExist
from django.core.paginator import Paginator
from django.utils import timezone
from django.conf import settings
from .models import AnalysisRun,ModelCall,SearchCall,Lead,Document,EntityCandidate,Company,Review
from .forms import AISettingsForm,CapabilityForm
from .ai_config import config,capability,secret,store_secret
from .analysis import enqueue,review_historical_for_display
from .views import _base

@require_http_methods(['GET','POST'])
def ai_settings(request):
    cfg=config(); form=AISettingsForm(request.POST or None,instance=cfg)
    if request.method=='POST' and form.is_valid():
        cfg=form.save(commit=False)
        try:
            store_secret(cfg,form.cleaned_data['api_key'],form.cleaned_data['delete_key'])
            store_secret(cfg,form.cleaned_data['search_api_key'],form.cleaned_data['delete_search_key'],search=True)
        except Exception:
            form.add_error(None,'无法保存密钥，请检查系统凭据库，或使用环境变量。')
        else:
            cfg.version+=1; cfg.test_status='untested'; cfg.tested_at=None;cfg.save()
            messages.success(request,'设置已保存，可以测试连接。')
            return redirect('radar:ai_settings')
    from .labels import ERROR_LABELS
    cfg.ui_test_status=ERROR_LABELS.get(cfg.test_status,cfg.test_status)
    from .operations_stats import dashboard
    stats=dashboard(request,cfg)
    if request.GET.get('format')=='json' and request.method=='GET':
        return JsonResponse(stats)
    ctx=_base('ai');ctx.update(stats=stats,form=form,cfg=cfg,key_present=bool(secret(cfg)),search_key_present=bool(secret(cfg,search=True)),
        request_key=str(uuid.uuid4()),today_calls=ModelCall.objects.filter(requested_at__date=timezone.localdate()).count(),
        search_calls=SearchCall.objects.filter(requested_at__date=timezone.localdate()).count())
    return render(request,'radar/ai_settings.html',ctx,status=400 if request.method=='POST' else 200)

@require_http_methods(['GET','POST'])
def capability_settings(request):
    cap=capability();form=CapabilityForm(request.POST or None,instance=cap)
    if request.method=='POST' and form.is_valid():
        cap=form.save(commit=False);cap.version+=1;cap.save();messages.success(request,'服务能力已保存。');return redirect('radar:capability')
    ctx=_base('capability');ctx.update(form=form);return render(request,'radar/capability.html',ctx)

@require_POST
def enqueue_analysis(request,pk=None):
    task=request.POST.get('task_type','analysis');request_key=request.POST.get('request_key','')
    try:
        tier=request.POST.get('tier','')
        if tier:
            from .sales_ui import sales_data
            from .models import ResearchEvent
            event=ResearchEvent.objects.filter(lead_id=pk).first() if pk else None
            if tier!='pro' or not event or not any(c.get('detail_url')==reverse('radar:research_detail',args=[event.pk]) for g in 'AB' for c in sales_data(request)[1][g]):
                raise ValueError('深度研判仅用于单条 A/B 级研究线索。')
        ids=[pk] if pk else list(dict.fromkeys(int(v) for v in request.POST.getlist('lead_ids')))
        if task=='test':ids=[None]
        if not ids or len(ids)>config().batch_limit:raise ValueError('请选择记录，且不要超过单批上限。')
        if pk is not None:get_object_or_404(Lead,pk=pk)
        if task!='test' and Lead.objects.filter(pk__in=ids).count()!=len(ids):raise ValueError('记录不存在。')
        created=0
        for lead_id in ids:
            _,new=enqueue(lead_id,task,request_key,force=request.POST.get('force')=='true',batch_key=request_key,attachment_url=request.POST.get('attachment_url',''),tier=tier,language=language_for(request));created+=new
        messages.success(request,'已排队，可在分析记录查看进度。' if created else '已有相同分析，可在分析记录查看。')
    except (ValueError,TypeError) as exc:return HttpResponseBadRequest(str(exc))
    return redirect('radar:analyses')

@require_GET
def analysis_list(request):
    jobs=AnalysisRun.objects.select_related('lead').all()
    if request.GET.get('batch'):jobs=jobs.filter(batch_key=request.GET['batch'])
    page=Paginator(jobs,30).get_page(request.GET.get('page'))
    cap_version=capability().version
    for run in page.object_list:
        if run.lead_id and not run.input_snapshot.get("translation_of") and (run.input_snapshot.get('source_hash')!=run.lead.content_hash or
            (run.task_type not in {'enrich','attachment','material'} and (run.input_snapshot.get('extraction_version')!=run.lead.extraction_version or
                                         run.capability_snapshot.get('version')!=cap_version))):
            run.stale=True
        review_historical_for_display(run)
    ctx=_base('analyses');ctx.update(page=page,request_key=str(uuid.uuid4()),
        counts={s:jobs.filter(status=s).count() for s,_ in AnalysisRun.STATUSES},total=jobs.count())
    return render(request,'radar/analyses.html',ctx)

@require_POST
def job_action(request,pk):
    run=get_object_or_404(AnalysisRun,pk=pk)
    if run.input_snapshot.get('translation_of') and request.POST.get('action')=='retry':
        updated=AnalysisRun.objects.filter(pk=pk,status__in=['failed','cancelled']).update(
            status='queued',error_code='',started_at=None,finished_at=None)
        return redirect('radar:analyses')
    if request.POST.get('action')=='cancel':
        AnalysisRun.objects.filter(pk=pk,status='queued').update(status='cancelled',finished_at=timezone.now())
    elif request.POST.get('action')=='retry':
        if run.status not in {'failed','cancelled','needs_review'}:return HttpResponseBadRequest('只能重试失败、待复核或已取消的任务。')
        try:enqueue(run.lead_id,run.task_type,request.POST.get('request_key',''),force=True,attachment_url=run.input_snapshot.get('attachment_url',''),material_id=run.input_snapshot.get('material_id'),candidate_id=run.input_snapshot.get('candidate_id'),tier=run.input_snapshot.get('mimo_tier',''),language=run.input_snapshot.get('language','zh'))
        except ValueError as exc:return HttpResponseBadRequest(str(exc))
        except ObjectDoesNotExist:return HttpResponseBadRequest('分析对象已不存在。')
        messages.info(request,'已重新排队。')
    return redirect('radar:analyses')

@require_GET
def document_view(request,pk):
    doc=get_object_or_404(Document,pk=pk);ctx=_base('leads');ctx.update(doc=doc)
    return render(request,'radar/document.html',ctx)

@require_POST
def upload_pdf(request,pk):
    from pathlib import Path
    lead=get_object_or_404(Lead,pk=pk); file=request.FILES.get('material') or request.FILES.get('pdf')
    if not file or file.size>10_000_000:return HttpResponseBadRequest('请选择不超过 10MB 的 EML、HTML 或 PDF。')
    try:uuid.UUID(request.POST.get('request_key',''))
    except (ValueError,TypeError):return HttpResponseBadRequest('无效操作幂等键')
    body=file.read()
    ext=Path(file.name).suffix.lower()
    kind={'.pdf':'pdf','.html':'html','.htm':'html','.eml':'eml'}.get(ext)
    if not kind or (kind=='pdf' and not body.startswith(b'%PDF-')):return HttpResponseBadRequest('请选择有效的 EML、HTML 或 PDF 文件。')
    h=hashlib.sha256(body).hexdigest(); path=settings.DATA_DIR/'raw'/'uploads'/f'{h}.{kind}';path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists():path.write_bytes(body)
    relative=path.relative_to(settings.DATA_DIR).as_posix()
    doc,created=Document.objects.get_or_create(lead=lead,content_hash=h,defaults={'text':'','raw_path':relative,'metadata':{'kind':'user_'+kind,'input_type':kind,'status':'queued','parent_hash':lead.content_hash,'user_import':True,'upload_path':relative}})
    if not created and not doc.metadata.get('user_import'):
        return HttpResponseBadRequest('相同内容已作为来源文档保存，请直接查看现有文档。')
    try:enqueue(pk,'material',request.POST.get('request_key',''),force=True,material_id=doc.pk)
    except ValueError as exc:return HttpResponseBadRequest(str(exc))
    messages.success(request,'材料已保存，正在整理。')
    return redirect('radar:lead_detail',pk=pk)

@require_POST
def reparse_job(request,pk):
    from collectors.japanese_public import job_detail
    from .services import ingest_item
    lead=get_object_or_404(Lead.objects.select_related('source'),pk=pk)
    if lead.source.key!='persol_jobs':return HttpResponseBadRequest('该记录不是 PERSOL 公开招聘信号。')
    doc=lead.documents.filter(content_hash=lead.content_hash).first()
    if not doc or not doc.raw_path:return HttpResponseBadRequest('该记录没有可重新解析的保存原文。')
    path=(settings.DATA_DIR/doc.raw_path).resolve()
    if not path.is_relative_to((settings.DATA_DIR/'raw').resolve()) or not path.is_file() or path.stat().st_size>3_000_000:
        return HttpResponseBadRequest('原文文件不存在或超过解析上限。')
    try:
        item=job_detail(path.read_bytes(),lead.url,lead.external_id,lead.metadata.get('matched_queries',[]))
        if not item:return HttpResponseBadRequest('原文未满足招聘信号筛选，原记录保持不变。')
        ingest_item(lead.source,item)
    except (ValueError,TypeError) as exc:return HttpResponseBadRequest('重新解析失败：'+str(exc)[:200])
    messages.success(request,'职位原文已重新整理。')
    return redirect('radar:lead_detail',pk=pk)

@require_POST
def accept_candidate(request,pk):
    candidate=get_object_or_404(EntityCandidate,pk=pk)
    if candidate.role!='buyer':return HttpResponseBadRequest('此候选不是采购人角色，请在企业资料中核对关联角色。')
    with transaction.atomic():
        lead=Lead.objects.select_for_update().get(pk=candidate.lead_id)
        lead.company=Company.objects.get_or_create(name=candidate.name)[0]
        lead.metadata={**lead.metadata,'manual_buyer':True,'manual_buyer_evidence':candidate.pk}
        lead.subject_reason='';lead.save(update_fields=['company','metadata','subject_reason'])
        candidate.status='human_confirmed';candidate.save(update_fields=['status'])
        Review.objects.create(lead=lead,status=lead.review_status,note=f'人工确认采购主体：{candidate.name}；候选证据 #{candidate.pk}。未改变跟进状态。')
    return redirect('radar:lead_detail',pk=lead.pk)

@require_GET
def companies(request):
    qs=Company.objects.order_by('name')
    if request.GET.get('q'):qs=qs.filter(name__icontains=request.GET['q'])
    if request.GET.get('discovery')=='1':qs=qs.exclude(discovery_evidence=[])
    from .entities import classify_japanese_link, business_role_for_company
    directory_ids=[];foreign_ids=[]
    for company in Company.objects.exclude(discovery_evidence=[]).iterator():
        link=classify_japanese_link(company.name,company.discovery_evidence,company.ownership_status,company.ownership_evidence,dynamic=False)
        if link['level'] in {'directory','confirmed'}:directory_ids.append(company.pk)
        elif any(isinstance(e,dict) and e.get('status')=='foreign_affiliation_unverified' for e in company.discovery_evidence):foreign_ids.append(company.pk)
    cohort=request.GET.get('cohort','')
    if cohort=='japan':qs=qs.filter(pk__in=directory_ids)
    elif cohort=='foreign':qs=qs.filter(pk__in=foreign_ids)
    page=Paginator(qs,30).get_page(request.GET.get('page'))
    labels={'official_zone_list':'园区名录','association_list':'协会名录','chamber_list':'商工会名录'}
    for c in page:
        link=classify_japanese_link(c.name,c.discovery_evidence,c.ownership_status,c.ownership_evidence)
        role=business_role_for_company(c.name,c.profile,link)
        c.ui_business_role={'shipper':'货主企业候选 · 待核实','peer_logistics':'日系物流同行候选 · 二级分包机会','unknown_service':'非物流服务／业务角色待核实'}[role]
        for e in c.discovery_evidence:
            if isinstance(e,dict):e['ui_basis']=labels.get(e.get('directory_basis'),'历史名录/详情证据')
    ctx=_base('companies');ctx.update(page=page,q=request.GET.get('q',''),cohort=cohort,
        discovery_count=Company.objects.exclude(discovery_evidence=[]).count(),japan_directory_count=len(directory_ids),foreign_directory_count=len(foreign_ids))
    return render(request,'radar/companies.html',ctx)

@require_POST
def company_profile(request,pk):
    from .forms import OWNERSHIP_CHOICES
    company=get_object_or_404(Company,pk=pk)
    status=request.POST.get('ownership_status','unknown');evidence=request.POST.get('ownership_evidence','').strip()
    if status not in dict(OWNERSHIP_CHOICES) or (status!='unknown' and not evidence):return HttpResponseBadRequest('确认股权背景必须填写对应法人的来源、时间和关系依据。')
    company.ownership_status=status;company.ownership_evidence=evidence[:4000]
    company.profile={**company.profile,'industry':request.POST.get('industry','')[:300],'region':request.POST.get('region','')[:300],'products':request.POST.get('products','')[:1000],'parent_relation':request.POST.get('parent_relation','')[:1000],'manual_checked_at':timezone.now().isoformat()}
    for field in ['foreign_name','website','procurement_entry']:
        company.profile[field]=request.POST.get(field,company.profile.get(field,''))[:1200]
    company.subscribed=request.POST.get('subscribed')=='on';company.save()
    messages.success(request,'企业资料已保存。')
    return redirect('radar:companies')


@require_POST
def translate_brief(request, pk):
    from .brief_translation import brief_url,enqueue_translation
    run=get_object_or_404(AnalysisRun,pk=pk,status__in=['succeeded','needs_review'])
    try:
        job,created=enqueue_translation(run,request.POST.get('target'))
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    if request.headers.get('Accept')=='application/json':
        return JsonResponse({'id':job.pk,'created':created,'status':job.status},status=202 if created else 200)
    messages.info(request,'翻译已排队。' if created else '已找到这份翻译。')
    return redirect(brief_url(run)+'#project-analysis')
