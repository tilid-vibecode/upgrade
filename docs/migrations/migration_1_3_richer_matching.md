# Migration 1.3 — Richer Employee-Role Matching Input

## Problem statement

Employee-role matching currently uses a severely truncated employee profile. The function `_load_employee_matching_inputs_sync` at `skill_blueprint/services.py:4010-4048` builds a payload with only:

- `full_name`
- `current_title`
- `org_units` (department names)
- `projects` (project names)
- `skills_from_evidence` (top 12 by weight, then truncated to top 6 at line 1721)

Meanwhile, `EmployeeCVProfile.extracted_payload` already contains rich structured data extracted by the CV LLM:

- **Role history** — previous companies, role titles, dates, responsibilities, achievements, domains, leadership signals
- **Achievements** — specific accomplishments with confidence scores
- **Domain experience** — industry verticals, product types, technical domains
- **Leadership signals** — team size, mentoring, cross-functional leadership, hiring

None of this reaches the matching LLM. The system prompt at line 1725-1750 only instructs the model to use "current_title alignment, org_unit relevance, project exposure, and demonstrated skills_from_evidence."

**Impact:** A CTO with 15 years of delivery leadership, ML research background, and 5 published papers is matched purely on "CEO" title + 6 skills. The model has no basis for a sophisticated fit assessment.

## Prerequisites

None (can be developed in parallel with migrations 1.1 and 1.2).

However, migration 1.1 amplifies the benefit — with provisional evidence, more skills reach the matching stage. If 1.1 is deployed first, this migration has richer skill input to work with.

## Model changes

None. All changes are in the service layer.

## Service changes

### File: `skill_blueprint/services.py`

#### Change 1: `_load_employee_matching_inputs_sync` (line 4010-4048)

**Current payload per employee:**
```python
{
    'employee_uuid': str,
    'full_name': str,
    'current_title': str,
    'org_units': list[str],
    'projects': list[str],
    'skills_from_evidence': list[dict],  # top 12 by weight
}
```

**New payload per employee:**
```python
{
    'employee_uuid': str,
    'full_name': str,
    'current_title': str,
    'org_units': list[str],
    'projects': list[str],
    'skills_from_evidence': list[dict],  # top 12 by weight (keep [:12] from loader)

    # NEW FIELDS from EmployeeCVProfile.extracted_payload
    'role_history': list[dict],          # last 5 roles, trimmed
    'achievements': list[dict],          # top 5 by confidence_score (float 0-1)
    'domain_experience': list[dict],     # top 6 domain items
    'leadership_signals': list[dict],    # top 5 leadership items
    'seniority': str,                    # from EmployeeCVProfile.seniority (also on model directly)
    'headline': str,                     # from EmployeeCVProfile.headline (also on model directly)
}
```

**Implementation:**

After the existing evidence query (line 4025-4028), add:

```python
cv_profile = EmployeeCVProfile.objects.filter(
    workspace=workspace,
    employee=employee,
    status=EmployeeCVProfile.Status.MATCHED,
).order_by('-updated_at').first()

extracted = (cv_profile.extracted_payload or {}) if cv_profile else {}

# IMPORTANT: Field names match CV_EXTRACTION_SCHEMA (cv_services.py:58-191)
# and the normalized payload from _normalize_cv_payload (cv_services.py:700)
role_history = (extracted.get('role_history') or [])[:5]
# Trim each role to essential fields only (reduce token usage)
# Use character cap per entry to prevent verbose CVs from blowing up prompts
role_history_trimmed = [
    {
        'company_name': r.get('company_name', '')[:80],       # NOT 'company'
        'role_title': r.get('role_title', '')[:80],
        'start_date': r.get('start_date', ''),                # NOT 'dates'
        'end_date': r.get('end_date', ''),
        'key_achievements': [a[:200] for a in (r.get('achievements') or [])[:3]],
        'domains': r.get('domains', [])[:5],
        'leadership_signals': [s[:150] for s in (r.get('leadership_signals') or [])[:3]],
    }
    for r in role_history
]

# IMPORTANT: The NORMALIZED payload (from _normalize_cv_payload) uses 'confidence_score'
# (float 0.0-1.0) for role_history, achievements, domain_experience, leadership_signals.
# Skills have BOTH 'confidence' (int 0-100) and 'confidence_score' (float 0-1).
# Always read 'confidence_score' from the normalized payload, not raw 'confidence'.
achievements = sorted(
    extracted.get('achievements') or [],
    key=lambda a: float(a.get('confidence_score', 0)),
    reverse=True,
)[:5]
achievements_trimmed = [
    {'summary': a.get('summary', '')[:200], 'confidence_score': a.get('confidence_score', 0)}
    for a in achievements
]

domain_experience = [
    {'domain': d.get('domain', '')[:100], 'confidence_score': d.get('confidence_score', 0)}
    for d in (extracted.get('domain_experience') or [])[:6]
]
leadership_signals = [
    {'signal': s.get('signal', '')[:150], 'confidence_score': s.get('confidence_score', 0)}
    for s in (extracted.get('leadership_signals') or [])[:5]
]
```

