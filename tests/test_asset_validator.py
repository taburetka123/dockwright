# tests/test_asset_validator.py
"""asset_validator.py unit tests against a fixture mini ~/.claude repo."""
import importlib.util
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

AV_PATH = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "asset_validator.py"


@pytest.fixture(scope="module")
def av():
    spec = importlib.util.spec_from_file_location("asset_validator_under_test", AV_PATH)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so the @dataclass decorator can resolve its module
    # (see test_worktree_prune.py — same gotcha with frozen dataclass + `from
    # __future__ import annotations`).
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mini_repo(tmp_path, monkeypatch):
    # Isolate from any ambient operator config so the fixture's own
    # dockwright/asset-validator.json is the only convention source.
    monkeypatch.delenv("ASSET_VALIDATOR_CONFIG", raising=False)
    (tmp_path / "dockwright").mkdir()
    (tmp_path / "dockwright" / "asset-validator.json").write_text(
        '{"name_prefixes": ["corp-", "dockwright-"], '
        '"command_exempt": ["manager", "tab", "fix"], '
        '"command_exempt_prefixes": [], "skill_exempt": []}\n'
    )
    (tmp_path / "rules").mkdir()
    (tmp_path / "skills" / "corp-good").mkdir(parents=True)
    (tmp_path / "skills" / "badname").mkdir(parents=True)
    (tmp_path / "commands").mkdir()
    (tmp_path / "agents").mkdir()
    (tmp_path / "flows").mkdir()
    (tmp_path / "rules" / "good-rule.md").write_text(
        "# Good rule\n\nTRIGGER: Load when testing.\n\nBody refers to `~/.claude/flows/real-flow.md`.\n"
    )
    (tmp_path / "rules" / "no-trigger.md").write_text("# Legacy\n\nJust prose.\n")
    (tmp_path / "flows" / "real-flow.md").write_text("# Real flow\n")
    (tmp_path / "skills" / "corp-good" / "SKILL.md").write_text(
        "---\nname: corp-good\ndescription: Fine skill.\n---\n\n# Good\nSee `references/notes.md`.\n"
    )
    (tmp_path / "skills" / "corp-good" / "references").mkdir()
    (tmp_path / "skills" / "corp-good" / "references" / "notes.md").write_text("n")
    (tmp_path / "skills" / "badname" / "SKILL.md").write_text(
        "---\nname: wrong\n---\n\n# Bad\n"
    )
    (tmp_path / "commands" / "corp-thing.md").write_text("# Thing\n")
    (tmp_path / "commands" / "tab.md").write_text("# Tab (exempt name)\n")
    (tmp_path / "commands" / "rogue.md").write_text("# Rogue\n")
    (tmp_path / "commands" / "old-alias.md").write_text(
        "# Old\n\nDEPRECATED alias for `corp-thing` (removed next release)\n"
    )
    (tmp_path / "commands" / "dead-alias.md").write_text(
        "# Dead\n\nDEPRECATED alias for `corp-ghost` (removed next release)\n"
    )
    (tmp_path / "agents" / "worker.md").write_text(
        "---\nname: worker\ndescription: A worker.\n---\nBody\n"
    )
    (tmp_path / "agents" / "misnamed.md").write_text(
        "---\nname: other\ndescription: X.\n---\nBody\n"
    )
    (tmp_path / "rules" / "bad-ref.md").write_text(
        "# Bad ref\n\nTRIGGER: x\n\nSee `~/.claude/rules/does-not-exist.md` and `~/.claude/rules/<topic>.md`.\n"
    )
    # --- Skill: reference corpus -------------------------------------------
    (tmp_path / "skills" / "corp-live").mkdir(parents=True)
    (tmp_path / "skills" / "corp-live" / "SKILL.md").write_text(
        "---\nname: corp-live\ndescription: Invocable.\n---\n\n# Live\n")
    (tmp_path / "skills" / "corp-off").mkdir(parents=True)
    (tmp_path / "skills" / "corp-off" / "SKILL.md").write_text(
        "---\nname: corp-off\ndescription: Vendor-disabled.\n"
        "disable-model-invocation: true\n---\n\n# Off\n")
    (tmp_path / "commands" / "corp-cmd.md").write_text(
        "---\nname: corp-cmd\ndescription: Command-backed.\n---\n\n# Cmd\n")
    # plugin skill, cache layout
    plug = tmp_path / "plugins" / "cache" / "mkt" / "pack" / "1.0.0" / "skills" / "thing"
    plug.mkdir(parents=True)
    (plug / "SKILL.md").write_text(
        "---\nname: thing\ndescription: Plugin skill.\n---\n\n# Thing\n")
    # plugin COMMAND, cache layout — the plannotator incident's surviving path is a command
    pcmd = tmp_path / "plugins" / "cache" / "mkt" / "pack" / "1.0.0" / "commands"
    pcmd.mkdir(parents=True)
    (pcmd / "last.md").write_text(
        "---\nname: last\ndescription: Plugin command, invocable.\n---\n\n# Last\n")
    # Two REAL marketplace-checkout shapes, both deliberately unmatched: a
    # marketplace dir used as the plugin root, and the nested plugins/<plugin>/.
    # A checkout carries every plugin the marketplace OFFERS, installed or not, so
    # resolving through either would claim a target the model cannot invoke.
    mkt = tmp_path / "plugins" / "marketplaces" / "solo" / "skills" / "only"
    mkt.mkdir(parents=True)
    (mkt / "SKILL.md").write_text(
        "---\nname: only\ndescription: Marketplace-root layout.\n---\n\n# O\n")
    nest = tmp_path / "plugins" / "marketplaces" / "big" / "plugins" / "inner" / "skills" / "deep"
    nest.mkdir(parents=True)
    (nest / "SKILL.md").write_text(
        "---\nname: deep\ndescription: Nested marketplace layout.\n---\n\n# D\n")
    # plugin whose NAME carries a dash — the majority shape of real installed plugins.
    # The old namespace group `(?:[a-z0-9]+:)?` had no dash, so a reference to any
    # of their skills matched NOTHING — not warned, not resolved, silently
    # uncovered. One enabled leaf and one vendor-disabled leaf so both the resolve
    # and the DISABLED-is-seen directions have a red-provable fixture.
    dash_live = tmp_path / "plugins" / "cache" / "mkt" / "two-part" / "1.0.0" / "skills" / "x"
    dash_live.mkdir(parents=True)
    (dash_live / "SKILL.md").write_text(
        "---\nname: x\ndescription: Dashed-plugin skill.\n---\n\n# X\n")
    dash_off = tmp_path / "plugins" / "cache" / "mkt" / "two-part" / "1.0.0" / "skills" / "off"
    dash_off.mkdir(parents=True)
    (dash_off / "SKILL.md").write_text(
        "---\nname: off\ndescription: Dashed-plugin, vendor-disabled.\n"
        "disable-model-invocation: true\n---\n\n# Off\n")
    # two plugin versions DISAGREEING on invocability
    stale = tmp_path / "plugins" / "cache" / "mkt" / "twoface" / "0.9.0" / "skills" / "split"
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text(
        "---\nname: split\ndescription: Stale version, still enabled.\n---\n\n# Split\n")
    live = tmp_path / "plugins" / "cache" / "mkt" / "twoface" / "1.0.0" / "skills" / "split"
    live.mkdir(parents=True)
    (live / "SKILL.md").write_text(
        "---\nname: split\ndescription: Current version, disabled.\n"
        "disable-model-invocation: true\n---\n\n# Split\n")
    return tmp_path


