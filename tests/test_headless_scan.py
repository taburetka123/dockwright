"""The spawn scanner, proven by the shapes that defeated its predecessor.

Every case below is a REAL blind spot found in the previous implementation, most
of them by an adversarial reviewer rather than by its author. They are kept as
tests rather than as a changelog entry because the failure mode is recurrence: it
grew a new blind spot every time somebody looked, which is the signature of
enumerating shapes instead of parsing.

`~/.claude/rules/drift-guard-tests.md` §ADD-ONE is the bar — a guard dies by
being OVERRIDDEN far more often than by being deleted, so most of these add
something rather than remove it.
"""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "deploy", "scripts", "headless_scan.py")


def _module():
    """The deployed script, loaded for the pins that must read real constants.

    It is a standalone stdlib-only script with no module-level path binding, so
    a plain spec_from_file_location is safe here (contrast `stale_monitor.py`,
    which derives ROOT/ACTIVE/CLOSED from HOME at import time)."""
    spec = importlib.util.spec_from_file_location("headless_scan", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _scan(*roots):
    proc = subprocess.run([sys.executable, SCRIPT, "--json", *map(str, roots)],
                          capture_output=True, text=True)
    return proc.returncode, json.loads(proc.stdout)


def _write(tmp_path, name, body, mode=0o644):
    path = tmp_path / name
    path.write_text(body)
    path.chmod(mode)
    return path


# --- the nine historical blind spots ------------------------------------------

BLIND_SPOTS = [
    ("shell-backslash-continuation", "x.sh",
     '#!/bin/bash\nclaude -p \\\n  --model m\n'),
    ("python-multiline-argv-list", "x.py",
     'import subprocess\ncmd = [\n    "claude",\n    "-p",\n    p,\n]\nsubprocess.run(cmd)\n'),
    ("binary-named-through-a-variable", "x.py",
     'import subprocess\nsubprocess.run([claude_bin, "-p", P])\n'),
    ("equals-form", "x.sh", 'claude -p="x"\n'),
    ("upper-case-shell-variable", "x.sh", 'write text "$CLAUDE -p \\"$S\\""\n'),
    ("stray-fence-inside-a-comment", "x.py",
     'import subprocess\n# a stray fence in a comment: it \'\'\' here\n'
     'subprocess.run(["claude", "-p", "x"])\n'),
    ("os-popen-lowercase-attr", "x.py", 'import os\nos.popen("claude -p x")\n'),
    ("pexpect-spawn", "x.py", 'import pexpect\npexpect.spawn("claude -p x")\n'),
    ("no-extension-no-exec-bit", "runner", 'claude -p "x"\n'),
]


@pytest.mark.parametrize("label,name,body", BLIND_SPOTS, ids=[b[0] for b in BLIND_SPOTS])
def test_every_historical_blind_spot_is_seen(tmp_path, label, name, body):
    """Each of these ran unhardened while the old scanner reported a clean tree."""
    _write(tmp_path, name, body)
    code, report = _scan(tmp_path)
    assert code == 1, f"blind again on: {label}"
    assert report["problems"], label


@pytest.mark.parametrize("name,body", [
    ("mcp.json", '{"command":"claude","args":["-p","go"]}\n'),
    ("a.js", 'const cfg = {command: "claude", args: ["-p", u]};\n'),
    # NOTE: a TOML `bin = "claude"` is deliberately NOT parametrized here. It
    # rides S2 (`=` is a shell separator, so the quoted token is at command
    # position in a non-Python file) and therefore passes with this guard DEAD —
    # a vacuous case, which is the coincidence-detector shape this suite exists
    # to avoid. It is covered by the ADD-ONE corpus instead.
    ("cfg.py", 'CLAUDE_BIN = "claude"\nARGS = ["-p", u]\n'),
])
def test_a_quoted_VALUE_after_a_colon_or_equals_is_an_element(tmp_path, name, body):
    """The sub-form the round-6 unification lost, found by attacking the one rule
    flagged as repeat-prone. `_is_element` strips the quotes and then reads the
    character underneath, which for a quoted value is `:` or `=` — neither of
    which is list punctuation — so a config naming its binary reported 0 sites.

    Immediate quote-adjacency is judged BEFORE the strip now, as a disjunct."""
    _write(tmp_path, name, body)
    code, report = _scan(tmp_path)
    assert code == 1, report
    assert report["sites"], report


@pytest.mark.parametrize("name,body", [
    ("Makefile", 'review:\n\tclaude -p "x"\n'),
    ("ci.yml", 'jobs:\n  a:\n    steps:\n      - run: claude -p "x"\n'),
    ("job.plist", '<string>/usr/bin/claude</string>\n<string>-p</string>\n'),
])
def test_files_the_old_extension_test_skipped_are_scanned(tmp_path, name, body):
    """The predecessor required an extension match or the exec bit, so a spawn in
    a Makefile or a CI workflow was invisible. Scanning everything and skipping by
    a pinned list means a mis-maintained list ADDS files to the guarded set."""
    _write(tmp_path, name, body)
    code, _ = _scan(tmp_path)
    assert code == 1


# --- self-exemption: the guard blinded by a comment ----------------------------

def test_a_comment_naming_the_launcher_cannot_mark_its_own_line_safe(tmp_path):
    """`drift-guard-tests.md`'s headline anti-pattern, which the predecessor
    reproduced inside the guard written to implement that rule: it computed
    `routed` over RAW lines, so a TODO mentioning the launcher scored the spawn
    as already-routed and the run exited 0."""
    _write(tmp_path, "x.sh",
           '#!/bin/bash\nclaude -p "/skill $U" --model m # TODO: move to headless_spawn.py later\n')
    code, report = _scan(tmp_path)
    assert code == 1


def test_a_bare_wrapper_marker_cannot_exempt_anything(tmp_path):
    """A bare `# headless-lockdown: wrapper` used to exempt any spawn at all,
    including one carrying --dangerously-skip-permissions: detail was demanded
    only for the other two verdicts."""
    _write(tmp_path, "x.sh",
           '#!/bin/bash\n# headless-lockdown: wrapper\n'
           'claude -p "x" --dangerously-skip-permissions\n')
    code, _ = _scan(tmp_path)
    assert code == 1


def test_a_wrapper_claim_must_be_true_not_merely_asserted(tmp_path):
    """`wrapper` is the one verdict that is checkable, so it is checked."""
    _write(tmp_path, "x.sh",
           '#!/bin/bash\n# headless-lockdown: wrapper(honest, promise)\nclaude -p "x"\n')
    code, report = _scan(tmp_path)
    assert code == 1
    assert any("does not invoke" in why for why in
               [p["why"] for p in report["problems"]])


@pytest.mark.parametrize("marker,ok", [
    ("# headless-lockdown: exempt(attended) — a human watches this tab", True),
    ("# headless-lockdown: verified-by-test(tests/x.py) — argv asserted there", True),
    ("# headless-lockdown: exempt()", False),
    ("# headless-lockdown: exempt", False),
    ("# lockdown is fine here honestly", False),
])
def test_an_exemption_needs_a_verdict_and_a_reason(tmp_path, marker, ok):
    _write(tmp_path, "x.sh", f'#!/bin/bash\n{marker}\nclaude -p "x"\n')
    code, _ = _scan(tmp_path)
    assert (code == 0) == ok


def test_a_marker_cannot_drift_onto_a_site_added_below_it(tmp_path):
    _write(tmp_path, "x.sh",
           '#!/bin/bash\n# headless-lockdown: exempt(attended) — for the one below\n'
           'echo unrelated\n\nclaude -p "x"\n')
    code, _ = _scan(tmp_path)
    assert code == 1


def test_a_python_marker_cannot_drift_across_TRAILING_comment_lines(tmp_path):
    """The `.sh` case above passed while the property was BROKEN for Python.

    A marker block is a contiguous run of comment lines — but `tokenize`'s COMMENT
    stream also carries TRAILING comments, so every line of executed code that
    happened to end in `# …` counted as part of the run. An exemption written for
    one site silently covered a `--dangerously-skip-permissions` spawn eleven
    lines below it. The block is whole-line comments only, in every language now.
    """
    _write(tmp_path, "x.py",
           'import subprocess\n'
           '# headless-lockdown: exempt(attended) — a human watches THIS one\n'
           'subprocess.run(["claude", "-p", "safe"])  # the exempted site\n'
           'x = 1  # trailing comment\n'
           'y = 2  # trailing comment\n'
           'subprocess.run(["claude", "-p", u, "--dangerously-skip-permissions"])\n')
    code, report = _scan(tmp_path)
    assert code == 1, report
    unmarked = [s for s in report["sites"] if s["line"] == 6]
    assert unmarked and unmarked[0]["verdict"] is None, report["sites"]


# --- fail closed ----------------------------------------------------------------

def test_an_unparseable_python_file_is_reported_not_skipped(tmp_path):
    """The whole reason this scanner exists in this shape: a green number for a
    file nobody could read is worse than no scanner at all."""
    _write(tmp_path, "broken.py", 'def f(:\n    subprocess.run(["claude","-p",x])\n')
    code, report = _scan(tmp_path)
    assert code == 1
    assert report["unscannable"] and "parse failed" in report["unscannable"][0]["why"]


def test_an_undecodable_file_is_reported_not_skipped(tmp_path):
    path = tmp_path / "weird.conf"
    path.write_bytes(b"\xff\xfe\x00\x01claude -p x\x00")
    code, report = _scan(tmp_path)
    assert code == 1
    assert report["unscannable"]


def test_a_missing_root_fails_loud(tmp_path):
    proc = subprocess.run([sys.executable, SCRIPT, str(tmp_path / "nope")],
                          capture_output=True, text=True)
    assert proc.returncode == 2


# --- prose must not be reported (or the markers stop being read) ----------------

@pytest.mark.parametrize("name,body,reason", [
    ("x.py", '"""Design note: we used to run claude -p here."""\nimport os\n',
     "python-docstring"),
    ("x.sh", '# claude -p is mentioned in this comment\necho hi\n', "comment"),
    ("x.sh", 'mkdir -p "$HOME/.claude/dockwright"\n', "config-path"),
    ("x.sh", 'source "$HOME/.claude/lib.sh"\n', "config-path"),
    ("x.conf", 'set -g focus-events on # forward focus-in/out (Claude redraw)\n',
     "comment"),
    ("job.plist", '<key>CLAUDE_CONFIG_DIR</key>\n', "xml-key"),
    ("x.sh", 'SOCK="${CLAUDE_ORCH_TMUX_SOCKET:-dockwright}"\n', "path-prefix"),
    ("x.sh", 'exec "$CLAUDE_PROJECT_DIR"/hooks/pre.sh\n', "path-prefix"),
])
def test_a_demotion_is_named_and_auditable_never_a_disappearance(
        tmp_path, name, body, reason):
    """Over-inclusion is the safe direction, but a scanner that reports every
    docstring trains the reader to paste exemption markers without reading —
    which `drift-guard-tests.md` names as its own failure.

    So a candidate may be demoted, and ONLY by a rule that can state its reason.
    The demoted candidate still appears in the report, under `mentions`, with
    that reason attached: a demotion is auditable, never a disappearance.
    """
    _write(tmp_path, name, body)
    code, report = _scan(tmp_path)
    assert code == 0, report["problems"]
    assert not report["sites"]
    assert [m["demoted_because"] for m in report["mentions"]] == [reason]


def test_an_error_message_mentioning_the_binary_is_now_REPORTED(tmp_path):
    """Expectation deliberately flipped in round 2, and this is the cost side of
    the trade the whole redesign makes.

    Round 2's first draft kept this quiet with a `one-literal-prose` rule: binary
    and print flag inside ONE string literal, and the statement names no process
    API. That rule ALSO deletes `def _sh(c): subprocess.run(c, shell=True)` +
    `_sh(f"claude -p {u}")` — a real unattended spawn — because its trigger
    condition IS the absence of a recognised process API. That is
    `_PROCESS_CALLS` acting as a filter under another name, which is exactly what
    this redesign exists to remove. It also kills the script-generator class,
    `Path("run.sh").write_text(f"claude -p {u}")`.

    So the rule is gone and an error-message string reports as a candidate. One
    line of output, exemptable with a marker, against a whole class of real
    spawns staying visible. `~/.claude/rules/drift-guard-tests.md`: prefer the
    check whose mis-implementation ADDS cases.
    """
    _write(tmp_path, "x.py",
           'import subprocess\nraise RuntimeError(f"claude -p exited {rc}")\n')
    code, report = _scan(tmp_path)
    assert code == 1
    assert report["sites"]


# --- C-A: a quoted binary was invisible, over the two live lanes ---------------

@pytest.mark.parametrize("name,body", [
    ("x.sh", '#!/bin/bash\n"$CLAUDE" -p "$U" --model m\n'),
    ("x.sh", '#!/bin/bash\n"/Users/dev/.local/bin/claude" -p "$U"\n'),
    ("x.sh", "#!/bin/bash\n'$CLAUDE' -p \"$U\"\n"),
    ("lanes.json", '{"argv":["claude","-p","$U"]}\n'),
    ("x.sh", '#!/bin/bash\n"$CLAUDE" "${CLAUDE_FLAGS[@]}" -p "$U" || rc=$?\n'),
])
def test_a_quoted_binary_is_visible(tmp_path, name, body):
    """The headline inversion of round 1, and the reason this PR exists.

    `_scan_generic` dropped the site when the character after the token was not
    whitespace or `<`. For a quoted binary that character is `"`, so
    `"$CLAUDE" -p …` gave 0 sites, 0 problems, exit 0 — and that is the VERBATIM
    live shape of `prod-support-poller:408` and `pr-review-poller:1096`, the two
    unattended lanes this work exists for. The suite missed it because its own
    case put the quote BEFORE the token: a coincidence detector inside an
    ADD-ONE suite.

    The allowlist is now a denylist of CONTINUATION characters, so a character
    nobody anticipated adds a candidate instead of deleting one.
    """
    _write(tmp_path, name, body)
    code, report = _scan(tmp_path)
    assert code == 1, report
    assert report["sites"]


# --- the live claim -------------------------------------------------------------

def test_the_scanner_states_its_limits_in_every_json_run(tmp_path):
    """It detects convention violations and enforces nothing — caller and child
    share a uid. A consumer reading `problems: []` must be able to see that from
    the output itself, not only from the docstring."""
    _write(tmp_path, "x.sh", "echo hi\n")
    _, report = _scan(tmp_path)
    assert any("uid" in lim for lim in report["limitations"])


# --- C-B: comment self-exemption, in EVERY language ----------------------------

@pytest.mark.parametrize("name,body", [
    ("x.py", 'import subprocess\nsubprocess.run(["claude","-p",u])  '
             '# TODO: move to headless_spawn.py later\n'),
    ("Makefile", 'review:\n\tclaude -p "$(U)" # TODO: move to headless_spawn.py\n'),
    ("ci.yml", 'jobs:\n  a:\n    steps:\n      - run: claude -p "$U"  '
               '# TODO: headless_spawn.py\n'),
    ("x.js", 'spawn("claude", ["-p", u]);  // TODO: move to headless_spawn.py\n'),
    ("runner", 'claude -p "$U"  # TODO: move to headless_spawn.py later\n'),
    # The launcher at COMMAND POSITION inside a comment. `_ROUTED`'s shape test
    # alone accepts this (the `;` reads as a command separator), so stripping
    # comments out of `_code_only` is the only thing that stops it — the other
    # cases above are now covered twice over and stopped proving that.
    ("x.py", 'import subprocess\nsubprocess.run(["claude","-p",u])'
             '  # then ; headless_spawn.py runs\n'),
    ("x.sh", 'claude -p "$U"\n# ; headless_spawn.py --lane x\n'),
])
def test_a_comment_cannot_route_a_spawn_in_any_language(tmp_path, name, body):
    """Round 1 closed this for exactly three shell extensions.

    `_routed` read `site["code"]`, which was RAW source for Python and the raw
    line for every generic file except `.sh/.bash/.zsh`. So the identical TODO
    that was caught in a `.sh` still scored `routed: true, problems: [], exit 0`
    in a `.py`, a Makefile, a workflow or an extension-less runner.
    """
    _write(tmp_path, name, body)
    code, report = _scan(tmp_path)
    assert code == 1, report


def test_the_launcher_name_inside_a_STRING_cannot_route_a_spawn(tmp_path):
    """The sibling gate, found by the round-2 spec review and missed by Tier-2.

    Ask 2 fixed self-exemption at the MARKER gate by stripping comments. But
    `routed` is a substring test, and a comment-stripper does nothing to a
    string, so the launcher's name in a redirect target still exempted the
    spawn:

        claude -p "$S" >> "$LOG_DIR/headless_spawn.log"   → 1 site, 0 problems

    `fix-the-level-not-the-instance.md`: the property is "no self-authored text
    may exempt a spawn", and it has to hold at every gate that reads text the
    author controls — not only at the one the repro pointed at.
    """
    _write(tmp_path, "x.sh",
           '#!/bin/bash\nclaude -p "$S" >> "$LOG_DIR/headless_spawn.log"\n')
    code, report = _scan(tmp_path)
    assert code == 1, report


# --- C-C: the six shapes an adversarial reviewer invented ----------------------

SIX_SHAPES = [
    ("helper-wrapper", 'import subprocess\ndef _sh(cmd): subprocess.run(cmd)\n'
                       '_sh(["claude","-p",u])\n'),
    ("augassign", 'import subprocess\ncmd = []\ncmd += ["claude","-p",u]\n'
                  'subprocess.run(cmd)\n'),
    ("kwarg-args", 'import subprocess\ncmd = ["claude","-p",u]\n'
                   'subprocess.run(args=cmd)\n'),
    ("os-posix-spawn", 'import os\nos.posix_spawn("/usr/bin/claude", '
                       '["claude","-p",u], {})\n'),
    ("fstring-shell-true", 'import subprocess\n'
                           'subprocess.run(f"claude -p {u}", shell=True)\n'),
    ("dict-of-lanes", 'import subprocess\nLANES = {"a": ["claude","-p"]}\n'
                      'subprocess.run(LANES["a"] + [u])\n'),
]


@pytest.mark.parametrize("label,body", SIX_SHAPES, ids=[s[0] for s in SIX_SHAPES])
def test_the_call_name_list_is_no_longer_load_bearing(tmp_path, label, body):
    """Round 1 swapped a SHAPE enumeration for a CALL-NAME enumeration, and on
    these six that was a net regression: 5 of 6 missed where the previous
    generation caught them.

    Every one of these has the literal `claude` visible in the source. They are
    caught now because the RAW pass is the safety net and `_PROCESS_CALLS` only
    refines it — so an unknown call name, an AugAssign, a keyword argument or a
    dict subscript costs detail, not coverage.
    """
    _write(tmp_path, "x.py", body)
    code, report = _scan(tmp_path)
    assert code == 1, f"{label}: {report}"
    assert report["sites"], label


def test_the_call_name_table_is_not_load_bearing_for_COVERAGE(tmp_path):
    """The claim `_PROCESS_CALLS` is "not load-bearing" was FALSE through round 2
    and is asserted here rather than merely written in a comment.

    It was false because the raw pass demoted a bare `Name` in an argv list, so
    the hand-maintained call-name table was the ONLY recovery for a
    variable-held binary — `self._exec([CLAUDE_BIN, "-p", p])` reported 0 sites,
    exit 0, because `_exec` was not in it. The 32nd wrapper name was unguarded by
    construction.

    Emptying the table must now leave every one of these still REPORTED. What the
    table still buys is detail, which the test below covers separately.
    """
    mod = _module()
    mod._PROCESS_CALLS_LOWER = frozenset()
    for name, body in [
        ("a.py", 'def launch(a): ...\nlaunch([CLAUDE_BIN, "-p", prompt])\n'),
        ("b.py", 'class R:\n    def go(self, p):\n        self._exec([CLAUDE_BIN, "-p", p])\n'),
        ("c.py", 'def _exec(a): ...\n_exec(["codex","exec",u])\n'),
        ("d.py", 'import subprocess\nsubprocess.run([claude_bin, "-p", P])\n'),
        # The PAREN forms. Under the `[`-only rule these five reported 0 sites,
        # exit 0, and `os.execvp` went reported → unreported when the table was
        # emptied — i.e. the "not load-bearing" claim was still false. `[` and
        # `(` both count now, and this is the pin for that.
        ("e.py", 'class R:\n    def go(self, p): self._exec(CLAUDE_BIN, "-p", p)\n'),
        ("f.py", 'import os\nos.execvp(CLAUDE_BIN, [CLAUDE_BIN, "-p", p])\n'),
        ("g.py", 'cmd = (CLAUDE_BIN, "-p", P)\nrun(cmd)\n'),
        # A wrapper carrying its OWN flags before the binary — the live shape in
        # this repo (`env -u CLAUDE_AGENT claude -p`). Command position rejected
        # it, which made the table load-bearing for it: the FOURTH time this
        # claim was measured false.
        ("h.py", 'import subprocess\n'
                 'subprocess.run(f"env -u CLAUDE_AGENT claude -p {q}", shell=True)\n'),
        ("i.py", 'def _sh(c): ...\n_sh(f"sudo -u ops claude -p {q}")\n'),
    ]:
        (tmp_path / name).write_text(body)
    sites, unscannable, _skipped, _notes = mod.scan([str(tmp_path)])
    seen = {os.path.basename(s["path"]) for s in sites if s["kind"] == "spawn"}
    assert seen == {"a.py", "b.py", "c.py", "d.py",
                    "e.py", "f.py", "g.py", "h.py", "i.py"}, seen


def test_a_binary_assembled_from_a_variable_is_what_the_AST_adds(tmp_path):
    """The one shape the raw pass genuinely cannot see, and the reason the AST
    pass is kept at all: no literal `claude` token exists on the call's line."""
    _write(tmp_path, "x.py",
           'import subprocess\nBIN = "/usr/local/bin/claude"\n'
           'subprocess.run(f"{BIN} -p {u}", shell=True)\n')
    code, report = _scan(tmp_path)
    assert code == 1
    assert any(s["source"] == "ast" or s.get("ast_confirmed") for s in report["sites"])


# --- C-D: only the FIRST token on a line was examined --------------------------

@pytest.mark.parametrize("body", [
    'cd "$HOME/.claude" && claude -p "$S"\n',
    'mkdir -p "$HOME/.claude/dockwright" && claude -p "$S"\n',
    'INNER_CMD="cd $D && env -u CLAUDE_AGENT claude -p \\"$S\\""\n',
    'source "$HOME/.claude/lib.sh"; exec claude -p "$S"\n',
])
def test_a_demoted_leading_token_does_not_take_its_line_with_it(tmp_path, body):
    """Found by the round-2 spec review; in neither the Tier-2 verdict nor round 1.

    `_scan_generic` used `_BIN_TOKEN.search` — the FIRST match only. So a leading
    `$HOME/.claude` path was demoted and the real invocation later on the SAME
    line was never examined: measured 0 sites, exit 0. This repo's own live spawn
    has that shape (`gardener-run.sh:530`). `finditer` + per-token classification
    is the fix; a line is a spawn if ANY of its tokens is.
    """
    _write(tmp_path, "x.sh", body)
    code, report = _scan(tmp_path)
    assert code == 1, report
    assert report["sites"]


# --- Ask 4: the skip sets are pinned, AND the walk that applies them -----------

def test_the_skip_sets_are_pinned_by_equality(tmp_path):
    """Measured: adding "scripts","bin" to SKIP_DIRS drops this repo 24 sites → 6
    with the whole unit suite green, `unscannable: []`, and no output field
    revealing it. Every spawn under `deploy/scripts/` vanishes silently."""
    mod = _module()
    assert mod.SKIP_DIRS == frozenset({
        ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
        ".pytest_cache", ".ruff_cache", "dist", "build", ".tox", ".idea",
        "site-packages", ".gradle", "target",
    })
    assert mod.SKIP_DIR_SUFFIXES == frozenset({".egg-info"})
    assert mod.SKIP_EXTENSIONS == frozenset({
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".pdf",
        ".zip", ".gz", ".tgz", ".bz2", ".xz", ".tar", ".whl", ".jar",
        ".so", ".dylib", ".dll", ".o", ".a", ".class", ".pyc", ".pyo",
        ".mp3", ".mp4", ".mov", ".wav", ".ttf", ".otf", ".woff", ".woff2",
        ".lock", ".sqlite", ".db",
        ".md", ".markdown", ".rst", ".txt",
    })
    assert mod.DEMOTIONS == frozenset({
        "comment", "config-path", "config-dir", "path-prefix",
        "python-docstring", "xml-key", "model-argument", "no-invocation-shape",
    })
    assert mod._MODEL_FLAGS == frozenset({
        "--model", "-m", "--fallback-model", "--small-model"})
    # Documented as `==`-pinned and previously was not: adding "codex" here
    # turned every bare `codex` spawn into mention(config-dir) with the whole
    # suite green.
    assert mod._CONFIG_DIR_BASENAMES == frozenset({".claude", ".codex"})
    assert mod._CONTINUATION == frozenset({"/"})
    assert mod._EXPANSION_OPS == frozenset({":", "#", "%"})
    # VERDICTS is DERIVED from MARKER rather than maintained beside it, so the
    # pin is that they agree — a verdict in one and not the other is how the
    # dispatch silently gains an unchecked branch.
    assert set(mod.VERDICTS) == {"wrapper", "verified-by-test", "exempt"}
    assert mod._VALUE_KEYS_THAT_ARE_NOT_COMMANDS == frozenset({
        "model", "models", "fallback_model", "small_model", "runtime", "agent",
        "provider", "name", "type", "image"})
    # The comment tables are the only ones whose entries REMOVE executed code,
    # and they were the ones missing from this pin. Adding `//` to yaml or `#` to
    # xml/json, or `:` to shell, left the suite green.
    assert mod._LINE_COMMENT == {
        "python": ("#",), "shell": ("#",), "yaml": ("#",), "make": ("#",),
        "ini": ("#", ";"), "cfamily": ("//", "/*"), "xml": ("<!--",),
        "json": (), "unknown": ("#", "//")}
    assert mod._TRAILING_COMMENT == {
        "python": ("#",), "shell": ("#",), "yaml": ("#",), "make": ("#",),
        "ini": ("#", ";"), "cfamily": ("//",), "xml": (), "json": (),
        "unknown": ("#", "//")}


def test_what_the_walk_ACTUALLY_yields_is_derived_not_asserted_from_the_constant(tmp_path):
    """The `==` pins above guard the CONSTANTS. They do not guard the PREDICATE.

    Round 1's walk was `d not in SKIP_DIRS and not d.endswith(".egg-info")` —
    that second clause dropped `pkg.egg-info/gen.sh` while `SKIP_DIRS` sat
    untouched and every pin stayed green. A future `or d.startswith(".")` would
    silently drop `.github/` workflows the same way. `drift-guard-tests.md`
    § ADD-ONE: the guard dies by being OVERRIDDEN, not deleted.

    So the guarded set is DERIVED — walk a fixture tree and assert the exact
    yielded set, one entry per category, including the categories most likely to
    be swept up by a new clause.
    """
    mod = _module()
    for rel in ["keep.sh", "keep_no_ext", ".github/wf.yml", "scripts/s.sh",
                "bin/b.sh", "pkg.egg-info/gen.sh", "node_modules/n.sh",
                "build/b.sh", "notes.md", "logo.png", "src/a.py"]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n")
    got = {os.path.relpath(f, tmp_path) for f in mod._iter_files([str(tmp_path)])}
    assert got == {"keep.sh", "keep_no_ext", ".github/wf.yml", "scripts/s.sh",
                   "bin/b.sh", "src/a.py"}


@pytest.mark.parametrize("gap,detected", [(0, True), (1, True), (3, True),
                                          (6, True), (7, True), (8, False)])
def test_the_flag_window_actually_spans_the_distance_it_claims(tmp_path, gap, detected):
    """`_FLAG_WINDOW` is the sole thing deciding whether a quoted argv element
    becomes a site, and at 4 nothing in this file exercised a distance above 1 —
    setting it to 1 left the suite green, so the documented rationale (a plist
    puts the binary and its `-p` in separate <string> elements; a TOML registry
    puts `args` below `bin`) was unmeasured, and a real `mcp.json` flipped from
    detected to invisible on line spacing alone.

    It is a CLIFF and both sides are asserted here, rather than only the side
    that passes. The residual — a data-file argv whose flags sit more than 8
    lines from the binary — is named in `limitations`, because widening the
    window only moves the cliff; removing it needs a per-format parser, which is
    out of scope for this PR."""
    filler = "".join(f'  <string>x{i}</string>\n' for i in range(gap))
    _write(tmp_path, "job.plist",
           f'<string>/usr/bin/claude</string>\n{filler}<string>-p</string>\n')
    code, report = _scan(tmp_path)
    assert (code == 1) == detected, f"gap={gap}: {report}"


def test_the_flag_window_is_pinned():
    assert _module()._FLAG_WINDOW == 8


def test_paren_call_candidates_are_bucketed_not_mixed_in(tmp_path):
    """Accepting `(` as an argv[0] delimiter takes this repo 136 → 299 sites, so
    the reader must not be handed ~300 undifferentiated lines — that is the
    "trains the reader to paste markers unread" failure in its own right.

    Nothing is dropped: a paren-call candidate is still a problem and still exits
    non-zero. It is REPORTED IN ITS OWN LABELLED BUCKET, with counts in both the
    plain footer and the JSON, so the confirmed spawns stay legible."""
    _write(tmp_path, "a.py",
           'import subprocess\nsubprocess.run(["claude","-p",u])\n')
    _write(tmp_path, "b.py",
           'class R:\n    def go(self, p): self._exec(CLAUDE_BIN, "-p", p)\n')
    code, report = _scan(tmp_path)
    assert code == 1
    buckets = {s["bucket"] for s in report["sites"]}
    assert buckets == {"confirmed", "paren-call-unconfirmed"}, report["sites"]
    assert report["buckets"]["confirmed"] >= 1
    assert report["buckets"]["paren-call-unconfirmed"] >= 1
    proc = subprocess.run([sys.executable, SCRIPT, str(tmp_path)],
                          capture_output=True, text=True)
    # Bind to the printed SITE LINES, not to the label: the label also appears in
    # the footer counts, so asserting the string alone passed even when the whole
    # section printed nothing. A bucket with no printed section is a site that
    # silently never reaches the reader while the footer still counts it.
    for name in ("a.py", "b.py"):
        assert f"{name}:" in proc.stdout, f"{name} never printed:\n{proc.stdout}"
    assert proc.stdout.count("--- ") == 2, proc.stdout


def test_every_demotion_reason_emitted_over_the_real_repo_is_in_the_pinned_table():
    """`DEMOTIONS` got the `==` pin but not the behavioural derivation the skip
    lists got — a new demotion reason could be emitted while the pin stayed green,
    which is a new way for a real spawn to vanish under a name nobody reviewed.

    Derived from a real corpus rather than from the constant: every reason the
    scanner ACTUALLY emits over `deploy`+`src` must be a member of the pinned
    table."""
    mod = _module()
    _, report = _scan(os.path.join(REPO, "deploy"), os.path.join(REPO, "src"))
    emitted = {r for m in report["mentions"]
               for r in m["demoted_because"].split(", ") if r}
    assert emitted, "no demotions at all — the corpus check is not exercising"
    assert emitted <= mod.DEMOTIONS, emitted - mod.DEMOTIONS


RECOVERY_SPAWN_ANCHOR = 'f"claude {rc_arg}{skip_arg}'


def _recovery_spawn_line():
    """Line number of the unattended recovery-manager spawn, RESOLVED not pinned.

    This was a hardcoded `== 1045`, which is a coincidence detector pointed at
    the wrong thing: it tracked the line's POSITION, so any unrelated insertion
    above it broke the test (one did, adding an import), and the fix pressure is
    to bump the number — at which point the guard has been "maintained" without
    anyone re-checking the property. Anchoring on the code itself keeps the
    assertion about the spawn and makes the test immune to edits elsewhere.
    """
    source = os.path.join(REPO, "src", "dockwright", "stale_monitor.py")
    with open(source, encoding="utf-8") as handle:
        hits = [i for i, line in enumerate(handle, start=1)
                if RECOVERY_SPAWN_ANCHOR in line]
    assert len(hits) == 1, (
        f"expected exactly one recovery-manager spawn matching "
        f"{RECOVERY_SPAWN_ANCHOR!r}, found {hits}. If the spawn was rewritten, "
        f"update the anchor — do NOT delete this test.")
    return hits[0]


def test_the_repos_most_dangerous_line_stays_in_the_confirmed_bucket():
    """The unattended recovery-manager spawn in `stale_monitor.py` — the one
    carrying `--dangerously-skip-permissions`, and the exact shape of the
    2026-07-30 incident this scanner exists for.

    Bucketing it by a per-line OR demoted it into `paren-call-unconfirmed` among
    ~150 entries: the noise mitigation buried the single most load-bearing row in
    the report. Buckets are decided by the STRONGEST signal on the line now, and
    this row is pinned so it can never drift out of the top bucket silently.
    """
    expected_line = _recovery_spawn_line()
    code, report = _scan(os.path.join(REPO, "src"))
    row = [s for s in report["sites"]
           if s["path"].endswith("stale_monitor.py")
           and s["line"] == expected_line]
    assert row, (
        f"the recovery-manager spawn at stale_monitor.py:{expected_line} is no "
        f"longer detected at all")
    assert row[0]["bucket"] == "confirmed", row[0]
    assert code == 1


def test_an_unhandled_marker_verdict_REFUSES_rather_than_exempting(tmp_path):
    """The dispatch was default-ALLOW: `if None / elif no detail / elif wrapper`
    with no `else`, so a verdict added to `MARKER` but not handled fell through
    as a silent, unchecked exemption — in the scanner whose whole thesis is that
    exemptions must be explicit and auditable. ADD-ONE proved it: one new verdict
    in `MARKER` + `VERDICTS` and a `--dangerously-skip-permissions` spawn exited
    0 with the suite green.

    `VERDICTS` is now DERIVED from `MARKER` so the two cannot drift, and the
    dispatch refuses anything it does not handle."""
    mod = _module()
    assert set(mod.VERDICTS) == {"wrapper", "verified-by-test", "exempt"}
    # every verdict the marker pattern admits must be handled by classify()
    for verdict in mod.VERDICTS:
        site = mod.Site(path="x", line=1, end=1, text="", code="", lang="shell",
                        kind="spawn", verdict=verdict, detail="reason",
                        tokens=[], demoted_because="")
        problems = mod.classify([site], [])
        if verdict == "wrapper":
            assert problems, "an unconfirmable wrapper claim must be refused"
        else:
            assert not problems, f"{verdict} with a reason must exempt"
    unknown = mod.Site(path="x", line=1, end=1, text="", code="", lang="shell",
                       kind="spawn", verdict="launcher", detail="routed",
                       tokens=[], demoted_because="")
    assert mod.classify([unknown], []), "an unhandled verdict must REFUSE"


def test_the_ast_confirmed_bucket_branch_is_not_dead(tmp_path):
    """Round 4's lesson was a grant branch that could be replaced with `if False:`
    while the suite stayed green. The branch that REPLACED it had the same
    property: `_bucket`'s `ast_confirmed` test was unpinned, because the one real
    row pinned to `confirmed` (stale_monitor.py:1045) has `ast_confirmed: False`
    and never exercises it."""
    _write(tmp_path, "x.py", 'import os\nos.execvp(CLAUDE_BIN, argv)\n')
    code, report = _scan(tmp_path)
    assert code == 1, report
    row = report["sites"][0]
    assert row["ast_confirmed"] or row["source"] == "ast", row
    assert row["bucket"] == "confirmed", row


@pytest.mark.parametrize("body", [
    # mid-list, neither first on its line nor adjacent to the bracket
    'class R:\n    def go(self, p):\n'
    '        self._launch(["env", "-u", "X", claude_bin, "-p", PROMPT])\n',
    # mid-list on a continuation line
    'r = _launch(\n    ["env", "-u", "X",\n     claude_bin, "-p", PROMPT],\n)\n',
    # tuple form
    'cmd = ("env", "-u", "X", claude_bin, "-p", P)\nrun(cmd)\n',
    # A quoted VALUE, whose left neighbour is `:` or `=` rather than list
    # punctuation. Unifying the four positional special-cases into one delimiter
    # rule DROPPED this sub-form — the rule strips the quotes and then reads the
    # `:`/`=` underneath — and the suite stayed green because every case here
    # covered only the `,`/line-start left-delimiter class. A simplification may
    # merge branches; it may never lose one.
    'CFG = {"command": "claude", "args": ["-p", u]}\n',
    'CLAUDE_BIN = "claude"\nARGS = ["-p", u]\n',
])
def test_an_argv_element_is_seen_at_ANY_position_in_the_list(tmp_path, body):
    """Four separate rounds each fixed one POSITION of the same property — argv[0]
    after `[`, a quoted item, a whole-line sequence item, a continuation line —
    and each left its siblings blind. `_is_element` is now one rule: delimited on
    both sides by list punctuation, with a line boundary counting as a delimiter.

    This case is the one that survived all four: `[…, "-u", "X", claude_bin,
    "-p", …]`, where the token is neither first on its line nor next to the
    bracket."""
    _write(tmp_path, "y.py", body)
    code, report = _scan(tmp_path)
    assert code == 1, report
    assert report["sites"], report


def test_a_multi_line_argv_continuation_line_is_seen(tmp_path):
    """This repo's own real spawns are formatted `[\n  claude_bin, "-p", P,\n]`.
    A bare Name SHARING its continuation line with other arguments had no signal
    at all — not argv[0] (no `[` immediately before), not a block element
    (trailing text), not a quoted element — so the hand-maintained call-name
    table was its only detector, and renaming `subprocess.run` to a wrapper made
    `distill.py`'s spawn vanish with a decoy line left in its place."""
    _write(tmp_path, "b.py",
           'class R:\n    def go(self, s):\n        r = self._launch(\n'
           '            [\n                claude_bin, "-p", PROMPT,\n'
           '                "--dangerously-skip-permissions",\n            ],\n'
           '            input=s,\n        )\n')
    code, report = _scan(tmp_path)
    assert code == 1, report
    assert report["sites"], report


def test_the_walk_yields_every_tracked_file_the_pinned_sets_do_not_exclude():
    """The fixture test above is a hand-written list, so the NEXT skip clause is
    unguarded by construction — measured: adding `name == "dockwright"` to
    `_skip_dir` took this repo from 98 sites to 31, left `skipped.counts.dirs`
    UNCHANGED at 4, and kept the whole suite green.

    This derives the expected set from an INDEPENDENT source — `git ls-files` —
    rather than from the scanner's own enumeration, per
    `drift-guard-tests.md` § "a cross-check must be independently derived".
    """
    mod = _module()
    tracked = subprocess.run(
        ["git", "ls-files", "deploy", "src"], cwd=REPO,
        capture_output=True, text=True, check=True).stdout.split()
    def excluded(rel):
        parts = rel.split("/")
        if any(p in mod.SKIP_DIRS or any(p.endswith(s) for s in mod.SKIP_DIR_SUFFIXES)
               for p in parts[:-1]):
            return True
        return os.path.splitext(parts[-1])[1].lower() in mod.SKIP_EXTENSIONS
    expected = {r for r in tracked if not excluded(r)}
    got = {os.path.relpath(f, REPO)
           for f in mod._iter_files([os.path.join(REPO, "deploy"),
                                     os.path.join(REPO, "src")])}
    assert expected - got == set(), f"the walk DROPPED tracked files: {expected - got}"


@pytest.mark.parametrize("name,body", [
    ("x.sh", 'codex exec -p "$U"\n'),
    ("x.sh", '"$CODEX" exec "$U"\n'),
    ("x.py", 'import subprocess\nsubprocess.run(["codex","exec",u])\n'),
    ("lanes.json", '{"argv":["codex","exec","$U"]}\n'),
])
def test_codex_is_scanned_too_not_only_claude(tmp_path, name, body):
    """Half the module's stated scope, and the word `codex` appeared ZERO times
    in this suite: deleting `|codex` from `_BIN_TOKEN` left every test green.
    dockwright orchestrates Codex sessions, so these are live spawns."""
    _write(tmp_path, name, body)
    code, report = _scan(tmp_path)
    assert code == 1, report
    assert report["sites"]


def test_a_skipped_directory_is_disclosed_by_path(tmp_path):
    """Round 1 disclosed nothing. A consumer reading `problems: []` could not tell
    a clean tree from a tree whose spawns all sat under a skipped directory.

    Stated honestly in `limitations` too: the skipped path is NOT scanned and the
    run can still exit 0 — disclosure, not coverage."""
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "launcher.sh").write_text('claude -p "$S"\n')
    code, report = _scan(tmp_path)
    assert report["skipped"]["counts"]["dirs"] == 1
    assert report["skipped"]["dirs"][0]["path"].endswith("/build")
    assert any("skipped" in lim.lower() and "exit 0" in lim
               for lim in report["limitations"]), report["limitations"]


