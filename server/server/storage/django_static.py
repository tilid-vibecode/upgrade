# File location: /server/server/storage/django_static.py
"""
Django ``STATICFILES_STORAGE`` backend that writes to an S3-compatible bucket.

Django's storage API is synchronous, so this uses ``boto3`` (sync) directly
rather than wrapping the async ``StorageClient``.  The bucket/config are
read from ``settings.STATIC_STORAGE`` and ``settings.STORAGE_BACKENDS``.

Usage in ``settings.py``::

    STORAGES = {
        'staticfiles': {
            'BACKEND': 'server.storage.django_static.S3StaticStorage',
        },
    }
"""
import logging
import os
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from django.conf import settings as django_settings
from django.core.files.base import ContentFile, File
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible

logger = logging.getLogger(__name__)


@deconstructible
class S3StaticStorage(Storage):
    """Sync S3 storage backend for Django static files."""

    def __init__(self) -> None:
        role_config = django_settings.STATIC_STORAGE
        backend_name = role_config['BACKEND']
        backend_config = django_settings.STORAGE_BACKENDS[backend_name]

        self._bucket = role_config['BUCKET']
        self._config = backend_config
        self._prefix = 'static'

        kw: Dict[str, Any] = {
            'config': BotoConfig(
                connect_timeout=backend_config.get('CONNECT_TIMEOUT', 5),
                read_timeout=backend_config.get('READ_TIMEOUT', 15),
                retries={
                    'max_attempts': backend_config.get('MAX_RETRIES', 3),
                    'mode': 'standard',
                },
            ),
        }
        if backend_config.get('REGION_NAME'):
            kw['region_name'] = backend_config['REGION_NAME']
        if backend_config.get('ENDPOINT_URL'):
            kw['endpoint_url'] = backend_config['ENDPOINT_URL']
        if backend_config.get('ACCESS_KEY_ID') and backend_config.get('SECRET_ACCESS_KEY'):
            kw['aws_access_key_id'] = backend_config['ACCESS_KEY_ID']
            kw['aws_secret_access_key'] = backend_config['SECRET_ACCESS_KEY']

        self._client = boto3.client('s3', **kw)

    def _full_key(self, name: str) -> str:
        """Prepend the static prefix to the file name."""
        name = name.lstrip('/')
        return f'{self._prefix}/{name}'

    # ------------------------------------------------------------------
    # Required Storage API
    # ------------------------------------------------------------------

    def _save(self, name: str, content: File) -> str:
        key = self._full_key(name)
        body = content.read()

        # Guess content type
        import mimetypes
        ct, _ = mimetypes.guess_type(name)
        extra: Dict[str, Any] = {}
        if ct:
            extra['ContentType'] = ct

        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body,
            **extra,
        )
        logger.debug('collectstatic: saved %s → %s/%s', name, self._bucket, key)
        return name

    def _open(self, name: str, mode: str = 'rb') -> File:
        key = self._full_key(name)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            data = response['Body'].read()
            return ContentFile(data, name=name)
        except ClientError as err:
            raise FileNotFoundError(
                f'Static file not found: {self._bucket}/{key}'
            ) from err

    def exists(self, name: str) -> bool:
        key = self._full_key(name)
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError:
            return False

    def delete(self, name: str) -> None:
        key = self._full_key(name)
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except ClientError as err:
            logger.warning('Failed to delete static %s: %s', key, err)

    def url(self, name: str) -> str:
        """Return a public (unsigned) URL for the static file."""
        key = self._full_key(name)
        endpoint = self._config.get('ENDPOINT_URL')
        region = self._config.get('REGION_NAME')

        if endpoint:
            return f'{endpoint.rstrip("/")}/{self._bucket}/{key}'
        if region:
            return f'https://{self._bucket}.s3.{region}.amazonaws.com/{key}'
        return f'https://{self._bucket}.s3.amazonaws.com/{key}'

    def size(self, name: str) -> int:
        key = self._full_key(name)
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=key)
            return response['ContentLength']
        except ClientError:
            return 0

    def listdir(self, path: str) -> Tuple[List[str], List[str]]:
        prefix = self._full_key(path).rstrip('/') + '/'
        dirs: List[str] = []
        files: List[str] = []

        paginator = self._client.get_paginator('list_objects_v2')
        for page in paginator.paginate(
            Bucket=self._bucket, Prefix=prefix, Delimiter='/',
        ):
            for cp in page.get('CommonPrefixes', []):
                dir_name = cp['Prefix'][len(prefix):].rstrip('/')
                if dir_name:
                    dirs.append(dir_name)
            for obj in page.get('Contents', []):
                file_name = obj['Key'][len(prefix):]
                if file_name:
                    files.append(file_name)

        return dirs, files
