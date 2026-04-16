# Migration 3.2 — Default Context Backfill and Auto-Creation

## Problem statement

With the `PlanningContext`, `ContextProfile`, and `PlanningContextSource` models in place (migration 3.1), existing workspaces need default contexts created, and new workspaces should auto-create them.

## Prerequisites

- Migration 3.1 (PlanningContext schema exists)

## Model changes

None (models created in 3.1).

## Data migration

### File: `org_context/migrations/0021_backfill_planning_contexts.py`

This is a Django data migration that creates default planning contexts for existing workspaces.

```python
from django.db import migrations

def backfill_planning_contexts(apps, schema_editor):
    IntakeWorkspace = apps.get_model('company_intake', 'IntakeWorkspace')
    PlanningContext = apps.get_model('org_context', 'PlanningContext')
    ContextProfile = apps.get_model('org_context', 'ContextProfile')
    PlanningContextSource = apps.get_model('org_context', 'PlanningContextSource')
    WorkspaceSource = apps.get_model('company_intake', 'WorkspaceSource')

    SOURCE_KIND_TO_USAGE = {
        'roadmap': 'roadmap',
        'strategy': 'strategy',
        'job_description': 'role_reference',
        'org_csv': 'org_structure',
        'employee_cv': 'employee_cv',
        'existing_matrix': 'other',
        'other': 'other',
    }

    for workspace in IntakeWorkspace.objects.all():
        # Skip if already has a planning context
        if PlanningContext.objects.filter(workspace=workspace).exists():
            continue

        # Create default org-level context
        context = PlanningContext.objects.create(
            workspace=workspace,
            organization=None,  # Will be linked later when workspace gets org FK
            name=workspace.name,
            slug='org-baseline',
            kind='org',
            status='active',
            description=f'Default organization baseline for workspace {workspace.name}',
        )

        # Create context profile from workspace metadata
        workspace_meta = workspace.metadata or {}
        ContextProfile.objects.create(
            planning_context=context,
            company_profile=workspace_meta.get('company_profile', {}),
            tech_stack=workspace_meta.get('tech_stack', []),
            constraints=workspace_meta.get('constraints', []),
            growth_goals=workspace_meta.get('growth_goals', []),
            inherit_from_parent=False,  # Root context doesn't inherit
            override_fields=[],
        )

        # Link all existing sources to the default context
        for source in WorkspaceSource.objects.filter(workspace=workspace):
            usage_type = SOURCE_KIND_TO_USAGE.get(source.source_kind, 'other')
            PlanningContextSource.objects.create(
                planning_context=context,
                workspace_source=source,
                usage_type=usage_type,
                is_active=True,
                include_in_blueprint=True,
                include_in_roadmap_analysis=(usage_type in ('roadmap', 'strategy')),
            )


def reverse_backfill(apps, schema_editor):
    PlanningContext = apps.get_model('org_context', 'PlanningContext')
    # Only delete auto-created contexts (slug='org-baseline')
    PlanningContext.objects.filter(slug='org-baseline').delete()


class Migration(migrations.Migration):
    dependencies = [
        ('org_context', '0020_planning_context'),
        ('company_intake', '0XXX_latest'),  # whatever the latest company_intake migration is
    ]

    operations = [
        migrations.RunPython(backfill_planning_contexts, reverse_backfill),
    ]
```

**Idempotency:** The migration checks specifically for the default org context (not any context) to avoid skipping partially initialized workspaces:
```python
if PlanningContext.objects.filter(workspace=workspace, slug='org-baseline', kind='org').exists():
    continue
```

**Reverse migration safety:** The reverse function should only delete auto-created contexts, not operator-created ones. Tag auto-created contexts in metadata:
```python
context = PlanningContext.objects.create(
    ...,
    metadata={'auto_created': True, 'created_by': 'backfill_migration_0021'},
)
```
Reverse deletes only rows with `metadata__auto_created=True`.

