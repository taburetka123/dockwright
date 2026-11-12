from typing import Any

from mcp.server.fastmcp import FastMCP

TEXT_LIMIT = 8 * 1024
DAEMON_SID = "daemon"

RECORD_FIELDS = frozenset({
    ("pipeline_start", "ask"),
    ("pipeline_rework", "ask"),
    ("ask_manager", "question"),
    ("ask_parent", "question"),
    ("ask_engineer", "question"),
    ("answer_question", "text"),
    ("answer_child", "text"),
    ("topic_message", "text"),
})

SERVER_INSTRUCTIONS = (
    "The artifact store takes and returns files, never text: artifact_put takes content_path=<file>; "
    "artifact_get returns content_path, a copy of the body outside the store — read that file. Other text "
    "arguments are limited to 8 KiB, except the texts the engine stores verbatim as work records — an ask, a "
    "question, an answer, a topic message: pass those whole. Put any other longer text in a file and pass a "
    "short text that names it."
)


def _strings(value: Any, where: str):
    if isinstance(value, str):
        yield where, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield f"{where} key", str(key)
            yield from _strings(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{where}[{index}]")


def check_arguments(tool: str, arguments: dict | None, declared: set[str] | None = None) -> None:
    for param, value in (arguments or {}).items():
        if (tool, param) in RECORD_FIELDS or (declared is not None and param not in declared):
            continue
        for where, text in _strings(value, param):
            size = len(text.encode("utf-8"))
            if size > TEXT_LIMIT:
                raise ValueError(f"{tool}: {where} is {size} bytes, over the 8 KiB limit. Put the text in a file "
                                 "and pass a short text that names it.")


def check_session_ids(tool: str, arguments: dict | None) -> None:
    for param, value in (arguments or {}).items():
        if param.endswith("_sid") and value == DAEMON_SID:
            raise ValueError(f"{tool}: {param} {value!r} names the fleet daemon, which calls the engine only from its "
                             "own pass; pass your own session id")


def check_required(tool: str, schema: dict, arguments: dict | None) -> None:
    arguments = arguments or {}
    missing = [param for param in schema.get("required", []) if param not in arguments]
    if not missing:
        return
    properties = schema.get("properties", {})
    described = ", ".join(
        f"{param} ({properties[param]['description']})" if properties.get(param, {}).get("description") else param
        for param in missing)
    raise ValueError(f"{tool} is missing {described}. Your client may be showing an older schema of {tool}: a "
                     "client can keep the schema a conversation first loaded after the server changes. Call "
                     f"{tool} again with {', '.join(missing)}; the server takes it even when your schema does not "
                     "list it.")


class GuardedFastMCP(FastMCP):
    async def call_tool(self, name: str, arguments: dict[str, Any]):
        tool = self._tool_manager.get_tool(name)
        if tool is not None:
            check_required(name, tool.parameters, arguments)
        check_arguments(name, arguments, None if tool is None else set(tool.parameters.get("properties", {})))
        check_session_ids(name, arguments)
        return await super().call_tool(name, arguments)
