# Migration 2.4 — Coverage Check and Gap Analysis

## Problem statement

After blueprint generation produces role candidates, there is no systematic check of whether the role set actually covers all the workstreams and capability bundles identified in the roadmap analysis. The current `_compute_role_gap_summaries_sync` at `skill_blueprint/services.py:1463-1467` computes gap summaries based on employee-role matches, but does not check roadmap coverage.

This means:
- A workstream may need "ML model serving" capability but no blueprint role covers it
- An enabling role (DevOps, QA) may be needed but not generated
- A single workstream may depend on one employee's unique skill, creating concentration risk

## Prerequisites

- Migration 2.3 (blueprint is linked to a roadmap analysis)

## Scope clarification

This is a **post-roadmap-analysis, blueprint-time coverage analysis** — NOT part of roadmap analysis itself. Roadmap analysis (2.1/2.2) depends only on roadmap/strategy/context sources. Coverage analysis runs after blueprint role generation and checks whether the generated roles cover the workstreams identified by roadmap analysis.

## Precondition: employee evidence may or may not exist

Coverage analysis runs during blueprint generation. At that point, the operator may or may not have run `cv-evidence/build`. The analysis handles both cases:

- **Workstream coverage** and **missing enabling role** checks always run — they compare roadmap workstreams against blueprint roles and do not need employee evidence.
- **Concentration risk** checks use `EmployeeSkillEvidence` if it exists. If no CV enrichment has been run, concentration risk is marked `concentration_risk_status: 'not_computed'` and no concentration-risk clarification questions are generated. This prevents the coverage analysis from silently pretending certainty about team depth when no evidence is available.

## Model changes

None. Coverage analysis results are stored in the existing `SkillBlueprintRun.gap_summary` JSONField, which is already a dict.

## Service changes

### File: `skill_blueprint/services.py`

#### New function: `_compute_coverage_analysis_sync`

```python
def _compute_coverage_analysis_sync(
    workspace_pk,
    blueprint_run_uuid: str,
    roadmap_analysis_uuid: str | None,
) -> dict:
    """
    Check blueprint role coverage against roadmap analysis workstreams
    and capability bundles.

    Returns a dict with:
    - workstream_coverage: per-workstream role coverage status
    - uncovered_workstreams: workstreams with no matching roles
    - uncovered_bundles: capability bundles with no matching roles
    - missing_enabling_roles: inferred enabling roles not in the blueprint
    - concentration_risks: skills with single-employee coverage
    - coverage_score: overall coverage percentage
    - clarification_suggestions: auto-generated clarification questions
    """
```

**Implementation:**

