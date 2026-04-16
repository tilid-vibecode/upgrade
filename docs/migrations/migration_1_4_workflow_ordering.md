# Migration 1.4 — Workflow Stage Ordering Alignment

## Problem statement

Three different sources in the codebase define three different stage orderings:

### Source A: `PROTOTYPE_FLOW_NOTES.md`
Manual prototype flow says:
1. Company context
2. Sources
3. Parse
4. **CV evidence** (step 5)
5. Role library sync (step 6)
6. **Blueprint** (step 7)
7. Assessments
8. Matrix
9. Plans

### Source B: `_WORKFLOW_STAGE_ORDER` at `company_intake/services.py:193-203`
```python
_WORKFLOW_STAGE_ORDER = [
    ('context', 'Workspace context'),
    ('sources', 'Source collection'),
    ('parse', 'Parsing and normalization'),
    ('blueprint', 'Blueprint generation'),        # <-- blueprint BEFORE evidence
    ('clarifications', 'Clarifications and publication'),
    ('evidence', 'CV evidence and role matching'),  # <-- evidence AFTER blueprint
    ('assessments', 'Assessments'),
    ('matrix', 'Evidence matrix'),
    ('plans', 'Development plans'),
]
```

### Source C: Route behavior
The CV build endpoint at `org_context/prototype_fastapi_views.py:346-360` does NOT enforce readiness gating — you can call it at any time.

Meanwhile, `build_workspace_readiness_response` at `company_intake/services.py:1173` requires `blueprint_published` before evidence build is "ready."

### Why this matters

This creates three kinds of confusion:
1. **Operator confusion** — the UI shows stages that contradict the actual dependency logic
2. **Developer confusion** — unclear whether CV evidence is a blueprint input or a post-blueprint enrichment
3. **Data assumption confusion** — downstream stages can't know whether employee evidence was available at blueprint time

## Goal

Replace the implicit linear stage list with an explicit dependency graph. Introduce the `roadmap_analysis` stage between `parse` and `blueprint` (preparing for Phase 2). Define which stages can run in parallel.

## Prerequisites

None (can be developed in parallel with migrations 1.1, 1.2, 1.3).

## Model changes

None. All changes are in the service layer.

## Service changes

### File: `company_intake/services.py`

#### Change 1: Replace `_WORKFLOW_STAGE_ORDER` (line 193-203)

**Before:** A flat ordered list.

**After:** Keep the list for display ordering, but add an explicit dependency map:

```python
_WORKFLOW_STAGE_ORDER = [
    ('context', 'Workspace context'),
    ('sources', 'Source collection'),
    ('parse', 'Parsing and normalization'),
    ('roadmap_analysis', 'Roadmap analysis'),       # NEW (Phase 2)
    ('blueprint', 'Blueprint generation'),
    ('clarifications', 'Clarifications and publication'),
    ('evidence', 'CV evidence and role matching'),
    ('assessments', 'Assessments'),
    ('matrix', 'Evidence matrix'),
    ('plans', 'Development plans'),
]

_STAGE_DEPENDENCIES = {
    'context': [],
    'sources': ['context'],
    'parse': ['sources'],
    'roadmap_analysis': ['parse'],                   # NEW (Phase 2)
    'blueprint': ['parse'],                          # roadmap_analysis added in Phase 2
    'clarifications': ['blueprint'],
    'evidence': ['clarifications'],                  # requires published blueprint
    'assessments': ['evidence'],                     # assessments depend on evidence, NOT parallel
    'matrix': ['assessments'],                       # matrix needs completed assessments
    'plans': ['matrix'],
}

# NOTE: Do NOT declare evidence and assessments as parallel.
# Assessments depend on evidence (assessment packs reference blueprint roles
# and use employee evidence to generate questions). Keep the dependency
# chain: evidence -> assessments -> matrix -> plans.
# Parallelism can be reintroduced later only if readiness logic, route docs,
# downstream selectors, and operator UX are all rewritten consistently.
```

Note: `roadmap_analysis` is listed in the order but its dependency on `blueprint` is not added yet. In Phase 2 (migration 2.2), the `blueprint` dependencies will be updated to `['roadmap_analysis']`. For now, `roadmap_analysis` is a no-op stage that shows as "not started" in the UI.

#### Change 2: Update `_resolve_current_stage` (line 1023-1038)

Replace position-based resolution with dependency-based resolution:

```python
def _resolve_current_stage(stage_statuses: dict[str, str]) -> str:
    """Return the earliest stage that is not yet 'completed' or 'ready',
    walking the dependency graph in topological order."""
    for stage_key, _ in _WORKFLOW_STAGE_ORDER:
        status = stage_statuses.get(stage_key, 'not_started')
        if status in _WORKFLOW_PENDING_STATUSES:
            return stage_key
    return _WORKFLOW_STAGE_ORDER[-1][0]
```

