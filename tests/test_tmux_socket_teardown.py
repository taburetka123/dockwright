"""Teardown discipline for ephemeral test tmux servers.

Every throwaway server a test spawns must be killed AND have its socket file
removed from the tmux socket dir (/tmp/tmux-<uid> or $TMUX_TMPDIR/tmux-<uid>) —
tmux 3.7b does NOT unlink the socket on kill-server, so files otherwise pile up
run after run (observed: 2554 dead sockets). The conftest helpers under test
here are the single teardown path for all real-tmux fixtures."""
import os
import subprocess
from pathlib import Path

import pytest

from tests.conftest import (
    _leaked_test_sockets,
    _teardown_ephemeral_tmux,
    _tmux_socket_path,
)


def test_socket_path_honors_tmux_tmpdir(monkeypatch, tmp_path):
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    assert _tmux_socket_path("s1") == tmp_path / f"tmux-{os.getuid()}" / "s1"
    monkeypatch.delenv("TMUX_TMPDIR")
    assert _tmux_socket_path("s1") == Path("/tmp") / f"tmux-{os.getuid()}" / "s1"


def test_leak_net_detects_stale_socket(monkeypatch, tmp_path):
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    sock_dir = tmp_path / f"tmux-{os.getuid()}"
    assert _leaked_test_sockets() == []  # socket dir absent -> no findings
    sock_dir.mkdir()
    assert _leaked_test_sockets() == []  # empty dir -> no findings
    mine = sock_dir / f"wt-iso-{os.getpid()}-x0"
    legacy = sock_dir / f"dockwright-e2e-{os.getpid()}"
    other = sock_dir / "wt-iso-99999999-x0"  # another process's socket: ignored
    live = sock_dir / "dockwright"           # live fleet socket name: ignored
    for p in (mine, legacy, other, live):
        p.touch()
    assert _leaked_test_sockets() == sorted([legacy, mine])


@pytest.mark.real_tmux
def test_teardown_helper_kills_server_and_removes_socket(real_tmux):
    sock = real_tmux
    subprocess.run(["tmux", "-L", sock, "new-session", "-d", "-s", "x",
                    "--", "sleep", "30"], check=True)
    assert _tmux_socket_path(sock).exists()
    _teardown_ephemeral_tmux(sock)
    assert not _tmux_socket_path(sock).exists()
    # has-session must NOT autostart a server; rc != 0 proves the server died.
    probe = subprocess.run(["tmux", "-L", sock, "has-session", "-t", "x"],
                           capture_output=True)
    assert probe.returncode != 0
    assert not _tmux_socket_path(sock).exists()  # probe didn't recreate it
