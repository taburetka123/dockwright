from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from . import config, cron, paths, state

INVALID_REPEAT_SEC = 3600
STATE_NAME = "schedules.json"
DAEMON_SID = "daemon"


def _engine():
    from . import mcp_server
    return mcp_server


def _state_path():
    return paths.DAEMON / STATE_NAME


def _load() -> dict:
    data = state.read_json(_state_path())
    return data if isinstance(data, dict) else {}


def _invalid(now: float, saved: dict) -> list[dict]:
    out = []
    raw = config.load().get("schedules")
    raw = raw if isinstance(raw, dict) else {}
    valid = config.schedules()
    reported = saved.setdefault("_invalid", {})
    for name, entry in raw.items():
        problem = None
        if name not in valid:
            problem = "name must match [A-Za-z0-9_-]{1,32}" if isinstance(entry, dict) else "entry must be a table"
        elif valid[name]["errors"]:
            problem = "; ".join(valid[name]["errors"])
        else:
            try:
                cron.parse(valid[name]["cron"])
            except ValueError as err:
                problem = str(err)
        if problem is None:
            reported.pop(name, None)
            continue
        last = reported.get(name)
        if isinstance(last, (int, float)) and now - last < INVALID_REPEAT_SEC:
            continue
        reported[name] = now
        out.append({"type": "schedule_invalid", "name": name, "reason": problem})
    return out


def _work_state(task_key: str, now: float) -> tuple[str, str | None]:
    from . import waiting_on
    ms = _engine()
    try:
        stamps = {f"{a.get('phase')}.{a.get('name')}": a for a in ms._list_artifacts(task_key)}
        status = ms._waiting_on_status(task_key, ms._load_snapshot_for(task_key),
                                       ms._read_events(paths.artifact_events_path(task_key)), stamps, now,
                                       index=ms._process_index())
    except Exception as err:
        return "unreadable", f"{type(err).__name__}: {str(err)[:200]}"
    fold_ = status.get("fold")
    if fold_ is not None and fold_.closed is not None:
        return "closed", None
    return ("running" if status["waiting_on"] != waiting_on.NONE else "closed"), None


def _slot_key(name: str, slot: float) -> str:
    return f"{name}-{datetime.fromtimestamp(slot):%Y%m%dT%H%M}"


def run(now: float) -> list[dict]:
    if paths.DAEMON_STOP.exists():
        return []
    saved = _load()
    records = _invalid(now, saved)
    for name, sched in config.schedules().items():
        if sched["errors"]:
            continue
        try:
            spec = cron.parse(sched["cron"])
        except ValueError:
            continue
        entry = saved.get(name) if isinstance(saved.get(name), dict) else None
        if entry is None or not isinstance(entry.get("last_slot"), (int, float)):
            saved[name] = {"last_slot": now, "last_task_key": None}
            continue
        if not sched["enabled"]:
            entry["last_slot"] = now
            continue
        slot = cron.latest_slot(spec, entry["last_slot"], now)
        if slot is None:
            continue
        previous = entry.get("last_task_key")
        previous_state, detail = _work_state(previous, now) if isinstance(previous, str) and previous else ("closed", None)
        if previous_state != "closed":
            entry["last_slot"] = slot
            record = {"type": "schedule_skipped", "name": name, "slot": slot, "reason": previous_state,
                      "task_key": previous}
            if detail is not None:
                record["detail"] = detail
            records.append(record)
            continue
        task_key = _slot_key(name, slot)
        cwd = sched["cwd"] or str(paths.ensure_worker_home())
        try:
            asyncio.run(_engine().pipeline_start_impl(DAEMON_SID, task_key, sched["schema"], cwd, sched["ask"],
                                                      domain=sched["domain"] or paths.DEFAULT_DOMAIN))
        except Exception as err:
            entry["last_slot"] = slot
            records.append({"type": "schedule_failed", "name": name, "slot": slot, "reason": str(err)[:200]})
            continue
        entry["last_slot"], entry["last_task_key"] = slot, task_key
        records.append({"type": "schedule_started", "name": name, "slot": slot, "task_key": task_key})
    paths.DAEMON.mkdir(parents=True, exist_ok=True)
    state.write_json_atomic(_state_path(), saved)
    return records


def _next_slot(spec: cron.CronSpec, now: float) -> float | None:
    cursor = datetime.fromtimestamp(now).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(cron.SEARCH_DEPTH_MIN):
        if cron.matches(spec, cursor):
            return cursor.timestamp()
        cursor += timedelta(minutes=1)
    return None


def describe(now: float) -> list[str]:
    saved = _load()
    lines = []
    for name, sched in sorted(config.schedules().items()):
        try:
            spec = cron.parse(sched["cron"]) if not sched["errors"] else None
        except ValueError:
            spec = None
        nxt = _next_slot(spec, now) if spec is not None and sched["enabled"] else None
        when = f"{datetime.fromtimestamp(nxt):%Y-%m-%d %H:%M}" if nxt else "-"
        entry = saved.get(name) if isinstance(saved.get(name), dict) else {}
        last = entry.get("last_task_key") or "-"
        running = _work_state(last, now)[0] if isinstance(last, str) and last != "-" else "-"
        lines.append(f"{name}  {sched['cron']}  next {when}  last {last}  state {running}")
    return lines
