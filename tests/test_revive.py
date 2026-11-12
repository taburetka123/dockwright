import builtins
import json
import os
import re
from pathlib import Path

import pytest

from dockwright import paths, revive, state

DEAD_PID = 999999


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    root = tmp_path / "dockwright"
    monkeypatch.setattr(paths, "ROOT", root)
    monkeypatch.setattr(paths, "ACTIVE", root / "active")
    monkeypatch.setattr(paths, "CLOSED", root / "closed")
    monkeypatch.setattr(paths, "HANDOFFS", root / "handoffs")
    monkeypatch.setattr(revive, "record_pid_is_session", lambda record: False)
    monkeypatch.setattr(revive, "record_pid_is_any_session", lambda record: False)
    (root / "active").mkdir(parents=True)
    (root / "closed").mkdir()
    return root


def _manager(sid, name, domain, started_at, **extra):
    record = {"claude_sid": sid, "agent": "manager", "name": name, "domain": domain,
              "started_at": started_at, "pid": 4242, "window_id": "%1",
              "cwd": "/tmp", "runtime": "claude", "account": "a", "state": "idle"}
    record.update(extra)
    return record


def _worker(sid, name, started_at, **extra):
    record = {"claude_sid": sid, "agent": "worker", "name": name,
              "started_at": started_at, "pid": 4243, "window_id": "%2",
              "cwd": "/tmp/wt", "runtime": "claude", "account": "a",
              "last_turn_at": 150.0, "last_summary": "did a thing"}
    record.update(extra)
    return record


def _write_records(root, records):
    for record in records:
        state.write_json_atomic(root / "active" / f"{record['claude_sid']}.json", record)


def _snapshot(directory):
    snap = {}
    for path in sorted(Path(directory).rglob("*")):
        if path.is_file():
            snap[str(path.relative_to(directory))] = path.read_bytes()
    return snap


def _spy_write_paths(monkeypatch):
    seen = []

    def record(target):
        try:
            seen.append(Path(target).resolve())
        except (OSError, TypeError, ValueError):
            pass

    real_unlink = Path.unlink
    real_write_text = Path.write_text
    real_write_bytes = Path.write_bytes
    real_rename = Path.rename
    real_replace = Path.replace
    real_open = builtins.open
    real_os_remove = os.remove
    real_os_unlink = os.unlink
    real_os_replace = os.replace
    real_os_rename = os.rename

    def unlink(self, *a, **k):
        record(self)
        return real_unlink(self, *a, **k)

    def write_text(self, *a, **k):
        record(self)
        return real_write_text(self, *a, **k)

    def write_bytes(self, *a, **k):
        record(self)
        return real_write_bytes(self, *a, **k)

    def rename(self, target):
        record(self)
        record(target)
        return real_rename(self, target)

    def replace(self, target):
        record(self)
        record(target)
        return real_replace(self, target)

    def opener(file, mode="r", *a, **k):
        if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
            record(file)
        return real_open(file, mode, *a, **k)

    def os_remove(path, *a, **k):
        record(path)
        return real_os_remove(path, *a, **k)

    def os_unlink(path, *a, **k):
        record(path)
        return real_os_unlink(path, *a, **k)

    def os_replace(src, dst, *a, **k):
        record(src)
        record(dst)
        return real_os_replace(src, dst, *a, **k)

    def os_rename(src, dst, *a, **k):
        record(src)
        record(dst)
        return real_os_rename(src, dst, *a, **k)

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(Path, "write_text", write_text)
    monkeypatch.setattr(Path, "write_bytes", write_bytes)
    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(builtins, "open", opener)
    monkeypatch.setattr(os, "remove", os_remove)
    monkeypatch.setattr(os, "unlink", os_unlink)
    monkeypatch.setattr(os, "replace", os_replace)
    monkeypatch.setattr(os, "rename", os_rename)
    return seen


def test_domain_defaults_to_general_when_absent_or_empty():
    assert revive.normalize_domain({"name": "x"}) == "general"
    assert revive.normalize_domain({"domain": None}) == "general"
    assert revive.normalize_domain({"domain": "  "}) == "general"
    assert revive.normalize_domain({"domain": " product "}) == "product"


