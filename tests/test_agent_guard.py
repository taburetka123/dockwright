import asyncio
import json
import os
import subprocess
import sys

from dockwright import agent_guard, config, mcp_server, paths, pipeline, spawner, state
from dockwright.pipeline_child import pipeline_child_impl
from tests.test_pipeline_core import SCHEMA
from tests.test_pipeline_reconcile import (_fake_registration, _manager, _setup_schema, _started_at_design,  # noqa: F401
                                           fresh_orchestrator_dir)

REAL_CONFIRM = mcp_server._confirm_spawn_registration
PRESET_HOOK = {"matcher": "Bash", "hooks": [{"type": "command", "command": "true", "timeout": 5}]}
LANE = 'role = "DocReviewer"\nmode = "report"\nmodel = "@sonnet"\neffort = "xhigh"\n'
DECLARED = SCHEMA.replace('extra_args = []\n',
                          'extra_args = []\n\n[nodes.design.subagents.scout]\ntype = "Explore"\n'
                          'model = "sonnet"\neffort = "xhigh"\n\n[nodes.design.children.reviewer]\nrounds = 2\n' + LANE, 1)
DECLARED_MODELED = (SCHEMA.replace('role = "SCOUT_DESIGN"\nmodel = "@opus"\neffort = "xhigh"',
                                   'role = "Designer"\nmodel = "@fable"\neffort = "medium"', 1)
                    .replace('extra_args = []\n',
                             '\n[nodes.design.subagents.scout]\ntype = "Explore"\nmodel = "sonnet"\neffort = "xhigh"\n'
                             '\n[nodes.design.children.reviewer]\nrounds = 2\n' + LANE, 1))
DECLARED_SLOT_SCOUT = DECLARED_MODELED.replace(
    'rounds = 2\n' + LANE, 'rounds = 2\n' + LANE + '\n[nodes.design.children.reviewer.subagents.scout]\ntype = "Explore"\n'
    'model = "sonnet"\neffort = "xhigh"\n', 1)


def _payload(**tool_input):
    return {"tool_name": "Agent", "tool_input": tool_input, "session_id": "s"}


def test_a_declared_pair_passes_and_a_missing_model_reads_as_inherit():
    assert agent_guard.decide(_payload(subagent_type="Explore", model="sonnet"), {"Explore:sonnet"}) is None
    assert agent_guard.decide(_payload(subagent_type="general-purpose"), {"general-purpose:inherit"}) is None


