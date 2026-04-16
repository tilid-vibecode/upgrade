# Migration 2.5 — Deterministic Shortlist + LLM Rerank

## Problem statement

Employee-role matching currently sends ALL employees to the LLM in batches of 8 (or 5 after migration 1.3). For a workspace with 50 employees and 10 roles, the LLM sees all 50 employees and must evaluate each against all 10 roles in a single prompt.

This has three problems:
1. **Noise** — the LLM wastes capacity evaluating clearly implausible matches (a UX designer against a DevOps role)
2. **Cost** — every employee is evaluated even when their profile has zero overlap with a role
3. **Instability** — the LLM's ranking can vary between runs because it processes too many candidates at once

A two-stage approach is more robust:
1. **Deterministic shortlist** — fast, reproducible, no LLM cost. Use skill overlap, title similarity, org/project relevance, and role history to select top-15 candidates per role.
2. **LLM rerank** — high quality, focused. Send only the 15 shortlisted candidates to the LLM for each role, with rich profile data.

## Prerequisites

- Migration 1.3 (richer matching input with role history, achievements, domain, leadership)

## Model changes

None.

## Service changes

### File: `skill_blueprint/services.py`

#### New function: `_build_deterministic_shortlist`

```python
def _build_deterministic_shortlist(
    employees: list[dict],
    role_profiles: list[dict],
    workspace: IntakeWorkspace,
    *,
    max_candidates_per_role: int = 8,
) -> dict[str, list[dict]]:
    """
    Build a deterministic shortlist of candidate employees for each role.

    Returns: {role_uuid: [employee_dict, ...]} where each list has
    at most max_candidates_per_role entries, sorted by deterministic score.

    CRITICAL: The role object shape must match the persisted RoleProfile rows,
    NOT the raw LLM blueprint payload. Use RoleProfile fields:
    - role_profile.name (not 'role_name' from LLM payload)
    - role_profile.family (not 'canonical_role_family')
    - role_profile.skill_requirements (not 'skills' list from LLM)
    Build the shortlist from persisted RoleProfile rows after _persist_blueprint_payload_sync.

    Scoring components:
    1. Title similarity (Jaccard over normalized tokens) — weight 0.20
    2. Skill overlap (shared canonical_keys between evidence and role requirements) — weight 0.30
    3. Org unit relevance (employee's department matches role's department) — weight 0.10
    4. Initiative/workstream relevance (from roadmap analysis) — weight 0.10
    5. Role history relevance (previous role titles similar to target role) — weight 0.20
    6. Seniority alignment (employee seniority vs role seniority) — weight 0.10
    """
```

**Scoring algorithm:**

