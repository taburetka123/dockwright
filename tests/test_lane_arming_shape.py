"""An armed lane must never discard its scan's exit code.

The retired form ran the scan as a loop BODY, where its exit code is thrown
away, so a lane whose reader died — or whose owning manager is gone — spun
forever while `TaskOutput` reported `running`. Four such loops were found still
scanning 7 days 21 hours after their manager exited.

The shipped form is:

    while dockwright monitor <lane> || exit $?; do sleep N; done

which runs the scan as the loop CONDITION (so a non-zero exit ends the loop)
and propagates that exact code out of the shell, so the Monitor task exits
non-zero instead of looking like a clean finish. Both halves were measured in
zsh and bash before being adopted.

The guard checks the PROPERTY — "a scan inside a shell loop must be able to
end that loop" — rather than banning one spelling. A spelling ban passes the
moment someone writes `while :;` or `until`, which are the same bug; the
property check catches every shape because it enumerates broadly (every line
that runs a scan inside a loop) and then asserts on each. Enumerate wide,
assert narrow: over-inclusion costs a redundant case, under-inclusion is
invisible.

⚠️ Every retired literal below is ASSEMBLED, never written out. Spelling one
would make this file its own first offender, and the natural fix for that —
exempting the guard from itself — is how a guard quietly stops guarding.
"""
import re
from pathlib import Path

import pytest

from dockwright import lane_io

REPO = Path(__file__).resolve().parents[1]

# `tests` is a SCOPE boundary, not a self-exemption: nothing under it is
# deployed (setup.sh copies deploy/ and src/, never tests/), so no file there
# can arm a real lane, while this module deliberately carries unsafe shapes as
# fixtures — and the tests below assert those fixtures are CAUGHT.
# test_the_deployed_surface_is_inside_the_scan pins the boundary.
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".pytest_cache",
             "tests"}
# A DENYLIST, not an allowlist. The allowlist version missed an extensionless
# `deploy/scripts/arm-lanes` and any `.yaml` preset — and the next text format
# someone adds would be missed too, which is the hand-maintained-set failure
# this file's own docstring warns about. Skipping known-binary suffixes and
# reading everything else covers a new format by construction; a file that
# cannot be decoded is skipped at read time anyway.
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip",
                 ".gz", ".tar", ".whl", ".pyc", ".so", ".dylib", ".db",
                 ".sqlite", ".jsonl", ".lock"}

# Loop keywords that can carry a scan. `for` is here because a future
# `for i in $(seq …); do dockwright monitor …; done` is the same defect.
LOOP_KEYWORDS = ("while ", "until ", "for ")

# What makes an arming construct SAFE: the scan's failure must both END the
# loop AND leave the shell with a non-zero status, so the Monitor task exit
# reads as an anomaly rather than a clean finish.
#
# An earlier version accepted any `|| exit` and either `break`, and a reviewer
# broke all three: `|| exit 0` ends the loop and exits 0 (manager never told);
# `|| break` and `&& break` leave the loop's own status, which is 0. Only the
# propagating form qualifies, and it is matched exactly rather than by family.
SAFE_PATTERN = re.compile(r"\|\|\s*exit\s+\$\?")

_RETIRED_LOOP = "while " + "true"

# DECLARED EXEMPTION, the only one. The design doc quotes the retired form to
# describe the bug it removes; a doc that cannot name what it fixed is worse
# than the lint. Anything else matching is a live defect.
EXEMPT = {"docs/specs/2026-08-06-monitor-lane-liveness-design.md"}


def _candidate_files(root: Path):
    """Every plausible carrier. Deliberately unfiltered beyond file type — a
    classifier that decided which files 'could' arm a lane would fail open on
    the first shape it did not recognise."""
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix in SKIP_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


# Windowed over WHITESPACE-NORMALIZED text, not per line: a fenced code block
# spreads the same construct across four lines, and a line-based scan walks
# straight past it. The reviewer demonstrated exactly that escape.
_WS = re.compile(r"\s+")
_SCAN = re.compile(r"dockwright monitor [a-z-]+")
_WINDOW = 160


