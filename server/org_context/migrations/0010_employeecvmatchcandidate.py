import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('org_context', '0009_rename_org_context_workspa_65c7d8_idx_org_context_workspa_686562_idx_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='EmployeeCVMatchCandidate',
            fields=[
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('rank', models.PositiveSmallIntegerField(default=1)),
                ('score', models.DecimalField(decimal_places=4, default=0, max_digits=5)),
                ('name_score', models.DecimalField(decimal_places=4, default=0, max_digits=5)),
                ('title_score', models.DecimalField(decimal_places=4, default=0, max_digits=5)),
                ('department_score', models.DecimalField(decimal_places=4, default=0, max_digits=5)),
                ('exact_name_match', models.BooleanField(default=False)),
                ('email_match', models.BooleanField(default=False)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('employee', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='cv_match_candidates', to='org_context.employee')),
                ('profile', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='candidate_matches', to='org_context.employeecvprofile')),
                ('workspace', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='employee_cv_match_candidates', to='company_intake.intakeworkspace')),
            ],
            options={
                'ordering': ['rank', '-score'],
                'unique_together': {('profile', 'employee')},
            },
        ),
        migrations.AddIndex(
            model_name='employeecvmatchcandidate',
            index=models.Index(fields=['workspace', 'employee'], name='org_context_workspa_6f4f0d_idx'),
        ),
        migrations.AddIndex(
            model_name='employeecvmatchcandidate',
            index=models.Index(fields=['workspace', 'profile'], name='org_context_workspa_44d047_idx'),
        ),
    ]
