from __future__ import annotations

from typing import Callable, Dict, NamedTuple, Optional

Fields = Dict[str, object]
ARROW = " → "
DASH = " — "
LINE_BUDGET = 500


class Kind(NamedTuple):
    head: Callable[[Fields], str]
    step: Callable[[Fields], Optional[str]]
    tail: Callable[[Fields], str]
    journal: bool


class _Blank(dict):
    def __missing__(self, key):
        return "?"


def _t(template: str, f: Fields) -> str:
    return template.format_map(_Blank(f))


def _tpl(template: str) -> Callable[[Fields], str]:
    return lambda f: _t(template, f)


def _silent(_f: Fields) -> Optional[str]:
    return None


def _nothing(_f: Fields) -> str:
    return ""


def _field(name: str) -> Callable[[Fields], str]:
    return lambda f: str(f.get(name) or "")


def _stuck_step(f: Fields) -> str:
    step = _t('pipeline_status("{task_key}")', f)
    if f.get("reason") in ("paused", "spawn-error"):
        step += "; the engine retries the spawn each requested_ttl_sec"
    if f.get("reason") == "paused":
        step += "; check the usage pause (spawn_worker's paused result)"
    if f.get("reason") == "no_register":
        step += "; the session never registered: an ADVANCE_UNREGISTERED line names its pane"
    return step


def _parked_step(f: Fields) -> str:
    if f.get("gate_lost"):
        step = _t('its gate died: tell the engineer his message to the topic reopens it, or resume_worker("{name}") '
                  'once he is back', f)
    else:
        step = _t('resume_worker("{name}")', f)
        step = step + " once the engineer is back at the gate" if f.get("gate") else step
    token = _t(', token_id="{token_id}"', f) if f.get("token_id") else ""
    return step + _t('; if it waits on someone the engine does not watch: pipeline_advance('
                     'claude_sid=<your sid>, task_key="{task_key}", to="?"', f) + token + (
        ", reason=<what it waits on>) holds it and ends this page")


def _poll_failed_step(f: Fields) -> str:
    check = "check gh in the work dir (auth, account, network, a gone dir)"
    if f.get("node"):
        return check + _t('; the waiting {node} node re-reads its PRs itself: send_manager_to_worker it, or let the '
                          'heartbeat wake it', f)
    if f.get("trigger") == "status":
        return _t("check the tracker env file and the issue {url}; the stale lane reads its status once per "
                  "trigger_poll_sec and moves the token to {to} once it is one of {status}", f)
    if f.get("trigger") == "merge":
        return check + _t('; once every PR has merged: pipeline_advance(claude_sid=<your sid>, task_key="{task_key}", '
                          'to=<next node>, reason=<mergedAt>)', f)
    return check + _t('; once the first PR is ready or merged: pipeline_advance(claude_sid=<your sid>, '
                      'task_key="{task_key}", to=<next node>, reason=<the observation>)', f)


def _unadvanced_step(f: Fields) -> str:
    send = _t('send_manager_to_worker("{name}", "continue your node", auto_resume=True)', f)
    if f.get("row"):
        return send + "; nothing to resume: kill_worker every row of the fork, then pipeline_rework at the fork source"
    return send + _t('; nothing to resume: pipeline_advance(claude_sid=<your sid>, task_key="{task_key}", '
                     'to="{node}", index={index}, override=True, reason=<why>)', f)


def _held_step(f: Fields) -> str:
    token = _t(', token_id="{token_id}"', f) if f.get("token_id") else ""
    return _t('once the reason clears: pipeline_advance(claude_sid=<your sid>, task_key="{task_key}", to="?"', f) + (
        token + ") with no reason releases it, or move it") + _t(
        '; if it will never run again: pipeline_close(manager_sid=<your sid>, task_key="{task_key}", reason=<why>); '
        "this page does not repeat", f)


def _reconcile_error_step(f: Fields) -> str:
    return "dockwright lanes" if f.get("task_key") == "*" else _t('pipeline_status("{task_key}")', f)


def _done_step(f: Fields) -> Optional[str]:
    if f.get("advanced") or f.get("repeat"):
        return None
    return _t('verify per § Verify before relay, then relay; a cut summary: get_worker_summary("{worker}"), never '
              "cat the done file; harvest it before any send_manager_to_worker to it", f)


def _finished_head(f: Fields) -> str:
    return _t("{name}", f) + (" (session exited)" if f.get("exited") else "")


def _nudged_step(f: Fields) -> Optional[str]:
    if not f.get("repeat"):
        return None
    return _t('get_worker_tail("{name}"); a dialog on the pane: clear it per dockwright-orchestrator-guide '
              "§ APPROVAL_PROMPT pages; otherwise "
              "surface it to the engineer and ask: kill_worker or follow up", f)


