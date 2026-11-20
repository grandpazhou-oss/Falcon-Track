"""Stable project-run fields shared by business exports and audit files."""
from .models import AnalysisRun


def audit_runs(queryset=None):
    queryset=AnalysisRun.objects.all() if queryset is None else queryset
    rows=[]
    for run in queryset.select_related('lead').order_by('pk'):
        result=run.result
        analysis=result.get('analysis') or {}
        rows.append({
            'run_id':run.pk,
            'title':run.input_snapshot.get('title') or (run.lead.title if run.lead_id else run.get_task_type_display()),
            'prompt_version':run.prompt_version,'status':run.status,'error_code':run.error_code,
            'usage':result.get('usage') or (run.provider_call.usage if run.provider_call else {}),
            'model':result.get('model'), 'estimated_rmb':result.get('cost'), 'prescreen':result.get('prescreen'),
            'seconds':(run.finished_at-run.started_at).total_seconds() if run.started_at and run.finished_at else None,
            'facts':[{**fact,'page':fact.get('page')} for fact in analysis.get('facts',[])],
            'verification':analysis.get('verification',[]),'validation_errors':result.get('validation_errors',[]),
        })
    return rows
