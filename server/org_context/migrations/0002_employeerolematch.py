import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('org_context', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='EmployeeRoleMatch',
            fields=[
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('source_kind', models.CharField(default='blueprint', max_length=64)),
                ('fit_score', models.DecimalField(decimal_places=2, default=0, max_digits=5)),
                ('rationale', models.TextField(blank=True, default='')),
                ('related_initiatives', models.JSONField(blank=True, default=list)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('employee', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='role_matches', to='org_context.employee')),
                ('role_profile', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='employee_matches', to='org_context.roleprofile')),
                ('workspace', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='employee_role_matches', to='company_intake.intakeworkspace')),
            ],
            options={
                'unique_together': {('employee', 'role_profile', 'source_kind')},
            },
        ),
        migrations.AddIndex(
            model_name='employeerolematch',
            index=models.Index(fields=['workspace', 'employee'], name='org_context_worksp_5a7f6e_idx'),
        ),
        migrations.AddIndex(
            model_name='employeerolematch',
            index=models.Index(fields=['workspace', 'role_profile'], name='org_context_worksp_30f521_idx'),
        ),
        migrations.AddIndex(
            model_name='employeerolematch',
            index=models.Index(fields=['workspace', 'source_kind'], name='org_context_worksp_171efe_idx'),
        ),
    ]
