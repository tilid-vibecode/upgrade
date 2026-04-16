import secrets as _secrets_mod
from typing import Optional
from uuid import UUID

from asgiref.sync import sync_to_async
from fastapi import APIRouter, Header, HTTPException, Query, status

from company_intake.models import IntakeWorkspace
from org_context.models import PlanningContext

from .entities import (
    DevelopmentPlanArtifactBundleResponse,
    DevelopmentPlanArtifactListResponse,
    DevelopmentPlanBatchResponse,
    DevelopmentPlanGenerateRequest,
    DevelopmentPlanRunResponse,
    DevelopmentPlanSliceResponse,
    DevelopmentPlanSummaryResponse,
)
from .services import (
    build_workspace_artifact_list_response,
    build_plan_slice_response,
    build_plan_response,
    generate_development_plans,
    get_current_individual_plan_artifact_bundle,
    get_current_individual_plan,
    get_current_plan_summary,
    get_current_team_plan_artifact_bundle,
    get_current_team_actions,
    get_current_team_plan,
    get_latest_individual_plan_artifact_bundle,
    get_latest_individual_plan,
    get_latest_plan_summary,
    get_latest_team_plan_artifact_bundle,
    get_latest_team_actions,
    get_latest_team_plan,
    list_latest_workspace_plan_artifacts,
    list_current_individual_plans,
    list_latest_individual_plans,
    list_workspace_plan_artifacts,
)

