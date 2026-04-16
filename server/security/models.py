# File location: /server/security/models.py
from __future__ import annotations

from django.db import models

from basics.models import TimestampedModel


class BlockLevel(models.IntegerChoices):
    WARNING = 0, 'Warning only'
    THIRTY_MINUTES = 1, '30 minutes'
    ONE_HOUR = 2, '1 hour'
    SIX_HOURS = 3, '6 hours'
    TWENTY_FOUR_HOURS = 4, '24 hours'
    ONE_WEEK = 5, '1 week'
    PERMANENT = 6, 'Permanent'


class SecurityViolation(TimestampedModel):
    user = models.ForeignKey(
        'authentication.User',
        on_delete=models.CASCADE,
        related_name='security_violations',
    )
    organization = models.ForeignKey(
        'organization.Organization',
        on_delete=models.CASCADE,
        related_name='security_violations',
    )
    discussion_uuid = models.UUIDField(
        db_index=True,
        help_text='Discussion where the violation occurred',
    )
    message_text = models.TextField(
        help_text='The off-topic message content',
    )
    detected_intent = models.CharField(
        max_length=64,
        default='off_topic',
        help_text='Intent detected by request parser',
    )
    violation_number = models.PositiveSmallIntegerField(
        default=1,
        help_text='Nth violation in this chat (1 = warning, 2 = block)',
    )
    resulted_in_block = models.BooleanField(
        default=False,
        help_text='Whether this violation triggered a block',
    )
    block_level_applied = models.SmallIntegerField(
        choices=BlockLevel.choices,
        null=True,
        blank=True,
        help_text='Block tier applied (if resulted_in_block)',
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['discussion_uuid', '-created_at']),
            models.Index(fields=['organization', '-created_at']),
        ]

    def __str__(self) -> str:
        return (
            f'Violation(user={self.user_id}, '
            f'discussion={self.discussion_uuid}, '
            f'#{self.violation_number}, '
            f'blocked={self.resulted_in_block})'
        )


class UserSecurityProfile(TimestampedModel):
    user = models.OneToOneField(
        'authentication.User',
        on_delete=models.CASCADE,
        related_name='security_profile',
    )
    total_violations = models.PositiveIntegerField(
        default=0,
        help_text='Lifetime count of off-topic violations',
    )
    total_blocks = models.PositiveIntegerField(
        default=0,
        help_text='Lifetime count of blocks applied',
    )
    current_block_level = models.SmallIntegerField(
        choices=BlockLevel.choices,
        default=BlockLevel.WARNING,
        help_text='Current escalation tier (persists across blocks)',
    )
    is_permanently_blocked = models.BooleanField(
        default=False,
        help_text='True when block_level reaches PERMANENT',
    )
    last_violation_at = models.DateTimeField(null=True, blank=True)
    last_blocked_at = models.DateTimeField(null=True, blank=True)
    block_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the current block expires (null if not blocked or permanent)',
    )

    class Meta:
        indexes = [
            models.Index(fields=['is_permanently_blocked']),
            models.Index(fields=['current_block_level']),
        ]

    def __str__(self) -> str:
        return (
            f'SecurityProfile(user={self.user_id}, '
            f'level={self.current_block_level}, '
            f'violations={self.total_violations})'
        )
