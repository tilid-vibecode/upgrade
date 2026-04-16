import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple, Union

from .logged_request import (
    LLMCallContext,
    LLMCallMeta,
    LLMCallType,
    _guarded_emit_log,
)
from .responses_agent import (
    LLMUsageInfo,
    ResponsesToolCallResult,
    responses_json_request,
)

logger = logging.getLogger(__name__)


async def logged_responses_request(
    input_items: List[Dict[str, Any]],
    model: str,
    provider: str,
    call_context: Optional[LLMCallContext] = None,
    call_meta: Optional[LLMCallMeta] = None,
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
    meta = call_meta or LLMCallMeta()

    response_id, response, usage_info = await responses_json_request(
        input_items=input_items,
        model=model,
        instructions=instructions,
        text_format=text_format,
        tools=tools,
        parallel_tool_calls=parallel_tool_calls,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
        include=include,
        store=store,
        previous_response_id=previous_response_id,
        reasoning=reasoning,
        background=background,
        max_tool_calls=max_tool_calls,
        tool_choice=tool_choice,
        service_tier=service_tier,
        truncation=truncation,
        metadata=metadata,
    )

    is_successful = False
    error_type = ''
    resolved_call_type = meta.call_type
    tool_names_used: List[str] = []

    if response is None:
        error_type = 'no_response'
    elif isinstance(response, dict):
        if 'final_result' in response:
            is_successful = True
        else:
            error_type = 'missing_final_result'
    elif isinstance(response, ResponsesToolCallResult):
        resolved_call_type = LLMCallType.TOOL_CALL
        tool_names_used = [function_call.name for function_call in response.function_calls]
        is_successful = True

    if call_context and call_context.organization_uuid:
        asyncio.create_task(
            _guarded_emit_log(
                call_context=call_context,
                provider=provider,
                model=model,
                usage_info=usage_info,
                is_successful=is_successful,
                error_type=error_type,
                call_type=resolved_call_type.value,
                tool_names=tool_names_used or meta.tool_names,
                caller_function=meta.caller_function,
                iteration=meta.iteration,
                attempt=meta.attempt,
            )
        )

    return response_id, response, usage_info
