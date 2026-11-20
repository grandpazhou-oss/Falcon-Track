from django.conf import settings
from django.utils import timezone
from radar.models import RuntimeState

def collector_alive(state=None):
    state = state or RuntimeState.objects.filter(key="worker_heartbeat").first()
    return bool(state and (timezone.now() - state.updated_at).total_seconds() < 90)


def runtime_context(request):
    from radar.services import automatic_enabled
    state = RuntimeState.objects.filter(key="worker_heartbeat").first()
    alive = collector_alive(state)
    from .ai_config import config
    from radar.sales_ui import sales_data
    _,groups=sales_data(request)
    return {"nav_count_a":len(groups["A"]), "nav_count_b":len(groups["B"]), "app_version": "0.2.0", "model_enabled": config().enabled,
            "automatic_enabled": automatic_enabled(), "worker_alive": alive,
            "worker_last_seen": state.updated_at if state else None,
            "local_pilot": True}

