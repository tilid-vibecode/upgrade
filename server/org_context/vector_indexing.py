import logging
import hashlib
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from company_intake.models import WorkspaceSourceKind
from server.embedding_manager import get_embedding_manager_sync
from server.qdrant_manager import get_qdrant_manager_sync

from .models import EmployeeCVProfile, EmployeeSkillEvidence, ParsedSource

logger = logging.getLogger(__name__)

VECTOR_INDEX_VERSION = 'stage1.1-v1'
CV_EVIDENCE_INDEX_VERSION = 'stage6-v1'
SELF_ASSESSMENT_INDEX_VERSION = 'stage7-v1'
INDEXABLE_SOURCE_KINDS = {
    WorkspaceSourceKind.ROADMAP,
    WorkspaceSourceKind.STRATEGY,
    WorkspaceSourceKind.JOB_DESCRIPTION,
    WorkspaceSourceKind.EXISTING_MATRIX,
    WorkspaceSourceKind.OTHER,
}
CV_EVIDENCE_DOC_TYPES = {
    'cv_skill_evidence',
    'cv_role_history',
    'cv_achievement',
    'cv_leadership_signal',
    'cv_domain_experience',
}
SELF_ASSESSMENT_EVIDENCE_DOC_TYPES = {
    'self_assessment_skill_evidence',
    'self_assessment_hidden_skill',
    'self_assessment_aspiration',
    'self_assessment_example',
}
_DOC_TYPE_BY_SOURCE_KIND = {
    WorkspaceSourceKind.ROADMAP: 'roadmap_context',
    WorkspaceSourceKind.STRATEGY: 'strategy_context',
    WorkspaceSourceKind.JOB_DESCRIPTION: 'role_reference',
    WorkspaceSourceKind.EXISTING_MATRIX: 'role_reference',
    WorkspaceSourceKind.OTHER: 'reference_material',
}
_CHUNK_FAMILY_BY_SOURCE_KIND = {
    WorkspaceSourceKind.ROADMAP: 'roadmap_context',
    WorkspaceSourceKind.STRATEGY: 'strategy_context',
    WorkspaceSourceKind.JOB_DESCRIPTION: 'role_reference',
    WorkspaceSourceKind.EXISTING_MATRIX: 'existing_matrix',
    WorkspaceSourceKind.OTHER: 'reference_material',
}


def should_index_source_kind(source_kind: str) -> bool:
    return source_kind in INDEXABLE_SOURCE_KINDS


def build_chunk_document_id(*, workspace_uuid: str, source_uuid: str, chunk_index: int) -> str:
    return f'workspace:{workspace_uuid}:source:{source_uuid}:chunk:{chunk_index}'


def build_cv_evidence_document_id(
    *,
    workspace_uuid: str,
    source_uuid: str,
    employee_uuid: str,
    doc_type: str,
    generation_id: str,
    item_key: str,
) -> str:
    return (
        f'workspace:{workspace_uuid}:source:{source_uuid}:employee:{employee_uuid}:'
        f'cv:{doc_type}:generation:{generation_id}:{item_key}'
    )


def build_self_assessment_document_id(
    *,
    workspace_uuid: str,
    employee_uuid: str,
    cycle_uuid: str,
    pack_uuid: str,
    doc_type: str,
    generation_id: str,
    item_key: str,
) -> str:
    return (
        f'workspace:{workspace_uuid}:employee:{employee_uuid}:cycle:{cycle_uuid}:'
        f'pack:{pack_uuid}:self-assessment:{doc_type}:generation:{generation_id}:{item_key}'
    )


def _doc_type_for_source_kind(source_kind: str) -> str:
    return _DOC_TYPE_BY_SOURCE_KIND.get(source_kind, 'reference_material')


def _chunk_family_for_source_kind(source_kind: str) -> str:
    return _CHUNK_FAMILY_BY_SOURCE_KIND.get(source_kind, 'reference_material')


def _embedding_model_name(embedding_manager) -> str:
    try:
        return embedding_manager.model_name
    except Exception:
        return 'unknown'


def _collect_vector_dimensions(embeddings: list[list[float]]) -> list[int]:
    return sorted({len(vector) for vector in embeddings if vector is not None})


def _build_cv_evidence_text(prefix: str, parts: list[str], *, min_body_chars: int = 24) -> str:
    normalized_parts = [str(part or '').strip() for part in parts if str(part or '').strip()]
    body = '\n'.join(normalized_parts).strip()
    if not body or len(body) < min_body_chars:
        return ''
    if not normalized_parts:
        return ''
    return '\n'.join([prefix.strip(), *normalized_parts]).strip()


def _fingerprint_cv_item(*parts: Any) -> str:
    normalized = '||'.join(str(part or '').strip().casefold() for part in parts if str(part or '').strip())
    return hashlib.sha1(normalized.encode('utf-8')).hexdigest()[:16]


def _append_cv_document(
    documents: list[dict[str, Any]],
    *,
    item_key: str,
    section_index: int,
    workspace_uuid: str,
    workspace_slug: str,
    source,
    employee,
    parsed_source_uuid: str,
    doc_type: str,
    skill_key: str,
    evidence_category: str,
    section_heading: str,
    text: str,
    confidence: float,
    language_code: str,
    current_title: str,
    role_family: str,
    generation_id: str,
    evidence_row_uuid: str = '',
) -> None:
    if not text.strip():
        return
    source_uuid = str(source.uuid)
    employee_uuid = str(employee.uuid)
    documents.append(
        {
            'id': build_cv_evidence_document_id(
                workspace_uuid=workspace_uuid,
                source_uuid=source_uuid,
                employee_uuid=employee_uuid,
                doc_type=doc_type,
                generation_id=generation_id,
                item_key=item_key,
            ),
            'payload': {
                'org_id': workspace_uuid,
                'workspace_slug': workspace_slug,
                'doc_type': doc_type,
                'source_type': source.source_kind,
                'source_kind': source.source_kind,
                'source_uuid': source_uuid,
                'parsed_source_uuid': parsed_source_uuid,
                'employee_uuid': employee_uuid,
                'skill_key': skill_key,
                'evidence_row_uuid': evidence_row_uuid,
                'evidence_category': evidence_category,
                'generation_id': generation_id,
                'node_id': f'{source_uuid}:{doc_type}:{item_key}',
                'chunk_index': section_index,
                'chunk_family': doc_type,
                'language_code': language_code,
                'source_title': source.title or source.source_kind,
                'source_transport': source.transport,
                'employee_name': employee.full_name,
                'current_title': current_title,
                'role_family': role_family,
                'chunk_text': text,
                'char_count': len(text),
                'section_index': section_index,
                'section_heading': section_heading,
                'page_number': None,
                'confidence': confidence,
                'embedding_model': '',
                'index_version': CV_EVIDENCE_INDEX_VERSION,
            },
        }
    )


