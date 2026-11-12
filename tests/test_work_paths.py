import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dockwright import mcp_server, paths, pipeline, state, topic
from dockwright.mcp_server import artifact_put_impl, pipeline_advance_impl, pipeline_close_impl
from tests.test_pipeline_core import FORK
from tests.test_pipeline_reconcile import (_complete_design_outputs, _fake_registration,  # noqa: F401
                                           _started_at_design, fresh_orchestrator_dir, killed,
                                           session_logs)

REPO_ROOT = Path(__file__).resolve().parent.parent
ROWS = ('{"tasks": [{"id": 1, "row": "contracts", "cwd": "/r/K-grp/contracts"}, '
        '{"id": 2, "row": "web", "stage": 2, "cwd": "/r/K-grp/web"}]}')


def _dead(*sids):
    for sid in sids:
        (paths.ACTIVE / f"{sid}.json").unlink(missing_ok=True)


def _cli(capsys, *args):
    code = pipeline.main(["active-paths", *args])
    out, err = capsys.readouterr()
    return code, out, err


def _works(capsys):
    code, out, err = _cli(capsys, "--json")
    assert code == 0, err
    payload = json.loads(out)
    assert payload["version"] == 1
    return {w["task_key"]: w["paths"] for w in payload["works"]}


def _refused(capsys):
    code, out, err = _cli(capsys, "--json")
    assert code == 1
    assert out == ""
    assert err.strip()


def _put_plan(sid, body):
    artifact_put_impl("K", "plan", "tasks", body, "complete", sid, scope="output", overwrite=True)


def _break_plan(sid):
    _put_plan(sid, ROWS)
    p = paths.artifact_path("K", "plan", "tasks")
    p.write_text(p.read_text().replace(ROWS, "not json {"))
    assert "not json {" in p.read_text()


def _finished(monkeypatch, tmp_path):
    _started_at_design(monkeypatch, tmp_path, cwd="/r/K-grp/svc")
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "post-merge", override=True, reason="jump"))
    artifact_put_impl("K", "summary", "post-merge", "done", "complete", "sid-K-post-merge",
                      scope="output")


def test_a_started_work_claims_its_start_cwd(fresh_orchestrator_dir, tmp_path, monkeypatch, capsys):
    _started_at_design(monkeypatch, tmp_path, cwd="/r/K-grp/svc")
    assert _works(capsys) == {"K": ["/r/K-grp/svc"]}


def test_an_advance_to_a_new_cwd_is_claimed(fresh_orchestrator_dir, tmp_path, monkeypatch, capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path, cwd="/r/K-grp/svc")
    _complete_design_outputs(sid)
    moved = tmp_path / "moved"
    moved.mkdir()
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task", cwd=str(moved)))
    assert {"/r/K-grp/svc", str(moved)} <= set(_works(capsys)["K"])


def test_every_row_of_a_forked_plan_is_claimed(fresh_orchestrator_dir, tmp_path, monkeypatch, capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path, cwd="/r/K-grp/svc", schema_text=FORK)
    for name, body in (("context.ask", "x"), ("design.hld", "x"), ("plan.tasks", ROWS)):
        phase, n = name.split(".", 1)
        artifact_put_impl("K", phase, n, body, "complete", sid, scope="output", overwrite=True)
    _fake_registration(monkeypatch, ["registered"] * 3)
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    assert {"/r/K-grp/svc", "/r/K-grp/contracts", "/r/K-grp/web"} <= set(_works(capsys)["K"])


def test_plan_rows_of_an_archived_round_are_claimed(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                    capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path, cwd="/r/K-grp/svc")
    _put_plan(sid, ROWS)
    mcp_server._archive_round("K", 1, set())
    assert not paths.artifact_path("K", "plan", "tasks").exists()
    assert {"/r/K-grp/contracts", "/r/K-grp/web"} <= set(_works(capsys)["K"])


