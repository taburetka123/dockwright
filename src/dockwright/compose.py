from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import config

MARKER_RE = re.compile(r"^<!-- overlay: ([A-Za-z0-9_-]+) -->$")
INCLUDE_RE = re.compile(r"^<!-- include: ([A-Za-z0-9_-]+) -->$")
INCLUDE_LIKE_RE = re.compile(r"^\s*<!--\s*include\b", re.IGNORECASE)
VAR_RE = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")
HOME_TOKEN = "<absolute-home>"

STAMP_NAME = ".compose-stamp.json"


class ComposeError(Exception):
    pass


@dataclass(frozen=True)
class DropIn:
    path: Path
    insert_at: str | None
    body: str


def parse_dropin(path: Path) -> DropIn:
    text = path.read_text()
    insert_at = None
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            raw_meta = text[4:end]
            body = text[end + 5:]
            for line in raw_meta.splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    if key.strip() == "insert_at":
                        insert_at = value.strip()
    if not body.endswith("\n"):
        body += "\n"
    return DropIn(path=path, insert_at=insert_at, body=body)


def marker_names(text: str) -> set[str]:
    return {m.group(1) for m in map(MARKER_RE.match, text.splitlines()) if m}


def lands(dropin: DropIn, markers, appends: bool) -> bool:
    return dropin.insert_at in markers if dropin.insert_at is not None else appends


def load_dropins(overlay_dir: Path, agent_stem: str) -> list[DropIn]:
    agent_dir = overlay_dir / agent_stem
    if not agent_dir.is_dir():
        return []
    return [parse_dropin(p) for p in sorted(agent_dir.glob("*.md"))]


def core_fragments_dir(core_dir) -> Path:
    return Path(core_dir).parent / "roles" / "fragments"


def expand_includes(text: str, fragments_dir: Path) -> str:
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        m = INCLUDE_RE.match(stripped)
        if not m:
            if INCLUDE_LIKE_RE.match(stripped):
                raise ComposeError(
                    f"malformed include line {stripped!r}; "
                    f"the form is `<!-- include: <name> -->`")
            out.append(line)
            continue
        path = Path(fragments_dir) / f"{m.group(1)}.md"
        if not path.is_file():
            raise ComposeError(f"include {m.group(1)!r}: no file {path}")
        body = _strip_frontmatter(path.read_text())
        if any(INCLUDE_LIKE_RE.match(inner) for inner in body.splitlines()):
            raise ComposeError(f"include {m.group(1)!r}: nested include in {path}")
        out.append(body if body.endswith("\n") else body + "\n")
    return "".join(out)


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5:]
    return text


def compose_text(core_text: str, dropins, vars) -> tuple[str, list[str]]:
    lines = core_text.splitlines(keepends=True)
    markers: list[str] = []
    for line in lines:
        m = MARKER_RE.match(line.rstrip("\n"))
        if m:
            name = m.group(1)
            if name in markers:
                raise ComposeError(
                    f"duplicate marker {name!r} in the core after includes are expanded")
            markers.append(name)
    unknown = sorted({d.insert_at for d in dropins
                      if d.insert_at is not None and d.insert_at not in markers})
    if unknown:
        raise ComposeError(
            f"drop-in insert_at {unknown} match no marker; valid markers: {markers}")
    by_marker: dict[str, list[DropIn]] = {}
    for d in dropins:
        if d.insert_at is not None:
            by_marker.setdefault(d.insert_at, []).append(d)
    out: list[str] = []
    for line in lines:
        m = MARKER_RE.match(line.rstrip("\n"))
        if m:
            for d in by_marker.get(m.group(1), ()):
                out.append(d.body)
            continue
        out.append(line)
    composed = "".join(out)
    for d in dropins:
        if d.insert_at is None:
            if composed and not composed.endswith("\n"):
                composed += "\n"
            composed += d.body
    if vars:
        expanded = {k: v.replace(HOME_TOKEN, str(Path.home()))
                    for k, v in vars.items()}
        composed = VAR_RE.sub(
            lambda m: expanded.get(m.group(1), m.group(0)), composed)
    if HOME_TOKEN in composed:
        raise ComposeError(
            f"literal {HOME_TOKEN} in composed output — the token is expanded "
            f"only inside [agent_vars] VALUES; move the path into a var, or "
            f"write the literal absolute path instead")
    unbound = sorted({m.group(1) for m in VAR_RE.finditer(composed)})
    warnings = [f"unbound vars left literal: {unbound}"] if unbound else []
    return composed, warnings


CORE_SUFFIX = ".core.md"


def output_name(core_filename: str) -> str:
    if core_filename.endswith(CORE_SUFFIX):
        return core_filename[: -len(CORE_SUFFIX)] + ".md"
    return core_filename


