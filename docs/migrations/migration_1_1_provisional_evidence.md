# Migration 1.1 — Provisional Skill Evidence Persistence

## Problem statement

When a CV is processed, the extractor returns 8-15 skills. Each skill is normalized via `normalize_skill_seed(..., allow_freeform=False)` in `org_context/skill_catalog.py:230`. When a skill does not match an existing ESCO skill or a `SkillResolutionOverride`, the function returns a payload with `needs_review: True` and `match_source: 'review_pending'` (line 264-274).

The caller, `resolve_workspace_skill_sync` at `org_context/skill_catalog.py:499`, checks:

```python
if normalized_skill.get('needs_review') and not allow_freeform:
    return None, normalized_skill, False
```

This returns `(None, normalized_skill, False)` — meaning no `Skill` object is created.

Then in `_persist_skill_evidence_rows` at `org_context/cv_services.py:1293-1304`, this call is made with `allow_freeform=False`:

```python
skill, normalized_skill, is_resolved = resolve_workspace_skill_sync(
    profile.workspace,
    raw_term=...,
    normalized_skill=normalized_skill,
    ...
    allow_freeform=False,
)
if not is_resolved or skill is None:
    pending_skill_candidates.append({...})
    continue
```

The `continue` at line 1325 means the skill never becomes an `EmployeeSkillEvidence` row. It is stored only as a JSON blob inside `profile.metadata['pending_skill_candidates']` (at `cv_services.py:1500-1501`), invisible to all downstream stages — role matching, assessments, matrix, plans.

**Impact:** A senior CV with 12 extracted skills where 8 are non-ESCO (e.g. "PLG strategy", "growth loops", "event instrumentation", "team topology design") keeps only 4 skills as real evidence. The employee appears thin in matching and assessment.

## Goal

Every extracted skill becomes a queryable `EmployeeSkillEvidence` row, regardless of ESCO resolution status. Unresolved skills get a provisional `Skill` record with `resolution_status='pending_review'` that operators can later accept, merge, or reject (covered in migration 1.2).

## Model changes

### File: `org_context/models.py` — `Skill` model (line 955)

Add two fields after the existing `metadata` field:

```python
class Skill(TimestampedModel):
    # ... existing fields ...
    metadata = models.JSONField(default=dict, blank=True)

    # NEW FIELDS
    resolution_status = models.CharField(
        max_length=32,
        choices=[
            ('resolved', 'Resolved'),
            ('pending_review', 'Pending review'),
            ('rejected', 'Rejected'),
        ],
        default='resolved',
        db_index=True,
    )
    is_operator_confirmed = models.BooleanField(default=False)
```

**Default value rationale:** `default='resolved'` ensures all existing Skill rows (which have already been ESCO-matched or override-matched) are automatically correct without a data migration.

Also add an optional field to store raw source term provenance on provisional skills, making later merge review easier:

```python
    source_terms = models.JSONField(
        default=list,
        blank=True,
        help_text='Raw terms from CV extraction that created this provisional skill. Aids merge review.',
    )
```

### Django migration

Create `org_context/migrations/0017_skill_resolution_fields.py`:
- `AddField('skill', 'resolution_status', ...)`
- `AddField('skill', 'is_operator_confirmed', ...)`
- `AddField('skill', 'source_terms', ...)` — the JSONField for raw term provenance

## Service changes

### File: `org_context/skill_catalog.py`

#### Change 1: `resolve_workspace_skill_sync` (line 499)

**Before:**
```python
if normalized_skill.get('needs_review') and not allow_freeform:
    return None, normalized_skill, False
```

**After:**
```python
if normalized_skill.get('needs_review') and not allow_freeform:
    skill = ensure_workspace_skill_sync(
        workspace,
        normalized_skill=normalized_skill,
        preferred_display_name_ru=preferred_display_name_ru,
        aliases=aliases,
        created_source=created_source,
        promote_aliases=promote_aliases,
        resolution_status='pending_review',
    )
    return skill, normalized_skill, True
```

The function now always returns a `Skill` object. The third return value is changed from a resolution boolean to a persistence boolean — rename it from `is_resolved` to `was_persisted` in callers for semantic clarity. The provisional nature of the skill is encoded in `skill.resolution_status` rather than in a `None` return.

**Important:** When an existing `Skill` with the same `canonical_key` already exists (from a previous CV or from another employee), do NOT downgrade its `resolution_status`. If it was already `resolved`, keep it `resolved`. Only set `resolution_status='pending_review'` on newly created skills.

#### Change 2: `ensure_workspace_skill_sync` (find this function — it creates `Skill` records)

Add an optional `resolution_status` parameter:

```python
def ensure_workspace_skill_sync(
    workspace,
    *,
    normalized_skill,
    preferred_display_name_ru='',
    aliases=None,
    created_source='catalog_seed',
    promote_aliases=True,
    resolution_status='resolved',  # NEW PARAMETER
):
```

When creating a new `Skill`, pass `resolution_status=resolution_status` and `is_operator_confirmed=False` if `resolution_status != 'resolved'`. Also populate `source_terms` with the raw term and aliases used during creation.

When an existing skill is found by `canonical_key` (get_or_create), do NOT downgrade an already-resolved skill to `pending_review`. Only set `resolution_status` on creation. Append new raw terms to `source_terms` for audit purposes.

### File: `org_context/cv_services.py`

#### Change 3: `_persist_skill_evidence_rows` (line 1274-1348)

The `if not is_resolved or skill is None` branch at line 1306 will never trigger for the CV evidence path anymore, since `resolve_workspace_skill_sync` now always returns a Skill. However, keep the defensive check:

```python
if skill is None:
    # Should not happen after migration 1.1, but keep as safety net
    pending_skill_candidates.append({...})
    continue
```

