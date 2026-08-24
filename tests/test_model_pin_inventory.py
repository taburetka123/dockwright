"""Every live --model pin in the shipping surface is an explicit, roster-listed id.

Scope: src/dockwright/**, deploy/** (minus the compose-prose agents/ surface
and .md mirrors), and setup.sh — the shipping payload roots per the repo
layout contract (CLAUDE.md: "deploy/ — everything setup.sh copies into
~/.claude"). A file shipped from OUTSIDE these roots would evade the sweep —
the repo layout contract forbids that, and there is no tractable parse of
setup.sh's copy commands that would be less fragile than the contract.

Three layers, per ~/.claude/rules/drift-guard-tests.md:

1a. Executed-lane SHAPE check: each spawn lane is DRIVEN through its real
    code path (bash --dry-run, FakeDrv argv capture, config-default
    resolution, template parse) and the resolved --model value must be an
    explicit id (never a bare opus/sonnet/haiku/fable alias, never
    *-latest).
1b. Roster MEMBERSHIP check: the same executed-lane values must additionally
    be members of the MODEL ROSTER table (~/.claude/rules/sdd-model-tiers.md),
    checked once in test_all_pins_roster_membership.
2.  Site-discovery sweep: a comment/docstring-stripped scan of the whole
    shipping surface; the {relpath: count} map of --model occurrences must
    EXACTLY equal EXPECTED_PIN_SITES, so a NEW pin site fails loudly instead
    of being silently un-guarded (the fixed-lane weakness
    test_skip_perms_lane_parity.py records as a known Minor).

Layers 1a and 2 are repo-self-contained and run EVERYWHERE, CI included.
Layer 1b runs only where an operator roster exists (see
_roster_for_membership) and SKIPS with a reason elsewhere (CI, public
clones) — 1a and 2 still enforce everywhere, so the skip never leaves the
surface unguarded. On an operator machine (rules dir present) a missing
roster FAILS LOUD, never skips.

Grandfather: the distill lane pins claude-sonnet-4-6 — explicit, predates the
roster table, deliberately NOT flipped here. It is EXACT-pinned below so any
change to it must revisit this guard and the roster together.

deploy/agents/** and *.md anywhere are OUT of scope for the EXECUTED-pin
discovery registry (EXPECTED_PIN_SITES / test_no_unregistered_pin_sites)
only: they are the compose-prose surface, never a shell-executed spawn
path, and matching them there would be exactly the prose-blinded guard
drift-guard-tests.md forbids. manager.core.md:45-50 DOES carry literal
--model MIRROR pins in prose (the manager's roster-synced lanes), and
vars.defaults.toml:71 carries --model inside a prose agent-var string —
both render into deployed agent PROSE and are LIVE routing surfaces (the
manager reads deployed manager.md #10 and routes real spawns off these
values). Excluding them from EXPECTED_PIN_SITES does not leave them
unguarded: a third layer, the prose-mirror check
(_prose_pin_values / test_prose_mirror_pins_explicit, plus the
`prose:<path>[i]` entries folded into test_all_pins_roster_membership),
validates `--model`/`/model`-adjacent code-span tokens (every such value,
no allowlist) across every deploy/**.md file plus every non-.md file under
deploy/agents/ — shape everywhere, membership wherever an operator roster
exists. A fourth check, test_prose_unanchored_model_occurrences_fail_closed,
closes the "pin outside a backtick code span" boundary for
`--model`/`/model`-adjacent text: raw occurrence accounting has no span
requirement, so an occurrence the extractor cannot anchor a value to is a
DEGRADED PIN and fails loud, reconciled per file — via the UNION of
accounted and registered files, so a registered file that vanishes from
the accounting still reports its leftover anchor — against the explicit
PROSE_VALUELESS_MENTIONS registry, whose values are each mention's own
distinctive source-line substring (not a bare count: a reworded-into-
degraded mention or a cancel-out edit pair both break the anchor loudly).
The raw counter is case-insensitive so a case-gamed pin (`--MODEL x`) is
counted and, unparseable by the case-sensitive span regex, flagged rather
than invisible. Remaining prose boundaries: (a) standalone non-flag-
adjacent model mentions (e.g. "Escalate to `claude-opus-5`", "pinned
`claude-opus-5[1m]` in code") — NOT machine-validated by this layer, they
remain under the roster rule's human mirror-sync discipline; (b) a raw
occurrence swallowed inside a parsed span token (e.g.
`` `--model claude-opus-5--model` ``) — caught only by roster membership,
which skips off-operator (review A13, pre-existing). No other known
evasion survives the anchor-registry + union + case fixes (each closed
hole cites its review id: A2/A3, A10/A11, F3, A15). Their sync against the
roster is otherwise a manual discipline (~/.claude/rules/sdd-model-tiers.md:
"manager-arm roles also need the mirrored pin updated in
deploy/agents/manager.core.md + a setup.sh deploy"), which the prose-mirror
layer now backstops with an automated check.

Dash-variant closure: a smart-punctuation dash mangling a flag (en-dash,
em-dash, non-breaking hyphen, any Unicode category-Pd char, plus the minus-
sign lookalikes − ﹣ － — none of them the ASCII hyphen both prose regexes
hard-code) previously matched NEITHER regex and vanished from the accounting
entirely. Closed by normalizing a copy of the text (every Pd char plus the
minus lookalikes folds 1:1 to ASCII '-', so line/position math survives) and
comparing the ORIGINAL slice against the match on the NORMALIZED copy: any
span where they differ contained a non-ASCII dash and is reported as an
unmatched occurrence, so a mangled flag now fails by position instead of
disappearing. Slash-variant `/model` lookalikes (fraction slash, division
slash, and similar Unicode slash characters) are a stated non-covered
exotic — no normalization is applied to `/model`, so those remain outside
this net.

Measured coverage magnitude (this session, real tree): of prose model
mentions, 6 flag-adjacent pins (--model/--model-adjacent code-span tokens)
are machine-validated by this net, while standalone model mentions outside
it number 9 (backticked model-family ids) to 15 (all backticked `claude-*`
tokens) depending on the counting rule — a MAJORITY of prose model mentions
sit outside the machine net and remain under the roster rule's human
mirror-sync discipline.

pr-review-run is NOT in this repo (deployed from claude-config to
~/.claude/scripts/) and cannot be guarded from this suite.

evals/ is also OUT of scope: dev-only harness spawns never shipped by
setup.sh, carrying their own model defaults independent of this roster.

Publish exclusion is still required: the membership skip (_roster_for_
membership) fires only when BOTH the roster file AND ~/.claude/rules/ are
absent. A public clone that keeps ANY global rules directory at
~/.claude/rules/ (a common, unrelated convention) but has no
sdd-model-tiers.md does not hit that skip — it falls through to
_roster_models()'s FAIL LOUD assert, a hard red suite carrying an
operator-specific message. Layers 1a and 2 are repo-self-contained and
correctly enforce everywhere, but layer 1b is not universally safe off-
operator, so this module still belongs in the dockwright-publish excludes
for the public export.
"""
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tokenize
import ast
import tomllib
import unicodedata
from pathlib import Path

