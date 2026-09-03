import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "deploy" / "scripts"
SELFFIX_TRIGGER = SCRIPTS / "selffix-trigger.sh"
LOOPS_STATUS = SCRIPTS / "loops_status.py"
REGISTRY = REPO_ROOT / "deploy" / "loops-registry.md"


def _selffix_block() -> str:
    for block in re.findall(r"```loop\n(.*?)```", REGISTRY.read_text(), re.S):
        if re.search(r"^name:\s*selffix\s*$", block, re.M):
            return block
    pytest.fail("no selffix ```loop block in deploy/loops-registry.md")


def _field(block: str, key: str) -> str:
    m = re.search(rf"^{key}:\s*(.+)$", block, re.M)
    assert m, f"selffix block has no {key}"
    return m.group(1).strip()


@pytest.fixture
def home(tmp_path):
    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(SELFFIX_TRIGGER, scripts / "selffix-trigger.sh")
    shutil.copy(SCRIPTS / "loop-label-prefix.sh", scripts / "loop-label-prefix.sh")
    shutil.copy(SCRIPTS / "transcript_signal.py", scripts / "transcript_signal.py")
    run_stub = scripts / "selffix-run.sh"
    run_stub.write_text("#!/bin/bash\nexit 0\n")
    run_stub.chmod(0o755)
    assert not (tmp_path / ".claude" / "dockwright" / "selffix" / "debug").exists()
    assert not (tmp_path / ".claude" / "selffix-debug").exists()
    return tmp_path


def _log(home: Path) -> Path:
    return home / ".claude" / "dockwright" / "selffix" / "trigger.log"


def _fire(home: Path, sid="s1", *, debug=False) -> list[str]:
    project = home / ".claude" / "projects" / "p"
    project.mkdir(parents=True, exist_ok=True)
    transcript = project / f"{sid}.jsonl"
    transcript.write_text(json.dumps(
        {"type": "user", "message": {"content": "hello"}}) + "\n")
    env = {**os.environ, "HOME": str(home)}
    env.pop("SELFFIX_DEBUG", None)
    if debug:
        env["SELFFIX_DEBUG"] = "1"
    subprocess.run(
        ["bash", str(home / ".claude" / "scripts" / "selffix-trigger.sh")],
        input=json.dumps({"session_id": sid, "transcript_path": str(transcript)}),
        text=True, timeout=15, check=False, capture_output=True, env=env,
    )
    if not _log(home).is_file():
        return []
    return [ln for ln in _log(home).read_text().splitlines() if ln.strip()]


def _verbs(lines: list[str]) -> list[str]:
    return [ln.split("  ")[1] for ln in lines if len(ln.split("  ")) >= 2]


def test_outcome_line_written_with_debug_off(home):
    lines = _fire(home)
    assert lines, (
        "no ledger line with DEBUG off — event_paths points at a file the loop "
        "does not write, so max_silence_hours can never go green")
    assert _verbs(lines) == ["none"], _verbs(lines)


def test_exactly_one_ledger_line_per_fire(home):
    _fire(home, "s1")
    _fire(home, "s2")
    assert len(_fire(home, "s3")) == 3


def test_prune_counter_stays_debug_only(home):
    assert "prune" not in _verbs(_fire(home, "s1"))
    assert "prune" in _verbs(_fire(home, "s2", debug=True))


def _freshness(home: Path, registry: Path) -> str:
    out = subprocess.run(
        ["python3", str(LOOPS_STATUS), "--json", "--registry", str(registry)],
        capture_output=True, text=True, check=True,
        env={**os.environ, "HOME": str(home)},
    ).stdout
    for report in json.loads(out):
        if report["name"] == "selffix":
            return report.get("freshness", "")
    pytest.fail("selffix missing from loops_status report")


@pytest.fixture
def live_registry(tmp_path):
    block = _selffix_block()
    assert _field(block, "status") == "pending-install"
    path = tmp_path / "registry.md"
    path.write_text("### selffix\n\n```loop\n"
                    + block.replace("status: pending-install", "status: live", 1)
                    + "```\n")
    return path


def test_freshness_is_fresh_after_a_fire(home, live_registry):
    _fire(home)
    assert _freshness(home, live_registry).startswith("fresh"), (
        "the gate did not go green after a real SessionEnd fire")


def test_freshness_goes_stale_when_the_ledger_ages_out(home, live_registry):
    _fire(home)
    limit = float(_field(_selffix_block(), "max_silence_hours"))
    aged = time.time() - (limit + 1) * 3600
    os.utime(_log(home), (aged, aged))
    freshness = _freshness(home, live_registry)
    assert freshness.startswith("STALE"), freshness
    assert f"limit {limit:.0f}h" in freshness, freshness


def test_freshness_reports_stale_when_the_loop_never_fired(home, live_registry):
    assert _freshness(home, live_registry) == "STALE (no events found)"


def test_ledger_written_on_an_early_exit_path(home):
    env = {**os.environ, "HOME": str(home)}
    env.pop("SELFFIX_DEBUG", None)
    subprocess.run(
        ["bash", str(home / ".claude" / "scripts" / "selffix-trigger.sh")],
        input="", text=True, timeout=15, check=False, capture_output=True, env=env,
    )
    assert _log(home).is_file(), "no ledger dir/file on the empty-payload early exit"
    assert _verbs([ln for ln in _log(home).read_text().splitlines() if ln.strip()]) \
        == ["skip:no-payload"]
