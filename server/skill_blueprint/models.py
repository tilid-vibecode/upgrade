from django.db import models

from basics.models import TimestampedModel


class BlueprintStatus(models.TextChoices):
    DRAFT = 'draft', 'Draft'
    RUNNING = 'running', 'Running'
    NEEDS_CLARIFICATION = 'needs_clarification', 'Needs clarification'
    REVIEWED = 'reviewed', 'Reviewed'
    APPROVED = 'approved', 'Approved'
    COMPLETED = 'completed', 'Completed'
    FAILED = 'failed', 'Failed'


class RoleLibraryStatus(models.TextChoices):
    DRAFT = 'draft', 'Draft'
    RUNNING = 'running', 'Running'
    COMPLETED = 'completed', 'Completed'
    FAILED = 'failed', 'Failed'


class ClarificationCycleStatus(models.TextChoices):
    OPEN = 'open', 'Open'
    COMPLETED = 'completed', 'Completed'


class ClarificationQuestionStatus(models.TextChoices):
    OPEN = 'open', 'Open'
    ANSWERED = 'answered', 'Answered'
    ACCEPTED = 'accepted', 'Accepted'
    REJECTED = 'rejected', 'Rejected'
    OBSOLETE = 'obsolete', 'Obsolete'


BLUEPRINT_REVIEW_READY_STATUSES = (
    BlueprintStatus.REVIEWED,
    BlueprintStatus.APPROVED,
    BlueprintStatus.COMPLETED,
)

BLUEPRINT_GENERATED_STATUSES = (
    BlueprintStatus.DRAFT,
    BlueprintStatus.NEEDS_CLARIFICATION,
    BlueprintStatus.REVIEWED,
    BlueprintStatus.APPROVED,
    BlueprintStatus.COMPLETED,
)


class RoleLibrarySnapshot(TimestampedModel):
    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='role_library_snapshots',
    )
    provider = models.CharField(max_length=64, default='gitlab_handbook')
    status = models.CharField(
        max_length=32,
        choices=RoleLibraryStatus.choices,
        default=RoleLibraryStatus.DRAFT,
    )
    base_urls = models.JSONField(default=list, blank=True)
    discovery_payload = models.JSONField(default=dict, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['workspace', 'status']),
            models.Index(fields=['provider', 'status']),
        ]

    def __str__(self) -> str:
        return f'{self.workspace.name} / role library / {self.provider}'


