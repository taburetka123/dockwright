import asyncio
import os
import shlex
import subprocess
import json
from pathlib import Path

import pytest

from dockwright import config, models, paths, spawner, terminal
from tests.test_model_pin_inventory import _assert_explicit
from dockwright.providers import (
    ChildRound, INTERACTIVE_MODE, REPLY_MODE, REPORT_MODE, get_provider, known_provider_names,
    register_provider, unregister_provider, validate_mode,
)

WORKER_PIN = models.families()["opus"] + "[1m]"
WORKER_DENY = "--disallowedTools=Artifact,SendFeedback,Workflow,AskUserQuestion,ReportFindings,ScheduleWakeup"
WORKER_FLAGS = ["--model", "@opus[1m]", "--effort", "xhigh"]
LAUNCH = f"{shlex.quote(str(paths.dockwright_bin()))} launch"
CLAUDE_PIN = f"cd /tmp/x && CLAUDE_AGENT=worker CLAUDE_WORKER_NAME=alpha CLAUDE_WORKER_RUNTIME=claude {LAUNCH} --model '{WORKER_PIN}' --effort xhigh {WORKER_DENY}"
CLAUDE_RESUME_PIN = f"cd /tmp/x && CLAUDE_AGENT=worker CLAUDE_WORKER_NAME=alpha CLAUDE_WORKER_RUNTIME=claude {LAUNCH} --model '{WORKER_PIN}' --effort xhigh {WORKER_DENY} --resume sid-1"
CODEX_PIN = (
    "cd /tmp/x && CLAUDE_AGENT=worker CLAUDE_WORKER_NAME=alpha CLAUDE_WORKER_RUNTIME=codex codex "
    "--ask-for-approval never --sandbox danger-full-access --dangerously-bypass-hook-trust --model gpt-5.5 "
    "'You are an orchestrator worker running in a separate tmux window. Do not ask the human directly.\n"
    "If you need a human decision, call `ask_manager(claude_sid, question)`. If it returns a `NO_ANSWER_YET:` "
    "sentinel, the question is still pending — call ask_manager again with the same question plus the "
    "resume_question_id named in the sentinel; never proceed without the answer.\n"
    "Use your session id as `claude_sid`; in Codex, run `echo $CODEX_THREAD_ID` if you need to inspect it.\n"
    "When the task is complete, call `worker_done(claude_sid, summary)` as your final action.\n\nTask:\nhello'"
)
CODEX_RESUME_PIN = (
    "cd /tmp/x && CLAUDE_AGENT=worker CLAUDE_WORKER_NAME=alpha CLAUDE_WORKER_RUNTIME=codex codex "
    "--ask-for-approval never --sandbox danger-full-access --dangerously-bypass-hook-trust resume sid-2"
)


class _FakeProc:
    returncode = 0

    async def communicate(self, input=None):
        return (b"%1", b"")


