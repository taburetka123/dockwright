"""compose engine — markers, drop-ins, vars, and the identity guarantee."""
from pathlib import Path

import pytest

from dockwright import compose
from dockwright.compose import ComposeError, DropIn


def _d(name, body, insert_at=None):
    return DropIn(path=Path(f"/x/{name}"), insert_at=insert_at, body=body)


# --- identity (the byte-equivalence foundation) ---

@pytest.mark.parametrize("text", [
    "",  # empty
    "plain text\n",
    "no trailing newline",
    "line1\n\nline3\n",
    "text with {curly} braces but no vars\n",
    "an <!-- html comment --> inline\n",
    "<!-- overlay-ish but not a marker\n",
])
def test_identity_without_markers_or_vars(text):
    composed, warnings = compose.compose_text(text, [], {})
    assert composed == text
    assert warnings == []


# --- markers ---

CORE = "top\n<!-- overlay: hook -->\nbottom\n"


def test_marker_removed_when_unbound():
    composed, _ = compose.compose_text(CORE, [], {})
    assert composed == "top\nbottom\n"


def test_marker_replaced_by_bound_dropins_in_filename_order():
    dropins = [_d("10-a.md", "AAA\n", "hook"), _d("20-b.md", "BBB\n", "hook")]
    composed, _ = compose.compose_text(CORE, dropins, {})
    assert composed == "top\nAAA\nBBB\nbottom\n"


def test_unknown_insert_at_fails_loud_listing_markers():
    with pytest.raises(ComposeError) as exc:
        compose.compose_text(CORE, [_d("10-a.md", "A\n", "nope")], {})
    assert "nope" in str(exc.value) and "hook" in str(exc.value)


def test_duplicate_marker_fails():
    with pytest.raises(ComposeError):
        compose.compose_text(
            "<!-- overlay: h -->\nmid\n<!-- overlay: h -->\n", [], {})


def test_marker_requires_exact_full_line():
    text = "  <!-- overlay: indented -->\nx <!-- overlay: inline -->\n"
    composed, _ = compose.compose_text(text, [], {})
    assert composed == text  # neither is a marker; both survive


# --- end-append drop-ins ---

def test_dropin_without_insert_at_appends_at_end():
    composed, _ = compose.compose_text("core\n", [_d("10-a.md", "extra\n")], {})
    assert composed == "core\nextra\n"


def test_end_append_adds_newline_when_core_lacks_one():
    composed, _ = compose.compose_text("core", [_d("10-a.md", "extra\n")], {})
    assert composed == "core\nextra\n"


# --- vars ---

def test_vars_substitute_in_core_and_dropins():
    composed, warnings = compose.compose_text(
        "regex: {{ticket}}\n<!-- overlay: h -->\n",
        [_d("10-a.md", "also {{ticket}}\n", "h")],
        {"ticket": "[A-Z]+-1"})
    assert composed == "regex: [A-Z]+-1\nalso [A-Z]+-1\n"
    assert warnings == []


def test_unbound_var_stays_literal_and_warns():
    composed, warnings = compose.compose_text("{{missing}} here\n", [], {"other": "x"})
    assert composed == "{{missing}} here\n"
    assert warnings and "missing" in warnings[0]


# --- drop-in parsing ---

def test_parse_dropin_frontmatter_and_body(tmp_path):
    p = tmp_path / "10-x.md"
    p.write_text("---\ninsert_at: hook\nignored: y\n---\nBody line\n")
    d = compose.parse_dropin(p)
    assert d.insert_at == "hook"
    assert d.body == "Body line\n"


def test_parse_dropin_no_frontmatter(tmp_path):
    p = tmp_path / "10-x.md"
    p.write_text("Just body")
    d = compose.parse_dropin(p)
    assert d.insert_at is None
    assert d.body == "Just body\n"  # bodies normalize to end with newline


def test_load_dropins_sorted_and_scoped(tmp_path):
    (tmp_path / "manager").mkdir()
    (tmp_path / "manager" / "20-b.md").write_text("b\n")
    (tmp_path / "manager" / "10-a.md").write_text("a\n")
    (tmp_path / "worker").mkdir()
    (tmp_path / "worker" / "10-w.md").write_text("w\n")
    names = [d.path.name for d in compose.load_dropins(tmp_path, "manager")]
    assert names == ["10-a.md", "20-b.md"]
    assert compose.load_dropins(tmp_path, "nope") == []
    assert compose.load_dropins(tmp_path / "absent-overlay", "manager") == []


# --- output_name (.core.md naming rule) ---

def test_output_name_strips_core_suffix():
    assert compose.output_name("manager.core.md") == "manager.md"


def test_output_name_plain_md_unchanged():
    assert compose.output_name("worker.md") == "worker.md"


def test_output_name_does_not_mangle_short_names():
    # "core.md" itself can't end with the (longer) ".core.md" suffix.
    assert compose.output_name("core.md") == "core.md"


# --- vars.defaults.toml (defaults layer) ---

def test_load_default_vars_absent_file_is_empty(tmp_path):
    assert compose.load_default_vars(tmp_path) == {}


def test_load_default_vars_reads_agent_vars_table(tmp_path):
    (tmp_path / "vars.defaults.toml").write_text(
        '[agent_vars]\nticket_key_regex = "TKT-SANDBOX-1"\nother = "x"\n')
    assert compose.load_default_vars(tmp_path) == {
        "ticket_key_regex": "TKT-SANDBOX-1", "other": "x"}


def test_load_default_vars_skips_non_string_entries(tmp_path):
    (tmp_path / "vars.defaults.toml").write_text(
        "[agent_vars]\ngood = \"x\"\nbad_num = 5\nbad_bool = true\n")
    assert compose.load_default_vars(tmp_path) == {"good": "x"}


def test_load_default_vars_missing_section_is_empty(tmp_path):
    (tmp_path / "vars.defaults.toml").write_text("[other_section]\nx = \"1\"\n")
    assert compose.load_default_vars(tmp_path) == {}


def test_load_default_vars_corrupt_toml_is_fail_open(tmp_path):
    (tmp_path / "vars.defaults.toml").write_text("not [ valid toml")
    assert compose.load_default_vars(tmp_path) == {}


def test_load_default_vars_ignores_non_toml_extension(tmp_path):
    # vars.defaults.toml itself is never treated as a core agent file — it's
    # not a .md at all, so the core glob (*.md) already excludes it.
    (tmp_path / "vars.defaults.toml").write_text('[agent_vars]\nfoo = "bar"\n')
    core_files = sorted(tmp_path.glob("*.md"))
    assert core_files == []
