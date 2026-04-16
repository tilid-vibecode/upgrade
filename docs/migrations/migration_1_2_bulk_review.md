# Migration 1.2 — Bulk Skill Review API and UX

## Problem statement

After migration 1.1, unresolved skills become real `EmployeeSkillEvidence` rows with `Skill.resolution_status='pending_review'`. Operators need a way to review, approve, merge, or reject these provisional skills efficiently.

The current review mechanism (`_approve_pending_skill_candidate_sync` at `cv_services.py:2104`) works one skill at a time, per CV source. An operator with 20 employees each having 5 pending skills must make 100 individual API calls. This is unusable at production scale.

The upgrade needs two review scopes:

1. **Employee-level bulk review** — "For this employee, accept all high-confidence skills, reject these 2, merge this alias"
2. **Workspace-level unknown-skill queue** — "Across all employees, 'PLG strategy' appears 4 times — merge it with 'Growth Strategy' or approve as a new skill"

## Prerequisites

- Migration 1.1 completed (Skill has `resolution_status`, `is_operator_confirmed` fields)

## Model changes

### File: `org_context/models.py` — `EmployeeSkillEvidence` (line 1073)

Add three fields after the existing `metadata` field:

```python
class EmployeeSkillEvidence(TimestampedModel):
    # ... existing fields ...
    metadata = models.JSONField(default=dict, blank=True)

    # NEW FIELDS
    is_operator_confirmed = models.BooleanField(
        default=False,
        help_text='True after an operator explicitly accepted this evidence row.',
    )
    operator_action = models.CharField(
        max_length=32,
        blank=True,
        default='',
        choices=[
            ('', ''),
            ('accepted', 'Accepted'),
            ('rejected', 'Rejected'),
            ('merged', 'Merged'),
        ],
        help_text='Last operator action on this evidence row.',
    )
    operator_note = models.TextField(
        blank=True,
        default='',
        help_text='Free-text note from operator review.',
    )
```

### New model: `SkillReviewDecision` — durable review registry

**Critical design point:** The function `_persist_cv_payload_sync` at `cv_services.py:1446-1450` deletes and recreates `EmployeeSkillEvidence` rows on every CV rebuild. Operator decisions stored only on evidence rows would be lost. A separate durable review record is required.

```python
class SkillReviewDecision(TimestampedModel):
    """
    Durable record of an operator's review decision for a specific
    employee + skill combination. Survives CV rebuilds because it is
    keyed by employee + skill canonical_key, not by evidence row UUID.

    When CV evidence is rebuilt, the persistence code checks for existing
    SkillReviewDecision records and re-applies them to new evidence rows.
    """
    workspace = models.ForeignKey(
        'company_intake.IntakeWorkspace',
        on_delete=models.CASCADE,
        related_name='skill_review_decisions',
    )
    employee = models.ForeignKey(
        'org_context.Employee',
        on_delete=models.CASCADE,
        related_name='skill_review_decisions',
    )
    skill_canonical_key = models.CharField(
        max_length=255,
        help_text='Canonical key of the skill being reviewed.',
    )
    action = models.CharField(
        max_length=32,
        choices=[
            ('accepted', 'Accepted'),
            ('rejected', 'Rejected'),
            ('merged', 'Merged'),
        ],
    )
    merge_target_skill_uuid = models.UUIDField(
        null=True, blank=True,
        help_text='Target skill UUID if action is merged.',
    )
    note = models.TextField(blank=True, default='')
    reviewed_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        indexes = [
            models.Index(fields=['workspace', 'employee']),
            models.Index(fields=['workspace', 'skill_canonical_key']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['workspace', 'employee', 'skill_canonical_key'],
                name='uq_skill_review_decision_per_employee',
            ),
        ]
```

**CV rebuild integration:** In `_persist_skill_evidence_rows`, after creating evidence rows, check for existing `SkillReviewDecision` records for this employee. Re-apply decisions:
- If `action='accepted'`: set `is_operator_confirmed=True` on the evidence row
- If `action='rejected'`: set `weight=0`, `operator_action='rejected'`
- If `action='merged'`: re-link to the merge target skill

