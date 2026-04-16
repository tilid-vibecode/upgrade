import logging
from typing import List, Optional, TYPE_CHECKING

from asgiref.sync import sync_to_async
from django.db import models

if TYPE_CHECKING:
    from organization.models import Organization
    from authentication.models import User
    from company_intake.models import IntakeWorkspace
    from .models import MediaFile, MediaFileVariant

logger = logging.getLogger(__name__)


class MediaFileManager(models.Manager):
    async def create_pending(
        self,
        organization: Optional['Organization'],
        uploaded_by: Optional['User'],
        original_filename: str,
        content_type: str,
        file_size: int,
        file_category: str,
        persistent_key: Optional[str] = None,
        processing_key: Optional[str] = None,
        discussion=None,
        prototype_workspace: Optional['IntakeWorkspace'] = None,
    ) -> 'MediaFile':
        from .models import MediaFile

        if not persistent_key and not processing_key:
            raise ValueError(
                'At least one of persistent_key or processing_key is required.'
            )

        media_file = await sync_to_async(self.create)(
            organization=organization,
            uploaded_by=uploaded_by,
            original_filename=original_filename,
            content_type=content_type,
            file_size=file_size,
            file_category=file_category,
            persistent_key=persistent_key,
            processing_key=processing_key,
            status=MediaFile.Status.PENDING,
            discussion=discussion,
            prototype_workspace=prototype_workspace,
        )
        if prototype_workspace is not None:
            org_label = f'workspace:{prototype_workspace.slug}'
        else:
            org_label = str(getattr(organization, 'uuid', 'prototype-public'))
        logger.info(
            'Created MediaFile %s (PENDING) for scope %s: %s (%s bytes, '
            'persistent=%s, processing=%s)',
            media_file.uuid,
            org_label,
            original_filename,
            file_size,
            persistent_key is not None,
            processing_key is not None,
        )
        return media_file

    async def create_processing_only(
        self,
        organization: Optional['Organization'],
        original_filename: str,
        content_type: str,
        file_size: int,
        file_category: str,
        processing_key: str,
        uploaded_by: Optional['User'] = None,
        discussion=None,
    ) -> 'MediaFile':
        from .models import MediaFile

        media_file = await sync_to_async(self.create)(
            organization=organization,
            uploaded_by=uploaded_by,
            original_filename=original_filename,
            content_type=content_type,
            file_size=file_size,
            file_category=file_category,
            persistent_key=None,
            processing_key=processing_key,
            status=MediaFile.Status.UPLOADED,
            discussion=discussion,
        )
        logger.info(
            'Created processing-only MediaFile %s: %s (%s bytes)',
            media_file.uuid,
            original_filename,
            file_size,
        )
        return media_file

    async def mark_uploaded(self, media_file: 'MediaFile') -> 'MediaFile':
        from .models import MediaFile, MediaFileVariant

        media_file.status = MediaFile.Status.UPLOADED
        await sync_to_async(media_file.save)(update_fields=['status', 'updated_at'])

        await sync_to_async(MediaFileVariant.objects.create)(
            source_file=media_file,
            variant_type=MediaFileVariant.VariantType.ORIGINAL,
            content_type=media_file.content_type,
            file_size=media_file.file_size,
            persistent_key=media_file.persistent_key,
            processing_key=media_file.processing_key,
        )
        logger.info(
            'MediaFile %s marked UPLOADED, original variant created.',
            media_file.uuid,
        )
        return media_file

    async def mark_failed(
        self,
        media_file: 'MediaFile',
        error_msg: str,
    ) -> 'MediaFile':
        from .models import MediaFile

        media_file.status = MediaFile.Status.FAILED
        media_file.error_msg = error_msg
        await sync_to_async(media_file.save)(
            update_fields=['status', 'error_msg', 'updated_at'],
        )
        logger.warning('MediaFile %s marked FAILED: %s', media_file.uuid, error_msg)
        return media_file

    async def mark_processing(self, media_file: 'MediaFile') -> 'MediaFile':
        from .models import MediaFile

        media_file.status = MediaFile.Status.PROCESSING
        await sync_to_async(media_file.save)(update_fields=['status', 'updated_at'])
        logger.info('MediaFile %s marked PROCESSING.', media_file.uuid)
        return media_file

    async def mark_ready(
        self,
        media_file: 'MediaFile',
        processing_description: str = '',
        processing_metadata: Optional[dict] = None,
    ) -> 'MediaFile':
        from .models import MediaFile

        media_file.status = MediaFile.Status.READY
        media_file.processing_description = processing_description
        if processing_metadata is not None:
            media_file.processing_metadata = processing_metadata
        await sync_to_async(media_file.save)(
            update_fields=[
                'status',
                'processing_description',
                'processing_metadata',
                'updated_at',
            ],
        )
        logger.info('MediaFile %s marked READY.', media_file.uuid)
        return media_file

    async def get_for_org(
        self,
        organization_uuid: str,
        file_uuid: str,
    ) -> Optional['MediaFile']:
        try:
            media_file = await sync_to_async(
                self.select_related('organization', 'uploaded_by').get
            )(
                uuid=file_uuid,
                organization__uuid=organization_uuid,
            )
            return media_file
        except self.model.DoesNotExist:
            return None

    async def get_by_uuid(self, file_uuid: str) -> Optional['MediaFile']:
        try:
            return await sync_to_async(
                self.select_related('organization', 'uploaded_by', 'prototype_workspace').get
            )(uuid=file_uuid)
        except self.model.DoesNotExist:
            return None

    async def get_for_prototype_workspace(
        self,
        workspace_pk: int,
        file_uuid: str,
    ) -> Optional['MediaFile']:
        """Fetch a media file that belongs to a specific prototype workspace.

        Returns None if the file does not exist or belongs to a different scope.
        Prototype routes should use this instead of ``get_by_uuid()``.
        """
        try:
            return await sync_to_async(
                self.select_related('uploaded_by', 'prototype_workspace').get
            )(
                uuid=file_uuid,
                prototype_workspace_id=workspace_pk,
                organization__isnull=True,
                discussion__isnull=True,
            )
        except self.model.DoesNotExist:
            return None

    async def list_for_org(
        self,
        organization_uuid: str,
        file_category: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List['MediaFile']:
        qs = (
            self.filter(organization__uuid=organization_uuid)
            .select_related('uploaded_by')
            .order_by('-created_at')
        )

        if file_category:
            qs = qs.filter(file_category=file_category)
        if status:
            qs = qs.filter(status=status)

        return await sync_to_async(list)(qs[offset:offset + limit])

    async def list_unscoped(
        self,
        file_category: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List['MediaFile']:
        qs = self.select_related('uploaded_by').order_by('-created_at')
        if file_category:
            qs = qs.filter(file_category=file_category)
        if status:
            qs = qs.filter(status=status)
        return await sync_to_async(list)(qs[offset:offset + limit])

    async def list_public_prototype(
        self,
        file_category: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List['MediaFile']:
        qs = (
            self.filter(
                prototype_workspace__isnull=True,
                organization__isnull=True,
                discussion__isnull=True,
            )
            .select_related('uploaded_by')
            .order_by('-created_at')
        )
        if file_category:
            qs = qs.filter(file_category=file_category)
        if status:
            qs = qs.filter(status=status)
        return await sync_to_async(list)(qs[offset:offset + limit])

    async def list_for_prototype_workspace(
        self,
        workspace_slug: str,
        file_category: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List['MediaFile']:
        qs = (
            self.filter(
                prototype_workspace__slug=workspace_slug,
                organization__isnull=True,
                discussion__isnull=True,
            )
            .select_related('uploaded_by', 'prototype_workspace')
            .order_by('-created_at')
        )
        if file_category:
            qs = qs.filter(file_category=file_category)
        if status:
            qs = qs.filter(status=status)
        return await sync_to_async(list)(qs[offset:offset + limit])

    async def count_for_org(
        self,
        organization_uuid: str,
        file_category: Optional[str] = None,
        status: Optional[str] = None,
    ) -> int:
        qs = self.filter(organization__uuid=organization_uuid)

        if file_category:
            qs = qs.filter(file_category=file_category)
        if status:
            qs = qs.filter(status=status)

        return await sync_to_async(qs.count)()

    async def count_unscoped(
        self,
        file_category: Optional[str] = None,
        status: Optional[str] = None,
    ) -> int:
        qs = self.all()
        if file_category:
            qs = qs.filter(file_category=file_category)
        if status:
            qs = qs.filter(status=status)
        return await sync_to_async(qs.count)()

    async def count_public_prototype(
        self,
        file_category: Optional[str] = None,
        status: Optional[str] = None,
    ) -> int:
        qs = self.filter(
            prototype_workspace__isnull=True,
            organization__isnull=True,
            discussion__isnull=True,
        )
        if file_category:
            qs = qs.filter(file_category=file_category)
        if status:
            qs = qs.filter(status=status)
        return await sync_to_async(qs.count)()

    async def count_for_prototype_workspace(
        self,
        workspace_slug: str,
        file_category: Optional[str] = None,
        status: Optional[str] = None,
    ) -> int:
        qs = self.filter(
            prototype_workspace__slug=workspace_slug,
            organization__isnull=True,
            discussion__isnull=True,
        )
        if file_category:
            qs = qs.filter(file_category=file_category)
        if status:
            qs = qs.filter(status=status)
        return await sync_to_async(qs.count)()

    async def update_description(
        self,
        media_file: 'MediaFile',
        description: str,
    ) -> 'MediaFile':
        media_file.processing_description = description
        await sync_to_async(media_file.save)(
            update_fields=['processing_description', 'updated_at'],
        )
        return media_file

    async def clear_processing_key(
        self,
        media_file: 'MediaFile',
    ) -> 'MediaFile':
        media_file.processing_key = None
        await sync_to_async(media_file.save)(
            update_fields=['processing_key', 'updated_at'],
        )
        logger.info(
            'Cleared processing_key on MediaFile %s.', media_file.uuid,
        )
        return media_file

    async def delete_with_variants(
        self,
        media_file: 'MediaFile',
        db_delete: bool = True,
    ) -> dict:
        from .models import MediaFileVariant

        variants = await sync_to_async(list)(
            MediaFileVariant.objects.filter(source_file=media_file)
        )

        persistent_keys: List[str] = []
        processing_keys: List[str] = []

        for variant in variants:
            if variant.persistent_key:
                persistent_keys.append(variant.persistent_key)
            if variant.processing_key:
                processing_keys.append(variant.processing_key)

        if media_file.persistent_key and media_file.persistent_key not in persistent_keys:
            persistent_keys.append(media_file.persistent_key)
        if media_file.processing_key and media_file.processing_key not in processing_keys:
            processing_keys.append(media_file.processing_key)

        if db_delete:
            await sync_to_async(media_file.delete)()
            logger.info(
                'Deleted MediaFile %s and %d variant(s) from DB.',
                media_file.uuid,
                len(variants),
            )

        return {
            'persistent': persistent_keys,
            'processing': processing_keys,
        }
