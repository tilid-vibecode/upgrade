import csv
import io
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

import httpx
import pdfplumber
from asgiref.sync import sync_to_async
from docx import Document
from django.db import transaction
from pypdf import PdfReader

from company_intake.models import (
    WorkspaceSource,
    WorkspaceSourceKind,
    WorkspaceSourceStatus,
    WorkspaceSourceTransport,
)
from media_storage.services import extract_media_text_for_parsing
from .models import (
    Employee,
    EmployeeOrgAssignment,
    EmployeeProjectAssignment,
    OrgUnit,
    ParsedSource,
    Project,
    ReportingLine,
    SourceChunk,
)
from .vector_indexing import index_parsed_source_chunks_sync

logger = logging.getLogger(__name__)

_CHUNK_MAX_CHARS = 1500
_CHUNK_OVERLAP = 200
_URL_FETCH_TIMEOUT = 45.0
_URL_USER_AGENT = 'UpgradePrototypeBot/0.2 (+https://example.invalid)'
_PARSE_METADATA_SCHEMA_VERSION = 'stage2-v1'
_PARSER_VERSION = '2.0'

_HEADER_ALIASES = {
    'employee_id': {
        'personid',
        'employee id',
        'employee_id',
        'employee number',
        'worker id',
        'staff id',
        'user id',
        'login',
        'логин',
        'id',
    },
    'full_name': {
        'name',
        'full name',
        'employee name',
        'employee full name',
        'full_name',
        'fio',
        'фио',
        'имя',
        'сотрудник',
        'employee',
    },
    'email': {
        'email',
        'work email',
        'email address',
        'business email',
        'corporate email',
        'почта',
        'e mail',
        'e-mail',
        'mail',
    },
    'supervisor_id': {
        'supervisorid',
        'supervisor id',
        'manager id',
        'managerid',
        'reports to id',
        'lead id',
        'line manager id',
    },
    'supervisor_name': {
        'supervisor',
        'manager',
        'manager name',
        'line manager',
        'reports to',
        'reports to name',
        'руководитель',
        'непосредственный руководитель',
        'лид подразделения',
        'lead',
    },
    'department': {
        'department',
        'department name',
        'dept',
        'подразделение',
        'департамент',
        'team',
        'team name',
        'functional team',
        'function',
        'команда',
    },
    'title': {
        'job title',
        'title',
        'role',
        'position',
        'position name',
        'роль',
        'должность',
        'job',
    },
    'projects': {
        'projects',
        'project',
        'project names',
        'проекты',
        'project list',
        'initiatives',
        'initiative',
        'products',
        'product',
    },
}
_REQUIRED_CSV_MAPPING_TARGETS = {'full_name'}
_CSV_HEADER_HINT_TOKENS = {
    'employee_id': {'employee', 'worker', 'staff', 'user', 'id', 'логин'},
    'full_name': {'employee', 'full', 'name', 'fio', 'фио', 'имя', 'сотрудник'},
    'email': {'email', 'mail', 'почта'},
    'supervisor_id': {'supervisor', 'manager', 'lead', 'reports', 'id', 'руководитель'},
    'supervisor_name': {'supervisor', 'manager', 'lead', 'reports', 'руководитель', 'лид'},
    'department': {'department', 'dept', 'team', 'function', 'подразделение', 'департамент', 'команда'},
    'title': {'title', 'role', 'position', 'job', 'роль', 'должность'},
    'projects': {'project', 'projects', 'initiative', 'initiatives', 'product', 'проекты'},
}


@dataclass
class ExtractedContent:
    text: str
    content_type: str
    page_count: Optional[int] = None
    metadata: Optional[dict] = None


class _LinkCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        if tag.lower() != 'a':
            return
        attrs_map = {key.lower(): value for key, value in attrs}
        href = attrs_map.get('href')
        if href:
            self.links.append(href)


class _HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self.parts: list[str] = []
        self.title_text = ''
        self._inside_title = False

    def handle_starttag(self, tag: str, attrs):
        lowered = tag.lower()
        if lowered in {'script', 'style', 'noscript', 'nav', 'header', 'footer', 'aside'}:
            self._skip_depth += 1
            return
        if lowered == 'title':
            self._inside_title = True
        if lowered in {'p', 'div', 'section', 'article', 'main', 'ul', 'ol', 'li', 'br', 'h1', 'h2', 'h3', 'h4'}:
            self.parts.append('\n')

    def handle_endtag(self, tag: str):
        lowered = tag.lower()
        if lowered in {'script', 'style', 'noscript', 'nav', 'header', 'footer', 'aside'} and self._skip_depth:
            self._skip_depth -= 1
            return
        if lowered == 'title':
            self._inside_title = False
        if lowered in {'p', 'div', 'section', 'article', 'main', 'ul', 'ol', 'li', 'br', 'h1', 'h2', 'h3', 'h4'}:
            self.parts.append('\n')

    def handle_data(self, data: str):
        if self._skip_depth:
            return
        text = unescape(data or '')
        if self._inside_title and text.strip():
            self.title_text += text.strip()
        if text.strip():
            self.parts.append(text)


async def load_source_bytes(source: WorkspaceSource) -> tuple[Optional[bytes], str, str]:
    """Return (payload, content_type, filename_or_label)."""
    if source.transport == WorkspaceSourceTransport.INLINE_TEXT:
        return source.inline_text.encode('utf-8'), 'text/plain', source.title or 'inline-text.txt'

    if source.transport == WorkspaceSourceTransport.EXTERNAL_URL:
        return None, 'text/html', source.external_url

    media_file = source.media_file
    if media_file is None:
        return None, '', source.title or 'missing-media'

    from server.storage import persistent_client, processing_client

    data = None
    if media_file.processing_key:
        try:
            data = await processing_client().download_bytes(media_file.processing_key)
        except Exception as exc:
            logger.warning('Failed to download processing copy for %s: %s', media_file.uuid, exc)
    if data is None and media_file.persistent_key:
        data = await persistent_client().download_bytes(media_file.persistent_key)
    return data, media_file.content_type, media_file.original_filename


async def fetch_external_url(url: str) -> ExtractedContent:
    if not url:
        raise ValueError('External URL is empty.')

    headers = {'User-Agent': _URL_USER_AGENT}
    async with httpx.AsyncClient(timeout=_URL_FETCH_TIMEOUT, follow_redirects=True, headers=headers) as client:
        response = await client.get(url)
        response.raise_for_status()

    content_type = response.headers.get('content-type', '').split(';')[0].strip().lower() or 'text/html'
    if 'html' in content_type:
        return extract_html_text(response.text, url=url)
    if 'text/plain' in content_type:
        return ExtractedContent(
            text=response.text,
            content_type='text/plain',
            metadata={'fetched_url': url, 'final_url': str(response.url)},
        )

    # Fallback: treat body as bytes and delegate to file extractor where possible.
    return extract_text(
        response.content,
        content_type=content_type,
        filename=urlparse(str(response.url)).path or url,
    )


