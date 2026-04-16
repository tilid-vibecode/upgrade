import json
from datetime import datetime, timedelta, timezone

from django.contrib import admin
from django.db.models import Count, Sum, Q
from django.http import JsonResponse
from django.template.response import TemplateResponse
from django.urls import path

from .models import (
    LLMRawLog,
    HourlyUsageAggregate,
    DailyUsageAggregate,
    UserUsageSummary,
    DiscussionUsageSummary,
)


class ReadOnlyMixin:
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class CostDisplayMixin:
    def display_cost(self, obj):
        dollars = obj.estimated_cost_micro / 1_000_000
        if dollars < 0.01:
            return f'${dollars:.6f}'
        return f'${dollars:.4f}'
    display_cost.short_description = 'Est. Cost'
    display_cost.admin_order_field = 'estimated_cost_micro'


@admin.register(LLMRawLog)
class LLMRawLogAdmin(ReadOnlyMixin, CostDisplayMixin, admin.ModelAdmin):
    list_display = [
        'called_at',
        'organization_uuid',
        'user_uuid',
        'provider',
        'model',
        'call_type',
        'total_tokens',
        'display_cost',
        'is_successful',
        'display_tools',
        'caller_function',
    ]
    list_filter = [
        'provider',
        'model',
        'call_type',
        'is_successful',
        'is_org_member',
        ('called_at', admin.DateFieldListFilter),
    ]
    search_fields = [
        'organization_uuid',
        'user_uuid',
        'discussion_uuid',
        'provider_request_id',
        'caller_function',
    ]
    date_hierarchy = 'called_at'
    list_per_page = 50
    ordering = ['-called_at']

    fieldsets = (
        ('Context', {
            'fields': (
                'uuid', 'organization_uuid', 'user_uuid',
                'discussion_uuid', 'is_org_member',
            ),
        }),
        ('LLM Call', {
            'fields': (
                'provider', 'model', 'call_type',
                'caller_function', 'tool_names',
                'iteration', 'attempt',
            ),
        }),
        ('Tokens & Cost', {
            'fields': (
                'prompt_tokens', 'completion_tokens', 'total_tokens',
                'estimated_cost_micro',
            ),
        }),
        ('Outcome', {
            'fields': ('is_successful', 'error_type', 'provider_request_id'),
        }),
        ('Timestamps', {
            'fields': ('called_at',),
        }),
    )

    def display_tools(self, obj):
        if not obj.tool_names:
            return '-'
        return ', '.join(obj.tool_names)
    display_tools.short_description = 'Tools Used'


@admin.register(HourlyUsageAggregate)
class HourlyUsageAggregateAdmin(ReadOnlyMixin, CostDisplayMixin, admin.ModelAdmin):
    list_display = [
        'hour',
        'organization_uuid',
        'provider',
        'model',
        'total_calls',
        'successful_calls',
        'failed_calls',
        'tool_call_turns',
        'total_tokens',
        'display_cost',
        'unique_users',
        'unique_discussions',
    ]
    list_filter = ['provider', 'model', ('hour', admin.DateFieldListFilter)]
    search_fields = ['organization_uuid']
    date_hierarchy = 'hour'
    list_per_page = 50


@admin.register(DailyUsageAggregate)
class DailyUsageAggregateAdmin(ReadOnlyMixin, CostDisplayMixin, admin.ModelAdmin):
    list_display = [
        'day',
        'organization_uuid',
        'provider',
        'model',
        'total_calls',
        'successful_calls',
        'tool_call_turns',
        'total_tokens',
        'display_cost',
        'unique_users',
    ]
    list_filter = ['provider', 'model']
    search_fields = ['organization_uuid']
    date_hierarchy = 'day'
    list_per_page = 50


@admin.register(UserUsageSummary)
class UserUsageSummaryAdmin(ReadOnlyMixin, CostDisplayMixin, admin.ModelAdmin):
    list_display = [
        'user_uuid',
        'organization_uuid',
        'provider',
        'model',
        'total_calls',
        'total_tokens',
        'display_cost',
        'last_call_at',
    ]
    list_filter = ['provider', 'model']
    search_fields = ['user_uuid', 'organization_uuid']
    list_per_page = 50


@admin.register(DiscussionUsageSummary)
class DiscussionUsageSummaryAdmin(ReadOnlyMixin, CostDisplayMixin, admin.ModelAdmin):
    list_display = [
        'discussion_uuid',
        'organization_uuid',
        'provider',
        'model',
        'total_calls',
        'total_tokens',
        'tool_call_turns',
        'display_cost',
    ]
    list_filter = ['provider', 'model']
    search_fields = ['discussion_uuid', 'organization_uuid']
    list_per_page = 50


class LLMUsageAdminSite(admin.AdminSite):
    pass


def register_dashboard_urls(site: admin.AdminSite) -> None:
    original_get_urls = site.get_urls

    def get_urls():
        custom = [
            path(
                'llm_usage/dashboard/',
                site.admin_view(dashboard_view),
                name='llm_usage_dashboard',
            ),
            path(
                'llm_usage/dashboard/data/',
                site.admin_view(dashboard_data_api),
                name='llm_usage_dashboard_data',
            ),
        ]
        return custom + original_get_urls()

    site.get_urls = get_urls


def dashboard_view(request):
    org_uuids = (
        DailyUsageAggregate.objects
        .values_list('organization_uuid', flat=True)
        .distinct()[:100]
    )
    models_used = (
        DailyUsageAggregate.objects
        .values_list('model', flat=True)
        .distinct()
    )
    providers = (
        DailyUsageAggregate.objects
        .values_list('provider', flat=True)
        .distinct()
    )

    context = {
        **admin.site.each_context(request),
        'title': 'LLM Usage Dashboard',
        'org_uuids': list(org_uuids),
        'models_used': sorted(set(models_used)),
        'providers': sorted(set(providers)),
    }
    return TemplateResponse(request, 'llm_usage/dashboard.html', context)


