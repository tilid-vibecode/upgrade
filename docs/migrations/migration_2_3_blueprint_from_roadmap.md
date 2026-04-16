# Migration 2.3 — Multi-Pass Blueprint from Structured Roadmap

## Problem statement

Blueprint generation currently builds its input from raw vector retrieval snippets. At `skill_blueprint/services.py:3159-3215`, `_build_blueprint_inputs_sync` calls `retrieve_workspace_evidence_sync` for roadmap, strategy, and role reference sources, then formats them as text digests. The blueprint LLM prompt receives these digests as unstructured text.

After migration 2.2, a structured `RoadmapAnalysisRun` exists with initiatives, workstreams, capability bundles, dependencies, and delivery risks. The blueprint should consume this structured analysis instead of re-interpreting raw document snippets.

## Prerequisites

- Migration 2.1 (RoadmapAnalysisRun model)
- Migration 2.2 (Roadmap analysis service produces completed runs)

## Model changes

### File: `skill_blueprint/models.py` — `SkillBlueprintRun` (line 109)

Add FK to link blueprint to the roadmap analysis it was derived from:

```python
class SkillBlueprintRun(TimestampedModel):
    # ... existing fields ...
    role_library_snapshot = models.ForeignKey(...)
    derived_from_run = models.ForeignKey(...)

    # NEW FIELD
    roadmap_analysis = models.ForeignKey(
        'org_context.RoadmapAnalysisRun',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='blueprint_runs',
        help_text='The roadmap analysis that provided structured input for this blueprint.',
    )
```

### Django migration

Create `skill_blueprint/migrations/NNNN_blueprint_roadmap_analysis_fk.py`:
- `AddField('skillblueprintrun', 'roadmap_analysis', ...)`

## Service changes

### File: `skill_blueprint/services.py`

#### Change 1: `_build_blueprint_inputs_sync` (line 3127-3215)

**Current approach:** Vector retrieval for roadmap/strategy/role reference sources, formatted as text digests.

**New approach:** Load the latest completed `RoadmapAnalysisRun` and use its structured fields as the primary roadmap input. Keep strategy retrieval and role reference retrieval as supplementary signals.

**Modified function:**

```python
def _build_blueprint_inputs_sync(workspace_pk, snapshot_pk) -> dict:
    workspace = IntakeWorkspace.objects.get(pk=workspace_pk)
    workspace_profile = build_workspace_profile_snapshot(workspace)

    # --- NEW: Load structured roadmap analysis ---
    roadmap_analysis = RoadmapAnalysisRun.objects.filter(
        workspace=workspace,
        status=RoadmapAnalysisRun.Status.COMPLETED,
    ).order_by('-created_at').first()

    if roadmap_analysis is not None:
        # Use structured roadmap data instead of raw retrieval
        roadmap_input = _build_structured_roadmap_input(roadmap_analysis)
    else:
        # Fallback to raw retrieval (backward compatibility)
        roadmap_input = _build_legacy_roadmap_input(workspace, parsed_sources)

    # Keep existing retrieval for supplementary sources
    # (strategy context, role references, existing matrices)
    ...

    return {
        'workspace_profile': workspace_profile,
        'source_summary': ...,
        'org_summary': ...,
        'roadmap_input': roadmap_input,        # NEW (replaces roadmap_evidence_digest)
        'roadmap_analysis_uuid': str(roadmap_analysis.uuid) if roadmap_analysis else None,  # NEW
        'strategy_evidence_digest': ...,       # KEPT (supplementary)
        'role_reference_evidence_digest': ..., # KEPT
        'role_library_digest': ...,            # KEPT
        ...
    }
```

**New helper function:**

