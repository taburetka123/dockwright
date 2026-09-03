import json
import os
import threading

import pytest

from dockwright import paths, state
from dockwright.mcp_server import (
    DEFAULT_SLOT_COUNTS,
    acquire_worker_slot_impl,
    register_self_impl,
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
