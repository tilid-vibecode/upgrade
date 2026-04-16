# Migration 2.1 — RoadmapAnalysisRun Model

## Problem statement

Blueprint generation currently jumps from raw retrieval snippets to role synthesis in a single LLM call. At `skill_blueprint/services.py:3159-3215`, the function `_build_blueprint_inputs_sync` performs vector retrieval for roadmap, strategy, and role reference sources, builds text digests, and passes everything into one `_extract_blueprint_with_llm` call.

This means the blueprint prompt must simultaneously:
- Parse and understand the roadmap
- Decompose it into initiatives and workstreams
- Infer technical enablers, dependencies, and risks
- Derive capability needs
- Synthesize roles and skill requirements
- Generate clarification questions

That is too much for one synthesis step. The output under-specifies enabling roles (QA, platform, analytics), misses delivery dependencies, and produces initiative-level roles instead of workstream-level capability needs.

## Goal

Introduce a new model `RoadmapAnalysisRun` that stores structured roadmap decomposition output. This model is the schema foundation — the actual analysis service is built in migration 2.2.

## Prerequisites

- Migration 1.4 (workflow ordering) — the `roadmap_analysis` stage is added to the stage order

## Model changes

### File: `org_context/models.py` — add after `SourceChunk` (around line 160)

```python
class RoadmapAnalysisRun(TimestampedModel):
    """
    Structured roadmap decomposition produced before blueprint generation.
    Breaks roadmap/strategy sources into initiatives, workstreams,
    capability needs, dependencies, and delivery risks.
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

    # --- Input tracking ---
    source_summary = models.JSONField(
        default=dict,
        blank=True,
        help_text='Summary of which sources were analyzed: source UUIDs, titles, kinds.',
    )
    input_snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text='Snapshot of company profile and context at analysis time.',
    )

    # --- Structured outputs ---
    initiatives = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            'Strategic initiatives extracted from roadmap. Each item: '
            '{"id": str, "name": str, "goal": str, "criticality": str, '
            '"planned_window": str, "source_refs": list, "confidence": float}'
        ),
    )
    workstreams = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            'Delivery workstreams derived from initiatives. Each item: '
            '{"id": str, "initiative_id": str, "name": str, "scope": str, '
            '"delivery_type": str, "affected_systems": list, '
            '"team_shape": dict, "required_capabilities": list, '
            '"estimated_effort": str, "confidence": float}'
        ),
    )
    dependencies = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            'Cross-workstream dependencies. Each item: '
            '{"from_workstream_id": str, "to_workstream_id": str, '
            '"dependency_type": str, "description": str, "criticality": str}'
        ),
    )
    delivery_risks = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            'Delivery risks identified from roadmap analysis. Each item: '
            '{"id": str, "risk_type": str, "description": str, '
            '"affected_workstreams": list, "severity": str, '
            '"mitigation_hint": str, "confidence": float}'
        ),
    )
    capability_bundles = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            'Clustered capability needs per workstream. Each item: '
            '{"bundle_id": str, "workstream_ids": list, "capability_name": str, '
            '"capability_type": str, "criticality": str, '
            '"inferred_role_families": list, "skill_hints": list, '
            '"evidence_refs": list, "confidence": float}'
        ),
    )
    prd_summaries = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            'Structured PRD/PDR-style summaries per initiative. Each item: '
            '{"initiative_id": str, "problem_statement": str, '
            '"proposed_solution": str, "success_metrics": list, '
            '"technical_approach": str, "open_questions": list}'
        ),
    )

    clarification_questions = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            'Open questions surfaced during roadmap analysis that need operator input. '
            'Each item: {"id": str, "question": str, "scope": str, '
            '"affected_initiatives": list, "priority": str}'
        ),
    )

    # --- Metadata and error handling ---
    error_message = models.TextField(
        blank=True,
        default='',
        help_text='Error details if status is FAILED.',
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['workspace', 'status']),
            models.Index(fields=['workspace', '-created_at']),
        ]

    def __str__(self):
        return f'{self.title} ({self.status})'
```

