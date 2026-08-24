#!/usr/bin/env python3
"""Find every `claude`/`codex` spawn in a tree, and say which ones nobody vouched for.

WHAT THIS IS, STATED HONESTLY
-----------------------------
**This detects convention violations. It enforces nothing.**

The caller and the child run as the SAME UID. A call site can therefore rewrite
any policy file this repo ships, rewrite the launcher it is asked to use, or
simply `exec claude` — so no argv-level scheme is a security boundary, and this
scanner is not one either. What it does is make a deviation from the convention
VISIBLE in a diff, before it ships. That is worth having; it is not confinement,
and this file must never be cited as if it were.

WHY IT IS SHAPED THE WAY IT IS
------------------------------
Generation 1 enumerated the spawn SHAPES its author could think of and grew a new
blind spot every time somebody looked. Generation 2 answered that with "parse,
don't pattern-match" — and made the parser a GATE. A gate that does not recognise
a shape does not report it conservatively; it DELETES it. Measured on the two
unattended lanes this work exists for, both quoted (`"$CLAUDE" -p …`), generation
2 reported neither, while still exiting 1 on a neighbouring `echo` — a green
number replaced by a decoy.

So the ordering is inverted, and that inversion is the whole design:

  1. **The crude raw-token pass is the SAFETY NET. Parsing only REFINES it.**
     Any file whose text contains a binary token yields a CANDIDATE. The parser
     may add detail, may add sites the raw pass cannot see (a binary assembled
     from a variable), and may DOWNGRADE a candidate — but only by a rule that
     can state its reason. It can never make a candidate silently disappear.

  2. **A demotion must be lexical and same-stream.** A rule may only demote a
     candidate using the evidence stream that produced it: the token's own text,
     or a token from a real lexer. An AST-derived judgment never removes a raw
     candidate — otherwise the call-name list is re-installed as a filter through
     the back door, and `raw ∪ ast` stops meaning what it says.

  3. **Fail CLOSED on anything not understood.** A file that will not decode or
     will not parse is UNSCANNABLE and makes the run non-zero. A scanner that
     prints "0 spawn sites" for a file it could not read manufactures a green
     number.

  4. **Skip by an explicit pinned list, and DISCLOSE every skip.** A skip list is
     a classification surface: adding "scripts" to it once dropped every spawn in
     `deploy/scripts/` with the whole unit suite still green and no output field
     revealing it. The list is `==`-pinned, the walk that applies it is asserted
     behaviourally, and skipped paths are named in the report.

  5. **No print-flag requirement.** A claude invocation is a site whether or not
     it is headless; the marker says which.

Over-inclusion costs a line of output. Under-inclusion is invisible — and here it
was invisible over the two most dangerous unattended lanes on the machine. Every
trade below is resolved in that direction.
"""
import argparse
import ast
import errno as _errno
import io
import json
import os
import re
import stat
import sys
import tokenize


# Every token that could name a claude/codex binary: `claude`, `/path/to/claude`,
# `$CLAUDE`, `${PSP_CLAUDE_BIN}`, `claude_bin`, `CLAUDE`, `claude.local`. Broad by
# construction — a token-level test whose mis-implementation ADDS candidates.
# Case-insensitive: an upper-case `$CLAUDE` was invisible to generation 1, and
# that is how the least-trusted lane on the machine hid.
_BIN_TOKEN = re.compile(r"(?i)(?:^|[^\w])([\w./${}~+-]*(?:claude|codex)[\w./${}~-]*)")

# The captured token's final path segment, when the token IS the binary.
_BIN_BASENAME = re.compile(r"(?i)[\w.${}~+-]*(?:claude|codex)[\w.-]*")

# String literals, in every language here.
_STRING_SPAN = re.compile(
    r"""(?s)("""
    r'"""(?:[^"]|"(?!""))*"""'
    r"|'''(?:[^']|'(?!''))*'''"
    r'|"(?:\\.|[^"\\])*"'
    r"|'(?:\\.|[^'\\])*'"
    r"|`(?:\\.|[^`\\])*`"
    r")")

MARKER = re.compile(
    r"#\s*headless-lockdown:\s*(?P<verdict>wrapper|verified-by-test|exempt)"
    r"\((?P<detail>[^)]*)\)")

# DERIVED from the marker pattern, not maintained beside it: a verdict added to
# one and not the other is how a dispatch silently gains an unchecked branch.
VERDICTS = tuple(re.search(r"\(\?P<verdict>([^)]*)\)", MARKER.pattern).group(1).split("|"))

# The launcher this convention would route through. Referenced only by the
# `wrapper`-verdict refusal message; nothing INFERS routing any more.
LAUNCHER = "headless_spawn"
SKIP_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".tox", ".idea",
    "site-packages", ".gradle", "target",
})
# Directory SUFFIXES skipped. Its own pinned set rather than an inline clause in
# the walk: as an inline clause it dropped `pkg.egg-info/gen.sh` while
# `SKIP_DIRS` sat untouched and every pin stayed green.
SKIP_DIR_SUFFIXES = frozenset({".egg-info"})
# Binary/asset extensions that cannot carry a spawn. Anything NOT here is read.
SKIP_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".pdf",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".tar", ".whl", ".jar",
    ".so", ".dylib", ".dll", ".o", ".a", ".class", ".pyc", ".pyo",
    ".mp3", ".mp4", ".mov", ".wav", ".ttf", ".otf", ".woff", ".woff2",
    ".lock", ".sqlite", ".db",
    # Prose. NOT executed by anything, and demanding an exemption marker in every
    # design doc that discusses `claude -p` is how a reader learns to paste
    # markers without reading them. Stated as a limit rather than hidden: a
    # fenced block inside a SKILL or command file can still instruct a live
    # session to spawn something — that is prompt content, a different threat
    # class from a spawn site, and it is not what this scanner covers.
    ".md", ".markdown", ".rst", ".txt",
})

# Python callables that hand a list/string to the OS.
#
# NOT load-bearing for any spawn whose BINARY TOKEN APPEARS IN THE SOURCE — the
# precise claim, after the blanket version ("not load-bearing for coverage") was
# measured FALSE three times running. State it narrowly or not at all.
#
# Measured on this tree: emptying this set loses 9 AST-only detections, all of
# which are indirect spawns (`Popen(["bash", <script that spawns claude>])`) or
# false positives (a tmux call in terminal.py holding a CLAUDE_* name). It loses
# ZERO sites whose own statement names the binary — including every real claude
# invocation in this repo, pinned by name in the suite.
#
# Each earlier version of the claim was false for a different shape: a paren call
# (`self._exec(CLAUDE_BIN, …)`), then a bare Name sharing a CONTINUATION LINE of
# a multi-line argv list, which is how this repo formats its own spawns. Both are
# fixed in `_is_element`/`_is_argv_zero` — never by widening this table.
_PROCESS_CALLS = frozenset({
    "run", "call", "check_call", "check_output", "popen", "spawn", "spawnl",
    "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "posix_spawn", "posix_spawnp", "system", "execv", "execve", "execvp",
    "execvpe", "execl", "execlp", "execle", "start", "communicate",
    "create_subprocess_exec", "create_subprocess_shell", "getoutput",
    "getstatusoutput", "check_run", "fork_exec", "Popen",
})
_PROCESS_CALLS_LOWER = frozenset(c.lower() for c in _PROCESS_CALLS)

