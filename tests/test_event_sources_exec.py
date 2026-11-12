import json
import subprocess
import time

import pytest

from dockwright import config, event_sources, events
from dockwright.event_sources import ATTEMPTED, BUDGET_SPENT, FAILED
from dockwright.event_sources import exec as exec_source
from dockwright.events import Event

NOW = 1792700000.0
DEFAULT = ('[events]\nenabled = true\n[events.sources.exec]\nevery_sec = 120\n[[events.sources.exec.commands]]\n'
           'argv = ["cmd-a"]\n[[events.sources.exec.commands]]\nargv = ["cmd-b", "--json"]\n')
INCIDENT = {"id": "oncall:Q1:1.0:2026-10-28T03:12:40Z", "kind": "incident", "match": {"incident": "Q1"},
            "summary": "INCIDENT Q1 TEAM: backlog *q* - *1.0*",
            "payload": {"title": "TEAM: backlog *q* - *1.0*", "count": "1.0", "status": "triggered", "url": "https://x"}}


@pytest.fixture
def exec_cfg(tmp_path, monkeypatch):
    cfg = tmp_path / "events.toml"
    cfg.write_text(DEFAULT)
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))

    def write(text):
        cfg.write_text(text)

    return write


class FakeCall:
    def __init__(self, by_argv0=None, raises=None):
        self.calls: list[tuple[list[str], float]] = []
        self.by_argv0 = by_argv0 or {}
        self.raises = raises or {}

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        if argv[0] in self.raises:
            raise self.raises[argv[0]]
        return self.by_argv0.get(argv[0], (0, b"", b""))


def _line(obj) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def _lines(*objs) -> bytes:
    return b"".join(_line(o) for o in objs)


def _shift(start, end, issued, ts=NOW - 100, **payload):
    body = {"start": start, "end": end, "issued": issued, **payload}
    events.append(Event(f"exec:shift:{start}:{end}:{issued}", ts, "exec", "shift", {"user": "U1", "schedule": "S1"},
                        "", f"SHIFT {start} → {end}", body))


def _poll(fake, cursor=None, until=None):
    return exec_source.poll([], {}, cursor or {}, {}, NOW, call=fake, until=until)


def _argv0s(fake):
    return [c[0][0] for c in fake.calls]


def test_each_json_line_of_each_command_becomes_an_exec_event_with_the_id_prefixed_and_ts_by_command_index(exec_cfg):
    fake = FakeCall({"cmd-a": (0, _lines(INCIDENT, {"id": "b", "kind": "k"}, {"id": "c", "kind": "k"}), b""),
                     "cmd-b": (0, _lines({"id": "d", "kind": "k"}), b"")})
    evs, cur = _poll(fake)
    assert len(evs) == 4
    first = evs[0]
    assert first.id == "exec:oncall:Q1:1.0:2026-10-28T03:12:40Z" and first.source == "exec"
    assert first.kind == "incident" and first.match == {"incident": "Q1"} and first.cursor_key == ""
    assert first.summary == "INCIDENT Q1 TEAM: backlog *q* - *1.0*" and first.payload == INCIDENT["payload"]
    assert first.ts == NOW and evs[3].id == "exec:d" and evs[3].ts == NOW + 0.001
    assert cur[BUDGET_SPENT] == [] and cur[FAILED] == []
    assert cur["last_run"] == {"cmd-a": NOW, "cmd-b --json": NOW}
    assert [c[0] for c in fake.calls] == [["cmd-a"], ["cmd-b", "--json"]]


def test_a_non_zero_exit_keeps_its_lines_as_events_and_marks_the_command_failed(exec_cfg):
    fake = FakeCall({"cmd-a": (1, _lines({"id": "a", "kind": "k"}, {"id": "b", "kind": "k"}), b"gate fetch failed\n")})
    evs, cur = _poll(fake)
    assert [e.id for e in evs] == ["exec:a", "exec:b"]
    assert cur[FAILED] == [["cmd-a", "exit 1: gate fetch failed"]]


def test_a_line_that_is_not_json_is_a_failure_with_its_line_number_and_the_valid_lines_stay(exec_cfg):
    fake = FakeCall({"cmd-a": (0, _line({"id": "a", "kind": "k"}) + b"not json\n" + _line({"id": "b", "kind": "k"}), b"")})
    evs, cur = _poll(fake)
    assert [e.id for e in evs] == ["exec:a", "exec:b"]
    assert cur[FAILED] == [["cmd-a", "line 2: not a JSON object with string id and kind"]]


def test_a_line_whose_id_or_kind_is_not_a_lowercase_string_is_a_failure_not_a_crash(exec_cfg):
    fake = FakeCall({"cmd-a": (0, _lines({"id": 5, "kind": "incident"}, {"id": "x", "kind": "Incident"}), b"")})
    evs, cur = _poll(fake)
    assert evs == [] and len(cur[FAILED]) == 2 and all(f[0] == "cmd-a" for f in cur[FAILED])


