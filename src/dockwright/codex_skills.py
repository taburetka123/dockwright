"""Install Codex skill wrappers from orchestrator slash-command markdown."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def render_skill_from_command(command_path: Path) -> str:
    meta, body = _read_command(command_path)
    name = command_path.stem
    description = meta.get("description") or f"Run /{name}."

    lines = [
        "---",
        f"name: {json.dumps(name)}",
        f"description: {json.dumps(description)}",
        "user-invocable: true",
    ]
    if argument_hint := meta.get("argument-hint"):
        lines.append(f"argument-hint: {json.dumps(argument_hint)}")
    lines.extend([
        "disable-model-invocation: false",
        "---",
        "",
        body.lstrip(),
    ])
    return "\n".join(lines).rstrip() + "\n"


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


def _read_command(command_path: Path) -> tuple[dict[str, str], str]:
    text = command_path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}, text

    lines = text.splitlines(keepends=True)
    end_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break
    if end_index is None:
        return {}, text

    meta = _parse_frontmatter("".join(lines[1:end_index]))
    body = "".join(lines[end_index + 1 :])
    return meta, body


def _parse_frontmatter(raw: str) -> dict[str, str]:
    meta = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip("\"'")
    return meta


if __name__ == "__main__":
    raise SystemExit(main())
