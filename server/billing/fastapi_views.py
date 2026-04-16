# File location: /server/billing/fastapi_views.py
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from server.rate_limit import GLOBAL_RPM
from authentication.models import User
from authentication.permissions import (
    has_org_access,
    is_authenticated,
    require_admin,
)
from organization.models import Organization, OrganizationMembership

from .entities import (
    CheckoutRequest,
    CheckoutResponse,
    CreditsSummaryResponse,
    FreeAllowanceInfo,
    MemberCheckoutRequest,
    PaymentHistoryResponse,
    PaymentRecordResponse,
    PlanListResponse,
    PlanPriceResponse,
    PlanResponse,
    PortalResponse,
    PromoCreditInfo,
    PromoRedeemRequest,
    PromoRedeemResponse,
    SubscriptionCreditInfo,
    SubscriptionResponse,
    UsageResponse,
)
from .managers import BillingManager

logger = logging.getLogger(__name__)



billing_router = APIRouter(
    prefix='/o/{org_uuid}/billing',
    tags=['billing'],
    dependencies=[GLOBAL_RPM],
)

billing_admin_router = APIRouter(
    prefix='/o/{org_uuid}/billing/members',
    tags=['billing-admin'],
    dependencies=[GLOBAL_RPM],
)

webhook_router = APIRouter(
    tags=['webhooks'],
)



@billing_router.get('/plans', response_model=PlanListResponse)
async def list_plans(
    user: User = Depends(is_authenticated),
):
    from asgiref.sync import sync_to_async
    from billing.models import Plan

    plans = await sync_to_async(list)(
        Plan.objects.filter(is_active=True, is_deleted=False)
        .prefetch_related('prices')
        .order_by('sort_order', 'name')
    )

    result = []
    for plan in plans:
        prices = [p for p in plan.prices.all() if p.is_active and not p.is_deleted]
        result.append(PlanResponse(
            uuid=plan.uuid,
            name=plan.name,
            slug=plan.slug,
            monthly_feature_chats=plan.monthly_feature_chats,
            max_free_members_per_discussion=plan.max_free_members_per_discussion,
            billing_interval=plan.billing_interval,
            prices=[
                PlanPriceResponse(
                    uuid=p.uuid,
                    currency=p.currency,
                    price_cents=p.price_cents,
                    is_active=p.is_active,
                )
                for p in prices
            ],
        ))

    return PlanListResponse(plans=result, total=len(result))



@billing_router.get('/subscription')
async def get_subscription(
    org: Organization = Depends(has_org_access),
    user: User = Depends(is_authenticated),
):
    from asgiref.sync import sync_to_async
    from billing.models import Subscription, SubscriptionStatus, UsagePeriod
    from django.utils import timezone

    now = timezone.now()

    sub = await sync_to_async(
        Subscription.objects.filter(
            user=user,
            organization=org,
            status__in=[
                SubscriptionStatus.ACTIVE,
                SubscriptionStatus.CANCELED,
                SubscriptionStatus.PAST_DUE,
            ],
            is_deleted=False,
        )
        .select_related('plan', 'paid_by')
        .first
    )()

    if sub is None:
        return {'has_subscription': False, 'subscription': None, 'usage': None}

    sub_response = SubscriptionResponse(
        uuid=sub.uuid,
        plan_name=sub.plan.name,
        plan_slug=sub.plan.slug,
        status=sub.status,
        currency=sub.currency,
        current_period_start=sub.current_period_start,
        current_period_end=sub.current_period_end,
        cancel_at_period_end=sub.cancel_at_period_end,
        canceled_at=sub.canceled_at,
        paid_by_email=sub.paid_by.email if sub.paid_by else None,
    )

    usage_response = None
    usage = await sync_to_async(
        UsagePeriod.objects.filter(
            subscription=sub,
            period_start__lte=now,
            period_end__gt=now,
            is_deleted=False,
        ).first
    )()

    if usage is not None:
        usage_response = UsageResponse(
            period_start=usage.period_start,
            period_end=usage.period_end,
            feature_chats_created=usage.feature_chats_created,
            feature_chats_limit=usage.feature_chats_limit,
            remaining=max(0, usage.feature_chats_limit - usage.feature_chats_created),
        )

    return {
        'has_subscription': True,
        'subscription': sub_response,
        'usage': usage_response,
    }



