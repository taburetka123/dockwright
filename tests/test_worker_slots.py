import json
import os
import threading
import time

import pytest

from dockwright import paths, state
from dockwright.mcp_server import (
    DEFAULT_SLOT_COUNTS,
    acquire_worker_slot_impl,
    lease_waits,
    register_self_impl,
    release_work_slots,
    release_worker_slot_impl,
)


@pytest.fixture
def fresh_orchestrator_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "ACTIVE", tmp_path / "active")
    monkeypatch.setattr(paths, "QUESTIONS", tmp_path / "questions")
    monkeypatch.setattr(paths, "ANSWERS", tmp_path / "answers")
    monkeypatch.setattr(paths, "DONE", tmp_path / "done")
    monkeypatch.setattr(paths, "CLOSED", tmp_path / "closed")
    monkeypatch.setattr(paths, "SLOTS", tmp_path / "slots")
    monkeypatch.setattr(paths, "ASSIGNMENTS", tmp_path / "assignments")
    monkeypatch.setattr(paths, "ASSIGNMENTS_PENDING", tmp_path / "assignments" / ".pending")
    paths.ensure_dirs()
    yield tmp_path


def _register_worker(sid: str, name: str = "w", pid: int | None = None) -> None:
    register_self_impl(
        claude_sid=sid,
        agent="worker",
        name=name,
        cwd="/tmp",
        iterm_sid="i",
        pid=pid if pid is not None else os.getpid(),
    )


def test_acquire_worker_slot_succeeds_under_cap(fresh_orchestrator_dir):
    _register_worker("sid-A", name="A")
    _register_worker("sid-B", name="B")
    r1 = acquire_worker_slot_impl(claude_sid="sid-A", category="mvn", max_concurrent=3)
    r2 = acquire_worker_slot_impl(claude_sid="sid-B", category="mvn", max_concurrent=3)
    assert "slot_id" in r1 and "slot_id" in r2
    assert r1["slot_id"] != r2["slot_id"]


def test_acquire_worker_slot_blocks_at_cap(fresh_orchestrator_dir):
    for n in ("A", "B", "C"):
        _register_worker(f"sid-{n}", name=n)
        acquire_worker_slot_impl(claude_sid=f"sid-{n}", category="mvn", max_concurrent=3)
    _register_worker("sid-D", name="D")
    with pytest.raises(TimeoutError):
        acquire_worker_slot_impl(
            claude_sid="sid-D", category="mvn", max_concurrent=3, timeout_sec=1
        )


def test_release_worker_slot_frees_one(fresh_orchestrator_dir):
    slot_ids = []
    for n in ("A", "B", "C"):
        _register_worker(f"sid-{n}", name=n)
        slot_ids.append(
            acquire_worker_slot_impl(
                claude_sid=f"sid-{n}", category="mvn", max_concurrent=3
            )["slot_id"]
        )
    release_worker_slot_impl(slot_id=slot_ids[1])
    _register_worker("sid-D", name="D")
    result = acquire_worker_slot_impl(
        claude_sid="sid-D", category="mvn", max_concurrent=3, timeout_sec=2
    )
    assert "slot_id" in result


def test_release_worker_slot_idempotent(fresh_orchestrator_dir):
    _register_worker("sid-A", name="A")
    slot = acquire_worker_slot_impl(claude_sid="sid-A", category="mvn", max_concurrent=3)
    r1 = release_worker_slot_impl(slot_id=slot["slot_id"])
    r2 = release_worker_slot_impl(slot_id=slot["slot_id"])
    assert r1["released"] is True
    assert r2["released"] is True
    assert "sid-A" not in (paths.SLOTS / "mvn.json").read_text()


def test_acquire_evicts_stale_holders(fresh_orchestrator_dir):
    import json
    (paths.SLOTS).mkdir(parents=True, exist_ok=True)
    (paths.SLOTS / "mvn.json").write_text(json.dumps({
        "max_concurrent": 1,
        "holders": [{
            "slot_id": "stale-1",
            "claude_sid": "ghost-sid",
            "acquired_at": 0.0,
            "pid": 999999,
        }],
    }))
    _register_worker("sid-A", name="A")
    result = acquire_worker_slot_impl(
        claude_sid="sid-A", category="mvn", max_concurrent=1, timeout_sec=2
    )
    assert "slot_id" in result and result["slot_id"] != "stale-1"


