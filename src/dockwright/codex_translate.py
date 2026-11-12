from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import config

MARKER = "<!-- dockwright:translate-codex — generated; edit the sources and rerun setup.sh -->"
STAMP_NAME = ".dockwright-translate.json"
BACKUP_DIR_NAME = ".dockwright-translate-backup"
SKIP_SKILL_PREFIXES = ("plannotator-",)
IGNORED_DIRS = {"__pycache__"}
IGNORED_FILES = {".DS_Store"}
IGNORED_SUFFIXES = (".pyc",)


def default_codex_dir() -> Path:
    env = os.environ.get("CODEX_HOME", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".codex"


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    lines = text.split("\n")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    meta: dict[str, str] = {}
    i = 1
    while i < end:
        line = lines[i]
        if not line.strip() or line[0] in " \t" or line.lstrip().startswith("#") or ":" not in line:
            i += 1
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if value and value[0] in "|>" and value.lstrip("|>+-") == "":
            joiner = " " if value[0] == ">" else "\n"
            block: list[str] = []
            i += 1
            while i < end and (not lines[i].strip() or lines[i][0] in " \t"):
                block.append(lines[i].strip())
                i += 1
            meta[key] = joiner.join(block).strip()
            continue
        meta[key] = value.strip("\"'")
        i += 1
    return meta, "\n".join(lines[end + 1:])


def render_agents_md(entries: list[tuple[str, Path]]) -> str:
    labels = [label for label, _ in entries]
    out = [
        MARKER,
        "# Rules",
        "",
        f"{len(entries)} rule files follow, each between BEGIN/END markers. A rule opens with a TRIGGER line",
        "naming when it applies: read the TRIGGER, apply the rule when it fires.",
        "",
        "Sources: " + ", ".join(labels),
        "",
    ]
    for label, path in entries:
        body = parse_frontmatter(path.read_text(encoding="utf-8"))[1].rstrip("\n") + "\n"
        out.append(f"<!-- BEGIN {label} -->")
        out.append(body + f"<!-- END {label} -->")
        out.append("")
    return "\n".join(out)


def render_skill_from_command(command_path: Path) -> str:
    meta, body = parse_frontmatter(command_path.read_text(encoding="utf-8"))
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
    lines.extend(["disable-model-invocation: false", "---", "", body.lstrip()])
    return "\n".join(lines).rstrip() + "\n"


def render_agent_toml(agent_path: Path) -> str:
    meta, body = parse_frontmatter(agent_path.read_text(encoding="utf-8"))
    name = meta.get("name") or agent_path.stem
    description = meta.get("description") or ""
    return ("name = " + json.dumps(name, ensure_ascii=False) + "\n"
            "description = " + json.dumps(description, ensure_ascii=False) + "\n"
            "developer_instructions = " + json.dumps(body.lstrip(), ensure_ascii=False) + "\n")


@dataclass
class Desired:
    files: dict[str, bytes | Path] = field(default_factory=dict)
    entries: dict[str, dict] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


@dataclass
class Result:
    notes: list[str]
    counts: dict[str, int]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ignored_file(name: str) -> bool:
    return name in IGNORED_FILES or name.endswith(IGNORED_SUFFIXES)


def _list_files(root: Path, notes: list[str] | None = None, label: str = "") -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        seen.add(os.path.realpath(dirpath))
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in IGNORED_DIRS and os.path.realpath(os.path.join(dirpath, d)) not in seen
        )
        for f in filenames:
            if _ignored_file(f):
                continue
            path = Path(dirpath) / f
            rel = path.relative_to(root).as_posix()
            if not path.is_file():
                if notes is not None:
                    notes.append(f"warning: skipped {label}/{rel}: dangling symlink")
                continue
            out.append(rel)
    return sorted(out)


def _content(item: bytes | Path) -> bytes:
    return item if isinstance(item, bytes) else item.read_bytes()


