from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Optional

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db.models import Q

from company_intake.models import IntakeWorkspace
from employee_assessment.models import AssessmentCycle, AssessmentStatus
from org_context.models import (
    Employee,
    EmployeeRoleMatch,
    EmployeeSkillEvidence,
    EscoOccupationBroaderRelation,
    EscoOccupationSkillRelation,
    EscoSkillBroaderRelation,
    EscoSkillRelation,
    OccupationMapping,
    RoleProfile,
    RoleSkillRequirement,
)
from org_context.skill_catalog import resolve_esco_occupation_sync
from org_context.vector_indexing import retrieve_employee_fused_evidence_sync
from server.embedding_manager import get_embedding_manager_sync
from skill_blueprint.models import SkillBlueprintRun
from skill_blueprint.services import get_current_published_blueprint_run
from tools.openai.structured_client import call_openai_structured

from .models import EvidenceMatrixRun, EvidenceMatrixStatus, EvidenceSourceType
from .weight_profiles import resolve_weight_profile_config

logger = logging.getLogger(__name__)

MATRIX_VERSION = 'stage8-v2'
TOP_GAP_LIMIT = 5
HEATMAP_COLUMN_LIMIT = 12
LOW_CONFIDENCE_THRESHOLD = 0.55
THIN_EVIDENCE_THRESHOLD = 0.45
READY_GAP_THRESHOLD = 0.5
ROLE_MATCH_UNCERTAIN_THRESHOLD = 0.65
NEAR_FIT_GAP_THRESHOLD = 1.0
NEAR_FIT_MIN_GAP = 0.25
ROLE_MATCH_READY_THRESHOLD = 0.70
OCCUPATION_PRIOR_MIN_ROLE_FIT = 0.45
PRIMARY_EVIDENCE_SOURCE_KINDS = {
    'employee_cv',
    'self_assessment',
}
PROVENANCE_CV_DOC_TYPES = ['cv_skill_evidence', 'cv_role_history']
PROVENANCE_SELF_ASSESSMENT_DOC_TYPES = [
    'self_assessment_skill_evidence',
    'self_assessment_example',
]

MATRIX_SUMMARY_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'team_summary': {'type': 'string'},
        'critical_gaps': {'type': 'array', 'items': {'type': 'string'}},
        'coverage_risks': {'type': 'array', 'items': {'type': 'string'}},
        'mobility_opportunities': {'type': 'array', 'items': {'type': 'string'}},
        'incompleteness_flags': {'type': 'array', 'items': {'type': 'string'}},
    },
    'required': [
        'team_summary',
        'critical_gaps',
        'coverage_risks',
        'mobility_opportunities',
        'incompleteness_flags',
    ],
}

EXACT_SUPPORT_TYPE = 'exact'
HIERARCHY_PARENT_SUPPORT_TYPE = 'hierarchy_parent'
HIERARCHY_CHILD_SUPPORT_TYPE = 'hierarchy_child'
RELATED_SUPPORT_TYPE = 'related'
OCCUPATION_PRIOR_SUPPORT_TYPE = 'occupation_prior'

EVIDENCE_MATRIX_CONFIG = getattr(settings, 'EVIDENCE_MATRIX_CONFIG', {})
WEIGHT_PROFILE_CONFIG = resolve_weight_profile_config(EVIDENCE_MATRIX_CONFIG)
REQUESTED_WEIGHT_PROFILE_KEY = str(WEIGHT_PROFILE_CONFIG['requested_key'])
ACTIVE_WEIGHT_PROFILE_KEY = str(WEIGHT_PROFILE_CONFIG['active_key'])
ACTIVE_WEIGHT_PROFILE = dict(WEIGHT_PROFILE_CONFIG['active_profile'])
AVAILABLE_WEIGHT_PROFILES = dict(WEIGHT_PROFILE_CONFIG['available_profiles'])
OCCUPATION_PRIOR_POLICY = str(EVIDENCE_MATRIX_CONFIG.get('OCCUPATION_PRIOR_POLICY') or 'direct_and_ancestor').strip()
OCCUPATION_PRIOR_LIMIT = int(EVIDENCE_MATRIX_CONFIG.get('OCCUPATION_PRIOR_LIMIT') or 2)
OCCUPATION_PRIOR_DISTANCE_DECAY = float(EVIDENCE_MATRIX_CONFIG.get('OCCUPATION_PRIOR_DISTANCE_DECAY') or 0.82)

SUPPORT_TYPE_PRIORITY = {
    EXACT_SUPPORT_TYPE: 4,
    HIERARCHY_CHILD_SUPPORT_TYPE: 3,
    HIERARCHY_PARENT_SUPPORT_TYPE: 2,
    RELATED_SUPPORT_TYPE: 1,
    OCCUPATION_PRIOR_SUPPORT_TYPE: 0,
}
SUPPORT_TYPE_LABELS = {
    EXACT_SUPPORT_TYPE: 'Exact ESCO skill match',
    HIERARCHY_CHILD_SUPPORT_TYPE: 'Child hierarchy match',
    HIERARCHY_PARENT_SUPPORT_TYPE: 'Parent hierarchy match',
    RELATED_SUPPORT_TYPE: 'Related skill match',
    OCCUPATION_PRIOR_SUPPORT_TYPE: 'Occupation-skill prior',
}
SUPPORT_LEVEL_MULTIPLIERS = {
    EXACT_SUPPORT_TYPE: 1.0,
    HIERARCHY_CHILD_SUPPORT_TYPE: float(
        (ACTIVE_WEIGHT_PROFILE.get('level_multipliers') or {}).get(HIERARCHY_CHILD_SUPPORT_TYPE, 0.9)
    ),
    HIERARCHY_PARENT_SUPPORT_TYPE: float(
        (ACTIVE_WEIGHT_PROFILE.get('level_multipliers') or {}).get(HIERARCHY_PARENT_SUPPORT_TYPE, 0.72)
    ),
    RELATED_SUPPORT_TYPE: float(
        (ACTIVE_WEIGHT_PROFILE.get('level_multipliers') or {}).get(RELATED_SUPPORT_TYPE, 0.58)
    ),
}
SUPPORT_CONFIDENCE_MULTIPLIERS = {
    EXACT_SUPPORT_TYPE: 1.0,
    HIERARCHY_CHILD_SUPPORT_TYPE: float(
        (ACTIVE_WEIGHT_PROFILE.get('confidence_multipliers') or {}).get(HIERARCHY_CHILD_SUPPORT_TYPE, 0.88)
    ),
    HIERARCHY_PARENT_SUPPORT_TYPE: float(
        (ACTIVE_WEIGHT_PROFILE.get('confidence_multipliers') or {}).get(HIERARCHY_PARENT_SUPPORT_TYPE, 0.76)
    ),
    RELATED_SUPPORT_TYPE: float(
        (ACTIVE_WEIGHT_PROFILE.get('confidence_multipliers') or {}).get(RELATED_SUPPORT_TYPE, 0.65)
    ),
}
SUPPORT_WEIGHT_MULTIPLIERS = {
    EXACT_SUPPORT_TYPE: 1.0,
    HIERARCHY_CHILD_SUPPORT_TYPE: float(
        (ACTIVE_WEIGHT_PROFILE.get('weight_multipliers') or {}).get(HIERARCHY_CHILD_SUPPORT_TYPE, 0.78)
    ),
    HIERARCHY_PARENT_SUPPORT_TYPE: float(
        (ACTIVE_WEIGHT_PROFILE.get('weight_multipliers') or {}).get(HIERARCHY_PARENT_SUPPORT_TYPE, 0.62)
    ),
    RELATED_SUPPORT_TYPE: float(
        (ACTIVE_WEIGHT_PROFILE.get('weight_multipliers') or {}).get(RELATED_SUPPORT_TYPE, 0.48)
    ),
}
OCCUPATION_PRIOR_ORIGIN_MULTIPLIERS = {
    'direct': float(
        (ACTIVE_WEIGHT_PROFILE.get('occupation_prior_origin_multipliers') or {}).get('direct', 1.0)
    ),
    'ancestor': float(
        (ACTIVE_WEIGHT_PROFILE.get('occupation_prior_origin_multipliers') or {}).get('ancestor', 0.72)
    ),
}


@dataclass(frozen=True)
class MatrixEvidenceSignal:
    signal_key: str
    source_kind: str
    current_level: float
    confidence: float
    weight: float
    raw_current_level: float
    raw_confidence: float
    raw_weight: float
    support_type: str
    support_label: str
    evidence_row: EmployeeSkillEvidence | None = None
    relation_detail: str = ''
    matched_skill_key: str = ''
    matched_skill_name_en: str = ''
    evidence_text: str = ''
    occupation_name_en: str = ''
    occupation_relation_type: str = ''
    prior_origin: str = 'direct'
    prior_distance: int = 0
    role_fit_score: float = 0.0
    occupation_match_score: float = 0.0
    raw_base_current_level: float = 0.0
    raw_base_confidence: float = 0.0
    raw_base_weight: float = 0.0


@dataclass
class EscoMatrixContext:
    ancestors_by_skill_id: dict[Any, set[Any]]
    related_by_skill_id: dict[Any, set[Any]]
    occupation_priors_by_role_skill: dict[tuple[Any, Any], list[dict[str, Any]]]


async def build_evidence_matrix(
    workspace,
    *,
    planning_context=None,
    title: str = 'Second-layer evidence matrix',
    assessment_cycle_uuid: str | None = None,
) -> EvidenceMatrixRun:
    blueprint = await get_current_published_blueprint_run(workspace, planning_context=planning_context)
    if blueprint is None:
        raise ValueError('A published blueprint is required before building the evidence matrix.')

    resolved_cycle = await sync_to_async(_resolve_matrix_assessment_cycle_sync)(
        workspace.pk,
        assessment_cycle_uuid,
        blueprint_run_uuid=str(blueprint.uuid),
        planning_context_pk=getattr(planning_context, 'pk', None),
    )

    run = await sync_to_async(EvidenceMatrixRun.objects.create)(
        workspace=workspace,
        planning_context=planning_context,
        blueprint_run=blueprint,
        title=title,
        status=EvidenceMatrixStatus.RUNNING,
        source_type=EvidenceSourceType.MANUAL,
        matrix_version=MATRIX_VERSION,
        snapshot_key=f'published-blueprint:{blueprint.uuid}:matrix:{MATRIX_VERSION}',
        input_snapshot={
            'blueprint_run_uuid': str(blueprint.uuid),
            'planning_context_uuid': str(getattr(planning_context, 'uuid', '') or ''),
            'matrix_version': MATRIX_VERSION,
            'selected_assessment_cycle_uuid': (
                str(resolved_cycle.uuid) if resolved_cycle is not None else ''
            ),
        },
    )
    try:
        artifacts = await sync_to_async(_build_matrix_artifacts_sync)(
            workspace.pk,
            str(blueprint.uuid),
            str(resolved_cycle.uuid) if resolved_cycle is not None else None,
            planning_context_pk=getattr(planning_context, 'pk', None),
        )
        try:
            summary_payload = await _build_matrix_summary_with_llm(
                blueprint,
                artifacts['matrix_payload'],
                artifacts['risk_payload'],
                artifacts['incompleteness_payload'],
            )
        except Exception as exc:
            logger.warning(
                'Evidence matrix summary fallback activated for workspace %s: %s',
                workspace.slug,
                exc,
                exc_info=True,
            )
            summary_payload = _build_deterministic_summary_payload(
                artifacts['matrix_payload'],
                artifacts['risk_payload'],
                artifacts['incompleteness_payload'],
            )
        await sync_to_async(_finalize_matrix_run_sync)(run.pk, artifacts, summary_payload)
    except Exception as exc:
        logger.exception('Evidence matrix generation failed for workspace %s', workspace.slug)
        await sync_to_async(_fail_matrix_run_sync)(run.pk, str(exc))

    return await sync_to_async(EvidenceMatrixRun.objects.select_related('blueprint_run').get)(pk=run.pk)


