# File location: /server/server/storage/helpers.py
"""
Storage helpers: key path builders and cross-role transfer utilities.
"""
import logging
import os
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from asgiref.sync import sync_to_async

from .roles import processing_client, persistent_client

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Key path builders
# ------------------------------------------------------------------

def build_persistent_key(
    org_uuid: str,
    file_uuid: str,
    variant_type: str,
    filename: str,
) -> str:
    """Build a key for persistent storage.

    Pattern: ``media/{org}/{YYYY}/{MM}/{file_uuid}/{variant}/{safe_name}``
    """
    now = datetime.now(timezone.utc)
    safe_name = os.path.basename(filename)
    return (
        f'media/{org_uuid}'
        f'/{now.year}/{now.month:02d}'
        f'/{file_uuid}/{variant_type}/{safe_name}'
    )


def build_processing_key(
    org_uuid: str,
    file_uuid: str,
    filename: str,
) -> str:
    """Build a key for a user-upload copy in processing storage.

    Pattern: ``processing/{org}/{file_uuid}/{safe_name}``
    """
    safe_name = os.path.basename(filename)
    return f'processing/{org_uuid}/{file_uuid}/{safe_name}'


def build_pipeline_key(
    org_uuid: str,
    run_id: str,
    component_name: str,
    filename: str,
) -> str:
    """Build a key for a pipeline intermediate file.

    Pattern: ``pipeline/{org}/{run_id}/{component}/{filename}``
    """
    safe_name = os.path.basename(filename)
    return f'pipeline/{org_uuid}/{run_id}/{component_name}/{safe_name}'


def build_artifact_key(
    discussion_uuid: str,
    artifact_type: str,
    content_hash: str,
) -> str:
    """Build a key for an artifact JSON blob in processing storage.

    Pattern: ``artifacts/{discussion}/{type}/{hash}.json``
    """
    return f'artifacts/{discussion_uuid}/{artifact_type}/{content_hash}.json'


def build_prototype_persistent_key(
    file_uuid: str,
    filename: str,
    scope: str = 'prototype',
    variant_type: str = 'original',
) -> str:
    """Build a persistent-storage key for unauthenticated prototype uploads."""
    now = datetime.now(timezone.utc)
    safe_name = os.path.basename(filename)
    safe_scope = scope.strip('/').replace(' ', '-').lower() or 'prototype'
    return (
        f'prototype-media/{safe_scope}'
        f'/{now.year}/{now.month:02d}'
        f'/{file_uuid}/{variant_type}/{safe_name}'
    )


def build_prototype_processing_key(
    file_uuid: str,
    filename: str,
    scope: str = 'prototype',
) -> str:
    """Build a processing-storage key for prototype uploads."""
    safe_name = os.path.basename(filename)
    safe_scope = scope.strip('/').replace(' ', '-').lower() or 'prototype'
    return f'prototype-processing/{safe_scope}/{file_uuid}/{safe_name}'


# ------------------------------------------------------------------
# Cross-role transfer utilities
# ------------------------------------------------------------------

async def ensure_in_processing(media_file) -> str:
    """Ensure that *media_file* is available in processing storage.

    - If ``processing_key`` is set and the object still exists → return it.
    - If it was TTL-expired (or is ``None``) but ``persistent_key`` exists →
      download from persistent, re-upload to processing, update the record.
    - Raises ``StorageError`` if the file cannot be found in any storage.

    Returns the (possibly refreshed) ``processing_key``.
    """
    proc = processing_client()

    # Fast path: processing key exists and object is still alive
    if media_file.processing_key:
        if await proc.object_exists(media_file.processing_key):
            return media_file.processing_key
        logger.info(
            'Processing object expired for MediaFile %s, will restore.',
            media_file.uuid,
        )

    # Restore from persistent
    if not media_file.persistent_key:
        raise StorageError(
            f'MediaFile {media_file.uuid} has no persistent_key — '
            f'cannot restore to processing.'
        )

    pers = persistent_client()
    data = await pers.download_bytes(media_file.persistent_key)

    new_key = build_processing_key(
        org_uuid=str(media_file.organization_id)
        if hasattr(media_file, 'organization_id')
        else str(media_file.organization.uuid),
        file_uuid=str(media_file.uuid),
        filename=media_file.original_filename,
    )
    await proc.upload_bytes(
        key=new_key,
        data=data,
        content_type=media_file.content_type,
    )

    media_file.processing_key = new_key
    await sync_to_async(media_file.save)(
        update_fields=['processing_key', 'updated_at'],
    )
    logger.info(
        'Restored MediaFile %s to processing storage: %s',
        media_file.uuid, new_key,
    )
    return new_key


async def promote_to_persistent(
    media_file,
    content_type: Optional[str] = None,
    filename: Optional[str] = None,
    cleanup_processing: bool = True,
) -> str:
    """Copy a file from processing → persistent storage.

    Creates a ``persistent_key`` on the record.  Optionally deletes
    the processing copy afterwards.

    Returns the new ``persistent_key``.
    """
    if media_file.persistent_key:
        logger.warning(
            'MediaFile %s already has persistent_key=%s — skipping promote.',
            media_file.uuid, media_file.persistent_key,
        )
        return media_file.persistent_key

    if not media_file.processing_key:
        raise StorageError(
            f'MediaFile {media_file.uuid} has no processing_key — '
            f'nothing to promote.'
        )

    ct = content_type or media_file.content_type
    fn = filename or media_file.original_filename

    proc = processing_client()
    pers = persistent_client()

    data = await proc.download_bytes(media_file.processing_key)

    org_uuid = (
        str(media_file.organization_id)
        if hasattr(media_file, 'organization_id')
        else str(media_file.organization.uuid)
    )
    persistent_key = build_persistent_key(
        org_uuid=org_uuid,
        file_uuid=str(media_file.uuid),
        variant_type='original',
        filename=fn,
    )
    await pers.upload_bytes(key=persistent_key, data=data, content_type=ct)

    update_fields = ['persistent_key', 'updated_at']
    media_file.persistent_key = persistent_key

    if cleanup_processing:
        await proc.delete_object(media_file.processing_key)
        media_file.processing_key = None
        update_fields.append('processing_key')

    await sync_to_async(media_file.save)(update_fields=update_fields)
    logger.info(
        'Promoted MediaFile %s to persistent: %s (cleanup=%s)',
        media_file.uuid, persistent_key, cleanup_processing,
    )
    return persistent_key


# ------------------------------------------------------------------
# Exception
# ------------------------------------------------------------------

class StorageError(Exception):
    """Raised when a storage operation cannot be completed."""
