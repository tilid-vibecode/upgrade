import logging
from typing import List, Optional

from .base import BaseFileProcessor, ProcessorResult

logger = logging.getLogger(__name__)


class TextProcessor(BaseFileProcessor):
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

        from media_storage.constants import MAX_TEXT_BYTES

        text = ''
        for encoding in ('utf-8', 'utf-8-sig', 'latin-1', 'cp1252', 'cp1251', 'windows-1251'):
            try:
                text = file_bytes[:MAX_TEXT_BYTES].decode(encoding)
                break
            except (UnicodeDecodeError, ValueError):
                continue

        if not text:
            logger.warning('Could not decode text file %s', media_file.uuid)
            return []

        results: list[ProcessorResult] = []
        if 'text_extraction' in analysis_kinds:
            results.append(self._extract(text, len(file_bytes), MAX_TEXT_BYTES))
        if 'text_structure' in analysis_kinds:
            results.append(self._structure(text))
        return results

    def _extract(self, text: str, raw_size: int, max_bytes: int) -> ProcessorResult:
        truncated = raw_size > max_bytes
        return ProcessorResult(
            analysis_kind='text_extraction',
            summary_text=f'Text: {len(text):,} chars' + (' (truncated)' if truncated else ''),
            result_json={
                'text': text[:500_000],
                'total_chars': len(text),
                'raw_size_bytes': raw_size,
                'truncated': truncated,
            },
        )

    def _structure(self, text: str) -> ProcessorResult:
        lines = text.splitlines()
        non_empty_lines = [line for line in lines if line.strip()]
        words = text.split()

        detected_format = 'plain'
        if any(line.startswith('#') for line in lines[:20]):
            detected_format = 'markdown'
        elif any(line.strip().startswith(('def ', 'class ', 'import ', 'from ')) for line in lines[:30]):
            detected_format = 'code'
        elif text.strip().startswith('{') or text.strip().startswith('['):
            detected_format = 'json'

        result = {
            'line_count': len(lines),
            'non_empty_lines': len(non_empty_lines),
            'word_count': len(words),
            'char_count': len(text),
            'detected_format': detected_format,
        }
        summary = f'Text: {len(lines)} lines, {len(words)} words ({detected_format})'
        return ProcessorResult(
            analysis_kind='text_structure',
            summary_text=summary,
            result_json=result,
        )
