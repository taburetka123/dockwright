import asyncio
import json
from pathlib import Path

import pytest

from dockwright import paths, spawner, state, terminal, topic, topic_agent
from dockwright import mcp_server as ms
from dockwright.providers import get_provider
from dockwright.providers.base import ChildRound, REPLY_MODE
from tests.test_mcp_tools import fresh_orchestrator_dir  # noqa: F401

REPLY_PRESET = Path(__file__).resolve().parents[1] / "deploy" / "presets" / "reply-settings.json"


def _manager(sid, name, domain):
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", {"claude_sid": sid, "name": name, "agent": "manager",
                                                          "domain": domain, "pid": 1})


def _card(key, domain):
    topic.update(key, lambda _: topic.new_card("note", domain, "renew the cert"))


@pytest.fixture(autouse=True)
def provider_table(tmp_path, monkeypatch):
    cfg = tmp_path / "dw.toml"
    cfg.write_text('[subworkers.providers.claude-exec]\nmodel = "@opus"\neffort = "high"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    return cfg


def test_the_agent_spawns_under_the_domain_manager_with_the_reply_mode(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager("m1", "mgr-meta", "meta")
    _card("note-meta-x", "meta")
    calls = []

    async def fake_spawn(brief, **kwargs):
        calls.append((brief, kwargs))
        return {"name": kwargs["name"], "status": "registered"}

    monkeypatch.setattr(ms, "spawn_worker_impl", fake_spawn)
    out = asyncio.run(topic_agent.spawn("note-meta-x", "decision.msg-20260930T101010"))
    assert out == {"route": "agent", "ok": True, "reason": None}
    brief, kwargs = calls[0]
    assert kwargs["manager_sid"] == "m1" and kwargs["name"] == "note-meta-x-agent"
    assert (kwargs["provider"], kwargs["mode"], kwargs["_tool"]) == ("claude-exec", "reply", "topic")
    assert "role" not in kwargs and (kwargs["model"], kwargs["effort"]) == ("@opus", "high")


    assert kwargs["env"] == {"DOCKWRIGHT_TOPIC_KEY": "note-meta-x"}
    assert "decision.msg-20260930T101010" in brief and "reply." in brief and "new work" in brief
    folder = paths.topic_export_dir("note-meta-x")
    assert folder.is_dir()
    assert paths.EXPORTS.stat().st_mode & 0o777 == 0o700
    assert str(folder) in brief
    assert "content_path=<that file>" in brief




def test_an_empty_provider_model_or_effort_refuses_the_agent(fresh_orchestrator_dir, monkeypatch, tmp_path):
    cfg = tmp_path / "dw.toml"
    cfg.write_text('[subworkers.providers.claude-exec]\nmodel = "@opus"\neffort = ""\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    _manager("m1", "mgr-meta", "meta")
    _card("note-meta-x", "meta")
    monkeypatch.setattr(ms, "spawn_worker_impl", pytest.fail)
    out = asyncio.run(topic_agent.spawn("note-meta-x", "decision.msg-1"))
    assert out["ok"] is False and "[subworkers.providers.claude-exec] model/effort is unset" in out["reason"]


def test_an_export_folder_it_cannot_trust_refuses_the_agent(fresh_orchestrator_dir, monkeypatch):
    _manager("m1", "mgr-meta", "meta")
    _card("note-meta-x", "meta")
    elsewhere = fresh_orchestrator_dir / "elsewhere"
    elsewhere.mkdir()
    paths.EXPORTS.symlink_to(elsewhere)
    monkeypatch.setattr(ms, "spawn_worker_impl", pytest.fail)
    out = asyncio.run(topic_agent.spawn("note-meta-x", "decision.msg-1"))
    assert out["ok"] is False and "not a folder this user owns" in out["reason"]


def test_no_live_manager_means_no_agent(fresh_orchestrator_dir, monkeypatch):
    _card("note-meta-x", "meta")
    monkeypatch.setattr(ms, "spawn_worker_impl", pytest.fail)
    assert asyncio.run(topic_agent.spawn("note-meta-x", "decision.msg-1"))["reason"] == "no manager"


def test_a_running_agent_is_not_spawned_twice(fresh_orchestrator_dir, monkeypatch):
    _manager("m1", "mgr-meta", "meta")
    _card("note-meta-x", "meta")
    state.write_json_atomic(paths.ACTIVE / "s9.json", {"claude_sid": "s9", "name": "note-meta-x-agent",
                                                      "agent": "worker", "pid": 1})
    monkeypatch.setattr(ms, "spawn_worker_impl", pytest.fail)
    assert asyncio.run(topic_agent.spawn("note-meta-x", "decision.msg-1"))["reason"] == "agent still answering"


def test_a_late_registering_agent_counts_as_delivered(fresh_orchestrator_dir, monkeypatch):
    _manager("m1", "mgr-meta", "meta")
    _card("note-meta-x", "meta")

    async def late(brief, **kwargs):
        return {"status": "no_register", "reason": "did not register within 12s"}

    monkeypatch.setattr(ms, "spawn_worker_impl", late)
    assert asyncio.run(topic_agent.spawn("note-meta-x", "decision.msg-1"))["ok"]


def test_an_agent_that_died_before_registering_is_queued(fresh_orchestrator_dir, monkeypatch):
    _manager("m1", "mgr-meta", "meta")
    _card("note-meta-x", "meta")

    async def died(brief, **kwargs):
        return {"status": "provider-error", "reason": "login expired"}

    monkeypatch.setattr(ms, "spawn_worker_impl", died)
    assert asyncio.run(topic_agent.spawn("note-meta-x", "decision.msg-1")) == \
        {"route": "agent", "ok": False, "reason": "provider-error"}
    state.write_json_atomic(paths.ASSIGNMENTS_PENDING / "a1.json", {"assignment_id": "a1",
                                                                   "name": "note-meta-x-agent"})

    async def registered(brief, **kwargs):
        return {"status": "registered"}

    monkeypatch.setattr(ms, "spawn_worker_impl", registered)
    assert asyncio.run(topic_agent.spawn("note-meta-x", "decision.msg-1"))["ok"]


def test_an_agent_whose_pane_died_before_registering_is_queued(fresh_orchestrator_dir, monkeypatch, tmp_path):
    _manager("m1", "mgr-meta", "meta")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    topic.update("note-meta-x", lambda _: topic.new_card("note", "meta", "renew the cert", worktree=str(worktree)))

    async def spawn(**kwargs):
        kwargs["child_round"].log_path.write_text("Error: settings file not found\n")
        return ("%5", kwargs["name"])

    monkeypatch.setattr(spawner, "spawn_worker_tab", spawn)
    monkeypatch.setattr(terminal.TmuxDriver, "pane_state", lambda self, target: "dead" if target == "%5" else "gone")
    assert asyncio.run(topic_agent.spawn("note-meta-x", "decision.msg-1")) == \
        {"route": "agent", "ok": False, "reason": "provider-error"}


def test_a_paused_account_queues_the_agent(fresh_orchestrator_dir, monkeypatch):
    _manager("m1", "mgr-meta", "meta")
    _card("note-meta-x", "meta")

    async def paused(brief, **kwargs):
        return {"status": "paused", "reason": "5h window"}

    monkeypatch.setattr(ms, "spawn_worker_impl", paused)
    assert asyncio.run(topic_agent.spawn("note-meta-x", "decision.msg-1"))["reason"] == "paused"


def _reply_round(tmp_path, **fields):
    return ChildRound(mode=REPLY_MODE, cwd=str(tmp_path), brief_path=tmp_path / "b",
                      report_path=tmp_path / "r", log_path=tmp_path / "l", model="@opus", effort="high", **fields)


def _deploy_reply_preset(bin_path="/opt/dockwright/bin/dockwright"):
    paths.PRESETS.mkdir(parents=True, exist_ok=True)
    (paths.PRESETS / "reply-settings.json").write_text(REPLY_PRESET.read_text().replace("@@DOCKWRIGHT_BIN@@", bin_path))


def test_a_reply_session_is_refused_until_its_guard_is_rendered(tmp_path):
    provider = get_provider("claude-exec")
    with pytest.raises(ValueError, match="reply-settings.json"):
        provider.spawn_command(_reply_round(tmp_path))
    _deploy_reply_preset("@@DOCKWRIGHT_BIN@@")
    with pytest.raises(ValueError, match="reply-settings.json"):
        provider.spawn_command(_reply_round(tmp_path))
    with pytest.raises(ValueError, match="reply-settings.json"):
        provider.resume_command(_reply_round(tmp_path, resume_sid="s"))
    _deploy_reply_preset()
    assert str(paths.PRESETS / "reply-settings.json") in provider.spawn_command(_reply_round(tmp_path))


def test_a_reply_preset_without_its_guard_hook_is_refused(tmp_path):
    provider = get_provider("claude-exec")
    paths.PRESETS.mkdir(parents=True, exist_ok=True)
    preset = paths.PRESETS / "reply-settings.json"
    guardless = [
        {"permissions": {"allow": ["mcp__dockwright__*"]}},
        {"hooks": {}},
        {"hooks": ["/opt/dw reply-guard"]},
        {"hooks": {"PreToolUse": {"matcher": "*"}}},
        [],
        {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
                                                                 "command": "/opt/dw reply-guard"}]}]}},
        {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "echo ok"}]}]}},
    ]
    for settings in guardless:
        preset.write_text(json.dumps(settings))
        with pytest.raises(ValueError, match="reply-guard"):
            provider.spawn_command(_reply_round(tmp_path))
    preset.write_text("{not json")
    with pytest.raises(ValueError, match="reply-settings.json"):
        provider.spawn_command(_reply_round(tmp_path))