def _codes_for(warnings, path_fragment):
    return {w.split()[0] for w in warnings if path_fragment in w}


def _warn_codes(av, repo, rel, text):
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text)
    return av.validate_one(str(repo), rel)


def test_skill_ref_to_disabled_skill_warns(av, mini_repo):
    out = _warn_codes(av, mini_repo, "flows/f.md", "Run `Skill: corp-off` now.\n")
    assert any(w.startswith("W-SKILL-DISABLED") and "corp-off" in w for w in out), out


def test_skill_ref_to_nonexistent_skill_warns(av, mini_repo):
    out = _warn_codes(av, mini_repo, "flows/f.md", "Run `Skill: corp-ghost` now.\n")
    assert any(w.startswith("W-SKILL-MISSING") and "corp-ghost" in w for w in out), out


def test_never_guard_mention_does_not_warn(av, mini_repo):
    out = _warn_codes(av, mini_repo, "flows/f.md",
                      "⚠️ NEVER `Skill: corp-off` here — the vendor disables it.\n")
    assert not any("SKILL" in w for w in out), out


def test_prohibition_that_is_not_adjacent_still_warns(av, mini_repo):
    # "never skip X" is a live instruction, not a NEVER-guard: it must be checked.
    out = _warn_codes(av, mini_repo, "flows/f.md", "Never skip `Skill: corp-ghost`.\n")
    assert any(w.startswith("W-SKILL-MISSING") for w in out), out


