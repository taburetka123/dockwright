import asyncio

import pytest

from dockwright import pipeline
from dockwright.mcp_server import artifact_get_impl, artifact_put_impl, pipeline_advance_impl, pipeline_status_impl
from tests.test_pipeline_core import FORK, SCHEMA, _manifest, schema_dir
from tests.test_pipeline_reconcile import (_complete_design_outputs, _fake_registration, _started_at_design,
                                           fresh_orchestrator_dir)

LANES = '''
[nodes.task.children.reviewer]
rounds = 3
step = "review"
[nodes.task.children.reviewer.lanes.tier2]
role = "CodeReviewer"
mode = "report"
model = "@opus"
effort = "xhigh"
[nodes.task.children.reviewer.lanes.tier2-high]
role = "CodeReviewer"
mode = "report"
model = "@fable"
effort = "medium"
codex = "gpt-6-astra"
'''
ONE_LANE = '''
[nodes.task.children.reviewer]
rounds = 3
role = "DocReviewer"
mode = "report"
model = "@opus"
effort = "xhigh"
'''
CHILDREN = ONE_LANE + '''
[nodes.task.children.reviewer.subagents.none]
type = "Explore"
model = "sonnet"
effort = "xhigh"

[nodes.task.subagents.scout]
type = "Explore"
model = "sonnet"
effort = "xhigh"
step = "searching a repo"
'''


def _reviewer_manifests():
    return {"CodeReviewer": _manifest("CodeReviewer", launch=("child",)),
            "DocReviewer": _manifest("DocReviewer", launch=("child",)),
            **{role: _manifest(role, launch=("worker",)) for role in ("SCOUT_DESIGN", "IMPL_HARD", "SCOUT")}}


def test_lanes_load_validate_and_survive_the_snapshot(schema_dir, tmp_path):
    s = _load(schema_dir, SCHEMA + LANES)
    assert pipeline.validate(s, str(tmp_path), manifests=_reviewer_manifests()) == []
    slot = s.nodes["task"].children["reviewer"]
    assert list(slot.lanes) == ["tier2", "tier2-high"]
    assert slot.lanes["tier2-high"] == pipeline.Lane("tier2-high", "CodeReviewer", "report", "@fable", "medium", "gpt-6-astra")
    again = pipeline.load_snapshot(pipeline.snapshot(s))
    assert again.nodes["task"].children["reviewer"].lanes == slot.lanes
    assert "roles" not in pipeline.snapshot(s)
    assert pipeline.declares_children(again)


def test_a_slot_with_one_lane_writes_its_keys_in_itself_and_is_named_default(schema_dir, tmp_path):
    s = _load(schema_dir, SCHEMA + ONE_LANE)
    assert pipeline.validate(s, str(tmp_path), manifests=_reviewer_manifests()) == []
    slot = s.nodes["task"].children["reviewer"]
    assert list(slot.lanes) == ["default"] and slot.lanes["default"].model == "@opus"
    text = pipeline.snapshot(s)
    assert "[nodes.task.children.reviewer]\nrounds = 3\nwarm = true\nrole = \"DocReviewer\"" in text
    assert ".lanes." not in text
    assert pipeline.load_snapshot(text).nodes["task"].children["reviewer"].lanes == slot.lanes


def test_an_old_snapshot_with_roles_still_loads(schema_dir):
    s = _load(schema_dir, SCHEMA + '\n[nodes.task.children.reviewer]\nroles = ["REVIEW_TIER2"]\nrounds = 3\n')
    slot = s.nodes["task"].children["reviewer"]
    assert slot.roles == ["REVIEW_TIER2"] and slot.lanes == {} and s.problems == []


