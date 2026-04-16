import uuid

from django.db import models

from basics.models import TimestampedModel


class LLMProvider(models.TextChoices):
    OPENAI = 'openai', 'OpenAI'
    LLAMA = 'llama', 'Llama'


class LLMCallType(models.TextChoices):
    COMPLETION = 'completion', 'Standard completion'
    TOOL_CALL = 'tool_call', 'LLM invoked tool(s)'
    TOOL_FOLLOWUP = 'tool_followup', 'Follow-up after tool result'
    VALIDATION_RETRY = 'validation_retry', 'Retry after validation failure'
    ITERATION_RETRY = 'iteration_retry', 'Quality iteration with higher temp'


class LLMRawLog(TimestampedModel):
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    stream_entry_id = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        unique=True,
        help_text='Redis stream entry ID (e.g. "1709123456789-0") used for dedup on flush retries',
    )
    organization_uuid = models.UUIDField(db_index=True)
    user_uuid = models.UUIDField(
        null=True, blank=True, db_index=True,
        help_text='Null for LLM calls without user context (e.g. component runners)',
    )
    discussion_uuid = models.UUIDField(
        null=True, blank=True, db_index=True,
        help_text='Null for non-discussion LLM calls (e.g. background tasks)',
    )
    is_org_member = models.BooleanField(
        default=True,
        help_text='Whether user was a confirmed org member at call time',
    )
    provider = models.CharField(max_length=16, choices=LLMProvider)
    model = models.CharField(max_length=64, help_text='Exact model string used')
    prompt_tokens = models.PositiveIntegerField(default=0)
    completion_tokens = models.PositiveIntegerField(default=0)
    total_tokens = models.PositiveIntegerField(default=0)
    is_successful = models.BooleanField(default=False)
    error_type = models.CharField(
        max_length=64, blank=True, default='',
        help_text='Short error class if failed (e.g. "timeout", "no_response")',
    )
    call_type = models.CharField(
        max_length=24,
        choices=LLMCallType,
        default=LLMCallType.COMPLETION,
        help_text='What kind of LLM round-trip this was',
    )
    tool_names = models.JSONField(
        default=list, blank=True,
        help_text='Names of tools invoked in this turn (empty for non-tool turns)',
    )
    caller_function = models.CharField(
        max_length=128, blank=True, default='',
        help_text='Which wrapper function initiated this call',
    )
    iteration = models.PositiveSmallIntegerField(
        default=0,
        help_text='Quality iteration index within a single processor invocation',
    )
    attempt = models.PositiveSmallIntegerField(
        default=0,
        help_text='Total attempt counter within a single processor invocation',
    )
    estimated_cost_micro = models.BigIntegerField(
        default=0,
        help_text='Estimated cost in microdollars (1 USD = 1,000,000)',
    )
    provider_request_id = models.CharField(
        max_length=128, blank=True, default='',
        help_text='Provider response ID for traceability',
    )
    called_at = models.DateTimeField(
        db_index=True,
        help_text='When the LLM API call was actually made',
    )

    class Meta:
        db_table = 'llm_usage_raw_log'
        ordering = ['-called_at']
        indexes = [
            models.Index(fields=['organization_uuid', '-called_at']),
            models.Index(fields=['user_uuid', '-called_at']),
            models.Index(fields=['discussion_uuid', '-called_at']),
            models.Index(fields=['provider', 'model', '-called_at']),
            models.Index(fields=['call_type', '-called_at']),
        ]

    def __str__(self) -> str:
        return (
            f'LLMLog({self.provider}/{self.model}, '
            f'type={self.call_type}, '
            f'tokens={self.total_tokens}, '
            f'ok={self.is_successful})'
        )

    @property
    def cost_usd(self) -> float:
        return self.estimated_cost_micro / 1_000_000


class HourlyUsageAggregate(TimestampedModel):
    hour = models.DateTimeField(help_text='Start of the hour (truncated)')
    organization_uuid = models.UUIDField()
    provider = models.CharField(max_length=16, choices=LLMProvider)
    model = models.CharField(max_length=64)

    total_calls = models.PositiveIntegerField(default=0)
    successful_calls = models.PositiveIntegerField(default=0)
    failed_calls = models.PositiveIntegerField(default=0)
    tool_call_turns = models.PositiveIntegerField(default=0)
    tool_followup_turns = models.PositiveIntegerField(default=0)

    prompt_tokens = models.BigIntegerField(default=0)
    completion_tokens = models.BigIntegerField(default=0)
    total_tokens = models.BigIntegerField(default=0)
    estimated_cost_micro = models.BigIntegerField(default=0)

    unique_users = models.PositiveIntegerField(default=0)
    unique_discussions = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'llm_usage_hourly_agg'
        unique_together = ['hour', 'organization_uuid', 'provider', 'model']
        indexes = [
            models.Index(fields=['organization_uuid', '-hour']),
            models.Index(fields=['-hour']),
        ]

    def __str__(self) -> str:
        return (
            f'HourlyAgg({self.hour:%Y-%m-%d %H:00}, '
            f'{self.provider}/{self.model}, '
            f'tokens={self.total_tokens})'
        )

    @property
    def cost_usd(self) -> float:
        return self.estimated_cost_micro / 1_000_000


