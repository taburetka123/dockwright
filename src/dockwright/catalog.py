from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from pathlib import Path

from . import compose, config, roles


def _citing_files(layout: roles.Layout, name: str) -> list[str]:
    found: list[str] = []
    for label, root, specs in roles._citation_roots(layout):
        try:
            out = subprocess.run(["git", "-C", str(root), "grep", "-l", "-E", f"(^|[^a-z0-9-]){name}\\.md", "--", *specs],
                                 capture_output=True, text=True)
        except OSError:
            continue
        found += [f"{label}{rel}" for rel in out.stdout.split("\n") if rel]
    return sorted(found)


def _guards(names: list[str], units: dict[str, roles.Unit]) -> list[str]:
    return [n for n in names if n in units and units[n].kind == "guard"]


def _profile_filter(m: roles.Manifest, units: dict[str, roles.Unit], sets: dict) -> dict:
    from . import profiles as _profiles
    out: dict = {}
    for p in sets:
        if p == roles.NONE_PROFILE:
            continue
        picked = _profiles.profile_units(units, p, sets)
        today = [u.name for u in picked if u.name not in m.profile_excludes]
        filtered = [u.name for u in picked if roles._bound(u, m)]
        out[p] = {"would_remove": [n for n in today if n not in filtered], "would_add": [n for n in filtered if n not in today]}
    return out


def build(layout: roles.Layout, vars: dict[str, str]) -> dict:
    from . import profiles as _profiles
    manifests = roles.load_manifests(layout.roles_dir)
    units = roles.load_units(layout)
    sets = config.profiles()["sets"]
    manual = {name: [e.name for e in m.fragments] for name, m in manifests.items()}
    computed = {name: roles.compute_fragments(m, units) for name, m in manifests.items()}
    effective = {name: roles.effective_fragments(m, units) for name, m in manifests.items()}
    data: dict = {"units": {}, "roles": {}, "actions": {}, "subagents": {}}
    for name, unit in sorted(units.items()):
        data["units"][name] = {
            "kind": unit.kind, "binds": list(unit.binds), "domain": unit.domain, "cites": list(unit.cites),
            "home": unit.home, "bytes": len(unit.body.encode()),
            "manual_roles": sorted(r for r, names in manual.items() if name in names),
            "computed_roles": sorted(r for r, names in computed.items() if name in names),
            "roles": sorted(r for r, names in effective.items() if name in names),
            "cited_by": _citing_files(layout, name),
        }
    for name, m in sorted(manifests.items()):
        by_kind: dict[str, int] = {}
        for n in effective[name]:
            if n in units:
                by_kind[units[n].kind] = by_kind.get(units[n].kind, 0) + len(units[n].body.encode())
        data["roles"][name] = {
            "manual": manual[name], "computed": computed[name],
            "units": effective[name], "guards": _guards(effective[name], units),
            "guards_manual": _guards(manual[name], units), "guards_computed": _guards(computed[name], units),
            "manual_minus_computed": sorted(set(manual[name]) - set(computed[name])),
            "computed_minus_manual": sorted(set(computed[name]) - set(manual[name])),
            "bytes_by_kind": by_kind,
            "profile_units": {p: [u.name for u in _profiles.profile_units(units, p, sets)
                                  if u.name not in m.profile_excludes] for p in sets if p != roles.NONE_PROFILE},
            "profile_filter": _profile_filter(m, units, sets),
            "skills": list(m.skills),
        }
    verbs, _ = roles.vocabulary(layout.roles_dir)
    for verb in sorted(verbs):
        data["actions"][verb] = {"guards": sorted(u.name for u in units.values() if u.kind == "guard" and verb in u.binds)}
    for agent, spec in sorted(config.subagents().items()):
        role = spec.get("role")
        if role in manifests:
            names = roles.effective_fragments(manifests[role], units)
            data["subagents"][agent] = {"role": role, "units": names, "guards": _guards(names, units)}
    return data


COMPOSITION_KEYS = ("kind", "binds", "domain", "home", "manual_roles", "computed_roles", "roles")


def composition(data: dict) -> dict:
    return {"units": {n: {k: u.get(k) for k in COMPOSITION_KEYS} for n, u in data["units"].items()},
            "roles": {n: {k: v for k, v in r.items() if k != "bytes_by_kind"} for n, r in data["roles"].items()},
            "actions": data["actions"], "subagents": data["subagents"]}


def to_markdown(data: dict) -> str:
    out = ["# Catalog", "", "## Roles", "", "| Role | units | guards | manual − computed | computed − manual |", "|---|---|---|---|---|"]
    for name, role in data["roles"].items():
        out.append(f"| {name} | {len(role['units'])} | {', '.join(role['guards'])} | "
                   f"{', '.join(role['manual_minus_computed'])} | {', '.join(role['computed_minus_manual'])} |")
    out += ["", "## Units", "", "| Unit | kind | domain | home | bytes | roles | cited by |", "|---|---|---|---|---|---|---|"]
    for name, unit in data["units"].items():
        out.append(f"| {name} | {unit['kind']} | {unit['domain']} | {unit['home']} | {unit['bytes']} | "
                   f"{', '.join(unit['roles'])} | {len(unit['cited_by'])} |")
    out += ["", "## Actions", "", "| Action | guards |", "|---|---|"]
    out += [f"| {verb} | {', '.join(entry['guards'])} |" for verb, entry in data["actions"].items()]
    out += ["", "## Subagents", "", "| Type | role | units |", "|---|---|---|"]
    out += [f"| {agent} | {entry['role']} | {len(entry['units'])} |" for agent, entry in data["subagents"].items()]
    return "\n".join(out) + "\n"