def test_prose_and_placeholder_shapes_do_not_warn(av, mini_repo):
    out = _warn_codes(av, mini_repo, "flows/f.md",
                      "- Skill: does the description carry the trigger words?\n"
                      "Invoke `Skill: corp-<skill>` on `<target>`.\n"
                      "Instruct the worker to run `Skill: <skill-name>`.\n"
                      "Prompt line: \"Skill: {{skill_prefix}}<skill> on <target>\"\n"
                      "Prose tail: the `plannotator-last` Skill:** `plannotator annotate-last`\n")
    assert not any("SKILL" in w for w in out), out


def test_resolving_skill_command_and_plugin_refs_do_not_warn(av, mini_repo):
    out = _warn_codes(av, mini_repo, "flows/f.md",
                      "`Skill: corp-live`, `Skill: corp-cmd`, `Skill: pack:thing`, "
                      "\"Skill: corp-live\", **Skill: corp-live**\n")
    assert not any("SKILL" in w for w in out), out


def test_plugin_command_ref_resolves(av, mini_repo):
    out = _warn_codes(av, mini_repo, "flows/f.md", "`Skill: pack:last`\n")
    assert not any("SKILL" in w for w in out), out


def test_dashed_plugin_namespace_ref_resolves(av, mini_repo):
    # A plugin whose NAME contains a dash — the majority shape of real installed
    # plugins. Under the old namespace group `(?:[a-z0-9]+:)?` this matched nothing
    # at all, so the reference was never resolved and never counted; the guard was
    # invisible-by-construction for exactly those plugins.
    out = _warn_codes(av, mini_repo, "flows/f.md", "`Skill: two-part:x`\n")
    assert not any("SKILL" in w for w in out), out


def test_dashed_plugin_namespace_disabled_skill_is_seen(av, mini_repo):
    # RED against the old regex: `two-part:off` produced NO match, so no W-SKILL-*
    # ever fired and this assertion failed. Proves the new namespace group both
    # matches a dashed plugin name AND resolves it to the on-disk file (reading its
    # frontmatter to reach the disabled verdict).
    out = _warn_codes(av, mini_repo, "flows/f.md", "`Skill: two-part:off`\n")
    assert any(w.startswith("W-SKILL-DISABLED") and "two-part:off" in w for w in out), out


def test_dashed_plugin_namespace_missing_skill_warns(av, mini_repo):
    # RED against the old regex, same mechanism: a reference to a non-existent
    # skill under a dashed plugin name must warn, not silently pass uncovered —
    # the exact "disabled/unknown reference goes unnoticed" incident the guard
    # exists to catch.
    out = _warn_codes(av, mini_repo, "flows/f.md", "`Skill: two-part:ghost`\n")
    assert any(w.startswith("W-SKILL-MISSING") and "two-part:ghost" in w for w in out), out


def test_dashed_namespace_placeholder_shapes_still_do_not_match(av, mini_repo):
    # The dashed-namespace widening must NOT re-open the documented false-positive
    # classes: a single-token stem and a `<...>` placeholder still match nothing,
    # so a placeholder that happens to sit before a dashed plugin name stays quiet.
    out = _warn_codes(av, mini_repo, "flows/f.md",
                      "Invoke `Skill: corp-<skill>` and `Skill: <skill-name>` and "
                      "consider `Skill: does the plugin carry a colon`.\n")
    assert not any("SKILL" in w for w in out), out


@pytest.mark.parametrize("ref", ["inner:deep", "solo:only"])
def test_marketplace_layouts_are_deliberately_unmatched(av, mini_repo, ref):
    """No marketplace shape resolves — nested `plugins/<plugin>/` included. A
    marketplace CHECKOUT carries every plugin it offers, installed or not (50
    offered / 6 installed on the live host), so matching one reports "resolved"
    for a target the model cannot invoke. That is a false RESOLVE, and it is the
    reason external_plugins/, per-app and marketplace-root were rejected; the
    nested shape is the same shape and gets the same answer."""
    out = _warn_codes(av, mini_repo, "flows/f.md", f"`Skill: {ref}`\n")
    assert any(w.startswith("W-SKILL-MISSING") for w in out), out