### Django migration

Create `org_context/migrations/0019_roadmapanalysisrun.py`:
- `CreateModel('RoadmapAnalysisRun', ...)`

## Design invariant: deterministic IDs

Initiative, workstream, and bundle IDs MUST be deterministic or normalized after LLM extraction. Random LLM-generated IDs make refresh/diff logic brittle. Use a convention:

- Initiative IDs: `init-{slugified-name}` (e.g., `init-ai-powered-learning-features`)
- Workstream IDs: `ws-{slugified-name}` (e.g., `ws-ai-inference-pipeline`)
- Bundle IDs: `bundle-{slugified-name}` (e.g., `bundle-ml-platform-engineering`)

The normalization pass between LLM passes (in migration 2.2) is responsible for generating stable IDs from LLM output.

## Provenance conventions

All structured output items should carry provenance where possible:
- `source_refs`: list of `source_uuid` values identifying which uploaded documents contributed
- Where feasible, include `source_uuid:page-N` or `source_uuid:section-name` for finer granularity
- Provenance must be carried forward from pass to pass so that later coverage checks and clarification questions can cite exact evidence

## Structured output field schemas

### `initiatives` — what the company wants to achieve

Each initiative represents a strategic goal from the roadmap:

```json
{
    "id": "init-ai-features",
    "name": "AI-Powered Learning Features",
    "goal": "Add AI tutoring, personalized learning paths, and automated assessment generation",
    "criticality": "high",
    "planned_window": "Q2-Q3 2026",
    "source_refs": ["source-uuid-roadmap-pdf"],
    "confidence": 0.85
}
```

### `workstreams` — how initiatives are delivered

Each workstream represents a concrete delivery track within an initiative:

```json
{
    "id": "ws-ai-inference",
    "initiative_id": "init-ai-features",
    "name": "AI Inference Pipeline",
    "scope": "Build and deploy ML inference service for real-time tutoring responses",
    "delivery_type": "backend_service",
    "affected_systems": ["api-gateway", "ml-platform", "content-service"],
    "team_shape": {
        "estimated_headcount": 3,
        "roles_needed": ["ml_engineer", "backend_engineer", "devops_engineer"],
        "duration_months": 4
    },
    "required_capabilities": [
        {"capability": "ML model serving", "level": "advanced", "criticality": "high"},
        {"capability": "Python", "level": "intermediate", "criticality": "high"},
        {"capability": "Kubernetes", "level": "intermediate", "criticality": "medium"}
    ],
    "estimated_effort": "3-4 engineer-months",
    "confidence": 0.75
}
```

### `dependencies` — what blocks what

```json
{
    "from_workstream_id": "ws-ai-inference",
    "to_workstream_id": "ws-ai-frontend",
    "dependency_type": "api_contract",
    "description": "Frontend integration depends on inference API being available",
    "criticality": "hard"
}
```

### `delivery_risks` — what could go wrong

```json
{
    "id": "risk-ml-single-person",
    "risk_type": "concentration",
    "description": "Only one team member has ML deployment experience",
    "affected_workstreams": ["ws-ai-inference", "ws-ai-training"],
    "severity": "high",
    "mitigation_hint": "Consider hiring or upskilling a second ML engineer",
    "confidence": 0.8
}
```

Risk types: `concentration`, `skill_gap`, `timeline`, `dependency_chain`, `scope_ambiguity`, `technology_risk`

### `capability_bundles` — clustered capability needs

```json
{
    "bundle_id": "bundle-ml-platform",
    "workstream_ids": ["ws-ai-inference", "ws-ai-training", "ws-ai-evaluation"],
    "capability_name": "ML Platform Engineering",
    "capability_type": "technical",
    "criticality": "high",
    "inferred_role_families": ["ml_engineer", "data_engineer", "devops_engineer"],
    "skill_hints": ["PyTorch", "MLflow", "Kubernetes", "model monitoring"],
    "evidence_refs": ["source-uuid-roadmap-pdf:page-3"],
    "confidence": 0.8
}
```

