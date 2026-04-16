# Migration 3.4 — Context-Scoped Downstream Runs

## Problem statement

After migration 3.3, blueprints and roadmap analyses can be scoped to a PlanningContext. But the downstream stages — assessments, evidence matrix, and development plans — are still workspace-scoped. For a complete project-scoped pipeline, these runs also need context scoping.

## Prerequisites

- Migration 3.3 (blueprints and roadmap analyses are context-scoped)

## Model changes

### File: `employee_assessment/models.py` — `AssessmentCycle`

```python
planning_context = models.ForeignKey(
    'org_context.PlanningContext',
    on_delete=models.SET_NULL,
    null=True,
    blank=True,
    related_name='assessment_cycles',
    help_text='Planning context this assessment cycle is scoped to.',
)
```

### File: `evidence_matrix/models.py` — `EvidenceMatrixRun`

```python
planning_context = models.ForeignKey(
    'org_context.PlanningContext',
    on_delete=models.SET_NULL,
    null=True,
    blank=True,
    related_name='evidence_matrix_runs',
    help_text='Planning context this matrix is scoped to.',
)
```

### File: `development_plans/models.py` — `DevelopmentPlanRun`

```python
planning_context = models.ForeignKey(
    'org_context.PlanningContext',
    on_delete=models.SET_NULL,
    null=True,
    blank=True,
    related_name='development_plan_runs',
    help_text='Planning context this plan is scoped to.',
)
```

### File: `org_context/models.py` — `EmployeeRoleMatch`

```python
planning_context = models.ForeignKey(
    'org_context.PlanningContext',
    on_delete=models.SET_NULL,
    null=True,
    blank=True,
    related_name='employee_role_matches',
    help_text='Planning context this match was computed for.',
)
```

### Critical: Constraint rewrites for `DevelopmentPlanRun`

The existing `DevelopmentPlanRun` model has workspace-scoped uniqueness constraints that will block multiple current plans across contexts:

```python
# CURRENT (from development_plans/models.py:93-101):
UniqueConstraint(
    fields=['workspace', 'scope'],
    condition=Q(is_current=True, scope='team'),
    name='development_plan_one_current_team_per_workspace',
)
UniqueConstraint(
    fields=['workspace', 'employee'],
    condition=Q(is_current=True, scope='individual'),
    name='development_plan_one_current_individual_per_workspace',
)
```

These must be rewritten. **Critical nuance:** Simply using `(workspace, planning_context, ...)` uniqueness will NOT work for legacy rows where `planning_context IS NULL`, because SQL NULL semantics allow multiple NULLs in unique constraints.

The solution requires **two conditional uniqueness families** — one for legacy (NULL context) and one for context-scoped:

```python
# LEGACY: workspace-scoped current plans (planning_context IS NULL)
UniqueConstraint(
    fields=['workspace', 'scope'],
    condition=Q(is_current=True, scope='team', planning_context__isnull=True),
    name='development_plan_one_current_team_legacy',
),
UniqueConstraint(
    fields=['workspace', 'employee'],
    condition=Q(is_current=True, scope='individual', planning_context__isnull=True),
    name='development_plan_one_current_individual_legacy',
),

# CONTEXT-SCOPED: one current plan per context
UniqueConstraint(
    fields=['workspace', 'planning_context', 'scope'],
    condition=Q(is_current=True, scope='team', planning_context__isnull=False),
    name='development_plan_one_current_team_per_context',
),
UniqueConstraint(
    fields=['workspace', 'planning_context', 'employee'],
    condition=Q(is_current=True, scope='individual', planning_context__isnull=False),
    name='development_plan_one_current_individual_per_context',
),
```

The old constraints must be dropped and replaced. This is a breaking schema change that must be handled carefully in the migration (remove old constraints first, then add new ones). The batch-generation constraint `(generation_batch_uuid, ...)` should also be reviewed for context-awareness.

### Django migrations

- `employee_assessment/migrations/NNNN_assessment_planning_context.py`
- `evidence_matrix/migrations/NNNN_matrix_planning_context.py`
- `development_plans/migrations/NNNN_plan_planning_context.py` — includes constraint rewrites
- `org_context/migrations/NNNN_role_match_planning_context.py`

