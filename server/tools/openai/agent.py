import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion

from server.settings import OPENAI_API_KEY

logger = logging.getLogger(__name__)

client = AsyncOpenAI(api_key=OPENAI_API_KEY)



@dataclass
class LLMUsageInfo:

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    provider_request_id: str = ''



def convert_json_to_dict(json_string: str) -> Optional[Dict]:
    try:
        return json.loads(json_string)
    except json.JSONDecodeError:
        logger.warning('Failed to decode JSON response')
        return None


def _extract_usage(response: ChatCompletion) -> LLMUsageInfo:
    if not response or not getattr(response, 'usage', None):
        return LLMUsageInfo()

    return LLMUsageInfo(
        prompt_tokens=response.usage.prompt_tokens or 0,
        completion_tokens=response.usage.completion_tokens or 0,
        total_tokens=response.usage.total_tokens or 0,
        provider_request_id=getattr(response, 'id', '') or '',
    )



async def chat_json_request(
    messages: List[Dict[str, str]],
    model: str = 'gpt-4o',
    tools: Optional[List[Dict]] = None,
    parallel_tool_calls: bool = False,
    max_tokens: Optional[int] = None,
) -> Tuple[Optional[str], Optional[Union[Dict[str, Any], ChatCompletion]], LLMUsageInfo]:
    usage_info = LLMUsageInfo()

    try:
        request_kwargs = {
            'model': model,
            'messages': messages,
            'response_format': {'type': 'json_object'},
        }

        if tools:
            request_kwargs['tools'] = tools
            request_kwargs['parallel_tool_calls'] = parallel_tool_calls

        if max_tokens is not None:
            request_kwargs['max_tokens'] = max_tokens

        response = await client.chat.completions.create(**request_kwargs)

        usage_info = _extract_usage(response)

        if tools:
            logger.debug(f'OpenAI response with tools: {response}')

        if not response.choices:
            return (None, None, usage_info)

        choice = response.choices[0]

        tool_call_data = getattr(choice.message, 'tool_calls', None)
        if tool_call_data:
            return (response.id, response, usage_info)

        response_dict = convert_json_to_dict(choice.message.content or '')
        return (response.id, response_dict, usage_info)

    except Exception as e:
        logger.error(f'Chat request error: {e}')
        return (None, None, usage_info)



async def upload_file_to_openai(file_path: str) -> Optional[str]:
    try:
        with open(file_path, 'rb') as file:
            upload_response = await client.files.create(
                file=file,
                purpose='assistants',
            )
        return upload_response.id
    except Exception as e:
        logger.error(f'File upload error: {e}')
        return None
