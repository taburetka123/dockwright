#!/usr/bin/env python3
"""Warn-only validator for ~/.claude assets (rules/skills/commands/agents/flows).

Called by the auto-commit Stop hook with --staged (only the files in this
commit — legacy files warn only when touched); --all is the on-demand audit.
ALWAYS exits 0 unless --strict: this tool must never block a commit.
Standalone + stdlib-only (deployed verbatim to ~/.claude/scripts/).
"""
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

# A `Skill: <name>` INVOCATION reference, optionally NAMESPACED `<plugin>:<skill>`.
# The PLUGIN name may itself contain dashes — `two-part-plugin:sub-skill`,
# `multi-word-vendor:some-command` — which is the majority shape of real installed
# plugins (most host plugin names are kebab-case), so the namespace group carries
# the same `[a-z0-9]+(?:-[a-z0-9]+)*` kebab token as the skill stem; a dashed-plugin
# reference resolves and is checked like any other, not silently dropped.
# Deliberately narrow, because the two observed false-positive classes are prose
# and placeholders:
#   "Skill: does the description carry…"  -> single-token candidate, skipped below
#   "Skill: corp-<skill>" / "<skill-name>" -> `-<` cannot satisfy `-[a-z0-9]+`, the
#   `:`-terminated namespace group can never reach its `:`, and the trailing
#   lookahead rejects the truncated stem, so nothing matches at all.
# Known blind spots, accepted deliberately: single-token names (`Skill: init`) are
# indistinguishable from prose and name built-ins that live nowhere on disk; and
# case variants (`Skill: Corp-X`) could not resolve on disk anyway.
# Linearity (no ReDoS): every `-` inside a kebab token is followed by a mandatory
# `[a-z0-9]+`, and the namespace token is pinned to the following `:`, so neither
# nested `[a-z0-9]+(?:-[a-z0-9]+)*` can backtrack across a `-` boundary and the two
# cannot interact multiplicatively — a failing match stays linear in input length
# (measured ~3ms on a 100k+100k-char namespaced payload; see the linearity test).
_SKILL_INVOKE_RE = re.compile(
    r"Skill:[ \t]*[`\"'*]*((?:[a-z0-9]+(?:-[a-z0-9]+)*:)?[a-z0-9]+(?:-[a-z0-9]+)*)(?![a-z0-9<>_:-])")
