#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _env_positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _env_positive_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _dockwright_config_file() -> Path | None:
    env = os.environ.get("DOCKWRIGHT_CONFIG", "").strip()
    if env:
        p = Path(env).expanduser()
        return p if p.is_file() else None
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    for candidate in (base / "dockwright" / "dockwright.toml",
                      Path.home() / ".claude" / "dockwright.toml"):
        if candidate.is_file():
            return candidate
    return None


def _scan_toml_bool(text: str, section: str, key: str) -> bool | None:
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1].strip()
            continue
        if cur != section or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() != key:
            continue
        v = v.split("#", 1)[0].strip()
        if v == "true":
            return True
        if v == "false":
            return False
        return None
    return None


def gardener_module_enabled() -> bool:
    path = _dockwright_config_file()
    if path is None:
        return True
    try:
        import tomllib
        with open(path, "rb") as fh:
            value = tomllib.load(fh).get("modules", {}).get("gardener")
    except ModuleNotFoundError:
        try:
            value = _scan_toml_bool(path.read_text(), "modules", "gardener")
        except OSError:
            return True
    except Exception:
        return True
    return value if isinstance(value, bool) else True


RESERVED_RETRY_ENV_KEYS = ("SELFFIX_RETRY_ATTEMPT", "HOME", "PATH")
RETRY_ENV_UNPARSED = "[gardener] retry_env present in a shape this reader cannot parse"
RETRY_ENV_NOT_ALONE = ("[gardener] retry_env refused: this reader needs its own line to be the only one "
                       "that mentions retry_env outside whole-line comments")
_TOML_UNICODE_ESCAPE = re.compile(r"\\u([0-9A-Fa-f]{4})|\\U([0-9A-Fa-f]{8})")


def _toml_string_end(s: str, i: int) -> int:
    quote = s[i]
    j = i + 1
    while j < len(s):
        if s[j] == quote:
            return j + 1
        j += 2 if quote == '"' and s[j] == "\\" else 1
    raise ValueError("unterminated string")


def _toml_multiline_after(line: str, ml: str | None) -> str | None:
    i = 0
    while i < len(line):
        if ml is not None:
            if ml == '"""' and line[i] == "\\":
                i += 2
            elif line.startswith(ml, i):
                i += 3
                while i < len(line) and line[i] == ml[0]:
                    i += 1
                ml = None
            else:
                i += 1
        elif line[i] == "#":
            break
        elif line.startswith('"""', i) or line.startswith("'''", i):
            ml = line[i:i + 3]
            i += 3
        elif line[i] in "\"'":
            i = _toml_string_end(line, i)
        else:
            i += 1
    return ml