@pytest.mark.parametrize("old,new,expected", [
    ('model = "@fable"\neffort = "medium"\ncodex', 'effort = "medium"\ncodex', "lanes.tier2-high] needs role, mode, model and effort"),
    ('effort = "xhigh"\n[nodes.task.children.reviewer.lanes.tier2-high]', '[nodes.task.children.reviewer.lanes.tier2-high]', "lanes.tier2] needs role, mode, model and effort"),
    ('role = "CodeReviewer"\nmode = "report"\nmodel = "@opus"', 'role = "Nobody"\nmode = "report"\nmodel = "@opus"', "lanes.tier2] role 'Nobody' names no role manifest"),
    ('role = "CodeReviewer"\nmode = "report"\nmodel = "@opus"', 'role = "Designer"\nmode = "report"\nmodel = "@opus"', "lanes.tier2] role 'Designer' does not declare launch 'child'"),
    ('mode = "report"\nmodel = "@opus"', 'mode = "loud"\nmodel = "@opus"', "lanes.tier2] mode 'loud' is not one of report, interactive"),
    ('model = "@opus"\neffort = "xhigh"', 'model = "opus"\neffort = "xhigh"', "lanes.tier2] model 'opus'"),
    ('effort = "xhigh"\n[nodes', 'effort = "extra"\n[nodes', "lanes.tier2] effort 'extra' is not one of"),
    ('codex = "gpt-6-astra"', 'codex = "@codex"', "lanes.tier2-high] codex '@codex' must be a concrete Codex model id"),
    ('codex = "gpt-6-astra"', 'codex = ""', "lanes.tier2-high] codex must be a non-empty string"),
    ('rounds = 3\nstep = "review"', 'rounds = 0\nstep = "review"', "rounds must be an integer of at least 1"),
    ('step = "review"', 'colour = "red"', "unknown key 'colour'"),
])
def test_each_lane_defect_is_named(schema_dir, tmp_path, old, new, expected):
    manifests = {**_reviewer_manifests(), "Designer": _manifest("Designer", launch=("worker",))}
    problems = pipeline.validate(_load(schema_dir, SCHEMA + LANES.replace(old, new, 1)), str(tmp_path), manifests=manifests)
    assert any(expected in p for p in problems), problems


def test_a_fresh_schema_slot_that_names_roster_rows_is_refused(schema_dir, tmp_path):
    text = SCHEMA + '\n[nodes.task.children.reviewer]\nroles = ["REVIEW_TIER2"]\nrounds = 3\n'
    problems = pipeline.validate(_load(schema_dir, text), str(tmp_path), manifests={})
    assert any("roles names roster rows; write lanes with role, mode, model, effort" in p for p in problems)


def test_a_slot_with_both_lanes_and_its_own_keys_or_neither_is_refused(schema_dir, tmp_path):
    both = SCHEMA + LANES.replace('rounds = 3\nstep = "review"', 'rounds = 3\nmodel = "@opus"\neffort = "xhigh"\nrole = "CodeReviewer"\nmode = "report"')
    neither = SCHEMA + '\n[nodes.task.children.reviewer]\nrounds = 3\n'
    assert any("names both lanes and its own lane keys" in p for p in pipeline.validate(_load(schema_dir, both), str(tmp_path), manifests=_reviewer_manifests()))
    assert any("name a lane" in p for p in pipeline.validate(_load(schema_dir, neither), str(tmp_path), manifests={}))


def test_an_old_tier_when_snapshot_still_loads(schema_dir):
    s = pipeline.load_snapshot(_ruled(TIER_WHEN))
    assert s.nodes["post-merge"].tier_when.tier == "SCOUT"

BACK = '\n[[edges]]\nfrom = "pr"\nto = "task"\nback = true\n'

TIER_WHEN = '''
[nodes.post-merge.tier_when]
input = "design.hld"
line = "internal"
equals = "yes"
unless = "design.explainer"
tier = "SCOUT"
'''

_LEGACY_POST_MERGE = 'role = "SCOUT"\nmodel = "@sonnet"\neffort = "xhigh"\nbrief = "briefs/post-merge.md"\ninputs = ["implement.repo"]'
_RULED_POST_MERGE = 'role = "IMPL_HARD"\nbrief = "briefs/post-merge.md"\ninputs = ["implement.repo", "design.hld"]'


def _ruled(rule=TIER_WHEN):
    assert _LEGACY_POST_MERGE in SCHEMA
    return SCHEMA.replace(_LEGACY_POST_MERGE, _RULED_POST_MERGE, 1) + rule


def _load(schema_dir, text, name="s.toml"):
    (schema_dir / name).write_text(text)
    return pipeline.load_schema(schema_dir / name)


def test_a_cold_slot_and_an_inherited_model_survive_the_snapshot(schema_dir, tmp_path):
    text = SCHEMA + CHILDREN.replace("rounds = 3", "rounds = 2\nwarm = false").replace('model = "sonnet"\neffort = "xhigh"\n\n', 'effort = "xhigh"\n\n')
    again = pipeline.load_snapshot(pipeline.snapshot(_load(schema_dir, text)))
    slot = again.nodes["task"].children["reviewer"]
    assert (slot.rounds, slot.warm) == (2, False)
    assert slot.subagents["none"].pair == "Explore:inherit"
    assert pipeline.validate(again, str(tmp_path)) == []
    assert pipeline.declares_children(again) and not pipeline.declares_children(_load(schema_dir, SCHEMA, "old.toml"))


