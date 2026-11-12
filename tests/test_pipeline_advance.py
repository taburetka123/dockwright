import asyncio
import json
import multiprocessing
import os
import re
import threading
import time
from pathlib import Path

import pytest

from dockwright import agent_guard, artifact_export, config, mcp_server, models, paths, pipeline, spawner, state
from dockwright.mcp_server import (_claude_worker_settings_args, _commit_or_pend, _node_settings_args, _read_events,
                                   _request_advance_locked, _resume_settings_args, _spawn_node, _token_lock,
                                   artifact_put_impl, pipeline_advance_impl, pipeline_start_impl, pipeline_status_impl,
                                   register_self_impl, spawn_worker_impl)
from tests.test_pipeline_core import SCHEMA


@pytest.fixture
def fresh_orchestrator_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "ACTIVE", tmp_path / "active")
    monkeypatch.setattr(paths, "QUESTIONS", tmp_path / "questions")
    monkeypatch.setattr(paths, "ANSWERS", tmp_path / "answers")
    monkeypatch.setattr(paths, "DONE", tmp_path / "done")
    monkeypatch.setattr(paths, "CLOSED", tmp_path / "closed")
    monkeypatch.setattr(paths, "HANDOFFS", tmp_path / "handoffs")
    monkeypatch.setattr(paths, "PRESETS", tmp_path / "presets")
    monkeypatch.setattr(paths, "MANAGER_TRIGGERS_LOG", tmp_path / "manager-triggers.jsonl")
    monkeypatch.setattr(paths, "MANAGER_MEMORY", tmp_path / "manager-memory")
    monkeypatch.setattr(paths, "SLOTS", tmp_path / "slots")
    monkeypatch.setattr(paths, "ARTIFACTS", tmp_path / "artifacts")
    monkeypatch.setattr(paths, "ASSIGNMENTS", tmp_path / "assignments")
    monkeypatch.setattr(paths, "ASSIGNMENTS_PENDING", tmp_path / "assignments" / ".pending")
    monkeypatch.setattr(paths, "PANE_SENDS", tmp_path / "pane-sends")
    monkeypatch.setattr(paths, "SPEND_LEDGER", tmp_path / "spend-ledger.jsonl")
    monkeypatch.setattr(paths, "TURN_ENDS", tmp_path / "turn-ends")
    monkeypatch.setattr(paths, "ARCHITECT", tmp_path / "architect")
    paths.ensure_dirs()
    yield tmp_path


_SHARED_ROOT_PATHS = {"ACTIVE": "active", "QUESTIONS": "questions", "ANSWERS": "answers", "DONE": "done",
                      "CLOSED": "closed", "HANDOFFS": "handoffs", "TURN_ENDS": "turn-ends", "PRESETS": "presets",
                      "SLOTS": "slots", "MANAGER_TRIGGERS_LOG": "manager-triggers.jsonl", "ARCHITECT": "architect",
                      "ARTIFACTS": "artifacts", "ASSIGNMENTS": "assignments",
                      "ASSIGNMENTS_PENDING": "assignments/.pending", "PANE_SENDS": "pane-sends",
                      "MANAGER_MEMORY": "manager-memory"}


