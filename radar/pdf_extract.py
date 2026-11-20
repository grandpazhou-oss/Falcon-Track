"""PDF text extraction runs as a bounded disposable process, never in a web request."""
import json
import sys
from pathlib import Path


def _result(pages, total, backend):
    return {
        'status': ('ocr_required' if pages and all(p['status']=='ocr_required' for p in pages)
                   else 'partial' if total>80 or any(p['status']!='text' for p in pages) else 'text'),
        'pages':pages, 'total_pages':total, 'unprocessed_pages':list(range(81,total+1)), 'backend':backend,
    }


def _page(number, text):
    return {'page':number,'text':text[:30000],
            'status':'text_limit' if len(text)>30000 else 'text' if text.strip() else 'ocr_required'}


def extract_pypdf(path, mode="layout"):
    from pypdf import PdfReader

    reader=PdfReader(path)
    if reader.is_encrypted and not reader.decrypt(''):
        return {'status':'encrypted','pages':[],'total_pages':0,'unprocessed_pages':[],'backend':'pypdf'}
    total=len(reader.pages)
    pages=[]
    for i,page in enumerate(reader.pages[:80],1):
        content=page.get_contents()
        if content and len(content.get_data())>8_000_000:
            pages.append({'page':i,'text':'','status':'page_size_limit'})
            continue
        pages.append(_page(i,page.extract_text(extraction_mode=mode) or ''))
    return _result(pages,total,'pypdf')


def extract_pymupdf(path, mode="layout"):
    import pymupdf

    with pymupdf.open(path) as doc:
        if doc.needs_pass and not doc.authenticate(''):
            return {'status':'encrypted','pages':[],'total_pages':0,'unprocessed_pages':[],'backend':'pymupdf'}
        pages=[_page(i,doc[i-1].get_text(sort=mode=='layout') or '') for i in range(1,min(len(doc),80)+1)]
        return _result(pages,len(doc),'pymupdf')


def extract(path, mode="layout"):
    if mode == "table_layout":
        return extract_pypdf(path, "layout")
    try:
        primary=extract_pymupdf(path,mode)
    except Exception:
        return extract_pypdf(path,mode)
    # Keep a valid page position even when one engine cannot read that page.
    if primary['status']=='encrypted' or all(p['text'].strip() for p in primary['pages']):
        return primary
    try:
        secondary=extract_pypdf(path,mode)
    except Exception:
        return primary
    replacements={p['page']:p for p in secondary['pages'] if p['text'].strip()}
    pages=[p if p['text'].strip() else replacements.get(p['page'],p) for p in primary['pages']]
    backend='pymupdf+pypdf' if pages!=primary['pages'] else 'pymupdf'
    return _result(pages,primary['total_pages'],backend)


if __name__=="__main__":
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS,(768*1024*1024,768*1024*1024))
    except (ImportError,ValueError,OSError):
        pass
    try:
        result=extract(sys.argv[1],sys.argv[3] if len(sys.argv)>3 else 'layout')
    except Exception:
        result={'status':'parse_failed','pages':[]}
    Path(sys.argv[2]).write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