This logic doesn't change much since we keep the topological order in `_WORKFLOW_STAGE_ORDER`, but the semantics are now explicitly dependency-driven.

#### Change 3: Update `_SOURCE_REQUIREMENTS` (line 146-192)

Add a `required_for_roadmap_analysis` flag to each source requirement:

```python
{
    'key': 'roadmap_or_strategy',
    'label': 'Roadmap or strategy',
    'source_kinds': [WorkspaceSourceKind.ROADMAP, WorkspaceSourceKind.STRATEGY],
    'required': True,
    'required_for_parse': True,
    'required_for_roadmap_analysis': True,  # NEW
    'required_for_blueprint': True,
    'required_for_evidence_build': False,
},
```

#### Change 4: Update `build_workspace_readiness_response` (line 1041-1256)

In the readiness computation:

1. Add a `roadmap_analysis` stage status computation:
   - **not_started**: No `RoadmapAnalysisRun` exists
   - **ready**: All roadmap/strategy sources are parsed
   - **running**: A `RoadmapAnalysisRun` with status RUNNING exists
   - **completed**: A `RoadmapAnalysisRun` with status COMPLETED exists
   - **failed**: Latest run has status FAILED

2. For the `blueprint` stage: Initially keep existing readiness logic. In Phase 2, the blueprint stage will additionally require a completed roadmap analysis.

3. Add the concept of stage "blockers by dependency":
   ```python
   def _compute_stage_blockers(stage_key, stage_statuses, ...):
       blockers = []
       for dep in _STAGE_DEPENDENCIES.get(stage_key, []):
           dep_status = stage_statuses.get(dep, 'not_started')
           if dep_status not in ('completed', 'ready'):
               blockers.append({
                   'stage': dep,
                   'status': dep_status,
                   'message': f'{dep} must be completed before {stage_key} can begin.',
               })
       return blockers
   ```

#### Change 5: Update `PROTOTYPE_FLOW_NOTES.md`

Align the documentation with the actual stage order:

```markdown
## Prototype stage order

1. Context — company profile, pilot scope
2. Sources — upload roadmap, strategy, org CSV, CVs, job descriptions
3. Parse — extract text, chunk, index vectors
4. Roadmap analysis — decompose roadmap into initiatives, workstreams, capability needs (Phase 2)
5. Blueprint — generate roles and skill requirements from roadmap analysis
6. Clarifications — operator answers blueprint questions, then publishes
7. Evidence — build CV evidence and match employees to roles
8. Assessments — generate and collect self-assessments
9. Matrix — aggregate evidence into skill matrix
10. Plans — generate team and individual development plans

Note: Assessments depend on evidence. The full chain is:
evidence -> assessments -> matrix -> plans.
```

## API changes

The `GET /api/v1/prototype/workspaces/{slug}/company-intake/readiness` response already includes `stages` with per-stage status. The response structure stays the same, but:

1. A new `roadmap_analysis` stage appears in the response
2. Each stage now includes a `dependencies` field listing its prerequisite stages
3. Each stage includes a `blockers` field (already exists, but now dependency-aware)

**Updated response shape (additive):**
```json
{
    "stages": [
        {
            "key": "blueprint",
            "label": "Blueprint generation",
            "status": "blocked",
            "dependencies": ["parse"],
            "blockers": [
                {
                    "stage": "parse",
                    "status": "running",
                    "message": "parse must be completed before blueprint can begin."
                }
            ]
        }
    ]
}
```

## Decision: three-layer evidence semantics

This migration formalizes the distinction between three separate data layers:

### Layer 1: Profile enrichment (explicit operator action, conceptually pre-blueprint)

CV extraction is produced by an explicit operator action via `POST .../org-context/cv-evidence/build`, NOT automatically during parsing. The `parse` stage covers source text extraction, chunking, and vector indexing only.

When the operator triggers CV evidence build, it produces:
- `EmployeeCVProfile.extracted_payload` — structured CV data (role history, achievements, domain, leadership)
- `EmployeeSkillEvidence` rows — provisional and resolved skills
- Vector index entries — for retrieval

This is NOT the "evidence" stage. It is profile enrichment that is conceptually pre-blueprint — its outputs are available to blueprint-time role matching. But it is operationally a distinct step the operator performs after CVs are uploaded and parsed.

### Layer 2: Blueprint-time role-fit preview (during `blueprint` stage)

Employee-role matching runs inside `generate_skill_blueprint` at `services.py:1458`. This produces `EmployeeRoleMatch` rows. This is explicitly a **preview heuristic** — it helps the blueprint understand current team shape and generates gap summaries.

It is NOT the authoritative evidence assessment. The blueprint's employee matches are preliminary and may be revised after blueprint publication when assessments provide deeper evidence.

### Layer 3: Post-publish evidence (the `evidence` stage)

