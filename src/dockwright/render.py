"""Render {{vars}} in deployed files — the deploy-time template seam (Step 7).

A thin wrapper over compose.compose_text(text, [], vars): NO drop-ins, NO
overlay markers. Commands and .md presets are single files that only ever
carry {{name}} substitutions, never the overlay-composition surface agent
files use — so render is just compose's var pass.

Semantics (inherited from compose_text, unchanged):
- `{{name}}` substitutes a merged-vars entry; an unbound `{{name}}` stays
  literal and is reported as a warning, never an error.
- IDENTITY: a var-free file renders byte-for-byte (trailing-newline state
  included). This keeps today's operator command/preset deploys byte-stable,
  since no command/preset carries {{ }} yet.

The CLI mirrors `dockwright compose`'s var-merging: the defaults layer
(<core-dir>/vars.defaults.toml) is overlaid by the operator's dockwright.toml
[agent_vars] per-key.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import compose, config


def render_text(text: str, vars: dict[str, str]) -> str:
    """Render `{{vars}}` in `text` (unbound left literal). Thin wrapper over
    compose_text with no drop-ins/markers; discards warnings."""
    composed, _warnings = compose.compose_text(text, [], vars)
    return composed


def render_file(src: Path, out: Path, vars: dict[str, str]) -> None:
    """Read `src`, render `{{vars}}`, write to `out` (parent dirs created).
    Unbound-var warnings surface on stderr (compose warning semantics)."""
    src, out = Path(src), Path(out)
    composed, warnings = compose.compose_text(src.read_text(), [], vars)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(composed)
    for w in warnings:
        print(f"warning: {src.name}: {w}", file=sys.stderr)


def _default_core_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "deploy" / "agents"


def _merged_vars(core_dir: Path) -> dict[str, str]:
    return {**compose.load_default_vars(core_dir), **config.agent_vars()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="dockwright render",
        description="Render {{vars}} in a file or dir with merged "
                    "(defaults ⊕ operator) vars.")
    parser.add_argument("--src", type=Path, required=True,
                        help="Source file, or dir (with --glob).")
    parser.add_argument("--out", type=Path, required=True,
                        help="Output file (file src), or dir (dir src).")
    parser.add_argument("--glob", default="*.md",
                        help="Glob for dir src (default: *.md).")
    parser.add_argument("--core-dir", type=Path, default=None,
                        help="Dir holding vars.defaults.toml (default: deploy/agents).")
    args = parser.parse_args(argv)
    merged_vars = _merged_vars(args.core_dir or _default_core_dir())
    if args.src.is_dir():
        args.out.mkdir(parents=True, exist_ok=True)
        files = sorted(args.src.glob(args.glob))
        for f in files:
            render_file(f, args.out / f.name, merged_vars)
        print(f"Rendered {len(files)} file(s) from {args.src} to {args.out}")
    else:
        render_file(args.src, args.out, merged_vars)
        print(f"Rendered {args.src} to {args.out}")
    return 0
