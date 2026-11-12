import os
import shlex

TOOL_RESULTS_SEGMENT = "/tool-results/"
SUBSTITUTION_MARKERS = ("$(", "`")


def symlink_tool_results_hint(command: str, config_dir: str, *, realpath=os.path.realpath) -> str | None:
    if any(marker in command for marker in SUBSTITUTION_MARKERS):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    prefix = config_dir.rstrip("/") + "/projects/"
    for token in tokens:
        token = os.path.expanduser(token)
        if not token.startswith(prefix) or TOOL_RESULTS_SEGMENT not in token:
            continue
        real = realpath(token)
        if real != token:
            return real
    return None