def test_each_plugin_pattern_is_the_sole_resolver_of_one_fixture(av, mini_repo, monkeypatch):
    """Delete-one-line sweep (drift-guard-tests.md): removing ANY single pattern
    must make exactly its own fixture go RED — no pattern ships unguarded, and
    none is dead weight kept alive by a sibling. The equality assertion is what
    makes it a gate: a pattern added without its own red proof fails here."""
    probes = {
        "plugins/cache/*/{plugin}/*/{leaf}": "pack:thing",
    }
    original = tuple(av._PLUGIN_LEAF_PATTERNS)
    assert set(probes) == set(original), original
    for pattern, ref in probes.items():
        monkeypatch.setattr(av, "_PLUGIN_LEAF_PATTERNS",
                            tuple(p for p in original if p != pattern))
        out = _warn_codes(av, mini_repo, "flows/f.md", f"`Skill: {ref}`\n")
        assert any(w.startswith("W-SKILL-MISSING") for w in out), (pattern, out)


def test_a_namespaced_ref_never_raises_when_no_plugin_pattern_matches(av, mini_repo, monkeypatch):
    # Found by the sweep above: with the pattern tuple emptied, a namespaced name
    # probes NOTHING, and the message used to index probes[0] — an IndexError
    # escaping validate_one, i.e. the warn-only contract broken by one edit to a
    # constant. The message must degrade, not raise.
    monkeypatch.setattr(av, "_PLUGIN_LEAF_PATTERNS", ())
    out = _warn_codes(av, mini_repo, "flows/f.md", "`Skill: pack:thing`\n")
    assert any(w.startswith("W-SKILL-MISSING") for w in out), out
    assert all("looked in" not in w for w in out), out


def test_missing_skill_warning_names_a_path_it_probed(av, mini_repo):
    out = _warn_codes(av, mini_repo, "flows/f.md", "Run `Skill: corp-ghost` now.\n")
    line = next(w for w in out if w.startswith("W-SKILL-MISSING"))
    assert "skills/corp-ghost/SKILL.md" in line, line


def test_disabled_skill_warning_names_the_sanctioned_never_guard(av, mini_repo):
    out = _warn_codes(av, mini_repo, "flows/f.md", "Run `Skill: corp-off` now.\n")
    line = next(w for w in out if w.startswith("W-SKILL-DISABLED"))
    assert "never" in line and "⛔" in line, line


def test_disagreeing_plugin_versions_warn_rather_than_resolve(av, mini_repo):
    out = _warn_codes(av, mini_repo, "flows/f.md", "`Skill: twoface:split`\n")
    assert any(w.startswith("W-SKILL-DISABLED") for w in out), out


@pytest.mark.parametrize("line", [
    "Load whenever `Skill: corp-off` fires.\n",
    "Whenever `Skill: corp-off` runs, do X.\n",
    "Use the never-guard form whenever `Skill: corp-off` appears.\n",
])
def test_whenever_is_not_a_never_guard(av, mini_repo, line):
    # 'whenever' ENDS in the substring 'never'. Without a word boundary the
    # exemption swallows it and the check silently switches off — and 'whenever'
    # occurs in live assets in exactly the instruction position a reference
    # follows ("Load whenever `Skill: x` is invoked").
    out = _warn_codes(av, mini_repo, "flows/f.md", line)
    assert any(w.startswith("W-SKILL-DISABLED") for w in out), out


def test_real_never_guards_stay_exempt_after_the_boundary_fix(av, mini_repo):
    for line in ("⚠️ NEVER `Skill: corp-off` — the vendor disables it.\n",
                 "— never `Skill: corp-off`\n",
                 "⛔ `Skill: corp-off`\n"):
        out = _warn_codes(av, mini_repo, "flows/f.md", line)
        assert not any("SKILL" in w for w in out), (line, out)


def test_the_emoji_presentation_stop_sign_is_a_never_guard_too(av, mini_repo):
    # ⛔ is U+26D4; the macOS emoji picker inserts U+26D4 + U+FE0F. The remedy text
    # W-SKILL-DISABLED prints says "put 'never' or ⛔ immediately before it" — an
    # author who does exactly that, from the picker, must not still be warned at.
    out = _warn_codes(av, mini_repo, "flows/f.md", "⛔️ `Skill: corp-off`\n")
    assert not any("SKILL" in w for w in out), out


@pytest.mark.parametrize("value", ["true", "True", "TRUE", '"true"', "'true'",
                                   "yes", "on", "1", "y", "Y",
                                   "true # vendor reinstall"])
def test_disable_model_invocation_truthy_spellings_all_warn(av, mini_repo, value):
    # The value is VENDOR-written: a reinstall changing the serializer is exactly
    # the failure this guard exists to catch, so the predicate must not be a
    # denylist of one literal that fails OPEN on the next spelling.
    (mini_repo / "skills" / "corp-vary").mkdir(parents=True, exist_ok=True)
    (mini_repo / "skills" / "corp-vary" / "SKILL.md").write_text(
        f"---\nname: corp-vary\ndescription: V.\n"
        f"disable-model-invocation: {value}\n---\n\n# V\n")
    out = _warn_codes(av, mini_repo, "flows/f.md", "Run `Skill: corp-vary` now.\n")
    assert any(w.startswith("W-SKILL-DISABLED") for w in out), (value, out)


