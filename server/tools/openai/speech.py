# File location: /server/tools/openai/speech.py
import asyncio
from typing import AsyncIterable, Optional, Tuple, Dict, Any

from openai import AsyncOpenAI
from openai.types.audio import Transcription
from server.settings import OPENAI_API_KEY

_DEFAULT_MODEL = "whisper-1"  # or "whisper-large-v3"
_OPENAI_CLIENT = AsyncOpenAI(api_key=OPENAI_API_KEY)


async def _run_openai_transcription(
    fp,  # Binary IO already at pos 0
    model: str,
    language: Optional[str],
    extra: Optional[Dict[str, Any]] = None,
) -> Transcription:
    """
    Single call to the OpenAI Audio → Text endpoint.
    Uses `response_format="verbose_json"` so that we also get `language`.
    """
    params = {
        "model": model,
        "file": fp,
        "response_format": "verbose_json",
    }
    if language:
        params["language"] = language
    params.update(extra or {})

    return await _OPENAI_CLIENT.audio.transcriptions.create(**params)


# ============  PUBLIC API  ============================================

async def transcribe_audio_stream(
    audio_iter: AsyncIterable[bytes],
    *,
    model: str = _DEFAULT_MODEL,
    language: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """
    Collects chunks from `audio_iter`, sends to OpenAI, returns
    (transcript, detected_language).
    Uses a bytes buffer → avoids tempfile issues.
    """
    # 1. Collect into memory (can switch to disk if you expect >30-40 MB)
    buf = bytearray()
    async for chunk in audio_iter:
        buf.extend(chunk)

    # 2. Construct multipart-friendly tuple (filename, bytes)
    file_tuple = ("audio.webm", bytes(buf))

    # 3. Call OpenAI
    params = {
        "model": model,
        "file": file_tuple,
        "response_format": "verbose_json",
    }
    if language:
        params["language"] = language
    params.update(extra or {})

    response: Transcription = await _OPENAI_CLIENT.audio.transcriptions.create(**params)

    data = response.model_dump()  # raw dict with all keys
    text: str = data.get("text", "")
    lang: str = data.get("language") or (language or "und")
    return text, lang


async def transcribe_audio_file(
    path: str,
    *,
    model: str = _DEFAULT_MODEL,
    language: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Convenience wrapper around the same endpoint but for a file already
    stored on disk.  Returns (text, language).
    """
    async with await asyncio.to_thread(open, path, "rb") as fp:  # open in a thread
        response = await _run_openai_transcription(
            fp, model=model, language=language
        )
    return response.text, response.language or (language or "und")