def test_a_pending_assignment_of_the_work_is_claimed(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                     capsys):
    _started_at_design(monkeypatch, tmp_path, cwd="/r/K-grp/svc")
    state.write_json_atomic(paths.pending_assignment_path("a-late"),
                            {"assignment_id": "a-late", "ticket": "K", "cwd": "/r/late-grp/svc"})
    paths.pending_window_path("a-late").write_text("123")
    state.write_json_atomic(paths.pending_assignment_path("a-other"),
                            {"assignment_id": "a-other", "ticket": "OTHER", "cwd": "/r/other/svc"})
    works = _works(capsys)
    assert "/r/late-grp/svc" in works["K"]
    assert "/r/other/svc" not in works["K"] and "OTHER" not in works


def test_a_terminal_work_is_listed_until_its_output_is_complete(fresh_orchestrator_dir, tmp_path,
                                                                monkeypatch, capsys):
    _started_at_design(monkeypatch, tmp_path, cwd="/r/K-grp/svc")
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "post-merge", override=True, reason="jump"))
    assert "K" in _works(capsys)
    artifact_put_impl("K", "summary", "post-merge", "done", "complete", "sid-K-post-merge",
                      scope="output")
    assert "K" not in _works(capsys)


def test_a_closed_work_is_not_listed(fresh_orchestrator_dir, tmp_path, monkeypatch, capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path, cwd="/r/K-grp/svc")
    _dead(sid)
    pipeline_close_impl("m1", "K", "absorbed elsewhere")
    assert _works(capsys) == {}


def test_a_finished_work_with_a_broken_schema_snapshot_is_listed(fresh_orchestrator_dir, tmp_path,
                                                                 monkeypatch, capsys):
    _finished(monkeypatch, tmp_path)
    assert "K" not in _works(capsys)
    (paths.artifact_ticket_dir("K") / "schema.toml").write_text("not = [valid")
    assert "/r/K-grp/svc" in _works(capsys)["K"]


def test_a_started_work_whose_event_log_is_gone_is_listed(fresh_orchestrator_dir, tmp_path,
                                                          monkeypatch, capsys):
    _started_at_design(monkeypatch, tmp_path, cwd="/r/K-grp/svc")
    paths.artifact_events_path("K").unlink()
    assert "K" in _works(capsys)


def test_a_started_work_whose_event_log_is_a_directory_is_listed(fresh_orchestrator_dir, tmp_path,
                                                                 monkeypatch, capsys):
    _started_at_design(monkeypatch, tmp_path, cwd="/r/K-grp/svc")
    paths.artifact_events_path("K").unlink()
    paths.artifact_events_path("K").mkdir()
    assert "K" in _works(capsys)


def test_a_work_whose_event_log_holds_no_start_is_listed(fresh_orchestrator_dir, tmp_path,
                                                         monkeypatch, capsys):
    _started_at_design(monkeypatch, tmp_path, cwd="/r/K-grp/svc")
    paths.artifact_events_path("K").write_text(
        json.dumps({"type": "artifact_put", "phase": "context", "name": "ask"}) + "\n")
    assert "K" in _works(capsys)


def test_a_started_work_with_no_schema_snapshot_claims_its_start_cwd(fresh_orchestrator_dir,
                                                                     capsys):
    paths.artifact_ticket_dir("K").mkdir(parents=True)
    paths.artifact_events_path("K").write_text(
        json.dumps({"type": "pipeline_started", "node": "design", "cwd": "/r/K-grp/svc"}) + "\n")
    assert _works(capsys) == {"K": ["/r/K-grp/svc"]}


def test_a_directory_with_no_schema_snapshot_and_no_start_is_not_listed(fresh_orchestrator_dir,
                                                                        capsys):
    paths.artifact_ticket_dir("K").mkdir(parents=True)
    paths.artifact_events_path("K").write_text(
        json.dumps({"type": "artifact_put", "phase": "plan", "cwd": "/r/K-grp/svc"}) + "\n")
    assert _works(capsys) == {}