async def get_latest_matrix_run(
    workspace,
    *,
    blueprint_run: SkillBlueprintRun | None = None,
    assessment_cycle_uuid: str | None = None,
    planning_context=None,
) -> Optional[EvidenceMatrixRun]:
    resolved_blueprint = blueprint_run
    if resolved_blueprint is None:
        return await sync_to_async(
            lambda: EvidenceMatrixRun.objects.select_related('blueprint_run')
            .filter(
                workspace=workspace,
                **(
                    {'planning_context': planning_context}
                    if planning_context is not None
                    else {'planning_context__isnull': True}
                ),
            )
            .order_by('-updated_at')
            .first()
        )()

    queryset = EvidenceMatrixRun.objects.select_related('blueprint_run').filter(
        workspace=workspace,
        blueprint_run=resolved_blueprint,
        **(
            {'planning_context': planning_context}
            if planning_context is not None
            else {'planning_context__isnull': True}
        ),
    )
    if assessment_cycle_uuid:
        queryset = queryset.filter(
            input_snapshot__selected_assessment_cycle_uuid=str(assessment_cycle_uuid)
        )
    return await sync_to_async(queryset.order_by('-updated_at').first)()


async def get_current_completed_matrix_run(
    workspace,
    *,
    blueprint_run: SkillBlueprintRun | None = None,
    assessment_cycle_uuid: str | None = None,
    planning_context=None,
) -> Optional[EvidenceMatrixRun]:
    resolved_blueprint = blueprint_run or await get_current_published_blueprint_run(
        workspace,
        planning_context=planning_context,
    )
    if resolved_blueprint is None:
        return None
    resolved_cycle = await sync_to_async(_resolve_matrix_assessment_cycle_sync)(
        workspace.pk,
        assessment_cycle_uuid,
        blueprint_run_uuid=str(resolved_blueprint.uuid),
        planning_context_pk=getattr(planning_context, 'pk', None),
    )
    cycle_uuid_value = str(resolved_cycle.uuid) if resolved_cycle is not None else ''
    def _get_current_completed_run():
        queryset = EvidenceMatrixRun.objects.select_related('blueprint_run').filter(
            workspace=workspace,
            status=EvidenceMatrixStatus.COMPLETED,
            blueprint_run=resolved_blueprint,
            **(
                {'planning_context': planning_context}
                if planning_context is not None
                else {'planning_context__isnull': True}
            ),
        )
        if resolved_cycle is not None:
            queryset = queryset.filter(
                input_snapshot__selected_assessment_cycle_uuid=cycle_uuid_value
            )
        return queryset.order_by('-updated_at').first()

    return await sync_to_async(_get_current_completed_run)()


async def build_matrix_run_response(run: EvidenceMatrixRun) -> dict:
    return {
        'uuid': run.uuid,
        'title': run.title,
        'status': run.status,
        'source_type': run.source_type,
        'blueprint_run_uuid': getattr(run.blueprint_run, 'uuid', None),
        'planning_context_uuid': run.planning_context_id,
        'connection_label': run.connection_label,
        'snapshot_key': run.snapshot_key,
        'matrix_version': run.matrix_version,
        'input_snapshot': run.input_snapshot or {},
        'summary_payload': run.summary_payload or {},
        'heatmap_payload': run.heatmap_payload or {},
        'risk_payload': run.risk_payload or {},
        'incompleteness_payload': run.incompleteness_payload or {},
        'matrix_payload': run.matrix_payload or {},
        'created_at': run.created_at,
        'updated_at': run.updated_at,
    }


async def build_matrix_slice_response(run: EvidenceMatrixRun, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        'run_uuid': run.uuid,
        'title': run.title,
        'status': run.status,
        'matrix_version': run.matrix_version,
        'payload': payload or {},
        'updated_at': run.updated_at,
    }


async def get_matrix_employee_payload(run: EvidenceMatrixRun, employee_uuid: str) -> Optional[dict[str, Any]]:
    target = str(employee_uuid)
    for item in list((run.matrix_payload or {}).get('employees') or []):
        if str(item.get('employee_uuid') or '') == target:
            return item
    return None


async def _build_matrix_summary_with_llm(
    blueprint: SkillBlueprintRun,
    matrix_payload: dict[str, Any],
    risk_payload: dict[str, Any],
    incompleteness_payload: dict[str, Any],
) -> dict[str, Any]:
    system_prompt = (
        'You are summarizing a deterministic evidence matrix for a pilot sponsor review.\n\n'

        '## Your task\n'
        'Write a concise, honest summary of the team skills matrix. The matrix scores '
        'were computed deterministically — your job is to narrate and highlight, '
        'not to recompute or re-score.\n\n'

        '## What to include\n'
        '- team_summary: 2-3 sentences. How many employees, which roles are covered, '
        'overall readiness impression. Mention the biggest single gap or strength.\n'
        '- critical_gaps: List the 3-5 most important skill gaps that threaten roadmap '
        'delivery. Each should name the role, the skill, and why it matters '
        '(e.g., "Backend Engineer / API Design: average gap 1.8 with priority 5, '
        'no internal near-fit candidate").\n'
        '- coverage_risks: List concentration risks and single points of failure. '
        'Name the skill and how many ready employees cover it.\n'
        '- mobility_opportunities: List internal employees who are near-fit for '
        'roles they don\'t currently hold. Name the employee and the target role.\n'
        '- incompleteness_flags: List evidence quality issues that limit confidence '
        'in the matrix (e.g., "12 cells have self_report_only evidence", '
        '"3 employees have no CV evidence").\n\n'

        '## Constraints\n'
        '- Stay close to the provided numbers. Do not invent certainty where '
        'confidence is low.\n'
        '- Do not use vague language like "generally strong" or "some gaps exist". '
        'Be specific: name roles, skills, and numbers.\n'
        '- If incompleteness is high, say so explicitly — do not hide it behind '
        'optimistic framing.\n'
        '- Keep each bullet under 20 words. The sponsor will scan this, not read paragraphs.'
    )
    company_name = (blueprint.company_context or {}).get('company_name', '')
    user_prompt = (
        f'## Company: {company_name}\n\n'
        f'## Roadmap context\n{json.dumps(blueprint.roadmap_context, ensure_ascii=False, indent=2)}\n\n'
        f'## Team metrics\n{json.dumps(matrix_payload.get("team_summary", {}), ensure_ascii=False, indent=2)}\n\n'
        f'## Top employee gaps (up to 10)\n{json.dumps(matrix_payload.get("employee_gap_summary", [])[:10], ensure_ascii=False, indent=2)}\n\n'
        f'## Risk indicators\n{json.dumps(risk_payload, ensure_ascii=False, indent=2)}\n\n'
        f'## Evidence completeness\n{json.dumps(incompleteness_payload, ensure_ascii=False, indent=2)}\n\n'
        '## Instructions\n'
        'Summarize the matrix now. Be specific, cite numbers, and flag incompleteness honestly.'
    )
    result = await call_openai_structured(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_name='matrix_team_summary',
        schema=MATRIX_SUMMARY_SCHEMA,
        temperature=0.2,
        max_tokens=1600,
    )
    return result.parsed


def _resolve_matrix_assessment_cycle_sync(
    workspace_pk: int,
    assessment_cycle_uuid: str | None,
    *,
    blueprint_run_uuid: str | None = None,
    planning_context_pk=None,
) -> Optional[AssessmentCycle]:
    current_cycles = (
        AssessmentCycle.objects.filter(
            workspace_id=workspace_pk,
            **(
                {'planning_context_id': planning_context_pk}
                if planning_context_pk is not None
                else {'planning_context__isnull': True}
            ),
        )
        .exclude(status__in=[AssessmentStatus.FAILED, AssessmentStatus.SUPERSEDED])
        .order_by('-updated_at')
    )
    if blueprint_run_uuid:
        current_cycles = current_cycles.filter(blueprint_run_id=blueprint_run_uuid)
    if assessment_cycle_uuid:
        cycle = current_cycles.filter(uuid=assessment_cycle_uuid).first()
        if cycle is None:
            raise ValueError('Selected assessment cycle was not found for this workspace.')
        return cycle
    return current_cycles.first()


