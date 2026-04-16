import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('company_intake', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='EvidenceMatrixRun',
            fields=[
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('title', models.CharField(default='Second-layer evidence matrix', max_length=255)),
                ('status', models.CharField(choices=[('draft', 'Draft'), ('ready', 'Ready'), ('running', 'Running'), ('completed', 'Completed')], default='draft', max_length=32)),
                ('source_type', models.CharField(choices=[('google_workspace', 'Google Workspace'), ('spreadsheet_upload', 'Spreadsheet upload'), ('manual', 'Manual'), ('api', 'API')], default='manual', max_length=32)),
                ('connection_label', models.CharField(blank=True, default='', max_length=255)),
                ('snapshot_key', models.CharField(blank=True, default='', max_length=1024)),
                ('matrix_payload', models.JSONField(blank=True, default=dict)),
                ('workspace', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='evidence_matrices', to='company_intake.intakeworkspace')),
            ],
            options={
                'ordering': ['-updated_at'],
            },
        ),
        migrations.AddIndex(
            model_name='evidencematrixrun',
            index=models.Index(fields=['workspace', 'status'], name='evidence_ma_workspa_7ce624_idx'),
        ),
        migrations.AddIndex(
            model_name='evidencematrixrun',
            index=models.Index(fields=['source_type', 'status'], name='evidence_ma_source__fc45d2_idx'),
        ),
    ]
