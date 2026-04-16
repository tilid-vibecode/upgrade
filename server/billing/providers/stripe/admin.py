# File location: /server/billing/providers/stripe/admin.py
from django.contrib import admin

from .models import StripeCustomer, StripeSubscriptionLink, StripeWebhookEvent


@admin.register(StripeCustomer)
class StripeCustomerAdmin(admin.ModelAdmin):

    list_display = ('user', 'stripe_customer_id', 'created_at')
    search_fields = ('user__email', 'stripe_customer_id')
    raw_id_fields = ('user',)
    readonly_fields = ('stripe_customer_id', 'created_at')


@admin.register(StripeSubscriptionLink)
class StripeSubscriptionLinkAdmin(admin.ModelAdmin):

    list_display = (
        'subscription', 'stripe_subscription_id',
        'stripe_price_id', 'created_at',
    )
    search_fields = ('stripe_subscription_id', 'stripe_price_id')
    raw_id_fields = ('subscription',)
    readonly_fields = (
        'stripe_subscription_id', 'stripe_price_id', 'created_at',
    )


@admin.register(StripeWebhookEvent)
class StripeWebhookEventAdmin(admin.ModelAdmin):

    list_display = (
        'stripe_event_id', 'event_type',
        'processed', 'created_at', 'processed_at',
    )
    list_filter = ('processed', 'event_type')
    search_fields = ('stripe_event_id',)
    readonly_fields = (
        'stripe_event_id', 'event_type',
        'processed', 'payload',
        'created_at', 'processed_at',
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
