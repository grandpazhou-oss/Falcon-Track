"""The local Prefect deployment. Uses the user's local server, never Cloud login."""
import os
import logging
import threading
import time
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from django.db import close_old_connections
from django.utils import timezone
from prefect import flow
from prefect.runtime import flow_run
from radar.models import RuntimeState
from radar.services import process_pending

@flow(name="物流商机 · 本机采集", retries=0, log_prints=False)
def dispatch():
    close_old_connections()
    result = process_pending(max_runs=1, prefect_run_id=str(flow_run.id or ""))
    close_old_connections()
    return result


def analysis_loop(stop_event, poll_seconds=2):
    """Independent worker: a slow website cannot hold a manual AI job."""
    from radar.analysis import process_pending_analyses
    while not stop_event.is_set():
        try:
            close_old_connections()
            process_pending_analyses(limit=1)
        except Exception:
            logging.exception('后台分析任务轮询失败，稍后重试')
        finally:
            close_old_connections()
        stop_event.wait(poll_seconds)

def heartbeat():
    while True:
        try:
            close_old_connections()
            RuntimeState.objects.update_or_create(key="worker_heartbeat", defaults={"value": {"pid": os.getpid(), "mode": "prefect", "time": timezone.now().isoformat()}})
            close_old_connections()
        except Exception:
            pass
        time.sleep(15)

if __name__ == "__main__":
    threading.Thread(target=heartbeat, daemon=True).start()
    threading.Thread(target=analysis_loop, args=(threading.Event(),), daemon=True).start()
    dispatch.serve(name="local-pilot", interval=30, pause_on_shutdown=True, limit=1)

