import json

ALL_BUILTINS = "<all-builtins>"

FLAG_SPELLINGS = {
    "--allowedTools": ("--allowedTools", "--allowed-tools"),
    "--disallowedTools": ("--disallowedTools", "--disallowed-tools"),
}


def occurrences_across_spellings(argv, canonical):
    out = []
    for spelling in FLAG_SPELLINGS.get(canonical, (canonical,)):
        out.extend(option_occurrences(argv, spelling))
    return out

PERMISSION_WIDENING_FLAGS = (
    "--settings",
    "--permission-mode",
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--permission-prompt-tool",
    "--plugin-dir",
    "--plugin-url",
    "--agents",
)

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
            tools.update(t for t in value.replace(",", " ").split() if t)
    return tools


def mcp_surface_closed(argv):
    if "--strict-mcp-config" not in argv:
        return False
    values = [v for occ in option_occurrences(argv, "--mcp-config") for v in occ]
    if not values:
        return False
    for value in values:
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return False
        if not isinstance(parsed, dict):
            return False
        if parsed.get("mcpServers"):
            return False
    return True


def settings_isolated(argv):
    occurrences = option_occurrences(argv, "--setting-sources")
    if not occurrences:
        return False
    return all(values == [""] for values in occurrences)


def permission_surface_widened(argv):
    return [flag for flag in PERMISSION_WIDENING_FLAGS
            if flag in argv or any(a.startswith(flag + "=") for a in argv)]


def unexpected_flags(argv, expected):
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
    tools = set()
    for values in occurrences_across_spellings(argv, "--allowedTools"):
        for value in values:
            tools.update(t for t in value.replace(",", " ").split() if t)
    return tools


def resolve_add_dirs(argv):
    return {v for values in option_occurrences(argv, "--add-dir") for v in values}


def unscoped_read_grants(argv):
    return sorted(t for t in resolve_allowed_tools(argv)
                  if t in {"Read", "Grep", "Glob", "Bash", "WebFetch", "WebSearch"})


def child_is_contained(argv, expected_tools, expected_allowed_tools, expected_flags,
                       expected_add_dirs=frozenset()):
    return (
        mcp_surface_closed(argv)
        and settings_isolated(argv)
        and resolve_builtin_tools(argv) == set(expected_tools)
        and resolve_allowed_tools(argv) == set(expected_allowed_tools)
        and resolve_add_dirs(argv) == set(expected_add_dirs)
        and not unexpected_flags(argv, expected_flags)
        and not unscoped_read_grants(argv)
    )
