import asyncio
import copy
import inspect

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from dockwright import config, mcp_server, roles, tool_schema

DESCRIPTION_LIMIT = 2048
ENUM_PARAMS = (("pipeline_advance", "to"), ("pipeline_rework", "to"), ("spawn_child", "context"),
               ("spawn_worker", "role"))

ALPHA = '''
schema = "alpha"
version = 1

[nodes.design]
role = "SCOUT"
model = "@sonnet"
effort = "xhigh"
brief = "briefs/design.md"
inputs = []
outputs = ["design.hld"]

[nodes.plan]
role = "SCOUT"
model = "@sonnet"
effort = "xhigh"
brief = "briefs/plan.md"
inputs = ["design.hld"]
outputs = ["plan.tasks"]

[[edges]]
from = "design"
to = "plan"
'''

BETA = '''
schema = "beta"
version = 1

[nodes.plan]
role = "SCOUT"
model = "@sonnet"
effort = "xhigh"
brief = "briefs/plan.md"
inputs = []
outputs = ["plan.tasks"]

[nodes.task]
role = "SCOUT"
model = "@sonnet"
effort = "xhigh"
brief = "briefs/task.md"
inputs = ["plan.tasks"]
outputs = ["review.task"]

[[edges]]
from = "plan"
to = "task"
'''

def _listed():
    async def scenario():
        async with create_connected_server_and_client_session(mcp_server.mcp) as client:
            return {t.name: t for t in (await client.list_tools()).tools}
    return asyncio.run(scenario())


def _enum(prop):
    for member in prop.get("anyOf", [prop]):
        if "enum" in member:
            return member["enum"]
    return prop.get("enum")


@pytest.fixture
def isolated_schemas(monkeypatch):
    for tool, _ in ENUM_PARAMS:
        registered = mcp_server.mcp._tool_manager.get_tool(tool)
        monkeypatch.setattr(registered, "parameters", copy.deepcopy(registered.parameters))


def test_every_description_fits_the_client_limit_on_every_supported_python():
    for name, tool in _listed().items():
        assert len(tool.description or "") <= DESCRIPTION_LIMIT, name
        doc = inspect.getdoc(getattr(mcp_server, name))
        assert len(doc) + 4 * doc.count("\n") <= DESCRIPTION_LIMIT, name


@pytest.mark.parametrize("name", ["kill_worker", "kill_child"])
def test_a_kill_tool_tells_its_caller_to_capture_the_pane_first(name):
    description = " ".join(_listed()[name].description.split())
    assert "Capture the pane first; a live approval prompt means the worker is alive: do not kill it." in description
    assert "a worker asks its manager" in description
    assert "clear the prompt" not in description


def test_spawn_child_carries_the_wait_contract():
    description = " ".join(_listed()["spawn_child"].description.split())
    assert "Check the spawn result before you wait" in description
    assert "Never kill a child that passed this check before wait_for_child returns done or exited" in description
    assert "no_register" in description


def test_artifact_put_offers_its_closed_values():
    props = _listed()["artifact_put"].inputSchema["properties"]
    assert _enum(props["scope"]) == ["output", "checkpoint", "internal"]
    assert _enum(props["status"]) == ["partial", "complete"]
    assert _enum(props["decided_by"]) == ["manager", "engineer"]


def test_every_manager_sid_parameter_is_described():
    for name, tool in _listed().items():
        prop = tool.inputSchema.get("properties", {}).get("manager_sid")
        if prop is not None:
            assert prop.get("description"), name


def test_advertise_offers_the_nodes_of_every_schema_and_the_roles(tmp_path, monkeypatch, isolated_schemas):
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    (schemas / "alpha.toml").write_text(ALPHA)
    (schemas / "beta.toml").write_text(BETA)
    monkeypatch.setattr(config, "pipeline_schemas_dir", lambda: schemas)
    monkeypatch.setattr(roles, "role_names", lambda roles_dir=None: ["Coder", "GenericWorker"])
    tool_schema.advertise(mcp_server.mcp)
    tools = _listed()
    assert _enum(tools["pipeline_advance"].inputSchema["properties"]["to"]) == ["design", "plan", "task", "?"]
    assert _enum(tools["pipeline_rework"].inputSchema["properties"]["to"]) == ["design", "plan", "task"]
    assert _enum(tools["spawn_child"].inputSchema["properties"]["role"]) is None
    assert _enum(tools["spawn_worker"].inputSchema["properties"]["tier"]) is None
    assert "effort" in tools["spawn_child"].inputSchema["properties"]
    assert "lane" in tools["pipeline_child"].inputSchema["properties"]
    assert _enum(tools["spawn_worker"].inputSchema["properties"]["role"]) == ["Coder", "GenericWorker"]
    assert _enum(tools["spawn_child"].inputSchema["properties"]["context"]) == ["Coder", "GenericWorker"]


def test_advertise_offers_no_context_role_before_any_manifest_exists(tmp_path, monkeypatch, isolated_schemas):
    monkeypatch.setattr(config, "pipeline_schemas_dir", lambda: tmp_path / "no-schemas")
    tool_schema.advertise(mcp_server.mcp)
    assert _enum(_listed()["spawn_worker"].inputSchema["properties"]["role"]) is None


def test_advertise_warns_and_offers_no_context_role_when_the_roles_cannot_be_listed(tmp_path, monkeypatch, capsys,
                                                                                   isolated_schemas):
    def broken(roles_dir=None):
        raise OSError("roles dir unreadable")

    monkeypatch.setattr(config, "pipeline_schemas_dir", lambda: tmp_path / "no-schemas")
    monkeypatch.setattr(roles, "role_names", broken)
    tool_schema.advertise(mcp_server.mcp)
    assert _enum(_listed()["spawn_child"].inputSchema["properties"]["context"]) is None
    assert "no role enum on spawn_worker/spawn_child: OSError: roles dir unreadable" in capsys.readouterr().err


def test_advertise_warns_and_offers_nothing_when_a_schema_cannot_be_read(tmp_path, monkeypatch, capsys,
                                                                           isolated_schemas):
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    (schemas / "broken.toml").write_text("[nodes.design\n")
    monkeypatch.setattr(config, "pipeline_schemas_dir", lambda: schemas)
    tool_schema.advertise(mcp_server.mcp)
    assert _enum(_listed()["pipeline_advance"].inputSchema["properties"]["to"]) is None
    assert "no node enum" in capsys.readouterr().err


def test_main_advertises_before_the_server_runs(monkeypatch):
    calls = []
    monkeypatch.setattr(tool_schema, "advertise", lambda server: calls.append(("advertise", server)))
    monkeypatch.setattr(mcp_server.mcp, "run", lambda: calls.append(("run", None)))
    mcp_server.main()
    assert calls == [("advertise", mcp_server.mcp), ("run", None)]