def _build_matrix_artifacts_sync(
    workspace_pk,
    blueprint_run_uuid: str,
    assessment_cycle_uuid: str | None = None,
    planning_context_pk=None,
) -> dict[str, Any]:
    selected_cycle = _resolve_matrix_assessment_cycle_sync(
        workspace_pk,
        assessment_cycle_uuid,
        blueprint_run_uuid=blueprint_run_uuid,
        planning_context_pk=planning_context_pk,
    )
    if selected_cycle is None:
        raise ValueError(
            'No active assessment cycle found for this workspace. '
            'Cannot build the matrix without explicit cycle lineage.'
        )
    selected_employee_ids = list(selected_cycle.packs.values_list('employee_id', flat=True))
    employees = list(Employee.objects.filter(workspace_id=workspace_pk, uuid__in=selected_employee_ids).order_by('full_name'))
    role_profiles = list(
        RoleProfile.objects.filter(workspace_id=workspace_pk, blueprint_run_id=blueprint_run_uuid)
        .order_by('name', 'seniority')
    )
    role_matches = list(
        EmployeeRoleMatch.objects.filter(
            workspace_id=workspace_pk,
            **(
                {'planning_context_id': planning_context_pk}
                if planning_context_pk is not None
                else {'planning_context__isnull': True}
            ),
            source_kind='blueprint',
            role_profile__blueprint_run_id=blueprint_run_uuid,
            employee_id__in=selected_employee_ids,
        )
        .select_related('role_profile', 'employee')
        .order_by('employee_id', '-fit_score', 'role_profile__name')
    )
    requirements = list(
        RoleSkillRequirement.objects.filter(
            workspace_id=workspace_pk,
            role_profile__blueprint_run_id=blueprint_run_uuid,
        )
        .select_related('role_profile', 'skill', 'skill__esco_skill')
        .order_by('role_profile_id', '-priority', '-target_level', 'skill__display_name_en')
    )
    occupation_mappings = list(
        OccupationMapping.objects.filter(
            workspace_id=workspace_pk,
            role_profile__blueprint_run_id=blueprint_run_uuid,
        )
        .select_related('role_profile', 'esco_occupation')
        .order_by('role_profile_id', '-match_score', 'occupation_name_en')
    )
    latest_current_cycle = (
        AssessmentCycle.objects.filter(
            workspace_id=workspace_pk,
            **(
                {'planning_context_id': planning_context_pk}
                if planning_context_pk is not None
                else {'planning_context__isnull': True}
            ),
        )
        .exclude(status__in=[AssessmentStatus.FAILED, AssessmentStatus.SUPERSEDED])
        .filter(blueprint_run_id=blueprint_run_uuid)
        .order_by('-updated_at')
        .first()
    )
    evidence_queryset = EmployeeSkillEvidence.objects.filter(
        workspace_id=workspace_pk,
        source_kind__in=sorted(PRIMARY_EVIDENCE_SOURCE_KINDS),
        weight__gt=0,
    ).filter(
        Q(source_kind='employee_cv')
        | Q(source_kind='self_assessment', assessment_cycle=selected_cycle)
        | Q(
            source_kind='self_assessment',
            assessment_cycle__isnull=True,
            metadata__assessment_cycle_uuid=str(selected_cycle.uuid),
        )
    ).filter(
        employee_id__in=selected_employee_ids
    )
    evidence_rows = list(
        evidence_queryset.select_related('skill', 'skill__esco_skill', 'employee').order_by(
            'employee_id', 'skill_id', '-weight', '-updated_at'
        )
    )

    matches_by_employee: dict[int, list[EmployeeRoleMatch]] = defaultdict(list)
    for match in role_matches:
        matches_by_employee[match.employee_id].append(match)

    requirements_by_role: dict[int, list[RoleSkillRequirement]] = defaultdict(list)
    for requirement in requirements:
        requirements_by_role[requirement.role_profile_id].append(requirement)

    evidence_by_employee: dict[int, list[EmployeeSkillEvidence]] = defaultdict(list)
    for row in evidence_rows:
        evidence_by_employee[row.employee_id].append(row)

    esco_context = _build_esco_matrix_context(
        requirements=requirements,
        evidence_rows=evidence_rows,
        occupation_mappings=occupation_mappings,
    )

    input_snapshot = {
        'matrix_version': MATRIX_VERSION,
        'blueprint_run_uuid': blueprint_run_uuid,
        'employee_count': len(employees),
        'published_role_count': len(role_profiles),
        'role_requirement_count': len(requirements),
        'occupation_mapping_count': len(occupation_mappings),
        'role_match_count': len(role_matches),
        'evidence_row_count': len(evidence_rows),
        'evidence_source_counts': _count_by_key([row.source_kind for row in evidence_rows]),
        'active_weight_profile': ACTIVE_WEIGHT_PROFILE_KEY,
        'requested_weight_profile': REQUESTED_WEIGHT_PROFILE_KEY,
        'resolved_weight_profile': dict(ACTIVE_WEIGHT_PROFILE),
        'available_weight_profiles': dict(AVAILABLE_WEIGHT_PROFILES),
        'occupation_prior_policy': OCCUPATION_PRIOR_POLICY,
        'selected_assessment_cycle_uuid': str(selected_cycle.uuid) if selected_cycle is not None else '',
        'selected_assessment_cycle_status': selected_cycle.status if selected_cycle is not None else '',
        'latest_current_assessment_cycle_uuid': (
            str(latest_current_cycle.uuid) if latest_current_cycle is not None else ''
        ),
        'assessment_cycle_uuids_used': sorted(
            {
                str((row.metadata or {}).get('assessment_cycle_uuid') or '').strip()
                for row in evidence_rows
                if str((row.metadata or {}).get('assessment_cycle_uuid') or '').strip()
            }
        ),
    }

    employee_payloads: list[dict[str, Any]] = []
    matrix_cells: list[dict[str, Any]] = []
    employee_gap_summary: list[dict[str, Any]] = []
    employees_with_insufficient_evidence: list[dict[str, Any]] = []

    for employee in employees:
        employee_matches = matches_by_employee.get(employee.pk, [])
        primary_match = employee_matches[0] if employee_matches else None
        adjacent_matches = employee_matches[1:3]
        primary_role = primary_match.role_profile if primary_match else None
        role_requirements = requirements_by_role.get(primary_role.pk, []) if primary_role is not None else []

        cells = [
            _build_matrix_cell_sync(
                employee=employee,
                primary_match=primary_match,
                role_profile=primary_role,
                requirement=requirement,
                employee_evidence_rows=evidence_by_employee.get(employee.pk, []),
                esco_context=esco_context,
            )
            for requirement in role_requirements
        ]
        matrix_cells.extend(cells)

        top_gaps = sorted(
            [cell for cell in cells if float(cell.get('gap') or 0.0) > 0.0],
            key=lambda item: (-float(item.get('gap') or 0.0), -int(item.get('priority') or 0)),
        )[:TOP_GAP_LIMIT]
        average_confidence = round(
            sum(float(cell.get('confidence') or 0.0) for cell in cells) / len(cells),
            2,
        ) if cells else 0.0
        total_gap_score = round(
            sum(float(cell.get('gap') or 0.0) * int(cell.get('priority') or 0) for cell in cells),
            2,
        )
        employee_flags = _dedupe_strings(
            flag for cell in cells for flag in list(cell.get('incompleteness_flags') or [])
        )
        employee_advisory_flags = _dedupe_strings(
            flag for cell in cells for flag in list(cell.get('advisory_flags') or [])
        )
        role_match_status = (
            'unmatched'
            if primary_match is None
            else (
                'uncertain'
                if _normalize_role_fit_score(primary_match.fit_score) < ROLE_MATCH_UNCERTAIN_THRESHOLD
                else 'matched'
            )
        )
        employee_payload = {
            'employee_uuid': str(employee.uuid),
            'full_name': employee.full_name,
            'current_title': employee.current_title,
            'best_fit_role': _serialize_role_match(primary_match),
            'adjacent_roles': [_serialize_role_match(match) for match in adjacent_matches],
            'role_match_status': role_match_status,
            'skills': [_build_employee_skill_row(cell) for cell in cells],
            'top_gaps': [_build_employee_skill_row(cell) for cell in top_gaps],
            'total_gap_score': total_gap_score,
            'average_confidence': average_confidence,
            'insufficient_evidence_flags': employee_flags,
            'advisory_flags': employee_advisory_flags,
            'critical_gap_count': sum(
                1
                for cell in cells
                if float(cell.get('gap') or 0.0) > 0.0 and int(cell.get('priority') or 0) >= 4
            ),
            'insufficient_evidence_count': sum(1 for cell in cells if bool(cell.get('is_incomplete'))),
        }
        employee_payloads.append(employee_payload)
        employee_gap_summary.append(
            {
                'employee_uuid': employee_payload['employee_uuid'],
                'full_name': employee_payload['full_name'],
                'best_fit_role': employee_payload['best_fit_role'],
                'top_gaps': employee_payload['top_gaps'],
                'insufficient_evidence_flags': employee_flags,
                'advisory_flags': employee_advisory_flags,
            }
        )
        if employee_flags or primary_match is None:
            employees_with_insufficient_evidence.append(
                {
                    'employee_uuid': employee_payload['employee_uuid'],
                    'full_name': employee_payload['full_name'],
                    'current_title': employee_payload['current_title'],
                    'best_fit_role': employee_payload['best_fit_role'],
                    'flags': employee_flags or ['role_match_uncertain'],
                }
            )

    _enrich_matrix_cells_with_provenance_sync(
        workspace_pk,
        matrix_cells,
        selected_cycle_uuid=str(selected_cycle.uuid) if selected_cycle is not None else '',
    )

    requirement_stats = _aggregate_requirement_stats(matrix_cells)
    role_coverage = _build_role_coverage(role_profiles, employee_payloads)
    uncovered_roles = [item for item in role_coverage if int(item.get('matched_employee_count') or 0) == 0]
    critical_skill_coverage = _build_critical_skill_coverage(requirement_stats)
    near_fit_candidates = _build_near_fit_candidates(employee_payloads)
    concentration_risks = _build_concentration_risks(matrix_cells)
    top_priority_gaps = _build_top_priority_gaps(requirement_stats)
    esco_support_summary = _build_esco_support_summary(matrix_cells)

    team_summary = {
        'matrix_version': MATRIX_VERSION,
        'employee_count': len(employee_payloads),
        'roles_covered': sorted(
            {
                item['best_fit_role']['role_name']
                for item in employee_payloads
                if item.get('best_fit_role')
            }
        ),
        'uncovered_roles': uncovered_roles,
        'skills_with_average_gap': [
            {
                'skill_key': item['skill_key'],
                'skill_name_en': item['skill_name_en'],
                'role_name': item['role_name'],
                'target_level': item['target_level'],
                'average_gap': item['average_gap'],
                'max_priority': item['max_priority'],
                'employees_meeting_target': item['employees_meeting_target'],
                'employees_below_target': item['employees_below_target'],
            }
            for item in top_priority_gaps[:20]
        ],
        'critical_skill_coverage': critical_skill_coverage,
        'esco_support_summary': esco_support_summary,
        'employees_with_insufficient_evidence': len(employees_with_insufficient_evidence),
        'near_fit_candidate_count': len(near_fit_candidates),
    }

    heatmap_payload = _build_heatmap_payload(employee_payloads, matrix_cells, top_priority_gaps)
    risk_payload = {
        'matrix_version': MATRIX_VERSION,
        'concentration_risks': concentration_risks,
        'near_fit_candidates': near_fit_candidates,
        'top_priority_gaps': top_priority_gaps[:12],
        'esco_support_summary': esco_support_summary,
        'uncovered_roles': uncovered_roles,
        'employees_with_insufficient_evidence': employees_with_insufficient_evidence,
    }
    incompleteness_payload = _build_incompleteness_payload(
        employee_payloads=employee_payloads,
        matrix_cells=matrix_cells,
        employees_with_insufficient_evidence=employees_with_insufficient_evidence,
    )
    matrix_payload = {
        'matrix_version': MATRIX_VERSION,
        'input_snapshot': input_snapshot,
        'employees': employee_payloads,
        'matrix_cells': matrix_cells,
        'team_summary': team_summary,
        'employee_gap_summary': employee_gap_summary,
        'team_risks': risk_payload,
        'incompleteness_summary': incompleteness_payload,
        'heatmap': heatmap_payload,
    }
    return {
        'matrix_payload': matrix_payload,
        'heatmap_payload': heatmap_payload,
        'risk_payload': risk_payload,
        'incompleteness_payload': incompleteness_payload,
        'input_snapshot': input_snapshot,
    }


