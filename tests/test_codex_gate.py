import asyncio
import json
import os
from pathlib import Path

import pytest

from dockwright import config, mcp_server, paths, spawner, state
from dockwright.mcp_server import register_self_impl, resume_worker_impl, spawn_worker_impl
from dockwright.providers import ChildRound, get_provider, register_provider, unregister_provider
from tests.conftest import allow_codex


@pytest.fixture
def fresh_orchestrator_dir(tmp_path, monkeypatch):
    for attr, sub in (("ROOT", ""), ("ACTIVE", "active"), ("QUESTIONS", "questions"), ("ANSWERS", "answers"),
                      ("DONE", "done"), ("CLOSED", "closed"), ("HANDOFFS", "handoffs"), ("PRESETS", "presets"),
                      ("TURN_ENDS", "turn-ends"), ("MANAGER_MEMORY", "manager-memory"), ("SLOTS", "slots"),
                      ("ARTIFACTS", "artifacts"), ("ASSIGNMENTS", "assignments")):
        monkeypatch.setattr(paths, attr, tmp_path / sub if sub else tmp_path)
    monkeypatch.setattr(paths, "ASSIGNMENTS_PENDING", tmp_path / "assignments" / ".pending")
    monkeypatch.setattr(paths, "MANAGER_TRIGGERS_LOG", tmp_path / "manager-triggers.jsonl")
    monkeypatch.setattr(paths, "SPEND_LEDGER", tmp_path / "spend-ledger.jsonl")
    monkeypatch.delenv("CLAUDE_PARENT_SID", raising=False)
    monkeypatch.delenv("CLAUDE_PARENT_MANAGER", raising=False)
    paths.ensure_dirs()
    yield tmp_path


class _FakeProc:
    returncode = 0

    async def communicate(self, input=None):
        return (b"%1", b"")