```python
def _build_structured_roadmap_input(roadmap_analysis: RoadmapAnalysisRun) -> str:
    """
    Format the structured roadmap analysis as a detailed input section
    for the blueprint LLM prompt.
    """
    sections = []

    # Initiatives
    sections.append('### Strategic initiatives')
    for init in roadmap_analysis.initiatives:
        sections.append(
            f"- **{init['name']}** (criticality: {init.get('criticality', 'medium')}, "
            f"window: {init.get('planned_window', 'unspecified')})\n"
            f"  Goal: {init.get('goal', '')}"
        )

    # Workstreams (the key input for role derivation)
    sections.append('\n### Delivery workstreams')
    for ws in roadmap_analysis.workstreams:
        caps = ', '.join(
            f"{c['capability']} ({c.get('level', '')}, {c.get('criticality', '')})"
            for c in ws.get('required_capabilities', [])
        )
        sections.append(
            f"- **{ws['name']}** (initiative: {ws.get('initiative_id', '')})\n"
            f"  Scope: {ws.get('scope', '')}\n"
            f"  Delivery type: {ws.get('delivery_type', '')}\n"
            f"  Affected systems: {', '.join(ws.get('affected_systems', []))}\n"
            f"  Team shape: {json.dumps(ws.get('team_shape', {}))}\n"
            f"  Required capabilities: {caps}"
        )

    # Capability bundles (pre-clustered skill needs)
    sections.append('\n### Capability bundles')
    for bundle in roadmap_analysis.capability_bundles:
        sections.append(
            f"- **{bundle['capability_name']}** ({bundle.get('capability_type', '')}, "
            f"criticality: {bundle.get('criticality', '')})\n"
            f"  Workstreams: {', '.join(bundle.get('workstream_ids', []))}\n"
            f"  Inferred role families: {', '.join(bundle.get('inferred_role_families', []))}\n"
            f"  Skill hints: {', '.join(bundle.get('skill_hints', []))}"
        )

    # Dependencies and risks (context for prioritization)
    if roadmap_analysis.dependencies:
        sections.append('\n### Cross-workstream dependencies')
        for dep in roadmap_analysis.dependencies:
            sections.append(
                f"- {dep.get('from_workstream_id', '')} -> {dep.get('to_workstream_id', '')}: "
                f"{dep.get('description', '')} ({dep.get('criticality', '')})"
            )

    if roadmap_analysis.delivery_risks:
        sections.append('\n### Delivery risks')
        for risk in roadmap_analysis.delivery_risks:
            sections.append(
                f"- [{risk.get('severity', '')}] {risk.get('risk_type', '')}: "
                f"{risk.get('description', '')} "
                f"(workstreams: {', '.join(risk.get('affected_workstreams', []))})"
            )

    return '\n'.join(sections)
```

#### Change 2: `_extract_blueprint_with_llm` (line 1484-1565)

Update the user prompt to use structured roadmap input:

**Before:**
```python
'## Roadmap evidence (from parsed documents and vector retrieval)\n'
f"{blueprint_inputs['roadmap_evidence_digest'] or 'No roadmap evidence available...'}\n\n"
```

**After:**
```python
'## Roadmap analysis (structured)\n'
f"{blueprint_inputs['roadmap_input']}\n\n"
```

Update the system prompt to instruct the model to use structured workstreams:

**Add to system prompt `## Constraints` section:**
```
- WORKSTREAM-ALIGNED ROLES: Each role candidate must link to specific workstreams
  from the roadmap analysis, not just broad initiatives. If the roadmap analysis
  identifies 3 workstreams under one initiative, the role set should reflect the
  delivery needs of each workstream.
- CAPABILITY BUNDLE COVERAGE: The roadmap analysis provides pre-clustered
  capability bundles. Each bundle should be covered by at least one role candidate.
  If a capability bundle has no matching role, either add a role or raise a
  clarification question.
- USE DELIVERY RISKS: If the roadmap analysis identifies concentration risks
  or skill gaps, reflect these in the blueprint. A concentration risk should
  generate a clarification question about hiring or upskilling.
- ENABLING ROLES: If workstreams mention infrastructure, testing, analytics,
  security, or platform needs, include enabling roles even if no initiative
  explicitly names them. Use the capability bundles as the guide.
```

#### Change 3: `generate_skill_blueprint` (line 1409-1481)

