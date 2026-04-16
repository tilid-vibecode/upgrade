from uuid import UUID

from asgiref.sync import sync_to_async
from django.core.exceptions import ValidationError
from fastapi import APIRouter, HTTPException, status

from company_intake.entities import WorkspaceSourceResponse
from company_intake.models import IntakeWorkspace, WorkspaceSource, WorkspaceSourceStatus
from company_intake.services import assert_workspace_ready_for_stage, build_workspace_source_response

from .cv_services import (
    accept_employee_high_confidence_skills,
    approve_pending_skill_candidate,
    build_cv_evidence_for_workspace,
    clear_employee_no_cv_available,
    delete_workspace_employee,
    get_cv_evidence_status,
    get_employee_cv_evidence_detail,
    get_workspace_pending_skills,
    list_cv_review_items,
    list_employees_without_cv_evidence,
    mark_employee_no_cv_available,
    list_unmatched_cv_profiles,
    rebuild_cv_evidence_for_workspace,
    resolve_workspace_skills_bulk,
    resolve_cv_profile_match,
    review_employee_skills_bulk,
)
from .entities import (
    CVEvidenceBuildRequest,
    CVEvidenceBuildResponse,
    CVMatchResolutionRequest,
    CVEvidenceReviewListResponse,
    CVEvidenceSourceResult,
    CVEvidenceStatusResponse,
    EmployeeSkillAcceptAllRequest,
    EmployeeSkillAcceptAllResponse,
    EmployeeSkillBulkReviewRequest,
    EmployeeSkillBulkReviewResponse,
    EmployeeCvAvailabilityRequest,
    EmployeeCvAvailabilityResponse,
    EmployeeCVProfileResponse,
    EmployeeCoverageGapResponse,
    EmployeeDeleteResponse,
    EmployeeEvidenceDetailResponse,
    EmployeeListResponse,
    EmployeeWithoutCVEvidenceResponse,
    EmployeesWithoutCVEvidenceListResponse,
    EmployeeResponse,
    EmployeeRoleMatchListResponse,
    EmployeeRoleMatchResponse,
    EmployeeSkillEvidenceResponse,
    PlanningContextCreateRequest,
    PlanningContextDetailResponse,
    PlanningContextListResponse,
    PlanningContextParentResponse,
    PlanningContextProjectResponse,
    PlanningContextProfilePayload,
    PlanningContextSourceCreateRequest,
    PlanningContextSourceLinkResponse,
    PlanningContextSummaryResponse,
    PlanningContextUpdateRequest,
    PendingWorkspaceSkillResponse,
    RoadmapAnalysisRunRequest,
    RoadmapAnalysisRunResponse,
    RoadmapAnalysisStatusResponse,
    RoadmapAnalysisTriggerResponse,
    RoadmapAnalysisRunSummaryResponse,
    OrgContextSummaryResponse,
    OrgCsvPreviewRequest,
    ProjectCreateRequest,
    ProjectListResponse,
    ProjectSummaryResponse,
    OrgCsvPreviewResponse,
    PendingSkillApprovalRequest,
    ParsedSourceDetailResponse,
    ParsedSourceListResponse,
    ParsedSourceReparseRequest,
    ParsedSourceReparseResponse,
    ParsedSourceResponse,
    SourceChunkResponse,
    UnmatchedCVListResponse,
    WorkspacePendingSkillsResponse,
    WorkspaceSkillResolutionRequest,
    WorkspaceSkillResolutionResponse,
)
from django.db.models import Count

from .models import (
    Employee,
    EmployeeRoleMatch,
    EmployeeSkillEvidence,
    ContextProfile,
    OrgUnit,
    ParsedSource,
    PlanningContext,
    PlanningContextSource,
    Project,
    RoadmapAnalysisRun,
    ReportingLine,
    Skill,
)
from .roadmap_services import (
    build_roadmap_analysis_response,
    build_roadmap_analysis_status_payload,
    get_latest_roadmap_analysis_run,
    run_roadmap_analysis,
)
from .services import parse_workspace_source, preview_org_csv_source

prototype_org_context_router = APIRouter(
    prefix='/prototype/workspaces/{workspace_slug}/org-context',
    tags=['prototype-org-context'],
)

prototype_planning_context_router = APIRouter(
    prefix='/prototype/workspaces/{workspace_slug}/planning-contexts',
    tags=['prototype-planning-contexts'],
)

_PLANNING_CONTEXT_SOURCE_USAGE_BY_KIND = {
    'roadmap': 'roadmap',
    'strategy': 'strategy',
    'job_description': 'role_reference',
    'org_csv': 'org_structure',
    'employee_cv': 'employee_cv',
    'existing_matrix': 'other',
    'other': 'other',
}


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


async def _get_employee_or_404(workspace: IntakeWorkspace, employee_uuid: UUID) -> Employee:
    employee = await sync_to_async(
        Employee.objects.filter(workspace=workspace, uuid=employee_uuid).first
    )()
    if employee is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Employee not found.',
        )
    return employee


async def _get_planning_context_or_404(
    workspace: IntakeWorkspace,
    planning_context_uuid: UUID | None,
) -> PlanningContext | None:
    if planning_context_uuid is None:
        return None
    planning_context = await sync_to_async(
        lambda: PlanningContext.objects.select_related('workspace', 'organization', 'project', 'parent_context')
        .filter(workspace=workspace, uuid=planning_context_uuid)
        .first()
    )()
    if planning_context is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Planning context not found in this workspace.',
        )
    return planning_context


async def _get_planning_context_by_slug_or_404(
    workspace: IntakeWorkspace,
    context_slug: str,
) -> PlanningContext:
    planning_context = await sync_to_async(
        lambda: PlanningContext.objects.select_related('workspace', 'organization', 'project', 'parent_context')
        .filter(workspace=workspace, slug=context_slug)
        .first()
    )()
    if planning_context is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Planning context not found in this workspace.',
        )
    return planning_context


async def _get_workspace_source_or_404(workspace: IntakeWorkspace, source_uuid: UUID) -> WorkspaceSource:
    source = await sync_to_async(
        WorkspaceSource.objects.select_related('workspace', 'media_file').filter(
            workspace=workspace,
            uuid=source_uuid,
        ).exclude(
            status=WorkspaceSourceStatus.ARCHIVED,
        ).first
    )()
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Workspace source not found.',
        )
    return source


