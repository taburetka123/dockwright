"""Compose deployed agent files: core + overlay drop-ins + config vars.

The OSS-split compose seam (Step 2). setup.sh and `dockwright compose`
render ~/.claude/agents/*.md from deploy/agents/*.md instead of cp.

Semantics:
- A line that is exactly `<!-- overlay: <name> -->` is an insertion point.
  Drop-ins bound to it (frontmatter `insert_at: <name>`) replace the marker
  line, sorted by filename; with no bound drop-ins the marker line is removed.
  A duplicate marker name in one core file is an error (ambiguous target).
- Drop-ins live at <overlay_dir>/<agent-stem>/*.md with an optional
  `---`-delimited frontmatter block; the only recognized key is `insert_at`.
  Drop-ins without insert_at are appended at end-of-file.
- `{{name}}` substitutes dockwright.toml [agent_vars] entries across the
  composed text; an unbound `{{name}}` stays literal and is reported as a
  warning, never an error.
- `<absolute-home>` inside a var VALUE expands to the installing user's
  absolute home (`Path.home()`, honors $HOME) at compose time. If the literal
  token survives anywhere in composed output (core prose, a drop-in body) the
  compose FAILS LOUD with ComposeError — a deployed agent file carrying the
  token is exactly the broken artifact this prevents. Narrow amendment to the
  IDENTITY GUARANTEE below: token-bearing text errors instead of passing
  through; token-free text is untouched.
- A drop-in naming an unknown marker FAILS LOUD (ComposeError listing the
  valid markers) — a silently misplaced overlay section would be worse.
- IDENTITY GUARANTEE: composing a core text with no markers and no vars
  returns it byte-for-byte (trailing-newline state included). The Step-2
  byte-equivalence gate rests on this.
- Provenance is a SIDECAR stamp (.compose-stamp.json in the out dir), never
  an in-file header — deployed agent files carry only prompt content.
- Core-dir naming (Step 3): `X.core.md` composes to OUTPUT `X.md`; a plain
  `X.md` composes to itself unchanged. Both present for the same output stem
  is ambiguous and FAILS LOUD. Drop-ins for either form live at
  `<overlay_dir>/X/*.md` — keyed by the OUTPUT stem, not the core filename.
- `<core_dir>/vars.defaults.toml` `[agent_vars]` is an optional defaults
  layer (same str-key/str-value validation as config.agent_vars()); operator
  vars (dockwright.toml `[agent_vars]`) win per-key over the defaults.
- The stamp's `core` keys are the deployed OUTPUT basenames (so a mirror
  step keyed off `stamp["core"]` resolves deployed files unchanged); the
  core SOURCE filename per output is recorded separately in
  `stamp["core_sources"]` (informational).
- The stamp's `outputs` records what compose last successfully WROTE per
  output basename — not necessarily what is on disk now. They diverge
  exactly when a file was skipped (its backup failed) or hand-edited since;
  that divergence IS the drift signal, so a reader must treat "differs from
  disk" as drift, never as a stale stamp.
"""
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


def load_dropins(overlay_dir: Path, agent_stem: str) -> list[DropIn]:
    agent_dir = overlay_dir / agent_stem
    if not agent_dir.is_dir():
        return []
    return [parse_dropin(p) for p in sorted(agent_dir.glob("*.md"))]


def compose_text(core_text: str, dropins, vars) -> tuple[str, list[str]]:
    """(composed, warnings). Only marker lines and var matches are touched —
    everything else passes through byte-for-byte."""
    lines = core_text.splitlines(keepends=True)
    markers: list[str] = []
    for line in lines:
        m = MARKER_RE.match(line.rstrip("\n"))
        if m:
            name = m.group(1)
            if name in markers:
                raise ComposeError(f"duplicate marker {name!r} in core file")
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
    """`X.core.md` -> `X.md`; any other filename (incl. a plain `X.md`) is
    returned unchanged."""
    if core_filename.endswith(CORE_SUFFIX):
        return core_filename[: -len(CORE_SUFFIX)] + ".md"
    return core_filename


