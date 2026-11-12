from __future__ import annotations

import sys
import time
import tomllib
from datetime import date, datetime
from typing import NamedTuple, Optional

from . import config, paths
from .spawner import _account_used_pct, _is_num, _read_usage, _to_epoch, _usage_is_fresh

PEND_REASON = "usage-cap"
ERROR_CHARS = 300
WINDOWS = (
    ("five_hour", "5h", "five_hour_pct", "five_hour_resets_at"),
    ("seven_day", "7d", "seven_day_pct", "seven_day_resets_at"),
)
CAP_KEYS = tuple(key for key, _label, _pct_key, _reset_key in WINDOWS)


class CapFileError(ValueError):
    pass


class Entry(NamedTuple):
    account: str
    caps: dict
    until: Optional[float]


class Breach(NamedTuple):
    account: str
    window: str
    pct: float
    cap: float
    lift_at: Optional[float]


class Verdict(NamedTuple):
    breaches: tuple
    error: Optional[str]


class Caps(NamedTuple):
    entries: list
    errors: list


def _live(until: Optional[float], now: float) -> bool:
    return until is None or now < until


def _until(account: str, value) -> float:
    try:
        if isinstance(value, datetime):
            return value.timestamp()
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day).timestamp()
    except (ValueError, OverflowError, OSError) as err:
        raise CapFileError(f"[{account}] until {value} is out of range ({err})") from err
    raise CapFileError(f"[{account}] until must be a TOML date or date-time, got {value!r}")


def _entry(account: str, table, now: float) -> Entry:
    if not isinstance(table, dict):
        raise CapFileError(f"{account!r} must be a table: [{account}] with five_hour, seven_day, until")
    until = _until(account, table["until"]) if "until" in table else None
    if _live(until, now) and account not in config.account_names():
        pool = ", ".join(config.account_names())
        raise CapFileError(f"unknown account {account!r} (the pool resolves to {pool}): fix the table name in "
                           f"{paths.USAGE_CAPS} or add the account to [accounts] pool in dockwright.toml")
    unknown = sorted(set(table) - {*CAP_KEYS, "until"})
    if unknown:
        raise CapFileError(f"[{account}] has unknown keys {unknown}; allowed: five_hour, seven_day, until")
    caps = {}
    for key in CAP_KEYS:
        if key not in table:
            continue
        value = table[key]
        if not (_is_num(value) and 1 <= value <= 100):
            raise CapFileError(f"[{account}] {key} must be a number from 1 to 100, got {value!r}")
        caps[key] = float(value)
    if not caps:
        raise CapFileError(f"[{account}] needs five_hour or seven_day")
    return Entry(account, caps, until)


