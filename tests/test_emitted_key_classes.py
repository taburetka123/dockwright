"""A failed delivery must un-record PAGES without un-recording ACTS.

`stale_monitor`'s `next_emitted` is two ledgers in one dict. Discarding it
wholesale on `LaneDead` was correct for the half that records "this line was
shown" and wrong for the half that records "this was DONE to the world" — a
nudge already typed into a worker's pane, a recovery session already launched,
the autoclose gate already advanced. Measured: worker nudged, reader dies, lane
re-armed, `resume your task` typed into the pane twice.

The classification is a hand-maintained prefix list, which
`~/.claude/rules/drift-guard-tests.md` § ADD-ONE calls unguarded by
construction: the next key someone adds joins neither class and silently takes
the page path. So the guard here PARSES the module for every key literal and
fails on one that is not classified — derived from the thing it guards rather
than from a second list of what I remember writing.
"""
import ast
import json
from pathlib import Path

import pytest

from dockwright import stale_monitor

SOURCE = Path(stale_monitor.__file__).read_text(encoding="utf-8")


def _key_literals():
    """Every `<prefix>:` literal that is USED as an emitted-state key.

    ⚠️ DELIBERATELY NOT COVERED, so the next reader does not re-open it as an
    oversight: a key held in a variable NOT named `*_key`. Accepting any
    variable used as a ledger subscript would mean reporting "cannot classify"
    on real keys whose value is dynamic — a guard that cries wolf on correct
    code, which trains the reader to shrug. That is the coincidence detector
    running in reverse, and it would be worse than the gap it closes. Breaking
    the `*_key` convention shows up in a diff; the colon gate did not, which is
    why that one was worth closing and this one is not.

    Derived from use, not from shape. A first attempt collected every
    colon-bearing f-string in the module and swept up prose
    (`f"stale_monitor: notify failed…"`) — a guard that fails on documentation
    gets silenced, which is worse than no guard. So: any variable whose name
    ends in `key` and is assigned a string, plus any literal subscript of the
    two ledger dicts.
    """
    found = set()
    tree = ast.parse(SOURCE)

    def _prefix(value):
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
            # `"nudge_sent:" + sid` — a key built by concatenation. Four lines
            # and no false positives, so it is worth folding in.
            return _prefix(value.left)
        if isinstance(value, ast.JoinedStr):
            first = value.values[0] if value.values else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                return first.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(n == "key" or n.endswith("_key") for n in names):
                text = _prefix(node.value)
                if text:
                    found.add(text.split(":")[0] + ":" if ":" in text else text)
        if (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id in ("next_emitted", "emitted")):
            text = _prefix(node.slice)
            if text:
                found.add(text.split(":")[0] + ":" if ":" in text else text)
    return found


def _classified(key: str) -> bool:
    """Exact names count as well as prefixes.

    Every `ACTION_KEY_EXACT` entry is colon-less, so a prefix-only check said
    "unclassified" for the four keys that ARE classified — and the earlier
    `":" in text` gate hid that by never showing them to it. A colon-less key
    added later took the page path in silence, which is the whole defect this
    file exists to stop, occurring inside the file that stops it.
    """
    return (key in stale_monitor.ACTION_KEY_EXACT
            or key in stale_monitor.PAGE_KEY_EXACT
            or key in stale_monitor.ACTION_KEY_PREFIXES
            or key in stale_monitor.PAGE_KEY_PREFIXES)


def test_every_key_prefix_in_the_module_is_classified():
    unclassified = sorted(p for p in _key_literals() if not _classified(p))
    assert unclassified == [], (
        f"these emitted-state key prefixes belong to neither class: "
        f"{unclassified}. An unclassified key takes the PAGE path, so if it "
        f"records an ACT the act repeats after any failed delivery. Add it to "
        f"ACTION_KEY_PREFIXES or PAGE_KEY_PREFIXES in stale_monitor.py.")


def test_the_parser_sees_colon_less_keys_too():
    """The shape that escaped: `next_emitted["last_autoclose_run"] = now`."""
    found = _key_literals()
    for expected in stale_monitor.ACTION_KEY_EXACT:
        assert expected in found, (
            f"the parser cannot see the colon-less key {expected!r}, so a new "
            f"one would join neither class and silently take the page path")


