import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import httpx
from django.conf import settings

from .wrapper import uses_responses_api_model

logger = logging.getLogger(__name__)

_CHAT_COMPLETIONS_URL = 'https://api.openai.com/v1/chat/completions'
_RESPONSES_URL = 'https://api.openai.com/v1/responses'
_DEFAULT_TIMEOUT = 120.0
_DEFAULT_MODEL = 'gpt-4o-mini'


class StructuredLLMError(RuntimeError):
    """Raised when a structured LLM request cannot be completed."""


@dataclass
class StructuredLLMResult:
    parsed: dict[str, Any]
    raw_response: dict[str, Any]
    model: str
    request_id: str = ''
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class OpenAIStructuredClient:
    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self.api_key = api_key or getattr(settings, 'OPENAI_API_KEY', None)
        self.model = model or getattr(settings, 'UPG_FLOW_MODEL', None) or _DEFAULT_MODEL
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise StructuredLLMError('OPENAI_API_KEY is not configured for prototype flow calls.')
        return {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        }

    async def create_json_response(
        self,
        *,
        messages: list[dict[str, Any]],
        schema_name: str,
        schema: dict[str, Any],
        temperature: float = 0.2,
        max_tokens: int = 4000,
    ) -> StructuredLLMResult:
        if uses_responses_api_model(self.model):
            return await self._create_responses_json_response(
                messages=messages,
                schema_name=schema_name,
                schema=schema,
                temperature=temperature,
                max_tokens=max_tokens,
            )

        return await self._create_chat_json_response(
            messages=messages,
            schema_name=schema_name,
            schema=schema,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    async def _create_chat_json_response(
        self,
        *,
        messages: list[dict[str, Any]],
        schema_name: str,
        schema: dict[str, Any],
        temperature: float,
        max_tokens: int,
    ) -> StructuredLLMResult:
        payload = {
            'model': self.model,
            'messages': messages,
            'temperature': temperature,
            'max_tokens': max_tokens,
            'response_format': {
                'type': 'json_schema',
                'json_schema': {
                    'name': _normalize_schema_name(schema_name),
                    'strict': True,
                    'schema': schema,
                },
            },
        }
        body, request_id = await self._post_json(_CHAT_COMPLETIONS_URL, payload)

        choices = body.get('choices') or []
        if not choices:
            raise StructuredLLMError('OpenAI returned no choices for structured response.')

        message = (choices[0] or {}).get('message') or {}
        refusal = message.get('refusal')
        if refusal:
            raise StructuredLLMError(f'OpenAI refused the request: {refusal}')

        content = _extract_chat_message_text(message)
        if not content:
            raise StructuredLLMError('OpenAI returned an empty structured response.')

        parsed = _parse_json_text(content)
        usage = body.get('usage') or {}
        return StructuredLLMResult(
            parsed=parsed,
            raw_response=body,
            model=body.get('model') or self.model,
            request_id=request_id or body.get('id') or '',
            prompt_tokens=usage.get('prompt_tokens') or 0,
            completion_tokens=usage.get('completion_tokens') or 0,
            total_tokens=usage.get('total_tokens') or 0,
        )

    async def _create_responses_json_response(
        self,
        *,
        messages: list[dict[str, Any]],
        schema_name: str,
        schema: dict[str, Any],
        temperature: float,
        max_tokens: int,
    ) -> StructuredLLMResult:
        instructions, input_items = _messages_to_responses_input(messages)
        if not input_items:
            raise StructuredLLMError('Structured response request had no user or assistant messages to send.')

        payload = {
            'model': self.model,
            'input': input_items,
            'temperature': temperature,
            'max_output_tokens': max_tokens,
            'text': {
                'format': {
                    'type': 'json_schema',
                    'name': _normalize_schema_name(schema_name),
                    'schema': schema,
                    'strict': True,
                }
            },
        }
        if instructions:
            payload['instructions'] = instructions

        body, request_id = await self._post_json(_RESPONSES_URL, payload)

        status = body.get('status')
        if status and status != 'completed':
            incomplete = body.get('incomplete_details') or {}
            error = body.get('error') or {}
            detail = error.get('message') or incomplete.get('reason') or status
            raise StructuredLLMError(f'Responses API request did not complete successfully: {detail}')

        content = _extract_responses_output_text(body)
        parsed = _parse_json_text(content)
        usage = body.get('usage') or {}
        prompt_tokens = usage.get('input_tokens') or 0
        completion_tokens = usage.get('output_tokens') or 0
        total_tokens = usage.get('total_tokens') or (prompt_tokens + completion_tokens)
        return StructuredLLMResult(
            parsed=parsed,
            raw_response=body,
            model=body.get('model') or self.model,
            request_id=request_id or body.get('id') or '',
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

    async def _post_json(self, url: str, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                url,
                headers=self._headers(),
                json=payload,
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error('Structured LLM request failed: %s', exc.response.text)
            raise StructuredLLMError(
                f'OpenAI request failed with status {exc.response.status_code}: {exc.response.text}'
            ) from exc
        return response.json(), response.headers.get('x-request-id', '')


def _normalize_schema_name(schema_name: str) -> str:
    sanitized = ''.join(
        char if char.isalnum() or char in {'_', '-'} else '_'
        for char in (schema_name or 'structured_response')
    )
    sanitized = sanitized.strip('_-') or 'structured_response'
    return sanitized[:64]


def _normalize_message_text(content: Any) -> str:
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            text = item.get('text')
            if isinstance(text, str) and text:
                parts.append(text)
                continue
            if item.get('type') == 'text' and isinstance(item.get('content'), str):
                parts.append(item['content'])
        return '\n'.join(part for part in parts if part).strip()
    if isinstance(content, dict):
        text = content.get('text')
        if isinstance(text, str):
            return text
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def _extract_chat_message_text(message: dict[str, Any]) -> str:
    content = message.get('content')
    return _normalize_message_text(content)


def _messages_to_responses_input(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    instructions_parts: list[str] = []
    input_items: list[dict[str, Any]] = []

    for message in messages:
        role = (message.get('role') or 'user').strip().lower()
        text = _normalize_message_text(message.get('content'))
        if not text:
            continue

        if role in {'system', 'developer'}:
            instructions_parts.append(text)
            continue

        normalized_role = role if role in {'user', 'assistant'} else 'user'
        input_items.append(
            {
                'role': normalized_role,
                'content': [{'type': 'input_text', 'text': text}],
            }
        )

    instructions = '\n\n'.join(part for part in instructions_parts if part).strip()
    return instructions, input_items


def _extract_responses_output_text(body: dict[str, Any]) -> str:
    output_text = body.get('output_text')
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    for item in body.get('output') or []:
        if (item or {}).get('type') != 'message':
            continue
        for content_part in (item or {}).get('content') or []:
            part_type = (content_part or {}).get('type')
            if part_type == 'output_text':
                text = (content_part or {}).get('text') or ''
                if text:
                    return text
            if part_type == 'refusal':
                refusal = (content_part or {}).get('refusal') or (content_part or {}).get('text') or 'unknown refusal'
                raise StructuredLLMError(f'OpenAI refused the request: {refusal}')

    raise StructuredLLMError('Responses API returned no structured text content.')


def _parse_json_text(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise StructuredLLMError(
            f'OpenAI response was not valid JSON: {content[:500]}'
        ) from exc
    if not isinstance(parsed, dict):
        raise StructuredLLMError('OpenAI structured response must be a JSON object.')
    return parsed


async def call_openai_structured(
    *,
    system_prompt: str,
    user_prompt: str,
    schema_name: str,
    schema: dict[str, Any],
    model: Optional[str] = None,
    temperature: float = 0.2,
    max_tokens: int = 4000,
    additional_messages: Optional[Iterable[dict[str, Any]]] = None,
    timeout: Optional[float] = None,
) -> StructuredLLMResult:
    messages = [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': user_prompt},
    ]
    if additional_messages:
        messages.extend(list(additional_messages))

    client = OpenAIStructuredClient(model=model, timeout=timeout or _DEFAULT_TIMEOUT)
    return await client.create_json_response(
        messages=messages,
        schema_name=schema_name,
        schema=schema,
        temperature=temperature,
        max_tokens=max_tokens,
    )
