from django.db import migrations, models


def _backfill_blueprint_run(apps, schema_editor):
    EvidenceMatrixRun = apps.get_model('evidence_matrix', 'EvidenceMatrixRun')
    SkillBlueprintRun = apps.get_model('skill_blueprint', 'SkillBlueprintRun')

    for run in EvidenceMatrixRun.objects.filter(blueprint_run__isnull=True).iterator():
        workspace_id = run.workspace_id
        selected_blueprint = None
        input_snapshot = dict(getattr(run, 'input_snapshot', {}) or {})
        matrix_payload = dict(getattr(run, 'matrix_payload', {}) or {})
        payload_snapshot = dict(matrix_payload.get('input_snapshot') or {})

        explicit_uuid = str(
            input_snapshot.get('blueprint_run_uuid')
            or payload_snapshot.get('blueprint_run_uuid')
            or ''
        ).strip()
        if explicit_uuid:
            selected_blueprint = SkillBlueprintRun.objects.filter(
                workspace_id=workspace_id,
                uuid=explicit_uuid,
            ).first()

        if selected_blueprint is None:
            workspace_blueprints = SkillBlueprintRun.objects.filter(workspace_id=workspace_id)
            if workspace_blueprints.count() == 1:
                selected_blueprint = workspace_blueprints.first()
            else:
                current_published = (
                    SkillBlueprintRun.objects.filter(workspace_id=workspace_id, is_published=True)
                    .order_by('-published_at', '-updated_at')
                    .first()
                )
                published_at = getattr(current_published, 'published_at', None)
                if current_published is not None and published_at is not None and run.updated_at >= published_at:
                    selected_blueprint = current_published

        if selected_blueprint is None:
            continue

        input_snapshot['blueprint_run_uuid'] = str(selected_blueprint.uuid)
        input_snapshot['blueprint_run_backfilled'] = True
        run.blueprint_run = selected_blueprint
        run.input_snapshot = input_snapshot
        run.save(update_fields=['blueprint_run', 'input_snapshot', 'updated_at'])


class Migration(migrations.Migration):

    dependencies = [
        ('skill_blueprint', '0008_rename_skill_bluep_workspa_491deb_idx_skill_bluep_workspa_70ae78_idx_and_more'),
        ('evidence_matrix', '0004_alter_evidencematrixrun_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='evidencematrixrun',
            name='blueprint_run',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.SET_NULL,
                related_name='evidence_matrix_runs',
                to='skill_blueprint.skillblueprintrun',
            ),
        ),
        migrations.AddField(
            model_name='evidencematrixrun',
            name='heatmap_payload',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='evidencematrixrun',
            name='incompleteness_payload',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='evidencematrixrun',
            name='input_snapshot',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='evidencematrixrun',
            name='matrix_version',
            field=models.CharField(blank=True, default='stage8-v1', max_length=32),
        ),
        migrations.AddField(
            model_name='evidencematrixrun',
            name='risk_payload',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddIndex(
            model_name='evidencematrixrun',
            index=models.Index(fields=['workspace', 'blueprint_run'], name='evidence_ma_workspa_a8ddce_idx'),
        ),
        migrations.RunPython(_backfill_blueprint_run, migrations.RunPython.noop),
    ]
