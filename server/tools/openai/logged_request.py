import asyncio
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

from openai.types.chat import ChatCompletion

from .agent import LLMUsageInfo, chat_json_request

logger = logging.getLogger(__name__)
_usage_logging_unavailable_warned = False


class LLMCallType(str, Enum):
    COMPLETION = 'completion'
    TOOL_CALL = 'tool_call'
    TOOL_FOLLOWUP = 'tool_followup'
    VALIDATION_RETRY = 'validation_retry'
    ITERATION_RETRY = 'iteration_retry'


@dataclass
class LLMCallContext:
    organization_uuid: str = ''
    user_uuid: str = ''
    discussion_uuid: Optional[str] = None
    is_org_member: bool = True


@dataclass
class LLMCallMeta:
    call_type: LLMCallType = LLMCallType.COMPLETION
    caller_function: str = ''
    tool_names: List[str] = field(default_factory=list)
    iteration: int = 0
    attempt: int = 0


async def logged_chat_request(
    messages: List[dict],
    model: str,
    provider: str,
    call_context: Optional[LLMCallContext] = None,
    call_meta: Optional[LLMCallMeta] = None,
    tools: Optional[List[dict]] = None,
    parallel_tool_calls: bool = False,
    max_tokens: Optional[int] = None,
) -> Tuple[Optional[str], Optional[Union[Dict[str, Any], ChatCompletion]], LLMUsageInfo]:
    meta = call_meta or LLMCallMeta()

    response_id, response, usage_info = await chat_json_request(
        messages=messages,
        model=model,
        tools=tools,
        parallel_tool_calls=parallel_tool_calls,
        max_tokens=max_tokens,
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

    elif hasattr(response, 'choices') and response.choices:
        choice = response.choices[0]
        tool_calls = getattr(choice.message, 'tool_calls', None)
        if tool_calls:
            resolved_call_type = LLMCallType.TOOL_CALL
            tool_names_used = [
                tc.function.name
                for tc in tool_calls
                if hasattr(tc, 'function') and hasattr(tc.function, 'name')
            ]
            is_successful = True
        else:
            error_type = 'empty_tool_response'

    if _should_emit_usage_log(call_context):
        asyncio.create_task(_guarded_emit_log(
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
        ))

    return response_id, response, usage_info


def _should_emit_usage_log(call_context: Optional[LLMCallContext]) -> bool:
    if os.getenv('LLM_USAGE_LOGGING_ENABLED', 'false').lower() != 'true':
        return False

    return bool(call_context and call_context.organization_uuid)


_LOG_SEMAPHORE = asyncio.Semaphore(
    int(__import__('os').getenv('LLM_LOG_MAX_INFLIGHT', '256'))
)
_log_drops = 0


async def _guarded_emit_log(**kwargs) -> None:
    global _log_drops

    if not _LOG_SEMAPHORE._value:
        _log_drops += 1
        if _log_drops % 100 == 1:
            logger.warning(
                'LLM usage logging backpressured — dropped %d log(s) so far.',
                _log_drops,
            )
        return

    async with _LOG_SEMAPHORE:
        await _emit_log(**kwargs)


async def _emit_log(
    call_context: LLMCallContext,
    provider: str,
    model: str,
    usage_info: LLMUsageInfo,
    is_successful: bool,
    error_type: str,
    call_type: str,
    tool_names: List[str],
    caller_function: str,
    iteration: int,
    attempt: int,
) -> None:
    global _usage_logging_unavailable_warned

    try:
        from llm_usage.redis_logger import log_llm_usage
    except ModuleNotFoundError:
        if not _usage_logging_unavailable_warned:
            logger.info('LLM usage logging is disabled because the llm_usage app is not installed.')
            _usage_logging_unavailable_warned = True
        return
    try:
        await log_llm_usage(
            organization_uuid=call_context.organization_uuid,
            user_uuid=call_context.user_uuid,
            discussion_uuid=call_context.discussion_uuid,
            is_org_member=call_context.is_org_member,
            provider=provider,
            model=model,
            prompt_tokens=usage_info.prompt_tokens,
            completion_tokens=usage_info.completion_tokens,
            total_tokens=usage_info.total_tokens,
            is_successful=is_successful,
            error_type=error_type,
            call_type=call_type,
            tool_names=tool_names,
            caller_function=caller_function,
            provider_request_id=usage_info.provider_request_id,
            iteration=iteration,
            attempt=attempt,
        )
    except Exception as exc:
        logger.error(f'Failed to emit LLM usage log: {exc}', exc_info=True)