class DailyUsageAggregate(TimestampedModel):
    day = models.DateField()
    organization_uuid = models.UUIDField()
    provider = models.CharField(max_length=16, choices=LLMProvider)
    model = models.CharField(max_length=64)

    total_calls = models.PositiveIntegerField(default=0)
    successful_calls = models.PositiveIntegerField(default=0)
    failed_calls = models.PositiveIntegerField(default=0)
    tool_call_turns = models.PositiveIntegerField(default=0)
    tool_followup_turns = models.PositiveIntegerField(default=0)

    prompt_tokens = models.BigIntegerField(default=0)
    completion_tokens = models.BigIntegerField(default=0)
    total_tokens = models.BigIntegerField(default=0)
    estimated_cost_micro = models.BigIntegerField(default=0)

    unique_users = models.PositiveIntegerField(default=0)
    unique_discussions = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'llm_usage_daily_agg'
        unique_together = ['day', 'organization_uuid', 'provider', 'model']
        indexes = [
            models.Index(fields=['organization_uuid', '-day']),
            models.Index(fields=['-day']),
        ]

    def __str__(self) -> str:
        return (
            f'DailyAgg({self.day}, '
            f'{self.provider}/{self.model}, '
            f'tokens={self.total_tokens})'
        )

    @property
    def cost_usd(self) -> float:
        return self.estimated_cost_micro / 1_000_000


class UserUsageSummary(TimestampedModel):
    user_uuid = models.UUIDField()
    organization_uuid = models.UUIDField()
    provider = models.CharField(max_length=16, choices=LLMProvider)
    model = models.CharField(max_length=64)

    total_calls = models.PositiveIntegerField(default=0)
    total_tokens = models.BigIntegerField(default=0)
    prompt_tokens = models.BigIntegerField(default=0)
    completion_tokens = models.BigIntegerField(default=0)
    estimated_cost_micro = models.BigIntegerField(default=0)
    last_call_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'llm_usage_user_summary'
        unique_together = ['user_uuid', 'organization_uuid', 'provider', 'model']
        indexes = [
            models.Index(fields=['organization_uuid', 'user_uuid']),
            models.Index(fields=['user_uuid', '-last_call_at']),
        ]

    def __str__(self) -> str:
        return (
            f'UserSummary({self.user_uuid}, '
            f'{self.provider}/{self.model}, '
            f'tokens={self.total_tokens})'
        )

    @property
    def cost_usd(self) -> float:
        return self.estimated_cost_micro / 1_000_000


class UserHourlyContribution(TimestampedModel):
    hour = models.DateTimeField(help_text='Start of the hour this contribution covers')
    user_uuid = models.UUIDField()
    organization_uuid = models.UUIDField()
    provider = models.CharField(max_length=16, choices=LLMProvider)
    model = models.CharField(max_length=64)

    total_calls = models.PositiveIntegerField(default=0)
    total_tokens = models.BigIntegerField(default=0)
    prompt_tokens = models.BigIntegerField(default=0)
    completion_tokens = models.BigIntegerField(default=0)
    estimated_cost_micro = models.BigIntegerField(default=0)

    class Meta:
        db_table = 'llm_usage_user_hourly_contrib'
        unique_together = [
            'hour', 'user_uuid', 'organization_uuid', 'provider', 'model',
        ]
        indexes = [
            models.Index(
                fields=['user_uuid', 'organization_uuid', 'provider', 'model'],
                name='idx_user_contrib_summary_key',
            ),
        ]

    def __str__(self) -> str:
        return (
            f'UserContrib({self.hour:%Y-%m-%d %H:00}, '
            f'{self.user_uuid}, {self.provider}/{self.model})'
        )


class DiscussionUsageSummary(TimestampedModel):
    discussion_uuid = models.UUIDField()
    organization_uuid = models.UUIDField()
    provider = models.CharField(max_length=16, choices=LLMProvider)
    model = models.CharField(max_length=64)

    total_calls = models.PositiveIntegerField(default=0)
    total_tokens = models.BigIntegerField(default=0)
    prompt_tokens = models.BigIntegerField(default=0)
    completion_tokens = models.BigIntegerField(default=0)
    tool_call_turns = models.PositiveIntegerField(default=0)
    estimated_cost_micro = models.BigIntegerField(default=0)

    class Meta:
        db_table = 'llm_usage_discussion_summary'
        unique_together = ['discussion_uuid', 'provider', 'model']
        indexes = [
            models.Index(fields=['organization_uuid', 'discussion_uuid']),
            models.Index(fields=['discussion_uuid']),
        ]

    def __str__(self) -> str:
        return (
            f'DiscussionSummary({self.discussion_uuid}, '
            f'{self.provider}/{self.model}, '
            f'tokens={self.total_tokens})'
        )

    @property
    def cost_usd(self) -> float:
        return self.estimated_cost_micro / 1_000_000


class DiscussionHourlyContribution(TimestampedModel):
    hour = models.DateTimeField(help_text='Start of the hour this contribution covers')
    discussion_uuid = models.UUIDField()
    organization_uuid = models.UUIDField()
    provider = models.CharField(max_length=16, choices=LLMProvider)
    model = models.CharField(max_length=64)

    total_calls = models.PositiveIntegerField(default=0)
    total_tokens = models.BigIntegerField(default=0)
    prompt_tokens = models.BigIntegerField(default=0)
    completion_tokens = models.BigIntegerField(default=0)
    estimated_cost_micro = models.BigIntegerField(default=0)
    tool_call_turns = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'llm_usage_discussion_hourly_contrib'
        unique_together = [
            'hour', 'discussion_uuid', 'provider', 'model',
        ]
        indexes = [
            models.Index(
                fields=['discussion_uuid', 'provider', 'model'],
                name='idx_disc_contrib_summary_key',
            ),
        ]

    def __str__(self) -> str:
        return (
            f'DiscussionContrib({self.hour:%Y-%m-%d %H:00}, '
            f'{self.discussion_uuid}, {self.provider}/{self.model})'
        )
