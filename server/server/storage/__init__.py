# File location: /server/server/storage/__init__.py
"""
Unified storage layer for Upgrade.

Two logical roles:
    - **processing**: fast, near-worker MinIO for pipeline scratch files (7-day TTL)
    - **persistent**: durable S3/MinIO for user-facing files and signed URLs

Usage::

    from server.storage import processing_client, persistent_client
    await persistent_client().upload_bytes(key, data, content_type)
    url = await persistent_client().generate_signed_url(key)
"""

from .roles import (
    initialize_storage,
    close_storage,
    processing_client,
    persistent_client,
    static_client,
)

__all__ = [
    'initialize_storage',
    'close_storage',
    'processing_client',
    'persistent_client',
    'static_client',
]
