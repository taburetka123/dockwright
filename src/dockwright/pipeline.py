from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import agent_guard, config
from . import models as models_mod
from . import topic
from .compaction import preserve_clause

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_PLACEHOLDER = re.compile(r"\{(task_key|cwd|node|i|inputs|checkpoints|round)\}")
EXIT_CHECK_TYPES = ("artifact", "value", "command", "when")
WHEN_VALUES = ("last",)
TRIGGER_VALUES = ("merge", "approve", "status")
STATUS_CATEGORIES = ("new", "indeterminate", "done")
MERGE_STATE = "merge.state"
SCOPES = ("output", "checkpoint", "internal")
SUMMARY_PHASE = "summary"
SUMMARY_NAME = "thread"
DECISION_SECTION = "decided_by on decision records"
SUMMARY_SECTION = "The running summary"
SUMMARY_THREAD = f"{SUMMARY_PHASE}.{SUMMARY_NAME}"
SUBAGENT_MODELS = ("sonnet", "opus", "haiku", "fable", "inherit")
PROFILE_KEYS = ("claudeMdExcludes", "skillOverrides", "skillListingMaxDescChars", "disabledMcpjsonServers")
ROLE_SETTINGS_KEYS = (*PROFILE_KEYS, "permissions")
PERMISSION_KEYS = ("allow", "deny")
SKILL_OVERRIDE_VALUES = ("on", "name-only", "user-invocable-only", "off")
DEFAULT_TOKEN_ID = "t1"
REPEAT_LIST = "plan.tasks"
TASK_LIST_CONTRACT = ('bare JSON {"tasks": [...]} with at least one entry, every entry a JSON object '
                      '{"id", "brief", "cwd", "checks"}, optionally with "row" (a non-empty string, on every entry '
                      'or on none) and "stage" (an integer >= 1, the same on every entry of a row), with no code '
                      'fence and no other text')
MAX_EXIT_CHECK_TIMEOUT_SEC = 86400
RESERVED_EVENT_TYPES = frozenset({
    "pipeline_started", "schema_snapshot", "advance_requested", "advance_committed",
    "advance_pending", "advance_refused", "token_override", "handed_off",
    "holder_restamped", "trigger_fired", "artifact_read", "round_started",
    "token_held", "token_released", "token_forked", "token_retired", "topic_set", "lane_event",
    "message_delivered", "question_answered", "work_closed", "child_round", "child_closed",
    "refresh_requested", "token_back", "inputs_handed",
    "subscribed", "unsubscribed", "event_delivered", "event_handled", "event_started",
})
TOOL_PREFIX = "mcp__dockwright__"
PR_URL = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+")


def pr_urls(body: str) -> list[str]:
    listed: dict[str, None] = {}
    for line in (body or "").split("\n"):
        match = PR_URL.search(line)
        if match:
            listed[match.group(0)] = None
    return list(listed)


_KIND_NAMES = {"str": "a string", "bool": "a boolean", "int": "an integer", "strs": "a list of strings",
               "table": "a table", "tables": "a list of tables"}
_TOP_SPEC = {"schema": "str", "version": "int", "rework_at": "str", "nodes": "table", "edges": "tables"}
_NODE_SPEC = {"role": "str", "tier": "str", "model": "str", "effort": "str", "gate": "bool", "brief": "str",
              "brief_text": "str", "inputs": "strs", "outputs": "strs", "checkpoints": "strs", "repeat": "str",
              "extra_args": "strs", "profiles": "strs", "profile_settings": "table", "children": "table",
              "subagents": "table", "tier_when": "table", "model_when": "table"}
_EDGE_SPEC = {"from": "str", "to": "str", "exit_checks": "tables", "when": "str", "trigger": "str",
              "status": "strs", "fork": "bool", "join": "bool", "redo": "bool", "back": "bool"}
_CHILD_SPEC = {"roles": "strs", "warm": "bool", "rounds": "int", "step": "str", "subagents": "table",
               "role": "str", "mode": "str", "model": "str", "effort": "str", "codex": "str", "lanes": "table"}
_LANE_SPEC = {"role": "str", "mode": "str", "model": "str", "effort": "str", "codex": "str"}
_LANE_KEYS = ("role", "mode", "model", "effort")
DEFAULT_LANE = "default"
_SUBAGENT_SPEC = {"type": "str", "model": "str", "effort": "str", "step": "str"}
_TIER_WHEN_SPEC = {"input": "str", "line": "str", "equals": "str", "unless": "str", "tier": "str"}
_MODEL_WHEN_SPEC = {"input": "str", "line": "str", "equals": "str", "unless": "str", "model": "str", "effort": "str"}
_EXIT_CHECK_REQUIRED = {"artifact": ("name",), "value": ("name", "field", "equals"), "command": ("run",),
                        "when": ("input", "line", "equals")}
_EXIT_CHECK_OPTIONAL = {"command": ("timeout_sec",), "when": ("unless",)}
_REQUEST_BOUND_EVENTS = ("advance_pending", "advance_committed", "holder_restamped")
_MODEL_FLAGS = (*models_mod.MODEL_OPTIONS, "--fallback-model")
_TASK_LIST_UNREADABLE = (f"{REPEAT_LIST} holds no task list the engine can read: the body must be "
                         f"{TASK_LIST_CONTRACT}; re-publish {REPEAT_LIST} with overwrite=True")
_TIMED_OUT = 256
_CANNOT_START = 257


@dataclass
class Subagent:
    name: str
    type: str
    model: str = "inherit"
    step: str | None = None
    effort: str | None = None

    @property
    def pair(self) -> str:
        return f"{self.type}:{self.model}"

    @property
    def entry(self) -> str:
        return f"{self.pair}:{self.effort}" if self.effort else self.pair


@dataclass(frozen=True)
class Lane:
    name: str
    role: str | None
    mode: str | None
    model: str | None
    effort: str | None
    codex: str | None = None


@dataclass
class ChildSlot:
    name: str
    roles: list[str] = field(default_factory=list)
    rounds: int = 0
    warm: bool = True
    step: str | None = None
    subagents: dict[str, Subagent] = field(default_factory=dict)
    lanes: dict[str, Lane] = field(default_factory=dict)
    own_keys: bool = False


@dataclass
class TierWhen:
    input: str
    line: str
    equals: str
    tier: str
    unless: str | None = None


@dataclass
class ModelWhen:
    input: str
    line: str
    equals: str
    model: str
    effort: str
    unless: str | None = None


@dataclass
class EdgeWhen:
    input: str
    line: str
    equals: str
    unless: str | None = None


@dataclass
class Node:
    name: str
    role: str
    tier: str | None = None
    gate: bool = False
    brief: str | None = None
    brief_text: str | None = None
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    checkpoints: list[str] = field(default_factory=list)
    repeat: str | None = None
    extra_args: list[str] = field(default_factory=list)
    profiles: list[str] = field(default_factory=list)
    profile_settings: dict = field(default_factory=dict)
    children: dict[str, ChildSlot] = field(default_factory=dict)
    subagents: dict[str, Subagent] = field(default_factory=dict)
    tier_when: TierWhen | None = None
    model: str | None = None
    effort: str | None = None
    model_when: ModelWhen | None = None


@dataclass
class Edge:
    frm: str
    to: str
    exit_checks: list[dict] = field(default_factory=list)
    when: str | None = None
    trigger: str | None = None
    status: list[str] = field(default_factory=list)
    fork: bool = False
    join: bool = False
    redo: bool = False
    back: bool = False


@dataclass
class Schema:
    name: str
    version: int
    nodes: dict[str, Node]
    edges: list[Edge]
    source_dir: Path | None = None
    problems: list[str] = field(default_factory=list)
    redo_edges: list[Edge] = field(default_factory=list)
    back_edges: list[Edge] = field(default_factory=list)
    rework_at: str | None = None


class SchemaError(ValueError):
    pass


def _fits(value, kind: str) -> bool:
    if kind == "str":
        return isinstance(value, str)
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "strs":
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    if kind == "table":
        return isinstance(value, dict)
    return isinstance(value, list) and all(isinstance(v, dict) for v in value)


def _typed(table: dict, spec: dict[str, str], where: str, problems: list[str]) -> dict:
    out: dict = {}
    for key, value in table.items():
        if key not in spec:
            problems.append(f"{where} unknown key {key!r}")
        elif _fits(value, spec[key]):
            out[key] = value
        else:
            problems.append(f"{where} `{key}` must be {_KIND_NAMES[spec[key]]}, not {type(value).__name__}")
    return out


def _subagents_from(table: dict, where: str, problems: list[str]) -> dict[str, Subagent]:
    out: dict[str, Subagent] = {}
    for slot, raw in table.items():
        at = f"{where}.subagents.{slot}]"
        if not isinstance(raw, dict):
            problems.append(f"{at} must be a table")
            continue
        t = _typed(raw, _SUBAGENT_SPEC, at, problems)
        out[slot] = Subagent(slot, t.get("type", ""), t.get("model", "inherit"), t.get("step"), t.get("effort"))
    return out


def _lane_from(name: str, raw: dict, where: str, problems: list[str]) -> Lane:
    t = _typed(raw, _LANE_SPEC, where, problems)
    return Lane(name, t.get("role"), t.get("mode"), t.get("model"), t.get("effort"), t.get("codex"))


def _children_from(table: dict, where: str, problems: list[str]) -> dict[str, ChildSlot]:
    out: dict[str, ChildSlot] = {}
    for slot, raw in table.items():
        at = f"{where}.children.{slot}"
        if not isinstance(raw, dict):
            problems.append(f"{at}] must be a table")
            continue
        t = _typed(raw, _CHILD_SPEC, f"{at}]", problems)
        own = any(key in t for key in (*_LANE_KEYS, "codex"))
        lanes: dict[str, Lane] = {}
        raw_lanes = t.get("lanes", {})
        if not isinstance(raw_lanes, dict):
            problems.append(f"{at}.lanes] must be a table of lane tables")
            raw_lanes = {}
        for lane_name, lane_raw in raw_lanes.items():
            if not isinstance(lane_raw, dict):
                problems.append(f"{at}.lanes.{lane_name}] must be a table")
                continue
            lanes[lane_name] = _lane_from(lane_name, lane_raw, f"{at}.lanes.{lane_name}]", problems)
        if own and raw_lanes:
            problems.append(f"{at}] names both lanes and its own lane keys; pick one")
        elif own:
            lanes[DEFAULT_LANE] = Lane(DEFAULT_LANE, t.get("role"), t.get("mode"), t.get("model"), t.get("effort"),
                                       t.get("codex"))
        out[slot] = ChildSlot(slot, list(t.get("roles", [])), t.get("rounds", 0), t.get("warm", True), t.get("step"),
                              _subagents_from(t.get("subagents", {}), at, problems), lanes, own and not raw_lanes)
    return out