## Service changes

Each downstream service receives the same pattern: an optional `planning_context` parameter that, when provided, scopes the run to a specific context.

### Assessment service

When generating assessment packs for a context-scoped blueprint:
- Use only employees matched to roles in the context-scoped blueprint
- Store `planning_context` FK on the `AssessmentCycle`
- Assessment packs are per-employee, so they inherit context from the cycle

### Evidence matrix service

When building a matrix for a context-scoped blueprint:
- Use evidence from context-scoped assessment cycle
- Use employee-role matches from the context-scoped blueprint
- Store `planning_context` FK on the `EvidenceMatrixRun`

### Development plans service

When generating plans from a context-scoped matrix:
- Use the context-scoped matrix
- Store `planning_context` FK on the `DevelopmentPlanRun`

### Employee role match persistence

When persisting matches from a context-scoped blueprint:
- Store `planning_context` FK on each `EmployeeRoleMatch`
- This allows the same employee to have different role matches for different project contexts

**Key design point:** Raw `EmployeeSkillEvidence` stays org-scoped (not context-scoped). An employee's Python skill is Python regardless of which project they're evaluated for. But their role FIT interpretation (EmployeeRoleMatch) can differ by context because different projects have different role requirements.

**EmployeeRoleMatch.planning_context as denormalized FK:** This is intentionally denormalized — the canonical context lives on `role_profile.blueprint_run.planning_context`. The denormalized FK on `EmployeeRoleMatch` enables efficient queries like "all matches for this context" without joining through `RoleProfile` -> `SkillBlueprintRun`. Enforce consistency in the persistence layer: when creating matches, always set `planning_context` from `blueprint_run.planning_context`.

## API changes

Add optional `planning_context_uuid` parameter to all downstream endpoints. Use the actual current route names:

```
POST /api/v1/prototype/workspaces/{slug}/assessments/generate?planning_context_uuid=...
POST /api/v1/prototype/workspaces/{slug}/evidence-matrix/build?planning_context_uuid=...
POST /api/v1/prototype/workspaces/{slug}/development-plans/generate?planning_context_uuid=...
```

Also add `planning_context_uuid` to relevant response entities so the frontend can display context association.

When context is specified, these endpoints use the latest context-scoped blueprint as their source.

## Complete context-scoped pipeline flow

```
PlanningContext (project: "AI Features")
  |
  +-- RoadmapAnalysisRun (context-scoped) -----> uses project roadmap sources
  |
  +-- SkillBlueprintRun (context-scoped) -------> uses roadmap analysis + project profile
  |    |
  |    +-- EmployeeRoleMatch (context-scoped) --> evaluates FULL org pool against AI roles
  |
  +-- AssessmentCycle (context-scoped) ----------> assesses selected matched employees
  |
  +-- EvidenceMatrixRun (context-scoped) --------> matrix for AI roles x selected employees
  |
  +-- DevelopmentPlanRun (context-scoped) -------> plans for selected employees in AI context
```

Meanwhile, the same employees can be evaluated in another context with different roles:

```
PlanningContext (project: "Mobile App")
  |
  +-- Different roadmap analysis
  +-- Different blueprint (mobile roles)
  +-- Different role matches (same full pool, different role catalog)
  +-- Different assessments/matrix/plans (different selected cohort)
```

The employee's RAW evidence (skills from CV) is shared org-wide. Their role-fit interpretation differs by context.

## Downstream employee selection rule

`EmployeeRoleMatch` is computed against the **full workspace/org employee pool** (migration 3.3). But downstream context-scoped runs (assessments, matrix, plans) operate on a **selected employee cohort**, not the full pool. The selection rule is:

1. **Default selection:** Employees with at least one `EmployeeRoleMatch` in this context with `fit_score >= configurable threshold` (default: 50).
2. **Operator override:** The operator can explicitly select/deselect employees for a context's downstream runs via the API.
3. **Project assignment is NOT an implicit hard filter.** An employee who is not currently assigned to a project but has a strong role match should still be available for selection.

This rule means:
- Blueprint-time matching evaluates everyone (discovers cross-project talent)
- Assessments/matrix/plans focus on the employees who are relevant to the context's roles
- The operator retains control over who enters downstream stages

