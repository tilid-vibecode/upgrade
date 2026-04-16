import asyncio
import json
import logging
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

from .function_call_executor import handle_function_call
from .logged_request import (
    LLMCallContext,
    LLMCallMeta,
    LLMCallType,
    logged_chat_request,
)
from .logged_responses_request import logged_responses_request
from .logger import log_iteration
from .responses_agent import ResponsesToolCallResult
from .validator import validate_response

logger = logging.getLogger(__name__)


class ProviderEnum(str, Enum):
    OPENAI = 'openai'
    LLAMA = 'llama'

    @classmethod
    def default(cls) -> 'ProviderEnum':
        return cls.OPENAI


class OpenAIModelEnum(str, Enum):
    GPT_4O = 'gpt-4o'
    GPT_4O_MINI = 'gpt-4o-mini'
    GPT_4_5 = 'gpt-4.5-preview'
    GPT_35TURBO = 'gpt-3.5-turbo'
    GPT_O1 = 'o1'
    GPT_O3_MINI = 'o3-mini'
    GPT_5 = 'gpt-5'
    GPT_5_MINI = 'gpt-5-mini'
    GPT_5_NANO = 'gpt-5-nano'
    GPT_5_PRO = 'gpt-5-pro'
    GPT_5_1 = 'gpt-5.1'
    GPT_5_2 = 'gpt-5.2'
    GPT_5_2_PRO = 'gpt-5.2-pro'
    GPT_5_4 = 'gpt-5.4'
    GPT_5_4_PRO = 'gpt-5.4-pro'
    GPT_5_4_MINI = 'gpt-5.4-mini'
    GPT_5_4_NANO = 'gpt-5.4-nano'

    @classmethod
    def default(cls) -> 'OpenAIModelEnum':
        return cls.GPT_4O


class LlamaModelEnum(str, Enum):
    LLAMA2_7B = 'llama2-7b'
    LLAMA2_13B = 'llama2-13b'

    @classmethod
    def default(cls) -> 'LlamaModelEnum':
        return cls.LLAMA2_7B


PROVIDER_TO_MODEL = {
    ProviderEnum.OPENAI: OpenAIModelEnum,
    ProviderEnum.LLAMA: LlamaModelEnum,
}


DEFAULT_LLM_REQUEST_PARAMS = {
    'temperature': 0.3,
    'temperature_step': 0.05,
    'max_attempts': 10,
    'limit': 1,
}

_CONTROL_PLANE_KEYS = {
    'continuations',
    'evidence_claims',
    'predictions',
    'warnings',
    'assumptions',
    'should_interrupt_plan',
}

_RESPONSES_API_PREFIXES: tuple[str, ...] = ('gpt-5',)
_JSON_OBJECT_FORMAT = {'format': {'type': 'json_object'}}
_JSON_HINT_MESSAGE = {
    'role': 'developer',
    'content': 'Always respond with a valid JSON object.',
}


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    return [value]


def _as_string_list(value: Any) -> List[str]:
    result: List[str] = []
    for item in _as_list(value):
        if item is None:
            continue
        text = item.strip() if isinstance(item, str) else str(item).strip()
        if text:
            result.append(text)
    return result


def _slugify_identifier(value: Any) -> str:
    raw = str(value or '').strip().lower()
    chars: List[str] = []
    last_was_sep = False
    for char in raw:
        if char.isalnum():
            chars.append(char)
            last_was_sep = False
        elif not last_was_sep:
            chars.append('_')
            last_was_sep = True
    slug = ''.join(chars).strip('_')
    return slug or 'item'


def _uses_responses_api(model_str: str) -> bool:
    return any(
        model_str == prefix
        or model_str.startswith(prefix + '-')
        or model_str.startswith(prefix + '.')
        for prefix in _RESPONSES_API_PREFIXES
    )


def uses_responses_api_model(model_str: str) -> bool:
    return _uses_responses_api(model_str)


def _prepare_responses_input(messages: List[dict]) -> Tuple[Optional[str], List[dict]]:
    instructions = None
    input_items = []

    for message in messages:
        role = message.get('role', 'user')
        content = message.get('content', '')

        if role == 'system' and instructions is None:
            instructions = content
            continue

        if role == 'function':
            call_id = message.get('call_id', '')
            if not call_id:
                logger.warning('Function message missing call_id for Responses API')
            input_items.append(
                {
                    'type': 'function_call_output',
                    'call_id': call_id,
                    'output': content,
                }
            )
        else:
            input_items.append({'role': role, 'content': content})

    return instructions, input_items


