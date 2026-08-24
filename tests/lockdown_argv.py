"""Shared resolver for "can a headless `claude -p` child actually do anything?"

Every lane that pipes untrusted transcript content into a headless child needs the
same question answered about its argv, so the predicates live here rather than in
one lane's test file. Consumers today: `test_headless_lane_lockdown.py` (the
selffix retro + gardener headless lanes) and `test_distill_injection_lockdown.py`
(the manager-memory distill lane — zero-tool, so it passes empty tool/pre-approval/
path sets and an expected-flags shape that omits `--add-dir` entirely). The
distill guard imports these predicates rather than keeping the #245-era private
copy, which had the add-one blind spot `permission_surface_widened` and the
default-deny shape check close.

Semantics measured against CLI 2.1.220:

  * the shipped denylist alone       -> 62-65 tools reachable, incl. ~30 MCP tools
                                        across every configured server;
  * `--tools ""` alone               -> MCP still reachable;
  * strict+empty --mcp-config alone  -> built-ins still reachable;
  * neither closes the PERMISSION layer: `--tools` keeps Bash, and the operator's
    settings (defaultMode "auto" + an allow list carrying `Bash(python3:*)`) load
    unless `--setting-sources ""` is passed;
  * and `--settings '<inline json>'` re-grants that layer WITHOUT being a
    "source" — which is why `permission_surface_widened` exists.
"""
import json

ALL_BUILTINS = "<all-builtins>"

# The CLI documents kebab-case aliases for these two value-taking options
# (`claude --help`, 2.1.220: "--allowedTools, --allowed-tools <tools...>" and
# "--disallowedTools, --disallowed-tools <tools...>" — one option, two
# spellings). A value-level predicate that matches only the camelCase spelling
# is BLIND to the other: measured, appending `--allowed-tools 'Bash(python3:*)'`
# to a lane while leaving `--allowedTools` byte-clean let #248's ACE token ride
# in with every value guard green. Every OTHER long flag we guard (`--tools`,
# `--mcp-config`, `--setting-sources`, `--add-dir`, `--settings`,
# `--permission-mode`, …) rejects its twin spelling with `error: unknown
# option`, so this is a two-option problem, not an open-ended one — pinned by
# `==` in the lockdown suite (test_flag_spellings_is_pinned_to_the_verified_cli_set).
# The DEFAULT-DENY shape check (`unexpected_flags`) already rejects an alias not
# on the expected-flags allowlist; matching both spellings here is the
# independent VALUE belt for a lane that does allow the option.
FLAG_SPELLINGS = {
    "--allowedTools": ("--allowedTools", "--allowed-tools"),
    "--disallowedTools": ("--disallowedTools", "--disallowed-tools"),
}


def occurrences_across_spellings(argv, canonical):
    """Every occurrence of a variadic option under ALL its CLI spellings.

    `canonical` is the camelCase name; `FLAG_SPELLINGS` supplies the alias set
    (falling back to the single spelling for options with no alias). Unions the
    occurrences exactly as the CLI does — a grant is a grant whichever spelling
    carried it.
    """
    out = []
    for spelling in FLAG_SPELLINGS.get(canonical, (canonical,)):
        out.extend(option_occurrences(argv, spelling))
    return out

# Flags that hand permission back to the child. `--settings` injects settings
# INLINE, so it is not a setting SOURCE and `--setting-sources ""` does not
# suppress it: measured on 2.1.220, the shipped argv plus
# `--settings '{"permissions":{"defaultMode":"auto","allow":["Bash(python3:*)"]}}'`
# ran `python3 -c` successfully. `gardener-run.sh` already computes a
# `$SETTINGS_FILE` in scope, so "give the headless lane its guard hook back" is
# the single most likely future edit — and it would re-open arbitrary execution.
PERMISSION_WIDENING_FLAGS = (
    "--settings",
    "--permission-mode",
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--permission-prompt-tool",
    # Proven live: a session plugin can ship a PreToolUse hook returning
    # `permissionDecision: allow`, and `--setting-sources ""` drops settings
    # FILES, not plugin hooks. Listed for diagnosis; the real guard is
    # `unexpected_flags`, which catches these whether or not they are named here.
    "--plugin-dir",
    "--plugin-url",
    "--agents",
)

# The ONLY flags either lane is allowed to pass. Default-deny on argv SHAPE: an
# unknown flag fails the guard whether or not anyone predicted it. This is what
# replaces the enumerate-the-dangerous-ones posture that was walked past twice.
EXPECTED_HEADLESS_FLAGS = frozenset({
    "-p",
    "--model",
    "--add-dir",
    "--allowedTools",
    "--tools",
    "--strict-mcp-config",
    "--mcp-config",
    "--setting-sources",
    "--no-session-persistence",
    "--disallowedTools",
})


