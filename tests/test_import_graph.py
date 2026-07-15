"""The every-session hook path must never import the FastMCP monolith.

hooks.py runs on EVERY session's SessionStart/UserPromptSubmit/Stop/SessionEnd
on this machine. A routine bad merge to mcp_server.py (2,700 LOC, the
fastest-growing file) must not be able to break fleet registration — hook
stderr is swallowed, so the failure would surface only as routing gaps. The
hook path's registry/distill helpers therefore live in FastMCP-free modules,
and these tests pin that boundary.

Subprocess-isolated: pytest itself imports mcp_server for other tests, so the
sys.modules assertion must run in a child interpreter that only runs hooks.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")

FORBIDDEN_CHECK = """
import sys
offenders = sorted(
    m for m in sys.modules
    if m == "dockwright.mcp_server" or m == "mcp" or m.startswith("mcp.")
)
assert not offenders, f"hook path imported forbidden modules: {offenders}"
print("OK")
"""


def _run_hook_child(tmp_path, agent, body):
    env = {
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": SRC,
        "CLAUDE_AGENT": agent,
    }
    if agent == "worker":
        env["CLAUDE_WORKER_NAME"] = "imp-worker"
    code = textwrap.dedent(body) + textwrap.dedent(FORBIDDEN_CHECK)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert result.returncode == 0, (
        f"hook child failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout


def test_worker_hook_path_never_imports_fastmcp(tmp_path):
    """Drives every worker hook: fresh registration (name resolution), prompt
    submit, stop, and session_end (drop-questions + closed-record archive)."""
    _run_hook_child(tmp_path, "worker", """
        import io, json, sys
        def feed(payload):
            sys.stdin = io.StringIO(json.dumps(payload))
        from dockwright import hooks, paths, state
        feed({"session_id": "imp-w1", "cwd": "/tmp"})
        hooks.session_start()
        record = state.read_json(paths.ACTIVE / "imp-w1.json")
        assert record is not None and record["name"] == "imp-worker", record
        feed({"session_id": "imp-w1"})
        hooks.user_prompt_submit()
        feed({"session_id": "imp-w1"})
        hooks.stop_hook()
        feed({"session_id": "imp-w1"})
        hooks.session_end()
        closed = state.read_json(paths.CLOSED / "imp-w1.json")
        assert closed is not None and closed["name"] == "imp-worker", closed
    """)


def test_mcp_server_reexports_registry_helpers():
    """Internal mcp_server call sites and existing test imports resolve the
    moved helpers through the re-export shim — same objects, not copies."""
    from dockwright import mcp_server, registry
    for name in ["_question_paths", "_drop_questions_for_worker",
                 "_prune_stale_active_records", "_resolve_unique_name"]:
        assert getattr(mcp_server, name) is getattr(registry, name), name


def test_manager_hook_path_never_imports_fastmcp(tmp_path):
    """Drives manager registration and the session_end distill branch: the
    pre-seeded memory file exercises the early-return (idempotence), so no
    real `claude -p` subprocess spawns in this child. The fallback distill is
    a detached subprocess now, not an in-process import — the hook path must
    stay distill-free."""
    _run_hook_child(tmp_path, "manager", """
        import io, json, sys
        from datetime import datetime
        def feed(payload):
            sys.stdin = io.StringIO(json.dumps(payload))
        from dockwright import hooks, paths, state
        feed({"session_id": "imp-m1", "cwd": "/tmp"})
        hooks.session_start()
        record = state.read_json(paths.ACTIVE / "imp-m1.json")
        assert record is not None and record["agent"] == "manager", record
        memory_dir = paths.manager_memory_domain_dir(paths.DEFAULT_DOMAIN)
        memory_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now().strftime("%Y-%m-%d")
        (memory_dir / f"{date_str}-imp-m1.md").write_text("seeded")
        feed({"session_id": "imp-m1"})
        hooks.session_end()
        assert not (paths.ACTIVE / "imp-m1.json").exists()
        assert "dockwright.distill" not in sys.modules, (
            "the SessionEnd hook path must stay distill-free: the fallback "
            "distill is a detached subprocess, not an in-process import"
        )
    """)


def test_mcp_server_reexports_distill_helpers():
    from dockwright import distill, mcp_server
    for name in ["_DISTILL_PROMPT", "_DISTILL_MAX_INPUT_BYTES",
                 "_DISTILL_TIMEOUT_SECONDS", "_extract_tool_result_text",
                 "_slim_transcript", "_distill_manager_session",
                 "_write_memory_file_atomic", "distill_and_write_memory"]:
        assert getattr(mcp_server, name) is getattr(distill, name), name


def test_hooks_source_never_references_the_monolith():
    """Static pin for ALL hook branches, including ones the subprocess legs
    don't drive (/clear rotation, nested detection): a retarget back to
    .mcp_server anywhere in hooks.py would pass the in-process suite (pytest
    imports mcp_server anyway), so the source itself is the contract."""
    hooks_source = (Path(__file__).resolve().parents[1]
                    / "src" / "dockwright" / "hooks.py").read_text()
    offenders = [f"{i}: {line.strip()}"
                 for i, line in enumerate(hooks_source.splitlines(), 1)
                 if "mcp_server" in line]
    assert not offenders, f"hooks.py references the monolith: {offenders}"
