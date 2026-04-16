from django.core.exceptions import ValidationError
from django.db import models

from basics.models import TimestampedModel


class EscoImportStatus(models.TextChoices):
    PENDING = 'pending', 'Pending'
    RUNNING = 'running', 'Running'
    COMPLETED = 'completed', 'Completed'
    FAILED = 'failed', 'Failed'


class ParsedSource(TimestampedModel):
    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='parsed_sources',
    )
    source = models.OneToOneField(
        'company_intake.WorkspaceSource',
        on_delete=models.CASCADE,
        related_name='parsed_source',
    )
    parser_name = models.CharField(max_length=64, default='prototype-v1')
    parser_version = models.CharField(max_length=32, default='1.0')
    content_type = models.CharField(max_length=255, blank=True, default='')
    page_count = models.PositiveIntegerField(null=True, blank=True)
    word_count = models.PositiveIntegerField(default=0)
    char_count = models.PositiveIntegerField(default=0)
    extracted_text = models.TextField(blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['workspace', '-updated_at']),
        ]

    def __str__(self) -> str:
        return f'{self.workspace.slug} / parsed / {self.source_id}'


class SourceChunk(TimestampedModel):
    parsed_source = models.ForeignKey(
        ParsedSource,
        on_delete=models.CASCADE,
        related_name='chunks',
    )
    chunk_index = models.PositiveIntegerField()
    text = models.TextField()
    char_count = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['chunk_index']
        unique_together = [('parsed_source', 'chunk_index')]
        indexes = [
            models.Index(fields=['parsed_source', 'chunk_index']),
        ]

    def __str__(self) -> str:
        return f'{self.parsed_source_id} / chunk {self.chunk_index}'


class RoadmapAnalysisRun(TimestampedModel):
    """
    Structured roadmap decomposition produced before blueprint generation.
    Breaks roadmap and strategy inputs into initiatives, workstreams,
    capability bundles, dependencies, and delivery risks.
    """

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='roadmap_analyses',
    )
    planning_context = models.ForeignKey(
        'org_context.PlanningContext',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='roadmap_analyses',
        help_text='Planning context this analysis is scoped to.',
    )
    title = models.CharField(max_length=255, default='Roadmap analysis')
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    analysis_version = models.CharField(
        max_length=32,
        default='roadmap-v1',
        help_text='Schema version for the structured output fields.',
    )
    source_summary = models.JSONField(
        default=dict,
        blank=True,
        help_text='Summary of which sources were analyzed: UUIDs, titles, kinds, and counts.',
    )
    input_snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text='Snapshot of workspace profile and analysis fingerprint at run time.',
    )
    initiatives = models.JSONField(
        default=list,
        blank=True,
        help_text='Structured initiatives extracted from roadmap and strategy inputs.',
    )
    workstreams = models.JSONField(
        default=list,
        blank=True,
        help_text='Structured delivery workstreams derived from initiatives.',
    )
    dependencies = models.JSONField(
        default=list,
        blank=True,
        help_text='Cross-workstream dependencies identified during analysis.',
    )
    delivery_risks = models.JSONField(
        default=list,
        blank=True,
        help_text='Delivery risks surfaced from roadmap decomposition.',
    )
    capability_bundles = models.JSONField(
        default=list,
        blank=True,
        help_text='Clustered capability bundles grouped across workstreams.',
    )
    prd_summaries = models.JSONField(
        default=list,
        blank=True,
        help_text='PRD-style initiative summaries derived from the roadmap.',
    )
    clarification_questions = models.JSONField(
        default=list,
        blank=True,
        help_text='Roadmap-analysis questions requiring operator clarification.',
    )
    error_message = models.TextField(
        blank=True,
        default='',
        help_text='Error details if the run failed.',
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['workspace', 'status']),
            models.Index(fields=['workspace', '-created_at']),
            models.Index(fields=['workspace', 'planning_context', 'status']),
        ]

    def __str__(self) -> str:
        return f'{self.title} ({self.status})'


