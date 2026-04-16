# File location: /server/billing/tasks.py
from __future__ import annotations

import logging
from typing import Optional

import dramatiq
from django.db.models import F
from django.utils import timezone as django_tz

logger = logging.getLogger(__name__)


@dramatiq.actor(
    queue_name='billing',
    max_retries=3,
    min_backoff=1000,
    max_backoff=30_000,
)
def record_feature_chat_usage(
    *,
    user_uuid: str,
    org_uuid: str,
    discussion_uuid: str,
    source: str,
    promo_redemption_uuid: Optional[str] = None,
) -> None:
    from authentication.models import User
    from billing.models import UsageRecord
    from organization.models import Organization

    try:
        user = User.objects.get(uuid=user_uuid)
        org = Organization.objects.get(uuid=org_uuid)
    except (User.DoesNotExist, Organization.DoesNotExist) as exc:
        logger.error('Cannot record billing usage — missing entity: %s', exc)
        return

    _, created = UsageRecord.objects.get_or_create(
        discussion_uuid=discussion_uuid,
        defaults={
            'user': user,
            'organization': org,
            'source': source,
            'promo_redemption_uuid': promo_redemption_uuid or '',
        },
    )

    if not created:
        logger.info(
            'Usage already recorded for discussion %s — skipping.',
            discussion_uuid,
        )
        return

    charged = False

    if source == 'free_allowance':
        charged = _record_free_allowance(user, org, discussion_uuid)

    elif source == 'promo':
        charged = _record_promo(user, org, discussion_uuid, promo_redemption_uuid)

    elif source == 'subscription':
        charged = _record_subscription(user, org, discussion_uuid)

    else:
        logger.error(
            'Unknown billing source "%s" for discussion %s',
            source, discussion_uuid,
        )

    if not charged:
        UsageRecord.objects.filter(discussion_uuid=discussion_uuid).delete()
        logger.warning(
            'Charge failed for discussion %s source=%s — '
            'idempotency record removed for retry.',
            discussion_uuid, source,
        )


def _record_free_allowance(user, org, discussion_uuid: str) -> bool:
    from billing.models import FreeOrgAllowance

    updated = FreeOrgAllowance.objects.filter(
        user=user,
        organization=org,
        is_deleted=False,
        chats_used__lt=F('chats_allowed'),
    ).update(
        chats_used=F('chats_used') + 1,
        updated_at=django_tz.now(),
    )

    if updated:
        logger.info(
            'Recorded free-allowance usage: user=%s, org=%s, discussion=%s',
            user.uuid, org.uuid, discussion_uuid,
        )
        return True

    logger.warning(
        'No FreeOrgAllowance with remaining credits: user=%s, org=%s',
        user.uuid, org.uuid,
    )
    return False


def _record_promo(user, org, discussion_uuid: str, redemption_uuid: Optional[str]) -> bool:
    from billing.models import PromoRedemption

    qs = PromoRedemption.objects.filter(
        user=user,
        organization=org,
        is_deleted=False,
    )

    if redemption_uuid:
        qs = qs.filter(uuid=redemption_uuid)
    else:
        qs = qs.filter(
            chats_used__lt=F('chats_granted'),
        ).order_by('created_at')

    redemption = qs.first()

    if redemption is None:
        logger.warning(
            'No PromoRedemption to update: user=%s, org=%s, uuid=%s',
            user.uuid, org.uuid, redemption_uuid,
        )
        return False

    updated = PromoRedemption.objects.filter(
        pk=redemption.pk,
        chats_used__lt=F('chats_granted'),
    ).update(
        chats_used=F('chats_used') + 1,
        updated_at=django_tz.now(),
    )

    if updated:
        logger.info(
            'Recorded promo usage: user=%s, org=%s, discussion=%s, promo=%s',
            user.uuid, org.uuid, discussion_uuid, redemption.promo_code_id,
        )
        return True

    logger.warning(
        'Promo %s has no remaining credits: user=%s, org=%s',
        redemption.pk, user.uuid, org.uuid,
    )
    return False


def _record_subscription(user, org, discussion_uuid: str) -> bool:
    from billing.models import Subscription, SubscriptionStatus, UsagePeriod

    now = django_tz.now()

    sub = Subscription.objects.filter(
        user=user,
        organization=org,
        status__in=[SubscriptionStatus.ACTIVE, SubscriptionStatus.CANCELED],
        current_period_end__gt=now,
        is_deleted=False,
    ).first()

    if sub is None:
        logger.warning(
            'No active subscription to update: user=%s, org=%s',
            user.uuid, org.uuid,
        )
        return False

    updated = UsagePeriod.objects.filter(
        subscription=sub,
        period_start__lte=now,
        period_end__gt=now,
        is_deleted=False,
        feature_chats_created__lt=F('feature_chats_limit'),
    ).update(
        feature_chats_created=F('feature_chats_created') + 1,
        updated_at=django_tz.now(),
    )

    if updated:
        logger.info(
            'Recorded subscription usage: user=%s, org=%s, discussion=%s, sub=%s',
            user.uuid, org.uuid, discussion_uuid, sub.uuid,
        )
        return True

    logger.warning(
        'No UsagePeriod with remaining credits for sub=%s', sub.uuid,
    )
    return False
