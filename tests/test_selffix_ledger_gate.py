"""The selffix ledger is written with DEBUG OFF, and the freshness gate over it
actually goes green and red.

Guard class: `~/.claude/rules/drift-guard-tests.md`. Until 2026-07-22 the
trigger's `log_line()` opened with `[ "$DEBUG" = "1" ] || return 0`, so the
registry's `event_paths: …/trigger.log` + `max_silence_hours: 48` was a
permanently-red-by-construction gate: `loops_status.py` reported
`STALE (141h since last event, limit 48h)` while the SessionEnd hook was wired
and the loop was writing findings normally. Nothing caught it, because nothing
had ever watched the gate go green.

Anchoring: every assertion below binds to the FILE THE SCRIPT PRODUCES and to
the REAL registry block's values (only `status:` is flipped to `live`, since the
shipped block is `pending-install` and freshness is only evaluated when live).
No assertion substring-matches the script's or the registry's prose, so a
comment mentioning `trigger.log` can never satisfy one.

RED proofs — each counted by re-running, not by estimate:

1. Re-add `[ "$DEBUG" = "1" ] || return 0` as the first line of `log_line()` in
   `deploy/scripts/selffix-trigger.sh` -> **6 of 9 fail**:
   `test_outcome_line_written_with_debug_off`, `test_exactly_one_ledger_line_per_fire`,
   `test_registry_event_path_is_what_the_script_writes`,
   `test_freshness_is_fresh_after_a_fire`,
   `test_freshness_goes_stale_when_the_ledger_ages_out`,
   `test_ledger_written_on_an_early_exit_path`.
2. Quietly lower `max_silence_hours` 72 -> 48 -> **1 of 9 fails**:
   `test_max_silence_keeps_margin_over_observed_fire_gaps`.
Both restore to 9 passed.

Known non-binding, stated rather than implied (delete-one-line sweep):
`test_prune_counter_stays_debug_only` and
`test_freshness_reports_stale_when_the_loop_never_fired` survive BOTH neuterings —
they cover adjacent behavior, not the guard itself.
"""
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
    """The shipped ```loop block for the selffix loop, verbatim."""
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
    """A synthetic $HOME with the trigger deployed and NO debug flag anywhere.

    Deliberately unlike tests/test_selffix_detect.py's fixture, which touches
    `~/.claude/selffix-debug` — that is exactly the configuration under which the
    old DEBUG gate looked fine, so it could never have caught this.
    """
    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(SELFFIX_TRIGGER, scripts / "selffix-trigger.sh")
    shutil.copy(SCRIPTS / "loop-label-prefix.sh", scripts / "loop-label-prefix.sh")
    # Canonical human-fix-flag predicate, imported by the trigger (setup.sh
    # deploys both together). Absent, the fire reports a
    # `fix-predicate-unavailable` reason — a different path than these tests mean.
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
    """One SessionEnd invocation; returns the ledger's non-blank lines."""
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
    """The event source the freshness gate watches must exist without opting in."""
    lines = _fire(home)
    assert lines, (
        "no ledger line with DEBUG off — event_paths points at a file the loop "
        "does not write, so max_silence_hours can never go green")
    assert _verbs(lines) == ["none"], _verbs(lines)


def test_exactly_one_ledger_line_per_fire(home):
    """`ledger_path` promises one line per fire; the prune counter must not
    double it (that is what keeps the always-on volume honest)."""
    _fire(home, "s1")
    _fire(home, "s2")
    assert len(_fire(home, "s3")) == 3


def test_prune_counter_stays_debug_only(home):
    assert "prune" not in _verbs(_fire(home, "s1"))
    assert "prune" in _verbs(_fire(home, "s2", debug=True))


