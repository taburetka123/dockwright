"""Worktree prune loop — merged+clean+unowned gating, dry-run by default.

Loads the standalone script the same way test_bootlite_watchdog.py loads the
watchdog (importlib spec_from_file_location). The fleet is never touched: a fake
`run` callable returns canned subprocess output, and active-records / lsof inputs
are injected directly. The safety invariant under test is "only ever under-prune"
— every error / parse failure / unavailable signal must resolve to SKIP.
"""
import importlib.util
import json
import sys
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
    # Register before exec so the @dataclass decorator can resolve its module.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def wp(tmp_path, monkeypatch):
    for var in ("WORKTREE_PRUNE_ROOTS", "WORKTREE_PRUNE_CLONE_PARENTS",
                "WORKTREE_PRUNE_MAX_REMOVALS"):
        monkeypatch.delenv(var, raising=False)
    mod = _load()
    wtdir = tmp_path / "worktree-prune"
    monkeypatch.setattr(mod, "WT_DIR", wtdir)
    monkeypatch.setattr(mod, "LEDGER_PATH", wtdir / "ledger.jsonl")
    monkeypatch.setattr(mod, "CHECK_LOG_PATH", wtdir / "check.log")
    monkeypatch.setattr(mod, "STOP_PATHS", (tmp_path / "worktree-prune-stop", tmp_path / "legacy-worktree-prune-stop"))
    monkeypatch.setattr(mod, "ORCH_ACTIVE", tmp_path / "active")
    return mod


def _ledger_events(wp):
    if not wp.LEDGER_PATH.is_file():
        return []
    return [json.loads(line) for line in wp.LEDGER_PATH.read_text().splitlines() if line.strip()]


def _RR(wp, rc=0, out="", err=""):
    return wp.RunResult(rc, out, err)


# ----------------------------------------------------------------------------
# Task 1 — scaffolding, stop-file gate
# ----------------------------------------------------------------------------
class TestStopFile:
    def test_stop_file_short_circuits_and_runs_nothing(self, wp):
        wp.STOP_PATHS[0].touch()
        calls = []

        def fake_run(args, cwd=None):
            calls.append(args)
            raise AssertionError("run must not be called once stopped")

        decision, info = wp.run_prune(NOW, apply=False, run=fake_run)
        assert decision == "stopped"
        assert calls == []
        assert not wp.LEDGER_PATH.exists()

    def test_legacy_stop_file_short_circuits(self, wp):
        wp.STOP_PATHS[1].touch()

        def fake_run(args, cwd=None):
            raise AssertionError("run must not be called once stopped")

        decision, _ = wp.run_prune(NOW, apply=False, run=fake_run)
        assert decision == "stopped"

    def test_stop_file_honored_even_with_apply(self, wp):
        wp.STOP_PATHS[0].touch()

        def fake_run(args, cwd=None):
            raise AssertionError("run must not be called once stopped")

        decision, _ = wp.run_prune(NOW, apply=True, run=fake_run)
        assert decision == "stopped"
        assert _ledger_events(wp) == []


# ----------------------------------------------------------------------------
# Task 2 — enumerate candidates (both roots, both layouts, main/off-root excl.)
# ----------------------------------------------------------------------------
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
        # a non-repo dir under a clone-parent must be ignored (no run call)
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
        # loose-dir has no .git, so no worktree-list call was issued for it
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
            return _RR(wp, 128, "", "fatal: not a git repository")

        cands = wp.enumerate_candidates(fake_run, [str(tmp_path / "projects" / "personal")], roots)
        assert cands == []


def _cand(wp, head="h1", branch="feat", detached=False, path="/wt", clone="/clone"):
    return wp.Candidate(path=path, head=head, branch=branch, detached=detached, clone=clone)


