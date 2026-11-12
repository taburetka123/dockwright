import hashlib
import json
import os
import tomllib
from pathlib import Path

import pytest

from dockwright import codex_translate as ct


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _tree(root: Path) -> dict[str, str]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for f in sorted(filenames):
            p = Path(dirpath) / f
            out[str(p.relative_to(root))] = _sha(p)
    return out


def test_parse_frontmatter_scalar_block_and_folded():
    text = ("---\n"
            "name: sample-x\n"
            "description: |\n"
            "  Use at the COMPLETION of a ticket.\n"
            "  Second line: with a colon.\n"
            "argument-hint: \"<handoff_id>\"\n"
            "note: >-\n"
            "  folded one\n"
            "  folded two\n"
            "---\n"
            "# Body\n"
            "text\n")
    meta, body = ct.parse_frontmatter(text)
    assert meta["name"] == "sample-x"
    assert meta["description"] == "Use at the COMPLETION of a ticket.\nSecond line: with a colon."
    assert meta["argument-hint"] == "<handoff_id>"
    assert meta["note"] == "folded one folded two"
    assert body == "# Body\ntext\n"


def test_parse_frontmatter_without_frontmatter_returns_whole_text():
    assert ct.parse_frontmatter("# Just a body\n") == ({}, "# Just a body\n")
    assert ct.parse_frontmatter("---\nunterminated: yes\n") == ({}, "---\nunterminated: yes\n")


def test_render_agents_md_is_ordered_labelled_and_normalises_trailing_newlines(tmp_path):
    a = tmp_path / "a.md"; a.write_text("# A\n\nTRIGGER: when a\n\n\n")
    b = tmp_path / "b.md"; b.write_text("# B\nTRIGGER: when b")
    text = ct.render_agents_md([("rules/a.md", a), ("rules/b.md", b)])
    assert text.startswith(ct.MARKER + "\n")
    assert "2 rule files follow" in text
    assert "Sources: rules/a.md, rules/b.md" in text
    assert "<!-- BEGIN rules/a.md -->\n# A\n\nTRIGGER: when a\n<!-- END rules/a.md -->\n" in text
    assert "<!-- BEGIN rules/b.md -->\n# B\nTRIGGER: when b\n<!-- END rules/b.md -->\n" in text
    assert text.index("BEGIN rules/a.md") < text.index("BEGIN rules/b.md")
    assert ct.render_agents_md([("rules/a.md", a), ("rules/b.md", b)]) == text


def test_render_agent_toml_carries_block_description_and_roundtrips(tmp_path):
    agent = tmp_path / "integrator.md"
    agent.write_text("---\nname: integrator\ndescription: |\n  Line one.\n  Line \"two\" § with é.\ntools: All tools\n---\n\n# Body\n\nUse `x`.\n")
    toml_text = ct.render_agent_toml(agent)
    data = tomllib.loads(toml_text)
    assert data["name"] == "integrator"
    assert data["description"] == 'Line one.\nLine "two" § with é.'
    assert data["developer_instructions"] == "# Body\n\nUse `x`.\n"


def test_render_agent_toml_without_frontmatter_uses_stem_and_whole_body(tmp_path):
    agent = tmp_path / "plain.md"
    agent.write_text("# Plain\nbody\n")
    data = tomllib.loads(ct.render_agent_toml(agent))
    assert data == {"name": "plain", "description": "", "developer_instructions": "# Plain\nbody\n"}


