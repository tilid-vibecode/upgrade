from __future__ import annotations

from copy import deepcopy
import json
import logging
from urllib.parse import quote
import uuid as uuid_mod
from collections import defaultdict
from typing import Any, Optional

from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.text import slugify

from company_intake.models import IntakeWorkspace
from employee_assessment.models import AssessmentPackStatus, EmployeeAssessmentPack
from evidence_matrix.models import EvidenceMatrixRun, EvidenceMatrixStatus
from evidence_matrix.services import get_current_completed_matrix_run
from media_storage.constants import SIGNED_URL_EXPIRY_SECONDS
from media_storage.services import (
    generate_signed_url_for_file,
    store_prototype_generated_text_artifact,
)
from org_context.models import Employee
from org_context.vector_indexing import (
    retrieve_employee_fused_evidence_sync,
    retrieve_workspace_evidence_sync,
)
from server.embedding_manager import get_embedding_manager_sync
from skill_blueprint.models import SkillBlueprintRun
from skill_blueprint.services import get_current_published_blueprint_run
from tools.openai.structured_client import call_openai_structured

from .models import ArtifactFormat, DevelopmentPlanArtifact, DevelopmentPlanRun, PlanRunStatus, PlanScope
from .renderers import ARTIFACT_VERSION as PLAN_ARTIFACT_VERSION
from .renderers import render_plan_artifact

logger = logging.getLogger(__name__)

PLAN_VERSION = 'stage9-v1'
DEFAULT_ARTIFACT_FORMATS = [ArtifactFormat.JSON, ArtifactFormat.MARKDOWN, ArtifactFormat.HTML]
MAX_TEAM_PRIORITY_ACTIONS = 8
MAX_INDIVIDUAL_ACTIONS = 3
MAX_CONTEXT_SNIPPETS = 3
HIGH_PRIORITY_THRESHOLD = 4
SEVERE_GAP_THRESHOLD = 1.5
DEVELOPABLE_GAP_THRESHOLD = 1.0
MOVE_GAP_THRESHOLD = 1.25
GOOD_CONFIDENCE_THRESHOLD = 0.55
STAGE9_CV_DOC_TYPES = ['cv_skill_evidence', 'cv_role_history']
STAGE9_SELF_ASSESSMENT_DOC_TYPES = [
    'self_assessment_skill_evidence',
    'self_assessment_example',
]

TEAM_PLAN_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'executive_summary': {'type': 'string'},
        'roadmap_priority_note': {'type': 'string'},
        'priority_actions': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'action_key': {'type': 'string'},
                    'why_now': {'type': 'string'},
                    'manager_note': {'type': 'string'},
                },
                'required': ['action_key', 'why_now', 'manager_note'],
            },
        },
        'hiring_recommendations': {'type': 'array', 'items': {'type': 'string'}},
        'development_focus': {'type': 'array', 'items': {'type': 'string'}},
        'single_points_of_failure': {'type': 'array', 'items': {'type': 'string'}},
    },
    'required': [
        'executive_summary',
        'roadmap_priority_note',
        'priority_actions',
        'hiring_recommendations',
        'development_focus',
        'single_points_of_failure',
    ],
}

INDIVIDUAL_PLAN_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'current_role_fit': {'type': 'string'},
        'adjacent_roles': {'type': 'array', 'items': {'type': 'string'}},
        'strengths': {'type': 'array', 'items': {'type': 'string'}},
        'priority_gaps': {'type': 'array', 'items': {'type': 'string'}},
        'development_actions': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'action_key': {'type': 'string'},
                    'action': {'type': 'string'},
                    'time_horizon': {'type': 'string'},
                    'expected_outcome': {'type': 'string'},
                    'coach_note': {'type': 'string'},
                },
                'required': ['action_key', 'action', 'time_horizon', 'expected_outcome', 'coach_note'],
            },
        },
        'roadmap_alignment': {'type': 'string'},
        'mobility_note': {'type': 'string'},
    },
    'required': [
        'current_role_fit',
        'adjacent_roles',
        'strengths',
        'priority_gaps',
        'development_actions',
        'roadmap_alignment',
        'mobility_note',
    ],
}


def _plan_context_filter_kwargs(planning_context_pk=None) -> dict[str, Any]:
    if planning_context_pk is not None:
        return {'planning_context_id': planning_context_pk}
    return {'planning_context__isnull': True}


async def generate_development_plans(workspace, *, planning_context=None, team_title: str = 'Final development plan') -> dict:
    blueprint, matrix = await _resolve_current_plan_inputs(workspace, planning_context=planning_context)
    if blueprint is None or matrix is None:
        raise ValueError(
            'A published blueprint and a completed evidence matrix run are required before generating development plans.'
        )

    generation_batch_uuid = uuid_mod.uuid4()
    employee_payloads, duplicate_employee_records = _prepare_employee_payloads_for_batch(
        list(matrix.matrix_payload.get('employees') or [])
    )
    team_recommendation_payload = await sync_to_async(_build_team_recommendation_payload_sync)(
        workspace.pk,
        blueprint.pk,
        matrix.pk,
    )
    team_run = await sync_to_async(DevelopmentPlanRun.objects.create)(
        workspace=workspace,
        planning_context=planning_context,
        blueprint_run=blueprint,
        matrix_run=matrix,
        title=team_title,
        scope=PlanScope.TEAM,
        status=PlanRunStatus.RUNNING,
        generation_batch_uuid=generation_batch_uuid,
        plan_version=PLAN_VERSION,
        input_snapshot=_build_input_snapshot(team_recommendation_payload, generation_batch_uuid),
        recommendation_payload=team_recommendation_payload,
    )
    try:
        team_plan_payload = await _generate_team_plan_payload(blueprint, matrix, team_recommendation_payload)
        team_artifact = await _upload_generated_plan_artifact(
            workspace=workspace,
            workspace_slug=workspace.slug,
            filename='team-development-plan.json',
            payload=team_plan_payload,
            description='Generated team development plan',
        )
        await sync_to_async(_finalize_plan_run_sync)(
            team_run.pk,
            team_plan_payload,
            team_artifact,
            team_recommendation_payload,
            _build_team_run_summary(
                team_recommendation_payload,
                team_artifact,
                expected_employee_count=len(employee_payloads),
            ),
        )
    except Exception as exc:
        logger.exception('Team development plan generation failed for workspace %s', workspace.slug)
        await sync_to_async(_fail_plan_run_sync)(team_run.pk, str(exc))
        raise

    individual_runs: list[DevelopmentPlanRun] = []
    missing_employee_records: list[dict[str, Any]] = list(duplicate_employee_records)
    expected_employee_uuids = [
        str(item.get('employee_uuid') or '').strip()
        for item in employee_payloads
        if str(item.get('employee_uuid') or '').strip()
    ]
    try:
        for employee_payload in employee_payloads:
            employee = await sync_to_async(
                lambda: Employee.objects.filter(workspace=workspace, uuid=employee_payload.get('employee_uuid')).first()
            )()
            if employee is None:
                missing_employee_records.append(
                    {
                        'employee_uuid': str(employee_payload.get('employee_uuid') or '').strip(),
                        'full_name': str(employee_payload.get('full_name') or '').strip(),
                        'reason': 'Employee record missing while generating Stage 9 plans.',
                    }
                )
                continue
            run = await sync_to_async(DevelopmentPlanRun.objects.create)(
                workspace=workspace,
                planning_context=planning_context,
                employee=employee,
                blueprint_run=blueprint,
                matrix_run=matrix,
                title=f'{employee.full_name} PDP',
                scope=PlanScope.INDIVIDUAL,
                status=PlanRunStatus.RUNNING,
                generation_batch_uuid=generation_batch_uuid,
                plan_version=PLAN_VERSION,
                input_snapshot={'generation_batch_uuid': str(generation_batch_uuid)},
                recommendation_payload={},
            )
            try:
                recommendation_payload = await sync_to_async(_build_individual_recommendation_payload_sync)(
                    workspace.pk,
                    blueprint.pk,
                    matrix.pk,
                    employee.pk,
                    employee_payload,
                )
                await sync_to_async(_update_plan_run_inputs_sync)(
                    run.pk,
                    _build_input_snapshot(recommendation_payload, generation_batch_uuid),
                    recommendation_payload,
                )
                individual_payload = await _generate_individual_plan_payload(
                    blueprint,
                    matrix,
                    employee,
                    employee_payload,
                    recommendation_payload=recommendation_payload,
                )
                artifact = await _upload_generated_plan_artifact(
                    workspace=workspace,
                    workspace_slug=workspace.slug,
                    filename=f'{employee.full_name}-pdp.json'.replace('/', '-'),
                    payload=individual_payload,
                    description=f'Generated PDP for {employee.full_name}',
                )
                await sync_to_async(_finalize_plan_run_sync)(
                    run.pk,
                    individual_payload,
                    artifact,
                    recommendation_payload,
                    _build_individual_run_summary(recommendation_payload, artifact),
                )
            except Exception as exc:
                logger.exception(
                    'Individual development plan generation failed for workspace %s employee %s',
                    workspace.slug,
                    employee.uuid,
                )
                await sync_to_async(_fail_plan_run_sync)(run.pk, str(exc))
            individual_runs.append(
                await sync_to_async(
                    lambda: DevelopmentPlanRun.objects.select_related(
                        'workspace', 'employee', 'blueprint_run', 'matrix_run'
                    ).get(pk=run.pk)
                )()
            )
    finally:
        batch_summary = await sync_to_async(_finalize_generation_batch_sync)(
            workspace.pk,
            generation_batch_uuid,
            expected_employee_uuids,
            missing_employee_records,
            getattr(planning_context, 'pk', None),
        )
        team_run = await sync_to_async(
            lambda: DevelopmentPlanRun.objects.select_related(
                'workspace', 'employee', 'blueprint_run', 'matrix_run'
            ).get(pk=team_run.pk)
        )()
        individual_runs = await sync_to_async(_list_batch_individual_runs_sync)(generation_batch_uuid)

    return {
        'team_plan': team_run,
        'individual_plans': individual_runs,
        'batch_summary': batch_summary,
    }


async def get_latest_team_plan(workspace, *, planning_context=None) -> Optional[DevelopmentPlanRun]:
    return await sync_to_async(
        lambda: _order_plan_queryset(
            DevelopmentPlanRun.objects.select_related('workspace', 'employee', 'blueprint_run', 'matrix_run').filter(
                workspace=workspace,
                scope=PlanScope.TEAM,
                **_plan_context_filter_kwargs(getattr(planning_context, 'pk', None)),
            )
        ).first()
    )()


async def get_current_team_plan(workspace, *, planning_context=None) -> Optional[DevelopmentPlanRun]:
    current_run = await sync_to_async(
        lambda: _order_plan_queryset(
            DevelopmentPlanRun.objects.select_related('workspace', 'employee', 'blueprint_run', 'matrix_run').filter(
                workspace=workspace,
                scope=PlanScope.TEAM,
                status=PlanRunStatus.COMPLETED,
                is_current=True,
                **_plan_context_filter_kwargs(getattr(planning_context, 'pk', None)),
            )
        ).first()
    )()
    if current_run is not None:
        return current_run
    blueprint, matrix = await _resolve_current_plan_inputs(workspace, planning_context=planning_context)
    if blueprint is None or matrix is None:
        return await sync_to_async(_get_latest_completed_team_plan_sync)(
            workspace.pk,
            getattr(planning_context, 'pk', None),
        )
    fallback_run = await sync_to_async(
        lambda: _order_plan_queryset(
            DevelopmentPlanRun.objects.select_related('workspace', 'employee', 'blueprint_run', 'matrix_run').filter(
                workspace=workspace,
                scope=PlanScope.TEAM,
                status=PlanRunStatus.COMPLETED,
                blueprint_run=blueprint,
                matrix_run=matrix,
                **_plan_context_filter_kwargs(getattr(planning_context, 'pk', None)),
            )
        ).first()
    )()
    if fallback_run is not None:
        return fallback_run
    return await sync_to_async(_get_latest_completed_team_plan_sync)(
        workspace.pk,
        getattr(planning_context, 'pk', None),
    )


