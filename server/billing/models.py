# File location: /server/billing/models.py
from typing import Optional

from django.db import models
from django.utils import timezone

from basics.models import TimeStampVisibleModel



class Currency(models.TextChoices):

    USD = 'usd', 'US Dollar'
    EUR = 'eur', 'Euro'
    GBP = 'gbp', 'British Pound'


class BillingInterval(models.TextChoices):

    MONTHLY = 'monthly', 'Monthly'
    YEARLY = 'yearly', 'Yearly'


class SubscriptionStatus(models.TextChoices):

    TRIALING = 'trialing', 'Trialing'
    ACTIVE = 'active', 'Active'
    CANCELED = 'canceled', 'Canceled'       # cancel requested, still in period
    PAST_DUE = 'past_due', 'Past Due'      # payment failed, grace period
    EXPIRED = 'expired', 'Expired'          # period ended after cancel / failure
    UNPAID = 'unpaid', 'Unpaid'             # exhausted retry attempts


class PaymentStatus(models.TextChoices):

    PENDING = 'pending', 'Pending'
    SUCCEEDED = 'succeeded', 'Succeeded'
    FAILED = 'failed', 'Failed'
    REFUNDED = 'refunded', 'Refunded'



class Plan(TimeStampVisibleModel):

    name = models.CharField(
        max_length=100,
        help_text='Display name, e.g. "Pro"',
    )
    slug = models.SlugField(
        max_length=100,
        unique=True,
        help_text='URL-safe identifier, e.g. "pro-monthly"',
    )

    monthly_feature_chats = models.PositiveIntegerField(
        default=5,
        help_text='Max feature chats per billing cycle',
    )
    max_free_members_per_discussion = models.PositiveIntegerField(
        default=5,
        help_text='Max free (non-paid) members allowed per feature discussion',
    )

    billing_interval = models.CharField(
        max_length=16,
        choices=BillingInterval.choices,
        default=BillingInterval.MONTHLY,
    )

    is_active = models.BooleanField(
        default=True,
        help_text='Controls visibility on the plan selector',
    )
    sort_order = models.PositiveIntegerField(
        default=0,
        help_text='Display ordering (lower = first)',
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text='Arbitrary feature flags for future extensibility',
    )

    class Meta:
        ordering = ['sort_order', 'name']
        indexes = [
            models.Index(fields=['is_active', 'sort_order']),
        ]

    def __str__(self) -> str:
        return f'{self.name} ({self.slug})'


class PlanPrice(TimeStampVisibleModel):

    plan = models.ForeignKey(
        Plan,
        on_delete=models.CASCADE,
        related_name='prices',
    )
    currency = models.CharField(
        max_length=8,
        choices=Currency.choices,
        default=Currency.USD,
    )
    price_cents = models.PositiveIntegerField(
        help_text='Price in smallest currency unit (e.g. 29900 = $299.00)',
    )
    stripe_price_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text='Stripe Price object ID for this plan+currency combo',
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ['plan', 'currency']
        indexes = [
            models.Index(fields=['plan', 'is_active']),
        ]

    def __str__(self) -> str:
        display = self.price_cents / 100
        return f'{self.plan.slug} — {self.currency.upper()} {display:.2f}'



class Subscription(TimeStampVisibleModel):

    user = models.ForeignKey(
        'authentication.User',
        on_delete=models.CASCADE,
        related_name='billing_subscriptions',
    )
    organization = models.ForeignKey(
        'organization.Organization',
        on_delete=models.CASCADE,
        related_name='billing_subscriptions',
    )
    plan = models.ForeignKey(
        Plan,
        on_delete=models.PROTECT,
        related_name='subscriptions',
    )
    plan_price = models.ForeignKey(
        PlanPrice,
        on_delete=models.PROTECT,
        related_name='subscriptions',
        help_text='The specific price row used at purchase time',
    )
    status = models.CharField(
        max_length=20,
        choices=SubscriptionStatus.choices,
        default=SubscriptionStatus.ACTIVE,
        db_index=True,
    )
    currency = models.CharField(
        max_length=8,
        choices=Currency.choices,
        default=Currency.USD,
        help_text='Snapshot of currency at purchase time',
    )

    current_period_start = models.DateTimeField()
    current_period_end = models.DateTimeField()

    cancel_at_period_end = models.BooleanField(default=False)
    canceled_at = models.DateTimeField(null=True, blank=True)

    paid_by = models.ForeignKey(
        'authentication.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='paid_subscriptions',
        help_text='User who pays — self or org admin',
    )

    provider_subscription_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        db_index=True,
        help_text='External subscription ID from payment provider',
    )
    provider_customer_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text='External customer ID from payment provider',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'organization'],
                condition=~models.Q(status__in=['expired', 'unpaid']),
                name='unique_active_subscription_per_user_org',
            ),
        ]
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['user', 'status']),
            models.Index(fields=['current_period_end']),
        ]

    def __str__(self) -> str:
        return (
            f'{self.user.email} — {self.plan.slug} '
            f'({self.status}) in {self.organization}'
        )

    @property
    def is_usable(self) -> bool:
        if self.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.CANCELED):
            return self.current_period_end > timezone.now()
        return False



class UsagePeriod(TimeStampVisibleModel):

    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.CASCADE,
        related_name='usage_periods',
    )
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()

    feature_chats_created = models.PositiveIntegerField(default=0)
    feature_chats_limit = models.PositiveIntegerField(
        help_text='Snapshot of plan limit at period start',
    )

    class Meta:
        unique_together = ['subscription', 'period_start']
        indexes = [
            models.Index(fields=['subscription', 'period_start', 'period_end']),
        ]

    def __str__(self) -> str:
        return (
            f'Usage({self.subscription.user.email}, '
            f'{self.period_start:%Y-%m-%d} – {self.period_end:%Y-%m-%d}, '
            f'{self.feature_chats_created}/{self.feature_chats_limit})'
        )

    @property
    def has_remaining(self) -> bool:
        return self.feature_chats_created < self.feature_chats_limit