prototype_development_plans_router = APIRouter(
    prefix='/prototype/workspaces/{workspace_slug}',
    tags=['prototype-development-plans'],
)


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
    x_operator_token: Optional[str] = None,
) -> IntakeWorkspace:
    workspace = await _get_workspace_or_404(workspace_slug)
    if not x_operator_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Missing X-Operator-Token header.')
    if not _secrets_mod.compare_digest(workspace.operator_token, x_operator_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Invalid operator token.')
    return workspace


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


@prototype_development_plans_router.post(
    '/development-plans/generate',
    response_model=DevelopmentPlanBatchResponse,
)
async def generate_plans(
    workspace_slug: str,
    body: DevelopmentPlanGenerateRequest,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    from company_intake.services import assert_workspace_ready_for_stage
    await assert_workspace_ready_for_stage(workspace, 'plans', planning_context=planning_context)
    try:
        result = await generate_development_plans(
            workspace,
            planning_context=planning_context,
            team_title=body.team_title,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return DevelopmentPlanBatchResponse(
        workspace_slug=workspace.slug,
        team_plan=DevelopmentPlanRunResponse(**(await build_plan_response(result['team_plan']))),
        individual_plans=[
            DevelopmentPlanRunResponse(**(await build_plan_response(run)))
            for run in result['individual_plans']
        ],
    )


@prototype_development_plans_router.get(
    '/development-plans/latest-team',
    response_model=DevelopmentPlanRunResponse,
)
async def get_latest_team_plan_view(
    workspace_slug: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    run = await get_latest_team_plan(workspace, planning_context=planning_context)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No team development plan found for this workspace.',
        )
    return DevelopmentPlanRunResponse(**(await build_plan_response(run)))


@prototype_development_plans_router.get(
    '/development-plans/latest-individual',
    response_model=list[DevelopmentPlanRunResponse],
)
async def list_latest_individual_plans_view(
    workspace_slug: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    runs = await list_latest_individual_plans(workspace, planning_context=planning_context)
    return [DevelopmentPlanRunResponse(**(await build_plan_response(run))) for run in runs]


@prototype_development_plans_router.get(
    '/development-plans/latest-summary',
    response_model=DevelopmentPlanSummaryResponse,
)
async def get_latest_plan_summary_view(
    workspace_slug: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    summary = await get_latest_plan_summary(workspace, planning_context=planning_context)
    return DevelopmentPlanSummaryResponse(**summary)


@prototype_development_plans_router.get(
    '/development-plans/latest-individual/{employee_uuid}',
    response_model=DevelopmentPlanRunResponse,
)
async def get_latest_individual_plan_view(
    workspace_slug: str,
    employee_uuid: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    run = await get_latest_individual_plan(workspace, employee_uuid, planning_context=planning_context)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No individual development plan found for this employee.',
        )
    return DevelopmentPlanRunResponse(**(await build_plan_response(run)))


@prototype_development_plans_router.get(
    '/development-plans/latest-team/actions',
    response_model=DevelopmentPlanSliceResponse,
)
async def get_latest_team_actions_view(
    workspace_slug: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    run = await get_latest_team_plan(workspace, planning_context=planning_context)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No team development plan found for this workspace.',
        )
    payload = await get_latest_team_actions(workspace, planning_context=planning_context)
    return DevelopmentPlanSliceResponse(
        **(await build_plan_slice_response(run, payload or {}))
    )


@prototype_development_plans_router.get(
    '/artifacts',
    response_model=DevelopmentPlanArtifactListResponse,
)
async def list_workspace_artifacts_view(
    workspace_slug: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    artifacts = await list_workspace_plan_artifacts(workspace, planning_context=planning_context)
    return DevelopmentPlanArtifactListResponse(
        **(await build_workspace_artifact_list_response(workspace, artifacts))
    )


@prototype_development_plans_router.get(
    '/artifacts/latest',
    response_model=DevelopmentPlanArtifactListResponse,
)
async def list_latest_workspace_artifacts_view(
    workspace_slug: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    artifacts = await list_latest_workspace_plan_artifacts(workspace, planning_context=planning_context)
    return DevelopmentPlanArtifactListResponse(
        **(await build_workspace_artifact_list_response(workspace, artifacts))
    )


@prototype_development_plans_router.get(
    '/development-plans/current-team',
    response_model=DevelopmentPlanRunResponse,
)
async def get_current_team_plan_view(
    workspace_slug: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    run = await get_current_team_plan(workspace, planning_context=planning_context)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No current team development plan found for this workspace.',
        )
    return DevelopmentPlanRunResponse(**(await build_plan_response(run)))


@prototype_development_plans_router.get(
    '/development-plans/current-individual',
    response_model=list[DevelopmentPlanRunResponse],
)
async def list_current_individual_plans_view(
    workspace_slug: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    runs = await list_current_individual_plans(workspace, planning_context=planning_context)
    return [DevelopmentPlanRunResponse(**(await build_plan_response(run))) for run in runs]


@prototype_development_plans_router.get(
    '/development-plans/current-summary',
    response_model=DevelopmentPlanSummaryResponse,
)
async def get_current_plan_summary_view(
    workspace_slug: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    summary = await get_current_plan_summary(workspace, planning_context=planning_context)
    return DevelopmentPlanSummaryResponse(**summary)


@prototype_development_plans_router.get(
    '/development-plans/current-individual/{employee_uuid}',
    response_model=DevelopmentPlanRunResponse,
)
async def get_current_individual_plan_view(
    workspace_slug: str,
    employee_uuid: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    run = await get_current_individual_plan(workspace, employee_uuid, planning_context=planning_context)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No current individual development plan found for this employee.',
        )
    return DevelopmentPlanRunResponse(**(await build_plan_response(run)))


@prototype_development_plans_router.get(
    '/development-plans/current-team/actions',
    response_model=DevelopmentPlanSliceResponse,
)
async def get_current_team_actions_view(
    workspace_slug: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    run = await get_current_team_plan(workspace, planning_context=planning_context)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No current team development plan found for this workspace.',
        )
    payload = await get_current_team_actions(workspace, planning_context=planning_context)
    return DevelopmentPlanSliceResponse(
        **(await build_plan_slice_response(run, payload or {}))
    )


@prototype_development_plans_router.get(
    '/development-plans/latest-team/downloads',
    response_model=DevelopmentPlanArtifactBundleResponse,
)
async def get_latest_team_downloads_view(
    workspace_slug: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    bundle = await get_latest_team_plan_artifact_bundle(workspace, planning_context=planning_context)
    if bundle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No team development plan artifacts found for this workspace.',
        )
    return DevelopmentPlanArtifactBundleResponse(**bundle)


@prototype_development_plans_router.get(
    '/development-plans/current-team/downloads',
    response_model=DevelopmentPlanArtifactBundleResponse,
)
async def get_current_team_downloads_view(
    workspace_slug: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    bundle = await get_current_team_plan_artifact_bundle(workspace, planning_context=planning_context)
    if bundle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No current team development plan artifacts found for this workspace.',
        )
    return DevelopmentPlanArtifactBundleResponse(**bundle)


@prototype_development_plans_router.get(
    '/development-plans/latest-individual/{employee_uuid}/downloads',
    response_model=DevelopmentPlanArtifactBundleResponse,
)
async def get_latest_individual_downloads_view(
    workspace_slug: str,
    employee_uuid: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    bundle = await get_latest_individual_plan_artifact_bundle(
        workspace,
        employee_uuid,
        planning_context=planning_context,
    )
    if bundle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No individual development plan artifacts found for this employee.',
        )
    return DevelopmentPlanArtifactBundleResponse(**bundle)


@prototype_development_plans_router.get(
    '/development-plans/current-individual/{employee_uuid}/downloads',
    response_model=DevelopmentPlanArtifactBundleResponse,
)
async def get_current_individual_downloads_view(
    workspace_slug: str,
    employee_uuid: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    bundle = await get_current_individual_plan_artifact_bundle(
        workspace,
        employee_uuid,
        planning_context=planning_context,
    )
    if bundle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No current individual development plan artifacts found for this employee.',
        )
    return DevelopmentPlanArtifactBundleResponse(**bundle)
