from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

SEARCH_DEPTH_MIN = 1500
_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("dom", 1, 31), ("month", 1, 12), ("dow", 0, 7))


@dataclass(frozen=True)
class CronSpec:
    minute: frozenset[int]
    hour: frozenset[int]
    dom: frozenset[int]
    month: frozenset[int]
    dow: frozenset[int]
    dom_star: bool
    dow_star: bool


def _int(text: str, name: str) -> int:
    if not text.isdigit():
        raise ValueError(f"cron: {name} field: {text!r} is not a number")
    return int(text)


def _range(part: str, name: str, lo: int, hi: int) -> set[int]:
    body, _, step_text = part.partition("/")
    step = _int(step_text, name) if step_text else 1
    if step < 1:
        raise ValueError(f"cron: {name} field: step must be at least 1")
    if body == "*":
        start, end = lo, hi
    elif "-" in body:
        a, b = body.split("-", 1)
        start, end = _int(a, name), _int(b, name)
    else:
        start = end = _int(body, name)
        if step_text:
            end = hi
    if not (lo <= start <= hi and lo <= end <= hi) or start > end:
        raise ValueError(f"cron: {name} field: {part!r} is outside {lo}-{hi}")
    return set(range(start, end + 1, step))


def parse(expr: str) -> CronSpec:
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron: expected five fields, got {len(parts)}: {expr!r}")
    values = []
    for text, (name, lo, hi) in zip(parts, _FIELDS):
        chosen: set[int] = set()
        for part in text.split(","):
            chosen |= _range(part, name, lo, hi)
        values.append(chosen)
    minute, hour, dom, month, dow = values
    if 7 in dow:
        dow = (dow - {7}) | {0}
    return CronSpec(frozenset(minute), frozenset(hour), frozenset(dom), frozenset(month), frozenset(dow),
                    parts[2] == "*", parts[4] == "*")


def matches(spec: CronSpec, dt: datetime) -> bool:
    if dt.minute not in spec.minute or dt.hour not in spec.hour or dt.month not in spec.month:
        return False
    dom_ok = dt.day in spec.dom
    dow_ok = (dt.isoweekday() % 7) in spec.dow
    if spec.dom_star and spec.dow_star:
        return True
    if spec.dom_star:
        return dow_ok
    if spec.dow_star:
        return dom_ok
    return dom_ok or dow_ok


def latest_slot(spec: CronSpec, after: float, until: float) -> float | None:
    end = datetime.fromtimestamp(until).replace(second=0, microsecond=0)
    floor = datetime.fromtimestamp(after)
    cursor = end
    for _ in range(SEARCH_DEPTH_MIN):
        if cursor <= floor:
            return None
        if matches(spec, cursor):
            return cursor.timestamp()
        cursor -= timedelta(minutes=1)
    return None
