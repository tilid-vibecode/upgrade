from uuid import UUID

from asgiref.sync import sync_to_async
from fastapi import APIRouter, HTTPException, Query, status

from company_intake.services import assert_workspace_ready_for_stage
from company_intake.models import IntakeWorkspace
from org_context.models import PlanningContext

from .entities import (
    BlueprintApproveRequest,
    BlueprintGenerateRequest,
    BlueprintPatchRequest,
    BlueprintPublishRequest,
    BlueprintRefreshRequest,
    BlueprintRevisionRequest,
    BlueprintReviewRequest,
    BlueprintRoadmapResponse,
    BlueprintRoleDetailResponse,
    ClarificationAnswerRequest,
    ClarificationCycleResponse,
    ClarificationQuestionResponse,
    ClarificationQuestionListResponse,
    RoleLibrarySnapshotResponse,
    RoleLibrarySyncRequest,
    SkillBlueprintRunResponse,
    SkillBlueprintRunListResponse,
)
from .models import RoleLibrarySnapshot, SkillBlueprintRun
from .services import (
    _resolve_workspace_latest_uuids,
    answer_blueprint_clarifications,
    approve_blueprint_run,
    get_active_clarification_run,
    build_clarification_cycle_response,
    build_clarification_question_response,
    build_blueprint_response,
    build_role_library_snapshot_response,
    generate_skill_blueprint,
    get_effective_blueprint_run,
    get_blueprint_run_or_none,
    get_latest_blueprint_run,
    get_latest_clarification_cycle,
    get_latest_role_library_snapshot,
    list_clarification_question_history,
    list_blueprint_runs,
    list_open_clarification_questions,
    patch_blueprint_run,
    publish_blueprint_run,
    refresh_blueprint_from_clarifications,
    review_blueprint_run,
    slugify_key,
    start_blueprint_revision,
    sync_role_library_for_workspace,
)

prototype_skill_blueprint_router = APIRouter(
    prefix='/prototype/workspaces/{workspace_slug}',
    tags=['prototype-skill-blueprint'],
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


async def _get_blueprint_run_or_404(workspace: IntakeWorkspace, blueprint_uuid: UUID) -> SkillBlueprintRun:
    run = await get_blueprint_run_or_none(workspace, blueprint_uuid)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Blueprint run not found for this workspace.',
        )
    return run


async def _get_planning_context_or_404(
    workspace: IntakeWorkspace,
    planning_context_uuid: UUID | None,
):
    if planning_context_uuid is None:
        return None
    context = await sync_to_async(
        lambda: PlanningContext.objects.filter(
            workspace=workspace,
            uuid=planning_context_uuid,
        ).first()
    )()
    if context is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Planning context not found in this workspace.',
        )
    return context


@prototype_skill_blueprint_router.post(
    '/role-library/sync',
    response_model=RoleLibrarySnapshotResponse,
)
async def sync_role_library(workspace_slug: str, body: RoleLibrarySyncRequest):
    workspace = await _get_workspace_or_404(workspace_slug)
    snapshot = await sync_role_library_for_workspace(
        workspace,
        base_urls=body.base_urls,
        max_pages=body.max_pages,
    )
    return RoleLibrarySnapshotResponse(**(await build_role_library_snapshot_response(snapshot)))


@prototype_skill_blueprint_router.get(
    '/role-library/latest',
    response_model=RoleLibrarySnapshotResponse,
)
async def get_latest_role_library(workspace_slug: str):
    workspace = await _get_workspace_or_404(workspace_slug)
    snapshot = await get_latest_role_library_snapshot(workspace)
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No role-library snapshot found for this workspace.',
        )
    return RoleLibrarySnapshotResponse(**(await build_role_library_snapshot_response(snapshot)))


@prototype_skill_blueprint_router.post(
    '/blueprint/generate',
    response_model=SkillBlueprintRunResponse,
)
async def generate_blueprint(
    workspace_slug: str,
    body: BlueprintGenerateRequest,
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _get_workspace_or_404(workspace_slug)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    await assert_workspace_ready_for_stage(workspace, 'blueprint', planning_context=planning_context)
    snapshot = None
    if body.role_library_snapshot_uuid:
        snapshot = await sync_to_async(
            lambda: RoleLibrarySnapshot.objects.filter(
                workspace=workspace,
                uuid=body.role_library_snapshot_uuid,
            ).first()
        )()
        if snapshot is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Requested role-library snapshot was not found for this workspace.',
            )

    run = await generate_skill_blueprint(workspace, planning_context=planning_context, role_library_snapshot=snapshot)
    return SkillBlueprintRunResponse(**(await build_blueprint_response(run)))


@prototype_skill_blueprint_router.get(
    '/blueprint/latest',
    response_model=SkillBlueprintRunResponse,
)
async def get_latest_blueprint(
    workspace_slug: str,
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _get_workspace_or_404(workspace_slug)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    run = await get_latest_blueprint_run(workspace, planning_context=planning_context)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No blueprint run found for this workspace.',
        )
    return SkillBlueprintRunResponse(**(await build_blueprint_response(run)))