def test_the_parser_actually_finds_the_known_keys():
    """A guard over an empty set passes vacuously."""
    found = _key_literals()
    for expected in ("nudge_sent:", "question:", "processing:", "lane_silent:"):
        assert expected in found, (
            f"the key parser missed {expected!r}; it is checking nothing")


def test_the_two_classes_do_not_overlap():
    overlap = set(stale_monitor.ACTION_KEY_PREFIXES) & set(
        stale_monitor.PAGE_KEY_PREFIXES)
    assert overlap == set(), overlap


@pytest.mark.parametrize("key,is_action", [
    ("nudge_sent:abc", True),
    ("nudged:abc:123", True),
    ("scheduled:abc", True),
    ("recovery:abc", True),
    ("auth-recovery:abc", True),
    ("last_autoclose_run", True),
    ("codex_log_cache", True),
    ("processing:abc:1", False),
    ("question:abc", False),
    ("orphan:%1", False),
    ("approval:a:b", False),
    ("auth-emit:abc", False),
    ("lane_silent:done", False),
])
def test_the_classifier_puts_each_known_key_on_the_right_side(key, is_action):
    assert stale_monitor._is_action_key(key) is is_action


def test_a_failed_delivery_keeps_the_acts_and_drops_the_pages(tmp_path):
    """The property, end to end, on the real helper."""
    state = tmp_path / ".stale-emitted-mgr.json"
    emitted = {"question:old": 4}
    next_emitted = {
        "nudge_sent:w1": 1234.0,        # the nudge was TYPED — must survive
        "last_autoclose_run": 99.0,     # the gate advanced — must survive
        "processing:w1:9": 30,          # a page nobody saw — must be dropped
        "lane_silent:done": {"at": 1.0, "level": 1},
    }
    stale_monitor._commit_actions_only(state, emitted, next_emitted)

    written = json.loads(state.read_text())
    assert written["nudge_sent:w1"] == 1234.0, "a performed nudge was un-recorded"
    assert written["last_autoclose_run"] == 99.0
    assert "processing:w1:9" not in written, (
        "a page that never reached the manager was recorded as shown")
    assert "lane_silent:done" not in written
    assert written["question:old"] == 4, "prior state was discarded"


def test_an_unclassified_key_takes_the_page_path(tmp_path):
    """Documents the failure direction, so the guard above earns its keep: an
    unclassified key is DROPPED, which is safe for a page and repeats an act."""
    state = tmp_path / ".stale-emitted-mgr.json"
    stale_monitor._commit_actions_only(state, {}, {"brand-new-key": 1})
    assert "brand-new-key" not in json.loads(state.read_text())


# --------------------------------------------------------------------------
# Write-ahead. An exception handler covers exceptions; a HARD kill runs no
# handler at all, and the window between typing a nudge into a worker's pane
# and reaching the end-of-scan ledger write is most of a scan's runtime.
# --------------------------------------------------------------------------

def _killed_mid_scan(tmp_path, mode):
    """Record a nudge either BEFORE or AFTER the act, then SIGKILL.

    Returns what the NEXT scan would find on disk. Uses a real subprocess and
    a real SIGKILL: the whole point is that no `finally`, no `except`, and no
    atexit hook runs, so simulating it in-process would prove nothing.
    """
    import subprocess
    import sys as _sys

    state = tmp_path / f".stale-emitted-{mode}.json"
    src = Path(stale_monitor.__file__).resolve().parents[1]
    script = (
        "import os, signal, sys\n"
        f"sys.path.insert(0, {str(src)!r})\n"
        "from pathlib import Path\n"
        "from dockwright import stale_monitor as sm\n"
        f"sm.ROOT = Path({str(tmp_path)!r})\n"
        f"state = Path({str(state)!r})\n"
        "emitted, next_emitted = {}, {}\n"
        + ("sm._record_action_ahead(state, emitted, next_emitted,"
           " 'nudge_sent:w1', 1234.0)\n"
           if mode == "ahead" else
           "next_emitted['nudge_sent:w1'] = 1234.0\n")
        + "os.kill(os.getpid(), signal.SIGKILL)\n")
    result = subprocess.run([_sys.executable, "-c", script], capture_output=True)
    assert result.returncode == -9, "the child was not hard-killed"
    if not state.exists():
        return None
    return json.loads(state.read_text()).get("nudge_sent:w1")