class PlanningContext(TimestampedModel):
    """
    Hierarchical planning scope for org-wide, project, or scenario planning.
    """

    class Kind(models.TextChoices):
        ORG = 'org', 'Organization baseline'
        PROJECT = 'project', 'Project'
        SCENARIO = 'scenario', 'Scenario'

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        ARCHIVED = 'archived', 'Archived'
        DRAFT = 'draft', 'Draft'

    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='planning_contexts',
        null=True,
        blank=True,
        help_text='Workspace this context belongs to. Null for org-only contexts.',
    )
    organization = models.ForeignKey(
        'organization.Organization',
        on_delete=models.CASCADE,
        related_name='planning_contexts',
        null=True,
        blank=True,
        help_text='Organization this context belongs to.',
    )
    project = models.ForeignKey(
        'org_context.Project',
        on_delete=models.SET_NULL,
        related_name='planning_contexts',
        null=True,
        blank=True,
        help_text='Project this context is scoped to when kind=project.',
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255)
    kind = models.CharField(
        max_length=32,
        choices=Kind.choices,
        default=Kind.ORG,
    )
    parent_context = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='child_contexts',
        help_text='Parent context for inheritance. Org contexts have no parent.',
    )
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    description = models.TextField(blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['kind', 'name']
        indexes = [
            models.Index(fields=['workspace', 'kind']),
            models.Index(fields=['organization', 'kind']),
            models.Index(fields=['workspace', 'slug']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['workspace', 'slug'],
                condition=models.Q(workspace__isnull=False),
                name='uq_planning_context_workspace_slug',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(workspace__isnull=False) | models.Q(organization__isnull=False)
                ),
                name='planning_context_has_scope',
            ),
            models.CheckConstraint(
                condition=models.Q(kind='org', parent_context__isnull=True) | ~models.Q(kind='org'),
                name='planning_context_org_no_parent',
            ),
            models.CheckConstraint(
                condition=models.Q(kind='project', project__isnull=False) | ~models.Q(kind='project'),
                name='planning_context_project_requires_project',
            ),
            models.CheckConstraint(
                condition=models.Q(kind='scenario', parent_context__isnull=False) | ~models.Q(kind='scenario'),
                name='planning_context_scenario_requires_parent',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.name} ({self.kind})'

    def clean(self):
        errors = {}
        parent_context = self.parent_context if self.parent_context_id else None
        project = self.project if self.project_id else None

        if self.kind == self.Kind.ORG:
            if parent_context is not None:
                errors['parent_context'] = 'Org contexts cannot have a parent.'
            if project is not None:
                errors['project'] = 'Org contexts cannot reference a project.'
        elif self.kind == self.Kind.PROJECT:
            if project is None:
                errors['project'] = 'Project contexts require project_uuid.'
            if parent_context is None or parent_context.kind != self.Kind.ORG:
                errors['parent_context'] = 'Project contexts must inherit from an org context.'
        elif self.kind == self.Kind.SCENARIO:
            if project is not None:
                errors['project'] = 'Scenario contexts cannot reference project_uuid directly.'
            if parent_context is None:
                errors['parent_context'] = 'Scenario contexts require parent_context_uuid.'
            elif parent_context.kind not in {self.Kind.ORG, self.Kind.PROJECT}:
                errors['parent_context'] = 'Scenario contexts must inherit from an org or project context.'

        if self.pk is not None and self.parent_context_id == self.pk:
            errors['parent_context'] = 'A planning context cannot be its own parent.'
        if parent_context is not None and self.workspace_id and parent_context.workspace_id != self.workspace_id:
            errors['parent_context'] = 'Parent context must belong to the same workspace.'
        if project is not None and self.workspace_id and project.workspace_id != self.workspace_id:
            errors['project'] = 'Project must belong to the same workspace.'
        if (
            parent_context is not None
            and self.organization_id is not None
            and parent_context.organization_id is not None
            and parent_context.organization_id != self.organization_id
        ):
            errors['parent_context'] = 'Parent context must belong to the same organization.'

        if errors:
            raise ValidationError(errors)

    @staticmethod
    def resolve_effective_profile(planning_context):
        chain = []
        current = planning_context
        while current is not None:
            chain.append(current)
            current = current.parent_context
        chain.reverse()

        effective = {
            'company_profile': {},
            'tech_stack': [],
            'constraints': [],
            'growth_goals': [],
        }

        for ctx in chain:
            profile = getattr(ctx, 'profile', None)
            if profile is None:
                continue
            inherits_from_parent = bool(profile.inherit_from_parent)
            if ctx.kind == PlanningContext.Kind.ORG and ctx.parent_context_id is None:
                inherits_from_parent = False
            if not inherits_from_parent:
                effective = {
                    'company_profile': dict(profile.company_profile or {}),
                    'tech_stack': list(profile.tech_stack or []),
                    'constraints': list(profile.constraints or []),
                    'growth_goals': list(profile.growth_goals or []),
                }
                continue

            for field in profile.override_fields or []:
                value = getattr(profile, field, None)
                if value is None:
                    continue
                if field == 'tech_stack':
                    combined = list(set(effective['tech_stack'] + list(value or [])))
                    removals = set(profile.tech_stack_remove or [])
                    effective['tech_stack'] = [item for item in combined if item not in removals]
                elif field == 'company_profile':
                    effective['company_profile'] = {**effective['company_profile'], **dict(value or {})}
                else:
                    effective[field] = list(value) if isinstance(value, list) else value

        effective['tech_stack'] = sorted(set(effective['tech_stack']))
        return effective

    @staticmethod
    def resolve_effective_sources(planning_context):
        chain = []
        current = planning_context
        while current is not None:
            chain.append(current.pk)
            current = current.parent_context

        all_links = list(
            PlanningContextSource.objects.filter(
                planning_context_id__in=chain,
            ).select_related('workspace_source', 'planning_context')
        )
        priority = {pk: index for index, pk in enumerate(chain)}
        best_by_source = {}
        for link in all_links:
            source_id = link.workspace_source_id
            link_priority = priority.get(link.planning_context_id, 999)
            if source_id not in best_by_source or link_priority < best_by_source[source_id][0]:
                best_by_source[source_id] = (link_priority, link)
        return [
            link
            for _priority, link in best_by_source.values()
            if link.is_active
        ]


class ContextProfile(TimestampedModel):
    planning_context = models.OneToOneField(
        PlanningContext,
        on_delete=models.CASCADE,
        related_name='profile',
    )
    company_profile = models.JSONField(
        default=dict,
        blank=True,
        help_text='Company profile fields. Root contexts keep the full profile; child contexts keep overrides.',
    )
    tech_stack = models.JSONField(
        default=list,
        blank=True,
        help_text='Technology stack additions or the full stack for non-inheriting contexts.',
    )
    tech_stack_remove = models.JSONField(
        default=list,
        blank=True,
        help_text='Technologies removed from the inherited stack for child contexts.',
    )
    constraints = models.JSONField(
        default=list,
        blank=True,
        help_text='Planning constraints such as budget or team-size caps.',
    )
    growth_goals = models.JSONField(
        default=list,
        blank=True,
        help_text='Growth goals relevant to this planning scope.',
    )
    inherit_from_parent = models.BooleanField(
        default=True,
        help_text='Whether this profile inherits unoverridden values from its parent context.',
    )
    override_fields = models.JSONField(
        default=list,
        blank=True,
        help_text='Field names that override parent values.',
    )

    class Meta:
        ordering = ['planning_context__name']

    def __str__(self) -> str:
        return f'Profile for {self.planning_context.name}'

    def clean(self):
        if (
            self.planning_context_id
            and self.planning_context.kind == PlanningContext.Kind.ORG
            and self.planning_context.parent_context_id is None
            and self.inherit_from_parent
        ):
            raise ValidationError({'inherit_from_parent': 'Root org contexts cannot inherit from a parent.'})


class PlanningContextSource(TimestampedModel):
    class UsageType(models.TextChoices):
        ROADMAP = 'roadmap', 'Roadmap'
        STRATEGY = 'strategy', 'Strategy'
        ROLE_REFERENCE = 'role_reference', 'Role reference'
        ORG_STRUCTURE = 'org_structure', 'Org structure'
        EMPLOYEE_CV = 'employee_cv', 'Employee CV'
        OTHER = 'other', 'Other'

    planning_context = models.ForeignKey(
        PlanningContext,
        on_delete=models.CASCADE,
        related_name='context_sources',
    )
    workspace_source = models.ForeignKey(
        'company_intake.WorkspaceSource',
        on_delete=models.CASCADE,
        related_name='planning_context_links',
    )
    usage_type = models.CharField(
        max_length=32,
        choices=UsageType.choices,
        default=UsageType.OTHER,
    )
    inherited_from = models.ForeignKey(
        PlanningContext,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='inherited_source_links',
        help_text='If set, this source was inherited or shadowed from another context.',
    )
    is_active = models.BooleanField(
        default=True,
        help_text='False to exclude an inherited source from this context.',
    )
    include_in_blueprint = models.BooleanField(
        default=True,
        help_text='Whether this source should be used for blueprint generation.',
    )
    include_in_roadmap_analysis = models.BooleanField(
        default=True,
        help_text='Whether this source should be used for roadmap analysis.',
    )

    class Meta:
        ordering = ['planning_context', 'usage_type']
        indexes = [
            models.Index(fields=['planning_context', 'usage_type']),
            models.Index(fields=['planning_context', 'is_active']),
            models.Index(fields=['workspace_source']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['planning_context', 'workspace_source'],
                name='uq_planning_context_source',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.workspace_source} -> {self.planning_context.name}'

    def clean(self):
        if (
            self.planning_context_id
            and self.workspace_source_id
            and self.planning_context.workspace_id
            and self.workspace_source.workspace_id != self.planning_context.workspace_id
        ):
            raise ValidationError(
                {'workspace_source': 'Workspace source must belong to the same workspace as the planning context.'}
            )


class ProjectCapabilityDemand(TimestampedModel):
    class SourceKind(models.TextChoices):
        ROADMAP_ANALYSIS = 'roadmap_analysis', 'From roadmap analysis'
        BLUEPRINT = 'blueprint', 'From blueprint'
        MANUAL = 'manual', 'Manually specified'

    class TimeHorizon(models.TextChoices):
        SHORT = 'short', 'Short term (0-3 months)'
        MEDIUM = 'medium', 'Medium term (3-6 months)'
        LONG = 'long', 'Long term (6-12 months)'

    planning_context = models.ForeignKey(
        PlanningContext,
        on_delete=models.CASCADE,
        related_name='capability_demands',
        help_text='Planning context that owns this demand.',
    )
    project = models.ForeignKey(
        'org_context.Project',
        on_delete=models.CASCADE,
        related_name='capability_demands',
        help_text='Project that needs this capability.',
    )
    skill = models.ForeignKey(
        'org_context.Skill',
        on_delete=models.CASCADE,
        related_name='project_demands',
        help_text='Skill or capability needed by the project.',
    )
    role_family = models.CharField(
        max_length=64,
        blank=True,
        default='',
        help_text='Associated role family, for example backend_engineer.',
    )
    demand_level = models.PositiveSmallIntegerField(
        default=3,
        help_text='Minimum skill level required on a 1-5 scale.',
    )
    fte_demand = models.DecimalField(
        max_digits=4,
        decimal_places=1,
        default=1.0,
        help_text='Full-time equivalent demand for this capability.',
    )
    priority = models.PositiveSmallIntegerField(
        default=2,
        help_text='Priority 1-5, where 1 is highest.',
    )
    time_horizon = models.CharField(
        max_length=16,
        choices=TimeHorizon.choices,
        default=TimeHorizon.MEDIUM,
    )
    source_kind = models.CharField(
        max_length=32,
        choices=SourceKind.choices,
        default=SourceKind.BLUEPRINT,
    )
    initiative_ref = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text='Reference to the roadmap initiative driving this demand.',
    )
    workstream_ref = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text='Reference to the workstream driving this demand.',
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['priority', 'role_family']
        indexes = [
            models.Index(fields=['planning_context', 'priority']),
            models.Index(fields=['planning_context', 'skill']),
            models.Index(fields=['project', 'skill']),
        ]

    def __str__(self) -> str:
        return f'{self.project.name}: {self.skill.display_name_en} (L{self.demand_level}, {self.fte_demand} FTE)'

    def clean(self):
        if (
            self.planning_context
            and self.planning_context.kind == PlanningContext.Kind.PROJECT
            and self.planning_context.project_id
            and self.project_id != self.planning_context.project_id
        ):
            from django.core.exceptions import ValidationError

            raise ValidationError(
                'ProjectCapabilityDemand.project must match planning_context.project when the context is project-scoped.'
            )


class EmployeeCapabilityAvailability(TimestampedModel):
    class ConfidenceLevel(models.TextChoices):
        HIGH = 'high', 'High (assessment confirmed)'
        MEDIUM = 'medium', 'Medium (CV evidence)'
        LOW = 'low', 'Low (inferred)'

    planning_context = models.ForeignKey(
        PlanningContext,
        on_delete=models.CASCADE,
        related_name='capability_availabilities',
        help_text='Planning context that scoped this availability snapshot.',
    )
    employee = models.ForeignKey(
        'org_context.Employee',
        on_delete=models.CASCADE,
        related_name='capability_availabilities',
    )
    skill = models.ForeignKey(
        'org_context.Skill',
        on_delete=models.CASCADE,
        related_name='employee_availabilities',
    )
    current_level = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        default=0,
        help_text='Current skill level on a 1-5 scale.',
    )
    available_fte = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        default=1.0,
        help_text='FTE available for future allocation.',
    )
    confidence = models.CharField(
        max_length=16,
        choices=ConfidenceLevel.choices,
        default=ConfidenceLevel.MEDIUM,
    )
    existing_allocation = models.JSONField(
        default=list,
        blank=True,
        help_text='Current allocations reducing availability.',
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['employee__full_name', 'skill__display_name_en']
        indexes = [
            models.Index(fields=['planning_context', 'employee']),
            models.Index(fields=['planning_context', 'skill']),
            models.Index(fields=['employee', 'skill']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['planning_context', 'employee', 'skill'],
                name='uq_employee_capability_availability',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.employee.full_name}: {self.skill.display_name_en} (L{self.current_level}, {self.available_fte} FTE)'


class AllocationConstraint(TimestampedModel):
    class ConstraintType(models.TextChoices):
        MAX_ALLOCATION = 'max_allocation', 'Max projects per employee'
        MIN_TEAM_SIZE = 'min_team_size', 'Min team size per project'
        REQUIRED_BACKUP = 'required_backup', 'Required skill backup'
        FIXED_ASSIGNMENT = 'fixed_assignment', 'Fixed employee-project assignment'
        MAX_FTE = 'max_fte', 'Max FTE per employee'
        SKILL_COVERAGE = 'skill_coverage', 'Minimum employees per critical skill'

    planning_context = models.ForeignKey(
        PlanningContext,
        on_delete=models.CASCADE,
        related_name='allocation_constraints',
    )
    constraint_type = models.CharField(
        max_length=32,
        choices=ConstraintType.choices,
    )
    constraint_value = models.JSONField(
        default=dict,
        help_text='Constraint parameters. Shape depends on constraint_type.',
    )
    description = models.TextField(
        blank=True,
        default='',
        help_text='Human-readable description for why this constraint exists.',
    )
    is_hard = models.BooleanField(
        default=True,
        help_text='Hard constraints must be satisfied; soft constraints are preferences.',
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['constraint_type']
        indexes = [
            models.Index(fields=['planning_context', 'constraint_type']),
        ]

    def __str__(self) -> str:
        return f'{self.constraint_type}: {self.description[:50]}'


class EscoImportRun(TimestampedModel):
    dataset_version = models.CharField(max_length=64, default='v1.2.1')
    language_code = models.CharField(max_length=16, default='en')
    dataset_path = models.TextField(blank=True, default='')
    status = models.CharField(
        max_length=32,
        choices=EscoImportStatus.choices,
        default=EscoImportStatus.PENDING,
    )
    summary = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['dataset_version', 'language_code']),
            models.Index(fields=['status', 'updated_at']),
        ]

    def __str__(self) -> str:
        return f'ESCO {self.dataset_version} ({self.language_code}) / {self.status}'


class EscoSkillGroup(TimestampedModel):
    concept_uri = models.URLField(max_length=1024, unique=True)
    concept_type = models.CharField(max_length=64, blank=True, default='SkillGroup')
    preferred_label = models.CharField(max_length=512)
    status = models.CharField(max_length=64, blank=True, default='')
    modified_date = models.DateTimeField(null=True, blank=True)
    scope_note = models.TextField(blank=True, default='')
    in_scheme = models.JSONField(default=list, blank=True)
    description = models.TextField(blank=True, default='')
    code = models.CharField(max_length=128, blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['preferred_label']
        indexes = [
            models.Index(fields=['code']),
            models.Index(fields=['preferred_label']),
        ]

    def __str__(self) -> str:
        return self.preferred_label


class EscoSkill(TimestampedModel):
    concept_uri = models.URLField(max_length=1024, unique=True)
    concept_type = models.CharField(max_length=64, blank=True, default='KnowledgeSkillCompetence')
    skill_type = models.CharField(max_length=64, blank=True, default='')
    reuse_level = models.CharField(max_length=64, blank=True, default='')
    preferred_label = models.CharField(max_length=512)
    normalized_preferred_label = models.CharField(max_length=512, blank=True, default='')
    status = models.CharField(max_length=64, blank=True, default='')
    modified_date = models.DateTimeField(null=True, blank=True)
    scope_note = models.TextField(blank=True, default='')
    definition = models.TextField(blank=True, default='')
    description = models.TextField(blank=True, default='')
    in_scheme = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['preferred_label']
        indexes = [
            models.Index(fields=['normalized_preferred_label']),
            models.Index(fields=['skill_type']),
            models.Index(fields=['status']),
        ]

    def __str__(self) -> str:
        return self.preferred_label


class EscoSkillLabel(TimestampedModel):
    class LabelKind(models.TextChoices):
        PREFERRED = 'preferred', 'Preferred'
        ALT = 'alt', 'Alternative'
        HIDDEN = 'hidden', 'Hidden'

    esco_skill = models.ForeignKey(
        EscoSkill,
        on_delete=models.CASCADE,
        related_name='labels',
    )
    label = models.CharField(max_length=512)
    normalized_label = models.CharField(max_length=512, blank=True, default='')
    label_kind = models.CharField(
        max_length=32,
        choices=LabelKind.choices,
        default=LabelKind.ALT,
    )
    language_code = models.CharField(max_length=16, default='en')

    class Meta:
        ordering = ['label']
        unique_together = [('esco_skill', 'label_kind', 'label', 'language_code')]
        indexes = [
            models.Index(fields=['normalized_label']),
            models.Index(fields=['label_kind']),
        ]

    def __str__(self) -> str:
        return self.label


class EscoOccupation(TimestampedModel):
    concept_uri = models.URLField(max_length=1024, unique=True)
    concept_type = models.CharField(max_length=64, blank=True, default='Occupation')
    isco_group = models.CharField(max_length=64, blank=True, default='')
    preferred_label = models.CharField(max_length=512)
    normalized_preferred_label = models.CharField(max_length=512, blank=True, default='')
    status = models.CharField(max_length=64, blank=True, default='')
    modified_date = models.DateTimeField(null=True, blank=True)
    regulated_profession_note = models.TextField(blank=True, default='')
    scope_note = models.TextField(blank=True, default='')
    definition = models.TextField(blank=True, default='')
    description = models.TextField(blank=True, default='')
    in_scheme = models.JSONField(default=list, blank=True)
    code = models.CharField(max_length=64, blank=True, default='')
    nace_code = models.TextField(blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['preferred_label']
        indexes = [
            models.Index(fields=['normalized_preferred_label']),
            models.Index(fields=['isco_group']),
            models.Index(fields=['code']),
        ]

    def __str__(self) -> str:
        return self.preferred_label


class EscoOccupationLabel(TimestampedModel):
    class LabelKind(models.TextChoices):
        PREFERRED = 'preferred', 'Preferred'
        ALT = 'alt', 'Alternative'
        HIDDEN = 'hidden', 'Hidden'

    esco_occupation = models.ForeignKey(
        EscoOccupation,
        on_delete=models.CASCADE,
        related_name='labels',
    )
    label = models.CharField(max_length=512)
    normalized_label = models.CharField(max_length=512, blank=True, default='')
    label_kind = models.CharField(
        max_length=32,
        choices=LabelKind.choices,
        default=LabelKind.ALT,
    )
    language_code = models.CharField(max_length=16, default='en')

    class Meta:
        ordering = ['label']
        unique_together = [('esco_occupation', 'label_kind', 'label', 'language_code')]
        indexes = [
            models.Index(fields=['normalized_label']),
            models.Index(fields=['label_kind']),
        ]

    def __str__(self) -> str:
        return self.label


class EscoSkillRelation(TimestampedModel):
    original_skill = models.ForeignKey(
        EscoSkill,
        on_delete=models.CASCADE,
        related_name='outgoing_skill_relations',
    )
    related_skill = models.ForeignKey(
        EscoSkill,
        on_delete=models.CASCADE,
        related_name='incoming_skill_relations',
    )
    original_skill_type = models.CharField(max_length=64, blank=True, default='')
    relation_type = models.CharField(max_length=64)
    related_skill_type = models.CharField(max_length=64, blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['relation_type', 'original_skill_id']
        unique_together = [('original_skill', 'related_skill', 'relation_type')]
        indexes = [
            models.Index(fields=['relation_type']),
        ]

    def __str__(self) -> str:
        return f'{self.original_skill} -> {self.related_skill} ({self.relation_type})'


class EscoOccupationSkillRelation(TimestampedModel):
    occupation = models.ForeignKey(
        EscoOccupation,
        on_delete=models.CASCADE,
        related_name='skill_relations',
    )
    skill = models.ForeignKey(
        EscoSkill,
        on_delete=models.CASCADE,
        related_name='occupation_relations',
    )
    relation_type = models.CharField(max_length=64)
    skill_type = models.CharField(max_length=64, blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['occupation_id', 'relation_type', 'skill_id']
        unique_together = [('occupation', 'skill', 'relation_type')]
        indexes = [
            models.Index(fields=['relation_type']),
            models.Index(fields=['skill_type']),
        ]

    def __str__(self) -> str:
        return f'{self.occupation} / {self.skill} ({self.relation_type})'


class EscoSkillBroaderRelation(TimestampedModel):
    concept_type = models.CharField(max_length=64, blank=True, default='')
    concept_uri = models.URLField(max_length=1024)
    concept_label = models.CharField(max_length=512, blank=True, default='')
    broader_type = models.CharField(max_length=64, blank=True, default='')
    broader_uri = models.URLField(max_length=1024)
    broader_label = models.CharField(max_length=512, blank=True, default='')
    esco_skill = models.ForeignKey(
        EscoSkill,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='broader_relations',
    )
    esco_skill_group = models.ForeignKey(
        EscoSkillGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='child_relations',
    )
    broader_skill = models.ForeignKey(
        EscoSkill,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='narrower_relations',
    )
    broader_skill_group = models.ForeignKey(
        EscoSkillGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='broader_group_relations',
    )

    class Meta:
        ordering = ['concept_label', 'broader_label']
        unique_together = [('concept_uri', 'broader_uri')]
        indexes = [
            models.Index(fields=['concept_uri']),
            models.Index(fields=['broader_uri']),
        ]

    def __str__(self) -> str:
        return f'{self.concept_label or self.concept_uri} -> {self.broader_label or self.broader_uri}'


class EscoOccupationBroaderRelation(TimestampedModel):
    concept_type = models.CharField(max_length=64, blank=True, default='')
    concept_uri = models.URLField(max_length=1024)
    concept_label = models.CharField(max_length=512, blank=True, default='')
    broader_type = models.CharField(max_length=64, blank=True, default='')
    broader_uri = models.URLField(max_length=1024)
    broader_label = models.CharField(max_length=512, blank=True, default='')
    esco_occupation = models.ForeignKey(
        EscoOccupation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='broader_relations',
    )
    broader_occupation = models.ForeignKey(
        EscoOccupation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='narrower_relations',
    )

    class Meta:
        ordering = ['concept_label', 'broader_label']
        unique_together = [('concept_uri', 'broader_uri')]
        indexes = [
            models.Index(fields=['concept_uri']),
            models.Index(fields=['broader_uri']),
        ]

    def __str__(self) -> str:
        return f'{self.concept_label or self.concept_uri} -> {self.broader_label or self.broader_uri}'


class EscoSkillCollectionMembership(TimestampedModel):
    esco_skill = models.ForeignKey(
        EscoSkill,
        on_delete=models.CASCADE,
        related_name='collection_memberships',
    )
    collection_key = models.CharField(max_length=64)
    collection_label = models.CharField(max_length=255, blank=True, default='')
    broader_concept_uris = models.JSONField(default=list, blank=True)
    broader_concept_labels = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['collection_key', 'esco_skill_id']
        unique_together = [('esco_skill', 'collection_key')]
        indexes = [
            models.Index(fields=['collection_key']),
        ]

    def __str__(self) -> str:
        return f'{self.esco_skill} / {self.collection_key}'


class EscoOccupationCollectionMembership(TimestampedModel):
    esco_occupation = models.ForeignKey(
        EscoOccupation,
        on_delete=models.CASCADE,
        related_name='collection_memberships',
    )
    collection_key = models.CharField(max_length=64)
    collection_label = models.CharField(max_length=255, blank=True, default='')
    broader_concept_uris = models.JSONField(default=list, blank=True)
    broader_concept_labels = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['collection_key', 'esco_occupation_id']
        unique_together = [('esco_occupation', 'collection_key')]
        indexes = [
            models.Index(fields=['collection_key']),
        ]

    def __str__(self) -> str:
        return f'{self.esco_occupation} / {self.collection_key}'


class EscoConceptScheme(TimestampedModel):
    concept_type = models.CharField(max_length=64, blank=True, default='ConceptScheme')
    concept_scheme_uri = models.URLField(max_length=1024, unique=True)
    preferred_label = models.CharField(max_length=255, blank=True, default='')
    title = models.CharField(max_length=255, blank=True, default='')
    status = models.CharField(max_length=64, blank=True, default='')
    description = models.TextField(blank=True, default='')
    has_top_concept = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ['preferred_label', 'title']

    def __str__(self) -> str:
        return self.preferred_label or self.title or self.concept_scheme_uri


class EscoDictionaryEntry(TimestampedModel):
    filename = models.CharField(max_length=64)
    data_header = models.CharField(max_length=255, blank=True, default='')
    property_name = models.CharField(max_length=255, blank=True, default='')
    description = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['filename', 'data_header']
        unique_together = [('filename', 'data_header', 'property_name')]

    def __str__(self) -> str:
        return f'{self.filename} / {self.data_header}'


class EscoIscoGroup(TimestampedModel):
    concept_type = models.CharField(max_length=64, blank=True, default='ISCOGroup')
    concept_uri = models.URLField(max_length=1024, unique=True)
    code = models.CharField(max_length=32, blank=True, default='')
    preferred_label = models.CharField(max_length=255)
    status = models.CharField(max_length=64, blank=True, default='')
    in_scheme = models.JSONField(default=list, blank=True)
    description = models.TextField(blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['code', 'preferred_label']
        indexes = [
            models.Index(fields=['code']),
        ]

    def __str__(self) -> str:
        return self.preferred_label


class EscoGreenOccupationShare(TimestampedModel):
    concept_type = models.CharField(max_length=64, blank=True, default='')
    concept_uri = models.URLField(max_length=1024)
    code = models.CharField(max_length=32, blank=True, default='')
    preferred_label = models.CharField(max_length=255)
    green_share = models.DecimalField(max_digits=24, decimal_places=20, default=0)
    esco_occupation = models.ForeignKey(
        EscoOccupation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='green_share_rows',
    )
    isco_group = models.ForeignKey(
        EscoIscoGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='green_share_rows',
    )

    class Meta:
        ordering = ['-green_share', 'preferred_label']
        unique_together = [('concept_uri', 'code')]
        indexes = [
            models.Index(fields=['code']),
        ]

    def __str__(self) -> str:
        return f'{self.preferred_label} ({self.green_share})'


class EscoSkillHierarchyPath(TimestampedModel):
    level_0_uri = models.URLField(max_length=1024, blank=True, default='')
    level_0_preferred_term = models.CharField(max_length=512, blank=True, default='')
    level_1_uri = models.URLField(max_length=1024, blank=True, default='')
    level_1_preferred_term = models.CharField(max_length=512, blank=True, default='')
    level_2_uri = models.URLField(max_length=1024, blank=True, default='')
    level_2_preferred_term = models.CharField(max_length=512, blank=True, default='')
    level_3_uri = models.URLField(max_length=1024, blank=True, default='')
    level_3_preferred_term = models.CharField(max_length=512, blank=True, default='')
    description = models.TextField(blank=True, default='')
    scope_note = models.TextField(blank=True, default='')
    level_0_code = models.CharField(max_length=64, blank=True, default='')
    level_1_code = models.CharField(max_length=64, blank=True, default='')
    level_2_code = models.CharField(max_length=64, blank=True, default='')
    level_3_code = models.CharField(max_length=64, blank=True, default='')

    class Meta:
        ordering = ['level_0_code', 'level_1_code', 'level_2_code', 'level_3_code']
        indexes = [
            models.Index(fields=['level_3_uri']),
            models.Index(fields=['level_2_uri']),
        ]

    def __str__(self) -> str:
        return ' / '.join(
            part
            for part in [
                self.level_0_preferred_term,
                self.level_1_preferred_term,
                self.level_2_preferred_term,
                self.level_3_preferred_term,
            ]
            if part
        ) or 'ESCO skill hierarchy path'


class CatalogOverrideStatus(models.TextChoices):
    SUGGESTED = 'suggested', 'Suggested'
    APPROVED = 'approved', 'Approved'
    REJECTED = 'rejected', 'Rejected'


class CatalogReviewStatus(models.TextChoices):
    OPEN = 'open', 'Open'
    RESOLVED = 'resolved', 'Resolved'
    IGNORED = 'ignored', 'Ignored'


class SkillResolutionOverride(TimestampedModel):
    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='skill_resolution_overrides',
        null=True,
        blank=True,
    )
    raw_term = models.CharField(max_length=255)
    normalized_term = models.CharField(max_length=255)
    canonical_key = models.CharField(max_length=255)
    display_name_en = models.CharField(max_length=255)
    display_name_ru = models.CharField(max_length=255, blank=True, default='')
    esco_skill = models.ForeignKey(
        EscoSkill,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='resolution_overrides',
    )
    aliases = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=32,
        choices=CatalogOverrideStatus.choices,
        default=CatalogOverrideStatus.SUGGESTED,
    )
    source = models.CharField(max_length=64, blank=True, default='')
    notes = models.TextField(blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['normalized_term', 'workspace_id']
        indexes = [
            models.Index(fields=['workspace', 'normalized_term']),
            models.Index(fields=['status', 'normalized_term']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['normalized_term'],
                condition=models.Q(workspace__isnull=True),
                name='uq_skill_resolution_override_global_term',
            ),
            models.UniqueConstraint(
                fields=['workspace', 'normalized_term'],
                condition=models.Q(workspace__isnull=False),
                name='uq_skill_resolution_override_workspace_term',
            ),
        ]

    def __str__(self) -> str:
        return self.display_name_en or self.raw_term


class OccupationResolutionOverride(TimestampedModel):
    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='occupation_resolution_overrides',
        null=True,
        blank=True,
    )
    raw_term = models.CharField(max_length=255)
    normalized_term = models.CharField(max_length=255)
    occupation_key = models.CharField(max_length=255, blank=True, default='')
    occupation_name_en = models.CharField(max_length=255, blank=True, default='')
    aliases = models.JSONField(default=list, blank=True)
    esco_occupation = models.ForeignKey(
        EscoOccupation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='resolution_overrides',
    )
    status = models.CharField(
        max_length=32,
        choices=CatalogOverrideStatus.choices,
        default=CatalogOverrideStatus.SUGGESTED,
    )
    source = models.CharField(max_length=64, blank=True, default='')
    notes = models.TextField(blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['normalized_term', 'workspace_id']
        indexes = [
            models.Index(fields=['workspace', 'normalized_term']),
            models.Index(fields=['status', 'normalized_term']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['normalized_term'],
                condition=models.Q(workspace__isnull=True),
                name='uq_occupation_resolution_override_global_term',
            ),
            models.UniqueConstraint(
                fields=['workspace', 'normalized_term'],
                condition=models.Q(workspace__isnull=False),
                name='uq_occupation_resolution_override_workspace_term',
            ),
        ]

    def __str__(self) -> str:
        return self.occupation_name_en or self.raw_term


class CatalogResolutionReviewItem(TimestampedModel):
    class TermKind(models.TextChoices):
        SKILL = 'skill', 'Skill'
        OCCUPATION = 'occupation', 'Occupation'

    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='catalog_resolution_review_items',
        null=True,
        blank=True,
    )
    term_kind = models.CharField(max_length=32, choices=TermKind.choices)
    raw_term = models.CharField(max_length=255)
    normalized_term = models.CharField(max_length=255)
    status = models.CharField(
        max_length=32,
        choices=CatalogReviewStatus.choices,
        default=CatalogReviewStatus.OPEN,
    )
    seen_count = models.PositiveIntegerField(default=1)
    last_seen_at = models.DateTimeField(auto_now=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['term_kind', 'normalized_term']
        indexes = [
            models.Index(fields=['workspace', 'term_kind', 'normalized_term']),
            models.Index(fields=['status', 'term_kind']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['term_kind', 'normalized_term'],
                condition=models.Q(workspace__isnull=True),
                name='uq_catalog_review_item_global_term',
            ),
            models.UniqueConstraint(
                fields=['workspace', 'term_kind', 'normalized_term'],
                condition=models.Q(workspace__isnull=False),
                name='uq_catalog_review_item_workspace_term',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.term_kind}: {self.raw_term}'


class Employee(TimestampedModel):
    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='employees',
    )
    external_employee_id = models.CharField(max_length=128, blank=True, default='')
    full_name = models.CharField(max_length=255)
    email = models.EmailField(blank=True, default='')
    current_title = models.CharField(max_length=255, blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)
    source = models.ForeignKey(
        'company_intake.WorkspaceSource',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='employees_owned',
    )

    class Meta:
        ordering = ['full_name']
        indexes = [
            models.Index(fields=['workspace', 'full_name']),
            models.Index(fields=['workspace', 'external_employee_id']),
        ]

    def __str__(self) -> str:
        return self.full_name


class OrgUnit(TimestampedModel):
    class UnitKind(models.TextChoices):
        DEPARTMENT = 'department', 'Department'
        FUNCTIONAL_TEAM = 'functional_team', 'Functional team'
        PRODUCT_TEAM = 'product_team', 'Product team'
        OTHER = 'other', 'Other'

    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='org_units',
    )
    name = models.CharField(max_length=255)
    unit_kind = models.CharField(
        max_length=32,
        choices=UnitKind.choices,
        default=UnitKind.DEPARTMENT,
    )
    metadata = models.JSONField(default=dict, blank=True)
    source = models.ForeignKey(
        'company_intake.WorkspaceSource',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='org_units_owned',
    )

    class Meta:
        ordering = ['name']
        unique_together = [('workspace', 'name', 'unit_kind')]
        indexes = [
            models.Index(fields=['workspace', 'unit_kind']),
        ]

    def __str__(self) -> str:
        return self.name


class Project(TimestampedModel):
    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='projects',
    )
    name = models.CharField(max_length=255)
    metadata = models.JSONField(default=dict, blank=True)
    source = models.ForeignKey(
        'company_intake.WorkspaceSource',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='projects_owned',
    )

    class Meta:
        ordering = ['name']
        unique_together = [('workspace', 'name')]
        indexes = [
            models.Index(fields=['workspace', 'name']),
        ]

    def __str__(self) -> str:
        return self.name


class ReportingLine(TimestampedModel):
    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='reporting_lines',
    )
    manager = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='direct_reports_links',
    )
    report = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='managers_links',
    )
    source = models.ForeignKey(
        'company_intake.WorkspaceSource',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reporting_lines',
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = [('workspace', 'manager', 'report')]
        indexes = [
            models.Index(fields=['workspace', 'manager']),
            models.Index(fields=['workspace', 'report']),
        ]

    def __str__(self) -> str:
        return f'{self.manager} -> {self.report}'


class EmployeeOrgAssignment(TimestampedModel):
    class AssignmentKind(models.TextChoices):
        HOME = 'home', 'Home department'
        FUNCTIONAL = 'functional', 'Functional'
        PRODUCT = 'product', 'Product'
        OTHER = 'other', 'Other'

    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='employee_org_assignments',
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='org_assignments',
    )
    org_unit = models.ForeignKey(
        OrgUnit,
        on_delete=models.CASCADE,
        related_name='employee_assignments',
    )
    assignment_kind = models.CharField(
        max_length=32,
        choices=AssignmentKind.choices,
        default=AssignmentKind.HOME,
    )
    is_primary = models.BooleanField(default=False)
    title_override = models.CharField(max_length=255, blank=True, default='')
    source = models.ForeignKey(
        'company_intake.WorkspaceSource',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='employee_org_assignments',
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = [('employee', 'org_unit', 'assignment_kind')]
        indexes = [
            models.Index(fields=['workspace', 'assignment_kind']),
            models.Index(fields=['workspace', 'employee']),
        ]

    def __str__(self) -> str:
        return f'{self.employee} / {self.org_unit} ({self.assignment_kind})'


class EmployeeProjectAssignment(TimestampedModel):
    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='employee_project_assignments',
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='project_assignments',
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='employee_assignments',
    )
    role_label = models.CharField(max_length=255, blank=True, default='')
    allocation_percent = models.PositiveSmallIntegerField(null=True, blank=True)
    source = models.ForeignKey(
        'company_intake.WorkspaceSource',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='employee_project_assignments',
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = [('employee', 'project', 'role_label')]
        indexes = [
            models.Index(fields=['workspace', 'employee']),
            models.Index(fields=['workspace', 'project']),
        ]

    def __str__(self) -> str:
        return f'{self.employee} / {self.project}'


class RoleProfile(TimestampedModel):
    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='role_profiles',
    )
    blueprint_run = models.ForeignKey(
        'skill_blueprint.SkillBlueprintRun',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='role_profiles',
    )
    name = models.CharField(max_length=255)
    family = models.CharField(max_length=255, blank=True, default='')
    seniority = models.CharField(max_length=64, blank=True, default='')
    canonical_occupation_key = models.CharField(max_length=255, blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['workspace', 'family']),
            models.Index(fields=['workspace', 'blueprint_run']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['workspace', 'blueprint_run', 'name', 'seniority'],
                condition=models.Q(blueprint_run__isnull=False),
                name='org_ctx_roleprofile_run_unique',
            ),
            models.UniqueConstraint(
                fields=['workspace', 'name', 'seniority'],
                condition=models.Q(blueprint_run__isnull=True),
                name='org_ctx_roleprofile_published_unique',
            ),
        ]

    def __str__(self) -> str:
        return self.name