def _tier_when_from(table: dict | None, where: str, problems: list[str]) -> TierWhen | None:
    if table is None:
        return None
    t = _typed(table, _TIER_WHEN_SPEC, where, problems)
    if any(key not in t for key in ("input", "line", "equals", "tier")):
        problems.append(f"{where} needs input, line, equals and tier")
        return None
    return TierWhen(t["input"], t["line"], t["equals"], t["tier"], t.get("unless"))


def _model_when_from(table: dict | None, where: str, problems: list[str]) -> ModelWhen | None:
    if table is None:
        return None
    t = _typed(table, _MODEL_WHEN_SPEC, where, problems)
    if any(key not in t for key in ("input", "line", "equals", "model", "effort")):
        problems.append(f"{where} needs input, line, equals, model and effort")
        return None
    return ModelWhen(t["input"], t["line"], t["equals"], t["model"], t["effort"], t.get("unless"))


def _node_from(name: str, table: dict, problems: list[str]) -> Node:
    t = _typed(table, _NODE_SPEC, f"[nodes.{name}]", problems)
    return Node(
        name=name, role=t.get("role", ""), tier=t.get("tier"), gate=t.get("gate", False),
        brief=t.get("brief"), brief_text=t.get("brief_text"),
        inputs=list(t.get("inputs", [])), outputs=list(t.get("outputs", [])),
        checkpoints=list(t.get("checkpoints", [])), repeat=t.get("repeat"),
        extra_args=list(t.get("extra_args", [])),
        profiles=list(t.get("profiles", [])), profile_settings=dict(t.get("profile_settings", {})),
        children=_children_from(t.get("children", {}), f"[nodes.{name}", problems),
        subagents=_subagents_from(t.get("subagents", {}), f"[nodes.{name}", problems),
        tier_when=_tier_when_from(t.get("tier_when"), f"[nodes.{name}.tier_when]", problems),
        model=t.get("model"), effort=t.get("effort"),
        model_when=_model_when_from(t.get("model_when"), f"[nodes.{name}.model_when]", problems),
    )


def _edge_from(number: int, table: dict, problems: list[str]) -> Edge:
    frm, to = table.get("from"), table.get("to")
    if not isinstance(frm, str) or not isinstance(to, str):
        raise SchemaError(f"[[edges]] #{number}: `from` and `to` must both be strings")
    t = _typed(table, _EDGE_SPEC, f"[[edges]] {frm} -> {to}:", problems)
    return Edge(frm=frm, to=to, exit_checks=list(t.get("exit_checks", [])),
                when=t.get("when"), trigger=t.get("trigger"), status=list(t.get("status", [])),
                fork=t.get("fork", False), join=t.get("join", False), redo=t.get("redo", False),
                back=t.get("back", False))


def _schema_from(data: dict, source_dir: Path | None) -> Schema:
    raw_nodes = data.get("nodes", {})
    if not _fits(raw_nodes, "table"):
        raise SchemaError("`nodes` must be a table of [nodes.<name>] tables")
    for name, table in raw_nodes.items():
        if not _fits(table, "table"):
            raise SchemaError(f"[nodes.{name}] must be a table")
    raw_edges = data.get("edges", [])
    if not _fits(raw_edges, "tables"):
        raise SchemaError("`edges` must be an array of [[edges]] tables")
    problems: list[str] = []
    top = _typed(data, _TOP_SPEC, "schema:", problems)
    nodes = {n: _node_from(n, t, problems) for n, t in raw_nodes.items()}
    edges = [_edge_from(i, e, problems) for i, e in enumerate(raw_edges, 1)]
    return Schema(name=top.get("schema", ""), version=top.get("version", 1),
                  nodes=nodes, edges=[e for e in edges if not e.redo and not e.back], source_dir=source_dir,
                  problems=problems, redo_edges=[e for e in edges if e.redo and not e.back],
                  back_edges=[e for e in edges if e.back], rework_at=top.get("rework_at"))


def load_schema(path: Path) -> Schema:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return _schema_from(data, Path(path).parent)


def load_snapshot(text: str) -> Schema:
    return _schema_from(tomllib.loads(text), None)


def start_node(schema: Schema) -> str | None:
    targets = {e.to for e in schema.edges if e.to != e.frm}
    starts = [n for n in schema.nodes if n not in targets]
    return starts[0] if len(starts) == 1 else None


def _needed_manifests(schema: Schema) -> tuple[dict, set[str], list[str]]:
    from . import roles
    names = set(roles.role_names())
    wanted = {node.role for node in schema.nodes.values() if node.role in names}
    wanted |= {lane.role for node in schema.nodes.values() for slot in node.children.values()
               for lane in slot.lanes.values() if lane.role in names}
    manifests: dict = {}
    problems: list[str] = []
    for name in sorted(wanted):
        try:
            manifests[name] = roles.load_role(name)
        except roles.RoleError as err:
            problems.append(f"role {name}: {err}")
    return manifests, names, problems


def validate(schema: Schema, cwd: str | None, manifests: dict | None = None) -> list[str]:
    problems: list[str] = list(schema.problems)
    if manifests is None:
        manifests, names, load_problems = _needed_manifests(schema)
        problems.extend(load_problems)
    else:
        names = set(manifests)
    if not schema.name:
        problems.append("schema: missing `schema` name")
    start = start_node(schema)
    if start is None:
        problems.append("schema: exactly one start node (no incoming edge) is required")
    for name, node in schema.nodes.items():
        problems.extend(_role_problems(name, node, manifests, names))
        if names:
            problems.extend(_role_launch_problems(name, node))
        brief = _brief_problem(schema, name, node)
        if brief is not None:
            problems.append(brief)
        problems.extend(_node_profile_problems(schema, name, node))
        for arg in node.extra_args:
            if arg in _MODEL_FLAGS or any(arg.startswith(f"{o}=") for o in _MODEL_FLAGS):
                problems.append(f"[nodes.{name}] extra_args carries {arg!r}; a node names its model in model")
            elif node.model is not None and (arg == "--effort" or arg.startswith("--effort=")):
                problems.append(f"[nodes.{name}] extra_args carries {arg!r}; a node that names model names its "
                                "effort in effort")
        for template in (*node.outputs, *node.checkpoints):
            if _matches(template, SUMMARY_THREAD):
                problems.append(f"[nodes.{name}] declares {template!r}, the work's running summary every node "
                                "writes; name the node's own output something else")
        if node.repeat and node.repeat != REPEAT_LIST:
            problems.append(f"[nodes.{name}] repeat {node.repeat!r}: stage 1 repeats over {REPEAT_LIST!r} only, "
                            "the list the index bound counts")
        if node.repeat and not any(e.frm == name and e.to == name for e in schema.edges):
            problems.append(f"[nodes.{name}] repeat needs a self-loop edge {name} -> {name}; "
                            "without it the token never moves past index 1")
        for slot in node.children.values():
            at = f"[nodes.{name}.children.{slot.name}]"
            problems.extend(_lane_problems(at, slot, manifests, names))
            if not _fits(slot.rounds, "int") or slot.rounds < 1:
                problems.append(f"{at} rounds must be an integer of at least 1")
            problems.extend(_subagent_problems(slot.subagents, f"[nodes.{name}.children.{slot.name}",
                                               node.model is not None))
        problems.extend(_subagent_problems(node.subagents, f"[nodes.{name}", node.model is not None))
        problems.extend(_model_problems(schema, name, node))
    for name in _topological(schema):
        node = schema.nodes[name]
        upstream = _upstream_outputs(schema, name)
        for inp in node.inputs:
            if inp not in upstream:
                problems.append(f"[nodes.{name}] input {inp!r} is not an output or checkpoint of any upstream node "
                                "(a node with an edge path to this one)")
        if node.repeat and node.repeat not in upstream:
            problems.append(f"[nodes.{name}] repeat {node.repeat!r} is not an output or checkpoint of any upstream "
                            "node (a node with an edge path to this one)")
    seen: set[tuple[str, str]] = set()
    fork_targets = {e.to for e in schema.edges if e.fork and e.frm != e.to}
    for name in schema.nodes:
        kinds = [e.trigger for e in schema.edges if e.frm == name and e.trigger]
        if kinds.count("status") > 1:
            problems.append(f"[nodes.{name}] has two status edges out; one node polls one status list")
        other = next((k for k in kinds if k != "status"), None)
        if "status" in kinds and other is not None:
            problems.append(f"[nodes.{name}] mixes a status edge with a {other} edge; a node takes one trigger kind")
    for e in schema.edges:
        where = f"[[edges]] {e.frm} -> {e.to}:"
        if e.frm not in schema.nodes or e.to not in schema.nodes:
            problems.append(f"{where} unknown node")
        elif e.frm == e.to and not schema.nodes[e.frm].repeat:
            problems.append(f"{where} a self-loop needs `repeat` on [nodes.{e.frm}]; without it the edge is never taken")
        if e.frm == e.to:
            for key, value in (("when", e.when), ("trigger", e.trigger)):
                if value is not None:
                    problems.append(f"{where} a self-loop cannot carry `{key}`")
        if e.fork and e.join:
            problems.append(f"{where} an edge cannot carry both `fork` and `join`")
        if e.fork or e.join:
            if e.frm == e.to:
                problems.append(f"{where} a self-loop cannot carry `fork` or `join`")
            if e.trigger is not None:
                problems.append(f"{where} a trigger edge cannot carry `fork` or `join`")
        if e.fork and e.to in schema.nodes and not schema.nodes[e.to].repeat:
            problems.append(f"{where} `fork` needs a repeat node as its target")
        if e.join and e.frm in schema.nodes and not (schema.nodes[e.frm].repeat and e.when == "last"):
            problems.append(f"{where} `join` needs a repeat node as its source and when = \"last\"")
        if e.to in fork_targets and e.frm != e.to and not e.fork:
            problems.append(f"{where} {e.to} is a fork target: only fork edges and its self-loop enter it")
        if e.frm in fork_targets and e.frm != e.to and not e.join:
            problems.append(f"{where} [nodes.{e.frm}] is entered by a fork edge, so it leaves only by its self-loop "
                            "or a join edge")
        if (e.frm, e.to) in seen:
            problems.append(f"{where} duplicate edge")
        seen.add((e.frm, e.to))
        if e.when is not None and e.when not in WHEN_VALUES:
            problems.append(f"{where} when {e.when!r} is not one of {list(WHEN_VALUES)}")
        if e.trigger is not None and e.trigger not in TRIGGER_VALUES:
            problems.append(f"{where} trigger {e.trigger!r} is not one of {list(TRIGGER_VALUES)}")
        if e.trigger == "status" and not e.status:
            problems.append(f'{where} trigger "status" needs a non-empty `status` list')
        if e.status and e.trigger != "status":
            problems.append(f'{where} `status` needs trigger = "status"')
        for entry in e.status:
            if not entry:
                problems.append(f"{where} `status` holds an empty entry")
            elif entry.startswith("category:") and entry[len("category:"):] not in STATUS_CATEGORIES:
                problems.append(f"{where} category {entry[len('category:'):]!r} is not one of {list(STATUS_CATEGORIES)}")
        if e.trigger == "approve" and e.to in schema.nodes and MERGE_STATE not in schema.nodes[e.to].checkpoints:
            problems.append(f'{where} trigger "approve" needs checkpoint "{MERGE_STATE}" on [nodes.{e.to}]')
        for c in e.exit_checks:
            check = _exit_check_problem(c)
            if check is not None:
                problems.append(f"{where} {check}")
            if c.get("type") == "when" and check is None and e.frm in schema.nodes:
                known = _templates(schema, _upstream_nodes(schema, e.frm) | {e.frm})
                for key in ("input", "unless"):
                    if key in c and c[key] not in known:
                        problems.append(f"{where} exit_check when `{key}` {c[key]!r} is not an output or checkpoint "
                                        f"of {e.frm} or a node upstream of it")
            kind = c.get("type")
            if kind in _EXIT_CHECK_REQUIRED:
                allowed = ("type", *_EXIT_CHECK_REQUIRED[kind], *_EXIT_CHECK_OPTIONAL.get(kind, ()))
                for key in c:
                    if key not in allowed:
                        problems.append(f"{where} exit_check unknown key {key!r} for type {kind!r}")
            if c.get("type") == "command" and any("\0" in a for a in _run_strings(c.get("run"))):
                problems.append(f"{where} exit_check command `run` contains a NUL character")
            try:
                _toml_value(c)
            except ValueError as err:
                problems.append(f"{where} exit_check does not fit a TOML snapshot: {err}")
    forward_pairs = {(e.frm, e.to) for e in schema.edges}
    redo_seen: set[tuple[str, str]] = set()
    for e in schema.redo_edges:
        where = f"[[edges]] {e.frm} -> {e.to} (redo):"
        if e.frm not in schema.nodes or e.to not in schema.nodes:
            problems.append(f"{where} unknown node")
            continue
        if e.frm == e.to:
            problems.append(f"{where} a redo edge goes back to an earlier node, never to its own source")
        elif e.frm not in _reachable(schema, e.to):
            problems.append(f"{where} {e.frm} is not reachable from {e.to} by forward edges; a redo edge goes back "
                            "along the work's path")
        for key, value in (("when", e.when), ("trigger", e.trigger), ("status", e.status or None)):
            if value is not None:
                problems.append(f"{where} a redo edge cannot carry `{key}`")
        if e.fork or e.join:
            problems.append(f"{where} a redo edge cannot carry `fork` or `join`")
        if e.exit_checks:
            problems.append(f"{where} a redo edge cannot carry `exit_checks`")
        if schema.nodes[e.to].repeat:
            problems.append(f"{where} a redo edge cannot enter the repeat node {e.to}")
        if e.to in fork_targets:
            problems.append(f"{where} {e.to} is a fork target: a redo edge cannot enter it")
        if (e.frm, e.to) in forward_pairs or (e.frm, e.to) in redo_seen:
            problems.append(f"{where} duplicate edge")
        redo_seen.add((e.frm, e.to))
    redo_pairs = {(e.frm, e.to) for e in schema.redo_edges}
    back_seen: set[tuple[str, str]] = set()
    for e in schema.back_edges:
        where = f"[[edges]] {e.frm} -> {e.to} (back):"
        if e.frm not in schema.nodes or e.to not in schema.nodes:
            problems.append(f"{where} unknown node")
            continue
        if e.redo:
            problems.append(f"{where} an edge is either redo or back, never both")
        if e.frm == e.to:
            problems.append(f"{where} a back edge goes back to an earlier node, never to its own source")
        elif e.frm not in _reachable(schema, e.to):
            problems.append(f"{where} {e.frm} is not reachable from {e.to} by forward edges; a back edge returns "
                            "along the work's path")
        if e.to == start:
            problems.append(f"{where} a back edge cannot enter the start node {e.to}")
        if e.frm in fork_targets:
            problems.append(f"{where} [nodes.{e.frm}] is entered by a fork edge, so it leaves only by its self-loop "
                            "or a join edge")
        for key, value in (("when", e.when), ("trigger", e.trigger), ("status", e.status or None)):
            if value is not None:
                problems.append(f"{where} a back edge cannot carry `{key}`")
        if e.fork or e.join:
            problems.append(f"{where} a back edge cannot carry `fork` or `join`")
        if e.exit_checks:
            problems.append(f"{where} a back edge cannot carry `exit_checks`")
        if (e.frm, e.to) in forward_pairs or (e.frm, e.to) in redo_pairs or (e.frm, e.to) in back_seen:
            problems.append(f"{where} duplicate edge")
        back_seen.add((e.frm, e.to))
    if schema.rework_at is not None and schema.rework_at not in schema.nodes:
        problems.append(f"schema: rework_at {schema.rework_at!r} is not a node of the schema")
    for name in sorted(fork_targets):
        if not any(e.join and e.frm == name for e in schema.edges):
            problems.append(f"[nodes.{name}] is entered by a fork edge and has no join edge out")
    if start is not None:
        reachable = _reachable(schema, start)
        for name in schema.nodes:
            if name not in reachable:
                problems.append(f"[nodes.{name}] is unreachable from the start node {start!r}")
    return problems