```python
def _compute_shortlist_score(employee: dict, role: dict) -> float:
    score = 0.0

    # --- 1. Title similarity (weight: 0.20) ---
    emp_title_tokens = _normalize_title_tokens(employee.get('current_title', ''))
    role_name_tokens = _normalize_title_tokens(role.get('name', ''))
    role_family_tokens = _normalize_title_tokens(role.get('family', ''))

    title_jaccard = _jaccard_similarity(emp_title_tokens, role_name_tokens | role_family_tokens)
    score += 0.20 * title_jaccard

    # --- 2. Skill overlap (weight: 0.30) ---
    emp_skill_keys = {
        s.get('skill_name_en', '').lower().strip()
        for s in employee.get('skills_from_evidence', [])
    }
    role_skill_keys = {
        s.get('skill_name_en', '').lower().strip()
        for s in role.get('skill_requirements', [])
    }
    if role_skill_keys:
        skill_overlap = len(emp_skill_keys & role_skill_keys) / len(role_skill_keys)
    else:
        skill_overlap = 0.0
    score += 0.30 * skill_overlap

    # --- 3. Org unit relevance (weight: 0.10) ---
    emp_units = {u.lower() for u in employee.get('org_units', [])}
    role_dept = role.get('department', '').lower()
    if role_dept and any(role_dept in unit or unit in role_dept for unit in emp_units):
        score += 0.10
    elif emp_units and role_dept:
        score += 0.05 * max(
            _jaccard_similarity(
                _normalize_title_tokens(unit),
                _normalize_title_tokens(role_dept)
            )
            for unit in emp_units
        )

    # --- 4. Initiative/workstream relevance (weight: 0.10) ---
    # NOTE: Employee projects and blueprint related_initiatives are NOT the same
    # taxonomy today. Use domain/keyword overlap instead of exact project name matching.
    emp_projects = {p.lower() for p in employee.get('projects', [])}
    emp_domains = set()
    for rh in employee.get('role_history', []):
        emp_domains.update(d.lower() for d in rh.get('domains', []))
    role_initiatives_text = ' '.join(role.get('related_initiatives', [])).lower()
    if emp_domains and role_initiatives_text:
        domain_hits = sum(1 for d in emp_domains if d in role_initiatives_text)
        score += 0.10 * min(domain_hits / max(len(emp_domains), 1), 1.0)
    elif emp_projects and role_initiatives_text:
        project_hits = sum(1 for p in emp_projects if p in role_initiatives_text)
        score += 0.05 * min(project_hits / max(len(emp_projects), 1), 1.0)

    # --- 5. Role history relevance (weight: 0.20) ---
    role_history = employee.get('role_history', [])
    if role_history:
        max_history_score = 0.0
        for past_role in role_history:
            past_title_tokens = _normalize_title_tokens(past_role.get('role_title', ''))
            history_sim = _jaccard_similarity(past_title_tokens, role_name_tokens | role_family_tokens)
            max_history_score = max(max_history_score, history_sim)
            # Bonus for domain overlap
            past_domains = {d.lower() for d in past_role.get('domains', [])}
            if past_domains & role_family_tokens:
                max_history_score = max(max_history_score, 0.5)
        score += 0.20 * max_history_score

    # --- 6. Seniority alignment (weight: 0.10) ---
    emp_seniority = _seniority_rank(employee.get('seniority', ''))
    role_seniority = _seniority_rank(role.get('seniority', ''))
    if emp_seniority > 0 and role_seniority > 0:
        seniority_diff = abs(emp_seniority - role_seniority)
        seniority_score = max(0, 1.0 - seniority_diff * 0.3)
        score += 0.10 * seniority_score

    return round(score, 4)


def _normalize_title_tokens(title: str) -> set[str]:
    """Normalize a title into a set of lowercase tokens, removing stopwords."""
    STOPWORDS = {'the', 'a', 'an', 'of', 'and', 'or', 'in', 'at', 'for', 'to', 'with'}
    tokens = re.split(r'[\s\-_/,]+', title.lower().strip())
    return {t for t in tokens if t and t not in STOPWORDS}


def _jaccard_similarity(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union > 0 else 0.0


def _seniority_rank(seniority: str) -> int:
    RANKS = {'intern': 1, 'junior': 2, 'mid': 3, 'mid-level': 3, 'senior': 4, 'lead': 5, 'staff': 5, 'principal': 6, 'director': 7, 'vp': 8, 'cto': 9, 'ceo': 9}
    return RANKS.get(seniority.lower().strip(), 0)
```

**Building the shortlist:**

```python
def _build_deterministic_shortlist(employees, role_profiles, workspace, *, max_candidates_per_role=15):
    shortlist = {}
    for role in role_profiles:
        scored = []
        for emp in employees:
            score = _compute_shortlist_score(emp, role)
            scored.append((score, emp))
        scored.sort(key=lambda x: x[0], reverse=True)
        shortlist[role['role_uuid']] = [
            {**emp, 'shortlist_score': score}
            for score, emp in scored[:max_candidates_per_role]
        ]
    # Keep the shortlist explanation for debugging — operators and developers
    # should be able to see WHY someone made or missed the shortlist.
    return shortlist
```

#### Modified function: `match_employees_to_roles` (line 1655-1788)

**New flow:**