def _patch_exec(monkeypatch):
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _FakeProc()

    monkeypatch.setattr(spawner.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(paths, "ACCOUNT_ACTIVE", Path("/nonexistent/__no_account_active__"))
    monkeypatch.setattr(spawner.trust, "pretrust_dir", lambda *a, **k: None)
    return captured


def _round(tmp_path, mode=REPORT_MODE, model=None, effort=None, **kw):
    fields = dict(mode=mode, cwd="/tmp/x", brief_path=tmp_path / "r1.brief.md",
                  report_path=tmp_path / "r1.report.md", log_path=tmp_path / "r1.log", model=model, effort=effort)
    fields.update(kw)
    return ChildRound(**fields)


def test_no_provider_claude_command_is_byte_identical(monkeypatch):
    captured = _patch_exec(monkeypatch)
    asyncio.run(spawner.spawn_worker_tab(cwd="/tmp/x", initial_prompt="hi", name="alpha", extra_args=WORKER_FLAGS))
    prompt_file = next(paths.PROMPT_FILES.glob("*.txt"))
    assert captured["args"][-1] == f'{CLAUDE_PIN} "$(cat {shlex.quote(str(prompt_file))})"'
    asyncio.run(spawner.spawn_worker_tab(cwd="/tmp/x", initial_prompt="", name="alpha", resume_sid="sid-1",
                                         extra_args=WORKER_FLAGS))
    assert captured["args"][-1] == CLAUDE_RESUME_PIN


def test_no_provider_codex_command_is_byte_identical(monkeypatch, tmp_path):
    from tests.conftest import allow_codex
    allow_codex(monkeypatch, tmp_path)
    captured = _patch_exec(monkeypatch)
    asyncio.run(spawner.spawn_worker_tab(cwd="/tmp/x", initial_prompt="hello", name="alpha",
                                         runtime="codex", extra_args=["--model", "gpt-5.5"]))
    assert captured["args"][-1] == CODEX_PIN
    asyncio.run(spawner.spawn_worker_tab(cwd="/tmp/x", initial_prompt="", name="alpha",
                                         runtime="codex", resume_sid="sid-2"))
    assert captured["args"][-1] == CODEX_RESUME_PIN


def test_claude_tui_provider_builds_the_same_command_as_no_provider(monkeypatch, tmp_path):
    captured = _patch_exec(monkeypatch)
    asyncio.run(spawner.spawn_worker_tab(cwd="/tmp/x", initial_prompt="hi", name="alpha", extra_args=WORKER_FLAGS,
                                         provider=get_provider("claude-tui"), child_round=_round(tmp_path)))
    prompt_file = next(paths.PROMPT_FILES.glob("*.txt"))
    assert captured["args"][-1] == f'{CLAUDE_PIN} "$(cat {shlex.quote(str(prompt_file))})"'
    assert "CLAUDE_WORKER_PROVIDER" not in captured["args"][-1]


def test_registry_knows_three_providers_and_the_legacy_label():
    assert set(known_provider_names()) == {"codex-exec", "claude-exec", "claude-tui", "codex-tui"}
    with pytest.raises(ValueError, match="unknown provider 'nope'"):
        get_provider("nope")
    with pytest.raises(ValueError, match="unknown provider 'codex-tui'"):
        get_provider("codex-tui")


def test_validate_mode():
    validate_mode(REPORT_MODE)
    validate_mode(INTERACTIVE_MODE)
    with pytest.raises(ValueError, match="mode must be one of"):
        validate_mode("review")


def test_codex_exec_report_spawn_command(tmp_path):
    cmd = get_provider("codex-exec").spawn_command(_round(tmp_path))
    tokens = shlex.split(cmd.split("|")[0].split("<")[0])
    assert cmd.startswith("codex exec ")
    assert "set -o pipefail" not in cmd
    assert tokens[:2] == ["codex", "exec"]
    assert "--json" in tokens and "--skip-git-repo-check" in tokens and "--dangerously-bypass-hook-trust" in tokens
    assert tokens[tokens.index("-C") + 1] == "/tmp/x"
    assert 'sandbox_mode="read-only"' in tokens and "mcp_servers.dockwright.enabled=false" in tokens
    assert 'approval_policy="never"' in tokens
    assert 'approval_policy="on-request"' not in cmd
    assert "approvals_reviewer" not in cmd and "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert "-m" not in tokens
    assert tokens[tokens.index("-o") + 1] == f"{tmp_path / 'r1.report.md'}.tmp"
    assert tokens[-1] == "-"
    assert f"- < {shlex.quote(str(tmp_path / 'r1.brief.md'))} 2>&1 | tee {shlex.quote(str(tmp_path / 'r1.log'))}" in cmd
    tmp = shlex.quote(f"{tmp_path / 'r1.report.md'}.tmp")
    assert cmd.endswith(f"; [ -f {tmp} ] && mv {tmp} {shlex.quote(str(tmp_path / 'r1.report.md'))}")


def test_codex_exec_mode_pins_come_after_caller_extra_args(tmp_path):
    cmd = get_provider("codex-exec").spawn_command(
        _round(tmp_path, extra_args=("-c", 'sandbox_mode="danger-full-access"')))
    tokens = shlex.split(cmd.split("|")[0].split("<")[0])
    assert tokens.index('sandbox_mode="danger-full-access"') < tokens.index('sandbox_mode="read-only"')
    assert tokens.index('sandbox_mode="danger-full-access"') < tokens.index('approval_policy="never"')
    assert tokens[tokens.index("-o") + 1] == f"{tmp_path / 'r1.report.md'}.tmp"


def test_codex_exec_interactive_spawn_command_carries_the_three_keys_and_tool_timeout(tmp_path):
    cmd = get_provider("codex-exec").spawn_command(_round(tmp_path, mode=INTERACTIVE_MODE, model="gpt-5.5"))
    assert 'approval_policy="on-request"' in cmd
    assert 'approvals_reviewer="auto_review"' in cmd
    assert 'sandbox_mode="workspace-write"' in cmd
    assert "mcp_servers.dockwright.tool_timeout_sec=300" in cmd
    assert "mcp_servers.dockwright.enabled=false" not in cmd
    assert "-m gpt-5.5" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd


def test_codex_exec_tool_timeout_follows_config(monkeypatch, tmp_path):
    cfg = tmp_path / "dw.toml"
    cfg.write_text('[subworkers.providers.codex-exec]\nask_parent_wait_sec = 1500\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    cmd = get_provider("codex-exec").spawn_command(_round(tmp_path, mode=INTERACTIVE_MODE))
    assert "mcp_servers.dockwright.tool_timeout_sec=1560" in cmd


def test_codex_exec_resume_command_repeats_mode_keys_without_cd(tmp_path):
    p = get_provider("codex-exec")
    report = p.resume_command(_round(tmp_path, resume_sid="sid-9"))
    assert report.startswith("codex exec resume sid-9 ")
    assert "set -o pipefail" not in report
    assert 'sandbox_mode="read-only"' in report and "-C" not in shlex.split(report.split("<")[0])
    interactive = p.resume_command(_round(tmp_path, mode=INTERACTIVE_MODE, resume_sid="sid-9"))
    assert 'approvals_reviewer="auto_review"' in interactive and "--dangerously-bypass-approvals-and-sandbox" not in interactive


CODEX_STUB = """#!/bin/bash
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
printf 'CODEX_HOME=%s CLAUDE_AGENT=%s CLAUDE_WORKER_PROVIDER=%s\\n' \\
  "$CODEX_HOME" "$CLAUDE_AGENT" "$CLAUDE_WORKER_PROVIDER" > "$out"
cat > /dev/null
exit 0
"""


def test_codex_exec_env_prefix_reaches_the_codex_process(monkeypatch, tmp_path):
    from tests.conftest import allow_codex
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "codex"
    stub.write_text(CODEX_STUB)
    stub.chmod(0o755)
    (tmp_path / "r1.brief.md").write_text("do the thing\n")
    allow_codex(monkeypatch, tmp_path, str(tmp_path))
    captured = _patch_exec(monkeypatch)
    asyncio.run(spawner.spawn_worker_tab(cwd=str(tmp_path), initial_prompt="", name="alpha",
                                         provider=get_provider("codex-exec"),
                                         child_round=_round(tmp_path, cwd=str(tmp_path)),
                                         env={"CODEX_HOME": "/scratch-home"}))
    inner = captured["args"][-1]
    child_env = {k: v for k, v in os.environ.items()
                 if k not in ("CODEX_HOME", "CLAUDE_AGENT", "CLAUDE_WORKER_PROVIDER")}
    child_env["PATH"] = f"{stub_dir}:{os.environ['PATH']}"
    subprocess.run(["bash", "-c", inner], timeout=20, env=child_env)
    report = (tmp_path / "r1.report.md").read_text()
    assert "CODEX_HOME=/scratch-home" in report
    assert "CLAUDE_AGENT=worker" in report
    assert "CLAUDE_WORKER_PROVIDER=codex-exec" in report
    assert (tmp_path / "r1.log").exists()
    assert not (tmp_path / "r1.report.md.tmp").exists()


def test_codex_exec_bootstrap_and_env():
    p = get_provider("codex-exec")
    assert "CODEX_THREAD_ID" in p.bootstrap(INTERACTIVE_MODE) and "ask_parent(child_sid, question)" in p.bootstrap(INTERACTIVE_MODE)
    assert "child_done(child_sid, summary)" in p.bootstrap(INTERACTIVE_MODE)
    assert "report" in p.bootstrap(REPORT_MODE) and "ask" in p.bootstrap(REPORT_MODE)
    assert p.env(REPORT_MODE) == {} and p.env(INTERACTIVE_MODE) == {}
    assert (p.name, p.family, p.runtime, p.headless) == ("codex-exec", "codex", "codex", True)


def test_claude_exec_commands(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "PRESETS", Path("/presets"))
    p = get_provider("claude-exec")
    assert (p.name, p.family, p.runtime, p.headless) == ("claude-exec", "claude", "claude", True)
    spawn = p.spawn_command(_round(tmp_path, session_id="11111111-2222-4333-8444-555555555555", model="@opus", effort="high"))
    head = shlex.split(spawn.split("<")[0])
    assert head[:2] == ["claude", "-p"]
    assert head[head.index("--session-id") + 1] == "11111111-2222-4333-8444-555555555555"
    assert head[head.index("--output-format") + 1] == "text"
    assert head[head.index("--model") + 1] == models.families()["opus"]
    assert head[head.index("--effort") + 1] == "high"
    assert head[head.index("--settings") + 1] == "/presets/verifier-settings.json"
    assert "--strict-mcp-config" in head
    tmp = shlex.quote(str(tmp_path / "r1.report.md") + ".tmp")
    assert spawn.endswith(f"< {shlex.quote(str(tmp_path / 'r1.brief.md'))} > {tmp} 2> {shlex.quote(str(tmp_path / 'r1.log'))} && mv {tmp} {shlex.quote(str(tmp_path / 'r1.report.md'))}")
    interactive = p.spawn_command(_round(tmp_path, mode=INTERACTIVE_MODE, model="claude-sonnet-5", effort="high", session_id="s"))
    ihead = shlex.split(interactive.split("<")[0])
    assert ihead[ihead.index("--settings") + 1] == "/presets/worker-headless-settings.json"
    assert "--strict-mcp-config" not in ihead and ihead[ihead.index("--model") + 1] == "claude-sonnet-5"
    resume = p.resume_command(_round(tmp_path, resume_sid="sid-3", model="@opus", effort="high", extra_args=("--mcp-config", "/m.json")))
    rhead = shlex.split(resume.split("<")[0])
    assert rhead[rhead.index("--resume") + 1] == "sid-3" and "--session-id" not in rhead
    assert rhead[rhead.index("--effort") + 1] == "high"
    assert rhead.index("--mcp-config") < rhead.index("--settings")
    caller_settings = p.resume_command(_round(tmp_path, resume_sid="sid-3", model="@opus", extra_args=("--settings", "/x.json")))
    chead = shlex.split(caller_settings.split("<")[0])
    assert chead.index("/x.json") < chead.index("/presets/verifier-settings.json")
    assert chead[chead.index("--strict-mcp-config") - 1] == "/presets/verifier-settings.json"
    with pytest.raises(ValueError, match="cannot end the options"):
        p.spawn_command(_round(tmp_path, model="@opus", extra_args=("--", "--settings", "/x.json")))
    assert p.bootstrap(INTERACTIVE_MODE) == "" and p.bootstrap(REPLY_MODE) == "" and "report" in p.bootstrap(REPORT_MODE)
    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "reply-settings.json").write_text(
        '{"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "/opt/dw reply-guard"}]}]}}')
    monkeypatch.setattr(paths, "PRESETS", presets)
    reply = p.spawn_command(_round(tmp_path, mode=REPLY_MODE, session_id="s", model="@opus"))
    yhead = shlex.split(reply.split("<")[0])
    assert yhead[yhead.index("--settings") + 1] == str(presets / "reply-settings.json")
    assert "--strict-mcp-config" not in yhead


def _child_role(tmp_path, monkeypatch, profile="back"):
    from dockwright import config
    roles_dir = tmp_path / "roles"
    role = roles_dir / "CodeReviewer"
    role.mkdir(parents=True)
    (role / "role.toml").write_text('core = "CodeReviewer"\nlaunch = ["child"]\n')
    (role / "role.child.md").write_text("<!-- dockwright-role: CodeReviewer launch=child -->\n")
    (role / f"role.child.{profile}.md").write_text(
        f"<!-- dockwright-role: CodeReviewer launch=child -->\n<!-- dockwright-profile: {profile} -->\n")
    (role / "role.codex.md").write_text("<!-- dockwright-role: CodeReviewer launch=codex -->\n")
    (role / f"role.codex.{profile}.md").write_text(
        f"<!-- dockwright-role: CodeReviewer launch=codex -->\n<!-- dockwright-profile: {profile} -->\n")
    (role / "role.settings.json").write_text('{"skillOverrides": {"skill-x": "name-only"}}')
    (roles_dir / "_agents").mkdir()
    (roles_dir / "_agents" / f"{profile}.json").write_text("{}")
    monkeypatch.setattr(paths, "ROLES_DIR", roles_dir)
    monkeypatch.setattr(paths, "ROLE_SETTINGS", tmp_path / "role-settings")
    work = tmp_path / "work" / "acme-api"
    work.mkdir(parents=True)
    monkeypatch.setattr(config, "profiles", lambda: {
        "sets": {"none": [], profile: ["acme"]}, "repos": {"acme-api": profile},
        "work_roots": [str(tmp_path / "work")], "work_default": "none"})
    return role, work


def test_claude_exec_carries_its_roles_render_settings_and_agents(monkeypatch, tmp_path):
    role, work = _child_role(tmp_path, monkeypatch)
    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "verifier-settings.json").write_text('{"permissions": {"allow": ["Bash(ls:*)"]}}')
    (presets / "worker-headless-settings.json").write_text('{"permissions": {"allow": ["Bash(ls:*)"]}}')
    monkeypatch.setattr(paths, "PRESETS", presets)
    p = get_provider("claude-exec")
    spawn = p.spawn_command(_round(tmp_path, cwd=str(work), session_id="s", context_role="CodeReviewer", model="claude-opus-5", effort="high"))
    head = shlex.split(spawn.split("<")[0])
    assert head[head.index("--append-system-prompt-file") + 1] == str(role / "role.codex.back.md")
    reply = p.spawn_command(_round(tmp_path, mode=INTERACTIVE_MODE, cwd=str(work), session_id="s", context_role="CodeReviewer", model="claude-opus-5", effort="high"))
    reply_head = shlex.split(reply.split("<")[0])
    assert reply_head[reply_head.index("--append-system-prompt-file") + 1] == str(role / "role.child.back.md")
    assert head.count("--settings") == 1
    merged = json.loads(Path(head[head.index("--settings") + 1]).read_text())
    assert merged == {"permissions": {"allow": ["Bash(ls:*)"]}, "skillOverrides": {"skill-x": "name-only"}}
    assert head[head.index("--agents") + 1] == str(tmp_path / "roles" / "_agents" / "back.json")
    assert "--agents-file" not in head
    resume = p.resume_command(_round(tmp_path, cwd=str(work), resume_sid="s", context_role="CodeReviewer", model="claude-opus-5", effort="high"))
    rhead = shlex.split(resume.split("<")[0])
    assert rhead[rhead.index("--append-system-prompt-file") + 1] == str(role / "role.codex.back.md")


