import importlib.util
import json
import time
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
STALE_MONITOR_PATH = REPO_ROOT / "src" / "dockwright" / "stale_monitor.py"

IDLE_PANE = "❯ \n"
BUSY_PANE = "❯ half a sentence he has not sent\n"


def _load_stale_monitor():
    spec = importlib.util.spec_from_file_location("stale_under_test_ha", STALE_MONITOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _idle_proc_index():
    return {"command_by_pid": {1: "/sbin/launchd"}, "child_commands": {}}


class PaneDriver:
    """A driver that reports pane contents, so the guard has something to measure."""

    def __init__(self, screens=None):
        self.screens = screens or {}
        self.sent = []
        self.captures = []
        self.closed = []

    def send_text(self, window_id, text, submit=True):
        self.sent.append((window_id, text))

    def send_text_checked(self, window_id, text):
        self.sent.append((window_id, text))
        return True

    def capture_screen(self, pane):
        return self.screens.get(pane, IDLE_PANE)

    def capture_screen_ansi(self, pane):
        self.captures.append(pane)
        return self.screens.get(pane, IDLE_PANE)

    def close(self, window_id):
        self.closed.append(window_id)


@pytest.fixture
def stale(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_ORCH_STALE_PROCESSING_MIN", raising=False)
    monkeypatch.delenv("CLAUDE_ORCH_AUTONUDGE", raising=False)
    mod = _load_stale_monitor()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "ACTIVE", tmp_path / "active")
    monkeypatch.setattr(mod, "QUESTIONS", tmp_path / "questions")
    monkeypatch.setattr(mod, "CLOSED", tmp_path / "closed")
    monkeypatch.setattr(mod, "PANE_SENDS", tmp_path / "pane-sends")
    monkeypatch.setattr(mod, "CLAUDE_PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(mod, "CODEX_SESSIONS", tmp_path / "codex-sessions")
    monkeypatch.setattr(mod, "ASSIGNMENTS", tmp_path / "assignments", raising=False)
    monkeypatch.setattr(mod, "ASSIGNMENTS_PENDING",
                        tmp_path / "assignments" / ".pending", raising=False)
    monkeypatch.setattr(mod, "GARDENER_LIVE_WINDOWS",
                        tmp_path / "gardener" / "live-windows", raising=False)
    monkeypatch.setattr(mod, "ACCOUNT_ACTIVE", tmp_path / "account-active")
    monkeypatch.setattr(mod, "ACCOUNT_LEDGER", tmp_path / "account-flips.jsonl")
    monkeypatch.setattr(mod, "ACCOUNT_STATE", tmp_path / "account-state.json")
    monkeypatch.setattr(mod, "ACCOUNT_LOCK", tmp_path / ".account-flip.lock")
    monkeypatch.setattr(mod, "_keychain_unlocked", lambda: False)
    monkeypatch.setattr(mod, "_process_index", _idle_proc_index)
    monkeypatch.setattr(mod, "AUTONUDGE", True)
    monkeypatch.setattr(mod, "_list_worker_windows", lambda: [])
    for d in ("active", "questions", "closed"):
        (tmp_path / d).mkdir()
    return mod


@pytest.fixture
def driver(stale, monkeypatch):
    drv = PaneDriver()
    monkeypatch.setattr(stale, "_get_driver", lambda: drv)
    return drv


def _record(stale, sid, **overrides):
    record = {
        "claude_sid": sid,
        "agent": "worker",
        "name": f"worker-{sid}",
        "cwd": "/x",
        "window_id": f"%{sid}",
        "iterm_sid": "",
        "pid": 0,
        "started_at": time.time(),
        "state": "idle",
        "last_summary": None,
        "last_turn_at": None,
    }
    record.update(overrides)
    path = stale.ACTIVE / f"{sid}.json"
    path.write_text(json.dumps(record))
    return path


def _idle_for(stale, sid, seconds, **overrides):
    """A worker whose last turn ended `seconds` ago, past the untasked threshold."""
    now = time.time()
    return _record(stale, sid, state="idle", last_turn_at=None,
                   started_at=now - seconds, tasked_at=now - seconds,
                   **overrides)


def _run(stale, capsys):
    stale.main()
    return capsys.readouterr().out


# --------------------------------------------------------------------------
# Defect 1 — no automation types into a pane whose input box holds text
# --------------------------------------------------------------------------

def test_an_idle_pane_still_takes_the_nudge(stale, driver, capsys, monkeypatch):
    silent = []
    monkeypatch.setattr(stale, "_journal_silent", lambda kind, fields, sid: silent.append(kind))
    _idle_for(stale, "w1", stale.IDLE_UNTASKED_SEC + 60)
    out = _run(stale, capsys)
    assert ("%w1", stale.IDLE_NUDGE_TEXT) in driver.sent
    assert "IDLE_NUDGED" not in out and silent == ["IDLE_NUDGED"]


def test_a_pane_holding_his_text_takes_no_nudge_and_pages_instead(stale, driver, capsys):
    driver.screens["%w1"] = BUSY_PANE
    _idle_for(stale, "w1", stale.IDLE_UNTASKED_SEC + 60)
    out = _run(stale, capsys)
    assert driver.sent == []
    assert "IDLE_UNTASKED" in out
    assert "holds unsent text" in out


def test_the_busy_pane_page_fires_once_per_idle_episode(stale, driver, capsys):
    driver.screens["%w1"] = BUSY_PANE
    _idle_for(stale, "w1", stale.IDLE_UNTASKED_SEC + 60)
    first = _run(stale, capsys)
    second = _run(stale, capsys)
    assert "IDLE_UNTASKED" in first
    assert "IDLE_UNTASKED" not in second, "a held pane must not page every scan"
    assert driver.sent == []


def test_an_unreadable_pane_is_treated_as_occupied(stale, driver, capsys):
    driver.screens["%w1"] = None
    _idle_for(stale, "w1", stale.IDLE_UNTASKED_SEC + 60)
    _run(stale, capsys)
    assert driver.sent == [], "a pane we cannot read is never typed into"


def test_a_codex_pane_is_not_measured_and_still_takes_its_nudge(stale, driver, capsys):
    driver.screens["%w1"] = "no caret anywhere in this pane"
    _idle_for(stale, "w1", stale.IDLE_UNTASKED_SEC + 60, runtime="codex")
    _run(stale, capsys)
    assert ("%w1", stale.IDLE_NUDGE_TEXT) in driver.sent


def test_the_input_capture_is_read_once_per_pane_per_scan(stale, driver, capsys):
    _idle_for(stale, "w1", stale.IDLE_UNTASKED_SEC + 60)
    _run(stale, capsys)
    assert driver.captures.count("%w1") == 1


def test_a_busy_pane_at_the_processing_threshold_pages_stale_processing(stale, driver, capsys):
    driver.screens["%w1"] = BUSY_PANE
    now = time.time()
    path = _record(stale, "w1", state="processing", processing_since=now - 4000)
    import os
    os.utime(path, (now - 4000, now - 4000))
    out = _run(stale, capsys)
    assert driver.sent == []
    assert "STALE_PROCESSING" in out
    assert "NUDGED" not in out.replace("IDLE_NUDGED", "")


def test_the_manager_pane_holding_text_is_never_typed_into(stale, driver):
    memo = {"input": {}}
    driver.screens["%mgr"] = BUSY_PANE
    assert stale._send_text("%mgr", stale.MANAGER_NUDGE_TEXT, memo) is False
    assert driver.sent == []


def test_a_send_into_an_idle_pane_reports_success(stale, driver):
    assert stale._send_text("%ok", "anything", {"input": {}}) is True
    assert driver.sent == [("%ok", "anything")]


# --------------------------------------------------------------------------
# The stamp — how automation's own typing is told from his
# --------------------------------------------------------------------------

def test_a_nudge_writes_no_active_record_so_the_staleness_clock_survives(stale, driver, capsys):
    path = _idle_for(stale, "w1", stale.IDLE_UNTASKED_SEC + 60)
    before = path.stat().st_mtime_ns
    _run(stale, capsys)
    assert driver.sent, "precondition: the nudge was typed"
    assert path.stat().st_mtime_ns == before, (
        "stamping must not touch the record whose mtime drives the ladder")


def test_a_typed_nudge_leaves_exactly_one_pending_stamp(stale, driver, capsys):
    _idle_for(stale, "w1", stale.IDLE_UNTASKED_SEC + 60)
    _run(stale, capsys)
    pending = json.loads((stale.PANE_SENDS / "w1.json").read_text())["pending"]
    assert len(pending) == 1


def test_slash_commands_are_not_stamped(stale, driver):
    stale._send_text("%w1", "/model claude-sonnet-5", {"input": {}}, "claude", "w1")
    assert not (stale.PANE_SENDS / "w1.json").exists(), (
        "a slash command fires no UserPromptSubmit, so it must leave no stamp to consume")


def test_a_prose_send_is_stamped(stale, driver):
    stale._send_text("%w1", "[MANAGER] resume your task", {"input": {}}, "claude", "w1")
    assert json.loads((stale.PANE_SENDS / "w1.json").read_text())["pending"]


def test_stamps_older_than_the_ttl_are_pruned(stale, driver):
    stale.PANE_SENDS.mkdir(parents=True)
    old = time.time() - stale.AUTOMATION_STAMP_TTL_SEC - 10
    (stale.PANE_SENDS / "w1.json").write_text(json.dumps({"pending": [old]}))
    stale._stamp_pane_send("w1")
    pending = json.loads((stale.PANE_SENDS / "w1.json").read_text())["pending"]
    assert len(pending) == 1, "the expired entry is dropped, not carried"


# --------------------------------------------------------------------------
# Defect 2 — the mode itself
# --------------------------------------------------------------------------

def test_is_human_attached_decays(stale):
    now = time.time()
    assert stale.is_human_attached({"human_attached_at": now}, now) is True
    assert stale.is_human_attached(
        {"human_attached_at": now - stale.HUMAN_ATTACHED_TTL_SEC - 1}, now) is False
    assert stale.is_human_attached({}, now) is False
    assert stale.is_human_attached({"human_attached_at": True}, now) is False
    assert stale.HUMAN_ATTACHED_TTL_SEC == 40 * 60


def test_an_attached_worker_is_never_nudged_and_pages_nothing(stale, driver, capsys):
    _idle_for(stale, "w1", stale.IDLE_UNTASKED_SEC + 60,
              human_attached_at=time.time())
    out = _run(stale, capsys)
    assert driver.sent == []
    assert "IDLE_NUDGED" not in out
    assert "IDLE_UNTASKED" not in out


def test_an_attached_worker_is_not_autoclosed(stale, driver, capsys):
    _idle_for(stale, "w1", stale.IDLE_THRESHOLD_SEC * 6,
              human_attached_at=time.time())
    _run(stale, capsys)
    assert driver.closed == []
    assert (stale.ACTIVE / "w1.json").exists()
    assert not (stale.CLOSED / "w1.json").exists()


def test_the_same_worker_unattached_is_autoclosed(stale, driver, capsys):
    _idle_for(stale, "w1", stale.IDLE_THRESHOLD_SEC * 6)
    out = _run(stale, capsys)
    assert out == ""
    assert not (stale.ACTIVE / "w1.json").exists()
    assert (stale.CLOSED / "w1.json").exists()


def test_an_attached_processing_worker_pages_no_stale_processing(stale, driver, capsys):
    import os
    now = time.time()
    path = _record(stale, "w1", state="processing", processing_since=now - 4000,
                   human_attached_at=now)
    os.utime(path, (now - 4000, now - 4000))
    out = _run(stale, capsys)
    assert "STALE_PROCESSING" not in out
    assert driver.sent == []


def test_a_handed_back_worker_takes_its_nudge_again(stale, driver, capsys):
    path = _idle_for(stale, "w1", stale.IDLE_UNTASKED_SEC + 60,
                     human_attached_at=time.time())
    _run(stale, capsys)
    assert driver.sent == []
    record = json.loads(path.read_text())
    record["human_attached_at"] = None
    path.write_text(json.dumps(record))
    _run(stale, capsys)
    assert ("%w1", stale.IDLE_NUDGE_TEXT) in driver.sent


def test_an_attached_worker_keeps_its_untasked_keys_across_the_scan(stale, driver, capsys):
    """The idle branch must `continue` before idle_branch_ran.add, or the cursor
    drops the untasked keys and the worker is nudged the instant it is handed back."""
    path = _idle_for(stale, "w1", stale.IDLE_UNTASKED_SEC + 60)
    _run(stale, capsys)
    assert driver.sent, "precondition: it was nudged while unattached"
    driver.sent.clear()
    record = json.loads(path.read_text())
    record["human_attached_at"] = time.time()
    path.write_text(json.dumps(record))
    _run(stale, capsys)
    record = json.loads(path.read_text())
    record["human_attached_at"] = None
    path.write_text(json.dumps(record))
    _run(stale, capsys)
    assert driver.sent == [], (
        "the spent untasked key must survive the attached scan, "
        "so hand-back does not re-fire the nudge it already spent")


# --------------------------------------------------------------------------
# The hook — who typed this prompt
# --------------------------------------------------------------------------

import io
import sys

from dockwright import paths, state
from dockwright.hooks import user_prompt_submit
from dockwright.monitor import (TURN_END_EMIT, TURN_END_SUPPRESS, classify_turn_end)


@pytest.fixture
def hooked(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT", "worker")
    monkeypatch.delenv("CLAUDE_WORKER_RUNTIME", raising=False)
    for name, sub in (("ROOT", ""), ("ACTIVE", "active"), ("QUESTIONS", "questions"),
                      ("ANSWERS", "answers"), ("DONE", "done"), ("CLOSED", "closed"),
                      ("HANDOFFS", "handoffs"), ("TURN_ENDS", "turn-ends"),
                      ("ARTIFACTS", "artifacts"), ("ASSIGNMENTS", "assignments"),
                      ("PANE_SENDS", "pane-sends")):
        monkeypatch.setattr(paths, name, tmp_path / sub if sub else tmp_path)
    monkeypatch.setattr(paths, "ASSIGNMENTS_PENDING", tmp_path / "assignments" / ".pending")
    monkeypatch.setattr(paths, "SPEND_LEDGER", tmp_path / "spend-ledger.jsonl")
    monkeypatch.setattr("dockwright.hooks.subprocess.run",
                        lambda a, **kw: type("R", (), {"returncode": 0})())
    paths.ensure_dirs()
    return tmp_path


def _worker(hooked, sid="s1", **overrides):
    record = {"claude_sid": sid, "agent": "worker", "name": "alpha", "cwd": "/x",
              "iterm_sid": "i1", "pid": 1, "started_at": 0, "state": "idle"}
    record.update(overrides)
    state.write_json_atomic(paths.ACTIVE / f"{sid}.json", record)
    return paths.ACTIVE / f"{sid}.json"


def _submit(monkeypatch, prompt, sid="s1", **fields):
    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"session_id": sid, "prompt": prompt, **fields})))
    user_prompt_submit()


