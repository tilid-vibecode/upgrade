# Migration 3.3 — Context-Scoped Blueprint and Roadmap Runs

## Problem statement

After migrations 3.1 and 3.2, `PlanningContext` exists with profiles and sources, but `RoadmapAnalysisRun` and `SkillBlueprintRun` are still workspace-scoped. Blueprint generation uses workspace-wide sources and company context. A project cannot have its own blueprint.

## Prerequisites

- Migration 3.2 (default contexts exist, profile resolution works)
- Migration 2.3 (blueprint reads from roadmap analysis)

## Model changes

### File: `skill_blueprint/models.py` — `SkillBlueprintRun`

```python
planning_context = models.ForeignKey(
    'org_context.PlanningContext',
    on_delete=models.SET_NULL,
    null=True,
    blank=True,
    related_name='blueprint_runs',
    help_text='Planning context this blueprint is scoped to.',
)
```

### File: `org_context/models.py` — `RoadmapAnalysisRun`

```python
planning_context = models.ForeignKey(
    'org_context.PlanningContext',
    on_delete=models.SET_NULL,
    null=True,
    blank=True,
    related_name='roadmap_analyses',
    help_text='Planning context this analysis is scoped to.',
)
```

### Django migrations

- `skill_blueprint/migrations/NNNN_blueprint_planning_context.py`: AddField
- `org_context/migrations/NNNN_roadmap_planning_context.py`: AddField

## Service changes

### File: `org_context/roadmap_services.py`

#### Context-scoped roadmap analysis

```python
async def run_roadmap_analysis(
    workspace: IntakeWorkspace,
    *,
    planning_context: PlanningContext | None = None,
    force_rebuild: bool = False,
) -> RoadmapAnalysisRun:
    """
    If planning_context is provided:
    - Use only sources linked to that context (via PlanningContextSource)
    - Use the context's effective profile for company context
    - Store the planning_context FK on the run

    If planning_context is None:
    - Fall back to workspace-wide sources (backward compatible)
    """
    if planning_context is not None:
        # Get sources from context (including inherited)
        context_sources = PlanningContext.resolve_effective_sources(planning_context)
        roadmap_sources = [
            cs.workspace_source
            for cs in context_sources
            if cs.include_in_roadmap_analysis
            and cs.usage_type in ('roadmap', 'strategy')
        ]
        company_context = PlanningContext.resolve_effective_profile(planning_context)
    else:
        # Legacy: use all workspace sources
        roadmap_sources = list(WorkspaceSource.objects.filter(
            workspace=workspace,
            source_kind__in=['roadmap', 'strategy'],
            status='parsed',
        ))
        company_context = build_workspace_profile_snapshot(workspace)

    run = await sync_to_async(RoadmapAnalysisRun.objects.create)(
        workspace=workspace,
        planning_context=planning_context,
        status=RoadmapAnalysisRun.Status.RUNNING,
        ...
    )
    # ... rest of analysis pipeline using roadmap_sources and company_context
```

### File: `skill_blueprint/services.py`

#### Context-scoped blueprint generation

```python
async def generate_skill_blueprint(
    workspace: IntakeWorkspace,
    *,
    planning_context: PlanningContext | None = None,
    role_library_snapshot: Optional[RoleLibrarySnapshot] = None,
) -> SkillBlueprintRun:
    """
    If planning_context is provided:
    - Load roadmap analysis scoped to this context
    - Use context's effective profile for company context
    - Use context's effective sources for supplementary evidence
    - Scope employee matching to employees assigned to the context's project(s)
    - Store planning_context FK on the run

    If planning_context is None:
    - Fall back to workspace-wide behavior (backward compatible)
    """
```

#### Context-scoped `_build_blueprint_inputs_sync`

```python
def _build_blueprint_inputs_sync(workspace_pk, snapshot_pk, planning_context_pk=None) -> dict:
    workspace = IntakeWorkspace.objects.get(pk=workspace_pk)

    if planning_context_pk:
        context = PlanningContext.objects.get(pk=planning_context_pk)
        workspace_profile = PlanningContext.resolve_effective_profile(context)
        effective_sources = PlanningContext.resolve_effective_sources(context)

        # Filter sources by context instead of workspace-wide
        parsed_sources = list(
            ParsedSource.objects.filter(
                source__in=[cs.workspace_source for cs in effective_sources if cs.is_active],
                source__status='parsed',
            ).select_related('source')
        )

        # Load roadmap analysis scoped to context
        roadmap_analysis = RoadmapAnalysisRun.objects.filter(
            workspace=workspace,
            planning_context=context,
            status='completed',
        ).order_by('-created_at').first()

        # Scope employees to project if context has a project
        if context.project:
            employee_ids = EmployeeProjectAssignment.objects.filter(
                project=context.project,
            ).values_list('employee_id', flat=True)
            employee_count = Employee.objects.filter(
                workspace=workspace, pk__in=employee_ids
            ).count()
        else:
            employee_count = Employee.objects.filter(workspace=workspace).count()
    else:
        # Legacy workspace-wide behavior
        workspace_profile = build_workspace_profile_snapshot(workspace)
        parsed_sources = list(ParsedSource.objects.filter(...))
        roadmap_analysis = RoadmapAnalysisRun.objects.filter(
            workspace=workspace, planning_context__isnull=True, status='completed'
        ).order_by('-created_at').first()
        employee_count = Employee.objects.filter(workspace=workspace).count()

    # ... rest of input building
```

