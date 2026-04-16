import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('company_intake', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='AssessmentCycle',
            fields=[
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('title', models.CharField(default='Initial assessment cycle', max_length=255)),
                ('status', models.CharField(choices=[('draft', 'Draft'), ('ready', 'Ready'), ('running', 'Running'), ('completed', 'Completed')], default='draft', max_length=32)),
                ('uses_self_report', models.BooleanField(default=True)),
                ('uses_performance_reviews', models.BooleanField(default=False)),
                ('uses_feedback_360', models.BooleanField(default=False)),
                ('uses_skill_tests', models.BooleanField(default=False)),
                ('configuration', models.JSONField(blank=True, default=dict)),
                ('result_summary', models.JSONField(blank=True, default=dict)),
                ('workspace', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='assessment_cycles', to='company_intake.intakeworkspace')),
            ],
            options={
                'ordering': ['-updated_at'],
            },
        ),
        migrations.AddIndex(
            model_name='assessmentcycle',
            index=models.Index(fields=['workspace', 'status'], name='employee_ass_workspa_67f8ef_idx'),
        ),
    ]
