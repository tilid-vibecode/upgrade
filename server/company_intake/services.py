import asyncio
import ipaddress
import logging
import mimetypes
import os
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse
from uuid import uuid4

from asgiref.sync import sync_to_async
from django.db import transaction
from django.utils import timezone as django_timezone
from fastapi import HTTPException, UploadFile, status

from server.storage import persistent_client, processing_client

from .entities import (
    IntakeWorkspaceDetailResponse,
    IntakeWorkspaceResponse,
    SourceDocumentResponse,
    WorkspaceBlueprintStateResponse,
    WorkspaceCompanyProfilePayload,
    WorkspacePilotScopePayload,
    WorkspaceProfileUpdateRequest,
    WorkspaceReadinessFlagsResponse,
    WorkspaceReadinessResponse,
    WorkspaceStageBlockersResponse,
    WorkspaceSectionCompletenessResponse,
    WorkspaceSourceChecklistPayload,
    WorkspaceSourceCreateRequest,
    WorkspaceSourceRequirementResponse,
    WorkspaceSourceResponse,
    WorkspaceSourceUpdateRequest,
    WorkspaceWorkflowStageResponse,
    WorkspaceWorkflowStatusResponse,
    WorkspaceWorkflowSummaryResponse,
)
from .models import (
    IntakeWorkspace,
    SourceDocument,
    SourceDocumentKind,
    SourceDocumentStatus,
    WorkspaceSource,
    WorkspaceSourceKind,
    WorkspaceSourceStatus,
    WorkspaceSourceTransport,
    WorkspaceStatus,
)

logger = logging.getLogger(__name__)

_ALLOWED_CONTENT_TYPES = {
    'text/csv': SourceDocumentKind.CSV,
    'application/csv': SourceDocumentKind.CSV,
    'application/vnd.ms-excel': SourceDocumentKind.CSV,
    'application/pdf': SourceDocumentKind.PDF,
}
_ALLOWED_EXTENSIONS = {
    '.csv': SourceDocumentKind.CSV,
    '.pdf': SourceDocumentKind.PDF,
}
_MAX_FILE_SIZE_MB = int(os.getenv('COMPANY_INTAKE_MAX_FILE_SIZE_MB', '25'))
_MAX_FILE_SIZE_BYTES = _MAX_FILE_SIZE_MB * 1024 * 1024

_SSRF_BLOCKED_HOSTS = {
    'localhost', '127.0.0.1', '0.0.0.0', '169.254.169.254',
    'metadata.google.internal', 'metadata.goog',
}


def _validate_external_url(url: str) -> None:
    """Reject URLs that could cause server-side request forgery."""
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Only http and https URLs are allowed for external sources.',
        )
    hostname = (parsed.hostname or '').lower()
    if not hostname:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='External URL must include a valid hostname.',
        )
    if hostname in _SSRF_BLOCKED_HOSTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='This URL target is not allowed.',
        )
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Private or internal IP addresses are not allowed for external sources.',
            )
    except ValueError:
        pass  # hostname is a domain name — allowed


_WORKSPACE_PROFILE_SCHEMA_VERSION = 'stage1-v1'
_PROFILE_TOP_LEVEL_KEYS = {
    'schema_version',
    'company_profile',
    'pilot_scope',
    'source_checklist',
    'operator_notes',
}
_COMPANY_PROFILE_REQUIRED_FIELDS = {
    'company_name': 'company name',
    'company_description': 'short company description',
    'main_products': 'main products',
    'primary_market_geography': 'primary market / geography',
    'target_customers': 'target customers',
    'rough_employee_count': 'rough employee count',
}
_COMPANY_PROFILE_RECOMMENDED_FIELDS = {
    'website_url': 'website or primary URL',
    'locations': 'team locations or hiring geographies',
    'current_tech_stack': 'current tech stack',
    'planned_tech_stack': 'planned tech stack or major tooling shifts',
    'pilot_scope_notes': 'pilot scope notes',
    'notable_constraints_or_growth_plans': 'constraints or growth plans',
}
_PILOT_SCOPE_FIELD_LABELS = {
    'scope_mode': 'scope mode',
    'departments_in_scope': 'departments in scope',
    'roles_in_scope': 'roles in scope',
    'products_in_scope': 'products in scope',
    'employee_count_in_scope': 'employee count in scope',
    'stakeholder_contact': 'stakeholder contact',
    'analyst_notes': 'analyst notes',
}
_MEDIA_CATEGORIES_BY_SOURCE_KIND = {
    WorkspaceSourceKind.ORG_CSV: {'spreadsheet'},
    WorkspaceSourceKind.EMPLOYEE_CV: {'document', 'word', 'text'},
    WorkspaceSourceKind.JOB_DESCRIPTION: {'document', 'word', 'text'},
    WorkspaceSourceKind.EXISTING_MATRIX: {'spreadsheet', 'document', 'word', 'text'},
    WorkspaceSourceKind.ROADMAP: {'document', 'word', 'text', 'spreadsheet'},
    WorkspaceSourceKind.STRATEGY: {'document', 'word', 'text', 'spreadsheet'},
    WorkspaceSourceKind.OTHER: {'image', 'document', 'word', 'text', 'spreadsheet'},
}
_PLANNING_CONTEXT_SOURCE_USAGE_BY_KIND = {
    WorkspaceSourceKind.ROADMAP: 'roadmap',
    WorkspaceSourceKind.STRATEGY: 'strategy',
    WorkspaceSourceKind.JOB_DESCRIPTION: 'role_reference',
    WorkspaceSourceKind.ORG_CSV: 'org_structure',
    WorkspaceSourceKind.EMPLOYEE_CV: 'employee_cv',
    WorkspaceSourceKind.EXISTING_MATRIX: 'other',
    WorkspaceSourceKind.OTHER: 'other',
}
_SOURCE_REQUIREMENTS = [
    {
        'key': 'roadmap_or_strategy',
        'label': 'Roadmap or strategy',
        'source_kinds': [WorkspaceSourceKind.ROADMAP, WorkspaceSourceKind.STRATEGY],
        'required': True,
        'required_for_parse': True,
        'required_for_roadmap_analysis': True,
        'required_for_blueprint': True,
        'required_for_evidence': False,
    },
    {
        'key': 'org_csv',
        'label': 'Organization spreadsheet',
        'source_kinds': [WorkspaceSourceKind.ORG_CSV],
        'required': True,
        'required_for_parse': True,
        'required_for_roadmap_analysis': False,
        'required_for_blueprint': True,
        'required_for_evidence': False,
    },
    {
        'key': 'employee_cv_set',
        'label': 'Employee CV set',
        'source_kinds': [WorkspaceSourceKind.EMPLOYEE_CV],
        'required': True,
        'required_for_parse': False,
        'required_for_roadmap_analysis': False,
        'required_for_blueprint': False,
        'required_for_evidence': True,
    },
    {
        'key': 'role_references',
        'label': 'Job descriptions (optional)',
        'source_kinds': [WorkspaceSourceKind.JOB_DESCRIPTION],
        'required': False,
        'required_for_parse': False,
        'required_for_roadmap_analysis': False,
        'required_for_blueprint': False,
        'required_for_evidence': False,
    },
    {
        'key': 'existing_matrix',
        'label': 'Existing matrix (optional)',
        'source_kinds': [WorkspaceSourceKind.EXISTING_MATRIX],
        'required': False,
        'required_for_parse': False,
        'required_for_roadmap_analysis': False,
        'required_for_blueprint': False,
        'required_for_evidence': False,
    },
]
_WORKFLOW_STAGE_ORDER = [
    ('context', 'Workspace context'),
    ('sources', 'Source collection'),
    ('parse', 'Parsing and normalization'),
    ('roadmap_analysis', 'Roadmap analysis'),
    ('blueprint', 'Blueprint generation'),
    ('clarifications', 'Clarifications and publication'),
    ('evidence', 'CV evidence and role matching'),
    ('assessments', 'Assessments'),
    ('matrix', 'Evidence matrix'),
    ('plans', 'Development plans'),
]
_STAGE_DEPENDENCIES = {
    'context': [],
    'sources': ['context'],
    'parse': ['sources'],
    'roadmap_analysis': ['parse'],
    'blueprint': ['roadmap_analysis'],
    'clarifications': ['blueprint'],
    'evidence': ['clarifications'],
    'assessments': ['evidence'],
    'matrix': ['assessments'],
    'plans': ['matrix'],
}
_WORKFLOW_PENDING_STATUSES = {
    'not_started',
    'blocked',
    'ready',
    'running',
    'action_required',
    'failed',
}


def build_workspace_slug(company_name: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', company_name.lower()).strip('-')
    return slug or 'company'


def build_persistent_key(
    workspace_slug: str,
    document_uuid: str,
    filename: str,
) -> str:
    safe_name = os.path.basename(filename)
    now = datetime.now(timezone.utc)
    return (
        f'company-intake/{workspace_slug}'
        f'/{now.year}/{now.month:02d}/{document_uuid}/persistent/{safe_name}'
    )


def build_processing_key(
    workspace_slug: str,
    document_uuid: str,
    filename: str,
) -> str:
    safe_name = os.path.basename(filename)
    return f'company-intake/{workspace_slug}/{document_uuid}/processing/{safe_name}'


def _resolve_document_kind(content_type: str, filename: str) -> str:
    normalized_ct = content_type.lower().strip()
    if normalized_ct in _ALLOWED_CONTENT_TYPES:
        return _ALLOWED_CONTENT_TYPES[normalized_ct]

    ext = os.path.splitext(filename)[1].lower()
    if ext in _ALLOWED_EXTENSIONS:
        return _ALLOWED_EXTENSIONS[ext]

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail='Only CSV and PDF files are supported in the first intake stage.',
    )


def validate_upload(file: UploadFile, content: bytes) -> str:
    filename = file.filename or 'upload'
    guessed_ct = mimetypes.guess_type(filename)[0] or ''
    content_type = (file.content_type or guessed_ct or 'application/octet-stream').lower()
    document_kind = _resolve_document_kind(content_type, filename)

    if len(content) > _MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f'File "{filename}" exceeds the {_MAX_FILE_SIZE_MB}MB limit for intake uploads.'
            ),
        )

    return document_kind


async def _cleanup_object(key: str, storage_label: str, *, processing: bool) -> None:
    try:
        client = processing_client() if processing else persistent_client()
        await client.delete_object(key)
    except Exception as exc:
        logger.warning('Failed to clean up %s object %s: %s', storage_label, key, exc)


