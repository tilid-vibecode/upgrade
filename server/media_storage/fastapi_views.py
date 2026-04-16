import asyncio
import logging
import uuid as uuid_mod
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    UploadFile,
    HTTPException,
    Query,
    status,
)
from asgiref.sync import sync_to_async

from server.rate_limit import GLOBAL_RPM
from server.storage import persistent_client, processing_client
from server.storage.helpers import build_persistent_key, build_processing_key
from authentication.permissions import is_authenticated, has_org_access, require_member
from authentication.models import User
from organization.models import OrganizationMembership

from .models import MediaFile, MediaFileVariant
from .constants import SIGNED_URL_EXPIRY_SECONDS, resolve_file_category
from .services import (
    build_media_file_response,
    build_media_file_detail_response,
    enrich_media_file_after_upload,
    generate_signed_url_for_file,
    generate_signed_url_for_variant,
    validate_file_size,
    validate_upload_file,
)
from .entities import (
    MediaFileResponse,
    MediaFileDetailResponse,
    MediaFileListResponse,
    MediaFileUpdateRequest,
    MediaFileBatchRequest,
    MediaFileBatchResponse,
    MediaFileBatchItemResponse,
    SignedUrlResponse,
)

logger = logging.getLogger(__name__)

media_router = APIRouter(dependencies=[GLOBAL_RPM])


async def _get_media_file_or_404(
    org_uuid: str,
    file_uuid: str,
) -> MediaFile:
    media_file = await MediaFile.objects.get_for_org(org_uuid, file_uuid)
    if media_file is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='File not found',
        )
    return media_file

@media_router.post(
    '/{org_uuid}/upload',
    response_model=MediaFileResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_file(
    org_uuid: str,
    file: UploadFile = File(...),
    current_user: User = Depends(is_authenticated),
):
    org = await has_org_access(org_uuid=org_uuid, current_user=current_user)

    validate_upload_file(file)

    content = await file.read()
    validate_file_size(content, file.filename or 'unknown')

    category = resolve_file_category(file.content_type or '', file.filename or '')
    file_uuid = str(uuid_mod.uuid4())
    ct = file.content_type or 'application/octet-stream'
    filename = file.filename or 'unknown'

    persistent_key = build_persistent_key(
        org_uuid=str(org.uuid),
        file_uuid=file_uuid,
        variant_type='original',
        filename=filename,
    )
    proc_key = build_processing_key(
        org_uuid=str(org.uuid),
        file_uuid=file_uuid,
        filename=filename,
    )

    persistent_ok = False
    processing_ok = False

    async def _upload_persistent():
        nonlocal persistent_ok
        await persistent_client().upload_bytes(
            key=persistent_key, data=content, content_type=ct,
        )
        persistent_ok = True

    async def _upload_processing():
        nonlocal processing_ok
        try:
            await processing_client().upload_bytes(
                key=proc_key, data=content, content_type=ct,
            )
            processing_ok = True
        except Exception as e:
            logger.warning(
                'Processing upload failed for %s (non-fatal): %s',
                filename, e,
            )

    try:
        await asyncio.gather(_upload_persistent(), _upload_processing())
    except Exception as e:
        logger.error('Persistent upload failed for %s: %s', filename, e, exc_info=True)
        if processing_ok:
            try:
                await processing_client().delete_object(proc_key)
            except Exception as cleanup_err:
                logger.warning('Failed to clean up processing object %s: %s', proc_key, cleanup_err)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='File storage upload failed. Please try again.',
        )

    if not persistent_ok:
        if processing_ok:
            try:
                await processing_client().delete_object(proc_key)
            except Exception as cleanup_err:
                logger.warning('Failed to clean up processing object %s: %s', proc_key, cleanup_err)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='File storage upload failed. Please try again.',
        )

    media_file = None
    try:
        media_file = await MediaFile.objects.create_pending(
            organization=org,
            uploaded_by=current_user,
            original_filename=filename,
            content_type=ct,
            file_size=len(content),
            file_category=category,
            persistent_key=persistent_key,
            processing_key=proc_key if processing_ok else None,
        )
        await MediaFile.objects.mark_uploaded(media_file)
        media_file = await enrich_media_file_after_upload(
            media_file,
            file_bytes=content,
            extra_metadata={'organization_uuid': str(org.uuid)},
        )
    except Exception as e:
        logger.error(
            'DB operations failed after storage upload for %s — cleaning up: %s',
            filename, e, exc_info=True,
        )
        if media_file is not None:
            try:
                await sync_to_async(media_file.delete)()
            except Exception as db_err:
                logger.error('Failed to roll back MediaFile row: %s', db_err)
        try:
            await persistent_client().delete_object(persistent_key)
        except Exception as cleanup_err:
            logger.warning('Failed to clean up persistent object %s: %s', persistent_key, cleanup_err)
        if processing_ok:
            try:
                await processing_client().delete_object(proc_key)
            except Exception as cleanup_err:
                logger.warning('Failed to clean up processing object %s: %s', proc_key, cleanup_err)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='File upload failed. Please try again.',
        )

    return build_media_file_response(media_file)

