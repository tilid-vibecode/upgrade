# File location: /server/billing/providers/stripe/webhooks.py
from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_tz
from typing import Optional
from uuid import UUID

from asgiref.sync import sync_to_async
from django.db import transaction
from django.utils import timezone as django_tz

logger = logging.getLogger(__name__)



def _ts_to_dt(ts: Optional[int]) -> Optional[datetime]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=dt_tz.utc)


def _get_subscription_period(stripe_sub) -> tuple:
    start = None
    end = None

    try:
        start = stripe_sub.current_period_start
    except (AttributeError, KeyError):
        pass

    try:
        end = stripe_sub.current_period_end
    except (AttributeError, KeyError):
        pass

    if start is None or end is None:
        try:
            items_data = stripe_sub['items']['data']
            if items_data:
                item = items_data[0]
                start = start or item.get('current_period_start')
                end = end or item.get('current_period_end')
        except (KeyError, IndexError, TypeError):
            pass

    return _ts_to_dt(start), _ts_to_dt(end)


async def _mark_processed(event_record) -> None:
    event_record.processed = True
    event_record.processed_at = django_tz.now()
    await sync_to_async(event_record.save)(
        update_fields=['processed', 'processed_at'],
    )



async def process_stripe_event(event) -> dict:
    from billing.providers.stripe.models import StripeWebhookEvent

    event_id = event['id']
    event_type = event['type']

    event_record, created = await sync_to_async(
        StripeWebhookEvent.objects.get_or_create
    )(
        stripe_event_id=event_id,
        defaults={
            'event_type': event_type,
            'payload': dict(event),
        },
    )

    if not created and event_record.processed:
        logger.info('Skipping already-processed event %s', event_id)
        return {'status': 'skipped', 'reason': 'already_processed'}

    def _try_lock_event():
        with transaction.atomic():
            locked = (
                StripeWebhookEvent.objects
                .select_for_update(skip_locked=True)
                .filter(pk=event_record.pk, processed=False)
                .first()
            )
            if locked is not None:
                locked.processed_at = django_tz.now()
                locked.save(update_fields=['processed_at'])
            return locked

    locked_record = await sync_to_async(_try_lock_event)()

    if locked_record is None:
        logger.info(
            'Event %s is being processed by another worker — skipping.',
            event_id,
        )
        return {'status': 'skipped', 'reason': 'locked_by_another_worker'}

    handler = _HANDLERS.get(event_type)
    if handler is None:
        logger.debug('Unhandled Stripe event type: %s', event_type)
        await _mark_processed(locked_record)
        return {'status': 'ignored', 'event_type': event_type}

    try:
        result = await handler(event)
        await _mark_processed(locked_record)
        logger.info('Processed %s (%s): %s', event_type, event_id, result)
        return {'status': 'processed', 'event_type': event_type, **result}
    except Exception:
        locked_record.processed_at = None
        await sync_to_async(locked_record.save)(
            update_fields=['processed_at'],
        )
        logger.exception('Error processing %s (%s)', event_type, event_id)
        raise