def _stamp(sid="s1", count=1, age=0.0):
    paths.PANE_SENDS.mkdir(parents=True, exist_ok=True)
    now = time.time() - age
    state.write_json_atomic(paths.PANE_SENDS / f"{sid}.json",
                            {"pending": [now] * count})


def test_the_spawn_brief_does_not_arm_the_mode(hooked, monkeypatch):
    """An argv prompt fires UserPromptSubmit and carries no marker, so only the
    first-submit condition keeps a spawn from arming."""
    path = _worker(hooked, last_turn_at=None)
    _submit(monkeypatch, "Implement the thing described below")
    assert state.read_json(path).get("human_attached_at") in (None, 0)


def test_his_first_message_after_the_brief_arms_even_before_a_turn_lands(hooked, monkeypatch):
    """Measured live: the Stop hook does not always record last_turn_at on the
    first turn, so an arm gated on it costs him an extra message. The hook counts
    its own submits instead."""
    path = _worker(hooked, last_turn_at=None)
    _submit(monkeypatch, "the spawn brief")
    assert state.read_json(path).get("human_attached_at") in (None, 0)
    _submit(monkeypatch, "wait, stop")
    assert state.read_json(path)["human_attached_at"] > 0


def test_a_bare_relay_does_not_arm_and_consumes_its_stamp(hooked, monkeypatch):
    path = _worker(hooked, tasked_at=time.time(), prompts_seen=1)
    _stamp()
    _submit(monkeypatch, "[MANAGER] say ok")
    assert state.read_json(path).get("human_attached_at") in (None, 0)
    assert json.loads((paths.PANE_SENDS / "s1.json").read_text())["pending"] == []


