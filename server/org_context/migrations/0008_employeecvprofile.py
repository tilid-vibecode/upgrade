import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('company_intake', '0004_workspacesource_parsing_status'),
        ('org_context', '0007_rename_org_context_workspa_1c7cc3_idx_org_context_workspa_a1b2b3_idx'),
    ]

    operations = [
        migrations.CreateModel(
            name='EmployeeCVProfile',
            fields=[
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('status', models.CharField(choices=[('matched', 'Matched'), ('ambiguous', 'Ambiguous'), ('unmatched', 'Unmatched'), ('low_confidence_match', 'Low confidence match'), ('extraction_failed', 'Extraction failed')], default='unmatched', max_length=32)),
                ('evidence_quality', models.CharField(choices=[('strong', 'Strong'), ('usable', 'Usable'), ('sparse', 'Sparse'), ('empty', 'Empty'), ('failed', 'Failed')], default='empty', max_length=16)),
                ('match_confidence', models.DecimalField(decimal_places=2, default=0, max_digits=4)),
                ('matched_by', models.CharField(blank=True, default='', max_length=64)),
                ('language_code', models.CharField(blank=True, default='', max_length=16)),
                ('input_revision', models.CharField(blank=True, default='', max_length=64)),
                ('active_vector_generation_id', models.CharField(blank=True, default='', max_length=64)),
                ('headline', models.CharField(blank=True, default='', max_length=255)),
                ('current_role', models.CharField(blank=True, default='', max_length=255)),
                ('seniority', models.CharField(blank=True, default='', max_length=64)),
                ('role_family', models.CharField(blank=True, default='', max_length=255)),
                ('extracted_payload', models.JSONField(blank=True, default=dict)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('employee', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='cv_profiles', to='org_context.employee')),
                ('source', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='cv_profile', to='company_intake.workspacesource')),
                ('workspace', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='employee_cv_profiles', to='company_intake.intakeworkspace')),
            ],
            options={
                'ordering': ['-updated_at'],
            },
        ),
        migrations.AddIndex(
            model_name='employeecvprofile',
            index=models.Index(fields=['workspace', 'status'], name='org_context_workspa_65c7d8_idx'),
        ),
        migrations.AddIndex(
            model_name='employeecvprofile',
            index=models.Index(fields=['workspace', 'employee'], name='org_context_workspa_d82df2_idx'),
        ),
        migrations.AddIndex(
            model_name='employeecvprofile',
            index=models.Index(fields=['workspace', 'evidence_quality'], name='org_context_workspa_2984f0_idx'),
        ),
        migrations.AddIndex(
            model_name='employeecvprofile',
            index=models.Index(fields=['workspace', 'input_revision'], name='org_context_workspa_3b0b42_idx'),
        ),
    ]
