import pytest

from dockwright.permission_policy import symlink_tool_results_hint

CONFIG = "/r/.claude-b"


def _rp(token):
    prefix = "/r/.claude-b/projects/"
    if token.startswith(prefix):
        return "/r/.claude/projects/" + token[len(prefix):]
    return token


def test_a_cp_of_a_tool_result_through_the_config_symlink_names_the_real_path():
    cmd = "cp /r/.claude-b/projects/-r-x/s1/tool-results/mcp-q.txt /r/.claude/scratch/k/x"
    assert symlink_tool_results_hint(cmd, CONFIG, realpath=_rp) == (
        "/r/.claude/projects/-r-x/s1/tool-results/mcp-q.txt")


def test_a_tilde_token_expands_against_home_before_the_check(monkeypatch):
    monkeypatch.setenv("HOME", "/r")
    assert symlink_tool_results_hint("cp ~/.claude-b/projects/p/s/tool-results/f.txt x", CONFIG, realpath=_rp) == (
        "/r/.claude/projects/p/s/tool-results/f.txt")


def test_a_chained_command_hints_on_its_first_symlink_token():
    cmd = "cp /r/.claude-b/projects/p/s/tool-results/f.txt x; head -c 400 x"
    assert symlink_tool_results_hint(cmd, CONFIG, realpath=_rp).endswith("/tool-results/f.txt")


def test_the_real_path_gets_no_hint():
    assert symlink_tool_results_hint("cp /r/.claude/projects/p/s/tool-results/f.txt x", CONFIG, realpath=_rp) is None


def test_a_config_dir_that_is_not_a_symlink_gets_no_hint():
    assert symlink_tool_results_hint("cp /r/.claude/projects/p/s/tool-results/f.txt x", "/r/.claude",
                                     realpath=_rp) is None


def test_a_trailing_slash_on_the_config_dir_still_matches():
    cmd = "cp /r/.claude-b/projects/p/s/tool-results/f.txt x"
    assert symlink_tool_results_hint(cmd, CONFIG + "/", realpath=_rp) == "/r/.claude/projects/p/s/tool-results/f.txt"


@pytest.mark.parametrize("cmd", [
    "cp ~/.ssh/id_rsa /tmp/x",
    "rm -rf /r/.claude-b/projects/p",
    "cp /r/.claude-b/settings.json x",
    "cat $(echo /r/.claude-b/projects/p/s/tool-results/f.txt)",
    "echo 'unterminated",
    "",
])
def test_other_commands_and_unparsable_commands_get_no_hint(cmd):
    assert symlink_tool_results_hint(cmd, CONFIG, realpath=_rp) is None