def _build_cv_evidence_documents(profile: EmployeeCVProfile) -> list[dict[str, Any]]:
    return _build_cv_evidence_documents_for_generation(profile, generation_id='')


def _build_cv_evidence_documents_for_generation(
    profile: EmployeeCVProfile,
    *,
    generation_id: str,
) -> list[dict[str, Any]]:
    if profile.employee is None:
        return []

    extracted = dict(profile.extracted_payload or {})
    workspace = profile.workspace
    source = profile.source
    employee = profile.employee
    parsed_source_uuid = str(getattr(getattr(source, 'parsed_source', None), 'uuid', '') or '')
    current_title = profile.current_role or employee.current_title or ''
    language_code = profile.language_code or source.language_code or ''
    role_family = profile.role_family or ''
    workspace_uuid = str(workspace.uuid)

    documents: list[dict[str, Any]] = []
    section_index = 1

    evidence_rows = list(
        EmployeeSkillEvidence.objects.select_related('skill').filter(
            workspace=workspace,
            employee=employee,
            source=source,
            source_kind=WorkspaceSourceKind.EMPLOYEE_CV,
            weight__gt=0,
        ).order_by('-confidence', 'skill__display_name_en')
    )
    for evidence_row in evidence_rows:
        skill_name = evidence_row.skill.display_name_en or evidence_row.skill.canonical_key
        evidence = str(evidence_row.evidence_text or '').strip()
        text = _build_cv_evidence_text(
            f'{employee.full_name} skill evidence: {skill_name or "Unspecified skill"}',
            [
                f'Category: {evidence_row.metadata.get("evidence_category", "")}'
                if (evidence_row.metadata or {}).get('evidence_category') else '',
                f'Current role: {current_title}' if current_title else '',
                evidence,
            ],
        )
        _append_cv_document(
            documents,
            item_key=str(evidence_row.uuid),
            section_index=section_index,
            workspace_uuid=workspace_uuid,
            workspace_slug=workspace.slug,
            source=source,
            employee=employee,
            parsed_source_uuid=parsed_source_uuid,
            doc_type='cv_skill_evidence',
            skill_key=evidence_row.skill.canonical_key,
            evidence_category=str((evidence_row.metadata or {}).get('evidence_category') or 'skill'),
            section_heading=skill_name or 'Skill evidence',
            text=text,
            confidence=float(evidence_row.confidence or 0.0),
            language_code=language_code,
            current_title=current_title,
            role_family=role_family,
            generation_id=generation_id,
            evidence_row_uuid=str(evidence_row.uuid),
        )
        section_index += 1

    for history in extracted.get('role_history', []):
        role_title = str(history.get('role_title') or '').strip()
        company_name = str(history.get('company_name') or '').strip()
        period = ' - '.join(
            [value for value in [str(history.get('start_date') or '').strip(), str(history.get('end_date') or '').strip()] if value]
        )
        text = _build_cv_evidence_text(
            f'{employee.full_name} role history: {role_title or "Role"}',
            [
                f'Company: {company_name}' if company_name else '',
                f'Period: {period}' if period else '',
                'Responsibilities: ' + '; '.join(history.get('responsibilities') or []) if history.get('responsibilities') else '',
                'Achievements: ' + '; '.join(history.get('achievements') or []) if history.get('achievements') else '',
                'Domains: ' + '; '.join(history.get('domains') or []) if history.get('domains') else '',
                str(history.get('evidence') or ''),
            ],
        )
        _append_cv_document(
            documents,
            item_key=_fingerprint_cv_item(
                'role_history',
                company_name,
                role_title,
                period,
                ';'.join(history.get('responsibilities') or []),
                ';'.join(history.get('achievements') or []),
                ';'.join(history.get('domains') or []),
                ';'.join(history.get('leadership_signals') or []),
                history.get('evidence', ''),
            ),
            section_index=section_index,
            workspace_uuid=workspace_uuid,
            workspace_slug=workspace.slug,
            source=source,
            employee=employee,
            parsed_source_uuid=parsed_source_uuid,
            doc_type='cv_role_history',
            skill_key='',
            evidence_category='role_history',
            section_heading=role_title or company_name or 'Role history',
            text=text,
            confidence=float(history.get('confidence_score') or 0.0),
            language_code=language_code,
            current_title=current_title,
            role_family=role_family,
            generation_id=generation_id,
        )
        section_index += 1

    for achievement in extracted.get('achievements', []):
        if isinstance(achievement, dict):
            label = str(achievement.get('summary') or achievement.get('achievement') or '').strip()
            evidence_text = str(achievement.get('evidence') or '').strip()
            confidence = float(achievement.get('confidence_score') or 0.0)
        else:
            label = str(achievement or '').strip()
            evidence_text = ''
            confidence = 0.6
        if not label:
            continue
        text = _build_cv_evidence_text(
            f'{employee.full_name} achievement',
            [label, evidence_text],
        )
        _append_cv_document(
            documents,
            item_key=_fingerprint_cv_item('achievement', label, evidence_text),
            section_index=section_index,
            workspace_uuid=workspace_uuid,
            workspace_slug=workspace.slug,
            source=source,
            employee=employee,
            parsed_source_uuid=parsed_source_uuid,
            doc_type='cv_achievement',
            skill_key='',
            evidence_category='achievement',
            section_heading='Achievement',
            text=text,
            confidence=confidence,
            language_code=language_code,
            current_title=current_title,
            role_family=role_family,
            generation_id=generation_id,
        )
        section_index += 1

    for signal in extracted.get('leadership_signals', []):
        if isinstance(signal, dict):
            label = str(signal.get('signal') or signal.get('summary') or '').strip()
            evidence_text = str(signal.get('evidence') or '').strip()
            confidence = float(signal.get('confidence_score') or 0.0)
        else:
            label = str(signal or '').strip()
            evidence_text = ''
            confidence = 0.6
        if not label:
            continue
        text = _build_cv_evidence_text(
            f'{employee.full_name} leadership signal',
            [label, evidence_text],
        )
        _append_cv_document(
            documents,
            item_key=_fingerprint_cv_item('leadership', label, evidence_text),
            section_index=section_index,
            workspace_uuid=workspace_uuid,
            workspace_slug=workspace.slug,
            source=source,
            employee=employee,
            parsed_source_uuid=parsed_source_uuid,
            doc_type='cv_leadership_signal',
            skill_key='',
            evidence_category='leadership',
            section_heading='Leadership signal',
            text=text,
            confidence=confidence,
            language_code=language_code,
            current_title=current_title,
            role_family=role_family,
            generation_id=generation_id,
        )
        section_index += 1

    for domain in extracted.get('domain_experience', []):
        if isinstance(domain, dict):
            label = str(domain.get('domain') or domain.get('summary') or '').strip()
            evidence_text = str(domain.get('evidence') or '').strip()
            confidence = float(domain.get('confidence_score') or 0.0)
        else:
            label = str(domain or '').strip()
            evidence_text = ''
            confidence = 0.6
        if not label:
            continue
        text = _build_cv_evidence_text(
            f'{employee.full_name} domain experience',
            [label, evidence_text],
        )
        _append_cv_document(
            documents,
            item_key=_fingerprint_cv_item('domain', label, evidence_text),
            section_index=section_index,
            workspace_uuid=workspace_uuid,
            workspace_slug=workspace.slug,
            source=source,
            employee=employee,
            parsed_source_uuid=parsed_source_uuid,
            doc_type='cv_domain_experience',
            skill_key='',
            evidence_category='domain',
            section_heading='Domain experience',
            text=text,
            confidence=confidence,
            language_code=language_code,
            current_title=current_title,
            role_family=role_family,
            generation_id=generation_id,
        )
        section_index += 1

    return documents