import pytest

from dockwright import config, manager_launch, spawner

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "dockwright"
DEPLOY = REPO / "deploy"
SCRIPTS = DEPLOY / "scripts"
AGENTS_DIR = DEPLOY / "agents"
SETUP_SH = REPO / "setup.sh"
ROSTER_PATH = Path.home() / ".claude" / "rules" / "sdd-model-tiers.md"
RULES_DIR = Path.home() / ".claude" / "rules"
BOOTSTRAP = SCRIPTS / "bootstrap-recreate.sh"
STALE_MONITOR_PATH = SRC / "stale_monitor.py"

EXPLICIT_RE = re.compile(r"^claude-[a-z0-9.-]+$")
DISTILL_GRANDFATHER = "claude-sonnet-4-6"

# Every file in the shipping surface that carries `--model` on an executed
# line, with its occurrence count. A mismatch means a pin site appeared,
# moved, or vanished: wire the new site into an executed-lane check below AND
# update this map in the same PR.
EXPECTED_PIN_SITES = {
    "deploy/scripts/bootstrap-recreate.sh": 2,
    "deploy/scripts/gardener-run.sh": 2,
    # NOT a pin: headless_scan's _MODEL_FLAGS names the flags whose VALUE is a
    # model, so it can demote `--model claude-opus-5` from spawn to mention.
    # Registered because this registry is deliberately broad — the executed-lane
    # check below asserts the site carries no model ID and so cannot become a
    # stealth pin.
    "deploy/scripts/headless_scan.py": 1,
    "deploy/scripts/selffix-run.sh": 1,
    "src/dockwright/distill.py": 1,
    "src/dockwright/manager_launch.py": 1,
    "src/dockwright/mcp_server.py": 1,
    "src/dockwright/spawner.py": 2,
    "src/dockwright/stale_monitor.py": 1,
}


# Anchored to the flag's own backtick code span: every real prose pin in this
# repo is written `--model <id>` / `/model <id>` inside one code span, so the
# captured token is exactly the value the prose instructs — and EVERY value is
# validated (no model-shaped allowlist: `opusplan`, `default`, `inherit`,
# `gpt-*` and any future alias all reach _assert_explicit and fail there).
PROSE_MODEL_RE = re.compile(r"`(?:--model|/model)[\s=]+([^`\s]+)`")

# Position-anchored exemptions: each registered value-less mention binds to
# a distinctive substring of ITS OWN source line, consumed 1:1 against
# un-anchorable occurrences. A bare COUNT is gameable two ways (proven in
# review: reword a registered mention into a degraded pin — count
# conserved; or cancel a new degraded pin against a removed mention).
# Rewording canon prose that carries an anchor breaks this loudly — that
# is the design: exemption edits are deliberate, never incidental.
PROSE_VALUELESS_MENTIONS = {
    "deploy/agents/manager.core.md": [
        "pass an explicit `--model` in `extra_args`",
    ],
    "deploy/agents/vars.defaults.toml": [
        "NOT by this spawn `--model`",
    ],
}

