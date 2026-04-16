# UPG Prototype Upgrade — Migration Overview

This folder contains detailed build specifications for each migration step in the UPG prototype upgrade. Each file is a self-contained reference for implementing one migration.

## Why this upgrade

The current prototype has three systemic problems:

1. **Evidence starvation** — CV extraction asks for 8-15 skills, but unresolved skills are dropped instead of persisted. Senior CVs routinely collapse to 2-3 visible capabilities.
2. **Shallow roadmap understanding** — Blueprint generation jumps from raw retrieval snippets to role synthesis in a single LLM call, missing workstream decomposition, delivery dependencies, enabling roles, and risk analysis.
3. **Workspace-only scoping** — The pipeline treats the workspace as the sole planning boundary. The product direction requires org-scoped teams with multiple project-scoped planning contexts, inherited company profiles, and future staffing recommendations.

## Key semantic distinction: profile enrichment vs evidence vs blueprint-time matching

The pipeline has three distinct data-flow layers that touch employee capabilities. These MUST NOT be conflated:

1. **Profile enrichment** (explicit operator action via `POST .../org-context/cv-evidence/build`) — CV extraction, skill persistence, provisional skills. Produces `EmployeeSkillEvidence` + `EmployeeCVProfile.extracted_payload`. Available BEFORE blueprint. This is an explicit operator action, NOT automatic during parsing. `parse` = source text extraction / chunking / indexing only.
2. **Blueprint-time role-fit preview** (during `blueprint` stage) — employee-role matching inside `generate_skill_blueprint`. This is a heuristic preview using enriched profiles to help the blueprint understand current team shape. It is NOT the authoritative evidence stage.
3. **Post-publish evidence stage** (after blueprint publication) — finalized role-fit interpretation, assessments, matrix aggregation, and development plans. This is the authoritative evidence layer. The dependency chain is: evidence -> assessments -> matrix -> plans (NOT parallel).

Roadmap analysis (2.1/2.2) depends ONLY on roadmap/strategy/context sources. It does NOT use employee skill evidence. Coverage analysis (2.4) may use employee evidence for concentration-risk checks **when CV enrichment has been run**, but returns `concentration_risk_status: 'not_computed'` when no evidence exists. Workstream and role coverage checks always run regardless.

Context-scoped blueprint generation (3.3) matches employees against the **full workspace/org pool**. Project assignment is a ranking signal, not a hard filter. Downstream context-scoped runs (assessments, matrix, plans) operate on a **selected cohort** of matched employees, not the full pool.

## Stage-key normalization

The current codebase uses inconsistent stage keys across different surfaces:
- Readiness flags: `ready_for_evidence_build`, `ready_for_assessment_generation`, `ready_for_matrix_build`, `ready_for_plan_generation`
- Workflow stage keys: `evidence`, `assessments`, `matrix`, `plans`
- Route gating: `evidence_build`, `assessment_generation`, `matrix_build`, `plan_generation`

Migration 1.4 MUST normalize these to **one canonical key set** used across `build_workspace_readiness_response`, `build_workspace_workflow_status_response`, `assert_workspace_ready_for_stage`, all route guards, and all `company_intake/entities.py` response models.

## Architecture choices

Every migration follows Option B (the stronger, more structured option) from the analysis documents:

- **Two-pass CV evidence** with provisional persistence (not one-pass with freeform)
- **Bulk confirmation / discard UX** at employee and workspace levels (not click-per-skill)
- **Deterministic shortlist + LLM rerank** for employee-role matching (not pure LLM batch)
- **Structured roadmap analysis stage** before blueprint (not enriched single-shot prompt)
- **Multi-pass blueprint synthesis** from structured inputs (not one-shot from retrieval)
- **Explicit PlanningContext layer** with inheritance (not overloaded workspace model)

## Interim architecture note: workspace-as-org

Phase 3 introduces `PlanningContext` with an `organization` FK and adds `IntakeWorkspace.organization` as a nullable FK. However, until employees, skills, and projects are fully migrated from workspace scope to org scope, the system operates in a **workspace-as-org** mode. The Phase 3 acceptance criteria below reflect this as "prototype foundation achieved" — the planning context layer is functional, but full org-normalized ownership is a future step.

## Phase structure

### Phase 1 — Tactical quality lift

Fix the most impactful problems without restructuring the database architecture.

| Migration | Title | Dependencies |
|---|---|---|
| [1.1](./migration_1_1_provisional_evidence.md) | Provisional skill evidence persistence | None |
| [1.2](./migration_1_2_bulk_review.md) | Bulk skill review API and UX | 1.1 |
| [1.3](./migration_1_3_richer_matching.md) | Richer employee-role matching input | None |
| [1.4](./migration_1_4_workflow_ordering.md) | Workflow stage ordering alignment | None |

Migrations 1.2, 1.3, and 1.4 can be developed in parallel after 1.1.

### Phase 2 — Roadmap analysis and blueprint hardening

Introduce structured roadmap decomposition and improve blueprint quality.