def test_a_wrapped_relay_does_not_arm(hooked, monkeypatch):
    path = _worker(hooked, tasked_at=time.time(), prompts_seen=1)
    _stamp()
    _submit(monkeypatch,
            '\n\n<pasted_content id="bd31">\n[MANAGER] a long instruction\n'
            '</pasted_content id="bd31">\n')
    assert state.read_json(path).get("human_attached_at") in (None, 0)


def test_an_unstamped_marker_prompt_still_does_not_arm(hooked, monkeypatch):
    """The belt: a typer added later that forgets to stamp is still caught."""
    path = _worker(hooked, tasked_at=time.time(), prompts_seen=1)
    _submit(monkeypatch, "[bootlite watchdog] Your manager session is gone")
    assert state.read_json(path).get("human_attached_at") in (None, 0)


def test_an_unstamped_wrapped_marker_prompt_does_not_arm(hooked, monkeypatch):
    path = _worker(hooked, tasked_at=time.time(), prompts_seen=1)
    _submit(monkeypatch,
            '\n\n<pasted_content id="7572">\n[bootlite watchdog] gone\n'
            '</pasted_content id="7572">\n')
    assert state.read_json(path).get("human_attached_at") in (None, 0)


def test_his_own_typing_arms_the_mode(hooked, monkeypatch):
    path = _worker(hooked, tasked_at=time.time(), prompts_seen=1)
    before = time.time()
    _submit(monkeypatch, "wait, stop and explain what you just did")
    assert state.read_json(path)["human_attached_at"] >= before


