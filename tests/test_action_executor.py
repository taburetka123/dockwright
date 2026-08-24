"""Tests for deploy/scripts/action_executor.py (Phase D T10).

Loaded via importlib (deployed scripts are standalone, not a package).
Every guard here was observed RED before its implementation (TDD;
drift-guard-tests.md): the adversarial-syntax class especially — without
it a fake-actuator harness only ever sees well-formed ids.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "action_executor.py"


def _load():
    spec = importlib.util.spec_from_file_location("action_executor", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ax = _load()


def make_config(**over):
    cfg = {
        "verb": "demo-replay",
        "actuator": ["/bin/echo", "replay", "{queue}", "{message_id}"],
        "id_template": "{queue}/{message_id}",
        "patterns": {"queue": "^[a-z0-9][a-z0-9-]*$", "message_id": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
        "require": {"replay": "transient"},
        "forbid": {"source": "gh-actions"},
        "allow": {"queue": ["orders-dlq", "billing-dlq"]},
        "max_items": 50,
        "timeout_sec": 5,
    }
    cfg.update(over)
    return cfg


def make_item(**over):
    item = {"queue": "orders-dlq", "message_id": "abc-123", "handler": "H",
            "replay": "transient", "source": "queue-service"}
    item.update(over)
    return item


def classify(item, cfg=None, executed=frozenset(), ground_cache=None):
    cfg = cfg or make_config()
    compiled = ax.compile_patterns(cfg)
    return ax.classify_item(item, cfg, compiled, set(executed), ground_cache if ground_cache is not None else {})


class TestParseProposal:
    def test_valid_minimal(self):
        p = ax.parse_proposal(json.dumps(
            {"proposal_format": 1, "verb": "demo-replay", "items": [make_item()]}))
        assert p["verb"] == "demo-replay" and len(p["items"]) == 1

    def test_not_json_raises(self):
        with pytest.raises(ValueError):
            ax.parse_proposal("not json {")

    def test_wrong_format_version_raises(self):
        with pytest.raises(ValueError):
            ax.parse_proposal(json.dumps({"proposal_format": 2, "verb": "v", "items": []}))

    def test_missing_items_raises(self):
        with pytest.raises(ValueError):
            ax.parse_proposal(json.dumps({"proposal_format": 1, "verb": "v"}))

    def test_non_dict_item_raises(self):
        with pytest.raises(ValueError):
            ax.parse_proposal(json.dumps({"proposal_format": 1, "verb": "v", "items": ["x"]}))


class TestValidateConfig:
    def test_valid_config_no_problems(self):
        assert ax.validate_config(make_config()) == []

    def test_unpatterned_templated_field_is_a_problem(self):
        cfg = make_config(patterns={"queue": "^[a-z0-9][a-z0-9-]*$"})  # message_id templated but unpatterned
        problems = ax.validate_config(cfg)
        assert any("message_id" in p for p in problems)

    def test_bad_regex_is_a_problem(self):
        cfg = make_config(patterns={"queue": "([", "message_id": "^x$"})  # queue: invalid regex
        assert any("queue" in p for p in ax.validate_config(cfg))

    def test_actuator_must_be_list_of_str(self):
        assert ax.validate_config(make_config(actuator="echo hi")) != []

    def test_missing_required_key_is_a_problem(self):
        cfg = make_config()
        del cfg["id_template"]
        assert any("id_template" in p for p in ax.validate_config(cfg))

    def test_ground_in_path_fields_must_be_patterned(self):
        cfg = make_config(ground_in={"message_id": "/tmp/raw/{topic}.json"})  # topic unpatterned
        assert any("topic" in p for p in ax.validate_config(cfg))


class TestClassify:
    def test_happy_item_executes_with_argv(self):
        d = classify(make_item())
        assert d.kind == "EXECUTE"
        assert d.argv == ["/bin/echo", "replay", "orders-dlq", "abc-123"]
        assert d.id_key == "orders-dlq/abc-123"

    def test_missing_field_refused(self):
        item = make_item()
        del item["source"]
        d = classify(item)
        assert d.kind == "REFUSED" and "source" in d.reason

    @pytest.mark.parametrize("bad_id", ["--discard", "123/x", "../x", "a b", ""])
    def test_adversarial_message_id_refused(self, bad_id):
        d = classify(make_item(message_id=bad_id))
        assert d.kind == "REFUSED" and "message_id" in d.reason

    def test_hold_filtered(self):
        d = classify(make_item(replay="hold"))
        assert d.kind == "FILTERED" and "replay" in d.reason

    def test_gh_actions_source_filtered(self):
        d = classify(make_item(source="gh-actions"))
        assert d.kind == "FILTERED"

    def test_queue_not_allowlisted_filtered(self):
        d = classify(make_item(queue="rogue-dlq"))
        assert d.kind == "FILTERED"

    def test_duplicate_id_filtered(self):
        d = classify(make_item(), executed=frozenset({"orders-dlq/abc-123"}))
        assert d.kind == "FILTERED" and "already executed" in d.reason

    def test_presence_beats_policy(self):
        # An item missing a templated field is REFUSED even when a policy
        # predicate would also have filtered it (fail-closed ordering).
        item = make_item(replay="hold")
        del item["message_id"]
        assert classify(item).kind == "REFUSED"


class TestGrounding:
    def _cfg(self, tmp_path):
        raw = tmp_path / "raw" / "orders-dlq.json"
        raw.parent.mkdir(parents=True)
        raw.write_text(json.dumps({"messages": [{"id": "abc-123"}]}))
        return make_config(ground_in={"message_id": str(tmp_path / "raw" / "{queue}.json")})

    def test_grounded_value_executes(self, tmp_path):
        d = classify(make_item(), cfg=self._cfg(tmp_path))
        assert d.kind == "EXECUTE"

    def test_ungrounded_value_refused(self, tmp_path):
        d = classify(make_item(message_id="zzz-999"), cfg=self._cfg(tmp_path))
        assert d.kind == "REFUSED" and "ground" in d.reason

    def test_missing_grounding_file_refused(self, tmp_path):
        cfg = make_config(ground_in={"message_id": str(tmp_path / "nope" / "{queue}.json")})
        d = classify(make_item(), cfg=cfg)
        assert d.kind == "REFUSED" and "ground" in d.reason


class TestGroundingJsonMembership:
    """I-3: grounding is exact leaf-value membership, not raw substring. The
    review proved abc, c-12, and the JSON KEY 'messages' all 'grounded' against
    {"messages":[{"id":"abc-123"}]} under substring matching."""

    def _cfg(self, tmp_path, content='{"messages":[{"id":"abc-123"}]}'):
        raw = tmp_path / "raw" / "orders-dlq.json"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(content)
        return make_config(ground_in={"message_id": str(tmp_path / "raw" / "{queue}.json")})

    @pytest.mark.parametrize("bypass", ["abc", "c-12", "messages"])
    def test_substring_and_key_bypasses_refused(self, tmp_path, bypass):
        d = classify(make_item(message_id=bypass), cfg=self._cfg(tmp_path))
        assert d.kind == "REFUSED" and "ground" in d.reason

    def test_exact_leaf_value_executes(self, tmp_path):
        d = classify(make_item(message_id="abc-123"), cfg=self._cfg(tmp_path))
        assert d.kind == "EXECUTE"

    def test_unparseable_grounding_file_refused(self, tmp_path):
        d = classify(make_item(), cfg=self._cfg(tmp_path, content="{not json"))
        assert d.kind == "REFUSED" and "not valid JSON" in d.reason

    def test_numeric_leaf_value_matched_as_string(self, tmp_path):
        # A numeric leaf grounds a string id via str(leaf) - exact, not substring.
        cfg = self._cfg(tmp_path, content='{"ids":[42, 123]}')
        assert classify(make_item(message_id="42"), cfg=cfg).kind == "EXECUTE"
        assert classify(make_item(message_id="4"), cfg=cfg).kind == "REFUSED"


FAKE_ACTUATOR = """#!/usr/bin/env python3
import sys
with open(sys.argv[1], "a") as fh:
    fh.write(" ".join(sys.argv[2:]) + "\\n")