def test_preboot_record_with_a_live_reused_pid_is_revived():
    records = [_manager("s1", "dead-one", "general", 100.0, pid=os.getpid())]
    plan = revive.build_plan(records, boot_epoch=200, transcript_mtime=lambda r: None)
    assert [c.verdict for c in plan] == ["revive"]


def test_postboot_record_is_skipped():
    records = [_manager("s1", "live-one", "general", 300.0)]
    plan = revive.build_plan(records, boot_epoch=200, transcript_mtime=lambda r: None)
    assert [c.verdict for c in plan] == ["skip-postboot"]


def test_candidate_whose_domain_is_already_covered_is_skipped():
    records = [_manager("dead", "ghost", "general", 100.0),
               _manager("live", "fresh", "general", 300.0)]
    plan = revive.build_plan(records, boot_epoch=200, transcript_mtime=lambda r: None)
    by_sid = {c.sid: c.verdict for c in plan}
    assert by_sid == {"dead": "skip-covered", "live": "skip-postboot"}
    covered = next(c for c in plan if c.sid == "dead")
    assert covered.reason == "already covered by fresh"


def test_two_ghosts_on_one_domain_yield_exactly_one_revive():
    records = [_manager("g1", "ghost-a", "product", 100.0),
               _manager("g2", "ghost-b", "product", 110.0)]
    plan = revive.build_plan(records, boot_epoch=200, transcript_mtime=lambda r: None)
    assert sorted(c.verdict for c in plan) == ["revive", "skip-covered"]
    assert next(c for c in plan if c.sid == "g2").verdict == "revive"


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_the_newest_ghost_wins_a_shared_domain_whatever_the_record_order(order):
    ghosts = [_manager("aaaa1111-0000-4000-8000-000000000000", "old-crash",
                       "general", 100.0),
              _manager("ffff2222-0000-4000-8000-000000000000", "died-in-reboot",
                       "general", 190.0)]
    records = [ghosts[i] for i in order]
    plan = revive.build_plan(records, boot_epoch=200, transcript_mtime=lambda r: None)
    by_name = {c.name: c.verdict for c in plan}
    assert by_name == {"died-in-reboot": "revive", "old-crash": "skip-covered"}
    assert next(c for c in plan
                if c.name == "old-crash").reason == "already covered by died-in-reboot"


def test_ghosts_sharing_a_started_at_still_claim_the_domain_the_same_way():
    ghosts = [_manager("aaaa1111-0000-4000-8000-000000000000", "first", "general", 100.0),
              _manager("ffff2222-0000-4000-8000-000000000000", "second", "general", 100.0)]
    forward = revive.build_plan(ghosts, boot_epoch=200, transcript_mtime=lambda r: None)
    backward = revive.build_plan(list(reversed(ghosts)), boot_epoch=200,
                                 transcript_mtime=lambda r: None)
    winner = {c.name for c in forward if c.verdict == "revive"}
    assert winner == {c.name for c in backward if c.verdict == "revive"}
    assert len(winner) == 1


def test_every_fixture_record_is_classified_by_property_not_by_filter():
    records = [_manager("g1", "ghost", "general", 100.0),
               _manager("l1", "live", "product", 300.0),
               _manager("n1", "nostart", "ops", None),
               {"claude_sid": "w1", "agent": "worker", "name": "w", "started_at": 100.0}]
    plan = revive.build_plan(records, boot_epoch=200, transcript_mtime=lambda r: None)
    assert {c.sid for c in plan} == {"g1", "l1", "n1"}
    assert {c.sid for c in plan if c.verdict == "revive"} == {"g1"}
    assert {c.verdict for c in plan} <= {"revive", "skip-postboot", "skip-covered",
                                         "skip-alive", "skip-unusable"}