def _newest_done():
    records = [state.read_json(p) for p in paths.DONE.rglob("*.json")]
    return max(records, key=lambda r: r["completed_at"])


def test_his_typed_line_stamps_the_next_done_and_a_relay_does_not(hooked, monkeypatch):
    from dockwright.mcp_server import worker_done_impl

    path = _worker(hooked, tasked_at=time.time(), prompts_seen=1)
    _submit(monkeypatch, "why does it page every time?")
    typed = state.read_json(path)["input_at"]
    worker_done_impl("s1", "PARKED — x\nanswered him in the pane")
    assert _newest_done()["engineer_input_at"] == typed
    _stamp()
    _submit(monkeypatch, "[MANAGER] status?")
    worker_done_impl("s1", "PARKED — x\nanswered the manager")
    assert "engineer_input_at" not in _newest_done()


def test_two_queued_relays_consume_two_stamps_and_the_third_submit_arms(hooked, monkeypatch):
    path = _worker(hooked, tasked_at=time.time(), prompts_seen=1)
    _stamp(count=2)
    _submit(monkeypatch, "[MANAGER] first")
    _submit(monkeypatch, "[MANAGER] second")
    assert state.read_json(path).get("human_attached_at") in (None, 0)
    _submit(monkeypatch, "now mine")
    assert state.read_json(path)["human_attached_at"] > 0


def test_a_stamp_past_its_ttl_no_longer_eats_his_message(hooked, monkeypatch):
    from dockwright import stale_monitor
    path = _worker(hooked, tasked_at=time.time(), prompts_seen=1)
    _stamp(age=stale_monitor.AUTOMATION_STAMP_TTL_SEC + 60)
    _submit(monkeypatch, "mine")
    assert state.read_json(path)["human_attached_at"] > 0


def test_handback_clears_the_mode_and_blocks_the_prompt(hooked, monkeypatch, capsys):
    path = _worker(hooked, tasked_at=time.time(), human_attached_at=time.time())
    _submit(monkeypatch, "handback")
    record = state.read_json(path)
    assert record["human_attached_at"] is None
    assert "human_attached_release" not in record
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"] == "block"
    assert "HANDBACK" in payload["reason"]
    assert "manager has been told" not in payload["reason"]


def test_handback_leaves_the_record_where_a_handback_should_leave_it(hooked, monkeypatch, capsys):
    """A blocked prompt runs no turn and fires no Stop hook, so nothing may move
    state to processing — or the lane types into the pane he just left."""
    path = _worker(hooked, tasked_at=time.time(), human_attached_at=time.time(),
                   state="idle", processing_since=123.0)
    _submit(monkeypatch, "  HandBack  ")
    capsys.readouterr()
    record = state.read_json(path)
    assert record["state"] == "idle"
    assert record["processing_since"] == 123.0


def test_an_ordinary_prompt_still_marks_processing(hooked, monkeypatch):
    path = _worker(hooked, tasked_at=time.time(), prompts_seen=1)
    _submit(monkeypatch, "carry on")
    assert state.read_json(path)["state"] == "processing"


HARNESS_PROMPTS = {
    "task-notification": (
        "<task-notification>\n<task-id>bgd5binlu</task-id>\n"
        "<tool-use-id>toolu_01CiRKqkN7dWMpgWUK24in9v</tool-use-id>\n"
        "<status>completed</status>\n</task-notification>"),
    "subagent-hand-back": (
        '<agent-message from="a4a00fd051c29536e">\n[Subagent hand-back] The text below '
        "is the final report of a subagent this session delegated to.\n  pong\n"
        "</agent-message>"),
    "cross-session-message": (
        '<cross-session-message from="uds:/tmp/cc-socks/32124.sock" from-name="manager-3e" '
        'from-mode="prompting">\nWrong manager.\n</cross-session-message>'),
    "mcp-channel": '<channel source="chat">\nnew message in #ops\n</channel>',
    "system-notification": (
        "[SYSTEM NOTIFICATION - NOT USER INPUT]\nThis is an automated background-task "
        "event, NOT a message from the user.\n\n<task-notification>\n<task-id>b1</task-id>\n"
        "</task-notification>"),
    "usage-limit-reset": (
        "Your claude.ai usage limit has reset. Continue the task you were working on when "
        "the limit was reached; do not repeat work that is already complete."),
    "usage-available-early": (
        "Your claude.ai usage is available again before the usage-limit reset. Continue the "
        "task you were working on when the limit was reached; do not repeat work that is "
        "already complete."),
    "loop-first-tick": (
        "# Autonomous loop check\n\nYou're being invoked on a timer while the user is away "
        "or occupied."),
    "loop-tick": (
        "# Autonomous loop tick (dynamic pacing)\n\nRun the autonomous check using the loop "
        "instructions established earlier in this conversation."),
    "loop-md-tick": (
        "# /loop tick — loop.md tasks\n\nWork the tasks from the loop.md contents "
        "established earlier in this conversation."),
    "ultraplan-approved": (
        "The user approved the ultraplan in the browser and chose to implement it in this "
        "session. The plan below is the cloud session’s output."),
    "missed-one-shot": (
        "The following one-shot scheduled task was missed while Claude was not running. "
        "It has already been removed from .claude/scheduled_tasks.json."),
}