## Service changes

### File: `company_intake/services.py`

#### Change 1: Auto-create default PlanningContext on workspace creation

Find the workspace creation function and add:

```python
from org_context.models import PlanningContext, ContextProfile

def _create_default_planning_context(workspace: IntakeWorkspace):
    """Create a default org-level planning context for a new workspace."""
    context = PlanningContext.objects.create(
        workspace=workspace,
        name=workspace.name,
        slug='org-baseline',
        kind=PlanningContext.Kind.ORG,
        status=PlanningContext.Status.ACTIVE,
    )
    ContextProfile.objects.create(
        planning_context=context,
        inherit_from_parent=False,
    )
    return context
```

Call this after workspace creation in the relevant service function.

#### Change 2: Auto-link new sources to default context

When a `WorkspaceSource` is created, also create a `PlanningContextSource` linking it to the workspace's default context:

```python
from org_context.models import PlanningContextSource

def _link_source_to_default_context(source: WorkspaceSource):
    """Link a new source to the workspace's default planning context."""
    default_context = PlanningContext.objects.filter(
        workspace=source.workspace,
        kind=PlanningContext.Kind.ORG,
    ).first()
    if default_context is None:
        return

    SOURCE_KIND_TO_USAGE = {
        'roadmap': 'roadmap',
        'strategy': 'strategy',
        'job_description': 'role_reference',
        'org_csv': 'org_structure',
        'employee_cv': 'employee_cv',
        'existing_matrix': 'other',
        'other': 'other',
    }
    usage_type = SOURCE_KIND_TO_USAGE.get(source.source_kind, 'other')

    PlanningContextSource.objects.get_or_create(
        planning_context=default_context,
        workspace_source=source,
        defaults={
            'usage_type': usage_type,
            'is_active': True,
            'include_in_blueprint': True,
            'include_in_roadmap_analysis': (usage_type in ('roadmap', 'strategy')),
        },
    )
```

### File: `org_context/models.py`

#### Helper: `resolve_effective_profile`

Implement the profile resolution algorithm documented in migration 3.1:

```python
@staticmethod
def resolve_effective_profile(planning_context):
    """Resolve the effective profile by walking up the parent chain."""
    chain = []
    current = planning_context
    while current is not None:
        chain.append(current)
        current = current.parent_context
    chain.reverse()

    effective = {
        'company_profile': {},
        'tech_stack': [],
        'constraints': [],
        'growth_goals': [],
    }

    for ctx in chain:
        profile = getattr(ctx, 'profile', None)
        if profile is None:
            continue
        if not profile.inherit_from_parent:
            effective = {
                'company_profile': profile.company_profile or {},
                'tech_stack': profile.tech_stack or [],
                'constraints': profile.constraints or [],
                'growth_goals': profile.growth_goals or [],
            }
        else:
            for field in (profile.override_fields or []):
                value = getattr(profile, field, None)
                if value is not None:
                    if field == 'tech_stack':
                        # Add new items, then remove excluded items
                        combined = list(set(effective['tech_stack'] + (value or [])))
                        removals = set(profile.tech_stack_remove or [])
                        effective['tech_stack'] = [t for t in combined if t not in removals]
                    elif field == 'company_profile':
                        effective['company_profile'] = {**effective['company_profile'], **(value or {})}
                    else:
                        effective[field] = value

    # Preserve stable ordering for tech_stack
    effective['tech_stack'] = sorted(set(effective['tech_stack']))

    return effective
```

#### Helper: `resolve_effective_sources`

**Critical:** The naive approach of filtering `is_active=True` across all ancestor contexts is INCORRECT. A child-level inactive link must shadow (override) the parent-level active link for the same physical source. Without this, operators cannot exclude an inherited source from a project context.