def _arming_lines(root: Path):
    """(relpath, occurrence, text) for every shell loop that runs a scan.

    A window qualifies only when it carries an actual loop — a loop keyword
    AND `; do` AND `done` — because prose in these files mentions
    `dockwright monitor` constantly (`pgrep -f "dockwright monitor <lane>"`,
    "For Claude managers, …") and matching the command alone fires on English.
    """
    found = []
    for path in _candidate_files(root):
        rel = path.relative_to(root).as_posix()
        if rel in EXEMPT:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        flat = _WS.sub(" ", text)
        for n, match in enumerate(_SCAN.finditer(flat), start=1):
            window = flat[max(0, match.start() - _WINDOW):
                          match.end() + _WINDOW]
            if not any(keyword in window for keyword in LOOP_KEYWORDS):
                continue
            if "; do" not in window or "done" not in window:
                continue
            found.append((rel, n, window.strip()))
    return found


def _unsafe(root: Path):
    return [(rel, n, window) for rel, n, window in _arming_lines(root)
            if not SAFE_PATTERN.search(window)]


def test_the_scan_is_never_run_where_its_exit_code_is_discarded():
    unsafe = _unsafe(REPO)
    assert unsafe == [], (
        "these lines run a monitor scan inside a shell loop without letting a "
        "non-zero exit end it, so a dead lane would spin forever: "
        f"{[(r, n) for r, n, _ in unsafe]}. Use "
        "`while dockwright monitor <lane> || exit $?; do sleep N; done`.")


def test_an_extensionless_or_yaml_arming_site_is_caught(tmp_path):
    """Both shapes the allowlist missed. `deploy/scripts/` holds extensionless
    executables today, so this is not hypothetical."""
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "arm-lanes").write_text(
        f"#!/bin/sh\n{_RETIRED_LOOP}; do dockwright monitor done; sleep 2; done\n")
    (tmp_path / "deploy" / "preset.yaml").write_text(
        f"command: {_RETIRED_LOOP}; do dockwright monitor stale; sleep 60; done\n")
    caught = {r for r, _, _ in _unsafe(tmp_path)}
    assert caught == {"deploy/arm-lanes", "deploy/preset.yaml"}, caught


def test_the_deployed_surface_is_inside_the_scan():
    """Pins the `tests` scope boundary.

    Excluding a directory is how a guard silently narrows. This asserts the
    exclusion cannot hide a deployed surface: every path setup.sh ships from
    is scanned, and `tests/` is not one of them.
    """
    scanned_roots = {p.relative_to(REPO).parts[0] for p in _candidate_files(REPO)}
    for shipped in ("deploy", "src"):
        assert shipped in scanned_roots, (
            f"{shipped}/ is deployed but excluded from the scan")
    setup = (REPO / "setup.sh").read_text(encoding="utf-8")
    assert "tests/" not in setup or "cp" not in setup.split("tests/")[0][-40:], (
        "setup.sh may now deploy tests/ — the scope exclusion above would then "
        "hide a real arming surface")


def test_the_repo_actually_contains_arming_lines_to_check():
    """A property check over an empty set passes vacuously. If the enumeration
    ever stops finding the real arming lines, this fails instead of the suite
    going quietly green over nothing."""
    lines = _arming_lines(REPO)
    assert len(lines) >= 20, (
        f"only {len(lines)} arming lines found; the enumeration is probably "
        f"broken, and a guard that finds nothing checks nothing")


def test_guard_catches_a_new_unsafe_occurrence(tmp_path):
    """ADD-ONE: the guard must fail on a surface that does not exist yet.

    Deleting one of the known edits is the easy half. This bug returns when
    someone ADDS a place that arms a lane — a new command file, a script, a
    README — so that is what gets proven.
    """
    (tmp_path / "deploy").mkdir()
    candidate = tmp_path / "deploy" / "some-new-command.md"
    candidate.write_text(
        "Arm it with `while dockwright monitor done || exit $?; do sleep 2; done`.\n")
    assert _unsafe(tmp_path) == []

    candidate.write_text(
        f"Arm it with `{_RETIRED_LOOP}; do dockwright monitor done; sleep 2; done`.\n")
    assert [r for r, _, _ in _unsafe(tmp_path)] == ["deploy/some-new-command.md"]


