"""Per-model token pricing for the dockwright spend meter.

Near-pure (one optional small config read) and FastMCP-free so it stays cheap
to import on the hook path. Rates are USD per million tokens, from the
claude-api skill pricing reference (cached 2026-06-04). Cache multipliers are
applied against the INPUT rate: read 0.1x, write 1.25x (5m TTL) / 2x (1h TTL).
The `[1m]` model-id suffix is a 1M-context marker, NOT a price premium — Opus
4.7/4.8 are documented as 1M context at standard pricing — so it is stripped
before lookup.
"""
import re

# canonical key -> (input $/MTok, output $/MTok)
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
    ("claude-mythos-", "fable"),   # Mythos 5 shares Fable pricing
    ("claude-opus-", "opus"),
    ("claude-sonnet-", "sonnet"),
    ("claude-haiku-", "haiku"),
)


def normalize_model(model_id):
    """Map a raw transcript `message.model` to a MODEL_RATES key, or None.

    Strips a trailing `[...]` context suffix (e.g. `[1m]`) and a trailing
    `-YYYYMMDD` date snapshot, lower-cases, then matches a known family prefix.
    `<synthetic>` and any unknown id return None (caller treats as unpriced).
    """
    if not isinstance(model_id, str) or not model_id:
        return None
    key = model_id.strip().lower()
    key = re.sub(r"\[.*?\]$", "", key)          # drop [1m] / [200k] context suffix
    key = re.sub(r"-\d{8}$", "", key)            # drop -YYYYMMDD snapshot suffix
    for prefix, canonical in _FAMILY_PREFIXES:
        if key.startswith(prefix):
            return canonical
    return None


def get_rates():
    """MODEL_RATES merged with dockwright.toml [pricing.rates] overrides.
    Fail-open: any config problem yields the built-ins. The config read is
    one small optional file per call — still hook-path cheap."""
    try:
        from . import config
        overrides = config.pricing_overrides()
    except Exception:
        overrides = {}
    return {**MODEL_RATES, **overrides} if overrides else MODEL_RATES


def cost_breakdown(model_id, *, output_tokens=0, input_tokens=0,
                   cache_read_tokens=0, cache_creation_5m_tokens=0,
                   cache_creation_1h_tokens=0):
    """USD cost of one model's token usage, split by component.

    Returns {"input","output","cache_read","cache_write","total","priced"}.
    Unknown / synthetic model -> all-zero with priced=False so the caller can
    surface unpriced token volume instead of silently undercounting.
    """
    rates = get_rates()
    key = normalize_model(model_id)
    if key is None or key not in rates:
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