## Entity / response changes

Each downstream app must update its response entities to include `planning_context_uuid`:

### File: `employee_assessment/entities.py` (or views)

```python
# In assessment cycle response:
planning_context_uuid: Optional[UUID] = None
```

### File: `evidence_matrix/entities.py` (or views)

```python
# In matrix run response:
planning_context_uuid: Optional[UUID] = None
```

### File: `development_plans/entities.py` (or views)

```python
# In plan run response:
planning_context_uuid: Optional[UUID] = None
```

Update all response builders that construct these entities to populate `planning_context_uuid` from the corresponding model field.

## Context-aware selectors

Add context-aware versions of "latest/current" selectors for each downstream model. These REPLACE the existing workspace-only lookups in the corresponding service files.

### File: `employee_assessment/services.py`

Replace any workspace-only assessment cycle lookup with:
```python
def get_latest_assessment_cycle(workspace, planning_context=None):
    qs = AssessmentCycle.objects.filter(workspace=workspace)
    if planning_context is not None:
        qs = qs.filter(planning_context=planning_context)
    else:
        qs = qs.filter(planning_context__isnull=True)
    return qs.order_by('-created_at').first()
```

### File: `evidence_matrix/services.py`

Replace any workspace-only matrix lookup with:
```python
def get_latest_completed_matrix(workspace, planning_context=None):
    qs = EvidenceMatrixRun.objects.filter(workspace=workspace, status='completed')
    if planning_context is not None:
        qs = qs.filter(planning_context=planning_context)
    else:
        qs = qs.filter(planning_context__isnull=True)
    return qs.order_by('-created_at').first()
```

### File: `development_plans/services.py`

Replace any workspace-only current-plan lookup with:
```python
def get_current_plan(workspace, employee=None, planning_context=None):
    qs = DevelopmentPlanRun.objects.filter(workspace=workspace, is_current=True)
    if planning_context is not None:
        qs = qs.filter(planning_context=planning_context)
    else:
        qs = qs.filter(planning_context__isnull=True)
    if employee is not None:
        qs = qs.filter(employee=employee, scope='individual')
    else:
        qs = qs.filter(scope='team')
    return qs.first()
```

**Critical implementation note:** The `planning_context__isnull=True` filter for the `None` case ensures that workspace-scoped lookups do not accidentally return context-scoped runs, and vice versa. Without this filter, a workspace-scoped request could return a project-specific plan.

## Backward compatibility

Fully backward compatible:
- All existing runs have `planning_context=None` (workspace-scoped)
- All endpoints work without `planning_context_uuid`
- Queries for "latest blueprint" without context return workspace-wide results
- The `_get_effective_blueprint_run_sync` function at `services.py:1831` continues to work for workspace-scoped lookups

## Testing checklist

1. **Integration test — full context-scoped pipeline:** Create a project context. Run roadmap analysis -> blueprint -> assessment -> matrix -> plans, all scoped to the context. Verify all runs have the correct planning_context FK.

2. **Integration test — two projects, full-pool matching:** Create 2 project contexts. Generate separate blueprints. Verify the full employee pool is matched against both contexts (different role catalogs). Verify the same employee can have different matches and scores in each context.

3. **Integration test — context-scoped queries:** List blueprints filtered by context. Verify only context-scoped blueprints are returned.

4. **Regression test — workspace-scoped pipeline unchanged:** Run the full pipeline without specifying any context. Verify all runs have `planning_context=None` and behavior is identical to pre-migration.

5. **Edge case — cross-project talent:** An employee NOT assigned to AI Features but with a strong ML role match should be selectable for the AI Features context's assessments and plans.

6. **Downstream cohort selection test:** Generate matches for a context. Verify that downstream assessment/matrix/plan generation receives only employees above the fit threshold, not the full pool.

## Estimated scope

- 4 Django migrations (AddField on 4 models + constraint rewrites on DevelopmentPlanRun)
- ~30 lines per downstream service (context parameter threading)
- ~20 lines API parameter additions across 3 endpoint files
- ~40 lines context-aware selector functions
- ~20 lines constraint consistency enforcement in persistence layer
