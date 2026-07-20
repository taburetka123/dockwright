"""render.py — the deploy-time {{vars}} seam for commands and .md presets.

A thin wrapper over compose.compose_text(text, [], vars): no drop-ins, no
overlay markers. var-free files render byte-identically (keeps today's
operator command/preset deploys byte-stable); unbound {{...}} stays literal
(compose warning semantics).
"""
from dockwright import render


# --- render_file / render_text: identity, substitution, unbound-literal ---

def test_render_identity_without_vars(tmp_path):
    src = tmp_path / "a.md"; src.write_text("plain, {curly} but no var\n")
    out = tmp_path / "out.md"
    render.render_file(src, out, {"x": "y"})
    assert out.read_text() == src.read_text()


def test_render_substitutes_merged_vars(tmp_path):
    src = tmp_path / "a.md"; src.write_text("chain: {{dev_chain}}\n")
    out = tmp_path / "out.md"
    render.render_file(src, out, {"dev_chain": "design -> plan -> TDD"})
    assert out.read_text() == "chain: design -> plan -> TDD\n"


def test_render_leaves_unbound_literal(tmp_path):
    src = tmp_path / "a.md"; src.write_text("{{never_defined_var}}\n")
    out = tmp_path / "out.md"
    render.render_file(src, out, {})
    assert "{{never_defined_var}}" in out.read_text()


def test_render_text_returns_str(tmp_path):
    assert render.render_text("hi {{who}}\n", {"who": "there"}) == "hi there\n"
    # unbound stays literal, var-free is byte-identical
    assert render.render_text("{{unbound}} x\n", {}) == "{{unbound}} x\n"
    assert render.render_text("no vars here\n", {"x": "y"}) == "no vars here\n"


def test_render_file_creates_parent_dirs(tmp_path):
    src = tmp_path / "a.md"; src.write_text("hi\n")
    out = tmp_path / "nested" / "deep" / "out.md"
    render.render_file(src, out, {})
    assert out.read_text() == "hi\n"


# --- CLI: mirrors compose's var-merging (defaults ⊕ operator) ---

def test_cli_render_single_file(tmp_path):
    core = tmp_path / "core"; core.mkdir()
    (core / "vars.defaults.toml").write_text('[agent_vars]\nk = "V"\n')
    src = tmp_path / "in.md"; src.write_text("val: {{k}}\n")
    out = tmp_path / "out.md"
    rc = render.main(["--src", str(src), "--out", str(out), "--core-dir", str(core)])
    assert rc == 0
    assert out.read_text() == "val: V\n"


def test_cli_render_dir_glob_uses_default_vars(tmp_path):
    core = tmp_path / "core"; core.mkdir()
    (core / "vars.defaults.toml").write_text('[agent_vars]\nk = "DEF"\n')
    srcdir = tmp_path / "src"; srcdir.mkdir()
    (srcdir / "a.md").write_text("a: {{k}}\n")
    (srcdir / "b.md").write_text("b: plain\n")
    (srcdir / "skip.json").write_text("not md\n")
    outdir = tmp_path / "out"
    rc = render.main(["--src", str(srcdir), "--out", str(outdir),
                      "--glob", "*.md", "--core-dir", str(core)])
    assert rc == 0
    assert (outdir / "a.md").read_text() == "a: DEF\n"
    assert (outdir / "b.md").read_text() == "b: plain\n"  # byte-identical
    assert not (outdir / "skip.json").exists()  # glob excludes it


def test_cli_render_operator_var_wins_over_default(tmp_path, monkeypatch):
    core = tmp_path / "core"; core.mkdir()
    (core / "vars.defaults.toml").write_text('[agent_vars]\nk = "DEF"\n')
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text('[agent_vars]\nk = "OP"\n')
    monkeypatch.setenv("DOCKWRIGHT_CONFIG", str(cfg))
    src = tmp_path / "in.md"; src.write_text("val: {{k}}\n")
    out = tmp_path / "out.md"
    rc = render.main(["--src", str(src), "--out", str(out), "--core-dir", str(core)])
    assert rc == 0
    assert out.read_text() == "val: OP\n"


# --- <absolute-home> token on the render surface (inherited from compose) ---

def test_render_expands_home_token_in_var_value(tmp_path):
    from pathlib import Path as _P
    src = tmp_path / "a.md"; src.write_text("p: {{k}}\n")
    out = tmp_path / "out.md"
    render.render_file(src, out, {"k": "<absolute-home>/.claude/f.json"})
    assert out.read_text() == f"p: {_P.home()}/.claude/f.json\n"


def test_render_file_fails_closed_on_literal_token(tmp_path):
    from dockwright.compose import ComposeError
    import pytest
    src = tmp_path / "a.md"; src.write_text("has <absolute-home> literal\n")
    out = tmp_path / "out.md"
    with pytest.raises(ComposeError):
        render.render_file(src, out, {})
    assert not out.exists()


def test_cli_render_reports_compose_error_cleanly(tmp_path, capsys):
    core = tmp_path / "core"; core.mkdir()
    (core / "vars.defaults.toml").write_text("[agent_vars]\n")
    src = tmp_path / "in.md"; src.write_text("bad <absolute-home> here\n")
    out = tmp_path / "out.md"
    rc = render.main(["--src", str(src), "--out", str(out), "--core-dir", str(core)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "render: ERROR:" in err and "<absolute-home>" in err
    assert not out.exists()