### Django migration

Create `org_context/migrations/0018_skill_review_and_evidence_fields.py`:
- `CreateModel('SkillReviewDecision', ...)`
- `AddField('employeeskillevidence', 'is_operator_confirmed', ...)`
- `AddField('employeeskillevidence', 'operator_action', ...)`
- `AddField('employeeskillevidence', 'operator_note', ...)`

## New service functions

### File: `org_context/cv_services.py` — add after line 2243

#### Function 1: `bulk_review_employee_skills`

```python
def bulk_review_employee_skills(
    workspace_pk,
    employee_uuid: str,
    actions: list[dict],
) -> dict:
    """
    Apply bulk review actions to an employee's skill evidence.

    Each action dict:
    {
        "evidence_uuid": str,        # UUID of EmployeeSkillEvidence
        "action": str,               # "accept" | "reject" | "merge"
        "merge_target_skill_uuid": str,  # required if action == "merge"
        "note": str,                 # optional operator note
    }

    Returns: {"processed": int, "accepted": int, "rejected": int, "merged": int, "errors": list}
    """
```

**Action semantics:**

- **accept**: Set `evidence.is_operator_confirmed = True`, `evidence.operator_action = 'accepted'`. Also set `evidence.skill.resolution_status = 'resolved'` and `evidence.skill.is_operator_confirmed = True`. Create a `SkillResolutionOverride(status=APPROVED)` so future CVs with the same term resolve automatically.

- **reject**: Set `evidence.operator_action = 'rejected'`, `evidence.weight = 0`. A rejected evidence row is NOT deleted — it stays for audit trail. Create a `SkillReviewDecision(action='rejected')` so the decision survives CV rebuilds. Also mark `evidence.skill.resolution_status = 'rejected'` if no other employees use this skill.

  **Critical:** Setting `weight=0` alone is insufficient. Multiple downstream consumers (e.g., `_load_employee_matching_inputs_sync` at line 4025) order by weight but do NOT filter `weight__gt=0`. This migration MUST also update the following downstream queries to explicitly exclude rejected rows:
  - `_load_employee_matching_inputs_sync` in `skill_blueprint/services.py` — add `.filter(weight__gt=0)` or `.exclude(operator_action='rejected')`
  - `index_employee_cv_profile_sync` in `org_context/vector_indexing.py` — skip rejected evidence from vector indexing
  - Any evidence aggregation in `evidence_matrix/` services

- **merge**: Re-link the evidence to a different Skill (the merge target). Set `evidence.operator_action = 'merged'`, `evidence.is_operator_confirmed = True`. Create a `SkillResolutionOverride` mapping the original term to the merge target. If the original Skill has no remaining evidence rows after merge, mark it `resolution_status = 'rejected'`.

All actions run inside `transaction.atomic()`. Also update `CatalogResolutionReviewItem` status to `RESOLVED` for accepted/merged terms.

#### Function 2: `list_workspace_pending_skills`

```python
def list_workspace_pending_skills(workspace_pk) -> list[dict]:
    """
    Return all skills with resolution_status='pending_review', grouped by skill.

    Each item:
    {
        "skill_uuid": str,
        "canonical_key": str,
        "display_name_en": str,
        "display_name_ru": str,
        "employee_count": int,          # how many employees have evidence for this skill
        "total_evidence_count": int,     # total evidence rows across all employees
        "avg_confidence": float,
        "sample_evidence_texts": list,   # first 3 evidence snippets
        "sample_employees": list,        # first 5 employee names
        "similar_resolved_skills": list, # existing resolved skills with similar names (for merge UI)
    }
    """
```

The `similar_resolved_skills` list enables the merge UI — operator sees "PLG Strategy" and the system suggests "Growth Strategy" (resolved, ESCO-backed) as a merge target.