# Demotion reasons. `==`-pinned in the suite: this table is a classification
# surface, and a new entry is a new way for a real spawn to vanish.
DEMOTIONS = frozenset({
    "comment", "config-path", "config-dir", "path-prefix", "python-docstring",
    "xml-key", "model-argument", "no-invocation-shape",
})

# An invocation carries FLAGS. `-p`, `--model`, `-C`, `--strict-mcp-config`.
# The preceding class is `[^\w-]`, not a list of quote/space characters: a plist
# writes its flag as `<string>-p</string>`, where the preceding char is `>`. Same
# lesson as C-A one level down — an enumerated set of "characters that may sit
# before the thing I am looking for" is a coincidence detector.
_FLAG = re.compile(r"(?:^|[^\w-])(--?[A-Za-z][\w-]*)")
# How far a flag may sit from the binary and still vouch for it. A plist puts the
# binary and its `-p` in SEPARATE <string> elements and a TOML lane registry puts
# `args` a line or more below `bin`, so the distance is real.
#
# This IS a cliff, and it is pinned and disclosed rather than left implicit: at 4
# nothing in the suite exercised a distance above 1, so the rationale above was
# unmeasured and a real `mcp.json` flipped from detected to invisible on line
# spacing alone. Widened to 8 (over-inclusion is the safe direction) and the
# boundary is now asserted from both sides — `limitations` names the residual.
_FLAG_WINDOW = 8
# Flags whose VALUE is a model name, never a binary. `--model claude-opus-5` is
# the single commonest false positive in this tree. `==`-pinned; a flag missing
# from this set ADDS a candidate.
_MODEL_FLAGS = frozenset({"--model", "-m", "--fallback-model", "--small-model"})
_TRAILING_FLAG = re.compile(r"""(--?[A-Za-z][\w-]*)[\s"'`=]*$""")
# Keys whose value is a model/runtime NAME rather than a command. `==`-pinned;
# a key missing from this set ADDS a candidate.
_VALUE_KEYS_THAT_ARE_NOT_COMMANDS = frozenset({
    "model", "models", "fallback_model", "small_model", "runtime", "agent",
    "provider", "name", "type", "image",
})

# Characters that mean the token CONTINUES rather than ends — the only way a
# candidate is demoted on its neighbourhood. A denylist, not an allowlist: a
# character nobody anticipated is a boundary and therefore ADDS a candidate.
# Generation 2 had the allowlist, and `"` was not in it, which is exactly how
# `"$CLAUDE" -p` became invisible.
_CONTINUATION = frozenset({"/"})
# Parameter-expansion operators. Demote ONLY inside a `${…}` token: applied
# unconditionally, `:` deletes `${CLAUDE:-claude}`, `tmux -t claude-workers:0.1`
# and the Makefile one-liner `claude-review: ; claude -p`, and `%` deletes
# `%CLAUDE%`. Inside `${…}` nothing is lost — where the expansion names a binary
# the inner token is a separate candidate of its own.
_EXPANSION_OPS = frozenset({":", "#", "%"})
_QUOTE_CHARS = "\"'`"
# `%` joins the quotes for a Windows batch `%CLAUDE%`. NOTE: `%CLAUDE% -p "%P%"`
# is actually carried by `%` inside `_COMMAND_PREFIX` (S2), not by this set — an
# earlier comment here credited the wrong guard, which would send the next fixer
# to the wrong line. Removing `%` here leaves the suite green.
_DELIM_CHARS = _QUOTE_CHARS + "%"

# Python 3.12+ tokenizes an f-string as FSTRING_START / FSTRING_MIDDLE /
# FSTRING_END rather than one STRING, so matching on `tokenize.STRING` alone
# leaves every f-string looking like executed code. Resolved by NAME so this
# stays correct on older interpreters too.
_STRING_TOKENS = frozenset(
    t for t in (getattr(tokenize, n, None)
                for n in ("STRING", "FSTRING_START", "FSTRING_MIDDLE",
                          "FSTRING_END"))
    if t is not None)
# Punctuation the broad token regex sweeps up at a token's edges.
_TOKEN_EDGE = "{}()[]<>\"'`$,;:!-"

# The config directory, never a binary. `==`-pinned; the residual is a binary
# literally named `.claude`, which is stated in `limitations`.
_CONFIG_DIR_BASENAMES = frozenset({".claude", ".codex"})


class Site(dict):
    pass


# --- language model ------------------------------------------------------------

_EXT_LANG = {
    ".py": "python",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".ksh": "shell",
    ".yml": "yaml", ".yaml": "yaml",
    ".toml": "ini", ".ini": "ini", ".cfg": "ini", ".conf": "ini",
    ".xml": "xml", ".plist": "xml", ".html": "xml", ".htm": "xml",
    ".json": "json",
    ".js": "cfamily", ".mjs": "cfamily", ".cjs": "cfamily", ".ts": "cfamily",
    ".c": "cfamily", ".h": "cfamily", ".cc": "cfamily", ".cpp": "cfamily",
    ".java": "cfamily", ".go": "cfamily", ".rs": "cfamily", ".swift": "cfamily",
    ".mk": "make",
}

# Whole-line comment openers per language. A WHOLE-LINE judgment is lexically
# unambiguous everywhere here, which is why it is the only comment judgment
# allowed to demote outside Python.
_LINE_COMMENT = {
    "python": ("#",), "shell": ("#",), "yaml": ("#",), "make": ("#",),
    # `*` is GONE from cfamily: it opens a generator method
    # (`*run(u) { spawn("claude", …) }`), a pointer deref and a block-comment
    # continuation line, and demoting on it deleted real spawns whole. `/*` and
    # `<!--` survive only when they do not CLOSE on the same line — see
    # `_line_comment_start`.
    "ini": ("#", ";"), "cfamily": ("//", "/*"), "xml": ("<!--",),
    "json": (), "unknown": ("#", "//"),
}
# Trailing comment openers. Demotion on these is gated three ways (see
# `_trailing_comment_start`) because a heuristic here DELETES real code.
_TRAILING_COMMENT = {
    "python": ("#",), "shell": ("#",), "yaml": ("#",), "make": ("#",),
    "ini": ("#", ";"), "cfamily": ("//",), "xml": (), "json": (),
    "unknown": ("#", "//"),
}


