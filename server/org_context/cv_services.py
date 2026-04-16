import logging
import hashlib
import re
from datetime import date, datetime
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any, Optional
from uuid import UUID

from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import Q

from company_intake.models import IntakeWorkspace, WorkspaceSource, WorkspaceSourceKind, WorkspaceSourceStatus
from tools.openai.structured_client import StructuredLLMError, call_openai_structured

from .models import (
    CatalogOverrideStatus,
    CatalogResolutionReviewItem,
    CatalogReviewStatus,
    Employee,
    EmployeeCVMatchCandidate,
    EmployeeCVProfile,
    EmployeeOrgAssignment,
    EmployeeSkillEvidence,
    EscoSkill,
    Skill,
    SkillAlias,
    SkillReviewDecision,
    SkillResolutionOverride,
)
from .esco_matching import normalize_lookup_key
from .skill_catalog import (
    _find_matching_esco_skill,
    _resolve_esco_skill_from_normalized,
    dedupe_strings,
    ensure_workspace_skill_sync,
    merge_skill_aliases_sync,
    normalize_skill_seed,
    resolve_workspace_skill_sync,
)
from .vector_indexing import (
    CV_EVIDENCE_INDEX_VERSION,
    clear_employee_cv_evidence_index_sync,
    index_employee_cv_profile_sync,
)

logger = logging.getLogger(__name__)

CV_PROFILE_SCHEMA_VERSION = 'stage6-v1'
_CV_EVIDENCE_SOURCE_KIND = WorkspaceSourceKind.EMPLOYEE_CV


def _json_safe(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(nested_value) for key, nested_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


CV_EXTRACTION_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'candidate_name': {'type': 'string'},
        'email': {'type': 'string'},
        'headline': {'type': 'string'},
        'summary': {'type': 'string'},
        'seniority': {'type': 'string'},
        'current_role': {'type': 'string'},
        'role_family': {'type': 'string'},
        'current_department': {'type': 'string'},
        'languages': {'type': 'array', 'items': {'type': 'string'}},
        'warnings': {'type': 'array', 'items': {'type': 'string'}},
        'sparse_cv': {'type': 'boolean'},
        'sparse_reason': {'type': 'string'},
        'skills': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'skill_name_en': {'type': 'string'},
                    'skill_name_ru': {'type': 'string'},
                    'original_term': {'type': 'string'},
                    'level': {'type': 'integer'},
                    'confidence': {'type': 'integer'},
                    'category': {'type': 'string'},
                    'aliases': {'type': 'array', 'items': {'type': 'string'}},
                    'evidence': {'type': 'string'},
                },
                'required': [
                    'skill_name_en',
                    'skill_name_ru',
                    'original_term',
                    'level',
                    'confidence',
                    'category',
                    'aliases',
                    'evidence',
                ],
            },
        },
        'role_history': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'company_name': {'type': 'string'},
                    'role_title': {'type': 'string'},
                    'start_date': {'type': 'string'},
                    'end_date': {'type': 'string'},
                    'responsibilities': {'type': 'array', 'items': {'type': 'string'}},
                    'achievements': {'type': 'array', 'items': {'type': 'string'}},
                    'domains': {'type': 'array', 'items': {'type': 'string'}},
                    'leadership_signals': {'type': 'array', 'items': {'type': 'string'}},
                    'evidence': {'type': 'string'},
                    'confidence': {'type': 'integer'},
                },
                'required': [
                    'company_name',
                    'role_title',
                    'start_date',
                    'end_date',
                    'responsibilities',
                    'achievements',
                    'domains',
                    'leadership_signals',
                    'evidence',
                    'confidence',
                ],
            },
        },
        'achievements': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'summary': {'type': 'string'},
                    'evidence': {'type': 'string'},
                    'confidence': {'type': 'integer'},
                },
                'required': ['summary', 'evidence', 'confidence'],
            },
        },
        'domain_experience': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'domain': {'type': 'string'},
                    'evidence': {'type': 'string'},
                    'confidence': {'type': 'integer'},
                },
                'required': ['domain', 'evidence', 'confidence'],
            },
        },
        'leadership_signals': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'signal': {'type': 'string'},
                    'evidence': {'type': 'string'},
                    'confidence': {'type': 'integer'},
                },
                'required': ['signal', 'evidence', 'confidence'],
            },
        },
    },
    'required': [
        'candidate_name',
        'email',
        'headline',
        'summary',
        'seniority',
        'current_role',
        'role_family',
        'current_department',
        'languages',
        'warnings',
        'sparse_cv',
        'sparse_reason',
        'skills',
        'role_history',
        'achievements',
        'domain_experience',
        'leadership_signals',
    ],
}


async def build_cv_evidence_for_workspace(
    workspace,
    *,
    source_uuids: Optional[list[str]] = None,
    force_rebuild: bool = False,
) -> dict:
    resolution = await sync_to_async(_resolve_requested_cv_sources_sync)(workspace.pk, source_uuids or [])
    sources = resolution['processable_sources']
    results: list[dict[str, Any]] = list(resolution['prebuilt_results'])

    processable_source_ids = {str(source.uuid) for source in sources}
    ordered_source_ids = resolution['ordered_processable_source_ids']
    ordered_sources = [
        next(source for source in sources if str(source.uuid) == source_uuid)
        for source_uuid in ordered_source_ids
        if source_uuid in processable_source_ids
    ]
    if ordered_sources:
        sources = ordered_sources

    for source in sources:
        if not force_rebuild and _cv_profile_is_fresh(source):
            if _cv_profile_needs_index_refresh(source):
                results.append(await sync_to_async(_refresh_existing_cv_index_sync)(source.pk))
            else:
                results.append(await sync_to_async(_build_cv_profile_result_sync)(source.pk, True))
            continue

        try:
            extracted = await _extract_cv_payload(source)
        except StructuredLLMError as exc:
            logger.warning('Structured CV extraction failed for source %s: %s', source.uuid, exc)
            result = await sync_to_async(_record_cv_extraction_failure_sync)(source.pk, str(exc))
        except Exception as exc:
            logger.exception('Unexpected CV extraction failure for source %s', source.uuid)
            result = await sync_to_async(_record_cv_extraction_failure_sync)(source.pk, str(exc))
        else:
            result = await sync_to_async(_persist_cv_payload_sync)(source.pk, extracted)
        results.append(result)

    status_counts: dict[str, int] = {}
    for item in results:
        status_key = item.get('status', '')
        status_counts[status_key] = status_counts.get(status_key, 0) + 1

    rebuilt_count = len([item for item in results if not item.get('reused')])
    reused_count = len(results) - rebuilt_count
    return {
        'workspace_slug': workspace.slug,
        'processed': len(results),
        'rebuilt_count': rebuilt_count,
        'reused_count': reused_count,
        'status_counts': status_counts,
        'results': results,
    }


async def rebuild_cv_evidence_for_workspace(
    workspace,
    *,
    source_uuids: Optional[list[str]] = None,
) -> dict:
    return await build_cv_evidence_for_workspace(
        workspace,
        source_uuids=source_uuids,
        force_rebuild=True,
    )


async def get_cv_evidence_status(workspace) -> dict:
    return await sync_to_async(_get_cv_evidence_status_sync)(workspace.pk)


async def list_unmatched_cv_profiles(workspace) -> list[dict]:
    return await sync_to_async(_list_unmatched_cv_profiles_sync)(workspace.pk)


async def list_cv_review_items(workspace) -> list[dict]:
    return await sync_to_async(_list_cv_review_items_sync)(workspace.pk)


async def list_employees_without_cv_evidence(workspace) -> list[dict]:
    return await sync_to_async(_list_employees_without_cv_evidence_sync)(workspace.pk)


async def get_employee_cv_evidence_detail(workspace, employee_uuid) -> dict | None:
    return await sync_to_async(_get_employee_cv_evidence_detail_sync)(workspace.pk, employee_uuid)


async def resolve_cv_profile_match(
    workspace,
    source_uuid,
    *,
    employee_uuid=None,
    operator_name: str = '',
    resolution_note: str = '',
) -> dict | None:
    return await sync_to_async(_resolve_cv_profile_match_sync)(
        workspace.pk,
        str(source_uuid),
        str(employee_uuid) if employee_uuid is not None else '',
        operator_name,
        resolution_note,
    )


async def mark_employee_no_cv_available(
    workspace,
    employee_uuid,
    *,
    operator_name: str = '',
    note: str = '',
) -> dict | None:
    return await sync_to_async(_mark_employee_no_cv_available_sync)(
        workspace.pk,
        str(employee_uuid),
        operator_name,
        note,
    )


async def clear_employee_no_cv_available(
    workspace,
    employee_uuid,
) -> dict | None:
    return await sync_to_async(_clear_employee_no_cv_available_sync)(
        workspace.pk,
        str(employee_uuid),
    )


async def approve_pending_skill_candidate(
    workspace,
    source_uuid,
    *,
    candidate_key: str,
    approved_name_en: str,
    approved_name_ru: str = '',
    alias_terms: list[str] | None = None,
    operator_name: str = '',
    approval_note: str = '',
) -> dict | None:
    return await sync_to_async(_approve_pending_skill_candidate_sync)(
        workspace.pk,
        str(source_uuid),
        candidate_key,
        approved_name_en,
        approved_name_ru,
        list(alias_terms or []),
        operator_name,
        approval_note,
    )


async def review_employee_skills_bulk(
    workspace,
    employee_uuid,
    *,
    actions: list[dict],
) -> dict:
    return await sync_to_async(bulk_review_employee_skills)(
        workspace.pk,
        str(employee_uuid),
        actions,
    )


async def accept_employee_high_confidence_skills(
    workspace,
    employee_uuid,
    *,
    confidence_threshold: float = 0.7,
) -> dict:
    return await sync_to_async(accept_all_high_confidence_skills)(
        workspace.pk,
        str(employee_uuid),
        confidence_threshold=confidence_threshold,
    )


async def get_workspace_pending_skills(workspace) -> list[dict]:
    return await sync_to_async(list_workspace_pending_skills)(workspace.pk)


async def resolve_workspace_skills_bulk(
    workspace,
    *,
    resolutions: list[dict],
) -> dict:
    return await sync_to_async(bulk_resolve_workspace_skills)(
        workspace.pk,
        resolutions,
    )


async def delete_workspace_employee(workspace, employee_uuid) -> dict | None:
    return await sync_to_async(_delete_workspace_employee_sync)(
        workspace.pk,
        str(employee_uuid),
    )


async def _extract_cv_payload(source: WorkspaceSource) -> dict:
    parsed_source = source.parsed_source
    source_title = source.title or (
        source.media_file.original_filename if source.media_file is not None else source.title
    )
    system_prompt = (
        'You are extracting structured employee evidence from a CV or resume.\n\n'

        '## Your task\n'
        'Parse the CV text below into a structured profile. Extract only what the text '
        'actually says — do not infer skills or experience that are not mentioned.\n\n'

        '## Extraction rules\n\n'

        '### Identity\n'
        '- candidate_name: Full name as written in the CV. If the CV is in Russian, '
        'keep the original Cyrillic name.\n'
        '- email: Extract if present, otherwise empty string.\n'
        '- headline: The person\'s self-described headline or current role title.\n'
        '- summary: One sentence capturing what this person does professionally.\n\n'

        '### Skills (the most important section)\n'
        '- Extract each distinct professional skill mentioned in the CV.\n'
        '- skill_name_en: Concise English canonical name (e.g., "Python", "API Design", '
        '"Product Analytics", "Stakeholder Management"). Do NOT list frameworks separately '
        'if they belong to one skill (e.g., use "Python" not "Python, Django, Flask" as '
        'three separate skills).\n'
        '- skill_name_ru: Natural Russian name. If the CV is in Russian and uses a specific '
        'term, use that term. For English-origin tech terms, use the standard Russian '
        'transliteration (e.g., "Python" stays "Python", "API Design" becomes '
        '"Проектирование API").\n'
        '- original_term: The exact wording used in the CV text.\n'
        '- level: Estimate on 0-5 scale based on evidence depth:\n'
        '  0 = mentioned but no evidence of use\n'
        '  1 = awareness / basic exposure\n'
        '  2 = guided practice / used with supervision\n'
        '  3 = independent / productive without guidance\n'
        '  4 = advanced / leading others, architectural decisions\n'
        '  5 = expert / recognized authority, conference talks, patents\n'
        '- confidence: Integer 0-100. How sure are you this skill is accurately '
        'captured? 80+ means clear textual evidence. 50-79 means inferred from context. '
        'Below 50 means speculative.\n'
        '- category: One of "core", "domain", "leadership", "adjacent", "tool".\n'
        '- evidence: Quote or paraphrase the specific CV text that supports this skill.\n'
        '- aliases: Other names this skill might go by.\n\n'

        '### Role history\n'
        '- Extract each position with company_name, role_title, start_date, end_date.\n'
        '- Include responsibilities (what they DID, not what the role IS).\n'
        '- Include achievements (measurable outcomes, launches, improvements).\n'
        '- Include domains (industries, verticals, product types worked on).\n'
        '- Include leadership_signals (managed team of N, led initiative, mentored).\n'
        '- evidence: The CV text supporting this role entry.\n\n'

        '### Achievements, domain experience, leadership signals\n'
        '- Extract standalone achievements not tied to a specific role.\n'
        '- Extract domain expertise (e.g., "fintech", "e-commerce", "SaaS").\n'
        '- Extract leadership signals (hiring, coaching, cross-team coordination).\n\n'

        '### Quality flags\n'
        '- sparse_cv: Set true if the CV has fewer than 200 words of meaningful content.\n'
        '- sparse_reason: Explain why (e.g., "CV contains only a list of technologies '
        'without context or role history").\n'
        '- warnings: List any extraction issues (e.g., "Dates are inconsistent", '
        '"Multiple roles described without clear separation", "CV appears to be '
        'a template with placeholder text").\n\n'

        '## Critical constraints\n'
        '- NEVER assume absence of a skill in the CV means the person lacks it. '
        'Only extract what IS present.\n'
        '- Prefer 8-15 skills for a typical CV. Do not list 25+ micro-skills.\n'
        '- Do not split synonyms into separate skills.\n'
        '- If the CV is in Russian, extract everything faithfully — do not skip content '
        'because it is not in English.\n'
        '- If the CV is very short or appears to be a stub, set sparse_cv=true and '
        'still extract whatever is available.'
    )
    user_prompt = (
        f'## Source metadata\n'
        f'- File name: {source_title}\n'
        f'- Language hint: {source.language_code or "unknown"}\n\n'
        f'## CV text\n{(parsed_source.extracted_text or "")[:22000]}'
    )
    result = await call_openai_structured(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_name='employee_cv_profile_stage6',
        schema=CV_EXTRACTION_SCHEMA,
        temperature=0.1,
        max_tokens=5000,
    )
    return await sync_to_async(_normalize_cv_payload)(
        result.parsed,
        workspace=source.workspace,
    )


def _load_cv_sources_sync(workspace_pk, source_uuids: list[str]) -> list[WorkspaceSource]:
    queryset = WorkspaceSource.objects.select_related(
        'parsed_source',
        'workspace',
        'media_file',
        'cv_profile',
        'cv_profile__employee',
    ).filter(
        workspace_id=workspace_pk,
        source_kind=WorkspaceSourceKind.EMPLOYEE_CV,
        status=WorkspaceSourceStatus.PARSED,
        parsed_source__isnull=False,
    ).exclude(
        status=WorkspaceSourceStatus.ARCHIVED,
    ).order_by('created_at')
    if source_uuids:
        queryset = queryset.filter(uuid__in=source_uuids)
    return list(queryset)


