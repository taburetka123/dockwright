import time

from dockwright import boot_brief, paths


def _setup(tmp_path, monkeypatch):
    root = tmp_path / "dockwright"
    root.mkdir()
    monkeypatch.setattr(paths, "ROOT", root)
    mem_root = tmp_path / "custom-memory"
    monkeypatch.setattr(paths, "MANAGER_MEMORY", mem_root)
    agents = tmp_path / "agents"
    agents.mkdir()
    agent_file = agents / "manager.md"
    agent_file.write_text("line1\nline2\nline3\n")
    monkeypatch.setattr(boot_brief, "_agent_file", lambda: agent_file)
    return root, mem_root


def test_boot_brief_prints_agent_lines_memory_and_notebook(tmp_path, monkeypatch, capsys):
    root, mem_root = _setup(tmp_path, monkeypatch)
    mem = mem_root / "general"
    mem.mkdir(parents=True)
    now = time.time()
    for i in range(7):
        f = mem / f"m{i}.md"
        f.write_text("x")
        age_days = i if i < 6 else 30
        import os
        os.utime(f, (now - age_days * 86400, now - age_days * 86400))
    nb_dir = root / "notebook"
    nb_dir.mkdir()
    (nb_dir / "general.md").write_text("n" * 5000)
    assert boot_brief.main(["--domain", "general"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "AGENT_LINES 3"
    memory_lines = [l for l in out if l.startswith("MEMORY ")]
    assert len(memory_lines) == 5
    assert memory_lines[0].endswith("m0.md")
    assert not any(l.endswith("m6.md") for l in memory_lines)
    assert all(str(mem_root) in l for l in memory_lines)
    assert any(l.startswith("NOTEBOOK ") and "(5000 bytes)" in l for l in out)
    assert any(l.startswith("NOTEBOOK_WARN") for l in out)


def test_boot_brief_empty_stores_prints_only_agent_lines(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    assert boot_brief.main(["--domain", "general"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["AGENT_LINES 3"]