def _ensure_json_in_responses_input(input_items: List[dict]) -> List[dict]:
    for item in input_items:
        content = item.get('content', '')
        if isinstance(content, str) and 'json' in content.lower():
            return input_items
        output = item.get('output', '')
        if isinstance(output, str) and 'json' in output.lower():
            return input_items

    return [_JSON_HINT_MESSAGE] + input_items


def _chat_tool_to_responses_tool(tool: dict) -> dict:
    if tool.get('type') != 'function':
        return tool

    function = tool.get('function', {})
    return {
        'type': 'function',
        'name': function['name'],
        'description': function.get('description', ''),
        'parameters': function.get('parameters', {'type': 'object', 'properties': {}}),
        'strict': False,
    }


def _merge_tools_for_responses(
    functions: Optional[List[dict]],
    builtin_tools: Optional[List[dict]] = None,
) -> Optional[List[dict]]:
    tools = []
    if functions:
        for function_tool in functions:
            tools.append(_chat_tool_to_responses_tool(function_tool))
    if builtin_tools:
        tools.extend(builtin_tools)
    return tools or None


def _coerce_canonical_envelope(
    response: Optional[Union[list, dict]],
    *,
    meta: Optional[dict] = None,
) -> Optional[Union[list, dict]]:
    if not isinstance(response, dict) or 'final_result' in response or not response:
        return response

    domain = {
        key: value for key, value in response.items()
        if key not in _CONTROL_PLANE_KEYS
    }
    if not domain:
        return response

    control = {
        key: response[key] for key in _CONTROL_PLANE_KEYS
        if key in response
    }
    ref = (meta or {}).get('component') or (meta or {}).get('function') or 'unknown'
    print(f'INFO: Coercing unwrapped JSON response into canonical envelope for {ref}.')
    return {'final_result': domain, **control}


def _normalize_component_response(
    response: Optional[Union[list, dict]],
    *,
    meta: Optional[dict] = None,
) -> Optional[Union[list, dict]]:
    component_id = (meta or {}).get('component', '')
    try:
        from brain.services.response_normalization import normalize_for_component
    except ModuleNotFoundError:
        return response
    return normalize_for_component(response, component_id)


def _maybe_normalize_envelope(response, normalizer, meta):
    if not isinstance(response, dict) or 'final_result' not in response:
        return response
    if normalizer:
        return normalizer(response)
    return _normalize_component_response(response, meta=meta)


def _select_result_payload(
    response: Dict[str, object],
    *,
    final_result_only: bool,
) -> Dict[str, object]:
    return response['final_result'] if final_result_only else response


def _resolve_model_str(model) -> str:
    return model.value if hasattr(model, 'value') else str(model)


async def __configure_request(request_params: Optional[dict] = None):
    params = {**DEFAULT_LLM_REQUEST_PARAMS, **(request_params or {})}
    return (
        params['temperature'],
        params['temperature_step'],
        params['max_attempts'],
        params['limit'],
    )


async def __append_temperature(messages: List[dict], temperature: float) -> List[dict]:
    temperature_message = {'role': 'system', 'content': f'Your processing temperature: {temperature}'}

    def process_messages():
        last_system_index = None
        temperature_indices = []

        for index, message in enumerate(messages):
            if message.get('role') == 'system':
                last_system_index = index
                if message.get('content', '').startswith('Your processing temperature:'):
                    temperature_indices.append(index)

        return last_system_index, temperature_indices

    last_system_index, temperature_indices = await asyncio.to_thread(process_messages)
    for index in reversed(temperature_indices):
        del messages[index]

    if last_system_index is not None:
        messages.insert(last_system_index + 1, temperature_message)

    return messages