```python
@staticmethod
def resolve_effective_sources(planning_context):
    """
    Resolve effective sources for a planning context using nearest-context-wins.

    For each workspace_source, prefer the PlanningContextSource record from the
    nearest context in the ancestry chain. A child-level is_active=False shadows
    (overrides) a parent-level is_active=True for the same source.
    """
    # Build ancestry chain: [self, parent, grandparent, ...]
    chain = []
    current = planning_context
    while current is not None:
        chain.append(current.pk)
        current = current.parent_context

    # Load all source links from the ancestry chain
    all_links = list(
        PlanningContextSource.objects.filter(
            planning_context_id__in=chain,
        ).select_related('workspace_source', 'planning_context')
    )

    # Build a priority map: context_pk -> position (0 = self = highest priority)
    priority = {pk: i for i, pk in enumerate(chain)}

    # Group by workspace_source_id, keep nearest context's record
    best_by_source = {}
    for link in all_links:
        source_id = link.workspace_source_id
        link_priority = priority.get(link.planning_context_id, 999)
        if source_id not in best_by_source or link_priority < best_by_source[source_id][0]:
            best_by_source[source_id] = (link_priority, link)

    # Return only active links after shadowing
    return [
        link for _, link in best_by_source.values()
        if link.is_active
    ]
```

### Profile adapter for backward compatibility

The existing `build_workspace_profile_snapshot(workspace)` function returns a specific shape consumed by blueprint generation. The new `resolve_effective_profile(planning_context)` must return a compatible shape, or an explicit adapter must convert between them:

```python
def context_profile_to_workspace_profile_snapshot(effective_profile: dict) -> dict:
    """Convert PlanningContext effective profile to the shape expected by
    _build_blueprint_inputs_sync and _extract_blueprint_with_llm."""
    return {
        'company_profile': effective_profile.get('company_profile', {}),
        'pilot_scope': {},  # Not yet context-scoped; inherit from workspace
        # ... map other fields as needed
    }
```

This adapter ensures that context-scoped blueprint generation (migration 3.3) can use the same downstream blueprint code without refactoring it.

## API endpoints

Planning-context endpoints use a consistent prefix: `/api/v1/prototype/workspaces/{slug}/planning-contexts/...`

### File: `org_context/prototype_fastapi_views.py`

#### Endpoint 1: List planning contexts

```
GET /api/v1/prototype/workspaces/{workspace_slug}/planning-contexts
```

**Response:**
```json
{
    "contexts": [
        {
            "uuid": "...",
            "name": "Acme Corp Baseline",
            "slug": "org-baseline",
            "kind": "org",
            "status": "active",
            "parent_context_uuid": null,
            "child_count": 2,
            "source_count": 5,
            "has_blueprint": true,
            "has_roadmap_analysis": false
        },
        {
            "uuid": "...",
            "name": "AI Features",
            "slug": "ai-features",
            "kind": "project",
            "status": "active",
            "parent_context_uuid": "...",
            "child_count": 0,
            "source_count": 2,
            "has_blueprint": false,
            "has_roadmap_analysis": false
        }
    ]
}
```

#### Endpoint 2: Create planning context

```
POST /api/v1/prototype/workspaces/{workspace_slug}/planning-contexts
```

**Request body:**
```json
{
    "name": "AI Features",
    "slug": "ai-features",
    "kind": "project",
    "parent_context_uuid": "...",
    "project_uuid": "...",
    "description": "Planning scope for the AI features initiative",
    "profile": {
        "tech_stack": ["PyTorch", "FastAPI", "Redis"],
        "override_fields": ["tech_stack"]
    }
}
```

#### Endpoint 3: Get context detail with resolved profile

```
GET /api/v1/prototype/workspaces/{workspace_slug}/planning-contexts/{context_slug}
```

