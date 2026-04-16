import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='IntakeWorkspace',
            fields=[
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=255)),
                ('slug', models.SlugField(max_length=255, unique=True)),
                ('notes', models.TextField(blank=True, default='')),
                ('status', models.CharField(choices=[('draft', 'Draft'), ('collecting', 'Collecting materials'), ('processing', 'Processing'), ('completed', 'Completed')], default='draft', max_length=32)),
                ('metadata', models.JSONField(blank=True, default=dict)),
            ],
            options={
                'ordering': ['-updated_at'],
            },
        ),
        migrations.CreateModel(
            name='SourceDocument',
            fields=[
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('original_filename', models.CharField(max_length=512)),
                ('content_type', models.CharField(max_length=255)),
                ('file_size', models.PositiveBigIntegerField()),
                ('document_kind', models.CharField(choices=[('csv', 'CSV'), ('pdf', 'PDF')], max_length=16)),
                ('status', models.CharField(choices=[('uploaded', 'Uploaded'), ('failed', 'Failed')], default='uploaded', max_length=16)),
                ('persistent_key', models.CharField(max_length=1024, unique=True)),
                ('processing_key', models.CharField(max_length=1024, unique=True)),
                ('storage_metadata', models.JSONField(blank=True, default=dict)),
                ('workspace', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='documents', to='company_intake.intakeworkspace')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='intakeworkspace',
            index=models.Index(fields=['slug'], name='company_inta_slug_b6da4e_idx'),
        ),
        migrations.AddIndex(
            model_name='intakeworkspace',
            index=models.Index(fields=['status', '-updated_at'], name='company_inta_status_2ba87d_idx'),
        ),
        migrations.AddIndex(
            model_name='sourcedocument',
            index=models.Index(fields=['workspace', 'document_kind'], name='company_inta_workspa_0f4f02_idx'),
        ),
        migrations.AddIndex(
            model_name='sourcedocument',
            index=models.Index(fields=['workspace', '-created_at'], name='company_inta_workspa_6f2664_idx'),
        ),
        migrations.AddIndex(
            model_name='sourcedocument',
            index=models.Index(fields=['status', '-created_at'], name='company_inta_status_a4fe9f_idx'),
        ),
    ]
