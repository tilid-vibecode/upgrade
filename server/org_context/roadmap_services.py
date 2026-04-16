import hashlib
import json
import logging
from collections import Counter, defaultdict
from copy import deepcopy
from typing import Any, Optional

from asgiref.sync import sync_to_async
from django.db import transaction

from company_intake.models import IntakeWorkspace, WorkspaceSourceKind, WorkspaceSourceStatus
from company_intake.services import build_planning_context_profile_snapshot, build_workspace_profile_snapshot
from tools.openai.structured_client import call_openai_structured

from .models import (
    Employee,
    OrgUnit,
    ParsedSource,
    PlanningContext,
    Project,
    RoadmapAnalysisRun,
    SourceChunk,
)
from .skill_catalog import slugify_key

logger = logging.getLogger(__name__)

_PASS_ONE_MAX_CHARS = 15000

TEAM_SHAPE_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'estimated_headcount': {'type': 'integer'},
        'roles_needed': {'type': 'array', 'items': {'type': 'string'}},
        'duration_months': {'type': 'integer'},
    },
    'required': ['estimated_headcount', 'roles_needed', 'duration_months'],
}

CAPABILITY_REQUIREMENT_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'capability': {'type': 'string'},
        'level': {'type': 'string'},
        'criticality': {'type': 'string'},
    },
    'required': ['capability', 'level', 'criticality'],
}

INITIATIVE_EXTRACTION_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'source_initiatives': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'name': {'type': 'string'},
                    'goal': {'type': 'string'},
                    'criticality': {'type': 'string'},
                    'planned_window': {'type': 'string'},
                    'key_deliverables': {'type': 'array', 'items': {'type': 'string'}},
                    'tech_references': {'type': 'array', 'items': {'type': 'string'}},
                    'team_references': {'type': 'array', 'items': {'type': 'string'}},
                    'success_metrics': {'type': 'array', 'items': {'type': 'string'}},
                    'evidence_quote': {'type': 'string'},
                    'confidence': {'type': 'number'},
                },
                'required': [
                    'name',
                    'goal',
                    'criticality',
                    'planned_window',
                    'key_deliverables',
                    'tech_references',
                    'team_references',
                    'success_metrics',
                    'evidence_quote',
                    'confidence',
                ],
            },
        },
    },
    'required': ['source_initiatives'],
}

WORKSTREAM_SYNTHESIS_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'initiatives': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'id': {'type': 'string'},
                    'name': {'type': 'string'},
                    'goal': {'type': 'string'},
                    'criticality': {'type': 'string'},
                    'planned_window': {'type': 'string'},
                    'source_refs': {'type': 'array', 'items': {'type': 'string'}},
                    'confidence': {'type': 'number'},
                },
                'required': [
                    'id',
                    'name',
                    'goal',
                    'criticality',
                    'planned_window',
                    'source_refs',
                    'confidence',
                ],
            },
        },
        'workstreams': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'id': {'type': 'string'},
                    'initiative_id': {'type': 'string'},
                    'name': {'type': 'string'},
                    'scope': {'type': 'string'},
                    'delivery_type': {'type': 'string'},
                    'affected_systems': {'type': 'array', 'items': {'type': 'string'}},
                    'team_shape': TEAM_SHAPE_SCHEMA,
                    'required_capabilities': {'type': 'array', 'items': CAPABILITY_REQUIREMENT_SCHEMA},
                    'estimated_effort': {'type': 'string'},
                    'confidence': {'type': 'number'},
                    'source_refs': {'type': 'array', 'items': {'type': 'string'}},
                },
                'required': [
                    'id',
                    'initiative_id',
                    'name',
                    'scope',
                    'delivery_type',
                    'affected_systems',
                    'team_shape',
                    'required_capabilities',
                    'estimated_effort',
                    'confidence',
                    'source_refs',
                ],
            },
        },
    },
    'required': ['initiatives', 'workstreams'],
}

CAPABILITY_BUNDLE_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'capability_bundles': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'bundle_id': {'type': 'string'},
                    'workstream_ids': {'type': 'array', 'items': {'type': 'string'}},
                    'capability_name': {'type': 'string'},
                    'capability_type': {'type': 'string'},
                    'criticality': {'type': 'string'},
                    'inferred_role_families': {'type': 'array', 'items': {'type': 'string'}},
                    'skill_hints': {'type': 'array', 'items': {'type': 'string'}},
                    'evidence_refs': {'type': 'array', 'items': {'type': 'string'}},
                    'confidence': {'type': 'number'},
                },
                'required': [
                    'bundle_id',
                    'workstream_ids',
                    'capability_name',
                    'capability_type',
                    'criticality',
                    'inferred_role_families',
                    'skill_hints',
                    'evidence_refs',
                    'confidence',
                ],
            },
        },
        'prd_summaries': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'initiative_id': {'type': 'string'},
                    'problem_statement': {'type': 'string'},
                    'proposed_solution': {'type': 'string'},
                    'success_metrics': {'type': 'array', 'items': {'type': 'string'}},
                    'technical_approach': {'type': 'string'},
                    'open_questions': {'type': 'array', 'items': {'type': 'string'}},
                },
                'required': [
                    'initiative_id',
                    'problem_statement',
                    'proposed_solution',
                    'success_metrics',
                    'technical_approach',
                    'open_questions',
                ],
            },
        },
    },
    'required': ['capability_bundles', 'prd_summaries'],
}