class Skill(TimestampedModel):
    class ResolutionStatus(models.TextChoices):
        RESOLVED = 'resolved', 'Resolved'
        PENDING_REVIEW = 'pending_review', 'Pending review'
        REJECTED = 'rejected', 'Rejected'

    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='skills',
        null=True,
        blank=True,
    )
    canonical_key = models.CharField(max_length=255)
    display_name_en = models.CharField(max_length=255)
    display_name_ru = models.CharField(max_length=255, blank=True, default='')
    source = models.CharField(max_length=64, blank=True, default='')
    esco_skill = models.ForeignKey(
        EscoSkill,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='workspace_skills',
    )
    metadata = models.JSONField(default=dict, blank=True)
    resolution_status = models.CharField(
        max_length=32,
        choices=ResolutionStatus.choices,
        default=ResolutionStatus.RESOLVED,
        db_index=True,
    )
    is_operator_confirmed = models.BooleanField(default=False)
    source_terms = models.JSONField(
        default=list,
        blank=True,
        help_text='Raw terms from CV extraction that created this provisional skill. Aids merge review.',
    )

    class Meta:
        ordering = ['display_name_en']
        unique_together = [('workspace', 'canonical_key')]
        indexes = [
            models.Index(fields=['canonical_key']),
        ]

    def __str__(self) -> str:
        return self.display_name_en


