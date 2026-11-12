import importlib
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
import time
import types

import pytest

from dockwright import chat_archive, config, event_poll, events, lane_io, paths
from dockwright.event_sources import chat
from tests.lockdown_argv import permission_surface_widened
from tests.test_mcp_tools import fresh_orchestrator_dir  # noqa: F401
from tests.test_pipeline_reconcile import _started_at_design

NOW = 1792700000.0
MAC = NOW - chat.MAC_EPOCH
REF = "E0000000000_1792700000.000100"
THREAD = "1792700000.000050"
RECORD_TABLE = ("CREATE TABLE record (rec_id INTEGER PRIMARY KEY, app_id INTEGER, uuid BLOB, data BLOB, "
                "request_date REAL, request_last_date REAL, delivered_date REAL, presented Bool, style INTEGER, "
                "snooze_fire_date REAL)")
EVENTS = '[events]\nenabled = true\n[events.sources.chat]\napps = ["com.example.chat"]\n'
ARCHIVE = ('[chat_archive]\nenabled = %s\nmcp_url = "https://chat.example/mcp"\nmcp_server = "chat"\n'
           'keychain_service = "kc"\nself_user_id = "U01ME00000"\nwrite_tools = ["w"]\n[chat_archive.tools]\nsearch = "s"\n'
           'read_conversation = "rc"\nread_thread = "rt"\nlist_conversations = "lc"\n')
ARCHIVE_OFF = EVENTS + ARCHIVE % "false"
ARCHIVE_ON = EVENTS + ARCHIVE % "true"


@pytest.fixture
def chat_cfg(tmp_path, monkeypatch):
    cfg = tmp_path / "chat.toml"
    cfg.write_text(ARCHIVE_ON)
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    monkeypatch.setattr(paths, "CHAT_DB", tmp_path / "chat" / "archive.db")
    return cfg


def _sub(after_at, kinds=None, match=None):
    return events.Sub("s1", "K", "design", "chat", match or {}, kinds, events.chat_time(after_at), None, None, None,
                      after_at, None)


LIVE = [_sub(NOW - 100)]


def _plist(subt="Ann Lee", body="hi", titl="Acme", iden=REF) -> bytes:
    req = {k: v for k, v in (("titl", titl), ("subt", subt), ("body", body), ("iden", iden)) if v is not None}
    return plistlib.dumps({"req": req}, fmt=plistlib.FMT_BINARY)


def _make_store(home, record=True):
    db = home / "Library" / "Group Containers" / "group.com.apple.usernoted" / "db2" / "db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("pragma journal_mode=wal")
    con.execute("CREATE TABLE app (app_id INTEGER PRIMARY KEY, identifier TEXT)")
    con.executemany("INSERT INTO app VALUES (?, ?)", [(1, "com.example.chat"), (2, "com.example.other")])
    if record:
        con.execute(RECORD_TABLE)
    con.commit()
    con.close()
    return db


