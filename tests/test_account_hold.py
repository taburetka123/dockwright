import json
import time

import pytest

from dockwright import account_hold, config, paths

ANY_INT = object()


@pytest.fixture
def hold_env(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ACCOUNT_STATE", tmp_path / "account-state.json")
    monkeypatch.setattr(paths, "ACCOUNT_LOCK", tmp_path / ".account-flip.lock")
    monkeypatch.setattr(paths, "ACCOUNT_LEDGER", tmp_path / "account-flips.jsonl")
    monkeypatch.setattr(paths, "ACCOUNT_ACTIVE", tmp_path / "account-active")
    cfg = tmp_path / "two-pool.toml"
    cfg.write_text('[accounts]\ndefault = "a"\n[[accounts.pool]]\nname = "a"\n[[accounts.pool]]\nname = "b"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    return tmp_path


def _state(hold_env):
    return json.loads((hold_env / "account-state.json").read_text())


def _ledger(hold_env):
    return [json.loads(line) for line in (hold_env / "account-flips.jsonl").read_text().splitlines() if line]


def test_list_with_nothing_held_prints_no_account_on_hold(hold_env, capsys):
    assert account_hold.main([]) == 0
    assert capsys.readouterr().out.strip() == "no account on hold"


def test_hold_writes_the_mark_and_the_ledger_and_touches_nothing_else(hold_env, capsys):
    (hold_env / "account-active").write_text("b\n")
    before = int(time.time())
    assert account_hold.main(["b", "--reason", "appeal pending"]) == 0
    mark = _state(hold_env)["held"]["b"]
    assert before <= mark.pop("at") <= int(time.time()) + 1
    assert mark == {"reason": "appeal pending", "source": "cli"}
    assert (hold_env / "account-active").read_text() == "b\n"
    assert _ledger(hold_env)[-1]["event"] == "hold" and _ledger(hold_env)[-1]["by"] == "cli"
    assert "running sessions finish on it" in capsys.readouterr().out


def test_hold_keeps_other_state_keys(hold_env):
    (hold_env / "account-state.json").write_text(json.dumps({"accounts": {"a": {"bricked_at": 5}}, "last_flip": {"ts": 1}}))
    assert account_hold.main(["b"]) == 0
    state = _state(hold_env)
    assert state["accounts"] == {"a": {"bricked_at": 5}} and state["last_flip"] == {"ts": 1}
    assert state["held"]["b"]["reason"] == "operator"


def test_hold_twice_reports_already_on_hold(hold_env, capsys):
    assert account_hold.main(["b"]) == 0
    capsys.readouterr()
    assert account_hold.main(["b"]) == 0
    assert "already on hold since" in capsys.readouterr().out
    assert len(_ledger(hold_env)) == 1


def test_release_removes_the_mark(hold_env, capsys):
    assert account_hold.main(["b"]) == 0
    assert account_hold.main(["--release", "b"]) == 0
    assert "held" in _state(hold_env) and "b" not in _state(hold_env)["held"]
    assert _ledger(hold_env)[-1]["event"] == "release"
    assert "released" in capsys.readouterr().out


def test_release_of_an_account_not_held_is_rc_1(hold_env, capsys):
    assert account_hold.main(["--release", "b"]) == 1
    assert "b is not on hold" in capsys.readouterr().err
    assert not (hold_env / "account-flips.jsonl").exists()


def test_an_unknown_account_is_rc_2(hold_env, capsys):
    assert account_hold.main(["zz"]) == 2
    assert "unknown account" in capsys.readouterr().err
    assert not (hold_env / "account-state.json").exists()
    assert account_hold.main(["--release", "zz"]) == 2


def test_bad_usage_is_rc_2(hold_env, capsys):
    assert account_hold.main(["--release"]) == 2
    assert account_hold.main(["a", "b"]) == 2
    assert "usage:" in capsys.readouterr().err


def test_list_prints_each_held_account(hold_env, capsys):
    (hold_env / "account-state.json").write_text(json.dumps({"accounts": {}, "held": {
        "a": {"at": 1_760_000_000, "reason": "banner", "source": "worker:w1"},
        "b": {"at": 1_760_000_060, "reason": "appeal pending", "source": "cli"}}}))
    assert account_hold.main([]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == ["a since 2025-10-09T08:53Z reason=banner source=worker:w1",
                     "b since 2025-10-09T08:54Z reason=appeal pending source=cli"]


def test_release_of_a_mark_outside_the_pool_still_releases(hold_env, capsys):
    (hold_env / "account-state.json").write_text(json.dumps(
        {"accounts": {}, "held": {"c": {"at": 1, "reason": "x", "source": "cli"}}}))
    assert account_hold.main(["--release", "c"]) == 0
    assert _state(hold_env)["held"] == {}
    assert _ledger(hold_env)[-1] == {**_ledger(hold_env)[-1], "event": "release", "account": "c"}