def test_a_non_utf8_byte_fails_its_line_and_keeps_the_rest(exec_cfg):
    fake = FakeCall({"cmd-a": (0, b'{"id":"a","kind":"k"}\n\xff\xfe\n{"id":"b","kind":"k"}\n', b"")})
    evs, cur = _poll(fake)
    assert [e.id for e in evs] == ["exec:a", "exec:b"]
    assert len(cur[FAILED]) == 1 and cur[FAILED][0][1].startswith("line 2")


def test_missing_summary_and_a_non_table_match_give_empty_fields(exec_cfg):
    fake = FakeCall({"cmd-a": (0, _lines({"id": "a", "kind": "k", "match": "nope", "payload": [1]},
                                         {"id": "b", "kind": "k", "match": {"a": 1, "b": "ok"}, "summary": "x" * 300}), b"")})
    evs, _ = _poll(fake)
    assert evs[0].match == {} and evs[0].summary == "" and evs[0].payload == {}
    assert evs[1].match == {"b": "ok"} and len(evs[1].summary) == 200


def test_a_timeout_keeps_the_partial_stdout_lines_and_marks_the_command_failed(exec_cfg):
    fake = FakeCall(raises={"cmd-a": subprocess.TimeoutExpired(["cmd-a"], 30, output=_line({"id": "a", "kind": "k"}))})
    evs, cur = _poll(fake)
    assert [e.id for e in evs] == ["exec:a"]
    assert cur[FAILED][0][0] == "cmd-a" and cur[FAILED][0][1].startswith("timeout after")
    fake = FakeCall(raises={"cmd-a": subprocess.TimeoutExpired(["cmd-a"], 30, output=None)})
    evs, cur = _poll(fake)
    assert evs == [] and cur[FAILED][0][1].startswith("timeout after")


def test_an_os_error_marks_the_command_failed_and_the_next_command_still_runs(exec_cfg):
    fake = FakeCall(raises={"cmd-a": OSError("no such file")})
    evs, cur = _poll(fake)
    assert evs == [] and cur[FAILED] == [["cmd-a", "OSError: no such file"]]
    assert _argv0s(fake) == ["cmd-a", "cmd-b"]


def test_empty_stdout_and_exit_zero_give_nothing(exec_cfg):
    fake = FakeCall()
    evs, cur = _poll(fake)
    assert evs == [] and cur[FAILED] == []
    assert cur["last_run"] == {"cmd-a": NOW, "cmd-b --json": NOW}


def test_every_sec_holds_a_command_back_until_its_own_cadence(exec_cfg):
    exec_cfg(DEFAULT + "every_sec = 3600\n")
    fake = FakeCall()
    _, cur = _poll(fake, cursor={"last_run": {"cmd-b --json": NOW - 100}})
    assert _argv0s(fake) == ["cmd-a"] and cur["last_run"]["cmd-b --json"] == NOW - 100
    fake = FakeCall()
    _, cur = _poll(fake, cursor={"last_run": {"cmd-b --json": NOW - 3600}})
    assert _argv0s(fake) == ["cmd-a", "cmd-b"] and cur["last_run"]["cmd-b --json"] == NOW


def test_attempted_names_the_commands_this_poll_ran(exec_cfg):
    exec_cfg(DEFAULT + "every_sec = 3600\n")
    _, cur = _poll(FakeCall(), cursor={"last_run": {"cmd-b --json": NOW - 100}})
    assert cur[ATTEMPTED] == ["cmd-a"]
    _, cur = _poll(FakeCall(), cursor={"last_run": {"cmd-b --json": NOW - 3600}})
    assert cur[ATTEMPTED] == ["cmd-a", "cmd-b --json"]
    _, cur = _poll(FakeCall(raises={"cmd-a": OSError("no such file")}))
    assert cur[ATTEMPTED] == ["cmd-a", "cmd-b --json"]
    assert cur[FAILED] == [["cmd-a", "OSError: no such file"]]


def test_a_present_stop_file_skips_the_command_without_a_failure(exec_cfg, tmp_path):
    stop = tmp_path / "stop"
    exec_cfg(DEFAULT.replace('argv = ["cmd-a"]\n', f'argv = ["cmd-a"]\nstop_file = "{stop}"\n'))
    stop.write_text("")
    fake = FakeCall()
    _, cur = _poll(fake)
    assert _argv0s(fake) == ["cmd-b"] and cur[FAILED] == []
    stop.unlink()
    fake = FakeCall()
    _poll(fake)
    assert _argv0s(fake) == ["cmd-a", "cmd-b"]


WHILE = DEFAULT + 'while = { kind = "shift" }\n'