def test_acquire_evicts_a_holder_whose_pid_is_unknown(fresh_orchestrator_dir):
    _register_worker("ghost-sid", name="ghost")
    (paths.SLOTS).mkdir(parents=True, exist_ok=True)
    (paths.SLOTS / "mvn.json").write_text(json.dumps({
        "max_concurrent": 1,
        "holders": [{
            "slot_id": "pidless-1",
            "claude_sid": "ghost-sid",
            "acquired_at": 0.0,
        }],
    }))
    _register_worker("sid-A", name="A")
    result = acquire_worker_slot_impl(
        claude_sid="sid-A", category="mvn", max_concurrent=1, timeout_sec=2
    )
    assert result["slot_id"] != "pidless-1"
    holders = json.loads((paths.SLOTS / "mvn.json").read_text())["holders"]
    assert [h["slot_id"] for h in holders] == [result["slot_id"]]


def test_env_var_overrides_default_count(fresh_orchestrator_dir, monkeypatch):
    monkeypatch.setenv("CLAUDE_ORCH_SLOTS_MVN", "2")
    for n in range(2):
        _register_worker(f"sid-{n}", name=f"W{n}")
        acquire_worker_slot_impl(claude_sid=f"sid-{n}", category="mvn")
    _register_worker("sid-X", name="X")
    with pytest.raises(TimeoutError):
        acquire_worker_slot_impl(claude_sid="sid-X", category="mvn", timeout_sec=1)


def test_concurrent_acquires_serialize_safely(fresh_orchestrator_dir):
    import threading
    _register_worker("sid-A", name="A")
    _register_worker("sid-B", name="B")
    results: list = []
    errors: list = []

    def grab(sid):
        try:
            results.append(
                acquire_worker_slot_impl(
                    claude_sid=sid, category="mvn", max_concurrent=2, timeout_sec=6
                )
            )
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=grab, args=("sid-A",))
    t2 = threading.Thread(target=grab, args=("sid-B",))
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert not errors
    assert len(results) == 2
    ids = {r["slot_id"] for r in results}
    assert len(ids) == 2


def test_default_path_resolves_the_cap_from_the_constant(
    fresh_orchestrator_dir, monkeypatch
):
    monkeypatch.delenv("CLAUDE_ORCH_SLOTS_MVN", raising=False)
    cap = DEFAULT_SLOT_COUNTS["mvn"]
    for n in range(cap):
        _register_worker(f"sid-{n}", name=f"W{n}")
        acquire_worker_slot_impl(claude_sid=f"sid-{n}", category="mvn")
    _register_worker("sid-over", name="over")
    with pytest.raises(TimeoutError):
        acquire_worker_slot_impl(
            claude_sid="sid-over", category="mvn", timeout_sec=1)


LEASE = "env:example/svc-a"


def test_a_category_with_a_slash_is_one_flat_file_that_release_finds(fresh_orchestrator_dir):
    _register_worker("sid-A", name="A")
    r = acquire_worker_slot_impl(claude_sid="sid-A", category=LEASE, max_concurrent=1, task_key="K", node="ship",
                                 wait=False)
    assert r["acquired"] is True and r["category"] == LEASE and r["renewed"] is False
    files = [p.name for p in paths.SLOTS.iterdir() if p.suffix == ".json"]
    assert files == ["env%3Aexample%2Fsvc-a.json"] and not (paths.SLOTS / "env:example").exists()
    assert json.loads((paths.SLOTS / files[0]).read_text())["category"] == LEASE
    rel = release_worker_slot_impl(r["slot_id"])
    assert rel == {"released": True, "slot_id": r["slot_id"], "found": True, "category": LEASE}
    _register_worker("sid-B", name="B")
    assert acquire_worker_slot_impl(claude_sid="sid-B", category=LEASE, max_concurrent=1, task_key="L",
                                    wait=False)["acquired"] is True