def _language(path, text):
    ext = os.path.splitext(path)[1].lower()
    if ext in _EXT_LANG:
        return _EXT_LANG[ext]
    base = os.path.basename(path).lower()
    if base.startswith("makefile") or base.endswith(".mk"):
        return "make"
    first = _lines(text)[0] if text else ""
    if first.startswith("#!"):
        if "python" in first:
            return "python"
        if any(s in first for s in ("sh", "bash", "zsh", "ksh")):
            return "shell"
    return "unknown"


def _string_spans(line):
    return [m.span() for m in _STRING_SPAN.finditer(line)]


def _in_spans(idx, spans):
    return any(start <= idx < end for start, end in spans)


def _line_comment_start(line, lang):
    """Index where a WHOLE-LINE comment opens, or None.

    Unambiguous in every language here, so this is the one comment judgment
    permitted to demote a candidate outside Python.
    """
    stripped = line.lstrip()
    if not stripped:
        return None
    offset = len(line) - len(stripped)
    for opener in _LINE_COMMENT.get(lang, ()):
        if not stripped.startswith(opener):
            continue
        closer = {"/*": "*/", "<!--": "-->"}.get(opener)
        if closer and closer in stripped[len(opener):]:
            continue          # the comment ends on this line; code follows it
        return offset
    return None


def _trailing_comment_start(line, lang):
    """Index where a TRAILING comment opens, or None.

    Three guards, because an eager stripper here deletes executed code and the
    site with it. Each was measured against a real counter-example:

      * preceded by whitespace — `${MODEL#*=}` and `sed 's#a#b#'` keep their `#`;
      * outside every string literal — `echo "issue #123"` keeps its `#`;
      * outside `${…}` — belt for the first guard.

    The whitespace guard is also what keeps a bare URL from opening a "comment"
    in an unknown-extension file: `https://example.com` has its `//` preceded by
    `:`, not by a space.
    """
    spans = _string_spans(line)
    # A running depth, not the |${| x |}| cross product it used to be: that was
    # quadratic and cost 26 s on a single 40k-`${}` line.
    depth_at, depth = [], 0
    for i, ch in enumerate(line):
        if ch == "$" and line[i + 1:i + 2] == "{":
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
        depth_at.append(depth)
    for opener in _TRAILING_COMMENT.get(lang, ()):
        start = 0
        while True:
            idx = line.find(opener, start)
            if idx < 0:
                break
            start = idx + 1
            if idx == 0 or not line[idx - 1].isspace():
                continue
            if _in_spans(idx, spans):
                continue
            if depth_at[idx]:
                continue
            return idx
    return None


def _demotion_for(code, match, lang, py_comment_cols=None, in_docstring=False):
    """Why this token is NOT an invocation, or None to leave it a spawn.

    Every branch is a judgment about the token's own text or about a token from a
    real lexer. Nothing here consults a call graph — an AST-derived reason may
    not remove a raw candidate, or the union collapses back into a gate.
    """
    token = match.group(1)
    start, end = match.span(1)

    if in_docstring:
        return "python-docstring"
    if py_comment_cols is not None and start in py_comment_cols:
        return "comment"

    # Strip the shell/quote punctuation the token regex swept up on either edge.
    # Without it `${CLAUDE:-claude}` yields the inner token `-claude}`, whose
    # trailing `}` fails the basename match — so BOTH tokens on that line demote
    # and a real `exec "${CLAUDE:-claude}" -p "$S"` reports nothing. Caught by
    # this suite's own ADD-ONE sweep against a claim made in the spec and wrong.
    # Stripping only ever makes the basename MORE likely to match: the safe
    # direction.
    base = token.rstrip("/").split("/")[-1].strip(_TOKEN_EDGE)
    if base.lower() in _CONFIG_DIR_BASENAMES:
        return "config-dir"
    if not _BIN_BASENAME.fullmatch(base):
        return "config-path"

    after = code[end:].lstrip(_QUOTE_CHARS)
    nxt = after[:1]
    if nxt in _CONTINUATION:
        return "path-prefix"
    if token.startswith("${") and nxt in _EXPANSION_OPS:
        return "path-prefix"

    if lang == "xml":
        before = code[:start].rstrip(_QUOTE_CHARS)
        if before.endswith("<key>") and after.lstrip().startswith("</key>"):
            return "xml-key"
    return None


# A wrapper word may carry its OWN flags and their values before the real binary:
# `env -u CLAUDE_AGENT claude -p …` is the shape this repo's live spawns use, and
# without the trailing `(?:-\S+|\S+)` run it failed command position, which made
# the process-call table load-bearing for it — the fourth time that claim was
# measured false. The run is admitted ONLY after a recognised wrapper word, so
# ordinary prose (`manager-memory: claude -p exit …`) still does not qualify.
_WRAPPERS = (r"exec|env|sudo|doas|su|runuser|nohup|setsid|stdbuf|command|time"
             r"|timeout|xargs|nice|ionice|watch|parallel|script|\$\(")
_COMMAND_PREFIX = re.compile(
    r"""^[\s"'`(%]*(?:(?:""" + _WRAPPERS + r""")\s+(?:-{1,2}[^\s]*\s+|[\w.]+=[^\s]*\s+|[^\s-]\S*\s+)*"""
    r"""|[\w.]+=[^\s]*\s+)*"""
    r"""["'`%]*$""")


def _first_on_line(code, start):
    """Is the token the COMMAND on its line, rather than a value inside a call?

    `claude -p x`, `\tclaude -p x`, `"$CLAUDE" -p x`, `exec claude -p x` — yes.
    `w.get("claude_sid")` — no; the text before it is `w.get(`.
    """
    return bool(_COMMAND_PREFIX.match(code[:start]))


def _flag_lines(text):
    return {i + 1 for i, raw in enumerate(_lines(text)) if _FLAG.search(raw)}


