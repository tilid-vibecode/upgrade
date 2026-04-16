import io
import logging
from typing import List, Optional

from .base import BaseFileProcessor, ProcessorResult

logger = logging.getLogger(__name__)


class PdfProcessor(BaseFileProcessor):
    async def run_baseline(
        self,
        file_bytes: bytes,
        media_file,
        tier: int,
        analysis_kinds: List[str],
        call_context: Optional[object] = None,
    ) -> List[ProcessorResult]:
        if tier != 0:
            return []

        import pdfplumber

        from media_storage.constants import MAX_PDF_PAGES_BLOCKING

        try:
            pdf = pdfplumber.open(io.BytesIO(file_bytes))
        except Exception as exc:
            logger.warning('Failed to open PDF %s: %s', media_file.uuid, exc)
            return []

        results: list[ProcessorResult] = []
        try:
            page_count = len(pdf.pages)
            if 'pdf_metadata' in analysis_kinds:
                results.append(self._extract_metadata(pdf, page_count))
            if 'pdf_text_extraction' in analysis_kinds:
                results.append(self._extract_text(pdf, file_bytes, page_count, MAX_PDF_PAGES_BLOCKING))
            if 'pdf_scan_detection' in analysis_kinds:
                results.append(self._detect_scanned(pdf, page_count))
            return results
        finally:
            pdf.close()

    def _extract_metadata(self, pdf, page_count: int) -> ProcessorResult:
        metadata = pdf.metadata or {}
        result = {
            'page_count': page_count,
            'creator': str(metadata.get('Creator', ''))[:200],
            'producer': str(metadata.get('Producer', ''))[:200],
            'title': str(metadata.get('Title', ''))[:200],
            'author': str(metadata.get('Author', ''))[:200],
        }
        summary = f'PDF: {page_count} pages'
        if result['title']:
            summary += f', title="{result["title"][:50]}"'
        return ProcessorResult(
            analysis_kind='pdf_metadata',
            summary_text=summary,
            result_json=result,
        )

    def _extract_text(self, pdf, file_bytes: bytes, page_count: int, max_pages: int) -> ProcessorResult:
        from pypdf import PdfReader

        pages_to_extract = min(page_count, max_pages)
        text_parts: list[str] = []
        page_texts: list[dict[str, object]] = []
        total_chars = 0

        for index in range(pages_to_extract):
            page_text = pdf.pages[index].extract_text() or ''
            if page_text:
                text_parts.append(page_text)
                total_chars += len(page_text)
                page_texts.append(
                    {
                        'page_number': index + 1,
                        'text': page_text,
                        'char_count': len(page_text),
                    }
                )

        full_text = '\n\n'.join(text_parts).strip()
        if not full_text:
            reader = PdfReader(io.BytesIO(file_bytes))
            fallback_parts: list[str] = []
            page_texts = []
            for index in range(min(len(reader.pages), max_pages)):
                page_text = reader.pages[index].extract_text() or ''
                if page_text:
                    fallback_parts.append(page_text)
                    page_texts.append(
                        {
                            'page_number': index + 1,
                            'text': page_text,
                            'char_count': len(page_text),
                        }
                    )
            full_text = '\n\n'.join(fallback_parts).strip()
            total_chars = len(full_text)

        truncated = page_count > max_pages
        result = {
            'text': full_text[:500_000],
            'page_texts': page_texts[:200],
            'total_chars': total_chars,
            'pages_extracted': pages_to_extract,
            'total_pages': page_count,
            'truncated': truncated,
        }
        summary = (
            f'Extracted {total_chars:,} chars from {pages_to_extract}/{page_count} pages'
            + (' (truncated)' if truncated else '')
        )
        return ProcessorResult(
            analysis_kind='pdf_text_extraction',
            summary_text=summary,
            result_json=result,
        )

    def _detect_scanned(self, pdf, page_count: int) -> ProcessorResult:
        sample_pages = min(page_count, 5)
        chars_per_page: list[int] = []
        for index in range(sample_pages):
            page_text = pdf.pages[index].extract_text() or ''
            chars_per_page.append(len(page_text.strip()))

        avg_chars = sum(chars_per_page) / max(len(chars_per_page), 1)
        is_scanned = avg_chars < 50
        result = {
            'is_scanned': is_scanned,
            'avg_chars_per_page': round(avg_chars, 1),
            'sample_pages': sample_pages,
            'chars_per_page': chars_per_page,
        }
        summary = f'{"Scanned" if is_scanned else "Text-based"} PDF (avg {avg_chars:.0f} chars/page)'
        return ProcessorResult(
            analysis_kind='pdf_scan_detection',
            summary_text=summary,
            result_json=result,
        )