@pytest.mark.parametrize("value", ["false", "False", "", "no", "off", "0",
                                   "false # deliberately invocable"])
def test_disable_model_invocation_falsey_spellings_do_not_warn(av, mini_repo, value):
    (mini_repo / "skills" / "corp-vary").mkdir(parents=True, exist_ok=True)
    (mini_repo / "skills" / "corp-vary" / "SKILL.md").write_text(
        f"---\nname: corp-vary\ndescription: V.\n"
        f"disable-model-invocation: {value}\n---\n\n# V\n")
    out = _warn_codes(av, mini_repo, "flows/f.md", "Run `Skill: corp-vary` now.\n")
    assert not any("SKILL" in w for w in out), (value, out)


def test_disabled_warning_quotes_the_value_the_file_actually_carries(av, mini_repo):
    # _DISABLED_VALUES has five entries precisely because the spelling varies, so a
    # message hardcoding `true` sends the operator grepping for a string the file
    # does not contain.
    (mini_repo / "skills" / "corp-yes").mkdir(parents=True)
    (mini_repo / "skills" / "corp-yes" / "SKILL.md").write_text(
        "---\nname: corp-yes\ndescription: Y.\ndisable-model-invocation: yes\n---\n\n# Y\n")
    out = _warn_codes(av, mini_repo, "flows/f.md", "Run `Skill: corp-yes` now.\n")
    line = next(w for w in out if w.startswith("W-SKILL-DISABLED"))
    assert "disable-model-invocation: yes at" in line, line
    assert "true" not in line, line


def _write_invalid_utf8(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"---\nname: corp-bin\ndescription: \xff\xfe not utf-8\n---\n\n# B\n")


def test_a_non_utf8_skill_target_never_raises(av, mini_repo):
    # _disabling_value opens VENDOR-authored files the operator does not control.
    # A UnicodeDecodeError is a ValueError, NOT an OSError — it would escape
    # validate_one and break the warn-only, always-exit-0 contract.
    _write_invalid_utf8(mini_repo / "skills" / "corp-bin" / "SKILL.md")
    out = _warn_codes(av, mini_repo, "flows/f.md", "Run `Skill: corp-bin` now.\n")
    assert not any(w.startswith("W-SKILL-MISSING") for w in out), out


def test_a_non_utf8_asset_file_never_raises(av, mini_repo):
    (mini_repo / "rules" / "binary-rule.md").write_bytes(
        b"# Rule\n\nTRIGGER: x\n\n\xff\xfe binary tail\n")
    assert av.validate_files(str(mini_repo), ["rules/binary-rule.md"]) == []


def test_cli_exits_zero_on_a_non_utf8_target(av, mini_repo, capsys):
    _write_invalid_utf8(mini_repo / "skills" / "corp-bin" / "SKILL.md")
    (mini_repo / "flows" / "f.md").write_text("Run `Skill: corp-bin` now.\n")
    assert av.main(["--repo", str(mini_repo), "--files", "flows/f.md"]) == 0


class TestChecks:
    def test_clean_assets_produce_no_warnings(self, av, mini_repo):
        files = ["rules/good-rule.md", "skills/corp-good/SKILL.md",
                 "commands/corp-thing.md", "commands/tab.md",
                 "agents/worker.md", "commands/old-alias.md"]
        assert av.validate_files(str(mini_repo), files) == []

    def test_rule_missing_trigger(self, av, mini_repo):
        w = av.validate_files(str(mini_repo), ["rules/no-trigger.md"])
        assert _codes_for(w, "no-trigger.md") == {"W-RULE-TRIGGER"}

    def test_skill_name_mismatch_and_naming(self, av, mini_repo):
        w = av.validate_files(str(mini_repo), ["skills/badname/SKILL.md"])
        codes = _codes_for(w, "badname/SKILL.md")
        assert "W-NAME-MISMATCH" in codes and "W-NAMING" in codes
        assert "W-FRONTMATTER" in codes  # description missing

    def test_command_naming_with_exempt(self, av, mini_repo):
        w = av.validate_files(str(mini_repo), ["commands/rogue.md", "commands/tab.md"])
        assert _codes_for(w, "rogue.md") == {"W-NAMING"}
        assert _codes_for(w, "tab.md") == set()

    def test_missing_ref_warns_placeholder_skipped(self, av, mini_repo):
        w = av.validate_files(str(mini_repo), ["rules/bad-ref.md"])
        assert _codes_for(w, "bad-ref.md") == {"W-REF-MISSING"}
        assert not any("<topic>" in line for line in w)

    def test_agent_name_mismatch(self, av, mini_repo):
        w = av.validate_files(str(mini_repo), ["agents/misnamed.md"])
        assert _codes_for(w, "misnamed.md") == {"W-NAME-MISMATCH"}

    def test_dead_alias_target(self, av, mini_repo):
        w = av.validate_files(str(mini_repo), ["commands/dead-alias.md"])
        assert _codes_for(w, "dead-alias.md") == {"W-ALIAS-TARGET"}

    def test_non_asset_paths_ignored(self, av, mini_repo):
        assert av.validate_files(str(mini_repo), ["scripts/foo.sh", "presets/x.json"]) == []