def _patch_exec(monkeypatch):
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return _FakeProc()

    monkeypatch.setattr(spawner.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(paths, "ACCOUNT_ACTIVE", Path("/nonexistent/__no_account_active__"))
    monkeypatch.setattr(spawner.trust, "pretrust_dir", lambda *a, **k: None)
    monkeypatch.setattr(spawner, "_pick_account", lambda force=False, fable=False: None)
    return calls


def _round(tmp_path, **kw):
    fields = dict(mode="report", cwd=str(tmp_path / "wt"), brief_path=tmp_path / "b.md",
                  report_path=tmp_path / "r.md", log_path=tmp_path / "l.log", model="@opus", effort="high")
    fields.update(kw)
    return ChildRound(**fields)


class FakeCodexFamily:
    name = "fake-codex"
    family = "codex"
    runtime = "codex"
    headless = True

    def spawn_command(self, child_round):
        return "true"

    def resume_command(self, child_round):
        return "true"

    def bootstrap(self, mode):
        return ""

    def env(self, mode):
        return {}


PERSONAL = os.path.expanduser("~/worktrees-personal/x")
WORK = os.path.expanduser("~/worktrees/x")
WORKER_HOME = os.path.expanduser("~/projects/work/worker")


def test_codex_gate_refuses_when_roots_are_empty(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="codex is disabled: \\[codex\\] allowed_roots is empty"):
        spawner.codex_gate(WORK)


@pytest.mark.parametrize("value", ['"oops"', "[1]", '["", "~/worktrees"]', "true"])
def test_codex_gate_treats_malformed_roots_as_disabled(monkeypatch, tmp_path, value):
    cfg = tmp_path / "dw.toml"
    cfg.write_text(f"[codex]\nallowed_roots = {value}\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    assert config.codex_allowed_roots() == ()
    with pytest.raises(ValueError, match="codex is disabled"):
        spawner.codex_gate(WORK)


def test_codex_gate_compares_path_components_not_string_prefixes(monkeypatch, tmp_path):
    allow_codex(monkeypatch, tmp_path, "~/worktrees")
    spawner.codex_gate(WORK)
    spawner.codex_gate(os.path.expanduser("~/worktrees"))
    with pytest.raises(ValueError, match="codex is not allowed under"):
        spawner.codex_gate(PERSONAL)
    with pytest.raises(ValueError, match="codex is not allowed under"):
        spawner.codex_gate(os.path.expanduser("~/worktrees-personal"))


def test_codex_gate_resolves_symlinks_on_both_sides(monkeypatch, tmp_path):
    real = tmp_path / "real-root"
    (real / "repo").mkdir(parents=True)
    link = tmp_path / "link-root"
    link.symlink_to(real)
    allow_codex(monkeypatch, tmp_path, str(link))
    spawner.codex_gate(str(real / "repo"))
    spawner.codex_gate(str(link / "repo"))


def test_codex_gate_accepts_worker_home_and_work_worktree(monkeypatch, tmp_path):
    allow_codex(monkeypatch, tmp_path, "~/projects/work", "~/worktrees")
    spawner.codex_gate(WORKER_HOME)
    spawner.codex_gate(WORK)


def test_spawn_worker_tab_codex_runtime_refuses_before_spawning(monkeypatch, tmp_path):
    allow_codex(monkeypatch, tmp_path, "~/worktrees")
    calls = _patch_exec(monkeypatch)
    with pytest.raises(ValueError, match="codex is not allowed under"):
        asyncio.run(spawner.spawn_worker_tab(cwd=PERSONAL, initial_prompt="x", name="alpha", runtime="codex"))
    assert calls == []
    asyncio.run(spawner.spawn_worker_tab(cwd=WORK, initial_prompt="x", name="alpha", runtime="codex"))
    assert len(calls) == 1


def test_spawn_worker_tab_hands_a_codex_workers_role_text_to_the_prompt_file(monkeypatch, tmp_path):
    allow_codex(monkeypatch, tmp_path, "~/worktrees")
    calls = _patch_exec(monkeypatch)
    asyncio.run(spawner.spawn_worker_tab(cwd=WORK, initial_prompt="x", name="alpha", runtime="codex",
                                         role_text="ROLE TEXT"))
    inner = calls[0][-1]
    assert '"$(cat ' in inner and "ROLE TEXT" not in inner
    prompt_file = next(paths.PROMPT_FILES.glob("*.txt"))
    assert prompt_file.read_text() == spawner._codex_worker_prompt("x", "ROLE TEXT")


def test_spawn_worker_tab_gate_precedes_codex_extra_args_validation(monkeypatch, tmp_path):
    _patch_exec(monkeypatch)
    with pytest.raises(ValueError, match="codex is disabled"):
        asyncio.run(spawner.spawn_worker_tab(cwd=WORK, initial_prompt="x", name="alpha", runtime="codex",
                                             extra_args=["--settings", "s.json"]))
    allow_codex(monkeypatch, tmp_path, "~/worktrees")
    with pytest.raises(ValueError, match="Claude-only"):
        asyncio.run(spawner.spawn_worker_tab(cwd=WORK, initial_prompt="x", name="alpha", runtime="codex",
                                             extra_args=["--settings", "s.json"]))


@pytest.mark.parametrize("extra_args", [
    ["-C", PERSONAL], ["--cd=/x"], ["--add-dir", "/"], ["--cd", PERSONAL], ["--", "-c", "x=1"],
])
def test_codex_extra_args_cannot_move_the_working_root_past_the_gate(monkeypatch, tmp_path, extra_args):
    allow_codex(monkeypatch, tmp_path, "~/worktrees")
    calls = _patch_exec(monkeypatch)
    with pytest.raises(ValueError, match="cannot override"):
        asyncio.run(spawner.spawn_worker_tab(cwd=WORK, initial_prompt="x", name="alpha", runtime="codex",
                                             extra_args=extra_args))
    assert calls == []


def test_codex_exec_provider_refuses_a_round_whose_extra_args_move_the_working_root(monkeypatch, tmp_path):
    allow_codex(monkeypatch, tmp_path, "~/worktrees")
    provider = get_provider("codex-exec")
    with pytest.raises(ValueError, match="cannot override"):
        provider.spawn_command(_round(tmp_path, cwd=WORK, extra_args=("-C", "/elsewhere")))
    with pytest.raises(ValueError, match="cannot override"):
        provider.resume_command(_round(tmp_path, cwd=WORK, resume_sid="s1", extra_args=("--add-dir", "/")))
    with pytest.raises(ValueError, match="cannot override"):
        provider.spawn_command(_round(tmp_path, cwd=WORK, extra_args=("--", "-c", 'sandbox_mode="danger-full-access"')))
    assert "-C" in provider.spawn_command(_round(tmp_path, cwd=WORK))


def test_codex_family_provider_that_knows_nothing_of_the_gate_is_refused(monkeypatch, tmp_path):
    allow_codex(monkeypatch, tmp_path, "~/worktrees")
    calls = _patch_exec(monkeypatch)
    register_provider(FakeCodexFamily())
    try:
        with pytest.raises(ValueError, match="codex is not allowed under"):
            asyncio.run(spawner.spawn_worker_tab(cwd=PERSONAL, initial_prompt="", name="alpha",
                                                 provider=get_provider("fake-codex"), child_round=_round(tmp_path, cwd=PERSONAL)))
        assert calls == []
        asyncio.run(spawner.spawn_worker_tab(cwd=WORK, initial_prompt="", name="alpha",
                                             provider=get_provider("fake-codex"), child_round=_round(tmp_path, cwd=WORK)))
        assert len(calls) == 1
    finally:
        unregister_provider("fake-codex")


def test_codex_gate_covers_the_rounds_cwd_when_it_diverges_from_the_window_cwd(monkeypatch, tmp_path):
    allow_codex(monkeypatch, tmp_path, "~/worktrees")
    calls = _patch_exec(monkeypatch)
    with pytest.raises(ValueError, match="codex is not allowed under"):
        asyncio.run(spawner.spawn_worker_tab(cwd=WORK, initial_prompt="", name="alpha",
                                             provider=get_provider("codex-exec"),
                                             child_round=_round(tmp_path, cwd=PERSONAL)))
    assert calls == []
    with pytest.raises(ValueError, match="codex is not allowed under"):
        asyncio.run(spawner.spawn_worker_tab(cwd=PERSONAL, initial_prompt="", name="alpha",
                                             provider=get_provider("codex-exec"),
                                             child_round=_round(tmp_path, cwd=WORK)))
    assert calls == []
    asyncio.run(spawner.spawn_worker_tab(cwd=WORK, initial_prompt="", name="alpha",
                                         provider=get_provider("codex-exec"),
                                         child_round=_round(tmp_path, cwd=WORK)))
    assert len(calls) == 1


def test_claude_family_provider_is_not_gated(monkeypatch, tmp_path):
    calls = _patch_exec(monkeypatch)
    asyncio.run(spawner.spawn_worker_tab(cwd=PERSONAL, initial_prompt="", name="alpha",
                                         provider=get_provider("claude-exec"),
                                         child_round=_round(tmp_path, cwd=PERSONAL, session_id="s")))
    assert len(calls) == 1


def test_spawn_worker_impl_codex_runtime_refuses_before_writing_a_pending_assignment(fresh_orchestrator_dir, monkeypatch):
    calls = _patch_exec(monkeypatch)
    with pytest.raises(ValueError, match="codex is disabled"):
        asyncio.run(spawn_worker_impl(initial_prompt="task", name="cx", cwd=PERSONAL, runtime="codex"))
    assert list(paths.ASSIGNMENTS_PENDING.glob("*")) == []
    assert calls == []


def test_resume_worker_of_a_codex_record_outside_roots_is_refused(fresh_orchestrator_dir, monkeypatch, tmp_path):
    allow_codex(monkeypatch, tmp_path, "~/worktrees")
    monkeypatch.setenv("HOME", str(tmp_path))
    sessions = tmp_path / ".codex" / "sessions" / "2026" / "05" / "26"
    sessions.mkdir(parents=True)
    (sessions / "rollout-2026-05-26T10-55-35-cx-sid.jsonl").write_text(json.dumps({"type": "session_meta"}))
    state.write_json_atomic(paths.CLOSED / "cx-sid.json", {
        "claude_sid": "cx-sid", "name": "cx", "cwd": PERSONAL, "runtime": "codex", "closed_at": 1.0})
    calls = _patch_exec(monkeypatch)
    with pytest.raises(ValueError, match="codex is not allowed under"):
        asyncio.run(resume_worker_impl("cx", _registration_timeout_sec=0.05, _poll_interval=0.01))
    assert calls == []
    assert (paths.CLOSED / "cx-sid.json").exists()