# Files that MUST each yield ≥1 extracted pin. Without this floor the layer
# goes green over a file it stopped reading: rewording a routing lane away
# from `--model` adjacency (e.g. the roster's role→model arrow form) would
# silently drop the file from `found` while a bare alias sits live in it —
# proven in review. Shrinking this set is a DELIBERATE edit with its own
# justification, never a side effect.
PROSE_PIN_FLOOR = {
    "deploy/agents/manager.core.md",
    "deploy/commands/manager.md",
}

# Case-insensitive on purpose (fail-closed direction): `--MODEL x` must be
# COUNTED (and, unparseable by the case-sensitive span regex, flagged) —
# not invisible. The span regex stays case-sensitive: a case-gamed pin is
# a defect to surface, not a value to accept.
PROSE_RAW_RE = re.compile(r"(?:--model|/model)(?![A-Za-z-])", re.IGNORECASE)


def _normalize_dashes(text: str) -> str:
    """Every Unicode dash-punctuation char (category Pd: hyphen, en/em dash,
    non-breaking hyphen, figure dash, …) plus minus-sign lookalikes folds to
    ASCII '-'. 1:1 per char, so positions/line numbers survive."""
    return "".join(
        "-" if (unicodedata.category(c) == "Pd" or c in "−﹣－")
        else c
        for c in text)


# Flag lookalike on NORMALIZED text: 1–4 dashes directly attached to
# "model" (en-dash folds 1:1, so a smart-punctuated `--model` arrives here
# as `-model`). Lookbehind keeps compounds like "cost-model" out.
MANGLED_FLAG_RE = re.compile(
    r"(?<![A-Za-z0-9])-{1,4}model(?![A-Za-z-])", re.IGNORECASE)


def _prose_files() -> list[Path]:
    """All shipped prose: deploy/**.md plus EVERY non-.md file under
    deploy/agents/ (the compose surface) — a stray non-md/non-toml file
    there was previously guarded by NEITHER sweep (review A15)."""
    md = sorted(DEPLOY.rglob("*.md"))
    agents_extra = [p for p in sorted((DEPLOY / "agents").rglob("*"))
                    if p.is_file() and p.suffix != ".md"
                    and "__pycache__" not in p.parts]
    return md + agents_extra


def _account_text(text: str) -> tuple[list[str], list[tuple[int, str]]]:
    """(parsed pin values, [(line_no, line_text)] of raw occurrences not
    consumed by a full flag+value span match) for one prose text."""
    lines = text.splitlines()
    full_spans = [m.span() for m in PROSE_MODEL_RE.finditer(text)]
    parsed = [m.group(1) for m in PROSE_MODEL_RE.finditer(text)]
    unmatched = []
    for m in PROSE_RAW_RE.finditer(text):
        if not any(s <= m.start() < e for s, e in full_spans):
            ln = text.count("\n", 0, m.start()) + 1
            unmatched.append((ln, lines[ln - 1] if ln <= len(lines) else ""))
    norm = _normalize_dashes(text)
    if norm != text:
        for m in MANGLED_FLAG_RE.finditer(norm):
            if text[m.start():m.end()] != m.group(0):
                # ≥1 dash in this span was a non-ASCII variant in the
                # original — a pin no regex on the raw text can see. Unknown
                # shape ⇒ visible and failing, never invisible.
                ln = text.count("\n", 0, m.start()) + 1
                unmatched.append((ln, lines[ln - 1] if ln <= len(lines) else ""))
    return parsed, unmatched


def _prose_pin_accounting() -> dict[str, tuple[list[str], list[tuple[int, str]]]]:
    out: dict[str, tuple[list[str], list[tuple[int, str]]]] = {}
    for path in _prose_files():
        parsed, unmatched = _account_text(path.read_text(errors="replace"))
        if parsed or unmatched:
            out[str(path.relative_to(REPO))] = (parsed, unmatched)
    return out


def _prose_pin_values() -> dict[str, list[str]]:
    """Model-shaped --model//model tokens in shipped PROSE: every deploy/**
    .md plus the compose-var defaults (vars.defaults.toml renders into
    deployed agent prose)."""
    return {f: parsed for f, (parsed, _) in _prose_pin_accounting().items() if parsed}