def test_a_lease_survives_a_missing_active_record_and_a_dead_pid(fresh_orchestrator_dir):
    held = acquire_worker_slot_impl(claude_sid="sid-engineer", category=LEASE, max_concurrent=1, wait=False)
    assert held["acquired"] is True and not (paths.ACTIVE / "sid-engineer.json").exists()
    slot_file = next(p for p in paths.SLOTS.iterdir() if p.suffix == ".json")
    data = json.loads(slot_file.read_text())
    data["holders"][0]["pid"] = 999999
    state.write_json_atomic(slot_file, data)
    _register_worker("sid-B", name="B")
    r = acquire_worker_slot_impl(claude_sid="sid-B", category=LEASE, max_concurrent=1, task_key="L", node="post-merge",
                                 wait=False)
    assert r["acquired"] is False
    assert [(h["owner"], h["task_key"], h["node"]) for h in r["holders"]] == [("session:sid-engineer", None, None)]


def test_a_repeat_call_by_the_same_owner_renews_the_lease(fresh_orchestrator_dir, monkeypatch):
    _register_worker("sid-A", name="A")
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    first = acquire_worker_slot_impl(claude_sid="sid-A", category=LEASE, max_concurrent=1, task_key="K", node="ship",
                                     wait=False)
    monkeypatch.setattr(time, "time", lambda: 2000.0)
    again = acquire_worker_slot_impl(claude_sid="sid-A2", category=LEASE, max_concurrent=1, task_key="K", node="ship",
                                     wait=False)
    assert again["acquired"] is True and again["slot_id"] == first["slot_id"] and again["renewed"] is True
    assert again["expires_at"] == 2000.0 + 7200 and first["expires_at"] == 1000.0 + 7200
    session = acquire_worker_slot_impl(claude_sid="sid-engineer", category="env:example/svc-b", max_concurrent=1,
                                       wait=False)
    repeat = acquire_worker_slot_impl(claude_sid="sid-engineer", category="env:example/svc-b", max_concurrent=1,
                                      wait=False)
    assert repeat["acquired"] is True and repeat["slot_id"] == session["slot_id"] and repeat["renewed"] is True


def test_a_lease_expires_by_expires_at(fresh_orchestrator_dir, monkeypatch):
    _register_worker("sid-A", name="A")
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    acquire_worker_slot_impl(claude_sid="sid-A", category=LEASE, max_concurrent=1, task_key="K", wait=False,
                             ttl_sec=60)
    _register_worker("sid-B", name="B")
    monkeypatch.setattr(time, "time", lambda: 1059.0)
    assert acquire_worker_slot_impl(claude_sid="sid-B", category=LEASE, max_concurrent=1, task_key="L", node="ship",
                                    wait=False)["acquired"] is False
    monkeypatch.setattr(time, "time", lambda: 1061.0)
    assert acquire_worker_slot_impl(claude_sid="sid-B", category=LEASE, max_concurrent=1, task_key="L",
                                    wait=False)["acquired"] is True


def test_a_held_lease_registers_a_waiter_only_for_a_work(fresh_orchestrator_dir):
    _register_worker("sid-A", name="A")
    acquire_worker_slot_impl(claude_sid="sid-A", category=LEASE, max_concurrent=1, task_key="K", node="ship", wait=False)
    _register_worker("sid-B", name="B")
    r = acquire_worker_slot_impl(claude_sid="sid-B", category=LEASE, max_concurrent=1, task_key="L", node="post-merge",
                                 wait=False)
    assert r["acquired"] is False and [h["task_key"] for h in r["holders"]] == ["K"]
    acquire_worker_slot_impl(claude_sid="sid-B", category=LEASE, max_concurrent=1, task_key="L", node="post-merge",
                             wait=False)
    assert [(w["category"], w["node"], w["free"]) for w in lease_waits("L")] == [(LEASE, "post-merge", False)]
    assert acquire_worker_slot_impl(claude_sid="sid-engineer", category=LEASE, max_concurrent=1,
                                    wait=False)["acquired"] is False
    waiters = json.loads(next(p for p in paths.SLOTS.iterdir() if p.suffix == ".json").read_text())["waiters"]
    assert [(w["task_key"], w["node"]) for w in waiters] == [("L", "post-merge")]
    release_worker_slot_impl(acquire_worker_slot_impl(claude_sid="sid-A", category=LEASE, max_concurrent=1,
                                                      task_key="K", node="ship", wait=False)["slot_id"])
    assert lease_waits("L")[0]["free"] is True
    won = acquire_worker_slot_impl(claude_sid="sid-B", category=LEASE, max_concurrent=1, task_key="L",
                                   node="post-merge", wait=False)
    assert won["acquired"] is True and lease_waits("L") == []