async def list_latest_individual_plans(workspace, *, planning_context=None) -> list[DevelopmentPlanRun]:
    planning_context_pk = getattr(planning_context, 'pk', None)
    team_run = await get_latest_team_plan(workspace, planning_context=planning_context)
    if team_run is None:
        return []
    if team_run.generation_batch_uuid:
        return await sync_to_async(_list_latest_individual_plans_sync)(
            workspace.pk,
            team_run.generation_batch_uuid,
            planning_context_pk,
        )
    if team_run.blueprint_run_id and team_run.matrix_run_id:
        return await sync_to_async(_list_latest_individual_plans_for_lineage_sync)(
            workspace.pk,
            team_run.blueprint_run_id,
            team_run.matrix_run_id,
            planning_context_pk,
        )
    return await sync_to_async(_list_latest_completed_individual_plans_sync)(workspace.pk, planning_context_pk)


async def list_current_individual_plans(workspace, *, planning_context=None) -> list[DevelopmentPlanRun]:
    planning_context_pk = getattr(planning_context, 'pk', None)
    current_runs = await sync_to_async(
        lambda: list(
            DevelopmentPlanRun.objects.filter(
                workspace=workspace,
                scope=PlanScope.INDIVIDUAL,
                status=PlanRunStatus.COMPLETED,
                is_current=True,
                **_plan_context_filter_kwargs(planning_context_pk),
            )
            .select_related('workspace', 'employee', 'blueprint_run', 'matrix_run')
            .order_by('employee__full_name', '-completed_at', '-created_at')
        )
    )()
    if current_runs:
        return current_runs
    blueprint, matrix = await _resolve_current_plan_inputs(workspace, planning_context=planning_context)
    if blueprint is None or matrix is None:
        return await sync_to_async(_list_latest_completed_individual_plans_sync)(workspace.pk, planning_context_pk)
    lineage_runs = await sync_to_async(_list_latest_individual_plans_for_lineage_sync)(
        workspace.pk,
        blueprint.pk,
        matrix.pk,
        planning_context_pk,
    )
    if lineage_runs:
        return lineage_runs
    return await sync_to_async(_list_latest_completed_individual_plans_sync)(workspace.pk, planning_context_pk)


async def get_latest_individual_plan(workspace, employee_uuid: str, *, planning_context=None) -> Optional[DevelopmentPlanRun]:
    planning_context_pk = getattr(planning_context, 'pk', None)
    team_run = await get_latest_team_plan(workspace, planning_context=planning_context)
    if team_run is None:
        return None
    if team_run.generation_batch_uuid:
        return await sync_to_async(_get_latest_individual_plan_sync)(
            workspace.pk,
            employee_uuid,
            team_run.generation_batch_uuid,
            planning_context_pk,
        )
    if team_run.blueprint_run_id and team_run.matrix_run_id:
        return await sync_to_async(_get_latest_individual_plan_for_lineage_sync)(
            workspace.pk,
            employee_uuid,
            team_run.blueprint_run_id,
            team_run.matrix_run_id,
            planning_context_pk,
        )
    return await sync_to_async(_get_latest_completed_individual_plan_sync)(workspace.pk, employee_uuid, planning_context_pk)


async def get_current_individual_plan(workspace, employee_uuid: str, *, planning_context=None) -> Optional[DevelopmentPlanRun]:
    planning_context_pk = getattr(planning_context, 'pk', None)
    current_run = await sync_to_async(
        lambda: _order_plan_queryset(
            DevelopmentPlanRun.objects.select_related('workspace', 'employee', 'blueprint_run', 'matrix_run').filter(
                workspace=workspace,
                scope=PlanScope.INDIVIDUAL,
                status=PlanRunStatus.COMPLETED,
                is_current=True,
                employee__uuid=employee_uuid,
                **_plan_context_filter_kwargs(planning_context_pk),
            )
        ).first()
    )()
    if current_run is not None:
        return current_run
    blueprint, matrix = await _resolve_current_plan_inputs(workspace, planning_context=planning_context)
    if blueprint is None or matrix is None:
        return await sync_to_async(_get_latest_completed_individual_plan_sync)(workspace.pk, employee_uuid, planning_context_pk)
    lineage_run = await sync_to_async(_get_latest_individual_plan_for_lineage_sync)(
        workspace.pk,
        employee_uuid,
        blueprint.pk,
        matrix.pk,
        planning_context_pk,
    )
    if lineage_run is not None:
        return lineage_run
    return await sync_to_async(_get_latest_completed_individual_plan_sync)(workspace.pk, employee_uuid, planning_context_pk)


async def get_latest_plan_summary(workspace, *, planning_context=None) -> dict[str, Any]:
    team_plan = await get_latest_team_plan(workspace, planning_context=planning_context)
    individual_plans = await list_latest_individual_plans(workspace, planning_context=planning_context)
    return _build_plan_summary_payload(workspace.slug, team_plan, individual_plans)


async def get_current_plan_summary(workspace, *, planning_context=None) -> dict[str, Any]:
    team_plan = await get_current_team_plan(workspace, planning_context=planning_context)
    individual_plans = await list_current_individual_plans(workspace, planning_context=planning_context)
    return _build_plan_summary_payload(workspace.slug, team_plan, individual_plans)


async def get_latest_team_actions(workspace, *, planning_context=None) -> Optional[dict[str, Any]]:
    run = await get_latest_team_plan(workspace, planning_context=planning_context)
    if run is None:
        return None
    return {
        'priority_actions': list((run.plan_payload or {}).get('priority_actions') or []),
        'action_counts': dict((run.recommendation_payload or {}).get('action_counts') or {}),
        'generation_batch_uuid': str(run.generation_batch_uuid) if run.generation_batch_uuid else '',
        'is_current': bool(run.is_current),
    }


async def get_current_team_actions(workspace, *, planning_context=None) -> Optional[dict[str, Any]]:
    run = await get_current_team_plan(workspace, planning_context=planning_context)
    if run is None:
        return None
    return {
        'priority_actions': list((run.plan_payload or {}).get('priority_actions') or []),
        'action_counts': dict((run.recommendation_payload or {}).get('action_counts') or {}),
        'generation_batch_uuid': str(run.generation_batch_uuid) if run.generation_batch_uuid else '',
        'is_current': bool(run.is_current),
    }


async def build_plan_response(run: DevelopmentPlanRun) -> dict:
    return {
        'uuid': run.uuid,
        'workspace_uuid': run.workspace.uuid,
        'employee_uuid': run.employee.uuid if run.employee_id else None,
        'blueprint_run_uuid': getattr(run.blueprint_run, 'uuid', None),
        'matrix_run_uuid': getattr(run.matrix_run, 'uuid', None),
        'planning_context_uuid': run.planning_context_id,
        'generation_batch_uuid': run.generation_batch_uuid,
        'title': run.title,
        'scope': run.scope,
        'status': run.status,
        'is_current': run.is_current,
        'plan_version': run.plan_version,
        'input_snapshot': run.input_snapshot or {},
        'recommendation_payload': run.recommendation_payload or {},
        'final_report_key': run.final_report_key,
        'summary': run.summary,
        'plan_payload': run.plan_payload,
        'created_at': run.created_at,
        'completed_at': run.completed_at,
        'updated_at': run.updated_at,
    }


async def build_plan_slice_response(run: DevelopmentPlanRun, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        'plan_uuid': run.uuid,
        'scope': run.scope,
        'title': run.title,
        'payload': payload or {},
        'updated_at': run.updated_at,
    }


async def build_plan_artifact_response(artifact: DevelopmentPlanArtifact) -> dict[str, Any]:
    original_filename = artifact.media_file.original_filename or 'development-plan'
    ascii_fallback = slugify(original_filename.rsplit('.', 1)[0]) or 'development-plan'
    extension = ''
    if '.' in original_filename:
        extension = f".{original_filename.rsplit('.', 1)[1]}"
    safe_filename = f'{ascii_fallback}{extension}'
    content_disposition = (
        f'attachment; filename="{safe_filename}"; filename*=UTF-8\'\'{quote(original_filename)}'
    )
    try:
        signed_url = await generate_signed_url_for_file(
            artifact.media_file,
            response_content_disposition=content_disposition,
            response_content_type=artifact.media_file.content_type or None,
        )
    except Exception as exc:
        logger.warning(
            'Failed to sign generated artifact %s for workspace %s: %s',
            artifact.uuid,
            artifact.workspace.slug,
            exc,
            exc_info=True,
        )
        signed_url = None
    return {
        'uuid': artifact.uuid,
        'workspace_uuid': artifact.workspace.uuid,
        'plan_run_uuid': artifact.plan_run.uuid,
        'employee_uuid': artifact.employee.uuid if artifact.employee_id else None,
        'blueprint_run_uuid': getattr(artifact.blueprint_run, 'uuid', None),
        'matrix_run_uuid': getattr(artifact.matrix_run, 'uuid', None),
        'planning_context_uuid': getattr(artifact.plan_run, 'planning_context_id', None),
        'generation_batch_uuid': artifact.generation_batch_uuid,
        'artifact_scope': artifact.artifact_scope,
        'artifact_format': artifact.artifact_format,
        'artifact_version': artifact.artifact_version,
        'is_current': artifact.is_current,
        'title': artifact.plan_run.title,
        'metadata': artifact.metadata or {},
        'file_uuid': artifact.media_file.uuid,
        'original_filename': original_filename,
        'content_type': artifact.media_file.content_type,
        'file_size': artifact.media_file.file_size,
        'signed_url': signed_url,
        'expires_in_seconds': SIGNED_URL_EXPIRY_SECONDS if signed_url else None,
        'source_run_completed_at': _run_effective_completed_at(artifact.plan_run),
        'created_at': artifact.created_at,
        'updated_at': artifact.updated_at,
    }


async def build_plan_artifact_bundle_response(
    run: DevelopmentPlanRun,
    artifacts: list[DevelopmentPlanArtifact],
    *,
    selected_as_current: bool = False,
) -> dict[str, Any]:
    return {
        'workspace_slug': run.workspace.slug,
        'plan_uuid': run.uuid,
        'employee_uuid': run.employee.uuid if run.employee_id else None,
        'generation_batch_uuid': run.generation_batch_uuid,
        'scope': run.scope,
        'title': run.title,
        'status': run.status,
        'is_current': bool(run.is_current),
        'selected_as_current': bool(selected_as_current),
        'artifacts': [
            await build_plan_artifact_response(artifact)
            for artifact in artifacts
        ],
        'updated_at': run.updated_at,
    }


async def build_workspace_artifact_list_response(
    workspace,
    artifacts: list[DevelopmentPlanArtifact],
) -> dict[str, Any]:
    return {
        'workspace_slug': workspace.slug,
        'artifacts': [await build_plan_artifact_response(artifact) for artifact in artifacts],
        'total': len(artifacts),
    }


async def get_latest_team_plan_artifact_bundle(workspace, *, planning_context=None) -> Optional[dict[str, Any]]:
    run = await sync_to_async(_get_latest_completed_team_plan_sync)(workspace.pk, getattr(planning_context, 'pk', None))
    if run is None:
        return None
    return await _build_plan_artifact_bundle_for_run(run)


async def get_current_team_plan_artifact_bundle(workspace, *, planning_context=None) -> Optional[dict[str, Any]]:
    run = await get_current_team_plan(workspace, planning_context=planning_context)
    if run is None:
        return None
    return await _build_plan_artifact_bundle_for_run(run, selected_as_current=True)


async def get_latest_individual_plan_artifact_bundle(
    workspace,
    employee_uuid: str,
    *,
    planning_context=None,
) -> Optional[dict[str, Any]]:
    planning_context_pk = getattr(planning_context, 'pk', None)
    team_run = await sync_to_async(_get_latest_completed_team_plan_sync)(workspace.pk, planning_context_pk)
    if team_run is None:
        return None
    if team_run.generation_batch_uuid:
        run = await sync_to_async(_get_latest_individual_plan_sync)(
            workspace.pk,
            employee_uuid,
            team_run.generation_batch_uuid,
            planning_context_pk,
        )
    elif team_run.blueprint_run_id and team_run.matrix_run_id:
        run = await sync_to_async(_get_latest_individual_plan_for_lineage_sync)(
            workspace.pk,
            employee_uuid,
            team_run.blueprint_run_id,
            team_run.matrix_run_id,
            planning_context_pk,
        )
    else:
        run = await sync_to_async(_get_latest_completed_individual_plan_sync)(workspace.pk, employee_uuid, planning_context_pk)
    if run is None:
        return None
    return await _build_plan_artifact_bundle_for_run(run)


async def get_current_individual_plan_artifact_bundle(
    workspace,
    employee_uuid: str,
    *,
    planning_context=None,
) -> Optional[dict[str, Any]]:
    run = await get_current_individual_plan(workspace, employee_uuid, planning_context=planning_context)
    if run is None:
        return None
    return await _build_plan_artifact_bundle_for_run(run, selected_as_current=True)