### `prd_summaries` — structured initiative briefs

```json
{
    "initiative_id": "init-ai-features",
    "problem_statement": "Current learning paths are static and don't adapt to individual student needs",
    "proposed_solution": "ML-powered adaptive learning with real-time tutoring assistance",
    "success_metrics": ["50% improvement in learning outcome scores", "30% reduction in time-to-mastery"],
    "technical_approach": "Fine-tuned LLM for tutoring, collaborative filtering for path optimization",
    "open_questions": [
        "What is the target latency for real-time tutoring responses?",
        "Will the system need to support offline/mobile learning?"
    ]
}
```

## Relationship to other models

```
IntakeWorkspace
  |
  +-- WorkspaceSource (roadmap, strategy)
  |     |
  |     +-- ParsedSource --> SourceChunk (text)
  |
  +-- RoadmapAnalysisRun (NEW)  <-- consumes parsed sources
  |     |
  |     +-- .initiatives, .workstreams, .capability_bundles (JSON)
  |
  +-- SkillBlueprintRun  <-- will consume RoadmapAnalysisRun (migration 2.3)
        |
        +-- .roadmap_analysis FK (added in migration 2.3)
```

## Helper functions (for future use in migration 2.2)

Add to `org_context/models.py` or a separate manager:

```python
@classmethod
def get_latest_completed(cls, workspace):
    """Return the most recent completed analysis for a workspace."""
    return cls.objects.filter(
        workspace=workspace,
        status=cls.Status.COMPLETED,
    ).order_by('-created_at').first()

@classmethod
def get_or_none(cls, workspace):
    """Return the latest completed analysis, or None if none exists."""
    return cls.get_latest_completed(workspace)
```

## API changes

None in this migration. API endpoints for roadmap analysis are added in migration 2.2.

## Stage gating update

In `company_intake/services.py`, add readiness computation for the `roadmap_analysis` stage:

```python
# In build_workspace_readiness_response, after parse stage computation:

roadmap_analysis_run = RoadmapAnalysisRun.objects.filter(
    workspace=workspace,
).order_by('-created_at').first()

if roadmap_analysis_run is None:
    roadmap_analysis_status = 'not_started'
elif roadmap_analysis_run.status == 'completed':
    roadmap_analysis_status = 'completed'
elif roadmap_analysis_run.status == 'running':
    roadmap_analysis_status = 'running'
elif roadmap_analysis_run.status == 'failed':
    roadmap_analysis_status = 'failed'
else:
    roadmap_analysis_status = 'ready'
```

The `roadmap_analysis` stage shows as `ready` when all roadmap/strategy sources are parsed. It shows as `completed` when a `RoadmapAnalysisRun` with status COMPLETED exists.

**Important:** Do NOT add `roadmap_analysis` as a dependency for `blueprint` yet. That happens in migration 2.2 when the analysis service is built. For now, blueprint generation still works without a roadmap analysis.

**Planning ahead for Phase 3:** The model intentionally does NOT include a `planning_context` FK yet. That FK is added in migration 3.3 to avoid premature coupling. The model should be designed so that adding the FK later is a clean additive change.

## Testing checklist

1. **Model test — CRUD:** Create a `RoadmapAnalysisRun` with all JSON fields populated. Verify save/load roundtrip preserves all data.

2. **Model test — status transitions:** Verify status field accepts all defined choices.

3. **Model test — workspace relationship:** Create a workspace with 2 roadmap analyses. Verify `workspace.roadmap_analyses.count() == 2`.

4. **Model test — ordering:** Create 3 analyses at different times. Verify `.first()` returns the most recent.

5. **Service test — readiness computation:** Verify the `roadmap_analysis` stage appears in readiness response with correct status.

6. **Migration test:** Run `python manage.py migrate` and verify the table is created.

## Estimated scope

- 1 Django migration file (CreateModel)
- ~80 lines model definition in `org_context/models.py`
- ~15 lines readiness computation in `company_intake/services.py`