def test_a_record_that_has_not_registered_a_domain_covers_nothing():
    records = [_manager("b1", "will-be-product", None, 300.0),
               _manager("g1", "dead-general", "general", 100.0)]
    plan = revive.build_plan(records, boot_epoch=200, transcript_mtime=lambda r: None)
    assert {c.sid: c.verdict for c in plan} == {"b1": "skip-postboot", "g1": "revive"}


def test_every_live_domainless_record_halts_the_run_whatever_its_verdict():
    booting = _manager("b1", "will-be-product", None, 300.0, pid=os.getpid())
    dead = _manager("b2", "never-registered", None, 300.0, pid=DEAD_PID)
    named = _manager("b3", "already-named", "product", 300.0, pid=os.getpid())
    resumed = _manager("b4", "resumed-no-domain", None, 100.0, pid=os.getpid())
    preboot = _manager("g1", "ghost", None, 100.0, pid=os.getpid())
    mtimes = {"b4": 300.0}
    plan = revive.build_plan([booting, dead, named, resumed, preboot], boot_epoch=200,
                             transcript_mtime=lambda r: mtimes.get(r["claude_sid"]))
    assert {c.sid: c.verdict for c in plan}["b4"] == "skip-alive"
    assert [c.sid for c in revive.booting_managers(plan)] == ["b1", "b4"]


def test_a_nested_manager_agent_neither_enters_the_plan_nor_covers_the_domain():
    nested = {"claude_sid": "nested-1a2b3c4d", "agent": "manager", "nested": True,
              "name": "nested-1a2b3c4d", "domain": None, "started_at": 100.0,
              "pid": 4242, "window_id": "", "cwd": "/tmp", "runtime": "claude"}
    records = [nested, _manager("g1", "proud-pegasus", "general", 100.0)]
    plan = revive.build_plan(records, boot_epoch=200, transcript_mtime=lambda r: None)
    assert [(c.sid, c.verdict) for c in plan] == [("g1", "revive")]


def test_a_nested_worker_agent_is_not_reported_as_a_dead_worker():
    records = [_manager("g1", "alpha", "general", 100.0),
               _worker("w1", "wk-one", 100.0, parent_manager_name="alpha"),
               _worker("n1", "nested-wk", 100.0, parent_manager_name="alpha",
                       nested=True)]
    plan = revive.build_plan(records, boot_epoch=200, transcript_mtime=lambda r: None)
    by_manager, unattributed = revive.split_dead_workers(records, 200, plan)
    assert [w["name"] for w in by_manager["alpha"]] == ["wk-one"]
    assert unattributed == []


def test_preboot_start_with_postboot_transcript_mtime_is_alive_not_revived():
    records = [_manager("s1", "recovered", "general", 100.0)]
    plan = revive.build_plan(records, boot_epoch=200, transcript_mtime=lambda r: 300.0)
    assert [c.verdict for c in plan] == ["skip-alive"]


def test_preboot_start_with_preboot_transcript_mtime_is_revived():
    records = [_manager("s1", "ghost", "general", 100.0)]
    plan = revive.build_plan(records, boot_epoch=200, transcript_mtime=lambda r: 150.0)
    assert [c.verdict for c in plan] == ["revive"]


def test_a_record_alive_by_transcript_activity_covers_its_domain():
    records = [_manager("g1", "ghost", "general", 100.0),
               _manager("a1", "still-here", "general", 100.0)]
    mtimes = {"a1": 300.0}
    plan = revive.build_plan(records, boot_epoch=200,
                             transcript_mtime=lambda r: mtimes.get(r["claude_sid"]))
    by_sid = {c.sid: c.verdict for c in plan}
    assert by_sid == {"a1": "skip-alive", "g1": "skip-covered"}
    assert next(c for c in plan if c.sid == "g1").reason == "already covered by still-here"
    assert not [c for c in plan if c.verdict == "revive"]


def test_a_record_with_no_started_at_does_not_cover_its_domain():
    records = [_manager("n1", "unreadable", "general", None),
               _manager("g1", "ghost", "general", 100.0)]
    plan = revive.build_plan(records, boot_epoch=200, transcript_mtime=lambda r: None)
    by_sid = {c.sid: c.verdict for c in plan}
    assert by_sid == {"n1": "skip-unusable", "g1": "revive"}


