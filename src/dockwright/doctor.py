from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path

from . import compose, config, env_install, homebrew_cleanup, paths, state
from .chat_archive import LEDGER_STALE_SEC as CHAT_LEDGER_STALE_SEC, keychain_items


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


def check_plugin_registration(settings: dict, marketplace_name: str = "dockwright",
                              plugin_key: str = "dockwright@dockwright") -> Check:
    enabled = (settings.get("enabledPlugins") or {}).get(plugin_key)
    source = ((settings.get("extraKnownMarketplaces") or {}).get(marketplace_name) or {}).get("source") or {}
    root = source.get("path")
    name = f"plugins:{marketplace_name}"
    fix = "run setup.sh to install and enable the dockwright plugin"
    if enabled is not True:
        return Check(name, False, f"{plugin_key} not enabled in settings — {fix}")
    if not root:
        return Check(name, False, f"marketplace {marketplace_name!r} not declared — {fix}")
    monitors = Path(root) / "plugins" / marketplace_name / "monitors" / "monitors.json"
    try:
        text = monitors.read_text()
    except OSError as e:
        return Check(name, False, f"{monitors} unreadable ({e}) — {fix}")
    if "@@DOCKWRIGHT_BIN@@" in text:
        return Check(name, False, f"{monitors} still carries the @@DOCKWRIGHT_BIN@@ placeholder — {fix}")
    return Check(name, True, f"{plugin_key} enabled, monitors rendered at {monitors}")


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


def check_account_pointer() -> Check:
    try:
        raw = paths.ACCOUNT_ACTIVE.read_text().rstrip("\n")
    except FileNotFoundError:
        return Check("accounts:pointer", True, "account-active absent (pool off by default)")
    except OSError as e:
        return Check("accounts:pointer", False, f"unreadable {paths.ACCOUNT_ACTIVE}: {e}")
    names = list(config.account_names())
    if raw in names:
        return Check("accounts:pointer", True, f"'{raw}' is in the registry {names}")
    return Check("accounts:pointer", False,
                 f"account-active reads '{raw}' but the registry is {names} — the pool "
                 f"is silently OFF; fix: echo <name> > {paths.ACCOUNT_ACTIVE}, or rm it, "
                 f"or add the account to [accounts] pool in dockwright.toml")


def _login_fix_command(name: str, default: str) -> str:
    if name == default:
        return "claude"
    farm = paths.account_config_dir(name)
    return f"CLAUDE_CONFIG_DIR={shlex.quote(str(farm))} claude"


def check_accounts_login(host_claude_json: "Path | None" = None) -> Check:
    default = config.default_account()
    host = host_claude_json if host_claude_json is not None else Path.home() / ".claude.json"
    missing = []
    for account in config.accounts():
        is_default = account.name == default
        marker_file = host if is_default else paths.account_config_dir(account.name) / ".claude.json"
        fix = f"fix: {_login_fix_command(account.name, default)}, then /login"
        try:
            data = json.loads(marker_file.read_text())
            has_marker = isinstance(data, dict) and bool(data.get("oauthAccount"))
        except FileNotFoundError:
            missing.append(f"{account.name} (no {marker_file} — never logged in? — {fix})")
            continue
        except (OSError, ValueError):
            missing.append(f"{account.name} (unreadable {marker_file} — {fix})")
            continue
        if not has_marker:
            reason = ("logged out, or never logged in" if is_default
                      else "either never logged in, or a farm rebuild dropped the marker")
            missing.append(f"{account.name} (no oauthAccount in {marker_file} — {reason} — {fix})")
    if not missing:
        return Check("accounts:login", True,
                     f"all {len(config.accounts())} pool account(s) carry an oauthAccount "
                     f"marker (default '{default}' via {host}) — marker presence only; "
                     f"an expired token still 401s with the marker intact")
    return Check("accounts:login", False,
                 "; ".join(missing) + " (a re-login also refreshes a dropped marker)")


def check_account_holds() -> Check:
    try:
        held = json.loads(paths.ACCOUNT_STATE.read_text()).get("held")
    except FileNotFoundError:
        held = None
    except (OSError, ValueError) as e:
        return Check("accounts:hold", False, f"unreadable {paths.ACCOUNT_STATE}: {e}")
    if not isinstance(held, dict) or not held:
        return Check("accounts:hold", True, "no account on hold")
    rows = []
    for name, mark in sorted(held.items()):
        mark = mark if isinstance(mark, dict) else {}
        rows.append(f"{name} since {_hold_clock(mark.get('at'))} ({mark.get('reason')}, by {mark.get('source')}; "
                    f"spawns, flips and resumes skip it; back with: dockwright account-hold --release {name})")
    return Check("accounts:hold", True, "; ".join(rows))


CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
KEYCHAIN_TIMEOUT_SEC = 10
_UNLOCK_FIX = "unlock the login keychain, then re-run dockwright doctor"


def _read_keychain_item(run, service: str, launch: str) -> "tuple[dict | None, str]":
    try:
        done = run(["security", "find-generic-password", "-s", service, "-a", getpass.getuser(), "-w"],
                   capture_output=True, check=False, timeout=KEYCHAIN_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return None, f"keychain read timed out after {KEYCHAIN_TIMEOUT_SEC} s — fix: {_UNLOCK_FIX}"
    except OSError as err:
        return None, (f"could not run security: {type(err).__name__} — fix: check that the security "
                      f"binary runs, then re-run dockwright doctor")
    if done.returncode == 44:
        return None, f'no keychain item "{service}" — fix: {launch}, then /login'
    if done.returncode != 0:
        return None, f'security exited {done.returncode} reading "{service}" — fix: {_UNLOCK_FIX}'
    try:
        data = json.loads(done.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return None, f'keychain item "{service}" is not JSON — fix: {launch}, then /login'
    return data, ""


def _usable_mcp_token(entry, now_ms: float) -> bool:
    if not isinstance(entry, dict):
        return False
    refresh, access, expires = entry.get("refreshToken"), entry.get("accessToken"), entry.get("expiresAt")
    if isinstance(refresh, str) and refresh:
        return True
    if not (isinstance(access, str) and access):
        return False
    if isinstance(expires, bool) or not isinstance(expires, (int, float)) or expires <= 0:
        return True
    return expires > now_ms


def check_mcp_oauth(run=subprocess.run, which=shutil.which, now=time.time) -> Check:
    servers = config.mcp_oauth_required()
    if not servers:
        return Check("accounts:mcp-oauth", True, "off ([accounts] mcp_oauth_required is empty)")
    if which("security") is None:
        return Check("accounts:mcp-oauth", False,
                     "cannot verify: no security binary on PATH (the macOS keychain reader) "
                     "— empty [accounts] mcp_oauth_required on this platform")
    default = config.default_account()
    now_ms = now() * 1000
    items = keychain_items({"keychain_service": CLAUDE_KEYCHAIN_SERVICE})
    problems, held = [], []
    for account, service in items:
        launch = _login_fix_command(account, default)
        data, reason = _read_keychain_item(run, service, launch)
        if data is None:
            problems.append(f"{account} ({reason})")
            continue
        entries = data.get("mcpOAuth")
        entries = entries if isinstance(entries, dict) else {}
        for server in servers:
            keys = [k for k in entries if isinstance(k, str) and (k == server or k.startswith(f"{server}|"))]
            usable = [k for k in keys if _usable_mcp_token(entries[k], now_ms)]
            if usable:
                held.append(f"{account}: {usable[-1]}")
                continue
            why = f"no usable token in {', '.join(keys)}" if keys else f"no {server} entry in mcpOAuth"
            problems.append(f"{account}: {server} ({why} — fix: {launch} in a directory whose .mcp.json "
                            f"declares {server}, then /mcp → {server} → Authenticate)")
    if problems:
        return Check("accounts:mcp-oauth", False, "; ".join(problems))
    return Check("accounts:mcp-oauth", True,
                 f"all {len(items)} pool account(s) hold a token for {', '.join(servers)} ({', '.join(held)}) "
                 f"— token presence only; a token the server revoked still answers needs-auth")


def _hold_clock(at) -> str:
    if not isinstance(at, (int, float)):
        return "?"
    return datetime.fromtimestamp(at, tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def check_config() -> Check:
    from . import config
    err = config.load_error()
    path = config.config_path()
    if err:
        return Check("config:dockwright", False, f"unparseable {path}: {err}")
    return Check("config:dockwright", True,
                 str(path) if path else "no config file (defaults)")


def check_legacy_roster() -> Check:
    from . import former_rows
    path = former_rows.legacy_path()
    if not path.is_file():
        return Check("config:legacy-roster", True,
                     f"no legacy roster file at {path}; snapshots started before roster-removal cannot start children")
    try:
        with open(path, "rb") as fh:
            tomllib.load(fh)
    except (OSError, ValueError) as e:
        return Check("config:legacy-roster", False, f"unparseable {path}: {e}")
    return Check("config:legacy-roster", True, f"{path}: read only for snapshots started before roster-removal")


def check_manifest_tier(roles_dir=None) -> Check:
    from . import roles
    broken = []
    for name in roles.role_names(roles_dir):
        try:
            roles.load_role(name, roles_dir)
        except roles.RoleError as err:
            broken.append(str(err))
    if broken:
        return Check("roles:tier", False, "; ".join(broken))
    if not roles.IGNORED_TIER:
        return Check("roles:tier", True, "no manifest carries tier")
    return Check("roles:tier", True,
                 "ignored tier key in: " + ", ".join(sorted(str(p) for p in roles.IGNORED_TIER)) + " — delete it")


def check_codex_translation(claude_dir, codex_dir) -> Check:
    from . import codex_translate
    claude_dir, codex_dir = Path(claude_dir), Path(codex_dir)
    if not codex_dir.is_dir():
        return Check("codex:translation", True, f"no CODEX_HOME at {codex_dir}")
    if shutil.which("codex") is None:
        return Check("codex:translation", True, "codex not on PATH — translation not expected")
    if not (codex_dir / codex_translate.STAMP_NAME).is_file():
        return Check("codex:translation", False,
                     f"never translated - run setup.sh (no {codex_translate.STAMP_NAME} in {codex_dir})")
    try:
        ok, problems = codex_translate.check(claude_dir, codex_dir)
    except (OSError, ValueError) as e:
        return Check("codex:translation", False, f"check failed: {e}")
    return Check("codex:translation", ok,
                 "; ".join(problems) if problems else "Codex home matches the Claude sources")


def _layout_for(core_dir, roles_dir, claude_dir):
    from . import roles
    if roles_dir is None:
        return None
    return roles.Layout(Path(roles_dir), roles.core_dirs_for(Path(core_dir)), Path(claude_dir or Path(roles_dir).parent))


def roles_error():
    from . import roles
    return roles.RoleError


def check_compose_fresh(core_dir, out_dir, overlay_dir=None, roles_dir=None, claude_dir=None) -> Check:
    from . import compose, config
    out = Path(out_dir)
    deployed = sorted(out.glob("*.md")) if out.is_dir() else []
    if not deployed:
        return Check("compose:fresh", True, "nothing deployed")
    if not Path(core_dir).is_dir():
        return Check("compose:fresh", True,
                     f"no agent core dir at {core_dir} — nothing to recompose "
                     f"against")
    if not (out / compose.STAMP_NAME).is_file():
        return Check("compose:fresh", False,
                     f"deployed agents in {out} lack a compose stamp (a legacy "
                     f"pre-compose deploy, or dockwright has not deployed "
                     f"agents here yet) — rerun setup.sh")
    overlay = Path(overlay_dir) if overlay_dir else config.overlay_dir()
    try:
        ok, problems = compose.check_agents(Path(core_dir), out, overlay,
                                            config.agent_vars(), roles_dir, _layout_for(core_dir, roles_dir, claude_dir))
    except (compose.ComposeError, roles_error()) as e:
        return Check("compose:fresh", False, f"compose failed: {e}")
    return Check("compose:fresh", ok,
                 "; ".join(problems) if problems else "deployed agents match recompose")


def check_roles_fresh(core_dir, roles_dir, claude_dir) -> Check:
    from . import roles
    core_dir, roles_dir = Path(core_dir), Path(roles_dir)
    layout = roles.Layout(roles_dir, roles.core_dirs_for(core_dir), Path(claude_dir))
    ok, problems = roles.check_roles(layout, {**compose.load_default_vars(core_dir), **config.agent_vars()})
    if ok and not roles.role_names(roles_dir):
        return Check("roles:fresh", True, f"no role manifests in {roles_dir}")
    if ok:
        return Check("roles:fresh", True, f"role renders in {roles_dir} match recompose")
    more = f" (+{len(problems) - 3} more; run `dockwright compose --check`)" if len(problems) > 3 else ""
    shown = sorted(problems, key=roles._is_warning)[:3]
    return Check("roles:fresh", False, f"{len(problems)} problem(s): " + "; ".join(shown) + more)


def check_agent_sizes(core_dir, overlay_dir, vars, ceilings=None, roles_dir=None, claude_dir=None) -> Check:
    if config.load_error() is not None:
        return Check("agents:size", False,
                     f"dockwright.toml does not parse ({config.load_error()}): "
                     f"[ceilings] and [agent_vars] both read as absent, so this "
                     f"check cannot tell an unbudgeted install from a silently "
                     f"disabled budget")
    ceilings = config.agent_ceilings() if ceilings is None else ceilings
    ceilings = {k: v for k, v in ceilings.items() if not k.startswith(("roles/", "profiles/"))}
    if not ceilings:
        return Check("agents:size", True,
                     "no [ceilings] in dockwright.toml — the composed agent "
                     "files are NOT size-budgeted on this install")
    core, overlay = Path(core_dir), Path(overlay_dir)
    if not core.is_dir():
        return Check("agents:size", False,
                     f"cannot measure: no agent core dir at {core} — pass "
                     f"--compose-core-dir, or drop [ceilings]")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        try:
            result = compose.compose_agents(core, td, overlay, vars, roles_dir,
                                            _layout_for(core, roles_dir, claude_dir))
        except (compose.ComposeError, roles_error()) as e:
            return Check("agents:size", False, f"compose failed: {e}")
        if result["warnings"]:
            return Check("agents:size", False,
                         f"compose warnings {result['warnings']} — an unbound "
                         f"var stays literal and renders SHORTER than its "
                         f"expansion, so this would pass on a render nobody "
                         f"deploys")
        stamp = json.loads((td / compose.STAMP_NAME).read_text())
        cfg = config.config_path()
        layers = (f"layers: {len(stamp['overlay'])} drop-in(s) under {overlay}, "
                  f"{len(vars)} [agent_vars] key(s) from "
                  f"{cfg if cfg else 'no dockwright.toml'}")
        problems: list[str] = []
        measured: list[str] = []
        for name in sorted(result["files"]):
            raw = (td / name).read_bytes()
            got = {"bytes": len(raw),
                   "lines": len(raw.decode(errors="replace").splitlines()),
                   "dropins": sum(1 for k in stamp["overlay"]
                                  if k.startswith(f"{Path(name).stem}/")),
                   "agent_vars": len(vars)}
            shape = f"{name} {got['bytes']}B/{got['lines']}L"
            limits = ceilings.get(name)
            if limits is None:
                measured.append(f"{shape} UNBUDGETED")
                continue
            measured.append(shape)
            if not limits:
                problems.append(
                    f"{name}: [ceilings] entry names no positive limit")
                continue
            for k in ("bytes", "lines"):
                if k in limits and got[k] > limits[k]:
                    problems.append(
                        f"{name}: {got[k]} {k} > {limits[k]} — compress a core "
                        f"section, an overlay drop-in or an [agent_vars] "
                        f"value, or raise the number in [ceilings] "
                        f"deliberately")
            where = {"dropins": f"{overlay / Path(name).stem}/",
                     "agent_vars": str(cfg) if cfg else "no dockwright.toml"}
            for k, floor in (("dropins", "min_dropins"),
                             ("agent_vars", "min_agent_vars")):
                if floor in limits and got[k] < limits[floor]:
                    problems.append(
                        f"{name}: {got[k]} {k} < {floor} {limits[floor]} in "
                        f"{where[k]} — a layer this render composes from is "
                        f"short, so every size above is of a file you may not "
                        f"want deployed; if you shrank that layer on purpose, "
                        f"lower {floor} in the same edit")
        for name in sorted(set(ceilings) - set(result["files"])):
            problems.append(
                f"{name}: named in [ceilings] but no agent composes to it")
    detail = f"{'; '.join(measured)}; {layers}"
    if problems:
        return Check("agents:size", False,
                     "; ".join(problems) + ". Measured: " + detail)
    return Check("agents:size", True, detail)


def _default_orch_bin() -> str:
    return str(Path(sys.executable).parent / "dockwright")


def _default_codex_dir() -> Path:
    from . import codex_translate
    return codex_translate.default_codex_dir()


def check_daemon_alive(now: float | None = None) -> Check:
    from . import daemon
    name = "daemon:alive"
    now = time.time() if now is None else now
    plist = daemon.plist_path()
    section = config.load().get("daemon")
    raw = section.get("enabled") if isinstance(section, dict) else None
    if raw is not None and not isinstance(raw, bool):
        return Check(name, False, f"[daemon] enabled = {json.dumps(raw, default=str)} is not a boolean, "
                                  f"so the daemon counts as disabled — fix: write enabled = true or enabled = false")
    if not plist.exists():
        if config.daemon_enabled():
            return Check(name, False, f"[daemon] enabled = true but {plist} is not installed — "
                                      f"fix: run `dockwright daemon sync`")
        return Check(name, True, "not installed ([daemon] enabled = false)")
    if config.daemon_enabled() and not daemon.wake_plist_path().exists():
        return Check(name, False, f"[daemon] enabled = true but {daemon.wake_plist_path()} (daemon-wake) is not "
                                  f"installed — fix: run `dockwright daemon sync`")
    if paths.DAEMON_STOP.exists():
        return Check(name, True, f"stopped by {paths.DAEMON_STOP}")
    fix = (f"fix: run `dockwright daemon status`; if it shows loaded: false, run `dockwright daemon sync`, "
           f"else read {paths.DAEMON / 'launchd-err.log'} and run `dockwright daemon tick` by hand to see the failure")
    beat = state.read_json(paths.DAEMON / "heartbeat.json")
    last = beat.get("ts") if isinstance(beat, dict) else None
    try:
        installed = plist.stat().st_mtime
    except OSError:
        installed = now
    if not isinstance(last, (int, float)) or last < installed:
        age = now - installed
        if age < daemon.HEARTBEAT_STALE_SEC:
            return Check(name, True, f"installed {age:.0f}s ago; no pass yet")
        return Check(name, False, f"no pass {age:.0f}s after install — {fix}")
    age = now - last
    detail = f"last pass {age:.0f}s ago, {beat.get('errors') or 0} scan error(s) in it"
    if age > daemon.HEARTBEAT_STALE_SEC:
        return Check(name, False, f"{detail} — {fix}")
    return Check(name, True, detail)


TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN")
DIRENV_VARS = ("DIRENV_DIFF", "DIRENV_DIR", "DIRENV_FILE", "DIRENV_WATCHES")
_ENV_PAIR_RE = re.compile(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=(.*?)(?=\s[A-Za-z_][A-Za-z0-9_]*=|$)")


def _tmux_socket() -> str:
    return os.environ.get("DOCKWRIGHT_TMUX_SOCKET") or os.environ.get("CLAUDE_ORCH_TMUX_SOCKET") or "dockwright"


def _run_text(argv: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _tmux_global_env() -> dict[str, str] | None:
    out = _run_text(["tmux", "-L", _tmux_socket(), "show-environment", "-g"])
    if out is None or out.returncode != 0:
        return None
    env = {}
    for line in out.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            env[key] = value
    return env


def _tmux_panes() -> list[tuple[str, str, str]]:
    out = _run_text(["tmux", "-L", _tmux_socket(), "list-panes", "-a", "-F",
                     "#{pane_pid}\t#{session_name}:#{window_name}\t#{pane_current_path}"])
    if out is None or out.returncode != 0:
        return []
    panes = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].isdigit():
            panes.append((parts[0], parts[1], parts[2]))
    return panes


def _parse_ps_env(text: str) -> dict[str, str]:
    return dict(_ENV_PAIR_RE.findall(text))


def _process_envs(pids: list[str]) -> dict[str, dict[str, str]] | None:
    if not pids:
        return {}
    if Path("/proc").is_dir():
        envs = {}
        for pid in pids:
            try:
                raw = Path(f"/proc/{pid}/environ").read_bytes().decode("utf-8", errors="replace")
            except OSError:
                continue
            envs[pid] = dict(item.split("=", 1) for item in raw.split("\0") if "=" in item)
        return envs
    commands = _ps_commands(pids)
    lines = _ps_commands(pids, "-E")
    if commands is None or lines is None:
        return None
    envs = {}
    for pid, line in lines.items():
        command = commands.get(pid)
        if command is not None and line.startswith(command):
            envs[pid] = _parse_ps_env(line[len(command):])
    return envs


def _ps_commands(pids: list[str], *flags: str) -> dict[str, str] | None:
    out = _run_text(["ps", *flags, "-ww", "-o", "pid=,command=", "-p", ",".join(pids)])
    if out is None:
        return None
    rows = {}
    for line in out.stdout.splitlines():
        pid, _, rest = line.strip().partition(" ")
        rows[pid] = rest
    return rows


def _own_envrc(env: dict[str, str], cwd: str) -> bool:
    envrc = env.get("DIRENV_FILE")
    if not envrc or not cwd:
        return False
    root = os.path.realpath(os.path.dirname(envrc))
    here = os.path.realpath(cwd)
    return here == root or here.startswith(root + os.sep)


def _leading_copies(path: str, prefix: str) -> tuple[int, str]:
    copies = 0
    while (path + ":").startswith(prefix + ":"):
        copies, path = copies + 1, path[len(prefix) + 1:]
    return copies, path


def _tmux_path_prepend(core_dir=None) -> str:
    merged = {**compose.load_default_vars(core_dir or compose._default_core_dir()), **config.agent_vars()}
    return merged.get("tmux_path_prepend", "").replace(compose.HOME_TOKEN, str(Path.home()))


def check_tmux_env(core_dir=None) -> Check:
    name = "tmux:env"
    live = _tmux_global_env()
    if live is None:
        return Check(name, True, "tmux not reachable, nothing to check")
    sock = _tmux_socket()
    problems = []
    inherited = [var for var in (*TOKEN_VARS, *DIRENV_VARS) if live.get(var)]
    if inherited:
        problems.append(f"the global environment carries {', '.join(inherited)}, which every new pane inherits — fix: "
                        + "; ".join(f"tmux -L {sock} set-environment -gu {var}" for var in inherited))
    prepend = _tmux_path_prepend(core_dir)
    path = live.get("PATH")
    if prepend.startswith("/") and path is not None:
        copies, rest = _leading_copies(path, prepend)
        if copies == 0:
            problems.append(f"the global PATH does not start with {prepend} ([agent_vars] tmux_path_prepend) "
                            f"— fix: tmux -L {sock} set-environment -g PATH {shlex.quote(prepend + ':' + path)}")
        elif copies > 1:
            fixed = f"{prepend}:{rest}" if rest else prepend
            problems.append(f"the global PATH starts with {prepend} {copies} times: either the value holds a "
                            "',', '}', '\"', '$' or glob character, which the conf's tmux format cannot match, so each re-source "
                            "prepends it again, or the shell that started the server already repeated it — fix: use plain "
                            f"directory names, then tmux -L {sock} set-environment -g PATH {shlex.quote(fixed)}")
    panes = _tmux_panes()
    envs = _process_envs([pid for pid, _, _ in panes])
    leaked = []
    unread = 0
    if envs is None:
        problems.append(f"ps failed or timed out, so the environment of {len(panes)} pane(s) was not read "
                        "— fix: check that ps runs, then run the check again")
    else:
        for pid, label, cwd in panes:
            env = envs.get(pid)
            if not env:
                unread += 1
                continue
            held = [var for var in TOKEN_VARS if env.get(var)]
            if held and not _own_envrc(env, cwd):
                leaked.append(f"{label} ({', '.join(held)})")
    if leaked:
        problems.append(f"{len(leaked)} live pane(s) carry a GitHub token that no .envrc of their own directory set: "
                        + ", ".join(leaked) + " — a pane keeps it until its process exits; fix: close and resume that worker, "
                        "or run /recreate-manager for a manager's pane")
    read_note = "no pane read" if envs is None else f"{len(panes) - unread} pane(s) read" + (
        f", {unread} unreadable (macOS hides a platform binary's environment)" if unread else "")
    if problems:
        return Check(name, False, "; ".join([*problems, read_note]))
    detail = ("no GitHub token or direnv state in the global environment and no pane token from outside "
              f"the pane's own directory; {read_note}")
    if prepend.startswith("/") and path is not None:
        detail += f"; PATH starts with {prepend}"
    return Check(name, True, detail)


def check_lanes_mode() -> Check:
    from . import lane_io
    error = lane_io.lanes_mode_error()
    if error is not None:
        return Check("lanes:mode", False, f"dockwright.toml does not parse or names no boolean [lanes] in_manager ({error}): the daemon and "
                                          "the lanes keep the last mode the daemon read — fix: repair the file")
    in_manager = lane_io.lanes_in_manager()
    if not in_manager and paths.DAEMON_STOP.exists() and paths.PAUSE_MANIFEST.exists():
        return Check("lanes:mode", True, f"fleet paused ({paths.PAUSE_MANIFEST}); dockwright resume lifts daemon-stop")
    if not in_manager and paths.DAEMON_STOP.exists():
        return Check("lanes:mode", False, f"lanes run in the daemon ([lanes] in_manager = false) and {paths.DAEMON_STOP} "
                                          f"(daemon-stop) holds it: nothing pages the managers — fix: rm {paths.DAEMON_STOP}")
    if not in_manager and not config.daemon_enabled():
        return Check("lanes:mode", False, "lanes are off ([lanes] in_manager = false) and the daemon is disabled "
                                          "([daemon] enabled = false): nothing pages the managers — fix: set one of them")
    where = "the daemon" if not in_manager else "the manager tabs"
    return Check("lanes:mode", True, f"the four lanes run in {where}")


def check_lanes_env() -> Check:
    from . import lane_io
    table = config.lanes_env()
    if lane_io.lanes_in_manager():
        return Check("lanes:env", True, f"{len(table)} pair(s) in [lanes.env]; the lanes run in the tabs, nothing to compare")
    live = _tmux_global_env()
    if live is None:
        return Check("lanes:env", True, f"{len(table)} pair(s) in [lanes.env]; tmux not reachable, nothing to compare")
    live = {k: v for k, v in live.items() if k.startswith("CLAUDE_ORCH_")}
    missing = sorted(k for k, v in live.items() if table.get(k) != v)
    if missing:
        return Check("lanes:env", False, "tmux global environment carries " + ", ".join(
            f"{k}={live[k]}" for k in missing) + " but [lanes.env] does not match — the daemon will run without it; "
            "fix: copy the pair into [lanes.env] in dockwright.toml and run setup.sh")
    return Check("lanes:env", True, f"{len(table)} pair(s) match the tmux global environment")


def check_gh_logins() -> Check:
    if not config.gh_configured():
        return Check("gh:logins", True, "unset ([gh] absent): the engine runs gh from the work directory")
    binary = config.gh_binary()
    resolved = shutil.which(binary) if not os.path.isabs(binary) else (binary if os.access(binary, os.X_OK) else None)
    if resolved is None:
        return Check("gh:logins", False, f"[gh] binary = {binary} is not an executable gh — fix: point it at the real gh")
    bad = []
    for owner, login in sorted(config.gh_logins().items()):
        try:
            out = subprocess.run([binary, "auth", "token", "-h", "github.com", "-u", login], capture_output=True,
                                 text=True, timeout=10, check=False,
                                 env={k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")})
            ok = out.returncode == 0 and bool((out.stdout or "").strip())
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        if not ok:
            bad.append(f"{owner} = {login}")
    if bad:
        return Check("gh:logins", False, "no gh token for " + ", ".join(bad) + " — fix: gh auth login -h github.com as that login")
    return Check("gh:logins", True, f"{len(config.gh_logins())} login(s) have a token")


def _schema_loads(name: str) -> bool:
    from . import pipeline
    try:
        return not pipeline.load_schema(config.pipeline_schemas_dir() / f"{name}.toml").problems
    except (OSError, ValueError):
        return False


def check_schedules_config() -> Check:
    from . import cron
    raw = config.load().get("schedules")
    raw = raw if isinstance(raw, dict) else {}
    valid = config.schedules()
    problems = []
    for name in sorted(raw):
        if name not in valid:
            problems.append(f"{name}: name must match [A-Za-z0-9_-]{{1,32}}")
            continue
        sched = valid[name]
        problems.extend(f"{name}: {e}" for e in sched["errors"])
        if sched["cron"]:
            try:
                cron.parse(sched["cron"])
            except ValueError as err:
                problems.append(f"{name}: cron {sched['cron']!r}: {err}")
        if sched["cwd"] and not os.path.isdir(sched["cwd"]):
            problems.append(f"{name}: cwd {sched['cwd']} does not exist")
        if sched["schema"] and not _schema_loads(sched["schema"]):
            problems.append(f"{name}: schema {sched['schema']} does not load")
    if problems:
        return Check("schedules:config", False, "; ".join(problems) + " — fix: edit [schedules] in dockwright.toml")
    return Check("schedules:config", True, f"{len(valid)} schedule(s)")


def check_events_config() -> Check:
    from . import event_poll, paths
    problems = []
    for name, rule in sorted(config.events_rules().items()):
        problems.extend(f"{name}: {e}" for e in rule["errors"])
    problems.extend(f"exec {cmd['selector']}: {cmd['error']}" for cmd in config.events_source("exec")["commands"]
                    if cmd["error"])
    if not config.events_enabled():
        orphans = sorted(p.stem for p in paths.EVENTS_SUBS.glob("*.json")) if paths.EVENTS_SUBS.is_dir() else []
        if orphans:
            problems.append(f"EVENTS_SUBS_ORPHANED: {len(orphans)} subscription(s) wait while the layer is off "
                            f"({', '.join(orphans[:5])}) — fix: set [events] enabled = true, or dockwright event "
                            "unsubscribe each")
        if problems:
            return Check("events:config", False, "; ".join(problems))
        return Check("events:config", True, "events: off")
    env_file = config.events_env_file()
    env, reason = event_poll.load_env(env_file)
    if reason is not None:
        problems.append(f"env_file {env_file} is {reason}; create it with mode 0600 and TRACKER_URL, TRACKER_EMAIL, "
                        "TRACKER_API_TOKEN (the tracker source stays idle until then)")
    if problems:
        return Check("events:config", False, "; ".join(problems) + " — fix: edit [events] in dockwright.toml")
    return Check("events:config", True, f"events: on, {len(config.events_rules())} rule(s)")


LEGACY_SCOUT_KEYS = ("scout_tools", "scout_mcp", "scout_model", "scout_timeout_sec")


def check_chat_archive() -> Check:
    from . import paths
    from .daemon import read_jsonl
    cfg = config.chat_archive()
    raw = config._section(config.load(), "chat_archive")
    chat_source = config._section(config._section(config._section(config.load(), "events"), "sources"), "chat")
    problems = [f"[events.sources.chat] {key} is no longer read ([chat_archive] replaces it)"
                for key in LEGACY_SCOUT_KEYS if key in chat_source]
    if raw.get("enabled") is True and cfg["errors"]:
        problems.extend(f"[chat_archive] {e}" for e in cfg["errors"])
    if raw.get("enabled") is True and not cfg["self_user_id"]:
        problems.append("[chat_archive] self_user_id is empty: no chat event is built from the archive")
    if not cfg["enabled"]:
        if problems:
            return Check("chat-archive:config", False, "; ".join(problems) + " — fix: edit dockwright.toml")
        return Check("chat-archive:config", True, "chat-archive: off")
    records = [r for r in read_jsonl(paths.CHAT_LEDGER) if isinstance(r.get("ts"), (int, float))]
    if not records:
        problems.append(f"no ledger at {paths.CHAT_LEDGER}: no pass has run — fix: dockwright chat-archive pass, "
                        "then dockwright chat-archive status")
    else:
        age = time.time() - records[-1]["ts"]
        if age > CHAT_LEDGER_STALE_SEC:
            problems.append(f"ledger {round(age)} s old (over {CHAT_LEDGER_STALE_SEC}) — fix: dockwright chat-archive "
                            f"status; runner = {cfg['runner']}")
        newest = next((r for r in reversed(records) if r.get("calls")), None)
        if newest and newest.get("errors"):
            problems.append(f"last pass with calls: {', '.join(str(e) for e in newest['errors'])} — fix: dockwright "
                            "chat-archive status")
    if problems:
        return Check("chat-archive:config", False, "; ".join(problems))
    detail = f"chat-archive: on, hand = {cfg['hand']}, runner = {cfg['runner']}"
    if not cfg["sent_footer"]:
        detail += "; sent_footer unset: reply takeover and the footer backstop are off"
    return Check("chat-archive:config", True, detail)


def check_daemon_push() -> Check:
    section = config.load().get("daemon")
    section = section if isinstance(section, dict) else {}
    problems = []
    push = section.get("push")
    if push is not None and push not in config.DAEMON_PUSH_CHANNELS:
        problems.append(f"push = {json.dumps(push, default=str)} is not one of {'/'.join(config.DAEMON_PUSH_CHANNELS)}")
    for key in ("push_after_sec", "push_cap_per_hour"):
        raw = section.get(key)
        if raw is not None and (isinstance(raw, bool) or not isinstance(raw, int) or raw < 0):
            problems.append(f"{key} = {json.dumps(raw, default=str)} is not a non-negative integer")
    if problems:
        return Check("daemon:push", False, "; ".join(problems) + " — fix: edit [daemon] in dockwright.toml")
    return Check("daemon:push", True, f"push = {config.daemon_push()}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Verify canonical dockwright env wiring.")
    p.add_argument("--orch-bin", default=_default_orch_bin())
    p.add_argument("--claude-json", type=Path, default=Path.home() / ".claude.json")
    p.add_argument("--host-claude-json", type=Path, default=None)
    p.add_argument("--settings", type=Path,
                   default=config.claude_config_home() / "settings.json")
    p.add_argument("--codex-hooks", type=Path,
                   default=Path.home() / ".codex" / "hooks.json")
    p.add_argument("--codex-config", type=Path,
                   default=Path.home() / ".codex" / "config.toml")
    p.add_argument("--claude-dir", type=Path, default=None,
                   help="Claude config home the Codex translation is checked against (default: [paths] claude_config_home).")
    p.add_argument("--codex-dir", type=Path, default=None,
                   help="Codex home to check (default: $CODEX_HOME, else ~/.codex).")
    p.add_argument("--brew-prefix", type=Path, default=Path("/opt/homebrew"))
    p.add_argument("--dist-name", default="dockwright")
    p.add_argument("--server-name", default="dockwright")
    p.add_argument("--strict", action="store_true")
    p.add_argument("--compose-core-dir", type=Path)
    p.add_argument("--compose-out-dir", type=Path)
    p.add_argument("--compose-overlay-dir", type=Path)
    p.add_argument("--compose-roles-dir", type=Path, default=None)
    args = p.parse_args(argv)

    checks = [check_venv_import(args.orch_bin),
              check_no_brew_editable(args.brew_prefix, args.dist_name),
              check_config(),
              check_legacy_roster(),
              check_manifest_tier(args.compose_roles_dir),
              check_codex_translation(args.claude_dir or config.claude_config_home(),
                                      args.codex_dir or _default_codex_dir()),
              check_account_pointer(),
              check_accounts_login(args.host_claude_json),
              check_mcp_oauth(),
              check_account_holds()]

    core = args.compose_core_dir or compose._default_core_dir()
    roles_dir = args.compose_roles_dir or config.roles_dir()
    claude_dir = args.claude_dir or (Path(args.compose_out_dir).parent if args.compose_out_dir
                                     else config.claude_config_home())
    checks.append(check_compose_fresh(
        core, args.compose_out_dir or (config.claude_config_home() / "agents"),
        args.compose_overlay_dir, roles_dir, claude_dir))
    checks.append(check_agent_sizes(
        core, args.compose_overlay_dir or config.overlay_dir(),
        config.agent_vars(), roles_dir=roles_dir, claude_dir=claude_dir))
    checks.append(check_roles_fresh(core, roles_dir, claude_dir))
    checks.append(check_daemon_alive())
    checks.extend([check_lanes_mode(), check_lanes_env(), check_gh_logins(), check_schedules_config(),
                   check_events_config(), check_chat_archive(), check_daemon_push(), check_tmux_env(core)])

    def _parsed(label, path, loader):
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
        if fail is None:
            checks.append(check_plugin_registration(data))
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
