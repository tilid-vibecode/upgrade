from django.contrib import admin

from .models import AssessmentCycle


@admin.register(AssessmentCycle)
class AssessmentCycleAdmin(admin.ModelAdmin):
    list_display = ('title', 'workspace', 'status', 'updated_at')
    list_filter = ('status', 'uses_self_report', 'uses_performance_reviews')
    search_fields = ('title', 'workspace__name', 'workspace__slug')
    raw_id_fields = ('workspace',)