def _normalize_string(value: Any) -> str:
    if value is None:
        return ''
    return str(value).strip()


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    normalized: list[str] = []
    for item in items:
        text = _normalize_string(item)
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _normalize_optional_positive_int(value: Any) -> Optional[int]:
    if value in (None, '', []):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _is_filled(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool([item for item in value if _is_filled(item)])
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, dict):
        return any(_is_filled(item) for item in value.values())
    return bool(value)


def _normalize_company_profile(
    raw: Optional[dict[str, Any]],
    *,
    fallback_company_name: str,
) -> dict[str, Any]:
    source = raw or {}
    company_name = _normalize_string(source.get('company_name')) or _normalize_string(fallback_company_name)
    return {
        'company_name': company_name,
        'website_url': _normalize_string(source.get('website_url')),
        'company_description': _normalize_string(source.get('company_description')),
        'main_products': _normalize_string_list(source.get('main_products')),
        'primary_market_geography': _normalize_string(source.get('primary_market_geography')),
        'locations': _normalize_string_list(source.get('locations')),
        'target_customers': _normalize_string_list(source.get('target_customers')),
        'current_tech_stack': _normalize_string_list(source.get('current_tech_stack')),
        'planned_tech_stack': _normalize_string_list(source.get('planned_tech_stack')),
        'rough_employee_count': _normalize_optional_positive_int(source.get('rough_employee_count')),
        'pilot_scope_notes': _normalize_string(source.get('pilot_scope_notes')),
        'notable_constraints_or_growth_plans': _normalize_string(
            source.get('notable_constraints_or_growth_plans')
        ),
    }


def _normalize_pilot_scope(raw: Optional[dict[str, Any]]) -> dict[str, Any]:
    source = raw or {}
    return {
        'scope_mode': _normalize_string(source.get('scope_mode')),
        'departments_in_scope': _normalize_string_list(source.get('departments_in_scope')),
        'roles_in_scope': _normalize_string_list(source.get('roles_in_scope')),
        'products_in_scope': _normalize_string_list(source.get('products_in_scope')),
        'employee_count_in_scope': _normalize_optional_positive_int(source.get('employee_count_in_scope')),
        'stakeholder_contact': _normalize_string(source.get('stakeholder_contact')),
        'analyst_notes': _normalize_string(source.get('analyst_notes')),
    }


def _normalize_source_checklist(raw: Optional[dict[str, Any]]) -> dict[str, Any]:
    source = raw or {}
    return {
        'existing_matrix_available': source.get('existing_matrix_available'),
        'sales_growth_plan_available': source.get('sales_growth_plan_available'),
        'architecture_overview_available': source.get('architecture_overview_available'),
        'product_notes_available': source.get('product_notes_available'),
        'hr_notes_available': source.get('hr_notes_available'),
        'notes': _normalize_string(source.get('notes')),
    }


def build_workspace_profile_snapshot(workspace: IntakeWorkspace) -> dict[str, Any]:
    raw = dict(workspace.metadata or {})
    return {
        'schema_version': _normalize_string(raw.get('schema_version')) or _WORKSPACE_PROFILE_SCHEMA_VERSION,
        'company_profile': _normalize_company_profile(
            raw.get('company_profile'),
            fallback_company_name=workspace.name,
        ),
        'pilot_scope': _normalize_pilot_scope(raw.get('pilot_scope')),
        'source_checklist': _normalize_source_checklist(raw.get('source_checklist')),
        'operator_notes': _normalize_string(raw.get('operator_notes')) or _normalize_string(workspace.notes),
    }


def context_profile_to_workspace_profile_snapshot(
    workspace: IntakeWorkspace,
    effective_profile: dict[str, Any],
) -> dict[str, Any]:
    snapshot = build_workspace_profile_snapshot(workspace)
    company_profile = dict(snapshot.get('company_profile') or {})
    company_profile.update(dict(effective_profile.get('company_profile') or {}))

    tech_stack = _normalize_string_list(effective_profile.get('tech_stack'))
    if tech_stack:
        company_profile['current_tech_stack'] = tech_stack
        planned_stack = _normalize_string_list(company_profile.get('planned_tech_stack'))
        company_profile['planned_tech_stack'] = planned_stack or list(tech_stack)

    constraints = _normalize_string_list(effective_profile.get('constraints'))
    growth_goals = _normalize_string_list(effective_profile.get('growth_goals'))
    if constraints or growth_goals:
        details: list[str] = []
        if constraints:
            details.append(f"Constraints: {'; '.join(constraints)}")
        if growth_goals:
            details.append(f"Growth goals: {'; '.join(growth_goals)}")
        existing_notes = _normalize_string(company_profile.get('notable_constraints_or_growth_plans'))
        company_profile['notable_constraints_or_growth_plans'] = ' | '.join(
            [item for item in [existing_notes, *details] if item]
        )

    return {
        **snapshot,
        'company_profile': _normalize_company_profile(
            company_profile,
            fallback_company_name=workspace.name,
        ),
    }


def build_planning_context_profile_snapshot(planning_context) -> dict[str, Any]:
    workspace = getattr(planning_context, 'workspace', None)
    if workspace is None:
        return {
            'schema_version': _WORKSPACE_PROFILE_SCHEMA_VERSION,
            'company_profile': _normalize_company_profile(
                dict((planning_context.profile.company_profile if getattr(planning_context, 'profile', None) else {}) or {}),
                fallback_company_name=getattr(planning_context, 'name', ''),
            ),
            'pilot_scope': _normalize_pilot_scope({}),
            'source_checklist': _normalize_source_checklist({}),
            'operator_notes': '',
        }
    effective_profile = type(planning_context).resolve_effective_profile(planning_context)
    return context_profile_to_workspace_profile_snapshot(workspace, effective_profile)


def _merge_workspace_profile_metadata(
    existing_metadata: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    metadata = {
        key: value
        for key, value in (existing_metadata or {}).items()
        if key not in _PROFILE_TOP_LEVEL_KEYS
    }
    metadata.update(
        {
            'schema_version': _WORKSPACE_PROFILE_SCHEMA_VERSION,
            'company_profile': snapshot['company_profile'],
            'pilot_scope': snapshot['pilot_scope'],
            'source_checklist': snapshot['source_checklist'],
            'operator_notes': snapshot['operator_notes'],
        }
    )
    return metadata


def _build_default_context_profile_payload(workspace: IntakeWorkspace) -> dict[str, Any]:
    snapshot = build_workspace_profile_snapshot(workspace)
    metadata = dict(workspace.metadata or {})
    company_profile = dict(snapshot.get('company_profile') or {})
    tech_stack = _normalize_string_list(
        list(metadata.get('tech_stack') or [])
        + list(company_profile.get('current_tech_stack') or [])
        + list(company_profile.get('planned_tech_stack') or [])
    )
    return {
        'company_profile': company_profile,
        'tech_stack': tech_stack,
        'constraints': _normalize_string_list(metadata.get('constraints')),
        'growth_goals': _normalize_string_list(metadata.get('growth_goals')),
        'inherit_from_parent': False,
        'override_fields': [],
    }


def _ensure_default_planning_context_sync(workspace_pk: int):
    from org_context.models import ContextProfile, PlanningContext

    workspace = IntakeWorkspace.objects.get(pk=workspace_pk)
    defaults = {
        'organization_id': workspace.organization_id,
        'name': workspace.name,
        'kind': PlanningContext.Kind.ORG,
        'status': PlanningContext.Status.ACTIVE,
        'description': f'Default organization baseline for workspace {workspace.name}',
        'metadata': {
            'auto_created': True,
            'created_by': 'workspace_service',
        },
    }
    context, _created = PlanningContext.objects.get_or_create(
        workspace=workspace,
        slug='org-baseline',
        defaults=defaults,
    )

    update_fields: list[str] = []
    if context.organization_id != workspace.organization_id:
        context.organization_id = workspace.organization_id
        update_fields.append('organization')
    if context.name != workspace.name:
        context.name = workspace.name
        update_fields.append('name')
    if update_fields:
        update_fields.append('updated_at')
        context.save(update_fields=update_fields)

    ContextProfile.objects.get_or_create(
        planning_context=context,
        defaults=_build_default_context_profile_payload(workspace),
    )
    return context


def _sync_source_default_context_link_sync(source_pk: int) -> None:
    from org_context.models import PlanningContextSource

    source = WorkspaceSource.objects.select_related('workspace').get(pk=source_pk)
    default_context = _ensure_default_planning_context_sync(source.workspace_id)
    usage_type = _PLANNING_CONTEXT_SOURCE_USAGE_BY_KIND.get(source.source_kind, 'other')
    PlanningContextSource.objects.update_or_create(
        planning_context=default_context,
        workspace_source=source,
        defaults={
            'usage_type': usage_type,
            'is_active': True,
            'include_in_blueprint': True,
            'include_in_roadmap_analysis': usage_type in {'roadmap', 'strategy'},
        },
    )


def build_workspace_response(workspace: IntakeWorkspace) -> IntakeWorkspaceResponse:
    return IntakeWorkspaceResponse(
        uuid=workspace.uuid,
        name=workspace.name,
        slug=workspace.slug,
        notes=workspace.notes,
        status=workspace.status,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
    )


def build_workspace_detail_response(workspace: IntakeWorkspace) -> IntakeWorkspaceDetailResponse:
    profile = build_workspace_profile_snapshot(workspace)
    return IntakeWorkspaceDetailResponse(
        uuid=workspace.uuid,
        name=workspace.name,
        slug=workspace.slug,
        notes=workspace.notes,
        status=workspace.status,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
        metadata_schema_version=profile['schema_version'],
        company_profile=WorkspaceCompanyProfilePayload(**profile['company_profile']),
        pilot_scope=WorkspacePilotScopePayload(**profile['pilot_scope']),
        source_checklist=WorkspaceSourceChecklistPayload(**profile['source_checklist']),
        operator_notes=profile['operator_notes'],
        operator_token=workspace.operator_token,
    )


async def build_document_response(
    document: SourceDocument,
    include_signed_url: bool = True,
) -> SourceDocumentResponse:
    signed_url: Optional[str] = None
    if include_signed_url:
        try:
            signed_url = await persistent_client().generate_signed_url(document.persistent_key)
        except Exception:
            signed_url = None

    return SourceDocumentResponse(
        uuid=document.uuid,
        workspace_slug=document.workspace.slug,
        original_filename=document.original_filename,
        content_type=document.content_type,
        file_size=document.file_size,
        document_kind=document.document_kind,
        status=document.status,
        persistent_key=document.persistent_key,
        processing_key=document.processing_key,
        download_url=signed_url,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def build_workspace_source_response(source: WorkspaceSource) -> WorkspaceSourceResponse:
    media_file_uuid = None
    media_filename = None
    if source.media_file_id:
        media_file_uuid = source.media_file.uuid
        media_filename = source.media_file.original_filename

    return WorkspaceSourceResponse(
        uuid=source.uuid,
        workspace_slug=source.workspace.slug,
        title=source.title,
        notes=source.notes,
        source_kind=source.source_kind,
        transport=source.transport,
        media_file_uuid=media_file_uuid,
        media_filename=media_filename,
        external_url=source.external_url,
        inline_text=source.inline_text,
        language_code=source.language_code,
        status=source.status,
        parse_error=source.parse_error,
        parse_metadata=source.parse_metadata,
        archived_at=source.archived_at,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


def _workspace_source_queryset(
    workspace: IntakeWorkspace,
    *,
    include_archived: bool = False,
):
    queryset = WorkspaceSource.objects.select_related('workspace', 'media_file').filter(workspace=workspace)
    if not include_archived:
        queryset = queryset.exclude(status=WorkspaceSourceStatus.ARCHIVED)
    return queryset


async def list_workspace_sources(
    workspace: IntakeWorkspace,
    *,
    include_archived: bool = False,
) -> list[WorkspaceSource]:
    return await sync_to_async(list)(
        _workspace_source_queryset(workspace, include_archived=include_archived).order_by('-created_at')
    )


async def get_workspace_source(
    workspace: IntakeWorkspace,
    source_uuid,
    *,
    include_archived: bool = False,
) -> Optional[WorkspaceSource]:
    return await sync_to_async(
        _workspace_source_queryset(workspace, include_archived=include_archived).filter(uuid=source_uuid).first
    )()


def _claim_media_file_for_workspace_sync(media_file_pk: int, workspace_pk: int):
    """Verify that *media_file_pk* belongs to *workspace_pk*.

    The file must already have ``prototype_workspace`` set (done at upload
    time).  Unclaimed files, org-scoped files, and files belonging to a
    different workspace are all rejected.
    """
    from media_storage.models import MediaFile

    with transaction.atomic():
        media_file = MediaFile.objects.select_for_update().get(pk=media_file_pk)

        if media_file.organization_id is not None or media_file.discussion_id is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Only prototype workspace media files can be attached to prototype sources.',
            )

        if media_file.prototype_workspace_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Media file has no workspace owner. Upload it through the workspace media endpoint first.',
            )

        if media_file.prototype_workspace_id != workspace_pk:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Media file does not belong to this workspace.',
            )

        return media_file


