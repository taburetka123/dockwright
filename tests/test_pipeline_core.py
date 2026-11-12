import hashlib
import os
import tomllib
from pathlib import Path

import pytest

from dockwright import compaction, config, pipeline

SCHEMA = '''
schema = "code-ticket"
version = 1

[nodes.design]
role = "SCOUT_DESIGN"
model = "@opus"
effort = "xhigh"
gate = true
brief = "briefs/design.md"
inputs = []
outputs = ["context.ask", "design.hld", "plan.tasks"]
checkpoints = ["design.explainer"]
extra_args = []

[nodes.task]
role = "IMPL_HARD"
model = "@opus"
effort = "xhigh"
repeat = "plan.tasks"
brief = "briefs/task.md"
inputs = ["plan.tasks", "design.hld"]
outputs = ["review.task-{i}"]

[nodes.pr]
role = "IMPL_HARD"
model = "@opus"
effort = "xhigh"
gate = true
brief = "briefs/pr.md"
inputs = ["plan.tasks"]
outputs = ["implement.repo"]

[nodes.post-merge]
role = "SCOUT"
model = "@sonnet"
effort = "xhigh"
brief = "briefs/post-merge.md"
inputs = ["implement.repo"]
outputs = ["summary.post-merge"]

[[edges]]
from = "design"
to = "task"
exit_checks = [{ type = "artifact", name = "plan.tasks" }]
[[edges]]
from = "task"
to = "task"
[[edges]]
from = "task"
to = "pr"
when = "last"
[[edges]]
from = "pr"
to = "post-merge"
trigger = "merge"
'''

@pytest.fixture
def schema_dir(tmp_path):
    d = tmp_path / "schemas"
    (d / "briefs").mkdir(parents=True)
    (d / "code-ticket.toml").write_text(SCHEMA)
    for n in ("design", "task", "pr", "post-merge"):
        (d / "briefs" / f"{n}.md").write_text(f"# {n} brief for {{task_key}} at {{cwd}} inputs {{inputs}}\n")
    return d


def test_load_schema_reads_nodes_and_edges(schema_dir):
    s = pipeline.load_schema(schema_dir / "code-ticket.toml")
    assert s.name == "code-ticket" and set(s.nodes) == {"design", "task", "pr", "post-merge"}
    assert s.nodes["task"].repeat == "plan.tasks" and s.nodes["design"].gate is True
    assert [(e.frm, e.to) for e in s.edges] == [("design", "task"), ("task", "task"), ("task", "pr"), ("pr", "post-merge")]
    assert s.edges[2].when == "last" and s.edges[3].trigger == "merge"


def test_validate_accepts_the_example(schema_dir, tmp_path):
    s = pipeline.load_schema(schema_dir / "code-ticket.toml")
    assert pipeline.validate(s, str(tmp_path)) == []


@pytest.mark.parametrize("edit,replacement,expected", [
    ('model = "@opus"\neffort = "xhigh"\ngate = true', 'gate = true', "names no model"),
    ('[nodes.task]\nrole = "IMPL_HARD"', '[nodes.task]\nrole = "IMPL_HARD"\ntier = "X"', "names tier"),
    ('inputs = ["plan.tasks", "design.hld"]', 'inputs = ["plan.tasks", "design.nowhere"]', "design.nowhere"),
    ('when = "last"', 'when = "first"', "first"),
    ('trigger = "merge"', 'trigger = "deploy"', "deploy"),
    ('type = "artifact"', 'type = "magic"', "magic"),
])
def test_validate_names_each_defect(schema_dir, tmp_path, edit, replacement, expected):
    text = SCHEMA.replace(edit, replacement)
    (schema_dir / "bad.toml").write_text(text)
    s = pipeline.load_schema(schema_dir / "bad.toml")
    problems = pipeline.validate(s, str(tmp_path))
    assert problems and any(expected in p for p in problems)


def test_an_input_may_name_an_upstream_checkpoint(schema_dir, tmp_path):
    ok = SCHEMA.replace('inputs = ["implement.repo"]', 'inputs = ["implement.repo", "design.explainer"]')
    (schema_dir / "ok.toml").write_text(ok)
    assert pipeline.validate(pipeline.load_schema(schema_dir / "ok.toml"), str(tmp_path)) == []
    bad = SCHEMA.replace('inputs = ["implement.repo"]', 'inputs = ["implement.repo", "nobody.wrote"]')
    (schema_dir / "bad.toml").write_text(bad)
    problems = pipeline.validate(pipeline.load_schema(schema_dir / "bad.toml"), str(tmp_path))
    assert problems == ["[nodes.post-merge] input 'nobody.wrote' is not an output or checkpoint of any upstream node "
                        "(a node with an edge path to this one)"]


APPROVE = SCHEMA.replace('trigger = "merge"', 'trigger = "approve"').replace(
    'outputs = ["summary.post-merge"]', 'outputs = ["summary.post-merge"]\ncheckpoints = ["merge.state"]')


def test_approve_is_a_trigger_value(schema_dir, tmp_path):
    (schema_dir / "ok.toml").write_text(APPROVE)
    assert pipeline.validate(pipeline.load_schema(schema_dir / "ok.toml"), str(tmp_path)) == []


def test_an_approve_edge_needs_the_merge_state_checkpoint_on_its_target(schema_dir, tmp_path):
    (schema_dir / "bad.toml").write_text(SCHEMA.replace('trigger = "merge"', 'trigger = "approve"'))
    problems = pipeline.validate(pipeline.load_schema(schema_dir / "bad.toml"), str(tmp_path))
    assert [p for p in problems if "approve" in p] == [
        '[[edges]] pr -> post-merge: trigger "approve" needs checkpoint "merge.state" on [nodes.post-merge]']


STATUS = SCHEMA.replace('trigger = "merge"', 'trigger = "status"\nstatus = ["Verified", "category:done"]')


def test_status_is_a_trigger_value_with_a_status_list(schema_dir, tmp_path):
    (schema_dir / "ok.toml").write_text(STATUS)
    s = pipeline.load_schema(schema_dir / "ok.toml")
    assert pipeline.validate(s, str(tmp_path)) == []
    assert [e.status for e in s.edges if e.trigger == "status"] == [["Verified", "category:done"]]


@pytest.mark.parametrize("edit,replacement,expected", [
    ('trigger = "merge"', 'trigger = "status"', 'trigger "status" needs a non-empty `status` list'),
    ('trigger = "merge"', 'trigger = "status"\nstatus = []', 'trigger "status" needs a non-empty `status` list'),
    ('trigger = "merge"', 'status = ["Verified"]', '`status` needs trigger = "status"'),
    ('trigger = "merge"', 'trigger = "status"\nstatus = "Verified"', "`status` must be a list of strings"),
    ('trigger = "merge"', 'trigger = "status"\nstatus = ["category:Done"]',
     "category 'Done' is not one of ['new', 'indeterminate', 'done']"),
    ('[[edges]]\nfrom = "pr"\nto = "post-merge"\ntrigger = "merge"',
     '[[edges]]\nfrom = "pr"\nto = "post-merge"\ntrigger = "status"\nstatus = ["Verified"]\n'
     '[[edges]]\nfrom = "pr"\nto = "design"\ntrigger = "status"\nstatus = ["Done"]',
     "[nodes.pr] has two status edges out; one node polls one status list"),
    ('[[edges]]\nfrom = "pr"\nto = "post-merge"\ntrigger = "merge"',
     '[[edges]]\nfrom = "pr"\nto = "post-merge"\ntrigger = "merge"\n'
     '[[edges]]\nfrom = "pr"\nto = "design"\ntrigger = "status"\nstatus = ["Done"]',
     "[nodes.pr] mixes a status edge with a merge edge; a node takes one trigger kind"),
    ('trigger = "merge"', 'trigger = "status"\nstatus = ["Verified", ""]', "`status` holds an empty entry"),
    ('[[edges]]\nfrom = "pr"\nto = "post-merge"\ntrigger = "merge"',
     '[[edges]]\nfrom = "pr"\nto = "post-merge"\ntrigger = "merge"\n'
     '[[edges]]\nfrom = "pr"\nto = "design"\nredo = true\nstatus = ["Verified"]',
     "a redo edge cannot carry `status`"),
])
def test_validate_names_each_status_edge_defect(schema_dir, tmp_path, edit, replacement, expected):
    (schema_dir / "bad.toml").write_text(SCHEMA.replace(edit, replacement))
    problems = pipeline.validate(pipeline.load_schema(schema_dir / "bad.toml"), str(tmp_path))
    assert problems and any(expected in p for p in problems)


def test_the_snapshot_and_schema_show_carry_the_status_list(schema_dir, tmp_path):
    (schema_dir / "ok.toml").write_text(STATUS)
    s = pipeline.load_schema(schema_dir / "ok.toml")
    text = pipeline.snapshot(s)
    assert 'trigger = "status"\nstatus = ["Verified", "category:done"]' in text
    assert [e.status for e in pipeline.load_snapshot(text).edges if e.trigger == "status"] == [["Verified", "category:done"]]
    assert "  pr -> post-merge  trigger=status  status=[Verified, category:done]" in pipeline.show(s)


def _manifest(name, launch=("worker",)):
    from dockwright import roles
    return roles.Manifest(name, name, (), (), tuple(launch), {}, (name,))


MODELED = 'role = "Designer"\nmodel = "@fable"\neffort = "medium"'
MANIFESTS = {"Designer": _manifest("Designer"), "GenericWorker": _manifest("GenericWorker")}


DESIGN_MODELED = 'role = "SCOUT_DESIGN"\nmodel = "@opus"\neffort = "xhigh"'


def _modeled(design=MODELED):
    assert DESIGN_MODELED in SCHEMA and "extra_args = []\n" in SCHEMA
    text = SCHEMA.replace(DESIGN_MODELED, design, 1)
    return text.replace("extra_args = []\n", "", 1) if "extra_args" in design else text


@pytest.mark.parametrize("design,expected", [
    (MODELED + '\ntier = "SCOUT_DESIGN"', "[nodes.design] names both model and tier; tier is deprecated, keep model"),
    ('role = "Designer"\nmodel = "@fable"', "[nodes.design] names model but no effort"),
    ('role = "Designer"\nmodel = "@fable"\neffort = "extra"', "effort 'extra' is not one of low, medium, high, xhigh, max"),
    ('role = "Designer"\neffort = "medium"\ntier = "SCOUT_DESIGN"', "[nodes.design] names effort without model"),
    ('role = "Designer"\nmodel = "@nosuch"\neffort = "medium"', "model '@nosuch' does not resolve"),
    ('role = "Designer"\nmodel = "opus"\neffort = "medium"', "model 'opus' is neither a @<family> token"),
    ('role = "Designer"\nmodel = ""\neffort = "medium"', "model '' is empty"),
    (MODELED + '\nextra_args = ["--effort", "high"]', "extra_args carries '--effort'; a node that names model"),
    (MODELED + '\nextra_args = ["--effort=high"]', "extra_args carries '--effort=high'"),
    (MODELED + '\nextra_args = ["--model", "x"]', "extra_args carries '--model'; a node names its model in model"),
    ('role = "Nobody"\nmodel = "@fable"\neffort = "medium"', "role 'Nobody' names no role manifest"),
])
def test_each_model_defect_is_named(schema_dir, tmp_path, design, expected):
    (schema_dir / "m.toml").write_text(_modeled(design))
    problems = pipeline.validate(pipeline.load_schema(schema_dir / "m.toml"), str(tmp_path),
                                 manifests=MANIFESTS)
    assert any(expected in p for p in problems), problems


def test_a_node_with_model_and_no_manifests_on_the_machine_takes_any_role_name(schema_dir, tmp_path):
    (schema_dir / "m.toml").write_text(_modeled('role = "Anything"\nmodel = "@fable"\neffort = "medium"'))
    assert pipeline.validate(pipeline.load_schema(schema_dir / "m.toml"), str(tmp_path),
                             manifests={}) == []


def _role_dir(root, name, text):
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "role.toml").write_text(text)


def test_validate_loads_only_the_roles_the_schema_names(schema_dir, tmp_path, monkeypatch):
    from dockwright import paths
    root = tmp_path / "roles"
    _role_dir(root, "GenericWorker", 'core = "GenericWorker"\nlaunch = ["worker"]\n')
    _role_dir(root, "Designer", 'core = "Designer"\nlaunch = ["worker"]\n')
    for name in ("IMPL_HARD", "SCOUT"):
        _role_dir(root, name, f'core = "{name}"\nlaunch = ["worker"]\n')
    _role_dir(root, "Broken", 'core = "Broken"\nnope = 1\n')
    monkeypatch.setattr(paths, "ROLES_DIR", root)
    (schema_dir / "t.toml").write_text(SCHEMA.replace('role = "SCOUT_DESIGN"', 'role = "Designer"', 1))
    assert pipeline.validate(pipeline.load_schema(schema_dir / "t.toml"), str(tmp_path)) == []
    (schema_dir / "u.toml").write_text(SCHEMA.replace('role = "SCOUT_DESIGN"', 'role = "Broken"', 1))
    problems = pipeline.validate(pipeline.load_schema(schema_dir / "u.toml"), str(tmp_path))
    assert len(problems) == 1 and "nope" in problems[0]


def test_a_relative_settings_value_is_refused_without_naming_a_profile():
    with pytest.raises(ValueError, match="--settings must be an absolute path or inline JSON") as err:
        pipeline.read_settings_value("rel.json")
    assert "profile" not in str(err.value)


def test_validate_rejects_two_start_nodes(schema_dir, tmp_path):
    text = SCHEMA.replace('[[edges]]\nfrom = "design"\nto = "task"\nexit_checks = [{ type = "artifact", name = "plan.tasks" }]\n', "")
    (schema_dir / "bad.toml").write_text(text)
    s = pipeline.load_schema(schema_dir / "bad.toml")
    assert any("start" in p for p in pipeline.validate(s, str(tmp_path)))


def test_snapshot_inlines_briefs_and_round_trips(schema_dir):
    s = pipeline.load_schema(schema_dir / "code-ticket.toml")
    text = pipeline.snapshot(s)
    data = tomllib.loads(text)
    assert data["nodes"]["design"]["brief_text"].startswith("# design brief")
    assert "brief" not in data["nodes"]["design"]
    again = pipeline.load_snapshot(text)
    assert again.nodes["design"].brief_text.startswith("# design brief")
    assert pipeline.schema_sha256(text) == hashlib.sha256(text.encode()).hexdigest()


def test_snapshot_is_independent_of_later_file_edits(schema_dir):
    s = pipeline.load_schema(schema_dir / "code-ticket.toml")
    text = pipeline.snapshot(s)
    (schema_dir / "briefs" / "design.md").write_text("changed\n")
    (schema_dir / "code-ticket.toml").write_text(SCHEMA.replace('to = "pr"', 'to = "post-merge"'))
    again = pipeline.load_snapshot(text)
    assert again.nodes["design"].brief_text.startswith("# design brief")
    assert [(e.frm, e.to) for e in again.edges][2] == ("task", "pr")


