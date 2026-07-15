"""Per-model token pricing (pure; FastMCP-free)."""
from dockwright import pricing


def test_normalize_model_maps_families_and_strips_suffixes():
    assert pricing.normalize_model("claude-fable-5") == "fable"
    assert pricing.normalize_model("claude-mythos-5") == "fable"
    assert pricing.normalize_model("claude-opus-4-8") == "opus"
    assert pricing.normalize_model("claude-opus-4-8[1m]") == "opus"   # [1m] = no premium
    assert pricing.normalize_model("claude-opus-4-7") == "opus"
    assert pricing.normalize_model("claude-sonnet-4-6") == "sonnet"
    assert pricing.normalize_model("claude-haiku-4-5-20251001") == "haiku"  # date suffix
    assert pricing.normalize_model("CLAUDE-OPUS-4-8") == "opus"        # case-insensitive
    assert pricing.normalize_model("<synthetic>") is None
    assert pricing.normalize_model("some-unknown-model") is None
    assert pricing.normalize_model("") is None
    assert pricing.normalize_model(None) is None


def test_cost_breakdown_prices_per_model_not_flat_sonnet():
    # 1M output tokens: Fable bills $50, Sonnet $15, Opus $25, Haiku $5.
    assert pricing.cost_breakdown("claude-fable-5", output_tokens=1_000_000)["output"] == 50.0
    assert pricing.cost_breakdown("claude-sonnet-4-6", output_tokens=1_000_000)["output"] == 15.0
    assert pricing.cost_breakdown("claude-opus-4-8", output_tokens=1_000_000)["output"] == 25.0
    assert pricing.cost_breakdown("claude-haiku-4-5", output_tokens=1_000_000)["output"] == 5.0


def test_cost_breakdown_uncached_input_at_base_rate():
    b = pricing.cost_breakdown("claude-fable-5", input_tokens=1_000_000)
    assert b["input"] == 10.0


def test_cost_breakdown_cache_read_is_tenth_of_input():
    # Fable input $10/MTok -> cache-read $1/MTok.
    b = pricing.cost_breakdown("claude-fable-5", cache_read_tokens=1_000_000)
    assert b["cache_read"] == 1.0
    assert b["cache_write"] == 0.0


def test_cost_breakdown_cache_write_uses_ttl_multiplier():
    # Fable input $10/MTok: 5m write = 1.25x = $12.50, 1h write = 2x = $20.00.
    five_m = pricing.cost_breakdown("claude-fable-5", cache_creation_5m_tokens=1_000_000)
    one_h = pricing.cost_breakdown("claude-fable-5", cache_creation_1h_tokens=1_000_000)
    assert five_m["cache_write"] == 12.5
    assert one_h["cache_write"] == 20.0


def test_cost_breakdown_cache_creation_is_counted_not_zero():
    # Regression guard for bug 3 (cache-creation omitted): a session that is
    # ALL 1h cache-creation must have a non-zero total.
    b = pricing.cost_breakdown("claude-fable-5", cache_creation_1h_tokens=5_000_000)
    assert b["cache_write"] == 100.0
    assert b["total"] == 100.0
    assert b["priced"] is True


def test_cost_breakdown_total_sums_all_components():
    b = pricing.cost_breakdown(
        "claude-fable-5",
        output_tokens=1_000_000,        # $50
        input_tokens=1_000_000,         # $10
        cache_read_tokens=1_000_000,    # $1
        cache_creation_1h_tokens=1_000_000,  # $20
    )
    assert b["total"] == 81.0


def test_cost_breakdown_unknown_model_is_unpriced_zero():
    b = pricing.cost_breakdown("<synthetic>", output_tokens=9_999_999, cache_read_tokens=9_999_999)
    assert b["priced"] is False
    assert b["total"] == 0.0
    b2 = pricing.cost_breakdown("some-future-model", output_tokens=1_000_000)
    assert b2["priced"] is False
    assert b2["total"] == 0.0
