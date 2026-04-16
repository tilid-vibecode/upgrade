import asyncio
import logging
import os
import uuid as uuid_mod
from typing import Any, Optional

from asgiref.sync import sync_to_async
from fastapi import HTTPException, UploadFile, status

from .constants import (
    ALLOWED_CONTENT_TYPES,
    ALLOWED_EXTENSIONS,
    MAX_FILE_SIZE_BYTES,
    MAX_FILE_SIZE_MB,
    SIGNED_URL_EXPIRY_SECONDS,
    resolve_file_category,
)
from .entities import (
    MediaFileDetailResponse,
    MediaFileResponse,
    MediaFileVariantResponse,
)
from .processors import get_processor
from .processors.tabular_processor import TabularProcessor

logger = logging.getLogger(__name__)

_BASELINE_ANALYSIS_KINDS = {
    'image': ['image_metadata'],
    'document': ['pdf_metadata', 'pdf_text_extraction', 'pdf_scan_detection'],
    'word': ['docx_text_extraction', 'docx_structure'],
    'text': ['text_extraction', 'text_structure'],
    'spreadsheet': ['tabular_schema_profile', 'tabular_sample_rows'],
}


def validate_upload_file(file: UploadFile) -> None:
    ct = (file.content_type or '').lower().strip()
    if ct not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"File type '{ct}' is not allowed. Accepted types: images, PDF, Word documents, "
                'text files, CSV, TSV, and XLSX spreadsheets.'
            ),
        )

    filename = file.filename or ''
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File extension '{ext}' is not allowed for '{filename}'.",
        )


def validate_file_size(content: bytes, filename: str) -> None:
    if len(content) > MAX_FILE_SIZE_BYTES:
        actual_mb = len(content) / (1024 * 1024)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File '{filename}' exceeds maximum size of {MAX_FILE_SIZE_MB:.0f}MB (was {actual_mb:.1f}MB).",
        )


def build_media_file_response(media_file) -> MediaFileResponse:
    uploaded_by_email = None
    uploaded_by_uuid = None
    if media_file.uploaded_by:
        uploaded_by_email = media_file.uploaded_by.email
        uploaded_by_uuid = getattr(media_file.uploaded_by, 'uuid', None)

    return MediaFileResponse(
        uuid=media_file.uuid,
        original_filename=media_file.original_filename,
        content_type=media_file.content_type,
        file_size=media_file.file_size,
        file_category=media_file.file_category,
        status=media_file.status,
        error_msg=media_file.error_msg,
        processing_description=media_file.processing_description,
        has_persistent=media_file.persistent_key is not None,
        has_processing=media_file.processing_key is not None,
        created_at=media_file.created_at,
        updated_at=media_file.updated_at,
        uploaded_by_email=uploaded_by_email,
        uploaded_by_uuid=uploaded_by_uuid,
    )


async def build_media_file_detail_response(media_file) -> MediaFileDetailResponse:
    from .models import MediaFileVariant

    uploaded_by_email = None
    uploaded_by_uuid = None
    if media_file.uploaded_by:
        uploaded_by_email = media_file.uploaded_by.email
        uploaded_by_uuid = getattr(media_file.uploaded_by, 'uuid', None)

    variants_qs = MediaFileVariant.objects.filter(
        source_file=media_file,
    ).order_by('variant_type')
    variant_list = await sync_to_async(list)(variants_qs)

    variant_responses = [
        MediaFileVariantResponse(
            uuid=v.uuid,
            variant_type=v.variant_type,
            content_type=v.content_type,
            file_size=v.file_size,
            width=v.width,
            height=v.height,
            metadata=v.metadata,
            created_at=v.created_at,
        )
        for v in variant_list
    ]

    return MediaFileDetailResponse(
        uuid=media_file.uuid,
        original_filename=media_file.original_filename,
        content_type=media_file.content_type,
        file_size=media_file.file_size,
        file_category=media_file.file_category,
        status=media_file.status,
        error_msg=media_file.error_msg,
        processing_description=media_file.processing_description,
        processing_metadata=media_file.processing_metadata,
        has_persistent=media_file.persistent_key is not None,
        has_processing=media_file.processing_key is not None,
        created_at=media_file.created_at,
        updated_at=media_file.updated_at,
        uploaded_by_email=uploaded_by_email,
        uploaded_by_uuid=uploaded_by_uuid,
        variants=variant_responses,
    )