def test_snapshot_round_trips_hostile_brief_text_byte_exact(schema_dir):
    hostile = 'seed \U0001F331 del \x7f quote " back \\ nl\n tab\t crlf\r\n end'
    (schema_dir / "briefs" / "design.md").write_bytes(hostile.encode("utf-8"))
    text = pipeline.snapshot(pipeline.load_schema(schema_dir / "code-ticket.toml"))
    assert pipeline.load_snapshot(text).nodes["design"].brief_text == hostile


def test_snapshot_round_trips_exit_check_list_run_and_int_timeout(schema_dir):
    edited = SCHEMA.replace('exit_checks = [{ type = "artifact", name = "plan.tasks" }]',
                            'exit_checks = [{ type = "command", run = ["make", "check"], timeout_sec = 90 }]')
    (schema_dir / "code-ticket.toml").write_text(edited)
    again = pipeline.load_snapshot(pipeline.snapshot(pipeline.load_schema(schema_dir / "code-ticket.toml")))
    assert again.edges[0].exit_checks == [{"type": "command", "run": ["make", "check"], "timeout_sec": 90}]


def test_snapshot_round_trips_inline_tables_floats_and_quoted_keys():
    check = {"type": "value", "name": "plan.tasks", "field": "contract_hash",
             "equals": {"nested key": [1.5, True, "x"]}}
    s = pipeline.Schema(name="s", version=2,
                        nodes={"tâche": pipeline.Node(name="tâche", role="R", brief_text="b")},
                        edges=[pipeline.Edge(frm="tâche", to="tâche", exit_checks=[check])])
    again = pipeline.load_snapshot(pipeline.snapshot(s))
    assert set(again.nodes) == {"tâche"} and again.version == 2
    assert again.edges[0].exit_checks == [check]


def test_snapshot_refuses_a_value_toml_cannot_hold():
    s = pipeline.Schema(name="s", version=1, nodes={"a": pipeline.Node(name="a", role="R", brief_text="b")},
                        edges=[pipeline.Edge(frm="a", to="a", exit_checks=[{"type": "value", "equals": None}])])
    with pytest.raises(ValueError):
        pipeline.snapshot(s)


def test_snapshot_is_idempotent(schema_dir):
    text = pipeline.snapshot(pipeline.load_schema(schema_dir / "code-ticket.toml"))
    assert pipeline.snapshot(pipeline.load_snapshot(text)) == text


ISLAND = '''trigger = "merge"
[nodes.island-a]
role = "SCOUT"
brief = "briefs/pr.md"
[nodes.island-b]
role = "SCOUT"
brief = "briefs/pr.md"
[[edges]]
from = "island-a"
to = "island-b"
[[edges]]
from = "island-b"
to = "island-a"'''

ARTIFACT_CHECK = '{ type = "artifact", name = "plan.tasks" }'


@pytest.mark.parametrize("edit,replacement,expected", [
    ('version = 1', 'version = 1\ncolour = "red"', "unknown key 'colour'"),
    ('checkpoints = ["design.explainer"]', 'checkpoints = ["design.explainer"]\ngaet = true', "unknown key 'gaet'"),
    ('when = "last"', 'when = "last"\nwehn = "last"', "unknown key 'wehn'"),
    ('schema = "code-ticket"', 'schema = 7', "`schema` must be a string"),
    ('version = 1', 'version = true', "`version` must be an integer"),
    ('role = "SCOUT"', 'role = ["SCOUT"]', "`role` must be a string"),
    ('gate = true\nbrief = "briefs/design.md"', 'gate = "yes"\nbrief = "briefs/design.md"', "`gate` must be a boolean"),
    ('outputs = ["implement.repo"]', 'outputs = "implement.repo"', "`outputs` must be a list of strings"),
    ('extra_args = []', 'extra_args = ["--effort", 3]', "`extra_args` must be a list of strings"),
    ('when = "last"', 'when = 1', "`when` must be a string"),
    (ARTIFACT_CHECK, '"plan.tasks"', "`exit_checks` must be a list of tables"),
    ('brief = "briefs/pr.md"', 'brief = "briefs/nope.md"', "briefs/nope.md"),
    (ARTIFACT_CHECK, '{ type = "artifact" }', "needs `name`"),
    (ARTIFACT_CHECK, '{ type = "value", name = "plan.tasks", field = "contract_hash" }', "needs `equals`"),
    (ARTIFACT_CHECK, '{ type = "value", name = "plan.tasks", equals = "x" }', "needs `field`"),
    (ARTIFACT_CHECK, '{ type = "command" }', "needs `run`"),
    (ARTIFACT_CHECK, '{ type = "command", run = [] }', "`run` must be"),
    (ARTIFACT_CHECK, '{ type = "command", run = "" }', "`run` must be"),
    (ARTIFACT_CHECK, '{ type = "command", run = ["make", 1] }', "`run` must be"),
    (ARTIFACT_CHECK, '{ type = "command", run = "make", timeout_sec = "soon" }', "`timeout_sec` must be"),
    (ARTIFACT_CHECK, '{ type = "value", name = "plan.tasks", field = "at", equals = 1979-05-27 }', "TOML"),
    ('extra_args = []', 'extra_args = ["--model", "x"]', "'--model'"),
    ('extra_args = []', 'extra_args = ["--model=x"]', "'--model=x'"),
    ('extra_args = []', 'extra_args = ["-m", "x"]', "'-m'"),
    ('extra_args = []', 'extra_args = ["-m=x"]', "'-m=x'"),
    ('trigger = "merge"', 'trigger = "merge"\n[[edges]]\nfrom = "pr"\nto = "pr"', "self-loop"),
    ('trigger = "merge"', 'trigger = "merge"\n[[edges]]\nfrom = "task"\nto = "task"', "duplicate"),
    ('trigger = "merge"', ISLAND, "unreachable"),
    ('inputs = ["plan.tasks", "design.hld"]', 'inputs = ["plan.tasks", "design.nowhere"]',
     "input 'design.nowhere' is not an output or checkpoint of any upstream node"),
    ('repeat = "plan.tasks"', 'repeat = "plan.nowhere"',
     "repeat 'plan.nowhere' is not an output or checkpoint of any upstream node"),
    ('[[edges]]\nfrom = "task"\nto = "task"\n', "", "[nodes.task] repeat needs a self-loop edge task -> task"),
    ('from = "task"\nto = "task"\n', 'from = "task"\nto = "task"\nwhen = "last"\n', "a self-loop cannot carry `when`"),
    ('from = "task"\nto = "task"\n', 'from = "task"\nto = "task"\ntrigger = "merge"\n', "a self-loop cannot carry `trigger`"),
    (ARTIFACT_CHECK, '{ type = "command", run = "make", timeout = 90 }', "exit_check unknown key 'timeout'"),
    ('extra_args = []', 'extra_args = ["--fallback-model", "x"]', "'--fallback-model'"),
    ('extra_args = []', 'extra_args = ["--fallback-model=x"]', "'--fallback-model=x'"),
    ('repeat = "plan.tasks"', 'repeat = "design.hld"', "repeat 'design.hld': stage 1 repeats over 'plan.tasks' only"),
    (ARTIFACT_CHECK, '{ type = "command", run = "true\\u0000" }', "`run` contains a NUL character"),
    (ARTIFACT_CHECK, '{ type = "command", run = ["true", "a\\u0000"] }', "`run` contains a NUL character"),
    (ARTIFACT_CHECK, '{ type = "command", run = "make", timeout_sec = 86401 }', "no larger than 86400"),
    ('outputs = ["summary.post-merge"]', 'outputs = ["summary.thread"]',
     "[nodes.post-merge] declares 'summary.thread', the work's running summary every node writes"),
    ('checkpoints = ["design.explainer"]', 'checkpoints = ["summary.thread"]',
     "[nodes.design] declares 'summary.thread', the work's running summary every node writes"),
    (ARTIFACT_CHECK, '{ type = "when", line = "internal", equals = "yes" }', "exit_check when needs `input`"),
    (ARTIFACT_CHECK, '{ type = "when", input = "context.ask", line = "in ternal", equals = "yes" }',
     "must be a bare key"),
    (ARTIFACT_CHECK, '{ type = "when", input = "context.ask", line = "internal", equals = "yes please" }',
     "can never match: it must be one word"),
    (ARTIFACT_CHECK, '{ type = "when", input = "context.ask", line = "internal", equals = "yes", unless = "" }',
     "exit_check when `unless` must be a non-empty string"),
    (ARTIFACT_CHECK, '{ type = "when", input = "intake.route", line = "internal", equals = "yes" }',
     "exit_check when `input` 'intake.route' is not an output or checkpoint of design or a node upstream of it"),
    (ARTIFACT_CHECK, '{ type = "when", input = "context.ask", line = "internal", equals = "yes", unless = "gate.open" }',
     "exit_check when `unless` 'gate.open' is not an output or checkpoint of design or a node upstream of it"),
    (ARTIFACT_CHECK, '{ type = "artifact", name = "plan.tasks", unless = "design.explainer" }',
     "exit_check unknown key 'unless' for type 'artifact'"),
    (ARTIFACT_CHECK, '{ type = "when", input = "context.ask", line = "internal", equals = "yes", name = "plan.tasks" }',
     "exit_check unknown key 'name' for type 'when'"),
    (ARTIFACT_CHECK, '{ type = "command", run = "make", input = "context.ask" }', "exit_check unknown key 'input' for type 'command'"),
    (ARTIFACT_CHECK, '{ type = "artifact", name = "plan.tasks", timeout_sec = 5 }',
     "exit_check unknown key 'timeout_sec' for type 'artifact'"),
])
def test_validate_names_each_structural_defect(schema_dir, tmp_path, edit, replacement, expected):
    assert edit in SCHEMA
    (schema_dir / "bad.toml").write_text(SCHEMA.replace(edit, replacement))
    s = pipeline.load_schema(schema_dir / "bad.toml")
    problems = pipeline.validate(s, str(tmp_path))
    assert any(expected in p for p in problems), problems


def test_validate_lists_parse_problems_first(schema_dir, tmp_path):
    text = SCHEMA.replace('role = "SCOUT"', 'role = "NOPE"').replace('version = 1', 'version = 1\ncolour = "red"')
    (schema_dir / "bad.toml").write_text(text)
    problems = pipeline.validate(pipeline.load_schema(schema_dir / "bad.toml"), str(tmp_path),
                                 manifests={name: _manifest(name) for name in ("SCOUT_DESIGN", "IMPL_HARD")})
    assert "colour" in problems[0] and any("NOPE" in p for p in problems[1:])


def test_validate_names_a_brief_that_is_not_utf8(schema_dir, tmp_path):
    (schema_dir / "briefs" / "pr.md").write_bytes(b"\xff\xfe broken")
    problems = pipeline.validate(pipeline.load_schema(schema_dir / "code-ticket.toml"), str(tmp_path))
    assert any("briefs/pr.md" in p for p in problems)


@pytest.mark.parametrize("text", [
    'schema = "x"\nnodes = 5\n',
    'schema = "x"\n[nodes]\ndesign = 5\n',
    'schema = "x"\nedges = "a"\n',
    'schema = "x"\nedges = [1]\n',
    'schema = "x"\n[[edges]]\nto = "a"\n',
    'schema = "x"\n[[edges]]\nfrom = "a"\nto = 5\n',
])
def test_loaders_raise_schema_error_when_no_object_can_be_built(tmp_path, text):
    (tmp_path / "s.toml").write_text(text)
    with pytest.raises(pipeline.SchemaError):
        pipeline.load_schema(tmp_path / "s.toml")
    with pytest.raises(pipeline.SchemaError):
        pipeline.load_snapshot(text)
    assert issubclass(pipeline.SchemaError, ValueError)


def test_loaders_pass_toml_syntax_errors_through(tmp_path):
    (tmp_path / "s.toml").write_text("schema = \n")
    with pytest.raises(tomllib.TOMLDecodeError):
        pipeline.load_schema(tmp_path / "s.toml")


def _ev(type_, **kw):
    kw.setdefault("token_id", "t1")
    kw.setdefault("ts", 1.0)
    return {"type": type_, **kw}


STARTED = _ev("pipeline_started", schema="code-ticket", node="design", index=1, holder="a-design", cwd="/w", manager="mgr")


def test_fold_reads_position_holder_and_open_requests():
    events = [
        STARTED,
        _ev("advance_requested", request_id="r1", edge={"from": "design", "to": "task"}, index_to=1, kind="advance", actor_sid="s1"),
        _ev("advance_pending", request_id="r1", reason="no_register", assignment_id="a-task-1"),
    ]
    f = pipeline.fold(events)
    t = f.tokens["t1"]
    assert (t.node, t.index, t.holder, t.cwd, t.manager) == ("design", 1, "a-design", "/w", "mgr")
    assert [r["request_id"] for r in f.open_requests] == ["r1"]
    assert f.open_requests[0]["pending"]["assignment_id"] == "a-task-1"
    events.append(_ev("advance_committed", request_id="r1", node="task", index=1, holder="a-task-1", sid="s2"))
    f = pipeline.fold(events)
    assert f.open_requests == [] and f.tokens["t1"].node == "task" and f.tokens["t1"].holder == "a-task-1"


def test_fold_override_moves_the_token_anywhere_and_ignores_foreign_types():
    events = [STARTED, _ev("token_override", **{"from": "design"}, to="pr", index=1, reason="skip", actor_sid="m"),
              {"type": "note", "reason": "free-form worker event"}]
    t = pipeline.fold(events).tokens["t1"]
    assert (t.node, t.index, t.holder) == ("pr", 1, None)


def test_fold_ignores_lane_events():
    events = [STARTED]
    with_lane = [*events, {"type": "lane_event", "kind": "HANDED_OFF_CLOSED", "fields": {"task_key": "K", "name": "w"}}]
    assert pipeline.fold(with_lane) == pipeline.fold(events)


def _facts(**artifacts):
    stamps = {k: {"status": v[0], "scope": v[1]} for k, v in artifacts.items()}
    return pipeline.Facts(artifacts=stamps, plan_tasks=[{"id": 1}, {"id": 2}], command_runner=None)


