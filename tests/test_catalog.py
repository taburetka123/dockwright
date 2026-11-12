import json

import pytest

from dockwright import catalog, roles
from tests.test_roles_compose import _manifest, _unit, _write, layout, profile_settings  # noqa: F401


def test_build_lists_units_roles_actions_and_subagents(layout, profile_settings, monkeypatch):
    monkeypatch.setattr(roles.config, "subagents", lambda: {"general-purpose": {"role": "Scout", "core": "general-purpose"}})
    _write(layout.core_dirs[2] / "general-purpose.core.md", "---\nname: general-purpose\ndescription: d\n---\nCORE\n")
    _write(layout.roles_dir / "Scout" / "role.toml", 'core = "general-purpose"\nlaunch = ["agent"]\nactions = ["git-write"]\nfragments = ["commit"]\n')
    monkeypatch.setattr(catalog, "_citing_files", lambda layout, name: ["flows/x.md"])
    data = catalog.build(layout, {})
    commit = data["units"]["commit"]
    assert commit["kind"] == "guard" and commit["domain"] == "git" and commit["home"] == "shared"
    assert commit["binds"] == ["git-write", "push"] and commit["bytes"] > 0 and commit["cited_by"] == ["flows/x.md"]
    assert commit["manual_roles"] == ["Coder", "Manager", "Scout"] and commit["computed_roles"] == ["Coder", "Manager", "Scout"]
    coder = data["roles"]["Coder"]
    assert coder["manual"] == ["evidence-map", "commit", "code-style"]
    assert coder["computed"] == ["code-style", "commit", "evidence-map"]
    assert coder["guards_manual"] == ["commit"] and coder["manual_minus_computed"] == [] and coder["computed_minus_manual"] == []
    assert coder["units"] == ["evidence-map", "commit", "code-style"] and coder["guards"] == ["commit"] and commit["roles"] == ["Coder", "Manager", "Scout"]
    assert coder["profile_units"]["back"] == ["acme-env", "acme-be-style"]
    assert coder["profile_filter"]["back"] == {"would_remove": [], "would_add": []}
    assert data["roles"]["Manager"]["profile_filter"]["back"] == {"would_remove": ["acme-env", "acme-be-style"], "would_add": []}
    assert data["actions"]["push"]["guards"] == ["commit"]
    assert data["subagents"]["general-purpose"] == {"role": "Scout", "units": ["commit"], "guards": ["commit"]}


def test_markdown_and_report_name_the_discrepancies(layout, profile_settings, monkeypatch):
    monkeypatch.setattr(catalog, "_citing_files", lambda layout, name: [])
    _write(layout.roles_dir / "Coder" / "role.toml",
           'core = "Coder"\nlaunch = ["worker"]\nactions = ["build"]\nfragments = ["commit"]\n')
    data = catalog.build(layout, {})
    md = catalog.to_markdown(data)
    assert "| Coder |" in md and "commit" in md
    report = catalog.report(data)
    assert "Coder: manual but not computed: commit" in report
    assert "Coder: computed but not manual: code-style, evidence-map" in report
    assert "Coder on back" not in report
    assert "Manager on back: the filter would remove: acme-env, acme-be-style" in report


def test_baseline_guard_fails_without_a_ruling(layout, profile_settings, monkeypatch):
    monkeypatch.setattr(catalog, "_citing_files", lambda layout, name: [])
    data = catalog.build(layout, {})
    (layout.roles_dir / roles.CATALOG_BASELINE).write_text(json.dumps(catalog.baseline(data)))
    assert catalog.baseline_problems(layout, data) == []
    _write(layout.roles_dir / "Coder" / "role.toml", 'core = "Coder"\nlaunch = ["worker"]\nactions = ["build"]\nfragments = ["code-style"]\n')
    later = catalog.build(layout, {})
    assert catalog.baseline_problems(layout, later) == ["(j) role Coder lost guard 'commit' since the baseline with no [[drop]] in rulings.toml"]
    _write(layout.roles_dir / roles.RULINGS_FILE, '[[drop]]\nrole = "Coder"\nunit = "commit"\nby = "engineer"\ndate = "2026-10-08"\n')
    assert catalog.baseline_problems(layout, later) == []


def test_a_baseline_an_earlier_engine_wrote_from_empty_manual_lists_is_refused(layout, profile_settings, monkeypatch):
    monkeypatch.setattr(catalog, "_citing_files", lambda layout, name: [])
    monkeypatch.setattr(roles.config, "roles_computed", lambda: True)
    _write(layout.roles_dir / "Coder" / "role.toml", 'core = "Coder"\nlaunch = ["worker"]\nactions = ["build", "git-write"]\n')
    earlier = {"roles": {"Coder": {"manual": [], "guards_manual": [], "guards_computed": ["commit"]},
                         "Manager": {"manual": ["commit"], "guards_manual": ["commit"], "guards_computed": ["commit"]}}}
    (layout.roles_dir / roles.CATALOG_BASELINE).write_text(json.dumps(earlier))
    assert catalog.baseline_problems(layout, catalog.build(layout, {})) == [
        "(j) role Coder: roles/catalog.baseline.json holds no guard list for it, only an empty manual list; "
        "rewrite the baseline with `dockwright roles catalog --write-baseline`"]


