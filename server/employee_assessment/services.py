from __future__ import annotations

import json
import logging
import re
from collections import Counter
from decimal import Decimal
from typing import Any, Optional

from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from org_context.models import Employee, EmployeeRoleMatch, EmployeeSkillEvidence, RoleSkillRequirement
from org_context.skill_catalog import ensure_workspace_skill_sync, normalize_skill_seed
from org_context.vector_indexing import (
    index_employee_assessment_pack_sync,
    retrieve_employee_cv_evidence_sync,
)
from skill_blueprint.models import SkillBlueprintRun
from skill_blueprint.services import get_current_published_blueprint_run
from tools.openai.structured_client import call_openai_structured

from .models import (
    AssessmentCycle,
    AssessmentPackStatus,
    AssessmentStatus,
    EmployeeAssessmentPack,
)

logger = logging.getLogger(__name__)

ASSESSMENT_PACK_VERSION = 'stage7-v1'
SELF_ASSESSMENT_SOURCE_KIND = 'self_assessment'
SELF_ASSESSMENT_SOURCE_WEIGHT = 0.55
MIN_TOTAL_QUESTIONS = 8
MAX_TOTAL_QUESTIONS = 12
MIN_TARGETED_QUESTIONS = 6
MAX_TARGETED_QUESTIONS = 10
TERMINAL_PACK_STATUSES = {
    AssessmentPackStatus.SUBMITTED,
    AssessmentPackStatus.COMPLETED,
}
ACTIVE_CYCLE_STATUSES = {
    AssessmentStatus.DRAFT,
    AssessmentStatus.GENERATED,
    AssessmentStatus.RUNNING,
}
DEFAULT_CONTEXT_ROLE_MATCH_THRESHOLD = 0.50

ASSESSMENT_PACK_WORDING_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'introduction': {'type': 'string'},
        'hidden_skills_prompt': {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'question_id': {'type': 'string'},
                'prompt': {'type': 'string'},
            },
            'required': ['question_id', 'prompt'],
        },
        'aspiration_prompt': {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'question_id': {'type': 'string'},
                'prompt': {'type': 'string'},
            },
            'required': ['question_id', 'prompt'],
        },
        'targeted_questions': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'question_id': {'type': 'string'},
                    'prompt': {'type': 'string'},
                    'optional_example_prompt': {'type': 'string'},
                },
                'required': ['question_id', 'prompt', 'optional_example_prompt'],
            },
        },
        'closing_prompt': {'type': 'string'},
    },
    'required': [
        'introduction',
        'hidden_skills_prompt',
        'aspiration_prompt',
        'targeted_questions',
        'closing_prompt',
    ],
}


async def generate_assessment_cycle(
    workspace,
    *,
    planning_context=None,
    title: str = 'Initial assessment cycle',
    selected_employee_uuids: list[str] | None = None,
) -> AssessmentCycle:
    blueprint = await get_current_published_blueprint_run(workspace, planning_context=planning_context)
    if blueprint is None:
        raise ValueError('A published blueprint is required before generating employee assessments.')

    employees = await sync_to_async(_resolve_assessment_cycle_employees_sync)(
        workspace.pk,
        str(blueprint.uuid),
        planning_context_pk=getattr(planning_context, 'pk', None),
        selected_employee_uuids=selected_employee_uuids or [],
    )
    if not employees:
        if selected_employee_uuids:
            raise ValueError('Assessment generation requires at least one valid employee selection.')
        if planning_context is not None:
            raise ValueError('Assessment generation requires at least one matched employee in scope.')
        raise ValueError('Assessment generation requires at least one employee in scope.')

    cycle = await sync_to_async(AssessmentCycle.objects.create)(
        workspace=workspace,
        planning_context=planning_context,
        title=title,
        status=AssessmentStatus.RUNNING,
        blueprint_run=blueprint,
        uses_self_report=True,
        uses_performance_reviews=False,
        uses_feedback_360=False,
        uses_skill_tests=False,
        configuration={
            **_build_cycle_configuration(blueprint),
            'target_employee_uuids': [str(employee.uuid) for employee in employees],
            'planning_context_uuid': str(getattr(planning_context, 'uuid', '') or ''),
            'selected_by_operator': bool(selected_employee_uuids),
        },
    )

    try:
        for employee in employees:
            pack_plan = await sync_to_async(_build_pack_plan_sync)(
                employee.pk,
                str(blueprint.uuid),
                dict(cycle.configuration or {}),
            )
            phrased_pack = await _phrase_assessment_pack_with_llm(pack_plan)
            questionnaire_payload = _compose_questionnaire_payload(pack_plan, phrased_pack)
            await sync_to_async(_upsert_employee_assessment_pack_sync)(
                cycle.pk,
                employee.pk,
                questionnaire_payload,
                pack_plan.get('selection_summary', {}),
            )
        await sync_to_async(_finalize_assessment_cycle_sync)(cycle.pk)
        await sync_to_async(_supersede_previous_cycles_sync)(
            workspace.pk,
            str(cycle.uuid),
            getattr(planning_context, 'pk', None),
        )
    except Exception as exc:
        logger.exception(
            'Assessment generation failed for workspace %s',
            workspace.slug,
        )
        await sync_to_async(_fail_assessment_cycle_sync)(cycle.pk, str(exc))

    return await sync_to_async(
        AssessmentCycle.objects.select_related('blueprint_run').get
    )(pk=cycle.pk)


async def regenerate_assessment_cycle(
    workspace,
    *,
    planning_context=None,
    title: str = 'Regenerated assessment cycle',
    selected_employee_uuids: list[str] | None = None,
) -> AssessmentCycle:
    return await generate_assessment_cycle(
        workspace,
        planning_context=planning_context,
        title=title,
        selected_employee_uuids=selected_employee_uuids,
    )


