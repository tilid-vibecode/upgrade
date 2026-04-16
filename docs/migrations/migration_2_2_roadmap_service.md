# Migration 2.2 — Roadmap Analysis Service (Multi-Pass)

## Problem statement

With the `RoadmapAnalysisRun` model in place (migration 2.1), this migration builds the actual analysis engine. The service decomposes roadmap and strategy documents into structured output through multiple LLM passes, each focused on a specific extraction task.

Currently, `_build_blueprint_inputs_sync` at `skill_blueprint/services.py:3159-3215` does vector retrieval for roadmap snippets and passes raw text digests to the blueprint LLM. The blueprint prompt must understand the roadmap AND synthesize roles simultaneously. After this migration, roadmap understanding becomes a separate, explicit, inspectable stage.

## Prerequisites

- Migration 2.1 (RoadmapAnalysisRun model exists)
- Migration 1.4 (roadmap_analysis stage exists in workflow order)

## Model changes

None (model created in 2.1).

## New file: `org_context/roadmap_services.py`

This is a new service file. It follows the same patterns as `cv_services.py` — async entry points wrapping sync database operations, with `call_openai_structured` for LLM calls.

### Architecture: 4-pass extraction pipeline

```
Pass 1: Initiative extraction (per source)
   |
   v
Pass 2: Cross-source synthesis and workstream decomposition
   |
   v
Pass 3: Capability bundle derivation
   |
   v
Pass 4: Risk analysis and dependency mapping
```

Each pass is a separate LLM call with a focused prompt and structured output schema. This is intentionally more LLM calls than a single-shot approach, but each call is simpler and produces more reliable output.

### Entry point

```python
async def run_roadmap_analysis(
    workspace: IntakeWorkspace,
    *,
    force_rebuild: bool = False,
) -> RoadmapAnalysisRun:
    """
    Analyze all roadmap and strategy sources for a workspace.
    Returns the completed (or failed) RoadmapAnalysisRun.

    If a completed analysis exists and sources haven't changed since,
    returns the existing one (unless force_rebuild=True).
    """
```

**Flow:**

1. Check for existing completed analysis with matching source fingerprint
2. Create `RoadmapAnalysisRun(status='running')`
3. Load all parsed roadmap + strategy sources
4. Build company context from workspace profile
5. Run Pass 1 — initiative extraction
6. Run Pass 2 — synthesis and workstream decomposition
7. Run Pass 3 — capability bundle derivation
8. Run Pass 4 — risk analysis
9. Persist results to `RoadmapAnalysisRun(status='completed')`
10. On any error: `RoadmapAnalysisRun(status='failed', error_message=str(exc))`

### Pass 1: Initiative extraction (per source)

**Purpose:** Extract strategic initiatives from each roadmap/strategy source independently. Running per-source allows the model to focus on one document at a time.

**Input per call:**
- Company profile (name, what it does, products, tech stack)
- Full parsed text of one source (up to 15,000 chars)
- Source metadata (title, kind)

**LLM prompt guidance:**
```
Extract all strategic initiatives from this document. An initiative is a
planned effort with a goal, scope, timeline, and expected impact.

For each initiative, identify:
- Name and goal (what the company is trying to achieve)
- Criticality (high/medium/low based on language: "must have" vs "nice to have")
- Planned window (quarter/half/year — extract from timeline language)
- Key features or deliverables mentioned
- Tech stack or technology references
- Teams or functions mentioned
- Success metrics if stated

Do NOT infer initiatives that aren't in the document.
Do NOT combine multiple distinct initiatives into one.
Mark confidence based on how explicit the evidence is.
```

**Output schema (`INITIATIVE_EXTRACTION_SCHEMA`):**
```json
{
    "source_initiatives": [
        {
            "name": "string",
            "goal": "string",
            "criticality": "high|medium|low",
            "planned_window": "string",
            "key_deliverables": ["string"],
            "tech_references": ["string"],
            "team_references": ["string"],
            "success_metrics": ["string"],
            "evidence_quote": "string",
            "confidence": 0.0
        }
    ]
}
```

**Input size handling:** The 15,000-character per-source cap is risky for long roadmap PDFs. Use section-by-section extraction for sources exceeding 15,000 chars:
- Split the source into `SourceChunk` segments (already available from parsing)
- Process each chunk independently in Pass 1
- Merge chunk-level initiatives in Pass 2

This prevents silently dropping later roadmap sections.

**Concurrency:** Process sources sequentially (same pattern as CV extraction). For workspaces with 2-3 roadmap/strategy sources, this is 2-3 LLM calls per source (potentially more if chunked).