def _context_step(f: Fields) -> Optional[str]:
    if f.get("own"):
        return ("tell the engineer a recycle is due: propose /manager-recycle at the next fleet-idle boundary; "
                "never recycle or recreate yourself on this line")
    threshold = int(f.get("threshold") or 0)
    if threshold >= 90:
        return "tell the engineer"
    if threshold >= 80:
        return _t('have it finish and report, then kill_worker("{name}") and spawn_worker a fresh one from its '
                  "artifacts; a token-holding pipeline node: kill_worker only, never one parked at a gate", f)
    return None


def _approval_head(f: Fields) -> str:
    return _t("{name}", f)


_IDLE_UNTASKED = {
    "no-input": "idle with no next step and its pane would not take input",
    "unsent-text": "idle with no next step and its pane holds unsent text (a human may be typing); nothing typed",
    "nothing-typed": "idle with no next step and nothing typed",
}


def _idle_untasked_step(f: Fields) -> str:
    if f.get("variant") == "unsent-text":
        return _t('leave the pane alone, or relay with send_manager_to_worker("{name}", <next step>)', f)
    return _t('send_manager_to_worker("{name}", <next step>) or kill_worker("{name}")', f)


_CREDIT = {
    "unresolved": ("out of credits for {spent} and the fallback model could not be resolved "
                   "(`python -m dockwright model resolve` failed), so nothing was typed",
                   "{remedy}, then {resume}"),
    "same-family": ("out of credits for {spent} and the fallback {model} is the same family, so no rescue "
                    "target is left and nothing was typed",
                    "{remedy}, then {resume}"),
    "still-out": ("still out of credits {confirm_sec}s after the lane acted.{named}",
                  "if that is {model}, the fallback itself is out{cascade}: tell the engineer; otherwise clear "
                  "its input line, {switch} by hand, and {resume}"),
    "not-typed": ("out of credits for its model and nothing typed", "{switch} and {resume}"),
    "unsent-text": ("out of credits for its model and its pane holds unsent text; nothing typed",
                    "clear that line, {switch} by hand, and {resume}"),
    "partial-switch": ("out of credits for its model and its pane would not take the whole switch; part of it "
                       "may sit unsubmitted on the input line",
                       "clear that line, {switch} by hand, and {resume}"),
}


def _credit(part: int) -> Callable[[Fields], str]:
    return lambda f: _t(_CREDIT[str(f.get("variant"))][part], f)


_SAFEGUARD_LOOK = ("tell the engineer to look at pane {pane}: submit or clear its input line, then type continue; "
                   "never relay into that pane")
_SAFEGUARD = {
    "continued": ('a safeguard flag paused it; the lane chose "{key}. Edit prompt and retry" and typed continue',
                  '{tail_tool}("{name}") in a few minutes to confirm it resumed'),
    "not-typed": ("a safeguard flag paused it; nothing typed: autonudge is off",
                  "tell the engineer to press {key} (Edit prompt and retry) in pane {pane}, then type continue; "
                  "never relay into that pane: the relay's Enter picks the model switch"),
    "again": ("a second safeguard flag paused it within {window}min of the lane's retry; nothing typed",
              "tell the engineer it flagged twice: press {key} (Edit prompt and retry) in pane {pane} and type "
              "continue, or pick the model switch there; never relay into that pane"),
    "unsettled": ("the lane's keypress {key} did not bring the pane back to an empty input line: the dialog stayed, "
                  "the press failed, or Claude Code put the refused prompt back on it; continue not typed",
                  "tell the engineer to look at pane {pane}: if the dialog still shows, press {key} (Edit prompt and "
                  "retry), never Enter; otherwise submit or clear its input line; then type continue; never relay "
                  "into that pane"),
    "unsent": ("the lane pressed {key} and continue did not submit; it may sit on the input line", _SAFEGUARD_LOOK),
}


_ACCOUNT_HOLD = {
    "banner": ("appeal at https://claude.ai/restricted",
               "resume_worker each worker list_closed_workers shows with closed_reason account-hold (it lands on a "
               "healthy account); tell the engineer account {account} is on hold (the API error is in its "
               "transcripts); to put it back run dockwright account-hold --release {account}"),
    "operator": ("",
                 "tell the engineer account {account} is held ({reason}); running sessions finish on it; to put it "
                 "back run dockwright account-hold --release {account}"),
}


