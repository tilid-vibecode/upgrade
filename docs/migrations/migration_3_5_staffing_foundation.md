# Migration 3.5 — Staffing Foundation Models

## Problem statement

The product direction includes future staffing recommendations: "Given project capability demands and employee capabilities, which employees should be allocated to which projects?" This migration adds the data models needed for that future feature without building the optimizer.

Without these models, adding staffing later would require understanding the entire planning architecture from scratch. With them, the future optimizer is a new service layer on top of already-correct data boundaries.

## Prerequisites

- Migration 3.1 (PlanningContext exists)

Can be developed in parallel with migrations 3.3 and 3.4.

## Model changes

### File: `org_context/models.py` — add after `PlanningContextSource`

#### Model 1: `ProjectCapabilityDemand`

```python
class ProjectCapabilityDemand(TimestampedModel):
    """
    What capabilities a project needs and at what level.
    Derived from roadmap analysis and blueprint, can be manually adjusted.

    Example: "AI Features project needs 2.0 FTE of ML Engineering at level 4,
    this is high priority and needed in the short term."
    """

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
        help_text='The planning context (project) that has this demand.',
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='capability_demands',
        help_text='The project that needs this capability.',
    )
    skill = models.ForeignKey(
        Skill,
        on_delete=models.CASCADE,
        related_name='project_demands',
        help_text='The specific skill/capability needed.',
    )
    role_family = models.CharField(
        max_length=64,
        blank=True,
        default='',
        help_text='The role family this demand is associated with (e.g., ml_engineer).',
    )
    demand_level = models.PositiveSmallIntegerField(
        default=3,
        help_text='Minimum skill level required (1-5 scale).',
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
        help_text='Reference to the workstream within the initiative.',
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['priority', 'role_family']
        indexes = [
            models.Index(fields=['planning_context', 'priority']),
            models.Index(fields=['planning_context', 'skill']),
            models.Index(fields=['project', 'skill']),
        ]
        constraints = [
            # Validate project consistency: if planning_context is project-scoped,
            # the demand's project should match the context's project.
            # This is enforced at the service layer (not a DB constraint because
            # the context's project FK requires a join), but documented here
            # as a design invariant.
        ]

    def __str__(self):
        return f'{self.project.name}: {self.skill.display_name_en} (L{self.demand_level}, {self.fte_demand} FTE)'

    def clean(self):
        """Validate that project matches planning_context.project when context is project-scoped."""
        if (self.planning_context
                and self.planning_context.kind == PlanningContext.Kind.PROJECT
                and self.planning_context.project_id
                and self.project_id != self.planning_context.project_id):
            from django.core.exceptions import ValidationError
            raise ValidationError(
                'ProjectCapabilityDemand.project must match planning_context.project '
                'when the context is project-scoped.'
            )
```

#### Model 2: `EmployeeCapabilityAvailability`

**Design clarification:** This model is an **org-baseline availability snapshot**, NOT a context/scenario-specific allocation view. It captures what each employee can offer organization-wide. Context-specific allocation interpretations (e.g., "Alice is 0.3 FTE on Project X, so only 0.7 FTE available for Project Y") are computed at query time by the future staffing optimizer, not stored here.

This prevents duplication: one availability snapshot per employee-skill pair, recomputed from the latest evidence matrix. The `planning_context` FK scopes WHEN this snapshot was computed (which evidence matrix version), not which project it's "for."

```python
class EmployeeCapabilityAvailability(TimestampedModel):
    """
    Org-baseline snapshot of an employee's capability availability for staffing.
    Derived from CV evidence and assessments, can be manually adjusted.

    This is NOT project-specific. Context-specific allocation views are
    computed at query time by the staffing optimizer.

    Example: "Alice has ML Engineering at level 4 with 0.8 FTE total available
    (0.2 FTE committed to existing projects per EmployeeProjectAssignment)."
    """

    class ConfidenceLevel(models.TextChoices):
        HIGH = 'high', 'High (assessment confirmed)'
        MEDIUM = 'medium', 'Medium (CV evidence)'
        LOW = 'low', 'Low (inferred)'

    planning_context = models.ForeignKey(
        PlanningContext,
        on_delete=models.CASCADE,
        related_name='capability_availabilities',
        help_text='The planning context for this availability snapshot.',
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='capability_availabilities',
    )
    skill = models.ForeignKey(
        Skill,
        on_delete=models.CASCADE,
        related_name='employee_availabilities',
    )
    current_level = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        default=0,
        help_text='Current skill level (1-5 scale, from evidence matrix).',
    )
    available_fte = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        default=1.0,
        help_text='FTE available for allocation (after existing commitments).',
    )
    confidence = models.CharField(
        max_length=16,
        choices=ConfidenceLevel.choices,
        default=ConfidenceLevel.MEDIUM,
    )
    existing_allocation = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            'Current allocations reducing availability. Each item: '
            '{"project_name": str, "fte_allocated": float, "role": str}'
        ),
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

    def __str__(self):
        return f'{self.employee.full_name}: {self.skill.display_name_en} (L{self.current_level}, {self.available_fte} FTE)'
```

#### Model 3: `AllocationConstraint`