def test_claude_exec_without_context_loads_generic_workers_render(monkeypatch, tmp_path):
    role, work = _child_role(tmp_path, monkeypatch)
    generic = role.parent / "GenericWorker"
    generic.mkdir()
    (generic / "role.toml").write_text('core = "GenericWorker"\nlaunch = ["child"]\n')
    (generic / "role.child.md").write_text("<!-- dockwright-role: GenericWorker launch=child -->\n")
    (generic / "role.codex.md").write_text("<!-- dockwright-role: GenericWorker launch=codex -->\n")
    (generic / "role.settings.json").write_text("{}")
    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "verifier-settings.json").write_text('{"permissions": {"allow": ["Bash(ls:*)"]}}')
    (presets / "worker-headless-settings.json").write_text('{"permissions": {"allow": ["Bash(ls:*)"]}}')
    monkeypatch.setattr(paths, "PRESETS", presets)
    p = get_provider("claude-exec")
    report = shlex.split(p.spawn_command(_round(tmp_path, session_id="s", model="claude-opus-5", effort="high")).split("<")[0])
    assert report[report.index("--append-system-prompt-file") + 1] == str(generic / "role.codex.md")
    interactive = shlex.split(p.spawn_command(_round(tmp_path, mode=INTERACTIVE_MODE, session_id="s", model="claude-opus-5", effort="high")).split("<")[0])
    assert interactive[interactive.index("--append-system-prompt-file") + 1] == str(generic / "role.child.md")