async def _handle_checkout_completed(event) -> dict:
    from billing.models import (
        PaymentRecord,
        PaymentStatus,
        Plan,
        PlanPrice,
        Subscription,
        SubscriptionStatus,
        UsagePeriod,
    )
    from billing.providers.stripe.models import StripeSubscriptionLink

    session = event['data']['object']
    metadata = session.get('metadata', {})
    stripe_sub_id = session.get('subscription')

    if not stripe_sub_id:
        logger.warning('checkout.session.completed without subscription ID — skipping.')
        return {'action': 'skipped', 'reason': 'no_subscription'}

    user_uuid = metadata.get('mula_user_uuid')
    org_uuid = metadata.get('mula_org_uuid')
    plan_slug = metadata.get('mula_plan_slug')
    plan_price_uuid = metadata.get('mula_plan_price_uuid')
    paid_by_uuid = metadata.get('mula_paid_by_uuid')
    currency = metadata.get('mula_currency', 'usd')

    if not all([user_uuid, org_uuid, plan_slug]):
        logger.error('checkout.session.completed missing required metadata: %s', metadata)
        return {'action': 'error', 'reason': 'missing_metadata'}

    from authentication.models import User
    from organization.models import Organization

    try:
        user = await sync_to_async(User.objects.get)(uuid=user_uuid)
        org = await sync_to_async(Organization.objects.get)(uuid=org_uuid)
        plan = await sync_to_async(Plan.objects.get)(slug=plan_slug, is_deleted=False)
    except (User.DoesNotExist, Organization.DoesNotExist, Plan.DoesNotExist) as exc:
        logger.error('Cannot resolve entities for checkout: %s', exc)
        return {'action': 'error', 'reason': str(exc)}

    plan_price = None
    if plan_price_uuid:
        plan_price = await sync_to_async(
            PlanPrice.objects.filter(uuid=plan_price_uuid).first
        )()
    if plan_price is None:
        plan_price = await sync_to_async(
            PlanPrice.objects.filter(plan=plan, currency=currency, is_active=True).first
        )()
    if plan_price is None:
        logger.error('No PlanPrice found for plan=%s currency=%s', plan_slug, currency)
        return {'action': 'error', 'reason': 'no_plan_price'}

    paid_by = user
    if paid_by_uuid and paid_by_uuid != str(user.uuid):
        try:
            paid_by = await sync_to_async(User.objects.get)(uuid=paid_by_uuid)
        except User.DoesNotExist:
            paid_by = user

    import stripe
    stripe_sub = await sync_to_async(stripe.Subscription.retrieve)(stripe_sub_id)
    period_start, period_end = _get_subscription_period(stripe_sub)

    customer_id = session.get('customer') or ''

    def _create_subscription_records():
        with transaction.atomic():
            Subscription.objects.filter(
                user=user,
                organization=org,
                status__in=[
                    SubscriptionStatus.ACTIVE,
                    SubscriptionStatus.CANCELED,
                    SubscriptionStatus.PAST_DUE,
                ],
            ).update(status=SubscriptionStatus.EXPIRED, updated_at=django_tz.now())

            sub = Subscription.objects.create(
                user=user,
                organization=org,
                plan=plan,
                plan_price=plan_price,
                status=SubscriptionStatus.ACTIVE,
                currency=currency,
                current_period_start=period_start,
                current_period_end=period_end,
                paid_by=paid_by,
                provider_subscription_id=stripe_sub_id,
                provider_customer_id=customer_id,
            )

            UsagePeriod.objects.create(
                subscription=sub,
                period_start=period_start,
                period_end=period_end,
                feature_chats_created=0,
                feature_chats_limit=plan.monthly_feature_chats,
            )

            StripeSubscriptionLink.objects.create(
                subscription=sub,
                stripe_subscription_id=stripe_sub_id,
                stripe_price_id=plan_price.stripe_price_id,
            )

            amount = session.get('amount_total', plan_price.price_cents)
            PaymentRecord.objects.create(
                user=user,
                organization=org,
                subscription=sub,
                amount_cents=amount or 0,
                currency=currency,
                status=PaymentStatus.SUCCEEDED,
                description=f'Subscription to {plan.name} ({plan.slug})',
                provider_payment_id=session.get('payment_intent') or '',
                provider_data={'checkout_session_id': session.get('id') or ''},
            )

            return sub

    sub = await sync_to_async(_create_subscription_records)()

    logger.info(
        'Created subscription %s for user=%s org=%s plan=%s',
        sub.uuid, user_uuid, org_uuid, plan_slug,
    )
    return {'action': 'subscription_created', 'subscription_uuid': str(sub.uuid)}



async def _handle_invoice_paid(event) -> dict:
    from billing.models import (
        PaymentRecord,
        PaymentStatus,
        Subscription,
        SubscriptionStatus,
        UsagePeriod,
    )

    invoice = event['data']['object']
    stripe_sub_id = invoice.get('subscription')

    if not stripe_sub_id:
        return {'action': 'skipped', 'reason': 'no_subscription_on_invoice'}

    billing_reason = invoice.get('billing_reason', '')
    if billing_reason == 'subscription_create':
        return {'action': 'skipped', 'reason': 'initial_invoice'}

    sub = await sync_to_async(
        Subscription.objects.filter(
            provider_subscription_id=stripe_sub_id,
            is_deleted=False,
        )
        .select_related('plan', 'plan_price')
        .first
    )()

    if sub is None:
        logger.warning('invoice.paid: no matching subscription for %s', stripe_sub_id)
        return {'action': 'skipped', 'reason': 'no_subscription'}

    import stripe
    stripe_sub = await sync_to_async(stripe.Subscription.retrieve)(stripe_sub_id)
    period_start, period_end = _get_subscription_period(stripe_sub)

    sub.current_period_start = period_start
    sub.current_period_end = period_end
    sub.status = SubscriptionStatus.ACTIVE
    await sync_to_async(sub.save)(
        update_fields=[
            'current_period_start', 'current_period_end',
            'status', 'updated_at',
        ],
    )

    _, created = await sync_to_async(UsagePeriod.objects.get_or_create)(
        subscription=sub,
        period_start=period_start,
        defaults={
            'period_end': period_end,
            'feature_chats_created': 0,
            'feature_chats_limit': sub.plan.monthly_feature_chats,
        },
    )

    amount = invoice.get('amount_paid', 0)
    currency = invoice.get('currency', sub.currency)
    await sync_to_async(PaymentRecord.objects.create)(
        user=sub.user,
        organization=sub.organization,
        subscription=sub,
        amount_cents=amount,
        currency=currency,
        status=PaymentStatus.SUCCEEDED,
        description=f'Renewal — {sub.plan.name}',
        provider_payment_id=invoice.get('payment_intent') or '',
        provider_data={'invoice_id': invoice.get('id') or ''},
    )

    action = 'usage_period_created' if created else 'usage_period_exists'
    return {'action': action, 'subscription_uuid': str(sub.uuid)}



