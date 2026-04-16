import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('company_intake', '0002_workspacesource_and_more'),
        ('skill_blueprint', '0002_rename_skill_bluep_workspa_558d33_idx_skill_bluep_workspa_8429d3_idx'),
    ]

    operations = [
        migrations.CreateModel(
            name='RoleLibrarySnapshot',
            fields=[
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('provider', models.CharField(default='gitlab_handbook', max_length=64)),
                ('status', models.CharField(choices=[('draft', 'Draft'), ('running', 'Running'), ('completed', 'Completed'), ('failed', 'Failed')], default='draft', max_length=32)),
                ('base_urls', models.JSONField(blank=True, default=list)),
                ('discovery_payload', models.JSONField(blank=True, default=dict)),
                ('summary', models.JSONField(blank=True, default=dict)),
                ('error_message', models.TextField(blank=True, default='')),
                ('workspace', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='role_library_snapshots', to='company_intake.intakeworkspace')),
            ],
            options={
                'ordering': ['-updated_at'],
            },
        ),
        migrations.CreateModel(
            name='RoleLibraryEntry',
            fields=[
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('role_name', models.CharField(max_length=255)),
                ('department', models.CharField(blank=True, default='', max_length=255)),
                ('role_family', models.CharField(blank=True, default='', max_length=255)),
                ('page_url', models.URLField(max_length=1024)),
                ('summary', models.TextField(blank=True, default='')),
                ('levels', models.JSONField(blank=True, default=list)),
                ('responsibilities', models.JSONField(blank=True, default=list)),
                ('requirements', models.JSONField(blank=True, default=list)),
                ('skills', models.JSONField(blank=True, default=list)),
                ('raw_text', models.TextField(blank=True, default='')),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('snapshot', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='entries', to='skill_blueprint.rolelibrarysnapshot')),
            ],
            options={
                'ordering': ['role_name'],
                'unique_together': {('snapshot', 'page_url')},
            },
        ),
        migrations.AddField(
            model_name='skillblueprintrun',
            name='company_context',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='skillblueprintrun',
            name='roadmap_context',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='skillblueprintrun',
            name='role_candidates',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='skillblueprintrun',
            name='clarification_questions',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='skillblueprintrun',
            name='employee_role_matches',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='skillblueprintrun',
            name='source_summary',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddIndex(
            model_name='rolelibrarysnapshot',
            index=models.Index(fields=['workspace', 'status'], name='skill_bluep_workspa_0efc61_idx'),
        ),
        migrations.AddIndex(
            model_name='rolelibrarysnapshot',
            index=models.Index(fields=['provider', 'status'], name='skill_bluep_provide_4f45e1_idx'),
        ),
        migrations.AddIndex(
            model_name='rolelibraryentry',
            index=models.Index(fields=['snapshot', 'department'], name='skill_bluep_snapsho_11ff02_idx'),
        ),
        migrations.AddIndex(
            model_name='rolelibraryentry',
            index=models.Index(fields=['snapshot', 'role_family'], name='skill_bluep_snapsho_7a8335_idx'),
        ),
    ]