Store the roadmap analysis FK:

```python
run = await sync_to_async(SkillBlueprintRun.objects.create)(
    workspace=workspace,
    title='First-layer blueprint',
    status=BlueprintStatus.RUNNING,
    role_library_snapshot=role_library_snapshot,
    roadmap_analysis_id=blueprint_inputs.get('roadmap_analysis_uuid'),  # NEW
    generation_mode='generation',
    ...
)
```

**Critical:** Do NOT overwrite `SkillBlueprintRun.roadmap_context` with a rendered text string. The current codebase treats `roadmap_context` as a structured JSON list (used by the response serializer and frontend). Writing a formatted prompt text string into it would break existing consumers.

Instead, store the rendered prompt text in `run.input_snapshot['roadmap_analysis_digest']`:

```python
await sync_to_async(_record_blueprint_run_inputs_sync)(
    run.pk,
    blueprint_inputs['source_summary'],
    {
        **input_snapshot,
        'roadmap_analysis_digest': blueprint_inputs['roadmap_input'],  # prompt text
        'roadmap_analysis_uuid': blueprint_inputs.get('roadmap_analysis_uuid'),
    },
)
# Keep roadmap_context as structured JSON from the LLM output (existing behavior)
```

The structured roadmap analysis data (initiatives, workstreams, etc.) is already accessible via the `roadmap_analysis` FK. The `roadmap_context` field continues to hold the LLM's structured output as before.

**Serializer contract reminder:** The following entities all consume `roadmap_context` as a `list`:
- `skill_blueprint/entities.py` — `SkillBlueprintRunResponse.roadmap_context: list`
- `skill_blueprint/entities.py` — `BlueprintPatchRequest.roadmap_context: Optional[list]`
- `skill_blueprint/entities.py` — `BlueprintRoadmapResponse.roadmap_context: list`
- `evidence_matrix/` and `development_plans/` services also read `roadmap_context` from blueprint runs

Writing a string into this field WILL break the response serializer and all downstream consumers.

#### Change 4: Legacy fallback

Keep the existing retrieval-based approach as a fallback when no `RoadmapAnalysisRun` exists:

```python
def _build_legacy_roadmap_input(workspace, parsed_sources):
    """
    Fallback: build roadmap input from vector retrieval (pre-migration behavior).
    Used when no RoadmapAnalysisRun exists.
    """
    retrieval_queries = _build_blueprint_retrieval_queries(...)
    roadmap_matches = retrieve_workspace_evidence_sync(...)
    roadmap_fallback = _build_parsed_source_digest(...)
    return (
        format_retrieved_evidence_digest(roadmap_matches, max_chars=12000)
        or roadmap_fallback
    )
```

This ensures backward compatibility — existing workspaces without a roadmap analysis can still generate blueprints.

## Lineage on derived runs

When `refresh_blueprint_from_clarifications` creates a derived run (using `derived_from_run`), the new run must automatically inherit the `roadmap_analysis` FK from the parent run. This preserves lineage so that:
- The derived run references the same roadmap analysis as the original
- The UI can show which roadmap analysis informed any blueprint revision
- Future diffs can compare blueprints that share the same roadmap analysis base

If the roadmap analysis has been re-run since the original blueprint, the derived run should still reference the ORIGINAL roadmap analysis (the one that informed the parent blueprint). A new roadmap analysis requires a new base blueprint, not a clarification refresh.

## Prompt compactness

Keep the structured roadmap input compact. A fully formatted workstream dump with all fields can exceed 5,000 tokens for large roadmaps. Strategies:
- Omit `team_shape.duration_months` and `estimated_effort` from the blueprint input (useful for planning, not for role derivation)
- Cap workstream scope descriptions at 200 characters
- Limit capability entries to top 5 per workstream by criticality
- For workstreams with identical capability needs, group them as "N workstreams requiring [bundle-name]"

## Expected quality improvements

### Before (single-shot from retrieval)

