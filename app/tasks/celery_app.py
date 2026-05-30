"""
Celery application configuration.

Uses Redis as broker and result backend. Configured for reliability:
- acks_late: tasks acknowledged only after completion
- reject_on_worker_lost: requeue tasks if worker dies
- task_track_started: track when tasks begin execution
"""

from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "geta_lead_pipeline",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    # --- Serialization ---
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",

    # --- Reliability ---
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,

    # --- Timeouts ---
    task_soft_time_limit=120,   # Soft limit: 2 minutes
    task_time_limit=180,        # Hard limit: 3 minutes

    # --- Result expiry ---
    result_expires=3600,  # Results expire after 1 hour

    # --- Queues ---
    task_default_queue="default",
    task_routes={
        "app.tasks.lead_pipeline.*": {"queue": "leads"},
    },

    # --- Timezone ---
    timezone="UTC",
    enable_utc=True,
)

# Auto-discover tasks in app.tasks package
celery_app.autodiscover_tasks(["app.tasks"])

# Explicit import to guarantee task registration
import app.tasks.lead_pipeline  # noqa: F401, E402