def _prose_accounting_problems(accounting, registry) -> list[str]:
    """Pure checker so its failure modes are unit-drivable. Iterates the
    UNION of files — a registered file that vanished from the accounting
    still reports its leftover anchors (review A10/A11: the vanish arm was
    vacuous when keyed on accounting alone)."""
    problems: list[str] = []
    for where in sorted(set(accounting) | set(registry)):
        _parsed, unmatched = accounting.get(where, ([], []))
        anchors: list[str | None] = list(registry.get(where, []))
        for ln, line_text in unmatched:
            for i, a in enumerate(anchors):
                if a is not None and a in line_text:
                    anchors[i] = None
                    break
            else:
                problems.append(
                    f"{where}:{ln}: un-anchorable `--model` occurrence with "
                    f"no registered value-less anchor — a degraded pin (fix "
                    f"its markup so flag AND value share one code span) or "
                    f"an unregistered new mention (register it deliberately)")
        leftover = [a for a in anchors if a is not None]
        if leftover:
            problems.append(
                f"{where}: registered value-less mention(s) no longer "
                f"found: {leftover} — removed or reworded; update the "
                f"registry deliberately")
    return problems


def _strip_1m(model: str) -> str:
    return model[:-4] if model.endswith("[1m]") else model


def _assert_explicit(model: str, where: str, roster: set[str] | None) -> None:
    base = _strip_1m(model)
    assert EXPLICIT_RE.match(base), (
        f"{where}: --model {model!r} is not an explicit claude-* id — bare "
        f"aliases silently change meaning across releases (roster rule 1)")
    assert not base.endswith("-latest"), (
        f"{where}: --model {model!r} is a floating -latest alias — same drift "
        f"class as a bare alias")
    if roster is not None:
        assert base in roster, (
            f"{where}: --model {model!r} is not in the MODEL ROSTER table "
            f"({ROSTER_PATH}); either the roster moved on and this site was "
            f"missed, or a new pin needs a roster decision first")


def _roster_models(path: Path = ROSTER_PATH) -> set[str]:
    """Distinct model ids from the roster table. Health floor is on ROW
    count, never on distinct ids: the healthy roster is many rows resolving
    to ~3 distinct models, so a distinct-count floor would fail day-one."""
    assert path.is_file(), (
        f"MODEL ROSTER not found at {path} — this guard validates pins "
        f"against it and must FAIL LOUD, never pass empty")
    rows = 0
    ids = set()
    for line in path.read_text().splitlines():
        m = re.match(r"^\|\s*`[A-Z0-9_]+`\s*\|\s*`([^`]+)`\s*\|", line)
        if m:
            rows += 1
            ids.add(_strip_1m(m.group(1).strip()))
    assert rows >= 10, (
        f"MODEL ROSTER table at {path} parsed to only {rows} row(s) — the "
        f"table moved or the format changed; fix the parser, never pass empty")
    return ids


def _roster_for_membership(path: Path = ROSTER_PATH, rules_dir: Path = RULES_DIR) -> set[str]:
    """Roster for the MEMBERSHIP layer only. Off-operator machines (CI, public
    clones — recognized by the rules dir itself being absent) SKIP with a
    reason: the explicit-id and discovery layers still enforce everywhere, so
    the skip never leaves the surface unguarded. On an operator machine
    (rules dir present) a missing roster falls through to _roster_models()'s
    FAIL LOUD assert — expected-but-missing never skips."""
    if not path.is_file() and not rules_dir.is_dir():
        pytest.skip(
            f"no operator roster at {path} (rules dir absent — non-operator "
            f"machine); membership layer skipped, explicit-id + discovery "
            f"layers still enforce")
    return _roster_models(path)


def test_roster_loader_fails_loud_when_missing(tmp_path):
    with pytest.raises(AssertionError, match="FAIL LOUD"):
        _roster_models(tmp_path / "absent.md")


def test_roster_loader_fails_loud_on_thin_table(tmp_path):
    """The rows >= 10 arm needs its own red proof: a table that parses but
    collapsed (format drift ate most rows) must fail, never pass empty-ish."""
    thin = tmp_path / "thin.md"
    thin.write_text("| `MGR_LANES` | `claude-opus-5` | manager lanes |\n")
    with pytest.raises(AssertionError, match="never pass empty"):
        _roster_models(thin)


def test_membership_gate_skips_off_operator(tmp_path):
    with pytest.raises(pytest.skip.Exception, match="non-operator"):
        _roster_for_membership(tmp_path / "absent.md", tmp_path / "no-rules-dir")


def test_membership_gate_fails_loud_when_roster_expected(tmp_path):
    """The expected-but-missing arm must FAIL, never skip. Deliberately NOT
    pytest.raises(AssertionError): pytest.skip.Exception derives from
    BaseException, so an escaping skip would mark this TEST skipped instead
    of failed — green-ish while the arm it guards is regressed (proven
    vacuous by the delta Tier-2). This shape fails on EVERY
    non-AssertionError outcome, including a skip."""
    rules = tmp_path / "rules"
    rules.mkdir()
    try:
        _roster_for_membership(rules / "sdd-model-tiers.md", rules)
    except AssertionError as e:
        assert "FAIL LOUD" in str(e)
    except BaseException as e:
        pytest.fail(
            f"gate raised {type(e).__name__} where it must raise "
            f"AssertionError (FAIL LOUD); a skip here means the "
            f"expected-but-missing arm regressed: {e!r}")
    else:
        pytest.fail("gate returned a roster where it must FAIL LOUD")