def test_three_sessions_walk_three_nodes(schema_dir, tmp_path):
    s = pipeline.load_snapshot(pipeline.snapshot(pipeline.load_schema(schema_dir / "code-ticket.toml")))
    token = pipeline.Token("t1", "design", 1, "a-design", "/w", "mgr")
    caller = pipeline.Caller(assignment_id="a-design", ticket="K", agent="worker")
    facts = _facts(**{"context.ask": ("complete", "output"), "design.hld": ("complete", "output"), "plan.tasks": ("complete", "output")})
    v = pipeline.check_advance(s, token, caller, "task", facts, [])
    assert v.ok and v.index_to == 1 and v.edge.to == "task"
    token = pipeline.Token("t1", "task", 1, "a-task-1", "/w", "mgr")
    caller = pipeline.Caller("a-task-1", "K", "worker")
    facts = _facts(**{"review.task-1": ("complete", "output")})
    v = pipeline.check_advance(s, token, caller, "task", facts, [])
    assert v.ok and v.index_to == 2
    v = pipeline.check_advance(s, token, caller, "pr", facts, [])
    assert not v.ok and "last" in v.reason
    token = pipeline.Token("t1", "task", 2, "a-task-2", "/w", "mgr")
    caller = pipeline.Caller("a-task-2", "K", "worker")
    facts = _facts(**{"review.task-2": ("complete", "output")})
    assert pipeline.check_advance(s, token, caller, "pr", facts, []).ok
    assert not pipeline.check_advance(s, token, caller, "task", facts, []).ok


def test_a_missing_edge_refusal_names_the_edges_out_of_the_node(schema_dir):
    s = pipeline.load_snapshot(pipeline.snapshot(pipeline.load_schema(schema_dir / "code-ticket.toml")))
    token = pipeline.Token("t1", "design", 1, "a-design", "/w", "mgr")
    caller = pipeline.Caller(assignment_id="a-design", ticket="K", agent="worker")
    v = pipeline.check_advance(s, token, caller, "pr", _facts(), [])
    assert not v.ok
    assert v.reason == "no edge design -> pr in the schema snapshot; edges out of design: task"


def test_check_advance_refuses_each_fixable_cause(schema_dir):
    s = pipeline.load_snapshot(pipeline.snapshot(pipeline.load_schema(schema_dir / "code-ticket.toml")))
    token = pipeline.Token("t1", "design", 1, "a-design", "/w", "mgr")
    holder = pipeline.Caller("a-design", "K", "worker")
    child = pipeline.Caller("a-child", "K", "worker")
    good = _facts(**{"context.ask": ("complete", "output"), "design.hld": ("complete", "output"), "plan.tasks": ("complete", "output")})
    assert "holder" in pipeline.check_advance(s, token, child, "task", good, []).reason
    assert "ask_manager" in pipeline.check_advance(s, token, holder, "?", good, []).reason
    assert "edge" in pipeline.check_advance(s, token, holder, "post-merge", good, []).reason
    partial = _facts(**{"context.ask": ("complete", "output"), "design.hld": ("partial", "output"), "plan.tasks": ("complete", "output")})
    assert "design.hld" in pipeline.check_advance(s, token, holder, "task", partial, []).reason
    wrong_scope = _facts(**{"context.ask": ("complete", "output"), "design.hld": ("complete", "internal"), "plan.tasks": ("complete", "output")})
    assert "scope" in pipeline.check_advance(s, token, holder, "task", wrong_scope, []).reason
    in_flight = [{"request_id": "r9", "edge": {"from": "design", "to": "task"}, "token_id": "t1"}]
    assert "in flight" in pipeline.check_advance(s, token, holder, "task", good, in_flight).reason
    manager = pipeline.Caller(None, None, "manager")
    assert "override" in pipeline.check_advance(s, token, manager, "task", good, []).reason


def test_exit_checks_of_three_types(tmp_path):
    facts = pipeline.Facts(artifacts={"plan.tasks": {"status": "complete", "scope": "output", "contract_hash": "abc"}},
                           plan_tasks=[], command_runner=lambda argv, cwd, timeout: 0 if argv == ["true"] else 1)
    assert pipeline.exit_check({"type": "artifact", "name": "plan.tasks"}, facts, str(tmp_path), 5)[0]
    assert not pipeline.exit_check({"type": "artifact", "name": "plan.none"}, facts, str(tmp_path), 5)[0]
    assert pipeline.exit_check({"type": "value", "name": "plan.tasks", "field": "contract_hash", "equals": "abc"}, facts, str(tmp_path), 5)[0]
    assert not pipeline.exit_check({"type": "value", "name": "plan.tasks", "field": "contract_hash", "equals": "zzz"}, facts, str(tmp_path), 5)[0]
    assert pipeline.exit_check({"type": "command", "run": ["true"]}, facts, str(tmp_path), 5)[0]
    ok, why = pipeline.exit_check({"type": "command", "run": ["false"]}, facts, str(tmp_path), 5)
    assert not ok and "false" in why


WHEN_CHECK = {"type": "when", "input": "intake.route", "line": "internal", "equals": "yes", "unless": "gate.open"}


def _route_facts(body, *present, read=True):
    stamps = {"intake.route": {"status": "complete", "scope": "output"}, **{n: {"status": "complete"} for n in present}}
    return pipeline.Facts(artifacts=stamps, plan_tasks=None, command_runner=None,
                          read=(lambda name: body) if read else None)


@pytest.mark.parametrize("facts,expected", [
    (_route_facts("next: plan\ninternal: yes — no ticket\n"), (True, "")),
    (_route_facts("internal: YES\n"), (True, "")),
    (_route_facts("internal: no — unsure\n"), (False, "intake.route has no `internal: yes` line")),
    (_route_facts("next: plan\n"), (False, "intake.route has no `internal: yes` line")),
    (_route_facts("internal: yes — no ticket\n", "gate.open"), (False, "gate.open exists")),
    (_route_facts("internal: yes — no ticket\n", read=False), (False, "intake.route cannot be read here")),
])
def test_a_when_exit_check_reads_one_line_of_its_input(tmp_path, facts, expected):
    assert pipeline.exit_check(WHEN_CHECK, facts, str(tmp_path), 5) == expected


def test_a_when_exit_check_refuses_an_input_that_is_not_complete(tmp_path):
    facts = pipeline.Facts(artifacts={"intake.route": {"status": "partial"}}, plan_tasks=None, command_runner=None,
                           read=lambda name: "internal: yes")
    assert pipeline.exit_check(WHEN_CHECK, facts, str(tmp_path), 5) == (False, "artifact intake.route is not complete")


def test_a_when_exit_check_validates_and_round_trips_the_snapshot(schema_dir, tmp_path):
    check = '{ type = "when", input = "context.ask", line = "internal", equals = "yes", unless = "design.explainer" }'
    (schema_dir / "code-ticket.toml").write_text(SCHEMA.replace(ARTIFACT_CHECK, check))
    s = pipeline.load_schema(schema_dir / "code-ticket.toml")
    assert pipeline.validate(s, str(tmp_path)) == []
    again = pipeline.load_snapshot(pipeline.snapshot(s))
    assert again.edges[0].exit_checks == [{"type": "when", "input": "context.ask", "line": "internal",
                                           "equals": "yes", "unless": "design.explainer"}]


def test_compose_brief_leaves_json_braces_alone(schema_dir):
    node = pipeline.Node(name="x", role="R", brief_text='write {"tasks": [{"id": 1}]} for {task_key}')
    text = pipeline.compose_brief(node, pipeline.Token("t1", "x", 1, None, "/w", "m"), "K", [], [], [], [])
    assert '{"tasks": [{"id": 1}]}' in text and "K" in text


def test_compose_brief_substitutes_names_not_paths(schema_dir):
    s = pipeline.load_snapshot(pipeline.snapshot(pipeline.load_schema(schema_dir / "code-ticket.toml")))
    token = pipeline.Token("t1", "task", 2, None, "/w", "mgr")
    text = pipeline.compose_brief(s.nodes["task"], token, "K", ["plan.tasks", "design.hld"], [], ["decision.q-ab12cd34"],
                                  [e for e in s.edges if e.frm == "task"])
    assert "K" in text and "/w" in text and "plan.tasks" in text and "decision.q-ab12cd34" in text
    assert "review.task-2" in text and "pipeline_advance" in text and 'to="?"' in text
    assert "artifacts/" not in text


def test_fold_ignores_a_pending_with_no_request():
    events = [STARTED, _ev("advance_pending", request_id="ghost", reason="no_register", assignment_id="a-x")]
    f = pipeline.fold(events)
    t = f.tokens["t1"]
    assert f.open_requests == []
    assert (t.node, t.index, t.holder) == ("design", 1, "a-design")


def test_fold_restamps_the_holder():
    events = [STARTED, _ev("holder_restamped", request_id="r1", old="a-design", new="a-design-2", transcript_bytes=10)]
    assert pipeline.fold(events).tokens["t1"].holder == "a-design-2"


def _design_ready():
    return _facts(**{"context.ask": ("complete", "output"), "design.hld": ("complete", "output"),
                     "plan.tasks": ("complete", "output")})


def test_check_advance_refuses_an_unassigned_caller_even_on_an_unheld_token(schema_dir):
    s = pipeline.load_snapshot(pipeline.snapshot(pipeline.load_schema(schema_dir / "code-ticket.toml")))
    token = pipeline.Token("t1", "design", 1, None, "/w", "mgr")
    v = pipeline.check_advance(s, token, pipeline.Caller(None, "K", "worker"), "task", _design_ready(), [])
    assert not v.ok and "holder" in v.reason


@pytest.mark.parametrize("check,missing", [
    ({"type": "artifact"}, "`name`"),
    ({"type": "value", "name": "plan.tasks", "equals": "abc"}, "`field`"),
    ({"type": "value", "name": "plan.tasks", "field": "contract_hash"}, "`equals`"),
    ({"type": "command"}, "`run`"),
])
def test_exit_check_names_a_missing_key_instead_of_raising(tmp_path, check, missing):
    ok, why = pipeline.exit_check(check, _design_ready(), str(tmp_path), 5)
    assert not ok and missing in why


def test_exit_check_runs_real_commands(tmp_path):
    facts = _design_ready()
    assert pipeline.exit_check({"type": "command", "run": ["true"]}, facts, str(tmp_path), 5) == (True, "")
    ok, why = pipeline.exit_check({"type": "command", "run": "exit 3"}, facts, str(tmp_path), 5)
    assert not ok and "exited 3" in why
    ok, why = pipeline.exit_check({"type": "command", "run": ["sleep", "5"], "timeout_sec": 1}, facts, str(tmp_path), 5)
    assert not ok and "timed out after 1 s" in why


def test_exit_check_says_when_a_command_cannot_start(tmp_path):
    facts = _design_ready()
    ok, why = pipeline.exit_check({"type": "command", "run": [str(tmp_path / "no-such-binary")]}, facts, str(tmp_path), 5)
    assert not ok and "could not start" in why
    ok, why = pipeline.exit_check({"type": "command", "run": ["true"]}, facts, str(tmp_path / "no-such-dir"), 5)
    assert not ok and "could not start" in why
    script = tmp_path / "not-executable.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    ok, why = pipeline.exit_check({"type": "command", "run": [str(script)]}, facts, str(tmp_path), 5)
    assert not ok and why.endswith("(missing binary or directory, or not executable)")


@pytest.mark.parametrize("code", [124, 127])
def test_a_real_exit_status_is_never_read_as_a_runner_failure(tmp_path, code):
    ok, why = pipeline.exit_check({"type": "command", "run": f"exit {code}"}, _design_ready(), str(tmp_path), 5)
    assert not ok and f"exited {code}" in why
    assert "timed out" not in why and "could not start" not in why


@pytest.mark.parametrize("stamped,ok", [(1, False), (True, True), ("true", False)])
def test_value_exit_check_needs_the_same_type(tmp_path, stamped, ok):
    facts = pipeline.Facts(artifacts={"plan.tasks": {"status": "complete", "flag": stamped}}, plan_tasks=[], command_runner=None)
    check = {"type": "value", "name": "plan.tasks", "field": "flag", "equals": True}
    assert pipeline.exit_check(check, facts, str(tmp_path), 5)[0] is ok


def _brief_for(schema_dir, name, task_key="K"):
    s = pipeline.load_snapshot(pipeline.snapshot(pipeline.load_schema(schema_dir / "code-ticket.toml")))
    token = pipeline.Token("t1", name, 1, None, "/w", "mgr")
    return pipeline.compose_brief(s.nodes[name], token, task_key, [], [], [], [e for e in s.edges if e.frm == name])


def test_compose_brief_for_a_trigger_only_node_leaves_the_move_to_the_manager(schema_dir):
    text = _brief_for(schema_dir, "pr")
    last = text.splitlines()[-1]
    assert "pipeline_advance(" not in text
    assert "manager" in last and "post-merge" in last and "merge" in last and "worker_done" in last


def test_compose_brief_for_a_terminal_node_says_so(schema_dir):
    text = _brief_for(schema_dir, "post-merge")
    last = text.splitlines()[-1]
    assert "pipeline_advance(" not in text
    assert "terminal node" in last and "worker_done" in last


@pytest.mark.parametrize("name", ["design", "pr", "post-merge"])
def test_every_footer_asks_for_the_running_summary_right_before_its_last_action(schema_dir, name):
    lines = _brief_for(schema_dir, name).splitlines()
    summary = lines[-2]
    assert summary == pipeline._summary_line()
    assert "before pipeline_advance, or before worker_done when you do not call it, a park included" in summary
    assert 'artifact_get(task_key, "summary", "thread", reader_sid=<your sid>)' in summary
    assert ('then write yours to a file and artifact_put(task_key, phase="summary", name="thread", '
            'content_path=<that file, at most 15 lines>, status="complete", scope="output", overwrite=True, '
            'writer_sid=<your sid>)') in summary
    assert len(compaction.PRESERVE_ITEMS) == 6
    assert all(f"({i}) {item}" in summary for i, item in enumerate(compaction.PRESERVE_ITEMS, 1))
    assert compaction.VOICES not in summary
    assert "worker_done" in lines[-1]


@pytest.mark.parametrize("name", ["design", "pr", "post-merge"])
def test_every_footer_asks_nodes_to_say_who_decided_right_before_the_summary(schema_dir, name):
    lines = _brief_for(schema_dir, name).splitlines()
    decision = lines[-3]
    assert decision == pipeline._decision_line()
    assert lines[-4].startswith("Edges out of this node: ")
    assert "decision.* record" in decision and "decided_by" in decision and "artifact_put" in decision
    assert ('Pass "manager" for anything that came in a [MANAGER] message, even when it quotes the engineer '
            "or relays their gate verdict.") in decision
    assert ('Pass "engineer" only for a message typed into your pane without a marker, for a [TOPIC … from '
            "engineer] line, or for a gate verdict you read from the gate yourself or from a decision.verdict-r<n> "
            "record.") in decision


def test_compose_brief_substitutes_in_one_pass(schema_dir):
    node = pipeline.Node(name="x", role="R", brief_text="key={task_key} at {cwd}")
    text = pipeline.compose_brief(node, pipeline.Token("t1", "x", 1, None, "/w", "m"), "{cwd}", [], [], [], [])
    assert text.startswith("key={cwd} at /w")