async def extract_workspace_source_content(
    source: WorkspaceSource,
) -> tuple[ExtractedContent, str, str]:
    if source.transport == WorkspaceSourceTransport.EXTERNAL_URL:
        return await fetch_external_url(source.external_url), source.external_url, 'remote_fetch'

    payload, content_type, label = await load_source_bytes(source)
    if payload is None:
        raise ValueError(f'Could not load source payload for {label}.')

    if source.media_file is not None:
        extracted_media = await extract_media_text_for_parsing(source.media_file, payload)
        if extracted_media is not None:
            return (
                ExtractedContent(
                    text=extracted_media.get('text', ''),
                    content_type=extracted_media.get('content_type', content_type),
                    page_count=extracted_media.get('page_count'),
                    metadata=extracted_media.get('metadata'),
                ),
                label,
                'media_processor',
            )

    return extract_text(payload, content_type=content_type, filename=label), label, 'builtin_extractor'


async def parse_workspace_source(
    source: WorkspaceSource,
    *,
    force: bool = False,
    mapping_override: Optional[dict[str, str]] = None,
) -> dict:
    if source.status == WorkspaceSourceStatus.PARSED and not force:
        parsed = await sync_to_async(lambda: getattr(source, 'parsed_source', None))()
        return {
            'status': 'parsed',
            'parse_metadata': source.parse_metadata,
            'parse_error': '',
            'source_uuid': str(source.uuid),
            'source_kind': source.source_kind,
            'already_parsed': True,
            'parsed_source_uuid': str(parsed.uuid) if parsed else None,
        }

    try:
        await _mark_source_parsing(source)
        extracted, label, extraction_method = await extract_workspace_source_content(source)

        language_code = (
            source.language_code
            or ((extracted.metadata or {}).get('language_code') if extracted.metadata else '')
            or ((extracted.metadata or {}).get('language_hint') if extracted.metadata else '')
            or ''
        )
        source_origin = _build_source_origin(source)
        parsed_at = datetime.now(timezone.utc).isoformat()
        chunk_payloads = build_chunk_payloads(
            source=source,
            extracted=extracted,
            source_origin=source_origin,
            language_code=language_code,
        )
        warnings: list[str] = []
        if not extracted.text.strip():
            warnings.append('No text content was extracted from the source.')
        parse_metadata = _build_parse_metadata(
            source=source,
            extracted=extracted,
            source_origin=source_origin,
            language_code=language_code,
            label=label,
            chunk_payloads=chunk_payloads,
            warnings=warnings,
            extraction_method=extraction_method,
            parsed_at=parsed_at,
        )
        if extracted.metadata:
            parse_metadata['extractor_metadata'] = dict(extracted.metadata)
            for key in ('title', 'fetched_url', 'final_url', 'paragraph_count'):
                value = extracted.metadata.get(key)
                if value not in (None, ''):
                    parse_metadata[key] = value

        if source.source_kind == WorkspaceSourceKind.EMPLOYEE_CV:
            cv_metadata = infer_cv_metadata(extracted.text)
            if cv_metadata:
                parse_metadata.update(cv_metadata)
                await sync_to_async(_upsert_employee_from_cv_sync)(source.pk, cv_metadata)

        if source.source_kind == WorkspaceSourceKind.ORG_CSV:
            # Use raw CSV text to avoid header loss from TabularProcessor
            # preprocessing (csv.Sniffer().has_header() may fail for Cyrillic
            # CSVs, causing pandas to replace headers with numeric indices).
            raw_payload, _ct, _label = await load_source_bytes(source)
            raw_csv_text = decode_bytes(raw_payload) if raw_payload else extracted.text
            org_summary = await sync_to_async(import_org_csv_sync)(
                source.pk,
                raw_csv_text,
                mapping_override,
            )
            parse_metadata['org_import'] = org_summary
            parse_metadata['warnings'].extend(org_summary.get('warnings') or [])
            parse_metadata['quality']['warning_count'] = len(parse_metadata['warnings'])

        parsed_source = await sync_to_async(save_parsed_source_sync)(
            source.pk,
            extracted.text,
            extracted.content_type,
            extracted.page_count,
            parse_metadata,
            chunk_payloads,
            False,
        )
        try:
            vector_index = await sync_to_async(index_parsed_source_chunks_sync)(parsed_source.pk)
        except Exception as exc:
            logger.warning(
                'Vector indexing failed for parsed source %s: %s',
                parsed_source.uuid,
                exc,
                exc_info=True,
            )
            vector_index = {
                'status': 'failed',
                'error': str(exc),
            }
        parse_metadata['vector_index'] = vector_index
        await sync_to_async(_finalize_parsed_source_sync)(parsed_source.pk, parse_metadata)
        return {
            'status': 'parsed',
            'parse_metadata': parse_metadata,
            'parse_error': '',
            'source_uuid': str(source.uuid),
            'source_kind': source.source_kind,
            'parsed_source_uuid': str(parsed_source.uuid),
        }
    except Exception as exc:
        logger.exception('Failed to parse source %s', source.uuid)
        return await _mark_source_failed(source, str(exc))


async def _mark_source_failed(source: WorkspaceSource, error: str) -> dict:
    def _save() -> None:
        parse_metadata = dict(source.parse_metadata or {})
        parse_metadata['failure'] = {
            'message': error,
            'failed_at': datetime.now(timezone.utc).isoformat(),
            'retryable': True,
        }
        source.status = WorkspaceSourceStatus.FAILED
        source.parse_error = error
        source.parse_metadata = parse_metadata
        source.save(update_fields=['status', 'parse_error', 'parse_metadata', 'updated_at'])

    await sync_to_async(_save)()
    return {
        'status': 'failed',
        'parse_metadata': {
            **(source.parse_metadata or {}),
            'failure': {
                'message': error,
                'retryable': True,
            },
        },
        'parse_error': error,
        'source_uuid': str(source.uuid),
        'source_kind': source.source_kind,
    }


async def _mark_source_parsing(source: WorkspaceSource) -> None:
    def _save() -> None:
        source.status = WorkspaceSourceStatus.PARSING
        source.parse_error = ''
        source.save(update_fields=['status', 'parse_error', 'updated_at'])

    await sync_to_async(_save)()


def extract_text(payload: bytes, *, content_type: str, filename: str) -> ExtractedContent:
    content_type = (content_type or '').lower()
    lower_name = (filename or '').lower()

    if 'pdf' in content_type or lower_name.endswith('.pdf'):
        return extract_pdf_text(payload)
    if 'wordprocessingml' in content_type or lower_name.endswith('.docx'):
        return extract_docx_text(payload)
    if 'text/html' in content_type or lower_name.endswith('.html') or lower_name.endswith('.htm'):
        return extract_html_text(decode_bytes(payload), url=filename)
    if 'text/plain' in content_type or lower_name.endswith('.txt'):
        text = decode_bytes(payload)
        return ExtractedContent(text=text, content_type='text/plain')
    if 'csv' in content_type or lower_name.endswith('.csv'):
        text = decode_bytes(payload)
        return ExtractedContent(text=text, content_type='text/csv')
    if 'msword' in content_type or lower_name.endswith('.doc'):
        raise ValueError('Legacy .doc parsing is not supported yet. Please convert the file to .docx or PDF.')

    text = decode_bytes(payload)
    return ExtractedContent(text=text, content_type=content_type or 'application/octet-stream')