def dashboard_data_api(request):
    org = request.GET.get('org', '')
    model_filter = request.GET.get('model', '')
    provider_filter = request.GET.get('provider', '')
    try:
        days = min(int(request.GET.get('days', 30)), 365)
    except (TypeError, ValueError):
        days = 30
    granularity = request.GET.get('granularity', 'daily')
    if granularity not in ('hourly', 'daily'):
        granularity = 'daily'

    now = datetime.now(timezone.utc)
    date_from = now - timedelta(days=days)

    if granularity == 'hourly':
        qs = HourlyUsageAggregate.objects.filter(hour__gte=date_from)
        if org:
            qs = qs.filter(organization_uuid=org)
        if model_filter:
            qs = qs.filter(model=model_filter)
        if provider_filter:
            qs = qs.filter(provider=provider_filter)

        time_series = list(
            qs.values('hour', 'model')
            .annotate(
                tokens=Sum('total_tokens'),
                cost=Sum('estimated_cost_micro'),
                calls=Sum('total_calls'),
            )
            .order_by('hour')
        )
        for row in time_series:
            row['period'] = row.pop('hour').isoformat()
            row['cost_usd'] = row.pop('cost', 0) / 1_000_000
    else:
        qs = DailyUsageAggregate.objects.filter(day__gte=date_from.date())
        if org:
            qs = qs.filter(organization_uuid=org)
        if model_filter:
            qs = qs.filter(model=model_filter)
        if provider_filter:
            qs = qs.filter(provider=provider_filter)

        time_series = list(
            qs.values('day', 'model')
            .annotate(
                tokens=Sum('total_tokens'),
                cost=Sum('estimated_cost_micro'),
                calls=Sum('total_calls'),
            )
            .order_by('day')
        )
        for row in time_series:
            row['period'] = row.pop('day').isoformat()
            row['cost_usd'] = row.pop('cost', 0) / 1_000_000

    model_qs = DailyUsageAggregate.objects.filter(day__gte=date_from.date())
    if org:
        model_qs = model_qs.filter(organization_uuid=org)
    if provider_filter:
        model_qs = model_qs.filter(provider=provider_filter)

    by_model = list(
        model_qs.values('model')
        .annotate(
            tokens=Sum('total_tokens'),
            cost_usd=Sum('estimated_cost_micro'),
            calls=Sum('total_calls'),
        )
        .order_by('-tokens')
    )
    for row in by_model:
        row['cost_usd'] = row['cost_usd'] / 1_000_000

    org_qs = DailyUsageAggregate.objects.filter(day__gte=date_from.date())
    if model_filter:
        org_qs = org_qs.filter(model=model_filter)
    if provider_filter:
        org_qs = org_qs.filter(provider=provider_filter)

    top_orgs = list(
        org_qs.values('organization_uuid')
        .annotate(
            tokens=Sum('total_tokens'),
            cost_usd=Sum('estimated_cost_micro'),
            calls=Sum('total_calls'),
        )
        .order_by('-cost_usd')[:20]
    )
    for row in top_orgs:
        row['organization_uuid'] = str(row['organization_uuid'])
        row['cost_usd'] = row['cost_usd'] / 1_000_000

    top_users = []
    if org:
        from .models import UserHourlyContribution

        user_qs = UserHourlyContribution.objects.filter(
            organization_uuid=org,
            hour__gte=date_from,
        )
        if model_filter:
            user_qs = user_qs.filter(model=model_filter)
        if provider_filter:
            user_qs = user_qs.filter(provider=provider_filter)

        top_users = list(
            user_qs.values('user_uuid')
            .annotate(
                tokens=Sum('total_tokens'),
                cost_usd=Sum('estimated_cost_micro'),
                calls=Sum('total_calls'),
            )
            .order_by('-tokens')[:20]
        )
        for row in top_users:
            row['user_uuid'] = str(row['user_uuid'])
            row['cost_usd'] = row['cost_usd'] / 1_000_000

    call_type_qs = LLMRawLog.objects.filter(called_at__gte=date_from)
    if org:
        call_type_qs = call_type_qs.filter(organization_uuid=org)
    if model_filter:
        call_type_qs = call_type_qs.filter(model=model_filter)
    if provider_filter:
        call_type_qs = call_type_qs.filter(provider=provider_filter)

    by_call_type = list(
        call_type_qs.values('call_type')
        .annotate(count=Count('id'), tokens=Sum('total_tokens'))
        .order_by('-count')
    )

    totals_qs = DailyUsageAggregate.objects.filter(day__gte=date_from.date())
    if org:
        totals_qs = totals_qs.filter(organization_uuid=org)
    if model_filter:
        totals_qs = totals_qs.filter(model=model_filter)
    if provider_filter:
        totals_qs = totals_qs.filter(provider=provider_filter)

    totals = totals_qs.aggregate(
        total_calls=Sum('total_calls'),
        total_tokens=Sum('total_tokens'),
        total_cost=Sum('estimated_cost_micro'),
        total_success=Sum('successful_calls'),
        total_failed=Sum('failed_calls'),
    )
    totals['total_cost_usd'] = (totals.pop('total_cost') or 0) / 1_000_000

    return JsonResponse({
        'time_series': time_series,
        'by_model': by_model,
        'top_orgs': top_orgs,
        'top_users': top_users,
        'by_call_type': by_call_type,
        'totals': totals,
    })