def test_recording_after_the_act_loses_it_to_a_hard_kill(tmp_path):
    """The control. Without this the test below proves nothing — it would pass
    just as well if SIGKILL never interrupted anything."""
    assert _killed_mid_scan(tmp_path, "after") is None


def test_write_ahead_survives_a_hard_kill(tmp_path):
    """The property: a crash leaves a recorded nudge that may not have
    happened, rather than an unrecorded one that did. One missed nudge is
    re-fired by the ladder on its own schedule; a duplicate lands a second
    `resume your task` in a live worker's pane."""
    assert _killed_mid_scan(tmp_path, "ahead") == 1234.0


# ⚠️ TWO GUARDS, DELIBERATELY. NEITHER CHECKS EFFECTIVENESS.
#
#   * the PRESENCE check — a `_record_action_ahead` exists ahead of each send;
#   * the ABSENCE check — no bare `next_emitted[...] =` sits in a send's own
#     block, i.e. no ledger write at a site uses the unsafe shape.
#
# Neither says whether the persisted VALUE is one the next scan reads as
# "already done" — measured 2026-08-06 at `ebb5fbe`, only ONE of the four sites
# passes that. Re-derive that number, do not inherit it: it rots upward the
# moment the follow-up fixes a site, which is the safe direction, but it is
# prose and nothing pins it. Make no effectiveness claim IN AN ASSERTION — two
# have now been made ("four", then "three") and both were wrong. A number that
# has visibly been audited is believed harder than one that never was, so a
# smaller wrong claim is worse than none.
#
# They are complementary, not redundant, and shipping only one is a hole:
# absence alone passes a site with NO record at all, and presence alone was
# measured insufficient (one write-ahead standing in for another). An earlier
# revision of THIS PR deleted the presence check as "superseded" — it was not;
# insufficient is not unnecessary, and deleting it left two sites with nothing
# anywhere requiring their record to exist.
#
# ⚠️ SHAPE LIMIT, and it belongs to the ABSENCE check ALONE. That one sees a
# send only as `Expr -> Call -> Name` (its `_has_send`), so a `getattr`
# dispatch is invisible to it. The PRESENCE check walks every `ast.Call`, so
# `_ok = _send_text(...)` and a comprehension DO red it — measured, both.
# Disclosed rather than widened: `_has_send` is byte-identical to merged main,
# and widening it here would repeat the over-reach this PR is correcting.
#
# Useful consequence, said out loud so the disagreement is not discovered:
# `sends == 4` is pinned in BOTH tests by two different mechanisms, so a
# legitimate non-`Expr` send reds one pin and not the other. That asymmetry is
# a feature — it points at which guard needs the update.
#
# What is OWED is a BEHAVIOURAL per-site check: persist the write-ahead value,
# re-enter the branch with a fresh `now`, assert no second `_send_text`. That
# validates the act rather than the declaration, and it is the only shape that
# can carry an effectiveness claim. Scoped in the cursor follow-up.


