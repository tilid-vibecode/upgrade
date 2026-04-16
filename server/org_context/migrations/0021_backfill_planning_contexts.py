from django.db import migrations


SOURCE_KIND_TO_USAGE = {
    'roadmap': 'roadmap',
    'strategy': 'strategy',
    'job_description': 'role_reference',
    'org_csv': 'org_structure',
    'employee_cv': 'employee_cv',
    'existing_matrix': 'other',
    'other': 'other',
}


def _dedupe_strings(values):
    seen = set()
    result = []
    for value in values or []:
        item = str(value or '').strip()
        if not item:
            continue
        lowered = item.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(item)
    return result


def backfill_planning_contexts(apps, schema_editor):
    IntakeWorkspace = apps.get_model('company_intake', 'IntakeWorkspace')
    WorkspaceSource = apps.get_model('company_intake', 'WorkspaceSource')
    PlanningContext = apps.get_model('org_context', 'PlanningContext')
    ContextProfile = apps.get_model('org_context', 'ContextProfile')
    PlanningContextSource = apps.get_model('org_context', 'PlanningContextSource')

    for workspace in IntakeWorkspace.objects.all():
        if PlanningContext.objects.filter(
            workspace=workspace,
            slug='org-baseline',
            kind='org',
        ).exists():
            continue

        metadata = dict(workspace.metadata or {})
        company_profile = dict(metadata.get('company_profile') or {})
        tech_stack = _dedupe_strings(
            list(metadata.get('tech_stack') or [])
            + list(company_profile.get('current_tech_stack') or [])
            + list(company_profile.get('planned_tech_stack') or [])
        )

        context = PlanningContext.objects.create(
            workspace=workspace,
            organization_id=workspace.organization_id,
            name=workspace.name,
            slug='org-baseline',
            kind='org',
            status='active',
            description=f'Default organization baseline for workspace {workspace.name}',
            metadata={
                'auto_created': True,
                'created_by': 'backfill_migration_0021',
            },
        )
        ContextProfile.objects.create(
            planning_context=context,
            company_profile=company_profile,
            tech_stack=tech_stack,
            constraints=list(metadata.get('constraints') or []),
            growth_goals=list(metadata.get('growth_goals') or []),
            inherit_from_parent=False,
            override_fields=[],
        )

        for source in WorkspaceSource.objects.filter(workspace=workspace).exclude(status='archived'):
            usage_type = SOURCE_KIND_TO_USAGE.get(source.source_kind, 'other')
            PlanningContextSource.objects.get_or_create(
                planning_context=context,
                workspace_source=source,
                defaults={
                    'usage_type': usage_type,
                    'is_active': True,
                    'include_in_blueprint': True,
                    'include_in_roadmap_analysis': usage_type in ('roadmap', 'strategy'),
                },
            )


def reverse_backfill(apps, schema_editor):
    PlanningContext = apps.get_model('org_context', 'PlanningContext')
    PlanningContext.objects.filter(
        slug='org-baseline',
        metadata__auto_created=True,
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('company_intake', '0008_intakeworkspace_organization'),
        ('org_context', '0020_planning_context'),
    ]

    operations = [
        migrations.RunPython(backfill_planning_contexts, reverse_backfill),
    ]
