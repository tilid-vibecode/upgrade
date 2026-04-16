from django.contrib import admin

from .models import DevelopmentPlanRun


@admin.register(DevelopmentPlanRun)
class DevelopmentPlanRunAdmin(admin.ModelAdmin):
    list_display = ('title', 'workspace', 'scope', 'status', 'updated_at')
    list_filter = ('status', 'scope')
    search_fields = ('title', 'workspace__name', 'workspace__slug')
    raw_id_fields = ('workspace',)
