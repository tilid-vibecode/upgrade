import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('company_intake', '0004_workspacesource_parsing_status'),
        ('skill_blueprint', '0006_rename_skill_bluep_workspa_1b1d0e_idx_skill_bluep_workspa_9599b3_idx'),
    ]

    operations = [
        migrations.AddField(
            model_name='skillblueprintrun',
            name='is_published',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='skillblueprintrun',
            name='published_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='skillblueprintrun',
            name='published_by',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='skillblueprintrun',
            name='published_notes',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddIndex(
            model_name='skillblueprintrun',
            index=models.Index(fields=['workspace', 'is_published'], name='skill_bluep_workspa_95d730_idx'),
        ),
        migrations.CreateModel(
            name='ClarificationCycle',
            fields=[
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('title', models.CharField(default='Clarification cycle', max_length=255)),
                ('status', models.CharField(choices=[('open', 'Open'), ('completed', 'Completed')], default='open', max_length=32)),
                ('summary', models.JSONField(blank=True, default=dict)),
                ('blueprint_run', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='clarification_cycle', to='skill_blueprint.skillblueprintrun')),
                ('workspace', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='clarification_cycles', to='company_intake.intakeworkspace')),
            ],
            options={
                'ordering': ['-updated_at'],
            },
        ),
        migrations.CreateModel(
            name='ClarificationQuestion',
            fields=[
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('question_key', models.CharField(max_length=255)),
                ('question_text', models.TextField()),
                ('scope', models.CharField(default='blueprint', max_length=64)),
                ('priority', models.CharField(default='medium', max_length=32)),
                ('intended_respondent_type', models.CharField(default='operator', max_length=64)),
                ('rationale', models.TextField(blank=True, default='')),
                ('evidence_refs', models.JSONField(blank=True, default=list)),
                ('impacted_roles', models.JSONField(blank=True, default=list)),
                ('impacted_initiatives', models.JSONField(blank=True, default=list)),
                ('status', models.CharField(choices=[('open', 'Open'), ('answered', 'Answered'), ('accepted', 'Accepted'), ('rejected', 'Rejected'), ('obsolete', 'Obsolete')], default='open', max_length=32)),
                ('answer_text', models.TextField(blank=True, default='')),
                ('answered_by', models.CharField(blank=True, default='', max_length=255)),
                ('answered_at', models.DateTimeField(blank=True, null=True)),
                ('status_note', models.TextField(blank=True, default='')),
                ('changed_target_model', models.BooleanField(default=False)),
                ('effect_metadata', models.JSONField(blank=True, default=dict)),
                ('blueprint_run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='clarification_questions_db', to='skill_blueprint.skillblueprintrun')),
                ('cycle', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='questions', to='skill_blueprint.clarificationcycle')),
                ('workspace', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='clarification_questions', to='company_intake.intakeworkspace')),
            ],
            options={
                'ordering': ['created_at', 'question_key'],
                'unique_together': {('cycle', 'question_key')},
            },
        ),
        migrations.AddIndex(
            model_name='clarificationcycle',
            index=models.Index(fields=['workspace', 'status'], name='skill_bluep_workspa_491deb_idx'),
        ),
        migrations.AddIndex(
            model_name='clarificationcycle',
            index=models.Index(fields=['workspace', 'created_at'], name='skill_bluep_workspa_bf7ae0_idx'),
        ),
        migrations.AddIndex(
            model_name='clarificationquestion',
            index=models.Index(fields=['workspace', 'status'], name='skill_bluep_workspa_ef0cca_idx'),
        ),
        migrations.AddIndex(
            model_name='clarificationquestion',
            index=models.Index(fields=['blueprint_run', 'status'], name='skill_bluep_bluepri_7eac9e_idx'),
        ),
        migrations.AddIndex(
            model_name='clarificationquestion',
            index=models.Index(fields=['cycle', 'status'], name='skill_bluep_cycle_i_4c8843_idx'),
        ),
    ]