def _subagent_problems(subagents: dict[str, Subagent], where: str, need_effort: bool = False) -> list[str]:
    problems: list[str] = []
    for sub in subagents.values():
        at = f"{where}.subagents.{sub.name}]"
        if not sub.type:
            problems.append(f"{at} type must be a non-empty string")
        if sub.model not in SUBAGENT_MODELS:
            problems.append(f"{at} model {sub.model!r} is not one of {', '.join(SUBAGENT_MODELS)}")
        if sub.effort is not None and sub.effort not in models_mod.EFFORTS:
            problems.append(f"{at} effort {sub.effort!r} is not one of {', '.join(models_mod.EFFORTS)}")
        elif sub.effort is None and need_effort:
            problems.append(f"{at} names no effort; a node that names its model names each subagent's effort")
    return problems


def _role_problems(name: str, node: Node, manifests: dict, names: set[str]) -> list[str]:
    where = f"[nodes.{name}]"
    if node.model is None:
        return [f"{where} names no model; name model and effort on the node"]
    if node.tier:
        return [f"{where} names tier {node.tier!r}; the roster is gone, name model and effort"]
    if not names:
        return []
    if node.role not in names:
        return [f"{where} role {node.role!r} names no role manifest"]
    manifest = manifests.get(node.role)
    if manifest is not None and "worker" not in manifest.launch:
        return [f"{where} role {node.role!r} does not declare launch 'worker'"]
    return []


def _role_launch_problems(name: str, node: Node) -> list[str]:
    from .spawner import SYSTEM_PROMPT_OPTIONS, _matches_option
    problems: list[str] = []
    if any(_matches_option(a, SYSTEM_PROMPT_OPTIONS) for a in node.extra_args):
        problems.append(f"[nodes.{name}] extra_args carries a system-prompt flag; a node loads its role's render")
    if not (node.profiles or node.profile_settings):
        _, value = split_settings_arg(node.extra_args)
        if value is not None:
            try:
                read_settings_value(value)
            except ValueError as err:
                problems.append(f"[nodes.{name}] extra_args --settings {err}")
    return problems


def _when_problems(schema: Schema, name: str, node: Node, rule: TierWhen | ModelWhen, where: str) -> list[str]:
    problems: list[str] = []
    if rule.input not in node.inputs:
        problems.append(f"{where} input {rule.input!r} is not among the node's inputs")
    if not _BARE_KEY.fullmatch(rule.line):
        problems.append(f"{where} line {rule.line!r} must be a bare key: letters, digits, '_' or '-'")
    elif not rule.equals or line_word(f"{rule.line}: {rule.equals}", rule.line) != rule.equals.lower():
        problems.append(f"{where} equals {rule.equals!r} can never match: it must be one word")
    if rule.unless is not None and rule.unless not in _templates(schema, _upstream_nodes(schema, name)):
        problems.append(f"{where} unless {rule.unless!r} is not an output or checkpoint of any upstream node")
    return problems


def _model_effort_problems(where: str, model: str, effort: str | None) -> list[str]:
    problems: list[str] = []
    problem = models_mod.model_value_problem(model)
    if problem is not None:
        problems.append(f"{where} model {model!r} {problem}")
    if effort is None:
        problems.append(f"{where} names model but no effort; name effort as one of {', '.join(models_mod.EFFORTS)}")
    elif effort not in models_mod.EFFORTS:
        problems.append(f"{where} effort {effort!r} is not one of {', '.join(models_mod.EFFORTS)}")
    return problems


def _lane_problems(at: str, slot: ChildSlot, manifests: dict, names: set[str]) -> list[str]:
    from .providers.base import LEG_MODES
    if slot.roles:
        return [f"{at} roles names roster rows; write lanes with role, mode, model, effort"]
    if not slot.lanes:
        return [f"{at} name a lane: role, mode, model and effort in the slot, or [{at[1:-1]}.lanes.<name>] tables"]
    problems: list[str] = []
    for lane in slot.lanes.values():
        where = at if lane.name == DEFAULT_LANE and slot.own_keys else f"[{at[1:-1]}.lanes.{lane.name}]"
        if any(getattr(lane, key) is None for key in _LANE_KEYS):
            problems.append(f"{where} needs role, mode, model and effort")
            continue
        if names and lane.role not in names:
            problems.append(f"{where} role {lane.role!r} names no role manifest")
        elif lane.role in manifests and "child" not in manifests[lane.role].launch:
            problems.append(f"{where} role {lane.role!r} does not declare launch 'child'")
        if lane.mode not in LEG_MODES:
            problems.append(f"{where} mode {lane.mode!r} is not one of {', '.join(LEG_MODES)}")
        problems.extend(_model_effort_problems(where, lane.model, lane.effort))
        if lane.codex is not None:
            if not lane.codex.strip():
                problems.append(f"{where} codex must be a non-empty string")
            elif lane.codex.startswith("@"):
                problems.append(f"{where} codex {lane.codex!r} must be a concrete Codex model id, not a family token")
    return problems


def _model_problems(schema: Schema, name: str, node: Node) -> list[str]:
    where = f"[nodes.{name}]"
    if node.model is None:
        problems = [f"{where} names effort without model; name both"] if node.effort is not None else []
        if node.model_when is not None:
            problems.append(f"[nodes.{name}.model_when] needs model and effort on the node")
        return problems
    problems = []
    if node.tier:
        problems.append(f"{where} names both model and tier; tier is deprecated, keep model")
    if node.tier_when is not None:
        problems.append(f"{where} names tier_when beside model; write model_when")
    problems.extend(_model_effort_problems(where, node.model, node.effort))
    rule = node.model_when
    if rule is not None:
        at = f"[nodes.{name}.model_when]"
        problems.extend(_when_problems(schema, name, node, rule, at))
        problems.extend(_model_effort_problems(at, rule.model, rule.effort))
    return problems