def _build_matrix_cell_sync(
    *,
    employee: Employee,
    primary_match: EmployeeRoleMatch | None,
    role_profile: RoleProfile | None,
    requirement: RoleSkillRequirement,
    employee_evidence_rows: list[EmployeeSkillEvidence],
    esco_context: EscoMatrixContext,
) -> dict[str, Any]:
    support_signals = _resolve_matrix_support_signals(
        requirement=requirement,
        employee_evidence_rows=employee_evidence_rows,
        role_profile=role_profile,
        primary_match=primary_match,
        esco_context=esco_context,
    )
    current_level = _weighted_level(support_signals)
    evidence_mass = _evidence_mass(support_signals)
    weighted_confidence = _weighted_confidence(support_signals)
    confidence = _fused_cell_confidence(
        weighted_confidence=weighted_confidence,
        evidence_mass=evidence_mass,
        source_diversity=len(
            {
                signal.source_kind
                for signal in support_signals
                if signal.source_kind and signal.source_kind != OCCUPATION_PRIOR_SUPPORT_TYPE
            }
        ),
    )
    role_fit_score = _normalize_role_fit_score(primary_match.fit_score) if primary_match is not None else 0.0
    gap = round(max(0.0, float(requirement.target_level or 0) - current_level), 2)
    source_mix = _build_source_mix(support_signals)
    support_breakdown = _build_support_breakdown(support_signals)
    provenance_snippets = _merge_cell_provenance(
        support_signals=support_signals,
        retrieved_matches=[],
    )
    flags = _build_incompleteness_flags(
        support_signals=support_signals,
        confidence=confidence,
        evidence_mass=evidence_mass,
    )
    advisory_flags = _build_advisory_flags(role_fit_score=role_fit_score)
    evidence_rows_payload = [_build_signal_payload(signal) for signal in support_signals[:5]]
    support_signals_payload = [_build_signal_payload(signal) for signal in support_signals]
    support_type_counts = _count_by_key(signal.support_type for signal in support_signals)
    contributing_row_ids = _dedupe_strings(
        [
            str(signal.evidence_row.uuid)
            for signal in support_signals
            if signal.evidence_row is not None
        ]
    )
    provenance_skill_keys = _dedupe_strings(
        [
            requirement.skill.canonical_key,
            *[
                signal.matched_skill_key
                for signal in support_signals
                if signal.evidence_row is not None and signal.matched_skill_key
            ],
        ]
    )
    return {
        'cell_key': f'{employee.uuid}:{requirement.skill.canonical_key}',
        'employee_uuid': str(employee.uuid),
        'employee_name': employee.full_name,
        'current_title': employee.current_title,
        'role_profile_uuid': str(role_profile.uuid) if role_profile is not None else '',
        'role_name': getattr(role_profile, 'name', ''),
        'role_family': getattr(role_profile, 'family', ''),
        'seniority': getattr(role_profile, 'seniority', ''),
        'role_fit_score': round(role_fit_score, 2),
        'skill_key': requirement.skill.canonical_key,
        'skill_name_en': requirement.skill.display_name_en,
        'skill_name_ru': requirement.skill.display_name_ru,
        'target_level': int(requirement.target_level or 0),
        'current_level': current_level,
        'gap': gap,
        'confidence': confidence,
        'priority': int(requirement.priority or 0),
        'is_required': bool(requirement.is_required),
        'requirement_type': str((requirement.metadata or {}).get('requirement_type') or ''),
        'criticality': str((requirement.metadata or {}).get('criticality') or ''),
        'supported_initiatives': list((requirement.metadata or {}).get('supported_initiatives') or []),
        'evidence_mass': evidence_mass,
        'weighted_evidence_confidence': weighted_confidence,
        'evidence_source_mix': source_mix,
        'esco_support_types': [item['support_type'] for item in support_breakdown],
        'esco_support_breakdown': support_breakdown,
        'support_signal_count': len(support_signals),
        'exact_match_count': int(support_type_counts.get(EXACT_SUPPORT_TYPE, 0)),
        'hierarchy_match_count': int(
            support_type_counts.get(HIERARCHY_CHILD_SUPPORT_TYPE, 0)
            + support_type_counts.get(HIERARCHY_PARENT_SUPPORT_TYPE, 0)
        ),
        'related_match_count': int(support_type_counts.get(RELATED_SUPPORT_TYPE, 0)),
        'occupation_prior_count': int(support_type_counts.get(OCCUPATION_PRIOR_SUPPORT_TYPE, 0)),
        'has_direct_evidence': bool(support_type_counts.get(EXACT_SUPPORT_TYPE, 0)),
        'has_indirect_evidence': any(
            signal.support_type != EXACT_SUPPORT_TYPE
            for signal in support_signals
        ),
        'dominant_evidence_sources': [item['source_kind'] for item in source_mix[:2]],
        'contributing_evidence_row_uuids': contributing_row_ids,
        'evidence_rows': evidence_rows_payload,
        'support_signals': support_signals_payload,
        'incompleteness_flags': flags,
        'advisory_flags': advisory_flags,
        'is_incomplete': bool(flags),
        'provenance_skill_keys': provenance_skill_keys,
        'provenance_snippets': provenance_snippets,
        'explanation_summary': _build_cell_explanation(
            requirement=requirement,
            current_level=current_level,
            confidence=confidence,
            source_mix=source_mix,
            support_breakdown=support_breakdown,
            flags=flags,
            advisory_flags=advisory_flags,
            provenance_snippets=provenance_snippets,
        ),
    }


def _build_esco_matrix_context(
    *,
    requirements: list[RoleSkillRequirement],
    evidence_rows: list[EmployeeSkillEvidence],
    occupation_mappings: list[OccupationMapping],
) -> EscoMatrixContext:
    requirement_esco_skill_ids = {
        requirement.skill.esco_skill_id
        for requirement in requirements
        if requirement.skill.esco_skill_id
    }
    evidence_esco_skill_ids = {
        row.skill.esco_skill_id
        for row in evidence_rows
        if row.skill.esco_skill_id
    }
    relevant_skill_ids = {
        skill_id
        for skill_id in [*requirement_esco_skill_ids, *evidence_esco_skill_ids]
        if skill_id
    }

    ancestors_by_skill_id = _build_esco_ancestor_map(relevant_skill_ids)

    related_by_skill_id: dict[Any, set[Any]] = defaultdict(set)
    if relevant_skill_ids:
        related_relations = list(
            EscoSkillRelation.objects.filter(
                Q(original_skill_id__in=relevant_skill_ids) | Q(related_skill_id__in=relevant_skill_ids)
            ).values_list('original_skill_id', 'related_skill_id')
        )
        for original_skill_id, related_skill_id in related_relations:
            if not original_skill_id or not related_skill_id:
                continue
            related_by_skill_id[original_skill_id].add(related_skill_id)
            related_by_skill_id[related_skill_id].add(original_skill_id)

    best_mapping_by_role: dict[Any, dict[str, Any]] = {}
    for mapping in sorted(
        occupation_mappings,
        key=lambda item: (
            item.role_profile_id,
            -float(item.match_score or 0.0),
            str(item.occupation_name_en or ''),
        ),
    ):
        resolved_occupation_id = mapping.esco_occupation_id
        resolved_occupation_name = str(getattr(mapping.esco_occupation, 'preferred_label', '') or '').strip()
        if resolved_occupation_id is None:
            resolved_occupation, _occupation_match = resolve_esco_occupation_sync(
                str(mapping.occupation_name_en or '').strip(),
                alternatives=[
                    str(getattr(mapping.role_profile, 'name', '') or '').strip(),
                    str(getattr(mapping.role_profile, 'canonical_occupation_key', '') or '').strip(),
                ],
                workspace=getattr(mapping, 'workspace', None),
                role_family_hint=str(getattr(mapping.role_profile, 'family', '') or '').strip(),
            )
            resolved_occupation_id = getattr(resolved_occupation, 'id', None)
            resolved_occupation_name = str(getattr(resolved_occupation, 'preferred_label', '') or '').strip()
        if resolved_occupation_id is None:
            continue
        if mapping.role_profile_id in best_mapping_by_role:
            continue
        best_mapping_by_role[mapping.role_profile_id] = {
            'occupation_id': resolved_occupation_id,
            'occupation_name_en': resolved_occupation_name or str(mapping.occupation_name_en or '').strip(),
            'occupation_match_score': _normalize_role_fit_score(mapping.match_score),
        }

    occupation_priors_by_role_skill: dict[tuple[Any, Any], list[dict[str, Any]]] = defaultdict(list)
    if requirement_esco_skill_ids and best_mapping_by_role:
        expanded_mappings_by_role: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        occupation_ids_to_expand = {
            mapping_info['occupation_id']
            for mapping_info in best_mapping_by_role.values()
            if mapping_info.get('occupation_id')
        }
        occupation_ancestors_by_occupation_id = _build_esco_occupation_ancestor_map(occupation_ids_to_expand)
        for role_profile_id, mapping_info in best_mapping_by_role.items():
            direct_mapping = {
                'occupation_id': mapping_info['occupation_id'],
                'occupation_name_en': mapping_info['occupation_name_en'],
                'occupation_match_score': _normalize_role_fit_score(mapping_info['occupation_match_score']),
                'prior_origin': 'direct',
                'prior_distance': 0,
            }
            expanded_mappings_by_role[role_profile_id].append(direct_mapping)
            if OCCUPATION_PRIOR_POLICY != 'direct_and_ancestor':
                continue
            for ancestor_id, distance in occupation_ancestors_by_occupation_id.get(mapping_info['occupation_id'], {}).items():
                expanded_mappings_by_role[role_profile_id].append(
                    {
                        'occupation_id': ancestor_id,
                        'occupation_name_en': '',
                        'occupation_match_score': round(
                            _normalize_role_fit_score(mapping_info['occupation_match_score'])
                            * (OCCUPATION_PRIOR_DISTANCE_DECAY ** max(1, distance)),
                            2,
                        ),
                        'prior_origin': 'ancestor',
                        'prior_distance': int(distance),
                    }
                )

        role_ids_by_occupation: dict[Any, set[Any]] = defaultdict(set)
        mapping_lookup_by_role_occupation: dict[tuple[Any, Any], dict[str, Any]] = {}
        for role_profile_id, mapping_items in expanded_mappings_by_role.items():
            for mapping_item in mapping_items:
                occupation_id = mapping_item['occupation_id']
                role_ids_by_occupation[occupation_id].add(role_profile_id)
                mapping_lookup_by_role_occupation[(role_profile_id, occupation_id)] = mapping_item
        occupation_skill_relations = list(
            EscoOccupationSkillRelation.objects.filter(
                occupation_id__in=list(role_ids_by_occupation.keys()),
                skill_id__in=list(requirement_esco_skill_ids),
            ).select_related('occupation', 'skill')
        )
        for relation in occupation_skill_relations:
            for role_profile_id in role_ids_by_occupation.get(relation.occupation_id, set()):
                mapping_info = mapping_lookup_by_role_occupation.get((role_profile_id, relation.occupation_id)) or {}
                occupation_priors_by_role_skill[(role_profile_id, relation.skill_id)].append(
                    {
                        'occupation_id': relation.occupation_id,
                        'occupation_name_en': str(
                            mapping_info.get('occupation_name_en')
                            or relation.occupation.preferred_label
                            or ''
                        ).strip(),
                        'relation_type': str(relation.relation_type or '').strip().lower(),
                        'skill_type': str(relation.skill_type or '').strip().lower(),
                        'occupation_match_score': _normalize_role_fit_score(
                            mapping_info.get('occupation_match_score', 0.0)
                        ),
                        'prior_origin': str(mapping_info.get('prior_origin') or 'direct'),
                        'prior_distance': int(mapping_info.get('prior_distance') or 0),
                    }
                )
    for priors in occupation_priors_by_role_skill.values():
        priors.sort(
            key=lambda item: (
                0 if str(item.get('prior_origin') or '') == 'direct' else 1,
                0 if str(item.get('relation_type') or '') == 'essential' else 1,
                -float(item.get('occupation_match_score') or 0.0),
                int(item.get('prior_distance') or 0),
                str(item.get('occupation_name_en') or ''),
            )
        )

    return EscoMatrixContext(
        ancestors_by_skill_id=ancestors_by_skill_id,
        related_by_skill_id=dict(related_by_skill_id),
        occupation_priors_by_role_skill=occupation_priors_by_role_skill,
    )