```python
def _compute_coverage_analysis_sync(workspace_pk, blueprint_run_uuid, roadmap_analysis_uuid):
    workspace = IntakeWorkspace.objects.get(pk=workspace_pk)

    if not roadmap_analysis_uuid:
        return {'coverage_score': None, 'message': 'No roadmap analysis available'}

    roadmap = RoadmapAnalysisRun.objects.get(pk=roadmap_analysis_uuid)
    role_profiles = list(RoleProfile.objects.filter(
        workspace=workspace,
        blueprint_run_id=blueprint_run_uuid,
    ).prefetch_related('skill_requirements__skill'))

    # Build lookup structures
    # CRITICAL: Use the canonical role family resolver (normalize_external_role_title)
    # to normalize workstream role names before comparing. Raw set intersection
    # of LLM-generated names vs blueprint families will under-report coverage.
    from skill_blueprint.services import normalize_external_role_title

    role_families = {rp.family for rp in role_profiles}
    role_skills = set()
    role_skill_aliases = set()
    for rp in role_profiles:
        for req in rp.skill_requirements.all():
            role_skills.add(req.skill.canonical_key)
            # Also collect aliases for fuzzy matching
            for alias in SkillAlias.objects.filter(skill=req.skill):
                role_skill_aliases.add(alias.alias.lower().strip())

    # --- Check 1: Workstream coverage ---
    workstream_coverage = []
    uncovered_workstreams = []
    for ws in roadmap.workstreams:
        ws_roles_needed = set(ws.get('team_shape', {}).get('roles_needed', []))
        ws_caps = ws.get('required_capabilities', [])

        # Normalize workstream role names through the same canonical family resolver
        ws_roles_normalized = set()
        for role_name in ws_roles_needed:
            normalized = normalize_external_role_title(
                role_name=role_name, role_family_hint='', department='', page_url=''
            )
            ws_roles_normalized.add(normalized.get('canonical_family', role_name))

        matched_roles = ws_roles_normalized & role_families

        # Check capabilities using canonical keys AND aliases, not just lowercase names
        cap_names = {c['capability'].lower().strip() for c in ws_caps}
        role_skill_display_names = {k.replace('-', ' ').replace('_', ' ') for k in role_skills}
        matched_caps = cap_names & (role_skill_display_names | role_skill_aliases)

        coverage = {
            'workstream_id': ws['id'],
            'workstream_name': ws['name'],
            'initiative_id': ws.get('initiative_id', ''),
            'roles_needed': list(ws_roles_needed),
            'roles_covered': list(matched_roles),
            'roles_missing': list(ws_roles_needed - matched_roles),
            'capabilities_needed': len(ws_caps),
            'capabilities_covered': len(matched_caps),
            'is_fully_covered': len(matched_roles) == len(ws_roles_needed) and len(matched_caps) >= len(cap_names) * 0.7,
        }
        workstream_coverage.append(coverage)
        if not coverage['is_fully_covered']:
            uncovered_workstreams.append(coverage)

    # --- Check 2: Capability bundle coverage ---
    uncovered_bundles = []
    for bundle in roadmap.capability_bundles:
        bundle_role_families = set(bundle.get('inferred_role_families', []))
        matched = bundle_role_families & role_families
        if not matched:
            uncovered_bundles.append({
                'bundle_id': bundle['bundle_id'],
                'capability_name': bundle['capability_name'],
                'criticality': bundle.get('criticality', 'medium'),
                'inferred_role_families': list(bundle_role_families),
                'workstream_ids': bundle.get('workstream_ids', []),
            })

    # --- Check 3: Missing enabling roles ---
    ENABLING_ROLE_SIGNALS = {
        'devops_engineer': ['infrastructure', 'deployment', 'kubernetes', 'docker', 'ci/cd', 'cloud'],
        'qa_engineer': ['testing', 'quality', 'test automation', 'qa'],
        'data_analyst': ['analytics', 'instrumentation', 'metrics', 'dashboards', 'reporting'],
        'security_engineer': ['security', 'compliance', 'authentication', 'authorization'],
        'technical_writer': ['documentation', 'api docs', 'knowledge base'],
        'release_manager': ['release', 'deployment', 'rollout', 'feature flags'],
    }
    missing_enabling = []
    all_workstream_text = ' '.join(
        f"{ws.get('name', '')} {ws.get('scope', '')} {' '.join(ws.get('affected_systems', []))}"
        for ws in roadmap.workstreams
    ).lower()
    for role_family, signals in ENABLING_ROLE_SIGNALS.items():
        if role_family not in role_families:
            if any(signal in all_workstream_text for signal in signals):
                missing_enabling.append({
                    'role_family': role_family,
                    'evidence_signals': [s for s in signals if s in all_workstream_text],
                    'recommendation': f'Consider adding a {role_family.replace("_", " ")} role — '
                                      f'workstreams reference related capabilities.',
                })

    # --- Check 4: Concentration risk ---
    # NOTE: This check uses EmployeeSkillEvidence, which only exists if the
    # operator has run cv-evidence/build before blueprint generation.
    # If no evidence exists, concentration risk is marked as not_computed.
    concentration_risks = []
    evidence_available = EmployeeSkillEvidence.objects.filter(
        workspace=workspace, weight__gt=0,
    ).exists()

    employee_skills = defaultdict(set)
    if evidence_available:
        for evidence in EmployeeSkillEvidence.objects.filter(
            workspace=workspace,
            weight__gt=0,
        ).select_related('skill', 'employee'):
            employee_skills[evidence.skill.canonical_key].add(evidence.employee.full_name)

    for bundle in roadmap.capability_bundles:
        if bundle.get('criticality') in ('high', 'medium'):
            for skill_hint in bundle.get('skill_hints', []):
                skill_key = slugify_key(skill_hint)
                employees_with_skill = employee_skills.get(skill_key, set())
                if len(employees_with_skill) <= 1:
                    concentration_risks.append({
                        'capability_bundle': bundle['capability_name'],
                        'skill': skill_hint,
                        'employee_count': len(employees_with_skill),
                        'employees': list(employees_with_skill),
                        'criticality': bundle['criticality'],
                        'risk': 'Single-person dependency' if employees_with_skill else 'No team member has this skill',
                    })

    # --- Check 5: Auto-generate clarification suggestions ---
    # De-duplicate questions before returning — multiple workstreams may produce
    # similar coverage or concentration questions.
    seen_question_keys = set()
    clarification_suggestions = []
    for ws in uncovered_workstreams:
        clarification_suggestions.append({
            'scope': 'workstream_coverage',
            'question': f"The workstream '{ws['workstream_name']}' requires roles "
                        f"[{', '.join(ws['roles_missing'])}] that are not in the current "
                        f"blueprint. Should these roles be added, or is the work covered "
                        f"by existing roles under different names?",
            'why_it_matters': f"Without coverage for this workstream, the initiative may "
                              f"lack necessary execution capacity.",
            'affected_workstream': ws['workstream_id'],
        })
    for risk in concentration_risks[:5]:  # limit to top 5
        clarification_suggestions.append({
            'scope': 'concentration_risk',
            'question': f"The capability '{risk['skill']}' (needed for "
                        f"'{risk['capability_bundle']}') has only "
                        f"{risk['employee_count']} team member(s) with this skill. "
                        f"Is hiring or upskilling planned for this area?",
            'why_it_matters': f"Single-person dependency creates delivery risk.",
        })

    # --- Compute overall score ---
    total_workstreams = len(roadmap.workstreams) or 1
    covered = sum(1 for wc in workstream_coverage if wc['is_fully_covered'])
    coverage_score = round(covered / total_workstreams * 100)

    return {
        'workstream_coverage': workstream_coverage,
        'uncovered_workstreams': uncovered_workstreams,
        'uncovered_bundles': uncovered_bundles,
        'missing_enabling_roles': missing_enabling,
        'concentration_risks': concentration_risks,
        'concentration_risk_status': 'computed' if evidence_available else 'not_computed',
        'coverage_score': coverage_score,
        'clarification_suggestions': clarification_suggestions,
    }
```