@prototype_skill_blueprint_router.get(
    '/blueprint/current',
    response_model=SkillBlueprintRunResponse,
)
async def get_current_blueprint(
    workspace_slug: str,
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _get_workspace_or_404(workspace_slug)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    run = await get_effective_blueprint_run(workspace, planning_context=planning_context)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No effective blueprint run found for this workspace.',
        )
    return SkillBlueprintRunResponse(**(await build_blueprint_response(run)))


@prototype_skill_blueprint_router.get(
    '/blueprint/runs',
    response_model=SkillBlueprintRunListResponse,
)
async def get_blueprint_runs(
    workspace_slug: str,
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _get_workspace_or_404(workspace_slug)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    runs = await list_blueprint_runs(workspace, planning_context=planning_context)
    # Resolve latest UUIDs once for the whole list instead of 3 queries per run.
    latest_uuids = await sync_to_async(_resolve_workspace_latest_uuids)(
        workspace.pk,
        getattr(planning_context, 'pk', None),
    )
    return SkillBlueprintRunListResponse(
        workspace_slug=workspace.slug,
        runs=[
            SkillBlueprintRunResponse(**(await build_blueprint_response(run, latest_uuids=latest_uuids)))
            for run in runs
        ],
    )


@prototype_skill_blueprint_router.get(
    '/blueprint/{blueprint_uuid}',
    response_model=SkillBlueprintRunResponse,
)
async def get_blueprint_by_uuid(workspace_slug: str, blueprint_uuid: UUID):
    workspace = await _get_workspace_or_404(workspace_slug)
    run = await _get_blueprint_run_or_404(workspace, blueprint_uuid)
    return SkillBlueprintRunResponse(**(await build_blueprint_response(run)))


@prototype_skill_blueprint_router.post(
    '/blueprint/{blueprint_uuid}/patch',
    response_model=SkillBlueprintRunResponse,
)
async def patch_blueprint(workspace_slug: str, blueprint_uuid: UUID, body: BlueprintPatchRequest):
    workspace = await _get_workspace_or_404(workspace_slug)
    run = await _get_blueprint_run_or_404(workspace, blueprint_uuid)
    try:
        patched = await patch_blueprint_run(
            run,
            patch_payload=body.model_dump(exclude_none=True, exclude={'skip_employee_matching'}),
            skip_employee_matching=body.skip_employee_matching,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return SkillBlueprintRunResponse(**(await build_blueprint_response(patched)))


@prototype_skill_blueprint_router.post(
    '/blueprint/{blueprint_uuid}/start-revision',
    response_model=SkillBlueprintRunResponse,
)
async def start_revision(workspace_slug: str, blueprint_uuid: UUID, body: BlueprintRevisionRequest):
    workspace = await _get_workspace_or_404(workspace_slug)
    run = await _get_blueprint_run_or_404(workspace, blueprint_uuid)
    try:
        revised = await start_blueprint_revision(
            run,
            operator_name=body.operator_name,
            revision_reason=body.revision_reason,
            skip_employee_matching=body.skip_employee_matching,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return SkillBlueprintRunResponse(**(await build_blueprint_response(revised)))


@prototype_skill_blueprint_router.post(
    '/blueprint/{blueprint_uuid}/review',
    response_model=SkillBlueprintRunResponse,
)
async def review_blueprint(workspace_slug: str, blueprint_uuid: UUID, body: BlueprintReviewRequest):
    workspace = await _get_workspace_or_404(workspace_slug)
    run = await _get_blueprint_run_or_404(workspace, blueprint_uuid)
    try:
        reviewed = await review_blueprint_run(
            run,
            reviewer_name=body.reviewer_name,
            review_notes=body.review_notes,
            clarification_updates=[item.model_dump() for item in body.clarification_updates],
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return SkillBlueprintRunResponse(**(await build_blueprint_response(reviewed)))


@prototype_skill_blueprint_router.post(
    '/blueprint/{blueprint_uuid}/approve',
    response_model=SkillBlueprintRunResponse,
)
async def approve_blueprint(workspace_slug: str, blueprint_uuid: UUID, body: BlueprintApproveRequest):
    workspace = await _get_workspace_or_404(workspace_slug)
    run = await _get_blueprint_run_or_404(workspace, blueprint_uuid)
    try:
        approved = await approve_blueprint_run(
            run,
            approver_name=body.approver_name,
            approval_notes=body.approval_notes,
            clarification_updates=[item.model_dump() for item in body.clarification_updates],
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return SkillBlueprintRunResponse(**(await build_blueprint_response(approved)))


@prototype_skill_blueprint_router.post(
    '/blueprint/{blueprint_uuid}/publish',
    response_model=SkillBlueprintRunResponse,
)
async def publish_blueprint(workspace_slug: str, blueprint_uuid: UUID, body: BlueprintPublishRequest):
    workspace = await _get_workspace_or_404(workspace_slug)
    run = await _get_blueprint_run_or_404(workspace, blueprint_uuid)
    try:
        published = await publish_blueprint_run(
            run,
            publisher_name=body.publisher_name,
            publish_notes=body.publish_notes,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return SkillBlueprintRunResponse(**(await build_blueprint_response(published)))


@prototype_skill_blueprint_router.post(
    '/blueprint/{blueprint_uuid}/clarifications/answer',
    response_model=SkillBlueprintRunResponse,
)
async def answer_clarifications(workspace_slug: str, blueprint_uuid: UUID, body: ClarificationAnswerRequest):
    workspace = await _get_workspace_or_404(workspace_slug)
    run = await _get_blueprint_run_or_404(workspace, blueprint_uuid)
    try:
        updated = await answer_blueprint_clarifications(
            run,
            operator_name=body.operator_name,
            answer_items=[item.model_dump(exclude_none=True) for item in body.items],
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return SkillBlueprintRunResponse(**(await build_blueprint_response(updated)))


@prototype_skill_blueprint_router.post(
    '/blueprint/{blueprint_uuid}/refresh-from-clarifications',
    response_model=SkillBlueprintRunResponse,
)
async def refresh_blueprint(
    workspace_slug: str,
    blueprint_uuid: UUID,
    body: BlueprintRefreshRequest,
):
    workspace = await _get_workspace_or_404(workspace_slug)
    run = await _get_blueprint_run_or_404(workspace, blueprint_uuid)
    try:
        refreshed = await refresh_blueprint_from_clarifications(
            run,
            operator_name=body.operator_name,
            refresh_note=body.refresh_note,
            skip_employee_matching=body.skip_employee_matching,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return SkillBlueprintRunResponse(**(await build_blueprint_response(refreshed)))


@prototype_skill_blueprint_router.get(
    '/clarifications/latest',
    response_model=ClarificationCycleResponse,
)
async def get_latest_workspace_clarifications(
    workspace_slug: str,
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _get_workspace_or_404(workspace_slug)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    cycle = await get_latest_clarification_cycle(workspace, planning_context=planning_context)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No clarification cycle found for this workspace.',
        )
    return ClarificationCycleResponse(**(await build_clarification_cycle_response(cycle)))


@prototype_skill_blueprint_router.get(
    '/clarifications/open',
    response_model=ClarificationQuestionListResponse,
)
async def get_open_workspace_clarifications(
    workspace_slug: str,
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _get_workspace_or_404(workspace_slug)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    active_run = await get_active_clarification_run(workspace, planning_context=planning_context)
    questions = await list_open_clarification_questions(
        workspace,
        blueprint_run=active_run,
        planning_context=planning_context,
    )
    return ClarificationQuestionListResponse(
        workspace_slug=workspace.slug,
        blueprint_uuid=(active_run.uuid if active_run is not None else None),
        questions=[
            ClarificationQuestionResponse(**build_clarification_question_response(question))
            for question in questions
        ],
    )


@prototype_skill_blueprint_router.get(
    '/clarifications/history',
    response_model=ClarificationQuestionListResponse,
)
async def get_workspace_clarification_history(
    workspace_slug: str,
    planning_context_uuid: UUID | None = Query(None),
):
    workspace = await _get_workspace_or_404(workspace_slug)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    questions = await list_clarification_question_history(workspace, planning_context=planning_context)
    blueprint_ids = {question.blueprint_run_id for question in questions}
    return ClarificationQuestionListResponse(
        workspace_slug=workspace.slug,
        blueprint_uuid=next(iter(blueprint_ids)) if len(blueprint_ids) == 1 else None,
        questions=[
            ClarificationQuestionResponse(**build_clarification_question_response(question))
            for question in questions
        ],
    )


@prototype_skill_blueprint_router.get(
    '/blueprint/{blueprint_uuid}/roadmap',
    response_model=BlueprintRoadmapResponse,
)
async def get_blueprint_roadmap(workspace_slug: str, blueprint_uuid: UUID):
    workspace = await _get_workspace_or_404(workspace_slug)
    run = await _get_blueprint_run_or_404(workspace, blueprint_uuid)
    return BlueprintRoadmapResponse(
        workspace_slug=workspace.slug,
        blueprint_uuid=run.uuid,
        roadmap_context=list(run.roadmap_context or []),
    )


@prototype_skill_blueprint_router.get(
    '/blueprint/{blueprint_uuid}/roles/{role_key}',
    response_model=BlueprintRoleDetailResponse,
)
async def get_blueprint_role_detail(workspace_slug: str, blueprint_uuid: UUID, role_key: str):
    workspace = await _get_workspace_or_404(workspace_slug)
    run = await _get_blueprint_run_or_404(workspace, blueprint_uuid)
    role_candidate = next(
        (
            item for item in (run.role_candidates or [])
            if (item.get('role_key') or slugify_key(
                f"{item.get('canonical_role_family', '')}-{item.get('seniority', '')}-{item.get('role_name', '')}"
            )) == role_key
        ),
        None,
    )
    if role_candidate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Role candidate not found in this blueprint run.',
        )
    return BlueprintRoleDetailResponse(
        workspace_slug=workspace.slug,
        blueprint_uuid=run.uuid,
        role_key=role_key,
        role_candidate=role_candidate,
    )