def _truncate_section(section: str, limit: int) -> str:
    if len(section) <= limit:
        return section
    if limit <= 1:
        return section[:limit]
    trimmed = section[: limit - 1].rsplit('\n', 1)[0].rstrip()
    if len(trimmed) < max(32, int(limit * 0.6)):
        trimmed = section[: limit - 1].rsplit(' ', 1)[0].rstrip()
    return f'{trimmed or section[: limit - 1]}…'


def index_parsed_source_chunks_sync(parsed_source_pk) -> dict[str, Any]:
    parsed_source = ParsedSource.objects.select_related(
        'workspace',
        'source',
        'source__media_file',
    ).get(pk=parsed_source_pk)
    source = parsed_source.source
    workspace = parsed_source.workspace
    workspace_uuid = str(workspace.uuid)
    source_uuid = str(source.uuid)
    doc_type = _doc_type_for_source_kind(source.source_kind)
    chunk_family = _chunk_family_for_source_kind(source.source_kind)
    indexed_at = datetime.now(timezone.utc).isoformat()

    if not should_index_source_kind(source.source_kind):
        return {
            'status': 'skipped',
            'reason': 'source_kind_not_indexed',
            'source_kind': source.source_kind,
            'index_version': VECTOR_INDEX_VERSION,
            'indexed_at': indexed_at,
        }

    qdrant = get_qdrant_manager_sync()
    embedding_manager = get_embedding_manager_sync()
    qdrant.delete_by_filters_sync(
        org_id=workspace_uuid,
        additional_filters={'source_uuid': source_uuid},
    )

    chunks = list(parsed_source.chunks.order_by('chunk_index'))
    chunk_rows = [
        chunk
        for chunk in chunks
        if (chunk.text or '').strip()
    ]
    if not chunk_rows:
        return {
            'status': 'skipped',
            'reason': 'no_text_chunks',
            'source_kind': source.source_kind,
            'doc_type': doc_type,
            'index_version': VECTOR_INDEX_VERSION,
            'indexed_at': indexed_at,
        }

    chunk_texts = [chunk.text for chunk in chunk_rows]
    embeddings = embedding_manager.embed_batch_sync(chunk_texts)
    embedding_model = _embedding_model_name(embedding_manager)
    vector_dimensions = _collect_vector_dimensions(embeddings)
    expected_dimensions = qdrant.vector_size
    configured_dimensions = getattr(embedding_manager, 'dimensions', expected_dimensions)
    if len(embeddings) != len(chunk_rows):
        logger.warning(
            'Embedding count mismatch for parsed source %s: expected %d chunks, got %d embeddings.',
            parsed_source.uuid,
            len(chunk_rows),
            len(embeddings),
        )
        return {
            'status': 'failed',
            'reason': 'embedding_count_mismatch',
            'source_kind': source.source_kind,
            'doc_type': doc_type,
            'expected_chunk_count': len(chunk_rows),
            'embedding_count': len(embeddings),
            'embedding_model': embedding_model,
            'index_version': VECTOR_INDEX_VERSION,
            'indexed_at': indexed_at,
        }
    if len(vector_dimensions) != 1 or vector_dimensions[0] != expected_dimensions:
        logger.warning(
            'Embedding dimension mismatch for parsed source %s: got %s, expected %d.',
            parsed_source.uuid,
            vector_dimensions,
            expected_dimensions,
        )
        return {
            'status': 'failed',
            'reason': 'embedding_dimension_mismatch',
            'source_kind': source.source_kind,
            'doc_type': doc_type,
            'expected_vector_size': expected_dimensions,
            'configured_embedding_dimensions': configured_dimensions,
            'actual_vector_sizes': vector_dimensions,
            'eligible_chunk_count': len(chunk_rows),
            'indexed_chunk_count': 0,
            'embedding_model': embedding_model,
            'index_version': VECTOR_INDEX_VERSION,
            'indexed_at': indexed_at,
        }
    language_code = (
        source.language_code
        or (parsed_source.metadata or {}).get('language_code')
        or ''
    )
    metadata = parsed_source.metadata or {}
    source_origin = (metadata.get('source') or {}).get('origin') or metadata.get('source_origin') or {}

    documents = []
    for chunk, vector in zip(chunk_rows, embeddings):
        chunk_metadata = dict(chunk.metadata or {})
        payload = {
            'org_id': workspace_uuid,
            'workspace_slug': workspace.slug,
            'doc_type': doc_type,
            'source_type': source.source_kind,
            'source_kind': source.source_kind,
            'source_uuid': source_uuid,
            'parsed_source_uuid': str(parsed_source.uuid),
            'node_id': f'{source_uuid}:{chunk.chunk_index}',
            'chunk_index': chunk.chunk_index,
            'chunk_family': chunk_metadata.get('chunk_family') or chunk_family,
            'language_code': language_code or chunk_metadata.get('language_code', ''),
            'source_title': source.title or source.source_kind,
            'source_transport': source.transport,
            'source_origin': source_origin,
            'chunk_text': chunk.text,
            'char_count': chunk.char_count,
            'section_index': chunk_metadata.get('section_index'),
            'section_heading': chunk_metadata.get('section_heading', ''),
            'page_number': chunk_metadata.get('page_number'),
            'embedding_model': embedding_model,
            'index_version': VECTOR_INDEX_VERSION,
        }
        documents.append(
            {
                'id': build_chunk_document_id(
                    workspace_uuid=workspace_uuid,
                    source_uuid=source_uuid,
                    chunk_index=chunk.chunk_index,
                ),
                'vector': vector,
                'payload': payload,
            }
        )

    indexed_count = qdrant.upsert_documents_batch_sync(documents)
    status = 'indexed' if indexed_count == len(documents) else 'partial'
    return {
        'status': status,
        'source_kind': source.source_kind,
        'doc_type': doc_type,
        'collection': qdrant.collection_name,
        'eligible_chunk_count': len(documents),
        'indexed_chunk_count': indexed_count,
        'embedding_model': embedding_model,
        'index_version': VECTOR_INDEX_VERSION,
        'indexed_at': indexed_at,
    }