async def build_media_analysis_snapshot(
    media_file,
    *,
    file_bytes: bytes,
    extra_metadata: Optional[dict] = None,
) -> dict[str, Any]:
    processor = get_processor(media_file.file_category)
    analysis_kinds = _BASELINE_ANALYSIS_KINDS.get(media_file.file_category, [])
    results = []
    if processor and analysis_kinds:
        results = await processor.run_baseline(
            file_bytes=file_bytes,
            media_file=media_file,
            tier=0,
            analysis_kinds=analysis_kinds,
        )

    summaries = [result.summary_text for result in results if result.summary_text]
    description = ' | '.join(summaries[:3]).strip()
    if not description:
        category_label = resolve_file_category(media_file.content_type, media_file.original_filename).replace('_', ' ')
        description = f'{category_label.title()} upload stored successfully.'

    metadata = {
        'analysis_version': 'media-basics-v1',
        'file_profile': {
            'original_filename': media_file.original_filename,
            'content_type': media_file.content_type,
            'file_size': media_file.file_size,
            'file_category': media_file.file_category,
        },
        'analysis': {
            'completed': True,
            'kinds': [result.analysis_kind for result in results],
            'summaries': summaries,
            'results': {
                result.analysis_kind: _compact_result_json(
                    result.analysis_kind,
                    result.result_json,
                )
                for result in results
            },
        },
    }
    if extra_metadata:
        metadata.update(extra_metadata)

    return {
        'description': description[:600],
        'metadata': metadata,
    }


async def enrich_media_file_after_upload(
    media_file,
    *,
    file_bytes: bytes,
    extra_metadata: Optional[dict] = None,
):
    from .models import MediaFile

    try:
        snapshot = await build_media_analysis_snapshot(
            media_file,
            file_bytes=file_bytes,
            extra_metadata=extra_metadata,
        )
    except Exception as exc:
        logger.warning(
            'Automatic media analysis failed for %s (%s): %s',
            media_file.uuid,
            media_file.original_filename,
            exc,
            exc_info=True,
        )
        media_file.processing_metadata = {
            **(extra_metadata or {}),
            'analysis_version': 'media-basics-v1',
            'analysis_error': str(exc),
        }
        await sync_to_async(media_file.save)(update_fields=['processing_metadata', 'updated_at'])
        return media_file

    return await MediaFile.objects.mark_ready(
        media_file,
        processing_description=snapshot['description'],
        processing_metadata=snapshot['metadata'],
    )


