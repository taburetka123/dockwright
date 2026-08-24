"""Tests for deploy/scripts/corpus_watch_gate.py (eval-direction C1 tick gate).

Decision-table tests — one per check.log decision string, driven through
main() against a REAL scratch git repo at tmp_path/.claude.

Sibling-module caching (plan Global Constraints / plan-review I4): the module
under test imports gardener_gate and gardener_eval_gate (which imports
gardener_postrun) under PLAIN names, and all of them bind HOME-derived paths
at import. So every test loads a fresh copy under a unique module name with
HOME=tmp_path set BEFORE exec_module, pops the three sibling names from
sys.modules before EACH exec, and an autouse fixture restores the originals
after each test — otherwise only the first test's tmp HOME would be honored,
and a tmp-HOME-poisoned gardener_postrun would leak into
tests/test_gardener_eval_gate.py (collected later), whose postrun_of fixture
is literally sys.modules["gardener_postrun"].
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "deploy" / "scripts"
SIBLINGS = ("gardener_gate", "gardener_eval_gate", "gardener_postrun")
# Old enough that the quiet window (30 min) never fires unless a test wants it.
OLD_DATE = "2021-01-01T00:00:00 +0000"
SKILL_REL = "skills/investigate-x/SKILL.md"


@pytest.fixture(autouse=True)
def _sibling_isolation():
    saved = {name: sys.modules.get(name) for name in SIBLINGS}
    for name in SIBLINGS:
        sys.modules.pop(name, None)
    yield
    for name, original in saved.items():
        if original is not None:
            sys.modules[name] = original
        else:
            sys.modules.pop(name, None)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.delenv("DOCKWRIGHT_GARDENER_DIR", raising=False)
    monkeypatch.delenv("DOCKWRIGHT_INVESTIGATE_SKILL", raising=False)
    return tmp_path


def _load(home):
    for name in SIBLINGS:
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        "corpus_watch_gate_under_test", SCRIPTS / "corpus_watch_gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- scratch ~/.claude git repo -----------------------------------------

def _git(repo, *args, env_extra=None):
    env = {**os.environ}
    if env_extra:
        env.update(env_extra)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         *args],
        check=True, capture_output=True, env=env)


def _commit(repo, msg, fresh=False):
    extra = None if fresh else {"GIT_AUTHOR_DATE": OLD_DATE,
                                "GIT_COMMITTER_DATE": OLD_DATE}
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", msg, env_extra=extra)


def _head(repo):
    proc = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def _repo(home, files=None):
    repo = home / ".claude"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"],
                   check=True, capture_output=True)
    # Runtime state lives INSIDE the watched repo on a real install; keep the
    # scratch repo's `git add -A` from committing it.
    (repo / ".gitignore").write_text("dockwright/\nlocks/\nscripts/\n")
    for rel, content in (files or {}).items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _commit(repo, "base")
    return repo


def _mapped_change(repo):
    """An edit the eval-gate map CAN route to a suite — pair it with
    _map_investigation(mod) below, which is what makes it mapped."""
    p = repo / SKILL_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("skill body\n")
    _commit(repo, "skill edit")
    return p


def _map_investigation(mod):
    """Install the operator overlay that maps the skill surface.

    Post-rung-3 (docs/specs/eval-direction.md § Ladder execution record) the
    gate's DEFAULT_MAP claims NO instruction surface — review guards the skill
    surface — so an overlay is the ONLY way anything under skills/ becomes
    "mapped". corpus_watch_gate calls `gardener_eval_gate.load_map(None)`, the
    live overlay path, so the eval branch of this loop now arms only on
    installs whose operator mapped a suite; the default install reaches the
    drift branch instead (test_investigate_skill_edit_is_drift_not_spawn).

    Written through the loaded sibling's own `overlay_path()` so it follows
    that module's HOME/legacy-dir resolve rather than assuming a path."""
    p = Path(mod.gardener_eval_gate.overlay_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"extends_default": True, "entries": [
        {"suite": "investigation", "patterns": ["*/skills/*investigat*"]}]}))
    return p


