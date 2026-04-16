# File location: /server/billing/providers/stripe/migrations/0001_initial.py

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("billing", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="StripeCustomer",
            fields=[
                (
                    "uuid",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("is_deleted", models.BooleanField(default=False)),
                ("is_hidden", models.BooleanField(default=False)),
                (
                    "stripe_customer_id",
                    models.CharField(
                        db_index=True,
                        help_text="Stripe Customer ID (cus_...)",
                        max_length=255,
                        unique=True,
                    ),
                ),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="stripe_customer",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Stripe Customer",
                "verbose_name_plural": "Stripe Customers",
            },
        ),
        migrations.CreateModel(
            name="StripeSubscriptionLink",
            fields=[
                (
                    "uuid",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("is_deleted", models.BooleanField(default=False)),
                ("is_hidden", models.BooleanField(default=False)),
                (
                    "stripe_subscription_id",
                    models.CharField(
                        db_index=True,
                        help_text="Stripe Subscription ID (sub_...)",
                        max_length=255,
                        unique=True,
                    ),
                ),
                (
                    "stripe_price_id",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Stripe Price ID used for this subscription (price_...)",
                        max_length=255,
                    ),
                ),
                (
                    "subscription",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="stripe_link",
                        to="billing.subscription",
                    ),
                ),
            ],
            options={
                "verbose_name": "Stripe Subscription Link",
                "verbose_name_plural": "Stripe Subscription Links",
            },
        ),
        migrations.CreateModel(
            name="StripeWebhookEvent",
            fields=[
                (
                    "stripe_event_id",
                    models.CharField(
                        help_text="Stripe Event ID (evt_...)",
                        max_length=255,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "event_type",
                    models.CharField(
                        db_index=True,
                        help_text="e.g. checkout.session.completed",
                        max_length=100,
                    ),
                ),
                ("processed", models.BooleanField(db_index=True, default=False)),
                (
                    "payload",
                    models.JSONField(
                        default=dict, help_text="Raw Stripe event JSON for audit"
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "verbose_name": "Stripe Webhook Event",
                "verbose_name_plural": "Stripe Webhook Events",
                "indexes": [
                    models.Index(
                        fields=["event_type", "-created_at"],
                        name="billing_str_event_t_397576_idx",
                    ),
                    models.Index(
                        fields=["processed", "-created_at"],
                        name="billing_str_process_5ba58b_idx",
                    ),
                ],
            },
        ),
    ]