# ----------------------------------------------------------------------------
# Task 3 — Gate A: merged (headRefOid guard + ancestor fallback)
# ----------------------------------------------------------------------------
class TestGateMerged:
    def test_merged_with_matching_head(self, wp):
        cand = _cand(wp, head="abc")

        def run(args, cwd=None):
            if args[0] == "gh":
                return _RR(wp, 0, json.dumps({"state": "MERGED", "headRefOid": "abc"}))
            return _RR(wp, 1)

        assert wp.gate_merged(run, cand) is True

    def test_gh_invoked_with_worktree_cwd(self, wp):
        cand = _cand(wp, head="abc", path="/wt-x")
        seen = {}

        def run(args, cwd=None):
            if args[0] == "gh":
                seen["cwd"] = cwd
                return _RR(wp, 0, json.dumps({"state": "MERGED", "headRefOid": "abc"}))
            return _RR(wp, 1)

        assert wp.gate_merged(run, cand) is True
        assert seen["cwd"] == "/wt-x"

    def test_merged_but_head_mismatch_is_not_merged(self, wp):
        """Post-merge local commits: PR is MERGED but HEAD moved past it — must
        NOT prune (would drop the local-only commits)."""
        cand = _cand(wp, head="abc")

        def run(args, cwd=None):
            if args[0] == "gh":
                return _RR(wp, 0, json.dumps({"state": "MERGED", "headRefOid": "DEADBEEF"}))
            return _RR(wp, 1)  # also not an ancestor

        assert wp.gate_merged(run, cand) is False

    def test_open_pr_and_not_ancestor_is_not_merged(self, wp):
        cand = _cand(wp)

        def run(args, cwd=None):
            if args[0] == "gh":
                return _RR(wp, 0, json.dumps({"state": "OPEN", "headRefOid": "h1"}))
            return _RR(wp, 1)

        assert wp.gate_merged(run, cand) is False

    def test_no_pr_and_not_ancestor_is_not_merged(self, wp):
        cand = _cand(wp)

        def run(args, cwd=None):
            if args[0] == "gh":
                return _RR(wp, 1, "", "no pull requests found")
            return _RR(wp, 1)

        assert wp.gate_merged(run, cand) is False

    def test_no_pr_but_ancestor_is_merged(self, wp):
        cand = _cand(wp)

        def run(args, cwd=None):
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
            seen.append(args[0])
            if "merge-base" in args:
                return _RR(wp, 0)
            return _RR(wp, 1)

        assert wp.gate_merged(run, cand) is True
        assert "gh" not in seen

    def test_gh_exception_falls_through_to_ancestor(self, wp):
        cand = _cand(wp)

        def run(args, cwd=None):
            if args[0] == "gh":
                raise TimeoutError("gh timed out")
            if "merge-base" in args:
                return _RR(wp, 0)
            return _RR(wp, 1)

        assert wp.gate_merged(run, cand) is True

    def test_gh_garbage_json_falls_through_to_ancestor(self, wp):
        cand = _cand(wp)

        def run(args, cwd=None):
            if args[0] == "gh":
                return _RR(wp, 0, "not json at all")
            if "merge-base" in args:
                return _RR(wp, 1)
            return _RR(wp, 1)

        assert wp.gate_merged(run, cand) is False


# ----------------------------------------------------------------------------
# Task 4 — Gate B: clean (untracked-injected-only)
# ----------------------------------------------------------------------------
class TestGateClean:
    def _run_status(self, wp, out, rc=0):
        cand = _cand(wp, path="/wt")

        def run(args, cwd=None):
            assert args == ["git", "-C", "/wt", "status", "--porcelain"]
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
        # A substring grep -v CLAUDE.md would wrongly pass this; the status-code
        # check must treat a tracked modification as dirty.
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
            raise OSError("boom")

        assert wp.gate_clean(run, cand) is False


# ----------------------------------------------------------------------------
# Task 5 — Gate C: unowned (active records + lsof + self-guard)
# gate_unowned returns True = UNOWNED (eligible), False = owned (SKIP).
# ----------------------------------------------------------------------------
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

    def test_active_none_lsof_empty_is_eligible(self, wp):
        cand = _cand(wp, path="/wt")
        assert wp.gate_unowned(cand, None, [], "/elsewhere", pid_alive=_live) is True

    def test_default_pid_alive_uses_current_process(self, wp):
        import os as _os
        cand = _cand(wp, path="/wt")
        recs = [{"cwd": "/wt", "pid": _os.getpid()}]
        assert wp.gate_unowned(cand, recs, [], "/elsewhere") is False