def test_an_agent_whose_guard_is_unrendered_is_queued_and_leaves_nothing_pending(fresh_orchestrator_dir, monkeypatch,
                                                                                 tmp_path):
    _manager("m1", "mgr-meta", "meta")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    topic.update("note-meta-x", lambda _: topic.new_card("note", "meta", "renew the cert", worktree=str(worktree)))
    _deploy_reply_preset("@@DOCKWRIGHT_BIN@@")

    async def spawn(**kwargs):
        return (kwargs["provider"].spawn_command(kwargs["child_round"]), kwargs["name"])

    monkeypatch.setattr(spawner, "spawn_worker_tab", spawn)
    out = asyncio.run(topic_agent.spawn("note-meta-x", "decision.msg-1"))
    assert (out["route"], out["ok"]) == ("agent", False)
    assert out["reason"].startswith("spawn refused: ") and "reply-settings.json" in out["reason"]
    assert list(paths.ASSIGNMENTS_PENDING.glob("*")) == []


def test_the_reply_mode_uses_its_preset_and_loads_the_users_mcp(tmp_path):
    _deploy_reply_preset()
    argv = get_provider("claude-exec")._argv(_reply_round(tmp_path), False)
    assert str(paths.PRESETS / "reply-settings.json") in argv
    assert "--strict-mcp-config" not in argv
    assert get_provider("claude-exec").bootstrap(REPLY_MODE) == ""