Blueprint prompt receives: ~12,000 chars of roadmap text snippets with uncertain relevance, retrieved by vector similarity.

Blueprint output: initiative-level roles like "Backend Engineer" and "ML Engineer" without workstream context.

### After (from structured roadmap analysis)

Blueprint prompt receives: structured initiatives, workstreams with scope and team shape, capability bundles with skill hints, dependencies, and delivery risks.

Blueprint output: workstream-aligned roles like "ML Inference Engineer (AI tutoring pipeline)" with specific skill requirements derived from capability bundles.

### Specific improvements

1. **Enabling roles surface automatically** — workstreams that mention "testing," "infrastructure," or "analytics" generate QA, DevOps, and analytics role candidates
2. **Role count is proportional to delivery complexity** — 12 workstreams across 3 initiatives naturally produce more roles than 3 initiatives alone
3. **Skill requirements are workstream-specific** — "Kubernetes" appears as high-priority for the ML inference workstream but medium for the API workstream
4. **Clarification questions are narrower** — instead of "What is your tech stack?", the model asks "Which inference framework will the AI tutoring pipeline use?"
5. **Concentration risks become clarification questions** — if the roadmap analysis flags that only 1 person has ML deployment skills, the blueprint generates a question about hiring

## Entity / response changes

### File: `skill_blueprint/entities.py`

Add `roadmap_analysis_uuid` to the blueprint response entity so the frontend can display which roadmap analysis informed the blueprint:

```python
class SkillBlueprintRunResponse(BaseModel):
    # ... existing fields ...
    roadmap_analysis_uuid: Optional[UUID] = None  # NEW
```

Also update `build_blueprint_response` (or wherever `SkillBlueprintRunResponse` is constructed) to populate the new field from `run.roadmap_analysis_id`.

## API changes

None. Blueprint generation endpoints remain the same. The roadmap analysis FK is visible in the blueprint detail response via the new entity field.

## Testing checklist

1. **Integration test — structured input reaches LLM:** Create a workspace with a completed roadmap analysis. Trigger blueprint generation. Verify the LLM prompt contains structured workstream data instead of retrieval snippets.

2. **Integration test — roadmap analysis FK is stored:** After blueprint generation, verify `blueprint_run.roadmap_analysis_id` matches the latest completed analysis.

3. **Integration test — blueprint references workstreams:** Verify the generated blueprint's `roadmap_context` field contains structured workstream references.

4. **Regression test — fallback to retrieval:** Create a workspace WITHOUT a roadmap analysis. Trigger blueprint generation. Verify it falls back to retrieval-based input and still produces a valid blueprint.

4b. **Regression test — legacy workspace-wide filter:** After Phase 3 lands, verify that workspace-wide blueprint generation (no planning_context) filters roadmap analysis by `planning_context__isnull=True` to avoid accidentally using a project-specific analysis.

## Overlap note for Codex

This migration and migration 3.3 both modify `_build_blueprint_inputs_sync` and `generate_skill_blueprint` in `skill_blueprint/services.py`. They MUST be merged in sequence: 2.3 first, then 3.3 adds context-scoping on top. Also, `skill_blueprint/entities.py` is touched by both — 2.3 may add a `roadmap_analysis_uuid` response field, and 3.3 adds `planning_context_uuid`.

5. **Quality test — enabling roles:** Create a roadmap analysis with workstreams mentioning QA, DevOps, and analytics. Generate blueprint. Verify these enabling roles appear in the role candidates.

6. **Quality test — capability bundle coverage:** Create a roadmap analysis with 5 capability bundles. Generate blueprint. Verify each bundle is covered by at least one role or surfaced as a clarification question.

## Estimated scope

- 1 Django migration (AddField on SkillBlueprintRun)
- ~60 lines new helper function `_build_structured_roadmap_input`
- ~20 lines new fallback function `_build_legacy_roadmap_input`
- ~30 lines modified in `_build_blueprint_inputs_sync`
- ~20 lines modified in system prompt
- ~10 lines modified in `generate_skill_blueprint`