# ----------------------------------------------------------------------------
# Task 6 — decide() + dry-run output + ledger/check-log
# ----------------------------------------------------------------------------
def _ledger_by_event(wp):
    events = {}
    for e in _ledger_events(wp):
        events.setdefault(e["event"], []).append(e)
    return events


class TestDecide:
    def test_eligible_and_first_failing_reason(self, wp):
        cand = _cand(wp)
        assert wp.decide(cand, {"merged": True, "clean": True, "unowned": True}) \
            == ("WOULD-REMOVE", None)
        assert wp.decide(cand, {"merged": False, "clean": True, "unowned": True}) \
            == ("SKIP", "unmerged")
        assert wp.decide(cand, {"merged": True, "clean": False, "unowned": True}) \
            == ("SKIP", "dirty")
        assert wp.decide(cand, {"merged": True, "clean": True, "unowned": False}) \
            == ("SKIP", "owned")
        # precedence: merged before clean before owned
        assert wp.decide(cand, {"merged": False, "clean": False, "unowned": False}) \
            == ("SKIP", "unmerged")


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
            calls.append(args)
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == "gh":
                branch = args[3]
                merged = {"feat-elig": "h-elig", "feat-dirty": "h-dirty"}
                if branch in merged:
                    return _RR(wp, 0, json.dumps({"state": "MERGED",
                                                  "headRefOid": merged[branch]}))
                return _RR(wp, 0, json.dumps({"state": "OPEN", "headRefOid": "x"}))
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
        assert res["/wt/un"]["reason"] == "unmerged"
        assert res["/wt/dirty"]["action"] == "SKIP"
        assert res["/wt/dirty"]["reason"] == "dirty"

        assert info["summary"]["scanned"] == 3
        assert info["summary"]["would_remove"] == 1
        assert info["summary"]["skipped"] == 2
        assert info["summary"]["by_reason"] == {"unmerged": 1, "dirty": 1}

        led = _ledger_by_event(wp)
        assert len(led.get("would_remove", [])) == 1
        assert led["would_remove"][0]["path"] == "/wt/elig"
        assert wp.CHECK_LOG_PATH.is_file()

        # no destructive op issued in dry-run
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
            if "fetch" in args:
                clone = args[2]
                return _RR(wp, 0) if clone == "/cloneA" else _RR(wp, 128, "", "boom")
            if args[0] == "gh":
                gh_branches.append(args[3])
                return _RR(wp, 0, json.dumps({"state": "MERGED",
                                              "headRefOid": {"A": "hA", "B": "hB"}[args[3]]}))
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
        # candB's gates were never evaluated
        assert "B" not in gh_branches
        assert info["summary"]["by_reason"].get("fetch_failed") == 1


# ----------------------------------------------------------------------------
# Task 7 — --apply removal (re-verify B+C, force-remove, branch -D, rate cap)
# ----------------------------------------------------------------------------
REMOVE = ["git", "-C", "/clone", "worktree", "remove", "--force", "/wt"]
BRANCH_D = ["git", "-C", "/clone", "branch", "-D", "feat"]


def _is_branch_delete(args):
    return args[:2] == ["git", "-C"] and "branch" in args and "-D" in args