### Pass 2: Cross-source synthesis and workstream decomposition

**Purpose:** Merge initiatives from all sources, deduplicate, resolve conflicts, and decompose each initiative into concrete delivery workstreams.

**Input:**
- All initiatives from Pass 1 (merged across sources)
- Company profile
- Organization context (departments, projects, employee count, tech stack)

**LLM prompt guidance:**
```
You have a set of strategic initiatives extracted from multiple documents.

Step 1: Merge and deduplicate
- Combine initiatives that describe the same effort from different sources
- Resolve conflicting timelines or priorities (prefer more specific source)
- Keep distinct initiatives separate even if related

Step 2: Decompose into workstreams
For each initiative, identify the concrete delivery workstreams needed.
A workstream is a track of work that requires a coherent team and skill set.

Consider ALL delivery aspects, not just feature development:
- Backend/API work
- Frontend/mobile work
- Data/ML pipeline work
- QA/testing work
- DevOps/infrastructure work
- Analytics/instrumentation work
- Security/compliance work
- Documentation/training work
- Design/UX work
- Release/deployment work
- Customer support/success impact

For each workstream, estimate:
- Scope (what needs to be built/changed)
- Delivery type (new_service, feature_extension, migration, integration, etc.)
- Affected systems
- Team shape (estimated headcount, role families, duration)
- Required capabilities with level and criticality

Do NOT create workstreams for business functions unless the roadmap
explicitly requires them. Focus on product and engineering execution.
```

**Output schema (`WORKSTREAM_SYNTHESIS_SCHEMA`):**
```json
{
    "initiatives": [
        {
            "id": "string",
            "name": "string",
            "goal": "string",
            "criticality": "string",
            "planned_window": "string",
            "source_refs": ["string"],
            "confidence": 0.0
        }
    ],
    "workstreams": [
        {
            "id": "string",
            "initiative_id": "string",
            "name": "string",
            "scope": "string",
            "delivery_type": "string",
            "affected_systems": ["string"],
            "team_shape": {
                "estimated_headcount": 0,
                "roles_needed": ["string"],
                "duration_months": 0
            },
            "required_capabilities": [
                {
                    "capability": "string",
                    "level": "string",
                    "criticality": "string"
                }
            ],
            "estimated_effort": "string",
            "confidence": 0.0
        }
    ]
}
```

### Pass 3: Capability bundle derivation

**Purpose:** Cluster the capabilities from all workstreams into coherent bundles. A capability bundle groups related skills that are needed together, making it easier for the blueprint to derive minimal role sets.

**Input:**
- All workstreams from Pass 2
- Company tech stack
- Organization context (departments, project names, sample titles from org CSV)

**Critical:** Do NOT use employee skill evidence as input to roadmap analysis. Roadmap analysis depends ONLY on roadmap/strategy/context sources. Using employee evidence would conflict with the stage ordering (roadmap analysis runs before evidence is built) and create a circular dependency. If you want org capability hints, use org CSV data (titles, departments) as lightweight team shape signals, not full skill evidence.

**LLM prompt guidance:**
```
Group the required capabilities from all workstreams into capability bundles.
A bundle is a coherent cluster of skills that typically belong to one role family.

Rules:
- Each bundle should map to 1-2 role families
- Skills that always appear together should be in the same bundle
- Cross-cutting capabilities (e.g., "code review", "system design") can appear
  in multiple bundles
- For each bundle, suggest which role families would satisfy the need
- Include skill hints (specific technologies, frameworks, methodologies)
- Reference which workstreams need this bundle
- Mark criticality based on how many workstreams depend on this bundle

Also produce a PRD/PDR-style summary for each initiative:
- Problem statement
- Proposed solution approach
- Success metrics
- Technical approach
- Open questions
```

**Output schema (`CAPABILITY_BUNDLE_SCHEMA`):**
```json
{
    "capability_bundles": [
        {
            "bundle_id": "string",
            "workstream_ids": ["string"],
            "capability_name": "string",
            "capability_type": "technical|domain|leadership|process",
            "criticality": "high|medium|low",
            "inferred_role_families": ["string"],
            "skill_hints": ["string"],
            "evidence_refs": ["string"],
            "confidence": 0.0
        }
    ],
    "prd_summaries": [
        {
            "initiative_id": "string",
            "problem_statement": "string",
            "proposed_solution": "string",
            "success_metrics": ["string"],
            "technical_approach": "string",
            "open_questions": ["string"]
        }
    ]
}
```