Similarity candidates are found by (use DB-portable approaches, not trigram-specific extensions):
1. Checking `SkillAlias` for case-insensitive exact matches
2. Checking `Skill.display_name_en` with Python-side token overlap (Jaccard on normalized tokens)
3. Checking ESCO label search via existing `_find_matching_esco_skill` helper
4. Falling back to `SequenceMatcher` ratio for fuzzy candidates if token overlap is insufficient

#### Function 3: `bulk_resolve_workspace_skills`

```python
def bulk_resolve_workspace_skills(
    workspace_pk,
    resolutions: list[dict],
) -> dict:
    """
    Apply workspace-level skill resolutions.

    Each resolution dict:
    {
        "skill_uuid": str,
        "action": str,        # "approve" | "reject" | "merge" | "create_override"
        "target_skill_uuid": str,    # for merge
        "target_esco_uri": str,      # for create_override with ESCO mapping
        "display_name_en": str,      # for approve/create_override
        "display_name_ru": str,      # optional
        "alias_terms": list[str],    # additional aliases
    }

    Returns: {"processed": int, "approved": int, "rejected": int, "merged": int, "errors": list}
    """
```

**Action semantics:**

- **approve**: Mark `skill.resolution_status = 'resolved'`, `skill.is_operator_confirmed = True`. Optionally update display names. Create `SkillResolutionOverride(status=APPROVED)`. Mark all evidence rows for this skill as `is_operator_confirmed = True`.

- **reject**: Mark `skill.resolution_status = 'rejected'`. Set `weight = 0` on all evidence rows for this skill. **Durability across future rebuilds:** Create a `SkillResolutionOverride(status=REJECTED)` for the skill's canonical key and all known aliases/source terms. During `_normalize_cv_payload` (migration 1.1 path), `normalize_skill_seed` already checks overrides first — a rejected override will prevent the term from ever materializing as a new provisional skill again. Without this, a rejected term silently reappears when new CVs are processed or existing CVs are rebuilt.

- **merge**: Re-link ALL evidence rows from this skill to the target skill. Create `SkillResolutionOverride(status=APPROVED)` mapping the rejected skill's terms to the target canonical key. Mark the original skill `resolution_status = 'rejected'`. Future CV rebuilds will resolve the merged term directly to the target skill via the override.

- **create_override**: Create a `SkillResolutionOverride(status=APPROVED)` with an explicit ESCO mapping. Update the skill's `esco_skill` FK. Mark `resolution_status = 'resolved'`.

#### Function 4: `accept_all_high_confidence_skills`

```python
def accept_all_high_confidence_skills(
    workspace_pk,
    employee_uuid: str,
    *,
    confidence_threshold: float = 0.7,
) -> dict:
    """
    Auto-accept all pending_review evidence rows above the confidence threshold
    for a single employee. Convenience wrapper for the common case.

    Returns: {"accepted_count": int, "skipped_count": int}
    """
```

## API endpoints

### File: `org_context/prototype_fastapi_views.py`

#### Endpoint 1: Employee bulk review

```
POST /api/v1/prototype/workspaces/{workspace_slug}/org-context/employees/{employee_uuid}/skills/bulk-review
```

**Request body:**
```json
{
    "actions": [
        {"evidence_uuid": "...", "action": "accept"},
        {"evidence_uuid": "...", "action": "reject", "note": "Not relevant to current role"},
        {"evidence_uuid": "...", "action": "merge", "merge_target_skill_uuid": "..."}
    ]
}
```

**Response:**
```json
{
    "processed": 3,
    "accepted": 1,
    "rejected": 1,
    "merged": 1,
    "errors": []
}
```

#### Endpoint 2: Accept all high-confidence skills for an employee

```
POST /api/v1/prototype/workspaces/{workspace_slug}/org-context/employees/{employee_uuid}/skills/accept-all
```

**Request body:**
```json
{
    "confidence_threshold": 0.7
}
```

#### Endpoint 3: Workspace pending skill queue

```
GET /api/v1/prototype/workspaces/{workspace_slug}/org-context/skills/pending-review
```