def option_occurrences(argv, option):
    """Every occurrence of a VARIADIC option, each as its list of values.

    Three bypasses this must survive:
      * APPEND — `--tools "" Bash` is one occurrence with TWO values; reading only
        argv[i+1] scores it "closed" while Bash is live. Re-opening a tool is an
        append, which a delete-one sweep never covers.
      * MULTI-OCCURRENCE — a second `--tools`/`--mcp-config` later in argv must not
        be invisible because the first one looked closed.
      * EQUALS FORM — the CLI also accepts `--tools=WebFetch` / `--mcp-config={…}`
        and UNIONS them with the plain form. Matching only `arg == option` scored
        both closed; a Tier-2 pass proved a fully-green run against scripts that
        handed the child back `mcp__dockwright__*`.
    """
    occurrences = []
    prefix = option + "="
    for i, arg in enumerate(argv):
        if arg.startswith(prefix):
            occurrences.append([arg[len(prefix):]])
            continue
        if arg != option:
            continue
        values = []
        for candidate in argv[i + 1:]:
            if candidate.startswith("--"):
                break
            values.append(candidate)
        occurrences.append(values)
    return occurrences


def resolve_builtin_tools(argv):
    """Built-in tools the child can call, or ALL_BUILTINS when unrestricted.

    Unions across occurrences deliberately: over-reporting the surface is the safe
    direction for a guard. `--allowedTools` is NOT credited — it is a permission
    pre-approval, not an availability switch, so an author swapping `--tools` for
    it must red the guard rather than sail through.
    """
    occurrences = option_occurrences(argv, "--tools")
    if not occurrences:
        return ALL_BUILTINS
    tools = set()
    for values in occurrences:
        if not values:
            return ALL_BUILTINS
        for value in values:
            if value == "default":
                return ALL_BUILTINS
            # The CLI accepts both separators, and the adjacent --allowedTools
            # line uses the space-separated style — an inviting edit.
            tools.update(t for t in value.replace(",", " ").split() if t)
    return tools


def mcp_surface_closed(argv):
    """True only if the `--mcp-config` MCP axis is closed, not merely forbidden.

    Scope, stated precisely so the name is not read as a stronger guarantee than
    it makes: this validates the `--strict-mcp-config` + `--mcp-config` axis — no
    MCP server is declared AND non-declared ones are refused. It does NOT see a
    server attached by a SEPARATE flag: measured (2.1.220), `--chrome` connects
    the `claude-in-chrome` server (22 tools incl. `computer`, `file_upload`)
    straight through `--strict-mcp-config --mcp-config '{}'`, and this returns
    True for that argv. Such flags are caught by the default-deny SHAPE check
    (`unexpected_flags`) — `--chrome` is not on any lane's expected-flags
    allowlist — plus the `==` golden pin that makes adding it there a loud edit;
    they are not this predicate's job.
    """
    if "--strict-mcp-config" not in argv:
        return False
    values = [v for occ in option_occurrences(argv, "--mcp-config") for v in occ]
    if not values:
        return False
    for value in values:
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            # A file path or unparseable payload: cannot vouch for its contents.
            return False
        if not isinstance(parsed, dict):
            # Valid JSON that is not an object ('[]', 'null', '"x"'): `.get` would
            # raise and obscure the reason. Fail closed.
            return False
        if parsed.get("mcpServers"):
            return False
    return True


def settings_isolated(argv):
    """True if the child does NOT load the operator's settings.

    Load-bearing, not cosmetic: `--tools` keeps Bash available, so with
    `~/.claude/settings.json` loaded the child inherits defaultMode "auto" and an
    allow list carrying `Bash(python3:*)` — arbitrary code execution, and a
    live-fleet reach via `tmux`, with MCP fully closed. Only an EMPTY source list
    counts: naming any source re-loads a settings file, and permission arrays
    MERGE across sources, so an inherited allow rule can never be removed — only
    not loaded.
    """
    occurrences = option_occurrences(argv, "--setting-sources")
    if not occurrences:
        return False
    return all(values == [""] for values in occurrences)


def permission_surface_widened(argv):
    """Does argv carry a NAMED flag that hands the permission layer back?

    Kept for its diagnostic value — it names WHICH flag re-opened the surface —
    but it is NOT the guard. A hand-maintained list of flag names is a denylist in
    a different hat, and `~/.claude/rules/drift-guard-tests.md` §ADD-ONE is
    explicit: "If the guarded set is a hand-maintained list, the next entry is
    unguarded by construction." Two sixth flags were duly found and proven live —
    `--plugin-dir` (a session plugin's PreToolUse hook returning
    `permissionDecision: allow`) and, with no new flag at all, one extra token
    inside the existing `--allowedTools`. `unexpected_flags` is the real guard.
    """
    return [flag for flag in PERMISSION_WIDENING_FLAGS
            if flag in argv or any(a.startswith(flag + "=") for a in argv)]


