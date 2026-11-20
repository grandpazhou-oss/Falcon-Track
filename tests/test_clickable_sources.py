from unittest.mock import Mock

import pytest
from django.urls import reverse
from lxml import html

from collectors import source_cards as cards
from radar.models import Source, SourceRun
from radar.services import process_run


@pytest.mark.django_db
@pytest.mark.parametrize('key', sorted(cards.NOTES | cards.ALTERNATIVES))
def test_card_submits_real_source(client, key):
    original = Source.objects.create(key=key, name=key)
    target = original
    if key in cards.ALTERNATIVES:
        target_key, _ = cards.ALTERNATIVES[key]
        target = Source.objects.create(key=target_key, name=target_key)
    page = html.fromstring(client.get(reverse('radar:sources')).content)
    [form] = page.xpath(f'//*[@data-source-id="{original.pk}"]//form[contains(@action,"/run/")]')
    assert form.xpath('.//button[not(@disabled)]')
    for _ in range(2):
        assert client.post(form.get('action')).status_code == 302
    assert SourceRun.objects.get().source_id == target.pk


@pytest.mark.django_db
def test_event_index_preserves_links_only_and_excludes_japans_chugoku(monkeypatch):
    src = Source.objects.create(key='jetro_china_pavilion',url='https://example.jp/list')
    markup = '<a href="/events/a/11.html">中国市場商談会</a><a href="/events/b/22.html">中国地域の募集</a>'
    mock = Mock(return_value={'body':markup.encode(), 'url':src.url})
    monkeypatch.setattr(cards, 'fetch', mock)
    monkeypatch.setattr(cards,'read',lambda *args,**kwargs:pytest.fail('indexed content downloaded'))
    run = process_run(SourceRun.objects.create(source=src).pk)
    assert mock.call_count == 1
    assert run.stats['index_entries'] == [{'url':'https://example.jp/events/a/11.html','title':'中国市場商談会'}]
    assert not run.stats['details_read'] and run.new_count == 0


@pytest.mark.django_db
def test_empty_index_has_no_success_time(monkeypatch):
    src = Source.objects.create(key='cifer_japan_registrations')
    monkeypatch.setattr(cards,'fetch',lambda *a,**k:{'body':b'<html></html>','url':'https://example.jp/'})
    run = process_run(SourceRun.objects.create(source=src).pk)
    src.refresh_from_db()
    assert run.status == 'failed' and not src.last_success_at


def test_monthly_index_reads_image_links_and_ignores_external_pdfs():
    markup = b'<a href="/files/libs/7/20261101121212.pdf"><img></a><a href="https://other.jp/files/libs/8/20261101111111.pdf"><img></a>'
    rows = cards.listing('jcea_monthly_reference', markup, 'https://example.jp/pages/56/')
    assert rows == [('https://example.jp/files/libs/7/20261101121212.pdf', '資料日中経済 · PDF 7')]