def _build_esco_ancestor_map(skill_ids: set[Any]) -> dict[Any, set[Any]]:
    if not skill_ids:
        return {}

    broader_by_child: dict[Any, set[Any]] = defaultdict(set)
    queued_children = set(skill_ids)
    queried_children: set[Any] = set()
    while queued_children:
        frontier = queued_children - queried_children
        if not frontier:
            break
        queried_children.update(frontier)
        broader_relations = list(
            EscoSkillBroaderRelation.objects.filter(
                esco_skill_id__in=list(frontier),
                esco_skill__isnull=False,
                broader_skill__isnull=False,
            ).values_list('esco_skill_id', 'broader_skill_id')
        )
        for child_id, parent_id in broader_relations:
            if not child_id or not parent_id:
                continue
            broader_by_child[child_id].add(parent_id)
            if parent_id not in queried_children:
                queued_children.add(parent_id)

    return {
        skill_id: _walk_skill_graph(skill_id, broader_by_child)
        for skill_id in skill_ids
    }


def _build_esco_occupation_ancestor_map(occupation_ids: set[Any]) -> dict[Any, dict[Any, int]]:
    if not occupation_ids:
        return {}

    broader_by_child: dict[Any, set[Any]] = defaultdict(set)
    queued_children = set(occupation_ids)
    queried_children: set[Any] = set()
    while queued_children:
        frontier = queued_children - queried_children
        if not frontier:
            break
        queried_children.update(frontier)
        broader_relations = list(
            EscoOccupationBroaderRelation.objects.filter(
                esco_occupation_id__in=list(frontier),
                esco_occupation__isnull=False,
                broader_occupation__isnull=False,
            ).values_list('esco_occupation_id', 'broader_occupation_id')
        )
        for child_id, parent_id in broader_relations:
            if not child_id or not parent_id:
                continue
            broader_by_child[child_id].add(parent_id)
            if parent_id not in queried_children:
                queued_children.add(parent_id)

    ancestor_distances: dict[Any, dict[Any, int]] = {}
    for occupation_id in occupation_ids:
        distances: dict[Any, int] = {}
        frontier: list[tuple[Any, int]] = [(parent_id, 1) for parent_id in broader_by_child.get(occupation_id, set())]
        while frontier:
            current_id, distance = frontier.pop(0)
            if current_id in distances and distances[current_id] <= distance:
                continue
            distances[current_id] = distance
            for next_id in broader_by_child.get(current_id, set()):
                frontier.append((next_id, distance + 1))
        ancestor_distances[occupation_id] = distances
    return ancestor_distances


def _walk_skill_graph(start_id: Any, adjacency: dict[Any, set[Any]]) -> set[Any]:
    visited: set[Any] = set()
    stack = list(adjacency.get(start_id, set()))
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        for next_skill_id in adjacency.get(current, set()):
            if next_skill_id not in visited:
                stack.append(next_skill_id)
    return visited


def _normalize_role_fit_score(value: Any) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        normalized = 0.0
    if normalized > 1:
        normalized = normalized / 100.0
    return round(max(0.0, min(1.0, normalized)), 2)


def _resolve_matrix_support_signals(
    *,
    requirement: RoleSkillRequirement,
    employee_evidence_rows: list[EmployeeSkillEvidence],
    role_profile: RoleProfile | None,
    primary_match: EmployeeRoleMatch | None,
    esco_context: EscoMatrixContext,
) -> list[MatrixEvidenceSignal]:
    signals_by_key: dict[str, MatrixEvidenceSignal] = {}
    requirement_esco_skill_id = requirement.skill.esco_skill_id
    requirement_ancestors = esco_context.ancestors_by_skill_id.get(requirement_esco_skill_id, set())
    related = esco_context.related_by_skill_id.get(requirement_esco_skill_id, set())

    for row in employee_evidence_rows:
        support_type = _resolve_row_support_type(
            requirement=requirement,
            evidence_row=row,
            requirement_ancestors=requirement_ancestors,
            evidence_ancestors=esco_context.ancestors_by_skill_id.get(row.skill.esco_skill_id, set()),
            related=related,
        )
        if not support_type:
            continue
        signal = _build_row_signal(
            evidence_row=row,
            requirement=requirement,
            support_type=support_type,
        )
        existing = signals_by_key.get(signal.signal_key)
        if existing is None or SUPPORT_TYPE_PRIORITY[signal.support_type] > SUPPORT_TYPE_PRIORITY[existing.support_type]:
            signals_by_key[signal.signal_key] = signal

    occupation_prior_signals = _build_occupation_prior_signals(
        requirement=requirement,
        role_profile=role_profile,
        primary_match=primary_match,
        esco_context=esco_context,
    )
    for signal in occupation_prior_signals:
        signals_by_key[signal.signal_key] = signal

    return sorted(
        signals_by_key.values(),
        key=lambda item: (
            -SUPPORT_TYPE_PRIORITY.get(item.support_type, -1),
            -float(item.weight or 0.0),
            -float(item.confidence or 0.0),
            item.signal_key,
        ),
    )


def _resolve_row_support_type(
    *,
    requirement: RoleSkillRequirement,
    evidence_row: EmployeeSkillEvidence,
    requirement_ancestors: set[Any],
    evidence_ancestors: set[Any],
    related: set[Any],
) -> str:
    if evidence_row.skill_id == requirement.skill_id:
        return EXACT_SUPPORT_TYPE

    requirement_esco_skill_id = requirement.skill.esco_skill_id
    evidence_esco_skill_id = evidence_row.skill.esco_skill_id
    if not requirement_esco_skill_id or not evidence_esco_skill_id:
        return ''
    if evidence_esco_skill_id == requirement_esco_skill_id:
        return EXACT_SUPPORT_TYPE
    if requirement_esco_skill_id in evidence_ancestors:
        return HIERARCHY_CHILD_SUPPORT_TYPE
    if evidence_esco_skill_id in requirement_ancestors:
        return HIERARCHY_PARENT_SUPPORT_TYPE
    if evidence_esco_skill_id in related:
        return RELATED_SUPPORT_TYPE
    return ''


def _build_row_signal(
    *,
    evidence_row: EmployeeSkillEvidence,
    requirement: RoleSkillRequirement,
    support_type: str,
) -> MatrixEvidenceSignal:
    relation_detail = ''
    if support_type == EXACT_SUPPORT_TYPE:
        if evidence_row.skill_id == requirement.skill_id:
            relation_detail = 'Direct evidence on the exact required workspace skill.'
        else:
            relation_detail = 'Direct evidence on a workspace skill mapped to the same ESCO skill.'
    elif support_type == HIERARCHY_CHILD_SUPPORT_TYPE:
        relation_detail = 'Evidence comes from a more specific child skill in the ESCO hierarchy.'
    elif support_type == HIERARCHY_PARENT_SUPPORT_TYPE:
        relation_detail = 'Evidence comes from a broader parent skill in the ESCO hierarchy.'
    elif support_type == RELATED_SUPPORT_TYPE:
        relation_detail = 'Evidence comes from a directly related ESCO skill.'

    return MatrixEvidenceSignal(
        signal_key=f'row:{evidence_row.pk}',
        source_kind=evidence_row.source_kind or 'unknown',
        current_level=round(
            float(evidence_row.current_level or 0.0) * SUPPORT_LEVEL_MULTIPLIERS.get(support_type, 1.0),
            2,
        ),
        confidence=round(
            min(
                1.0,
                float(evidence_row.confidence or 0.0) * SUPPORT_CONFIDENCE_MULTIPLIERS.get(support_type, 1.0),
            ),
            2,
        ),
        weight=round(
            float(evidence_row.weight or 0.0) * SUPPORT_WEIGHT_MULTIPLIERS.get(support_type, 1.0),
            2,
        ),
        raw_current_level=round(float(evidence_row.current_level or 0.0), 2),
        raw_confidence=round(float(evidence_row.confidence or 0.0), 2),
        raw_weight=round(float(evidence_row.weight or 0.0), 2),
        raw_base_current_level=round(float(evidence_row.current_level or 0.0), 2),
        raw_base_confidence=round(float(evidence_row.confidence or 0.0), 2),
        raw_base_weight=round(float(evidence_row.weight or 0.0), 2),
        support_type=support_type,
        support_label=SUPPORT_TYPE_LABELS[support_type],
        evidence_row=evidence_row,
        relation_detail=relation_detail,
        matched_skill_key=evidence_row.skill.canonical_key,
        matched_skill_name_en=evidence_row.skill.display_name_en,
        evidence_text=str(evidence_row.evidence_text or '').strip(),
    )