def unexpected_flags(argv, expected):
    """Flags in argv that are NOT on the expected allowlist — default-deny SHAPE.

    The inversion. Every previous version of this guard enumerated what must not
    appear, and each time a flag nobody had thought of walked straight past it
    into live arbitrary code execution. This asks the opposite question: is every
    flag here one we deliberately put here? An unknown flag fails whether or not
    anyone has ever heard of it, so this never needs a sixth entry.

    A value is not a flag: only tokens starting with `-` are considered, and the
    `--flag=value` form is reduced to its name.
    """
    allowed = set(expected)
    found = []
    for arg in argv:
        if not arg.startswith("-"):
            continue
        name = arg.split("=", 1)[0]
        if name not in allowed:
            found.append(name)
    return found


def resolve_allowed_tools(argv):
    """The set of permission grants in `--allowedTools`.

    `--allowedTools` IS a permission grant, so its VALUE needs the same `==`
    treatment `--tools` gets. Measured: adding the single token `Bash(python3:*)`
    to the existing flag restored arbitrary execution while every other assertion
    stayed green — no new flag involved. Values are space-separated within one
    token (`'Bash(jq:*) Read'`), and the option is variadic, so union everything.

    Both CLI spellings are read (`--allowedTools` AND `--allowed-tools`): the
    kebab alias carried the identical ACE token past an earlier camelCase-only
    resolver with every value guard green (see `FLAG_SPELLINGS`).
    """
    tools = set()
    for values in occurrences_across_spellings(argv, "--allowedTools"):
        for value in values:
            tools.update(t for t in value.replace(",", " ").split() if t)
    return tools


def resolve_add_dirs(argv):
    """The set of directories `--add-dir` grants the child.

    The SECOND authority-carrying value in this argv, and it needs the same `==`
    treatment `--allowedTools` gets. A subset check ("contains the transcript
    dir") catches a REPLACEMENT but not an APPEND: measured on the shipped
    selffix argv plus one appended `--add-dir /`, with no bare tool name present,
    the child read `~/.claude/settings.json`, `~/.ssh/config`, and ran
    `grep -c . ~/.claude/settings.json` and `head -c 40 /etc/hosts`. `--add-dir`
    widens the path scope the pre-approved Bash verbs are bounded by, so this is
    not only a Read-tool axis — the whole boundary comes back through it.
    """
    return {v for values in option_occurrences(argv, "--add-dir") for v in values}


def unscoped_read_grants(argv):
    """Bare tool names in `--allowedTools` that grant a tool for ANY path.

    A bare `Read` / `Grep` / `Glob` pre-approves that tool everywhere and
    OVERRIDES `--add-dir` scoping. Measured on the gardener argv: with the bare
    tokens the child read `~/.claude/settings.json`, `~/.claude.json`
    (`oauthAccount`, trust list, MCP approvals) and `~/.ssh/config`; without them
    those are DENIED while every in-scope read still works, because `--add-dir`
    already grants Read/Grep/Glob within scope. The scoped form `Read(<dir>/**)`
    is fine — only the bare name is unscoped.
    """
    return sorted(t for t in resolve_allowed_tools(argv)
                  if t in {"Read", "Grep", "Glob", "Bash", "WebFetch", "WebSearch"})


def child_is_contained(argv, expected_tools, expected_allowed_tools, expected_flags,
                       expected_add_dirs=frozenset()):
    """The whole contract in one predicate, for a lane that needs tools.

    EVERY authority-carrying value is asserted by `==`, never by containment —
    three consecutive review rounds found an ADDITION walking past a subset
    check. A lane whose child needs no tools, no pre-approvals and no granted
    paths (distill: its transcript arrives on stdin) passes empty sets for all
    three and omits `--add-dir` from `expected_flags`, at which point
    `unexpected_flags` rejects an added `--add-dir` outright.
    """
    return (
        mcp_surface_closed(argv)
        and settings_isolated(argv)
        and resolve_builtin_tools(argv) == set(expected_tools)
        and resolve_allowed_tools(argv) == set(expected_allowed_tools)
        and resolve_add_dirs(argv) == set(expected_add_dirs)
        and not unexpected_flags(argv, expected_flags)
        and not unscoped_read_grants(argv)
    )
