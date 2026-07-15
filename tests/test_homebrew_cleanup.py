from pathlib import Path
import pytest
from dockwright import homebrew_cleanup as hc


def _seed_editable(fs, prefix="/opt/homebrew", ver="3.14", with_finder=False):
    site = Path(prefix) / "lib" / f"python{ver}" / "site-packages"
    fs.create_file(site / "__editable__.dockwright-0.2.0.pth",
                   contents="/Users/testop/projects/personal/claude-orchestrator/src\n")
    fs.create_dir(site / "dockwright-0.2.0.dist-info")
    if with_finder:
        fs.create_file(site / "__editable___dockwright_0_2_0_finder.py")
    fs.create_file(Path(prefix) / "bin" / f"python{ver}")
    return site


def test_find_detects_pth_and_distinfo_without_finder(fs):
    _seed_editable(fs, with_finder=False)
    found = hc.find_brew_editable(Path("/opt/homebrew"), "dockwright")
    assert len(found) == 1
    assert found[0].python_bin == Path("/opt/homebrew/bin/python3.14")
    # no finder.py exists; detection still succeeds
    assert not any("finder" in p.name for p in found[0].artifacts)


def test_find_collects_finder_when_present(fs):
    _seed_editable(fs, with_finder=True)
    found = hc.find_brew_editable(Path("/opt/homebrew"), "dockwright")
    assert any("finder" in p.name for p in found[0].artifacts)


def test_find_does_not_collect_sibling_dist_finder(fs):
    # The real finder name embeds the version (digit after the dist token); a sibling
    # distribution (dockwright_extra) starts with a letter and must NOT be collected.
    site = _seed_editable(fs, with_finder=True)
    fs.create_file(site / "__editable___dockwright_extra_0_1_finder.py")
    found = hc.find_brew_editable(Path("/opt/homebrew"), "dockwright")
    finders = [p.name for p in found[0].artifacts if "finder" in p.name]
    assert "__editable___dockwright_0_2_0_finder.py" in finders
    assert "__editable___dockwright_extra_0_1_finder.py" not in finders


def test_find_empty_when_absent(fs):
    fs.create_dir("/opt/homebrew/lib/python3.14/site-packages")
    assert hc.find_brew_editable(Path("/opt/homebrew"), "dockwright") == []


def test_stray_console_script_only_when_brew_shebang(fs):
    fs.create_file("/opt/homebrew/bin/orchestrator",
                   contents="#!/opt/homebrew/opt/python@3.14/bin/python3.14\nprint(1)\n")
    assert hc.find_stray_console_script(Path("/opt/homebrew/bin"), "orchestrator", Path("/opt/homebrew"))
    fs.create_file("/Users/testop/.local/bin/orchestrator",
                   contents="#!/Users/testop/projects/personal/claude-orchestrator/.venv/bin/python\n")
    assert hc.find_stray_console_script(Path("/Users/testop/.local/bin"), "orchestrator", Path("/opt/homebrew")) is None


def test_clean_uninstalls_per_interp_removes_script_and_verifies(fs):
    _seed_editable(fs)
    fs.create_file("/opt/homebrew/bin/orchestrator",
                   contents="#!/opt/homebrew/opt/python@3.14/bin/python3.14\n")
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        class R: returncode = 1  # import check fails => gone
        return R()
    report = hc.clean(Path("/opt/homebrew"), "dockwright", "orchestrator", run=fake_run)
    assert any("uninstall" in c for c in calls)
    assert report["removed_scripts"] == ["/opt/homebrew/bin/orchestrator"]
    # artifacts physically removed
    assert hc.find_brew_editable(Path("/opt/homebrew"), "dockwright") == []


def test_clean_raises_on_residual_import(fs):
    _seed_editable(fs)
    def fake_run(cmd, **kw):
        class R: returncode = 0  # import check still succeeds => residual
        return R()
    with pytest.raises(hc.CleanupError):
        hc.clean(Path("/opt/homebrew"), "dockwright", "orchestrator", run=fake_run)


def test_clean_dry_run_does_nothing(fs):
    _seed_editable(fs)
    def boom(*a, **k): raise AssertionError("should not run")
    report = hc.clean(Path("/opt/homebrew"), "dockwright", "orchestrator", run=boom, dry_run=True)
    assert report["dry_run"] is True
    assert hc.find_brew_editable(Path("/opt/homebrew"), "dockwright")  # still present
