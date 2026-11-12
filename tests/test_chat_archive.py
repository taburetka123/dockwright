import datetime
import email.message
import fcntl
import getpass
import importlib
import io
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

from dockwright import chat_archive, config, failure_streak, paths
from tests.lockdown_argv import permission_surface_widened
from tests.test_mcp_tools import fresh_orchestrator_dir  # noqa: F401

NOW = 1793106700.0
FULL_SECTION = (
    '[chat_archive]\nenabled = true\nhand = "direct"\nrunner = "launchd"\nmcp_url = "https://chat.example.test/mcp"\n'
    'mcp_server = "srv"\nkeychain_service = "kc-service"\nmodel = "pin-x"\nself_user_id = "U01ME00000"\n'
    'mention_keys = ["U01ME00000", "S01GRP0001", "S01GRP0002", "S01GRP0003"]\nretention_days = 90\n'
    'every_sec = 300\ngroup_sweep_sec = 3600\nregistry_sec = 86400\nsearch_overlap_sec = 900\n'
    'write_tools = ["write_a", "write_b"]\n'
    '[chat_archive.tools]\nsearch = "search_tool"\nread_conversation = "read_conversation_tool"\n'
    'read_thread = "read_thread_tool"\nlist_conversations = "list_conversations_tool"\n'
)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "dockwright.toml"
    path.write_text("")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(path))

    def write(text):
        path.write_text(text)
        return path

    return write


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.delenv("DOCKWRIGHT_CONFIG", raising=False)
    importlib.reload(config)
    importlib.reload(paths)
    yield root
    monkeypatch.undo()
    importlib.reload(config)
    importlib.reload(paths)


def test_chat_archive_config_defaults_and_disables_itself_without_the_required_keys(cfg):
    got = config.chat_archive()
    assert got["enabled"] is False and got["retention_days"] == 90 and got["every_sec"] == 300
    assert got["hand"] == "direct" and got["runner"] == "launchd" and got["mention_keys"] == []
    assert got["group_sweep_sec"] == 3600 and got["registry_sec"] == 86400 and got["search_overlap_sec"] == 900
    assert got["tools"] == {"search": "", "read_conversation": "", "read_thread": "", "list_conversations": ""}
    assert got["write_tools"] == [] and got["model"] == ""
    assert got["errors"] == ["mcp_url is empty", "mcp_server is empty", "keychain_service is empty",
                             "tools.search is empty", "tools.read_conversation is empty",
                             "tools.read_thread is empty", "tools.list_conversations is empty", "write_tools is empty"]


def test_a_complete_chat_archive_section_is_enabled_and_scout_model_reads_its_model(cfg):
    cfg(FULL_SECTION)
    assert config.chat_archive()["enabled"] is True and config.chat_archive()["errors"] == []
    assert config.chat_archive()["mention_keys"] == ["U01ME00000", "S01GRP0001", "S01GRP0002", "S01GRP0003"]
    assert config.chat_archive()["write_tools"] == ["write_a", "write_b"]
    assert config.scout_model() == "pin-x"


def test_an_enabled_section_missing_a_key_is_forced_off_with_the_reason(cfg):
    cfg(FULL_SECTION.replace('mcp_server = "srv"\n', ""))
    got = config.chat_archive()
    assert got["enabled"] is False and got["errors"] == ["mcp_server is empty"]


def test_events_source_chat_no_longer_returns_the_scout_keys(cfg):
    got = config.events_source("chat")
    assert "scout_tools" not in got and "scout_mcp" not in got and "scout_model" not in got
    assert "scout_timeout_sec" not in got and "scout_retry_sec" not in got


def test_chat_paths_hang_off_the_state_root_and_ensure_dirs_leaves_them_to_the_pass(home):
    paths.ensure_dirs()
    assert paths.CHAT == paths.ROOT / "chat" and paths.CHAT_STOP == paths.ROOT / "chat-archive-stop"
    assert paths.CHAT_DB == paths.CHAT / "archive.db" and paths.CHAT_LEDGER == paths.CHAT / "ledger.jsonl"
    assert paths.CHAT_STARTED == paths.CHAT / "started" and paths.CHAT_LOCK == paths.CHAT / "pass.lock"
    assert paths.CHAT_TRIGGERS == paths.CHAT / "triggers" and paths.CHAT_RAW == paths.CHAT / "raw"
    assert not paths.CHAT.exists()


FIXTURES = Path(__file__).parent / "fixtures" / "chat_archive"


def FIXTURE(name):
    return (FIXTURES / name).read_text()


@pytest.mark.parametrize("name,kind,count", [("conversation.txt", "conversation", 3), ("search-dm.txt", "search", 2),
                                             ("thread.txt", "thread", 3), ("search-context.txt", "search", 1)])
def test_parse_returns_every_message_of_a_fixture_and_drops_context_blocks(name, kind, count):
    got = chat_archive.parse(kind, FIXTURE(name), conversation_id="C01ABCDEF")
    assert len(got["messages"]) == count and got["unparsed"] == 0
    assert all("never stored" not in m["text"] for m in got["messages"])


def test_parse_reads_ids_thread_parent_bot_flag_and_reply_marks():
    dm = chat_archive.parse("search", FIXTURE("search-dm.txt"))["messages"]
    assert dm[0]["conversation_id"] == "D01ABCDEF" and dm[0]["conversation_kind"] == "dm" and dm[0]["bot"] is True
    assert dm[0]["sender_id"] == "U01BOT000" and dm[0]["sender"] == "Bot Example" and dm[1]["bot"] is False
    assert dm[0]["ts"] == "1793106658.209309" and dm[0]["thread_ts"] is None
    assert dm[0]["link"] == "https://chat.example.test/archives/D01ABCDEF/p1793106658209309"
    assert dm[1]["text"] == "Consectetur adipiscing elit, sed do eiusmod."
    win = chat_archive.parse("search", FIXTURE("search-window.txt"))["messages"]
    assert win[0]["thread_ts"] == "1793106047.590119" and win[0]["conversation_kind"] == "channel"
    assert win[0]["conversation_name"] == "some-channel" and win[1]["thread_ts"] is None
    ctx = chat_archive.parse("search", FIXTURE("search-context.txt"))["messages"]
    assert ctx[0]["reply_count"] == 2 and ctx[0]["thread_ts"] == ctx[0]["ts"]
    conv = chat_archive.parse("conversation", FIXTURE("conversation.txt"), conversation_id="D01ABCDEF")["messages"]
    assert conv[0]["reply_count"] == 2 and conv[0]["thread_ts"] == conv[0]["ts"] and conv[1]["thread_ts"] is None
    assert conv[0]["latest_reply"] == "13:20" and conv[0]["conversation_id"] == "D01ABCDEF"
    assert conv[0]["conversation_kind"] == "dm" and conv[0]["sender_id"] == "U01ABCDEF" and conv[0]["sender"] == "Ada Example"
    assert conv[1]["text"] == "Sed do eiusmod tempor <incididunt> ut labore & dolore.\n*Sent using* <@U01APPAPP|SomeApp>"
    assert conv[2]["text"] == "" and conv[2]["ts"] == "1789987596.964679"
    thread = chat_archive.parse("thread", FIXTURE("thread.txt"), conversation_id="C01ABCDEF")["messages"]
    assert thread[0]["thread_ts"] == thread[0]["ts"] and thread[1]["thread_ts"] == thread[0]["ts"]
    assert thread[1]["conversation_id"] == "C01ABCDEF" and thread[1]["sender_id"] == "U01BOBBOB"
    assert thread[2]["text"] == "Lorem ipsum reply in a thread, <@U01ME00000> dolor."


def test_parse_reads_the_registry_and_never_takes_kind_from_the_id_prefix():
    got = chat_archive.parse("list", FIXTURE("list.txt"))["conversations"]
    assert [(c["id"][0], c["kind"]) for c in got] == [("C", "group"), ("D", "dm")]
    assert got[0] == {"id": "C01GRPDM01", "kind": "group", "name": "",
                      "members": "@ada.example, @bob.example, @me.example"}
    assert got[1] == {"id": "D01ABCDEF", "kind": "dm", "name": "Ada Example", "members": "U01ABCDEF"}


def test_parse_takes_the_cursor_from_backticks_or_quotes_and_a_response_without_a_cursor_token_is_the_last_page():
    assert chat_archive.parse("list", FIXTURE("list.txt"))["cursor"] == "dHJ1bmM6"
    assert chat_archive.parse("thread", FIXTURE("thread.txt"), conversation_id="C1")["cursor"] == "bmV4dF90cw"
    assert chat_archive.parse("conversation", FIXTURE("conversation.txt"), conversation_id="D1")["cursor"] == "bmV4dF9jb252"
    assert chat_archive.parse("search", FIXTURE("search-dm.txt"))["cursor"] == "c2VhcmNoLWRt"
    assert chat_archive.parse("search", FIXTURE("search-window.txt"))["cursor"] is None
    assert chat_archive.parse("search", "# Search Results for: \n\n## Messages (0 results)\n")["cursor"] is None
    assert chat_archive.parse("search", "x", pagination_info="For the next page use cursor `abc`")["cursor"] == "abc"
    assert chat_archive.parse("search", "x", pagination_info='Use cursor: "q1"')["cursor"] == "q1"
    assert chat_archive.parse("search", "x", pagination_info="no more pages")["cursor"] is None


def test_unwrap_takes_the_single_string_field_and_the_pagination_info():
    text, page = chat_archive.unwrap(json.dumps({"messages": "A", "pagination_info": "use cursor: `t`"}))
    assert (text, page) == ("A", "use cursor: `t`")
    assert chat_archive.unwrap(json.dumps({"results": "R"})) == ("R", "")
    assert chat_archive.unwrap("plain") == ("plain", "")
    assert chat_archive.unwrap(json.dumps({"n": 1})) == ("", "")


def test_an_unparsed_block_is_counted_not_raised():
    got = chat_archive.parse("conversation", "Channel: DM (D1)\n\n=== Message from X (U1) at now ===\nno ts here\n", conversation_id="D1")
    assert got["messages"] == [] and got["unparsed"] == 1
    got = chat_archive.parse("conversation", "Channel: DM (D1)\n\n=== garbage ===\nno ts here\n", conversation_id="D1")
    assert got["messages"] == [] and got["unparsed"] == 0
    assert chat_archive.parse("search", "# Search Results for: \n\n## Messages (0 results)\n")["unparsed"] == 0


DAY = "2026-10-27"
SEEDED_DM_TS = f"{NOW - 3600:.6f}"
SEEDED_BOT_TS = f"{NOW - 3500:.6f}"
SEEDED_GROUP_TS = f"{NOW - 3000:.6f}"
UNREGISTERED_DM_TS = f"{NOW - 2000:.6f}"
SEEDED_PARENT_TS = "1793106047.590119"
SEEDED_CHANNEL_TS = f"{NOW - 100:.6f}"
ANY_STR = pytest.approx


@pytest.fixture
def con(home):
    connection = chat_archive.open_db()
    yield connection
    connection.close()


