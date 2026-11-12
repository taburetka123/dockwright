from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import compose, config, paths, pipeline

DEFAULT_ROLE = "GenericWorker"
SHARED = "_shared"
MANIFEST = "role.toml"
SETTINGS = "role.settings.json"
FORMER_RULES = "former-rules.txt"
LAUNCHES = ("worker", "child", "manager", "lane", "interactive", "agent")
CODEX = "codex"
PROTOCOL = {"worker": "worker-protocol", "child": "child-protocol"}
NONE_PROFILE = "none"
AGENTS_DIR = "_agents"
PROFILES_DIR = "_profiles"
ACTIONS_FILE = "actions.toml"
RULINGS_FILE = "rulings.toml"
CATALOG_FILE = "catalog.json"
CATALOG_MD = "catalog.md"
CATALOG_BASELINE = "catalog.baseline.json"
TRIPWIRE_RULE = "no-role.md"
KINDS = ("guard", "fact", "ruling", "pointer")
ALL_SKILLS = "*"
_KEYS = {"core": str, "extends": str, "tier": str, "fragments": list, "skills": list, "launch": list,
         "settings": dict, "actions": list, "reachable": list, "profiles": list, "profile_excludes": list}
IGNORED_TIER: set[Path] = set()
_RULE_CITE = re.compile(r"rules/([a-z0-9-]+)\.md")
_FLAT_SHARED_CITE = re.compile(r"roles/_shared/fragments/([a-z0-9-]+)\.md")
_TEXT_SUFFIXES = {".md", ".toml", ".json", ".sh", ".py", ".txt", ""}


class RoleError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    name: str
    owner: str


@dataclass(frozen=True)
class Manifest:
    name: str
    core: str
    fragments: tuple[Entry, ...]
    skills: tuple[str, ...]
    launch: tuple[str, ...]
    settings: dict
    chain: tuple[str, ...]
    actions: tuple[str, ...] = ()
    reachable: tuple[str, ...] = ()
    profiles: tuple[str, ...] | None = None
    profile_excludes: tuple[str, ...] = ()
    fragments_declared: bool = True


@dataclass(frozen=True)
class Unit:
    name: str
    path: Path
    home: str
    kind: str
    binds: tuple[str, ...]
    domain: str
    cites: tuple[str, ...]
    body: str


@dataclass(frozen=True)
class Layout:
    roles_dir: Path
    core_dirs: tuple[Path, ...]
    claude_dir: Path

    @property
    def canon_fragments(self) -> Path:
        return self.core_dirs[0] / "fragments"


def mark(role: str, launch: str) -> str:
    return f"<!-- dockwright-role: {role} launch={launch} -->"


def profile_mark(profile: str) -> str:
    return f"<!-- dockwright-profile: {profile} -->"


def core_dirs_for(core_dir: Path, roles_core_dir: Path | None = None) -> tuple[Path, Path, Path]:
    core_dir = Path(core_dir)
    canon = Path(roles_core_dir) if roles_core_dir is not None else core_dir.parent / "roles"
    return (canon, core_dir, canon / "subagents")


def _split_list(value: str) -> list[str]:
    inner = value.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    return [v.strip().strip("\"'") for v in inner.split(",") if v.strip()]