def test_claude_exec_without_role_manifests_keeps_the_preset_alone(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "PRESETS", Path("/presets"))
    monkeypatch.setattr(paths, "ROLES_DIR", tmp_path / "no-roles")
    head = shlex.split(get_provider("claude-exec").spawn_command(_round(tmp_path, session_id="s", model="claude-opus-5", effort="high")).split("<")[0])
    assert "--append-system-prompt-file" not in head and "--agents" not in head
    assert head[head.index("--settings") + 1] == "/presets/verifier-settings.json"


def test_headless_provider_command_gets_provider_key_and_remain_on_exit(monkeypatch, tmp_path):
    captured = _patch_exec(monkeypatch)
    calls = []
    monkeypatch.setattr(terminal.TmuxDriver, "set_window_option",
                        lambda self, pane, option, value: calls.append((pane, option, value)))
    asyncio.run(spawner.spawn_worker_tab(cwd="/tmp/x", initial_prompt="", name="alpha",
                                         provider=get_provider("claude-exec"),
                                         child_round=_round(tmp_path, session_id="s1", model="@opus", effort="high"),
                                         env={"CLAUDE_WORKER_PROVIDER": "evil", "MY": "1"}))
    inner = captured["args"][-1]
    assert "CLAUDE_WORKER_RUNTIME=claude CLAUDE_WORKER_PROVIDER=claude-exec " in inner
    assert "CLAUDE_WORKER_PROVIDER=evil" not in inner and "MY=1" in inner
    assert inner.startswith("cd /tmp/x && ")
    assert calls == [("%1", "remain-on-exit", "on")]


