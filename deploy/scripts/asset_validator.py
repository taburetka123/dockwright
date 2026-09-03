#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import signal
import subprocess
import sys
import time

COMMAND_EXEMPT = {
    "manager", "manager-assign", "manager-close", "manager-reboot", "manager-recycle",
    "manager-resume", "manager-takeover-recovery", "recreate-manager", "tab", "fix",
}
COMMAND_EXEMPT_PREFIXES: tuple[str, ...] = ()
SKILL_EXEMPT: set[str] = set()
NAME_PREFIXES = ("dockwright-",)

_REF_RE = re.compile(r"~/\.claude/[A-Za-z0-9_/.-]+\.md")
_SKILL_REF_RE = re.compile(r"\breferences/[A-Za-z0-9_.-]+")
_ALIAS_RE = re.compile(
    r"DEPRECATED alias(?:[ \t]+(?:for|use|renamed(?:[ \t]+to)?)|[ \t]*[—–-]+[ \t]*(?:for|use|renamed(?:[ \t]+to)?))?[ \t]+[`/]*([a-z0-9][a-z0-9:-]*)",
    re.IGNORECASE,
)
_PLACEHOLDER_CHARS = ("<", ">", "*", "{", "}")

_SKILL_INVOKE_RE = re.compile(
    r"Skill:[ \t]*[`\"'*]*((?:[a-z0-9]+(?:-[a-z0-9]+)*:)?[a-z0-9]+(?:-[a-z0-9]+)*)(?![a-z0-9<>_:-])")
_NEVER_GUARD_RE = re.compile(r"(?:\bnever\b|⛔)[ \t\ufe0f`*\"'()]*$", re.IGNORECASE)
_NEVER_LOOKBACK = 60


