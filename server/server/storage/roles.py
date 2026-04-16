# File location: /server/server/storage/roles.py
"""
Storage role singletons.

Initialised once during FastAPI lifespan startup, accessed everywhere via::

    from server.storage import processing_client, persistent_client, static_client
"""
import logging
from typing import Any, Dict, Optional

import aioboto3
from django.conf import settings as django_settings

from .client import StorageClient

logger = logging.getLogger(__name__)

_processing: Optional[StorageClient] = None
_persistent: Optional[StorageClient] = None
_static: Optional[StorageClient] = None


def _build_session(backend_config: Dict[str, Any]) -> aioboto3.Session:
    """Create an ``aioboto3.Session`` from a backend config dict."""
    kwargs: Dict[str, Any] = {}
    if backend_config.get('REGION_NAME'):
        kwargs['region_name'] = backend_config['REGION_NAME']
    if backend_config.get('ACCESS_KEY_ID') and backend_config.get('SECRET_ACCESS_KEY'):
        kwargs['aws_access_key_id'] = backend_config['ACCESS_KEY_ID']
        kwargs['aws_secret_access_key'] = backend_config['SECRET_ACCESS_KEY']
    return aioboto3.Session(**kwargs)


def _build_client(role_config: Dict[str, Any]) -> StorageClient:
    """Build a :class:`StorageClient` from a role mapping (e.g. ``PROCESSING_STORAGE``)."""
    backend_name = role_config['BACKEND']
    bucket = role_config['BUCKET']
    backend_config = django_settings.STORAGE_BACKENDS[backend_name]

    session = _build_session(backend_config)
    return StorageClient(session=session, config=backend_config, bucket=bucket)


async def initialize_storage() -> None:
    """Initialise all three storage role clients.

    Called once from the FastAPI lifespan.  Replaces the old
    ``initialize_minio_manager``, ``initialize_s3_util``, and
    ``initialize_media_storage_service`` calls.
    """
    global _processing, _persistent, _static

    if _processing is not None:
        logger.info('Storage clients already initialised.')
        return

    _processing = _build_client(django_settings.PROCESSING_STORAGE)
    _persistent = _build_client(django_settings.PERSISTENT_STORAGE)
    _static = _build_client(django_settings.STATIC_STORAGE)

    try:
        # Ensure buckets exist and credentials are valid.
        # Errors propagate — fastapi_main treats storage init as fatal.
        for name, client in [
            ('processing', _processing),
            ('persistent', _persistent),
            ('static', _static),
        ]:
            await client.ensure_bucket()
            logger.info('Storage bucket OK: %s (%s)', client.bucket, name)

        # Set 7-day lifecycle TTL on the processing bucket
        ttl_days = django_settings.PROCESSING_STORAGE.get('LIFECYCLE_TTL_DAYS', 7)
        try:
            await _processing.set_lifecycle_expiration(prefix='', days=ttl_days)
        except Exception as err:
            logger.warning(
                'Failed to set processing lifecycle TTL (%d days): %s',
                ttl_days, err,
            )

        # Set public-read policy on the static bucket
        try:
            await _static.set_public_read_policy()
        except Exception as err:
            logger.warning(
                'Failed to set public-read policy on static bucket: %s', err,
            )
    except Exception:
        # Clear half-initialised singletons so retry is possible
        _processing = _persistent = _static = None
        raise

    logger.info('Storage layer initialised (processing / persistent / static).')


async def close_storage() -> None:
    """Clear singleton references.  Called from FastAPI shutdown."""
    global _processing, _persistent, _static
    _processing = _persistent = _static = None
    logger.info('Storage layer closed.')


def processing_client() -> StorageClient:
    """Return the processing storage client (MinIO scratch space)."""
    if _processing is None:
        raise RuntimeError(
            'Storage not initialised.  Call initialize_storage() first.'
        )
    return _processing


def persistent_client() -> StorageClient:
    """Return the persistent storage client (S3 / durable MinIO)."""
    if _persistent is None:
        raise RuntimeError(
            'Storage not initialised.  Call initialize_storage() first.'
        )
    return _persistent


def static_client() -> StorageClient:
    """Return the static-files storage client."""
    if _static is None:
        raise RuntimeError(
            'Storage not initialised.  Call initialize_storage() first.'
        )
    return _static
