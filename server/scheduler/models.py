import uuid
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from croniter import croniter
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from basics.models import TimestampedModel



class ScheduleType(models.TextChoices):
    CRON = 'cron', 'Cron expression'
    INTERVAL = 'interval', 'Fixed interval (seconds)'
    ONCE = 'once', 'One-shot at specific time'


class ExecutionStatus(models.TextChoices):
    DISPATCHED = 'dispatched', 'Message dispatched to broker'
    SKIPPED = 'skipped', 'Skipped (misfire / concurrency)'
    FAILED = 'failed', 'Dispatch failed'



class ScheduledTask(TimestampedModel):

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    name = models.CharField(
        max_length=200, unique=True,
        help_text='Unique human-readable identifier (e.g. "scheduler.cleanup_executions")',
    )
    description = models.TextField(
        blank=True, default='',
        help_text='What this task does — shown in admin and future UI',
    )

    task_path = models.CharField(
        max_length=255,
        help_text='Dotted path to the Dramatiq actor (e.g. "scheduler.tasks.cleanup_old_executions")',
    )
    task_kwargs = models.JSONField(
        default=dict, blank=True,
        help_text='Keyword arguments passed to the actor',
    )
    queue = models.CharField(
        max_length=64, default='default',
        help_text='Dramatiq queue name',
    )

    schedule_type = models.CharField(
        max_length=16, choices=ScheduleType,
    )
    cron_expression = models.CharField(
        max_length=100, blank=True, default='',
        help_text='5-field cron: "minute hour day month weekday" (only for cron type)',
    )
    interval_seconds = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Seconds between runs (only for interval type)',
    )
    run_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Exact time for one-shot tasks (only for once type)',
    )

    is_active = models.BooleanField(
        default=True, db_index=True,
        help_text='Inactive tasks are never dispatched',
    )
    next_run_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text='When the scheduler should next dispatch this task',
    )
    last_run_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When this task was last dispatched',
    )

    max_instances = models.PositiveSmallIntegerField(
        default=1,
        help_text='Max concurrent Dramatiq messages for this schedule',
    )
    misfire_grace_seconds = models.PositiveIntegerField(
        default=300,
        help_text='Skip dispatch if overdue by more than this many seconds',
    )
    consecutive_failures = models.PositiveIntegerField(
        default=0,
        help_text='Number of consecutive dispatch failures. Reset on success.',
    )
    auto_paused = models.BooleanField(
        default=False,
        help_text='True if the scheduler auto-paused this task after repeated failures. '
                  'Re-activating via admin clears this flag.',
    )

    is_system = models.BooleanField(
        default=False,
        help_text='System tasks are seeded by code and cannot be deleted via API',
    )
    organization_uuid = models.UUIDField(
        null=True, blank=True, db_index=True,
        help_text='Null for system tasks; set for org-scoped schedules',
    )
    created_by_uuid = models.UUIDField(
        null=True, blank=True,
        help_text='User or LLM agent that created this schedule',
    )
    user_timezone = models.CharField(
        max_length=64, blank=True, default='UTC',
        help_text='IANA timezone for user-facing schedules (e.g. "America/New_York"). '
                  'Cron expressions are evaluated in this timezone.',
    )

    class Meta:
        db_table = 'scheduler_task'
        ordering = ['name']
        indexes = [
            models.Index(fields=['is_active', 'next_run_at']),
            models.Index(fields=['organization_uuid', 'is_active']),
        ]
        constraints = [
            models.CheckConstraint(
                name='sched_cron_requires_expression',
                condition=~models.Q(schedule_type='cron') | ~models.Q(cron_expression=''),
            ),
            models.CheckConstraint(
                name='sched_interval_requires_seconds',
                condition=~models.Q(schedule_type='interval') | models.Q(interval_seconds__gte=1),
            ),
            models.CheckConstraint(
                name='sched_once_requires_run_at',
                condition=~models.Q(schedule_type='once') | ~models.Q(run_at__isnull=True),
            ),
            models.CheckConstraint(
                name='sched_max_instances_positive',
                condition=models.Q(max_instances__gte=1),
            ),
        ]

    def __str__(self) -> str:
        status = 'active' if self.is_active else 'paused'
        return f'{self.name} ({self.schedule_type}, {status})'

    def clean(self) -> None:
        errors = {}

        if self.schedule_type == ScheduleType.CRON:
            if not self.cron_expression:
                errors['cron_expression'] = (
                    'Cron expression is required for cron schedule type.'
                )
            else:
                try:
                    croniter(self.cron_expression)
                except (ValueError, KeyError) as exc:
                    errors['cron_expression'] = (
                        f'Invalid cron expression: {exc}'
                    )

        elif self.schedule_type == ScheduleType.INTERVAL:
            if not self.interval_seconds or self.interval_seconds < 1:
                errors['interval_seconds'] = (
                    'A positive interval_seconds is required for interval schedule type.'
                )

        elif self.schedule_type == ScheduleType.ONCE:
            if not self.run_at:
                errors['run_at'] = (
                    'run_at datetime is required for one-shot schedule type.'
                )

        tz_value = self.user_timezone or 'UTC'
        try:
            ZoneInfo(tz_value)
        except (KeyError, ValueError):
            errors['user_timezone'] = (
                f'Unknown IANA timezone: \'{tz_value}\'. '
                f'Example valid values: \'UTC\', \'America/New_York\', \'Europe/London\'.'
            )

        if self.max_instances is not None and self.max_instances < 1:
            errors['max_instances'] = 'max_instances must be at least 1.'

        if errors:
            raise ValidationError(errors)

    def compute_next_run(self, after=None) -> Optional[datetime]:
        after = after or timezone.now()

        if self.schedule_type == ScheduleType.CRON:
            if not self.cron_expression:
                return None
            tz = ZoneInfo(self.user_timezone or 'UTC')
            after_local = after.astimezone(tz)
            cron = croniter(self.cron_expression, after_local)
            next_local = cron.get_next(datetime)
            return next_local.astimezone(ZoneInfo('UTC'))

        if self.schedule_type == ScheduleType.INTERVAL:
            if not self.interval_seconds:
                return None
            return after + timedelta(seconds=self.interval_seconds)

        if self.schedule_type == ScheduleType.ONCE:
            if self.run_at and self.run_at > after and not self.last_run_at:
                return self.run_at
            return None

        return None

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        _execution_state_fields = {
            'next_run_at', 'last_run_at', 'is_active', 'updated_at',
            'consecutive_failures', 'auto_paused',
        }
        if not update_fields or not set(update_fields).issubset(_execution_state_fields):
            self.full_clean()

        if self.is_active and not self.next_run_at:
            if self.schedule_type == ScheduleType.ONCE and self.run_at:
                self.next_run_at = self.run_at
            else:
                self.next_run_at = self.compute_next_run()
        super().save(*args, **kwargs)



class TaskExecution(TimestampedModel):

    task = models.ForeignKey(
        ScheduledTask,
        on_delete=models.CASCADE,
        related_name='executions',
    )
    status = models.CharField(
        max_length=16, choices=ExecutionStatus,
    )
    dispatched_at = models.DateTimeField(default=timezone.now)
    error = models.TextField(blank=True, default='')
    dramatiq_message_id = models.CharField(
        max_length=64, blank=True, default='',
        help_text='Dramatiq message ID for tracing',
    )

    class Meta:
        db_table = 'scheduler_execution'
        ordering = ['-dispatched_at']
        indexes = [
            models.Index(fields=['task', '-dispatched_at']),
        ]

    def __str__(self) -> str:
        return f'{self.task.name} @ {self.dispatched_at:%Y-%m-%d %H:%M} → {self.status}'
