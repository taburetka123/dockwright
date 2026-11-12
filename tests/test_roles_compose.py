import json
import subprocess
from pathlib import Path

import pytest

from dockwright import paths, roles


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


ACTIONS_TOML = (
    '[actions]\n'
    'git-write = "edit a tracked file"\n'
    'push = "push a branch"\n'
    'build = "run a build"\n'
    'review-code = "review a diff"\n'
    'deploy = "deploy"\n'
    '[domains]\n'
    'git = "git"\n'
    'build = "build"\n'
    'acme = "project layer"\n'
    'acme-be = "project layer, backend"\n'
)


def _unit(kind, binds, domain, body, cites=()):
    cite = f"cites: [{', '.join(cites)}]\n" if cites else ""
    return f"---\nkind: {kind}\nbinds: [{', '.join(binds)}]\ndomain: {domain}\n{cite}---\n{body}\n"


@pytest.fixture
def layout(tmp_path):
    claude = tmp_path / "claude"
    canon = tmp_path / "deploy" / "roles"
    agents = tmp_path / "deploy" / "agents"
    units = tmp_path / "project-units"
    _write(canon / "Coder.core.md", "# Coder core\n<!-- overlay: extra -->\n")
    _write(canon / "fragments" / "worker-protocol.md", "WORKER PROTOCOL\n<!-- overlay: proto-hook -->\n")
    _write(canon / "fragments" / "child-protocol.md", "CHILD PROTOCOL\n")
    _write(canon / "fragments" / "evidence-map.md", _unit("fact", ["build"], "build", "EVIDENCE MAP"))
    _write(agents / "manager.core.md", "---\nname: manager\n---\n# Manager core\n")
    _write(claude / "roles" / "_shared" / "fragments" / "git" / "commit.md",
           _unit("guard", ["git-write", "push"], "git", "COMMIT RULE"))
    _write(claude / "roles" / "Coder" / "fragments" / "code-style.md", _unit("fact", ["build"], "build", "CODE STYLE"))
    _write(claude / "roles" / "actions.toml", ACTIONS_TOML)
    _write(units / "common" / "acme-env.md", _unit("fact", ["build"], "acme", "ACME ENV"))
    _write(units / "back" / "acme-be-style.md", _unit("fact", ["build", "review-code"], "acme-be", "ACME BE STYLE"))
    _write(claude / "skills" / "skill-a" / "SKILL.md", "---\nname: skill-a\n---\n")
    _write(claude / "skills" / "synced" / "vendor-b" / "SKILL.md", "---\nname: vendor-b\n---\n")
    _write(claude / "commands" / "cmd-c.md", "---\ndescription: c\n---\n")
    _write(claude / "roles" / "Coder" / "role.toml",
           'core = "Coder"\ntier = "IMPL_HARD"\nlaunch = ["worker", "child"]\n'
           'actions = ["build", "git-write"]\nreachable = ["push"]\n'
           'fragments = ["evidence-map", "commit", "code-style"]\nskills = ["skill-a"]\n'
           '[settings]\nskillListingMaxDescChars = 160\n')
    _write(claude / "roles" / "Manager" / "role.toml",
           'core = "manager"\ntier = "MGR_LANES"\nlaunch = ["manager"]\nactions = ["push"]\nfragments = ["commit"]\n')
    return roles.Layout(claude / "roles", roles.core_dirs_for(agents, canon), claude)


@pytest.fixture
def profile_settings(monkeypatch, tmp_path):
    settings = {"sets": {"none": [], "common": ["acme"], "back": ["acme", "acme-be"]},
                "repos": {"acme-web": "common", "worker": "back"},
                "work_roots": [str(tmp_path / "work")], "work_default": "back"}
    monkeypatch.setattr(roles.config, "profiles", lambda: settings)
    monkeypatch.setattr(roles.config, "project_units_dir", lambda: tmp_path / "project-units")
    return settings


def _manifest(layout, name):
    return roles.load_manifests(layout.roles_dir)[name]


def test_worker_render_is_mark_core_fragments_then_protocol(layout):
    text, warnings, _ = roles.render(layout, _manifest(layout, "Coder"), "worker", {})
    assert text.startswith("<!-- dockwright-role: Coder launch=worker -->\n\n# Coder core\n")
    assert text.index("EVIDENCE MAP") < text.index("COMMIT RULE") < text.index("CODE STYLE") < text.index("WORKER PROTOCOL")
    assert "CORE RULE" not in text and "CHILD PROTOCOL" not in text and warnings == []


def test_child_render_carries_the_child_protocol_and_codex_render_no_protocol(layout):
    m = _manifest(layout, "Coder")
    child, _, _ = roles.render(layout, m, "child", {})
    codex, _, _ = roles.render(layout, m, roles.CODEX, {})
    assert "CHILD PROTOCOL" in child and "WORKER PROTOCOL" not in child
    assert codex.startswith("<!-- dockwright-role: Coder launch=codex -->")
    assert "PROTOCOL" not in codex


def test_a_core_in_the_agents_dir_resolves_without_its_frontmatter(layout):
    text, _, _ = roles.render(layout, _manifest(layout, "Manager"), "manager", {})
    assert "# Manager core" in text and "name: manager" not in text and "PROTOCOL" not in text


def test_a_shared_fragment_is_byte_identical_in_two_roles(layout):
    roles.compose_roles(layout, {})
    shared = roles._body((layout.roles_dir / "_shared" / "fragments" / "git" / "commit.md").read_text()).strip()
    for path in ("Coder/role.worker.md", "Manager/role.manager.md"):
        assert shared in (layout.roles_dir / path).read_text()


def test_settings_mark_every_unlisted_skill_and_command_name_only(layout):
    roles.compose_roles(layout, {})
    settings = json.loads((layout.roles_dir / "Coder" / "role.settings.json").read_text())
    assert settings == {"skillListingMaxDescChars": 160,
                        "skillOverrides": {"cmd-c": "name-only", "vendor-b": "name-only"}}