@pytest.mark.parametrize("started", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_started_at_is_unusable_and_does_not_cover_its_domain(started):
    records = [_manager("n1", "weird", "general", started),
               _manager("g1", "ghost", "general", 100.0)]
    plan = revive.build_plan(records, boot_epoch=200, transcript_mtime=lambda r: None)
    by_sid = {c.sid: c.verdict for c in plan}
    assert by_sid == {"n1": "skip-unusable", "g1": "revive"}


def test_reduction_allow_lists_by_type_so_a_new_vendor_type_cannot_leak():
    lines = [json.dumps({"type": "atis-latch", "message": {"content": "LEAK"}}),
             json.dumps({"type": "user", "message": {"content": "keep me"}})]
    blocks = revive.reduce_transcript(lines)
    assert [b.text for b in blocks] == ["keep me"]


def test_reduction_allow_lists_the_eng_shape():
    lines = [json.dumps({"type": "user", "message": {"content": "<never-seen-wrapper>x"}}),
             json.dumps({"type": "user", "isMeta": True, "message": {"content": "meta"}}),
             json.dumps({"type": "user", "message": {"content": "real"}})]
    blocks = revive.reduce_transcript(lines)
    assert [b.text for b in blocks] == ["real"]


def test_reduction_keeps_assistant_text_and_drops_tool_traffic():
    lines = [json.dumps({"type": "assistant", "timestamp": "2026-08-15T00:35:45.000Z",
                         "message": {"content": [
                             {"type": "text", "text": "planning the thing"},
                             {"type": "tool_use", "name": "Bash",
                              "input": {"command": "rm -rf /"}}]}}),
             json.dumps({"type": "assistant", "message": {"content": [
                 {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}}]}})]
    blocks = revive.reduce_transcript(lines)
    assert [(b.who, b.text) for b in blocks] == [("MGR", "planning the thing")]


def test_ceiling_truncates_from_the_start_and_names_the_dropped_bytes():
    blocks = [revive.Block(ts=None, who="ENG", text=f"block-{i:02d} " + "x" * 200)
              for i in range(20)]
    record = _manager("s1", "ghost", "general", 100.0)
    digest, dropped_bytes = revive.render_digest(record, blocks, [],
                                                 Path("/t/s1.jsonl"), 999999,
                                                 max_bytes=1200)
    assert "block-00" not in digest
    assert "block-19" in digest
    assert len(digest.encode("utf-8")) <= 1200
    match = re.search(r"dropped (\d+) bytes from the start", digest)
    assert match is not None
    assert int(match.group(1)) > 0
    assert dropped_bytes == int(match.group(1))


def test_digest_under_the_ceiling_keeps_every_block_and_says_nothing_about_truncation():
    blocks = [revive.Block(ts=None, who="ENG", text=f"block-{i:02d}") for i in range(5)]
    record = _manager("s1", "ghost", "general", 100.0)
    digest, dropped_bytes = revive.render_digest(record, blocks, [],
                                                 Path("/t/s1.jsonl"), 100,
                                                 max_bytes=600000)
    assert all(f"block-{i:02d}" in digest for i in range(5))
    assert "TRUNCATED" not in digest
    assert dropped_bytes == 0


def test_ceiling_reads_its_env_seam():
    assert revive.digest_max_bytes({}) == 600000
    assert revive.digest_max_bytes(
        {"DOCKWRIGHT_REVIVE_DIGEST_MAX_BYTES": "1234"}) == 1234
    assert revive.digest_max_bytes(
        {"DOCKWRIGHT_REVIVE_DIGEST_MAX_BYTES": "junk"}) == 600000