async def _llm_request_processor_impl(
    messages: List[dict],
    schema: Optional[dict] = None,
    meta: Optional[dict] = None,
    request_params: Optional[dict] = None,
    provider: ProviderEnum = ProviderEnum.default(),
    model: Optional[Union[OpenAIModelEnum, LlamaModelEnum]] = None,
    call_context: Optional[LLMCallContext] = None,
    final_result_only: bool = False,
    normalizer: Optional[callable] = None,
) -> Optional[Union[list, dict]]:
    params = request_params or {}
    max_tokens = params.get('max_tokens')
    builtin_tools = _as_list(params.get('builtin_tools'))
    include = _as_string_list(params.get('include')) or None
    reasoning = params.get('reasoning')
    background = params.get('background')
    max_tool_calls = params.get('max_tool_calls')
    tool_choice = params.get('tool_choice')
    service_tier = params.get('service_tier')
    truncation = params.get('truncation')
    metadata = params.get('metadata')
    store = params.get('store', False)

    if params.get('provider'):
        provider = ProviderEnum(params['provider'])
    if params.get('model'):
        model = params['model']

    if model is None:
        model = PROVIDER_TO_MODEL[provider].default()

    model_str = _resolve_model_str(model)
    use_responses_api = _uses_responses_api(model_str)
    result = None

    temperature, temperature_step, max_attempts, iteration_limit = await __configure_request(request_params)
    iteration = 0
    total_attempts = 0

    while total_attempts < max_attempts:
        print(
            f'DEBUG: Iteration {iteration}, Temperature: {temperature}, '
            f'Total Attempts: {total_attempts}, Provider: {provider}'
        )

        if not use_responses_api and provider == ProviderEnum.OPENAI and temperature <= 1.0:
            messages = await __append_temperature(messages, temperature=temperature)

        if total_attempts == 0 and iteration == 0:
            call_type = LLMCallType.COMPLETION
        elif iteration > 0:
            call_type = LLMCallType.ITERATION_RETRY
        else:
            call_type = LLMCallType.VALIDATION_RETRY

        if use_responses_api:
            instructions, input_items = _prepare_responses_input(messages)
            input_items = _ensure_json_in_responses_input(input_items)
            responses_tools = _merge_tools_for_responses(None, builtin_tools or None)

            _, response, usage_info = await logged_responses_request(
                input_items=input_items,
                model=model_str,
                provider=provider.value,
                call_context=call_context,
                call_meta=LLMCallMeta(
                    call_type=call_type,
                    caller_function='llm_request_processor',
                    iteration=iteration,
                    attempt=total_attempts,
                ),
                instructions=instructions,
                text_format=_JSON_OBJECT_FORMAT,
                tools=responses_tools,
                max_output_tokens=max_tokens,
                temperature=temperature if temperature <= 1.0 else None,
                include=include,
                store=store,
                reasoning=reasoning,
                background=background,
                max_tool_calls=max_tool_calls,
                tool_choice=tool_choice,
                service_tier=service_tier,
                truncation=truncation,
                metadata=metadata,
            )
        else:
            _, response, usage_info = await logged_chat_request(
                messages=messages,
                model=model_str,
                provider=provider.value,
                call_context=call_context,
                call_meta=LLMCallMeta(
                    call_type=call_type,
                    caller_function='llm_request_processor',
                    iteration=iteration,
                    attempt=total_attempts,
                ),
                max_tokens=max_tokens,
            )

        if isinstance(response, dict):
            response.pop('_responses_meta', None)

        response = _coerce_canonical_envelope(response, meta=meta)
        response = _maybe_normalize_envelope(response, normalizer, meta)

        if response and 'final_result' in response:
            if schema:
                is_valid = await validate_response(schema, response)
                if is_valid:
                    result = _select_result_payload(
                        response,
                        final_result_only=final_result_only,
                    )
                    await log_iteration(
                        meta,
                        iteration,
                        total_attempts,
                        'SUCCESS',
                        'iteration completed with validated response',
                    )
                    temperature += temperature_step
                    iteration += 1
                    total_attempts += 1
                    if iteration == iteration_limit:
                        break
                    messages.append(
                        {
                            'role': 'assistant',
                            'content': (
                                'This is a result of your processing, please validate it with an increased'
                                ' temperature defined in system message, ensure you done your best job '
                                'in previous iteration, if not make response better and follow all the '
                                f'rules defined for this task, here is your previous response:\n{response}'
                            ),
                        }
                    )
                    continue

                await log_iteration(
                    meta,
                    iteration,
                    total_attempts,
                    'VALIDATION FAILED',
                    'response schema validation failed',
                )
                print(f'The response were: \n{response}')
                total_attempts += 1
                continue

            result = _select_result_payload(
                response,
                final_result_only=final_result_only,
            )
            await log_iteration(
                meta,
                iteration,
                total_attempts,
                'SUCCESS',
                'iteration completed without schema validation',
            )
            temperature += temperature_step
            iteration += 1
            total_attempts += 1
            if iteration == iteration_limit:
                print(f'The response were: \n{response}')
                break
        else:
            await log_iteration(
                meta,
                iteration,
                total_attempts,
                'RESPONSE FAILED' if not response else 'ITERATION FAILED',
                'missed response from OpenAI' if not response else 'missed proper response from OpenAI.',
            )
            total_attempts += 1

    if total_attempts >= max_attempts:
        await log_iteration(
            meta,
            iteration,
            total_attempts,
            'FUNCTION FAILED',
            'reached maximum number of attempts, returning None',
        )

    return result