# --- Ask 5: symlinked directories ----------------------------------------------

def test_a_symlinked_directory_is_followed_not_silently_empty(tmp_path):
    """`_iter_files` used os.walk with the default followlinks=False, so a root
    whose only content is `vendor -> ../outside` holding a real `claude -p`
    reported "0 spawn sites, 0 unscannable, 0 problems", exit 0 — precisely the
    "green means I found nothing to look at" failure the module exists to
    prevent."""
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "spawn.sh").write_text('claude -p "$U"\n')
    (tmp_path / "root").mkdir()
    os.symlink("../outside", str(tmp_path / "root" / "vendor"))
    code, report = _scan(tmp_path / "root")
    assert code == 1, report
    assert report["sites"]


def test_a_symlink_cycle_terminates_and_a_second_path_is_not_a_problem(tmp_path):
    """Following links needs a visited-inode set or the walk never returns.

    And the two outcomes are kept apart: a genuine cycle is `unscannable` (a
    problem), while a second path to an already-walked tree — overlapping roots,
    two links into one directory — is informational. Folding the second into
    `unscannable` would poison the bucket I-4 exists to make meaningful.
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "s.sh").write_text("echo hi\n")
    os.symlink(str(tmp_path / "a"), str(tmp_path / "a" / "loop"))
    code, report = _scan(tmp_path)
    # A genuine cycle: the walk was truncated by a loop, so it is a PROBLEM.
    assert code == 1, report
    assert any("symlink cycle" in u["why"] for u in report["unscannable"]), report


def test_a_second_path_to_one_tree_is_informational_not_a_problem(tmp_path):
    """The other half of the split. Overlapping roots and two links into one tree
    are ordinary; reporting them as `unscannable` would poison the bucket that
    exists to make a genuinely unreadable file mean something."""
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "s.sh").write_text("echo hi\n")
    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()
    os.symlink(str(tmp_path / "shared"), str(tmp_path / "x" / "vendor"))
    os.symlink(str(tmp_path / "shared"), str(tmp_path / "y" / "vendor"))
    code, report = _scan(tmp_path / "x", tmp_path / "y")
    assert code == 0, report["problems"]
    assert report["notes"] and not report["unscannable"], report


# --- I-4: the three unreadable causes are distinguishable ----------------------

def test_each_unreadable_cause_names_itself(tmp_path):
    """`_decode` swallowed every OSError and returned None, so chmod-000, a
    dangling symlink and a real binary all reported "not decodable as text" and
    sent the fixer hunting an encoding bug."""
    locked = tmp_path / "locked.sh"
    locked.write_text('claude -p x\n')
    locked.chmod(0o000)
    os.symlink("/nonexistent/target", str(tmp_path / "broken.sh"))
    (tmp_path / "blob.dat").write_bytes(b"\xff\xfe\x00\x01claude -p x\x00")
    try:
        _, report = _scan(tmp_path)
    finally:
        locked.chmod(0o644)
    why = {u["path"].split("/")[-1]: u["why"] for u in report["unscannable"]}
    assert "EACCES" in why["locked.sh"], why
    assert "ENOENT" in why["broken.sh"], why
    assert "NUL byte" in why["blob.dat"], why


# --- M-1: a marker may only come out of a real comment -------------------------

def test_a_string_in_executed_code_cannot_carry_an_exemption_marker(tmp_path):
    """`_generic_comments` treated ANY line containing `#` as a comment and the
    marker search ran over that stream, so an ordinary line of executed code
    could exempt the spawn below it:

        echo "docs say: # headless-lockdown: exempt(attended) — a human watches"
        claude -p "$U"

    measured 1 site, 0 problems, exit 0. Same self-exemption class as C-B,
    reachable from executed code, and in neither the Tier-2 verdict nor round 1.
    """
    _write(tmp_path, "x.sh",
           '#!/bin/bash\n'
           'echo "docs say: # headless-lockdown: exempt(attended) — a human watches"\n'
           'claude -p "$U"\n')
    code, report = _scan(tmp_path)
    assert code == 1, report


@pytest.mark.parametrize("body", [
    # saved by the outside-`${…}` guard
    'env M=${MODEL#*=} claude -p "$S"\n',
    # saved by the outside-a-string guard
    'sed \'s#a#b#\' f && claude -p "$S"\n',
    'echo "issue #123" && claude -p "$S"\n',
    # saved by the whitespace-preceded guard ALONE — neither of the other two
    # covers a bare `#` sitting in an ordinary argument. Added after the
    # red-proof showed the first three cases stayed green when that guard was
    # deleted, i.e. they were not testing it.
    'awk -F# "{print}" f && claude -p "$S"\n',
    'curl "$U" -o out#1 && claude -p "$S"\n',
])
def test_the_trailing_comment_model_does_not_eat_executed_code(tmp_path, body):
    """A comment model that DEMOTES is a deletion path wearing a reason.

    An eager `#`-stripper reads `${MODEL#*=}`, `s#a#b#` and `-F#` as opening a
    comment and swallows the spawn after it. Hence the three guards, one case
    each above: the opener must be whitespace-preceded, outside every string
    literal, and outside `${…}`.
    """
    _write(tmp_path, "x.sh", body)
    code, report = _scan(tmp_path)
    assert code == 1, report


def test_a_bare_url_does_not_open_a_comment_in_an_unknown_file(tmp_path):
    """The `//` half of the same class: an unknown extension gets both `#` and
    `//` openers, and a bare URL would otherwise swallow the rest of the line."""
    _write(tmp_path, "thing.conf",
           'see https://example.com/docs and then claude -p "$S"\n')
    code, report = _scan(tmp_path)
    assert code == 1, report


# --- the two noise demotions, which are deletion paths and must be bounded -----

def test_a_model_argument_is_not_a_binary_but_its_spawn_still_reports(tmp_path):
    """`--model claude-opus-5` has the command-line shape and is the commonest
    false positive in this tree. A binary is never a model argument.

    The second half is the part that matters: demoting the model VALUE must not
    demote the invocation it belongs to."""
    _write(tmp_path, "x.sh", '#!/bin/bash\nclaude -p "$U" --model claude-opus-5\n')
    code, report = _scan(tmp_path)
    assert code == 1, report
    assert report["sites"], "the spawn itself must survive its model argument"
    _write(tmp_path, "y.sh",
           '#!/bin/bash\nCLAUDE_FLAGS=(--model claude-opus-5 --effort medium)\n')
    _, report = _scan(tmp_path / "y.sh")
    reasons = {r for m in report["mentions"] for r in m["demoted_because"].split(", ")}
    assert "model-argument" in reasons, report


@pytest.mark.parametrize("body,reported", [
    # command position inside the string — a real embedded command line.
    # The first two use a call name the AST does NOT know, so the command-position
    # rule is the ONLY thing keeping them visible; with `subprocess.run` the AST
    # would cover for it and the case would prove nothing.
    ('def _sh(c): ...\n_sh(f"cd {d} && claude -p {q}")\n', True),
    ('def _sh(c): ...\n_sh(f"env X=1 claude -p {q}")\n', True),
    ('import subprocess\nsubprocess.run(f"cd {d} && claude -p {q}", shell=True)\n', True),
    ('import os\nos.popen("claude -p x")\n', True),
    # mid-sentence prose in a Python string — not an invocation
    ('print(f"manager-memory: claude -p exit {rc} for {sid}")\n', False),
    # the control for the wrapper-flag widening: a bare English phrase before the
    # binary must NOT become command position just because wrappers may carry
    # flags. The run of extra words is admitted only AFTER a wrapper word.
    ('print(f"then invoking claude -p on {n} items")\n', False),
    ('X = "some prose claude -p mention"\n', False),
    ('P = "Distill this Claude Code manager session transcript"\n', False),
])
def test_python_strings_need_the_binary_at_command_position(tmp_path, body, reported):
    """Python source is not a command line, and its strings carry pages of prose
    that name the binary mid-sentence. Requiring command position INSIDE the
    string separates `cd {d} && claude -p {q}` from `manager-memory: claude -p
    exit {rc}` without an English-vs-argument heuristic.

    Scoped to Python deliberately: applying it to shell would be an
    under-inclusion risk this scanner cannot take.
    """
    _write(tmp_path, "x.py", body)
    code, report = _scan(tmp_path)
    assert bool(report["sites"]) == reported, report


def test_every_demoted_candidate_still_appears_with_its_reason(tmp_path):
    """The contract that makes demotion safe to have at all: nothing vanishes.

    A reader auditing a clean run can enumerate exactly what was set aside and
    why, and every reason is a member of the `==`-pinned DEMOTIONS set."""
    _write(tmp_path, "x.sh",
           '# claude -p in a comment\nmkdir -p "$HOME/.claude/dockwright"\n'
           'MODELS="--model claude-opus-5"\n')
    _, report = _scan(tmp_path)
    mod = _module()
    assert report["mentions"]
    for m in report["mentions"]:
        assert m["demoted_because"], m
        for reason in m["demoted_because"].split(", "):
            assert reason in mod.DEMOTIONS, reason


# --- found by the round-2 CODE review, after the spec review --------------------

@pytest.mark.parametrize("name,body", [
    ("lanes.yml", '  headless_spawn: claude -p "$UNTRUSTED"\n'),
    ("Makefile", 'headless_spawn: ; claude -p "$$UNTRUSTED"\n'),
    ("r4.py", 'import subprocess\nheadless_spawn = subprocess.run(["claude","-p",u])\n'),
    ("x.toml", 'headless_spawn = claude -p go\n'),
    ("a.sh", 'case $x in\nheadless_spawn) claude -p "$S";;\nesac\n'),
    ("a.sh", 'headless_spawn=1 claude -p "$S"\n'),
    ("a.sh", 'headless_spawn () { claude -p "$S"; }\n'),
    ("a.js", 'headless_spawn: spawn("claude", ["-p", u]);\n'),
    # The launcher mid-STRING after a separator, with the text before the string
    # ALSO ending in a separator — the one shape where "the launcher must OPEN
    # the string" is the only thing between this and a granted exemption.
    # Verified load-bearing: neutering that check flips this line to routed=True.
    # (A first attempt at this case put `echo ` before the string, which blocked
    # the grant on its own and proved nothing.)
    ("a.sh", 'claude -p "$S" ; "prefix ; headless_spawn.py --lane x"\n'),
    # The launcher in the OTHER half of a chain. `routed` means THIS invocation
    # goes through the launcher, not that the launcher is mentioned on the line.
    # Third appearance of one defect: the name in a COMMENT, then in a STRING,
    # now after a `&&`.
    ("a.sh", 'claude -p "$UNTRUSTED" && headless_spawn.py --check\n'),
    ("a.sh", 'claude -p "$UNTRUSTED" || headless_spawn.py --check\n'),
    ("a.sh", 'claude -p "$UNTRUSTED" ; headless_spawn.py --check\n'),
    # The previously-pinned `|| echo headless_spawn failed` survived only
    # because `echo` blocked the grant before the segment rule existed — a
    # coincidence detector. It is kept, but it is these three that pin the rule.
    ("a.sh", 'claude -p "$UNTRUSTED" | headless_spawn.py --check\n'),
])
def test_the_launcher_NAME_being_declared_does_not_grant_an_exemption(tmp_path, name, body):
    """`routed` skips a site outright — no marker, no detail, nothing printed —
    so the GRANTING side is the dangerous one, and through round 2 it had ZERO
    positive coverage: all four `routed` assertions in this file were negative,
    and making `_routed` return False unconditionally left the suite green.

    In every declarative language the first token on a line is a key, target,
    label or assignment target — not a command. Each case here granted a
    marker-free exemption over a real `claude -p "$UNTRUSTED"`.
    """
    _write(tmp_path, name, body)
    code, report = _scan(tmp_path)
    assert code == 1, report


def test_an_explicit_wrapper_marker_is_now_the_ONLY_launcher_exemption(tmp_path):
    """The INFERRED exemption (`routed`) was deleted after granting a false
    marker-free exemption in five consecutive review rounds — comment, string,
    unquoted, declared name, `&&` chain, `$(command substitution)` — while having
    ZERO reachable true positives, because `headless_spawn.py` is not in this
    tree.

    What replaced it is what was always there: an explicit marker somebody wrote
    deliberately. And a bare `wrapper` claim is refused, because this scanner no
    longer infers launcher routing and therefore cannot confirm one."""
    _write(tmp_path, "x.sh",
           '#!/bin/bash\npython3 "$DIR/headless_spawn.py" spawn --bin "$CLAUDE" -p "$S"\n')
    code, report = _scan(tmp_path)
    assert code == 1, "an inferred launcher exemption must not exist"
    assert report["problems"], report
    _write(tmp_path, "y.sh",
           '#!/bin/bash\n# headless-lockdown: exempt(launcher) — goes through headless_spawn.py\n'
           'python3 "$DIR/headless_spawn.py" spawn --bin "$CLAUDE" -p "$S"\n')
    code, _ = _scan(tmp_path / "y.sh")
    assert code == 0, "an explicit, reasoned marker must still exempt"


@pytest.mark.parametrize("body", [
    'claude -p "$S" >> /var/log/headless_spawn.log\n',
    'headless_spawn_v2=1 claude -p "$S"\n',
    'claude -p "$S" || echo headless_spawn failed\n',
    'headless_spawn() { claude -p "$1"; }\n',
    # command position INSIDE a prompt string — only `_code_only`'s string
    # stripping stops this one; the `_ROUTED` shape test alone accepts it.
    'claude -p "; headless_spawn.py --lane x"\n',
])
def test_unquoted_text_naming_the_launcher_cannot_route_a_spawn(tmp_path, body):
    """The C-E fix stripped comments and STRING literals, so it closed the quoted
    redirect and left the unquoted one: deleting two characters from the pinned
    case reopened `routed=True, exit 0`. Same property, third gate — the launcher
    must be INVOKED, not merely named."""
    _write(tmp_path, "x.sh", body)
    code, report = _scan(tmp_path)
    assert code == 1, report


@pytest.mark.parametrize("name,body", [
    ("pod.yaml", 'spec:\n  containers:\n    - command:\n        - claude\n'
                 '        - -p\n        - "go"\n'),
    ("docker-compose.yml", 'services:\n  t:\n    entrypoint:\n      - claude\n      - -p\n'),
    ("Dockerfile", 'FROM x\nENTRYPOINT ["claude"]\n'),
    ("lanes.json", '{"argv":["claude"]}\n'),
])
def test_block_form_list_argv_is_seen_not_only_the_flow_form(tmp_path, name, body):
    """`_is_element` demanded a delimiter on BOTH sides, so a YAML sequence item
    (`        - claude`, whose left neighbour is a space) reported 0 sites.

    The suite pinned only the FLOW forms — `args: ["claude","-p"]` — and an
    ADD-ONE corpus of hand-written shapes leaves the next syntax unguarded by
    construction. Hence the block rule: the token is the whole content of its
    line, modulo a sequence dash and trailing punctuation."""
    _write(tmp_path, name, body)
    code, report = _scan(tmp_path)
    assert code == 1, report
    assert report["sites"]


@pytest.mark.parametrize("name,body", [
    ("pod.yaml", 'spec:\n  containers:\n    - command: claude\n'
                 '      args: ["-p", "$(PROMPT)"]\n'),
    ("docker-compose.yml", 'services:\n  t:\n    entrypoint: claude\n'
                           '    command: ["-p", "$P"]\n'),
    ("w.yml", 'jobs:\n  a:\n    steps:\n      - run: claude\n'),
    ("mcp.json", '{"servers":{"x":{"command":"claude",\n"args":["-p","go"]}}}\n'),
    ("job.service", '[Service]\nExecStart=claude\n'),
])
def test_a_declarative_mapping_VALUE_is_seen(tmp_path, name, body):
    """`command: claude` with `args:` on the NEXT line reported 0 sites.

    S1 needs whitespace-then-something after the token, S2 does not treat `:` as
    a separator, and S3's `lead` was `command:` — so a flagless mapping value fell
    through every branch. These are the SAME files as the round-1 block-form
    Critical: the adjacent syntax in the same document was still deleted, which is
    what `fix-the-level-not-the-instance.md` calls patching the instance.

    In every declarative format here the value of such a key IS the command, so
    S4 needs no flag to vouch for it."""
    _write(tmp_path, name, body)
    code, report = _scan(tmp_path)
    assert code == 1, report
    assert report["sites"], report


@pytest.mark.parametrize("name,body", [
    ("cfg.yml", 'model: claude-opus-5\n'),
    ("cfg.yml", 'runtime: claude\n'),
])
def test_a_mapping_value_that_names_a_MODEL_is_not_a_command(tmp_path, name, body):
    """The bound on S4: `model:` and `runtime:` take a name, not a command.
    `==`-pinned, and a key missing from that set ADDS a candidate."""
    _write(tmp_path, name, body)
    code, report = _scan(tmp_path)
    assert code == 0, report["problems"]


@pytest.mark.parametrize("name,body", [
    ("x.sh", 'echo "$P" | claude\n'),
    ("x.sh", 'cd /tmp && claude\n'),
    ("x.sh", 'setup; claude\n'),
    ("x.sh", 'printf "%s" "$P" | xargs claude\n'),
    ("job.service", '[Service]\nExecStart=/usr/local/bin/claude\n'),
    ("r.c", 'int main(){ execvp("claude", a); }\n'),
])
def test_a_flagless_invocation_after_a_separator_is_seen(tmp_path, name, body):
    """Design point 5 says a claude invocation is a site whether or not it is
    headless — and a bare `claude` alone on a line WAS reported, so this was not
    a policy choice. `_first_on_line` matched the whole line prefix while the
    sibling `_command_position` implemented the same property correctly and was
    restricted to Python strings: one property, two implementations, the weaker
    one on the shell path (`fix-the-level-not-the-instance.md`)."""
    _write(tmp_path, name, body)
    code, report = _scan(tmp_path)
    assert code == 1, report


@pytest.mark.parametrize("name,body", [
    ("r.js", 'class R {\n  *run(u) { spawn("claude", ["-p", u]); }\n}\n'),
    ("r.go", 'func f(){ *p = 1; exec.Command("claude", "-p", u).Run() }\n'),
    ("r.js", '/* legacy */ spawn("claude", ["-p", u]);\n'),
    ("job.plist", '<!-- legacy --><string>/usr/bin/claude</string>\n<string>-p</string>\n'),
])
def test_a_comment_opener_that_is_not_a_comment_does_not_delete_the_line(tmp_path, name, body):
    """`_LINE_COMMENT["cfamily"]` carried `*`, which opens a generator method, a
    pointer deref and a block-comment continuation — and the whole-line demotion
    took the rest of the line with it. `/*` and `<!--` need the same care: they
    can CLOSE on the same line, after which what follows is executed code.

    The source claimed a whole-line judgment "is lexically unambiguous everywhere
    here". It was not."""
    _write(tmp_path, name, body)
    code, report = _scan(tmp_path)
    assert code == 1, report


