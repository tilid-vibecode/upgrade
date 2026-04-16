# Migration 3.1 — PlanningContext Schema

## Problem statement

The current workforce-planning pipeline is scoped entirely to `IntakeWorkspace`. Every source, every employee, every blueprint, every assessment — all live under one workspace. This means:

- One workspace = one roadmap = one blueprint = one planning boundary
- An organization with multiple projects cannot have project-specific blueprints
- An organization cannot inherit shared company context across projects
- The same employee pool cannot be evaluated against different project blueprints without data duplication

The product direction requires:
- **Organization** owns the team (employees, org units, skills)
- **Projects** are planning scopes with their own roadmaps and blueprints
- **Context inheritance** — project inherits org company profile but can override tech stack
- **Future staffing** — project demand vs employee availability across projects

## Goal

Introduce three new first-class models: `PlanningContext`, `ContextProfile`, and `PlanningContextSource`. This migration is schema-only — no service changes, no API changes, no behavioral changes.

## Prerequisites

- Migration 1.4 (workflow ordering alignment — the stage structure is clarified)

## Foundation: IntakeWorkspace.organization FK

**Critical prerequisite:** Before PlanningContext can meaningfully reference an organization, the workspace itself must be linkable to one. Without this, `PlanningContext.organization` will always be null and the system remains "workspace-as-org" in practice.

### File: `company_intake/models.py` — `IntakeWorkspace`

Add a nullable FK to Organization:

```python
class IntakeWorkspace(TimestampedModel):
    # ... existing fields ...
    organization = models.ForeignKey(
        'organization.Organization',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='workspaces',
        help_text='Organization this workspace belongs to. Null for legacy/unlinked workspaces.',
    )
```

This is nullable because existing workspaces predate the organization linkage. Backfill is best-effort: if a workspace can be matched to an organization (by operator membership or metadata), link it. Otherwise leave null.

**This is explicitly an interim prototype compromise.** Full org-normalized ownership (where employees, skills, and projects belong to the organization rather than the workspace) is a future step beyond these migrations.

## New models

### File: `org_context/models.py` — add after `RoadmapAnalysisRun`

#### Model 1: `PlanningContext`

```python
class PlanningContext(TimestampedModel):
    """
    A planning scope within an organization. Can represent:
    - org: organization-wide baseline planning
    - project: project-specific planning (inherits from org)
    - scenario: what-if scenario (inherits from org or project)

    PlanningContexts form a tree via parent_context:
    Organization baseline (kind=org)
      +-- Project A (kind=project, parent=org baseline)
      +-- Project B (kind=project, parent=org baseline)
           +-- Scenario B1 (kind=scenario, parent=Project B)
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
        help_text='Project this context is scoped to (for kind=project).',
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
            # Structural check constraints
            models.CheckConstraint(
                condition=(
                    # At least one of workspace or organization must be set
                    models.Q(workspace__isnull=False) | models.Q(organization__isnull=False)
                ),
                name='planning_context_has_scope',
            ),
            models.CheckConstraint(
                condition=(
                    # ORG contexts must NOT have a parent_context
                    models.Q(kind='org', parent_context__isnull=True)
                    | ~models.Q(kind='org')
                ),
                name='planning_context_org_no_parent',
            ),
            models.CheckConstraint(
                condition=(
                    # PROJECT contexts must have a project FK
                    models.Q(kind='project', project__isnull=False)
                    | ~models.Q(kind='project')
                ),
                name='planning_context_project_requires_project',
            ),
            models.CheckConstraint(
                condition=(
                    # SCENARIO contexts must have a parent_context
                    models.Q(kind='scenario', parent_context__isnull=False)
                    | ~models.Q(kind='scenario')
                ),
                name='planning_context_scenario_requires_parent',
            ),
        ]

    def __str__(self):
        return f'{self.name} ({self.kind})'
```

#### Model 2: `ContextProfile`