def test_noop_line_reports_unverified_liveness_instead_of_a_healthy_fleet():
    records = [_manager("s1", "live", "general", 300.0, pid=DEAD_PID)]
    plan = revive.build_plan(records, boot_epoch=200, transcript_mtime=lambda r: None)
    line = revive.render_noop_line(plan, 200)
    assert line is not None
    assert "no pre-reboot ghosts" in line
    assert "1 record(s) postdate boot" in line
    assert "liveness NOT verified" in line
    assert "healthy" not in line
    assert "unreadable" not in line
    assert "transcript activity" not in line

    mixed = revive.build_plan(records + [_manager("s2", "murky", "ops", None)],
                              boot_epoch=200, transcript_mtime=lambda r: None)
    mixed_line = revive.render_noop_line(mixed, 200)
    assert "1 record(s) postdate boot" in mixed_line
    assert "1 record(s) with an unreadable started_at" in mixed_line

    empty_line = revive.render_noop_line([], 200)
    assert empty_line == "revive: no pre-reboot ghosts (boot 200); no manager records"


def test_noop_line_reports_the_measurement_not_a_liveness_verdict():
    records = [_manager("a1", "still-here", "product", 100.0)]
    plan = revive.build_plan(records, boot_epoch=200, transcript_mtime=lambda r: 300.0)
    assert [c.verdict for c in plan] == ["skip-alive"]
    line = revive.render_noop_line(plan, 200)
    assert "1 record(s) showed transcript activity after boot" in line
    assert "liveness" not in line
    assert "verified" not in line
    assert "postdate boot" not in line
    assert "healthy" not in line


def test_noop_line_is_withheld_while_a_ghost_is_in_the_plan():
    records = [_manager("g1", "ghost", "general", 100.0)]
    plan = revive.build_plan(records, boot_epoch=200, transcript_mtime=lambda r: None)
    assert revive.render_noop_line(plan, 200) is None


def test_dead_workers_attribute_by_parent_manager_name(state_root):
    records = [_manager("g1", "alpha", "general", 100.0),
               _worker("w1", "wk-one", 100.0, parent_manager_name="alpha"),
               _worker("w2", "wk-two", 100.0),
               _worker("w3", "wk-three", 100.0, parent_manager_name="nobody"),
               _worker("w4", "wk-four", 300.0, parent_manager_name="alpha")]
    plan = revive.build_plan(records, boot_epoch=200, transcript_mtime=lambda r: None)
    by_manager, unattributed = revive.split_dead_workers(records, 200, plan)
    assert [w["name"] for w in by_manager["alpha"]] == ["wk-one"]
    assert sorted(w["name"] for w in unattributed) == ["wk-three", "wk-two"]
    assert revive.unattributed_digest_path().name == "_unattributed.md"
    assert revive.unattributed_digest_path().parent == state_root / "revive"
    body = revive.render_unattributed(unattributed)
    assert "wk-two" in body and "wk-three" in body and "wk-one" not in body


def test_a_covered_ghost_keeps_its_own_dead_workers(state_root):
    records = [_manager("g1", "old-crash", "product", 100.0),
               _manager("g2", "died-in-reboot", "product", 190.0),
               _worker("w1", "wk-one", 100.0, parent_manager_name="old-crash")]
    plan = revive.build_plan(records, boot_epoch=200, transcript_mtime=lambda r: None)
    by_manager, unattributed = revive.split_dead_workers(records, 200, plan)
    assert [w["name"] for w in by_manager["old-crash"]] == ["wk-one"]
    assert by_manager["died-in-reboot"] == []
    assert unattributed == []


def test_digest_lists_the_dead_workers_of_its_own_manager(state_root):
    records = [_manager("g1", "alpha", "general", 100.0),
               _worker("w1", "wk-one", 100.0, parent_manager_name="alpha")]
    plan = revive.build_plan(records, boot_epoch=200, transcript_mtime=lambda r: None)
    by_manager, _ = revive.split_dead_workers(records, 200, plan)
    digest, _dropped = revive.render_digest(records[0], [], by_manager["alpha"],
                                            Path("/t/g1.jsonl"), 10)
    assert "wk-one" in digest
    assert "/tmp/wt" in digest
    assert "did a thing" in digest
    assert "w1" in digest