class TestActionableMessages:
    """These lines now land in-session on every ~/.claude asset edit, so each one
    must say what to DO, not only what is wrong — and EVERY code the module can
    emit is covered, so a new code cannot ship without a remedy."""

    def _sample_warnings(self, av, mini_repo):
        (mini_repo / "skills" / "corp-noname").mkdir(parents=True)
        (mini_repo / "skills" / "corp-noname" / "SKILL.md").write_text(
            "---\nname: corp-noname\n")           # unterminated fence
        (mini_repo / "rules" / "untitled.md").write_text("TRIGGER: x\n\nProse.\n")
        (mini_repo / "flows" / "refs.md").write_text(
            "See `~/.claude/rules/gone.md`, run `Skill: corp-off` and "
            "`Skill: corp-ghost`.\n")
        return av.validate_files(str(mini_repo), [
            "rules/no-trigger.md", "rules/untitled.md", "skills/badname/SKILL.md",
            "skills/corp-noname/SKILL.md", "commands/rogue.md",
            "commands/dead-alias.md", "agents/misnamed.md", "flows/refs.md",
        ])

    def test_every_emitted_code_is_exercised_and_carries_a_remedy(self, av, mini_repo):
        emitted = set(re.findall(r"\bW-[A-Z]+(?:-[A-Z]+)*\b", AV_PATH.read_text()))
        warnings = self._sample_warnings(av, mini_repo)
        seen = {w.split()[0] for w in warnings}
        assert seen == emitted, f"uncovered codes: {emitted - seen}"
        missing_remedy = [w for w in warnings if " — " not in w]
        assert missing_remedy == [], missing_remedy

    def test_remedies_stay_one_line_and_terse(self, av, mini_repo):
        for w in self._sample_warnings(av, mini_repo):
            assert "\n" not in w, w
            assert len(w) < 240, (len(w), w)


class TestCliAndGit:
    def test_staged_mode_and_always_exit_zero(self, av, mini_repo, capsys):
        subprocess.run(["git", "init", "-q"], cwd=mini_repo, check=True)
        subprocess.run(["git", "add", "rules/no-trigger.md"], cwd=mini_repo, check=True)
        rc = av.main(["--repo", str(mini_repo), "--staged"])
        out = capsys.readouterr().out
        assert rc == 0 and "W-RULE-TRIGGER" in out

    def test_strict_exits_one_on_warnings(self, av, mini_repo):
        assert av.main(["--repo", str(mini_repo), "--files", "rules/no-trigger.md", "--strict"]) == 1

    def test_all_mode_walks_asset_dirs(self, av, mini_repo, capsys):
        rc = av.main(["--repo", str(mini_repo), "--all"])
        out = capsys.readouterr().out
        assert rc == 0 and "no-trigger.md" in out and "misnamed.md" in out

    def test_warning_order_does_not_depend_on_the_hash_seed(self, mini_repo):
        """str hashing is salted per process, so iterating a bare set of refs
        reorders the output between runs. The commit hook captures this stdout
        whole and shows it to the author; a reshuffle for no reason reads as the
        tool having found something new. Two seeds, byte-identical output."""
        (mini_repo / "flows" / "many-refs.md").parent.mkdir(parents=True, exist_ok=True)
        (mini_repo / "flows" / "many-refs.md").write_text(
            "".join(f"See `~/.claude/rules/gone-{i}.md`.\n" for i in range(12)))
        runs = []
        for seed in ("0", "1"):
            proc = subprocess.run(
                [sys.executable, str(AV_PATH), "--repo", str(mini_repo),
                 "--files", "flows/many-refs.md"],
                capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": seed})
            assert proc.returncode == 0, proc.stderr
            runs.append(proc.stdout)
        assert runs[0].count("W-REF-MISSING") == 12, runs[0]
        assert runs[0] == runs[1], "output order depends on PYTHONHASHSEED"

    @pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0,
                        reason="root bypasses directory permissions")
    def test_all_mode_exits_zero_on_an_unreadable_asset_dir(self, av, mini_repo, capsys):
        # --all is the only mode that walks directories it was not handed, so it is
        # the only one that can meet an unreadable one. A bare os.listdir raises
        # PermissionError out of main() -> exit 1 with no --strict, breaking the
        # warn-only contract exactly as an unreadable asset file would.
        (mini_repo / "rules").chmod(0o000)
        try:
            rc = av.main(["--repo", str(mini_repo), "--all"])
        finally:
            (mini_repo / "rules").chmod(0o755)
        assert rc == 0
        assert "misnamed.md" in capsys.readouterr().out   # the readable dirs still scanned