async def llm_request_processor(
    messages: List[dict],
    schema: Optional[dict] = None,
    meta: Optional[dict] = None,
    request_params: Optional[dict] = None,
    provider: ProviderEnum = ProviderEnum.default(),
    model: Optional[Union[OpenAIModelEnum, LlamaModelEnum]] = None,
    call_context: Optional[LLMCallContext] = None,
    normalizer: Optional[callable] = None,
) -> Optional[Union[list, dict]]:
    return await _llm_request_processor_impl(
        messages=messages,
        schema=schema,
        meta=meta,
        request_params=request_params,
        provider=provider,
        model=model,
        call_context=call_context,
        final_result_only=False,
        normalizer=normalizer,
    )


async def llm_request_final_result(
    messages: List[dict],
    schema: Optional[dict] = None,
    meta: Optional[dict] = None,
    request_params: Optional[dict] = None,
    provider: ProviderEnum = ProviderEnum.default(),
    model: Optional[Union[OpenAIModelEnum, LlamaModelEnum]] = None,
    call_context: Optional[LLMCallContext] = None,
    normalizer: Optional[callable] = None,
) -> Optional[Union[list, dict]]:
    return await _llm_request_processor_impl(
        messages=messages,
        schema=schema,
        meta=meta,
        request_params=request_params,
        provider=provider,
        model=model,
        call_context=call_context,
        final_result_only=True,
        normalizer=normalizer,
    )