def _brief_problem(schema: Schema, name: str, node: Node) -> str | None:
    if node.brief_text is not None:
        return None
    if node.brief is None:
        return f"[nodes.{name}] brief is required"
    path = schema.source_dir / node.brief if schema.source_dir is not None else None
    if path is None or not path.is_file():
        return f"[nodes.{name}] brief file {node.brief!r} does not exist"
    try:
        _read_brief(path)
    except (OSError, UnicodeDecodeError) as err:
        return f"[nodes.{name}] brief file {node.brief!r} cannot be read as UTF-8: {err}"
    return None


def _exit_check_problem(check: dict) -> str | None:
    kind = check.get("type")
    if kind not in EXIT_CHECK_TYPES:
        return f"exit_check type {kind!r} is not one of {list(EXIT_CHECK_TYPES)}"
    for key in _EXIT_CHECK_REQUIRED[kind]:
        if key not in check:
            return f"exit_check {kind} needs `{key}`"
        if key in ("name", "field") and not (isinstance(check[key], str) and check[key]):
            return f"exit_check {kind} `{key}` must be a non-empty string"
    if kind == "when":
        for key in ("input", "line", "equals", "unless"):
            if key in check and not (isinstance(check[key], str) and check[key]):
                return f"exit_check when `{key}` must be a non-empty string"
        if not _BARE_KEY.fullmatch(check["line"]):
            return f"exit_check when `line` {check['line']!r} must be a bare key: letters, digits, '_' or '-'"
        if line_word(f"{check['line']}: {check['equals']}", check["line"]) != check["equals"].lower():
            return f"exit_check when `equals` {check['equals']!r} can never match: it must be one word"
    if kind == "command":
        run = check["run"]
        if not ((isinstance(run, str) and run.strip())
                or (isinstance(run, list) and run and all(isinstance(a, str) for a in run))):
            return "exit_check command `run` must be a non-empty string or a non-empty list of strings"
        timeout = check.get("timeout_sec")
        if timeout is not None and not (_fits(timeout, "int") and 0 < timeout <= MAX_EXIT_CHECK_TIMEOUT_SEC):
            return f"exit_check command `timeout_sec` must be a positive integer no larger than {MAX_EXIT_CHECK_TIMEOUT_SEC}"
    return None


def profile_problems(settings: dict, where: str, keys=PROFILE_KEYS) -> list[str]:
    problems: list[str] = []
    for key, value in settings.items():
        if key not in keys:
            problems.append(f"{where} unknown profile key {key!r}; allowed: {list(keys)}")
        elif key == "permissions":
            if not (isinstance(value, dict) and set(value) <= set(PERMISSION_KEYS)
                    and all(isinstance(v, list) and all(isinstance(s, str) and s.strip() for s in v)
                            for v in value.values())):
                problems.append(f"{where} `permissions` must be a table of {list(PERMISSION_KEYS)}, "
                                "each a list of non-empty strings")
        elif key in ("claudeMdExcludes", "disabledMcpjsonServers"):
            if not (isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)):
                problems.append(f"{where} `{key}` must be a list of non-empty strings")
        elif key == "skillOverrides":
            if not (isinstance(value, dict)
                    and all(isinstance(k, str) and k and v in SKILL_OVERRIDE_VALUES for k, v in value.items())):
                problems.append(f"{where} `skillOverrides` must map skill names to one of {list(SKILL_OVERRIDE_VALUES)}")
        elif key == "skillListingMaxDescChars":
            if not (_fits(value, "int") and value > 0):
                problems.append(f"{where} `skillListingMaxDescChars` must be a positive integer")
    return problems


def guard_hook(allowed: list[str], bin_path: str) -> dict:
    allow = "".join(f" --allow {shlex.quote(pair)}" for pair in allowed)
    fallback = shlex.quote(json.dumps(agent_guard.deny(agent_guard.FAILED)))
    return {"matcher": agent_guard.MATCHER,
            "hooks": [{"type": "command", "command": f"{shlex.quote(bin_path)} agent-guard{allow} || echo {fallback}",
                       "timeout": 10}]}


def with_guard(settings: dict, allowed: list[str], bin_path: str) -> dict:
    hooks = dict(settings.get("hooks") or {})
    hooks["PreToolUse"] = [*(hooks.get("PreToolUse") or []), guard_hook(allowed, bin_path)]
    return {**settings, "hooks": hooks}


def merge_settings(base: dict, extra: dict) -> dict:
    merged = dict(base)
    for key, value in extra.items():
        prior = merged.get(key)
        if isinstance(value, list) and isinstance(prior, list):
            merged[key] = list(dict.fromkeys([*prior, *value]))
        elif isinstance(value, dict) and isinstance(prior, dict):
            merged[key] = merge_settings(prior, value)
        else:
            merged[key] = value
    return merged


def _profile_file(schema: Schema, node: Node, rel: str) -> tuple[dict | None, str | None]:
    where = f"[nodes.{node.name}] profile {rel!r}"
    if schema.source_dir is None:
        return None, f"{where}: a snapshot carries profile_settings, never profile files"
    path = schema.source_dir / rel
    if not path.is_file():
        return None, f"{where} does not exist"
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        return None, f"{where} cannot be read as TOML: {err}"
    problems = profile_problems(data, where)
    return (None, "; ".join(problems)) if problems else (data, None)


def node_profile_settings(schema: Schema, node: Node) -> dict:
    if not node.profiles:
        return dict(node.profile_settings)
    merged: dict = {}
    for rel in node.profiles:
        data, problem = _profile_file(schema, node, rel)
        if problem is not None:
            raise SchemaError(problem)
        merged = merge_settings(merged, data)
    return merged


def split_settings_arg(args: list[str]) -> tuple[list[str], str | None]:
    rest: list[str] = []
    value: str | None = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--settings" and i + 1 < len(args):
            value = args[i + 1]
            i += 2
            continue
        if arg.startswith("--settings="):
            value = arg.split("=", 1)[1]
        else:
            rest.append(arg)
        i += 1
    return rest, value


def read_settings_value(value: str) -> dict:
    if value.lstrip().startswith("{"):
        text = value
    elif not Path(value).is_absolute():
        raise ValueError(f"{value!r}: --settings must be an absolute path or inline JSON")
    else:
        try:
            text = Path(value).read_text()
        except (OSError, UnicodeDecodeError) as err:
            raise ValueError(f"{value!r} cannot be read: {err}") from err
    try:
        data = json.loads(text)
    except ValueError as err:
        raise ValueError(f"{value!r} is not JSON: {err}") from err
    if not isinstance(data, dict):
        raise ValueError(f"{value!r} is not a JSON object")
    return data


def _node_profile_problems(schema: Schema, name: str, node: Node) -> list[str]:
    problems: list[str] = []
    if node.profiles and node.profile_settings:
        problems.append(f"[nodes.{name}] sets both `profiles` and `profile_settings`; name profile files in "
                        "`profiles`, since `profile_settings` is the snapshot's key")
    for rel in node.profiles:
        _, problem = _profile_file(schema, node, rel)
        if problem is not None:
            problems.append(problem)
    problems.extend(profile_problems(node.profile_settings, f"[nodes.{name}] profile_settings"))
    if node.profiles or node.profile_settings:
        _, value = split_settings_arg(node.extra_args)
        if value is not None:
            try:
                read_settings_value(value)
            except ValueError as err:
                problems.append(f"[nodes.{name}] extra_args --settings {err}")
    return problems


def _run_strings(run) -> list[str]:
    if isinstance(run, str):
        return [run]
    if isinstance(run, list):
        return [a for a in run if isinstance(a, str)]
    return []


def _reachable(schema: Schema, start: str) -> set[str]:
    seen = {start}
    stack = [start]
    while stack:
        n = stack.pop()
        for e in schema.edges:
            if e.frm == n and e.to not in seen:
                seen.add(e.to)
                stack.append(e.to)
    return seen


def _topological(schema: Schema) -> list[str]:
    order: list[str] = []
    seen: set[str] = set()

    def visit(n: str) -> None:
        if n in seen or n not in schema.nodes:
            return
        seen.add(n)
        for e in schema.edges:
            if e.to == n and e.frm != n:
                visit(e.frm)
        order.append(n)

    for n in schema.nodes:
        visit(n)
    return order


def _upstream_nodes(schema: Schema, name: str) -> set[str]:
    stack = [e.frm for e in schema.edges if e.to == name and e.frm != name]
    seen: set[str] = set()
    while stack:
        n = stack.pop()
        if n in seen or n not in schema.nodes:
            continue
        seen.add(n)
        stack.extend(e.frm for e in schema.edges if e.to == n and e.frm != n)
    return seen


def _upstream_outputs(schema: Schema, name: str) -> set[str]:
    return set(_templates(schema, _upstream_nodes(schema, name)))


def redo_edge(schema: Schema, frm: str, to: str) -> Edge | None:
    if any(e.frm == frm and e.to == to for e in schema.edges):
        return None
    return next((e for e in schema.redo_edges if e.frm == frm and e.to == to), None)


def back_edge(schema: Schema, frm: str, to: str) -> Edge | None:
    if any(e.frm == frm and e.to == to for e in schema.edges):
        return None
    return next((e for e in schema.back_edges if e.frm == frm and e.to == to), None)


def declares_children(schema: Schema) -> bool:
    return any(node.children or node.subagents for node in schema.nodes.values())


def redo_round(round_event: dict | None) -> dict | None:
    return round_event if isinstance(round_event, dict) and round_event.get("via") == "redo" else None


def redo_carried_prs(round_event: dict | None) -> list[str]:
    return [u for u in (redo_round(round_event) or {}).get("carried_prs") or [] if isinstance(u, str)]


def last_rework_ask(events: list[dict]) -> str | None:
    asks = [e.get("ask") for e in events if e.get("type") == "round_started" and not redo_round(e)]
    return asks[-1] if asks else None


def rework_ask_name(round_no: int) -> str:
    return f"context.rework-{round_no}"


def _matches(template: str, name: str) -> bool:
    if "{i}" not in template:
        return template == name
    return re.fullmatch(re.escape(template).replace(re.escape("{i}"), r"\d+"), name) is not None


def _templates(schema: Schema, names: set[str]) -> list[str]:
    return [t for n in names for t in (*schema.nodes[n].outputs, *schema.nodes[n].checkpoints)]


def carried_names(schema: Schema, to: str, present: set[str], inputs: list[str]) -> set[str]:
    kept = _templates(schema, set(schema.nodes) - _reachable(schema, to))
    carried = {name for name in present
               if name in ("context.ask", SUMMARY_THREAD) or name.startswith("context.rework-") or name in inputs
               or any(_matches(t, name) for t in kept)}
    if "gate.open" in carried:
        carried |= {name for name in present if name.startswith("decision.verdict-r")}
    return carried


