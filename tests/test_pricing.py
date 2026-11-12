import pytest

from dockwright import pricing


def test_normalize_model_maps_families_and_strips_suffixes():
    assert pricing.normalize_model("claude-fable-5") == "fable"
    assert pricing.normalize_model("claude-mythos-5") == "fable"
    assert pricing.normalize_model("claude-opus-4-8") == "opus"
    assert pricing.normalize_model("claude-opus-4-8[1m]") == "opus"
    assert pricing.normalize_model("claude-opus-4-7") == "opus"
    assert pricing.normalize_model("claude-sonnet-4-6") == "sonnet"
    assert pricing.normalize_model("claude-haiku-4-5-20251001") == "haiku"
    assert pricing.normalize_model("CLAUDE-OPUS-4-8") == "opus"
    assert pricing.normalize_model("<synthetic>") is None
    assert pricing.normalize_model("some-unknown-model") is None
    assert pricing.normalize_model("") is None
    assert pricing.normalize_model(None) is None


def test_cost_breakdown_prices_per_model_not_flat_sonnet():
    assert pricing.cost_breakdown("claude-fable-5", output_tokens=1_000_000)["output"] == 50.0
    assert pricing.cost_breakdown("claude-sonnet-4-6", output_tokens=1_000_000)["output"] == 15.0
    assert pricing.cost_breakdown("claude-opus-4-8", output_tokens=1_000_000)["output"] == 25.0
    assert pricing.cost_breakdown("claude-haiku-4-5", output_tokens=1_000_000)["output"] == 5.0


def test_cost_breakdown_uncached_input_at_base_rate():
    b = pricing.cost_breakdown("claude-fable-5", input_tokens=1_000_000)
    assert b["input"] == 10.0


def test_cost_breakdown_cache_read_is_tenth_of_input():
    b = pricing.cost_breakdown("claude-fable-5", cache_read_tokens=1_000_000)
    assert b["cache_read"] == 1.0
    assert b["cache_write"] == 0.0


def test_cost_breakdown_cache_write_uses_ttl_multiplier():
    five_m = pricing.cost_breakdown("claude-fable-5", cache_creation_5m_tokens=1_000_000)
    one_h = pricing.cost_breakdown("claude-fable-5", cache_creation_1h_tokens=1_000_000)
    assert five_m["cache_write"] == 12.5
    assert one_h["cache_write"] == 20.0


def test_cost_breakdown_cache_creation_is_counted_not_zero():
    b = pricing.cost_breakdown("claude-fable-5", cache_creation_1h_tokens=5_000_000)
    assert b["cache_write"] == 100.0
    assert b["total"] == 100.0
    assert b["priced"] is True


def test_cost_breakdown_total_sums_all_components():
    b = pricing.cost_breakdown(
        "claude-fable-5",
        output_tokens=1_000_000,
        input_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_creation_1h_tokens=1_000_000,
    )
    assert b["total"] == 81.0


def test_cost_breakdown_unknown_model_is_unpriced_zero():
    b = pricing.cost_breakdown("<synthetic>", output_tokens=9_999_999, cache_read_tokens=9_999_999)
    assert b["priced"] is False
    assert b["total"] == 0.0
    b2 = pricing.cost_breakdown("some-future-model", output_tokens=1_000_000)
    assert b2["priced"] is False
    assert b2["total"] == 0.0


def test_exact_key_rate_prices_unknown_family():
    from dockwright import pricing
    rates = dict(pricing.MODEL_RATES)
    rates["claude-newfamily-7"] = (2.0, 4.0)
    b = pricing.cost_breakdown("claude-NewFamily-7-20270101[1m]",
                               rates=rates, output_tokens=1_000_000)
    assert b["priced"] is True
    assert b["total"] == 4.0


def test_exact_key_wins_over_family_key():
    from dockwright import pricing
    rates = dict(pricing.MODEL_RATES)
    rates["claude-opus-5"] = (1.0, 1.0)
    b = pricing.cost_breakdown("claude-opus-5", rates=rates,
                               output_tokens=1_000_000)
    assert b["total"] == 1.0