def test_cli_validate_prints_problems_and_exit_codes(schema_dir, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(config, "pipeline_schemas_dir", lambda: schema_dir)
    assert pipeline.main(["validate", "code-ticket", "--cwd", str(tmp_path)]) == 0
    assert "ok" in capsys.readouterr().out
    (schema_dir / "bad.toml").write_text(SCHEMA.replace('when = "last"', 'when = "first"'))
    assert pipeline.main(["validate", "bad", "--cwd", str(tmp_path)]) == 1
    assert "first" in capsys.readouterr().out
    assert pipeline.main(["validate", "missing", "--cwd", str(tmp_path)]) == 2


def test_cli_validate_takes_a_path(schema_dir, tmp_path, capsys):
    assert pipeline.main(["validate", str(schema_dir / "code-ticket.toml"), "--cwd", str(tmp_path)]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "ok"


@pytest.mark.parametrize("text", ["schema = \n", 'schema = "x"\nnodes = 5\n'])
def test_cli_validate_reports_a_file_it_cannot_load(schema_dir, tmp_path, capsys, monkeypatch, text):
    monkeypatch.setattr(config, "pipeline_schemas_dir", lambda: schema_dir)
    (schema_dir / "broken.toml").write_text(text)
    assert pipeline.main(["validate", "broken", "--cwd", str(tmp_path)]) == 1
    assert str(schema_dir / "broken.toml") in capsys.readouterr().out


def test_pipeline_config_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "none.toml"))
    assert config.pipeline_schemas_dir() == config.claude_config_home() / "dockwright-schemas"
    assert config.pipeline_schemas_dir() == tmp_path / ".claude" / "dockwright-schemas"
    assert config.pipeline_int("requested_ttl_sec", 300) == 300
    (tmp_path / "c.toml").write_text('[pipeline]\nrequested_ttl_sec = 42\nschemas_dir = "/x/y"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "c.toml"))
    assert config.pipeline_int("requested_ttl_sec", 300) == 42
    assert config.pipeline_schemas_dir() == Path("/x/y")


@pytest.mark.parametrize("body,ttl,schemas", [
    ('requested_ttl_sec = true\nschemas_dir = "~/s"\n', 300, "s"),
    ('requested_ttl_sec = "42"\nschemas_dir = ""\n', 300, ".claude/dockwright-schemas"),
    ('requested_ttl_sec = -5\nschemas_dir = 7\n', 0, ".claude/dockwright-schemas"),
])
def test_pipeline_config_falls_back_on_bad_values(monkeypatch, tmp_path, body, ttl, schemas):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "c.toml").write_text("[pipeline]\n" + body)
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "c.toml"))
    assert config.pipeline_int("requested_ttl_sec", 300) == ttl
    assert config.pipeline_schemas_dir() == tmp_path / schemas


@pytest.mark.parametrize("raw_token_id", [None, ""])
def test_an_open_request_with_a_blank_token_id_still_blocks_the_token(schema_dir, raw_token_id):
    s = pipeline.load_snapshot(pipeline.snapshot(pipeline.load_schema(schema_dir / "code-ticket.toml")))
    events = [STARTED, _ev("advance_requested", request_id="r1", token_id=raw_token_id,
                           edge={"from": "design", "to": "task"}, index_to=1, kind="advance", actor_sid="s1")]
    f = pipeline.fold(events)
    v = pipeline.check_advance(s, f.tokens["t1"], pipeline.Caller("a-design", "K", "worker"), "task",
                               _design_ready(), f.open_requests)
    assert not v.ok and "in flight" in v.reason


@pytest.mark.parametrize("type_,extra", [("advance_committed", {"request_id": "r1", "node": "task", "holder": "a"}),
                                         ("token_override", {"from": "design", "to": "pr", "reason": "skip"})])
def test_fold_refuses_a_move_without_an_index(type_, extra):
    with pytest.raises(KeyError):
        pipeline.fold([STARTED, _ev(type_, **extra)])


def test_fold_restamps_the_token_of_the_named_request():
    events = [STARTED, _ev("pipeline_started", token_id="t2", node="task", index=1, holder="a-t2", cwd="/w", manager="mgr"),
              _ev("advance_requested", token_id="t2", request_id="r2", edge={"from": "task", "to": "task"}, index_to=2),
              _ev("holder_restamped", token_id=None, request_id="r2", old="a-t2", new="a-t2-b"),
              _ev("holder_restamped", token_id=None, request_id="unknown", old="a-design", new="a-design-b")]
    tokens = pipeline.fold(events).tokens
    assert tokens["t2"].holder == "a-t2-b" and tokens["t1"].holder == "a-design-b"


def test_check_advance_names_the_holder_before_the_question_mark(schema_dir):
    s = pipeline.load_snapshot(pipeline.snapshot(pipeline.load_schema(schema_dir / "code-ticket.toml")))
    token = pipeline.Token("t1", "design", 1, "a-design", "/w", "mgr")
    v = pipeline.check_advance(s, token, pipeline.Caller("a-child", "K", "worker"), "?", _design_ready(), [])
    assert not v.ok and "not the holder" in v.reason


def test_the_manager_fires_a_trigger_edge(schema_dir):
    s = pipeline.load_snapshot(pipeline.snapshot(pipeline.load_schema(schema_dir / "code-ticket.toml")))
    token = pipeline.Token("t1", "pr", 1, "a-pr", "/w", "mgr")
    v = pipeline.check_advance(s, token, pipeline.Caller(None, None, "manager"), "post-merge", _facts(), [])
    assert v.ok and v.index_to == 1 and v.edge.trigger == "merge"


def test_the_holder_cannot_fire_a_trigger_edge(schema_dir):
    s = pipeline.load_snapshot(pipeline.snapshot(pipeline.load_schema(schema_dir / "code-ticket.toml")))
    token = pipeline.Token("t1", "pr", 1, "a-pr", "/w", "mgr")
    v = pipeline.check_advance(s, token, pipeline.Caller("a-pr", "K", "worker"), "post-merge", _facts(), [])
    assert not v.ok and "only the manager" in v.reason


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0o000 file, so no OSError is raised")
def test_cli_validate_reports_a_schema_file_it_cannot_read(schema_dir, tmp_path, capsys):
    path = schema_dir / "code-ticket.toml"
    path.chmod(0)
    try:
        assert pipeline.main(["validate", str(path), "--cwd", str(tmp_path)]) == 1
    finally:
        path.chmod(0o644)
    assert str(path) in capsys.readouterr().out


def _overridden_while_in_flight():
    return [STARTED,
            _ev("advance_requested", request_id="r1", edge={"from": "design", "to": "task"}, index_to=1, kind="advance"),
            _ev("advance_pending", request_id="r1", reason="no_register", assignment_id="a-task-1"),
            _ev("token_override", **{"from": "design"}, to="pr", index=1, reason="stuck", actor_sid="m")]


def test_an_override_closes_the_open_requests_of_its_token(schema_dir):
    s = pipeline.load_snapshot(pipeline.snapshot(pipeline.load_schema(schema_dir / "code-ticket.toml")))
    f = pipeline.fold(_overridden_while_in_flight())
    assert f.open_requests == []
    v = pipeline.check_advance(s, f.tokens["t1"], pipeline.Caller(None, None, "manager"), "post-merge", _facts(), f.open_requests)
    assert v.ok


@pytest.mark.parametrize("late", [
    _ev("advance_committed", request_id="r1", node="task", index=1, holder="a-task-1", sid="s2"),
    _ev("advance_pending", request_id="r1", reason="paused", assignment_id="a-task-1"),
    _ev("holder_restamped", token_id=None, request_id="r1", old=None, new="a-task-1"),
])
def test_a_late_event_of_a_closed_request_changes_nothing(late):
    f = pipeline.fold([*_overridden_while_in_flight(), late])
    t = f.tokens["t1"]
    assert (t.node, t.index, t.holder) == ("pr", 1, None) and f.open_requests == []


def test_an_override_leaves_other_tokens_requests_open_and_later_requests_open():
    events = _overridden_while_in_flight()
    events[1:1] = [_ev("pipeline_started", token_id="t2", node="task", index=1, holder="a-t2", cwd="/w", manager="mgr"),
                   _ev("advance_requested", token_id="t2", request_id="r2", edge={"from": "task", "to": "task"}, index_to=2)]
    events.append(_ev("advance_requested", request_id="r3", edge={"from": "pr", "to": "pr"}, index_to=1, kind="restart"))
    f = pipeline.fold(events)
    assert [r["request_id"] for r in f.open_requests] == ["r2", "r3"]
    assert [r["token_id"] for r in f.open_requests] == ["t2", "t1"]


def test_an_override_supersedes_a_request_committed_before_it():
    events = [_ev("pipeline_started", node="task", index=1, holder="a-task", cwd="/w", manager="mgr"),
              _ev("advance_requested", request_id="r1", edge={"from": "task", "to": "task"}, index_to=1, kind="restart"),
              _ev("advance_committed", request_id="r1", node="task", index=1, holder="a-task-2", sid="s2"),
              _ev("token_override", **{"from": "task"}, to="pr", index=1, reason="stuck", actor_sid="m"),
              _ev("holder_restamped", token_id=None, request_id="r1", old="a-task-2", new="a-task-2")]
    f = pipeline.fold(events)
    t = f.tokens["t1"]
    assert (t.node, t.index, t.holder) == ("pr", 1, None) and f.open_requests == []


def _assert_task_list_refusal(reason):
    assert reason.startswith("plan.tasks holds no task list the engine can read")
    assert '{"tasks": [...]}' in reason and "no code fence" in reason and "overwrite=True" in reason


@pytest.mark.parametrize("plan", [None, []])
def test_the_producer_of_an_unreadable_task_list_cannot_leave(schema_dir, plan):
    s = pipeline.load_snapshot(pipeline.snapshot(pipeline.load_schema(schema_dir / "code-ticket.toml")))
    token = pipeline.Token("t1", "design", 1, "a-design", "/w", "mgr")
    facts = _design_ready()
    facts.plan_tasks = plan
    v = pipeline.check_advance(s, token, pipeline.Caller("a-design", "K", "worker"), "task", facts, [])
    assert not v.ok
    _assert_task_list_refusal(v.reason)


def test_an_unpublished_task_list_is_reported_as_incomplete_first(schema_dir):
    s = pipeline.load_snapshot(pipeline.snapshot(pipeline.load_schema(schema_dir / "code-ticket.toml")))
    token = pipeline.Token("t1", "design", 1, "a-design", "/w", "mgr")
    facts = _facts(**{"context.ask": ("complete", "output"), "design.hld": ("complete", "output")})
    facts.plan_tasks = None
    v = pipeline.check_advance(s, token, pipeline.Caller("a-design", "K", "worker"), "task", facts, [])
    assert v.reason == "declared output plan.tasks is not complete"


def _gated_by_a_middle_node():
    nodes = {"design": pipeline.Node(name="design", role="R", brief_text="b", outputs=["plan.tasks"]),
             "gate": pipeline.Node(name="gate", role="R", brief_text="b"),
             "task": pipeline.Node(name="task", role="R", brief_text="b", repeat="plan.tasks")}
    edges = [pipeline.Edge(frm="design", to="gate"), pipeline.Edge(frm="gate", to="task"), pipeline.Edge(frm="task", to="task")]
    return pipeline.Schema(name="g", version=1, nodes=nodes, edges=edges)


def test_the_task_list_refusal_lands_on_the_producer_not_on_a_later_node():
    s = _gated_by_a_middle_node()
    facts = pipeline.Facts(artifacts={"plan.tasks": {"status": "complete", "scope": "output"}}, plan_tasks=[],
                           command_runner=None)
    at_design = pipeline.check_advance(s, pipeline.Token("t1", "design", 1, "d", "/w", "m"),
                                       pipeline.Caller("d", "K", "worker"), "gate", facts, [])
    assert not at_design.ok
    _assert_task_list_refusal(at_design.reason)
    at_gate = pipeline.check_advance(s, pipeline.Token("t1", "gate", 1, "g", "/w", "m"),
                                     pipeline.Caller("g", "K", "worker"), "task", facts, [])
    assert "overwrite" not in (at_gate.reason or "")


def test_a_task_list_no_node_repeats_over_does_not_block_its_producer():
    nodes = {"design": pipeline.Node(name="design", role="R", brief_text="b", outputs=["plan.tasks"]),
             "pr": pipeline.Node(name="pr", role="R", brief_text="b")}
    s = pipeline.Schema(name="g", version=1, nodes=nodes, edges=[pipeline.Edge(frm="design", to="pr")])
    facts = pipeline.Facts(artifacts={"plan.tasks": {"status": "complete", "scope": "output"}}, plan_tasks=None,
                           command_runner=None)
    assert pipeline.check_advance(s, pipeline.Token("t1", "design", 1, "d", "/w", "m"),
                                  pipeline.Caller("d", "K", "worker"), "pr", facts, []).ok


@pytest.mark.parametrize("plan,index,count", [(None, 1, 0), ([], 1, 0), ([{"id": 1}, {"id": 2}], 3, 2)])
def test_a_token_past_its_list_gets_one_refusal_on_every_edge(schema_dir, plan, index, count):
    s = pipeline.load_snapshot(pipeline.snapshot(pipeline.load_schema(schema_dir / "code-ticket.toml")))
    token = pipeline.Token("t1", "task", index, "a-task", "/w", "mgr")
    facts = pipeline.Facts(artifacts={}, plan_tasks=plan, command_runner=None)
    caller = pipeline.Caller("a-task", "K", "worker")
    reasons = {pipeline.check_advance(s, token, caller, to, facts, []).reason for to in ("task", "pr")}
    assert reasons == {f"index {index} is past the task list the engine read from plan.tasks ({count} readable tasks); ask_manager"}


def _gated(check, outputs=("a.out",)):
    nodes = {"a": pipeline.Node(name="a", role="R", brief_text="b", outputs=list(outputs)),
             "b": pipeline.Node(name="b", role="R", brief_text="b")}
    return pipeline.Schema(name="g", version=1, nodes=nodes, edges=[pipeline.Edge(frm="a", to="b", exit_checks=[check])])


def _gated_facts(runner=None):
    return pipeline.Facts(artifacts={"a.out": {"status": "complete", "scope": "output", "hash": "abc"}},
                          plan_tasks=None, command_runner=runner)


@pytest.mark.parametrize("failing,passing", [
    ({"type": "artifact", "name": "a.none"}, {"type": "artifact", "name": "a.out"}),
    ({"type": "value", "name": "a.out", "field": "hash", "equals": "zzz"},
     {"type": "value", "name": "a.out", "field": "hash", "equals": "abc"}),
    ({"type": "command", "run": ["false"]}, {"type": "command", "run": ["true"]}),
])
def test_check_advance_is_gated_by_each_exit_check_type(failing, passing):
    token = pipeline.Token("t1", "a", 1, "h", "/w", "m")
    caller = pipeline.Caller("h", "K", "worker")
    facts = _gated_facts(lambda argv, cwd, timeout: 0 if argv == ["true"] else 1)
    refused = pipeline.check_advance(_gated(failing), token, caller, "b", facts, [])
    assert not refused.ok and refused.reason.startswith("exit_check failed")
    assert pipeline.check_advance(_gated(passing), token, caller, "b", facts, []).ok


