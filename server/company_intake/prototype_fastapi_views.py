import secrets as _secrets_mod
from typing import List, Optional
from uuid import UUID

from asgiref.sync import sync_to_async
from fastapi import APIRouter, Header, HTTPException, Path, Query, Response, status

from media_storage.entities import SignedUrlResponse
from org_context.models import PlanningContext
from org_context.services import parse_workspace_source

from .entities import (
    IntakeWorkspaceDetailResponse,
    IntakeWorkspaceResponse,
    ParseSourcesRequest,
    ParseSourcesResponse,
    ParsedSourceResult,
    WorkspaceCreateRequest,
    WorkspaceProfileUpdateRequest,
    WorkspaceReadinessResponse,
    WorkspaceSourceCreateRequest,
    WorkspaceSourceListResponse,
    WorkspaceSourceResponse,
    WorkspaceSourceUpdateRequest,
    WorkspaceWorkflowStatusResponse,
)
from .models import IntakeWorkspace, WorkspaceSource, WorkspaceSourceStatus
from .services import (
    archive_workspace_source,
    build_workspace_source_download_response,
    build_workspace_detail_response,
    build_workspace_readiness_response,
    build_workspace_response,
    build_workspace_source_response,
    build_workspace_workflow_status_response,
    create_workspace_source,
    get_workspace_source,
    get_or_create_workspace,
    list_workspace_sources,
    update_workspace_source,
    update_workspace_profile,
)

prototype_workspace_router = APIRouter(prefix='/prototype', tags=['prototype-workspaces'])


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


async def _require_operator(
    workspace_slug: str,
    x_operator_token: Optional[str] = Header(None),
) -> IntakeWorkspace:
    """Resolve workspace and verify operator token.

    The token is passed via the ``X-Operator-Token`` header.
    """
    workspace = await _get_workspace_or_404(workspace_slug)
    if not x_operator_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Missing X-Operator-Token header.',
        )
    if not _secrets_mod.compare_digest(workspace.operator_token, x_operator_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Invalid operator token.',
        )
    return workspace


async def _get_sources(workspace: IntakeWorkspace) -> List[WorkspaceSource]:
    return await list_workspace_sources(workspace)


async def _get_source_or_404(
    workspace: IntakeWorkspace,
    source_uuid,
    *,
    include_archived: bool = False,
) -> WorkspaceSource:
    source = await get_workspace_source(workspace, source_uuid, include_archived=include_archived)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Workspace source not found.',
        )
    return source


async def _get_planning_context_or_404(
    workspace: IntakeWorkspace,
    planning_context_uuid: UUID | None,
) -> PlanningContext | None:
    if planning_context_uuid is None:
        return None
    planning_context = await sync_to_async(
        lambda: PlanningContext.objects.filter(workspace=workspace, uuid=planning_context_uuid).first()
    )()
    if planning_context is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Planning context not found in this workspace.',
        )
    return planning_context