**Response:**
```json
{
    "pending_skills": [
        {
            "skill_uuid": "...",
            "canonical_key": "plg-strategy",
            "display_name_en": "PLG Strategy",
            "employee_count": 4,
            "total_evidence_count": 6,
            "avg_confidence": 0.82,
            "sample_evidence_texts": ["Led PLG adoption across...", ...],
            "sample_employees": ["Alice Smith", "Bob Jones", ...],
            "similar_resolved_skills": [
                {"skill_uuid": "...", "display_name_en": "Growth Strategy", "esco_mapped": true}
            ]
        }
    ],
    "total_pending": 12,
    "total_resolved": 45
}
```

#### Endpoint 4: Workspace bulk resolve

```
POST /api/v1/prototype/workspaces/{workspace_slug}/org-context/skills/bulk-resolve
```

**Request body:**
```json
{
    "resolutions": [
        {"skill_uuid": "...", "action": "approve"},
        {"skill_uuid": "...", "action": "merge", "target_skill_uuid": "..."},
        {"skill_uuid": "...", "action": "reject"}
    ]
}
```

## Workspace-level reject durability via normalization

The key insight: workspace-level rejection must be durable across CV rebuilds through the **normalization layer**, not only through evidence-row mutation. The mechanism:

1. When a skill is rejected at workspace level, create a `SkillResolutionOverride(status=REJECTED)` for the term.
2. `normalize_skill_seed` at `org_context/skill_catalog.py:230` already checks overrides FIRST (line 238). If it finds a rejected override, it should return a payload with `needs_review: False` and a new flag `is_rejected: True`.
3. `resolve_workspace_skill_sync` checks this flag. If `is_rejected: True`, it does NOT create a `Skill` or `EmployeeSkillEvidence` row — the term is silently dropped during normalization.
4. This prevents the "zombie skill" problem: a workspace-level rejected term cannot reappear on future CV rebuilds or new employee CVs.

**Implementation in `normalize_skill_seed`:**

```python
override = _find_skill_resolution_override(cleaned, workspace=workspace)
if override is not None:
    if override.status == CatalogOverrideStatus.REJECTED:
        return {
            'canonical_key': override.canonical_key,
            'display_name_en': override.display_name_en,
            'is_rejected': True,
            'match_source': 'rejected_override',
        }
    return _build_skill_override_payload(override)
```

**Implementation in `resolve_workspace_skill_sync`:**

```python
if normalized_skill.get('is_rejected'):
    return None, normalized_skill, False  # Intentionally returns None — skill was rejected
```

**Implementation in `_persist_skill_evidence_rows`:**

The existing `if skill is None: continue` branch handles this — rejected skills produce no evidence rows.

## Interaction with existing approval mechanism

The existing `_approve_pending_skill_candidate_sync` at `cv_services.py:2104` and the public `approve_pending_skill_candidate` function remain for backward compatibility. However, the new bulk review functions are the preferred path.

After migration 1.1, the `pending_skill_candidates` metadata list may still be populated. The new bulk review functions operate on `EmployeeSkillEvidence` rows directly (which now exist for all skills), not on the metadata JSON. When a skill is approved via the new bulk review, also clean up the `pending_skill_candidates` metadata for consistency.

## Downstream effects

After an operator reviews skills:
- **Accepted skills** participate fully in role matching (migration 1.3), assessments, and matrix
- **Rejected skills** have `weight=0` and `operator_action='rejected'` — excluded by updated downstream queries AND removed from vector index (stale retrieval vectors for rejected evidence must be cleared)
- **Merged skills** are re-linked to the target skill — all downstream queries see the canonical skill, vector index entries are updated to reference the canonical skill

**Important:** After any bulk review action (accept, reject, merge), call `index_employee_cv_profile_sync(profile.pk)` to reindex the affected employee's CV evidence in Qdrant. Without reindexing, vector retrieval continues to surface rejected/merged evidence as if it were still active.

