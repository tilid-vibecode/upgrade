STREAM_KEY = 'llm_usage:log_stream'
CONSUMER_GROUP = 'llm_usage_workers'
CONSUMER_NAME_PREFIX = 'worker'
COUNTER_KEY_PREFIX = 'llm_usage:counters'
COUNTER_TTL_SECONDS = 48 * 3600
FLUSH_BATCH_SIZE = 500

RAW_LOG_RETENTION_DAYS = 90
HOURLY_AGG_RETENTION_DAYS = 365
STREAM_MAX_LEN = 100_000

MODEL_COST_PER_1M_TOKENS = {
    'gpt-4o': {'prompt': 2_500_000, 'completion': 10_000_000},
    'gpt-4o-mini': {'prompt': 150_000, 'completion': 600_000},
    'gpt-4.5-preview': {'prompt': 75_000_000, 'completion': 150_000_000},
    'gpt-3.5-turbo': {'prompt': 500_000, 'completion': 1_500_000},
    'o1': {'prompt': 15_000_000, 'completion': 60_000_000},
    'o3-mini': {'prompt': 1_100_000, 'completion': 4_400_000},
    'gpt-5': {'prompt': 1_250_000, 'completion': 10_000_000},
    'gpt-5-mini': {'prompt': 250_000, 'completion': 2_000_000},
    'gpt-5-nano': {'prompt': 50_000, 'completion': 400_000},
    'gpt-5-pro': {'prompt': 15_000_000, 'completion': 120_000_000},
    'gpt-5.1': {'prompt': 1_250_000, 'completion': 10_000_000},
    'gpt-5.2': {'prompt': 1_750_000, 'completion': 14_000_000},
    'gpt-5.2-pro': {'prompt': 21_000_000, 'completion': 168_000_000},
    'gpt-5.4': {'prompt': 2_500_000, 'completion': 15_000_000},
    'gpt-5.4-pro': {'prompt': 30_000_000, 'completion': 180_000_000},
    'gpt-5.4-mini': {'prompt': 750_000, 'completion': 4_500_000},
    'gpt-5.4-nano': {'prompt': 200_000, 'completion': 1_250_000},
    'llama2-7b': {'prompt': 0, 'completion': 0},
    'llama2-13b': {'prompt': 0, 'completion': 0},
}


def _resolve_cost_model_key(model: str) -> str | None:
    if model in MODEL_COST_PER_1M_TOKENS:
        return model

    for key in sorted(MODEL_COST_PER_1M_TOKENS, key=len, reverse=True):
        if model.startswith(key + '-'):
            return key

    return None


def estimate_cost_micro(model: str, prompt_tokens: int, completion_tokens: int) -> int:
    resolved_model = _resolve_cost_model_key(model)
    if not resolved_model:
        return 0

    costs = MODEL_COST_PER_1M_TOKENS[resolved_model]
    if not costs:
        return 0
    prompt_cost = (prompt_tokens * costs['prompt']) // 1_000_000
    completion_cost = (completion_tokens * costs['completion']) // 1_000_000
    return prompt_cost + completion_cost