def _build_occupation_prior_signals(
    *,
    requirement: RoleSkillRequirement,
    role_profile: RoleProfile | None,
    primary_match: EmployeeRoleMatch | None,
    esco_context: EscoMatrixContext,
) -> list[MatrixEvidenceSignal]:
    if role_profile is None or primary_match is None or requirement.skill.esco_skill_id is None:
        return []

    role_fit_score = _normalize_role_fit_score(primary_match.fit_score)
    if role_fit_score < OCCUPATION_PRIOR_MIN_ROLE_FIT:
        return []

    priors = list(
        esco_context.occupation_priors_by_role_skill.get((role_profile.pk, requirement.skill.esco_skill_id), [])
    )
    signals: list[MatrixEvidenceSignal] = []
    for prior in priors[:OCCUPATION_PRIOR_LIMIT]:
        relation_type = str(prior.get('relation_type') or '').strip().lower() or 'optional'
        is_essential = relation_type == 'essential'
        occupation_match_score = _normalize_role_fit_score(prior.get('occupation_match_score', 0.0))
        prior_origin = str(prior.get('prior_origin') or 'direct')
        prior_distance = int(prior.get('prior_distance') or 0)
        origin_multiplier = OCCUPATION_PRIOR_ORIGIN_MULTIPLIERS.get(prior_origin, 1.0)
        blended_support = (0.35 + (role_fit_score * 0.45) + (occupation_match_score * 0.20)) * origin_multiplier
        base_weight = (0.18 if is_essential else 0.10) * blended_support
        base_current_level = min(
            float(requirement.target_level or 0),
            (2.2 if is_essential else 1.4) * blended_support,
        )
        base_confidence = min(
            0.6,
            0.18 + (role_fit_score * 0.18) + (occupation_match_score * 0.14) + (0.06 if is_essential else 0.0),
        )
        weight = round(base_weight, 2)
        current_level = round(
            min(
                float(requirement.target_level or 0),
                base_current_level,
            ),
            2,
        )
        confidence = round(base_confidence, 2)
        if weight <= 0 or current_level <= 0:
            continue
        occupation_name = str(prior.get('occupation_name_en') or '').strip()
        relation_detail = (
            f"{occupation_name or 'Mapped occupation'} lists this skill as {relation_type}."
        )
        if prior_origin == 'ancestor':
            relation_detail = (
                f"{occupation_name or 'Broader occupation'} inherits this skill prior through the ESCO occupation hierarchy."
            )
        signals.append(
            MatrixEvidenceSignal(
                signal_key=f'occupation:{role_profile.pk}:{requirement.skill.esco_skill_id}:{prior.get("occupation_id")}:{relation_type}',
                source_kind=OCCUPATION_PRIOR_SUPPORT_TYPE,
                current_level=current_level,
                confidence=confidence,
                weight=weight,
                raw_current_level=round(base_current_level, 2),
                raw_confidence=round(base_confidence, 2),
                raw_weight=round(base_weight, 2),
                support_type=OCCUPATION_PRIOR_SUPPORT_TYPE,
                support_label=SUPPORT_TYPE_LABELS[OCCUPATION_PRIOR_SUPPORT_TYPE],
                evidence_row=None,
                relation_detail=relation_detail,
                matched_skill_key=requirement.skill.canonical_key,
                matched_skill_name_en=requirement.skill.display_name_en,
                evidence_text='',
                occupation_name_en=occupation_name,
                occupation_relation_type=relation_type,
                prior_origin=prior_origin,
                prior_distance=prior_distance,
                role_fit_score=role_fit_score,
                occupation_match_score=occupation_match_score,
                raw_base_current_level=round(base_current_level / max(origin_multiplier, 1e-9), 2),
                raw_base_confidence=round(base_confidence, 2),
                raw_base_weight=round(base_weight / max(origin_multiplier, 1e-9), 2),
            )
        )
    return signals