async def extract_media_text_for_parsing(media_file, file_bytes: bytes) -> Optional[dict[str, Any]]:
    processor = get_processor(media_file.file_category)
    if processor is None:
        return None

    if media_file.file_category == 'document':
        results = await processor.run_baseline(
            file_bytes=file_bytes,
            media_file=media_file,
            tier=0,
            analysis_kinds=['pdf_metadata', 'pdf_text_extraction', 'pdf_scan_detection'],
        )
        results_by_kind = {result.analysis_kind: result for result in results}
        text = (
            results_by_kind.get('pdf_text_extraction')
            and results_by_kind['pdf_text_extraction'].result_json.get('text', '')
        ) or ''
        if not text.strip():
            return None
        metadata = {
            'processor': 'pdf',
            'page_count': (
                results_by_kind.get('pdf_metadata')
                and results_by_kind['pdf_metadata'].result_json.get('page_count')
            ),
            'title': (
                results_by_kind.get('pdf_metadata')
                and results_by_kind['pdf_metadata'].result_json.get('title')
            ) or '',
            'author': (
                results_by_kind.get('pdf_metadata')
                and results_by_kind['pdf_metadata'].result_json.get('author')
            ) or '',
            'is_scanned': (
                results_by_kind.get('pdf_scan_detection')
                and results_by_kind['pdf_scan_detection'].result_json.get('is_scanned')
            ),
            'page_texts': (
                results_by_kind.get('pdf_text_extraction')
                and results_by_kind['pdf_text_extraction'].result_json.get('page_texts')
            ) or [],
            'pages_extracted': (
                results_by_kind.get('pdf_text_extraction')
                and results_by_kind['pdf_text_extraction'].result_json.get('pages_extracted')
            ),
            'total_pages': (
                results_by_kind.get('pdf_text_extraction')
                and results_by_kind['pdf_text_extraction'].result_json.get('total_pages')
            ),
            'truncated': (
                results_by_kind.get('pdf_text_extraction')
                and results_by_kind['pdf_text_extraction'].result_json.get('truncated')
            ),
        }
        return {
            'text': text,
            'content_type': 'application/pdf',
            'page_count': metadata['page_count'],
            'metadata': metadata,
        }

    if media_file.file_category == 'word':
        results = await processor.run_baseline(
            file_bytes=file_bytes,
            media_file=media_file,
            tier=0,
            analysis_kinds=['docx_text_extraction', 'docx_structure'],
        )
        results_by_kind = {result.analysis_kind: result for result in results}
        text = (
            results_by_kind.get('docx_text_extraction')
            and results_by_kind['docx_text_extraction'].result_json.get('text', '')
        ) or ''
        if not text.strip():
            return None
        structure = (
            results_by_kind.get('docx_structure')
            and results_by_kind['docx_structure'].result_json
        ) or {}
        return {
            'text': text,
            'content_type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'page_count': None,
            'metadata': {
                'processor': 'docx',
                'paragraph_count': (
                    results_by_kind.get('docx_text_extraction')
                    and results_by_kind['docx_text_extraction'].result_json.get('paragraph_count')
                ),
                'heading_count': structure.get('heading_count'),
                'table_count': structure.get('table_count'),
                'image_count': structure.get('image_count'),
            },
        }

    if media_file.file_category == 'text':
        results = await processor.run_baseline(
            file_bytes=file_bytes,
            media_file=media_file,
            tier=0,
            analysis_kinds=['text_extraction', 'text_structure'],
        )
        results_by_kind = {result.analysis_kind: result for result in results}
        text = (
            results_by_kind.get('text_extraction')
            and results_by_kind['text_extraction'].result_json.get('text', '')
        ) or ''
        if not text.strip():
            return None
        structure = (
            results_by_kind.get('text_structure')
            and results_by_kind['text_structure'].result_json
        ) or {}
        return {
            'text': text,
            'content_type': 'text/plain',
            'page_count': None,
            'metadata': {
                'processor': 'text',
                'line_count': structure.get('line_count'),
                'word_count': structure.get('word_count'),
                'detected_format': structure.get('detected_format'),
            },
        }

    if media_file.file_category == 'spreadsheet' and isinstance(processor, TabularProcessor):
        dataframe, parse_info = processor.parse_dataframe(file_bytes, media_file)
        if dataframe is None:
            return None
        csv_text = processor.dataframe_to_csv_text(dataframe)
        return {
            'text': csv_text,
            'content_type': 'text/csv',
            'page_count': None,
            'metadata': {
                'processor': 'tabular',
                'row_count': int(len(dataframe)),
                'column_count': int(len(dataframe.columns)),
                'columns': [str(column) for column in list(dataframe.columns)[:100]],
                'parse_info': parse_info,
            },
        }

    return None


def _compact_result_json(analysis_kind: str, result_json: dict[str, Any]) -> dict[str, Any]:
    compacted = _truncate_jsonish(result_json)
    if analysis_kind in {'pdf_text_extraction', 'docx_text_extraction', 'text_extraction'}:
        source_text = str(result_json.get('text') or '')
        compacted.pop('text', None)
        if source_text:
            compacted['text_preview'] = source_text[:1000]
            compacted['text_preview_char_count'] = len(compacted['text_preview'])
            compacted['text_total_char_count'] = len(source_text)
    if analysis_kind == 'tabular_schema_profile':
        compacted['columns'] = (compacted.get('columns') or [])[:50]
    if analysis_kind == 'tabular_sample_rows':
        compacted['head'] = (compacted.get('head') or [])[:3]
        compacted['tail'] = (compacted.get('tail') or [])[:2]
    return compacted


def _truncate_jsonish(value: Any, *, max_items: int = 20, max_string_length: int = 500, depth: int = 0) -> Any:
    if depth >= 4:
        return _truncate_leaf(value, max_string_length=max_string_length)

    if isinstance(value, dict):
        items = list(value.items())[:max_items]
        return {
            str(key): _truncate_jsonish(item, max_items=max_items, max_string_length=max_string_length, depth=depth + 1)
            for key, item in items
        }

    if isinstance(value, list):
        return [
            _truncate_jsonish(item, max_items=max_items, max_string_length=max_string_length, depth=depth + 1)
            for item in value[:max_items]
        ]

    return _truncate_leaf(value, max_string_length=max_string_length)