async def get_latest_cycle(workspace, *, planning_context=None) -> Optional[AssessmentCycle]:
    return await sync_to_async(
        lambda: AssessmentCycle.objects.select_related('blueprint_run')
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


async def get_current_cycle(workspace, *, planning_context=None) -> Optional[AssessmentCycle]:
    return await sync_to_async(
        lambda: AssessmentCycle.objects.select_related('blueprint_run')
        .filter(
            workspace=workspace,
            **(
                {'planning_context': planning_context}
                if planning_context is not None
                else {'planning_context__isnull': True}
            ),
        )
        .exclude(status__in=[AssessmentStatus.SUPERSEDED, AssessmentStatus.FAILED])
        .order_by('-updated_at')
        .first()
    )()


async def list_cycle_packs(cycle: AssessmentCycle) -> list[EmployeeAssessmentPack]:
    return await sync_to_async(list)(
        EmployeeAssessmentPack.objects.filter(cycle=cycle)
        .select_related('employee', 'cycle')
        .order_by('employee__full_name')
    )


async def get_pack_by_uuid(pack_uuid: str, *, mark_opened: bool = False) -> Optional[EmployeeAssessmentPack]:
    pack = await sync_to_async(
        lambda: EmployeeAssessmentPack.objects.select_related(
            'employee',
            'cycle',
            'cycle__workspace',
            'cycle__blueprint_run',
        ).filter(uuid=pack_uuid).first()
    )()
    if pack is None:
        return None
    if mark_opened:
        await sync_to_async(_mark_pack_opened_sync)(pack.pk)
        pack = await sync_to_async(
            EmployeeAssessmentPack.objects.select_related(
                'employee',
                'cycle',
                'cycle__workspace',
                'cycle__blueprint_run',
            ).get
        )(pk=pack.pk)
    return pack


async def open_assessment_pack(pack: EmployeeAssessmentPack) -> EmployeeAssessmentPack:
    await sync_to_async(_mark_pack_opened_sync)(pack.pk)
    return await sync_to_async(
        EmployeeAssessmentPack.objects.select_related(
            'employee',
            'cycle',
            'cycle__workspace',
            'cycle__blueprint_run',
        ).get
    )(pk=pack.pk)


async def get_assessment_status(workspace, *, planning_context=None) -> dict:
    return await sync_to_async(_build_assessment_status_sync)(
        workspace.pk,
        workspace.slug,
        getattr(planning_context, 'pk', None),
    )


async def get_latest_submitted_pack(employee: Employee) -> Optional[EmployeeAssessmentPack]:
    return await sync_to_async(
        lambda: EmployeeAssessmentPack.objects.filter(
            employee=employee,
            status__in=list(TERMINAL_PACK_STATUSES),
        )
        .select_related('cycle')
        .order_by('-submitted_at', '-updated_at')
        .first()
    )()


async def submit_assessment_pack_response(
    pack: EmployeeAssessmentPack,
    submission_payload: dict,
) -> EmployeeAssessmentPack:
    await sync_to_async(_apply_pack_response_sync)(pack.pk, submission_payload or {})
    return await sync_to_async(
        EmployeeAssessmentPack.objects.select_related(
            'employee',
            'cycle',
            'cycle__workspace',
            'cycle__blueprint_run',
        ).get
    )(pk=pack.pk)


async def build_cycle_response(cycle: AssessmentCycle) -> dict:
    pack_count = await sync_to_async(cycle.packs.count)()
    return {
        'uuid': cycle.uuid,
        'title': cycle.title,
        'status': cycle.status,
        'blueprint_run_uuid': getattr(cycle.blueprint_run, 'uuid', None),
        'planning_context_uuid': cycle.planning_context_id,
        'uses_self_report': cycle.uses_self_report,
        'uses_performance_reviews': cycle.uses_performance_reviews,
        'uses_feedback_360': cycle.uses_feedback_360,
        'uses_skill_tests': cycle.uses_skill_tests,
        'configuration': cycle.configuration or {},
        'result_summary': cycle.result_summary or {},
        'pack_count': pack_count,
        'created_at': cycle.created_at,
        'updated_at': cycle.updated_at,
    }


async def build_pack_response(pack: EmployeeAssessmentPack) -> dict:
    return {
        'uuid': pack.uuid,
        'cycle_uuid': pack.cycle_id,
        'employee_uuid': pack.employee_id,
        'employee_name': pack.employee.full_name,
        'status': pack.status,
        'title': pack.title,
        'questionnaire_version': pack.questionnaire_version,
        'questionnaire_payload': pack.questionnaire_payload or {},
        'selection_summary': pack.selection_summary or {},
        'response_payload': pack.response_payload or {},
        'fused_summary': pack.fused_summary or {},
        'opened_at': pack.opened_at,
        'submitted_at': pack.submitted_at,
        'created_at': pack.created_at,
        'updated_at': pack.updated_at,
    }


def _build_cycle_configuration(blueprint: SkillBlueprintRun) -> dict[str, Any]:
    assessment_plan = dict(blueprint.assessment_plan or {})
    question_count_target = _clamp_int(
        assessment_plan.get('per_employee_question_count'),
        default=8,
        minimum=MIN_TOTAL_QUESTIONS,
        maximum=MAX_TOTAL_QUESTIONS,
    )
    return {
        'schema_version': ASSESSMENT_PACK_VERSION,
        'question_count_target': question_count_target,
        'targeted_question_cap': min(
            MAX_TARGETED_QUESTIONS,
            max(MIN_TARGETED_QUESTIONS, question_count_target - 2),
        ),
        'question_themes': list(assessment_plan.get('question_themes') or []),
        'global_notes': str(assessment_plan.get('global_notes') or '').strip(),
        'source_blueprint_uuid': str(blueprint.uuid),
    }


def _resolve_assessment_cycle_employees_sync(
    workspace_pk,
    blueprint_run_uuid: str | None,
    *,
    planning_context_pk=None,
    selected_employee_uuids: list[str] | None = None,
) -> list[Employee]:
    queryset = Employee.objects.filter(workspace_id=workspace_pk).order_by('full_name')
    if selected_employee_uuids:
        return list(queryset.filter(uuid__in=[value for value in selected_employee_uuids if value]))
    if planning_context_pk is None or not blueprint_run_uuid:
        return list(queryset)
    employee_ids = EmployeeRoleMatch.objects.filter(
        workspace_id=workspace_pk,
        planning_context_id=planning_context_pk,
        source_kind='blueprint',
        role_profile__blueprint_run_id=blueprint_run_uuid,
        fit_score__gte=DEFAULT_CONTEXT_ROLE_MATCH_THRESHOLD,
    ).values_list('employee_id', flat=True)
    return list(queryset.filter(uuid__in=employee_ids).distinct())


async def count_default_assessment_cycle_employees(
    workspace,
    *,
    planning_context=None,
) -> int:
    blueprint = await get_current_published_blueprint_run(workspace, planning_context=planning_context)
    if blueprint is None:
        return 0
    employees = await sync_to_async(_resolve_assessment_cycle_employees_sync)(
        workspace.pk,
        str(blueprint.uuid),
        planning_context_pk=getattr(planning_context, 'pk', None),
        selected_employee_uuids=[],
    )
    return len(employees)


def _build_pack_plan_sync(
    employee_pk,
    blueprint_run_uuid: str,
    configuration: dict[str, Any],
) -> dict[str, Any]:
    employee = Employee.objects.get(pk=employee_pk)
    blueprint = SkillBlueprintRun.objects.get(pk=blueprint_run_uuid)
    role_matches = list(
        EmployeeRoleMatch.objects.filter(
            employee=employee,
            source_kind='blueprint',
            role_profile__blueprint_run_id=blueprint_run_uuid,
        )
        .select_related('role_profile')
        .order_by('-fit_score', 'role_profile__name')[:3]
    )
    primary_match = role_matches[0] if role_matches else None
    primary_role = primary_match.role_profile if primary_match else None
    targeted_cap = int(configuration.get('targeted_question_cap') or 8)

    candidate_selection = _collect_targeted_question_candidates(
        employee=employee,
        blueprint=blueprint,
        primary_role=primary_role,
        targeted_cap=targeted_cap,
    )
    candidate_rows = list(candidate_selection.get('selected_candidates') or [])
    targeted_questions = candidate_rows[:targeted_cap]
    hidden_prompt = {
        'question_id': 'hidden-skills',
        'question_type': 'hidden_skills',
        'why_asked': 'CVs and existing evidence may miss recent or adjacent skills.',
        'prompt_title': 'Hidden skills',
    }
    aspiration_prompt = {
        'question_id': 'aspiration',
        'question_type': 'aspiration',
        'why_asked': 'Adjacent-role interests help identify mobility and development opportunities.',
        'prompt_title': 'Aspirations',
    }
    selection_summary = {
        'schema_version': ASSESSMENT_PACK_VERSION,
        'question_count_target': int(configuration.get('question_count_target') or 8),
        'targeted_question_cap': targeted_cap,
        'targeted_question_count': len(targeted_questions),
        'primary_role': _serialize_role_match(primary_match),
        'adjacent_roles': [_serialize_role_match(match) for match in role_matches[1:]],
        'question_themes': list(configuration.get('question_themes') or []),
        'targeted_skill_keys': [item.get('skill_key', '') for item in targeted_questions],
        'skipped_due_to_strong_evidence': list(candidate_selection.get('skipped_candidates') or [])[:10],
        'selection_rules': [
            'Prefer roadmap-critical skills with the largest target-vs-current gap.',
            'Boost low-confidence evidence and weak source diversity.',
            'Suppress questions for skills already strongly evidenced in CV or prior evidence.',
            'Keep one question per skill and cap targeted prompts to a short pack.',
        ],
        'used_cv_retrieval': any(
            bool(item.get('retrieved_cv_matches'))
            for item in [*candidate_rows, *(candidate_selection.get('skipped_candidates') or [])]
        ),
    }
    return {
        'employee_uuid': str(employee.uuid),
        'employee_name': employee.full_name,
        'current_title': employee.current_title,
        'question_count_target': int(configuration.get('question_count_target') or 8),
        'primary_role': _serialize_role_match(primary_match),
        'adjacent_roles': [_serialize_role_match(match) for match in role_matches[1:]],
        'hidden_skills_prompt': hidden_prompt,
        'aspiration_prompt': aspiration_prompt,
        'targeted_questions': targeted_questions,
        'selection_summary': selection_summary,
        'global_notes': str(configuration.get('global_notes') or '').strip(),
        'question_themes': list(configuration.get('question_themes') or []),
    }


def _collect_targeted_question_candidates(
    *,
    employee: Employee,
    blueprint: SkillBlueprintRun,
    primary_role,
    targeted_cap: int,
) -> dict[str, list[dict[str, Any]]]:
    raw_candidates: list[dict[str, Any]] = []
    if primary_role is not None:
        requirements = list(
            RoleSkillRequirement.objects.filter(role_profile=primary_role)
            .select_related('skill')
            .order_by('-priority', '-target_level', 'skill__display_name_en')
        )
        for requirement in requirements:
            raw_candidates.append(_build_requirement_candidate(employee, primary_role, requirement))

    if not raw_candidates:
        for item in list(blueprint.required_skill_set or [])[: max(targeted_cap * 2, 12)]:
            skill_key = str(item.get('canonical_key') or '').strip()
            if not skill_key:
                continue
            existing_skill = getattr(employee.workspace.skills.filter(canonical_key=skill_key).first(), 'display_name_en', '')  # type: ignore[attr-defined]
            raw_candidates.append(
                {
                    'question_id': f'skill:{skill_key}',
                    'question_type': 'targeted_skill',
                    'skill_key': skill_key,
                    'skill_name_en': str(item.get('skill_name_en') or existing_skill or skill_key).strip(),
                    'skill_name_ru': str(item.get('skill_name_ru') or '').strip(),
                    'target_level': _clamp_int(item.get('max_target_level'), default=3, minimum=1, maximum=5),
                    'priority': _clamp_int(item.get('max_priority'), default=3, minimum=1, maximum=5),
                    'current_level': 0.0,
                    'current_confidence': 0.0,
                    'current_evidence_mass': 0.0,
                    'evidence_sources': [],
                    'existing_evidence_summary': 'No direct evidence recorded yet.',
                    'role_name': '',
                    'seniority': '',
                    'supported_initiatives': list(item.get('supported_initiatives') or []),
                    'requirement_type': ', '.join(item.get('requirement_types') or []),
                    'criticality': _criticality_from_text(str(item.get('criticality') or 'medium')),
                    'gap': float(_clamp_int(item.get('max_target_level'), default=3, minimum=1, maximum=5)),
                    'selection_score': 0.0,
                }
            )

    if not raw_candidates:
        return {
            'selected_candidates': [],
            'skipped_candidates': [],
        }

    enriched_candidates: list[dict[str, Any]] = []
    for candidate in raw_candidates[: max(targeted_cap * 2, 12)]:
        skill_key = str(candidate.get('skill_key') or '').strip()
        skill_name = str(candidate.get('skill_name_en') or '').strip()
        query_text = (
            f'{employee.current_title or ""} {skill_name} '
            f'roadmap execution hands-on evidence'
        ).strip()
        cv_matches = retrieve_employee_cv_evidence_sync(
            employee.workspace,
            query_text=query_text,
            employee_uuids=[str(employee.uuid)],
            skill_keys=[skill_key] if skill_key else None,
            limit=4,
            min_score=0.2,
        )
        candidate = {
            **candidate,
            'retrieved_cv_matches': _summarize_cv_matches(cv_matches),
        }
        if _should_skip_candidate(candidate, cv_matches):
            candidate['skipped_reason'] = 'strong_existing_evidence'
            enriched_candidates.append(candidate)
            continue
        candidate['selection_score'] = _compute_selection_score(candidate, cv_matches)
        candidate['why_asked'] = _build_why_asked(candidate, cv_matches)
        candidate['optional_example_prompt'] = _build_optional_example_prompt(candidate)
        enriched_candidates.append(candidate)

    selected_candidates = sorted(
        [item for item in enriched_candidates if not item.get('skipped_reason')],
        key=lambda item: (-float(item.get('selection_score') or 0.0), item.get('skill_name_en', '')),
    )
    skipped_candidates = [
        item for item in enriched_candidates if item.get('skipped_reason')
    ]
    return {
        'selected_candidates': selected_candidates,
        'skipped_candidates': skipped_candidates,
    }


def _build_requirement_candidate(employee: Employee, role_profile, requirement: RoleSkillRequirement) -> dict[str, Any]:
    evidence_rows = list(
        EmployeeSkillEvidence.objects.filter(employee=employee, skill=requirement.skill).order_by('-weight', '-updated_at')
    )
    current_level = _weighted_level(evidence_rows)
    current_confidence = _weighted_confidence(evidence_rows)
    current_evidence_mass = _evidence_mass(evidence_rows)
    target_level = int(requirement.target_level or 0)
    gap = round(max(0.0, float(target_level) - current_level), 2)
    source_list = _dedupe_strings([row.source_kind for row in evidence_rows[:5]])
    metadata = dict(requirement.metadata or {})
    return {
        'question_id': f'skill:{requirement.skill.canonical_key}',
        'question_type': 'targeted_skill',
        'skill_key': requirement.skill.canonical_key,
        'skill_name_en': requirement.skill.display_name_en,
        'skill_name_ru': requirement.skill.display_name_ru,
        'target_level': target_level,
        'priority': int(requirement.priority or 0),
        'current_level': current_level,
        'current_confidence': current_confidence,
        'current_evidence_mass': current_evidence_mass,
        'evidence_sources': source_list,
        'existing_evidence_summary': _build_existing_evidence_summary(evidence_rows),
        'role_name': role_profile.name,
        'seniority': role_profile.seniority,
        'supported_initiatives': list(metadata.get('supported_initiatives') or []),
        'requirement_type': str(metadata.get('requirement_type') or ('core' if requirement.is_required else 'adjacent')),
        'criticality': _criticality_from_text(str(metadata.get('criticality') or 'medium')),
        'gap': gap,
        'selection_score': 0.0,
    }


async def _phrase_assessment_pack_with_llm(pack_plan: dict[str, Any]) -> dict[str, Any]:
    system_prompt = (
        'You are writing a short employee self-assessment pack for a software company.\n\n'

        '## Your task\n'
        'Rephrase the provided question specifications into friendly, low-anxiety, '
        'employee-facing prompts. You are a WORDING assistant — the question selection '
        'has already been decided. Do not add, remove, or reorder questions.\n\n'

        '## Tone and style rules\n'
        '- SUPPORTIVE: This is career development, not a performance review or exam. '
        'The employee should feel this is about helping them grow, not evaluating them.\n'
        '- SPECIFIC: Reference the actual skill name and what it means in practice. '
        'BAD: "Rate your engineering skills." '
        'GOOD: "How confidently can you design and maintain backend APIs used by '
        'multiple product workflows?"\n'
        '- CONCISE: Each prompt should be 1-2 sentences. No paragraphs.\n'
        '- PRACTICAL: Ask about what they DO, not abstract self-ratings. '
        'Frame questions around recent work, concrete examples, and real situations.\n'
        '- GROWTH-ORIENTED: Use phrases like "How comfortable are you with...", '
        '"Can you describe a recent example of...", "What would help you grow in...".\n'
        '- AVOID: Jargon-heavy questions, double-barreled questions (asking about two '
        'things at once), leading questions, questions that feel like traps.\n\n'

        '## Structural rules\n'
        '- Preserve ALL question_id values exactly as provided.\n'
        '- hidden_skills_prompt: Rephrase as an open invitation to share unexpected '
        'strengths. Keep it warm and curious.\n'
        '- aspiration_prompt: Rephrase as a forward-looking career interest question. '
        'Do not make it feel like the employee is being asked to justify their position.\n'
        '- targeted_questions: Each must keep its question_id. Rephrase the prompt_text '
        'to be specific to the skill_name and contextualized to the employee\'s role.\n'
        '- Do not invent new questions or change question ids.\n\n'

        '## Example transformations\n'
        'Input spec: {"skill_name": "API Design", "prompt_text": "How confident are you in API Design?"}\n'
        'Better output: "When you design or extend an API, how comfortable are you making '
        'decisions about contracts, versioning, and error handling without guidance?"\n\n'
        'Input spec: {"skill_name": "Product Analytics", "prompt_text": "Rate your Product Analytics skills."}\n'
        'Better output: "Can you describe a recent situation where you used product metrics '
        'or experiment data to influence a product decision?"'
    )
    employee_name = pack_plan.get('employee_name', '')
    current_title = pack_plan.get('current_title', '')
    user_prompt = (
        f'## Employee context\n'
        f'- Name: {employee_name}\n'
        f'- Current title: {current_title}\n'
        f'- Primary role: {pack_plan.get("primary_role")}\n'
        f'- Adjacent roles: {pack_plan.get("adjacent_roles")}\n\n'
        f'## Question themes\n{pack_plan.get("question_themes")}\n\n'
        f'## Hidden skills prompt spec (rephrase this)\n{pack_plan.get("hidden_skills_prompt")}\n\n'
        f'## Aspiration prompt spec (rephrase this)\n{pack_plan.get("aspiration_prompt")}\n\n'
        f'## Targeted question specs (rephrase each, keep question_ids)\n'
        f'{json.dumps(pack_plan.get("targeted_questions"), ensure_ascii=False, indent=2)}\n\n'
        f'## Global notes from operator\n{pack_plan.get("global_notes", "None")}\n\n'
        '## Instructions\n'
        'Rephrase all prompts now. Keep question_ids. Make each prompt specific, '
        f'supportive, and relevant to {employee_name}\'s context as a {current_title}.'
    )
    result = await call_openai_structured(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_name='employee_assessment_pack_wording',
        schema=ASSESSMENT_PACK_WORDING_SCHEMA,
        temperature=0.2,
        max_tokens=1800,
    )
    return _normalize_pack_wording(pack_plan, result.parsed)


def _normalize_pack_wording(pack_plan: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    targeted_by_id = {
        str(item.get('question_id') or ''): item
        for item in list(parsed.get('targeted_questions') or [])
        if str(item.get('question_id') or '').strip()
    }
    normalized_targeted = []
    for spec in pack_plan.get('targeted_questions', []):
        question_id = spec['question_id']
        phrased = targeted_by_id.get(question_id, {})
        normalized_targeted.append(
            {
                'question_id': question_id,
                'prompt': str(phrased.get('prompt') or _deterministic_targeted_prompt(spec)).strip(),
                'optional_example_prompt': str(
                    phrased.get('optional_example_prompt') or spec.get('optional_example_prompt') or ''
                ).strip(),
            }
        )

    hidden_prompt = dict(parsed.get('hidden_skills_prompt') or {})
    aspiration_prompt = dict(parsed.get('aspiration_prompt') or {})
    return {
        'introduction': str(
            parsed.get('introduction')
            or 'This short self-assessment helps us fill evidence gaps without turning the process into an exam.'
        ).strip(),
        'hidden_skills_prompt': {
            'question_id': pack_plan['hidden_skills_prompt']['question_id'],
            'prompt': str(
                hidden_prompt.get('prompt')
                or 'What skills, tools, or domains are you comfortable with that may not be visible in your CV or recent role history?'
            ).strip(),
        },
        'aspiration_prompt': {
            'question_id': pack_plan['aspiration_prompt']['question_id'],
            'prompt': str(
                aspiration_prompt.get('prompt')
                or 'Are there adjacent roles or responsibilities you would be interested in growing into over the next 6–12 months?'
            ).strip(),
        },
        'targeted_questions': normalized_targeted,
        'closing_prompt': str(
            parsed.get('closing_prompt')
            or 'Short practical examples are enough. If a question does not fit your work, say so plainly.'
        ).strip(),
    }


def _compose_questionnaire_payload(pack_plan: dict[str, Any], wording: dict[str, Any]) -> dict[str, Any]:
    targeted_prompts = {
        item['question_id']: item
        for item in list(wording.get('targeted_questions') or [])
    }
    targeted_questions = []
    flat_questions = []
    for spec in pack_plan.get('targeted_questions', []):
        prompt_payload = targeted_prompts.get(spec['question_id'], {})
        question = {
            **spec,
            'prompt': str(prompt_payload.get('prompt') or _deterministic_targeted_prompt(spec)).strip(),
            'optional_example_prompt': str(
                prompt_payload.get('optional_example_prompt') or spec.get('optional_example_prompt') or ''
            ).strip(),
        }
        targeted_questions.append(question)
        flat_questions.append(question)

    hidden_question = {
        **pack_plan['hidden_skills_prompt'],
        'prompt': wording['hidden_skills_prompt']['prompt'],
    }
    aspiration_question = {
        **pack_plan['aspiration_prompt'],
        'prompt': wording['aspiration_prompt']['prompt'],
    }
    return {
        'schema_version': ASSESSMENT_PACK_VERSION,
        'introduction': wording['introduction'],
        'hidden_skills_prompt': hidden_question,
        'aspiration_prompt': aspiration_question,
        'targeted_questions': targeted_questions,
        'questions': [hidden_question, aspiration_question, *flat_questions],
        'closing_prompt': wording['closing_prompt'],
        'selection_summary': pack_plan.get('selection_summary', {}),
    }


def _upsert_employee_assessment_pack_sync(
    cycle_pk,
    employee_pk,
    questionnaire: dict[str, Any],
    selection_summary: dict[str, Any],
) -> None:
    cycle = AssessmentCycle.objects.get(pk=cycle_pk)
    employee = Employee.objects.get(pk=employee_pk)
    EmployeeAssessmentPack.objects.update_or_create(
        cycle=cycle,
        employee=employee,
        defaults={
            'title': f'Assessment for {employee.full_name}',
            'status': AssessmentPackStatus.GENERATED,
            'questionnaire_version': ASSESSMENT_PACK_VERSION,
            'questionnaire_payload': questionnaire,
            'selection_summary': selection_summary or {},
            'response_payload': {},
            'fused_summary': {},
            'opened_at': None,
            'submitted_at': None,
        },
    )


def _finalize_assessment_cycle_sync(cycle_pk) -> None:
    cycle = AssessmentCycle.objects.get(pk=cycle_pk)
    cycle.status = AssessmentStatus.GENERATED
    cycle.result_summary = _build_cycle_progress_summary(cycle)
    cycle.save(update_fields=['status', 'result_summary', 'updated_at'])


def _fail_assessment_cycle_sync(cycle_pk, error_message: str) -> None:
    cycle = AssessmentCycle.objects.get(pk=cycle_pk)
    cycle.status = AssessmentStatus.FAILED
    cycle.result_summary = {
        **(cycle.result_summary or {}),
        'error_message': error_message,
    }
    cycle.save(update_fields=['status', 'result_summary', 'updated_at'])


def _supersede_previous_cycles_sync(workspace_pk, current_cycle_uuid: str, planning_context_pk=None) -> None:
    planning_context_filter = (
        {'planning_context_id': planning_context_pk}
        if planning_context_pk is not None
        else {'planning_context__isnull': True}
    )
    previous_cycles = list(
        AssessmentCycle.objects.filter(workspace_id=workspace_pk, **planning_context_filter)
        .exclude(uuid=current_cycle_uuid)
        .exclude(status__in=[AssessmentStatus.FAILED, AssessmentStatus.SUPERSEDED])
    )
    for cycle in previous_cycles:
        cycle.status = AssessmentStatus.SUPERSEDED
        cycle.save(update_fields=['status', 'updated_at'])
        EmployeeAssessmentPack.objects.filter(cycle=cycle).exclude(
            status__in=list(TERMINAL_PACK_STATUSES | {AssessmentPackStatus.SUPERSEDED})
        ).update(status=AssessmentPackStatus.SUPERSEDED, updated_at=timezone.now())


def _mark_pack_opened_sync(pack_pk) -> None:
    with transaction.atomic():
        pack = EmployeeAssessmentPack.objects.select_for_update().select_related('cycle').get(pk=pack_pk)
        if pack.status == AssessmentPackStatus.SUPERSEDED or pack.cycle.status == AssessmentStatus.SUPERSEDED:
            raise ValueError('This assessment pack belongs to a superseded cycle and can no longer be opened.')
        if pack.status in TERMINAL_PACK_STATUSES:
            return  # Already finalized — do not reopen
        update_fields: list[str] = []
        if pack.status == AssessmentPackStatus.GENERATED:
            pack.status = AssessmentPackStatus.OPENED
            update_fields.append('status')
        if pack.opened_at is None:
            pack.opened_at = timezone.now()
            update_fields.append('opened_at')
        if update_fields:
            pack.save(update_fields=[*update_fields, 'updated_at'])
    _refresh_cycle_status_sync(pack.cycle_id)


def _apply_pack_response_sync(pack_pk, submission_payload: dict) -> None:
    with transaction.atomic():
        pack = EmployeeAssessmentPack.objects.select_for_update().select_related(
            'employee', 'cycle', 'cycle__workspace',
        ).get(pk=pack_pk)
        if pack.status == AssessmentPackStatus.SUPERSEDED or pack.cycle.status == AssessmentStatus.SUPERSEDED:
            raise ValueError('This assessment pack belongs to a superseded cycle and can no longer accept responses.')
        if pack.status in TERMINAL_PACK_STATUSES:
            raise ValueError('This assessment pack has already been finalized and can no longer accept changes.')
        employee = pack.employee
        workspace = pack.cycle.workspace
        normalized_submission = _normalize_pack_submission(pack.questionnaire_payload or {}, submission_payload or {})
        final_submit = bool(normalized_submission.get('final_submit', True))
        now = timezone.now()

        pack.response_payload = normalized_submission
        if pack.opened_at is None:
            pack.opened_at = now

        if not final_submit:
            if pack.status in {AssessmentPackStatus.GENERATED, AssessmentPackStatus.DRAFT}:
                pack.status = AssessmentPackStatus.OPENED
            pack.save(update_fields=['response_payload', 'status', 'opened_at', 'updated_at'])
            _refresh_cycle_status_sync(pack.cycle_id)
            return

        targeted_row_specs = []
        hidden_row_specs = []
        pending_targeted_skills = []
        pending_hidden_skills = []
        overlapping_skill_keys: set[str] = set()
        for answer in normalized_submission.get('targeted_answers', []):
            question = _question_lookup(pack.questionnaire_payload, answer['question_id'])
            if question is None:
                continue
            skill, normalized_skill = _resolve_targeted_skill_sync(workspace, question, answer)
            if skill is None:
                pending_targeted_skills.append(
                    {
                        'question_id': answer['question_id'],
                        'skill_name_en': str(
                            question.get('skill_name_en') or normalized_skill.get('display_name_en') or ''
                        ).strip(),
                        'skill_name_ru': str(question.get('skill_name_ru') or '').strip(),
                        'proposed_key': str(normalized_skill.get('canonical_key') or '').strip(),
                        'match_source': str(normalized_skill.get('match_source') or '').strip(),
                    }
                )
                continue
            overlapping_skill_keys.add(skill.canonical_key)
            targeted_row_specs.append(
                {
                    'question_id': answer['question_id'],
                    'question': question,
                    'answer': answer,
                    'skill': skill,
                }
            )

        targeted_keys = {spec['skill'].canonical_key for spec in targeted_row_specs}
        for item in normalized_submission.get('hidden_skills', []):
            skill, normalized_skill = _resolve_hidden_skill_sync(workspace, item)
            if skill is None:
                pending_hidden_skills.append(
                    {
                        'skill_name_en': str(
                            item.get('skill_name_en') or normalized_skill.get('display_name_en') or ''
                        ).strip(),
                        'skill_name_ru': str(item.get('skill_name_ru') or '').strip(),
                        'proposed_key': str(normalized_skill.get('canonical_key') or '').strip(),
                        'match_source': str(normalized_skill.get('match_source') or '').strip(),
                    }
                )
                continue
            if skill.canonical_key in targeted_keys:
                continue  # Skip — already covered by a targeted question
            overlapping_skill_keys.add(skill.canonical_key)
            hidden_row_specs.append(
                {
                    'item': item,
                    'skill': skill,
                }
            )

        normalized_submission['pending_targeted_skills'] = pending_targeted_skills
        normalized_submission['pending_hidden_skills'] = pending_hidden_skills

        if overlapping_skill_keys:
            EmployeeSkillEvidence.objects.filter(
                workspace=workspace,
                employee=employee,
                source_kind=SELF_ASSESSMENT_SOURCE_KIND,
                skill__canonical_key__in=sorted(overlapping_skill_keys),
            ).filter(
                Q(assessment_cycle=pack.cycle)
                | Q(assessment_cycle__isnull=True, metadata__assessment_cycle_uuid=str(pack.cycle.uuid))
            ).delete()

        EmployeeSkillEvidence.objects.filter(
            workspace=workspace,
            employee=employee,
            source_kind=SELF_ASSESSMENT_SOURCE_KIND,
            metadata__assessment_pack_uuid=str(pack.uuid),
        ).delete()

        evidence_row_uuids: dict[str, str] = {}
        submitted_skill_rows = []
        for row_spec in targeted_row_specs:
            answer = row_spec['answer']
            question = row_spec['question']
            skill = row_spec['skill']
            evidence_row = EmployeeSkillEvidence.objects.create(
                workspace=workspace,
                employee=employee,
                skill=skill,
                source_kind=SELF_ASSESSMENT_SOURCE_KIND,
                source=None,
                assessment_cycle=pack.cycle,
                assessment_pack=pack,
                current_level=Decimal(str(answer['self_rated_level'])),
                confidence=Decimal(str(round(answer['answer_confidence'], 2))),
                weight=Decimal(str(round(SELF_ASSESSMENT_SOURCE_WEIGHT * answer['answer_confidence'], 2))),
                evidence_text=str(answer.get('example_text') or answer.get('notes') or '').strip(),
                metadata={
                    'assessment_cycle_uuid': str(pack.cycle.uuid),
                    'assessment_pack_uuid': str(pack.uuid),
                    'question_id': answer['question_id'],
                    'question_type': question.get('question_type', 'targeted_skill'),
                    'target_level': int(question.get('target_level') or 0),
                    'source_weight': SELF_ASSESSMENT_SOURCE_WEIGHT,
                    'answer_confidence': float(answer['answer_confidence']),
                    'why_asked': str(question.get('why_asked') or '').strip(),
                    'skill_name_en': skill.display_name_en,
                },
            )
            evidence_row_uuids[answer['question_id']] = str(evidence_row.uuid)
            submitted_skill_rows.append(
                {
                    'question_id': answer['question_id'],
                    'skill_key': skill.canonical_key,
                    'skill_name_en': skill.display_name_en,
                    'self_rated_level': answer['self_rated_level'],
                    'answer_confidence': answer['answer_confidence'],
                    'example_text': answer.get('example_text', ''),
                }
            )

        hidden_skill_rows = []
        for row_spec in hidden_row_specs:
            item = row_spec['item']
            skill = row_spec['skill']
            evidence_row = EmployeeSkillEvidence.objects.create(
                workspace=workspace,
                employee=employee,
                skill=skill,
                source_kind=SELF_ASSESSMENT_SOURCE_KIND,
                source=None,
                assessment_cycle=pack.cycle,
                assessment_pack=pack,
                current_level=Decimal(str(item['self_rated_level'])),
                confidence=Decimal(str(round(item['answer_confidence'], 2))),
                weight=Decimal(str(round(SELF_ASSESSMENT_SOURCE_WEIGHT * item['answer_confidence'], 2))),
                evidence_text=str(item.get('example_text') or '').strip(),
                metadata={
                    'assessment_cycle_uuid': str(pack.cycle.uuid),
                    'assessment_pack_uuid': str(pack.uuid),
                    'question_id': 'hidden-skills',
                    'question_type': 'hidden_skills',
                    'source_weight': SELF_ASSESSMENT_SOURCE_WEIGHT,
                    'answer_confidence': float(item['answer_confidence']),
                    'skill_name_en': skill.display_name_en,
                },
            )
            evidence_row_uuids[f'hidden:{skill.canonical_key}'] = str(evidence_row.uuid)
            hidden_skill_rows.append(
                {
                    'skill_key': skill.canonical_key,
                    'skill_name_en': skill.display_name_en,
                    'skill_name_ru': skill.display_name_ru,
                    'self_rated_level': item['self_rated_level'],
                    'answer_confidence': item['answer_confidence'],
                    'example_text': item.get('example_text', ''),
                }
            )

        pack.fused_summary = {
            'schema_version': ASSESSMENT_PACK_VERSION,
            'hidden_skills': hidden_skill_rows,
            'aspiration': dict(normalized_submission.get('aspiration') or {}),
            'confidence_statement': str(normalized_submission.get('confidence_statement') or '').strip(),
            'submitted_skill_rows': submitted_skill_rows,
            'evidence_row_uuids': evidence_row_uuids,
        }
        pack.status = AssessmentPackStatus.SUBMITTED
        pack.submitted_at = now
        pack.save(
            update_fields=[
                'response_payload',
                'fused_summary',
                'status',
                'opened_at',
                'submitted_at',
                'updated_at',
            ]
        )
        _refresh_cycle_status_sync(pack.cycle_id)

    vector_index = index_employee_assessment_pack_sync(pack.pk)
    pack = EmployeeAssessmentPack.objects.get(pk=pack.pk)
    pack.fused_summary = {
        **(pack.fused_summary or {}),
        'vector_index': vector_index,
    }
    pack.save(update_fields=['fused_summary', 'updated_at'])


def _refresh_cycle_status_sync(cycle_pk) -> None:
    cycle = AssessmentCycle.objects.get(pk=cycle_pk)
    summary = _build_cycle_progress_summary(cycle)
    pack_counts = summary.get('pack_status_counts', {})
    total_packs = int(summary.get('total_packs', 0) or 0)
    completed_packs = int(summary.get('submitted_packs', 0) or 0) + int(summary.get('completed_packs', 0) or 0)

    if total_packs and completed_packs >= total_packs:
        cycle.status = AssessmentStatus.COMPLETED
    elif int(pack_counts.get(AssessmentPackStatus.OPENED, 0) or 0) > 0 or int(pack_counts.get(AssessmentPackStatus.SUBMITTED, 0) or 0) > 0:
        cycle.status = AssessmentStatus.RUNNING
    else:
        cycle.status = AssessmentStatus.GENERATED

    cycle.result_summary = summary
    cycle.save(update_fields=['status', 'result_summary', 'updated_at'])


def _build_cycle_progress_summary(cycle: AssessmentCycle) -> dict[str, Any]:
    packs = list(cycle.packs.only('status', 'employee_id'))
    counter = Counter(pack.status for pack in packs)
    employee_ids_with_packs = {str(pack.employee_id) for pack in packs}
    configured_targets = list((cycle.configuration or {}).get('target_employee_uuids') or [])
    total_employees = len(configured_targets) if configured_targets else Employee.objects.filter(workspace=cycle.workspace).count()
    employees_missing_packs = max(0, total_employees - len(employee_ids_with_packs))
    submitted_packs = int(counter.get(AssessmentPackStatus.SUBMITTED, 0))
    completed_packs = int(counter.get(AssessmentPackStatus.COMPLETED, 0))
    completion_rate = round(((submitted_packs + completed_packs) / len(packs)), 2) if packs else 0.0
    return {
        'schema_version': ASSESSMENT_PACK_VERSION,
        'total_packs': len(packs),
        'generated_packs': int(counter.get(AssessmentPackStatus.GENERATED, 0)),
        'opened_packs': int(counter.get(AssessmentPackStatus.OPENED, 0)),
        'submitted_packs': submitted_packs,
        'completed_packs': completed_packs,
        'superseded_packs': int(counter.get(AssessmentPackStatus.SUPERSEDED, 0)),
        'employees_missing_packs': employees_missing_packs,
        'completion_rate': completion_rate,
        'pack_status_counts': dict(counter),
    }


def _build_assessment_status_sync(workspace_pk, workspace_slug: str, planning_context_pk=None) -> dict[str, Any]:
    planning_context_filter = (
        {'planning_context_id': planning_context_pk}
        if planning_context_pk is not None
        else {'planning_context__isnull': True}
    )
    latest_attempt = (
        AssessmentCycle.objects.filter(workspace_id=workspace_pk, **planning_context_filter)
        .order_by('-updated_at')
        .first()
    )
    cycle = (
        AssessmentCycle.objects.select_related('blueprint_run')
        .filter(workspace_id=workspace_pk, **planning_context_filter)
        .exclude(status__in=[AssessmentStatus.SUPERSEDED, AssessmentStatus.FAILED])
        .order_by('-updated_at')
        .first()
    )
    if planning_context_pk is None:
        total_employees = Employee.objects.filter(workspace_id=workspace_pk).count()
        target_employee_queryset = Employee.objects.filter(workspace_id=workspace_pk).order_by('full_name')
    else:
        current_blueprint = (
            SkillBlueprintRun.objects.filter(
                workspace_id=workspace_pk,
                planning_context_id=planning_context_pk,
                is_published=True,
            )
            .order_by('-published_at', '-updated_at')
            .first()
        )
        target_employees = _resolve_assessment_cycle_employees_sync(
            workspace_pk,
            str(current_blueprint.uuid) if current_blueprint is not None else None,
            planning_context_pk=planning_context_pk,
            selected_employee_uuids=[],
        )
        total_employees = len(target_employees)
        target_employee_queryset = target_employees
    if cycle is None:
        return {
            'workspace_slug': workspace_slug,
            'latest_attempt_uuid': getattr(latest_attempt, 'uuid', None),
            'latest_attempt_status': getattr(latest_attempt, 'status', ''),
            'current_cycle_uuid': None,
            'current_cycle_status': '',
            'blueprint_run_uuid': None,
            'planning_context_uuid': getattr(latest_attempt, 'planning_context_id', None),
            'total_employees': total_employees,
            'total_packs': 0,
            'generated_packs': 0,
            'opened_packs': 0,
            'submitted_packs': 0,
            'completed_packs': 0,
            'superseded_packs': 0,
            'completion_rate': 0.0,
            'employees_missing_packs': [
                {
                    'employee_uuid': str(employee.uuid),
                    'full_name': employee.full_name,
                    'current_title': employee.current_title,
                }
                for employee in target_employee_queryset
            ],
            'employees_with_submitted_self_assessment': 0,
            'cycle_summary': {},
        }

    configured_target_ids = list((cycle.configuration or {}).get('target_employee_uuids') or [])
    if configured_target_ids:
        target_employee_queryset = list(
            Employee.objects.filter(uuid__in=configured_target_ids).order_by('full_name')
        )
        total_employees = len(configured_target_ids)

    summary = _build_cycle_progress_summary(cycle)
    cycle_employee_ids = set(cycle.packs.values_list('employee_id', flat=True))
    missing_employees = [
        {
            'employee_uuid': str(employee.uuid),
            'full_name': employee.full_name,
            'current_title': employee.current_title,
        }
        for employee in (
            Employee.objects.filter(uuid__in=[employee.uuid for employee in target_employee_queryset])
            .exclude(uuid__in=cycle_employee_ids)
            .order_by('full_name')
        )
    ]
    submitted_employee_count = (
        EmployeeSkillEvidence.objects.filter(
            workspace_id=workspace_pk,
            source_kind=SELF_ASSESSMENT_SOURCE_KIND,
            metadata__assessment_cycle_uuid=str(cycle.uuid),
        )
        .values('employee_id')
        .distinct()
        .count()
    )
    return {
        'workspace_slug': workspace_slug,
        'latest_attempt_uuid': getattr(latest_attempt, 'uuid', None),
        'latest_attempt_status': getattr(latest_attempt, 'status', ''),
        'current_cycle_uuid': cycle.uuid,
        'current_cycle_status': cycle.status,
        'blueprint_run_uuid': getattr(cycle.blueprint_run, 'uuid', None),
        'planning_context_uuid': cycle.planning_context_id,
        'total_employees': total_employees,
        'total_packs': int(summary.get('total_packs', 0)),
        'generated_packs': int(summary.get('generated_packs', 0)),
        'opened_packs': int(summary.get('opened_packs', 0)),
        'submitted_packs': int(summary.get('submitted_packs', 0)),
        'completed_packs': int(summary.get('completed_packs', 0)),
        'superseded_packs': int(summary.get('superseded_packs', 0)),
        'completion_rate': float(summary.get('completion_rate', 0.0)),
        'employees_missing_packs': missing_employees,
        'employees_with_submitted_self_assessment': submitted_employee_count,
        'cycle_summary': summary,
    }


def _serialize_role_match(match: EmployeeRoleMatch | None) -> dict[str, Any] | None:
    if match is None:
        return None
    return {
        'role_name': match.role_profile.name,
        'seniority': match.role_profile.seniority,
        'fit_score': float(match.fit_score or 0.0),
        'reason': match.rationale,
        'role_family': match.role_profile.family,
    }


def _normalize_pack_submission(questionnaire_payload: dict[str, Any], submission_payload: dict[str, Any]) -> dict[str, Any]:
    question_lookup = {
        str(item.get('question_id') or ''): item
        for item in list(questionnaire_payload.get('questions') or [])
        if str(item.get('question_id') or '').strip()
    }
    expected_targeted_ids = {
        str(item.get('question_id') or '')
        for item in list(questionnaire_payload.get('targeted_questions') or [])
        if str(item.get('question_id') or '').strip()
    }
    final_submit = bool(submission_payload.get('final_submit', True))

    seen_targeted_ids: set[str] = set()
    targeted_answers: list[dict[str, Any]] = []
    for raw_answer in list(submission_payload.get('targeted_answers') or []):
        question_id = str(raw_answer.get('question_id') or '').strip()
        if not question_id:
            raise ValueError('Each targeted answer must include question_id.')
        if question_id in seen_targeted_ids:
            raise ValueError(f'Duplicate targeted answer for question_id={question_id}.')
        if question_id not in expected_targeted_ids:
            raise ValueError(f'Unknown targeted question_id={question_id}.')
        seen_targeted_ids.add(question_id)
        question = question_lookup.get(question_id, {})
        targeted_answers.append(
            {
                'question_id': question_id,
                'skill_key': str(raw_answer.get('skill_key') or question.get('skill_key') or '').strip(),
                'self_rated_level': _clamp_int(raw_answer.get('self_rated_level'), default=0, minimum=0, maximum=5),
                'answer_confidence': _clamp_float(raw_answer.get('answer_confidence'), default=0.6, minimum=0.0, maximum=1.0),
                'example_text': str(raw_answer.get('example_text') or '').strip(),
                'notes': str(raw_answer.get('notes') or '').strip(),
            }
        )

    if final_submit:
        missing = sorted(expected_targeted_ids - seen_targeted_ids)
        if missing:
            raise ValueError(f'Final submission is missing targeted answers for: {", ".join(missing)}.')

    hidden_skills = []
    for item in list(submission_payload.get('hidden_skills') or []):
        skill_name_en = str(item.get('skill_name_en') or '').strip()
        if not skill_name_en:
            continue
        hidden_skills.append(
            {
                'skill_name_en': skill_name_en,
                'skill_name_ru': str(item.get('skill_name_ru') or '').strip(),
                'self_rated_level': _clamp_int(item.get('self_rated_level'), default=3, minimum=0, maximum=5),
                'answer_confidence': _clamp_float(item.get('answer_confidence'), default=0.6, minimum=0.0, maximum=1.0),
                'example_text': str(item.get('example_text') or '').strip(),
            }
        )

    aspiration = {
        'target_role_family': str((submission_payload.get('aspiration') or {}).get('target_role_family') or '').strip(),
        'notes': str((submission_payload.get('aspiration') or {}).get('notes') or '').strip(),
        'interest_signal': str((submission_payload.get('aspiration') or {}).get('interest_signal') or '').strip(),
    }
    return {
        'schema_version': ASSESSMENT_PACK_VERSION,
        'final_submit': final_submit,
        'targeted_answers': targeted_answers,
        'hidden_skills': hidden_skills,
        'aspiration': aspiration,
        'confidence_statement': str(submission_payload.get('confidence_statement') or '').strip(),
    }


def _resolve_targeted_skill_sync(workspace, question: dict[str, Any], answer: dict[str, Any]):
    skill_key = str(answer.get('skill_key') or question.get('skill_key') or '').strip()
    if skill_key:
        skill = workspace.skills.filter(canonical_key=skill_key).first()  # type: ignore[attr-defined]
        if skill is not None:
            return skill, {
                'canonical_key': skill.canonical_key,
                'display_name_en': skill.display_name_en,
                'match_source': 'workspace_existing',
            }
    normalized = normalize_skill_seed(
        str(question.get('skill_name_en') or answer.get('skill_key') or ''),
        workspace=workspace,
        review_metadata={
            'source': 'self_assessment_targeted',
            'question_id': str(question.get('question_id') or ''),
        },
        allow_freeform=False,
    )
    if normalized.get('needs_review'):
        return None, normalized
    return (
        ensure_workspace_skill_sync(
            workspace,
            normalized_skill=normalized,
            preferred_display_name_ru=str(question.get('skill_name_ru') or '').strip(),
            created_source='self_assessment',
            promote_aliases=False,
        ),
        normalized,
    )


def _resolve_hidden_skill_sync(workspace, item: dict[str, Any]):
    normalized = normalize_skill_seed(
        str(item.get('skill_name_en') or '').strip(),
        workspace=workspace,
        review_metadata={
            'source': 'self_assessment_hidden_skill',
            'skill_name_ru': str(item.get('skill_name_ru') or '').strip(),
        },
        allow_freeform=False,
    )
    if normalized.get('needs_review'):
        return None, normalized
    return (
        ensure_workspace_skill_sync(
            workspace,
            normalized_skill=normalized,
            preferred_display_name_ru=str(item.get('skill_name_ru') or '').strip(),
            created_source='self_assessment',
            promote_aliases=False,
        ),
        normalized,
    )


def _question_lookup(questionnaire_payload: dict[str, Any], question_id: str) -> dict[str, Any] | None:
    for question in list(questionnaire_payload.get('questions') or []):
        if str(question.get('question_id') or '') == question_id:
            return question
    return None


def _build_existing_evidence_summary(evidence_rows: list[EmployeeSkillEvidence]) -> str:
    if not evidence_rows:
        return 'No direct evidence recorded yet.'
    source_kinds = _dedupe_strings([row.source_kind for row in evidence_rows[:4]])
    snippets = [str(row.evidence_text or '').strip() for row in evidence_rows[:2] if str(row.evidence_text or '').strip()]
    level = _weighted_level(evidence_rows)
    confidence = _weighted_confidence(evidence_rows)
    summary = f'Current evidence level {level}/5 with confidence {confidence}. Sources: {", ".join(source_kinds) or "unknown"}'
    if snippets:
        summary += f'. Recent evidence: {snippets[0][:180]}'
    return summary


def _should_skip_candidate(candidate: dict[str, Any], cv_matches: list[dict[str, Any]]) -> bool:
    current_level = float(candidate.get('current_level') or 0.0)
    target_level = float(candidate.get('target_level') or 0.0)
    confidence = float(candidate.get('current_confidence') or 0.0)
    has_strong_cv_context = len(cv_matches) >= 2 and max(float(item.get('score') or 0.0) for item in cv_matches) >= 0.35
    return (
        current_level >= max(0.0, target_level - 0.35)
        and confidence >= 0.65
        and has_strong_cv_context
    )


def _compute_selection_score(candidate: dict[str, Any], cv_matches: list[dict[str, Any]]) -> float:
    priority = float(candidate.get('priority') or 0.0)
    gap = float(candidate.get('gap') or 0.0)
    uncertainty = max(0.0, 1.0 - float(candidate.get('current_confidence') or 0.0))
    criticality = float(candidate.get('criticality') or 1.0)
    initiative_bonus = min(1.0, len(candidate.get('supported_initiatives') or []) * 0.2)
    low_evidence_bonus = 0.7 if float(candidate.get('current_evidence_mass') or 0.0) < 0.6 else 0.0
    retrieval_bonus = 0.2 if cv_matches else 0.0
    return round(
        (priority * 1.4)
        + (gap * 1.6)
        + (uncertainty * 1.2)
        + (criticality * 0.9)
        + initiative_bonus
        + low_evidence_bonus
        + retrieval_bonus,
        3,
    )


def _build_why_asked(candidate: dict[str, Any], cv_matches: list[dict[str, Any]]) -> str:
    reasons = []
    if float(candidate.get('gap') or 0.0) >= 1.0:
        reasons.append('target level is meaningfully above current evidence')
    if float(candidate.get('current_confidence') or 0.0) < 0.55:
        reasons.append('current evidence is low confidence')
    if candidate.get('supported_initiatives'):
        reasons.append('this skill supports roadmap initiatives')
    if not cv_matches:
        reasons.append('recent CV evidence is sparse')
    return '; '.join(reasons) or 'this is an important skill for the likely target role'


def _build_optional_example_prompt(candidate: dict[str, Any]) -> str:
    skill_name = str(candidate.get('skill_name_en') or 'this skill').strip()
    return f'If helpful, share one recent example that shows how you use {skill_name}.'


def _deterministic_targeted_prompt(candidate: dict[str, Any]) -> str:
    skill_name = str(candidate.get('skill_name_en') or 'this skill').strip()
    role_name = str(candidate.get('role_name') or 'your role').strip()
    target_level = int(candidate.get('target_level') or 0)
    return (
        f'How confidently can you apply {skill_name} in day-to-day work for {role_name}? '
        f'Please rate yourself from 0 to 5, where {target_level} is the current target level for this skill.'
    ).strip()


def _summarize_cv_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            'doc_type': item.get('doc_type', ''),
            'score': float(item.get('score') or 0.0),
            'section_heading': item.get('section_heading', ''),
            'chunk_text': str(item.get('chunk_text') or '')[:220],
        }
        for item in matches[:3]
    ]


