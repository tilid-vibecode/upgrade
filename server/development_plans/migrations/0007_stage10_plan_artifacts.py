import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('media_storage', '0001_initial'),
        ('evidence_matrix', '0005_stage8_matrix_run_inputs'),
        ('org_context', '0010_employeecvmatchcandidate'),
        ('skill_blueprint', '0007_stage5_clarifications_and_publish'),
        ('development_plans', '0006_rename_developmen_worksp_9d8341_idx_development_workspa_49fd4a_idx_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='developmentplanrun',
            name='export_snapshot',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.CreateModel(
            name='DevelopmentPlanArtifact',
            fields=[
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('generation_batch_uuid', models.UUIDField(blank=True, null=True)),
                ('artifact_scope', models.CharField(choices=[('team', 'Team'), ('individual', 'Individual')], default='team', max_length=32)),
                ('artifact_format', models.CharField(choices=[('json', 'JSON'), ('markdown', 'Markdown'), ('html', 'HTML')], default='json', max_length=32)),
                ('artifact_version', models.CharField(blank=True, default='stage10-v1', max_length=32)),
                ('is_current', models.BooleanField(default=False)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('blueprint_run', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='development_plan_artifacts', to='skill_blueprint.skillblueprintrun')),
                ('employee', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='development_plan_artifacts', to='org_context.employee')),
                ('matrix_run', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='development_plan_artifacts', to='evidence_matrix.evidencematrixrun')),
                ('media_file', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='development_plan_artifacts', to='media_storage.mediafile')),
                ('plan_run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='artifacts', to='development_plans.developmentplanrun')),
                ('workspace', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='development_plan_artifacts', to='company_intake.intakeworkspace')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='developmentplanartifact',
            index=models.Index(fields=['workspace', 'artifact_scope', 'artifact_format'], name='developmen_workspa_95b0e1_idx'),
        ),
        migrations.AddIndex(
            model_name='developmentplanartifact',
            index=models.Index(fields=['workspace', 'generation_batch_uuid'], name='developmen_workspa_43ab8d_idx'),
        ),
        migrations.AddIndex(
            model_name='developmentplanartifact',
            index=models.Index(fields=['workspace', 'is_current'], name='developmen_workspa_1da1dc_idx'),
        ),
        migrations.AddIndex(
            model_name='developmentplanartifact',
            index=models.Index(fields=['plan_run', 'artifact_format'], name='developmen_plan_ru_61d2f1_idx'),
        ),
        migrations.AddIndex(
            model_name='developmentplanartifact',
            index=models.Index(fields=['workspace', 'employee', 'artifact_scope'], name='developmen_workspa_4bf586_idx'),
        ),
        migrations.AddConstraint(
            model_name='developmentplanartifact',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('artifact_scope', 'team'), ('employee__isnull', True)), models.Q(('artifact_scope', 'individual'), ('employee__isnull', False)), _connector='OR'), name='development_plan_artifact_scope_employee_consistent'),
        ),
        migrations.AddConstraint(
            model_name='developmentplanartifact',
            constraint=models.UniqueConstraint(fields=('plan_run', 'artifact_format'), name='development_plan_one_artifact_per_format_per_run'),
        ),
    ]