def test_registry_event_path_is_what_the_script_writes(home):
    """Bind the declared event source to the path the script actually assigns —
    the executed `LOG=` line, with comments stripped, not any prose that names
    the file."""
    code = "\n".join(
        ln for ln in SELFFIX_TRIGGER.read_text().splitlines()
        if not ln.lstrip().startswith("#"))
    m = re.search(r'^LOG="([^"]+)"', code, re.M)
    assert m, "no executed LOG= assignment in selffix-trigger.sh"
    assigned = m.group(1).replace("$HOME", "~")
    assert _field(_selffix_block(), "event_paths") == assigned
    # …and the script really produces it, so the binding is not just textual.
    _fire(home)
    assert _log(home).is_file()


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
    """The SHIPPED selffix block with only `status:` flipped live, so the freshness
    legs run against the real `event_paths` / `max_silence_hours`.

    Scope of what that actually catches, stated precisely: repointing
    `event_paths` at a file the loop does not write turns these red, and so does
    `max_silence_hours: none`. Lowering the NUMBER does not — the aging leg reads
    the declared limit and ages past it, so it is self-consistent at any numeric
    value. `test_max_silence_keeps_margin_over_observed_fire_gaps` below is what
    guards the number."""
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
    """The other half: watch it go red, and for the right reason."""
    _fire(home)
    limit = float(_field(_selffix_block(), "max_silence_hours"))
    aged = time.time() - (limit + 1) * 3600
    os.utime(_log(home), (aged, aged))
    freshness = _freshness(home, live_registry)
    assert freshness.startswith("STALE"), freshness
    assert f"limit {limit:.0f}h" in freshness, freshness


def test_freshness_reports_stale_when_the_loop_never_fired(home, live_registry):
    """A missing ledger must read STALE, never silently pass as fresh."""
    assert _freshness(home, live_registry) == "STALE (no events found)"


# PROVENANCE — this is an OBSERVATION, not a measured invariant of the system.
# Source: ONE operator's `~/.claude/dockwright/selffix/trigger.log` (4095 lines,
# 2026-05-19 -> 2026-07-16), read on 2026-07-22, counting only the windows where
# debug logging was on and excluding the two known dark windows (403.8h severed
# hook, 583.9h debug-off). Worst real gap between fires there: 32.5h
# (2026-06-20->06-22), then 31.2h and 26.9h.
#
# A different machine — lighter use, a laptop that sleeps, an operator who takes
# weeks off — will have a LARGER worst gap, and this number does not describe it.
# What the assertion below is actually defending is narrower and does generalize:
# `max_silence_hours` must not be quietly lowered toward the observed working
# range without fresh evidence, because a limit at or below real fire cadence
# reports a false STALE during ordinary quiet periods — the "always red, so
# ignore the report" failure this whole entry exists to fix. Re-derive from your
# own trigger.log before treating 32.5 as true here.
WORST_OBSERVED_FIRE_GAP_HOURS = 32.5


def test_max_silence_keeps_margin_over_observed_fire_gaps():
    """Guard the NUMBER, not just the mechanism (the freshness legs above are
    self-consistent at any numeric limit, so they cannot catch a quiet lowering).
    Moving this below 2x the worst observed gap must be a deliberate edit here,
    with fresh evidence — the same contract test_agent_size_ceiling.py uses."""
    declared = _field(_selffix_block(), "max_silence_hours")
    assert declared != "none", (
        "max_silence_hours: none disarms the gate entirely — that was the "
        "pre-2026-07-22 state this entry exists to leave behind")
    assert float(declared) >= 2 * WORST_OBSERVED_FIRE_GAP_HOURS, (
        f"max_silence_hours={declared} leaves under 2x margin over the worst "
        f"observed fire gap ({WORST_OBSERVED_FIRE_GAP_HOURS}h); a normal quiet "
        f"period would report a false STALE")


def test_ledger_written_on_an_early_exit_path(home):
    """The `mkdir -p` moved out of the DEBUG branch is load-bearing on the exits
    that fire BEFORE the dedup dir is created (empty stdin, bad JSON, module-off):
    on a fresh machine nothing else has made the log's parent directory yet."""
    env = {**os.environ, "HOME": str(home)}
    env.pop("SELFFIX_DEBUG", None)
    subprocess.run(
        ["bash", str(home / ".claude" / "scripts" / "selffix-trigger.sh")],
        input="", text=True, timeout=15, check=False, capture_output=True, env=env,
    )
    assert _log(home).is_file(), "no ledger dir/file on the empty-payload early exit"
    assert _verbs([ln for ln in _log(home).read_text().splitlines() if ln.strip()]) \
        == ["skip:no-payload"]