def _is_element(code, match):
    """Is the token a standalone DATA ELEMENT — an argv item, XML text, a value?

    `["claude","-p"]`, `<string>/usr/bin/claude</string>`, `bin = "claude"`,
    `- claude`. The delimiters on BOTH sides are what separate an element from an
    ordinary shell assignment like `CLAUDE_DIR="$HOME/.claude"`, whose token is
    bounded by a line start and an `=`.

    ONE rule, replacing four positional special-cases that each had to be found
    by its own review round: the quoted item, the XML text node, the whole-line
    sequence item, and the continuation line. Each covered the position its repro
    happened to use and left the siblings blind — most recently
    `[…, "-u", "X", claude_bin, "-p", …]`, where the token is neither first on
    its line nor adjacent to the bracket.

    An element is delimited on BOTH sides by list punctuation, modulo quotes and
    whitespace, and a LINE BOUNDARY counts as a delimiter — which is what covers
    the continuation line without a special case of its own.
    """
    start, end = match.span(1)
    # (a) IMMEDIATE quote adjacency, judged BEFORE the quotes are stripped. The
    #     unification below strips them and then reads the character underneath,
    #     which is `:` or `=` for a quoted VALUE — `bin = "claude"`,
    #     `{"command":"claude"}`, `CLAUDE_BIN = "claude"`. Unifying four
    #     positional special-cases into one rule silently DROPPED this sub-form;
    #     a simplification may merge branches, never lose one.
    if (code[start - 1] if start else "") in "\"'`>%" and \
            code[end:end + 1] in "\"'`<,]}%":
        return True
    # (b) the general form: delimited on both sides by list punctuation.
    before = code[:start].rstrip(_QUOTE_CHARS + " \t")
    after = code[end:].lstrip(_QUOTE_CHARS + " \t")
    # `-` is the YAML sequence dash; `>` an XML text node; `%` a batch variable.
    # `(` is deliberately ABSENT: a paren call is handled by `_is_argv_zero`,
    # which marks the site `paren_argv0` so it lands in its own bucket. Admitting
    # `(` here would make `str(CLAUDE_REPO)` a strong signal and flood the
    # `confirmed` bucket with the noise the buckets exist to keep out of it.
    opens = not before or before[-1] in "[,>%-"
    closes = not after or after[0] in ",]})<>%"
    return opens and closes


_STRING_OPEN = re.compile(r"""^[A-Za-z]{0,3}(?:\"\"\"|'''|["'`])""")
_CMD_SEPARATORS = (";", "|", "&", "(", "`", "\n", "{")
# Outside Python only. `=` makes `ExecStart=/usr/local/bin/claude` command
# position and `[` makes `ENTRYPOINT ["claude"]`; inside a Python string they
# would make `"claude_sid": sid` and `f"note: claude -p exit"` command position
# too, which is how prose came back as spawns. `:` is deliberately NOT here — a
# YAML `run: claude -p` already qualifies through S1.
_SHELL_SEPARATORS = _CMD_SEPARATORS + ("[", "=")


def _command_position(code, start, floor=0, separators=_CMD_SEPARATORS):
    """Is the token at COMMAND position — start of the segment, not mid-phrase?

    Formal rather than vibes: scan back to the nearest command separator at or
    after `floor`, then require everything between it and the token to be
    whitespace, quotes, or an exec-wrapper word. `cd {d} && claude -p {q}` and
    `env X=1 claude -p` are command position; `manager-memory: claude -p exit`
    is not.

    Used ONLY inside Python string literals, where an invocation string starts
    with its binary and prose does not. Applying it to shell would be an
    under-inclusion risk this scanner cannot take.
    """
    seg_start = floor
    for sep in separators:
        idx = code.rfind(sep, floor, start)
        if idx >= 0:
            seg_start = max(seg_start, idx + 1)
    seg = code[seg_start:start]
    if seg_start == floor:
        opener = _STRING_OPEN.match(seg)
        if opener:
            seg = seg[opener.end():]
    return bool(_COMMAND_PREFIX.match(seg))


def _is_argv_zero(code, match, allow_paren=True):
    """Is the token the FIRST element of a bracketed list — i.e. argv[0]?

    `["codex","exec",u]`, `[CLAUDE_BIN, "-p", p]`. argv[0] IS the binary, so this
    needs no flag to vouch for it; requiring one left a flagless wrapper call
    (`_exec(["codex","exec",u])`) dependent on the AST's hand-maintained
    call-name table, which is the gate this module claims not to have.

    `(` is included as well as `[`. Restricting to `[` left
    `self._exec(CLAUDE_BIN, "-p", p)` and four siblings at 0 sites, exit 0 — a
    real unattended spawn reporting nothing. It costs noise (`str(CLAUDE_REPO)`
    inside a `["git", …]` call now qualifies, and the tree goes 136 → ~299
    sites), which is why a `(`-only match is BUCKETED rather than mixed in with
    confirmed spawns; see `_bucket`. Under-inclusion is invisible, over-inclusion
    is annoying, and this scanner exists because a missed site cost 2h09m with no
    manager.
    """
    lead = code[:match.start(1)].rstrip(_QUOTE_CHARS + " \t")
    return lead.endswith("[") or (allow_paren and lead.endswith("("))


def _bucket(site):
    """Which reporting bucket a spawn belongs in. Nothing is dropped by this.

    `paren-call-unconfirmed` is the widening above: an argv[0]-position token in
    a PAREN call that the AST could not confirm. Reported and counted like any
    other problem, but kept in its own labelled bucket so a reader is not handed
    ~299 undifferentiated lines and trained to paste exemption markers unread —
    which `drift-guard-tests.md` names as its own failure mode.
    """
    if site.get("ast_confirmed") or site.get("source") == "ast":
        return "confirmed"
    if site.get("paren_argv0"):
        return "paren-call-unconfirmed"
    return "confirmed"


def _is_mapping_value(code, match):
    """`key: <token>` where the token is the whole value — YAML/JSON/Docker/systemd."""
    start, end = match.span(1)
    lead = code[:start].rstrip(_QUOTE_CHARS + " \t")
    if not lead.endswith(":"):
        return False
    key = lead[:-1].rstrip(_QUOTE_CHARS + " \t").split()[-1:] or [""]
    if key[0].strip(_QUOTE_CHARS).lower() in _VALUE_KEYS_THAT_ARE_NOT_COMMANDS:
        return False
    return not code[end:].strip().strip(_QUOTE_CHARS + ",]}")