def test_headless_provider_without_round_is_refused(monkeypatch, tmp_path):
    _patch_exec(monkeypatch)
    with pytest.raises(ValueError, match="needs a child_round"):
        asyncio.run(spawner.spawn_worker_tab(cwd="/tmp/x", initial_prompt="", name="alpha",
                                             provider=get_provider("claude-exec")))


def test_codex_family_skips_account_and_pretrust_and_clears_orch_account(monkeypatch, tmp_path):
    from tests.conftest import allow_codex
    allow_codex(monkeypatch, tmp_path)
    captured = _patch_exec(monkeypatch)
    picks = []
    monkeypatch.setattr(spawner, "_pick_account", lambda force=False, fable=False: picks.append(force) or "a")
    trusts = []
    monkeypatch.setattr(spawner.trust, "pretrust_dir", lambda *a, **k: trusts.append(a))
    asyncio.run(spawner.spawn_worker_tab(cwd="/tmp/x", initial_prompt="", name="alpha",
                                         provider=get_provider("codex-exec"), child_round=_round(tmp_path)))
    inner = captured["args"][-1]
    assert picks == [] and trusts == []
    assert "CLAUDE_ORCH_ACCOUNT='' " in inner and "CLAUDE_CONFIG_DIR" not in inner
    assert "CLAUDE_WORKER_RUNTIME=codex CLAUDE_WORKER_PROVIDER=codex-exec " in inner