#### Integration point: `generate_skill_blueprint` (line 1409-1481)

Add coverage analysis after role generation and employee matching:

```python
# After employee matching (line 1462) and gap summaries (line 1463):
coverage_analysis = await sync_to_async(_compute_coverage_analysis_sync)(
    workspace.pk,
    str(run.uuid),
    blueprint_inputs.get('roadmap_analysis_uuid'),
)

# Merge coverage analysis into gap_summary
gap_summary = {
    **gap_summary,
    'coverage_analysis': coverage_analysis,
}

# If there are clarification suggestions from coverage analysis,
# add them to the blueprint's clarification questions
# IMPORTANT: Merge clarification suggestions into the normalized_payload
# that will be passed to _finalize_blueprint_run_sync, NOT directly into the
# run object. The current finalize logic writes clarification questions
# from the normalized payload.
if coverage_analysis.get('clarification_suggestions'):
    existing_questions = list(normalized_payload.get('clarification_questions') or [])
    existing_question_texts = {q.get('question', '') for q in existing_questions}
    for suggestion in coverage_analysis['clarification_suggestions']:
        # Deduplicate before appending
        if suggestion['question'] not in existing_question_texts:
            existing_questions.append({
                'question': suggestion['question'],
                'scope': suggestion['scope'],
                'priority': 'high' if suggestion['scope'] == 'workstream_coverage' else 'medium',
                'rationale': suggestion['why_it_matters'],
                'source': 'coverage_analysis',
            })
    normalized_payload['clarification_questions'] = existing_questions
```

