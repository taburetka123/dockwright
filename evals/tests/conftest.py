"""Shared test guards: no eval test may read the operator's real dockwright.toml
or write the operator's live spend ledger.

Mirrors tests/conftest.py::_dockwright_config_hermetic and
::_no_live_spend_ledger, duplicated here because a pytest conftest.py's
fixtures apply to its own directory and subtree only — evals/tests/ is a
SIBLING of tests/, not a descendant, so the top-level suite's autouse fixtures
never reach it despite both being under the same `testpaths` list in
pyproject.toml. tests/test_spend_ledger_isolation.py's testpaths sweep fails
if any testpaths root loses its own copy of the ledger guard."""
import pytest

from dockwright import paths

# The operator's real ledger path, captured before any test patches paths —
# the forbidden target the _no_live_spend_ledger guard checks against.
_LIVE_SPEND_LEDGER = paths.SPEND_LEDGER


@pytest.fixture(autouse=True)
def _dockwright_config_hermetic(monkeypatch, tmp_path):
    """Every eval test runs as if no dockwright.toml exists unless it sets
    DOCKWRIGHT_CONFIG itself — an operator's real ~/.claude/dockwright.toml
    must never leak into the suite. An explicit env path that doesn't exist
    is authoritative 'no config' per config.config_path() / runner._toml_config().
    A test that calls monkeypatch.setenv("DOCKWRIGHT_CONFIG", ...) itself wins
    for the duration of that test — monkeypatch layers same-key writes and
    unwinds them in reverse order at teardown, so the test's own value is the
    live one while it runs and the true original is restored after."""
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "no-dockwright.toml"))


@pytest.fixture(autouse=True)
def _no_live_spend_ledger(monkeypatch, tmp_path):
    """No eval test may write the operator's LIVE spend-ledger.jsonl — the
    2026-07-29 Tier-2 probe proved this sibling root got NONE of the three
    prongs the tests/ conftest carries (the 2026-07-28 contamination class,
    still open one directory over). Same three prongs as tests/conftest.py:
      (a) redirect paths.SPEND_LEDGER to tmp for every test;
      (b) DOCKWRIGHT_STATE_DIR redirects any fresh standalone load of
          preflight_cleanup.py;
      (c) fail-loud: spend_ledger._append_line is wrapped to SUPPRESS + RECORD
          any write still aimed at the live subtree — the teardown assert
          fails the offending test."""
    from dockwright import spend_ledger
    monkeypatch.setattr(paths, "SPEND_LEDGER", tmp_path / "no-live-spend-ledger.jsonl")
    state_dir = tmp_path / "no-live-state"
    state_dir.mkdir()          # preflight's ledger append does no parent mkdir
    monkeypatch.setenv("DOCKWRIGHT_STATE_DIR", str(state_dir))
    violations: list[str] = []
    real_append = spend_ledger._append_line

    def guarded(entry):
        target = paths.SPEND_LEDGER
        try:
            is_live = target.resolve().is_relative_to(_LIVE_SPEND_LEDGER.parent.resolve())
        except OSError:
            is_live = True   # unresolvable target: fail safe, treat as live
        if is_live:
            violations.append(f"live-ledger write attempted: {entry!r}")
            return
        real_append(entry)

    monkeypatch.setattr(spend_ledger, "_append_line", guarded)
    yield violations
    assert not violations, (
        "test attempted to write the LIVE spend ledger: " + "; ".join(violations))