class PromoCode(TimeStampVisibleModel):

    code = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
        help_text='Unique promo code',
    )
    description = models.TextField(
        blank=True,
        default='',
    )
    granted_feature_chats = models.PositiveIntegerField(
        help_text='Number of free feature chats granted on redemption',
    )

    max_redemptions = models.PositiveIntegerField(
        default=0,
        help_text='Max total redemptions (0 = unlimited)',
    )
    redemption_count = models.PositiveIntegerField(default=0)

    is_active = models.BooleanField(default=True)
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When this code expires (null = never)',
    )

    created_by = models.ForeignKey(
        'authentication.User',
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_promo_codes',
        help_text='Staff user who created this code',
    )

    allowed_emails = models.JSONField(
        default=list,
        blank=True,
        help_text='Restrict to these emails. Empty list = anyone can redeem.',
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['code']),
            models.Index(fields=['is_active', 'expires_at']),
        ]

    def __str__(self) -> str:
        return f'Promo({self.code}, grants={self.granted_feature_chats})'

    @property
    def is_valid(self) -> bool:
        if not self.is_active:
            return False
        if self.expires_at and self.expires_at < timezone.now():
            return False
        if self.max_redemptions > 0 and self.redemption_count >= self.max_redemptions:
            return False
        return True

    @property
    def remaining_redemptions(self) -> Optional[int]:
        if self.max_redemptions == 0:
            return None  # unlimited
        return max(0, self.max_redemptions - self.redemption_count)


class PromoRedemption(TimeStampVisibleModel):

    promo_code = models.ForeignKey(
        PromoCode,
        on_delete=models.CASCADE,
        related_name='redemptions',
    )
    user = models.ForeignKey(
        'authentication.User',
        on_delete=models.CASCADE,
        related_name='promo_redemptions',
    )
    organization = models.ForeignKey(
        'organization.Organization',
        on_delete=models.CASCADE,
        related_name='promo_redemptions',
    )

    chats_granted = models.PositiveIntegerField()
    chats_used = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ['promo_code', 'user', 'organization']
        indexes = [
            models.Index(fields=['user', 'organization']),
        ]

    def __str__(self) -> str:
        return (
            f'PromoRedemption({self.user.email}, {self.promo_code.code}, '
            f'{self.chats_used}/{self.chats_granted})'
        )

    @property
    def remaining(self) -> int:
        return max(0, self.chats_granted - self.chats_used)



class FreeOrgAllowance(TimeStampVisibleModel):

    user = models.ForeignKey(
        'authentication.User',
        on_delete=models.CASCADE,
        related_name='free_org_allowances',
    )
    organization = models.ForeignKey(
        'organization.Organization',
        on_delete=models.CASCADE,
        related_name='free_org_allowances',
    )

    chats_allowed = models.PositiveIntegerField(default=1)
    chats_used = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ['user', 'organization']

    def __str__(self) -> str:
        return (
            f'FreeAllowance({self.user.email}, '
            f'{self.organization}, {self.chats_used}/{self.chats_allowed})'
        )

    @property
    def remaining(self) -> int:
        return max(0, self.chats_allowed - self.chats_used)



class PaymentRecord(TimeStampVisibleModel):

    user = models.ForeignKey(
        'authentication.User',
        on_delete=models.CASCADE,
        related_name='payment_records',
    )
    organization = models.ForeignKey(
        'organization.Organization',
        on_delete=models.CASCADE,
        related_name='payment_records',
    )
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='payment_records',
    )

    amount_cents = models.IntegerField(
        help_text='Amount in smallest currency unit',
    )
    currency = models.CharField(
        max_length=8,
        choices=Currency.choices,
        default=Currency.USD,
    )
    status = models.CharField(
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.PENDING,
        db_index=True,
    )
    description = models.TextField(
        blank=True,
        default='',
    )

    provider_payment_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        db_index=True,
        help_text='Payment ID from provider (e.g. Stripe PaymentIntent)',
    )
    provider_data = models.JSONField(
        default=dict,
        blank=True,
        help_text='Raw provider response for audit trail',
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'organization', '-created_at']),
            models.Index(fields=['provider_payment_id']),
        ]

    def __str__(self) -> str:
        display = self.amount_cents / 100
        return (
            f'Payment({self.user.email}, '
            f'{self.currency.upper()} {display:.2f}, {self.status})'
        )


class UsageRecord(TimeStampVisibleModel):

    user = models.ForeignKey(
        'authentication.User',
        on_delete=models.CASCADE,
        related_name='usage_records',
    )
    organization = models.ForeignKey(
        'organization.Organization',
        on_delete=models.CASCADE,
        related_name='usage_records',
    )
    discussion_uuid = models.UUIDField(
        unique=True,
        help_text='The feature discussion this credit was consumed for.',
    )
    source = models.CharField(
        max_length=32,
        help_text='One of: free_allowance, promo, subscription.',
    )
    promo_redemption_uuid = models.CharField(
        max_length=64,
        blank=True,
        default='',
    )
    subscription_uuid = models.CharField(
        max_length=64,
        blank=True,
        default='',
    )

    class Meta:
        db_table = 'billing_usage_record'

    def __str__(self):
        return f'UsageRecord({self.discussion_uuid}, {self.source})'