def load_default_vars(core_dir) -> dict[str, str]:
    """`<core_dir>/vars.defaults.toml` `[agent_vars]` table — same per-entry
    str-key/str-value validation as config.agent_vars(). Missing file,
    missing/malformed section, or an unparseable file is fail-open to {}
    (mirrors config.load()'s fail-open discipline)."""
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
    core_name: str  # source filename in core_dir, e.g. "manager.core.md"


def _compose_all(core_dir: Path, overlay_dir: Path, vars) -> dict[str, _Rendered]:
    """Compose every core file IN MEMORY first — a ComposeError must abort
    before anything is written (no half-deployed agent set). Keyed by OUTPUT
    name (the deployed basename): `X.core.md` -> `X.md`, a plain `X.md` is
    unchanged. Both forms present for the same output stem is ambiguous."""
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
    for out_name, paths in by_output.items():
        core = paths[0]
        dropins = load_dropins(Path(overlay_dir), Path(out_name).stem)
        composed, warnings = compose_text(core.read_text(), dropins, vars)
        rendered[out_name] = _Rendered(composed, warnings, dropins, core.name)
    return rendered


def _prev_outputs(out_dir: Path) -> dict:
    """Output hashes the LAST compose wrote, or {} when unavailable (first
    deploy, a pre-`outputs` stamp, an unreadable or corrupt one). Fail-open:
    with no history we make no drift claim — never a false one."""
    try:
        data = json.loads((out_dir / STAMP_NAME).read_text())
    except (OSError, ValueError):
        return {}
    outputs = data.get("outputs")
    return outputs if isinstance(outputs, dict) else {}


@dataclass(frozen=True)
class _Drift:
    message: str
    saved: bool  # False: the backup failed, so the write MUST be skipped
    # Two lines for the abort summary main() prints LAST when ANY file was
    # skipped: `abort_line` for a skipped file (saved=False), `backup_line` for
    # one that WAS rewritten in that same run (saved=True). Both self-contained
    # on purpose: they are the last thing an operator sees before set -e halts
    # setup.sh, so neither may lean on the DRIFT line scrolled above.
    abort_line: str = ""
    backup_line: str = ""


def _pick_backup_path(target: Path, current: bytes) -> Path:
    """A backup path that will not clobber a DIFFERENT saved copy: `<name>.bak`
    when it is free or already holds exactly these bytes, else `<name>.bak.2`,
    `.bak.3`, … A backup we cannot read counts as differing — the whole point is
    that the content it holds may exist nowhere else."""
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
    """A deployed file whose bytes differ from what compose last wrote holds
    content with no home in core or overlay — the write that follows would
    destroy it with no trace. Copy it aside and report it. Returns the drift,
    or None when this write destroys nothing."""
    if not isinstance(prev_sha, str) or not prev_sha or not target.is_file():
        return None
    try:
        current = target.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(current).hexdigest() == prev_sha:
        return None
    if current == about_to_write:
        # The edit already matches what we are about to write (the operator
        # hand-applied their core change to the deployed copy). Mirrors
        # setup.sh's `! cmp -s` guard: no change, nothing to preserve.
        return None
    remedy = (f"to keep it, move the wanted lines into a drop-in at "
              f"{overlay_dir / Path(target.name).stem}/*.md and rerun")
    backup = _pick_backup_path(target, current)
    try:
        backup.write_bytes(current)
    except OSError as exc:
        # Prescribe from what is ACTUALLY there, not from the errno: _pick_backup_path
        # only ever returns a free name or one already holding these exact bytes, so a
        # non-existent backup means the DIRECTORY refused the new entry — telling the
        # operator to clear an obstruction at a file that is not there is a dead end.
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


