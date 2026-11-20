"""Persist one canonical candidate before applying business admission rules."""
from radar.models import KeywordCandidate
from radar.search_provider import canonical_url


def store_candidate(*, url, title, summary, provider, query_id='', published='', text='', document=None, discovery=None):
    url = canonical_url(url)
    title, summary = title.strip(), summary.strip()
    if not url or len(url) > 2000 or not title or not (summary or text):
        return False
    candidate, created = KeywordCandidate.objects.get_or_create(canonical_url=url, defaults={
        'title':title[:600], 'snippet':summary[:4000], 'published_at':published[:80],
        'provider':provider, 'query_id':query_id, 'result':discovery or {},
    })
    if text and document and document.get('sha256') and not candidate.raw_path:
        candidate.text, candidate.raw_path = text[:200000], document['raw_path']
        candidate.result.update(discovery or {})
        candidate.result.update(source_sha256=document['sha256'], source_url=url, content_mode='full_text')
        candidate.save(update_fields=['text','raw_path','result'])
    return created