async def list_workspace_plan_artifacts(workspace, *, planning_context=None) -> list[DevelopmentPlanArtifact]:
    planning_context_pk = getattr(planning_context, 'pk', None)
    runs = await sync_to_async(
        lambda: list(
            _order_plan_queryset(
                DevelopmentPlanRun.objects.select_related('workspace', 'employee', 'blueprint_run', 'matrix_run').filter(
                    workspace=workspace,
                    status=PlanRunStatus.COMPLETED,
                    **_plan_context_filter_kwargs(planning_context_pk),
                )
            )
        )
    )()
    for run in runs:
        await ensure_plan_export_artifacts(run)
    return await sync_to_async(
        lambda: list(
            DevelopmentPlanArtifact.objects.select_related(
                'workspace', 'plan_run', 'employee', 'blueprint_run', 'matrix_run', 'media_file'
            )
            .filter(
                workspace=workspace,
                **(
                    {'plan_run__planning_context_id': planning_context_pk}
                    if planning_context_pk is not None
                    else {'plan_run__planning_context__isnull': True}
                ),
            )
            .order_by('-plan_run__completed_at', '-plan_run__updated_at', '-created_at')
        )
    )()


async def list_latest_workspace_plan_artifacts(workspace, *, planning_context=None) -> list[DevelopmentPlanArtifact]:
    planning_context_pk = getattr(planning_context, 'pk', None)
    team_run = await sync_to_async(_get_latest_completed_team_plan_sync)(workspace.pk, planning_context_pk)
    if team_run is None:
        return []
    if team_run.generation_batch_uuid:
        individual_runs = await sync_to_async(_list_latest_individual_plans_sync)(
            workspace.pk,
            team_run.generation_batch_uuid,
            planning_context_pk,
        )
    elif team_run.blueprint_run_id and team_run.matrix_run_id:
        individual_runs = await sync_to_async(_list_latest_individual_plans_for_lineage_sync)(
            workspace.pk,
            team_run.blueprint_run_id,
            team_run.matrix_run_id,
            planning_context_pk,
        )
    else:
        individual_runs = await sync_to_async(_list_latest_completed_individual_plans_sync)(workspace.pk, planning_context_pk)
    runs = [team_run, *individual_runs]
    return await _ensure_and_list_artifacts_for_runs(runs)


async def ensure_plan_export_artifacts(run: DevelopmentPlanRun) -> list[DevelopmentPlanArtifact]:
    if run.status != PlanRunStatus.COMPLETED:
        return []
    refreshed_run = await sync_to_async(
        lambda: DevelopmentPlanRun.objects.select_related(
            'workspace', 'employee', 'blueprint_run', 'matrix_run'
        ).get(pk=run.pk)
    )()
    if not refreshed_run.export_snapshot:
        await sync_to_async(_persist_plan_export_snapshot_sync)(refreshed_run.pk)
        refreshed_run = await sync_to_async(
            lambda: DevelopmentPlanRun.objects.select_related(
                'workspace', 'employee', 'blueprint_run', 'matrix_run'
            ).get(pk=run.pk)
        )()
    existing_artifacts = await sync_to_async(
        lambda: list(
            DevelopmentPlanArtifact.objects.select_related(
                'workspace', 'plan_run', 'employee', 'blueprint_run', 'matrix_run', 'media_file'
            )
            .filter(plan_run=refreshed_run)
            .order_by('artifact_format')
        )
    )()
    existing_formats = {artifact.artifact_format for artifact in existing_artifacts}
    missing_formats = [fmt for fmt in DEFAULT_ARTIFACT_FORMATS if fmt not in existing_formats]
    for artifact_format in missing_formats:
        generated_at = timezone.now().isoformat()
        render_result = render_plan_artifact(
            refreshed_run,
            artifact_format=artifact_format,
            generated_at=generated_at,
            frozen_snapshot=dict(refreshed_run.export_snapshot or {}),
        )
        filename = _build_plan_export_filename(
            refreshed_run,
            extension=render_result['extension'],
            artifact_format=artifact_format,
        )
        media_file = await store_prototype_generated_text_artifact(
            scope=f'{refreshed_run.workspace.slug}/exports',
            filename=filename,
            content=render_result['content'],
            content_type=render_result['content_type'],
            description=_build_plan_export_description(refreshed_run, artifact_format),
            metadata={
                'plan_run_uuid': str(refreshed_run.uuid),
                'artifact_format': artifact_format,
                'artifact_version': PLAN_ARTIFACT_VERSION,
                'generation_batch_uuid': str(refreshed_run.generation_batch_uuid or ''),
                'export_generated_at': generated_at,
            },
            prototype_workspace=refreshed_run.workspace,
        )
        created = await sync_to_async(_create_plan_artifact_record_sync)(
            refreshed_run.pk,
            media_file.pk,
            artifact_format,
            filename,
        )
        if not created:
            await _cleanup_generated_artifact_media_file(media_file)
    return await sync_to_async(
        lambda: list(
            DevelopmentPlanArtifact.objects.select_related(
                'workspace', 'plan_run', 'employee', 'blueprint_run', 'matrix_run', 'media_file'
            )
            .filter(plan_run=refreshed_run)
            .order_by('artifact_format')
        )
    )()


async def _build_plan_artifact_bundle_for_run(
    run: DevelopmentPlanRun,
    *,
    selected_as_current: bool = False,
) -> dict[str, Any]:
    artifacts = await ensure_plan_export_artifacts(run)
    return await build_plan_artifact_bundle_response(
        run,
        artifacts,
        selected_as_current=selected_as_current,
    )


async def _ensure_and_list_artifacts_for_runs(
    runs: list[DevelopmentPlanRun],
) -> list[DevelopmentPlanArtifact]:
    artifacts: list[DevelopmentPlanArtifact] = []
    for run in runs:
        artifacts.extend(await ensure_plan_export_artifacts(run))
    return sorted(
        artifacts,
        key=lambda item: (
            _run_effective_completed_at(item.plan_run) or item.created_at,
            item.created_at,
        ),
        reverse=True,
    )


async def _generate_team_plan_payload(
    blueprint: SkillBlueprintRun,
    matrix: EvidenceMatrixRun,
    recommendation_payload: dict[str, Any],
) -> dict[str, Any]:
    narrative_payload = await _generate_team_plan_narrative(blueprint, matrix, recommendation_payload)
    return _merge_team_plan_payload(recommendation_payload, narrative_payload)