async def _llm_request_processor_with_functions_impl(
    messages: List[dict],
    schema: Optional[dict] = None,
    meta: Optional[dict] = None,
    request_params: Optional[dict] = None,
    functions: Optional[List[dict]] = None,
    function_map: Optional[Dict[str, callable]] = None,
    provider: ProviderEnum = ProviderEnum.OPENAI,
    model: Optional[Union[OpenAIModelEnum, LlamaModelEnum]] = None,
    call_context: Optional[LLMCallContext] = None,
    final_result_only: bool = False,
    normalizer: Optional[callable] = None,
) -> Optional[Union[list, dict]]:
    params = request_params or {}
    max_tokens = params.get('max_tokens')
    builtin_tools = _as_list(params.get('builtin_tools'))
    include = _as_string_list(params.get('include')) or None
    reasoning = params.get('reasoning')
    background = params.get('background')
    max_tool_calls = params.get('max_tool_calls')
    tool_choice = params.get('tool_choice')
    service_tier = params.get('service_tier')
    truncation = params.get('truncation')
    metadata = params.get('metadata')
    store = params.get('store', False)

    if params.get('provider'):
        provider = ProviderEnum(params['provider'])
    if params.get('model'):
        model = params['model']

    if model is None:
        model = PROVIDER_TO_MODEL[provider].default()

    model_str = _resolve_model_str(model)
    use_responses_api = _uses_responses_api(model_str)
    result = None

    temperature, temperature_step, max_attempts, iteration_limit = await __configure_request(request_params)
    iteration = 0
    total_attempts = 0
    tool_rounds = 0
    max_tool_rounds = params.get('max_tool_rounds', 8)
    pending_tool_followup = False
    previous_response_id: Optional[str] = None
    responses_instructions: Optional[str] = None

    if use_responses_api:
        responses_instructions, _ = _prepare_responses_input(messages)

    while total_attempts < max_attempts:
        if not use_responses_api and provider == ProviderEnum.OPENAI and temperature <= 1.0:
            base_messages = [
                message for message in messages
                if not (
                    message.get('role') == 'system'
                    and 'Your processing temperature:' in message.get('content', '')
                )
            ]
            messages = await __append_temperature(base_messages, temperature=temperature)

        print(
            f'[DEBUG] Attempt={total_attempts}, Iteration={iteration}, '
            f'Temp={temperature}, Model={model}, Provider={provider}'
        )

        if pending_tool_followup:
            call_type = LLMCallType.TOOL_FOLLOWUP
            pending_tool_followup = False
        elif total_attempts == 0:
            call_type = LLMCallType.COMPLETION
        else:
            call_type = LLMCallType.VALIDATION_RETRY

        is_responses_followup = (
            use_responses_api
            and previous_response_id is not None
            and any(message.get('role') == 'function' and message.get('_pending') for message in messages)
        )

        if use_responses_api:
            if is_responses_followup:
                input_items = [
                    {
                        'type': 'function_call_output',
                        'call_id': message['call_id'],
                        'output': message['content'],
                    }
                    for message in messages
                    if message.get('role') == 'function' and message.get('_pending', False)
                ]
            else:
                responses_instructions, input_items = _prepare_responses_input(messages)
                input_items = _ensure_json_in_responses_input(input_items)

            responses_tools = _merge_tools_for_responses(functions, builtin_tools or None)
            _, response, usage_info = await logged_responses_request(
                input_items=input_items,
                model=model_str,
                provider=provider.value,
                call_context=call_context,
                call_meta=LLMCallMeta(
                    call_type=call_type,
                    caller_function='llm_request_processor_with_functions',
                    iteration=iteration,
                    attempt=total_attempts,
                ),
                instructions=responses_instructions,
                text_format=_JSON_OBJECT_FORMAT,
                tools=responses_tools,
                parallel_tool_calls=False,
                max_output_tokens=max_tokens,
                temperature=temperature if temperature <= 1.0 else None,
                include=include,
                store=bool(functions) if params.get('store') is None else bool(store),
                previous_response_id=previous_response_id,
                reasoning=reasoning,
                background=background,
                max_tool_calls=max_tool_calls,
                tool_choice=tool_choice,
                service_tier=service_tier,
                truncation=truncation,
                metadata=metadata,
            )
        else:
            _, response, usage_info = await logged_chat_request(
                messages=messages,
                model=model_str,
                provider=provider.value,
                call_context=call_context,
                call_meta=LLMCallMeta(
                    call_type=call_type,
                    caller_function='llm_request_processor_with_functions',
                    iteration=iteration,
                    attempt=total_attempts,
                ),
                tools=functions,
                max_tokens=max_tokens,
            )

        if isinstance(response, dict):
            response.pop('_responses_meta', None)

        response = _coerce_canonical_envelope(response, meta=meta)

        if not response:
            print('No response or invalid from LLM. Trying again...')
            total_attempts += 1
            continue

        tool_calls_responses: List = []
        tool_calls_chat: list = []

        if isinstance(response, ResponsesToolCallResult):
            tool_calls_responses = response.function_calls
            previous_response_id = response.response_id
        elif hasattr(response, 'choices') and len(response.choices) > 0:
            first_choice = response.choices[0]
            if hasattr(first_choice.message, 'tool_calls') and first_choice.message.tool_calls:
                tool_calls_chat = first_choice.message.tool_calls

        if tool_calls_responses:
            messages = [message for message in messages if not message.get('_pending')]

            for function_call in tool_calls_responses:
                function_name = function_call.name
                try:
                    function_args = json.loads(function_call.arguments)
                except json.JSONDecodeError:
                    print(f'Function call arguments not valid JSON: {function_call.arguments}')
                    total_attempts += 1
                    continue

                if not function_map or function_name not in function_map:
                    print(f"LLM requested function '{function_name}', not in function_map. Retrying...")
                    total_attempts += 1
                    continue

                try:
                    called_name, function_result = await handle_function_call(
                        function_name,
                        function_args,
                        function_map,
                    )
                except Exception as exc:
                    print(f'Error calling function {function_name}: {exc}')
                    total_attempts += 1
                    continue

                messages.append(
                    {
                        'role': 'function',
                        'name': called_name,
                        'content': json.dumps(function_result),
                        'call_id': function_call.call_id,
                        '_pending': True,
                    }
                )
                pending_tool_followup = True

            total_attempts += 1
            tool_rounds += 1
            if tool_rounds >= max_tool_rounds:
                print(f'Max tool rounds ({max_tool_rounds}) reached. Breaking.')
                break
            continue

        if tool_calls_chat:
            for call in tool_calls_chat:
                if not hasattr(call, 'function') or not hasattr(call.function, 'name'):
                    print('Invalid function call structure from LLM. Retrying...')
                    total_attempts += 1
                    continue

                function_name = call.function.name
                function_args_raw = call.function.arguments

                try:
                    function_args = json.loads(function_args_raw)
                except json.JSONDecodeError:
                    print(f'Function call arguments not valid JSON: {function_args_raw}')
                    total_attempts += 1
                    continue

                if not function_map or function_name not in function_map:
                    print(f"LLM requested function '{function_name}', not in function_map. Retrying...")
                    total_attempts += 1
                    continue

                try:
                    called_name, function_result = await handle_function_call(
                        function_name,
                        function_args,
                        function_map,
                    )
                except Exception as exc:
                    print(f'Error calling function {function_name}: {exc}')
                    total_attempts += 1
                    continue

                messages.append(
                    {
                        'role': 'function',
                        'name': called_name,
                        'content': json.dumps(function_result),
                    }
                )
                pending_tool_followup = True

            total_attempts += 1
            tool_rounds += 1
            if tool_rounds >= max_tool_rounds:
                print(f'Max tool rounds ({max_tool_rounds}) reached. Breaking.')
                break
            continue

        response = _maybe_normalize_envelope(response, normalizer, meta)

        if isinstance(response, dict) and 'final_result' in response:
            if schema:
                is_valid = await validate_response(schema, response)
                if is_valid:
                    result = _select_result_payload(
                        response,
                        final_result_only=final_result_only,
                    )
                    await log_iteration(meta, iteration, total_attempts, 'SUCCESS', 'final_result validated')
                    break
                print('Schema validation failed. Retrying...')
                print(f'The response were: \n{response}')
                total_attempts += 1
                continue

            result = _select_result_payload(
                response,
                final_result_only=final_result_only,
            )
            await log_iteration(
                meta,
                iteration,
                total_attempts,
                'SUCCESS',
                'final_result without schema check',
            )
            break

        print("No 'tool_calls' or 'final_result' in response, retrying...")
        total_attempts += 1

    if not result:
        print('Reached max attempts or never got final_result.')
        await log_iteration(meta, iteration, total_attempts, 'FAILURE', "didn't produce final_result")

    return result