@pytest.fixture
def shared_state_root(tmp_path, monkeypatch):
    root = tmp_path / "state"
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(f'[paths]\nstate_root = "{root}"\nmanager_memory = "{root / "manager-memory"}"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    monkeypatch.setattr(paths, "ROOT", root)
    for attr, rel in _SHARED_ROOT_PATHS.items():
        monkeypatch.setattr(paths, attr, root / rel)
    paths.ensure_dirs()
    return tmp_path


def _write_example_schema(tmp_path, text=SCHEMA):
    d = tmp_path / "schemas"
    (d / "briefs").mkdir(parents=True, exist_ok=True)
    (d / "code-ticket.toml").write_text(text)
    for n in ("design", "task", "pr", "post-merge"):
        (d / "briefs" / f"{n}.md").write_text(f"# {n} brief for {{task_key}} at {{cwd}} inputs {{inputs}}\n")
    return d / "code-ticket.toml"


def _manager(sid="m1", name="mgr"):
    register_self_impl(sid, "manager", name, "/tmp", "", pid=None)
    return sid


def _register_worker(name, sid, assignment_id, task_key, cwd="/w", manager_sid="m1"):
    register_self_impl(sid, "worker", name, cwd, "", pid=None, parent_manager_name="mgr", parent_sid=manager_sid)
    state.write_json_atomic(paths.assignment_path(sid), {"assignment_id": assignment_id, "claude_sid": sid,
                                                          "ticket": task_key, "name": name, "manager_sid": manager_sid,
                                                          "parent_name": "mgr", "cwd": cwd, "spawned_at": 1.0})


def _pending_record_for(name):
    for p in paths.ASSIGNMENTS_PENDING.glob("*.json"):
        record = state.read_json(p) or {}
        if record.get("name") == name:
            return p, record
    raise AssertionError(f"no pending assignment named {name}")


def _fake_registration(monkeypatch, outcomes, spawn_error=None, on_first_spawn=None):
    calls = []

    async def fake_spawn(**kw):
        calls.append(kw)
        if on_first_spawn is not None and len(calls) == 1:
            await on_first_spawn()
        await asyncio.sleep(0.05)
        if spawn_error is not None:
            raise spawn_error
        return ("999", kw["name"])

    async def fake_confirm(name, timeout, poll):
        if (outcomes.pop(name) if isinstance(outcomes, dict) else outcomes.pop(0)) != "registered":
            return None
        sid = f"sid-{name}"
        pending, record = _pending_record_for(name)
        register_self_impl(sid, "worker", name, record.get("cwd") or "/w", "", pid=None,
                           parent_manager_name=record.get("parent_name"), parent_sid=record.get("manager_sid"))
        os.replace(pending, paths.assignment_path(sid))
        state.write_json_atomic(paths.assignment_path(sid),
                                {**record, "claude_sid": sid, "name": name, "claimed_at": time.time()})
        return state.read_json(paths.ACTIVE / f"{sid}.json")

    monkeypatch.setattr("dockwright.spawner.spawn_worker_tab", fake_spawn)
    monkeypatch.setattr("dockwright.mcp_server._confirm_spawn_registration", fake_confirm)
    return calls


def _events(task_key="K"):
    return _read_events(paths.artifact_events_path(task_key))


def _types(task_key="K"):
    return [e["type"] for e in _events(task_key)]


def _fold(task_key="K"):
    return pipeline.fold(_events(task_key))


def _setup_schema(monkeypatch, tmp_path, text=SCHEMA):
    schema_path = _write_example_schema(tmp_path, text)
    monkeypatch.setattr("dockwright.config.pipeline_schemas_dir", lambda: schema_path.parent)
    return schema_path


def _started_at_design(monkeypatch, tmp_path):
    _setup_schema(monkeypatch, tmp_path)
    _manager()
    _fake_registration(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_start_impl("m1", "K", "code-ticket", "/w", "ask"))
    return r["next"]["sid"], r["next"]["assignment_id"]


_POST_MERGE = 'role = "SCOUT"\nmodel = "@sonnet"\neffort = "xhigh"\nbrief = "briefs/post-merge.md"\ninputs = ["implement.repo"]'
RULED_SCHEMA = SCHEMA.replace(
    _POST_MERGE,
    'role = "IMPL_HARD"\nbrief = "briefs/post-merge.md"\ninputs = ["implement.repo", "design.hld"]', 1) + '''
[nodes.post-merge.tier_when]
input = "design.hld"
line = "internal"
equals = "yes"
unless = "design.explainer"
tier = "SCOUT"
'''


MODEL_RULED_SCHEMA = SCHEMA.replace(
    _POST_MERGE,
    'role = "GenericWorker"\nmodel = "@opus"\neffort = "xhigh"\nbrief = "briefs/post-merge.md"\n'
    'inputs = ["implement.repo", "design.hld"]', 1) + '''
[nodes.post-merge.model_when]
input = "design.hld"
line = "internal"
equals = "yes"
unless = "design.explainer"
model = "@sonnet"
effort = "high"
'''
MODELED_DESIGN_SCHEMA = SCHEMA.replace('role = "SCOUT_DESIGN"\nmodel = "@opus"\neffort = "xhigh"',
                                       'role = "Designer"\nmodel = "@fable"\neffort = "medium"', 1)
LEGACY_ROSTER = '''
[roles.SCOUT_DESIGN]
providers = ["claude"]
[roles.SCOUT_DESIGN.claude]
model = "@sonnet"
effort = "xhigh"
[roles.IMPL_HARD]
providers = ["claude"]
[roles.IMPL_HARD.claude]
model = "@opus"
effort = "xhigh"
[roles.IMPL_ROW]
providers = ["claude"]
[roles.IMPL_ROW.claude]
model = "@opus"
effort = "xhigh"
[roles.SCOUT]
providers = ["claude"]
[roles.SCOUT.claude]
model = "@sonnet"
effort = "xhigh"
'''


def _legacy_file(monkeypatch, tmp_path, text=LEGACY_ROSTER):
    home = tmp_path / "claude-home"
    home.mkdir(exist_ok=True)
    (home / "dockwright-roster.toml").write_text(text)
    monkeypatch.setattr(config, "claude_config_home", lambda: home)


def _post_merge_launch(monkeypatch, tmp_path, schema_text, hld=None, explainer=False, suffix=""):
    _setup_schema(monkeypatch, tmp_path)
    _manager()
    _fake_registration(monkeypatch, ["registered"])
    sid = asyncio.run(pipeline_start_impl("m1", "K", "code-ticket", "/w", "ask"))["next"]["sid"]
    (paths.artifact_ticket_dir("K") / "schema.toml").write_text(schema_text)
    if hld is not None:
        artifact_put_impl("K", "design", "hld", hld, "complete", sid, scope="output")
    if explainer:
        artifact_put_impl("K", "design", "explainer", "x", "complete", sid, scope="checkpoint")
    calls = _fake_registration(monkeypatch, ["registered"])
    snap = pipeline.load_snapshot((paths.artifact_ticket_dir("K") / "schema.toml").read_text())
    kind = "restart" if suffix else "advance"
    asyncio.run(_spawn_node("K", snap, _fold().tokens["t1"], "post-merge", 1, "m1", "rq", kind, [],
                            name_suffix=suffix))
    return calls[0]["extra_args"], state.read_json(paths.assignment_path(f"sid-K-post-merge{suffix}"))


def _ruled_post_merge_launch(monkeypatch, tmp_path, hld=None, explainer=False, suffix=""):
    assert 'role = "IMPL_HARD"\nbrief = "briefs/post-merge.md"' in RULED_SCHEMA
    _legacy_file(monkeypatch, tmp_path)
    args, record = _post_merge_launch(monkeypatch, tmp_path, RULED_SCHEMA, hld, explainer, suffix)
    return args[args.index("--model") + 1], args[args.index("--effort") + 1]


def _complete_design_outputs(sid, plan='{"tasks": [{"id": 1}, {"id": 2}]}'):
    for name in ("context.ask", "design.hld", "plan.tasks"):
        phase, n = name.split(".", 1)
        body = plan if name == "plan.tasks" else "x"
        try:
            artifact_put_impl("K", phase, n, body, "complete", sid, scope="output")
        except ValueError:
            artifact_put_impl("K", phase, n, body, "complete", sid, scope="output", overwrite=True)


def _put_output(sid, phase, name):
    artifact_put_impl("K", phase, name, "done", "complete", sid, scope="output")


@pytest.mark.parametrize("body,expected", [
    ('{"tasks": [{"id": 1}, {"id": 2, "cwd": "/r"}]}', [{"id": 1}, {"id": 2, "cwd": "/r"}]),
    ('  {"tasks": [{"id": 1}]}\n', [{"id": 1}]),
    ('```json\n{"tasks": [{"id": 1}]}\n```', []),
    ('{"tasks": []}', []),
    ('{"tasks": [1, 2]}', []),
    ('{"tasks": [{"id": 1}, "two"]}', []),
    ('{"tasks": {"id": 1}}', []),
    ('[{"id": 1}]', []),
    ("not json", []),
    ("", []),
    ('{"tasks": [{"id": 1}]} trailing', []),
    ("[" * 100000, []),
])
def test_read_task_list_accepts_only_a_bare_object_with_a_non_empty_list_of_objects(body, expected):
    assert pipeline.read_task_list(body) == expected


def test_start_snapshots_and_spawns_the_start_node(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _setup_schema(monkeypatch, tmp_path)
    _manager()
    calls = _fake_registration(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_start_impl("m1", "K", "code-ticket", "/w", "make it blue"))
    assert r["state"] == "committed" and r["next"]["name"] == "K-design"
    assert (paths.artifact_ticket_dir("K") / "schema.toml").is_file()
    types = _types()
    assert types.index("pipeline_started") < types.index("advance_requested") < types.index("advance_committed")
    assert "artifact_put" in types
    assert calls[0]["initial_prompt"].startswith("# design brief") and "--model" in calls[0]["extra_args"]
    f = _fold()
    assert f.tokens["t1"].node == "design" and f.tokens["t1"].holder == r["next"]["assignment_id"]
    assert state.read_json(paths.assignment_path(r["next"]["sid"]))["assignment_id"] == r["next"]["assignment_id"]
    started = [e for e in _events() if e["type"] == "pipeline_started"][0]
    assert started["schema"] == "code-ticket" and started["manager"] == "mgr" and started["cwd"] == "/w"
    assert started["schema_sha256"] == pipeline.schema_sha256((paths.artifact_ticket_dir("K") / "schema.toml").read_text())
    ask = [e for e in _events() if e["type"] == "artifact_put"][0]
    assert (ask["phase"], ask["name"], ask["actor_sid"]) == ("context", "ask", "m1")


def test_start_spawns_through_the_worker_path_with_the_nodes_model_token(fresh_orchestrator_dir, tmp_path,
                                                                          monkeypatch):
    _setup_schema(monkeypatch, tmp_path)
    _manager()
    calls = _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_start_impl("m1", "K", "code-ticket", "/w", "ask"))
    args = calls[0]["extra_args"]
    i = args.index("--model")
    assert args[i:i + 4] == ["--model", "@opus", "--effort", "xhigh"]
    assert not any("verifier-settings" in a for a in args)
    assert "[orchestrator] Artifact discipline" in calls[0]["initial_prompt"]
    assert "[pipeline] task_key=K node=design index=1 cwd=/w" in calls[0]["initial_prompt"]
    command = spawner._runtime_command("claude", "brief", args)
    assert "@opus" not in command and models.resolve_model("@opus") in command


def test_start_refuses_bad_callers_and_names(fresh_orchestrator_dir, tmp_path, monkeypatch):
    schema_path = _setup_schema(monkeypatch, tmp_path)
    _manager()
    _register_worker("w", "sid-w", "a-w", "K")
    calls = _fake_registration(monkeypatch, [])
    with pytest.raises(ValueError, match="manager_sid"):
        asyncio.run(pipeline_start_impl("", "K", "code-ticket", "/w", "ask"))
    with pytest.raises(ValueError, match="manager"):
        asyncio.run(pipeline_start_impl("sid-w", "K", "code-ticket", "/w", "ask"))
    with pytest.raises(ValueError, match="schema"):
        asyncio.run(pipeline_start_impl("m1", "K", "../code-ticket", "/w", "ask"))
    with pytest.raises(ValueError, match=re.escape(str(schema_path.parent / "nope.toml"))):
        asyncio.run(pipeline_start_impl("m1", "K", "nope", "/w", "ask"))
    with pytest.raises(ValueError, match="task_key"):
        asyncio.run(pipeline_start_impl("m1", "K/1", "code-ticket", "/w", "ask"))
    assert calls == [] and not paths.artifact_events_path("K").exists()


def test_start_refuses_an_invalid_schema_before_writing(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _install_roles(monkeypatch, tmp_path)
    _setup_schema(monkeypatch, tmp_path, SCHEMA.replace('role = "SCOUT"', 'role = "NOPE"'))
    _manager()
    calls = _fake_registration(monkeypatch, [])
    with pytest.raises(ValueError, match="NOPE"):
        asyncio.run(pipeline_start_impl("m1", "K", "code-ticket", "/w", "ask"))
    assert calls == [] and not paths.artifact_ticket_dir("K").joinpath("schema.toml").exists()
    assert _types() == []


def test_start_refuses_a_second_start_and_a_refused_ask_put_writes_no_token(fresh_orchestrator_dir, tmp_path,
                                                                            monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    before = _types()
    with pytest.raises(ValueError, match="already has a token"):
        asyncio.run(pipeline_start_impl("m1", "K", "code-ticket", "/w", "ask"))
    assert _types() == before
    artifact_put_impl("K2", "context", "ask", "someone else's", "complete", "other-sid", scope="output")
    with pytest.raises(ValueError, match="artifact_put refused"):
        asyncio.run(pipeline_start_impl("m1", "K2", "code-ticket", "/w", "ask"))
    assert "pipeline_started" not in _types("K2") and "advance_requested" not in _types("K2")
    assert not (paths.artifact_ticket_dir("K2") / "schema.toml").exists()


def test_advance_commits_hands_off_and_refuses_repeat(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    assert r["state"] == "committed" and r["next"]["name"] == "K-task-1"
    assert state.read_json(paths.assignment_path(sid))["handed_off_at"] > 0
    handed = [e for e in _events() if e["type"] == "handed_off"]
    assert [(e["assignment_id"], e["sid"], e["token_id"]) for e in handed] == [(aid, sid, "t1")]
    with pytest.raises(ValueError, match="handed off"):
        artifact_put_impl("K", "design", "extra", "x", "complete", sid, scope="output")
    assert [e for e in _events() if e["type"] == "artifact_put_refused"][-1]["reason"] == "handed_off"
    assert not paths.artifact_path("K", "design", "extra").exists()
    with pytest.raises(ValueError, match="handed off"):
        asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    f = _fold()
    assert (f.tokens["t1"].node, f.tokens["t1"].index, f.tokens["t1"].holder) == ("task", 1, r["next"]["assignment_id"])


def test_a_handed_off_event_refuses_the_session_without_its_stamp(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    record = state.read_json(paths.assignment_path(sid))
    record.pop("handed_off_at")
    state.write_json_atomic(paths.assignment_path(sid), record)
    with pytest.raises(ValueError, match="node handed off"):
        artifact_put_impl("K", "design", "extra", "x", "complete", sid, scope="output")
    with pytest.raises(ValueError, match="handed off"):
        asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    assert artifact_put_impl("J", "design", "extra", "x", "complete", sid, scope="output")["ok"]


def test_handed_off_refusal_reads_the_writers_own_assignment(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _register_worker("K-design-child", "sid-child", "a-child", "K")
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    assert artifact_put_impl("K", "design", "child-notes", "x", "complete", "sid-child")["ok"]


def test_child_of_the_node_cannot_advance(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _register_worker("K-design-child", "sid-child", "a-child", "K")
    with pytest.raises(ValueError, match="holder"):
        asyncio.run(pipeline_advance_impl("sid-child", "K", "task"))
    assert [e for e in _events() if e["type"] == "advance_refused"]


def test_a_caller_keyed_to_another_work_is_refused(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _register_worker("other", "sid-other", aid, "OTHER")
    with pytest.raises(ValueError, match="OTHER"):
        asyncio.run(pipeline_advance_impl("sid-other", "K", "task"))
    refused = [e for e in _events() if e["type"] == "advance_refused"]
    assert refused and refused[0]["actor_sid"] == "sid-other" and refused[0]["to"] == "task"


def test_outputs_and_task_list_are_checked_before_the_request(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    calls = _fake_registration(monkeypatch, [])
    with pytest.raises(ValueError, match="design.hld is not complete"):
        asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    _complete_design_outputs(sid, plan='{"tasks": [1, 2]}')
    with pytest.raises(ValueError, match="every entry a JSON object"):
        asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    assert calls == [] and _types().count("advance_requested") == 1
    assert len([e for e in _events() if e["type"] == "advance_refused"]) == 2


def test_spawn_failure_leaves_the_request_open_and_repeat_is_refused(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["no_register"])
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    assert r["state"] == "pending"
    f = _fold()
    assert f.tokens["t1"].node == "design" and len(f.open_requests) == 1
    assert f.open_requests[0]["pending"]["reason"] == "no_register" and f.open_requests[0]["pending"]["assignment_id"]
    assert paths.pending_assignment_path(f.open_requests[0]["pending"]["assignment_id"]).exists()
    assert "handed_off_at" not in state.read_json(paths.assignment_path(sid))
    with pytest.raises(ValueError, match="in flight"):
        asyncio.run(pipeline_advance_impl(sid, "K", "task"))


def test_a_spawn_that_raises_pends_the_request_as_spawn_error(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, [], spawn_error=OSError("no tmux"))
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    assert r["state"] == "pending" and r["next"] is None and "Could not spawn a tab" in r["reason"]
    pending = _fold().open_requests[0]["pending"]
    assert pending["reason"] == "spawn-error" and pending["assignment_id"] is None and pending["error"]


def test_no_live_manager_pends_without_spawning(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    calls = _fake_registration(monkeypatch, [])
    (paths.ACTIVE / "m1.json").unlink()
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    assert (r["state"], r["next"], r["reason"]) == ("pending", None, "no-manager") and calls == []
    pending = _fold().open_requests[0]["pending"]
    assert (pending["reason"], pending["assignment_id"]) == ("no-manager", None)


def test_a_limited_manager_pends_without_spawning(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    calls = _fake_registration(monkeypatch, [])
    (paths.ROOT / ".manager-limited-mgr").touch()
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    assert (r["state"], r["reason"]) == ("pending", "manager-limited") and calls == []
    assert _fold().open_requests[0]["pending"]["reason"] == "manager-limited"


def test_a_dead_manager_sid_falls_back_to_the_live_parent_by_name(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    (paths.ACTIVE / "m1.json").unlink()
    _manager("m2", "mgr")
    calls = _fake_registration(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    assert r["state"] == "committed"
    assert state.read_json(paths.assignment_path(r["next"]["sid"]))["manager_sid"] == "m2"
    assert calls[0]["env"]["CLAUDE_PARENT_SID"] == "m2"


def test_override_is_manager_only_and_moves_anywhere(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="manager"):
        asyncio.run(pipeline_advance_impl(sid, "K", "pr", override=True, reason="skip"))
    _fake_registration(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason="skip ahead"))
    assert r["state"] == "committed"
    events = _events()
    override = [e for e in events if e["type"] == "token_override"][0]
    assert (override["reason"], override["from"], override["to"], override["index"], override["actor_sid"],
            override["manager"]) == ("skip ahead", "design", "pr", 1, "m1", "mgr")
    assert [e["type"] for e in events[events.index(override):]][:3] == ["token_override", "advance_requested",
                                                                        "handed_off"]
    assert state.read_json(paths.assignment_path(sid))["handed_off_at"] > 0
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "design", override=True, reason="back"))
    assert _fold().tokens["t1"].node == "design"


def test_override_refuses_an_unknown_node_a_bad_index_and_a_missing_reason(fresh_orchestrator_dir, tmp_path,
                                                                          monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    calls = _fake_registration(monkeypatch, [])
    with pytest.raises(ValueError, match="nowhere"):
        asyncio.run(pipeline_advance_impl("m1", "K", "nowhere", override=True, reason="r"))
    with pytest.raises(ValueError, match="index"):
        asyncio.run(pipeline_advance_impl("m1", "K", "task", override=True, reason="r", index=0))
    with pytest.raises(ValueError, match="reason"):
        asyncio.run(pipeline_advance_impl("m1", "K", "task", override=True))
    with pytest.raises(ValueError, match="override"):
        asyncio.run(pipeline_advance_impl("m1", "K", "task", index=2))
    assert calls == [] and "token_override" not in _types()
    assert len([e for e in _events() if e["type"] == "advance_refused"]) == 4


def test_override_refusal_names_the_snapshot_nodes(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    _fake_registration(monkeypatch, [])
    with pytest.raises(ValueError) as err:
        asyncio.run(pipeline_advance_impl("m1", "K", "nowhere", override=True, reason="r"))
    nodes = list(pipeline.load_snapshot((paths.artifact_ticket_dir("K") / "schema.toml").read_text()).nodes)
    assert f"its nodes are {', '.join(nodes)}" in str(err.value)


def test_override_with_resnapshot_validates_first_then_replaces_the_snapshot(fresh_orchestrator_dir, tmp_path,
                                                                            monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    snap_path = paths.artifact_ticket_dir("K") / "schema.toml"
    before = snap_path.read_text()
    schema_path = tmp_path / "schemas" / "code-ticket.toml"
    schema_path.write_text(SCHEMA.replace('model = "@sonnet"', 'model = "NOPE"'))
    calls = _fake_registration(monkeypatch, [])
    with pytest.raises(ValueError, match="NOPE"):
        asyncio.run(pipeline_advance_impl("m1", "K", "task", override=True, reason="r", resnapshot=True))
    assert snap_path.read_text() == before and "token_override" not in _types() and calls == []
    (tmp_path / "schemas" / "briefs" / "task.md").write_text("# task brief v2 for {task_key}\n")
    schema_path.write_text(SCHEMA)
    calls = _fake_registration(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_advance_impl("m1", "K", "task", override=True, reason="new brief", index=2,
                                          resnapshot=True))
    assert r["state"] == "committed" and r["next"]["name"] == "K-task-2"
    text = snap_path.read_text()
    assert "task brief v2" in text
    snapshot_event = [e for e in _events() if e["type"] == "schema_snapshot"][0]
    assert snapshot_event["schema_sha256"] == pipeline.schema_sha256(text)
    assert calls[0]["initial_prompt"].startswith("# task brief v2 for K")
    assert (_fold().tokens["t1"].node, _fold().tokens["t1"].index) == ("task", 2)


def test_question_mark_is_always_refused_with_an_event(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="ask_manager"):
        asyncio.run(pipeline_advance_impl(sid, "K", "?", reason="which next?"))
    assert [e for e in _events() if e["type"] == "advance_refused"][0]["reason"]
    with pytest.raises(ValueError):
        asyncio.run(pipeline_advance_impl("m1", "K", "?", override=True, reason="which"))


def test_trigger_edge_is_manager_only_without_override(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason="jump"))
    pr_sid = "sid-K-pr"
    with pytest.raises(ValueError, match="trigger"):
        asyncio.run(pipeline_advance_impl(pr_sid, "K", "post-merge"))
    _fake_registration(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_advance_impl("m1", "K", "post-merge", reason="merged 2026-09-23"))
    assert r["state"] == "committed"
    fired = [e for e in _events() if e["type"] == "trigger_fired"]
    assert len(fired) == 1 and fired[0]["evidence"] == "merged 2026-09-23" and fired[0]["edge"]["to"] == "post-merge"
    assert state.read_json(paths.assignment_path(pr_sid))["handed_off_at"] > 0


def test_one_work_walks_design_tasks_pr_and_post_merge(fresh_orchestrator_dir, tmp_path, monkeypatch):
    design_sid, design_aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(design_sid)
    _fake_registration(monkeypatch, ["registered"])
    task1 = asyncio.run(pipeline_advance_impl(design_sid, "K", "task"))["next"]
    _put_output(task1["sid"], "review", "task-1")
    _fake_registration(monkeypatch, ["registered"])
    task2 = asyncio.run(pipeline_advance_impl(task1["sid"], "K", "task"))["next"]
    with pytest.raises(ValueError, match="review.task-2 is not complete"):
        asyncio.run(pipeline_advance_impl(task2["sid"], "K", "pr"))
    _put_output(task2["sid"], "review", "task-2")
    with pytest.raises(ValueError, match="last of 2"):
        asyncio.run(pipeline_advance_impl(task2["sid"], "K", "task"))
    _fake_registration(monkeypatch, ["registered"])
    pr = asyncio.run(pipeline_advance_impl(task2["sid"], "K", "pr"))["next"]
    _put_output(pr["sid"], "implement", "repo")
    _fake_registration(monkeypatch, ["registered"])
    post = asyncio.run(pipeline_advance_impl("m1", "K", "post-merge", reason="merged"))["next"]
    assert [task1["name"], task2["name"], pr["name"], post["name"]] == ["K-task-1", "K-task-2", "K-pr", "K-post-merge"]
    token = _fold().tokens["t1"]
    assert (token.node, token.index, token.holder) == ("post-merge", 1, post["assignment_id"])
    assert _fold().open_requests == []
    engine = [e["type"] for e in _events() if e["type"] in pipeline.RESERVED_EVENT_TYPES
              and e["type"] not in ("advance_refused", "artifact_read", "inputs_handed")]
    assert engine == (["pipeline_started", "advance_requested", "advance_committed"]
                      + ["advance_requested", "advance_committed", "handed_off"] * 3
                      + ["trigger_fired", "advance_requested", "advance_committed", "handed_off"])
    committed = [(e["node"], e["index"]) for e in _events() if e["type"] == "advance_committed"]
    assert committed == [("design", 1), ("task", 1), ("task", 2), ("pr", 1), ("post-merge", 1)]
    handed = [e["assignment_id"] for e in _events() if e["type"] == "handed_off"]
    assert handed == [design_aid, task1["assignment_id"], task2["assignment_id"], pr["assignment_id"]]


def test_a_spawned_node_reads_its_written_inputs_from_copies(fresh_orchestrator_dir, tmp_path, monkeypatch):
    design_sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(design_sid)
    calls = _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl(design_sid, "K", "task"))
    prompt = calls[-1]["initial_prompt"]
    match = re.search(r"design\.hld = (\S+) written_at=", prompt)
    assert match and any(line.startswith("Tools: in your first message") for line in prompt.splitlines())
    copy = Path(match.group(1))
    assert copy.read_text() == mcp_server.artifact_get_impl("K", "design", "hld")["content"]
    assert not copy.resolve().is_relative_to(paths.ARTIFACTS.resolve())
    events = _events()
    handed = [i for i, e in enumerate(events) if e["type"] == "inputs_handed"]
    assert len(handed) == 1 and events[handed[0]]["names"] == ["plan.tasks", "design.hld"]
    committed = next(i for i, e in enumerate(events) if e["type"] == "advance_committed" and e["node"] == "task")
    assert events[handed[0]]["assignment_id"] == events[committed]["holder"]
    assert handed[0] < committed


def test_an_input_whose_copy_fails_shows_no_copy_and_is_not_handed(fresh_orchestrator_dir, tmp_path, monkeypatch):
    design_sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(design_sid)
    calls = _fake_registration(monkeypatch, ["registered"])

    def refuse(*args, **kwargs):
        raise ValueError("exports folder refused")

    monkeypatch.setattr("dockwright.artifact_export.export_body", refuse)
    r = asyncio.run(pipeline_advance_impl(design_sid, "K", "task"))
    assert "design.hld (no copy)" in calls[-1]["initial_prompt"]
    assert r["state"] == "committed"
    assert "inputs_handed" not in _types()


def test_a_spawn_prunes_the_exports_once_however_many_inputs_it_copies(fresh_orchestrator_dir, tmp_path, monkeypatch):
    design_sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(design_sid)
    _fake_registration(monkeypatch, ["registered"])
    walks = []
    prune = artifact_export.prune
    monkeypatch.setattr(artifact_export, "prune", lambda *a, **k: walks.append(1) or prune(*a, **k))
    asyncio.run(pipeline_advance_impl(design_sid, "K", "task"))
    assert len(walks) == 1


def test_a_usage_paused_spawn_writes_no_handed_event(fresh_orchestrator_dir, tmp_path, monkeypatch):
    design_sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(design_sid)
    _fake_registration(monkeypatch, ["registered"])
    monkeypatch.setattr("dockwright.spawner.usage_spawn_gate", lambda force=False: {"status": "paused", "reason": "x"})
    r = asyncio.run(pipeline_advance_impl(design_sid, "K", "task"))
    assert r["state"] == "pending"
    assert "inputs_handed" not in _types()


def test_a_handed_event_that_cannot_be_written_never_refuses_the_spawn(fresh_orchestrator_dir, tmp_path, monkeypatch):
    design_sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(design_sid)
    _fake_registration(monkeypatch, ["registered"])
    append = mcp_server._append_task_event

    def refuse_handed(task_key, event):
        if event.get("type") == "inputs_handed":
            raise ValueError("journal line too long")
        return append(task_key, event)

    monkeypatch.setattr(mcp_server, "_append_task_event", refuse_handed)
    r = asyncio.run(pipeline_advance_impl(design_sid, "K", "task"))
    assert r["state"] == "committed"


def test_two_concurrent_advances_commit_exactly_once(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["registered", "registered"])

    async def both():
        return await asyncio.gather(pipeline_advance_impl(sid, "K", "task"), pipeline_advance_impl(sid, "K", "task"),
                                    return_exceptions=True)

    results = asyncio.run(both())
    oks = [r for r in results if isinstance(r, dict)]
    errs = [r for r in results if isinstance(r, Exception)]
    assert len(oks) == 1 and len(errs) == 1 and "in flight" in str(errs[0])
    assert len([e for e in _events() if e["type"] == "advance_committed"]) == 2
    assert len([e for e in _events() if e["type"] == "advance_requested"]) == 2


def _advance_superseded_mid_spawn(monkeypatch, sid, advance_outcome):
    overridden = {}

    async def override():
        overridden["result"] = await pipeline_advance_impl("m1", "K", "pr", override=True, reason="jump")

    _fake_registration(monkeypatch, {"K-task-1": advance_outcome, "K-pr": "registered"}, on_first_spawn=override)
    superseded = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    return superseded, overridden["result"]


def test_an_override_during_a_spawn_supersedes_it_and_kills_the_fresh_session(fresh_orchestrator_dir, tmp_path,
                                                                             monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    killed = []
    monkeypatch.setattr("dockwright.mcp_server.kill_worker_impl", lambda worker, dry_run=False: killed.append(worker))
    superseded, committed = _advance_superseded_mid_spawn(monkeypatch, sid, "registered")
    assert superseded == {"ok": False, "state": "superseded", "request_id": superseded["request_id"]}
    assert killed == ["sid-K-task-1"]
    assert committed["state"] == "committed" and committed["next"]["name"] == "K-pr"
    assert [e for e in _events() if e["type"] == "advance_committed" and e["request_id"] == superseded["request_id"]] == []
    assert [e["assignment_id"] for e in _events() if e["type"] == "handed_off"] == [aid]
    assert (_fold().tokens["t1"].node, _fold().tokens["t1"].holder) == ("pr", committed["next"]["assignment_id"])


def test_a_superseded_unregistered_spawn_closes_its_window_and_pending_record(fresh_orchestrator_dir, tmp_path,
                                                                             monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    closed = []
    monkeypatch.setattr("dockwright.mcp_server._close_window", closed.append)
    superseded, committed = _advance_superseded_mid_spawn(monkeypatch, sid, "no_register")
    assert superseded["state"] == "superseded" and closed == ["999"]
    assert [p.name for p in paths.ASSIGNMENTS_PENDING.glob("*.json")] == []
    assert [e for e in _events() if e["type"] == "advance_pending"] == []
    assert committed["state"] == "committed"


def test_a_failing_close_of_a_superseded_session_is_reported_not_raised(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)

    def boom(worker, dry_run=False):
        raise RuntimeError("tmux gone")

    monkeypatch.setattr("dockwright.mcp_server.kill_worker_impl", boom)
    superseded, _ = _advance_superseded_mid_spawn(monkeypatch, sid, "registered")
    assert superseded["state"] == "superseded" and "tmux gone" in superseded["close_error"]


def test_restart_kind_adds_the_resume_line_and_a_name_suffix(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    calls = _fake_registration(monkeypatch, ["no_register", "no_register"])
    snap = pipeline.load_snapshot((paths.artifact_ticket_dir("K") / "schema.toml").read_text())
    token = _fold().tokens["t1"]
    asyncio.run(_spawn_node("K", snap, token, "design", 1, "m1", "rq", "advance", []))
    r = asyncio.run(_spawn_node("K", snap, token, "design", 1, "m1", "rq", "restart", ["design.explainer"],
                                name_suffix="-r1"))
    assert r["name"] == "K-design-r1" and r["assignment_id"]
    assert "previous session died" not in calls[0]["initial_prompt"]
    assert "previous session died" in calls[1]["initial_prompt"] and "overwrite=True" in calls[1]["initial_prompt"]
    assert "for K at /w inputs (none)" in calls[1]["initial_prompt"]


def test_a_repeat_node_spawns_in_its_plan_task_cwd(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid, plan='{"tasks": [{"id": 1, "cwd": "/repo-a"}, {"id": 2}]}')
    calls = _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    assert calls[0]["cwd"] == "/repo-a" and "cwd=/repo-a" in calls[0]["initial_prompt"]


def _request_in_subprocess(task_key, sid, hold_sec, out):
    from dockwright import mcp_server
    mcp_server._after_fold_hook = (lambda: time.sleep(hold_sec)) if hold_sec else None
    try:
        out.put(("ok", mcp_server._request_advance_locked(sid, task_key, "task")["request_id"]))
    except ValueError as err:
        out.put(("err", str(err)))


def test_two_processes_request_the_same_edge_once(shared_state_root, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, shared_state_root)
    _complete_design_outputs(sid)
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    slow = ctx.Process(target=_request_in_subprocess, args=("K", sid, 1.0, q))
    fast = ctx.Process(target=_request_in_subprocess, args=("K", sid, 0.0, q))
    slow.start()
    time.sleep(0.4)
    fast.start()
    slow.join(30)
    fast.join(30)
    outcomes = sorted(q.get(timeout=5) for _ in (slow, fast))
    assert [o[0] for o in outcomes] == ["err", "ok"] and "in flight" in outcomes[0][1]
    requested = [e for e in _events() if e["type"] == "advance_requested"]
    assert len(requested) == 2


def test_the_locked_request_calls_the_after_fold_hook(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    seen = []
    monkeypatch.setattr("dockwright.mcp_server._after_fold_hook", lambda: seen.append(True))
    request = _request_advance_locked(sid, "K", "task")
    assert seen == [True] and (request["to"], request["index_to"]) == ("task", 1)


def test_the_event_loop_keeps_running_while_another_holder_has_the_work_lock(fresh_orchestrator_dir, tmp_path,
                                                                             monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["registered"])
    held, released = threading.Event(), []

    def hold():
        with state.file_lock(paths.artifact_ticket_dir("K") / ".token.lock"):
            held.set()
            time.sleep(1.0)
            released.append(time.monotonic())

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(5)

    async def advance_beside_a_ticker():
        ticks = []

        async def ticker():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.05)

        ticking = asyncio.create_task(ticker())
        result = await pipeline_advance_impl(sid, "K", "task")
        ticking.cancel()
        return result, ticks

    result, ticks = asyncio.run(advance_beside_a_ticker())
    holder.join()
    assert result["state"] == "committed"
    assert len([t for t in ticks if t < released[0]]) >= 5


def test_a_limited_flag_does_not_pend_the_managers_own_override(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    (paths.ROOT / ".manager-limited-mgr").touch()
    calls = _fake_registration(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason="jump"))
    assert r["state"] == "committed" and r["next"]["name"] == "K-pr" and len(calls) == 1


_LONG_NON_ASCII = ("Ο μηχανικός ζητά να παραλειφθεί ο σχεδιασμός και να ανοίξει αμέσως το αίτημα συγχώνευσης. " * 12)[:720]


def test_a_long_non_ascii_override_reason_keeps_the_journal_readable(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    _fake_registration(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason=_LONG_NON_ASCII))
    assert r["state"] == "committed"
    token = _fold().tokens["t1"]
    assert (token.node, token.holder) == ("pr", r["next"]["assignment_id"])
    override = [e for e in _events() if e["type"] == "token_override"][0]
    assert (override["to"], override["index"], override["token_id"]) == ("pr", 1, "t1")
    assert _LONG_NON_ASCII.startswith(override["reason"].removesuffix("…[truncated]"))
    assert override["reason"].endswith("[truncated]")


def test_a_long_non_ascii_trigger_evidence_keeps_the_edge(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason="jump"))
    _fake_registration(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_advance_impl("m1", "K", "post-merge", reason=_LONG_NON_ASCII))
    assert r["state"] == "committed" and _fold().tokens["t1"].node == "post-merge"
    fired = [e for e in _events() if e["type"] == "trigger_fired"][0]
    assert fired["edge"] == {"from": "pr", "to": "post-merge", "trigger": "merge"}
    assert fired["evidence"].endswith("[truncated]")


def test_a_long_non_ascii_spawn_error_keeps_the_request(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, [], spawn_error=RuntimeError(_LONG_NON_ASCII))
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    assert r["state"] == "pending"
    request = _fold().open_requests[0]
    assert request["request_id"] == r["request_id"] and request["edge"] == {"from": "design", "to": "task"}
    assert request["pending"]["reason"] == "spawn-error" and request["pending"]["error"].endswith("[truncated]")


def test_a_start_whose_event_cannot_fit_raises_before_writing(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _setup_schema(monkeypatch, tmp_path)
    _manager()
    calls = _fake_registration(monkeypatch, ["registered"])
    with pytest.raises(ValueError, match="pipeline_started"):
        asyncio.run(pipeline_start_impl("m1", "K", "code-ticket", "/w/" + "d" * 3600, "ask"))
    assert "pipeline_started" not in _types() and calls == []
    assert not (paths.artifact_ticket_dir("K") / "schema.toml").exists()
    assert not paths.artifact_path("K", "context", "ask").exists()


def _append_malformed_commit(task_key):
    state.append_event(paths.artifact_events_path(task_key),
                       {"type": "advance_committed", "request_id": "r-bad", "token_id": "t1", "node": "task"})


def test_start_refuses_an_unreadable_journal(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _setup_schema(monkeypatch, tmp_path)
    _manager()
    calls = _fake_registration(monkeypatch, [])
    _append_malformed_commit("K")
    with pytest.raises(ValueError, match="events.jsonl for K is unreadable"):
        asyncio.run(pipeline_start_impl("m1", "K", "code-ticket", "/w", "ask"))
    assert calls == [] and "pipeline_started" not in _types()


def test_advance_refuses_an_unreadable_journal_with_an_event(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    calls = _fake_registration(monkeypatch, [])
    _append_malformed_commit("K")
    with pytest.raises(ValueError, match="events.jsonl for K is unreadable"):
        asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    refused = [e for e in _events() if e["type"] == "advance_refused"]
    assert calls == [] and len(refused) == 1 and "unreadable" in refused[0]["reason"]


def test_a_second_commit_of_the_same_registered_session_is_idempotent(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    killed = []
    monkeypatch.setattr("dockwright.mcp_server.kill_worker_impl", lambda worker, dry_run=False: killed.append(worker))
    same = {"status": "registered", "assignment_id": r["next"]["assignment_id"], "claude_sid": r["next"]["sid"],
            "name": r["next"]["name"]}
    before = _events()
    again = _commit_or_pend("K", r["request_id"], "t1", "task", 1, same, None, reconciled=True)
    assert again == r and _events() == before and killed == []
    other = {**same, "assignment_id": "a-other", "claude_sid": "sid-other"}
    assert _commit_or_pend("K", r["request_id"], "t1", "task", 1, other, None)["state"] == "superseded"
    assert killed == ["sid-other"] and _events() == before


def test_the_token_lock_of_one_work_does_not_block_another_work(fresh_orchestrator_dir):
    held, release, acquired = threading.Event(), threading.Event(), threading.Event()

    def hold_a():
        with _token_lock("A"):
            held.set()
            release.wait(5)

    def take_b():
        with _token_lock("B"):
            acquired.set()

    a = threading.Thread(target=hold_a)
    a.start()
    assert held.wait(5)
    b = threading.Thread(target=take_b)
    b.start()
    try:
        assert acquired.wait(2)
    finally:
        release.set()
        a.join()
        b.join()


@pytest.mark.parametrize("target", ["design", "pr"])
def test_an_override_hands_off_the_displaced_holder(fresh_orchestrator_dir, tmp_path, monkeypatch, target):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _fake_registration(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_advance_impl("m1", "K", target, override=True, reason="redo"))
    assert r["state"] == "committed" and r["next"]["assignment_id"] != aid
    assert state.read_json(paths.assignment_path(sid))["handed_off_at"] > 0
    assert [e["assignment_id"] for e in _events() if e["type"] == "handed_off"] == [aid]
    with pytest.raises(ValueError, match="node handed off"):
        artifact_put_impl("K", "plan", "tasks", '{"tasks": [{"id": 1}]}', "complete", sid, scope="output",
                          overwrite=True)


def test_an_override_refuses_an_index_past_the_task_list(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, aid = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    calls = _fake_registration(monkeypatch, [])
    with pytest.raises(ValueError, match="index 5 is past the 2 task"):
        asyncio.run(pipeline_advance_impl("m1", "K", "task", override=True, reason="x", index=5))
    assert calls == [] and "token_override" not in _types()
    assert [e for e in _events() if e["type"] == "advance_refused"][-1]["to"] == "task"


PROFILE = 'claudeMdExcludes = ["/h/.claude*/rules/a.md"]\nskillListingMaxDescChars = 160\n\n[skillOverrides]\n"x-skill" = "off"\n'
PRESET = {"enableAllProjectMcpServers": True,
          "permissions": {"defaultMode": "auto", "allow": ["mcp__dockwright__worker_done"]}}


def _profiled_setup(monkeypatch, tmp_path, extra='extra_args = []', preset=PRESET):
    text = SCHEMA.replace('extra_args = []', extra + '\nprofiles = ["profiles/base.toml"]', 1)
    schema_path = _setup_schema(monkeypatch, tmp_path, text)
    monkeypatch.setattr(config, "worker_headless_preset", lambda: True)
    (schema_path.parent / "profiles").mkdir(exist_ok=True)
    (schema_path.parent / "profiles" / "base.toml").write_text(PROFILE)
    paths.PRESETS.mkdir(parents=True, exist_ok=True)
    (paths.PRESETS / "worker-headless-settings.json").write_text(json.dumps(preset))


def _started(monkeypatch):
    _manager()
    calls = _fake_registration(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_start_impl("m1", "K", "code-ticket", "/w", "ask"))
    return r, calls[0]["extra_args"]


def _settings_file(args):
    assert args.count("--settings") == 1, args
    return Path(args[args.index("--settings") + 1])


def _install_roles(monkeypatch, tmp_path):
    roles_dir = tmp_path / "roles"
    for name in ("GenericWorker", "Designer", "SCOUT_DESIGN", "IMPL_HARD", "SCOUT"):
        (roles_dir / name).mkdir(parents=True, exist_ok=True)
        (roles_dir / name / "role.toml").write_text(f'core = "{name}"\nlaunch = ["worker"]\n')
        (roles_dir / name / "role.worker.md").write_text(f"<!-- dockwright-role: {name} launch=worker -->\n")
        (roles_dir / name / "role.settings.json").write_text(json.dumps({"skillOverrides": {name: "name-only"}}))
    monkeypatch.setattr(paths, "ROLES_DIR", roles_dir)
    return roles_dir


def test_a_role_node_spawns_with_its_render_and_its_settings_in_the_artifact_store(fresh_orchestrator_dir, tmp_path,
                                                                                  monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    roles_dir = _install_roles(monkeypatch, tmp_path)
    _setup_schema(monkeypatch, tmp_path, SCHEMA.replace('role = "SCOUT_DESIGN"', 'role = "Designer"', 1))
    r, args = _started(monkeypatch)
    assert args[:2] == ["--append-system-prompt-file", str(roles_dir / "Designer" / "role.worker.md")]
    path = _settings_file(args)
    assert path == paths.artifact_ticket_dir("K") / "settings" / f"{r['request_id']}.json"
    assert json.loads(path.read_text())["skillOverrides"] == {"Designer": "name-only"}
    assert args[args.index("--model") + 1] == "@opus"
    (pending,) = [state.read_json(p) for p in paths.ASSIGNMENTS.glob("*.json")]
    assert "role" not in pending and pending["context_role"] == "Designer"


def test_a_role_node_spawns_with_its_profiles_agents_file(fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    roles_dir = _install_roles(monkeypatch, tmp_path)
    (roles_dir / "_agents").mkdir()
    (roles_dir / "_agents" / "none.json").write_text("{}")
    monkeypatch.setattr(config, "profiles", lambda: {"sets": {"none": []}, "repos": {}, "work_roots": [], "work_default": "none"})
    _setup_schema(monkeypatch, tmp_path, SCHEMA.replace('role = "SCOUT_DESIGN"', 'role = "Designer"', 1))
    _, args = _started(monkeypatch)
    assert args[:2] == ["--append-system-prompt-file", str(roles_dir / "Designer" / "role.worker.md")]
    assert args[-2:] == ["--agents-file", str(roles_dir / "_agents" / "none.json")]


def test_a_role_node_spawns_with_the_roles_disabled_mcp_servers_over_the_preset(fresh_orchestrator_dir, tmp_path,
                                                                                monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    roles_dir = _install_roles(monkeypatch, tmp_path)
    (roles_dir / "Designer" / "role.settings.json").write_text(
        json.dumps({"disabledMcpjsonServers": ["incident-mcp"]}))
    _setup_schema(monkeypatch, tmp_path, SCHEMA.replace('role = "SCOUT_DESIGN"', 'role = "Designer"', 1))
    monkeypatch.setattr(config, "worker_headless_preset", lambda: True)
    paths.PRESETS.mkdir(parents=True, exist_ok=True)
    (paths.PRESETS / "worker-headless-settings.json").write_text(json.dumps(PRESET))
    r, args = _started(monkeypatch)
    path = _settings_file(args)
    assert path == paths.artifact_ticket_dir("K") / "settings" / f"{r['request_id']}.json"
    data = json.loads(path.read_text())
    assert data["disabledMcpjsonServers"] == ["incident-mcp"] and data["enableAllProjectMcpServers"] is True
    assert data["permissions"] == PRESET["permissions"]


def test_a_modeled_node_spawns_on_its_model_and_effort(fresh_orchestrator_dir, tmp_path, monkeypatch):
    roles_dir = _install_roles(monkeypatch, tmp_path)
    _setup_schema(monkeypatch, tmp_path, MODELED_DESIGN_SCHEMA)
    _, args = _started(monkeypatch)
    assert args[:2] == ["--append-system-prompt-file", str(roles_dir / "Designer" / "role.worker.md")]
    i = args.index("--model")
    assert args[i:i + 4] == ["--model", "@fable", "--effort", "medium"]
    assert args.count("--model") == 1 and args.count("--effort") == 1
    (pending,) = [state.read_json(p) for p in paths.ASSIGNMENTS.glob("*.json")]
    assert "role" not in pending and pending["context_role"] == "Designer"
    command = spawner._runtime_command("claude", "brief", args)
    assert "@fable" not in command and models.resolve_model("@fable") in command


def _old_snapshot_launch(monkeypatch, tmp_path, snapshot_text):
    _setup_schema(monkeypatch, tmp_path)
    _manager()
    _fake_registration(monkeypatch, ["registered"])
    sid = asyncio.run(pipeline_start_impl("m1", "K", "code-ticket", "/w", "ask"))["next"]["sid"]
    (paths.artifact_ticket_dir("K") / "schema.toml").write_text(snapshot_text)
    _complete_design_outputs(sid)
    calls = _fake_registration(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    return r, calls


def test_an_old_snapshot_node_spawns_as_generic_worker_on_its_row_from_the_legacy_file(fresh_orchestrator_dir, tmp_path,
                                                                                       monkeypatch):
    roles_dir = _install_roles(monkeypatch, tmp_path)
    _legacy_file(monkeypatch, tmp_path)
    old = SCHEMA.replace('role = "IMPL_HARD"\nmodel = "@opus"\neffort = "xhigh"', 'role = "IMPL_ROW"', 1)
    r, calls = _old_snapshot_launch(monkeypatch, tmp_path, old)
    assert r["state"] == "committed"
    args = calls[0]["extra_args"]
    assert args[:2] == ["--append-system-prompt-file", str(roles_dir / "GenericWorker" / "role.worker.md")]
    i = args.index("--model")
    assert args[i:i + 4] == ["--model", "@opus", "--effort", "xhigh"]
    record = state.read_json(paths.assignment_path("sid-K-task-1"))
    assert "role" not in record and record["context_role"] == "GenericWorker"


def test_an_old_snapshot_node_without_the_legacy_file_pends_as_spawn_error(fresh_orchestrator_dir, tmp_path, monkeypatch):
    home = tmp_path / "claude-home"
    home.mkdir()
    monkeypatch.setattr(config, "claude_config_home", lambda: home)
    old = SCHEMA.replace('role = "IMPL_HARD"\nmodel = "@opus"\neffort = "xhigh"', 'role = "IMPL_HARD"', 1)
    r, calls = _old_snapshot_launch(monkeypatch, tmp_path, old)
    assert r["state"] == "pending" and calls == []
    assert "has no claude model for it; rework the work" in r["reason"]
    assert _fold().open_requests[0]["pending"]["reason"] == "spawn-error"


def test_a_profiled_node_spawns_with_one_settings_file_of_preset_and_profile(fresh_orchestrator_dir, tmp_path,
                                                                             monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    _profiled_setup(monkeypatch, tmp_path)
    r, args = _started(monkeypatch)
    path = _settings_file(args)
    assert path == paths.artifact_ticket_dir("K") / "settings" / f"{r['request_id']}.json"
    data = json.loads(path.read_text())
    assert data["permissions"] == PRESET["permissions"] and data["enableAllProjectMcpServers"] is True
    assert data["claudeMdExcludes"] == ["/h/.claude*/rules/a.md"] and data["skillOverrides"] == {"x-skill": "off"}
    assert data["skillListingMaxDescChars"] == 160
    assert "--remote-control" not in args
    assert args[args.index("--model"):args.index("--model") + 4] == ["--model", "@opus", "--effort", "xhigh"]


def test_a_profiled_node_with_its_own_settings_file_merges_into_that_file(fresh_orchestrator_dir, tmp_path,
                                                                          monkeypatch):
    own = tmp_path / "verifier.json"
    own.write_text(json.dumps({"permissions": {"deny": ["Write"]}}))
    _profiled_setup(monkeypatch, tmp_path, extra=f'extra_args = ["--settings", "{own}"]')
    _, args = _started(monkeypatch)
    data = json.loads(_settings_file(args).read_text())
    assert data["permissions"] == {"deny": ["Write"]} and "enableAllProjectMcpServers" not in data
    assert data["claudeMdExcludes"] == ["/h/.claude*/rules/a.md"]
    assert str(own) not in args


def test_a_node_with_its_own_settings_file_and_no_profile_spawns_over_that_file(fresh_orchestrator_dir, tmp_path,
                                                                                monkeypatch):
    own = tmp_path / "own.json"
    own.write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"], "defaultMode": "auto"},
                               "env": {"CLAUDE_CODE_THRIFTY_SONIC": "0"}}))
    _setup_schema(monkeypatch, tmp_path, SCHEMA.replace(
        'extra_args = []\n',
        f'extra_args = ["--settings", "{own}"]\n\n[nodes.design.subagents.scout]\ntype = "Explore"\n'
        'model = "sonnet"\neffort = "xhigh"\nstep = "searching a repo"\n', 1))
    r, args = _started(monkeypatch)
    path = _settings_file(args)
    assert path == paths.artifact_ticket_dir("K") / "settings" / f"{r['request_id']}.json"
    data = json.loads(path.read_text())
    assert data["env"] == {"CLAUDE_CODE_THRIFTY_SONIC": "0"}
    assert data["permissions"]["allow"] == ["Bash(ls:*)"] and "enableAllProjectMcpServers" not in data
    assert any(e.get("matcher") == agent_guard.MATCHER and "agent-guard" in e["hooks"][0]["command"]
               for e in data["hooks"]["PreToolUse"])

def test_a_profiled_node_with_inline_settings_merges_into_them(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _profiled_setup(monkeypatch, tmp_path,
                    extra='extra_args = ["--settings", "{\\"permissions\\": {\\"allow\\": [\\"A\\"]}}"]')
    _, args = _started(monkeypatch)
    data = json.loads(_settings_file(args).read_text())
    assert data["permissions"] == {"allow": ["A"]} and data["skillOverrides"] == {"x-skill": "off"}


def test_remote_control_drops_the_preset_remote_keys_and_keeps_the_flag(fresh_orchestrator_dir, tmp_path,
                                                                        monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_RC", "1")
    _profiled_setup(monkeypatch, tmp_path,
                    preset={**PRESET, "remoteControlAtStartup": False, "disableRemoteControl": True})
    _, args = _started(monkeypatch)
    data = json.loads(_settings_file(args).read_text())
    assert "remoteControlAtStartup" not in data and "disableRemoteControl" not in data
    assert "--remote-control" in args


def test_a_node_without_a_profile_spawns_with_the_old_arguments(fresh_orchestrator_dir, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_RC", raising=False)
    _setup_schema(monkeypatch, tmp_path)
    _, args = _started(monkeypatch)
    node_args = ["--model", "@opus", "--effort", "xhigh"]
    assert args == _claude_worker_settings_args(node_args) + node_args
    assert not (paths.artifact_ticket_dir("K") / "settings").exists()


def test_resume_replays_the_profiled_settings_file(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _profiled_setup(monkeypatch, tmp_path)
    r, args = _started(monkeypatch)
    assert str(_settings_file(args)) in _resume_settings_args(r["next"]["sid"])


def test_the_pane_command_carries_the_settings_path_never_the_json(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _profiled_setup(monkeypatch, tmp_path)
    _, args = _started(monkeypatch)
    command = spawner._runtime_command("claude", "brief", args)
    assert str(_settings_file(args)) in command and "claudeMdExcludes" not in command


def test_node_settings_args_refuses_an_unreadable_base(fresh_orchestrator_dir, tmp_path):
    with pytest.raises(ValueError):
        _node_settings_args("K", "r1", ["--settings", str(tmp_path / "gone.json")], {"skillListingMaxDescChars": 1})


def test_node_settings_args_refuses_a_profile_carrying_permissions(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="permissions") as err:
        _node_settings_args("T-42", "r1", ["--effort", "x"], {"permissions": {"allow": ["Bash"]}})
    assert "T-42" in str(err.value)
    assert not (paths.artifact_ticket_dir("T-42") / "settings").exists()


def test_node_settings_args_refuses_a_base_whose_profile_keys_are_malformed(fresh_orchestrator_dir, tmp_path):
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"claudeMdExcludes": [{"not": "a string"}]}))
    with pytest.raises(ValueError, match="claudeMdExcludes") as err:
        _node_settings_args("T-42", "r1", ["--settings", str(base)], {"claudeMdExcludes": ["/h/a.md"]})
    assert "T-42" in str(err.value)
    assert not (paths.artifact_ticket_dir("T-42") / "settings").exists()


def test_a_cwd_on_advance_spawns_the_next_node_there_and_moves_the_work(fresh_orchestrator_dir, tmp_path,
                                                                       monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid, plan='{"tasks": [{"id": 1}, {"id": 2}]}')
    moved = tmp_path / "worktree"
    moved.mkdir()
    calls = _fake_registration(monkeypatch, ["registered", "registered"])
    r = asyncio.run(pipeline_advance_impl(sid, "K", "task", cwd=str(moved)))
    assert calls[0]["cwd"] == str(moved) and f"cwd={moved}" in calls[0]["initial_prompt"]
    assert [e.get("cwd") for e in _events() if e["type"] == "advance_requested"][-1] == str(moved)
    assert _fold().tokens["t1"].cwd == str(moved)
    task_sid = r["next"]["sid"]
    artifact_put_impl("K", "review", "task-1", "APPROVE", "complete", task_sid, scope="output")
    asyncio.run(pipeline_advance_impl(task_sid, "K", "task"))
    assert calls[1]["cwd"] == str(moved) and _fold().tokens["t1"].cwd == str(moved)


@pytest.mark.parametrize("bad", ["relative/dir", "/no/such/dir/anywhere", ""])
def test_a_cwd_that_is_not_an_existing_absolute_directory_is_refused(fresh_orchestrator_dir, tmp_path, monkeypatch,
                                                                    bad):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    calls = _fake_registration(monkeypatch, ["registered"])
    with pytest.raises(ValueError, match="must be an absolute path to an existing directory"):
        asyncio.run(pipeline_advance_impl(sid, "K", "task", cwd=bad))
    assert calls == [] and _fold().tokens["t1"].node == "design" and _fold().tokens["t1"].cwd == "/w"
    assert _events()[-1]["type"] == "advance_refused"


def test_an_override_resnapshot_with_a_moved_cwd_validates_the_schema_there(fresh_orchestrator_dir, tmp_path,
                                                                           monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    moved = tmp_path / "worktree"
    moved.mkdir()
    real = mcp_server._schema_snapshot_text
    seen = []

    def spy(schema, cwd):
        seen.append(cwd)
        return real(schema, cwd)

    monkeypatch.setattr("dockwright.mcp_server._schema_snapshot_text", spy)
    calls = _fake_registration(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason="move", resnapshot=True,
                                          cwd=str(moved)))
    assert seen == [str(moved)] and r["state"] == "committed"
    assert calls[0]["cwd"] == str(moved) and _fold().tokens["t1"].cwd == str(moved)
    assert [e for e in _events() if e["type"] == "schema_snapshot"][-1]["schema"] == "code-ticket"


def test_a_cwd_is_stored_normalized(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    moved = tmp_path / "worktree"
    moved.mkdir()
    calls = _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl(sid, "K", "task", cwd=f"{tmp_path}/./worktree/"))
    assert [e.get("cwd") for e in _events() if e["type"] == "advance_requested"][-1] == str(moved)
    assert calls[0]["cwd"] == str(moved) and _fold().tokens["t1"].cwd == str(moved)
    assert f"cwd={moved}\n" in calls[0]["initial_prompt"]


def test_status_shows_the_cwd_a_pending_request_moves_the_work_to(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    moved = tmp_path / "worktree"
    moved.mkdir()
    _fake_registration(monkeypatch, ["no_register"])
    request_id = asyncio.run(pipeline_advance_impl(sid, "K", "task", cwd=str(moved)))["request_id"]
    token = pipeline_status_impl("K").split("## token", 1)[1].split("\n## ", 1)[0]
    assert "- t1: node=design index=1" in token and "cwd=/w " in token
    request = next(line for line in token.splitlines() if line.startswith(f"- open request {request_id} "))
    assert request.endswith(f" cwd={moved}")


def test_the_manager_holds_and_releases_a_token_and_a_node_is_still_refused(fresh_orchestrator_dir, tmp_path,
                                                                           monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="next node unknown to the caller; ask_manager with the reason"):
        asyncio.run(pipeline_advance_impl(sid, "K", "?", reason="unsure"))
    with pytest.raises(ValueError, match="no hold to release"):
        asyncio.run(pipeline_advance_impl("m1", "K", "?"))
    calls = _fake_registration(monkeypatch, [])
    r = asyncio.run(pipeline_advance_impl("m1", "K", "?", reason="wait for the reporter"))
    assert r == {"ok": True, "state": "held", "request_id": None, "next": None} and calls == []
    assert _fold().held == {"t1": "wait for the reporter"} and _fold().tokens["t1"].node == "design"
    assert "held: wait for the reporter" in pipeline_status_impl("K")
    r = asyncio.run(pipeline_advance_impl("m1", "K", "?"))
    assert r == {"ok": True, "state": "released", "request_id": None, "next": None} and _fold().held == {}


def test_status_prints_a_multi_line_hold_reason_on_the_token_line(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    asyncio.run(pipeline_advance_impl("m1", "K", "?", reason="wait for\n  the reporter"))
    token_line = next(line for line in pipeline_status_impl("K").splitlines() if line.startswith("- t1: "))
    assert token_line.endswith(" held: wait for the reporter")


def test_a_hold_with_a_bad_cwd_is_refused_as_a_hold_that_takes_only_a_reason(fresh_orchestrator_dir, tmp_path,
                                                                             monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="a hold or a release takes only a reason"):
        asyncio.run(pipeline_advance_impl("m1", "K", "?", reason="wait", cwd="relative/dir"))
    assert _fold().held == {} and _fold().tokens["t1"].cwd == "/w"


def test_a_hold_is_refused_while_an_advance_is_in_flight(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    _complete_design_outputs(sid)
    _fake_registration(monkeypatch, ["no_register"])
    assert asyncio.run(pipeline_advance_impl(sid, "K", "task"))["state"] == "pending"
    with pytest.raises(ValueError, match="advance in flight"):
        asyncio.run(pipeline_advance_impl("m1", "K", "?", reason="wait"))
    assert _fold().held == {}


@pytest.mark.parametrize("hld,explainer,expected", [
    ("internal: yes — the subject is dockwright itself", False, ("@sonnet", "xhigh")),
    ("internal: no — the effect test fired", False, ("@opus", "xhigh")),
    (None, False, ("@opus", "xhigh")),
    ("internal: yes — the subject is dockwright itself", True, ("@opus", "xhigh")),
])
def test_a_ruled_node_launches_on_the_row_its_rule_picks(fresh_orchestrator_dir, tmp_path, monkeypatch, hld,
                                                         explainer, expected):
    assert _ruled_post_merge_launch(monkeypatch, tmp_path, hld, explainer) == expected
    assert "artifact_read" not in _types()


def test_a_restarted_ruled_node_takes_the_same_row(fresh_orchestrator_dir, tmp_path, monkeypatch):
    launch = _ruled_post_merge_launch(monkeypatch, tmp_path, "internal: yes — dockwright", suffix="-r1")
    assert launch == ("@sonnet", "xhigh")


@pytest.mark.parametrize("hld,explainer,expected", [
    ("internal: yes — the subject is dockwright itself", False, ["--model", "@sonnet", "--effort", "high"]),
    ("internal: no — the effect test fired", False, ["--model", "@opus", "--effort", "xhigh"]),
    (None, False, ["--model", "@opus", "--effort", "xhigh"]),
    ("internal: yes — the subject is dockwright itself", True, ["--model", "@opus", "--effort", "xhigh"]),
])
def test_a_model_rule_switches_the_model_and_the_effort_together(fresh_orchestrator_dir, tmp_path, monkeypatch, hld,
                                                                 explainer, expected):
    args, record = _post_merge_launch(monkeypatch, tmp_path, MODEL_RULED_SCHEMA, hld, explainer)
    i = args.index("--model")
    assert args[i:i + 4] == expected and args.count("--model") == 1 and args.count("--effort") == 1
    assert "role" not in record


def _daemon_alive(tmp_path, monkeypatch, fresh=True):
    monkeypatch.setattr(paths, "DAEMON", tmp_path / "daemon")
    monkeypatch.setattr(paths, "DAEMON_STOP", tmp_path / "daemon-stop")
    paths.DAEMON.mkdir(exist_ok=True)
    state.write_json_atomic(paths.DAEMON / "alive.json", {"ts": time.time() - (10 if fresh else 1000), "awake": 1.0, "pid": 1})


def test_the_daemon_record_resolves_for_the_pipeline_tool_only_while_the_heartbeat_is_fresh(fresh_orchestrator_dir,
                                                                                            tmp_path, monkeypatch):
    _daemon_alive(tmp_path, monkeypatch)
    record = mcp_server._resolve_parent_record("daemon", tool="pipeline")
    assert record["name"] == "daemon" and record["agent"] == "daemon" and record["kind"] == "system"
    for tool in ("spawn_worker", "spawn_child", "wait_for_child", "list_children", "topic"):
        with pytest.raises(ValueError, match="does not match any active"):
            mcp_server._resolve_parent_record("daemon", tool=tool)
    _daemon_alive(tmp_path, monkeypatch, fresh=False)
    with pytest.raises(ValueError, match="daemon is not running"):
        mcp_server._resolve_parent_record("daemon", tool="pipeline")
    paths.DAEMON_STOP.write_text("")
    assert mcp_server._daemon_record() is None


def test_pipeline_start_impl_under_the_daemon_starts_the_work_with_the_given_domain(fresh_orchestrator_dir, tmp_path,
                                                                                   monkeypatch):
    _setup_schema(monkeypatch, tmp_path)
    _daemon_alive(tmp_path, monkeypatch)
    calls = _fake_registration(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_start_impl("daemon", "K", "code-ticket", "/w", "nightly", domain="ops"))
    assert r["state"] == "committed" and r["next"]["name"] == "K-design"
    started = [e for e in _events() if e["type"] == "pipeline_started"][0]
    assert started["manager"] == "daemon" and started["actor_sid"] == "daemon"
    assert calls[0]["env"]["CLAUDE_PARENT_MANAGER"] == "daemon" and calls[0]["env"]["CLAUDE_PARENT_SID"] == "daemon"
    from dockwright import topic
    assert topic.load("K")["domain"] == "ops"
    assert _fold().tokens["t1"].manager == "daemon"


def test_a_domain_argument_wins_over_the_managers_domain(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _setup_schema(monkeypatch, tmp_path)
    _manager()
    _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_start_impl("m1", "K", "code-ticket", "/w", "ask", domain="ops"))
    from dockwright import topic
    assert topic.load("K")["domain"] == "ops"


def test_the_mcp_pipeline_start_tool_refuses_the_daemon_sid(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _daemon_alive(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="the daemon starts works only from its own pass"):
        asyncio.run(mcp_server.pipeline_start("daemon", "K", "code-ticket", "/w", "ask"))


def test_a_node_under_the_daemon_advances_and_spawns_the_next_node_under_the_daemon(fresh_orchestrator_dir, tmp_path,
                                                                                   monkeypatch):
    _setup_schema(monkeypatch, tmp_path)
    _daemon_alive(tmp_path, monkeypatch)
    calls = _fake_registration(monkeypatch, {"K-design": "registered", "K-task-1": "registered"})
    r = asyncio.run(pipeline_start_impl("daemon", "K", "code-ticket", "/w", "ask"))
    sid = r["next"]["sid"]
    for full, body in (("design.hld", "x"), ("plan.tasks", '{"tasks": [{"id": 1}]}')):
        artifact_put_impl("K", *full.split("."), body, "complete", writer_sid=sid, scope="output")
    r2 = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    assert r2["state"] == "committed"
    assert calls[-1]["env"]["CLAUDE_PARENT_MANAGER"] == "daemon"
    assert state.read_json(paths.assignment_path(r2["next"]["sid"]))["manager_sid"] == "daemon"


def test_a_node_under_a_dead_daemon_pends_with_no_manager(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _setup_schema(monkeypatch, tmp_path)
    _daemon_alive(tmp_path, monkeypatch)
    _fake_registration(monkeypatch, ["registered"])
    r = asyncio.run(pipeline_start_impl("daemon", "K", "code-ticket", "/w", "ask"))
    sid = r["next"]["sid"]
    for full, body in (("design.hld", "x"), ("plan.tasks", '{"tasks": [{"id": 1}]}')):
        artifact_put_impl("K", *full.split("."), body, "complete", writer_sid=sid, scope="output")
    _daemon_alive(tmp_path, monkeypatch, fresh=False)
    r2 = asyncio.run(pipeline_advance_impl(sid, "K", "task"))
    assert r2["state"] == "pending" and r2["reason"] == "no-manager"


def test_the_daemon_caller_has_manager_authority_only_while_the_daemon_is_alive(fresh_orchestrator_dir, tmp_path,
                                                                               monkeypatch):
    _daemon_alive(tmp_path, monkeypatch)
    assert mcp_server._caller_for("daemon")[0].agent == "manager"
    _daemon_alive(tmp_path, monkeypatch, fresh=False)
    assert mcp_server._caller_for("daemon")[0].agent == "worker"