def rework_problems(schema: Schema, to: str, stamps: dict[str, dict], inputs: list[str]) -> list[str]:
    if to not in schema.nodes:
        return [f"{to!r} is not a node of the schema; its nodes are {', '.join(schema.nodes)}"]
    redo = _reachable(schema, to)
    redone = _templates(schema, redo)
    problems: list[str] = []
    for name in inputs:
        if name not in stamps:
            problems.append(f"input {name!r} is not in the store")
        elif name == "gate.open":
            problems.append("input 'gate.open' never rides a rework: the gate's own node opens it again, or the "
                            "schema carries it with that node's checkpoints")
        elif any(_matches(t, name) for t in redone):
            problems.append(f"input {name!r} is an output or checkpoint of a node this round redoes; carried, it "
                            "would count as the round's own work")
    carried = carried_names(schema, to, set(stamps), inputs)
    verdicts = sorted(c for c in carried if c.startswith("decision.verdict-r"))
    if verdicts and "gate.open" not in carried:
        problems.append(f"{', '.join(verdicts)} would outlive its gate.open: the next gate restarts at round 1 and its "
                        "verdict collides; carry his ruling in the ask or as decision.gate-<N>")
    produced = set(redone)
    seen: set[str] = set()
    for name in (n for n in schema.nodes if n in redo):
        node = schema.nodes[name]
        for want in (*node.inputs, *([node.repeat] if node.repeat else [])):
            if want in produced or want in seen:
                continue
            seen.add(want)
            found = [c for c in carried if _matches(want, c)]
            if found and all(stamps[c].get("status") == "complete" for c in found):
                continue
            source = next((m for m, other in schema.nodes.items() if want in other.outputs), None)
            hint = f"; enter at {source!r} or earlier" if source else ""
            problems.append(f"node {name!r} needs {want!r}, which no node this round redoes produces and the store "
                            f"does not carry complete{hint}")
    return problems


def _round_lines(round_no: int, cwd: str, redo_from: str | None = None, rework_ask: str | None = None) -> list[str]:
    ask = rework_ask_name(round_no)
    if redo_from:
        binds = (f"context.ask and {rework_ask} still bind; where they differ, {rework_ask} wins; where the reason "
                 "conflicts with them, ask_manager." if rework_ask else
                 "context.ask still binds; where the reason conflicts with it, ask_manager.")
        first = (f"Round {round_no} was opened by the {redo_from} node through its redo edge with the engineer's "
                 f"permission. {ask} is its reason. {binds} The store holds this round's work and what the round "
                 "carried forward; earlier rounds are archived and are not your inputs.")
    else:
        first = (f"Round {round_no} of this work is a rework. {ask} is this round's ask: where it differs from "
                 "anything written before this round - context.ask, an earlier round's ask, a carried output or "
                 "decision - it wins. The store holds this round's work and what the round carried forward; earlier "
                 "rounds are archived and are not your inputs.")
    return [first,
            f"If {cwd} is on a branch whose pull request has merged, move to a new branch off origin/main before you "
            "change anything there."]


def _toml_str(s: str) -> str:
    out: list[str] = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else _toml_str(key)


def _toml_value(value) -> str:
    if isinstance(value, str):
        return _toml_str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items())
        return "{ " + inner + " }" if inner else "{}"
    raise ValueError(f"cannot write {type(value).__name__} {value!r} as a TOML value")


