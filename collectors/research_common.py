"""Bounded original-source reads and existing disposable PDF extraction."""
import hashlib,json,subprocess,sys,tempfile,time
from pathlib import Path
from django.conf import settings
from radar.public_fetch import fetch


def read_original(url,*,max_bytes=3_000_000,timeout=25,probe=False):
    result=fetch(url,max_bytes=max_bytes,timeout=timeout);body=result['body']
    digest=hashlib.sha256(body).hexdigest()
    ext='pdf' if body.startswith(b'%PDF-') else ('xlsx' if body.startswith(b'PK\x03\x04') else 'html')
    path=settings.DATA_DIR/'raw'/('probe_cache' if probe else 'research')/f'{digest}.{ext}';path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists():path.write_bytes(body)
    return result,{'url':result['url'],'sha256':digest,'raw_path':path.relative_to(settings.DATA_DIR).as_posix(),
       'redirect_chain':result.get('redirect_chain',[]),'robots_redirect_chains':result.get('robots_redirect_chains',[])}


def pdf_pages(raw_path,remaining_seconds=25, *, mode="layout"):
    path=(settings.DATA_DIR/raw_path).resolve()
    if not path.is_relative_to((settings.DATA_DIR/'raw').resolve()) or not path.is_file():raise ValueError('pdf_not_saved')
    with tempfile.TemporaryDirectory() as tmp:
        output=Path(tmp)/'parsed.json'
        try:
            subprocess.run([sys.executable,'-m','radar.pdf_extract',str(path),str(output),mode],cwd=settings.BASE_DIR,
                timeout=max(1,min(25,remaining_seconds)),check=True,capture_output=True)
            result=json.loads(output.read_text(encoding='utf-8'))
        except (subprocess.TimeoutExpired,subprocess.CalledProcessError,ValueError,OSError) as exc:
            raise ValueError('pdf_processing_failed') from exc
    if not result.get('pages'):raise ValueError('pdf_no_text_pages')
    return result