**Character budget:** Each entry is capped (80 chars for names, 200 for achievements, 150 for signals) to prevent verbose CVs from exceeding token limits. Total per-employee payload stays under ~800 tokens even with maximum-length fields.

Then add these to the payload dict.

**Semantic note:** Blueprint-time employee-role matching is a **role-fit preview** — a heuristic that helps the blueprint understand current team shape. It is NOT the authoritative evidence stage. The authoritative evidence stage (assessments, matrix, plans) runs after blueprint publication. This distinction should be documented in the system prompt.

#### Change 2: Remove skill truncation at line 1721

**Before:**
```python
'skills_from_evidence': emp.get('skills_from_evidence', [])[:6],
```

**After:**
```python
'skills_from_evidence': emp.get('skills_from_evidence', []),
```

The loader already limits to 12 skills (line 4028). No need for a second truncation at the batch-building stage.

#### Change 3: Reduce batch size

**Before:**
```python
_EMPLOYEE_MATCH_BATCH_SIZE = 8
```

**After:**
```python
_EMPLOYEE_MATCH_BATCH_SIZE = 5
```

Each employee profile is now ~3x larger due to role history, achievements, etc. Reducing batch size from 8 to 5 keeps the total LLM prompt within token limits. The net effect is more LLM calls but each with better input quality.

Also update `max_tokens` calculation at line 1764:
```python
max_tokens=1200 + 800 * len(batch),  # was: 1200 + 600 * len(batch)
```

#### Change 4: Update system prompt (line 1725-1750)

**Add to the `## Matching rules` section:**

```
- Use role_history to assess career trajectory and depth:
  - If an employee has 3+ years in a role aligned to the target, boost fit significantly.
  - Prior experience at companies in the same domain is a strong signal.
  - Career progression (IC -> lead -> manager) indicates leadership readiness.
- Use achievements to assess demonstrated impact:
  - Achievements that directly relate to the role's required outcomes are strong evidence.
  - Quantified achievements (revenue, team size, users) carry more weight.
- Use domain_experience to assess market and product fit:
  - Domain overlap with the role's target market or product area is a moderate signal.
- Use leadership_signals to assess management and mentoring readiness:
  - For senior/lead roles, leadership signals are a strong differentiator.
  - For IC roles, leadership signals are a weak positive signal.
- When role_history, achievements, or domain_experience are available, they should
  carry MORE weight than skills_from_evidence alone. A person who shipped a ML product
  at scale is a better ML lead candidate than someone who lists "machine learning" as a skill.
```

**Update the batch_profiles construction (line 1714-1723):**