def test_every_keystroke_injection_is_preceded_by_its_record():
    """PRESENCE: each keystroke site has a `_record_action_ahead` before it.

    Restored after being deleted as "superseded" — it is not. The absence
    check below passes a site with no record whatsoever, so without this one a
    refactor that drops the `_record_action_ahead` call leaves the suite green
    and the nudge unrecorded.

    AST-anchored rather than counting `"_send_text("` in the source text: the
    textual version breaks the moment any docstring mentions the call, which it
    did — matching a spelling instead of a property, in the file whose subject
    is that mistake.
    """
    import ast
    import pathlib

    source = pathlib.Path(stale_monitor.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    lines = source.splitlines()

    sends = [n.lineno for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_send_text"]
    assert len(sends) == 4, (
        f"expected 4 keystroke-injection sites, found {len(sends)} at lines "
        f"{sorted(sends)} — a new one must be write-ahead too, so update this "
        f"pin only after checking it")
    offenders = [ln for ln in sends
                 if "_record_action_ahead" not in "\n".join(
                     lines[max(0, ln - 7):ln - 1])]
    assert offenders == [], (
        "these keystroke injections are not preceded by a durable record, so "
        f"a hard kill mid-scan makes the retry type into the pane again: "
        f"lines {offenders}")

    # One site records TWO keys, so "a record exists nearby" cannot see one of
    # the pair being deleted — measured: the presence check above and the
    # absence check below were BOTH green for that mutation. Pinning the total
    # closes it as an ADD-ONE tripwire. It is a count, not a per-key property;
    # the per-key version is the owed behavioural check.
    records = [n.lineno for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "_record_action_ahead"]
    assert len(records) == 5, (
        f"expected 5 _record_action_ahead calls (one per site, two at the "
        f"site that records two keys), found {len(records)} at {sorted(records)}. "
        f"If a site legitimately gained or lost a key, update this pin — but "
        f"check first that no site lost a record it still needs.")

    # The total alone is blind to a SWAP: drop a record at a keystroke site,
    # add one anywhere else, total still 5. Measured — and it stops being
    # academic the moment the owed follow-up gives the manager site a second
    # key, which is exactly what it is scoped to do. Source order, not a
    # sorted multiset: a multiset stays [1, 1, 1, 2] under the swap.
    per_site = [sum(1 for r in records if ln - 6 <= r <= ln - 1)
                for ln in sorted(sends)]
    assert per_site == [1, 1, 1, 2], (
        f"records per keystroke site, in source order, is {per_site} not "
        f"[1, 1, 1, 2] — a site lost or gained a write-ahead record. Moving "
        f"one between sites keeps the TOTAL at 5, which is why this is "
        f"per-site.")


def test_no_action_is_recorded_bare_at_a_keystroke_site():
    """Every ledger write in a keystroke site's own block is write-AHEAD.

    Placement only — see the note above for what this deliberately does not
    assert. Scoped to a send's OWN block: a bare write in a sibling or parent
    branch is a different statement sequence and not this check's business.
    """
    import ast
    import pathlib

    source = pathlib.Path(stale_monitor.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    def _has_send(stmt):
        """The statement IS the send, not merely an ancestor of one.

        Walking the whole subtree counts the same call once per enclosing
        `if`, which put the site count at 24 instead of 4 — and the cheapest
        way out of that red is to update the pin to 24, which is exactly the
        bug this note exists to prevent. Match the statement, not the subtree.
        """
        return (isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Name)
                and stmt.value.func.id == "_send_text")

    def _is_bare_ledger_write(stmt):
        return (isinstance(stmt, ast.Assign)
                and any(isinstance(tgt, ast.Subscript)
                        and isinstance(tgt.value, ast.Name)
                        and tgt.value.id == "next_emitted"
                        for tgt in stmt.targets))

    sends, offenders = 0, []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            if not any(_has_send(s) for s in block):
                continue
            sends += sum(1 for s in block if _has_send(s))
            for stmt in block:
                if _is_bare_ledger_write(stmt):
                    offenders.append(f"line {stmt.lineno}")

    assert sends == 4, (
        f"expected 4 keystroke-injection sites, found {sends} — a new one must "
        f"be write-ahead too, so update this pin only after checking it")
    assert offenders == [], (
        "these ledger writes sit in the SAME block as a keystroke injection "
        "without going through _record_action_ahead, so a hard kill between "
        f"the act and the end-of-scan write makes the retry type again: "
        f"{offenders}")


def test_the_bare_shape_is_what_the_check_looks_for():
    """ADD-ONE on the guard itself: prove the pattern matches the unsafe form
    and not the safe one."""
    import re
    bare = re.compile(r"^\s*next_emitted\[[^\]]+\]\s*=")
    assert bare.match("        next_emitted[nudge_sent_key] = now")
    assert not bare.match(
        "        _record_action_ahead(state, emitted, next_emitted, k, now)")


