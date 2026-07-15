from pathlib import Path
import os
import pytest
from dockwright import migrate


def _mk(claude: Path, rel: str, content: str = "x") -> Path:
    p = claude / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


@pytest.fixture
def claude(tmp_path, monkeypatch):
    d = tmp_path / ".claude"
    d.mkdir()
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(tmp_path / "absent.toml"))
    return d


def test_fresh_home_is_noop(claude):
    lines = migrate.run(claude)
    assert not (claude / "dockwright").exists()
    assert all("skip" in l or "absent" in l for l in lines)


def test_root_moves_and_symlinks(claude):
    _mk(claude, "orchestrator/active/s1.json")
    migrate.run(claude)
    assert (claude / "dockwright/active/s1.json").is_file()
    legacy = claude / "orchestrator"
    assert legacy.is_symlink()
    assert os.readlink(legacy) == "dockwright"
    assert (legacy / "active/s1.json").is_file()  # resolves through link


def test_all_rows_migrate(claude):
    _mk(claude, "orchestrator/account-active", "a")
    _mk(claude, "manager-memory/general/j.md")
    _mk(claude, "selffix-findings/f1.md")
    _mk(claude, "selffix-retry/r1.json")
    _mk(claude, "selffix-trigger.log", "log")
    _mk(claude, "gardener/ledger.jsonl")
    _mk(claude, "bootlite/state.json")
    _mk(claude, "worktree-prune/ledger.jsonl")
    _mk(claude, "loops-registry.md", "reg")
    _mk(claude, ".orchestrator-deploy", "sha=x")
    _mk(claude, "orchestrator-overlay/agent_vars.md")
    migrate.run(claude)
    assert (claude / "dockwright/manager-memory/general/j.md").is_file()
    assert (claude / "dockwright/selffix/findings/f1.md").is_file()
    assert (claude / "dockwright/selffix/retry/r1.json").is_file()
    assert (claude / "dockwright/selffix/trigger.log").read_text() == "log"
    assert (claude / "dockwright/gardener/ledger.jsonl").is_file()
    assert (claude / "dockwright/bootlite/state.json").is_file()
    assert (claude / "dockwright/worktree-prune/ledger.jsonl").is_file()
    assert (claude / "dockwright/loops-registry.md").read_text() == "reg"
    assert (claude / "dockwright/.deploy-stamp").read_text() == "sha=x"
    assert (claude / "dockwright-overlay/agent_vars.md").is_file()
    for legacy in ("manager-memory", "selffix-findings", "selffix-retry",
                   "selffix-trigger.log", "gardener", "bootlite",
                   "worktree-prune", "loops-registry.md",
                   ".orchestrator-deploy", "orchestrator-overlay"):
        assert (claude / legacy).is_symlink(), legacy
    # relative target pinned for a NESTED row, not just row 1
    assert os.readlink(claude / "manager-memory") == "dockwright/manager-memory"


def test_idempotent_second_run(claude):
    _mk(claude, "orchestrator/active/s1.json")
    migrate.run(claude)
    lines = migrate.run(claude)
    assert (claude / "dockwright/active/s1.json").is_file()
    assert (claude / "orchestrator").is_symlink()
    assert all("error" not in l.lower() for l in lines)


def test_new_preexists_real_dir_merges(claude):
    _mk(claude, "orchestrator/active/s1.json")
    _mk(claude, "dockwright/manager-memory/keep.md")  # ensure_dirs artifact
    _mk(claude, "manager-memory/old.md")
    migrate.run(claude)
    assert (claude / "dockwright/manager-memory/keep.md").is_file()
    assert (claude / "dockwright/manager-memory/old.md").is_file()
    assert (claude / "manager-memory").is_symlink()


def test_file_collision_aborts_loudly(claude):
    _mk(claude, "loops-registry.md", "old")
    _mk(claude, "dockwright/loops-registry.md", "new")
    with pytest.raises(migrate.MigrationError):
        migrate.run(claude)
    assert (claude / "loops-registry.md").read_text() == "old"  # untouched


def test_stop_files_absent_no_symlink(claude):
    _mk(claude, "orchestrator/account-active", "a")
    migrate.run(claude)
    assert not (claude / "gardener-stop").exists()
    assert not (claude / "gardener-stop").is_symlink()