def test_prose_mirror_pins_explicit():
    """SHAPE layer for prose mirrors — runs everywhere (no roster needed).
    A bare alias written into shipped prose is exactly the deploy-lag defect
    the roster rule exists to kill; it must fail here, not wait for a human
    re-read of the deployed manager.md.

    Validates `--model`/`/model`-adjacent code-span tokens only (every such
    value, no allowlist); standalone model mentions in prose (e.g. "Escalate
    to `claude-opus-5`", "pinned `claude-opus-5[1m]` in code") are NOT
    machine-validated here — they remain under the roster rule's human
    mirror-sync discipline. The "pin outside a backtick code span" boundary
    is closed for `--model`/`/model`-adjacent text by the sibling
    test_prose_unanchored_model_occurrences_fail_closed (raw accounting has
    no span requirement, anchor-registered per file, union-iterated, and
    case-insensitive). The residual known boundaries after that: (a)
    standalone non-flag-adjacent model mentions (this test's own
    exclusion, above); (b) a raw occurrence swallowed inside a parsed span
    token (`` `--model claude-opus-5--model` ``), caught only by roster
    membership, which skips off-operator (review A13, pre-existing). No
    other known evasion survives the anchor-registry + union + case fixes
    (each closed hole cites its review id: A2/A3, A10/A11, F3, A15).

    Measured coverage magnitude (this session, real tree): 6 flag-adjacent
    pins are machine-validated by this net, while standalone model mentions
    outside it number 9 (backticked model-family ids) to 15 (all backticked
    `claude-*` tokens) depending on the counting rule — a MAJORITY of prose
    model mentions sit outside the machine net and remain under the roster
    rule's human mirror-sync discipline."""
    found = _prose_pin_values()
    assert found, ("no prose mirror pins extracted — manager.core.md #10 "
                   "carries several; extraction broke, never pass empty")
    for where, vals in found.items():
        for v in vals:
            _assert_explicit(v, f"prose mirror {where}", None)
    missing_floor = PROSE_PIN_FLOOR - set(found)
    assert not missing_floor, (
        f"floor files yielded NO prose pins: {sorted(missing_floor)} — their "
        f"prose was reworded away from `--model` code-span adjacency; "
        f"re-anchor the extraction or shrink PROSE_PIN_FLOOR deliberately")


def test_prose_unanchored_model_occurrences_fail_closed():
    problems = _prose_accounting_problems(
        _prose_pin_accounting(), PROSE_VALUELESS_MENTIONS)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("case,text,anchors,expect_clean", [
    # review A2: registered mention reworded INTO a degraded pin — count
    # conserved, anchor broken → must flag (both directions fire).
    ("degraded-at-registered-slot",
     "pass an explicit `--model` **opus** now\n",
     ["pass an explicit `--model` in `extra_args`"], False),
    # review A10/A11: file's only mention gone (and the checker must see
    # the file at all via the union) → leftover anchor must flag.
    ("vanished-mention", "no flags here at all\n",
     ["NOT by this spawn `--model`"], False),
    # review F3: case-gamed pin — raw counts it, span regex cannot parse
    # it, no anchor covers it → must flag.
    ("case-gamed-pin", "use `--MODEL claude-fake-9` here\n", [], False),
    # negative control: a real pin + a registered mention on separate
    # lines → clean.
    ("legit-mixed",
     "so omitting `--model` no longer yields the default\n"
     "pass `--model claude-opus-5[1m]` explicitly\n",
     ["omitting `--model` no longer yields the default"], True),
    # round-4: a smart-punctuation en-dash pin matched NEITHER regex and
    # vanished from the accounting entirely — normalization + original-vs-
    # normalized comparison makes any dash-variant flag visible and failing.
    ("en-dash-mangled-pin", "use `–model claude-fake-9` here\n", [], False),
])
def test_prose_accounting_checker_fails_closed(case, text, anchors, expect_clean):
    """The holes found by breaking, pinned as permanent in-suite cases —
    scratch-proof once, regression-test forever."""
    parsed, unmatched = _account_text(text)
    accounting = {"f": (parsed, unmatched)} if (parsed or unmatched) else {}
    problems = _prose_accounting_problems(
        accounting, {"f": anchors} if anchors else {})
    assert (not problems) == expect_clean, (case, problems)


# --- executed lanes -------------------------------------------------------

