from django.db import models

from basics.models import TimestampedModel


class AssessmentStatus(models.TextChoices):
    DRAFT = 'draft', 'Draft'
    GENERATED = 'generated', 'Generated'
    RUNNING = 'running', 'Running'
    COMPLETED = 'completed', 'Completed'
    FAILED = 'failed', 'Failed'
    SUPERSEDED = 'superseded', 'Superseded'


class AssessmentPackStatus(models.TextChoices):
    DRAFT = 'draft', 'Draft'
    GENERATED = 'generated', 'Generated'
    OPENED = 'opened', 'Opened'
    SUBMITTED = 'submitted', 'Submitted'
    COMPLETED = 'completed', 'Completed'
    SUPERSEDED = 'superseded', 'Superseded'


class AssessmentCycle(TimestampedModel):
    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='assessment_cycles',
    )
    planning_context = models.ForeignKey(
        'org_context.PlanningContext',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assessment_cycles',
        help_text='Planning context this assessment cycle is scoped to.',
    )
    title = models.CharField(max_length=255, default='Initial assessment cycle')
    status = models.CharField(
        max_length=32,
        choices=AssessmentStatus.choices,
        default=AssessmentStatus.DRAFT,
    )
    blueprint_run = models.ForeignKey(
        'skill_blueprint.SkillBlueprintRun',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assessment_cycles',
    )
    uses_self_report = models.BooleanField(default=True)
    uses_performance_reviews = models.BooleanField(default=False)
    uses_feedback_360 = models.BooleanField(default=False)
    uses_skill_tests = models.BooleanField(default=False)
    configuration = models.JSONField(default=dict, blank=True)
    result_summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['workspace', 'status']),
            models.Index(fields=['workspace', 'blueprint_run']),
            models.Index(fields=['workspace', 'planning_context']),
        ]

    def __str__(self) -> str:
        return f'{self.workspace.name} / {self.title}'


class EmployeeAssessmentPack(TimestampedModel):
    cycle = models.ForeignKey(
        AssessmentCycle,
        on_delete=models.CASCADE,
        related_name='packs',
    )
    employee = models.ForeignKey(
        'org_context.Employee',
        on_delete=models.CASCADE,
        related_name='assessment_packs',
    )
    title = models.CharField(max_length=255, blank=True, default='')
    status = models.CharField(
        max_length=32,
        choices=AssessmentPackStatus.choices,
        default=AssessmentPackStatus.GENERATED,
    )
    questionnaire_version = models.CharField(max_length=32, blank=True, default='stage7-v1')
    active_vector_generation_id = models.CharField(max_length=64, blank=True, default='')
    questionnaire_payload = models.JSONField(default=dict, blank=True)
    selection_summary = models.JSONField(default=dict, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    fused_summary = models.JSONField(default=dict, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['employee__full_name']
        unique_together = [('cycle', 'employee')]
        indexes = [
            models.Index(fields=['cycle', 'status']),
            models.Index(fields=['employee', 'status']),
        ]

    def __str__(self) -> str:
        return f'{self.employee.full_name} / {self.cycle.title}'
