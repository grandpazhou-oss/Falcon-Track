"""Portable Chinese/Japanese UI labels."""
import json
from pathlib import Path
from functools import lru_cache

from django.conf import settings
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST


@lru_cache(maxsize=1)
def japanese_labels():
    path=Path(__file__).with_name('ui_ja.json')
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def language_for(request):
    return 'ja' if request.session.get('ui_language')=='ja' else 'zh'


def label(value,language):
    return japanese_labels().get(str(value),value) if language=='ja' else value


def language_context(request):
    return {'ui_lang':language_for(request)}


@require_POST
def switch(request):
    request.session['ui_language']='ja' if request.POST.get('language')=='ja' else 'zh'
    location=request.POST.get('next','')
    if not location.startswith('/') or not url_has_allowed_host_and_scheme(location,allowed_hosts={request.get_host()},require_https=request.is_secure()):
        location='/'
    return redirect(location)