# A DELIBERATE never-guard ("⚠️ NEVER `Skill: x`") names a skill precisely to forbid
# calling it. Exemption is ADJACENCY, not line-scanning: the prohibition token must sit
# immediately before the reference. Line-scanning would also exempt "never skip
# `Skill: x`" — a real reference — i.e. fail open in the one direction a guard must not.
# The sanctioned form is therefore: put `never` (or ⛔) directly in front of the reference.
# `\b` is load-bearing: without it the ordinary word "wheNEVER" ends in the literal
# `never` and silently exempts every reference that follows it — and "whenever" occurs
# in real assets in exactly the instruction position a reference follows ("Load whenever
# `Skill: x` is invoked"). It also correctly stops exempting "never-guard `Skill: x`".
# U+FE0F (VARIATION SELECTOR-16) is in the trailing class, written as the escape
# `\ufe0f` because the character itself is INVISIBLE in source. The macOS emoji
# picker inserts ⛔ as U+26D4 + U+FE0F, so without it an author who follows this
# guard's own remedy text ("put 'never' or ⛔ immediately before it") is warned at
# anyway, with nothing on screen to explain why their ⛔ differs from the exempt one.
_NEVER_GUARD_RE = re.compile(r"(?:\bnever\b|⛔)[ \t\ufe0f`*\"'()]*$", re.IGNORECASE)
_NEVER_LOOKBACK = 60


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
    # UnicodeDecodeError is a ValueError, NOT an OSError: a non-UTF-8 asset would
    # otherwise escape validate_one and break the warn-only, always-exit-0 contract.
    try:
        with open(os.path.join(repo, rel), encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
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
    # sorted(), not bare set(): str hashing is salted per process, so a bare set
    # reorders these lines between runs. The commit hook captures this stdout whole
    # and shows it to the author, and a diff that reshuffles for no reason reads as
    # the tool having found something new.
    for ref in sorted(set(_REF_RE.findall(text))):
        if any(c in ref for c in _PLACEHOLDER_CHARS):
            continue
        # Resolve ~/.claude/... against --repo, not the live home dir: auditing a
        # worktree/fixture repo must not consult the operator's real ~/.claude
        # (in production repo == ~/.claude, so the behavior is identical there).
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


# Where a NAMESPACED `Skill: <plugin>:<target>` can live under <repo>/plugins/.
#
# INSTALLED is the bar, not present-on-disk: the model can only invoke a plugin the
# host actually installed, and installed_plugins.json records every one of them with
# an installPath of exactly cache/<marketplace>/<plugin>/<version>/. So the cache
# layout is not merely the common case — it is the definition of invocable, which is
# why it is the only pattern here.
#
# Enumeration of every distinct on-disk shape holding a `skills/*/SKILL.md` or
# `commands/*.md`, measured against a live ~/.claude (2026-07-23):
#
#   shape                                                  plugins  installed  matched
#   cache/<mkt>/<plugin>/<version>/<leaf>                     12        12       yes
#   marketplaces/<mkt>/plugins/<plugin>/<leaf>                50         6       no
#   marketplaces/<mkt>/external_plugins/<plugin>/<leaf>        3         0       no
#   marketplaces/<mkt>/{apps/<app>,.agents}/<leaf>             5         0       no
#   marketplaces/<mkt>/<leaf> (mkt dir IS the plugin root)     2         0       no
#
# A marketplace CHECKOUT carries every plugin the marketplace offers, installed or
# not — 50 offered, 6 installed on this host. Matching any marketplace shape would
# therefore report "resolved" for ~44 targets the model cannot invoke: a false
# RESOLVE, the direction a guard must never fail. That reasoning rejected the
# external_plugins / per-app / marketplace-root shapes, and it condemns the nested
# `marketplaces/*/plugins/<plugin>/` shape identically — so that pattern was dropped
# too (it had been kept as a fallback "for a checkout whose cache copy was pruned",
# a case with ZERO real instances: every installed plugin has its cache copy).
#
# The cost is the opposite fail direction, deliberately chosen: a reference to an
# offered-but-uninstalled plugin now warns W-SKILL-MISSING. That warning is TRUE —
# the model cannot invoke that target — and a warning the author can dismiss beats a
# silent "resolved" on a reference that will not work.
_PLUGIN_LEAF_PATTERNS = (
    "plugins/cache/*/{plugin}/*/{leaf}",
)


def _skill_probes(name: str) -> list[str]:
    """Repo-relative paths/globs `Skill: <name>` is looked up in — the same list
    _skill_candidates resolves, reused so W-SKILL-MISSING can say WHERE it looked."""
    if ":" in name:
        plugin, target = name.split(":", 1)
        return [pat.format(plugin=plugin, leaf=leaf)
                for leaf in (f"skills/{target}/SKILL.md", f"commands/{target}.md")
                for pat in _PLUGIN_LEAF_PATTERNS]
    return [f"skills/{name}/SKILL.md", f"commands/{name}.md"]


def _skill_candidates(repo: str, name: str) -> list[str]:
    """Every on-disk target `Skill: <name>` could invoke. The invocable namespace
    is not only skills/*/SKILL.md — command-backed entries are invocable too, and
    `disable-model-invocation` is honored in command frontmatter."""
    if ":" in name:
        hits: list[str] = []
        for probe in _skill_probes(name):
            hits += glob.glob(os.path.join(repo, probe))
        return sorted(set(hits))
    return [os.path.join(repo, p) for p in _skill_probes(name)
            if os.path.isfile(os.path.join(repo, p))]


# YAML 1.1 spells true six ways and a vendor may quote it or trail a comment. The
# value is VENDOR-written — a reinstall changing the serializer is exactly the
# failure this guard exists to catch — so matching one bare literal fails OPEN on
# the next spelling.
# KNOWN EDGE, not handled: a folded/block scalar (`disable-model-invocation: >` with
# `true` on the following line) captures `>` and reads as invocable. _frontmatter is
# a line parser by design (stdlib-only, no YAML dependency), so multi-line scalars
# are out of its reach; no vendor has been observed emitting one for this key.
_DISABLED_VALUES = ("true", "yes", "on", "1", "y")


def _disabling_value(path: str) -> str | None:
    """The `disable-model-invocation` value AS WRITTEN when it forbids invocation,
    else None. Presence is not resolution: a target carrying that key cannot be
    invoked by the model at all. Returning the raw text (not a bool) lets the
    warning quote what the file actually says — the whole reason _DISABLED_VALUES
    has five entries is that the spelling varies, so a message hardcoding `true`
    sends an operator grepping for a string that is not there. An unreadable file
    makes no claim (None) — this validator never invents a failure.
    UnicodeDecodeError is caught alongside OSError: these are vendor-authored files
    under plugins/, and a ValueError escaping here would break the warn-only
    contract."""
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
    """' (looked in <first> +N more)', or '' when there is nothing to name. Never
    indexes blindly: a NAMESPACED reference probes nothing at all if
    _PLUGIN_LEAF_PATTERNS is ever emptied, and an IndexError raised there would
    escape validate_one and break the warn-only contract."""
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
                # "no invocable target", not "no target on disk": an offered-but-
                # uninstalled plugin's file IS on disk, under a marketplace checkout
                # no pattern matches. The old wording would read as plainly false to
                # anyone who went and looked.
                f"W-SKILL-MISSING {rel}: 'Skill: {name}' resolves to no invocable "
                f"target{_probe_summary(_skill_probes(name))} — fix the name or drop "
                f"the reference")
            continue
        # Hits must AGREE. The plugin cache wildcards the version dir, so a stale
        # enabled copy beside the live disabled one would otherwise report clean
        # while the invocation is refused.
        disabled = []
        for hit in hits:
            value = _disabling_value(hit)
            if value is not None:
                disabled.append((os.path.relpath(hit, repo), value))
        disabled.sort()
        if disabled:
            # Quote each file's OWN value: two cached versions can disagree in
            # spelling as well as in verdict.
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
        # A deprecated alias legitimately keeps its retired (unprefixed) name;
        # its own check is W-ALIAS-TARGET below, not W-NAMING.
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
        return []  # not an asset class we validate
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
    """sorted(os.listdir) that yields nothing on an unreadable directory. A bare
    listdir raises PermissionError straight out of main(), which would exit 1 with
    no --strict — the same warn-only breach as an unreadable asset file, and the
    --all audit is the one mode that walks directories it was not handed."""
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
