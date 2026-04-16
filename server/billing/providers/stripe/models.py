# File location: /server/billing/providers/stripe/models.py
from django.db import models

from basics.models import TimeStampVisibleModel


class StripeCustomer(TimeStampVisibleModel):

    user = models.OneToOneField(
        'authentication.User',
        on_delete=models.CASCADE,
        related_name='stripe_customer',
    )
    stripe_customer_id = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text='Stripe Customer ID (cus_...)',
    )

    class Meta:
        verbose_name = 'Stripe Customer'
        verbose_name_plural = 'Stripe Customers'

    def __str__(self) -> str:
        return f'{self.user.email} → {self.stripe_customer_id}'


class StripeSubscriptionLink(TimeStampVisibleModel):

    subscription = models.OneToOneField(
        'billing.Subscription',
        on_delete=models.CASCADE,
        related_name='stripe_link',
    )
    stripe_subscription_id = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text='Stripe Subscription ID (sub_...)',
    )
    stripe_price_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text='Stripe Price ID used for this subscription (price_...)',
    )

    class Meta:
        verbose_name = 'Stripe Subscription Link'
        verbose_name_plural = 'Stripe Subscription Links'

    def __str__(self) -> str:
        return f'Sub {self.subscription_id} → {self.stripe_subscription_id}'


class StripeWebhookEvent(models.Model):

    stripe_event_id = models.CharField(
        max_length=255,
        primary_key=True,
        help_text='Stripe Event ID (evt_...)',
    )
    event_type = models.CharField(
        max_length=100,
        db_index=True,
        help_text='e.g. checkout.session.completed',
    )
    processed = models.BooleanField(
        default=False,
        db_index=True,
    )
    payload = models.JSONField(
        default=dict,
        help_text='Raw Stripe event JSON for audit',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Stripe Webhook Event'
        verbose_name_plural = 'Stripe Webhook Events'
        indexes = [
            models.Index(fields=['event_type', '-created_at']),
            models.Index(fields=['processed', '-created_at']),
        ]

    def __str__(self) -> str:
        status = 'processed' if self.processed else 'pending'
        return f'{self.stripe_event_id} ({self.event_type}) [{status}]'
