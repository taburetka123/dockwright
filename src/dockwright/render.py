from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import compose, config


def render_text(text: str, vars: dict[str, str]) -> str:
    composed, _warnings = compose.compose_text(text, [], vars)
    return composed


def render_file(src: Path, out: Path, vars: dict[str, str]) -> None:
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
    try:
        if args.src.is_dir():
            args.out.mkdir(parents=True, exist_ok=True)
            files = sorted(args.src.glob(args.glob))
            for f in files:
                render_file(f, args.out / f.name, merged_vars)
            print(f"Rendered {len(files)} file(s) from {args.src} to {args.out}")
        else:
            render_file(args.src, args.out, merged_vars)
            print(f"Rendered {args.src} to {args.out}")
    except compose.ComposeError as e:
        print(f"render: ERROR: {e}", file=sys.stderr)
        return 1
    return 0