```python
async def match_employees_to_roles(workspace, role_candidates, *, blueprint_run_uuid):
    employees = await sync_to_async(_load_employee_matching_inputs_sync)(workspace.pk)
    role_catalog = _build_role_catalog(role_candidates)

    # Step 1: Deterministic shortlist
    shortlist = _build_deterministic_shortlist(employees, role_catalog, workspace)

    # Step 2: LLM rerank per role (batch roles, not employees)
    matches_by_employee = defaultdict(list)

    # Delete existing matches
    await sync_to_async(
        lambda: EmployeeRoleMatch.objects.filter(
            workspace=workspace,
            source_kind='blueprint',
            role_profile__blueprint_run_id=blueprint_run_uuid,
        ).delete()
    )()

    # Process roles in batches of 5
    role_items = list(shortlist.items())
    for batch_start in range(0, len(role_items), _ROLE_RERANK_BATCH_SIZE):
        batch = role_items[batch_start:batch_start + _ROLE_RERANK_BATCH_SIZE]

        batch_input = [
            {
                'role': next(r for r in role_catalog if r['role_uuid'] == role_uuid),
                'candidates': candidates,
            }
            for role_uuid, candidates in batch
        ]

        result = await _rerank_candidates_llm(workspace, batch_input)

        for role_result in result:
            role_uuid = role_result['role_uuid']
            for match in role_result.get('matches', []):
                emp_uuid = match['employee_uuid']
                matches_by_employee[emp_uuid].append(match)
                await sync_to_async(_persist_employee_role_matches_sync)(
                    workspace.pk, str(blueprint_run_uuid), emp_uuid, [match]
                )

    # Format output grouped by employee
    return [
        {
            'employee_uuid': emp['employee_uuid'],
            'full_name': emp['full_name'],
            'matches': matches_by_employee.get(emp['employee_uuid'], []),
        }
        for emp in employees
    ]
```

**New constant:**
```python
_ROLE_RERANK_BATCH_SIZE = 3  # roles per LLM call (keep small for first rollout)
```

**Candidate card compression:** When sending candidates to the LLM for reranking, compress each candidate to reduce token usage:
- Skills: top 8 by weight, name only (no evidence text)
- Role history: top 3, company + title + key achievement only
- Achievements: top 3, summary only (no evidence text)
- Domain/leadership: name/signal only, no evidence text
- Total per-candidate target: ~250 tokens

#### New function: `_rerank_candidates_llm`

```python
async def _rerank_candidates_llm(workspace, batch_input: list[dict]) -> list[dict]:
    """
    LLM rerank: for each role in the batch, rerank its shortlisted candidates.
    """
    system_prompt = (
        'You are reranking candidate employees for specific roles.\n\n'
        '## Your task\n'
        'For each role provided, evaluate the shortlisted candidates and return '
        'the top 3 matches with fit scores and detailed rationale.\n\n'
        '## Scoring rules\n'
        '- fit_score 85-100: Strong fit — career trajectory, skills, and delivery '
        'experience clearly align with the role.\n'
        '- fit_score 70-84: Good fit — most signals align, some gaps addressable.\n'
        '- fit_score 50-69: Partial fit — significant stretch required.\n'
        '- Below 50: Do not include.\n\n'
        '## Evidence hierarchy (strongest to weakest)\n'
        '1. Demonstrated delivery in role_history (shipped relevant products/features)\n'
        '2. Quantified achievements related to the role requirements\n'
        '3. Domain experience matching the role target area\n'
        '4. Skill evidence with high confidence scores\n'
        '5. Leadership signals (for senior/lead roles)\n'
        '6. Title and department alignment\n\n'
        '## Rationale requirements\n'
        'Cite SPECIFIC evidence: name the company, achievement, skill, or domain '
        'from the candidate profile. Never use generic phrases like "strong candidate."\n'
    )

    user_prompt = json.dumps(batch_input, ensure_ascii=False, indent=2)

    result = await call_openai_structured(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_name='role_candidate_rerank',
        schema=ROLE_RERANK_SCHEMA,
        temperature=0.1,
        max_tokens=1500 + 400 * len(batch_input),
        timeout=300.0,
    )
    return result.parsed.get('role_results', [])
```

**Critical persistence contract:** The current `_persist_employee_role_matches_sync` (at `skill_blueprint/services.py:4052`) looks up `RoleProfile` by:
```python
role_profile = RoleProfile.objects.filter(
    workspace=workspace,
    blueprint_run_id=blueprint_run_uuid,
    name=item.get('role_name', ''),
    seniority=item.get('seniority', ''),
).first()
```