def test_legacy_toml_pin_aborts(claude, tmp_path, monkeypatch):
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text('[paths]\nstate_root = "~/.claude/orchestrator"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    _mk(claude, "orchestrator/active/s1.json")
    with pytest.raises(migrate.MigrationError):
        migrate.run(claude)


def test_dry_run_touches_nothing(claude):
    _mk(claude, "orchestrator/active/s1.json")
    migrate.run(claude, dry_run=True)
    assert (claude / "orchestrator").is_dir()
    assert not (claude / "orchestrator").is_symlink()
    assert not (claude / "dockwright").exists()


# --- hardening beyond the brief -------------------------------------------


def test_nested_file_collision_aborts_with_both_intact(claude):
    """A collision deep inside a dir merge aborts loudly and neither side's
    file content is touched."""
    _mk(claude, "manager-memory/general/j.md", "old")
    _mk(claude, "dockwright/manager-memory/general/j.md", "new")
    with pytest.raises(migrate.MigrationError):
        migrate.run(claude)
    assert (claude / "manager-memory/general/j.md").read_text() == "old"
    assert (claude / "dockwright/manager-memory/general/j.md").read_text() == "new"


def test_merge_never_writes_through_dst_symlink(claude):
    """A symlink at the merge destination is a collision, never followed —
    following it would silently relocate state to wherever it points."""
    elsewhere = claude / "elsewhere"
    elsewhere.mkdir()
    _mk(claude, "manager-memory/sub/old.md", "old")
    (claude / "dockwright/manager-memory").mkdir(parents=True)
    os.symlink(str(elsewhere), claude / "dockwright/manager-memory/sub")
    with pytest.raises(migrate.MigrationError):
        migrate.run(claude)
    assert (claude / "manager-memory/sub/old.md").read_text() == "old"
    assert not (elsewhere / "old.md").exists()


def test_symlink_child_is_moved_as_link_not_followed(claude):
    """A child that is itself a symlink is renamed as-is; an intra-tree
    relative link keeps resolving at the new home."""
    _mk(claude, "gardener/data/ledger.jsonl", "L")
    os.symlink("data", claude / "gardener/link")
    _mk(claude, "dockwright/gardener/keep.md")  # force the merge path
    migrate.run(claude)
    moved = claude / "dockwright/gardener/link"
    assert moved.is_symlink()
    assert os.readlink(moved) == "data"
    assert (moved / "ledger.jsonl").read_text() == "L"


def test_reborn_legacy_dir_is_merged_and_symlinked(claude, monkeypatch):
    """A live poller's mkdir re-creating the legacy dir between rename and
    symlink (EEXIST) gets folded into the new home and the symlink lands."""
    _mk(claude, "orchestrator/active/s1.json")
    real_symlink = os.symlink
    reborn = {"done": False}

    def racy_symlink(target, link, *a, **kw):
        if not reborn["done"] and str(link).endswith("orchestrator"):
            reborn["done"] = True
            (claude / "orchestrator/questions").mkdir(parents=True)
            (claude / "orchestrator/questions/q1.json").write_text("q")
        return real_symlink(target, link, *a, **kw)

    monkeypatch.setattr(os, "symlink", racy_symlink)
    migrate.run(claude)
    assert (claude / "dockwright/active/s1.json").is_file()
    assert (claude / "dockwright/questions/q1.json").read_text() == "q"
    assert (claude / "orchestrator").is_symlink()


def test_symlink_retry_exhaustion_fails_loudly_with_state_safe(claude, monkeypatch):
    """If the legacy dir keeps being reborn past the retry bound, migration
    fails loudly and everything already moved is intact at the new home."""
    _mk(claude, "orchestrator/active/s1.json")

    def always_reborn_symlink(target, link, *a, **kw):
        Path(link).mkdir(parents=True, exist_ok=True)
        raise FileExistsError(17, "File exists", str(link))

    monkeypatch.setattr(os, "symlink", always_reborn_symlink)
    with pytest.raises(migrate.MigrationError):
        migrate.run(claude)
    assert (claude / "dockwright/active/s1.json").is_file()


def test_legacy_pin_via_absolute_path_aborts(claude, tmp_path, monkeypatch):
    """The pin assert catches an expanded absolute path, not just the ~ form."""
    abs_legacy = os.path.expanduser("~/.claude/manager-memory")
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text(f'[paths]\nmanager_memory = "{abs_legacy}/"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    _mk(claude, "manager-memory/j.md")
    with pytest.raises(migrate.MigrationError):
        migrate.run(claude)


def test_wrong_symlink_at_legacy_aborts(claude):
    (claude / "somewhere-else").mkdir()
    os.symlink("somewhere-else", claude / "orchestrator")
    with pytest.raises(migrate.MigrationError):
        migrate.run(claude)


def test_dry_run_previews_collision(claude):
    """Dry-run predicts the loud abort a real run would hit, still touching
    nothing."""
    _mk(claude, "loops-registry.md", "old")
    _mk(claude, "dockwright/loops-registry.md", "new")
    lines = migrate.run(claude, dry_run=True)
    assert any("would-FAIL" in l for l in lines)
    assert (claude / "loops-registry.md").read_text() == "old"
    assert (claude / "dockwright/loops-registry.md").read_text() == "new"


def test_crash_residue_new_only_gets_compat_symlink(claude):
    """Crash between rename and symlink leaves new populated + legacy absent;
    a re-run must place the compat link — old deployed code hardcodes legacy
    paths and would otherwise fork state at the cold path for weeks."""
    _mk(claude, "dockwright/manager-memory/j.md")
    lines = migrate.run(claude)
    mm = claude / "manager-memory"
    assert mm.is_symlink()
    assert os.readlink(mm) == "dockwright/manager-memory"
    assert (mm / "j.md").is_file()
    assert any(l.startswith("linked") and "manager-memory" in l for l in lines)


def test_dry_run_crash_residue_touches_nothing(claude):
    _mk(claude, "dockwright/manager-memory/j.md")
    lines = migrate.run(claude, dry_run=True)
    assert not (claude / "manager-memory").is_symlink()
    assert any("would-link" in l and "manager-memory" in l for l in lines)


def test_symlink_at_new_path_is_loud_collision(claude):
    """A symlink squatting at the new path (dangling included) is a loud
    collision — os.rename would silently replace the link itself."""
    _mk(claude, "loops-registry.md", "old")
    (claude / "dockwright").mkdir()
    os.symlink("elsewhere", claude / "dockwright/loops-registry.md")  # dangling
    with pytest.raises(migrate.MigrationError):
        migrate.run(claude)
    assert (claude / "loops-registry.md").read_text() == "old"
    assert os.readlink(claude / "dockwright/loops-registry.md") == "elsewhere"


def test_rename_race_with_ensure_dirs_falls_back_to_merge(claude, monkeypatch):
    """A concurrent ensure_dirs creating new (non-empty) between the existence
    check and os.rename must fold into a merge, not escape as a raw OSError."""
    import errno
    _mk(claude, "orchestrator/active/s1.json")
    real_rename = os.rename
    raced = {"done": False}

    def racy_rename(src, dst, *a, **kw):
        if not raced["done"] and str(dst).endswith("dockwright"):
            raced["done"] = True
            (Path(dst) / "manager-memory").mkdir(parents=True)
            raise OSError(errno.ENOTEMPTY, "Directory not empty", str(dst))
        return real_rename(src, dst, *a, **kw)

    monkeypatch.setattr(os, "rename", racy_rename)
    lines = migrate.run(claude)
    assert (claude / "dockwright/active/s1.json").is_file()
    assert (claude / "dockwright/manager-memory").is_dir()
    assert (claude / "orchestrator").is_symlink()
    assert all("error" not in l.lower() for l in lines)


def test_cli_main_ok_and_error_paths(claude, capsys):
    _mk(claude, "orchestrator/active/s1.json")
    assert migrate.main(["--claude-dir", str(claude)]) == 0
    assert (claude / "orchestrator").is_symlink()
    _mk(claude, "loops-registry.md", "old")
    _mk(claude, "dockwright/loops-registry.md", "new")
    assert migrate.main(["--claude-dir", str(claude)]) == 1
    assert "error:" in capsys.readouterr().err
