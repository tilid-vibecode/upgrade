# File location: /server/scheduler/feature_monitors.py

import logging
from datetime import timedelta

import dramatiq
from django.db import close_old_connections
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger('scheduler')

STALE_THRESHOLD = timedelta(minutes=10)
PENDING_BACKLOG_THRESHOLD = timedelta(minutes=3)
MAX_RETRIES = 3


@dramatiq.actor(max_retries=1, min_backoff=5_000, time_limit=60_000, queue_name='default')
def sweep_stale_messages() -> None:
    close_old_connections()

    from feature.models import DiscussionMessage, ProcessingStatus
    from feature.tasks import process_user_message_task

    cutoff = timezone.now() - STALE_THRESHOLD

    stale_qs = DiscussionMessage.objects.filter(
        processing_status=ProcessingStatus.PROCESSING,
        processing_started_at__lt=cutoff,
        retry_count__lt=MAX_RETRIES,
    )

    stale_messages = list(stale_qs.values(
        'uuid', 'discussion_uuid', 'organization_uuid', 'retry_count',
    ))

    if not stale_messages:
        return

    logger.warning('Found %d stale message(s) -- re-enqueueing.', len(stale_messages))

    uuids = [m['uuid'] for m in stale_messages]
    updated = stale_qs.filter(uuid__in=uuids).update(
        processing_status=ProcessingStatus.RETRYING,
        processing_completed_at=timezone.now(),
        retry_count=F('retry_count') + 1,
        worker_id=None,
    )
    logger.info('Marked %d message(s) as RETRYING.', updated)

    for msg in stale_messages:
        try:
            process_user_message_task.send(
                message_uuid=str(msg['uuid']),
                discussion_uuid=str(msg['discussion_uuid']),
                org_uuid=str(msg['organization_uuid']),
            )
            logger.info(
                'Re-enqueued message %s (retry #%d).',
                msg['uuid'], msg['retry_count'] + 1,
            )
        except Exception as exc:
            logger.error(
                'Failed to re-enqueue message %s: %s', msg['uuid'], exc,
            )
            DiscussionMessage.objects.filter(
                uuid=msg['uuid'],
                processing_status=ProcessingStatus.RETRYING,
            ).update(
                processing_status=ProcessingStatus.PROCESSING,
                retry_count=F('retry_count') - 1,
            )


@dramatiq.actor(max_retries=1, min_backoff=5_000, time_limit=60_000, queue_name='default')
def mark_permanently_failed() -> None:
    close_old_connections()

    from feature.models import DiscussionMessage, ProcessingStatus

    cutoff = timezone.now() - STALE_THRESHOLD

    count = DiscussionMessage.objects.filter(
        processing_status__in=[
            ProcessingStatus.PROCESSING,
            ProcessingStatus.RETRYING,
        ],
        processing_started_at__lt=cutoff,
        retry_count__gte=MAX_RETRIES,
    ).update(
        processing_status=ProcessingStatus.FAILED,
        processing_completed_at=timezone.now(),
        worker_id=None,
    )

    if count:
        logger.error(
            '%d message(s) permanently failed after %d retries.', count, MAX_RETRIES,
        )


@dramatiq.actor(max_retries=1, min_backoff=5_000, time_limit=30_000, queue_name='default')
def alert_pending_backlog() -> None:
    close_old_connections()

    from feature.models import DiscussionMessage, ProcessingStatus

    cutoff = timezone.now() - PENDING_BACKLOG_THRESHOLD

    backlog_count = DiscussionMessage.objects.filter(
        processing_status=ProcessingStatus.PENDING,
        processing_enqueued_at__isnull=False,
        processing_enqueued_at__lt=cutoff,
    ).count()

    if backlog_count > 0:
        logger.warning(
            'PENDING backlog alert: %d message(s) waiting >%s. '
            'Consider scaling feature_processing workers.',
            backlog_count, PENDING_BACKLOG_THRESHOLD,
        )