async def _generate_individual_plan_payload(
    blueprint: SkillBlueprintRun,
    matrix: EvidenceMatrixRun,
    employee: Employee,
    employee_matrix_payload: dict,
    recommendation_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if recommendation_payload is None:
        recommendation_payload = await sync_to_async(_build_individual_recommendation_payload_sync)(
            employee.workspace_id,
            blueprint.pk,
            matrix.pk,
            employee.pk,
            employee_matrix_payload,
        )
    narrative_payload = await _generate_individual_plan_narrative(
        blueprint,
        matrix,
        employee,
        employee_matrix_payload,
        recommendation_payload,
    )
    return _merge_individual_plan_payload(recommendation_payload, narrative_payload)


async def _generate_team_plan_narrative(
    blueprint: SkillBlueprintRun,
    matrix: EvidenceMatrixRun,
    recommendation_payload: dict[str, Any],
) -> dict[str, Any]:
    system_prompt = (
        'You are writing a team development plan for a pilot sponsor review.\n\n'

        '## Your task\n'
        'Write sponsor-ready narrative that explains the precomputed priority actions. '
        'The actions have already been classified (hire, develop, move, de-risk) and '
        'prioritized by the system. Your job is to make them readable, persuasive, '
        'and actionable for a leadership audience.\n\n'

        '## What to produce\n'
        '- executive_summary: 3-5 sentences capturing the overall picture. '
        'Name the company, the headcount in scope, the top 2-3 actions, and the '
        'biggest risk. A sponsor should understand the situation after reading this alone.\n'
        '- roadmap_priority_note: 1-2 sentences about which roadmap initiatives '
        'are driving the most urgent capability needs.\n'
        '- priority_actions: For each action_key from the recommendations, write:\n'
        '  - why_now: One sentence explaining the timing urgency in business terms.\n'
        '  - manager_note: One practical sentence for the hiring/team manager about '
        'what to do next.\n'
        '- hiring_recommendations: Plain language list of hiring needs, each under 15 words.\n'
        '- development_focus: Plain language list of internal development priorities.\n'
        '- single_points_of_failure: Plain language list of concentration risks.\n\n'

        '## Constraints\n'
        '- Do NOT invent new actions, roles, or skills not present in the recommendation payload.\n'
        '- Do NOT contradict the precomputed action types (if the system says "hire", '
        'do not soften it to "consider hiring").\n'
        '- Keep language direct and specific. Avoid corporate euphemisms.\n'
        '- Mention specific roles and skills by name, not "various engineering needs".\n'
        '- If the data shows high incompleteness or low confidence, say so — do not '
        'present uncertain recommendations as confident conclusions.'
    )
    company_context = blueprint.company_context if isinstance(blueprint.company_context, dict) else {}
    company_name = company_context.get('company_name', '')
    roadmap_context = blueprint.roadmap_context if isinstance(blueprint.roadmap_context, (list, dict)) else []
    user_prompt = (
        f'## Company: {company_name}\n'
        f'{json.dumps(company_context, ensure_ascii=False, indent=2)}\n\n'
        f'## Roadmap initiatives\n{json.dumps(roadmap_context, ensure_ascii=False, indent=2)}\n\n'
        f'## Matrix summary\n{json.dumps(matrix.summary_payload or {}, ensure_ascii=False, indent=2)}\n\n'
        f'## Matrix risks\n{json.dumps(matrix.risk_payload or {}, ensure_ascii=False, indent=2)}\n\n'
        f'## Precomputed priority actions (write narrative for these)\n'
        f'{json.dumps(recommendation_payload, ensure_ascii=False, indent=2)}\n\n'
        '## Instructions\n'
        'Write the team plan now. Use action_key values from the recommendations. '
        'Be specific, cite role names and skill names, keep each bullet under 15 words.'
    )
    try:
        result = await call_openai_structured(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema_name='team_development_plan_stage9',
            schema=TEAM_PLAN_SCHEMA,
            temperature=0.2,
            max_tokens=2200,
        )
        return _normalize_team_narrative_payload(result.parsed, recommendation_payload)
    except Exception as exc:
        logger.warning('Team plan narrative fallback activated for blueprint %s: %s', blueprint.uuid, exc, exc_info=True)
        return _build_team_plan_fallback(recommendation_payload)


async def _generate_individual_plan_narrative(
    blueprint: SkillBlueprintRun,
    matrix: EvidenceMatrixRun,
    employee: Employee,
    employee_matrix_payload: dict[str, Any],
    recommendation_payload: dict[str, Any],
) -> dict[str, Any]:
    employee_name = recommendation_payload.get('employee_name', employee.full_name)
    current_title = recommendation_payload.get('current_title', employee.current_title)
    best_fit_role = recommendation_payload.get('best_fit_role') or {}
    system_prompt = (
        'You are writing a personal development plan (PDP) for one employee.\n\n'

        '## Your task\n'
        'Write a coaching-quality PDP from the precomputed strengths, gaps, and actions. '
        'The structured recommendations have already been decided. Your job is to make '
        'them feel personal, actionable, and motivating.\n\n'

        '## What to produce\n'
        '- current_role_fit: 1-2 sentences about how the employee fits their target role '
        'today, mentioning strongest signals and biggest gaps.\n'
        '- adjacent_roles: List roles the employee could grow into, based on the data.\n'
        '- strengths: 3-5 bullet points naming the employee\'s strongest evidenced '
        'capabilities. Be specific: "Strong API Design (level 4, high confidence from '
        'CV evidence)" not "Good at technical work".\n'
        '- priority_gaps: 3-5 bullet points naming the most important gaps. '
        'Reference the target level and what roadmap initiative needs this.\n'
        '- development_actions: For each action_key from the recommendations, write:\n'
        '  - action: One clear sentence describing what to do.\n'
        '  - time_horizon: When this should happen.\n'
        '  - expected_outcome: What success looks like after the action.\n'
        '  - coach_note: One sentence of practical coaching advice for the manager.\n'
        '- roadmap_alignment: 1 sentence connecting the top development priority to '
        'a specific roadmap initiative.\n'
        '- mobility_note: 1 sentence about internal mobility potential. Be honest — '
        'if mobility is low, say so gently.\n\n'

        '## Tone and style\n'
        '- PERSONAL: Use the employee\'s name. Reference their actual current title '
        'and target role.\n'
        '- GROWTH-ORIENTED: Frame gaps as growth opportunities, not deficiencies. '
        '"Your next growth edge in API Design" not "You lack API Design skills".\n'
        '- PRACTICAL: Every action should describe something the employee can actually '
        'DO in the next quarter, not abstract advice.\n'
        '- HONEST: If evidence is thin or confidence is low, acknowledge it. '
        '"Based on limited evidence, we see potential in..." not false certainty.\n\n'

        '## Constraints\n'
        '- Do NOT invent new actions, skills, or roles not in the recommendation payload.\n'
        '- Do NOT create real course URLs or learning platform references. '
        'Use placeholder labels like "Placeholder resource: API Design guided track".\n'
        '- Keep action_key values exactly as provided.\n'
        '- Keep each bullet under 20 words. PDPs should be scannable, not essays.'
    )
    individual_company_context = blueprint.company_context if isinstance(blueprint.company_context, dict) else {}
    individual_roadmap_context = blueprint.roadmap_context if isinstance(blueprint.roadmap_context, (list, dict)) else []
    user_prompt = (
        f'## Employee: {employee_name} ({current_title})\n'
        f'## Target role: {best_fit_role.get("role_name", "Not yet assigned")}\n\n'
        f'## Company context\n{json.dumps(individual_company_context, ensure_ascii=False, indent=2)}\n\n'
        f'## Roadmap initiatives\n{json.dumps(individual_roadmap_context, ensure_ascii=False, indent=2)}\n\n'
        f'## Employee matrix data\n{json.dumps(employee_matrix_payload, ensure_ascii=False, indent=2)}\n\n'
        f'## Precomputed PDP recommendations (write narrative for these)\n'
        f'{json.dumps(recommendation_payload, ensure_ascii=False, indent=2)}\n\n'
        f'## Instructions\n'
        f'Write {employee_name}\'s PDP now. Use their name, reference their specific '
        f'skills and gaps, keep action_keys from recommendations. Be personal and practical.'
    )
    try:
        result = await call_openai_structured(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema_name='individual_development_plan_stage9',
            schema=INDIVIDUAL_PLAN_SCHEMA,
            temperature=0.2,
            max_tokens=1900,
        )
        return _normalize_individual_narrative_payload(result.parsed, recommendation_payload)
    except Exception as exc:
        logger.warning(
            'Individual plan narrative fallback activated for employee %s: %s',
            employee.uuid,
            exc,
            exc_info=True,
        )
        return _build_individual_plan_fallback(recommendation_payload)


def _build_team_recommendation_payload_sync(workspace_pk, blueprint_pk, matrix_pk) -> dict[str, Any]:
    workspace = IntakeWorkspace.objects.get(pk=workspace_pk)
    blueprint = SkillBlueprintRun.objects.get(pk=blueprint_pk)
    matrix = EvidenceMatrixRun.objects.get(pk=matrix_pk)
    employees = list(matrix.matrix_payload.get('employees') or [])
    matrix_cells = list(matrix.matrix_payload.get('matrix_cells') or [])
    top_priority_gaps = list((matrix.risk_payload or {}).get('top_priority_gaps') or [])
    concentration_risks = list((matrix.risk_payload or {}).get('concentration_risks') or [])
    near_fit_candidates = list((matrix.risk_payload or {}).get('near_fit_candidates') or [])
    uncovered_roles = list((matrix.risk_payload or {}).get('uncovered_roles') or [])
    cells_by_column = _group_cells_by_column(matrix_cells)
    near_fit_by_column = _group_near_fit_candidates_by_column(near_fit_candidates)

    actions: list[dict[str, Any]] = []
    seen_action_keys: set[str] = set()

    for role in uncovered_roles[:3]:
        action = _build_uncovered_role_action(role)
        if action['action_key'] in seen_action_keys:
            continue
        seen_action_keys.add(action['action_key'])
        actions.append(action)

    for risk in concentration_risks[:3]:
        action = _build_concentration_risk_action(risk)
        if action['action_key'] in seen_action_keys:
            continue
        seen_action_keys.add(action['action_key'])
        actions.append(action)

    for gap in top_priority_gaps[:8]:
        supporting_cells = cells_by_column.get(str(gap.get('column_key') or ''), [])
        linked_initiatives = _dedupe_strings(
            initiative
            for cell in supporting_cells
            for initiative in list(cell.get('supported_initiatives') or [])
        )
        near_fit_matches = near_fit_by_column.get(str(gap.get('column_key') or ''), [])
        action = _build_gap_action(gap, linked_initiatives=linked_initiatives, near_fit_matches=near_fit_matches)
        if action['action_key'] in seen_action_keys:
            continue
        seen_action_keys.add(action['action_key'])
        actions.append(action)

    for employee_payload in employees:
        action = _build_move_action(employee_payload, cells_by_column)
        if action is None or action['action_key'] in seen_action_keys:
            continue
        seen_action_keys.add(action['action_key'])
        actions.append(action)

    actions = sorted(actions, key=_team_action_sort_key)[:MAX_TEAM_PRIORITY_ACTIONS]
    actions = _attach_team_action_context(workspace, matrix, actions, cells_by_column)

    action_counts = _count_by_key(action.get('action_type', '') for action in actions)
    return {
        'plan_version': PLAN_VERSION,
        'scope': PlanScope.TEAM,
        'workspace_uuid': str(workspace.uuid),
        'blueprint_run_uuid': str(blueprint.uuid),
        'matrix_run_uuid': str(matrix.uuid),
        'employee_count': len(employees),
        'priority_actions': actions,
        'action_counts': action_counts,
        'top_priority_gaps': top_priority_gaps[:8],
        'concentration_risks': concentration_risks[:6],
        'near_fit_candidates': near_fit_candidates[:6],
        'uncovered_roles': uncovered_roles[:6],
        'input_snapshot': {
            'blueprint_run_uuid': str(blueprint.uuid),
            'matrix_run_uuid': str(matrix.uuid),
            'employee_count': len(employees),
            'top_priority_gap_count': len(top_priority_gaps),
            'concentration_risk_count': len(concentration_risks),
        },
    }


def _build_individual_recommendation_payload_sync(
    workspace_pk,
    blueprint_pk,
    matrix_pk,
    employee_pk,
    employee_matrix_payload: dict[str, Any],
) -> dict[str, Any]:
    workspace = IntakeWorkspace.objects.get(pk=workspace_pk)
    blueprint = SkillBlueprintRun.objects.get(pk=blueprint_pk)
    matrix = EvidenceMatrixRun.objects.get(pk=matrix_pk)
    employee = Employee.objects.get(pk=employee_pk)
    latest_pack = _resolve_matrix_assessment_pack_sync(employee, blueprint, matrix)

    employee_uuid = str(employee.uuid)
    cells = [
        cell
        for cell in list(matrix.matrix_payload.get('matrix_cells') or [])
        if str(cell.get('employee_uuid') or '') == employee_uuid
    ]
    strengths = _select_strength_cells(cells)
    gap_cells = _select_priority_gap_cells(cells)
    aspiration = dict((latest_pack.fused_summary or {}).get('aspiration') or {}) if latest_pack else {}
    goal_type = _resolve_goal_type(employee_matrix_payload, aspiration)
    mobility_potential = _resolve_mobility_potential(employee_matrix_payload, aspiration)
    actions = [
        _build_individual_action(cell, goal_type=goal_type, aspiration=aspiration)
        for cell in gap_cells[:MAX_INDIVIDUAL_ACTIONS]
    ]
    actions = _attach_individual_action_context(workspace, blueprint, matrix, employee, actions, strengths)

    adjacent_roles = _collect_adjacent_role_labels(employee_matrix_payload, aspiration)
    return {
        'plan_version': PLAN_VERSION,
        'scope': PlanScope.INDIVIDUAL,
        'workspace_uuid': str(workspace.uuid),
        'blueprint_run_uuid': str(blueprint.uuid),
        'matrix_run_uuid': str(matrix.uuid),
        'employee_uuid': employee_uuid,
        'employee_name': employee.full_name,
        'current_title': employee.current_title,
        'best_fit_role': employee_matrix_payload.get('best_fit_role'),
        'current_role_goal': goal_type,
        'mobility_potential': mobility_potential,
        'aspiration': aspiration,
        'adjacent_roles': adjacent_roles,
        'strength_cells': strengths[:4],
        'gap_cells': gap_cells[:4],
        'development_actions': actions,
        'latest_self_report_summary': dict(latest_pack.fused_summary or {}) if latest_pack else {},
        'input_snapshot': {
            'blueprint_run_uuid': str(blueprint.uuid),
            'matrix_run_uuid': str(matrix.uuid),
            'employee_uuid': employee_uuid,
            'assessment_cycle_uuids_used': list((matrix.input_snapshot or {}).get('assessment_cycle_uuids_used') or []),
            'selected_assessment_pack_uuid': str(latest_pack.uuid) if latest_pack else '',
            'strength_count': len(strengths),
            'gap_count': len(gap_cells),
        },
    }


def _build_uncovered_role_action(role: dict[str, Any]) -> dict[str, Any]:
    role_name = str(role.get('role_name') or 'Uncovered role').strip()
    seniority = str(role.get('seniority') or '').strip()
    role_label = f'{role_name} ({seniority})' if seniority else role_name
    return {
        'action_key': f'hire-role:{role.get("role_profile_uuid", role_name)}',
        'action_type': 'hire',
        'action': f'Hire into the uncovered {role_label} capability.',
        'owner_role': role_name or 'Functional lead',
        'time_horizon': 'this quarter',
        'urgency': 'high',
        'linked_initiatives': [],
        'impact_if_unresolved': f'{role_label} has no matched internal coverage in the current matrix.',
        'why': f'{role_label} is currently uncovered, so roadmap delivery depends on external capacity unless scope changes.',
        'supporting_signals': {
            'matched_employee_count': int(role.get('matched_employee_count') or 0),
        },
    }


def _build_concentration_risk_action(risk: dict[str, Any]) -> dict[str, Any]:
    role_name = str(risk.get('role_name') or '').strip()
    skill_name = str(risk.get('skill_name_en') or risk.get('skill_key') or '').strip()
    return {
        'action_key': f'derisk:{role_name}:{risk.get("skill_key", "")}',
        'action_type': 'de-risk',
        'action': f'De-risk {skill_name} coverage inside {role_name}.',
        'owner_role': role_name or 'Functional lead',
        'time_horizon': 'next cycle',
        'urgency': 'high' if int(risk.get('priority') or 0) >= 5 else 'medium',
        'linked_initiatives': [],
        'impact_if_unresolved': f'{skill_name} currently has {int(risk.get("ready_employee_count") or 0)} ready employee(s).',
        'why': 'This capability is concentrated in too few people for reliable roadmap execution.',
        'supporting_signals': {
            'ready_employee_count': int(risk.get('ready_employee_count') or 0),
            'priority': int(risk.get('priority') or 0),
        },
    }


def _build_gap_action(
    gap: dict[str, Any],
    *,
    linked_initiatives: list[str],
    near_fit_matches: list[dict[str, Any]],
) -> dict[str, Any]:
    role_name = str(gap.get('role_name') or '').strip()
    skill_name = str(gap.get('skill_name_en') or gap.get('skill_key') or '').strip()
    average_gap = float(gap.get('average_gap') or 0.0)
    max_priority = int(gap.get('max_priority') or 0)
    employees_meeting_target = int(gap.get('employees_meeting_target') or 0)
    severe = average_gap >= SEVERE_GAP_THRESHOLD or (max_priority >= 5 and employees_meeting_target == 0)

    if severe and not near_fit_matches:
        action_type = 'hire'
        action = f'Hire targeted {skill_name} depth for {role_name}.'
        time_horizon = 'this quarter'
        why = 'The matrix shows a severe roadmap-relevant gap without a credible internal near-fit candidate.'
        impact = 'Delivery risk stays high unless the capability is added externally or scope changes.'
    elif near_fit_matches:
        candidate_names = ', '.join(item.get('full_name', '') for item in near_fit_matches[:2] if item.get('full_name'))
        action_type = 'develop'
        action = f'Develop internal near-fit coverage for {skill_name} in {role_name}.'
        time_horizon = 'next cycle' if average_gap <= DEVELOPABLE_GAP_THRESHOLD else 'this quarter'
        why = (
            f'Near-fit internal candidates already exist ({candidate_names}) and can close this gap faster than a fresh hire.'
            if candidate_names else
            'There is credible internal base evidence to close this gap through targeted development.'
        )
        impact = 'Targeted development is likely enough to unblock the roadmap if started now.'
    else:
        action_type = 'develop'
        action = f'Strengthen {skill_name} capability inside {role_name}.'
        time_horizon = 'next cycle'
        why = 'The gap is meaningful, but the current matrix still shows enough internal base to prioritize development first.'
        impact = 'Internal development should reduce delivery risk without immediate hiring.'

    return {
        'action_key': f'{action_type}:{gap.get("column_key", role_name)}',
        'action_type': action_type,
        'action': action,
        'owner_role': role_name or 'Functional lead',
        'time_horizon': time_horizon,
        'urgency': _urgency_from_priority(max_priority, severe=severe),
        'linked_initiatives': linked_initiatives,
        'impact_if_unresolved': impact,
        'why': why,
        'column_key': gap.get('column_key', ''),
        'skill_key': gap.get('skill_key', ''),
        'supporting_signals': {
            'average_gap': average_gap,
            'max_priority': max_priority,
            'employees_meeting_target': employees_meeting_target,
            'employees_below_target': int(gap.get('employees_below_target') or 0),
            'near_fit_candidate_count': len(near_fit_matches),
        },
    }


def _build_move_action(employee_payload: dict[str, Any], cells_by_column: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    best_fit_role = dict(employee_payload.get('best_fit_role') or {})
    if not best_fit_role:
        return None
    current_title = str(employee_payload.get('current_title') or '').strip()
    role_name = str(best_fit_role.get('role_name') or '').strip()
    fit_score = float(best_fit_role.get('fit_score') or 0.0)
    total_gap_score = float(employee_payload.get('total_gap_score') or 0.0)
    average_confidence = float(employee_payload.get('average_confidence') or 0.0)
    if not current_title or current_title.casefold() == role_name.casefold():
        return None
    if fit_score < 0.8 or total_gap_score > 6.0 or average_confidence < GOOD_CONFIDENCE_THRESHOLD:
        return None

    linked_initiatives = _dedupe_strings(
        initiative
        for top_gap in list(employee_payload.get('top_gaps') or [])
        for initiative in list(top_gap.get('supported_initiatives') or [])
    )
    return {
        'action_key': f'move:{employee_payload.get("employee_uuid", "")}:{role_name}',
        'action_type': 'move',
        'action': f'Use {employee_payload.get("full_name", "the employee")} as a stretch fit toward {role_name}.',
        'owner_role': role_name or 'Functional lead',
        'time_horizon': 'next cycle',
        'urgency': 'medium',
        'linked_initiatives': linked_initiatives,
        'impact_if_unresolved': 'This employee may remain under-used relative to the best-fit role indicated by the matrix.',
        'why': 'The matrix shows a strong role match outside the current title, which creates an internal mobility option.',
        'employee_uuid': employee_payload.get('employee_uuid', ''),
        'supporting_signals': {
            'fit_score': fit_score,
            'total_gap_score': total_gap_score,
            'average_confidence': average_confidence,
        },
    }


def _select_strength_cells(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = [
        cell
        for cell in cells
        if float(cell.get('gap') or 0.0) <= 0.5
        and float(cell.get('confidence') or 0.0) >= GOOD_CONFIDENCE_THRESHOLD
        and not bool(cell.get('is_incomplete'))
    ]
    return sorted(candidates, key=lambda item: (-float(item.get('confidence') or 0.0), -int(item.get('priority') or 0)))


def _select_priority_gap_cells(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = [cell for cell in cells if float(cell.get('gap') or 0.0) > 0.25]
    return sorted(
        candidates,
        key=lambda item: (
            -(float(item.get('gap') or 0.0) * int(item.get('priority') or 0)),
            -float(item.get('confidence') or 0.0),
        ),
    )


def _resolve_goal_type(employee_matrix_payload: dict[str, Any], aspiration: dict[str, Any]) -> str:
    adjacent_roles = list(employee_matrix_payload.get('adjacent_roles') or [])
    best_fit_role = dict(employee_matrix_payload.get('best_fit_role') or {})
    target_role_family = str(aspiration.get('target_role_family') or '').strip()
    if target_role_family and any(
        str(item.get('role_family') or '').strip() == target_role_family for item in adjacent_roles
    ):
        return 'adjacent_role_growth'
    if best_fit_role and str(employee_matrix_payload.get('current_title') or '').strip().casefold() != str(best_fit_role.get('role_name') or '').strip().casefold():
        return 'adjacent_role_growth'
    return 'current_role_excellence'


def _resolve_mobility_potential(employee_matrix_payload: dict[str, Any], aspiration: dict[str, Any]) -> str:
    adjacent_roles = list(employee_matrix_payload.get('adjacent_roles') or [])
    if not adjacent_roles:
        return 'low'
    if str(aspiration.get('interest_signal') or '').strip().lower() in {'high', 'strong'}:
        return 'high'
    if float((adjacent_roles[0] or {}).get('fit_score') or 0.0) >= 0.75:
        return 'medium'
    return 'low'


def _collect_adjacent_role_labels(employee_matrix_payload: dict[str, Any], aspiration: dict[str, Any]) -> list[str]:
    labels = [
        _format_role_label(item)
        for item in list(employee_matrix_payload.get('adjacent_roles') or [])
    ]
    target_role_family = str(aspiration.get('target_role_family') or '').strip()
    if target_role_family:
        labels.append(f'Aspiration: {target_role_family}')
    return _dedupe_strings(labels)


def _build_individual_action(
    cell: dict[str, Any],
    *,
    goal_type: str,
    aspiration: dict[str, Any],
) -> dict[str, Any]:
    skill_name = str(cell.get('skill_name_en') or cell.get('skill_key') or '').strip()
    gap = float(cell.get('gap') or 0.0)
    confidence = float(cell.get('confidence') or 0.0)
    flags = list(cell.get('incompleteness_flags') or [])
    if 'self_report_only' in flags or 'low_confidence' in flags:
        action_type = 'evidence_building'
        action = f'Build stronger evidence for {skill_name} through a scoped deliverable.'
        expected_outcome = f'Produce recent, reviewable examples that prove {skill_name} at the target level.'
        resource_type = 'project'
    elif goal_type == 'adjacent_role_growth' and gap <= MOVE_GAP_THRESHOLD:
        action_type = 'stretch_assignment'
        action = f'Use a stretch assignment to grow {skill_name} toward the next role.'
        expected_outcome = f'Demonstrate {skill_name} in a role-adjacent initiative over the next cycle.'
        resource_type = 'stretch'
    elif gap >= SEVERE_GAP_THRESHOLD:
        action_type = 'guided_practice'
        action = f'Run a guided practice plan for {skill_name} with explicit checkpoints.'
        expected_outcome = f'Raise {skill_name} by one level with evidence from roadmap-relevant work.'
        resource_type = 'guided_track'
    else:
        action_type = 'applied_project'
        action = f'Apply {skill_name} in a roadmap-facing project with regular feedback.'
        expected_outcome = f'Close the gap in {skill_name} while contributing to current roadmap work.'
        resource_type = 'project'

    return {
        'action_key': f'{cell.get("employee_uuid", "")}:{cell.get("skill_key", "")}',
        'skill_key': cell.get('skill_key', ''),
        'skill_name_en': skill_name,
        'skill_name_ru': cell.get('skill_name_ru', ''),
        'goal_type': goal_type,
        'action_type': action_type,
        'action': action,
        'time_horizon': _time_horizon_from_priority(int(cell.get('priority') or 0), gap=gap),
        'expected_outcome': expected_outcome,
        'course_placeholder': f'Placeholder resource: {skill_name} {resource_type.replace("_", " ")}',
        'placeholder_resource_type': resource_type,
        'why_now': (
            f'{skill_name} is blocking or constraining roadmap work right now.'
            if int(cell.get('priority') or 0) >= HIGH_PRIORITY_THRESHOLD
            else f'{skill_name} will improve execution reliability in the next cycle.'
        ),
        'linked_initiatives': list(cell.get('supported_initiatives') or []),
        'supporting_signals': {
            'gap': gap,
            'confidence': confidence,
            'priority': int(cell.get('priority') or 0),
            'incompleteness_flags': flags,
        },
    }


def _attach_team_action_context(
    workspace,
    matrix: EvidenceMatrixRun,
    actions: list[dict[str, Any]],
    cells_by_column: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    enriched_actions = []
    cycle_uuids = _extract_matrix_assessment_cycle_uuids(matrix)
    query_texts = [_build_team_action_query(action) for action in actions]
    query_vectors = _embed_context_query_texts(workspace.slug, query_texts)
    for query_index, action in enumerate(actions):
        query_text = query_texts[query_index]
        query_vector = query_vectors[query_index] if query_index < len(query_vectors) else None
        roadmap_matches = retrieve_workspace_evidence_sync(
            workspace,
            query_text=query_text,
            query_vector=query_vector,
            doc_types=['roadmap_context', 'strategy_context', 'role_reference'],
            limit=MAX_CONTEXT_SNIPPETS,
            min_score=0.15,
        )
        employee_matches: list[dict[str, Any]] = []
        if action.get('employee_uuid'):
            employee_matches = retrieve_employee_fused_evidence_sync(
                workspace,
                query_text=query_text,
                query_vector=query_vector,
                employee_uuids=[str(action.get('employee_uuid'))],
                cycle_uuids=cycle_uuids or None,
                skill_keys=[str(action.get('skill_key'))] if action.get('skill_key') else None,
                cv_doc_types=STAGE9_CV_DOC_TYPES,
                self_assessment_doc_types=STAGE9_SELF_ASSESSMENT_DOC_TYPES,
                include_contextual_cv_matches=True,
                limit=MAX_CONTEXT_SNIPPETS,
                min_score=0.15,
            )
        supporting_cells = list(cells_by_column.get(str(action.get('column_key') or ''), []))[:MAX_CONTEXT_SNIPPETS]
        action['supporting_context'] = {
            'roadmap_context': _summarize_workspace_matches(roadmap_matches),
            'employee_context': _summarize_employee_matches(employee_matches),
            'matrix_provenance': _summarize_matrix_cells(supporting_cells),
        }
        enriched_actions.append(action)
    return enriched_actions


def _attach_individual_action_context(
    workspace,
    blueprint: SkillBlueprintRun,
    matrix: EvidenceMatrixRun,
    employee: Employee,
    actions: list[dict[str, Any]],
    strength_cells: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    enriched_actions = []
    cycle_uuids = _extract_matrix_assessment_cycle_uuids(matrix)
    query_texts = [_build_individual_action_query(action, employee.current_title) for action in actions]
    query_vectors = _embed_context_query_texts(workspace.slug, query_texts)
    for query_index, action in enumerate(actions):
        query_text = query_texts[query_index]
        query_vector = query_vectors[query_index] if query_index < len(query_vectors) else None
        employee_matches = retrieve_employee_fused_evidence_sync(
            workspace,
            query_text=query_text,
            query_vector=query_vector,
            employee_uuids=[str(employee.uuid)],
            cycle_uuids=cycle_uuids or None,
            skill_keys=[str(action.get('skill_key') or '')] if action.get('skill_key') else None,
            cv_doc_types=STAGE9_CV_DOC_TYPES,
            self_assessment_doc_types=STAGE9_SELF_ASSESSMENT_DOC_TYPES,
            include_contextual_cv_matches=True,
            limit=MAX_CONTEXT_SNIPPETS,
            min_score=0.15,
        )
        roadmap_matches = retrieve_workspace_evidence_sync(
            workspace,
            query_text=query_text,
            query_vector=query_vector,
            doc_types=['roadmap_context', 'strategy_context', 'role_reference'],
            limit=MAX_CONTEXT_SNIPPETS,
            min_score=0.15,
        )
        action['supporting_context'] = {
            'employee_evidence': _summarize_employee_matches(employee_matches),
            'roadmap_context': _summarize_workspace_matches(roadmap_matches),
            'strength_context': _summarize_matrix_cells(strength_cells[:2]),
        }
        enriched_actions.append(action)
    return enriched_actions


def _normalize_team_narrative_payload(parsed: dict[str, Any], recommendation_payload: dict[str, Any]) -> dict[str, Any]:
    action_map = {
        str(item.get('action_key') or ''): item
        for item in list(parsed.get('priority_actions') or [])
        if str(item.get('action_key') or '').strip()
    }
    return {
        'executive_summary': str(parsed.get('executive_summary') or '').strip(),
        'roadmap_priority_note': str(parsed.get('roadmap_priority_note') or '').strip(),
        'priority_actions': action_map,
        'hiring_recommendations': [str(item).strip() for item in list(parsed.get('hiring_recommendations') or []) if str(item).strip()],
        'development_focus': [str(item).strip() for item in list(parsed.get('development_focus') or []) if str(item).strip()],
        'single_points_of_failure': [str(item).strip() for item in list(parsed.get('single_points_of_failure') or []) if str(item).strip()],
    }


def _embed_context_query_texts(workspace_slug: str, query_texts: list[str]) -> list[Optional[list[float]]]:
    non_empty_queries = [str(item or '').strip() for item in query_texts]
    if not any(non_empty_queries):
        return [None] * len(query_texts)
    try:
        embedding_manager = get_embedding_manager_sync()
        embedded_queries = [item for item in non_empty_queries if item]
        vectors = embedding_manager.embed_batch_sync(embedded_queries)
        results: list[Optional[list[float]]] = []
        vector_index = 0
        for query_text in non_empty_queries:
            if not query_text:
                results.append(None)
                continue
            if vector_index >= len(vectors):
                results.append(None)
                continue
            results.append(vectors[vector_index])
            vector_index += 1
        return results
    except Exception as exc:
        logger.warning(
            'Stage 9 context batch embedding failed for workspace %s: %s',
            workspace_slug,
            exc,
            exc_info=True,
        )
        return [None] * len(query_texts)


def _normalize_individual_narrative_payload(parsed: dict[str, Any], recommendation_payload: dict[str, Any]) -> dict[str, Any]:
    by_action_key = {
        str(item.get('action_key') or ''): item
        for item in list(parsed.get('development_actions') or [])
        if str(item.get('action_key') or '').strip()
    }
    by_skill_name = {
        str(item.get('skill_name_en') or '').strip(): item
        for item in list(parsed.get('development_actions') or [])
        if str(item.get('skill_name_en') or '').strip()
    }
    action_map = {}
    for action in list(recommendation_payload.get('development_actions') or []):
        key = str(action.get('action_key') or '')
        action_map[key] = by_action_key.get(key) or by_skill_name.get(str(action.get('skill_name_en') or '').strip()) or {}

    return {
        'current_role_fit': str(parsed.get('current_role_fit') or '').strip(),
        'adjacent_roles': [str(item).strip() for item in list(parsed.get('adjacent_roles') or []) if str(item).strip()],
        'strengths': [str(item).strip() for item in list(parsed.get('strengths') or []) if str(item).strip()],
        'priority_gaps': [str(item).strip() for item in list(parsed.get('priority_gaps') or []) if str(item).strip()],
        'development_actions': action_map,
        'roadmap_alignment': str(parsed.get('roadmap_alignment') or '').strip(),
        'mobility_note': str(parsed.get('mobility_note') or '').strip(),
    }


def _build_team_plan_fallback(recommendation_payload: dict[str, Any]) -> dict[str, Any]:
    actions = list(recommendation_payload.get('priority_actions') or [])
    return {
        'executive_summary': (
            f"Prioritize {len(actions)} actions across hire, develop, move, and de-risk decisions. "
            f"Highest pressure sits around {', '.join(action.get('action_type', '') for action in actions[:3]) or 'development'}."
        ),
        'roadmap_priority_note': 'The most urgent actions are the ones tied to high-priority gaps and concentration risk.',
        'priority_actions': {},
        'hiring_recommendations': [
            action.get('action', '')
            for action in actions
            if action.get('action_type') == 'hire'
        ],
        'development_focus': [
            action.get('action', '')
            for action in actions
            if action.get('action_type') in {'develop', 'move'}
        ],
        'single_points_of_failure': [
            action.get('action', '')
            for action in actions
            if action.get('action_type') == 'de-risk'
        ],
    }


def _build_individual_plan_fallback(recommendation_payload: dict[str, Any]) -> dict[str, Any]:
    strength_rows = list(recommendation_payload.get('strength_cells') or [])
    gap_rows = list(recommendation_payload.get('gap_cells') or [])
    return {
        'current_role_fit': _deterministic_current_role_fit_text(recommendation_payload),
        'adjacent_roles': list(recommendation_payload.get('adjacent_roles') or []),
        'strengths': [
            _format_strength_label(cell)
            for cell in strength_rows[:4]
        ],
        'priority_gaps': [
            _format_gap_label(cell)
            for cell in gap_rows[:4]
        ],
        'development_actions': {},
        'roadmap_alignment': _deterministic_roadmap_alignment_text(recommendation_payload),
        'mobility_note': _deterministic_mobility_note(recommendation_payload),
    }


def _merge_team_plan_payload(recommendation_payload: dict[str, Any], narrative_payload: dict[str, Any]) -> dict[str, Any]:
    narrative_actions = dict(narrative_payload.get('priority_actions') or {})
    merged_actions = []
    for action in list(recommendation_payload.get('priority_actions') or []):
        narrative = narrative_actions.get(str(action.get('action_key') or ''), {})
        merged_actions.append(
            {
                **action,
                'why_now': str(narrative.get('why_now') or action.get('why') or '').strip(),
                'manager_note': str(narrative.get('manager_note') or '').strip(),
            }
        )
    return {
        'plan_version': PLAN_VERSION,
        'scope': PlanScope.TEAM,
        'executive_summary': str(
            narrative_payload.get('executive_summary') or _build_team_plan_fallback(recommendation_payload)['executive_summary']
        ).strip(),
        'roadmap_priority_note': str(
            narrative_payload.get('roadmap_priority_note') or _build_team_plan_fallback(recommendation_payload)['roadmap_priority_note']
        ).strip(),
        'priority_actions': merged_actions,
        'hiring_recommendations': list(
            narrative_payload.get('hiring_recommendations') or _build_team_plan_fallback(recommendation_payload)['hiring_recommendations']
        ),
        'development_focus': list(
            narrative_payload.get('development_focus') or _build_team_plan_fallback(recommendation_payload)['development_focus']
        ),
        'single_points_of_failure': list(
            narrative_payload.get('single_points_of_failure') or _build_team_plan_fallback(recommendation_payload)['single_points_of_failure']
        ),
        'action_counts': dict(recommendation_payload.get('action_counts') or {}),
        'input_lineage': {
            'blueprint_run_uuid': recommendation_payload.get('blueprint_run_uuid', ''),
            'matrix_run_uuid': recommendation_payload.get('matrix_run_uuid', ''),
        },
    }


def _merge_individual_plan_payload(recommendation_payload: dict[str, Any], narrative_payload: dict[str, Any]) -> dict[str, Any]:
    fallback = _build_individual_plan_fallback(recommendation_payload)
    narrative_actions = dict(narrative_payload.get('development_actions') or {})
    merged_actions = []
    for action in list(recommendation_payload.get('development_actions') or []):
        narrative = narrative_actions.get(str(action.get('action_key') or ''), {})
        merged_actions.append(
            {
                **action,
                'action': str(narrative.get('action') or action.get('action') or '').strip(),
                'time_horizon': str(narrative.get('time_horizon') or action.get('time_horizon') or '').strip(),
                'expected_outcome': str(
                    narrative.get('expected_outcome') or action.get('expected_outcome') or ''
                ).strip(),
                'coach_note': str(narrative.get('coach_note') or '').strip(),
            }
        )
    return {
        'plan_version': PLAN_VERSION,
        'scope': PlanScope.INDIVIDUAL,
        'employee_uuid': recommendation_payload.get('employee_uuid', ''),
        'employee_name': recommendation_payload.get('employee_name', ''),
        'current_title': recommendation_payload.get('current_title', ''),
        'current_role_goal': recommendation_payload.get('current_role_goal', ''),
        'mobility_potential': recommendation_payload.get('mobility_potential', ''),
        'current_role_fit': str(narrative_payload.get('current_role_fit') or fallback['current_role_fit']).strip(),
        'adjacent_roles': list(narrative_payload.get('adjacent_roles') or fallback['adjacent_roles']),
        'strengths': list(narrative_payload.get('strengths') or fallback['strengths']),
        'priority_gaps': list(narrative_payload.get('priority_gaps') or fallback['priority_gaps']),
        'development_actions': merged_actions,
        'roadmap_alignment': str(
            narrative_payload.get('roadmap_alignment') or fallback['roadmap_alignment']
        ).strip(),
        'mobility_note': str(narrative_payload.get('mobility_note') or fallback['mobility_note']).strip(),
        'aspiration': dict(recommendation_payload.get('aspiration') or {}),
    }


async def _resolve_current_plan_inputs(workspace, *, planning_context=None):
    blueprint = await get_current_published_blueprint_run(workspace, planning_context=planning_context)
    if blueprint is None:
        return None, None
    matrix = await get_current_completed_matrix_run(
        workspace,
        blueprint_run=blueprint,
        planning_context=planning_context,
    )
    return blueprint, matrix


def _build_input_snapshot(
    recommendation_payload: dict[str, Any],
    generation_batch_uuid: uuid_mod.UUID | None = None,
) -> dict[str, Any]:
    snapshot = dict(recommendation_payload.get('input_snapshot') or {})
    if generation_batch_uuid is not None:
        snapshot['generation_batch_uuid'] = str(generation_batch_uuid)
    return snapshot


def _build_export_snapshot_payload(
    run: DevelopmentPlanRun,
    plan_payload: dict[str, Any],
    recommendation_payload: dict[str, Any],
    summary_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        'snapshot_version': PLAN_ARTIFACT_VERSION,
        'title': run.title,
        'scope': run.scope,
        'status': PlanRunStatus.COMPLETED,
        'workspace': {
            'uuid': str(run.workspace.uuid),
            'name': run.workspace.name,
            'slug': run.workspace.slug,
        },
        'employee': {
            'uuid': str(run.employee.uuid) if run.employee_id else '',
            'full_name': run.employee.full_name if run.employee_id else '',
            'current_title': run.employee.current_title if run.employee_id else '',
        },
        'lineage': {
            'plan_run_uuid': str(run.uuid),
            'blueprint_run_uuid': str(getattr(run.blueprint_run, 'uuid', '') or ''),
            'matrix_run_uuid': str(getattr(run.matrix_run, 'uuid', '') or ''),
            'generation_batch_uuid': str(run.generation_batch_uuid or ''),
            'plan_completed_at': (run.completed_at or timezone.now()).isoformat(),
        },
        'company_context': deepcopy(getattr(run.blueprint_run, 'company_context', {}) or {}),
        'roadmap_context': deepcopy(getattr(run.blueprint_run, 'roadmap_context', []) or []),
        'summary': summary_payload or {},
        'plan_payload': plan_payload or {},
        'recommendation_payload': recommendation_payload or {},
    }


def _build_team_run_summary(
    recommendation_payload: dict[str, Any],
    artifact: dict[str, Any],
    *,
    expected_employee_count: int,
) -> dict[str, Any]:
    return {
        'artifact_media_file_uuid': artifact.get('media_file_uuid'),
        'artifact_persistent_key': artifact.get('persistent_key'),
        'action_counts': dict(recommendation_payload.get('action_counts') or {}),
        'priority_action_count': len(list(recommendation_payload.get('priority_actions') or [])),
        'expected_employee_count': expected_employee_count,
        'completed_individual_plan_count': 0,
        'failed_individual_plan_count': 0,
        'missing_individual_plan_count': 0,
        'batch_status': 'running',
    }


def _build_individual_run_summary(recommendation_payload: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        'artifact_media_file_uuid': artifact.get('media_file_uuid'),
        'artifact_persistent_key': artifact.get('persistent_key'),
        'development_action_count': len(list(recommendation_payload.get('development_actions') or [])),
        'current_role_goal': recommendation_payload.get('current_role_goal', ''),
        'mobility_potential': recommendation_payload.get('mobility_potential', ''),
    }


async def _upload_generated_plan_artifact(
    *,
    workspace,
    workspace_slug: str,
    filename: str,
    payload: dict,
    description: str,
) -> dict:
    media_file = await store_prototype_generated_text_artifact(
        scope=f'{workspace_slug}/generated',
        filename=filename,
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        content_type='application/json',
        description=description,
        metadata={'workspace_slug': workspace_slug, 'generated': True},
        prototype_workspace=workspace,
    )
    return {
        'media_file_uuid': str(media_file.uuid),
        'persistent_key': media_file.persistent_key or '',
    }


def _order_plan_queryset(queryset):
    return queryset.annotate(
        effective_completed_at=Coalesce('completed_at', 'updated_at')
    ).order_by('-effective_completed_at', '-created_at', '-pk')


def _finalize_plan_run_sync(
    run_pk,
    plan_payload: dict[str, Any],
    artifact: dict[str, Any],
    recommendation_payload: dict[str, Any],
    summary_payload: dict[str, Any],
) -> None:
    run = DevelopmentPlanRun.objects.select_related('workspace', 'employee', 'blueprint_run', 'matrix_run').get(pk=run_pk)
    run.status = PlanRunStatus.COMPLETED
    run.completed_at = run.completed_at or timezone.now()
    run.is_current = False
    run.plan_payload = plan_payload
    run.recommendation_payload = recommendation_payload
    run.export_snapshot = _build_export_snapshot_payload(run, plan_payload, recommendation_payload, summary_payload)
    run.summary = summary_payload
    run.final_report_key = artifact.get('persistent_key', '')
    run.save(
        update_fields=[
            'status',
            'plan_payload',
            'recommendation_payload',
            'export_snapshot',
            'summary',
            'final_report_key',
            'completed_at',
            'is_current',
            'updated_at',
        ]
    )


def _fail_plan_run_sync(run_pk, error_message: str) -> None:
    run = DevelopmentPlanRun.objects.get(pk=run_pk)
    run.status = PlanRunStatus.FAILED
    run.is_current = False
    run.summary = {
        **(run.summary or {}),
        'error_message': error_message,
    }
    run.save(update_fields=['status', 'is_current', 'summary', 'updated_at'])


def _list_batch_individual_runs_sync(generation_batch_uuid: uuid_mod.UUID | str) -> list[DevelopmentPlanRun]:
    return list(
        DevelopmentPlanRun.objects.filter(
            generation_batch_uuid=generation_batch_uuid,
            scope=PlanScope.INDIVIDUAL,
        )
        .select_related('workspace', 'employee', 'blueprint_run', 'matrix_run')
        .order_by('employee__full_name', '-completed_at', '-created_at')
    )


def _prepare_employee_payloads_for_batch(
    employee_payloads: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    deduped: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    seen_employee_uuids: set[str] = set()
    for payload in employee_payloads:
        employee_uuid = str(payload.get('employee_uuid') or '').strip()
        if not employee_uuid:
            duplicates.append(
                {
                    'employee_uuid': '',
                    'full_name': str(payload.get('full_name') or '').strip(),
                    'reason': 'Employee payload missing employee_uuid.',
                }
            )
            continue
        if employee_uuid in seen_employee_uuids:
            duplicates.append(
                {
                    'employee_uuid': employee_uuid,
                    'full_name': str(payload.get('full_name') or '').strip(),
                    'reason': 'Duplicate employee_uuid in matrix payload.',
                }
            )
            continue
        seen_employee_uuids.add(employee_uuid)
        deduped.append(payload)
    return deduped, duplicates


def _update_plan_run_inputs_sync(
    run_pk: int,
    input_snapshot: dict[str, Any],
    recommendation_payload: dict[str, Any],
) -> None:
    DevelopmentPlanRun.objects.filter(pk=run_pk).update(
        input_snapshot=input_snapshot,
        recommendation_payload=recommendation_payload,
    )


def _list_latest_individual_plans_sync(
    workspace_pk: int,
    generation_batch_uuid: uuid_mod.UUID | str | None,
    planning_context_pk=None,
) -> list[DevelopmentPlanRun]:
    queryset = DevelopmentPlanRun.objects.filter(
        workspace_id=workspace_pk,
        scope=PlanScope.INDIVIDUAL,
        **_plan_context_filter_kwargs(planning_context_pk),
    ).select_related('workspace', 'employee', 'blueprint_run', 'matrix_run')
    if generation_batch_uuid:
        return list(
            queryset.filter(generation_batch_uuid=generation_batch_uuid).order_by(
                'employee__full_name',
                '-completed_at',
                '-updated_at',
                '-created_at',
            )
        )
    latest_by_employee: dict[int, DevelopmentPlanRun] = {}
    for run in _order_plan_queryset(queryset):
        if run.employee_id is None or run.employee_id in latest_by_employee:
            continue
        latest_by_employee[run.employee_id] = run
    return list(latest_by_employee.values())


def _get_latest_completed_team_plan_sync(workspace_pk: int, planning_context_pk=None) -> Optional[DevelopmentPlanRun]:
    return _order_plan_queryset(
        DevelopmentPlanRun.objects.filter(
            workspace_id=workspace_pk,
            scope=PlanScope.TEAM,
            status=PlanRunStatus.COMPLETED,
            **_plan_context_filter_kwargs(planning_context_pk),
        ).select_related('workspace', 'employee', 'blueprint_run', 'matrix_run')
    ).first()


def _list_latest_completed_individual_plans_sync(workspace_pk: int, planning_context_pk=None) -> list[DevelopmentPlanRun]:
    queryset = DevelopmentPlanRun.objects.filter(
        workspace_id=workspace_pk,
        scope=PlanScope.INDIVIDUAL,
        status=PlanRunStatus.COMPLETED,
        **_plan_context_filter_kwargs(planning_context_pk),
    ).select_related('workspace', 'employee', 'blueprint_run', 'matrix_run')
    latest_by_employee: dict[int, DevelopmentPlanRun] = {}
    for run in _order_plan_queryset(queryset):
        if run.employee_id is None or run.employee_id in latest_by_employee:
            continue
        latest_by_employee[run.employee_id] = run
    return list(latest_by_employee.values())


def _list_latest_individual_plans_for_lineage_sync(
    workspace_pk: int,
    blueprint_pk,
    matrix_pk,
    planning_context_pk=None,
) -> list[DevelopmentPlanRun]:
    queryset = DevelopmentPlanRun.objects.filter(
        workspace_id=workspace_pk,
        scope=PlanScope.INDIVIDUAL,
        blueprint_run_id=blueprint_pk,
        matrix_run_id=matrix_pk,
        **_plan_context_filter_kwargs(planning_context_pk),
    ).select_related('workspace', 'employee', 'blueprint_run', 'matrix_run')
    latest_by_employee: dict[int, DevelopmentPlanRun] = {}
    for run in _order_plan_queryset(queryset):
        if run.employee_id is None or run.employee_id in latest_by_employee:
            continue
        latest_by_employee[run.employee_id] = run
    return list(latest_by_employee.values())


def _get_latest_individual_plan_sync(
    workspace_pk: int,
    employee_uuid: str,
    generation_batch_uuid: uuid_mod.UUID | str | None,
    planning_context_pk=None,
) -> Optional[DevelopmentPlanRun]:
    queryset = DevelopmentPlanRun.objects.filter(
        workspace_id=workspace_pk,
        scope=PlanScope.INDIVIDUAL,
        employee__uuid=employee_uuid,
        **_plan_context_filter_kwargs(planning_context_pk),
    ).select_related('workspace', 'employee', 'blueprint_run', 'matrix_run')
    if generation_batch_uuid:
        queryset = queryset.filter(generation_batch_uuid=generation_batch_uuid)
    return _order_plan_queryset(queryset).first()


def _get_latest_completed_individual_plan_sync(
    workspace_pk: int,
    employee_uuid: str,
    planning_context_pk=None,
) -> Optional[DevelopmentPlanRun]:
    return _order_plan_queryset(
        DevelopmentPlanRun.objects.filter(
            workspace_id=workspace_pk,
            scope=PlanScope.INDIVIDUAL,
            status=PlanRunStatus.COMPLETED,
            employee__uuid=employee_uuid,
            **_plan_context_filter_kwargs(planning_context_pk),
        ).select_related('workspace', 'employee', 'blueprint_run', 'matrix_run')
    ).first()


def _get_latest_individual_plan_for_lineage_sync(
    workspace_pk: int,
    employee_uuid: str,
    blueprint_pk,
    matrix_pk,
    planning_context_pk=None,
) -> Optional[DevelopmentPlanRun]:
    return _order_plan_queryset(
        DevelopmentPlanRun.objects.filter(
            workspace_id=workspace_pk,
            scope=PlanScope.INDIVIDUAL,
            employee__uuid=employee_uuid,
            blueprint_run_id=blueprint_pk,
            matrix_run_id=matrix_pk,
            **_plan_context_filter_kwargs(planning_context_pk),
        ).select_related('workspace', 'employee', 'blueprint_run', 'matrix_run')
    ).first()


def _build_plan_summary_payload(
    workspace_slug: str,
    team_plan: DevelopmentPlanRun | None,
    individual_plans: list[DevelopmentPlanRun],
) -> dict[str, Any]:
    if team_plan is None:
        return {
            'workspace_slug': workspace_slug,
            'blueprint_run_uuid': None,
            'matrix_run_uuid': None,
            'planning_context_uuid': None,
            'generation_batch_uuid': None,
            'team_plan_uuid': None,
            'team_plan_status': '',
            'batch_status': '',
            'is_current': False,
            'individual_plan_count': 0,
            'employee_count_in_scope': 0,
            'completed_individual_plan_count': 0,
            'failed_individual_plan_count': 0,
            'missing_individual_plan_count': 0,
            'action_counts': {},
            'updated_at': None,
        }
    recommendation_payload = dict(team_plan.recommendation_payload or {})
    summary = dict(team_plan.summary or {})
    return {
        'workspace_slug': workspace_slug,
        'blueprint_run_uuid': getattr(team_plan.blueprint_run, 'uuid', None),
        'matrix_run_uuid': getattr(team_plan.matrix_run, 'uuid', None),
        'planning_context_uuid': team_plan.planning_context_id,
        'generation_batch_uuid': team_plan.generation_batch_uuid,
        'team_plan_uuid': team_plan.uuid,
        'team_plan_status': team_plan.status,
        'batch_status': str(summary.get('batch_status') or team_plan.status).strip(),
        'is_current': bool(team_plan.is_current),
        'individual_plan_count': len(individual_plans),
        'employee_count_in_scope': int(
            summary.get('expected_employee_count')
            or recommendation_payload.get('employee_count')
            or len(individual_plans)
        ),
        'completed_individual_plan_count': int(
            summary.get('completed_individual_plan_count')
            or len([run for run in individual_plans if run.status == PlanRunStatus.COMPLETED])
        ),
        'failed_individual_plan_count': int(summary.get('failed_individual_plan_count') or 0),
        'missing_individual_plan_count': int(summary.get('missing_individual_plan_count') or 0),
        'action_counts': dict(recommendation_payload.get('action_counts') or {}),
        'updated_at': team_plan.updated_at,
    }


def _resolve_matrix_assessment_pack_sync(
    employee: Employee,
    blueprint: SkillBlueprintRun,
    matrix: EvidenceMatrixRun,
) -> Optional[EmployeeAssessmentPack]:
    cycle_uuids = _extract_matrix_assessment_cycle_uuids(matrix)
    latest_cycle_uuid = str((matrix.input_snapshot or {}).get('latest_current_assessment_cycle_uuid') or '').strip()
    queryset = EmployeeAssessmentPack.objects.filter(
        employee=employee,
        status__in=[AssessmentPackStatus.SUBMITTED, AssessmentPackStatus.COMPLETED],
        cycle__blueprint_run=blueprint,
    )
    if cycle_uuids:
        queryset = queryset.filter(cycle__uuid__in=cycle_uuids)
        return queryset.order_by('-submitted_at', '-updated_at').first()
    if latest_cycle_uuid:
        return queryset.filter(cycle__uuid=latest_cycle_uuid).order_by('-submitted_at', '-updated_at').first()
    return None


def _extract_matrix_assessment_cycle_uuids(matrix: EvidenceMatrixRun) -> list[str]:
    return [
        str(item).strip()
        for item in list((matrix.input_snapshot or {}).get('assessment_cycle_uuids_used') or [])
        if str(item).strip()
    ]


def _finalize_generation_batch_sync(
    workspace_pk: int,
    generation_batch_uuid: uuid_mod.UUID | str,
    expected_employee_uuids: list[str],
    missing_employee_records: list[dict[str, Any]],
    planning_context_pk=None,
) -> dict[str, Any]:
    with transaction.atomic():
        runs = list(
            DevelopmentPlanRun.objects.select_related('employee')
            .filter(generation_batch_uuid=generation_batch_uuid)
            .order_by('scope', 'employee__full_name', '-completed_at', '-created_at')
        )
        team_run = next((run for run in runs if run.scope == PlanScope.TEAM), None)
        individual_runs = [run for run in runs if run.scope == PlanScope.INDIVIDUAL]
        expected_count = len({item for item in expected_employee_uuids if item})
        completed_count = len([run for run in individual_runs if run.status == PlanRunStatus.COMPLETED])
        failed_count = len([run for run in individual_runs if run.status == PlanRunStatus.FAILED])
        missing_count = len(missing_employee_records)
        batch_status = 'completed'
        if team_run is None or team_run.status != PlanRunStatus.COMPLETED:
            batch_status = 'failed'
        elif failed_count or missing_count or completed_count != expected_count:
            batch_status = 'partial_failed'

        DevelopmentPlanRun.objects.filter(generation_batch_uuid=generation_batch_uuid).update(is_current=False)
        DevelopmentPlanArtifact.objects.filter(generation_batch_uuid=generation_batch_uuid).update(is_current=False)
        if batch_status == 'completed':
            DevelopmentPlanRun.objects.filter(
                workspace_id=workspace_pk,
                is_current=True,
                **_plan_context_filter_kwargs(planning_context_pk),
            ).update(is_current=False)
            DevelopmentPlanRun.objects.filter(generation_batch_uuid=generation_batch_uuid).update(is_current=True)
            DevelopmentPlanArtifact.objects.filter(
                workspace_id=workspace_pk,
                is_current=True,
                **(
                    {'plan_run__planning_context_id': planning_context_pk}
                    if planning_context_pk is not None
                    else {'plan_run__planning_context__isnull': True}
                ),
            ).update(is_current=False)
            DevelopmentPlanArtifact.objects.filter(generation_batch_uuid=generation_batch_uuid).update(is_current=True)

        if team_run is not None:
            DevelopmentPlanRun.objects.filter(pk=team_run.pk).update(
                summary={
                    **(team_run.summary or {}),
                    'generation_batch_uuid': str(generation_batch_uuid),
                    'expected_employee_count': expected_count,
                    'completed_individual_plan_count': completed_count,
                    'failed_individual_plan_count': failed_count,
                    'missing_individual_plan_count': missing_count,
                    'missing_employee_records': missing_employee_records,
                    'batch_status': batch_status,
                }
            )

    return {
        'generation_batch_uuid': str(generation_batch_uuid),
        'expected_employee_count': expected_count,
        'completed_individual_plan_count': completed_count,
        'failed_individual_plan_count': failed_count,
        'missing_individual_plan_count': missing_count,
        'batch_status': batch_status,
    }


def _group_cells_by_column(matrix_cells: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cell in matrix_cells:
        column_key = str(cell.get('column_key') or _build_column_key(cell)).strip()
        if not column_key:
            continue
        grouped[column_key].append(cell)
    return grouped


def _group_near_fit_candidates_by_column(near_fit_candidates: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in near_fit_candidates:
        for gap in list(item.get('top_gaps') or []):
            column_key = str(gap.get('column_key') or _build_column_key(gap)).strip()
            if not column_key:
                continue
            grouped[column_key].append(item)
    return grouped


def _build_team_action_query(action: dict[str, Any]) -> str:
    return ' '.join(
        part
        for part in [
            str(action.get('action') or '').strip(),
            str(action.get('owner_role') or '').strip(),
            ' '.join(list(action.get('linked_initiatives') or [])),
        ]
        if part
    ).strip()


def _build_individual_action_query(action: dict[str, Any], current_title: str) -> str:
    return ' '.join(
        part
        for part in [
            current_title.strip(),
            str(action.get('skill_name_en') or '').strip(),
            str(action.get('action') or '').strip(),
            ' '.join(list(action.get('linked_initiatives') or [])),
        ]
        if part
    ).strip()


def _summarize_workspace_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            'source_kind': item.get('source_kind', ''),
            'source_title': item.get('source_title', ''),
            'section_heading': item.get('section_heading', ''),
            'score': round(float(item.get('score') or 0.0), 2),
            'excerpt': _truncate_text(str(item.get('chunk_text') or '').strip(), 180),
        }
        for item in matches[:MAX_CONTEXT_SNIPPETS]
    ]


def _summarize_employee_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            'retrieval_lane': item.get('retrieval_lane', ''),
            'doc_type': item.get('doc_type', ''),
            'score': round(float(item.get('score') or 0.0), 2),
            'section_heading': item.get('section_heading', ''),
            'excerpt': _truncate_text(str(item.get('chunk_text') or '').strip(), 180),
        }
        for item in matches[:MAX_CONTEXT_SNIPPETS]
    ]


def _summarize_matrix_cells(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summarized = []
    for cell in cells[:MAX_CONTEXT_SNIPPETS]:
        snippets = list(cell.get('provenance_snippets') or [])
        top_snippet = snippets[0] if snippets else {}
        summarized.append(
            {
                'skill_key': cell.get('skill_key', ''),
                'skill_name_en': cell.get('skill_name_en', ''),
                'gap': float(cell.get('gap') or 0.0),
                'confidence': float(cell.get('confidence') or 0.0),
                'excerpt': str(top_snippet.get('excerpt') or cell.get('explanation_summary') or '')[:180],
            }
        )
    return summarized


def _team_action_sort_key(action: dict[str, Any]) -> tuple[int, int, float]:
    urgency_rank = {'high': 0, 'medium': 1, 'low': 2}.get(str(action.get('urgency') or '').strip(), 3)
    action_rank = {'hire': 0, 'de-risk': 1, 'develop': 2, 'move': 3}.get(str(action.get('action_type') or ''), 4)
    severity = float((action.get('supporting_signals') or {}).get('average_gap') or 0.0)
    return (urgency_rank, action_rank, -severity)


def _count_by_key(values) -> dict[str, int]:
    counter: dict[str, int] = {}
    for value in values:
        key = str(value or '').strip()
        if not key:
            continue
        counter[key] = int(counter.get(key, 0)) + 1
    return counter


def _urgency_from_priority(priority: int, *, severe: bool = False) -> str:
    if priority >= 5 or severe:
        return 'high'
    if priority >= HIGH_PRIORITY_THRESHOLD:
        return 'medium'
    return 'low'


def _time_horizon_from_priority(priority: int, *, gap: float) -> str:
    if priority >= 5 or gap >= SEVERE_GAP_THRESHOLD:
        return 'this quarter'
    if priority >= HIGH_PRIORITY_THRESHOLD:
        return 'next cycle'
    return 'next quarter'


def _format_role_label(role_payload: dict[str, Any]) -> str:
    role_name = str(role_payload.get('role_name') or '').strip()
    seniority = str(role_payload.get('seniority') or '').strip()
    if role_name and seniority:
        return f'{role_name} ({seniority})'
    return role_name


def _format_strength_label(cell: dict[str, Any]) -> str:
    return (
        f"{cell.get('skill_name_en', '')}: level {cell.get('current_level', 0.0)}/5 "
        f"with confidence {cell.get('confidence', 0.0)}"
    ).strip()


def _format_gap_label(cell: dict[str, Any]) -> str:
    return (
        f"{cell.get('skill_name_en', '')}: gap {cell.get('gap', 0.0)} "
        f"against target {cell.get('target_level', 0)}"
    ).strip()


def _deterministic_current_role_fit_text(recommendation_payload: dict[str, Any]) -> str:
    best_fit_role = dict(recommendation_payload.get('best_fit_role') or {})
    role_name = str(best_fit_role.get('role_name') or '').strip()
    goal = str(recommendation_payload.get('current_role_goal') or '').strip()
    if role_name:
        if goal == 'adjacent_role_growth':
            return f'Current evidence suggests a strong path toward {role_name}, with some adjacent-role growth potential.'
        return f'Current evidence suggests a workable fit for {role_name}, with clear opportunities to deepen priority skills.'
    return 'Current role fit is still provisional because the matrix does not show a stable target-role match yet.'


def _deterministic_roadmap_alignment_text(recommendation_payload: dict[str, Any]) -> str:
    top_action = next(iter(list(recommendation_payload.get('development_actions') or [])), {})
    initiatives = list(top_action.get('linked_initiatives') or [])
    if initiatives:
        return f"The highest-priority development work supports: {', '.join(initiatives)}."
    return 'The development focus is aligned to the highest-priority gaps in the current matrix.'


def _deterministic_mobility_note(recommendation_payload: dict[str, Any]) -> str:
    mobility_potential = str(recommendation_payload.get('mobility_potential') or '').strip()
    adjacent_roles = list(recommendation_payload.get('adjacent_roles') or [])
    if mobility_potential == 'high' and adjacent_roles:
        return f'There is a credible internal mobility path toward {adjacent_roles[0]}.'
    if mobility_potential == 'medium':
        return 'There is some adjacent-role potential, but it should stay tied to current roadmap needs.'
    return 'The current recommendation is to strengthen core role execution first.'


def _build_column_key(cell: dict[str, Any]) -> str:
    return (
        f"{cell.get('role_profile_uuid', '')}:{cell.get('skill_key', '')}:"
        f"{int(cell.get('target_level') or 0)}"
    )


def _build_plan_export_filename(
    run: DevelopmentPlanRun,
    *,
    extension: str,
    artifact_format: str,
) -> str:
    title_slug = slugify(run.title) or ('team-development-plan' if run.scope == PlanScope.TEAM else 'individual-development-plan')
    if run.scope == PlanScope.INDIVIDUAL and run.employee_id:
        title_slug = slugify(f'{run.employee.full_name}-pdp') or title_slug
    suffix = 'export' if artifact_format == ArtifactFormat.JSON else artifact_format
    return f'{title_slug}-{suffix}.{extension}'


def _build_plan_export_description(run: DevelopmentPlanRun, artifact_format: str) -> str:
    label = 'team development plan' if run.scope == PlanScope.TEAM else 'individual PDP'
    return f'Stage 10 exported {label} in {artifact_format} format.'


def _persist_plan_export_snapshot_sync(run_pk) -> None:
    run = DevelopmentPlanRun.objects.select_related('workspace', 'employee', 'blueprint_run', 'matrix_run').get(pk=run_pk)
    if run.export_snapshot:
        return
    run.export_snapshot = _build_export_snapshot_payload(
        run,
        dict(run.plan_payload or {}),
        dict(run.recommendation_payload or {}),
        dict(run.summary or {}),
    )
    run.save(update_fields=['export_snapshot', 'updated_at'])


async def _cleanup_generated_artifact_media_file(media_file) -> None:
    from server.storage import persistent_client, processing_client

    if media_file.persistent_key:
        try:
            await persistent_client().delete_object(media_file.persistent_key)
        except Exception:
            logger.warning(
                'Failed to clean up orphaned persistent generated artifact %s',
                media_file.persistent_key,
                exc_info=True,
            )
    if media_file.processing_key:
        try:
            await processing_client().delete_object(media_file.processing_key)
        except Exception:
            logger.warning(
                'Failed to clean up orphaned processing generated artifact %s',
                media_file.processing_key,
                exc_info=True,
            )
    try:
        await sync_to_async(media_file.delete)()
    except Exception:
        logger.warning('Failed to delete orphaned generated artifact MediaFile %s', media_file.uuid, exc_info=True)


def _create_plan_artifact_record_sync(
    run_pk,
    media_file_pk,
    artifact_format: str,
    filename: str,
) -> bool:
    from media_storage.models import MediaFile

    run = DevelopmentPlanRun.objects.select_related(
        'workspace', 'employee', 'blueprint_run', 'matrix_run'
    ).get(pk=run_pk)
    media_file = MediaFile.objects.get(pk=media_file_pk)
    artifact, _created = DevelopmentPlanArtifact.objects.get_or_create(
        plan_run=run,
        artifact_format=artifact_format,
        defaults={
            'workspace': run.workspace,
            'employee': run.employee if run.employee_id else None,
            'media_file': media_file,
            'blueprint_run': run.blueprint_run,
            'matrix_run': run.matrix_run,
            'generation_batch_uuid': run.generation_batch_uuid,
            'artifact_scope': run.scope,
            'artifact_version': PLAN_ARTIFACT_VERSION,
            'is_current': bool(run.is_current),
            'metadata': {
                'filename': filename,
                'plan_run_uuid': str(run.uuid),
                'generation_batch_uuid': str(run.generation_batch_uuid or ''),
                'artifact_version': PLAN_ARTIFACT_VERSION,
            },
        },
    )
    return bool(_created)


def _run_effective_completed_at(run: DevelopmentPlanRun):
    return run.completed_at or run.updated_at or run.created_at


def _dedupe_strings(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value or '').strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _truncate_text(value: str, max_chars: int) -> str:
    text = str(value or '').strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + '…'