def test_register_and_unregister_provider():
    class Custom:
        name = "custom-exec"
        family = "claude"
        runtime = "claude"
        headless = True

        def spawn_command(self, child_round):
            return "true"

        def resume_command(self, child_round):
            return "true"

        def bootstrap(self, mode):
            return ""

        def env(self, mode):
            return {}

    register_provider(Custom())
    try:
        assert get_provider("custom-exec").name == "custom-exec"
        assert "custom-exec" in known_provider_names()
    finally:
        unregister_provider("custom-exec")
    with pytest.raises(ValueError):
        get_provider("custom-exec")


import os
import subprocess
import sys
import time

from dockwright import mcp_server, state
from dockwright.mcp_server import (
    answer_question_impl, register_self_impl, send_to_child_impl, spawn_worker_impl, wait_for_child_impl,
)

FAKE_SCRIPT = Path(__file__).with_name("fake_child_provider.py")


class FakeExec:
    name = "fake-exec"
    family = "claude"
    runtime = "claude"
    headless = True

    def _cmd(self, child_round, resume):
        parts = [shlex.quote(sys.executable), shlex.quote(str(FAKE_SCRIPT)),
                 "--brief", shlex.quote(str(child_round.brief_path)),
                 "--report", shlex.quote(str(child_round.report_path))]
        if child_round.mode == INTERACTIVE_MODE:
            parts.append("--ask")
        if resume:
            parts.extend(["--resume", shlex.quote(child_round.resume_sid)])
        return " ".join(parts) + f" > {shlex.quote(str(child_round.log_path))} 2>&1"

    def spawn_command(self, child_round):
        return self._cmd(child_round, resume=False)

    def resume_command(self, child_round):
        return self._cmd(child_round, resume=True)

    def bootstrap(self, mode):
        return "FAKE-BOOTSTRAP"

    def env(self, mode):
        return {}


class _PopenDriver:
    def __init__(self):
        self.procs = []

    async def spawn(self, *, cwd, title, argv, route_to_workers_window=False,
                    route_to_manager_session=False, target_window_match=None):
        self.procs.append(subprocess.Popen(["bash", "-c", argv[-1]], cwd=cwd))
        return f"%fake{len(self.procs)}"

    def set_window_option(self, pane, option, value):
        pass

    def close(self, pane):
        pass

    async def pane_exists(self, pane):
        return True

    async def find_group_pane(self):
        return None

    def ls(self):
        return []

    def current_pane_id(self):
        return None