def extract_pdf_text(payload: bytes) -> ExtractedContent:
    text_parts: list[str] = []
    page_texts: list[dict[str, Any]] = []
    page_count = 0
    with pdfplumber.open(io.BytesIO(payload)) as pdf:
        page_count = len(pdf.pages)
        for index, page in enumerate(pdf.pages, start=1):
            page_text = page.extract_text() or ''
            if page_text.strip():
                text_parts.append(page_text)
                page_texts.append(
                    {
                        'page_number': index,
                        'text': page_text,
                        'char_count': len(page_text),
                    }
                )

    text = '\n\n'.join(text_parts).strip()
    if not text:
        reader = PdfReader(io.BytesIO(payload))
        page_count = len(reader.pages)
        fallback_texts = []
        page_texts = []
        for index, page in enumerate(reader.pages, start=1):
            page_text = page.extract_text() or ''
            if page_text.strip():
                fallback_texts.append(page_text)
                page_texts.append(
                    {
                        'page_number': index,
                        'text': page_text,
                        'char_count': len(page_text),
                    }
                )
        text = '\n\n'.join(fallback_texts).strip()

    return ExtractedContent(
        text=text,
        content_type='application/pdf',
        page_count=page_count,
        metadata={
            'page_texts': page_texts[:200],
            'pages_extracted': len(page_texts),
            'total_pages': page_count,
        },
    )


def extract_docx_text(payload: bytes) -> ExtractedContent:
    document = Document(io.BytesIO(payload))
    lines = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    text = '\n'.join(lines).strip()
    return ExtractedContent(
        text=text,
        content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        metadata={'paragraph_count': len(lines)},
    )


def extract_html_text(html_text: str, *, url: str = '') -> ExtractedContent:
    parser = _HTMLTextExtractor()
    parser.feed(html_text)
    raw_text = ''.join(parser.parts)
    raw_text = re.sub(r'\n{3,}', '\n\n', raw_text)
    raw_text = re.sub(r'[ \t]{2,}', ' ', raw_text)
    text = raw_text.strip()
    title = parser.title_text.strip()
    seen_links: set[str] = set()
    links: list[str] = []
    for link in extract_links_from_html(html_text):
        normalized = (link or '').strip()
        if not normalized or normalized in seen_links:
            continue
        seen_links.add(normalized)
        links.append(normalized)
    return ExtractedContent(
        text=text,
        content_type='text/html',
        metadata={
            'fetched_url': url,
            'title': title,
            'links': links[:50],
            'link_count': len(links),
        },
    )


def extract_links_from_html(html_text: str) -> list[str]:
    collector = _LinkCollector()
    collector.feed(html_text)
    return collector.links


def decode_bytes(payload: bytes) -> str:
    for encoding in ('utf-8-sig', 'utf-8', 'cp1251', 'windows-1251', 'latin-1'):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode('utf-8', errors='ignore')


def chunk_text(text: str, *, max_chars: int = _CHUNK_MAX_CHARS, overlap: int = _CHUNK_OVERLAP) -> Iterable[str]:
    normalized = re.sub(r'\n{3,}', '\n\n', text.strip())
    if not normalized:
        return []

    paragraphs = [paragraph.strip() for paragraph in normalized.split('\n\n') if paragraph.strip()]
    chunks: list[str] = []
    current = ''

    for paragraph in paragraphs:
        candidate = paragraph if not current else f'{current}\n\n{paragraph}'
        if len(candidate) <= max_chars:
            current = candidate
            continue

        if current:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ''
            current = f'{tail}\n\n{paragraph}'.strip()
            if len(current) <= max_chars:
                continue

        start = 0
        paragraph_text = paragraph
        while start < len(paragraph_text):
            end = min(start + max_chars, len(paragraph_text))
            chunk = paragraph_text[start:end]
            if chunk.strip():
                chunks.append(chunk.strip())
            if end >= len(paragraph_text):
                current = ''
                break
            start = max(0, end - overlap)
        else:
            current = ''

    if current:
        chunks.append(current)
    return chunks


def _looks_like_heading(paragraph: str) -> bool:
    candidate = (paragraph or '').strip()
    if not candidate or len(candidate) > 120 or '\n' in candidate:
        return False
    if re.match(r'^(#{1,6}\s+|\d+[\.\)]\s+)', candidate):
        return True
    if candidate.endswith(':') and len(candidate.split()) <= 12:
        return True
    if any(mark in candidate for mark in '.!?'):
        return False
    return len(candidate.split()) <= 6