def clear_employee_cv_evidence_index_sync(
    *,
    workspace_uuid: str,
    source_uuid: str,
    generation_id: str | None = None,
) -> bool:
    try:
        qdrant = get_qdrant_manager_sync()
        additional_filters = {
            'source_uuid': source_uuid,
            'source_kind': WorkspaceSourceKind.EMPLOYEE_CV,
        }
        if generation_id:
            additional_filters['generation_id'] = generation_id
        return qdrant.delete_by_filters_sync(
            org_id=workspace_uuid,
            doc_types=sorted(CV_EVIDENCE_DOC_TYPES),
            additional_filters=additional_filters,
        )
    except Exception as exc:
        logger.warning(
            'Failed to clear employee CV evidence index for source %s: %s',
            source_uuid,
            exc,
            exc_info=True,
        )
        return False


def index_employee_cv_profile_sync(cv_profile_pk) -> dict[str, Any]:
    profile = EmployeeCVProfile.objects.select_related(
        'workspace',
        'source',
        'source__parsed_source',
        'employee',
    ).get(pk=cv_profile_pk)
    workspace = profile.workspace
    source = profile.source
    workspace_uuid = str(workspace.uuid)
    source_uuid = str(source.uuid)
    indexed_at = datetime.now(timezone.utc).isoformat()
    previous_generation_id = str(profile.active_vector_generation_id or '').strip()

    if profile.employee is None or profile.status != EmployeeCVProfile.Status.MATCHED:
        clear_employee_cv_evidence_index_sync(
            workspace_uuid=workspace_uuid,
            source_uuid=source_uuid,
        )
        if profile.active_vector_generation_id:
            profile.active_vector_generation_id = ''
            profile.save(update_fields=['active_vector_generation_id', 'updated_at'])
        return {
            'status': 'skipped',
            'reason': 'profile_not_matched',
            'source_kind': source.source_kind,
            'index_version': CV_EVIDENCE_INDEX_VERSION,
            'indexed_at': indexed_at,
            'active_generation_id': '',
        }

    generation_id = str(uuid4())
    documents = _build_cv_evidence_documents_for_generation(
        profile,
        generation_id=generation_id,
    )
    if not documents:
        clear_employee_cv_evidence_index_sync(
            workspace_uuid=workspace_uuid,
            source_uuid=source_uuid,
        )
        if profile.active_vector_generation_id:
            profile.active_vector_generation_id = ''
            profile.save(update_fields=['active_vector_generation_id', 'updated_at'])
        return {
            'status': 'skipped',
            'reason': 'no_structured_cv_evidence',
            'source_kind': source.source_kind,
            'index_version': CV_EVIDENCE_INDEX_VERSION,
            'indexed_at': indexed_at,
            'active_generation_id': '',
        }

    qdrant = get_qdrant_manager_sync()
    embedding_manager = get_embedding_manager_sync()
    embeddings = embedding_manager.embed_batch_sync([doc['payload']['chunk_text'] for doc in documents])
    embedding_model = _embedding_model_name(embedding_manager)
    vector_dimensions = _collect_vector_dimensions(embeddings)
    expected_dimensions = qdrant.vector_size
    configured_dimensions = getattr(embedding_manager, 'dimensions', expected_dimensions)

    if len(embeddings) != len(documents):
        return {
            'status': 'failed',
            'reason': 'embedding_count_mismatch',
            'expected_document_count': len(documents),
            'embedding_count': len(embeddings),
            'embedding_model': embedding_model,
            'index_version': CV_EVIDENCE_INDEX_VERSION,
            'indexed_at': indexed_at,
            'active_generation_id': previous_generation_id,
        }
    if len(vector_dimensions) != 1 or vector_dimensions[0] != expected_dimensions:
        return {
            'status': 'failed',
            'reason': 'embedding_dimension_mismatch',
            'expected_vector_size': expected_dimensions,
            'configured_embedding_dimensions': configured_dimensions,
            'actual_vector_sizes': vector_dimensions,
            'eligible_document_count': len(documents),
            'indexed_document_count': 0,
            'embedding_model': embedding_model,
            'index_version': CV_EVIDENCE_INDEX_VERSION,
            'indexed_at': indexed_at,
            'active_generation_id': previous_generation_id,
        }

    for document, vector in zip(documents, embeddings):
        document['vector'] = vector
        document['payload']['embedding_model'] = embedding_model

    indexed_count = qdrant.upsert_documents_batch_sync(documents)
    status = 'indexed' if indexed_count == len(documents) else 'partial'
    if status != 'indexed':
        clear_employee_cv_evidence_index_sync(
            workspace_uuid=workspace_uuid,
            source_uuid=source_uuid,
            generation_id=generation_id,
        )
        return {
            'status': status,
            'source_kind': source.source_kind,
            'collection': qdrant.collection_name,
            'eligible_document_count': len(documents),
            'indexed_document_count': indexed_count,
            'embedding_model': embedding_model,
            'index_version': CV_EVIDENCE_INDEX_VERSION,
            'indexed_at': indexed_at,
            'attempted_generation_id': generation_id,
            'active_generation_id': previous_generation_id,
        }

    if previous_generation_id and previous_generation_id != generation_id:
        clear_employee_cv_evidence_index_sync(
            workspace_uuid=workspace_uuid,
            source_uuid=source_uuid,
            generation_id=previous_generation_id,
        )
    if profile.active_vector_generation_id != generation_id:
        profile.active_vector_generation_id = generation_id
        profile.save(update_fields=['active_vector_generation_id', 'updated_at'])
    return {
        'status': status,
        'source_kind': source.source_kind,
        'collection': qdrant.collection_name,
        'eligible_document_count': len(documents),
        'indexed_document_count': indexed_count,
        'embedding_model': embedding_model,
        'index_version': CV_EVIDENCE_INDEX_VERSION,
        'indexed_at': indexed_at,
        'attempted_generation_id': generation_id,
        'active_generation_id': generation_id,
    }