@billing_router.get('/status')
async def get_billing_status(
    org: Organization = Depends(has_org_access),
    user: User = Depends(is_authenticated),
):
    from asgiref.sync import sync_to_async
    from billing.models import Subscription, SubscriptionStatus, UsagePeriod
    from django.utils import timezone

    now = timezone.now()

    sub = await sync_to_async(
        Subscription.objects.filter(
            user=user,
            organization=org,
            status__in=[
                SubscriptionStatus.ACTIVE,
                SubscriptionStatus.CANCELED,
                SubscriptionStatus.PAST_DUE,
            ],
            is_deleted=False,
        )
        .select_related('plan', 'paid_by')
        .first
    )()

    sub_response = None
    usage_response = None

    if sub is not None:
        sub_response = SubscriptionResponse(
            uuid=sub.uuid,
            plan_name=sub.plan.name,
            plan_slug=sub.plan.slug,
            status=sub.status,
            currency=sub.currency,
            current_period_start=sub.current_period_start,
            current_period_end=sub.current_period_end,
            cancel_at_period_end=sub.cancel_at_period_end,
            canceled_at=sub.canceled_at,
            paid_by_email=sub.paid_by.email if sub.paid_by else None,
        )

        usage = await sync_to_async(
            UsagePeriod.objects.filter(
                subscription=sub,
                period_start__lte=now,
                period_end__gt=now,
                is_deleted=False,
            ).first
        )()

        if usage is not None:
            usage_response = UsageResponse(
                period_start=usage.period_start,
                period_end=usage.period_end,
                feature_chats_created=usage.feature_chats_created,
                feature_chats_limit=usage.feature_chats_limit,
                remaining=max(0, usage.feature_chats_limit - usage.feature_chats_created),
            )

    credits_summary = await BillingManager.get_credits_summary(user, org)

    can_create, allowance = await BillingManager.can_create_feature_chat(user, org)

    return {
        'has_subscription': sub is not None,
        'subscription': sub_response,
        'usage': usage_response,
        'credits': CreditsSummaryResponse(
            free_allowance=(
                FreeAllowanceInfo(**credits_summary['free_allowance'])
                if credits_summary['free_allowance'] else None
            ),
            promo_credits=[PromoCreditInfo(**p) for p in credits_summary['promo_credits']],
            subscription=(
                SubscriptionCreditInfo(**credits_summary['subscription'])
                if credits_summary['subscription'] else None
            ),
            total_available=credits_summary['total_available'],
        ),
        'can_create_feature_chat': can_create,
        'budget_source': allowance.source if can_create else None,
    }



@billing_router.get('/members-billing')
async def list_members_billing(
    org: Organization = Depends(has_org_access),
    user: User = Depends(is_authenticated),
):
    from asgiref.sync import sync_to_async
    from billing.models import Subscription, SubscriptionStatus

    subs = await sync_to_async(list)(
        Subscription.objects.filter(
            organization=org,
            status__in=[
                SubscriptionStatus.ACTIVE,
                SubscriptionStatus.CANCELED,
                SubscriptionStatus.PAST_DUE,
            ],
            is_deleted=False,
        )
        .select_related('plan', 'user')
    )

    memberships = await sync_to_async(list)(
        OrganizationMembership.objects.filter(
            organization=org,
            is_deleted=False,
        ).select_related('user')
    )

    user_sub_map = {}
    for s in subs:
        user_sub_map[s.user_id] = s

    result = {}
    for m in memberships:
        s = user_sub_map.get(m.user_id)
        if s and s.status != SubscriptionStatus.EXPIRED:
            result[str(m.uuid)] = {
                'has_subscription': True,
                'plan_name': s.plan.name,
                'status': s.status,
            }
        else:
            result[str(m.uuid)] = {
                'has_subscription': False,
            }

    return {'members': result}