def _resolve_requested_cv_sources_sync(workspace_pk, source_uuids: list[str]) -> dict[str, Any]:
    if not source_uuids:
        sources = _load_cv_sources_sync(workspace_pk, [])
        return {
            'processable_sources': sources,
            'ordered_processable_source_ids': [str(source.uuid) for source in sources],
            'prebuilt_results': [],
        }

    all_sources = {
        str(source.uuid): source
        for source in WorkspaceSource.objects.select_related(
            'parsed_source',
            'workspace',
            'media_file',
            'cv_profile',
            'cv_profile__employee',
        ).filter(
            workspace_id=workspace_pk,
            uuid__in=source_uuids,
        )
    }
    processable_sources: list[WorkspaceSource] = []
    ordered_processable_source_ids: list[str] = []
    prebuilt_results: list[dict[str, Any]] = []

    for requested_uuid in source_uuids:
        source = all_sources.get(str(requested_uuid))
        if source is None:
            prebuilt_results.append(
                {
                    'source_uuid': requested_uuid,
                    'source_title': '',
                    'status': 'missing',
                    'evidence_quality': '',
                    'employee_uuid': None,
                    'full_name': '',
                    'current_title': '',
                    'matched_by': '',
                    'match_confidence': 0.0,
                    'skill_evidence_count': 0,
                    'warnings': ['Requested source was not found in this workspace.'],
                    'vector_index_status': '',
                    'reused': False,
                }
            )
            continue
        if source.source_kind != WorkspaceSourceKind.EMPLOYEE_CV:
            prebuilt_results.append(
                {
                    'source_uuid': str(source.uuid),
                    'source_title': source.title or source.source_kind,
                    'status': 'wrong_kind',
                    'evidence_quality': '',
                    'employee_uuid': None,
                    'full_name': '',
                    'current_title': '',
                    'matched_by': '',
                    'match_confidence': 0.0,
                    'skill_evidence_count': 0,
                    'warnings': ['Requested source is not an employee CV.'],
                    'vector_index_status': '',
                    'reused': False,
                }
            )
            continue
        if source.status == WorkspaceSourceStatus.ARCHIVED:
            prebuilt_results.append(
                {
                    'source_uuid': str(source.uuid),
                    'source_title': source.title or source.source_kind,
                    'status': 'archived',
                    'evidence_quality': '',
                    'employee_uuid': None,
                    'full_name': '',
                    'current_title': '',
                    'matched_by': '',
                    'match_confidence': 0.0,
                    'skill_evidence_count': 0,
                    'warnings': ['Requested CV source is archived and must be restored or replaced before evidence build.'],
                    'vector_index_status': '',
                    'reused': False,
                }
            )
            continue
        if source.status != WorkspaceSourceStatus.PARSED or getattr(source, 'parsed_source', None) is None:
            prebuilt_results.append(
                {
                    'source_uuid': str(source.uuid),
                    'source_title': source.title or source.source_kind,
                    'status': 'parse_failed' if source.status == WorkspaceSourceStatus.FAILED else 'not_parsed',
                    'evidence_quality': '',
                    'employee_uuid': None,
                    'full_name': '',
                    'current_title': '',
                    'matched_by': '',
                    'match_confidence': 0.0,
                    'skill_evidence_count': 0,
                    'warnings': [
                        'Requested CV source has not been parsed yet.'
                        if source.status != WorkspaceSourceStatus.FAILED
                        else 'Requested CV source failed parsing and needs reparse before evidence build.'
                    ],
                    'vector_index_status': '',
                    'reused': False,
                }
            )
            continue
        processable_sources.append(source)
        ordered_processable_source_ids.append(str(source.uuid))

    return {
        'processable_sources': processable_sources,
        'ordered_processable_source_ids': ordered_processable_source_ids,
        'prebuilt_results': prebuilt_results,
    }


def _cv_profile_is_fresh(source: WorkspaceSource) -> bool:
    profile = getattr(source, 'cv_profile', None)
    parsed_source = getattr(source, 'parsed_source', None)
    if profile is None or parsed_source is None:
        return False
    if profile.status == EmployeeCVProfile.Status.EXTRACTION_FAILED:
        return False
    if profile.evidence_quality == EmployeeCVProfile.EvidenceQuality.FAILED:
        return False
    if profile.input_revision != _build_cv_input_revision(source):
        return False
    if str((profile.metadata or {}).get('schema_version') or '') != CV_PROFILE_SCHEMA_VERSION:
        return False
    if _profile_has_known_stale_rebuild_signal(profile):
        return False
    return True


def _profile_has_known_stale_rebuild_signal(profile: EmployeeCVProfile) -> bool:
    warnings = list((profile.metadata or {}).get('warnings') or [])
    for warning in warnings:
        normalized = str(warning or '').strip().lower()
        if not normalized:
            continue
        if 'cannot call this from an async context' in normalized:
            return True
        if 'async context' in normalized and 'sync_to_async' in normalized:
            return True
    return False


def _build_cv_input_revision(source: WorkspaceSource) -> str:
    parsed_source = getattr(source, 'parsed_source', None)
    if parsed_source is None:
        return ''
    fingerprint = '||'.join(
        [
            CV_PROFILE_SCHEMA_VERSION,
            str(source.language_code or ''),
            str(source.title or ''),
            str(getattr(source.media_file, 'original_filename', '') or ''),
            str(parsed_source.uuid or ''),
            parsed_source.updated_at.isoformat() if getattr(parsed_source, 'updated_at', None) else '',
            str(parsed_source.content_type or ''),
            str(parsed_source.word_count or 0),
            str(parsed_source.char_count or 0),
        ]
    )
    return hashlib.sha1(fingerprint.encode('utf-8')).hexdigest()


def _cv_profile_needs_index_refresh(source: WorkspaceSource) -> bool:
    profile = getattr(source, 'cv_profile', None)
    if profile is None:
        return False
    if profile.status != EmployeeCVProfile.Status.MATCHED or profile.employee_id is None:
        return False
    vector_index = dict(profile.metadata or {}).get('vector_index') or {}
    if str(vector_index.get('index_version') or '') != CV_EVIDENCE_INDEX_VERSION:
        return True
    if profile.active_vector_generation_id:
        return False
    return str(vector_index.get('reason') or '') not in {'no_structured_cv_evidence'}


def _refresh_existing_cv_index_sync(source_pk) -> dict:
    source = WorkspaceSource.objects.select_related('workspace', 'cv_profile', 'cv_profile__employee').get(pk=source_pk)
    profile = getattr(source, 'cv_profile', None)
    if profile is None:
        return _build_cv_profile_result_sync(source.pk, True)

    previous_metadata = dict(profile.metadata or {})
    warnings = list(previous_metadata.get('warnings') or [])
    fact_counts = dict(previous_metadata.get('fact_counts') or {})
    pending_skill_candidates = list(previous_metadata.get('pending_skill_candidates') or [])
    match_details = {
        'candidate_matches': previous_metadata.get('candidate_matches') or [],
    }
    try:
        vector_index = index_employee_cv_profile_sync(profile.pk)
    except Exception as exc:
        logger.warning('CV evidence re-index failed for source %s: %s', source.uuid, exc, exc_info=True)
        vector_index = {
            'status': 'failed',
            'reason': 'indexing_exception',
            'message': str(exc),
            'active_generation_id': profile.active_vector_generation_id,
        }

    if vector_index.get('active_generation_id', '') != profile.active_vector_generation_id:
        profile.active_vector_generation_id = str(vector_index.get('active_generation_id') or '')
    vector_index = _json_safe(vector_index)
    profile.metadata = {
        **previous_metadata,
        'vector_index': vector_index,
    }
    profile.metadata = _json_safe(profile.metadata)
    profile.save(update_fields=['active_vector_generation_id', 'metadata', 'updated_at'])
    _merge_cv_source_metadata(
        source,
        profile=profile,
        warnings=warnings,
        match_details=match_details,
        vector_index=vector_index,
        fact_counts=fact_counts,
        pending_skill_candidates=pending_skill_candidates,
    )
    return _build_cv_profile_result_sync(source.pk, True)


def _normalize_cv_payload(payload: dict[str, Any], workspace=None) -> dict[str, Any]:
    from skill_blueprint.services import normalize_external_role_title

    raw_role_family = str(payload.get('role_family') or '').strip()
    normalized_role = normalize_external_role_title(
        role_name=str(payload.get('current_role') or payload.get('headline') or '').strip(),
        role_family_hint=raw_role_family,
        department=str(payload.get('current_department') or '').strip(),
        page_url='',
    )
    normalized = {
        'candidate_name': str(payload.get('candidate_name') or '').strip(),
        'email': _normalize_email(payload.get('email')),
        'headline': str(payload.get('headline') or '').strip(),
        'summary': str(payload.get('summary') or '').strip(),
        'seniority': str(payload.get('seniority') or '').strip(),
        'current_role': str(payload.get('current_role') or '').strip(),
        'role_family': normalized_role.get('canonical_family', ''),
        'raw_role_family': raw_role_family,
        'current_department': str(payload.get('current_department') or '').strip(),
        'languages': dedupe_strings(payload.get('languages') or []),
        'warnings': dedupe_strings(payload.get('warnings') or []),
        'sparse_cv': bool(payload.get('sparse_cv')),
        'sparse_reason': str(payload.get('sparse_reason') or '').strip(),
        'skills': [],
        'role_history': [],
        'achievements': [],
        'domain_experience': [],
        'leadership_signals': [],
    }

    for item in payload.get('skills', []):
        normalized_skill = normalize_skill_seed(
            item.get('skill_name_en') or item.get('skill_name_ru') or item.get('original_term') or '',
            workspace=workspace,
            review_metadata={
                'source': 'cv_normalization',
                'candidate_name': str(payload.get('candidate_name') or '').strip(),
                'current_role': str(payload.get('current_role') or '').strip(),
            },
            allow_freeform=False,
        )
        confidence_value = item.get('confidence')
        if confidence_value in (None, ''):
            confidence_value = round(float(item.get('confidence_score') or 0.6) * 100)
        normalized['skills'].append(
            {
                'canonical_key': str(item.get('canonical_key') or normalized_skill.get('canonical_key', '')).strip(),
                'skill_name_en': normalized_skill.get('display_name_en', ''),
                'skill_name_ru': str(item.get('skill_name_ru') or normalized_skill.get('display_name_ru') or '').strip(),
                'original_term': str(item.get('original_term') or item.get('skill_name_en') or '').strip(),
                'level': _coerce_int(item.get('level'), default=2, minimum=0, maximum=5),
                'confidence': _coerce_int(confidence_value, default=60, minimum=0, maximum=100),
                'confidence_score': round(_coerce_int(confidence_value, default=60, minimum=0, maximum=100) / 100, 2),
                'category': str(item.get('category') or '').strip(),
                'aliases': dedupe_strings([
                    *(normalized_skill.get('aliases') or []),
                    *(item.get('aliases') or []),
                ]),
                'evidence': str(item.get('evidence') or '').strip(),
                'match_source': str(normalized_skill.get('match_source') or '').strip(),
                'needs_review': bool(normalized_skill.get('needs_review')),
                'esco_skill_id': normalized_skill.get('esco_skill_id'),
                'esco_skill_uri': str(normalized_skill.get('esco_skill_uri') or '').strip(),
            }
        )

    for item in payload.get('role_history', []):
        confidence_value = item.get('confidence')
        if confidence_value in (None, ''):
            confidence_value = round(float(item.get('confidence_score') or 0.6) * 100)
        normalized['role_history'].append(
            {
                'company_name': str(item.get('company_name') or '').strip(),
                'role_title': str(item.get('role_title') or '').strip(),
                'start_date': str(item.get('start_date') or '').strip(),
                'end_date': str(item.get('end_date') or '').strip(),
                'responsibilities': dedupe_strings(item.get('responsibilities') or []),
                'achievements': dedupe_strings(item.get('achievements') or []),
                'domains': dedupe_strings(item.get('domains') or []),
                'leadership_signals': dedupe_strings(item.get('leadership_signals') or []),
                'evidence': str(item.get('evidence') or '').strip(),
                'confidence_score': round(_coerce_int(confidence_value, default=60, minimum=0, maximum=100) / 100, 2),
            }
        )

    for item in payload.get('achievements', []):
        confidence_value = item.get('confidence')
        if confidence_value in (None, ''):
            confidence_value = round(float(item.get('confidence_score') or 0.6) * 100)
        normalized['achievements'].append(
            {
                'summary': str(item.get('summary') or '').strip(),
                'evidence': str(item.get('evidence') or '').strip(),
                'confidence_score': round(_coerce_int(confidence_value, default=60, minimum=0, maximum=100) / 100, 2),
            }
        )

    for item in payload.get('domain_experience', []):
        confidence_value = item.get('confidence')
        if confidence_value in (None, ''):
            confidence_value = round(float(item.get('confidence_score') or 0.6) * 100)
        normalized['domain_experience'].append(
            {
                'domain': str(item.get('domain') or '').strip(),
                'evidence': str(item.get('evidence') or '').strip(),
                'confidence_score': round(_coerce_int(confidence_value, default=60, minimum=0, maximum=100) / 100, 2),
            }
        )

    for item in payload.get('leadership_signals', []):
        confidence_value = item.get('confidence')
        if confidence_value in (None, ''):
            confidence_value = round(float(item.get('confidence_score') or 0.6) * 100)
        normalized['leadership_signals'].append(
            {
                'signal': str(item.get('signal') or '').strip(),
                'evidence': str(item.get('evidence') or '').strip(),
                'confidence_score': round(_coerce_int(confidence_value, default=60, minimum=0, maximum=100) / 100, 2),
            }
        )

    return normalized


def _normalize_email(value: Any) -> str:
    return str(value or '').strip().lower()