def test_while_runs_the_command_only_inside_a_window_of_the_newest_issued_set(exec_cfg):
    exec_cfg(WHILE)
    fake = FakeCall()
    _poll(fake)
    assert _argv0s(fake) == ["cmd-a"]
    _shift("2026-10-20T00:00:00Z", "2026-11-03T00:00:00Z", "2026-10-22T06")
    fake = FakeCall()
    _poll(fake)
    assert _argv0s(fake) == ["cmd-a", "cmd-b"]
    _shift("2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z", "2026-10-22T07")
    fake = FakeCall()
    _poll(fake)
    assert _argv0s(fake) == ["cmd-a"]


def test_while_takes_only_the_newest_issued_set(exec_cfg):
    exec_cfg(WHILE)
    _shift("2026-10-20T00:00:00Z", "2026-11-03T00:00:00Z", "2026-10-22T05")
    events.append(Event("exec:shift:none:2026-10-22T06", NOW - 50, "exec", "shift", {"user": "U1", "schedule": "S1"}, "",
                        "SHIFT none", {"issued": "2026-10-22T06"}))
    fake = FakeCall()
    _poll(fake)
    assert _argv0s(fake) == ["cmd-a"]
    _shift("2026-10-20T00:00:00Z", "2026-11-03T00:00:00Z", "2026-10-22T07")
    fake = FakeCall()
    _poll(fake)
    assert _argv0s(fake) == ["cmd-a", "cmd-b"]


def test_a_window_event_without_a_string_issued_is_ignored_not_fatal(exec_cfg):
    exec_cfg(WHILE)
    events.append(Event("exec:shift:bad-1", NOW - 50, "exec", "shift", {}, "", "s",
                        {"start": "2026-10-20T00:00:00Z", "end": "2026-11-03T00:00:00Z"}))
    events.append(Event("exec:shift:bad-2", NOW - 40, "exec", "shift", {}, "", "s",
                        {"start": "2026-10-20T00:00:00Z", "end": "2026-11-03T00:00:00Z", "issued": 5}))
    fake = FakeCall()
    _poll(fake)
    assert _argv0s(fake) == ["cmd-a"]
    _shift("2026-10-20T00:00:00Z", "2026-11-03T00:00:00Z", "2026-10-22T06")
    fake = FakeCall()
    _poll(fake)
    assert _argv0s(fake) == ["cmd-a", "cmd-b"]


def test_while_match_filters_the_window_events(exec_cfg):
    exec_cfg(DEFAULT + 'while = { kind = "shift", match = { schedule = "S2" } }\n')
    _shift("2026-10-20T00:00:00Z", "2026-11-03T00:00:00Z", "2026-10-22T06")
    fake = FakeCall()
    _poll(fake)
    assert _argv0s(fake) == ["cmd-a"]


def test_a_window_printed_by_an_earlier_command_of_the_same_poll_opens_a_later_one(exec_cfg):
    exec_cfg(WHILE)
    window = {"id": "shift:2026-10-20T00:00:00Z:2026-11-03T00:00:00Z:2026-10-22T06", "kind": "shift",
              "payload": {"start": "2026-10-20T00:00:00Z", "end": "2026-11-03T00:00:00Z", "issued": "2026-10-22T06"}}
    fake = FakeCall({"cmd-a": (0, _line(window), b"")})
    evs, _ = _poll(fake)
    assert _argv0s(fake) == ["cmd-a", "cmd-b"] and len(evs) == 1


def test_a_malformed_command_is_a_failure_and_the_rest_run(exec_cfg):
    exec_cfg('[events]\nenabled = true\n[events.sources.exec]\n[[events.sources.exec.commands]]\nargv = ["x"]\n'
             'while = "shift"\n[[events.sources.exec.commands]]\nargv = "y --json"\n'
             '[[events.sources.exec.commands]]\nargv = ["cmd-a"]\n')
    fake = FakeCall()
    _, cur = _poll(fake)
    assert _argv0s(fake) == ["cmd-a"]
    assert cur[FAILED] == [["x", "while must be a table with a string kind"],
                           ["commands[1]", "argv must be a list of non-empty strings"]]


def test_an_empty_commands_list_is_a_poll_error(exec_cfg):
    exec_cfg("[events]\nenabled = true\n[events.sources.exec]\nevery_sec = 120\n")
    with pytest.raises(events.PollError, match=r"\[events.sources.exec\] commands is empty"):
        _poll(FakeCall())


def test_the_call_timeout_is_bounded_by_until(exec_cfg):
    fake = FakeCall()
    _poll(fake, until=time.time() + 5)
    assert 1 <= fake.calls[0][1] <= 5
    fake = FakeCall()
    _poll(fake)
    assert fake.calls[0][1] == event_sources.CALL_TIMEOUT_SEC
