import asyncio
import logging
import uuid as uuid_mod
from typing import Optional

from asgiref.sync import sync_to_async
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status

from company_intake.models import IntakeWorkspace
from server.rate_limit import GLOBAL_RPM
from server.storage import persistent_client, processing_client
from server.storage.helpers import (
    build_prototype_persistent_key,
    build_prototype_processing_key,
)

from .constants import SIGNED_URL_EXPIRY_SECONDS, resolve_file_category
from .entities import (
    MediaFileDetailResponse,
    MediaFileListResponse,
    PrototypeMediaUploadResponse,
    SignedUrlResponse,
)
from .models import MediaFile
from .services import (
    build_media_file_detail_response,
    build_media_file_response,
    enrich_media_file_after_upload,
    generate_signed_url_for_file,
    validate_file_size,
    validate_upload_file,
)

logger = logging.getLogger(__name__)

prototype_media_router = APIRouter(
    prefix='/prototype/workspaces/{workspace_slug}/media',
    tags=['prototype-media'],
    dependencies=[GLOBAL_RPM],
)


async def _cleanup_object(key: str, *, processing: bool) -> None:
    try:
        client = processing_client() if processing else persistent_client()
        await client.delete_object(key)
    except Exception as exc:
        logger.warning('Failed to clean up %s object %s: %s', 'processing' if processing else 'persistent', key, exc)


async def _get_workspace_or_404(workspace_slug: str) -> IntakeWorkspace:
    workspace = await sync_to_async(
        IntakeWorkspace.objects.filter(slug=workspace_slug).first
    )()
    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Workspace not found.',
        )
    return workspace


async def _get_or_404(file_uuid: str, *, workspace: IntakeWorkspace) -> MediaFile:
    """Fetch a media file that belongs to *workspace*.

    Rejects files owned by a different workspace, unclaimed files, and
    files that belong to an organisation or discussion scope.
    """
    media_file = await MediaFile.objects.get_for_prototype_workspace(
        workspace_pk=workspace.pk,
        file_uuid=file_uuid,
    )
    if media_file is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='File not found.')
    return media_file


@prototype_media_router.post(
    '/upload',
    response_model=PrototypeMediaUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_prototype_file(
    workspace_slug: str,
    file: UploadFile = File(...),
    scope: str = Form('prototype'),
):
    workspace = await _get_workspace_or_404(workspace_slug)

    content = await file.read()
    filename = file.filename or 'unknown'
    validate_upload_file(file)
    validate_file_size(content, filename)

    file_uuid = str(uuid_mod.uuid4())
    content_type = file.content_type or 'application/octet-stream'
    category = resolve_file_category(content_type, filename)

    resolved_scope = scope
    if resolved_scope == 'prototype':
        resolved_scope = f'{workspace.slug}/uploads'

    persistent_key = build_prototype_persistent_key(
        file_uuid=file_uuid,
        filename=filename,
        scope=resolved_scope,
    )
    processing_key = build_prototype_processing_key(
        file_uuid=file_uuid,
        filename=filename,
        scope=resolved_scope,
    )

    persistent_ok = False
    processing_ok = False

    async def _upload_persistent() -> None:
        nonlocal persistent_ok
        await persistent_client().upload_bytes(
            key=persistent_key,
            data=content,
            content_type=content_type,
            metadata={'scope': resolved_scope},
        )
        persistent_ok = True

    async def _upload_processing() -> None:
        nonlocal processing_ok
        await processing_client().upload_bytes(
            key=processing_key,
            data=content,
            content_type=content_type,
            metadata={'scope': resolved_scope},
        )
        processing_ok = True

    try:
        await asyncio.gather(_upload_persistent(), _upload_processing())
    except Exception as exc:
        logger.error('Prototype upload failed for %s: %s', filename, exc, exc_info=True)
        if persistent_ok:
            await _cleanup_object(persistent_key, processing=False)
        if processing_ok:
            await _cleanup_object(processing_key, processing=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to store the uploaded file.',
        ) from exc

    media_file = None
    try:
        media_file = await MediaFile.objects.create_pending(
            organization=None,
            uploaded_by=None,
            original_filename=filename,
            content_type=content_type,
            file_size=len(content),
            file_category=category,
            persistent_key=persistent_key,
            processing_key=processing_key,
            prototype_workspace=workspace,
        )
        await MediaFile.objects.mark_uploaded(media_file)
        media_file = await enrich_media_file_after_upload(
            media_file,
            file_bytes=content,
            extra_metadata={
                'scope': resolved_scope,
                'prototype_mode': True,
                'workspace_slug': workspace.slug,
            },
        )
    except Exception as exc:
        logger.error('Prototype media DB write failed for %s: %s', filename, exc, exc_info=True)
        if media_file is not None:
            try:
                await sync_to_async(media_file.delete)()
            except Exception:
                logger.exception('Failed to delete partially created MediaFile %s', media_file.uuid)
        await _cleanup_object(persistent_key, processing=False)
        await _cleanup_object(processing_key, processing=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='File uploaded to storage, but metadata could not be saved.',
        ) from exc

    signed_url: Optional[str] = None
    try:
        signed_url = await generate_signed_url_for_file(media_file)
    except Exception as exc:
        logger.warning('Failed to generate signed URL for prototype file %s: %s', media_file.uuid, exc)

    return PrototypeMediaUploadResponse(
        file=build_media_file_response(media_file),
        signed_url=signed_url,
    )


@prototype_media_router.get('/files', response_model=MediaFileListResponse)
async def list_prototype_files(
    workspace_slug: str,
    file_category: Optional[str] = Query(None),
    file_status: Optional[str] = Query(None, alias='status'),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    workspace = await _get_workspace_or_404(workspace_slug)
    files = await MediaFile.objects.list_for_prototype_workspace(
        workspace.slug,
        file_category=file_category,
        status=file_status,
        limit=limit,
        offset=offset,
    )
    total = await MediaFile.objects.count_for_prototype_workspace(
        workspace.slug,
        file_category=file_category,
        status=file_status,
    )
    return MediaFileListResponse(
        files=[build_media_file_response(item) for item in files],
        total=total,
        limit=limit,
        offset=offset,
    )


@prototype_media_router.get('/files/{file_uuid}', response_model=MediaFileDetailResponse)
async def get_prototype_file(workspace_slug: str, file_uuid: str):
    workspace = await _get_workspace_or_404(workspace_slug)
    media_file = await _get_or_404(file_uuid, workspace=workspace)
    return await build_media_file_detail_response(media_file)


@prototype_media_router.get('/files/{file_uuid}/signed-url', response_model=SignedUrlResponse)
async def get_prototype_signed_url(workspace_slug: str, file_uuid: str):
    workspace = await _get_workspace_or_404(workspace_slug)
    media_file = await _get_or_404(file_uuid, workspace=workspace)
    signed_url = await generate_signed_url_for_file(media_file)
    if not signed_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='The file does not have a persistent object.',
        )
    return SignedUrlResponse(
        url=signed_url,
        expires_in_seconds=SIGNED_URL_EXPIRY_SECONDS,
        variant_type='original',
        file_uuid=media_file.uuid,
    )