def report(data: dict) -> str:
    lines: list[str] = []
    for name, role in data["roles"].items():
        if role["manual_minus_computed"]:
            lines.append(f"{name}: manual but not computed: {', '.join(role['manual_minus_computed'])}")
        if role["computed_minus_manual"]:
            lines.append(f"{name}: computed but not manual: {', '.join(role['computed_minus_manual'])}")
        for profile, delta in role.get("profile_filter", {}).items():
            if delta["would_remove"]:
                lines.append(f"{name} on {profile}: the filter would remove: {', '.join(delta['would_remove'])}")
            if delta["would_add"]:
                lines.append(f"{name} on {profile}: the filter would add: {', '.join(delta['would_add'])}")
    return "\n".join(lines) + ("\n" if lines else "")


def write(layout: roles.Layout, data: dict) -> None:
    (layout.roles_dir / roles.CATALOG_FILE).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (layout.roles_dir / roles.CATALOG_MD).write_text(to_markdown(data), encoding="utf-8")


def _rulings(layout: roles.Layout) -> set[tuple[str, str]]:
    path = layout.roles_dir / roles.RULINGS_FILE
    if not path.is_file():
        return set()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return {(d.get("role", ""), d.get("unit", "")) for d in data.get("drop", []) if isinstance(d, dict)}


def baseline(data: dict) -> dict:
    return {"roles": {name: {"guards": role["guards"]} for name, role in data["roles"].items()},
            "subagents": {agent: {"guards": entry["guards"]} for agent, entry in data["subagents"].items()}}


def _baseline_file(layout: roles.Layout) -> dict | None:
    path = layout.roles_dir / roles.CATALOG_BASELINE
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _empty_legacy_entry(entry: dict) -> bool:
    return "guards" not in entry and entry.get("manual") == []


def lost_guards(layout: roles.Layout, data: dict) -> list[str]:
    baseline = _baseline_file(layout)
    if baseline is None:
        return []
    rulings = _rulings(layout)
    problems: list[str] = []
    for name, entry in baseline.get("roles", {}).items():
        if _empty_legacy_entry(entry):
            continue
        now = set(data["roles"][name]["guards"]) if name in data["roles"] else set()
        for guard in sorted(set(entry["guards"] if "guards" in entry else entry["guards_manual"]) - now):
            if (name, guard) not in rulings:
                problems.append(f"(j) role {name} lost guard {guard!r} since the baseline with no [[drop]] in rulings.toml")
    for agent, entry in baseline.get("subagents", {}).items():
        now = set(data["subagents"].get(agent, {}).get("guards", []))
        for guard in sorted(set(entry["guards"]) - now):
            if (agent, guard) not in rulings:
                problems.append(f"(j) subagent {agent} lost guard {guard!r} since the baseline with no [[drop]] in rulings.toml")
    return problems


def baseline_problems(layout: roles.Layout, data: dict) -> list[str]:
    baseline = _baseline_file(layout)
    if baseline is None:
        return []
    legacy = [f"(j) role {name}: roles/{roles.CATALOG_BASELINE} holds no guard list for it, only an empty manual list; "
              "rewrite the baseline with `dockwright roles catalog --write-baseline`"
              for name, entry in baseline.get("roles", {}).items() if _empty_legacy_entry(entry)]
    return legacy + lost_guards(layout, data)


def freshness_problems(layout: roles.Layout, data: dict) -> list[str]:
    path = layout.roles_dir / roles.CATALOG_FILE
    if not path.is_file():
        return [f"(k) roles/{roles.CATALOG_FILE} missing; run `dockwright compose`"]
    if composition(json.loads(path.read_text(encoding="utf-8"))) != composition(data):
        return [f"(k) roles/{roles.CATALOG_FILE} is stale; run `dockwright compose`"]
    return []


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="dockwright roles catalog")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--roles-dir", type=Path, default=None)
    parser.add_argument("--core-dir", type=Path, default=None)
    parser.add_argument("--claude-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    core_dir = args.core_dir or compose._default_core_dir()
    roles_dir = args.roles_dir or config.roles_dir()
    layout = roles.Layout(roles_dir, roles.core_dirs_for(core_dir),
                          args.claude_dir or (roles_dir.parent if args.roles_dir else config.claude_config_home()))
    vars = {**compose.load_default_vars(core_dir), **config.agent_vars()}
    try:
        data = build(layout, vars)
    except roles.RoleError as err:
        print(f"catalog: {err}", file=sys.stderr)
        return 2
    if args.report:
        sys.stdout.write(report(data) or "no discrepancies\n")
        return 0
    if args.check:
        problems = freshness_problems(layout, data) + baseline_problems(layout, data)
        for p in problems:
            print(p)
        return 1 if problems else 0
    if args.write_baseline:
        floor = baseline(data)
        held = {(name, guard) for half in floor.values() for name, entry in half.items() for guard in entry["guards"]}
        stale = sorted(held & _rulings(layout))
        lost = lost_guards(layout, data)
        for name, guard in stale:
            print(f"catalog: rulings.toml drops guard {guard!r} from {name}, whose render holds it again; "
                  "remove that [[drop]], then rerun --write-baseline", file=sys.stderr)
        for line in lost:
            print(f"catalog: {line}; restore the guard or record the engineer's [[drop]], then rerun --write-baseline",
                  file=sys.stderr)
        if stale or lost:
            return 1
        (layout.roles_dir / roles.CATALOG_BASELINE).write_text(json.dumps(floor, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write(layout, data)
    print(f"wrote roles/{roles.CATALOG_FILE}, roles/{roles.CATALOG_MD}"
          + (f", roles/{roles.CATALOG_BASELINE}" if args.write_baseline else ""))
    return 0