async def _handle_invoice_payment_failed(event) -> dict:
    from billing.models import Subscription, SubscriptionStatus

    invoice = event['data']['object']
    stripe_sub_id = invoice.get('subscription')

    if not stripe_sub_id:
        return {'action': 'skipped', 'reason': 'no_subscription'}

    updated = await sync_to_async(
        Subscription.objects.filter(
            provider_subscription_id=stripe_sub_id,
            is_deleted=False,
        ).update
    )(status=SubscriptionStatus.PAST_DUE, updated_at=django_tz.now())

    if updated:
        logger.warning('Subscription %s marked past_due after payment failure.', stripe_sub_id)
    return {'action': 'marked_past_due', 'updated': updated}



async def _handle_subscription_updated(event) -> dict:
    from billing.models import Subscription, SubscriptionStatus

    stripe_sub = event['data']['object']
    stripe_sub_id = stripe_sub.get('id')

    sub = await sync_to_async(
        Subscription.objects.filter(
            provider_subscription_id=stripe_sub_id,
            is_deleted=False,
        ).first
    )()

    if sub is None:
        return {'action': 'skipped', 'reason': 'no_subscription'}

    _STATUS_MAP = {
        'active': SubscriptionStatus.ACTIVE,
        'past_due': SubscriptionStatus.PAST_DUE,
        'canceled': SubscriptionStatus.CANCELED,
        'unpaid': SubscriptionStatus.UNPAID,
        'trialing': SubscriptionStatus.TRIALING,
    }

    stripe_status = stripe_sub.get('status', '')
    new_status = _STATUS_MAP.get(stripe_status)

    fields_to_update = ['updated_at']

    if new_status and new_status != sub.status:
        sub.status = new_status
        fields_to_update.append('status')

    cancel_at = stripe_sub.get('cancel_at_period_end', False)
    if cancel_at != sub.cancel_at_period_end:
        sub.cancel_at_period_end = cancel_at
        fields_to_update.append('cancel_at_period_end')

    period_start = _ts_to_dt(stripe_sub.get('current_period_start'))
    period_end = _ts_to_dt(stripe_sub.get('current_period_end'))

    if period_start and period_start != sub.current_period_start:
        sub.current_period_start = period_start
        fields_to_update.append('current_period_start')

    if period_end and period_end != sub.current_period_end:
        sub.current_period_end = period_end
        fields_to_update.append('current_period_end')

    await sync_to_async(sub.save)(update_fields=fields_to_update)

    return {'action': 'synced', 'subscription_uuid': str(sub.uuid), 'status': sub.status}



async def _handle_subscription_deleted(event) -> dict:
    from billing.models import Subscription, SubscriptionStatus

    stripe_sub = event['data']['object']
    stripe_sub_id = stripe_sub.get('id')

    updated = await sync_to_async(
        Subscription.objects.filter(
            provider_subscription_id=stripe_sub_id,
            is_deleted=False,
        ).update
    )(status=SubscriptionStatus.EXPIRED, updated_at=django_tz.now())

    if updated:
        logger.info('Subscription %s expired (Stripe deleted).', stripe_sub_id)
    return {'action': 'expired', 'updated': updated}



async def _handle_charge_refunded(event) -> dict:
    from billing.models import PaymentRecord, PaymentStatus

    charge = event['data']['object']
    payment_intent = charge.get('payment_intent') or ''

    if payment_intent:
        updated = await sync_to_async(
            PaymentRecord.objects.filter(
                provider_payment_id=payment_intent,
            ).update
        )(status=PaymentStatus.REFUNDED, updated_at=django_tz.now())
        return {'action': 'refund_recorded', 'updated': updated}

    return {'action': 'skipped', 'reason': 'no_payment_intent'}



_HANDLERS = {
    'checkout.session.completed': _handle_checkout_completed,
    'invoice.paid': _handle_invoice_paid,
    'invoice.payment_failed': _handle_invoice_payment_failed,
    'customer.subscription.updated': _handle_subscription_updated,
    'customer.subscription.deleted': _handle_subscription_deleted,
    'charge.refunded': _handle_charge_refunded,
}
