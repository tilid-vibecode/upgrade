import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('org_context', '0002_employeerolematch'),
        ('employee_assessment', '0002_rename_employee_ass_workspa_67f8ef_idx_employee_as_workspa_8583c4_idx'),
    ]

    operations = [
        migrations.CreateModel(
            name='EmployeeAssessmentPack',
            fields=[
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('title', models.CharField(blank=True, default='', max_length=255)),
                ('status', models.CharField(choices=[('ready', 'Ready'), ('sent', 'Sent'), ('answered', 'Answered'), ('completed', 'Completed')], default='ready', max_length=32)),
                ('questionnaire_payload', models.JSONField(blank=True, default=dict)),
                ('response_payload', models.JSONField(blank=True, default=dict)),
                ('fused_summary', models.JSONField(blank=True, default=dict)),
                ('cycle', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='packs', to='employee_assessment.assessmentcycle')),
                ('employee', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='assessment_packs', to='org_context.employee')),
            ],
            options={
                'ordering': ['employee__full_name'],
                'unique_together': {('cycle', 'employee')},
            },
        ),
        migrations.AddIndex(
            model_name='employeeassessmentpack',
            index=models.Index(fields=['cycle', 'status'], name='employee_as_cycle_i_f75ca8_idx'),
        ),
        migrations.AddIndex(
            model_name='employeeassessmentpack',
            index=models.Index(fields=['employee', 'status'], name='employee_as_employe_eb1661_idx'),
        ),
    ]
