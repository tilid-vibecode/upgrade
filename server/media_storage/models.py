from django.conf import settings
from django.db import models

from basics.models import TimestampedModel
from .managers import MediaFileManager


class MediaFile(TimestampedModel):

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending upload'
        UPLOADED = 'uploaded', 'Uploaded to storage'
        PROCESSING = 'processing', 'Being processed'
        READY = 'ready', 'Ready (processing complete)'
        FAILED = 'failed', 'Upload or processing failed'

    class FileCategory(models.TextChoices):
        IMAGE = 'image', 'Image'
        DOCUMENT = 'document', 'Document (PDF)'
        WORD = 'word', 'Word Document'
        TEXT = 'text', 'Text File'
        SPREADSHEET = 'spreadsheet', 'Spreadsheet / CSV'

    organization = models.ForeignKey(
        'organization.Organization',
        on_delete=models.CASCADE,
        related_name='media_files',
        null=True,
        blank=True,
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='uploaded_media_files',
    )

    original_filename = models.CharField(max_length=512)
    content_type = models.CharField(max_length=255)
    file_size = models.PositiveBigIntegerField(help_text='Size in bytes')
    file_category = models.CharField(
        max_length=32,
        choices=FileCategory.choices,
    )

    persistent_key = models.CharField(
        max_length=1024,
        unique=True,
        null=True,
        blank=True,
        help_text='Object key in persistent storage (S3 / MinIO-persistent).',
    )
    processing_key = models.CharField(
        max_length=1024,
        null=True,
        blank=True,
        help_text='Object key in processing storage (MinIO). Null when cleaned up.',
    )

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    error_msg = models.TextField(blank=True, default='')

    processing_description = models.TextField(
        blank=True,
        default='',
        help_text='Textual summary of processing results, editable by user',
    )
    processing_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text='Structured processing output: page count, dimensions, etc.',
    )

    discussion = models.ForeignKey(
        'feature.Discussion',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='media_files',
        help_text='If set, access is scoped to discussion members. Null = org-wide.',
    )
    prototype_workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='prototype_media_files',
        help_text='Workspace owner for prototype uploads and generated artifacts.',
    )

    objects = MediaFileManager()

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(
                    persistent_key__isnull=True,
                    processing_key__isnull=True,
                ),
                name='media_file_at_least_one_key',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        organization__isnull=False,
                        discussion__isnull=True,
                        prototype_workspace__isnull=True,
                    )
                    | models.Q(
                        organization__isnull=True,
                        discussion__isnull=False,
                        prototype_workspace__isnull=True,
                    )
                    | models.Q(
                        organization__isnull=True,
                        discussion__isnull=True,
                        prototype_workspace__isnull=False,
                    )
                    | models.Q(
                        organization__isnull=True,
                        discussion__isnull=True,
                        prototype_workspace__isnull=True,
                    )
                ),
                name='media_file_single_scope',
            ),
        ]
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['organization', 'file_category']),
            models.Index(fields=['organization', '-created_at']),
            models.Index(fields=['discussion', 'status']),
            models.Index(fields=['prototype_workspace', 'status']),
            models.Index(fields=['prototype_workspace', '-created_at']),
            models.Index(fields=['prototype_workspace', 'file_category']),
        ]

    def __str__(self):
        return f'{self.original_filename} ({self.status})'

    @property
    def file_size_display(self) -> str:
        if self.file_size < 1024:
            return f'{self.file_size} B'
        if self.file_size < 1024 * 1024:
            return f'{self.file_size / 1024:.1f} KB'
        return f'{self.file_size / (1024 * 1024):.1f} MB'

    @property
    def has_persistent(self) -> bool:
        return self.persistent_key is not None

    @property
    def has_processing(self) -> bool:
        return self.processing_key is not None


class MediaFileVariant(TimestampedModel):

    class VariantType(models.TextChoices):
        ORIGINAL = 'original', 'Original'
        THUMBNAIL = 'thumbnail', 'Thumbnail'
        PREVIEW = 'preview', 'Preview'
        OPTIMIZED = 'optimized', 'Optimized'

    source_file = models.ForeignKey(
        MediaFile,
        on_delete=models.CASCADE,
        related_name='variants',
    )
    variant_type = models.CharField(
        max_length=32,
        choices=VariantType.choices,
    )

    content_type = models.CharField(max_length=255)
    file_size = models.PositiveBigIntegerField()

    persistent_key = models.CharField(
        max_length=1024,
        unique=True,
        null=True,
        blank=True,
    )
    processing_key = models.CharField(
        max_length=1024,
        null=True,
        blank=True,
    )

    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)

    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text='Extra info: compression ratio, page number, etc.',
    )

    class Meta:
        unique_together = [('source_file', 'variant_type')]
        indexes = [
            models.Index(fields=['source_file', 'variant_type']),
        ]

    def __str__(self):
        return f'{self.source_file.original_filename} [{self.variant_type}]'
