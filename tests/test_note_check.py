import fcntl
import os
import subprocess
import sys
import time

import pytest

from dockwright import note_check, paths, state, topic
from dockwright.mcp_server import artifact_put_impl
from tests.test_pipeline_advance import fresh_orchestrator_dir

HOME = os.path.expanduser("~")
NOW = 1_800_000_000.0


def _ok(argvs, cwd):
    return "ok", 0, ""


def _note(key, ask, *, status="open", at=None, worktree=None):
    artifact_put_impl(key, "context", "ask", ask, "complete", "cli:test", scope="output")
    card = topic.new_card("note", "meta", key, worktree=worktree, status=status)
    if at is not None:
        card["last_check"] = {"at": at, "runs": []}
    state.write_json_atomic(paths.topic_path(key), card)


@pytest.mark.parametrize("segment", [
    "gh pr merge 1", "gh pr view 1 --web", "gh pr view 1 --repo -x", "gh api --method=DELETE repos/o/r",
    "gh api -iXDELETE repos/o/r", "gh api x '--method=DELETE'", "gh api -f a=b repos/o/r", "gh api repos/o/r extra",
    "gh api 'repos/{owner}/r'", "gh -R o/r pr view 1", "git grep -Ocmd x", "git ls-remote --upload-pack=cmd origin",
    "git -c core.pager=x log", "git -C -x log", "git push", "git ls-remote 'ext::sh -c touch% /tmp/pwn'",
    "git ls-remote foo::bar", "git ls-remote ext://sh", "git ls-remote zzq://host/x", "git ls-remote ../repo",
    "git ls-remote git@host:o/r", "ls; rm x", "ls > out", "ls & ls", "echo $(id)",
    "ls\nrm x", "tail -f log", "tail -F log", "head -n -5 f", "head -n5 f", "cat /dev/zero", "cat /dev/../dev/zero",
    "cat --", "ls -", "python3 s.py", "grep x f | rm y", "ls |", "| ls", "pipeline_status", "pipeline_status a b",
    "dockwright pipeline status a/b", "dockwright pipeline advance K", "GH_HOST=x gh pr view 1", "ls 'unclosed",
])
def test_a_command_off_the_list_is_refused(segment, tmp_path):
    with pytest.raises(note_check.Refused):
        note_check.parse(segment, str(tmp_path))


@pytest.mark.parametrize("segment,argvs", [
    ("gh pr view 1 --repo o/r --json state", [["gh", "pr", "view", "1", "--repo", "o/r", "--json", "state"]]),
    ("gh pr list -R o/r --state open --json number --jq .[].number",
     [["gh", "pr", "list", "-R", "o/r", "--state", "open", "--json", "number", "--jq", ".[].number"]]),
    ("gh api repos/o/r/pulls/7 --jq .state", [["gh", "api", "repos/o/r/pulls/7", "--jq", ".state"]]),
    ("ls ~/x | grep '^a'", [["ls", f"{HOME}/x"], ["grep", "^a"]]),
    ("ls -la", [["ls", "-la"]]),
    ("tail -5", [["tail", "-5"]]),
    ("tail -n 5 f", [["tail", "-n", "5", "f"]]),
    ("ls a*.log", [["ls", "a*.log"]]),
    ("git -C ~/r log --oneline -3", [["git", "-C", f"{HOME}/r", "log", "--oneline", "-3"]]),
    ("git status --porcelain", [["git", "status", "--porcelain"]]),
    ("git ls-remote --heads origin main", [["git", "ls-remote", "--heads", "origin", "main"]]),
    ("git grep -n Foo::bar", [["git", "grep", "-n", "Foo::bar"]]),
    ("test -f ~/x", [["test", "-f", f"{HOME}/x"]]),
    ("pipeline_status K", [[sys.executable, "-P", "-m", "dockwright", "pipeline", "status", "K", "--json"]]),
    ("dockwright pipeline status K --json", [[sys.executable, "-P", "-m", "dockwright", "pipeline", "status", "K",
                                              "--json"]]),
])
def test_a_listed_read_passes_as_the_exact_argv(segment, argvs, tmp_path):
    assert note_check.parse(segment, str(tmp_path)) == argvs


