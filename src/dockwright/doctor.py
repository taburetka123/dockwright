"""Verify dockwright's environment wiring is canonical: explicit venv-binary path
everywhere (hooks + Claude/Codex MCP), no Homebrew editable duplicate, venv import OK.
`dockwright doctor` is the manager's post-deploy validator and setup.sh's fail-loud gate.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import compose, config, env_install, homebrew_cleanup


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def mcp_command_claude(data: dict, server: str):
    return (data.get("mcpServers", {}).get(server) or {}).get("command")


def mcp_command_codex(data: dict, server: str):
    return (data.get("mcp_servers", {}).get(server) or {}).get("command")


def check_mcp(label: str, command, expected_bin: str) -> Check:
    return Check(f"mcp:{label}", command == expected_bin, f"command={command!r} expected={expected_bin!r}")


def _hook_commands(settings: dict):
    return [h["command"]
            for blocks in settings.get("hooks", {}).values()
            for b in blocks for h in b.get("hooks", []) if "command" in h]


def check_hooks_abspath(settings: dict, expected_bin: str, label: str) -> Check:
    bad = [c for c in _hook_commands(settings)
           if env_install.orch_subcommand(c) is not None and expected_bin not in c]
    return Check(f"hooks:{label}", not bad,
                 f"non-abs dockwright hooks: {bad}" if bad else "all dockwright hooks use abspath")


def check_no_brew_editable(brew_prefix, dist_name: str) -> Check:
    found = homebrew_cleanup.find_brew_editable(Path(brew_prefix), dist_name)
    return Check("no-brew-editable", not found,
                 f"brew editable: {[str(e.site_packages) for e in found]}" if found else "none")


def check_venv_import(orch_bin: str, run=subprocess.run) -> Check:
    py = Path(orch_bin).parent / "python"
    try:
        r = run([str(py), "-c", "import dockwright"], capture_output=True, check=False)
    except OSError as e:
        return Check("venv-import", False, f"could not run {py}: {e}")
    return Check("venv-import", getattr(r, "returncode", 1) == 0,
                 f"{py} import rc={getattr(r, 'returncode', '?')}")


def check_config() -> Check:
    from . import config
    err = config.load_error()
    path = config.config_path()
    if err:
        return Check("config:dockwright", False, f"unparseable {path}: {err}")
    return Check("config:dockwright", True,
                 str(path) if path else "no config file (defaults)")


def check_compose_fresh(core_dir, out_dir, overlay_dir=None) -> Check:
    """Deployed agent files match a recompose of core+overlay+vars.

    Runs only when the caller passes --compose-out-dir (setup.sh does) so an
    ad-hoc flagless doctor stays hermetic. Deployed agents without a stamp =
    a legacy pre-compose deploy — fail loud, the fix is one setup.sh run.
    """
    from . import compose, config
    out = Path(out_dir)
    deployed = sorted(out.glob("*.md")) if out.is_dir() else []
    if not deployed:
        return Check("compose:fresh", True, "nothing deployed")
    if not (out / compose.STAMP_NAME).is_file():
        return Check("compose:fresh", False,
                     f"deployed agents in {out} lack a compose stamp "
                     f"(legacy pre-compose deploy) — rerun setup.sh")
    overlay = Path(overlay_dir) if overlay_dir else config.overlay_dir()
    try:
        ok, problems = compose.check_agents(Path(core_dir), out, overlay,
                                            config.agent_vars())
    except compose.ComposeError as e:
        return Check("compose:fresh", False, f"compose failed: {e}")
    return Check("compose:fresh", ok,
                 "; ".join(problems) if problems else "deployed agents match recompose")


def _default_orch_bin() -> str:
    """The console script beside the running interpreter — identical to the
    $DOCKWRIGHT_BIN setup.sh passes, so a bare `dockwright doctor` (README) and
    the setup.sh invocation verify the same wiring."""
    return str(Path(sys.executable).parent / "dockwright")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Verify canonical dockwright env wiring.")
    p.add_argument("--orch-bin", default=_default_orch_bin())
    p.add_argument("--claude-json", type=Path, default=Path.home() / ".claude.json")
    p.add_argument("--settings", type=Path,
                   default=config.claude_config_home() / "settings.json")
    p.add_argument("--codex-hooks", type=Path,
                   default=Path.home() / ".codex" / "hooks.json")
    p.add_argument("--codex-config", type=Path,
                   default=Path.home() / ".codex" / "config.toml")
    p.add_argument("--brew-prefix", type=Path, default=Path("/opt/homebrew"))
    p.add_argument("--dist-name", default="dockwright")
    p.add_argument("--server-name", default="dockwright")
    p.add_argument("--strict", action="store_true")  # accepted for setup.sh symmetry; all checks already hard
    p.add_argument("--compose-core-dir", type=Path)
    p.add_argument("--compose-out-dir", type=Path)
    p.add_argument("--compose-overlay-dir", type=Path)
    args = p.parse_args(argv)

    checks = [check_venv_import(args.orch_bin),
              check_no_brew_editable(args.brew_prefix, args.dist_name),
              check_config()]

    if args.compose_out_dir:
        core = args.compose_core_dir or compose._default_core_dir()
        checks.append(check_compose_fresh(core, args.compose_out_dir,
                                          args.compose_overlay_dir))

    def _parsed(label, path, loader):
        """Parse an existing config file, or return a FAILING parse Check.

        A file that exists but won't parse must FAIL the fail-loud gate — never
        skip (skipping would let the dependent check pass vacuously on {}).
        """
        try:
            return loader(Path(path).read_text()), None
        except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
            return None, Check(f"parse:{label}", False, f"unparseable {path}: {e}")

    if args.claude_json and Path(args.claude_json).exists():
        data, fail = _parsed("claude", args.claude_json, json.loads)
        checks.append(fail or check_mcp("claude", mcp_command_claude(data, args.server_name), args.orch_bin))
    if args.codex_config and Path(args.codex_config).exists():
        data, fail = _parsed("codex-config", args.codex_config, tomllib.loads)
        checks.append(fail or check_mcp("codex", mcp_command_codex(data, args.server_name), args.orch_bin))
    if args.settings and Path(args.settings).exists():
        data, fail = _parsed("settings", args.settings, json.loads)
        checks.append(fail or check_hooks_abspath(data, args.orch_bin, "claude"))
    if args.codex_hooks and Path(args.codex_hooks).exists():
        data, fail = _parsed("codex-hooks", args.codex_hooks, json.loads)
        checks.append(fail or check_hooks_abspath(data, args.orch_bin, "codex"))

    for c in checks:
        print(f"  [{'PASS' if c.ok else 'FAIL'}] {c.name}: {c.detail}")
    failed = [c for c in checks if not c.ok]
    if failed:
        print(f"doctor: {len(failed)} check(s) FAILED", file=sys.stderr)
        return 1
    print("doctor: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