def _split_sections_from_text(
    text: str,
    *,
    fallback_heading: str,
    base_metadata: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    normalized = re.sub(r'\n{3,}', '\n\n', (text or '').strip())
    if not normalized:
        return []

    paragraphs = [paragraph.strip() for paragraph in normalized.split('\n\n') if paragraph.strip()]
    if not paragraphs:
        return []

    sections: list[dict[str, Any]] = []
    current_heading = fallback_heading.strip()
    current_paragraphs: list[str] = []

    for index, paragraph in enumerate(paragraphs):
        has_following_content = index < (len(paragraphs) - 1)
        if has_following_content and _looks_like_heading(paragraph):
            if current_paragraphs:
                section = {
                    'heading': current_heading,
                    'text': '\n\n'.join(current_paragraphs).strip(),
                }
                if base_metadata:
                    section.update(base_metadata)
                sections.append(section)
                current_paragraphs = []
            current_heading = paragraph
            continue
        current_paragraphs.append(paragraph)

    if current_paragraphs:
        section = {
            'heading': current_heading,
            'text': '\n\n'.join(current_paragraphs).strip(),
        }
        if base_metadata:
            section.update(base_metadata)
        sections.append(section)

    if not sections:
        section = {
            'heading': fallback_heading.strip(),
            'text': normalized,
        }
        if base_metadata:
            section.update(base_metadata)
        sections.append(section)

    return sections


def build_chunk_payloads(
    *,
    source: WorkspaceSource,
    extracted: ExtractedContent,
    source_origin: dict,
    language_code: str,
) -> list[dict[str, Any]]:
    normalized = re.sub(r'\n{3,}', '\n\n', (extracted.text or '').strip())
    if not normalized:
        return []

    title = ((extracted.metadata or {}).get('title') if extracted.metadata else '') or source.title or ''
    sections: list[dict[str, Any]] = []
    page_texts = ((extracted.metadata or {}).get('page_texts') if extracted.metadata else None) or []
    if page_texts:
        for page_entry in page_texts:
            page_text = (page_entry.get('text') or '').strip()
            if not page_text:
                continue
            page_number = page_entry.get('page_number')
            sections.extend(
                _split_sections_from_text(
                    page_text,
                    fallback_heading=title.strip() or f'Page {page_number}',
                    base_metadata={'page_number': page_number},
                )
            )
    else:
        sections = _split_sections_from_text(
            normalized,
            fallback_heading=title.strip(),
        )

    if not sections:
        sections.append({'heading': title.strip(), 'text': normalized})

    chunk_payloads: list[dict[str, Any]] = []
    for section_index, section in enumerate(sections, start=1):
        section_heading = (section.get('heading') or '').strip()
        section_text = (section.get('text') or '').strip()
        if not section_text:
            continue
        for chunk in chunk_text(section_text):
            chunk_payloads.append(
                {
                    'index': len(chunk_payloads) + 1,
                    'text': chunk,
                    'char_count': len(chunk),
                    'metadata': _build_chunk_metadata(
                        source,
                        source_origin=source_origin,
                        language_code=language_code,
                        section_index=section_index,
                        section_heading=section_heading,
                        page_number=section.get('page_number'),
                    ),
                }
            )
    return chunk_payloads


def normalize_header(header: str) -> str:
    return re.sub(r'[^a-zа-я0-9]+', ' ', (header or '').strip().lower()).strip()


def _score_header_match(target: str, candidate: str) -> int:
    if not candidate:
        return 0

    aliases = _HEADER_ALIASES.get(target, set())
    if candidate in aliases:
        return 300

    alias_contains = [
        alias
        for alias in aliases
        if len(alias) > 2 and (candidate.startswith(f'{alias} ') or candidate.endswith(f' {alias}') or f' {alias} ' in candidate)
    ]
    if alias_contains:
        return 220

    candidate_tokens = set(candidate.split())
    hint_tokens = _CSV_HEADER_HINT_TOKENS.get(target, set())
    overlap = candidate_tokens & hint_tokens
    if overlap:
        score = 100 + (len(overlap) * 20)
        if target in {'employee_id', 'supervisor_id'} and 'id' in candidate_tokens:
            score += 40
        if target == 'full_name' and candidate_tokens & {'name', 'имя', 'фио'}:
            score += 40
        return score

    return 0


def infer_csv_mapping_details(
    headers: Iterable[str],
    mapping_override: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    header_list = [str(header) for header in headers if str(header).strip()]
    normalized = {header: normalize_header(header) for header in header_list}
    inferred_mapping: dict[str, str] = {}
    ambiguous_targets: dict[str, list[str]] = {}

    for target in _HEADER_ALIASES:
        scored_candidates = [
            (original, _score_header_match(target, candidate))
            for original, candidate in normalized.items()
        ]
        scored_candidates = [item for item in scored_candidates if item[1] > 0]
        if not scored_candidates:
            continue

        scored_candidates.sort(key=lambda item: (-item[1], item[0]))
        best_score = scored_candidates[0][1]
        best_headers = [header for header, score in scored_candidates if score == best_score]

        if len(best_headers) == 1:
            inferred_mapping[target] = best_headers[0]
        else:
            ambiguous_targets[target] = best_headers

    override_applied: dict[str, str] = {}
    effective_mapping = dict(inferred_mapping)
    if mapping_override:
        for target, header in mapping_override.items():
            if target not in _HEADER_ALIASES:
                raise ValueError(f"Unknown CSV mapping target '{target}'.")
            header_name = (header or '').strip()
            if not header_name:
                effective_mapping.pop(target, None)
                ambiguous_targets.pop(target, None)
                continue
            if header_name not in header_list:
                raise ValueError(f"CSV mapping override for '{target}' references missing header '{header_name}'.")
            effective_mapping[target] = header_name
            ambiguous_targets.pop(target, None)
            override_applied[target] = header_name

    missing_targets = [
        target
        for target in _HEADER_ALIASES
        if target not in effective_mapping
    ]
    return {
        'headers': header_list,
        'inferred_mapping': inferred_mapping,
        'effective_mapping': effective_mapping,
        'ambiguous_targets': ambiguous_targets,
        'missing_targets': missing_targets,
        'override_applied': override_applied,
    }


def infer_csv_mapping(headers: Iterable[str]) -> dict[str, str]:
    return infer_csv_mapping_details(headers).get('effective_mapping', {})


def _build_source_origin(source: WorkspaceSource) -> dict:
    origin = {'transport': source.transport}
    if source.transport == WorkspaceSourceTransport.MEDIA_FILE and source.media_file_id:
        origin['media_file_uuid'] = str(source.media_file_id)
        if source.media_file is not None:
            origin['media_filename'] = source.media_file.original_filename
    if source.transport == WorkspaceSourceTransport.EXTERNAL_URL and source.external_url:
        origin['external_url'] = source.external_url
    if source.transport == WorkspaceSourceTransport.INLINE_TEXT and source.title:
        origin['inline_label'] = source.title
    return origin


def _resolve_parser_name(*, extraction_method: str, extracted: ExtractedContent) -> str:
    processor = ((extracted.metadata or {}).get('processor') if extracted.metadata else '') or ''
    if extraction_method == 'media_processor' and processor:
        return f'media_{processor}'

    content_type = (extracted.content_type or '').lower()
    if extraction_method == 'remote_fetch':
        return 'remote_html_fetch' if 'html' in content_type else 'remote_content_fetch'
    if 'pdf' in content_type:
        return 'builtin_pdf'
    if 'wordprocessingml' in content_type:
        return 'builtin_docx'
    if 'html' in content_type:
        return 'builtin_html'
    if 'csv' in content_type:
        return 'builtin_csv'
    if 'text/plain' in content_type:
        return 'builtin_text'
    return 'builtin_generic'


def _build_parse_metadata(
    *,
    source: WorkspaceSource,
    extracted: ExtractedContent,
    source_origin: dict,
    language_code: str,
    label: str,
    chunk_payloads: list[dict],
    warnings: list[str],
    extraction_method: str,
    parsed_at: str,
) -> dict[str, Any]:
    extractor_metadata = dict(extracted.metadata or {})
    parser_name = _resolve_parser_name(extraction_method=extraction_method, extracted=extracted)
    heading_count = extractor_metadata.get('heading_count')
    derived_section_count = len(
        {item.get('metadata', {}).get('section_index') for item in chunk_payloads if item.get('metadata', {}).get('section_index') is not None}
    )
    section_count = derived_section_count or heading_count or extractor_metadata.get('paragraph_count') or extractor_metadata.get('line_count')
    scan_detected = bool(extractor_metadata.get('is_scanned'))
    if scan_detected:
        warnings.append('Source appears to be scanned; text quality may be incomplete.')

    char_count = len(extracted.text)
    word_count = len(extracted.text.split())
    quality = {
        'has_text': bool(extracted.text.strip()),
        'is_short': len(extracted.text.strip()) < 200,
        'chunking_strategy': 'section_page_overlap_v2',
        'warning_count': len(warnings),
        'scan_detected': scan_detected,
        'language_code': language_code,
        'extraction_method': extraction_method,
    }
    content = {
        'content_type': extracted.content_type,
        'page_count': extracted.page_count,
        'char_count': char_count,
        'word_count': word_count,
        'chunk_count': len(chunk_payloads),
        'section_count': section_count,
        'heading_count': heading_count,
    }
    return {
        'metadata_schema_version': _PARSE_METADATA_SCHEMA_VERSION,
        'parser': {
            'name': parser_name,
            'version': _PARSER_VERSION,
            'extraction_method': extraction_method,
        },
        'source': {
            'kind': source.source_kind,
            'transport': source.transport,
            'label': label,
            'origin': source_origin,
        },
        'timing': {
            'parsed_at': parsed_at,
            'retrieved_at': parsed_at if source.transport == WorkspaceSourceTransport.EXTERNAL_URL else None,
        },
        'content': content,
        'quality': quality,
        'warnings': warnings,
        'errors': [],
        'can_retry': True,
        'language_code': language_code,
    }


def _chunk_family_for_source_kind(source_kind: str) -> str:
    return {
        WorkspaceSourceKind.ROADMAP: 'roadmap_context',
        WorkspaceSourceKind.STRATEGY: 'strategy_context',
        WorkspaceSourceKind.JOB_DESCRIPTION: 'role_reference',
        WorkspaceSourceKind.EXISTING_MATRIX: 'existing_matrix',
        WorkspaceSourceKind.ORG_CSV: 'org_structure',
        WorkspaceSourceKind.EMPLOYEE_CV: 'employee_profile',
    }.get(source_kind, 'reference_material')


def _build_chunk_metadata(
    source: WorkspaceSource,
    *,
    source_origin: dict,
    language_code: str,
    section_index: Optional[int] = None,
    section_heading: str = '',
    page_number: Optional[int] = None,
) -> dict:
    metadata = {
        'source_kind': source.source_kind,
        'chunk_family': _chunk_family_for_source_kind(source.source_kind),
        'transport': source.transport,
        'source_title': source.title,
        'source_origin': source_origin,
    }
    if language_code:
        metadata['language_code'] = language_code
    if section_index is not None:
        metadata['section_index'] = section_index
    if section_heading:
        metadata['section_heading'] = section_heading
    if page_number is not None:
        metadata['page_number'] = page_number
    return metadata


def _merge_source_metadata(
    existing_metadata: Optional[dict],
    *,
    source: WorkspaceSource,
    extra: Optional[dict] = None,
) -> dict:
    metadata = dict(existing_metadata or {})
    source_uuid = str(source.uuid)

    source_uuids = list(metadata.get('source_uuids') or [])
    if source_uuid not in source_uuids:
        source_uuids.append(source_uuid)
    metadata['source_uuids'] = source_uuids
    metadata['source_uuid'] = source_uuid
    metadata['source_kind'] = source.source_kind

    if source.language_code:
        language_codes = list(metadata.get('language_codes') or [])
        if source.language_code not in language_codes:
            language_codes.append(source.language_code)
        metadata['language_codes'] = language_codes
        metadata['language_code'] = source.language_code

    if extra:
        metadata.update(extra)
    return metadata


def _normalize_label(value: str) -> str:
    return re.sub(r'\s+', ' ', (value or '').strip())


def _sniff_csv_delimiter(csv_text: str) -> str:
    sample = csv_text[:2048]
    delimiter = ','
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=',;\t')
        delimiter = dialect.delimiter
    except Exception:
        if sample.count('\t') > max(sample.count(';'), sample.count(',')):
            delimiter = '\t'
        else:
            delimiter = ';' if sample.count(';') > sample.count(',') else ','
    return delimiter


def _read_csv_rows(csv_text: str) -> tuple[str, list[str], list[dict[str, Any]]]:
    delimiter = _sniff_csv_delimiter(csv_text)
    reader = csv.reader(io.StringIO(csv_text), delimiter=delimiter)
    raw_rows = [
        [str(value or '') for value in row]
        for row in reader
        if any(str(value or '').strip() for value in row)
    ]
    if not raw_rows:
        return delimiter, [], []

    first_row = raw_rows[0]
    headers = [header for header in first_row if str(header).strip()]
    if _should_use_headerless_org_csv_fallback(raw_rows):
        synthetic_headers = _build_headerless_org_csv_headers(len(first_row))
        rows = []
        for row_index, row in enumerate(raw_rows, start=1):
            rows.append(
                {
                    'row_index': row_index,
                    'data': {
                        header: row[column_index] if column_index < len(row) else ''
                        for column_index, header in enumerate(synthetic_headers)
                    },
                }
            )
        return delimiter, synthetic_headers, rows

    dict_reader = csv.DictReader(io.StringIO(csv_text), delimiter=delimiter)
    rows = []
    for row_index, row in enumerate(dict_reader, start=2):
        if any((value or '').strip() for value in row.values()):
            rows.append(
                {
                    'row_index': row_index,
                    'data': row,
                }
            )
    return delimiter, headers, rows


def _looks_like_person_name(value: str) -> bool:
    text = _normalize_label(value)
    if not text or any(char.isdigit() for char in text):
        return False
    tokens = re.findall(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё'`-]*", text)
    if len(tokens) < 2 or len(tokens) > 4:
        return False
    return all(len(token) >= 2 for token in tokens)


def _should_use_headerless_org_csv_fallback(raw_rows: list[list[str]]) -> bool:
    sample_rows = raw_rows[: min(10, len(raw_rows))]
    if not sample_rows:
        return False
    widths = {len(row) for row in sample_rows}
    if len(widths) != 1:
        return False
    width = widths.pop()
    if width < 4:
        return False

    first_row = sample_rows[0]
    normalized_first_row = [normalize_header(value) for value in first_row]
    header_like_cells = sum(
        1
        for cell in normalized_first_row
        if any(_score_header_match(target, cell) > 0 for target in _HEADER_ALIASES)
    )
    if header_like_cells >= 2:
        return False

    first_column_numeric = sum(1 for row in sample_rows if row and re.fullmatch(r'\d+', _normalize_label(row[0]) or '')) >= max(2, len(sample_rows) - 1)
    second_column_names = (
        width >= 2
        and sum(1 for row in sample_rows if len(row) > 1 and _looks_like_person_name(row[1])) >= max(2, len(sample_rows) - 1)
    )
    third_column_numeric = (
        width >= 3
        and sum(
            1
            for row in sample_rows
            if len(row) > 2 and (not _normalize_label(row[2]) or re.fullmatch(r'\d+', _normalize_label(row[2]) or ''))
        ) >= max(2, len(sample_rows) - 1)
    )
    return first_column_numeric and second_column_names and third_column_numeric


def _build_headerless_org_csv_headers(column_count: int) -> list[str]:
    base_headers = ['employee_id', 'full_name', 'supervisor_id', 'projects', 'title']
    if column_count <= len(base_headers):
        return base_headers[:column_count]
    extra_headers = [f'extra_column_{index}' for index in range(1, column_count - len(base_headers) + 1)]
    return [*base_headers, *extra_headers]


def _build_csv_mapping_warnings(mapping_details: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for target, headers in sorted((mapping_details.get('ambiguous_targets') or {}).items()):
        warnings.append(
            f"CSV mapping for '{target}' is ambiguous across headers: {', '.join(headers)}."
        )

    missing_targets = set(mapping_details.get('missing_targets') or [])
    for target in sorted(missing_targets):
        if target in _REQUIRED_CSV_MAPPING_TARGETS:
            warnings.append(f"Required CSV mapping target '{target}' is missing.")
        else:
            warnings.append(f"Optional CSV mapping target '{target}' is missing.")
    return warnings


def _build_org_csv_row_provenance(
    *,
    source: WorkspaceSource,
    row_index: int,
    row: dict[str, Any],
    mapping: dict[str, str],
    fields: Optional[list[str]] = None,
    captured_at: str,
) -> dict[str, Any]:
    selected_fields: dict[str, str] = {}
    for target in fields or []:
        header = mapping.get(target)
        if not header:
            continue
        value = _normalize_label(str(row.get(header, '') or ''))
        if value:
            selected_fields[target] = value

    snippet = ' | '.join(f'{key}: {value}' for key, value in selected_fields.items())[:500]
    return {
        'source_uuid': str(source.uuid),
        'source_title': source.title or source.source_kind,
        'source_kind': source.source_kind,
        'row_index': row_index,
        'fields': selected_fields,
        'snippet': snippet,
        'captured_at': captured_at,
    }


def _merge_provenance_example(
    metadata: dict[str, Any],
    provenance: dict[str, Any],
    *,
    key: str = 'org_csv_row_examples',
    max_items: int = 3,
) -> None:
    examples = list(metadata.get(key) or [])
    dedupe_key = (
        provenance.get('source_uuid'),
        provenance.get('row_index'),
        provenance.get('snippet'),
    )
    retained = [
        item
        for item in examples
        if (
            item.get('source_uuid'),
            item.get('row_index'),
            item.get('snippet'),
        ) != dedupe_key
    ]
    retained.append(provenance)
    metadata[key] = retained[-max_items:]


def _apply_org_csv_provenance(
    metadata: Optional[dict],
    *,
    provenance: dict[str, Any],
    include_examples: bool = False,
) -> dict[str, Any]:
    result = dict(metadata or {})
    result['org_csv_provenance'] = provenance
    if include_examples:
        _merge_provenance_example(result, provenance)
    return result


def build_org_csv_preview_sync(
    csv_text: str,
    *,
    mapping_override: Optional[dict[str, str]] = None,
    sample_row_count: int = 5,
) -> dict[str, Any]:
    delimiter, headers, rows = _read_csv_rows(csv_text)
    mapping_details = infer_csv_mapping_details(headers, mapping_override)
    warnings = _build_csv_mapping_warnings(mapping_details)
    can_parse = 'full_name' in (mapping_details.get('effective_mapping') or {})

    return {
        'delimiter': delimiter,
        'row_count': len(rows),
        'headers': headers,
        'inferred_mapping': mapping_details.get('inferred_mapping', {}),
        'effective_mapping': mapping_details.get('effective_mapping', {}),
        'ambiguous_targets': mapping_details.get('ambiguous_targets', {}),
        'missing_targets': mapping_details.get('missing_targets', []),
        'override_applied': mapping_details.get('override_applied', {}),
        'warnings': warnings,
        'sample_rows': [dict(item['data']) for item in rows[:sample_row_count]],
        'can_parse': can_parse,
    }


async def preview_org_csv_source(
    source: WorkspaceSource,
    *,
    mapping_override: Optional[dict[str, str]] = None,
    sample_row_count: int = 5,
) -> dict[str, Any]:
    if source.source_kind != WorkspaceSourceKind.ORG_CSV:
        raise ValueError('CSV preview is only supported for org_csv sources.')

    extracted, _, _ = await extract_workspace_source_content(source)
    return build_org_csv_preview_sync(
        extracted.text,
        mapping_override=mapping_override,
        sample_row_count=sample_row_count,
    )


def import_org_csv_sync(
    source_pk,
    csv_text: str,
    mapping_override: Optional[dict[str, str]] = None,
) -> dict:
    source = WorkspaceSource.objects.select_related('workspace').get(pk=source_pk)
    workspace = source.workspace
    with transaction.atomic():
        delimiter, headers, row_entries = _read_csv_rows(csv_text)
        mapping_details = infer_csv_mapping_details(headers, mapping_override)
        mapping = mapping_details.get('effective_mapping', {})
        warnings = _build_csv_mapping_warnings(mapping_details)
        if 'full_name' not in mapping:
            raise ValueError('Could not identify a full_name column in the org CSV. Provide a mapping override and retry.')

        ReportingLine.objects.filter(source=source).delete()
        EmployeeOrgAssignment.objects.filter(source=source).delete()
        EmployeeProjectAssignment.objects.filter(source=source).delete()

        employees_by_external_id: dict[str, Employee] = {}
        employees_by_name: dict[str, Employee] = {}
        employees_by_email: dict[str, Employee] = {}
        department_leaders: dict[str, Employee] = {}
        department_leader_manager_labels: dict[str, str] = {}
        captured_at = datetime.now(timezone.utc).isoformat()

        created_employees = 0
        org_units_seen: set[tuple[str, str]] = set()
        projects_seen: set[str] = set()
        reporting_lines_created = 0
        inferred_reporting_lines_created = 0
        project_assignments_created = 0
        org_assignments_created = 0
        department_lead_count = 0

        seen_employee_ids: set = set()
        seen_org_unit_ids: set = set()
        seen_project_ids: set = set()

        for row_entry in row_entries:
            row = row_entry['data']
            row_index = row_entry['row_index']
            external_employee_id = _normalize_label(row.get(mapping.get('employee_id', ''), '') or '')
            full_name = _normalize_label(row.get(mapping.get('full_name', ''), '') or '')
            if not full_name:
                continue
            current_title = _normalize_label(row.get(mapping.get('title', ''), '') or '')
            department_name = _normalize_label(row.get(mapping.get('department', ''), '') or '')
            email = _normalize_label(row.get(mapping.get('email', ''), '') or '')
            supervisor_name_raw = _normalize_label(row.get(mapping.get('supervisor_name', ''), '') or '')
            projects_raw = _normalize_label(row.get(mapping.get('projects', ''), '') or '')
            employee_provenance = _build_org_csv_row_provenance(
                source=source,
                row_index=row_index,
                row=row,
                mapping=mapping,
                fields=['employee_id', 'full_name', 'email', 'title', 'department'],
                captured_at=captured_at,
            )

            employee = None
            if external_employee_id:
                employee = Employee.objects.filter(
                    workspace=workspace,
                    external_employee_id=external_employee_id,
                ).first()
            if employee is None and email:
                employee = Employee.objects.filter(
                    workspace=workspace,
                    email__iexact=email,
                ).first()
            if employee is None:
                employee = Employee.objects.filter(
                    workspace=workspace,
                    full_name=full_name,
                ).first()

            if employee is None:
                employee = Employee.objects.create(
                    workspace=workspace,
                    source=source,
                    external_employee_id=external_employee_id,
                    full_name=full_name,
                    email=email,
                    current_title=current_title,
                    metadata=_apply_org_csv_provenance(
                        _merge_source_metadata(
                            None,
                            source=source,
                            extra={
                                'imported_from': 'org_csv',
                                'language_code': source.language_code or '',
                            },
                        ),
                        provenance=employee_provenance,
                    ),
                )
                created_employees += 1
            else:
                update_fields = []
                if employee.source_id != source.pk:
                    employee.source = source
                    update_fields.append('source')
                if external_employee_id and employee.external_employee_id != external_employee_id:
                    employee.external_employee_id = external_employee_id
                    update_fields.append('external_employee_id')
                if email and employee.email != email:
                    employee.email = email
                    update_fields.append('email')
                if current_title and employee.current_title != current_title:
                    employee.current_title = current_title
                    update_fields.append('current_title')
                merged_metadata = _merge_source_metadata(
                    employee.metadata,
                    source=source,
                    extra={
                        'imported_from': 'org_csv',
                        'language_code': source.language_code or '',
                    },
                )
                merged_metadata = _apply_org_csv_provenance(
                    merged_metadata,
                    provenance=employee_provenance,
                )
                if merged_metadata != (employee.metadata or {}):
                    employee.metadata = merged_metadata
                    update_fields.append('metadata')
                if update_fields:
                    update_fields.append('updated_at')
                    employee.save(update_fields=update_fields)

            seen_employee_ids.add(employee.pk)
            if external_employee_id:
                employees_by_external_id[external_employee_id] = employee
            employees_by_name[full_name.lower()] = employee
            if email:
                employees_by_email[email.lower()] = employee

            if department_name:
                department_key = department_name.casefold()
                org_unit, _ = OrgUnit.objects.get_or_create(
                    workspace=workspace,
                    name=department_name,
                    unit_kind=OrgUnit.UnitKind.DEPARTMENT,
                )
                org_unit_updates = []
                if org_unit.source_id != source.pk:
                    org_unit.source = source
                    org_unit_updates.append('source')
                org_unit_provenance = _build_org_csv_row_provenance(
                    source=source,
                    row_index=row_index,
                    row=row,
                    mapping=mapping,
                    fields=['department', 'full_name', 'supervisor_name'],
                    captured_at=captured_at,
                )
                org_unit_metadata = _merge_source_metadata(
                    org_unit.metadata,
                    source=source,
                    extra={
                        'imported_from': 'org_csv',
                        'language_code': source.language_code or '',
                    },
                )
                org_unit_metadata = _apply_org_csv_provenance(
                    org_unit_metadata,
                    provenance=org_unit_provenance,
                    include_examples=True,
                )
                if is_department_lead_marker(supervisor_name_raw):
                    org_unit_metadata.update(
                        {
                            'leader_name': full_name,
                            'leader_employee_uuid': str(employee.uuid),
                        }
                    )
                if org_unit_metadata != (org_unit.metadata or {}):
                    org_unit.metadata = org_unit_metadata
                    org_unit_updates.append('metadata')
                if org_unit_updates:
                    org_unit.save(update_fields=org_unit_updates + ['updated_at'])

                seen_org_unit_ids.add(org_unit.pk)
                org_units_seen.add((org_unit.name, org_unit.unit_kind))
                _, created_assignment = EmployeeOrgAssignment.objects.get_or_create(
                    workspace=workspace,
                    employee=employee,
                    org_unit=org_unit,
                    assignment_kind=EmployeeOrgAssignment.AssignmentKind.HOME,
                    defaults={
                        'is_primary': True,
                        'title_override': current_title,
                        'source': source,
                        'metadata': _apply_org_csv_provenance(
                            _merge_source_metadata(
                                None,
                                source=source,
                                extra={
                                    'imported_from': 'org_csv',
                                    'language_code': source.language_code or '',
                                },
                            ),
                            provenance=_build_org_csv_row_provenance(
                                source=source,
                                row_index=row_index,
                                row=row,
                                mapping=mapping,
                                fields=['full_name', 'department', 'title'],
                                captured_at=captured_at,
                            ),
                        ),
                    },
                )
                if created_assignment:
                    org_assignments_created += 1

                if is_department_lead_marker(supervisor_name_raw):
                    department_leaders[department_key] = employee
                    department_lead_count += 1
                    manager_label = clean_supervisor_label(supervisor_name_raw)
                    if manager_label:
                        department_leader_manager_labels[department_key] = manager_label

            for project_name in split_projects(projects_raw):
                normalized_project_name = _normalize_label(project_name)
                if not normalized_project_name:
                    continue
                project, _ = Project.objects.get_or_create(
                    workspace=workspace,
                    name=normalized_project_name,
                )
                project_updates = []
                if project.source_id != source.pk:
                    project.source = source
                    project_updates.append('source')
                project_provenance = _build_org_csv_row_provenance(
                    source=source,
                    row_index=row_index,
                    row=row,
                    mapping=mapping,
                    fields=['full_name', 'projects', 'department'],
                    captured_at=captured_at,
                )
                project_metadata = _merge_source_metadata(
                    project.metadata,
                    source=source,
                    extra={
                        'imported_from': 'org_csv',
                        'language_code': source.language_code or '',
                    },
                )
                project_metadata = _apply_org_csv_provenance(
                    project_metadata,
                    provenance=project_provenance,
                    include_examples=True,
                )
                if project_metadata != (project.metadata or {}):
                    project.metadata = project_metadata
                    project_updates.append('metadata')
                if project_updates:
                    project.save(update_fields=project_updates + ['updated_at'])

                seen_project_ids.add(project.pk)
                projects_seen.add(project.name)
                _, created_project_assignment = EmployeeProjectAssignment.objects.get_or_create(
                    workspace=workspace,
                    employee=employee,
                    project=project,
                    role_label=current_title,
                    defaults={
                        'source': source,
                        'metadata': _apply_org_csv_provenance(
                            _merge_source_metadata(
                                None,
                                source=source,
                                extra={
                                    'imported_from': 'org_csv',
                                    'language_code': source.language_code or '',
                                },
                            ),
                            provenance=_build_org_csv_row_provenance(
                                source=source,
                                row_index=row_index,
                                row=row,
                                mapping=mapping,
                                fields=['full_name', 'projects', 'title'],
                                captured_at=captured_at,
                            ),
                        ),
                    },
                )
                if created_project_assignment:
                    project_assignments_created += 1

        for row_entry in row_entries:
            row = row_entry['data']
            row_index = row_entry['row_index']
            full_name = _normalize_label(row.get(mapping.get('full_name', ''), '') or '')
            if not full_name:
                continue
            employee = employees_by_name.get(full_name.lower())
            if employee is None:
                continue

            supervisor_id = _normalize_label(row.get(mapping.get('supervisor_id', ''), '') or '')
            supervisor_name_raw = _normalize_label(row.get(mapping.get('supervisor_name', ''), '') or '')
            supervisor_name = _normalize_label(clean_supervisor_label(supervisor_name_raw))
            department_name = _normalize_label(row.get(mapping.get('department', ''), '') or '')
            department_key = department_name.casefold() if department_name else ''
            inferred_from_department_lead = False

            manager = None
            if supervisor_id:
                manager = employees_by_external_id.get(supervisor_id)
                if manager is None:
                    manager = Employee.objects.filter(
                        workspace=workspace,
                        external_employee_id=supervisor_id,
                    ).first()
            if manager is None and supervisor_name:
                manager = employees_by_name.get(supervisor_name.lower())
                if manager is None:
                    manager = Employee.objects.filter(
                        workspace=workspace,
                        full_name__iexact=supervisor_name,
                    ).first()
                if manager is None:
                    manager = employees_by_email.get(supervisor_name.lower())
                if manager is None:
                    manager = Employee.objects.filter(
                        workspace=workspace,
                        current_title__iexact=supervisor_name,
                    ).first()

            if manager is None and is_department_lead_marker(supervisor_name_raw) and department_key:
                manager_label = department_leader_manager_labels.get(department_key, '')
                if manager_label:
                    manager = employees_by_name.get(manager_label.lower())
                    if manager is None:
                        manager = Employee.objects.filter(
                            workspace=workspace,
                            full_name__iexact=manager_label,
                        ).first()
                    if manager is None:
                        manager = Employee.objects.filter(
                            workspace=workspace,
                            current_title__iexact=manager_label,
                        ).first()

            if manager is None and department_key and not is_department_lead_marker(supervisor_name_raw):
                manager = department_leaders.get(department_key)
                inferred_from_department_lead = manager is not None

            if manager and manager.pk != employee.pk:
                _, created_rl = ReportingLine.objects.get_or_create(
                    workspace=workspace,
                    manager=manager,
                    report=employee,
                    defaults={
                        'source': source,
                        'metadata': _apply_org_csv_provenance(
                            _merge_source_metadata(
                                None,
                                source=source,
                                extra={
                                    'imported_from': 'org_csv',
                                    'language_code': source.language_code or '',
                                    'inferred_from_department_lead': inferred_from_department_lead,
                                },
                            ),
                            provenance=_build_org_csv_row_provenance(
                                source=source,
                                row_index=row_index,
                                row=row,
                                mapping=mapping,
                                fields=['full_name', 'supervisor_id', 'supervisor_name', 'department'],
                                captured_at=captured_at,
                            ),
                        ),
                    },
                )
                if created_rl:
                    reporting_lines_created += 1
                    if inferred_from_department_lead:
                        inferred_reporting_lines_created += 1

        stale_employee_ids = list(
            Employee.objects.filter(source=source).exclude(pk__in=seen_employee_ids).values_list('pk', flat=True)
        )
        stale_org_unit_ids = list(
            OrgUnit.objects.filter(source=source).exclude(pk__in=seen_org_unit_ids).values_list('pk', flat=True)
        )
        stale_project_ids = list(
            Project.objects.filter(source=source).exclude(pk__in=seen_project_ids).values_list('pk', flat=True)
        )

        if stale_employee_ids:
            ReportingLine.objects.filter(workspace=workspace, manager_id__in=stale_employee_ids).delete()
            ReportingLine.objects.filter(workspace=workspace, report_id__in=stale_employee_ids).delete()
            EmployeeOrgAssignment.objects.filter(employee_id__in=stale_employee_ids).delete()
            EmployeeProjectAssignment.objects.filter(employee_id__in=stale_employee_ids).delete()
            Employee.objects.filter(pk__in=stale_employee_ids).delete()
        if stale_org_unit_ids:
            EmployeeOrgAssignment.objects.filter(org_unit_id__in=stale_org_unit_ids).delete()
            OrgUnit.objects.filter(pk__in=stale_org_unit_ids).delete()
        if stale_project_ids:
            EmployeeProjectAssignment.objects.filter(project_id__in=stale_project_ids).delete()
            Project.objects.filter(pk__in=stale_project_ids).delete()

        return {
            'row_count': len(row_entries),
            'delimiter': delimiter,
            'column_mapping': mapping,
            'inferred_mapping': mapping_details.get('inferred_mapping', {}),
            'ambiguous_targets': mapping_details.get('ambiguous_targets', {}),
            'missing_targets': mapping_details.get('missing_targets', []),
            'override_applied': mapping_details.get('override_applied', {}),
            'employees_created': created_employees,
            'employees_deleted': len(stale_employee_ids),
            'org_units_deleted': len(stale_org_unit_ids),
            'projects_deleted': len(stale_project_ids),
            'org_unit_count': len(org_units_seen),
            'department_lead_count': department_lead_count,
            'project_count': len(projects_seen),
            'org_assignments_created': org_assignments_created,
            'project_assignments_created': project_assignments_created,
            'reporting_lines_created': reporting_lines_created,
            'inferred_reporting_lines_created': inferred_reporting_lines_created,
            'warnings': warnings,
        }


def split_projects(projects_raw: str) -> list[str]:
    if not projects_raw:
        return []
    items = re.split(r'[;,\n]+', projects_raw)
    return [item.strip() for item in items if item.strip()]


def clean_supervisor_label(value: str) -> str:
    if not value:
        return ''
    lowered = value.strip()
    if lowered.lower().startswith('да'):
        parts = re.split(r'[—\-:]', lowered, maxsplit=1)
        if len(parts) > 1:
            return parts[1].strip()
        return ''
    return lowered


def is_department_lead_marker(value: str) -> bool:
    return bool(value and value.strip().lower().startswith('да'))


def infer_cv_metadata(text: str) -> dict:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    preview = lines[:5]
    candidate_name = ''
    for line in preview:
        if len(line) > 80:
            continue
        tokens = [token for token in re.split(r'\s+', line) if token]
        if 2 <= len(tokens) <= 4 and not any(char.isdigit() for char in line):
            candidate_name = line
            break
    result = {'preview_lines': preview}
    if candidate_name:
        result['candidate_name'] = candidate_name
    return result


def _upsert_employee_from_cv_sync(source_pk: str, cv_metadata: dict) -> None:
    candidate_name = (cv_metadata or {}).get('candidate_name')
    if not candidate_name:
        return
    source = WorkspaceSource.objects.select_related('workspace').get(pk=source_pk)
    employee, _ = Employee.objects.get_or_create(
        workspace=source.workspace,
        full_name=candidate_name,
        defaults={
            'source': source,
            'metadata': _merge_source_metadata(
                {'cv_source_uuids': [str(source.uuid)]},
                source=source,
            ),
        },
    )
    metadata = dict(employee.metadata or {})
    cv_sources = metadata.get('cv_source_uuids', [])
    if str(source.uuid) not in cv_sources:
        cv_sources.append(str(source.uuid))
    metadata['cv_source_uuids'] = cv_sources
    employee.metadata = _merge_source_metadata(metadata, source=source)
    update_fields = ['metadata', 'updated_at']
    if employee.source_id is None:
        employee.source = source
        update_fields.insert(0, 'source')
    employee.save(update_fields=update_fields)


def _finalize_parsed_source_sync(parsed_source_pk, parse_metadata: dict[str, Any]) -> None:
    parsed_source = ParsedSource.objects.select_related('source').get(pk=parsed_source_pk)
    parsed_source.metadata = dict(parse_metadata or {})
    parsed_source.save(update_fields=['metadata', 'updated_at'])

    source = parsed_source.source
    source.status = WorkspaceSourceStatus.PARSED
    source.parse_error = ''
    source.parse_metadata = dict(parse_metadata or {})
    source.save(update_fields=['status', 'parse_error', 'parse_metadata', 'updated_at'])


def save_parsed_source_sync(
    source_pk: str,
    extracted_text: str,
    content_type: str,
    page_count: Optional[int],
    metadata: dict,
    chunk_payloads: list[dict],
    mark_source_parsed: bool = True,
):
    with transaction.atomic():
        source = WorkspaceSource.objects.select_related('workspace').get(pk=source_pk)
        parsed_source, _ = ParsedSource.objects.update_or_create(
            source=source,
            defaults={
                'workspace': source.workspace,
                'parser_name': (metadata.get('parser') or {}).get('name', 'prototype-v1'),
                'parser_version': (metadata.get('parser') or {}).get('version', '1.0'),
                'content_type': content_type,
                'page_count': page_count,
                'word_count': len(extracted_text.split()),
                'char_count': len(extracted_text),
                'extracted_text': extracted_text,
                'metadata': metadata,
            },
        )

        SourceChunk.objects.filter(parsed_source=parsed_source).delete()
        SourceChunk.objects.bulk_create(
            [
                SourceChunk(
                    parsed_source=parsed_source,
                    chunk_index=item['index'],
                    text=item['text'],
                    char_count=item['char_count'],
                    metadata=item.get('metadata', {'source_kind': source.source_kind}),
                )
                for item in chunk_payloads
            ]
        )

        if mark_source_parsed:
            source.status = WorkspaceSourceStatus.PARSED
            source.parse_error = ''
            source.parse_metadata = metadata
            source.save(update_fields=['status', 'parse_error', 'parse_metadata', 'updated_at'])
        return parsed_source