def _create_run_script(home):
    """Create the RUN_SCRIPT the gate spawns on a mapped change — tests that
    expect a real "spawn" decision must create this first (Minor-2: the gate
    now refuses to spawn a script that isn't there)."""
    p = home / ".claude" / "scripts" / "corpus-watch-run.sh"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/bin/bash\n")
    return p


# ---- state / log / ledger helpers ---------------------------------------

def _watch_dir(home):
    return home / ".claude" / "dockwright" / "corpus-watch"


def _state_path(home):
    return _watch_dir(home) / "state.json"


def _write_state(home, last_sha, files=0, bytes_=0):
    p = _state_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(
        {"last_sha": last_sha, "drift_files": files, "drift_bytes": bytes_}))


def _read_state(home):
    return json.loads(_state_path(home).read_text())


def _check_log(home):
    p = _watch_dir(home) / "check.log"
    return p.read_text() if p.exists() else ""


def _findings(home):
    d = home / ".claude" / "dockwright" / "selffix" / "findings"
    return sorted(d.glob("corpus-drift-*.md")) if d.is_dir() else []


def _ledger_event(home, ts, lane="corpus-watch"):
    ledger = home / ".claude" / "dockwright" / "gardener" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a") as f:
        f.write(json.dumps({"type": "run_start", "lane": lane, "ts": ts}) + "\n")


class _SubprocessProxy:
    """Stands in for the module-under-test's `subprocess` global: captures
    Popen (the spawn), delegates everything else (run, DEVNULL, ...) to the
    real module — patching the real subprocess.Popen would break the gate's
    own `_git` subprocess.run calls, which use Popen internally."""

    def __init__(self, calls):
        self._calls = calls

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def Popen(self, argv, **kwargs):
        self._calls.append((argv, kwargs))

        class P:
            pid = 4242
        return P()


def _capture_popen(mod, monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "subprocess", _SubprocessProxy(calls))
    return calls


# ---- decision table ------------------------------------------------------

def test_module_off_is_silent_no_dirs_no_log(home, monkeypatch):
    cfg = home / "dockwright.toml"
    cfg.write_text("[modules]\ngardener = false\n")
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    _repo(home)
    mod = _load(home)
    calls = _capture_popen(mod, monkeypatch)
    assert mod.main([]) == 0
    assert not _watch_dir(home).exists()
    assert _check_log(home) == ""
    assert not calls


def test_stopped(home, monkeypatch):
    _repo(home)
    stop = home / ".claude" / "dockwright" / "corpus-watch-stop"
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.touch()
    mod = _load(home)
    calls = _capture_popen(mod, monkeypatch)
    assert mod.main([]) == 0
    assert "  stopped  " in _check_log(home)
    assert not _state_path(home).exists()
    assert not calls


def test_no_repo(home, monkeypatch):
    (home / ".claude").mkdir()
    mod = _load(home)
    calls = _capture_popen(mod, monkeypatch)
    assert mod.main([]) == 0
    assert "  no-repo  " in _check_log(home)
    assert not _state_path(home).exists()
    assert not calls


def test_init_records_head_no_spawn(home, monkeypatch):
    repo = _repo(home)
    mod = _load(home)
    calls = _capture_popen(mod, monkeypatch)
    assert mod.main([]) == 0
    assert _read_state(home) == {
        "last_sha": _head(repo), "drift_files": 0, "drift_bytes": 0}
    assert not calls
    assert not list(_watch_dir(home).glob("*.tmp"))
    # check.log line format: <UTC-iso>  <decision>  <sorted-json-detail>
    line = _check_log(home).strip().splitlines()[-1]
    stamp, decision, payload = line.split("  ", 2)
    assert decision == "init"
    assert stamp.endswith("Z")
    assert isinstance(json.loads(payload), dict)


def test_bad_sha_is_loud_reinit_and_finding(home, monkeypatch, capsys):
    repo = _repo(home)
    head = _head(repo)
    _write_state(home, "not-a-sha", files=2, bytes_=999)
    mod = _load(home)
    calls = _capture_popen(mod, monkeypatch)
    assert mod.main([]) == 0
    assert "not-a-sha" in capsys.readouterr().err
    assert "  bad-sha  " in _check_log(home)
    assert _read_state(home) == {
        "last_sha": head, "drift_files": 0, "drift_bytes": 0}
    findings = _findings(home)
    assert len(findings) == 1
    assert f"not-a-sha..{head}" in findings[0].read_text()
    assert not calls