def _tree_sha256(items: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for rel, content in sorted(items):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _dir_tree_sha256(root: Path, rel_files: list[str]) -> str:
    return _tree_sha256([(rel, (root / rel).read_bytes()) for rel in rel_files])


def _inside_codex_home(rel: str) -> bool:
    norm = os.path.normpath(rel)
    if os.path.isabs(norm) or norm == ".." or norm.startswith(".." + os.sep):
        return False
    return norm != STAMP_NAME and norm.split(os.sep)[0] != BACKUP_DIR_NAME


def _contained(codex_real: str, path: Path) -> bool:
    parent = os.path.realpath(path.parent)
    return parent == codex_real or parent.startswith(codex_real + os.sep)


def _is_yaml_true(value: str) -> bool:
    return value.split("#", 1)[0].strip().strip("\"'").strip().lower() in {"true", "yes", "on", "1"}


def _entry_rel(file_rel: str, entries: dict[str, dict]) -> str:
    prefix = file_rel
    while prefix not in entries and "/" in prefix:
        prefix = prefix.rsplit("/", 1)[0]
    return prefix


def render_stamp(entries: dict[str, dict]) -> str:
    return json.dumps({"version": 1, "entries": entries}, indent=2, sort_keys=True) + "\n"


def load_stamp(codex_dir: Path, notes: list[str]) -> dict[str, dict]:
    path = codex_dir / STAMP_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("stamp is not a table")
        if data.get("version") != 1:
            raise ValueError(f"unsupported stamp version {data.get('version')!r}")
        entries = data["entries"]
        if not isinstance(entries, dict):
            raise ValueError("entries is not a table")
        if not all(isinstance(meta, dict) for meta in entries.values()):
            raise ValueError("an entry is not a table")
        return entries
    except (OSError, ValueError, KeyError, TypeError) as e:
        notes.append(f"warning: unreadable stamp {path} ({e}) - read as empty; existing targets go to backup")
        return {}


def plan(claude_dir: Path) -> Desired:
    claude_dir = Path(claude_dir)
    d = Desired(counts={"rules": 0, "skills": 0, "wrappers": 0, "wrappers_skipped": 0, "agents": 0})

    rules_dir = claude_dir / "rules"
    rules = sorted(rules_dir.glob("*.md")) if rules_dir.is_dir() else []
    if rules:
        text = render_agents_md([(f"rules/{p.name}", p) for p in rules]).encode("utf-8")
        d.files["AGENTS.md"] = text
        d.entries["AGENTS.md"] = {"kind": "agents_md", "sha256": _sha256(text)}
        d.counts["rules"] = len(rules)
    else:
        d.notes.append(f"no rules under {rules_dir} - AGENTS.md not generated")

    skill_names: set[str] = set()
    skills_dir = claude_dir / "skills"
    if skills_dir.is_dir():
        for src in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
            if src.name.startswith(SKIP_SKILL_PREFIXES):
                continue
            skill_md = src / "SKILL.md"
            if not skill_md.is_file():
                d.notes.append(f"warning: skipped skills/{src.name}: no SKILL.md")
                continue
            meta, _ = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
            missing = [k for k in ("name", "description") if not meta.get(k)]
            if missing:
                d.notes.append(f"warning: skills/{src.name}/SKILL.md lacks {', '.join(missing)} - Codex reads both")
            rel_files = _list_files(src, d.notes, f"skills/{src.name}")
            for rel in rel_files:
                d.files[f"skills/{src.name}/{rel}"] = src / rel
            d.entries[f"skills/{src.name}"] = {
                "kind": "copy",
                "files": rel_files,
                "sha256": _dir_tree_sha256(src, rel_files),
            }
            skill_names.add(src.name)
            d.counts["skills"] += 1

    commands_dir = claude_dir / "commands"
    if commands_dir.is_dir():
        for cmd in sorted(commands_dir.glob("*.md")):
            if cmd.stem.startswith(SKIP_SKILL_PREFIXES):
                d.notes.append(f"skipped command wrapper {cmd.stem}: plugin-owned name")
                d.counts["wrappers_skipped"] += 1
                continue
            meta, _ = parse_frontmatter(cmd.read_text(encoding="utf-8"))
            if _is_yaml_true(meta.get("disable-model-invocation", "")):
                d.notes.append(f"skipped command wrapper {cmd.stem}: disable-model-invocation")
                d.counts["wrappers_skipped"] += 1
                continue
            if cmd.stem in skill_names:
                d.notes.append(f"skipped command wrapper {cmd.stem}: a skill of that name exists")
                d.counts["wrappers_skipped"] += 1
                continue
            text = render_skill_from_command(cmd).encode("utf-8")
            d.files[f"skills/{cmd.stem}/SKILL.md"] = text
            d.entries[f"skills/{cmd.stem}"] = {
                "kind": "wrapper",
                "files": ["SKILL.md"],
                "sha256": _tree_sha256([("SKILL.md", text)]),
            }
            d.counts["wrappers"] += 1

    agents_dir = claude_dir / "agents"
    if agents_dir.is_dir():
        for agent in sorted(agents_dir.glob("*.md")):
            text = render_agent_toml(agent).encode("utf-8")
            rel = f"agents/{agent.stem}.toml"
            d.files[rel] = text
            d.entries[rel] = {"kind": "agent", "sha256": _sha256(text)}
            d.counts["agents"] += 1

    for render in sorted((claude_dir / "roles").glob("*/role.codex*.md")):
        suffix = render.name[len("role.codex"):-len(".md")]
        rel = f"roles/{render.parent.name}{suffix}.md"
        data = render.read_bytes()
        d.files[rel] = data
        d.entries[rel] = {"kind": "role", "sha256": _sha256(data)}
    return d


def _disk_matches(target: Path, meta: dict) -> bool:
    kind = meta.get("kind")
    if kind in ("copy", "wrapper"):
        if not target.is_dir():
            return False
        rel_files = _list_files(target)
        if rel_files != meta.get("files"):
            return False
        return _dir_tree_sha256(target, rel_files) == meta.get("sha256")
    if kind in ("agents_md", "agent", "role"):
        return target.is_file() and _sha256(target.read_bytes()) == meta.get("sha256")
    return False


def _remove(target: Path) -> None:
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink()


def _free_backup_path(base: Path) -> Path:
    if not base.exists() and not base.is_symlink():
        return base
    n = 1
    while True:
        candidate = base.with_name(f"{base.name}.{n}")
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
        n += 1


def _archive_markdown_rules(codex_dir: Path, codex_real: str, notes: list[str]) -> int:
    rules = codex_dir / "rules"
    if rules.is_symlink():
        notes.append("warning: rules/ is a symlink - left alone")
        return 0
    if not rules.is_dir():
        return 0
    archive = codex_dir / BACKUP_DIR_NAME / "rules"
    if not _contained(codex_real, archive / "any-rule.md"):
        notes.append(f"warning: {BACKUP_DIR_NAME}/rules resolves outside CODEX_HOME - rules/ left alone")
        return 0
    removed = 0
    for p in sorted(rules.glob("*.md")):
        dest = _free_backup_path(archive / p.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dest))
        removed += 1
    try:
        rules.rmdir()
    except OSError:
        pass
    if removed:
        notes.append(f"archived {removed} markdown rule(s) from rules/ -> "
                     f"{archive.relative_to(codex_dir).as_posix()}")
    return removed