### Pass 4: Risk analysis and dependency mapping

**Purpose:** Identify delivery risks and cross-workstream dependencies.

**Input:**
- All initiatives and workstreams
- Capability bundles
- Organization context (team size, current skill distribution)

**LLM prompt guidance:**
```
Analyze the planned delivery for risks and dependencies.

Dependency types:
- api_contract: One workstream needs APIs from another
- data_pipeline: One workstream needs data produced by another
- shared_service: Multiple workstreams need the same infrastructure
- sequential: One must complete before another starts
- shared_team: Same people needed for multiple workstreams

Risk types:
- concentration: Only 1-2 people can do this work
- skill_gap: Required capability not present in current team
- timeline: Workstream duration conflicts with planned window
- dependency_chain: Long chain of sequential dependencies
- scope_ambiguity: Unclear scope that could expand
- technology_risk: Unproven technology choices

For each risk, suggest a mitigation hint (hire, upskill, rescope, resequence).
```

**Output schema (`RISK_ANALYSIS_SCHEMA`):**
```json
{
    "dependencies": [
        {
            "from_workstream_id": "string",
            "to_workstream_id": "string",
            "dependency_type": "string",
            "description": "string",
            "criticality": "hard|soft"
        }
    ],
    "delivery_risks": [
        {
            "id": "string",
            "risk_type": "string",
            "description": "string",
            "affected_workstreams": ["string"],
            "severity": "high|medium|low",
            "mitigation_hint": "string",
            "confidence": 0.0
        }
    ]
}
```

### Persistence

After all 4 passes complete, update the `RoadmapAnalysisRun` atomically:

```python
with transaction.atomic():
    run.initiatives = pass2_result['initiatives']
    run.workstreams = pass2_result['workstreams']
    run.capability_bundles = pass3_result['capability_bundles']
    run.prd_summaries = pass3_result['prd_summaries']
    run.dependencies = pass4_result['dependencies']
    run.delivery_risks = pass4_result['delivery_risks']
    run.status = RoadmapAnalysisRun.Status.COMPLETED
    run.save()
```

### Source fingerprinting (skip if unchanged)

Build a fingerprint from source metadata AND effective context to detect when re-analysis is unnecessary. A context can change without source files changing (company profile update, tech stack override, constraint change), so the fingerprint MUST include both.

```python
def _build_analysis_fingerprint(workspace, planning_context=None) -> str:
    """
    Build a fingerprint that captures everything that could change roadmap analysis output.
    Includes: source identity + content revision + effective context/profile.
    """
    # 1. Source identity and content revision
    sources = WorkspaceSource.objects.filter(
        workspace=workspace,
        source_kind__in=[WorkspaceSourceKind.ROADMAP, WorkspaceSourceKind.STRATEGY],
        status=WorkspaceSourceStatus.PARSED,
    ).order_by('uuid')

    source_parts = []
    for source in sources:
        parsed = getattr(source, 'parsed_source', None)
        parsed_meta_hash = ''
        if parsed and parsed.metadata:
            parsed_meta_hash = hashlib.md5(
                json.dumps(parsed.metadata, sort_keys=True).encode()
            ).hexdigest()
        source_parts.append((str(source.uuid), str(source.updated_at), parsed_meta_hash))

    # 2. Effective context / profile
    if planning_context is not None:
        effective_profile = PlanningContext.resolve_effective_profile(planning_context)
    else:
        effective_profile = build_workspace_profile_snapshot(workspace)
    profile_hash = hashlib.md5(
        json.dumps(effective_profile, sort_keys=True, default=str).encode()
    ).hexdigest()

    # 3. Combine
    fingerprint_data = {
        'sources': source_parts,
        'profile_hash': profile_hash,
    }
    return hashlib.sha256(
        json.dumps(fingerprint_data, sort_keys=True).encode()
    ).hexdigest()
```

Store fingerprint in `run.input_snapshot['analysis_fingerprint']`. If existing completed run has matching fingerprint, skip re-analysis (unless `force_rebuild=True`).

**ID normalization between passes:** Add a deterministic normalization step between LLM passes that:
- Slugifies initiative/workstream/bundle names into stable IDs
- Normalizes delivery_type values to a controlled vocabulary
- Normalizes criticality to `high`/`medium`/`low`
- Normalizes capability names for consistent downstream matching
- Carries provenance (`source_refs`) forward from Pass 1 through all subsequent passes

## API endpoints

### File: `org_context/prototype_fastapi_views.py`

#### Endpoint 1: Trigger analysis

