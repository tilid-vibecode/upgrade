# File location: /server/billing/providers/stripe/client.py
from __future__ import annotations

import logging
from typing import Optional

import stripe
from asgiref.sync import sync_to_async
from django.conf import settings

from billing.providers import (
    AbstractPaymentProvider,
    CancelResult,
    CheckoutResult,
    PortalResult,
)

logger = logging.getLogger(__name__)


def _safe_get_period_end(stripe_sub):
    try:
        return stripe_sub.current_period_end
    except (AttributeError, KeyError):
        pass

    try:
        items_data = stripe_sub['items']['data']
        if items_data:
            return items_data[0].get('current_period_end')
    except (KeyError, IndexError, TypeError):
        pass

    return None


def _configure_stripe() -> None:
    stripe.api_key = getattr(settings, 'STRIPE_SECRET_KEY', '')
    if not stripe.api_key:
        logger.warning('STRIPE_SECRET_KEY is not set — Stripe calls will fail.')
        return

    is_test = getattr(settings, 'STRIPE_TEST_MODE', True)
    mode = 'TEST' if is_test else 'LIVE'
    logger.info('Stripe configured in %s mode.', mode)


class StripeProvider(AbstractPaymentProvider):

    def __init__(self) -> None:
        _configure_stripe()


    async def get_or_create_customer(self, user) -> str:
        from billing.providers.stripe.models import StripeCustomer

        existing = await sync_to_async(
            StripeCustomer.objects.filter(user=user).first
        )()

        if existing is not None:
            return existing.stripe_customer_id

        customer = await sync_to_async(stripe.Customer.create)(
            email=user.email,
            name=user.full_name or user.email,
            metadata={'mula_user_uuid': str(user.uuid)},
        )

        await sync_to_async(StripeCustomer.objects.create)(
            user=user,
            stripe_customer_id=customer.id,
        )

        logger.info(
            'Created Stripe customer %s for user %s',
            customer.id, user.uuid,
        )
        return customer.id


    async def create_checkout_session(
        self,
        *,
        user,
        organization,
        plan,
        plan_price,
        paid_by=None,
        success_url: str,
        cancel_url: str,
    ) -> CheckoutResult:
        if not plan_price.stripe_price_id:
            raise ValueError(
                f'PlanPrice {plan_price.uuid} ({plan_price.currency}) '
                f'has no stripe_price_id configured.'
            )

        customer_id = await self.get_or_create_customer(user)

        metadata = {
            'mula_user_uuid': str(user.uuid),
            'mula_org_uuid': str(organization.uuid),
            'mula_plan_slug': plan.slug,
            'mula_plan_price_uuid': str(plan_price.uuid),
            'mula_currency': plan_price.currency,
            'mula_paid_by_uuid': str(paid_by.uuid) if paid_by else str(user.uuid),
        }

        session = await sync_to_async(stripe.checkout.Session.create)(
            mode='subscription',
            customer=customer_id,
            line_items=[{
                'price': plan_price.stripe_price_id,
                'quantity': 1,
            }],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=metadata,
            subscription_data={
                'metadata': metadata,
            },
        )

        logger.info(
            'Created Stripe Checkout Session %s for user=%s, org=%s, plan=%s',
            session.id, user.uuid, organization.uuid, plan.slug,
        )

        return CheckoutResult(
            checkout_url=session.url,
            provider_session_id=session.id,
        )


    async def create_portal_session(
        self,
        *,
        user,
        return_url: str,
    ) -> PortalResult:
        customer_id = await self.get_or_create_customer(user)

        session = await sync_to_async(
            stripe.billing_portal.Session.create
        )(
            customer=customer_id,
            return_url=return_url,
        )

        return PortalResult(portal_url=session.url)


    async def cancel_subscription(
        self,
        *,
        provider_subscription_id: str,
    ) -> CancelResult:
        if not provider_subscription_id:
            return CancelResult(
                canceled=False,
                provider_subscription_id='',
            )

        sub = await sync_to_async(stripe.Subscription.modify)(
            provider_subscription_id,
            cancel_at_period_end=True,
        )

        period_end = _safe_get_period_end(sub)

        logger.info(
            'Canceled Stripe subscription %s at period end (ends %s)',
            sub.id, period_end,
        )

        return CancelResult(
            canceled=True,
            effective_end=str(period_end) if period_end else '',
            provider_subscription_id=sub.id,
        )


    async def handle_webhook(self, *, payload: bytes, signature: str) -> dict:
        from billing.providers.stripe.webhooks import process_stripe_event

        webhook_secret = getattr(settings, 'STRIPE_WEBHOOK_SECRET', '')

        try:
            event = stripe.Webhook.construct_event(
                payload, signature, webhook_secret,
            )
        except stripe.error.SignatureVerificationError:
            logger.warning('Stripe webhook signature verification failed.')
            raise
        except ValueError:
            logger.warning('Stripe webhook payload invalid.')
            raise

        return await process_stripe_event(event)



_provider_instance: Optional[StripeProvider] = None


def get_provider() -> StripeProvider:
    global _provider_instance
    if _provider_instance is None:
        _provider_instance = StripeProvider()
    return _provider_instance
