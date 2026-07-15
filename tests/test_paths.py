import pytest
from pathlib import Path
from dockwright import paths

def test_root_under_dot_claude(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    # Re-import to pick up env change
    import importlib
    importlib.reload(paths)
    assert paths.ROOT == tmp_path / ".claude" / "dockwright"
    assert paths.ACTIVE == paths.ROOT / "active"
    assert paths.QUESTIONS == paths.ROOT / "questions"
    assert paths.ANSWERS == paths.ROOT / "answers"
    assert paths.DONE == paths.ROOT / "done"
    assert paths.HANDOFFS == paths.ROOT / "handoffs"
    assert paths.MANAGER_TRIGGERS_LOG == paths.ROOT / "manager-triggers.jsonl"

def test_ensure_dirs_creates_all(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    import importlib
    importlib.reload(paths)
    paths.ensure_dirs()
    assert paths.ACTIVE.is_dir()
    assert paths.QUESTIONS.is_dir()
    assert paths.ANSWERS.is_dir()
    assert paths.DONE.is_dir()

def test_handoffs_dir_created_by_ensure_dirs(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    import importlib
    importlib.reload(paths)
    paths.ensure_dirs()
    assert paths.HANDOFFS.is_dir()


def test_question_dir_for_scopes_parent_and_keeps_legacy_flat():
    assert paths.question_dir_for(None) == paths.QUESTIONS
    assert paths.question_dir_for("manager-a") == paths.QUESTIONS / "manager-a"
    assert paths.question_dir_for("mgr/a") == paths.QUESTIONS / "mgr_a"


def test_safe_segment_sanitizes_and_rejects():
    assert paths._safe_segment("TKT-SANDBOX-8353") == "TKT-SANDBOX-8353"
    assert paths._safe_segment("a b/c.d") == "a_b_c_d"
    # Reject set is empty/whitespace only: dots sanitize to underscores, so the
    # spec §5 "."/".." rejects are unreachable via the regex — they guard direct misuse.
    for bad in ("", "   "):
        with pytest.raises(ValueError):
            paths._safe_segment(bad)


def test_artifact_paths_layout():
    d = paths.artifact_ticket_dir("TKT-SANDBOX-1")
    assert d == paths.ARTIFACTS / "TKT-SANDBOX-1"
    assert paths.artifact_path("TKT-SANDBOX-1", "spec", "srs") == d / "spec.srs.md"
    assert paths.artifact_events_path("TKT-SANDBOX-1") == d / "events.jsonl"


def test_assignment_paths_layout():
    assert paths.assignment_path("sid-1") == paths.ASSIGNMENTS / "sid-1.json"
    assert paths.pending_assignment_path("abc123") == paths.ASSIGNMENTS_PENDING / "abc123.json"


def test_ensure_dirs_creates_artifact_planes(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    import importlib
    importlib.reload(paths)
    paths.ensure_dirs()
    assert paths.ARTIFACTS.is_dir()
    assert paths.ASSIGNMENTS.is_dir()
    assert paths.ASSIGNMENTS_PENDING.is_dir()


def test_assignment_env_key_in_strip_list():
    assert "CLAUDE_ASSIGNMENT_ID" in paths.ORCHESTRATOR_ENV_KEYS


def test_account_active_under_root():
    assert paths.ACCOUNT_ACTIVE == paths.ROOT / "account-active"


def test_orch_account_env_key_registered():
    assert "CLAUDE_ORCH_ACCOUNT" in paths.ORCHESTRATOR_ENV_KEYS


def test_worker_home_default_under_projects_work(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_ORCH_WORKER_HOME", raising=False)
    assert paths.worker_home() == tmp_path / "projects" / "work" / "worker"

def test_worker_home_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_HOME", "/custom/worker/home")
    assert paths.worker_home() == Path("/custom/worker/home")

def test_worker_home_blank_env_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_ORCH_WORKER_HOME", "   ")
    assert paths.worker_home() == tmp_path / "projects" / "work" / "worker"


def test_account_usage_path_uses_account_usage_dir(monkeypatch, tmp_path):
    from dockwright import paths
    monkeypatch.setattr(paths, "ACCOUNT_USAGE", tmp_path / "usage")
    assert paths.account_usage_path("b") == tmp_path / "usage" / "b.json"


def test_account_usage_not_in_ensure_dirs(tmp_path, monkeypatch):
    # ACCOUNT_USAGE is lazy (statusline mkdir -p's it); ensure_dirs must NOT create it.
    from dockwright import paths
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    for attr in ("ACTIVE","QUESTIONS","ANSWERS","DONE","CLOSED","HANDOFFS",
                 "TURN_ENDS","PRESETS","SLOTS","ARCHITECT","ARTIFACTS","ASSIGNMENTS",
                 "ASSIGNMENTS_PENDING"):
        monkeypatch.setattr(paths, attr, tmp_path / attr.lower())
    monkeypatch.setattr(paths, "ACCOUNT_USAGE", tmp_path / "usage")
    monkeypatch.setattr(paths, "MANAGER_MEMORY", tmp_path / "mm")
    monkeypatch.setattr(paths, "UNSCOPED_BUCKET", "_unscoped")
    paths.ensure_dirs()
    assert not (tmp_path / "usage").exists()


def test_tmux_conf_under_root():
    import importlib
    fresh = importlib.reload(paths)
    assert fresh.TMUX_CONF == fresh.ROOT / "dockwright.tmux.conf"

def test_tmux_conf_legacy_under_root():
    import importlib
    fresh = importlib.reload(paths)
    assert fresh.TMUX_CONF_LEGACY == fresh.ROOT / "claude-orch.tmux.conf"

def test_tmux_conf_not_in_ensure_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    for attr in ("ACTIVE","QUESTIONS","ANSWERS","DONE","CLOSED","HANDOFFS",
                 "TURN_ENDS","PRESETS","SLOTS","ARCHITECT","ARTIFACTS","ASSIGNMENTS",
                 "ASSIGNMENTS_PENDING"):
        monkeypatch.setattr(paths, attr, tmp_path / attr.lower())
    monkeypatch.setattr(paths, "MANAGER_MEMORY", tmp_path / "mm")
    monkeypatch.setattr(paths, "TMUX_CONF", tmp_path / "dockwright.tmux.conf")
    monkeypatch.setattr(paths, "TMUX_CONF_LEGACY", tmp_path / "claude-orch.tmux.conf")  # NEW
    monkeypatch.setattr(paths, "UNSCOPED_BUCKET", "_unscoped")
    paths.ensure_dirs()
    assert not (tmp_path / "dockwright.tmux.conf").exists()
    assert not (tmp_path / "claude-orch.tmux.conf").exists()  # NEW