@media_router.get(
    '/{org_uuid}/files',
    response_model=MediaFileListResponse,
)
async def list_files(
    org_uuid: str,
    file_category: Optional[str] = Query(None),
    file_status: Optional[str] = Query(None, alias='status'),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(is_authenticated),
):
    await has_org_access(org_uuid=org_uuid, current_user=current_user)

    files = await MediaFile.objects.list_for_org(
        organization_uuid=org_uuid,
        file_category=file_category,
        status=file_status,
        limit=limit,
        offset=offset,
    )
    total = await MediaFile.objects.count_for_org(
        organization_uuid=org_uuid,
        file_category=file_category,
        status=file_status,
    )

    return MediaFileListResponse(
        files=[build_media_file_response(f) for f in files],
        total=total,
        limit=limit,
        offset=offset,
    )


@media_router.get(
    '/{org_uuid}/files/{file_uuid}',
    response_model=MediaFileDetailResponse,
)
async def get_file(
    org_uuid: str,
    file_uuid: str,
    current_user: User = Depends(is_authenticated),
):
    await has_org_access(org_uuid=org_uuid, current_user=current_user)
    media_file = await _get_media_file_or_404(org_uuid, file_uuid)
    return await build_media_file_detail_response(media_file)

@media_router.post(
    '/{org_uuid}/files/batch',
    response_model=MediaFileBatchResponse,
)
async def batch_resolve_files(
    org_uuid: str,
    body: MediaFileBatchRequest,
    current_user: User = Depends(is_authenticated),
):
    await has_org_access(org_uuid=org_uuid, current_user=current_user)

    resolved = []
    not_found = []

    for file_uuid in body.file_uuids:
        media_file = await MediaFile.objects.get_for_org(
            org_uuid, str(file_uuid),
        )
        if media_file is None:
            not_found.append(file_uuid)
            continue

        response_data = build_media_file_response(media_file)

        signed_url = None
        if (
            media_file.file_category == 'image'
            and media_file.persistent_key
            and media_file.status in ('uploaded', 'processing', 'ready')
        ):
            try:
                signed_url = await generate_signed_url_for_file(media_file)
            except Exception as e:
                logger.warning(
                    'Failed to generate signed URL for %s: %s',
                    media_file.uuid, e,
                )

        resolved.append(
            MediaFileBatchItemResponse(
                **response_data.model_dump(),
                signed_url=signed_url,
            )
        )

    return MediaFileBatchResponse(files=resolved, not_found=not_found)