def _invocation_shape(code, match, vouched, in_string=None, string_floor=0,
                      allow_paren=True):
    """Does this token occurrence have the SHAPE of a binary being invoked?

    A UNION of positive signals, so a signal nobody thought of leaves the
    candidate reported rather than deleting it — `drift-guard-tests.md`, prefer
    the check whose mis-implementation ADDS cases.

      S1  followed by whitespace and then something — the command-line shape.
          `"$CLAUDE" -p …`, `claude -p \\`, `RUNTIME_CMD="claude ${RC_ARG}…"`.
          In PYTHON this is required to sit inside a string literal: Python
          source is not a command line, so `roots.claude_dir / "orchestrator"`
          and `claude_sid in pending` would otherwise both read as invocations.
          The Python identifier shapes are covered by S3 and by the AST pass,
          which is the right instrument there.
      S2  the command on its line, with nothing or a terminator after — a bare
          interactive launch, which carries no flags at all.
      S3  a standalone data ELEMENT with a flag near it: the argv shape. Covers
          the JSON `["claude","-p",…]`, the plist whose `-p` is a separate
          <string> two lines down, the TOML registry whose `args` sit on the
          next line, and `[claude_bin, "-p", P]`.

    Without this, every `claude_sid` dict key, every `CLAUDE_ORCH_*` env-var
    name and every `claude-opus-5` model string reports as a spawn — measured
    586 sites over deploy+src, which trains a reader to paste exemption markers
    unread, the failure `drift-guard-tests.md` names in its own right. It is
    also the manager brief's own candidate model ("the token near a `-p`-ish
    invocation") and the model generation 1 used to catch both live poller sites.
    """
    start, end = match.span(1)
    after = code[end:].lstrip(_DELIM_CHARS)
    if after[:1].isspace() and after.strip():
        # In Python, S1 additionally demands command position inside the string.
        # Python source is not a command line, and its strings carry pages of
        # prose that mention the binary mid-sentence.
        if in_string is None:
            return True
        if in_string and _command_position(code, start, string_floor):
            return True
    # S2. Outside Python, command position is judged from the nearest command
    # SEPARATOR, not from the start of the line: `echo "$P" | claude`,
    # `cd /tmp && claude` and `ExecStart=/usr/local/bin/claude` are invocations,
    # and `_first_on_line` alone reported none of them while a bare `claude` on
    # its own line WAS reported — one property, two implementations, the weaker
    # one on the shell path. Inside Python it stays line-anchored: `(` is a
    # separator there too, and `w.get("claude_sid")` would otherwise qualify.
    if in_string is not None:
        # Python. `_first_on_line` matches pure indentation, so without the
        # trailing condition every line STARTING with a `claude_*` expression
        # (`roots.claude_dir / "orchestrator",`) reads as a bare command.
        if _first_on_line(code, start) and (not after.strip()
                                            or after.lstrip()[:1] in ";|&)`"):
            return True
    elif (_command_position(code, start, separators=_SHELL_SEPARATORS)
          and code[end:end + 1] != "="):
        return True
    if (vouched and _is_element(code, match)
            and not match.group(1).lstrip(_QUOTE_CHARS).startswith("-")):
        return True
    if _is_argv_zero(code, match, allow_paren=allow_paren):
        return True
    # S4. A mapping VALUE: `command: claude`, `entrypoint: claude`, `- run: claude`,
    # `"command": "claude"`. In every declarative format here the value of such a
    # key IS the command, so this needs no flag to vouch for it — and requiring
    # one deleted a k8s `command: claude` whose `args:` sat on the very next line,
    # the adjacent syntax to the block form fixed in round 1.
    return _is_mapping_value(code, match)


def _is_model_argument(code, match):
    """Is the token the VALUE of a `--model`-style flag rather than a binary?

    `--model claude-opus-5 \\` has the command-line shape S1 and is the single
    commonest false positive in this tree. A binary is never a model argument,
    so this is a positive judgment about the token's own neighbourhood.
    """
    # A trailing-flag REGEX, not a whitespace split: the flag is routinely glued
    # to its surroundings — `CLAUDE_FLAGS=(--model claude-opus-5`, `MODELS="--model
    # claude-opus-5"` — where `.split()[-1]` returns the whole blob and matches
    # nothing.
    prev = _TRAILING_FLAG.search(code[:match.start(1)])
    return bool(prev) and prev.group(1) in _MODEL_FLAGS


def _scan_raw(path, text, lang, py_comment_cols=None, docstring_lines=frozenset(),
              py_string_cols=None):
    """Every binary token in the file, classified. Never returns fewer than it saw.

    `finditer`, not `search`: generation 2 examined only the FIRST token on a
    line, so a demoted leading token took the rest of the line with it —
    `cd "$HOME/.claude" && claude -p "$S"` reported ZERO sites, and this repo's
    own live spawn (`gardener-run.sh:530`) has that shape.
    """
    sites = []
    flagged = _flag_lines(text)
    for i, raw in enumerate(_lines(text)):
        line = i + 1
        whole = _line_comment_start(raw, lang)
        trailing = _trailing_comment_start(raw, lang)
        cols = py_comment_cols.get(line) if py_comment_cols else None
        spans = py_string_cols.get(line, []) if py_string_cols is not None else None
        vouched = any(ln in flagged
                      for ln in range(line - _FLAG_WINDOW, line + _FLAG_WINDOW + 1))
        spawn_tokens, demoted, spawn_spans = [], [], []
        strong_tokens = 0

        def shape(match, allow_paren=True, _raw=raw, _spans=spans, _v=vouched):
            """One definition, two callers. They were duplicated verbatim and
            differed only by `allow_paren`; a new signal added at one call site —
            the natural edit — would have made a token a spawn with
            `strong_tokens == 0`, dropping its line into the noisy bucket. That
            is the round-4 regression, reachable by a one-sided edit."""
            start = match.start(1)
            return _invocation_shape(
                _raw, match, _v,
                in_string=None if _spans is None
                else any(lo <= start < hi for lo, hi in _spans),
                string_floor=next((lo for lo, hi in (_spans or [])
                                   if lo <= start < hi), 0),
                allow_paren=allow_paren)
        for match in _BIN_TOKEN.finditer(raw):
            start = match.start(1)
            if whole is not None and start >= whole:
                reason = "comment"
            elif lang != "python" and trailing is not None and start >= trailing:
                reason = "comment"
            else:
                reason = _demotion_for(
                    raw, match, lang,
                    py_comment_cols=cols,
                    in_docstring=line in docstring_lines)
                if not reason and _is_model_argument(raw, match):
                    reason = "model-argument"
                if not reason and not shape(match):
                    reason = "no-invocation-shape"
            if reason:
                demoted.append((match.group(1), reason))
            else:
                spawn_tokens.append(match.group(1))
                spawn_spans.append(match.span(1))
                if shape(match, allow_paren=False):
                    strong_tokens += 1
        if not spawn_tokens and not demoted:
            continue
        # ONE record per line: a line is a spawn if ANY of its tokens is. Per-token
        # classification is what restores `cd "$HOME/.claude" && claude -p "$S"`,
        # but per-token REPORTING would print the same line several times and
        # train the reader to skim.
        sites.append(Site(
            path=path, line=line, end=line, text=_squash(raw), code=raw,
            lang=lang, source="raw", spawn_spans=spawn_spans,
            paren_argv0=bool(spawn_tokens) and strong_tokens == 0,
            tokens=spawn_tokens or [t for t, _ in demoted],
            # Demoted tokens are recorded even when the line also holds a spawn:
            # "every candidate reaches the output" has to mean every candidate.
            also_demoted=[{"token": tok, "why": why} for tok, why in demoted],
            kind="spawn" if spawn_tokens else "mention",
            demoted_because="" if spawn_tokens
            else ", ".join(sorted({r for _, r in demoted}))))
    return sites