async def _get_parsed_source_or_404(workspace: IntakeWorkspace, parsed_source_uuid: UUID) -> ParsedSource:
    parsed_source = await sync_to_async(
        ParsedSource.objects.select_related('source', 'source__workspace', 'source__media_file')
        .prefetch_related('chunks')
        .filter(workspace=workspace, uuid=parsed_source_uuid)
        .filter(source__status=WorkspaceSourceStatus.PARSED)
        .first
    )()
    if parsed_source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Parsed source not found.',
        )
    return parsed_source


def _build_parsed_source_response(parsed_source: ParsedSource) -> ParsedSourceResponse:
    metadata = dict(parsed_source.metadata or {})
    warnings = metadata.get('warnings') or []
    vector_index = metadata.get('vector_index') or {}
    language_code = metadata.get('language_code') or ((metadata.get('quality') or {}).get('language_code')) or ''
    # Prefer the annotated count from the list query, then prefetched cache, then metadata fallback.
    db_chunk_count = getattr(parsed_source, 'db_chunk_count', None)
    if db_chunk_count is not None:
        chunk_count = db_chunk_count
    else:
        prefetched_chunks = getattr(parsed_source, '_prefetched_objects_cache', {}).get('chunks')
        chunk_count = (
            len(prefetched_chunks) if prefetched_chunks is not None
            else int((metadata.get('content') or {}).get('chunk_count') or metadata.get('chunk_count') or 0)
        )
    source = parsed_source.source

    return ParsedSourceResponse(
        uuid=parsed_source.uuid,
        source_uuid=source.uuid,
        source_kind=source.source_kind,
        source_title=source.title,
        source_status=source.status,
        parse_error=source.parse_error,
        parser_name=parsed_source.parser_name,
        parser_version=parsed_source.parser_version,
        content_type=parsed_source.content_type,
        page_count=parsed_source.page_count,
        word_count=parsed_source.word_count,
        char_count=parsed_source.char_count,
        chunk_count=chunk_count,
        warning_count=len(warnings),
        language_code=language_code,
        vector_index_status=vector_index.get('status', ''),
        metadata=metadata,
        created_at=parsed_source.created_at,
        updated_at=parsed_source.updated_at,
    )


def _build_source_chunk_response(chunk) -> SourceChunkResponse:
    return SourceChunkResponse(
        chunk_index=chunk.chunk_index,
        char_count=chunk.char_count,
        text=chunk.text,
        metadata=chunk.metadata or {},
        created_at=chunk.created_at,
        updated_at=chunk.updated_at,
    )


def _build_planning_context_profile_payload(
    profile: ContextProfile | None,
    *,
    planning_context: PlanningContext | None = None,
) -> PlanningContextProfilePayload:
    if profile is None:
        return PlanningContextProfilePayload()
    inherit_from_parent = bool(profile.inherit_from_parent)
    if planning_context is not None and planning_context.kind == PlanningContext.Kind.ORG and planning_context.parent_context_id is None:
        inherit_from_parent = False
    return PlanningContextProfilePayload(
        company_profile=dict(profile.company_profile or {}),
        tech_stack=list(profile.tech_stack or []),
        tech_stack_remove=list(profile.tech_stack_remove or []),
        constraints=list(profile.constraints or []),
        growth_goals=list(profile.growth_goals or []),
        inherit_from_parent=inherit_from_parent,
        override_fields=list(profile.override_fields or []),
    )


def _serialize_effective_profile(effective_profile: dict | None) -> dict:
    payload = dict(effective_profile or {})
    return {
        'company_profile': dict(payload.get('company_profile') or {}),
        'tech_stack': list(payload.get('tech_stack') or []),
        'constraints': list(payload.get('constraints') or []),
        'growth_goals': list(payload.get('growth_goals') or []),
    }


def _build_context_source_links_sync(planning_context_pk) -> list[PlanningContextSourceLinkResponse]:
    planning_context = PlanningContext.objects.select_related('parent_context').get(pk=planning_context_pk)
    chain: list[PlanningContext] = []
    current = planning_context
    while current is not None:
        chain.append(current)
        current = current.parent_context

    links = list(
        PlanningContextSource.objects.select_related('workspace_source', 'planning_context')
        .filter(planning_context_id__in=[context.pk for context in chain])
    )

    priority = {context.pk: index for index, context in enumerate(chain)}
    nearest_by_source: dict[UUID, tuple[int, PlanningContextSource]] = {}
    for link in links:
        rank = priority.get(link.planning_context_id, 999)
        if (
            link.workspace_source_id not in nearest_by_source
            or rank < nearest_by_source[link.workspace_source_id][0]
        ):
            nearest_by_source[link.workspace_source_id] = (rank, link)

    responses: list[PlanningContextSourceLinkResponse] = []
    for _rank, link in nearest_by_source.values():
        origin = 'direct'
        excluded_reason = ''
        inherited_from_context_uuid = None
        inherited_from_context_slug = ''

        if not link.is_active:
            origin = 'excluded'
            if link.planning_context_id != planning_context.pk:
                inherited_from_context_uuid = link.planning_context.uuid
                inherited_from_context_slug = link.planning_context.slug
                excluded_reason = 'Deactivated by an inherited context rule.'
            else:
                excluded_reason = 'Deactivated at this context.'
        elif link.planning_context_id != planning_context.pk:
            origin = 'inherited'
            inherited_from_context_uuid = link.planning_context.uuid
            inherited_from_context_slug = link.planning_context.slug

        responses.append(
            PlanningContextSourceLinkResponse(
                uuid=link.uuid,
                workspace_source_uuid=link.workspace_source.uuid,
                title=link.workspace_source.title,
                source_kind=link.workspace_source.source_kind,
                usage_type=link.usage_type,
                is_active=bool(link.is_active),
                include_in_blueprint=bool(link.include_in_blueprint),
                include_in_roadmap_analysis=bool(link.include_in_roadmap_analysis),
                origin=origin,
                inherited_from_context_uuid=inherited_from_context_uuid,
                inherited_from_context_slug=inherited_from_context_slug,
                excluded_reason=excluded_reason,
            )
        )
    return sorted(
        responses,
        key=lambda item: (item.origin != 'direct', item.origin == 'excluded', item.title.casefold(), item.source_kind),
    )