def _apply_workspace_profile_sync(
    workspace_pk,
    *,
    company_profile: Optional[dict[str, Any]] = None,
    pilot_scope: Optional[dict[str, Any]] = None,
    source_checklist: Optional[dict[str, Any]] = None,
    operator_notes: Optional[str] = None,
    notes: Optional[str] = None,
) -> IntakeWorkspace:
    workspace = IntakeWorkspace.objects.get(pk=workspace_pk)
    snapshot = build_workspace_profile_snapshot(workspace)

    if company_profile is not None:
        merged = {**snapshot['company_profile'], **company_profile}
        snapshot['company_profile'] = _normalize_company_profile(
            merged,
            fallback_company_name=workspace.name,
        )
    if pilot_scope is not None:
        snapshot['pilot_scope'] = _normalize_pilot_scope({**snapshot['pilot_scope'], **pilot_scope})
    if source_checklist is not None:
        snapshot['source_checklist'] = _normalize_source_checklist(
            {**snapshot['source_checklist'], **source_checklist}
        )
    if operator_notes is not None:
        snapshot['operator_notes'] = _normalize_string(operator_notes)

    workspace.metadata = _merge_workspace_profile_metadata(workspace.metadata or {}, snapshot)
    update_fields = ['metadata', 'updated_at']

    company_name = snapshot['company_profile'].get('company_name') or workspace.name
    if company_name and workspace.name != company_name:
        workspace.name = company_name
        update_fields.append('name')

    next_notes = workspace.notes
    if notes is not None:
        next_notes = _normalize_string(notes)
    elif operator_notes is not None and not workspace.notes:
        next_notes = snapshot['operator_notes']
    if workspace.notes != next_notes:
        workspace.notes = next_notes
        update_fields.append('notes')

    workspace.save(update_fields=update_fields)
    return workspace


async def update_workspace_profile(
    workspace: IntakeWorkspace,
    body: WorkspaceProfileUpdateRequest,
) -> IntakeWorkspace:
    company_profile = (
        body.company_profile.model_dump(exclude_none=True, exclude_unset=True)
        if body.company_profile else None
    )
    pilot_scope = (
        body.pilot_scope.model_dump(exclude_none=True, exclude_unset=True)
        if body.pilot_scope else None
    )
    source_checklist = (
        body.source_checklist.model_dump(exclude_none=True, exclude_unset=True)
        if body.source_checklist else None
    )
    return await sync_to_async(_apply_workspace_profile_sync)(
        workspace.pk,
        company_profile=company_profile,
        pilot_scope=pilot_scope,
        source_checklist=source_checklist,
        operator_notes=body.operator_notes,
        notes=body.notes,
    )


async def get_or_create_workspace(
    company_name: str,
    notes: str = '',
    company_profile: Optional[WorkspaceCompanyProfilePayload] = None,
    pilot_scope: Optional[WorkspacePilotScopePayload] = None,
    source_checklist: Optional[WorkspaceSourceChecklistPayload] = None,
    operator_notes: Optional[str] = None,
) -> IntakeWorkspace:
    profile_company_name = company_profile.company_name if company_profile else ''
    company_name = _normalize_string(profile_company_name or company_name)
    if not company_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Company name is required.',
        )

    slug = build_workspace_slug(company_name)

    def _get_or_create() -> IntakeWorkspace:
        workspace, _ = IntakeWorkspace.objects.get_or_create(
            slug=slug,
            defaults={
                'name': company_name,
                'notes': notes,
                'status': WorkspaceStatus.COLLECTING,
                'metadata': {},
            },
        )

        update_fields = []
        if workspace.name != company_name:
            workspace.name = company_name
            update_fields.append('name')
        if notes and workspace.notes != notes:
            workspace.notes = notes
            update_fields.append('notes')
        if workspace.status == WorkspaceStatus.DRAFT:
            workspace.status = WorkspaceStatus.COLLECTING
            update_fields.append('status')

        if update_fields:
            update_fields.append('updated_at')
            workspace.save(update_fields=update_fields)

        return workspace

    workspace = await sync_to_async(_get_or_create)()
    workspace = await sync_to_async(_apply_workspace_profile_sync)(
        workspace.pk,
        company_profile=(
            company_profile.model_dump(exclude_none=True, exclude_unset=True)
            if company_profile else {}
        ),
        pilot_scope=(
            pilot_scope.model_dump(exclude_none=True, exclude_unset=True)
            if pilot_scope else {}
        ),
        source_checklist=(
            source_checklist.model_dump(exclude_none=True, exclude_unset=True)
            if source_checklist else {}
        ),
        operator_notes=operator_notes if operator_notes is not None else notes,
        notes=notes,
    )
    await sync_to_async(_ensure_default_planning_context_sync)(workspace.pk)
    return workspace


async def get_workspace_by_slug(workspace_slug: str) -> Optional[IntakeWorkspace]:
    return await sync_to_async(IntakeWorkspace.objects.filter(slug=workspace_slug).first)()


def validate_workspace_source_payload(
    body: WorkspaceSourceCreateRequest,
    *,
    media_file=None,
) -> None:
    valid_source_kinds = {choice for choice, _ in WorkspaceSourceKind.choices}
    if body.source_kind not in valid_source_kinds:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Unknown source_kind. Expected one of: {", ".join(sorted(valid_source_kinds))}.',
        )

    valid_transports = {choice for choice, _ in WorkspaceSourceTransport.choices}
    if body.transport not in valid_transports:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Unknown transport. Expected one of: {", ".join(sorted(valid_transports))}.',
        )

    payload_presence = {
        WorkspaceSourceTransport.MEDIA_FILE: body.media_file_uuid is not None,
        WorkspaceSourceTransport.EXTERNAL_URL: bool(_normalize_string(body.external_url)),
        WorkspaceSourceTransport.INLINE_TEXT: bool(_normalize_string(body.inline_text)),
    }
    active_payloads = [transport for transport, present in payload_presence.items() if present]
    if len(active_payloads) != 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Exactly one transport payload must be provided: media_file_uuid, external_url, or inline_text.',
        )

    expected_transport = active_payloads[0]
    if body.transport != expected_transport:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'transport must match the provided payload: expected "{expected_transport}".',
        )

    if body.transport == WorkspaceSourceTransport.EXTERNAL_URL and body.external_url:
        _validate_external_url(body.external_url)

    if body.source_kind == WorkspaceSourceKind.ORG_CSV and body.transport != WorkspaceSourceTransport.MEDIA_FILE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='org_csv sources must use an uploaded spreadsheet file.',
        )

    if media_file is not None:
        allowed_categories = _MEDIA_CATEGORIES_BY_SOURCE_KIND.get(body.source_kind)
        if allowed_categories and media_file.file_category not in allowed_categories:
            allowed_labels = ', '.join(sorted(allowed_categories))
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f'Source kind "{body.source_kind}" requires a compatible file category. '
                    f'Allowed categories: {allowed_labels}.'
                ),
            )


async def create_workspace_source(
    workspace: IntakeWorkspace,
    body: WorkspaceSourceCreateRequest,
) -> WorkspaceSource:
    media_file = None
    if body.media_file_uuid is not None:
        from media_storage.models import MediaFile

        media_file = await MediaFile.objects.get_by_uuid(str(body.media_file_uuid))
        if media_file is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Media file not found.',
            )
        validate_workspace_source_payload(body, media_file=media_file)
        # Enforces workspace ownership (rejects unclaimed, cross-workspace, and org/discussion files)
        media_file = await sync_to_async(_claim_media_file_for_workspace_sync)(media_file.pk, workspace.pk)

        # Block duplicate attachment of the same file under the same source kind.
        duplicate_exists = await sync_to_async(
            WorkspaceSource.objects.filter(
                workspace=workspace,
                media_file=media_file,
                source_kind=body.source_kind,
            ).exclude(
                status=WorkspaceSourceStatus.ARCHIVED,
            ).exists
        )()
        if duplicate_exists:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='This file is already attached as this source kind.',
            )
    else:
        validate_workspace_source_payload(body, media_file=media_file)

    source = await sync_to_async(WorkspaceSource.objects.create)(
        workspace=workspace,
        title=_normalize_string(body.title),
        notes=_normalize_string(body.notes),
        source_kind=body.source_kind,
        transport=body.transport,
        media_file=media_file,
        external_url=_normalize_string(body.external_url),
        inline_text=_normalize_string(body.inline_text),
        language_code=_normalize_string(body.language_code),
        status=WorkspaceSourceStatus.ATTACHED,
    )
    await sync_to_async(_sync_source_default_context_link_sync)(source.pk)
    return await sync_to_async(
        WorkspaceSource.objects.select_related('workspace', 'media_file').get
    )(pk=source.pk)


