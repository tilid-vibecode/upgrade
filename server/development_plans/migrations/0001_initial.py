import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('company_intake', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='DevelopmentPlanRun',
            fields=[
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('title', models.CharField(default='Final development plan', max_length=255)),
                ('scope', models.CharField(choices=[('team', 'Team'), ('individual', 'Individual')], default='team', max_length=32)),
                ('status', models.CharField(choices=[('draft', 'Draft'), ('ready', 'Ready'), ('running', 'Running'), ('completed', 'Completed')], default='draft', max_length=32)),
                ('final_report_key', models.CharField(blank=True, default='', max_length=1024)),
                ('summary', models.JSONField(blank=True, default=dict)),
                ('plan_payload', models.JSONField(blank=True, default=dict)),
                ('workspace', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='development_plans', to='company_intake.intakeworkspace')),
            ],
            options={
                'ordering': ['-updated_at'],
            },
        ),
        migrations.AddIndex(
            model_name='developmentplanrun',
            index=models.Index(fields=['workspace', 'status'], name='developmen_workspa_7b6323_idx'),
        ),
        migrations.AddIndex(
            model_name='developmentplanrun',
            index=models.Index(fields=['scope', 'status'], name='developmen_scope_0992dd_idx'),
        ),
    ]
