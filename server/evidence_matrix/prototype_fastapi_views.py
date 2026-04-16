import secrets as _secrets_mod
from typing import Optional
from uuid import UUID

from asgiref.sync import sync_to_async
from fastapi import APIRouter, Header, HTTPException, Query, status

from company_intake.models import IntakeWorkspace
from org_context.models import PlanningContext

from .entities import (
    EvidenceMatrixBuildRequest,
    EvidenceMatrixRunResponse,
    EvidenceMatrixSliceResponse,
)
from .services import (
    build_evidence_matrix,
    build_matrix_run_response,
    build_matrix_slice_response,
    get_current_completed_matrix_run,
    get_matrix_employee_payload,
)

prototype_evidence_matrix_router = APIRouter(
    prefix='/prototype/workspaces/{workspace_slug}',
    tags=['prototype-evidence-matrix'],
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


@prototype_evidence_matrix_router.post(
    '/evidence-matrix/build',
    response_model=EvidenceMatrixRunResponse,
)
async def build_matrix(
    workspace_slug: str,
    body: EvidenceMatrixBuildRequest,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    _assert_planning_context_mutable(planning_context, action_label='build an evidence matrix')
    from company_intake.services import assert_workspace_ready_for_stage
    await assert_workspace_ready_for_stage(workspace, 'matrix', planning_context=planning_context)
    try:
        run = await build_evidence_matrix(
            workspace,
            planning_context=planning_context,
            title=body.title,
            assessment_cycle_uuid=str(body.assessment_cycle_uuid) if body.assessment_cycle_uuid else None,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return EvidenceMatrixRunResponse(**(await build_matrix_run_response(run)))


@prototype_evidence_matrix_router.get(
    '/evidence-matrix/latest',
    response_model=EvidenceMatrixRunResponse,
)
async def get_latest_matrix(
    workspace_slug: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    run = await get_current_completed_matrix_run(workspace, planning_context=planning_context)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No completed evidence matrix run found for the current published blueprint.',
        )
    return EvidenceMatrixRunResponse(**(await build_matrix_run_response(run)))


@prototype_evidence_matrix_router.get(
    '/evidence-matrix/latest/heatmap',
    response_model=EvidenceMatrixSliceResponse,
)
async def get_latest_matrix_heatmap(
    workspace_slug: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    run = await get_current_completed_matrix_run(workspace, planning_context=planning_context)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No completed evidence matrix run found for the current published blueprint.',
        )
    return EvidenceMatrixSliceResponse(
        **(await build_matrix_slice_response(run, run.heatmap_payload or {}))
    )


@prototype_evidence_matrix_router.get(
    '/evidence-matrix/latest/cells',
    response_model=EvidenceMatrixSliceResponse,
)
async def get_latest_matrix_cells(
    workspace_slug: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    run = await get_current_completed_matrix_run(workspace, planning_context=planning_context)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No completed evidence matrix run found for the current published blueprint.',
        )
    payload = {
        'matrix_cells': list((run.matrix_payload or {}).get('matrix_cells') or []),
        'employee_count': len(list((run.matrix_payload or {}).get('employees') or [])),
    }
    return EvidenceMatrixSliceResponse(**(await build_matrix_slice_response(run, payload)))


@prototype_evidence_matrix_router.get(
    '/evidence-matrix/latest/risks',
    response_model=EvidenceMatrixSliceResponse,
)
async def get_latest_matrix_risks(
    workspace_slug: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    run = await get_current_completed_matrix_run(workspace, planning_context=planning_context)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No completed evidence matrix run found for the current published blueprint.',
        )
    return EvidenceMatrixSliceResponse(
        **(await build_matrix_slice_response(run, run.risk_payload or {}))
    )


@prototype_evidence_matrix_router.get(
    '/evidence-matrix/latest/employees/{employee_uuid}',
    response_model=EvidenceMatrixSliceResponse,
)
async def get_latest_matrix_employee(
    workspace_slug: str,
    employee_uuid: str,
    x_operator_token: Optional[str] = Header(None),
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _require_operator(workspace_slug, x_operator_token)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    run = await get_current_completed_matrix_run(workspace, planning_context=planning_context)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No completed evidence matrix run found for the current published blueprint.',
        )
    employee_payload = await get_matrix_employee_payload(run, employee_uuid)
    if employee_payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Employee matrix payload not found in the latest run.',
        )
    return EvidenceMatrixSliceResponse(
        **(await build_matrix_slice_response(run, employee_payload))
    )
