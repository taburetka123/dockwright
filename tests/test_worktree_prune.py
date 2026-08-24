"""Worktree prune loop — merged+clean+unowned gating, dry-run by default.

Loads the standalone script the same way test_bootlite_watchdog.py loads the
watchdog (importlib spec_from_file_location). The fleet is never touched: a fake
`run` callable returns canned subprocess output, and active-records / lsof inputs
are injected directly. The safety invariant under test is "only ever under-prune"
— every error / parse failure / unavailable signal must resolve to SKIP.
"""
import importlib.util
import json
import os
import shlex
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
    # The keep-list's ABSENCE stops the whole run by design, so every test needs
    # a present (empty) one; cases that exercise the stop delete or repoint it.
    wtdir.mkdir(parents=True, exist_ok=True)
    keep = wtdir / "keep.txt"
    keep.write_text("# holds, one path or glob per line\n")
    monkeypatch.setattr(mod, "KEEPLIST_PATH", keep)
    return mod


def _ledger_events(wp):
    if not wp.LEDGER_PATH.is_file():
        return []
    return [json.loads(line) for line in wp.LEDGER_PATH.read_text().splitlines() if line.strip()]


def _RR(wp, rc=0, out="", err=""):
    return wp.RunResult(rc, out, err)


def _infra_rr(wp, args):
    """Canned answers for calls every scan makes regardless of the case under
    test. Returns None when `args` is not one of them, so a stub can fall
    through to its own assertions."""
    if "for-each-ref" in args:
        # Default: the commit is on origin/main, so a detached candidate is
        # contained. Cases about containment override this with their own stub.
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
    """Branch from a gh argv, whichever subcommand shape it is. Reading a fixed
    index breaks silently when `pr view <branch>` becomes `pr list --head <b>`."""
    if "--head" in args:
        return args[args.index("--head") + 1]
    return args[3]


def _gh_body(args, state, head, number=1):
    """`pr list` -> array, `pr view` -> object. gate_terminal reads the array and
    gate_merged the object; a stub emitting one shape tests only one gate."""
    obj = {"number": number, "state": state, "headRefOid": head}
    return json.dumps([obj] if "list" in args else obj)


def _gh_reply(wp, args, head, state="MERGED"):
    """`pr list` yields an ARRAY, `pr view` an object — the two gates read
    different shapes and a stub that conflates them proves nothing."""
    if "list" in args:
        return _RR(wp, 0, json.dumps([{"number": 1, "state": state,
                                       "headRefOid": head}]))
    return _RR(wp, 0, json.dumps({"state": state, "headRefOid": head}))


# ----------------------------------------------------------------------------
# Task 1 — scaffolding, stop-file gate
# ----------------------------------------------------------------------------
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
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
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
        """Post-merge local commits: PR is MERGED but HEAD moved past it — must
        NOT prune (would drop the local-only commits)."""
        cand = _cand(wp, head="abc")

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            if args[0] == "gh":
                return _RR(wp, 0, json.dumps({"state": "MERGED", "headRefOid": "DEADBEEF"}))
            return _RR(wp, 1)  # also not an ancestor

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


# ----------------------------------------------------------------------------
# Task 4 — Gate B: clean (untracked-injected-only)
# ----------------------------------------------------------------------------
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
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
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


# Mirrors the shapes `_scan` actually produces: `contained` and `ignored_ok` are
# tuple-valued (they can fail for more than one distinguishable cause), the rest
# are bare bools. A fixture that drifts from production is how a shape change
# stops being noticed.
ALL_PASS = {"kept": False, "not_locked": True, "in_progress_clear": True,
            "contained": (True, None), "terminal": True, "clean": True,
            "ignored_ok": (True, None), "unowned": True}


