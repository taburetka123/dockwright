"""Meta-guards: no test may write the operator's LIVE spend ledger.

The 2026-07-28 incident: fresh_orchestrator_dir patched ROOT/ACTIVE/... but not
SPEND_LEDGER, so dead-pid prunes in test_mcp_tools appended synthetic rows
(sids w1/old-mgr/w-dead, source=prune, spend={}) to the live file — 1784 rows
across 446 suite runs. These tests pin the three prongs of the conftest
_no_live_spend_ledger fixture; see docs/specs/2026-07-28-spend-accounting-design.md.
"""
import importlib.util
import json
import os
import time
from pathlib import Path

from dockwright import paths, spend_ledger, state
from tests.conftest import _LIVE_SPEND_LEDGER

REPO_ROOT = Path(__file__).resolve().parent.parent
PREFLIGHT_PATH = REPO_ROOT / "deploy" / "scripts" / "preflight_cleanup.py"


def test_default_ledger_target_is_redirected_off_live():
    assert paths.SPEND_LEDGER != _LIVE_SPEND_LEDGER
    assert not str(paths.SPEND_LEDGER).startswith(str(_LIVE_SPEND_LEDGER.parent))


def test_dead_pid_prune_lands_in_tmp_not_live(tmp_path, monkeypatch):
    """The exact historical leak shape — dead-pid record pruned during a
    registry sweep — in a test that does NOT patch the ledger itself: the row
    must land in the autouse tmp redirect."""
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "ACTIVE", tmp_path / "active")
    monkeypatch.setattr(paths, "QUESTIONS", tmp_path / "questions")  # prune drops questions too — keep it off the live dir
    paths.ACTIVE.mkdir(parents=True)
    state.write_json_atomic(paths.ACTIVE / "w-dead.json", {
        "claude_sid": "w-dead", "agent": "worker", "name": "beta",
        "pid": 99999999, "started_at": time.time(),
    })
    from dockwright.registry import _prune_stale_active_records
    _prune_stale_active_records()
    assert not (paths.ACTIVE / "w-dead.json").exists()
    rows = [json.loads(l) for l in paths.SPEND_LEDGER.read_text().splitlines()]
    assert [r["sid"] for r in rows] == ["w-dead"]
    assert paths.SPEND_LEDGER != _LIVE_SPEND_LEDGER


def test_live_ledger_write_attempt_is_recorded_and_suppressed(
        _no_live_spend_ledger, monkeypatch):
    """Prong (c): even if a test re-points paths.SPEND_LEDGER at the live file,
    the wrapped _append_line suppresses the write and records the violation
    (append_* swallow exceptions by contract, so raising would be silenced —
    the fixture's teardown assert is what fails the offending test)."""
    monkeypatch.setattr(paths, "SPEND_LEDGER", _LIVE_SPEND_LEDGER)
    spend_ledger.append_drop_event(
        {"claude_sid": "meta-guard", "spend": {"turns": 1, "out_tokens": 1}},
        "prune")
    assert len(_no_live_spend_ledger) == 1
    assert "meta-guard" in _no_live_spend_ledger[0]

    # Subtree hardening: a ".."-bearing alias of the SAME live file must be
    # caught too, not just byte-identical paths (Finding 1 — the guard used to
    # compare paths.SPEND_LEDGER == _LIVE_SPEND_LEDGER by exact equality).
    alias = _LIVE_SPEND_LEDGER.parent / ".." / _LIVE_SPEND_LEDGER.parent.name / _LIVE_SPEND_LEDGER.name
    assert alias.resolve() == _LIVE_SPEND_LEDGER.resolve()
    monkeypatch.setattr(paths, "SPEND_LEDGER", alias)
    spend_ledger.append_drop_event(
        {"claude_sid": "meta-guard-alias", "spend": {"turns": 1, "out_tokens": 1}},
        "prune")
    assert len(_no_live_spend_ledger) == 2
    assert "meta-guard-alias" in _no_live_spend_ledger[1]
    _no_live_spend_ledger.clear()      # consumed — this test must not fail teardown


def test_preflight_fresh_load_lands_in_env_state_dir():
    """Prong (b): a FRESH spec_from_file_location load of the standalone
    preflight_cleanup.py — with NO per-test SPEND_LEDGER patch (the forgotten-
    patch lane) — must bind its ROOT under DOCKWRIGHT_STATE_DIR, set by the
    autouse fixture, never the live HOME-derived path."""
    spec = importlib.util.spec_from_file_location("preflight_fresh_meta", PREFLIGHT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    env_root = Path(os.environ["DOCKWRIGHT_STATE_DIR"])
    assert mod.ROOT == env_root
    assert mod.SPEND_LEDGER == env_root / "spend-ledger.jsonl"
    mod._append_spend_drop(
        {"claude_sid": "meta-preflight", "spend": {"turns": 1, "out_tokens": 2}},
        "preflight_prune")
    rows = [json.loads(l) for l in mod.SPEND_LEDGER.read_text().splitlines()]
    assert [r["sid"] for r in rows] == ["meta-preflight"]


def test_preflight_append_creates_missing_state_dir(tmp_path, monkeypatch):
    """The PRODUCTION mkdir in _append_spend_drop: on a machine whose state
    root does not exist yet, the append must create it rather than being
    silently swallowed by the function's best-effort except."""
    monkeypatch.setenv("DOCKWRIGHT_STATE_DIR", str(tmp_path / "never-created"))
    spec = importlib.util.spec_from_file_location("preflight_mkdir_meta", PREFLIGHT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._append_spend_drop(
        {"claude_sid": "meta-mkdir", "spend": {"turns": 1, "out_tokens": 3}},
        "preflight_prune")
    rows = [json.loads(l) for l in mod.SPEND_LEDGER.read_text().splitlines()]
    assert [r["sid"] for r in rows] == ["meta-mkdir"]


def test_every_testpaths_root_carries_the_ledger_guard():
    """A sibling test root does not inherit tests/conftest.py — the exact lane
    the 2026-07-29 Tier-2 probe found unguarded (evals/tests/ had none of the
    three prongs). Every root named in pyproject testpaths must define the
    autouse _no_live_spend_ledger fixture somewhere in its own conftest chain,
    so a new sibling root cannot silently opt out of the guard."""
    import tomllib
    with open(REPO_ROOT / "pyproject.toml", "rb") as f:
        testpaths = tomllib.load(f)["tool"]["pytest"]["ini_options"]["testpaths"]
    assert testpaths, "testpaths vanished from pyproject.toml — nothing to pin"
    for root in testpaths:
        chain = []
        directory = (REPO_ROOT / root).resolve()
        while True:
            candidate = directory / "conftest.py"
            if candidate.is_file():
                chain.append(candidate)
            if directory == REPO_ROOT:
                break
            directory = directory.parent
        assert any(_defines_autouse_ledger_guard(c) for c in chain), (
            f"testpaths root {root!r} has no conftest defining the autouse "
            f"_no_live_spend_ledger fixture (checked: {[str(c) for c in chain]})")


def _defines_autouse_ledger_guard(conftest_path: Path) -> bool:
    """Structurally (never by substring): load the conftest and check the
    fixture object's own autouse marker."""
    spec = importlib.util.spec_from_file_location(
        f"conftest_guard_check_{abs(hash(conftest_path))}", conftest_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = getattr(module, "_no_live_spend_ledger", None)
    marker = getattr(fixture, "_fixture_function_marker", None)
    return marker is not None and getattr(marker, "autouse", False) is True
