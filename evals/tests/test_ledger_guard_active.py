"""Probe: the three live-ledger guard prongs are ACTIVE in this test root.

The 2026-07-29 Tier-2 review placed exactly this probe here and found all
three prongs absent — evals/tests/ is a sibling of tests/, so the top-level
conftest's autouse guard never reached it. This test keeps the probe as a
permanent guard: it fails if this root's own conftest copy is removed or
neutered."""
import os

from dockwright import paths, spend_ledger


def test_ledger_guard_prongs_active_here():
    # prong (a): redirect off the live path
    assert paths.SPEND_LEDGER.name == "no-live-spend-ledger.jsonl"
    # prong (b): fresh standalone loads land in the env state dir
    state_dir = os.environ.get("DOCKWRIGHT_STATE_DIR")
    assert state_dir and os.path.isdir(state_dir)
    # prong (c): _append_line is the guard wrapper, not the raw appender
    assert spend_ledger._append_line.__name__ == "guarded"