def _normalize_profile_payload(
    profile_payload: dict | None,
    *,
    planning_context: PlanningContext | None = None,
    context_kind: str,
    parent_context_pk=None,
) -> dict:
    payload = dict(profile_payload or {})
    normalized = {
        'company_profile': dict(payload.get('company_profile') or {}),
        'tech_stack': list(payload.get('tech_stack') or []),
        'tech_stack_remove': list(payload.get('tech_stack_remove') or []),
        'constraints': list(payload.get('constraints') or []),
        'growth_goals': list(payload.get('growth_goals') or []),
        'inherit_from_parent': bool(payload.get('inherit_from_parent', True)),
        'override_fields': list(payload.get('override_fields') or []),
    }
    if context_kind == PlanningContext.Kind.ORG and parent_context_pk is None:
        normalized['inherit_from_parent'] = False
    elif planning_context is not None and 'inherit_from_parent' not in payload:
        normalized['inherit_from_parent'] = bool(getattr(planning_context.profile, 'inherit_from_parent', True))
    return normalized


def _validation_error_detail(exc: ValidationError) -> str:
    messages = list(getattr(exc, 'messages', []) or [])
    if messages:
        return '; '.join(messages)
    message_dict = getattr(exc, 'message_dict', {}) or {}
    flattened = [message for field_messages in message_dict.values() for message in field_messages]
    return '; '.join(flattened) if flattened else str(exc)


def _build_planning_context_summary_sync(planning_context_pk) -> PlanningContextSummaryResponse:
    planning_context = PlanningContext.objects.select_related('parent_context').get(pk=planning_context_pk)
    return PlanningContextSummaryResponse(
        uuid=planning_context.uuid,
        name=planning_context.name,
        slug=planning_context.slug,
        kind=planning_context.kind,
        status=planning_context.status,
        parent_context_uuid=getattr(planning_context.parent_context, 'uuid', None),
        child_count=planning_context.child_contexts.count(),
        source_count=len(PlanningContext.resolve_effective_sources(planning_context)),
        has_blueprint=planning_context.blueprint_runs.exists(),
        has_roadmap_analysis=planning_context.roadmap_analyses.filter(
            status=RoadmapAnalysisRun.Status.COMPLETED
        ).exists(),
    )


def _build_planning_context_detail_sync(planning_context_pk) -> PlanningContextDetailResponse:
    planning_context = (
        PlanningContext.objects.select_related('parent_context', 'profile', 'project', 'workspace')
        .filter(pk=planning_context_pk)
        .first()
    )
    if planning_context is None:
        raise PlanningContext.DoesNotExist()
    effective_profile = PlanningContext.resolve_effective_profile(planning_context)
    parent_context = None
    if planning_context.parent_context_id:
        parent_context = PlanningContextParentResponse(
            uuid=planning_context.parent_context.uuid,
            name=planning_context.parent_context.name,
            slug=planning_context.parent_context.slug,
        )
    return PlanningContextDetailResponse(
        uuid=planning_context.uuid,
        name=planning_context.name,
        slug=planning_context.slug,
        kind=planning_context.kind,
        status=planning_context.status,
        description=planning_context.description,
        metadata=dict(planning_context.metadata or {}),
        parent_context=parent_context,
        project=(
            PlanningContextProjectResponse(
                uuid=planning_context.project.uuid,
                name=planning_context.project.name,
            )
            if planning_context.project_id
            else None
        ),
        profile=_build_planning_context_profile_payload(
            getattr(planning_context, 'profile', None),
            planning_context=planning_context,
        ),
        effective_profile=_serialize_effective_profile(effective_profile),
        sources=_build_context_source_links_sync(planning_context.pk),
        created_at=planning_context.created_at,
        updated_at=planning_context.updated_at,
    )


def _create_planning_context_sync(
    workspace_pk,
    payload: dict,
    parent_context_pk=None,
    project_pk=None,
) -> PlanningContext:
    workspace = IntakeWorkspace.objects.get(pk=workspace_pk)
    planning_context = PlanningContext(
        workspace=workspace,
        organization=workspace.organization,
        parent_context_id=parent_context_pk,
        project_id=project_pk,
        name=str(payload.get('name') or '').strip(),
        slug=str(payload.get('slug') or '').strip(),
        kind=str(payload.get('kind') or PlanningContext.Kind.ORG).strip(),
        status=PlanningContext.Status.ACTIVE,
        description=str(payload.get('description') or '').strip(),
        metadata=dict(payload.get('metadata') or {}),
    )
    planning_context.full_clean()
    planning_context.save()
    profile_payload = _normalize_profile_payload(
        payload.get('profile'),
        context_kind=planning_context.kind,
        parent_context_pk=parent_context_pk,
    )
    profile = ContextProfile(
        planning_context=planning_context,
        company_profile=profile_payload['company_profile'],
        tech_stack=profile_payload['tech_stack'],
        tech_stack_remove=profile_payload['tech_stack_remove'],
        constraints=profile_payload['constraints'],
        growth_goals=profile_payload['growth_goals'],
        inherit_from_parent=profile_payload['inherit_from_parent'],
        override_fields=profile_payload['override_fields'],
    )
    profile.full_clean()
    profile.save()
    return planning_context


def _create_workspace_project_sync(workspace_pk, project_name: str) -> Project:
    workspace = IntakeWorkspace.objects.get(pk=workspace_pk)
    project = Project(
        workspace=workspace,
        name=project_name.strip(),
    )
    project.full_clean()
    project.save()
    return project


