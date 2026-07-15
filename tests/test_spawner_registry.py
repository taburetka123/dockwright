"""N-account registry behavior in spawner (config-driven pool)."""
import json

import pytest

from dockwright import config, paths, spawner
from tests.carve_helpers import operator_forbidden_tokens


@pytest.fixture
def sp(tmp_path, monkeypatch):
    """Point every account state file into tmp; default pool unless a config
    file is installed by the test."""
    monkeypatch.setattr(paths, "ACCOUNT_ACTIVE", tmp_path / "account-active")
    monkeypatch.setattr(paths, "ACCOUNT_STATE", tmp_path / "account-state.json")
    monkeypatch.setattr(paths, "SPAWN_COUNTER", tmp_path / "spawn-counter.json")
    monkeypatch.setattr(paths, "ACCOUNT_USAGE", tmp_path / "usage")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(tmp_path / "no-config.toml"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    for name in ("A", "B", "MAIN", "ALT", "THIRD"):
        monkeypatch.delenv(f"CLAUDE_ORCH_ACCOUNT_WEIGHT_{name}", raising=False)
    return tmp_path


def _use_pool(monkeypatch, tmp_path, toml_text):
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(toml_text)
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))


THREE_POOL = '''
[accounts]
default = "main"
[[accounts.pool]]
name = "main"
[[accounts.pool]]
name = "alt"
[[accounts.pool]]
name = "third"
'''


def test_pick_by_counter_two_accounts_is_legacy_formula():
    # the exact pinned sequences from test_spawner_account must fall out
    seq = [spawner._pick_by_counter(["a", "b"], [6, 4], c) for c in range(10)]
    assert seq == ["a", "b", "a", "b", "a", "a", "b", "a", "b", "a"]
    seq = [spawner._pick_by_counter(["a", "b"], [2, 8], c) for c in range(10)]
    assert seq == ["a", "b", "b", "b", "b", "a", "b", "b", "b", "b"]
    seq = [spawner._pick_by_counter(["a", "b"], [1, 1], c) for c in range(4)]
    assert seq == ["a", "b", "a", "b"]


def test_pick_by_counter_three_accounts_smooth_and_fair():
    names, weights = ["x", "y", "z"], [1, 1, 1]
    seq = [spawner._pick_by_counter(names, weights, c) for c in range(6)]
    assert seq == ["x", "y", "z", "x", "y", "z"]
    names, weights = ["x", "y", "z"], [2, 1, 1]
    period = [spawner._pick_by_counter(names, weights, c) for c in range(4)]
    assert period.count("x") == 2 and period.count("y") == 1 and period.count("z") == 1
    assert period[:2] != ["x", "x"], "smooth WRR must interleave, not clump"


def test_three_account_pool_round_robins(sp, monkeypatch):
    _use_pool(monkeypatch, sp, THREE_POOL)
    (sp / "account-active").write_text("main")
    seq = [spawner._pick_account() for _ in range(6)]
    assert seq == ["main", "alt", "third", "main", "alt", "third"]


def test_pool_off_when_anchor_not_in_registry(sp, monkeypatch):
    _use_pool(monkeypatch, sp, THREE_POOL)
    (sp / "account-active").write_text("a")   # not a pool name in THIS registry
    assert spawner._pick_account() is None
    assert spawner._active_account() is None


def test_three_account_gate_emits_per_account_pct(sp, monkeypatch):
    import time
    _use_pool(monkeypatch, sp, THREE_POOL)
    (sp / "account-active").write_text("main")
    now = time.time()
    usage = sp / "usage"
    usage.mkdir()
    for n in ("main", "alt", "third"):
        (usage / f"{n}.json").write_text(json.dumps(
            {"five_hour_pct": 95, "seven_day_pct": 10,
             "five_hour_resets_at": now + 3600, "ts": now}))
    payload = spawner.usage_spawn_gate()
    assert payload["status"] == "paused"
    assert payload["main_pct"] == 95
    assert payload["alt_pct"] == 95
    assert payload["third_pct"] == 95


def test_default_pool_gate_keys_unchanged(sp, monkeypatch):
    import time
    (sp / "account-active").write_text("a")
    now = time.time()
    usage = sp / "usage"
    usage.mkdir()
    for n in ("a", "b"):
        (usage / f"{n}.json").write_text(json.dumps(
            {"five_hour_pct": 95, "seven_day_pct": 10,
             "five_hour_resets_at": now + 3600, "ts": now}))
    payload = spawner.usage_spawn_gate()
    assert payload["status"] == "paused"
    assert "a_pct" in payload and "b_pct" in payload


def test_worker_model_from_config(sp, monkeypatch):
    _use_pool(monkeypatch, sp, '[spawn]\nworker_model = "sonnet"\n')
    cmd = spawner._runtime_command("claude", "hi", None, None)
    assert "--model sonnet" in cmd
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(sp / "no-config.toml"))
    cmd = spawner._runtime_command("claude", "hi", None, None)
    assert "--model 'opus[1m]'" in cmd or "--model opus[1m]" in cmd


def test_env_weight_override_generalizes(sp, monkeypatch):
    _use_pool(monkeypatch, sp, THREE_POOL)
    monkeypatch.setenv("CLAUDE_ORCH_ACCOUNT_WEIGHT_MAIN", "2")
    (sp / "account-active").write_text("main")
    seq = [spawner._pick_account() for _ in range(4)]
    assert seq.count("main") == 2


def test_identity_scrub_no_real_account_names():
    """The spawner source must never hardcode the operator's real account
    handles. The handles live ONLY in the live dockwright.toml
    ([genericness].extra_forbidden_tokens) so this guard can check them without
    the handles themselves leaking into the repo. Runs on an operator machine
    (non-empty token list); skips on a generic clone (nothing to enforce)."""
    import inspect
    tokens = operator_forbidden_tokens()
    if not tokens:
        pytest.skip("generic clone — operator token list empty")
    src = inspect.getsource(spawner)
    for t in tokens:
        assert t not in src
