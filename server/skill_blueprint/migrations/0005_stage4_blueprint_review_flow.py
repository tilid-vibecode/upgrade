import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('skill_blueprint', '0004_rename_skill_bluep_snapsho_11ff02_idx_skill_bluep_snapsho_f92d1a_idx_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='skillblueprintrun',
            name='status',
            field=models.CharField(
                choices=[
                    ('draft', 'Draft'),
                    ('running', 'Running'),
                    ('needs_clarification', 'Needs clarification'),
                    ('reviewed', 'Reviewed'),
                    ('approved', 'Approved'),
                    ('completed', 'Completed'),
                    ('failed', 'Failed'),
                ],
                default='draft',
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name='skillblueprintrun',
            name='approved_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='skillblueprintrun',
            name='approved_by',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='skillblueprintrun',
            name='approval_notes',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='skillblueprintrun',
            name='change_log',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='skillblueprintrun',
            name='derived_from_run',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='derived_runs',
                to='skill_blueprint.skillblueprintrun',
            ),
        ),
        migrations.AddField(
            model_name='skillblueprintrun',
            name='generation_mode',
            field=models.CharField(default='generation', max_length=32),
        ),
        migrations.AddField(
            model_name='skillblueprintrun',
            name='input_snapshot',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='skillblueprintrun',
            name='review_notes',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='skillblueprintrun',
            name='review_summary',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='skillblueprintrun',
            name='reviewed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='skillblueprintrun',
            name='reviewed_by',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='skillblueprintrun',
            name='role_library_snapshot',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='blueprint_runs',
                to='skill_blueprint.rolelibrarysnapshot',
            ),
        ),
        migrations.AddIndex(
            model_name='skillblueprintrun',
            index=models.Index(fields=['workspace', 'created_at'], name='skill_bluep_workspa_1b1d0e_idx'),
        ),
    ]