def clear_employee_assessment_pack_index_sync(
    *,
    workspace_uuid: str,
    pack_uuid: str,
    generation_id: str | None = None,
) -> bool:
    try:
        qdrant = get_qdrant_manager_sync()
        additional_filters = {
            'source_kind': ['self_assessment'],
            'pack_uuid': pack_uuid,
        }
        if generation_id:
            additional_filters['generation_id'] = generation_id
        return qdrant.delete_by_filters_sync(
            org_id=workspace_uuid,
            doc_types=sorted(SELF_ASSESSMENT_EVIDENCE_DOC_TYPES),
            additional_filters=additional_filters,
        )
    except Exception as exc:
        logger.warning(
            'Failed to clear assessment pack index for %s: %s',
            pack_uuid,
            exc,
            exc_info=True,
        )
        return False


def _build_self_assessment_documents_for_generation(pack, *, generation_id: str) -> list[dict[str, Any]]:
    response_payload = dict(pack.response_payload or {})
    workspace = pack.cycle.workspace
    employee = pack.employee
    workspace_uuid = str(workspace.uuid)
    cycle_uuid = str(pack.cycle.uuid)
    pack_uuid = str(pack.uuid)
    employee_uuid = str(employee.uuid)
    blueprint_run_uuid = str(getattr(pack.cycle.blueprint_run, 'uuid', '') or '')
    documents: list[dict[str, Any]] = []
    section_index = 1

    def append_document(
        *,
        question_id: str,
        item_key: str,
        doc_type: str,
        skill_key: str,
        evidence_category: str,
        section_heading: str,
        text: str,
        confidence: float,
        evidence_row_uuid: str = '',
    ) -> None:
        if not (text or '').strip():
            return
        documents.append(
            {
                'id': build_self_assessment_document_id(
                    workspace_uuid=workspace_uuid,
                    employee_uuid=employee_uuid,
                    cycle_uuid=cycle_uuid,
                    pack_uuid=pack_uuid,
                    doc_type=doc_type,
                    generation_id=generation_id,
                    item_key=item_key,
                ),
                'payload': {
                    'org_id': workspace_uuid,
                    'workspace_slug': workspace.slug,
                    'doc_type': doc_type,
                    'source_type': 'self_assessment',
                    'source_kind': 'self_assessment',
                    'source_uuid': '',
                    'parsed_source_uuid': '',
                    'employee_uuid': employee_uuid,
                    'skill_key': skill_key,
                    'evidence_row_uuid': evidence_row_uuid,
                    'evidence_category': evidence_category,
                    'generation_id': generation_id,
                    'blueprint_run_uuid': blueprint_run_uuid,
                    'cycle_uuid': cycle_uuid,
                    'pack_uuid': pack_uuid,
                    'question_id': question_id,
                    'node_id': f'{pack_uuid}:{question_id}:{item_key}',
                    'chunk_index': section_index,
                    'chunk_family': doc_type,
                    'language_code': '',
                    'source_title': pack.title or 'Self-assessment',
                    'source_transport': 'inline',
                    'employee_name': employee.full_name,
                    'current_title': employee.current_title,
                    'role_family': '',
                    'chunk_text': text,
                    'char_count': len(text),
                    'section_index': section_index,
                    'section_heading': section_heading,
                    'page_number': None,
                    'confidence': confidence,
                    'embedding_model': '',
                    'index_version': SELF_ASSESSMENT_INDEX_VERSION,
                },
            }
        )

    evidence_rows = list(
        EmployeeSkillEvidence.objects.select_related('skill').filter(
            workspace=workspace,
            employee=employee,
            source_kind='self_assessment',
            metadata__assessment_pack_uuid=pack_uuid,
        ).order_by('metadata__question_id', 'skill__display_name_en')
    )
    for evidence_row in evidence_rows:
        metadata = dict(evidence_row.metadata or {})
        question_id = str(metadata.get('question_id') or '').strip()
        question_type = str(metadata.get('question_type') or '').strip()
        skill_key = evidence_row.skill.canonical_key
        skill_name = evidence_row.skill.display_name_en or skill_key or 'Skill'
        item_key = f'{question_id}:{skill_key or evidence_row.uuid}:{evidence_row.uuid}'
        answer_confidence = float(metadata.get('answer_confidence') or evidence_row.confidence or 0.0)
        source_weight = float(metadata.get('source_weight') or 0.55)
        doc_type = (
            'self_assessment_hidden_skill'
            if question_type == 'hidden_skills'
            else 'self_assessment_skill_evidence'
        )
        evidence_category = 'hidden_skill' if question_type == 'hidden_skills' else 'targeted_skill'
        text = _build_cv_evidence_text(
            f'{employee.full_name} self-assessment: {skill_name}',
            [
                f'Self-rated level: {float(evidence_row.current_level or 0.0):.2f}/5',
                f'Answer confidence: {answer_confidence:.2f}',
                f'Source weight: {source_weight:.2f}',
                f'Why asked: {metadata.get("why_asked", "")}'
                if metadata.get('why_asked') else '',
                str(evidence_row.evidence_text or ''),
            ],
            min_body_chars=12,
        )
        append_document(
            question_id=question_id,
            item_key=item_key,
            doc_type=doc_type,
            skill_key=skill_key,
            evidence_category=evidence_category,
            section_heading=skill_name,
            text=text,
            confidence=float(evidence_row.confidence or 0.0),
            evidence_row_uuid=str(evidence_row.uuid),
        )
        example_text = str(evidence_row.evidence_text or '').strip()
        if example_text:
            append_document(
                question_id=question_id,
                item_key=f'{item_key}:example',
                doc_type='self_assessment_example',
                skill_key=skill_key,
                evidence_category=(
                    'hidden_skill_example' if question_type == 'hidden_skills' else 'targeted_example'
                ),
                section_heading=f'{skill_name} example',
                text=_build_cv_evidence_text(
                    f'{employee.full_name} self-assessment example: {skill_name}',
                    [example_text],
                    min_body_chars=8,
                ),
                confidence=float(evidence_row.confidence or 0.0),
                evidence_row_uuid=str(evidence_row.uuid),
            )
        section_index += 1

    aspiration = dict(response_payload.get('aspiration') or {})
    aspiration_text = _build_cv_evidence_text(
        f'{employee.full_name} aspiration',
        [
            f'Target role family: {aspiration.get("target_role_family", "")}'
            if aspiration.get('target_role_family') else '',
            f'Interest signal: {aspiration.get("interest_signal", "")}'
            if aspiration.get('interest_signal') else '',
            str(aspiration.get('notes') or ''),
        ],
        min_body_chars=8,
    )
    if aspiration_text:
        append_document(
            question_id='aspiration',
            item_key='aspiration',
            doc_type='self_assessment_aspiration',
            skill_key='',
            evidence_category='aspiration',
            section_heading='Aspiration',
            text=aspiration_text,
            confidence=0.6,
        )
    return documents