@pytest.fixture
def claude(tmp_path):
    c = tmp_path / "claude"
    (c / "rules").mkdir(parents=True)
    (c / "rules" / "b-style.md").write_text("# Style\nTRIGGER: always\n\n")
    (c / "rules" / "a-commit.md").write_text("# Commit\nTRIGGER: git\n")
    (c / "rules" / "notes.txt").write_text("not a rule\n")
    skills = c / "skills"
    skills.mkdir()
    (skills / ".DS_Store").write_bytes(b"\x00\x01")
    (skills / "stray.txt").write_text("stray\n")
    s1 = skills / "alpha-one"
    (s1 / "references").mkdir(parents=True)
    (s1 / "SKILL.md").write_text("---\nname: alpha-one\ndescription: one\n---\n# One\n")
    (s1 / "references" / "r.md").write_text("ref\n")
    (s1 / "__pycache__").mkdir()
    (s1 / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    (s1 / ".DS_Store").write_bytes(b"\x00")
    s2 = skills / "beta-two"
    (s2 / "scripts").mkdir(parents=True)
    (s2 / "SKILL.md").write_text("---\nname: beta-two\ndescription: two {{not a template}}\n---\n# Two\n")
    script = s2 / "scripts" / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    outside = tmp_path / "outside-skill"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: linked\ndescription: linked\n---\n# Linked\n")
    (skills / "linked").symlink_to(outside, target_is_directory=True)
    (skills / "plannotator-annotate").mkdir()
    (skills / "plannotator-annotate" / "SKILL.md").write_text("---\nname: p\ndescription: p\n---\n")
    (skills / "no-skill-md").mkdir()
    (skills / "no-skill-md" / "README.md").write_text("nothing\n")
    (skills / "no-desc").mkdir()
    (skills / "no-desc" / "SKILL.md").write_text("---\nname: no-desc\n---\n# missing description\n")
    (skills / "ticket-explain").mkdir()
    (skills / "ticket-explain" / "SKILL.md").write_text("---\nname: ticket-explain\ndescription: skill wins\n---\n# Skill\n")
    cmds = c / "commands"
    cmds.mkdir()
    (cmds / "review-pr.md").write_text("---\ndescription: Review\nargument-hint: \"<pr>\"\n---\n# Review\nbody\n")
    (cmds / "tab.md").write_text("# Tab\nno frontmatter\n")
    (cmds / "ticket-explain.md").write_text("---\ndescription: command loses\n---\n# Cmd\n")
    (cmds / "plannotator-last.md").write_text("---\ndescription: annotate the last message\n---\n# Last\n")
    agents = c / "agents"
    agents.mkdir()
    (agents / "worker.md").write_text("---\nname: worker\ndescription: A worker.\n---\n# Worker\n")
    (agents / "integrator.md").write_text("---\nname: integrator\ndescription: |\n  Line one.\n  Line two.\n---\n# Integrator\n")
    (agents / "folded.md").write_text("---\nname: folded\ndescription: >-\n  folded a\n  folded b\n---\n# Folded\n")
    (agents / ".compose-stamp.json").write_text("{}")
    return c


@pytest.fixture
def codex(tmp_path):
    d = tmp_path / "codex"
    d.mkdir()
    return d


def test_plan_lists_every_source_and_skips_what_the_spec_skips(claude):
    desired = ct.plan(claude)
    entries = desired.entries
    assert entries["AGENTS.md"]["kind"] == "agents_md"
    copies = sorted(k for k, m in entries.items() if m["kind"] == "copy")
    assert copies == ["skills/alpha-one", "skills/beta-two", "skills/linked", "skills/no-desc", "skills/ticket-explain"]
    wrappers = sorted(k for k, m in entries.items() if m["kind"] == "wrapper")
    assert wrappers == ["skills/review-pr", "skills/tab"]
    agents = sorted(k for k, m in entries.items() if m["kind"] == "agent")
    assert agents == ["agents/folded.toml", "agents/integrator.toml", "agents/worker.toml"]
    assert entries["skills/alpha-one"]["files"] == ["SKILL.md", "references/r.md"]
    assert "skills/alpha-one/__pycache__/x.pyc" not in desired.files
    assert "skills/alpha-one/.DS_Store" not in desired.files
    assert "skills/stray.txt" not in desired.files
    assert not any(k.startswith("skills/plannotator-") for k in entries)
    assert "skills/plannotator-last" not in entries
    assert any(n == "skipped command wrapper plannotator-last: plugin-owned name" for n in desired.notes)
    assert "skills/no-skill-md" not in entries
    assert any("no-skill-md" in n for n in desired.notes)
    assert any("no-desc" in n and "description" in n for n in desired.notes)
    assert any("ticket-explain" in n and "skipped command wrapper" in n for n in desired.notes)
    assert desired.counts == {"rules": 2, "skills": 5, "wrappers": 2, "wrappers_skipped": 2, "agents": 3}


def test_translate_writes_every_target_and_is_idempotent(claude, codex):
    r1 = ct.translate(claude, codex)
    agents_md = (codex / "AGENTS.md").read_text()
    assert agents_md.startswith(ct.MARKER)
    assert agents_md.index("BEGIN rules/a-commit.md") < agents_md.index("BEGIN rules/b-style.md")
    assert "<!-- BEGIN rules/b-style.md -->\n# Style\nTRIGGER: always\n<!-- END rules/b-style.md -->" in agents_md
    assert "notes.txt" not in agents_md
    assert (codex / "skills" / "alpha-one" / "references" / "r.md").read_text() == "ref\n"
    assert not (codex / "skills" / "alpha-one" / "__pycache__").exists()
    assert not (codex / "skills" / "alpha-one" / ".DS_Store").exists()
    assert os.access(codex / "skills" / "beta-two" / "scripts" / "run.sh", os.X_OK)
    assert (codex / "skills" / "linked" / "SKILL.md").is_file()
    assert not (codex / "skills" / "linked").is_symlink()
    assert not (codex / "skills" / "plannotator-annotate").exists()
    assert not (codex / "skills" / "plannotator-last").exists()
    assert not (codex / "skills" / "stray.txt").exists()
    assert (codex / "skills" / "ticket-explain" / "SKILL.md").read_text().endswith("# Skill\n")
    wrapper = (codex / "skills" / "review-pr" / "SKILL.md").read_text()
    assert 'name: "review-pr"' in wrapper and 'argument-hint: "<pr>"' in wrapper and "user-invocable: true" in wrapper
    assert 'description: "Run /tab."' in (codex / "skills" / "tab" / "SKILL.md").read_text()
    for stem in ("worker", "integrator", "folded"):
        tomllib.loads((codex / "agents" / f"{stem}.toml").read_text())
    assert tomllib.loads((codex / "agents" / "integrator.toml").read_text())["description"] == "Line one.\nLine two."
    assert tomllib.loads((codex / "agents" / "folded.toml").read_text())["description"] == "folded a folded b"
    stamp = json.loads((codex / ct.STAMP_NAME).read_text())
    assert stamp["version"] == 1
    assert stamp["entries"]["AGENTS.md"] == {"kind": "agents_md", "sha256": _sha(codex / "AGENTS.md")}
    wrapper_entry = stamp["entries"]["skills/review-pr"]
    assert wrapper_entry["kind"] == "wrapper" and wrapper_entry["files"] == ["SKILL.md"]
    assert len(wrapper_entry["sha256"]) == 64
    assert len(stamp["entries"]["skills/alpha-one"]["sha256"]) == 64
    assert stamp["entries"]["agents/worker.toml"]["sha256"] == _sha(codex / "agents" / "worker.toml")
    assert r1.counts["skills"] == 5 and r1.counts["wrappers"] == 2 and r1.counts["agents"] == 3
    assert r1.counts["agents_md_bytes"] == (codex / "AGENTS.md").stat().st_size
    before = _tree(codex)
    ct.translate(claude, codex)
    assert _tree(codex) == before
    assert not (codex / ct.BACKUP_DIR_NAME).exists()


def test_translate_archives_markdown_rules_only_and_keeps_execpolicy(claude, codex):
    (codex / "rules").mkdir()
    (codex / "rules" / "old.md").write_text("old\n")
    (codex / "rules" / "default.rules").write_text('prefix_rule(pattern=["ls"], decision="allow")\n')
    archive = codex / ct.BACKUP_DIR_NAME / "rules"
    r = ct.translate(claude, codex)
    assert not (codex / "rules" / "old.md").exists()
    assert (archive / "old.md").read_text() == "old\n"
    assert (codex / "rules" / "default.rules").is_file()
    assert r.counts["rules_removed"] == 1
    assert any(n == f"archived 1 markdown rule(s) from rules/ -> {ct.BACKUP_DIR_NAME}/rules"
               for n in r.notes)
    (codex / "rules" / "again.md").write_text("again\n")
    r = ct.translate(claude, codex)
    assert (archive / "again.md").read_text() == "again\n"
    assert (archive / "old.md").read_text() == "old\n"
    assert r.counts["rules_removed"] == 1
    assert (codex / "rules").is_dir()
    (codex / "rules" / "default.rules").unlink()
    r = ct.translate(claude, codex)
    assert not (codex / "rules").exists()
    assert r.counts["rules_removed"] == 0
    assert not any(n.startswith("archived") for n in r.notes)


def test_translate_never_overwrites_a_rule_it_already_archived(claude, codex):
    (codex / "rules").mkdir()
    (codex / "rules" / "old.md").write_text("first\n")
    ct.translate(claude, codex)
    (codex / "rules").mkdir()
    (codex / "rules" / "old.md").write_text("second\n")
    ct.translate(claude, codex)
    archive = codex / ct.BACKUP_DIR_NAME / "rules"
    assert (archive / "old.md").read_text() == "first\n"
    assert (archive / "old.md.1").read_text() == "second\n"


def test_translate_leaves_foreign_entries_untouched(claude, codex):
    system = codex / "skills" / ".system" / "skill-creator"
    system.mkdir(parents=True)
    (system / "SKILL.md").write_text("codex-owned\n")
    (codex / "skills" / ".DS_Store").write_bytes(b"\x01")
    foreign = codex / "skills" / "foreign-tool"
    foreign.mkdir()
    (foreign / "SKILL.md").write_text("SENTINEL\n")
    (codex / "agents").mkdir()
    (codex / "agents" / "foreign.toml").write_text('name = "foreign"\n')
    (codex / "config.toml").write_text("model = \"x\"\n")
    ct.translate(claude, codex)
    assert (system / "SKILL.md").read_text() == "codex-owned\n"
    assert (codex / "skills" / ".DS_Store").read_bytes() == b"\x01"
    assert (foreign / "SKILL.md").read_text() == "SENTINEL\n"
    assert (codex / "agents" / "foreign.toml").read_text() == 'name = "foreign"\n'
    assert (codex / "config.toml").read_text() == "model = \"x\"\n"
    assert not (codex / ct.BACKUP_DIR_NAME).exists()


def test_translate_backs_up_same_named_targets_it_does_not_own(claude, codex):
    (codex / "AGENTS.md").write_text("hand-written may snapshot\n")
    old_skill = codex / "skills" / "alpha-one"
    old_skill.mkdir(parents=True)
    (old_skill / "SKILL.md").write_text("may copy\n")
    (old_skill / "notes.md").write_text("someone's notes\n")
    old_wrapper = codex / "skills" / "review-pr"
    old_wrapper.mkdir()
    (old_wrapper / "SKILL.md").write_text("old wrapper\n")
    (codex / "agents").mkdir()
    (codex / "agents" / "worker.toml").write_text("old toml\n")
    r = ct.translate(claude, codex)
    backup = codex / ct.BACKUP_DIR_NAME
    assert (backup / "AGENTS.md").read_text() == "hand-written may snapshot\n"
    assert (backup / "skills" / "alpha-one" / "notes.md").read_text() == "someone's notes\n"
    assert (backup / "skills" / "alpha-one" / "SKILL.md").read_text() == "may copy\n"
    assert (backup / "skills" / "review-pr" / "SKILL.md").read_text() == "old wrapper\n"
    assert (backup / "agents" / "worker.toml").read_text() == "old toml\n"
    assert (codex / "AGENTS.md").read_text().startswith(ct.MARKER)
    assert not (codex / "skills" / "alpha-one" / "notes.md").exists()
    assert sum("backed up" in n for n in r.notes) == 4
    before = _tree(backup)
    ct.translate(claude, codex)
    assert _tree(backup) == before


def test_backup_is_write_once_even_after_the_stamp_is_lost(claude, codex):
    (codex / "AGENTS.md").write_text("may snapshot\n")
    ct.translate(claude, codex)
    ours = (codex / "AGENTS.md").read_bytes()
    (codex / ct.STAMP_NAME).unlink()
    r = ct.translate(claude, codex)
    backup = codex / ct.BACKUP_DIR_NAME
    assert (backup / "AGENTS.md").read_text() == "may snapshot\n"
    assert (backup / "AGENTS.md.1").read_bytes() == ours
    assert any(n.startswith("backed up AGENTS.md") and "AGENTS.md.1" in n for n in r.notes)


def test_translate_prunes_owned_targets_whose_source_vanished_only_when_unchanged(claude, codex):
    ct.translate(claude, codex)
    import shutil as _sh
    _sh.rmtree(claude / "skills" / "alpha-one")
    (claude / "commands" / "tab.md").unlink()
    (claude / "agents" / "folded.md").unlink()
    (codex / "skills" / "beta-two" / "extra.md").write_text("added by hand\n")
    _sh.rmtree(claude / "skills" / "beta-two")
    r = ct.translate(claude, codex)
    assert not (codex / "skills" / "alpha-one").exists()
    assert not (codex / "skills" / "tab").exists()
    assert not (codex / "agents" / "folded.toml").exists()
    assert (codex / "skills" / "beta-two" / "extra.md").exists()
    assert any(n.startswith("left skills/beta-two") for n in r.notes)
    stamp = json.loads((codex / ct.STAMP_NAME).read_text())
    assert "skills/beta-two" not in stamp["entries"]
    assert ct.check(claude, codex) == (True, [])


def test_translate_keeps_a_hand_edited_agents_md_or_toml_when_its_source_vanishes(claude, codex):
    ct.translate(claude, codex)
    (codex / "AGENTS.md").write_text("edited by the operator\n")
    (codex / "agents" / "worker.toml").write_text("edited toml\n")
    for p in (claude / "rules").glob("*.md"):
        p.unlink()
    (claude / "agents" / "worker.md").unlink()
    r = ct.translate(claude, codex)
    assert (codex / "AGENTS.md").read_text() == "edited by the operator\n"
    assert (codex / "agents" / "worker.toml").read_text() == "edited toml\n"
    assert any(n.startswith("left AGENTS.md") for n in r.notes)
    assert "AGENTS.md" not in json.loads((codex / ct.STAMP_NAME).read_text())["entries"]
    assert ct.check(claude, codex) == (True, [])


def test_translate_removes_its_own_agents_md_when_rules_vanish(claude, codex):
    ct.translate(claude, codex)
    for p in (claude / "rules").glob("*.md"):
        p.unlink()
    r = ct.translate(claude, codex)
    assert not (codex / "AGENTS.md").exists()
    assert any("AGENTS.md" in n and "removed" in n for n in r.notes)
    assert ct.check(claude, codex) == (True, [])


def test_translate_without_rules_dir_generates_no_agents_md_and_keeps_a_foreign_one(claude, codex):
    import shutil as _sh
    _sh.rmtree(claude / "rules")
    (codex / "AGENTS.md").write_text("theirs\n")
    r = ct.translate(claude, codex)
    assert (codex / "AGENTS.md").read_text() == "theirs\n"
    assert any("AGENTS.md not generated" in n for n in r.notes)
    assert "AGENTS.md" not in json.loads((codex / ct.STAMP_NAME).read_text())["entries"]


def test_kind_change_between_runs_converges(claude, codex):
    import shutil as _sh
    ct.translate(claude, codex)
    (codex / "skills" / "ticket-explain" / "SKILL.md").write_text("edited in place\n")
    _sh.rmtree(claude / "skills" / "ticket-explain")
    r = ct.translate(claude, codex)
    assert not any(n.startswith("left skills/ticket-explain") for n in r.notes)
    assert any(n == "skills/ticket-explain: changed since we wrote it; backing it up before rewriting"
               for n in r.notes)
    assert any(n.startswith("backed up skills/ticket-explain") for n in r.notes)
    assert (codex / ct.BACKUP_DIR_NAME / "skills" / "ticket-explain" / "SKILL.md").read_text() == "edited in place\n"
    assert sorted(p.name for p in (codex / "skills" / "ticket-explain").iterdir()) == ["SKILL.md"]
    assert "command loses" in (codex / "skills" / "ticket-explain" / "SKILL.md").read_text()
    assert ct.check(claude, codex) == (True, [])
    (claude / "skills" / "ticket-explain").mkdir()
    (claude / "skills" / "ticket-explain" / "SKILL.md").write_text("---\nname: ticket-explain\ndescription: back\n---\n# Back\n")
    (claude / "skills" / "ticket-explain" / "extra.md").write_text("e\n")
    ct.translate(claude, codex)
    assert (codex / "skills" / "ticket-explain" / "extra.md").exists()
    assert ct.check(claude, codex) == (True, [])


def test_corrupt_stamp_is_a_warning_and_deletes_nothing(claude, codex):
    ct.translate(claude, codex)
    (codex / ct.STAMP_NAME).write_text("{ not json")
    tree_before = {k: v for k, v in _tree(codex).items() if k != ct.STAMP_NAME}
    r = ct.translate(claude, codex)
    assert any("stamp" in n.lower() and "warning" in n.lower() for n in r.notes)
    after = _tree(codex)
    for k, v in tree_before.items():
        assert after[k] == v
    assert (codex / ct.BACKUP_DIR_NAME / "AGENTS.md").read_bytes() == (codex / "AGENTS.md").read_bytes()
    json.loads((codex / ct.STAMP_NAME).read_text())


def test_check_without_stamp_is_never_translated(claude, codex):
    ok, problems = ct.check(claude, codex)
    assert ok is False and problems == ["never translated - run setup.sh"]


def test_check_is_green_after_a_run_and_red_on_every_drift(claude, codex):
    ct.translate(claude, codex)
    assert ct.check(claude, codex) == (True, [])
    (codex / "skills" / "alpha-one" / ".DS_Store").write_bytes(b"\x00")
    assert ct.check(claude, codex) == (True, [])
    (claude / "rules" / "a-commit.md").write_text("# Commit\nTRIGGER: changed\n")
    ok, problems = ct.check(claude, codex)
    assert not ok and any(p.startswith("AGENTS.md: differs") for p in problems)
    assert any(p == "stamp differs" for p in problems)
    ct.translate(claude, codex)
    (codex / "skills" / "alpha-one" / "extra.txt").write_text("x\n")
    ok, problems = ct.check(claude, codex)
    assert not ok and problems == ["extra skills/alpha-one/extra.txt"]
    (codex / "skills" / "alpha-one" / "extra.txt").unlink()
    (codex / "rules").mkdir()
    (codex / "rules" / "z.md").write_text("z\n")
    ok, problems = ct.check(claude, codex)
    assert not ok and problems == ["hand-written rule present: z.md"]
    (codex / "rules" / "z.md").unlink()
    (codex / "agents" / "worker.toml").unlink()
    ok, problems = ct.check(claude, codex)
    assert not ok and problems == ["agents/worker.toml: not deployed"]


def test_check_flags_a_stale_owned_entry_that_a_run_would_prune(claude, codex):
    ct.translate(claude, codex)
    import shutil as _sh
    _sh.rmtree(claude / "skills" / "alpha-one")
    ok, problems = ct.check(claude, codex)
    assert not ok and "stale owned entry skills/alpha-one" in problems
    ct.translate(claude, codex)
    assert ct.check(claude, codex) == (True, [])


def test_cli_translate_prints_summary_and_check_exit_codes(claude, codex, capsys):
    rc = ct.main(["--claude-dir", str(claude), "--codex-dir", str(codex)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "translated: 2 rules -> AGENTS.md (" in out
    assert "5 skills, 2 command wrappers (2 skipped), 3 agents; removed 0 markdown rule(s) from rules/" in out
    assert "skipped command wrapper ticket-explain" in out
    assert ct.main(["--claude-dir", str(claude), "--codex-dir", str(codex), "--check"]) == 0
    assert "translate-codex check: fresh" in capsys.readouterr().out
    (claude / "rules" / "a-commit.md").write_text("changed\n")
    assert ct.main(["--claude-dir", str(claude), "--codex-dir", str(codex), "--check"]) == 1
    out = capsys.readouterr().out
    assert "stale: AGENTS.md: differs" in out and "STALE" in out


def test_cli_translate_defaults_come_from_config_and_codex_home(claude, codex, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setattr(ct.config, "claude_config_home", lambda: claude)
    assert ct.default_codex_dir() == codex
    assert ct.main([]) == 0
    assert (codex / ct.STAMP_NAME).is_file()
    monkeypatch.delenv("CODEX_HOME")
    monkeypatch.setenv("HOME", str(codex.parent))
    assert ct.default_codex_dir() == codex.parent / ".codex"


def test_cli_render_agents_md_keeps_argument_order_and_labels(tmp_path, capsys):
    (tmp_path / "AGENTS.md").write_text("team\n")
    sub = tmp_path / ".claude" / "rules" / "common"
    sub.mkdir(parents=True)
    (sub / "x.md").write_text("x rule\n\n")
    out_file = tmp_path / "out" / "AGENTS.override.md"
    import os as _os
    cwd = _os.getcwd()
    _os.chdir(tmp_path)
    try:
        rc = ct.render_main(["--out", str(out_file), "AGENTS.md", ".claude/rules/common/x.md"])
    finally:
        _os.chdir(cwd)
    assert rc == 0
    text = out_file.read_text()
    assert text.startswith(ct.MARKER)
    assert "Sources: AGENTS.md, .claude/rules/common/x.md" in text
    assert text.index("<!-- BEGIN AGENTS.md -->") < text.index("<!-- BEGIN .claude/rules/common/x.md -->")
    assert "<!-- BEGIN .claude/rules/common/x.md -->\nx rule\n<!-- END .claude/rules/common/x.md -->" in text
    _os.chdir(tmp_path)
    try:
        assert ct.render_main(["--out", str(out_file), "missing.md"]) == 2
    finally:
        _os.chdir(cwd)
    assert "missing.md" in capsys.readouterr().err


def test_dispatcher_routes_both_subcommands_and_the_alias(claude, codex, tmp_path, monkeypatch):
    import sys as _sys
    from dockwright import __main__ as entry
    monkeypatch.setattr(_sys, "argv", ["dockwright", "translate-codex", "--claude-dir", str(claude), "--codex-dir", str(codex)])
    with pytest.raises(SystemExit) as e:
        entry.main()
    assert e.value.code == 0 and (codex / ct.STAMP_NAME).is_file()
    out_file = tmp_path / "o.md"
    monkeypatch.setattr(_sys, "argv", ["dockwright", "render-agents-md", "--out", str(out_file), str(claude / "rules" / "a-commit.md")])
    with pytest.raises(SystemExit) as e:
        entry.main()
    assert e.value.code == 0 and out_file.read_text().startswith(ct.MARKER)
    skills_out = tmp_path / "alias-skills"
    monkeypatch.setattr(_sys, "argv", ["dockwright", "install-codex-skills", str(claude / "commands"), str(skills_out)])
    with pytest.raises(SystemExit) as e:
        entry.main()
    assert e.value.code == 0 and (skills_out / "review-pr" / "SKILL.md").is_file()


def test_translate_leaves_an_in_place_edited_skill_whose_source_vanished(claude, codex):
    ct.translate(claude, codex)
    (codex / "skills" / "alpha-one" / "SKILL.md").write_text("edited in place, same file names\n")
    import shutil as _sh
    _sh.rmtree(claude / "skills" / "alpha-one")
    r = ct.translate(claude, codex)
    assert (codex / "skills" / "alpha-one" / "SKILL.md").read_text() == "edited in place, same file names\n"
    assert any(n == "left skills/alpha-one: changed since we wrote it" for n in r.notes)
    assert not (codex / ct.BACKUP_DIR_NAME).exists()
    assert "skills/alpha-one" not in json.loads((codex / ct.STAMP_NAME).read_text())["entries"]
    assert ct.check(claude, codex) == (True, [])


def test_check_reports_an_unreadable_stamp_before_it_reports_the_stamp_differing(claude, codex):
    ct.translate(claude, codex)
    (codex / ct.STAMP_NAME).write_text("{ not json")
    ok, problems = ct.check(claude, codex)
    assert not ok
    assert problems[0].startswith("warning: unreadable stamp")
    assert sum(p.startswith("warning: unreadable stamp") for p in problems) == 1
    assert problems.index("stamp differs") > 0


def test_plan_skips_a_dangling_symlink_under_a_skill_and_the_run_completes(claude, codex):
    (claude / "skills" / "alpha-one" / "broken").symlink_to(claude / "skills" / "alpha-one" / "nowhere")
    desired = ct.plan(claude)
    assert desired.entries["skills/alpha-one"]["files"] == ["SKILL.md", "references/r.md"]
    assert "skills/alpha-one/broken" not in desired.files
    assert any(n == "warning: skipped skills/alpha-one/broken: dangling symlink" for n in desired.notes)
    ct.translate(claude, codex)
    assert (codex / "skills" / "alpha-one" / "SKILL.md").is_file()
    assert not os.path.lexists(codex / "skills" / "alpha-one" / "broken")
    assert ct.check(claude, codex) == (True, [])


def test_plan_terminates_on_a_symlink_cycle_and_lists_each_real_file_once(claude, codex):
    cyc = claude / "skills" / "cyc"
    (cyc / "sub").mkdir(parents=True)
    (cyc / "SKILL.md").write_text("---\nname: cyc\ndescription: cyc\n---\n# Cyc\n")
    (cyc / "sub" / "inner.md").write_text("inner\n")
    (cyc / "sub" / "up").symlink_to(cyc, target_is_directory=True)
    desired = ct.plan(claude)
    assert desired.entries["skills/cyc"]["files"] == ["SKILL.md", "sub/inner.md"]
    ct.translate(claude, codex)
    assert (codex / "skills" / "cyc" / "sub" / "inner.md").read_text() == "inner\n"
    assert not os.path.lexists(codex / "skills" / "cyc" / "sub" / "up")
    assert ct.check(claude, codex) == (True, [])


def test_stamp_entries_pointing_outside_the_codex_home_are_ignored(claude, codex, tmp_path):
    ct.translate(claude, codex)
    outside = tmp_path / "escape"
    outside.write_text("SENTINEL\n")
    stamp_path = codex / ct.STAMP_NAME
    tampered = json.loads(stamp_path.read_text())
    kept = codex / ct.BACKUP_DIR_NAME / "x"
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_text("BACKED UP\n")
    tampered["entries"]["../escape"] = {"kind": "agents_md", "sha256": _sha(outside)}
    tampered["entries"]["/abs"] = {"kind": "agents_md", "sha256": "0" * 64}
    tampered["entries"][f"{ct.BACKUP_DIR_NAME}/x"] = {"kind": "agents_md", "sha256": _sha(kept)}
    tampered["entries"][ct.STAMP_NAME] = {"kind": "agents_md", "sha256": "0" * 64}
    escaping = ["../escape", "/abs", f"{ct.BACKUP_DIR_NAME}/x", ct.STAMP_NAME]
    stamp_path.write_text(json.dumps(tampered))
    r = ct.translate(claude, codex)
    assert outside.read_text() == "SENTINEL\n"
    assert kept.read_text() == "BACKED UP\n"
    for rel in escaping:
        assert f"warning: ignored stamp entry {rel}: outside CODEX_HOME" in r.notes
    stamp_path.write_text(json.dumps(tampered))
    ok, problems = ct.check(claude, codex)
    assert not ok
    for rel in escaping:
        assert f"warning: ignored stamp entry {rel}: outside CODEX_HOME" in problems
    assert outside.read_text() == "SENTINEL\n"
    assert kept.read_text() == "BACKED UP\n"


def test_translate_announces_a_rewrite_of_an_owned_target_that_drifted(claude, codex):
    ct.translate(claude, codex)
    quiet = ct.translate(claude, codex)
    assert not any("changed since we wrote it" in n for n in quiet.notes)
    (codex / "skills" / "alpha-one" / "SKILL.md").write_text("hand-edited\n")
    (codex / "AGENTS.md").write_text("hand-edited\n")
    r = ct.translate(claude, codex)
    assert "skills/alpha-one: changed since we wrote it; rewriting from source" in r.notes
    assert "AGENTS.md: changed since we wrote it; rewriting from source" in r.notes
    assert not (codex / ct.BACKUP_DIR_NAME).exists()
    assert (codex / "AGENTS.md").read_text().startswith(ct.MARKER)
    assert (codex / "skills" / "alpha-one" / "SKILL.md").read_text().endswith("# One\n")
    assert ct.check(claude, codex) == (True, [])


def test_check_flags_a_deployed_file_whose_mode_drifted(claude, codex):
    ct.translate(claude, codex)
    assert ct.check(claude, codex) == (True, [])
    (codex / "skills" / "beta-two" / "scripts" / "run.sh").chmod(0o644)
    ok, problems = ct.check(claude, codex)
    assert not ok and problems == ["skills/beta-two/scripts/run.sh: mode differs"]
    ct.translate(claude, codex)
    assert os.access(codex / "skills" / "beta-two" / "scripts" / "run.sh", os.X_OK)
    assert ct.check(claude, codex) == (True, [])


def test_plan_skips_a_command_the_model_may_not_invoke(claude, codex):
    cmds = claude / "commands"
    (cmds / "gated.md").write_text(
        "---\ndescription: gated\ndisable-model-invocation: true\nallowed-tools: Bash\n---\n# Gated\n")
    (cmds / "open.md").write_text("---\ndescription: open\ndisable-model-invocation: false\n---\n# Open\n")
    desired = ct.plan(claude)
    assert "skills/gated" not in desired.entries
    assert "skills/gated/SKILL.md" not in desired.files
    assert any(n == "skipped command wrapper gated: disable-model-invocation" for n in desired.notes)
    assert desired.entries["skills/open"]["kind"] == "wrapper"
    assert desired.counts["wrappers_skipped"] == 3
    assert desired.counts["wrappers"] == 3
    ct.translate(claude, codex)
    assert not (codex / "skills" / "gated").exists()
    assert (codex / "skills" / "open" / "SKILL.md").is_file()
    assert ct.check(claude, codex) == (True, [])


def test_translate_leaves_an_externally_symlinked_rules_dir_alone(claude, codex, tmp_path):
    external = tmp_path / "external-rules"
    external.mkdir()
    (external / "keepme.md").write_text("KEEP\n")
    (codex / "rules").symlink_to(external, target_is_directory=True)
    r = ct.translate(claude, codex)
    assert (external / "keepme.md").read_text() == "KEEP\n"
    assert r.counts["rules_removed"] == 0
    assert "warning: rules/ is a symlink - left alone" in r.notes
    assert not (codex / ct.BACKUP_DIR_NAME / "rules").exists()
    ok, problems = ct.check(claude, codex)
    assert not ok
    assert "rules/: is a symlink - left alone" in problems
    assert not any(p.startswith("hand-written rule present") for p in problems)
    assert (external / "keepme.md").read_text() == "KEEP\n"


def test_translate_writes_nothing_through_an_externally_symlinked_skills_dir(claude, codex, tmp_path):
    external = tmp_path / "external-skills"
    (external / "foreign").mkdir(parents=True)
    (external / "foreign" / "SKILL.md").write_text("FOREIGN\n")
    (external / "alpha-one").mkdir()
    (external / "alpha-one" / "SKILL.md").write_text("PRE-EXISTING\n")
    (codex / "skills").symlink_to(external, target_is_directory=True)
    before = sorted(p.name for p in external.iterdir())
    r1 = ct.translate(claude, codex)
    assert sorted(p.name for p in external.iterdir()) == before
    assert (external / "alpha-one" / "SKILL.md").read_text() == "PRE-EXISTING\n"
    assert (external / "foreign" / "SKILL.md").read_text() == "FOREIGN\n"
    assert "warning: skipped skills/alpha-one: resolves outside CODEX_HOME" in r1.notes
    assert r1.counts["skills"] == 0 and r1.counts["wrappers"] == 0
    assert r1.counts["agents"] == 3 and r1.counts["rules"] == 2
    stamp = json.loads((codex / ct.STAMP_NAME).read_text())
    assert not any(k.startswith("skills/") for k in stamp["entries"])
    assert "AGENTS.md" in stamp["entries"]
    r2 = ct.translate(claude, codex)
    assert (external / "alpha-one" / "SKILL.md").read_text() == "PRE-EXISTING\n"
    assert (external / "foreign" / "SKILL.md").read_text() == "FOREIGN\n"
    assert any(n.startswith("warning: skipped skills/") for n in r2.notes)
    ok, problems = ct.check(claude, codex)
    assert not ok
    assert "skills/alpha-one: resolves outside CODEX_HOME" in problems


def test_a_normal_run_warns_about_nothing_resolving_outside(claude, codex):
    r = ct.translate(claude, codex)
    assert not any("resolves outside CODEX_HOME" in n for n in r.notes)
    assert not any("is a symlink" in n for n in r.notes)
    assert ct.check(claude, codex) == (True, [])


def test_a_json_valid_stamp_with_a_malformed_shape_is_a_warning_not_a_crash(claude, codex):
    for bad in ('{"version": 1, "entries": {"AGENTS.md": "agents_md"}}',
                '{"version": 2, "entries": {}}',
                '["not", "a", "table"]'):
        ct.translate(claude, codex)
        (codex / ct.STAMP_NAME).write_text(bad)
        ok, problems = ct.check(claude, codex)
        assert not ok
        assert any(p.startswith("warning: unreadable stamp") for p in problems)
        r = ct.translate(claude, codex)
        assert any(n.startswith("warning: unreadable stamp") for n in r.notes)
        assert (codex / "AGENTS.md").read_text().startswith(ct.MARKER)
        assert (codex / ct.BACKUP_DIR_NAME).is_dir()
        json.loads((codex / ct.STAMP_NAME).read_text())


def test_translate_leaves_rules_alone_when_the_backup_dir_escapes(claude, codex, tmp_path):
    external = tmp_path / "external-backup"
    external.mkdir()
    (codex / ct.BACKUP_DIR_NAME).symlink_to(external, target_is_directory=True)
    (codex / "rules").mkdir()
    (codex / "rules" / "old.md").write_text("KEEP\n")
    r = ct.translate(claude, codex)
    assert (codex / "rules" / "old.md").read_text() == "KEEP\n"
    assert r.counts["rules_removed"] == 0
    assert not (external / "rules").exists()
    assert any("resolves outside CODEX_HOME" in n and "rules/ left alone" in n for n in r.notes)


@pytest.mark.parametrize("value", ["true", "yes", "on", "1", "TRUE", "Yes", '"true"', "true # why"])
def test_a_command_is_skipped_for_every_truthy_disable_model_invocation(claude, value):
    (claude / "commands" / "gated.md").write_text(
        f"---\ndescription: gated\ndisable-model-invocation: {value}\n---\n# Gated\n")
    desired = ct.plan(claude)
    assert "skills/gated" not in desired.entries
    assert any(n == "skipped command wrapper gated: disable-model-invocation" for n in desired.notes)


@pytest.mark.parametrize("value", ["false", "no", "off", "0", "", "maybe"])
def test_a_command_stays_a_wrapper_for_every_untruthy_disable_model_invocation(claude, value):
    (claude / "commands" / "open.md").write_text(
        f"---\ndescription: open\ndisable-model-invocation: {value}\n---\n# Open\n")
    desired = ct.plan(claude)
    assert desired.entries["skills/open"]["kind"] == "wrapper"
    assert not any("skipped command wrapper open" in n for n in desired.notes)


def test_translate_counts_what_it_wrote_not_what_it_planned(claude, codex):
    r = ct.translate(claude, codex)
    assert r.counts["skills"] == 5 and r.counts["wrappers"] == 2 and r.counts["agents"] == 3
    assert r.counts["agents_md_bytes"] == (codex / "AGENTS.md").stat().st_size
    assert r.counts["rules"] == 2 and r.counts["wrappers_skipped"] == 2


def test_plan_mirrors_each_roles_codex_render(claude):
    role = claude / "roles" / "Coder"
    role.mkdir(parents=True)
    (role / "role.codex.md").write_bytes(b"# Coder\nbody\n")
    (role / "role.worker.md").write_text("claude-side only\n")
    (claude / "roles" / "_shared" / "fragments").mkdir(parents=True)
    desired = ct.plan(claude)
    assert desired.files["roles/Coder.md"] == b"# Coder\nbody\n"
    assert desired.entries["roles/Coder.md"] == {"kind": "role", "sha256": hashlib.sha256(b"# Coder\nbody\n").hexdigest()}
    assert sorted(k for k in desired.entries if k.startswith("roles/")) == ["roles/Coder.md"]


def test_translate_writes_role_renders_and_prunes_one_whose_source_is_gone(claude, codex):
    for name in ("Coder", "Verifier"):
        (claude / "roles" / name).mkdir(parents=True)
        (claude / "roles" / name / "role.codex.md").write_text(f"# {name}\n")
    ct.translate(claude, codex)
    assert (codex / "roles" / "Coder.md").read_text() == "# Coder\n"
    assert ct.check(claude, codex) == (True, [])
    (claude / "roles" / "Coder" / "role.codex.md").unlink()
    (codex / "roles" / "Verifier.md").write_text("edited by hand\n")
    (claude / "roles" / "Verifier" / "role.codex.md").unlink()
    r = ct.translate(claude, codex)
    assert not (codex / "roles" / "Coder.md").exists()
    assert "removed roles/Coder.md (source gone)" in r.notes
    assert (codex / "roles" / "Verifier.md").read_text() == "edited by hand\n"
    assert ct.check(claude, codex) == (True, [])


def test_render_agents_md_strips_a_units_frontmatter(tmp_path):
    a = tmp_path / "a.md"; a.write_text("---\nkind: fact\nbinds: [build]\ndomain: build\n---\n# A\n\nTRIGGER: when a\n")
    text = ct.render_agents_md([("rules/a.md", a)])
    assert "<!-- BEGIN rules/a.md -->\n# A\n\nTRIGGER: when a\n<!-- END rules/a.md -->\n" in text
    assert "kind: fact" not in text


def test_plan_builds_agents_md_from_the_tripwire_alone(claude):
    for extra in (claude / "rules").glob("*.md"):
        extra.unlink()
    (claude / "rules" / "no-role.md").write_text("# No role\n\nTRIGGER: Load when bare.\n")
    desired = ct.plan(claude)
    assert desired.counts["rules"] == 1
    assert b"TRIGGER: Load when bare." in desired.files["AGENTS.md"]


def test_plan_mirrors_each_roles_profile_codex_render(claude):
    role = claude / "roles" / "Coder"
    role.mkdir(parents=True)
    (role / "role.codex.md").write_bytes(b"# Coder\n")
    (role / "role.codex.back.md").write_bytes(b"# Coder back\n")
    desired = ct.plan(claude)
    assert desired.files["roles/Coder.md"] == b"# Coder\n"
    assert desired.files["roles/Coder.back.md"] == b"# Coder back\n"
    assert desired.entries["roles/Coder.back.md"]["kind"] == "role"