| Migration | Title | Dependencies |
|---|---|---|
| [2.1](./migration_2_1_roadmap_model.md) | RoadmapAnalysisRun model | 1.4 |
| [2.2](./migration_2_2_roadmap_service.md) | Roadmap analysis service (multi-pass) | 2.1 |
| [2.3](./migration_2_3_blueprint_from_roadmap.md) | Multi-pass blueprint from structured roadmap | 2.2 |
| [2.4](./migration_2_4_coverage_check.md) | Coverage check and gap analysis | 2.3 |
| [2.5](./migration_2_5_shortlist_rerank.md) | Deterministic shortlist + LLM rerank | 1.3 |

Migration 2.5 can be developed in parallel with 2.3 and 2.4.

### Phase 3 — Org / project planning architecture

Restructure the scope model from flat workspace to hierarchical Org > PlanningContext > Runs.

| Migration | Title | Dependencies |
|---|---|---|
| [3.1](./migration_3_1_planning_context_schema.md) | PlanningContext schema | 1.4 |
| [3.2](./migration_3_2_context_backfill.md) | Default context backfill and auto-creation | 3.1 |
| [3.3](./migration_3_3_context_scoped_runs.md) | Context-scoped blueprint and roadmap runs | 3.2, 2.3 |
| [3.4](./migration_3_4_downstream_scoping.md) | Context-scoped downstream runs | 3.3 |
| [3.5](./migration_3_5_staffing_foundation.md) | Staffing foundation models | 3.1 |

Migration 3.1 can begin as soon as 1.4 is complete (parallel with Phase 2). Migration 3.5 can be developed in parallel with 3.3/3.4.

## Dependency graph

```
1.1 Provisional Evidence
 |
 +-- 1.2 Bulk Review (depends on 1.1)
 |
 +-- 1.3 Richer Matching (independent, parallel with 1.2)
 |    |
 |    +-- 2.5 Shortlist + Rerank (depends on 1.3)
 |
 +-- 1.4 Workflow Fix (independent, parallel with 1.2)
      |
      +-- 2.1 Roadmap Model
      |    |
      |    +-- 2.2 Roadmap Service
      |         |
      |         +-- 2.3 Multi-Pass Blueprint
      |              |
      |              +-- 2.4 Coverage Check
      |              |
      |              +-- 3.3 Context-Scoped Runs (also depends on 3.2)
      |                   |
      |                   +-- 3.4 Downstream Scoping
      |
      +-- 3.1 PlanningContext Schema (parallel with Phase 2)
           |
           +-- 3.2 Backfill + Auto-Create
           |
           +-- 3.5 Staffing Foundation (parallel with 3.3/3.4)
```

## Critical files across all migrations

| File | Role |
|---|---|
| `org_context/models.py` | Skill fields, RoadmapAnalysisRun, PlanningContext, ContextProfile, PlanningContextSource, staffing models |
| `company_intake/models.py` | `IntakeWorkspace` — add `organization` FK (Phase 3 foundation) |
| `company_intake/services.py` | `_WORKFLOW_STAGE_ORDER`, `_SOURCE_REQUIREMENTS`, `build_workspace_readiness_response`, `assert_workspace_ready_for_stage` |
| `company_intake/entities.py` | `WorkspaceReadinessFlagsResponse`, `WorkspaceStageBlockersResponse` — stage key normalization |
| `org_context/skill_catalog.py` | `resolve_workspace_skill_sync`, `normalize_skill_seed`, `ensure_workspace_skill_sync` |
| `org_context/cv_services.py` | `_persist_skill_evidence_rows`, `_normalize_cv_payload`, `_persist_cv_payload_sync`, bulk review functions |
| `org_context/roadmap_services.py` | New file — multi-pass roadmap analysis |
| `org_context/vector_indexing.py` | `index_employee_cv_profile_sync` — must exclude rejected evidence after 1.2 |
| `skill_blueprint/services.py` | `_build_blueprint_inputs_sync`, `_extract_blueprint_with_llm`, `_load_employee_matching_inputs_sync`, `match_employees_to_roles`, `_persist_employee_role_matches_sync` |
| `skill_blueprint/models.py` | `SkillBlueprintRun` — roadmap_analysis FK, planning_context FK |
| `skill_blueprint/entities.py` | `SkillBlueprintRunResponse` — `roadmap_context: list` contract must be preserved |
| `org_context/prototype_fastapi_views.py` | Bulk review endpoints, roadmap analysis endpoints, planning context endpoints |
| `development_plans/models.py` | Current-plan uniqueness constraints must be rewritten for Phase 3 |

## Acceptance criteria (system-wide)

After all migrations:

1. Senior CVs routinely produce 8+ visible capabilities, not 2-3.
2. Operators can process 20 employees without one-click-per-skill confirmation.
3. Role matches show justification citing role history, achievements, and domain — not just title similarity.
4. Blueprint roles are explainable by workstreams, not only broad initiatives.
5. AI / platform / QA / analytics needs appear when implied by delivery shape.
6. Clarification questions become narrower and more actionable.
7. (Prototype foundation) One workspace can have multiple planning contexts with different roadmaps and blueprints.
8. (Prototype foundation) A project context can inherit org context but override tech stack or constraints.
9. (Prototype foundation) The same employee pool can be evaluated against several project blueprints without data duplication.