@prototype_workspace_router.post(
    '/workspaces',
    response_model=IntakeWorkspaceDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_workspace(body: WorkspaceCreateRequest):
    workspace = await get_or_create_workspace(
        company_name=body.company_name,
        notes=body.notes,
        company_profile=body.company_profile,
        pilot_scope=body.pilot_scope,
        source_checklist=body.source_checklist,
        operator_notes=body.operator_notes,
    )
    return build_workspace_detail_response(workspace)


@prototype_workspace_router.get('/workspaces', response_model=List[IntakeWorkspaceResponse])
async def list_workspaces():
    workspaces = await sync_to_async(list)(
        IntakeWorkspace.objects.order_by('-updated_at')
    )
    return [build_workspace_response(workspace) for workspace in workspaces]


@prototype_workspace_router.get(
    '/workspaces/{workspace_slug}',
    response_model=IntakeWorkspaceDetailResponse,
)
async def get_workspace(workspace_slug: str):
    workspace = await _get_workspace_or_404(workspace_slug)
    return build_workspace_detail_response(workspace)


@prototype_workspace_router.patch(
    '/workspaces/{workspace_slug}/profile',
    response_model=IntakeWorkspaceDetailResponse,
)
async def patch_workspace_profile(
    workspace_slug: str,
    body: WorkspaceProfileUpdateRequest,
    x_operator_token: Optional[str] = Header(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    workspace = await update_workspace_profile(workspace, body)
    return build_workspace_detail_response(workspace)


@prototype_workspace_router.get(
    '/workspaces/{workspace_slug}/readiness',
    response_model=WorkspaceReadinessResponse,
)
async def get_workspace_readiness(
    workspace_slug: str,
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _get_workspace_or_404(workspace_slug)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    return await build_workspace_readiness_response(workspace, planning_context=planning_context)


@prototype_workspace_router.get(
    '/workspaces/{workspace_slug}/workflow-status',
    response_model=WorkspaceWorkflowStatusResponse,
)
async def get_workspace_workflow_status(
    workspace_slug: str,
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _get_workspace_or_404(workspace_slug)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    return await build_workspace_workflow_status_response(workspace, planning_context=planning_context)


@prototype_workspace_router.post(
    '/workspaces/{workspace_slug}/sources',
    response_model=WorkspaceSourceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def attach_workspace_source(
    workspace_slug: str = Path(...),
    body: WorkspaceSourceCreateRequest = ...,
    x_operator_token: Optional[str] = Header(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    source = await create_workspace_source(workspace, body)
    return build_workspace_source_response(source)


@prototype_workspace_router.get(
    '/workspaces/{workspace_slug}/sources',
    response_model=WorkspaceSourceListResponse,
)
async def list_workspace_sources_view(
    workspace_slug: str,
    include_archived: bool = Query(False),
):
    workspace = await _get_workspace_or_404(workspace_slug)
    sources = await list_workspace_sources(workspace, include_archived=include_archived)
    return WorkspaceSourceListResponse(
        workspace=build_workspace_response(workspace),
        sources=[build_workspace_source_response(source) for source in sources],
    )


@prototype_workspace_router.get(
    '/workspaces/{workspace_slug}/sources/{source_uuid}',
    response_model=WorkspaceSourceResponse,
)
async def get_workspace_source_view(workspace_slug: str, source_uuid: str):
    workspace = await _get_workspace_or_404(workspace_slug)
    source = await _get_source_or_404(workspace, source_uuid)
    return build_workspace_source_response(source)


@prototype_workspace_router.patch(
    '/workspaces/{workspace_slug}/sources/{source_uuid}',
    response_model=WorkspaceSourceResponse,
)
async def patch_workspace_source_view(
    workspace_slug: str,
    source_uuid: str,
    body: WorkspaceSourceUpdateRequest,
    x_operator_token: Optional[str] = Header(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    source = await _get_source_or_404(workspace, source_uuid)
    source = await update_workspace_source(source, body)
    return build_workspace_source_response(source)


@prototype_workspace_router.delete(
    '/workspaces/{workspace_slug}/sources/{source_uuid}',
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_workspace_source_view(
    workspace_slug: str,
    source_uuid: str,
    x_operator_token: Optional[str] = Header(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    source = await _get_source_or_404(workspace, source_uuid)
    await archive_workspace_source(source)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@prototype_workspace_router.get(
    '/workspaces/{workspace_slug}/sources/{source_uuid}/download',
    response_model=SignedUrlResponse,
)
async def download_workspace_source_view(workspace_slug: str, source_uuid: str):
    workspace = await _get_workspace_or_404(workspace_slug)
    source = await _get_source_or_404(workspace, source_uuid)
    return await build_workspace_source_download_response(source)


@prototype_workspace_router.post(
    '/workspaces/{workspace_slug}/parse',
    response_model=ParseSourcesResponse,
)
async def parse_sources(
    workspace_slug: str,
    body: ParseSourcesRequest,
    x_operator_token: Optional[str] = Header(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)

    sources_qs = WorkspaceSource.objects.select_related('workspace', 'media_file').filter(
        workspace=workspace
    ).exclude(status=WorkspaceSourceStatus.ARCHIVED)
    if body.source_uuids:
        sources_qs = sources_qs.filter(uuid__in=list(body.source_uuids))

    sources = await sync_to_async(list)(sources_qs.order_by('created_at'))
    if not sources:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No matching sources found for parsing.',
        )

    results: List[ParsedSourceResult] = []
    processed = 0
    for source in sources:
        result = await parse_workspace_source(source, force=body.force)
        processed += 1
        results.append(
            ParsedSourceResult(
                source_uuid=source.uuid,
                source_kind=source.source_kind,
                status=result['status'],
                parse_error=result.get('parse_error', ''),
                parse_metadata=result.get('parse_metadata', {}) or {},
            )
        )

    workspace = await _get_workspace_or_404(workspace_slug)
    return ParseSourcesResponse(
        workspace=build_workspace_response(workspace),
        processed=processed,
        results=results,
    )