@media_router.get(
    '/{org_uuid}/files/{file_uuid}/url',
    response_model=SignedUrlResponse,
)
async def get_file_url(
    org_uuid: str,
    file_uuid: str,
    current_user: User = Depends(is_authenticated),
):
    await has_org_access(org_uuid=org_uuid, current_user=current_user)
    media_file = await _get_media_file_or_404(org_uuid, file_uuid)

    if not media_file.persistent_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='File is not available in persistent storage.',
        )

    url = await generate_signed_url_for_file(media_file)

    return SignedUrlResponse(
        url=url,
        expires_in_seconds=SIGNED_URL_EXPIRY_SECONDS,
        variant_type='original',
        file_uuid=media_file.uuid,
    )


@media_router.get(
    '/{org_uuid}/files/{file_uuid}/url/{variant_type}',
    response_model=SignedUrlResponse,
)
async def get_file_variant_url(
    org_uuid: str,
    file_uuid: str,
    variant_type: str,
    current_user: User = Depends(is_authenticated),
):
    await has_org_access(org_uuid=org_uuid, current_user=current_user)
    media_file = await _get_media_file_or_404(org_uuid, file_uuid)

    try:
        variant = await sync_to_async(MediaFileVariant.objects.get)(
            source_file=media_file,
            variant_type=variant_type,
        )
    except MediaFileVariant.DoesNotExist:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Variant \'{variant_type}\' not found for this file',
        )

    if not variant.persistent_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Variant \'{variant_type}\' is not available in persistent storage.',
        )

    url = await generate_signed_url_for_variant(variant)

    return SignedUrlResponse(
        url=url,
        expires_in_seconds=SIGNED_URL_EXPIRY_SECONDS,
        variant_type=variant_type,
        file_uuid=media_file.uuid,
    )

@media_router.patch(
    '/{org_uuid}/files/{file_uuid}',
    response_model=MediaFileResponse,
)
async def update_file(
    org_uuid: str,
    file_uuid: str,
    body: MediaFileUpdateRequest,
    membership: OrganizationMembership = Depends(require_member),
):
    media_file = await _get_media_file_or_404(org_uuid, file_uuid)

    if body.processing_description is not None:
        await MediaFile.objects.update_description(
            media_file, body.processing_description,
        )

    return build_media_file_response(media_file)


@media_router.delete(
    '/{org_uuid}/files/{file_uuid}',
    status_code=status.HTTP_200_OK,
)
async def delete_file(
    org_uuid: str,
    file_uuid: str,
    membership: OrganizationMembership = Depends(require_member),
):
    media_file = await _get_media_file_or_404(org_uuid, file_uuid)

    keys_by_role = await MediaFile.objects.delete_with_variants(media_file, db_delete=False)

    storage_failures = []
    deleted_count = 0

    for key in keys_by_role.get('persistent', []):
        try:
            ok = await persistent_client().delete_object(key)
            if ok:
                deleted_count += 1
            else:
                storage_failures.append(f'persistent:{key}')
        except Exception as e:
            logger.error('Failed to delete persistent object %s: %s', key, e)
            storage_failures.append(f'persistent:{key}')

    for key in keys_by_role.get('processing', []):
        try:
            ok = await processing_client().delete_object(key)
            if ok:
                deleted_count += 1
            else:
                storage_failures.append(f'processing:{key}')
        except Exception as e:
            logger.error('Failed to delete processing object %s: %s', key, e)
            storage_failures.append(f'processing:{key}')

    if storage_failures:
        logger.warning(
            'Some storage objects could not be deleted for MediaFile %s: %s — '
            'DB record preserved for retry.',
            file_uuid, storage_failures,
        )
        await MediaFile.objects.mark_failed(
            media_file, f'Partial delete: {len(storage_failures)} storage object(s) remain',
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f'File partially deleted: {deleted_count} storage object(s) removed, '
                f'{len(storage_failures)} failed. Record preserved for retry.'
            ),
        )

    await sync_to_async(media_file.delete)()

    return {
        'detail': 'File deleted successfully',
        'file_uuid': str(file_uuid),
        'storage_objects_deleted': deleted_count,
    }