async def update_workspace_source(
    source: WorkspaceSource,
    body: WorkspaceSourceUpdateRequest,
) -> WorkspaceSource:
    if source.status == WorkspaceSourceStatus.ARCHIVED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Archived sources cannot be updated.',
        )

    next_source_kind = body.source_kind or source.source_kind
    next_title = source.title if body.title is None else _normalize_string(body.title)
    next_notes = source.notes if body.notes is None else _normalize_string(body.notes)
    next_language_code = source.language_code if body.language_code is None else _normalize_string(body.language_code)
    next_external_url = source.external_url if body.external_url is None else _normalize_string(body.external_url)
    next_inline_text = source.inline_text if body.inline_text is None else _normalize_string(body.inline_text)

    validation_payload = WorkspaceSourceCreateRequest(
        source_kind=next_source_kind,
        transport=source.transport,
        media_file_uuid=source.media_file.uuid if source.media_file_id else None,
        external_url=next_external_url or None,
        inline_text=next_inline_text or None,
        title=next_title,
        notes=next_notes,
        language_code=next_language_code,
    )
    validate_workspace_source_payload(validation_payload, media_file=source.media_file)

    requires_reparse = any(
        [
            next_source_kind != source.source_kind,
            next_language_code != source.language_code,
            next_external_url != source.external_url,
            next_inline_text != source.inline_text,
        ]
    )

    source.title = next_title
    source.notes = next_notes
    source.source_kind = next_source_kind
    source.language_code = next_language_code
    if source.transport == WorkspaceSourceTransport.EXTERNAL_URL:
        source.external_url = next_external_url
    if source.transport == WorkspaceSourceTransport.INLINE_TEXT:
        source.inline_text = next_inline_text
    if requires_reparse:
        source.status = WorkspaceSourceStatus.ATTACHED
        source.parse_error = ''
        source.parse_metadata = {}
    await sync_to_async(source.save)(
        update_fields=[
            'title',
            'notes',
            'source_kind',
            'language_code',
            'external_url',
            'inline_text',
            'status',
            'parse_error',
            'parse_metadata',
            'updated_at',
        ]
    )
    await sync_to_async(_sync_source_default_context_link_sync)(source.pk)
    return await sync_to_async(
        WorkspaceSource.objects.select_related('workspace', 'media_file').get
    )(pk=source.pk)


async def archive_workspace_source(source: WorkspaceSource) -> WorkspaceSource:
    if source.status != WorkspaceSourceStatus.ARCHIVED:
        source.status = WorkspaceSourceStatus.ARCHIVED
        source.archived_at = django_timezone.now()
        source.parse_error = ''
        source.parse_metadata = {}
        await sync_to_async(source.save)(
            update_fields=['status', 'archived_at', 'parse_error', 'parse_metadata', 'updated_at']
        )
    return source


async def build_workspace_source_download_response(source: WorkspaceSource):
    if source.media_file_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='This source does not have an uploaded file.',
        )
    if source.media_file.organization_id is not None or source.media_file.discussion_id is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='This source file is not available through the prototype workspace flow.',
        )
    if source.media_file.prototype_workspace_id not in {None, source.workspace_id}:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='This source file does not belong to this workspace.',
        )

    from media_storage.constants import SIGNED_URL_EXPIRY_SECONDS
    from media_storage.entities import SignedUrlResponse
    from media_storage.services import generate_signed_url_for_file

    signed_url = await generate_signed_url_for_file(source.media_file)
    if not signed_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='The source file does not have a persistent object.',
        )
    return SignedUrlResponse(
        url=signed_url,
        expires_in_seconds=SIGNED_URL_EXPIRY_SECONDS,
        variant_type='original',
        file_uuid=source.media_file.uuid,
    )


def _build_company_profile_completeness(profile: dict[str, Any]) -> WorkspaceSectionCompletenessResponse:
    all_labels = {**_COMPANY_PROFILE_REQUIRED_FIELDS, **_COMPANY_PROFILE_RECOMMENDED_FIELDS}
    completed_fields = sum(1 for key in all_labels if _is_filled(profile.get(key)))
    total_fields = len(all_labels)
    missing_required = [
        label
        for key, label in _COMPANY_PROFILE_REQUIRED_FIELDS.items()
        if not _is_filled(profile.get(key))
    ]
    missing_recommended = [
        label
        for key, label in _COMPANY_PROFILE_RECOMMENDED_FIELDS.items()
        if not _is_filled(profile.get(key))
    ]
    return WorkspaceSectionCompletenessResponse(
        completed_fields=completed_fields,
        total_fields=total_fields,
        completion_ratio=round(completed_fields / total_fields, 2) if total_fields else 1.0,
        missing_required_fields=missing_required,
        missing_recommended_fields=missing_recommended,
        is_complete=not missing_required,
    )


def _build_pilot_scope_completeness(scope: dict[str, Any]) -> WorkspaceSectionCompletenessResponse:
    completed_fields = sum(1 for key in _PILOT_SCOPE_FIELD_LABELS if _is_filled(scope.get(key)))
    total_fields = len(_PILOT_SCOPE_FIELD_LABELS)

    missing_required: list[str] = []
    if not _is_filled(scope.get('stakeholder_contact')):
        missing_required.append(_PILOT_SCOPE_FIELD_LABELS['stakeholder_contact'])
    if not (
        _is_filled(scope.get('scope_mode'))
        or _is_filled(scope.get('departments_in_scope'))
        or _is_filled(scope.get('roles_in_scope'))
        or _is_filled(scope.get('products_in_scope'))
    ):
        missing_required.append('scope definition (scope mode, departments, roles, or products)')

    missing_recommended = [
        _PILOT_SCOPE_FIELD_LABELS[key]
        for key in ('employee_count_in_scope', 'analyst_notes')
        if not _is_filled(scope.get(key))
    ]
    return WorkspaceSectionCompletenessResponse(
        completed_fields=completed_fields,
        total_fields=total_fields,
        completion_ratio=round(completed_fields / total_fields, 2) if total_fields else 1.0,
        missing_required_fields=missing_required,
        missing_recommended_fields=missing_recommended,
        is_complete=not missing_required,
    )


def _evaluate_requirement_state(
    *,
    required: bool,
    attached_count: int,
    parsed_count: int,
    required_min_count: int = 1,
) -> tuple[bool, bool]:
    if required:
        return attached_count >= required_min_count, parsed_count >= required_min_count
    if attached_count == 0:
        return True, True
    return attached_count >= required_min_count, parsed_count >= required_min_count


def _build_source_kind_counts(
    sources: list[WorkspaceSource],
) -> tuple[Counter[str], Counter[str]]:
    return (
        Counter(source.source_kind for source in sources),
        Counter(source.source_kind for source in sources if source.status == WorkspaceSourceStatus.PARSED),
    )


def _evaluate_requirement_counts(
    requirement: dict[str, Any],
    *,
    attached_counts: Counter[str],
    parsed_counts: Counter[str],
) -> tuple[int, int, bool, bool]:
    attached_count = sum(attached_counts.get(kind, 0) for kind in requirement['source_kinds'])
    parsed_count = sum(parsed_counts.get(kind, 0) for kind in requirement['source_kinds'])
    is_satisfied, is_parsed_ready = _evaluate_requirement_state(
        required=requirement['required'],
        attached_count=attached_count,
        parsed_count=parsed_count,
        required_min_count=requirement.get('required_min_count', 1),
    )
    return attached_count, parsed_count, is_satisfied, is_parsed_ready


def _stage_requirement_gap_messages_for_counts(
    requirements: list[dict[str, Any]],
    *,
    flag_name: str,
    parsed: bool,
    attached_counts: Counter[str],
    parsed_counts: Counter[str],
) -> list[str]:
    blockers: list[str] = []
    for requirement in requirements:
        if not requirement.get(flag_name):
            continue
        _attached_count, _parsed_count, is_satisfied, is_parsed_ready = _evaluate_requirement_counts(
            requirement,
            attached_counts=attached_counts,
            parsed_counts=parsed_counts,
        )
        stage_ready = is_parsed_ready if parsed else is_satisfied
        if stage_ready:
            continue
        if parsed:
            blockers.append(f"{requirement['label']} must be parsed.")
        else:
            blockers.append(f"Missing required source group: {requirement['label']}.")
    return blockers


def _stage_requirement_gap_messages(
    requirements: list[WorkspaceSourceRequirementResponse],
    *,
    flag_name: str,
    parsed: bool,
) -> list[str]:
    blockers: list[str] = []
    for requirement in requirements:
        if not getattr(requirement, flag_name):
            continue
        stage_ready = requirement.is_parsed_ready if parsed else requirement.is_satisfied
        if stage_ready:
            continue
        if parsed:
            blockers.append(f'{requirement.label} must be parsed.')
        else:
            blockers.append(f'Missing required source group: {requirement.label}.')
    return blockers


def _resolve_current_stage(stage_statuses: dict[str, str]) -> str:
    for stage_key, _label in _WORKFLOW_STAGE_ORDER:
        status = stage_statuses.get(stage_key, 'not_started')
        if status in _WORKFLOW_PENDING_STATUSES:
            return stage_key
    return _WORKFLOW_STAGE_ORDER[-1][0]


def _planning_context_filter_kwargs(planning_context=None) -> dict[str, Any]:
    if planning_context is not None:
        return {'planning_context': planning_context}
    return {'planning_context__isnull': True}


def _list_effective_workspace_sources_sync(
    workspace_pk: int,
    planning_context_pk=None,
    *,
    include_archived: bool = False,
) -> list[WorkspaceSource]:
    workspace = IntakeWorkspace.objects.get(pk=workspace_pk)
    if planning_context_pk is None:
        return list(
            _workspace_source_queryset(workspace, include_archived=include_archived).order_by('-created_at')
        )

    from org_context.models import PlanningContext

    planning_context = PlanningContext.objects.get(pk=planning_context_pk, workspace_id=workspace_pk)
    effective_links = PlanningContext.resolve_effective_sources(planning_context)
    source_ids = [link.workspace_source_id for link in effective_links]
    if not source_ids:
        return []
    return list(
        _workspace_source_queryset(workspace, include_archived=include_archived)
        .filter(pk__in=source_ids)
        .order_by('-created_at')
    )