def _bootstrap_model(tmp_path, with_settings: bool) -> str:
    """Drive the REAL script with --dry-run (prints the resolved RUNTIME_CMD
    verbatim as cmd=[…]) and parse the --model value out of it. Fake-tmux
    PATH prefix keeps it off any real socket."""
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(parents=True)
    (fakebin / "tmux").write_text(
        "#!/bin/bash\ncase \"$*\" in *has-session*) exit 1 ;; esac\nexit 0\n")
    (fakebin / "tmux").chmod(0o755)
    (fakebin / "jq").symlink_to(shutil.which("jq"))
    (fakebin / "uuidgen").symlink_to(shutil.which("uuidgen"))
    home = tmp_path / "home"
    active = home / ".claude" / "dockwright" / "active"
    active.mkdir(parents=True)
    (active / "sid-x.json").write_text(json.dumps(
        {"claude_sid": "sid-x", "agent": "manager", "name": "mighty-demon",
         "domain": "personal", "pid": 4242}))
    if with_settings:
        presets = home / ".claude" / "dockwright" / "presets"
        presets.mkdir(parents=True)
        (presets / "manager-settings.json").write_text("{}")
    env = {**os.environ, "HOME": str(home),
           "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}"}
    env.pop("DOCKWRIGHT_MANAGER_RC", None)
    env.pop("DOCKWRIGHT_MANAGER_SKIP_PERMS", None)
    r = subprocess.run(
        ["bash", str(BOOTSTRAP), "--narrative", "probe", "--from-sid",
         "sid-x", "--dry-run"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    cmd = next(l for l in r.stdout.splitlines() if "cmd=[" in l)
    if with_settings:
        assert "--settings" in cmd, "settings branch not taken — harness bug"
    else:
        assert "--settings" not in cmd, "no-settings branch not taken — harness bug"
    m = re.search(r"--model '([^']+)'", cmd)
    assert m, f"no quoted --model value in dry-run cmd: {cmd}"
    return m.group(1)


@pytest.mark.parametrize("with_settings", [True, False],
                         ids=["settings-branch", "no-settings-branch"])
def test_bootstrap_recreate_lane(tmp_path, with_settings):
    model = _bootstrap_model(tmp_path, with_settings)
    _assert_explicit(model, "bootstrap-recreate.sh RUNTIME_CMD", None)


def test_stale_monitor_recovery_lane(monkeypatch, tmp_path):
    """Same loader discipline as test_skip_perms_lane_parity: exec the real
    module under a scratch HOME, capture the spawn argv via a fake driver."""
    import importlib.util
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "stale_monitor_pin_guard", STALE_MONITOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "_account_config_prefix", lambda letter: "")
    captured = {}

    class FakeDrv:
        async def spawn(self, **kw):
            captured.update(kw)
            return "%9"

    monkeypatch.setattr(mod, "_get_driver", lambda: FakeDrv())
    mod._launch_recovery_manager({"cwd": "/c", "name": "m"}, "sid-1", "a")
    inner = captured["argv"][-1]
    toks = shlex.split(inner)
    assert "--model" in toks, f"no --model in recovery inner cmd: {inner}"
    model = toks[toks.index("--model") + 1]
    _assert_explicit(model, "stale_monitor._launch_recovery_manager", None)


def test_manager_launch_default_lane(monkeypatch, tmp_path):
    """Fresh-boot lane; spawn_replacement_manager shares the same
    config.manager_model() resolution (a HARDCODE at mcp_server.py's call
    site is caught by test_mcp_tools' exact-equality extra_args assertions —
    test_account_validation's two-literal denylist only guards the known
    spellings)."""
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(tmp_path / "absent.toml"))
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "no-presets")
    argv = manager_launch._runtime_argv()
    model = argv[argv.index("--model") + 1]
    _assert_explicit(model, "manager_launch._runtime_argv (DEFAULT_MANAGER_MODEL)",
                     None)


def test_spawner_worker_fallback_lane(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(tmp_path / "absent.toml"))
    cmd = spawner._runtime_command("claude", "hi", None, None)
    toks = shlex.split(cmd)
    model = toks[toks.index("--model") + 1]
    _assert_explicit(model, "spawner._runtime_command (DEFAULT_WORKER_MODEL)",
                     None)


def test_default_toml_template_lane():
    """dockwright init writes DEFAULT_TOML verbatim — a bare alias here
    resurrects on every fresh install as a toml override."""
    spawn = tomllib.loads(config.DEFAULT_TOML)["spawn"]
    _assert_explicit(spawn["worker_model"], "DEFAULT_TOML worker_model", None)
    _assert_explicit(spawn["manager_model"], "DEFAULT_TOML manager_model", None)
    assert spawn["distill_model"] == DISTILL_GRANDFATHER, (
        "DEFAULT_TOML distill_model moved off the grandfathered "
        f"{DISTILL_GRANDFATHER} — pick a roster model (or add a roster row) "
        "and update this guard deliberately")
    _assert_explicit(spawn["distill_model"], "DEFAULT_TOML distill_model", None)


def test_distill_default_lane(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(tmp_path / "absent.toml"))
    model = config.distill_model()
    assert model == DISTILL_GRANDFATHER, (
        f"distill default moved to {model!r} — grandfather is exact; revisit "
        f"the roster and this guard together")
    _assert_explicit(model, "config.distill_model()", None)