def _read_brief(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _lane_lines(lane: Lane) -> list[str]:
    out = [f"{key} = {_toml_str(getattr(lane, key))}" for key in _LANE_KEYS if getattr(lane, key) is not None]
    if lane.codex is not None:
        out.append(f"codex = {_toml_str(lane.codex)}")
    return out


def snapshot(schema: Schema) -> str:
    lines = [f"schema = {_toml_str(schema.name)}", f"version = {schema.version}"]
    if schema.rework_at:
        lines.append(f"rework_at = {_toml_str(schema.rework_at)}")
    lines.append("")
    for name, node in schema.nodes.items():
        text = node.brief_text
        if text is None and node.brief is not None and schema.source_dir is not None:
            text = _read_brief(schema.source_dir / node.brief)
        lines.append(f"[nodes.{_toml_key(name)}]")
        lines.append(f"role = {_toml_str(node.role)}")
        if node.model is not None:
            lines.append(f"model = {_toml_str(node.model)}")
        if node.effort is not None:
            lines.append(f"effort = {_toml_str(node.effort)}")
        if node.tier:
            lines.append(f"tier = {_toml_str(node.tier)}")
        lines.append(f"gate = {_toml_value(node.gate)}")
        lines.append(f"brief_text = {_toml_str(text or '')}")
        lines.append(f"inputs = {_toml_value(node.inputs)}")
        lines.append(f"outputs = {_toml_value(node.outputs)}")
        lines.append(f"checkpoints = {_toml_value(node.checkpoints)}")
        if node.repeat:
            lines.append(f"repeat = {_toml_str(node.repeat)}")
        lines.append(f"extra_args = {_toml_value(node.extra_args)}")
        settings = node_profile_settings(schema, node)
        if settings:
            lines.append(f"profile_settings = {_toml_value(settings)}")
        lines.append("")
        prefix = f"nodes.{_toml_key(name)}"
        lines.extend(_tier_when_table(prefix, node.tier_when))
        lines.extend(_model_when_table(prefix, node.model_when))
        lines.extend(_subagent_tables(prefix, node.subagents))
        for slot in node.children.values():
            at = f"{prefix}.children.{_toml_key(slot.name)}"
            lines.extend([f"[{at}]", f"rounds = {_toml_value(slot.rounds)}", f"warm = {_toml_value(slot.warm)}"])
            if slot.roles:
                lines.append(f"roles = {_toml_value(slot.roles)}")
            if slot.step is not None:
                lines.append(f"step = {_toml_str(slot.step)}")
            single = slot.lanes.get(DEFAULT_LANE) if list(slot.lanes) == [DEFAULT_LANE] else None
            if single is not None:
                lines.extend(_lane_lines(single))
            lines.append("")
            if single is None:
                for lane in slot.lanes.values():
                    lines.extend([f"[{at}.lanes.{_toml_key(lane.name)}]", *_lane_lines(lane), ""])
            lines.extend(_subagent_tables(at, slot.subagents))
    for e in [*schema.edges, *schema.redo_edges, *schema.back_edges]:
        lines.append("[[edges]]")
        lines.append(f"from = {_toml_str(e.frm)}")
        lines.append(f"to = {_toml_str(e.to)}")
        if e.when:
            lines.append(f"when = {_toml_str(e.when)}")
        if e.trigger:
            lines.append(f"trigger = {_toml_str(e.trigger)}")
        if e.status:
            lines.append(f"status = {_toml_value(e.status)}")
        if e.fork:
            lines.append("fork = true")
        if e.join:
            lines.append("join = true")
        if e.redo:
            lines.append("redo = true")
        if e.back:
            lines.append("back = true")
        if e.exit_checks:
            lines.append(f"exit_checks = {_toml_value(e.exit_checks)}")
        lines.append("")
    return "\n".join(lines)


def _tier_when_table(prefix: str, rule: TierWhen | None) -> list[str]:
    if rule is None:
        return []
    lines = [f"[{prefix}.tier_when]", f"input = {_toml_str(rule.input)}", f"line = {_toml_str(rule.line)}",
             f"equals = {_toml_str(rule.equals)}", f"tier = {_toml_str(rule.tier)}"]
    if rule.unless is not None:
        lines.append(f"unless = {_toml_str(rule.unless)}")
    return [*lines, ""]


def _model_when_table(prefix: str, rule: ModelWhen | None) -> list[str]:
    if rule is None:
        return []
    lines = [f"[{prefix}.model_when]", f"input = {_toml_str(rule.input)}", f"line = {_toml_str(rule.line)}",
             f"equals = {_toml_str(rule.equals)}", f"model = {_toml_str(rule.model)}",
             f"effort = {_toml_str(rule.effort)}"]
    if rule.unless is not None:
        lines.append(f"unless = {_toml_str(rule.unless)}")
    return [*lines, ""]


def _subagent_tables(prefix: str, subagents: dict[str, Subagent]) -> list[str]:
    lines: list[str] = []
    for sub in subagents.values():
        lines.extend([f"[{prefix}.subagents.{_toml_key(sub.name)}]", f"type = {_toml_str(sub.type)}",
                      f"model = {_toml_str(sub.model)}"])
        if sub.effort is not None:
            lines.append(f"effort = {_toml_str(sub.effort)}")
        if sub.step is not None:
            lines.append(f"step = {_toml_str(sub.step)}")
        lines.append("")
    return lines


def schema_sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class Token:
    token_id: str
    node: str
    index: int
    holder: str | None
    cwd: str
    manager: str | None
    row: str | None = None
    forked: bool = False


@dataclass
class Fold:
    tokens: dict[str, Token]
    open_requests: list[dict]
    round: int = 1
    round_event: dict | None = None
    superseded: set[str] = field(default_factory=set)
    held: dict[str, str] = field(default_factory=dict)
    fork: dict | None = None
    closed: dict | None = None


@dataclass
class Caller:
    assignment_id: str | None
    ticket: str | None
    agent: str


@dataclass
class Facts:
    artifacts: dict[str, dict]
    plan_tasks: list[dict] | None
    command_runner: Callable[[list[str], str, int], int] | None
    read: Callable[[str], str] | None = None


@dataclass
class Verdict:
    ok: bool
    reason: str | None = None
    index_to: int | None = None
    edge: Edge | None = None


def _token_of(ev: dict, requests: dict[str, dict]) -> str:
    if ev.get("token_id"):
        return ev["token_id"]
    request = requests.get(ev.get("request_id"))
    return request["token_id"] if request is not None else DEFAULT_TOKEN_ID


def running_schema(events: list[dict], token_id: str, fallback):
    name = fallback
    for e in events:
        if ((e.get("token_id") or DEFAULT_TOKEN_ID) == token_id
                and e.get("type") in ("pipeline_started", "schema_snapshot") and isinstance(e.get("schema"), str)):
            name = e["schema"]
    return name


def fold(events: list[dict]) -> Fold:
    tokens: dict[str, Token] = {}
    held: dict[str, str] = {}
    requests: dict[str, dict] = {}
    committed: set[str] = set()
    superseded: set[str] = set()
    round_no, round_event = 1, None
    fork: dict | None = None
    closed: dict | None = None
    for ev in events:
        t = ev.get("type")
        if t not in RESERVED_EVENT_TYPES:
            continue
        if t == "lane_event":
            continue
        if t in _REQUEST_BOUND_EVENTS and ev.get("request_id") in superseded:
            continue
        tid = _token_of(ev, requests)
        if t == "pipeline_started":
            tokens[tid] = Token(tid, ev["node"], int(ev.get("index", 1)), ev.get("holder"), ev.get("cwd", ""), ev.get("manager"))
        elif t == "advance_committed":
            prev = tokens.get(tid)
            request = requests.get(ev.get("request_id")) or {}
            parent = tokens.get(DEFAULT_TOKEN_ID)
            cwd = request.get("cwd") or (prev.cwd if prev else ev.get("cwd", ""))
            manager = prev.manager if prev else (parent.manager if parent else ev.get("manager"))
            tokens[tid] = Token(tid, ev["node"], int(ev["index"]), ev.get("holder"), cwd, manager,
                                prev.row if prev else request.get("row"))
            committed.add(ev.get("request_id"))
            held.pop(tid, None)
            if tid == DEFAULT_TOKEN_ID:
                fork = None
        elif t == "token_back":
            prev = tokens.get(tid)
            rows = ev.get("rows") or []
            if prev is not None:
                tokens[tid] = Token(tid, ev.get("to", prev.node) if rows else prev.node, prev.index, None, prev.cwd,
                                    prev.manager, prev.row, bool(rows))
            held.pop(tid, None)
            if rows:
                fork = {"parent": tid, "node": ev.get("to"),
                        "rows": {r["row"]: {"stage": int(r.get("stage", 1)), "token_id": r["token_id"],
                                            "state": "pending" if r["row"] == ev.get("row") else "done"}
                                 for r in rows}}
        elif t == "token_override":
            prev = tokens.get(tid)
            tokens[tid] = Token(tid, ev["to"], int(ev["index"]), None,
                                prev.cwd if prev else ev.get("cwd", ""),
                                ev.get("manager") or (prev.manager if prev else None), prev.row if prev else None)
            held.pop(tid, None)
            family = f"{tid}/"
            for child in [k for k in tokens if k.startswith(family)]:
                del tokens[child]
                held.pop(child, None)
            superseded.update(rid for rid, r in requests.items()
                              if r["token_id"] == tid or r["token_id"].startswith(family))
            if fork is not None and fork["parent"] == tid:
                fork = None
        elif t == "token_forked":
            prev = tokens.get(tid)
            if prev is not None:
                tokens[tid] = Token(tid, ev.get("node", prev.node), prev.index, None, prev.cwd, prev.manager,
                                    prev.row, True)
            held.pop(tid, None)
            fork = {"parent": tid, "node": ev.get("node"),
                    "rows": {r["row"]: {"stage": int(r.get("stage", 1)), "token_id": r["token_id"], "state": "pending"}
                             for r in ev.get("rows") or []}}
        elif t == "token_retired":
            tokens.pop(tid, None)
            held.pop(tid, None)
            for r in (fork or {}).get("rows", {}).values():
                if r["token_id"] == tid:
                    r["state"] = "done"
        elif t == "advance_requested":
            requests[ev["request_id"]] = dict(ev, token_id=tid, pending=None)
            if ev.get("kind") in ("fork", "back") and fork is not None and ev.get("row") in fork["rows"]:
                fork["rows"][ev["row"]]["state"] = "started"
            if closed is not None:
                superseded.add(ev["request_id"])
        elif t == "advance_pending":
            if ev.get("request_id") in requests:
                requests[ev["request_id"]]["pending"] = ev
        elif t == "holder_restamped":
            tok = tokens.get(tid)
            if tok is not None:
                tok.holder = ev.get("new")
        elif t == "round_started":
            round_no, round_event, closed = int(ev.get("round") or round_no + 1), ev, None
        elif t == "token_held":
            if tid in tokens:
                held[tid] = str(ev.get("reason") or "")
        elif t == "token_released":
            held.pop(tid, None)
        elif t == "work_closed":
            closed = {"node": ev.get("node"), "reason": str(ev.get("reason") or ""), "ts": ev.get("ts"),
                      "actor_sid": ev.get("actor_sid")}
            held.clear()
            superseded.update(rid for rid in requests if rid not in committed)
    open_requests = [r for rid, r in requests.items() if rid not in committed and rid not in superseded]
    return Fold(tokens=tokens, open_requests=open_requests, round=round_no, round_event=round_event,
                superseded=set(superseded), held=dict(held), fork=fork, closed=closed)


def latest_hold(events: list[dict], token_id: str) -> dict | None:
    return next((e for e in reversed(events)
                 if e.get("type") == "token_held" and (e.get("token_id") or DEFAULT_TOKEN_ID) == token_id), None)


def _expand(name: str, index: int) -> str:
    return name.replace("{i}", str(index))


def line_word(text: str, key: str) -> str | None:
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(f"{key}:"):
            return re.split(r"[\s—–]", line[len(key) + 1:].strip(), maxsplit=1)[0].lower()
    return None


def _when_matches(rule: TierWhen | ModelWhen | EdgeWhen | None, facts: Facts, index: int, read: Callable[[str], str]) -> bool:
    if rule is None or (rule.unless is not None and _expand(rule.unless, index) in facts.artifacts):
        return False
    name = _expand(rule.input, index)
    if (facts.artifacts.get(name) or {}).get("status") != "complete":
        return False
    try:
        word = line_word(read(name), rule.line)
    except (OSError, ValueError, KeyError):
        return False
    return word == rule.equals.lower()


def node_tier(node: Node, base: str, facts: Facts, index: int, read: Callable[[str], str]) -> str:
    return node.tier_when.tier if _when_matches(node.tier_when, facts, index, read) else base


def node_model(node: Node, facts: Facts, index: int, read: Callable[[str], str]) -> tuple[str, str]:
    rule = node.model_when
    if _when_matches(rule, facts, index, read):
        return rule.model, rule.effort
    return node.model, node.effort


def expand_inputs(node: Node, index: int, indexes: list[int]) -> list[str]:
    out: list[str] = []
    for name in node.inputs:
        if "{i}" in name and not node.repeat:
            out.extend(_expand(name, i) for i in (indexes or [index]))
        else:
            out.append(_expand(name, index))
    return list(dict.fromkeys(out))


def last_index(node: Node, facts: Facts) -> int:
    if not node.repeat:
        return 1
    return len(facts.plan_tasks or [])


def check_advance(snapshot: Schema, token: Token, caller: Caller, to: str, facts: Facts,
                  open_requests: list[dict], cwd: str | None = None, timeout_sec: int = 600,
                  reason: str | None = None, row: str | None = None) -> Verdict:
    node = snapshot.nodes.get(token.node)
    if node is None:
        return Verdict(False, f"token sits on unknown node {token.node!r}; ask_manager")
    if caller.agent != "manager" and (caller.assignment_id is None or caller.assignment_id != token.holder):
        return Verdict(False, f"caller {caller.assignment_id!r} is not the holder {token.holder!r}")
    if to == "?":
        return Verdict(False, "next node unknown to the caller; ask_manager with the reason")
    redo = redo_edge(snapshot, token.node, to)
    if redo is not None:
        if caller.agent == "manager":
            return Verdict(False, f"{token.node} -> {to} is a redo edge: a redo edge is the holder's; the manager "
                                  "reworks")
        if not (isinstance(reason, str) and reason.strip()):
            return Verdict(False, f"redo edge {token.node} -> {to} needs reason: the finding, its link, the decision "
                                  "it breaks")
        for r in open_requests:
            if r["token_id"] == token.token_id:
                return Verdict(False, f"advance in flight (request {r.get('request_id')}); wait for reconciliation")
        return Verdict(True, None, 1, redo)
    back = back_edge(snapshot, token.node, to)
    if back is not None:
        return _back_verdict(snapshot, token, caller, to, facts, open_requests, reason, row, back)
    indexes = token_indexes(facts, token) if node.repeat else [1]
    last = indexes[-1] if indexes else 0
    if node.repeat and token.index not in indexes:
        return Verdict(False, f"index {token.index} is past the task list the engine read from {node.repeat} "
                              f"({len(facts.plan_tasks or [])} readable tasks); ask_manager")
    if caller.agent == "manager" and not any(e.trigger for e in snapshot.edges if e.frm == token.node and e.to == to):
        return Verdict(False, f"a manager moves only trigger edges without override; {token.node} -> {to} is not one")
    for r in open_requests:
        if r["token_id"] == token.token_id:
            return Verdict(False, f"advance in flight (request {r.get('request_id')}); wait for reconciliation")
    edges = [e for e in snapshot.edges if e.frm == token.node and e.to == to]
    if not edges:
        outs = list(dict.fromkeys(e.to for e in snapshot.edges if e.frm == token.node))
        redo_outs = [e.to for e in snapshot.redo_edges if e.frm == token.node]
        return Verdict(False, f"no edge {token.node} -> {to} in the schema snapshot; "
                              f"edges out of {token.node}: {', '.join(outs) or 'none'}"
                              + (f"; redo edges: {', '.join(redo_outs)}" if redo_outs else ""))
    edge = edges[0]
    if edge.trigger and caller.agent != "manager":
        return Verdict(False, f"edge {token.node} -> {to} is a {edge.trigger} trigger; only the manager fires it")
    if token.row is not None and edge.to != token.node and not edge.join:
        return Verdict(False, f"a row leaves {token.node} only by its join edge; {token.node} -> {to} is not one")
    if edge.to == token.node:
        later = [i for i in indexes if i > token.index]
        if not later:
            return Verdict(False, f"index {token.index} is the last of {last}; take the edge marked when=\"last\"")
        index_to = later[0]
    else:
        if edge.when == "last" and token.index != last:
            return Verdict(False, f"edge {token.node} -> {to} needs the last index ({last}); token is at {token.index}")
        index_to = 1
    if not edge.trigger:
        for out in node.outputs:
            name = _expand(out, token.index)
            stamp = facts.artifacts.get(name)
            if stamp is None or stamp.get("status") != "complete":
                return Verdict(False, f"declared output {name} is not complete")
            if (stamp.get("scope") or "internal") != "output":
                return Verdict(False, f"declared output {name} has scope {stamp.get('scope') or 'internal'}, expected output")
        if _publishes_an_unreadable_task_list(snapshot, node, facts):
            return Verdict(False, _TASK_LIST_UNREADABLE)
    for check in edge.exit_checks:
        ok, why = exit_check(check, facts, cwd or token.cwd, timeout_sec, index=token.index)
        if not ok:
            return Verdict(False, f"exit_check failed: {why}")
    return Verdict(True, None, index_to, edge)


def back_rows(snapshot: Schema, to: str, tasks: list[dict] | None) -> list[dict]:
    rows = task_rows(tasks)
    return rows if len(rows) > 1 and fork_source(snapshot, to) is not None else []


def _back_verdict(snapshot: Schema, token: Token, caller: Caller, to: str, facts: Facts, open_requests: list[dict],
                  reason: str | None, row: str | None, edge: Edge) -> Verdict:
    if caller.agent == "manager":
        return Verdict(False, f"{token.node} -> {to} is a back edge: a back edge is the holder's")
    if not (isinstance(reason, str) and reason.strip()):
        return Verdict(False, f"back edge {token.node} -> {to} needs reason: what failed and its evidence")
    for r in open_requests:
        if r["token_id"] == token.token_id:
            return Verdict(False, f"advance in flight (request {r.get('request_id')}); wait for reconciliation")
    target = snapshot.nodes[to]
    if target.repeat and not facts.plan_tasks:
        return Verdict(False, _TASK_LIST_UNREADABLE)
    rows = back_rows(snapshot, to, facts.plan_tasks)
    if not rows:
        return Verdict(True, None, last_index(target, facts), edge)
    names = [r["row"] for r in rows]
    if row is None:
        return Verdict(False, f"back edge {token.node} -> {to} needs row: one of {', '.join(names)}")
    if row not in names:
        return Verdict(False, f"row {row!r} is not a row of plan.tasks; its rows are {', '.join(names)}")
    return Verdict(True, None, next(r["indexes"][-1] for r in rows if r["row"] == row), edge)


def read_task_list(body: str) -> list[dict]:
    try:
        data = json.loads(body)
    except (TypeError, ValueError, RecursionError):
        return []
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list) or not tasks or not all(isinstance(t, dict) for t in tasks):
        return []
    return tasks if _rows_readable(tasks) else []


