# File location: /server/billing/managers.py
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Tuple

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import models, transaction
from django.db.models import F, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

FREE_ORG_FEATURE_CHATS: int = getattr(settings, 'FREE_ORG_FEATURE_CHATS', 1)


@dataclass
class BillingAllowance:

    allowed: bool
    source: str = ''           # 'free_allowance' | 'promo' | 'subscription' | ''
    reason: str = ''
    promo_redemption_uuid: Optional[str] = None
    subscription_uuid: Optional[str] = None
    usage_period_uuid: Optional[str] = None


class BillingManager:


    @staticmethod
    async def can_create_feature_chat(user, organization) -> Tuple[bool, BillingAllowance]:
        from billing.models import (
            FreeOrgAllowance,
            PromoRedemption,
            Subscription,
            SubscriptionStatus,
            UsagePeriod,
        )

        now = timezone.now()

        free_allowance = await sync_to_async(
            FreeOrgAllowance.objects.filter(
                user=user,
                organization=organization,
                is_deleted=False,
                chats_used__lt=F('chats_allowed'),
            )
            .first
        )()

        if free_allowance is not None:
            return True, BillingAllowance(
                allowed=True,
                source='free_allowance',
            )

        promo = await sync_to_async(
            PromoRedemption.objects.filter(
                user=user,
                organization=organization,
                is_deleted=False,
                chats_used__lt=F('chats_granted'),
            )
            .order_by('created_at')     # FIFO — oldest promo first
            .first
        )()

        if promo is not None:
            return True, BillingAllowance(
                allowed=True,
                source='promo',
                promo_redemption_uuid=str(promo.uuid),
            )

        sub = await sync_to_async(
            Subscription.objects.filter(
                user=user,
                organization=organization,
                status__in=[
                    SubscriptionStatus.ACTIVE,
                    SubscriptionStatus.CANCELED,
                ],
                current_period_end__gt=now,
                is_deleted=False,
            )
            .select_related('plan')
            .first
        )()

        if sub is not None:
            usage = await sync_to_async(
                UsagePeriod.objects.filter(
                    subscription=sub,
                    period_start__lte=now,
                    period_end__gt=now,
                    is_deleted=False,
                ).first
            )()

            if usage is None:
                logger.warning(
                    'No UsagePeriod for subscription %s in current cycle — allowing.',
                    sub.uuid,
                )
                return True, BillingAllowance(
                    allowed=True,
                    source='subscription',
                    subscription_uuid=str(sub.uuid),
                )

            if usage.feature_chats_created < usage.feature_chats_limit:
                return True, BillingAllowance(
                    allowed=True,
                    source='subscription',
                    subscription_uuid=str(sub.uuid),
                    usage_period_uuid=str(usage.uuid),
                )

            return False, BillingAllowance(
                allowed=False,
                reason=(
                    f'Monthly limit reached '
                    f'({usage.feature_chats_created}/{usage.feature_chats_limit} used). '
                    f'Resets on {usage.period_end:%b %d, %Y}.'
                ),
            )

        return False, BillingAllowance(
            allowed=False,
            reason='No active subscription or credits. Upgrade to create feature chats.',
        )


    @staticmethod
    async def reserve_feature_chat_credit(
        user,
        organization,
        discussion_uuid: str,
    ) -> Tuple[bool, BillingAllowance]:
        from billing.models import (
            FreeOrgAllowance,
            PromoRedemption,
            Subscription,
            SubscriptionStatus,
            UsagePeriod,
            UsageRecord,
        )

        now = timezone.now()

        existing = await sync_to_async(
            UsageRecord.objects.filter(
                discussion_uuid=discussion_uuid,
            ).first
        )()
        if existing is not None:
            return True, BillingAllowance(
                allowed=True,
                source=existing.source,
            )

        def _try_reserve_free():
            with transaction.atomic():
                updated = FreeOrgAllowance.objects.filter(
                    user=user,
                    organization=organization,
                    is_deleted=False,
                    chats_used__lt=F('chats_allowed'),
                ).update(
                    chats_used=F('chats_used') + 1,
                    updated_at=now,
                )
                if updated:
                    UsageRecord.objects.create(
                        user=user,
                        organization=organization,
                        discussion_uuid=discussion_uuid,
                        source='free_allowance',
                    )
                return updated

        updated = await sync_to_async(_try_reserve_free)()
        if updated:
            logger.info(
                'Reserved free-allowance credit: user=%s org=%s discussion=%s',
                user.uuid, organization.uuid, discussion_uuid,
            )
            return True, BillingAllowance(allowed=True, source='free_allowance')

        promo = await sync_to_async(
            PromoRedemption.objects.filter(
                user=user,
                organization=organization,
                is_deleted=False,
                chats_used__lt=F('chats_granted'),
            )
            .order_by('created_at')
            .first
        )()
        if promo is not None:
            def _try_reserve_promo():
                with transaction.atomic():
                    updated = PromoRedemption.objects.filter(
                        pk=promo.pk,
                        chats_used__lt=F('chats_granted'),
                    ).update(
                        chats_used=F('chats_used') + 1,
                        updated_at=now,
                    )
                    if updated:
                        UsageRecord.objects.create(
                            user=user,
                            organization=organization,
                            discussion_uuid=discussion_uuid,
                            source='promo',
                            promo_redemption_uuid=str(promo.uuid),
                        )
                    return updated

            updated = await sync_to_async(_try_reserve_promo)()
            if updated:
                logger.info(
                    'Reserved promo credit: user=%s org=%s discussion=%s promo=%s',
                    user.uuid, organization.uuid, discussion_uuid, promo.uuid,
                )
                return True, BillingAllowance(
                    allowed=True,
                    source='promo',
                    promo_redemption_uuid=str(promo.uuid),
                )

        sub = await sync_to_async(
            Subscription.objects.filter(
                user=user,
                organization=organization,
                status__in=[
                    SubscriptionStatus.ACTIVE,
                    SubscriptionStatus.CANCELED,
                ],
                current_period_end__gt=now,
                is_deleted=False,
            )
            .select_related('plan')
            .first
        )()
        if sub is not None:
            def _try_reserve_subscription():
                with transaction.atomic():
                    updated = UsagePeriod.objects.filter(
                        subscription=sub,
                        period_start__lte=now,
                        period_end__gt=now,
                        is_deleted=False,
                        feature_chats_created__lt=F('feature_chats_limit'),
                    ).update(
                        feature_chats_created=F('feature_chats_created') + 1,
                        updated_at=now,
                    )
                    if updated:
                        UsageRecord.objects.create(
                            user=user,
                            organization=organization,
                            discussion_uuid=discussion_uuid,
                            source='subscription',
                            subscription_uuid=str(sub.uuid),
                        )
                    return updated

            updated = await sync_to_async(_try_reserve_subscription)()
            if updated:
                logger.info(
                    'Reserved subscription credit: user=%s org=%s discussion=%s sub=%s',
                    user.uuid, organization.uuid, discussion_uuid, sub.uuid,
                )
                return True, BillingAllowance(
                    allowed=True,
                    source='subscription',
                    subscription_uuid=str(sub.uuid),
                )

            usage = await sync_to_async(
                UsagePeriod.objects.filter(
                    subscription=sub,
                    period_start__lte=now,
                    period_end__gt=now,
                    is_deleted=False,
                ).first
            )()
            if usage:
                return False, BillingAllowance(
                    allowed=False,
                    reason=(
                        f'Monthly limit reached '
                        f'({usage.feature_chats_created}/{usage.feature_chats_limit} used). '
                        f'Resets on {usage.period_end:%b %d, %Y}.'
                    ),
                )

        return False, BillingAllowance(
            allowed=False,
            reason='No active subscription or credits. Upgrade to create feature chats.',
        )


    @staticmethod
    async def create_free_org_allowance(user, organization) -> None:
        from billing.models import FreeOrgAllowance

        _, created = await sync_to_async(
            FreeOrgAllowance.objects.get_or_create
        )(
            user=user,
            organization=organization,
            defaults={
                'chats_allowed': FREE_ORG_FEATURE_CHATS,
                'chats_used': 0,
            },
        )

        if created:
            logger.info(
                'Created free org allowance: user=%s, org=%s, chats=%d',
                user.uuid, organization.uuid, FREE_ORG_FEATURE_CHATS,
            )


    @staticmethod
    async def count_free_members_in_discussion(discussion) -> int:
        from billing.models import Subscription, SubscriptionStatus
        from feature.models import DiscussionMember

        now = timezone.now()

        member_user_ids = await sync_to_async(list)(
            DiscussionMember.objects.filter(
                discussion=discussion,
            ).values_list('user_id', flat=True)
        )

        if not member_user_ids:
            return 0

        paid_user_ids = set(await sync_to_async(list)(
            Subscription.objects.filter(
                user_id__in=member_user_ids,
                organization=discussion.organization,
                status__in=[
                    SubscriptionStatus.ACTIVE,
                    SubscriptionStatus.CANCELED,
                ],
                current_period_end__gt=now,
                is_deleted=False,
            ).values_list('user_id', flat=True)
        ))

        free_count = sum(1 for uid in member_user_ids if uid not in paid_user_ids)
        return free_count

    @staticmethod
    async def get_free_member_limit(user, organization) -> int:
        from billing.models import Subscription, SubscriptionStatus

        now = timezone.now()

        sub = await sync_to_async(
            Subscription.objects.filter(
                user=user,
                organization=organization,
                status__in=[
                    SubscriptionStatus.ACTIVE,
                    SubscriptionStatus.CANCELED,
                ],
                current_period_end__gt=now,
                is_deleted=False,
            )
            .select_related('plan')
            .first
        )()

        if sub is not None:
            return sub.plan.max_free_members_per_discussion

        default = getattr(settings, 'DEFAULT_MAX_FREE_MEMBERS', 5)
        return default


    @staticmethod
    async def cancel_subscription_on_removal(membership) -> bool:
        from billing.models import Subscription, SubscriptionStatus

        sub = await sync_to_async(
            Subscription.objects.filter(
                user=membership.user,
                organization=membership.organization,
                status__in=[
                    SubscriptionStatus.ACTIVE,
                    SubscriptionStatus.CANCELED,
                ],
                is_deleted=False,
            ).first
        )()

        if sub is None:
            return True

        if sub.provider_subscription_id:
            try:
                from billing.providers.stripe.client import get_provider

                provider = get_provider()
                await provider.cancel_subscription(
                    provider_subscription_id=sub.provider_subscription_id,
                )
                logger.info(
                    'Provider-side cancel succeeded for subscription %s',
                    sub.provider_subscription_id,
                )
            except Exception:
                logger.exception(
                    'Failed to cancel subscription %s provider-side — '
                    'aborting removal to prevent billing desync.',
                    sub.provider_subscription_id,
                )
                return False

        sub.cancel_at_period_end = True
        sub.canceled_at = timezone.now()
        sub.status = SubscriptionStatus.CANCELED
        await sync_to_async(sub.save)(
            update_fields=['cancel_at_period_end', 'canceled_at', 'status', 'updated_at'],
        )

        logger.info(
            'Canceled subscription %s for removed member %s in org %s',
            sub.uuid, membership.user.uuid, membership.organization.uuid,
        )
        return True


    @staticmethod
    async def user_has_active_subscription(user, organization) -> bool:
        from billing.models import Subscription, SubscriptionStatus

        now = timezone.now()
        return await sync_to_async(
            Subscription.objects.filter(
                user=user,
                organization=organization,
                status__in=[
                    SubscriptionStatus.ACTIVE,
                    SubscriptionStatus.CANCELED,
                ],
                current_period_end__gt=now,
                is_deleted=False,
            ).exists
        )()


    @staticmethod
    async def redeem_promo_code(user, organization, code: str) -> Tuple[bool, str, Optional[int]]:
        from billing.models import PromoCode, PromoRedemption

        code = code.upper().strip()

        def _atomic_redeem():
            with transaction.atomic():
                promo = (
                    PromoCode.objects
                    .select_for_update()
                    .filter(code=code, is_deleted=False)
                    .first()
                )

                if promo is None:
                    return False, 'Invalid promo code.', None

                if not promo.is_valid:
                    if not promo.is_active:
                        return False, 'This promo code has been deactivated.', None
                    if promo.expires_at and promo.expires_at < timezone.now():
                        return False, 'This promo code has expired.', None
                    return False, 'This promo code has reached its usage limit.', None

                if promo.allowed_emails:
                    if user.email.lower() not in [e.lower() for e in promo.allowed_emails]:
                        return False, 'This promo code is not available for your account.', None

                _, created = PromoRedemption.objects.get_or_create(
                    promo_code=promo,
                    user=user,
                    organization=organization,
                    defaults={
                        'chats_granted': promo.granted_feature_chats,
                        'chats_used': 0,
                    },
                )

                if not created:
                    return False, 'You have already redeemed this code for this organization.', None

                updated = PromoCode.objects.filter(
                    pk=promo.pk,
                ).update(
                    redemption_count=F('redemption_count') + 1,
                    updated_at=timezone.now(),
                )

                if not updated:
                    raise Exception('Failed to increment promo usage count')

                return True, f'Success! {promo.granted_feature_chats} free feature chats applied.', promo.granted_feature_chats

        success, message, chats = await sync_to_async(_atomic_redeem)()

        if success:
            logger.info(
                'Promo redeemed: code=%s, user=%s, org=%s, chats=%d',
                code, user.uuid, organization.uuid, chats,
            )

        return success, message, chats


    @staticmethod
    async def get_credits_summary(user, organization) -> dict:
        from billing.models import (
            FreeOrgAllowance,
            PromoRedemption,
            Subscription,
            SubscriptionStatus,
            UsagePeriod,
        )

        now = timezone.now()
        result = {
            'free_allowance': None,
            'promo_credits': [],
            'subscription': None,
            'total_available': 0,
        }

        free = await sync_to_async(
            FreeOrgAllowance.objects.filter(
                user=user, organization=organization, is_deleted=False,
            ).first
        )()

        if free is not None:
            remaining = free.remaining
            result['free_allowance'] = {
                'allowed': free.chats_allowed,
                'used': free.chats_used,
                'remaining': remaining,
            }
            result['total_available'] += remaining

        promos = await sync_to_async(list)(
            PromoRedemption.objects.filter(
                user=user, organization=organization, is_deleted=False,
            ).select_related('promo_code')
        )

        for p in promos:
            rem = p.remaining
            result['promo_credits'].append({
                'code': p.promo_code.code,
                'granted': p.chats_granted,
                'used': p.chats_used,
                'remaining': rem,
            })
            result['total_available'] += rem

        sub = await sync_to_async(
            Subscription.objects.filter(
                user=user,
                organization=organization,
                status__in=[
                    SubscriptionStatus.ACTIVE,
                    SubscriptionStatus.CANCELED,
                ],
                current_period_end__gt=now,
                is_deleted=False,
            )
            .select_related('plan')
            .first
        )()

        if sub is not None:
            usage = await sync_to_async(
                UsagePeriod.objects.filter(
                    subscription=sub,
                    period_start__lte=now,
                    period_end__gt=now,
                    is_deleted=False,
                ).first
            )()

            sub_remaining = 0
            if usage is not None:
                sub_remaining = max(0, usage.feature_chats_limit - usage.feature_chats_created)

            result['subscription'] = {
                'plan_name': sub.plan.name,
                'plan_slug': sub.plan.slug,
                'status': sub.status,
                'cancel_at_period_end': sub.cancel_at_period_end,
                'current_period_end': sub.current_period_end.isoformat(),
                'chats_limit': usage.feature_chats_limit if usage else sub.plan.monthly_feature_chats,
                'chats_used': usage.feature_chats_created if usage else 0,
                'chats_remaining': sub_remaining,
            }
            result['total_available'] += sub_remaining

        return result