def test_a_disabled_mcp_server_list_reaches_role_settings_and_an_extends_child_unions_it(layout):
    _write(layout.roles_dir / "Coder" / "role.toml",
           'core = "Coder"\ntier = "IMPL_HARD"\nlaunch = ["worker", "child"]\n'
           'fragments = ["evidence-map", "commit", "code-style"]\nskills = ["skill-a"]\n'
           '[settings]\nskillListingMaxDescChars = 160\ndisabledMcpjsonServers = ["incident-mcp"]\n')
    _write(layout.roles_dir / "Integrator" / "fragments" / "integrator.md", "INTEGRATOR BODY\n")
    _write(layout.roles_dir / "Integrator" / "role.toml",
           'extends = "Coder"\nlaunch = ["child"]\nfragments = ["integrator"]\n'
           '[settings]\ndisabledMcpjsonServers = ["metrics-mcp"]\n')
    roles.compose_roles(layout, {})
    coder = json.loads((layout.roles_dir / "Coder" / "role.settings.json").read_text())
    integrator = json.loads((layout.roles_dir / "Integrator" / "role.settings.json").read_text())
    assert coder["disabledMcpjsonServers"] == ["incident-mcp"]
    assert integrator["disabledMcpjsonServers"] == ["incident-mcp", "metrics-mcp"]


def test_a_permissions_allow_list_reaches_role_settings_and_unions_with_the_base(layout):
    _write(layout.roles_dir / "Merger" / "role.toml",
           'extends = "Coder"\nlaunch = ["worker"]\nfragments = []\n'
           '[settings]\npermissions = { allow = ["Bash(gh pr merge:*)"] }\n')
    roles.compose_roles(layout, {})
    merger = json.loads((layout.roles_dir / "Merger" / "role.settings.json").read_text())
    assert merger["permissions"] == {"allow": ["Bash(gh pr merge:*)"]}
    base = {"permissions": {"defaultMode": "auto", "allow": ["mcp__dockwright__worker_done"], "deny": ["Write"]}}
    merged = roles.apply_layer(base, roles.read_role_layer(layout.roles_dir / "Merger" / "role.settings.json"))
    assert merged["permissions"] == {"defaultMode": "auto", "deny": ["Write"],
                                     "allow": ["mcp__dockwright__worker_done", "Bash(gh pr merge:*)"]}


def test_own_overlay_lands_at_its_marker_and_shared_overlay_only_where_its_marker_exists(layout):
    _write(layout.roles_dir / "Coder" / "overlay" / "10-x.md", "---\ninsert_at: extra\n---\nOWN DROP\n")
    _write(layout.roles_dir / "_shared" / "overlay" / "10-p.md", "---\ninsert_at: proto-hook\n---\nSHARED DROP\n")
    worker, _, _ = roles.render(layout, _manifest(layout, "Coder"), "worker", {})
    child, _, _ = roles.render(layout, _manifest(layout, "Coder"), "child", {})
    assert "OWN DROP" in worker and "SHARED DROP" in worker
    assert "OWN DROP" in child and "SHARED DROP" not in child


def test_extends_inherits_and_adds(layout):
    _write(layout.roles_dir / "Integrator" / "fragments" / "integrator.md", "INTEGRATOR BODY\n")
    _write(layout.roles_dir / "Integrator" / "role.toml",
           'extends = "Coder"\nlaunch = ["child"]\nfragments = ["integrator"]\nskills = ["cmd-c"]\n')
    m = _manifest(layout, "Integrator")
    assert (m.core, m.launch, m.skills) == ("Coder", ("child",), ("skill-a", "cmd-c"))
    text, _, _ = roles.render(layout, m, "child", {})
    assert text.index("CODE STYLE") < text.index("INTEGRATOR BODY") < text.index("CHILD PROTOCOL")


@pytest.mark.parametrize("body,match", [
    ('core = "Coder"\nbogus = 1\n', "unknown key"),
    ('core = "Coder"\nlaunch = ["desk"]\n', "launch"),
    ('core = "Coder"\n[settings]\nmodel = "x"\n', "settings"),
    ('extends = "Nope"\n', "Nope"),
    ('fragments = ["a"]\n', "`core` is required"),
    ('core = "Coder"\nactions = "build"\n', "must be a list"),
    ('core = "Coder"\nactions = ["nope"]\n', "is not in the actions vocabulary"),
    ('core = "Coder"\nprofiles = ["x"]\n', "is not a profile"),
    ('core = "Coder"\n[settings]\npermissions = { allow = "Bash(x)" }\n', "permissions"),
    ('core = "Coder"\n[settings]\npermissions = { ask = ["Bash(x)"] }\n', "permissions"),
])
def test_a_bad_manifest_is_refused(layout, body, match):
    _write(layout.roles_dir / "Bad" / "role.toml", body)
    with pytest.raises(roles.RoleError, match=match):
        roles.load_manifests(layout.roles_dir)


def test_compose_removes_a_render_for_a_launch_the_manifest_dropped(layout):
    roles.compose_roles(layout, {})
    _write(layout.roles_dir / "Coder" / "role.toml",
           'core = "Coder"\ntier = "IMPL_HARD"\nlaunch = ["worker"]\nfragments = []\n')
    roles.compose_roles(layout, {})
    assert not (layout.roles_dir / "Coder" / "role.child.md").exists()


def test_check_is_fresh_after_compose(layout):
    roles.compose_roles(layout, {})
    assert roles.check_roles(layout, {}) == (True, [])


def test_check_names_each_catalog_defect(layout):
    _write(layout.roles_dir / "former-rules.txt", "core-rule\ncommit\nlost-rule\n")
    _write(layout.roles_dir / "Coder" / "fragments" / "orphan.md", "ORPHAN\n")
    _write(layout.roles_dir / "Coder" / "fragments" / "commit.md", "SECOND COPY\n")
    _write(layout.roles_dir / "Manager" / "role.toml",
           'core = "manager"\nlaunch = ["manager"]\nfragments = ["commit"]\nskills = ["skill-missing"]\n')
    ok, problems = roles.check_roles(layout, {})
    text = "\n".join(problems)
    assert not ok
    assert "(a) former rule lost-rule" in text
    assert "(b) role Coder: fragment 'commit' is in 2 places" in text
    assert "(b) role Manager: skill 'skill-missing' is in no place" in text
    assert "(c) Coder/fragments/orphan.md is named by no manifest" in text
    assert "(d) role Coder: cannot render" in text


def test_check_reports_a_missing_render_and_a_dropin_that_lands_nowhere(layout):
    _write(layout.roles_dir / "Coder" / "overlay" / "10-y.md", "---\ninsert_at: nowhere\n---\nY\n")
    ok, problems = roles.check_roles(layout, {})
    assert not ok
    assert "(d) roles/Coder/role.worker.md is not rendered; run `dockwright compose`" in problems
    assert any("overlay/10-y.md targets marker 'nowhere'" in p for p in problems)