def index_employee_assessment_pack_sync(pack_pk) -> dict[str, Any]:
    from employee_assessment.models import AssessmentPackStatus, EmployeeAssessmentPack

    pack = EmployeeAssessmentPack.objects.select_related(
        'cycle',
        'cycle__workspace',
        'employee',
    ).get(pk=pack_pk)
    workspace = pack.cycle.workspace
    workspace_uuid = str(workspace.uuid)
    pack_uuid = str(pack.uuid)
    indexed_at = datetime.now(timezone.utc).isoformat()
    previous_generation_id = str(pack.active_vector_generation_id or '').strip()

    if pack.status not in {AssessmentPackStatus.SUBMITTED, AssessmentPackStatus.COMPLETED}:
        clear_employee_assessment_pack_index_sync(
            workspace_uuid=workspace_uuid,
            pack_uuid=pack_uuid,
        )
        if pack.active_vector_generation_id:
            pack.active_vector_generation_id = ''
            pack.save(update_fields=['active_vector_generation_id', 'updated_at'])
        return {
            'status': 'skipped',
            'reason': 'pack_not_submitted',
            'index_version': SELF_ASSESSMENT_INDEX_VERSION,
            'indexed_at': indexed_at,
            'active_generation_id': '',
        }

    generation_id = str(uuid4())
    documents = _build_self_assessment_documents_for_generation(
        pack,
        generation_id=generation_id,
    )
    if not documents:
        clear_employee_assessment_pack_index_sync(
            workspace_uuid=workspace_uuid,
            pack_uuid=pack_uuid,
        )
        if pack.active_vector_generation_id:
            pack.active_vector_generation_id = ''
            pack.save(update_fields=['active_vector_generation_id', 'updated_at'])
        return {
            'status': 'skipped',
            'reason': 'no_self_assessment_documents',
            'index_version': SELF_ASSESSMENT_INDEX_VERSION,
            'indexed_at': indexed_at,
            'active_generation_id': '',
        }

    qdrant = get_qdrant_manager_sync()
    embedding_manager = get_embedding_manager_sync()
    embeddings = embedding_manager.embed_batch_sync([doc['payload']['chunk_text'] for doc in documents])
    embedding_model = _embedding_model_name(embedding_manager)
    vector_dimensions = _collect_vector_dimensions(embeddings)
    expected_dimensions = qdrant.vector_size
    configured_dimensions = getattr(embedding_manager, 'dimensions', expected_dimensions)

    if len(embeddings) != len(documents):
        return {
            'status': 'failed',
            'reason': 'embedding_count_mismatch',
            'expected_document_count': len(documents),
            'embedding_count': len(embeddings),
            'embedding_model': embedding_model,
            'index_version': SELF_ASSESSMENT_INDEX_VERSION,
            'indexed_at': indexed_at,
            'active_generation_id': previous_generation_id,
        }
    if len(vector_dimensions) != 1 or vector_dimensions[0] != expected_dimensions:
        return {
            'status': 'failed',
            'reason': 'embedding_dimension_mismatch',
            'expected_vector_size': expected_dimensions,
            'configured_embedding_dimensions': configured_dimensions,
            'actual_vector_sizes': vector_dimensions,
            'eligible_document_count': len(documents),
            'indexed_document_count': 0,
            'embedding_model': embedding_model,
            'index_version': SELF_ASSESSMENT_INDEX_VERSION,
            'indexed_at': indexed_at,
            'active_generation_id': previous_generation_id,
        }

    for document, vector in zip(documents, embeddings):
        document['vector'] = vector
        document['payload']['embedding_model'] = embedding_model

    indexed_count = qdrant.upsert_documents_batch_sync(documents)
    status = 'indexed' if indexed_count == len(documents) else 'partial'
    if status != 'indexed':
        clear_employee_assessment_pack_index_sync(
            workspace_uuid=workspace_uuid,
            pack_uuid=pack_uuid,
            generation_id=generation_id,
        )
        return {
            'status': status,
            'eligible_document_count': len(documents),
            'indexed_document_count': indexed_count,
            'embedding_model': embedding_model,
            'index_version': SELF_ASSESSMENT_INDEX_VERSION,
            'indexed_at': indexed_at,
            'attempted_generation_id': generation_id,
            'active_generation_id': previous_generation_id,
        }

    if previous_generation_id and previous_generation_id != generation_id:
        clear_employee_assessment_pack_index_sync(
            workspace_uuid=workspace_uuid,
            pack_uuid=pack_uuid,
            generation_id=previous_generation_id,
        )
    if pack.active_vector_generation_id != generation_id:
        pack.active_vector_generation_id = generation_id
        pack.save(update_fields=['active_vector_generation_id', 'updated_at'])
    return {
        'status': 'indexed',
        'eligible_document_count': len(documents),
        'indexed_document_count': indexed_count,
        'embedding_model': embedding_model,
        'index_version': SELF_ASSESSMENT_INDEX_VERSION,
        'indexed_at': indexed_at,
        'attempted_generation_id': generation_id,
        'active_generation_id': generation_id,
    }