@pytest.fixture
def store(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    db = _make_store(home)

    def add(app_id, delivered, subt="Ann Lee", body="hi", titl="Acme", iden=REF, data=None, rec_id=None, uid=None):
        uid = uid if uid is not None else os.urandom(16)
        blob = data if data is not None else _plist(subt, body, titl, iden)
        con = sqlite3.connect(db)
        con.execute("INSERT INTO record (rec_id, app_id, uuid, data, delivered_date, presented, style) "
                    "VALUES (?, ?, ?, ?, ?, 0, 1)", (rec_id, app_id, uid, blob, delivered))
        con.commit()
        con.close()
        return f"{uid.hex()}:{delivered:.6f}"

    return types.SimpleNamespace(path=db, add=add)


@pytest.fixture
def home(store, monkeypatch):
    importlib.reload(config)
    importlib.reload(paths)
    yield store.path.parents[4]
    monkeypatch.undo()
    importlib.reload(config)
    importlib.reload(paths)


def _sql(path, query, args=()):
    con = sqlite3.connect(path)
    con.execute(query, args)
    con.commit()
    con.close()


def test_the_first_pass_emits_every_row_in_the_store(chat_cfg, store):
    chat_cfg.write_text(ARCHIVE_OFF)
    first, second = store.add(1, MAC - 3600), store.add(1, MAC - 10)
    evs, cursor = chat.poll([], {}, {}, {}, NOW)
    assert sorted(e.id for e in evs) == sorted([f"chat:{first}", f"chat:{second}"])
    assert [e.ts for e in evs] == [NOW, NOW]
    assert cursor["seen"] == sorted([first, second])


def test_a_direct_message_becomes_a_dm_event_with_the_subtitle_as_sender(chat_cfg, store):
    chat_cfg.write_text(ARCHIVE_OFF)
    key = store.add(1, MAC + 5)
    evs, cursor = chat.poll([], {}, {"seen": []}, {}, NOW)
    assert [(e.id, e.ts, e.source, e.kind, e.match, e.cursor_key, e.summary, e.payload) for e in evs] == [
        (f"chat:{key}", NOW, "chat", "dm", {"app": "com.example.chat", "conversation": "Ann Lee", "sender": "Ann Lee",
                                            "channel": "", "thread": ""},
         "1792700005.000000", f"Ann Lee · ref {REF}", {"title": "Acme", "ref": REF, "delivered": NOW + 5,
                                                         "scout": "off"})]
    assert cursor == {"seen": [key], "pending": {}, "budget_spent": [], "failed": []}


def test_a_channel_notification_becomes_a_channel_event_with_the_sender_from_the_body(chat_cfg, store):
    chat_cfg.write_text(ARCHIVE_OFF)
    store.add(1, MAC + 1, subt="#team-x", body="Bob Ray: @me ship it")
    store.add(1, MAC + 2, subt="#team-x", body="no prefix", iden="")
    evs, _ = chat.poll([], {}, {"seen": []}, {}, NOW)
    assert sorted((e.kind, e.match["sender"], e.match["conversation"], e.match["app"], e.summary) for e in evs) == [
        ("channel", "", "#team-x", "com.example.chat", "#team-x"),
        ("channel", "Bob Ray", "#team-x", "com.example.chat", f"Bob Ray in #team-x · ref {REF}")]


def test_a_long_summary_is_cut_at_200_characters(chat_cfg, store):
    chat_cfg.write_text(ARCHIVE_OFF)
    store.add(1, MAC + 1, subt="A" * 300)
    evs, _ = chat.poll([], {}, {"seen": []}, {}, NOW)
    assert len(evs[0].summary) == 200 and evs[0].summary.endswith(f" · ref {REF}")
    assert evs[0].summary.startswith("AAAA") and evs[0].match["sender"] == "A" * 300

def test_a_pass_returns_its_events_in_id_order_so_a_wake_mark_on_an_early_one_refuses_no_later_one(chat_cfg, store):
    chat_cfg.write_text(ARCHIVE_OFF)
    store.add(1, MAC + 1, uid=b"\xff" * 16)
    store.add(1, MAC + 2, uid=b"\x00" * 16)
    evs, _ = chat.poll([], {}, {"seen": []}, {}, NOW)
    marked = events.Sub("s1", "K", "design", "chat", {}, None, events.chat_time(NOW - 60), None, None, None, NOW - 60,
                        [evs[0].ts, evs[0].id])
    assert len(evs) == 2 and events.matches(marked, evs[1])


def test_a_cursor_without_pending_reads_as_empty(chat_cfg, store):
    evs, cursor = chat.poll(LIVE, {}, {"seen": [], "pending": "junk"}, {}, NOW)
    assert evs == [] and cursor["pending"] == {}


def _fallback(store, subt, body, chat_cfg):
    chat_cfg.write_text(ARCHIVE_OFF)
    store.add(1, MAC + 5, subt=subt, body=body)
    evs, _ = chat.poll([], {}, {"seen": []}, {}, NOW)
    assert len(evs) == 1 and evs[0].payload["scout"] == "off"
    return evs[0]


def test_a_private_channel_notification_without_a_hash_is_a_channel_event_with_the_sender_from_the_body(chat_cfg,
                                                                                                        store):
    e = _fallback(store, "team-x", "Bob Ray: hi", chat_cfg)
    assert (e.kind, e.match["sender"], e.match["conversation"], e.summary) == (
        "channel", "Bob Ray", "#team-x", f"Bob Ray in #team-x · ref {REF}")


def test_a_direct_message_whose_text_starts_with_a_colon_phrase_stays_a_dm(chat_cfg, store):
    e = _fallback(store, "ann", "Note: hi", chat_cfg)
    assert (e.kind, e.match["sender"], e.match["conversation"]) == ("dm", "ann", "ann")


def test_an_app_notification_with_a_colon_in_its_text_stays_a_dm(chat_cfg, store):
    e = _fallback(store, "Builder", "@bob requested your review on KEY-1: title", chat_cfg)
    assert (e.kind, e.match["sender"]) == ("dm", "Builder")


def test_a_lowercase_one_word_subtitle_without_a_name_like_prefix_stays_a_dm(chat_cfg, store):
    e = _fallback(store, "dbot", "deploy done", chat_cfg)
    assert (e.kind, e.match["sender"], e.match["conversation"]) == ("dm", "dbot", "dbot")


def test_a_hash_row_with_a_bare_lead_takes_no_sender(chat_cfg, store):
    e = _fallback(store, "#team-x", "note: x", chat_cfg)
    assert (e.match["sender"], e.summary) == ("", f"#team-x · ref {REF}")


def test_a_greek_capitalised_lead_is_a_name(chat_cfg, store):
    e = _fallback(store, "team-x", "\u0391\u03bd\u03bd\u03b1 \u039b\u03b9: \u03bc\u03ae\u03bd\u03c5\u03bc\u03b1", chat_cfg)
    assert (e.kind, e.match["sender"], e.match["conversation"]) == ("channel", "\u0391\u03bd\u03bd\u03b1 \u039b\u03b9",
                                                                   "#team-x")


def test_only_listed_apps_are_read_and_a_seen_row_is_not_read_again(chat_cfg, store):
    chat_cfg.write_text(ARCHIVE_OFF)
    store.add(2, MAC + 1)
    key = store.add(1, MAC + 2)
    evs, cursor = chat.poll([], {}, {"seen": []}, {}, NOW)
    assert [e.id for e in evs] == [f"chat:{key}"]
    again, _ = chat.poll([], {}, cursor, {}, NOW + 60)
    assert again == []


def test_a_row_inserted_late_with_an_older_delivery_time_is_read(chat_cfg, store):
    chat_cfg.write_text(ARCHIVE_OFF)
    store.add(1, MAC + 10)
    _, cursor = chat.poll([], {}, {"seen": []}, {}, NOW)
    late = store.add(1, MAC + 3)
    evs, _ = chat.poll([], {}, cursor, {}, NOW + 60)
    assert [(e.id, e.ts) for e in evs] == [(f"chat:{late}", NOW + 60)]


def test_a_reused_rec_id_is_read(chat_cfg, store):
    chat_cfg.write_text(ARCHIVE_OFF)
    store.add(1, MAC + 1, rec_id=10)
    _, cursor = chat.poll([], {}, {"seen": []}, {}, NOW)
    _sql(store.path, "DELETE FROM record WHERE rec_id = 10")
    key = store.add(1, MAC + 2, rec_id=10)
    evs, cursor = chat.poll([], {}, cursor, {}, NOW + 60)
    assert [e.id for e in evs] == [f"chat:{key}"]
    assert cursor["seen"] == [key]


def test_a_rewritten_row_is_read_again(chat_cfg, store):
    chat_cfg.write_text(ARCHIVE_OFF)
    store.add(1, MAC + 1, rec_id=20)
    _, cursor = chat.poll([], {}, {"seen": []}, {}, NOW)
    _sql(store.path, "UPDATE record SET delivered_date = ?, data = ? WHERE rec_id = 20",
         (MAC + 9, _plist(body="edited")))
    evs, _ = chat.poll([], {}, cursor, {}, NOW + 60)
    assert len(evs) == 1 and evs[0].id.endswith(f":{MAC + 9:.6f}")


def test_a_row_that_does_not_decode_or_has_no_subtitle_is_failed_once(chat_cfg, store):
    chat_cfg.write_text(ARCHIVE_OFF)
    bad1 = store.add(1, MAC + 1, data=b"junk")
    bad2 = store.add(1, MAC + 2, subt=None)
    good = store.add(1, MAC + 3)
    evs, cursor = chat.poll([], {}, {"seen": []}, {}, NOW)
    assert [e.id for e in evs] == [f"chat:{good}"]
    assert [s for s, _ in cursor["failed"]] == [f"notification {bad1}", f"notification {bad2}"]
    again, cursor = chat.poll([], {}, cursor, {}, NOW + 60)
    assert again == [] and cursor["failed"] == []


def test_an_empty_apps_list_raises_poll_error(chat_cfg, store):
    chat_cfg.write_text('[events]\nenabled = true\n[events.sources.chat]\napps = []\n')
    store.add(1, MAC + 1)
    with pytest.raises(events.PollError, match="apps is empty"):
        chat.poll([], {}, {}, {}, NOW)


def test_a_missing_store_raises_poll_error_naming_the_path_without_the_full_disk_access_hint(chat_cfg, tmp_path,
                                                                                            monkeypatch):
    empty = tmp_path / "empty-home"
    empty.mkdir()
    monkeypatch.setenv("HOME", str(empty))
    with pytest.raises(events.PollError, match="no macOS notification store at") as err:
        chat.poll([], {}, {}, {}, NOW)
    assert str(empty / chat.STORE) in str(err.value) and "Full Disk Access" not in str(err.value)


def _unreadable(store):
    store.path.chmod(0)
    with pytest.raises(events.PollError, match="Full Disk Access") as err:
        chat.poll([], {}, {}, {}, NOW)
    store.path.chmod(0o600)
    return str(err.value)


ROOT_READS = pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0o000 file, so the store stays readable")


@ROOT_READS
def test_an_unreadable_store_names_the_interpreter_first_when_the_daemon_polls(chat_cfg, store):
    chat_cfg.write_text(chat_cfg.read_text() + "[lanes]\nin_manager = false\n")
    text = _unreadable(store)
    exe = os.path.realpath(sys.executable)
    assert exe in text and text.index(exe) < text.index("unable to open")


@ROOT_READS
def test_an_unreadable_store_names_the_interpreter_when_the_remembered_lanes_mode_says_the_daemon_polls(chat_cfg,
                                                                                                    store):
    paths.DAEMON.mkdir(parents=True, exist_ok=True)
    (paths.DAEMON / lane_io.MODE_NAME).write_text(json.dumps({"in_manager": False}))
    assert os.path.realpath(sys.executable) in _unreadable(store)


@ROOT_READS
def test_an_unreadable_store_names_the_terminal_app_when_the_manager_polls(chat_cfg, store):
    text = _unreadable(store)
    assert "Full Disk Access to the terminal app" in text and os.path.realpath(sys.executable) not in text


def test_a_row_whose_uuid_or_delivery_time_has_another_type_is_keyed_by_its_rec_id_and_still_read(chat_cfg, store):
    chat_cfg.write_text(ARCHIVE_OFF)
    _sql(store.path, "INSERT INTO record (rec_id, app_id, uuid, data, delivered_date) VALUES (1, 1, NULL, ?, ?)",
         (_plist(), MAC + 1))
    _sql(store.path, "INSERT INTO record (rec_id, app_id, uuid, data, delivered_date) VALUES (2, 1, 'text', ?, ?)",
         (_plist(), MAC + 2))
    _sql(store.path, "INSERT INTO record (rec_id, app_id, uuid, data, delivered_date) VALUES (3, 1, ?, ?, 'soon')",
         (os.urandom(16), _plist()))
    good = store.add(1, MAC + 4)
    evs, cursor = chat.poll([], {}, {"seen": []}, {}, NOW)
    assert sorted(e.id for e in evs) == sorted(["chat:rec:1", "chat:rec:2", f"chat:{good}"])
    assert {e.id: e.cursor_key for e in evs}["chat:rec:2"] == "1792700002.000000"
    assert [s for s, _ in cursor["failed"]] == ["notification rec:3"]
    assert cursor["seen"] == sorted(["rec:1", "rec:2", "rec:3", good])
    assert chat.poll([], {}, cursor, {}, NOW + 60) == ([], {**cursor, "failed": []})


def test_a_row_with_a_broken_xml_plist_is_failed_once(chat_cfg, store):
    chat_cfg.write_text(ARCHIVE_OFF)
    bad = store.add(1, MAC + 1, data=b"<?xml junk")
    evs, cursor = chat.poll([], {}, {"seen": []}, {}, NOW)
    assert evs == [] and [s for s, _ in cursor["failed"]] == [f"notification {bad}"]
    assert chat.poll([], {}, cursor, {}, NOW + 60) == ([], {**cursor, "failed": []})


def test_a_store_without_the_record_table_raises_poll_error_naming_the_schema(chat_cfg, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _make_store(home, record=False)
    with pytest.raises(events.PollError, match="schema changed"):
        chat.poll([], {}, {}, {}, NOW)


def test_a_store_that_is_not_a_database_raises_poll_error_without_the_full_disk_access_hint(chat_cfg, store):
    store.path.write_bytes(b"junk" * 100)
    with pytest.raises(events.PollError, match="cannot read the macOS notification store") as err:
        chat.poll([], {}, {}, {}, NOW)
    assert "Full Disk Access" not in str(err.value)


def _quiet(*args, **kwargs):
    return None


def _by_id(evs):
    return {e.id: e for e in evs}


def test_the_poller_stores_a_chat_event_and_only_the_matching_subscription_matches_it(
        fresh_orchestrator_dir, chat_cfg, store, tmp_path, monkeypatch):
    chat_cfg.write_text(ARCHIVE_OFF)
    _started_at_design(monkeypatch, tmp_path)
    a = events.subscribe("K", "design", "chat", {"sender": "Ann Lee"}, kinds=["dm"])
    b = events.subscribe("K", "design", "chat", {"conversation": "#team-x"})
    now = time.time()
    event_poll.step(now, [], _quiet)
    dm = f"chat:{store.add(1, now + 5 - chat.MAC_EPOCH)}"
    channel = f"chat:{store.add(1, now + 1 - chat.MAC_EPOCH, subt='#team-x', body='Bob Ray: hi')}"
    event_poll.step(now + 31, [], _quiet)
    stored = _by_id(events.recent("chat", now + 31))
    assert sorted(stored) == sorted([dm, channel])
    assert [i for i, e in stored.items() if events.matches(a, e)] == [dm]
    assert [i for i, e in stored.items() if events.matches(b, e)] == [channel]
    events.mark_delivered([a.sub_id], stored[dm])
    after_since = f"chat:{store.add(1, now + 2 - chat.MAC_EPOCH)}"
    before_since = f"chat:{store.add(1, a.since - 1 - chat.MAC_EPOCH)}"
    event_poll.step(now + 62, [], _quiet)
    a = next(s for s in events.subs_for("K", "chat", "design") if s.sub_id == a.sub_id)
    late = _by_id(events.recent("chat", now + 62))
    assert late[after_since].ts == now + 62
    assert events.matches(a, late[after_since])
    assert not events.matches(a, late[before_since])


def test_a_subscription_made_after_a_gap_wakes_only_on_rows_delivered_after_it(
        fresh_orchestrator_dir, chat_cfg, store, tmp_path, monkeypatch):
    chat_cfg.write_text(ARCHIVE_OFF)
    _started_at_design(monkeypatch, tmp_path)
    a = events.subscribe("K", "design", "chat", {"sender": "Ann Lee"})
    t = time.time()
    event_poll.step(t, [], _quiet)
    events.unsubscribe(a.sub_id)
    gap = f"chat:{store.add(1, time.time() - 5 - chat.MAC_EPOCH)}"
    c = events.subscribe("K", "design", "chat", {"sender": "Ann Lee"})
    fresh = f"chat:{store.add(1, c.since + 1 - chat.MAC_EPOCH)}"
    event_poll.step(t + 31, [], _quiet)
    stored = _by_id(events.recent("chat", t + 31))
    assert sorted(stored) == sorted([gap, fresh])
    assert [i for i, e in stored.items() if events.matches(c, e)] == [fresh]


def _trigger_files():
    return sorted(p for p in paths.CHAT_TRIGGERS.iterdir() if p.name != ".wake") if paths.CHAT_TRIGGERS.is_dir() else []


def _mark(at):
    paths.CHAT.mkdir(parents=True, exist_ok=True)
    paths.CHAT_STARTED.touch()
    os.utime(paths.CHAT_STARTED, (at, at))


def test_a_new_row_after_the_started_mark_writes_a_trigger_file_whatever_the_subscription_window(chat_cfg, store, home):
    _mark(NOW - 1)
    key = store.add(1, MAC + 0.5, subt="Ann Lee", body="secret-body-text", iden=REF)
    evs, cursor = chat.poll([], {}, {}, {}, NOW)
    assert evs == [] and key in cursor["pending"]
    files = _trigger_files()
    assert len(files) == 1 and ":" not in files[0].name and files[0].name == f"{key.replace(':', '_')}.json"
    body = json.loads(files[0].read_text())
    assert body == {"key": key, "iden": REF, "ts": "1792700000.000100", "subt": "Ann Lee", "kind": "dm",
                    "delivered": NOW + 0.5, "tries": 0}
    assert "secret-body-text" not in files[0].read_text()


def test_no_trigger_is_written_for_a_row_delivered_before_the_started_mark_or_when_the_archive_is_off(chat_cfg, store, home):
    _mark(NOW + 1)
    store.add(1, MAC + 0.5, subt="Ann Lee")
    chat.poll([], {}, {}, {}, NOW)
    assert _trigger_files() == []
    _mark(NOW - 1)
    chat_cfg.write_text(ARCHIVE_OFF)
    store.add(1, MAC + 0.7, subt="Ann Lee")
    evs, _ = chat.poll([], {}, {}, {}, NOW)
    assert _trigger_files() == [] and [e.payload["scout"] for e in evs] == ["off", "off"]


def test_no_trigger_is_written_while_the_started_mark_is_missing(chat_cfg, store, home):
    store.add(1, MAC + 0.5, subt="Ann Lee")
    chat.poll([], {}, {}, {}, NOW)
    assert not paths.CHAT_TRIGGERS.is_dir() or _trigger_files() == []


def test_a_row_the_cursor_has_seen_writes_no_second_trigger(chat_cfg, store, home):
    _mark(NOW - 1)
    store.add(1, MAC + 5, subt="Ann Lee")
    _, cursor = chat.poll([], {}, {}, {}, NOW)
    for f in _trigger_files():
        f.unlink()
    chat.poll([], {}, cursor, {}, NOW + 60)
    assert _trigger_files() == []
    store.add(1, MAC + 6, subt="#team-x", body="Bob Ray: hi")
    _, cursor = chat.poll(LIVE, {}, cursor, {}, NOW + 120)
    assert len(_trigger_files()) == 1
    for f in _trigger_files():
        f.unlink()
    chat.poll(LIVE, {}, cursor, {}, NOW + 180)
    assert _trigger_files() == []


def test_wanted_polls_the_chat_source_while_the_archive_is_enabled_without_a_subscription(chat_cfg):
    assert event_poll._wanted("chat", [], {}) is True
    chat_cfg.write_text(ARCHIVE_OFF)
    assert event_poll._wanted("chat", [], {}) is False
    assert event_poll._wanted("tracker", [], {}) is False


def test_no_trigger_is_written_for_a_row_whose_message_ts_is_not_a_timestamp(chat_cfg, store, home):
    chat_cfg.write_text(ARCHIVE_OFF)
    _mark(NOW - 1)
    store.add(1, MAC + 0.5, subt="#team-x", body="no prefix", iden="")
    store.add(1, MAC + 0.6, subt="#team-x", body="Bob Ray: x", iden="E_abc")
    evs, _ = chat.poll([], {}, {}, {}, NOW)
    assert len(evs) == 2 and _trigger_files() == []


def test_no_wake_toggle_without_the_started_mark(chat_cfg, store, home):
    store.add(1, MAC + 5, subt="#team-x", body="Bob Ray: hi")
    chat.poll(LIVE, {}, {"seen": []}, {}, NOW)
    assert not paths.CHAT.exists()


TS = "1792700000.000100"
LINK = "https://chat.example/p/9"
SECRET = "secret-archive-text"
PENDING_ROW = {"app": "com.example.chat", "subt": "#team-x", "kind": "channel", "sender": "Bob Ray",
               "conversation": "#team-x", "titl": "Acme", "iden": REF, "delivered": NOW + 5}


def _msg(ts=TS, cid="C1", kind="channel", name="team-x", members="", thread=None, sender_id="U1", sender="Bob Ray",
         captured_at=NOW):
    return cid, kind, name, members, ts, thread, sender_id, sender, captured_at


def _archive(*rows):
    con = chat_archive.open_db()
    for cid, kind, name, members, ts, thread, sender_id, sender, captured_at in rows:
        con.execute("insert or ignore into conversations (id, kind, name, members, last_ts, swept_at) "
                    "values (?, ?, ?, ?, null, null)", (cid, kind, name, members))
        con.execute("insert into messages (conversation_id, ts, thread_ts, sender_id, sender, text, link, source, "
                    "captured_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (cid, ts, thread, sender_id, sender, SECRET, LINK, "sweep", captured_at))
    con.commit()
    con.close()


def _resolved(iden, ts, cid="C1", resolved_at=NOW):
    con = chat_archive.open_db()
    con.execute("insert into resolved values (?,?,?,?,?,?,?,?,?,?)",
                (iden, ts, cid, "channel", "team-x", None, "U1", "Bob Ray", LINK, resolved_at))
    con.commit()
    con.close()


@pytest.mark.parametrize("row, kind, conversation, sender, summary", [
    (_msg(cid="D1", kind="dm", name="Ann Lee", sender="Ann Lee"), "dm", "Ann Lee", "Ann Lee",
     f"Ann Lee · channel D1 · ref archive_{TS}"),
    (_msg(cid="D1", kind="dm", name="Ann Lee", sender="Ann Lee", thread=THREAD), "dm-thread", "Ann Lee", "Ann Lee",
     f"Ann Lee · thread {THREAD} · channel D1 · ref archive_{TS}"),
    (_msg(), "channel", "#team-x", "Bob Ray", f"Bob Ray in #team-x · channel C1 · ref archive_{TS}"),
    (_msg(thread=THREAD), "channel-thread", "#team-x", "Bob Ray",
     f"Bob Ray in #team-x · thread {THREAD} · channel C1 · ref archive_{TS}"),
    (_msg(cid="G1", kind="group", name="", members="Ann Lee, Bob Ray, Me"), "dm", "Ann Lee, Bob Ray, Me", "Bob Ray",
     f"Bob Ray · channel G1 · ref archive_{TS}")])
def test_a_message_without_a_notification_becomes_an_event_on_the_first_poll_after_its_capture(
        chat_cfg, store, row, kind, conversation, sender, summary):
    _archive(row)
    evs, cursor = chat.poll([], {}, {"seen": []}, {}, NOW + 30)
    assert evs == [events.Event(
        f"chat:msg:{row[0]}:{TS}", NOW + 30, "chat", kind,
        {"app": "com.example.chat", "conversation": conversation, "sender": sender, "channel": row[0],
         "thread": row[5] or ""},
        events.chat_time(float(TS)), summary,
        {"title": "", "ref": f"archive_{TS}", "delivered": float(TS), "sender_id": "U1", "link": LINK, "scout": "ok"})]
    assert cursor == {"seen": [], "pending": {}, "archive": {"messages": NOW, "resolved": 0.0}, "budget_spent": [],
                      "failed": []}


def test_a_dm_event_names_the_conversation_as_sender_and_conversation(chat_cfg, store):
    _archive(_msg(cid="D1", kind="dm", name="Ann Lee", sender="Ann L."),
             _msg(ts="1792700000.000200", cid="D2", kind="dm", name="", sender="Cy Roe"))
    evs, _ = chat.poll([], {}, {}, {}, NOW)
    assert {e.match["channel"]: (e.match["sender"], e.match["conversation"]) for e in evs} == {
        "D1": ("Ann Lee", "Ann Lee"), "D2": ("Cy Roe", "Cy Roe")}


def test_a_thread_parent_equal_to_the_message_ts_is_not_a_thread_kind(chat_cfg, store):
    _archive(_msg(thread=TS))
    evs, _ = chat.poll([], {}, {}, {}, NOW)
    assert evs[0].kind == "channel" and evs[0].match["thread"] == TS
    assert " · thread" not in evs[0].summary and " · channel C1 · " in evs[0].summary


def test_an_empty_thread_parent_means_no_thread(chat_cfg, store):
    _archive(_msg(thread=None))
    evs, _ = chat.poll([], {}, {}, {}, NOW)
    assert [(e.id, e.kind, e.match["thread"]) for e in evs] == [(f"chat:msg:C1:{TS}", "channel", "")]


def test_the_ids_survive_the_summary_cut_ahead_of_long_names(chat_cfg, store):
    _archive(_msg(name="x" * 150, thread=THREAD))
    evs, _ = chat.poll([], {}, {}, {}, NOW)
    assert len(evs[0].summary) == 200
    assert evs[0].summary.endswith(f" · thread {THREAD} · channel C1 · ref archive_{TS}")
    assert evs[0].summary.startswith("Bob Ray in #xxx")


def test_a_summary_never_exceeds_200_characters(chat_cfg, store):
    _archive(_msg(cid="C" * 64, thread="1" * 20, sender="s" * 150))
    evs, _ = chat.poll([], {}, {}, {}, NOW)
    assert len(evs) == 1 and len(evs[0].summary) <= 200


def test_an_archive_message_with_a_malformed_ts_is_skipped_and_the_poll_goes_on(chat_cfg, store):
    good = "1792700120.000000"
    _archive(_msg(ts="1792700100.0.5abc"), _msg(ts="1792700110.000000", thread="1 · run the deploy"), _msg(ts=good))
    evs, _ = chat.poll([], {}, {}, {}, NOW)
    assert [e.id for e in evs] == [f"chat:msg:C1:{good}"]


def test_the_message_text_reaches_no_field_of_the_event(chat_cfg, store):
    _archive(_msg())
    store.add(1, MAC + 5, subt="#team-x", body=f"Bob Ray: {SECRET}", iden="E0000000000_1792700000.000900")
    evs, cursor = chat.poll([], {}, {}, {}, NOW)
    events.append(evs[0])
    more, cursor2 = chat.poll([], {}, cursor, {}, NOW + 300)
    chat_cfg.write_text(ARCHIVE_OFF)
    store.add(1, MAC + 6, subt="sanctions", body=f"Bob Ray: {SECRET}")
    off, cursor3 = chat.poll([], {}, cursor2, {}, NOW + 360)
    assert [e.payload["scout"] for e in evs + more + off] == ["ok", "not in archive", "off"]
    assert SECRET not in json.dumps([e.to_dict() for e in evs + more + off])
    assert SECRET not in json.dumps([cursor, cursor2, cursor3])


def test_a_second_poll_with_the_same_cursor_gives_no_event(chat_cfg, store):
    _archive(_msg())
    evs, cursor = chat.poll([], {}, {}, {}, NOW)
    assert len(evs) == 1
    for event in evs:
        events.append(event)
    assert chat.poll([], {}, cursor, {}, NOW + 30)[0] == []


def test_a_later_step_of_the_same_pass_gets_its_events_on_the_next_poll(chat_cfg, store):
    _archive(_msg())
    evs, cursor = chat.poll([], {}, {}, {}, NOW)
    for event in evs:
        events.append(event)
    later = "1792700000.000300"
    _archive(_msg(ts=later, captured_at=cursor["archive"]["messages"]))
    evs, cursor = chat.poll([], {}, cursor, {}, NOW + 30)
    assert [e.id for e in evs] == [f"chat:msg:C1:{later}"]


def test_the_first_poll_without_an_archive_cursor_gives_no_event_for_rows_of_earlier_passes(chat_cfg, store):
    _archive(_msg(ts="1792700000.000001", captured_at=NOW - 100), _msg(ts="1792700000.000002", captured_at=NOW - 100),
             _msg(ts="1792700000.000003", captured_at=NOW), _msg(ts="1792700000.000004", cid="D1", kind="dm",
                                                                 name="Ann Lee", captured_at=NOW))
    _resolved("E_0", "1792700000.000002", resolved_at=NOW - 90)
    _resolved("E_1", "1792700000.000003", resolved_at=NOW + 2)
    evs, cursor = chat.poll([], {}, {"seen": []}, {}, NOW + 30)
    assert [e.id for e in evs] == ["chat:msg:C1:1792700000.000003", "chat:msg:D1:1792700000.000004"]
    assert cursor["archive"] == {"messages": NOW, "resolved": NOW + 2}


@pytest.mark.parametrize("archive", ["junk", {"messages": "x", "resolved": 1.0}, {"messages": 1.0}])
def test_a_malformed_archive_cursor_reads_from_the_last_pass(chat_cfg, store, archive):
    _archive(_msg(ts="1792700000.000001", captured_at=NOW - 100), _msg(ts="1792700000.000003", captured_at=NOW))
    evs, cursor = chat.poll([], {}, {"archive": archive}, {}, NOW + 30)
    assert [e.id for e in evs] == ["chat:msg:C1:1792700000.000003"]
    assert cursor["archive"] == {"messages": NOW, "resolved": 0.0}


@pytest.mark.parametrize("old_id, ref", [("chat:k:scout", f"E1_{TS}"), (f"chat:catchup:C1:{TS}", f"catchup_{TS}")])
def test_a_message_with_an_event_of_the_old_id_form_gets_no_event(chat_cfg, store, old_id, ref):
    events.append(events.Event(old_id, NOW - 60, "chat", "channel", {"conversation": "#team-x", "channel": "C1"},
                               events.chat_time(float(TS)), "Bob Ray in #team-x", {"ref": ref}))
    _archive(_msg(), _msg(ts="1792700000.000200"))
    evs, _ = chat.poll([], {}, {}, {}, NOW)
    assert [e.id for e in evs] == ["chat:msg:C1:1792700000.000200"]


def test_a_notification_whose_message_is_in_the_archive_gives_no_banner_event(chat_cfg, store):
    key = store.add(1, MAC + 5, subt="#team-x", body="Bob Ray: hi")
    evs, cursor = chat.poll([], {}, {}, {}, NOW)
    assert evs == [] and cursor["pending"] == {key: {"first": NOW, "row": PENDING_ROW}} and cursor["seen"] == []
    _archive(_msg(captured_at=NOW + 20))
    evs, cursor = chat.poll([], {}, cursor, {}, NOW + 30)
    assert [e.id for e in evs] == [f"chat:msg:C1:{TS}"]
    assert cursor["pending"] == {} and cursor["seen"] == [key]
    events.append(evs[0])
    assert chat.poll([], {}, cursor, {}, NOW + 330)[0] == []


def test_a_notification_of_a_message_with_a_logged_archive_event_gives_no_banner_event(chat_cfg, store):
    _archive(_msg())
    evs, cursor = chat.poll([], {}, {}, {}, NOW)
    events.append(evs[0])
    key = store.add(1, MAC + 40, subt="#team-x", body="Bob Ray: hi")
    evs, cursor = chat.poll([], {}, cursor, {}, NOW + 60)
    assert evs == [] and cursor["pending"] == {} and cursor["seen"] == [key]


def test_a_notification_the_archive_never_holds_gives_a_banner_event_after_300s(chat_cfg, store):
    assert chat.FALLBACK_SEC == 300
    key = store.add(1, MAC + 5, subt="#team-x", body="Bob Ray: hi")
    _archive(_msg(ts="1792700000.000900"))
    evs, cursor = chat.poll([], {}, {}, {}, NOW)
    events.append(evs[0])
    evs, cursor = chat.poll([], {}, cursor, {}, NOW + 299)
    assert evs == [] and list(cursor["pending"]) == [key]
    evs, cursor = chat.poll([], {}, cursor, {}, NOW + 300)
    assert [(e.id, e.kind, e.match, e.summary, e.payload) for e in evs] == [
        (f"chat:{key}", "channel", {"app": "com.example.chat", "conversation": "#team-x", "sender": "Bob Ray",
                                    "channel": "", "thread": ""},
         f"Bob Ray in #team-x · ref {REF}",
         {"title": "Acme", "ref": REF, "delivered": NOW + 5, "scout": "not in archive"})]
    assert cursor["seen"] == [key] and cursor["pending"] == {}


def test_an_archive_event_after_the_banner_goes_out_as_a_second_event(chat_cfg, store):
    key = store.add(1, MAC + 5, subt="#team-x", body="Bob Ray: hi")
    _, cursor = chat.poll([], {}, {}, {}, NOW)
    banner, cursor = chat.poll([], {}, cursor, {}, NOW + 300)
    events.append(banner[0])
    _archive(_msg(captured_at=NOW + 320))
    evs, cursor = chat.poll([], {}, cursor, {}, NOW + 330)
    assert [e.id for e in banner] == [f"chat:{key}"] and [e.id for e in evs] == [f"chat:msg:C1:{TS}"]


def test_a_row_the_store_dropped_while_pending_still_emits_its_fallback_from_the_cursor(chat_cfg, store):
    a = store.add(1, MAC + 5, subt="sanctions", body="Bob Ray: hi")
    _, cursor = chat.poll([], {}, {"seen": []}, {}, NOW)
    _sql(store.path, "DELETE FROM record")
    evs, cursor = chat.poll([], {}, cursor, {}, NOW + 300)
    assert [(e.id, e.kind, e.match["sender"], e.match["conversation"]) for e in evs] == [
        (f"chat:{a}", "channel", "Bob Ray", "#sanctions")]
    assert cursor["pending"] == {} and cursor["seen"] == []


def _raising(apps):
    raise events.PollError("cannot read the macOS notification store: locked")


def test_an_unreadable_notification_store_raises_and_the_next_poll_gives_the_rows_captured_meanwhile(chat_cfg, store):
    _archive(_msg())
    evs, cursor = chat.poll([], {}, {}, {}, NOW)
    events.append(evs[0])
    later = "1792700000.000300"
    _archive(_msg(ts=later, captured_at=NOW + 10))
    with pytest.raises(events.PollError, match="locked"):
        chat.poll([], {}, cursor, {}, NOW + 30, call=_raising)
    evs, _ = chat.poll([], {}, cursor, {}, NOW + 60)
    assert [e.id for e in evs] == [f"chat:msg:C1:{later}"]


@pytest.mark.parametrize("junk", [False, True])
def test_an_unreadable_archive_gives_no_archive_events_and_keeps_the_cursor(chat_cfg, store, junk):
    if junk:
        paths.CHAT_DB.parent.mkdir(parents=True)
        paths.CHAT_DB.write_bytes(b"junk" * 300)
    key = store.add(1, MAC + 5, subt="#team-x", body="Bob Ray: hi")
    marks = {"messages": 5.0, "resolved": 6.0}
    evs, cursor = chat.poll([], {}, {"archive": marks}, {}, NOW)
    assert evs == [] and cursor["archive"] == marks and list(cursor["pending"]) == [key]
    evs, cursor = chat.poll([], {}, cursor, {}, NOW + 300)
    assert [(e.id, e.payload["scout"]) for e in evs] == [(f"chat:{key}", "not in archive")]
    assert cursor["archive"] == marks
    assert "archive" not in chat.poll([], {}, {}, {}, NOW + 330)[1]


def test_with_the_archive_off_every_row_falls_back_at_once(chat_cfg, store):
    chat_cfg.write_text(ARCHIVE_OFF)
    _archive(_msg())
    store.add(1, MAC + 5, subt="#team-x", body="Bob Ray: hi")
    evs, cursor = chat.poll([], {}, {"seen": []}, {}, NOW)
    assert len(evs) == 1
    assert evs[0].payload["scout"] == "off" and evs[0].match["channel"] == "" and evs[0].kind == "channel"
    assert cursor["pending"] == {} and "archive" not in cursor and cursor["failed"] == []


def test_a_row_pending_when_the_archive_goes_off_falls_back_on_the_next_poll(chat_cfg, store):
    key = store.add(1, MAC + 5, subt="#team-x", body="Bob Ray: hi")
    _, cursor = chat.poll([], {}, {}, {}, NOW)
    chat_cfg.write_text(ARCHIVE_OFF)
    evs, cursor = chat.poll([], {}, cursor, {}, NOW + 30)
    assert [(e.id, e.payload["scout"]) for e in evs] == [(f"chat:{key}", "off")] and cursor["pending"] == {}


def test_an_enabled_archive_with_an_empty_self_user_id_reads_no_archive_and_falls_back_at_once(chat_cfg, store, home):
    chat_cfg.write_text(ARCHIVE_ON.replace('self_user_id = "U01ME00000"\n', ""))
    assert config.chat_archive()["enabled"] is True
    _mark(NOW - 1)
    _archive(_msg())
    key = store.add(1, MAC + 5, subt="#team-x", body="Bob Ray: hi")
    evs, cursor = chat.poll([], {}, {"seen": []}, {}, NOW)
    assert [(e.id, e.payload["scout"]) for e in evs] == [(f"chat:{key}", "off")]
    assert "archive" not in cursor and len(_trigger_files()) == 1


def test_his_own_message_in_the_archive_gives_no_event(chat_cfg, store):
    _archive(_msg(sender_id="U01ME00000", sender="Me"))
    assert chat.poll([], {}, {}, {}, NOW)[0] == []


def test_an_archive_message_older_than_the_retention_gives_no_event(chat_cfg, store):
    old = f"{NOW - events.retention_sec() - 60:.6f}"
    _archive(_msg(ts=old), _msg())
    assert [e.id for e in chat.poll([], {}, {}, {}, NOW)[0]] == [f"chat:msg:C1:{TS}"]


def _old_pending(first, fallback_sent, iden=REF):
    return {"first": first, "first_try": first, "tries": 1, "fallback_sent": fallback_sent, "reason": "not found",
            "row": {**PENDING_ROW, "iden": iden}}


def test_a_pending_entry_of_the_old_shape_is_read_and_one_with_its_fallback_sent_is_dropped(chat_cfg, store, home):
    _mark(NOW - 1)
    waiting = store.add(1, MAC + 5, subt="#team-x", body="Bob Ray: hi")
    sent = store.add(1, MAC + 6, subt="#team-x", body="Bob Ray: ho", iden="E0000000000_1792700000.000200")
    pending = {waiting: _old_pending(NOW - 10, False), sent: _old_pending(NOW - 400, True,
                                                                         "E0000000000_1792700000.000200")}
    evs, cursor = chat.poll([], {}, {"seen": [], "pending": pending}, {}, NOW)
    assert evs == [] and cursor["pending"] == {waiting: {"first": NOW - 10, "row": PENDING_ROW}}
    assert cursor["seen"] == [sent] and _trigger_files() == []
    evs, cursor = chat.poll([], {}, cursor, {}, NOW + 290)
    assert [e.id for e in evs] == [f"chat:{waiting}"]


@pytest.mark.parametrize("broken", [lambda e: e.pop("first"), lambda e: e["row"].update(delivered="x")])
def test_a_pending_entry_with_a_broken_field_is_dropped_and_the_row_read_again(chat_cfg, store, broken):
    key = store.add(1, MAC + 5, subt="#team-x", body="Bob Ray: hi")
    entry = {"first": NOW - 10, "row": dict(PENDING_ROW)}
    broken(entry)
    evs, cursor = chat.poll([], {}, {"seen": [], "pending": {key: entry}}, {}, NOW)
    assert evs == [] and cursor["pending"] == {key: {"first": NOW, "row": PENDING_ROW}}


def test_the_cursor_drops_the_scout_last_and_catchup_keys(chat_cfg, store):
    old = {"seen": [], "pending": {}, "scout_last": {"rows": 1}, "catchup": {"#team-x": NOW}, "notes": [{"type": "x"}],
           "kept": 1}
    _, cursor = chat.poll([], {}, old, {}, NOW)
    assert cursor == {"seen": [], "pending": {}, "kept": 1, "budget_spent": [], "failed": []}


def test_the_daemon_toggles_the_wake_file_while_a_pending_row_is_unresolved(chat_cfg, store, home):
    _mark(NOW - 1)
    store.add(1, MAC + 5, subt="#team-x", body="Bob Ray: hi")
    _, cursor = chat.poll([], {}, {"seen": []}, {}, NOW)
    wake = paths.CHAT_TRIGGERS / ".wake"
    assert wake.exists()
    _, cursor = chat.poll([], {}, cursor, {}, NOW + 60)
    assert not wake.exists()
    _archive(_msg(captured_at=NOW + 90))
    _, cursor = chat.poll([], {}, cursor, {}, NOW + 120)
    assert not wake.exists() and cursor["pending"] == {}
    _, cursor = chat.poll([], {}, cursor, {}, NOW + 180)
    assert not wake.exists()


def test_the_poller_stores_an_archive_event_and_a_thread_subscription_matches_it(
        fresh_orchestrator_dir, chat_cfg, store, tmp_path, monkeypatch):
    _started_at_design(monkeypatch, tmp_path)
    t = events.subscribe("K", "design", "chat", {"thread": THREAD})
    s = events.subscribe("K", "design", "chat", {"sender": "Bob Ray"})
    other = events.subscribe("K", "design", "chat", {"thread": "1792700000.000060"})
    now = time.time()
    event_poll.step(now, [], _quiet)
    ts = f"{now + 1:.6f}"
    _archive(_msg(ts=ts, thread=THREAD, captured_at=now + 5))
    records = event_poll.step(now + 31, [], _quiet)
    stored = events.recent("chat", now + 31)
    assert [(e.id, e.kind) for e in stored] == [(f"chat:msg:C1:{ts}", "channel-thread")]
    assert events.matches(t, stored[0]) and events.matches(s, stored[0]) and not events.matches(other, stored[0])
    polled = next(r for r in records if r["type"] == "events_polled" and r["source"] == "chat")
    assert polled == {"type": "events_polled", "source": "chat", "n": 1}
    assert event_poll.step(now + 62, [], _quiet) is not None and len(events.recent("chat", now + 62)) == 1