def _transcript(hooked, *tool_uses):
    path = hooked / "transcript.jsonl"
    entries = [
        {"type": "user", "message": {"role": "user", "content": "the brief"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": f"toolu_{n}", "name": name, "input": tool_input}
            for n, (name, tool_input) in enumerate(tool_uses)]}},
    ]
    path.write_text("".join(json.dumps(e, separators=(",", ":")) + "\n" for e in entries))
    return str(path)


@pytest.mark.parametrize("prompt", HARNESS_PROMPTS.values(), ids=HARNESS_PROMPTS.keys())
def test_a_harness_prompt_does_not_arm_the_mode(hooked, monkeypatch, prompt):
    path = _worker(hooked, tasked_at=time.time(), prompts_seen=1)
    _submit(monkeypatch, prompt)
    assert state.read_json(path).get("human_attached_at") in (None, 0)


def test_an_envelope_no_list_names_does_not_arm_either(hooked, monkeypatch):
    path = _worker(hooked, tasked_at=time.time(), prompts_seen=1)
    _submit(monkeypatch, '<background-digest id="d1">\nthree jobs finished\n</background-digest>')
    assert state.read_json(path).get("human_attached_at") in (None, 0)


@pytest.mark.parametrize("source", ["system", "loop_wakeup", "schedule_wakeup", "sdk",
                                    "poll_event", "a-kind-added-later"])
def test_a_source_other_than_the_composer_never_arms(hooked, monkeypatch, source):
    path = _worker(hooked, tasked_at=time.time(), prompts_seen=1)
    _submit(monkeypatch, "keep going with the review", source=source)
    assert state.read_json(path).get("human_attached_at") in (None, 0)


def test_a_composer_source_does_not_rescue_a_harness_envelope(hooked, monkeypatch):
    path = _worker(hooked, tasked_at=time.time(), prompts_seen=1)
    _submit(monkeypatch, HARNESS_PROMPTS["task-notification"], source="user")
    assert state.read_json(path).get("human_attached_at") in (None, 0)


def test_a_composer_source_leaves_his_line_arming(hooked, monkeypatch):
    path = _worker(hooked, tasked_at=time.time(), prompts_seen=1)
    _submit(monkeypatch, "wait, stop", source="user")
    assert state.read_json(path)["human_attached_at"] > 0


@pytest.mark.parametrize("tool,tool_input", [
    ("ScheduleWakeup", {"delaySeconds": 1200, "reason": "fallback", "noop": True,
                        "prompt": "check whether the review subagent finished"}),
    ("CronCreate", {"cron": "7 * * * *", "prompt": "check whether the review subagent finished"}),
])
def test_a_prompt_the_session_scheduled_does_not_arm(hooked, monkeypatch, tool, tool_input):
    path = _worker(hooked, tasked_at=time.time(), prompts_seen=1)
    transcript = _transcript(hooked, (tool, tool_input))
    _submit(monkeypatch, "check whether the review subagent finished",
            transcript_path=transcript)
    assert state.read_json(path).get("human_attached_at") in (None, 0)


def test_the_same_words_arm_when_nothing_scheduled_them(hooked, monkeypatch):
    path = _worker(hooked, tasked_at=time.time(), prompts_seen=1)
    transcript = _transcript(
        hooked, ("ScheduleWakeup", {"delaySeconds": 600, "reason": "r", "noop": True,
                                    "prompt": "something else entirely"}),
        ("Bash", {"command": "echo check whether the review subagent finished"}))
    _submit(monkeypatch, "check whether the review subagent finished",
            transcript_path=transcript)
    assert state.read_json(path)["human_attached_at"] > 0


def test_a_missing_transcript_leaves_his_line_arming(hooked, monkeypatch):
    path = _worker(hooked, tasked_at=time.time(), prompts_seen=1)
    _submit(monkeypatch, "wait, stop", transcript_path=str(hooked / "missing.jsonl"))
    assert state.read_json(path)["human_attached_at"] > 0


def test_hand_backs_and_notifications_after_the_brief_leave_the_worker_unattached(
        hooked, monkeypatch):
    path = _worker(hooked, last_turn_at=None)
    _submit(monkeypatch, "/ticket-start ABC-123")
    _submit(monkeypatch, HARNESS_PROMPTS["subagent-hand-back"])
    _submit(monkeypatch, HARNESS_PROMPTS["task-notification"])
    record = state.read_json(path)
    assert record.get("human_attached_at") in (None, 0)
    assert record["prompts_seen"] == 3
    _submit(monkeypatch, "wait, stop")
    assert state.read_json(path)["human_attached_at"] > 0


def test_a_harness_prompt_leaves_a_pending_stamp_for_the_send_that_owns_it(
        hooked, monkeypatch):
    path = _worker(hooked, tasked_at=time.time(), prompts_seen=1)
    _stamp()
    _submit(monkeypatch, HARNESS_PROMPTS["task-notification"])
    assert len(json.loads((paths.PANE_SENDS / "s1.json").read_text())["pending"]) == 1
    _submit(monkeypatch, "/dockwright-recap")
    assert state.read_json(path).get("human_attached_at") in (None, 0)
    _submit(monkeypatch, "mine")
    assert state.read_json(path)["human_attached_at"] > 0