def test_build_plan_leaves_the_state_tree_byte_identical(state_root):
    _write_records(state_root, [_manager("g1", "ghost", "general", 100.0),
                                _manager("l1", "live", "product", 300.0),
                                _worker("w1", "wk", 100.0, parent_manager_name="ghost")])
    before = _snapshot(state_root)
    plan = revive.build_plan(revive.load_active_records(), boot_epoch=200,
                             transcript_mtime=lambda r: None)
    assert sorted(c.verdict for c in plan) == ["revive", "skip-postboot"]
    assert _snapshot(state_root) == before


def test_no_core_path_removes_or_rewrites_an_active_record(state_root, monkeypatch):
    _write_records(state_root, [_manager("g1", "ghost", "general", 100.0),
                                _manager("l1", "live", "product", 300.0),
                                _worker("w1", "wk", 100.0, parent_manager_name="ghost")])
    before = _snapshot(state_root)
    active = paths.ACTIVE.resolve()

    seen = _spy_write_paths(monkeypatch)
    records = revive.load_active_records()
    plan = revive.build_plan(records, boot_epoch=200)
    by_manager, unattributed = revive.split_dead_workers(records, 200, plan)
    blocks = revive.reduce_transcript(
        [json.dumps({"type": "user", "message": {"content": "hello"}})])
    for candidate in plan:
        source_path, _searched = revive.resolve_transcript(candidate.record)
        revive.render_digest(candidate.record, blocks,
                             by_manager.get(candidate.name, []), source_path, 0)
    revive.render_unattributed(unattributed)

    touched = [p for p in seen if p == active or active in p.parents]
    assert touched == []
    assert _snapshot(state_root) == before


def test_unresolvable_transcript_names_the_failure_and_the_searched_paths(
        state_root, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    record = _manager("s1", "ghost", "general", 100.0, account="z")
    source_path, searched = revive.resolve_transcript(record)
    assert source_path is None
    assert searched
    digest, _dropped = revive.render_digest(record, [], [], source_path, 0)
    assert "NO TRANSCRIPT RESOLVED" in digest
    assert "sid s1" in digest
    assert "account z" in digest
    for path in searched:
        assert str(path) in digest


def test_transcript_resolves_from_the_record_field_first(state_root, tmp_path):
    log = tmp_path / "recorded.jsonl"
    log.write_text("{}\n")
    record = _manager("s1", "ghost", "general", 100.0, transcript_path=str(log))
    source_path, _searched = revive.resolve_transcript(record)
    assert source_path == log


def _account_projects(tmp_path, monkeypatch, letter="z"):
    account_root = tmp_path / f"account-{letter}"
    project = account_root / "projects" / "-Users-x-proj"
    project.mkdir(parents=True)
    (project / "s1.jsonl").write_text("{}\n")
    monkeypatch.setattr(paths, "account_config_dir",
                        lambda name: account_root if name == letter
                        else pytest.fail(f"resolved the wrong account: {name!r}"))
    return project / "s1.jsonl"


def test_transcript_falls_back_to_the_pool_account_projects_dir(
        state_root, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    log = _account_projects(tmp_path, monkeypatch)
    record = _manager("s1", "ghost", "general", 100.0, account="z")
    source_path, _searched = revive.resolve_transcript(record)
    assert source_path == log


def test_the_home_projects_dir_beats_the_pool_account_dir(
        state_root, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home_project = home / ".claude" / "projects" / "-Users-x-proj"
    home_project.mkdir(parents=True)
    (home_project / "s1.jsonl").write_text("{}\n")
    monkeypatch.setenv("HOME", str(home))
    account_log = _account_projects(tmp_path, monkeypatch)
    record = _manager("s1", "ghost", "general", 100.0, account="z")
    source_path, _searched = revive.resolve_transcript(record)
    assert source_path == home_project / "s1.jsonl"
    assert source_path != account_log


def test_digest_path_is_scoped_to_the_state_root(state_root):
    assert revive.digest_path("s1") == state_root / "revive" / "s1.md"