## API changes

The coverage analysis is available as part of the blueprint detail response:

```
GET /api/v1/prototype/workspaces/{slug}/blueprint/latest
```

The response already includes `gap_summary`. After this migration, `gap_summary` contains an additional `coverage_analysis` key with the full analysis.

No new endpoints needed.

## Expected behavior

### Example: AI Features initiative with 4 workstreams

**Workstreams:**
1. AI Inference Pipeline (ML engineer, Backend engineer, DevOps)
2. AI Frontend Integration (Frontend engineer, UX designer)
3. AI Data Pipeline (Data engineer, ML engineer)
4. AI Analytics Dashboard (Data analyst, Frontend engineer)

**Blueprint generates:**
- ML Engineer (senior)
- Backend Engineer (mid)
- Frontend Engineer (mid)
- Data Engineer (mid)

**Coverage analysis identifies:**
- Missing enabling roles: DevOps Engineer (workstream 1 mentions infrastructure), Data Analyst (workstream 4 needs analytics)
- UX Designer not covered by any role
- Concentration risk: ML Engineer skills held by only 1 employee
- Coverage score: 50% (2 of 4 workstreams fully covered)

**Clarification questions generated:**
- "Workstream 'AI Analytics Dashboard' requires a Data Analyst role not in the blueprint. Should this be added?"
- "ML model serving skills are held by only 1 team member. Is hiring planned?"

## Testing checklist

1. **Unit test — full coverage:** Create a blueprint with roles matching all workstreams. Verify `coverage_score == 100` and no uncovered workstreams.

2. **Unit test — partial coverage:** Create a blueprint missing roles for 2 of 4 workstreams. Verify `coverage_score == 50`, correct workstreams listed as uncovered.

3. **Unit test — enabling role detection:** Create workstreams mentioning "deployment" and "testing." Verify `devops_engineer` and `qa_engineer` appear in `missing_enabling_roles`.

4. **Unit test — concentration risk:** Create one employee with a rare skill needed by a high-criticality bundle. Verify risk is flagged.

5. **Unit test — no roadmap analysis:** Call with `roadmap_analysis_uuid=None`. Verify graceful fallback with `coverage_score=None`.

5b. **Unit test — normalized family matching:** Create a workstream needing "devops_engineer" and a blueprint with family "devops_engineer". Also test with workstream using "DevOps Engineer" (display name) vs family key. Verify normalized comparison works.

5c. **Unit test — skill alias matching:** Create a workstream capability "K8s" and a role skill "Kubernetes" with alias "K8s". Verify coverage detects the match via alias.

6. **Unit test — clarification generation:** Verify uncovered workstreams and concentration risks produce clarification suggestions.

7. **Integration test — merged into gap_summary:** Run full blueprint generation with roadmap analysis. Verify `blueprint_run.gap_summary['coverage_analysis']` is populated.

## Phase 3 note

Once Phase 3 lands and runs become context-scoped, the concentration check should accept a context-scoped employee pool (employees assigned to the context's project) rather than always scanning the whole workspace. The function signature already accepts `workspace_pk` — add an optional `planning_context_pk` parameter and filter employees accordingly.

## Estimated scope

- 0 Django migrations
- ~150 lines new function `_compute_coverage_analysis_sync`
- ~20 lines integration in `generate_skill_blueprint`