def test_garbage_json_state_is_loud_reinit(home, monkeypatch, capsys):
    """Important-1: state.json holding non-JSON content must route into the
    SAME loud bad-sha path as an unresolvable last_sha — never the silent
    'init' branch that would discard the watch anchor without a trace."""
    repo = _repo(home)
    head = _head(repo)
    state_path = _state_path(home)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("not json")
    mod = _load(home)
    calls = _capture_popen(mod, monkeypatch)
    assert mod.main([]) == 0
    log = _check_log(home)
    assert "  bad-sha  " in log
    assert "  init  " not in log
    assert capsys.readouterr().err.strip() != ""
    assert _read_state(home) == {
        "last_sha": head, "drift_files": 0, "drift_bytes": 0}
    assert len(_findings(home)) == 1
    assert not calls


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores file-permission bits — chmod 000 stays readable")
def test_unreadable_state_file_is_loud_reinit_not_plain_init(
        home, monkeypatch, capsys):
    """Important-1: an OSError distinct from FileNotFoundError (e.g. a
    permission-denied state.json) must NOT read as "missing" — that silently
    re-inits with a success-shaped 'init' line and drops the unexamined
    range + accumulated drift counters with no trace. It must take the same
    loud bad-sha path as an unresolvable last_sha."""
    repo = _repo(home)
    head = _head(repo)
    state_path = _state_path(home)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(
        {"last_sha": "deadbeef", "drift_files": 2, "drift_bytes": 999}))
    state_path.chmod(0o000)
    if os.access(state_path, os.R_OK):
        pytest.skip("current user can still read a chmod 000 file")
    try:
        mod = _load(home)
        calls = _capture_popen(mod, monkeypatch)
        assert mod.main([]) == 0
    finally:
        state_path.chmod(0o644)
    log = _check_log(home)
    assert "  bad-sha  " in log
    assert "  init  " not in log
    assert capsys.readouterr().err.strip() != ""
    assert _read_state(home) == {
        "last_sha": head, "drift_files": 0, "drift_bytes": 0}
    assert len(_findings(home)) == 1
    assert not calls


def test_no_new(home, monkeypatch):
    repo = _repo(home)
    head = _head(repo)
    _write_state(home, head, files=1, bytes_=10)
    mod = _load(home)
    calls = _capture_popen(mod, monkeypatch)
    assert mod.main([]) == 0
    assert "  no-new  " in _check_log(home)
    assert _read_state(home) == {
        "last_sha": head, "drift_files": 1, "drift_bytes": 10}
    assert not calls


def test_quiet_fresh_commit_not_examined(home, monkeypatch):
    repo = _repo(home)
    first = _head(repo)
    _write_state(home, first)
    (repo / "rules").mkdir()
    (repo / "rules" / "foo.md").write_text("x\n")
    _commit(repo, "fresh rule edit", fresh=True)
    mod = _load(home)
    calls = _capture_popen(mod, monkeypatch)
    assert mod.main([]) == 0
    assert "  quiet  " in _check_log(home)
    assert _read_state(home)["last_sha"] == first
    assert not calls


def test_locked_skips_with_no_state_write(home, monkeypatch):
    repo = _repo(home)
    first = _head(repo)
    _write_state(home, first)
    _mapped_change(repo)
    lock = home / ".claude" / "locks" / "analyst-run.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(os.getpid()))
    mod = _load(home)
    _map_investigation(mod)   # keep the change genuinely spawn-worthy, so the
    # lock is provably what stopped it (an unmapped change would reach the
    # drift branch instead — a weaker property).
    calls = _capture_popen(mod, monkeypatch)
    before = _state_path(home).read_text()
    assert mod.main([]) == 0
    assert "  locked  " in _check_log(home)
    assert _state_path(home).read_text() == before
    assert not calls