_TASK = 'role = "IMPL_HARD"\nmodel = "@opus"\neffort = "xhigh"\nrepeat = "plan.tasks"'
_MODELED_TASK = 'role = "Coder"\nmodel = "@fable"\neffort = "medium"\nrepeat = "plan.tasks"'
EFFORT_CHILDREN = CHILDREN
NO_EFFORT_CHILDREN = CHILDREN.replace('model = "sonnet"\neffort = "xhigh"\n', 'model = "sonnet"\n')


def _modeled_task(children=EFFORT_CHILDREN):
    assert _TASK in SCHEMA
    return SCHEMA.replace(_TASK, _MODELED_TASK, 1) + children


def test_a_scouts_effort_loads_validates_and_survives_the_snapshot(schema_dir, tmp_path):
    s = _load(schema_dir, _modeled_task())
    assert pipeline.validate(s, str(tmp_path), manifests={}) == []
    again = pipeline.load_snapshot(pipeline.snapshot(s))
    assert again.nodes["task"].subagents["scout"].entry == "Explore:sonnet:xhigh"
    assert again.nodes["task"].subagents["scout"].pair == "Explore:sonnet"
    assert again.nodes["task"].children["reviewer"].subagents["none"].entry == "Explore:sonnet:xhigh"


@pytest.mark.parametrize("children,expected", [
    (NO_EFFORT_CHILDREN, "[nodes.task.subagents.scout] names no effort; a node that names its model names each subagent's effort"),
    (NO_EFFORT_CHILDREN, "[nodes.task.children.reviewer.subagents.none] names no effort"),
    (EFFORT_CHILDREN.replace('effort = "xhigh"', 'effort = "huge"', 1),
     "effort 'huge' is not one of low, medium, high, xhigh, max"),
])
def test_each_scout_effort_defect_is_named(schema_dir, tmp_path, children, expected):
    problems = pipeline.validate(_load(schema_dir, _modeled_task(children)), str(tmp_path), manifests={})
    assert any(expected in p for p in problems), problems


def test_a_back_edge_loads_apart_from_the_forward_graph_and_survives_the_snapshot(schema_dir, tmp_path):
    s = _load(schema_dir, FORK + BACK)
    assert pipeline.validate(s, str(tmp_path)) == []
    assert [(e.frm, e.to) for e in s.back_edges] == [("pr", "task")]
    assert ("pr", "task") not in {(e.frm, e.to) for e in s.edges}
    assert pipeline.back_edge(pipeline.load_snapshot(pipeline.snapshot(s)), "pr", "task") is not None


@pytest.mark.parametrize("edge,expected", [
    ('from = "pr"\nto = "pr"\nback = true', "a back edge goes back to an earlier node, never to its own source"),
    ('from = "pr"\nto = "design"\nback = true', "a back edge cannot enter the start node design"),
    ('from = "task"\nto = "post-merge"\nback = true', "task is not reachable from post-merge"),
    ('from = "pr"\nto = "task"\nback = true\nfork = true', "a back edge cannot carry `fork` or `join`"),
    ('from = "pr"\nto = "task"\nback = true\nwhen = "last"', "a back edge cannot carry `when`"),
    ('from = "pr"\nto = "task"\nback = true\nredo = true', "an edge is either redo or back"),
    ('from = "pr"\nto = "task"\nback = true\ntrigger = "merge"', "a back edge cannot carry `trigger`"),
    ('from = "pr"\nto = "task"\nback = true\nexit_checks = [{ type = "artifact", name = "plan.tasks" }]',
     "a back edge cannot carry `exit_checks`"),
    ('from = "pr"\nto = "task"\nback = true\n[[edges]]\nfrom = "pr"\nto = "task"\nback = true', "duplicate edge"),
])
def test_each_back_edge_defect_is_named(schema_dir, tmp_path, edge, expected):
    problems = pipeline.validate(_load(schema_dir, FORK + "\n[[edges]]\n" + edge + "\n"), str(tmp_path))
    assert any(expected in p for p in problems), problems


INTAKE_FIRST = FORK + '''
[nodes.intake]
role = "SCOUT"
model = "@sonnet"
effort = "xhigh"
brief = "briefs/design.md"
outputs = ["intake.route"]

[[edges]]
from = "intake"
to = "design"
'''