def _skip_blanks(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t":
        i += 1
    return i


def _toml_one_line_str_list(value: str, what: str) -> list[str]:
    s = value.strip()
    if not s.startswith("["):
        raise ValueError(f"{what} is not a one-line list")
    items: list[str] = []
    i = 1
    while True:
        i = _skip_blanks(s, i)
        if s.startswith("]", i):
            break
        if i >= len(s) or s[i] not in "\"'":
            raise ValueError(f"{what} is not a one-line list of strings")
        end = _toml_string_end(s, i)
        body = s[i + 1:end - 1]
        if s[i] == '"' and "\\" in body:
            raise ValueError(f"{what} uses an escape sequence, which this reader does not support")
        items.append(body)
        i = _skip_blanks(s, end)
        if s.startswith(",", i):
            i += 1
        elif not s.startswith("]", i):
            raise ValueError(f"{what} is not a one-line list of strings")
    rest = s[i + 1:].strip()
    if rest and not rest.startswith("#"):
        raise ValueError(f"{what} has text after the list")
    return items


def _scan_toml_str_list(text: str, section: str, key: str) -> list[str] | None:
    cur = None
    ml = None
    for raw in text.split("\n"):
        if ml is not None:
            ml = _toml_multiline_after(raw, ml)
            continue
        line = raw.strip()
        if line.startswith("["):
            head = line.split("#", 1)[0].rstrip()
            cur = head[1:-1].strip() if head.endswith("]") else None
        elif cur == section:
            k, sep, v = line.partition("=")
            if sep and k.strip() == key:
                return _toml_one_line_str_list(v, f"[{section}] {key}")
        ml = _toml_multiline_after(raw, None)
    return None


def _decode_unicode_escape(m: re.Match) -> str:
    try:
        return chr(int(m.group(1) or m.group(2), 16))
    except ValueError:
        return m.group(0)


def _toml_key_mention_lines(text: str, key: str) -> list[int]:
    mention = re.compile(re.escape(key) + r"[\"']?\s*[=\].]")
    found = []
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw.strip()
        if line.startswith("#"):
            continue
        if mention.search(_TOML_UNICODE_ESCAPE.sub(_decode_unicode_escape, line)):
            found.append(number)
    return found


def _line_list(numbers: list[int]) -> str:
    return ("line " if len(numbers) == 1 else "lines ") + ", ".join(map(str, numbers))


def retry_env() -> tuple[dict[str, str], list[str]]:
    path = _dockwright_config_file()
    if path is None:
        return {}, []
    try:
        try:
            import tomllib
        except ImportError:
            tomllib = None
        if tomllib is not None:
            with open(path, "rb") as fh:
                raw = tomllib.load(fh).get("gardener", {}).get("retry_env")
        else:
            text = path.read_text()
            raw = _scan_toml_str_list(text, "gardener", "retry_env")
            mentions = _toml_key_mention_lines(text, "retry_env")
            if raw is None and mentions:
                return {}, [f"{RETRY_ENV_UNPARSED}, on {_line_list(mentions)}"]
            if raw is not None and len(mentions) > 1:
                return {}, [f"{RETRY_ENV_NOT_ALONE}; mentioned on {_line_list(mentions)}"]
    except Exception as e:
        return {}, [f"[gardener] retry_env unreadable: {e}"]
    if raw is None:
        return {}, []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        return {}, ["[gardener] retry_env must be a list of KEY=VALUE strings"]
    pairs: dict[str, str] = {}
    for item in raw:
        if "\x00" in item:
            return {}, ["[gardener] retry_env entry contains a NUL character"]
        key, sep, value = item.partition("=")
        if not sep or not key:
            return {}, [f"[gardener] retry_env entry without KEY=: {item!r}"]
        if key in RESERVED_RETRY_ENV_KEYS:
            return {}, [f"[gardener] retry_env may not set {key}"]
        pairs[key] = value
    return pairs, []


HOME = Path(os.environ.get("HOME", ""))


def _prefer_new(new: Path, legacy: Path) -> Path:
    if new.exists():
        return new
    if legacy.exists():
        return legacy
    return new


GARDENER_DIR = _prefer_new(HOME / ".claude" / "dockwright" / "gardener", HOME / ".claude" / "gardener")
DIGESTS_DIR = GARDENER_DIR / "digests"
PROPOSALS_DIR = GARDENER_DIR / "proposals"
LEDGER_PATH = GARDENER_DIR / "ledger.jsonl"
MARKER_PATH = GARDENER_DIR / "last-digest"
GATE_LOG_PATH = GARDENER_DIR / "gate.log"
STOP_PATHS = (HOME / ".claude" / "dockwright" / "gardener-stop", HOME / ".claude" / "gardener-stop")
FINDINGS_DIR = _prefer_new(HOME / ".claude" / "dockwright" / "selffix" / "findings", HOME / ".claude" / "selffix-findings")
RUN_LOCK_DIR = HOME / ".claude" / "locks" / "analyst-run.lock"
RUN_SCRIPT = HOME / ".claude" / "scripts" / "gardener-run.sh"
CLOSED_DIR = _prefer_new(HOME / ".claude" / "dockwright", HOME / ".claude" / "orchestrator") / "closed"
SETTINGS_PATH = HOME / ".claude" / "settings.json"
WARN_MARKER_PATH = GARDENER_DIR / ".producer-warn"
RETRY_DIR = _prefer_new(HOME / ".claude" / "dockwright" / "selffix" / "retry", HOME / ".claude" / "selffix-retry")
ORCHESTRATOR_DIR = _prefer_new(HOME / ".claude" / "dockwright", HOME / ".claude" / "orchestrator")
SELFFIX_RUN_SCRIPT = HOME / ".claude" / "scripts" / "selffix-run.sh"
SELFFIX_LOG_PATH = _prefer_new(HOME / ".claude" / "dockwright" / "selffix" / "trigger.log", HOME / ".claude" / "selffix-trigger.log")
SELFFIX_DEBUG_PATHS = (HOME / ".claude" / "dockwright" / "selffix" / "debug", HOME / ".claude" / "selffix-debug")
BRICK_FRESH_SEC = 300
EXPECTED_SESSION_END_HOOKS = (
    ("dockwright session-end", "orchestrator session-end"),
    ("selffix-trigger.sh",),
)

FLOOR_DAYS = _env_positive_float("GARDENER_FLOOR_DAYS", 7.0)
MAX_RUNS_PER_WEEK = _env_positive_int("GARDENER_MAX_RUNS_PER_WEEK", 3)
MIN_RUN_GAP_SEC = _env_positive_int("GARDENER_MIN_RUN_GAP", 6 * 3600)
TRAILING_WINDOW_SEC = 7 * 86400
PRODUCER_STALE_GAP_SEC = _env_positive_int("GARDENER_PRODUCER_STALE_SEC", 48 * 3600)
WARN_NOTIFY_THROTTLE_SEC = 24 * 3600

EXIT_OK = 0
EXIT_REFUSED_STOPPED = 3
EXIT_FORCE_LOCKED = 4


def _marker_mtime() -> float:
    try:
        return MARKER_PATH.stat().st_mtime
    except OSError:
        return 0.0


def count_new_unreviewed(since: float) -> int:
    if not FINDINGS_DIR.is_dir():
        return 0
    count = 0
    for finding in FINDINGS_DIR.glob("*.md"):
        if finding.with_suffix(".reviewed").exists():
            continue
        try:
            if finding.stat().st_mtime > since:
                count += 1
        except OSError:
            continue
    return count


def _run_start_timestamps(lane: str = "digest") -> list[float]:
    if not LEDGER_PATH.is_file():
        return []
    try:
        lines = LEDGER_PATH.read_text().splitlines()
    except OSError:
        return []
    stamps: list[float] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if (event.get("type") or event.get("event")) != "run_start":
            continue
        if (event.get("lane") or "digest") != lane:
            continue
        ts = event.get("ts")
        if isinstance(ts, (int, float)):
            stamps.append(float(ts))
    return stamps


def runs_in_trailing_week(now: float) -> int:
    return sum(1 for ts in _run_start_timestamps() if now - ts < TRAILING_WINDOW_SEC)


def newest_run_start_age(now: float) -> float | None:
    stamps = _run_start_timestamps()
    if not stamps:
        return None
    return now - max(stamps)


def lock_held_by_live_pid() -> bool:
    if not RUN_LOCK_DIR.is_dir():
        return False
    try:
        pid = int((RUN_LOCK_DIR / "pid").read_text().strip())
    except (OSError, ValueError):
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _newest_mtime(directory: Path, pattern: str) -> float:
    try:
        return max((p.stat().st_mtime for p in directory.glob(pattern)), default=0.0)
    except OSError:
        return 0.0


def producer_warnings() -> list[str]:
    warnings: list[str] = []
    try:
        hooks = json.loads(SETTINGS_PATH.read_text()).get("hooks", {})
        session_end_cmds = " | ".join(
            h.get("command") or ""
            for block in hooks.get("SessionEnd", []) for h in block.get("hooks", []))
        for alternatives in EXPECTED_SESSION_END_HOOKS:
            if not any(needle in session_end_cmds for needle in alternatives):
                wanted = " / ".join(repr(n) for n in alternatives)
                warnings.append(f"hooks_missing {wanted} not wired in settings.json SessionEnd")
    except (OSError, ValueError):
        warnings.append(f"hooks_missing {SETTINGS_PATH} unreadable")
    newest_finding = _newest_mtime(FINDINGS_DIR, "*.md")
    newest_closed = _newest_mtime(CLOSED_DIR, "*.json")
    if newest_closed and newest_closed - newest_finding > PRODUCER_STALE_GAP_SEC:
        gap_h = (newest_closed - newest_finding) / 3600
        warnings.append(
            f"producer_stale newest finding lags newest closed session by {gap_h:.0f}h "
            f"(threshold {PRODUCER_STALE_GAP_SEC // 3600}h)")
    return warnings


def _notify(message: str) -> None:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e",
             f'display notification "{message[:200]}" with title "gardener-gate"'],
            capture_output=True, timeout=10)
    except Exception:
        pass


