from typing import Optional

from .base import BaseFileProcessor

_REGISTRY: dict[str, BaseFileProcessor] = {}
_INITIALIZED = False


def _ensure_registry() -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return

    from .docx_processor import DocxProcessor
    from .image_processor import ImageProcessor
    from .pdf_processor import PdfProcessor
    from .tabular_processor import TabularProcessor
    from .text_processor import TextProcessor

    _REGISTRY.update(
        {
            'image': ImageProcessor(),
            'document': PdfProcessor(),
            'word': DocxProcessor(),
            'text': TextProcessor(),
            'spreadsheet': TabularProcessor(),
        }
    )
    _INITIALIZED = True


def get_processor(file_category: str) -> Optional[BaseFileProcessor]:
    _ensure_registry()
    return _REGISTRY.get(file_category)