def _gh_merged(wp, head):
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
            calls.append(args)
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == "gh":
                return _gh_merged(wp, "h")
            if "status" in args:
                return _RR(wp, 0, "")
            if "rev-parse" in args:
                return _RR(wp, 0, "h\n")  # HEAD unchanged since scan
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
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == "gh":
                return _gh_merged(wp, "h")
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
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == "gh":
                return _gh_merged(wp, "h")
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
            calls.append(args)
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == "gh":
                raise AssertionError("no gh for detached HEAD")
            if "status" in args:
                return _RR(wp, 0, "")
            if "merge-base" in args:
                return _RR(wp, 0)  # ancestor -> merged
            if "rev-parse" in args:
                return _RR(wp, 0, "h\n")  # detached HEAD unchanged since scan
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
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == "gh":
                return _gh_merged(wp, {"f1": "h1", "f2": "h2"}[args[3]])
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

    def test_apply_remove_failure_skips_branch_delete(self, wp, monkeypatch):
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        self._wire(wp, monkeypatch, [cand])
        calls = []

        def run(args, cwd=None):
            calls.append(args)
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == "gh":
                return _gh_merged(wp, "h")
            if "status" in args:
                return _RR(wp, 0, "")
            if "rev-parse" in args:
                return _RR(wp, 0, "h\n")  # HEAD unchanged since scan
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
        # A commit lands on the merged branch between scan and removal: it stays
        # clean (committed) + unowned (process exited), but HEAD != the scan-time
        # SHA -> removing would destroy a tip carrying commits not in origin/main.
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        self._wire(wp, monkeypatch, [cand])

        def run(args, cwd=None):
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == "gh":
                return _gh_merged(wp, "h")
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
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == "gh":
                return _gh_merged(wp, "h")
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


# ----------------------------------------------------------------------------
# Task 8 — CLI (main, --apply, --json) + summary line
# ----------------------------------------------------------------------------
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
        # Footgun guard: --apply --dry-run must resolve to the SAFE direction.
        seen = {}
        self._stub(wp, monkeypatch, "dry-run",
                   {"results": [], "summary": wp._empty_summary()}, seen)
        assert wp.main(["--apply", "--dry-run"]) == 0
        assert seen["apply"] is False


# ----------------------------------------------------------------------------
# Review findings — direct coverage for helpers previously only monkeypatched
# ----------------------------------------------------------------------------
class TestCollectLsof:
    def test_rc0_parses_n_lines(self, wp):
        out = "p100\nn/path/a\np200\nn/path/b\n"

        def run(args, cwd=None):
            assert args == ["lsof", "-d", "cwd", "-F", "pn"]
            return _RR(wp, 0, out)

        assert wp._collect_lsof_cwds(run) == ["/path/a", "/path/b"]

    def test_rc_nonzero_with_output_keeps_paths(self, wp):
        # Load-bearing safety branch: a permission-limited lsof exits non-zero
        # while still listing accessible cwds — discarding them would turn a
        # real owner into a false "no owner" and over-prune.
        out = "p100\nn/path/a\n"

        def run(args, cwd=None):
            return _RR(wp, 1, out, "lsof: WARNING: can't stat()")

        assert wp._collect_lsof_cwds(run) == ["/path/a"]

    def test_rc_nonzero_no_output_is_none(self, wp):
        def run(args, cwd=None):
            return _RR(wp, 1, "", "lsof: command failed")

        assert wp._collect_lsof_cwds(run) is None

    def test_run_raises_is_none(self, wp):
        def run(args, cwd=None):
            raise FileNotFoundError("lsof not installed")

        assert wp._collect_lsof_cwds(run) is None

    def test_rc0_no_n_lines_is_empty_list(self, wp):
        def run(args, cwd=None):
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
        # /wt-foo must NOT count as under /wt (string-prefix bug guard)
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

        def boom(run, c):
            raise RuntimeError("gate blew up")

        monkeypatch.setattr(wp, "gate_merged", boom)

        def run(args, cwd=None):
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


class TestNowTimestamp:
    def test_ledger_ts_uses_now(self, wp, monkeypatch):
        # The run-scoped `now` is threaded into ledger timestamps (determinism).
        cand = _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        monkeypatch.setattr(wp, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda r: [])

        def run(args, cwd=None):
            if "fetch" in args:
                return _RR(wp, 0)
            if args[0] == "gh":
                return _gh_merged(wp, "h")
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
