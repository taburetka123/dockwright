from dataclasses import dataclass
from pathlib import Path

import pytest

from dockwright import compose

CORE_DIR = Path(__file__).resolve().parents[1] / "deploy" / "agents"

MEASURE_HOME = "/home/dockwright"


@dataclass(frozen=True)
class Ceiling:
    bytes: int
    lines: int


SHIPPED_CEILINGS = {
    "manager.md": Ceiling(bytes=36_099, lines=305),
    "worker.md": Ceiling(bytes=17_532, lines=165),
    "dockwright-reviewer.md": Ceiling(bytes=4_275, lines=74),
}


@pytest.fixture
def shipped(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", MEASURE_HOME)
    out = tmp_path / "out"
    result = compose.compose_agents(CORE_DIR, out, tmp_path / "no-overlay", {})
    assert not result["warnings"], (
        f"compose warnings {result['warnings']} — a {{{{var}}}} with no binding "
        f"in deploy/agents/vars.defaults.toml stays literal and renders SHORTER "
        f"than its expansion, so this ceiling would pass on a render nobody "
        f"ships; bind the var first")
    assert sorted(result["files"]) == sorted(SHIPPED_CEILINGS), (
        f"deploy/agents renders {sorted(result['files'])} but SHIPPED_CEILINGS "
        f"covers {sorted(SHIPPED_CEILINGS)} — an agent with no entry here is an "
        f"always-on file with no ceiling at all; add its measured size plus "
        f"headroom, or drop the stale entry")
    return out


def _measured(out: Path, name: str) -> tuple[int, int]:
    p = out / name
    return p.stat().st_size, len(p.read_text().splitlines())


_ADVICE = (
    "Compress something in the same PR to pay for this — do NOT raise the "
    "constant to make your own diff fit. The ceiling is OPERATOR-SET: it moves "
    "when the operator says so, on the corpus as a whole, never as a side "
    "effect of the change in flight.")


@pytest.mark.parametrize("name", sorted(SHIPPED_CEILINGS))
def test_shipped_byte_ceiling(name, shipped):
    size, _ = _measured(shipped, name)
    ceiling = SHIPPED_CEILINGS[name]
    assert size <= ceiling.bytes, (
        f"{name} renders to {size} bytes (> {ceiling.bytes}) from "
        f"{CORE_DIR}/{Path(name).stem}.core.md plus vars.defaults.toml, with no "
        f"operator overlay and HOME={MEASURE_HOME}. {_ADVICE}")


@pytest.mark.parametrize("name", sorted(SHIPPED_CEILINGS))
def test_shipped_line_ceiling(name, shipped):
    _, lines = _measured(shipped, name)
    ceiling = SHIPPED_CEILINGS[name]
    assert lines <= ceiling.lines, (
        f"{name} renders to {lines} lines (> {ceiling.lines}) from "
        f"{CORE_DIR}/{Path(name).stem}.core.md plus vars.defaults.toml, with no "
        f"operator overlay. {_ADVICE}")