def test_an_unlistable_directory_is_reported_not_silently_empty(tmp_path):
    """`os.walk` swallows a scandir failure and never yields the directory, so a
    chmod-000 dir holding a real spawn produced 0 sites, 0 unscannable, 0
    skipped, 0 notes, exit 0 — total silence. `_decode` covered the FILE-level
    analogue, so the fail-closed property was installed one level too low."""
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "spawn.sh").write_text('claude -p "$S"\n')
    (tmp_path / "ok.sh").write_text("echo hi\n")
    locked.chmod(0o000)
    try:
        code, report = _scan(tmp_path)
    finally:
        locked.chmod(0o755)
    assert code == 1, report
    assert any("not listable" in u["why"] for u in report["unscannable"]), report


def test_a_form_feed_does_not_desynchronise_the_python_line_maps(tmp_path):
    """`str.splitlines()` breaks on \x0c \x0b \x1c-\x1e \x85 \u2028 \u2029;
    `ast` and `tokenize` break on \n only. One form feed therefore shifted every
    raw line number relative to the docstring/comment/string maps built from the
    parser, and the resulting off-by-one aimed a `python-docstring` demotion
    straight at a real spawn — measured, exit 0."""
    body = ('def _sh(c):\n    pass\n\x0c\n\ndef go(u):\n'
            '    _sh(f"cd /tmp && claude -p {u}")\n')
    _write(tmp_path, "runner.py", body)
    code, report = _scan(tmp_path)
    assert code == 1, report
    assert report["sites"]