The rerank result MUST include `role_name` and `seniority` in each match dict, not only `role_uuid`. Either:
- Include `role_name` and `seniority` in the LLM rerank output schema, or
- Add a post-processing step that enriches each match with `role_name` and `seniority` from the role lookup before passing to the persistence function.

The recommended approach is post-processing enrichment:
```python
for role_result in result:
    role_uuid = role_result['role_uuid']
    role_info = role_lookup[role_uuid]  # from the shortlist input
    for match in role_result.get('matches', []):
        match['role_name'] = role_info['name']
        match['seniority'] = role_info['seniority']
```
```

**New schema: `ROLE_RERANK_SCHEMA`**

```json
{
    "type": "object",
    "properties": {
        "role_results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "role_uuid": {"type": "string"},
                    "matches": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "employee_uuid": {"type": "string"},
                                "fit_score": {"type": "integer"},
                                "rationale": {"type": "string"},
                                "related_initiatives": {"type": "array", "items": {"type": "string"}}
                            },
                            "required": ["employee_uuid", "fit_score", "rationale"]
                        }
                    }
                },
                "required": ["role_uuid", "matches"]
            }
        }
    },
    "required": ["role_results"]
}
```

#### Fallback for small workspaces

For workspaces with fewer than 20 employees, the deterministic shortlist adds little value (all employees would be shortlisted anyway). Keep the existing batch-by-employee approach for small workspaces:

```python
SHORTLIST_THRESHOLD = 20

if len(employees) < SHORTLIST_THRESHOLD:
    # Use existing batch-by-employee matching (unchanged from current code)
    return await _match_employees_batch_legacy(workspace, employees, role_catalog, blueprint_run_uuid)
else:
    # Use shortlist + rerank approach
    shortlist = _build_deterministic_shortlist(employees, role_catalog, workspace)
    return await _match_with_shortlist(workspace, shortlist, role_catalog, employees, blueprint_run_uuid)
```

## Token and cost comparison

### Before (batch-by-employee, 50 employees, 10 roles)

| Metric | Value |
|---|---|
| Batches | 50/5 = 10 LLM calls |
| Per-call input | 5 employees x 550 tokens + 10 roles x 150 tokens = 4,250 tokens |
| Total input tokens | 42,500 |
| Total output tokens | ~15,000 |

### After (shortlist + rerank, 50 employees, 10 roles, revised sizes)

| Metric | Value |
|---|---|
| Shortlist | 0 LLM calls (deterministic) |
| Rerank batches | 10/3 = 4 LLM calls |
| Per-call input | 3 roles x (150 tokens + 8 candidates x 250 tokens) = 6,450 tokens |
| Total input tokens | 25,800 |
| Total output tokens | ~4,800 |

With compressed candidate cards (250 tokens each) and smaller batch sizes (3 roles x 8 candidates), the total token usage is actually lower than the legacy approach. The quality improvement is significant: the LLM evaluates only pre-filtered candidates with focused attention.

## Testing checklist

1. **Unit test — shortlist scoring:** Create employees with varying title/skill/role-history overlap. Verify the deterministic score correctly ranks candidates.

2. **Unit test — shortlist size:** Create 50 employees, 10 roles. Verify each role gets exactly 15 candidates.

3. **Unit test — Jaccard similarity:** Verify `_jaccard_similarity({'backend', 'engineer'}, {'backend', 'developer'})` returns expected value.

4. **Unit test — seniority alignment:** Verify a senior employee scores higher against a senior role than a junior employee.

5. **Unit test — role history boost:** Create an employee with past role "ML Engineer at Google". Verify they score high against an "ML Engineer" role even if current title is different.

6. **Unit test — small workspace fallback:** Create 15 employees. Verify the legacy batch-by-employee path is used.

7. **Integration test — full pipeline:** Run blueprint generation with shortlist+rerank for a 30-employee workspace. Verify matches are persisted and quality is at least equal to the legacy approach.

8. **Quality test — comparison:** Run both legacy and shortlist approaches on the same data. Compare fit_score distributions and rationale quality.

## Estimated scope

- 0 Django migrations
- ~100 lines new shortlist function and scoring helpers
- ~80 lines new rerank LLM function and schema
- ~40 lines modified matching orchestration
- ~20 lines fallback logic