def test_check_reports_a_stale_render_and_a_ceiling(layout, monkeypatch):
    roles.compose_roles(layout, {})
    (layout.roles_dir / "Coder" / "role.worker.md").write_text("edited by hand\n")
    monkeypatch.setattr(roles.config, "agent_ceilings", lambda: {"roles/Coder/role.child.md": {"bytes": 10}})
    ok, problems = roles.check_roles(layout, {})
    assert not ok
    assert any(p.startswith("(d) roles/Coder/role.worker.md is stale") for p in problems)
    assert any(p.startswith("(f) roles/Coder/role.child.md:") for p in problems)


def test_check_finds_a_tracked_citation_of_a_moved_rule(layout):
    roles.compose_roles(layout, {})
    _write(layout.roles_dir / "former-rules.txt", "commit\n")
    _write(layout.claude_dir / "flows" / "f.md", "see `~/.claude/rules/commit.md`\n")
    _write(layout.claude_dir / "agents" / ".compose-stamp.json", json.dumps({"outputs": {"worker.md": "x"}}))
    _write(layout.claude_dir / "agents" / "worker.md", "see rules/commit.md\n")
    subprocess.run(["git", "init", "-q", str(layout.claude_dir)], check=True)
    subprocess.run(["git", "-C", str(layout.claude_dir), "add", "-A"], check=True)
    ok, problems = roles.check_roles(layout, {})
    assert "(e) flows/f.md:1 cites rules/commit.md, which moved out of rules/" in problems
    assert not any("agents/worker.md" in p for p in problems)


def test_launch_files(layout, tmp_path, monkeypatch):
    assert roles.launch_files("Coder", "worker", tmp_path / "empty") is None
    with pytest.raises(roles.RoleError, match="missing; run `dockwright compose`"):
        roles.launch_files("Coder", "worker", layout.roles_dir)
    roles.compose_roles(layout, {})
    render, settings = roles.launch_files("Coder", "worker", layout.roles_dir)
    assert render.name == "role.worker.md" and settings.name == "role.settings.json"
    with pytest.raises(roles.RoleError, match="unknown role 'Nope'"):
        roles.launch_files("Nope", "worker", layout.roles_dir)
    with pytest.raises(roles.RoleError, match="does not declare launch 'lane'"):
        roles.launch_files("Coder", "lane", layout.roles_dir)
    monkeypatch.setattr(paths, "ROLES_DIR", layout.roles_dir)
    with pytest.raises(roles.RoleError, match="unknown role 'GenericWorker'"):
        roles.launch_files(None, "worker")


def test_a_skill_name_in_skills_and_commands_is_one_entry(layout):
    _write(layout.claude_dir / "commands" / "skill-a.md", "---\ndescription: a\n---\n")
    roles.compose_roles(layout, {})
    assert roles.check_roles(layout, {}) == (True, [])


def test_a_broken_manifest_of_another_role_does_not_block_a_launch(layout):
    roles.compose_roles(layout, {})
    _write(layout.roles_dir / "Broken" / "role.toml", "bogus = 1\n")
    render, _ = roles.launch_files("Coder", "worker", layout.roles_dir)
    assert render.name == "role.worker.md"


def test_spawn_settings_are_content_addressed(tmp_path):
    first = roles.write_spawn_settings({"b": 1, "a": [1]})
    assert roles.write_spawn_settings({"a": [1], "b": 1}) == first
    assert first.parent == paths.ROLE_SETTINGS
    assert json.loads(first.read_text()) == {"a": [1], "b": 1}