def load_caps(now: Optional[float] = None) -> Caps:
    now = time.time() if now is None else now
    try:
        with open(paths.USAGE_CAPS, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return Caps([], [])
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        return Caps([], [str(err)])
    entries, errors = [], []
    for account, table in data.items():
        try:
            entries.append(_entry(account, table, now))
        except CapFileError as err:
            errors.append(str(err))
    return Caps(entries, errors)


def _breach(entry: Entry, rec: dict, now: float, key: str, label: str, pct_key: str,
            reset_key: str) -> Optional[Breach]:
    cap = entry.caps.get(key)
    pct = rec.get(pct_key)
    if cap is None or not _is_num(pct) or pct < cap:
        return None
    resets_at = _to_epoch(rec.get(reset_key))
    if resets_at is not None and now >= resets_at:
        return None
    if resets_at is None and not _usage_is_fresh(rec, now):
        return None
    lifts = [t for t in (entry.until, resets_at) if t is not None]
    return Breach(entry.account, label, float(pct), cap, min(lifts) if lifts else None)


def breaches_of(entry: Entry, now: float) -> list:
    if not _live(entry.until, now):
        return []
    rec = _read_usage(entry.account)
    if rec is None:
        return []
    return [b for b in (_breach(entry, rec, now, *window) for window in WINDOWS) if b is not None]


def evaluate(now: Optional[float] = None) -> Verdict:
    now = time.time() if now is None else now
    caps = load_caps(now)
    error = "; ".join(caps.errors)[:ERROR_CHARS] if caps.errors else None
    return Verdict(tuple(b for entry in caps.entries for b in breaches_of(entry, now)), error)


def lift_at(verdict: Verdict) -> Optional[float]:
    lifts = [b.lift_at for b in verdict.breaches]
    return None if not lifts or None in lifts else max(lifts)


def _num(value) -> str:
    return f"{float(value):g}"


def describe(breach: Breach) -> str:
    return f"{breach.account} {breach.window} {_num(breach.pct)}% ≥ {_num(breach.cap)}%"


def clock(ts: Optional[float], now: float) -> str:
    try:
        moment = None if ts is None else datetime.fromtimestamp(ts)
    except (ValueError, OverflowError, OSError):
        moment = None
    if moment is None:
        return "its window resets"
    if moment.date() == datetime.fromtimestamp(now).date():
        return moment.strftime("%H:%M")
    return moment.strftime("%Y-%m-%d %H:%M")


def refusal(now: Optional[float] = None) -> Optional[dict]:
    now = time.time() if now is None else now
    verdict = evaluate(now)
    if verdict.error is None and not verdict.breaches:
        return None
    reasons = []
    if verdict.breaches:
        reasons.append("usage cap: " + "; ".join(describe(b) for b in verdict.breaches))
    if verdict.error is not None:
        reasons.append(f"usage cap file {paths.USAGE_CAPS} is broken: {verdict.error}")
        hint = f"force does not lift an operator cap; fix {paths.USAGE_CAPS}: waiting does not lift a broken file"
    else:
        hint = f"force does not lift an operator cap; edit {paths.USAGE_CAPS} or wait"
    reason = "; ".join(reasons)
    lift = None if verdict.error is not None else lift_at(verdict)
    payload = {"status": "paused", "pend_reason": PEND_REASON, "reason": reason, "hint": hint}
    for name in config.account_names():
        payload[f"{name}_pct"] = _account_used_pct(name)
    payload["earliest_reset_ts"] = lift
    payload["retry_after_s"] = max(0.0, lift - now) if lift is not None else None
    return payload


def page_fields(verdict: Verdict, now: float) -> dict:
    until = clock(lift_at(verdict), now)
    if verdict.error is not None:
        until = f"{until} and the cap file is fixed"
    return {"breaches": "; ".join(describe(b) for b in verdict.breaches),
            "until": until, "path": str(paths.USAGE_CAPS), "error": verdict.error or ""}


def main(argv: list) -> int:
    if argv == ["--paused"]:
        payload = refusal()
        if payload is None:
            return 1
        print(payload["reason"])
        return 0
    if argv:
        print("usage: dockwright usage-cap [--paused]", file=sys.stderr)
        return 2
    now = time.time()
    caps = load_caps(now)
    for error in caps.errors:
        print(f"usage cap file {paths.USAGE_CAPS} is broken: {error}", file=sys.stderr)
    if not caps.entries and not caps.errors:
        print(f"no usage caps: {paths.USAGE_CAPS} is absent or empty")
        return 0
    for entry in caps.entries:
        rec = _read_usage(entry.account) or {}
        breached = {b.window: b for b in breaches_of(entry, now)}
        until = clock(entry.until, now) if entry.until is not None else "no expiry"
        for key, label, pct_key, _reset_key in WINDOWS:
            if key not in entry.caps:
                continue
            pct = rec.get(pct_key)
            used = f"{_num(pct)}%" if _is_num(pct) else "?"
            if not _live(entry.until, now):
                state = "expired"
            elif label in breached:
                state = f"breached until {clock(breached[label].lift_at, now)}"
            else:
                state = "ok"
            print(f"{entry.account} {label} used {used} cap {_num(entry.caps[key])}% until {until}: {state}")
    return 1 if caps.errors else 0