#### Context-scoped employee matching

**Critical strategy correction:** Do NOT default to only employees assigned to the context's project. That narrows the system too early and conflicts with the later org-wide staffing goal. The blueprint should know about the full team pool so it can assess coverage, gaps, and concentration risks across the organization.

```python
def _load_employee_matching_inputs_sync(workspace_pk, planning_context_pk=None) -> list[dict]:
    workspace = IntakeWorkspace.objects.get(pk=workspace_pk)

    # ALWAYS match against the full org/workspace employee pool.
    # Context-scoped matching means the ROLES come from the context's blueprint,
    # but the EMPLOYEES are the full team.
    #
    # Rationale: The staffing question is "who in the org could fill these roles?"
    # not "who is already assigned to this project?" Narrowing to assigned-only
    # would prevent discovering cross-project staffing opportunities.
    employees = list(Employee.objects.filter(
        workspace=workspace
    ).order_by('full_name'))

    # If the context has a project, annotate each employee with their assignment
    # status so the matching LLM can use it as a signal (but not a filter):
    if planning_context_pk:
        context = PlanningContext.objects.get(pk=planning_context_pk)
        if context.project:
            assigned_ids = set(
                EmployeeProjectAssignment.objects.filter(
                    project=context.project,
                ).values_list('employee_id', flat=True)
            )
            for emp_payload in payloads:
                emp_payload['is_assigned_to_context_project'] = (
                    emp_payload['employee_uuid'] in {str(pk) for pk in assigned_ids}
                )

    # ... rest of payload building (same as before)
```

This approach:
- Matches ALL employees against context-scoped roles (discovers cross-project talent)
- Annotates assignment status as a signal (currently-assigned employees may have higher fit)
- Preserves the future staffing optimizer's ability to recommend cross-project allocation

## Entity / response changes

### File: `skill_blueprint/entities.py`

Add `planning_context_uuid` to blueprint response and related entities:

```python
class SkillBlueprintRunResponse(BaseModel):
    # ... existing fields ...
    roadmap_analysis_uuid: Optional[UUID] = None   # from 2.3
    planning_context_uuid: Optional[UUID] = None   # NEW
```

Update `build_blueprint_response` to populate `planning_context_uuid` from `run.planning_context_id`.

### File: `org_context/prototype_fastapi_views.py` (or new planning_context views)

Add response entities for roadmap analysis listing/detail that include `planning_context_uuid`.

## Context-aware helper functions

Add context-aware versions of the "latest/effective" helper functions:

```python
def _get_effective_blueprint_run_for_context_sync(workspace_pk, planning_context_pk):
    """Return the effective blueprint for a specific planning context.
    Precedence: published > latest review-ready, scoped to context."""
    return SkillBlueprintRun.objects.filter(
        workspace_id=workspace_pk,
        planning_context_id=planning_context_pk,
        is_published=True,
    ).order_by('-published_at').first() or SkillBlueprintRun.objects.filter(
        workspace_id=workspace_pk,
        planning_context_id=planning_context_pk,
        status__in=['reviewed', 'approved'],
    ).order_by('-created_at').first()
```

Similarly add `_get_latest_roadmap_analysis_for_context_sync`.

When `refresh_blueprint_from_clarifications` creates a derived run, automatically carry the `planning_context` FK from the parent run. Do not require the caller to re-specify it.

## API changes

### File: `org_context/prototype_fastapi_views.py`

Add optional `planning_context_uuid` query parameter to roadmap analysis endpoints:

```
POST /api/v1/prototype/workspaces/{slug}/org-context/roadmap-analysis/run?planning_context_uuid=...
```

### File: `skill_blueprint/prototype_fastapi_views.py`

Add optional `planning_context_uuid` to blueprint generation:

```
POST /api/v1/prototype/workspaces/{slug}/blueprint/generate?planning_context_uuid=...
```

Add context filtering to blueprint listing (note: current route is `/blueprint/runs`, not `/blueprint/list`):

```
GET /api/v1/prototype/workspaces/{slug}/blueprint/runs?planning_context_uuid=...
```

### Context validation at route boundary

All endpoints that accept `planning_context_uuid` MUST validate that the context belongs to the specified workspace:

```python
context = PlanningContext.objects.filter(
    uuid=planning_context_uuid,
    workspace=workspace,
).first()
if context is None:
    raise HTTPException(status_code=404, detail='Planning context not found in this workspace.')
```

## Overlap note for Codex

Migrations 2.3 and 3.3 both patch `_build_blueprint_inputs_sync` and `generate_skill_blueprint` in `skill_blueprint/services.py`. They MUST be merged in order: 2.3 first (adds roadmap analysis input), then 3.3 (adds context scoping on top). Both also touch `skill_blueprint/entities.py` — 2.3 may add `roadmap_analysis_uuid`, 3.3 adds `planning_context_uuid`.

## Usage example

### Organization with 2 projects

```
Org: Acme Corp (50 employees total)
  |
  +-- Project: AI Features (15 currently assigned)
  |     Roadmap: ai-roadmap.pdf
  |     Tech stack override: +PyTorch, +MLflow
  |
  +-- Project: Mobile App (20 currently assigned)
        Roadmap: mobile-roadmap.pdf
        Tech stack override: +React Native, +Swift

Shared: company-strategy.pdf, org-structure.csv, all CVs
```

**Workflow:**

1. Create workspace, upload all sources
2. Default `org-baseline` context auto-created with all sources
3. Create `ai-features` context (kind=project, parent=org-baseline, project=AI Features)
   - Link `ai-roadmap.pdf` as roadmap
   - Inherit `company-strategy.pdf` from org
   - Override tech_stack to add PyTorch, MLflow
4. Create `mobile-app` context (kind=project, parent=org-baseline, project=Mobile App)
   - Link `mobile-roadmap.pdf` as roadmap
   - Override tech_stack to add React Native, Swift
5. Run roadmap analysis for `ai-features` context — uses ai-roadmap.pdf + company-strategy.pdf
6. Generate blueprint for `ai-features` context — uses AI roadmap analysis + AI context profile; employee matching evaluates **all 50 employees** (project assignment is a signal, not a filter)
7. Run roadmap analysis for `mobile-app` context — uses mobile-roadmap.pdf + company-strategy.pdf
8. Generate blueprint for `mobile-app` context — uses mobile roadmap analysis + mobile context profile; employee matching evaluates **all 50 employees**

Each project gets its own blueprint with roles tailored to its roadmap. Employee matching evaluates the full org pool against project-specific roles — this enables cross-project staffing insights (e.g., discovering that a backend engineer currently on Mobile is a strong fit for an AI role).

## Backward compatibility

Fully backward compatible:
- All existing endpoints work without `planning_context_uuid` parameter
- When no context is specified, workspace-wide behavior is preserved
- Existing blueprints and roadmap analyses have `planning_context=None` (workspace-scoped)
- The UI can gradually adopt context-scoping — existing flows work unchanged

## Testing checklist

1. **Integration test — context-scoped roadmap analysis:** Create a project context with its own roadmap source. Run analysis. Verify it uses only the project's sources (inherited + own).

2. **Integration test — context-scoped blueprint:** Generate a blueprint for a project context. Verify it uses the context's roadmap analysis and effective profile.

3. **Integration test — full-pool matching with assignment annotation:** Create project with 10 of 50 employees assigned. Generate project-context blueprint. Verify all 50 employees are evaluated for matching. Verify the 10 assigned employees have `is_assigned_to_context_project=True` in their matching payload.

4. **Integration test — inherited sources:** Project context inherits strategy from org but has its own roadmap. Verify both are used.

5. **Integration test — profile override:** Org has tech_stack=[Django, React]. Project overrides with +[PyTorch]. Verify effective tech_stack has all three.

6. **Regression test — no context parameter:** Call all endpoints without planning_context_uuid. Verify workspace-wide behavior is preserved.

7. **Regression test — existing data unchanged:** Verify existing blueprints and analyses (with planning_context=None) still load and display correctly.

## Estimated scope

- 2 Django migrations (AddField on SkillBlueprintRun and RoadmapAnalysisRun)
- ~80 lines modified in `skill_blueprint/services.py` (context-aware input building)
- ~40 lines modified in `org_context/roadmap_services.py` (context-aware source selection)
- ~20 lines API parameter additions