@pytest.fixture
def seam_dir(tmp_path, monkeypatch):
    root = tmp_path / "state"
    for attr, sub in (("ROOT", ""), ("ACTIVE", "active"), ("QUESTIONS", "questions"), ("ANSWERS", "answers"),
                      ("DONE", "done"), ("CLOSED", "closed"), ("HANDOFFS", "handoffs"), ("PRESETS", "presets"),
                      ("TURN_ENDS", "turn-ends"), ("MANAGER_MEMORY", "manager-memory"), ("SLOTS", "slots"),
                      ("ARTIFACTS", "artifacts"), ("ASSIGNMENTS", "assignments")):
        monkeypatch.setattr(paths, attr, root / sub if sub else root)
    monkeypatch.setattr(paths, "ASSIGNMENTS_PENDING", root / "assignments" / ".pending")
    monkeypatch.setattr(paths, "MANAGER_TRIGGERS_LOG", root / "manager-triggers.jsonl")
    monkeypatch.setattr(paths, "SPEND_LEDGER", root / "spend-ledger.jsonl")
    monkeypatch.delenv("CLAUDE_PARENT_SID", raising=False)
    monkeypatch.delenv("CLAUDE_PARENT_MANAGER", raising=False)
    paths.ensure_dirs()
    cfg = tmp_path / "seam.toml"
    cfg.write_text(f'[paths]\nstate_root = "{root}"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    driver = _PopenDriver()
    monkeypatch.setattr(spawner, "get_driver", lambda: driver)
    monkeypatch.setattr(spawner.trust, "pretrust_dir", lambda *a, **k: None)
    monkeypatch.setattr(spawner, "_pick_account", lambda force=False, fable=False: None)
    monkeypatch.setattr(spawner, "usage_spawn_gate", lambda force=False: {"status": "ok"})
    monkeypatch.setattr(mcp_server, "_has_live_transcript", lambda sid, runtime=None: True)
    register_provider(FakeExec())
    yield root, cfg, driver
    unregister_provider("fake-exec")
    for proc in driver.procs:
        if proc.poll() is None:
            proc.kill()


def test_fake_provider_round_trip_spawn_question_answer_report_resume(seam_dir, tmp_path):
    root, cfg, driver = seam_dir
    register_self_impl(claude_sid="mgr-a", agent="manager", name="boss", cwd=str(tmp_path), iterm_sid="", pid=os.getpid())
    register_self_impl(claude_sid="w1", agent="worker", name="alpha", cwd=str(tmp_path), iterm_sid="", pid=os.getpid(),
                       parent_manager_name="boss", parent_sid="mgr-a")
    result = asyncio.run(spawn_worker_impl(
        "do the thing", manager_sid="w1", _tool="spawn_child", provider="fake-exec", mode="interactive", model="fake-1",
        effort="high",
        cwd=str(tmp_path), env={"DOCKWRIGHT_CONFIG": str(cfg)},
        _registration_timeout_sec=20.0, _poll_interval=0.1))
    assert result["status"] == "registered", result
    name, sid = result["name"], result["claude_sid"]
    record = state.read_json(paths.ACTIVE / f"{sid}.json")
    assert (record["kind"], record["provider"], record["parent_name"]) == ("subworker", "fake-exec", "alpha")
    assignment = state.read_json(paths.assignment_path(sid))
    assert assignment["provider"] == "fake-exec" and assignment["round"] == 1
    assert assignment["spawn_env"] == {"DOCKWRIGHT_CONFIG": str(cfg)}
    first = asyncio.run(wait_for_child_impl(name, timeout_sec=30, _poll_interval=0.1, manager_name="alpha"))
    assert first["found"] == "question" and first["question"] == "fake ping"
    assert answer_question_impl(first["question_id"], "pong") == {"ok": True}
    done = asyncio.run(wait_for_child_impl(name, timeout_sec=30, _poll_interval=0.1, manager_name="alpha"))
    assert done["found"] == "done" and done["round"] == 1
    assert f"REPORT sid={sid}" in done["summary"] and "answer=pong" in done["summary"]
    assert "brief=do the thing" in done["summary"]
    driver.procs[0].wait(timeout=20)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (paths.CLOSED / f"{sid}.json").exists():
        time.sleep(0.1)
    assert (paths.CLOSED / f"{sid}.json").exists()
    second = asyncio.run(send_to_child_impl(name, "round two please"))
    assert second["ok"] is True and second["round"] == 2 and second["sid"] == sid
    asked_again = asyncio.run(wait_for_child_impl(name, timeout_sec=30, _poll_interval=0.1, manager_name="alpha"))
    assert asked_again["found"] == "question" and asked_again["question"] == "fake ping"
    assert answer_question_impl(asked_again["question_id"], "pong-2") == {"ok": True}
    done2 = asyncio.run(wait_for_child_impl(name, timeout_sec=30, _poll_interval=0.1, manager_name="alpha"))
    assert done2["found"] == "done" and done2["round"] == 2
    assert "brief=round two please" in done2["summary"] and f"REPORT sid={sid}" in done2["summary"]
    assert "answer=pong-2" in done2["summary"]
    assert done2["report_path"].endswith("r2.report.md")
    assert not mcp_server._RESUMES_IN_FLIGHT


def test_headless_provider_for_family():
    from dockwright.providers import headless_provider_for_family, register_provider, unregister_provider
    assert headless_provider_for_family("codex").name == "codex-exec"
    assert headless_provider_for_family("claude").name == "claude-exec"
    with pytest.raises(ValueError, match="no headless provider registered for family 'nope'"):
        headless_provider_for_family("nope")

    class Twin:
        name, family, runtime, headless = "codex-twin", "codex", "codex", True

    register_provider(Twin())
    try:
        with pytest.raises(ValueError, match="family 'codex' has 2 headless providers"):
            headless_provider_for_family("codex")
    finally:
        unregister_provider("codex-twin")
    assert headless_provider_for_family("codex").name == "codex-exec"


def test_codex_exec_pins_service_tier_default_after_caller_extra_args(tmp_path):
    p = get_provider("codex-exec")
    spawn = p.spawn_command(_round(tmp_path, extra_args=("-c", 'service_tier="fast"')))
    tokens = shlex.split(spawn.split("|")[0].split("<")[0])
    assert tokens[tokens.index('service_tier="default"') - 1] == "-c"
    assert tokens.index('service_tier="fast"') < tokens.index('service_tier="default"')
    for cmd in (p.spawn_command(_round(tmp_path, mode=INTERACTIVE_MODE)),
                p.resume_command(_round(tmp_path, resume_sid="sid-9")),
                p.resume_command(_round(tmp_path, mode=INTERACTIVE_MODE, resume_sid="sid-9"))):
        cmd_tokens = shlex.split(cmd.split("|")[0].split("<")[0])
        assert cmd_tokens[cmd_tokens.index('service_tier="default"') - 1] == "-c"


def test_claude_exec_refuses_every_spelling_the_cli_would_resolve(monkeypatch, tmp_path):
    """The CLI trims and lower-cases before resolving, and `fable` is a tier alias, so a
    denylist over the raw string is not a guard. Anything that is not a concrete
    claude-<family>-<version> id is refused, whatever it would have resolved to."""
    monkeypatch.setattr(paths, "PRESETS", Path("/presets"))
    p = get_provider("claude-exec")
    refused = ("claude-fable-5-1", "claude-fable-5-1[1m]", "CLAUDE-FABLE-5-1",
               "  claude-fable-5-1  ", "fable", "FABLE", " fable", "fable-5-1",
               "best", "opus", "sonnet", "haiku", "latest", "   ")
    for model in refused:
        with pytest.raises(ValueError, match="claude-exec refuses --model"):
            p.spawn_command(_round(tmp_path, model=model, session_id="s"))
        with pytest.raises(ValueError, match="claude-exec refuses --model"):
            p.resume_command(_round(tmp_path, model=model, resume_sid="sid-5"))


def test_claude_exec_refusal_reaches_the_provider_config(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "PRESETS", Path("/presets"))
    cfg = tmp_path / "dw.toml"
    cfg.write_text('[subworkers.providers.claude-exec]\nmodel = "claude-fable-5-1"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    rnd = _round(tmp_path, model=config.provider_model("claude-exec"), session_id="s")
    with pytest.raises(ValueError, match="claude-exec refuses --model"):
        get_provider("claude-exec").spawn_command(rnd)


def test_claude_exec_admits_every_non_fable_model(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "PRESETS", Path("/presets"))
    p = get_provider("claude-exec")
    for model in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001",
                  "claude-opus-5[1m]", "claude-sonnet-4-6", "claude-fabled-9-1"):
        head = shlex.split(p.spawn_command(_round(tmp_path, model=model, session_id="s")).split("<")[0])
        assert head[head.index("--model") + 1] == model


def test_claude_exec_without_a_model_is_refused(tmp_path):
    with pytest.raises(ValueError, match="claude-exec needs a model"):
        get_provider("claude-exec").spawn_command(_round(tmp_path, session_id="s", model=None))


def test_claude_exec_resolves_a_token_before_the_shape_guard(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "PRESETS", Path("/presets"))
    head = shlex.split(get_provider("claude-exec").spawn_command(
        _round(tmp_path, model="@opus[1m]", session_id="s")).split("<")[0])
    assert not any("@" in a for a in head)
    assert head[head.index("--model") + 1] == models.families()["opus"] + "[1m]"


def test_claude_exec_still_refuses_fable_through_a_token(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "PRESETS", Path("/presets"))
    with pytest.raises(ValueError) as err:
        get_provider("claude-exec").spawn_command(_round(tmp_path, model="@fable", session_id="s"))
    assert models.families()["fable"] in str(err.value), (
        "the message must name the RESOLVED id, so this test fails if @fable stops resolving")


def test_every_registered_provider_resolves_or_refuses_a_token(tmp_path):
    from dockwright import providers
    (tmp_path / "r1.brief.md").write_text("brief")
    checked = []
    for name in providers.known_provider_names():
        if name in providers.LEGACY_TUI_LABELS:
            continue
        provider = providers.get_provider(name)
        rnd = _round(tmp_path, model="@opus", effort="high", extra_args=("--model", "@opus", "--effort", "high"))
        try:
            cmd = provider.spawn_command(rnd)
        except ValueError as err:
            assert "@opus" in str(err), (
                f"{name} refused the token but not for the token's sake: {err}")
            checked.append(name)
            continue
        assert "@" not in cmd, f"{name} passed a family token through to its command: {cmd}"
        checked.append(name)
    assert set(checked) == {n for n in providers.known_provider_names()
                            if n not in providers.LEGACY_TUI_LABELS}, \
        "every registered provider must be judged, never skipped"


def test_claude_exec_denies_the_six_tools_in_every_mode(monkeypatch, tmp_path):
    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "reply-settings.json").write_text(
        '{"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "/opt/dw reply-guard"}]}]}}')
    monkeypatch.setattr(paths, "PRESETS", presets)
    p = get_provider("claude-exec")
    for mode in (REPORT_MODE, REPLY_MODE, INTERACTIVE_MODE):
        head = shlex.split(p.spawn_command(_round(tmp_path, mode=mode, session_id="s", model="@opus")).split("<")[0])
        assert head.count(WORKER_DENY) == 1
        assert head.index("--settings") < head.index(WORKER_DENY)
    resume = shlex.split(p.resume_command(_round(tmp_path, resume_sid="sid-3", model="@opus")).split("<")[0])
    assert resume.count(WORKER_DENY) == 1