# --- Python: the AST pass, which only ADDS ------------------------------------

def _py_strings_and_names(node):
    out = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            out.append(child.value)
        elif isinstance(child, ast.Name):
            out.append(child.id)
        elif isinstance(child, ast.Attribute):
            out.append(child.attr)
    return out


def _call_name(call):
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _simple_assignments(tree):
    """`name -> [value nodes]` for single-target assignments, `+=` included.

    `cmd = ["claude", "-p", p]` then `subprocess.run(cmd)` is the commonest real
    shape here, and the claude token lives in the ASSIGNMENT. AugAssign is
    tracked too — `cmd += ["claude","-p"]` was invisible to generation 2 even
    though the literal sits in plain sight.
    """
    table = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and \
                isinstance(node.targets[0], ast.Name):
            table.setdefault(node.targets[0].id, []).append(node.value)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            table.setdefault(node.target.id, []).append(node.value)
    return table


def _docstring_lines(tree):
    """Line numbers occupied by string-expression statements — provably not code."""
    lines = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            start = getattr(node, "lineno", None)
            end = getattr(node, "end_lineno", start)
            if start:
                lines.update(range(start, (end or start) + 1))
    return lines


def _scan_python_ast(path, text):
    """Sites the RAW pass cannot see: a binary assembled through a variable.

    This is a REFINEMENT, never a gate. Its call-name list may be incomplete and
    its dataflow is one level deep; both cost detail, not coverage, because every
    literal `claude` token is already a raw candidate. What it adds is the one
    shape with no literal in the source at all — `BIN = "…/claude"` then
    `run(f"{BIN} -p …")`.
    """
    tree = ast.parse(text, filename=path)
    lines = _lines(text)
    assigned = _simple_assignments(tree)
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node).lower() not in _PROCESS_CALLS_LOWER:
            continue
        parts = _py_strings_and_names(node)
        # `node.keywords` as well as `node.args`: `subprocess.run(args=cmd)` was
        # invisible to generation 2, and four characters is the whole difference.
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Name):
                    for value in assigned.get(sub.id, []):
                        parts.extend(_py_strings_and_names(value))
        blob = " ".join(parts)
        if not _BIN_TOKEN.search(blob):
            continue
        start = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", start) or start
        code = "\n".join(lines[start - 1:end])
        sites.append(Site(path=path, line=start, end=end, text=_squash(code),
                          code=code, lang="python", tokens=["<ast-resolved>"],
                          source="ast", kind="spawn", demoted_because=""))
    return sites, tree


def _py_comment_cols(text):
    """`{lineno: {col, …}}` for real Python comments, plus the comment stream.

    `tokenize` is a real lexer, which is why Python — and only Python — is
    allowed to demote on a TRAILING comment.
    """
    cols, stream, strings = {}, [], {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                row, col = tok.start
                cols.setdefault(row, set()).update(
                    range(col, col + len(tok.string) + 1))
                stream.append((row, tok.string))
            elif tok.type in _STRING_TOKENS:
                (srow, scol), (erow, ecol) = tok.start, tok.end
                for row in range(srow, erow + 1):
                    lo = scol if row == srow else 0
                    hi = ecol if row == erow else 1 << 30
                    strings.setdefault(row, []).append((lo, hi))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None, None, None
    return cols, stream, strings


# --- comments, for markers only ------------------------------------------------

def _comment_stream(text, lang):
    """(lineno, text) for WHOLE-LINE comments only.

    Markers may not be read out of arbitrary text. Generation 2 treated any line
    containing `#` as a comment, so a line of executed code could carry one:

        echo "docs say: # headless-lockdown: exempt(attended) — a human watches"
        claude -p "$U"

    reported 1 site and 0 problems. Restricting the marker stream to whole-line
    comments closes that; Python additionally gets its `tokenize` stream, which
    is a lexer rather than a guess.
    """
    out = []
    for i, raw in enumerate(_lines(text)):
        idx = _line_comment_start(raw, lang)
        if idx is not None:
            out.append((i + 1, raw[idx:]))
    return out


def _lines(text):
    """Split on \n ONLY, matching `ast`/`tokenize`.

    `str.splitlines()` also breaks on \x0c, \x0b, \x1c-\x1e, \x85, \u2028 and
    \u2029, so a single FORM FEED shifted every raw line number relative to the
    docstring/comment/string maps built from the parser — and the resulting
    off-by-one aimed a `python-docstring` demotion straight at a real spawn."""
    return text.split("\n")


def _squash(text):
    return " ".join(text.split())[:200]


# --- marker resolution ---------------------------------------------------------

def _marker_for(site, comments):
    """The marker governing a site: on its own lines, or in the block above.

    Bounded to a CONTIGUOUS run of comment lines immediately above, so a marker
    written for one site cannot drift down onto another added below it.
    """
    lines = {ln for ln, _ in comments}
    own = [txt for ln, txt in comments if site["line"] <= ln <= site["end"]]
    for text in own:
        match = MARKER.search(text)
        if match:
            return match
    probe = site["line"] - 1
    seen = 0
    while probe in lines and seen < 12:
        for ln, txt in comments:
            if ln == probe:
                match = MARKER.search(txt)
                if match:
                    return match
        probe -= 1
        seen += 1
    return None


def _skip_dir(name):
    return name in SKIP_DIRS or any(name.endswith(s) for s in SKIP_DIR_SUFFIXES)


def _iter_files(roots, skipped=None, notes=None, unscannable=None):
    """Yield scannable files; record every skip and every symlink outcome.

    `followlinks=True` with a visited-inode set: with the default, a root whose
    only content is `vendor -> ../outside` holding a real `claude -p` reported
    "0 spawn sites, 0 unscannable, 0 problems", exit 0 — the exact
    "green means I found nothing to look at" failure this module exists to
    prevent.
    """
    skipped = {"dirs": [], "files": []} if skipped is None else skipped
    notes = [] if notes is None else notes
    unscannable = [] if unscannable is None else unscannable
    visited = {}
    for root in roots:
        if os.path.isfile(root):
            yield root
            continue
        def _walk_error(exc, _sink=unscannable):
            # os.walk swallows a scandir failure and never yields the directory,
            # so a chmod-000 dir holding a real spawn produced 0 sites, 0
            # unscannable, exit 0 — the module's own "green means I found
            # nothing to look at" failure, one level above where _decode covers.
            name = _errno.errorcode.get(getattr(exc, "errno", None), "OSError")
            _sink.append({"path": getattr(exc, "filename", root) or root,
                          "why": f"directory not listable ({name}): {exc.strerror}"})

        for dirpath, dirnames, filenames in os.walk(root, followlinks=True,
                                                    onerror=_walk_error):
            try:
                st = os.stat(dirpath)
                key = (st.st_dev, st.st_ino)
            except OSError as exc:
                notes.append({"path": dirpath, "why": f"cannot stat: {exc.strerror}"})
                dirnames[:] = []
                continue
            if key in visited:
                # Two OUTCOMES, not one. The CHANGELOG claimed this split through
                # round 2 while the code had only the note — a claim nobody could
                # have checked without reading the walk.
                seen_at = visited[key]
                if dirpath != seen_at and dirpath.startswith(seen_at.rstrip(os.sep) + os.sep):
                    # A directory that is its own ANCESTOR: the walk was
                    # truncated by a loop, so say so loudly.
                    unscannable.append({"path": dirpath,
                                        "why": f"symlink cycle (already inside {seen_at})"})
                else:
                    # Ordinary: overlapping roots (`deploy deploy/scripts`) or two
                    # links into one tree. Informational — folding it into
                    # `unscannable` would poison the bucket I-4 exists to make
                    # meaningful.
                    notes.append({"path": dirpath,
                                  "why": "already scanned (second path to the same directory)"})
                dirnames[:] = []
                continue
            visited[key] = dirpath
            keep = []
            for d in dirnames:
                if _skip_dir(d):
                    skipped["dirs"].append({"path": os.path.join(dirpath, d),
                                            "why": "skip-list directory"})
                else:
                    keep.append(d)
            dirnames[:] = keep
            for name in sorted(filenames):
                ext = os.path.splitext(name)[1].lower()
                if ext in SKIP_EXTENSIONS:
                    skipped["files"].append({"path": os.path.join(dirpath, name),
                                             "why": f"skip-list extension {ext}"})
                    continue
                yield os.path.join(dirpath, name)


def _decode(path):
    """(text, problem). Three distinguishable causes, never one blurred message.

    Collapsing them sent the fixer hunting an encoding bug for a chmod-000 file
    and for a dangling symlink alike.
    """
    try:
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode):
            # A FIFO blocked `open().read()` forever with no output at all —
            # against a module whose contract is to fail closed, LOUDLY.
            return None, f"not a regular file (mode {stat.filemode(st.st_mode)})"
        with open(path, "rb") as fh:
            blob = fh.read()
    except OSError as exc:
        name = _errno.errorcode.get(exc.errno, "OSError")
        return None, f"cannot read ({name}): {exc.strerror}"
    if b"\x00" in blob[:8192]:
        return None, "binary: NUL byte in the first 8 KiB"
    try:
        return blob.decode("utf-8"), None
    except UnicodeDecodeError:
        try:
            return blob.decode("latin-1"), None
        except UnicodeDecodeError as exc:
            return None, f"undecodable text: {exc.reason}"


