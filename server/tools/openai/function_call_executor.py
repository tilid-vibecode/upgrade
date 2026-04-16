# File location: /server/tools/openai/function_call_executor.py
"""
Function call dispatcher for OpenAI tool calling.

Executes Python functions referenced by name from LLM tool_calls.
The function_map is a dict of name -> callable, where callables can
be sync or async (e.g. closures capturing a BlackboardService).

Results are always returned as JSON-serializable dicts.
"""

import asyncio
import json
import logging
from typing import Callable, Dict, Tuple

logger = logging.getLogger(__name__)


async def handle_function_call(
    function_name: str,
    arguments: dict,
    function_map: Dict[str, Callable],
) -> Tuple[str, dict]:
    """
    Execute a registered function with the given arguments.

    The function_map values can be sync or async callables.
    Async closures (e.g. from blackboard_tools) are fully supported.

    Args:
        function_name: Name of the function to call (from LLM tool_calls)
        arguments: Parsed arguments dict from the LLM
        function_map: Registry of name -> callable

    Returns:
        Tuple of (function_name, result_dict)

    Raises:
        ValueError: If function_name is not in the map
    """
    if function_name not in function_map:
        raise ValueError(f'No registered function named \'{function_name}\'')

    func = function_map[function_name]

    try:
        # Dispatch — async closures are the common case for
        # blackboard/context tools; sync functions also supported.
        if asyncio.iscoroutinefunction(func):
            result = await func(**arguments)
        else:
            result = func(**arguments)

    except TypeError as e:
        # Argument mismatch — LLM passed wrong params
        logger.warning(
            f'Function {function_name} argument error: {e}. '
            f'Arguments received: {list(arguments.keys())}'
        )
        result = {
            'error': f'Invalid arguments: {e}',
            'expected': _get_param_names(func),
        }

    except Exception as e:
        # Catch-all so a failing tool doesn't crash the whole
        # LLM conversation loop. The LLM will see the error
        # and can retry or proceed without the tool result.
        logger.error(
            f'Function {function_name} execution failed: {e}',
            exc_info=True,
        )
        result = {
            'error': f'Function execution failed: {type(e).__name__}: {e}',
        }

    # Ensure result is a dict
    if not isinstance(result, dict):
        result = {'value': result}

    # Ensure JSON-serializable (closures may return FieldValue
    # objects or other non-primitive types)
    result = _ensure_serializable(result)

    return function_name, result


def _get_param_names(func: Callable) -> list:
    """Extract parameter names from a function for error messages."""
    try:
        import inspect
        sig = inspect.signature(func)
        return list(sig.parameters.keys())
    except (ValueError, TypeError):
        return ['unknown']


def _ensure_serializable(obj):
    """
    Recursively ensure an object is JSON-serializable.

    Handles common non-serializable types that may leak from
    blackboard FieldValue objects or Django model instances.
    """
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        pass

    if isinstance(obj, dict):
        return {str(k): _ensure_serializable(v) for k, v in obj.items()}

    if isinstance(obj, (list, tuple)):
        return [_ensure_serializable(item) for item in obj]

    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj

    # Fallback: stringify
    return str(obj)
