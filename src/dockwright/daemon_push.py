from __future__ import annotations

from . import board, config, notify, paths, state

LABELS = {"dialog": "permission dialog"}
TITLE = "dockwright"
CAP_WINDOW_SEC = 3600


def state_path():
    return paths.DAEMON / "push-state.json"


def waiting_topics(items: list[dict]) -> dict[str, dict]:
    human = set(board.HUMAN_KINDS.values())
    topics: dict[str, dict] = {}
    for item in items:
        kind = item.get("kind")
        if kind not in human:
            continue
        entry = topics.setdefault(str(item.get("topic") or "?"), {"kinds": [], "asked_at": None})
        if kind not in entry["kinds"]:
            entry["kinds"].append(kind)
        anchor = _stamp(item.get("anchor"))
        if kind == "question" and anchor is not None:
            entry["asked_at"] = anchor if entry["asked_at"] is None else min(entry["asked_at"], anchor)
    return topics


def run(now: float, start: float, items: list[dict], send=None) -> list[dict]:
    if config.daemon_push() == "none":
        return []
    send = send or notify.macos
    after = config.daemon_int("push_after_sec", config.DEFAULT_DAEMON_PUSH_AFTER_SEC)
    cap = config.daemon_int("push_cap_per_hour", config.DEFAULT_DAEMON_PUSH_CAP_PER_HOUR)
    saved = state.read_json(state_path())
    saved = saved if isinstance(saved, dict) else {}
    known = saved.get("topics") if isinstance(saved.get("topics"), dict) else {}
    stamps = saved.get("sent") if isinstance(saved.get("sent"), list) else []
    sent = [t for t in map(_stamp, stamps) if t is not None and now - t < CAP_WINDOW_SEC]
    skipped = saved.get("skipped")
    skipped = skipped if isinstance(skipped, int) and not isinstance(skipped, bool) else 0
    last = _stamp(saved.get("pass_at"))
    topics: dict[str, dict] = {}
    due = []
    for topic, info in waiting_topics(items).items():
        prior = known.get(topic) if isinstance(known.get(topic), dict) else {}
        since, notified = _wait(prior, info["asked_at"], last, now, start)
        topics[topic] = {"since": since, "notified": notified}
        if not notified and now - since >= after:
            due.append((since, topic, sorted(info["kinds"])))
    records: list[dict] = []
    for _, topic, kinds in sorted(due):
        topics[topic]["notified"] = True
        if len(sent) >= cap:
            skipped += 1
            records.append({"type": "push_capped", "topic": topic, "kinds": kinds})
            continue
        text = f"{topic}: {', '.join(LABELS.get(k, k) for k in kinds)}"
        if skipped:
            text += f" (+{skipped} more waited while capped)"
        sent.append(now)
        state.write_json_atomic(state_path(), {"topics": topics, "sent": sent, "skipped": skipped, "pass_at": now})
        rc = send(TITLE, text)
        if rc == 0:
            skipped = 0
        records.append({"type": "push", "topic": topic, "kinds": kinds, "ok": rc == 0, "rc": rc})
    state.write_json_atomic(state_path(), {"topics": topics, "sent": sent, "skipped": skipped, "pass_at": now})
    return records


def _wait(prior: dict, asked: float | None, last: float | None, now: float, start: float) -> tuple[float, bool]:
    renewed = prior.get("notified") and asked is not None and last is not None and asked > last
    if prior and not renewed:
        since = _stamp(prior.get("since"))
        since = now if since is None else since
        return since, bool(prior.get("notified")) or since < start
    since = max(t for t in (asked if asked is not None else now, last) if t is not None)
    return since, since < start


def _stamp(value) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