RISK_ANALYSIS_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'dependencies': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'from_workstream_id': {'type': 'string'},
                    'to_workstream_id': {'type': 'string'},
                    'dependency_type': {'type': 'string'},
                    'description': {'type': 'string'},
                    'criticality': {'type': 'string'},
                },
                'required': [
                    'from_workstream_id',
                    'to_workstream_id',
                    'dependency_type',
                    'description',
                    'criticality',
                ],
            },
        },
        'delivery_risks': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'id': {'type': 'string'},
                    'risk_type': {'type': 'string'},
                    'description': {'type': 'string'},
                    'affected_workstreams': {'type': 'array', 'items': {'type': 'string'}},
                    'severity': {'type': 'string'},
                    'mitigation_hint': {'type': 'string'},
                    'confidence': {'type': 'number'},
                },
                'required': [
                    'id',
                    'risk_type',
                    'description',
                    'affected_workstreams',
                    'severity',
                    'mitigation_hint',
                    'confidence',
                ],
            },
        },
    },
    'required': ['dependencies', 'delivery_risks'],
}


def _dedupe_strings(values: list[Any], *, limit: Optional[int] = None) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values or []:
        text = str(value or '').strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(text)
        if limit is not None and len(deduped) >= limit:
            break
    return deduped


def _normalize_confidence(value: Any, *, default: float = 0.7) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(round(confidence, 4), 1.0))


def _normalize_choice(value: Any, allowed: set[str], *, default: str) -> str:
    normalized = str(value or '').strip().lower()
    return normalized if normalized in allowed else default


def _normalize_source_refs(value: Any) -> list[str]:
    return _dedupe_strings([str(item or '').strip() for item in (value or [])])