def _update_planning_context_sync(
    planning_context_pk,
    payload: dict,
) -> PlanningContext:
    planning_context = PlanningContext.objects.select_related('profile').get(pk=planning_context_pk)
    if payload.get('name') is not None:
        planning_context.name = str(payload.get('name') or '').strip()
    if payload.get('slug') is not None:
        planning_context.slug = str(payload.get('slug') or '').strip()
    if payload.get('status') is not None:
        planning_context.status = str(payload.get('status') or '').strip()
    if payload.get('description') is not None:
        planning_context.description = str(payload.get('description') or '').strip()
    if payload.get('metadata') is not None:
        planning_context.metadata = dict(payload.get('metadata') or {})
    planning_context.full_clean()
    planning_context.save(update_fields=['name', 'slug', 'status', 'description', 'metadata', 'updated_at'])

    profile_payload = payload.get('profile')
    if profile_payload is not None:
        profile, _created = ContextProfile.objects.get_or_create(planning_context=planning_context)
        normalized_profile = _normalize_profile_payload(
            profile_payload,
            planning_context=planning_context,
            context_kind=planning_context.kind,
            parent_context_pk=planning_context.parent_context_id,
        )
        profile.company_profile = normalized_profile['company_profile']
        profile.tech_stack = normalized_profile['tech_stack']
        profile.tech_stack_remove = normalized_profile['tech_stack_remove']
        profile.constraints = normalized_profile['constraints']
        profile.growth_goals = normalized_profile['growth_goals']
        profile.inherit_from_parent = normalized_profile['inherit_from_parent']
        profile.override_fields = normalized_profile['override_fields']
        profile.full_clean()
        profile.save(
            update_fields=[
                'company_profile',
                'tech_stack',
                'tech_stack_remove',
                'constraints',
                'growth_goals',
                'inherit_from_parent',
                'override_fields',
                'updated_at',
            ]
        )

    return planning_context


def _upsert_planning_context_source_sync(
    planning_context_pk,
    workspace_source_pk,
    payload: dict,
) -> PlanningContextSource:
    planning_context = PlanningContext.objects.get(pk=planning_context_pk)
    workspace_source = WorkspaceSource.objects.get(pk=workspace_source_pk)
    usage_type = str(
        payload.get('usage_type')
        or _PLANNING_CONTEXT_SOURCE_USAGE_BY_KIND.get(
            workspace_source.source_kind,
            PlanningContextSource.UsageType.OTHER,
        )
    ).strip()
    include_in_roadmap_analysis = payload.get('include_in_roadmap_analysis')
    if include_in_roadmap_analysis is None:
        include_in_roadmap_analysis = usage_type in {
            PlanningContextSource.UsageType.ROADMAP,
            PlanningContextSource.UsageType.STRATEGY,
        }

    link = PlanningContextSource.objects.filter(
        planning_context=planning_context,
        workspace_source=workspace_source,
    ).first() or PlanningContextSource(
        planning_context=planning_context,
        workspace_source=workspace_source,
    )
    link.usage_type = usage_type
    link.is_active = bool(payload.get('is_active', True))
    link.include_in_blueprint = bool(payload.get('include_in_blueprint', True))
    link.include_in_roadmap_analysis = bool(include_in_roadmap_analysis)
    link.inherited_from = None
    link.full_clean()
    link.save()
    return PlanningContextSource.objects.select_related('workspace_source', 'planning_context').get(pk=link.pk)


@prototype_planning_context_router.get('', response_model=PlanningContextListResponse)
async def list_planning_contexts(workspace_slug: str):
    workspace = await _get_workspace_or_404(workspace_slug)
    contexts = await sync_to_async(list)(
        PlanningContext.objects.filter(workspace=workspace).order_by('kind', 'name')
    )
    items = await sync_to_async(
        lambda: [_build_planning_context_summary_sync(context.pk) for context in contexts]
    )()
    return PlanningContextListResponse(
        workspace_slug=workspace.slug,
        contexts=items,
    )


@prototype_planning_context_router.post('', response_model=PlanningContextDetailResponse)
async def create_planning_context(workspace_slug: str, body: PlanningContextCreateRequest):
    workspace = await _get_workspace_or_404(workspace_slug)
    if await sync_to_async(
        PlanningContext.objects.filter(workspace=workspace, slug=body.slug).exists
    )():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='A planning context with this slug already exists in the workspace.',
        )

    parent_context = await _get_planning_context_or_404(workspace, body.parent_context_uuid)
    project = None
    if body.project_uuid is not None:
        project = await sync_to_async(
            lambda: Project.objects.filter(workspace=workspace, uuid=body.project_uuid).first()
        )()
        if project is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Project not found in this workspace.',
            )

    if body.kind == PlanningContext.Kind.ORG and parent_context is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Org contexts cannot have a parent.')
    if body.kind == PlanningContext.Kind.PROJECT and project is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Project contexts require project_uuid.')
    if body.kind == PlanningContext.Kind.PROJECT and parent_context is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Project contexts require an org parent.')
    if body.kind == PlanningContext.Kind.PROJECT and parent_context is not None and parent_context.kind != PlanningContext.Kind.ORG:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Project contexts must inherit from an org context.',
        )
    if body.kind != PlanningContext.Kind.PROJECT and project is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Only project contexts can reference project_uuid.',
        )
    if body.kind == PlanningContext.Kind.SCENARIO and parent_context is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Scenario contexts require parent_context_uuid.',
        )
    if (
        body.kind == PlanningContext.Kind.SCENARIO
        and parent_context is not None
        and parent_context.kind not in {PlanningContext.Kind.ORG, PlanningContext.Kind.PROJECT}
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Scenario contexts must inherit from an org or project context.',
        )

    try:
        planning_context = await sync_to_async(_create_planning_context_sync)(
            workspace.pk,
            body.model_dump(),
            getattr(parent_context, 'pk', None),
            getattr(project, 'pk', None),
        )
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_validation_error_detail(exc)) from exc
    return await sync_to_async(_build_planning_context_detail_sync)(planning_context.pk)


@prototype_planning_context_router.get('/{context_slug}', response_model=PlanningContextDetailResponse)
async def get_planning_context_detail(workspace_slug: str, context_slug: str):
    workspace = await _get_workspace_or_404(workspace_slug)
    planning_context = await _get_planning_context_by_slug_or_404(workspace, context_slug)
    return await sync_to_async(_build_planning_context_detail_sync)(planning_context.pk)


