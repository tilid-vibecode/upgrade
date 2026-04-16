import secrets as _secrets_mod
from typing import Optional
from uuid import UUID

from asgiref.sync import sync_to_async
from fastapi import APIRouter, Header, HTTPException, Query, status

from company_intake.models import IntakeWorkspace
from org_context.models import PlanningContext

from .entities import (
    AssessmentCycleResponse,
    AssessmentGenerateRequest,
    AssessmentPackListResponse,
    AssessmentPackSubmitRequest,
    AssessmentStatusResponse,
    EmployeeAssessmentPackResponse,
)
from .services import (
    build_cycle_response,
    build_pack_response,
    generate_assessment_cycle,
    get_current_cycle,
    get_assessment_status,
    get_pack_by_uuid,
    list_cycle_packs,
    open_assessment_pack,
    regenerate_assessment_cycle,
    submit_assessment_pack_response,
)

prototype_employee_assessment_router = APIRouter(
    prefix='/prototype',
    tags=['prototype-employee-assessment'],
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


async def _get_pack_or_404(pack_uuid: str, *, mark_opened: bool = False):
    pack = await get_pack_by_uuid(pack_uuid, mark_opened=mark_opened)
    if pack is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Assessment pack not found.',
        )
    return pack


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


def _assert_planning_context_mutable(
    planning_context: PlanningContext | None,
    *,
    action_label: str,
):
    if planning_context is None:
        return
    if planning_context.status == PlanningContext.Status.ARCHIVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'Cannot {action_label} for an archived planning context.',
        )


# ---------------------------------------------------------------------------
# Operator routes (require X-Operator-Token)
# ---------------------------------------------------------------------------

@prototype_employee_assessment_router.post(
    '/workspaces/{workspace_slug}/assessments/generate',
    response_model=AssessmentCycleResponse,
)
async def generate_assessments(
    workspace_slug: str,
    body: AssessmentGenerateRequest,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    _assert_planning_context_mutable(planning_context, action_label='generate an assessment cycle')
    try:
        cycle = await generate_assessment_cycle(
            workspace,
            planning_context=planning_context,
            title=body.title,
            selected_employee_uuids=[str(item) for item in body.selected_employee_uuids],
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return AssessmentCycleResponse(**(await build_cycle_response(cycle)))


@prototype_employee_assessment_router.post(
    '/workspaces/{workspace_slug}/assessments/regenerate',
    response_model=AssessmentCycleResponse,
)
async def regenerate_assessments(
    workspace_slug: str,
    body: AssessmentGenerateRequest,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    _assert_planning_context_mutable(planning_context, action_label='regenerate an assessment cycle')
    try:
        cycle = await regenerate_assessment_cycle(
            workspace,
            planning_context=planning_context,
            title=body.title,
            selected_employee_uuids=[str(item) for item in body.selected_employee_uuids],
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return AssessmentCycleResponse(**(await build_cycle_response(cycle)))


@prototype_employee_assessment_router.get(
    '/workspaces/{workspace_slug}/assessments/latest',
    response_model=AssessmentCycleResponse,
)
async def get_latest_assessment_cycle(
    workspace_slug: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    cycle = await get_current_cycle(workspace, planning_context=planning_context)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No assessment cycle found for this workspace.',
        )
    return AssessmentCycleResponse(**(await build_cycle_response(cycle)))


@prototype_employee_assessment_router.get(
    '/workspaces/{workspace_slug}/assessments/status',
    response_model=AssessmentStatusResponse,
)
async def get_assessment_status_view(
    workspace_slug: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    payload = await get_assessment_status(workspace, planning_context=planning_context)
    return AssessmentStatusResponse(**payload)


@prototype_employee_assessment_router.get(
    '/workspaces/{workspace_slug}/assessments/latest/packs',
    response_model=AssessmentPackListResponse,
)
async def list_latest_assessment_packs(
    workspace_slug: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    cycle = await get_current_cycle(workspace, planning_context=planning_context)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No assessment cycle found for this workspace.',
        )
    packs = await list_cycle_packs(cycle)
    return AssessmentPackListResponse(
        workspace_slug=workspace.slug,
        cycle_uuid=cycle.uuid,
        packs=[EmployeeAssessmentPackResponse(**(await build_pack_response(pack))) for pack in packs],
    )


# ---------------------------------------------------------------------------
# Participant routes (public — pack UUID is the capability token)
# ---------------------------------------------------------------------------

@prototype_employee_assessment_router.get(
    '/assessment-packs/{pack_uuid}',
    response_model=EmployeeAssessmentPackResponse,
)
async def get_assessment_pack(pack_uuid: str):
    pack = await _get_pack_or_404(pack_uuid, mark_opened=False)
    return EmployeeAssessmentPackResponse(**(await build_pack_response(pack)))


@prototype_employee_assessment_router.post(
    '/assessment-packs/{pack_uuid}/open',
    response_model=EmployeeAssessmentPackResponse,
)
async def open_pack(pack_uuid: str):
    pack = await _get_pack_or_404(pack_uuid, mark_opened=False)
    try:
        pack = await open_assessment_pack(pack)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return EmployeeAssessmentPackResponse(**(await build_pack_response(pack)))


@prototype_employee_assessment_router.post(
    '/assessment-packs/{pack_uuid}/submit',
    response_model=EmployeeAssessmentPackResponse,
)
async def submit_pack_response(pack_uuid: str, body: AssessmentPackSubmitRequest):
    pack = await _get_pack_or_404(pack_uuid)
    try:
        pack = await submit_assessment_pack_response(pack, body.model_dump())
    except ValueError as exc:
        error_msg = str(exc)
        if 'finalized' in error_msg or 'superseded' in error_msg:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=error_msg) from exc
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg,
        ) from exc
    return EmployeeAssessmentPackResponse(**(await build_pack_response(pack)))
