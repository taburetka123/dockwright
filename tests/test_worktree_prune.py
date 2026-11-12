import importlib.util
import json
import os
import shlex
import shutil
import sys
import time as _time
from types import SimpleNamespace as _SimpleNamespace
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "deploy" / "scripts" / "worktree_prune.py"

NOW = 1_700_000_000.0
LIVE_PID = 4111
DEAD_PID = 4222


def _load():
    spec = importlib.util.spec_from_file_location("worktree_prune_under_test", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ANCIENT = 0.0


def _configure(mod, tmp_path, monkeypatch):
    wtdir = tmp_path / "worktree-prune"
    monkeypatch.setattr(mod, "WT_DIR", wtdir)
    monkeypatch.setattr(mod, "LEDGER_PATH", wtdir / "ledger.jsonl")
    monkeypatch.setattr(mod, "CHECK_LOG_PATH", wtdir / "check.log")
    monkeypatch.setattr(mod, "STOP_PATHS", (tmp_path / "worktree-prune-stop", tmp_path / "legacy-worktree-prune-stop"))
    monkeypatch.setattr(mod, "ORCH_ACTIVE", tmp_path / "active")
    wtdir.mkdir(parents=True, exist_ok=True)
    keep = wtdir / "keep.txt"
    keep.write_text("# holds, one path or glob per line\n")
    monkeypatch.setattr(mod, "KEEPLIST_PATH", keep)
    monkeypatch.setattr(mod, "_load_open_works", lambda run: [])
    return mod


@pytest.fixture
def wp_clock(tmp_path, monkeypatch):
    """The module with its REAL age clock, for tests that exercise the age gate."""
    for var in [k for k in os.environ if k.startswith("WORKTREE_PRUNE_")]:
        monkeypatch.delenv(var, raising=False)
    return _configure(_load(), tmp_path, monkeypatch)


@pytest.fixture
def wp(tmp_path, monkeypatch, wp_clock):
    """Age gate held open, so a test about some OTHER gate does not have to own a
    worktree admin dir on disk. Tests of the age gate itself take `wp_clock`."""
    monkeypatch.setattr(wp_clock, "worktree_born_at", lambda run, cand: ANCIENT)
    return wp_clock


@pytest.fixture
def wp_works(tmp_path, monkeypatch):
    for var in [k for k in os.environ if k.startswith("WORKTREE_PRUNE_")]:
        monkeypatch.delenv(var, raising=False)
    mod = _load()
    real = mod._load_open_works
    _configure(mod, tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "_load_open_works", real)
    monkeypatch.setattr(mod, "worktree_born_at", lambda run, cand: ANCIENT)
    return mod


def _ledger_events(wp):
    if not wp.LEDGER_PATH.is_file():
        return []
    return [json.loads(line) for line in wp.LEDGER_PATH.read_text().splitlines() if line.strip()]


def _RR(wp, rc=0, out="", err=""):
    return wp.RunResult(rc, out, err)


def _infra_rr(wp, args):
    if "for-each-ref" in args:
        if wp.PROOF_MAIN_REF in args:
            return _RR(wp, 0, wp.PROOF_MAIN_REF + "\n")
        return _RR(wp, 0, "")
    if "--git-path" in args:
        return _RR(wp, 0, "".join(f".git/{m}\n" for m in wp.IN_PROGRESS_MARKERS))
    if "ls-remote" in args:
        return _RR(wp, 0, "")
    if "submodule" in args:
        return _RR(wp, 0, "")
    return None


def _gh_head_branch(args):
    if "--head" in args:
        return args[args.index("--head") + 1]
    return args[3]


def _gh_body(args, state, head, number=1, base="main"):
    obj = {"number": number, "state": state, "headRefOid": head}
    if base is not None:
        obj["baseRefName"] = base
    return json.dumps([obj] if "list" in args else obj)


class TestStopFile:
    def test_stop_file_short_circuits_and_runs_nothing(self, wp):
        wp.STOP_PATHS[0].touch()
        calls = []

        def fake_run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            calls.append(args)
            raise AssertionError("run must not be called once stopped")

        decision, info = wp.run_prune(NOW, apply=False, run=fake_run)
        assert decision == "stopped"
        assert calls == []
        assert not wp.LEDGER_PATH.exists()

    def test_legacy_stop_file_short_circuits(self, wp):
        wp.STOP_PATHS[1].touch()

        def fake_run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            raise AssertionError("run must not be called once stopped")

        decision, _ = wp.run_prune(NOW, apply=False, run=fake_run)
        assert decision == "stopped"

    def test_stop_file_honored_even_with_apply(self, wp):
        wp.STOP_PATHS[0].touch()

        def fake_run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            raise AssertionError("run must not be called once stopped")

        decision, _ = wp.run_prune(NOW, apply=True, run=fake_run)
        assert decision == "stopped"
        assert _ledger_events(wp) == []


def _mk_clone(parent: Path, name: str) -> Path:
    clone = parent / name
    (clone / ".git").mkdir(parents=True)
    return clone


def _porcelain(*records: str) -> str:
    return "\n".join(records) + "\n"


class TestEnumerate:
    def test_both_layouts_with_main_and_offroot_excluded(self, wp, tmp_path):
        work = tmp_path / "projects" / "work"
        personal = tmp_path / "projects" / "personal"
        clone_a = _mk_clone(work, "repo-a")
        clone_b = _mk_clone(personal, "repo-b")
        (personal / "loose-dir").mkdir()

        roots = [str(tmp_path / "worktrees"), str(tmp_path / "worktrees-personal")]
        nested_wt = tmp_path / "worktrees-personal" / "task-1" / "repo-a"
        flat_wt = tmp_path / "worktrees" / "task-2"
        detached_wt = tmp_path / "worktrees" / "task-3"
        offroot_wt = tmp_path / "elsewhere" / "task-x"

        porc_a = _porcelain(
            f"worktree {clone_a}", "HEAD a0", "branch refs/heads/main", "",
            f"worktree {nested_wt}", "HEAD a1", "branch refs/heads/feat-nested", "",
        )
        porc_b = _porcelain(
            f"worktree {clone_b}", "HEAD b0", "branch refs/heads/main", "",
            f"worktree {flat_wt}", "HEAD b1", "branch refs/heads/feat-flat", "",
            f"worktree {detached_wt}", "HEAD b2", "detached", "",
            f"worktree {offroot_wt}", "HEAD b3", "branch refs/heads/feat-off", "",
        )
        by_clone = {str(clone_a): porc_a, str(clone_b): porc_b}

        seen_clones = []

        def fake_run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            assert args[:2] == ["git", "-C"]
            assert args[3:] == ["worktree", "list", "--porcelain"]
            seen_clones.append(args[2])
            return _RR(wp, 0, by_clone[args[2]])

        cands = wp.enumerate_candidates(fake_run, [str(work), str(personal)], roots)
        by_path = {c.path: c for c in cands}

        assert set(by_path) == {str(nested_wt), str(flat_wt), str(detached_wt)}
        assert str(clone_a) not in by_path
        assert str(clone_b) not in by_path
        assert str(offroot_wt) not in by_path
        assert sorted(seen_clones) == sorted([str(clone_a), str(clone_b)])

        assert by_path[str(nested_wt)].branch == "feat-nested"
        assert by_path[str(nested_wt)].detached is False
        assert by_path[str(nested_wt)].clone == str(clone_a)
        assert by_path[str(nested_wt)].head == "a1"

        assert by_path[str(detached_wt)].detached is True
        assert by_path[str(detached_wt)].branch is None
        assert by_path[str(flat_wt)].clone == str(clone_b)

    def test_worktree_list_failure_yields_no_candidates_for_that_clone(self, wp, tmp_path):
        clone = _mk_clone(tmp_path / "projects" / "personal", "repo")
        roots = [str(tmp_path / "worktrees")]

        def fake_run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            return _RR(wp, 128, "", "fatal: not a git repository")

        cands = wp.enumerate_candidates(fake_run, [str(tmp_path / "projects" / "personal")], roots)
        assert cands == []


def _cand(wp, head="h1", branch="feat", detached=False, path="/wt", clone="/clone"):
    return wp.Candidate(path=path, head=head, branch=branch, detached=detached, clone=clone)


class TestGateMerged:
    def test_merged_with_matching_head(self, wp):
        cand = _cand(wp, head="abc")

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if args[0] == "gh":
                return _RR(wp, 0, json.dumps({"state": "MERGED", "headRefOid": "abc"}))
            return _RR(wp, 1)

        assert wp.gate_merged(run, cand) is True

    def test_gh_invoked_with_worktree_cwd(self, wp):
        cand = _cand(wp, head="abc", path="/wt-x")
        seen = {}

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if args[0] == "gh":
                seen["cwd"] = cwd
                return _RR(wp, 0, json.dumps({"state": "MERGED", "headRefOid": "abc"}))
            return _RR(wp, 1)

        assert wp.gate_merged(run, cand) is True
        assert seen["cwd"] == "/wt-x"

    def test_merged_but_head_mismatch_is_not_merged(self, wp):
        cand = _cand(wp, head="abc")

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if args[0] == "gh":
                return _RR(wp, 0, json.dumps({"state": "MERGED", "headRefOid": "DEADBEEF"}))
            return _RR(wp, 1)

        assert wp.gate_merged(run, cand) is False

    def test_open_pr_and_not_ancestor_is_not_merged(self, wp):
        cand = _cand(wp)

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if args[0] == "gh":
                return _RR(wp, 0, json.dumps({"state": "OPEN", "headRefOid": "h1"}))
            return _RR(wp, 1)

        assert wp.gate_merged(run, cand) is False

    def test_no_pr_and_not_ancestor_is_not_merged(self, wp):
        cand = _cand(wp)

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if args[0] == "gh":
                return _RR(wp, 1, "", "no pull requests found")
            return _RR(wp, 1)

        assert wp.gate_merged(run, cand) is False

    def test_no_pr_but_ancestor_is_merged(self, wp):
        cand = _cand(wp)

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if args[0] == "gh":
                return _RR(wp, 1)
            if "merge-base" in args:
                return _RR(wp, 0)
            return _RR(wp, 1)

        assert wp.gate_merged(run, cand) is True

    def test_detached_uses_ancestor_only_no_gh(self, wp):
        cand = _cand(wp, branch=None, detached=True)
        seen = []

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            seen.append(args[0])
            if "merge-base" in args:
                return _RR(wp, 0)
            return _RR(wp, 1)

        assert wp.gate_merged(run, cand) is True
        assert "gh" not in seen

    def test_gh_exception_falls_through_to_ancestor(self, wp):
        cand = _cand(wp)

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if args[0] == "gh":
                raise TimeoutError("gh timed out")
            if "merge-base" in args:
                return _RR(wp, 0)
            return _RR(wp, 1)

        assert wp.gate_merged(run, cand) is True

    def test_gh_garbage_json_falls_through_to_ancestor(self, wp):
        cand = _cand(wp)

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if args[0] == "gh":
                return _RR(wp, 0, "not json at all")
            if "merge-base" in args:
                return _RR(wp, 1)
            return _RR(wp, 1)

        assert wp.gate_merged(run, cand) is False


class TestGateClean:
    def _run_status(self, wp, out, rc=0):
        cand = _cand(wp, path="/wt")

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            assert args == ["git", "-C", "/wt", "status", "--porcelain", "--ignored"]
            return _RR(wp, rc, out)

        return wp.gate_clean(run, cand)

    def test_empty_is_clean(self, wp):
        assert self._run_status(wp, "") is True

    def test_injected_untracked_only_is_clean(self, wp):
        out = "?? .claude/\n?? CLAUDE.md\n?? .mcp.json\n"
        assert self._run_status(wp, out) is True

    def test_injected_untracked_nested_path_is_clean(self, wp):
        out = "?? .claude/settings.local.json\n"
        assert self._run_status(wp, out) is True

    def test_tracked_modified_claude_md_is_dirty(self, wp):
        assert self._run_status(wp, " M CLAUDE.md\n") is False

    def test_untracked_non_injected_is_dirty(self, wp):
        assert self._run_status(wp, "?? somefile.py\n") is False

    def test_tracked_modified_source_is_dirty(self, wp):
        assert self._run_status(wp, " M src/x.py\n") is False

    def test_mixed_injected_plus_real_change_is_dirty(self, wp):
        assert self._run_status(wp, "?? .claude/\n M src/x.py\n") is False

    def test_status_command_error_is_dirty(self, wp):
        assert self._run_status(wp, "", rc=128) is False

    def test_run_exception_is_dirty(self, wp):
        cand = _cand(wp, path="/wt")

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            raise OSError("boom")

        assert wp.gate_clean(run, cand) is False


def _live(p):
    return p == LIVE_PID


class TestGateUnowned:
    def test_active_cwd_equals_path_live_pid_is_owned(self, wp):
        cand = _cand(wp, path="/wt")
        recs = [{"cwd": "/wt", "pid": LIVE_PID}]
        assert wp.gate_unowned(cand, recs, [], "/elsewhere", pid_alive=_live) is False

    def test_active_cwd_under_path_live_pid_is_owned(self, wp):
        cand = _cand(wp, path="/wt")
        recs = [{"cwd": "/wt/sub", "pid": LIVE_PID}]
        assert wp.gate_unowned(cand, recs, [], "/elsewhere", pid_alive=_live) is False

    def test_active_cwd_under_path_dead_pid_is_not_owned(self, wp):
        cand = _cand(wp, path="/wt")
        recs = [{"cwd": "/wt/sub", "pid": DEAD_PID}]
        assert wp.gate_unowned(cand, recs, [], "/elsewhere", pid_alive=_live) is True

    def test_active_cwd_for_other_worktree_is_eligible(self, wp):
        cand = _cand(wp, path="/wt")
        recs = [{"cwd": "/other-wt", "pid": LIVE_PID}]
        assert wp.gate_unowned(cand, recs, [], "/elsewhere", pid_alive=_live) is True

    def test_lsof_cwd_under_path_is_owned(self, wp):
        cand = _cand(wp, path="/wt")
        assert wp.gate_unowned(cand, [], ["/wt/sub"], "/elsewhere", pid_alive=_live) is False

    def test_self_path_inside_is_owned(self, wp):
        cand = _cand(wp, path="/wt")
        assert wp.gate_unowned(cand, [], [], "/wt", pid_alive=_live) is False

    def test_readable_empty_signals_are_eligible(self, wp):
        cand = _cand(wp, path="/wt")
        assert wp.gate_unowned(cand, [], [], "/elsewhere", pid_alive=_live) is True

    def test_both_signals_unavailable_is_owned(self, wp):
        cand = _cand(wp, path="/wt")
        assert wp.gate_unowned(cand, None, None, "/elsewhere", pid_alive=_live) is False

    def test_active_unavailable_is_owned_even_when_lsof_reads_clean(self, wp):
        cand = _cand(wp, path="/wt")
        assert wp.gate_unowned(cand, None, [], "/elsewhere", pid_alive=_live) is False

    def test_lsof_unavailable_is_owned_even_when_records_read_clean(self, wp):
        cand = _cand(wp, path="/wt")
        recs = [{"cwd": "/somewhere/else", "pid": 1}]
        assert wp.gate_unowned(cand, recs, None, "/elsewhere", pid_alive=_live) is False

    def test_unresolvable_pin_falls_back_to_path_lookup(self, wp, monkeypatch):
        monkeypatch.setattr(wp, "LSOF_BIN", "/nonexistent/bogus-lsof")
        monkeypatch.setattr(wp.shutil, "which", lambda name: "/usr/bin/lsof")
        tried = []

        def run(args, cwd=None):
            tried.append(args[0])
            if args[0] == "/nonexistent/bogus-lsof":
                raise FileNotFoundError(args[0])
            return _RR(wp, 0, "p100\nn/path/a\n")

        assert wp._collect_lsof_cwds(run) == ["/path/a"]
        assert tried == ["/nonexistent/bogus-lsof", "/usr/bin/lsof"]

    def test_lsof_binary_missing_under_launchd_path_leaves_tree_owned(self, wp):
        cand = _cand(wp, path="/wt")

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            raise FileNotFoundError(args[0])

        lsof_cwds = wp._collect_lsof_cwds(run)
        assert lsof_cwds is None
        recs = [{"cwd": "/somewhere/else", "pid": 1}]
        assert wp.gate_unowned(cand, recs, lsof_cwds, "/elsewhere",
                               pid_alive=_live) is False

    def test_default_pid_alive_uses_current_process(self, wp):
        import os as _os
        cand = _cand(wp, path="/wt")
        recs = [{"cwd": "/wt", "pid": _os.getpid()}]
        assert wp.gate_unowned(cand, recs, [], "/elsewhere") is False


def _ledger_by_event(wp):
    events = {}
    for e in _ledger_events(wp):
        events.setdefault(e["event"], []).append(e)
    return events


ALL_PASS = {"kept": False, "not_locked": True, "no_open_work": (True, None),
            "old_enough": (True, None),
            "in_progress_clear": True,
            "contained": (True, None), "merged": (True, None), "clean": True,
            "ignored_ok": (True, None), "unowned": True}


class TestDecide:
    def test_all_gates_pass_is_eligible(self, wp):
        assert wp.decide(_cand(wp), dict(ALL_PASS)) == ("WOULD-REMOVE", None)

    @pytest.mark.parametrize("key,expected", [
        ("kept", "kept"),
        ("not_locked", "locked"),
        ("no_open_work", "open_work"),
        ("old_enough", "too_young"),
        ("in_progress_clear", "in_progress"),
        ("merged", "unmerged"),
        ("clean", "dirty"),
        ("ignored_ok", "ignored_content"),
        ("unowned", "owned"),
    ])
    def test_each_failing_gate_names_itself(self, wp, key, expected):
        gates = dict(ALL_PASS)
        if key == "kept":
            gates[key] = True
        elif isinstance(ALL_PASS[key], tuple):
            gates[key] = (False, None)
        else:
            gates[key] = False
        assert wp.decide(_cand(wp), gates) == ("SKIP", expected)

    @pytest.mark.parametrize("why", ["uncontained", "remote_unconfirmed"])
    def test_containment_carries_its_own_reason(self, wp, why):
        gates = dict(ALL_PASS)
        gates["contained"] = (False, why)
        assert wp.decide(_cand(wp), gates) == ("SKIP", why)

    def test_kept_outranks_every_other_failure(self, wp):
        gates = {k: (False if k != "contained" else (False, "uncontained"))
                 for k in ALL_PASS}
        gates["kept"] = True
        assert wp.decide(_cand(wp), gates) == ("SKIP", "kept")


class TestDryRun:
    def test_dry_run_decisioning_ledger_and_no_mutation(self, wp, monkeypatch):
        elig = _cand(wp, path="/wt/elig", branch="feat-elig", head="h-elig", clone="/clone")
        unmerged = _cand(wp, path="/wt/un", branch="feat-un", head="h-un", clone="/clone")
        dirty = _cand(wp, path="/wt/dirty", branch="feat-dirty", head="h-dirty", clone="/clone")
        monkeypatch.setattr(wp, "enumerate_candidates",
                            lambda run, cp, roots: [elig, unmerged, dirty])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda run: [])

        calls = []

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            calls.append(args)
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == "gh":
                branch = _gh_head_branch(args)
                merged = {"feat-elig": "h-elig", "feat-dirty": "h-dirty"}
                if branch in merged:
                    return _RR(wp, 0, _gh_body(args, "MERGED", merged[branch]))
                return _RR(wp, 0, _gh_body(args, "OPEN", "x"))
            if "status" in args:
                return _RR(wp, 0, " M src/x.py\n" if args[2] == "/wt/dirty" else "")
            if "merge-base" in args:
                return _RR(wp, 1)
            raise AssertionError(f"unexpected run: {args}")

        decision, info = wp.run_prune(NOW, apply=False, run=run,
                                      clone_parents=["/cp"], roots=["/r"],
                                      self_path="/elsewhere")

        res = {r["path"]: r for r in info["results"]}
        assert res["/wt/elig"]["action"] == "WOULD-REMOVE"
        assert res["/wt/elig"]["reason"] is None
        assert res["/wt/un"]["action"] == "SKIP"
        assert res["/wt/un"]["reason"] == "open_pr"
        assert res["/wt/dirty"]["action"] == "SKIP"
        assert res["/wt/dirty"]["reason"] == "dirty"

        assert info["summary"]["scanned"] == 3
        assert info["summary"]["would_remove"] == 1
        assert info["summary"]["skipped"] == 2
        assert info["summary"]["by_reason"] == {"open_pr": 1, "dirty": 1}

        led = _ledger_by_event(wp)
        assert len(led.get("would_remove", [])) == 1
        assert led["would_remove"][0]["path"] == "/wt/elig"
        assert wp.CHECK_LOG_PATH.is_file()

        for args in calls:
            assert "remove" not in args
            assert not (args[:2] == ["git", "-C"] and "branch" in args and "-D" in args)

    def test_fetch_failure_skips_only_that_clone(self, wp, monkeypatch):
        candA = _cand(wp, path="/wtA", branch="A", head="hA", clone="/cloneA")
        candB = _cand(wp, path="/wtB", branch="B", head="hB", clone="/cloneB")
        monkeypatch.setattr(wp, "enumerate_candidates",
                            lambda run, cp, roots: [candA, candB])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda run: [])

        gh_branches = []

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if "fetch" in args:
                clone = args[2]
                return _RR(wp, 0) if clone == "/cloneA" else _RR(wp, 128, "", "boom")
            if args[0] == "gh":
                gh_branches.append(_gh_head_branch(args))
                return _RR(wp, 0, _gh_body(
                    args, "MERGED", {"A": "hA", "B": "hB"}[_gh_head_branch(args)]))
            if "status" in args:
                return _RR(wp, 0, "")
            if "merge-base" in args:
                return _RR(wp, 1)
            raise AssertionError(f"unexpected run: {args}")

        decision, info = wp.run_prune(NOW, apply=False, run=run,
                                      clone_parents=["/cp"], roots=["/r"],
                                      self_path="/elsewhere")
        res = {r["path"]: r for r in info["results"]}
        assert res["/wtB"]["action"] == "SKIP"
        assert res["/wtB"]["reason"] == "fetch_failed"
        assert res["/wtA"]["action"] == "WOULD-REMOVE"
        assert "B" not in gh_branches
        assert info["summary"]["by_reason"].get("fetch_failed") == 1