def _merge(raw_sites, ast_sites):
    """`raw ∪ ast`. An AST record never removes a raw candidate.

    An AST site whose statement span already holds a raw SPAWN is redundant — the
    raw one is kept and flagged `ast_confirmed`. An AST site over lines the raw
    pass only demoted, or did not see at all, is ADDED. That asymmetry is the
    whole point: the merge may promote, never demote.
    """
    out = list(raw_sites)
    for site in ast_sites:
        covered = [s for s in raw_sites
                   if s["kind"] == "spawn" and site["line"] <= s["line"] <= site["end"]]
        if covered:
            for s in covered:
                s["ast_confirmed"] = True
            continue
        site["ast_confirmed"] = True
        out.append(site)
    return out


def scan(roots):
    sites, unscannable = [], []
    skipped = {"dirs": [], "files": []}
    notes = []
    for path in _iter_files(roots, skipped, notes, unscannable):
        text, why = _decode(path)
        if text is None:
            unscannable.append({"path": path, "why": why})
            continue
        lang = _language(path, text)
        py_cols, py_stream, docstrings = None, None, frozenset()
        found = []
        if lang == "python":
            try:
                ast_sites, tree = _scan_python_ast(path, text)
            except (SyntaxError, ValueError, RecursionError) as exc:
                unscannable.append({"path": path, "why": f"python parse failed: {exc}"})
                continue
            py_cols, py_stream, py_strings = _py_comment_cols(text)
            if py_cols is None:
                unscannable.append({"path": path, "why": "python tokenize failed"})
                continue
            docstrings = _docstring_lines(tree)
            # A marker BLOCK is a contiguous run of WHOLE-LINE comments, in
            # Python too. `tokenize`'s COMMENT stream also carries TRAILING
            # comments, so a line of executed code counted as part of the run and
            # an exemption written for one site drifted down over a
            # --dangerously-skip-permissions spawn eleven lines below. The `.sh`
            # path never had this because its stream is whole-line only — which
            # is exactly why `test_a_marker_cannot_drift_onto_a_site_added_below_it`
            # passed while the property was broken for Python.
            whole = {ln for ln, _ in _comment_stream(text, "python")}
            py_stream = [(ln, txt) for ln, txt in py_stream if ln in whole]
            raw_sites = _scan_raw(path, text, lang, py_cols, docstrings, py_strings)
            found = _merge(raw_sites, ast_sites)
            comments = py_stream
        else:
            found = _scan_raw(path, text, lang)
            comments = _comment_stream(text, lang)
        for site in found:
            marker = _marker_for(site, comments)
            site["verdict"] = marker.group("verdict") if marker else None
            site["detail"] = (marker.group("detail") or "").strip() if marker else ""
            site.setdefault("ast_confirmed", False)
            site["bucket"] = _bucket(site) if site["kind"] == "spawn" else ""
            sites.append(site)
    return sites, unscannable, skipped, notes


def classify(sites, unscannable):
    problems = []
    for site in sites:
        if site["kind"] != "spawn":
            continue
        verdict, detail = site["verdict"], site["detail"]
        if verdict is None:
            problems.append((site, "no headless-lockdown marker"))
        elif not detail:
            # EVERY verdict needs a reason, `wrapper` included. A bare
            # `# headless-lockdown: wrapper` used to exempt anything at all —
            # including a spawn carrying --dangerously-skip-permissions.
            problems.append((site, f"marker '{verdict}' carries no detail"))
        elif verdict == "wrapper":
            # An unverifiable self-declaration is not a marker. This scanner no
            # longer INFERS launcher routing at all (see the note above
            # `_iter_files`), and `{LAUNCHER}.py` is not in this tree, so a
            # `wrapper` claim cannot be true here.
            problems.append((site, "marker claims 'wrapper' but this scanner does "
                                   f"not invoke or infer {LAUNCHER} routing — use "
                                   "exempt(<class>) or verified-by-test(<path>)"))
        elif verdict in ("exempt", "verified-by-test"):
            # The two verdicts a human can actually satisfy: a reason nobody can
            # machine-check, deliberately written, and read by a reviewer.
            continue
        else:
            # DEFAULT-DENY. Without this `else`, a verdict added to MARKER but
            # not handled here falls through as a silent, unchecked exemption —
            # in a scanner whose whole thesis is that exemptions must be explicit
            # and auditable. The sibling `artifact-put-clobber` guard in this repo
            # was fixed for exactly this shape.
            problems.append((site, f"marker verdict '{verdict}' is not handled by "
                                   "this scanner — refusing rather than exempting"))
    for entry in unscannable:
        problems.append((entry, f"UNSCANNABLE — {entry['why']}"))
    return problems


