import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

from .agent import LLMUsageInfo, client, convert_json_to_dict

logger = logging.getLogger(__name__)


@dataclass
class ResponsesFunctionCall:
    call_id: str
    name: str
    arguments: str


@dataclass
class ResponsesToolCallResult:
    response_id: str
    raw_output: list
    function_calls: List[ResponsesFunctionCall]


def _extract_responses_usage(response) -> LLMUsageInfo:
    usage = getattr(response, 'usage', None)
    if not usage:
        return LLMUsageInfo()

    input_tokens = getattr(usage, 'input_tokens', 0) or 0
    output_tokens = getattr(usage, 'output_tokens', 0) or 0
    return LLMUsageInfo(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        provider_request_id=getattr(response, 'id', '') or '',
    )


def _extract_sources(output_items: list) -> Optional[list]:
    sources = []
    for item in output_items:
        if getattr(item, 'type', None) != 'web_search_call':
            continue
        action = getattr(item, 'action', None)
        if not action:
            continue
        action_sources = getattr(action, 'sources', None)
        if action_sources:
            sources.extend(action_sources)
    return sources or None


async def responses_json_request(
    input_items: List[Dict[str, Any]],
    model: str,
    instructions: Optional[str] = None,
    text_format: Optional[Dict[str, Any]] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    parallel_tool_calls: bool = False,
    max_output_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    include: Optional[List[str]] = None,
    store: bool = False,
    previous_response_id: Optional[str] = None,
    reasoning: Optional[Dict[str, Any]] = None,
    background: Optional[bool] = None,
    max_tool_calls: Optional[int] = None,
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
    service_tier: Optional[str] = None,
    truncation: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], Optional[Union[Dict[str, Any], ResponsesToolCallResult]], LLMUsageInfo]:
    usage_info = LLMUsageInfo()

    try:
        request_kwargs: Dict[str, Any] = {
            'model': model,
            'input': input_items,
            'store': store,
        }
        if instructions:
            request_kwargs['instructions'] = instructions
        if text_format:
            request_kwargs['text'] = text_format
        if tools:
            request_kwargs['tools'] = tools
            request_kwargs['parallel_tool_calls'] = parallel_tool_calls
        if max_output_tokens is not None:
            request_kwargs['max_output_tokens'] = max_output_tokens
        if temperature is not None:
            request_kwargs['temperature'] = temperature
        if include:
            request_kwargs['include'] = include
        if previous_response_id:
            request_kwargs['previous_response_id'] = previous_response_id
        if reasoning is not None:
            request_kwargs['reasoning'] = reasoning
        if background is not None:
            request_kwargs['background'] = background
        if max_tool_calls is not None:
            request_kwargs['max_tool_calls'] = max_tool_calls
        if tool_choice is not None:
            request_kwargs['tool_choice'] = tool_choice
        if service_tier is not None:
            request_kwargs['service_tier'] = service_tier
        if truncation is not None:
            request_kwargs['truncation'] = truncation
        if metadata is not None:
            request_kwargs['metadata'] = metadata

        response = await client.responses.create(**request_kwargs)
        usage_info = _extract_responses_usage(response)

        status = getattr(response, 'status', None)
        if status not in (None, 'completed'):
            logger.warning(
                'Responses API returned non-completed status %s for model %s (id=%s)',
                status,
                model,
                getattr(response, 'id', ''),
            )
            return getattr(response, 'id', None), None, usage_info

        output = getattr(response, 'output', None)
        if not output:
            return response.id, None, usage_info

        function_calls = [
            ResponsesFunctionCall(
                call_id=item.call_id,
                name=item.name,
                arguments=item.arguments,
            )
            for item in output
            if getattr(item, 'type', None) == 'function_call'
        ]
        if function_calls:
            return (
                response.id,
                ResponsesToolCallResult(
                    response_id=response.id,
                    raw_output=output,
                    function_calls=function_calls,
                ),
                usage_info,
            )

        for item in output:
            if getattr(item, 'type', None) != 'message':
                continue
            for content_part in getattr(item, 'content', []):
                if getattr(content_part, 'type', None) != 'output_text':
                    continue
                text = getattr(content_part, 'text', '')
                parsed = convert_json_to_dict(text)
                if parsed is not None:
                    parsed['_responses_meta'] = {
                        'raw_output': output,
                        'sources': _extract_sources(output),
                        'response_id': response.id,
                    }
                return response.id, parsed, usage_info

        return response.id, None, usage_info

    except Exception as exc:
        logger.error('Responses API request error: %s', exc)
        return None, None, usage_info