**Important:** Employee-level reject must NOT globally reject the shared `Skill` if other employees still have active evidence for the same skill. Only mark `skill.resolution_status = 'rejected'` when `EmployeeSkillEvidence.objects.filter(skill=skill, weight__gt=0).count() == 0`.
- The `CatalogResolutionReviewItem` table is updated, reducing the workspace-level review backlog
- `SkillResolutionOverride` records are created, so future CV extractions for the same terms resolve automatically

## Frontend guidance (not in scope of this migration, but for reference)

### Employee profile review screen

For each employee, show:
- All skills grouped by resolution status (resolved vs pending_review)
- Pending skills highlighted with confidence score and evidence snippets
- Bulk actions: "Accept all above 70%", "Select and reject", "Merge with..."
- After review, show updated skill count and resolution stats

### Workspace skill queue screen

Show a table of all pending_review skills across the workspace:
- Columns: skill name, employee count, avg confidence, similar skills
- Actions per row: approve, reject, merge (with autocomplete for merge target)
- Batch select and apply action to multiple skills

## Testing checklist

1. **Unit test — bulk accept:** Create employee with 5 pending_review evidence rows. Call `bulk_review_employee_skills` with all "accept". Verify all evidence rows have `is_operator_confirmed=True`, all skills have `resolution_status='resolved'`, `SkillResolutionOverride` records created.

2. **Unit test — bulk reject:** Reject 2 evidence rows. Verify `weight=0`, `operator_action='rejected'`. Verify downstream query `EmployeeSkillEvidence.objects.filter(weight__gt=0)` excludes them.

3. **Unit test — merge:** Merge skill A into skill B. Verify evidence rows now point to skill B. Verify skill A has `resolution_status='rejected'`. Verify `SkillResolutionOverride` maps A's terms to B.

4. **Unit test — workspace pending list:** Create 3 pending skills used by 2, 3, and 1 employees respectively. Verify `list_workspace_pending_skills` returns correct counts and sorted by employee_count descending.

5. **Unit test — workspace bulk resolve:** Approve 1, reject 1, merge 1 at workspace level. Verify ALL evidence rows for each skill are updated.

6. **Unit test — accept_all_high_confidence_skills:** Create 8 evidence rows (4 with confidence > 0.7, 4 with confidence < 0.7). Verify only the 4 high-confidence rows are accepted.

7. **Integration test — downstream exclusion:** Reject a skill, then run `_load_employee_matching_inputs_sync`. Verify rejected skills do not appear in `skills_from_evidence`.

8. **Regression test — existing approval path:** Verify `_approve_pending_skill_candidate_sync` still works for backward compat.

9. **Durability test — decisions survive CV rebuild:** Accept 3 skills and reject 1 for an employee. Trigger CV rebuild (`build_cv_evidence_for_workspace` with `force_rebuild=True`). Verify: accepted skills are re-confirmed, rejected skill has `weight=0` again, `SkillReviewDecision` records are unchanged.

10. **Vector reindex test:** Reject a skill. Verify the Qdrant index no longer contains evidence for that skill. Accept a skill. Verify the index is updated.

11. **Workspace-level reject anti-resurrection test:** Reject a skill at workspace level via `bulk_resolve_workspace_skills`. Verify `SkillResolutionOverride(status=REJECTED)` is created. Upload a new employee CV that contains the same skill term. Run `cv-evidence/build`. Verify the term does NOT produce a new `Skill` or `EmployeeSkillEvidence` row — normalization drops it via the rejected override.

12. **Workspace-level reject + re-approve test:** Reject a skill, then later approve it at workspace level. Verify the `SkillResolutionOverride.status` is updated from REJECTED to APPROVED. Verify future CV rebuilds now resolve the term normally.

## Estimated scope

- 1 Django migration file (SkillReviewDecision model + 3 fields on EmployeeSkillEvidence)
- ~200 lines new service functions in `cv_services.py`
- ~100 lines new API endpoints in `prototype_fastapi_views.py`
- ~20 lines Pydantic request/response schemas