class TestAliasParsing:
    """_ALIAS_RE must parse all three live deprecation phrasings, extract the
    correct target (never the word 'for'), pass a live target and warn a dead one."""

    PHRASINGS = {
        "for-backtick": "# X\n\nDEPRECATED alias for `{t}` (removed next release)\n",
        "for-slash-backtick": "# X\n\nDeprecated alias for `/{t}` (removed next release)\n",
        "emdash-use-slash": "# X\n\nDEPRECATED alias — use /{t} (removed next release)\n",
    }

    def _extracted(self, av, text):
        m = av._ALIAS_RE.search(text)
        return m.group(1).lstrip("/").split(":")[-1] if m else None

    @pytest.mark.parametrize("phrasing", list(PHRASINGS))
    def test_target_extracted_never_for(self, av, phrasing):
        text = self.PHRASINGS[phrasing].format(t="corp-thing")
        target = self._extracted(av, text)
        assert target == "corp-thing", f"{phrasing}: got {target!r}"
        assert target != "for"

    @pytest.mark.parametrize("phrasing", list(PHRASINGS))
    def test_live_target_no_warning(self, av, mini_repo, phrasing):
        # corp-thing exists in mini_repo (commands/corp-thing.md).
        text = self.PHRASINGS[phrasing].format(t="corp-thing")
        assert av._check_alias(str(mini_repo), "commands/some-alias.md", text) == []

    @pytest.mark.parametrize("phrasing", list(PHRASINGS))
    def test_dead_target_warns(self, av, mini_repo, phrasing):
        text = self.PHRASINGS[phrasing].format(t="corp-ghost")
        w = av._check_alias(str(mini_repo), "commands/some-alias.md", text)
        assert len(w) == 1 and w[0].startswith("W-ALIAS-TARGET")
        assert "corp-ghost" in w[0] and "'for'" not in w[0]

    def test_bare_target_orchestrator_guide_not_worker(self, av):
        # Live false positive: a bare (undelimited) target followed by a
        # parenthetical containing "manager/worker" — the slash-delimiter-required
        # form captured 'worker' from the parenthetical instead of the real target.
        text = ("DEPRECATED alias for dockwright-orchestrator-guide (the product "
                "manual for the manager/worker orchestration tool)")
        target = self._extracted(av, text)
        assert target == "dockwright-orchestrator-guide"
        assert target != "worker"

    def test_bare_target_sentence_dot_not_captured(self, av, mini_repo):
        # Live false positive: a bare target ending a sentence — the `.`-inclusive
        # capture class swallowed the sentence-final dot, so the target became
        # 'dockwright-gardener-digest.' and a live skill warned W-ALIAS-TARGET.
        (mini_repo / "skills" / "dockwright-gardener-digest").mkdir(parents=True)
        (mini_repo / "skills" / "dockwright-gardener-digest" / "SKILL.md").write_text(
            "---\nname: dockwright-gardener-digest\ndescription: D.\n---\n\n# D\n"
        )
        text = "DEPRECATED alias for dockwright-gardener-digest. Removed next release."
        assert self._extracted(av, text) == "dockwright-gardener-digest"
        assert av._check_alias(str(mini_repo), "commands/some-alias.md", text) == []

    def test_bare_target_todo_not_todos(self, av):
        # Live false positive: a bare target followed by a parenthetical containing
        # a path with a slash ("~/.claude/todos/") — the old regex captured 'todos'.
        text = ('DEPRECATED alias for dockwright-todo (save a todo to '
                '~/.claude/todos/; triggers on "/corp-todo <text>")')
        target = self._extracted(av, text)
        assert target == "dockwright-todo"
        assert target != "todos"