_FABLE_LIMIT = {
    "moved": ("", None),
    "child-closed": ("", None),
    "resume-refused": ("the Fable move closed it and the resume was refused {attempts} times",
                       'resume_worker("{name}") once it is in list_closed_workers (closed_reason fable-moved, or moved '
                       'fable); it lands on an account with Fable or on Opus; last refusal: {reason}'),
    "close-refused": ("the Fable move closed pane {pane} and pid {pid} still runs: {reason}",
                      'kill_worker("{name}") then resume_worker("{name}"); it lands on an account with Fable or on '
                      'Opus'),
    "human": ("nothing was typed into the attached pane",
              'the worker is resumed on a fresh account and its pane is human-attached; '
              'send_manager_to_worker("{name}", "resume your task") when he is done'),
    "not-typed": ("the Fable limit banner is up; the lane did not move it: {reason}", "{switch} and {resume}"),
    "unsent-text": ("the Fable limit banner is up and text sits unsubmitted in its pane; the lane moves it on a later "
                    "tick once the line is clear",
                    "tell the engineer to clear the input line of pane {pane}; never relay into that pane: the "
                    "relay's Enter submits that text"),
}


def _fable_limit(part: int) -> Callable[[Fields], Optional[str]]:
    def render(f: Fields) -> Optional[str]:
        template = _FABLE_LIMIT[str(f.get("variant"))][part]
        return None if template is None else _t(template, f)
    return render


def _event_poll_failed_step(f: Fields) -> str:
    if f.get("skipped"):
        return _t("the {source} source skipped the named items, which are not retried; act only when every item "
                  "fails: then the source's data format changed", f)
    return _t("the {source} source failed on the named issue, run or host; its cursor did not move and it is retried "
              "last; fix the credential, the host or the subscription", f)


def _account_hold(part: int) -> Callable[[Fields], str]:
    return lambda f: _t(_ACCOUNT_HOLD["banner" if f.get("reason") == "banner" else "operator"][part], f)


def _safeguard(part: int) -> Callable[[Fields], str]:
    def render(f: Fields) -> str:
        tail_tool = "get_child_tail" if f.get("parent") else "get_worker_tail"
        return _t(_SAFEGUARD[str(f.get("variant"))][part], {**f, "tail_tool": tail_tool})
    return render


def _safeguard_head(f: Fields) -> str:
    return _t("{name} (child of {parent})" if f.get("parent") else "{name}", f)


