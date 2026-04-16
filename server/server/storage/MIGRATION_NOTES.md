# Migration & Integration Notes

## 1. Artifact Model — Add media_file FK

Add this field to the `Artifact` model in `feature/models.py`:

```python
# In class Artifact(TimestampedModel):
# Add after existing fields:

media_file = models.ForeignKey(
    'media_storage.MediaFile',
    on_delete=models.SET_NULL,
    null=True,
    blank=True,
    related_name='source_artifacts',
    help_text='Linked MediaFile for user-facing access (signed URLs).',
)
```

## 2. Django Migrations

Run these in order after placing all files:

```bash
# Step 1: Generate migrations for model changes
python manage.py makemigrations media_storage
python manage.py makemigrations feature

# Step 2: Create a data migration to copy storage_key → persistent_key
python manage.py makemigrations media_storage --empty --name copy_storage_keys

# Then edit the generated migration file to add:
```

```python
# media_storage/migrations/XXXX_copy_storage_keys.py
from django.db import migrations


def copy_keys_forward(apps, schema_editor):
    MediaFile = apps.get_model('media_storage', 'MediaFile')
    for mf in MediaFile.objects.all().iterator():
        mf.persistent_key = mf.storage_key
        mf.save(update_fields=['persistent_key'])

    MediaFileVariant = apps.get_model('media_storage', 'MediaFileVariant')
    for v in MediaFileVariant.objects.all().iterator():
        v.persistent_key = v.storage_key
        v.save(update_fields=['persistent_key'])


def copy_keys_reverse(apps, schema_editor):
    MediaFile = apps.get_model('media_storage', 'MediaFile')
    for mf in MediaFile.objects.all().iterator():
        mf.storage_key = mf.persistent_key
        mf.save(update_fields=['storage_key'])

    MediaFileVariant = apps.get_model('media_storage', 'MediaFileVariant')
    for v in MediaFileVariant.objects.all().iterator():
        v.storage_key = v.persistent_key
        v.save(update_fields=['storage_key'])


class Migration(migrations.Migration):
    dependencies = [
        ('media_storage', 'XXXX_previous_migration'),  # ← fill in
    ]

    operations = [
        migrations.RunPython(copy_keys_forward, copy_keys_reverse),
    ]
```

```bash
# Step 3: After data migration succeeds, create a migration to drop old fields
# Generate another empty migration:
python manage.py makemigrations media_storage --empty --name drop_old_storage_fields
```

```python
# media_storage/migrations/XXXX_drop_old_storage_fields.py
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('media_storage', 'XXXX_copy_storage_keys'),  # ← fill in
    ]

    operations = [
        # MediaFile
        migrations.RemoveField(model_name='mediafile', name='storage_key'),
        migrations.RemoveField(model_name='mediafile', name='storage_bucket'),
        migrations.RemoveField(model_name='mediafile', name='storage_backend'),
        # MediaFileVariant
        migrations.RemoveField(model_name='mediafilevariant', name='storage_key'),
        migrations.RemoveField(model_name='mediafilevariant', name='storage_bucket'),
    ]
```

```bash
# Step 4: Run all migrations
python manage.py migrate
```

**Important**: If you want a safer rollout, you can keep the old fields temporarily
and drop them in a later release after verifying everything works.


## 3. Requirements Changes

```
# Remove:
minio

# Keep (already present):
aioboto3
boto3
botocore
```

## 4. Files to Delete (Phase 6)

After verifying everything works:

```bash
rm server/s3_util.py
rm server/media_storage_service.py
rm server/minio_client_manager.py
```

## 5. Environment Variables

### Local development (.env)
```bash
# Storage backends
PROCESSING_STORAGE_BACKEND=local_minio
PERSISTENT_STORAGE_BACKEND=local_minio
STATIC_STORAGE_BACKEND=local_minio

PROCESSING_STORAGE_BUCKET=upg-processing
PERSISTENT_STORAGE_BUCKET=upg-persistent
STATIC_STORAGE_BUCKET=upg-static

# MinIO credentials
MINIO_ENDPOINT_URL=http://localhost:9000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin

# Lifecycle TTL
PROCESSING_LIFECYCLE_TTL_DAYS=7
```

### Staging / Production (.env)
```bash
# Storage backends
PROCESSING_STORAGE_BACKEND=cluster_minio
PERSISTENT_STORAGE_BACKEND=aws_s3
STATIC_STORAGE_BACKEND=aws_s3

PROCESSING_STORAGE_BUCKET=upg-processing
PERSISTENT_STORAGE_BUCKET=upg-persistent
STATIC_STORAGE_BUCKET=upg-static

# MinIO cluster (processing)
MINIO_CLUSTER_ENDPOINT_URL=http://minio.upg-namespace.svc.cluster.local:9000
MINIO_CLUSTER_ACCESS_KEY=<cluster-minio-key>
MINIO_CLUSTER_SECRET_KEY=<cluster-minio-secret>

# AWS S3 (persistent + static)
AWS_S3_ACCESS_KEY_ID=<aws-key>
AWS_S3_SECRET_ACCESS_KEY=<aws-secret>
AWS_S3_REGION=us-east-1

PROCESSING_LIFECYCLE_TTL_DAYS=7
```

## 6. File Placement Summary

```
server/
├── server/
│   ├── storage/
│   │   ├── __init__.py          ← NEW
│   │   ├── client.py            ← NEW
│   │   ├── roles.py             ← NEW
│   │   ├── helpers.py           ← NEW
│   │   └── django_static.py     ← NEW
│   ├── settings.py              ← UPDATED
│   └── fastapi_main.py          ← UPDATED
├── media_storage/
│   ├── models.py                ← UPDATED
│   ├── managers.py              ← UPDATED
│   ├── services.py              ← UPDATED
│   ├── fastapi_views.py         ← UPDATED
│   ├── entities.py              ← UPDATED
│   ├── constants.py             ← UPDATED
│   ├── admin.py                 ← UPDATED
│   └── apps.py                  (unchanged)
├── brain/
│   └── services/
│       └── artifact_store.py    ← UPDATED
└── feature/
    └── models.py                ← ADD media_file FK to Artifact
```