def retrieve_employee_self_assessment_evidence_sync(
    workspace,
    *,
    query_text: str,
    query_vector: Optional[list[float]] = None,
    employee_uuids: Optional[list[str]] = None,
    cycle_uuids: Optional[list[str]] = None,
    skill_keys: Optional[list[str]] = None,
    evidence_doc_types: Optional[list[str]] = None,
    limit: int = 6,
    min_score: Optional[float] = None,
    include_superseded_cycles: bool = True,
) -> list[dict[str, Any]]:
    if query_vector is None and not (query_text or '').strip():
        return []

    base_filters: dict[str, Any] = {
        'source_kind': ['self_assessment'],
    }
    generation_ids = _collect_current_self_assessment_generation_ids_sync(
        workspace.pk,
        employee_uuids=employee_uuids,
        cycle_uuids=cycle_uuids,
        include_superseded_cycles=include_superseded_cycles,
    )
    if not generation_ids:
        return []
    base_filters['generation_id'] = generation_ids
    if employee_uuids:
        base_filters['employee_uuid'] = employee_uuids
    if skill_keys:
        base_filters['skill_key'] = skill_keys
    default_doc_types = evidence_doc_types or sorted(SELF_ASSESSMENT_EVIDENCE_DOC_TYPES)

    try:
        qdrant = get_qdrant_manager_sync()
        resolved_query_vector = query_vector
        if resolved_query_vector is None:
            embedding_manager = get_embedding_manager_sync()
            resolved_query_vector = embedding_manager.embed_sync(query_text)
        raw_results = qdrant.search_sync(
            org_id=str(workspace.uuid),
            query_vector=resolved_query_vector,
            doc_types=default_doc_types,
            top_k=limit,
            min_score=min_score,
            additional_filters=base_filters,
        )
    except Exception as exc:
        logger.warning(
            'Employee self-assessment retrieval failed for %s: %s',
            workspace.slug,
            exc,
            exc_info=True,
        )
        return []

    normalized_results: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in sorted(raw_results, key=lambda row: float(row.get('score') or 0.0), reverse=True):
        if item.get('id') in seen_ids:
            continue
        seen_ids.add(item.get('id'))
        payload = dict(item.get('payload') or {})
        normalized_results.append(
            {
                'id': item.get('id'),
                'score': float(item.get('score') or 0.0),
                'employee_uuid': payload.get('employee_uuid', ''),
                'doc_type': payload.get('doc_type', ''),
                'skill_key': payload.get('skill_key', ''),
                'evidence_row_uuid': payload.get('evidence_row_uuid', ''),
                'evidence_category': payload.get('evidence_category', ''),
                'cycle_uuid': payload.get('cycle_uuid', ''),
                'pack_uuid': payload.get('pack_uuid', ''),
                'question_id': payload.get('question_id', ''),
                'source_title': payload.get('source_title', ''),
                'section_heading': payload.get('section_heading', ''),
                'employee_name': payload.get('employee_name', ''),
                'current_title': payload.get('current_title', ''),
                'confidence': float(payload.get('confidence') or 0.0),
                'chunk_text': payload.get('chunk_text', ''),
            }
        )
        if len(normalized_results) >= limit:
            break
    return normalized_results


def _collect_current_self_assessment_generation_ids_sync(
    workspace_pk,
    *,
    employee_uuids: Optional[list[str]] = None,
    cycle_uuids: Optional[list[str]] = None,
    include_superseded_cycles: bool = True,
) -> list[str]:
    from employee_assessment.models import AssessmentPackStatus, EmployeeAssessmentPack

    queryset = EmployeeAssessmentPack.objects.filter(
        cycle__workspace_id=workspace_pk,
        status__in=[AssessmentPackStatus.SUBMITTED, AssessmentPackStatus.COMPLETED],
    )
    if include_superseded_cycles:
        queryset = queryset.exclude(cycle__status='failed')
    else:
        queryset = queryset.exclude(cycle__status__in=['failed', 'superseded'])
    if employee_uuids:
        queryset = queryset.filter(employee__uuid__in=employee_uuids)
    if cycle_uuids:
        queryset = queryset.filter(cycle__uuid__in=cycle_uuids)

    generation_ids: list[str] = []
    for pack in queryset.only('active_vector_generation_id'):
        generation_id = str(pack.active_vector_generation_id or '').strip()
        if not generation_id:
            continue
        if generation_id not in generation_ids:
            generation_ids.append(generation_id)
    return generation_ids


def _collect_current_cv_generation_ids_sync(
    workspace_pk,
    *,
    employee_uuids: Optional[list[str]] = None,
) -> list[str]:
    queryset = EmployeeCVProfile.objects.filter(
        workspace_id=workspace_pk,
        status=EmployeeCVProfile.Status.MATCHED,
    )
    if employee_uuids:
        queryset = queryset.filter(employee__uuid__in=employee_uuids)

    generation_ids: list[str] = []
    for profile in queryset.only('active_vector_generation_id'):
        generation_id = str(profile.active_vector_generation_id or '').strip()
        if not generation_id:
            continue
        if generation_id not in generation_ids:
            generation_ids.append(generation_id)
    return generation_ids


