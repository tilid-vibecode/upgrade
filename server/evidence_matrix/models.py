from django.db import models

from basics.models import TimestampedModel


class EvidenceMatrixStatus(models.TextChoices):
    DRAFT = 'draft', 'Draft'
    READY = 'ready', 'Ready'
    RUNNING = 'running', 'Running'
    COMPLETED = 'completed', 'Completed'
    FAILED = 'failed', 'Failed'


class EvidenceSourceType(models.TextChoices):
    GOOGLE_WORKSPACE = 'google_workspace', 'Google Workspace'
    SPREADSHEET_UPLOAD = 'spreadsheet_upload', 'Spreadsheet upload'
    MANUAL = 'manual', 'Manual'
    API = 'api', 'API'


class EvidenceMatrixRun(TimestampedModel):
    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='evidence_matrices',
    )
    planning_context = models.ForeignKey(
        'org_context.PlanningContext',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='evidence_matrix_runs',
        help_text='Planning context this matrix is scoped to.',
    )
    blueprint_run = models.ForeignKey(
        'skill_blueprint.SkillBlueprintRun',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='evidence_matrix_runs',
    )
    title = models.CharField(max_length=255, default='Second-layer evidence matrix')
    status = models.CharField(
        max_length=32,
        choices=EvidenceMatrixStatus.choices,
        default=EvidenceMatrixStatus.DRAFT,
    )
    source_type = models.CharField(
        max_length=32,
        choices=EvidenceSourceType.choices,
        default=EvidenceSourceType.MANUAL,
    )
    connection_label = models.CharField(max_length=255, blank=True, default='')
    snapshot_key = models.CharField(max_length=1024, blank=True, default='')
    matrix_version = models.CharField(max_length=32, blank=True, default='stage8-v1')
    input_snapshot = models.JSONField(default=dict, blank=True)
    summary_payload = models.JSONField(default=dict, blank=True)
    heatmap_payload = models.JSONField(default=dict, blank=True)
    risk_payload = models.JSONField(default=dict, blank=True)
    incompleteness_payload = models.JSONField(default=dict, blank=True)
    matrix_payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['workspace', 'status']),
            models.Index(fields=['source_type', 'status']),
            models.Index(fields=['workspace', 'blueprint_run']),
            models.Index(fields=['workspace', 'planning_context']),
        ]

    def __str__(self) -> str:
        return f'{self.workspace.name} / {self.title}'