```python
class ContextProfile(TimestampedModel):
    """
    Override / inheritance layer for context attributes.
    Each PlanningContext has one ContextProfile that defines
    what company context is effective for that scope.

    Inheritance rules:
    - If inherit_from_parent is True and a field is NOT in override_fields,
      the value is inherited from the parent context's profile.
    - If a field IS in override_fields, this profile's value is used.
    - Resolution walks up the parent chain until a value is found.
    """

    planning_context = models.OneToOneField(
        PlanningContext,
        on_delete=models.CASCADE,
        related_name='profile',
    )
    company_profile = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            'Company profile fields. For org context: full company profile. '
            'For project context: overrides only (e.g., different target market).'
        ),
    )
    tech_stack = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            'Technology stack. For org context: full stack. '
            'For project context: additions/removals relative to org stack.'
        ),
    )
    tech_stack_remove = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            'Technologies to REMOVE from the inherited stack (for project/scenario contexts). '
            'Example: parent has ["Java", "Spring"], child sets tech_stack_remove=["Java"] '
            'to indicate Java is not used in this project context.'
        ),
    )
    constraints = models.JSONField(
        default=list,
        blank=True,
        help_text='Planning constraints (e.g., budget limits, timeline, team size caps).',
    )
    growth_goals = models.JSONField(
        default=list,
        blank=True,
        help_text='Growth and strategic goals relevant to this planning scope.',
    )
    inherit_from_parent = models.BooleanField(
        default=True,
        help_text='Whether to inherit unoverridden fields from parent context.',
    )
    override_fields = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            'List of field names that override parent values. '
            'Fields not listed here are inherited. '
            'Example: ["tech_stack", "constraints"]'
        ),
    )

    class Meta:
        ordering = ['planning_context__name']

    def __str__(self):
        return f'Profile for {self.planning_context.name}'
```

#### Model 3: `PlanningContextSource`

```python
class PlanningContextSource(TimestampedModel):
    """
    Links a WorkspaceSource to a PlanningContext with usage metadata.

    A single uploaded source can be:
    - Attached to the org baseline context
    - Inherited by several project contexts
    - Excluded from one scenario context
    - Marked as primary in one context but secondary in another

    This decouples physical source ownership (WorkspaceSource)
    from planning-scope usage (PlanningContextSource).
    """

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
        help_text='If set, this source was inherited from another context.',
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

    def __str__(self):
        return f'{self.workspace_source} -> {self.planning_context.name}'
```

### Source inheritance precedence rules

Source inheritance follows nearest-context-wins semantics. When resolving effective sources for a context:

1. Collect all `PlanningContextSource` records from the context and all ancestors
2. Group by `workspace_source_id`
3. For each group, prefer the record from the nearest context (child > parent > grandparent)
4. A child-level inactive link (`is_active=False`) SHADOWS the parent-level active link for the same source
5. This prevents inherited sources from being "undeletable" at the project level

This is documented here so it informs both the model design and the resolution algorithm in migration 3.2.

### Django migrations

Because the `IntakeWorkspace.organization` FK lives in the `company_intake` app and the PlanningContext models live in `org_context`, these must be **two separate migration files**:

1. **`company_intake/migrations/0008_intakeworkspace_organization.py`**:
   - `AddField('intakeworkspace', 'organization', FK to 'organization.Organization', null=True, blank=True)`

2. **`org_context/migrations/0020_planning_context.py`**:
   - `CreateModel('PlanningContext', ...)`
   - `CreateModel('ContextProfile', ...)`
   - `CreateModel('PlanningContextSource', ...)`
   - Dependencies: `('company_intake', '0008_intakeworkspace_organization')`

Do not put cross-app `AddField` operations in a single migration file — Django requires each migration to live in its own app.

## Context inheritance resolution

This is not implemented in this migration (schema only), but documenting the intended resolution algorithm for future service implementation:

```python
def resolve_effective_profile(planning_context: PlanningContext) -> dict:
    """
    Resolve the effective profile for a planning context by walking
    up the parent chain and merging overrides.

    Returns a dict with resolved values for:
    - company_profile
    - tech_stack
    - constraints
    - growth_goals
    """
    chain = []
    current = planning_context
    while current is not None:
        chain.append(current)
        current = current.parent_context

    # Start with the root (org) context
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

        if not profile.inherit_from_parent:
            # This context doesn't inherit — use its values directly
            effective = {
                'company_profile': profile.company_profile or {},
                'tech_stack': profile.tech_stack or [],
                'constraints': profile.constraints or [],
                'growth_goals': profile.growth_goals or [],
            }
        else:
            # Merge overrides
            for field in profile.override_fields:
                value = getattr(profile, field, None)
                if value is not None:
                    if field == 'tech_stack':
                        # For tech_stack: union of parent + child
                        effective['tech_stack'] = list(set(effective['tech_stack'] + (value or [])))
                    elif field == 'company_profile':
                        # For company_profile: child keys override parent keys
                        effective['company_profile'] = {**effective['company_profile'], **(value or {})}
                    else:
                        # For other fields: child replaces parent
                        effective[field] = value

    return effective


def resolve_effective_sources(planning_context: PlanningContext) -> QuerySet:
    """
    Resolve the effective set of sources for a planning context.
    Includes directly attached sources and inherited sources that are active.
    """
    context_ids = []
    current = planning_context
    while current is not None:
        context_ids.append(current.pk)
        current = current.parent_context

    return PlanningContextSource.objects.filter(
        planning_context_id__in=context_ids,
        is_active=True,
    ).select_related('workspace_source', 'planning_context').order_by(
        'planning_context__kind',  # org sources first
        'usage_type',
    )
```

## Ownership and scope rules

### Keep org-scoped (not context-scoped)
- `Employee` — people belong to the organization, not to a project
- `OrgUnit` — departments are org-level
- `EmployeeOrgAssignment` — department membership is org-level
- `EmployeeProjectAssignment` — project assignment is org-level (links employee to project)
- `Skill` — capability identity is shared across the org
- `SkillResolutionOverride` — resolution rules are shared
- `EmployeeSkillEvidence` (raw CV evidence) — raw capabilities are org-scoped
- `EmployeeCVProfile` — CV data belongs to the employee, not a project

### Make context-scoped (in migrations 3.3 and 3.4)
- `RoadmapAnalysisRun` — analysis of a specific context's roadmap
- `SkillBlueprintRun` — blueprint for a specific context
- `AssessmentCycle` — assessment against a specific blueprint/context
- `EvidenceMatrixRun` — matrix for a specific context
- `DevelopmentPlanRun` — plans within a specific context
- `EmployeeRoleMatch` — role fit interpretation is context-specific

### Mixed / linked
- `WorkspaceSource` — physical source record stays workspace-scoped. `PlanningContextSource` links it to contexts.
- `RoleProfile` — created by blueprints, which will be context-scoped. Role profiles are implicitly context-scoped via their blueprint.

## Relationship to existing models

```
Organization (existing)
  |
  +-- PlanningContext (kind=org, name="Acme Corp Baseline") [NEW]
  |    |
  |    +-- ContextProfile (company_profile, tech_stack) [NEW]
  |    |
  |    +-- PlanningContextSource (roadmap.pdf, strategy.pdf) [NEW]
  |    |
  |    +-- PlanningContext (kind=project, name="AI Features") [NEW]
  |         |
  |         +-- ContextProfile (overrides: tech_stack adds PyTorch) [NEW]
  |         |
  |         +-- PlanningContextSource (ai-roadmap.pdf, inherited: strategy.pdf) [NEW]
  |
  +-- IntakeWorkspace (existing, backward compatible)
       |
       +-- Employee, OrgUnit, Skill, ... (unchanged)
       |
       +-- WorkspaceSource (unchanged, physical source records)
```

## Testing checklist

1. **Model test — PlanningContext CRUD:** Create org, project, and scenario contexts with parent-child relationships.

2. **Model test — ContextProfile creation:** Create a profile for each context kind. Verify OneToOne constraint.

3. **Model test — PlanningContextSource linking:** Link the same WorkspaceSource to both org and project contexts. Verify unique constraint prevents duplicate links.

4. **Model test — inheritance chain:** Create 3-level chain: org -> project -> scenario. Walk parent_context links.

5. **Model test — slug uniqueness:** Verify two contexts in the same workspace cannot share a slug. Verify contexts in different workspaces can.

6. **Model test — cascade delete:** Delete a workspace. Verify all contexts are deleted. Delete a parent context. Verify child contexts' parent_context becomes NULL.

7. **Migration test:** Run `python manage.py migrate` and verify all tables are created.

## Estimated scope

- 1 Django migration file (3 CreateModel operations)
- ~150 lines model definitions in `org_context/models.py`
- 0 service changes
- 0 API changes
