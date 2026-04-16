# File location: /server/scheduler/tasks.py

import logging
import os
from datetime import timedelta

import dramatiq
from django.utils import timezone

logger = logging.getLogger('scheduler')

_RETENTION_DAYS = int(os.getenv('SCHEDULER_EXECUTION_RETENTION_DAYS', '30'))


@dramatiq.actor(max_retries=2, min_backoff=10_000, queue_name='default')
def cleanup_old_executions():
    from .models import TaskExecution

    cutoff = timezone.now() - timedelta(days=_RETENTION_DAYS)
    count, _ = TaskExecution.objects.filter(dispatched_at__lt=cutoff).delete()

    if count:
        logger.info('Cleaned up %d execution records older than %d days.', count, _RETENTION_DAYS)
