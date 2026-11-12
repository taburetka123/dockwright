import os
import subprocess

import pytest

from dockwright import config, gh_cli

URL = "https://github.com/acme/widgets/pull/7"


@pytest.fixture
def gh_cfg(tmp_path, monkeypatch):
    cfg = tmp_path / "dockwright.toml"
    cfg.write_text('[gh]\nbinary = "/opt/fake/gh"\ntoken_ttl_sec = 600\n[gh.logins]\nacme = "alice"\nwork = "bob"\n')
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(cfg))
    gh_cli._TOKENS.clear()
    return cfg


class Runs:
    def __init__(self, token="tok-alice", token_rc=0, pr_stdout='{"state": "MERGED"}', pr_rc=0):
        self.calls = []
        self.token, self.token_rc, self.pr_stdout, self.pr_rc = token, token_rc, pr_stdout, pr_rc

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        if argv[1:3] == ["auth", "token"]:
            return subprocess.CompletedProcess(argv, self.token_rc, stdout=self.token + "\n", stderr="")
        return subprocess.CompletedProcess(argv, self.pr_rc, stdout=self.pr_stdout, stderr="boom")


def test_owner_of_reads_the_url_owner():
    assert gh_cli.owner_of(URL) == "acme"
    assert gh_cli.owner_of("https://github.com/acme") is None
    assert gh_cli.owner_of("not a url") is None


@pytest.mark.parametrize("cwd", [None, "/Users/x/projects/work/worker", "/gone/worktree"])
def test_with_gh_configured_the_token_follows_the_owner_and_cwd_is_never_passed(gh_cfg, monkeypatch, cwd, tmp_path):
    runs = Runs()
    monkeypatch.setattr(gh_cli.subprocess, "run", runs)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "inherited")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert gh_cli.pr_json(URL, "state", cwd) == {"state": "MERGED"}
    token_call, pr_call = runs.calls
    assert token_call[0] == ["/opt/fake/gh", "auth", "token", "-h", "github.com", "-u", "alice"]
    assert "GITHUB_TOKEN" not in token_call[1]["env"] and "GH_TOKEN" not in token_call[1]["env"]
    assert "GITHUB_TOKEN" not in pr_call[1]["env"]
    assert pr_call[0] == ["/opt/fake/gh", "pr", "view", URL, "--json", "state"]
    assert pr_call[1]["cwd"] is None and pr_call[1]["env"]["GH_TOKEN"] == "tok-alice"
    assert "GH_TOKEN" not in os.environ


def test_the_token_is_cached_per_login_until_the_ttl(gh_cfg, monkeypatch):
    runs = Runs()
    monkeypatch.setattr(gh_cli.subprocess, "run", runs)
    clock = [1000.0]
    monkeypatch.setattr(gh_cli.time, "time", lambda: clock[0])
    gh_cli.env_for(URL)
    gh_cli.env_for(URL)
    assert sum(1 for c, _ in runs.calls if c[1:3] == ["auth", "token"]) == 1
    clock[0] += 601
    gh_cli.env_for(URL)
    assert sum(1 for c, _ in runs.calls if c[1:3] == ["auth", "token"]) == 2


def test_an_owner_outside_the_table_is_refused_by_name_and_gh_never_runs(gh_cfg, monkeypatch):
    runs = Runs()
    monkeypatch.setattr(gh_cli.subprocess, "run", runs)
    with pytest.raises(ValueError, match="no gh login configured for owner other"):
        gh_cli.pr_json("https://github.com/other/r/pull/1", "state")
    assert runs.calls == []


def test_an_empty_token_is_refused_without_leaking_anything(gh_cfg, monkeypatch):
    runs = Runs(token="", token_rc=1)
    monkeypatch.setattr(gh_cli.subprocess, "run", runs)
    with pytest.raises(ValueError, match="gh has no token for login alice"):
        gh_cli.env_for(URL)


def test_without_gh_configured_the_call_is_byte_for_byte_todays(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(tmp_path / "absent.toml"))
    runs = Runs()
    monkeypatch.setattr(gh_cli.subprocess, "run", runs)
    repo = tmp_path / "repo"
    repo.mkdir()
    assert gh_cli.pr_json(URL, "state,isDraft", str(repo)) == {"state": "MERGED"}
    (argv, kw), = runs.calls
    assert argv == ["gh", "pr", "view", URL, "--json", "state,isDraft"]
    assert kw == {"capture_output": True, "text": True, "timeout": 30, "cwd": str(repo), "env": None}
    assert gh_cli.pr_json(URL, "state", str(tmp_path / "gone")) == {"state": "MERGED"}
    assert runs.calls[-1][1]["cwd"] is None


def test_run_text_takes_the_url_from_argv_and_returns_stdout_or_none(gh_cfg, monkeypatch):
    runs = Runs()
    monkeypatch.setattr(gh_cli.subprocess, "run", runs)
    assert gh_cli.run_text(["pr", "view", URL, "--json", "state,mergedAt"]) == '{"state": "MERGED"}'
    runs.pr_rc = 1
    assert gh_cli.run_text(["pr", "view", URL, "--json", "state,mergedAt"]) is None
    with pytest.raises(ValueError):
        gh_cli.run_text(["api", "user"])


@pytest.mark.parametrize("stdout,rc,message", [("{}", 1, "gh exited 1"), ("not json", 0, "gh printed no JSON")])
def test_pr_json_keeps_todays_error_texts(gh_cfg, monkeypatch, stdout, rc, message):
    monkeypatch.setattr(gh_cli.subprocess, "run", Runs(pr_stdout=stdout, pr_rc=rc))
    with pytest.raises(ValueError, match=message):
        gh_cli.pr_json(URL, "state")


def test_run_json_runs_gh_with_the_owners_token_and_parses_json(gh_cfg, monkeypatch):
    runs = Runs(pr_stdout='[{"databaseId": 7}]')
    monkeypatch.setattr(subprocess, "run", runs)
    assert gh_cli.run_json("acme", ["run", "list", "--json", "databaseId"]) == [{"databaseId": 7}]
    argv, kw = runs.calls[-1]
    assert argv == ["/opt/fake/gh", "run", "list", "--json", "databaseId"] and kw["env"]["GH_TOKEN"] == "tok-alice"
    assert kw["timeout"] == 30


def test_run_json_returns_none_on_a_failed_or_unparsable_call(gh_cfg, monkeypatch):
    monkeypatch.setattr(subprocess, "run", Runs(pr_rc=1))
    assert gh_cli.run_json("acme", ["run", "list"]) is None
    monkeypatch.setattr(subprocess, "run", Runs(pr_stdout="not json"))
    assert gh_cli.run_json("acme", ["run", "list"]) is None


def test_run_json_returns_none_for_an_owner_without_a_login(gh_cfg, monkeypatch):
    runs = Runs()
    monkeypatch.setattr(subprocess, "run", runs)
    assert gh_cli.run_json("nobody", ["run", "list"]) is None
    assert runs.calls == []