def test_an_unreadable_event_log_with_no_schema_snapshot_refuses(fresh_orchestrator_dir, capsys):
    paths.artifact_ticket_dir("K").mkdir(parents=True)
    paths.artifact_events_path("K").write_bytes(b'{"type": "pipeline_started"}\n\xff\xfe\n')
    _refused(capsys)


def test_an_open_lane_card_claims_its_worktree_and_a_closed_one_does_not(fresh_orchestrator_dir,
                                                                         capsys):
    paths.artifact_ticket_dir("lane-x").mkdir(parents=True)
    state.write_json_atomic(paths.topic_path("lane-x"),
                            topic.new_card("lane", "fleet", "t", worktree="/r/lane-grp/repo"))
    assert _works(capsys) == {"lane-x": ["/r/lane-grp/repo"]}
    state.write_json_atomic(paths.topic_path("lane-x"),
                            topic.new_card("lane", "fleet", "t", worktree="/r/lane-grp/repo",
                                           status="closed"))
    assert _works(capsys) == {}


def test_the_text_form_names_each_work_and_its_paths(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                     capsys):
    _started_at_design(monkeypatch, tmp_path, cwd="/r/K-grp/svc")
    code, out, _err = _cli(capsys)
    assert code == 0
    assert out.splitlines() == ["K  /r/K-grp/svc"]


def test_a_missing_artifacts_directory_refuses(fresh_orchestrator_dir, capsys):
    shutil.rmtree(paths.ARTIFACTS)
    _refused(capsys)


def test_a_missing_assignments_directory_refuses(fresh_orchestrator_dir, capsys):
    shutil.rmtree(paths.ASSIGNMENTS)
    _refused(capsys)


def test_a_broken_assignment_refuses(fresh_orchestrator_dir, capsys):
    (paths.ASSIGNMENTS / "x.json").write_text("{")
    _refused(capsys)


def test_a_broken_pending_assignment_refuses(fresh_orchestrator_dir, capsys):
    (paths.ASSIGNMENTS_PENDING / "x.json").write_text("{")
    _refused(capsys)


def test_an_assignment_that_is_not_an_object_refuses(fresh_orchestrator_dir, capsys):
    (paths.ASSIGNMENTS / "x.json").write_text("[1]")
    _refused(capsys)


def test_a_broken_plan_of_an_unfinished_work_refuses(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                     capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path, cwd="/r/K-grp/svc")
    _break_plan(sid)
    _refused(capsys)


def test_a_broken_plan_of_an_archived_round_refuses(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                    capsys):
    sid, _ = _started_at_design(monkeypatch, tmp_path, cwd="/r/K-grp/svc")
    _break_plan(sid)
    mcp_server._archive_round("K", 1, set())
    _refused(capsys)


def test_a_broken_topic_card_refuses(fresh_orchestrator_dir, capsys):
    paths.artifact_ticket_dir("note-x").mkdir(parents=True)
    paths.topic_path("note-x").write_text("{")
    _refused(capsys)


def test_the_cli_reads_the_configured_state_root(tmp_path):
    root = tmp_path / "state"
    work = root / "artifacts" / "W"
    work.mkdir(parents=True)
    (root / "assignments").mkdir()
    (work / "schema.toml").write_text("not = [valid")
    (work / "events.jsonl").write_text(json.dumps({"type": "pipeline_started", "cwd": "/r/W/svc"}) + "\n")
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(f'[paths]\nstate_root = "{root}"\n')
    env = dict(os.environ, DOCKWRIGHT_CONFIG=str(cfg), HOME=str(tmp_path / "home"))
    proc = subprocess.run([sys.executable, "-m", "dockwright", "pipeline", "active-paths", "--json"],
                          capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"version": 1,
                                       "works": [{"task_key": "W", "paths": ["/r/W/svc"]}]}
