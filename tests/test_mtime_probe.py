"""N-1 regression guards: the boot memory-loader mtime probe must be portable.

The old idiom `stat -f %m "$f" 2>/dev/null || stat -c %Y "$f"` is poisoned on
GNU coreutils: `stat -f %m <file>` prints an fs-info block to STDOUT before
exiting 1, `$(a || b)` concatenates both stdouts, and the age arithmetic
aborts — every Linux manager boot then reports "no memory" (E2E rc.2 N-1).
The fix is the single portable probe `date -r "$f" +%s` (verified GNU+BSD,
files and dirs). These tests run the ACTUAL snippet extracted from each boot
command file under both stat personalities, so any reintroduced stat-based
probe goes red on the GNU leg.
"""
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from tests.stat_shims import write_bsd_stat_shim, write_gnu_stat_shim

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy"
COMMANDS = DEPLOY / "commands"
BOOT_FILES = [
    "manager.md",
    "manager-reboot.md",
    "manager-resume.md",
    "manager-takeover-recovery.md",
]
SNIPPET_RE = re.compile(r"bash -c '([^']+)'")


def _loader_match(filename: str) -> re.Match:
    text = (COMMANDS / filename).read_text()
    for m in SNIPPET_RE.finditer(text):
        if "manager-memory" in m.group(1):
            return m
    raise AssertionError(f"{filename}: memory-loader bash -c snippet not found")


def _run_loader(filename: str, tmp_path: Path, shim_writer) -> subprocess.CompletedProcess:
    memdir = tmp_path / "manager-memory" / "general"
    memdir.mkdir(parents=True)
    (memdir / "fresh.md").write_text("# fresh distill\n")
    old = memdir / "old.md"
    old.write_text("# stale distill\n")
    stamp = time.time() - 30 * 86400
    os.utime(old, (stamp, stamp))
    nb = tmp_path / "notebook" / "general.md"
    nb.parent.mkdir(parents=True)
    nb.write_text("## [ ] planned entry\n")

    body = _loader_match(filename).group(1)
    body = body.replace("~/.claude/dockwright/manager-memory/<domain>", str(memdir))
    body = body.replace("~/.claude/dockwright/notebook/<domain>.md", str(nb))
    assert "~" not in body, f"{filename}: unrewritten path remains in snippet: {body}"

    env = dict(os.environ)
    env["PATH"] = f"{shim_writer(tmp_path / 'shims')}:{env['PATH']}"
    return subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                          env=env, timeout=30)


@pytest.mark.parametrize("filename", BOOT_FILES)
@pytest.mark.parametrize("shim_writer", [write_gnu_stat_shim, write_bsd_stat_shim],
                         ids=["gnu-stat", "bsd-stat"])
def test_loader_survives_both_stat_personalities(filename, tmp_path, shim_writer):
    r = _run_loader(filename, tmp_path, shim_writer)
    assert r.returncode == 0, f"loader died: rc={r.returncode} stderr={r.stderr!r}"
    assert r.stderr == "", f"loader wrote stderr: {r.stderr!r}"
    lines = r.stdout.splitlines()
    assert any(line.startswith("MEMORY ") and line.endswith("/fresh.md") for line in lines), (
        f"fresh distill not loaded; stdout={r.stdout!r}")
    assert "old.md" not in r.stdout, "31-day-old distill leaked past the 7-day window"
    assert any(line.startswith("NOTEBOOK ") for line in lines), (
        f"notebook pointer missing; stdout={r.stdout!r}")


@pytest.mark.parametrize("filename", BOOT_FILES)
def test_loader_has_no_outer_stderr_suppression(filename):
    # The outer `2>/dev/null` after the closing quote hid the Linux failure for
    # a full release cycle — the loader must fail loudly.
    text = (COMMANDS / filename).read_text()
    m = _loader_match(filename)
    tail = text[m.end():m.end() + 20]
    assert not tail.strip().startswith("2>/dev/null"), (
        f"{filename}: error-hiding outer 2>/dev/null reintroduced after the loader")


def test_no_platform_split_stat_probes_in_deploy():
    # Guard the whole probe class: `stat -f` is GNU-poisoned, a bare `stat -c`
    # silently breaks macOS. Portable form: `date -r <path> +%s`.
    offenders = []
    for path in sorted(DEPLOY.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, PermissionError):
            continue
        if "stat -f" in text or "stat -c" in text:
            offenders.append(str(path.relative_to(REPO)))
    assert not offenders, (
        f"platform-split stat probes found (use `date -r <path> +%s`; see "
        f"docs/superpowers/specs/2026-07-16-rc3-linux-fixes-design.md): {offenders}")
