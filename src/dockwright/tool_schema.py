from __future__ import annotations

import sys

from . import config, pipeline, roles


def schema_nodes() -> list[str]:
    names: list[str] = []
    for path in sorted(config.pipeline_schemas_dir().glob("*.toml")):
        for node in pipeline.load_schema(path).nodes:
            if node not in names:
                names.append(node)
    return names


def _string_branch(prop: dict) -> dict:
    for member in prop.get("anyOf", ()):
        if member.get("type") == "string":
            return member
    return prop


def _offer(server, tool: str, param: str, values: list[str]) -> None:
    prop = server._tool_manager.get_tool(tool).parameters["properties"][param]
    _string_branch(prop)["enum"] = list(values)


def advertise(server) -> None:
    try:
        nodes = schema_nodes()
        if nodes:
            _offer(server, "pipeline_advance", "to", [*nodes, "?"])
            _offer(server, "pipeline_rework", "to", nodes)
    except Exception as err:
        print(f"dockwright: no node enum on pipeline_advance/pipeline_rework: {type(err).__name__}: {err}",
              file=sys.stderr)
    try:
        contexts = roles.role_names()
        if contexts:
            _offer(server, "spawn_worker", "role", contexts)
            _offer(server, "spawn_child", "context", contexts)
    except Exception as err:
        print(f"dockwright: no role enum on spawn_worker/spawn_child: {type(err).__name__}: {err}", file=sys.stderr)
