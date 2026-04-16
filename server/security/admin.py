# File location: /server/security/admin.py
from django.contrib import admin

from .models import SecurityViolation, UserSecurityProfile


@admin.register(SecurityViolation)
class SecurityViolationAdmin(admin.ModelAdmin):
    list_display = (
        'uuid',
        'user',
        'organization',
        'discussion_uuid',
        'violation_number',
        'resulted_in_block',
        'block_level_applied',
        'created_at',
    )
    list_filter = (
        'resulted_in_block',
        'block_level_applied',
        'detected_intent',
        'created_at',
    )
    search_fields = (
        'user__email',
        'discussion_uuid',
        'message_text',
    )
    readonly_fields = (
        'uuid',
        'user',
        'organization',
        'discussion_uuid',
        'message_text',
        'detected_intent',
        'violation_number',
        'resulted_in_block',
        'block_level_applied',
        'created_at',
        'updated_at',
    )
    ordering = ('-created_at',)


@admin.register(UserSecurityProfile)
class UserSecurityProfileAdmin(admin.ModelAdmin):
    list_display = (
        'user',
        'total_violations',
        'total_blocks',
        'current_block_level',
        'is_permanently_blocked',
        'last_violation_at',
        'block_expires_at',
    )
    list_filter = (
        'is_permanently_blocked',
        'current_block_level',
    )
    search_fields = ('user__email',)
    readonly_fields = (
        'uuid',
        'user',
        'total_violations',
        'total_blocks',
        'current_block_level',
        'is_permanently_blocked',
        'last_violation_at',
        'last_blocked_at',
        'block_expires_at',
        'created_at',
        'updated_at',
    )
