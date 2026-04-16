# File location: /server/security/tasks.py
from __future__ import annotations

import logging
from typing import Optional

import dramatiq
from django.utils import timezone as django_tz

logger = logging.getLogger(__name__)


@dramatiq.actor(
    queue_name='security',
    max_retries=3,
    min_backoff=1000,
    max_backoff=30_000,
)
def log_security_violation(
    *,
    user_uuid: str,
    org_uuid: str,
    discussion_uuid: str,
    message_text: str,
    detected_intent: str,
    violation_number: int,
    resulted_in_block: bool,
    block_level_applied: Optional[int],
) -> None:
    from authentication.models import User
    from organization.models import Organization
    from security.models import SecurityViolation, UserSecurityProfile

    try:
        user = User.objects.get(uuid=user_uuid)
        org = Organization.objects.get(uuid=org_uuid)
    except (User.DoesNotExist, Organization.DoesNotExist) as exc:
        logger.error('Cannot log violation — missing entity: %s', exc)
        return

    SecurityViolation.objects.create(
        user=user,
        organization=org,
        discussion_uuid=discussion_uuid,
        message_text=message_text,
        detected_intent=detected_intent,
        violation_number=violation_number,
        resulted_in_block=resulted_in_block,
        block_level_applied=block_level_applied,
    )

    profile, _created = UserSecurityProfile.objects.get_or_create(
        user=user,
    )
    profile.total_violations += 1
    profile.last_violation_at = django_tz.now()

    if resulted_in_block and block_level_applied is not None:
        profile.total_blocks += 1
        profile.current_block_level = block_level_applied
        profile.last_blocked_at = django_tz.now()

        from security.constants import BLOCK_DURATION_MAP, MAX_BLOCK_LEVEL

        if block_level_applied >= MAX_BLOCK_LEVEL:
            profile.is_permanently_blocked = True
            profile.block_expires_at = None
        else:
            duration = BLOCK_DURATION_MAP.get(block_level_applied)
            if duration:
                from datetime import timedelta
                profile.block_expires_at = (
                    django_tz.now() + timedelta(seconds=duration)
                )

    profile.save()

    logger.info(
        'Logged violation: user=%s, org=%s, chat=%s, '
        'violation=#%d, blocked=%s, level=%s',
        user_uuid, org_uuid, discussion_uuid,
        violation_number, resulted_in_block, block_level_applied,
    )