def parse_unit(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    meta: dict = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        meta[key] = _split_list(value) if key in ("binds", "cites") or value.startswith("[") else value.strip("\"'")
    return meta, text[end + 5:]


def vocabulary(roles_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    path = Path(roles_dir) / ACTIONS_FILE
    if not path.is_file():
        return {}, {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        raise RoleError(f"{path}: {err}") from err
    actions = {k: v for k, v in data.get("actions", {}).items() if isinstance(v, str)}
    domains = {k: v for k, v in data.get("domains", {}).items() if isinstance(v, str)}
    return actions, domains


def _root(roles_dir: Path | None) -> Path:
    return Path(roles_dir) if roles_dir is not None else paths.ROLES_DIR


def default_layout(core_dir: Path | None = None) -> Layout:
    agents = Path(core_dir) if core_dir is not None else compose._default_core_dir()
    return Layout(paths.ROLES_DIR, core_dirs_for(agents), paths.CONFIG_HOME)


def role_names(roles_dir: Path | None = None) -> list[str]:
    root = _root(roles_dir)
    return sorted(p.parent.name for p in root.glob(f"*/{MANIFEST}") if p.parent.name != SHARED)


def _read_manifest(path: Path) -> dict:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        raise RoleError(f"{path}: {err}") from err
    unknown = sorted(set(data) - set(_KEYS))
    if unknown:
        raise RoleError(f"{path}: unknown key(s) {unknown}; known: {sorted(_KEYS)}")
    for key, kind in _KEYS.items():
        if key in data and not isinstance(data[key], kind):
            raise RoleError(f"{path}: `{key}` must be a {kind.__name__}")
    for key in ("fragments", "skills", "launch"):
        if not all(isinstance(v, str) and v.strip() for v in data.get(key, [])):
            raise RoleError(f"{path}: `{key}` must list non-empty strings")
    bad = [v for v in data.get("launch", []) if v not in LAUNCHES]
    if bad:
        raise RoleError(f"{path}: launch {bad} is not in {list(LAUNCHES)}")
    for key in ("actions", "reachable", "profiles", "profile_excludes"):
        if not all(isinstance(v, str) and v.strip() for v in data.get(key, [])):
            raise RoleError(f"{path}: `{key}` must list non-empty strings")
    actions, _ = vocabulary(path.parent.parent)
    if actions:
        bad = [v for key in ("actions", "reachable") for v in data.get(key, []) if v not in actions]
        if bad:
            raise RoleError(f"{path}: {bad} is not in the actions vocabulary ({ACTIONS_FILE})")
    sets = config.profiles()["sets"]
    bad = [p for p in data.get("profiles", []) if p != NONE_PROFILE and p not in sets]
    if bad:
        raise RoleError(f"{path}: {bad} is not a profile ([profiles.sets] in dockwright.toml)")
    problems = pipeline.profile_problems(data.get("settings", {}), f"{path} [settings]",
                                         keys=pipeline.ROLE_SETTINGS_KEYS)
    if problems:
        raise RoleError("; ".join(problems))
    if "tier" in data:
        data.pop("tier")
        IGNORED_TIER.add(path)
    return data


def _resolve(name: str, raw: dict[str, dict], seen: tuple[str, ...]) -> Manifest:
    if name in seen:
        raise RoleError(f"role {seen[0]}: extends cycle {' -> '.join((*seen, name))}")
    if name not in raw:
        raise RoleError(f"role {seen[-1] if seen else name}: {name!r} has no {MANIFEST}")
    data = raw[name]
    own = tuple(Entry(f, name) for f in data.get("fragments", []))
    if "extends" not in data:
        if "core" not in data:
            raise RoleError(f"role {name}: `core` is required on a role that extends nothing")
        return Manifest(name, data["core"], own, tuple(data.get("skills", [])),
                        tuple(data.get("launch", [])), dict(data.get("settings", {})), (name,),
                        actions=tuple(data.get("actions", [])), reachable=tuple(data.get("reachable", [])),
                        profiles=tuple(data["profiles"]) if "profiles" in data else None,
                        profile_excludes=tuple(data.get("profile_excludes", [])),
                        fragments_declared="fragments" in data)
    base = _resolve(data["extends"], raw, (*seen, name))
    return Manifest(
        name,
        data.get("core", base.core),
        base.fragments + own,
        tuple(dict.fromkeys((*base.skills, *data.get("skills", [])))),
        tuple(data.get("launch", base.launch)),
        pipeline.merge_settings(base.settings, dict(data.get("settings", {}))),
        (*base.chain, name),
        actions=tuple(dict.fromkeys((*base.actions, *data.get("actions", [])))),
        reachable=tuple(dict.fromkeys((*base.reachable, *data.get("reachable", [])))),
        profiles=tuple(data["profiles"]) if "profiles" in data else base.profiles,
        profile_excludes=tuple(dict.fromkeys((*base.profile_excludes, *data.get("profile_excludes", [])))),
        fragments_declared=base.fragments_declared or "fragments" in data,
    )


def load_manifests(roles_dir: Path | None = None) -> dict[str, Manifest]:
    root = _root(roles_dir)
    raw = {name: _read_manifest(root / name / MANIFEST) for name in role_names(root)}
    return {name: _resolve(name, raw, ()) for name in raw}


def load_role(name: str, roles_dir: Path | None = None) -> Manifest:
    root = _root(roles_dir)
    raw: dict[str, dict] = {}
    current: str | None = name
    while current is not None and current not in raw and (root / current / MANIFEST).is_file():
        raw[current] = _read_manifest(root / current / MANIFEST)
        current = raw[current].get("extends")
    return _resolve(name, raw, ())


def _places(layout: Layout, entry: Entry) -> list[Path]:
    found = sorted((layout.roles_dir / entry.owner / "fragments").rglob(f"{entry.name}.md"))
    found += sorted((layout.roles_dir / SHARED / "fragments").rglob(f"{entry.name}.md"))
    canon = layout.canon_fragments / f"{entry.name}.md"
    if canon.is_file():
        found.append(canon)
    return [p for p in found if p.is_file()]


def locate(layout: Layout, entry: Entry) -> Path:
    found = _places(layout, entry)
    if len(found) != 1:
        where = ", ".join(str(p) for p in found) or (
            f"roles/{entry.owner}/fragments, roles/{SHARED}/fragments, the canonical fragments")
        raise RoleError(f"(b) role {entry.owner}: fragment {entry.name!r} is in {len(found)} places: {where}")
    return found[0]


def core_file(layout: Layout, core: str) -> Path:
    found = [d / f"{core}.core.md" for d in layout.core_dirs if (d / f"{core}.core.md").is_file()]
    if len(found) != 1:
        raise RoleError(f"(b) core {core!r}: {len(found)} files named {core}.core.md in "
                        + ", ".join(str(d) for d in layout.core_dirs))
    return found[0]


def _body(text: str) -> str:
    return parse_unit(text)[1]


def _unit_from(path: Path, home: str) -> Unit:
    meta, body = parse_unit(path.read_text(encoding="utf-8"))
    binds = tuple(meta.get("binds", [])) if isinstance(meta.get("binds"), list) else ()
    cites = tuple(meta.get("cites", [])) if isinstance(meta.get("cites"), list) else ()
    return Unit(path.stem, path, home, str(meta.get("kind", "")), binds, str(meta.get("domain", "")), cites, body)


def _unit_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.md") if p.is_file()) if root.is_dir() else []


def unit_domain(path: Path) -> str:
    meta, _ = parse_unit(path.read_text(encoding="utf-8"))
    return str(meta.get("domain", ""))


def load_units(layout: Layout, project_units_dir: Path | None = None) -> dict[str, Unit]:
    units: dict[str, Unit] = {}

    def add(path: Path, home: str) -> None:
        unit = _unit_from(path, home)
        if unit.name in units:
            raise RoleError(f"(b) unit {unit.name!r} is in 2 places: {units[unit.name].path}, {path}")
        units[unit.name] = unit

    for path in _unit_files(layout.roles_dir / SHARED / "fragments"):
        add(path, "shared")
    for name in role_names(layout.roles_dir):
        for path in _unit_files(layout.roles_dir / name / "fragments"):
            add(path, f"role:{name}")
    for path in _unit_files(layout.canon_fragments):
        if path.stem not in PROTOCOL.values():
            add(path, "canon")
    project = project_units_dir if project_units_dir is not None else config.project_units_dir()
    if project is not None:
        if not Path(project).is_dir():
            raise RoleError(f"(g) [paths] project_units_dir {project} is not a directory; "
                            "every profile render would carry no project unit")
        for path in _unit_files(Path(project)):
            add(path, f"profile:{unit_domain(path)}")
    for name in role_names(layout.roles_dir):
        for path in sorted((layout.roles_dir / name / "overlay").glob("*.md")):
            units[f"overlay:{name}/{path.stem}"] = _unit_from(path, f"overlay:{name}")
    return units


def _bound(unit: Unit, manifest: Manifest) -> bool:
    actions = set(manifest.actions)
    reach = actions | set(manifest.reachable) if unit.kind == "guard" else actions
    return bool(set(unit.binds) & reach)


def compute_fragments(manifest: Manifest, units: dict[str, Unit]) -> list[str]:
    owners = {f"role:{name}" for name in manifest.chain}
    picked = [u.name for u in units.values()
              if (u.home in owners) or (u.home in ("shared", "canon") and _bound(u, manifest))]
    return sorted(picked)


def effective_fragments(manifest: Manifest, units: dict[str, Unit]) -> list[str]:
    if manifest.fragments_declared and not config.roles_computed():
        return [e.name for e in manifest.fragments]
    return compute_fragments(manifest, units)


def _own_dropins(layout: Layout, manifest: Manifest) -> list[compose.DropIn]:
    return [d for name in manifest.chain for d in compose.load_dropins(layout.roles_dir / name, "overlay")]


def _profiles_of(manifest: Manifest) -> list[str]:
    if manifest.profiles is not None:
        return [NONE_PROFILE, *[p for p in manifest.profiles if p != NONE_PROFILE]]
    return [NONE_PROFILE, *[p for p in config.profiles()["sets"] if p != NONE_PROFILE]]


def _role_units(layout: Layout, manifest: Manifest, units: dict[str, Unit]) -> list[Path]:
    if manifest.fragments_declared and not config.roles_computed():
        return [locate(layout, entry) for entry in manifest.fragments]
    return [units[name].path for name in compute_fragments(manifest, units)]


def _profile_bodies(manifest: Manifest, units: dict[str, Unit], profile: str) -> list[str]:
    from . import profiles as _profiles
    if profile == NONE_PROFILE:
        return []
    picked = _profiles.profile_units(units, profile, config.profiles()["sets"])
    if config.roles_computed():
        picked = [u for u in picked if _bound(u, manifest)]
    else:
        picked = [u for u in picked if u.name not in manifest.profile_excludes]
    return [u.body for u in picked]


def render(layout: Layout, manifest: Manifest, launch: str, vars: dict[str, str],
           profile: str = NONE_PROFILE, units: dict[str, Unit] | None = None) -> tuple[str, list[str], list[Path]]:
    units = load_units(layout) if units is None else units
    core = _body(core_file(layout, manifest.core).read_text(encoding="utf-8"))
    parts = [compose.expand_includes(core, layout.canon_fragments)]
    seen = {layout.canon_fragments / f"{m.group(1)}.md" for m in map(compose.INCLUDE_RE.match, core.splitlines()) if m}
    for path in _role_units(layout, manifest, units):
        if path not in seen:
            seen.add(path)
            parts.append(_body(path.read_text(encoding="utf-8")))
    parts += _profile_bodies(manifest, units, profile)
    if launch in PROTOCOL:
        proto = layout.canon_fragments / f"{PROTOCOL[launch]}.md"
        if not proto.is_file():
            raise RoleError(f"role {manifest.name}: protocol fragment {proto} missing")
        if proto not in seen:
            parts.append(_body(proto.read_text(encoding="utf-8")))
    text = "\n\n".join(p.strip("\n") for p in parts if p.strip()) + "\n"
    markers = compose.marker_names(text)
    shared = compose.load_dropins(layout.roles_dir / SHARED, "overlay")
    dropins = ([d for d in _own_dropins(layout, manifest) if compose.lands(d, markers, appends=True)]
               + [d for d in shared if compose.lands(d, markers, appends=False)])
    composed, warnings = compose.compose_text(text, dropins, vars)
    literal = [line for line in composed.splitlines() if compose.INCLUDE_LIKE_RE.match(line)]
    if literal:
        raise RoleError(f"role {manifest.name} launch={launch}: {literal} left literal in the render; "
                        "an include line expands only in a core")
    head = mark(manifest.name, launch) + ("" if profile == NONE_PROFILE else "\n" + profile_mark(profile))
    return f"{head}\n\n{composed}", warnings, [d.path for d in dropins]


def render_name(launch: str, profile: str) -> str:
    return f"role.{launch}.md" if profile == NONE_PROFILE else f"role.{launch}.{profile}.md"


def skill_entries(claude_dir: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    skills = claude_dir / "skills"
    for path in sorted(skills.glob("*/SKILL.md")) + sorted((skills / "synced").glob("*/SKILL.md")):
        found.setdefault(path.parent.name, []).append(str(path.relative_to(claude_dir)))
    for path in sorted((claude_dir / "commands").glob("*.md")):
        found.setdefault(path.stem, []).append(str(path.relative_to(claude_dir)))
    return found


def role_settings(layout: Layout, manifest: Manifest) -> dict:
    listed = set(manifest.skills)
    if ALL_SKILLS in listed:
        return pipeline.merge_settings({}, manifest.settings)
    overrides = {n: "name-only" for n in sorted(skill_entries(layout.claude_dir)) if n not in listed}
    return pipeline.merge_settings({"skillOverrides": overrides} if overrides else {}, manifest.settings)


def _launches(manifest: Manifest) -> list[str]:
    launches = list(manifest.launch)
    if {"worker", "child"} & set(launches):
        launches.append(CODEX)
    return launches


def outputs(layout: Layout, manifest: Manifest, vars: dict[str, str],
            units: dict[str, Unit] | None = None) -> tuple[dict[Path, str], list[str], set[Path]]:
    units = load_units(layout) if units is None else units
    role_dir = layout.roles_dir / manifest.name
    files: dict[Path, str] = {}
    warnings: list[str] = []
    applied: set[Path] = set()
    for launch in _launches(manifest):
        for profile in _profiles_of(manifest):
            text, warns, used = render(layout, manifest, launch, vars, profile, units)
            files[role_dir / render_name(launch, profile)] = text
            warnings += [f"{manifest.name}/{launch}/{profile}: {w}" for w in warns]
            applied.update(used)
    files[role_dir / SETTINGS] = json.dumps(role_settings(layout, manifest), indent=2, sort_keys=True) + "\n"
    return files, warnings, applied


def _is_render(path: Path, role: str) -> bool:
    try:
        first = path.read_text(encoding="utf-8").split("\n", 1)[0]
    except (OSError, UnicodeDecodeError):
        return False
    launch = path.name[len("role."):-len(".md")].split(".")[0]
    return first == mark(role, launch)


def landing_problems(layout: Layout, manifests: dict[str, Manifest],
                     applied: dict[str, set[Path]]) -> list[str]:
    problems: list[str] = []
    for name, used in applied.items():
        for d in _own_dropins(layout, manifests[name]):
            if d.insert_at is not None and d.path not in used:
                problems.append(f"(d) role {name}: overlay/{d.path.name} targets marker {d.insert_at!r}, "
                                "which no render of the role carries")
    landed = set().union(*applied.values())
    legacy = {role for sources in compose.LEGACY_ROLE_OVERLAYS.values() for role in sources}
    for path in sorted(layout.roles_dir.glob("*/overlay/*.md")):
        owner = path.parent.parent.name
        if owner in manifests or path in landed:
            continue
        if owner == SHARED and manifests:
            problems.append(f"(d) roles/{path.relative_to(layout.roles_dir)} lands in no role render")
        elif owner not in legacy:
            problems.append(f"(d) roles/{path.relative_to(layout.roles_dir)}: roles/{owner} has no {MANIFEST}, "
                            "so no render reads it")
    return problems


@dataclass(frozen=True)
class Plan:
    manifests: dict[str, Manifest]
    files: dict[Path, str]
    warnings: list[str]


def _core_meta(layout: Layout, core: str) -> dict:
    meta, _ = parse_unit(core_file(layout, core).read_text(encoding="utf-8"))
    return meta


def agent_definitions(layout: Layout, manifests: dict[str, Manifest], files: dict[Path, str],
                      profile: str) -> dict:
    out: dict = {}
    for name, spec in config.subagents().items():
        role, core = spec.get("role"), spec.get("core")
        if role not in manifests or not isinstance(core, str):
            raise RoleError(f"[subagents] {name}: role {role!r} has no manifest or `core` is missing")
        if "agent" not in manifests[role].launch:
            raise RoleError(f"[subagents] {name}: role {role} does not declare launch 'agent'")
        meta = _core_meta(layout, core)
        prompt = files.get(layout.roles_dir / role / render_name("agent", profile))
        if prompt is None:
            raise RoleError(f"[subagents] {name}: role {role} renders no agent file for profile {profile}")
        entry = {"description": str(meta.get("description", "")), "prompt": prompt}
        if meta.get("tools"):
            entry["tools"] = _split_list(meta["tools"]) if isinstance(meta["tools"], str) else list(meta["tools"])
        if meta.get("model"):
            entry["model"] = str(meta["model"])
        out[name] = entry
    return out


def _bundle_texts(units: dict[str, Unit], sets: dict[str, list[str]]) -> dict[str, str]:
    from . import profiles as _profiles
    return {profile: _profiles.profile_text(profile, _profiles.profile_units(units, profile, sets))
            for profile in sets if profile != NONE_PROFILE}


def plan_roles(layout: Layout, vars: dict[str, str]) -> Plan:
    manifests = load_manifests(layout.roles_dir)
    units = load_units(layout)
    files: dict[Path, str] = {}
    warnings: list[str] = []
    applied: dict[str, set[Path]] = {}
    for manifest in manifests.values():
        out, warns, applied[manifest.name] = outputs(layout, manifest, vars, units)
        files.update(out)
        warnings += warns
    sets = config.profiles()["sets"]
    for profile, text in _bundle_texts(units, sets).items():
        files[layout.roles_dir / PROFILES_DIR / f"{profile}.md"] = text
    if config.subagents() and manifests:
        for profile in [NONE_PROFILE, *[p for p in sets if p != NONE_PROFILE]]:
            data = agent_definitions(layout, manifests, files, profile)
            files[layout.roles_dir / AGENTS_DIR / f"{profile}.json"] = json.dumps(data, indent=2, sort_keys=True) + "\n"
    problems = landing_problems(layout, manifests, applied)
    if problems:
        raise RoleError("; ".join(problems))
    return Plan(manifests, files, warnings)


def orphan_renders(layout: Layout, manifests: dict[str, Manifest]) -> list[Path]:
    return [path for path in sorted(layout.roles_dir.glob("*/role.*.md"))
            if path.parent.name not in manifests and _is_render(path, path.parent.name)]


def compose_roles(layout: Layout, vars: dict[str, str], plan: Plan | None = None) -> dict:
    plan = plan or plan_roles(layout, vars)
    for name in plan.manifests:
        for stale in sorted((layout.roles_dir / name).glob("role.*.md")):
            if stale not in plan.files and _is_render(stale, name):
                stale.unlink()
    for orphan in orphan_renders(layout, plan.manifests):
        orphan.unlink()
    for bundle in [*(layout.roles_dir / PROFILES_DIR).glob("*.md"), *(layout.roles_dir / AGENTS_DIR).glob("*.json")]:
        if bundle not in plan.files:
            bundle.unlink()
    written: list[str] = []
    for path, text in plan.files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(str(path.relative_to(layout.roles_dir)))
    if plan.manifests:
        from . import catalog
        catalog.write(layout, catalog.build(layout, vars))
    return {"files": written, "warnings": plan.warnings}


def _names(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def _deployed_copy(layout: Layout, rel: str, generated: set[str]) -> bool:
    if rel in generated:
        return True
    canon = layout.core_dirs[0].parent
    if rel.startswith(("skills/", "commands/")) and (canon / rel).exists():
        return True
    try:
        head = (layout.claude_dir / rel).read_text(encoding="utf-8").splitlines()[:2]
    except (OSError, UnicodeDecodeError):
        return False
    return any("deployed-from:" in line for line in head)


def _tracked(root: Path, pathspecs: tuple[str, ...] = ()) -> list[str] | str:
    try:
        inside = subprocess.run(["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
                                capture_output=True, text=True)
        if inside.returncode != 0:
            return []
        listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", *pathspecs],
                                capture_output=True, text=True)
    except OSError as err:
        return f"(e) git unavailable in {root}: {err}"
    if listed.returncode != 0:
        return f"(e) git ls-files failed in {root}: {listed.stderr.strip()}"
    return list(filter(None, listed.stdout.split("\0")))


def _citation_roots(layout: Layout) -> list[tuple[str, Path, tuple[str, ...]]]:
    roots = [("", layout.claude_dir, ())]
    canon = config.dockwright_repo()
    if canon:
        roots.append(("canon:", Path(canon), ("deploy", "src")))
    project = config.project_units_dir()
    if project is not None:
        roots.append(("project:", Path(project).parent, ()))
    return roots


def stale_citations(layout: Layout) -> list[str]:
    former = layout.roles_dir / FORMER_RULES
    moved_rules = {n for n in (_names(former) if former.is_file() else [])
                   if not (layout.claude_dir / "rules" / f"{n}.md").is_file()}
    shared = layout.roles_dir / SHARED / "fragments"
    flat_gone = {p.stem for p in shared.rglob("*.md") if p.parent != shared and not (shared / p.name).is_file()}
    if not moved_rules and not flat_gone:
        return []
    stamp = layout.claude_dir / "agents" / compose.STAMP_NAME
    try:
        generated = {f"agents/{n}" for n in json.loads(stamp.read_text()).get("outputs", {})}
    except (OSError, ValueError):
        generated = set()
    skip = f"{layout.roles_dir.name}/{FORMER_RULES}"
    problems: list[str] = []
    for label, root, specs in _citation_roots(layout):
        listed = _tracked(root, specs)
        if isinstance(listed, str):
            problems.append(listed)
            continue
        for rel in listed:
            if Path(rel).suffix not in _TEXT_SUFFIXES:
                continue
            if label == "" and (rel == skip or _deployed_copy(layout, rel, generated)):
                continue
            try:
                text = (root / rel).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                for m in _RULE_CITE.finditer(line):
                    if m.group(1) in moved_rules:
                        problems.append(f"(e) {label}{rel}:{number} cites rules/{m.group(1)}.md, which moved out of rules/")
                for m in _FLAT_SHARED_CITE.finditer(line):
                    if m.group(1) in flat_gone:
                        problems.append(f"(e) {label}{rel}:{number} cites roles/_shared/fragments/{m.group(1)}.md, "
                                        "which now lives in a domain folder")
    return problems


SIZE_TARGETS = {"GenericWorker": 61440, "Manager": 76800, "Engineer": 51200}
CHILD_TARGET = 25600
_WARNING_PREFIXES = ("(i) ", "(m) ")


def _unit_rel(layout: Layout, unit: Unit) -> str:
    where = unit.path
    if unit.home.startswith("profile:"):
        return str(where)
    return str(where.relative_to(layout.roles_dir)) if layout.roles_dir in where.parents else str(where)


def _unit_problems(layout: Layout, units: dict[str, Unit]) -> list[str]:
    problems: list[str] = []
    actions, domains = vocabulary(layout.roles_dir)
    strict = config.roles_strict()
    for unit in units.values():
        rel = _unit_rel(layout, unit)
        if not unit.kind:
            if strict:
                problems.append(f"(g) {rel}: no frontmatter (kind, binds, domain)")
            continue
        if unit.kind not in KINDS:
            problems.append(f"(g) {rel}: kind {unit.kind!r} is not one of {list(KINDS)}")
        if actions:
            bad = [b for b in (*unit.binds, *unit.cites) if b not in actions]
            if bad:
                problems.append(f"(g) {rel}: {bad} is not in the actions vocabulary")
        if domains and unit.domain not in domains:
            problems.append(f"(g) {rel}: domain {unit.domain!r} is not in the domains vocabulary")
        if unit.home == "shared" and unit.path.parent.name != unit.domain:
            problems.append(f"(g) {rel}: folder {unit.path.parent.name!r} differs from domain {unit.domain!r}")
    rules_dir = layout.claude_dir / "rules"
    if (rules_dir / TRIPWIRE_RULE).is_file():
        for extra in sorted(rules_dir.glob("*.md")):
            if extra.name != TRIPWIRE_RULE:
                problems.append(f"(h) rules/{extra.name}: rules/ holds only {TRIPWIRE_RULE} once the core has moved")
    carried = {unit.domain for unit in units.values() if unit.home.startswith("profile:")}
    for profile, domains in config.profiles()["sets"].items():
        for domain in domains:
            if domain not in carried:
                problems.append(f"(n) profile {profile} names domain {domain!r}, which no unit under "
                                "[paths] project_units_dir carries")
    pairs: dict[tuple[str, str], list[str]] = {}
    for unit in units.values():
        if unit.kind == "guard" and not unit.cites:
            for bind in unit.binds:
                pairs.setdefault((bind, unit.domain), []).append(unit.name)
    for (bind, domain), names in sorted(pairs.items()):
        if len(names) > 1:
            problems.append(f"(i) guards {sorted(names)} share bind {bind!r} in domain {domain!r} with no `cites`")
    return problems


def _flow_problems(layout: Layout) -> list[str]:
    problems: list[str] = []
    flows = layout.claude_dir / "flows"
    for flow in sorted(flows.glob("*.md")) if flows.is_dir() else []:
        lines = flow.read_text(encoding="utf-8").splitlines()
        heads = [i for i, line in enumerate(lines) if line.startswith("## ")]
        for k, start in enumerate(heads):
            end = heads[k + 1] if k + 1 < len(heads) else len(lines)
            read_by = [line for line in lines[start:end] if line.startswith("Read by:")]
            if not read_by:
                problems.append(f"(l) flows/{flow.name} § {lines[start][3:].strip()}: no `Read by:` line")
                continue
            for target in re.findall(r"`([^`]+)`", read_by[0]):
                if "/" not in target or " " in target:
                    continue
                if not (layout.claude_dir / target.rstrip("/")).exists():
                    problems.append(f"(l) flows/{flow.name} § {lines[start][3:].strip()}: `Read by:` names {target}, "
                                    "which does not exist")
    return problems


def _size_target(manifest: Manifest, launch: str) -> int | None:
    if launch == "child":
        return CHILD_TARGET
    return SIZE_TARGETS.get(manifest.name)


def _is_warning(problem: str) -> bool:
    return problem.startswith(_WARNING_PREFIXES) or problem.endswith("no `Read by:` line")


def check_roles(layout: Layout, vars: dict[str, str]) -> tuple[bool, list[str]]:
    try:
        manifests = load_manifests(layout.roles_dir)
    except RoleError as err:
        return False, [str(err)]
    problems: list[str] = []
    try:
        units = load_units(layout)
    except RoleError as err:
        problems.append(str(err))
        units = {}
    named: set[Path] = set()
    entries = skill_entries(layout.claude_dir)
    for m in manifests.values():
        try:
            core_file(layout, m.core)
        except RoleError as err:
            problems.append(str(err))
        try:
            named.update(_role_units(layout, m, units))
        except RoleError as err:
            problems.append(str(err))
        for skill in m.skills:
            if skill != ALL_SKILLS and skill not in entries:
                problems.append(f"(b) role {m.name}: skill {skill!r} is in no place "
                                "(skills/, skills/synced/, commands/)")
    former = layout.roles_dir / FORMER_RULES
    if former.is_file():
        listed = {p.stem for p in named}
        for name in _names(former):
            if not (layout.claude_dir / "rules" / f"{name}.md").is_file() and name not in listed:
                problems.append(f"(a) former rule {name} is neither in rules/ nor named by a manifest")
    for path in sorted(layout.roles_dir.glob("*/fragments/**/*.md")):
        if path not in named:
            problems.append(f"(c) {path.relative_to(layout.roles_dir)} is named by no manifest")
    if units:
        problems += _unit_problems(layout, units)
    ceilings = config.agent_ceilings()
    sets = config.profiles()["sets"]
    applied: dict[str, set[Path]] = {}
    for m in manifests.values():
        try:
            files, warns, applied[m.name] = outputs(layout, m, vars, units)
        except (RoleError, compose.ComposeError, OSError) as err:
            problems.append(f"(d) role {m.name}: cannot render: {err}")
            continue
        problems += [f"(d) role {w}" for w in warns]
        for path, text in files.items():
            key = f"roles/{m.name}/{path.name}"
            if not path.is_file():
                problems.append(f"(d) {key} is not rendered; run `dockwright compose`")
            elif path.read_text(encoding="utf-8") != text:
                problems.append(f"(d) {key} is stale; run `dockwright compose`")
            size, lines = len(text.encode()), text.count("\n")
            parts = path.name[len("role."):-len(".md")].split(".") if path.name.endswith(".md") else []
            launch, profile = (parts[0], parts[1]) if len(parts) == 2 else (parts[0] if parts else "", NONE_PROFILE)
            if profile == NONE_PROFILE:
                limit = ceilings.get(key) or {}
            else:
                role_limit = (ceilings.get(f"roles/{m.name}/role.{launch}.md") or {}).get("bytes")
                profile_limit = (ceilings.get(f"profiles/{profile}.md") or {}).get("bytes")
                limit = {"bytes": role_limit + profile_limit} if role_limit and profile_limit else {}
            if limit.get("bytes") and size > limit["bytes"]:
                problems.append(f"(f) {key}: {size} B is over the [ceilings] {limit['bytes']} B")
            if limit.get("lines") and lines > limit["lines"]:
                problems.append(f"(f) {key}: {lines} lines is over the [ceilings] {limit['lines']}")
            target = _size_target(m, launch) if parts and profile == NONE_PROFILE else None
            if target and size > target:
                problems.append(f"(m) {key}: {size} B is past the size target {target} B")
    for profile, text in _bundle_texts(units, sets).items():
        key, size = f"profiles/{profile}.md", len(text.encode())
        limit = ceilings.get(key) or {}
        if limit.get("bytes") and size > limit["bytes"]:
            problems.append(f"(f) {key}: {size} B is over the [ceilings] {limit['bytes']} B")
    known = {f"roles/{m.name}/{name}" for m in manifests.values()
             for name in (*(f"role.{launch}.md" for launch in _launches(m)), SETTINGS)}
    known |= {f"profiles/{p}.md" for p in sets}
    for key in sorted(k for k in ceilings if k.startswith(("roles/", "profiles/")) and k not in known):
        problems.append(f"(f) {key}: named in [ceilings] but no role renders it")
    problems += landing_problems(layout, manifests, applied)
    problems += [f"(d) roles/{path.relative_to(layout.roles_dir)} is a render with no manifest; run `dockwright compose`"
                 for path in orphan_renders(layout, manifests)]
    problems += stale_citations(layout)
    from . import catalog
    data = None
    if (layout.roles_dir / CATALOG_BASELINE).is_file() or (layout.roles_dir / CATALOG_FILE).is_file():
        try:
            data = catalog.build(layout, vars)
        except RoleError as err:
            problems.append(f"(j) catalog: {err}")
    if data is not None:
        if (layout.roles_dir / CATALOG_BASELINE).is_file():
            problems += catalog.baseline_problems(layout, data)
        if (layout.roles_dir / CATALOG_FILE).is_file():
            problems += catalog.freshness_problems(layout, data)
    problems += _flow_problems(layout)
    ok = not [p for p in problems if not _is_warning(p)]
    return ok, problems


def launch_files(role: str | None, launch: str, roles_dir: Path | None = None,
                 profile: str = NONE_PROFILE) -> tuple[Path, Path] | None:
    root = _root(roles_dir)
    names = role_names(root)
    if not names:
        return None
    name = role or DEFAULT_ROLE
    if name not in names:
        raise RoleError(f"unknown role {name!r}; roles with a manifest in {root}: {', '.join(names)}")
    manifest = load_role(name, root)
    if launch not in manifest.launch:
        raise RoleError(f"role {name} does not declare launch {launch!r} (its launch: {list(manifest.launch)})")
    files = (root / name / render_name(launch, profile), root / name / SETTINGS)
    missing = [str(p) for p in files if not p.is_file()]
    if missing:
        raise RoleError(f"role {name}: {', '.join(missing)} missing; run `dockwright compose`")
    return files


def codex_file(role: str | None, roles_dir: Path | None = None, profile: str = NONE_PROFILE) -> Path | None:
    root = _root(roles_dir)
    names = role_names(root)
    if not names:
        return None
    name = role or DEFAULT_ROLE
    if name not in names:
        raise RoleError(f"unknown role {name!r}; roles with a manifest in {root}: {', '.join(names)}")
    path = root / name / render_name(CODEX, profile)
    if not path.is_file():
        raise RoleError(f"role {name}: {path} missing; run `dockwright compose` "
                        "(a role renders for Codex only when its launch holds worker or child)")
    return path


def codex_text(role: str | None, roles_dir: Path | None = None, profile: str = NONE_PROFILE) -> str:
    path = codex_file(role, roles_dir, profile)
    return "" if path is None else path.read_text(encoding="utf-8")


def read_role_layer(settings_file: Path) -> dict:
    try:
        layer = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as err:
        raise RoleError(f"{settings_file}: {err}; run `dockwright compose`") from err
    if not isinstance(layer, dict):
        raise RoleError(f"{settings_file}: not a JSON object; run `dockwright compose`")
    problems = pipeline.profile_problems(layer, str(settings_file), keys=pipeline.ROLE_SETTINGS_KEYS)
    if problems:
        raise RoleError("; ".join(problems))
    return layer


_SKILL_STRICTNESS = {"on": 0, "name-only": 1, "user-invocable-only": 2, "off": 3}


def apply_layer(base: dict, layer: dict) -> dict:
    merged = pipeline.merge_settings(base, layer)
    prior, now = base.get("skillOverrides"), merged.get("skillOverrides")
    if isinstance(prior, dict) and isinstance(now, dict):
        merged["skillOverrides"] = {
            name: prior[name] if _SKILL_STRICTNESS.get(prior.get(name), -1) > _SKILL_STRICTNESS.get(value, -1)
            else value for name, value in now.items()}
    return merged


def agents_arg(profile: str, roles_dir: Path | None = None, print_mode: bool = False) -> list[str]:
    path = _root(roles_dir) / AGENTS_DIR / f"{profile}.json"
    if not path.is_file():
        return []
    return ["--agents" if print_mode else "--agents-file", str(path)]


def default_role_args(extra_args: list[str], launch: str, cwd: str | Path | None = None) -> list[str]:
    from .profiles import profile_for
    profile = profile_for(cwd or os.getcwd())
    files = launch_files(None, launch, profile=profile)
    if files is None:
        return list(extra_args)
    layer = read_role_layer(files[1])
    rest, value = pipeline.split_settings_arg(list(extra_args))
    base = {} if value is None else pipeline.read_settings_value(value)
    return ["--append-system-prompt-file", str(files[0]), *rest,
            "--settings", str(write_spawn_settings(apply_layer(base, layer))), *agents_arg(profile)]


def write_spawn_settings(settings: dict) -> Path:
    text = json.dumps(settings, indent=2, sort_keys=True) + "\n"
    target = paths.ROLE_SETTINGS / f"{hashlib.sha256(text.encode()).hexdigest()[:16]}.json"
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
    return target


def main(argv=None) -> int:
    from .profiles import profile_for
    parser = argparse.ArgumentParser(prog="dockwright role-args",
                                     description="Print the flags that launch a session in a role, one per line.")
    parser.add_argument("role")
    parser.add_argument("--launch", required=True, choices=LAUNCHES)
    parser.add_argument("--base", default=None,
                        help="A settings file or inline JSON the role's settings merge onto.")
    parser.add_argument("--cwd", default=None, help="The directory the session starts in; picks its project profile.")
    parser.add_argument("--render-only", action="store_true",
                        help="Print only --append-system-prompt-file <render>, for a locked-down headless lane.")
    parser.description += (" For --launch lane the definitions come as --agents <path>, the form claude -p accepts;"
                           " every other launch gets --agents-file <path> for dockwright launch to expand.")
    args = parser.parse_args(argv)
    try:
        profile = profile_for(args.cwd) if args.cwd else NONE_PROFILE
        files = launch_files(args.role, args.launch, profile=profile)
        base = pipeline.read_settings_value(args.base) if args.base else {}
        layer = None if files is None else read_role_layer(files[1])
    except (RoleError, ValueError, OSError) as err:
        print(f"role-args: {err}", file=sys.stderr)
        return 2
    if files is None:
        if args.base and not args.render_only:
            print("--settings")
            print(args.base)
        return 0
    render_file, _ = files
    out = ["--append-system-prompt-file", str(render_file)]
    if not args.render_only:
        out += ["--settings", str(write_spawn_settings(apply_layer(base, layer))),
                *agents_arg(profile, print_mode=args.launch == "lane")]
    for arg in out:
        print(arg)
    return 0