def _list_effective_planning_context_links_sync(
    workspace_pk: int,
    planning_context_pk,
):
    from org_context.models import PlanningContext

    planning_context = PlanningContext.objects.get(pk=planning_context_pk, workspace_id=workspace_pk)
    return list(PlanningContext.resolve_effective_sources(planning_context))


async def build_workspace_readiness_response(
    workspace: IntakeWorkspace,
    *,
    planning_context=None,
) -> WorkspaceReadinessResponse:
    detail = build_workspace_detail_response(workspace)
    sources = await sync_to_async(_list_effective_workspace_sources_sync)(
        workspace.pk,
        getattr(planning_context, 'pk', None),
    )
    attached_counts, parsed_counts = _build_source_kind_counts(sources)
    roadmap_scope_sources = list(sources)
    blueprint_scope_sources = list(sources)
    if planning_context is not None:
        effective_links = await sync_to_async(_list_effective_planning_context_links_sync)(
            workspace.pk,
            planning_context.pk,
        )
        roadmap_scope_sources = [
            link.workspace_source
            for link in effective_links
            if link.include_in_roadmap_analysis
        ]
        blueprint_scope_sources = [
            link.workspace_source
            for link in effective_links
            if link.include_in_blueprint
        ]
    roadmap_attached_counts, roadmap_parsed_counts = _build_source_kind_counts(roadmap_scope_sources)
    blueprint_attached_counts, blueprint_parsed_counts = _build_source_kind_counts(blueprint_scope_sources)

    company_completeness = _build_company_profile_completeness(detail.company_profile.model_dump())
    pilot_scope_completeness = _build_pilot_scope_completeness(detail.pilot_scope.model_dump())

    source_requirements: list[WorkspaceSourceRequirementResponse] = []
    for requirement in _SOURCE_REQUIREMENTS:
        attached_count, parsed_count, is_satisfied, is_parsed_ready = _evaluate_requirement_counts(
            requirement,
            attached_counts=attached_counts,
            parsed_counts=parsed_counts,
        )
        notes: list[str] = []

        if requirement['key'] == 'existing_matrix':
            availability = detail.source_checklist.existing_matrix_available
            if availability is False:
                notes.append('Marked as not available by the operator.')
            elif availability is True and attached_count == 0:
                notes.append('Expected by operator, but not attached yet.')

        source_requirements.append(
            WorkspaceSourceRequirementResponse(
                key=requirement['key'],
                label=requirement['label'],
                required=requirement['required'],
                required_for_parse=requirement.get('required_for_parse', False),
                required_for_roadmap_analysis=requirement.get('required_for_roadmap_analysis', False),
                required_for_blueprint=requirement.get('required_for_blueprint', False),
                required_for_evidence=requirement.get('required_for_evidence', False),
                source_kinds=requirement['source_kinds'],
                required_min_count=requirement.get('required_min_count', 1),
                attached_count=attached_count,
                parsed_count=parsed_count,
                is_satisfied=is_satisfied,
                is_parsed_ready=is_parsed_ready,
                notes=notes,
            )
        )

    parse_blockers: list[str] = []
    for field in company_completeness.missing_required_fields:
        parse_blockers.append(f'Missing company profile field: {field}.')
    for field in pilot_scope_completeness.missing_required_fields:
        parse_blockers.append(f'Missing pilot scope field: {field}.')
    parse_blockers.extend(
        _stage_requirement_gap_messages_for_counts(
            _SOURCE_REQUIREMENTS,
            flag_name='required_for_parse',
            parsed=False,
            attached_counts=attached_counts,
            parsed_counts=parsed_counts,
        )
    )

    ready_for_parse = (
        company_completeness.is_complete
        and pilot_scope_completeness.is_complete
        and all(item.is_satisfied for item in source_requirements if item.required_for_parse)
    )

    roadmap_analysis_blockers: list[str] = []
    if not ready_for_parse:
        roadmap_analysis_blockers.extend(parse_blockers)
    roadmap_analysis_blockers.extend(
        _stage_requirement_gap_messages_for_counts(
            _SOURCE_REQUIREMENTS,
            flag_name='required_for_roadmap_analysis',
            parsed=True,
            attached_counts=roadmap_attached_counts,
            parsed_counts=roadmap_parsed_counts,
        )
    )
    ready_for_roadmap_analysis = ready_for_parse and not roadmap_analysis_blockers

    blueprint_blockers = []
    if not ready_for_parse:
        blueprint_blockers.extend(parse_blockers)
    blueprint_blockers.extend(
        _stage_requirement_gap_messages_for_counts(
            _SOURCE_REQUIREMENTS,
            flag_name='required_for_blueprint',
            parsed=True,
            attached_counts=blueprint_attached_counts,
            parsed_counts=blueprint_parsed_counts,
        )
    )

    blueprint_review_ready = False
    blueprint_published = False
    blueprint_exists = False
    roadmap_analysis_completed = False
    employee_count = 0
    cv_evidence_completed = False
    try:
        from skill_blueprint.models import BLUEPRINT_REVIEW_READY_STATUSES, SkillBlueprintRun

        blueprint_exists = await sync_to_async(
            SkillBlueprintRun.objects.filter(
                workspace=workspace,
                **_planning_context_filter_kwargs(planning_context),
            ).exists
        )()
        blueprint_review_ready = await sync_to_async(
            SkillBlueprintRun.objects.filter(
                workspace=workspace,
                status__in=BLUEPRINT_REVIEW_READY_STATUSES,
                **_planning_context_filter_kwargs(planning_context),
            ).exists
        )()
        blueprint_published = await sync_to_async(
            SkillBlueprintRun.objects.filter(
                workspace=workspace,
                is_published=True,
                **_planning_context_filter_kwargs(planning_context),
            ).exists
        )()
    except Exception:
        blueprint_exists = False
        blueprint_review_ready = False
        blueprint_published = False

    try:
        from org_context.models import RoadmapAnalysisRun

        roadmap_analysis_completed = await sync_to_async(
            RoadmapAnalysisRun.objects.filter(
                workspace=workspace,
                status=RoadmapAnalysisRun.Status.COMPLETED,
                **_planning_context_filter_kwargs(planning_context),
            ).exists
        )()
    except Exception:
        roadmap_analysis_completed = False

    if not roadmap_analysis_completed:
        blueprint_blockers.append('Complete roadmap analysis before generating blueprint.')

    ready_for_blueprint = ready_for_parse and roadmap_analysis_completed and not blueprint_blockers

    try:
        from org_context.models import Employee

        employee_count = await sync_to_async(
            Employee.objects.filter(workspace=workspace).count
        )()
    except Exception:
        employee_count = 0

    try:
        from org_context.models import EmployeeSkillEvidence

        cv_evidence_completed = await sync_to_async(
            EmployeeSkillEvidence.objects.filter(
                workspace=workspace,
                source_kind='employee_cv',
                weight__gt=0,
            ).exists
        )()
    except Exception:
        cv_evidence_completed = False

    evidence_blockers: list[str] = []
    if not ready_for_blueprint and not blueprint_exists:
        evidence_blockers.extend(blueprint_blockers)
    if not blueprint_review_ready:
        evidence_blockers.append('CV evidence build requires a reviewed or approved blueprint before publication.')
    elif not blueprint_published:
        evidence_blockers.append('CV evidence build requires a published blueprint.')
    if employee_count == 0:
        evidence_blockers.append('CV evidence build requires employees imported from org context.')
    evidence_blockers.extend(
        [
            blocker.replace(' must be parsed.', ' must be attached and parsed for evidence build.')
            if blocker.endswith(' must be parsed.') else blocker
            for blocker in _stage_requirement_gap_messages(
                source_requirements,
                flag_name='required_for_evidence',
                parsed=True,
            )
        ]
    )

    ready_for_evidence = ready_for_blueprint and blueprint_published and employee_count > 0 and not any(
        requirement.required_for_evidence and not requirement.is_parsed_ready
        for requirement in source_requirements
    )

    assessment_blockers: list[str] = []
    assessment_target_employee_count = 0
    try:
        from employee_assessment.services import count_default_assessment_cycle_employees

        assessment_target_employee_count = await count_default_assessment_cycle_employees(
            workspace,
            planning_context=planning_context,
        )
    except Exception:
        logger.exception('Failed to resolve assessment target cohort for workspace %s', workspace.slug)
        assessment_target_employee_count = 0
    if not blueprint_review_ready:
        assessment_blockers.append('Assessment generation requires a reviewed or approved blueprint before publication.')
    elif not blueprint_published:
        assessment_blockers.append('Assessment generation requires a published blueprint.')
    if employee_count == 0:
        assessment_blockers.append('Assessment generation requires employees imported from org context.')
    if assessment_target_employee_count == 0:
        assessment_blockers.append('Assessment generation requires at least one matched employee in scope.')
    if not cv_evidence_completed:
        assessment_blockers.append('Assessments depend on completed CV evidence and role matching.')
    ready_for_assessments = (
        blueprint_published
        and cv_evidence_completed
        and assessment_target_employee_count > 0
        and not assessment_blockers
    )

    # Late-stage readiness flags
    matrix_blockers: list[str] = []
    plan_blockers: list[str] = []
    try:
        from employee_assessment.services import get_assessment_status as _get_assessment_status_sync
        from evidence_matrix.services import (
            PRIMARY_EVIDENCE_SOURCE_KINDS,
            get_current_completed_matrix_run as _get_completed_matrix,
        )
        from org_context.models import EmployeeSkillEvidence

        assessment_status_data = await _get_assessment_status_sync(workspace, planning_context=planning_context)
        submitted_or_completed = (
            int(assessment_status_data.get('submitted_packs', 0))
            + int(assessment_status_data.get('completed_packs', 0))
        )
        usable_evidence_count = await sync_to_async(
            EmployeeSkillEvidence.objects.filter(
                workspace=workspace,
                source_kind__in=sorted(PRIMARY_EVIDENCE_SOURCE_KINDS),
                weight__gt=0,
            ).count
        )()
        ready_for_matrix = blueprint_published and usable_evidence_count > 0 and submitted_or_completed > 0
        if not blueprint_published:
            matrix_blockers.append('A published blueprint is required before building the matrix.')
        if usable_evidence_count == 0:
            matrix_blockers.append('No usable evidence rows are available for matrix generation.')
        if submitted_or_completed == 0:
            matrix_blockers.append('No submitted or completed assessment packs are available yet.')

        from skill_blueprint.services import get_current_published_blueprint_run
        current_pub_blueprint = await get_current_published_blueprint_run(workspace, planning_context=planning_context)
        completed_matrix = await _get_completed_matrix(
            workspace,
            planning_context=planning_context,
            blueprint_run=current_pub_blueprint,
        ) if current_pub_blueprint else None
        ready_for_plans = completed_matrix is not None
        if completed_matrix is None:
            plan_blockers.append('A completed evidence matrix is required before generating development plans.')
    except Exception:
        logger.exception('Failed to compute late-stage readiness for workspace %s', workspace.slug)
        ready_for_matrix = False
        ready_for_plans = False
        matrix_blockers = ['Late-stage readiness could not be computed.']
        plan_blockers = ['Late-stage readiness could not be computed.']

    clarification_blockers: list[str] = []
    if not blueprint_exists:
        clarification_blockers.extend(blueprint_blockers)
    elif not blueprint_published:
        clarification_blockers.append('Blueprint review and publication must be completed before downstream evidence can begin.')

    stage_statuses = {
        'context': 'completed' if company_completeness.is_complete and pilot_scope_completeness.is_complete else 'blocked',
        'sources': 'completed' if all(item.is_satisfied for item in source_requirements if item.required_for_parse) else 'blocked',
        'parse': 'completed' if ready_for_parse else 'blocked',
        'roadmap_analysis': 'completed' if (
            roadmap_analysis_completed or (planning_context is None and blueprint_exists)
        ) else ('ready' if ready_for_roadmap_analysis else 'blocked'),
        'blueprint': 'completed' if blueprint_exists else ('ready' if ready_for_blueprint else 'blocked'),
        'clarifications': 'completed' if blueprint_published else ('ready' if blueprint_exists else 'blocked'),
        'evidence': 'completed' if cv_evidence_completed else ('ready' if ready_for_evidence else 'blocked'),
        'assessments': 'completed' if ready_for_matrix else ('ready' if ready_for_assessments else 'blocked'),
        'matrix': 'completed' if ready_for_plans else ('ready' if ready_for_matrix else 'blocked'),
        'plans': 'ready' if ready_for_plans else 'blocked',
    }
    current_stage = _resolve_current_stage(stage_statuses)
    if current_stage == 'plans' and ready_for_plans:
        current_stage = 'ready'

    stage_blockers = WorkspaceStageBlockersResponse(
        context=[
            *(f'Missing company profile field: {field}.' for field in company_completeness.missing_required_fields),
            *(f'Missing pilot scope field: {field}.' for field in pilot_scope_completeness.missing_required_fields),
        ],
        sources=_stage_requirement_gap_messages(
            source_requirements,
            flag_name='required_for_parse',
            parsed=False,
        ),
        parse=parse_blockers,
        roadmap_analysis=roadmap_analysis_blockers,
        blueprint=blueprint_blockers,
        clarifications=clarification_blockers,
        evidence=evidence_blockers,
        assessments=assessment_blockers,
        matrix=matrix_blockers,
        plans=plan_blockers,
    )
    blocking_items = getattr(stage_blockers, current_stage, []) if current_stage != 'ready' else []

    return WorkspaceReadinessResponse(
        workspace=detail,
        company_profile_completeness=company_completeness,
        pilot_scope_completeness=pilot_scope_completeness,
        source_requirements=source_requirements,
        source_counts=dict(attached_counts),
        parsed_source_counts=dict(parsed_counts),
        total_attached_sources=len(sources),
        total_parsed_sources=sum(1 for source in sources if source.status == WorkspaceSourceStatus.PARSED),
        current_stage=current_stage,
        blueprint_state=WorkspaceBlueprintStateResponse(
            review_ready=blueprint_review_ready,
            published=blueprint_published,
        ),
        stage_blockers=stage_blockers,
        blocking_items=blocking_items,
        readiness=WorkspaceReadinessFlagsResponse(
            ready_for_parse=ready_for_parse,
            ready_for_roadmap_analysis=ready_for_roadmap_analysis,
            ready_for_blueprint=ready_for_blueprint,
            ready_for_evidence=ready_for_evidence,
            ready_for_assessments=ready_for_assessments,
            ready_for_matrix=ready_for_matrix,
            ready_for_plans=ready_for_plans,
        ),
    )