def test_a_row_node_never_leaves_by_a_back_edge(schema_dir, tmp_path):
    assert pipeline.validate(_load(schema_dir, INTAKE_FIRST), str(tmp_path)) == []
    problems = pipeline.validate(_load(schema_dir, INTAKE_FIRST + '\n[[edges]]\nfrom = "task"\nto = "design"\nback = true\n',
                                       "row.toml"), str(tmp_path))
    assert problems == ["[[edges]] task -> design (back): [nodes.task] is entered by a fork edge, so it leaves only by "
                        "its self-loop or a join edge"], problems


def test_rework_at_names_a_node(schema_dir, tmp_path):
    good = _load(schema_dir, 'rework_at = "task"\n' + SCHEMA)
    assert pipeline.validate(good, str(tmp_path)) == []
    assert pipeline.load_snapshot(pipeline.snapshot(good)).rework_at == "task"
    bad = _load(schema_dir, 'rework_at = "nowhere"\n' + SCHEMA, "bad.toml")
    assert any("rework_at 'nowhere' is not a node of the schema" in p
               for p in pipeline.validate(bad, str(tmp_path)))


def test_an_old_schema_snapshots_without_the_new_keys(schema_dir):
    text = pipeline.snapshot(_load(schema_dir, FORK))
    s = pipeline.load_snapshot(text)
    assert s.back_edges == [] and s.rework_at is None and "tier_when" not in text
    assert all(not n.children and not n.subagents and n.tier_when is None for n in s.nodes.values())


def test_an_i_input_on_a_non_repeat_node_reads_every_row():
    node = pipeline.Node(name="ship", role="Coder", inputs=["implement.repo-{i}", "plan.tasks"])
    assert pipeline.expand_inputs(node, 1, [1, 2, 3]) == ["implement.repo-1", "implement.repo-2", "implement.repo-3",
                                                          "plan.tasks"]
    rep = pipeline.Node(name="repo", role="Coder", repeat="plan.tasks", inputs=["implement.state-{i}"])
    assert pipeline.expand_inputs(rep, 2, [1, 2, 3]) == ["implement.state-2"]


def test_schema_show_prints_children_subagents_and_every_edge_kind(schema_dir, capsys):
    path = schema_dir / "s.toml"
    path.write_text('rework_at = "task"\n' + FORK + BACK + CHILDREN)
    assert pipeline.schema_main(["show", str(path)]) == 0
    out = capsys.readouterr().out
    for line in ("rework_at: task", "child reviewer: lanes=default:DocReviewer/report/@opus/xhigh", "rounds=3", "subagent scout: Explore:sonnet",
                 "pr -> task  back", "design -> task  fork"):
        assert line in out, out


def test_the_cli_shows_a_schema_by_name(schema_dir, monkeypatch, capsys):
    from dockwright import __main__ as cli
    (schema_dir / "named.toml").write_text(FORK + BACK)
    monkeypatch.setattr("dockwright.config.pipeline_schemas_dir", lambda: schema_dir)
    monkeypatch.setattr(cli.sys, "argv", ["dockwright", "schema", "show", "named"])
    with pytest.raises(SystemExit) as ended:
        cli.main()
    assert ended.value.code == 0
    assert "pr -> task  back" in capsys.readouterr().out


def test_schema_show_names_a_missing_schema(tmp_path, capsys):
    assert pipeline.schema_main(["show", str(tmp_path / "none.toml")]) == 2
    assert "no schema file at" in capsys.readouterr().out


JOIN_READS_EVERY_ROW = (SCHEMA.replace('outputs = ["review.task-{i}"]', 'outputs = ["review.task-{i}", "implement.repo-{i}"]')
                        .replace('inputs = ["plan.tasks"]\noutputs = ["implement.repo"]',
                                 'inputs = ["plan.tasks", "implement.repo-{i}"]\noutputs = ["implement.repo"]'))