def test_a_harness_prompt_does_not_push_his_decay_forward(hooked, monkeypatch):
    armed_at = time.time() - 600
    path = _worker(hooked, tasked_at=time.time(), prompts_seen=2, human_attached_at=armed_at)
    _submit(monkeypatch, HARNESS_PROMPTS["subagent-hand-back"])
    assert state.read_json(path)["human_attached_at"] == armed_at


@pytest.mark.parametrize("prompt", [
    "/dockwright-recap",
    "<3 thanks, carry on",
    "<https://example.test/pr/1> look at this",
    '\n\n<pasted_content id="c1">\n<task-notification>\n<task-id>x</task-id>\n'
    '</task-notification>\nwhy did this fire?\n</pasted_content id="c1">\n',
])
def test_his_own_prompts_still_arm_whatever_they_start_with(hooked, monkeypatch, prompt):
    path = _worker(hooked, tasked_at=time.time(), prompts_seen=1)
    _submit(monkeypatch, prompt)
    assert state.read_json(path)["human_attached_at"] > 0


def test_a_notification_or_hand_back_does_not_move_input_at(hooked, monkeypatch):
    path = _worker(hooked, prompts_seen=1, input_at=1.0)
    _submit(monkeypatch, HARNESS_PROMPTS["task-notification"])
    _submit(monkeypatch, HARNESS_PROMPTS["subagent-hand-back"])
    assert state.read_json(path)["input_at"] == 1.0


def test_his_line_moves_input_at(hooked, monkeypatch):
    path = _worker(hooked, prompts_seen=1, input_at=1.0)
    _submit(monkeypatch, "wait, stop")
    assert state.read_json(path)["input_at"] > 1.0


def test_a_manager_relay_moves_input_at(hooked, monkeypatch):
    path = _worker(hooked, prompts_seen=1, input_at=1.0)
    _submit(monkeypatch, "[MANAGER] do Z instead")
    assert state.read_json(path)["input_at"] > 1.0


def test_handback_keeps_input_at(hooked, monkeypatch):
    path = _worker(hooked, prompts_seen=1)
    _submit(monkeypatch, "wait, stop")
    stamped = state.read_json(path)["input_at"]
    _submit(monkeypatch, "handback")
    assert state.read_json(path)["input_at"] == stamped


# --------------------------------------------------------------------------
# The other consumers of the mode
# --------------------------------------------------------------------------

def test_an_attached_worker_pages_no_finished_silently(hooked, tmp_path):
    now = time.time()
    _worker(hooked, human_attached_at=now)
    entry = paths.TURN_ENDS / "mgr" / "s1-1.json"
    entry.parent.mkdir(parents=True, exist_ok=True)
    payload = {"sid": "s1", "agent": "worker", "name": "alpha",
               "completed_at": now - 2000}
    entry.write_text(json.dumps(payload))
    assert classify_turn_end(payload, entry, "mgr", None, now,
                             lambda: None) == TURN_END_SUPPRESS


def test_the_same_worker_unattached_pages_finished_silently(hooked, tmp_path):
    now = time.time()
    _worker(hooked)
    entry = paths.TURN_ENDS / "mgr" / "s1-1.json"
    entry.parent.mkdir(parents=True, exist_ok=True)
    payload = {"sid": "s1", "agent": "worker", "name": "alpha",
               "completed_at": now - 2000}
    entry.write_text(json.dumps(payload))
    assert classify_turn_end(payload, entry, "mgr", None, now,
                             lambda: None) == TURN_END_EMIT


# --------------------------------------------------------------------------
# The manager's view and the relay
# --------------------------------------------------------------------------

from dockwright import mcp_server


@pytest.fixture
def relay(hooked, monkeypatch):
    sent = []
    def _typed(wid, text):
        sent.append((wid, text))
        return True

    monkeypatch.setattr(mcp_server, "_type_unguarded", _typed)
    monkeypatch.setattr(mcp_server, "_resolve_live_worker_window",
                        lambda record: record.get("window_id") or "%p1")
    return sent


def test_list_workers_carries_the_live_mode(hooked, monkeypatch):
    from dockwright import stale_monitor
    _worker(hooked, human_attached_at=time.time())
    monkeypatch.setattr(mcp_server, "find_session_log", lambda sid, runtime="claude": None)
    row = mcp_server.list_workers_impl()[0]
    assert row["human_attached"] is True


def test_list_workers_reports_a_decayed_stamp_as_off(hooked, monkeypatch):
    from dockwright import stale_monitor
    _worker(hooked,
            human_attached_at=time.time() - stale_monitor.HUMAN_ATTACHED_TTL_SEC - 1)
    monkeypatch.setattr(mcp_server, "find_session_log", lambda sid, runtime="claude": None)
    row = mcp_server.list_workers_impl()[0]
    assert row["human_attached"] is False


def test_the_managers_relay_into_an_attached_pane_is_refused_whatever_its_input_line_holds(hooked, monkeypatch,
                                                                                            relay):
    for screen in (BUSY_PANE, IDLE_PANE):
        _worker(hooked, human_attached_at=time.time(), window_id="%p1")
        monkeypatch.setattr(mcp_server, "_capture_text", lambda wid, s=screen: s)
        result = mcp_server.send_manager_to_worker_impl("alpha", "status?")
        assert result["status"] == "human_attached" and result["delivered"] is False
        assert result["worker"] == "alpha" and len(result["until"]) == 5
        assert "set_human_attached(\"alpha\", false)" in result["explanation"]
        assert relay == [], "nothing the manager sends is typed into a pane he holds"


