from django.contrib import admin
from django.utils.html import format_html

from .models import ScheduledTask, TaskExecution


class RecentExecutionsInline(admin.TabularInline):
    model = TaskExecution
    extra = 0
    max_num = 10
    readonly_fields = [
        'status', 'dispatched_at', 'error', 'dramatiq_message_id',
    ]
    ordering = ['-dispatched_at']
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ScheduledTask)
class ScheduledTaskAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'schedule_type', 'schedule_display',
        'is_active', 'auto_paused', 'consecutive_failures',
        'is_system', 'next_run_at', 'last_run_at',
        'queue',
    ]
    list_filter = ['is_active', 'auto_paused', 'is_system', 'schedule_type', 'queue']
    search_fields = ['name', 'task_path', 'description']
    readonly_fields = [
        'uuid', 'next_run_at', 'last_run_at', 'created_at', 'updated_at',
        'consecutive_failures', 'auto_paused',
    ]
    list_editable = ['is_active']
    ordering = ['name']

    fieldsets = [
        (None, {
            'fields': ('uuid', 'name', 'description', 'is_active', 'is_system'),
        }),
        ('Task', {
            'fields': ('task_path', 'task_kwargs', 'queue'),
        }),
        ('Schedule', {
            'fields': (
                'schedule_type', 'cron_expression',
                'interval_seconds', 'run_at', 'user_timezone',
            ),
        }),
        ('Execution State', {
            'fields': (
                'next_run_at', 'last_run_at', 'max_instances', 'misfire_grace_seconds',
                'consecutive_failures', 'auto_paused',
            ),
        }),
        ('Ownership', {
            'fields': ('organization_uuid', 'created_by_uuid'),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    ]

    inlines = [RecentExecutionsInline]

    @admin.display(description='Schedule')
    def schedule_display(self, obj: ScheduledTask) -> str:
        if obj.schedule_type == 'cron':
            return format_html('<code>{expr}</code>', expr=obj.cron_expression)
        if obj.schedule_type == 'interval':
            return f'every {obj.interval_seconds}s'
        if obj.schedule_type == 'once':
            return f'once at {obj.run_at}'
        return '—'

    _SYSTEM_READONLY_FIELDS = [
        'name', 'task_path', 'task_kwargs', 'is_system',
        'schedule_type', 'cron_expression', 'interval_seconds',
        'run_at', 'queue', 'user_timezone',
    ]

    def get_readonly_fields(self, request, obj: ScheduledTask = None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj and obj.is_system:
            fields.extend(self._SYSTEM_READONLY_FIELDS)
        return fields

    def has_delete_permission(self, request, obj=None):
        if obj and obj.is_system:
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        actions = super().get_actions(request)
        if 'delete_selected' in actions:
            actions['delete_selected'] = (
                self._safe_delete_selected,
                'delete_selected',
                'Delete selected non-system tasks',
            )
        return actions

    @staticmethod
    def _safe_delete_selected(modeladmin, request, queryset):
        from django.contrib.admin.actions import delete_selected
        safe_qs = queryset.filter(is_system=False)
        skipped = queryset.filter(is_system=True).count()
        if skipped:
            modeladmin.message_user(
                request,
                f'Skipped {skipped} system task(s) — system tasks cannot be deleted.',
                level='warning',
            )
        if safe_qs.exists():
            return delete_selected(modeladmin, request, safe_qs)

    def save_model(self, request, obj: ScheduledTask, form, change):
        if change and obj.is_active and obj.auto_paused:
            obj.auto_paused = False
            obj.consecutive_failures = 0
            if not obj.next_run_at:
                obj.next_run_at = obj.compute_next_run()
        super().save_model(request, obj, form, change)


@admin.register(TaskExecution)
class TaskExecutionAdmin(admin.ModelAdmin):
    list_display = ['task', 'status', 'dispatched_at', 'dramatiq_message_id']
    list_filter = ['status', 'dispatched_at']
    search_fields = ['task__name', 'dramatiq_message_id']
    readonly_fields = [
        'task', 'status', 'dispatched_at', 'error', 'dramatiq_message_id',
    ]
    ordering = ['-dispatched_at']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