@prototype_planning_context_router.patch('/{context_slug}', response_model=PlanningContextDetailResponse)
async def patch_planning_context(
    workspace_slug: str,
    context_slug: str,
    body: PlanningContextUpdateRequest,
):
    workspace = await _get_workspace_or_404(workspace_slug)
    planning_context = await _get_planning_context_by_slug_or_404(workspace, context_slug)
    payload = body.model_dump(exclude_none=True)
    next_slug = str(payload.get('slug') or '').strip()
    if next_slug and next_slug != planning_context.slug and await sync_to_async(
        PlanningContext.objects.filter(workspace=workspace, slug=next_slug).exclude(pk=planning_context.pk).exists
    )():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='A planning context with this slug already exists in the workspace.',
        )
    try:
        updated_context = await sync_to_async(_update_planning_context_sync)(planning_context.pk, payload)
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_validation_error_detail(exc)) from exc
    return await sync_to_async(_build_planning_context_detail_sync)(updated_context.pk)


@prototype_planning_context_router.post(
    '/{context_slug}/sources',
    response_model=PlanningContextSourceLinkResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_planning_context_source(
    workspace_slug: str,
    context_slug: str,
    body: PlanningContextSourceCreateRequest,
):
    workspace = await _get_workspace_or_404(workspace_slug)
    planning_context = await _get_planning_context_by_slug_or_404(workspace, context_slug)
    workspace_source = await _get_workspace_source_or_404(workspace, body.workspace_source_uuid)
    try:
        link = await sync_to_async(_upsert_planning_context_source_sync)(
            planning_context.pk,
            workspace_source.pk,
            body.model_dump(),
        )
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_validation_error_detail(exc)) from exc
    return PlanningContextSourceLinkResponse(
        uuid=link.uuid,
        workspace_source_uuid=link.workspace_source.uuid,
        title=link.workspace_source.title,
        source_kind=link.workspace_source.source_kind,
        usage_type=link.usage_type,
        is_active=bool(link.is_active),
        include_in_blueprint=bool(link.include_in_blueprint),
        include_in_roadmap_analysis=bool(link.include_in_roadmap_analysis),
        origin='direct',
    )


@prototype_planning_context_router.delete(
    '/{context_slug}/sources/{source_link_uuid}',
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_planning_context_source(
    workspace_slug: str,
    context_slug: str,
    source_link_uuid: UUID,
):
    workspace = await _get_workspace_or_404(workspace_slug)
    planning_context = await _get_planning_context_by_slug_or_404(workspace, context_slug)
    deleted_count = await sync_to_async(
        lambda: PlanningContextSource.objects.filter(
            planning_context=planning_context,
            uuid=source_link_uuid,
        ).delete()[0]
    )()
    if deleted_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Planning context source link not found.',
        )
    return None


@prototype_org_context_router.get('/projects', response_model=ProjectListResponse)
async def list_workspace_projects(workspace_slug: str):
    workspace = await _get_workspace_or_404(workspace_slug)
    projects = await sync_to_async(list)(
        Project.objects.filter(workspace=workspace).order_by('name')
    )
    return ProjectListResponse(
        workspace_slug=workspace.slug,
        projects=[
            ProjectSummaryResponse(
                uuid=project.uuid,
                name=project.name,
            )
            for project in projects
        ],
    )


@prototype_org_context_router.post(
    '/projects',
    response_model=ProjectSummaryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_workspace_project(workspace_slug: str, body: ProjectCreateRequest):
    workspace = await _get_workspace_or_404(workspace_slug)
    normalized_name = body.name.strip()
    if not normalized_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Project name is required.')
    if await sync_to_async(Project.objects.filter(workspace=workspace, name__iexact=normalized_name).exists)():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='A workspace project with this name already exists.',
        )

    try:
        project = await sync_to_async(_create_workspace_project_sync)(workspace.pk, normalized_name)
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_validation_error_detail(exc)) from exc

    return ProjectSummaryResponse(
        uuid=project.uuid,
        name=project.name,
    )


@prototype_org_context_router.get('/summary', response_model=OrgContextSummaryResponse)
async def get_org_context_summary(
    workspace_slug: str,
    planning_context_uuid: UUID | None = None,
):
    workspace = await _get_workspace_or_404(workspace_slug)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)

    employee_count = await sync_to_async(Employee.objects.filter(workspace=workspace).count)()
    org_unit_count = await sync_to_async(OrgUnit.objects.filter(workspace=workspace).count)()
    project_count = await sync_to_async(Project.objects.filter(workspace=workspace).count)()
    reporting_line_count = await sync_to_async(ReportingLine.objects.filter(workspace=workspace).count)()
    parsed_source_count = await sync_to_async(
        ParsedSource.objects.filter(workspace=workspace).filter(source__status=WorkspaceSourceStatus.PARSED).count
    )()
    try:
        from skill_blueprint.services import get_effective_blueprint_run

        latest_blueprint_run = await get_effective_blueprint_run(workspace, planning_context=planning_context)
    except Exception:
        latest_blueprint_run = None

    if latest_blueprint_run is not None:
        role_match_count = await sync_to_async(
            EmployeeRoleMatch.objects.filter(
                workspace=workspace,
                **(
                    {'planning_context': planning_context}
                    if planning_context is not None
                    else {'planning_context__isnull': True}
                ),
                role_profile__blueprint_run=latest_blueprint_run,
            ).count
        )()
    else:
        role_match_count = 0
    skill_evidence_count = await sync_to_async(EmployeeSkillEvidence.objects.filter(workspace=workspace).count)()

    return OrgContextSummaryResponse(
        workspace_slug=workspace.slug,
        employee_count=employee_count,
        org_unit_count=org_unit_count,
        project_count=project_count,
        reporting_line_count=reporting_line_count,
        parsed_source_count=parsed_source_count,
        role_match_count=role_match_count,
        skill_evidence_count=skill_evidence_count,
    )


