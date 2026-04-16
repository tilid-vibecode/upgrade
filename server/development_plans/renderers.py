from __future__ import annotations

import ast
import json
from html import escape
from typing import Any

from .models import ArtifactFormat, DevelopmentPlanRun, PlanScope

ARTIFACT_VERSION = 'stage10-v1'


def build_plan_export_payload(
    run: DevelopmentPlanRun,
    *,
    generated_at: str = '',
    frozen_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if run.scope == PlanScope.TEAM:
        return _build_team_export_payload(run, generated_at=generated_at, frozen_snapshot=frozen_snapshot)
    return _build_individual_export_payload(run, generated_at=generated_at, frozen_snapshot=frozen_snapshot)


def render_plan_artifact(
    run: DevelopmentPlanRun,
    *,
    artifact_format: str,
    generated_at: str = '',
    frozen_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    export_payload = build_plan_export_payload(
        run,
        generated_at=generated_at,
        frozen_snapshot=frozen_snapshot,
    )
    if artifact_format == ArtifactFormat.JSON:
        content = json.dumps(export_payload, ensure_ascii=False, indent=2)
        content_type = 'application/json'
        extension = 'json'
    elif artifact_format == ArtifactFormat.MARKDOWN:
        content = _render_markdown(export_payload)
        content_type = 'text/markdown'
        extension = 'md'
    elif artifact_format == ArtifactFormat.HTML:
        content = _render_html(export_payload)
        content_type = 'text/html'
        extension = 'html'
    else:
        raise ValueError(f'Unsupported artifact format: {artifact_format}')
    return {
        'payload': export_payload,
        'content': content,
        'content_type': content_type,
        'extension': extension,
    }


def _build_team_export_payload(
    run: DevelopmentPlanRun,
    *,
    generated_at: str = '',
    frozen_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = _build_export_source(run, frozen_snapshot=frozen_snapshot)
    plan = source['plan']
    recommendation = source['recommendation']
    summary = source['summary']
    return {
        'artifact_version': ARTIFACT_VERSION,
        'scope': PlanScope.TEAM,
        'title': source['title'],
        'status': source['status'],
        'is_current': bool(run.is_current),
        'workspace': source['workspace'],
        'lineage': _serialize_lineage(run, generated_at=generated_at, frozen_snapshot=frozen_snapshot),
        'summary': summary,
        'sections': {
            'pilot_scope': {
                'employee_count_in_scope': int(recommendation.get('employee_count') or 0),
                'expected_employee_count': int(summary.get('expected_employee_count') or 0),
                'batch_status': str(summary.get('batch_status') or ''),
                'plan_status': source['status'],
            },
            'company_and_roadmap_context': {
                'company_context': source['company_context'],
                'roadmap_context': source['roadmap_context'],
                'company_context_summary': _format_company_context_lines(source['company_context']),
                'roadmap_context_summary': _format_roadmap_context_lines(source['roadmap_context']),
                'executive_summary': str(plan.get('executive_summary') or '').strip(),
                'roadmap_priority_note': str(plan.get('roadmap_priority_note') or '').strip(),
            },
            'target_model': {
                'action_counts': dict(plan.get('action_counts') or recommendation.get('action_counts') or {}),
                'blueprint_run_uuid': source['lineage'].get('blueprint_run_uuid', ''),
                'matrix_run_uuid': source['lineage'].get('matrix_run_uuid', ''),
            },
            'matrix_highlights': {
                'top_priority_gaps': list(recommendation.get('top_priority_gaps') or []),
                'concentration_risks': list(recommendation.get('concentration_risks') or []),
                'near_fit_candidates': list(recommendation.get('near_fit_candidates') or []),
                'uncovered_roles': list(recommendation.get('uncovered_roles') or []),
            },
            'recommendations': {
                'priority_actions': list(plan.get('priority_actions') or []),
                'hiring_recommendations': list(plan.get('hiring_recommendations') or []),
                'development_focus': list(plan.get('development_focus') or []),
                'single_points_of_failure': list(plan.get('single_points_of_failure') or []),
            },
            'appendix': {
                'confidence_note': (
                    'These actions come from deterministic Stage 8 matrix facts and Stage 9 heuristics. '
                    'Narrative text is explanatory only.'
                ),
                'artifact_source_key': str(run.final_report_key or ''),
                'plan_run_uuid': source['lineage'].get('plan_run_uuid', ''),
                'generation_batch_uuid': source['lineage'].get('generation_batch_uuid', ''),
            },
        },
        'structured_plan': plan,
        'structured_recommendations': recommendation,
    }


def _build_individual_export_payload(
    run: DevelopmentPlanRun,
    *,
    generated_at: str = '',
    frozen_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = _build_export_source(run, frozen_snapshot=frozen_snapshot)
    plan = source['plan']
    recommendation = source['recommendation']
    summary = source['summary']
    return {
        'artifact_version': ARTIFACT_VERSION,
        'scope': PlanScope.INDIVIDUAL,
        'title': source['title'],
        'status': source['status'],
        'is_current': bool(run.is_current),
        'workspace': source['workspace'],
        'lineage': _serialize_lineage(run, generated_at=generated_at, frozen_snapshot=frozen_snapshot),
        'employee': source['employee'],
        'summary': summary,
        'sections': {
            'employee_summary': {
                'current_role_fit': str(plan.get('current_role_fit') or '').strip(),
                'current_role_goal': str(plan.get('current_role_goal') or recommendation.get('current_role_goal') or '').strip(),
                'mobility_potential': str(plan.get('mobility_potential') or recommendation.get('mobility_potential') or '').strip(),
            },
            'target_role_context': {
                'adjacent_roles': list(plan.get('adjacent_roles') or recommendation.get('adjacent_roles') or []),
                'roadmap_alignment': str(plan.get('roadmap_alignment') or '').strip(),
                'mobility_note': str(plan.get('mobility_note') or '').strip(),
                'aspiration': dict(plan.get('aspiration') or recommendation.get('aspiration') or {}),
            },
            'strongest_capabilities': {
                'strengths': list(plan.get('strengths') or []),
                'strength_cells': list(recommendation.get('strength_cells') or []),
            },
            'main_gaps': {
                'priority_gaps': list(plan.get('priority_gaps') or []),
                'gap_cells': list(recommendation.get('gap_cells') or []),
            },
            'development_actions': {
                'actions': list(plan.get('development_actions') or []),
            },
            'placeholder_resources': {
                'resources': [
                    {
                        'skill_name_en': action.get('skill_name_en', ''),
                        'course_placeholder': action.get('course_placeholder', ''),
                        'placeholder_resource_type': action.get('placeholder_resource_type', ''),
                    }
                    for action in list(plan.get('development_actions') or [])
                ],
            },
            'appendix': {
                'confidence_note': (
                    'This PDP reflects the latest completed matrix and the selected self-assessment cycle snapshot. '
                    'Narrative coaching notes do not change the structured actions.'
                ),
                'artifact_source_key': str(run.final_report_key or ''),
                'plan_run_uuid': source['lineage'].get('plan_run_uuid', ''),
                'generation_batch_uuid': source['lineage'].get('generation_batch_uuid', ''),
            },
        },
        'structured_plan': plan,
        'structured_recommendations': recommendation,
    }


def _render_markdown(export_payload: dict[str, Any]) -> str:
    if export_payload.get('scope') == PlanScope.TEAM:
        return _render_team_markdown(export_payload)
    return _render_individual_markdown(export_payload)


def _render_team_markdown(export_payload: dict[str, Any]) -> str:
    sections = export_payload['sections']
    recommendations = sections['recommendations']
    matrix_highlights = sections['matrix_highlights']
    company_and_roadmap = sections['company_and_roadmap_context']
    return '\n'.join(
        [
            f"# {export_payload.get('title', 'Team Development Plan')}",
            '',
            _render_lineage_block_markdown(export_payload),
            '',
            '## Pilot Scope',
            f"- Employees in scope: {sections['pilot_scope'].get('employee_count_in_scope', 0)}",
            f"- Expected employee count: {sections['pilot_scope'].get('expected_employee_count', 0)}",
            f"- Batch status: {sections['pilot_scope'].get('batch_status', '') or 'unknown'}",
            '',
            '## Company and Roadmap Context',
            _render_markdown_list(
                'Company context',
                list(company_and_roadmap.get('company_context_summary') or []),
            ),
            _render_markdown_list(
                'Roadmap context',
                list(company_and_roadmap.get('roadmap_context_summary') or []),
            ),
            _render_markdown_list(
                'Narrative summary',
                [
                    company_and_roadmap.get('executive_summary') or 'No summary available.',
                    company_and_roadmap.get('roadmap_priority_note') or 'No roadmap note available.',
                ],
            ),
            '',
            '## Target Model Snapshot',
            f"- Blueprint run: {sections['target_model'].get('blueprint_run_uuid', '') or 'n/a'}",
            f"- Matrix run: {sections['target_model'].get('matrix_run_uuid', '') or 'n/a'}",
            f"- Action mix: {_format_mapping_markdown(sections['target_model'].get('action_counts') or {})}",
            '',
            '## Matrix Highlights',
            _render_markdown_list(
                'Top priority gaps',
                [
                    _format_gap_markdown(item)
                    for item in list(matrix_highlights.get('top_priority_gaps') or [])[:6]
                ],
            ),
            _render_markdown_list(
                'Concentration risks',
                [
                    _format_concentration_risk_markdown(item)
                    for item in list(matrix_highlights.get('concentration_risks') or [])[:6]
                ],
            ),
            _render_markdown_list(
                'Near-fit candidates',
                [
                    _format_near_fit_markdown(item)
                    for item in list(matrix_highlights.get('near_fit_candidates') or [])[:6]
                ],
            ),
            '',
            '## Priority Actions',
            _render_markdown_list(
                '',
                [
                    _format_team_action_markdown(item)
                    for item in list(recommendations.get('priority_actions') or [])
                ],
            ),
            _render_markdown_list('Hiring recommendations', list(recommendations.get('hiring_recommendations') or [])),
            _render_markdown_list('Development focus', list(recommendations.get('development_focus') or [])),
            _render_markdown_list('Risk notes', list(recommendations.get('single_points_of_failure') or [])),
            '',
            '## Appendix',
            f"- Confidence note: {sections['appendix'].get('confidence_note', '')}",
            f"- Plan run: {sections['appendix'].get('plan_run_uuid', '') or 'n/a'}",
            f"- Generation batch: {sections['appendix'].get('generation_batch_uuid', '') or 'n/a'}",
        ]
    ).strip() + '\n'


def _render_individual_markdown(export_payload: dict[str, Any]) -> str:
    sections = export_payload['sections']
    actions = list(sections['development_actions'].get('actions') or [])
    resources = list(sections['placeholder_resources'].get('resources') or [])
    employee = export_payload.get('employee') or {}
    return '\n'.join(
        [
            f"# {export_payload.get('title', 'Individual Development Plan')}",
            '',
            _render_lineage_block_markdown(export_payload),
            '',
            '## Employee Summary',
            f"- Employee: {employee.get('full_name', '') or 'Unknown'}",
            f"- Current title: {employee.get('current_title', '') or 'Unknown'}",
            f"- Current role fit: {sections['employee_summary'].get('current_role_fit', '') or 'No fit summary available.'}",
            f"- Current role goal: {sections['employee_summary'].get('current_role_goal', '') or 'n/a'}",
            f"- Mobility potential: {sections['employee_summary'].get('mobility_potential', '') or 'n/a'}",
            '',
            '## Target Role and Adjacent Context',
            _render_markdown_list('Adjacent roles', list(sections['target_role_context'].get('adjacent_roles') or [])),
            f"Roadmap alignment: {sections['target_role_context'].get('roadmap_alignment', '') or 'No alignment note available.'}",
            '',
            f"Mobility note: {sections['target_role_context'].get('mobility_note', '') or 'No mobility note available.'}",
            '',
            '## Strongest Capabilities',
            _render_markdown_list('Strengths', list(sections['strongest_capabilities'].get('strengths') or [])),
            '',
            '## Main Gaps',
            _render_markdown_list('Priority gaps', list(sections['main_gaps'].get('priority_gaps') or [])),
            '',
            '## Development Actions',
            _render_markdown_list('', [_format_individual_action_markdown(item) for item in actions]),
            _render_markdown_list(
                'Placeholder resources',
                [
                    f"{item.get('skill_name_en', '')}: {item.get('course_placeholder', '')}"
                    for item in resources
                    if item.get('course_placeholder')
                ],
            ),
            '',
            '## Appendix',
            f"- Confidence note: {sections['appendix'].get('confidence_note', '')}",
            f"- Plan run: {sections['appendix'].get('plan_run_uuid', '') or 'n/a'}",
            f"- Generation batch: {sections['appendix'].get('generation_batch_uuid', '') or 'n/a'}",
        ]
    ).strip() + '\n'


def _render_html(export_payload: dict[str, Any]) -> str:
    if export_payload.get('scope') == PlanScope.TEAM:
        body = _render_team_html_body(export_payload)
    else:
        body = _render_individual_html_body(export_payload)
    title = escape(str(export_payload.get('title') or 'Development Plan'))
    return (
        '<!doctype html>'
        '<html lang="en"><head><meta charset="utf-8">'
        f'<title>{title}</title>'
        '<style>'
        'body{font-family:Georgia,serif;max-width:980px;margin:40px auto;padding:0 24px;line-height:1.6;color:#1f2937;background:#faf8f3;}'
        'h1,h2,h3{font-family:"Avenir Next",Helvetica,sans-serif;color:#0f172a;}'
        'section{background:#fff;padding:20px 24px;margin:18px 0;border:1px solid #e5e7eb;border-radius:14px;box-shadow:0 10px 30px rgba(15,23,42,0.05);}'
        'ul{padding-left:20px;}'
        '.meta{font-size:0.92rem;color:#475569;}'
        '.pill{display:inline-block;padding:4px 10px;border-radius:999px;background:#e2e8f0;margin-right:8px;font-size:0.86rem;}'
        'code{background:#f1f5f9;padding:2px 6px;border-radius:6px;}'
        '</style></head><body>'
        f'{body}'
        '</body></html>'
    )


def _render_team_html_body(export_payload: dict[str, Any]) -> str:
    sections = export_payload['sections']
    recommendations = sections['recommendations']
    matrix_highlights = sections['matrix_highlights']
    company_and_roadmap = sections['company_and_roadmap_context']
    return ''.join(
        [
            f"<h1>{escape(str(export_payload.get('title') or 'Team Development Plan'))}</h1>",
            _render_lineage_block_html(export_payload),
            '<section><h2>Pilot Scope</h2><ul>'
            f"<li>Employees in scope: {sections['pilot_scope'].get('employee_count_in_scope', 0)}</li>"
            f"<li>Expected employee count: {sections['pilot_scope'].get('expected_employee_count', 0)}</li>"
            f"<li>Batch status: {escape(str(sections['pilot_scope'].get('batch_status') or 'unknown'))}</li>"
            '</ul></section>',
            '<section><h2>Company and Roadmap Context</h2>'
            + _render_html_list(
                'Company context',
                list(company_and_roadmap.get('company_context_summary') or []),
            )
            + _render_html_list(
                'Roadmap context',
                list(company_and_roadmap.get('roadmap_context_summary') or []),
            )
            + _render_html_list(
                'Narrative summary',
                [
                    company_and_roadmap.get('executive_summary') or 'No summary available.',
                    company_and_roadmap.get('roadmap_priority_note') or 'No roadmap note available.',
                ],
            )
            + '</section>',
            f"<section><h2>Target Model Snapshot</h2><p>Blueprint run: <code>{escape(str(sections['target_model'].get('blueprint_run_uuid') or 'n/a'))}</code></p><p>Matrix run: <code>{escape(str(sections['target_model'].get('matrix_run_uuid') or 'n/a'))}</code></p><p>Action mix: {escape(_format_mapping_markdown(sections['target_model'].get('action_counts') or {}))}</p></section>",
            '<section><h2>Matrix Highlights</h2>'
            + _render_html_list('Top priority gaps', [_format_gap_markdown(item) for item in list(matrix_highlights.get('top_priority_gaps') or [])[:6]])
            + _render_html_list('Concentration risks', [_format_concentration_risk_markdown(item) for item in list(matrix_highlights.get('concentration_risks') or [])[:6]])
            + _render_html_list('Near-fit candidates', [_format_near_fit_markdown(item) for item in list(matrix_highlights.get('near_fit_candidates') or [])[:6]])
            + '</section>',
            '<section><h2>Priority Actions</h2>'
            + _render_html_list('', [_format_team_action_markdown(item) for item in list(recommendations.get('priority_actions') or [])])
            + _render_html_list('Hiring recommendations', list(recommendations.get('hiring_recommendations') or []))
            + _render_html_list('Development focus', list(recommendations.get('development_focus') or []))
            + _render_html_list('Risk notes', list(recommendations.get('single_points_of_failure') or []))
            + '</section>',
            f"<section><h2>Appendix</h2><p>{escape(str(sections['appendix'].get('confidence_note') or ''))}</p><p>Plan run: <code>{escape(str(sections['appendix'].get('plan_run_uuid') or 'n/a'))}</code></p><p>Generation batch: <code>{escape(str(sections['appendix'].get('generation_batch_uuid') or 'n/a'))}</code></p></section>",
        ]
    )


def _render_individual_html_body(export_payload: dict[str, Any]) -> str:
    sections = export_payload['sections']
    employee = export_payload.get('employee') or {}
    actions = list(sections['development_actions'].get('actions') or [])
    resources = [
        f"{item.get('skill_name_en', '')}: {item.get('course_placeholder', '')}"
        for item in list(sections['placeholder_resources'].get('resources') or [])
        if item.get('course_placeholder')
    ]
    return ''.join(
        [
            f"<h1>{escape(str(export_payload.get('title') or 'Individual Development Plan'))}</h1>",
            _render_lineage_block_html(export_payload),
            '<section><h2>Employee Summary</h2><ul>'
            f"<li>Employee: {escape(str(employee.get('full_name') or 'Unknown'))}</li>"
            f"<li>Current title: {escape(str(employee.get('current_title') or 'Unknown'))}</li>"
            f"<li>Current role fit: {escape(str(sections['employee_summary'].get('current_role_fit') or 'No fit summary available.'))}</li>"
            f"<li>Current role goal: {escape(str(sections['employee_summary'].get('current_role_goal') or 'n/a'))}</li>"
            f"<li>Mobility potential: {escape(str(sections['employee_summary'].get('mobility_potential') or 'n/a'))}</li>"
            '</ul></section>',
            '<section><h2>Target Role and Adjacent Context</h2>'
            + _render_html_list('Adjacent roles', list(sections['target_role_context'].get('adjacent_roles') or []))
            + f"<p>Roadmap alignment: {escape(str(sections['target_role_context'].get('roadmap_alignment') or 'No alignment note available.'))}</p>"
            + f"<p>Mobility note: {escape(str(sections['target_role_context'].get('mobility_note') or 'No mobility note available.'))}</p>"
            + '</section>',
            '<section><h2>Strongest Capabilities</h2>'
            + _render_html_list('Strengths', list(sections['strongest_capabilities'].get('strengths') or []))
            + '</section>',
            '<section><h2>Main Gaps</h2>'
            + _render_html_list('Priority gaps', list(sections['main_gaps'].get('priority_gaps') or []))
            + '</section>',
            '<section><h2>Development Actions</h2>'
            + _render_html_list('', [_format_individual_action_markdown(item) for item in actions])
            + _render_html_list('Placeholder resources', resources)
            + '</section>',
            f"<section><h2>Appendix</h2><p>{escape(str(sections['appendix'].get('confidence_note') or ''))}</p><p>Plan run: <code>{escape(str(sections['appendix'].get('plan_run_uuid') or 'n/a'))}</code></p><p>Generation batch: <code>{escape(str(sections['appendix'].get('generation_batch_uuid') or 'n/a'))}</code></p></section>",
        ]
    )


def _render_lineage_block_markdown(export_payload: dict[str, Any]) -> str:
    lineage = export_payload['lineage']
    return '\n'.join(
        [
            f"- Workspace: {export_payload['workspace'].get('name', '')} (`{export_payload['workspace'].get('slug', '')}`)",
            f"- Plan run: `{lineage.get('plan_run_uuid', '') or 'n/a'}`",
            f"- Blueprint run: `{lineage.get('blueprint_run_uuid', '') or 'n/a'}`",
            f"- Matrix run: `{lineage.get('matrix_run_uuid', '') or 'n/a'}`",
            f"- Generated: {lineage.get('generated_at', '') or 'n/a'}",
        ]
    )


def _render_lineage_block_html(export_payload: dict[str, Any]) -> str:
    lineage = export_payload['lineage']
    return (
        '<section class="meta">'
        f'<span class="pill">Workspace {escape(str(export_payload["workspace"].get("slug") or ""))}</span>'
        f'<span class="pill">Plan run {escape(str(lineage.get("plan_run_uuid") or "n/a"))}</span>'
        f'<span class="pill">Blueprint {escape(str(lineage.get("blueprint_run_uuid") or "n/a"))}</span>'
        f'<span class="pill">Matrix {escape(str(lineage.get("matrix_run_uuid") or "n/a"))}</span>'
        f'<span class="pill">Generated {escape(str(lineage.get("generated_at") or "n/a"))}</span>'
        '</section>'
    )


def _render_markdown_list(title: str, items: list[str]) -> str:
    cleaned = [str(item).strip() for item in items if str(item).strip()]
    lines: list[str] = []
    if title:
        lines.append(f"### {title}")
    if not cleaned:
        lines.append('- None noted.')
    else:
        lines.extend(f'- {item}' for item in cleaned)
    return '\n'.join(lines)


def _render_html_list(title: str, items: list[str]) -> str:
    cleaned = [str(item).strip() for item in items if str(item).strip()]
    header = f"<h3>{escape(title)}</h3>" if title else ''
    if not cleaned:
        return f'{header}<ul><li>None noted.</li></ul>'
    return header + '<ul>' + ''.join(f'<li>{escape(item)}</li>' for item in cleaned) + '</ul>'


def _format_mapping_markdown(values: dict[str, Any]) -> str:
    if not values:
        return 'None noted.'
    return ', '.join(f'{key}: {value}' for key, value in sorted(values.items()) if str(key).strip())


def _format_gap_markdown(item: dict[str, Any]) -> str:
    return (
        f"{item.get('role_name', '')} / {item.get('skill_name_en', item.get('skill_key', ''))}: "
        f"avg gap {item.get('average_gap', 0.0)}, priority {item.get('max_priority', 0)}"
    ).strip()


def _format_concentration_risk_markdown(item: dict[str, Any]) -> str:
    return (
        f"{item.get('role_name', '')} / {item.get('skill_name_en', item.get('skill_key', ''))}: "
        f"{item.get('ready_employee_count', 0)} ready employee(s)"
    ).strip()


def _format_near_fit_markdown(item: dict[str, Any]) -> str:
    return (
        f"{item.get('full_name', '')} is near-fit for {item.get('role_name', '')} "
        f"with weighted gap {item.get('weighted_gap', item.get('gap', 0.0))}"
    ).strip()


def _format_team_action_markdown(item: dict[str, Any]) -> str:
    label = str(item.get('action') or '').strip()
    why_now = str(item.get('why_now') or item.get('why') or '').strip()
    owner_role = str(item.get('owner_role') or '').strip()
    return f"{label} [{item.get('action_type', '')}] Owner: {owner_role or 'n/a'}. Why now: {why_now or 'n/a'}"


def _format_individual_action_markdown(item: dict[str, Any]) -> str:
    label = str(item.get('action') or '').strip()
    expected = str(item.get('expected_outcome') or '').strip()
    horizon = str(item.get('time_horizon') or '').strip()
    return f"{label} ({horizon or 'n/a'}). Expected outcome: {expected or 'n/a'}"


def _serialize_workspace(run: DevelopmentPlanRun) -> dict[str, Any]:
    return {
        'uuid': _stringify_uuid(getattr(run.workspace, 'uuid', None)),
        'name': str(getattr(run.workspace, 'name', '') or ''),
        'slug': str(getattr(run.workspace, 'slug', '') or ''),
    }


def _serialize_lineage(
    run: DevelopmentPlanRun,
    *,
    generated_at: str = '',
    frozen_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot_lineage = dict((frozen_snapshot or {}).get('lineage') or {})
    run_generated_at = generated_at or snapshot_lineage.get('generated_at') or ''
    if not run_generated_at:
        effective = run.completed_at or run.updated_at or run.created_at
        run_generated_at = effective.isoformat() if effective else ''
    return {
        'plan_run_uuid': str(snapshot_lineage.get('plan_run_uuid') or _stringify_uuid(run.uuid)),
        'blueprint_run_uuid': str(snapshot_lineage.get('blueprint_run_uuid') or _stringify_uuid(getattr(run.blueprint_run, 'uuid', None))),
        'matrix_run_uuid': str(snapshot_lineage.get('matrix_run_uuid') or _stringify_uuid(getattr(run.matrix_run, 'uuid', None))),
        'generation_batch_uuid': str(snapshot_lineage.get('generation_batch_uuid') or _stringify_uuid(run.generation_batch_uuid)),
        'generated_at': run_generated_at,
        'plan_completed_at': str(snapshot_lineage.get('plan_completed_at') or ''),
    }


def _stringify_uuid(value: Any) -> str:
    return str(value).strip() if value else ''


def _build_export_source(
    run: DevelopmentPlanRun,
    *,
    frozen_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot = frozen_snapshot or {}
    workspace = dict(snapshot.get('workspace') or _serialize_workspace(run))
    lineage = dict(snapshot.get('lineage') or {})
    employee = dict(snapshot.get('employee') or {
        'uuid': _stringify_uuid(getattr(run.employee, 'uuid', None)),
        'full_name': str(getattr(run.employee, 'full_name', '') or ''),
        'current_title': str(getattr(run.employee, 'current_title', '') or ''),
    })
    return {
        'title': str(snapshot.get('title') or run.title),
        'status': str(snapshot.get('status') or run.status),
        'workspace': workspace,
        'employee': employee,
        'lineage': {
            'plan_run_uuid': str(lineage.get('plan_run_uuid') or _stringify_uuid(run.uuid)),
            'blueprint_run_uuid': str(lineage.get('blueprint_run_uuid') or _stringify_uuid(getattr(run.blueprint_run, 'uuid', None))),
            'matrix_run_uuid': str(lineage.get('matrix_run_uuid') or _stringify_uuid(getattr(run.matrix_run, 'uuid', None))),
            'generation_batch_uuid': str(lineage.get('generation_batch_uuid') or _stringify_uuid(run.generation_batch_uuid)),
            'plan_completed_at': str(lineage.get('plan_completed_at') or ''),
        },
        'company_context': _coerce_export_context_value(
            snapshot.get('company_context', getattr(run.blueprint_run, 'company_context', ''))
        ),
        'roadmap_context': _coerce_export_context_value(
            snapshot.get('roadmap_context', getattr(run.blueprint_run, 'roadmap_context', ''))
        ),
        'summary': dict(snapshot.get('summary') or run.summary or {}),
        'plan': dict(snapshot.get('plan_payload') or run.plan_payload or {}),
        'recommendation': dict(snapshot.get('recommendation_payload') or run.recommendation_payload or {}),
    }


def _coerce_export_context_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ''
        if text[:1] in '{[':
            for loader in (json.loads, ast.literal_eval):
                try:
                    parsed = loader(text)
                except Exception:
                    continue
                if isinstance(parsed, tuple):
                    return list(parsed)
                if isinstance(parsed, (dict, list)):
                    return parsed
        return text
    return value


def _format_company_context_lines(value: Any) -> list[str]:
    normalized = _coerce_export_context_value(value)
    if isinstance(normalized, dict):
        preferred_keys = [
            ('company_name', 'Company name'),
            ('what_company_does', 'What the company does'),
            ('why_skills_improvement_now', 'Why skills improvement now'),
            ('products', 'Products'),
            ('customers', 'Customers'),
            ('markets', 'Markets'),
            ('locations', 'Locations'),
            ('current_tech_stack', 'Current tech stack'),
            ('planned_tech_stack', 'Planned tech stack'),
            ('missing_information', 'Missing information'),
        ]
        lines: list[str] = []
        seen_keys: set[str] = set()
        for key, label in preferred_keys:
            text = _format_context_value(normalized.get(key))
            if text:
                lines.append(f'{label}: {text}')
                seen_keys.add(key)
        for key, raw_value in normalized.items():
            if key in seen_keys:
                continue
            text = _format_context_value(raw_value)
            if text:
                lines.append(f'{_humanize_context_key(key)}: {text}')
        return lines or ['No company context available.']
    if isinstance(normalized, list):
        lines = [_format_context_value(item) for item in normalized]
        cleaned = [item for item in lines if item]
        return cleaned or ['No company context available.']
    text = _format_context_value(normalized)
    return [text] if text else ['No company context available.']


def _format_roadmap_context_lines(value: Any) -> list[str]:
    normalized = _coerce_export_context_value(value)
    if isinstance(normalized, dict):
        normalized = [normalized]
    if isinstance(normalized, list):
        lines: list[str] = []
        for item in normalized:
            if isinstance(item, dict):
                lines.append(_format_roadmap_item(item))
            else:
                text = _format_context_value(item)
                if text:
                    lines.append(text)
        cleaned = [item for item in lines if item]
        return cleaned or ['No roadmap context available.']
    text = _format_context_value(normalized)
    return [text] if text else ['No roadmap context available.']


def _format_roadmap_item(item: dict[str, Any]) -> str:
    title = str(item.get('title') or item.get('initiative') or item.get('initiative_id') or 'Initiative').strip()
    summary = str(item.get('summary') or '').strip()
    time_horizon = str(item.get('time_horizon') or item.get('timing') or '').strip()
    criticality = str(item.get('criticality') or '').strip()
    desired_market_outcome = str(item.get('desired_market_outcome') or item.get('expected_outcome') or '').strip()
    functions_required = _format_context_value(item.get('functions_required'))
    tech_stack = _format_context_value(item.get('tech_stack'))
    ambiguities = _format_context_value(item.get('ambiguities'))

    label = title
    if time_horizon:
        label = f'{label} ({time_horizon})'

    detail_parts = [
        summary,
        f'Criticality: {criticality}' if criticality else '',
        f'Desired outcome: {desired_market_outcome}' if desired_market_outcome else '',
        f'Functions required: {functions_required}' if functions_required else '',
        f'Tech stack: {tech_stack}' if tech_stack else '',
        f'Ambiguities: {ambiguities}' if ambiguities else '',
    ]
    details = '; '.join(part for part in detail_parts if part)
    return f'{label}: {details}' if details else label


def _format_context_value(value: Any) -> str:
    normalized = _coerce_export_context_value(value)
    if isinstance(normalized, dict):
        parts = []
        for key, item in normalized.items():
            text = _format_context_value(item)
            if text:
                parts.append(f'{_humanize_context_key(key)}: {text}')
        return '; '.join(parts)
    if isinstance(normalized, list):
        cleaned = [_format_context_value(item) for item in normalized]
        return ', '.join(item for item in cleaned if item)
    if isinstance(normalized, bool):
        return 'Yes' if normalized else 'No'
    if normalized is None:
        return ''
    return str(normalized).strip()


def _humanize_context_key(value: str) -> str:
    words = str(value or '').replace('-', ' ').replace('_', ' ').split()
    return ' '.join(word.capitalize() for word in words)
