"""Shared web/worker settings; OS secrets never enter business tables."""
import os
import uuid
from django.conf import settings
from .models import AIConfig, BusinessCapability

SERVICE = 'logistics-lead-radar-v02'

def config():
    return AIConfig.objects.get_or_create(pk=1, defaults={
        'enabled': False, 'daily_limit': settings.LEAD_MODEL_DAILY_BUDGET,
        'base_url': os.environ.get('LEAD_MODEL_BASE_URL', ''),
        'model': os.environ.get('LEAD_MODEL_NAME', 'mimo-v2.6-flash'),
    })[0]

def capability():
    return BusinessCapability.objects.get_or_create(pk=1)[0]

def secure_keyring():
    import keyring
    backend = keyring.get_keyring()
    if not type(backend).__module__.startswith(('keyring.backends.macOS', 'keyring.backends.Windows', 'keyring.backends.SecretService', 'keyring.backends.kwallet')):
        raise ValueError('系统凭据库不可用，请使用本项目环境变量。')
    return keyring

def secret(cfg, search=False):
    ref = cfg.search_key_ref if search else cfg.key_ref
    if ref=='__disabled__':return ''
    if ref:
        try:
            return secure_keyring().get_password(SERVICE, ref) or ''
        except Exception:
            return ''
    if search:
        return os.environ.get('LEAD_SEARCH_API_KEY', '')
    return (os.environ.get('LEAD_MODEL_API_KEY') or os.environ.get('MIMO_API_KEY','')) if cfg.key_source == 'environment' else ''

def store_secret(cfg, value, delete=False, search=False):
    field = 'search_key_ref' if search else 'key_ref'
    old = getattr(cfg, field)
    if delete:
        if old and old!='__disabled__':
            secure_keyring().delete_password(SERVICE, old)
        setattr(cfg, field, '__disabled__' if search else '')
        if not search:
            cfg.key_source = 'disabled'
    elif value:
        ref = old if old and old!='__disabled__' else str(uuid.uuid4())
        secure_keyring().set_password(SERVICE, ref, value)
        setattr(cfg, field, ref)
        if not search:
            cfg.key_source = 'keyring'

def public_snapshot(cfg):
    return {k: getattr(cfg, k) for k in ('protocol','base_url','model','pro_thinking','daily_limit','batch_limit','max_tokens','input_chars','timeout_seconds','version','search_enabled','search_daily_limit')}

def capability_snapshot(cap):
    return {k: getattr(cap, k) for k in ('regions','services','industries','qualifications','exclusions','version')}