REMOVE = ["git", "-C", "/clone", "worktree", "remove", "--force", "/wt"]
BRANCH_D = ["git", "-C", "/clone", "branch", "-D", "feat"]


def _is_branch_delete(args):
    return args[:2] == ["git", "-C"] and "branch" in args and "-D" in args


def _gh_merged(wp, head, args=None):
    if args is not None and "list" in args:
        return _RR(wp, 0, json.dumps([{"number": 1, "state": "MERGED",
                                       "headRefOid": head, "baseRefName": "main"}]))
    return _RR(wp, 0, json.dumps({"state": "MERGED", "headRefOid": head}))


class TestApply:
    def _wire(self, wp, monkeypatch, cands, active=None, lsof=None):
        monkeypatch.setattr(wp, "enumerate_candidates", lambda r, cp, roots: cands)
        monkeypatch.setattr(wp, "_load_active_records",
                            active if callable(active) else (lambda d: active or []))
        monkeypatch.setattr(wp, "_collect_lsof_cwds",
                            lsof if callable(lsof) else (lambda r: lsof or []))

    def test_apply_removes_and_deletes_branch(self, wp, monkeypatch):
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        self._wire(wp, monkeypatch, [cand])
        calls = []

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            calls.append(args)
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == "gh":
                return _gh_merged(wp, "h", args)
            if "status" in args:
                return _RR(wp, 0, "")
            if "rev-parse" in args:
                return _RR(wp, 0, "h\n")
            if "remove" in args:
                return _RR(wp, 0)
            if _is_branch_delete(args):
                return _RR(wp, 0)
            raise AssertionError(args)

        decision, info = wp.run_prune(NOW, apply=True, run=run, clone_parents=["/cp"],
                                      roots=["/r"], self_path="/elsewhere")
        assert REMOVE in calls and BRANCH_D in calls
        assert calls.index(REMOVE) < calls.index(BRANCH_D)
        led = _ledger_by_event(wp)
        assert led.get("removed") and led.get("branch_deleted")
        assert info["summary"]["removed"] == 1
        assert {r["path"]: r["action"] for r in info["results"]}["/wt"] == "REMOVED"

    def test_apply_reverify_dirty_skips(self, wp, monkeypatch):
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        self._wire(wp, monkeypatch, [cand])
        n = {"status": 0}

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == "gh":
                return _gh_merged(wp, "h", args)
            if "status" in args:
                n["status"] += 1
                return _RR(wp, 0, "" if n["status"] == 1 else " M src/x.py\n")
            if "remove" in args:
                raise AssertionError("must not remove on toctou-dirty")
            raise AssertionError(args)

        decision, info = wp.run_prune(NOW, apply=True, run=run, clone_parents=["/cp"],
                                      roots=["/r"], self_path="/elsewhere")
        led = _ledger_by_event(wp)
        assert led["skip_toctou"][0]["reason"] == "toctou_dirty"
        assert info["summary"]["removed"] == 0

    def test_apply_reverify_owner_skips(self, wp, monkeypatch):
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        n = {"lsof": 0}

        def fake_lsof(r):
            n["lsof"] += 1
            return [] if n["lsof"] == 1 else ["/wt/sub"]

        self._wire(wp, monkeypatch, [cand], lsof=fake_lsof)

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == "gh":
                return _gh_merged(wp, "h", args)
            if "status" in args:
                return _RR(wp, 0, "")
            if "remove" in args:
                raise AssertionError("must not remove on toctou-owned")
            raise AssertionError(args)

        decision, info = wp.run_prune(NOW, apply=True, run=run, clone_parents=["/cp"],
                                      roots=["/r"], self_path="/elsewhere")
        led = _ledger_by_event(wp)
        assert led["skip_toctou"][0]["reason"] == "toctou_owned"
        assert info["summary"]["removed"] == 0

    def test_apply_detached_removes_without_branch_delete(self, wp, monkeypatch):
        cand = _cand(wp, path="/wt", branch=None, detached=True, head="h", clone="/clone")
        self._wire(wp, monkeypatch, [cand])
        calls = []

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            calls.append(args)
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == "gh":
                raise AssertionError("no gh for detached HEAD")
            if "status" in args:
                return _RR(wp, 0, "")
            if "merge-base" in args:
                return _RR(wp, 0)
            if "rev-parse" in args:
                return _RR(wp, 0, "h\n")
            if "remove" in args:
                return _RR(wp, 0)
            raise AssertionError(args)

        decision, info = wp.run_prune(NOW, apply=True, run=run, clone_parents=["/cp"],
                                      roots=["/r"], self_path="/elsewhere")
        assert REMOVE in calls
        assert not any(_is_branch_delete(a) for a in calls)
        assert info["summary"]["removed"] == 1

    def test_apply_rate_cap(self, wp, monkeypatch):
        c1 = _cand(wp, path="/wt1", branch="f1", head="h1", clone="/clone")
        c2 = _cand(wp, path="/wt2", branch="f2", head="h2", clone="/clone")
        self._wire(wp, monkeypatch, [c1, c2])
        removed = []

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == "gh":
                return _gh_merged(wp, {"f1": "h1", "f2": "h2"}[_gh_head_branch(args)], args)
            if "status" in args:
                return _RR(wp, 0, "")
            if "rev-parse" in args:
                return _RR(wp, 0, {"/wt1": "h1", "/wt2": "h2"}[args[2]] + "\n")
            if "remove" in args:
                removed.append(args[6])
                return _RR(wp, 0)
            if _is_branch_delete(args):
                return _RR(wp, 0)
            raise AssertionError(args)

        decision, info = wp.run_prune(NOW, apply=True, run=run, max_removals=1,
                                      clone_parents=["/cp"], roots=["/r"],
                                      self_path="/elsewhere")
        assert len(removed) == 1
        assert info["summary"]["removed"] == 1
        assert info["summary"]["capped"] == 1

    def test_apply_rate_cap_uses_module_default_when_unset(self, wp, monkeypatch):
        cands = [_cand(wp, path=f"/wt{i}", branch=f"f{i}", head=f"h{i}",
                       clone="/clone")
                 for i in range(76)]
        self._wire(wp, monkeypatch, cands)
        head_of_branch = {c.branch: c.head for c in cands}
        head_of_path = {c.path: c.head for c in cands}
        removed = []

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == "gh":
                return _gh_merged(wp, head_of_branch[_gh_head_branch(args)], args)
            if "status" in args:
                return _RR(wp, 0, "")
            if "rev-parse" in args:
                return _RR(wp, 0, head_of_path[args[2]] + "\n")
            if "remove" in args:
                removed.append(args[6])
                return _RR(wp, 0)
            if _is_branch_delete(args):
                return _RR(wp, 0)
            raise AssertionError(args)

        decision, info = wp.run_prune(NOW, apply=True, run=run,
                                      clone_parents=["/cp"], roots=["/r"],
                                      self_path="/elsewhere")
        assert len(removed) == 75
        assert info["summary"]["removed"] == 75
        assert info["summary"]["capped"] == 1

    def test_apply_remove_failure_skips_branch_delete(self, wp, monkeypatch):
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        self._wire(wp, monkeypatch, [cand])
        calls = []

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            calls.append(args)
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == "gh":
                return _gh_merged(wp, "h", args)
            if "status" in args:
                return _RR(wp, 0, "")
            if "rev-parse" in args:
                return _RR(wp, 0, "h\n")
            if "remove" in args:
                return _RR(wp, 1, "", "cannot remove worktree")
            if _is_branch_delete(args):
                raise AssertionError("must not delete branch after a failed remove")
            raise AssertionError(args)

        decision, info = wp.run_prune(NOW, apply=True, run=run, clone_parents=["/cp"],
                                      roots=["/r"], self_path="/elsewhere")
        led = _ledger_by_event(wp)
        assert led.get("remove_failed")
        assert not any(_is_branch_delete(a) for a in calls)
        assert info["summary"]["removed"] == 0

    def test_apply_reverify_head_moved_skips(self, wp, monkeypatch):
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        self._wire(wp, monkeypatch, [cand])

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == "gh":
                return _gh_merged(wp, "h", args)
            if "status" in args:
                return _RR(wp, 0, "")
            if "rev-parse" in args:
                return _RR(wp, 0, "NEWSHA-landed-after-scan\n")
            if "remove" in args:
                raise AssertionError("must not remove when HEAD moved past scan SHA")
            if _is_branch_delete(args):
                raise AssertionError("must not delete branch when HEAD moved")
            raise AssertionError(args)

        decision, info = wp.run_prune(NOW, apply=True, run=run, clone_parents=["/cp"],
                                      roots=["/r"], self_path="/elsewhere")
        led = _ledger_by_event(wp)
        assert led["skip_toctou"][0]["reason"] == "toctou_head_moved"
        assert info["summary"]["removed"] == 0
        assert {x["path"]: x["action"] for x in info["results"]}["/wt"] == "SKIP"

    def test_apply_reverify_revparse_failure_skips(self, wp, monkeypatch):
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        self._wire(wp, monkeypatch, [cand])

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == "gh":
                return _gh_merged(wp, "h", args)
            if "status" in args:
                return _RR(wp, 0, "")
            if "rev-parse" in args:
                return _RR(wp, 128, "", "fatal: not a git repository")
            if "remove" in args:
                raise AssertionError("must not remove when rev-parse fails")
            raise AssertionError(args)

        decision, info = wp.run_prune(NOW, apply=True, run=run, clone_parents=["/cp"],
                                      roots=["/r"], self_path="/elsewhere")
        assert _ledger_by_event(wp)["skip_toctou"][0]["reason"] == "toctou_head_moved"
        assert info["summary"]["removed"] == 0