def translate(claude_dir: Path, codex_dir: Path) -> Result:
    claude_dir, codex_dir = Path(claude_dir), Path(codex_dir)
    notes: list[str] = []
    old = load_stamp(codex_dir, notes)
    desired = plan(claude_dir)
    notes.extend(desired.notes)
    codex_dir.mkdir(parents=True, exist_ok=True)
    codex_real = os.path.realpath(codex_dir)

    unreachable: set[str] = set()
    for rel in sorted(set(desired.entries) | {r for r in old if _inside_codex_home(r)}):
        if not _contained(codex_real, codex_dir / rel):
            unreachable.add(rel)
            notes.append(f"warning: skipped {rel}: resolves outside CODEX_HOME")

    owned: set[str] = set()
    for rel, meta in sorted(old.items()):
        if not _inside_codex_home(rel):
            notes.append(f"warning: ignored stamp entry {rel}: outside CODEX_HOME")
            continue
        if rel in unreachable:
            continue
        target = codex_dir / rel
        want = desired.entries.get(rel)
        if want is not None and want.get("kind") == meta.get("kind"):
            owned.add(rel)
            if (target.exists() or target.is_symlink()) and not _disk_matches(target, meta):
                notes.append(f"{rel}: changed since we wrote it; rewriting from source")
            continue
        if not (target.exists() or target.is_symlink()):
            continue
        if _disk_matches(target, meta):
            _remove(target)
            notes.append(f"removed {rel} (source gone)")
        elif want is not None:
            notes.append(f"{rel}: changed since we wrote it; backing it up before rewriting")
        else:
            notes.append(f"left {rel}: changed since we wrote it")

    backup_root = codex_dir / BACKUP_DIR_NAME
    for rel in sorted(desired.entries):
        target = codex_dir / rel
        if rel in owned or rel in unreachable or not (target.exists() or target.is_symlink()):
            continue
        dest = _free_backup_path(backup_root / rel)
        if not _contained(codex_real, dest):
            unreachable.add(rel)
            notes.append(f"warning: skipped {rel}: resolves outside CODEX_HOME")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(dest))
        notes.append(f"backed up {rel} (existing, not ours) -> {dest.relative_to(codex_dir).as_posix()}")

    for rel, meta in desired.entries.items():
        if meta["kind"] in ("copy", "wrapper") and rel not in unreachable:
            target = codex_dir / rel
            if target.exists() or target.is_symlink():
                _remove(target)
            target.mkdir(parents=True)
    for rel, item in desired.files.items():
        if _entry_rel(rel, desired.entries) in unreachable:
            continue
        target = codex_dir / rel
        if not _contained(codex_real, target):
            unreachable.add(_entry_rel(rel, desired.entries))
            notes.append(f"warning: skipped {rel}: resolves outside CODEX_HOME")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(item, bytes):
            target.write_bytes(item)
        else:
            shutil.copy2(item, target)

    removed_rules = _archive_markdown_rules(codex_dir, codex_real, notes)
    stamped = {rel: meta for rel, meta in desired.entries.items() if rel not in unreachable}
    (codex_dir / STAMP_NAME).write_text(render_stamp(stamped), encoding="utf-8")

    counts = dict(desired.counts)
    counts["skills"] = sum(1 for meta in stamped.values() if meta["kind"] == "copy")
    counts["wrappers"] = sum(1 for meta in stamped.values() if meta["kind"] == "wrapper")
    counts["agents"] = sum(1 for meta in stamped.values() if meta["kind"] == "agent")
    counts["rules_removed"] = removed_rules
    counts["agents_md_bytes"] = len(desired.files["AGENTS.md"]) if "AGENTS.md" in stamped else 0
    return Result(notes=notes, counts=counts)


