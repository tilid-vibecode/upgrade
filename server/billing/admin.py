# File location: /server/billing/admin.py
from django.contrib import admin

from .models import (
    FreeOrgAllowance,
    PaymentRecord,
    Plan,
    PlanPrice,
    PromoCode,
    PromoRedemption,
    Subscription,
    UsagePeriod,
)



class PlanPriceInline(admin.TabularInline):

    model = PlanPrice
    extra = 1
    fields = ('currency', 'price_cents', 'stripe_price_id', 'is_active')
    readonly_fields = ()


class UsagePeriodInline(admin.TabularInline):

    model = UsagePeriod
    extra = 0
    readonly_fields = (
        'period_start', 'period_end',
        'feature_chats_created', 'feature_chats_limit',
        'created_at',
    )
    can_delete = False


class PromoRedemptionInline(admin.TabularInline):

    model = PromoRedemption
    extra = 0
    readonly_fields = (
        'user', 'organization',
        'chats_granted', 'chats_used',
        'created_at',
    )
    can_delete = False



@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):

    list_display = (
        'name', 'slug', 'billing_interval',
        'monthly_feature_chats', 'max_free_members_per_discussion',
        'is_active', 'sort_order',
    )
    list_filter = ('is_active', 'billing_interval')
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}
    inlines = [PlanPriceInline]
    ordering = ('sort_order', 'name')


@admin.register(PlanPrice)
class PlanPriceAdmin(admin.ModelAdmin):

    list_display = ('plan', 'currency', 'price_cents', 'stripe_price_id', 'is_active')
    list_filter = ('currency', 'is_active')
    search_fields = ('plan__name', 'plan__slug', 'stripe_price_id')



@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):

    list_display = (
        'user', 'organization', 'plan',
        'status', 'currency',
        'current_period_start', 'current_period_end',
        'cancel_at_period_end',
    )
    list_filter = ('status', 'currency', 'cancel_at_period_end')
    search_fields = (
        'user__email', 'organization__name',
        'provider_subscription_id',
    )
    raw_id_fields = ('user', 'organization', 'plan', 'plan_price', 'paid_by')
    readonly_fields = ('provider_subscription_id', 'provider_customer_id')
    inlines = [UsagePeriodInline]


@admin.register(UsagePeriod)
class UsagePeriodAdmin(admin.ModelAdmin):

    list_display = (
        'subscription', 'period_start', 'period_end',
        'feature_chats_created', 'feature_chats_limit',
    )
    list_filter = ('period_start',)
    raw_id_fields = ('subscription',)



@admin.register(PromoCode)
class PromoCodeAdmin(admin.ModelAdmin):

    list_display = (
        'code', 'granted_feature_chats',
        'redemption_count', 'max_redemptions',
        'is_active', 'expires_at', 'created_by',
    )
    list_filter = ('is_active', 'expires_at')
    search_fields = ('code', 'description')
    readonly_fields = ('redemption_count', 'created_by')
    inlines = [PromoRedemptionInline]

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(PromoRedemption)
class PromoRedemptionAdmin(admin.ModelAdmin):

    list_display = (
        'promo_code', 'user', 'organization',
        'chats_granted', 'chats_used', 'created_at',
    )
    list_filter = ('created_at',)
    search_fields = ('user__email', 'promo_code__code', 'organization__name')
    raw_id_fields = ('user', 'organization', 'promo_code')



@admin.register(FreeOrgAllowance)
class FreeOrgAllowanceAdmin(admin.ModelAdmin):

    list_display = (
        'user', 'organization',
        'chats_allowed', 'chats_used', 'created_at',
    )
    search_fields = ('user__email', 'organization__name')
    raw_id_fields = ('user', 'organization')



@admin.register(PaymentRecord)
class PaymentRecordAdmin(admin.ModelAdmin):

    list_display = (
        'user', 'organization',
        'amount_cents', 'currency', 'status',
        'provider_payment_id', 'created_at',
    )
    list_filter = ('status', 'currency', 'created_at')
    search_fields = (
        'user__email', 'organization__name',
        'provider_payment_id',
    )
    raw_id_fields = ('user', 'organization', 'subscription')
    readonly_fields = (
        'user', 'organization', 'subscription',
        'amount_cents', 'currency', 'status',
        'description', 'provider_payment_id', 'provider_data',
        'created_at',
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