async def assert_workspace_ready_for_stage(workspace: IntakeWorkspace, stage: str, *, planning_context=None) -> None:
    """Check readiness for a given stage and raise 409 if not ready."""
    readiness = await build_workspace_readiness_response(workspace, planning_context=planning_context)
    flag_map = {
        'parse': readiness.readiness.ready_for_parse,
        'roadmap_analysis': readiness.readiness.ready_for_roadmap_analysis,
        'blueprint': readiness.readiness.ready_for_blueprint,
        'evidence': readiness.readiness.ready_for_evidence,
        'assessments': readiness.readiness.ready_for_assessments,
        'matrix': readiness.readiness.ready_for_matrix,
        'plans': readiness.readiness.ready_for_plans,
    }
    if not flag_map.get(stage, False):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'Workspace is not ready for {stage}. Check readiness or workflow-status for blockers.',
        )


def _build_workflow_stage(
    *,
    key: str,
    label: str,
    status_value: str,
    dependencies: Optional[list[str]] = None,
    blockers: Optional[list[str]] = None,
    recommended_action: str = '',
    latest_run_uuid=None,
    metadata: Optional[dict[str, Any]] = None,
) -> WorkspaceWorkflowStageResponse:
    return WorkspaceWorkflowStageResponse(
        key=key,
        label=label,
        status=status_value,
        dependencies=dependencies or list(_STAGE_DEPENDENCIES.get(key, [])),
        blockers=blockers or [],
        recommended_action=recommended_action,
        latest_run_uuid=latest_run_uuid,
        metadata=metadata or {},
    )


def _resolve_workflow_summary(stages: list[WorkspaceWorkflowStageResponse]) -> tuple[str, str, int]:
    current_stage_key = ''
    next_stage_key = ''
    total_blocker_count = sum(
        len(stage.blockers)
        for stage in stages
        if stage.status in {'blocked', 'action_required', 'failed'}
    )
    for stage in stages:
        if stage.status in _WORKFLOW_PENDING_STATUSES:
            current_stage_key = stage.key
            break
    if not current_stage_key and stages:
        current_stage_key = stages[-1].key

    for stage in stages:
        if stage.status in {'ready', 'action_required', 'running', 'blocked', 'failed', 'not_started'}:
            next_stage_key = stage.key
            break

    return current_stage_key, next_stage_key, total_blocker_count