def _normalize_capabilities(value: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in value or []:
        if not isinstance(item, dict):
            text = str(item or '').strip()
            if not text:
                continue
            normalized.append(
                {
                    'capability': text,
                    'level': 'intermediate',
                    'criticality': 'medium',
                }
            )
            continue
        capability = str(item.get('capability') or item.get('name') or '').strip()
        if not capability:
            continue
        normalized.append(
            {
                'capability': capability,
                'level': _normalize_choice(
                    item.get('level'),
                    {'awareness', 'beginner', 'intermediate', 'advanced', 'expert'},
                    default='intermediate',
                ),
                'criticality': _normalize_choice(
                    item.get('criticality'),
                    {'high', 'medium', 'low'},
                    default='medium',
                ),
            }
        )
    return normalized[:8]


def _normalize_team_shape(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    try:
        estimated_headcount = int(payload.get('estimated_headcount') or 0)
    except (TypeError, ValueError):
        estimated_headcount = 0
    try:
        duration_months = int(payload.get('duration_months') or 0)
    except (TypeError, ValueError):
        duration_months = 0
    return {
        'estimated_headcount': max(0, estimated_headcount),
        'roles_needed': _dedupe_strings(payload.get('roles_needed') or [], limit=8),
        'duration_months': max(0, duration_months),
    }


def _normalize_initiative_id(name: str) -> str:
    return f'init-{slugify_key(name) or "initiative"}'


def _normalize_workstream_id(name: str) -> str:
    return f'ws-{slugify_key(name) or "workstream"}'


def _normalize_bundle_id(name: str) -> str:
    return f'bundle-{slugify_key(name) or "capability"}'


def _normalize_risk_id(risk_type: str, description: str) -> str:
    seed = slugify_key(f'{risk_type}-{description}') or slugify_key(description) or 'risk'
    return f'risk-{seed}'


def _resolve_roadmap_profile_snapshot(workspace: IntakeWorkspace, planning_context=None) -> dict[str, Any]:
    if planning_context is None:
        return build_workspace_profile_snapshot(workspace)
    return build_planning_context_profile_snapshot(planning_context)


def _resolve_roadmap_parsed_sources_sync(
    workspace_pk: int,
    *,
    planning_context_pk: int | None = None,
) -> list[ParsedSource]:
    workspace = IntakeWorkspace.objects.get(pk=workspace_pk)
    queryset = (
        ParsedSource.objects.select_related('source')
        .prefetch_related('chunks')
        .filter(
            workspace=workspace,
            source__status=WorkspaceSourceStatus.PARSED,
        )
    )
    if planning_context_pk is None:
        return list(
            queryset.filter(
                source__source_kind__in=[WorkspaceSourceKind.ROADMAP, WorkspaceSourceKind.STRATEGY],
            ).order_by('source__source_kind', 'created_at')
        )

    planning_context = PlanningContext.objects.select_related('workspace', 'parent_context').get(pk=planning_context_pk)
    effective_links = PlanningContext.resolve_effective_sources(planning_context)
    source_ids = [
        link.workspace_source_id
        for link in effective_links
        if link.include_in_roadmap_analysis and link.usage_type in {'roadmap', 'strategy'}
    ]
    if not source_ids:
        return []
    return list(
        queryset.filter(source_id__in=source_ids).order_by('source__source_kind', 'created_at')
    )


def _build_analysis_fingerprint(
    workspace: IntakeWorkspace,
    planning_context=None,
    *,
    parsed_sources: list[ParsedSource] | None = None,
    profile_snapshot: dict[str, Any] | None = None,
) -> str:
    sources = parsed_sources or _resolve_roadmap_parsed_sources_sync(
        workspace.pk,
        planning_context_pk=getattr(planning_context, 'pk', None),
    )
    source_parts: list[tuple[str, str, str]] = []
    for parsed in sources:
        parsed_meta_hash = hashlib.md5(
            json.dumps(parsed.metadata or {}, sort_keys=True, default=str).encode()
        ).hexdigest()
        source_parts.append(
            (
                str(parsed.source_id),
                str(parsed.updated_at),
                parsed_meta_hash,
            )
        )

    effective_profile = profile_snapshot or _resolve_roadmap_profile_snapshot(workspace, planning_context)
    profile_hash = hashlib.md5(
        json.dumps(effective_profile, sort_keys=True, default=str).encode()
    ).hexdigest()

    fingerprint_data = {
        'sources': source_parts,
        'profile_hash': profile_hash,
        'planning_context_uuid': str(getattr(planning_context, 'uuid', '') or ''),
    }
    return hashlib.sha256(
        json.dumps(fingerprint_data, sort_keys=True, default=str).encode()
    ).hexdigest()


def _serialize_source_summary(parsed_sources: list[ParsedSource]) -> dict[str, Any]:
    counts_by_kind = Counter(parsed.source.source_kind for parsed in parsed_sources)
    source_items = [
        {
            'source_uuid': str(parsed.source_id),
            'parsed_source_uuid': str(parsed.uuid),
            'title': parsed.source.title,
            'source_kind': parsed.source.source_kind,
            'chunk_count': parsed.chunks.count() if hasattr(parsed, 'chunks') else 0,
            'char_count': parsed.char_count,
        }
        for parsed in parsed_sources
    ]
    return {
        'counts_by_kind': dict(counts_by_kind),
        'source_count': len(parsed_sources),
        'sources': source_items,
    }


def _build_org_summary(workspace: IntakeWorkspace) -> dict[str, Any]:
    employee_count = Employee.objects.filter(workspace=workspace).count()
    org_units = list(
        OrgUnit.objects.filter(workspace=workspace).order_by('name').values_list('name', flat=True)[:20]
    )
    projects = list(
        Project.objects.filter(workspace=workspace).order_by('name').values_list('name', flat=True)[:20]
    )
    sample_titles = list(
        Employee.objects.filter(workspace=workspace)
        .exclude(current_title='')
        .order_by('current_title')
        .values_list('current_title', flat=True)[:30]
    )
    return {
        'employee_count': employee_count,
        'org_units': org_units,
        'projects': projects,
        'sample_current_titles': sample_titles,
    }


def _build_source_sections(parsed_source: ParsedSource) -> list[dict[str, Any]]:
    base_text = str(parsed_source.extracted_text or '').strip()
    if len(base_text) <= _PASS_ONE_MAX_CHARS or not parsed_source.chunks.exists():
        return [
            {
                'text': base_text[:_PASS_ONE_MAX_CHARS],
                'source_refs': [str(parsed_source.source_id)],
                'section_label': parsed_source.source.title or parsed_source.source.source_kind,
            }
        ]

    sections: list[dict[str, Any]] = []
    current_parts: list[str] = []
    current_refs: list[str] = []
    current_size = 0
    for chunk in parsed_source.chunks.order_by('chunk_index'):
        chunk_text = str(chunk.text or '').strip()
        if not chunk_text:
            continue
        chunk_ref = f'{parsed_source.source_id}:chunk-{chunk.chunk_index}'
        if current_parts and current_size + len(chunk_text) > _PASS_ONE_MAX_CHARS:
            sections.append(
                {
                    'text': '\n\n'.join(current_parts)[:_PASS_ONE_MAX_CHARS],
                    'source_refs': current_refs[:],
                    'section_label': f'{parsed_source.source.title or parsed_source.source.source_kind} / section {len(sections) + 1}',
                }
            )
            current_parts = []
            current_refs = []
            current_size = 0
        current_parts.append(chunk_text)
        current_refs.append(chunk_ref)
        current_size += len(chunk_text)
    if current_parts:
        sections.append(
            {
                'text': '\n\n'.join(current_parts)[:_PASS_ONE_MAX_CHARS],
                'source_refs': current_refs[:],
                'section_label': f'{parsed_source.source.title or parsed_source.source.source_kind} / section {len(sections) + 1}',
            }
        )
    return sections or [
        {
            'text': base_text[:_PASS_ONE_MAX_CHARS],
            'source_refs': [str(parsed_source.source_id)],
            'section_label': parsed_source.source.title or parsed_source.source.source_kind,
        }
    ]


async def _extract_initiatives_for_section(
    *,
    workspace_profile: dict[str, Any],
    parsed_source: ParsedSource,
    section_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    company_profile = workspace_profile.get('company_profile') or {}
    pilot_scope = workspace_profile.get('pilot_scope') or {}
    user_prompt = (
        '## Company profile\n'
        f'{json.dumps(company_profile, ensure_ascii=False, indent=2)}\n\n'
        '## Pilot scope\n'
        f'{json.dumps(pilot_scope, ensure_ascii=False, indent=2)}\n\n'
        '## Source metadata\n'
        f'{json.dumps({"title": parsed_source.source.title, "kind": parsed_source.source.source_kind, "section_label": section_payload["section_label"]}, ensure_ascii=False, indent=2)}\n\n'
        '## Source text\n'
        f'{section_payload["text"]}'
    )
    system_prompt = (
        'Extract strategic initiatives from this roadmap or strategy document section.\n\n'
        'Rules:\n'
        '- An initiative is a planned effort with a goal, scope, timeline, and impact.\n'
        '- Do not invent initiatives not grounded in the document.\n'
        '- Keep initiatives distinct even when they are related.\n'
        '- Mark criticality as high, medium, or low.\n'
        '- Use planned_window for any quarter, half, or year timing language.\n'
        '- Confidence should be 0.0 to 1.0 based on how explicit the evidence is.'
    )
    result = await call_openai_structured(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_name='roadmap_initiative_extraction',
        schema=INITIATIVE_EXTRACTION_SCHEMA,
        temperature=0.1,
        max_tokens=2800,
    )
    initiatives: list[dict[str, Any]] = []
    for item in result.parsed.get('source_initiatives', []):
        if not str(item.get('name') or '').strip():
            continue
        source_refs = _normalize_source_refs(item.get('source_refs') or section_payload.get('source_refs'))
        initiatives.append(
            {
                'id': _normalize_initiative_id(str(item.get('name') or 'initiative')),
                'name': str(item.get('name') or '').strip(),
                'goal': str(item.get('goal') or '').strip(),
                'criticality': _normalize_choice(item.get('criticality'), {'high', 'medium', 'low'}, default='medium'),
                'planned_window': str(item.get('planned_window') or '').strip(),
                'source_refs': source_refs,
                'confidence': _normalize_confidence(item.get('confidence')),
                'key_deliverables': _dedupe_strings(item.get('key_deliverables') or [], limit=8),
                'tech_references': _dedupe_strings(item.get('tech_references') or [], limit=10),
                'team_references': _dedupe_strings(item.get('team_references') or [], limit=10),
                'success_metrics': _dedupe_strings(item.get('success_metrics') or [], limit=8),
                'evidence_quote': str(item.get('evidence_quote') or '').strip(),
            }
        )
    return initiatives


def _merge_pass_one_initiatives(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        key = _normalize_initiative_id(item.get('name') or 'initiative')
        current = grouped.get(key)
        if current is None:
            grouped[key] = deepcopy(item)
            grouped[key]['id'] = key
            continue
        current['source_refs'] = _dedupe_strings((current.get('source_refs') or []) + (item.get('source_refs') or []))
        current['key_deliverables'] = _dedupe_strings((current.get('key_deliverables') or []) + (item.get('key_deliverables') or []), limit=10)
        current['tech_references'] = _dedupe_strings((current.get('tech_references') or []) + (item.get('tech_references') or []), limit=12)
        current['team_references'] = _dedupe_strings((current.get('team_references') or []) + (item.get('team_references') or []), limit=12)
        current['success_metrics'] = _dedupe_strings((current.get('success_metrics') or []) + (item.get('success_metrics') or []), limit=10)
        current['confidence'] = max(
            _normalize_confidence(current.get('confidence')),
            _normalize_confidence(item.get('confidence')),
        )
        if len(str(item.get('goal') or '')) > len(str(current.get('goal') or '')):
            current['goal'] = item.get('goal') or current.get('goal')
        if len(str(item.get('planned_window') or '')) > len(str(current.get('planned_window') or '')):
            current['planned_window'] = item.get('planned_window') or current.get('planned_window')
        if current.get('criticality') != 'high':
            current['criticality'] = _normalize_choice(
                item.get('criticality'),
                {'high', 'medium', 'low'},
                default=current.get('criticality', 'medium'),
            )
    return list(grouped.values())


async def _run_pass_two(
    *,
    workspace_profile: dict[str, Any],
    org_summary: dict[str, Any],
    pass_one_initiatives: list[dict[str, Any]],
) -> dict[str, Any]:
    user_prompt = (
        '## Company profile\n'
        f'{json.dumps(workspace_profile.get("company_profile") or {}, ensure_ascii=False, indent=2)}\n\n'
        '## Organization summary\n'
        f'{json.dumps(org_summary, ensure_ascii=False, indent=2)}\n\n'
        '## Initiatives extracted from source sections\n'
        f'{json.dumps(pass_one_initiatives, ensure_ascii=False, indent=2)}'
    )
    system_prompt = (
        'You are synthesizing roadmap initiatives into delivery workstreams.\n\n'
        'Step 1: Merge duplicate initiatives across documents.\n'
        'Step 2: Decompose each initiative into concrete workstreams.\n'
        'Consider backend, frontend, QA, platform, analytics, security, data, documentation, and release needs when the evidence supports them.\n'
        'Use source_refs from the incoming initiatives where possible.'
    )
    result = await call_openai_structured(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_name='roadmap_workstream_synthesis',
        schema=WORKSTREAM_SYNTHESIS_SCHEMA,
        temperature=0.1,
        max_tokens=4200,
    )
    return result.parsed


async def _run_pass_three(
    *,
    workspace_profile: dict[str, Any],
    org_summary: dict[str, Any],
    initiatives: list[dict[str, Any]],
    workstreams: list[dict[str, Any]],
) -> dict[str, Any]:
    user_prompt = (
        '## Company profile\n'
        f'{json.dumps(workspace_profile.get("company_profile") or {}, ensure_ascii=False, indent=2)}\n\n'
        '## Organization summary\n'
        f'{json.dumps(org_summary, ensure_ascii=False, indent=2)}\n\n'
        '## Initiatives\n'
        f'{json.dumps(initiatives, ensure_ascii=False, indent=2)}\n\n'
        '## Workstreams\n'
        f'{json.dumps(workstreams, ensure_ascii=False, indent=2)}'
    )
    system_prompt = (
        'Group workstream capability needs into coherent capability bundles.\n'
        'Each bundle should map to one or two role families and include concrete skill hints.\n'
        'Also write concise PRD-style summaries per initiative with open questions when scope is ambiguous.'
    )
    result = await call_openai_structured(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_name='roadmap_capability_bundles',
        schema=CAPABILITY_BUNDLE_SCHEMA,
        temperature=0.1,
        max_tokens=3200,
    )
    return result.parsed


async def _run_pass_four(
    *,
    org_summary: dict[str, Any],
    initiatives: list[dict[str, Any]],
    workstreams: list[dict[str, Any]],
    capability_bundles: list[dict[str, Any]],
) -> dict[str, Any]:
    user_prompt = (
        '## Organization summary\n'
        f'{json.dumps(org_summary, ensure_ascii=False, indent=2)}\n\n'
        '## Initiatives\n'
        f'{json.dumps(initiatives, ensure_ascii=False, indent=2)}\n\n'
        '## Workstreams\n'
        f'{json.dumps(workstreams, ensure_ascii=False, indent=2)}\n\n'
        '## Capability bundles\n'
        f'{json.dumps(capability_bundles, ensure_ascii=False, indent=2)}'
    )
    system_prompt = (
        'Analyze roadmap delivery for dependencies and risks.\n'
        'Dependency types include api_contract, data_pipeline, shared_service, sequential, and shared_team.\n'
        'Risk types include concentration, skill_gap, timeline, dependency_chain, scope_ambiguity, and technology_risk.\n'
        'Suggest concise mitigation hints for each risk.'
    )
    result = await call_openai_structured(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_name='roadmap_risk_analysis',
        schema=RISK_ANALYSIS_SCHEMA,
        temperature=0.1,
        max_tokens=2600,
    )
    return result.parsed


def _normalize_pass_two_output(
    result: dict[str, Any],
    pass_one_initiatives: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pass_one_by_id = {
        item['id']: item
        for item in pass_one_initiatives
        if item.get('id')
    }

    initiatives: list[dict[str, Any]] = []
    initiative_lookup: dict[str, dict[str, Any]] = {}
    for item in result.get('initiatives', []):
        name = str(item.get('name') or '').strip()
        if not name:
            continue
        initiative_id = _normalize_initiative_id(name)
        source_refs = _normalize_source_refs(item.get('source_refs'))
        if not source_refs:
            for source_item in pass_one_initiatives:
                if _normalize_initiative_id(source_item.get('name') or '') == initiative_id:
                    source_refs.extend(source_item.get('source_refs') or [])
        normalized = {
            'id': initiative_id,
            'name': name,
            'goal': str(item.get('goal') or '').strip(),
            'criticality': _normalize_choice(item.get('criticality'), {'high', 'medium', 'low'}, default='medium'),
            'planned_window': str(item.get('planned_window') or '').strip(),
            'source_refs': _dedupe_strings(source_refs),
            'confidence': _normalize_confidence(item.get('confidence')),
        }
        initiatives.append(normalized)
        initiative_lookup[initiative_id] = normalized

    if not initiatives:
        for item in pass_one_initiatives:
            initiative_lookup[item['id']] = {
                'id': item['id'],
                'name': item.get('name', ''),
                'goal': item.get('goal', ''),
                'criticality': item.get('criticality', 'medium'),
                'planned_window': item.get('planned_window', ''),
                'source_refs': _normalize_source_refs(item.get('source_refs')),
                'confidence': _normalize_confidence(item.get('confidence')),
            }
        initiatives = list(initiative_lookup.values())

    workstreams: list[dict[str, Any]] = []
    for item in result.get('workstreams', []):
        name = str(item.get('name') or '').strip()
        if not name:
            continue
        initiative_name_or_id = str(item.get('initiative_id') or '').strip()
        initiative_id = initiative_name_or_id if initiative_name_or_id in initiative_lookup else _normalize_initiative_id(initiative_name_or_id)
        if initiative_id not in initiative_lookup and pass_one_by_id:
            initiative_id = next(iter(pass_one_by_id))
        workstreams.append(
            {
                'id': _normalize_workstream_id(name),
                'initiative_id': initiative_id,
                'name': name,
                'scope': str(item.get('scope') or '').strip(),
                'delivery_type': _normalize_choice(
                    item.get('delivery_type'),
                    {
                        'backend_service',
                        'frontend_app',
                        'mobile_app',
                        'data_pipeline',
                        'new_service',
                        'feature_extension',
                        'migration',
                        'integration',
                        'infrastructure',
                        'analytics',
                        'qa',
                        'security',
                        'documentation',
                        'release',
                    },
                    default='feature_extension',
                ),
                'affected_systems': _dedupe_strings(item.get('affected_systems') or [], limit=10),
                'team_shape': _normalize_team_shape(item.get('team_shape')),
                'required_capabilities': _normalize_capabilities(item.get('required_capabilities')),
                'estimated_effort': str(item.get('estimated_effort') or '').strip(),
                'confidence': _normalize_confidence(item.get('confidence')),
                'source_refs': _normalize_source_refs(item.get('source_refs') or (initiative_lookup.get(initiative_id, {}).get('source_refs') or [])),
            }
        )
    return initiatives, workstreams


def _build_clarification_questions_from_prds(prd_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    for summary in prd_summaries:
        initiative_id = str(summary.get('initiative_id') or '').strip()
        for index, question in enumerate(summary.get('open_questions') or [], start=1):
            question_text = str(question or '').strip()
            if not question_text:
                continue
            questions.append(
                {
                    'id': f'roadmap-{initiative_id or "initiative"}-{index}',
                    'question': question_text,
                    'scope': 'roadmap_analysis',
                    'affected_initiatives': [initiative_id] if initiative_id else [],
                    'priority': 'medium',
                }
            )
    deduped: dict[str, dict[str, Any]] = {}
    for item in questions:
        key = slugify_key(item.get('question') or '')
        if key and key not in deduped:
            deduped[key] = item
    return list(deduped.values())


def _normalize_pass_three_output(
    result: dict[str, Any],
    initiatives: list[dict[str, Any]],
    workstreams: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    initiative_lookup = {item['id']: item for item in initiatives}
    workstream_lookup = {item['id']: item for item in workstreams}

    bundles: list[dict[str, Any]] = []
    for item in result.get('capability_bundles', []):
        capability_name = str(item.get('capability_name') or '').strip()
        if not capability_name:
            continue
        workstream_ids = []
        for workstream_id in item.get('workstream_ids') or []:
            raw_id = str(workstream_id or '').strip()
            normalized_id = raw_id if raw_id in workstream_lookup else _normalize_workstream_id(raw_id)
            if normalized_id in workstream_lookup:
                workstream_ids.append(normalized_id)
        evidence_refs = _normalize_source_refs(item.get('evidence_refs'))
        if not evidence_refs:
            for workstream_id in workstream_ids:
                evidence_refs.extend(workstream_lookup.get(workstream_id, {}).get('source_refs') or [])
        bundles.append(
            {
                'bundle_id': _normalize_bundle_id(capability_name),
                'workstream_ids': _dedupe_strings(workstream_ids),
                'capability_name': capability_name,
                'capability_type': _normalize_choice(
                    item.get('capability_type'),
                    {'technical', 'domain', 'leadership', 'process'},
                    default='technical',
                ),
                'criticality': _normalize_choice(item.get('criticality'), {'high', 'medium', 'low'}, default='medium'),
                'inferred_role_families': _dedupe_strings(item.get('inferred_role_families') or [], limit=6),
                'skill_hints': _dedupe_strings(item.get('skill_hints') or [], limit=10),
                'evidence_refs': _dedupe_strings(evidence_refs),
                'confidence': _normalize_confidence(item.get('confidence')),
            }
        )

    prd_summaries: list[dict[str, Any]] = []
    for item in result.get('prd_summaries', []):
        initiative_id = str(item.get('initiative_id') or '').strip()
        normalized_initiative_id = initiative_id if initiative_id in initiative_lookup else _normalize_initiative_id(initiative_id)
        prd_summaries.append(
            {
                'initiative_id': normalized_initiative_id,
                'problem_statement': str(item.get('problem_statement') or '').strip(),
                'proposed_solution': str(item.get('proposed_solution') or '').strip(),
                'success_metrics': _dedupe_strings(item.get('success_metrics') or [], limit=8),
                'technical_approach': str(item.get('technical_approach') or '').strip(),
                'open_questions': _dedupe_strings(item.get('open_questions') or [], limit=8),
            }
        )
    return bundles, prd_summaries, _build_clarification_questions_from_prds(prd_summaries)


def _normalize_pass_four_output(
    result: dict[str, Any],
    workstreams: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    workstream_ids = {item['id'] for item in workstreams}
    dependencies: list[dict[str, Any]] = []
    for item in result.get('dependencies', []):
        from_id = str(item.get('from_workstream_id') or '').strip()
        to_id = str(item.get('to_workstream_id') or '').strip()
        normalized_from = from_id if from_id in workstream_ids else _normalize_workstream_id(from_id)
        normalized_to = to_id if to_id in workstream_ids else _normalize_workstream_id(to_id)
        if normalized_from not in workstream_ids or normalized_to not in workstream_ids:
            continue
        dependencies.append(
            {
                'from_workstream_id': normalized_from,
                'to_workstream_id': normalized_to,
                'dependency_type': _normalize_choice(
                    item.get('dependency_type'),
                    {'api_contract', 'data_pipeline', 'shared_service', 'sequential', 'shared_team'},
                    default='shared_service',
                ),
                'description': str(item.get('description') or '').strip(),
                'criticality': _normalize_choice(item.get('criticality'), {'hard', 'soft'}, default='soft'),
            }
        )

    risks: list[dict[str, Any]] = []
    for item in result.get('delivery_risks', []):
        description = str(item.get('description') or '').strip()
        risk_type = str(item.get('risk_type') or '').strip()
        if not description or not risk_type:
            continue
        affected_workstreams = []
        for workstream_id in item.get('affected_workstreams') or []:
            raw_id = str(workstream_id or '').strip()
            normalized_id = raw_id if raw_id in workstream_ids else _normalize_workstream_id(raw_id)
            if normalized_id in workstream_ids:
                affected_workstreams.append(normalized_id)
        risks.append(
            {
                'id': str(item.get('id') or '').strip() or _normalize_risk_id(risk_type, description),
                'risk_type': risk_type,
                'description': description,
                'affected_workstreams': _dedupe_strings(affected_workstreams),
                'severity': _normalize_choice(item.get('severity'), {'high', 'medium', 'low'}, default='medium'),
                'mitigation_hint': str(item.get('mitigation_hint') or '').strip(),
                'confidence': _normalize_confidence(item.get('confidence')),
            }
        )
    return dependencies, risks


async def get_latest_roadmap_analysis_run(
    workspace: IntakeWorkspace,
    *,
    planning_context=None,
    completed_only: bool = False,
) -> Optional[RoadmapAnalysisRun]:
    queryset = RoadmapAnalysisRun.objects.filter(workspace=workspace)
    if planning_context is not None:
        queryset = queryset.filter(planning_context=planning_context)
    else:
        queryset = queryset.filter(planning_context__isnull=True)
    if completed_only:
        queryset = queryset.filter(status=RoadmapAnalysisRun.Status.COMPLETED)
    return await sync_to_async(queryset.order_by('-created_at').first)()


def _build_roadmap_analysis_summary(run: RoadmapAnalysisRun) -> dict[str, Any]:
    source_summary = dict(run.source_summary or {})
    return {
        'uuid': run.uuid,
        'title': run.title,
        'status': run.status,
        'planning_context_uuid': run.planning_context_id,
        'created_at': run.created_at,
        'updated_at': run.updated_at,
        'initiative_count': len(run.initiatives or []),
        'workstream_count': len(run.workstreams or []),
        'bundle_count': len(run.capability_bundles or []),
        'risk_count': len(run.delivery_risks or []),
        'source_count': int(source_summary.get('source_count') or len(source_summary.get('sources') or [])),
    }


async def build_roadmap_analysis_status_payload(workspace: IntakeWorkspace, *, planning_context=None) -> dict[str, Any]:
    latest_run = await get_latest_roadmap_analysis_run(workspace, planning_context=planning_context)
    completed_exists = await sync_to_async(
        lambda: RoadmapAnalysisRun.objects.filter(
            workspace=workspace,
            status=RoadmapAnalysisRun.Status.COMPLETED,
            **(
                {'planning_context': planning_context}
                if planning_context is not None
                else {'planning_context__isnull': True}
            ),
        ).exists()
    )()
    return {
        'has_analysis': completed_exists,
        'latest_run': _build_roadmap_analysis_summary(latest_run) if latest_run is not None else None,
    }


async def build_roadmap_analysis_response(run: RoadmapAnalysisRun) -> dict[str, Any]:
    return {
        'uuid': run.uuid,
        'title': run.title,
        'status': run.status,
        'planning_context_uuid': run.planning_context_id,
        'analysis_version': run.analysis_version,
        'source_summary': dict(run.source_summary or {}),
        'input_snapshot': dict(run.input_snapshot or {}),
        'initiatives': list(run.initiatives or []),
        'workstreams': list(run.workstreams or []),
        'dependencies': list(run.dependencies or []),
        'delivery_risks': list(run.delivery_risks or []),
        'capability_bundles': list(run.capability_bundles or []),
        'prd_summaries': list(run.prd_summaries or []),
        'clarification_questions': list(run.clarification_questions or []),
        'error_message': run.error_message,
        'created_at': run.created_at,
        'updated_at': run.updated_at,
    }


async def run_roadmap_analysis(
    workspace: IntakeWorkspace,
    *,
    planning_context: PlanningContext | None = None,
    force_rebuild: bool = False,
) -> RoadmapAnalysisRun:
    parsed_sources = await sync_to_async(_resolve_roadmap_parsed_sources_sync)(
        workspace.pk,
        planning_context_pk=getattr(planning_context, 'pk', None),
    )
    if not parsed_sources:
        raise ValueError('Roadmap analysis requires parsed roadmap or strategy sources.')

    # Building a planning-context profile snapshot can traverse lazy Django relations
    # (for example `profile` and parent contexts), so keep that ORM work on a sync thread.
    workspace_profile = await sync_to_async(_resolve_roadmap_profile_snapshot)(
        workspace,
        planning_context,
    )
    fingerprint = await sync_to_async(_build_analysis_fingerprint)(
        workspace,
        planning_context,
        parsed_sources=parsed_sources,
        profile_snapshot=workspace_profile,
    )
    if not force_rebuild:
        existing = await sync_to_async(
            lambda: RoadmapAnalysisRun.objects.filter(
                workspace=workspace,
                status=RoadmapAnalysisRun.Status.COMPLETED,
                **(
                    {'planning_context': planning_context}
                    if planning_context is not None
                    else {'planning_context__isnull': True}
                ),
                input_snapshot__analysis_fingerprint=fingerprint,
            )
            .order_by('-created_at')
            .first()
        )()
        if existing is not None:
            return existing

    org_summary = await sync_to_async(_build_org_summary)(workspace)
    source_summary = _serialize_source_summary(parsed_sources)
    input_snapshot = {
        'analysis_fingerprint': fingerprint,
        'workspace_profile': workspace_profile,
        'org_summary': org_summary,
        'force_rebuild': bool(force_rebuild),
        'planning_context_uuid': str(getattr(planning_context, 'uuid', '') or ''),
    }

    run = await sync_to_async(RoadmapAnalysisRun.objects.create)(
        workspace=workspace,
        planning_context=planning_context,
        title='Roadmap analysis',
        status=RoadmapAnalysisRun.Status.RUNNING,
        analysis_version='roadmap-v1',
        source_summary=source_summary,
        input_snapshot=input_snapshot,
    )

    try:
        pass_one_items: list[dict[str, Any]] = []
        for parsed_source in parsed_sources:
            for section_payload in _build_source_sections(parsed_source):
                pass_one_items.extend(
                    await _extract_initiatives_for_section(
                        workspace_profile=workspace_profile,
                        parsed_source=parsed_source,
                        section_payload=section_payload,
                    )
                )
        pass_one_initiatives = _merge_pass_one_initiatives(pass_one_items)

        pass_two_result = await _run_pass_two(
            workspace_profile=workspace_profile,
            org_summary=org_summary,
            pass_one_initiatives=pass_one_initiatives,
        )
        initiatives, workstreams = _normalize_pass_two_output(pass_two_result, pass_one_initiatives)

        pass_three_result = await _run_pass_three(
            workspace_profile=workspace_profile,
            org_summary=org_summary,
            initiatives=initiatives,
            workstreams=workstreams,
        )
        capability_bundles, prd_summaries, clarification_questions = _normalize_pass_three_output(
            pass_three_result,
            initiatives,
            workstreams,
        )

        pass_four_result = await _run_pass_four(
            org_summary=org_summary,
            initiatives=initiatives,
            workstreams=workstreams,
            capability_bundles=capability_bundles,
        )
        dependencies, delivery_risks = _normalize_pass_four_output(pass_four_result, workstreams)

        def _complete_run() -> None:
            with transaction.atomic():
                fresh_run = RoadmapAnalysisRun.objects.select_for_update().get(pk=run.pk)
                fresh_run.initiatives = initiatives
                fresh_run.workstreams = workstreams
                fresh_run.capability_bundles = capability_bundles
                fresh_run.prd_summaries = prd_summaries
                fresh_run.clarification_questions = clarification_questions
                fresh_run.dependencies = dependencies
                fresh_run.delivery_risks = delivery_risks
                fresh_run.status = RoadmapAnalysisRun.Status.COMPLETED
                fresh_run.error_message = ''
                fresh_run.save(
                    update_fields=[
                        'initiatives',
                        'workstreams',
                        'capability_bundles',
                        'prd_summaries',
                        'clarification_questions',
                        'dependencies',
                        'delivery_risks',
                        'status',
                        'error_message',
                        'updated_at',
                    ]
                )

        await sync_to_async(_complete_run)()
    except Exception as exc:
        logger.exception('Roadmap analysis failed for workspace %s', workspace.slug)
        await sync_to_async(
            lambda: RoadmapAnalysisRun.objects.filter(pk=run.pk).update(
                status=RoadmapAnalysisRun.Status.FAILED,
                error_message=str(exc),
            )
        )()

    return await sync_to_async(RoadmapAnalysisRun.objects.get)(pk=run.pk)
