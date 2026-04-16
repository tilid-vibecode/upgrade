# File location: /server/billing/providers/stripe/apps.py
from django.apps import AppConfig


class BillingStripeConfig(AppConfig):

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'billing.providers.stripe'
    label = 'billing_stripe'
    verbose_name = 'Billing — Stripe Provider'