def test_unknown_model_still_unpriced_with_default_rates():
    from dockwright import pricing
    b = pricing.cost_breakdown("claude-newfamily-7", output_tokens=5)
    assert b["priced"] is False and b["total"] == 0.0


def _cache_read_per_mtok(model, **kw):
    return pricing.cost_breakdown(model, cache_read_tokens=1_000_000, **kw)["cache_read"]


def test_cache_read_priced_at_published_per_model_rate():
    assert _cache_read_per_mtok("claude-fable-5-1") == pytest.approx(0.25)
    assert _cache_read_per_mtok("claude-mythos-5-1") == pytest.approx(0.25)
    assert _cache_read_per_mtok("claude-fable-5") == pytest.approx(1.00)
    assert _cache_read_per_mtok("claude-mythos-5") == pytest.approx(1.00)
    assert _cache_read_per_mtok("claude-opus-5") == pytest.approx(0.50)
    assert _cache_read_per_mtok("claude-opus-4-8") == pytest.approx(0.50)
    assert _cache_read_per_mtok("claude-sonnet-5") == pytest.approx(0.20)
    assert _cache_read_per_mtok("claude-sonnet-4-6") == pytest.approx(0.30)
    assert _cache_read_per_mtok("claude-haiku-4-5") == pytest.approx(0.10)
    assert _cache_read_per_mtok("claude-opus-5-5") == pytest.approx(0.20)
    assert _cache_read_per_mtok("claude-sonnet-5-5") == pytest.approx(0.10)


def test_opus_5_5_priced_at_its_own_rate_not_the_opus_family_rate():
    newer = pricing.cost_breakdown("claude-opus-5-5",
                                   input_tokens=1_000_000, output_tokens=1_000_000)
    older = pricing.cost_breakdown("claude-opus-5",
                                   input_tokens=1_000_000, output_tokens=1_000_000)
    assert (newer["input"], newer["output"]) == pytest.approx((4.0, 20.0))
    assert (older["input"], older["output"]) == pytest.approx((5.0, 25.0))


def test_sonnet_5_5_priced_at_its_own_rate_not_the_sonnet_family_rate():
    newer = pricing.cost_breakdown("claude-sonnet-5-5",
                                   input_tokens=1_000_000, output_tokens=1_000_000)
    older = pricing.cost_breakdown("claude-sonnet-4-6",
                                   input_tokens=1_000_000, output_tokens=1_000_000)
    assert (newer["input"], newer["output"]) == pytest.approx((2.0, 10.0))
    assert (older["input"], older["output"]) == pytest.approx((3.0, 15.0))


def test_sonnet_5_priced_at_its_own_rate_not_the_sonnet_family_rate():
    newer = pricing.cost_breakdown("claude-sonnet-5[1m]",
                                   input_tokens=1_000_000, output_tokens=1_000_000)
    older = pricing.cost_breakdown("claude-sonnet-4-6",
                                   input_tokens=1_000_000, output_tokens=1_000_000)
    assert (newer["input"], newer["output"]) == pytest.approx((2.0, 10.0))
    assert (older["input"], older["output"]) == pytest.approx((3.0, 15.0))


def test_fable_5_1_cache_read_is_a_quarter_of_fable_5():
    newer = pricing.cost_breakdown("claude-fable-5-1[1m]", cache_read_tokens=100_000_000)
    older = pricing.cost_breakdown("claude-fable-5", cache_read_tokens=100_000_000)
    assert newer["cache_read"] == pytest.approx(25.0)
    assert newer["total"] == pytest.approx(25.0)
    assert older["cache_read"] == pytest.approx(100.0)


def test_cache_read_override_survives_context_and_date_suffixes():
    assert _cache_read_per_mtok("claude-fable-5-1[1m]") == pytest.approx(0.25)
    assert _cache_read_per_mtok("claude-fable-5-1-20260401") == pytest.approx(0.25)
    assert _cache_read_per_mtok("CLAUDE-FABLE-5-1") == pytest.approx(0.25)
    assert _cache_read_per_mtok("claude-mythos-5-1[1m]") == pytest.approx(0.25)
    assert _cache_read_per_mtok("claude-opus-5-5[1m]") == pytest.approx(0.20)
    assert _cache_read_per_mtok("claude-opus-5-5-20261001") == pytest.approx(0.20)