def load_default_vars(core_dir) -> dict[str, str]:
    path = Path(core_dir) / "vars.defaults.toml"
    if not path.is_file():
        return {}
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    section = data.get("agent_vars")
    if not isinstance(section, dict):
        return {}
    return {k: v for k, v in section.items()
            if isinstance(k, str) and isinstance(v, str)}


def _git_sha(repo_path: Path) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(repo_path), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


@dataclass(frozen=True)
class _Rendered:
    composed: str
    warnings: list[str]
    dropins: list[DropIn]
    core_name: str
    core_path: Path


LEGACY_ROLE_OVERLAYS = {"manager": ("Manager",), "worker": ("_shared",), "dockwright-reviewer": ("CodeReviewer",)}


def _legacy_dropins(overlay_dir: Path, roles_dir: Path | None, stem: str, text: str) -> list[DropIn]:
    dropins = load_dropins(Path(overlay_dir), stem)
    homed: list[DropIn] = []
    if roles_dir is not None:
        from . import roles
        manifests = roles.role_names(roles_dir)
        for role in LEGACY_ROLE_OVERLAYS.get(stem, ()):
            found = load_dropins(Path(roles_dir) / role, "overlay")
            has_render = bool(manifests) if role == roles.SHARED else role in manifests
            if has_render and dropins:
                raise ComposeError(
                    f"{stem}: drop-in(s) {[d.path.name for d in dropins]} in {Path(overlay_dir) / stem} reach "
                    f"{stem}.md but no role render; roles in {roles_dir} have manifests, so move them to "
                    f"{Path(roles_dir) / role / 'overlay'}")
            (homed if has_render else dropins).extend(found)
    names = [d.path.name for d in dropins + homed]
    twice = sorted({n for n in names if names.count(n) > 1})
    if twice:
        raise ComposeError(f"{stem}: drop-in file name(s) {twice} exist in both {overlay_dir}/{stem} "
                           "and the role overlay; keep one")
    markers = marker_names(text)
    dropins += [d for d in homed if lands(d, markers, appends=True)]
    return sorted(dropins, key=lambda d: d.path.name)


def _subagent_renders(layout, vars) -> dict[str, tuple[str, Path]]:
    subagents = config.subagents() if layout is not None else {}
    if not subagents:
        return {}
    from . import roles
    plan = roles.plan_roles(layout, vars)
    out: dict[str, tuple[str, Path]] = {}
    for stem, spec in subagents.items():
        role_dir = layout.roles_dir / str(spec.get("role"))
        text = plan.files.get(role_dir / "role.agent.md")
        if text is None:
            raise ComposeError(f"[subagents] {stem}: role {spec.get('role')!r} renders no role.agent.md")
        core = roles.core_file(layout, str(spec.get("core")))
        core_text = core.read_text()
        meta_end = core_text.find("\n---\n", 4)
        header = core_text[: meta_end + 5] if core_text.startswith("---\n") and meta_end != -1 else ""
        out[f"{stem}.md"] = (header + text, core)
    return out


def _compose_all(core_dir: Path, overlay_dir: Path, vars, roles_dir: Path | None = None,
                 layout=None) -> dict[str, _Rendered]:
    core_files = sorted(Path(core_dir).glob("*.md"))
    if not core_files:
        raise ComposeError(f"no core agent files in {core_dir}")
    by_output: dict[str, list[Path]] = {}
    for core in core_files:
        by_output.setdefault(output_name(core.name), []).append(core)
    ambiguous = {out: [p.name for p in paths]
                for out, paths in by_output.items() if len(paths) > 1}
    if ambiguous:
        raise ComposeError(
            f"ambiguous core agent files (both .core.md and .md present) "
            f"for output name(s): {ambiguous}")
    rendered: dict[str, _Rendered] = {}
    agent_files = _subagent_renders(layout, vars)
    for out_name, paths in by_output.items():
        if out_name in agent_files:
            continue
        core = paths[0]
        text = expand_includes(core.read_text(), core_fragments_dir(core_dir))
        dropins = _legacy_dropins(Path(overlay_dir), roles_dir, Path(out_name).stem, text)
        composed, warnings = compose_text(text, dropins, vars)
        rendered[out_name] = _Rendered(composed, warnings, dropins, core.name, core)
    for out_name, (text, core) in agent_files.items():
        if load_dropins(Path(overlay_dir), Path(out_name).stem):
            raise ComposeError(f"{out_name}: drop-ins in {Path(overlay_dir) / Path(out_name).stem} cannot reach a "
                               "composed subagent; move them to the role's overlay")
        composed, warnings = compose_text(text, [], vars)
        rendered[out_name] = _Rendered(composed, warnings, [], core.name, core)
    return rendered


