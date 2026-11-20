from django import template
from radar.ui_language import label

register=template.Library()


@register.filter
def ui(value,lang):
    return label(value,lang)


@register.filter
def tier(value):
    value=str(value or '').lower()
    return {'mimo-v2.6-flash':'Flash','mimo-v2.6-pro':'Pro','flash':'Flash','pro':'Pro'}.get(value,'')


@register.simple_tag(takes_context=True)
def brief_version(context,run):
    from django.urls import reverse
    from radar.brief_translation import brief_values,cache_key,source_language
    from radar.models import AnalysisRun

    original=brief_values(run)
    if not original:
        return {}
    request=context.get('request')
    source=source_language(run)
    if request is None or not getattr(run,'pk',None):
        return {'brief':original,'source':source}
    target='zh' if source=='ja' else 'ja'
    desired=context.get('ui_lang','zh')
    job=AnalysisRun.objects.filter(request_key=cache_key(run,target)).only('status','result').first()
    translated=job.result.get('brief') if job and job.status=='succeeded' else None
    show_translation=bool(translated and (request.GET.get('brief')=='translated' or
                         request.GET.get('brief')!='original' and desired==target))
    return {'brief':translated if show_translation else original,'source':source,'target':target,
            'other_language':desired!=source,'can_toggle':bool(translated),'selected':'translated' if show_translation else 'original',
            'shown_lang':target if show_translation else source,
            'status':job.status if job else '', 'action':reverse('radar:translate_brief',args=[run.pk])}


@register.simple_tag
def ui_field(bound,lang):
    from copy import deepcopy
    from django.utils.html import format_html, format_html_join

    widget=deepcopy(bound.field.widget)
    if 'placeholder' in widget.attrs:
        widget.attrs['placeholder']=label(widget.attrs['placeholder'],lang)
    if hasattr(widget,'choices'):
        widget.choices=[(value,label(text,lang)) for value,text in widget.choices]
    errors=format_html_join('', '<li>{}</li>', ((label(entry,lang),) for entry in bound.errors))
    return format_html('<label for="{}">{}</label>{}{}',bound.id_for_label,label(bound.label,lang),
                       bound.as_widget(widget),format_html('<ul class="errorlist">{}</ul>',errors) if errors else '')