def test_cooldown_skips_with_no_state_write(home, monkeypatch):
    repo = _repo(home)
    first = _head(repo)
    _write_state(home, first)
    _mapped_change(repo)
    _ledger_event(home, time.time() - 120)
    mod = _load(home)
    _map_investigation(mod)   # as above: the cooldown, not an unmapped path,
    # must be what prevents the spawn.
    calls = _capture_popen(mod, monkeypatch)
    before = _state_path(home).read_text()
    assert mod.main([]) == 0
    assert "  cooldown  " in _check_log(home)
    assert _state_path(home).read_text() == before
    assert not calls
    assert not _findings(home)


def test_spawn_argv_state_untouched(home, monkeypatch):
    repo = _repo(home)
    first = _head(repo)
    _write_state(home, first, files=1, bytes_=7)
    target = _mapped_change(repo)
    _create_run_script(home)
    head = _head(repo)
    # stale corpus-watch run_start (cooldown expired) + a FRESH digest-lane
    # event that must NOT arm the corpus-watch cooldown (lane isolation).
    _ledger_event(home, time.time() - 7 * 3600)
    _ledger_event(home, time.time() - 60, lane="digest")
    mod = _load(home)
    _map_investigation(mod)
    calls = _capture_popen(mod, monkeypatch)
    before = _state_path(home).read_text()
    assert mod.main([]) == 0
    assert "  spawn  " in _check_log(home)
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == [
        "bash",
        str(home / ".claude" / "scripts" / "corpus-watch-run.sh"),
        head,
        f"{first}..{head}",
        str(target),
        # Minor-4: bound to the loaded gardener_gate's OWN legacy-home
        # resolve (_prefer_new), not a hardcoded new-home path — see
        # test_spawn_argv_ledger_path_binds_legacy_resolve for the
        # legacy-dir-present case this distinction actually matters for.
        str(mod.gardener_gate.LEDGER_PATH.parent),
    ]
    assert kwargs.get("start_new_session") is True
    # run.sh owns the advance to the examined sha; the gate must not advance.
    assert _state_path(home).read_text() == before


def test_spawn_argv_ledger_path_binds_legacy_resolve(home, monkeypatch):
    """The 4th spawn arg must track gardener_gate's M6 legacy-home resolve
    (_prefer_new: legacy dir wins if it exists), not assume the new-home
    path. Plant a legacy ~/.claude/gardener dir and confirm the argv follows
    it — a hardcoded new-home-path assertion would fail this."""
    repo = _repo(home)
    first = _head(repo)
    _write_state(home, first)
    _mapped_change(repo)
    _create_run_script(home)
    legacy_gardener = home / ".claude" / "gardener"
    legacy_gardener.mkdir(parents=True)
    mod = _load(home)
    # overlay_path() follows the same legacy resolve, so this lands in the
    # legacy dir here — the reason _map_investigation asks the module.
    _map_investigation(mod)
    calls = _capture_popen(mod, monkeypatch)
    assert mod.main([]) == 0
    assert len(calls) == 1
    argv, _kwargs = calls[0]
    assert argv[5] == str(mod.gardener_gate.LEDGER_PATH.parent)
    assert argv[5] == str(legacy_gardener)


def test_deleted_mapped_file_still_spawns(home, monkeypatch):
    repo = _repo(home, {SKILL_REL: "skill body\n"})
    first = _head(repo)
    _write_state(home, first)
    _git(repo, "rm", "-q", SKILL_REL)
    _commit(repo, "delete skill")
    _create_run_script(home)
    head = _head(repo)
    mod = _load(home)
    _map_investigation(mod)
    calls = _capture_popen(mod, monkeypatch)
    assert mod.main([]) == 0
    assert "  spawn  " in _check_log(home)
    assert len(calls) == 1
    argv, _kwargs = calls[0]
    assert argv[2] == head
    assert str(home / ".claude" / SKILL_REL) in argv[4].split(",")