async def build_workspace_workflow_status_response(
    workspace: IntakeWorkspace,
    *,
    planning_context=None,
) -> WorkspaceWorkflowStatusResponse:
    from development_plans.models import PlanRunStatus
    from development_plans.services import get_current_team_plan, get_latest_team_plan
    from employee_assessment.models import AssessmentCycle, AssessmentStatus
    from employee_assessment.services import (
        count_default_assessment_cycle_employees,
        get_assessment_status,
        get_current_cycle,
    )
    from evidence_matrix.models import EvidenceMatrixStatus
    from evidence_matrix.services import PRIMARY_EVIDENCE_SOURCE_KINDS, get_current_completed_matrix_run, get_latest_matrix_run
    from org_context.models import Employee, EmployeeRoleMatch, EmployeeSkillEvidence, RoadmapAnalysisRun
    from skill_blueprint.models import BlueprintStatus
    from skill_blueprint.services import (
        get_active_clarification_run,
        get_current_published_blueprint_run,
        get_latest_blueprint_run,
        list_open_clarification_questions,
    )

    readiness = await build_workspace_readiness_response(workspace, planning_context=planning_context)
    detail = readiness.workspace
    sources = await sync_to_async(_list_effective_workspace_sources_sync)(
        workspace.pk,
        getattr(planning_context, 'pk', None),
    )
    latest_roadmap_analysis = await sync_to_async(
        lambda: RoadmapAnalysisRun.objects.filter(
            workspace=workspace,
            **_planning_context_filter_kwargs(planning_context),
        ).order_by('-created_at').first()
    )()
    latest_blueprint = await get_latest_blueprint_run(workspace, planning_context=planning_context)
    current_published_blueprint = await get_current_published_blueprint_run(workspace, planning_context=planning_context)
    active_clarification_run = await get_active_clarification_run(workspace, planning_context=planning_context)
    open_clarification_questions = (
        await list_open_clarification_questions(
            workspace,
            blueprint_run=active_clarification_run,
            planning_context=planning_context,
        )
        if active_clarification_run is not None
        else []
    )
    assessment_status = await get_assessment_status(workspace, planning_context=planning_context)
    current_cycle = await get_current_cycle(workspace, planning_context=planning_context)
    latest_assessment_attempt = await sync_to_async(
        lambda: AssessmentCycle.objects.filter(
            workspace=workspace,
            **_planning_context_filter_kwargs(planning_context),
        ).order_by('-updated_at').first()
    )()
    latest_matrix_run = await get_latest_matrix_run(
        workspace,
        blueprint_run=current_published_blueprint,
        planning_context=planning_context,
    ) if current_published_blueprint is not None else await get_latest_matrix_run(
        workspace,
        planning_context=planning_context,
    )
    current_matrix_run = await get_current_completed_matrix_run(
        workspace,
        blueprint_run=current_published_blueprint,
        planning_context=planning_context,
    ) if current_published_blueprint is not None else None
    latest_team_plan = await get_latest_team_plan(workspace, planning_context=planning_context)
    current_team_plan = await get_current_team_plan(workspace, planning_context=planning_context)
    aligned_current_team_plan = current_team_plan
    if (
        aligned_current_team_plan is not None
        and current_published_blueprint is not None
        and current_matrix_run is not None
        and (
            aligned_current_team_plan.status != PlanRunStatus.COMPLETED
            or aligned_current_team_plan.blueprint_run_id != current_published_blueprint.pk
            or aligned_current_team_plan.matrix_run_id != current_matrix_run.pk
        )
    ):
        aligned_current_team_plan = None
    assessment_target_employee_count = await count_default_assessment_cycle_employees(
        workspace,
        planning_context=planning_context,
    )

    employee_count = await sync_to_async(Employee.objects.filter(workspace=workspace).count)()
    cv_evidence_count = await sync_to_async(
        EmployeeSkillEvidence.objects.filter(
            workspace=workspace,
            source_kind='employee_cv',
        ).count
    )()
    role_match_count = 0
    if current_published_blueprint is not None:
        role_match_count = await sync_to_async(
            EmployeeRoleMatch.objects.filter(
                workspace=workspace,
                **_planning_context_filter_kwargs(planning_context),
                role_profile__blueprint_run=current_published_blueprint,
            ).count
        )()
    usable_evidence_count = await sync_to_async(
        EmployeeSkillEvidence.objects.filter(
            workspace=workspace,
            source_kind__in=sorted(PRIMARY_EVIDENCE_SOURCE_KINDS),
            weight__gt=0,
        ).count
    )()

    context_blockers = [
        *(f'Missing company profile field: {field}.' for field in readiness.company_profile_completeness.missing_required_fields),
        *(f'Missing pilot scope field: {field}.' for field in readiness.pilot_scope_completeness.missing_required_fields),
    ]
    context_stage = _build_workflow_stage(
        key='context',
        label='Workspace context',
        status_value='completed' if not context_blockers else 'action_required',
        blockers=context_blockers,
        recommended_action=(
            'Complete the company profile and pilot scope.'
            if context_blockers else 'Workspace context is complete.'
        ),
        metadata={
            'company_profile_completion_ratio': readiness.company_profile_completeness.completion_ratio,
            'pilot_scope_completion_ratio': readiness.pilot_scope_completeness.completion_ratio,
        },
    )

    required_source_blockers = _stage_requirement_gap_messages(
        readiness.source_requirements,
        flag_name='required_for_parse',
        parsed=False,
    )
    source_status = 'completed'
    source_action = 'Required source groups are attached.'
    if context_blockers:
        source_status = 'blocked'
        source_action = 'Finish workspace context before treating source collection as complete.'
    elif required_source_blockers:
        source_status = 'action_required'
        source_action = 'Attach the missing required source groups.'
    elif not sources:
        source_status = 'not_started'
        source_action = 'Attach the first source files for this workspace.'
    sources_stage = _build_workflow_stage(
        key='sources',
        label='Source collection',
        status_value=source_status,
        blockers=required_source_blockers if source_status != 'blocked' else context_blockers,
        recommended_action=source_action,
        metadata={
            'total_attached_sources': readiness.total_attached_sources,
            'required_source_groups': len([item for item in readiness.source_requirements if item.required_for_parse]),
        },
    )

    parse_failed_items = [
        f'{source.title or source.source_kind}: {source.parse_error or "Parsing failed."}'
        for source in sources
        if source.status == WorkspaceSourceStatus.FAILED
    ]
    parse_running = any(source.status == WorkspaceSourceStatus.PARSING for source in sources)
    parse_stage_blockers = list(readiness.stage_blockers.parse)
    if parse_failed_items:
        parse_stage_blockers = parse_failed_items
    parse_status = 'completed'
    parse_action = 'Required sources are parsed.'
    if context_blockers or required_source_blockers:
        parse_status = 'blocked'
        parse_action = 'Finish context and attach required sources before parsing.'
    elif parse_running:
        parse_status = 'running'
        parse_action = 'Wait for parsing to finish.'
    elif parse_failed_items:
        parse_status = 'failed'
        parse_action = 'Fix failed sources and re-run parsing.'
    parse_stage = _build_workflow_stage(
        key='parse',
        label='Parsing and normalization',
        status_value=parse_status,
        blockers=parse_stage_blockers,
        recommended_action=parse_action,
        metadata={
            'total_parsed_sources': readiness.total_parsed_sources,
            'parsed_source_counts': readiness.parsed_source_counts,
        },
    )

    roadmap_stage_blockers = list(readiness.stage_blockers.roadmap_analysis)
    roadmap_status_value = 'ready'
    roadmap_action = 'Run roadmap analysis to structure initiatives and workstreams.'
    if parse_status in {'blocked', 'running', 'failed'}:
        roadmap_status_value = 'blocked'
        roadmap_action = 'Finish parsing successfully before roadmap analysis.'
    elif roadmap_stage_blockers:
        roadmap_status_value = 'blocked'
        roadmap_action = 'Parse roadmap and strategy sources before roadmap analysis.'
    elif latest_roadmap_analysis is not None and latest_roadmap_analysis.status == RoadmapAnalysisRun.Status.RUNNING:
        roadmap_status_value = 'running'
        roadmap_action = 'Wait for the current roadmap-analysis run to finish.'
    elif latest_roadmap_analysis is not None and latest_roadmap_analysis.status == RoadmapAnalysisRun.Status.FAILED:
        roadmap_status_value = 'failed'
        roadmap_action = 'Inspect the failed roadmap-analysis run and retry.'
        roadmap_stage_blockers = [latest_roadmap_analysis.error_message or 'Latest roadmap-analysis run failed.']
    elif latest_roadmap_analysis is not None and latest_roadmap_analysis.status == RoadmapAnalysisRun.Status.COMPLETED:
        roadmap_status_value = 'completed'
        roadmap_action = 'A completed roadmap analysis is available for blueprint generation.'
    elif latest_blueprint is not None and planning_context is None:
        roadmap_status_value = 'completed'
        roadmap_action = 'A legacy blueprint exists, so roadmap analysis is treated as satisfied for this workspace lineage.'
    roadmap_analysis_stage = _build_workflow_stage(
        key='roadmap_analysis',
        label='Roadmap analysis',
        status_value=roadmap_status_value,
        blockers=roadmap_stage_blockers if roadmap_status_value in {'blocked', 'failed'} else [],
        recommended_action=roadmap_action,
        latest_run_uuid=getattr(latest_roadmap_analysis, 'uuid', None),
        metadata={
            'ready_for_roadmap_analysis': readiness.readiness.ready_for_roadmap_analysis,
            'legacy_blueprint_backfilled': (
                planning_context is None
                and latest_roadmap_analysis is None
                and latest_blueprint is not None
            ),
            'initiative_count': len((latest_roadmap_analysis.initiatives if latest_roadmap_analysis is not None else []) or []),
            'workstream_count': len((latest_roadmap_analysis.workstreams if latest_roadmap_analysis is not None else []) or []),
        },
    )

    blueprint_blockers = list(readiness.stage_blockers.blueprint)
    blueprint_status_value = 'completed'
    blueprint_action = 'A blueprint run exists for this workspace.'
    if latest_blueprint is None and not readiness.readiness.ready_for_blueprint:
        blueprint_status_value = 'blocked'
        blueprint_action = 'Complete roadmap analysis and parse the required sources before generating a blueprint.'
    elif latest_blueprint is None:
        blueprint_status_value = 'ready'
        blueprint_action = 'Generate the first blueprint run.'
    elif latest_blueprint.status == BlueprintStatus.RUNNING:
        blueprint_status_value = 'running'
        blueprint_action = 'Wait for the blueprint generation run to finish.'
    elif latest_blueprint.status == BlueprintStatus.FAILED:
        blueprint_status_value = 'failed'
        blueprint_action = 'Inspect the failed blueprint run and retry.'
    elif current_published_blueprint is not None:
        blueprint_status_value = 'completed'
        blueprint_action = (
            'A published blueprint is available for downstream stages.'
            if latest_blueprint.uuid == current_published_blueprint.uuid
            else 'A published blueprint is currently effective downstream while a newer working run remains under review.'
        )
    elif latest_blueprint.status == BlueprintStatus.DRAFT:
        blueprint_status_value = 'action_required'
        blueprint_action = 'Review the generated blueprint run and promote it when you are ready.'
    elif latest_blueprint.status == BlueprintStatus.NEEDS_CLARIFICATION:
        blueprint_status_value = 'action_required'
        blueprint_action = 'Resolve open clarifications, refresh the run if needed, then review it again.'
    elif latest_blueprint.status == BlueprintStatus.REVIEWED:
        blueprint_status_value = 'action_required'
        blueprint_action = 'Approve the reviewed blueprint run when you are ready.'
    elif latest_blueprint.status == BlueprintStatus.APPROVED:
        blueprint_status_value = 'action_required'
        blueprint_action = 'Publish the approved blueprint run to make it effective downstream.'
    blueprint_stage = _build_workflow_stage(
        key='blueprint',
        label='Blueprint generation',
        status_value=blueprint_status_value,
        blockers=blueprint_blockers if blueprint_status_value in {'blocked', 'action_required'} and latest_blueprint is None else [],
        recommended_action=blueprint_action,
        latest_run_uuid=getattr(latest_blueprint, 'uuid', None),
        metadata={
            'latest_blueprint_status': getattr(latest_blueprint, 'status', ''),
            'published': readiness.blueprint_state.published,
        },
    )

    clarification_blockers: list[str] = []
    clarification_status_value = 'completed' if current_published_blueprint is not None else 'action_required'
    clarification_action = 'The current blueprint is published.'
    if latest_blueprint is None:
        clarification_status_value = 'blocked'
        clarification_blockers = ['Generate a blueprint before starting clarifications and publication.']
        clarification_action = 'Generate a blueprint run first.'
    elif current_published_blueprint is not None:
        clarification_status_value = 'completed'
        clarification_action = 'The current published blueprint is ready for downstream stages.'
    elif open_clarification_questions:
        clarification_status_value = 'action_required'
        clarification_blockers = [
            f'{len(open_clarification_questions)} clarification questions are still open.'
        ]
        clarification_action = 'Answer and resolve open clarification questions.'
    elif readiness.blueprint_state.review_ready and not readiness.blueprint_state.published:
        clarification_status_value = 'action_required'
        clarification_blockers = ['Blueprint is review-ready but not published yet.']
        clarification_action = 'Review, approve, and publish the blueprint.'
    elif latest_blueprint.status == BlueprintStatus.FAILED:
        clarification_status_value = 'blocked'
        clarification_blockers = ['Latest blueprint run failed before publication.']
        clarification_action = 'Recover the blueprint run before moving forward.'
    clarifications_stage = _build_workflow_stage(
        key='clarifications',
        label='Clarifications and publication',
        status_value=clarification_status_value,
        blockers=clarification_blockers,
        recommended_action=clarification_action,
        latest_run_uuid=getattr(current_published_blueprint or latest_blueprint, 'uuid', None),
        metadata={
            'open_clarification_count': len(open_clarification_questions),
            'review_ready': readiness.blueprint_state.review_ready,
            'published': readiness.blueprint_state.published,
        },
    )

    evidence_blockers = list(readiness.stage_blockers.evidence)
    evidence_status_value = 'completed' if cv_evidence_count > 0 else 'ready'
    evidence_action = 'CV evidence exists for the current workspace.'
    if cv_evidence_count > 0:
        evidence_status_value = 'completed'
        evidence_action = 'CV evidence and role matches exist for the current workspace.'
    elif not readiness.readiness.ready_for_evidence:
        evidence_status_value = 'blocked'
        evidence_action = 'Publish the blueprint, import employees, and attach parsed CVs first.'
    else:
        evidence_status_value = 'ready'
        evidence_action = 'Build CV evidence for the employees in scope.'
    evidence_stage = _build_workflow_stage(
        key='evidence',
        label='CV evidence and role matching',
        status_value=evidence_status_value,
        blockers=evidence_blockers if evidence_status_value == 'blocked' else [],
        recommended_action=evidence_action,
        latest_run_uuid=getattr(current_published_blueprint, 'uuid', None),
        metadata={
            'employee_count': employee_count,
            'cv_evidence_count': cv_evidence_count,
            'role_match_count': role_match_count,
        },
    )

    assessment_reference = latest_assessment_attempt or current_cycle
    assessment_blockers = list(readiness.stage_blockers.assessments)
    assessment_status_value = 'completed'
    assessment_action = 'Assessment cycle is complete.'
    if not readiness.readiness.ready_for_assessments:
        assessment_status_value = 'blocked'
        assessment_action = 'Complete evidence generation before generating assessments.'
    elif assessment_reference is None:
        assessment_status_value = 'ready'
        assessment_action = 'Generate the first assessment cycle.'
    elif assessment_reference.status == AssessmentStatus.FAILED:
        assessment_status_value = 'failed'
        assessment_action = 'Recover the failed assessment cycle or regenerate it.'
        assessment_blockers = ['The latest assessment cycle failed and needs regeneration.']
    elif assessment_reference.status == AssessmentStatus.COMPLETED:
        assessment_status_value = 'completed'
        assessment_action = 'Assessment cycle is complete.'
    elif assessment_reference.status == AssessmentStatus.RUNNING:
        assessment_status_value = 'running'
        assessment_action = 'Track completion and wait for employees to submit.'
    else:
        assessment_status_value = 'action_required'
        assessment_action = 'Distribute the generated packs and begin tracking completion.'
    assessments_stage = _build_workflow_stage(
        key='assessments',
        label='Assessments',
        status_value=assessment_status_value,
        blockers=assessment_blockers if assessment_status_value in {'blocked', 'failed'} else [],
        recommended_action=assessment_action,
        latest_run_uuid=getattr(assessment_reference, 'uuid', None),
        metadata={
            'completion_rate': assessment_status.get('completion_rate', 0.0),
            'total_packs': assessment_status.get('total_packs', 0),
            'submitted_packs': assessment_status.get('submitted_packs', 0),
            'completed_packs': assessment_status.get('completed_packs', 0),
            'target_employee_count': assessment_target_employee_count,
        },
    )

    matrix_blockers: list[str] = []
    submitted_or_completed_packs = int(assessment_status.get('submitted_packs', 0)) + int(
        assessment_status.get('completed_packs', 0)
    )
    matrix_status_value = 'completed'
    matrix_action = 'A completed matrix exists for the current published blueprint.'
    matrix_reference = current_matrix_run or latest_matrix_run
    if current_published_blueprint is None:
        matrix_status_value = 'blocked'
        matrix_blockers = ['A published blueprint is required before building the matrix.']
        matrix_action = 'Publish the blueprint first.'
    elif usable_evidence_count == 0:
        matrix_status_value = 'blocked'
        matrix_blockers = ['No usable evidence rows are available for matrix generation.']
        matrix_action = 'Build CV evidence and collect assessment responses first.'
    elif submitted_or_completed_packs == 0:
        matrix_status_value = 'blocked'
        matrix_blockers = ['No completed assessment responses are available for the matrix yet.']
        matrix_action = 'Collect at least one submitted assessment response.'
    elif latest_matrix_run is not None and latest_matrix_run.status == EvidenceMatrixStatus.RUNNING:
        matrix_status_value = 'running'
        matrix_action = 'Wait for the current matrix run to finish.'
        matrix_reference = latest_matrix_run
    elif latest_matrix_run is not None and latest_matrix_run.status == EvidenceMatrixStatus.FAILED:
        matrix_status_value = 'failed'
        matrix_action = 'Inspect the failed matrix run and retry with the intended cycle.'
        matrix_reference = latest_matrix_run
    elif current_matrix_run is None:
        matrix_status_value = 'ready'
        matrix_action = (
            'Build the evidence matrix for the current assessment cycle.'
            if latest_matrix_run is not None
            else 'Build the evidence matrix.'
        )
    matrix_stage = _build_workflow_stage(
        key='matrix',
        label='Evidence matrix',
        status_value=matrix_status_value,
        blockers=matrix_blockers,
        recommended_action=matrix_action,
        latest_run_uuid=getattr(matrix_reference, 'uuid', None),
        metadata={
            'usable_evidence_count': usable_evidence_count,
            'submitted_or_completed_packs': submitted_or_completed_packs,
            'latest_matrix_status': getattr(latest_matrix_run, 'status', ''),
        },
    )

    plan_blockers: list[str] = []
    plan_status_value = 'completed'
    plan_action = 'Latest team development plan is complete.'
    plan_reference = aligned_current_team_plan or latest_team_plan
    if current_matrix_run is None:
        plan_status_value = 'blocked'
        plan_blockers = ['A completed evidence matrix is required before generating development plans.']
        plan_action = 'Build the matrix first.'
    elif aligned_current_team_plan is None:
        plan_status_value = 'ready'
        plan_action = (
            'Generate a fresh team and individual plan batch for the current matrix.'
            if latest_team_plan is not None
            else 'Generate the first team and individual plans.'
        )
    elif aligned_current_team_plan.status == PlanRunStatus.RUNNING:
        plan_status_value = 'running'
        plan_action = 'Wait for the development plan batch to finish.'
    elif aligned_current_team_plan.status == PlanRunStatus.FAILED:
        plan_status_value = 'failed'
        plan_action = 'Inspect the failed plan batch and retry.'
    plans_stage = _build_workflow_stage(
        key='plans',
        label='Development plans',
        status_value=plan_status_value,
        blockers=plan_blockers,
        recommended_action=plan_action,
        latest_run_uuid=getattr(plan_reference, 'uuid', None),
        metadata={
            'latest_team_plan_status': getattr(latest_team_plan, 'status', ''),
            'current_team_plan_status': getattr(aligned_current_team_plan, 'status', ''),
            'is_current': bool(getattr(aligned_current_team_plan, 'is_current', False)),
        },
    )

    stages = [
        context_stage,
        sources_stage,
        parse_stage,
        roadmap_analysis_stage,
        blueprint_stage,
        clarifications_stage,
        evidence_stage,
        assessments_stage,
        matrix_stage,
        plans_stage,
    ]
    current_stage_key, next_stage_key, total_blocker_count = _resolve_workflow_summary(stages)

    return WorkspaceWorkflowStatusResponse(
        workspace=detail,
        stages=stages,
        summary=WorkspaceWorkflowSummaryResponse(
            current_stage_key=current_stage_key,
            next_stage_key=next_stage_key,
            total_blocker_count=total_blocker_count,
            latest_blueprint_status=getattr(latest_blueprint, 'status', ''),
            blueprint_published=bool(current_published_blueprint is not None),
            latest_assessment_status=getattr(assessment_reference, 'status', ''),
            assessment_completion_rate=float(assessment_status.get('completion_rate', 0.0) or 0.0),
            latest_matrix_status=getattr(matrix_reference, 'status', ''),
            latest_plan_status=getattr(latest_team_plan, 'status', ''),
            latest_blueprint_run_uuid=getattr(latest_blueprint, 'uuid', None),
            current_published_blueprint_run_uuid=getattr(current_published_blueprint, 'uuid', None),
            latest_assessment_cycle_uuid=getattr(assessment_reference, 'uuid', None),
            latest_matrix_run_uuid=getattr(matrix_reference, 'uuid', None),
            latest_team_plan_uuid=getattr(latest_team_plan, 'uuid', None),
        ),
    )


