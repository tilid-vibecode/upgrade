# File location: /server/billing/management/commands/seed_plans.py
import logging

from django.core.management.base import BaseCommand

from billing.models import BillingInterval, Currency, Plan, PlanPrice

logger = logging.getLogger(__name__)

PLANS = [
    {
        'name': 'Pro',
        'slug': 'pro-monthly',
        'monthly_feature_chats': 5,
        'max_free_members_per_discussion': 5,
        'billing_interval': BillingInterval.MONTHLY,
        'sort_order': 10,
        'metadata': {},
        'prices': [
            {'currency': Currency.USD, 'price_cents': 29900},    # $299.00
            {'currency': Currency.EUR, 'price_cents': 27900},    # €279.00
            {'currency': Currency.GBP, 'price_cents': 23900},    # £239.00
        ],
    },
]


class Command(BaseCommand):

    help = 'Create or update initial billing plans and per-currency prices.'

    def handle(self, *args, **options):
        for plan_data in PLANS:
            prices_data = plan_data.pop('prices')

            plan, created = Plan.objects.update_or_create(
                slug=plan_data['slug'],
                defaults=plan_data,
            )

            action = 'Created' if created else 'Updated'
            self.stdout.write(f'{action} plan: {plan.name} ({plan.slug})')

            for price_data in prices_data:
                plan_price, p_created = PlanPrice.objects.update_or_create(
                    plan=plan,
                    currency=price_data['currency'],
                    defaults={
                        'price_cents': price_data['price_cents'],
                        'is_active': True,
                    },
                )
                p_action = 'Created' if p_created else 'Updated'
                display = price_data['price_cents'] / 100
                self.stdout.write(
                    f'  {p_action} price: {price_data["currency"].upper()} '
                    f'{display:.2f}'
                )

            plan_data['prices'] = prices_data

        self.stdout.write(self.style.SUCCESS('Done — billing plans seeded.'))