def _prev_outputs(out_dir: Path) -> dict:
    try:
        data = json.loads((out_dir / STAMP_NAME).read_text())
    except (OSError, ValueError):
        return {}
    outputs = data.get("outputs")
    return outputs if isinstance(outputs, dict) else {}


@dataclass(frozen=True)
class _Drift:
    message: str
    saved: bool
    abort_line: str = ""
    backup_line: str = ""


def _pick_backup_path(target: Path, current: bytes) -> Path:
    backup = target.with_name(target.name + ".bak")
    n = 1
    while backup.exists():
        try:
            if backup.read_bytes() == current:
                return backup
        except OSError:
            pass
        n += 1
        backup = target.with_name(f"{target.name}.bak.{n}")
    return backup


def _backup_drifted(target: Path, prev_sha, about_to_write: bytes,
                    overlay_dir: Path) -> _Drift | None:
    if not isinstance(prev_sha, str) or not prev_sha or not target.is_file():
        return None
    try:
        current = target.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(current).hexdigest() == prev_sha:
        return None
    if current == about_to_write:
        return None
    remedy = (f"to keep it, move the wanted lines into a drop-in at "
              f"{overlay_dir / Path(target.name).stem}/*.md and rerun")
    backup = _pick_backup_path(target, current)
    try:
        backup.write_bytes(current)
    except OSError as exc:
        unblock = (f"make {backup} writable (or move it aside), then rerun"
                   if backup.exists() else
                   f"make the directory {backup.parent} writable, then rerun")
        return _Drift(
            f"{target.name}: edited since the last compose — those bytes live in "
            f"NEITHER the core NOR the overlay and could NOT be saved ({exc}), so "
            f"{target.name} was NOT rewritten and your edit is still on disk; "
            f"{unblock}, or {remedy}",
            saved=False,
            abort_line=(f"{target.name}: NOT rewritten — the backup at {backup} "
                        f"could not be written ({exc}); {unblock}"))
    return _Drift(
        f"{target.name}: edited since the last compose — those bytes live in "
        f"NEITHER the core NOR the overlay and this compose overwrites them; "
        f"saved to {backup.name}; {remedy}",
        saved=True,
        backup_line=(f"{target.name}: REWRITTEN — your previous contents were "
                     f"saved to {backup}"))


def compose_agents(core_dir, out_dir, overlay_dir, vars, roles_dir=None, layout=None) -> dict:
    core_dir, out_dir, overlay_dir = Path(core_dir), Path(out_dir), Path(overlay_dir)
    merged_vars = {**load_default_vars(core_dir), **vars}
    rendered = _compose_all(core_dir, overlay_dir, merged_vars, roles_dir, layout)
    out_dir.mkdir(parents=True, exist_ok=True)
    prev_outputs = _prev_outputs(out_dir)
    stamp: dict = {
        "composed_at": time.time(),
        "core": {}, "core_sources": {}, "overlay": {}, "outputs": {},
        "vars_sha256": hashlib.sha256(
            json.dumps(dict(sorted(merged_vars.items()))).encode()).hexdigest(),
        "core_git_sha": _git_sha(core_dir),
    }
    all_warnings: dict[str, list[str]] = {}
    drift: list[str] = []
    skipped: list[str] = []
    skip_details: list[str] = []
    backed_up: list[str] = []
    for out_name, r in rendered.items():
        target = out_dir / out_name
        drifted = _backup_drifted(target, prev_outputs.get(out_name),
                                  r.composed.encode(), overlay_dir)
        if drifted is not None:
            drift.append(drifted.message)
        if drifted is not None and not drifted.saved:
            skipped.append(out_name)
            skip_details.append(drifted.abort_line)
            stamp["outputs"][out_name] = prev_outputs[out_name]
        else:
            if drifted is not None:
                backed_up.append(drifted.backup_line)
            target.write_text(r.composed)
            stamp["outputs"][out_name] = hashlib.sha256(target.read_bytes()).hexdigest()
        stamp["core"][out_name] = hashlib.sha256(r.core_path.read_bytes()).hexdigest()
        stamp["core_sources"][out_name] = r.core_name
        stem = Path(out_name).stem
        for d in r.dropins:
            stamp["overlay"][f"{stem}/{d.path.name}"] = hashlib.sha256(
                d.path.read_bytes()).hexdigest()
        if r.warnings:
            all_warnings[out_name] = r.warnings
    (out_dir / STAMP_NAME).write_text(json.dumps(stamp, indent=2))
    return {"files": sorted(rendered), "warnings": all_warnings,
            "drift": drift, "skipped": sorted(skipped),
            "skip_details": sorted(skip_details),
            "backed_up": sorted(backed_up)}


