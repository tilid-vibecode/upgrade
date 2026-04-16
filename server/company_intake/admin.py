from django.contrib import admin

from .models import IntakeWorkspace, SourceDocument, WorkspaceSource


@admin.register(IntakeWorkspace)
class IntakeWorkspaceAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'status', 'updated_at')
    search_fields = ('name', 'slug')
    list_filter = ('status',)
    readonly_fields = ('uuid', 'created_at', 'updated_at')


@admin.register(SourceDocument)
class SourceDocumentAdmin(admin.ModelAdmin):
    list_display = (
        'original_filename',
        'workspace',
        'document_kind',
        'status',
        'file_size',
        'created_at',
    )
    search_fields = ('original_filename', 'workspace__name', 'workspace__slug')
    list_filter = ('document_kind', 'status')
    raw_id_fields = ('workspace',)
    readonly_fields = ('uuid', 'created_at', 'updated_at')


@admin.register(WorkspaceSource)
class WorkspaceSourceAdmin(admin.ModelAdmin):
    list_display = (
        'title',
        'workspace',
        'source_kind',
        'transport',
        'status',
        'updated_at',
    )
    search_fields = (
        'title',
        'workspace__name',
        'workspace__slug',
        'media_file__original_filename',
        'external_url',
    )
    list_filter = ('source_kind', 'transport', 'status')
    raw_id_fields = ('workspace', 'media_file')
    readonly_fields = ('uuid', 'created_at', 'updated_at')