def test_his_own_record_into_an_attached_idle_pane_is_typed_and_into_a_busy_one_is_not(hooked, monkeypatch, relay):
    _worker(hooked, human_attached_at=time.time(), window_id="%p1")
    monkeypatch.setattr(mcp_server, "_capture_text", lambda wid: BUSY_PANE)
    busy = mcp_server.deliver_to_pane("alpha", "approve", "[TOPIC decision.verdict-r1 from engineer] ",
                                      mcp_server.idle_line_policy)
    assert busy["status"] == "human_attached" and relay == []
    assert busy["line"] == "draft"
    monkeypatch.setattr(mcp_server, "_capture_text", lambda wid: IDLE_PANE)
    idle = mcp_server.deliver_to_pane("alpha", "approve", "[TOPIC decision.verdict-r1 from engineer] ",
                                      mcp_server.idle_line_policy)
    assert idle["status"] == "delivered" and relay[0][1].startswith("[TOPIC decision.verdict-r1 from engineer] ")


def test_a_managers_refusal_reads_no_line(hooked, monkeypatch, relay):
    _worker(hooked, human_attached_at=time.time(), window_id="%p1")
    monkeypatch.setattr(mcp_server, "_capture_text", lambda wid: BUSY_PANE)
    result = mcp_server.send_manager_to_worker_impl("alpha", "status?")
    assert result["status"] == "human_attached" and "line" not in result


def test_a_relay_into_a_pane_whose_flag_dropped_delivers(hooked, monkeypatch, relay):
    from dockwright import stale_monitor
    _worker(hooked, human_attached_at=time.time() - stale_monitor.HUMAN_ATTACHED_TTL_SEC - 1, window_id="%p1")
    monkeypatch.setattr(mcp_server, "_capture_text", lambda wid: BUSY_PANE)
    assert mcp_server.send_manager_to_worker_impl("alpha", "status?")["status"] == "delivered"
    assert relay and relay[0][1].startswith("[MANAGER] ")


def test_a_relay_into_an_unattached_busy_pane_delivers_exactly_as_today(hooked, monkeypatch, relay):
    _worker(hooked, window_id="%p1")
    monkeypatch.setattr(mcp_server, "_capture_text", lambda wid: BUSY_PANE)
    result = mcp_server.send_manager_to_worker_impl("alpha", "status?")
    assert result["status"] == "delivered"
    assert relay, "an ordinary worker's relay path is unchanged"


def test_a_delivered_relay_leaves_a_stamp_the_hook_will_consume(hooked, monkeypatch, relay):
    _worker(hooked, window_id="%p1")
    monkeypatch.setattr(mcp_server, "_capture_text", lambda wid: IDLE_PANE)
    mcp_server.send_manager_to_worker_impl("alpha", "status?")
    assert json.loads((paths.PANE_SENDS / "s1.json").read_text())["pending"]


def test_the_manager_can_arm_and_release_the_mode(hooked, monkeypatch):
    path = _worker(hooked)
    mcp_server.set_human_attached_impl("alpha", True)
    assert state.read_json(path)["human_attached_at"] > 0
    mcp_server.set_human_attached_impl("alpha", False)
    record = state.read_json(path)
    assert record["human_attached_at"] is None
    assert "human_attached_release" not in record


@pytest.mark.parametrize("tool", ["kill_worker", "kill_child"])
def test_a_kill_of_an_attached_worker_is_refused_with_the_way_out(hooked, monkeypatch, tool):
    _worker(hooked, human_attached_at=time.time())
    calls = []
    monkeypatch.setattr(mcp_server, "kill_worker_impl", lambda worker, dry_run=False: calls.append(worker) or {})
    with pytest.raises(ValueError, match=r"refused: alpha is human-attached") as err:
        getattr(mcp_server, tool)("alpha")
    assert "set_human_attached(\"alpha\", false)" in str(err.value) and "40 min" in str(err.value)
    assert calls == []


@pytest.mark.parametrize("tool", ["kill_worker", "kill_child"])
def test_a_kill_after_the_flag_dropped_goes_through(hooked, monkeypatch, tool):
    from dockwright import stale_monitor
    _worker(hooked, human_attached_at=time.time() - stale_monitor.HUMAN_ATTACHED_TTL_SEC - 1)
    calls = []
    monkeypatch.setattr(mcp_server, "kill_worker_impl", lambda worker, dry_run=False: calls.append(worker) or {})
    getattr(mcp_server, tool)("alpha")
    assert calls == ["alpha"]


def test_a_kill_of_an_unknown_name_still_reaches_the_impl(hooked, monkeypatch):
    calls = []
    monkeypatch.setattr(mcp_server, "kill_worker_impl", lambda worker, dry_run=False: calls.append(worker) or {})
    mcp_server.kill_worker("ghost")
    assert calls == ["ghost"]


def test_list_workers_names_when_the_flag_drops(hooked, monkeypatch):
    from dockwright import stale_monitor
    path = _worker(hooked)
    monkeypatch.setattr(mcp_server, "find_session_log", lambda sid, runtime="claude": None)
    assert mcp_server.list_workers_impl()[0]["human_attached_until"] is None
    mcp_server.set_human_attached_impl("alpha", True)
    row = mcp_server.list_workers_impl()[0]
    stamp = state.read_json(path)["human_attached_at"]
    expected = datetime.fromtimestamp(stamp + stale_monitor.HUMAN_ATTACHED_TTL_SEC).astimezone()
    assert row["human_attached"] is True
    assert row["human_attached_until"] == expected.isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Round-2 fixes: what may be stamped, and the file four processes share
# --------------------------------------------------------------------------

def test_a_custom_slash_command_is_stamped(stale, driver):
    """A custom or skill slash command DOES fire UserPromptSubmit — only the
    built-in control commands are hookless — so stamping is default-deny."""
    stale._send_text("%w1", "/spikecmd do the thing", {"input": {}}, "claude", "w1")
    assert json.loads((stale.PANE_SENDS / "w1.json").read_text())["pending"]


@pytest.mark.parametrize("text", ["/model claude-sonnet-5", "/effort high"])
def test_the_two_hookless_control_commands_are_not_stamped(stale, driver, text):
    stale._send_text("%w1", text, {"input": {}}, "claude", "w1")
    assert not (stale.PANE_SENDS / "w1.json").exists()


