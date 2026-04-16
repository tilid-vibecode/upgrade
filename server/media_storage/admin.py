from django.contrib import admin
from .models import MediaFile, MediaFileVariant


class MediaFileVariantInline(admin.TabularInline):
    model = MediaFileVariant
    extra = 0
    readonly_fields = (
        'uuid',
        'variant_type',
        'content_type',
        'file_size',
        'persistent_key',
        'processing_key',
        'width',
        'height',
        'metadata',
        'created_at',
    )


@admin.register(MediaFile)
class MediaFileAdmin(admin.ModelAdmin):
    list_display = (
        'uuid',
        'original_filename',
        'file_category',
        'status',
        'file_size',
        'has_persistent',
        'has_processing',
        'organization',
        'uploaded_by',
        'created_at',
    )
    list_filter = ('status', 'file_category')
    search_fields = (
        'original_filename',
        'persistent_key',
        'processing_key',
        'organization__name',
    )
    readonly_fields = (
        'uuid',
        'created_at',
        'updated_at',
        'persistent_key',
        'processing_key',
    )
    raw_id_fields = ('organization', 'uploaded_by', 'discussion')
    inlines = [MediaFileVariantInline]

    fieldsets = (
        (
            None,
            {
                'fields': (
                    'uuid',
                    'organization',
                    'uploaded_by',
                    'discussion',
                    'original_filename',
                    'content_type',
                    'file_size',
                    'file_category',
                ),
            },
        ),
        (
            'Storage',
            {
                'fields': (
                    'persistent_key',
                    'processing_key',
                ),
            },
        ),
        (
            'State',
            {
                'fields': (
                    'status',
                    'error_msg',
                ),
            },
        ),
        (
            'Processing',
            {
                'fields': (
                    'processing_description',
                    'processing_metadata',
                ),
            },
        ),
        (
            'Timestamps',
            {
                'fields': ('created_at', 'updated_at'),
            },
        ),
    )

    @admin.display(boolean=True, description='Persistent?')
    def has_persistent(self, obj):
        return obj.persistent_key is not None

    @admin.display(boolean=True, description='Processing?')
    def has_processing(self, obj):
        return obj.processing_key is not None


@admin.register(MediaFileVariant)
class MediaFileVariantAdmin(admin.ModelAdmin):
    list_display = (
        'uuid',
        'source_file',
        'variant_type',
        'content_type',
        'file_size',
        'created_at',
    )
    list_filter = ('variant_type',)
    search_fields = (
        'source_file__original_filename',
        'persistent_key',
        'processing_key',
    )
    readonly_fields = (
        'uuid',
        'created_at',
        'updated_at',
        'persistent_key',
        'processing_key',
    )
    raw_id_fields = ('source_file',)