```
POST /api/v1/prototype/workspaces/{workspace_slug}/org-context/roadmap-analysis/run
```

**Request body:**
```json
{
    "force_rebuild": false
}
```

**Response:**
```json
{
    "run_uuid": "...",
    "status": "running",
    "message": "Roadmap analysis started"
}
```

#### Endpoint 2: Get status

```
GET /api/v1/prototype/workspaces/{workspace_slug}/org-context/roadmap-analysis/status
```

**Response:**
```json
{
    "has_analysis": true,
    "latest_run": {
        "uuid": "...",
        "status": "completed",
        "created_at": "...",
        "initiative_count": 5,
        "workstream_count": 12,
        "risk_count": 4,
        "source_count": 2
    }
}
```

#### Endpoint 3: Get latest completed analysis

```
GET /api/v1/prototype/workspaces/{workspace_slug}/org-context/roadmap-analysis/latest
```

**Response:** Full `RoadmapAnalysisRun` serialized with all JSON fields.

## Stage gating update

### File: `company_intake/services.py`

Update `_STAGE_DEPENDENCIES`:

```python
'blueprint': ['roadmap_analysis'],  # Changed from ['parse']
```

Blueprint generation is now blocked until a completed `RoadmapAnalysisRun` exists. This is the key architectural gate — blueprint no longer works from raw retrieval.

**Also update `company_intake/entities.py`:** Add `ready_for_roadmap_analysis` to `WorkspaceReadinessFlagsResponse` and `roadmap_analysis` to `WorkspaceStageBlockersResponse` using the canonical stage keys established in migration 1.4. Also update `build_workspace_workflow_status_response` to include the `roadmap_analysis` stage status.

Also update the blueprint readiness check to verify:
```python
has_roadmap_analysis = RoadmapAnalysisRun.objects.filter(
    workspace=workspace,
    status=RoadmapAnalysisRun.Status.COMPLETED,
).exists()
if not has_roadmap_analysis:
    blockers.append({
        'kind': 'missing_roadmap_analysis',
        'message': 'Complete roadmap analysis before generating blueprint.',
    })
```

## LLM cost analysis

For a typical workspace with 2 roadmap/strategy sources:

| Pass | Calls | Input tokens (est.) | Output tokens (est.) |
|---|---|---|---|
| Pass 1 (per source) | 2 | 2 x 8,000 = 16,000 | 2 x 2,000 = 4,000 |
| Pass 2 (synthesis) | 1 | 5,000 | 3,000 |
| Pass 3 (capabilities) | 1 | 4,000 | 2,000 |
| Pass 4 (risks) | 1 | 4,000 | 1,500 |
| **Total** | **5** | **~29,000** | **~10,500** |

At gpt-4o-mini rates ($0.15/1M input, $0.60/1M output): ~$0.01 per analysis run. Negligible cost for significant quality improvement.

## Testing checklist

1. **Unit test — Pass 1 extraction:** Feed a mock roadmap text. Verify initiatives are extracted with correct fields.

2. **Unit test — Pass 2 synthesis:** Feed initiatives from 2 sources with overlapping content. Verify deduplication and workstream decomposition.

3. **Unit test — Pass 3 capability bundling:** Feed workstreams with overlapping capabilities. Verify bundles are clustered correctly.

4. **Unit test — Pass 4 risk analysis:** Feed workstreams with single-person skills. Verify concentration risk is identified.

5. **Integration test — full pipeline:** Upload 2 roadmap PDFs to a workspace, trigger analysis, verify all JSON fields are populated.

6. **Integration test — fingerprint skip:** Run analysis, then run again without changing sources or profile. Verify second run returns existing analysis.

7. **Integration test — force rebuild:** Run analysis with `force_rebuild=True` even when sources haven't changed. Verify new run is created.

7b. **Integration test — profile change invalidates fingerprint:** Run analysis. Change the workspace company profile (e.g., update tech stack). Run analysis again without `force_rebuild`. Verify a NEW analysis is created because the profile hash changed.

8. **Integration test — stage gating:** Verify blueprint generation is blocked when no completed roadmap analysis exists.

9. **Error test — LLM failure:** Simulate LLM failure in Pass 2. Verify run status is FAILED with error message.

## Estimated scope

- 1 new file: `org_context/roadmap_services.py` (~400-500 lines)
- 4 LLM prompt schemas (JSON schema definitions)
- ~30 lines API endpoints in `prototype_fastapi_views.py`
- ~15 lines stage gating update in `company_intake/services.py`
- ~20 lines Pydantic schemas for request/response