def check(claude_dir: Path, codex_dir: Path) -> tuple[bool, list[str]]:
    claude_dir, codex_dir = Path(claude_dir), Path(codex_dir)
    stamp_path = codex_dir / STAMP_NAME
    if not stamp_path.is_file():
        return False, ["never translated - run setup.sh"]
    notes: list[str] = []
    old = load_stamp(codex_dir, notes)
    desired = plan(claude_dir)
    problems: list[str] = list(notes)
    codex_real = os.path.realpath(codex_dir)
    escaped: set[str] = set()
    for rel in sorted(desired.entries):
        if not _contained(codex_real, codex_dir / rel):
            escaped.add(rel)
            problems.append(f"{rel}: resolves outside CODEX_HOME")
    for rel, item in desired.files.items():
        if _entry_rel(rel, desired.entries) in escaped:
            continue
        target = codex_dir / rel
        if not _contained(codex_real, target):
            problems.append(f"{rel}: resolves outside CODEX_HOME")
            continue
        if not target.is_file():
            problems.append(f"{rel}: not deployed")
            continue
        if target.read_bytes() != _content(item):
            problems.append(f"{rel}: differs")
        if isinstance(item, Path) and (target.stat().st_mode & 0o111) != (item.stat().st_mode & 0o111):
            problems.append(f"{rel}: mode differs")
    for rel, meta in desired.entries.items():
        if rel in escaped:
            continue
        if meta["kind"] in ("copy", "wrapper") and (codex_dir / rel).is_dir():
            for extra in sorted(set(_list_files(codex_dir / rel)) - set(meta["files"])):
                problems.append(f"extra {rel}/{extra}")
    for rel, meta in sorted(old.items()):
        if not _inside_codex_home(rel):
            problems.append(f"warning: ignored stamp entry {rel}: outside CODEX_HOME")
            continue
        if rel in escaped:
            continue
        if not _contained(codex_real, codex_dir / rel):
            problems.append(f"{rel}: resolves outside CODEX_HOME")
            continue
        want = desired.entries.get(rel)
        if want is not None and want.get("kind") == meta.get("kind"):
            continue
        target = codex_dir / rel
        if target.exists() or target.is_symlink():
            problems.append(f"stale owned entry {rel}")
    rules = codex_dir / "rules"
    if rules.is_symlink():
        problems.append("rules/: is a symlink - left alone")
    elif rules.is_dir():
        for p in sorted(rules.glob("*.md")):
            problems.append(f"hand-written rule present: {p.name}")
    if stamp_path.read_text(encoding="utf-8") != render_stamp(desired.entries):
        problems.append("stamp differs")
    return (not problems, problems)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dockwright translate-codex",
        description="Rebuild CODEX_HOME (AGENTS.md, skills/, agents/*.toml, roles/*.md) from the Claude sources; "
                    "removes hand-written rules/*.md; --check diffs without writing.")
    parser.add_argument("--claude-dir", type=Path, default=None,
                        help="Claude config home (default: [paths] claude_config_home, ~/.claude).")
    parser.add_argument("--codex-dir", type=Path, default=None,
                        help="Codex home (default: $CODEX_HOME, else ~/.codex).")
    parser.add_argument("--check", action="store_true",
                        help="Recompute in memory and diff against CODEX_HOME; exit 1 if stale.")
    args = parser.parse_args(argv)
    claude_dir = args.claude_dir or config.claude_config_home()
    codex_dir = args.codex_dir or default_codex_dir()
    if args.check:
        ok, problems = check(claude_dir, codex_dir)
        for p in problems:
            print(f"stale: {p}")
        print("translate-codex check: fresh" if ok
              else "translate-codex check: STALE - rerun setup.sh")
        return 0 if ok else 1
    result = translate(claude_dir, codex_dir)
    for note in result.notes:
        print(note)
    c = result.counts
    print(f"translated: {c['rules']} rules -> AGENTS.md ({c['agents_md_bytes']} bytes), "
          f"{c['skills']} skills, {c['wrappers']} command wrappers ({c['wrappers_skipped']} skipped), "
          f"{c['agents']} agents; removed {c['rules_removed']} markdown rule(s) from rules/ -> {codex_dir}")
    return 0


def render_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dockwright render-agents-md",
        description="Concatenate markdown sources into one AGENTS.md-shaped file, in argument order, "
                    "each labelled by the argument as given.")
    parser.add_argument("--out", type=Path, required=True, help="Output file (overwritten).")
    parser.add_argument("sources", nargs="+", help="Source markdown files, in the order to emit them.")
    args = parser.parse_args(argv)
    entries: list[tuple[str, Path]] = []
    for src in args.sources:
        path = Path(src)
        if not path.is_file():
            print(f"render-agents-md: no such file: {src}", file=sys.stderr)
            return 2
        entries.append((src, path))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_agents_md(entries), encoding="utf-8")
    print(f"Rendered {len(entries)} source(s) to {args.out}")
    return 0
