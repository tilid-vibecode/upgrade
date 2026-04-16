import secrets

from django.db import models

from basics.models import TimestampedModel


class WorkspaceStatus(models.TextChoices):
    DRAFT = 'draft', 'Draft'
    COLLECTING = 'collecting', 'Collecting materials'
    PROCESSING = 'processing', 'Processing'
    COMPLETED = 'completed', 'Completed'


class SourceDocumentStatus(models.TextChoices):
    UPLOADED = 'uploaded', 'Uploaded'
    FAILED = 'failed', 'Failed'


class SourceDocumentKind(models.TextChoices):
    CSV = 'csv', 'CSV'
    PDF = 'pdf', 'PDF'


class WorkspaceSourceKind(models.TextChoices):
    STRATEGY = 'strategy', 'Strategy'
    ROADMAP = 'roadmap', 'Roadmap'
    JOB_DESCRIPTION = 'job_description', 'Job description'
    EXISTING_MATRIX = 'existing_matrix', 'Existing matrix'
    ORG_CSV = 'org_csv', 'Organization CSV'
    EMPLOYEE_CV = 'employee_cv', 'Employee CV'
    OTHER = 'other', 'Other'


class WorkspaceSourceTransport(models.TextChoices):
    MEDIA_FILE = 'media_file', 'Uploaded media file'
    INLINE_TEXT = 'inline_text', 'Inline text'
    EXTERNAL_URL = 'external_url', 'External URL'


class WorkspaceSourceStatus(models.TextChoices):
    ATTACHED = 'attached', 'Attached'
    PARSING = 'parsing', 'Parsing'
    PARSED = 'parsed', 'Parsed'
    FAILED = 'failed', 'Failed'
    ARCHIVED = 'archived', 'Archived'


class IntakeWorkspace(TimestampedModel):
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    notes = models.TextField(blank=True, default='')
    organization = models.ForeignKey(
        'organization.Organization',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='workspaces',
        help_text='Organization this workspace belongs to. Null for legacy or unlinked workspaces.',
    )
    status = models.CharField(
        max_length=32,
        choices=WorkspaceStatus.choices,
        default=WorkspaceStatus.DRAFT,
    )
    metadata = models.JSONField(default=dict, blank=True)
    operator_token = models.CharField(
        max_length=64,
        unique=True,
        default=secrets.token_urlsafe,
        help_text='Bearer token required for workspace admin operations.',
    )

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['slug']),
            models.Index(fields=['status', '-updated_at']),
        ]

    def __str__(self) -> str:
        return self.name


class SourceDocument(TimestampedModel):
    """Legacy intake document record.

    Kept for backward compatibility with the first upload flow. New prototype
    flows should prefer ``WorkspaceSource`` linked to ``media_storage.MediaFile``.
    """

    workspace = models.ForeignKey(
        IntakeWorkspace,
        on_delete=models.CASCADE,
        related_name='documents',
    )
    original_filename = models.CharField(max_length=512)
    content_type = models.CharField(max_length=255)
    file_size = models.PositiveBigIntegerField()
    document_kind = models.CharField(
        max_length=16,
        choices=SourceDocumentKind.choices,
    )
    status = models.CharField(
        max_length=16,
        choices=SourceDocumentStatus.choices,
        default=SourceDocumentStatus.UPLOADED,
    )
    persistent_key = models.CharField(max_length=1024, unique=True)
    processing_key = models.CharField(max_length=1024, unique=True)
    storage_metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['workspace', 'document_kind']),
            models.Index(fields=['workspace', '-created_at']),
            models.Index(fields=['status', '-created_at']),
        ]

    def __str__(self) -> str:
        return f'{self.original_filename} ({self.document_kind})'


class WorkspaceSource(TimestampedModel):
    workspace = models.ForeignKey(
        IntakeWorkspace,
        on_delete=models.CASCADE,
        related_name='sources',
    )
    title = models.CharField(max_length=255, blank=True, default='')
    notes = models.TextField(blank=True, default='')
    source_kind = models.CharField(
        max_length=32,
        choices=WorkspaceSourceKind.choices,
    )
    transport = models.CharField(
        max_length=32,
        choices=WorkspaceSourceTransport.choices,
    )
    media_file = models.ForeignKey(
        'media_storage.MediaFile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='workspace_sources',
    )
    external_url = models.URLField(blank=True, default='')
    inline_text = models.TextField(blank=True, default='')
    language_code = models.CharField(max_length=16, blank=True, default='')
    status = models.CharField(
        max_length=16,
        choices=WorkspaceSourceStatus.choices,
        default=WorkspaceSourceStatus.ATTACHED,
    )
    parse_error = models.TextField(blank=True, default='')
    parse_metadata = models.JSONField(default=dict, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['workspace', 'source_kind']),
            models.Index(fields=['workspace', 'status']),
            models.Index(fields=['transport', 'status']),
        ]

    def __str__(self) -> str:
        label = self.title or self.source_kind
        return f'{self.workspace.slug} / {label}'
