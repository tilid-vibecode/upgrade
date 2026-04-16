import io
import logging
from typing import List, Optional

from .base import BaseFileProcessor, ProcessorResult

logger = logging.getLogger(__name__)


class ImageProcessor(BaseFileProcessor):
    async def run_baseline(
        self,
        file_bytes: bytes,
        media_file,
        tier: int,
        analysis_kinds: List[str],
        call_context: Optional[object] = None,
    ) -> List[ProcessorResult]:
        if tier != 0 or 'image_metadata' not in analysis_kinds:
            return []

        try:
            from PIL import Image
        except ImportError:
            logger.warning('Pillow is not installed, skipping image analysis for %s', media_file.uuid)
            return []

        try:
            image = Image.open(io.BytesIO(file_bytes))
        except Exception as exc:
            logger.warning('Failed to open image %s: %s', media_file.uuid, exc)
            return []

        megapixels = round((image.width * image.height) / 1_000_000, 2)
        result = {
            'width': image.width,
            'height': image.height,
            'mode': image.mode,
            'format': image.format,
            'megapixels': megapixels,
        }
        summary = f'{image.width}x{image.height} {image.format or "image"} ({megapixels}MP)'
        return [
            ProcessorResult(
                analysis_kind='image_metadata',
                summary_text=summary,
                result_json=result,
            )
        ]
