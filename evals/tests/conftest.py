import pytest

from dockwright import paths

_LIVE_SPEND_LEDGER = paths.SPEND_LEDGER


@pytest.fixture(autouse=True)
def _dockwright_config_hermetic(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "no-dockwright.toml"))


@pytest.fixture(autouse=True)
def _no_live_spend_ledger(monkeypatch, tmp_path):
    from dockwright import spend_ledger
    monkeypatch.setattr(paths, "SPEND_LEDGER", tmp_path / "no-live-spend-ledger.jsonl")
    state_dir = tmp_path / "no-live-state"
    state_dir.mkdir()
    monkeypatch.setenv("DOCKWRIGHT_STATE_DIR", str(state_dir))
    violations: list[str] = []
    real_append = spend_ledger._append_line

    def guarded(entry):
        target = paths.SPEND_LEDGER
        try:
            is_live = target.resolve().is_relative_to(_LIVE_SPEND_LEDGER.parent.resolve())
        except OSError:
            is_live = True
        if is_live:
            violations.append(f"live-ledger write attempted: {entry!r}")
            return
        real_append(entry)

    monkeypatch.setattr(spend_ledger, "_append_line", guarded)
    yield violations
    assert not violations, (
        "test attempted to write the LIVE spend ledger: " + "; ".join(violations))
