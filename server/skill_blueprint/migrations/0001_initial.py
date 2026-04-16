import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('company_intake', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='SkillBlueprintRun',
            fields=[
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('title', models.CharField(default='First-layer blueprint', max_length=255)),
                ('status', models.CharField(choices=[('draft', 'Draft'), ('ready', 'Ready'), ('running', 'Running'), ('completed', 'Completed')], default='draft', max_length=32)),
                ('required_skill_set', models.JSONField(blank=True, default=list)),
                ('automation_candidates', models.JSONField(blank=True, default=list)),
                ('occupation_map', models.JSONField(blank=True, default=list)),
                ('gap_summary', models.JSONField(blank=True, default=dict)),
                ('redundancy_summary', models.JSONField(blank=True, default=dict)),
                ('assessment_plan', models.JSONField(blank=True, default=dict)),
                ('workspace', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='skill_blueprints', to='company_intake.intakeworkspace')),
            ],
            options={
                'ordering': ['-updated_at'],
            },
        ),
        migrations.AddIndex(
            model_name='skillblueprintrun',
            index=models.Index(fields=['workspace', 'status'], name='skill_bluep_workspa_558d33_idx'),
        ),
    ]