def test_opus_5_5_rates_survive_context_and_date_suffixes():
    for model in ("claude-opus-5-5[1m]", "claude-opus-5-5-20261001"):
        b = pricing.cost_breakdown(model, input_tokens=1_000_000, output_tokens=1_000_000)
        assert (b["input"], b["output"]) == pytest.approx((4.0, 20.0))


def test_sibling_models_keep_the_standard_multiplier():
    assert _cache_read_per_mtok("claude-fable-5") == pytest.approx(1.00)
    assert _cache_read_per_mtok("claude-fable-5-15") == pytest.approx(1.00)
    assert _cache_read_per_mtok("claude-opus-5-1") == pytest.approx(0.50)


def test_cache_read_override_leaves_input_output_and_cache_write_alone():
    b = pricing.cost_breakdown("claude-fable-5-1",
                               input_tokens=1_000_000, output_tokens=1_000_000,
                               cache_creation_5m_tokens=1_000_000,
                               cache_creation_1h_tokens=1_000_000)
    assert b["input"] == pytest.approx(10.0)
    assert b["output"] == pytest.approx(50.0)
    assert b["cache_write"] == pytest.approx(32.5)


def test_cache_read_override_applies_to_the_overridden_base_rate():
    rates = dict(pricing.MODEL_RATES)
    rates["fable"] = (20.0, 100.0)
    assert _cache_read_per_mtok("claude-fable-5-1", rates=rates) == pytest.approx(0.5)
    assert _cache_read_per_mtok("claude-fable-5", rates=rates) == pytest.approx(2.0)


def test_unpriced_model_stays_zero_under_the_override_lookup():
    b = pricing.cost_breakdown("claude-mythos-5-1", cache_read_tokens=1_000_000,
                               rates={"opus": (5.0, 25.0)})
    assert b["priced"] is False
    assert b["total"] == 0.0


def test_fable_5_2_priced_at_its_own_rate_not_the_fable_family_rate():
    newer = pricing.cost_breakdown("claude-fable-5-2",
                                   input_tokens=1_000_000, output_tokens=1_000_000)
    older = pricing.cost_breakdown("claude-fable-5-1",
                                   input_tokens=1_000_000, output_tokens=1_000_000)
    assert (newer["input"], newer["output"]) == pytest.approx((8.0, 40.0))
    assert (older["input"], older["output"]) == pytest.approx((10.0, 50.0))


def test_fable_5_2_cache_read_at_its_published_rate():
    assert _cache_read_per_mtok("claude-fable-5-2") == pytest.approx(0.20)
    assert _cache_read_per_mtok("claude-fable-5-2[1m]") == pytest.approx(0.20)
    assert _cache_read_per_mtok("claude-fable-5-2-20261101") == pytest.approx(0.20)


def test_haiku_5_5_priced_at_its_own_rate_not_the_haiku_family_rate():
    for model in ("claude-haiku-5-5", "claude-haiku-5-5[1m]"):
        b = pricing.cost_breakdown(model, input_tokens=1_000_000, output_tokens=1_000_000)
        assert (b["input"], b["output"]) == pytest.approx((0.10, 0.50))
    older = pricing.cost_breakdown("claude-haiku-4-5",
                                   input_tokens=1_000_000, output_tokens=1_000_000)
    assert (older["input"], older["output"]) == pytest.approx((1.0, 5.0))


def test_haiku_5_5_cache_read_is_a_tenth_of_its_input_rate():
    assert _cache_read_per_mtok("claude-haiku-5-5") == pytest.approx(0.01)
    assert _cache_read_per_mtok("claude-haiku-5-5[1m]") == pytest.approx(0.01)


def test_sonnet_5_5_cache_read_survives_context_and_date_suffixes():
    assert _cache_read_per_mtok("claude-sonnet-5-5[1m]") == pytest.approx(0.10)
    assert _cache_read_per_mtok("claude-sonnet-5-5-20261001") == pytest.approx(0.10)
    assert _cache_read_per_mtok("claude-sonnet-5") == pytest.approx(0.20)