def test_check_advance_expands_exit_check_names_with_the_token_index():
    nodes = {"t": pipeline.Node(name="t", role="R", brief_text="b", repeat="plan.tasks"),
             "u": pipeline.Node(name="u", role="R", brief_text="b")}
    s = pipeline.Schema(name="g", version=1, nodes=nodes, edges=[
        pipeline.Edge(frm="t", to="t"),
        pipeline.Edge(frm="t", to="u", when="last", exit_checks=[{"type": "artifact", "name": "t.check-{i}"}])])
    token = pipeline.Token("t1", "t", 2, "h", "/w", "m")
    caller = pipeline.Caller("h", "K", "worker")

    def facts(name):
        return pipeline.Facts(artifacts={name: {"status": "complete"}}, plan_tasks=[{}, {}], command_runner=None)

    assert pipeline.check_advance(s, token, caller, "u", facts("t.check-2"), []).ok
    assert not pipeline.check_advance(s, token, caller, "u", facts("t.check-1"), []).ok


def test_check_advance_runs_commands_in_the_token_cwd_unless_given_one():
    seen = []
    facts = _gated_facts(lambda argv, cwd, timeout: seen.append(cwd) or 0)
    schema = _gated({"type": "command", "run": ["true"]})
    token = pipeline.Token("t1", "a", 1, "h", "/token-cwd", "m")
    caller = pipeline.Caller("h", "K", "worker")
    assert pipeline.check_advance(schema, token, caller, "b", facts, []).ok
    assert pipeline.check_advance(schema, token, caller, "b", facts, [], cwd="/given-cwd").ok
    assert seen == ["/token-cwd", "/given-cwd"]


@pytest.mark.parametrize("check,reason", [
    ({"type": "command", "run": "true\u0000"},
     "command ['sh', '-c', 'true\\x00'] could not start in {cwd} (missing binary or directory, or not executable)"),
    ({"type": "command", "run": ["true"], "timeout_sec": 10**10},
     "exit_check command `timeout_sec` must be a positive integer no larger than 86400"),
])
def test_a_command_exit_check_never_raises(tmp_path, check, reason):
    ok, why = pipeline.exit_check(check, _design_ready(), str(tmp_path), 5)
    assert not ok and why == reason.format(cwd=tmp_path)


def test_exit_check_takes_the_largest_timeout(tmp_path):
    seen = []
    facts = pipeline.Facts(artifacts={}, plan_tasks=[], command_runner=lambda argv, cwd, timeout: seen.append(timeout) or 0)
    assert pipeline.exit_check({"type": "command", "run": ["true"], "timeout_sec": 86400}, facts, str(tmp_path), 5) == (True, "")
    assert seen == [86400]


PROFILE_BASE = '''claudeMdExcludes = ["/h/.claude*/rules/a.md", "/h/.claude*/rules/b.md"]
skillListingMaxDescChars = 200

[skillOverrides]
"x-skill" = "off"
"anthropic-skills:pdf" = "on"
'''

PROFILE_NODE = '''claudeMdExcludes = ["/h/.claude*/rules/b.md", "/h/.claude*/rules/c.md"]
skillListingMaxDescChars = 160

[skillOverrides]
"x-skill" = "user-invocable-only"
'''


def _profiled(schema_dir, node=PROFILE_NODE, extra='extra_args = []'):
    (schema_dir / "profiles").mkdir(exist_ok=True)
    (schema_dir / "profiles" / "base.toml").write_text(PROFILE_BASE)
    (schema_dir / "profiles" / "design.toml").write_text(node)
    text = SCHEMA.replace('extra_args = []',
                          extra + '\nprofiles = ["profiles/base.toml", "profiles/design.toml"]', 1)
    (schema_dir / "code-ticket.toml").write_text(text)
    return pipeline.load_schema(schema_dir / "code-ticket.toml")


def test_validate_accepts_profiles_and_the_snapshot_carries_them_merged(schema_dir, tmp_path):
    s = _profiled(schema_dir)
    assert pipeline.validate(s, str(tmp_path)) == []
    data = tomllib.loads(pipeline.snapshot(s))
    assert data["nodes"]["design"]["profile_settings"] == {
        "claudeMdExcludes": ["/h/.claude*/rules/a.md", "/h/.claude*/rules/b.md", "/h/.claude*/rules/c.md"],
        "skillListingMaxDescChars": 160,
        "skillOverrides": {"x-skill": "user-invocable-only", "anthropic-skills:pdf": "on"},
    }
    assert "profiles" not in data["nodes"]["design"]
    assert "profile_settings" not in data["nodes"]["task"]


def test_a_profiled_snapshot_round_trips_and_is_idempotent(schema_dir):
    text = pipeline.snapshot(_profiled(schema_dir))
    again = pipeline.load_snapshot(text)
    assert again.nodes["design"].profile_settings["skillOverrides"]["anthropic-skills:pdf"] == "on"
    assert again.nodes["task"].profile_settings == {}
    assert pipeline.snapshot(again) == text


def test_a_profile_edit_after_the_snapshot_changes_nothing(schema_dir):
    text = pipeline.snapshot(_profiled(schema_dir))
    (schema_dir / "profiles" / "design.toml").write_text("skillListingMaxDescChars = 1\n")
    assert pipeline.load_snapshot(text).nodes["design"].profile_settings["skillListingMaxDescChars"] == 160


def test_a_schema_without_profiles_snapshots_no_profile_line(schema_dir):
    assert "profile_settings" not in pipeline.snapshot(pipeline.load_schema(schema_dir / "code-ticket.toml"))


@pytest.mark.parametrize("node,expected", [
    ("nope = 1\n", "unknown profile key 'nope'"),
    ('enabledMcpjsonServers = ["datadog"]\n', "unknown profile key 'enabledMcpjsonServers'"),
    ('disabledMcpjsonServers = "incident-mcp"\n', "`disabledMcpjsonServers` must be a list of non-empty strings"),
    ('disabledMcpjsonServers = [""]\n', "`disabledMcpjsonServers` must be a list of non-empty strings"),
    ('claudeMdExcludes = "x"\n', "claudeMdExcludes"),
    ('claudeMdExcludes = [""]\n', "claudeMdExcludes"),
    ('[skillOverrides]\n"x" = "hidden"\n', "skillOverrides"),
    ("skillListingMaxDescChars = 0\n", "skillListingMaxDescChars"),
    ("skillListingMaxDescChars = true\n", "skillListingMaxDescChars"),
    ("not toml [\n", "cannot be read as TOML"),
])
def test_validate_names_each_profile_defect(schema_dir, tmp_path, node, expected):
    problems = pipeline.validate(_profiled(schema_dir, node=node), str(tmp_path))
    assert any(expected in p for p in problems), problems


def test_validate_accepts_a_disabled_mcp_server_list_and_the_snapshot_carries_it(schema_dir, tmp_path):
    s = _profiled(schema_dir, node='disabledMcpjsonServers = ["incident-mcp"]\n')
    assert pipeline.validate(s, str(tmp_path)) == []
    data = tomllib.loads(pipeline.snapshot(s))
    assert data["nodes"]["design"]["profile_settings"]["disabledMcpjsonServers"] == ["incident-mcp"]


def test_validate_names_a_missing_profile_file(schema_dir, tmp_path):
    s = _profiled(schema_dir)
    (schema_dir / "profiles" / "design.toml").unlink()
    problems = pipeline.validate(s, str(tmp_path))
    assert any("profiles/design.toml" in p and "does not exist" in p for p in problems), problems


def test_validate_refuses_a_profiled_node_whose_settings_arg_is_no_json_object(schema_dir, tmp_path):
    missing = tmp_path / "nope.json"
    s = _profiled(schema_dir, extra=f'extra_args = ["--settings", "{missing}"]')
    assert any("--settings" in p for p in pipeline.validate(s, str(tmp_path)))
    good = tmp_path / "preset.json"
    good.write_text('{"permissions": {}}')
    s = _profiled(schema_dir, extra=f'extra_args = ["--settings", "{good}"]')
    assert pipeline.validate(s, str(tmp_path)) == []


def test_validate_checks_profile_settings_a_snapshot_carries(schema_dir, tmp_path):
    text = pipeline.snapshot(_profiled(schema_dir)).replace("skillListingMaxDescChars = 160", "bogus = 1")
    problems = pipeline.validate(pipeline.load_snapshot(text), str(tmp_path))
    assert any("unknown profile key 'bogus'" in p for p in problems), problems


def test_merge_settings_keeps_base_keys_unions_lists_and_leaves_the_base_alone():
    base = {"permissions": {"allow": ["A"]}, "claudeMdExcludes": ["x"]}
    merged = pipeline.merge_settings(base, {"claudeMdExcludes": ["x", "y"], "skillOverrides": {"s": "off"},
                                            "skillListingMaxDescChars": 9})
    assert merged == {"permissions": {"allow": ["A"]}, "claudeMdExcludes": ["x", "y"],
                      "skillOverrides": {"s": "off"}, "skillListingMaxDescChars": 9}
    assert base == {"permissions": {"allow": ["A"]}, "claudeMdExcludes": ["x"]}


def test_split_settings_arg_takes_both_spellings_and_leaves_the_rest():
    assert pipeline.split_settings_arg(["--effort", "x", "--settings", "/p"]) == (["--effort", "x"], "/p")
    assert pipeline.split_settings_arg(["--settings=/p", "--effort", "x"]) == (["--effort", "x"], "/p")
    assert pipeline.split_settings_arg(["--effort", "x"]) == (["--effort", "x"], None)


def test_read_settings_value_reads_a_file_or_inline_json_and_refuses_the_rest(tmp_path):
    f = tmp_path / "s.json"
    f.write_text('{"a": 1}')
    assert pipeline.read_settings_value(str(f)) == {"a": 1}
    assert pipeline.read_settings_value(' {"b": 2}') == {"b": 2}
    for bad in (str(tmp_path / "gone.json"), "[1]", "{not json"):
        with pytest.raises(ValueError):
            pipeline.read_settings_value(bad)


def test_read_settings_value_refuses_a_relative_path(tmp_path, monkeypatch):
    (tmp_path / "s.json").write_text('{"a": 1}')
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="absolute path or inline JSON"):
        pipeline.read_settings_value("s.json")


def test_validate_names_a_source_node_that_sets_both_profiles_and_profile_settings(schema_dir, tmp_path):
    inline = "profile_settings = { skillListingMaxDescChars = 5 }"
    s = _profiled(schema_dir, extra=inline)
    problems = pipeline.validate(s, str(tmp_path))
    assert any("[nodes.design]" in p and "`profiles` and `profile_settings`" in p for p in problems), problems
    (schema_dir / "code-ticket.toml").write_text(SCHEMA.replace('checkpoints = ["design.explainer"]',
                                                                'checkpoints = ["design.explainer"]\n' + inline, 1))
    assert pipeline.validate(pipeline.load_schema(schema_dir / "code-ticket.toml"), str(tmp_path)) == []


EXPLAIN_SCHEMA = SCHEMA.replace(
    '[nodes.design]',
    '[nodes.explain]\nrole = "SCOUT_DESIGN"\nmodel = "@opus"\neffort = "xhigh"\nbrief = "briefs/explain.md"\ninputs = []\n'
    'outputs = ["context.ask", "explain.validation"]\n\n[nodes.design]',
).replace(
    'inputs = []\noutputs = ["context.ask", "design.hld", "plan.tasks"]',
    'inputs = ["explain.validation"]\noutputs = ["design.hld", "plan.tasks"]',
) + '[[edges]]\nfrom = "explain"\nto = "design"\n'


def _started(node="design"):
    return {"type": "pipeline_started", "token_id": "t1", "node": node, "index": 1, "holder": None, "cwd": "/w",
            "manager": "m"}


def test_fold_counts_rounds_and_exposes_the_superseded_requests():
    events = [
        _started(),
        {"type": "advance_requested", "token_id": "t1", "request_id": "r1", "edge": {"from": None, "to": "design"},
         "index_to": 1, "kind": "advance"},
        {"type": "round_started", "token_id": "t1", "round": 2, "to": "design", "ask": "context.rework-2",
         "inputs": []},
        {"type": "token_override", "token_id": "t1", "from": "design", "to": "design", "index": 1, "manager": "m"},
        {"type": "advance_requested", "token_id": "t1", "request_id": "r2", "edge": {"from": "design", "to": "design"},
         "index_to": 1, "kind": "advance"},
    ]
    f = pipeline.fold(events)
    assert f.round == 2 and f.round_event["ask"] == "context.rework-2"
    assert f.superseded == {"r1"}
    assert [r["request_id"] for r in f.open_requests] == ["r2"]
    first = pipeline.fold(events[:2])
    assert (first.round, first.round_event, first.superseded) == (1, None, set())


def test_the_carry_set_keeps_the_asks_named_inputs_and_what_the_round_does_not_redo():
    s = pipeline.load_snapshot(SCHEMA)
    present = {"context.ask", "context.rework-2", "design.hld", "plan.tasks", "design.explainer", "review.task-1",
               "review.task-2", "implement.repo", "summary.post-merge", "decision.q-1", "investigate.findings",
               "context.notes"}
    assert pipeline.carried_names(s, "task", present, ["investigate.findings"]) == {
        "context.ask", "context.rework-2", "design.hld", "plan.tasks", "design.explainer", "investigate.findings"}
    assert pipeline.carried_names(s, "design", present, ["decision.q-1"]) == {
        "context.ask", "context.rework-2", "decision.q-1"}


def test_the_carry_set_always_keeps_the_works_running_summary():
    s = pipeline.load_snapshot(SCHEMA)
    present = {"context.ask", "design.hld", "summary.thread", "summary.post-merge"}
    assert pipeline.carried_names(s, "design", present, []) == {"context.ask", "summary.thread"}


def test_rework_problems_name_the_node_to_enter_at_when_a_carried_input_is_missing():
    s = pipeline.load_snapshot(EXPLAIN_SCHEMA)
    stamps = {n: {"status": "complete"} for n in ("context.ask", "design.hld", "plan.tasks", "implement.repo")}
    assert pipeline.rework_problems(s, "design", stamps, []) == [
        "node 'design' needs 'explain.validation', which no node this round redoes produces and the store does "
        "not carry complete; enter at 'explain' or earlier"]
    assert pipeline.rework_problems(s, "explain", stamps, []) == []


def test_rework_problems_accept_an_input_that_names_a_redone_nodes_checkpoint():
    s = pipeline.load_snapshot(SCHEMA.replace('inputs = ["implement.repo"]', 'inputs = ["implement.repo", "design.explainer"]'))
    stamps = {n: {"status": "complete"} for n in ("context.ask", "design.hld", "plan.tasks", "implement.repo",
                                                   "design.explainer", "summary.post-merge")}
    assert pipeline.rework_problems(s, "design", stamps, []) == []
    assert pipeline.rework_problems(s, "task", stamps, []) == []