```python
batch_profiles = [
    {
        'employee_uuid': emp['employee_uuid'],
        'full_name': emp['full_name'],
        'current_title': emp['current_title'],
        'seniority': emp.get('seniority', ''),
        'headline': emp.get('headline', ''),
        'org_units': emp['org_units'],
        'projects': emp['projects'],
        'skills_from_evidence': emp.get('skills_from_evidence', []),
        'role_history': emp.get('role_history', []),        # trimmed in loader
        'achievements': emp.get('achievements', []),        # trimmed in loader
        'domain_experience': emp.get('domain_experience', []),  # trimmed in loader
        'leadership_signals': emp.get('leadership_signals', []),  # trimmed in loader
    }
    for emp in batch
]
```

## API changes

None. The matching output format (`EmployeeRoleMatch` model fields) stays the same. The improvement is in input quality, which produces better `fit_score` and `rationale` values.

## Token budget analysis

**Before (per employee in batch):**
- title + org_units + projects + 6 skills = ~150-250 tokens

**After (per employee in batch):**
- title + headline + seniority + org_units + projects + 12 skills + 5 role_history + 5 achievements + 6 domain + 5 leadership = ~400-700 tokens

**Before (per batch call):** 8 employees x ~200 tokens = ~1,600 tokens employee data
**After (per batch call):** 5 employees x ~550 tokens = ~2,750 tokens employee data

The role catalog portion of the prompt stays the same (~1,000-2,000 tokens). Total prompt size increases from ~3,600 to ~4,750 tokens — well within model limits.

**Cost impact:** For a 20-person workspace:
- Before: 3 LLM calls (20/8 rounded up)
- After: 4 LLM calls (20/5 rounded up)
- Net: 1 additional call per blueprint generation (~$0.01 at gpt-4o-mini rates)

## Expected quality improvements

1. **Senior profiles get appropriate matches** — a CTO with 15 years of engineering leadership will now be matched to leadership/architecture roles, not just "engineer" based on title.

2. **Career trajectory informs fit** — someone who progressed from IC to team lead to director over 10 years has demonstrable leadership growth that the model can assess.

3. **Domain expertise differentiates** — two "Backend Engineers" where one has 5 years in fintech and the other in gaming will be differentiated for domain-specific roles.

4. **Achievement quality strengthens rationale** — instead of "strong candidate based on title alignment", the rationale can cite "shipped ML inference pipeline serving 10M requests/day at [Company], directly relevant to the AI features initiative."

## Testing checklist

1. **Unit test — rich profile reaches LLM prompt:** Create an employee with rich CV profile (role history, achievements, domain, leadership). Mock the LLM call. Verify the user prompt sent to LLM contains all new fields.

2. **Unit test — graceful degradation without CV profile:** Create an employee with no `EmployeeCVProfile`. Verify matching still works with just title + skills (empty lists for new fields).

3. **Unit test — batch size respects limit:** Create 12 employees. Verify 3 batches are created (12/5 = 2.4, rounded up to 3).

4. **Unit test — no skills truncation:** Create employee with 12 skills. Verify all 12 appear in the batch profile (no `[:6]` truncation).

5. **Integration test — better match quality:** Use a mock LLM. Feed two employees — one with rich role history matching a target role, one with only a matching title. Verify the model receives enough context to differentiate them.

6. **Performance test — token limits:** Create 5 employees with maximum-length profiles (5 role history entries each with 3 achievements, 6 domain entries, 5 leadership signals). Verify the total prompt stays under 8,000 tokens.

## Estimated scope

- 0 Django migrations
- ~40 lines changed in `_load_employee_matching_inputs_sync`
- ~20 lines changed in batch_profiles construction
- ~15 lines changed in system prompt
- ~5 lines changed (batch size, max_tokens)

## Codebase note: after 1.2 lands

Once migration 1.2 is deployed, `_load_employee_matching_inputs_sync` must explicitly exclude rejected/zero-weight skill evidence (`.filter(weight__gt=0)` or `.exclude(operator_action='rejected')`). This migration can be implemented before or after 1.2, but the evidence filter MUST be added once 1.2 exists.

## Codebase note: legacy path after 2.5 lands

The batch-size reduction and richer payload in this migration become the **legacy matching path** for small workspaces (< 20 employees) once migration 2.5 introduces shortlist + rerank for larger workspaces.