Also add `resolution_status` to the evidence metadata so the UI can visually distinguish provisional evidence:

```python
EmployeeSkillEvidence.objects.create(
    # ... existing fields ...
    metadata={
        # ... existing metadata ...
        'resolution_status': skill.resolution_status,  # NEW
    },
)
```

Still populate `pending_skill_candidates` in `_persist_cv_payload_sync` (line 1497-1501) for backward compatibility with the review UI, but now every pending candidate ALSO has a real evidence row.

## API changes

None in this migration. The existing `GET /api/v1/workspaces/{slug}/org-context/employees/{uuid}/evidence` response already returns skill evidence — it will now return more rows.

Frontend can use `metadata.resolution_status` to visually distinguish provisional skills (e.g. dotted border, amber badge) from resolved ones.

## Data flow after this change

```
CV text
  |
  v
LLM extraction (8-15 skills)
  |
  v
normalize_skill_seed (per skill)
  |
  +-- ESCO match found --> Skill(resolution_status='resolved')
  |
  +-- Override match found --> Skill(resolution_status='resolved')
  |
  +-- No match (needs_review) --> Skill(resolution_status='pending_review')  <-- NEW
  |
  v
EmployeeSkillEvidence created for ALL skills  <-- CHANGED (was: only resolved)
  |
  v
Vector indexing (all evidence indexed)
  |
  v
Available for: role matching, assessments, matrix, plans
```

## Critical: patch the legacy approval path

The existing `_approve_pending_skill_candidate_sync` at `cv_services.py:2104` was designed for a world where unresolved skills had NO `Skill` or `EmployeeSkillEvidence` rows. After this migration, provisional skills already exist as real rows. The legacy function must be patched in this same migration to avoid creating duplicate skills or orphaned evidence:

1. When approving a pending candidate, check if a provisional `Skill` with the same `canonical_key` already exists. If so, update its `resolution_status` to `resolved` and `is_operator_confirmed` to `True`, rather than creating a new skill.
2. Check if `EmployeeSkillEvidence` rows already exist for this employee + skill. If so, update them (set `is_operator_confirmed=True` once migration 1.2 adds that field) rather than creating duplicates.
3. Mark the legacy approval path as deprecated — it will be superseded by the bulk review API in migration 1.2, but must remain functional until then.

If this is not patched, the old approval UI will create duplicate skills and evidence rows alongside the provisional ones.

## CV rebuild behavior — important safety note

The function `_persist_cv_payload_sync` at `cv_services.py:1446-1450` deletes and recreates ALL `EmployeeSkillEvidence` rows for a source on every CV rebuild:

```python
EmployeeSkillEvidence.objects.filter(
    workspace=workspace, source=source, source_kind=_CV_EVIDENCE_SOURCE_KIND,
).delete()
```

This means operator review decisions stored only on `EmployeeSkillEvidence` rows (added in migration 1.2) will be lost on rebuild. Migration 1.2 MUST add a durable review registry that survives CV rebuilds. This migration (1.1) is safe because `resolution_status` lives on the `Skill` row (not the evidence row), and `SkillResolutionOverride` records are durable.

## What this does NOT change

- The `CatalogResolutionReviewItem` mechanism — still records unresolved terms for workspace-level review
- The `SkillResolutionOverride` mechanism — still used for operator-approved term mappings
- Blueprint-side skill creation via `ensure_workspace_skill_sync` — blueprint skills already use `allow_freeform=True` effectively, so they are unaffected.

## Backward compatibility

Fully backward compatible:
- Existing `Skill` rows get `resolution_status='resolved'` via default — no data migration needed.
- Existing `EmployeeSkillEvidence` rows are unaffected.
- Existing API responses gain more rows (additive, not breaking).
- The `pending_skill_candidates` metadata field is still populated for backward compat with any UI that reads it.

## Testing checklist

1. **Unit test — all skills become evidence:**
   Extract a test CV with 10 skills where 4 match ESCO and 6 do not. Verify all 10 become `EmployeeSkillEvidence` rows. Verify the 6 unresolved ones have `skill.resolution_status='pending_review'`.

2. **Unit test — no duplicate skills on re-extraction:**
   Run CV extraction twice for the same employee. Verify old evidence rows are deleted (line 1446-1450 already handles this) and new ones created. Verify no duplicate `Skill` records for the same `canonical_key`.

3. **Unit test — existing resolved skills unchanged:**
   Process a CV where all skills match ESCO. Verify all get `resolution_status='resolved'` and `is_operator_confirmed=False`.

4. **Unit test — ensure_workspace_skill_sync idempotency:**
   Call `ensure_workspace_skill_sync` twice with the same `canonical_key` and `resolution_status='pending_review'`. Verify only one Skill is created. Verify calling it a third time with `resolution_status='resolved'` does NOT downgrade the existing skill.

5. **Integration test — downstream visibility:**
   After CV extraction with provisional skills, call `_load_employee_matching_inputs_sync`. Verify provisional skills appear in `skills_from_evidence`.

6. **Regression test — blueprint skill creation unaffected:**
   Run `generate_skill_blueprint`. Verify blueprint-created skills still get `resolution_status='resolved'`.

7. **Regression test — legacy approval path after provisional evidence:**
   Create a provisional skill via CV extraction. Call `_approve_pending_skill_candidate_sync` for that skill. Verify it upgrades the existing Skill to `resolved` and does NOT create a duplicate. Verify existing `EmployeeSkillEvidence` rows are updated, not duplicated.

## Estimated scope

- 1 Django migration file (schema only, no data migration)
- ~20 lines changed in `skill_catalog.py`
- ~10 lines changed in `cv_services.py`
- ~5 lines changed in `ensure_workspace_skill_sync`