def compose_agents(core_dir, out_dir, overlay_dir, vars) -> dict:
    core_dir, out_dir, overlay_dir = Path(core_dir), Path(out_dir), Path(overlay_dir)
    merged_vars = {**load_default_vars(core_dir), **vars}
    rendered = _compose_all(core_dir, overlay_dir, merged_vars)
    out_dir.mkdir(parents=True, exist_ok=True)
    prev_outputs = _prev_outputs(out_dir)
    stamp: dict = {
        "composed_at": time.time(),
        "core": {}, "core_sources": {}, "overlay": {}, "outputs": {},
        # Hashed over the UNEXPANDED merged map — informational only; do not
        # build freshness logic on it (it never moves when $HOME differs).
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
        # write_text() encodes with the platform default; comparing against UTF-8
        # can only make the guard MORE conservative (a false "differs", never a
        # false "same"), so the fail-safe direction is preserved off-UTF-8.
        drifted = _backup_drifted(target, prev_outputs.get(out_name),
                                  r.composed.encode(), overlay_dir)
        if drifted is not None:
            drift.append(drifted.message)
        if drifted is not None and not drifted.saved:
            # Fail closed: the operator's only copy could not be preserved, so
            # leave the file alone. Carrying the PREVIOUS hash (never the
            # hand-edited file's) keeps the next compose re-detecting this drift.
            skipped.append(out_name)
            skip_details.append(drifted.abort_line)
            stamp["outputs"][out_name] = prev_outputs[out_name]
        else:
            if drifted is not None:
                backed_up.append(drifted.backup_line)
            target.write_text(r.composed)
            # Hash the bytes READ BACK rather than the in-memory string: defence
            # in depth so the stamp reflects what a reader finds on disk even if
            # the platform encoding altered them on the way out. No test can
            # distinguish the two on a UTF-8 host.
            stamp["outputs"][out_name] = hashlib.sha256(target.read_bytes()).hexdigest()
        stamp["core"][out_name] = hashlib.sha256(
            (core_dir / r.core_name).read_bytes()).hexdigest()
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


def check_agents(core_dir, out_dir, overlay_dir, vars) -> tuple[bool, list[str]]:
    core_dir, out_dir = Path(core_dir), Path(out_dir)
    merged_vars = {**load_default_vars(core_dir), **vars}
    rendered = _compose_all(core_dir, Path(overlay_dir), merged_vars)
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
    parser.add_argument("--check", action="store_true",
                        help="Recompose in memory and diff against the deployed files; exit 1 if stale.")
    args = parser.parse_args(argv)
    core_dir = args.core_dir or _default_core_dir()
    out_dir = args.out_dir or (config.claude_config_home() / "agents")
    overlay = args.overlay_dir or config.overlay_dir()
    vars = config.agent_vars()
    try:
        if args.check:
            ok, problems = check_agents(core_dir, out_dir, overlay, vars)
            for p in problems:
                print(f"stale: {p}")
            print("compose check: fresh" if ok else "compose check: STALE — rerun `dockwright compose` or setup.sh")
            return 0 if ok else 1
        result = compose_agents(core_dir, out_dir, overlay, vars)
    except ComposeError as e:
        print(f"compose: ERROR: {e}", file=sys.stderr)
        return 1
    written = len(result["files"]) - len(result["skipped"])
    print(f"Composed {written} agent file(s) to {out_dir}")
    for fname, warns in sorted(result["warnings"].items()):
        for w in warns:
            print(f"warning: {fname}: {w}", file=sys.stderr)
    for d in result["drift"]:
        print(f"DRIFT: {d}", file=sys.stderr)
    if result["skipped"]:
        # This is the last thing an operator sees: setup.sh runs under `set -e`, so
        # returning 1 here aborts the deploy mid-run. Every element below is load
        # bearing — which file, the backup path AND the OS error, what happened to
        # each edited file, and the one action that unblocks it.
        #
        # The headline is CONDITIONAL because the loop above has three outcomes, not
        # two: skipped (untouched), drifted-and-backed-up (REWRITTEN, copy saved),
        # and clean. "NOTHING WAS OVERWRITTEN" is true only when no sibling was
        # rewritten this run; claiming it otherwise would tell an operator whose
        # worker.md was just overwritten that they have nothing to look for — the
        # DRIFT line naming that backup has already scrolled away, which is the
        # entire reason this self-contained block exists.
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