class RoleLibraryEntry(TimestampedModel):
    snapshot = models.ForeignKey(
        RoleLibrarySnapshot,
        on_delete=models.CASCADE,
        related_name='entries',
    )
    role_name = models.CharField(max_length=255)
    department = models.CharField(max_length=255, blank=True, default='')
    role_family = models.CharField(max_length=255, blank=True, default='')
    page_url = models.URLField(max_length=1024)
    summary = models.TextField(blank=True, default='')
    levels = models.JSONField(default=list, blank=True)
    responsibilities = models.JSONField(default=list, blank=True)
    requirements = models.JSONField(default=list, blank=True)
    skills = models.JSONField(default=list, blank=True)
    raw_text = models.TextField(blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['role_name']
        unique_together = [('snapshot', 'page_url')]
        indexes = [
            models.Index(fields=['snapshot', 'department']),
            models.Index(fields=['snapshot', 'role_family']),
        ]

    def __str__(self) -> str:
        return self.role_name


class SkillBlueprintRun(TimestampedModel):
    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='skill_blueprints',
    )
    planning_context = models.ForeignKey(
        'org_context.PlanningContext',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='blueprint_runs',
        help_text='Planning context this blueprint is scoped to.',
    )
    title = models.CharField(max_length=255, default='First-layer blueprint')
    status = models.CharField(
        max_length=32,
        choices=BlueprintStatus.choices,
        default=BlueprintStatus.DRAFT,
    )
    role_library_snapshot = models.ForeignKey(
        RoleLibrarySnapshot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='blueprint_runs',
    )
    derived_from_run = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='derived_runs',
    )
    roadmap_analysis = models.ForeignKey(
        'org_context.RoadmapAnalysisRun',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='blueprint_runs',
        help_text='The roadmap analysis that provided structured input for this blueprint.',
    )
    generation_mode = models.CharField(max_length=32, default='generation')
    source_summary = models.JSONField(default=dict, blank=True)
    input_snapshot = models.JSONField(default=dict, blank=True)
    company_context = models.JSONField(default=dict, blank=True)
    roadmap_context = models.JSONField(default=list, blank=True)
    role_candidates = models.JSONField(default=list, blank=True)
    clarification_questions = models.JSONField(default=list, blank=True)
    employee_role_matches = models.JSONField(default=list, blank=True)
    required_skill_set = models.JSONField(default=list, blank=True)
    automation_candidates = models.JSONField(default=list, blank=True)
    occupation_map = models.JSONField(default=list, blank=True)
    gap_summary = models.JSONField(default=dict, blank=True)
    redundancy_summary = models.JSONField(default=dict, blank=True)
    assessment_plan = models.JSONField(default=dict, blank=True)
    review_summary = models.JSONField(default=dict, blank=True)
    change_log = models.JSONField(default=list, blank=True)
    reviewed_by = models.CharField(max_length=255, blank=True, default='')
    review_notes = models.TextField(blank=True, default='')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.CharField(max_length=255, blank=True, default='')
    approval_notes = models.TextField(blank=True, default='')
    approved_at = models.DateTimeField(null=True, blank=True)
    is_published = models.BooleanField(default=False)
    published_by = models.CharField(max_length=255, blank=True, default='')
    published_notes = models.TextField(blank=True, default='')
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['workspace', 'status']),
            models.Index(fields=['workspace', 'created_at']),
            models.Index(fields=['workspace', 'is_published']),
            models.Index(fields=['workspace', 'planning_context']),
        ]

    def __str__(self) -> str:
        return f'{self.workspace.name} / {self.title}'


class ClarificationCycle(TimestampedModel):
    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='clarification_cycles',
    )
    blueprint_run = models.OneToOneField(
        SkillBlueprintRun,
        on_delete=models.CASCADE,
        related_name='clarification_cycle',
    )
    title = models.CharField(max_length=255, default='Clarification cycle')
    status = models.CharField(
        max_length=32,
        choices=ClarificationCycleStatus.choices,
        default=ClarificationCycleStatus.OPEN,
    )
    summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['workspace', 'status']),
            models.Index(fields=['workspace', 'created_at']),
        ]

    def __str__(self) -> str:
        return f'{self.workspace.name} / {self.title}'


class ClarificationQuestion(TimestampedModel):
    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='clarification_questions',
    )
    cycle = models.ForeignKey(
        ClarificationCycle,
        on_delete=models.CASCADE,
        related_name='questions',
    )
    blueprint_run = models.ForeignKey(
        SkillBlueprintRun,
        on_delete=models.CASCADE,
        related_name='clarification_questions_db',
    )
    question_key = models.CharField(max_length=255)
    question_text = models.TextField()
    scope = models.CharField(max_length=64, default='blueprint')
    priority = models.CharField(max_length=32, default='medium')
    intended_respondent_type = models.CharField(max_length=64, default='operator')
    rationale = models.TextField(blank=True, default='')
    evidence_refs = models.JSONField(default=list, blank=True)
    impacted_roles = models.JSONField(default=list, blank=True)
    impacted_initiatives = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=32,
        choices=ClarificationQuestionStatus.choices,
        default=ClarificationQuestionStatus.OPEN,
    )
    answer_text = models.TextField(blank=True, default='')
    answered_by = models.CharField(max_length=255, blank=True, default='')
    answered_at = models.DateTimeField(null=True, blank=True)
    status_note = models.TextField(blank=True, default='')
    changed_target_model = models.BooleanField(default=False)
    effect_metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['created_at', 'question_key']
        unique_together = [('cycle', 'question_key')]
        indexes = [
            models.Index(fields=['workspace', 'status']),
            models.Index(fields=['blueprint_run', 'status']),
            models.Index(fields=['cycle', 'status']),
        ]

    def __str__(self) -> str:
        return self.question_text[:80]