def test_release_work_slots_drops_the_work_s_holders_and_waits_everywhere(fresh_orchestrator_dir):
    _register_worker("sid-A", name="A")
    acquire_worker_slot_impl(claude_sid="sid-A", category=LEASE, max_concurrent=1, task_key="K", node="ship", wait=False)
    acquire_worker_slot_impl(claude_sid="sid-A", category="env:example/svc-b", max_concurrent=1, task_key="L",
                             wait=False)
    _register_worker("sid-B", name="B")
    acquire_worker_slot_impl(claude_sid="sid-B", category="env:example/svc-b", max_concurrent=1, task_key="K",
                             node="post-merge", wait=False)
    r = release_work_slots("K")
    assert [x["category"] for x in r["released"]] == [LEASE] and r["waits_removed"] == 1
    assert lease_waits("K") == []
    assert acquire_worker_slot_impl(claude_sid="sid-B", category=LEASE, max_concurrent=1, task_key="L",
                                    wait=False)["acquired"] is True
    assert acquire_worker_slot_impl(claude_sid="sid-B", category="env:example/svc-b", max_concurrent=1, task_key="M",
                                    node="ship", wait=False)["acquired"] is False


def test_a_plain_slot_keeps_its_shape(fresh_orchestrator_dir):
    _register_worker("sid-A", name="A")
    r = acquire_worker_slot_impl(claude_sid="sid-A", category="mvn", max_concurrent=2)
    assert set(r) == {"slot_id", "category", "max_concurrent", "holders_count"}
    holder = json.loads((paths.SLOTS / "mvn.json").read_text())["holders"][0]
    assert holder["owner"] is None and holder["expires_at"] is None


def test_a_work_s_lease_wait_without_a_node_is_refused_and_registers_no_waiter(fresh_orchestrator_dir):
    _register_worker("sid-A", name="A")
    acquire_worker_slot_impl(claude_sid="sid-A", category=LEASE, max_concurrent=1, task_key="K", node="ship", wait=False)
    _register_worker("sid-B", name="B")
    with pytest.raises(ValueError, match="node"):
        acquire_worker_slot_impl(claude_sid="sid-B", category=LEASE, max_concurrent=1, task_key="L", wait=False)
    assert lease_waits("L") == []
    assert acquire_worker_slot_impl(claude_sid="sid-B", category=LEASE, max_concurrent=1, task_key="L", node="ship",
                                    wait=False)["acquired"] is False
    assert acquire_worker_slot_impl(claude_sid="sid-C", category="env:example/svc-b", max_concurrent=1, task_key="L",
                                    wait=False)["acquired"] is True


def test_a_renewal_by_another_node_of_the_work_records_that_node(fresh_orchestrator_dir):
    _register_worker("sid-A", name="A")
    acquire_worker_slot_impl(claude_sid="sid-A", category=LEASE, max_concurrent=1, task_key="K", node="ship", wait=False)
    again = acquire_worker_slot_impl(claude_sid="sid-A", category=LEASE, max_concurrent=1, task_key="K",
                                     node="post-merge", wait=False)
    assert again["renewed"] is True
    _register_worker("sid-B", name="B")
    r = acquire_worker_slot_impl(claude_sid="sid-B", category=LEASE, max_concurrent=1, task_key="L", node="ship",
                                 wait=False)
    assert [h["node"] for h in r["holders"]] == ["post-merge"]
