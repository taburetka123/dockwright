import asyncio

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from dockwright import mcp_server, paths
from dockwright.payload_guard import GuardedFastMCP, SERVER_INSTRUCTIONS, TEXT_LIMIT, check_arguments


def _call(server, tool, arguments):
    async def scenario():
        async with create_connected_server_and_client_session(server) as client:
            return await client.call_tool(tool, arguments)
    return asyncio.run(scenario())


def _fresh_server():
    server = GuardedFastMCP("t")

    @server.tool()
    def echo(text: str = "", items: list[str] | None = None) -> dict:
        return {"ok": True}

    return server


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for name in ("ROOT", "ACTIVE", "QUESTIONS", "ANSWERS", "DONE", "ARTIFACTS", "ASSIGNMENTS"):
        monkeypatch.setattr(paths, name, tmp_path / name.lower())
    monkeypatch.setattr(paths, "ASSIGNMENTS_PENDING", tmp_path / "assignments" / ".pending")
    return tmp_path


def test_a_tool_registered_on_a_guarded_server_refuses_a_text_over_the_limit():
    result = _call(_fresh_server(), "echo", {"text": "x" * (TEXT_LIMIT + 1)})
    assert result.isError
    assert "echo: text is 8193 bytes, over the 8 KiB limit" in result.content[0].text
    assert "Put the text in a file" in result.content[0].text


def test_the_limit_itself_passes():
    assert not _call(_fresh_server(), "echo", {"text": "x" * TEXT_LIMIT}).isError


def test_the_limit_counts_utf8_bytes():
    result = _call(_fresh_server(), "echo", {"text": "é" * (TEXT_LIMIT // 2 + 1)})
    assert result.isError and "8194 bytes" in result.content[0].text


def test_a_long_string_inside_a_list_is_refused():
    result = _call(_fresh_server(), "echo", {"items": ["a", "x" * (TEXT_LIMIT + 1)]})
    assert result.isError and "items[1] is 8193 bytes" in result.content[0].text


def test_a_long_key_inside_a_dict_is_refused():
    with pytest.raises(ValueError, match="spawn_worker: env key is 8193 bytes"):
        check_arguments("spawn_worker", {"env": {"k" * (TEXT_LIMIT + 1): "v"}})


def test_the_dockwright_server_refuses_a_long_summary(isolated):
    result = _call(mcp_server.mcp, "worker_done", {"claude_sid": "w1", "summary": "s" * 9000})
    assert result.isError and "worker_done: summary is 9000 bytes" in result.content[0].text


def test_a_record_field_passes_the_guard_and_reaches_its_tool(isolated):
    result = _call(mcp_server.mcp, "answer_question", {"question_id": "nope", "text": "x" * 12 * 1024})
    assert result.isError
    assert "over the 8 KiB limit" not in result.content[0].text


def test_a_topic_message_keeps_its_own_16_kib_cap(isolated):
    result = _call(mcp_server.mcp, "topic_message", {"task_key": "K", "text": "x" * 17 * 1024})
    assert result.isError and "16384" in result.content[0].text
    assert "over the 8 KiB limit" not in result.content[0].text


def test_artifact_put_without_content_path_names_the_field_and_the_older_schema(isolated):
    result = _call(mcp_server.mcp, "artifact_put", {"task_key": "K", "phase": "spec", "name": "a", "content": "x",
                                                    "status": "complete", "writer_sid": "w"})
    assert result.isError
    text = result.content[0].text
    assert ("artifact_put is missing content_path (An absolute path to a UTF-8 text file; the store copies its "
            "text as the body.). Your client may be showing an older schema of artifact_put") in text
    assert "Call artifact_put again with content_path; the server takes it even when your schema does not list it." in text
    assert "input_value" not in text
    assert not paths.artifact_path("K", "spec", "a").exists()


def test_artifact_put_stores_the_file_when_a_long_stale_content_rides_beside_content_path(isolated):
    body = isolated / "body.md"
    body.write_bytes("título\n\ncuerpo — 1\n".encode("utf-8"))
    result = _call(mcp_server.mcp, "artifact_put", {"task_key": "K", "phase": "spec", "name": "a",
                                                    "content": "x" * 9000, "content_path": str(body),
                                                    "status": "complete", "writer_sid": "w"})
    assert not result.isError
    assert mcp_server.artifact_get_impl("K", "spec", "a")["content"] == "título\n\ncuerpo — 1\n"


def test_a_tool_the_guard_does_not_know_names_its_missing_argument():
    server = GuardedFastMCP("t")

    @server.tool()
    def probe(beta_path: str) -> dict:
        return {"ok": True}

    result = _call(server, "probe", {"alpha": "x"})
    assert result.isError
    assert ("probe is missing beta_path. Your client may be showing an older schema of probe: a client can keep "
            "the schema a conversation first loaded after the server changes. Call probe again with beta_path; "
            "the server takes it even when your schema does not list it.") in result.content[0].text


def test_the_server_states_the_rule_to_every_client():
    assert mcp_server.mcp.instructions == SERVER_INSTRUCTIONS
    assert "content_path" in SERVER_INSTRUCTIONS and "8 KiB" in SERVER_INSTRUCTIONS


@pytest.mark.parametrize("tool,arguments", [
    ("pipeline_close", {"manager_sid": "daemon", "task_key": "K", "reason": "done"}),
    ("pipeline_rework", {"manager_sid": "daemon", "task_key": "K", "ask": "redo"}),
    ("pipeline_advance", {"claude_sid": "daemon", "task_key": "K", "to": "task"}),
    ("pipeline_event", {"task_key": "K", "type": "publish", "actor_sid": "daemon"}),
    ("artifact_put", {"task_key": "K", "phase": "x", "name": "y", "content_path": "/nope", "status": "complete",
                      "writer_sid": "daemon"}),
    ("spawn_worker", {"initial_prompt": "x", "manager_sid": "daemon"}),
])
def test_every_tool_refuses_the_literal_daemon_as_a_session_id(isolated, tool, arguments):
    result = _call(mcp_server.mcp, tool, arguments)
    assert result.isError
    text = result.content[0].text
    assert "names the fleet daemon" in text and "only from its own pass" in text