def test_spawn_blocked_when_run_script_missing(home, monkeypatch, capsys):
    """Minor-2: a mapped change with no corpus-watch-run.sh on disk must NOT
    log a success-shaped 'spawn' or call Popen on a script that isn't
    there — it logs 'spawn-blocked' and leaves state untouched."""
    repo = _repo(home)
    first = _head(repo)
    _write_state(home, first)
    _mapped_change(repo)
    run_script = home / ".claude" / "scripts" / "corpus-watch-run.sh"
    assert not run_script.exists()
    mod = _load(home)
    _map_investigation(mod)
    calls = _capture_popen(mod, monkeypatch)
    before = _state_path(home).read_text()
    assert mod.main([]) == 0
    log = _check_log(home)
    assert "  spawn-blocked  " in log
    assert "  spawn  " not in log
    assert "run_script_missing" in log
    assert str(run_script) in capsys.readouterr().err
    assert not calls
    assert _state_path(home).read_text() == before


def test_mixed_range_spawns_and_writes_drift_finding_at_threshold(
        home, monkeypatch):
    """Tier-2 F4: a MIXED range (mapped skill edit + unmapped rules churn)
    used to return at the spawn branch before the drift accounting ever ran —
    corpus-watch-run.sh then advanced last_sha past the whole range and the
    rules churn was never counted, never surfaced, never revisited. The
    accounting must run for EVERY classified range: decision stays `spawn`
    (mapped subset only in the argv), AND the drift counters update — here 3
    unmapped rule files, at threshold, so the drift finding is written and
    the counters reset. last_sha is NOT advanced (run.sh owns it); drift_sha
    records the examined head so the accounting is at-most-once per head.

    RED-proof: against the pre-fix code the spawn branch returns first — no
    finding, no state write. Verified; output pasted in the task report."""
    repo = _repo(home, {"rules/a.md": "a" * 400 + "\n",
                        "rules/b.md": "b" * 400 + "\n",
                        "rules/c.md": "c" * 400 + "\n"})
    first = _head(repo)
    _write_state(home, first)
    for name in ("a", "b", "c"):
        (repo / "rules" / f"{name}.md").write_text("x\n")  # gut the rules
    _mapped_change(repo)  # git add -A commits the gut + the skill edit
    _create_run_script(home)
    head = _head(repo)
    mod = _load(home)
    _map_investigation(mod)
    calls = _capture_popen(mod, monkeypatch)
    assert mod.main([]) == 0
    log = _check_log(home)
    assert "  spawn  " in log
    assert len(calls) == 1
    argv, _kwargs = calls[0]
    assert argv[4] == str(home / ".claude" / SKILL_REL)  # mapped subset only
    findings = _findings(home)
    assert len(findings) == 1
    text = findings[0].read_text()
    assert f"{first}..{head}" in text
    assert "rules/a.md" in text and "rules/b.md" in text and "rules/c.md" in text
    assert _read_state(home) == {
        "last_sha": first,  # spawn does NOT advance — run.sh owns it
        "drift_files": 0, "drift_bytes": 0,  # reset at threshold
        "drift_sha": head}


def test_mixed_range_below_threshold_rerun_not_double_counted(
        home, monkeypatch):
    """Tier-2 F4 double-count guard: the spawn branch does not advance
    last_sha, so a re-examined range (run.sh died, lock skip, cooldown
    retry) re-classifies the same last..head. drift_sha must make the churn
    accounting at-most-once per head — the second tick spawns again but the
    counters stay exactly where the first tick left them."""
    repo = _repo(home, {"rules/foo.md": "a\n"})
    first = _head(repo)
    _write_state(home, first)
    (repo / "rules" / "foo.md").write_text("ab\n")  # 2 -> 3 bytes: delta 1
    _mapped_change(repo)  # same commit range gains the mapped skill edit
    _create_run_script(home)
    head = _head(repo)
    mod = _load(home)
    _map_investigation(mod)
    calls = _capture_popen(mod, monkeypatch)
    assert mod.main([]) == 0
    assert len(calls) == 1
    expected = {"last_sha": first, "drift_files": 1, "drift_bytes": 1,
                "drift_sha": head}
    assert _read_state(home) == expected
    assert not _findings(home)
    # spawn not consumed: run.sh never ran, so the same tick state is
    # re-examined — must spawn again WITHOUT re-accumulating the same churn.
    assert mod.main([]) == 0
    assert len(calls) == 2
    assert _read_state(home) == expected
    assert not _findings(home)