@pytest.mark.parametrize("shape", [
    "{loop}; do dockwright monitor done; sleep 2; done",
    "while :; do dockwright monitor done; sleep 2; done",
    "until false; do dockwright monitor done; sleep 2; done",
    "for i in 1 2 3; do dockwright monitor done; done",
    # Condition-driven, so the loop DOES end — but the shell still exits 0 and
    # the manager reads a clean finish.
    "while dockwright monitor done; do sleep 2; done",
    # Ends the loop and exits 0. The reviewer's first escape.
    "while dockwright monitor done || exit 0; do sleep 2; done",
    # `break` leaves the LOOP's status, which is 0. Both escapes.
    "while :; do dockwright monitor done || break; sleep 2; done",
    "{loop}; do dockwright monitor done && break; sleep 2; done",
])
def test_guard_catches_every_unsafe_loop_shape(tmp_path, shape):
    """A spelling ban would catch only the first of these. The last one is the
    subtlest: condition-driven, so the loop DOES end — but the shell still
    exits 0, which reads to a manager as a clean finish rather than a death."""
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "x.md").write_text(
        f"`{shape.format(loop=_RETIRED_LOOP)}`\n")
    assert _unsafe(tmp_path), f"shape not caught: {shape}"


def test_guard_catches_a_construct_split_across_lines(tmp_path):
    """A fenced code block is the normal way these files show a command, and a
    line-based scan walks straight past it."""
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "fenced.md").write_text(
        "Arm it:\n\n```bash\n"
        f"{_RETIRED_LOOP}; do\n"
        "  dockwright monitor done\n"
        "  sleep 2\n"
        "done\n"
        "```\n")
    assert _unsafe(tmp_path), "a multi-line arming block escaped the scan"


def test_guard_accepts_the_shipped_shape_split_across_lines(tmp_path):
    """The other direction: normalizing must not make the SAFE form fail."""
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "fenced-ok.md").write_text(
        "```bash\nwhile dockwright monitor done || exit $?; do\n"
        "  sleep 2\ndone\n```\n")
    assert _unsafe(tmp_path) == []


def test_guard_scans_more_than_the_files_we_happen_to_know_about(tmp_path):
    """The enumeration must be a walk, not a hand-maintained path list."""
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (nested / "buried.sh").write_text(
        f"{_RETIRED_LOOP}; do dockwright monitor stale; sleep 60; done\n")
    assert [r for r, _, _ in _unsafe(tmp_path)] == ["a/b/c/buried.sh"]


@pytest.mark.parametrize("lane", sorted(lane_io.LANES))
def test_every_lane_is_still_armed_somewhere(lane):
    """Coverage in the other direction, derived from the canonical lane set so
    a fifth lane is checked without anyone editing this file: the rewrite must
    not have dropped a lane's arming instruction while fixing the others."""
    interval = lane_io.LANES[lane]
    expected = f"while dockwright monitor {lane} || exit $?; do sleep {interval}; done"
    carriers = [p.relative_to(REPO).as_posix() for p in _candidate_files(REPO)
                if expected in p.read_text(encoding="utf-8", errors="ignore")]
    assert carriers, f"no surface arms the {lane} lane any more"


def test_no_surface_still_carries_the_retired_literal():
    """Narrower second assertion. The property check above is the real guard;
    this one keeps the specific retired string from creeping back inside a
    line the property check would pass (a comment, a quoted example)."""
    offenders = []
    for path in _candidate_files(REPO):
        rel = path.relative_to(REPO).as_posix()
        if rel in EXEMPT:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if re.search(rf"{re.escape(_RETIRED_LOOP)}; do dockwright monitor", text):
            offenders.append(rel)
    assert offenders == [], offenders