def test_a_send_the_driver_refused_leaves_no_stamp(stale, monkeypatch):
    """A stamp with no submit behind it eats his next message, which is the
    original defect restored."""
    class RefusingDriver(PaneDriver):
        def send_text_checked(self, window_id, text):
            return False

    drv = RefusingDriver()
    monkeypatch.setattr(stale, "_get_driver", lambda: drv)
    assert stale._send_text("%w1", "[MANAGER] resume", {"input": {}}, "claude", "w1") is False
    assert not (stale.PANE_SENDS / "w1.json").exists()


def test_concurrent_stamps_do_not_lose_entries(stale, driver):
    """Four processes read-modify-write this file. Unlocked, entries vanish; a
    vanished entry arms the mode on a worker nobody is at."""
    import threading
    writers, each = 4, 2
    barrier = threading.Barrier(writers)

    def hammer():
        barrier.wait()
        for _ in range(each):
            stale._stamp_pane_send("w1")

    threads = [threading.Thread(target=hammer) for _ in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    pending = json.loads((stale.PANE_SENDS / "w1.json").read_text())["pending"]
    assert len(pending) == writers * each, f"lost {writers * each - len(pending)} stamps"


# --------------------------------------------------------------------------
# Round-3 fixes: the lock must survive a consumer, and every stamper confirms
# --------------------------------------------------------------------------

def test_the_lock_survives_a_consumer_running_beside_stampers(hooked, monkeypatch):
    """`flock` holds an inode, not a path. Unlinking the lock file from inside
    the critical section lets the next caller create a fresh inode and walk
    straight in, which loses stamps in both directions."""
    import threading
    from dockwright import hooks, stale_monitor
    monkeypatch.setattr(stale_monitor, "AUTOMATION_STAMP_MAX", 10_000)
    sid = "race"
    stamps, consumes = 120, 60
    barrier = threading.Barrier(2)

    def stamper():
        barrier.wait()
        for _ in range(stamps):
            stale_monitor._stamp_pane_send(sid, root=paths.PANE_SENDS)

    stale_monitor._stamp_pane_send(sid, root=paths.PANE_SENDS)
    lock_inode = (paths.PANE_SENDS / f"{sid}.lock").stat().st_ino
    written = stamps + 1
    consumed = []

    def consumer():
        barrier.wait()
        for _ in range(consumes):
            consumed.append(hooks._consume_pane_send(sid, time.time()))

    threads = [threading.Thread(target=stamper), threading.Thread(target=consumer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    left = json.loads((paths.PANE_SENDS / f"{sid}.json").read_text())["pending"]
    taken = sum(1 for c in consumed if c)
    assert taken + len(left) == written, (
        f"every stamp is either consumed or still pending: "
        f"{taken} taken + {len(left)} left != {written} written")
    while hooks._consume_pane_send(sid, time.time()):
        pass
    assert (paths.PANE_SENDS / f"{sid}.lock").stat().st_ino == lock_inode, (
        "the lock file must keep its inode across a run that empties the list: "
        "flock holds an inode, not a path, so unlinking it hands the next caller "
        "a fresh one and excludes nobody")


def test_a_relay_the_terminal_refused_leaves_no_stamp(hooked, monkeypatch):
    _worker(hooked, window_id="%p1")
    monkeypatch.setattr(mcp_server, "_resolve_live_worker_window", lambda r: "%p1")
    monkeypatch.setattr(mcp_server, "_capture_text", lambda wid: IDLE_PANE)

    class RefusingDriver:
        def send_text_checked(self, window_id, text):
            return False

    monkeypatch.setattr(mcp_server, "get_driver", lambda: RefusingDriver())
    result = mcp_server.send_manager_to_worker_impl("alpha", "status?")
    assert result["status"] == "delivered", (
        "delivery reporting is deliberately unchanged here: manager.core.md's "
        "no-silent-inbox rule is about a DEAD window, which still raises")
    assert not (paths.PANE_SENDS / "s1.json").exists(), (
        "an unconfirmed send must not leave a stamp to eat his next message")


def test_handback_clears_a_pending_stamp(hooked, monkeypatch, capsys):
    path = _worker(hooked, prompts_seen=2, human_attached_at=time.time())
    _stamp()
    _submit(monkeypatch, "handback")
    capsys.readouterr()
    assert json.loads((paths.PANE_SENDS / "s1.json").read_text())["pending"] == []
    _submit(monkeypatch, "back to you now")
    assert state.read_json(path)["human_attached_at"] > 0, (
        "a stamp left over from before the hand-back must not eat his next line")


def test_a_malformed_pane_sends_file_heals_instead_of_wedging(hooked, monkeypatch):
    path = _worker(hooked, prompts_seen=2)
    paths.PANE_SENDS.mkdir(parents=True, exist_ok=True)
    (paths.PANE_SENDS / "s1.json").write_text("{not json at all")
    _submit(monkeypatch, "mine")
    assert json.loads((paths.PANE_SENDS / "s1.json").read_text())["pending"] == []
    assert state.read_json(path)["human_attached_at"] > 0


def test_a_record_predating_this_change_arms_on_his_first_line(hooked, monkeypatch):
    """A worker already running when this ships has last_turn_at but no counter.
    The fallback is what stops his first line being spent priming the counter.
    It does NOT cover resume_worker: a resumed session registers fresh, with no
    counter and last_turn_at reset, so its first line is spent like a spawn's."""
    path = _worker(hooked, last_turn_at="2026-09-22T09:00:00.000Z")
    assert state.read_json(path).get("prompts_seen") is None
    _submit(monkeypatch, "carry on from where you were")
    assert state.read_json(path)["human_attached_at"] > 0