KINDS: Dict[str, Kind] = {
    "ADVANCE_STUCK": Kind(_tpl("{task_key} {to} {reason}"), _stuck_step, _nothing, False),
    "ADVANCE_UNREGISTERED": Kind(
        _tpl("{task_key} {name} {window}"),
        _tpl('clear the prompt of pane {window} or close the pane; kill_worker("{name}") only once it is dead'),
        _nothing, False),
    "NODE_PARKED": Kind(_tpl("{task_key} {node} {name}"), _parked_step, _field("reason"), False),
    "NODE_UNADVANCED": Kind(_tpl("{task_key} {node} {index} {name}"), _unadvanced_step, _nothing, False),
    "NODE_HELD": Kind(_tpl("{task_key} {node} {name}"), _held_step, _field("reason"), False),
    "TRIGGER_POLL_FAILED": Kind(_tpl("{task_key} {url}"), _poll_failed_step, _nothing, False),
    "RECONCILE_ERROR": Kind(_tpl("{task_key}"), _reconcile_error_step, _field("error"), False),
    "MESSAGE_UNDELIVERED": Kind(
        _tpl("{task_key} {record}"),
        _tpl("fix the reason; the queue keeps retrying it every 5 min. If it cannot land, tell the engineer"),
        _field("reason"), False),
    "ADVANCE_RECONCILED": Kind(_tpl("{task_key} {to} {name}"), _silent, _nothing, False),
    "NODE_RESTARTED": Kind(_tpl("{task_key} {to} {name}"), _silent, _nothing, False),
    "ADVANCE_SETTLED": Kind(_tpl("{task_key} {to} {name}"), _silent, _nothing, False),
    "TRIGGER_FIRED": Kind(_tpl("{task_key} {to} {state}"), _silent, _nothing, False),
    "TRIGGER_WAKE": Kind(_tpl("{task_key} {node}"), _silent, _nothing, True),
    "LEASE_WAKE": Kind(_tpl("{task_key} {node} {category}"), _silent, _nothing, True),
    "EVENT_WAKE": Kind(_tpl("{task_key} {node} {events}"), _silent, _nothing, True),
    "EVENT_STARTED": Kind(_tpl("{task_key} {rule}"),
                          _tpl("the engine started this work from an event; relay its intake like any other work"),
                          _field("event_id"), True),
    "EVENT_PAGE": Kind(_tpl("{rule}"), _tpl("{line}; act on it as your rule for [events.rules.{rule}] says"),
                       _field("event_id"), False),
    "EVENT_QUEUE": Kind(_tpl("{rule}"),
                        _tpl('{count} pending, oldest {line}; event_queue("{rule}") lists them, event_ack closes each'),
                        _field("event_id"), False),
    "CHAT_ARCHIVE_FAILED": Kind(_tpl("{failure_kind}"), _tpl("{failure}; see dockwright chat-archive status"), _nothing,
                                False),
    "EVENT_STALLED": Kind(_tpl("{rule} {domain}"),
                          _tpl("no live manager of domain {domain} since {since}; start one or disable the rule in "
                               "[events.rules.{rule}]"), _nothing, False),
    "EVENT_RULE_FAILED": Kind(_tpl("{rule}"),
                              _tpl("the rule raised and the other rules ran; it retries from its cursor next pass; "
                                   "fix the cause in [events.rules.{rule}] or the code"), _field("error"), False),
    "EVENT_POLL_FAILED": Kind(_tpl("{source}"), _event_poll_failed_step, _field("error"), False),
    "EVENT_ENV_INSECURE": Kind(_tpl("{path} {reason}"),
                               _tpl("the events env file is {reason}; create it with mode 0600 (TRACKER_URL, TRACKER_EMAIL, "
                                    "TRACKER_API_TOKEN); the tracker source stays idle until then"), _nothing, False),
    "HANDED_OFF_CLOSED": Kind(_tpl("{task_key} {name} ({reason})"), _silent, _nothing, True),
    "NODE_NO_SUMMARY": Kind(_tpl("{task_key} {node} {name}"), _silent, _nothing, True),
    "DONE": Kind(_tpl("{worker}"), _done_step, _field("summary"), True),
    "QUESTION": Kind(
        _tpl("{worker}"),
        _tpl("list_pending_questions(manager_sid=<your sid>), relay the oldest verbatim, answer_question"),
        _field("question"), False),
    "ANSWER": Kind(
        _tpl("{task_key} {qid}"),
        _tpl('artifact_get("{task_key}", "decision", "q-{qid}") — the engineer answered on the board or the CLI: '
             "read the full answer there, act on it, do not re-ask"),
        _field("answer"), False),
    "FINISHED_SILENTLY": Kind(
        _finished_head,
        _tpl('get_worker_summary("{name}"); rule out a plannotator park (§ Design-gate relays), then harvest, '
             "re-instruct it with the worker_done reminder, or close it"),
        _field("summary"), False),
    "STALE_PROCESSING": Kind(
        _tpl("{name} ({minutes}min)"),
        _tpl('get_worker_tail("{name}"), surface it to the engineer and ask: kill_worker or follow up'),
        _nothing, False),
    "NUDGED": Kind(_tpl("{name} ({why})"), _nudged_step, _nothing, True),
    "RESUMED": Kind(_tpl("{name}"), _silent, _nothing, True),
    "LIMIT_CLEARED": Kind(
        _tpl("{clock}"),
        _tpl("list_workers(manager_sid=<your sid>); the held question, done and turn-end events follow this line"),
        _field("summary"), False),
    "CONTEXT": Kind(_tpl("{name} {used}k of {window} ({pct}%), {threshold}% step"), _context_step, _nothing, True),
    "APPROVAL_PROMPT": Kind(
        _approval_head,
        _tpl('get_worker_pane("{name}") and read the whole command; '
             'answer_permission_prompt("{name}", approve|refuse, prompt_at=<its prompt_at>) per '
             "dockwright-orchestrator-guide § APPROVAL_PROMPT pages, or escalate with it verbatim"),
        _field("excerpt"), False),
    "SAFEGUARD_PAUSE": Kind(_safeguard_head, _safeguard(1), _safeguard(0), False),
    "ORPHAN_WINDOW": Kind(
        _tpl("{pane} {what} ({minutes}min)"),
        _tpl("capture the pane; a live one is a ghost: keep it running, drive it through the pane, root-cause "
             "the missing record"),
        lambda f: "no backing record", False),
    "ORPHANED_CHILD": Kind(_tpl("{name} (parent {parent} gone)"), _silent, _nothing, True),
    "SWITCHED": Kind(
        _tpl("account {account}→{new} ({subject})"),
        _tpl("kill_worker, then resume_worker (once it is in list_closed_workers) each bricked worker promptly; "
             "healthy workers may finish on their birth account"),
        _nothing, False),
    "AUTH_401": Kind(
        _tpl("{name} ({account})"),
        _tpl('kill_worker("{name}"), then resume_worker("{name}") once it is in list_closed_workers, on the same '
             "account; do NOT flip"),
        lambda f: "transient auth-401", False),
    "AUTH_401_ESCALATED": Kind(
        _tpl("{account} ({subject})"),
        _tpl("resume bricked workers per the SWITCHED line that follows; the engineer must run {fix}, then /login"),
        lambda f: "login suspect after repeated 401s", False),
    "IDLE_UNTASKED": Kind(
        _tpl("{name} ({minutes}min)"), _idle_untasked_step,
        lambda f: _IDLE_UNTASKED.get(str(f.get("variant")), ""), False),
    "IDLE_NUDGED": Kind(_tpl("{name} ({minutes}min)"), _silent, _nothing, True),
    "MODEL_FALLBACK": Kind(_tpl("{name}"), _silent, _nothing, True),
    "MODEL_CREDIT_EXHAUSTED": Kind(_tpl("{name}"), _credit(1), _credit(0), False),
    "ACCOUNT_HOLD": Kind(_tpl("{account} since {since}"), _account_hold(1), _account_hold(0), False),
    "FABLE_LIMIT": Kind(_tpl("{name}"), _fable_limit(1), _fable_limit(0), True),
    "USAGE_CAP": Kind(
        _tpl("{breaches}"),
        _tpl("tell the engineer the fleet is paused until {until}; every start is refused; to lift early edit {path}"),
        lambda f: "list_closed_workers shows who was closed (reason usage-cap)", False),
    "USAGE_CAP_CLEARED": Kind(
        lambda f: "fleet",
        lambda f: ('tell the engineer the pause is lifted; for each regular worker list_closed_workers shows closed '
                   'with reason usage-cap: send_manager_to_worker(<name>, "[MANAGER] resume your task", '
                   'auto_resume=True)'),
        lambda f: "pipeline nodes restart on their own", False),
    "USAGE_CAP_FILE": Kind(
        _tpl("{path}"),
        lambda f: "fix the file or [accounts] pool; every start is refused until it parses",
        _field("error"), False),
    "LANE_SILENT": Kind(
        _tpl("{lane} ({minutes}min)"), _tpl("dockwright lanes, then follow that row's recovery line"),
        _tpl("no scan for {minutes}min (expected every {interval}s); events are NOT reaching you on that lane"),
        False),
    "DAEMON_STOPPED": Kind(
        _tpl("daemon-stop"), _tpl("rm {stop}"),
        _tpl("the stop file holds the daemon while the lanes run in it ([lanes] in_manager = false), so no QUESTION, "
             "DONE or FINISHED_SILENTLY reaches you until it is removed"),
        False),
    "CONFIG_UNREADABLE": Kind(
        _tpl("{path}"), _tpl("fix dockwright.toml"),
        _tpl("{error}; the daemon and the lanes keep the last mode they read, and nothing new in the file takes effect "
             "until it parses"),
        False),
    "DAEMON_SILENT": Kind(
        _tpl("{minutes}min"), _tpl("dockwright daemon status"),
        _tpl("the daemon has not stamped daemon/alive.json for {minutes}min of machine-awake time; the lanes run "
             "in the daemon ([lanes] in_manager = false), so no QUESTION, DONE or FINISHED_SILENTLY reaches you "
             "until it runs; then dockwright doctor and daemon/launchd-err.log"),
        False),
    "DAEMON_STUCK": Kind(
        _tpl("{step} {minutes}min"), _tpl("dockwright daemon status"),
        _tpl("the daemon tick is alive, but its step {step} has run for {minutes}min of machine-awake time; no new "
             "pass starts while it holds tick.lock, and a manual dockwright daemon tick does nothing; tell the "
             "human, restart nothing"),
        False),
}


def render(kind: str, **fields: object) -> Optional[str]:
    spec = KINDS[kind]
    step = spec.step(fields)
    if step is None:
        return None
    line = f"{kind} {spec.head(fields)}{ARROW}{step}"
    tail = spec.tail(fields).replace("\n", " ").strip()
    return f"{line}{DASH}{tail}" if tail else line


def head(kind: str, **fields: object) -> str:
    return KINDS[kind].head(fields)


def journals(kind: str) -> bool:
    return KINDS[kind].journal


def label(kind: object, fields: object) -> str:
    spec = KINDS.get(kind) if isinstance(kind, str) else None
    if spec is None or not isinstance(fields, dict):
        return str(kind)
    return f"{kind} {spec.head(fields)}"