class TestDecide:
    def test_all_gates_pass_is_eligible(self, wp):
        assert wp.decide(_cand(wp), dict(ALL_PASS)) == ("WOULD-REMOVE", None)

    @pytest.mark.parametrize("key,expected", [
        ("kept", "kept"),
        ("not_locked", "locked"),
        ("in_progress_clear", "in_progress"),
        ("terminal", "not_terminal"),
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
        # by_reason["kept"] must be the true hold count, so `kept` is checked
        # before any gate that could also fail on the same candidate.
        gates = {k: (False if k != "contained" else (False, "uncontained"))
                 for k in ALL_PASS}
        gates["kept"] = True
        assert wp.decide(_cand(wp), gates) == ("SKIP", "kept")

    def test_decide_order_covers_every_gate_key(self, wp):
        # Pins DECIDE_ORDER against the TEST fixture. Necessary but not
        # sufficient: it cannot see the production dict growing a gate — that is
        # test_every_gate_in_the_scan_dict_is_consulted, below.
        assert {k for k, _ in wp.DECIDE_ORDER} == set(ALL_PASS)

    def test_every_gate_in_the_scan_dict_is_consulted(self, wp, monkeypatch):
        # ADD-ONE, bound to PRODUCTION. `decide` iterates DECIDE_ORDER, so a key
        # present in the scan's dict but absent from that tuple is never read:
        # the gate does nothing, reports nothing, and the suite stays green. A
        # real safety gate added that way would silently not run.
        seen = {}
        real = wp.decide
        monkeypatch.setattr(wp, "decide",
                            lambda cand, gates: (seen.update(gates), real(cand, gates))[1])
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
            return _RR(wp, 0, "")

        wp.run_prune(NOW, apply=False, run=run, clone_parents=["/cp"], roots=["/r"],
                     self_path="/elsewhere")
        assert seen, "the scan must have evaluated at least one candidate"
        assert set(seen) == {k for k, _ in wp.DECIDE_ORDER}, (
            "a gate in the scan dict with no DECIDE_ORDER entry is never read: "
            f"{set(seen) ^ {k for k, _ in wp.DECIDE_ORDER}}")


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
        assert res["/wt/un"]["reason"] == "not_terminal"
        assert res["/wt/dirty"]["action"] == "SKIP"
        assert res["/wt/dirty"]["reason"] == "dirty"

        assert info["summary"]["scanned"] == 3
        assert info["summary"]["would_remove"] == 1
        assert info["summary"]["skipped"] == 2
        assert info["summary"]["by_reason"] == {"not_terminal": 1, "dirty": 1}

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


def _gh_merged(wp, head, args=None):
    if args is not None and "list" in args:
        return _RR(wp, 0, json.dumps([{"number": 1, "state": "MERGED",
                                       "headRefOid": head}]))
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
            rr = _infra_rr(wp, args)
            if rr is not None:
                return rr
            assert args == ["lsof", "-d", "cwd", "-F", "pn"]
            return _RR(wp, 0, out)

        assert wp._collect_lsof_cwds(run) == ["/path/a", "/path/b"]

    def test_rc_nonzero_with_output_keeps_paths(self, wp):
        # Load-bearing safety branch: a permission-limited lsof exits non-zero
        # while still listing accessible cwds — discarding them would turn a
        # real owner into a false "no owner" and over-prune.
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

        monkeypatch.setattr(wp, "gate_terminal", boom)

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


class TestNowTimestamp:
    def test_ledger_ts_uses_now(self, wp, monkeypatch):
        # The run-scoped `now` is threaded into ledger timestamps (determinism).
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


# ----------------------------------------------------------------------------
# Widening — real-repo fixtures
#
# Several guards here cannot be proven against a stubbed `run`: the thing under
# test IS git's behaviour (what survives `worktree remove`, what `--git-path`
# accepts, which refs `--contains` reports). Those use throwaway repos and a
# SELECTIVE run — real git, canned gh.
# ----------------------------------------------------------------------------
import subprocess as _sp


def _git(cwd, *args, check=True):
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return _sp.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                   env=env, check=check)


def _real_run(wp, gh_json=None):
    """git for real, gh canned. gh has no repo here, so it must be stubbed."""
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
    """A clone with one commit on main, no remote."""
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
        # The class that cost two review rounds: X is held ONLY by refs/heads/feat,
        # a ref this loop can `branch -D`. Removing the detached tree on that
        # strength orphans X the moment the branch goes — same run or next week.
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
        # A tag is durable on its own; confirming it against a server would be
        # a network call for nothing.
        assert not any("ls-remote" in a for a in calls)

    @pytest.mark.parametrize("ns,ref", [
        ("stash", "refs/stash"),
        ("tmp", "refs/tmp/pr1"),
        ("heads", "refs/heads/rescue-x"),
    ])
    def test_non_durable_namespaces_are_not_proof(self, wp, repo, tmp_path, ns, ref):
        # ADD-ONE over the namespaces someone would plausibly re-admit.
        # refs/heads is the highest-value member: it is the one just removed.
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
        # `==` so a namespace appended to the proof set has to come here first.
        assert (wp.PROOF_MAIN_REF, wp.PROOF_TAG_NAMESPACE, wp.PROOF_REMOTE_NAMESPACE) \
            == ("refs/remotes/origin/main", "refs/tags/", "refs/remotes/")


class TestInProgressRealGit:
    def test_marker_flag_is_repeated_per_marker(self, wp, repo, tmp_path):
        # `rev-parse --git-path a b c` is an ERROR, not a batch. Getting this
        # wrong makes every candidate look in-progress and silently no-ops the
        # whole loop, so assert against real git, not a stub.
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
        # A paused `rebase -i` is detached AND porcelain-clean, so gate_clean
        # cannot see it. This gate is the only thing that can.
        for i in (1, 2):
            (repo / f"f{i}").write_text(str(i))
            _git(repo, "add", f"f{i}")
            _git(repo, "commit", "-qm", f"c{i}")
        wt = tmp_path / "w"
        _git(repo, "worktree", "add", "-q", "-b", "rb", str(wt))
        # `sed -i ''` is BSD sed. GNU sed reads the '' as its script, the editor
        # fails, the rebase never pauses, and this test's whole premise is gone
        # — which is why it passed on macOS and failed on CI. A python editor
        # behaves identically on both.
        seq_editor = tmp_path / "seq_editor.py"
        seq_editor.write_text(
            "import sys\n"
            "path = sys.argv[1]\n"
            "lines = open(path, encoding='utf-8').readlines()\n"
            # The old sed was anchored (`2s/^pick/edit/`) and no-op'd on any
            # other line; say the assumption out loud instead of slicing blind.
            "assert lines[1].startswith('pick '), lines[1]\n"
            "lines[1] = 'edit' + lines[1][len('pick'):]\n"
            "open(path, 'w', encoding='utf-8').writelines(lines)\n")
        # git runs GIT_SEQUENCE_EDITOR through a shell, so both words are quoted:
        # a space in sys.executable or in tmp_path would otherwise split them.
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
        # Derived per member rather than three hand-written cases.
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
        # `==`, so adding an ignored name has to come through here — and through
        # the RED proof below — rather than being appended quietly.
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
        # Derived over the set, not three hand-picked cases: build output nests
        # (common/target/), so a top-level-only match would block most trees.
        assert wp.ignored_ok_from_porcelain(f"!! {name}\n") is True
        assert wp.ignored_ok_from_porcelain(f"!! service/{name}/\n") is True

    @pytest.mark.parametrize("entry", [
        "docs/", "docs/superpowers/specs/x-design.md", ".superpowers/",
        "notes.md", "service/src/main/graphql/sibi/schema.json", ".idea/",
    ])
    def test_unknown_ignored_content_blocks(self, wp, entry):
        assert wp.ignored_ok_from_porcelain(f"!! {entry}\n") is False

    def test_ignored_lines_are_not_dirt_for_gate_b(self, wp):
        # The two gates read one porcelain text; `!!` belongs to B2 only.
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
                return _RR(wp, 0, "!! target/\n!! docs/superpowers/specs/d.md\n")
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
        # `~/worktrees/*` unexpanded has prefix `~`, which is not a directory.
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
        # load_keeplist_text requires the ENTRY to exist, so create exactly the
        # path it is handed; keeplist_matches never requires the CANDIDATE to
        # exist, so the case difference lives there instead. Creating one case
        # and querying the other made this pass only on a case-INSENSITIVE
        # filesystem: green on APFS, keeplist_entry_missing on CI's ext4.
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
        # Holds are computed before the fetch loop, so by_reason["kept"] is the
        # true hold count rather than being masked by fetch_failed.
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
        # A worktree under a root sitting on main passes `is-ancestor` trivially,
        # so without this the loop deletes the clone's main branch.
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
        # `git worktree lock --reason "..."` emits the second form, and that is
        # the one an operator who cares actually uses.
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
        # git's own hint for a locked tree is "use 'remove -f -f' to override".
        # That single character is the guard; assert the RECORDED argv, because
        # both this script's docstring and the installer mention `remove -f`.
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


class TestTerminalityAndGhVisibility:
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

    @pytest.mark.parametrize("state,expected", [
        ("MERGED", True), ("CLOSED", True), ("OPEN", False),
    ])
    def test_pr_state_decides_terminality(self, wp, state, expected):
        def run(args, cwd=None):
            if args[0] == wp.GH_BIN:
                return _RR(wp, 0, _gh_body(args, state, "h"))
            return _RR(wp, 1)  # not an ancestor
        assert wp.gate_terminal(run, _cand(wp, branch="feat", head="h")) is expected

    def test_an_open_pr_beats_a_closed_one_on_the_same_head(self, wp):
        # `pr view` returns ONE pr; a head carrying both a stale CLOSED and a
        # live OPEN pr must not read as terminal.
        def run(args, cwd=None):
            if args[0] == wp.GH_BIN:
                return _RR(wp, 0, json.dumps([
                    {"number": 1, "state": "CLOSED", "headRefOid": "h"},
                    {"number": 2, "state": "OPEN", "headRefOid": "h"}]))
            return _RR(wp, 1)
        assert wp.gate_terminal(run, _cand(wp, branch="feat", head="h")) is False

    @pytest.mark.parametrize("gh", ["fail", "empty"])
    def test_ancestor_is_a_peer_signal_not_a_gh_fallback(self, wp, gh):
        # "no PR at all, but HEAD already on main" is removable today and must
        # stay removable — including when gh answers perfectly with [].
        def run(args, cwd=None):
            if args[0] == wp.GH_BIN:
                return _RR(wp, 1, "", "boom") if gh == "fail" else _RR(wp, 0, "[]")
            if "merge-base" in args:
                return _RR(wp, 0)  # IS an ancestor
            return _RR(wp, 0, "")
        assert wp.gate_terminal(run, _cand(wp, branch="feat", head="h")) is True

    def test_gh_failures_are_counted(self, wp, monkeypatch):
        # A gh failure is not a skip reason, so without this counter a broken gh
        # is indistinguishable from a repo full of unmerged branches.
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
        # The name still exists; the history under it no longer holds X. Checking
        # existence alone would confirm this, which is why the SHA is compared.
        run = self._run(wp, "rewritten\trefs/heads/gw/feat\n")
        assert wp.gate_contained(run, self._det(wp)) == (False, "remote_unconfirmed")

    def test_branch_deleted_server_side_is_caught(self, wp):
        # GitHub's delete-head-branch-on-merge shape: rc=0 with no rows.
        run = self._run(wp, "")
        assert wp.gate_contained(run, self._det(wp)) == (False, "remote_unconfirmed")

    def test_ls_remote_failure_is_signal_not_noise(self, wp):
        run = self._run(wp, "", server_rc=1)
        assert wp.gate_contained(run, self._det(wp)) == (False, "remote_unconfirmed")

    @pytest.mark.parametrize("ref,expected", [
        ("refs/remotes/origin/feat", ("origin", "feat")),
        ("refs/remotes/origin/gw/steal-phase-d", ("origin", "gw/steal-phase-d")),
        ("refs/remotes/upstream/copilot/tkt-8696-x", ("upstream", "copilot/tkt-8696-x")),
        ("refs/heads/feat", None),
    ])
    def test_remote_and_branch_split_on_the_first_slash(self, wp, ref, expected):
        # 15 of 17 remote-proven trees here carry a slash in the branch name, so
        # splitting on the last slash would query a branch that does not exist.
        assert wp._remote_and_branch(ref) == expected

    def test_head_pseudo_ref_is_never_used_as_proof(self, wp):
        # refs/remotes/origin/HEAD sorts before every branch name, and
        # `ls-remote --heads origin HEAD` returns rc=0 with no rows.
        def run(args, cwd=None):
            if "for-each-ref" in args:
                if wp.PROOF_REMOTE_NAMESPACE in args:
                    return _RR(wp, 0, "refs/remotes/origin/HEAD\n")
                return _RR(wp, 0, "")
            return _RR(wp, 0, "")
        assert wp.gate_contained(run, self._det(wp)) == (False, "uncontained")


class TestForensics:
    def test_removal_records_head_and_reflog(self, wp, monkeypatch, repo, tmp_path):
        # 223 removals before this change recorded no SHA at all. After
        # `worktree remove` the objects survive unreachable for ~2 weeks, so a
        # logged SHA is the difference between irreversible and recoverable.
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
        assert row["reason"] == "not_terminal"
        assert set(row) >= {"path", "action", "reason", "failed_gates", "branch",
                            "detached", "clone", "head", "locked", "reflog"}
        assert "terminal" in row["failed_gates"]
        assert snap["summary"]["gh_failed"] == 0


class TestCapabilityProbe:
    def test_tokens_derive_from_the_functions_that_implement_them(self, wp):
        assert wp.capabilities() == [t for t, _ in wp.CAPABILITY_IMPLS]

    def test_a_missing_implementation_drops_its_token(self, wp, monkeypatch):
        # The point of a behavioural probe: deleting the code deletes the token,
        # where a `grep keep.txt` over the file would still match the docstring.
        monkeypatch.delitem(wp.__dict__, "load_keeplist_text")
        assert "keeplist" not in wp.capabilities()


INSTALLER = REPO_ROOT / "deploy" / "scripts" / "worktree-prune-install.sh"


def _render_plist(tmp_path, env_extra):
    """Run the installer far enough to produce a plist, without touching launchd."""
    home = tmp_path / "home"
    (home / ".claude" / "scripts").mkdir(parents=True, exist_ok=True)
    (home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
    real = REPO_ROOT / "deploy" / "scripts" / "worktree_prune.py"
    (home / ".claude" / "scripts" / "worktree_prune.py").write_text(real.read_text())
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / "launchctl").write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / "launchctl").chmod(0o755)
    env = dict(os.environ, HOME=str(home),
               PATH=f"{fake_bin}:{os.environ['PATH']}", **env_extra)
    proc = _sp.run(["bash", str(INSTALLER)], capture_output=True, text=True, env=env)
    plists = list((home / "Library" / "LaunchAgents").glob("*.plist"))
    return proc, (plists[0].read_text() if plists else None)


