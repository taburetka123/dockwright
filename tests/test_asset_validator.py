# tests/test_asset_validator.py
"""asset_validator.py unit tests against a fixture mini ~/.claude repo."""
import importlib.util
import os
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
    return tmp_path


def _codes_for(warnings, path_fragment):
    return {w.split()[0] for w in warnings if path_fragment in w}


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