def retrieve_workspace_evidence_sync(
    workspace,
    *,
    query_text: str,
    query_vector: Optional[list[float]] = None,
    doc_types: Optional[list[str]] = None,
    source_kinds: Optional[list[str]] = None,
    limit: int = 6,
    min_score: Optional[float] = None,
    additional_filters: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    if query_vector is None and not (query_text or '').strip():
        return []

    filters = dict(additional_filters or {})
    if source_kinds:
        # We intentionally keep both semantic channels: doc_type expresses the Stage 04 retrieval lane,
        # while source_kind narrows the physical source set inside that lane.
        filters['source_kind'] = source_kinds

    try:
        qdrant = get_qdrant_manager_sync()
        resolved_query_vector = query_vector
        if resolved_query_vector is None:
            embedding_manager = get_embedding_manager_sync()
            resolved_query_vector = embedding_manager.embed_sync(query_text)
        raw_results = qdrant.search_sync(
            org_id=str(workspace.uuid),
            query_vector=resolved_query_vector,
            doc_types=doc_types,
            top_k=limit,
            min_score=min_score,
            additional_filters=filters,
        )
    except Exception as exc:
        logger.warning(
            'Workspace evidence retrieval failed for %s: %s',
            workspace.slug,
            exc,
            exc_info=True,
        )
        return []

    seen_keys = set()
    results: list[dict[str, Any]] = []
    for item in raw_results:
        payload = dict(item.get('payload') or {})
        dedupe_key = (payload.get('source_uuid'), payload.get('chunk_index'))
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        results.append(
            {
                'id': item.get('id'),
                'score': float(item.get('score') or 0.0),
                'source_uuid': payload.get('source_uuid', ''),
                'parsed_source_uuid': payload.get('parsed_source_uuid', ''),
                'source_kind': payload.get('source_kind', ''),
                'doc_type': payload.get('doc_type', ''),
                'chunk_index': int(payload.get('chunk_index') or 0),
                'chunk_family': payload.get('chunk_family', ''),
                'source_title': payload.get('source_title', ''),
                'language_code': payload.get('language_code', ''),
                'section_index': payload.get('section_index'),
                'section_heading': payload.get('section_heading', ''),
                'page_number': payload.get('page_number'),
                'chunk_text': payload.get('chunk_text', ''),
            }
        )
    return results


def retrieve_employee_cv_evidence_sync(
    workspace,
    *,
    query_text: str,
    query_vector: Optional[list[float]] = None,
    employee_uuids: Optional[list[str]] = None,
    skill_keys: Optional[list[str]] = None,
    evidence_doc_types: Optional[list[str]] = None,
    limit: int = 6,
    min_score: Optional[float] = None,
    include_contextual_matches: bool = True,
) -> list[dict[str, Any]]:
    if query_vector is None and not (query_text or '').strip():
        return []

    base_filters: dict[str, Any] = {
        'source_kind': [WorkspaceSourceKind.EMPLOYEE_CV],
    }
    generation_ids = _collect_current_cv_generation_ids_sync(
        workspace.pk,
        employee_uuids=employee_uuids,
    )
    if not generation_ids:
        return []
    base_filters['generation_id'] = generation_ids
    if employee_uuids:
        base_filters['employee_uuid'] = employee_uuids
    default_doc_types = evidence_doc_types or sorted(CV_EVIDENCE_DOC_TYPES)

    try:
        qdrant = get_qdrant_manager_sync()
        resolved_query_vector = query_vector
        if resolved_query_vector is None:
            embedding_manager = get_embedding_manager_sync()
            resolved_query_vector = embedding_manager.embed_sync(query_text)
        raw_results: list[dict[str, Any]] = []
        if skill_keys:
            skill_filters = {
                **base_filters,
                'skill_key': skill_keys,
            }
            raw_results.extend(
                qdrant.search_sync(
                    org_id=str(workspace.uuid),
                    query_vector=resolved_query_vector,
                    doc_types=default_doc_types,
                    top_k=limit,
                    min_score=min_score,
                    additional_filters=skill_filters,
                )
            )
            contextual_doc_types = [doc_type for doc_type in default_doc_types if doc_type != 'cv_skill_evidence']
            if contextual_doc_types and include_contextual_matches:
                raw_results.extend(
                    qdrant.search_sync(
                        org_id=str(workspace.uuid),
                        query_vector=resolved_query_vector,
                        doc_types=contextual_doc_types,
                        top_k=max(2, limit // 2),
                        min_score=min_score,
                        additional_filters=base_filters,
                    )
                )
        else:
            raw_results = qdrant.search_sync(
                org_id=str(workspace.uuid),
                query_vector=resolved_query_vector,
                doc_types=default_doc_types,
                top_k=limit,
                min_score=min_score,
                additional_filters=base_filters,
            )
    except Exception as exc:
        logger.warning(
            'Employee CV evidence retrieval failed for %s: %s',
            workspace.slug,
            exc,
            exc_info=True,
        )
        return []

    normalized_results: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in sorted(raw_results, key=lambda row: float(row.get('score') or 0.0), reverse=True):
        if item.get('id') in seen_ids:
            continue
        seen_ids.add(item.get('id'))
        payload = dict(item.get('payload') or {})
        normalized_results.append(
            {
                'id': item.get('id'),
                'score': float(item.get('score') or 0.0),
                'source_uuid': payload.get('source_uuid', ''),
                'parsed_source_uuid': payload.get('parsed_source_uuid', ''),
                'employee_uuid': payload.get('employee_uuid', ''),
                'evidence_row_uuid': payload.get('evidence_row_uuid', ''),
                'source_kind': payload.get('source_kind', ''),
                'doc_type': payload.get('doc_type', ''),
                'skill_key': payload.get('skill_key', ''),
                'evidence_category': payload.get('evidence_category', ''),
                'chunk_index': int(payload.get('chunk_index') or 0),
                'chunk_family': payload.get('chunk_family', ''),
                'source_title': payload.get('source_title', ''),
                'language_code': payload.get('language_code', ''),
                'section_index': payload.get('section_index'),
                'section_heading': payload.get('section_heading', ''),
                'page_number': payload.get('page_number'),
                'employee_name': payload.get('employee_name', ''),
                'current_title': payload.get('current_title', ''),
                'confidence': float(payload.get('confidence') or 0.0),
                'chunk_text': payload.get('chunk_text', ''),
            }
        )
        if len(normalized_results) >= limit:
            break
    return normalized_results


def retrieve_employee_fused_evidence_sync(
    workspace,
    *,
    query_text: str,
    query_vector: Optional[list[float]] = None,
    employee_uuids: Optional[list[str]] = None,
    cycle_uuids: Optional[list[str]] = None,
    skill_keys: Optional[list[str]] = None,
    cv_doc_types: Optional[list[str]] = None,
    self_assessment_doc_types: Optional[list[str]] = None,
    limit: int = 6,
    min_score: Optional[float] = None,
    include_contextual_cv_matches: bool = True,
    include_superseded_self_assessment_cycles: bool = True,
) -> list[dict[str, Any]]:
    if query_vector is None and not (query_text or '').strip():
        return []

    cv_matches = retrieve_employee_cv_evidence_sync(
        workspace,
        query_text=query_text,
        query_vector=query_vector,
        employee_uuids=employee_uuids,
        skill_keys=skill_keys,
        evidence_doc_types=cv_doc_types,
        limit=limit,
        min_score=min_score,
        include_contextual_matches=include_contextual_cv_matches,
    )
    self_assessment_matches = retrieve_employee_self_assessment_evidence_sync(
        workspace,
        query_text=query_text,
        query_vector=query_vector,
        employee_uuids=employee_uuids,
        cycle_uuids=cycle_uuids,
        skill_keys=skill_keys,
        evidence_doc_types=self_assessment_doc_types,
        limit=limit,
        min_score=min_score,
        include_superseded_cycles=include_superseded_self_assessment_cycles,
    )

    merged_results: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for item in sorted(
        [
            *[{**row, 'retrieval_lane': 'cv'} for row in cv_matches],
            *[{**row, 'retrieval_lane': 'self_assessment'} for row in self_assessment_matches],
        ],
        key=lambda row: float(row.get('score') or 0.0),
        reverse=True,
    ):
        dedupe_key = str(item.get('id') or '') or str(item.get('evidence_row_uuid') or '') or (
            f"{item.get('retrieval_lane', '')}:{item.get('doc_type', '')}:"
            f"{item.get('section_heading', '')}:{str(item.get('chunk_text') or '')[:80]}"
        )
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        merged_results.append(item)
        if len(merged_results) >= limit:
            break
    return merged_results


def format_retrieved_evidence_digest(
    matches: list[dict[str, Any]],
    *,
    max_chars: int = 12000,
) -> str:
    sections: list[str] = []
    current_length = 0
    for match in matches:
        section = (
            f"[{match.get('source_kind')}] {match.get('source_title')} "
            f"(chunk {match.get('chunk_index')}, "
            f"section={match.get('section_heading') or match.get('section_index') or '-'}, "
            f"page={match.get('page_number') or '-'}, "
            f"score={match.get('score', 0.0):.2f})\n"
            f"{match.get('chunk_text', '')}"
        ).strip()
        if not section:
            continue
        separator_length = 2 if sections else 0
        remaining = max_chars - current_length - separator_length
        if remaining <= 0:
            break
        if len(section) > remaining:
            if sections:
                break
            sections.append(_truncate_section(section, remaining))
            current_length = max_chars
            break
        sections.append(section)
        current_length += separator_length + len(section)
    return '\n\n'.join(sections)