def _warn_producer(now: float) -> None:
    warnings = producer_warnings()
    if not warnings:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with GATE_LOG_PATH.open("a") as f:
        for warning in warnings:
            f.write(f"{stamp}  WARN  {warning}\n")
    try:
        last_notified = WARN_MARKER_PATH.stat().st_mtime
    except OSError:
        last_notified = 0.0
    if now - last_notified > WARN_NOTIFY_THROTTLE_SEC:
        suffix = f" (+{len(warnings) - 1} more)" if len(warnings) > 1 else ""
        _notify(warnings[0] + suffix)
        WARN_MARKER_PATH.touch()


def _log_warn(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        GATE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with GATE_LOG_PATH.open("a") as f:
            f.write(f"{stamp}  WARN  {message}\n")
    except OSError:
        pass


def _selffix_log(verb: str, sid: str, detail: str) -> None:
    if not (any(p.exists() for p in SELFFIX_DEBUG_PATHS) or os.environ.get("SELFFIX_DEBUG") == "1"):
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with SELFFIX_LOG_PATH.open("a") as f:
            f.write(f"{stamp}  {verb}  {sid}  {detail}\n")
    except OSError:
        pass


def limit_bricked(now: float) -> bool:
    try:
        for flag in ORCHESTRATOR_DIR.glob(".manager-limited-*"):
            if now - flag.stat().st_mtime < BRICK_FRESH_SEC:
                return True
    except OSError:
        pass
    return False


def usage_capped() -> bool:
    try:
        return subprocess.run(["dockwright", "usage-cap", "--paused"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def spawn_retry(transcript: str, sid: str) -> None:
    extra, warnings = retry_env()
    for warning in warnings:
        _log_warn(warning)
    subprocess.Popen(
        ["bash", str(SELFFIX_RUN_SCRIPT), transcript, sid],
        env={**os.environ, **extra, "SELFFIX_RETRY_ATTEMPT": "1"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def process_retry_queue(now: float) -> bool:
    if not RETRY_DIR.is_dir():
        return False
    try:
        entries = sorted(RETRY_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return False
    for path in entries:
        try:
            entry = json.loads(path.read_text())
            sid = entry["sid"]
            transcript = str(entry["transcript_path"])
            attempts = int(entry.get("attempts", 0))
        except Exception:
            _selffix_log("retry:dropped", path.stem, "reason=garbage")
            path.unlink(missing_ok=True)
            continue
        if not Path(transcript).is_file():
            _selffix_log("retry:dropped", sid, "reason=transcript-missing")
            path.unlink(missing_ok=True)
            continue
        if attempts >= 1:
            _selffix_log("retry:dropped", sid, "reason=attempts-exhausted")
            path.unlink(missing_ok=True)
            continue
        if limit_bricked(now):
            _selffix_log("retry:deferred", sid, "reason=brick")
            return False
        if usage_capped():
            _selffix_log("retry:deferred", sid, "reason=usage-cap")
            return False
        path.unlink(missing_ok=True)
        try:
            spawn_retry(transcript, sid)
        except (OSError, ValueError) as e:
            _selffix_log("retry:dropped", sid, f"reason=spawn-failed {e}")
            return False
        _selffix_log("retry:spawn", sid, "attempts=1")
        return True
    return False


def decide(now: float, force: bool) -> tuple[str, dict]:
    detail: dict = {"force": force}
    if any(p.exists() for p in STOP_PATHS):
        return ("refused_stopped" if force else "stopped"), detail
    if lock_held_by_live_pid():
        return "locked", detail
    runs = runs_in_trailing_week(now)
    detail["runs_in_week"] = runs
    if not force:
        last_age = newest_run_start_age(now)
        if last_age is not None and last_age < MIN_RUN_GAP_SEC:
            detail["last_run_age_sec"] = int(last_age)
            return "cooldown", detail
        if runs >= MAX_RUNS_PER_WEEK:
            return "cap", detail
    if force:
        return "force", detail
    marker = _marker_mtime()
    fresh = count_new_unreviewed(marker)
    detail["new_unreviewed"] = fresh
    detail["marker_age_days"] = round((now - marker) / 86400, 2) if marker else None
    if marker and (now - marker) < FLOOR_DAYS * 86400:
        return "not_due", detail
    if fresh >= 1:
        return "floor", detail
    return "no_material", detail


def spawn_run(trigger: str) -> None:
    subprocess.Popen(
        ["bash", str(RUN_SCRIPT), "--trigger", trigger],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _log_gate(decision: str, detail: dict, spawned: bool) -> None:
    GATE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = json.dumps(detail, sort_keys=True)
    with GATE_LOG_PATH.open("a") as f:
        f.write(f"{stamp}  {decision}  spawned={spawned}  {payload}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gardener trigger gate (LLM-free).")
    parser.add_argument("--force", action="store_true",
                        help="Bypass the weekly interval, cooldown and cap. Still "
                             "refuses under the stop file and the live run mutex.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the decision without spawning a run.")
    args = parser.parse_args(argv)

    if not gardener_module_enabled():
        print("gardener-gate: module-off ([modules] gardener=false) — no-op")
        return EXIT_OK

    for d in (GARDENER_DIR, DIGESTS_DIR, PROPOSALS_DIR):
        d.mkdir(parents=True, exist_ok=True)

    now = time.time()
    decision, detail = decide(now, force=args.force)
    retry_spawned = False
    if (not args.force and not args.dry_run
            and decision not in ("stopped", "refused_stopped", "locked")):
        try:
            retry_queue = len(list(RETRY_DIR.glob("*.json")))
        except OSError:
            retry_queue = 0
        if retry_queue:
            detail["retry_queue"] = retry_queue
            retry_spawned = process_retry_queue(now)
            detail["retry_spawned"] = retry_spawned
    should_spawn = (decision in ("floor", "force")
                    and not args.dry_run and not retry_spawned)
    if should_spawn:
        spawn_run(decision)
    _log_gate(decision, detail, spawned=should_spawn)
    _warn_producer(now)
    print(f"gardener-gate: {decision} spawned={should_spawn} {json.dumps(detail, sort_keys=True)}")

    if decision == "refused_stopped":
        print(f"gardener-gate: stopped — remove {STOP_PATHS[0]} first", file=sys.stderr)
        return EXIT_REFUSED_STOPPED
    if decision == "locked" and args.force:
        print("gardener-gate: run mutex held by a live process — retry when the "
              "current selffix/gardener run finishes", file=sys.stderr)
        return EXIT_FORCE_LOCKED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