def test_freshness_compares_composition_only(layout, profile_settings, monkeypatch):
    monkeypatch.setattr(catalog, "_citing_files", lambda layout, name: [])
    data = catalog.build(layout, {})
    assert catalog.freshness_problems(layout, data) == ["(k) roles/catalog.json missing; run `dockwright compose`"]
    catalog.write(layout, data)
    assert catalog.freshness_problems(layout, data) == []
    assert (layout.roles_dir / roles.CATALOG_MD).read_text().startswith("# Catalog")
    monkeypatch.setattr(catalog, "_citing_files", lambda layout, name: ["flows/new.md"])
    _write(layout.roles_dir / "_shared" / "fragments" / "git" / "commit.md", _unit("guard", ["git-write", "push"], "git", "COMMIT RULE, longer"))
    assert catalog.freshness_problems(layout, catalog.build(layout, {})) == []
    _write(layout.roles_dir / "_shared" / "fragments" / "git" / "commit.md", _unit("guard", ["push"], "git", "COMMIT RULE"))
    assert catalog.freshness_problems(layout, catalog.build(layout, {})) == ["(k) roles/catalog.json is stale; run `dockwright compose`"]


def test_freshness_reports_a_catalog_an_earlier_version_wrote_as_stale(layout, profile_settings, monkeypatch):
    monkeypatch.setattr(catalog, "_citing_files", lambda layout, name: [])
    data = catalog.build(layout, {})
    earlier = json.loads(json.dumps(data))
    for unit in earlier["units"].values():
        del unit["roles"]
    (layout.roles_dir / roles.CATALOG_FILE).write_text(json.dumps(earlier))
    assert catalog.freshness_problems(layout, data) == ["(k) roles/catalog.json is stale; run `dockwright compose`"]


def test_compose_roles_writes_the_catalog(layout, profile_settings, monkeypatch):
    monkeypatch.setattr(catalog, "_citing_files", lambda layout, name: [])
    roles.compose_roles(layout, {})
    assert (layout.roles_dir / roles.CATALOG_FILE).is_file() and (layout.roles_dir / roles.CATALOG_MD).is_file()


def test_check_roles_reports_a_lost_guard_and_a_stale_catalog(layout, profile_settings, monkeypatch):
    monkeypatch.setattr(catalog, "_citing_files", lambda layout, name: [])
    roles.compose_roles(layout, {})
    (layout.roles_dir / roles.CATALOG_BASELINE).write_text((layout.roles_dir / roles.CATALOG_FILE).read_text())
    assert roles.check_roles(layout, {}) == (True, [])
    _write(layout.roles_dir / "Coder" / "role.toml", 'core = "Coder"\nlaunch = ["worker"]\nactions = ["build"]\nfragments = ["code-style"]\n')
    ok, problems = roles.check_roles(layout, {})
    assert not ok
    assert any(p.startswith("(j) role Coder lost guard 'commit'") for p in problems)
    assert any(p.startswith("(k) roles/catalog.json is stale") for p in problems)


def test_catalog_cli_report_check_and_write(layout, profile_settings, monkeypatch, capsys):
    monkeypatch.setattr(catalog.config, "subagents", lambda: {})
    monkeypatch.setattr(catalog, "_citing_files", lambda layout, name: [])
    args = ["--roles-dir", str(layout.roles_dir), "--core-dir", str(layout.core_dirs[1]), "--claude-dir", str(layout.claude_dir)]
    assert catalog.main(["--report", *args]) == 0
    assert capsys.readouterr().out == ("Manager on common: the filter would remove: acme-env\n"
                                       "Manager on back: the filter would remove: acme-env, acme-be-style\n")
    assert catalog.main(["--check", *args]) == 1
    assert "catalog.json missing" in capsys.readouterr().out
    assert catalog.main(["--write-baseline", *args]) == 0
    assert (layout.roles_dir / roles.CATALOG_BASELINE).is_file() and (layout.roles_dir / roles.CATALOG_FILE).is_file()
    assert json.loads((layout.roles_dir / roles.CATALOG_BASELINE).read_text()) == {
        "roles": {"Coder": {"guards": ["commit"]}, "Manager": {"guards": ["commit"]}}, "subagents": {}}
    assert catalog.main(["--check", *args]) == 0