Note: Criteria 7-9 are labeled "prototype foundation" because employees, skills, and projects remain workspace-scoped. Full org-normalized ownership is a future step beyond these migrations.

## Migration stream rules

Phases 2 and 3 may be parallel in product planning, but Django migrations within the same app MUST be serialized. Do not hardcode migration filenames in parallel branches within the same app.

**Rule:** Land migrations in each app serially. If parallel branches are used, require merge migrations / dependency rebasing before merge.

## Shared touchpoint merge map

These migrations touch the same files and functions. They MUST be merged in sequence, not as independent blind patches.

| Shared touchpoint | Migrations | Merge order |
|---|---|---|
| `org_context/models.py` | 1.1, 2.1, 3.1, 3.3, 3.4, 3.5 | Serial in this order |
| `org_context/cv_services.py` | 1.1, 1.2 | 1.1 first, then 1.2 |
| `company_intake/services.py` | 1.4, 2.1/2.2, 3.2 | Unify stage keys (1.4) before adding roadmap gating (2.2) and context hooks (3.2) |
| `skill_blueprint/services.py` | 1.3, 2.3, 2.4, 2.5, 3.3 | Merge in order: 1.3 -> 2.3 -> 2.4 -> 2.5 -> 3.3 |
| `skill_blueprint/entities.py` | 2.3, 3.3 | Preserve `roadmap_context: list` contract while adding new FK surface fields |
| `development_plans/models.py` | 3.4 | Constraint rewrite must handle legacy NULL `planning_context` |

## Recommended serial implementation order

1. **1.1** -> 2. **1.2** -> 3. **1.3** -> 4. **1.4** -> 5. **2.1** -> 6. **2.2** -> 7. **2.3** -> 8. **2.4** -> 9. **2.5** -> 10. **3.1** -> 11. **3.2** -> 12. **3.3** -> 13. **3.4** -> 14. **3.5**

## Suggested migration filenames (serial from current head)

### `company_intake`
- `0008_intakeworkspace_organization.py` (3.1)

### `org_context`
- `0017_skill_resolution_fields.py` (1.1)
- `0018_skill_review_and_evidence_fields.py` (1.2)
- `0019_roadmapanalysisrun.py` (2.1)
- `0020_planning_context.py` (3.1)
- `0021_backfill_planning_contexts.py` (3.2)
- `0022_roadmap_analysis_planning_context.py` (3.3)
- `0023_employee_role_match_planning_context.py` (3.4)
- `0024_staffing_foundation.py` (3.5)

### `skill_blueprint`
- `0009_blueprint_roadmap_analysis_fk.py` (2.3)
- `0010_blueprint_planning_context.py` (3.3)

### `employee_assessment`
- `0007_assessment_planning_context.py` (3.4)

### `evidence_matrix`
- `0007_matrix_planning_context.py` (3.4)

### `development_plans`
- `0009_plan_planning_context.py` (3.4 — includes current-plan constraint rewrite)

## Actual current API routes (for reference)

- CV build: `POST /api/v1/prototype/workspaces/{slug}/org-context/cv-evidence/build`
- CV rebuild: `POST /api/v1/prototype/workspaces/{slug}/org-context/cv-evidence/rebuild`
- Blueprint generate: `POST /api/v1/prototype/workspaces/{slug}/blueprint/generate`
- Blueprint list: `GET /api/v1/prototype/workspaces/{slug}/blueprint/runs`
- Matrix build: `POST /api/v1/prototype/workspaces/{slug}/evidence-matrix/build`
- Plans generate: `POST /api/v1/prototype/workspaces/{slug}/development-plans/generate`

Planning-context endpoints: use prefix `/api/v1/prototype/workspaces/{slug}/planning-contexts/...`

## Codex execution notes

1. **Use root app paths** (`company_intake/...`, `org_context/...`, etc.), not `server/...` paths. The `server/` package contains project-level settings/urls, not app code.
2. **Follow latest uploaded versions only** when files with the same name exist across review rounds.
3. **Merge overlapping service changes in order**: 1.1 before 1.2 in `cv_services.py`; 2.3 before 3.3 in `skill_blueprint/services.py`; 3.3 before 3.4 for context-aware downstream selectors.
4. **Preserve `SkillBlueprintRun.roadmap_context` as structured JSON/list**. Prompt digests belong in `input_snapshot`, never in `roadmap_context`.
5. **Do not reintroduce project-assignment hard filtering** in employee matching. Context-scoped matching evaluates the full employee pool. Project assignment is a signal, not a filter.
6. **Assessments depend on evidence**, not parallel. The chain is: evidence -> assessments -> matrix -> plans.
7. **CV enrichment is an explicit operator action** (`POST .../org-context/cv-evidence/build`), not automatic during parsing.
