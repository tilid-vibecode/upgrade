from django.db import migrations, models


def _backfill_completion_timestamps(apps, schema_editor):
    DevelopmentPlanRun = apps.get_model('development_plans', 'DevelopmentPlanRun')
    DevelopmentPlanRun.objects.filter(
        status='completed',
        completed_at__isnull=True,
    ).update(completed_at=models.F('updated_at'))


class Migration(migrations.Migration):

    dependencies = [
        ('development_plans', '0004_rename_development_workspa_63eb2c_idx_development_workspa_74193f_idx_and_more'),
        ('evidence_matrix', '0006_rename_evidence_ma_workspa_a8ddce_idx_evidence_ma_workspa_d5d355_idx'),
        ('skill_blueprint', '0008_rename_skill_bluep_workspa_491deb_idx_skill_bluep_workspa_70ae78_idx_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='developmentplanrun',
            name='blueprint_run',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.SET_NULL,
                related_name='development_plans',
                to='skill_blueprint.skillblueprintrun',
            ),
        ),
        migrations.AddField(
            model_name='developmentplanrun',
            name='completed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='developmentplanrun',
            name='generation_batch_uuid',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='developmentplanrun',
            name='input_snapshot',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='developmentplanrun',
            name='is_current',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='developmentplanrun',
            name='matrix_run',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.SET_NULL,
                related_name='development_plans',
                to='evidence_matrix.evidencematrixrun',
            ),
        ),
        migrations.AddField(
            model_name='developmentplanrun',
            name='plan_version',
            field=models.CharField(blank=True, default='stage9-v1', max_length=32),
        ),
        migrations.AddField(
            model_name='developmentplanrun',
            name='recommendation_payload',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddIndex(
            model_name='developmentplanrun',
            index=models.Index(fields=['workspace', 'blueprint_run'], name='developmen_worksp_9d8341_idx'),
        ),
        migrations.AddIndex(
            model_name='developmentplanrun',
            index=models.Index(fields=['workspace', 'matrix_run'], name='developmen_worksp_2fd08a_idx'),
        ),
        migrations.AddIndex(
            model_name='developmentplanrun',
            index=models.Index(fields=['workspace', 'generation_batch_uuid'], name='developmen_worksp_0677ba_idx'),
        ),
        migrations.AddIndex(
            model_name='developmentplanrun',
            index=models.Index(fields=['workspace', 'is_current'], name='developmen_worksp_7150cf_idx'),
        ),
        migrations.AddConstraint(
            model_name='developmentplanrun',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(scope='team', employee__isnull=True)
                    | models.Q(scope='individual', employee__isnull=False)
                ),
                name='development_plan_scope_employee_consistent',
            ),
        ),
        migrations.AddConstraint(
            model_name='developmentplanrun',
            constraint=models.UniqueConstraint(
                condition=models.Q(is_current=True, scope='team'),
                fields=('workspace', 'scope'),
                name='development_plan_one_current_team_per_workspace',
            ),
        ),
        migrations.AddConstraint(
            model_name='developmentplanrun',
            constraint=models.UniqueConstraint(
                condition=models.Q(is_current=True, scope='individual'),
                fields=('workspace', 'employee'),
                name='development_plan_one_current_individual_per_workspace',
            ),
        ),
        migrations.AddConstraint(
            model_name='developmentplanrun',
            constraint=models.UniqueConstraint(
                condition=models.Q(scope='team', generation_batch_uuid__isnull=False),
                fields=('generation_batch_uuid', 'scope'),
                name='development_plan_one_team_per_batch',
            ),
        ),
        migrations.AddConstraint(
            model_name='developmentplanrun',
            constraint=models.UniqueConstraint(
                condition=models.Q(scope='individual', generation_batch_uuid__isnull=False),
                fields=('generation_batch_uuid', 'employee'),
                name='development_plan_one_individual_per_batch',
            ),
        ),
        migrations.RunPython(_backfill_completion_timestamps, migrations.RunPython.noop),
    ]
