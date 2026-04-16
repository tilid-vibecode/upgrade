from django.db import models

from basics.models import TimestampedModel


class PlanScope(models.TextChoices):
    TEAM = 'team', 'Team'
    INDIVIDUAL = 'individual', 'Individual'


class PlanRunStatus(models.TextChoices):
    DRAFT = 'draft', 'Draft'
    READY = 'ready', 'Ready'
    RUNNING = 'running', 'Running'
    COMPLETED = 'completed', 'Completed'
    FAILED = 'failed', 'Failed'


class ArtifactFormat(models.TextChoices):
    JSON = 'json', 'JSON'
    MARKDOWN = 'markdown', 'Markdown'
    HTML = 'html', 'HTML'


class DevelopmentPlanRun(TimestampedModel):
    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='development_plans',
    )
    planning_context = models.ForeignKey(
        'org_context.PlanningContext',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='development_plan_runs',
        help_text='Planning context this plan is scoped to.',
    )
    employee = models.ForeignKey(
        'org_context.Employee',
        on_delete=models.CASCADE,
        related_name='development_plans',
        null=True,
        blank=True,
    )
    blueprint_run = models.ForeignKey(
        'skill_blueprint.SkillBlueprintRun',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='development_plans',
    )
    matrix_run = models.ForeignKey(
        'evidence_matrix.EvidenceMatrixRun',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='development_plans',
    )
    title = models.CharField(max_length=255, default='Final development plan')
    scope = models.CharField(
        max_length=32,
        choices=PlanScope.choices,
        default=PlanScope.TEAM,
    )
    status = models.CharField(
        max_length=32,
        choices=PlanRunStatus.choices,
        default=PlanRunStatus.DRAFT,
    )
    generation_batch_uuid = models.UUIDField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    is_current = models.BooleanField(default=False)
    plan_version = models.CharField(max_length=32, blank=True, default='stage9-v1')
    input_snapshot = models.JSONField(default=dict, blank=True)
    recommendation_payload = models.JSONField(default=dict, blank=True)
    export_snapshot = models.JSONField(default=dict, blank=True)
    final_report_key = models.CharField(max_length=1024, blank=True, default='')
    summary = models.JSONField(default=dict, blank=True)
    plan_payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['workspace', 'status']),
            models.Index(fields=['scope', 'status']),
            models.Index(fields=['workspace', 'employee']),
            models.Index(fields=['workspace', 'blueprint_run']),
            models.Index(fields=['workspace', 'matrix_run']),
            models.Index(fields=['workspace', 'generation_batch_uuid']),
            models.Index(fields=['workspace', 'is_current']),
            models.Index(fields=['workspace', 'planning_context']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(scope=PlanScope.TEAM, employee__isnull=True)
                    | models.Q(scope=PlanScope.INDIVIDUAL, employee__isnull=False)
                ),
                name='development_plan_scope_employee_consistent',
            ),
            models.UniqueConstraint(
                fields=['workspace', 'scope'],
                condition=models.Q(
                    is_current=True,
                    scope=PlanScope.TEAM,
                    planning_context__isnull=True,
                ),
                name='development_plan_one_current_team_legacy',
            ),
            models.UniqueConstraint(
                fields=['workspace', 'employee'],
                condition=models.Q(
                    is_current=True,
                    scope=PlanScope.INDIVIDUAL,
                    planning_context__isnull=True,
                ),
                name='development_plan_one_current_individual_legacy',
            ),
            models.UniqueConstraint(
                fields=['workspace', 'planning_context', 'scope'],
                condition=models.Q(
                    is_current=True,
                    scope=PlanScope.TEAM,
                    planning_context__isnull=False,
                ),
                name='development_plan_one_current_team_per_context',
            ),
            models.UniqueConstraint(
                fields=['workspace', 'planning_context', 'employee'],
                condition=models.Q(
                    is_current=True,
                    scope=PlanScope.INDIVIDUAL,
                    planning_context__isnull=False,
                ),
                name='development_plan_one_current_individual_per_context',
            ),
            models.UniqueConstraint(
                fields=['generation_batch_uuid', 'scope'],
                condition=models.Q(scope=PlanScope.TEAM, generation_batch_uuid__isnull=False),
                name='development_plan_one_team_per_batch',
            ),
            models.UniqueConstraint(
                fields=['generation_batch_uuid', 'employee'],
                condition=models.Q(scope=PlanScope.INDIVIDUAL, generation_batch_uuid__isnull=False),
                name='development_plan_one_individual_per_batch',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.workspace.name} / {self.title}'


class DevelopmentPlanArtifact(TimestampedModel):
    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='development_plan_artifacts',
    )
    plan_run = models.ForeignKey(
        DevelopmentPlanRun,
        on_delete=models.CASCADE,
        related_name='artifacts',
    )
    employee = models.ForeignKey(
        'org_context.Employee',
        on_delete=models.CASCADE,
        related_name='development_plan_artifacts',
        null=True,
        blank=True,
    )
    media_file = models.ForeignKey(
        'media_storage.MediaFile',
        on_delete=models.PROTECT,
        related_name='development_plan_artifacts',
    )
    blueprint_run = models.ForeignKey(
        'skill_blueprint.SkillBlueprintRun',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='development_plan_artifacts',
    )
    matrix_run = models.ForeignKey(
        'evidence_matrix.EvidenceMatrixRun',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='development_plan_artifacts',
    )
    generation_batch_uuid = models.UUIDField(null=True, blank=True)
    artifact_scope = models.CharField(
        max_length=32,
        choices=PlanScope.choices,
        default=PlanScope.TEAM,
    )
    artifact_format = models.CharField(
        max_length=32,
        choices=ArtifactFormat.choices,
        default=ArtifactFormat.JSON,
    )
    artifact_version = models.CharField(max_length=32, blank=True, default='stage10-v1')
    is_current = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['workspace', 'artifact_scope', 'artifact_format']),
            models.Index(fields=['workspace', 'generation_batch_uuid']),
            models.Index(fields=['workspace', 'is_current']),
            models.Index(fields=['plan_run', 'artifact_format']),
            models.Index(fields=['workspace', 'employee', 'artifact_scope']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(artifact_scope=PlanScope.TEAM, employee__isnull=True)
                    | models.Q(artifact_scope=PlanScope.INDIVIDUAL, employee__isnull=False)
                ),
                name='development_plan_artifact_scope_employee_consistent',
            ),
            models.UniqueConstraint(
                fields=['plan_run', 'artifact_format'],
                name='development_plan_one_artifact_per_format_per_run',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.plan_run.title} [{self.artifact_format}]'