def test_role_args_prints_one_argument_per_line(layout, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(paths, "ROLES_DIR", layout.roles_dir)
    roles.compose_roles(layout, {})
    base = _write(tmp_path / "base.json", json.dumps({"permissions": {"allow": ["Bash"]}}))
    assert roles.main(["Coder", "--launch", "worker", "--base", str(base)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "--append-system-prompt-file" and lines[1].endswith("Coder/role.worker.md")
    merged = json.loads(Path(lines[3]).read_text())
    assert merged["permissions"] == {"allow": ["Bash"]} and merged["skillListingMaxDescChars"] == 160
    assert roles.main(["Nope", "--launch", "worker"]) == 2


@pytest.mark.parametrize("text,expected", [
    (json.dumps({"bogus": 1}), "unknown profile key 'bogus'"),
    ("{not json", "role.settings.json"),
])
def test_role_args_refuses_a_settings_file_outside_the_profile_keys(layout, tmp_path, monkeypatch, capsys, text, expected):
    monkeypatch.setattr(paths, "ROLES_DIR", layout.roles_dir)
    roles.compose_roles(layout, {})
    (layout.roles_dir / "Coder" / "role.settings.json").write_text(text)
    base = _write(tmp_path / "base.json", json.dumps({"permissions": {"deny": ["Write"]}}))
    assert roles.main(["Coder", "--launch", "worker", "--base", str(base)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and expected in captured.err


def test_role_args_accepts_permissions_in_the_role_layer_and_unions_them_with_the_base(layout, tmp_path, monkeypatch,
                                                                                       capsys):
    monkeypatch.setattr(paths, "ROLES_DIR", layout.roles_dir)
    roles.compose_roles(layout, {})
    (layout.roles_dir / "Coder" / "role.settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(gh pr merge:*)"]}}))
    base = _write(tmp_path / "base.json", json.dumps({"permissions": {"deny": ["Write"], "allow": ["Bash(printenv:*)"]}}))
    assert roles.main(["Coder", "--launch", "worker", "--base", str(base)]) == 0
    lines = capsys.readouterr().out.splitlines()
    merged = json.loads(Path(lines[3]).read_text())
    assert merged["permissions"] == {"deny": ["Write"], "allow": ["Bash(printenv:*)", "Bash(gh pr merge:*)"]}


def test_role_args_without_manifests_prints_only_the_base(tmp_path, capsys):
    base = _write(tmp_path / "base.json", "{}")
    assert roles.main(["GenericWorker", "--launch", "lane", "--base", str(base)]) == 0
    assert capsys.readouterr().out.splitlines() == ["--settings", str(base)]


def test_a_core_with_include_lines_renders_the_included_fragments(layout):
    _write(layout.core_dirs[0] / "Legacy.core.md", "# Legacy\n<!-- include: evidence-map -->\n")
    _write(layout.roles_dir / "Legacy" / "role.toml", 'core = "Legacy"\nlaunch = ["lane"]\n')
    text, _, _ = roles.render(layout, _manifest(layout, "Legacy"), "lane", {})
    assert text == "<!-- dockwright-role: Legacy launch=lane -->\n\n# Legacy\nEVIDENCE MAP\n"


@pytest.mark.parametrize("where", ["fragment", "protocol", "dropin"])
def test_an_include_line_left_in_a_render_is_refused(layout, where):
    line = "<!-- include: evidence-map -->\n"
    target = {"fragment": layout.roles_dir / "Coder" / "fragments" / "code-style.md",
              "protocol": layout.canon_fragments / "worker-protocol.md",
              "dropin": layout.roles_dir / "Coder" / "overlay" / "10-i.md"}[where]
    _write(target, f"BODY\n{line}")
    with pytest.raises(roles.RoleError, match="role Coder.*include"):
        roles.render(layout, _manifest(layout, "Coder"), "worker", {})
    ok, problems = roles.check_roles(layout, {})
    assert not ok and any(p.startswith("(d) role Coder: cannot render:") and "include" in p for p in problems)


def test_check_reports_a_role_ceiling_that_names_no_render(layout, monkeypatch):
    roles.compose_roles(layout, {})
    monkeypatch.setattr(roles.config, "agent_ceilings",
                        lambda: {"roles/Coder/role.wroker.md": {"bytes": 1}, "manager.md": {"bytes": 1},
                                 "roles/Coder/role.worker.md": {"bytes": 100000}})
    assert roles.check_roles(layout, {}) == (
        False, ["(f) roles/Coder/role.wroker.md: named in [ceilings] but no role renders it"])


def test_check_reports_a_role_ceiling_when_no_role_has_a_manifest(tmp_path, monkeypatch):
    empty = roles.Layout(tmp_path / "roles", (tmp_path / "canon", tmp_path / "agents"), tmp_path)
    monkeypatch.setattr(roles.config, "agent_ceilings", lambda: {"roles/Nope/role.worker.md": {"bytes": 1}})
    assert roles.check_roles(empty, {}) == (
        False, ["(f) roles/Nope/role.worker.md: named in [ceilings] but no role renders it"])


def test_a_role_that_cannot_render_is_reported_once_not_again_through_its_ceiling(layout, monkeypatch):
    _write(layout.roles_dir / "Coder" / "overlay" / "10-i.md", "<!-- include: evidence-map -->\n")
    monkeypatch.setattr(roles.config, "agent_ceilings", lambda: {"roles/Coder/role.worker.md": {"bytes": 1}})
    ok, problems = roles.check_roles(layout, {})
    assert not ok and not any("named in [ceilings]" in p for p in problems)


def test_a_fragment_reached_twice_renders_once(layout):
    _write(layout.roles_dir / "Integrator" / "role.toml",
           'extends = "Coder"\nlaunch = ["child"]\nfragments = ["evidence-map", "child-protocol"]\n')
    text, _, _ = roles.render(layout, _manifest(layout, "Integrator"), "child", {})
    assert text.count("EVIDENCE MAP") == 1 and text.count("CHILD PROTOCOL") == 1


def test_a_fragment_the_core_includes_is_not_appended_again(layout):
    _write(layout.core_dirs[1] / "legacy.core.md", "---\nname: legacy\n---\n<!-- include: worker-protocol -->\n")
    _write(layout.roles_dir / "Legacy" / "role.toml", 'core = "legacy"\nlaunch = ["worker"]\n')
    text, _, _ = roles.render(layout, _manifest(layout, "Legacy"), "worker", {})
    assert text.count("WORKER PROTOCOL") == 1


def test_check_without_git_reports_instead_of_crashing(layout, monkeypatch):
    roles.compose_roles(layout, {})
    _write(layout.roles_dir / "former-rules.txt", "commit\n")
    monkeypatch.setenv("PATH", str(layout.claude_dir / "no-bin"))
    ok, problems = roles.check_roles(layout, {})
    assert not ok and any(p.startswith("(e) git unavailable") for p in problems), problems


def test_compose_keeps_a_role_file_it_did_not_render(layout):
    notes = _write(layout.roles_dir / "Coder" / "role.notes.md", "operator notes\n")
    roles.compose_roles(layout, {})
    assert notes.read_text() == "operator notes\n"


def test_check_reports_an_unbound_var_in_a_render(layout):
    _write(layout.roles_dir / "Coder" / "fragments" / "code-style.md", "CODE STYLE {{nosuch}}\n")
    roles.compose_roles(layout, {})
    ok, problems = roles.check_roles(layout, {})
    assert not ok and any(p.startswith("(d) role Coder/worker/none:") and "nosuch" in p for p in problems), problems


def test_a_role_layer_never_loosens_a_skill_override_and_role_args_keeps_the_stricter(layout, tmp_path, monkeypatch, capsys):
    base = {"skillOverrides": {"a": "off", "b": "user-invocable-only", "c": "on", "d": "name-only"}, "x": 1}
    layer = {"skillOverrides": {"a": "name-only", "b": "name-only", "c": "name-only", "d": "off", "e": "name-only"}}
    assert roles.apply_layer(base, layer) == {
        "x": 1, "skillOverrides": {"a": "off", "b": "user-invocable-only", "c": "name-only", "d": "off",
                                   "e": "name-only"}}
    monkeypatch.setattr(paths, "ROLES_DIR", layout.roles_dir)
    roles.compose_roles(layout, {})
    base_file = _write(tmp_path / "base.json", json.dumps({"skillOverrides": {"vendor-b": "off"}}))
    assert roles.main(["Coder", "--launch", "worker", "--base", str(base_file)]) == 0
    merged = json.loads(Path(capsys.readouterr().out.splitlines()[3]).read_text())
    assert merged["skillOverrides"] == {"cmd-c": "name-only", "vendor-b": "off"}


@pytest.mark.parametrize("removed", [("Manager",), ("Coder", "Manager")])
def test_compose_deletes_the_renders_of_a_role_whose_manifest_is_gone_and_check_reports_them(layout, removed):
    roles.compose_roles(layout, {})
    notes = _write(layout.roles_dir / "Manager" / "role.notes.md", "operator notes\n")
    for name in removed:
        (layout.roles_dir / name / "role.toml").unlink()
    ok, problems = roles.check_roles(layout, {})
    assert not ok
    assert "(d) roles/Manager/role.manager.md is a render with no manifest; run `dockwright compose`" in problems
    roles.compose_roles(layout, {})
    assert not (layout.roles_dir / "Manager" / "role.manager.md").exists()
    assert notes.read_text() == "operator notes\n"
    assert all(not any(p.startswith(f"(d) roles/{n}/role.") for p in roles.check_roles(layout, {})[1]) for n in removed)


def test_a_manifest_tier_key_is_ignored_and_remembered(tmp_path, monkeypatch):
    root = tmp_path / "roles"
    (root / "X").mkdir(parents=True)
    (root / "X" / "role.toml").write_text('core = "x"\ntier = "REVIEW_TIER2"\nlaunch = ["child"]\n')
    monkeypatch.setattr(roles, "IGNORED_TIER", set())
    m = roles.load_role("X", root)
    roles.load_role("X", root)
    assert not hasattr(m, "tier") and roles.IGNORED_TIER == {root / "X" / "role.toml"}


def test_parse_unit_reads_the_four_keys_and_strips_the_header():
    meta, body = roles.parse_unit("---\nkind: guard\nbinds: [git-write, push]\ndomain: git\ncites: [merge]\n---\nBODY\n")
    assert meta == {"kind": "guard", "binds": ["git-write", "push"], "domain": "git", "cites": ["merge"]}
    assert body == "BODY\n"
    assert roles.parse_unit("BODY\n") == ({}, "BODY\n")


def test_load_units_finds_pool_role_canon_and_profile_units(layout, profile_settings):
    units = roles.load_units(layout)
    assert units["commit"].home == "shared" and units["commit"].kind == "guard"
    assert units["commit"].binds == ("git-write", "push") and units["commit"].domain == "git"
    assert units["code-style"].home == "role:Coder"
    assert units["evidence-map"].home == "canon"
    assert units["acme-env"].home == "profile:acme" and units["acme-be-style"].home == "profile:acme-be"
    assert "worker-protocol" not in units and "child-protocol" not in units


def test_overlay_dropins_are_catalog_units_that_no_role_computes(layout, profile_settings):
    _write(layout.roles_dir / "Manager" / "overlay" / "10-relay.md",
           "---\ninsert_at: relay\nkind: guard\nbinds: [push]\ndomain: git\n---\nRELAY\n")
    units = roles.load_units(layout)
    assert units["overlay:Manager/10-relay"].home == "overlay:Manager" and units["overlay:Manager/10-relay"].kind == "guard"
    assert "overlay:Manager/10-relay" not in roles.compute_fragments(_manifest(layout, "Manager"), units)


def test_compute_fragments_binds_guard_on_reachable_and_the_rest_on_actions(layout, profile_settings):
    units = roles.load_units(layout)
    coder = _manifest(layout, "Coder")
    assert roles.compute_fragments(coder, units) == ["code-style", "commit", "evidence-map"]
    manager = _manifest(layout, "Manager")
    assert roles.compute_fragments(manager, units) == ["commit"]
    narrowed = roles.Manifest("X", "Coder", (), (), ("worker",), {}, ("X",), actions=("build",), reachable=())
    assert roles.compute_fragments(narrowed, units) == ["evidence-map"]


def test_manifest_actions_and_reachable_merge_through_extends(layout):
    _write(layout.roles_dir / "Fixer" / "role.toml", 'extends = "Coder"\nactions = ["deploy"]\nreachable = ["build"]\n')
    fixer = _manifest(layout, "Fixer")
    assert fixer.actions == ("build", "git-write", "deploy") and fixer.reachable == ("push", "build")
    assert fixer.fragments_declared is True


def test_render_on_a_profile_appends_its_units_after_the_role_units_before_overlays(layout, profile_settings):
    _write(layout.roles_dir / "Coder" / "overlay" / "10-extra.md", "---\ninsert_at: extra\n---\nOVERLAY\n")
    text, _, _ = roles.render(layout, _manifest(layout, "Coder"), "worker", {}, profile="back")
    assert text.splitlines()[0] == "<!-- dockwright-role: Coder launch=worker -->"
    assert text.splitlines()[1] == "<!-- dockwright-profile: back -->"
    assert text.index("CODE STYLE") < text.index("ACME ENV") < text.index("ACME BE STYLE") < text.index("WORKER PROTOCOL")
    assert "OVERLAY" in text and "kind: fact" not in text
    none, _, _ = roles.render(layout, _manifest(layout, "Coder"), "worker", {})
    assert "ACME" not in none and "dockwright-profile" not in none


def test_profile_excludes_drop_a_unit_until_the_ruling(layout, profile_settings):
    _write(layout.roles_dir / "Coder" / "role.toml",
           'core = "Coder"\nlaunch = ["worker"]\nactions = ["build"]\nfragments = ["code-style"]\n'
           'profile_excludes = ["acme-be-style"]\n')
    text, _, _ = roles.render(layout, _manifest(layout, "Coder"), "worker", {}, profile="back")
    assert "ACME ENV" in text and "ACME BE STYLE" not in text


def test_computed_mode_filters_pool_and_profile_units_by_binds(layout, profile_settings, monkeypatch):
    monkeypatch.setattr(roles.config, "roles_computed", lambda: True)
    _write(layout.roles_dir / "Coder" / "role.toml",
           'core = "Coder"\nlaunch = ["worker"]\nactions = ["review-code"]\nfragments = ["evidence-map"]\n')
    text, _, _ = roles.render(layout, _manifest(layout, "Coder"), "worker", {}, profile="back")
    assert "ACME BE STYLE" in text and "ACME ENV" not in text
    assert "EVIDENCE MAP" not in text and "CODE STYLE" in text


def test_agent_launch_renders_marker_core_units_and_no_protocol(layout):
    _write(layout.core_dirs[2] / "general-purpose.core.md",
           "---\nname: general-purpose\ndescription: Finds things.\n---\n# Scout core\n")
    _write(layout.roles_dir / "Scout" / "role.toml",
           'core = "general-purpose"\nlaunch = ["agent"]\nactions = ["build"]\nfragments = ["commit"]\n')
    text, _, _ = roles.render(layout, _manifest(layout, "Scout"), "agent", {})
    assert text.startswith("<!-- dockwright-role: Scout launch=agent -->\n\n# Scout core\n")
    assert "COMMIT RULE" in text and "PROTOCOL" not in text and "name: general-purpose" not in text
    assert roles._launches(_manifest(layout, "Scout")) == ["agent"]


def test_role_without_verbs_renders_marker_and_core_only(layout, monkeypatch):
    monkeypatch.setattr(roles.config, "roles_computed", lambda: True)
    _write(layout.core_dirs[0] / "Blank.core.md", "# Blank\n\nYou run one headless task.\n")
    _write(layout.roles_dir / "Blank" / "role.toml",
           'core = "Blank"\nlaunch = ["lane"]\nactions = []\nreachable = []\nprofiles = []\n')
    text, _, _ = roles.render(layout, _manifest(layout, "Blank"), "lane", {})
    assert text == "<!-- dockwright-role: Blank launch=lane -->\n\n# Blank\n\nYou run one headless task.\n"
    files, _, _ = roles.outputs(layout, _manifest(layout, "Blank"), {})
    assert sorted(p.name for p in files) == ["role.lane.md", "role.settings.json"]


def test_outputs_cover_every_launch_and_profile(layout, profile_settings):
    files, _, _ = roles.outputs(layout, _manifest(layout, "Coder"), {})
    names = sorted(p.name for p in files)
    assert names == sorted(["role.worker.md", "role.worker.common.md", "role.worker.back.md",
                            "role.child.md", "role.child.common.md", "role.child.back.md",
                            "role.codex.md", "role.codex.common.md", "role.codex.back.md", "role.settings.json"])
    assert roles._is_render(_write(layout.roles_dir / "Coder" / "role.worker.back.md",
                                   files[layout.roles_dir / "Coder" / "role.worker.back.md"]), "Coder")


def test_a_role_can_restrict_its_profiles(layout, profile_settings):
    _write(layout.roles_dir / "Manager" / "role.toml",
           'core = "manager"\nlaunch = ["manager"]\nactions = ["push"]\nfragments = ["commit"]\nprofiles = ["common"]\n')
    files, _, _ = roles.outputs(layout, _manifest(layout, "Manager"), {})
    assert sorted(p.name for p in files) == ["role.manager.common.md", "role.manager.md", "role.settings.json"]


def test_plan_writes_profile_bundles_and_agents_json(layout, profile_settings, monkeypatch):
    monkeypatch.setattr(roles.config, "subagents", lambda: {
        "general-purpose": {"role": "Scout", "core": "general-purpose"}})
    _write(layout.core_dirs[2] / "general-purpose.core.md",
           "---\nname: general-purpose\ndescription: Finds things.\ntools: Read, Grep\nmodel: sonnet\n---\n# Scout core\n")
    _write(layout.roles_dir / "Scout" / "role.toml",
           'core = "general-purpose"\nlaunch = ["agent"]\nactions = ["build"]\nfragments = ["commit"]\n')
    plan = roles.plan_roles(layout, {})
    bundle = plan.files[layout.roles_dir / roles.PROFILES_DIR / "back.md"]
    assert bundle.startswith("<!-- dockwright-profile: back -->\n") and "ACME BE STYLE" in bundle
    assert layout.roles_dir / roles.PROFILES_DIR / "none.md" not in plan.files
    agents = json.loads(plan.files[layout.roles_dir / roles.AGENTS_DIR / "back.json"])
    entry = agents["general-purpose"]
    assert entry["description"] == "Finds things." and entry["tools"] == ["Read", "Grep"] and entry["model"] == "sonnet"
    assert entry["prompt"].startswith("<!-- dockwright-role: Scout launch=agent -->\n<!-- dockwright-profile: back -->\n")
    assert "ACME ENV" in entry["prompt"] and "COMMIT RULE" in entry["prompt"]
    none = json.loads(plan.files[layout.roles_dir / roles.AGENTS_DIR / "none.json"])
    assert "ACME" not in none["general-purpose"]["prompt"]


def test_no_subagents_table_means_no_agents_json(layout, profile_settings, monkeypatch):
    monkeypatch.setattr(roles.config, "subagents", lambda: {})
    plan = roles.plan_roles(layout, {})
    assert not [p for p in plan.files if p.parent.name == roles.AGENTS_DIR]


def test_skills_star_lists_every_skill(layout):
    _write(layout.roles_dir / "Coder" / "role.toml",
           'core = "Coder"\nlaunch = ["worker"]\nfragments = []\nskills = ["*"]\n[settings]\nskillListingMaxDescChars = 160\n')
    assert roles.role_settings(layout, _manifest(layout, "Coder")) == {"skillListingMaxDescChars": 160}


def test_launch_files_and_codex_text_take_a_profile(layout, profile_settings, monkeypatch):
    roles.compose_roles(layout, {})
    render, settings = roles.launch_files("Coder", "worker", layout.roles_dir, profile="back")
    assert render.name == "role.worker.back.md" and settings.name == "role.settings.json"
    assert roles.codex_text("Coder", layout.roles_dir, profile="back").splitlines()[1] == "<!-- dockwright-profile: back -->"
    with pytest.raises(roles.RoleError, match="role.worker.front.md"):
        roles.launch_files("Coder", "worker", layout.roles_dir, profile="front")


def test_agents_arg_names_the_profile_file_or_nothing(layout, profile_settings, monkeypatch):
    monkeypatch.setattr(roles.config, "subagents", lambda: {"general-purpose": {"role": "Scout", "core": "general-purpose"}})
    _write(layout.core_dirs[2] / "general-purpose.core.md", "---\nname: general-purpose\ndescription: d\n---\nCORE\n")
    _write(layout.roles_dir / "Scout" / "role.toml", 'core = "general-purpose"\nlaunch = ["agent"]\nfragments = []\n')
    roles.compose_roles(layout, {})
    assert roles.agents_arg("back", layout.roles_dir) == ["--agents-file", str(layout.roles_dir / "_agents" / "back.json")]
    assert roles.agents_arg("back", layout.roles_dir, print_mode=True) == ["--agents", str(layout.roles_dir / "_agents" / "back.json")]
    assert roles.agents_arg("nope", layout.roles_dir) == []


def test_role_args_cli_prints_the_three_pairs_and_honours_render_only(layout, profile_settings, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(roles.config, "subagents", lambda: {"general-purpose": {"role": "Scout", "core": "general-purpose"}})
    _write(layout.core_dirs[2] / "general-purpose.core.md", "---\nname: general-purpose\ndescription: d\n---\nCORE\n")
    _write(layout.roles_dir / "Scout" / "role.toml", 'core = "general-purpose"\nlaunch = ["agent"]\nfragments = []\n')
    roles.compose_roles(layout, {})
    monkeypatch.setattr(paths, "ROLES_DIR", layout.roles_dir)
    monkeypatch.setattr(paths, "ROLE_SETTINGS", tmp_path / "spawn-settings")
    repo = tmp_path / "work" / "acme-web"
    repo.mkdir(parents=True)
    assert roles.main(["Coder", "--launch", "worker", "--cwd", str(repo)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "--append-system-prompt-file" and lines[1].endswith("Coder/role.worker.common.md")
    assert lines[2] == "--settings" and lines[4] == "--agents-file" and lines[5].endswith("_agents/common.json")
    assert roles.main(["Coder", "--launch", "worker", "--cwd", str(repo), "--render-only"]) == 0
    assert capsys.readouterr().out.splitlines() == ["--append-system-prompt-file", lines[1]]


def test_default_role_args_pick_the_profile_of_the_cwd(layout, profile_settings, monkeypatch, tmp_path):
    monkeypatch.setattr(roles.config, "subagents", lambda: {"general-purpose": {"role": "Scout", "core": "general-purpose"}})
    _write(layout.core_dirs[2] / "general-purpose.core.md", "---\nname: general-purpose\ndescription: d\n---\nCORE\n")
    _write(layout.roles_dir / "Scout" / "role.toml", 'core = "general-purpose"\nlaunch = ["agent"]\nfragments = []\n')
    _write(layout.roles_dir / "GenericWorker" / "role.toml", 'core = "Coder"\nlaunch = ["worker"]\nfragments = []\n')
    roles.compose_roles(layout, {})
    monkeypatch.setattr(paths, "ROLES_DIR", layout.roles_dir)
    monkeypatch.setattr(paths, "ROLE_SETTINGS", tmp_path / "spawn-settings")
    repo = tmp_path / "work" / "acme-api"
    repo.mkdir(parents=True)
    args = roles.default_role_args(["--model", "m"], "worker", cwd=repo)
    assert args[:2] == ["--append-system-prompt-file", str(layout.roles_dir / "GenericWorker" / "role.worker.back.md")]
    assert args[2:4] == ["--model", "m"] and args[4] == "--settings"
    assert args[6:] == ["--agents-file", str(layout.roles_dir / "_agents" / "back.json")]


def _check(layout):
    ok, problems = roles.check_roles(layout, {})
    return ok, problems


def test_check_g_refuses_a_unit_without_frontmatter_only_when_strict(layout, profile_settings, monkeypatch):
    _write(layout.roles_dir / "_shared" / "fragments" / "git" / "bare.md", "NO HEADER\n")
    roles.compose_roles(layout, {})
    assert "(g)" not in " ".join(_check(layout)[1])
    monkeypatch.setattr(roles.config, "roles_strict", lambda: True)
    ok, problems = _check(layout)
    assert not ok and any(p.startswith("(g) ") and "bare.md" in p and "kind" in p for p in problems)


def test_check_g_refuses_a_bad_verb_domain_or_folder(layout, profile_settings, monkeypatch):
    monkeypatch.setattr(roles.config, "roles_strict", lambda: True)
    _write(layout.roles_dir / "_shared" / "fragments" / "git" / "odd.md", _unit("guard", ["fly"], "sky", "ODD"))
    _write(layout.roles_dir / "_shared" / "fragments" / "build" / "misplaced.md", _unit("fact", ["build"], "git", "MIS"))
    roles.compose_roles(layout, {})
    problems = _check(layout)[1]
    assert any("odd.md" in p and "fly" in p for p in problems)
    assert any("odd.md" in p and "sky" in p for p in problems)
    assert any("misplaced.md" in p and "folder" in p for p in problems)


def test_check_h_rules_dir_holds_only_the_tripwire_once_armed(layout, profile_settings):
    roles.compose_roles(layout, {})
    _write(layout.claude_dir / "rules" / "leftover.md", "X\n")
    assert "(h)" not in " ".join(_check(layout)[1])
    _write(layout.claude_dir / "rules" / roles.TRIPWIRE_RULE, "TRIPWIRE\n")
    assert any(p.startswith("(h) ") and "leftover.md" in p for p in _check(layout)[1])


def test_check_i_warns_on_two_guards_sharing_a_bind_and_domain_without_cites(layout, profile_settings):
    _write(layout.roles_dir / "_shared" / "fragments" / "git" / "commit2.md", _unit("guard", ["push"], "git", "TWIN"))
    _write(layout.roles_dir / "Manager" / "role.toml",
           'core = "manager"\nlaunch = ["manager"]\nactions = ["push"]\nfragments = ["commit", "commit2"]\n')
    roles.compose_roles(layout, {})
    ok, problems = _check(layout)
    assert ok and any(p.startswith("(i) ") and "commit" in p and "commit2" in p and "push" in p for p in problems)
    _write(layout.roles_dir / "_shared" / "fragments" / "git" / "commit2.md", _unit("guard", ["push"], "git", "TWIN", cites=["push"]))
    roles.compose_roles(layout, {})
    assert not [p for p in _check(layout)[1] if p.startswith("(i) ")]


def test_check_c_sees_an_orphan_in_a_domain_folder_and_a_and_c_follow_the_effective_set(layout, profile_settings, monkeypatch):
    _write(layout.roles_dir / "_shared" / "fragments" / "git" / "orphan.md", _unit("guard", ["deploy"], "git", "ORPHAN"))
    roles.compose_roles(layout, {})
    assert any(p.startswith("(c) ") and "git/orphan.md" in p for p in _check(layout)[1])
    monkeypatch.setattr(roles.config, "roles_computed", lambda: True)
    _write(layout.roles_dir / roles.FORMER_RULES, "commit\n")
    _write(layout.roles_dir / "Coder" / "role.toml", 'core = "Coder"\nlaunch = ["worker"]\nactions = ["git-write", "deploy"]\n')
    roles.compose_roles(layout, {})
    problems = _check(layout)[1]
    assert not [p for p in problems if p.startswith("(a) ") or p.startswith("(c) ")]


def test_check_f_profile_render_is_bounded_by_role_plus_profile_ceiling(layout, profile_settings, monkeypatch):
    roles.compose_roles(layout, {})
    monkeypatch.setattr(roles.config, "agent_ceilings", lambda: {
        "roles/Coder/role.worker.md": {"bytes": 60}, "profiles/back.md": {"bytes": 10}})
    problems = _check(layout)[1]
    assert any(p.startswith("(f) roles/Coder/role.worker.back.md") and "over the [ceilings] 70 B" in p for p in problems)
    assert any(p.startswith("(f) profiles/back.md") for p in problems)
    assert not any("named in [ceilings] but no role renders it" in p for p in problems)


def _grown_units(tmp_path, body):
    units = tmp_path / "grown-units"
    _write(units / "common" / "acme-env.md", _unit("fact", ["build"], "acme", "ACME ENV"))
    _write(units / "back" / "acme-be-style.md", _unit("fact", ["build", "review-code"], "acme-be", "ACME BE STYLE"))
    _write(units / "back" / "acme-be-big.md", _unit("fact", ["build"], "acme-be", body))
    return units


def test_check_f_measures_the_bundle_compose_would_write_not_the_one_on_disk(layout, profile_settings, monkeypatch, tmp_path):
    roles.compose_roles(layout, {})
    bundle = layout.roles_dir / roles.PROFILES_DIR / "back.md"
    ceiling = len(bundle.read_bytes()) + 50
    monkeypatch.setattr(roles.config, "agent_ceilings", lambda: {"profiles/back.md": {"bytes": ceiling}})
    monkeypatch.setattr(roles.config, "project_units_dir", lambda: _grown_units(tmp_path, "X" * 500))
    lines = [p for p in _check(layout)[1] if p.startswith("(f) profiles/back.md")]
    roles.compose_roles(layout, {})
    assert lines == [f"(f) profiles/back.md: {len(bundle.read_bytes())} B is over the [ceilings] {ceiling} B"]


def test_check_f_passes_a_bundle_compose_would_write_under_its_ceiling(layout, profile_settings, monkeypatch, tmp_path):
    roles.compose_roles(layout, {})
    ceiling = len((layout.roles_dir / roles.PROFILES_DIR / "back.md").read_bytes()) + 50
    small = roles.config.project_units_dir()
    monkeypatch.setattr(roles.config, "project_units_dir", lambda: _grown_units(tmp_path, "X" * 500))
    roles.compose_roles(layout, {})
    monkeypatch.setattr(roles.config, "project_units_dir", lambda: small)
    monkeypatch.setattr(roles.config, "agent_ceilings", lambda: {"profiles/back.md": {"bytes": ceiling}})
    assert not [p for p in _check(layout)[1] if p.startswith("(f) profiles/back.md")]


def test_check_l_wants_a_read_by_line_naming_existing_paths(layout, profile_settings):
    roles.compose_roles(layout, {})
    flow = _write(layout.claude_dir / "flows" / "f.md",
                  "# F\n\n## One\n\nRead by: `skills/skill-a/SKILL.md`.\n\n## Two\n\ntext\n")
    ok, problems = _check(layout)
    assert ok and any(p.startswith("(l) flows/f.md § Two") and "no `Read by:`" in p for p in problems)
    assert not any("skill-a" in p for p in problems)
    flow.write_text("# F\n\n## One\n\nRead by: `flows/nope.md`.\n")
    ok, problems = _check(layout)
    assert not ok and any(p.startswith("(l) flows/f.md § One") and "flows/nope.md" in p for p in problems)


def test_check_m_warns_past_the_size_target(layout, profile_settings, monkeypatch):
    monkeypatch.setattr(roles, "SIZE_TARGETS", {"Coder": 10})
    roles.compose_roles(layout, {})
    ok, problems = _check(layout)
    assert ok and any(p.startswith("(m) roles/Coder/role.worker.md") for p in problems)


def test_stale_citations_catch_rules_and_flat_shared_forms_in_three_repos(layout, profile_settings, tmp_path, monkeypatch):
    for root in (layout.claude_dir, tmp_path):
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    _write(layout.roles_dir / roles.FORMER_RULES, "commit\nstyle\n")
    _write(layout.claude_dir / "flows" / "f.md", "see rules/style.md and roles/_shared/fragments/commit.md\n")
    _write(tmp_path / "deploy" / "roles" / "Coder.core.md", "# Coder core\ncite roles/_shared/fragments/commit.md\n<!-- overlay: extra -->\n")
    _write(tmp_path / "project-units" / "common" / "acme-env.md", _unit("fact", ["build"], "acme", "see rules/style.md"))
    subprocess.run(["git", "-C", str(layout.claude_dir), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "deploy", "project-units"], check=True)
    monkeypatch.setattr(roles.config, "dockwright_repo", lambda: str(tmp_path))
    problems = roles.stale_citations(layout)
    assert any("flows/f.md:1 cites rules/style.md" in p for p in problems)
    assert any("flows/f.md:1 cites roles/_shared/fragments/commit.md" in p for p in problems)
    assert any("deploy/roles/Coder.core.md:2" in p for p in problems)
    assert any("common/acme-env.md" in p and "rules/style.md" in p for p in problems)


def test_compose_without_a_roles_dir_writes_no_catalog(tmp_path):
    layout = roles.Layout(tmp_path / "no-roles", roles.core_dirs_for(tmp_path / "agents"), tmp_path)
    assert roles.compose_roles(layout, {}) == {"files": [], "warnings": []}
    assert not (tmp_path / "no-roles").exists()


def test_role_args_prints_agents_with_the_path_for_the_lane_launch(layout, profile_settings, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(roles.config, "subagents", lambda: {"general-purpose": {"role": "Scout", "core": "general-purpose"}})
    _write(layout.core_dirs[2] / "general-purpose.core.md", "---\nname: general-purpose\ndescription: d\n---\nCORE\n")
    _write(layout.roles_dir / "Scout" / "role.toml", 'core = "general-purpose"\nlaunch = ["agent"]\nfragments = []\n')
    _write(layout.roles_dir / "Coder" / "role.toml",
           'core = "Coder"\nlaunch = ["worker", "lane"]\nactions = ["build"]\nfragments = ["commit"]\n')
    roles.compose_roles(layout, {})
    monkeypatch.setattr(paths, "ROLES_DIR", layout.roles_dir)
    monkeypatch.setattr(paths, "ROLE_SETTINGS", tmp_path / "spawn-settings")
    repo = tmp_path / "work" / "acme-web"
    repo.mkdir(parents=True)
    assert roles.main(["Coder", "--launch", "lane", "--cwd", str(repo)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[4] == "--agents" and lines[5].endswith("_agents/common.json")
    assert roles.main(["Coder", "--launch", "worker", "--cwd", str(repo)]) == 0
    assert capsys.readouterr().out.splitlines()[4] == "--agents-file"


def test_a_configured_project_units_dir_that_is_missing_fails_loud(layout, profile_settings, monkeypatch, tmp_path):
    monkeypatch.setattr(roles.config, "project_units_dir", lambda: tmp_path / "gone")
    with pytest.raises(roles.RoleError, match="project_units_dir"):
        roles.load_units(layout)
    ok, problems = roles.check_roles(layout, {})
    assert not ok and any("project_units_dir" in p for p in problems)


def test_check_n_reports_a_profile_domain_that_no_unit_carries(layout, profile_settings, monkeypatch):
    sets = {"none": [], "common": ["acme"], "back": ["acme", "acme-be"], "front": ["acme", "acme-fe"]}
    monkeypatch.setattr(roles.config, "profiles", lambda: {**profile_settings, "sets": sets})
    roles.compose_roles(layout, {})
    ok, problems = roles.check_roles(layout, {})
    assert not ok
    assert any(p.startswith("(n) ") and "acme-fe" in p and "front" in p for p in problems), problems
    assert not any("acme-be" in p and p.startswith("(n) ") for p in problems)