def test_distill_spawn_lane(monkeypatch, tmp_path):
    """Drive the ACT, not the declaration: the grandfathered pin must hold in
    the composed `claude -p` argv at distill's real call site — a bare-alias
    hardcode there would keep the sweep count stable and every declaration
    check green (the vacuous pass drift-guard-tests.md forbids)."""
    from dockwright import distill
    log = tmp_path / "t.jsonl"
    # Needs a real assistant turn: distill skips transcripts whose model never
    # ran, so a placeholder event would short-circuit before argv is composed.
    log.write_text(
        '{"type": "assistant", "message": {"content": '
        '[{"type": "text", "text": "ok"}]}}\n'
    )
    monkeypatch.setattr(distill, "find_session_log", lambda sid: log)
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(tmp_path / "absent.toml"))
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout=b"distilled", stderr=b"")

    monkeypatch.setattr(distill.subprocess, "run", fake_run)
    distill._distill_manager_session("sid-pin-guard")
    argv = captured["argv"]
    assert "--model" in argv, f"no --model in distill argv: {argv}"
    model = argv[argv.index("--model") + 1]
    assert model == DISTILL_GRANDFATHER, (
        f"distill spawns --model {model!r}; grandfather is exact "
        f"({DISTILL_GRANDFATHER}) — revisit the roster and this guard together")
    _assert_explicit(model, "distill._distill_manager_session argv", None)


SCRIPT_MODEL_RE = re.compile(r"--model[= ]+('[^']+'|\"[^\"]+\"|[^\s\\]+)")


def _script_pin_values(path: Path) -> list[str]:
    """--model values on executed (non-comment) lines of a shell script."""
    values = []
    for line in path.read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        for m in SCRIPT_MODEL_RE.finditer(line):
            values.append(m.group(1).strip("'\""))
    return values


@pytest.mark.parametrize("script", ["selffix-run.sh", "gardener-run.sh"])
def test_headless_script_pins(script):
    values = _script_pin_values(SCRIPTS / script)
    assert values, f"{script}: no --model pin found on an executed line"
    for v in values:
        _assert_explicit(v, script, None)


def test_headless_scan_model_flags_is_a_flag_set_not_a_pin():
    """`headless_scan.py` carries `--model` on an executed line without pinning a
    model, so it is registered above with this check rather than exempted.

    The assertion is that it stays that way: `_MODEL_FLAGS` holds FLAG NAMES, and
    no model ID may appear on any executed line of that file. A pin smuggled in
    beside a legitimately-registered `--model` occurrence is exactly what this
    registry exists to prevent."""
    spec = importlib.util.spec_from_file_location(
        "headless_scan", SCRIPTS / "headless_scan.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._MODEL_FLAGS == frozenset(
        {"--model", "-m", "--fallback-model", "--small-model"})
    executed = "\n".join(_py_executed_lines(SCRIPTS / "headless_scan.py"))
    assert not re.search(r"claude-(opus|sonnet|haiku|fable|mythos)-\d", executed), (
        "headless_scan.py must not pin a model on an executed line")


def test_all_pins_roster_membership(monkeypatch, tmp_path):
    """MEMBERSHIP layer: every executed pin's id appears in the MODEL ROSTER
    table. Runs only where a roster exists (operator machines) — see
    _roster_for_membership; adopting a new model = edit the roster rows, run
    the suite, and this test names every site that still has to move.

    The stale_monitor lane re-execs the module under its OWN scratch HOME
    (same loader discipline as test_stale_monitor_recovery_lane) so its
    membership coverage does not depend on that shape test having already
    run — a lane no other test in this file checks against the roster."""
    roster = _roster_for_membership()
    values = {}
    values["bootstrap[settings]"] = _bootstrap_model(tmp_path / "b1", True)
    values["bootstrap[no-settings]"] = _bootstrap_model(tmp_path / "b2", False)
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(tmp_path / "absent.toml"))
    monkeypatch.setattr(manager_launch.paths, "PRESETS", tmp_path / "no-presets")
    argv = manager_launch._runtime_argv()
    values["manager_launch"] = argv[argv.index("--model") + 1]
    cmd = spawner._runtime_command("claude", "hi", None, None)
    toks = shlex.split(cmd)
    values["spawner"] = toks[toks.index("--model") + 1]
    spawn = tomllib.loads(config.DEFAULT_TOML)["spawn"]
    values["toml.worker_model"] = spawn["worker_model"]
    values["toml.manager_model"] = spawn["manager_model"]
    for script in ("selffix-run.sh", "gardener-run.sh"):
        for i, v in enumerate(_script_pin_values(SCRIPTS / script)):
            values[f"{script}[{i}]"] = v
    for where, vals in _prose_pin_values().items():
        for i, v in enumerate(vals):
            values[f"prose:{where}[{i}]"] = v
    import importlib.util
    monkeypatch.setenv("HOME", str(tmp_path / "sm"))
    spec = importlib.util.spec_from_file_location("sm_pin_membership", STALE_MONITOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ROOT", tmp_path / "sm")
    monkeypatch.setattr(mod, "_account_config_prefix", lambda letter: "")
    cap = {}

    class FakeDrv2:
        async def spawn(self, **kw):
            cap.update(kw)
            return "%9"

    monkeypatch.setattr(mod, "_get_driver", lambda: FakeDrv2())
    mod._launch_recovery_manager({"cwd": "/c", "name": "m"}, "sid-1", "a")
    sm_toks = shlex.split(cap["argv"][-1])
    values["stale_monitor"] = sm_toks[sm_toks.index("--model") + 1]
    missing = {where: v for where, v in values.items()
               if _strip_1m(v) not in roster}
    assert not missing, (
        f"pins not in the MODEL ROSTER table ({ROSTER_PATH}): {missing} — "
        f"either the roster moved on and these sites were missed, or a new "
        f"pin needs a roster decision first")


# --- site discovery -------------------------------------------------------

def _py_executed_lines(path: Path) -> list[str]:
    """Source lines with comments removed and docstring-position string
    constants blanked. F-strings and ordinary string ARGUMENTS survive —
    stale_monitor's inner-command f-string is a real pin and must count.
    The final textual `#`-prefix filter drops comment-SHAPED lines living
    inside string literals (config.py's DEFAULT_TOML template comments quote
    `--model` as TOML prose; tokenize sees string content, not COMMENT
    tokens) — a real pin cannot start with `#` on any surface we scan."""
    src = path.read_text()
    lines = src.splitlines()
    doc_lines: set[int] = set()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                              ast.AsyncFunctionDef))
                and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            doc_lines.update(range(body[0].lineno, body[0].end_lineno + 1))
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            row, col = tok.start
            lines[row - 1] = lines[row - 1][:col]
    return [l for i, l in enumerate(lines, 1)
            if i not in doc_lines and not l.lstrip().startswith("#")]