@prototype_org_context_router.post(
    '/roadmap-analysis/run',
    response_model=RoadmapAnalysisTriggerResponse,
)
async def trigger_roadmap_analysis(
    workspace_slug: str,
    body: RoadmapAnalysisRunRequest | None = None,
    planning_context_uuid: UUID | None = None,
):
    workspace = await _get_workspace_or_404(workspace_slug)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    await assert_workspace_ready_for_stage(workspace, 'roadmap_analysis', planning_context=planning_context)
    body = body or RoadmapAnalysisRunRequest()
    run = await run_roadmap_analysis(
        workspace,
        planning_context=planning_context,
        force_rebuild=body.force_rebuild,
    )
    return RoadmapAnalysisTriggerResponse(
        run_uuid=run.uuid,
        status=run.status,
        message=(
            'Roadmap analysis completed.'
            if run.status == RoadmapAnalysisRun.Status.COMPLETED
            else (
                f'Roadmap analysis failed: {run.error_message}'
                if run.status == RoadmapAnalysisRun.Status.FAILED
                else 'Roadmap analysis started.'
            )
        ),
    )


@prototype_org_context_router.get(
    '/roadmap-analysis/status',
    response_model=RoadmapAnalysisStatusResponse,
)
async def get_roadmap_analysis_status(
    workspace_slug: str,
    planning_context_uuid: UUID | None = None,
):
    workspace = await _get_workspace_or_404(workspace_slug)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    payload = await build_roadmap_analysis_status_payload(workspace, planning_context=planning_context)
    latest_run = payload.get('latest_run')
    return RoadmapAnalysisStatusResponse(
        has_analysis=bool(payload.get('has_analysis')),
        latest_run=RoadmapAnalysisRunSummaryResponse(**latest_run) if latest_run else None,
    )


@prototype_org_context_router.get(
    '/roadmap-analysis/latest',
    response_model=RoadmapAnalysisRunResponse,
)
async def get_latest_roadmap_analysis(
    workspace_slug: str,
    planning_context_uuid: UUID | None = None,
):
    workspace = await _get_workspace_or_404(workspace_slug)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    run = await get_latest_roadmap_analysis_run(
        workspace,
        planning_context=planning_context,
        completed_only=True,
    )
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No completed roadmap analysis found for this workspace.',
        )
    return RoadmapAnalysisRunResponse(**(await build_roadmap_analysis_response(run)))


@prototype_org_context_router.get('/parsed-sources', response_model=ParsedSourceListResponse)
async def list_workspace_parsed_sources(workspace_slug: str):
    workspace = await _get_workspace_or_404(workspace_slug)
    # Use annotation instead of prefetch_related('chunks') to avoid loading
    # all chunk rows into memory just to count them.
    parsed_sources = await sync_to_async(list)(
        ParsedSource.objects.select_related('source', 'source__media_file')
        .annotate(db_chunk_count=Count('chunks'))
        .filter(workspace=workspace)
        .filter(source__status=WorkspaceSourceStatus.PARSED)
        .order_by('-updated_at')
    )
    return ParsedSourceListResponse(
        workspace_slug=workspace.slug,
        parsed_sources=[_build_parsed_source_response(parsed_source) for parsed_source in parsed_sources],
    )


@prototype_org_context_router.get(
    '/parsed-sources/{parsed_source_uuid}',
    response_model=ParsedSourceDetailResponse,
)
async def get_parsed_source_detail(workspace_slug: str, parsed_source_uuid: UUID):
    workspace = await _get_workspace_or_404(workspace_slug)
    parsed_source = await _get_parsed_source_or_404(workspace, parsed_source_uuid)
    source_response: WorkspaceSourceResponse = build_workspace_source_response(parsed_source.source)
    return ParsedSourceDetailResponse(
        workspace_slug=workspace.slug,
        parsed_source=_build_parsed_source_response(parsed_source),
        source=source_response,
        extracted_text=parsed_source.extracted_text,
        chunks=[_build_source_chunk_response(chunk) for chunk in parsed_source.chunks.all()],
    )


@prototype_org_context_router.post(
    '/sources/{source_uuid}/reparse',
    response_model=ParsedSourceReparseResponse,
)
async def reparse_workspace_source(
    workspace_slug: str,
    source_uuid: UUID,
    body: ParsedSourceReparseRequest | None = None,
):
    workspace = await _get_workspace_or_404(workspace_slug)
    source = await _get_workspace_source_or_404(workspace, source_uuid)
    result = await parse_workspace_source(
        source,
        force=True,
        mapping_override=(body.mapping_override if body is not None else None),
    )
    source = await _get_workspace_source_or_404(workspace, source_uuid)
    parsed_source = await sync_to_async(
        ParsedSource.objects.select_related('source', 'source__media_file')
        .prefetch_related('chunks')
        .filter(source=source)
        .first
    )()
    return ParsedSourceReparseResponse(
        workspace_slug=workspace.slug,
        source=build_workspace_source_response(source),
        parsed_source=_build_parsed_source_response(parsed_source) if parsed_source is not None else None,
        status=result['status'],
        parse_error=result.get('parse_error', ''),
        parse_metadata=result.get('parse_metadata', {}) or {},
    )