sys.exit(int(sys.argv[2] == "FAIL") * 3)
"""


def setup_env(tmp_path, items, cfg_over=None, proposal_over=None):
    """Actions dir + verb config + fake recording actuator + proposal file."""
    actions = tmp_path / "actions"
    (actions / "verbs").mkdir(parents=True)
    record = tmp_path / "record.txt"
    fake = tmp_path / "fake_actuator.py"
    fake.write_text(FAKE_ACTUATOR)
    cfg = make_config(
        actuator=[sys.executable, str(fake), str(record), "{queue}", "{message_id}"])
    cfg.update(cfg_over or {})
    (actions / "verbs" / "demo-replay.json").write_text(json.dumps(cfg))
    proposal = {"proposal_format": 1, "verb": "demo-replay", "run_id": "r1",
                "source_artifact": "src.json", "items": items}
    proposal.update(proposal_over or {})
    ppath = tmp_path / "proposal.json"
    ppath.write_text(json.dumps(proposal))
    return actions, ppath, record


def run_cli(actions, ppath, *extra):
    return ax.main(["--verb", "demo-replay", "--proposal", str(ppath),
                    "--actions-dir", str(actions)] + list(extra))


def ledger_events(actions):
    path = actions / "ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestCli:
    def test_happy_path_executes_ledgers_exit0(self, tmp_path, capsys):
        actions, ppath, record = setup_env(tmp_path, [make_item()])
        assert run_cli(actions, ppath, "--approved-by", "human") == 0
        assert record.read_text() == "orders-dlq abc-123\n"
        events = ledger_events(actions)
        types = [e["type"] for e in events]
        assert types == ["run", "action_dispatch", "action_executed"]
        run_ev = events[0]
        assert run_ev["approved_by"] == "human"
        assert run_ev["proposal_sha256"] == __import__("hashlib").sha256(
            ppath.read_bytes()).hexdigest()
        assert all(e["v"] == 1 and "ts" in e for e in events)

    def test_filtered_items_exit0_and_ledgered(self, tmp_path):
        actions, ppath, record = setup_env(
            tmp_path, [make_item(replay="hold"), make_item(source="gh-actions")])
        assert run_cli(actions, ppath) == 0
        assert not record.exists()
        assert [e["type"] for e in ledger_events(actions)] == [
            "run", "action_filtered", "action_filtered"]

    def test_refused_item_exit1_actuator_untouched(self, tmp_path):
        actions, ppath, record = setup_env(tmp_path, [make_item(message_id="--discard")])
        assert run_cli(actions, ppath) == 1
        assert not record.exists()
        assert [e["type"] for e in ledger_events(actions)] == ["run", "action_refused"]

    def test_second_run_is_idempotent(self, tmp_path):
        actions, ppath, record = setup_env(tmp_path, [make_item()])
        assert run_cli(actions, ppath) == 0
        assert run_cli(actions, ppath) == 0  # retry stays green (I2)
        assert record.read_text() == "orders-dlq abc-123\n"  # exactly one write
        types = [e["type"] for e in ledger_events(actions)]
        assert types == ["run", "action_dispatch", "action_executed",
                         "run", "action_filtered"]

    def test_intra_proposal_duplicate_filtered(self, tmp_path):
        actions, ppath, record = setup_env(tmp_path, [make_item(), make_item()])
        assert run_cli(actions, ppath) == 0
        assert record.read_text() == "orders-dlq abc-123\n"  # dispatched once
        events = ledger_events(actions)
        assert [e["type"] for e in events] == [
            "run", "action_dispatch", "action_executed", "action_filtered"]
        # M-b: an intra-run duplicate uses a reason DISTINCT from the ledger
        # idempotency reason - the first instance may still later fail, so
        # "already executed (idempotency ledger)" would be a lie here.
        assert events[-1]["reason"] == "duplicate id within proposal"

    def test_actuator_failure_exit1_action_failed(self, tmp_path):
        actions, ppath, record = setup_env(tmp_path, [make_item(message_id="x1")])
        # Swap in a failing actuator: literal FAIL as the recorded first arg.
        fake = tmp_path / "fake_actuator.py"
        cfg = make_config(actuator=[sys.executable, str(fake),
                                    str(tmp_path / "record.txt"), "FAIL", "{message_id}"])
        (actions / "verbs" / "demo-replay.json").write_text(json.dumps(cfg))
        assert run_cli(actions, ppath) == 1
        events = ledger_events(actions)
        assert events[-1]["type"] == "action_failed" and events[-1]["exit_code"] == 3

    def test_failed_item_does_not_block_the_rest(self, tmp_path):
        actions, ppath, record = setup_env(tmp_path, [make_item(), make_item(message_id="n2")])
        fake = tmp_path / "fake_actuator.py"
        cfg = make_config(actuator=[sys.executable, str(fake), str(record),
                                    "{message_id}", "{queue}"])
        # First item's message_id "FAIL-1" triggers exit 3; second still runs.
        (actions / "verbs" / "demo-replay.json").write_text(json.dumps(cfg))
        items = [make_item(message_id="FAIL"), make_item(message_id="ok-2")]
        ppath.write_text(json.dumps({"proposal_format": 1, "verb": "demo-replay",
                                     "items": items}))
        assert run_cli(actions, ppath) == 1
        assert [e["type"] for e in ledger_events(actions)] == [
            "run", "action_dispatch", "action_failed",
            "action_dispatch", "action_executed"]

    def test_dry_run_no_ledger_no_dispatch(self, tmp_path, capsys):
        actions, ppath, record = setup_env(tmp_path, [make_item()])
        assert run_cli(actions, ppath, "--dry-run") == 0
        assert not record.exists()
        assert ledger_events(actions) == []
        assert "execute=1" in capsys.readouterr().out

    def test_dry_run_with_refused_exits1(self, tmp_path):
        actions, ppath, _ = setup_env(tmp_path, [make_item(message_id="../x")])
        assert run_cli(actions, ppath, "--dry-run") == 1


class TestCallAnomalies:
    def test_empty_id_key_exit2_nothing_runs(self, tmp_path):
        # A degenerate operator pattern (^.*$) + single-field id_template can
        # produce an empty id_key — a record invisible to BOTH the dedup set
        # and the unresolved-intent check (they filter on id_key truthiness).
        actions, ppath, record = setup_env(
            tmp_path, [make_item(message_id="")],
            cfg_over={"patterns": {"queue": "^[a-z0-9][a-z0-9-]*$",
                                   "message_id": "^.*$"},
                      "id_template": "{message_id}"})
        assert run_cli(actions, ppath) == 2
        assert not record.exists() and ledger_events(actions) == []

    def test_verb_mismatch_exit2(self, tmp_path, capsys):
        # I-4: bind the mismatch guard. other-verb gets its OWN valid config +
        # actuator, so exit 2 can come ONLY from the verb-mismatch guard - NOT
        # from config-missing. Deleting the guard lets the demo-replay proposal
        # dispatch through the WRONG verb's actuator (exit 0), failing this test.
        actions, ppath, record = setup_env(tmp_path, [make_item()])
        other_record = tmp_path / "other_record.txt"
        other_cfg = make_config(
            verb="other-verb",
            actuator=[sys.executable, str(tmp_path / "fake_actuator.py"),
                      str(other_record), "{queue}", "{message_id}"])
        (actions / "verbs" / "other-verb.json").write_text(json.dumps(other_cfg))
        capsys.readouterr()
        assert ax.main(["--verb", "other-verb", "--proposal", str(ppath),
                        "--actions-dir", str(actions)]) == 2
        assert not record.exists()        # demo-replay actuator never ran
        assert not other_record.exists()  # other-verb actuator never ran either
        assert ledger_events(actions) == []
        err = capsys.readouterr().err
        assert "other-verb" in err and "demo-replay" in err  # names the mismatch

    def test_empty_items_exit2_empty_ledger(self, tmp_path, capsys):
        # I-2: an empty items list must NOT be a vacuous exit-0 success; a broken
        # adapter emitting [] must be loud (exit 2), no ledger writes.
        actions, ppath, record = setup_env(tmp_path, [])
        capsys.readouterr()
        assert run_cli(actions, ppath) == 2
        assert not record.exists()
        assert ledger_events(actions) == []
        assert "zero items" in capsys.readouterr().err

    def test_unparseable_proposal_exit2(self, tmp_path):
        actions, ppath, _ = setup_env(tmp_path, [make_item()])
        ppath.write_text("{broken")
        assert run_cli(actions, ppath) == 2

    def test_missing_verb_config_exit2(self, tmp_path):
        actions, ppath, _ = setup_env(tmp_path, [make_item()])
        (actions / "verbs" / "demo-replay.json").unlink()
        assert run_cli(actions, ppath) == 2

    def test_unpatterned_field_config_exit2_nothing_runs(self, tmp_path):
        actions, ppath, record = setup_env(
            tmp_path, [make_item()], cfg_over={"patterns": {"queue": "^[a-z0-9-]+$"}})
        assert run_cli(actions, ppath) == 2
        assert not record.exists() and ledger_events(actions) == []

    def test_over_cap_exit2_nothing_executes(self, tmp_path):
        items = [make_item(message_id=f"m{i}") for i in range(3)]
        actions, ppath, record = setup_env(tmp_path, items, cfg_over={"max_items": 2})
        assert run_cli(actions, ppath) == 2
        assert not record.exists() and ledger_events(actions) == []

    def test_stale_proposal_exit2(self, tmp_path):
        actions, ppath, record = setup_env(
            tmp_path, [make_item()], cfg_over={"max_age_sec": 60})
        mtime = ppath.stat().st_mtime
        assert run_cli(actions, ppath, "--now", str(mtime + 61)) == 2
        assert run_cli(actions, ppath, "--now", str(mtime + 30)) == 0

    def test_lock_busy_exit2(self, tmp_path):
        import fcntl
        actions, ppath, record = setup_env(tmp_path, [make_item()])
        lock_path = actions / "ledger.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            assert run_cli(actions, ppath) == 2
            assert not record.exists()
        finally:
            os.close(fd)

    def test_corrupt_ledger_line_exit2_no_double_fire(self, tmp_path, capsys):
        # I-1: a truncated action_executed line must NOT silently drop from the
        # dedup set (silent double-fire). A non-blank undecodable line is a
        # structural anomaly: exit 2 before any dispatch, no second actuator fire.
        actions, ppath, record = setup_env(tmp_path, [make_item()])
        assert run_cli(actions, ppath) == 0
        first = record.read_text()
        ledger = actions / "ledger.jsonl"
        lines = ledger.read_text().splitlines()
        lines[-1] = '{"type": "action_execu'  # truncated last (action_executed) line
        ledger.write_text("\n".join(lines) + "\n")
        n_lines_before = len(ledger.read_text().splitlines())
        capsys.readouterr()
        assert run_cli(actions, ppath) == 2
        assert record.read_text() == first  # actuator did NOT fire a second time
        assert len(ledger.read_text().splitlines()) == n_lines_before  # no new events
        err = capsys.readouterr().err
        assert "ledger" in err and "line" in err

    def test_unresolved_dispatch_intent_exit2(self, tmp_path, capsys):
        # M-a: a crash between actuator success and the outcome append leaves a
        # dispatch intent with no action_executed/action_failed. On rerun that
        # is UNRESOLVED - the actuator may already have run, so re-firing is
        # unsafe: exit 2 naming the id_key, no dispatch.
        actions, ppath, record = setup_env(tmp_path, [make_item()])
        ax.ledger_append(actions, "action_dispatch", verb="demo-replay",
                         id_key="orders-dlq/abc-123", argv=["x"])
        capsys.readouterr()
        assert run_cli(actions, ppath) == 2
        assert not record.exists()  # actuator NOT fired
        assert "orders-dlq/abc-123" in capsys.readouterr().err

    def test_resolved_dispatch_intent_is_not_an_anomaly(self, tmp_path):
        # M-a boundary: an intent followed by its outcome is resolved - a normal
        # rerun stays idempotent (green), not exit 2.
        actions, ppath, record = setup_env(tmp_path, [make_item()])
        assert run_cli(actions, ppath) == 0
        assert run_cli(actions, ppath) == 0  # dispatch+executed both present

    def test_dispatch_intent_written_before_outcome(self, tmp_path):
        # M-a: the WAL intent (action_dispatch) precedes the outcome in the ledger.
        actions, ppath, record = setup_env(tmp_path, [make_item()])
        assert run_cli(actions, ppath) == 0
        types = [e["type"] for e in ledger_events(actions)]
        assert types == ["run", "action_dispatch", "action_executed"]

    def test_blank_ledger_lines_still_skipped(self, tmp_path):
        # I-1 boundary: blank lines are not anomalies.
        actions, ppath, record = setup_env(tmp_path, [make_item()])
        assert run_cli(actions, ppath) == 0
        ledger = actions / "ledger.jsonl"
        with ledger.open("a") as fh:
            fh.write("\n   \n")
        assert run_cli(actions, ppath) == 0  # rerun idempotent, blank lines ignored