def test_commands_come_from_the_first_check_line_only():
    ask = "## [ ] A\n- when: x\n- check: `a` then `b` `c` `d` `e` `f`\ncheck: `z`\n"
    assert note_check.commands_of(ask) == ["a", "b", "c", "d", "e"]
    assert note_check.commands_of("## [ ] A\ncheck: `z`\n") == ["z"]
    assert note_check.commands_of("## [ ] A\n**check:** `git log --oneline -5`\n") == ["git log --oneline -5"]
    assert note_check.commands_of("## [ ] A\n- **check:** `ls`\n") == ["ls"]
    assert note_check.commands_of("## [ ] A\n- when: x\n") == []


def test_the_executor_times_out(tmp_path):
    started = time.monotonic()
    status, code, _ = note_check.execute([["sleep", "5"]], None, timeout=0.3)
    assert (status, code) == ("timeout", None) and time.monotonic() - started < 3


def test_the_executor_caps_the_output():
    started = time.monotonic()
    status, code, output = note_check.execute(
        [[sys.executable, "-c", "import sys; sys.stdout.write('x' * 400000); sys.stdout.flush()"]], None)
    assert (status, code) == ("ok", 0) and output == "x" * note_check.RECORD_CHARS
    assert time.monotonic() - started < 10


def test_the_executor_gives_no_stdin():
    assert note_check.execute([["cat"]], None, timeout=5) == ("ok", 0, "")


def test_the_executor_reports_a_missing_program():
    status, code, output = note_check.execute([["/nonexistent/prog-a1"]], None)
    assert (status, code) == ("error", None) and output


def test_a_due_note_records_each_command(fresh_orchestrator_dir, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "marker.txt").write_text("m")
    _note("note-meta-a", "## [ ] A\n- check: `ls` and `python3 s.py`\n", worktree=str(wt))
    assert note_check.run(NOW) == ["note-meta-a"]
    record = topic.read("note-meta-a")["last_check"]
    assert record["at"] == NOW
    assert [(r["cmd"], r["status"]) for r in record["runs"]] == [("ls", "ok"), ("python3 s.py", "refused")]
    assert record["runs"][0]["exit"] == 0 and "marker.txt" in record["runs"][0]["output"]
    assert record["runs"][1]["exit"] is None and record["runs"][1]["output"]