class SkillAlias(TimestampedModel):
    skill = models.ForeignKey(
        Skill,
        on_delete=models.CASCADE,
        related_name='aliases',
    )
    alias = models.CharField(max_length=255)
    language_code = models.CharField(max_length=16, blank=True, default='')

    class Meta:
        ordering = ['alias']
        unique_together = [('skill', 'alias', 'language_code')]
        indexes = [
            models.Index(fields=['alias']),
        ]

    def __str__(self) -> str:
        return self.alias


class OccupationMapping(TimestampedModel):
    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='occupation_mappings',
    )
    role_profile = models.ForeignKey(
        RoleProfile,
        on_delete=models.CASCADE,
        related_name='occupation_mappings',
    )
    occupation_key = models.CharField(max_length=255)
    occupation_name_en = models.CharField(max_length=255)
    occupation_name_ru = models.CharField(max_length=255, blank=True, default='')
    esco_occupation = models.ForeignKey(
        EscoOccupation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='workspace_mappings',
    )
    match_score = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-match_score']
        indexes = [
            models.Index(fields=['workspace', 'occupation_key']),
        ]

    def __str__(self) -> str:
        return self.occupation_name_en


class RoleSkillRequirement(TimestampedModel):
    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='role_skill_requirements',
    )
    role_profile = models.ForeignKey(
        RoleProfile,
        on_delete=models.CASCADE,
        related_name='skill_requirements',
    )
    skill = models.ForeignKey(
        Skill,
        on_delete=models.CASCADE,
        related_name='role_requirements',
    )
    target_level = models.PositiveSmallIntegerField(default=0)
    priority = models.PositiveSmallIntegerField(default=0)
    is_required = models.BooleanField(default=True)
    source_kind = models.CharField(max_length=64, blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = [('role_profile', 'skill')]
        indexes = [
            models.Index(fields=['workspace', 'target_level']),
        ]

    def __str__(self) -> str:
        return f'{self.role_profile} / {self.skill}'


class EmployeeSkillEvidence(TimestampedModel):
    class OperatorAction(models.TextChoices):
        ACCEPTED = 'accepted', 'Accepted'
        REJECTED = 'rejected', 'Rejected'
        MERGED = 'merged', 'Merged'

    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='employee_skill_evidence',
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='skill_evidence',
    )
    skill = models.ForeignKey(
        Skill,
        on_delete=models.CASCADE,
        related_name='employee_evidence',
    )
    source_kind = models.CharField(max_length=64)
    source = models.ForeignKey(
        'company_intake.WorkspaceSource',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='skill_evidence',
    )
    current_level = models.DecimalField(max_digits=4, decimal_places=2, default=0)
    confidence = models.DecimalField(max_digits=4, decimal_places=2, default=0)
    weight = models.DecimalField(max_digits=4, decimal_places=2, default=0)
    evidence_text = models.TextField(blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)
    is_operator_confirmed = models.BooleanField(
        default=False,
        help_text='True after an operator explicitly accepted this evidence row.',
    )
    operator_action = models.CharField(
        max_length=32,
        blank=True,
        default='',
        choices=[('', '')] + list(OperatorAction.choices),
        help_text='Last operator action on this evidence row.',
    )
    operator_note = models.TextField(
        blank=True,
        default='',
        help_text='Free-text note from operator review.',
    )
    assessment_cycle = models.ForeignKey(
        'employee_assessment.AssessmentCycle',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='evidence_rows',
        help_text='The assessment cycle that produced this evidence (self_assessment only).',
    )
    assessment_pack = models.ForeignKey(
        'employee_assessment.EmployeeAssessmentPack',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='evidence_rows',
        help_text='The assessment pack that produced this evidence (self_assessment only).',
    )

    class Meta:
        indexes = [
            models.Index(fields=['workspace', 'employee']),
            models.Index(fields=['workspace', 'skill']),
            models.Index(fields=['source_kind']),
            models.Index(fields=['workspace', 'source_kind', 'assessment_cycle']),
        ]

    def __str__(self) -> str:
        return f'{self.employee} / {self.skill} / {self.source_kind}'