def _truncate_leaf(value: Any, *, max_string_length: int = 500) -> Any:
    if isinstance(value, str):
        return value[:max_string_length]
    return value


async def generate_signed_url_for_file(
    media_file,
    expiry: int = SIGNED_URL_EXPIRY_SECONDS,
    *,
    response_content_disposition: str | None = None,
    response_content_type: str | None = None,
) -> Optional[str]:
    if not media_file.persistent_key:
        return None

    from server.storage import persistent_client

    return await persistent_client().generate_signed_url(
        key=media_file.persistent_key,
        expiry=expiry,
        response_content_disposition=response_content_disposition,
        response_content_type=response_content_type,
    )


async def generate_signed_url_for_variant(
    variant,
    expiry: int = SIGNED_URL_EXPIRY_SECONDS,
) -> Optional[str]:
    if not variant.persistent_key:
        return None

    from server.storage import persistent_client

    return await persistent_client().generate_signed_url(
        key=variant.persistent_key,
        expiry=expiry,
    )


async def store_prototype_generated_text_artifact(
    *,
    scope: str,
    filename: str,
    content: str | bytes,
    content_type: str,
    description: str,
    metadata: Optional[dict[str, Any]] = None,
    prototype_workspace=None,
):
    from server.storage import persistent_client, processing_client
    from server.storage.helpers import (
        build_prototype_persistent_key,
        build_prototype_processing_key,
    )
    from .models import MediaFile

    file_uuid = str(uuid_mod.uuid4())
    raw_content = content.encode('utf-8') if isinstance(content, str) else content
    persistent_key = build_prototype_persistent_key(
        file_uuid=file_uuid,
        filename=filename,
        scope=scope,
    )
    processing_key = build_prototype_processing_key(
        file_uuid=file_uuid,
        filename=filename,
        scope=scope,
    )

    upload_metadata = {'scope': scope, 'generated': 'true'}
    if metadata:
        upload_metadata.update({key: str(value) for key, value in metadata.items() if value is not None})

    persistent_ok = False
    processing_ok = False
    media_file = None
    try:
        async def _upload_persistent() -> None:
            nonlocal persistent_ok
            await persistent_client().upload_bytes(
                key=persistent_key,
                data=raw_content,
                content_type=content_type,
                metadata=upload_metadata,
            )
            persistent_ok = True

        async def _upload_processing() -> None:
            nonlocal processing_ok
            await processing_client().upload_bytes(
                key=processing_key,
                data=raw_content,
                content_type=content_type,
                metadata=upload_metadata,
            )
            processing_ok = True

        await asyncio.gather(_upload_persistent(), _upload_processing())

        media_file = await MediaFile.objects.create_pending(
            organization=None,
            uploaded_by=None,
            original_filename=filename,
            content_type=content_type,
            file_size=len(raw_content),
            file_category='text',
            persistent_key=persistent_key,
            processing_key=processing_key,
            prototype_workspace=prototype_workspace,
        )
        media_file.processing_metadata = {
            **(metadata or {}),
            'scope': scope,
            'generated': True,
            'content_type': content_type,
        }
        await sync_to_async(media_file.save)(update_fields=['processing_metadata', 'updated_at'])
        await MediaFile.objects.mark_uploaded(media_file)
        await MediaFile.objects.mark_ready(
            media_file,
            processing_description=description,
            processing_metadata=media_file.processing_metadata,
        )
        return media_file
    except Exception:
        if media_file is not None:
            try:
                await sync_to_async(media_file.delete)()
            except Exception:
                logger.exception('Failed to delete partially created generated artifact MediaFile %s', media_file.uuid)
        if persistent_ok:
            try:
                await persistent_client().delete_object(persistent_key)
            except Exception:
                logger.warning('Failed to clean up persistent generated artifact %s', persistent_key, exc_info=True)
        if processing_ok:
            try:
                await processing_client().delete_object(processing_key)
            except Exception:
                logger.warning('Failed to clean up processing generated artifact %s', processing_key, exc_info=True)
        raise