async def llm_request_processor_with_functions(
    messages: List[dict],
    schema: Optional[dict] = None,
    meta: Optional[dict] = None,
    request_params: Optional[dict] = None,
    functions: Optional[List[dict]] = None,
    function_map: Optional[Dict[str, callable]] = None,
    provider: ProviderEnum = ProviderEnum.OPENAI,
    model: Optional[Union[OpenAIModelEnum, LlamaModelEnum]] = None,
    call_context: Optional[LLMCallContext] = None,
    normalizer: Optional[callable] = None,
) -> Optional[Union[list, dict]]:
    return await _llm_request_processor_with_functions_impl(
        messages=messages,
        schema=schema,
        meta=meta,
        request_params=request_params,
        functions=functions,
        function_map=function_map,
        provider=provider,
        model=model,
        call_context=call_context,
        final_result_only=False,
        normalizer=normalizer,
    )


async def llm_request_final_result_with_functions(
    messages: List[dict],
    schema: Optional[dict] = None,
    meta: Optional[dict] = None,
    request_params: Optional[dict] = None,
    functions: Optional[List[dict]] = None,
    function_map: Optional[Dict[str, callable]] = None,
    provider: ProviderEnum = ProviderEnum.OPENAI,
    model: Optional[Union[OpenAIModelEnum, LlamaModelEnum]] = None,
    call_context: Optional[LLMCallContext] = None,
    normalizer: Optional[callable] = None,
) -> Optional[Union[list, dict]]:
    return await _llm_request_processor_with_functions_impl(
        messages=messages,
        schema=schema,
        meta=meta,
        request_params=request_params,
        functions=functions,
        function_map=function_map,
        provider=provider,
        model=model,
        call_context=call_context,
        final_result_only=True,
        normalizer=normalizer,
    )
