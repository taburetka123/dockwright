"""Headless claude spawns must disallow file-writing tools.

The selffix retro, gardener headless digest, and manager-memory distill lanes
all feed session-transcript content (untrusted: Jira text, PR comments,
fetched pages) into `claude -p`. Their only legitimate output is markdown on
stdout, captured by the caller (selffix-run.sh > $OUT, gardener-run.sh >
$DIGEST, distill.py subprocess capture) — so Write/Edit/NotebookEdit are
reachable-but-unused surface and must stay hard-disallowed. Repo copies under
deploy/scripts/ are the source of truth; setup.sh deploys them.

The distill lane has since moved PAST this denylist to a default-deny surface
(no built-ins, no MCP servers) after the 2026-07-29 incident in which the
denylist's admitted `mcp__dockwright__*` tools killed a live manager. Its guard
lives in test_distill_injection_lockdown.py; what remains here is the
regression guard that it never reverts to the denylist as its primary defence.

⚠️ The two shell lanes still carry ONLY the denylist, and these tests assert
its presence — not its sufficiency. Both feed untrusted transcript content to
`claude -p` with no `--strict-mcp-config`, so both can still reach Bash and
every globally-configured MCP server, exactly as distill.py could before the
lockdown. selffix is reachable on the same trigger as the incident's distill
(selffix-trigger.sh makes `agent:manager` an unconditional HIGH). Treat a green
run here as "the denylist is still wired", never as "these lanes are contained".
"""
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "deploy" / "scripts"
LOCKDOWN_FLAG = '--disallowedTools "Write,Edit,NotebookEdit"'


def _code_lines(text: str) -> list[str]:
    # The run scripts also document the flag in comments (selffix-run.sh's
    # header contract), so a raw substring match over the whole text stays
    # green even when the actual invocation drops the flag. Match only
    # non-comment lines so the guard binds to executed code, not prose.
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def test_selffix_retro_disallows_file_writing_tools():
    src = (SCRIPTS / "selffix-run.sh").read_text()
    assert any(LOCKDOWN_FLAG in line for line in _code_lines(src))


def test_gardener_headless_path_disallows_file_writing_tools():
    # Anchored to the headless block: the visible tmux lane intentionally
    # scopes tools via the gardener-analyst settings preset instead.
    src = (SCRIPTS / "gardener-run.sh").read_text()
    headless_block = src.split('if [ "$MODE" = "headless" ]')[1].split("\nfi\n")[0]
    assert any(LOCKDOWN_FLAG in line for line in _code_lines(headless_block))


def test_distill_does_not_fall_back_to_the_denylist(tmp_path, monkeypatch):
    """Distill must stay default-deny; a denylist here would fail open again.

    `--disallowedTools` enumerates what is forbidden and admits everything
    unnamed — including the `mcp__dockwright__*` fleet-mutating tools that
    killed manager `mighty-demon` on 2026-07-29. Reintroducing it for the
    distill child, even alongside the allowlist, signals the wrong model of
    the guard to the next reader.
    """
    from dockwright import distill

    log = tmp_path / "transcript.jsonl"
    log.write_text(
        '{"type": "user", "message": {"content": "go"}}\n'
        '{"type": "assistant", "message": {"content": '
        '[{"type": "text", "text": "ok"}]}}\n'
    )
    monkeypatch.setattr("dockwright.distill.find_session_log", lambda sid: log)

    captured = {}

    class _FakeCompleted:
        returncode = 0
        stdout = b"## Decisions\nok\n"
        stderr = b""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompleted()

    monkeypatch.setattr("dockwright.distill.subprocess.run", fake_run)
    assert distill._distill_manager_session("sid-lockdown") is not None

    assert "--disallowedTools" not in captured["cmd"]