**Response:**
```json
{
    "uuid": "...",
    "name": "AI Features",
    "slug": "ai-features",
    "kind": "project",
    "status": "active",
    "parent_context": {
        "uuid": "...",
        "name": "Acme Corp Baseline",
        "slug": "org-baseline"
    },
    "profile": {
        "company_profile": {"name": "Acme Corp", "what_it_does": "..."},
        "tech_stack": ["Django", "React", "PostgreSQL", "PyTorch", "FastAPI", "Redis"],
        "constraints": [],
        "growth_goals": []
    },
    "effective_profile": {
        "company_profile": {"name": "Acme Corp", "what_it_does": "..."},
        "tech_stack": ["Django", "React", "PostgreSQL", "PyTorch", "FastAPI", "Redis"],
        "constraints": ["Budget: $200k for AI initiative"],
        "growth_goals": ["Launch AI tutoring by Q3"]
    },
    "sources": [
        {"uuid": "...", "title": "AI Roadmap", "usage_type": "roadmap", "origin": "direct"},
        {"uuid": "...", "title": "Company Strategy 2026", "usage_type": "strategy", "origin": "inherited"},
        {"uuid": "...", "title": "Legacy Roadmap", "usage_type": "roadmap", "origin": "excluded", "excluded_reason": "deactivated at project level"}
    ]
}
```

#### Endpoint 4: Update context profile

```
PATCH /api/v1/prototype/workspaces/{workspace_slug}/planning-contexts/{context_slug}
```

#### Endpoint 5: Manage context sources

```
POST /api/v1/prototype/workspaces/{workspace_slug}/planning-contexts/{context_slug}/sources
```

**Request body:**
```json
{
    "workspace_source_uuid": "...",
    "usage_type": "roadmap",
    "include_in_blueprint": true,
    "include_in_roadmap_analysis": true
}
```

```
DELETE /api/v1/prototype/workspaces/{workspace_slug}/planning-contexts/{context_slug}/sources/{source_link_uuid}
```

## Backward compatibility

All existing behavior is preserved:
- Workspaces that had no PlanningContext now have a default `org-baseline` context
- All existing sources are linked to the default context
- Blueprint generation, roadmap analysis, and all other services continue to work with workspace-level scoping
- The planning context system is additive — it doesn't change any existing queries or flows until migration 3.3 adds context-scoped runs

## Testing checklist

1. **Data migration test:** Create 3 workspaces with sources. Run migration. Verify each workspace has a `PlanningContext(kind='org')` with linked sources.

2. **Auto-creation test:** Create a new workspace after migration. Verify default PlanningContext is auto-created.

3. **Source auto-linking test:** Add a new source to a workspace. Verify `PlanningContextSource` is created linking it to the default context.

4. **Profile resolution test:** Create org context with company_profile. Create project context inheriting from org but overriding tech_stack. Verify `resolve_effective_profile` returns merged result.

5. **Source resolution test:** Create org context with 3 sources. Create project context inheriting sources but deactivating 1. Verify `resolve_effective_sources` returns 2 active sources.

6. **Source shadowing test:** Create org context with source A active. Create project context inheriting from org with source A inactive. Call `resolve_effective_sources` for project context. Verify source A is NOT returned (child inactive shadows parent active).

7. **Tech stack removal test:** Create org context with tech_stack=["Django", "React", "Java"]. Create project context with tech_stack=["PyTorch"], tech_stack_remove=["Java"], override_fields=["tech_stack"]. Verify effective tech_stack is ["Django", "PyTorch", "React"] (sorted, Java removed).

8. **API test — list contexts:** Verify the listing endpoint returns correct context tree.

7. **API test — create project context:** Create a project context under the org baseline. Verify parent-child relationship.

8. **API test — context detail with effective profile:** Verify inheritance merging is correct in the response.

9. **Regression test:** Verify blueprint generation still works without specifying a planning context.

## Estimated scope

- 1 Django data migration
- ~40 lines auto-creation logic in `company_intake/services.py`
- ~40 lines profile/source resolution helpers
- ~100 lines API endpoints in `prototype_fastapi_views.py`
- ~30 lines Pydantic schemas