def test_no_instruction_churn_advances_counters_untouched(home, monkeypatch):
    repo = _repo(home)
    first = _head(repo)
    _write_state(home, first, files=1, bytes_=100)
    (repo / "settings.json").write_text("{}\n")
    _commit(repo, "settings change")
    head = _head(repo)
    mod = _load(home)
    calls = _capture_popen(mod, monkeypatch)
    assert mod.main([]) == 0
    assert "  no-instruction-churn  " in _check_log(home)
    assert "  drift  " not in _check_log(home)
    assert _read_state(home) == {
        "last_sha": head, "drift_files": 1, "drift_bytes": 100}
    assert not calls
    assert not _findings(home)


def test_drift_below_threshold_accumulates_no_finding(home, monkeypatch):
    repo = _repo(home, {"rules/foo.md": "a\n"})
    first = _head(repo)
    _write_state(home, first)
    (repo / "rules" / "foo.md").write_text("ab\n")  # 2 -> 3 bytes: delta 1
    _commit(repo, "small rule edit")
    head = _head(repo)
    mod = _load(home)
    calls = _capture_popen(mod, monkeypatch)
    assert mod.main([]) == 0
    assert "  drift  " in _check_log(home)
    assert _read_state(home) == {
        "last_sha": head, "drift_files": 1, "drift_bytes": 1}
    assert not _findings(home)
    assert not calls


def test_drift_threshold_files_writes_finding_and_resets(home, monkeypatch):
    repo = _repo(home)
    first = _head(repo)
    _write_state(home, first)
    (repo / "rules").mkdir()
    for name in ("a", "b", "c"):
        (repo / "rules" / f"{name}.md").write_text(f"{name} body\n")
    _commit(repo, "three rules at once")
    head = _head(repo)
    mod = _load(home)
    calls = _capture_popen(mod, monkeypatch)
    assert mod.main([]) == 0
    assert "  drift  " in _check_log(home)
    assert _read_state(home) == {
        "last_sha": head, "drift_files": 0, "drift_bytes": 0}
    findings = _findings(home)
    assert len(findings) == 1
    text = findings[0].read_text()
    assert f"{first}..{head}" in text
    assert "rules/a.md" in text and "rules/b.md" in text and "rules/c.md" in text
    assert "0 of 3 behaviorally covered" in text
    assert "docs/specs/eval-direction.md" in text
    assert not calls


def test_drift_bytes_accumulate_across_ticks_to_finding(home, monkeypatch):
    repo = _repo(home, {"rules/foo.md": "a\n"})
    first = _head(repo)
    # 2040 accumulated bytes from prior ticks; this tick adds 119 -> 2159 >= 2048
    _write_state(home, first, files=1, bytes_=2040)
    (repo / "rules" / "foo.md").write_text("a" * 120 + "\n")
    _commit(repo, "rule edit")
    head = _head(repo)
    mod = _load(home)
    calls = _capture_popen(mod, monkeypatch)
    assert mod.main([]) == 0
    assert "  drift  " in _check_log(home)
    assert _read_state(home) == {
        "last_sha": head, "drift_files": 0, "drift_bytes": 0}
    assert len(_findings(home)) == 1
    assert not calls


def test_investigate_skill_edit_is_drift_not_spawn_by_default(home, monkeypatch):
    """The rung-3 corpus-watch contract (docs/specs/eval-direction.md § Ladder
    execution record): on a DEFAULT install nothing under the instruction
    corpus is gate-mapped — review guards the skill surface — so even an
    investigate-named skill edit takes the DRIFT branch (accumulate, no model
    spend), never the spawn branch. "In the spec's rung-3 world, 0 eval runs
    per day — drift findings only."

    Delete-one-line sweep in one test: the SAME commit range flips to "spawn"
    the moment an operator overlay maps the suite, so this cannot pass for the
    wrong reason (e.g. a missing run script, a stale quiet window, or the path
    simply never reaching classification). RUN_SCRIPT is planted first so a
    spawn is genuinely available.

    RED proof: re-add {"suite": "investigation", "patterns":
    ["*/skills/*investigat*"]} to gardener_eval_gate.DEFAULT_MAP in a scratch
    copy -> the first half sees "spawn" and FAILS. Verified; output pasted in
    the task report."""
    repo = _repo(home)
    first = _head(repo)
    _write_state(home, first)
    _mapped_change(repo)
    _create_run_script(home)
    mod = _load(home)

    decision, _detail, effects = mod.decide(time.time())
    assert decision == "drift"
    assert "spawn" not in effects
    assert effects["state"]["drift_files"] == 1

    _map_investigation(mod)
    decision_mapped, _detail2, effects2 = mod.decide(time.time())
    assert decision_mapped == "spawn"
    assert str(home / ".claude" / SKILL_REL) in effects2["spawn"][4].split(",")


