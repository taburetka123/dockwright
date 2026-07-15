#!/usr/bin/env python3
"""Warn-only validator for ~/.claude assets (rules/skills/commands/agents/flows).

Called by the auto-commit Stop hook with --staged (only the files in this
commit — legacy files warn only when touched); --all is the on-demand audit.
ALWAYS exits 0 unless --strict: this tool must never block a commit.
Standalone + stdlib-only (deployed verbatim to ~/.claude/scripts/).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time

# Generic product defaults. Operator-specific conventions (extra name prefixes,
# operator-only exempt command/skill names) live in an optional runtime config —
# see load_config(); they REPLACE the matching default when present.
COMMAND_EXEMPT = {
    "manager", "manager-assign", "manager-close", "manager-reboot", "manager-recycle",
    "manager-resume", "manager-takeover-recovery", "recreate-manager", "tab", "fix",
}
COMMAND_EXEMPT_PREFIXES: tuple[str, ...] = ()
SKILL_EXEMPT: set[str] = set()
NAME_PREFIXES = ("dockwright-",)

_REF_RE = re.compile(r"~/\.claude/[A-Za-z0-9_/.-]+\.md")
_SKILL_REF_RE = re.compile(r"\breferences/[A-Za-z0-9_.-]+")
# Anchor the capture to the connective word ("for"/"use"/"renamed [to]", optionally
# preceded by an em/en-dash) rather than to a delimiter: on live files the target is
# often BARE (no backtick or slash), e.g. "DEPRECATED alias for dockwright-todo (...)".
# Requiring a `/` or backtick delimiter (the prior form) skips a bare target entirely
# and instead captures the first slash-delimited word later in the line (e.g. a path
# fragment in the trailing parenthetical) — a live false positive. The delimiter is
# now optional: consumed if present (`for `dockwright-thing``, `for /dockwright-fix`,
# "— use /dockwright-fix"), skipped if absent (`for dockwright-todo`). The capture
# class is kebab-case only (no `.`/`_`), so a sentence-final dot right after a bare
# target ("... for dockwright-thing. Removed next release.") is never captured.
# Linearity (no ReDoS): whitespace classes are [ \t] only (never \s — the marker and
# target are always same-line in live assets, and \s would span newlines), and in the
# dashed branch the mandatory [—–-]+ separator makes the split deterministic — no two
# adjacent unbounded whitespace quantifiers anywhere.
_ALIAS_RE = re.compile(
    r"DEPRECATED alias(?:[ \t]+(?:for|use|renamed(?:[ \t]+to)?)|[ \t]*[—–-]+[ \t]*(?:for|use|renamed(?:[ \t]+to)?))?[ \t]+[`/]*([a-z0-9][a-z0-9:-]*)",
    re.IGNORECASE,
)
_PLACEHOLDER_CHARS = ("<", ">", "*", "{", "}")


def load_config(repo: str) -> dict:
    """Optional operator overrides for naming conventions.

    Read JSON from $ASSET_VALIDATOR_CONFIG (if set) else
    <repo>/dockwright/asset-validator.json. Fail-soft: a missing file or bad
    JSON yields {} — this validator must never break the commit hook.
    Recognized keys, each REPLACING its generic default when present:
    name_prefixes, command_exempt, command_exempt_prefixes, skill_exempt.
    """
    path = os.environ.get("ASSET_VALIDATOR_CONFIG") or os.path.join(
        repo, "dockwright", "asset-validator.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _str_list(config: dict, key: str, default):
    """A config value is honored only if it is a list of strings; otherwise fall
    back to the key's generic default. Guards against a null (TypeError on
    tuple(None)) or a bare string (silently split into per-character prefixes)."""
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
    except OSError:
        return None


def _frontmatter(text: str) -> tuple[dict | None, str | None]:
    """(fields, error). (None, None) = no frontmatter at all."""
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
    for ref in set(_REF_RE.findall(text)):
        if any(c in ref for c in _PLACEHOLDER_CHARS):
            continue
        # Resolve ~/.claude/... against --repo, not the live home dir: auditing a
        # worktree/fixture repo must not consult the operator's real ~/.claude
        # (in production repo == ~/.claude, so the behavior is identical there).
        target = os.path.join(repo, ref[len("~/.claude/"):]) if ref.startswith("~/.claude/") \
            else os.path.expanduser(ref)
        if not os.path.exists(target):
            warnings.append(f"W-REF-MISSING {rel}: {ref} does not exist")
    if rel.startswith("skills/"):
        skill_dir = os.path.join(repo, os.path.dirname(rel))
        for ref in set(_SKILL_REF_RE.findall(text)):
            if any(c in ref for c in _PLACEHOLDER_CHARS):
                continue
            if not os.path.exists(os.path.join(skill_dir, ref)):
                warnings.append(f"W-REF-MISSING {rel}: {ref} does not exist")
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
    return [f"W-ALIAS-TARGET {rel}: deprecated-alias target '{target}' not found"]


def _base(rel: str) -> str:
    return os.path.splitext(os.path.basename(rel))[0]


def _name_ok(name: str, exempt: set, exempt_prefixes: tuple, name_prefixes: tuple) -> bool:
    return (name.startswith(name_prefixes) or name in exempt
            or any(name.startswith(p) for p in exempt_prefixes))


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
            warnings.append(f"W-RULE-TRIGGER {rel}: no TRIGGER: line in first 10 lines")
        if not text.lstrip().startswith("# "):
            warnings.append(f"W-RULE-TITLE {rel}: does not start with a '# ' title")
    elif rel.startswith("skills/") and rel.endswith("SKILL.md"):
        dirname = rel.split("/")[1]
        fields, err = _frontmatter(text)
        if err:
            warnings.append(f"W-FRONTMATTER {rel}: {err}")
        elif fields is None:
            warnings.append(f"W-FRONTMATTER {rel}: missing frontmatter")
        else:
            if not fields.get("description"):
                warnings.append(f"W-FRONTMATTER {rel}: missing description")
            if not fields.get("name"):
                warnings.append(f"W-FRONTMATTER {rel}: missing name")
            elif fields["name"] != dirname:
                warnings.append(f"W-NAME-MISMATCH {rel}: name '{fields['name']}' != dir '{dirname}'")
        if not _name_ok(dirname, settings["skill_exempt"], (), settings["name_prefixes"]):
            warnings.append(
                f"W-NAMING {rel}: skill dir '{dirname}' lacks one of prefixes "
                f"{'/'.join(settings['name_prefixes'])}")
    elif rel.startswith("commands/") and rel.endswith(".md"):
        name = _base(rel)
        # A deprecated alias legitimately keeps its retired (unprefixed) name;
        # its own check is W-ALIAS-TARGET below, not W-NAMING.
        is_alias = _ALIAS_RE.search(text) is not None
        if not is_alias and not _name_ok(
                name, settings["command_exempt"], settings["command_exempt_prefixes"],
                settings["name_prefixes"]):
            warnings.append(
                f"W-NAMING {rel}: command '{name}' lacks one of prefixes "
                f"{'/'.join(settings['name_prefixes'])} and is not exempt")
        _, err = _frontmatter(text)
        if err:
            warnings.append(f"W-FRONTMATTER {rel}: {err}")
    elif rel.startswith("agents/") and rel.endswith(".md"):
        fields, err = _frontmatter(text)
        if err:
            warnings.append(f"W-FRONTMATTER {rel}: {err}")
        elif fields is None:
            warnings.append(f"W-FRONTMATTER {rel}: missing frontmatter")
        else:
            if not fields.get("description"):
                warnings.append(f"W-FRONTMATTER {rel}: missing description")
            if fields.get("name") and fields["name"] != _base(rel):
                warnings.append(f"W-NAME-MISMATCH {rel}: name '{fields['name']}' != file '{_base(rel)}'")
    elif not (rel.startswith("flows/") and rel.endswith(".md")):
        return []  # not an asset class we validate
    warnings += _check_refs(repo, rel, text)
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


def _all_asset_files(repo: str) -> list[str]:
    rels: list[str] = []
    for sub in ("rules", "commands", "agents", "flows"):
        d = os.path.join(repo, sub)
        if os.path.isdir(d):
            rels += [f"{sub}/{f}" for f in sorted(os.listdir(d)) if f.endswith(".md")]
    skills = os.path.join(repo, "skills")
    if os.path.isdir(skills):
        for name in sorted(os.listdir(skills)):
            if os.path.isfile(os.path.join(skills, name, "SKILL.md")):
                rels.append(f"skills/{name}/SKILL.md")
    return rels


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Warn-only ~/.claude asset validator")
    parser.add_argument("--repo", default=os.path.expanduser("~/.claude"))
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true")  # the default mode; flag kept for explicit calls
    group.add_argument("--files", nargs="+")
    group.add_argument("--all", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--max-seconds", type=int, default=10)
    args = parser.parse_args(argv)

    # In-process runtime cap, second fail-soft layer: the live commit hook cannot
    # rely on an external `timeout` binary (absent on stock macOS). On expiry,
    # exit 0 with no output — warn-only fail-soft even mid-scan must never block
    # or dirty a commit.
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, lambda signum, frame: os._exit(0))
        signal.alarm(args.max_seconds)
    if os.environ.get("ASSET_VALIDATOR_TEST_SLEEP"):
        # Test-only hook for the timeout regression test.
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
