import time

from dockwright import boot_brief, notebook_mark, paths


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
    assert len(memory_lines) == 3
    assert [l.rsplit("/", 1)[1] for l in memory_lines] == ["m0.md", "m1.md", "m2.md"]
    assert not any(l.endswith("m6.md") for l in memory_lines)
    assert all(str(mem_root) in l for l in memory_lines)
    assert any(l.startswith("NOTEBOOK ") and "(5000 bytes)" in l for l in out)
    assert any(l.startswith("NOTEBOOK_WARN") for l in out)


def test_boot_brief_empty_stores_prints_only_agent_lines(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    assert boot_brief.main(["--domain", "general"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["AGENT_LINES 3"]


def test_agent_lines_count_the_manager_render_when_it_exists(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(paths, "ROOT", tmp_path / "dockwright")
    monkeypatch.setattr(paths, "MANAGER_MEMORY", tmp_path / "memory")
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "manager.md").write_text("a\nb\nc\n")
    monkeypatch.setattr(paths, "AGENTS_DIR", agents)
    monkeypatch.setattr(paths, "ROLES_DIR", tmp_path / "roles")
    assert boot_brief.main([]) == 0
    assert capsys.readouterr().out.splitlines() == ["AGENT_LINES 3"]
    render = tmp_path / "roles" / "Manager" / "role.manager.md"
    render.parent.mkdir(parents=True)
    render.write_text("1\n2\n3\n4\n5\n")
    assert boot_brief.main([]) == 0
    assert capsys.readouterr().out.splitlines() == ["AGENT_LINES 5"]


def _notebook(root):
    nb = root / "notebook" / "general.md"
    nb.parent.mkdir(parents=True, exist_ok=True)
    nb.write_text("# N\n\n## e\n")


def test_boot_brief_prints_not_actualized_mark(tmp_path, monkeypatch, capsys):
    root, _ = _setup(tmp_path, monkeypatch)
    _notebook(root)
    notebook_mark.write("abcdef123456", "general", reason="context exhausted", now=0.0)
    assert boot_brief.main(["--domain", "general"]) == 0
    assert ("NOTEBOOK_NOT_ACTUALIZED [context exhausted — session abcdef12 at 1970-01-01T00:00Z; "
            "after the boot steps, run Skill: dockwright-notebook-hygiene]") in capsys.readouterr().out.splitlines()


def test_boot_brief_quiet_on_actualized_mark(tmp_path, monkeypatch, capsys):
    root, _ = _setup(tmp_path, monkeypatch)
    _notebook(root)
    notebook_mark.write("abcdef123456", "general")
    assert boot_brief.main(["--domain", "general"]) == 0
    assert "NOTEBOOK_NOT_ACTUALIZED" not in capsys.readouterr().out


def test_boot_brief_survives_an_unrepresentable_mark_time(tmp_path, monkeypatch, capsys):
    root, _ = _setup(tmp_path, monkeypatch)
    _notebook(root)
    mark = root / "notebook" / "marks" / "general.json"
    mark.parent.mkdir(parents=True)
    mark.write_text('{"sid": "s1", "at": 1e300, "actualized": false, "reason": "r"}')
    assert boot_brief.main(["--domain", "general"]) == 0
    assert "NOTEBOOK_NOT_ACTUALIZED" not in capsys.readouterr().out