@billing_router.post('/checkout', response_model=CheckoutResponse)
async def create_checkout(
    request: Request,
    body: CheckoutRequest,
    org_uuid: UUID,
    org: Organization = Depends(has_org_access),
    user: User = Depends(is_authenticated),
):
    from asgiref.sync import sync_to_async
    from billing.models import Plan, PlanPrice
    from billing.providers.stripe.client import get_provider
    from django.conf import settings as django_settings

    plan = await sync_to_async(
        Plan.objects.filter(
            slug=body.plan_slug, is_active=True, is_deleted=False,
        ).first
    )()

    if plan is None:
        raise HTTPException(status_code=404, detail='Plan not found.')

    plan_price = await sync_to_async(
        PlanPrice.objects.filter(
            plan=plan, currency=body.currency, is_active=True, is_deleted=False,
        ).first
    )()

    if plan_price is None:
        raise HTTPException(
            status_code=400,
            detail=f'No pricing available for currency "{body.currency}".',
        )

    frontend_url = getattr(django_settings, 'FRONTEND_URL', 'https://localhost:3000')
    success_url = f'{frontend_url}/o/{org_uuid}/settings/billing?success=1'
    cancel_url = f'{frontend_url}/o/{org_uuid}/settings/billing'

    provider = get_provider()
    result = await provider.create_checkout_session(
        user=user,
        organization=org,
        plan=plan,
        plan_price=plan_price,
        paid_by=user,
        success_url=success_url,
        cancel_url=cancel_url,
    )

    return CheckoutResponse(checkout_url=result.checkout_url)



@billing_router.post('/portal', response_model=PortalResponse)
async def create_portal(
    org_uuid: UUID,
    org: Organization = Depends(has_org_access),
    user: User = Depends(is_authenticated),
):
    from billing.providers.stripe.client import get_provider
    from django.conf import settings as django_settings

    frontend_url = getattr(django_settings, 'FRONTEND_URL', 'https://localhost:3000')
    return_url = f'{frontend_url}/o/{org_uuid}/settings/billing'

    provider = get_provider()
    result = await provider.create_portal_session(user=user, return_url=return_url)

    return PortalResponse(portal_url=result.portal_url)



@billing_router.post('/subscription/cancel')
async def cancel_subscription(
    org: Organization = Depends(has_org_access),
    user: User = Depends(is_authenticated),
):
    from asgiref.sync import sync_to_async
    from billing.models import Subscription, SubscriptionStatus
    from billing.providers.stripe.client import get_provider
    from django.utils import timezone

    sub = await sync_to_async(
        Subscription.objects.filter(
            user=user,
            organization=org,
            status=SubscriptionStatus.ACTIVE,
            is_deleted=False,
        ).first
    )()

    if sub is None:
        raise HTTPException(status_code=404, detail='No active subscription to cancel.')

    if sub.provider_subscription_id:
        provider = get_provider()
        await provider.cancel_subscription(
            provider_subscription_id=sub.provider_subscription_id,
        )

    sub.cancel_at_period_end = True
    sub.canceled_at = timezone.now()
    sub.status = SubscriptionStatus.CANCELED
    await sync_to_async(sub.save)(
        update_fields=['cancel_at_period_end', 'canceled_at', 'status', 'updated_at'],
    )

    return {
        'status': 'canceled',
        'effective_end': sub.current_period_end.isoformat(),
    }



@billing_router.get('/usage')
async def get_usage(
    org: Organization = Depends(has_org_access),
    user: User = Depends(is_authenticated),
):
    from asgiref.sync import sync_to_async
    from billing.models import Subscription, SubscriptionStatus, UsagePeriod
    from django.utils import timezone

    now = timezone.now()

    sub = await sync_to_async(
        Subscription.objects.filter(
            user=user,
            organization=org,
            status__in=[SubscriptionStatus.ACTIVE, SubscriptionStatus.CANCELED],
            current_period_end__gt=now,
            is_deleted=False,
        ).first
    )()

    if sub is None:
        return {'has_subscription': False, 'usage': None}

    usage = await sync_to_async(
        UsagePeriod.objects.filter(
            subscription=sub,
            period_start__lte=now,
            period_end__gt=now,
            is_deleted=False,
        ).first
    )()

    if usage is None:
        return {'has_subscription': True, 'usage': None}

    return {
        'has_subscription': True,
        'usage': UsageResponse(
            period_start=usage.period_start,
            period_end=usage.period_end,
            feature_chats_created=usage.feature_chats_created,
            feature_chats_limit=usage.feature_chats_limit,
            remaining=max(0, usage.feature_chats_limit - usage.feature_chats_created),
        ),
    }