def _plist_program_args(text):
    """The <string> elements of ProgramArguments — NOT a substring search over
    the file: the installer's own comments mention `--apply`."""
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
        # The dry-run first tick is the only measurement taken before the first
        # irreversible action, and it is impossible if the flag is hard-coded.
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
        # Behavioural probe. A grep would pass here, because the stub below still
        # carries the word `keep.txt` in its docstring.
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
        # `worktree-prune-install.sh` is documented as idempotent and its own
        # header shows bare re-runs. Dropping the binding on one would return the
        # loop to the 62-run defect with nothing reporting it.
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
    """One artifact-named directory must not launder the path around it.

    `_is_artifact_path` originally matched ANY segment, so a single `target/`,
    `out/` or `dist/` anywhere in an ignored path made the whole thing read as
    build output — reopening, inside the gate, the exact blind spot the gate
    exists to close.
    """

    @pytest.mark.parametrize("entry", [
        "docs/target/notes.md",
        "notes/out/plan.md",
        "src/dist/README-IMPORTANT.md",
        "docs/build/spec-v2.md",
        "planning/coverage/decisions.md",
        "docs/superpowers/specs/design.md",
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
        # `git status --ignored` collapses a wholly-ignored directory, so the
        # shapes that actually occur carry the name first or last, never buried.
        assert wp._is_artifact_path(entry) is True

    def test_only_the_first_or_last_segment_is_consulted(self, wp):
        # Pins the rule itself: a name in the middle is inert in both directions.
        assert wp._is_artifact_path("target/x/y") is True      # first
        assert wp._is_artifact_path("x/y/target") is True      # last
        assert wp._is_artifact_path("x/target/y") is False     # middle only


class TestBranchDeleteProof:
    """The proof that governs `git branch -D`, which the split must NOT widen.

    Removal only needs the work terminal — the branch ref survives it. Deleting
    that ref destroys commits, so it keeps the original, stricter proof. A CLOSED
    (abandoned, never merged) PR is terminal and proves nothing.
    """

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
    def test_pr_state_and_head_sha_decide_branch_deletion(
            self, wp, monkeypatch, label, state, oid, expect_delete):
        calls, info = self._drive(wp, monkeypatch, state, oid)
        assert info["summary"]["removed"] == 1, f"{label}: removal is terminal-gated"
        deleted = any(_is_branch_delete(a) for a in calls)
        assert deleted is expect_delete, f"{label}: branch -D should be {expect_delete}"

    def test_a_closed_pr_removes_the_tree_but_keeps_the_branch(self, wp, monkeypatch):
        # The whole point of the split, stated as one case.
        calls, info = self._drive(wp, monkeypatch, "CLOSED", "h")
        assert info["summary"]["removed"] == 1
        assert not any(_is_branch_delete(a) for a in calls)
        assert _ledger_by_event(wp).get("branch_delete_refused")

    def test_no_pr_but_ancestor_still_deletes(self, wp, monkeypatch):
        calls, _info = self._drive(wp, monkeypatch, None, None, ancestor=True)
        assert any(_is_branch_delete(a) for a in calls)

    def test_no_pr_and_not_ancestor_removes_nothing(self, wp, monkeypatch):
        _calls, info = self._drive(wp, monkeypatch, None, None, ancestor=False)
        assert info["summary"]["removed"] == 0

    def test_gate_merged_is_reachable_from_production(self, wp, monkeypatch):
        # The defect this class exists for: gate_merged was live, tested, and
        # called from nowhere, so 9 green tests implied a proof that was unwired.
        seen = []
        real = wp.gate_merged
        monkeypatch.setattr(wp, "gate_merged",
                            lambda run, cand: (seen.append(cand.path), real(run, cand))[1])
        self._drive(wp, monkeypatch, "MERGED", "h")
        assert seen, "gate_merged must be consulted before `branch -D`"

    def test_gate_merged_uses_the_named_gh_binary(self, wp, monkeypatch):
        # GH_BIN defaults to the literal "gh", so asserting argv[0] == GH_BIN is
        # vacuous unless GH_BIN is bound to something else first: a hard-coded
        # "gh" would satisfy it forever. Bind a distinctive path.
        monkeypatch.setattr(wp, "GH_BIN", "/opt/distinct-gh")
        seen = {}

        def run(args, cwd=None):
            seen.setdefault("argv", args)
            return _RR(wp, 1)

        wp.gate_merged(run, _cand(wp, branch="feat", head="h", clone="/c"))
        assert seen["argv"][0] == "/opt/distinct-gh"


class TestKeepListHardening:
    def test_a_glob_that_matches_nothing_stops_the_run(self, wp, tmp_path):
        # An existing PREFIX is not enough: `TKT-9855-*` (one hyphen too many)
        # has a real prefix and holds nothing, which is the dangerous direction.
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
        # Case-insensitivity and directory-containment are documented as
        # composing; testing each axis alone leaves the crossing unguarded.
        # Same fixture rule as test_matching_is_case_insensitive: the entry is
        # the path actually created, and the candidate carries the case
        # difference, so neither side needs a case-insensitive filesystem.
        held = tmp_path / "worktrees" / "tkt-9855"
        held.mkdir(parents=True)
        entries, fatal = wp.load_keeplist_text(str(held))
        assert fatal is None
        child = tmp_path / "worktrees" / "TKT-9855" / "acme-communication-service"
        assert wp.keeplist_matches(entries, str(child)) is True


class TestReflogIdentity:
    def test_reflog_is_read_from_the_worktrees_own_gitdir(self, wp, repo, tmp_path):
        # Git de-duplicates the admin id with a numeric suffix, and this layout
        # gives many worktrees the same basename, so deriving the path reads
        # ANOTHER tree's reflog and reports a complete-looking wrong SHA list.
        a = tmp_path / "t1" / "repo"
        b = tmp_path / "t2" / "repo"
        a.parent.mkdir(parents=True)
        b.parent.mkdir(parents=True)
        _git(repo, "worktree", "add", "-q", "-b", "fa", str(a))
        _git(repo, "worktree", "add", "-q", "-b", "fb", str(b))
        # Distinct, non-colliding names: the fixture already holds `a.txt`, and
        # APFS is case-insensitive, so `A.txt` would overwrite it rather than
        # create a new file.
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
        # The in-memory summary is the one surface no operator sees. The plist
        # runs without --json, so stdout, check.log and last-scan.json are it.
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
        # The superproject's `status --porcelain --ignored` is EMPTY for this
        # shape — a submodule is a separate repository and --ignored does not
        # descend. Every other submodule state surfaces as ` M sub` already.
        # Feeding the gate a porcelain string cannot catch this; only a real repo can.
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
        # Testing gate_ignored directly leaves the wiring in `_scan` unguarded:
        # `_scan` computes ignored_ok itself rather than calling gate_ignored.
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
    """Everything the delete step needs must be computed while the tree EXISTS.

    `gate_merged` shells out with cwd = the worktree and `_worktree_reflog_shas`
    reads that worktree's gitdir. After `remove --force` both fail rc!=0 on the
    missing directory — silently, in the safe direction. A stub `run` that
    ignores `cwd` is green through all of it, which is why these use a `run` that
    honours cwd the way `_default_run` does.
    """

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
                return _RR(wp, 1)          # squash-merged: never an ancestor
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
        # The inverted defect: with the proof evaluated after `remove`, gh can
        # never answer, every branch falls through to `is-ancestor`, and a
        # squash-merged branch is never an ancestor — so the loop would refuse
        # nearly every branch while logging it as intentional.
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

    def test_closed_pr_branch_still_refused_with_a_cwd_honouring_run(self, wp, monkeypatch):
        # The same ordering must not accidentally start deleting CLOSED branches.
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
        assert info["summary"]["removed"] == 1
        assert not any(_is_branch_delete(a) for a in calls)

    def test_removed_rows_keep_the_pre_removal_reflog(self, wp, monkeypatch, repo, tmp_path):
        # last-scan.json is the file deploy step 2 tells the operator to read.
        # Recomputing the reflog after removal records [] for exactly the trees
        # that were destroyed.
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
                return _RR(wp, 0)          # the fixture clone has no remote
            if "merge-base" in args:
                return _RR(wp, 0)          # treat as merged; gh agrees above
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
        # `logs/HEAD`'s creation line carries 0000…0000, which passes a bare
        # 40-hex-char test and would sit in a list presented as recoverable.
        wt = tmp_path / "w"
        _git(repo, "worktree", "add", "-q", "-b", "f", str(wt))
        cand = wp.Candidate(path=str(wt), head="", branch="f", detached=False,
                            clone=str(repo))
        shas = wp._worktree_reflog_shas(_real_run(wp), cand)
        assert shas, "precondition: the reflog is non-empty"
        assert "0" * 40 not in shas

    def test_a_failed_removal_still_records_the_shas(self, wp, monkeypatch):
        # `remove --force` can delete the directory and still exit non-zero, so
        # the SHAs must be logged on this path too — they are already in hand.
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
    """N1: the composition lived in three places and only one was exercised.

    The apply-time copy is the last look before the destructive command — the one
    that catches content appearing in the scan→apply window, which is exactly when
    a worker writes a design doc. Removing it left the whole suite green.
    """

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
                    return _RR(wp, 0, "")      # clean at scan, dirty at apply
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
                # The SUPERPROJECT half arrives as check_ignored's `text` arg, so
                # a stale-text caller is invisible unless this differs too.
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
        # A worker writing docs/superpowers/specs/x-design.md between the scan
        # and the removal: gitignored, in the SUPERPROJECT, not a submodule.
        _d, info = self._drive(
            wp, monkeypatch,
            status_at_apply="!! docs/superpowers/specs/x-design.md\n")
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
        # Only the apply-time copy can see this; the scan said clean.
        _d, info = self._drive(wp, monkeypatch, "!! sub/notes/\n", at_apply_only=True)
        row = {r["path"]: r for r in info["results"]}["/wt"]
        assert row["action"] == "SKIP"
        assert row["reason"].startswith("toctou_")
        assert info["summary"]["removed"] == 0

    def test_a_broken_sweep_is_not_reported_as_successful_protection(self, wp, monkeypatch):
        # N2: `ignored_content` reads as "the gate worked". A sweep that could not
        # RUN must say so, or the widening goes dead while last-scan.json looks fine.
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
        # Pins the generator, not the instances: if a call site stops using
        # check_ignored, this counts fewer visits than there are gate evaluations.
        calls = []
        real = wp.check_ignored
        monkeypatch.setattr(wp, "check_ignored",
                            lambda *a, **k: (calls.append(1), real(*a, **k))[1])
        self._drive(wp, monkeypatch, "!! sub/x/\n", apply=False)
        assert calls, "the scan must consult check_ignored"


class TestHoldListDocsMatchTheRule:
    def test_the_shipped_template_does_not_teach_a_loop_stopping_example(self, wp, tmp_path):
        # The installer writes keep.txt only if absent, so whatever ships is
        # permanent for anyone installing from this commit. Every uncommented
        # example must satisfy the rule the code enforces.
        _proc, _plist = _render_plist(tmp_path, {})
        keep = tmp_path / "home" / ".claude" / "dockwright" / "worktree-prune" / "keep.txt"
        body = keep.read_text()
        live = [l.strip() for l in body.splitlines()
                if l.strip() and not l.strip().startswith("#")]
        assert live == [], "the shipped template must be comments only"
        entries, fatal = wp.load_keeplist_text(body)
        assert (entries, fatal) == ([], None), \
            "the shipped template must not stop the loop as written"

    def test_the_template_states_the_matches_nothing_rule(self, tmp_path):
        _proc, _plist = _render_plist(tmp_path, {})
        keep = tmp_path / "home" / ".claude" / "dockwright" / "worktree-prune" / "keep.txt"
        body = keep.read_text().lower()
        assert "matches no existing path" in body or "match something that exists" in body
        assert "parent directory is not enough" in body



class TestFailedGatesFidelity:
    """`failed_gates` must agree with `reason`, for every gate shape.

    `decide` treats any tuple-valued gate as carrying its own reason. The loop
    that builds `failed_gates` reads the same dict, so it has to enumerate the
    same way — classifying by gate NAME meant a failing tuple gate (truthy!) was
    recorded as a pass, and the row contradicted itself on the gate carrying the
    most weight.
    """

    def _row(self, wp, monkeypatch, gates, cand=None):
        cand = cand or _cand(wp, path="/wt", branch="feat", head="h", clone="/clone")
        monkeypatch.setattr(wp, "enumerate_candidates", lambda r, cp, roots: [cand])
        monkeypatch.setattr(wp, "_load_active_records", lambda d: [])
        monkeypatch.setattr(wp, "_collect_lsof_cwds", lambda r: [])
        # Drive the gate values directly: this is about the bookkeeping, not the
        # gates, and forcing each real gate would not reach every shape.
        monkeypatch.setattr(wp, "_porcelain", lambda r, c: "")
        monkeypatch.setattr(wp, "clean_from_porcelain", lambda x: gates["clean"])
        monkeypatch.setattr(wp, "check_ignored",
                            lambda r, c, text=None: gates["ignored_ok"])
        monkeypatch.setattr(wp, "gate_in_progress",
                            lambda r, c: gates["in_progress_clear"])
        monkeypatch.setattr(wp, "gate_contained", lambda r, c: gates["contained"])
        monkeypatch.setattr(wp, "gate_terminal",
                            lambda r, c, s=None: gates["terminal"])
        monkeypatch.setattr(wp, "gate_merged", lambda r, c: True)
        monkeypatch.setattr(wp, "gate_unowned",
                            lambda c, a, l, s: gates["unowned"])

        def run(args, cwd=None):
            rr = _infra_rr(wp, args)
            return rr if rr is not None else _RR(wp, 0, "")

        _d, info = wp.run_prune(NOW, apply=False, run=run, clone_parents=["/cp"],
                                roots=["/r"], self_path="/elsewhere")
        return {r["path"]: r for r in info["results"]}["/wt"]

    def _pass_all(self):
        return {"clean": True, "ignored_ok": (True, None), "in_progress_clear": True,
                "contained": (True, None), "terminal": True, "unowned": True}

    # Derived from the gate set, not hand-listed — the promise the comment used
    # to make while the decorator quietly omitted `contained`, the original
    # tuple-valued gate and the very shape this class exists for. `kept` is
    # excluded because it is recorded on the passing branch, not the failing one.
    @pytest.mark.parametrize("key", sorted(set(ALL_PASS) - {"kept"}))
    def test_every_failing_gate_appears_in_failed_gates(self, wp, monkeypatch, key):
        gates = self._pass_all()
        cand = None
        if key == "contained":
            # _scan short-circuits containment for a branch worktree, so the
            # patched gate can only take effect on a detached candidate.
            cand = wp.Candidate(path="/wt", head="h", branch=None, detached=True,
                                clone="/clone")
            gates["contained"] = (False, "uncontained")
        elif key == "not_locked":
            # Comes from the Candidate, not from a patchable function.
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
        # Two gates failing at once: the reported reason must be among the misses.
        gates = self._pass_all()
        gates["ignored_ok"] = (False, "ignored_content")
        gates["unowned"] = False
        row = self._row(wp, monkeypatch, gates)
        assert row["reason"] == "ignored_content"
        assert "ignored_ok" in row["failed_gates"]
        assert "unowned" in row["failed_gates"]

    def test_contained_has_a_meaningful_default_reason(self, wp):
        # `None` would fall through to "error", which is the string _scan's
        # except-handler produces — a real failure and a crash would be
        # indistinguishable in by_reason.
        defaults = dict(wp.DECIDE_ORDER)
        assert defaults["contained"] == "uncontained"
        assert wp.decide(_cand(wp), {**{k: True for k, _ in wp.DECIDE_ORDER},
                                     "kept": False,
                                     "contained": (False, None)}) == ("SKIP", "uncontained")