def test_rework_problems_refuse_a_partial_carried_input():
    s = pipeline.load_snapshot(SCHEMA)
    stamps = {"context.ask": {"status": "complete"}, "design.hld": {"status": "partial"},
              "plan.tasks": {"status": "complete"}}
    assert pipeline.rework_problems(s, "task", stamps, []) == [
        "node 'task' needs 'design.hld', which no node this round redoes produces and the store does not carry "
        "complete; enter at 'design' or earlier"]


def test_rework_problems_refuse_inputs_that_are_absent_or_a_redone_nodes_own_work():
    s = pipeline.load_snapshot(SCHEMA)
    stamps = {n: {"status": "complete"} for n in ("context.ask", "design.hld", "plan.tasks", "implement.repo")}
    assert pipeline.rework_problems(s, "design", stamps, ["investigate.findings"]) == [
        "input 'investigate.findings' is not in the store"]
    assert pipeline.rework_problems(s, "task", stamps, ["implement.repo"]) == [
        "input 'implement.repo' is an output or checkpoint of a node this round redoes; carried, it would count "
        "as the round's own work"]
    assert pipeline.rework_problems(s, "nowhere", stamps, [])[0].startswith("'nowhere' is not a node of the schema")


def test_the_carry_set_keeps_numbered_outputs_of_a_node_the_round_does_not_redo():
    s = pipeline.load_snapshot(SCHEMA)
    present = {"context.ask", "design.hld", "plan.tasks", "review.task-1", "review.task-2", "implement.repo"}
    assert pipeline.carried_names(s, "pr", present, []) == {
        "context.ask", "design.hld", "plan.tasks", "review.task-1", "review.task-2"}


def test_rework_problems_refuse_a_numbered_output_of_a_redone_node_as_an_input():
    s = pipeline.load_snapshot(SCHEMA)
    stamps = {n: {"status": "complete"} for n in ("context.ask", "design.hld", "plan.tasks", "review.task-1")}
    assert pipeline.rework_problems(s, "task", stamps, ["review.task-1"]) == [
        "input 'review.task-1' is an output or checkpoint of a node this round redoes; carried, it would count "
        "as the round's own work"]


def _brief_node():
    return pipeline.Node(name="design", role="R", brief_text="B {inputs}", outputs=["design.hld"])


def test_a_round_one_footer_prints_tools_inputs_outputs_and_edges():
    tok = pipeline.Token("t1", "design", 1, None, "/w", "mgr")
    text = pipeline.compose_brief(_brief_node(), tok, "K", ["context.ask"], [], ["decision.q-1"], [])
    assert text == (
        "B context.ask, decision.q-1\n---\n[pipeline] task_key=K node=design index=1 cwd=/w\n"
        'Tools: in your first message, beside your first Reads, call ToolSearch(query="select:'
        'mcp__dockwright__artifact_get,mcp__dockwright__artifact_put,mcp__dockwright__worker_done", max_results=3). '
        "If it answers that a server is still connecting, call it again beside your next calls.\n"
        "Inputs (read ONLY these. Read the copy after each name for your first read of it; call artifact_get(task_key, "
        "phase, name, reader_sid=<your sid>) for a name with no copy, a copy that is gone, and every later read. A "
        "read_set entry takes the written_at and contract_hash shown): context.ask (not written); decision.q-1 (not "
        "written)\n"
        'Outputs you must publish with artifact_put(..., scope="output", status="complete"), the puts you have ready '
        "in one message, one call each: design.hld\n"
        'Checkpoints (scope="checkpoint"): (none)\n'
        "Edges out of this node: (terminal node)\n"
        + pipeline._decision_line() + "\n"
        + pipeline._summary_line()
        + "\nThis is a terminal node: publish your outputs, then call worker_done.")


def test_the_inputs_line_names_each_copy_and_each_unwritten_input():
    tok = pipeline.Token("t1", "design", 1, None, "/w", "mgr")
    copies = {"context.ask": pipeline.InputCopy("/x/ask.md", 12.5, None, "complete"),
              "design.hld": pipeline.InputCopy("/x/hld.md", 13.0, "h1", "partial"),
              "decision.q-1": pipeline.InputCopy(None, 14.0)}
    text = pipeline.compose_brief(_brief_node(), tok, "K", ["context.ask", "design.hld", "intake.route"], [],
                                  ["decision.q-1"], [], copies=copies)
    inputs_line = next(line for line in text.splitlines() if line.startswith("Inputs"))
    assert inputs_line.endswith(
        "context.ask = /x/ask.md written_at=12.5; design.hld = /x/hld.md written_at=13.0 contract_hash=h1 "
        "status=partial; intake.route (not written); decision.q-1 (no copy)")


def _tools_line(text):
    return next(line for line in text.splitlines() if line.startswith("Tools: "))


def test_the_tools_line_names_the_nodes_protocol_tools():
    tok = pipeline.Token("t1", "design", 1, None, "/w", "mgr")
    with_children = pipeline.Node(name="design", role="R", brief_text="B", outputs=["design.hld"],
                                  children={"reviewer": pipeline.ChildSlot(name="reviewer")})
    advancing = pipeline.compose_brief(with_children, tok, "K", [], [], [], [pipeline.Edge("design", "plan")])
    names = ["artifact_get", "artifact_put", "worker_done", "pipeline_advance", "pipeline_child", "wait_for_child"]
    assert _tools_line(advancing).startswith(
        'Tools: in your first message, beside your first Reads, call ToolSearch(query="select:'
        + ",".join(pipeline.TOOL_PREFIX + n for n in names) + '", max_results=6). ')
    terminal = pipeline.compose_brief(_brief_node(), tok, "K", [], [], [], [])
    assert ('query="select:mcp__dockwright__artifact_get,mcp__dockwright__artifact_put,mcp__dockwright__worker_done", '
            "max_results=3)") in _tools_line(terminal)


def test_the_no_rules_summary_line_lets_the_summary_ride_the_last_batch():
    assert pipeline._summary_line().endswith(" It may be the last call of the message that puts your final records.")


RULES_FILE = "# Node rules\n\n## decided_by on decision records\n\nx\n\n## The running summary\n\ny\n"


@pytest.mark.parametrize("text", [None, "# Node rules\n\n## The running summary\n",
                                  "## decided_by on decision records\n\n### The running summary\n"])
def test_a_node_rules_file_that_is_missing_or_lacks_a_section_leaves_the_footer_unchanged(tmp_path, monkeypatch, text):
    rules = tmp_path / "pipeline-node.md"
    if text is not None:
        rules.write_text(text)
    monkeypatch.setattr(pipeline.config, "pipeline_node_rules", lambda: rules)
    tok = pipeline.Token("t1", "design", 1, None, "/w", "mgr")
    with_key = pipeline.compose_brief(_brief_node(), tok, "K", ["context.ask"], [], ["decision.q-1"], [])
    monkeypatch.setattr(pipeline.config, "pipeline_node_rules", lambda: None)
    assert with_key == pipeline.compose_brief(_brief_node(), tok, "K", ["context.ask"], [], ["decision.q-1"], [])


def test_a_footer_with_node_rules_names_the_file_first_and_points_at_its_two_sections(tmp_path, monkeypatch):
    rules = tmp_path / "pipeline-node.md"
    rules.write_text(RULES_FILE)
    monkeypatch.setattr(pipeline.config, "pipeline_node_rules", lambda: rules)
    tok = pipeline.Token("t1", "design", 1, None, "/w", "mgr")
    text = pipeline.compose_brief(_brief_node(), tok, "K", ["context.ask"], [], ["decision.q-1"], [], round_no=2)
    assert text.splitlines()[2:4] == ["[pipeline] task_key=K node=design index=1 cwd=/w round=2",
                                      f"Read {rules} before anything else."]
    assert text.splitlines()[-3:] == [
        "If you publish a decision.* record, pass decided_by to artifact_put and rank decisions as "
        f"{rules} § decided_by on decision records says.",
        "Your last store write before you leave the node is the work's running summary, summary.thread, written "
        f"as {rules} § The running summary says.",
        "This is a terminal node: publish your outputs, then call worker_done."]


def test_a_rework_rounds_footer_asks_for_the_same_summary():
    tok = pipeline.Token("t1", "design", 1, None, "/w", "mgr")
    text = pipeline.compose_brief(_brief_node(), tok, "K", ["context.ask"], [], [], [], round_no=2)
    assert text.splitlines()[-2] == pipeline._summary_line()


def test_a_round_two_footer_names_the_round_ask_the_branch_rule_and_each_input_once():
    node = pipeline.Node(name="design", role="R", brief_text="B {round} {inputs}", outputs=["design.hld"])
    tok = pipeline.Token("t1", "design", 1, None, "/w", "mgr")
    text = pipeline.compose_brief(node, tok, "K", ["context.ask"], [], ["decision.q-keep"], [], round_no=2,
                                  round_inputs=["context.rework-2", "decision.q-keep", "investigate.findings"])
    assert text.startswith("B 2 context.ask, context.rework-2, investigate.findings, decision.q-keep\n")
    assert "\n[pipeline] task_key=K node=design index=1 cwd=/w round=2\n" in text
    assert ("Round 2 of this work is a rework. context.rework-2 is this round's ask: where it differs from anything "
            "written before this round") in text
    assert ("If /w is on a branch whose pull request has merged, move to a new branch off origin/main before you "
            "change anything there.") in text
    inputs_line = next(line for line in text.splitlines() if line.startswith("Inputs"))
    assert inputs_line.endswith("context.ask (not written); context.rework-2 (not written); investigate.findings "
                                "(not written); decision.q-keep (not written)")


@pytest.mark.parametrize("body,expected", [
    ("https://github.com/o/a/pull/1\nhttps://github.com/o/b/pull/2\nbranch x @ abc",
     ["https://github.com/o/a/pull/1", "https://github.com/o/b/pull/2"]),
    ("https://github.com/o/a/pull/1, replacing https://github.com/o/b/pull/2",
     ["https://github.com/o/a/pull/1"]),
    ("https://github.com/o/a/pull/1\nsee https://github.com/o/b/pull/2",
     ["https://github.com/o/a/pull/1", "https://github.com/o/b/pull/2"]),
    ("PR: https://github.com/o/a/pull/1 and https://github.com/o/b/pull/2", ["https://github.com/o/a/pull/1"]),
    ("no link here", []),
    ("", []),
    (None, []),
    ("- https://github.com/o/a/pull/1\n- https://github.com/o/b/pull/2",
     ["https://github.com/o/a/pull/1", "https://github.com/o/b/pull/2"]),
    ("* https://github.com/o/a/pull/1\n+ https://github.com/o/b/pull/2",
     ["https://github.com/o/a/pull/1", "https://github.com/o/b/pull/2"]),
    ("  https://github.com/o/a/pull/1\n  https://github.com/o/b/pull/2",
     ["https://github.com/o/a/pull/1", "https://github.com/o/b/pull/2"]),
    ("\nhttps://github.com/o/a/pull/1\nhttps://github.com/o/b/pull/2",
     ["https://github.com/o/a/pull/1", "https://github.com/o/b/pull/2"]),
    ("https://github.com/o/a/pull/1\n\nhttps://github.com/o/b/pull/2",
     ["https://github.com/o/a/pull/1", "https://github.com/o/b/pull/2"]),
    ("1. https://github.com/o/a/pull/1\n2. https://github.com/o/b/pull/2",
     ["https://github.com/o/a/pull/1", "https://github.com/o/b/pull/2"]),
    ("1) https://github.com/o/a/pull/1\n2) https://github.com/o/b/pull/2\nbranch x",
     ["https://github.com/o/a/pull/1", "https://github.com/o/b/pull/2"]),
    ("## PRs\nhttps://github.com/o/a/pull/1\nhttps://github.com/o/b/pull/2",
     ["https://github.com/o/a/pull/1", "https://github.com/o/b/pull/2"]),
    ("PRs:\n- https://github.com/o/a/pull/1\n- https://github.com/o/b/pull/2",
     ["https://github.com/o/a/pull/1", "https://github.com/o/b/pull/2"]),
    ("- https://github.com/o/a/pull/1\n- https://github.com/o/b/pull/2\n- https://github.com/o/a/pull/1",
     ["https://github.com/o/a/pull/1", "https://github.com/o/b/pull/2"]),
    ("https://github.com/o/a/pull/1\n\nhttps://github.com/o/c/pull/9 closed",
     ["https://github.com/o/a/pull/1", "https://github.com/o/c/pull/9"]),
    ("https://github.com/o/a/pull/1\nbranch x @ abc\nhttps://github.com/o/c/pull/9 closed",
     ["https://github.com/o/a/pull/1", "https://github.com/o/c/pull/9"]),
    ("- - https://github.com/o/a/pull/1\nhttps://github.com/o/b/pull/2",
     ["https://github.com/o/a/pull/1", "https://github.com/o/b/pull/2"]),
    ("| [#1](https://github.com/o/a/pull/1) |\n| [#2](https://github.com/o/b/pull/2) |",
     ["https://github.com/o/a/pull/1", "https://github.com/o/b/pull/2"]),
    ("| repo | PR |\n|---|---|\n| a | [#1](https://github.com/o/a/pull/1) |\n"
     "| b | [#2](https://github.com/o/b/pull/2) |",
     ["https://github.com/o/a/pull/1", "https://github.com/o/b/pull/2"]),
    ("- Orchestrator: https://github.com/o/a/pull/1 (engine)\n- Configs: https://github.com/o/b/pull/2",
     ["https://github.com/o/a/pull/1", "https://github.com/o/b/pull/2"]),
    ("Alpha (PR https://github.com/o/a/pull/1, contracts):\nBeta (PR https://github.com/o/b/pull/2, consumer):",
     ["https://github.com/o/a/pull/1", "https://github.com/o/b/pull/2"]),
    ("- owner/repo **#20** — https://github.com/o/a/pull/20 — note", ["https://github.com/o/a/pull/20"]),
])
def test_pr_urls_reads_the_first_pr_url_of_every_line(body, expected):
    assert pipeline.pr_urls(body) == expected


def test_a_hold_is_set_by_token_held_and_ended_by_a_release_a_commit_or_an_override():
    base = [_ev("pipeline_started", node="design", index=1, holder="a-design", cwd="/w", manager="mgr"),
            _ev("token_held", node="design", reason="wait for the reporter", actor_sid="m")]
    assert pipeline.fold(base).held == {"t1": "wait for the reporter"}
    assert pipeline.fold(base + [_ev("token_released", node="design", actor_sid="m")]).held == {}
    committed = base + [_ev("advance_requested", request_id="r1", edge={"from": "design", "to": "task"}, index_to=1),
                        _ev("advance_committed", request_id="r1", node="task", index=1, holder="a-task", sid="s")]
    assert pipeline.fold(committed).held == {}
    overridden = base + [_ev("token_override", **{"from": "design"}, to="design", index=1, reason="redo",
                             actor_sid="m")]
    assert pipeline.fold(overridden).held == {}
    assert {"token_held", "token_released"} <= pipeline.RESERVED_EVENT_TYPES


