"""Process manually submitted local collection jobs."""
import logging
import os
import time
from threading import Thread

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from radar.models import RuntimeState, SourceRun
from radar.services import automatic_enabled, process_pending


def heartbeat():
    while True:
        try:
            close_old_connections()
            RuntimeState.objects.update_or_create(
                key="worker_heartbeat", defaults={"value": {"pid": os.getpid(), "mode": "source_worker"}}
            )
        except Exception:
            logging.exception("暂时无法更新采集后台状态")
        finally:
            close_old_connections()
        time.sleep(15)


class Command(BaseCommand):
    help = "Process the local source queue. Scheduled discovery follows the existing global switch."

    def handle(self, *args, **options):
        Thread(target=heartbeat, daemon=True).start()
        while True:
            try:
                close_old_connections()
                if automatic_enabled() or SourceRun.objects.filter(status__in=["queued", "running"]).exists():
                    process_pending(max_runs=1)
            except Exception:
                logging.exception("采集后台暂时无法读取任务")
            finally:
                close_old_connections()
            time.sleep(2)
