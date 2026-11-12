"""Install Codex skill wrappers from dockwright slash-command markdown."""

from __future__ import annotations

import argparse
from pathlib import Path

from .codex_translate import render_skill_from_command


def install_codex_skills(commands_dir: Path, skills_dir: Path) -> list[Path]:
    installed = []
    for command_path in sorted(commands_dir.glob("*.md")):
        skill_dir = skills_dir / command_path.stem
        skill_dir.mkdir(parents=True, exist_ok=True)
        target = skill_dir / "SKILL.md"
        target.write_text(render_skill_from_command(command_path), encoding="utf-8")
        installed.append(target)
    return installed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commands_dir", type=Path)
    parser.add_argument("skills_dir", type=Path)
    args = parser.parse_args(argv)
    installed = install_codex_skills(args.commands_dir, args.skills_dir)
    print(f"Installed {len(installed)} Codex skill wrappers to {args.skills_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
