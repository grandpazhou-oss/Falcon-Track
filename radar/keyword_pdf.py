"""Extract already saved keyword PDFs in the existing bounded PDF subprocess."""
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

from django.conf import settings

from .management.commands.backup_data import raw_source_file


def pdf_publication(first_page):
    match = re.match(r'\s*(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日', first_page or '')
    if not match:
        return {}
    try:
        published = date(*map(int, match.groups())).isoformat()
    except ValueError:
        return {}
    return {'date':published, 'field':'pdf:first_page', 'page':1, 'quote':match.group(0).strip()}


def inspect_keyword_pdf(candidate):
    expected = (candidate.result or {}).get('source_sha256', '')
    if not re.fullmatch(r'[a-f0-9]{64}', expected):
        raise ValueError('missing_source_hash')
    relative = candidate.raw_path or f'raw/research/{expected}.pdf'
    path = raw_source_file(relative)
    if path.stat().st_size > 10_000_000:
        raise ValueError('pdf_too_large')
    data = path.read_bytes()
    if not data.startswith(b'%PDF-') or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError('pdf_source_hash_mismatch')
    with tempfile.TemporaryDirectory(prefix='.keyword-pdf-', dir=settings.DATA_DIR) as tmp:
        result_path = Path(tmp)/'result.json'
        try:
            subprocess.run([sys.executable,'-m','radar.pdf_extract',str(path),str(result_path)],
                           cwd=settings.BASE_DIR, timeout=20, check=True, capture_output=True)
            result = json.loads(result_path.read_text(encoding='utf-8'))
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError, ValueError):
            return {'status':'processing_limit_or_parse_failure', 'raw_path':relative, 'pages':0, 'text':''}
    pages = result.get('pages', [])
    text = '\n'.join(p.get('text', '') for p in pages)
    visible = [c for c in text if not c.isspace()]
    cjk = sum(bool(re.match(r'[\u3040-\u30ff\u3400-\u9fff]', c)) for c in visible)
    odd = sum(bool(re.match(r'[\u0300-\u036f\u0530-\u1fff]', c)) for c in visible)
    status = result.get('status', 'parse_failed')
    if not visible:
        status = 'no_text_layer'
    elif odd / len(visible) > .2 and cjk / len(visible) < .03:
        status = 'text_encoding_unreadable'
    first = pages[0].get('text', '') if pages else ''
    year = re.search(r'(?<!\d)(20\d{2})\s*年', first[:800])
    return {'status':status, 'raw_path':relative, 'pages':result.get('total_pages', 0),
            'text':text, 'publication':pdf_publication(first), 'first_page_year':year.group(1) if year else '',
            'page_lengths':[len(p.get('text', '')) for p in pages], 'backend':result.get('backend','')}