def test_write_baseline_refuses_a_drop_for_a_guard_a_render_holds_again(layout, profile_settings, monkeypatch, capsys):
    monkeypatch.setattr(catalog.config, "subagents", lambda: {"general-purpose": {"role": "Manager", "core": "manager"}})
    monkeypatch.setattr(catalog, "_citing_files", lambda layout, name: [])
    args = ["--roles-dir", str(layout.roles_dir), "--core-dir", str(layout.core_dirs[1]), "--claude-dir", str(layout.claude_dir)]
    drop = '[[drop]]\nrole = "{}"\nunit = "commit"\nby = "engineer"\ndate = "2026-10-16"\n'
    for holder in ("Coder", "general-purpose"):
        _write(layout.roles_dir / roles.RULINGS_FILE, drop.format(holder))
        assert catalog.main(["--write-baseline", *args]) == 1
        assert not (layout.roles_dir / roles.CATALOG_BASELINE).exists()
        assert capsys.readouterr().err == (
            f"catalog: rulings.toml drops guard 'commit' from {holder}, whose render holds it again; "
            "remove that [[drop]], then rerun --write-baseline\n")
    _write(layout.roles_dir / roles.RULINGS_FILE, drop.format("Integrator"))
    assert catalog.main(["--write-baseline", *args]) == 0
    assert (layout.roles_dir / roles.CATALOG_BASELINE).is_file()


def test_write_baseline_refuses_while_check_j_reports_a_lost_guard(layout, profile_settings, monkeypatch, capsys):
    monkeypatch.setattr(catalog.config, "subagents", lambda: {})
    monkeypatch.setattr(catalog, "_citing_files", lambda layout, name: [])
    monkeypatch.setattr(roles.config, "roles_computed", lambda: True)
    args = ["--roles-dir", str(layout.roles_dir), "--core-dir", str(layout.core_dirs[1]), "--claude-dir", str(layout.claude_dir)]
    floor = layout.roles_dir / roles.CATALOG_BASELINE
    floor.write_text(json.dumps({"roles": {"Coder": {"guards": ["commit"]}}, "subagents": {}}))
    _write(layout.roles_dir / "Coder" / "role.toml", 'core = "Coder"\nlaunch = ["worker"]\nactions = ["build"]\n')
    assert catalog.main(["--write-baseline", *args]) == 1
    assert json.loads(floor.read_text()) == {"roles": {"Coder": {"guards": ["commit"]}}, "subagents": {}}
    assert capsys.readouterr().err == (
        "catalog: (j) role Coder lost guard 'commit' since the baseline with no [[drop]] in rulings.toml; "
        "restore the guard or record the engineer's [[drop]], then rerun --write-baseline\n")
    _write(layout.roles_dir / roles.RULINGS_FILE, '[[drop]]\nrole = "Coder"\nunit = "commit"\nby = "engineer"\ndate = "2026-10-16"\n')
    assert catalog.main(["--write-baseline", *args]) == 0
    assert json.loads(floor.read_text())["roles"]["Coder"] == {"guards": []}
    floor.write_text(json.dumps({"roles": {"Coder": {"manual": [], "guards_manual": [], "guards_computed": ["commit"]}}}))
    (layout.roles_dir / roles.RULINGS_FILE).unlink()
    assert catalog.main(["--write-baseline", *args]) == 0


def test_baseline_gates_on_the_guards_a_computed_role_holds(layout, profile_settings, monkeypatch):
    monkeypatch.setattr(catalog, "_citing_files", lambda layout, name: [])
    monkeypatch.setattr(roles.config, "roles_computed", lambda: True)
    _write(layout.roles_dir / "Coder" / "role.toml", 'core = "Coder"\nlaunch = ["worker"]\nactions = ["build", "git-write"]\n')
    data = catalog.build(layout, {})
    assert data["roles"]["Coder"]["guards"] == ["commit"] and data["roles"]["Coder"]["guards_manual"] == []
    assert data["units"]["commit"]["roles"] == ["Coder", "Manager"]
    md = catalog.to_markdown(data)
    assert "| Coder | 3 | commit |" in md
    assert any(line.startswith("| commit | guard |") and line.endswith("| Coder, Manager | 0 |") for line in md.splitlines())
    (layout.roles_dir / roles.CATALOG_BASELINE).write_text(json.dumps(catalog.baseline(data)))
    _write(layout.roles_dir / "Coder" / "role.toml", 'core = "Coder"\nlaunch = ["worker"]\nactions = ["build"]\n')
    assert catalog.baseline_problems(layout, catalog.build(layout, {})) == [
        "(j) role Coder lost guard 'commit' since the baseline with no [[drop]] in rulings.toml"]


def test_a_hand_made_baseline_still_gates_on_guards_manual(layout, profile_settings, monkeypatch):
    monkeypatch.setattr(catalog, "_citing_files", lambda layout, name: [])
    monkeypatch.setattr(roles.config, "roles_computed", lambda: True)
    (layout.roles_dir / roles.CATALOG_BASELINE).write_text(json.dumps({"roles": {"Coder": {"guards_manual": ["commit"]}}}))
    _write(layout.roles_dir / "Coder" / "role.toml", 'core = "Coder"\nlaunch = ["worker"]\nactions = ["build"]\n')
    assert catalog.baseline_problems(layout, catalog.build(layout, {})) == [
        "(j) role Coder lost guard 'commit' since the baseline with no [[drop]] in rulings.toml"]
