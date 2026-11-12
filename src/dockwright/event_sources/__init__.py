from __future__ import annotations

import importlib
import time as _clock

NAMES = ("tracker", "deploy", "time", "github", "chat", "exec")
CALL_TIMEOUT_SEC = 30
BUDGET_SPENT = "budget_spent"
FAILED = "failed"
NOTES = "notes"
ATTEMPTED = "attempted"
ERROR_CHARS = 200


class Budget(Exception):
    pass


class SelectorFailed(Exception):
    pass


def module(name: str):
    return importlib.import_module(f".{name}", __package__)


def budget_left(until: float | None) -> float | None:
    return None if until is None else max(0.0, until - _clock.time())


def spent(until: float | None) -> bool:
    return until is not None and _clock.time() >= until


def call_timeout(until: float | None) -> float:
    left = budget_left(until)
    return CALL_TIMEOUT_SEC if left is None else min(CALL_TIMEOUT_SEC, left)


def ordered(selectors: list, cursor: dict) -> list:
    first = [s for s in cursor.get(BUDGET_SPENT, []) if s in selectors]
    last = [f[0] for f in cursor.get(FAILED, []) if isinstance(f, list) and f and f[0] in selectors and f[0] not in first]
    return first + [s for s in selectors if s not in first and s not in last] + last


def failure(selector: str, err: Exception | str) -> list:
    return [selector, str(err)[:ERROR_CHARS]]