class SkillReviewDecision(TimestampedModel):
    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='skill_review_decisions',
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='skill_review_decisions',
    )
    skill_canonical_key = models.CharField(
        max_length=255,
        help_text='Canonical key of the skill being reviewed.',
    )
    action = models.CharField(
        max_length=32,
        choices=EmployeeSkillEvidence.OperatorAction.choices,
    )
    merge_target_skill_uuid = models.UUIDField(
        null=True,
        blank=True,
        help_text='Target skill UUID if action is merged.',
    )
    note = models.TextField(blank=True, default='')
    reviewed_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        indexes = [
            models.Index(fields=['workspace', 'employee']),
            models.Index(fields=['workspace', 'skill_canonical_key']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['workspace', 'employee', 'skill_canonical_key'],
                name='uq_skill_review_decision_per_employee',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.employee} / {self.skill_canonical_key} ({self.action})'


class EmployeeCVProfile(TimestampedModel):
    class Status(models.TextChoices):
        MATCHED = 'matched', 'Matched'
        AMBIGUOUS = 'ambiguous', 'Ambiguous'
        UNMATCHED = 'unmatched', 'Unmatched'
        LOW_CONFIDENCE_MATCH = 'low_confidence_match', 'Low confidence match'
        EXTRACTION_FAILED = 'extraction_failed', 'Extraction failed'

    class EvidenceQuality(models.TextChoices):
        STRONG = 'strong', 'Strong'
        USABLE = 'usable', 'Usable'
        SPARSE = 'sparse', 'Sparse'
        EMPTY = 'empty', 'Empty'
        FAILED = 'failed', 'Failed'

    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='employee_cv_profiles',
    )
    source = models.OneToOneField(
        'company_intake.WorkspaceSource',
        on_delete=models.CASCADE,
        related_name='cv_profile',
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cv_profiles',
    )
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.UNMATCHED,
    )
    evidence_quality = models.CharField(
        max_length=16,
        choices=EvidenceQuality.choices,
        default=EvidenceQuality.EMPTY,
    )
    match_confidence = models.DecimalField(max_digits=4, decimal_places=2, default=0)
    matched_by = models.CharField(max_length=64, blank=True, default='')
    language_code = models.CharField(max_length=16, blank=True, default='')
    input_revision = models.CharField(max_length=64, blank=True, default='')
    active_vector_generation_id = models.CharField(max_length=64, blank=True, default='')
    headline = models.CharField(max_length=255, blank=True, default='')
    current_role = models.CharField(max_length=255, blank=True, default='')
    seniority = models.CharField(max_length=64, blank=True, default='')
    role_family = models.CharField(max_length=255, blank=True, default='')
    extracted_payload = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['workspace', 'status']),
            models.Index(fields=['workspace', 'employee']),
            models.Index(fields=['workspace', 'evidence_quality']),
            models.Index(fields=['workspace', 'input_revision']),
        ]

    def __str__(self) -> str:
        return f'{self.workspace.slug} / CV / {self.source_id}'