```python
class AllocationConstraint(TimestampedModel):
    """
    Constraints on how employees can be allocated to projects.
    Used by the future staffing optimizer.

    Examples:
    - max_allocation: "No employee should be allocated to more than 2 projects"
    - min_team_size: "Each project needs at least 3 engineers"
    - required_backup: "Each critical skill must have 2+ people"
    - fixed_assignment: "Alice must stay on Project X"
    """

    class ConstraintType(models.TextChoices):
        MAX_ALLOCATION = 'max_allocation', 'Max projects per employee'
        MIN_TEAM_SIZE = 'min_team_size', 'Min team size per project'
        REQUIRED_BACKUP = 'required_backup', 'Required skill backup'
        FIXED_ASSIGNMENT = 'fixed_assignment', 'Fixed employee-project assignment'
        MAX_FTE = 'max_fte', 'Max FTE per employee'
        SKILL_COVERAGE = 'skill_coverage', 'Min employees per critical skill'

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
        help_text=(
            'Constraint parameters. Shape depends on constraint_type:\n'
            '- max_allocation: {"max_projects": 2}\n'
            '- min_team_size: {"min_engineers": 3, "project_uuid": "..."}\n'
            '- required_backup: {"skill_uuid": "...", "min_employees": 2}\n'
            '- fixed_assignment: {"employee_uuid": "...", "project_uuid": "..."}\n'
            '- max_fte: {"employee_uuid": "...", "max_fte": 0.8}\n'
            '- skill_coverage: {"skill_uuid": "...", "min_employees": 2}'
        ),
    )
    description = models.TextField(
        blank=True,
        default='',
        help_text='Human-readable description of why this constraint exists.',
    )
    is_hard = models.BooleanField(
        default=True,
        help_text='True = must be satisfied. False = preference (soft constraint).',
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['constraint_type']
        indexes = [
            models.Index(fields=['planning_context', 'constraint_type']),
        ]

    def __str__(self):
        return f'{self.constraint_type}: {self.description[:50]}'
```

### Django migration

Create `org_context/migrations/NNNN_staffing_foundation.py`:
- `CreateModel('ProjectCapabilityDemand', ...)`
- `CreateModel('EmployeeCapabilityAvailability', ...)`
- `CreateModel('AllocationConstraint', ...)`

## How these models prepare for the future staffing optimizer

The future optimizer (not built now) will:

1. **Read demands** from `ProjectCapabilityDemand` — what each project needs
2. **Read availability** from `EmployeeCapabilityAvailability` — what each employee offers
3. **Apply constraints** from `AllocationConstraint` — what's not allowed
4. **Produce recommendations** — which employees to allocate where

```
ProjectCapabilityDemand (Project A needs ML@L4, 2 FTE)
  +
EmployeeCapabilityAvailability (Alice has ML@L4, 0.8 FTE available)
  +
AllocationConstraint (Alice max 2 projects, skill backup required)
  =
StaffingRecommendation (assign Alice 0.5 FTE to Project A for ML)
```

The `StaffingRecommendation` model will be added when the optimizer is built.

## Future population lineage (for reference, not implemented now)

When staffing optimization is eventually built, these models should be populated from:

- **ProjectCapabilityDemand**: Derived from `RoadmapAnalysisRun.capability_bundles` (each bundle's skills become demands) and `RoleProfile.skill_requirements` (each role's skills become demands with initiative/workstream refs)
- **EmployeeCapabilityAvailability**: Derived from the latest completed `EvidenceMatrixRun.matrix_payload` (aggregated skill levels per employee) combined with `EmployeeProjectAssignment` records (current allocations that reduce available FTE)
- **AllocationConstraint**: Manually configured by operators via a future UI

This lineage is documented here so future implementation does not invent incompatible population logic.

## Population strategy (future, not in this migration)

`ProjectCapabilityDemand` can be auto-populated from:
- `RoadmapAnalysisRun.capability_bundles` — each bundle's skills become demands
- `SkillBlueprintRun.role_profiles` — each role's skill requirements become demands

`EmployeeCapabilityAvailability` can be auto-populated from:
- `EmployeeSkillEvidence` — current levels from CV evidence + assessments
- `EvidenceMatrixRun.matrix_payload` — aggregated evidence levels
- `EmployeeProjectAssignment` — current allocations

`AllocationConstraint` will be manually configured by operators.

## API changes

None in this migration. These are schema-only models. API endpoints for managing demands, availability, and constraints will be added when the staffing optimizer is built.

## Testing checklist

1. **Model test — ProjectCapabilityDemand CRUD:** Create demands for a project. Verify FK relationships to PlanningContext, Project, and Skill.

2. **Model test — EmployeeCapabilityAvailability CRUD:** Create availability records. Verify unique constraint `(planning_context, employee, skill)`.

3. **Model test — AllocationConstraint CRUD:** Create constraints of each type. Verify constraint_value JSON is preserved.

4. **Model test — cascade delete:** Delete a PlanningContext. Verify all demands, availabilities, and constraints are deleted.

5. **Model test — employee-skill unique:** Try creating two availability records for the same employee-skill pair in the same context. Verify unique constraint violation.

6. **Migration test:** Run `python manage.py migrate` and verify all 3 tables are created.

## Estimated scope

- 1 Django migration file (3 CreateModel operations)
- ~120 lines model definitions in `org_context/models.py`
- 0 service changes
- 0 API changes