@pytest.mark.parametrize("dirname,rel", [
    ("rules/", "rules/some-rule.md"),
    # An ordinary skill path: unmapped instruction churn (the drift branch)
    # on any install. Post-rung-3 an investigate-named skill is unmapped by
    # default too (test_investigate_skill_edit_is_drift_not_spawn_by_default);
    # this case is about INSTRUCTION_DIRS routing, not about the map.
    ("skills/", "skills/other-skill/SKILL.md"),
    ("commands/", "commands/some-command.md"),
    ("agents/", "agents/some-agent.md"),
])
def test_instruction_dirs_bind_all_four(home, monkeypatch, dirname, rel):
    """Important-2: each of the four INSTRUCTION_DIRS entries must actually
    route an unmapped edit under it into the drift branch. The sweep proves
    the guard: dropping just THIS entry from INSTRUCTION_DIRS must break the
    branch back to no-instruction-churn — a passing param case alone
    wouldn't prove the tuple entry is load-bearing."""
    repo = _repo(home)
    first = _head(repo)
    _write_state(home, first)
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x\n")
    _commit(repo, f"edit {rel}")
    mod = _load(home)

    decision, _detail, effects = mod.decide(time.time())
    assert decision == "drift"
    assert effects["state"]["drift_files"] == 1

    stripped = tuple(d for d in mod.INSTRUCTION_DIRS if d != dirname)
    monkeypatch.setattr(mod, "INSTRUCTION_DIRS", stripped)
    decision_stripped, _detail2, _effects2 = mod.decide(time.time())
    assert decision_stripped == "no-instruction-churn"


def test_non_ascii_path_classified_as_instruction_drift(home, monkeypatch):
    """Minor-1: git quotes non-ASCII paths in --name-only output by default
    (core.quotePath=true), which would make the raw line start with a
    literal `"` instead of `rules/` and misclassify as
    no-instruction-churn. -c core.quotePath=false must be in effect."""
    repo = _repo(home)
    first = _head(repo)
    _write_state(home, first)
    p = repo / "rules" / "тест.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x\n", encoding="utf-8")
    _commit(repo, "non-ascii rule edit")
    head = _head(repo)
    mod = _load(home)
    calls = _capture_popen(mod, monkeypatch)
    assert mod.main([]) == 0
    log = _check_log(home)
    assert "  drift  " in log
    assert "  no-instruction-churn  " not in log
    assert _read_state(home) == {
        "last_sha": head, "drift_files": 1, "drift_bytes": 2}
    assert not calls


def test_constants_pinned(home):
    """Minor-4: pin the tuning constants so a drive-by edit is a deliberate,
    reviewed change, not a silent behavior shift."""
    mod = _load(home)
    assert mod.QUIET_SEC == 1800
    assert mod.COOLDOWN_SEC == 21600
    assert mod.DRIFT_FILES_THRESHOLD == 3
    assert mod.DRIFT_BYTES_THRESHOLD == 2048


def test_dry_run_prints_decision_writes_nothing(home, monkeypatch, capsys):
    repo = _repo(home)
    first = _head(repo)
    _write_state(home, first)
    _mapped_change(repo)
    _create_run_script(home)
    mod = _load(home)
    _map_investigation(mod)
    calls = _capture_popen(mod, monkeypatch)
    before = _state_path(home).read_text()
    assert mod.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "corpus-watch-gate: spawn " in out
    assert _check_log(home) == ""
    assert _state_path(home).read_text() == before
    assert not calls
    assert not _findings(home)