def _criticality_from_text(value: str) -> float:
    lowered = str(value or '').strip().lower()
    if lowered in {'critical', 'very_high', 'highest'}:
        return 3.0
    if lowered in {'high', 'important'}:
        return 2.5
    if lowered in {'medium', 'moderate'}:
        return 1.8
    if lowered in {'low', 'nice_to_have'}:
        return 1.0
    return 1.6


def _weighted_level(evidence_rows) -> float:
    if not evidence_rows:
        return 0.0
    total_weight = 0.0
    weighted_sum = 0.0
    for row in evidence_rows:
        weight = float(row.weight or 0.0)
        weighted_sum += float(row.current_level or 0.0) * weight
        total_weight += weight
    if total_weight <= 0:
        return 0.0
    return round(weighted_sum / total_weight, 2)


def _weighted_confidence(evidence_rows) -> float:
    if not evidence_rows:
        return 0.0
    total_weight = 0.0
    weighted_sum = 0.0
    for row in evidence_rows:
        weight = float(row.weight or 0.0)
        weighted_sum += float(row.confidence or 0.0) * weight
        total_weight += weight
    if total_weight <= 0:
        return 0.0
    return round(weighted_sum / total_weight, 2)


def _evidence_mass(evidence_rows) -> float:
    return round(sum(float(row.weight or 0.0) for row in evidence_rows), 2)


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value or '').strip()
        if not normalized:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(maximum, normalized))


def _clamp_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(maximum, normalized))
