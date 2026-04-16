import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

from server.redis_connection import get_redis_client
from .schemas import LLMUsageEntry
from .constants import (
    STREAM_KEY,
    STREAM_MAX_LEN,
    COUNTER_KEY_PREFIX,
    COUNTER_TTL_SECONDS,
    estimate_cost_micro,
)

logger = logging.getLogger(__name__)


async def log_llm_usage(
    organization_uuid: str,
    user_uuid: str,
    provider: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    is_successful: bool = False,
    error_type: str = '',
    provider_request_id: str = '',
    caller_function: str = '',
    discussion_uuid: Optional[str] = None,
    is_org_member: bool = True,
    call_type: str = 'completion',
    tool_names: Optional[List[str]] = None,
    iteration: int = 0,
    attempt: int = 0,
) -> None:
    try:
        cost = estimate_cost_micro(model, prompt_tokens, completion_tokens)
        now = datetime.now(timezone.utc)

        clean_user_uuid = user_uuid if user_uuid else None
        clean_discussion_uuid = discussion_uuid if discussion_uuid else None

        entry = LLMUsageEntry(
            organization_uuid=organization_uuid,
            user_uuid=clean_user_uuid,
            discussion_uuid=clean_discussion_uuid,
            is_org_member=is_org_member,
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            is_successful=is_successful,
            error_type=error_type,
            call_type=call_type,
            tool_names=tool_names or [],
            caller_function=caller_function,
            iteration=iteration,
            attempt=attempt,
            estimated_cost_micro=cost,
            provider_request_id=provider_request_id,
            called_at=now,
        )

        r = await get_redis_client()

        stream_data = entry.model_dump(mode='json')
        stream_data['tool_names'] = json.dumps(stream_data['tool_names'])

        for k, v in stream_data.items():
            if v is None:
                stream_data[k] = ''
            elif isinstance(v, bool):
                stream_data[k] = str(v).lower()

        await r.xadd(STREAM_KEY, stream_data, maxlen=STREAM_MAX_LEN, approximate=True)

        hour_key = now.strftime('%Y-%m-%dT%H')
        counter_hash = f'{COUNTER_KEY_PREFIX}:{organization_uuid}:{hour_key}'

        async with r.pipeline() as pipe:
            await pipe.hincrby(counter_hash, f'{model}:total_calls', 1)
            await pipe.hincrby(counter_hash, f'{model}:total_tokens', total_tokens)
            await pipe.hincrby(counter_hash, f'{model}:prompt_tokens', prompt_tokens)
            await pipe.hincrby(counter_hash, f'{model}:completion_tokens', completion_tokens)
            await pipe.hincrby(counter_hash, f'{model}:cost_micro', cost)

            if is_successful:
                await pipe.hincrby(counter_hash, f'{model}:successful_calls', 1)
            else:
                await pipe.hincrby(counter_hash, f'{model}:failed_calls', 1)

            if call_type == 'tool_call':
                await pipe.hincrby(counter_hash, f'{model}:tool_call_turns', 1)
            elif call_type == 'tool_followup':
                await pipe.hincrby(counter_hash, f'{model}:tool_followup_turns', 1)

            await pipe.expire(counter_hash, COUNTER_TTL_SECONDS)
            await pipe.execute()

    except Exception as exc:
        logger.error(f'Failed to log LLM usage to Redis: {exc}', exc_info=True)