@prototype_org_context_router.post(
    '/sources/{source_uuid}/csv-preview',
    response_model=OrgCsvPreviewResponse,
)
async def preview_workspace_org_csv_source(
    workspace_slug: str,
    source_uuid: UUID,
    body: OrgCsvPreviewRequest | None = None,
):
    workspace = await _get_workspace_or_404(workspace_slug)
    source = await _get_workspace_source_or_404(workspace, source_uuid)
    body = body or OrgCsvPreviewRequest()
    try:
        preview = await preview_org_csv_source(
            source,
            mapping_override=body.mapping_override,
            sample_row_count=body.sample_row_count,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return OrgCsvPreviewResponse(
        workspace_slug=workspace.slug,
        source_uuid=source.uuid,
        **preview,
    )


@prototype_org_context_router.get('/employees', response_model=EmployeeListResponse)
async def list_workspace_employees(workspace_slug: str):
    workspace = await _get_workspace_or_404(workspace_slug)
    employees = await sync_to_async(list)(
        Employee.objects.filter(workspace=workspace).order_by('full_name')
    )
    return EmployeeListResponse(
        workspace_slug=workspace.slug,
        employees=[
            EmployeeResponse(
                uuid=employee.uuid,
                full_name=employee.full_name,
                email=employee.email,
                current_title=employee.current_title,
                external_employee_id=employee.external_employee_id,
                metadata=employee.metadata,
                cv_availability=EmployeeCvAvailabilityResponse(**((employee.metadata or {}).get('cv_availability') or {})),
            )
            for employee in employees
        ],
    )


@prototype_org_context_router.post('/cv-evidence/build', response_model=CVEvidenceBuildResponse)
async def build_cv_evidence(workspace_slug: str, body: CVEvidenceBuildRequest):
    workspace = await _get_workspace_or_404(workspace_slug)
    result = await build_cv_evidence_for_workspace(
        workspace,
        source_uuids=[str(item) for item in body.source_uuids],
    )
    items = [CVEvidenceSourceResult(**item) for item in result['results']]
    return CVEvidenceBuildResponse(
        workspace_slug=result['workspace_slug'],
        processed=result['processed'],
        rebuilt_count=result['rebuilt_count'],
        reused_count=result['reused_count'],
        status_counts=result['status_counts'],
        results=items,
        employees=items,
    )


@prototype_org_context_router.post('/cv-evidence/rebuild', response_model=CVEvidenceBuildResponse)
async def rebuild_cv_evidence(workspace_slug: str, body: CVEvidenceBuildRequest):
    workspace = await _get_workspace_or_404(workspace_slug)
    result = await rebuild_cv_evidence_for_workspace(
        workspace,
        source_uuids=[str(item) for item in body.source_uuids],
    )
    items = [CVEvidenceSourceResult(**item) for item in result['results']]
    return CVEvidenceBuildResponse(
        workspace_slug=result['workspace_slug'],
        processed=result['processed'],
        rebuilt_count=result['rebuilt_count'],
        reused_count=result['reused_count'],
        status_counts=result['status_counts'],
        results=items,
        employees=items,
    )


@prototype_org_context_router.get('/cv-evidence/status', response_model=CVEvidenceStatusResponse)
async def get_workspace_cv_evidence_status(workspace_slug: str):
    workspace = await _get_workspace_or_404(workspace_slug)
    status_payload = await get_cv_evidence_status(workspace)
    return CVEvidenceStatusResponse(**status_payload)


@prototype_org_context_router.get('/unmatched-cvs', response_model=UnmatchedCVListResponse)
async def get_workspace_unmatched_cvs(workspace_slug: str):
    workspace = await _get_workspace_or_404(workspace_slug)
    items = await list_unmatched_cv_profiles(workspace)
    return UnmatchedCVListResponse(
        workspace_slug=workspace.slug,
        items=[EmployeeCVProfileResponse(**item) for item in items],
    )


@prototype_org_context_router.get('/cv-evidence/review-items', response_model=CVEvidenceReviewListResponse)
async def get_workspace_cv_review_items(workspace_slug: str):
    workspace = await _get_workspace_or_404(workspace_slug)
    items = await list_cv_review_items(workspace)
    return CVEvidenceReviewListResponse(
        workspace_slug=workspace.slug,
        items=[EmployeeCVProfileResponse(**item) for item in items],
    )


@prototype_org_context_router.post(
    '/cv-evidence/sources/{source_uuid}/resolve-match',
    response_model=EmployeeCVProfileResponse,
)
async def resolve_workspace_cv_match(
    workspace_slug: str,
    source_uuid: UUID,
    body: CVMatchResolutionRequest,
):
    workspace = await _get_workspace_or_404(workspace_slug)
    payload = await resolve_cv_profile_match(
        workspace,
        source_uuid,
        employee_uuid=body.employee_uuid,
        operator_name=body.operator_name,
        resolution_note=body.resolution_note,
    )
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='CV profile or employee not found.',
    )
    return EmployeeCVProfileResponse(**payload)


@prototype_org_context_router.post(
    '/cv-evidence/sources/{source_uuid}/approve-pending-skill',
    response_model=EmployeeCVProfileResponse,
)
async def approve_workspace_pending_skill(
    workspace_slug: str,
    source_uuid: UUID,
    body: PendingSkillApprovalRequest,
):
    workspace = await _get_workspace_or_404(workspace_slug)
    try:
        payload = await approve_pending_skill_candidate(
            workspace,
            source_uuid,
            candidate_key=body.candidate_key,
            approved_name_en=body.approved_name_en,
            approved_name_ru=body.approved_name_ru,
            alias_terms=body.alias_terms,
            operator_name=body.operator_name,
            approval_note=body.approval_note,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='CV profile not found.',
        )
    return EmployeeCVProfileResponse(**payload)