async def store_company_document(
    *,
    company_name: str,
    notes: str,
    file: UploadFile,
) -> tuple[IntakeWorkspace, SourceDocument]:
    workspace = await get_or_create_workspace(company_name=company_name, notes=notes)

    filename = file.filename or 'upload'
    guessed_ct = mimetypes.guess_type(filename)[0] or ''
    content_type = (file.content_type or guessed_ct or 'application/octet-stream').lower()
    content = await file.read()
    document_kind = validate_upload(file, content)
    if content_type not in _ALLOWED_CONTENT_TYPES:
        content_type = 'application/pdf' if document_kind == SourceDocumentKind.PDF else 'text/csv'

    document_uuid = uuid4()
    persistent_key = build_persistent_key(workspace.slug, str(document_uuid), filename)
    processing_key = build_processing_key(workspace.slug, str(document_uuid), filename)

    persistent_ok = False
    processing_ok = False

    async def _write_persistent() -> None:
        nonlocal persistent_ok
        await persistent_client().upload_bytes(
            key=persistent_key,
            data=content,
            content_type=content_type,
            metadata={'workspace_slug': workspace.slug, 'document_kind': document_kind},
        )
        persistent_ok = True

    async def _write_processing() -> None:
        nonlocal processing_ok
        await processing_client().upload_bytes(
            key=processing_key,
            data=content,
            content_type=content_type,
            metadata={'workspace_slug': workspace.slug, 'document_kind': document_kind},
        )
        processing_ok = True

    try:
        await asyncio.gather(_write_persistent(), _write_processing())
    except Exception as exc:
        if persistent_ok:
            await _cleanup_object(persistent_key, 'persistent', processing=False)
        if processing_ok:
            await _cleanup_object(processing_key, 'processing', processing=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to store the document in both storage layers.',
        ) from exc

    try:
        document = await sync_to_async(SourceDocument.objects.create)(
            uuid=document_uuid,
            workspace=workspace,
            original_filename=filename,
            content_type=content_type,
            file_size=len(content),
            document_kind=document_kind,
            status=SourceDocumentStatus.UPLOADED,
            persistent_key=persistent_key,
            processing_key=processing_key,
            storage_metadata={
                'persistent_bucket': persistent_client().bucket,
                'processing_bucket': processing_client().bucket,
            },
        )
    except Exception as exc:
        await _cleanup_object(persistent_key, 'persistent', processing=False)
        await _cleanup_object(processing_key, 'processing', processing=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Document uploaded, but metadata could not be saved.',
        ) from exc

    return workspace, document