def test_a_node_that_reads_every_rows_output_is_briefed_with_and_allowed_each_row(fresh_orchestrator_dir, tmp_path,
                                                                                  monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path, schema_text=JOIN_READS_EVERY_ROW)
    _complete_design_outputs(sid)
    calls = _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_advance_impl("m1", "K", "pr", override=True, reason="jump"))
    inputs = next(line for line in calls[-1]["initial_prompt"].splitlines() if line.startswith("Inputs (read ONLY"))
    assert "implement.repo-1" in inputs and "implement.repo-2" in inputs
    for i in (1, 2):
        artifact_put_impl("K", "implement", f"repo-{i}", "b", "complete", f"w-{i}", scope="output")
        artifact_get_impl("K", "implement", f"repo-{i}", reader_sid="sid-K-pr")
    reads = pipeline_status_impl("K").split("## reads\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert "- K-pr (pr) read implement.repo-1" in reads and "- K-pr (pr) read implement.repo-2" in reads, reads


def test_schema_show_prints_a_tier_rule(schema_dir, capsys):
    path = schema_dir / "ruled.toml"
    path.write_text(_ruled())
    assert pipeline.schema_main(["show", str(path)]) == 0
    assert "    tier_when=design.hld:internal=yes unless design.explainer -> SCOUT" in capsys.readouterr().out


MODEL_WHEN = '''
[nodes.post-merge.model_when]
input = "design.hld"
line = "internal"
equals = "yes"
unless = "design.explainer"
model = "@sonnet"
effort = "xhigh"
'''

_MODELED_POST_MERGE = ('role = "GenericWorker"\nmodel = "@opus"\neffort = "xhigh"\nbrief = "briefs/post-merge.md"\n'
                       'inputs = ["implement.repo", "design.hld"]')


def _model_ruled(rule=MODEL_WHEN):
    assert _LEGACY_POST_MERGE in SCHEMA
    return SCHEMA.replace(_LEGACY_POST_MERGE, _MODELED_POST_MERGE, 1) + rule


def test_a_model_rule_loads_validates_and_survives_the_snapshot(schema_dir, tmp_path):
    s = _load(schema_dir, _model_ruled())
    assert pipeline.validate(s, str(tmp_path), manifests={}) == []
    again = pipeline.load_snapshot(pipeline.snapshot(s))
    node = again.nodes["post-merge"]
    assert (node.model, node.effort, node.tier, node.tier_when) == ("@opus", "xhigh", None, None)
    assert node.model_when == pipeline.ModelWhen(input="design.hld", line="internal", equals="yes", model="@sonnet",
                                                 effort="xhigh", unless="design.explainer")
    assert pipeline.validate(again, str(tmp_path), manifests={}) == []


@pytest.mark.parametrize("old,new,expected", [
    ('effort = "xhigh"\n', "", "[nodes.post-merge.model_when] needs input, line, equals, model and effort"),
    ('input = "design.hld"', 'input = "plan.tasks"',
     "[nodes.post-merge.model_when] input 'plan.tasks' is not among the node's inputs"),
    ('line = "internal"', 'line = "internal:"', "[nodes.post-merge.model_when] line 'internal:' must be a bare key"),
    ('equals = "yes"', 'equals = "yes please"', "[nodes.post-merge.model_when] equals 'yes please' can never match"),
    ('unless = "design.explainer"', 'unless = "design.explainr"',
     "[nodes.post-merge.model_when] unless 'design.explainr' is not an output or checkpoint of any upstream node"),
    ('model = "@sonnet"', 'model = "sonnet"', "[nodes.post-merge.model_when] model 'sonnet' is neither"),
    ('effort = "xhigh"', 'effort = "huge"', "[nodes.post-merge.model_when] effort 'huge' is not one of"),
])
def test_each_model_rule_defect_is_named(schema_dir, tmp_path, old, new, expected):
    assert old in MODEL_WHEN
    problems = pipeline.validate(_load(schema_dir, _model_ruled(MODEL_WHEN.replace(old, new, 1))), 
                                 str(tmp_path), manifests={})
    assert any(expected in p for p in problems), problems


def test_a_model_rule_needs_model_on_the_node_and_a_tier_rule_cannot_sit_beside_model(schema_dir, 
                                                                                         tmp_path):
    no_model = SCHEMA.replace(_LEGACY_POST_MERGE, _RULED_POST_MERGE, 1) + MODEL_WHEN
    problems = pipeline.validate(_load(schema_dir, no_model), str(tmp_path), manifests={})
    assert any("[nodes.post-merge.model_when] needs model and effort on the node" in p for p in problems), problems
    both = pipeline.validate(_load(schema_dir, _model_ruled(TIER_WHEN), "both.toml"), str(tmp_path),
                             manifests={})
    assert any("[nodes.post-merge] names tier_when beside model; write model_when" in p for p in both), both


def test_schema_show_prints_a_nodes_model_its_rule_and_a_scouts_effort(schema_dir, capsys):
    path = schema_dir / "modeled.toml"
    path.write_text(_model_ruled() + EFFORT_CHILDREN)
    assert pipeline.schema_main(["show", str(path)]) == 0
    out = capsys.readouterr().out
    for line in ("  post-merge  role=GenericWorker model=@opus effort=xhigh",
                 "    model_when=design.hld:internal=yes unless design.explainer -> @sonnet/xhigh",
                 "    subagent scout: Explore:sonnet:xhigh  (searching a repo)"):
        assert line in out, out