def _build_support_breakdown(support_signals: list[MatrixEvidenceSignal]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for signal in support_signals:
        bucket = grouped.setdefault(
            signal.support_type,
            {
                'support_type': signal.support_type,
                'label': signal.support_label,
                'signal_count': 0,
                'total_weight': 0.0,
                'weighted_level_sum': 0.0,
                'weighted_confidence_sum': 0.0,
                'source_kinds': [],
                'matched_skill_names': [],
                'occupation_names': [],
            },
        )
        bucket['signal_count'] += 1
        bucket['total_weight'] += float(signal.weight or 0.0)
        bucket['weighted_level_sum'] += float(signal.current_level or 0.0) * float(signal.weight or 0.0)
        bucket['weighted_confidence_sum'] += float(signal.confidence or 0.0) * float(signal.weight or 0.0)
        bucket['source_kinds'] = _dedupe_strings([*bucket['source_kinds'], signal.source_kind])
        bucket['matched_skill_names'] = _dedupe_strings(
            [*bucket['matched_skill_names'], signal.matched_skill_name_en]
        )
        if signal.occupation_name_en:
            bucket['occupation_names'] = _dedupe_strings(
                [*bucket['occupation_names'], signal.occupation_name_en]
            )

    breakdown: list[dict[str, Any]] = []
    for item in grouped.values():
        total_weight = float(item.pop('total_weight') or 0.0)
        weighted_level_sum = float(item.pop('weighted_level_sum') or 0.0)
        weighted_confidence_sum = float(item.pop('weighted_confidence_sum') or 0.0)
        item['total_weight'] = round(total_weight, 2)
        item['current_level'] = round(weighted_level_sum / total_weight, 2) if total_weight > 0 else 0.0
        item['confidence'] = round(weighted_confidence_sum / total_weight, 2) if total_weight > 0 else 0.0
        breakdown.append(item)
    return sorted(
        breakdown,
        key=lambda item: (
            -SUPPORT_TYPE_PRIORITY.get(str(item.get('support_type') or ''), -1),
            -float(item.get('total_weight') or 0.0),
        ),
    )


def _build_signal_payload(signal: MatrixEvidenceSignal) -> dict[str, Any]:
    return {
        'evidence_row_uuid': str(getattr(signal.evidence_row, 'uuid', '') or ''),
        'source_kind': signal.source_kind,
        'support_type': signal.support_type,
        'support_label': signal.support_label,
        'relation_detail': signal.relation_detail,
        'matched_skill_key': signal.matched_skill_key,
        'matched_skill_name_en': signal.matched_skill_name_en,
        'current_level': round(float(signal.current_level or 0.0), 2),
        'confidence': round(float(signal.confidence or 0.0), 2),
        'weight': round(float(signal.weight or 0.0), 2),
        'raw_current_level': round(float(signal.raw_current_level or 0.0), 2),
        'raw_confidence': round(float(signal.raw_confidence or 0.0), 2),
        'raw_weight': round(float(signal.raw_weight or 0.0), 2),
        'raw_base_current_level': round(float(signal.raw_base_current_level or 0.0), 2),
        'raw_base_confidence': round(float(signal.raw_base_confidence or 0.0), 2),
        'raw_base_weight': round(float(signal.raw_base_weight or 0.0), 2),
        'occupation_name_en': signal.occupation_name_en,
        'occupation_relation_type': signal.occupation_relation_type,
        'prior_origin': signal.prior_origin,
        'prior_distance': int(signal.prior_distance or 0),
        'role_fit_score': round(float(signal.role_fit_score or 0.0), 2),
        'occupation_match_score': round(float(signal.occupation_match_score or 0.0), 2),
        'evidence_text': _truncate_text(str(signal.evidence_text or '').strip(), 180),
    }


def _enrich_matrix_cells_with_provenance_sync(
    workspace_pk,
    matrix_cells: list[dict[str, Any]],
    *,
    selected_cycle_uuid: str = '',
) -> None:
    if not matrix_cells:
        return
    workspace = IntakeWorkspace.objects.get(pk=workspace_pk)
    query_specs = []
    query_texts: list[str] = []
    for cell in matrix_cells:
        query_text = _build_cell_provenance_query_text(cell)
        if not query_text:
            continue
        query_specs.append((cell, query_text))
        query_texts.append(query_text)
    if not query_specs:
        return

    try:
        embedding_manager = get_embedding_manager_sync()
        query_vectors = embedding_manager.embed_batch_sync(query_texts)
    except Exception as exc:
        logger.warning(
            'Matrix provenance batch embedding failed for workspace %s: %s',
            workspace.slug,
            exc,
            exc_info=True,
        )
        query_vectors = [None] * len(query_specs)

    for index, (cell, query_text) in enumerate(query_specs):
        query_vector = query_vectors[index] if index < len(query_vectors) else None
        provenance_skill_keys = _dedupe_strings(list(cell.get('provenance_skill_keys') or []))
        retrieved_matches = _retrieve_cell_provenance(
            workspace=workspace,
            employee_uuid=str(cell.get('employee_uuid') or ''),
            skill_keys=provenance_skill_keys,
            query_text=query_text,
            query_vector=query_vector,
            cycle_uuids=[selected_cycle_uuid] if selected_cycle_uuid else None,
        )
        cell['provenance_snippets'] = _merge_provenance_payloads(
            existing_matches=list(cell.get('provenance_snippets') or []),
            retrieved_matches=retrieved_matches,
        )
        cell.pop('provenance_skill_keys', None)


def _build_cell_provenance_query_text(cell: dict[str, Any]) -> str:
    return ' '.join(
        part
        for part in [
            str(cell.get('current_title') or '').strip(),
            str(cell.get('role_name') or '').strip(),
            str(cell.get('skill_name_en') or cell.get('skill_key') or '').strip(),
            f"target level {int(cell.get('target_level') or 0)}",
            'evidence for roadmap delivery',
        ]
        if part
    ).strip()


def _retrieve_cell_provenance(
    *,
    workspace,
    employee_uuid: str,
    skill_keys: list[str] | None,
    query_text: str,
    query_vector: list[float] | None = None,
    cycle_uuids: list[str] | None = None,
) -> list[dict[str, Any]]:
    matches = retrieve_employee_fused_evidence_sync(
        workspace,
        query_text=query_text,
        query_vector=query_vector,
        employee_uuids=[employee_uuid] if employee_uuid else None,
        cycle_uuids=cycle_uuids,
        skill_keys=_dedupe_strings(skill_keys or []),
        cv_doc_types=PROVENANCE_CV_DOC_TYPES,
        self_assessment_doc_types=PROVENANCE_SELF_ASSESSMENT_DOC_TYPES,
        limit=4,
        min_score=0.15,
        include_contextual_cv_matches=False,
    )
    return [
        {
            'retrieval_lane': item.get('retrieval_lane', ''),
            'doc_type': item.get('doc_type', ''),
            'score': round(float(item.get('score') or 0.0), 2),
            'section_heading': item.get('section_heading', ''),
            'source_title': item.get('source_title', ''),
            'evidence_row_uuid': item.get('evidence_row_uuid', ''),
            'question_id': item.get('question_id', ''),
            'excerpt': _truncate_text(str(item.get('chunk_text') or '').strip(), 200),
        }
        for item in matches[:3]
    ]


def _merge_provenance_payloads(
    *,
    existing_matches: list[dict[str, Any]],
    retrieved_matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*existing_matches, *retrieved_matches]:
        dedupe_key = str(item.get('evidence_row_uuid') or '') or (
            f"{item.get('retrieval_lane', '')}:{item.get('doc_type', '')}:{item.get('excerpt', '')[:80]}"
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        merged.append(item)
        if len(merged) >= 4:
            break
    return merged


def _merge_cell_provenance(
    *,
    support_signals: list[MatrixEvidenceSignal],
    retrieved_matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for signal in support_signals[:3]:
        dedupe_key = signal.signal_key
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        if signal.evidence_row is not None:
            row = signal.evidence_row
            merged.append(
                {
                    'retrieval_lane': 'postgres',
                    'doc_type': row.source_kind,
                    'score': round(float(signal.confidence or 0.0), 2),
                    'section_heading': signal.matched_skill_name_en or row.skill.display_name_en,
                    'source_title': f'{row.source_kind} / {signal.support_label}',
                    'evidence_row_uuid': str(row.uuid),
                    'question_id': str((row.metadata or {}).get('question_id') or ''),
                    'excerpt': _truncate_text(
                        (
                            f"{signal.relation_detail} {str(signal.evidence_text or '').strip()}"
                        ).strip(),
                        200,
                    ),
                }
            )
            continue
        merged.append(
            {
                'retrieval_lane': 'esco',
                'doc_type': OCCUPATION_PRIOR_SUPPORT_TYPE,
                'score': round(float(signal.confidence or 0.0), 2),
                'section_heading': signal.occupation_name_en or signal.support_label,
                'source_title': signal.support_label,
                'evidence_row_uuid': '',
                'question_id': '',
                'excerpt': _truncate_text(signal.relation_detail, 200),
            }
        )
    for item in retrieved_matches:
        dedupe_key = str(item.get('evidence_row_uuid') or '') or (
            f"{item.get('retrieval_lane', '')}:{item.get('doc_type', '')}:{item.get('excerpt', '')[:80]}"
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        merged.append(item)
        if len(merged) >= 4:
            break
    return merged


def _build_incompleteness_flags(
    *,
    support_signals: list[MatrixEvidenceSignal],
    confidence: float,
    evidence_mass: float,
) -> list[str]:
    if not support_signals:
        return ['no_evidence']

    flags: list[str] = []
    direct_signal_count = sum(1 for signal in support_signals if signal.support_type == EXACT_SUPPORT_TYPE)
    actual_source_kinds = {
        signal.source_kind
        for signal in support_signals
        if signal.source_kind and signal.source_kind != OCCUPATION_PRIOR_SUPPORT_TYPE
    }
    if direct_signal_count == 0:
        flags.append('indirect_evidence_only')
    if support_signals and all(signal.support_type == OCCUPATION_PRIOR_SUPPORT_TYPE for signal in support_signals):
        flags.append('occupation_prior_only')
    if len(actual_source_kinds) == 1:
        flags.append('single_source_only')
    if actual_source_kinds == {'self_assessment'}:
        flags.append('self_report_only')
    if actual_source_kinds == {'employee_cv'}:
        flags.append('cv_only')
    if confidence < LOW_CONFIDENCE_THRESHOLD:
        flags.append('low_confidence')
    if evidence_mass < THIN_EVIDENCE_THRESHOLD:
        flags.append('thin_evidence')
    return _dedupe_strings(flags)


def _build_advisory_flags(*, role_fit_score: float) -> list[str]:
    flags: list[str] = []
    if role_fit_score < ROLE_MATCH_UNCERTAIN_THRESHOLD:
        flags.append('role_match_uncertain')
    return flags


def _build_cell_explanation(
    *,
    requirement: RoleSkillRequirement,
    current_level: float,
    confidence: float,
    source_mix: list[dict[str, Any]],
    support_breakdown: list[dict[str, Any]],
    flags: list[str],
    advisory_flags: list[str],
    provenance_snippets: list[dict[str, Any]],
) -> str:
    skill_name = requirement.skill.display_name_en or requirement.skill.canonical_key
    target_level = int(requirement.target_level or 0)
    if 'no_evidence' in flags:
        return f'No direct evidence is recorded yet for {skill_name} against target level {target_level}.'
    sources = ', '.join(item['source_kind'] for item in source_mix[:2]) or 'unknown sources'
    support_summary = ', '.join(item.get('label', '') for item in support_breakdown[:3] if item.get('label'))
    summary = (
        f'Current level {current_level}/5 against target {target_level}/5 '
        f'with confidence {confidence}. Evidence currently comes from {sources}.'
    )
    if support_summary:
        summary += f' ESCO support types used: {support_summary}.'
    if 'indirect_evidence_only' in flags:
        summary += ' This score is inferred from ESCO-related signals rather than exact direct skill evidence.'
    if 'role_match_uncertain' in advisory_flags:
        summary += ' The role-fit signal is still tentative, so treat this as skill evidence rather than a final staffing decision.'
    if provenance_snippets:
        summary += f' Top support: {provenance_snippets[0].get("excerpt", "")}'
    return summary.strip()


def _aggregate_requirement_stats(matrix_cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    aggregated: dict[tuple[str, str, int], dict[str, Any]] = {}
    for cell in matrix_cells:
        role_profile_uuid = str(cell.get('role_profile_uuid') or '')
        skill_key = str(cell.get('skill_key') or '')
        target_level = int(cell.get('target_level') or 0)
        if not role_profile_uuid or not skill_key:
            continue
        bucket = aggregated.setdefault(
            (role_profile_uuid, skill_key, target_level),
            {
                'column_key': f'{role_profile_uuid}:{skill_key}:{target_level}',
                'role_profile_uuid': role_profile_uuid,
                'role_name': cell.get('role_name', ''),
                'seniority': cell.get('seniority', ''),
                'role_family': cell.get('role_family', ''),
                'skill_key': skill_key,
                'skill_name_en': cell.get('skill_name_en', ''),
                'target_level': target_level,
                'gaps': [],
                'priorities': [],
                'confidences': [],
                'employees_meeting_target': 0,
                'employees_below_target': 0,
                'incomplete_count': 0,
            },
        )
        gap = float(cell.get('gap') or 0.0)
        confidence = float(cell.get('confidence') or 0.0)
        bucket['gaps'].append(gap)
        bucket['priorities'].append(int(cell.get('priority') or 0))
        bucket['confidences'].append(confidence)
        if gap <= READY_GAP_THRESHOLD and confidence >= LOW_CONFIDENCE_THRESHOLD and not cell.get('is_incomplete'):
            bucket['employees_meeting_target'] += 1
        else:
            bucket['employees_below_target'] += 1
        if cell.get('is_incomplete'):
            bucket['incomplete_count'] += 1

    requirement_stats = []
    for item in aggregated.values():
        gaps = item.pop('gaps')
        priorities = item.pop('priorities')
        confidences = item.pop('confidences')
        item['average_gap'] = round(sum(gaps) / len(gaps), 2) if gaps else 0.0
        item['average_confidence'] = round(sum(confidences) / len(confidences), 2) if confidences else 0.0
        item['max_priority'] = max(priorities) if priorities else 0
        item['priority_gap_score'] = round(item['average_gap'] * item['max_priority'], 2)
        requirement_stats.append(item)
    return sorted(
        requirement_stats,
        key=lambda row: (-float(row.get('priority_gap_score') or 0.0), row.get('skill_name_en', '')),
    )


def _build_role_coverage(role_profiles: list[RoleProfile], employee_payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matched_counts = _count_by_key(
        payload.get('best_fit_role', {}).get('role_profile_uuid', '')
        for payload in employee_payloads
        if payload.get('best_fit_role')
    )
    role_coverage = []
    for role in role_profiles:
        role_coverage.append(
            {
                'role_profile_uuid': str(role.uuid),
                'role_name': role.name,
                'seniority': role.seniority,
                'role_family': role.family,
                'matched_employee_count': int(matched_counts.get(str(role.uuid), 0)),
            }
        )
    return role_coverage


def _build_critical_skill_coverage(requirement_stats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            'column_key': item['column_key'],
            'role_name': item['role_name'],
            'seniority': item['seniority'],
            'skill_key': item['skill_key'],
            'skill_name_en': item['skill_name_en'],
            'target_level': item['target_level'],
            'average_gap': item['average_gap'],
            'max_priority': item['max_priority'],
            'employees_meeting_target': item['employees_meeting_target'],
            'employees_below_target': item['employees_below_target'],
            'incomplete_count': item['incomplete_count'],
        }
        for item in requirement_stats[:15]
    ]


def _build_near_fit_candidates(employee_payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = []
    for payload in employee_payloads:
        best_fit_role = payload.get('best_fit_role') or {}
        if not best_fit_role:
            continue
        skill_rows = list(payload.get('skills') or [])
        if not skill_rows:
            continue
        weighted_gap = round(
            sum(float(item.get('gap') or 0.0) * int(item.get('priority') or 0) for item in skill_rows)
            / max(1, sum(int(item.get('priority') or 0) for item in skill_rows)),
            2,
        )
        average_confidence = float(payload.get('average_confidence') or 0.0)
        if not (NEAR_FIT_MIN_GAP <= weighted_gap <= NEAR_FIT_GAP_THRESHOLD):
            continue
        if average_confidence < 0.45:
            continue
        candidates.append(
            {
                'employee_uuid': payload.get('employee_uuid', ''),
                'full_name': payload.get('full_name', ''),
                'role_name': best_fit_role.get('role_name', ''),
                'fit_score': float(best_fit_role.get('fit_score') or 0.0),
                'weighted_gap': weighted_gap,
                'average_confidence': average_confidence,
                'top_gaps': list(payload.get('top_gaps') or [])[:3],
            }
        )
    return sorted(candidates, key=lambda item: (float(item['weighted_gap']), -float(item['fit_score'])))


def _build_concentration_risks(matrix_cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for cell in matrix_cells:
        role_profile_uuid = str(cell.get('role_profile_uuid') or '')
        skill_key = str(cell.get('skill_key') or '')
        if not role_profile_uuid or not skill_key:
            continue
        grouped[(role_profile_uuid, skill_key)].append(cell)

    risks = []
    for (_role_profile_uuid, skill_key), cells in grouped.items():
        if max(int(cell.get('priority') or 0) for cell in cells) < 4:
            continue
        ready_cells = [
            cell
            for cell in cells
            if float(cell.get('gap') or 0.0) <= READY_GAP_THRESHOLD
            and float(cell.get('confidence') or 0.0) >= LOW_CONFIDENCE_THRESHOLD
            and not bool(cell.get('is_incomplete'))
        ]
        if len(ready_cells) > 1:
            continue
        risks.append(
            {
                'role_name': cells[0].get('role_name', ''),
                'seniority': cells[0].get('seniority', ''),
                'skill_key': skill_key,
                'skill_name_en': cells[0].get('skill_name_en', ''),
                'ready_employee_count': len(ready_cells),
                'priority': max(int(cell.get('priority') or 0) for cell in cells),
                'employees': [
                    {
                        'employee_uuid': cell.get('employee_uuid', ''),
                        'full_name': cell.get('employee_name', ''),
                        'gap': cell.get('gap', 0.0),
                        'confidence': cell.get('confidence', 0.0),
                    }
                    for cell in sorted(cells, key=lambda row: (float(row.get('gap') or 0.0), -float(row.get('confidence') or 0.0)))[:3]
                ],
            }
        )
    return sorted(risks, key=lambda item: (-int(item['priority']), int(item['ready_employee_count']), item['skill_name_en']))


def _build_top_priority_gaps(requirement_stats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            'column_key': item['column_key'],
            'role_name': item['role_name'],
            'seniority': item['seniority'],
            'role_family': item['role_family'],
            'skill_key': item['skill_key'],
            'skill_name_en': item['skill_name_en'],
            'target_level': item['target_level'],
            'average_gap': item['average_gap'],
            'average_confidence': item['average_confidence'],
            'max_priority': item['max_priority'],
            'priority_gap_score': item['priority_gap_score'],
            'employees_meeting_target': item['employees_meeting_target'],
            'employees_below_target': item['employees_below_target'],
            'incomplete_count': item['incomplete_count'],
        }
        for item in requirement_stats
    ]


def _build_esco_support_summary(matrix_cells: list[dict[str, Any]]) -> dict[str, int]:
    return {
        'cells_with_exact_match': sum(1 for cell in matrix_cells if int(cell.get('exact_match_count') or 0) > 0),
        'cells_with_hierarchy_match': sum(1 for cell in matrix_cells if int(cell.get('hierarchy_match_count') or 0) > 0),
        'cells_with_related_match': sum(1 for cell in matrix_cells if int(cell.get('related_match_count') or 0) > 0),
        'cells_with_occupation_prior': sum(1 for cell in matrix_cells if int(cell.get('occupation_prior_count') or 0) > 0),
        'cells_with_indirect_only_support': sum(
            1
            for cell in matrix_cells
            if 'indirect_evidence_only' in list(cell.get('incompleteness_flags') or [])
        ),
    }


def _build_heatmap_payload(
    employee_payloads: list[dict[str, Any]],
    matrix_cells: list[dict[str, Any]],
    top_priority_gaps: list[dict[str, Any]],
) -> dict[str, Any]:
    skill_columns = top_priority_gaps[:HEATMAP_COLUMN_LIMIT]
    selected_column_keys = {item['column_key'] for item in skill_columns}
    cells_by_employee: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for cell in matrix_cells:
        employee_uuid = str(cell.get('employee_uuid') or '')
        column_key = f"{cell.get('role_profile_uuid', '')}:{cell.get('skill_key', '')}:{int(cell.get('target_level') or 0)}"
        if column_key not in selected_column_keys:
            continue
        cells_by_employee[employee_uuid][column_key] = {
            'column_key': column_key,
            'skill_key': str(cell.get('skill_key') or ''),
            'gap': float(cell.get('gap') or 0.0),
            'current_level': float(cell.get('current_level') or 0.0),
            'target_level': int(cell.get('target_level') or 0),
            'confidence': float(cell.get('confidence') or 0.0),
            'incompleteness_flags': list(cell.get('incompleteness_flags') or []),
        }

    employee_rows = []
    for payload in employee_payloads:
        employee_rows.append(
            {
                'employee_uuid': payload.get('employee_uuid', ''),
                'full_name': payload.get('full_name', ''),
                'current_title': payload.get('current_title', ''),
                'best_fit_role': payload.get('best_fit_role'),
                'total_gap_score': payload.get('total_gap_score', 0.0),
                'average_confidence': payload.get('average_confidence', 0.0),
                'cells': [
                    cells_by_employee.get(str(payload.get('employee_uuid') or ''), {}).get(
                        column['column_key'],
                        {
                            'column_key': column['column_key'],
                            'skill_key': column['skill_key'],
                            'gap': 0.0,
                            'current_level': 0.0,
                            'target_level': int(column.get('target_level') or 0),
                            'confidence': 0.0,
                            'incompleteness_flags': ['not_required'],
                        },
                    )
                    for column in skill_columns
                ],
            }
        )

    return {
        'matrix_version': MATRIX_VERSION,
        'skill_columns': skill_columns,
        'employee_rows': employee_rows,
        'legend': {
            'gap_scale': '0 means at or above target, higher values mean larger remaining gap.',
            'confidence_scale': '0-1 fused confidence from evidence mass, source diversity, and evidence quality.',
        },
    }


def _build_incompleteness_payload(
    *,
    employee_payloads: list[dict[str, Any]],
    matrix_cells: list[dict[str, Any]],
    employees_with_insufficient_evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    flag_counts = _count_by_key(
        flag for cell in matrix_cells for flag in list(cell.get('incompleteness_flags') or [])
    )
    return {
        'matrix_version': MATRIX_VERSION,
        'employee_count': len(employee_payloads),
        'cell_count': len(matrix_cells),
        'employees_with_insufficient_evidence_count': len(employees_with_insufficient_evidence),
        'employees_with_insufficient_evidence': employees_with_insufficient_evidence,
        'flag_counts': flag_counts,
    }


def _build_deterministic_summary_payload(
    matrix_payload: dict[str, Any],
    risk_payload: dict[str, Any],
    incompleteness_payload: dict[str, Any],
) -> dict[str, Any]:
    team_summary = matrix_payload.get('team_summary', {})
    top_gaps = list(risk_payload.get('top_priority_gaps') or [])[:3]
    concentration = list(risk_payload.get('concentration_risks') or [])[:3]
    near_fit = list(risk_payload.get('near_fit_candidates') or [])[:3]
    incomplete_count = int(incompleteness_payload.get('employees_with_insufficient_evidence_count') or 0)
    return {
        'team_summary': (
            f"Built a matrix for {int(team_summary.get('employee_count') or 0)} employees. "
            f"Roles covered: {', '.join(team_summary.get('roles_covered') or []) or 'none yet'}. "
            f'Employees with incomplete evidence: {incomplete_count}.'
        ),
        'critical_gaps': [
            f"{item.get('skill_name_en', '')}: average gap {item.get('average_gap', 0.0)} "
            f"(priority {item.get('max_priority', 0)})"
            for item in top_gaps
        ],
        'coverage_risks': [
            f"{item.get('role_name', '')} / {item.get('skill_name_en', '')}: "
            f"{item.get('ready_employee_count', 0)} ready employee(s)"
            for item in concentration
        ],
        'mobility_opportunities': [
            f"{item.get('full_name', '')} is near-fit for {item.get('role_name', '')} "
            f'with weighted gap {item.get("weighted_gap", 0.0)}'
            for item in near_fit
        ],
        'incompleteness_flags': [
            f"{flag}: {count}"
            for flag, count in sorted((incompleteness_payload.get('flag_counts') or {}).items())
            if flag
        ],
    }


def _serialize_role_match(match: EmployeeRoleMatch | None) -> dict[str, Any] | None:
    if match is None:
        return None
    return {
        'role_profile_uuid': str(match.role_profile.uuid),
        'role_name': match.role_profile.name,
        'seniority': match.role_profile.seniority,
        'fit_score': _normalize_role_fit_score(match.fit_score),
        'role_family': match.role_profile.family,
        'reason': match.rationale,
    }


def _build_employee_column_key(cell: dict[str, Any]) -> str:
    return (
        f"{cell.get('role_profile_uuid', '')}:"
        f"{cell.get('skill_key', '')}:"
        f"{int(cell.get('target_level') or 0)}"
    )


def _build_column_key(cell: dict[str, Any]) -> str:
    return _build_employee_column_key(cell)


def _build_employee_skill_row(cell: dict[str, Any]) -> dict[str, Any]:
    return {
        'column_key': cell.get('column_key') or _build_employee_column_key(cell),
        'role_profile_uuid': cell.get('role_profile_uuid', ''),
        'skill_key': cell.get('skill_key', ''),
        'skill_name_en': cell.get('skill_name_en', ''),
        'skill_name_ru': cell.get('skill_name_ru', ''),
        'target_level': int(cell.get('target_level') or 0),
        'current_level': float(cell.get('current_level') or 0.0),
        'gap': float(cell.get('gap') or 0.0),
        'confidence': float(cell.get('confidence') or 0.0),
        'priority': int(cell.get('priority') or 0),
        'evidence_sources': [item['source_kind'] for item in list(cell.get('evidence_source_mix') or [])],
        'esco_support_types': list(cell.get('esco_support_types') or []),
        'esco_support_breakdown': list(cell.get('esco_support_breakdown') or []),
        'incompleteness_flags': list(cell.get('incompleteness_flags') or []),
        'explanation_summary': cell.get('explanation_summary', ''),
    }


def _build_source_mix(support_signals: list[MatrixEvidenceSignal]) -> list[dict[str, Any]]:
    aggregated: dict[str, dict[str, Any]] = {}
    for signal in support_signals:
        source_kind = signal.source_kind or 'unknown'
        current_level = float(signal.current_level or 0.0)
        weight = float(signal.weight or 0.0)
        bucket = aggregated.setdefault(
            source_kind,
            {
                'source_kind': source_kind,
                'total_weight': 0.0,
                'row_count': 0,
                'weighted_level_sum': 0.0,
                'level_sum': 0.0,
                'support_types': [],
            },
        )
        bucket['total_weight'] += weight
        bucket['row_count'] += 1
        bucket['weighted_level_sum'] += current_level * weight
        bucket['level_sum'] += current_level
        bucket['support_types'] = _dedupe_strings([*bucket['support_types'], signal.support_type])
    return sorted(
        [
            {
                'source_kind': source_kind,
                'total_weight': round(values['total_weight'], 2),
                'row_count': values['row_count'],
                'support_types': list(values.get('support_types') or []),
                'current_level': round(
                    (
                        values['weighted_level_sum'] / values['total_weight']
                        if values['total_weight'] > 0
                        else values['level_sum'] / values['row_count']
                    ),
                    2,
                ),
            }
            for source_kind, values in aggregated.items()
        ],
        key=lambda row: (-float(row['total_weight']), row['source_kind']),
    )


def _weighted_level(support_signals: list[MatrixEvidenceSignal]) -> float:
    if not support_signals:
        return 0.0
    weighted_sum = 0.0
    total_weight = 0.0
    for signal in support_signals:
        weight = float(signal.weight or 0.0)
        weighted_sum += float(signal.current_level or 0.0) * weight
        total_weight += weight
    if total_weight <= 0:
        return 0.0
    return round(weighted_sum / total_weight, 2)


def _weighted_confidence(support_signals: list[MatrixEvidenceSignal]) -> float:
    if not support_signals:
        return 0.0
    weighted_sum = 0.0
    total_weight = 0.0
    for signal in support_signals:
        weight = float(signal.weight or 0.0)
        weighted_sum += float(signal.confidence or 0.0) * weight
        total_weight += weight
    if total_weight <= 0:
        return 0.0
    return round(weighted_sum / total_weight, 2)


def _evidence_mass(support_signals: list[MatrixEvidenceSignal]) -> float:
    return round(sum(float(signal.weight or 0.0) for signal in support_signals), 2)


def _fused_cell_confidence(*, weighted_confidence: float, evidence_mass: float, source_diversity: int) -> float:
    diversity_score = min(1.0, max(0.0, source_diversity) / 2.0)
    confidence = (
        float(weighted_confidence or 0.0) * 0.65
        + min(1.0, float(evidence_mass or 0.0)) * 0.25
        + diversity_score * 0.10
    )
    return round(min(1.0, confidence), 2)


def _count_by_key(values) -> dict[str, int]:
    counter: dict[str, int] = {}
    for value in values:
        key = str(value or '').strip()
        if not key:
            continue
        counter[key] = int(counter.get(key, 0)) + 1
    return counter


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


def _finalize_matrix_run_sync(run_pk, artifacts: dict[str, Any], summary_payload: dict[str, Any]) -> None:
    run = EvidenceMatrixRun.objects.get(pk=run_pk)
    run.status = EvidenceMatrixStatus.COMPLETED
    run.matrix_version = MATRIX_VERSION
    run.input_snapshot = artifacts.get('input_snapshot', {})
    run.summary_payload = summary_payload
    run.heatmap_payload = artifacts.get('heatmap_payload', {})
    run.risk_payload = artifacts.get('risk_payload', {})
    run.incompleteness_payload = artifacts.get('incompleteness_payload', {})
    run.matrix_payload = artifacts.get('matrix_payload', {})
    run.save(
        update_fields=[
            'status',
            'matrix_version',
            'input_snapshot',
            'summary_payload',
            'heatmap_payload',
            'risk_payload',
            'incompleteness_payload',
            'matrix_payload',
            'updated_at',
        ]
    )


def _fail_matrix_run_sync(run_pk, error_message: str) -> None:
    run = EvidenceMatrixRun.objects.get(pk=run_pk)
    run.status = EvidenceMatrixStatus.FAILED
    run.summary_payload = {'error_message': error_message}
    run.save(update_fields=['status', 'summary_payload', 'updated_at'])
