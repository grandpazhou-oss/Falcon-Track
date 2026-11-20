"""Roster evidence admits candidates; independent websites only enrich identity."""
import hashlib
import re
import unicodedata
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from filelock import FileLock
from .models import Company


def normalize_name(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value or '')).strip('·、,;；')


def entity_type(name):
    if re.match(r'^(?:有限公司|有限责任公司|分公司)', name):return 'unresolved'
    if re.search(r'幼儿园|学校|中国语中心|苏州校|個人|个人会员', name):
        return 'other_organization'
    if re.search(r'分公司|分行|支店|代表处|办事处|事务所', name):
        return 'branch'
    if re.search(r'公司|会社|集团|集団|工厂|銀行|银行', name):
        return 'company'
    return 'unresolved'


def import_roster(source, rows, response, *, basis, japanese=True, limit=50):
    """Consume unique row identities, never an offset into a changing roster."""
    limit = max(1, min(int(limit), 50))
    digest = hashlib.sha256(response['body']).hexdigest()
    path = settings.DATA_DIR / 'raw' / 'directories' / f'{digest}.html'
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(response['body'])
    unique = {}
    for row in rows:
        raw = row['name']
        name = normalize_name(raw)
        if not name:
            continue
        # Case/width/spacing variants are equivalent, branches remain distinct.
        identity = hashlib.sha256(name.casefold().encode()).hexdigest()
        unique.setdefault(identity, {**row, 'name': name, 'raw_name': raw})
    if not unique:
        raise ValueError('directory_layout_no_entities')
    checked = []; outcomes = []; created = 0
    with FileLock(str(settings.DATA_DIR / 'directory-write.lock'), timeout=120), transaction.atomic():
        current = type(source).objects.select_for_update().get(pk=source.pk)
        state = dict(current.checkpoint or {})
        processed = set(state.get('roster_processed', []))
        pending = [(key, row) for key, row in unique.items() if key not in processed]
        if not pending:
            processed = set(); pending = list(unique.items())
        batch = pending[:limit]
        existing = {normalize_name(c.name).casefold(): c for c in Company.objects.all()}
        for key, row in batch:
            name = row['name']; kind = entity_type(name)
            outcome = {'raw_name': row['raw_name'], 'name': name, 'entity_type': kind}
            if kind == 'other_organization' or len(name) > 240:
                outcomes.append({**outcome, 'outcome': 'retained_nonenterprise' if kind == 'other_organization' else 'name_too_long'})
                processed.add(key); continue
            company = existing.get(name.casefold())
            if company is None:
                company = Company.objects.create(name=name)
                existing[name.casefold()] = company; created += 1
            if not company.subscribed:
                outcomes.append({**outcome, 'company_id': company.pk, 'outcome': 'paused_preserved'})
                processed.add(key); continue
            affiliation = row.get('japan_affiliation_basis', 'japanese_specific_roster' if japanese else 'unknown')
            evidence = {
                'schema_version': 1, 'source': source.key, 'url': response['url'],
                'document_hash': digest, 'raw_path': path.relative_to(settings.DATA_DIR).as_posix(),
                'quote': row.get('quote', row['raw_name']), 'listed_name': row['raw_name'],
                'status': 'japan_affiliation_unverified' if affiliation != 'unknown' else 'foreign_affiliation_unverified',
                'directory_basis': basis, 'identity_verification': 'list_only',
                'japan_affiliation_basis': affiliation, 'entity_type': kind,
                'checked_at': timezone.now().isoformat(),
            }
            prior = list(company.discovery_evidence or [])
            identity_fields = ('source', 'url', 'document_hash', 'listed_name', 'status')
            if not any(isinstance(e, dict) and all(e.get(k) == evidence[k] for k in identity_fields) for e in prior):
                prior.append(evidence)
            company.discovery_evidence = prior
            profile = dict(company.profile or {})
            profile.setdefault('last_discovered_at', timezone.now().isoformat())
            company.profile = profile
            company.save(update_fields=['discovery_evidence', 'profile', 'updated_at'])
            checked.append(company.pk)
            outcomes.append({**outcome, 'company_id': company.pk, 'outcome': 'admitted'})
            processed.add(key)
        current.checkpoint = {**state, 'roster_hash': digest,
                              'roster_processed': sorted(processed & set(unique)),
                              'roster_total': len(unique), 'roster_complete': set(unique) <= processed}
        current.save(update_fields=['checkpoint'])
    source.checkpoint = current.checkpoint
    return {'items': [], 'status': 'success', 'next_cursor': 1, 'stats': {
        'directory': True, 'roster_rows': len(rows), 'companies_found': len(unique),
        'companies_processed': len(checked), 'companies_new': created,
        'rows_processed': len(batch), 'rows_remaining': len(set(unique) - processed),
        'outcomes': outcomes, 'limit': limit, 'document_hash': digest,
        'discovery_url': response['url'], 'error_count': 0,
        'coverage_complete': set(unique) <= processed,
        'scope': '公开名录全量分批；无需官网详情。名录关联不等于股权或物流需求。'}}
