from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from filelock import FileLock
from lxml import html

from radar.models import KeywordCandidate, Source, SourceRun
from radar.services import enqueue_source, process_run


def card(response, source):
    return html.fromstring(response.content).xpath(f'//*[@data-source-id="{source.pk}"]')[0]


@pytest.mark.django_db
def test_manual_button_queues_free_source_while_schedule_and_worker_are_off(client, settings, tmp_path):
    settings.DATA_DIR = tmp_path
    settings.LEAD_AUTOMATIC_ENABLED = False
    source = Source.objects.create(key='shokochukin_news', name='商工中金')
    url = reverse('radar:run_source', args=[source.pk])
    before = card(client.get(reverse('radar:sources')), source)
    assert before.xpath(f'.//form[@action="{url}"]//button[not(@disabled)]')
    first = client.post(url, follow=True)
    assert '已加入队列，启动后台采集进程后执行。' in first.content.decode()
    client.post(url)
    assert SourceRun.objects.get(source=source).status == 'queued'


@pytest.mark.django_db
def test_policy_source_offers_import_and_blocks_old_queued_jobs(client, monkeypatch):
    source = Source.objects.create(key='jbic_sme_china', name='日本国际协力银行')
    url = reverse('radar:run_source', args=[source.pk])
    row = card(client.get(reverse('radar:sources')), source)
    assert not row.xpath(f'.//form[@action="{url}"]')
    assert row.xpath(f'.//a[@href="{reverse("radar:jbic_import")}"]')
    assert '商业复制权限' in row.text_content()
    client.post(url)
    assert not SourceRun.objects.filter(source=source).exists()
    monkeypatch.setattr('radar.services._execute_collector', lambda _: pytest.fail('policy source fetched'))
    assert process_run(SourceRun.objects.create(source=source).pk).status == 'blocked'


@pytest.mark.django_db
def test_dead_worker_job_is_replaced_but_active_locked_job_is_reused(settings, tmp_path):
    settings.DATA_DIR = tmp_path
    source = Source.objects.create(key='ysczw', name='运输采招')
    interrupted = SourceRun.objects.create(source=source, status='running', started_at=timezone.now())
    with FileLock(str(tmp_path / 'worker.lock')):
        assert enqueue_source(source.pk).pk == interrupted.pk
    replacement = enqueue_source(source.pk)
    interrupted.refresh_from_db()
    assert interrupted.status == 'failed' and replacement.status == 'queued'
    SourceRun.objects.filter(pk=replacement.pk).update(requested_at=timezone.now()-timedelta(hours=3))
    assert enqueue_source(source.pk).pk != replacement.pk
    replacement.refresh_from_db()
    assert replacement.status == 'cancelled'


@pytest.mark.django_db
def test_research_candidates_and_successful_partial_run_update_card(client):
    source = Source.objects.create(key='shokochukin_news', name='商工中金')
    KeywordCandidate.objects.create(provider=source.key, canonical_url='https://example.jp/notice',
                                    title='合成融资案例', query_id='test')
    SourceRun.objects.create(source=source, status='partial', discovered_count=1, finished_at=timezone.now())
    row = card(client.get(reverse('radar:sources')), source)
    assert row.xpath('string(.//div[@class="source-facts"]/div[1]/strong)') == '1'
    assert '尚未成功采集' not in row.text_content()


@pytest.mark.django_db
def test_free_source_dispatch_records_partial_success_once(monkeypatch):
    source = Source.objects.create(key='shokochukin_news', name='商工中金')
    monkeypatch.setattr('radar.public_fetch.fetch', lambda *args, **kwargs: pytest.fail('duplicate index probe'))
    calls = []

    def collect(key, *, max_details, record_run):
        calls.append((key, max_details, record_run))
        return {'errors': [{'reason': 'timeout'}], 'requests': [{'url': 'https://example.jp/index'}],
                'created': [1, 2], 'retained': 2, 'endpoints': [], 'checkpoint': None, 'status': 'partial'}

    monkeypatch.setattr('collectors.china_entry_sources.collect_group', collect)
    run = process_run(SourceRun.objects.create(source=source).pk)
    source.refresh_from_db()
    assert len(calls) == 1 and calls[0][0] == source.key and calls[0][2] is False
    assert (run.status, run.new_count, run.discovered_count) == ('partial', 2, 2)
    assert source.last_success_at is not None and SourceRun.objects.filter(source=source).count() == 1