After blueprint publication:
- Role matches may be refined based on assessment data
- Assessments are generated and collected
- Evidence matrix aggregates all evidence sources
- Development plans are generated

This is the authoritative evidence layer.

### Why this matters

Migration 1.3 improves Layer 2 (blueprint-time matching) with richer input. Migration 2.5 further improves it with shortlist + rerank. But both are PREVIEW matching — they do not replace the post-publish evidence stage.

Roadmap analysis (Layer 0, migrations 2.1-2.2) depends ONLY on roadmap/strategy/context. It does NOT depend on employee skill evidence.

Route-level gating remains **advisory** in this phase — routes do not enforce readiness checks (the current code enforces readiness on matrix and plans but not on CV build or blueprint generation). The readiness response is the **authoritative** source of truth — operators should trust it to tell them what can run next. Making route gating enforced everywhere is a future improvement, not part of this migration.

## Decision: CV extraction stays as an explicit operator action

The current codebase has an explicit route for CV extraction:
```
POST /api/v1/prototype/workspaces/{workspace_slug}/org-context/cv-evidence/build
```

This plan keeps CV extraction as an explicit operator action (**Option A**), NOT an automatic part of parsing. The `parse` stage covers source text extraction and chunking only. CV extraction is a separate action that the operator triggers when CVs are uploaded and ready.

The conceptual "profile enrichment" layer from the overview refers to the data these CV build operations produce (skills, evidence rows, CV profiles), which become available BEFORE blueprint generation. The key distinction is:
- `parse` = text extraction from PDFs/docs
- `cv-evidence/build` = LLM extraction of structured skills/role-history from parsed CV text
- `blueprint/generate` = role synthesis using enriched profiles as a preview signal
- `evidence` stage = post-publish assessment-backed evidence

This is the implementation target for Codex.

## Stage-key normalization

**Critical:** The current codebase uses inconsistent stage keys. This migration MUST normalize them to one canonical set.

### Current inconsistencies:
- Readiness entity (`company_intake/entities.py:182-188`): `ready_for_evidence_build`, `ready_for_assessment_generation`, `ready_for_matrix_build`, `ready_for_plan_generation`
- Workflow stage keys (`services.py:193-203`): `evidence`, `assessments`, `matrix`, `plans`
- Route gating (`services.py:1265-1268`): `evidence_build`, `assessment_generation`, `matrix_build`, `plan_generation`

### Normalization target:
Choose one canonical key per stage and use it everywhere:

| Canonical key | Display label | Readiness flag | Route gate key |
|---|---|---|---|
| `context` | Workspace context | `ready_for_parse` | n/a |
| `sources` | Source collection | `ready_for_parse` | n/a |
| `parse` | Parsing and normalization | `ready_for_parse` | `parse` |
| `roadmap_analysis` | Roadmap analysis | `ready_for_roadmap_analysis` | `roadmap_analysis` |
| `blueprint` | Blueprint generation | `ready_for_blueprint` | `blueprint` |
| `clarifications` | Clarifications and publication | n/a | n/a |
| `evidence` | CV evidence and role matching | `ready_for_evidence` | `evidence` |
| `assessments` | Assessments | `ready_for_assessments` | `assessments` |
| `matrix` | Evidence matrix | `ready_for_matrix` | `matrix` |
| `plans` | Development plans | `ready_for_plans` | `plans` |

### Files to update:
- `company_intake/services.py` — `_WORKFLOW_STAGE_ORDER`, `_resolve_current_stage`, `assert_workspace_ready_for_stage`, all `ready_for_*` variable names
- `company_intake/entities.py` — `WorkspaceReadinessFlagsResponse`, `WorkspaceStageBlockersResponse`, `WorkspaceSourceRequirementResponse`
- All route files that call `assert_workspace_ready_for_stage` — use the canonical key

## Testing checklist

1. **Unit test — dependency resolution:** Create stage_statuses where `parse` is completed but `roadmap_analysis` is not started. Verify `_resolve_current_stage` returns `roadmap_analysis`.

2. **Unit test — blocker computation:** Verify that `blueprint` stage shows `parse` as a blocker when parse is not completed.

3. **Unit test — dependency chain:** Verify that `assessments` stage is blocked until `evidence` is completed. Verify `matrix` is blocked until `assessments` is completed.

4. **Unit test — readiness response includes new stage:** Verify the readiness response contains the `roadmap_analysis` stage with `not_started` status initially.

5. **Integration test — full stage progression:** Walk through all stages from `context` to `plans`, verifying each stage transitions correctly based on dependencies.

6. **Regression test — existing stage order preserved:** Verify that the display order of stages in the readiness response matches the expected UI order.

## Estimated scope

- 0 Django migrations
- ~50 lines changed in `company_intake/services.py` (stage order, dependencies, blockers)
- ~10 lines changed in readiness computation
- ~20 lines updated in `PROTOTYPE_FLOW_NOTES.md`