def test_only_the_topic_tool_may_ask_for_the_reply_mode(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="reply"):
        asyncio.run(ms.spawn_worker_impl("x", name="c", manager_sid=None, provider="claude-exec",
                                         mode="reply", _tool="spawn_child"))


def test_the_codex_provider_refuses_the_reply_mode(tmp_path):
    with pytest.raises(ValueError, match="reply"):
        get_provider("codex-exec").bootstrap(REPLY_MODE)


def test_the_reply_mode_runs_only_on_claude_exec(fresh_orchestrator_dir):
    with pytest.raises(ValueError, match="claude-exec"):
        asyncio.run(ms.spawn_worker_impl("x", name="c", manager_sid=None, provider="claude-tui",
                                         mode="reply", _tool="topic"))


def test_a_running_agent_under_a_uniquified_name_is_seen(fresh_orchestrator_dir, monkeypatch):
    _manager("m1", "mgr-meta", "meta")
    _card("note-meta-x", "meta")
    state.write_json_atomic(paths.ACTIVE / "s9.json", {"claude_sid": "s9", "name": "note-meta-x-agent-2",
                                                      "agent": "worker", "pid": 1})
    monkeypatch.setattr(ms, "spawn_worker_impl", pytest.fail)
    assert asyncio.run(topic_agent.spawn("note-meta-x", "decision.msg-1"))["reason"] == "agent still answering"


def test_another_topics_agent_does_not_block_this_one(fresh_orchestrator_dir, monkeypatch):
    _manager("m1", "mgr-meta", "meta")
    _card("note-meta-x", "meta")
    state.write_json_atomic(paths.ACTIVE / "s9.json", {"claude_sid": "s9", "name": "note-meta-x-agent-extra",
                                                      "agent": "worker", "pid": 1})

    async def registered(brief, **kwargs):
        return {"status": "registered"}

    monkeypatch.setattr(ms, "spawn_worker_impl", registered)
    assert asyncio.run(topic_agent.spawn("note-meta-x", "decision.msg-1"))["ok"]


def test_live_domains_are_the_domains_a_live_manager_accepts(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.setattr(ms, "_pid_alive", lambda pid: pid == 1)
    _manager("m1", "mgr-meta", "meta")
    _manager("m2", "mgr-general", None)
    _manager("m5", "mgr-odd", "not a slug")
    for sid, extra in (("m3", {"agent": "manager", "domain": "nested-only", "pid": 1, "nested": True}),
                       ("w1", {"agent": "worker", "domain": "worker-only", "pid": 1}),
                       ("m4", {"agent": "manager", "domain": "dead", "pid": 424299})):
        state.write_json_atomic(paths.ACTIVE / f"{sid}.json", {"claude_sid": sid, "name": sid, **extra})
    domains = topic_agent.live_domains()
    assert domains == ["general", "meta"]
    for domain in (*domains, "nested-only", "worker-only", "dead", "not a slug"):
        accepted = topic.is_key(domain) and topic_agent.live_domain_manager(domain) is not None
        assert accepted == (domain in domains), domain


def test_a_file_in_active_that_is_not_a_record_is_skipped(fresh_orchestrator_dir):
    _manager("m1", "mgr-meta", "meta")
    (paths.ACTIVE / "junk.json").write_text("[]")
    assert topic_agent.live_domains() == ["meta"]
    assert topic_agent.live_domain_manager("meta") == "m1"