class EmployeeCVMatchCandidate(TimestampedModel):
    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='employee_cv_match_candidates',
    )
    profile = models.ForeignKey(
        EmployeeCVProfile,
        on_delete=models.CASCADE,
        related_name='candidate_matches',
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='cv_match_candidates',
    )
    rank = models.PositiveSmallIntegerField(default=1)
    score = models.DecimalField(max_digits=5, decimal_places=4, default=0)
    name_score = models.DecimalField(max_digits=5, decimal_places=4, default=0)
    title_score = models.DecimalField(max_digits=5, decimal_places=4, default=0)
    department_score = models.DecimalField(max_digits=5, decimal_places=4, default=0)
    exact_name_match = models.BooleanField(default=False)
    email_match = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['rank', '-score']
        unique_together = [('profile', 'employee')]
        indexes = [
            models.Index(fields=['workspace', 'employee']),
            models.Index(fields=['workspace', 'profile']),
        ]

    def __str__(self) -> str:
        return f'{self.profile_id} / {self.employee_id} / {self.score}'


class EmployeeRoleMatch(TimestampedModel):
    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='employee_role_matches',
    )
    planning_context = models.ForeignKey(
        'org_context.PlanningContext',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='employee_role_matches',
        help_text='Planning context this match was computed for.',
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='role_matches',
    )
    role_profile = models.ForeignKey(
        RoleProfile,
        on_delete=models.CASCADE,
        related_name='employee_matches',
    )
    source_kind = models.CharField(max_length=64, default='blueprint')
    fit_score = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    rationale = models.TextField(blank=True, default='')
    related_initiatives = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = [('employee', 'role_profile', 'source_kind')]
        indexes = [
            models.Index(fields=['workspace', 'employee']),
            models.Index(fields=['workspace', 'role_profile']),
            models.Index(fields=['workspace', 'source_kind']),
            models.Index(fields=['workspace', 'planning_context']),
        ]

    def __str__(self) -> str:
        return f'{self.employee} / {self.role_profile} ({self.fit_score})'
