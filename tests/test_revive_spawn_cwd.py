import asyncio

import pytest

from dockwright import revive, spawner


def _candidate(record):
    return revive.Candidate(record=record, sid="g1", name="alpha", domain="general",
                            verdict="revive", reason="")


def test_spawn_manager_window_pins_cwd_to_manager_home(monkeypatch, tmp_path):
    mgr_home = tmp_path / "mgr-home"
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(mgr_home))
    captured = {}

    async def fake_spawn(**kwargs):
        captured.update(kwargs)
        return ("%1", kwargs.get("name", ""))

    monkeypatch.setattr(spawner, "spawn_worker_tab", fake_spawn)
    record = {"claude_sid": "g1", "agent": "manager", "cwd": str(tmp_path)}
    for resume in (False, True):
        captured.clear()
        asyncio.run(revive.spawn_manager_window(_candidate(record), resume=resume))
        assert captured["cwd"] == str(mgr_home)
        assert captured["agent"] == "manager"
    assert mgr_home.is_dir()


def test_spawn_manager_window_raises_when_manager_home_cannot_be_created(monkeypatch, tmp_path):
    parent_file = tmp_path / "afile"
    parent_file.write_text("x")
    monkeypatch.setenv("DOCKWRIGHT_MANAGER_HOME", str(parent_file / "manager"))
    spawned = []

    async def fake_spawn(**kwargs):
        spawned.append(kwargs)
        return ("%1", "")

    monkeypatch.setattr(spawner, "spawn_worker_tab", fake_spawn)
    with pytest.raises(ValueError, match="could not be created"):
        asyncio.run(revive.spawn_manager_window(_candidate({"cwd": str(tmp_path)})))
    assert spawned == []