def _seed_message(con, cid, ts, text, sender_id="U01ABCDEF", sender="Ada Example", thread_ts=None, bot=0,
                  link="https://chat.example.test/p", captured_at=NOW):
    con.execute("insert into messages (conversation_id, ts, thread_ts, sender_id, sender, bot, text, link, source, "
                "captured_at, edited_at, raw_ref) values (?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, ts, thread_ts, sender_id, sender, bot, text, link, "sweep", captured_at, None, ""))


@pytest.fixture
def seeded(con):
    con.executemany("insert into conversations (id, kind, name, members, last_ts, swept_at) values (?,?,?,?,?,?)", [
        ("D01ABCDEF", "dm", "Ada Example", "U01ABCDEF", SEEDED_BOT_TS, None),
        ("C01GRPDM01", "group", "", "@ada.example, @bob.example, @me.example", SEEDED_GROUP_TS, None),
        ("D01CYCYCY", "dm", "", "", UNREGISTERED_DM_TS, None),
        ("C01ABCDEF", "channel", "some-channel", "", SEEDED_CHANNEL_TS, None)])
    _seed_message(con, "D01ABCDEF", SEEDED_DM_TS, "lorem ipsum lorem")
    _seed_message(con, "D01ABCDEF", SEEDED_BOT_TS, "a bot wrote this", sender_id="U01BOT", sender="Bot Example", bot=1)
    _seed_message(con, "C01GRPDM01", SEEDED_GROUP_TS, "group dolor sit amet")
    _seed_message(con, "D01CYCYCY", UNREGISTERED_DM_TS, "cy says hi", sender_id="U01CYCYCY", sender="Cy Example")
    _seed_message(con, "C01ABCDEF", SEEDED_PARENT_TS, "top level with a mention", thread_ts=SEEDED_PARENT_TS)
    _seed_message(con, "C01ABCDEF", SEEDED_CHANNEL_TS, "a longer reply that says lorem once among many other words",
                  sender_id="U01BOBBOB", sender="Bob Example", thread_ts=SEEDED_PARENT_TS)
    con.commit()
    return con


def test_open_db_creates_the_schema_in_wal_mode_with_private_permissions(home):
    con = chat_archive.open_db()
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    assert tables >= {"conversations", "messages", "threads", "resolved", "cursors", "messages_fts"}
    assert con.execute("pragma journal_mode").fetchone()[0] == "wal"
    assert oct(paths.CHAT_DB.stat().st_mode & 0o777) == "0o600"
    con.close()
    again = chat_archive.open_db()
    assert again.execute("select count(*) from messages").fetchone() == (0,)
    ro = chat_archive.open_db(readonly=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("insert into cursors values ('x', '1', 1)")


def test_record_upserts_by_conversation_and_ts_marks_an_edit_and_keeps_thread_ts_null(home, con):
    parsed = chat_archive.parse("conversation", FIXTURE("conversation.txt"), conversation_id="D01ABCDEF")
    got = chat_archive.record(con, paths.CHAT_RAW, "read_conversation", {"channel_id": "D01ABCDEF"}, "raw", parsed,
                              "sweep", NOW, keep_raw=True)
    assert got == (3, 0)
    assert chat_archive.record(con, paths.CHAT_RAW, "read_conversation", {}, "raw", parsed, "sweep", NOW + 1,
                               keep_raw=False) == (0, 0)
    edited = {**parsed, "messages": [{**parsed["messages"][1], "text": "changed", "thread_ts": ""}]}
    assert chat_archive.record(con, paths.CHAT_RAW, "read_conversation", {}, "raw2", edited, "sweep", NOW + 5,
                               keep_raw=True) == (0, 1)
    row = con.execute("select thread_ts, edited_at, text from messages where ts=?", (parsed["messages"][1]["ts"],)).fetchone()
    assert row == (None, NOW + 5, "changed")
    parent = con.execute("select thread_ts, bot from messages where ts=?", (parsed["messages"][0]["ts"],)).fetchone()
    assert parent == (parsed["messages"][0]["ts"], 0)
    assert con.execute("select kind, last_ts from conversations where id='D01ABCDEF'").fetchone() == (
        "dm", parsed["messages"][0]["ts"])
    lines = (paths.CHAT_RAW / f"{DAY}.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0]) == {"ts": NOW, "tool": "read_conversation",
                                                        "args": {"channel_id": "D01ABCDEF"}, "text": "raw"}
    assert oct((paths.CHAT_RAW / f"{DAY}.jsonl").stat().st_mode & 0o777) == "0o600"
    assert con.execute("select count(*) from messages_fts where messages_fts match 'changed'").fetchone() == (1,)


def test_record_with_keep_raw_false_writes_no_raw_line(home, con):
    parsed = chat_archive.parse("search", FIXTURE("search-dm.txt"))
    paths.CHAT_RAW.mkdir(parents=True, exist_ok=True)
    assert chat_archive.record(con, paths.CHAT_RAW, "search", {}, "raw", parsed, "trigger", NOW, keep_raw=False) == (2, 0)
    assert not list(paths.CHAT_RAW.iterdir())
    assert con.execute("select kind, name from conversations where id='D01ABCDEF'").fetchone() == ("dm", "")


def test_record_never_overwrites_a_registry_kind_or_name(home, con, seeded):
    parsed = chat_archive.parse("search", FIXTURE("search-dm.txt"))
    chat_archive.record(con, paths.CHAT_RAW, "search", {}, "raw", parsed, "trigger", NOW, keep_raw=False)
    assert con.execute("select kind, name from conversations where id='D01ABCDEF'").fetchone() == ("dm", "Ada Example")


def test_read_groups_by_conversation_in_ts_order_and_hides_bots_on_request(home, con, seeded):
    got = chat_archive.read("10d", con=con, now=NOW)
    assert [c["id"] for c in got["conversations"]] == ["D01ABCDEF", "C01GRPDM01", "D01CYCYCY", "C01ABCDEF"]
    assert got["truncated"] is False and got["archive"] == "off"
    first = got["conversations"][0]
    assert (first["kind"], first["name"]) == ("dm", "Ada Example")
    assert [m["ts"] for m in first["messages"]] == [SEEDED_DM_TS, SEEDED_BOT_TS]
    assert set(first["messages"][0]) == {"ts", "time", "sender_id", "sender", "text", "thread_ts", "edited", "link"}
    assert first["messages"][0]["edited"] is False and first["messages"][0]["thread_ts"] is None
    assert all(m["sender_id"] != "U01BOT" for c in chat_archive.read("10d", bots=False, con=con, now=NOW)["conversations"]
               for m in c["messages"])
    thread = chat_archive.read("10d", thread=SEEDED_PARENT_TS, con=con, now=NOW)["conversations"]
    assert [c["id"] for c in thread] == ["C01ABCDEF"] and thread[0]["messages"][0]["ts"] == SEEDED_PARENT_TS
    one = chat_archive.read("10d", conversation="D01CYCYCY", con=con, now=NOW)["conversations"]
    assert [c["id"] for c in one] == ["D01CYCYCY"]
    window = chat_archive.read(f"{NOW - 2500:.6f}", before=f"{NOW - 500:.6f}", con=con, now=NOW)["conversations"]
    assert [c["id"] for c in window] == ["D01CYCYCY", "C01ABCDEF"]
    capped = chat_archive.read("10d", limit=2, con=con, now=NOW)
    assert capped["truncated"] is True and sum(len(c["messages"]) for c in capped["conversations"]) == 2
    assert chat_archive.read("10d", limit=500, con=con, now=NOW)["truncated"] is False


def test_read_of_a_missing_or_empty_archive_says_empty(home, cfg):
    cfg(FULL_SECTION)
    assert chat_archive.read("10d") == {"conversations": [], "truncated": False, "archive": "empty", "since": None}
    chat_archive.open_db().close()
    assert chat_archive.read("10d") == {"conversations": [], "truncated": False, "archive": "empty", "since": None}
    assert chat_archive.search("x") == {"hits": [], "archive": "empty", "since": None}
    assert chat_archive.conversations() == {"conversations": [], "archive": "empty", "since": None}


def test_when_parses_iso_dates_chat_ts_and_relative_days():
    assert chat_archive.when("2026-10-20", NOW, tz=datetime.timezone.utc) == "1792454400.000000"
    assert chat_archive.when("1793106047.590119", NOW) == "1793106047.590119"
    assert chat_archive.when("10d", NOW) == f"{NOW - 10 * 86400:.6f}"
    assert chat_archive.when("3h", NOW) == f"{NOW - 3 * 3600:.6f}"
    assert chat_archive.when("2026-10-20", NOW, tz=datetime.timezone(datetime.timedelta(hours=2))) == "1792447200.000000"
    assert chat_archive.when("2026-10-20T01:00:00+02:00", NOW) == "1792450800.000000"
    assert chat_archive.when("2026-10-20T00:00:00", NOW, tz=datetime.timezone.utc) == "1792454400.000000"
    with pytest.raises(ValueError):
        chat_archive.when("yesterday", NOW)


def test_search_ranks_by_fts_and_a_bad_fts_query_returns_an_error_field_and_no_hits(home, con, seeded):
    got = chat_archive.search("lorem", con=con, now=NOW)
    assert [h["conversation"] for h in got["hits"]] == ["D01ABCDEF", "C01ABCDEF"]
    assert set(got["hits"][0]) == {"conversation", "name", "ts", "time", "sender", "text", "thread_ts", "link"}
    assert got["hits"][0]["name"] == "Ada Example"
    bad = chat_archive.search('"unbalanced', con=con, now=NOW)
    assert bad["hits"] == [] and isinstance(bad["error"], str) and bad["error"]
    assert [h["ts"] for h in chat_archive.search("lorem", sender="U01BOBBOB", con=con, now=NOW)["hits"]] == [SEEDED_CHANNEL_TS]
    assert [h["ts"] for h in chat_archive.search("lorem", conversation="D01ABCDEF", con=con, now=NOW)["hits"]] == [SEEDED_DM_TS]
    assert chat_archive.search("lorem", after=f"{NOW - 1000:.6f}", con=con, now=NOW)["hits"][0]["ts"] == SEEDED_CHANNEL_TS
    assert chat_archive.search("lorem", before=f"{NOW - 1000:.6f}", con=con, now=NOW)["hits"][0]["ts"] == SEEDED_DM_TS
    assert len(chat_archive.search("lorem", limit=1, con=con, now=NOW)["hits"]) == 1


def test_conversations_lists_kind_name_last_ts_and_counts(home, con, seeded):
    got = chat_archive.conversations(con=con, now=NOW)["conversations"]
    assert [c["id"] for c in got] == ["C01ABCDEF", "D01CYCYCY", "C01GRPDM01", "D01ABCDEF"]
    assert got[0] == {"id": "C01ABCDEF", "kind": "channel", "name": "some-channel", "last_ts": SEEDED_CHANNEL_TS,
                      "messages": 2}
    assert [c["id"] for c in chat_archive.conversations(kind="dm", con=con, now=NOW)["conversations"]] == ["D01CYCYCY", "D01ABCDEF"]
    assert [c["id"] for c in chat_archive.conversations(since=f"{NOW - 2500:.6f}", con=con, now=NOW)["conversations"]] == [
        "C01ABCDEF", "D01CYCYCY"]


ME = "U01ME00000"
ROW_TS = f"{NOW - 500:.6f}"
PERSON_ROW = {"conversation_id": "C01OPS", "kind": "channel", "name": "ops-help", "members": "", "ts": ROW_TS,
              "thread_ts": "", "sender_id": "U01ABCDEF", "sender": "Ada Example", "link": "https://chat.example.test/p",
              "captured_at": NOW, "table": "messages"}


def _conversation(con, cid, kind, name="", members=""):
    con.execute("insert into conversations (id, kind, name, members, last_ts, swept_at) values (?,?,?,?,?,?)",
                (cid, kind, name, members, None, None))


def _resolved(con, iden, cid, ts, kind="channel", name="ops-help", thread_ts=None, sender_id="U01PERSON",
              sender_name="Pat Person", link="https://chat.example.test/r", resolved_at=NOW):
    con.execute("insert into resolved values (?,?,?,?,?,?,?,?,?,?)",
                (iden, ts, cid, kind, name, thread_ts, sender_id, sender_name, link, resolved_at))


def _new_rows(con, since_messages=0.0, since_resolved=0.0, floor_ts=NOW - 1000):
    return chat_archive.new_rows(since_messages, since_resolved, floor_ts, ME, con=con)


def test_new_rows_skips_his_own_a_bot_an_old_and_an_unknown_kind_row(home, con):
    _conversation(con, "C01OPS", "channel", "ops-help")
    _conversation(con, "X01NOKIND", "")
    _seed_message(con, "C01OPS", ROW_TS, "a person wrote")
    _seed_message(con, "C01OPS", f"{NOW - 400:.6f}", "he wrote", sender_id=ME)
    _seed_message(con, "C01OPS", f"{NOW - 300:.6f}", "a bot wrote", sender_id="U01BOT", bot=1)
    _seed_message(con, "C01OPS", f"{NOW - 5000:.6f}", "too old")
    _seed_message(con, "X01NOKIND", f"{NOW - 200:.6f}", "no kind")
    assert _new_rows(con) == [PERSON_ROW]


def test_new_rows_treats_a_sender_as_a_bot_by_its_id_or_by_another_row(home, con):
    _conversation(con, "C01OPS", "channel", "ops-help")
    _conversation(con, "C01ELSE", "channel", "else")
    _seed_message(con, "C01OPS", f"{NOW - 400:.6f}", "an app id", sender_id="B01BOT")
    _seed_message(con, "C01ELSE", f"{NOW - 5000:.6f}", "marked", sender_id="U01APP", bot=1, captured_at=NOW - 5000)
    _seed_message(con, "C01OPS", f"{NOW - 300:.6f}", "unmarked row of a marked sender", sender_id="U01APP")
    _resolved(con, "E_1", "C01OPS", f"{NOW - 200:.6f}", sender_id="U01APP")
    _seed_message(con, "C01OPS", ROW_TS, "no sender id", sender_id="")
    assert _new_rows(con) == [{**PERSON_ROW, "sender_id": ""}]


def test_new_rows_gives_one_record_for_a_message_in_both_tables(home, con):
    _conversation(con, "C01OPS", "channel", "ops-help")
    _seed_message(con, "C01OPS", ROW_TS, "in both", sender_id="", link="")
    _resolved(con, "E_1", "C01OPS", ROW_TS, thread_ts="1793106000.000100", resolved_at=NOW + 5)
    assert _new_rows(con) == [{**PERSON_ROW, "thread_ts": "1793106000.000100", "sender_id": "U01PERSON",
                               "link": "https://chat.example.test/r"}]


@pytest.mark.parametrize("message_sender, resolved_sender, captured_at", [
    ("B01BOT", "U01PERSON", NOW), ("B01BOT", "U01PERSON", NOW - 50), ("", ME, NOW)])
def test_new_rows_drops_a_message_when_either_of_its_rows_names_a_bot_or_him(home, con, message_sender,
                                                                              resolved_sender, captured_at):
    _conversation(con, "C01OPS", "channel", "ops-help")
    _seed_message(con, "C01OPS", ROW_TS, "in both", sender_id=message_sender, captured_at=captured_at)
    _resolved(con, "E_1", "C01OPS", ROW_TS, sender_id=resolved_sender)
    assert _new_rows(con, since_messages=NOW - 10, since_resolved=NOW - 10) == []


def test_new_rows_gives_a_resolved_only_message_with_the_resolved_fields(home, con):
    _resolved(con, "E_1", "C01OPS", ROW_TS, thread_ts="1793106000.000100", resolved_at=NOW + 7)
    assert _new_rows(con) == [{
        "conversation_id": "C01OPS", "kind": "channel", "name": "ops-help", "members": "", "ts": ROW_TS,
        "thread_ts": "1793106000.000100", "sender_id": "U01PERSON", "sender": "Pat Person",
        "link": "https://chat.example.test/r", "captured_at": NOW + 7, "table": "resolved"}]


def test_new_rows_reads_the_name_and_id_of_an_external_sender_from_its_stored_string(home, con):
    _conversation(con, "C01OPS", "channel", "ops-help")
    _seed_message(con, "C01OPS", ROW_TS, "from outside", sender_id="",
                  sender="Eve Outside <eve@other.example> (U07EXT0001, external: Other Org)")
    _seed_message(con, "C01OPS", f"{NOW - 400:.6f}", "his, seen from outside", sender_id="",
                  sender=f"Me Example <me@corp.example> ({ME}, external: Corp)")
    _seed_message(con, "C01OPS", f"{NOW - 300:.6f}", "an outside bot", sender_id="",
                  sender="Robo <robo@other.example> (U07EXT0002, external: Other Org)")
    _seed_message(con, "C01OPS", f"{NOW - 5000:.6f}", "marked", sender_id="U07EXT0002", bot=1, captured_at=NOW - 5000)
    _seed_message(con, "C01OPS", f"{NOW - 200:.6f}", "brackets but no id", sender_id="", sender="Ann (on leave)")
    _seed_message(con, "C01OPS", f"{NOW - 100:.6f}", "an id and the raw string", sender_id="U07EXT0003",
                  sender="Dan Outside <dan@other.example> (U07EXT0003, external: Other Org)")
    assert [(r["sender_id"], r["sender"]) for r in _new_rows(con)] == [
        ("U07EXT0001", "Eve Outside"), ("", "Ann (on leave)"), ("U07EXT0003", "Dan Outside")]


def test_new_rows_reads_from_each_mark_inclusive(home, con):
    _conversation(con, "C01OPS", "channel", "ops-help")
    _seed_message(con, "C01OPS", f"{NOW - 400:.6f}", "below the mark", captured_at=NOW - 1)
    _seed_message(con, "C01OPS", f"{NOW - 300:.6f}", "at the mark", captured_at=NOW)
    _resolved(con, "E_1", "C01OPS", f"{NOW - 200:.6f}", resolved_at=NOW + 9)
    _resolved(con, "E_2", "C01OPS", f"{NOW - 100:.6f}", resolved_at=NOW + 10)
    got = _new_rows(con, since_messages=NOW, since_resolved=NOW + 10)
    assert [(r["ts"], r["table"]) for r in got] == [(f"{NOW - 300:.6f}", "messages"), (f"{NOW - 100:.6f}", "resolved")]


def test_new_rows_orders_by_capture_then_ts(home, con):
    _conversation(con, "C01OPS", "channel", "ops-help")
    _seed_message(con, "C01OPS", f"{NOW - 100:.6f}", "later ts, earlier capture", captured_at=NOW - 5)
    _seed_message(con, "C01OPS", f"{NOW - 200:.6f}", "second of the last capture", captured_at=NOW)
    _seed_message(con, "C01OPS", f"{NOW - 300:.6f}", "first of the last capture", captured_at=NOW)
    assert [r["ts"] for r in _new_rows(con)] == [f"{NOW - 100:.6f}", f"{NOW - 300:.6f}", f"{NOW - 200:.6f}"]


def test_high_marks_are_the_largest_marks_and_zero_for_empty_tables(home, con):
    assert chat_archive.high_marks(con) == (0.0, 0.0)
    _seed_message(con, "C01OPS", ROW_TS, "one", captured_at=NOW - 5)
    _seed_message(con, "C01OPS", f"{NOW - 400:.6f}", "two", captured_at=NOW + 3)
    _resolved(con, "E_1", "C01OPS", ROW_TS, resolved_at=NOW + 8)
    _resolved(con, "E_2", "C01OPS", f"{NOW - 400:.6f}", resolved_at=NOW + 2)
    assert chat_archive.high_marks(con) == (NOW + 3, NOW + 8)


def test_prune_removes_only_messages_whose_whole_thread_is_older_than_retention(home, con):
    day = 86400
    parent, reply, lone = f"{NOW - 100 * day:.6f}", f"{NOW - 10 * day:.6f}", f"{NOW - 91 * day:.6f}"
    con.execute("insert into conversations (id, kind, name, members, last_ts, swept_at) values ('C1','channel','c','',?,NULL)", (reply,))
    _seed_message(con, "C1", parent, "old parent", thread_ts=None)
    _seed_message(con, "C1", reply, "fresh reply", thread_ts=parent)
    _seed_message(con, "D1", lone, "lone old dm")
    con.execute("insert into threads (conversation_id, thread_ts, last_reply_ts, checked_at) values ('C1', ?, ?, ?)", (parent, reply, NOW))
    con.execute("insert into threads (conversation_id, thread_ts, last_reply_ts, checked_at) values ('C1', '1.000000', '1.000000', ?)", (NOW,))
    con.execute("insert into resolved values (?,?,?,?,?,?,?,?,?,?)", ("E_old", lone, "D1", "dm", "n", None, "U", "N", "", NOW - 95 * day))
    con.execute("insert into resolved values (?,?,?,?,?,?,?,?,?,?)", ("E_new", reply, "C1", "channel", "n", None, "U", "N", "", NOW))
    con.commit()
    paths.CHAT_RAW.mkdir(parents=True, exist_ok=True)
    old_day = datetime.datetime.fromtimestamp(NOW - 100 * day, datetime.timezone.utc).strftime("%Y-%m-%d")
    (paths.CHAT_RAW / f"{old_day}.jsonl").write_text("{}\n")
    (paths.CHAT_RAW / f"{DAY}.jsonl").write_text("{}\n")
    got = chat_archive.prune(con, NOW, 90, paths.CHAT_RAW)
    assert got == {"messages": 1, "threads": 1, "resolved": 1, "raw_files": 1}
    assert [r[0] for r in con.execute("select ts from messages order by ts")] == [parent, reply]
    assert con.execute("select count(*) from threads").fetchone() == (1,)
    assert sorted(p.name for p in paths.CHAT_RAW.iterdir()) == [f"{DAY}.jsonl"]


# the hands


class FakeRun:
    def __init__(self):
        self.argv, self.kwargs, self.keychain_items, self.claude_items, self.services = [], [], [], [], {}

    def __call__(self, argv, **kwargs):
        self.argv.append(list(argv))
        self.kwargs.append(kwargs)
        if argv[0] == "security" and self.services:
            replies = self.services.get(argv[argv.index("-s") + 1])
            if not replies:
                return subprocess.CompletedProcess(argv, 44, stdout=b"", stderr=b"")
            item = replies.pop(0) if len(replies) > 1 else replies[0]
        else:
            queue = self.keychain_items if argv[0] == "security" else self.claude_items
            if not queue:
                return subprocess.CompletedProcess(argv, 0, stdout=b"{}", stderr=b"")
            item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeResponse:
    def __init__(self, status, body, headers):
        self.status, self._body = status, body
        self.headers = email.message.Message()
        for key, value in headers.items():
            self.headers[key] = value

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Fake:
    def __init__(self, monkeypatch):
        self.run, self.requests, self.responses = FakeRun(), [], []
        monkeypatch.setattr(chat_archive.shutil, "which", lambda name: "/bin/claude")

    def keychain(self, data):
        self.run.keychain_items.append(subprocess.CompletedProcess(["security"], 0, stdout=(json.dumps(data) + "\n").encode(),
                                                                   stderr=b""))

    def keychain_fails(self, exc):
        self.run.keychain_items.append(exc)

    def item(self, service, *replies):
        self.run.services[service] = [
            r if isinstance(r, BaseException)
            else subprocess.CompletedProcess(["security"], r, stdout=b"", stderr=b"") if isinstance(r, int)
            else subprocess.CompletedProcess(["security"], 0, stdout=(json.dumps(r) + "\n").encode(), stderr=b"")
            for r in replies]

    def http(self, status, body=None, headers=None, raw=None):
        self.responses.append((status, body, headers or {}, raw))

    def session(self):
        self.http(200, {"result": {"protocolVersion": "2025-06-18"}}, {"Mcp-Session-Id": "sess-1"})
        self.http(202, None)

    def stream_json(self, pairs, exit_code=0):
        lines = []
        for n, (name, args, text) in enumerate(pairs):
            lines.append({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": f"toolu_{n}", "name": name, "input": args}]}})
            lines.append({"type": "user", "message": {"content": [
                {"tool_use_id": f"toolu_{n}", "type": "tool_result", "content": text}]}})
        lines.append({"type": "result", "result": "done"})
        out = "".join(json.dumps(l) + "\n" for l in lines).encode()
        self.run.claude_items.append(subprocess.CompletedProcess(["claude"], exit_code, stdout=out, stderr=b""))

    def urlopen(self, request, timeout=None):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("no scripted response")
        status, body, headers, raw = self.responses.pop(0)
        if raw is None:
            raw = b"" if body is None else json.dumps(body).encode()
            headers = {"Content-Type": "application/json", **headers}
        if status >= 400:
            message = email.message.Message()
            for key, value in headers.items():
                message[key] = value
            raise urllib.error.HTTPError(request.full_url, status, "err", message, io.BytesIO(raw))
        return FakeResponse(status, raw, headers)


@pytest.fixture
def fake(monkeypatch):
    return Fake(monkeypatch)


@pytest.fixture
def hand(home, cfg, fake):
    cfg(FULL_SECTION)
    return chat_archive.DirectHand(config.chat_archive(), run=fake.run, urlopen=fake.urlopen, now=lambda: NOW)


OK = {"result": {"content": [{"type": "text", "text": "{\"messages\": \"A\"}"}]}}
TOK1 = {"mcpOAuth": {"srv|0123456789abcdef": {"accessToken": "tok", "expiresAt": NOW * 1000 + 3600000}}}
TOK2 = {"mcpOAuth": {"srv|0123456789abcdef": {"accessToken": "tok2", "expiresAt": NOW * 1000 + 7200000}}}


def test_direct_hand_reads_the_keychain_with_the_user_account_and_sends_a_bearer_tools_call(hand, fake):
    fake.keychain({"mcpOAuth": {"srv|0123456789abcdef": {"accessToken": "tok", "expiresAt": NOW * 1000 + 3600000},
                                "other|fedcba9876543210": {"accessToken": "no"}}})
    fake.session()
    fake.http(200, OK)
    assert hand.call("read_conversation", {"channel_id": "D1"}) == "{\"messages\": \"A\"}"
    assert hand.token_key == "srv|0123456789abcdef"
    assert fake.run.argv[0] == ["security", "find-generic-password", "-s", "kc-service", "-a", getpass.getuser(), "-w"]
    assert fake.run.kwargs[0]["capture_output"] is True and fake.run.kwargs[0]["check"] is False
    last = fake.requests[-1]
    assert last.full_url == "https://chat.example.test/mcp" and last.get_header("Authorization") == "Bearer tok"
    body = json.loads(last.data)
    assert body["method"] == "tools/call" and body["jsonrpc"] == "2.0" and isinstance(body["id"], int)
    assert body["params"] == {"name": "read_conversation_tool", "arguments": {"channel_id": "D1"}}
    assert last.get_header("Mcp-protocol-version") == "2025-06-18" and last.get_header("Mcp-session-id") == "sess-1"
    assert last.get_header("Accept") == "application/json, text/event-stream"
    assert len(fake.run.argv) == 1


@pytest.mark.parametrize("entries", [{}, {"srvx": {"accessToken": "1"}}, {"other|a": {"accessToken": "1"}}])
def test_direct_hand_refuses_when_no_entry_matches_the_server(hand, fake, entries):
    fake.keychain({"mcpOAuth": entries})
    with pytest.raises(chat_archive.AuthError, match="keychain: no entry for srv"):
        hand.token()


def test_direct_hand_turns_a_keychain_failure_into_an_auth_error_naming_the_class_only(hand, fake):
    fake.keychain_fails(OSError("secret path"))
    with pytest.raises(chat_archive.AuthError) as err:
        hand.token()
    assert str(err.value) == "keychain: OSError"
    fake.run.keychain_items.append(subprocess.CompletedProcess(["security"], 44, stdout=b"", stderr=b"The specified item could not be found"))
    with pytest.raises(chat_archive.AuthError, match="^keychain: CalledProcessError$"):
        hand.token()
    fake.run.keychain_items.append(subprocess.CompletedProcess(["security"], 0, stdout=b"not json", stderr=b""))
    with pytest.raises(chat_archive.AuthError, match="^keychain: ValueError$"):
        hand.token()


def test_direct_hand_takes_the_freshest_of_several_entries_for_the_server(hand, fake):
    fake.keychain({"mcpOAuth": {"srv|a": {"accessToken": "old"}, "srv|b": {"accessToken": "new", "expiresAt": 2},
                                "srv|c": {"accessToken": "mid", "expiresAt": 1}, "srv": {"accessToken": "bare", "expiresAt": "x"}}})
    assert hand.token() == ("new", "srv|b")


POOL = ('[accounts]\ndefault = "a"\n[[accounts.pool]]\nname = "a"\n'
        '[[accounts.pool]]\nname = "b"\nconfig_dir = "/Users/x/.claude-b"\n'
        '[[accounts.pool]]\nname = "c"\nconfig_dir = "/Users/x/.claude-c"\n')
SVC_B, SVC_C = "kc-service-1de326db", "kc-service-e9f2d001"


def grant(token, hours):
    return {"mcpOAuth": {"srv|0123456789abcdef": {"accessToken": token, "expiresAt": NOW * 1000 + hours * 3600000}}}


def pool_hand(fake):
    return chat_archive.DirectHand(config.chat_archive(), run=fake.run, urlopen=fake.urlopen, now=lambda: NOW)


def three_grants(fake):
    fake.item("kc-service", grant("tok-a", 1))
    fake.item(SVC_B, grant("tok-b", 3))
    fake.item(SVC_C, grant("tok-c", 2))


def test_keychain_items_name_the_default_account_bare_and_every_other_by_its_config_dir_hash(home, cfg):
    cfg(FULL_SECTION + POOL)
    assert chat_archive.keychain_items(config.chat_archive()) == [("a", "kc-service"), ("b", SVC_B), ("c", SVC_C)]
    cfg(FULL_SECTION + POOL.replace('default = "a"', 'default = "c"'))
    items = chat_archive.keychain_items(config.chat_archive())
    assert [n for n, _ in items] == ["c", "a", "b"] and items[0] == ("c", "kc-service") and dict(items)["b"] == SVC_B
    cfg(FULL_SECTION)
    assert chat_archive.keychain_items(config.chat_archive()) == [("a", "kc-service")]


def test_direct_hand_takes_the_freshest_grant_across_every_account_item(home, cfg, fake):
    cfg(FULL_SECTION + POOL)
    three_grants(fake)
    hand = pool_hand(fake)
    assert hand.token() == ("tok-b", "srv|0123456789abcdef") and hand.token_account == "b"
    assert [a[a.index("-s") + 1] for a in fake.run.argv] == ["kc-service", SVC_B, SVC_C]


def test_direct_hand_on_401_retries_with_the_next_freshest_account_token_without_a_nudge(home, cfg, fake, model_pins):
    cfg(FULL_SECTION + POOL)
    three_grants(fake)
    fake.session()
    fake.http(401)
    fake.session()
    fake.http(200, OK)
    hand = pool_hand(fake)
    assert hand.call("search", {}) == "{\"messages\": \"A\"}"
    assert fake.requests[-1].get_header("Authorization") == "Bearer tok-c"
    assert hand.token_account == "c" and hand.auth_retries == 1
    assert [a[0] for a in fake.run.argv] == ["security"] * 6


def test_direct_hand_nudges_once_only_after_every_account_token_got_a_401(home, cfg, fake, model_pins):
    cfg(FULL_SECTION + POOL)
    fake.item("kc-service", *[grant("tok-a", 1)] * 4, grant("tok-a2", 4))
    fake.item(SVC_B, grant("tok-b", 3))
    fake.item(SVC_C, grant("tok-c", 2))
    for _ in range(3):
        fake.session()
        fake.http(401)
    fake.session()
    fake.http(200, OK)
    hand = pool_hand(fake)
    assert hand.call("search", {}) == "{\"messages\": \"A\"}"
    assert fake.requests[-1].get_header("Authorization") == "Bearer tok-a2"
    assert hand.auth_retries == 3 and hand.token_account == "a"
    assert [a[0] for a in fake.run.argv].count("/bin/claude") == 1
    fake.http(401)
    with pytest.raises(chat_archive.AuthError) as err:
        hand.call("search", {})
    assert str(err.value) == "AuthError" and [a[0] for a in fake.run.argv].count("/bin/claude") == 1
    assert not any(t in f"{hand.token_account}{hand.token_key}{hand.auth_retries}" for t in ("tok-a", "tok-b", "tok-c"))


def test_direct_hand_on_401_takes_a_token_another_session_refreshed_without_a_nudge(hand, fake, model_pins):
    fake.keychain(TOK1)
    fake.keychain(TOK2)
    fake.session()
    fake.http(401)
    fake.session()
    fake.http(200, OK)
    assert hand.call("search", {}) == "{\"messages\": \"A\"}"
    assert [a[0] for a in fake.run.argv] == ["security", "security"]
    assert fake.requests[-1].get_header("Authorization") == "Bearer tok2" and hand.auth_retries == 1


def test_direct_hand_skips_an_account_item_it_cannot_read_and_fails_only_when_no_item_reads(home, cfg, fake):
    cfg(FULL_SECTION + POOL)
    fake.item("kc-service", grant("tok-a", 1))
    fake.item(SVC_B, 44)
    fake.item(SVC_C, {"mcpOAuth": {}})
    hand = pool_hand(fake)
    assert hand.token() == ("tok-a", "srv|0123456789abcdef") and hand.token_account == "a"
    for service in ("kc-service", SVC_B, SVC_C):
        fake.item(service, OSError("secret path"))
    with pytest.raises(chat_archive.AuthError) as err:
        pool_hand(fake).token()
    assert str(err.value) == "keychain: OSError"
    fake.item("kc-service", 44)
    with pytest.raises(chat_archive.AuthError, match="^keychain: CalledProcessError$"):
        pool_hand(fake).token()
    fake.item("kc-service", {"mcpOAuth": {}})
    fake.item(SVC_B, 44)
    fake.item(SVC_C, 44)
    with pytest.raises(chat_archive.AuthError, match="^keychain: no entry for srv$"):
        pool_hand(fake).token()


def test_direct_hand_sends_initialize_with_the_spike_protocol_and_initialized_once_per_session(hand, fake):
    fake.keychain(TOK1)
    fake.session()
    fake.http(200, OK)
    fake.http(200, OK)
    hand.call("search", {})
    hand.call("search", {})
    methods = [json.loads(r.data)["method"] for r in fake.requests]
    assert methods == ["initialize", "notifications/initialized", "tools/call", "tools/call"]
    init = json.loads(fake.requests[0].data)
    assert init["params"]["protocolVersion"] == "2025-06-18" and init["params"]["clientInfo"]["name"] == "dockwright-chat-archive"
    assert "id" not in json.loads(fake.requests[1].data) and fake.requests[0].get_header("Mcp-session-id") is None
    assert fake.requests[1].get_header("Mcp-session-id") == "sess-1"
    assert len(fake.run.argv) == 1


def test_direct_hand_batch_runs_the_calls_in_order(hand, fake):
    fake.keychain(TOK1)
    fake.session()
    fake.http(200, {"result": {"content": [{"type": "text", "text": "one"}]}})
    fake.http(200, {"result": {"content": [{"type": "text", "text": "two"}, {"type": "text", "text": "more"}]}})
    assert hand.batch([("search", {"a": 1}), ("read_thread", {"b": 2})]) == ["one", "two\nmore"]
    assert [json.loads(r.data)["params"]["name"] for r in fake.requests[2:]] == ["search_tool", "read_thread_tool"]


def test_direct_hand_on_401_nudges_claude_once_re_reads_the_keychain_and_retries_then_raises_auth_error(hand, fake, model_pins):
    fake.keychain(TOK1)
    fake.http(401)
    fake.keychain(TOK1)
    fake.keychain(TOK2)
    fake.session()
    fake.http(200, OK)
    assert hand.call("search", {}) == "{\"messages\": \"A\"}"
    assert [a[0] for a in fake.run.argv] == ["security", "security", "/bin/claude", "security"]
    nudge = fake.run.argv[2]
    assert nudge[:2] == ["/bin/claude", "-p"] and "list_conversations_tool" in nudge[2] and "types=im" in nudge[2]
    assert nudge[nudge.index("--model") + 1] == "pin-x" and nudge[nudge.index("--max-turns") + 1] == "2"
    assert nudge[nudge.index("--effort") + 1] == "low" and nudge[nudge.index("--output-format") + 1] == "json"
    assert nudge[nudge.index("--allowedTools") + 1] == "mcp__srv__list_conversations_tool"
    assert nudge[nudge.index("--disallowedTools") + 1] == "mcp__srv__write_a,mcp__srv__write_b"
    assert json.loads(nudge[nudge.index("--mcp-config") + 1]) == {"mcpServers": {"srv": {"type": "http", "url": "https://chat.example.test/mcp"}}}
    assert "--no-session-persistence" in nudge and nudge[nudge.index("--tools") + 1] == "" and "--strict-mcp-config" in nudge
    assert nudge[nudge.index("--setting-sources") + 1] == "" and permission_surface_widened(nudge) == []
    kwargs = fake.run.kwargs[2]
    assert kwargs["timeout"] == 60 and not any(k in kwargs["env"] for k in paths.ORCHESTRATOR_ENV_KEYS)
    assert fake.requests[-1].get_header("Authorization") == "Bearer tok2" and hand.token_key == "srv|0123456789abcdef"
    fake.keychain(TOK2)
    fake.http(401)
    with pytest.raises(chat_archive.AuthError, match="^AuthError$"):
        hand.call("search", {})
    assert [a[0] for a in fake.run.argv].count("/bin/claude") == 1


def test_direct_hand_pages_auth_when_the_nudge_itself_fails(hand, fake, model_pins):
    fake.keychain(TOK1)
    fake.http(401)
    fake.keychain(TOK1)
    fake.run.claude_items.append(FileNotFoundError())
    fake.keychain(TOK1)
    with pytest.raises(chat_archive.AuthError, match="^AuthError$"):
        hand.call("search", {})
    assert [a[0] for a in fake.run.argv] == ["security", "security", "/bin/claude", "security"]


def test_direct_hand_nudges_at_most_once_per_call_when_the_nudge_mark_cannot_be_written(hand, fake, model_pins):
    paths.CHAT.parent.mkdir(parents=True, exist_ok=True)
    paths.CHAT.write_text("")
    fake.keychain(TOK1)
    fake.http(401)
    fake.keychain(TOK1)
    fake.keychain(TOK2)
    fake.http(401)
    fake.keychain(TOK2)
    with pytest.raises(chat_archive.AuthError, match="^AuthError$"):
        hand.call("search", {})
    assert [a[0] for a in fake.run.argv] == ["security", "security", "/bin/claude", "security", "security"]


def test_direct_hand_raises_rate_limited_with_retry_after_on_429_and_hand_error_on_5xx(hand, fake):
    fake.keychain(TOK1)
    fake.session()
    fake.http(429, None, {"Retry-After": "7"})
    with pytest.raises(chat_archive.RateLimited) as err:
        hand.call("search", {})
    assert err.value.retry_after == 7.0 and err.value.status == 429 and str(err.value) == "HTTPError 429"
    fake.http(503, None, raw=b"secret body text")
    with pytest.raises(chat_archive.HandError) as err:
        hand.call("search", {})
    assert str(err.value) == "HTTPError 503" and err.value.tool == "search" and "secret" not in repr(err.value)
    fake.responses.append((0, None, {}, None))
    fake.urlopen = lambda request, timeout=None: (_ for _ in ()).throw(urllib.error.URLError("dns secret"))
    hand = chat_archive.DirectHand(config.chat_archive(), run=fake.run, urlopen=fake.urlopen, now=lambda: NOW)
    fake.keychain(TOK1)
    with pytest.raises(chat_archive.HandError) as err:
        hand.call("search", {})
    assert str(err.value) == "URLError" and err.value.status is None


def test_direct_hand_parses_an_event_stream_body_and_a_tool_error(hand, fake):
    fake.keychain(TOK1)
    fake.session()
    stream = b"event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "S"}]}}).encode() + b"\n\n"
    fake.http(200, None, {"Content-Type": "text/event-stream"}, raw=stream)
    assert hand.call("search", {}) == "S"
    fake.http(200, {"result": {"content": [{"type": "text", "text": "boom text"}], "isError": True}})
    with pytest.raises(chat_archive.HandError) as err:
        hand.call("search", {})
    assert str(err.value) == "ToolError" and "boom" not in str(err.value)
    fake.http(200, {"error": {"code": -32600, "message": "bad"}})
    with pytest.raises(chat_archive.HandError, match="^JSONRPCError$"):
        hand.call("search", {})


def test_claude_hand_orders_exactly_the_calls_and_checks_the_tool_result_pairs(cfg, fake, model_pins):
    cfg(FULL_SECTION)
    hand = chat_archive.ClaudeHand(config.chat_archive(), run=fake.run)
    fake.stream_json([("mcp__srv__read_conversation_tool", {"channel_id": "D1"}, "{\"messages\": \"A\"}")])
    assert hand.call("read_conversation", {"channel_id": "D1"}) == "{\"messages\": \"A\"}"
    argv, kwargs = fake.run.argv[0], fake.run.kwargs[0]
    assert argv[:2] == ["/bin/claude", "-p"] and argv[argv.index("--output-format") + 1] == "stream-json" and "--verbose" in argv
    assert argv[argv.index("--model") + 1] == "pin-x" and argv[argv.index("--max-turns") + 1] == "2"
    assert argv[argv.index("--allowedTools") + 1] == ("mcp__srv__search_tool,mcp__srv__read_conversation_tool,"
                                                       "mcp__srv__read_thread_tool,mcp__srv__list_conversations_tool")
    assert argv[argv.index("--disallowedTools") + 1] == "mcp__srv__write_a,mcp__srv__write_b"
    assert nudge_lockdown(argv) and permission_surface_widened(argv) == []
    assert json.loads(kwargs["input"]) == [{"tool": "mcp__srv__read_conversation_tool", "arguments": {"channel_id": "D1"}}]
    assert kwargs["timeout"] == 90 and not any(k in kwargs["env"] for k in paths.ORCHESTRATOR_ENV_KEYS)
    fake.stream_json([("mcp__srv__search_tool", {}, [{"type": "text", "text": "L1"}, {"type": "text", "text": "L2"}]),
                      ("mcp__srv__read_thread_tool", {"x": 1}, "T")])
    assert hand.batch([("search", {}), ("read_thread", {"x": 1})]) == ["L1\nL2", "T"]
    assert fake.run.argv[1][fake.run.argv[1].index("--max-turns") + 1] == "3" and fake.run.kwargs[1]["timeout"] == 120
    fake.stream_json([("mcp__srv__search_tool", {}, "wrong order")])
    with pytest.raises(chat_archive.HandError, match="^hand: call 1 missing$") as err:
        hand.batch([("read_thread", {}), ("search", {})])
    assert err.value.tool == "claude"
    fake.stream_json([("mcp__srv__search_tool", {"q": 1}, "args differ")])
    with pytest.raises(chat_archive.HandError, match="^hand: call 1 missing$"):
        hand.call("search", {"q": 2})


def nudge_lockdown(argv):
    return (argv[argv.index("--tools") + 1] == "" and "--strict-mcp-config" in argv and "--no-session-persistence" in argv
            and argv[argv.index("--setting-sources") + 1] == "")


@pytest.mark.parametrize("item, reason", [
    (FileNotFoundError(), "FileNotFoundError"), (subprocess.TimeoutExpired("claude", 90), "TimeoutExpired"),
    (subprocess.CompletedProcess(["claude"], 2, stdout=b"", stderr=b"secret stderr"), "CalledProcessError")])
def test_claude_hand_failures_carry_the_class_only(cfg, fake, model_pins, item, reason):
    cfg(FULL_SECTION)
    hand = chat_archive.ClaudeHand(config.chat_archive(), run=fake.run)
    fake.run.claude_items.append(item)
    with pytest.raises(chat_archive.HandError) as err:
        hand.call("search", {})
    assert str(err.value) == reason and err.value.tool == "claude" and "secret" not in repr(err.value)


def test_claude_hand_retries_a_file_pointer_result_with_half_the_limit(cfg, fake, model_pins):
    cfg(FULL_SECTION)
    hand = chat_archive.ClaudeHand(config.chat_archive(), run=fake.run)
    fake.stream_json([("mcp__srv__read_conversation_tool", {"channel_id": "D1", "limit": 40}, "Error: result (90000 characters) exceeds")])
    fake.stream_json([("mcp__srv__read_conversation_tool", {"channel_id": "D1", "limit": 20}, "small")])
    assert hand.call("read_conversation", {"channel_id": "D1", "limit": 40}) == "small"
    assert json.loads(fake.run.kwargs[1]["input"])[0]["arguments"] == {"channel_id": "D1", "limit": 20}
    fake.stream_json([("mcp__srv__read_conversation_tool", {"channel_id": "D1", "limit": 40}, "Error: result (90000 characters) exceeds")])
    fake.stream_json([("mcp__srv__read_conversation_tool", {"channel_id": "D1", "limit": 20}, "Error: result (80000 characters) exceeds")])
    with pytest.raises(chat_archive.HandError, match="^hand: call 1 oversized$") as err:
        hand.call("read_conversation", {"channel_id": "D1", "limit": 40})
    assert err.value.tool == "claude"


def test_claude_hand_splits_a_batch_past_the_byte_budget(cfg, fake, model_pins):
    cfg(FULL_SECTION)
    hand = chat_archive.ClaudeHand(config.chat_archive(), run=fake.run)
    calls = [("search", {"n": i}) for i in range(4)]
    fake.stream_json([("mcp__srv__search_tool", {"n": i}, f"r{i}") for i in range(3)])
    fake.stream_json([("mcp__srv__search_tool", {"n": 3}, "r3")])
    assert hand.batch(calls) == ["r0", "r1", "r2", "r3"]
    assert len(fake.run.argv) == 2 and chat_archive.BATCH_BYTES == 150_000


def test_hand_for_picks_by_the_hand_key(cfg):
    cfg(FULL_SECTION)
    assert isinstance(chat_archive.hand_for({**config.chat_archive(), "hand": "claude"}), chat_archive.ClaudeHand)
    assert isinstance(chat_archive.hand_for(config.chat_archive()), chat_archive.DirectHand)


def test_hand_errors_render_the_reason_and_the_status_only():
    assert str(chat_archive.HandError("HTTPError", status=503)) == "HTTPError 503"
    assert str(chat_archive.HandError("TimeoutExpired", tool="claude")) == "TimeoutExpired"
    assert chat_archive.RateLimited("HTTPError", status=429, retry_after=2.5).retry_after == 2.5
    assert issubclass(chat_archive.AuthError, chat_archive.HandError) and issubclass(chat_archive.RateLimited, chat_archive.HandError)


# the pass

T0 = 1793106000.0
DM_TS = "1793106658.209309"
DM2_TS = "1793107259.250289"
PARENT_TS = "1793106047.590119"
REPLY_TS = "1793107259.250289"
TOP_TS = "1793107260.000100"


def DAY_BEFORE(ts):
    return datetime.datetime.fromtimestamp(float(ts) - 86400, datetime.timezone.utc).strftime("%Y-%m-%d")


def paged(text, cursor):
    field = "results" if text.startswith("# Search") else "result" if text.startswith("## My") else "messages"
    return json.dumps({field: text, "pagination_info": f"use cursor: `{cursor}`" if cursor else "last page"})


class FakeHand:
    def __init__(self):
        self.calls, self.scripts, self.failures, self.token_key = [], [], [], "srv|0123456789abcdef"
        self.token_account, self.auth_retries = "a", 0
        self.fail_every = None
        self.batches = []

    def script(self, tool, match, *responses):
        self.scripts.append((tool, match, list(responses)))

    def fail_on(self, tool, match, err):
        self.failures.append((tool, match, err))

    def fail_all(self, err):
        self.fail_every = err

    def http_error(self, tool, match, status, body):
        self.failures.append((tool, match, chat_archive.HandError("HTTPError", status=status, tool=tool)))

    @staticmethod
    def _matches(match, args):
        return all(args.get(k) == v for k, v in match.items())

    def call(self, tool, args):
        self.calls.append((tool, dict(args)))
        if self.fail_every is not None:
            raise self.fail_every
        for t, match, err in self.failures:
            if t == tool and self._matches(match, args):
                raise err
        for t, match, responses in self.scripts:
            if t == tool and self._matches(match, args):
                return responses.pop(0) if len(responses) > 1 else responses[0]
        return {"search": "# Search Results for: \n\n## Messages (0 results)\n", "list_conversations": "## My Channels (showing 0)\n"}.get(tool, "")

    def batch(self, calls):
        self.batches.append([(t, dict(a)) for t, a in calls])
        out = []
        for t, a in calls:
            try:
                out.append(self.call(t, a))
            except chat_archive.HandError as err:
                if not chat_archive.item_failure(err):
                    raise
                out.append(err)
        return out

    @property
    def search_calls(self):
        return sum(1 for c in self.calls if c[0] == "search")


@pytest.fixture
def cfg_dict(cfg):
    cfg(FULL_SECTION)
    return config.chat_archive()


@pytest.fixture
def fake_hand(home):
    hand = FakeHand()
    hand.script("list_conversations", {}, paged(FIXTURE("list.txt"), None))
    hand.script("read_conversation", {"channel_id": "D01ABCDEF", "cursor": None}, paged(FIXTURE("conversation.txt"), None))
    hand.script("read_thread", {"channel_id": "C01ABCDEF", "message_ts": PARENT_TS}, paged(FIXTURE("thread.txt"), None))
    return hand


def run_pass(cfg_dict, hand, now, wake="schedule", managers=None, sleep=None):
    return chat_archive.pass_(cfg_dict, hand=hand, now=now, wake=wake, managers=managers or [{"name": "m1"}],
                              sleep=sleep or (lambda s: None))


def con_ro():
    return chat_archive.open_db(readonly=True)


def cursor(step):
    row = con_ro().execute("select ts from cursors where step = ?", (step,)).fetchone()
    return row[0] if row else None


def outbox_lines(name):
    folder = paths.notify_outbox_dir_for(name)
    return [json.loads(p.read_text())["line"] for p in sorted(folder.glob("*.json"))] if folder.is_dir() else []


def write_trigger(key, ts, subt, delivered, kind="dm", tries=0, last_try=None):
    paths.CHAT_TRIGGERS.mkdir(parents=True, exist_ok=True)
    body = {"key": key, "iden": f"E_{ts}", "ts": ts, "subt": subt, "kind": kind, "delivered": delivered, "tries": tries}
    if last_try is not None:
        body["last_try"] = last_try
    path = paths.CHAT_TRIGGERS / f"{key.replace(':', '_')}.json"
    path.write_text(json.dumps(body))
    return path


def ledger():
    return [json.loads(l) for l in paths.CHAT_LEDGER.read_text().splitlines()]


@pytest.fixture
def awake(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(failure_streak, "awake_seconds", lambda: clock["t"])
    return clock


def streaks():
    path = paths.CHAT / "failures.json"
    return json.loads(path.read_text()) if path.is_file() else {}


def pass_at(cfg_dict, hand, awake, at, **kw):
    awake["t"] = float(at)
    return run_pass(cfg_dict, hand, T0 + at, **kw)


HTTP_503 = chat_archive.HandError("HTTPError", status=503, tool="search")


def test_a_pass_with_an_empty_registry_runs_the_registry_step_first(cfg_dict, fake_hand):
    run_pass(cfg_dict, fake_hand, T0)
    assert fake_hand.calls[0] == ("list_conversations", {"types": "im,mpim", "limit": 200})
    rows = con_ro().execute("select id, kind, name, members from conversations order by id").fetchall()
    assert ("C01GRPDM01", "group", "", "@ada.example, @bob.example, @me.example") in rows
    assert ("D01ABCDEF", "dm", "Ada Example", "U01ABCDEF") in rows


def test_the_first_pass_floors_every_cursor_at_the_started_mark_and_reads_nothing_older(cfg_dict, fake_hand):
    fake_hand.script("search", {"filters": f"is:dm after:{DAY_BEFORE(T0)}"}, paged(FIXTURE("search-dm.txt"), None))
    fake_hand.script("search", {"keywords": ["U01ME00000"]}, paged(FIXTURE("search-context.txt"), None))
    rec = run_pass(cfg_dict, fake_hand, T0)
    assert paths.CHAT_STARTED.exists() and cursor("started") == f"{T0:.6f}" and rec["full"] is True
    for step in ("dm", "threads", "sweep", "registry", *(f"mention:{k}" for k in cfg_dict["mention_keys"])):
        assert cursor(step) is not None, step
    dm_search = next(c for c in fake_hand.calls if c[0] == "search" and "is:dm" in c[1]["filters"])
    assert dm_search[1] == {"filters": f"is:dm after:{DAY_BEFORE(T0)}", "after": f"{T0 - 900:.6f}", "sort": "timestamp",
                            "sort_dir": "asc", "response_format": "detailed", "include_context": False,
                            "include_bots": True, "limit": 20}
    assert all(c[1]["after"] == f"{T0 - 900:.6f}" for c in fake_hand.calls if c[0] == "search" and "keywords" in c[1])
    reads = [c for c in fake_hand.calls if c[0] == "read_conversation"]
    assert len(reads) == 1 and reads[0][1] == {"channel_id": "D01ABCDEF", "limit": 40, "response_format": "detailed",
                                               "oldest": f"{T0:.6f}"}
    assert all("oldest" not in c[1] for c in fake_hand.calls if c[0] == "read_thread")
    assert cursor("dm") == DM2_TS and cursor("mention:U01ME00000") == PARENT_TS
    assert rec["messages_new"] > 0 and rec["calls"] == len(fake_hand.calls) and rec["searches"] == 5
    assert [[c[0] for c in b] for b in fake_hand.batches] == [["read_conversation"], ["read_thread"], ["read_thread", "read_thread"]]
    assert rec["token_key"] == "srv|0123456789abcdef" and rec["errors"] == [] and rec["wake"] == "schedule"
    assert rec["token_account"] == "a" and rec["auth_retries"] == 0
    assert oct(paths.CHAT_LEDGER.stat().st_mode & 0o777) == "0o600" and ledger()[-1]["kind"] == "pass"


def test_a_dm_trigger_resolves_by_exact_ts_reads_the_conversation_to_the_first_known_ts_and_removes_the_file(cfg_dict, fake_hand):
    run_pass(cfg_dict, fake_hand, T0)
    fake_hand.calls.clear()
    fake_hand.script("search", {"before": f"{float(DM_TS) + 1.5:.6f}"}, paged(FIXTURE("search-dm.txt"), None))
    file = write_trigger(f"E_{DM_TS}", DM_TS, "Ada Example", T0 + 60)
    rec = run_pass(cfg_dict, fake_hand, T0 + 90, wake="trigger")
    assert fake_hand.calls[0] == ("search", {"filters": f"after:{DAY_BEFORE(DM_TS)}", "after": f"{float(DM_TS) - 1:.6f}",
                                             "before": f"{float(DM_TS) + 1.5:.6f}", "sort": "timestamp",
                                             "response_format": "detailed", "include_context": False, "include_bots": True,
                                             "limit": 20})
    assert fake_hand.calls[1][0] == "read_conversation" and fake_hand.calls[1][1]["channel_id"] == "D01ABCDEF"
    assert fake_hand.calls[1][1]["oldest"] == f"{T0:.6f}" and len(fake_hand.calls) == 2
    assert rec["triggers_done"] == 1 and not file.exists() and rec["full"] is False
    assert con_ro().execute("select conversation_id, kind, name, thread_ts, sender_id, sender_name, link from resolved "
                            "where iden = ?", (f"E_{DM_TS}",)).fetchone() == (
        "D01ABCDEF", "dm", "", None, "U01BOT000", "Bot Example",
        "https://chat.example.test/archives/D01ABCDEF/p1793106658209309")
    assert con_ro().execute("select kind from conversations where id='D01ABCDEF'").fetchone() == ("dm",)
    assert (paths.CHAT_RAW / f"{DAY}.jsonl").exists()


def test_a_conversation_read_pages_newest_first_until_a_known_ts(cfg_dict, fake_hand):
    fake_hand.script("search", {"filters": f"is:dm after:{DAY_BEFORE(T0)}"}, paged(FIXTURE("search-dm.txt"), None))
    run_pass(cfg_dict, fake_hand, T0)
    fake_hand.calls.clear()
    fake_hand.scripts = [s for s in fake_hand.scripts if s[0] != "read_conversation"]
    fake_hand.script("search", {"before": f"{float(DM_TS) + 1.5:.6f}"}, paged(FIXTURE("search-dm.txt"), None))
    fresh = FIXTURE("conversation.txt").replace("1793106047.590119", "1793106900.000001").replace("1793104809.851149", "1793106900.000002")
    fake_hand.script("read_conversation", {"channel_id": "D01ABCDEF", "cursor": None}, paged(fresh.replace("1789987596.964679", "1793106900.000003"), "p2"))
    fake_hand.script("read_conversation", {"channel_id": "D01ABCDEF", "cursor": "p2"}, paged(FIXTURE("conversation.txt"), "p3"))
    fake_hand.script("read_conversation", {"channel_id": "D01ABCDEF", "cursor": "p3"}, paged(fresh, "p4"))
    write_trigger(f"E_{DM_TS}", DM_TS, "Ada Example", T0 + 60)
    rec = run_pass(cfg_dict, fake_hand, T0 + 90, wake="trigger")
    reads = [c for c in fake_hand.calls if c[0] == "read_conversation"]
    assert [c[1].get("cursor") for c in reads] == [None, "p2"] and rec["messages_new"] == 3


def test_a_window_search_with_several_results_resolves_only_the_exact_ts(cfg_dict, fake_hand):
    run_pass(cfg_dict, fake_hand, T0)
    fake_hand.calls.clear()
    fake_hand.script("search", {"sort": "timestamp"}, paged(FIXTURE("search-dm.txt"), None))
    exact = write_trigger(f"E_{DM2_TS}", DM2_TS, "Ada Example", T0 + 60)
    nowhere = write_trigger("E_1793106659.000000", "1793106659.000000", "Ada Example", T0 + 61)
    rec = run_pass(cfg_dict, fake_hand, T0 + 90, wake="trigger")
    assert rec["triggers_done"] == 1 and not exact.exists() and nowhere.exists()
    assert json.loads(nowhere.read_text())["tries"] == 1 and json.loads(nowhere.read_text())["last_try"] == T0 + 90
    assert con_ro().execute("select ts from resolved").fetchall() == [(DM2_TS,)]


def test_a_channel_trigger_without_a_mention_key_writes_resolved_only_and_no_text_anywhere(cfg_dict, fake_hand):
    run_pass(cfg_dict, fake_hand, T0 - 100)
    fake_hand.calls.clear()
    fake_hand.script("search", {"sort": "timestamp"}, paged(FIXTURE("search-window.txt"), None))
    write_trigger(f"E_{TOP_TS}", TOP_TS, "#some-channel", T0 + 60, kind="channel")
    rec = run_pass(cfg_dict, fake_hand, T0 + 90, wake="trigger")
    assert rec["triggers_done"] == 1 and [c[0] for c in fake_hand.calls] == ["search"]
    con = con_ro()
    assert con.execute("select count(*) from messages").fetchone()[0] == 0
    assert [p.name for p in paths.CHAT_RAW.iterdir()] == [f"{DAY}.jsonl"] and "lorem" not in (paths.CHAT_RAW / f"{DAY}.jsonl").read_text()
    assert con.execute("select conversation_id, kind, name, thread_ts, sender_id, sender_name, link from resolved "
                       "where iden = ?", (f"E_{TOP_TS}",)).fetchone() == (
        "C01ABCDEF", "channel", "some-channel", None, "U01BOBBOB", "Bob Example",
        "https://chat.example.test/archives/C01ABCDEF/p1793107260000100")
    assert "lorem" not in paths.CHAT_LEDGER.read_text() and "eiusmod" not in paths.CHAT_LEDGER.read_text()


def test_a_channel_trigger_with_a_mention_key_or_a_known_thread_reads_the_thread(cfg_dict, fake_hand):
    run_pass(cfg_dict, fake_hand, T0 - 100)
    fake_hand.calls.clear()
    fake_hand.script("search", {"sort": "timestamp"}, paged(FIXTURE("search-window.txt"), None))
    write_trigger(f"E_{REPLY_TS}", REPLY_TS, "#some-channel", T0 + 60, kind="channel")
    rec = run_pass(cfg_dict, fake_hand, T0 + 90, wake="trigger")
    assert rec["triggers_done"] == 1
    assert [c for c in fake_hand.calls if c[0] == "read_thread"] == [
        ("read_thread", {"channel_id": "C01ABCDEF", "message_ts": PARENT_TS, "response_format": "detailed", "limit": 1000})]
    assert [c[0] for c in fake_hand.calls].count("read_conversation") == 0 and rec["full"] is False
    con = con_ro()
    assert con.execute("select count(*) from messages where conversation_id='C01ABCDEF'").fetchone() == (3,)
    assert con.execute("select thread_ts, last_reply_ts from threads").fetchall() == [(PARENT_TS, REPLY_TS)]
    assert con.execute("select thread_ts from resolved").fetchone() == (PARENT_TS,)
    assert "mention_text_marker" not in paths.CHAT_LEDGER.read_text() and (paths.CHAT_RAW / f"{DAY}.jsonl").exists()
    fake_hand.calls.clear()
    con.close()
    write_trigger(f"E2_{REPLY_TS}", REPLY_TS, "#some-channel", T0 + 100, kind="channel")
    run_pass({**cfg_dict, "mention_keys": []}, fake_hand, T0 + 150, wake="trigger")
    assert [c[1]["message_ts"] for c in fake_hand.calls if c[0] == "read_thread"] == [PARENT_TS]
    fake_hand.calls.clear()
    with chat_archive.open_db() as w:
        w.execute("delete from threads")
    write_trigger(f"E3_{REPLY_TS}", REPLY_TS, "#some-channel", T0 + 160, kind="channel")
    rec = run_pass({**cfg_dict, "mention_keys": []}, fake_hand, T0 + 170, wake="trigger")
    assert [c[0] for c in fake_hand.calls] == ["search"] and rec["triggers_done"] == 1


def test_a_missed_trigger_retries_no_sooner_than_a_minute_and_dies_an_hour_after_delivery(cfg_dict, fake_hand):
    run_pass(cfg_dict, fake_hand, T0 - 100)
    fake_hand.calls.clear()
    file = write_trigger("E_1000000000.000000", "1000000000.000000", "Ada Example", T0)
    fake_hand.calls.clear()
    rec1 = run_pass(cfg_dict, fake_hand, T0 + 10, wake="trigger")
    assert rec1["triggers_dead"] == 0 and json.loads(file.read_text())["tries"] == 1 and fake_hand.search_calls == 1
    assert rec1["errors"] == [] and outbox_lines("m1") == []
    run_pass(cfg_dict, fake_hand, T0 + 30, wake="trigger")
    assert fake_hand.search_calls == 1
    run_pass(cfg_dict, fake_hand, T0 + 71, wake="trigger")
    assert fake_hand.search_calls == 2 and json.loads(file.read_text())["tries"] == 2
    rec3 = run_pass(cfg_dict, fake_hand, T0 + 3700, wake="trigger")
    assert rec3["triggers_dead"] == 1 and not file.exists()
    assert sum(1 for c in fake_hand.calls if c[0] == "search" and "before" in c[1]) == 2


def test_a_trigger_wake_with_an_empty_queue_and_a_fresh_full_pass_is_a_no_op(cfg_dict, fake_hand):
    run_pass(cfg_dict, fake_hand, T0)
    fake_hand.calls.clear()
    (paths.CHAT_TRIGGERS / ".wake").touch()
    rec = run_pass(cfg_dict, fake_hand, T0 + 30, wake="trigger")
    assert fake_hand.calls == [] and rec["calls"] == 0 and rec["full"] is False
    rec = run_pass(cfg_dict, fake_hand, T0 + 301, wake="trigger")
    assert rec["full"] is True and fake_hand.calls


def test_a_broken_trigger_file_is_removed_and_counted_dead(cfg_dict, fake_hand):
    paths.CHAT_TRIGGERS.mkdir(parents=True, exist_ok=True)
    (paths.CHAT_TRIGGERS / "junk.json").write_text("{nope")
    rec = run_pass(cfg_dict, fake_hand, T0, wake="trigger")
    assert rec["triggers_dead"] == 1 and not (paths.CHAT_TRIGGERS / "junk.json").exists()


def test_the_dm_search_starts_at_the_cursor_minus_the_overlap_and_moves_the_cursor_in_the_same_transaction(cfg_dict, fake_hand):
    fake_hand.script("search", {"filters": f"is:dm after:{DAY_BEFORE(T0)}"}, paged(FIXTURE("search-dm.txt"), None))
    run_pass(cfg_dict, fake_hand, T0)
    assert cursor("dm") == DM2_TS
    fake_hand.calls.clear()
    fake_hand.scripts.insert(0, ("search", {"after": f"{float(DM2_TS) - 900:.6f}"}, [paged(FIXTURE("search-dm.txt").replace("D01ABCDEF", "D09ROLLBK"), None)]))
    fake_hand.fail_on("read_conversation", {"channel_id": "D09ROLLBK"}, chat_archive.HandError("HTTPError", status=500))
    rec = run_pass(cfg_dict, fake_hand, T0 + 400)
    dm_search = next(c for c in fake_hand.calls if c[0] == "search" and "is:dm" in c[1]["filters"])
    assert dm_search[1]["after"] == f"{float(DM2_TS) - 900:.6f}" and dm_search[1]["filters"] == f"is:dm after:{DAY_BEFORE(DM2_TS)}"
    assert cursor("dm") == DM2_TS and rec["errors"] == ["HTTPError 500"]
    assert con_ro().execute("select count(*) from messages where conversation_id = 'D01ABCDEF'").fetchone() == (5,)
    assert con_ro().execute("select count(*) from messages where conversation_id = 'D09ROLLBK'").fetchone() == (0,)


def test_a_conversation_read_stops_at_the_page_cap_and_keeps_what_it_read(cfg_dict, fake_hand):
    fake_hand.scripts = [s for s in fake_hand.scripts if s[0] != "read_conversation"]
    fake_hand.script("search", {"filters": f"is:dm after:{DAY_BEFORE(T0)}"}, paged(FIXTURE("search-dm.txt"), None))
    for n in range(12):
        text = FIXTURE("conversation.txt").replace("1793106047.590119", f"1793106047.{n:06d}")
        fake_hand.script("read_conversation", {"cursor": f"p{n}"} if n else {"channel_id": "D01ABCDEF", "cursor": None}, paged(text, f"p{n + 1}"))
    rec = run_pass(cfg_dict, fake_hand, T0)
    assert sum(1 for c in fake_hand.calls if c[0] == "read_conversation") == 10 and rec["pages_capped"] == 1
    assert con_ro().execute("select count(*) from messages where ts like '1793106047.%'").fetchone() == (10,)


def test_each_mention_key_is_searched_by_keyword_and_each_hit_reads_its_whole_thread_never_the_channel(cfg_dict, fake_hand):
    fake_hand.script("search", {"keywords": ["S01GRP0001"]}, paged(FIXTURE("search-context.txt"), None))
    fake_hand.script("search", {"keywords": ["U01ME00000"]}, paged(FIXTURE("search-window.txt"), None))
    run_pass(cfg_dict, fake_hand, T0)
    keys = cfg_dict["mention_keys"]
    assert [c[1] for c in fake_hand.calls if c[0] == "search" and "keywords" in c[1]] == [
        {"keywords": [k], "filters": f"after:{DAY_BEFORE(T0)}", "after": f"{T0 - 900:.6f}", "sort": "timestamp",
         "sort_dir": "asc", "response_format": "detailed", "include_context": False, "include_bots": True, "limit": 20}
        for k in keys]
    kinds = dict(con_ro().execute("select id, kind from conversations"))
    assert all(kinds.get(c[1]["channel_id"]) != "channel" for c in fake_hand.calls if c[0] == "read_conversation")
    threads = [c for c in fake_hand.calls if c[0] == "read_thread"]
    assert {c[1]["message_ts"] for c in threads} == {PARENT_TS, TOP_TS}
    assert all(c[1] == {"channel_id": "C01ABCDEF", "message_ts": c[1]["message_ts"], "response_format": "detailed",
                        "limit": 1000} for c in threads)
    assert cursor("mention:U01ME00000") == TOP_TS and cursor("mention:S01GRP0001") == PARENT_TS
    assert cursor("mention:S01GRP0002") == f"{T0:.6f}"
    assert con_ro().execute("select count(*) from messages where conversation_id = 'C01ABCDEF'").fetchone()[0] >= 3


def test_live_threads_are_reread_each_pass_for_seven_days_then_daily_then_never_after_retention(cfg_dict, fake_hand):
    fake_hand.script("search", {"keywords": ["U01ME00000"]}, paged(FIXTURE("search-context.txt"), None))
    run_pass(cfg_dict, fake_hand, T0)
    fake_hand.scripts = [s for s in fake_hand.scripts if s[0] != "search"]
    fake_hand.calls.clear()
    run_pass(cfg_dict, fake_hand, T0 + 400)
    assert sum(1 for c in fake_hand.calls if c[0] == "read_thread") == 1
    with chat_archive.open_db() as w:
        w.execute("update threads set last_reply_ts = ?, checked_at = ?", (f"{T0 - 8 * 86400:.6f}", T0 + 400))
    fake_hand.calls.clear()
    run_pass(cfg_dict, fake_hand, T0 + 800)
    assert sum(1 for c in fake_hand.calls if c[0] == "read_thread") == 0
    run_pass(cfg_dict, fake_hand, T0 + 400 + 86401)
    assert sum(1 for c in fake_hand.calls if c[0] == "read_thread") == 1
    with chat_archive.open_db() as w:
        w.execute("update threads set last_reply_ts = ?, checked_at = 0", (f"{T0 - 91 * 86400:.6f}",))
    fake_hand.calls.clear()
    run_pass(cfg_dict, fake_hand, T0 + 2 * 86400)
    assert sum(1 for c in fake_hand.calls if c[0] == "read_thread") == 0


def test_a_conversation_page_parent_with_replies_joins_the_thread_schedule(cfg_dict, fake_hand):
    fake_hand.script("search", {"filters": f"is:dm after:{DAY_BEFORE(T0)}"}, paged(FIXTURE("search-dm.txt"), None))
    fake_hand.script("read_thread", {"channel_id": "D01ABCDEF", "message_ts": PARENT_TS}, paged(FIXTURE("thread.txt"), None))
    run_pass(cfg_dict, fake_hand, T0)
    assert con_ro().execute("select conversation_id, thread_ts, last_reply_ts from threads").fetchall() == [
        ("D01ABCDEF", PARENT_TS, REPLY_TS)]
    assert [c for c in fake_hand.calls if c[0] == "read_thread"][0][1]["channel_id"] == "D01ABCDEF"


def test_the_hourly_sweep_rereads_the_last_page_of_active_conversations_and_probes_idle_group_dms(cfg_dict, fake_hand):
    probe = FIXTURE("conversation.txt").replace("D01ABCDEF", "C01GRPDM01").split("=== Message from Bob")[0]
    fake_hand.script("read_conversation", {"channel_id": "C01GRPDM01", "limit": 1}, paged(probe, None))
    fake_hand.script("read_conversation", {"channel_id": "C01GRPDM01", "limit": 40}, paged(probe, None))
    fake_hand.scripts = [s for s in fake_hand.scripts if s[1].get("channel_id") != "D01ABCDEF"]
    fake_hand.script("read_conversation", {"channel_id": "D01ABCDEF", "limit": 40}, paged(FIXTURE("conversation.txt"), "sw2"))
    run_pass(cfg_dict, fake_hand, T0)
    with chat_archive.open_db() as w:
        w.execute("update conversations set last_ts = ? where id = 'D01ABCDEF'", (f"{T0 - 86400:.6f}",))
        w.execute("update conversations set last_ts = ? where id = 'C01GRPDM01'", (f"{T0 - 30 * 86400:.6f}",))
        w.execute("insert into messages (conversation_id, ts, sender_id, sender, text, captured_at, contiguous) values ('D01ABCDEF', ?, 'U01ABCDEF', 'Ada Example', 'before edit', ?, 1)", (PARENT_TS, T0))
    fake_hand.calls.clear()
    run_pass(cfg_dict, fake_hand, T0 + 600)
    assert [c for c in fake_hand.calls if c[0] == "read_conversation"] == []
    rec = run_pass(cfg_dict, fake_hand, T0 + 3601)
    reads = [c for c in fake_hand.calls if c[0] == "read_conversation"]
    assert ("read_conversation", {"channel_id": "D01ABCDEF", "limit": 40, "response_format": "detailed", "oldest": f"{T0:.6f}"}) in reads
    assert ("read_conversation", {"channel_id": "C01GRPDM01", "limit": 1, "response_format": "detailed", "oldest": f"{T0:.6f}"}) in reads
    assert all(c[1]["oldest"] == f"{T0:.6f}" for c in reads) and all("cursor" not in c[1] for c in reads)
    assert [b for b in fake_hand.batches if any(c[1]["channel_id"] == "D01ABCDEF" for c in b)]
    assert con_ro().execute("select edited_at, text from messages where conversation_id='D01ABCDEF' and ts=?", (PARENT_TS,)).fetchone() == (
        T0 + 3601, "Lorem ipsum dolor sit amet, consectetur adipiscing elit.")
    assert rec["messages_updated"] == 1 and cursor("sweep") == f"{T0 + 3601:.6f}"
    probes = [c for c in reads if c[1]["channel_id"] == "C01GRPDM01"]
    assert [c[1]["limit"] for c in probes] == [1, 40]
    fake_hand.calls.clear()
    run_pass(cfg_dict, fake_hand, T0 + 7300)
    probes = [c for c in fake_hand.calls if c[0] == "read_conversation" and c[1]["channel_id"] == "C01GRPDM01"]
    assert [c[1]["limit"] for c in probes] == [40]
    with chat_archive.open_db() as w:
        w.execute("update conversations set last_ts = ? where id = 'C01GRPDM01'", (f"{T0 - 30 * 86400:.6f}",))
    stale = probe.replace("1793106047.590119", f"{T0 - 31 * 86400:.6f}")
    fake_hand.scripts.insert(0, ("read_conversation", {"channel_id": "C01GRPDM01", "limit": 1}, [paged(stale, None)]))
    fake_hand.calls.clear()
    run_pass(cfg_dict, fake_hand, T0 + 7300 + 3700)
    probes = [c for c in fake_hand.calls if c[0] == "read_conversation" and c[1]["channel_id"] == "C01GRPDM01"]
    assert [c[1]["limit"] for c in probes] == [1]


def test_the_registry_is_refreshed_daily_through_every_page(cfg_dict, fake_hand):
    fake_hand.scripts = [s for s in fake_hand.scripts if s[0] != "list_conversations"]
    fake_hand.script("list_conversations", {"cursor": "L2"}, paged(FIXTURE("list.txt").replace("D01ABCDEF", "D01OTHER01"), None))
    fake_hand.script("list_conversations", {}, paged(FIXTURE("list.txt"), "L2"))
    run_pass(cfg_dict, fake_hand, T0)
    assert [c[1].get("cursor") for c in fake_hand.calls if c[0] == "list_conversations"] == [None, "L2"]
    assert con_ro().execute("select count(*) from conversations").fetchone() == (3,)
    fake_hand.calls.clear()
    run_pass(cfg_dict, fake_hand, T0 + 400)
    assert [c for c in fake_hand.calls if c[0] == "list_conversations"] == []
    run_pass(cfg_dict, fake_hand, T0 + 86401)
    assert [c for c in fake_hand.calls if c[0] == "list_conversations"]
    assert cursor("registry") == f"{T0 + 86401:.6f}"


def test_prune_runs_every_full_pass_and_the_ledger_counts_it(cfg_dict, fake_hand):
    run_pass(cfg_dict, fake_hand, T0)
    with chat_archive.open_db() as w:
        w.execute("insert into messages (conversation_id, ts, text) values ('D9', ?, 'ancient')", (f"{T0 - 100 * 86400:.6f}",))
    rec = run_pass(cfg_dict, fake_hand, T0 + 400)
    assert rec["pruned"] == {"messages": 1, "threads": 0, "resolved": 0, "raw_files": 0}
    rec = run_pass(cfg_dict, fake_hand, T0 + 430, wake="trigger")
    assert rec["pruned"] == {"messages": 0, "threads": 0, "resolved": 0, "raw_files": 0}


def test_a_401_that_survives_the_nudge_pages_auth_once_an_hour_and_ends_the_pass_without_writes(cfg_dict, fake_hand):
    fake_hand.fail_all(chat_archive.AuthError("AuthError", tool="search"))
    rec = run_pass(cfg_dict, fake_hand, T0)
    assert outbox_lines("m1") == ["CHAT_ARCHIVE_FAILED auth → AuthError; see dockwright chat-archive status"]
    assert rec["errors"] == ["AuthError"] and len(fake_hand.calls) == 1
    assert con_ro().execute("select count(*) from conversations").fetchone() == (0,)
    run_pass(cfg_dict, fake_hand, T0 + 600)
    assert len(outbox_lines("m1")) == 1
    run_pass(cfg_dict, fake_hand, T0 + 3700)
    assert len(outbox_lines("m1")) == 2
    assert all(json.loads(p.read_text())["source"] == "chat-archive" for p in paths.notify_outbox_dir_for("m1").glob("*.json"))


def test_a_keychain_read_failure_pages_auth_after_three_passes_and_ten_awake_minutes_and_moves_no_cursor(cfg_dict,
                                                                                                         fake_hand, awake):
    fake_hand.fail_all(chat_archive.AuthError("keychain: CalledProcessError"))
    fake_hand.token_key = "none"
    rec = pass_at(cfg_dict, fake_hand, awake, 0)
    assert rec["held"] == {"keychain": 1} and outbox_lines("m1") == []
    assert rec["errors"] == ["keychain: CalledProcessError"] and rec["token_key"] == "none"
    rec = pass_at(cfg_dict, fake_hand, awake, 300)
    assert rec["held"] == {"keychain": 2} and outbox_lines("m1") == []
    pass_at(cfg_dict, fake_hand, awake, 600)
    assert outbox_lines("m1") == ["CHAT_ARCHIVE_FAILED auth → keychain: CalledProcessError; see dockwright chat-archive status"]
    assert cursor("dm") == f"{T0:.6f}"


def test_a_5xx_or_timeout_pages_http_after_three_passes_and_no_op_wakes_neither_count_nor_reset(cfg_dict, fake_hand,
                                                                                               awake):
    assert pass_at(cfg_dict, fake_hand, awake, -400)["errors"] == []
    fake_hand.fail_all(HTTP_503)
    rec = pass_at(cfg_dict, fake_hand, awake, 0)
    assert rec["held"] == {"http": 1} and outbox_lines("m1") == []
    for i in range(1, 5):
        assert run_pass(cfg_dict, fake_hand, T0 + 60 * i, wake="trigger")["calls"] == 0
    assert streaks()["http"]["n"] == 1 and set(chat_archive.status(now=T0 + 300)["last_call_errors"]) == {"HTTPError 503"}
    rec = pass_at(cfg_dict, fake_hand, awake, 300)
    assert rec["held"] == {"http": 2} and outbox_lines("m1") == []
    pass_at(cfg_dict, fake_hand, awake, 600)
    assert outbox_lines("m1") == ["CHAT_ARCHIVE_FAILED http → HTTPError 503; see dockwright chat-archive status"]
    fake_hand.fail_all(chat_archive.HandError("TimeoutError", tool="search"))
    pass_at(cfg_dict, fake_hand, awake, 4300)
    assert outbox_lines("m1")[-1] == "CHAT_ARCHIVE_FAILED http → TimeoutError; see dockwright chat-archive status"
    fake_hand.fail_every = None
    rec = pass_at(cfg_dict, fake_hand, awake, 4600)
    assert rec["errors"] == [] and rec["calls"] > 0 and streaks() == {}
    fake_hand.fail_all(chat_archive.HandError("FileNotFoundError", tool="claude"))
    for at in (4900, 5200):
        pass_at({**cfg_dict, "hand": "claude"}, fake_hand, awake, at)
    assert len(outbox_lines("m1")) == 2
    pass_at({**cfg_dict, "hand": "claude"}, fake_hand, awake, 5500)
    assert outbox_lines("m1")[-1] == "CHAT_ARCHIVE_FAILED hand → FileNotFoundError; see dockwright chat-archive status"
    fake_hand.fail_all(chat_archive.HandError("JSONRPCError", tool="search"))
    rec = pass_at(cfg_dict, fake_hand, awake, 5800)
    assert set(rec["errors"]) == {"JSONRPCError"} and len(rec["errors"]) >= 5
    assert streaks()["http"]["n"] == 1 and rec["held"] == {"http": 1}


def test_a_full_pass_that_runs_to_the_end_clears_every_streak(cfg_dict, fake_hand, awake):
    fake_hand.fail_all(HTTP_503)
    pass_at(cfg_dict, fake_hand, awake, 0)
    pass_at(cfg_dict, fake_hand, awake, 300)
    assert streaks()["http"]["n"] == 2
    failure_streak.save(paths.CHAT / "failures.json", {**streaks(), "hand": {"n": 2, "since": T0, "awake": 0},
                                                       "keychain": {"n": 1, "since": T0, "awake": 0}})
    fake_hand.fail_every = None
    rec = pass_at(cfg_dict, fake_hand, awake, 600)
    assert rec["full"] is True and rec["errors"] == [] and rec["held"] == {} and streaks() == {}


def _mention_search_fails(fake_hand, monkeypatch):
    real = fake_hand.call

    def call(tool, args):
        if tool == "search" and args.get("keywords"):
            fake_hand.calls.append((tool, dict(args)))
            raise chat_archive.HandError("ToolError", tool="search")
        return real(tool, args)

    monkeypatch.setattr(fake_hand, "call", call)


def test_a_trigger_pass_that_succeeds_between_failing_full_passes_keeps_the_streak(cfg_dict, fake_hand, awake,
                                                                                   monkeypatch):
    assert pass_at(cfg_dict, fake_hand, awake, -400)["errors"] == []
    _mention_search_fails(fake_hand, monkeypatch)
    for at in (0, 300, 600):
        assert outbox_lines("m1") == []
        assert pass_at(cfg_dict, fake_hand, awake, at)["full"] is True
        if at < 600:
            write_trigger(f"E_{at}", f"{T0 + at + 100:.6f}", "Ada Example", T0 + at + 100)
            rec = pass_at(cfg_dict, fake_hand, awake, at + 150, wake="trigger")
            assert rec["full"] is False and rec["calls"] > 0 and rec["errors"] == []
            assert streaks()["http"]["n"] == at // 300 + 1
    assert outbox_lines("m1") == ["CHAT_ARCHIVE_FAILED http → ToolError; see dockwright chat-archive status"]


def _trigger_search_fails(fake_hand, monkeypatch):
    real = fake_hand.call

    def call(tool, args):
        if tool == "search" and "before" in args:
            fake_hand.calls.append((tool, dict(args)))
            raise chat_archive.HandError("ToolError", tool="search")
        return real(tool, args)

    monkeypatch.setattr(fake_hand, "call", call)


def test_a_full_pass_that_leaves_a_failing_trigger_untried_keeps_the_streak(cfg_dict, fake_hand, awake, monkeypatch):
    assert pass_at(cfg_dict, fake_hand, awake, -400)["errors"] == []
    _trigger_search_fails(fake_hand, monkeypatch)
    assert pass_at(cfg_dict, fake_hand, awake, 0)["full"] is True
    for at in (0, 300, 600):
        write_trigger(f"E_{at}", f"{T0 + at + 240:.6f}", "Ada Example", T0 + at + 240)
        assert outbox_lines("m1") == []
        rec = pass_at(cfg_dict, fake_hand, awake, at + 250)
        assert rec["full"] is False and rec["errors"] == ["ToolError"] * (at // 300 + 1)
        if at < 600:
            rec = pass_at(cfg_dict, fake_hand, awake, at + 300)
            assert rec["full"] is True and rec["errors"] == [] and streaks()["http"]["n"] == at // 300 + 1
    assert outbox_lines("m1") == ["CHAT_ARCHIVE_FAILED http → ToolError; see dockwright chat-archive status"]


def test_a_not_found_trigger_left_untried_does_not_hold_a_clear(cfg_dict, fake_hand, awake):
    assert pass_at(cfg_dict, fake_hand, awake, -400)["errors"] == []
    fake_hand.fail_all(HTTP_503)
    pass_at(cfg_dict, fake_hand, awake, 0)
    assert streaks()["http"]["n"] == 1
    fake_hand.fail_every = None
    write_trigger(f"E_{DM_TS}", f"{T0 + 240:.6f}", "Ada Example", T0 + 240)
    rec = pass_at(cfg_dict, fake_hand, awake, 250)
    assert rec["full"] is False and rec["errors"] == [] and rec["triggers_done"] == 0
    rec = pass_at(cfg_dict, fake_hand, awake, 300)
    assert rec["full"] is True and rec["errors"] == [] and streaks() == {}


def test_a_pass_cut_short_by_rate_limits_keeps_the_streak(cfg_dict, fake_hand, awake):
    assert pass_at(cfg_dict, fake_hand, awake, -400)["errors"] == []
    limited = chat_archive.RateLimited("HTTPError", status=429, tool="search", retry_after=1.0)
    for at in (0, 300, 600, 900, 1200):
        assert outbox_lines("m1") == []
        fake_hand.fail_all(HTTP_503 if at % 600 == 0 else limited)
        rec = pass_at(cfg_dict, fake_hand, awake, at)
        assert rec["full"] is True and rec["calls"] > 0
    assert outbox_lines("m1") == ["CHAT_ARCHIVE_FAILED http → HTTPError 503; see dockwright chat-archive status"]


def test_a_pass_the_machine_slept_through_does_not_count(cfg_dict, fake_hand, awake, monkeypatch):
    wall = {"t": T0}
    monkeypatch.setattr(failure_streak, "wall_seconds", lambda: wall["t"])
    real = fake_hand.call

    def asleep(tool, args):
        wall["t"] += 30
        return real(tool, args)

    monkeypatch.setattr(fake_hand, "call", asleep)
    fake_hand.fail_all(HTTP_503)
    rec = pass_at(cfg_dict, fake_hand, awake, 0)
    assert rec["slept"] is True and rec["held"] == {"http": 0} and "http" not in streaks()
    assert outbox_lines("m1") == []


def test_an_unwritable_streak_file_pages_at_once(cfg_dict, fake_hand, awake):
    (paths.CHAT / "failures.json").mkdir(parents=True)
    fake_hand.fail_all(HTTP_503)
    rec = pass_at(cfg_dict, fake_hand, awake, 0)
    assert outbox_lines("m1") == ["CHAT_ARCHIVE_FAILED http → HTTPError 503; see dockwright chat-archive status"]
    assert rec.get("held", {}) == {}


def test_a_failure_still_counts_when_the_pass_raises_after_it(cfg_dict, fake_hand, awake):
    fake_hand.fail_all(HTTP_503)
    write_trigger(f"E_{DM_TS}", DM_TS, "Ada Example", T0).chmod(0o400)
    with pytest.raises(PermissionError):
        pass_at(cfg_dict, fake_hand, awake, 0, wake="trigger")
    assert streaks()["http"]["n"] == 1 and outbox_lines("m1") == []
    with pytest.raises(PermissionError):
        pass_at(cfg_dict, fake_hand, awake, 300, wake="trigger")
    assert outbox_lines("m1") == []
    with pytest.raises(PermissionError):
        pass_at(cfg_dict, fake_hand, awake, 600, wake="trigger")
    assert outbox_lines("m1") == ["CHAT_ARCHIVE_FAILED http → HTTPError 503; see dockwright chat-archive status"]


def test_a_streak_entry_without_an_awake_stamp_restarts_the_count(cfg_dict, fake_hand, awake):
    paths.CHAT.mkdir(parents=True, exist_ok=True)
    (paths.CHAT / "failures.json").write_text(json.dumps({"http": {"n": 2}}))
    fake_hand.fail_all(HTTP_503)
    rec = pass_at(cfg_dict, fake_hand, awake, 0)
    assert rec["held"] == {"http": 1} and outbox_lines("m1") == []


def test_errors_ledger_and_pages_carry_no_response_text(cfg_dict, fake_hand):
    fake_hand.script("search", {"filters": f"is:dm after:{DAY_BEFORE(T0)}"}, paged(FIXTURE("search-dm.txt"), None))
    fake_hand.http_error("read_conversation", {"channel_id": "D01ABCDEF"}, 500, "lorem secret body")
    rec = run_pass(cfg_dict, fake_hand, T0)
    text = paths.CHAT_LEDGER.read_text() + "".join(outbox_lines("m1")) + json.dumps(rec)
    assert "secret" not in text and "lorem" not in text and "eiusmod" not in text and rec["errors"] == ["HTTPError 500"]


def test_a_parser_miss_pages_parser_keeps_the_raw_line_and_the_pass_continues(cfg_dict, fake_hand):
    fake_hand.scripts = [s for s in fake_hand.scripts if s[0] != "read_conversation"]
    fake_hand.script("search", {"filters": f"is:dm after:{DAY_BEFORE(T0)}"}, paged(FIXTURE("search-dm.txt"), None))
    fake_hand.script("read_conversation", {"channel_id": "D01ABCDEF"}, paged("Channel: DM (D01ABCDEF)\n\n=== Message from X (U1) at now ===\ngarbage no ts\n", None))
    fake_hand.script("search", {"keywords": ["U01ME00000"]}, paged(FIXTURE("search-context.txt"), None))
    rec = run_pass(cfg_dict, fake_hand, T0)
    assert rec["parser_failed"] == 1 and rec["errors"] == []
    assert outbox_lines("m1") == ["CHAT_ARCHIVE_FAILED parser → read_conversation; see dockwright chat-archive status"]
    assert "garbage" in (paths.CHAT_RAW / f"{DAY}.jsonl").read_text()
    assert [c for c in fake_hand.calls if c[0] == "read_thread"]
    assert chat_archive.status(now=T0 + 10)["parser_failed_24h"] == 1


def test_a_429_sleeps_retry_after_and_three_in_a_row_end_the_pass_with_the_cursors_unmoved(cfg_dict, fake_hand):
    waits = []
    fake_hand.script("search", {"filters": f"is:dm after:{DAY_BEFORE(T0)}"}, paged(FIXTURE("search-dm.txt"), None))
    limited = chat_archive.RateLimited("HTTPError", status=429, tool="read_conversation", retry_after=3.0)
    fake_hand.fail_on("read_conversation", {"channel_id": "D01ABCDEF"}, limited)
    rec = run_pass(cfg_dict, fake_hand, T0, sleep=waits.append)
    assert waits == [3.0, 3.0] and rec["errors"] == ["HTTPError 429"] and cursor("dm") == f"{T0:.6f}"
    assert sum(1 for c in fake_hand.calls if c[0] == "read_conversation") == 3 and outbox_lines("m1") == []
    assert [c for c in fake_hand.calls if c[0] == "search" and "keywords" in c[1]] == []
    assert cursor("threads") == f"{T0:.6f}" and cursor("full") == "0"
    fake_hand.failures.clear()
    fake_hand.calls.clear()
    waits.clear()
    once = FakeHand()
    once.scripts = fake_hand.scripts
    state = {"n": 0}
    real_call = once.call

    def flaky(tool, args):
        if tool == "read_conversation" and state["n"] == 0:
            state["n"] += 1
            once.calls.append((tool, dict(args)))
            raise limited
        return real_call(tool, args)
    once.call = flaky
    once.batch = lambda calls: [flaky(t, a) for t, a in calls]
    rec = run_pass(cfg_dict, once, T0 + 400, sleep=waits.append)
    assert waits == [3.0] and rec["errors"] == [] and cursor("dm") == DM2_TS


def test_the_rate_cap_holds_fifteen_searches_and_forty_five_reads_a_minute(cfg_dict, fake_hand):
    waits = []
    fake_hand.scripts = [s for s in fake_hand.scripts if s[0] != "read_conversation"]
    many = FIXTURE("search-dm.txt")
    for n in range(10):
        fake_hand.script("search", {"cursor": f"s{n}" if n else None},
                         paged(many.replace("D01ABCDEF", f"D{n:08d}"), f"s{n + 1}" if n < 9 else None))
        fake_hand.script("read_conversation", {"channel_id": f"D{n:08d}"}, paged(FIXTURE("conversation.txt").replace("D01ABCDEF", f"D{n:08d}"), None))
    rec = run_pass(cfg_dict, fake_hand, T0, sleep=waits.append)
    assert rec["searches"] == 50 and sum(1 for c in fake_hand.calls if c[0] in ("read_conversation", "read_thread")) >= 50
    assert any(len(b) == 10 for b in fake_hand.batches)
    assert len(waits) >= 2 and all(0 < w <= 60 for w in waits)
    assert chat_archive.SEARCHES_PER_MIN == 15 and chat_archive.READS_PER_MIN == 45 and chat_archive.MAX_PAGES == 10


def test_the_stop_file_and_a_held_lock_skip_the_pass(cfg_dict, fake_hand):
    paths.CHAT_STOP.parent.mkdir(parents=True, exist_ok=True)
    paths.CHAT_STOP.touch()
    assert run_pass(cfg_dict, fake_hand, T0) == {"skipped": "stop-file"} and fake_hand.calls == []
    paths.CHAT_STOP.unlink()
    paths.CHAT.mkdir(parents=True, exist_ok=True)
    with open(paths.CHAT_LOCK, "w") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run_pass(cfg_dict, fake_hand, T0) == {"skipped": "locked"} and fake_hand.calls == []
    assert run_pass(cfg_dict, fake_hand, T0)["kind"] == "pass"


def test_the_first_pass_writes_the_started_mark_and_the_ledger_record_has_the_contract_fields(cfg_dict, fake_hand):
    rec = run_pass(cfg_dict, fake_hand, T0)
    assert paths.CHAT_STARTED.exists() and oct(paths.CHAT.stat().st_mode & 0o777) == "0o700"
    assert set(rec) == {"ts", "kind", "wake", "full", "calls", "searches", "messages_new", "messages_updated",
                        "triggers_done", "triggers_dead", "pages_capped", "parser_failed", "items_failed", "pruned",
                        "seconds", "token_key", "token_account", "auth_retries", "errors", "held", "slept"}
    assert ledger()[-1] == rec and rec["ts"] == T0 and oct(paths.CHAT_DB.stat().st_mode & 0o777) == "0o600"
    assert chat_archive.pass_(cfg_dict, hand=fake_hand, now=T0 + 1, wake="trigger")["calls"] == 0


def test_a_disabled_config_makes_no_call_and_writes_nothing(cfg, home, fake_hand):
    cfg("")
    assert chat_archive.pass_(hand=fake_hand, now=T0) == {"skipped": "disabled"} and not paths.CHAT_DB.exists()


def test_page_renders_to_every_live_manager_under_an_hourly_throttle_per_kind(home):
    assert chat_archive.page("auth", "AuthError", T0, managers=[{"name": "m1"}, {"name": "m2"}]) is True
    assert outbox_lines("m1") == outbox_lines("m2") == ["CHAT_ARCHIVE_FAILED auth → AuthError; see dockwright chat-archive status"]
    assert chat_archive.page("auth", "other", T0 + 10, managers=[{"name": "m1"}]) is False
    assert chat_archive.page("http", "HTTPError 503", T0 + 10, managers=[{"name": "m1"}]) is True
    assert chat_archive.page("auth", "AuthError", T0 + 3600, managers=[{"name": "m1"}]) is True
    assert chat_archive.page("auth", "AuthError", T0 + 3700, managers=[]) is False


# CLI, status, sync, loop, reindex, the installer, the MCP tools


def test_main_read_prints_json_and_status_reports_freshness_and_failures(home, con, seeded, cfg, capsys, monkeypatch):
    cfg(FULL_SECTION)
    con.close()
    monkeypatch.setattr(chat_archive.time, "time", lambda: NOW)
    assert chat_archive.main(["read", "--after", "10d"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [c["id"] for c in out["conversations"]] and out["truncated"] is False
    assert chat_archive.main(["read", "--after", "10d", "--no-bots", "--conversation", "D01ABCDEF", "--limit", "1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [m["sender_id"] for c in out["conversations"] for m in c["messages"]] == ["U01ABCDEF"]
    assert chat_archive.main(["search", "lorem", "--limit", "1"]) == 0
    assert len(json.loads(capsys.readouterr().out)["hits"]) == 1
    assert chat_archive.main(["conversations", "--kind", "dm"]) == 0
    assert [c["kind"] for c in json.loads(capsys.readouterr().out)["conversations"]] == ["dm", "dm"]
    records = [{"ts": NOW - 7200, "kind": "pass", "calls": 3, "errors": ["HTTPError 503"], "token_key": "srv|a",
                "token_account": "b", "auth_retries": 2, "triggers_dead": 1, "parser_failed": 2},
               {"ts": NOW - 100, "kind": "pass", "calls": 0, "errors": [], "token_key": "none", "triggers_dead": 0,
                "auth_retries": 1}]
    paths.CHAT.mkdir(parents=True, exist_ok=True)
    paths.CHAT_LEDGER.write_text("".join(json.dumps(r) + "\n" for r in records))
    paths.CHAT_TRIGGERS.mkdir(exist_ok=True)
    (paths.CHAT_TRIGGERS / "a.json").write_text("{}")
    (paths.CHAT_TRIGGERS / ".wake").write_text("")
    monkeypatch.setattr(chat_archive.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("security ran")))
    assert chat_archive.main(["status"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got == {"enabled": True, "hand": "direct", "runner": "launchd", "ledger_age_sec": 100.0, "messages": 6,
                   "conversations": 4, "cursors": {}, "triggers_pending": 1, "triggers_dead_24h": 1,
                   "parser_failed_24h": 2, "last_call_errors": ["HTTPError 503"], "token_key": "srv|a",
                   "token_account": "b", "auth_retries_24h": 3}
    with pytest.raises(SystemExit) as exit_:
        chat_archive.main(["read"])
    assert exit_.value.code == 2
    with pytest.raises(SystemExit):
        chat_archive.main(["nope"])


def test_main_pass_prints_nothing_and_exits_zero(home, cfg, capsys, monkeypatch):
    cfg(FULL_SECTION)
    seen = {}
    monkeypatch.setattr(chat_archive, "pass_", lambda **kw: seen.update(kw) or {"kind": "pass"})
    assert chat_archive.main(["pass"]) == 0 and capsys.readouterr().out == "" and seen["wake"] == "schedule"
    assert chat_archive.main(["pass", "--wake", "trigger"]) == 0 and seen["wake"] == "trigger"


def test_reindex_runs_under_the_pass_lock_and_rebuilds_the_db_from_raw_files(home, cfg_dict, fake_hand):
    fake_hand.script("search", {"keywords": ["U01ME00000"]}, paged(FIXTURE("search-context.txt"), None))
    fake_hand.script("search", {"filters": f"is:dm after:{DAY_BEFORE(T0)}"}, paged(FIXTURE("search-dm.txt"), None))
    run_pass(cfg_dict, fake_hand, T0)
    before = con_ro().execute("select conversation_id, ts, text from messages order by 1, 2").fetchall()
    assert before
    paths.CHAT_DB.unlink()
    for leftover in paths.CHAT.glob("archive.db-*"):
        leftover.unlink()
    got = chat_archive.reindex()
    assert got["files"] == 1 and got["lines"] >= 4 and got["messages_new"] == len(before) and got["unparsed"] == 0
    assert con_ro().execute("select conversation_id, ts, text from messages order by 1, 2").fetchall() == before
    assert con_ro().execute("select count(*) from conversations where kind = 'group'").fetchone() == (1,)
    assert chat_archive.reindex()["messages_new"] == 0
    with open(paths.CHAT_LOCK, "w") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert chat_archive.reindex() == {"skipped": "locked"}


def test_the_monitors_entry_for_the_manager_runner_is_the_one_non_lane_entry():
    from dockwright import lane_io
    from tests.test_plugin_monitors import _monitors
    monitors = {m["name"]: m for m in _monitors()}
    assert set(monitors) == set(lane_io.LANES) | {"chat-archive"}
    assert monitors["chat-archive"]["command"] == "@@DOCKWRIGHT_BIN@@ chat-archive loop"
    assert monitors["chat-archive"]["description"] == "dockwright chat archive (manager runner)"


class FakeSync:
    def __init__(self):
        self.argv, self.loaded = [], set()

    def __call__(self, cmd):
        self.argv.append(list(cmd))
        if cmd[:2] == ["launchctl", "print"]:
            return 0 if cmd[2] in self.loaded else 1
        if cmd[:2] == ["launchctl", "bootout"]:
            self.loaded.discard(cmd[2])
        if cmd[0] == "bash":
            self.loaded.add(f"gui/501/{cmd[4]}")
        return 0


@pytest.fixture
def fake_run():
    return FakeSync()


def _plist_for(tmp_path, label, bin_, interval=300):
    plist = paths.LAUNCH_AGENTS / f"{label}.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plistlib.dump({"Label": label, "ProgramArguments": [str(bin_), "chat-archive", "pass"], "StartInterval": interval,
                   "WatchPaths": [str(paths.CHAT_TRIGGERS)], "RunAtLoad": True,
                   "EnvironmentVariables": {"PATH": "/usr/bin"}}, plist.open("wb"))
    return plist


def test_sync_installs_one_plist_with_both_triggers_only_when_enabled_and_runner_is_launchd(home, cfg, fake_run, tmp_path):
    cfg(FULL_SECTION)
    bin_, script = tmp_path / "bin" / "dockwright", tmp_path / "chat-archive-install.sh"
    label = f"{config.loop_label_prefix()}.chat-archive"
    assert chat_archive.sync(run=fake_run, uid=501, program=bin_, script=script) == 0
    cmd = fake_run.argv[-1]
    assert cmd[:3] == ["bash", str(script), str(bin_)] and cmd[3] == str(paths.CHAT) and cmd[4] == label
    assert cmd[5] == "300" and cmd[6] == str(paths.CHAT_TRIGGERS) and cmd[7].endswith(".json")
    plist = _plist_for(tmp_path, label, bin_)
    fake_run.argv.clear()
    assert chat_archive.sync(run=fake_run, uid=501, program=bin_, script=script) == 0
    assert all(c[0] != "bash" for c in fake_run.argv)
    _plist_for(tmp_path, label, bin_, interval=60)
    assert chat_archive.sync(run=fake_run, uid=501, program=bin_, script=script) == 0 and fake_run.argv[-1][0] == "bash"
    cfg(FULL_SECTION.replace('runner = "launchd"', 'runner = "manager"'))
    fake_run.argv.clear()
    assert chat_archive.sync(run=fake_run, uid=501, program=bin_, script=script) == 0
    assert not plist.exists() and ["launchctl", "bootout", f"gui/501/{label}"] in fake_run.argv
    cfg(FULL_SECTION.replace("enabled = true", "enabled = false"))
    fake_run.argv.clear()
    assert chat_archive.sync(run=fake_run, uid=501, program=bin_, script=script) == 0 and fake_run.argv == [
        ["launchctl", "print", f"gui/501/{label}"]]
    cfg("")
    assert chat_archive.sync(run=fake_run, uid=501, program=bin_, script=script) == 0


def test_sync_refuses_a_program_under_a_worktree_root(home, cfg, fake_run, tmp_path, monkeypatch, capsys):
    cfg(FULL_SECTION)
    monkeypatch.setattr(config, "worktree_roots", lambda: str(tmp_path / "wt"))
    bin_ = tmp_path / "wt" / "x" / ".venv" / "bin" / "dockwright"
    assert chat_archive.sync(run=fake_run, uid=501, program=bin_, script=tmp_path / "s.sh") == 1
    assert fake_run.argv == [] and "worktree root" in capsys.readouterr().err


def test_sync_reports_a_failed_installer(home, cfg, tmp_path, capsys):
    cfg(FULL_SECTION)
    assert chat_archive.sync(run=lambda cmd: 3 if cmd[0] == "bash" else 1, uid=501, program=tmp_path / "b",
                             script=tmp_path / "s.sh") == 3
    assert "exited 3" in capsys.readouterr().err


def test_loop_exits_zero_at_once_unless_runner_is_manager_and_runs_the_pass_once_with_once(home, cfg, monkeypatch):
    cfg(FULL_SECTION)
    passes = []
    monkeypatch.setattr(chat_archive, "pass_", lambda *a, **kw: passes.append(kw["wake"]) or {})
    monkeypatch.setattr(chat_archive.lane_io, "detach_stdout", lambda: passes.append("detached"))
    assert chat_archive.loop(once=True) == 0 and passes == []
    cfg(FULL_SECTION.replace('runner = "launchd"', 'runner = "manager"'))
    clock = {"t": T0}
    assert chat_archive.loop(once=True, sleep=lambda s: None, now=lambda: clock["t"]) == 0
    assert passes == ["detached", "schedule"]
    passes.clear()
    paths.CHAT_TRIGGERS.mkdir(parents=True, exist_ok=True)
    (paths.CHAT_TRIGGERS / "x.json").write_text("{}")
    slept = []
    assert chat_archive.loop(once=True, sleep=slept.append, now=lambda: clock["t"]) == 0
    assert passes == ["detached", "trigger", "schedule"] and slept == []


def test_the_installer_script_renders_a_plist_with_start_interval_and_watch_paths(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "launchctl").write_text("#!/bin/sh\necho \"$@\" >> \"$STUB_LOG\"\n")
    (stub / "launchctl").chmod(0o755)
    bin_ = tmp_path / "dockwright"
    bin_.write_text("#!/bin/sh\n")
    bin_.chmod(0o755)
    env_file = tmp_path / "env.json"
    env_file.write_text(json.dumps({"CLAUDE_ORCH_AUTONUDGE": "1"}))
    chat_dir, triggers = home / ".claude" / "dockwright" / "chat", home / ".claude" / "dockwright" / "chat" / "triggers"
    script = Path(__file__).resolve().parent.parent / "deploy" / "scripts" / "chat-archive-install.sh"
    done = subprocess.run(["bash", str(script), str(bin_), str(chat_dir), "x.chat-archive", "300", str(triggers), str(env_file)],
                          env={**os.environ, "HOME": str(home), "PATH": f"{stub}:{os.environ['PATH']}", "STUB_LOG": str(tmp_path / "log")},
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    data = plistlib.load(open(home / "Library" / "LaunchAgents" / "x.chat-archive.plist", "rb"))
    assert data["ProgramArguments"] == [str(bin_), "chat-archive", "pass"] and data["StartInterval"] == 300
    assert data["WatchPaths"] == [str(triggers)] and data["RunAtLoad"] is True and data["Label"] == "x.chat-archive"
    assert list(data["EnvironmentVariables"])[0] == "PATH" and ".local/bin" in data["EnvironmentVariables"]["PATH"]
    assert data["EnvironmentVariables"]["CLAUDE_ORCH_AUTONUDGE"] == "1"
    assert data["StandardOutPath"] == str(chat_dir / "launchd-out.log") and data["StandardErrorPath"] == str(chat_dir / "launchd-err.log")
    assert chat_dir.is_dir() and "bootstrap gui/" in (tmp_path / "log").read_text()


def test_chat_read_search_and_conversations_are_thin_over_the_module_and_carry_the_privacy_sentence(fresh_orchestrator_dir, monkeypatch):
    from dockwright import mcp_server as ms
    seen = {}
    monkeypatch.setattr(chat_archive, "read", lambda *a, **k: seen.update(read=(a, k)) or {"r": 1})
    monkeypatch.setattr(chat_archive, "search", lambda *a, **k: seen.update(search=(a, k)) or {"s": 1})
    monkeypatch.setattr(chat_archive, "conversations", lambda *a, **k: seen.update(conv=(a, k)) or {"c": 1})
    assert ms.chat_read("7d", before="1d", conversation="D1", thread="2.0", bots=False, limit=9) == {"r": 1}
    assert seen["read"] == (("7d",), {"before": "1d", "conversation": "D1", "thread": "2.0", "bots": False, "limit": 9})
    assert ms.chat_search("x", after="7d", sender="U1", limit=3) == {"s": 1}
    assert seen["search"] == (("x",), {"after": "7d", "before": None, "conversation": None, "sender": "U1", "limit": 3})
    assert ms.chat_conversations(kind="dm") == {"c": 1} and seen["conv"] == ((), {"kind": "dm", "since": None})
    sentence = "Text from the archive never goes into a PR, a tracker comment, a post or any artifact another person reads."
    for tool in (ms.chat_read, ms.chat_search, ms.chat_conversations):
        assert sentence in tool.__doc__
    assert "scout" not in json.dumps(ms.event_subscribe.__annotations__["source"].__metadata__[0].description)


def test_the_dispatcher_routes_chat_archive_to_the_module_main(monkeypatch):
    import dockwright.__main__ as entry
    seen = {}
    monkeypatch.setattr(chat_archive, "main", lambda argv: seen.update(argv=argv) or 7)
    monkeypatch.setattr(sys, "argv", ["dockwright", "chat-archive", "status"])
    with pytest.raises(SystemExit) as exit_:
        entry.main()
    assert exit_.value.code == 7 and seen["argv"] == ["status"]


# round 1 findings


def test_the_window_search_response_never_reaches_raw_and_reindex_adds_no_neighbour(cfg_dict, fake_hand):
    run_pass(cfg_dict, fake_hand, T0 - 100)
    fake_hand.script("search", {"sort": "timestamp"}, paged(FIXTURE("search-window.txt"), None))
    write_trigger(f"E_{REPLY_TS}", REPLY_TS, "#some-channel", T0 + 60, kind="channel")
    run_pass(cfg_dict, fake_hand, T0 + 90, wake="trigger")
    raw = "".join(p.read_text() for p in paths.CHAT_RAW.iterdir())
    assert "a top-level message in the same second" not in raw and "the first reply" in raw
    before = con_ro().execute("select count(*) from messages").fetchone()
    assert before == (3,) and chat_archive.reindex()["messages_new"] == 0
    assert con_ro().execute("select count(*) from messages").fetchone() == before


def test_a_dm_trigger_keeps_the_conversation_read_in_raw_and_not_the_window_search(cfg_dict, fake_hand):
    run_pass(cfg_dict, fake_hand, T0 - 100)
    fake_hand.script("search", {"before": f"{float(DM_TS) + 1.5:.6f}"}, paged(FIXTURE("search-dm.txt"), None))
    write_trigger(f"E_{DM_TS}", DM_TS, "Ada Example", T0 + 100)
    rec = run_pass(cfg_dict, fake_hand, T0 + 150, wake="trigger")
    raw = "".join(p.read_text() for p in paths.CHAT_RAW.iterdir())
    assert rec["triggers_done"] == 1 and "Lorem ipsum from a bot" not in raw and "Thread: 2 replies" in raw


def test_a_trigger_whose_ts_is_not_a_number_is_counted_dead_and_the_pass_goes_on(cfg_dict, fake_hand):
    run_pass(cfg_dict, fake_hand, T0 - 100)
    bad = write_trigger("E_", "", "Ada Example", T0)
    rec = run_pass(cfg_dict, fake_hand, T0 + 10, wake="trigger")
    assert rec["triggers_dead"] == 1 and not bad.exists() and rec["errors"] == []
    other = write_trigger("E_x", "1.5.3", "Ada Example", T0)
    assert run_pass(cfg_dict, fake_hand, T0 + 20, wake="trigger")["triggers_dead"] == 1 and not other.exists()


def test_a_cursor_search_pages_oldest_first_and_the_cap_leaves_the_cursor_at_the_last_read_hit(cfg_dict, fake_hand):
    fake_hand.scripts = [s for s in fake_hand.scripts if s[0] != "read_conversation"]
    many = FIXTURE("search-dm.txt")
    for n in range(12):
        text = many.replace("D01ABCDEF", f"D{n:08d}").replace("1793107259.250289", f"1793107259.{n:06d}")
        fake_hand.script("search", {"cursor": f"s{n}" if n else None, "filters": f"is:dm after:{DAY_BEFORE(T0)}"},
                         paged(text, f"s{n + 1}" if n < 11 else None))
        fake_hand.script("read_conversation", {"channel_id": f"D{n:08d}"}, paged(FIXTURE("conversation.txt").replace("D01ABCDEF", f"D{n:08d}"), None))
    rec = run_pass(cfg_dict, fake_hand, T0)
    dm = [c for c in fake_hand.calls if c[0] == "search" and "is:dm" in c[1]["filters"]]
    assert len(dm) == 10 and all(c[1]["sort_dir"] == "asc" for c in dm)
    assert cursor("dm") == "1793107259.000009" and rec["pages_capped"] == 1
    assert con_ro().execute("select count(*) from conversations where id in ('D00000010', 'D00000011')").fetchone() == (0,)
    assert all(c[1]["sort_dir"] == "asc" for c in fake_hand.calls if c[0] == "search" and "keywords" in c[1])


def test_the_trigger_window_search_keeps_newest_first(cfg_dict, fake_hand):
    run_pass(cfg_dict, fake_hand, T0 - 100)
    write_trigger(f"E_{DM_TS}", DM_TS, "Ada Example", T0 + 60)
    run_pass(cfg_dict, fake_hand, T0 + 90, wake="trigger")
    window = [c for c in fake_hand.calls if c[0] == "search" and "before" in c[1]]
    assert window and "sort_dir" not in window[0][1]


def test_every_failing_class_counts_toward_the_http_streak(cfg_dict, fake_hand, awake):
    for at, err in ((0, chat_archive.HandError("JSONRPCError", tool="search")),
                    (300, chat_archive.HandError("HTTPError", status=403, tool="search")),
                    (600, chat_archive.HandError("ToolError", tool="search"))):
        assert outbox_lines("m1") == []
        fake_hand.fail_all(err)
        pass_at(cfg_dict, fake_hand, awake, at)
    assert outbox_lines("m1") == ["CHAT_ARCHIVE_FAILED http → ToolError; see dockwright chat-archive status"]


def test_the_claude_hand_batches_several_thread_and_conversation_reads(cfg, fake, model_pins):
    cfg(FULL_SECTION)
    hand = chat_archive.ClaudeHand(config.chat_archive(), run=fake.run)
    threads = [("read_thread", {"channel_id": "C1", "message_ts": f"{n}.0", "limit": 1000}) for n in range(3)]
    fake.stream_json([("mcp__srv__read_thread_tool", a, f"t{n}") for n, (_, a) in enumerate(threads)])
    assert hand.batch(threads) == ["t0", "t1", "t2"] and len(fake.run.argv) == 1
    pages = [("read_conversation", {"channel_id": f"D{n}", "limit": 40}) for n in range(3)]
    fake.stream_json([("mcp__srv__read_conversation_tool", a, f"p{n}") for n, (_, a) in enumerate(pages)])
    assert hand.batch(pages) == ["p0", "p1", "p2"] and len(fake.run.argv) == 2
    assert chat_archive._expected_bytes("read_thread", {"limit": 1000}) == chat_archive.THREAD_BYTES
    assert chat_archive._expected_bytes("read_conversation", {"limit": 1}) == chat_archive.READ_BYTES


def test_parse_takes_a_label_line_inside_a_body_as_body(cfg):
    text = ("Channel: DM (D1)\n\n=== Message from Ada Example <ada@example.test> (U01ABCDEF) at 2026-10-27 13:00:47 +00 === \n"
            "Message TS: 1793106047.590119\nForwarding this:\nFrom: Carol Other <carol@example.test>\nTime: tomorrow 10am works?\n"
            "=== Agenda ===\nItem one\n--- break ---\nReactions: eyes (1)\n")
    got = chat_archive.parse("conversation", text, conversation_id="D1")
    assert got["unparsed"] == 0 and len(got["messages"]) == 1
    m = got["messages"][0]
    assert (m["sender"], m["sender_id"]) == ("Ada Example", "U01ABCDEF")
    assert m["text"] == "Forwarding this:\nFrom: Carol Other <carol@example.test>\nTime: tomorrow 10am works?\n=== Agenda ===\nItem one\n--- break ---"
    search = FIXTURE("search-dm.txt").replace("Consectetur adipiscing elit, sed do eiusmod.", "first part\n---\nsecond part\nFrom: nobody")
    hits = chat_archive.parse("search", search)["messages"]
    assert hits[1]["text"] == "first part\n---\nsecond part\nFrom: nobody" and hits[1]["sender"] == "Ada Example"
    thread = FIXTURE("thread.txt").replace("Consectetur adipiscing elit, the first reply.", "reply says\nMessage TS: 9.9\n--- Reply ---\nnot a head")
    replies = chat_archive.parse("thread", thread, conversation_id="C1")["messages"]
    assert len(replies) == 3 and replies[1]["ts"] == "1793106855.095329"
    assert replies[1]["text"] == "reply says\nMessage TS: 9.9\n--- Reply ---\nnot a head"
    garbage = chat_archive.parse("conversation", "Channel: DM (D1)\n\n=== Message from X (U1) at now ===\nno ts here\n", conversation_id="D1")
    assert garbage["unparsed"] == 1


def test_claude_runs_drop_the_config_dir_and_the_nudge_is_throttled_for_an_hour(hand, fake, model_pins, monkeypatch, home):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/Users/x/.claude-b")
    assert "CLAUDE_CONFIG_DIR" not in chat_archive._claude_env()
    fake.keychain(TOK1)
    fake.http(401)
    fake.keychain(TOK1)
    fake.keychain(TOK2)
    fake.http(401)
    fake.keychain(TOK2)
    with pytest.raises(chat_archive.AuthError, match="^AuthError$"):
        hand.call("search", {})
    assert [a[0] for a in fake.run.argv].count("/bin/claude") == 1 and "CLAUDE_CONFIG_DIR" not in fake.run.kwargs[2]["env"]
    assert hand.auth_retries == 1 and not fake.responses
    again = chat_archive.DirectHand(config.chat_archive(), run=fake.run, urlopen=fake.urlopen, now=lambda: NOW + 600)
    fake.keychain(TOK1)
    fake.http(401)
    fake.keychain(TOK1)
    with pytest.raises(chat_archive.AuthError, match="^AuthError$"):
        again.call("search", {})
    assert [a[0] for a in fake.run.argv].count("/bin/claude") == 1
    later = chat_archive.DirectHand(config.chat_archive(), run=fake.run, urlopen=fake.urlopen, now=lambda: NOW + 3700)
    fake.keychain(TOK1)
    fake.http(401)
    fake.keychain(TOK1)
    fake.keychain(TOK1)
    with pytest.raises(chat_archive.AuthError, match="^AuthError$"):
        later.call("search", {})
    assert [a[0] for a in fake.run.argv].count("/bin/claude") == 2


def test_a_truncated_read_keeps_the_newest_messages(home, con, seeded):
    got = chat_archive.read("10d", limit=2, con=con, now=NOW)
    assert got["truncated"] is True
    assert [m["ts"] for c in got["conversations"] for m in c["messages"]] == [SEEDED_PARENT_TS, SEEDED_CHANNEL_TS]


def test_write_tools_is_required_and_self_user_id_is_not(cfg):
    got = config.chat_archive()
    assert "self_user_id is empty" not in got["errors"] and got["errors"][-1] == "write_tools is empty"
    cfg(FULL_SECTION.replace('self_user_id = "U01ME00000"\n', ""))
    assert config.chat_archive()["enabled"] is True
    cfg(FULL_SECTION.replace('write_tools = ["write_a", "write_b"]\n', ""))
    assert config.chat_archive()["enabled"] is False and config.chat_archive()["errors"] == ["write_tools is empty"]


def test_the_ledger_is_trimmed_past_its_cap(cfg_dict, fake_hand):
    paths.CHAT.mkdir(parents=True, exist_ok=True)
    paths.CHAT_LEDGER.write_text("".join(json.dumps({"ts": T0 - i, "kind": "pass"}) + "\n" for i in range(chat_archive.LEDGER_KEEP + 50)))
    run_pass(cfg_dict, fake_hand, T0)
    lines = paths.CHAT_LEDGER.read_text().splitlines()
    assert len(lines) == chat_archive.LEDGER_KEEP and json.loads(lines[-1])["ts"] == T0


def test_loop_survives_a_crashing_pass(home, cfg, monkeypatch, capsys):
    cfg(FULL_SECTION.replace('runner = "launchd"', 'runner = "manager"'))
    monkeypatch.setattr(chat_archive.lane_io, "detach_stdout", lambda: None)
    monkeypatch.setattr(chat_archive, "pass_", lambda *a, **kw: (_ for _ in ()).throw(ValueError("secret detail")))
    assert chat_archive.loop(once=True, sleep=lambda s: None, now=lambda: T0) == 0
    err = capsys.readouterr().err
    assert "ValueError" in err and "secret" not in err


# round 2 findings


def test_one_item_that_fails_every_pass_does_not_wedge_its_step(cfg_dict, fake_hand):
    fake_hand.script("search", {"keywords": ["U01ME00000"]}, paged(FIXTURE("search-context.txt"), None))
    run_pass(cfg_dict, fake_hand, T0)
    with chat_archive.open_db() as w:
        w.execute("insert into threads (conversation_id, thread_ts, last_reply_ts, checked_at) values ('C01ABCDEF', ?, ?, 0)", (TOP_TS, TOP_TS))
        w.execute("insert into messages (conversation_id, ts, thread_ts, text) values ('C01ABCDEF', ?, ?, 'top')", (TOP_TS, TOP_TS))
        w.execute("update threads set last_reply_ts = ? where thread_ts = ?", (f"{T0 - 100:.6f}", PARENT_TS))
    fake_hand.scripts = [s for s in fake_hand.scripts if s[0] != "search"]
    fake_hand.fail_on("read_thread", {"message_ts": TOP_TS}, chat_archive.HandError("ToolError", tool="read_thread"))
    fresh = FIXTURE("thread.txt").replace("1793107259.250289", "1793108000.000001")
    fake_hand.scripts.insert(0, ("read_thread", {"message_ts": PARENT_TS}, [paged(fresh, None)]))
    rec = run_pass(cfg_dict, fake_hand, T0 + 400, managers=[{"name": "m1"}])
    assert rec["errors"] == ["ToolError"] and rec["items_failed"] == 1 and cursor("threads") == f"{T0 + 400:.6f}"
    assert con_ro().execute("select last_reply_ts, next_try, backoff from threads where thread_ts = ?", (PARENT_TS,)).fetchone() == ("1793108000.000001", 0, 0)
    assert con_ro().execute("select next_try, backoff from threads where thread_ts = ?", (TOP_TS,)).fetchone() == (T0 + 400 + 600, 600)
    assert outbox_lines("m1") == []
    fake_hand.calls.clear()
    rec = run_pass(cfg_dict, fake_hand, T0 + 800, managers=[{"name": "m1"}])
    assert [c for c in fake_hand.calls if c[0] == "read_thread" and c[1]["message_ts"] == TOP_TS] == []
    assert rec["errors"] == [] and rec["items_failed"] == 0
    run_pass(cfg_dict, fake_hand, T0 + 1100, managers=[{"name": "m1"}])
    assert con_ro().execute("select next_try, backoff from threads where thread_ts = ?", (TOP_TS,)).fetchone() == (T0 + 1100 + 1200, 1200)
    with chat_archive.open_db() as w:
        w.execute("update threads set backoff = 86400, next_try = ? where thread_ts = ?", (T0 + 1200, TOP_TS))
    run_pass(cfg_dict, fake_hand, T0 + 1500, managers=[{"name": "m1"}])
    assert con_ro().execute("select next_try, backoff from threads where thread_ts = ?", (TOP_TS,)).fetchone() == (T0 + 1500 + 86400, 86400)
    assert chat_archive.BACKOFF_MIN_SEC == 600 and chat_archive.BACKOFF_MAX_SEC == 86400
    fake_hand.failures.clear()
    with chat_archive.open_db() as w:
        w.execute("update threads set next_try = 0 where thread_ts = ?", (TOP_TS,))
    fake_hand.scripts.insert(0, ("read_thread", {"message_ts": TOP_TS}, [paged(FIXTURE("thread.txt").replace(PARENT_TS, TOP_TS), None)]))
    run_pass(cfg_dict, fake_hand, T0 + 2000, managers=[{"name": "m1"}])
    assert con_ro().execute("select next_try, backoff from threads where thread_ts = ?", (TOP_TS,)).fetchone() == (0, 0)
    fake_hand.fail_on("read_thread", {"message_ts": PARENT_TS}, chat_archive.HandError("HTTPError", status=503, tool="read_thread"))
    rec = run_pass(cfg_dict, fake_hand, T0 + 2400, managers=[{"name": "m1"}])
    assert rec["errors"] == ["HTTPError 503"] and rec["items_failed"] == 0 and cursor("threads") == f"{T0 + 2000:.6f}"


def test_a_batch_in_which_every_item_fails_is_a_step_failure_and_nothing_is_marked(cfg_dict, fake_hand):
    fake_hand.script("search", {"keywords": ["U01ME00000"]}, paged(FIXTURE("search-window.txt"), None))
    run_pass(cfg_dict, fake_hand, T0)
    assert con_ro().execute("select count(*) from threads").fetchone() == (2,)
    fake_hand.scripts = [s for s in fake_hand.scripts if s[0] != "search"]
    fake_hand.fail_all(chat_archive.HandError("ValueError", tool="read_thread"))
    for i in range(1, 4):
        rec = run_pass(cfg_dict, fake_hand, T0 + 400 * i, managers=[{"name": "m1"}])
        assert rec["items_failed"] == 0 and "ValueError" in rec["errors"]
    assert con_ro().execute("select max(next_try) from threads").fetchone() == (0,) and cursor("threads") == f"{T0:.6f}"
    fake_hand.fail_every = None
    fake_hand.calls.clear()
    run_pass(cfg_dict, fake_hand, T0 + 2000)
    assert sorted(c[1]["message_ts"] for c in fake_hand.calls if c[0] == "read_thread") == sorted([PARENT_TS, TOP_TS])


def test_a_lone_item_that_fails_gets_a_backoff_rather_than_a_step_failure(cfg_dict, fake_hand):
    fake_hand.script("search", {"keywords": ["U01ME00000"]}, paged(FIXTURE("search-context.txt"), None))
    run_pass(cfg_dict, fake_hand, T0)
    fake_hand.scripts = [s for s in fake_hand.scripts if s[0] != "search"]
    fake_hand.fail_on("read_thread", {"message_ts": PARENT_TS}, chat_archive.HandError("HTTPError", status=400, tool="read_thread"))
    rec = run_pass(cfg_dict, fake_hand, T0 + 400)
    assert rec["items_failed"] == 1 and cursor("threads") == f"{T0 + 400:.6f}"
    assert con_ro().execute("select next_try from threads").fetchone() == (T0 + 400 + 600,)


def test_one_unreadable_conversation_does_not_stop_the_dm_step_or_the_sweep(cfg_dict, fake_hand):
    two = FIXTURE("search-dm.txt").replace("### Result 2 of 2\nChannel: DM (ID: D01ABCDEF)", "### Result 2 of 2\nChannel: DM (ID: D02BADBAD)")
    fake_hand.script("search", {"filters": f"is:dm after:{DAY_BEFORE(T0)}"}, paged(two, None))
    fake_hand.fail_on("read_conversation", {"channel_id": "D02BADBAD"}, chat_archive.HandError("HTTPError", status=404, tool="read_conversation"))
    rec = run_pass(cfg_dict, fake_hand, T0)
    assert rec["errors"] == ["HTTPError 404"] and rec["items_failed"] == 1 and cursor("dm") == DM2_TS
    assert con_ro().execute("select count(*) from messages where conversation_id = 'D01ABCDEF'").fetchone() == (4,)
    assert con_ro().execute("select next_try, backoff from conversations where id = 'D02BADBAD'").fetchone() == (T0 + 600, 600)
    with chat_archive.open_db() as w:
        w.execute("update conversations set kind = 'group', last_ts = ? where id = 'D02BADBAD'", (f"{T0 - 30 * 86400:.6f}",))
        w.execute("update conversations set next_try = ? where id = 'D02BADBAD'", (T0 + 7200,))
    fake_hand.calls.clear()
    rec = run_pass(cfg_dict, fake_hand, T0 + 3700)
    assert [c for c in fake_hand.calls if c[0] == "read_conversation" and c[1]["channel_id"] == "D02BADBAD"] == []
    assert cursor("sweep") == f"{T0 + 3700:.6f}" and rec["items_failed"] == 0
    fake_hand.failures.clear()
    fake_hand.script("read_conversation", {"channel_id": "D02BADBAD"}, paged(FIXTURE("conversation.txt").replace("D01ABCDEF", "D02BADBAD"), None))
    run_pass(cfg_dict, fake_hand, T0 + 7300)
    assert con_ro().execute("select next_try, backoff from conversations where id = 'D02BADBAD'").fetchone() == (0, 0)


def test_the_sweeps_full_read_after_a_probe_fails_as_an_item(cfg_dict, fake_hand):
    probe = FIXTURE("conversation.txt").replace("D01ABCDEF", "C01GRPDM01").split("=== Message from Bob")[0]
    fake_hand.script("read_conversation", {"channel_id": "C01GRPDM01", "limit": 1}, paged(probe, None))
    fake_hand.fail_on("read_conversation", {"channel_id": "C01GRPDM01", "limit": 40}, chat_archive.HandError("ToolError", tool="read_conversation"))
    run_pass(cfg_dict, fake_hand, T0)
    with chat_archive.open_db() as w:
        w.execute("update conversations set last_ts = ? where id = 'C01GRPDM01'", (f"{T0 - 30 * 86400:.6f}",))
    rec = run_pass(cfg_dict, fake_hand, T0 + 3700)
    assert rec["items_failed"] == 1 and rec["errors"] == ["ToolError"] and cursor("sweep") == f"{T0 + 3700:.6f}"
    assert con_ro().execute("select next_try from conversations where id = 'C01GRPDM01'").fetchone() == (T0 + 3700 + 600,)


def test_a_step_failure_pages_by_its_kind(cfg_dict, fake_hand, awake):
    fake_hand.script("search", {"keywords": ["U01ME00000"]}, paged(FIXTURE("search-window.txt"), None))
    run_pass(cfg_dict, fake_hand, T0)
    fake_hand.scripts = [s for s in fake_hand.scripts if s[0] != "search"]
    fake_hand.fail_all(chat_archive.HandError("hand: call 1 oversized", tool="claude"))
    for at in (0, 300, 600):
        assert outbox_lines("m1") == []
        awake["t"] = float(at)
        run_pass({**cfg_dict, "hand": "claude"}, fake_hand, T0 + 400 + at, managers=[{"name": "m1"}])
    assert outbox_lines("m1") == ["CHAT_ARCHIVE_FAILED hand → hand: call 1 oversized; see dockwright chat-archive status"]


def test_a_later_page_that_fails_as_an_item_keeps_what_was_read(cfg_dict, fake_hand):
    fake_hand.scripts = [s for s in fake_hand.scripts if s[0] != "read_conversation"]
    fake_hand.script("search", {"filters": f"is:dm after:{DAY_BEFORE(T0)}"}, paged(FIXTURE("search-dm.txt"), None))
    fake_hand.script("read_conversation", {"channel_id": "D01ABCDEF", "cursor": None}, paged(FIXTURE("conversation.txt"), "p2"))
    fake_hand.fail_on("read_conversation", {"cursor": "p2"}, chat_archive.HandError("ToolError", tool="read_conversation"))
    rec = run_pass(cfg_dict, fake_hand, T0)
    assert rec["items_failed"] == 1 and con_ro().execute("select count(*) from messages where conversation_id = 'D01ABCDEF'").fetchone() == (5,)
    assert cursor("dm") == DM2_TS
    assert con_ro().execute("select next_try, backoff from conversations where id = 'D01ABCDEF'").fetchone() == (T0 + 600, 600)
    run_pass(cfg_dict, fake_hand, T0 + 400)
    assert con_ro().execute("select next_try, backoff from conversations where id = 'D01ABCDEF'").fetchone() == (T0 + 600, 600)
    run_pass(cfg_dict, fake_hand, T0 + 1000)
    assert con_ro().execute("select next_try, backoff from conversations where id = 'D01ABCDEF'").fetchone() == (T0 + 1000 + 1200, 1200)
    assert con_ro().execute("select gap_ts, gap_cursor from conversations where id = 'D01ABCDEF'").fetchone() == ("1789987596.964679", None)
    fake_hand.failures.clear()
    fake_hand.script("read_conversation", {"channel_id": "D01ABCDEF", "cursor": "p2"}, paged(FIXTURE("conversation.txt"), None))
    run_pass(cfg_dict, fake_hand, T0 + 2300)
    assert con_ro().execute("select next_try, backoff, gap_cursor from conversations where id = 'D01ABCDEF'").fetchone() == (0, 0, None)


def test_direct_hand_batch_returns_an_item_failure_and_raises_a_transport_failure(hand, fake):
    fake.keychain(TOK1)
    fake.session()
    fake.http(200, OK)
    fake.http(404, None)
    fake.http(200, {"result": {"content": [{"type": "text", "text": "boom"}], "isError": True}})
    got = hand.batch([("search", {}), ("read_thread", {}), ("read_conversation", {})])
    assert got[0] == "{\"messages\": \"A\"}" and isinstance(got[1], chat_archive.HandError) and str(got[1]) == "HTTPError 404"
    assert isinstance(got[2], chat_archive.HandError) and str(got[2]) == "ToolError"
    fake.http(503, None)
    with pytest.raises(chat_archive.HandError, match="^HTTPError 503$"):
        hand.batch([("search", {})])
    assert chat_archive.item_failure(chat_archive.HandError("hand: call 1 oversized", tool="claude")) is True
    assert chat_archive.item_failure(chat_archive.HandError("hand: call 1 missing", tool="claude")) is False
    assert chat_archive.item_failure(chat_archive.HandError("TimeoutError", tool="search")) is False
    assert chat_archive.item_failure(chat_archive.AuthError("AuthError")) is False


def test_claude_hand_batch_returns_an_oversized_item_instead_of_failing_the_batch(cfg, fake, model_pins):
    cfg(FULL_SECTION)
    hand = chat_archive.ClaudeHand(config.chat_archive(), run=fake.run)
    fake.stream_json([("mcp__srv__read_thread_tool", {"channel_id": "C1", "message_ts": "1.0", "limit": 1000}, "Error: result (90000 characters)"),
                      ("mcp__srv__read_thread_tool", {"channel_id": "C1", "message_ts": "2.0", "limit": 1000}, "fine")])
    fake.stream_json([("mcp__srv__read_thread_tool", {"channel_id": "C1", "message_ts": "1.0", "limit": 500}, "Error: result (80000 characters)")])
    got = hand.batch([("read_thread", {"channel_id": "C1", "message_ts": "1.0", "limit": 1000}),
                      ("read_thread", {"channel_id": "C1", "message_ts": "2.0", "limit": 1000})])
    assert isinstance(got[0], chat_archive.HandError) and str(got[0]) == "hand: call 1 oversized" and got[1] == "fine"


def test_parse_keeps_context_and_pagination_looking_body_lines_outside_their_layouts():
    text = ("Channel: DM (D1)\n\n=== Message from Ada Example (U01ABCDEF) at 2026-10-27 13:00:47 +00 === \n"
            "Message TS: 1793106047.590119\nline one\nContext before: we agreed on X\nline two\n"
            "There are more messages in `general` today\nline three\n")
    got = chat_archive.parse("conversation", text, conversation_id="D1")
    assert got["messages"][0]["text"] == "line one\nContext before: we agreed on X\nline two\nThere are more messages in `general` today\nline three"
    assert got["cursor"] is None
    strict = text + "\nThere are more messages available. To view the next page, use cursor: `abc`\n"
    got = chat_archive.parse("conversation", strict, conversation_id="D1")
    assert got["cursor"] == "abc" and got["messages"][0]["text"].endswith("line three")
    assert chat_archive.parse("conversation", strict, conversation_id="D1", pagination_info="last page")["cursor"] is None
    search = FIXTURE("search-dm.txt").replace("Consectetur adipiscing elit, sed do eiusmod.", "Context before: in the body\nstill body")
    hits = chat_archive.parse("search", search)["messages"]
    assert hits[1]["text"] == "" or hits[1]["text"] == "Context before: in the body\nstill body"
    assert chat_archive.parse("search", FIXTURE("search-context.txt"))["messages"][0]["text"] == "Lorem ipsum top-level <@S01GRP0001> dolor sit amet."


# round 4 minors


def test_a_backed_off_thread_is_due_at_its_next_try_whatever_its_age(cfg_dict, fake_hand):
    old = f"{T0 - 10 * 86400:.6f}"
    hit = FIXTURE("search-context.txt").replace(PARENT_TS, old)
    fake_hand.script("search", {"keywords": ["U01ME00000"]}, paged(hit, None))
    fake_hand.fail_on("read_thread", {"message_ts": old}, chat_archive.HandError("ToolError", tool="read_thread"))
    rec = run_pass(cfg_dict, fake_hand, T0)
    assert rec["items_failed"] == 1
    assert con_ro().execute("select next_try, backoff from threads where thread_ts = ?", (old,)).fetchone() == (T0 + 600, 600)
    fake_hand.scripts = [s for s in fake_hand.scripts if s[0] != "search"]
    fake_hand.failures.clear()
    fake_hand.script("read_thread", {"message_ts": old}, paged(FIXTURE("thread.txt").replace(PARENT_TS, old), None))
    fake_hand.calls.clear()
    run_pass(cfg_dict, fake_hand, T0 + 700)
    assert [c[1]["message_ts"] for c in fake_hand.calls if c[0] == "read_thread"] == [old]
    assert con_ro().execute("select next_try, backoff from threads where thread_ts = ?", (old,)).fetchone() == (0, 0)


def test_an_item_failure_does_not_page_but_the_ledger_and_status_carry_it(cfg_dict, fake_hand):
    fake_hand.script("search", {"keywords": ["U01ME00000"]}, paged(FIXTURE("search-window.txt"), None))
    run_pass(cfg_dict, fake_hand, T0)
    fake_hand.scripts = [s for s in fake_hand.scripts if s[0] != "search"]
    fake_hand.fail_on("read_thread", {"message_ts": TOP_TS}, chat_archive.HandError("ToolError", tool="read_thread"))
    rec = run_pass(cfg_dict, fake_hand, T0 + 400, managers=[{"name": "m1"}])
    assert rec["items_failed"] == 1 and rec["errors"] == ["ToolError"] and outbox_lines("m1") == []
    assert chat_archive.status(now=T0 + 410)["last_call_errors"] == ["ToolError"]


def _page_text(cid, label, tss):
    out = [f"Channel: {label} ({cid})", ""]
    for ts in tss:
        out += ["=== Message from Ada Example <ada@example.test> (U01ABCDEF) at 2026-10-27 13:00:47 +00 ===",
                f"Message TS: {ts}", f"body {ts}", ""]
    return "\n".join(out)


def gap(cid):
    return con_ro().execute("select gap_ts, gap_cursor from conversations where id = ?", (cid,)).fetchone()


def test_a_read_that_fails_past_page_one_leaves_a_gap_mark_and_the_next_read_fills_it(cfg_dict, fake_hand):
    fake_hand.scripts = [s for s in fake_hand.scripts if s[0] != "read_conversation"]
    fake_hand.script("search", {"filters": f"is:dm after:{DAY_BEFORE(T0)}"}, paged(FIXTURE("search-dm.txt"), None))
    p1 = _page_text("D01ABCDEF", "DM", ["1793106100.000003", "1793106100.000002"])
    p2 = _page_text("D01ABCDEF", "DM", ["1793106050.000002", "1793106050.000001"])
    fake_hand.script("read_conversation", {"channel_id": "D01ABCDEF", "cursor": None}, paged(p1, "p2"))
    fake_hand.fail_on("read_conversation", {"cursor": "p2"}, chat_archive.HandError("ToolError", tool="read_conversation"))
    rec = run_pass(cfg_dict, fake_hand, T0)
    assert rec["items_failed"] == 1 and gap("D01ABCDEF") == ("1793106100.000002", "p2")
    fake_hand.failures.clear()
    fake_hand.script("read_conversation", {"channel_id": "D01ABCDEF", "cursor": "p2"}, paged(p2, None))
    fake_hand.calls.clear()
    run_pass(cfg_dict, fake_hand, T0 + 1000)
    reads = [c[1].get("cursor") for c in fake_hand.calls if c[0] == "read_conversation"]
    assert reads == [None, "p2"]
    have = {r[0] for r in con_ro().execute("select ts from messages where conversation_id = 'D01ABCDEF'")}
    assert {"1793106050.000002", "1793106050.000001"} <= have and gap("D01ABCDEF") == (None, None)
    assert con_ro().execute("select next_try, backoff from conversations where id = 'D01ABCDEF'").fetchone() == (0, 0)


def test_a_capped_read_continues_from_its_cursor_on_the_next_pass_until_the_end(cfg_dict, fake_hand):
    fake_hand.scripts = [s for s in fake_hand.scripts if s[0] != "read_conversation"]
    fake_hand.script("search", {"filters": f"is:dm after:{DAY_BEFORE(T0)}"}, paged(FIXTURE("search-dm.txt"), None))
    for n in range(12):
        text = _page_text("D01ABCDEF", "DM", [f"1793106047.{99 - n:06d}"])
        fake_hand.script("read_conversation", {"cursor": f"p{n}"} if n else {"channel_id": "D01ABCDEF", "cursor": None},
                         paged(text, f"p{n + 1}" if n < 11 else None))
    rec = run_pass(cfg_dict, fake_hand, T0)
    assert rec["pages_capped"] == 1 and gap("D01ABCDEF") == ("1793106047.000090", "p10")
    fake_hand.calls.clear()
    rec = run_pass(cfg_dict, fake_hand, T0 + 400)
    reads = [c[1].get("cursor") for c in fake_hand.calls if c[0] == "read_conversation"]
    assert reads == [None, "p10", "p11"] and rec["pages_capped"] == 0
    assert con_ro().execute("select count(*) from messages where ts like '1793106047.0000%'").fetchone() == (12,)
    assert gap("D01ABCDEF") == (None, None)


def test_the_sweep_follows_the_cursor_of_an_active_conversation_until_a_known_message(cfg_dict, fake_hand):
    run_pass(cfg_dict, fake_hand, T0)
    with chat_archive.open_db() as w:
        w.execute("update conversations set last_ts = ? where id = 'C01GRPDM01'", (f"{T0 - 86400:.6f}",))
        w.execute("insert into messages (conversation_id, ts, text, captured_at, contiguous) values ('C01GRPDM01', '1793100000.000001', 'old', ?, 1)", (T0,))
    g1 = _page_text("C01GRPDM01", "Group DM", ["1793108000.000004", "1793108000.000003"])
    g2 = _page_text("C01GRPDM01", "Group DM", ["1793107000.000002", "1793100000.000001"])
    fake_hand.script("read_conversation", {"channel_id": "C01GRPDM01", "cursor": None}, paged(g1, "g2"))
    fake_hand.script("read_conversation", {"channel_id": "C01GRPDM01", "cursor": "g2"}, paged(g2, "g3"))
    fake_hand.calls.clear()
    rec = run_pass(cfg_dict, fake_hand, T0 + 3601)
    reads = [c[1].get("cursor") for c in fake_hand.calls if c[0] == "read_conversation" and c[1]["channel_id"] == "C01GRPDM01"]
    assert reads == [None, "g2"] and rec["errors"] == [] and rec["items_failed"] == 0
    have = {r[0] for r in con_ro().execute("select ts from messages where conversation_id = 'C01GRPDM01'")}
    assert {"1793108000.000004", "1793108000.000003", "1793107000.000002"} <= have
    assert con_ro().execute("select swept_at from conversations where id = 'C01GRPDM01'").fetchone() == (T0 + 3601,)


def test_a_malformed_message_ts_is_an_unparsed_block_and_an_unexpected_exception_is_a_step_failure(cfg_dict, fake_hand):
    fake_hand.script("search", {"keywords": ["U01ME00000"]}, paged(FIXTURE("search-context.txt"), None))
    bad = FIXTURE("thread.txt").replace("Message TS: 1793106855", "Message TS: x1793106855", 1)
    fake_hand.scripts = [s for s in fake_hand.scripts if s[0] != "read_thread"]
    fake_hand.script("read_thread", {"channel_id": "C01ABCDEF"}, paged(bad, None))
    rec = run_pass(cfg_dict, fake_hand, T0)
    assert rec["parser_failed"] == 2 and rec["errors"] == []
    assert con_ro().execute("select count(*) from messages where ts like 'x%'").fetchone() == (0,)
    assert outbox_lines("m1") == ["CHAT_ARCHIVE_FAILED parser → read_thread; see dockwright chat-archive status"]
    fake_hand.fail_all(ValueError("boom"))
    rec = run_pass(cfg_dict, fake_hand, T0 + 400, managers=[{"name": "m2"}])
    assert "dm: ValueError" in rec["errors"] and cursor("full") == f"{T0 + 400:.6f}" and cursor("dm") == f"{T0:.6f}"
    assert outbox_lines("m2") == ["CHAT_ARCHIVE_FAILED crash → dm: ValueError; see dockwright chat-archive status"]
    assert chat_archive.status(now=T0 + 410)["last_call_errors"] == rec["errors"]


def test_read_answers_carry_the_archive_state_and_the_started_mark(home, cfg, fake_hand):
    assert chat_archive.read("7d")["archive"] == "off" and chat_archive.search("x")["archive"] == "off"
    cfg(FULL_SECTION)
    assert chat_archive.read("7d") == {"conversations": [], "truncated": False, "archive": "empty", "since": None}
    assert chat_archive.conversations() == {"conversations": [], "archive": "empty", "since": None}
    fake_hand.script("search", {"filters": f"is:dm after:{DAY_BEFORE(T0)}"}, paged(FIXTURE("search-dm.txt"), None))
    run_pass(config.chat_archive(), fake_hand, T0)
    got = chat_archive.read("7d", now=T0 + 10)
    assert got["archive"] == "ok" and got["since"] == chat_archive._time(f"{T0:.6f}") and got["conversations"]
    assert chat_archive.search("lorem", now=T0 + 10)["archive"] == "ok"
    assert chat_archive.conversations(now=T0 + 10)["since"] == chat_archive._time(f"{T0:.6f}")
    assert chat_archive.read("7d", now=T0 + 901)["archive"] == "stale"
    assert chat_archive.read("7d", now=T0 + 901)["conversations"]
    cfg(FULL_SECTION.replace("enabled = true", "enabled = false"))
    assert chat_archive.read("7d", now=T0 + 10)["archive"] == "off"


def test_a_trigger_whose_conversation_read_fails_as_an_item_keeps_the_resolved_row(cfg_dict, fake_hand):
    run_pass(cfg_dict, fake_hand, T0)
    fake_hand.script("search", {"before": f"{float(DM_TS) + 1.5:.6f}"}, paged(FIXTURE("search-dm.txt"), None))
    fake_hand.fail_on("read_conversation", {"channel_id": "D01ABCDEF"}, chat_archive.HandError("HTTPError", status=400, tool="read_conversation"))
    file = write_trigger(f"E_{DM_TS}", DM_TS, "Ada Example", T0 + 60)
    rec = run_pass(cfg_dict, fake_hand, T0 + 90, wake="trigger")
    assert rec["triggers_done"] == 1 and rec["items_failed"] == 1 and rec["errors"] == ["HTTPError 400"]
    assert not file.exists() and outbox_lines("m1") == []
    assert con_ro().execute("select conversation_id from resolved where ts = ?", (DM_TS,)).fetchone() == ("D01ABCDEF",)
    assert con_ro().execute("select next_try from conversations where id = 'D01ABCDEF'").fetchone() == (T0 + 90 + 600,)


def test_mention_keys_default_to_the_self_user_id(cfg):
    cfg(FULL_SECTION.replace('mention_keys = ["U01ME00000", "S01GRP0001", "S01GRP0002", "S01GRP0003"]\n', ""))
    assert config.chat_archive()["mention_keys"] == ["U01ME00000"]
    cfg(FULL_SECTION.replace('mention_keys = ["U01ME00000", "S01GRP0001", "S01GRP0002", "S01GRP0003"]\n', "").replace('self_user_id = "U01ME00000"\n', ""))
    assert config.chat_archive()["mention_keys"] == []


def test_a_trigger_read_does_not_stop_on_the_message_the_trigger_just_recorded(cfg_dict, fake_hand):
    run_pass(cfg_dict, fake_hand, T0)
    with chat_archive.open_db() as w:
        w.execute("insert into messages (conversation_id, ts, text, captured_at, contiguous) values ('C01GRPDM01', '1793100000.000001', 'old', ?, 1)", (T0,))
    newest = "1793108000.000050"
    hit = (FIXTURE("search-dm.txt").split("### Result 2 of 2")[0].replace("2 results", "1 result").replace("Result 1 of 2", "Result 1 of 1")
           .replace("Channel: DM (ID: D01ABCDEF)", "Channel: Group DM (ID: C01GRPDM01)").replace("1793106658.209309", newest)
           .replace("p1793106658209309", "p1793108000000050"))
    fake_hand.script("search", {"before": f"{float(newest) + 1.5:.6f}"}, paged(hit, None))
    fake_hand.script("read_conversation", {"channel_id": "C01GRPDM01", "cursor": None},
                     paged(_page_text("C01GRPDM01", "Group DM", [newest, "1793108000.000049"]), "q2"))
    fake_hand.script("read_conversation", {"channel_id": "C01GRPDM01", "cursor": "q2"},
                     paged(_page_text("C01GRPDM01", "Group DM", ["1793108000.000048", "1793108000.000047"]), "q3"))
    fake_hand.script("read_conversation", {"channel_id": "C01GRPDM01", "cursor": "q3"},
                     paged(_page_text("C01GRPDM01", "Group DM", ["1793100000.000001"]), "q4"))
    write_trigger(f"E_{newest}", newest, "Group DM", T0 + 60)
    fake_hand.calls.clear()
    rec = run_pass(cfg_dict, fake_hand, T0 + 90, wake="trigger")
    reads = [c[1].get("cursor") for c in fake_hand.calls if c[0] == "read_conversation"]
    assert reads == [None, "q2", "q3"] and rec["triggers_done"] == 1
    have = {r[0] for r in con_ro().execute("select ts from messages where conversation_id = 'C01GRPDM01'")}
    assert {"1793108000.000048", "1793108000.000047"} <= have and gap("C01GRPDM01") == (None, None)


def test_the_archive_is_stale_when_a_search_step_has_not_committed_within_the_window(home, cfg, fake_hand):
    cfg(FULL_SECTION)
    fake_hand.script("search", {"filters": f"is:dm after:{DAY_BEFORE(T0)}"}, paged(FIXTURE("search-dm.txt"), None))
    run_pass(config.chat_archive(), fake_hand, T0)
    fake_hand.fail_all(chat_archive.AuthError("HTTPError", status=401, tool="search"))
    rec = run_pass(config.chat_archive(), fake_hand, T0 + 600)
    assert rec["errors"] == ["HTTPError 401"]
    assert chat_archive.read("1d", now=T0 + 899)["archive"] == "ok"
    assert chat_archive.read("1d", now=T0 + 910)["archive"] == "stale"
    assert chat_archive.read("1d", now=T0 + 910)["conversations"]
    fake_hand.fail_every = None
    fake_hand.fail_on("search", {"keywords": ["S01GRP0002"]}, chat_archive.HandError("HTTPError", status=503, tool="search"))
    run_pass(config.chat_archive(), fake_hand, T0 + 1200)
    assert dict(con_ro().execute("select step, updated_at from cursors where step in ('dm', 'mention:S01GRP0002')")) == {
        "dm": T0 + 1200, "mention:S01GRP0002": T0}
    assert chat_archive.read("1d", now=T0 + 1210)["archive"] == "stale"
    fake_hand.failures.clear()
    cfg(FULL_SECTION.replace("every_sec = 300", "every_sec = 600"))
    run_pass(config.chat_archive(), fake_hand, T0 + 1900)
    assert chat_archive.read("1d", now=T0 + 1900 + 1700)["archive"] == "ok"
    assert chat_archive.read("1d", now=T0 + 1900 + 1801)["archive"] == "stale"


def _group_hit(newest):
    return (FIXTURE("search-dm.txt").split("### Result 2 of 2")[0].replace("2 results", "1 result").replace("Result 1 of 2", "Result 1 of 1")
            .replace("Channel: DM (ID: D01ABCDEF)", "Channel: Group DM (ID: C01GRPDM01)").replace("1793106658.209309", newest)
            .replace("p1793106658209309", "p" + newest.replace(".", "")))


def test_a_trigger_hit_whose_read_fails_on_page_one_is_not_stored_and_the_sweep_reads_the_whole_conversation(cfg_dict, fake_hand):
    run_pass(cfg_dict, fake_hand, T0)
    newest = "1793108000.000050"
    fake_hand.script("search", {"before": f"{float(newest) + 1.5:.6f}"}, paged(_group_hit(newest), None))
    fake_hand.fail_on("read_conversation", {"channel_id": "C01GRPDM01", "limit": 40}, chat_archive.HandError("ToolError", tool="read_conversation"))
    write_trigger(f"E_{newest}", newest, "Group DM", T0 + 60)
    rec = run_pass(cfg_dict, fake_hand, T0 + 90, wake="trigger")
    assert rec["triggers_done"] == 1 and rec["items_failed"] == 1
    assert con_ro().execute("select count(*) from messages where conversation_id = 'C01GRPDM01'").fetchone() == (0,)
    assert con_ro().execute("select conversation_id, kind from resolved where ts = ?", (newest,)).fetchone() == ("C01GRPDM01", "group")
    assert con_ro().execute("select last_ts, next_try from conversations where id = 'C01GRPDM01'").fetchone() == (None, T0 + 90 + 600)
    fake_hand.failures.clear()
    fake_hand.script("read_conversation", {"channel_id": "C01GRPDM01", "limit": 1}, paged(_page_text("C01GRPDM01", "Group DM", [newest]), "x2"))
    fake_hand.script("read_conversation", {"channel_id": "C01GRPDM01", "limit": 40, "cursor": None},
                     paged(_page_text("C01GRPDM01", "Group DM", [newest, "1793108000.000049"]), "q2"))
    fake_hand.script("read_conversation", {"channel_id": "C01GRPDM01", "cursor": "q2"},
                     paged(_page_text("C01GRPDM01", "Group DM", ["1793108000.000048", "1793108000.000047"]), None))
    fake_hand.calls.clear()
    run_pass(cfg_dict, fake_hand, T0 + 3700)
    reads = [(c[1]["limit"], c[1].get("cursor")) for c in fake_hand.calls if c[0] == "read_conversation" and c[1]["channel_id"] == "C01GRPDM01"]
    assert reads == [(1, None), (40, None), (40, "q2")]
    have = {r[0] for r in con_ro().execute("select ts from messages where conversation_id = 'C01GRPDM01'")}
    assert have == {newest, "1793108000.000049", "1793108000.000048", "1793108000.000047"}


def test_the_sweep_probe_stores_nothing_until_the_full_read_succeeds(cfg_dict, fake_hand):
    run_pass(cfg_dict, fake_hand, T0)
    with chat_archive.open_db() as w:
        w.execute("update conversations set last_ts = ? where id = 'C01GRPDM01'", (f"{T0 - 30 * 86400:.6f}",))
    newest = "1793108000.000050"
    fake_hand.script("read_conversation", {"channel_id": "C01GRPDM01", "limit": 1}, paged(_page_text("C01GRPDM01", "Group DM", [newest]), "x2"))
    fake_hand.fail_on("read_conversation", {"channel_id": "C01GRPDM01", "limit": 40}, chat_archive.HandError("ToolError", tool="read_conversation"))
    raw_before = (paths.CHAT_RAW / f"{DAY}.jsonl").read_text()
    rec = run_pass(cfg_dict, fake_hand, T0 + 3700)
    assert rec["items_failed"] == 1
    assert con_ro().execute("select count(*) from messages where conversation_id = 'C01GRPDM01'").fetchone() == (0,)
    assert con_ro().execute("select last_ts from conversations where id = 'C01GRPDM01'").fetchone() == (f"{T0 - 30 * 86400:.6f}",)
    assert newest not in (paths.CHAT_RAW / f"{DAY}.jsonl").read_text().replace(raw_before, "")
    fake_hand.failures.clear()
    fake_hand.script("read_conversation", {"channel_id": "C01GRPDM01", "limit": 40, "cursor": None},
                     paged(_page_text("C01GRPDM01", "Group DM", [newest, "1793108000.000049"]), None))
    run_pass(cfg_dict, fake_hand, T0 + 7400)
    have = {r[0] for r in con_ro().execute("select ts from messages where conversation_id = 'C01GRPDM01'")}
    assert have == {newest, "1793108000.000049"} and gap("C01GRPDM01") == (None, None)


def test_a_mention_hit_in_a_group_dm_does_not_stop_the_sweeps_read(cfg_dict, fake_hand):
    run_pass(cfg_dict, fake_hand, T0)
    with chat_archive.open_db() as w:
        w.execute("insert into messages (conversation_id, ts, text, contiguous) values ('C01GRPDM01', '1793100000.000001', 'old', 1)")
    newest = "1793108000.000050"
    fake_hand.script("search", {"keywords": ["U01ME00000"]}, paged(_group_hit(newest), None))
    fake_hand.script("read_conversation", {"channel_id": "C01GRPDM01", "cursor": None},
                     paged(_page_text("C01GRPDM01", "Group DM", [newest, "1793108000.000049"]), "q2"))
    fake_hand.script("read_conversation", {"channel_id": "C01GRPDM01", "cursor": "q2"},
                     paged(_page_text("C01GRPDM01", "Group DM", ["1793108000.000048", "1793108000.000047", "1793100000.000001"]), "q3"))
    fake_hand.calls.clear()
    rec = run_pass(cfg_dict, fake_hand, T0 + 3700)
    assert rec["errors"] == [] and [c for c in fake_hand.calls if c[0] == "read_thread" and c[1]["message_ts"] == newest]
    reads = [c[1].get("cursor") for c in fake_hand.calls if c[0] == "read_conversation" and c[1]["channel_id"] == "C01GRPDM01"]
    assert reads == [None, "q2"]
    have = {r[0] for r in con_ro().execute("select ts from messages where conversation_id = 'C01GRPDM01'")}
    assert {newest, "1793108000.000049", "1793108000.000048", "1793108000.000047"} <= have and gap("C01GRPDM01") == (None, None)
    assert con_ro().execute("select contiguous from messages where ts = ?", (newest,)).fetchone() == (1,)


def test_a_dm_read_does_not_stop_on_a_message_only_the_search_stored(cfg_dict, fake_hand):
    newest = "1793108000.000050"
    hit = _group_hit(newest).replace("Channel: Group DM (ID: C01GRPDM01)", "Channel: DM (ID: D02NEWNEW)")
    fake_hand.script("search", {"filters": f"is:dm after:{DAY_BEFORE(T0)}"}, paged(hit, None))
    fake_hand.script("read_conversation", {"channel_id": "D02NEWNEW", "cursor": None},
                     paged(_page_text("D02NEWNEW", "DM", [newest, "1793108000.000049"]), "n2"))
    fake_hand.script("read_conversation", {"channel_id": "D02NEWNEW", "cursor": "n2"},
                     paged(_page_text("D02NEWNEW", "DM", ["1793108000.000048"]), None))
    run_pass(cfg_dict, fake_hand, T0)
    reads = [c[1].get("cursor") for c in fake_hand.calls if c[0] == "read_conversation" and c[1]["channel_id"] == "D02NEWNEW"]
    assert reads == [None, "n2"]
    rows = dict(con_ro().execute("select ts, contiguous from messages where conversation_id = 'D02NEWNEW'"))
    assert rows == {newest: 1, "1793108000.000049": 1, "1793108000.000048": 1}


def test_a_conversation_read_never_reaches_below_the_retention_window(cfg_dict, fake_hand):
    run_pass(cfg_dict, fake_hand, T0)
    assert all(c[1]["oldest"] == f"{T0:.6f}" for c in fake_hand.calls if c[0] == "read_conversation")
    later = T0 + 200 * 86400
    fake_hand.script("search", {"filters": f"is:dm after:{DAY_BEFORE(later)}"}, paged(FIXTURE("search-dm.txt").replace("1793107259.250289", f"{later - 60:.6f}"), None))
    fake_hand.calls.clear()
    run_pass(cfg_dict, fake_hand, later)
    reads = [c for c in fake_hand.calls if c[0] == "read_conversation"]
    assert reads and all(c[1]["oldest"] == f"{later - 90 * 86400:.6f}" for c in reads)


FOOTER = "*Sent using* <@U01APPAPP|SomeApp>"
SELF = "U01ABCDEF"


def _seed_dm(con):
    _seed_message(con, "D01ABCDEF", "10.1", "hi " + FOOTER, sender_id=SELF)
    _seed_message(con, "D01ABCDEF", "10.5", "ok, I'll handle", sender_id=SELF)
    _seed_message(con, "D01ABCDEF", "11.0", "thanks", sender_id="U01OTHER1", sender="Other One")
    con.commit()


def test_taken_over_sees_his_unsigned_message_between_our_send_and_the_reply(con):
    _seed_dm(con)
    assert chat_archive.taken_over("D01ABCDEF", "10.1", "11.0", FOOTER, SELF, con=con) is True


def test_dm_closed_names_a_listed_sender_as_skip(con):
    assert chat_archive.dm_closed("D01ABCDEF", "20.0", "U04APP", SELF, FOOTER, ["U04APP"], con=con) == "skip"


def test_dm_closed_names_his_unsigned_message_after_the_colleagues_as_answered(con):
    _seed_message(con, "D01ABCDEF", "20.0", "is it safe?", sender_id="U01OTHER1")
    _seed_message(con, "D01ABCDEF", "21.0", "yes, go ahead", sender_id=SELF)
    con.commit()
    assert chat_archive.dm_closed("D01ABCDEF", "20.0", "U01OTHER1", SELF, FOOTER, [], con=con) == "answered"


def test_dm_closed_is_none_for_an_unanswered_message_and_for_our_signed_answer(con):
    _seed_message(con, "D01ABCDEF", "20.0", "is it safe?", sender_id="U01OTHER1")
    _seed_message(con, "D01ABCDEF", "21.0", "yes " + FOOTER, sender_id=SELF)
    con.commit()
    assert chat_archive.dm_closed("D01ABCDEF", "20.0", "U01OTHER1", SELF, FOOTER, [], con=con) is None


def test_dm_closed_is_none_without_a_database(home, monkeypatch):
    monkeypatch.setattr(paths, "CHAT_DB", home / "none" / "archive.db")
    assert chat_archive.dm_closed("D01ABCDEF", "20.0", "U01OTHER1", SELF, FOOTER, []) is None


def test_taken_over_ignores_our_signed_messages_and_other_conversations(con):
    _seed_message(con, "D01ABCDEF", "10.1", "hi " + FOOTER, sender_id=SELF)
    _seed_message(con, "D01ABCDEF", "10.5", "again " + FOOTER, sender_id=SELF)
    _seed_message(con, "D01OTHER", "10.7", "me elsewhere", sender_id=SELF)
    _seed_message(con, "D01ABCDEF", "11.0", "thanks", sender_id="U01OTHER1", sender="Other One")
    con.commit()
    assert chat_archive.taken_over("D01ABCDEF", "10.1", "11.0", FOOTER, SELF, con=con) is False


def test_taken_over_and_sent_by_footer_return_false_or_none_without_a_db(home):
    assert not paths.CHAT_DB.exists()
    assert chat_archive.taken_over("D01ABCDEF", "10.1", "11.0", FOOTER, SELF) is False
    assert chat_archive.sent_by_footer("D01ABCDEF", None, "11.0", FOOTER, SELF) is None


def test_taken_over_and_sent_by_footer_swallow_a_sqlite_error(con, monkeypatch):
    _seed_dm(con)
    assert paths.CHAT_DB.exists()

    def broken(*a, **k):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(chat_archive, "open_db", broken)
    assert chat_archive.taken_over("D01ABCDEF", "10.1", "11.0", FOOTER, SELF) is False
    assert chat_archive.sent_by_footer("D01ABCDEF", None, "11.0", FOOTER, SELF) is None


def test_sent_by_footer_finds_our_latest_signed_message_in_the_conversation_or_thread(con):
    T = "9.0"
    _seed_dm(con)
    _seed_message(con, "D01ABCDEF", "12.0", "in thread " + FOOTER, sender_id=SELF, thread_ts=T)
    con.commit()
    assert chat_archive.sent_by_footer("D01ABCDEF", None, "11.0", FOOTER, SELF, con=con) == {"ts": "10.1", "thread": ""}
    assert chat_archive.sent_by_footer("D01ABCDEF", T, "13.0", FOOTER, SELF, con=con) == {"ts": "12.0", "thread": T}
    assert chat_archive.sent_by_footer("D01ABCDEF", None, "13.0", FOOTER, SELF, con=con) == {"ts": "10.1", "thread": ""}
    assert chat_archive.sent_by_footer("D01ABCDEF", None, "10.0", FOOTER, SELF, con=con) is None


def test_sent_by_footer_ignores_an_unsigned_message(con):
    _seed_message(con, "D01ABCDEF", "10.5", "ok, I'll handle", sender_id=SELF)
    _seed_message(con, "D01ABCDEF", "10.6", "signed by another " + FOOTER, sender_id="U01OTHER1")
    con.commit()
    assert chat_archive.sent_by_footer("D01ABCDEF", None, "11.0", FOOTER, SELF, con=con) is None


def _thread_with_our_footer_post():
    text = FIXTURE("thread.txt")
    assert "Bob Example <bob@example.test> (U01BOBBOB)\nTime: 2026-10-27 13:14:15 +00\nMessage TS: 1793106855.095329\nConsectetur adipiscing elit, the first reply." in text
    return text.replace(
        "Bob Example <bob@example.test> (U01BOBBOB)\nTime: 2026-10-27 13:14:15 +00\nMessage TS: 1793106855.095329\n"
        "Consectetur adipiscing elit, the first reply.",
        "Me Example <me@example.test> (U01ME00000)\nTime: 2026-10-27 13:14:15 +00\nMessage TS: 1793106855.095329\n"
        "our post in this thread\n" + FOOTER)


def _untracked_thread_trigger(cfg_dict, fake_hand, thread_text, **cfg_extra):
    run_pass(cfg_dict, fake_hand, T0 - 100)
    with chat_archive.open_db() as w:
        w.execute("delete from threads")
        w.execute("delete from messages where conversation_id = 'C01ABCDEF'")
    fake_hand.calls.clear()
    fake_hand.script("search", {"sort": "timestamp"}, paged(FIXTURE("search-window.txt"), None))
    fake_hand.scripts.insert(0, ("read_thread", {"channel_id": "C01ABCDEF", "message_ts": PARENT_TS}, [paged(thread_text, None)]))
    write_trigger(f"E3_{REPLY_TS}", REPLY_TS, "#some-channel", T0 + 160, kind="channel")
    return run_pass({**cfg_dict, "mention_keys": [], **cfg_extra}, fake_hand, T0 + 170, wake="trigger")


def test_an_untracked_channel_thread_joins_the_archive_when_it_holds_our_footer_signed_post(cfg_dict, fake_hand):
    rec = _untracked_thread_trigger(cfg_dict, fake_hand, _thread_with_our_footer_post(), sent_footer=FOOTER)
    assert rec["triggers_done"] == 1
    assert [c[0] for c in fake_hand.calls] == ["search", "read_thread"]
    con = con_ro()
    assert con.execute("select count(*) from messages where conversation_id='C01ABCDEF'").fetchone() == (3,)
    assert con.execute("select thread_ts, last_reply_ts from threads").fetchall() == [(PARENT_TS, REPLY_TS)]
    assert con.execute("select thread_ts from resolved where ts = ?", (REPLY_TS,)).fetchone() == (PARENT_TS,)
    assert chat_archive.sent_by_footer("C01ABCDEF", PARENT_TS, REPLY_TS, FOOTER, "U01ME00000", con=con) == {
        "ts": "1793106855.095329", "thread": PARENT_TS}


def test_an_untracked_channel_thread_without_our_footer_signed_post_stores_no_text(cfg_dict, fake_hand):
    rec = _untracked_thread_trigger(cfg_dict, fake_hand, FIXTURE("thread.txt"), sent_footer=FOOTER)
    assert rec["triggers_done"] == 1
    assert [c[0] for c in fake_hand.calls] == ["search", "read_thread"]
    con = con_ro()
    assert con.execute("select count(*) from messages where conversation_id='C01ABCDEF'").fetchone() == (0,)
    assert con.execute("select count(*) from threads").fetchone() == (0,)
    assert con.execute("select thread_ts from resolved where ts = ?", (REPLY_TS,)).fetchone() == (PARENT_TS,)
    raw = "".join(p.read_text() for p in paths.CHAT_RAW.glob("*.jsonl"))
    assert "Consectetur" not in raw and "Consectetur" not in paths.CHAT_LEDGER.read_text()


def test_an_untracked_channel_thread_is_not_read_while_sent_footer_is_empty(cfg_dict, fake_hand):
    rec = _untracked_thread_trigger(cfg_dict, fake_hand, _thread_with_our_footer_post())
    assert rec["triggers_done"] == 1 and [c[0] for c in fake_hand.calls] == ["search"]
    assert con_ro().execute("select count(*) from messages where conversation_id='C01ABCDEF'").fetchone() == (0,)