def load_config(repo: str) -> dict:
    path = os.environ.get("ASSET_VALIDATOR_CONFIG") or os.path.join(
        repo, "dockwright", "asset-validator.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _str_list(config: dict, key: str, default):
    value = config.get(key)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return default


def _settings(config: dict) -> dict:
    return {
        "name_prefixes": tuple(_str_list(config, "name_prefixes", NAME_PREFIXES)),
        "command_exempt": set(_str_list(config, "command_exempt", COMMAND_EXEMPT)),
        "command_exempt_prefixes": tuple(
            _str_list(config, "command_exempt_prefixes", COMMAND_EXEMPT_PREFIXES)),
        "skill_exempt": set(_str_list(config, "skill_exempt", SKILL_EXEMPT)),
    }


def _read(repo: str, rel: str) -> str | None:
    try:
        with open(os.path.join(repo, rel), encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def _frontmatter(text: str) -> tuple[dict | None, str | None]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, None
    fields: dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return fields, None
        m = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if m:
            fields[m.group(1)] = m.group(2).strip()
    return None, "frontmatter fence never closes"


def _check_refs(repo: str, rel: str, text: str) -> list[str]:
    warnings = []
    for ref in sorted(set(_REF_RE.findall(text))):
        if any(c in ref for c in _PLACEHOLDER_CHARS):
            continue
        target = os.path.join(repo, ref[len("~/.claude/"):]) if ref.startswith("~/.claude/") \
            else os.path.expanduser(ref)
        if not os.path.exists(target):
            warnings.append(
                f"W-REF-MISSING {rel}: {ref} does not exist — repoint it at the "
                f"moved file or delete the reference")
    if rel.startswith("skills/"):
        skill_dir = os.path.join(repo, os.path.dirname(rel))
        for ref in sorted(set(_SKILL_REF_RE.findall(text))):
            if any(c in ref for c in _PLACEHOLDER_CHARS):
                continue
            if not os.path.exists(os.path.join(skill_dir, ref)):
                warnings.append(
                    f"W-REF-MISSING {rel}: {ref} does not exist — add the file "
                    f"under this skill dir or delete the reference")
    return warnings


def _check_alias(repo: str, rel: str, text: str) -> list[str]:
    m = _ALIAS_RE.search(text)
    if not m:
        return []
    target = m.group(1).split(":")[-1]
    candidates = (
        os.path.join(repo, "commands", f"{target}.md"),
        os.path.join(repo, "skills", target, "SKILL.md"),
    )
    if any(os.path.exists(c) for c in candidates):
        return []
    return [f"W-ALIAS-TARGET {rel}: deprecated-alias target '{target}' not found "
            f"(no commands/{target}.md, no skills/{target}/SKILL.md) — name the "
            f"live replacement, or delete this alias file"]


def _base(rel: str) -> str:
    return os.path.splitext(os.path.basename(rel))[0]


def _name_ok(name: str, exempt: set, exempt_prefixes: tuple, name_prefixes: tuple) -> bool:
    return (name.startswith(name_prefixes) or name in exempt
            or any(name.startswith(p) for p in exempt_prefixes))


_PLUGIN_LEAF_PATTERNS = (
    "plugins/cache/*/{plugin}/*/{leaf}",
)


def _skill_probes(name: str) -> list[str]:
    if ":" in name:
        plugin, target = name.split(":", 1)
        return [pat.format(plugin=plugin, leaf=leaf)
                for leaf in (f"skills/{target}/SKILL.md", f"commands/{target}.md")
                for pat in _PLUGIN_LEAF_PATTERNS]
    return [f"skills/{name}/SKILL.md", f"commands/{name}.md"]


def _skill_candidates(repo: str, name: str) -> list[str]:
    if ":" in name:
        hits: list[str] = []
        for probe in _skill_probes(name):
            hits += glob.glob(os.path.join(repo, probe))
        return sorted(set(hits))
    return [os.path.join(repo, p) for p in _skill_probes(name)
            if os.path.isfile(os.path.join(repo, p))]


_DISABLED_VALUES = ("true", "yes", "on", "1", "y")


def _disabling_value(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            fields, _ = _frontmatter(fh.read())
    except (OSError, UnicodeDecodeError):
        return None
    if not fields:
        return None
    raw = str(fields.get("disable-model-invocation", "")).split("#", 1)[0].strip()
    return raw if raw.strip("\"'").lower() in _DISABLED_VALUES else None


def _probe_summary(probes: list[str]) -> str:
    if not probes:
        return ""
    if len(probes) == 1:
        return f" (looked in {probes[0]})"
    return f" (looked in {probes[0]} +{len(probes) - 1} more)"


def _check_skill_refs(repo: str, rel: str, text: str) -> list[str]:
    warnings: list[str] = []
    seen: set[str] = set()
    for m in _SKILL_INVOKE_RE.finditer(text):
        name = m.group(1)
        if "-" not in name and ":" not in name:
            continue
        if _NEVER_GUARD_RE.search(text[max(0, m.start() - _NEVER_LOOKBACK):m.start()]):
            continue
        if name in seen:
            continue
        seen.add(name)
        hits = _skill_candidates(repo, name)
        if not hits:
            warnings.append(
                f"W-SKILL-MISSING {rel}: 'Skill: {name}' resolves to no invocable "
                f"target{_probe_summary(_skill_probes(name))} — fix the name or drop "
                f"the reference")
            continue
        disabled = []
        for hit in hits:
            value = _disabling_value(hit)
            if value is not None:
                disabled.append((os.path.relpath(hit, repo), value))
        disabled.sort()
        if disabled:
            where = ", ".join(f"{value} at {path}" for path, value in disabled)
            warnings.append(
                f"W-SKILL-DISABLED {rel}: 'Skill: {name}' is not model-invocable "
                f"(disable-model-invocation: {where}) — drop the reference; if the "
                f"mention is deliberate, put 'never' or ⛔ immediately before it")
    return warnings


def validate_one(repo: str, rel: str, config: dict | None = None) -> list[str]:
    text = _read(repo, rel)
    if text is None:
        return []
    if config is None:
        config = load_config(repo)
    settings = _settings(config)
    warnings: list[str] = []
    if rel.startswith("rules/") and rel.endswith(".md"):
        head = text.splitlines()[:10]
        if not any(line.startswith("TRIGGER:") or " TRIGGER:" in line for line in head):
            warnings.append(
                f"W-RULE-TRIGGER {rel}: no TRIGGER: line in first 10 lines — add "
                f"'TRIGGER: Load when <situation>' so a reader knows when it applies")
        if not text.lstrip().startswith("# "):
            warnings.append(
                f"W-RULE-TITLE {rel}: does not start with a '# ' title — add "
                f"'# <Rule name>' as the first line")
    elif rel.startswith("skills/") and rel.endswith("SKILL.md"):
        dirname = rel.split("/")[1]
        fields, err = _frontmatter(text)
        if err:
            warnings.append(f"W-FRONTMATTER {rel}: {err} — close it with a '---' line")
        elif fields is None:
            warnings.append(
                f"W-FRONTMATTER {rel}: missing frontmatter — add a '---' block with "
                f"name: and description:")
        else:
            if not fields.get("description"):
                warnings.append(
                    f"W-FRONTMATTER {rel}: missing description — add 'description: "
                    f"<one line saying when to use this>'; without it the skill is "
                    f"never selected")
            if not fields.get("name"):
                warnings.append(
                    f"W-FRONTMATTER {rel}: missing name — add 'name: {dirname}'")
            elif fields["name"] != dirname:
                warnings.append(
                    f"W-NAME-MISMATCH {rel}: name '{fields['name']}' != dir "
                    f"'{dirname}' — set 'name: {dirname}' or rename the directory "
                    f"to '{fields['name']}'")
        if not _name_ok(dirname, settings["skill_exempt"], (), settings["name_prefixes"]):
            warnings.append(
                f"W-NAMING {rel}: skill dir '{dirname}' lacks one of prefixes "
                f"{'/'.join(settings['name_prefixes'])} — rename the dir (and its "
                f"name: field) with one, or list it under skill_exempt in "
                f"dockwright/asset-validator.json")
    elif rel.startswith("commands/") and rel.endswith(".md"):
        name = _base(rel)
        is_alias = _ALIAS_RE.search(text) is not None
        if not is_alias and not _name_ok(
                name, settings["command_exempt"], settings["command_exempt_prefixes"],
                settings["name_prefixes"]):
            warnings.append(
                f"W-NAMING {rel}: command '{name}' lacks one of prefixes "
                f"{'/'.join(settings['name_prefixes'])} and is not exempt — rename "
                f"the file with one, or list '{name}' under command_exempt in "
                f"dockwright/asset-validator.json")
        _, err = _frontmatter(text)
        if err:
            warnings.append(f"W-FRONTMATTER {rel}: {err} — close it with a '---' line")
    elif rel.startswith("agents/") and rel.endswith(".md"):
        fields, err = _frontmatter(text)
        if err:
            warnings.append(f"W-FRONTMATTER {rel}: {err} — close it with a '---' line")
        elif fields is None:
            warnings.append(
                f"W-FRONTMATTER {rel}: missing frontmatter — add a '---' block with "
                f"name: and description:")
        else:
            if not fields.get("description"):
                warnings.append(
                    f"W-FRONTMATTER {rel}: missing description — add 'description: "
                    f"<one line saying when to dispatch this agent>'")
            if fields.get("name") and fields["name"] != _base(rel):
                warnings.append(
                    f"W-NAME-MISMATCH {rel}: name '{fields['name']}' != file "
                    f"'{_base(rel)}' — set 'name: {_base(rel)}' or rename the file "
                    f"to '{fields['name']}.md'")
    elif not (rel.startswith("flows/") and rel.endswith(".md")):
        return []
    warnings += _check_refs(repo, rel, text)
    warnings += _check_skill_refs(repo, rel, text)
    warnings += _check_alias(repo, rel, text)
    return warnings


def validate_files(repo: str, files: list[str], config: dict | None = None) -> list[str]:
    if config is None:
        config = load_config(repo)
    warnings: list[str] = []
    for rel in files:
        warnings += validate_one(repo, rel, config)
    return warnings


def staged_files(repo: str) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", repo, "diff", "--staged", "--name-only"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [f for f in proc.stdout.splitlines() if f.strip()]


def _listdir(path: str) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _all_asset_files(repo: str) -> list[str]:
    rels: list[str] = []
    for sub in ("rules", "commands", "agents", "flows"):
        d = os.path.join(repo, sub)
        if os.path.isdir(d):
            rels += [f"{sub}/{f}" for f in _listdir(d) if f.endswith(".md")]
    skills = os.path.join(repo, "skills")
    if os.path.isdir(skills):
        for name in _listdir(skills):
            if os.path.isfile(os.path.join(skills, name, "SKILL.md")):
                rels.append(f"skills/{name}/SKILL.md")
    return rels


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Warn-only ~/.claude asset validator")
    parser.add_argument("--repo", default=os.path.expanduser("~/.claude"))
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true")
    group.add_argument("--files", nargs="+")
    group.add_argument("--all", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--max-seconds", type=int, default=10)
    args = parser.parse_args(argv)

    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, lambda signum, frame: os._exit(0))
        signal.alarm(args.max_seconds)
    if os.environ.get("ASSET_VALIDATOR_TEST_SLEEP"):
        time.sleep(float(os.environ["ASSET_VALIDATOR_TEST_SLEEP"]))

    if args.files:
        files = args.files
    elif args.all:
        files = _all_asset_files(args.repo)
    else:
        files = staged_files(args.repo)

    warnings = validate_files(args.repo, files, load_config(args.repo))
    if args.json:
        print(json.dumps({"warnings": warnings, "files_checked": len(files)}))
    else:
        for w in warnings:
            print(w)
    if hasattr(signal, "SIGALRM"):
        signal.alarm(0)
    return 1 if (args.strict and warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