FORK = (SCHEMA.replace('from = "design"\nto = "task"\n', 'from = "design"\nto = "task"\nfork = true\n')
        .replace('to = "pr"\nwhen = "last"\n', 'to = "pr"\nwhen = "last"\njoin = true\n'))

REDO_EDGES = '''
[[edges]]
from = "pr"
to = "design"
redo = true
[[edges]]
from = "task"
to = "design"
redo = true
'''
REDO = SCHEMA + REDO_EDGES
FORK_REDO = FORK + REDO_EDGES


def _redo_snapshot(schema_dir, text=REDO):
    (schema_dir / "redo.toml").write_text(text)
    return pipeline.load_snapshot(pipeline.snapshot(pipeline.load_schema(schema_dir / "redo.toml")))


def test_a_redo_edge_loads_validates_and_snapshots_apart_from_the_forward_graph(schema_dir, tmp_path):
    (schema_dir / "redo.toml").write_text(REDO)
    s = pipeline.load_schema(schema_dir / "redo.toml")
    assert pipeline.validate(s, str(tmp_path)) == []
    assert [(e.frm, e.to) for e in s.edges] == [("design", "task"), ("task", "task"), ("task", "pr"),
                                                ("pr", "post-merge")]
    assert [(e.frm, e.to, e.redo) for e in s.redo_edges] == [("pr", "design", True), ("task", "design", True)]
    again = pipeline.load_snapshot(pipeline.snapshot(s))
    assert [(e.frm, e.to) for e in again.redo_edges] == [("pr", "design"), ("task", "design")]
    assert pipeline.start_node(again) == "design"


def test_a_schema_without_redo_edges_writes_no_redo_key(schema_dir):
    s = pipeline.load_schema(schema_dir / "code-ticket.toml")
    assert s.redo_edges == [] and "redo" not in pipeline.snapshot(s)


@pytest.mark.parametrize("edge,expected", [
    ('from = "design"\nto = "pr"\nredo = true', "design is not reachable from pr"),
    ('from = "pr"\nto = "pr"\nredo = true', "never to its own source"),
    ('from = "pr"\nto = "design"\nredo = true\ntrigger = "merge"', "cannot carry `trigger`"),
    ('from = "pr"\nto = "design"\nredo = true\nwhen = "last"', "cannot carry `when`"),
    ('from = "pr"\nto = "design"\nredo = true\nfork = true', "cannot carry `fork` or `join`"),
    ('from = "pr"\nto = "design"\nredo = true\nexit_checks = [{ type = "artifact", name = "plan.tasks" }]',
     "cannot carry `exit_checks`"),
    ('from = "pr"\nto = "task"\nredo = true', "cannot enter the repeat node task"),
    ('from = "pr"\nto = "nowhere"\nredo = true', "unknown node"),
    ('from = "post-merge"\nto = "pr"\nredo = true\n[[edges]]\nfrom = "post-merge"\nto = "pr"\nredo = true',
     "duplicate edge"),
])
def test_validate_names_each_redo_edge_defect(schema_dir, tmp_path, edge, expected):
    (schema_dir / "bad.toml").write_text(SCHEMA + "\n[[edges]]\n" + edge + "\n")
    problems = pipeline.validate(pipeline.load_schema(schema_dir / "bad.toml"), str(tmp_path))
    assert any(expected in p for p in problems), problems


def test_a_redo_edge_cannot_enter_a_fork_target(schema_dir, tmp_path):
    (schema_dir / "bad.toml").write_text(FORK + '\n[[edges]]\nfrom = "pr"\nto = "task"\nredo = true\n')
    problems = pipeline.validate(pipeline.load_schema(schema_dir / "bad.toml"), str(tmp_path))
    assert any("task is a fork target: a redo edge cannot enter it" in p for p in problems), problems


def test_a_fork_schema_with_redo_edges_validates(schema_dir, tmp_path):
    (schema_dir / "f.toml").write_text(FORK_REDO)
    assert pipeline.validate(pipeline.load_schema(schema_dir / "f.toml"), str(tmp_path)) == []


def test_a_redo_edge_changes_neither_upstream_outputs_nor_what_a_rework_carries(schema_dir):
    plain = pipeline.load_schema(schema_dir / "code-ticket.toml")
    (schema_dir / "redo.toml").write_text(REDO)
    redo = pipeline.load_schema(schema_dir / "redo.toml")
    present = {"context.ask", "design.hld", "plan.tasks", "review.task-1", "implement.repo", "summary.post-merge"}
    for name in plain.nodes:
        assert pipeline._upstream_outputs(redo, name) == pipeline._upstream_outputs(plain, name)
        assert pipeline.carried_names(redo, name, present, []) == pipeline.carried_names(plain, name, present, [])


def test_fork_and_join_keys_load_validate_and_snapshot(schema_dir, tmp_path):
    (schema_dir / "fork.toml").write_text(FORK)
    s = pipeline.load_schema(schema_dir / "fork.toml")
    assert pipeline.validate(s, str(tmp_path)) == []
    assert [(e.frm, e.to, e.fork, e.join) for e in s.edges][0] == ("design", "task", True, False)
    assert [(e.frm, e.to, e.fork, e.join) for e in s.edges][2] == ("task", "pr", False, True)
    again = pipeline.load_snapshot(pipeline.snapshot(s))
    assert again.edges[0].fork is True and again.edges[2].join is True
    plain = pipeline.load_schema(schema_dir / "code-ticket.toml")
    text = pipeline.snapshot(plain)
    assert "fork" not in text and "join" not in text


@pytest.mark.parametrize("edit,replacement,expected", [
    ('to = "pr"\nwhen = "last"\njoin = true\n', 'to = "pr"\nwhen = "last"\n', "entered by a fork edge and has no join edge out"),
    ('to = "task"\nfork = true\n', 'to = "task"\nfork = true\njoin = true\n', "`join` needs a repeat node as its source"),
    ('trigger = "merge"', 'trigger = "merge"\nfork = true', "a trigger edge cannot carry `fork` or `join`"),
    ('from = "task"\nto = "task"\n', 'from = "task"\nto = "task"\nfork = true\n', "a self-loop cannot carry `fork` or `join`"),
    ('to = "pr"\nwhen = "last"\njoin = true\n', 'to = "pr"\njoin = true\n', "`join` needs a repeat node as its source and when = \"last\""),
    ('to = "pr"\nwhen = "last"\njoin = true\n', 'to = "pr"\nwhen = "last"\njoin = true\nfork = true\n',
     "an edge cannot carry both `fork` and `join`"),
    ('to = "pr"\nwhen = "last"\njoin = true\n', 'to = "pr"\nwhen = "last"\nfork = true\n',
     "`fork` needs a repeat node as its target"),
    ('trigger = "merge"\n', 'trigger = "merge"\n[[edges]]\nfrom = "pr"\nto = "task"\n',
     "task is a fork target: only fork edges and its self-loop enter it"),
    ('trigger = "merge"\n', 'trigger = "merge"\n[[edges]]\nfrom = "post-merge"\nto = "task"\ntrigger = "merge"\n',
     "task is a fork target: only fork edges and its self-loop enter it"),
    ('trigger = "merge"\n', 'trigger = "merge"\n[[edges]]\nfrom = "task"\nto = "post-merge"\n',
     "[[edges]] task -> post-merge: [nodes.task] is entered by a fork edge, so it leaves only by its self-loop "
     "or a join edge"),
    ('trigger = "merge"\n', 'trigger = "merge"\n[[edges]]\nfrom = "task"\nto = "post-merge"\ntrigger = "merge"\n',
     "[[edges]] task -> post-merge: [nodes.task] is entered by a fork edge, so it leaves only by its self-loop "
     "or a join edge"),
    ('trigger = "merge"\n', 'trigger = "merge"\n[[edges]]\nfrom = "task"\nto = "post-merge"\nfork = true\n',
     "[[edges]] task -> post-merge: [nodes.task] is entered by a fork edge, so it leaves only by its self-loop "
     "or a join edge"),
])
def test_validate_names_each_fork_join_defect(schema_dir, tmp_path, edit, replacement, expected):
    assert edit in FORK
    (schema_dir / "bad.toml").write_text(FORK.replace(edit, replacement))
    problems = pipeline.validate(pipeline.load_schema(schema_dir / "bad.toml"), str(tmp_path))
    assert any(expected in p for p in problems), problems


def test_a_fork_target_may_leave_by_two_join_edges(schema_dir, tmp_path):
    two = FORK.replace('trigger = "merge"\n',
                       'trigger = "merge"\n[[edges]]\nfrom = "task"\nto = "post-merge"\nwhen = "last"\njoin = true\n')
    (schema_dir / "two.toml").write_text(two)
    assert pipeline.validate(pipeline.load_schema(schema_dir / "two.toml"), str(tmp_path)) == []


@pytest.mark.parametrize("body,readable", [
    ('{"tasks": [{"id": 1, "row": "a"}, {"id": 2, "row": "b", "stage": 2}]}', True),
    ('{"tasks": [{"id": 1, "row": "a"}, {"id": 2}]}', False),
    ('{"tasks": [{"id": 1, "row": ""}]}', False),
    ('{"tasks": [{"id": 1, "row": "a", "stage": 1}, {"id": 2, "row": "a", "stage": 2}]}', False),
    ('{"tasks": [{"id": 1, "row": "a", "stage": 0}]}', False),
    ('{"tasks": [{"id": 1, "row": "a", "stage": true}]}', False),
])
def test_read_task_list_refuses_inconsistent_rows(body, readable):
    assert bool(pipeline.read_task_list(body)) is readable


def test_task_rows_group_global_indexes_by_row_in_first_appearance_order():
    tasks = [{"row": "a"}, {"row": "b", "stage": 2}, {"row": "a"}]
    assert pipeline.task_rows(tasks) == [{"row": "a", "stage": 1, "indexes": [1, 3]},
                                         {"row": "b", "stage": 2, "indexes": [2]}]
    assert pipeline.task_rows([{"id": 1}, {"id": 2}]) == [{"row": None, "stage": 1, "indexes": [1, 2]}]


def _row_facts(tasks):
    return pipeline.Facts(artifacts={"review.task-1": {"status": "complete", "scope": "output"},
                                     "review.task-3": {"status": "complete", "scope": "output"}},
                          plan_tasks=tasks, command_runner=None)


def test_a_row_token_advances_within_its_row_and_leaves_only_by_the_join(schema_dir):
    (schema_dir / "fork.toml").write_text(FORK)
    snap = pipeline.load_snapshot(pipeline.snapshot(pipeline.load_schema(schema_dir / "fork.toml")))
    facts = _row_facts([{"row": "a"}, {"row": "b"}, {"row": "a"}])
    tok = pipeline.Token("t1/a", "task", 1, "h", "/w", "mgr", "a")
    caller = pipeline.Caller("h", "K", "worker")
    v = pipeline.check_advance(snap, tok, caller, "task", facts, [])
    assert v.ok and v.index_to == 3
    assert "take the edge marked when=\"last\"" in pipeline.check_advance(
        snap, pipeline.Token("t1/a", "task", 3, "h", "/w", "mgr", "a"), caller, "task", facts, []).reason
    assert pipeline.check_advance(snap, pipeline.Token("t1/a", "task", 3, "h", "/w", "mgr", "a"), caller, "pr",
                                  facts, []).ok
    assert "needs the last index" in pipeline.check_advance(snap, tok, caller, "pr", facts, []).reason


def test_the_fold_tracks_a_fork_its_rows_and_their_retirement():
    events = [
        {"type": "pipeline_started", "token_id": "t1", "node": "design", "index": 1, "holder": "h0", "cwd": "/w",
         "manager": "mgr"},
        {"type": "token_forked", "token_id": "t1", "node": "task",
         "rows": [{"row": "a", "stage": 1, "token_id": "t1/a"}, {"row": "b", "stage": 2, "token_id": "t1/b"}]},
        {"type": "advance_requested", "token_id": "t1/a", "request_id": "r1", "edge": {"from": "design", "to": "task"},
         "index_to": 1, "kind": "fork", "row": "a", "cwd": "/w"},
        {"type": "advance_committed", "token_id": "t1/a", "request_id": "r1", "node": "task", "index": 1,
         "holder": "ha"},
    ]
    f = pipeline.fold(events)
    assert f.tokens["t1"].forked is True and f.tokens["t1"].holder is None and f.tokens["t1"].node == "task"
    a = f.tokens["t1/a"]
    assert (a.row, a.cwd, a.manager, a.holder, a.index) == ("a", "/w", "mgr", "ha", 1)
    assert f.fork["rows"] == {"a": {"stage": 1, "token_id": "t1/a", "state": "started"},
                              "b": {"stage": 2, "token_id": "t1/b", "state": "pending"}}
    f = pipeline.fold(events + [{"type": "token_retired", "token_id": "t1/a"}])
    assert "t1/a" not in f.tokens and f.fork["rows"]["a"]["state"] == "done"
    f = pipeline.fold(events + [
        {"type": "token_retired", "token_id": "t1/a"},
        {"type": "advance_requested", "token_id": "t1", "request_id": "r9", "edge": {"from": "task", "to": "pr"},
         "index_to": 1, "kind": "advance"},
        {"type": "advance_committed", "token_id": "t1", "request_id": "r9", "node": "pr", "index": 1, "holder": "hp"}])
    assert f.tokens["t1"].forked is False and f.tokens["t1"].node == "pr" and f.fork is None
    held_first = [events[0], {"type": "token_held", "token_id": "t1", "node": "design", "reason": "wait"}, *events[1:]]
    assert pipeline.fold(held_first).held == {}


def test_an_override_of_t1_removes_every_row_and_supersedes_a_pended_row_request():
    events = [
        {"type": "pipeline_started", "token_id": "t1", "node": "design", "index": 1, "holder": "h0", "cwd": "/w",
         "manager": "mgr"},
        {"type": "token_forked", "token_id": "t1", "node": "task",
         "rows": [{"row": "a", "stage": 1, "token_id": "t1/a"}, {"row": "b", "stage": 1, "token_id": "t1/b"}]},
        {"type": "advance_requested", "token_id": "t1/a", "request_id": "r1", "edge": {"from": "design", "to": "task"},
         "index_to": 1, "kind": "fork", "row": "a", "cwd": "/w"},
        {"type": "advance_committed", "token_id": "t1/a", "request_id": "r1", "node": "task", "index": 1,
         "holder": "ha"},
        {"type": "advance_requested", "token_id": "t1/b", "request_id": "r2", "edge": {"from": "design", "to": "task"},
         "index_to": 2, "kind": "fork", "row": "b", "cwd": "/w"},
        {"type": "advance_pending", "request_id": "r2", "reason": "spawn-error", "assignment_id": None},
        {"type": "token_override", "token_id": "t1", "from": "task", "to": "design", "index": 1, "reason": "redo"},
    ]
    f = pipeline.fold(events)
    assert list(f.tokens) == ["t1"] and f.open_requests == [] and f.fork is None
    assert f.tokens["t1"].forked is False and f.tokens["t1"].node == "design"