def test_an_undeclared_pair_is_denied_with_the_declared_list():
    out = agent_guard.decide(_payload(subagent_type="Explore", model="opus"), {"Explore:sonnet"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Explore:opus" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Explore:sonnet" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_an_empty_allow_list_denies_every_agent_call_and_a_missing_type_reads_as_general_purpose():
    assert agent_guard.decide(_payload(subagent_type="Explore", model="sonnet"), set())["hookSpecificOutput"][
        "permissionDecision"] == "deny"
    assert agent_guard.decide(_payload(model="opus"), {"general-purpose:opus"}) is None
    assert agent_guard.decide(_payload(model="opus"), {"Explore:opus"})["hookSpecificOutput"][
        "permissionDecision"] == "deny"


def test_other_tools_pass_and_a_task_call_is_judged_like_agent():
    assert agent_guard.decide({"tool_name": "Bash", "tool_input": {"command": "ls"}}, set()) is None
    assert agent_guard.decide({"tool_name": "Task", "tool_input": {"subagent_type": "Explore", "model": "opus"}},
                              {"Explore:sonnet"}) is not None


def test_a_workflow_call_is_denied_whatever_the_node_declares():
    out = agent_guard.decide({"tool_name": "Workflow", "tool_input": {"script": "x"}}, {"Explore:sonnet"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Workflow" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert pipeline.guard_hook([], "/bin/dockwright")["matcher"] == "Agent|Task|Workflow"


def _reason(out):
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_three_part_entry_admits_only_its_own_effort():
    allowed = {"Explore:sonnet:xhigh"}
    assert agent_guard.decide(_payload(subagent_type="Explore", model="sonnet", effort="xhigh"), allowed) is None
    bare = _reason(agent_guard.decide(_payload(subagent_type="Explore", model="sonnet"), allowed))
    assert "Explore:sonnet with no effort" in bare and "Explore:sonnet:xhigh" in bare
    assert "re-issue the call with that entry's model and effort" in bare
    other = _reason(agent_guard.decide(_payload(subagent_type="Explore", model="sonnet", effort="medium"), allowed))
    assert "Explore:sonnet:medium is not declared" in other


def test_a_two_part_entry_of_an_old_snapshot_admits_any_effort():
    for extra in ({}, {"effort": "low"}, {"effort": "xhigh"}):
        assert agent_guard.decide(_payload(subagent_type="Explore", model="sonnet", **extra), {"Explore:sonnet"}) is None


def test_a_named_call_is_denied_where_the_entry_declares_an_effort():
    allowed = {"general-purpose:opus:high"}
    assert agent_guard.decide(_payload(subagent_type="general-purpose", model="opus", effort="high"), allowed) is None
    assert agent_guard.decide(_payload(subagent_type="general-purpose", model="opus", effort="high", name=""),
                              allowed) is None
    named = _reason(agent_guard.decide(
        _payload(subagent_type="general-purpose", model="opus", effort="high", name="staging"), allowed))
    assert "`name`" in named and "teammate" in named and "without `name`" in named


def test_a_named_call_passes_a_two_part_entry():
    assert agent_guard.decide(_payload(subagent_type="Explore", model="sonnet", name="scout"), {"Explore:sonnet"}) is None


def test_a_call_with_no_type_and_no_model_reads_as_general_purpose_inherit_and_needs_the_effort():
    assert agent_guard.decide(_payload(effort="xhigh"), {"general-purpose:inherit:xhigh"}) is None
    assert "general-purpose:inherit with no effort" in _reason(agent_guard.decide(_payload(),
                                                                                  {"general-purpose:inherit:xhigh"}))


def test_the_cli_admits_a_three_part_entry_only_with_its_effort():
    def run(payload):
        return subprocess.run([sys.executable, "-m", "dockwright", "agent-guard", "--allow", "Explore:sonnet:xhigh"],
                              input=json.dumps(payload), capture_output=True, text=True)

    ok = run(_payload(subagent_type="Explore", model="sonnet", effort="xhigh"))
    assert ok.returncode == 0 and ok.stdout.strip() == ""
    bad = run(_payload(subagent_type="Explore", model="sonnet"))
    assert json.loads(bad.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_cli_reads_stdin_and_denies_garbage():
    def run(stdin, *allow):
        return subprocess.run([sys.executable, "-m", "dockwright", "agent-guard", *allow], input=stdin,
                              capture_output=True, text=True)

    ok = run(json.dumps(_payload(subagent_type="Explore", model="sonnet")), "--allow", "Explore:sonnet")
    assert ok.returncode == 0 and ok.stdout.strip() == ""
    bad = run("not json", "--allow", "Explore:sonnet")
    assert json.loads(bad.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def _write_preset(name, settings):
    paths.PRESETS.mkdir(parents=True, exist_ok=True)
    (paths.PRESETS / name).write_text(json.dumps(settings))


def _settings_of(extra_args):
    _, value = pipeline.split_settings_arg(list(extra_args))
    assert value is not None
    return pipeline.read_settings_value(value)


def _guards(settings):
    return [e for e in (settings.get("hooks") or {}).get("PreToolUse") or []
            if e.get("matcher") == agent_guard.MATCHER]


def _start(monkeypatch, tmp_path, schema_text):
    _setup_schema(monkeypatch, tmp_path, schema_text=schema_text)
    _manager()
    calls = _fake_registration(monkeypatch, ["registered"])
    from dockwright.mcp_server import pipeline_start_impl
    r = asyncio.run(pipeline_start_impl("m1", "K", "code-ticket", "/w", "ask"))
    return r, calls


def test_a_node_of_a_declared_schema_gets_the_guard_with_its_pairs(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _write_preset("worker-headless-settings.json",
                  {"permissions": {"allow": ["Bash(git status:*)"]}, "hooks": {"PreToolUse": [PRESET_HOOK]}})
    _, calls = _start(monkeypatch, tmp_path, DECLARED)
    settings = _settings_of(calls[0]["extra_args"])
    pre = settings["hooks"]["PreToolUse"]
    assert PRESET_HOOK in pre
    [guard] = _guards(settings)
    command = guard["hooks"][0]["command"]
    assert "agent-guard" in command and "--allow Explore:sonnet" in command and "|| echo" in command
    assert settings["permissions"]["allow"] == ["Bash(git status:*)"]


def test_a_node_of_an_old_schema_gets_no_guard(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _write_preset("worker-headless-settings.json",
                  {"permissions": {"allow": ["Bash(git status:*)"]}, "hooks": {"PreToolUse": [PRESET_HOOK]}})
    _, calls = _start(monkeypatch, tmp_path, SCHEMA)
    assert _guards(_settings_of(calls[0]["extra_args"])) == []
    assert not (paths.artifact_ticket_dir("K") / "settings").is_dir() or not any(
        _guards(json.loads(p.read_text())) for p in (paths.artifact_ticket_dir("K") / "settings").glob("*.json"))


def test_a_modeled_node_gets_the_guard_with_its_three_part_entries(fresh_orchestrator_dir, tmp_path, monkeypatch):
    _write_preset("worker-headless-settings.json", {"hooks": {"PreToolUse": [PRESET_HOOK]}})
    _, calls = _start(monkeypatch, tmp_path, DECLARED_MODELED)
    [guard] = _guards(_settings_of(calls[0]["extra_args"]))
    assert "--allow Explore:sonnet:xhigh" in guard["hooks"][0]["command"]


def _child_guard_command(monkeypatch, tmp_path, schema_text):
    _write_preset("verifier-settings.json",
                  {"permissions": {"deny": ["Write"]}, "hooks": {"PreToolUse": [PRESET_HOOK]}})
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=schema_text)
    captured = []

    async def fake_tab(**kw):
        captured.append(kw)
        child_sid = f"sid-{kw['name']}"
        state.write_json_atomic(paths.ACTIVE / f"{child_sid}.json", {
            "claude_sid": child_sid, "agent": "worker", "name": kw["name"], "cwd": kw["cwd"], "window_id": "999",
            "pid": os.getpid(), "started_at": 0})
        return ("999", kw["name"])

    monkeypatch.setattr(spawner, "spawn_worker_tab", fake_tab)
    monkeypatch.setattr("dockwright.mcp_server._confirm_spawn_registration", REAL_CONFIRM)
    monkeypatch.setattr("dockwright.mcp_server._DEFAULT_REGISTRATION_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr("dockwright.mcp_server._DEFAULT_REGISTRATION_POLL_SEC", 0.01)
    asyncio.run(pipeline_child_impl(sid, "K", child="reviewer", message="review this"))
    claude = [kw for kw in captured if kw.get("provider") is None]
    assert len(claude) == 1
    settings = _settings_of(claude[0]["extra_args"])
    assert PRESET_HOOK in settings["hooks"]["PreToolUse"] and settings["permissions"]["deny"] == ["Write"]
    [guard] = _guards(settings)
    return guard["hooks"][0]["command"]


def test_a_child_gets_the_guard_with_its_slots_pairs(fresh_orchestrator_dir, tmp_path, monkeypatch):
    command = _child_guard_command(monkeypatch, tmp_path, DECLARED)
    assert "agent-guard" in command and "--allow" not in command


def test_a_child_of_a_modeled_node_gets_its_slots_three_part_entries(fresh_orchestrator_dir, tmp_path, monkeypatch):
    assert "--allow Explore:sonnet:xhigh" in _child_guard_command(monkeypatch, tmp_path, DECLARED_SLOT_SCOUT)