def test_a_failing_stage_fails_the_command_and_a_glob_stays_literal(fresh_orchestrator_dir, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    _note("note-meta-a", "check: `grep x a*.log | wc -l`\n", worktree=str(wt))
    note_check.run(NOW)
    run = topic.read("note-meta-a")["last_check"]["runs"][0]
    assert run["status"] == "failed" and run["exit"] == 2 and "a*.log" in run["output"]


def test_a_prose_check_records_no_runs(fresh_orchestrator_dir):
    _note("note-meta-b", "## [ ] B\ncheck: see whether the PR merged\n")
    note_check.run(NOW)
    assert topic.read("note-meta-b")["last_check"] == {"at": NOW, "runs": []}


def test_only_open_notes_an_hour_stale_are_checked(fresh_orchestrator_dir):
    _note("note-meta-fresh", "check: `ls`\n", at=NOW - 100)
    _note("note-meta-stale", "check: `ls`\n", at=NOW - 3600)
    _note("note-meta-closed", "check: `ls`\n", status="closed")
    state.write_json_atomic(paths.topic_path("K"), topic.new_card("code-ticket", "meta", "w"))
    assert note_check.run(NOW, runner=_ok) == ["note-meta-stale"]


def test_a_note_another_manager_holds_is_skipped(fresh_orchestrator_dir):
    _note("note-meta-a", "check: `ls`\n")
    with open(paths.artifact_ticket_dir("note-meta-a") / note_check.LOCK_NAME, "a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        assert note_check.run(NOW, runner=_ok) == []
    assert note_check.run(NOW, runner=_ok) == ["note-meta-a"]


def test_no_note_starts_after_the_budget(fresh_orchestrator_dir):
    for key in ("note-meta-a", "note-meta-b", "note-meta-c"):
        _note(key, "check: `ls`\n")
    ticks = iter(range(0, 1000, 15))
    assert note_check.run(NOW, budget_sec=20, runner=_ok, clock=lambda: next(ticks)) == ["note-meta-a"]


def test_a_failing_note_is_named_and_does_not_starve_the_others(fresh_orchestrator_dir, monkeypatch):
    _note("note-meta-a", "check: `ls`\n")
    _note("note-meta-b", "check: `ls`\n")
    body = topic.artifact_body

    def flaky(key, phase, name):
        if key == "note-meta-a":
            raise OSError("boom")
        return body(key, phase, name)

    monkeypatch.setattr(topic, "artifact_body", flaky)
    with pytest.raises(RuntimeError, match="note-meta-a: OSError: boom"):
        note_check.run(NOW, runner=_ok)
    assert "last_check" not in topic.read("note-meta-a")
    assert topic.read("note-meta-b")["last_check"]["runs"][0]["status"] == "ok"


def test_a_command_that_crashes_is_recorded_and_the_note_is_not_due_again(fresh_orchestrator_dir, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    _note("note-meta-a", "check: `ls` `cat a\x00b`\n", worktree=str(wt))
    calls = []

    def counted(argvs, cwd):
        calls.append(argvs)
        return note_check.execute(argvs, cwd)

    assert note_check.run(NOW, runner=counted) == ["note-meta-a"]
    runs = topic.read("note-meta-a")["last_check"]["runs"]
    assert [r["status"] for r in runs] == ["ok", "error"] and "null" in runs[1]["output"]
    assert note_check.run(NOW + 60, runner=counted) == [] and len(calls) == 1


def test_a_program_a_command_starts_never_comes_from_a_relative_path_entry(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    marker = tmp_path / "hijacked"
    _write_program(wt / "bin" / "ls", marker)
    monkeypatch.setenv("PATH", "bin:" + os.environ["PATH"])
    status, _, _ = note_check.execute([[sys.executable, "-c", "import subprocess; subprocess.run(['ls'])"]], str(wt))
    assert status == "ok" and not marker.exists()


def test_the_oldest_check_goes_first(fresh_orchestrator_dir):
    _note("note-meta-a", "check: `ls`\n", at=NOW - 5000)
    _note("note-meta-b", "check: `ls`\n", at=NOW - 9000)
    assert note_check.due(NOW) == ["note-meta-b", "note-meta-a"]


def _write_program(path, marker):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\ntouch {marker}\n")
    path.chmod(0o755)


def test_a_module_in_the_check_directory_never_replaces_dockwright(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    wt = tmp_path / "wt"
    marker = tmp_path / "hijacked"
    (wt / "dockwright").mkdir(parents=True)
    (wt / "dockwright" / "__init__.py").write_text("")
    (wt / "dockwright" / "__main__.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    note_check.execute(note_check.parse("pipeline_status K", str(wt)), str(wt))
    assert not marker.exists()


def test_a_relative_path_entry_never_picks_the_program(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    marker = tmp_path / "hijacked"
    _write_program(wt / "bin" / "ls", marker)
    monkeypatch.setenv("PATH", "bin:" + os.environ["PATH"])
    status, _, _ = note_check.execute(note_check.parse("ls", str(wt)), str(wt))
    assert status == "ok" and not marker.exists()


def test_a_remote_named_by_the_repo_never_reaches_a_helper_transport(tmp_path):
    repo = tmp_path / "repo"
    marker = tmp_path / "hijacked"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "protocol.ext.allow", "always"], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", f"ext::sh -c touch% {marker}"], check=True)
    note_check.execute(note_check.parse("git ls-remote origin", str(repo)), str(repo))
    assert not marker.exists()


def test_a_segment_with_more_stages_than_the_cap_is_refused(tmp_path):
    under = " | ".join(["ls"] + ["cat"] * (note_check.MAX_STAGES - 1))
    assert len(note_check.parse(under, str(tmp_path))) == note_check.MAX_STAGES
    with pytest.raises(note_check.Refused):
        note_check.parse(under + " | cat", str(tmp_path))


def test_a_device_path_is_refused_in_any_letter_case(tmp_path):
    with pytest.raises(note_check.Refused):
        note_check.parse("cat /DEV/zero", str(tmp_path))


def test_an_upstream_stage_its_reader_stopped_does_not_fail_the_command(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text(("x" * 80 + "\n") * 100000)
    assert note_check.execute([["cat", str(big)], ["head", "-n", "3"]], None)[:2] == ("ok", 0)


def test_an_upstream_stage_that_fails_still_fails_the_command(tmp_path):
    status, code, _ = note_check.execute([["ls", str(tmp_path / "missing")], ["wc", "-l"]], None)
    assert status == "failed" and code not in (0, None)


def test_no_command_starts_after_the_budget(fresh_orchestrator_dir):
    _note("note-meta-a", "check: `ls` `ls -a` `ls -l`\n")
    calls = []

    def counted(argvs, cwd):
        calls.append(argvs)
        return "ok", 0, ""

    ticks = iter(range(0, 1000, 15))
    note_check.run(NOW, budget_sec=20, runner=counted, clock=lambda: next(ticks))
    runs = topic.read("note-meta-a")["last_check"]["runs"]
    assert len(calls) == 1 and [r["status"] for r in runs] == ["ok", "timeout", "timeout"]
    assert runs[1]["exit"] is None and "budget" in runs[1]["output"]


def _fake_gh(tmp_path):
    gh = tmp_path / "gh"
    gh.write_text('#!/bin/sh\nif [ "$1" = auth ]; then echo "tok-$6"; exit 0; fi\necho "token=${GH_TOKEN:-none} args=$*"\n')
    gh.chmod(0o755)
    return gh


@pytest.mark.parametrize("segment,owner", [
    ("gh pr view 123 --repo acme/widgets --json state", "acme"),
    ("gh pr view 861 -R acme/widgets --json state", "acme"),
    ("gh api repos/acme/widgets/pulls/1", "acme"),
    ("gh api /repos/bigco/widgets/pulls/1", "bigco"),
    ("gh pr view https://github.com/acme/widgets/pull/861 --json state", "acme"),
])
def test_a_gh_check_runs_under_the_owners_login_when_gh_is_configured(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                     segment, owner):
    from dockwright import config, gh_cli
    gh = _fake_gh(tmp_path)
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(f'[gh]\nbinary = "{gh}"\n[gh.logins]\nbigco = "work-login"\nacme = "alice"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    gh_cli._TOKENS.clear()
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    _note("note-meta-g", f"check: `{segment}`\n")
    note_check.run(NOW)
    run = topic.read("note-meta-g")["last_check"]["runs"][0]
    login = "work-login" if owner == "bigco" else "alice"
    assert run["status"] == "ok" and f"token=tok-{login}" in run["output"]


def test_a_gh_check_with_no_owner_in_its_argv_runs_as_today(fresh_orchestrator_dir, tmp_path, monkeypatch):
    from dockwright import config
    gh = _fake_gh(tmp_path)
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(f'[gh]\nbinary = "{gh}"\n[gh.logins]\nacme = "alice"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    _note("note-meta-h", "check: `gh pr view 7 --json state`\n")
    note_check.run(NOW)
    run = topic.read("note-meta-h")["last_check"]["runs"][0]
    assert run["status"] == "ok" and "token=none" in run["output"]