def test_the_new_events_are_reserved():
    assert {"token_forked", "token_retired"} <= pipeline.RESERVED_EVENT_TYPES


def test_a_fork_target_is_entered_directly_only_with_one_row(schema_dir):
    (schema_dir / "fork.toml").write_text(FORK)
    snap = pipeline.load_snapshot(pipeline.snapshot(pipeline.load_schema(schema_dir / "fork.toml")))
    two = [{"row": "a"}, {"row": "b"}]
    assert pipeline.fork_entry_problem(snap, "task", two) == (
        "task is entered by a fork edge and plan.tasks has 2 rows: enter at design or earlier")
    assert pipeline.fork_entry_problem(snap, "task", [{"id": 1}, {"id": 2}]) is None
    assert pipeline.fork_entry_problem(snap, "task", None) is None
    assert pipeline.fork_entry_problem(snap, "design", two) is None


def test_a_footer_with_rulings_names_them_right_after_the_inputs(schema_dir):
    s = pipeline.load_snapshot(pipeline.snapshot(pipeline.load_schema(schema_dir / "code-ticket.toml")))
    token = pipeline.Token("t1", "design", 1, None, "/w", "mgr")
    brief = pipeline.compose_brief(s.nodes["design"], token, "K", ["context.ask"], [], [],
                                   [e for e in s.edges if e.frm == "design"], rulings=("fleet-meta", "meta"))
    lines = brief.splitlines()
    i = next(n for n, line in enumerate(lines) if line.startswith("Inputs (read ONLY these"))
    assert lines[i + 1] == ('Domain rulings (an input; tier 2 of the decision tiers below): '
                            'artifact_get(task_key="fleet-meta", phase="decisions", name="meta", '
                            "reader_sid=<your sid>) — the one store read whose task_key is not this work's.")
    assert lines[i + 2].startswith("Outputs you must publish")
    assert lines[-3] == pipeline._decision_line()


def test_the_holder_takes_a_redo_edge_without_complete_outputs(schema_dir):
    s = _redo_snapshot(schema_dir)
    token = pipeline.Token("t1", "pr", 1, "a-pr", "/w", "mgr")
    holder = pipeline.Caller("a-pr", "K", "worker")
    v = pipeline.check_advance(s, token, holder, "design", _facts(), [], reason="review: X breaks decision 2")
    assert v.ok and v.index_to == 1 and v.edge.redo and (v.edge.frm, v.edge.to) == ("pr", "design")


def test_a_redo_edge_is_refused_to_the_manager_a_non_holder_and_an_empty_reason(schema_dir):
    s = _redo_snapshot(schema_dir)
    token = pipeline.Token("t1", "pr", 1, "a-pr", "/w", "mgr")
    manager = pipeline.Caller(None, None, "manager")
    holder = pipeline.Caller("a-pr", "K", "worker")
    other = pipeline.Caller("a-other", "K", "worker")
    assert "the manager reworks" in pipeline.check_advance(s, token, manager, "design", _facts(), [], reason="x").reason
    assert "needs reason" in pipeline.check_advance(s, token, holder, "design", _facts(), [], reason="  ").reason
    assert "is not the holder" in pipeline.check_advance(s, token, other, "design", _facts(), [], reason="x").reason


def test_a_row_takes_its_redo_edge_and_the_join_rule_does_not_apply(schema_dir):
    s = _redo_snapshot(schema_dir, FORK_REDO)
    token = pipeline.Token("t1/svc", "task", 2, "a-svc", "/s", "mgr", "svc")
    v = pipeline.check_advance(s, token, pipeline.Caller("a-svc", "K", "worker"), "design", _facts(), [], reason="x")
    assert v.ok and v.edge.redo


def test_a_missing_edge_names_the_redo_edges_too(schema_dir):
    s = _redo_snapshot(schema_dir)
    token = pipeline.Token("t1", "pr", 1, "a-pr", "/w", "mgr")
    v = pipeline.check_advance(s, token, pipeline.Caller("a-pr", "K", "worker"), "task", _facts(), [])
    assert v.reason == "no edge pr -> task in the schema snapshot; edges out of pr: post-merge; redo edges: design"


def test_a_footer_with_a_redo_edge_names_it_and_the_permission_protocol(schema_dir):
    s = _redo_snapshot(schema_dir)
    token = pipeline.Token("t1", "pr", 1, None, "/w", "mgr")
    text = pipeline.compose_brief(s.nodes["pr"], token, "K", [], [], [], [e for e in s.edges if e.frm == "pr"],
                                  redo_out=[e for e in s.redo_edges if e.frm == "pr"])
    lines = text.splitlines()
    assert lines[-4] == "Redo edges: design"
    assert lines[-3] == pipeline._decision_line() and lines[-2] == pipeline._summary_line()
    assert lines[-1].startswith("The manager moves the token to post-merge when merge fires. Publish your outputs, "
                                "then call worker_done; call pipeline_advance only along a redo edge. ")
    assert ("ask_manager first with a first line `Redesign request` and change nothing until the answer. Only after "
            "an answer whose first line is `redesign: yes`, call pipeline_advance(claude_sid=<your sid>, "
            'task_key=..., to="design", reason=<the finding, its link, the decision it breaks>) as your last call '
            "before worker_done.") in lines[-1]


def test_a_footer_whose_only_edge_out_is_a_status_trigger_names_the_statuses(schema_dir):
    (schema_dir / "ok.toml").write_text(STATUS)
    s = pipeline.load_schema(schema_dir / "ok.toml")
    token = pipeline.Token("t1", "pr", 1, None, "/w", "mgr")
    text = pipeline.compose_brief(s.nodes["pr"], token, "K", [], [], [], [e for e in s.edges if e.frm == "pr"])
    assert text.splitlines()[-1].startswith(
        "The engine moves the token to post-merge once the ticket's status is one of Verified, category:done. "
        "Publish your outputs, then call worker_done; do not call pipeline_advance.")


def test_a_redo_rounds_footer_names_the_node_that_opened_it_and_keeps_context_ask_binding():
    tok = pipeline.Token("t1", "design", 1, None, "/w", "mgr")
    text = pipeline.compose_brief(_brief_node(), tok, "K", ["context.ask"], [], [], [], round_no=2, round_from="pr")
    assert ("Round 2 was opened by the pr node through its redo edge with the engineer's permission. "
            "context.rework-2 is its reason. context.ask still binds; where the reason conflicts with it, "
            "ask_manager.") in text
    assert "is a rework" not in text
    assert "If /w is on a branch whose pull request has merged" in text


def test_the_last_rework_ask_before_a_redo_round_skips_redo_rounds():
    events = [_ev("round_started", round=2, ask="context.rework-2"),
              _ev("round_started", round=3, ask="context.rework-3", via="redo", **{"from": "pr"}),
              _ev("round_started", round=4, ask="context.rework-4", via="redo", **{"from": "task"})]
    assert pipeline.last_rework_ask(events) == "context.rework-2"
    assert pipeline.last_rework_ask(events[1:]) is None


def test_work_closed_closes_the_work_until_the_next_round_and_supersedes_every_request():
    base = [_ev("pipeline_started", node="design", index=1, holder="a-design", cwd="/w", manager="mgr"),
            _ev("token_held", node="design", reason="wait", actor_sid="m"),
            _ev("advance_requested", request_id="r1", edge={"from": "design", "to": "task"}, index_to=1,
                kind="advance"),
            _ev("work_closed", node="design", reason="absorbed by repo-node", actor_sid="m", ts=5.0)]
    assert pipeline.fold(base[:3]).closed is None
    closed = pipeline.fold(base)
    assert closed.closed == {"node": "design", "reason": "absorbed by repo-node", "ts": 5.0, "actor_sid": "m"}
    assert closed.held == {} and closed.open_requests == [] and "r1" in closed.superseded
    assert closed.tokens["t1"].node == "design" and closed.tokens["t1"].holder == "a-design"
    late = base + [_ev("advance_requested", request_id="r2", edge={"from": "design", "to": "design"}, index_to=1,
                       kind="restart"),
                   _ev("advance_committed", request_id="r2", node="design", index=1, holder="a-new", sid="s")]
    assert pipeline.fold(late).open_requests == [] and pipeline.fold(late).tokens["t1"].holder == "a-design"
    reopened = base + [_ev("round_started", round=2, to="design"),
                       _ev("token_override", **{"from": "design"}, to="design", index=1, reason="reopen",
                           actor_sid="m"),
                       _ev("advance_requested", request_id="r3", edge={"from": "design", "to": "design"},
                           index_to=1, kind="advance")]
    folded = pipeline.fold(reopened)
    assert folded.closed is None and folded.round == 2
    assert [r["request_id"] for r in folded.open_requests] == ["r3"]
    assert "work_closed" in pipeline.RESERVED_EVENT_TYPES


def test_latest_hold_is_the_last_token_held_of_that_token():
    events = [_ev("pipeline_started", node="design", index=1, holder="a", cwd="/w", manager="mgr"),
              _ev("token_held", node="design", reason="one", event_id="h1"),
              _ev("token_held", token_id="t1/a", node="task", reason="row", event_id="hr"),
              _ev("token_released", node="design"),
              _ev("token_held", node="design", reason="two", event_id="h2")]
    assert (pipeline.latest_hold(events, "t1") or {}).get("event_id") == "h2"
    assert (pipeline.latest_hold(events, "t1") or {}).get("reason") == "two"
    assert (pipeline.latest_hold(events, "t1/a") or {}).get("event_id") == "hr"
    assert pipeline.latest_hold(events, "t9") is None


RULE = pipeline.TierWhen(input="intake.route", line="internal", equals="yes", tier="SCOUT", unless="gate.open")
RULED_NODE = pipeline.Node(name="post-merge", role="GenericWorker", tier="IMPL_HARD", inputs=["intake.route"],
                           tier_when=RULE)
ROUTE = {"intake.route": ("complete", "output")}


@pytest.mark.parametrize("text,word", [
    ("next: plan\ninternal: yes — the subject is dockwright itself\nrepos:\n", "yes"),
    ("internal:yes—dockwright", "yes"),
    ("  internal: Yes because", "yes"),
    ("internal: no — the effect test fired", "no"),
    ("internal: n/a", "n/a"),
    ("internal:", ""),
    ("internal: no\ninternal: yes", "no"),
    ("next: plan\nrepos:\n", None),
    ("internals: yes", None),
])
def test_line_word_reads_the_first_word_of_the_first_named_line(text, word):
    assert pipeline.line_word(text, "internal") == word


@pytest.mark.parametrize("artifacts,body,tier", [
    (ROUTE, "internal: yes — the subject is dockwright itself", "SCOUT"),
    (ROUTE, "internal:yes—dockwright", "SCOUT"),
    (ROUTE, "internal: Yes because", "SCOUT"),
    (ROUTE, "internal: no — the effect test fired", "IMPL_HARD"),
    (ROUTE, "internal: n/a", "IMPL_HARD"),
    (ROUTE, "internal: true", "IMPL_HARD"),
    (ROUTE, "next: plan\nrepos:\n", "IMPL_HARD"),
    ({"intake.route": ("partial", "output")}, "internal: yes", "IMPL_HARD"),
    ({}, "internal: yes", "IMPL_HARD"),
    ({**ROUTE, "gate.open": ("complete", "checkpoint")}, "internal: yes", "IMPL_HARD"),
    ({**ROUTE, "gate.open": ("partial", "checkpoint")}, "internal: yes", "IMPL_HARD"),
])
def test_node_tier_takes_the_rule_row_only_on_a_complete_match_without_the_unless_record(artifacts, body, tier):
    assert pipeline.node_tier(RULED_NODE, "IMPL_HARD", _facts(**artifacts), 1, lambda name: body) == tier


@pytest.mark.parametrize("error", [OSError("gone"), ValueError("no artifact intake.route for K"), KeyError("content")])
def test_node_tier_keeps_the_base_row_when_the_read_fails(error):
    def read(name):
        raise error

    assert pipeline.node_tier(RULED_NODE, "IMPL_HARD", _facts(**ROUTE), 1, read) == "IMPL_HARD"


def test_a_node_without_a_rule_reads_nothing():
    def read(name):
        raise AssertionError(f"read {name}")

    node = pipeline.Node(name="post-merge", role="GenericWorker", tier="IMPL_HARD", inputs=["intake.route"])
    assert pipeline.node_tier(node, "IMPL_HARD", _facts(**ROUTE), 1, read) == "IMPL_HARD"


def test_node_tier_reads_the_input_and_the_unless_record_at_the_nodes_index():
    rule = pipeline.TierWhen(input="implement.state-{i}", line="internal", equals="yes", tier="SCOUT",
                             unless="gate.open-{i}")
    node = pipeline.Node(name="repo", role="GenericWorker", tier="IMPL_HARD", inputs=["implement.state-{i}"],
                         tier_when=rule)
    seen = []

    def read(name):
        seen.append(name)
        return "internal: yes"

    facts = _facts(**{"implement.state-2": ("complete", "checkpoint"), "gate.open-1": ("complete", "checkpoint")})
    assert pipeline.node_tier(node, "IMPL_HARD", facts, 2, read) == "SCOUT"
    assert seen == ["implement.state-2"]


MODEL_RULE = pipeline.ModelWhen(input="intake.route", line="internal", equals="yes", model="@sonnet", effort="high",
                                unless="gate.open")
MODELED_NODE = pipeline.Node(name="post-merge", role="GenericWorker", model="@opus", effort="xhigh",
                             inputs=["intake.route"], model_when=MODEL_RULE)


@pytest.mark.parametrize("artifacts,body,launch", [
    (ROUTE, "internal: yes — the subject is dockwright itself", ("@sonnet", "high")),
    (ROUTE, "internal: no — the effect test fired", ("@opus", "xhigh")),
    ({"intake.route": ("partial", "output")}, "internal: yes", ("@opus", "xhigh")),
    ({}, "internal: yes", ("@opus", "xhigh")),
    ({**ROUTE, "gate.open": ("complete", "checkpoint")}, "internal: yes", ("@opus", "xhigh")),
])
def test_node_model_takes_the_rules_model_and_effort_only_on_a_complete_match(artifacts, body, launch):
    assert pipeline.node_model(MODELED_NODE, _facts(**artifacts), 1, lambda name: body) == launch


def test_node_model_keeps_its_own_on_a_failed_read_and_reads_nothing_without_a_rule():
    def fail(name):
        raise OSError("gone")

    def never(name):
        raise AssertionError(f"read {name}")

    assert pipeline.node_model(MODELED_NODE, _facts(**ROUTE), 1, fail) == ("@opus", "xhigh")
    plain = pipeline.Node(name="design", role="Designer", model="@fable", effort="medium")
    assert pipeline.node_model(plain, _facts(**ROUTE), 1, never) == ("@fable", "medium")
