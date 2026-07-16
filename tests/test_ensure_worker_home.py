import os
from pathlib import Path

import pytest

from dockwright import paths
from dockwright import ensure_worker_home as ewh


def test_ensure_worker_home_creates_absent_dir(monkeypatch, tmp_path):
    home = tmp_path / "projects" / "work" / "worker"
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_HOME", str(home))
    assert not home.exists()
    result = paths.ensure_worker_home()
    assert result == home
    assert home.is_dir()


def test_ensure_worker_home_idempotent_on_existing(monkeypatch, tmp_path):
    home = tmp_path / "wh"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_HOME", str(home))
    assert paths.ensure_worker_home() == home
    assert home.is_dir()


def test_ensure_worker_home_failopen_when_mkdir_raises(monkeypatch, tmp_path):
    # Parent is a regular file → mkdir(parents=True) raises OSError; swallowed.
    parent_file = tmp_path / "afile"
    parent_file.write_text("x")
    home = parent_file / "worker"
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_HOME", str(home))
    result = paths.ensure_worker_home()   # must NOT raise
    assert result == home
    assert not home.is_dir()


def test_cli_prints_created_path(monkeypatch, tmp_path, capsys):
    home = tmp_path / "wh-cli"
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_HOME", str(home))
    rc = ewh.main([])
    assert rc == 0
    assert capsys.readouterr().out.strip() == str(home)
    assert home.is_dir()
