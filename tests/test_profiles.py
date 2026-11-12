import subprocess
from pathlib import Path

import pytest

from dockwright import profiles


@pytest.fixture
def settings(tmp_path):
    return {"sets": {"none": [], "common": ["acme"], "back": ["acme", "acme-be"], "front": ["acme", "acme-fe"],
                     "union": ["acme", "acme-be", "acme-fe"]},
            "repos": {"acme-web": "front", "worker": "union", "manager": "common"},
            "work_roots": [str(tmp_path / "work"), str(tmp_path / "worktrees")], "work_default": "back"}


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_profile_by_repo_table_wins(tmp_path, settings):
    repo = _repo(tmp_path / "work" / "acme-web")
    assert profiles.profile_for(repo, settings) == "front"
    (repo / "src").mkdir()
    assert profiles.profile_for(repo / "src", settings) == "front"


def test_a_worktree_under_a_work_root_is_named_by_its_repo_or_the_default(tmp_path, settings):
    assert profiles.profile_for(_repo(tmp_path / "worktrees" / "T-1" / "acme-web"), settings) == "front"
    assert profiles.profile_for(_repo(tmp_path / "work" / "acme-api"), settings) == "back"


def test_homes_and_personal_paths(tmp_path, settings):
    (tmp_path / "work" / "worker").mkdir(parents=True)
    (tmp_path / "work" / "manager").mkdir(parents=True)
    assert profiles.profile_for(tmp_path / "work" / "worker", settings) == "union"
    assert profiles.profile_for(tmp_path / "work" / "manager", settings) == "common"
    assert profiles.profile_for(_repo(tmp_path / "personal" / "tool"), settings) == "none"


def test_a_personal_owner_under_a_work_root_is_none(tmp_path, settings):
    repo = _repo(tmp_path / "worktrees" / "T-2" / "tool")
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:acme-person/tool.git"], check=True)
    assert profiles.profile_for(repo, {**settings, "personal_owners": ["acme-person"]}) == "none"
    assert profiles.profile_for(repo, settings) == "back"


def test_no_sets_means_none(tmp_path):
    assert profiles.profile_for(tmp_path, {"sets": {}, "repos": {}, "work_roots": [], "work_default": "back"}) == "none"