@prototype_org_context_router.post(
    '/employees/{employee_uuid}/skills/bulk-review',
    response_model=EmployeeSkillBulkReviewResponse,
)
async def bulk_review_employee_skill_evidence(
    workspace_slug: str,
    employee_uuid: UUID,
    body: EmployeeSkillBulkReviewRequest,
):
    workspace = await _get_workspace_or_404(workspace_slug)
    await _get_employee_or_404(workspace, employee_uuid)
    try:
        payload = await review_employee_skills_bulk(
            workspace,
            employee_uuid,
            actions=[item.model_dump(mode='json') for item in body.actions],
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return EmployeeSkillBulkReviewResponse(**payload)


@prototype_org_context_router.post(
    '/employees/{employee_uuid}/skills/accept-all',
    response_model=EmployeeSkillAcceptAllResponse,
)
async def accept_high_confidence_employee_skills(
    workspace_slug: str,
    employee_uuid: UUID,
    body: EmployeeSkillAcceptAllRequest,
):
    workspace = await _get_workspace_or_404(workspace_slug)
    await _get_employee_or_404(workspace, employee_uuid)
    try:
        payload = await accept_employee_high_confidence_skills(
            workspace,
            employee_uuid,
            confidence_threshold=body.confidence_threshold,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return EmployeeSkillAcceptAllResponse(**payload)


@prototype_org_context_router.get(
    '/skills/pending-review',
    response_model=WorkspacePendingSkillsResponse,
)
async def list_workspace_pending_skill_queue(workspace_slug: str):
    workspace = await _get_workspace_or_404(workspace_slug)
    pending_skills = await get_workspace_pending_skills(workspace)
    total_resolved = await sync_to_async(
        Skill.objects.filter(
            workspace=workspace,
            resolution_status=Skill.ResolutionStatus.RESOLVED,
        ).count
    )()
    return WorkspacePendingSkillsResponse(
        pending_skills=[PendingWorkspaceSkillResponse(**item) for item in pending_skills],
        total_pending=len(pending_skills),
        total_resolved=total_resolved,
    )


@prototype_org_context_router.post(
    '/skills/bulk-resolve',
    response_model=WorkspaceSkillResolutionResponse,
)
async def bulk_resolve_workspace_skill_queue(
    workspace_slug: str,
    body: WorkspaceSkillResolutionRequest,
):
    workspace = await _get_workspace_or_404(workspace_slug)
    try:
        payload = await resolve_workspace_skills_bulk(
            workspace,
            resolutions=[item.model_dump(mode='json') for item in body.resolutions],
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return WorkspaceSkillResolutionResponse(**payload)


@prototype_org_context_router.get(
    '/employees-without-cv-evidence',
    response_model=EmployeesWithoutCVEvidenceListResponse,
)
async def get_workspace_employees_without_cv_evidence(workspace_slug: str):
    workspace = await _get_workspace_or_404(workspace_slug)
    items = await list_employees_without_cv_evidence(workspace)
    return EmployeesWithoutCVEvidenceListResponse(
        workspace_slug=workspace.slug,
        items=[EmployeeWithoutCVEvidenceResponse(**item) for item in items],
    )


@prototype_org_context_router.get('/employees/{employee_uuid}/evidence', response_model=EmployeeEvidenceDetailResponse)
async def get_employee_evidence_detail(workspace_slug: str, employee_uuid: UUID):
    workspace = await _get_workspace_or_404(workspace_slug)
    employee = await _get_employee_or_404(workspace, employee_uuid)
    payload = await get_employee_cv_evidence_detail(workspace, employee.uuid)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Employee evidence not found.',
        )
    return EmployeeEvidenceDetailResponse(
        workspace_slug=workspace.slug,
        employee_uuid=employee.uuid,
        full_name=payload['full_name'],
        current_title=payload['current_title'],
        external_employee_id=payload.get('external_employee_id') or '',
        metadata=payload.get('metadata') or {},
        cv_availability=EmployeeCvAvailabilityResponse(**(payload.get('cv_availability') or {})),
        coverage_gap=EmployeeCoverageGapResponse(**payload['coverage_gap']) if payload.get('coverage_gap') else None,
        cv_profiles=[EmployeeCVProfileResponse(**item) for item in payload['cv_profiles']],
        candidate_cv_profiles=[EmployeeCVProfileResponse(**item) for item in payload['candidate_cv_profiles']],
        evidence_rows=[EmployeeSkillEvidenceResponse(**item) for item in payload['evidence_rows']],
    )


@prototype_org_context_router.post(
    '/employees/{employee_uuid}/mark-no-cv',
    response_model=EmployeeCvAvailabilityResponse,
)
async def mark_workspace_employee_no_cv(
    workspace_slug: str,
    employee_uuid: UUID,
    body: EmployeeCvAvailabilityRequest,
):
    workspace = await _get_workspace_or_404(workspace_slug)
    try:
        payload = await mark_employee_no_cv_available(
            workspace,
            employee_uuid,
            operator_name=body.operator_name,
            note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Employee not found.',
        )
    return EmployeeCvAvailabilityResponse(**payload)


@prototype_org_context_router.post(
    '/employees/{employee_uuid}/clear-no-cv',
    response_model=EmployeeCvAvailabilityResponse,
)
async def clear_workspace_employee_no_cv(
    workspace_slug: str,
    employee_uuid: UUID,
):
    workspace = await _get_workspace_or_404(workspace_slug)
    payload = await clear_employee_no_cv_available(workspace, employee_uuid)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Employee not found.',
        )
    return EmployeeCvAvailabilityResponse(**payload)


@prototype_org_context_router.delete(
    '/employees/{employee_uuid}',
    response_model=EmployeeDeleteResponse,
)
async def delete_workspace_employee_view(workspace_slug: str, employee_uuid: UUID):
    workspace = await _get_workspace_or_404(workspace_slug)
    payload = await delete_workspace_employee(workspace, employee_uuid)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Employee not found.',
        )
    return EmployeeDeleteResponse(workspace_slug=workspace.slug, **payload)


@prototype_org_context_router.get('/role-matches', response_model=EmployeeRoleMatchListResponse)
async def list_employee_role_matches(
    workspace_slug: str,
    planning_context_uuid: UUID | None = None,
):
    workspace = await _get_workspace_or_404(workspace_slug)
    planning_context = await _get_planning_context_or_404(workspace, planning_context_uuid)
    try:
        from skill_blueprint.services import get_effective_blueprint_run

        latest_blueprint_run = await get_effective_blueprint_run(workspace, planning_context=planning_context)
    except Exception:
        latest_blueprint_run = None

    employees = await sync_to_async(list)(Employee.objects.filter(workspace=workspace).order_by('full_name'))
    payload = []
    for employee in employees:
        if latest_blueprint_run is None:
            matches = []
        else:
            matches = await sync_to_async(list)(
                EmployeeRoleMatch.objects.filter(
                    employee=employee,
                    **(
                        {'planning_context': planning_context}
                        if planning_context is not None
                        else {'planning_context__isnull': True}
                    ),
                    role_profile__blueprint_run=latest_blueprint_run,
                )
                .select_related('role_profile')
                .order_by('-fit_score', 'role_profile__name')
            )
        payload.append(
            EmployeeRoleMatchResponse(
                employee_uuid=employee.uuid,
                full_name=employee.full_name,
                matches=[
                    {
                        'role_name': match.role_profile.name,
                        'seniority': match.role_profile.seniority,
                        'fit_score': float(match.fit_score),
                        'reason': match.rationale,
                        'related_initiatives': match.related_initiatives,
                    }
                    for match in matches
                ],
            )
        )
    return EmployeeRoleMatchListResponse(
        workspace_slug=workspace.slug,
        employees=payload,
    )