def test_show_mentions_renders_without_crashing(tmp_path):
    """The human-facing audit path for demotions raised KeyError on its first
    mention (`site['token']` after the field became `tokens`) and had zero test
    coverage — the helper here always passes --json, so the whole "a demotion is
    auditable" contract was enforced only in the JSON branch."""
    _write(tmp_path, "x.sh", '# claude -p in a comment\necho hi\n')
    proc = subprocess.run([sys.executable, SCRIPT, "--show-mentions", str(tmp_path)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "mention (comment)" in proc.stdout, proc.stdout


def test_a_fifo_does_not_hang_the_scanner(tmp_path):
    """`open(path,"rb").read()` on a FIFO blocked forever with no output at all,
    against a module whose contract is to fail closed LOUDLY."""
    os.mkfifo(str(tmp_path / "pipe"))
    (tmp_path / "ok.sh").write_text('claude -p x\n')
    proc = subprocess.run([sys.executable, SCRIPT, "--json", str(tmp_path)],
                          capture_output=True, text=True, timeout=30)
    report = json.loads(proc.stdout)
    assert any("not a regular file" in u["why"] for u in report["unscannable"]), report


def test_a_demoted_token_survives_a_spawn_on_the_same_line(tmp_path):
    """`tokens=spawn_tokens or [...]` discarded the demoted tokens whenever the
    line held any spawn, contradicting "every candidate reaches the output"."""
    _write(tmp_path, "x.sh",
           'MODELS="--model claude-opus-5" && cd "$HOME/.claude" && claude -p "$S"\n')
    code, report = _scan(tmp_path)
    assert code == 1
    reasons = {d["why"] for d in report["sites"][0]["also_demoted"]}
    assert "model-argument" in reasons and "config-dir" in reasons, report["sites"][0]


# --- ADD-ONE: shapes neither the author nor the Tier-2 reviewer wrote down -----

ADD_ONE = [
    ("makefile-oneliner-recipe", "Makefile",
     'claude-review: ; claude -p "$(U)"\n'),
    ("makefile-variable-binary", "Makefile",
     'CLAUDE ?= claude\nreview:\n\t$(CLAUDE) -p "$(U)"\n'),
    ("ci-yaml-block-scalar", "ci.yml",
     'jobs:\n  a:\n    steps:\n      - run: |\n          set -e\n'
     '          claude -p "$PROMPT"\n'),
    ("ci-yaml-list-argv", "ci.yml",
     'steps:\n  - uses: x\n    with:\n      args: ["claude", "-p", "$P"]\n'),
    ("plist-programarguments", "job.plist",
     '<key>ProgramArguments</key>\n<array>\n  <string>/usr/bin/claude</string>\n'
     '  <string>-p</string>\n  <string>$P</string>\n</array>\n'),
    ("json-config-argv", "lanes.json",
     '{"lanes":{"triage":{"argv":["claude","-p","{prompt}"]}}}\n'),
    ("toml-lane-registry", "lanes.toml",
     '[lane.triage]\nbin = "claude"\nargs = ["-p", "{prompt}"]\n'),
    ("shell-indirection-eval", "x.sh",
     'CMD="claude -p"\neval "$CMD \\"$U\\""\n'),
    ("shell-array-expansion", "x.sh",
     'ARGV=(claude -p "$U")\n"${ARGV[@]}"\n'),
    ("shell-default-value-binary", "x.sh",
     'exec "${CLAUDE:-claude}" -p "$S"\n'),
    ("dollar-paren-subshell", "x.sh",
     'OUT=$(claude -p "$S")\n'),
    ("xargs-pipeline", "x.sh",
     'printf "%s\\n" "$P" | xargs -0 claude -p\n'),
    ("windows-batch-var", "run.bat",
     '%CLAUDE% -p "%P%"\n'),
    ("tmux-send-keys", "x.sh",
     'tmux send-keys -t claude-workers:0.1 "claude -p \\"$S\\"" Enter\n'),
    ("dockerfile-cmd", "Dockerfile",
     'FROM x\nCMD ["claude", "-p", "$PROMPT"]\n'),
    ("systemd-unit", "job.service",
     '[Service]\nExecStart=/usr/local/bin/claude -p "%i"\n'),
    ("python-script-generator", "gen.py",
     'import pathlib\npathlib.Path("run.sh").write_text(f"claude -p {u}\\n")\n'),
    ("python-wrapper-list-concat", "x.py",
     'import subprocess\nPRE = ["env", "-u", "CLAUDE_AGENT"]\n'
     'subprocess.run(PRE + ["claude", "-p", u])\n'),
]


@pytest.mark.parametrize("label,name,body", ADD_ONE, ids=[a[0] for a in ADD_ONE])
def test_add_one_shapes_nobody_wrote_down(tmp_path, label, name, body):
    """`drift-guard-tests.md` § ADD-ONE, applied by a third party.

    A guard dies by being OVERRIDDEN far more often than by being deleted, and
    the shapes that kill it are the ones nobody on the previous two rounds
    thought to write. These were invented against the round-2 implementation
    with no knowledge of which branch handles them; the non-Python paths are
    pressed hardest, because only Python gets the AST treatment.
    """
    _write(tmp_path, name, body)
    code, report = _scan(tmp_path)
    assert code == 1, f"{label} went unreported: {report}"
    assert report["sites"], label


def test_this_repo_is_fully_readable_by_the_scanner(tmp_path):
    """Two properties this PR owns, separate from the marker pass.

    (1) Nothing in the tree is UNSCANNABLE — every file was decoded and, if
        Python, parsed. This is the fail-closed promise; a single unreadable file
        would mean the clean numbers below cover less than they appear to.
    (2) It still finds sites. A scanner reporting zero has gone blind, and that
        reads identically to a clean tree.

    It does NOT yet assert zero problems: the marker pass over this repo's real
    spawn sites (selffix-run.sh, gardener-run.sh, distill.py, bootstrap-recreate.sh)
    lands with the launcher change, not here — this PR is the detector alone.
    """
    code, report = _scan(os.path.join(REPO, "deploy"), os.path.join(REPO, "src"))
    assert not report["unscannable"], report["unscannable"]
    assert report["sites"], "scanner found NO spawn sites in this repo — it has gone blind"
    assert code in (0, 1)