def _bucket_counts(spawns):
    """Counts keyed by the buckets actually present, never a fixed tuple."""
    out = {"confirmed": 0, "paren-call-unconfirmed": 0}
    for site in spawns:
        out[site.get("bucket") or "unlabelled"] = \
            out.get(site.get("bucket") or "unlabelled", 0) + 1
    return out


def _limitations(sites, skipped):
    return [
        "detection only — caller and child share a uid, so this is a "
        "convention check, not a boundary",
        f"a binary whose name contains no claude/codex token ANYWHERE in the "
        f"source is invisible to this scanner: a `dw -> claude` shim, a PATH "
        f"shadow, or argv read at runtime from a data file. The raw-token pass "
        f"is the safety net only for names it can see.",
        f"{len(skipped['dirs'])} directory(ies) and {len(skipped['files'])} "
        f"file(s) were SKIPPED by policy and not read at all; a spawn inside one "
        f"of them is not scanned and this run can still exit 0. They are listed "
        f"under `skipped`.",
        "symlinked directories ARE followed, with a visited-inode set: a "
        "directory that is its own ancestor is reported as a symlink cycle "
        "(a problem), a second path to an already-walked tree is a note. A "
        "symlink to / or $HOME is not a cycle by inode and would walk that tree",
        "9 AST-only detections in this tree depend on the process-call name "
        "list; all are indirect spawns or false positives, and no spawn naming "
        "its binary in-source depends on it",
        "an argv[0]-position token inside a PAREN call is reported in the "
        "`paren-call-unconfirmed` bucket: accepting `(` is what makes "
        "`self._exec(CLAUDE_BIN, '-p', p)` visible at all, and it triples the "
        "count on a Python-heavy tree. Nothing is dropped; read the buckets",
        f"a data-file argv whose flags sit more than {_FLAG_WINDOW} lines from "
        f"the binary is demoted to a mention: a plist/JSON/YAML registry is "
        f"vouched for by a nearby flag, and that window is a cliff. Removing it "
        f"needs a per-format parser.",
        "the demotion table is a maintained classification surface — a new "
        "entry is a new way for a real spawn to be reported as a mention",
        f"the INFERRED launcher exemption was removed: it granted a false "
        f"exemption in five consecutive review rounds and had no reachable true "
        f"positive ({LAUNCHER}.py is not in this tree). Nothing is exempted "
        f"without an explicit marker, and the `wrapper` verdict is REFUSED "
        f"outright because nothing here can confirm it — use "
        f"`exempt(<class>) — <why>` or `verified-by-test(<path>) — <why>`, "
        f"whose detail a human reads",
        "only Python is parsed; every other language is token-matched, and a "
        "spawn assembled across shell variables (C=claude; $C -p) is resolved "
        "only when the assignment carries the token",
        "prose files (.md/.rst/.txt) are not scanned — a fenced spawn in a "
        "skill body is prompt content, a different threat class",
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="headless_scan.py",
        description="Detect claude/codex spawns that deviate from the launcher "
                    "convention. Detection only — this enforces nothing.")
    parser.add_argument("roots", nargs="+")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--show-mentions", action="store_true",
                        help="also print demoted candidates and why")
    args = parser.parse_args(argv)

    missing = [r for r in args.roots if not os.path.exists(r)]
    if missing:
        print(f"headless_scan: root(s) do not exist: {', '.join(missing)}", file=sys.stderr)
        return 2

    sites, unscannable, skipped, notes = scan(args.roots)
    problems = classify(sites, unscannable)
    spawns = [s for s in sites if s["kind"] == "spawn"]
    mentions = [s for s in sites if s["kind"] != "spawn"]

    if args.json:
        json.dump({
            "sites": [dict(s) for s in spawns],
            "buckets": _bucket_counts(spawns),
            "mentions": [dict(s) for s in mentions],
            "unscannable": unscannable,
            "skipped": {
                "dirs": skipped["dirs"], "files": skipped["files"],
                "counts": {"dirs": len(skipped["dirs"]),
                           "files": len(skipped["files"])},
            },
            "notes": notes,
            "problems": [{"where": dict(w) if isinstance(w, dict) else w, "why": why}
                         for w, why in problems],
            "limitations": _limitations(sites, skipped),
        }, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        # Two labelled sections, not one undifferentiated list. The
        # `paren-call-unconfirmed` bucket is the cost of seeing
        # `self._exec(CLAUDE_BIN, "-p", p)` at all; keeping it separate is what
        # stops a reader skimming ~300 lines and pasting markers unread.
        # Derived from what `_bucket` actually returned, plus a catch-all: a
        # hand-maintained label tuple silently printed NOTHING for a bucket
        # nobody added to it, while the footer still counted the problem.
        for label in sorted({s.get("bucket", "") for s in spawns} | {""}):
            chunk = [(w, why) for w, why in problems
                     if (w.get("bucket", "") if isinstance(w, dict) else "") == label]
            if not chunk:
                continue
            if label == "paren-call-unconfirmed":
                print(f"--- {label}: argv[0]-position token in a paren call the "
                      f"parser could not confirm ({len(chunk)}) ---")
            elif label == "confirmed":
                print(f"--- {label} ({len(chunk)}) ---")
            for where, why in chunk:
                path = where["path"]
                line = where.get("line", "?")
                print(f"{path}:{line}: {why}")
                if where.get("text"):
                    print(f"    {where['text']}")
        if args.show_mentions:
            for site in mentions:
                print(f"{site['path']}:{site['line']}: mention "
                      f"({site['demoted_because']}) — {', '.join(site['tokens'])}")
        by_bucket = _bucket_counts(spawns)
        print(f"{len(spawns)} spawn site(s) "
              f"({', '.join(f'{v} {k}' for k, v in sorted(by_bucket.items()))}), "
              f"{len(mentions)} mention(s), "
              f"{len(unscannable)} unscannable file(s), "
              f"{len(skipped['dirs'])} dir(s) + {len(skipped['files'])} file(s) "
              f"skipped by policy, {len(problems)} problem(s)")
        print("NOTE: detection only. Same-uid callers can bypass any of this. A "
              "skipped path was never read, so a clean run does not cover it; "
              "run with --json to see `skipped` and `limitations`.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