class TestCLI:
    def _stub(self, wp, monkeypatch, decision, info, seen=None):
        def fake_run_prune(now, apply=False, **kw):
            if seen is not None:
                seen["apply"] = apply
            return decision, info

        monkeypatch.setattr(wp, "run_prune", fake_run_prune)

    def test_json_output(self, wp, monkeypatch, capsys):
        seen = {}
        info = {"results": [{"path": "/wt", "action": "WOULD-REMOVE", "reason": None}],
                "summary": {"scanned": 1, "would_remove": 1, "removed": 0,
                            "skipped": 0, "capped": 0, "by_reason": {}}}
        self._stub(wp, monkeypatch, "dry-run", info, seen)
        rc = wp.main(["--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["decision"] == "dry-run"
        assert data["results"][0]["path"] == "/wt"
        assert seen["apply"] is False

    def test_default_does_not_apply(self, wp, monkeypatch):
        seen = {}
        self._stub(wp, monkeypatch, "dry-run",
                   {"results": [], "summary": wp._empty_summary()}, seen)
        assert wp.main([]) == 0
        assert seen["apply"] is False

    def test_apply_flag_passed_through(self, wp, monkeypatch):
        seen = {}
        self._stub(wp, monkeypatch, "applied",
                   {"results": [], "summary": wp._empty_summary()}, seen)
        assert wp.main(["--apply"]) == 0
        assert seen["apply"] is True

    def test_table_and_summary_line(self, wp, monkeypatch, capsys):
        info = {"results": [
            {"path": "/wt/a", "action": "WOULD-REMOVE", "reason": None},
            {"path": "/wt/b", "action": "SKIP", "reason": "dirty"},
        ], "summary": {"scanned": 2, "would_remove": 1, "removed": 0,
                       "skipped": 1, "capped": 0, "by_reason": {"dirty": 1}}}
        self._stub(wp, monkeypatch, "dry-run", info)
        wp.main([])
        out = capsys.readouterr().out
        assert "/wt/a" in out and "WOULD-REMOVE" in out
        assert "/wt/b" in out and "dirty" in out
        assert "scanned" in out and "would_remove" in out

    def test_dry_run_overrides_apply(self, wp, monkeypatch):
        seen = {}
        self._stub(wp, monkeypatch, "dry-run",
                   {"results": [], "summary": wp._empty_summary()}, seen)
        assert wp.main(["--apply", "--dry-run"]) == 0
        assert seen["apply"] is False


class TestCollectLsof:
    def test_rc0_parses_n_lines(self, wp):
        out = "p100\nn/path/a\np200\nn/path/b\n"

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            assert args == [wp.LSOF_BIN, "-d", "cwd", "-F", "pn"]
            return _RR(wp, 0, out)

        assert wp._collect_lsof_cwds(run) == ["/path/a", "/path/b"]

    def test_rc_nonzero_with_output_keeps_paths(self, wp):
        out = "p100\nn/path/a\n"

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            return _RR(wp, 1, out, "lsof: WARNING: can't stat()")

        assert wp._collect_lsof_cwds(run) == ["/path/a"]

    def test_rc_nonzero_no_output_is_none(self, wp):
        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            return _RR(wp, 1, "", "lsof: command failed")

        assert wp._collect_lsof_cwds(run) is None

    def test_run_raises_is_none(self, wp):
        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            raise FileNotFoundError("lsof not installed")

        assert wp._collect_lsof_cwds(run) is None

    def test_rc0_no_n_lines_is_empty_list(self, wp):
        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            return _RR(wp, 0, "p100\np200\n")

        assert wp._collect_lsof_cwds(run) == []


class TestLoadActiveRecords:
    def test_missing_dir_is_none(self, wp, tmp_path):
        assert wp._load_active_records(tmp_path / "nope") is None

    def test_valid_plus_corrupt_returns_valid_only(self, wp, tmp_path):
        d = tmp_path / "active"
        d.mkdir()
        (d / "good.json").write_text(json.dumps({"cwd": "/wt", "pid": LIVE_PID}))
        (d / "bad.json").write_text("{not json")
        assert wp._load_active_records(d) == [{"cwd": "/wt", "pid": LIVE_PID}]

    def test_unreadable_dir_is_none(self, wp, tmp_path):
        import os as _os
        if _os.geteuid() == 0:
            pytest.skip("root bypasses directory permissions")
        d = tmp_path / "active"
        d.mkdir()
        (d / "x.json").write_text("{}")
        _os.chmod(d, 0)
        try:
            assert wp._load_active_records(d) is None
        finally:
            _os.chmod(d, 0o755)


class TestIsUnder:
    def test_descendant_is_under(self, wp):
        assert wp._is_under("/wt/sub", "/wt") is True

    def test_equal_is_under(self, wp):
        assert wp._is_under("/wt", "/wt") is True

    def test_sibling_prefix_is_not_under(self, wp):
        assert wp._is_under("/wt-foo", "/wt") is False

    def test_unrelated_is_not_under(self, wp):
        assert wp._is_under("/other", "/wt") is False

    def test_symlinked_root_matches(self, wp, tmp_path):
        import os as _os
        real = tmp_path / "realroot"
        (real / "wt").mkdir(parents=True)
        link = tmp_path / "linkroot"
        _os.symlink(real, link)
        assert wp._is_under(str(link / "wt"), str(real)) is True
        assert wp._is_under(str(real / "wt"), str(link)) is True


class TestScanGateException:
    def test_gate_exception_resolves_to_skip_error_no_removal(self, wp, monkeypatch):
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        monkeypatch.setattr(wp, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda r: [])

        def boom(run, c, stats=None):
            raise RuntimeError("gate blew up")

        monkeypatch.setattr(wp, "gate_merged_head", boom)

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if "fetch" in args:
                return _RR(wp, 0)
            if "remove" in args:
                raise AssertionError("must not remove when a gate raised")
            return _RR(wp, 0, "")

        decision, info = wp.run_prune(NOW, apply=True, run=run, clone_parents=["/cp"],
                                      roots=["/r"], self_path="/elsewhere")
        r = {x["path"]: x for x in info["results"]}["/wt"]
        assert r["action"] == "SKIP"
        assert r["reason"] == "error"
        assert info["summary"]["removed"] == 0
        assert info["summary"]["by_reason"].get("error") == 1

    def test_a_gate_missing_from_decide_order_refuses_instead_of_passing(self, wp, monkeypatch):
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        monkeypatch.setattr(wp, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda r: [])
        monkeypatch.setattr(wp, "DECIDE_ORDER",
                            tuple(e for e in wp.DECIDE_ORDER if e[0] != "merged"))
        calls = []

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            calls.append(args)
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == wp.GH_BIN:
                return _RR(wp, 0, _gh_body(args, "CLOSED", "h"))
            if "merge-base" in args:
                return _RR(wp, 1)
            if "rev-parse" in args:
                return _RR(wp, 0, "h\n")
            return _RR(wp, 0, "")

        _d, info = wp.run_prune(NOW, apply=True, run=run, clone_parents=["/cp"],
                                roots=["/r"], self_path="/elsewhere")
        r = {x["path"]: x for x in info["results"]}["/wt"]
        assert (r["action"], r["reason"]) == ("SKIP", "error")
        assert info["summary"]["removed"] == 0
        assert not any("remove" in a for a in calls)


class TestNowTimestamp:
    def test_ledger_ts_uses_now(self, wp, monkeypatch):
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        monkeypatch.setattr(wp, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda r: [])

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == "gh":
                return _gh_merged(wp, "h", args)
            if "status" in args:
                return _RR(wp, 0, "")
            return _RR(wp, 1)

        wp.run_prune(NOW, apply=False, run=run, clone_parents=["/cp"], roots=["/r"],
                     self_path="/elsewhere")
        assert _ledger_by_event(wp)["would_remove"][0]["ts"] == NOW


class TestHomeFallback:
    def test_prefers_dockwright_homes(self, tmp_path, monkeypatch):
        claude = tmp_path / ".claude"
        for rel in ("dockwright", "orchestrator", "dockwright/worktree-prune", "worktree-prune"):
            (claude / rel).mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        mod = _load()
        assert mod.ORCH_ACTIVE == claude / "dockwright" / "active"
        assert mod.WT_DIR == claude / "dockwright" / "worktree-prune"
        assert mod.STOP_PATHS[0] == claude / "dockwright" / "worktree-prune-stop"

    def test_falls_back_to_legacy_homes(self, tmp_path, monkeypatch):
        claude = tmp_path / ".claude"
        for rel in ("orchestrator", "worktree-prune"):
            (claude / rel).mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        mod = _load()
        assert mod.ORCH_ACTIVE == claude / "orchestrator" / "active"
        assert mod.WT_DIR == claude / "worktree-prune"


import subprocess as _sp


def _git(cwd, *args, check=True):
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return _sp.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                   env=env, check=check)


def _real_run(wp, gh_json=None):
    def run(args, cwd=None):
        if args and args[0] == wp.GH_BIN:
            if gh_json is None:
                return _RR(wp, 1, "", "no gh")
            return _RR(wp, 0, gh_json(args))
        p = _sp.run(args, cwd=cwd, capture_output=True, text=True,
                    env=dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null",
                             GIT_CONFIG_SYSTEM="/dev/null"))
        return _RR(wp, p.returncode, p.stdout, p.stderr)
    return run


