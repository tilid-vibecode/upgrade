from django.contrib import admin

from .models import EvidenceMatrixRun


@admin.register(EvidenceMatrixRun)
class EvidenceMatrixRunAdmin(admin.ModelAdmin):
    list_display = ('title', 'workspace', 'source_type', 'status', 'updated_at')
    list_filter = ('status', 'source_type')
    search_fields = ('title', 'workspace__name', 'workspace__slug', 'connection_label')
    raw_id_fields = ('workspace',)