def _rows_readable(tasks: list[dict]) -> bool:
    tagged = ["row" in t for t in tasks]
    if any(tagged) and not all(tagged):
        return False
    stages: dict = {}
    for t in tasks:
        row, stage = t.get("row"), t.get("stage", 1)
        if "row" in t and not (isinstance(row, str) and row):
            return False
        if not (isinstance(stage, int) and not isinstance(stage, bool) and stage >= 1):
            return False
        if stages.setdefault(row, stage) != stage:
            return False
    return True


def task_rows(tasks: list[dict] | None) -> list[dict]:
    rows: dict = {}
    for i, t in enumerate(tasks or [], 1):
        entry = rows.setdefault(t.get("row"), {"row": t.get("row"), "stage": t.get("stage", 1), "indexes": []})
        entry["indexes"].append(i)
    return list(rows.values())


def token_indexes(facts: Facts, token: Token) -> list[int]:
    if token.row is None:
        return list(range(1, len(facts.plan_tasks or []) + 1))
    return next((r["indexes"] for r in task_rows(facts.plan_tasks) if r["row"] == token.row), [])


def row_token_id(parent: str, row: str) -> str:
    return f"{parent}/{row}"


def fork_source(snap: Schema, to: str) -> str | None:
    return next((e.frm for e in snap.edges if e.fork and e.to == to and e.frm != e.to), None)


def fork_entry_problem(snap: Schema, to: str, tasks: list[dict] | None) -> str | None:
    source = fork_source(snap, to)
    rows = task_rows(tasks)
    if source is None or len(rows) < 2:
        return None
    return f"{to} is entered by a fork edge and plan.tasks has {len(rows)} rows: enter at {source} or earlier"


def _publishes_an_unreadable_task_list(snapshot: Schema, node: Node, facts: Facts) -> bool:
    return (REPEAT_LIST in node.outputs and not facts.plan_tasks
            and any(n.repeat == REPEAT_LIST for n in snapshot.nodes.values()))


def exit_check(check: dict, facts: Facts, cwd: str, timeout_sec: int, index: int = 1) -> tuple[bool, str]:
    problem = _exit_check_problem(check)
    if problem is not None:
        return False, problem
    kind = check["type"]
    if kind == "artifact":
        name = _expand(check["name"], index)
        stamp = facts.artifacts.get(name)
        ok = stamp is not None and stamp.get("status") == "complete"
        return ok, ("" if ok else f"artifact {name} is not complete")
    if kind == "value":
        name = _expand(check["name"], index)
        stamp = facts.artifacts.get(name) or {}
        actual = stamp.get(check["field"])
        ok = type(actual) is type(check["equals"]) and actual == check["equals"]
        return ok, ("" if ok else f"{name}.{check['field']} is {actual!r}, expected {check['equals']!r}")
    if kind == "when":
        return _when_check(check, facts, index)
    runner = facts.command_runner or _run_command
    argv = check["run"] if isinstance(check["run"], list) else ["sh", "-c", check["run"]]
    timeout = check.get("timeout_sec", timeout_sec)
    rc = runner(argv, cwd, timeout)
    if rc == 0:
        return True, ""
    if rc == _CANNOT_START:
        return False, f"command {argv} could not start in {cwd} (missing binary or directory, or not executable)"
    if rc == _TIMED_OUT:
        return False, f"command {argv} timed out after {timeout} s"
    return False, f"command {argv} exited {rc}"


def _when_check(check: dict, facts: Facts, index: int) -> tuple[bool, str]:
    rule = EdgeWhen(check["input"], check["line"], check["equals"], check.get("unless"))
    name = _expand(rule.input, index)
    if rule.unless is not None and _expand(rule.unless, index) in facts.artifacts:
        return False, f"{_expand(rule.unless, index)} exists"
    if (facts.artifacts.get(name) or {}).get("status") != "complete":
        return False, f"artifact {name} is not complete"
    if facts.read is None:
        return False, f"{name} cannot be read here"
    if _when_matches(rule, facts, index, facts.read):
        return True, ""
    return False, f"{name} has no `{rule.line}: {rule.equals}` line"


def _run_command(argv: list[str], cwd: str, timeout: int) -> int:
    try:
        return subprocess.run(argv, cwd=cwd, timeout=timeout, capture_output=True).returncode
    except subprocess.TimeoutExpired:
        return _TIMED_OUT
    except (OSError, ValueError, OverflowError):
        return _CANNOT_START


@dataclass(frozen=True)
class InputCopy:
    path: str | None
    written_at: float | None = None
    contract_hash: str | None = None
    status: str | None = None


_INPUTS_PREFIX = ("Inputs (read ONLY these. Read the copy after each name for your first read of it; call "
                  "artifact_get(task_key, phase, name, reader_sid=<your sid>) for a name with no copy, a copy that is "
                  "gone, and every later read. A read_set entry takes the written_at and contract_hash shown): ")


def listed_inputs(inputs: list[str], decisions: list[str], round_inputs: list[str] | None = None) -> list[str]:
    extra = [n for n in dict.fromkeys(round_inputs or []) if n not in inputs and n not in decisions]
    return [*inputs, *extra, *decisions]


def _input_entry(name: str, copy: InputCopy | None) -> str:
    if copy is None:
        return f"{name} (not written)"
    if not copy.path:
        return f"{name} (no copy)"
    entry = f"{name} = {copy.path} written_at={copy.written_at}"
    if copy.contract_hash:
        entry += f" contract_hash={copy.contract_hash}"
    if copy.status and copy.status != "complete":
        entry += f" status={copy.status}"
    return entry


def _tools_line(node: Node, edges_out: list[Edge], redo_out: list[Edge] | None,
                back_out: list[Edge] | None) -> str:
    names = ["artifact_get", "artifact_put", "worker_done"]
    if holder_advances(edges_out) or redo_out or back_out:
        names.append("pipeline_advance")
    if node.children:
        names.extend(["pipeline_child", "wait_for_child"])
    return ('Tools: in your first message, beside your first Reads, call ToolSearch(query="select:'
            + ",".join(TOOL_PREFIX + n for n in names) + f'", max_results={len(names)}). '
            "If it answers that a server is still connecting, call it again beside your next calls.")


def compose_brief(node: Node, token: Token, task_key: str, inputs: list[str], checkpoints: list[str],
                  decisions: list[str], edges_out: list[Edge], round_no: int = 1,
                  round_inputs: list[str] | None = None, rulings: tuple[str, str] | None = None,
                  redo_out: list[Edge] | None = None, round_from: str | None = None,
                  round_rework_ask: str | None = None, back_out: list[Edge] | None = None,
                  back_rows: dict[str, list[str]] | None = None,
                  copies: dict[str, InputCopy] | None = None) -> str:
    outputs = [_expand(o, token.index) for o in node.outputs]
    listed = listed_inputs(inputs, decisions, round_inputs)
    values = {"task_key": task_key, "cwd": token.cwd, "node": node.name, "i": str(token.index),
              "inputs": ", ".join(listed) or "(none)",
              "checkpoints": ", ".join(checkpoints) or "(none)", "round": str(round_no)}
    body = _PLACEHOLDER.sub(lambda m: values[m.group(1)], node.brief_text or "")
    targets = ", ".join(f"{e.to} ({'trigger: ' + e.trigger if e.trigger else 'when=' + e.when if e.when else 'next'})" for e in edges_out)
    header = f"[pipeline] task_key={task_key} node={node.name} index={token.index} cwd={token.cwd}"
    lines = ["", "---", header + (f" round={round_no}" if round_no > 1 else "")]
    rules = _node_rules()
    if rules:
        lines.append(f"Read {rules} before anything else.")
    if round_no > 1:
        lines.extend(_round_lines(round_no, token.cwd, round_from, round_rework_ask))
    lines.append(_tools_line(node, edges_out, redo_out, back_out))
    lines.append(_INPUTS_PREFIX + ("; ".join(_input_entry(n, (copies or {}).get(n)) for n in listed) or "(none)"))
    if rulings:
        lines.append(_rulings_line(*rulings))
    lines.extend([
        "Outputs you must publish with artifact_put(..., scope=\"output\", status=\"complete\"), the puts you have "
        "ready in one message, one call each: " + (", ".join(outputs) or "(none)"),
        "Checkpoints (scope=\"checkpoint\"): " + (", ".join(_expand(c, token.index) for c in node.checkpoints) or "(none)"),
        f"Edges out of this node: {targets or '(terminal node)'}",
    ])
    if redo_out:
        lines.append("Redo edges: " + ", ".join(e.to for e in redo_out))
    if back_out:
        lines.append("Back edges: " + ", ".join(_back_target(e.to, (back_rows or {}).get(e.to) or [])
                                                 for e in back_out))
    lines.extend([_decision_line(rules), _summary_line(rules), _last_action(edges_out, redo_out, back_out)])
    return body + "\n".join(lines)


def _back_target(to: str, rows: list[str]) -> str:
    return f"{to} (rows: {', '.join(rows)})" if len(rows) > 1 else to


def holder_advances(edges_out: list[Edge]) -> bool:
    return any(not e.trigger for e in edges_out)


def _last_action(edges_out: list[Edge], redo_out: list[Edge] | None = None,
                 back_out: list[Edge] | None = None) -> str:
    tail = (_redo_action(redo_out) if redo_out else "") + (_back_action() if back_out else "")
    if holder_advances(edges_out):
        return ("Last action before worker_done: pipeline_advance(claude_sid=<your sid>, task_key=..., to=<next node>). "
                "If the next step is unclear, call pipeline_advance(to=\"?\", reason=...) and then ask_manager." + tail)
    if edges_out:
        kinds = " or ".join(k for k, out in (("redo", redo_out), ("back", back_out)) if out)
        stop = f"call pipeline_advance only along a {kinds} edge." if kinds else "do not call pipeline_advance."
        if all(e.trigger == "status" for e in edges_out):
            moves = " ".join(f"The engine moves the token to {e.to} once the ticket's status is one of "
                             f"{', '.join(e.status)}." for e in edges_out)
            return f"{moves} Publish your outputs, then call worker_done; {stop}" + tail
        moves = ", ".join(f"to {e.to} when the ticket's status is one of {', '.join(e.status)}"
                          if e.trigger == "status" else f"to {e.to} when {e.trigger} fires" for e in edges_out)
        return f"The manager moves the token {moves}. Publish your outputs, then call worker_done; {stop}" + tail
    return "This is a terminal node: publish your outputs, then call worker_done." + tail