def _normalize_person_name(value: str) -> str:
    text = str(value or '').strip()
    text = re.sub(r'\.(pdf|docx?|rtf)$', '', text, flags=re.IGNORECASE)
    text = re.sub(r'[_\-]+', ' ', text)
    text = re.sub(r'\b(cv|resume|резюме|curriculum vitae)\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'[^0-9A-Za-zА-Яа-яЁё\s]+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip().casefold()
    return text


def _extract_candidate_names(extracted: dict[str, Any], source: WorkspaceSource) -> list[str]:
    title_name = source.title or (
        source.media_file.original_filename if source.media_file is not None else ''
    )
    names = dedupe_strings([
        extracted.get('candidate_name', ''),
        title_name,
    ])
    return [name for name in names if _normalize_person_name(name)]


def _title_similarity(lhs: str, rhs: str) -> float:
    left = re.sub(r'\s+', ' ', str(lhs or '').strip()).casefold()
    right = re.sub(r'\s+', ' ', str(rhs or '').strip()).casefold()
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.8
    left_tokens = set(re.findall(r'[a-zа-я0-9]+', left))
    right_tokens = set(re.findall(r'[a-zа-я0-9]+', right))
    if left_tokens and right_tokens:
        overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
        if overlap > 0:
            return round(overlap, 2)
    return 0.0


def _department_similarity(lhs: str, rhs: str) -> float:
    left = _normalize_person_name(lhs)
    right = _normalize_person_name(rhs)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.8
    return round(SequenceMatcher(None, left, right).ratio(), 2)


def _build_match_candidates(
    employees: list[Employee],
    *,
    candidate_names: list[str],
    candidate_email: str,
    current_role: str,
    current_department: str,
    employee_departments: dict[str, list[str]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    normalized_candidate_names = [_normalize_person_name(item) for item in candidate_names]
    for employee in employees:
        employee_name = _normalize_person_name(employee.full_name)
        if not employee_name:
            continue
        name_score = max(
            [SequenceMatcher(None, candidate_name, employee_name).ratio() for candidate_name in normalized_candidate_names],
            default=0.0,
        )
        exact_name_match = any(candidate_name == employee_name for candidate_name in normalized_candidate_names)
        title_score = _title_similarity(current_role, employee.current_title)
        department_score = max(
            [_department_similarity(current_department, department_name) for department_name in employee_departments.get(str(employee.uuid), [])],
            default=0.0,
        )
        email_match = bool(candidate_email and employee.email and employee.email.casefold() == candidate_email)
        score = name_score
        if exact_name_match:
            score = max(score, 0.92)
        if email_match:
            score = 1.0
        else:
            score = round(
                (score * 0.82)
                + (title_score * 0.14 if current_role else 0.0)
                + (department_score * 0.04 if current_department else 0.0),
                4,
            )
            score = min(1.0, score)
        candidates.append(
            {
                'employee_uuid': str(employee.uuid),
                'full_name': employee.full_name,
                'email': employee.email,
                'current_title': employee.current_title,
                'departments': employee_departments.get(str(employee.uuid), []),
                'name_score': round(name_score, 4),
                'title_score': round(title_score, 4),
                'department_score': round(department_score, 4),
                'score': round(score, 4),
                'exact_name_match': exact_name_match,
                'email_match': email_match,
            }
        )
    return sorted(candidates, key=lambda item: (-item['score'], -item['name_score'], item['full_name']))


def _match_employee_for_cv_sync(workspace, extracted: dict[str, Any], source: WorkspaceSource) -> dict[str, Any]:
    employees = list(Employee.objects.filter(workspace=workspace).order_by('full_name'))
    candidate_names = _extract_candidate_names(extracted, source)
    candidate_email = _normalize_email(extracted.get('email'))
    current_role = str(extracted.get('current_role') or '').strip()
    current_department = str(extracted.get('current_department') or '').strip()
    employee_departments: dict[str, list[str]] = {}
    for assignment in (
        EmployeeOrgAssignment.objects.select_related('org_unit', 'employee')
        .filter(workspace=workspace, is_primary=True)
        .order_by('employee_id')
    ):
        employee_departments.setdefault(str(assignment.employee_id), []).append(assignment.org_unit.name)

    if not employees:
        return {
            'status': EmployeeCVProfile.Status.UNMATCHED,
            'match_confidence': 0.0,
            'matched_by': '',
            'employee_uuid': None,
            'candidate_matches': [],
            'message': 'No employees exist in the workspace yet.',
        }

    email_matches = [
        employee for employee in employees
        if candidate_email and employee.email and employee.email.casefold() == candidate_email
    ]
    if len(email_matches) == 1:
        employee = email_matches[0]
        return {
            'status': EmployeeCVProfile.Status.MATCHED,
            'match_confidence': 1.0,
            'matched_by': 'email_exact',
            'employee_uuid': str(employee.uuid),
            'candidate_matches': [
                {
                    'employee_uuid': str(employee.uuid),
                    'full_name': employee.full_name,
                    'email': employee.email,
                    'current_title': employee.current_title,
                    'departments': employee_departments.get(str(employee.uuid), []),
                    'score': 1.0,
                    'name_score': 1.0,
                    'title_score': _title_similarity(current_role, employee.current_title),
                    'department_score': max(
                        [
                            _department_similarity(current_department, department_name)
                            for department_name in employee_departments.get(str(employee.uuid), [])
                        ],
                        default=0.0,
                    ),
                    'exact_name_match': True,
                    'email_match': True,
                }
            ],
            'message': 'Matched employee by exact email.',
        }
    if len(email_matches) > 1:
        return {
            'status': EmployeeCVProfile.Status.AMBIGUOUS,
            'match_confidence': 0.6,
            'matched_by': 'email_duplicate',
            'employee_uuid': None,
            'candidate_matches': [
                {
                    'employee_uuid': str(employee.uuid),
                    'full_name': employee.full_name,
                    'email': employee.email,
                    'current_title': employee.current_title,
                    'departments': employee_departments.get(str(employee.uuid), []),
                    'score': 1.0,
                    'name_score': 1.0,
                    'title_score': _title_similarity(current_role, employee.current_title),
                    'department_score': max(
                        [
                            _department_similarity(current_department, department_name)
                            for department_name in employee_departments.get(str(employee.uuid), [])
                        ],
                        default=0.0,
                    ),
                    'exact_name_match': True,
                    'email_match': True,
                }
                for employee in email_matches
            ],
            'message': 'Multiple employees share the same email match.',
        }

    candidates = _build_match_candidates(
        employees,
        candidate_names=candidate_names,
        candidate_email=candidate_email,
        current_role=current_role,
        current_department=current_department,
        employee_departments=employee_departments,
    )
    if not candidates:
        return {
            'status': EmployeeCVProfile.Status.UNMATCHED,
            'match_confidence': 0.0,
            'matched_by': '',
            'employee_uuid': None,
            'candidate_matches': [],
            'message': 'No plausible employee match was found.',
        }

    top = candidates[0]
    second = candidates[1] if len(candidates) > 1 else None
    score_gap = round(top['score'] - (second['score'] if second is not None else 0.0), 4)

    if top['exact_name_match'] and top['score'] >= 0.92 and score_gap >= 0.08:
        return {
            'status': EmployeeCVProfile.Status.MATCHED,
            'match_confidence': round(top['score'], 2),
            'matched_by': 'full_name_exact' if top['title_score'] < 0.5 else 'full_name_plus_title',
            'employee_uuid': top['employee_uuid'],
            'candidate_matches': candidates[:5],
            'message': 'Matched employee by unique full-name alignment.',
        }

    if top['score'] >= 0.9 and score_gap >= 0.1 and top['title_score'] >= 0.5:
        return {
            'status': EmployeeCVProfile.Status.MATCHED,
            'match_confidence': round(top['score'], 2),
            'matched_by': 'fuzzy_name_plus_title',
            'employee_uuid': top['employee_uuid'],
            'candidate_matches': candidates[:5],
            'message': 'Matched employee by high-confidence fuzzy name and title alignment.',
        }

    if top['score'] >= 0.84 and score_gap >= 0.06:
        return {
            'status': EmployeeCVProfile.Status.LOW_CONFIDENCE_MATCH,
            'match_confidence': round(top['score'], 2),
            'matched_by': 'low_confidence_name_match',
            'employee_uuid': top['employee_uuid'],
            'candidate_matches': candidates[:5],
            'message': 'A plausible employee match was found, but confidence is too low for automatic evidence attachment.',
        }

    if second is not None and second['score'] >= top['score'] - 0.04:
        return {
            'status': EmployeeCVProfile.Status.AMBIGUOUS,
            'match_confidence': round(top['score'], 2),
            'matched_by': 'ambiguous_name_match',
            'employee_uuid': None,
            'candidate_matches': candidates[:5],
            'message': 'Multiple employees look similarly plausible for this CV.',
        }

    return {
        'status': EmployeeCVProfile.Status.UNMATCHED,
        'match_confidence': round(top['score'], 2),
        'matched_by': '',
        'employee_uuid': None,
        'candidate_matches': candidates[:5],
        'message': 'The CV could not be matched safely to an existing employee.',
    }


def _sync_profile_candidate_matches_sync(profile: EmployeeCVProfile, candidate_matches: list[dict[str, Any]]) -> None:
    EmployeeCVMatchCandidate.objects.filter(profile=profile).delete()
    rows: list[EmployeeCVMatchCandidate] = []
    for rank, candidate in enumerate(candidate_matches or [], start=1):
        employee_uuid = str(candidate.get('employee_uuid') or '').strip()
        if not employee_uuid:
            continue
        employee = Employee.objects.filter(workspace=profile.workspace, uuid=employee_uuid).first()
        if employee is None:
            continue
        rows.append(
            EmployeeCVMatchCandidate(
                workspace=profile.workspace,
                profile=profile,
                employee=employee,
                rank=rank,
                score=round(float(candidate.get('score') or 0.0), 4),
                name_score=round(float(candidate.get('name_score') or 0.0), 4),
                title_score=round(float(candidate.get('title_score') or 0.0), 4),
                department_score=round(float(candidate.get('department_score') or 0.0), 4),
                exact_name_match=bool(candidate.get('exact_name_match')),
                email_match=bool(candidate.get('email_match')),
                metadata={
                    'full_name': candidate.get('full_name', ''),
                    'email': candidate.get('email', ''),
                    'current_title': candidate.get('current_title', ''),
                    'departments': list(candidate.get('departments') or []),
                },
            )
        )
    if rows:
        EmployeeCVMatchCandidate.objects.bulk_create(rows)


def _serialize_profile_candidate_matches(profile: EmployeeCVProfile) -> list[dict[str, Any]]:
    candidates = list(
        profile.candidate_matches.select_related('employee').order_by('rank', '-score')
    )
    if candidates:
        return [
            {
                'employee_uuid': str(candidate.employee.uuid),
                'full_name': candidate.metadata.get('full_name') or candidate.employee.full_name,
                'email': candidate.metadata.get('email') or candidate.employee.email,
                'current_title': candidate.metadata.get('current_title') or candidate.employee.current_title,
                'departments': candidate.metadata.get('departments') or [],
                'name_score': float(candidate.name_score or 0.0),
                'title_score': float(candidate.title_score or 0.0),
                'department_score': float(candidate.department_score or 0.0),
                'score': float(candidate.score or 0.0),
                'exact_name_match': candidate.exact_name_match,
                'email_match': candidate.email_match,
            }
            for candidate in candidates
        ]
    return list((profile.metadata or {}).get('candidate_matches') or [])


def _candidate_profiles_for_employee_sync(workspace_pk, employee_uuid) -> list[EmployeeCVProfile]:
    candidate_profiles = [
        candidate.profile
        for candidate in EmployeeCVMatchCandidate.objects.select_related('profile', 'profile__source').filter(
            workspace_id=workspace_pk,
            employee__uuid=employee_uuid,
            profile__employee__isnull=True,
        ).order_by('rank', '-score')
    ]
    if candidate_profiles:
        return candidate_profiles

    # Backward-compatible fallback for legacy/test profiles that still only have
    # candidate_matches in JSON metadata and have not been backfilled yet.
    return [
        profile
        for profile in EmployeeCVProfile.objects.select_related('source').prefetch_related('candidate_matches__employee').filter(
            workspace_id=workspace_pk,
            employee__isnull=True,
        )
        if any(
            str(candidate.get('employee_uuid') or '') == str(employee_uuid)
            for candidate in (profile.metadata or {}).get('candidate_matches', [])
        )
    ]


def _coerce_int(value: Any, *, default: int = 0, minimum: int = 0, maximum: Optional[int] = None) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    normalized = max(minimum, normalized)
    if maximum is not None:
        normalized = min(maximum, normalized)
    return normalized


def _determine_evidence_quality(extracted: dict[str, Any]) -> str:
    skill_count = len(extracted.get('skills') or [])
    role_history_count = len(extracted.get('role_history') or [])
    achievement_count = len(extracted.get('achievements') or [])
    domain_count = len(extracted.get('domain_experience') or [])
    leadership_count = len(extracted.get('leadership_signals') or [])

    if extracted.get('sparse_cv'):
        return EmployeeCVProfile.EvidenceQuality.SPARSE
    if skill_count >= 5 and role_history_count >= 2:
        return EmployeeCVProfile.EvidenceQuality.STRONG
    if skill_count >= 2 or role_history_count >= 1 or achievement_count + domain_count + leadership_count >= 2:
        return EmployeeCVProfile.EvidenceQuality.USABLE
    if skill_count or role_history_count or achievement_count or domain_count or leadership_count:
        return EmployeeCVProfile.EvidenceQuality.SPARSE
    return EmployeeCVProfile.EvidenceQuality.EMPTY


def _aggregate_skill_evidence_items(skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    aggregated: dict[str, dict[str, Any]] = {}
    for skill in skills:
        canonical_key = str(skill.get('canonical_key') or '').strip()
        if not canonical_key:
            continue
        bucket = aggregated.setdefault(
            canonical_key,
            {
                'canonical_key': canonical_key,
                'skill_name_en': skill.get('skill_name_en', ''),
                'skill_name_ru': skill.get('skill_name_ru', ''),
                'original_terms': [],
                'level': 0,
                'confidence_score': 0.0,
                'category': skill.get('category', ''),
                'aliases': [],
                'evidence_texts': [],
                'match_source': str(skill.get('match_source') or '').strip(),
                'needs_review': bool(skill.get('needs_review')),
                'esco_skill_id': skill.get('esco_skill_id'),
                'esco_skill_uri': str(skill.get('esco_skill_uri') or '').strip(),
            },
        )
        bucket['level'] = max(bucket['level'], _coerce_int(skill.get('level'), default=2, minimum=0, maximum=5))
        bucket['confidence_score'] = max(bucket['confidence_score'], float(skill.get('confidence_score') or 0.0))
        bucket['category'] = bucket['category'] or skill.get('category', '')
        bucket['skill_name_en'] = bucket['skill_name_en'] or skill.get('skill_name_en', '')
        bucket['skill_name_ru'] = bucket['skill_name_ru'] or skill.get('skill_name_ru', '')
        bucket['original_terms'] = dedupe_strings([
            *bucket['original_terms'],
            skill.get('original_term', ''),
        ])
        bucket['aliases'] = dedupe_strings([
            *bucket['aliases'],
            *(skill.get('aliases') or []),
        ])
        bucket['evidence_texts'] = dedupe_strings([
            *bucket['evidence_texts'],
            skill.get('evidence', ''),
        ])
        if not bucket.get('match_source') and skill.get('match_source'):
            bucket['match_source'] = str(skill.get('match_source') or '').strip()
        bucket['needs_review'] = bool(bucket.get('needs_review') or skill.get('needs_review'))
        if not bucket.get('esco_skill_id') and skill.get('esco_skill_id'):
            bucket['esco_skill_id'] = skill.get('esco_skill_id')
        if not bucket.get('esco_skill_uri') and skill.get('esco_skill_uri'):
            bucket['esco_skill_uri'] = str(skill.get('esco_skill_uri') or '').strip()
    return list(aggregated.values())


def _weight_for_skill_category(category: str) -> float:
    normalized = str(category or '').strip().lower()
    if normalized in {'core', 'technical', 'primary'}:
        return 0.8
    if normalized in {'domain', 'product', 'analytics', 'leadership'}:
        return 0.65
    if normalized in {'adjacent', 'secondary'}:
        return 0.55
    return 0.6


def _build_pending_skill_candidate_payload(
    item: dict[str, Any],
    normalized_skill: dict[str, Any],
) -> dict[str, Any]:
    candidate_key = normalize_lookup_key(
        str(item.get('canonical_key') or '')
        or str(item.get('skill_name_en') or '')
        or str((item.get('original_terms') or [''])[0] or '')
    )
    return {
        'candidate_key': candidate_key,
        'proposed_key': normalized_skill.get('canonical_key', ''),
        'display_name_en': normalized_skill.get('display_name_en', ''),
        'display_name_ru': item.get('skill_name_ru', ''),
        'original_terms': item.get('original_terms', []),
        'aliases': item.get('aliases', []),
        'category': item.get('category', ''),
        'confidence_score': float(item.get('confidence_score') or 0.0),
        'evidence_texts': item.get('evidence_texts', []),
    }


def _set_skill_state(
    skill: Skill,
    *,
    resolution_status: str | None = None,
    is_operator_confirmed: bool | None = None,
    display_name_en: str | None = None,
    display_name_ru: str | None = None,
    esco_skill=None,
    source_terms: list[str] | None = None,
) -> None:
    update_fields: list[str] = []
    if resolution_status is not None and skill.resolution_status != resolution_status:
        skill.resolution_status = resolution_status
        update_fields.append('resolution_status')
    if is_operator_confirmed is not None and skill.is_operator_confirmed != is_operator_confirmed:
        skill.is_operator_confirmed = is_operator_confirmed
        update_fields.append('is_operator_confirmed')
    if display_name_en is not None and skill.display_name_en != display_name_en:
        skill.display_name_en = display_name_en
        update_fields.append('display_name_en')
    if display_name_ru is not None and skill.display_name_ru != display_name_ru:
        skill.display_name_ru = display_name_ru
        update_fields.append('display_name_ru')
    if esco_skill is not None and skill.esco_skill_id != getattr(esco_skill, 'pk', None):
        skill.esco_skill = esco_skill
        update_fields.append('esco_skill')
    if source_terms is not None:
        merged_terms = dedupe_strings([*(skill.source_terms or []), *source_terms])
        if merged_terms != list(skill.source_terms or []):
            skill.source_terms = merged_terms
            update_fields.append('source_terms')
    if update_fields:
        skill.save(update_fields=[*update_fields, 'updated_at'])


def _collect_skill_terms(
    skill: Skill,
    *,
    evidence_row: EmployeeSkillEvidence | None = None,
    extra_terms: list[str] | None = None,
) -> list[str]:
    metadata = dict((evidence_row.metadata or {}) if evidence_row is not None else {})
    return dedupe_strings(
        [
            skill.display_name_en,
            skill.display_name_ru,
            *(skill.source_terms or []),
            *(metadata.get('original_terms') or []),
            *(metadata.get('aliases') or []),
            *(extra_terms or []),
        ]
    )


def _upsert_skill_resolution_overrides(
    workspace: IntakeWorkspace,
    *,
    terms: list[str],
    target_skill: Skill,
    status: str,
    source: str,
    note: str = '',
    metadata: dict[str, Any] | None = None,
) -> None:
    clean_terms = dedupe_strings(terms)
    for term in clean_terms:
        normalized_term = normalize_lookup_key(term)
        if not normalized_term:
            continue
        alias_terms = dedupe_strings([value for value in clean_terms if value.casefold() != term.casefold()])
        SkillResolutionOverride.objects.update_or_create(
            workspace=workspace,
            normalized_term=normalized_term,
            defaults={
                'raw_term': str(term).strip(),
                'canonical_key': target_skill.canonical_key,
                'display_name_en': target_skill.display_name_en,
                'display_name_ru': target_skill.display_name_ru,
                'esco_skill_id': target_skill.esco_skill_id,
                'aliases': alias_terms,
                'status': status,
                'source': source,
                'notes': str(note or '').strip(),
                'metadata': _json_safe(metadata or {}),
            },
        )


def _mark_catalog_review_items_resolved(
    workspace: IntakeWorkspace,
    *,
    terms: list[str],
    resolved_via: str,
    operator_name: str = '',
    override_canonical_key: str = '',
) -> None:
    normalized_terms = [normalize_lookup_key(term) for term in terms if normalize_lookup_key(term)]
    if not normalized_terms:
        return
    for item in CatalogResolutionReviewItem.objects.filter(
        workspace=workspace,
        term_kind=CatalogResolutionReviewItem.TermKind.SKILL,
        normalized_term__in=normalized_terms,
    ):
        item.status = CatalogReviewStatus.RESOLVED
        item.metadata = {
            **dict(item.metadata or {}),
            'resolved_via': resolved_via,
            'resolved_at': datetime.utcnow().isoformat(),
            'operator_name': str(operator_name or '').strip(),
            'override_canonical_key': str(override_canonical_key or '').strip(),
        }
        item.save(update_fields=['status', 'metadata', 'last_seen_at', 'updated_at'])


def _maybe_mark_skill_rejected_if_unused(skill: Skill) -> None:
    has_active_evidence = EmployeeSkillEvidence.objects.filter(
        workspace=skill.workspace,
        skill=skill,
        weight__gt=0,
    ).exists()
    if not has_active_evidence:
        _set_skill_state(
            skill,
            resolution_status=Skill.ResolutionStatus.REJECTED,
            is_operator_confirmed=skill.is_operator_confirmed,
        )


def _build_skill_review_decision_lookup(employee: Employee) -> dict[str, SkillReviewDecision]:
    decisions = SkillReviewDecision.objects.filter(
        workspace=employee.workspace,
        employee=employee,
    ).order_by('-reviewed_at', '-updated_at', '-created_at', '-pk')
    lookup: dict[str, SkillReviewDecision] = {}
    for decision in decisions:
        lookup.setdefault(str(decision.skill_canonical_key or '').strip(), decision)
    return lookup


def _candidate_matches_skill_terms(candidate: dict[str, Any], *, canonical_keys: set[str], normalized_terms: set[str]) -> bool:
    if str(candidate.get('proposed_key') or '').strip() in canonical_keys:
        return True
    candidate_terms = {
        normalize_lookup_key(value)
        for value in [
            str(candidate.get('candidate_key') or '').strip(),
            str(candidate.get('display_name_en') or '').strip(),
            str(candidate.get('display_name_ru') or '').strip(),
            *(candidate.get('original_terms') or []),
            *(candidate.get('aliases') or []),
        ]
        if normalize_lookup_key(value)
    }
    return bool(candidate_terms & normalized_terms)


def _clear_pending_skill_candidates_for_profile(
    profile: EmployeeCVProfile,
    *,
    canonical_keys: list[str],
    terms: list[str],
) -> None:
    pending_candidates = list((profile.metadata or {}).get('pending_skill_candidates') or [])
    if not pending_candidates:
        return
    canonical_key_set = {str(key or '').strip() for key in canonical_keys if str(key or '').strip()}
    normalized_term_set = {
        normalize_lookup_key(term)
        for term in terms
        if normalize_lookup_key(term)
    }
    remaining_candidates = [
        candidate
        for candidate in pending_candidates
        if not _candidate_matches_skill_terms(
            candidate,
            canonical_keys=canonical_key_set,
            normalized_terms=normalized_term_set,
        )
    ]
    if remaining_candidates == pending_candidates:
        return
    profile.metadata = {
        **dict(profile.metadata or {}),
        'pending_skill_candidates': _json_safe(remaining_candidates),
    }
    profile.metadata = _json_safe(profile.metadata)
    profile.save(update_fields=['metadata', 'updated_at'])


def _reindex_cv_profiles_for_sources(source_ids: set[int]) -> None:
    if not source_ids:
        return
    profile_ids = list(
        EmployeeCVProfile.objects.filter(source_id__in=sorted(source_ids)).values_list('pk', flat=True)
    )
    for profile_id in profile_ids:
        try:
            index_employee_cv_profile_sync(profile_id)
        except Exception as exc:
            logger.warning('CV evidence indexing failed while reindexing profile %s: %s', profile_id, exc, exc_info=True)


def _persist_skill_evidence_rows(profile: EmployeeCVProfile, extracted: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    employee = profile.employee
    source = profile.source
    if employee is None:
        return 0, []

    evidence_count = 0
    pending_skill_candidates: list[dict[str, Any]] = []
    review_decisions = _build_skill_review_decision_lookup(employee)
    skills_to_recheck: set[int] = set()
    for item in _aggregate_skill_evidence_items(extracted.get('skills') or []):
        normalized_skill = {
            'canonical_key': str(item.get('canonical_key') or '').strip(),
            'display_name_en': str(item.get('skill_name_en') or '').strip(),
            'display_name_ru': str(item.get('skill_name_ru') or '').strip(),
            'aliases': list(item.get('aliases') or []),
            'esco_skill_id': item.get('esco_skill_id'),
            'esco_skill_uri': str(item.get('esco_skill_uri') or '').strip(),
            'match_source': str(item.get('match_source') or '').strip(),
            'needs_review': bool(item.get('needs_review')),
        }
        skill, normalized_skill, is_resolved = resolve_workspace_skill_sync(
            profile.workspace,
            raw_term=item.get('skill_name_en') or item.get('skill_name_ru') or '',
            normalized_skill=normalized_skill,
            preferred_display_name_ru=item.get('skill_name_ru', ''),
            aliases=[
                *(item.get('aliases') or []),
                *(item.get('original_terms') or []),
            ],
            created_source='employee_cv_seed',
            promote_aliases=False,
            allow_freeform=False,
        )
        if normalized_skill.get('is_rejected'):
            continue
        if bool(normalized_skill.get('needs_review')) or (
            skill is not None and skill.resolution_status == Skill.ResolutionStatus.PENDING_REVIEW
        ):
            pending_skill_candidates.append(_build_pending_skill_candidate_payload(item, normalized_skill))
        if skill is None:
            continue
        evidence_text = '\n'.join(item.get('evidence_texts') or [])[:4000]
        evidence_skill = skill
        weight = _weight_for_skill_category(item.get('category', ''))
        is_operator_confirmed = False
        operator_action = ''
        operator_note = ''
        metadata = {
            'evidence_category': item.get('category', ''),
            'original_terms': item.get('original_terms', []),
            'aliases': item.get('aliases', []),
            'source_uuid': str(source.uuid),
            'cv_profile_uuid': str(profile.uuid),
            'snippet': evidence_text,
            'skill_key': skill.canonical_key,
            'resolution_status': skill.resolution_status,
        }
        review_decision = review_decisions.get(skill.canonical_key)
        if review_decision is not None:
            operator_note = review_decision.note
            if review_decision.action == EmployeeSkillEvidence.OperatorAction.ACCEPTED:
                _set_skill_state(
                    skill,
                    resolution_status=Skill.ResolutionStatus.RESOLVED,
                    is_operator_confirmed=True,
                )
                metadata['resolution_status'] = Skill.ResolutionStatus.RESOLVED
                is_operator_confirmed = True
                operator_action = EmployeeSkillEvidence.OperatorAction.ACCEPTED
            elif review_decision.action == EmployeeSkillEvidence.OperatorAction.REJECTED:
                weight = 0
                operator_action = EmployeeSkillEvidence.OperatorAction.REJECTED
            elif review_decision.action == EmployeeSkillEvidence.OperatorAction.MERGED and review_decision.merge_target_skill_uuid:
                merge_target = Skill.objects.filter(
                    workspace=profile.workspace,
                    uuid=review_decision.merge_target_skill_uuid,
                ).first()
                if merge_target is not None:
                    evidence_skill = merge_target
                    metadata.update(
                        {
                            'skill_key': merge_target.canonical_key,
                            'resolution_status': merge_target.resolution_status,
                            'merged_from_skill_uuid': str(skill.uuid),
                            'merged_from_skill_key': skill.canonical_key,
                        }
                    )
                    is_operator_confirmed = True
                    operator_action = EmployeeSkillEvidence.OperatorAction.MERGED
                    skills_to_recheck.add(skill.pk)
        EmployeeSkillEvidence.objects.create(
            workspace=profile.workspace,
            employee=employee,
            skill=evidence_skill,
            source_kind=_CV_EVIDENCE_SOURCE_KIND,
            source=source,
            current_level=item.get('level', 0),
            confidence=round(float(item.get('confidence_score') or 0.0), 2),
            weight=weight,
            evidence_text=evidence_text,
            metadata=metadata,
            is_operator_confirmed=is_operator_confirmed,
            operator_action=operator_action,
            operator_note=operator_note,
        )
        evidence_count += 1
        if operator_action == EmployeeSkillEvidence.OperatorAction.REJECTED:
            skills_to_recheck.add(skill.pk)
    for skill_pk in skills_to_recheck:
        reviewed_skill = Skill.objects.filter(pk=skill_pk).first()
        if reviewed_skill is not None:
            _maybe_mark_skill_rejected_if_unused(reviewed_skill)
    return evidence_count, pending_skill_candidates


def _build_fact_counts(extracted: dict[str, Any], skill_evidence_count: int = 0) -> dict[str, int]:
    return {
        'skill_mentions': len(extracted.get('skills') or []),
        'skill_evidence_rows': skill_evidence_count,
        'pending_skill_candidates': 0,
        'role_history_count': len(extracted.get('role_history') or []),
        'achievement_count': len(extracted.get('achievements') or []),
        'domain_count': len(extracted.get('domain_experience') or []),
        'leadership_signal_count': len(extracted.get('leadership_signals') or []),
    }


def _merge_cv_source_metadata(
    source: WorkspaceSource,
    *,
    profile: EmployeeCVProfile,
    warnings: list[str],
    match_details: dict[str, Any],
    vector_index: dict[str, Any],
    fact_counts: dict[str, int],
    pending_skill_candidates: list[dict[str, Any]],
) -> None:
    parse_metadata = dict(source.parse_metadata or {})
    parse_metadata['cv_evidence'] = {
        'schema_version': CV_PROFILE_SCHEMA_VERSION,
        'status': profile.status,
        'evidence_quality': profile.evidence_quality,
        'matched_employee_uuid': str(profile.employee.uuid) if profile.employee_id else '',
        'matched_employee_name': profile.employee.full_name if profile.employee_id else '',
        'matched_by': profile.matched_by,
        'match_confidence': float(profile.match_confidence or 0.0),
        'warning_count': len(warnings),
        'warnings': warnings,
        'fact_counts': fact_counts,
        'pending_skill_candidates': pending_skill_candidates,
        'candidate_matches': match_details.get('candidate_matches', []),
        'vector_index': vector_index,
    }
    source.parse_metadata = _json_safe(parse_metadata)
    source.save(update_fields=['parse_metadata', 'updated_at'])


def _persist_cv_payload_sync(source_pk, extracted: dict) -> dict:
    source = WorkspaceSource.objects.select_related('workspace', 'parsed_source', 'cv_profile').get(pk=source_pk)
    workspace = source.workspace
    input_revision = _build_cv_input_revision(source)
    normalized_extracted = _normalize_cv_payload(extracted, workspace=workspace)
    match_details = _match_employee_for_cv_sync(workspace, normalized_extracted, source)
    normalized_extracted = _json_safe(normalized_extracted)
    match_details = _json_safe(match_details)
    evidence_quality = _determine_evidence_quality(normalized_extracted)
    warnings = dedupe_strings([
        *(normalized_extracted.get('warnings') or []),
        normalized_extracted.get('sparse_reason', '') if normalized_extracted.get('sparse_cv') else '',
        match_details.get('message', ''),
    ])

    employee = None
    if match_details.get('status') == EmployeeCVProfile.Status.MATCHED and match_details.get('employee_uuid'):
        employee = Employee.objects.filter(workspace=workspace, uuid=match_details['employee_uuid']).first()

    with transaction.atomic():
        profile, _created = EmployeeCVProfile.objects.select_for_update().get_or_create(
            source=source,
            defaults={'workspace': workspace},
        )
        previous_input_revision = str(profile.input_revision or '')
        previous_active_generation_id = str(profile.active_vector_generation_id or '')
        profile.workspace = workspace
        profile.employee = employee
        profile.status = match_details['status']
        profile.evidence_quality = evidence_quality
        profile.match_confidence = round(float(match_details.get('match_confidence') or 0.0), 2)
        profile.matched_by = match_details.get('matched_by', '')
        profile.language_code = source.language_code or ''
        profile.input_revision = input_revision
        profile.headline = normalized_extracted.get('headline', '')
        profile.current_role = normalized_extracted.get('current_role', '')
        profile.seniority = normalized_extracted.get('seniority', '')
        profile.role_family = normalized_extracted.get('role_family', '')
        profile.extracted_payload = normalized_extracted
        profile.metadata = {
            'schema_version': CV_PROFILE_SCHEMA_VERSION,
            'candidate_matches': match_details.get('candidate_matches', []),
            'warnings': warnings,
            'summary': normalized_extracted.get('summary', ''),
            'languages': normalized_extracted.get('languages', []),
            'sparse_cv': normalized_extracted.get('sparse_cv', False),
            'sparse_reason': normalized_extracted.get('sparse_reason', ''),
        }
        profile.extracted_payload = _json_safe(profile.extracted_payload)
        profile.metadata = _json_safe(profile.metadata)
        profile.save()
        _sync_profile_candidate_matches_sync(profile, match_details.get('candidate_matches', []))

        EmployeeSkillEvidence.objects.filter(
            workspace=workspace,
            source=source,
            source_kind=_CV_EVIDENCE_SOURCE_KIND,
        ).delete()

        if employee is not None:
            skill_evidence_count, pending_skill_candidates = _persist_skill_evidence_rows(profile, normalized_extracted)
        else:
            skill_evidence_count = 0
            pending_skill_candidates = []
        pending_skill_candidates = _json_safe(pending_skill_candidates)

    if employee is not None:
        try:
            vector_index = index_employee_cv_profile_sync(profile.pk)
        except Exception as exc:
            logger.warning('CV evidence indexing failed for source %s: %s', source.uuid, exc, exc_info=True)
            vector_index = {
                'status': 'failed',
                'reason': 'indexing_exception',
                'message': str(exc),
                'active_generation_id': profile.active_vector_generation_id,
            }

        if previous_input_revision and previous_input_revision != input_revision and vector_index.get('status') != 'indexed':
            clear_employee_cv_evidence_index_sync(
                workspace_uuid=str(workspace.uuid),
                source_uuid=str(source.uuid),
                generation_id=previous_active_generation_id or None,
            )
            vector_index = {
                **vector_index,
                'active_generation_id': '',
                'stale_generation_cleared': bool(previous_active_generation_id),
            }
    else:
        clear_employee_cv_evidence_index_sync(
            workspace_uuid=str(workspace.uuid),
            source_uuid=str(source.uuid),
        )
        if profile.active_vector_generation_id:
            profile.active_vector_generation_id = ''
        vector_index = {
            'status': 'skipped',
            'reason': 'no_safe_employee_match',
            'active_generation_id': '',
        }
    vector_index = _json_safe(vector_index)

    fact_counts = _build_fact_counts(normalized_extracted, skill_evidence_count=skill_evidence_count)
    fact_counts['pending_skill_candidates'] = len(pending_skill_candidates)
    profile.metadata = {
        **dict(profile.metadata or {}),
        'fact_counts': fact_counts,
        'pending_skill_candidates': pending_skill_candidates,
        'vector_index': vector_index,
        'raw_role_family': normalized_extracted.get('raw_role_family', ''),
    }
    profile.metadata = _json_safe(profile.metadata)
    profile.active_vector_generation_id = str(vector_index.get('active_generation_id') or profile.active_vector_generation_id or '')
    profile.save(update_fields=['metadata', 'active_vector_generation_id', 'updated_at'])
    _merge_cv_source_metadata(
        source,
        profile=profile,
        warnings=warnings,
        match_details=match_details,
        vector_index=vector_index,
        fact_counts=fact_counts,
        pending_skill_candidates=pending_skill_candidates,
    )
    return _build_cv_profile_result_sync(source.pk)


def _record_cv_extraction_failure_sync(source_pk, error_message: str) -> dict:
    source = WorkspaceSource.objects.select_related('workspace').get(pk=source_pk)
    workspace = source.workspace
    input_revision = _build_cv_input_revision(source)
    with transaction.atomic():
        profile, _created = EmployeeCVProfile.objects.select_for_update().get_or_create(
            source=source,
            defaults={'workspace': workspace},
        )
        profile.workspace = workspace
        profile.employee = None
        profile.status = EmployeeCVProfile.Status.EXTRACTION_FAILED
        profile.evidence_quality = EmployeeCVProfile.EvidenceQuality.FAILED
        profile.match_confidence = 0
        profile.matched_by = ''
        profile.language_code = source.language_code or ''
        profile.input_revision = input_revision
        profile.active_vector_generation_id = ''
        profile.headline = ''
        profile.current_role = ''
        profile.seniority = ''
        profile.role_family = ''
        profile.extracted_payload = {}
        profile.metadata = {
            'schema_version': CV_PROFILE_SCHEMA_VERSION,
            'warnings': [error_message],
            'fact_counts': _build_fact_counts({}, skill_evidence_count=0),
            'vector_index': {
                'status': 'skipped',
                'reason': 'extraction_failed',
                'active_generation_id': '',
            },
        }
        profile.extracted_payload = _json_safe(profile.extracted_payload)
        profile.metadata = _json_safe(profile.metadata)
        profile.save()
        EmployeeCVMatchCandidate.objects.filter(profile=profile).delete()
        EmployeeSkillEvidence.objects.filter(
            workspace=workspace,
            source=source,
            source_kind=_CV_EVIDENCE_SOURCE_KIND,
        ).delete()

    clear_employee_cv_evidence_index_sync(
        workspace_uuid=str(workspace.uuid),
        source_uuid=str(source.uuid),
    )

    parse_metadata = dict(source.parse_metadata or {})
    parse_metadata['cv_evidence'] = {
        'schema_version': CV_PROFILE_SCHEMA_VERSION,
        'status': EmployeeCVProfile.Status.EXTRACTION_FAILED,
        'evidence_quality': EmployeeCVProfile.EvidenceQuality.FAILED,
        'warnings': [error_message],
        'warning_count': 1,
        'fact_counts': _build_fact_counts({}, skill_evidence_count=0),
        'vector_index': {
            'status': 'skipped',
            'reason': 'extraction_failed',
            'active_generation_id': '',
        },
    }
    source.parse_metadata = _json_safe(parse_metadata)
    source.save(update_fields=['parse_metadata', 'updated_at'])
    return _build_cv_profile_result_sync(source.pk)


def _build_cv_profile_result_sync(source_pk, reused: bool = False) -> dict:
    source = WorkspaceSource.objects.select_related(
        'workspace',
        'cv_profile',
        'cv_profile__employee',
    ).get(pk=source_pk)
    profile = getattr(source, 'cv_profile', None)
    profile_metadata = dict((profile.metadata or {}) if profile is not None else {})
    vector_index = profile_metadata.get('vector_index') or {}
    warnings = profile_metadata.get('warnings') or []
    fact_counts = profile_metadata.get('fact_counts') or {}
    return {
        'source_uuid': str(source.uuid),
        'source_title': source.title or source.source_kind,
        'status': profile.status if profile is not None else EmployeeCVProfile.Status.UNMATCHED,
        'evidence_quality': profile.evidence_quality if profile is not None else EmployeeCVProfile.EvidenceQuality.EMPTY,
        'employee_uuid': str(profile.employee.uuid) if profile is not None and profile.employee_id else None,
        'full_name': profile.employee.full_name if profile is not None and profile.employee_id else '',
        'current_title': profile.employee.current_title if profile is not None and profile.employee_id else '',
        'matched_by': profile.matched_by if profile is not None else '',
        'match_confidence': float(profile.match_confidence or 0.0) if profile is not None else 0.0,
        'skill_evidence_count': int(fact_counts.get('skill_evidence_rows') or 0),
        'warnings': warnings,
        'vector_index_status': vector_index.get('status', ''),
        'reused': reused,
    }


def _employee_cv_availability_payload(employee: Employee) -> dict[str, Any]:
    raw_payload = dict((employee.metadata or {}).get('cv_availability') or {})
    status = str(raw_payload.get('status') or '').strip()
    if status != 'no_cv_available':
        return {
            'status': '',
            'note': '',
            'confirmed_by': '',
            'confirmed_at': '',
        }
    return {
        'status': status,
        'note': str(raw_payload.get('note') or '').strip(),
        'confirmed_by': str(raw_payload.get('confirmed_by') or '').strip(),
        'confirmed_at': str(raw_payload.get('confirmed_at') or '').strip(),
    }


def _employee_is_confirmed_without_cv(employee: Employee) -> bool:
    return _employee_cv_availability_payload(employee).get('status') == 'no_cv_available'


def _pending_skill_candidate_key(candidate: dict[str, Any]) -> str:
    candidates = [
        str(candidate.get('candidate_key') or '').strip(),
        str(candidate.get('proposed_key') or '').strip(),
        str(candidate.get('display_name_en') or '').strip(),
        *(candidate.get('original_terms') or []),
        *(candidate.get('aliases') or []),
    ]
    for value in candidates:
        normalized = normalize_lookup_key(value)
        if normalized:
            return normalized
    return ''


def _refresh_profile_evidence_state_sync(
    profile: EmployeeCVProfile,
    *,
    warnings: list[str] | None = None,
) -> tuple[int, list[dict[str, Any]], dict[str, int], dict[str, Any]]:
    workspace = profile.workspace
    source = profile.source
    extracted = _normalize_cv_payload(dict(profile.extracted_payload or {}), workspace=workspace)
    warnings = list(warnings if warnings is not None else ((profile.metadata or {}).get('warnings') or []))

    EmployeeSkillEvidence.objects.filter(
        workspace=workspace,
        source=source,
        source_kind=_CV_EVIDENCE_SOURCE_KIND,
    ).delete()

    if profile.employee_id:
        skill_evidence_count, pending_skill_candidates = _persist_skill_evidence_rows(profile, extracted)
        try:
            vector_index = index_employee_cv_profile_sync(profile.pk)
        except Exception as exc:
            logger.warning(
                'CV evidence indexing failed while refreshing profile %s: %s',
                source.uuid,
                exc,
                exc_info=True,
            )
            vector_index = {
                'status': 'failed',
                'reason': 'indexing_exception',
                'message': str(exc),
                'active_generation_id': profile.active_vector_generation_id,
            }
    else:
        skill_evidence_count = 0
        pending_skill_candidates = list((profile.metadata or {}).get('pending_skill_candidates') or [])
        clear_employee_cv_evidence_index_sync(
            workspace_uuid=str(workspace.uuid),
            source_uuid=str(source.uuid),
        )
        vector_index = {
            'status': 'skipped',
            'reason': 'no_employee_match',
            'active_generation_id': '',
        }

    pending_skill_candidates = _json_safe(pending_skill_candidates)
    vector_index = _json_safe(vector_index)
    fact_counts = _build_fact_counts(extracted, skill_evidence_count=skill_evidence_count)
    fact_counts['pending_skill_candidates'] = len(pending_skill_candidates)

    profile.active_vector_generation_id = str(vector_index.get('active_generation_id') or '')
    profile.metadata = {
        **dict(profile.metadata or {}),
        'fact_counts': fact_counts,
        'pending_skill_candidates': pending_skill_candidates,
        'vector_index': vector_index,
        'warnings': warnings,
    }
    profile.metadata = _json_safe(profile.metadata)
    profile.save(update_fields=['active_vector_generation_id', 'metadata', 'updated_at'])
    _merge_cv_source_metadata(
        source,
        profile=profile,
        warnings=warnings,
        match_details={'candidate_matches': _serialize_profile_candidate_matches(profile)},
        vector_index=vector_index,
        fact_counts=fact_counts,
        pending_skill_candidates=pending_skill_candidates,
    )
    return skill_evidence_count, pending_skill_candidates, fact_counts, vector_index


def _get_cv_evidence_status_sync(workspace_pk) -> dict:
    workspace = IntakeWorkspace.objects.get(pk=workspace_pk)
    cv_sources = list(
        WorkspaceSource.objects.select_related('cv_profile', 'cv_profile__employee', 'parsed_source').filter(
            workspace=workspace,
            source_kind=WorkspaceSourceKind.EMPLOYEE_CV,
        )
    )
    profiles = [source.cv_profile for source in cv_sources if getattr(source, 'cv_profile', None) is not None]
    status_counts = {
        EmployeeCVProfile.Status.MATCHED: 0,
        EmployeeCVProfile.Status.AMBIGUOUS: 0,
        EmployeeCVProfile.Status.UNMATCHED: 0,
        EmployeeCVProfile.Status.LOW_CONFIDENCE_MATCH: 0,
        EmployeeCVProfile.Status.EXTRACTION_FAILED: 0,
    }
    quality_counts = {
        EmployeeCVProfile.EvidenceQuality.STRONG: 0,
        EmployeeCVProfile.EvidenceQuality.USABLE: 0,
        EmployeeCVProfile.EvidenceQuality.SPARSE: 0,
        EmployeeCVProfile.EvidenceQuality.EMPTY: 0,
        EmployeeCVProfile.EvidenceQuality.FAILED: 0,
    }
    vector_indexed_source_count = 0
    for profile in profiles:
        status_counts[profile.status] = status_counts.get(profile.status, 0) + 1
        quality_counts[profile.evidence_quality] = quality_counts.get(profile.evidence_quality, 0) + 1
        if profile.active_vector_generation_id:
            vector_indexed_source_count += 1

    employees_with_cv_evidence_count = Employee.objects.filter(
        workspace=workspace,
        skill_evidence__source_kind=_CV_EVIDENCE_SOURCE_KIND,
    ).distinct().count()
    total_employees = Employee.objects.filter(workspace=workspace).count()
    low_confidence_evidence_count = EmployeeSkillEvidence.objects.filter(
        workspace=workspace,
        source_kind=_CV_EVIDENCE_SOURCE_KIND,
        confidence__lt=0.55,
    ).count()
    pending_source_count = len([source for source in cv_sources if getattr(source, 'cv_profile', None) is None])
    parse_failed_count = WorkspaceSource.objects.filter(
        workspace=workspace,
        source_kind=WorkspaceSourceKind.EMPLOYEE_CV,
        status=WorkspaceSourceStatus.FAILED,
    ).count()
    unresolved_source_count = len([profile for profile in profiles if _review_reasons_for_profile(profile)])
    employees_without_cv_evidence_count = len(_list_employees_without_cv_evidence_sync(workspace_pk))
    return {
        'workspace_slug': workspace.slug,
        'total_cv_sources': len(cv_sources),
        'parsed_cv_sources': len([source for source in cv_sources if getattr(source, 'parsed_source', None) is not None]),
        'pending_source_count': pending_source_count,
        'parse_failed_count': parse_failed_count,
        'processed_profile_count': len(profiles),
        'matched_count': status_counts[EmployeeCVProfile.Status.MATCHED],
        'ambiguous_count': status_counts[EmployeeCVProfile.Status.AMBIGUOUS],
        'unmatched_count': status_counts[EmployeeCVProfile.Status.UNMATCHED],
        'low_confidence_count': status_counts[EmployeeCVProfile.Status.LOW_CONFIDENCE_MATCH],
        'extraction_failed_count': status_counts[EmployeeCVProfile.Status.EXTRACTION_FAILED],
        'strong_profile_count': quality_counts[EmployeeCVProfile.EvidenceQuality.STRONG],
        'usable_profile_count': quality_counts[EmployeeCVProfile.EvidenceQuality.USABLE],
        'sparse_profile_count': quality_counts[EmployeeCVProfile.EvidenceQuality.SPARSE],
        'empty_profile_count': quality_counts[EmployeeCVProfile.EvidenceQuality.EMPTY],
        'employees_with_cv_evidence_count': employees_with_cv_evidence_count,
        'employees_without_cv_evidence_count': min(
            max(0, total_employees - employees_with_cv_evidence_count),
            employees_without_cv_evidence_count,
        ),
        'skill_evidence_count': EmployeeSkillEvidence.objects.filter(
            workspace=workspace,
            source_kind=_CV_EVIDENCE_SOURCE_KIND,
        ).count(),
        'low_confidence_evidence_count': low_confidence_evidence_count,
        'unresolved_source_count': unresolved_source_count,
        'vector_indexed_source_count': vector_indexed_source_count,
    }


def _list_unmatched_cv_profiles_sync(workspace_pk) -> list[dict]:
    profiles = list(
        EmployeeCVProfile.objects.select_related('source', 'employee', 'workspace')
        .prefetch_related('candidate_matches__employee')
        .filter(workspace_id=workspace_pk)
        .filter(
            Q(
                status__in=[
                    EmployeeCVProfile.Status.AMBIGUOUS,
                    EmployeeCVProfile.Status.UNMATCHED,
                    EmployeeCVProfile.Status.LOW_CONFIDENCE_MATCH,
                    EmployeeCVProfile.Status.EXTRACTION_FAILED,
                ]
            )
        )
        .order_by('-updated_at')
    )
    return [_build_cv_profile_detail_item(profile) for profile in profiles]


def _list_cv_review_items_sync(workspace_pk) -> list[dict]:
    profiles = list(
        EmployeeCVProfile.objects.select_related('source', 'employee', 'workspace')
        .prefetch_related('candidate_matches__employee')
        .filter(workspace_id=workspace_pk)
        .order_by('-updated_at')
    )
    return [
        _build_cv_profile_detail_item(profile)
        for profile in profiles
        if _review_reasons_for_profile(profile)
    ]


def _list_employees_without_cv_evidence_sync(workspace_pk) -> list[dict]:
    employees = list(Employee.objects.filter(workspace_id=workspace_pk).order_by('full_name'))
    candidate_profiles_by_employee = _candidate_profiles_by_employee_sync(workspace_pk)

    rows: list[dict] = []
    for employee in employees:
        if _employee_is_confirmed_without_cv(employee):
            continue
        row = _build_employee_without_cv_evidence_row_sync(
            workspace_pk,
            employee,
            candidate_profiles_by_employee=candidate_profiles_by_employee,
        )
        if row is not None:
            rows.append(row)
    return rows


def _candidate_profiles_by_employee_sync(workspace_pk) -> dict[str, list[EmployeeCVProfile]]:
    candidate_profiles_by_employee: dict[str, list[EmployeeCVProfile]] = {}
    unresolved_candidates = list(
        EmployeeCVMatchCandidate.objects.select_related('profile', 'profile__source').filter(
            workspace_id=workspace_pk,
            profile__employee__isnull=True,
            profile__status__in=[
                EmployeeCVProfile.Status.AMBIGUOUS,
                EmployeeCVProfile.Status.LOW_CONFIDENCE_MATCH,
            ],
        ).order_by('employee_id', 'rank')
    )
    for candidate in unresolved_candidates:
        candidate_profiles_by_employee.setdefault(str(candidate.employee.uuid), []).append(candidate.profile)

    # Backward-compatible fallback for unresolved profiles that predate the
    # normalized candidate table.
    if not candidate_profiles_by_employee:
        unresolved_profiles = list(
            EmployeeCVProfile.objects.select_related('source').filter(
                workspace_id=workspace_pk,
                employee__isnull=True,
                status__in=[
                    EmployeeCVProfile.Status.AMBIGUOUS,
                    EmployeeCVProfile.Status.LOW_CONFIDENCE_MATCH,
                ],
            )
        )
        for profile in unresolved_profiles:
            for candidate in (profile.metadata or {}).get('candidate_matches', []):
                employee_uuid = str(candidate.get('employee_uuid') or '').strip()
                if not employee_uuid:
                    continue
                candidate_profiles_by_employee.setdefault(employee_uuid, []).append(profile)
    return candidate_profiles_by_employee


def _build_employee_without_cv_evidence_row_sync(
    workspace_pk,
    employee: Employee,
    *,
    candidate_profiles_by_employee: dict[str, list[EmployeeCVProfile]] | None = None,
) -> dict[str, Any] | None:
    if _employee_is_confirmed_without_cv(employee):
        return None

    profiles = list(
        EmployeeCVProfile.objects.filter(workspace_id=workspace_pk, employee=employee).order_by('-updated_at')
    )
    evidence_row_count = EmployeeSkillEvidence.objects.filter(
        workspace_id=workspace_pk,
        employee=employee,
        source_kind=_CV_EVIDENCE_SOURCE_KIND,
    ).count()
    if evidence_row_count > 0:
        return None

    candidate_profiles_lookup = candidate_profiles_by_employee or _candidate_profiles_by_employee_sync(workspace_pk)
    if not profiles:
        candidate_profiles = candidate_profiles_lookup.get(str(employee.uuid), [])
        if candidate_profiles:
            review_reasons = ['candidate_cv_pending_review']
            warnings = dedupe_strings([
                warning
                for profile in candidate_profiles
                for warning in ((profile.metadata or {}).get('warnings') or [])
            ])
            latest_status = candidate_profiles[0].status
            related_source_uuids = [str(profile.source.uuid) for profile in candidate_profiles]
        else:
            review_reasons = ['no_matched_cv_profile']
            warnings = ['No matched CV profile exists for this employee yet.']
            latest_status = ''
            related_source_uuids = []
    else:
        latest_profile = profiles[0]
        review_reasons = _review_reasons_for_profile(latest_profile) or ['no_cv_evidence']
        warnings = list((latest_profile.metadata or {}).get('warnings') or [])
        latest_status = latest_profile.status
        related_source_uuids = [str(profile.source.uuid) for profile in profiles]

    return {
        'employee_uuid': str(employee.uuid),
        'full_name': employee.full_name,
        'current_title': employee.current_title,
        'review_reason': review_reasons[0],
        'review_reasons': review_reasons,
        'related_source_uuids': related_source_uuids,
        'cv_profile_count': len(profiles),
        'cv_evidence_row_count': evidence_row_count,
        'latest_profile_status': latest_status,
        'warnings': warnings,
    }


def _get_employee_cv_evidence_detail_sync(workspace_pk, employee_uuid) -> dict | None:
    employee = Employee.objects.filter(workspace_id=workspace_pk, uuid=employee_uuid).first()
    if employee is None:
        return None

    profiles = list(
        EmployeeCVProfile.objects.select_related('source').prefetch_related('candidate_matches__employee')
        .filter(workspace_id=workspace_pk, employee=employee)
        .order_by('-updated_at')
    )
    candidate_profiles = _candidate_profiles_for_employee_sync(workspace_pk, employee.uuid)
    evidence_rows = list(
        EmployeeSkillEvidence.objects.select_related('skill', 'source')
        .filter(
            workspace_id=workspace_pk,
            employee=employee,
            source_kind=_CV_EVIDENCE_SOURCE_KIND,
        )
        .order_by('-confidence', 'skill__display_name_en')
    )
    coverage_gap = _build_employee_without_cv_evidence_row_sync(workspace_pk, employee)
    return {
        'employee_uuid': str(employee.uuid),
        'full_name': employee.full_name,
        'current_title': employee.current_title,
        'external_employee_id': employee.external_employee_id,
        'metadata': employee.metadata or {},
        'cv_availability': _employee_cv_availability_payload(employee),
        'coverage_gap': coverage_gap,
        'cv_profiles': [_build_cv_profile_detail_item(profile) for profile in profiles],
        'candidate_cv_profiles': [_build_cv_profile_detail_item(profile) for profile in candidate_profiles],
        'evidence_rows': [
            {
                'skill_uuid': str(row.skill.uuid),
                'skill_key': row.skill.canonical_key,
                'skill_name_en': row.skill.display_name_en,
                'skill_name_ru': row.skill.display_name_ru,
                'resolution_status': row.skill.resolution_status,
                'current_level': float(row.current_level),
                'confidence': float(row.confidence),
                'weight': float(row.weight),
                'evidence_text': row.evidence_text,
                'source_uuid': str(row.source.uuid) if row.source_id else None,
                'is_operator_confirmed': bool(row.is_operator_confirmed),
                'operator_action': row.operator_action,
                'operator_note': row.operator_note,
                'metadata': row.metadata or {},
            }
            for row in evidence_rows
        ],
    }


def _resolve_cv_profile_match_sync(
    workspace_pk,
    source_uuid: str,
    employee_uuid: str,
    operator_name: str,
    resolution_note: str,
) -> dict | None:
    source = WorkspaceSource.objects.select_related(
        'workspace',
        'parsed_source',
        'cv_profile',
        'cv_profile__employee',
    ).filter(
        workspace_id=workspace_pk,
        uuid=source_uuid,
        source_kind=WorkspaceSourceKind.EMPLOYEE_CV,
    ).first()
    if source is None:
        return None

    workspace = source.workspace
    profile = getattr(source, 'cv_profile', None)
    if profile is None:
        return None

    resolved_employee = None
    if employee_uuid:
        resolved_employee = Employee.objects.filter(workspace=workspace, uuid=employee_uuid).first()
        if resolved_employee is None:
            return None

    candidate_matches = _serialize_profile_candidate_matches(profile)
    warnings = list((profile.metadata or {}).get('warnings') or [])

    with transaction.atomic():
        profile = EmployeeCVProfile.objects.select_for_update().get(pk=profile.pk)
        profile.employee = resolved_employee
        profile.status = (
            EmployeeCVProfile.Status.MATCHED
            if resolved_employee is not None
            else EmployeeCVProfile.Status.UNMATCHED
        )
        profile.match_confidence = 1.0 if resolved_employee is not None else 0.0
        profile.matched_by = 'operator_override' if resolved_employee is not None else 'operator_unassigned'

        profile.metadata = {
            **dict(profile.metadata or {}),
            'resolution': {
                'resolved_by': operator_name,
                'resolution_note': resolution_note,
                'resolved_employee_uuid': str(resolved_employee.uuid) if resolved_employee is not None else '',
                'resolved_status': profile.status,
                'candidate_matches_snapshot': candidate_matches,
            },
        }
        profile.metadata = _json_safe(profile.metadata)
        profile.save(update_fields=['employee', 'status', 'match_confidence', 'matched_by', 'metadata', 'updated_at'])
    profile.refresh_from_db()
    _refresh_profile_evidence_state_sync(profile, warnings=warnings)
    return _build_cv_profile_detail_item(profile)


def _mark_employee_no_cv_available_sync(
    workspace_pk,
    employee_uuid: str,
    operator_name: str,
    note: str,
) -> dict | None:
    employee = Employee.objects.filter(workspace_id=workspace_pk, uuid=employee_uuid).first()
    if employee is None:
        return None

    matched_profile_count = EmployeeCVProfile.objects.filter(workspace_id=workspace_pk, employee=employee).count()
    evidence_count = EmployeeSkillEvidence.objects.filter(
        workspace_id=workspace_pk,
        employee=employee,
        source_kind=_CV_EVIDENCE_SOURCE_KIND,
    ).count()
    if matched_profile_count > 0 or evidence_count > 0:
        raise ValueError('This employee already has a matched CV or CV-derived evidence. Clear those links before marking no CV available.')

    metadata = dict(employee.metadata or {})
    metadata['cv_availability'] = {
        'status': 'no_cv_available',
        'note': str(note or '').strip(),
        'confirmed_by': str(operator_name or '').strip(),
        'confirmed_at': datetime.utcnow().isoformat(),
    }
    employee.metadata = _json_safe(metadata)
    employee.save(update_fields=['metadata', 'updated_at'])
    return _employee_cv_availability_payload(employee)


def _clear_employee_no_cv_available_sync(workspace_pk, employee_uuid: str) -> dict | None:
    employee = Employee.objects.filter(workspace_id=workspace_pk, uuid=employee_uuid).first()
    if employee is None:
        return None

    metadata = dict(employee.metadata or {})
    if 'cv_availability' in metadata:
        metadata.pop('cv_availability', None)
        employee.metadata = _json_safe(metadata)
        employee.save(update_fields=['metadata', 'updated_at'])
    return _employee_cv_availability_payload(employee)


def _approve_pending_skill_candidate_sync(
    workspace_pk,
    source_uuid: str,
    candidate_key: str,
    approved_name_en: str,
    approved_name_ru: str,
    alias_terms: list[str],
    operator_name: str,
    approval_note: str,
) -> dict | None:
    source = WorkspaceSource.objects.select_related(
        'workspace',
        'cv_profile',
        'cv_profile__employee',
    ).filter(
        workspace_id=workspace_pk,
        uuid=source_uuid,
        source_kind=WorkspaceSourceKind.EMPLOYEE_CV,
    ).first()
    if source is None:
        return None

    profile = getattr(source, 'cv_profile', None)
    if profile is None:
        return None

    normalized_candidate_key = normalize_lookup_key(candidate_key)
    pending_candidates = list((profile.metadata or {}).get('pending_skill_candidates') or [])
    candidate = next(
        (
            item
            for item in pending_candidates
            if _pending_skill_candidate_key(item) == normalized_candidate_key
        ),
        None,
    )
    if candidate is None:
        raise ValueError('Pending skill candidate not found for this CV profile.')

    approved_display_name_en = str(approved_name_en or candidate.get('display_name_en') or '').strip()
    if not approved_display_name_en:
        approved_display_name_en = dedupe_strings(candidate.get('original_terms') or [''])[0] if (candidate.get('original_terms') or []) else ''
    if not approved_display_name_en:
        raise ValueError('Approved skill name is required.')
    approved_display_name_ru = str(approved_name_ru or candidate.get('display_name_ru') or '').strip()

    workspace = source.workspace
    normalized_skill = normalize_skill_seed(
        approved_display_name_en,
        workspace=workspace,
        review_metadata={
            'approved_via_pending_skill': True,
            'source_uuid': str(source.uuid),
        },
        allow_freeform=True,
    )
    terms = dedupe_strings([
        approved_display_name_en,
        approved_display_name_ru,
        *(candidate.get('original_terms') or []),
        *(candidate.get('aliases') or []),
        *alias_terms,
    ])
    approved_esco_skill = _resolve_esco_skill_from_normalized(normalized_skill)
    override_metadata = {
        'approved_via': 'pending_skill_review',
        'source_uuid': str(source.uuid),
        'cv_profile_uuid': str(profile.uuid),
        'operator_name': str(operator_name or '').strip(),
        'approval_note': str(approval_note or '').strip(),
        'candidate_snapshot': _json_safe(candidate),
    }

    with transaction.atomic():
        profile = EmployeeCVProfile.objects.select_related('workspace', 'source', 'employee').get(pk=profile.pk)
        pending_candidates = list((profile.metadata or {}).get('pending_skill_candidates') or [])
        candidate = next(
            (
                item
                for item in pending_candidates
                if _pending_skill_candidate_key(item) == normalized_candidate_key
            ),
            None,
        )
        if candidate is None:
            raise ValueError('Pending skill candidate no longer exists on this profile.')

        provisional_keys = dedupe_strings(
            [
                str(candidate.get('proposed_key') or '').strip(),
                normalized_candidate_key,
            ]
        )
        provisional_skill = Skill.objects.filter(
            workspace=workspace,
            canonical_key__in=provisional_keys,
            resolution_status=Skill.ResolutionStatus.PENDING_REVIEW,
        ).order_by('-updated_at', '-created_at').first()

        target_canonical_key = str(normalized_skill.get('canonical_key') or '').strip()
        target_skill = (
            Skill.objects.filter(workspace=workspace, canonical_key=target_canonical_key).first()
            if target_canonical_key else None
        )

        if target_skill is None and provisional_skill is not None:
            update_fields: list[str] = []
            if target_canonical_key and provisional_skill.canonical_key != target_canonical_key:
                provisional_skill.canonical_key = target_canonical_key
                update_fields.append('canonical_key')
            if provisional_skill.display_name_en != approved_display_name_en:
                provisional_skill.display_name_en = approved_display_name_en
                update_fields.append('display_name_en')
            if provisional_skill.display_name_ru != approved_display_name_ru:
                provisional_skill.display_name_ru = approved_display_name_ru
                update_fields.append('display_name_ru')
            if approved_esco_skill is not None and provisional_skill.esco_skill_id != approved_esco_skill.pk:
                provisional_skill.esco_skill = approved_esco_skill
                update_fields.append('esco_skill')
            merged_metadata = dict(provisional_skill.metadata or {})
            match_source = str(normalized_skill.get('match_source') or '').strip()
            if match_source:
                merged_metadata['catalog_match_source'] = match_source
            if approved_esco_skill is not None:
                merged_metadata.update(
                    {
                        'esco_skill_uri': approved_esco_skill.concept_uri,
                        'esco_skill_match_source': match_source or 'override',
                    }
                )
            if merged_metadata != (provisional_skill.metadata or {}):
                provisional_skill.metadata = merged_metadata
                update_fields.append('metadata')
            merged_terms = dedupe_strings([*(provisional_skill.source_terms or []), *terms])
            if merged_terms != list(provisional_skill.source_terms or []):
                provisional_skill.source_terms = merged_terms
                update_fields.append('source_terms')
            if provisional_skill.resolution_status != Skill.ResolutionStatus.RESOLVED:
                provisional_skill.resolution_status = Skill.ResolutionStatus.RESOLVED
                update_fields.append('resolution_status')
            if not provisional_skill.is_operator_confirmed:
                provisional_skill.is_operator_confirmed = True
                update_fields.append('is_operator_confirmed')
            if update_fields:
                provisional_skill.save(update_fields=[*update_fields, 'updated_at'])
            merge_skill_aliases_sync(provisional_skill, terms)
            target_skill = provisional_skill

        if target_skill is None:
            target_skill = ensure_workspace_skill_sync(
                workspace,
                normalized_skill=normalized_skill,
                preferred_display_name_ru=approved_display_name_ru,
                aliases=terms,
                raw_term=approved_display_name_en,
                created_source='pending_skill_review',
                promote_aliases=True,
                resolution_status=Skill.ResolutionStatus.RESOLVED,
            )
        _set_skill_state(
            target_skill,
            resolution_status=Skill.ResolutionStatus.RESOLVED,
            is_operator_confirmed=True,
            display_name_en=approved_display_name_en,
            display_name_ru=approved_display_name_ru,
            esco_skill=approved_esco_skill,
            source_terms=terms,
        )
        merge_skill_aliases_sync(target_skill, terms)

        _upsert_skill_resolution_overrides(
            workspace,
            terms=terms,
            target_skill=target_skill,
            status=CatalogOverrideStatus.APPROVED,
            source='pending_skill_review',
            note=approval_note,
            metadata=override_metadata,
        )
        _mark_catalog_review_items_resolved(
            workspace,
            terms=terms,
            resolved_via='pending_skill_review',
            operator_name=operator_name,
            override_canonical_key=target_skill.canonical_key,
        )

        if profile.employee_id:
            SkillReviewDecision.objects.update_or_create(
                workspace=workspace,
                employee=profile.employee,
                skill_canonical_key=target_skill.canonical_key,
                defaults={
                    'action': EmployeeSkillEvidence.OperatorAction.ACCEPTED,
                    'merge_target_skill_uuid': None,
                    'note': str(approval_note or '').strip(),
                    'reviewed_by': str(operator_name or '').strip(),
                },
            )

        remaining_pending_candidates = [
            item
            for item in pending_candidates
            if _pending_skill_candidate_key(item) != normalized_candidate_key
        ]
        profile.metadata = {
            **dict(profile.metadata or {}),
            'pending_skill_candidates': _json_safe(remaining_pending_candidates),
        }
        profile.metadata = _json_safe(profile.metadata)
        profile.save(update_fields=['metadata', 'updated_at'])
    profile.refresh_from_db()
    _refresh_profile_evidence_state_sync(profile)
    if provisional_skill is not None and target_skill.pk != provisional_skill.pk:
        provisional_skill.refresh_from_db()
        _maybe_mark_skill_rejected_if_unused(provisional_skill)

    return _build_cv_profile_detail_item(profile)


def _normalize_review_action(action: str) -> str:
    normalized = str(action or '').strip().lower()
    if normalized == 'accept':
        return EmployeeSkillEvidence.OperatorAction.ACCEPTED
    if normalized == 'reject':
        return EmployeeSkillEvidence.OperatorAction.REJECTED
    if normalized == 'merge':
        return EmployeeSkillEvidence.OperatorAction.MERGED
    return ''


def _rebuild_weight_for_evidence_row(evidence_row: EmployeeSkillEvidence) -> float:
    metadata = dict(evidence_row.metadata or {})
    return _weight_for_skill_category(str(metadata.get('evidence_category') or ''))


def _resolved_skill_candidate_payload(skill: Skill, *, similarity_score: float) -> dict[str, Any]:
    return {
        'skill_uuid': str(skill.uuid),
        'display_name_en': skill.display_name_en,
        'display_name_ru': skill.display_name_ru,
        'esco_mapped': bool(skill.esco_skill_id),
        'similarity_score': round(similarity_score, 3),
    }


def _find_similar_resolved_skills(workspace: IntakeWorkspace, skill: Skill) -> list[dict[str, Any]]:
    resolved_skills = list(
        Skill.objects.filter(
            workspace=workspace,
            resolution_status=Skill.ResolutionStatus.RESOLVED,
        ).exclude(pk=skill.pk).prefetch_related('aliases').order_by('display_name_en')
    )
    search_terms = _collect_skill_terms(skill)
    normalized_search_terms = {
        normalize_lookup_key(term)
        for term in search_terms
        if normalize_lookup_key(term)
    }
    base_tokens = {
        token
        for value in normalized_search_terms
        for token in value.split('-')
        if token
    }
    esco_match = _find_matching_esco_skill(*search_terms)
    candidates: list[dict[str, Any]] = []
    for candidate in resolved_skills:
        candidate_terms = _collect_skill_terms(candidate)
        normalized_candidate_terms = {
            normalize_lookup_key(term)
            for term in candidate_terms
            if normalize_lookup_key(term)
        }
        candidate_tokens = {
            token
            for value in normalized_candidate_terms
            for token in value.split('-')
            if token
        }
        exact_match = bool(normalized_search_terms & normalized_candidate_terms)
        token_overlap = (
            len(base_tokens & candidate_tokens) / len(base_tokens | candidate_tokens)
            if base_tokens and candidate_tokens else 0.0
        )
        fuzzy_score = max(
            [
                SequenceMatcher(None, term.casefold(), other.casefold()).ratio()
                for term in search_terms or [skill.display_name_en]
                for other in candidate_terms or [candidate.display_name_en]
            ] or [0.0]
        )
        score = 0.0
        if exact_match:
            score = 1.0
        elif token_overlap >= 0.34:
            score = token_overlap
        elif fuzzy_score >= 0.72:
            score = fuzzy_score
        if esco_match is not None and candidate.esco_skill_id == esco_match.pk:
            score = max(score, 0.9)
        if score < 0.6:
            continue
        candidates.append(_resolved_skill_candidate_payload(candidate, similarity_score=score))
    candidates.sort(key=lambda item: (-item['similarity_score'], item['display_name_en']))
    return candidates[:5]


def bulk_review_employee_skills(
    workspace_pk,
    employee_uuid: str,
    actions: list[dict],
) -> dict:
    employee = Employee.objects.filter(workspace_id=workspace_pk, uuid=employee_uuid).first()
    if employee is None:
        raise ValueError('Employee not found.')

    evidence_uuids = [str(item.get('evidence_uuid') or '').strip() for item in actions if str(item.get('evidence_uuid') or '').strip()]
    evidence_rows = list(
        EmployeeSkillEvidence.objects.select_related('skill', 'source')
        .filter(
            workspace_id=workspace_pk,
            employee=employee,
            uuid__in=evidence_uuids,
            source_kind=_CV_EVIDENCE_SOURCE_KIND,
        )
        .order_by('created_at')
    )
    evidence_by_uuid = {str(row.uuid): row for row in evidence_rows}
    reindex_source_ids: set[int] = set()
    profile_ids_to_clean: set[int] = set()
    processed = accepted = rejected = merged = 0
    errors: list[dict[str, Any]] = []
    skills_to_recheck: set[int] = set()

    with transaction.atomic():
        for item in actions:
            evidence_uuid = str(item.get('evidence_uuid') or '').strip()
            action = _normalize_review_action(str(item.get('action') or ''))
            note = str(item.get('note') or '').strip()
            evidence_row = evidence_by_uuid.get(evidence_uuid)
            if evidence_row is None:
                errors.append({'evidence_uuid': evidence_uuid, 'message': 'Evidence row not found.'})
                continue
            if not action:
                errors.append({'evidence_uuid': evidence_uuid, 'message': 'Unsupported action.'})
                continue

            profile = EmployeeCVProfile.objects.filter(source=evidence_row.source).first() if evidence_row.source_id else None
            if profile is not None:
                profile_ids_to_clean.add(profile.pk)
            if evidence_row.source_id:
                reindex_source_ids.add(evidence_row.source_id)

            skill = evidence_row.skill
            default_weight = _rebuild_weight_for_evidence_row(evidence_row)
            terms = _collect_skill_terms(skill, evidence_row=evidence_row)

            if action == EmployeeSkillEvidence.OperatorAction.ACCEPTED:
                _set_skill_state(
                    skill,
                    resolution_status=Skill.ResolutionStatus.RESOLVED,
                    is_operator_confirmed=True,
                    source_terms=terms,
                )
                evidence_row.is_operator_confirmed = True
                evidence_row.operator_action = EmployeeSkillEvidence.OperatorAction.ACCEPTED
                evidence_row.operator_note = note
                evidence_row.weight = max(float(evidence_row.weight or 0.0), default_weight)
                evidence_row.metadata = {
                    **dict(evidence_row.metadata or {}),
                    'resolution_status': Skill.ResolutionStatus.RESOLVED,
                }
                evidence_row.metadata = _json_safe(evidence_row.metadata)
                evidence_row.save(
                    update_fields=[
                        'is_operator_confirmed',
                        'operator_action',
                        'operator_note',
                        'weight',
                        'metadata',
                        'updated_at',
                    ]
                )
                _upsert_skill_resolution_overrides(
                    employee.workspace,
                    terms=terms,
                    target_skill=skill,
                    status=CatalogOverrideStatus.APPROVED,
                    source='bulk_employee_skill_review',
                    note=note,
                    metadata={
                        'review_scope': 'employee',
                        'employee_uuid': str(employee.uuid),
                        'evidence_uuid': str(evidence_row.uuid),
                    },
                )
                _mark_catalog_review_items_resolved(
                    employee.workspace,
                    terms=terms,
                    resolved_via='bulk_employee_skill_review',
                    operator_name='',
                    override_canonical_key=skill.canonical_key,
                )
                SkillReviewDecision.objects.update_or_create(
                    workspace=employee.workspace,
                    employee=employee,
                    skill_canonical_key=skill.canonical_key,
                    defaults={
                        'action': EmployeeSkillEvidence.OperatorAction.ACCEPTED,
                        'merge_target_skill_uuid': None,
                        'note': note,
                        'reviewed_by': '',
                    },
                )
                accepted += 1
                processed += 1
                continue

            if action == EmployeeSkillEvidence.OperatorAction.REJECTED:
                evidence_row.is_operator_confirmed = False
                evidence_row.operator_action = EmployeeSkillEvidence.OperatorAction.REJECTED
                evidence_row.operator_note = note
                evidence_row.weight = 0
                evidence_row.save(
                    update_fields=[
                        'is_operator_confirmed',
                        'operator_action',
                        'operator_note',
                        'weight',
                        'updated_at',
                    ]
                )
                SkillReviewDecision.objects.update_or_create(
                    workspace=employee.workspace,
                    employee=employee,
                    skill_canonical_key=skill.canonical_key,
                    defaults={
                        'action': EmployeeSkillEvidence.OperatorAction.REJECTED,
                        'merge_target_skill_uuid': None,
                        'note': note,
                        'reviewed_by': '',
                    },
                )
                skills_to_recheck.add(skill.pk)
                rejected += 1
                processed += 1
                continue

            merge_target_skill_uuid = str(item.get('merge_target_skill_uuid') or '').strip()
            merge_target = Skill.objects.filter(
                workspace=employee.workspace,
                uuid=merge_target_skill_uuid,
            ).first()
            if merge_target is None:
                errors.append({'evidence_uuid': evidence_uuid, 'message': 'Merge target skill not found.'})
                continue
            _set_skill_state(
                merge_target,
                resolution_status=Skill.ResolutionStatus.RESOLVED,
                is_operator_confirmed=True,
                source_terms=terms,
            )
            evidence_row.skill = merge_target
            evidence_row.is_operator_confirmed = True
            evidence_row.operator_action = EmployeeSkillEvidence.OperatorAction.MERGED
            evidence_row.operator_note = note
            evidence_row.weight = max(float(evidence_row.weight or 0.0), default_weight)
            evidence_row.metadata = {
                **dict(evidence_row.metadata or {}),
                'resolution_status': merge_target.resolution_status,
                'merged_from_skill_uuid': str(skill.uuid),
                'merged_from_skill_key': skill.canonical_key,
                'skill_key': merge_target.canonical_key,
            }
            evidence_row.metadata = _json_safe(evidence_row.metadata)
            evidence_row.save(
                update_fields=[
                    'skill',
                    'is_operator_confirmed',
                    'operator_action',
                    'operator_note',
                    'weight',
                    'metadata',
                    'updated_at',
                ]
            )
            _upsert_skill_resolution_overrides(
                employee.workspace,
                terms=terms,
                target_skill=merge_target,
                status=CatalogOverrideStatus.APPROVED,
                source='bulk_employee_skill_review',
                note=note,
                metadata={
                    'review_scope': 'employee',
                    'employee_uuid': str(employee.uuid),
                    'merged_from_skill_uuid': str(skill.uuid),
                    'merge_target_skill_uuid': str(merge_target.uuid),
                },
            )
            _mark_catalog_review_items_resolved(
                employee.workspace,
                terms=terms,
                resolved_via='bulk_employee_skill_review',
                operator_name='',
                override_canonical_key=merge_target.canonical_key,
            )
            SkillReviewDecision.objects.update_or_create(
                workspace=employee.workspace,
                employee=employee,
                skill_canonical_key=skill.canonical_key,
                defaults={
                    'action': EmployeeSkillEvidence.OperatorAction.MERGED,
                    'merge_target_skill_uuid': merge_target.uuid,
                    'note': note,
                    'reviewed_by': '',
                },
            )
            skills_to_recheck.add(skill.pk)
            merged += 1
            processed += 1

        for skill_pk in skills_to_recheck:
            skill = Skill.objects.filter(pk=skill_pk).first()
            if skill is not None:
                _maybe_mark_skill_rejected_if_unused(skill)

        for profile in EmployeeCVProfile.objects.filter(pk__in=sorted(profile_ids_to_clean)).select_related('source'):
            related_rows = EmployeeSkillEvidence.objects.filter(
                workspace=employee.workspace,
                employee=employee,
                source=profile.source,
                source_kind=_CV_EVIDENCE_SOURCE_KIND,
            ).select_related('skill')
            canonical_keys = list({row.skill.canonical_key for row in related_rows})
            terms = dedupe_strings(
                [
                    value
                    for row in related_rows
                    for value in _collect_skill_terms(row.skill, evidence_row=row)
                ]
            )
            _clear_pending_skill_candidates_for_profile(
                profile,
                canonical_keys=canonical_keys,
                terms=terms,
            )

    _reindex_cv_profiles_for_sources(reindex_source_ids)
    return {
        'processed': processed,
        'accepted': accepted,
        'rejected': rejected,
        'merged': merged,
        'errors': errors,
    }


def list_workspace_pending_skills(workspace_pk) -> list[dict]:
    workspace = IntakeWorkspace.objects.get(pk=workspace_pk)
    pending_skills = list(
        Skill.objects.filter(
            workspace=workspace,
            resolution_status=Skill.ResolutionStatus.PENDING_REVIEW,
        ).order_by('display_name_en', 'canonical_key')
    )
    evidence_rows = list(
        EmployeeSkillEvidence.objects.select_related('skill', 'employee')
        .filter(
            workspace=workspace,
            skill__in=pending_skills,
            source_kind=_CV_EVIDENCE_SOURCE_KIND,
            weight__gt=0,
        )
        .order_by('skill_id', '-confidence', 'employee__full_name')
    )
    rows_by_skill: dict[int, list[EmployeeSkillEvidence]] = {}
    for row in evidence_rows:
        rows_by_skill.setdefault(row.skill_id, []).append(row)

    items: list[dict[str, Any]] = []
    for skill in pending_skills:
        rows = rows_by_skill.get(skill.pk, [])
        if not rows:
            continue
        confidence_values = [float(row.confidence or 0.0) for row in rows]
        items.append(
            {
                'skill_uuid': str(skill.uuid),
                'canonical_key': skill.canonical_key,
                'display_name_en': skill.display_name_en,
                'display_name_ru': skill.display_name_ru,
                'employee_count': len({row.employee_id for row in rows}),
                'total_evidence_count': len(rows),
                'avg_confidence': round(sum(confidence_values) / len(confidence_values), 2) if confidence_values else 0.0,
                'sample_evidence_texts': [str(row.evidence_text or '').strip() for row in rows[:3] if str(row.evidence_text or '').strip()],
                'sample_employees': [row.employee.full_name for row in rows[:5]],
                'similar_resolved_skills': _find_similar_resolved_skills(workspace, skill),
            }
        )
    items.sort(key=lambda item: (-item['employee_count'], -item['avg_confidence'], item['display_name_en']))
    return items


def bulk_resolve_workspace_skills(
    workspace_pk,
    resolutions: list[dict],
) -> dict:
    workspace = IntakeWorkspace.objects.get(pk=workspace_pk)
    processed = approved = rejected = merged = 0
    errors: list[dict[str, Any]] = []
    reindex_source_ids: set[int] = set()
    skills_to_recheck: set[int] = set()

    with transaction.atomic():
        for item in resolutions:
            skill_uuid = str(item.get('skill_uuid') or '').strip()
            action = str(item.get('action') or '').strip().lower()
            skill = Skill.objects.filter(workspace=workspace, uuid=skill_uuid).first()
            if skill is None:
                errors.append({'skill_uuid': skill_uuid, 'message': 'Skill not found.'})
                continue

            terms = _collect_skill_terms(
                skill,
                extra_terms=list(item.get('alias_terms') or []),
            )
            evidence_rows = list(
                EmployeeSkillEvidence.objects.select_related('employee', 'source')
                .filter(workspace=workspace, skill=skill)
                .order_by('employee__full_name', '-updated_at')
            )
            for row in evidence_rows:
                if row.source_id and row.source_kind == _CV_EVIDENCE_SOURCE_KIND:
                    reindex_source_ids.add(row.source_id)

            if action == 'approve':
                display_name_en = str(item.get('display_name_en') or skill.display_name_en).strip() or skill.display_name_en
                display_name_ru = str(item.get('display_name_ru') or skill.display_name_ru).strip()
                _set_skill_state(
                    skill,
                    resolution_status=Skill.ResolutionStatus.RESOLVED,
                    is_operator_confirmed=True,
                    display_name_en=display_name_en,
                    display_name_ru=display_name_ru,
                    source_terms=terms,
                )
                merge_skill_aliases_sync(skill, terms)
                for row in evidence_rows:
                    row.is_operator_confirmed = True
                    row.operator_action = EmployeeSkillEvidence.OperatorAction.ACCEPTED
                    row.operator_note = str(item.get('note') or '').strip()
                    row.weight = max(float(row.weight or 0.0), _rebuild_weight_for_evidence_row(row))
                    row.metadata = {
                        **dict(row.metadata or {}),
                        'resolution_status': Skill.ResolutionStatus.RESOLVED,
                        'skill_key': skill.canonical_key,
                    }
                    row.metadata = _json_safe(row.metadata)
                    row.save(
                        update_fields=[
                            'is_operator_confirmed',
                            'operator_action',
                            'operator_note',
                            'weight',
                            'metadata',
                            'updated_at',
                        ]
                    )
                    SkillReviewDecision.objects.update_or_create(
                        workspace=workspace,
                        employee=row.employee,
                        skill_canonical_key=skill.canonical_key,
                        defaults={
                            'action': EmployeeSkillEvidence.OperatorAction.ACCEPTED,
                            'merge_target_skill_uuid': None,
                            'note': str(item.get('note') or '').strip(),
                            'reviewed_by': '',
                        },
                    )
                _upsert_skill_resolution_overrides(
                    workspace,
                    terms=terms,
                    target_skill=skill,
                    status=CatalogOverrideStatus.APPROVED,
                    source='workspace_skill_resolution',
                    note=str(item.get('note') or '').strip(),
                    metadata={'resolution_scope': 'workspace', 'action': 'approve'},
                )
                _mark_catalog_review_items_resolved(
                    workspace,
                    terms=terms,
                    resolved_via='workspace_skill_resolution',
                    override_canonical_key=skill.canonical_key,
                )
                approved += 1
                processed += 1
                continue

            if action == 'reject':
                _set_skill_state(
                    skill,
                    resolution_status=Skill.ResolutionStatus.REJECTED,
                    is_operator_confirmed=skill.is_operator_confirmed,
                    source_terms=terms,
                )
                for row in evidence_rows:
                    row.is_operator_confirmed = False
                    row.operator_action = EmployeeSkillEvidence.OperatorAction.REJECTED
                    row.operator_note = str(item.get('note') or '').strip()
                    row.weight = 0
                    row.save(
                        update_fields=[
                            'is_operator_confirmed',
                            'operator_action',
                            'operator_note',
                            'weight',
                            'updated_at',
                        ]
                    )
                    SkillReviewDecision.objects.update_or_create(
                        workspace=workspace,
                        employee=row.employee,
                        skill_canonical_key=skill.canonical_key,
                        defaults={
                            'action': EmployeeSkillEvidence.OperatorAction.REJECTED,
                            'merge_target_skill_uuid': None,
                            'note': str(item.get('note') or '').strip(),
                            'reviewed_by': '',
                        },
                    )
                _upsert_skill_resolution_overrides(
                    workspace,
                    terms=terms,
                    target_skill=skill,
                    status=CatalogOverrideStatus.REJECTED,
                    source='workspace_skill_resolution',
                    note=str(item.get('note') or '').strip(),
                    metadata={'resolution_scope': 'workspace', 'action': 'reject'},
                )
                rejected += 1
                processed += 1
                continue

            if action == 'merge':
                target_skill_uuid = str(item.get('target_skill_uuid') or '').strip()
                target_skill = Skill.objects.filter(workspace=workspace, uuid=target_skill_uuid).first()
                if target_skill is None:
                    errors.append({'skill_uuid': skill_uuid, 'message': 'Merge target skill not found.'})
                    continue
                _set_skill_state(
                    target_skill,
                    resolution_status=Skill.ResolutionStatus.RESOLVED,
                    is_operator_confirmed=True,
                    source_terms=terms,
                )
                merge_skill_aliases_sync(target_skill, terms)
                for row in evidence_rows:
                    row.skill = target_skill
                    row.is_operator_confirmed = True
                    row.operator_action = EmployeeSkillEvidence.OperatorAction.MERGED
                    row.operator_note = str(item.get('note') or '').strip()
                    row.weight = max(float(row.weight or 0.0), _rebuild_weight_for_evidence_row(row))
                    row.metadata = {
                        **dict(row.metadata or {}),
                        'resolution_status': target_skill.resolution_status,
                        'merged_from_skill_uuid': str(skill.uuid),
                        'merged_from_skill_key': skill.canonical_key,
                        'skill_key': target_skill.canonical_key,
                    }
                    row.metadata = _json_safe(row.metadata)
                    row.save(
                        update_fields=[
                            'skill',
                            'is_operator_confirmed',
                            'operator_action',
                            'operator_note',
                            'weight',
                            'metadata',
                            'updated_at',
                        ]
                    )
                    SkillReviewDecision.objects.update_or_create(
                        workspace=workspace,
                        employee=row.employee,
                        skill_canonical_key=skill.canonical_key,
                        defaults={
                            'action': EmployeeSkillEvidence.OperatorAction.MERGED,
                            'merge_target_skill_uuid': target_skill.uuid,
                            'note': str(item.get('note') or '').strip(),
                            'reviewed_by': '',
                        },
                    )
                _upsert_skill_resolution_overrides(
                    workspace,
                    terms=terms,
                    target_skill=target_skill,
                    status=CatalogOverrideStatus.APPROVED,
                    source='workspace_skill_resolution',
                    note=str(item.get('note') or '').strip(),
                    metadata={'resolution_scope': 'workspace', 'action': 'merge', 'merged_from_skill_uuid': str(skill.uuid)},
                )
                _mark_catalog_review_items_resolved(
                    workspace,
                    terms=terms,
                    resolved_via='workspace_skill_resolution',
                    override_canonical_key=target_skill.canonical_key,
                )
                _set_skill_state(
                    skill,
                    resolution_status=Skill.ResolutionStatus.REJECTED,
                    is_operator_confirmed=skill.is_operator_confirmed,
                )
                skills_to_recheck.add(skill.pk)
                merged += 1
                processed += 1
                continue

            if action == 'create_override':
                target_esco_uri = str(item.get('target_esco_uri') or '').strip()
                display_name_en = str(item.get('display_name_en') or skill.display_name_en).strip() or skill.display_name_en
                display_name_ru = str(item.get('display_name_ru') or skill.display_name_ru).strip()
                normalized_override = normalize_skill_seed(display_name_en, workspace=workspace, allow_freeform=True)
                esco_skill = EscoSkill.objects.filter(concept_uri=target_esco_uri).first() if target_esco_uri else None
                if target_esco_uri and esco_skill is None:
                    errors.append({'skill_uuid': skill_uuid, 'message': 'ESCO skill not found for target_esco_uri.'})
                    continue
                if esco_skill is not None:
                    normalized_override = {
                        **normalized_override,
                        'esco_skill_id': str(esco_skill.pk),
                        'esco_skill_uri': esco_skill.concept_uri,
                        'match_source': 'override',
                    }
                override_skill = ensure_workspace_skill_sync(
                    workspace,
                    normalized_skill=normalized_override,
                    preferred_display_name_ru=display_name_ru,
                    aliases=terms,
                    raw_term=display_name_en,
                    created_source='workspace_skill_resolution',
                    promote_aliases=True,
                    resolution_status=Skill.ResolutionStatus.RESOLVED,
                )
                _set_skill_state(
                    override_skill,
                    resolution_status=Skill.ResolutionStatus.RESOLVED,
                    is_operator_confirmed=True,
                    esco_skill=esco_skill,
                    source_terms=terms,
                )
                merge_skill_aliases_sync(override_skill, terms)
                for row in evidence_rows:
                    row.skill = override_skill
                    row.is_operator_confirmed = True
                    row.operator_action = EmployeeSkillEvidence.OperatorAction.ACCEPTED
                    row.operator_note = str(item.get('note') or '').strip()
                    row.weight = max(float(row.weight or 0.0), _rebuild_weight_for_evidence_row(row))
                    row.metadata = {
                        **dict(row.metadata or {}),
                        'resolution_status': Skill.ResolutionStatus.RESOLVED,
                        'skill_key': override_skill.canonical_key,
                    }
                    row.metadata = _json_safe(row.metadata)
                    row.save(
                        update_fields=[
                            'skill',
                            'is_operator_confirmed',
                            'operator_action',
                            'operator_note',
                            'weight',
                            'metadata',
                            'updated_at',
                        ]
                    )
                    SkillReviewDecision.objects.update_or_create(
                        workspace=workspace,
                        employee=row.employee,
                        skill_canonical_key=override_skill.canonical_key,
                        defaults={
                            'action': EmployeeSkillEvidence.OperatorAction.ACCEPTED,
                            'merge_target_skill_uuid': None,
                            'note': str(item.get('note') or '').strip(),
                            'reviewed_by': '',
                        },
                    )
                _upsert_skill_resolution_overrides(
                    workspace,
                    terms=terms,
                    target_skill=override_skill,
                    status=CatalogOverrideStatus.APPROVED,
                    source='workspace_skill_resolution',
                    note=str(item.get('note') or '').strip(),
                    metadata={'resolution_scope': 'workspace', 'action': 'create_override'},
                )
                _mark_catalog_review_items_resolved(
                    workspace,
                    terms=terms,
                    resolved_via='workspace_skill_resolution',
                    override_canonical_key=override_skill.canonical_key,
                )
                if override_skill.pk != skill.pk:
                    skills_to_recheck.add(skill.pk)
                approved += 1
                processed += 1
                continue

            errors.append({'skill_uuid': skill_uuid, 'message': 'Unsupported action.'})

        for skill_pk in skills_to_recheck:
            checked_skill = Skill.objects.filter(pk=skill_pk).first()
            if checked_skill is not None:
                _maybe_mark_skill_rejected_if_unused(checked_skill)

        profile_queryset = EmployeeCVProfile.objects.filter(source_id__in=sorted(reindex_source_ids))
        for profile in profile_queryset:
            related_rows = EmployeeSkillEvidence.objects.filter(
                workspace=workspace,
                source=profile.source,
                source_kind=_CV_EVIDENCE_SOURCE_KIND,
            ).select_related('skill')
            canonical_keys = list({row.skill.canonical_key for row in related_rows})
            terms = dedupe_strings(
                [
                    value
                    for row in related_rows
                    for value in _collect_skill_terms(row.skill, evidence_row=row)
                ]
            )
            _clear_pending_skill_candidates_for_profile(
                profile,
                canonical_keys=canonical_keys,
                terms=terms,
            )

    _reindex_cv_profiles_for_sources(reindex_source_ids)
    return {
        'processed': processed,
        'approved': approved,
        'rejected': rejected,
        'merged': merged,
        'errors': errors,
    }


def accept_all_high_confidence_skills(
    workspace_pk,
    employee_uuid: str,
    *,
    confidence_threshold: float = 0.7,
) -> dict:
    employee = Employee.objects.filter(workspace_id=workspace_pk, uuid=employee_uuid).first()
    if employee is None:
        raise ValueError('Employee not found.')
    pending_rows = list(
        EmployeeSkillEvidence.objects.select_related('skill')
        .filter(
            workspace_id=workspace_pk,
            employee=employee,
            source_kind=_CV_EVIDENCE_SOURCE_KIND,
            skill__resolution_status=Skill.ResolutionStatus.PENDING_REVIEW,
            confidence__gte=confidence_threshold,
            weight__gt=0,
        )
        .order_by('-confidence', 'skill__display_name_en')
    )
    if not pending_rows:
        return {'accepted_count': 0, 'skipped_count': 0}
    result = bulk_review_employee_skills(
        workspace_pk,
        str(employee.uuid),
        actions=[
            {
                'evidence_uuid': str(row.uuid),
                'action': 'accept',
            }
            for row in pending_rows
        ],
    )
    return {
        'accepted_count': int(result.get('accepted') or 0),
        'skipped_count': max(0, len(pending_rows) - int(result.get('accepted') or 0)),
        'errors': result.get('errors') or [],
    }


def _delete_workspace_employee_sync(workspace_pk, employee_uuid: str) -> dict | None:
    employee = Employee.objects.filter(workspace_id=workspace_pk, uuid=employee_uuid).first()
    if employee is None:
        return None

    linked_profiles = list(
        EmployeeCVProfile.objects.select_related('workspace', 'source').filter(
            workspace_id=workspace_pk,
            employee=employee,
        )
    )
    detached_count = 0
    deleted_name = employee.full_name
    deleted_uuid = str(employee.uuid)

    refresh_specs: list[tuple[EmployeeCVProfile, list[str]]] = []
    with transaction.atomic():
        for profile in linked_profiles:
            warnings = list((profile.metadata or {}).get('warnings') or [])
            profile.employee = None
            profile.status = EmployeeCVProfile.Status.UNMATCHED
            profile.match_confidence = 0
            profile.matched_by = 'employee_deleted'
            profile.metadata = {
                **dict(profile.metadata or {}),
                'resolution': {
                    'resolved_by': 'employee_deleted',
                    'resolution_note': 'Employee record deleted from workspace.',
                    'resolved_employee_uuid': '',
                    'resolved_status': EmployeeCVProfile.Status.UNMATCHED,
                    'deleted_employee_uuid': deleted_uuid,
                    'deleted_employee_name': deleted_name,
                },
            }
            profile.metadata = _json_safe(profile.metadata)
            profile.save(
                update_fields=['employee', 'status', 'match_confidence', 'matched_by', 'metadata', 'updated_at']
            )
            refresh_specs.append((profile, warnings))
            detached_count += 1

        deleted_employee_uuid = deleted_uuid
        deleted_candidate_profiles = list(
            EmployeeCVProfile.objects.filter(workspace_id=workspace_pk).exclude(pk__in=[profile.pk for profile in linked_profiles])
        )
        for profile in deleted_candidate_profiles:
            metadata = dict(profile.metadata or {})
            candidate_matches = list(metadata.get('candidate_matches') or [])
            filtered_candidate_matches = [
                item
                for item in candidate_matches
                if str(item.get('employee_uuid') or item.get('uuid') or '').strip() != deleted_employee_uuid
            ]
            if filtered_candidate_matches != candidate_matches:
                metadata['candidate_matches'] = filtered_candidate_matches
                profile.metadata = _json_safe(metadata)
                profile.save(update_fields=['metadata', 'updated_at'])
        employee.delete()

    for profile, warnings in refresh_specs:
        profile.refresh_from_db()
        _refresh_profile_evidence_state_sync(profile, warnings=warnings)

    return {
        'employee_uuid': deleted_uuid,
        'full_name': deleted_name,
        'detached_cv_profile_count': detached_count,
    }


def _build_cv_profile_detail_item(profile: EmployeeCVProfile) -> dict:
    metadata = dict(profile.metadata or {})
    vector_index = metadata.get('vector_index') or {}
    return {
        'source_uuid': str(profile.source.uuid),
        'source_title': profile.source.title or profile.source.source_kind,
        'status': profile.status,
        'evidence_quality': profile.evidence_quality,
        'employee_uuid': str(profile.employee.uuid) if profile.employee_id else None,
        'full_name': profile.employee.full_name if profile.employee_id else '',
        'current_title': profile.employee.current_title if profile.employee_id else '',
        'matched_by': profile.matched_by,
        'match_confidence': float(profile.match_confidence or 0.0),
        'headline': profile.headline,
        'profile_current_role': profile.current_role,
        'seniority': profile.seniority,
        'role_family': profile.role_family,
        'warnings': metadata.get('warnings') or [],
        'candidate_matches': _serialize_profile_candidate_matches(profile),
        'fact_counts': metadata.get('fact_counts') or {},
        'review_reasons': _review_reasons_for_profile(profile),
        'pending_skill_candidates': metadata.get('pending_skill_candidates') or [],
        'vector_index_status': vector_index.get('status', ''),
        'created_at': profile.created_at,
        'updated_at': profile.updated_at,
    }


def _review_reasons_for_profile(profile: EmployeeCVProfile) -> list[str]:
    reasons: list[str] = []
    if profile.status == EmployeeCVProfile.Status.AMBIGUOUS:
        reasons.append('ambiguous_match')
    elif profile.status == EmployeeCVProfile.Status.UNMATCHED:
        reasons.append('unmatched_source')
    elif profile.status == EmployeeCVProfile.Status.LOW_CONFIDENCE_MATCH:
        reasons.append('low_confidence_match')
    elif profile.status == EmployeeCVProfile.Status.EXTRACTION_FAILED:
        reasons.append('extraction_failed')

    if profile.evidence_quality == EmployeeCVProfile.EvidenceQuality.SPARSE:
        reasons.append('sparse_cv')
    elif profile.evidence_quality == EmployeeCVProfile.EvidenceQuality.EMPTY:
        reasons.append('empty_cv_evidence')

    if (profile.metadata or {}).get('pending_skill_candidates'):
        reasons.append('pending_skill_candidates')
    return dedupe_strings(reasons)