@billing_router.post('/promo/redeem', response_model=PromoRedeemResponse)
async def redeem_promo(
    body: PromoRedeemRequest,
    org: Organization = Depends(has_org_access),
    user: User = Depends(is_authenticated),
):
    success, message, chats_granted = await BillingManager.redeem_promo_code(
        user, org, body.code,
    )

    if not success:
        raise HTTPException(status_code=400, detail=message)

    return PromoRedeemResponse(
        success=True,
        message=message,
        chats_granted=chats_granted,
    )



@billing_router.get('/credits', response_model=CreditsSummaryResponse)
async def get_credits(
    org: Organization = Depends(has_org_access),
    user: User = Depends(is_authenticated),
):
    summary = await BillingManager.get_credits_summary(user, org)

    return CreditsSummaryResponse(
        free_allowance=(
            FreeAllowanceInfo(**summary['free_allowance'])
            if summary['free_allowance'] else None
        ),
        promo_credits=[PromoCreditInfo(**p) for p in summary['promo_credits']],
        subscription=(
            SubscriptionCreditInfo(**summary['subscription'])
            if summary['subscription'] else None
        ),
        total_available=summary['total_available'],
    )



@billing_router.get('/payments', response_model=PaymentHistoryResponse)
async def list_payments(
    org: Organization = Depends(has_org_access),
    user: User = Depends(is_authenticated),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    from asgiref.sync import sync_to_async
    from billing.models import PaymentRecord

    qs = PaymentRecord.objects.filter(
        user=user,
        organization=org,
        is_deleted=False,
    ).order_by('-created_at')

    total = await sync_to_async(qs.count)()
    records = await sync_to_async(list)(qs[offset:offset + limit])

    return PaymentHistoryResponse(
        payments=[
            PaymentRecordResponse(
                uuid=r.uuid,
                amount_cents=r.amount_cents,
                currency=r.currency,
                status=r.status,
                description=r.description,
                created_at=r.created_at,
            )
            for r in records
        ],
        total=total,
    )



@billing_admin_router.post('/{member_uuid}/checkout', response_model=CheckoutResponse)
async def admin_member_checkout(
    member_uuid: UUID,
    body: MemberCheckoutRequest,
    org_uuid: UUID,
    membership: OrganizationMembership = Depends(require_admin),
    admin_user: User = Depends(is_authenticated),
):
    from asgiref.sync import sync_to_async
    from billing.models import Plan, PlanPrice
    from billing.providers.stripe.client import get_provider
    from django.conf import settings as django_settings

    target_membership = await sync_to_async(
        OrganizationMembership.objects.filter(
            uuid=member_uuid,
            organization=membership.organization,
            is_deleted=False,
        )
        .select_related('user')
        .first
    )()

    if target_membership is None:
        raise HTTPException(status_code=404, detail='Member not found.')

    plan = await sync_to_async(
        Plan.objects.filter(
            slug=body.plan_slug, is_active=True, is_deleted=False,
        ).first
    )()

    if plan is None:
        raise HTTPException(status_code=404, detail='Plan not found.')

    plan_price = await sync_to_async(
        PlanPrice.objects.filter(
            plan=plan, currency=body.currency, is_active=True, is_deleted=False,
        ).first
    )()

    if plan_price is None:
        raise HTTPException(
            status_code=400,
            detail=f'No pricing available for currency "{body.currency}".',
        )

    frontend_url = getattr(django_settings, 'FRONTEND_URL', 'https://localhost:3000')
    success_url = f'{frontend_url}/o/{org_uuid}/settings/members?upgraded={member_uuid}'
    cancel_url = f'{frontend_url}/o/{org_uuid}/settings/members'

    provider = get_provider()
    result = await provider.create_checkout_session(
        user=target_membership.user,
        organization=membership.organization,
        plan=plan,
        plan_price=plan_price,
        paid_by=admin_user,
        success_url=success_url,
        cancel_url=cancel_url,
    )

    return CheckoutResponse(checkout_url=result.checkout_url)


@billing_admin_router.post('/{member_uuid}/cancel')
async def admin_cancel_member(
    member_uuid: UUID,
    membership: OrganizationMembership = Depends(require_admin),
    admin_user: User = Depends(is_authenticated),
):
    from asgiref.sync import sync_to_async
    from billing.models import Subscription, SubscriptionStatus
    from billing.providers.stripe.client import get_provider
    from django.utils import timezone

    target_membership = await sync_to_async(
        OrganizationMembership.objects.filter(
            uuid=member_uuid,
            organization=membership.organization,
            is_deleted=False,
        )
        .select_related('user')
        .first
    )()

    if target_membership is None:
        raise HTTPException(status_code=404, detail='Member not found.')

    sub = await sync_to_async(
        Subscription.objects.filter(
            user=target_membership.user,
            organization=membership.organization,
            status=SubscriptionStatus.ACTIVE,
            is_deleted=False,
        ).first
    )()

    if sub is None:
        raise HTTPException(
            status_code=404,
            detail='This member does not have an active subscription.',
        )

    if sub.provider_subscription_id:
        provider = get_provider()
        await provider.cancel_subscription(
            provider_subscription_id=sub.provider_subscription_id,
        )

    sub.cancel_at_period_end = True
    sub.canceled_at = timezone.now()
    sub.status = SubscriptionStatus.CANCELED
    await sync_to_async(sub.save)(
        update_fields=['cancel_at_period_end', 'canceled_at', 'status', 'updated_at'],
    )

    return {
        'status': 'canceled',
        'member_email': target_membership.user.email,
        'effective_end': sub.current_period_end.isoformat(),
    }


@billing_admin_router.get('/{member_uuid}/subscription')
async def admin_get_member_subscription(
    member_uuid: UUID,
    membership: OrganizationMembership = Depends(require_admin),
):
    from asgiref.sync import sync_to_async
    from billing.models import Subscription, SubscriptionStatus

    target_membership = await sync_to_async(
        OrganizationMembership.objects.filter(
            uuid=member_uuid,
            organization=membership.organization,
            is_deleted=False,
        )
        .select_related('user')
        .first
    )()

    if target_membership is None:
        raise HTTPException(status_code=404, detail='Member not found.')

    sub = await sync_to_async(
        Subscription.objects.filter(
            user=target_membership.user,
            organization=membership.organization,
            status__in=[
                SubscriptionStatus.ACTIVE,
                SubscriptionStatus.CANCELED,
                SubscriptionStatus.PAST_DUE,
            ],
            is_deleted=False,
        )
        .select_related('plan', 'paid_by')
        .first
    )()

    if sub is None:
        return {
            'has_subscription': False,
            'member_email': target_membership.user.email,
            'subscription': None,
        }

    return {
        'has_subscription': True,
        'member_email': target_membership.user.email,
        'subscription': SubscriptionResponse(
            uuid=sub.uuid,
            plan_name=sub.plan.name,
            plan_slug=sub.plan.slug,
            status=sub.status,
            currency=sub.currency,
            current_period_start=sub.current_period_start,
            current_period_end=sub.current_period_end,
            cancel_at_period_end=sub.cancel_at_period_end,
            canceled_at=sub.canceled_at,
            paid_by_email=sub.paid_by.email if sub.paid_by else None,
        ),
    }



@webhook_router.post('/webhooks/stripe')
async def stripe_webhook(request: Request):
    from billing.providers.stripe.client import get_provider

    payload = await request.body()
    signature = request.headers.get('stripe-signature', '')

    if not signature:
        raise HTTPException(status_code=400, detail='Missing Stripe signature.')

    provider = get_provider()

    try:
        result = await provider.handle_webhook(payload=payload, signature=signature)
    except Exception as exc:
        logger.warning('Webhook processing failed: %s', exc)
        raise HTTPException(status_code=400, detail='Webhook verification failed.')

    return result