def _redo_action(redo_out: list[Edge]) -> str:
    targets = " or ".join(f'"{e.to}"' for e in redo_out)
    return (" If your brief's condition for going back holds, ask_manager first with a first line `Redesign request` "
            "and change nothing until the answer. Only after an answer whose first line is `redesign: yes`, call "
            f"pipeline_advance(claude_sid=<your sid>, task_key=..., to={targets}, reason=<the finding, its link, the "
            "decision it breaks>) as your last call before worker_done.")


def back_cap() -> int:
    return config.pipeline_int("back_cap", 10)


def _back_action() -> str:
    return (" To send the work back to a repo row: pipeline_advance(claude_sid=<your sid>, task_key=..., to=<node>, "
            "row=<row>, reason=<what failed and its evidence>). At most "
            f"{back_cap()} returns per row per round; past that, ask_manager.")


def _node_rules() -> Path | None:
    rules = config.pipeline_node_rules()
    if rules is None:
        return None
    try:
        headings = {line.strip() for line in rules.read_text(encoding="utf-8").splitlines()}
    except (OSError, UnicodeDecodeError):
        return None
    return rules if {f"## {DECISION_SECTION}", f"## {SUMMARY_SECTION}"} <= headings else None


def _decision_line(rules: Path | None = None) -> str:
    if rules:
        return ("If you publish a decision.* record, pass decided_by to artifact_put and rank decisions as "
                f"{rules} § {DECISION_SECTION} says.")
    return ("If you publish a decision.* record, pass decided_by to artifact_put: it names the channel the decision "
            "reached you through. Pass \"manager\" for anything that came in a [MANAGER] message, even when it quotes "
            "the engineer or relays their gate verdict. Pass \"engineer\" only for a message typed into your pane "
            "without a marker, for a [TOPIC … from engineer] line, or for a gate verdict you read from the gate "
            "yourself or from a decision.verdict-r<n> record. A [TOPIC <record> from <who>] line in your pane is "
            "already recorded as <record>: read <record>, act on the record, and publish no decision.pane-* for it; "
            "a [TOPIC … from engineer] message gets a reply. record before you go on: artifact_put(task_key, "
            "phase=\"reply\", name=<UTC YYYYMMDDTHHMMSS>, scope=\"output\", status=\"complete\"), its body's first "
            "line re: <record>, then what you did or will do in at most 15 lines; a verdict line, a from manager "
            "line, and a decision.msg-* starting with author-wait:, lease:, trigger: or event: get no reply; "
            "if <record> does not exist, ask_manager. Decisions rank in three tiers, and "
            "written_at never ranks across tiers: (1) the work's records not marked as the manager's, the latest "
            "written_at winning among them; (2) the domain rulings registry, binding while no tier-1 record speaks "
            "to the same point; (3) records marked as the manager's (decided_by: manager, or a \"from: manager\" "
            "line in the body), applying where tiers 1 and 2 are silent, the latest winning among them. A tier-3 "
            "record that contradicts tier 1 or 2 is not yours to settle: ask_manager, naming both records; the "
            "decision.q-* answer to that ask settles the point over the records its question names. A "
            "decision.msg-* record whose text starts with author-wait:, lease:, trigger: or event: is an automated wake, "
            "never a decision: "
            "it ranks in no tier.")


def _rulings_line(fleet_key: str, domain: str) -> str:
    return (f"Domain rulings (an input; tier 2 of the decision tiers below): "
            f"artifact_get(task_key=\"{fleet_key}\", phase=\"{topic.DECISIONS_PHASE}\", name=\"{domain}\", "
            "reader_sid=<your sid>) — the one store read whose task_key is not this work's.")


def _summary_line(rules: Path | None = None) -> str:
    if rules:
        return ("Your last store write before you leave the node is the work's running summary, "
                f"{SUMMARY_PHASE}.{SUMMARY_NAME}, written as {rules} § {SUMMARY_SECTION} says.")
    return (f"Your last store write before you leave the node — before pipeline_advance, or before worker_done when "
            f"you do not call it, a park included — is the work's running summary: artifact_get(task_key, "
            f"\"{SUMMARY_PHASE}\", \"{SUMMARY_NAME}\", reader_sid=<your sid>) if one exists, fold it into yours (an "
            f"attempt your work redoes or replaces goes under item 2, set aside), then write yours to a file and "
            f"artifact_put(task_key, phase=\"{SUMMARY_PHASE}\", name=\"{SUMMARY_NAME}\", content_path=<that file, at "
            f"most 15 lines>, status=\"complete\", scope=\"output\", overwrite=True, writer_sid=<your sid>). Write it "
            f"so the work can continue without redoing work or re-asking for constraints; preserve: "
            f"{preserve_clause()}. It may be the last call of the message that puts your final records.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dockwright pipeline",
                                     description="Check pipeline schemas and print a work's status.")
    sub = parser.add_subparsers(dest="command", required=True)
    val = sub.add_parser("validate", help="Print every problem in a schema; exit 0 when it has none.")
    val.add_argument("schema", help="Schema name under [pipeline] schemas_dir, or a path to a .toml file.")
    val.add_argument("--cwd", default=None, help="The directory the schema is checked for (default: the current directory).")
    val.add_argument("--roles-dir", default=None,
                     help="Directory of role manifests to check against (default: [paths] roles_dir).")
    status = sub.add_parser("status", help="Print a work's pipeline status; --json prints one JSON object.")
    status.add_argument("task_key", help="The work's task key.")
    status.add_argument("--json", action="store_true", help="Print the machine form (version 1).")
    active = sub.add_parser("active-paths",
                            help="Print every unfinished work and open note, lane or fleet card with the "
                                 "paths it claims; exit 1, printing no list, when any record cannot be read.")
    active.add_argument("--json", action="store_true", help="Print the machine form (version 1).")
    args = parser.parse_args(argv)
    if args.command == "status":
        return _status_main(args.task_key, args.json)
    if args.command == "active-paths":
        return _active_paths_main(args.json)
    path = Path(args.schema)
    if not path.suffix:
        path = config.pipeline_schemas_dir() / f"{args.schema}.toml"
    if not path.is_file():
        print(f"no schema file at {path}")
        return 2
    try:
        schema = load_schema(path)
    except (OSError, ValueError) as err:
        print(f"{path}: {err}")
        return 1
    from . import roles
    manifests = None
    if args.roles_dir is not None:
        try:
            manifests = roles.load_manifests(Path(args.roles_dir))
        except roles.RoleError as err:
            print(f"{args.roles_dir}: {err}")
            return 1
    problems = validate(schema, args.cwd, manifests=manifests)
    for line in problems:
        print(line)
    print("ok" if not problems else f"{len(problems)} problem(s)")
    return 0 if not problems else 1


def _step(step: str | None) -> str:
    return f"  ({step})" if step else ""


def _exit_label(check: dict) -> str:
    target = check.get("name") or check.get("run") or ""
    return f"exit: {check.get('type')} {' '.join(target) if isinstance(target, list) else target}"


def show(schema: Schema) -> str:
    lines = [f"schema {schema.name} v{schema.version}"]
    if schema.rework_at:
        lines.append(f"  rework_at: {schema.rework_at}")
    lines.append("nodes:")
    for name, node in schema.nodes.items():
        lines.append(f"  {name}  role={node.role}" + (f" tier={node.tier}" if node.tier else "")
                     + (f" model={node.model}" if node.model is not None else "")
                     + (f" effort={node.effort}" if node.effort is not None else "")
                     + (f" repeat={node.repeat}" if node.repeat else ""))
        if node.tier_when is not None:
            rule = node.tier_when
            lines.append(f"    tier_when={rule.input}:{rule.line}={rule.equals}"
                         + (f" unless {rule.unless}" if rule.unless is not None else "") + f" -> {rule.tier}")
        if node.model_when is not None:
            rule = node.model_when
            lines.append(f"    model_when={rule.input}:{rule.line}={rule.equals}"
                         + (f" unless {rule.unless}" if rule.unless is not None else "")
                         + f" -> {rule.model}/{rule.effort}")
        for slot in node.children.values():
            lanes = ", ".join(f"{lane.name}:{lane.role}/{lane.mode}/{lane.model}/{lane.effort}"
                              + (f"+codex {lane.codex}" if lane.codex else "") for lane in slot.lanes.values())
            lines.append(f"    child {slot.name}: lanes={lanes or ', '.join(slot.roles)}  warm={_toml_value(slot.warm)} "
                         f"rounds={slot.rounds}{_step(slot.step)}")
            lines.extend(f"      subagent {sub.name}: {sub.entry}{_step(sub.step)}" for sub in slot.subagents.values())
        lines.extend(f"    subagent {sub.name}: {sub.entry}{_step(sub.step)}" for sub in node.subagents.values())
    lines.append("edges:")
    for e in [*schema.edges, *schema.redo_edges, *schema.back_edges]:
        flags = [flag for flag, on in (("fork", e.fork), ("join", e.join), ("redo", e.redo), ("back", e.back)) if on]
        flags += [f"{key}={value}" for key, value in (("when", e.when), ("trigger", e.trigger)) if value]
        if e.status:
            flags.append(f"status=[{', '.join(e.status)}]")
        flags += [_exit_label(c) for c in e.exit_checks]
        lines.append(f"  {e.frm} -> {e.to}" + "".join(f"  {flag}" for flag in flags))
    return "\n".join(lines)


def schema_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dockwright schema", description="Show a pipeline schema.")
    sub = parser.add_subparsers(dest="command", required=True)
    shown = sub.add_parser("show", help="Print the schema's nodes, children, subagents and edges.")
    shown.add_argument("schema", help="Schema name under [pipeline] schemas_dir, or a path to a .toml file.")
    args = parser.parse_args(argv)
    path = Path(args.schema)
    if not path.suffix:
        path = config.pipeline_schemas_dir() / f"{args.schema}.toml"
    if not path.is_file():
        print(f"no schema file at {path}")
        return 2
    try:
        schema = load_schema(path)
    except (OSError, ValueError) as err:
        print(f"{path}: {err}")
        return 1
    print(show(schema))
    return 0


def _status_main(task_key: str, as_json: bool) -> int:
    if not task_key.strip():
        print("empty task key")
        return 2
    from . import mcp_server
    print(mcp_server.pipeline_status_impl(task_key.strip(), "json" if as_json else "text"))
    return 0


def _active_paths_main(as_json: bool) -> int:
    from . import work_paths
    try:
        works = work_paths.open_work_paths()
    except Exception as e:
        print(f"active-paths: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps({"version": 1, "works": works}, sort_keys=True))
        return 0
    for w in works:
        print(f"{w['task_key']}  {' '.join(w['paths']) or '-'}")
    return 0