@pytest.fixture
def repo(tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-q", "-b", "main", ".")
    (clone / "a.txt").write_text("a")
    _git(clone, "add", "a.txt")
    _git(clone, "commit", "-qm", "base")
    return clone


class TestContainmentRealRepo:
    def _cands(self, wp, clone):
        run = _real_run(wp)
        recs = wp._parse_worktree_porcelain(
            _git(clone, "worktree", "list", "--porcelain").stdout)
        return [wp.Candidate(path=r["path"], head=r.get("head", ""),
                             branch=r.get("branch"), detached=bool(r.get("detached")),
                             clone=str(clone), locked=bool(r.get("locked")))
                for r in recs if r["path"] != str(clone)], run

    def test_detached_held_only_by_a_deletable_branch_is_skipped(self, wp, repo, tmp_path):
        _git(repo, "worktree", "add", "-q", "-b", "feat", str(tmp_path / "w1"))
        (tmp_path / "w1" / "u.txt").write_text("u")
        _git(tmp_path / "w1", "add", "u.txt")
        _git(tmp_path / "w1", "commit", "-qm", "work")
        x = _git(tmp_path / "w1", "rev-parse", "HEAD").stdout.strip()
        _git(repo, "worktree", "add", "-q", "--detach", str(tmp_path / "w2"), x)

        cands, run = self._cands(wp, repo)
        det = [c for c in cands if c.detached][0]
        assert det.head == x
        ok, why = wp.gate_contained(run, det)
        assert (ok, why) == (False, "uncontained")

    def test_detached_on_a_tag_is_contained_without_touching_the_remote(self, wp, repo, tmp_path):
        _git(repo, "tag", "v1")
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _git(repo, "worktree", "add", "-q", "--detach", str(tmp_path / "w"), head)
        cands, _ = self._cands(wp, repo)
        det = [c for c in cands if c.detached][0]
        calls = []

        def run(args, cwd=None):
            calls.append(args)
            return _real_run(wp)(args, cwd)

        assert wp.gate_contained(run, det) == (True, None)
        assert not any("ls-remote" in a for a in calls)

    @pytest.mark.parametrize("ns,ref", [
        ("stash", "refs/stash"),
        ("tmp", "refs/tmp/pr1"),
        ("heads", "refs/heads/rescue-x"),
    ])
    def test_non_durable_namespaces_are_not_proof(self, wp, repo, tmp_path, ns, ref):
        _git(repo, "worktree", "add", "-q", "--detach", str(tmp_path / "w"))
        (tmp_path / "w" / "z.txt").write_text("z")
        _git(tmp_path / "w", "add", "z.txt")
        _git(tmp_path / "w", "commit", "-qm", "detached work")
        x = _git(tmp_path / "w", "rev-parse", "HEAD").stdout.strip()
        _git(repo, "update-ref", ref, x)

        cands, run = self._cands(wp, repo)
        det = [c for c in cands if c.detached][0]
        ok, _why = wp.gate_contained(run, det)
        assert ok is False, f"{ref} must not count as durable proof"

    def test_allowlisted_namespaces_are_pinned(self, wp):
        assert (wp.PROOF_MAIN_REF, wp.PROOF_TAG_NAMESPACE, wp.PROOF_REMOTE_NAMESPACE) \
            == ("refs/remotes/origin/main", "refs/tags/", "refs/remotes/")


@pytest.fixture
def merged_repo(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    clone = tmp_path / "clones" / "svc"
    clone.mkdir(parents=True)
    _git(clone, "init", "-q", "-b", "main", ".")
    (clone / "a.txt").write_text("a")
    _git(clone, "add", "a.txt")
    _git(clone, "commit", "-qm", "base")
    _git(clone, "remote", "add", "origin", str(origin))
    _git(clone, "push", "-q", "origin", "main")
    return clone


def _branch_tree(clone, root, name):
    path = root / "grp" / name
    _git(clone, "worktree", "add", "-q", "-b", name, str(path))
    (path / f"{name}.txt").write_text(name)
    _git(path, "add", f"{name}.txt")
    _git(path, "commit", "-qm", name)
    return path, _git(path, "rev-parse", "HEAD").stdout.strip()


def _squash_into_origin_main(clone, name):
    _git(clone, "merge", "-q", "--squash", name)
    _git(clone, "commit", "-qm", f"squash {name}")
    _git(clone, "push", "-q", "origin", "main")


def _gh_answer(prs):
    def answer(args):
        if "list" in args:
            return json.dumps(prs)
        return json.dumps(prs[-1] if prs else {})
    return answer


class TestMergedGateRealRepo:
    def _prune(self, wp, monkeypatch, tmp_path, gh_json):
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda r: [])
        _d, info = wp.run_prune(NOW, apply=True, run=_real_run(wp, gh_json),
                                clone_parents=[str(tmp_path / "clones")],
                                roots=[str(tmp_path / "wt")], self_path="/elsewhere")
        return info

    def _row(self, info, tree):
        return {r["path"]: r for r in info["results"]}[os.path.realpath(tree)]

    def _branch_exists(self, clone, name):
        return bool(_git(clone, "branch", "--list", name).stdout.strip())

    def test_a_pr_closed_unmerged_keeps_the_tree_and_the_branch(self, wp, merged_repo, tmp_path,
                                                                monkeypatch):
        tree, head = _branch_tree(merged_repo, tmp_path / "wt", "feat")
        info = self._prune(wp, monkeypatch, tmp_path,
                           _gh_answer([{"number": 1, "state": "CLOSED", "headRefOid": head}]))
        assert self._row(info, tree)["reason"] == "unmerged"
        assert info["summary"]["removed"] == 0
        assert tree.exists() and self._branch_exists(merged_repo, "feat")

    def test_a_pr_merged_at_an_older_commit_keeps_the_tree(self, wp, merged_repo, tmp_path,
                                                           monkeypatch):
        older = _git(merged_repo, "rev-parse", "HEAD").stdout.strip()
        tree, _head = _branch_tree(merged_repo, tmp_path / "wt", "feat")
        info = self._prune(wp, monkeypatch, tmp_path,
                           _gh_answer([{"number": 1, "state": "MERGED", "headRefOid": older,
                                        "baseRefName": "main"}]))
        assert self._row(info, tree)["reason"] == "unmerged"
        assert tree.exists() and self._branch_exists(merged_repo, "feat")

    def test_a_squash_merged_pr_at_this_head_removes_the_tree_and_the_branch(
            self, wp, merged_repo, tmp_path, monkeypatch):
        tree, head = _branch_tree(merged_repo, tmp_path / "wt", "feat")
        _squash_into_origin_main(merged_repo, "feat")
        info = self._prune(wp, monkeypatch, tmp_path,
                           _gh_answer([{"number": 1, "state": "MERGED", "headRefOid": head,
                                        "baseRefName": "main"}]))
        assert self._row(info, tree)["action"] == "REMOVED"
        assert not tree.exists() and not self._branch_exists(merged_repo, "feat")

    def test_an_ancestor_of_origin_main_without_a_pr_is_removed(self, wp, merged_repo, tmp_path,
                                                                monkeypatch):
        tree, _head = _branch_tree(merged_repo, tmp_path / "wt", "feat")
        _git(merged_repo, "merge", "-q", "--ff-only", "feat")
        _git(merged_repo, "push", "-q", "origin", "main")
        info = self._prune(wp, monkeypatch, tmp_path, _gh_answer([]))
        assert self._row(info, tree)["action"] == "REMOVED"
        assert not tree.exists() and not self._branch_exists(merged_repo, "feat")

    def test_an_open_pr_beside_a_merged_one_keeps_the_tree(self, wp, merged_repo, tmp_path,
                                                           monkeypatch):
        tree, head = _branch_tree(merged_repo, tmp_path / "wt", "feat")
        _squash_into_origin_main(merged_repo, "feat")
        info = self._prune(wp, monkeypatch, tmp_path, _gh_answer([
            {"number": 1, "state": "MERGED", "headRefOid": head, "baseRefName": "main"},
            {"number": 2, "state": "OPEN", "headRefOid": head, "baseRefName": "main"}]))
        assert self._row(info, tree)["reason"] == "open_pr"
        assert tree.exists()

    def test_a_pr_merged_into_another_branch_keeps_the_tree_and_the_branch(
            self, wp, merged_repo, tmp_path, monkeypatch):
        tree, head = _branch_tree(merged_repo, tmp_path / "wt", "feat")
        _git(merged_repo, "branch", "stack", "feat")
        info = self._prune(wp, monkeypatch, tmp_path, _gh_answer([
            {"number": 1, "state": "MERGED", "headRefOid": head, "baseRefName": "stack"}]))
        assert self._row(info, tree)["reason"] == "unmerged"
        assert info["summary"]["removed"] == 0
        assert tree.exists() and self._branch_exists(merged_repo, "feat")

    def test_a_gh_failure_keeps_an_unmerged_squash(self, wp, merged_repo, tmp_path, monkeypatch):
        tree, _head = _branch_tree(merged_repo, tmp_path / "wt", "feat")
        _squash_into_origin_main(merged_repo, "feat")
        info = self._prune(wp, monkeypatch, tmp_path, None)
        assert self._row(info, tree)["reason"] == "unmerged"
        assert info["summary"]["gh_failed"] == 1
        assert tree.exists() and self._branch_exists(merged_repo, "feat")


class TestInProgressRealGit:
    def test_marker_flag_is_repeated_per_marker(self, wp, repo, tmp_path):
        _git(repo, "worktree", "add", "-q", "--detach", str(tmp_path / "w"))
        cand = wp.Candidate(path=str(tmp_path / "w"), head="", branch=None,
                            detached=True, clone=str(repo))
        seen = []

        def run(args, cwd=None):
            seen.append(args)
            return _real_run(wp)(args, cwd)

        assert wp.gate_in_progress(run, cand) is True
        argv = [a for a in seen if "--git-path" in a][0]
        assert argv.count("--git-path") == len(wp.IN_PROGRESS_MARKERS)

    def test_paused_rebase_blocks_removal(self, wp, repo, tmp_path):
        for i in (1, 2):
            (repo / f"f{i}").write_text(str(i))
            _git(repo, "add", f"f{i}")
            _git(repo, "commit", "-qm", f"c{i}")
        wt = tmp_path / "w"
        _git(repo, "worktree", "add", "-q", "-b", "rb", str(wt))
        seq_editor = tmp_path / "seq_editor.py"
        seq_editor.write_text(
            "import sys\n"
            "path = sys.argv[1]\n"
            "lines = open(path, encoding='utf-8').readlines()\n"
            "assert lines[1].startswith('pick '), lines[1]\n"
            "lines[1] = 'edit' + lines[1][len('pick'):]\n"
            "open(path, 'w', encoding='utf-8').writelines(lines)\n")
        env_editor = f"{shlex.quote(sys.executable)} {shlex.quote(str(seq_editor))}"
        _sp.run(["git", "rebase", "-i", "HEAD~2"], cwd=str(wt), capture_output=True,
                env=dict(os.environ, GIT_SEQUENCE_EDITOR=env_editor,
                         GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null"))
        cand = wp.Candidate(path=str(wt), head="", branch=None, detached=True,
                            clone=str(repo))
        run = _real_run(wp)
        assert _git(wt, "status", "--porcelain").stdout.strip() == "", \
            "precondition: a paused rebase looks clean to gate B"
        assert wp.gate_in_progress(run, cand) is False

    @pytest.mark.parametrize("marker", ["rebase-merge", "rebase-apply", "BISECT_START",
                                        "MERGE_HEAD", "CHERRY_PICK_HEAD",
                                        "REVERT_HEAD", "sequencer"])
    def test_every_marker_blocks(self, wp, repo, tmp_path, marker):
        wt = tmp_path / "w"
        _git(repo, "worktree", "add", "-q", "--detach", str(wt))
        cand = wp.Candidate(path=str(wt), head="", branch=None, detached=True,
                            clone=str(repo))
        run = _real_run(wp)
        assert wp.gate_in_progress(run, cand) is True
        gitdir = _git(wt, "rev-parse", "--git-path", marker).stdout.strip()
        target = os.path.join(str(wt), gitdir) if not os.path.isabs(gitdir) else gitdir
        os.makedirs(os.path.dirname(target), exist_ok=True)
        Path(target).write_text("x")
        assert wp.gate_in_progress(run, cand) is False

    def test_git_failure_is_treated_as_in_progress(self, wp):
        cand = _cand(wp, path="/wt")
        assert wp.gate_in_progress(lambda a, c=None: _RR(wp, 128), cand) is False


class TestIgnoredContent:
    def test_allowlist_is_pinned(self, wp):
        assert wp.IGNORED_ARTIFACT_NAMES == frozenset({
            "target", "node_modules", ".venv", "venv", "__pycache__",
            ".pytest_cache", "build", "dist", ".gradle", "out", "coverage",
            ".angular", ".nx", ".playwright-mcp", ".ruff_cache", ".mypy_cache",
            ".terraform", "htmlcov", ".tox", ".claude", "CLAUDE.md", ".mcp.json",
            ".codex", ".DS_Store", ".flattened-pom.xml"})

    @pytest.mark.parametrize("name", sorted({
        "target", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
        "build", "dist", ".gradle", "out", "coverage", ".angular", ".nx",
        ".playwright-mcp", ".ruff_cache", ".mypy_cache", ".terraform", "htmlcov",
        ".tox", ".claude", "CLAUDE.md", ".mcp.json", ".codex", ".DS_Store",
        ".flattened-pom.xml"}))
    def test_every_allowlisted_name_passes_at_top_level_and_nested(self, wp, name):
        assert wp.ignored_ok_from_porcelain(f"!! {name}\n") is True
        assert wp.ignored_ok_from_porcelain(f"!! service/{name}/\n") is True

    @pytest.mark.parametrize("entry", [
        "docs/", "docs/sidecar/specs/x-design.md", ".sidecar/",
        "notes.md", "service/src/main/codegen/output/schema.json", ".idea/",
    ])
    def test_unknown_ignored_content_blocks(self, wp, entry):
        assert wp.ignored_ok_from_porcelain(f"!! {entry}\n") is False

    def test_ignored_lines_are_not_dirt_for_gate_b(self, wp):
        text = "!! docs/\n"
        assert wp.clean_from_porcelain(text) is True
        assert wp.ignored_ok_from_porcelain(text) is False

    def test_one_status_call_serves_both_gates(self, wp):
        calls = []

        def run(args, cwd=None):
            calls.append(args)
            return _RR(wp, 0, "!! target/\n")

        cand = _cand(wp, path="/wt")
        assert wp.gate_clean(run, cand) is True
        assert wp.gate_ignored(run, cand) is True
        status_calls = [a for a in calls if "status" in a and "submodule" not in a]
        assert status_calls and all("--ignored" in a for a in status_calls)

    def test_design_doc_blocks_an_otherwise_eligible_tree(self, wp, monkeypatch):
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        monkeypatch.setattr(wp, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda r: [])

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == wp.GH_BIN:
                return _RR(wp, 0, _gh_body(args, "MERGED", "h"))
            if "status" in args:
                return _RR(wp, 0, "!! target/\n!! docs/sidecar/specs/d.md\n")
            if "remove" in args:
                raise AssertionError("must not remove a tree holding a design doc")
            return _RR(wp, 0, "")

        _decision, info = wp.run_prune(NOW, apply=True, run=run,
                                       clone_parents=["/cp"], roots=["/r"],
                                       self_path="/elsewhere")
        row = {r["path"]: r for r in info["results"]}["/wt"]
        assert (row["action"], row["reason"]) == ("SKIP", "ignored_content")


class TestKeepList:
    def _run_never(self, wp):
        def run(args, cwd=None):
            raise AssertionError("nothing may run once the hold list is unusable")
        return run

    @pytest.mark.parametrize("apply", [False, True])
    def test_missing_file_stops_the_whole_run(self, wp, apply):
        wp.KEEPLIST_PATH.unlink()
        decision, info = wp.run_prune(NOW, apply=apply, run=self._run_never(wp))
        assert decision == "stopped"
        assert info["summary"] == wp._empty_summary()
        assert _ledger_events(wp) == []

    @pytest.mark.parametrize("apply", [False, True])
    def test_literal_entry_pointing_nowhere_stops_the_run(self, wp, apply):
        wp.KEEPLIST_PATH.write_text("/nope/does/not/exist\n")
        decision, _ = wp.run_prune(NOW, apply=apply, run=self._run_never(wp))
        assert decision == "stopped"

    def test_glob_with_a_missing_prefix_stops_the_run(self, wp):
        wp.KEEPLIST_PATH.write_text("/nope/definitely/*\n")
        decision, _ = wp.run_prune(NOW, apply=False, run=self._run_never(wp))
        assert decision == "stopped"

    def test_tilde_is_expanded(self, wp, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "worktrees" / "TKT-1").mkdir(parents=True)
        entries, fatal = wp.load_keeplist_text("~/worktrees/*")
        assert fatal is None
        assert wp.keeplist_matches(entries, str(tmp_path / "worktrees" / "TKT-1"))

    def test_matching_is_case_insensitive(self, wp, tmp_path):
        held = tmp_path / "tkt-9855"
        held.mkdir()
        entries, fatal = wp.load_keeplist_text(str(held))
        assert fatal is None
        assert wp.keeplist_matches(entries, str(tmp_path / "TKT-9855")) is True

    def test_a_held_directory_covers_its_children(self, wp, tmp_path):
        (tmp_path / "hold" / "repo").mkdir(parents=True)
        entries, _ = wp.load_keeplist_text(str(tmp_path / "hold"))
        assert wp.keeplist_matches(entries, str(tmp_path / "hold" / "repo")) is True

    def test_comments_and_blanks_are_ignored(self, wp):
        entries, fatal = wp.load_keeplist_text("# a comment\n\n   \n")
        assert (entries, fatal) == ([], None)

    def test_kept_is_reported_and_nothing_runs_against_it(self, wp, monkeypatch, tmp_path):
        held = tmp_path / "held"
        held.mkdir()
        wp.KEEPLIST_PATH.write_text(str(held) + "\n")
        cand = _cand(wp, path=str(held), branch="feat", head="h", clone="/clone")
        monkeypatch.setattr(wp, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda r: [])

        def run(args, cwd=None):
            if "remove" in args:
                raise AssertionError("a held tree must never be removed")
            return _RR(wp, 0, "")

        _d, info = wp.run_prune(NOW, apply=True, run=run, clone_parents=["/cp"],
                                roots=["/r"], self_path="/elsewhere")
        row = {r["path"]: r for r in info["results"]}[str(held)]
        assert (row["action"], row["reason"]) == ("SKIP", "kept")
        assert info["summary"]["by_reason"].get("kept") == 1

    def test_a_held_tree_in_a_fetch_failing_clone_still_reports_kept(self, wp, monkeypatch, tmp_path):
        held = tmp_path / "held"
        held.mkdir()
        wp.KEEPLIST_PATH.write_text(str(held) + "\n")
        cand = _cand(wp, path=str(held), branch="feat", head="h", clone="/clone")
        monkeypatch.setattr(wp, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda r: [])

        def run(args, cwd=None):
            if "fetch" in args:
                return _RR(wp, 128, "", "boom")
            return _RR(wp, 0, "")

        _d, info = wp.run_prune(NOW, apply=False, run=run, clone_parents=["/cp"],
                                roots=["/r"], self_path="/elsewhere")
        assert info["summary"]["by_reason"].get("kept") == 1
        assert info["summary"]["by_reason"].get("fetch_failed") is None


class TestProtectedBranchesAndLocks:
    def _wire(self, wp, monkeypatch, cand):
        monkeypatch.setattr(wp, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda r: [])

    @pytest.mark.parametrize("branch", sorted({"main", "master"}))
    def test_protected_branch_is_never_deleted(self, wp, monkeypatch, branch):
        cand = _cand(wp, path="/wt", branch=branch, head="h", clone="/clone")
        self._wire(wp, monkeypatch, cand)
        calls = []

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            calls.append(args)
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == wp.GH_BIN:
                return _RR(wp, 0, _gh_body(args, "MERGED", "h"))
            if "status" in args:
                return _RR(wp, 0, "")
            if "rev-parse" in args:
                return _RR(wp, 0, "h\n")
            if "remove" in args:
                return _RR(wp, 0)
            if _is_branch_delete(args):
                raise AssertionError(f"must never branch -D {branch}")
            return _RR(wp, 0, "")

        _d, info = wp.run_prune(NOW, apply=True, run=run, clone_parents=["/cp"],
                                roots=["/r"], self_path="/elsewhere")
        assert info["summary"]["removed"] == 1
        assert not any(_is_branch_delete(a) for a in calls)
        assert _ledger_by_event(wp).get("branch_delete_refused")

    def test_protected_set_is_pinned(self, wp):
        assert wp.PROTECTED_BRANCHES == frozenset({"main", "master"})

    @pytest.mark.parametrize("line", ["locked", "locked engineer is bisecting"])
    def test_both_locked_spellings_are_parsed(self, wp, line):
        recs = wp._parse_worktree_porcelain(
            f"worktree /wt\nHEAD h\ndetached\n{line}\n\n")
        assert recs[0].get("locked") is True

    def test_locked_tree_is_skipped_without_burning_a_removal_slot(self, wp, monkeypatch):
        cand = wp.Candidate(path="/wt", head="h", branch=None, detached=True,
                            clone="/clone", locked=True)
        self._wire(wp, monkeypatch, cand)

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if "fetch" in args:
                return _RR(wp, 0)
            if "remove" in args:
                raise AssertionError("a locked tree must not reach `worktree remove`")
            return _RR(wp, 0, "")

        _d, info = wp.run_prune(NOW, apply=True, run=run, clone_parents=["/cp"],
                                roots=["/r"], self_path="/elsewhere")
        row = {r["path"]: r for r in info["results"]}["/wt"]
        assert (row["action"], row["reason"]) == ("SKIP", "locked")

    def test_removal_passes_exactly_one_force(self, wp, monkeypatch):
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        self._wire(wp, monkeypatch, cand)
        seen = []

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            seen.append(args)
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == wp.GH_BIN:
                return _RR(wp, 0, _gh_body(args, "MERGED", "h"))
            if "status" in args:
                return _RR(wp, 0, "")
            if "rev-parse" in args:
                return _RR(wp, 0, "h\n")
            return _RR(wp, 0)

        wp.run_prune(NOW, apply=True, run=run, clone_parents=["/cp"], roots=["/r"],
                     self_path="/elsewhere")
        rm = [a for a in seen if "remove" in a][0]
        assert rm.count("--force") == 1 and rm.count("-f") == 0


class TestMergedGateAndGhVisibility:
    def _wire(self, wp, monkeypatch, cand):
        monkeypatch.setattr(wp, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda r: [])

    def test_gh_uses_the_named_binary_and_the_worktree_cwd(self, wp, monkeypatch):
        seen = {}

        def run(args, cwd=None):
            seen["argv"] = args
            seen["cwd"] = cwd
            return _RR(wp, 0, "[]")

        monkeypatch.setattr(wp, "GH_BIN", "/opt/distinct-gh")
        wp._gh_prs(run, _cand(wp, path="/wt", branch="feat"))
        assert seen["argv"][0] == "/opt/distinct-gh"
        assert seen["cwd"] == "/wt"
        assert "list" in seen["argv"] and "--head" in seen["argv"]

    @pytest.mark.parametrize("state,oid,expected", [
        ("MERGED", "h", (True, None)),
        ("MERGED", "older", (False, "unmerged")),
        ("CLOSED", "h", (False, "unmerged")),
        ("OPEN", "h", (False, "open_pr")),
    ])
    def test_pr_state_and_head_decide_merged(self, wp, state, oid, expected):
        def run(args, cwd=None):
            if args[0] == wp.GH_BIN:
                return _RR(wp, 0, _gh_body(args, state, oid))
            return _RR(wp, 1)
        assert wp.gate_merged_head(run, _cand(wp, branch="feat", head="h")) == expected

    @pytest.mark.parametrize("base,expected", [
        ("main", (True, None)),
        ("stack", (False, "unmerged")),
        (None, (False, "unmerged")),
    ])
    def test_a_merged_pr_counts_only_when_it_merged_into_main(self, wp, base, expected):
        def run(args, cwd=None):
            if args[0] == wp.GH_BIN:
                return _RR(wp, 0, _gh_body(args, "MERGED", "h", base=base))
            return _RR(wp, 1)
        assert wp.gate_merged_head(run, _cand(wp, branch="feat", head="h")) == expected

    @pytest.mark.parametrize("other", ["CLOSED", "MERGED"])
    def test_an_open_pr_beats_any_other_on_the_same_head(self, wp, other):
        def run(args, cwd=None):
            if args[0] == wp.GH_BIN:
                return _RR(wp, 0, json.dumps([
                    {"number": 1, "state": other, "headRefOid": "h"},
                    {"number": 2, "state": "OPEN", "headRefOid": "h"}]))
            return _RR(wp, 1)
        assert wp.gate_merged_head(run, _cand(wp, branch="feat", head="h")) == (False, "open_pr")

    @pytest.mark.parametrize("gh", ["fail", "empty"])
    def test_ancestor_is_a_peer_signal_not_a_gh_fallback(self, wp, gh):
        def run(args, cwd=None):
            if args[0] == wp.GH_BIN:
                return _RR(wp, 1, "", "boom") if gh == "fail" else _RR(wp, 0, "[]")
            if "merge-base" in args:
                return _RR(wp, 0)
            return _RR(wp, 0, "")
        assert wp.gate_merged_head(run, _cand(wp, branch="feat", head="h")) == (True, None)

    def test_a_detached_tree_asks_nothing_and_passes(self, wp):
        def run(args, cwd=None):
            raise AssertionError(args)
        assert wp.gate_merged_head(run, _cand(wp, branch=None, detached=True, head="h")) \
            == (True, None)

    def test_gh_failures_are_counted(self, wp, monkeypatch):
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        self._wire(wp, monkeypatch, cand)

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == wp.GH_BIN:
                return _RR(wp, 1, "", "Could not resolve to a Repository")
            if "merge-base" in args:
                return _RR(wp, 1)
            return _RR(wp, 0, "")

        _d, info = wp.run_prune(NOW, apply=False, run=run, clone_parents=["/cp"],
                                roots=["/r"], self_path="/elsewhere")
        assert info["summary"]["gh_failed"] == 1

    def test_healthy_run_reports_zero_gh_failures(self, wp, monkeypatch):
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        self._wire(wp, monkeypatch, cand)

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == wp.GH_BIN:
                return _RR(wp, 0, _gh_body(args, "MERGED", "h"))
            return _RR(wp, 0, "")

        _d, info = wp.run_prune(NOW, apply=False, run=run, clone_parents=["/cp"],
                                roots=["/r"], self_path="/elsewhere")
        assert info["summary"]["gh_failed"] == 0


class TestRemoteConfirmation:
    def _det(self, wp):
        return wp.Candidate(path="/wt", head="X", branch=None, detached=True,
                            clone="/clone")

    def _run(self, wp, server_out, server_rc=0, local_sha="cached"):
        def run(args, cwd=None):
            if "for-each-ref" in args:
                if wp.PROOF_MAIN_REF in args or wp.PROOF_TAG_NAMESPACE in args:
                    return _RR(wp, 0, "")
                return _RR(wp, 0, "refs/remotes/origin/gw/feat\n")
            if "ls-remote" in args:
                return _RR(wp, server_rc, server_out)
            if "rev-parse" in args:
                return _RR(wp, 0, local_sha + "\n")
            return _RR(wp, 0, "")
        return run

    def test_matching_sha_confirms(self, wp):
        run = self._run(wp, "cached\trefs/heads/gw/feat\n")
        assert wp.gate_contained(run, self._det(wp)) == (True, None)

    def test_force_push_is_caught(self, wp):
        run = self._run(wp, "rewritten\trefs/heads/gw/feat\n")
        assert wp.gate_contained(run, self._det(wp)) == (False, "remote_unconfirmed")

    def test_branch_deleted_server_side_is_caught(self, wp):
        run = self._run(wp, "")
        assert wp.gate_contained(run, self._det(wp)) == (False, "remote_unconfirmed")

    def test_ls_remote_failure_is_signal_not_noise(self, wp):
        run = self._run(wp, "", server_rc=1)
        assert wp.gate_contained(run, self._det(wp)) == (False, "remote_unconfirmed")

    @pytest.mark.parametrize("ref,expected", [
        ("refs/remotes/origin/feat", ("origin", "feat")),
        ("refs/remotes/origin/gw/feature-slug", ("origin", "gw/feature-slug")),
        ("refs/remotes/upstream/bot/tkt-8696-x", ("upstream", "bot/tkt-8696-x")),
        ("refs/heads/feat", None),
    ])
    def test_remote_and_branch_split_on_the_first_slash(self, wp, ref, expected):
        assert wp._remote_and_branch(ref) == expected

    def test_head_pseudo_ref_is_never_used_as_proof(self, wp):
        def run(args, cwd=None):
            if "for-each-ref" in args:
                if wp.PROOF_REMOTE_NAMESPACE in args:
                    return _RR(wp, 0, "refs/remotes/origin/HEAD\n")
                return _RR(wp, 0, "")
            return _RR(wp, 0, "")
        assert wp.gate_contained(run, self._det(wp)) == (False, "uncontained")


class TestForensics:
    def test_removal_records_head_and_reflog(self, wp, monkeypatch, repo, tmp_path):
        wt = tmp_path / "w"
        _git(repo, "worktree", "add", "-q", "-b", "feat", str(wt))
        (wt / "u.txt").write_text("u")
        _git(wt, "add", "u.txt")
        _git(wt, "commit", "-qm", "work")
        head = _git(wt, "rev-parse", "HEAD").stdout.strip()
        _git(wt, "reset", "--hard", "HEAD~1")
        after = _git(wt, "rev-parse", "HEAD").stdout.strip()

        cand = wp.Candidate(path=str(wt), head=after, branch="feat",
                            detached=False, clone=str(repo))
        shas = wp._worktree_reflog_shas(_real_run(wp), cand)
        assert head in shas, "the reset-away commit must be recorded before removal"
        assert after in shas

    def test_last_scan_snapshot_names_every_candidate_and_reason(self, wp, monkeypatch):
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        monkeypatch.setattr(wp, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda r: [])

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == wp.GH_BIN:
                return _RR(wp, 0, "[]")
            if "merge-base" in args:
                return _RR(wp, 1)
            return _RR(wp, 0, "")

        wp.run_prune(NOW, apply=False, run=run, clone_parents=["/cp"], roots=["/r"],
                     self_path="/elsewhere")
        snap = json.loads((wp.WT_DIR / "last-scan.json").read_text())
        row = {c["path"]: c for c in snap["candidates"]}["/wt"]
        assert row["reason"] == "unmerged"
        assert set(row) >= {"path", "action", "reason", "failed_gates", "branch",
                            "detached", "clone", "head", "locked", "reflog"}
        assert "merged" in row["failed_gates"]
        assert snap["summary"]["gh_failed"] == 0


class TestCapabilityProbe:
    def test_tokens_derive_from_the_functions_that_implement_them(self, wp):
        assert wp.capabilities() == [t for t, _ in wp.CAPABILITY_IMPLS]

    def test_a_missing_implementation_drops_its_token(self, wp, monkeypatch):
        monkeypatch.delitem(wp.__dict__, "load_keeplist_text")
        assert "keeplist" not in wp.capabilities()


INSTALLER = REPO_ROOT / "deploy" / "scripts" / "worktree-prune-install.sh"


def _render_plist(tmp_path, env_extra):
    home = tmp_path / "home"
    (home / ".claude" / "scripts").mkdir(parents=True, exist_ok=True)
    (home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
    real = REPO_ROOT / "deploy" / "scripts" / "worktree_prune.py"
    (home / ".claude" / "scripts" / "worktree_prune.py").write_text(real.read_text())
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / "launchctl").write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / "launchctl").chmod(0o755)
    inherited = {k: v for k, v in os.environ.items()
                 if not k.startswith("WORKTREE_PRUNE_")}
    env = dict(inherited, HOME=str(home),
               PATH=f"{fake_bin}:{os.environ['PATH']}", **env_extra)
    proc = _sp.run(["bash", str(INSTALLER)], capture_output=True, text=True, env=env)
    plists = list((home / "Library" / "LaunchAgents").glob("*.plist"))
    return proc, (plists[0].read_text() if plists else None)


def _plist_program_args(text):
    import re
    block = re.search(r"<key>ProgramArguments</key>\s*<array>(.*?)</array>",
                      text, re.S).group(1)
    return re.findall(r"<string>(.*?)</string>", block)


class TestInstaller:
    def test_default_install_applies(self, tmp_path):
        _proc, plist = _render_plist(tmp_path, {})
        assert plist is not None
        assert "--apply" in _plist_program_args(plist)

    def test_no_apply_mode_omits_the_flag(self, tmp_path):
        _proc, plist = _render_plist(tmp_path, {"WORKTREE_PRUNE_INSTALL_APPLY": "0"})
        assert plist is not None
        assert "--apply" not in _plist_program_args(plist)

    def test_gh_binary_is_passed_as_an_env_var_not_a_path_prefix(self, tmp_path):
        _proc, plist = _render_plist(tmp_path, {"WORKTREE_PRUNE_GH": "/opt/mygh"})
        assert "<key>WORKTREE_PRUNE_GH</key>" in plist
        assert "<string>/opt/mygh</string>" in plist

    def test_hold_file_is_created_and_never_overwritten(self, tmp_path):
        home = tmp_path / "home"
        _proc, _plist = _render_plist(tmp_path, {})
        keep = home / ".claude" / "dockwright" / "worktree-prune" / "keep.txt"
        assert keep.is_file()
        keep.write_text("/my/hold\n")
        _render_plist(tmp_path, {})
        assert keep.read_text() == "/my/hold\n"

    def test_install_is_refused_over_a_script_without_the_keeplist(self, tmp_path):
        home = tmp_path / "home"
        (home / ".claude" / "scripts").mkdir(parents=True)
        (home / "Library" / "LaunchAgents").mkdir(parents=True)
        (home / ".claude" / "scripts" / "worktree_prune.py").write_text(
            '#!/usr/bin/env python3\n"""Old pruner. Mentions keep.txt and keeplist."""\n'
            'import sys\nsys.exit(0)\n')
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        (fake_bin / "launchctl").write_text("#!/bin/sh\nexit 0\n")
        (fake_bin / "launchctl").chmod(0o755)
        env = dict(os.environ, HOME=str(home),
                   PATH=f"{fake_bin}:{os.environ['PATH']}")
        proc = _sp.run(["bash", str(INSTALLER)], capture_output=True, text=True, env=env)
        assert proc.returncode != 0
        assert "keeplist" in proc.stderr
        assert not list((home / "Library" / "LaunchAgents").glob("*.plist"))

    def test_gh_binding_survives_a_bare_re_run(self, tmp_path):
        _proc, plist = _render_plist(tmp_path, {"WORKTREE_PRUNE_GH": "/opt/mygh"})
        assert "<string>/opt/mygh</string>" in plist
        proc2, plist2 = _render_plist(tmp_path, {})
        assert "<key>WORKTREE_PRUNE_GH</key>" in plist2, \
            "a bare re-run must preserve the binding, not silently drop it"
        assert "<string>/opt/mygh</string>" in plist2
        assert "Preserving WORKTREE_PRUNE_GH" in proc2.stdout

    def test_missing_gh_binding_warns_loudly(self, tmp_path):
        proc, _plist = _render_plist(tmp_path, {})
        assert "WORKTREE_PRUNE_GH is unset" in proc.stderr


class TestArtifactLaundering:

    @pytest.mark.parametrize("entry", [
        "docs/target/notes.md",
        "notes/out/plan.md",
        "src/dist/README-IMPORTANT.md",
        "docs/build/spec-v2.md",
        "planning/coverage/decisions.md",
        "docs/sidecar/specs/design.md",
    ])
    def test_a_middle_artifact_segment_does_not_launder_the_path(self, wp, entry):
        assert wp._is_artifact_path(entry) is False
        assert wp.ignored_ok_from_porcelain(f"!! {entry}\n") is False

    @pytest.mark.parametrize("entry", [
        "target/", "common/target/", "service/target/", "target/classes/Main.class",
        ".claude/", ".claude/settings.local.json", "node_modules/",
        "packages/web/node_modules/", "service/x.egg-info/", ".DS_Store",
    ])
    def test_real_artifact_shapes_still_pass(self, wp, entry):
        assert wp._is_artifact_path(entry) is True

    def test_only_the_first_or_last_segment_is_consulted(self, wp):
        assert wp._is_artifact_path("target/x/y") is True
        assert wp._is_artifact_path("x/y/target") is True
        assert wp._is_artifact_path("x/target/y") is False


class TestBranchDeleteProof:

    CASES = [
        ("merged at this head", "MERGED", "h", True),
        ("merged at another SHA", "MERGED", "other", False),
        ("closed, never merged", "CLOSED", "h", False),
    ]

    def _drive(self, wp, monkeypatch, state, head_oid, ancestor=False):
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        monkeypatch.setattr(wp, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda r: [])
        calls = []

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            calls.append(args)
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == wp.GH_BIN:
                if state is None:
                    return _RR(wp, 0, "[]" if "list" in args else "")
                return _RR(wp, 0, _gh_body(args, state, head_oid))
            if "merge-base" in args:
                return _RR(wp, 0 if ancestor else 1)
            if "status" in args:
                return _RR(wp, 0, "")
            if "rev-parse" in args:
                return _RR(wp, 0, "h\n")
            return _RR(wp, 0)

        decision, info = wp.run_prune(NOW, apply=True, run=run, clone_parents=["/cp"],
                                      roots=["/r"], self_path="/elsewhere")
        return calls, info

    @pytest.mark.parametrize("label,state,oid,expect_delete", CASES)
    def test_pr_state_and_head_sha_decide_removal_and_branch_deletion(
            self, wp, monkeypatch, label, state, oid, expect_delete):
        calls, info = self._drive(wp, monkeypatch, state, oid)
        assert info["summary"]["removed"] == (1 if expect_delete else 0), \
            f"{label}: removal needs the same merged head as branch -D"
        deleted = any(_is_branch_delete(a) for a in calls)
        assert deleted is expect_delete, f"{label}: branch -D should be {expect_delete}"

    def test_a_closed_pr_keeps_the_tree_and_the_branch(self, wp, monkeypatch):
        calls, info = self._drive(wp, monkeypatch, "CLOSED", "h")
        assert info["summary"]["removed"] == 0
        assert {r["path"]: r for r in info["results"]}["/wt"]["reason"] == "unmerged"
        assert not any(_is_branch_delete(a) for a in calls)
        assert not any("remove" in a for a in calls)

    def test_no_pr_but_ancestor_still_deletes(self, wp, monkeypatch):
        calls, _info = self._drive(wp, monkeypatch, None, None, ancestor=True)
        assert any(_is_branch_delete(a) for a in calls)

    def test_no_pr_and_not_ancestor_removes_nothing(self, wp, monkeypatch):
        _calls, info = self._drive(wp, monkeypatch, None, None, ancestor=False)
        assert info["summary"]["removed"] == 0

    def test_gate_merged_is_reachable_from_production(self, wp, monkeypatch):
        seen = []
        real = wp.gate_merged
        monkeypatch.setattr(wp, "gate_merged",
                            lambda run, cand: (seen.append(cand.path), real(run, cand))[1])
        self._drive(wp, monkeypatch, "MERGED", "h")
        assert seen, "gate_merged must be consulted before `branch -D`"

    def test_gate_merged_uses_the_named_gh_binary(self, wp, monkeypatch):
        monkeypatch.setattr(wp, "GH_BIN", "/opt/distinct-gh")
        seen = {}

        def run(args, cwd=None):
            seen.setdefault("argv", args)
            return _RR(wp, 1)

        wp.gate_merged(run, _cand(wp, branch="feat", head="h", clone="/c"))
        assert seen["argv"][0] == "/opt/distinct-gh"


class TestKeepListHardening:
    def test_a_glob_that_matches_nothing_stops_the_run(self, wp, tmp_path):
        (tmp_path / "worktrees" / "TKT-9855").mkdir(parents=True)
        entries, fatal = wp.load_keeplist_text(str(tmp_path / "worktrees" / "TKT-9855-*"))
        assert fatal == "keeplist_entry_missing"
        assert entries is None

    def test_a_glob_that_matches_is_accepted(self, wp, tmp_path):
        (tmp_path / "worktrees" / "TKT-9855").mkdir(parents=True)
        entries, fatal = wp.load_keeplist_text(str(tmp_path / "worktrees" / "TKT-98*"))
        assert fatal is None
        assert wp.keeplist_matches(entries, str(tmp_path / "worktrees" / "TKT-9855"))

    def test_wrong_case_directory_hold_still_covers_children(self, wp, tmp_path):
        held = tmp_path / "worktrees" / "tkt-9855"
        held.mkdir(parents=True)
        entries, fatal = wp.load_keeplist_text(str(held))
        assert fatal is None
        child = tmp_path / "worktrees" / "TKT-9855" / "acme-communication-service"
        assert wp.keeplist_matches(entries, str(child)) is True


class TestReflogIdentity:
    def test_reflog_is_read_from_the_worktrees_own_gitdir(self, wp, repo, tmp_path):
        a = tmp_path / "t1" / "repo"
        b = tmp_path / "t2" / "repo"
        a.parent.mkdir(parents=True)
        b.parent.mkdir(parents=True)
        _git(repo, "worktree", "add", "-q", "-b", "fa", str(a))
        _git(repo, "worktree", "add", "-q", "-b", "fb", str(b))
        for wt, name in ((a, "alpha"), (b, "beta")):
            (wt / f"{name}.txt").write_text(name)
            _git(wt, "add", f"{name}.txt")
            _git(wt, "commit", "-qm", f"commit {name}")

        run = _real_run(wp)
        ca = wp.Candidate(path=str(a), head="", branch="fa", detached=False,
                          clone=str(repo))
        cb = wp.Candidate(path=str(b), head="", branch="fb", detached=False,
                          clone=str(repo))
        sa = set(wp._worktree_reflog_shas(run, ca))
        sb = set(wp._worktree_reflog_shas(run, cb))
        assert sa and sb, "both worktrees must yield their own reflog"
        head_a = _git(a, "rev-parse", "HEAD").stdout.strip()
        head_b = _git(b, "rev-parse", "HEAD").stdout.strip()
        assert head_a in sa and head_a not in sb
        assert head_b in sb and head_b not in sa


class TestGhFailedSurfaces:
    def _broken_gh_run(self, wp):
        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == wp.GH_BIN:
                return _RR(wp, 1, "", "GraphQL: Could not resolve to a Repository")
            if "merge-base" in args:
                return _RR(wp, 1)
            return _RR(wp, 0, "")
        return run

    def test_gh_failed_reaches_check_log_and_last_scan(self, wp, monkeypatch, capsys):
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        monkeypatch.setattr(wp, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda r: [])
        wp.run_prune(NOW, apply=False, run=self._broken_gh_run(wp),
                     clone_parents=["/cp"], roots=["/r"], self_path="/elsewhere")

        check = wp.CHECK_LOG_PATH.read_text()
        assert '"gh_failed": 1' in check, "check.log must not report 0 here"
        snap = json.loads((wp.WT_DIR / "last-scan.json").read_text())
        assert snap["summary"]["gh_failed"] == 1

    def test_stdout_line_carries_gh_failed(self, wp, monkeypatch, capsys):
        monkeypatch.setattr(wp, "run_prune",
                            lambda *a, **k: ("dry-run", {"results": [], "summary": {
                                "scanned": 1, "would_remove": 0, "removed": 0,
                                "skipped": 1, "capped": 0, "gh_failed": 7}}))
        wp.main([])
        assert "gh_failed=7" in capsys.readouterr().out


class TestSubmoduleIgnoredContent:
    def test_ignored_file_inside_a_submodule_blocks_removal(self, wp, tmp_path):
        up = tmp_path / "sub-origin"
        up.mkdir()
        _git(up, "init", "-q", "-b", "main", ".")
        (up / "s.txt").write_text("s")
        (up / ".gitignore").write_text("secret-notes/\n")
        _git(up, "add", "-A")
        _git(up, "commit", "-qm", "sub base")

        super_ = tmp_path / "super"
        super_.mkdir()
        _git(super_, "init", "-q", "-b", "main", ".")
        (super_ / "a.txt").write_text("a")
        _git(super_, "add", "a.txt")
        _git(super_, "commit", "-qm", "base")
        _git(super_, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
             str(up), "sub")
        _git(super_, "commit", "-qm", "add submodule")

        wt = tmp_path / "wt"
        _git(super_, "worktree", "add", "-q", "-b", "feat", str(wt))
        _git(wt, "-c", "protocol.file.allow=always", "submodule", "update",
             "--init", "--recursive")
        notes = wt / "sub" / "secret-notes"
        notes.mkdir(parents=True)
        (notes / "design.md").write_text("the only copy")

        cand = wp.Candidate(path=str(wt), head="", branch="feat", detached=False,
                            clone=str(super_))
        run = _real_run(wp)
        top = wp._porcelain(run, cand)
        assert wp.ignored_ok_from_porcelain(top) is True, \
            "precondition: the superproject cannot see it"
        assert wp.gate_ignored(run, cand) is False, \
            "the submodule sweep is what must catch it"

    def test_the_scan_path_also_consults_the_submodule_sweep(self, wp, monkeypatch):
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        monkeypatch.setattr(wp, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda r: [])

        def run(args, cwd=None):
            if "submodule" in args:
                return _RR(wp, 0, "!! sub/secret-notes/\n")
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == wp.GH_BIN:
                return _RR(wp, 0, _gh_body(args, "MERGED", "h"))
            if "status" in args:
                return _RR(wp, 0, "")
            if "remove" in args:
                raise AssertionError("submodule-hidden content must block removal")
            return _RR(wp, 0, "")

        _d, info = wp.run_prune(NOW, apply=True, run=run, clone_parents=["/cp"],
                                roots=["/r"], self_path="/elsewhere")
        row = {r["path"]: r for r in info["results"]}["/wt"]
        assert (row["action"], row["reason"]) == ("SKIP", "ignored_content")

    def test_submodule_sweep_failure_blocks(self, wp):
        def run(args, cwd=None):
            if "submodule" in args:
                return _RR(wp, 128, "", "boom")
            return _RR(wp, 0, "")
        assert wp.gate_ignored(run, _cand(wp, path="/wt")) is False


class TestPostRemovalBlindness:

    def _cwd_honouring_run(self, wp, removed, state="MERGED", oid="h"):
        def run(args, cwd=None):
            if cwd is not None and cwd in removed:
                return _RR(wp, 1, "", "[Errno 2] No such file or directory")
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == wp.GH_BIN:
                return _RR(wp, 0, _gh_body(args, state, oid))
            if "merge-base" in args:
                return _RR(wp, 1)
            if "status" in args:
                return _RR(wp, 0, "")
            if "rev-parse" in args:
                return _RR(wp, 0, "h\n")
            if "remove" in args:
                removed.add("/wt")
                return _RR(wp, 0)
            return _RR(wp, 0)
        return run

    def test_squash_merged_branch_is_still_deleted_after_removal(self, wp, monkeypatch):
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        monkeypatch.setattr(wp, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda r: [])
        removed = set()
        calls = []
        base = self._cwd_honouring_run(wp, removed)

        def run(args, cwd=None):
            calls.append(args)
            return base(args, cwd)

        _d, info = wp.run_prune(NOW, apply=True, run=run, clone_parents=["/cp"],
                                roots=["/r"], self_path="/elsewhere")
        assert info["summary"]["removed"] == 1
        assert any(_is_branch_delete(a) for a in calls), \
            "the merge proof must be evaluated while the worktree still exists"

    def test_a_closed_pr_tree_is_kept_with_a_cwd_honouring_run(self, wp, monkeypatch):
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        monkeypatch.setattr(wp, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda r: [])
        removed = set()
        calls = []
        base = self._cwd_honouring_run(wp, removed, state="CLOSED")

        def run(args, cwd=None):
            calls.append(args)
            return base(args, cwd)

        _d, info = wp.run_prune(NOW, apply=True, run=run, clone_parents=["/cp"],
                                roots=["/r"], self_path="/elsewhere")
        assert info["summary"]["removed"] == 0
        assert not any(_is_branch_delete(a) for a in calls)

    def test_removed_rows_keep_the_pre_removal_reflog(self, wp, monkeypatch, repo, tmp_path):
        wt = tmp_path / "victim"
        _git(repo, "worktree", "add", "-q", "-b", "feat", str(wt))
        (wt / "u.txt").write_text("u")
        _git(wt, "add", "u.txt")
        _git(wt, "commit", "-qm", "work that gets reset away")
        lost = _git(wt, "rev-parse", "HEAD").stdout.strip()
        _git(wt, "reset", "--hard", "HEAD~1")
        head = _git(wt, "rev-parse", "HEAD").stdout.strip()

        cand = wp.Candidate(path=str(wt), head=head, branch="feat",
                            detached=False, clone=str(repo))
        monkeypatch.setattr(wp, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda r: [])
        gitreal = _real_run(wp)

        def run(args, cwd=None):
            if args and args[0] == wp.GH_BIN:
                return _RR(wp, 0, _gh_body(args, "MERGED", head))
            if "fetch" in args:
                return _RR(wp, 0)
            if "merge-base" in args:
                return _RR(wp, 0)
            return gitreal(args, cwd)

        _d, info = wp.run_prune(NOW, apply=True, run=run, clone_parents=["/cp"],
                                roots=["/r"], self_path="/elsewhere")
        row = {r["path"]: r for r in info["results"]}[str(wt)]
        assert row["action"] == "REMOVED", f"gates: {row.get('failed_gates')}"
        assert lost in row["reflog"], \
            "the reset-away SHA is the whole point of the record"
        snap = json.loads((wp.WT_DIR / "last-scan.json").read_text())
        assert lost in {c["path"]: c for c in snap["candidates"]}[str(wt)]["reflog"]

    def test_the_null_sha_is_not_recorded(self, wp, repo, tmp_path):
        wt = tmp_path / "w"
        _git(repo, "worktree", "add", "-q", "-b", "f", str(wt))
        cand = wp.Candidate(path=str(wt), head="", branch="f", detached=False,
                            clone=str(repo))
        shas = wp._worktree_reflog_shas(_real_run(wp), cand)
        assert shas, "precondition: the reflog is non-empty"
        assert "0" * 40 not in shas

    def test_a_failed_removal_still_records_the_shas(self, wp, monkeypatch):
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        monkeypatch.setattr(wp, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda r: [])
        monkeypatch.setattr(wp, "_worktree_reflog_shas", lambda r, c: ["deadbeef" * 5])

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == wp.GH_BIN:
                return _RR(wp, 0, _gh_body(args, "MERGED", "h"))
            if "status" in args:
                return _RR(wp, 0, "")
            if "rev-parse" in args:
                return _RR(wp, 0, "h\n")
            if "remove" in args:
                return _RR(wp, 1, "", "could not remove")
            return _RR(wp, 0, "")

        wp.run_prune(NOW, apply=True, run=run, clone_parents=["/cp"], roots=["/r"],
                     self_path="/elsewhere")
        events = _ledger_events(wp)
        failed = [e for e in events if e["event"] == "remove_failed"]
        assert failed and failed[0].get("reflog") == ["deadbeef" * 5]
        assert failed[0].get("head") == "h"


class TestIgnoredChokePoint:

    def _drive(self, wp, monkeypatch, sub_out="", sub_rc=0, apply=True,
               at_apply_only=False, status_at_apply=None):
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        monkeypatch.setattr(wp, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda r: [])
        seen = {"sub": 0, "status": 0}

        def run(args, cwd=None):
            if "submodule" in args:
                seen["sub"] += 1
                if at_apply_only and seen["sub"] == 1:
                    return _RR(wp, 0, "")
                return _RR(wp, sub_rc, sub_out)
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == wp.GH_BIN:
                return _RR(wp, 0, _gh_body(args, "MERGED", "h"))
            if "status" in args:
                seen["status"] += 1
                if status_at_apply is not None and seen["status"] > 1:
                    return _RR(wp, 0, status_at_apply)
                return _RR(wp, 0, "")
            if "rev-parse" in args:
                return _RR(wp, 0, "h\n")
            if "remove" in args:
                raise AssertionError("must not remove: the ignored gate blocked")
            return _RR(wp, 0, "")

        return wp.run_prune(NOW, apply=apply, run=run, clone_parents=["/cp"],
                            roots=["/r"], self_path="/elsewhere")

    def test_superproject_content_appearing_in_the_window_blocks(self, wp, monkeypatch):
        _d, info = self._drive(
            wp, monkeypatch,
            status_at_apply="!! docs/sidecar/specs/x-design.md\n")
        row = {r["path"]: r for r in info["results"]}["/wt"]
        assert row["action"] == "SKIP"
        assert row["reason"] == "toctou_ignored_content"
        assert info["summary"]["removed"] == 0

    @pytest.mark.parametrize("apply", [False, True])
    def test_submodule_content_blocks_in_both_arms(self, wp, monkeypatch, apply):
        _d, info = self._drive(wp, monkeypatch, "!! sub/notes/\n", apply=apply)
        row = {r["path"]: r for r in info["results"]}["/wt"]
        assert (row["action"], row["reason"]) == ("SKIP", "ignored_content")

    def test_content_appearing_in_the_scan_to_apply_window_blocks(self, wp, monkeypatch):
        _d, info = self._drive(wp, monkeypatch, "!! sub/notes/\n", at_apply_only=True)
        row = {r["path"]: r for r in info["results"]}["/wt"]
        assert row["action"] == "SKIP"
        assert row["reason"].startswith("toctou_")
        assert info["summary"]["removed"] == 0

    def test_a_broken_sweep_is_not_reported_as_successful_protection(self, wp, monkeypatch):
        _d, info = self._drive(wp, monkeypatch, "", sub_rc=1, apply=False)
        row = {r["path"]: r for r in info["results"]}["/wt"]
        assert row["reason"] == "submodule_sweep_failed"
        assert info["summary"]["by_reason"].get("ignored_content") is None

    def test_the_two_failure_causes_do_not_share_a_reason(self, wp, monkeypatch):
        _d, found = self._drive(wp, monkeypatch, "!! sub/notes/\n", apply=False)
        _d2, broke = self._drive(wp, monkeypatch, "", sub_rc=1, apply=False)
        r1 = {r["path"]: r for r in found["results"]}["/wt"]["reason"]
        r2 = {r["path"]: r for r in broke["results"]}["/wt"]["reason"]
        assert r1 != r2

    def test_every_caller_routes_through_the_choke_point(self, wp, monkeypatch):
        calls = []
        real = wp.check_ignored
        monkeypatch.setattr(wp, "check_ignored",
                            lambda *a, **k: (calls.append(1), real(*a, **k))[1])
        self._drive(wp, monkeypatch, "!! sub/x/\n", apply=False)
        assert calls, "the scan must consult check_ignored"


class TestHoldListDocsMatchTheRule:
    def test_the_shipped_template_does_not_teach_a_loop_stopping_example(self, wp, tmp_path):
        _proc, _plist = _render_plist(tmp_path, {})
        keep = tmp_path / "home" / ".claude" / "dockwright" / "worktree-prune" / "keep.txt"
        body = keep.read_text()
        live = [l.strip() for l in body.splitlines()
                if l.strip() and not l.strip().startswith("#")]
        assert live == [], "the shipped template must be comments only"
        entries, fatal = wp.load_keeplist_text(body)
        assert (entries, fatal) == ([], None), \
            "the shipped template must not stop the loop as written"


class TestFailedGatesFidelity:

    def _row(self, wp, monkeypatch, gates, cand=None):
        cand = cand or _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        monkeypatch.setattr(wp, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda r: [])
        monkeypatch.setattr(wp, "_porcelain", lambda r, c: "")
        monkeypatch.setattr(wp, "clean_from_porcelain", lambda x: gates["clean"])
        monkeypatch.setattr(wp, "check_ignored",
                            lambda r, c, text=None: gates["ignored_ok"])
        monkeypatch.setattr(wp, "gate_in_progress",
                            lambda r, c: gates["in_progress_clear"])
        monkeypatch.setattr(wp, "gate_contained", lambda r, c: gates["contained"])
        monkeypatch.setattr(wp, "gate_merged_head",
                            lambda r, c, s=None: gates["merged"])
        monkeypatch.setattr(wp, "gate_merged", lambda r, c: True)
        monkeypatch.setattr(wp, "gate_unowned",
                            lambda c, a, l, s: gates["unowned"])
        monkeypatch.setattr(wp, "gate_old_enough",
                            lambda r, c, n, d: gates["old_enough"])
        monkeypatch.setattr(wp, "gate_no_open_work",
                            lambda c, w, r: gates["no_open_work"])

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            return rr if rr is not None else _RR(wp, 0, "")

        _d, info = wp.run_prune(NOW, apply=False, run=run, clone_parents=["/cp"],
                                roots=["/r"], self_path="/elsewhere")
        return {r["path"]: r for r in info["results"]}["/wt"]

    def _pass_all(self):
        return {"clean": True, "ignored_ok": (True, None), "in_progress_clear": True,
                "contained": (True, None), "merged": (True, None), "unowned": True,
                "old_enough": (True, None), "no_open_work": (True, None)}

    @pytest.mark.parametrize("key", sorted(set(ALL_PASS) - {"kept"}))
    def test_every_failing_gate_appears_in_failed_gates(self, wp, monkeypatch, key):
        gates = self._pass_all()
        cand = None
        if key == "contained":
            cand = wp.Candidate(path="/wt", head="h", branch=None, detached=True,
                                clone="/clone")
            gates["contained"] = (False, "uncontained")
        elif key == "not_locked":
            cand = wp.Candidate(path="/wt", head="h", branch="feat", detached=False,
                                clone="/clone", locked=True)
        elif isinstance(gates.get(key), tuple):
            gates[key] = (False, "ignored_content")
        else:
            gates[key] = False
        row = self._row(wp, monkeypatch, gates, cand)
        assert key in row["failed_gates"], \
            f"{key} failed but failed_gates says it passed: {row['failed_gates']}"

    def test_a_skip_row_never_has_an_empty_failed_gates(self, wp, monkeypatch):
        gates = self._pass_all()
        gates["ignored_ok"] = (False, "submodule_sweep_failed")
        row = self._row(wp, monkeypatch, gates)
        assert row["action"] == "SKIP"
        assert row["failed_gates"], "a SKIP must name at least one failing gate"

    def test_reason_and_failed_gates_cannot_disagree(self, wp, monkeypatch):
        gates = self._pass_all()
        gates["ignored_ok"] = (False, "ignored_content")
        gates["unowned"] = False
        row = self._row(wp, monkeypatch, gates)
        assert row["reason"] == "ignored_content"
        assert "ignored_ok" in row["failed_gates"]
        assert "unowned" in row["failed_gates"]

    def test_contained_has_a_meaningful_default_reason(self, wp):
        defaults = dict(wp.DECIDE_ORDER)
        assert defaults["contained"] == "uncontained"
        assert wp.decide(_cand(wp), {**{k: True for k, _ in wp.DECIDE_ORDER},
                                     "kept": False,
                                     "contained": (False, None)}) == ("SKIP", "uncontained")


HAS_BIRTHTIME = hasattr(os.stat(os.sep), "st_birthtime")
needs_birthtime = pytest.mark.skipif(
    not HAS_BIRTHTIME,
    reason="filesystem reports no st_birthtime; the reflog is the only clock here, "
           "which is the accepted-freeze case covered by its own tests below")


REFLOG_LINE = ("{zero} 1ed74f16c66dbe9f917e3eceb45d6d3452c2f487 "
               "Test User With Spaces <t@t> {ts} +0000\n")


def _admin_dir(tmp_path, name="wt-admin", reflog_ts=None, reflog_text=None):
    gd = tmp_path / "admin" / name
    (gd / "logs").mkdir(parents=True)
    if reflog_text is not None:
        body = reflog_text
    elif reflog_ts is None:
        body = ""
    else:
        body = REFLOG_LINE.format(zero="0" * 40, ts=int(reflog_ts))
    (gd / "logs" / "HEAD").write_text(body)
    return gd


def _age_run(wp, gitdir, porcelain="", pr_state="MERGED", head="h"):
    def run(args, cwd=None):
        if "--absolute-git-dir" in args:
            return _RR(wp, 0, f"{gitdir}\n") if gitdir is not None else _RR(wp, 128)
        rr = _infra_rr(wp, args)
        if rr is not None:
            return rr
        if "fetch" in args:
            return _RR(wp, 0)
        if args[0] == wp.GH_BIN:
            return _RR(wp, 0, _gh_body(args, pr_state, head))
        if "status" in args:
            return _RR(wp, 0, porcelain)
        if "rev-parse" in args:
            return _RR(wp, 0, f"{head}\n")
        if "remove" in args:
            return _RR(wp, 0)
        if _is_branch_delete(args):
            return _RR(wp, 0)
        if "merge-base" in args:
            return _RR(wp, 0)
        raise AssertionError(f"unexpected run: {args}")
    return run


class TestAgeGateIncidentShape:
    def _wire(self, wp_clock, monkeypatch, tmp_path, age_seconds, apply=False):
        wt = tmp_path / "worktrees" / "example-branch"
        wt.mkdir(parents=True)
        born = _time.time()
        gd = _admin_dir(tmp_path, reflog_ts=born)
        cand = wp_clock.Candidate(path=str(wt), head="h",
                                  branch="gw/example-branch",
                                  detached=False, clone="/clone")
        monkeypatch.setattr(wp_clock, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp_clock, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp_clock, "_collect_lsof_cwds", lambda r: [])
        calls = []
        base = _age_run(wp_clock, gd)

        def run(args, cwd=None):
            calls.append(args)
            return base(args, cwd)

        decision, info = wp_clock.run_prune(born + age_seconds, apply=apply, run=run,
                                      clone_parents=["/cp"], roots=["/r"],
                                      self_path="/elsewhere")
        return info, calls

    def test_a_fourteen_minute_old_clean_tree_with_no_pr_survives(self, wp_clock, monkeypatch, tmp_path):
        info, _calls = self._wire(wp_clock, monkeypatch, tmp_path, 14 * 60)
        row = info["results"][0]
        assert row["action"] == "SKIP"
        assert row["reason"] == "too_young"
        assert info["summary"]["would_remove"] == 0
        assert info["summary"]["by_reason"] == {"too_young": 1}

    def test_the_incident_row_now_names_the_gate_instead_of_an_empty_list(
            self, wp_clock, monkeypatch, tmp_path):
        info, _calls = self._wire(wp_clock, monkeypatch, tmp_path, 14 * 60)
        assert info["results"][0]["failed_gates"] == ["old_enough"]

    def test_apply_neither_removes_the_tree_nor_deletes_the_branch(
            self, wp_clock, monkeypatch, tmp_path):
        info, calls = self._wire(wp_clock, monkeypatch, tmp_path, 14 * 60, apply=True)
        assert info["summary"]["removed"] == 0
        assert not any("remove" in a for a in calls), "a 14-minute-old tree was removed"
        assert not any(_is_branch_delete(a) for a in calls), "its branch was deleted"
        assert not [e for e in _ledger_events(wp_clock) if e["event"] == "removed"]

    def test_the_same_tree_past_the_window_is_still_prunable(
            self, wp_clock, monkeypatch, tmp_path):
        info, _calls = self._wire(wp_clock, monkeypatch, tmp_path, 30 * 86400)
        assert info["results"][0]["action"] == "WOULD-REMOVE"
        assert info["summary"]["would_remove"] == 1


class TestAgeClock:
    """worktree_born_at reads the worktree's git admin dir. Both sources are
    real files on disk here; only the returned instant is asserted."""

    def _cand_for(self, wp_clock, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir(exist_ok=True)
        return wp_clock.Candidate(path=str(wt), head="h", branch="feat",
                                  detached=False, clone="/clone")

    def test_reads_the_first_reflog_entry_through_an_author_name_with_spaces(
            self, wp_clock, tmp_path):
        gd = _admin_dir(tmp_path, reflog_ts=1_788_745_457)
        assert wp_clock._reflog_first_timestamp(str(gd)) == 1_788_745_457.0

    def test_the_reflog_is_used_when_it_is_the_later_of_the_two(
            self, wp_clock, tmp_path):
        gd = _admin_dir(tmp_path, reflog_ts=None)
        later = int((wp_clock._admin_birthtime(str(gd)) or 0) + 7200)
        (gd / "logs" / "HEAD").write_text(
            REFLOG_LINE.format(zero="0" * 40, ts=later))
        cand = self._cand_for(wp_clock, tmp_path)
        got = wp_clock.worktree_born_at(_age_run(wp_clock, gd), cand)
        assert got == pytest.approx(later)

    @needs_birthtime
    def test_an_emptied_reflog_falls_through_to_the_admin_dir_birthtime(
            self, wp_clock, tmp_path):
        gd = _admin_dir(tmp_path, reflog_ts=None)
        assert (gd / "logs" / "HEAD").read_text() == ""
        cand = self._cand_for(wp_clock, tmp_path)
        got = wp_clock.worktree_born_at(_age_run(wp_clock, gd), cand)
        assert got == pytest.approx(wp_clock._admin_birthtime(str(gd)))

    @pytest.mark.skipif(HAS_BIRTHTIME, reason="this filesystem has st_birthtime")
    def test_without_birthtime_an_emptied_reflog_leaves_the_age_unknown(
            self, wp_clock, tmp_path):
        gd = _admin_dir(tmp_path, reflog_ts=None)
        cand = self._cand_for(wp_clock, tmp_path)
        assert wp_clock.worktree_born_at(_age_run(wp_clock, gd), cand) is None
        assert wp_clock.gate_old_enough(_age_run(wp_clock, gd), cand,
                                        1_700_000_000, 14) == (False, "age_unknown"), \
            "the documented accepted freeze: no birthtime and a gc-emptied reflog " \
            "means the tree is never prunable, which must refuse rather than allow"

    def test_a_corrupt_reflog_line_falls_through_instead_of_raising(
            self, wp_clock, tmp_path):
        gd = _admin_dir(tmp_path, reflog_text="not a reflog line at all\n")
        cand = self._cand_for(wp_clock, tmp_path)
        got = wp_clock.worktree_born_at(_age_run(wp_clock, gd), cand)
        assert got == (pytest.approx(wp_clock._admin_birthtime(str(gd)))
                       if HAS_BIRTHTIME else None)

    def test_the_later_of_the_two_clocks_wins_when_the_reflog_is_later(
            self, wp_clock, tmp_path):
        gd = _admin_dir(tmp_path, reflog_ts=1)
        later = int((wp_clock._admin_birthtime(str(gd)) or 0) + 3600)
        (gd / "logs" / "HEAD").write_text(
            REFLOG_LINE.format(zero="0" * 40, ts=later))
        cand = self._cand_for(wp_clock, tmp_path)
        got = wp_clock.worktree_born_at(_age_run(wp_clock, gd), cand)
        assert got == pytest.approx(later), "a gc-truncated reflog must win"

    @needs_birthtime
    def test_a_restored_copy_is_dated_by_its_local_birthtime_not_the_travelled_reflog(
            self, wp_clock, tmp_path):
        gd = _admin_dir(tmp_path, reflog_ts=1_600_000_000)
        birth = wp_clock._admin_birthtime(str(gd))
        assert birth > 1_600_000_000, "fixture premise: the copy is younger"
        cand = self._cand_for(wp_clock, tmp_path)
        got = wp_clock.worktree_born_at(_age_run(wp_clock, gd), cand)
        assert got == pytest.approx(birth), \
            "a reflog restored from backup must not age the local copy"

    @pytest.mark.parametrize("birth", [0.0, -1.0])
    def test_an_implausible_birthtime_is_unresolved_not_the_epoch(
            self, wp_clock, tmp_path, monkeypatch, birth):
        gd = _admin_dir(tmp_path, reflog_ts=None)
        real_stat = os.stat

        def fake_stat(path, *a, **k):
            if str(path) == str(gd):
                return _SimpleNamespace(st_birthtime=birth)
            return real_stat(path, *a, **k)

        monkeypatch.setattr(wp_clock.os, "stat", fake_stat)
        cand = self._cand_for(wp_clock, tmp_path)
        assert wp_clock.worktree_born_at(_age_run(wp_clock, gd), cand) is None

    def test_a_filesystem_without_birthtime_and_an_empty_reflog_is_unresolved(
            self, wp_clock, tmp_path, monkeypatch):
        gd = _admin_dir(tmp_path, reflog_ts=None)
        real_stat = os.stat

        def fake_stat(path, *a, **k):
            if str(path) == str(gd):
                return _SimpleNamespace()
            return real_stat(path, *a, **k)

        monkeypatch.setattr(wp_clock.os, "stat", fake_stat)
        cand = self._cand_for(wp_clock, tmp_path)
        assert wp_clock.worktree_born_at(_age_run(wp_clock, gd), cand) is None

    def test_an_unresolvable_admin_dir_is_unresolved(self, wp_clock, tmp_path):
        cand = self._cand_for(wp_clock, tmp_path)
        assert wp_clock.worktree_born_at(_age_run(wp_clock, None), cand) is None


class TestAgeGateDecision:
    def _cand(self, wp_clock, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir(exist_ok=True)
        return wp_clock.Candidate(path=str(wt), head="h", branch="feat",
                                  detached=False, clone="/clone")

    def _born_and_gate(self, wp_clock, tmp_path, offset_seconds, days=14):
        """The reflog is the later clock on every platform, so it alone sets born."""
        gd = _admin_dir(tmp_path, reflog_ts=None)
        born = int((wp_clock._admin_birthtime(str(gd)) or 0) + 3600)
        (gd / "logs" / "HEAD").write_text(
            REFLOG_LINE.format(zero="0" * 40, ts=born))
        return wp_clock.gate_old_enough(_age_run(wp_clock, gd),
                                        self._cand(wp_clock, tmp_path),
                                        born + offset_seconds, days)

    def test_a_tree_inside_the_window_is_refused_as_too_young(self, wp_clock, tmp_path):
        assert self._born_and_gate(wp_clock, tmp_path, 13 * 86400) \
            == (False, "too_young")

    def test_exactly_at_the_window_the_tree_is_old_enough(self, wp_clock, tmp_path):
        assert self._born_and_gate(wp_clock, tmp_path, 14 * 86400) == (True, None)

    def test_a_tree_created_in_the_future_is_refused(self, wp_clock, tmp_path):
        assert self._born_and_gate(wp_clock, tmp_path, -3600) == (False, "too_young")

    def test_an_unreadable_clock_is_refused_and_names_itself(self, wp_clock, tmp_path):
        cand = self._cand(wp_clock, tmp_path)
        assert wp_clock.gate_old_enough(_age_run(wp_clock, None), cand,
                                        1_700_000_000, 14) == (False, "age_unknown")

    def test_the_module_window_reads_a_positive_environment_value(self, monkeypatch):
        monkeypatch.setenv("WORKTREE_PRUNE_MIN_AGE_DAYS", "3")
        assert _load().MIN_AGE_DAYS == 3

    @pytest.mark.parametrize("raw", ["0", "-1", "abc", "", "  ", "14.5"])
    def test_the_module_window_cannot_be_switched_off_from_the_environment(
            self, monkeypatch, raw):
        monkeypatch.setenv("WORKTREE_PRUNE_MIN_AGE_DAYS", raw)
        assert _load().MIN_AGE_DAYS == 14, (
            "MIN_AGE_DAYS is what run_prune's clamp falls back to, so a module "
            "binding that accepts this value disables the gate on both surfaces")

    def test_the_shipped_default_window_is_fourteen_days(self, monkeypatch):
        monkeypatch.delenv("WORKTREE_PRUNE_MIN_AGE_DAYS", raising=False)
        assert _load().MIN_AGE_DAYS == 14


class TestAgeGateIntegration:
    def _tree(self, wp_clock, monkeypatch, tmp_path, name, reflog_ts):
        wt = tmp_path / "worktrees" / name
        wt.mkdir(parents=True)
        gd = _admin_dir(tmp_path, name=f"admin-{name}", reflog_ts=reflog_ts)
        cand = wp_clock.Candidate(path=str(wt), head="h", branch=f"feat-{name}",
                                  detached=False, clone="/clone")
        return cand, gd

    def test_the_parameter_cannot_switch_the_gate_off_either(
            self, wp_clock, monkeypatch, tmp_path):
        born = _time.time()
        cand, gd = self._tree(wp_clock, monkeypatch, tmp_path, "fresh", born)
        monkeypatch.setattr(wp_clock, "enumerate_candidates", lambda r, c, ro: [cand])
        monkeypatch.setattr(wp_clock, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp_clock, "_collect_lsof_cwds", lambda r: [])
        _d, info = wp_clock.run_prune(born + 60, apply=False,
                                      run=_age_run(wp_clock, gd),
                                      clone_parents=["/cp"], roots=["/r"],
                                      self_path="/elsewhere", min_age_days=0)
        assert info["results"][0]["reason"] == "too_young", \
            "min_age_days=0 must fall back to the default, not disable the gate"

    def test_the_two_refusals_are_counted_separately(
            self, wp_clock, monkeypatch, tmp_path):
        born = _time.time()
        young, gd_young = self._tree(wp_clock, monkeypatch, tmp_path, "young", born)
        blind = wp_clock.Candidate(path=str(tmp_path / "gone"), head="h",
                                   branch="feat-gone", detached=False, clone="/clone")
        monkeypatch.setattr(wp_clock, "enumerate_candidates",
                            lambda r, c, ro: [young, blind])
        monkeypatch.setattr(wp_clock, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp_clock, "_collect_lsof_cwds", lambda r: [])

        def run(args, cwd=None):
            if "--absolute-git-dir" in args:
                if args[2] == str(young.path):
                    return _RR(wp_clock, 0, f"{gd_young}\n")
                return _RR(wp_clock, 128)
            return _age_run(wp_clock, gd_young)(args, cwd)

        _d, info = wp_clock.run_prune(born + 60, apply=False, run=run,
                                      clone_parents=["/cp"], roots=["/r"],
                                      self_path="/elsewhere")
        assert info["summary"]["by_reason"] == {"too_young": 1, "age_unknown": 1}

    def test_a_tree_recreated_between_scan_and_removal_is_refused(
            self, wp_clock, monkeypatch, tmp_path):
        born = _time.time()
        now = born + 40 * 86400
        cand, gd_old = self._tree(wp_clock, monkeypatch, tmp_path, "swap", born)
        gd_new = _admin_dir(tmp_path, name="admin-new", reflog_ts=now)
        monkeypatch.setattr(wp_clock, "enumerate_candidates", lambda r, c, ro: [cand])
        monkeypatch.setattr(wp_clock, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp_clock, "_collect_lsof_cwds", lambda r: [])
        seen = {"n": 0}
        calls = []

        def run(args, cwd=None):
            calls.append(args)
            if "--absolute-git-dir" in args:
                seen["n"] += 1
                return _RR(wp_clock, 0, f"{gd_old if seen['n'] == 1 else gd_new}\n")
            return _age_run(wp_clock, gd_old)(args, cwd)

        _d, info = wp_clock.run_prune(now, apply=True, run=run,
                                      clone_parents=["/cp"], roots=["/r"],
                                      self_path="/elsewhere")
        assert info["summary"]["removed"] == 0
        assert not any("remove" in a for a in calls), \
            "a worktree re-created under the same path was removed"
        led = [e for e in _ledger_events(wp_clock) if e["event"] == "skip_toctou"]
        assert led and led[0]["reason"] == "toctou_too_young"


class TestAgeCapabilityIsGated:
    def test_the_script_reports_the_age_gate_capability(self, wp):
        assert "age-gate" in wp.capabilities()

    def test_a_missing_age_gate_drops_its_token(self, wp, monkeypatch):
        monkeypatch.delitem(wp.__dict__, "gate_old_enough")
        assert "age-gate" not in wp.capabilities()

    def test_the_installer_refuses_a_script_without_the_age_gate(self, tmp_path):
        proc, plist = _render_plist(tmp_path, {})
        assert proc.returncode == 0 and plist, "baseline install must succeed"

        home = tmp_path / "home"
        script = home / ".claude" / "scripts" / "worktree_prune.py"
        script_without_the_gate = script.read_text().replace(
            '("age-gate", "gate_old_enough"),', "")
        assert "age-gate" not in script_without_the_gate, (
            "fixture premise only: the negative script must really lack the token "
            "before the installer's exit code can mean anything")
        script.write_text(script_without_the_gate)

        fake_bin = tmp_path / "bin"
        env = dict({k: v for k, v in os.environ.items()
                    if not k.startswith("WORKTREE_PRUNE_")},
                   HOME=str(home), PATH=f"{fake_bin}:{os.environ['PATH']}")
        again = _sp.run(["bash", str(INSTALLER)], capture_output=True, text=True,
                        env=env)
        assert again.returncode != 0, \
            "the installer accepted a plist over a script with no age gate"
        assert "age-gate" in (again.stdout + again.stderr)


class TestAgeWindowReachesLaunchd:
    def _env_block(self, text):
        start = text.index("<key>EnvironmentVariables</key>")
        return text[start:text.index("</dict>", start)]

    def test_the_window_is_emitted_into_the_plist(self, tmp_path):
        proc, plist = _render_plist(tmp_path, {"WORKTREE_PRUNE_MIN_AGE_DAYS": "21"})
        assert proc.returncode == 0, proc.stderr
        env = self._env_block(plist)
        assert "<key>WORKTREE_PRUNE_MIN_AGE_DAYS</key>" in env
        assert "<string>21</string>" in env

    def test_the_window_survives_a_bare_reinstall(self, tmp_path):
        proc, plist = _render_plist(tmp_path, {"WORKTREE_PRUNE_MIN_AGE_DAYS": "21"})
        assert proc.returncode == 0 and "<string>21</string>" in self._env_block(plist)
        again, plist2 = _render_plist(tmp_path, {})
        assert again.returncode == 0, again.stderr
        assert "<string>21</string>" in self._env_block(plist2), (
            "a bare re-run dropped the operator's window back to the default "
            "silently — the WORKTREE_PRUNE_LSOF trap the registry documents")

    def test_an_unset_window_emits_no_key_and_leaves_the_default(self, tmp_path):
        proc, plist = _render_plist(tmp_path, {})
        assert proc.returncode == 0
        assert "WORKTREE_PRUNE_MIN_AGE_DAYS" not in self._env_block(plist)

    @pytest.mark.parametrize("bad", ["7&8", "3O", "0", "00", "000", "-1", "abc",
                                     "14.5", "007x", "+7", " 7", "7 ", "1e3"])
    def test_a_non_integer_window_is_refused_rather_than_written(self, tmp_path, bad):
        proc, _plist = _render_plist(tmp_path, {"WORKTREE_PRUNE_MIN_AGE_DAYS": bad})
        assert proc.returncode != 0, (
            f"the installer wrote {bad!r} into the plist; a value the script cannot "
            "parse silently becomes the built-in default and sticks across re-runs")
        assert "WORKTREE_PRUNE_MIN_AGE_DAYS" in (proc.stdout + proc.stderr)

    @pytest.mark.parametrize("good", ["1", "7", "07", "010", "007", "14", "21", "365"])
    def test_a_positive_integer_window_is_accepted(self, tmp_path, good):
        proc, plist = _render_plist(tmp_path, {"WORKTREE_PRUNE_MIN_AGE_DAYS": good})
        assert proc.returncode == 0, proc.stderr
        assert f"<string>{good}</string>" in self._env_block(plist)

    def test_a_refused_window_leaves_no_plist_behind_on_a_first_install(self, tmp_path):
        proc, plist = _render_plist(tmp_path, {"WORKTREE_PRUNE_MIN_AGE_DAYS": "7&8"})
        assert proc.returncode != 0
        assert plist is None, "a refused install must not leave a half-written plist"


class TestGateTokensAreInstallerGated:
    @pytest.mark.parametrize("missing", ["merged-gate", "open-work-gate"])
    def test_the_installer_refuses_a_script_missing_a_gate_token(self, wp, tmp_path, missing):
        tokens = [t for t in wp.capabilities() if t != missing]
        assert missing not in tokens and "keeplist" in tokens and "age-gate" in tokens
        home = tmp_path / "home"
        (home / ".claude" / "scripts").mkdir(parents=True)
        (home / "Library" / "LaunchAgents").mkdir(parents=True)
        (home / ".claude" / "scripts" / "worktree_prune.py").write_text(
            "import sys\n"
            f"TOKENS = {tokens!r}\n"
            'if "--capabilities" in sys.argv:\n'
            '    print("\\n".join(TOKENS))\n'
            "sys.exit(0)\n")
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        (fake_bin / "launchctl").write_text("#!/bin/sh\nexit 0\n")
        (fake_bin / "launchctl").chmod(0o755)
        env = dict({k: v for k, v in os.environ.items() if not k.startswith("WORKTREE_PRUNE_")},
                   HOME=str(home), PATH=f"{fake_bin}:{os.environ['PATH']}")
        proc = _sp.run(["bash", str(INSTALLER)], capture_output=True, text=True, env=env)
        assert proc.returncode != 0
        assert missing in proc.stderr
        assert not list((home / "Library" / "LaunchAgents").glob("*.plist"))


DW = "/opt/dw"
DW_ARGV = [DW, "pipeline", "active-paths", "--json"]


def _works_json(works, version=1):
    return json.dumps({"version": version,
                       "works": [{"task_key": k, "paths": p} for k, p in works]})


class TestOpenWorkClaims:
    def test_a_path_inside_the_tree_claims_it(self, wp):
        assert wp.gate_no_open_work(_cand(wp, path="/r/grp/svc"),
                                    [("W", ["/r/grp/svc/sub"])], ["/r"]) == (False, "open_work")

    def test_a_sibling_repo_of_the_same_group_claims_it(self, wp):
        assert wp.gate_no_open_work(_cand(wp, path="/r/grp/svc"),
                                    [("W", ["/r/grp/web"])], ["/r"]) == (False, "open_work")

    def test_a_group_named_after_the_work_key_is_claimed(self, wp):
        assert wp.gate_no_open_work(_cand(wp, path="/r/Grp-1/svc"),
                                    [("grp-1", [])], ["/r"]) == (False, "open_work")

    @pytest.mark.parametrize("path", ["/r", "/elsewhere/grp/svc", "/r/other/svc"])
    def test_a_root_an_outside_path_or_another_group_claims_nothing(self, wp, path):
        assert wp.gate_no_open_work(_cand(wp, path="/r/grp/svc"),
                                    [("W", [path])], ["/r"]) == (True, None)

    def test_no_answer_refuses(self, wp):
        assert wp.gate_no_open_work(_cand(wp, path="/r/grp/svc"), None, ["/r"]) \
            == (False, "work_scan_failed")


class TestOpenWorkGate:
    def _drive(self, wp, monkeypatch, dw_rr, apply=True):
        cand = _cand(wp, path="/r/grp/svc", branch="feat", head="h", clone="/clone")
        monkeypatch.setattr(wp, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda r: [])
        monkeypatch.setenv("WORKTREE_PRUNE_DOCKWRIGHT", DW)
        calls = []

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            calls.append(args)
            if args == DW_ARGV:
                return dw_rr
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == wp.GH_BIN:
                return _gh_merged(wp, "h", args)
            if "status" in args:
                return _RR(wp, 0, "")
            if "rev-parse" in args:
                return _RR(wp, 0, "h\n")
            if "remove" in args or _is_branch_delete(args):
                return _RR(wp, 0)
            raise AssertionError(args)

        _d, info = wp.run_prune(NOW, apply=apply, run=run, clone_parents=["/cp"],
                                roots=["/r"], self_path="/elsewhere")
        return calls, info, {r["path"]: r for r in info["results"]}["/r/grp/svc"]

    def test_an_unclaimed_tree_is_removed(self, wp_works, monkeypatch):
        calls, info, row = self._drive(wp_works, monkeypatch,
                                       _RR(wp_works, 0, _works_json([("W", ["/r/other/svc"])])))
        assert row["action"] == "REMOVED"
        assert info["summary"]["works_failed"] == 0
        assert calls.count(DW_ARGV) == 2

    def test_a_claimed_tree_is_kept(self, wp_works, monkeypatch):
        calls, info, row = self._drive(wp_works, monkeypatch,
                                       _RR(wp_works, 0, _works_json([("W", ["/r/grp/web"])])))
        assert row["reason"] == "open_work"
        assert info["summary"]["removed"] == 0
        assert not any("remove" in a for a in calls)

    @pytest.mark.parametrize("rc,out", [
        (1, ""),
        (0, "not json"),
        (0, json.dumps({"version": 2, "works": []})),
        (0, json.dumps({"version": 1, "works": {}})),
        (0, json.dumps({"version": 1, "works": ["W"]})),
        (0, json.dumps({"version": 1, "works": [{"task_key": 7, "paths": []}]})),
        (0, json.dumps({"version": 1, "works": [{"task_key": "W", "paths": [1]}]})),
        (0, json.dumps({"version": 1, "works": [{"task_key": "W"}]})),
    ])
    def test_any_broken_answer_refuses_every_tree(self, wp_works, monkeypatch, rc, out):
        calls, info, row = self._drive(wp_works, monkeypatch, _RR(wp_works, rc, out, "boom"))
        assert row["reason"] == "work_scan_failed"
        assert info["summary"]["works_failed"] == 1
        assert info["summary"]["removed"] == 0
        assert not any("remove" in a for a in calls)

    def test_no_binary_refuses_every_tree(self, wp_works, monkeypatch, tmp_path):
        monkeypatch.setattr(wp_works, "HOME", tmp_path)
        monkeypatch.setattr(wp_works.shutil, "which", lambda name: None)
        cand = _cand(wp_works, path="/r/grp/svc", branch="feat", head="h", clone="/clone")
        monkeypatch.setattr(wp_works, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp_works, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp_works, "_collect_lsof_cwds", lambda r: [])

        def run(args, cwd=None):
            rr = _infra_rr(wp_works, args)
            if rr is not None:
                return rr
            if "fetch" in args:
                return _RR(wp_works, 0)
            if args[0] == wp_works.GH_BIN:
                return _gh_merged(wp_works, "h", args)
            if "remove" in args:
                raise AssertionError("must not remove without an answer")
            return _RR(wp_works, 0, "")

        _d, info = wp_works.run_prune(NOW, apply=True, run=run, clone_parents=["/cp"],
                                      roots=["/r"], self_path="/elsewhere")
        assert info["results"][0]["reason"] == "work_scan_failed"
        assert info["summary"]["works_failed"] == 1

    def test_the_binary_resolves_from_env_then_path_then_local_bin(self, wp_works, monkeypatch,
                                                                    tmp_path):
        monkeypatch.setattr(wp_works, "HOME", tmp_path)
        monkeypatch.setenv("WORKTREE_PRUNE_DOCKWRIGHT", "/opt/env-dw")
        monkeypatch.setattr(wp_works.shutil, "which", lambda name: "/usr/local/bin/dockwright")
        assert wp_works._dockwright_bin() == "/opt/env-dw"
        monkeypatch.delenv("WORKTREE_PRUNE_DOCKWRIGHT")
        assert wp_works._dockwright_bin() == "/usr/local/bin/dockwright"
        monkeypatch.setattr(wp_works.shutil, "which", lambda name: None)
        assert wp_works._dockwright_bin() is None
        local = tmp_path / ".local" / "bin" / "dockwright"
        local.parent.mkdir(parents=True)
        local.write_text("")
        assert wp_works._dockwright_bin() == str(local)

    def test_a_failed_query_reaches_the_check_log_with_its_stderr(self, wp_works, monkeypatch):
        self._drive(wp_works, monkeypatch, _RR(wp_works, 1, "", "dw exploded"), apply=False)
        check = wp_works.CHECK_LOG_PATH.read_text()
        assert "work_scan_failed" in check and "dw exploded" in check
        assert '"works_failed": 1' in check

    def test_the_summary_line_carries_works_failed(self, wp, monkeypatch, capsys):
        monkeypatch.setattr(wp, "run_prune",
                            lambda *a, **k: ("dry-run", {"results": [], "summary": {
                                "scanned": 1, "would_remove": 0, "removed": 0, "skipped": 1,
                                "capped": 0, "gh_failed": 0, "works_failed": 3}}))
        wp.main([])
        assert "works_failed=3" in capsys.readouterr().out


class TestOpenWorkReverify:
    def _drive(self, wp, monkeypatch, second):
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        monkeypatch.setattr(wp, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda r: [])
        n = {"calls": 0}

        def works(run):
            n["calls"] += 1
            return [] if n["calls"] == 1 else second()

        monkeypatch.setattr(wp, "_load_open_works", works)
        calls = []

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            calls.append(args)
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == wp.GH_BIN:
                return _gh_merged(wp, "h", args)
            if "status" in args:
                return _RR(wp, 0, "")
            if "rev-parse" in args:
                return _RR(wp, 0, "h\n")
            if "remove" in args or _is_branch_delete(args):
                return _RR(wp, 0)
            raise AssertionError(args)

        _d, info = wp.run_prune(NOW, apply=True, run=run, clone_parents=["/cp"],
                                roots=["/r"], self_path="/elsewhere")
        assert not any("remove" in a for a in calls), "a re-verify refusal must not reach git"
        assert info["summary"]["removed"] == 0
        return _ledger_by_event(wp)["skip_toctou"][0]["reason"]

    def test_a_work_claiming_the_tree_before_removal_keeps_it(self, wp, monkeypatch):
        assert self._drive(wp, monkeypatch, lambda: [("W", ["/wt"])]) == "toctou_open_work"

    def test_a_failed_requery_keeps_the_tree(self, wp, monkeypatch):
        assert self._drive(wp, monkeypatch, lambda: None) == "toctou_work_scan_failed"

    def test_an_exception_in_the_requery_removes_nothing(self, wp, monkeypatch):
        def boom():
            raise RuntimeError("requery blew up")
        assert self._drive(wp, monkeypatch, boom).startswith("toctou_")


class TestOpenWorkGateEndToEnd:
    def test_a_tree_a_real_store_claims_is_kept_until_the_work_is_gone(self, wp_works, merged_repo,
                                                                       tmp_path, monkeypatch):
        tree, _head = _branch_tree(merged_repo, tmp_path / "wt", "feat")
        _git(merged_repo, "merge", "-q", "--ff-only", "feat")
        _git(merged_repo, "push", "-q", "origin", "main")
        root = tmp_path / "state"
        work = root / "artifacts" / "W"
        work.mkdir(parents=True)
        (root / "assignments").mkdir()
        (work / "schema.toml").write_text("not = [valid")
        (work / "events.jsonl").write_text(
            json.dumps({"type": "pipeline_started", "cwd": os.path.realpath(tree)}) + "\n")
        cfg = tmp_path / "dockwright.toml"
        cfg.write_text(f'[paths]\nstate_root = "{root}"\n')
        monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
        dockwright_bin = shutil.which("dockwright")
        assert dockwright_bin, "tests/conftest.py puts an in-tree dockwright shim on PATH"
        monkeypatch.setenv("WORKTREE_PRUNE_DOCKWRIGHT", dockwright_bin)
        monkeypatch.setattr(wp_works, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp_works, "_collect_lsof_cwds", lambda r: [])
        run = _real_run(wp_works, _gh_answer([]))
        kwargs = dict(apply=True, run=run, clone_parents=[str(tmp_path / "clones")],
                      roots=[str(tmp_path / "wt")], self_path="/elsewhere")

        _d, info = wp_works.run_prune(NOW, **kwargs)
        row = {r["path"]: r for r in info["results"]}[os.path.realpath(tree)]
        assert row["reason"] == "open_work"
        assert info["summary"]["works_failed"] == 0
        assert tree.exists()

        shutil.rmtree(work)
        _d, info = wp_works.run_prune(NOW, **kwargs)
        assert info["summary"]["removed"] == 1
        assert not tree.exists()