class TestRedosAndTimeout:
    def test_alias_regex_linear_on_pathological_whitespace(self, av, mini_repo):
        # The pre-fix regex had two adjacent unbounded \s* quantifiers in the
        # optional connective group — O(n^2) on a long whitespace run right after
        # the marker (7.5s at 30k spaces). The linear form must stay fast.
        text = "DEPRECATED alias" + " " * 200_000 + "\nno target here"
        (mini_repo / "rules" / "patho.md").write_text(text)
        start = time.perf_counter()
        av._ALIAS_RE.search(text)
        av.validate_files(str(mini_repo), ["rules/patho.md"])
        assert time.perf_counter() - start < 2.0

    def test_skill_regex_linear_on_pathological_input(self, av):
        payload = "Skill: " + "a-" * 50_000 + "<"
        start = time.monotonic()
        assert av._SKILL_INVOKE_RE.search(payload) is None
        assert time.monotonic() - start < 1.0

    def test_skill_regex_linear_on_pathological_namespaced_input(self, av):
        # The dashed-namespace group added a SECOND nested `[a-z0-9]+(?:-[a-z0-9]+)*`
        # before the `:`. Measure — do not assume — that a long kebab run on BOTH
        # sides of the `:` (namespace token then skill token, both failing the
        # trailing lookahead) stays linear rather than multiplying.
        payload = "Skill: " + "a-" * 50_000 + ":" + "a-" * 50_000 + "<"
        start = time.monotonic()
        assert av._SKILL_INVOKE_RE.search(payload) is None
        assert time.monotonic() - start < 1.0

    def test_max_seconds_fail_soft_under_hang(self, av, mini_repo):
        env = {k: v for k, v in os.environ.items() if k != "ASSET_VALIDATOR_CONFIG"}
        env["ASSET_VALIDATOR_TEST_SLEEP"] = "5"
        start = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, str(AV_PATH), "--repo", str(mini_repo),
             "--files", "rules/x.md", "--max-seconds", "1"],
            env=env, capture_output=True, timeout=10,
        )
        elapsed = time.perf_counter() - start
        assert proc.returncode == 0
        assert proc.stdout in (b"", "")
        assert elapsed < 4


class TestConfig:
    def _skill(self, root, name):
        (root / "skills" / name).mkdir(parents=True)
        (root / "skills" / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: A skill.\n---\n\n# {name}\n"
        )

    def test_defaults_without_config_prefer_dockwright(self, av, tmp_path, monkeypatch):
        # No dockwright/asset-validator.json and no env override: only the
        # generic 'dockwright-' default prefix applies, so 'corp-' warns.
        monkeypatch.delenv("ASSET_VALIDATOR_CONFIG", raising=False)
        self._skill(tmp_path, "corp-foo")
        self._skill(tmp_path, "dockwright-foo")
        kz = av.validate_files(str(tmp_path), ["skills/corp-foo/SKILL.md"])
        dw = av.validate_files(str(tmp_path), ["skills/dockwright-foo/SKILL.md"])
        assert "W-NAMING" in _codes_for(kz, "corp-foo")
        assert "W-NAMING" not in _codes_for(dw, "dockwright-foo")

    def test_settings_type_guard_rejects_non_list(self, av):
        # A null (would TypeError on tuple(None)) and a bare string (would split
        # into per-character prefixes) both fall back to the generic default.
        assert av._settings({"name_prefixes": None})["name_prefixes"] == av.NAME_PREFIXES
        assert av._settings({"name_prefixes": "dockwright-"})["name_prefixes"] == av.NAME_PREFIXES

    def test_env_var_config_override_honored(self, av, tmp_path, monkeypatch):
        # ASSET_VALIDATOR_CONFIG replaces name_prefixes with ['acme-'].
        cfg = tmp_path / "custom.json"
        cfg.write_text('{"name_prefixes": ["acme-"]}\n')
        monkeypatch.setenv("ASSET_VALIDATOR_CONFIG", str(cfg))
        repo = tmp_path / "repo"
        self._skill(repo, "acme-thing")
        self._skill(repo, "corp-thing")
        acme = av.validate_files(str(repo), ["skills/acme-thing/SKILL.md"])
        kz = av.validate_files(str(repo), ["skills/corp-thing/SKILL.md"])
        assert "W-NAMING" not in _codes_for(acme, "acme-thing")
        assert "W-NAMING" in _codes_for(kz, "corp-thing")
