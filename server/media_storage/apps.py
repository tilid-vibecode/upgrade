# File location: /server/media_storage/apps.py
from django.apps import AppConfig


class MediaStorageConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'media_storage'
    verbose_name = 'Media Storage'
