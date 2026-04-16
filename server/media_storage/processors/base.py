from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class ProcessorResult:
    analysis_kind: str
    summary_text: str
    result_json: Dict[str, Any]
    confidence: float = 1.0


class BaseFileProcessor:
    async def run_baseline(
        self,
        file_bytes: bytes,
        media_file,
        tier: int,
        analysis_kinds: List[str],
        call_context: Optional[object] = None,
    ) -> List[ProcessorResult]:
        raise NotImplementedError