def _sh_executed_lines(path: Path) -> list[str]:
    """Stated boundary: reads as UTF-8 with errors="replace", so a non-UTF-8
    file (e.g. UTF-16) carrying a --model pin would silently evade this
    sweep instead of failing loud. All 74 files currently swept are
    ASCII/UTF-8."""
    return [l for l in path.read_text(errors="replace").splitlines()
            if not l.lstrip().startswith("#")]


def test_no_unregistered_pin_sites():
    """A NEW --model pin anywhere in the shipping SURFACE must fail here
    until it is wired into an executed-lane check above and registered in
    EXPECTED_PIN_SITES. Silent skips are how drift survives.

    Scope: src/dockwright/**, deploy/** (minus the compose-prose agents/
    surface and .md mirrors), and setup.sh — the shipping payload roots per
    the repo layout contract (CLAUDE.md: "deploy/ — everything setup.sh
    copies into ~/.claude"). A file shipped from OUTSIDE these roots would
    evade the sweep — the repo layout contract forbids that, and there is no
    tractable parse of setup.sh's copy commands that would be less fragile
    than the contract.

    Exclusions:
    - any path containing __pycache__ (binary caches, gitignored);
    - *.md anywhere: excluded from THIS executed registry only — covered by
      the prose-mirror layer (_prose_pin_values / test_prose_mirror_pins_
      explicit), not because prose is unguarded;
    - deploy/agents/** entirely: the compose-prose surface. manager.core.md
      carries literal --model MIRROR pins in prose and vars.defaults.toml:71
      carries --model inside a prose agent-var string; both render into
      deployed agent PROSE, not a shell-executed spawn path. Excluded from
      the executed registry because rendered prose is not an executed spawn
      surface — not because nothing under deploy/agents/ is executable, and
      not because the pins go unguarded: they are validated directly by the
      prose-mirror layer (shape everywhere via test_prose_mirror_pins_
      explicit; roster membership wherever an operator roster exists, via
      the `prose:<path>[i]` entries folded into
      test_all_pins_roster_membership).

    Stated boundary: a pin assembled by string concatenation that never
    spells the literal `--model` is outside this sweep's reach — the
    executed-lane checks above are the net for the known lanes; a wholly
    new concat-built lane needs a human eye at review time."""
    found: dict[str, int] = {}
    candidates = (sorted(SRC.rglob("*")) + sorted(DEPLOY.rglob("*"))
                  + [SETUP_SH])
    for path in candidates:
        if not path.is_file():
            continue
        if "__pycache__" in path.parts:
            continue
        if path.suffix == ".md":
            continue
        if AGENTS_DIR in path.parents:
            continue
        lines = (_py_executed_lines(path) if path.suffix == ".py"
                 else _sh_executed_lines(path))
        n = sum(l.count("--model") for l in lines)
        if n:
            found[str(path.relative_to(REPO))] = n
    assert found == EXPECTED_PIN_SITES, (
        "the set of --model pin sites changed. New/moved sites must get an "
        "executed-lane check in this file AND a row in EXPECTED_PIN_SITES; "
        f"removed sites shrink both.\nfound:    {found}\n"
        f"expected: {EXPECTED_PIN_SITES}")