def check_agents(core_dir, out_dir, overlay_dir, vars, roles_dir=None, layout=None) -> tuple[bool, list[str]]:
    core_dir, out_dir = Path(core_dir), Path(out_dir)
    merged_vars = {**load_default_vars(core_dir), **vars}
    rendered = _compose_all(core_dir, Path(overlay_dir), merged_vars, roles_dir, layout)
    problems: list[str] = []
    for out_name, r in rendered.items():
        deployed = out_dir / out_name
        if not deployed.is_file():
            problems.append(f"{out_name}: not deployed")
        elif deployed.read_text() != r.composed:
            problems.append(f"{out_name}: deployed differs from composed")
    return (not problems, problems)


def _default_core_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "deploy" / "agents"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="dockwright compose",
        description="Compose deployed agent files from core + overlay + vars.")
    parser.add_argument("--core-dir", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--overlay-dir", type=Path, default=None)
    parser.add_argument("--roles-dir", type=Path, default=None,
                        help="Render the role manifests here too. A bare `dockwright compose` uses [paths] roles_dir.")
    parser.add_argument("--roles-core-dir", type=Path, default=None,
                        help="The canonical role cores and fragments. Default: roles/ beside the core dir.")
    parser.add_argument("--claude-dir", type=Path, default=None,
                        help="The Claude config dir whose rules/, skills/ and commands/ the roles read.")
    parser.add_argument("--check", action="store_true",
                        help="Recompose in memory and diff against the deployed files; exit 1 if stale.")
    args = parser.parse_args(argv)
    core_dir = args.core_dir or _default_core_dir()
    out_dir = args.out_dir or (config.claude_config_home() / "agents")
    overlay = args.overlay_dir or config.overlay_dir()
    vars = config.agent_vars()
    from . import roles
    layout = None
    if args.roles_dir is not None or (args.core_dir is None and args.out_dir is None):
        roles_dir = args.roles_dir or config.roles_dir()
        layout = roles.Layout(roles_dir, roles.core_dirs_for(core_dir, args.roles_core_dir),
                              args.claude_dir or (roles_dir.parent if args.roles_dir else config.claude_config_home()))
    merged_vars = {**load_default_vars(core_dir), **vars}
    roles_dir = layout.roles_dir if layout is not None else None
    role_result = None
    try:
        if args.check:
            ok, problems = check_agents(core_dir, out_dir, overlay, vars, roles_dir, layout)
            for p in problems:
                print(f"stale: {p}")
            warned = 0
            if layout is not None:
                roles_ok, role_problems = roles.check_roles(layout, merged_vars)
                for p in role_problems:
                    print(f"roles: {p}")
                warned = sum(1 for p in role_problems if roles._is_warning(p))
                ok = ok and roles_ok
            if ok:
                print("compose check: fresh" + (f" ({warned} warning(s))" if warned else ""))
            else:
                print("compose check: STALE — rerun `dockwright compose` or setup.sh")
            return 0 if ok else 1
        role_plan = roles.plan_roles(layout, merged_vars) if layout is not None else None
        result = compose_agents(core_dir, out_dir, overlay, vars, roles_dir, layout)
        if role_plan is not None:
            role_result = roles.compose_roles(layout, merged_vars, role_plan)
    except (ComposeError, roles.RoleError) as e:
        print(f"compose: ERROR: {e}", file=sys.stderr)
        return 1
    written = len(result["files"]) - len(result["skipped"])
    print(f"Composed {written} agent file(s) to {out_dir}")
    for fname, warns in sorted(result["warnings"].items()):
        for w in warns:
            print(f"warning: {fname}: {w}", file=sys.stderr)
    if role_result is not None:
        if role_result["files"]:
            print(f"Composed {len(role_result['files'])} role file(s) to {layout.roles_dir}")
        for w in role_result["warnings"]:
            print(f"warning: roles: {w}", file=sys.stderr)
    for d in result["drift"]:
        print(f"DRIFT: {d}", file=sys.stderr)
    if result["skipped"]:
        if result["backed_up"]:
            print(f"compose: ERROR: YOUR EDITS ARE NOT LOST — but this run did BOTH: "
                  f"{len(result['skipped'])} file(s) were NOT rewritten and still hold "
                  f"your edits byte-for-byte, and {len(result['backed_up'])} other "
                  f"edited file(s) WERE rewritten with the previous contents saved "
                  f"aside. Every affected file, and what to do about it:",
                  file=sys.stderr)
        else:
            print(f"compose: ERROR: NOTHING WAS OVERWRITTEN — {len(result['skipped'])} "
                  f"deployed file(s) still hold your edits byte-for-byte, because the "
                  f"backup that must precede a rewrite could not be written:",
                  file=sys.stderr)
        for line in result["skip_details"] + result["backed_up"]:
            print(f"compose: ERROR:   {line}", file=sys.stderr)
        return 1
    return 0
