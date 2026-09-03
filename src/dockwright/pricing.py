import re

MODEL_RATES = {
    "fable": (10.0, 50.0),
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}

CACHE_READ_MULT = 0.1
CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.0

_FAMILY_PREFIXES = (
    ("claude-fable-", "fable"),
    ("claude-mythos-", "fable"),
    ("claude-opus-", "opus"),
    ("claude-sonnet-", "sonnet"),
    ("claude-haiku-", "haiku"),
)


def _strip_model_id(model_id):
    if not isinstance(model_id, str) or not model_id:
        return None
    key = model_id.strip().lower()
    key = re.sub(r"\[.*?\]$", "", key)
    key = re.sub(r"-\d{8}$", "", key)
    return key or None


def normalize_model(model_id):
    key = _strip_model_id(model_id)
    if key is None:
        return None
    for prefix, canonical in _FAMILY_PREFIXES:
        if key.startswith(prefix):
            return canonical
    return None


def get_rates():
    try:
        from . import config
        overrides = config.pricing_overrides()
    except Exception:
        overrides = {}
    return {**MODEL_RATES, **overrides} if overrides else MODEL_RATES


def rate_key(model_id, rates=None):
    rates = get_rates() if rates is None else rates
    stripped = _strip_model_id(model_id)
    if stripped is not None and stripped in rates:
        return stripped
    family = normalize_model(model_id)
    return family if family in rates else None


def cost_breakdown(model_id, *, rates=None, output_tokens=0, input_tokens=0,
                   cache_read_tokens=0, cache_creation_5m_tokens=0,
                   cache_creation_1h_tokens=0):
    rates = get_rates() if rates is None else rates
    key = rate_key(model_id, rates)
    if key is None:
        return {"input": 0.0, "output": 0.0, "cache_read": 0.0,
                "cache_write": 0.0, "total": 0.0, "priced": False}
    in_rate, out_rate = rates[key]
    per = 1_000_000
    input_cost = input_tokens / per * in_rate
    output_cost = output_tokens / per * out_rate
    cache_read_cost = cache_read_tokens / per * in_rate * CACHE_READ_MULT
    cache_write_cost = (
        cache_creation_5m_tokens / per * in_rate * CACHE_WRITE_5M_MULT
        + cache_creation_1h_tokens / per * in_rate * CACHE_WRITE_1H_MULT
    )
    total = input_cost + output_cost + cache_read_cost + cache_write_cost
    return {"input": input_cost, "output": output_cost,
            "cache_read": cache_read_cost, "cache_write": cache_write_cost,
            "total": total, "priced": True}
