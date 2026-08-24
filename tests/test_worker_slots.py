"""Worker-slot semaphore: the cap, its resolution order, and its guards.

Split out of test_mcp_tools.py on 2026-08-03. The collision guard below
enumerates EVERY integer literal in the files that hold slot tests, and while
those tests lived in a 6,000-line general-purpose module that meant any
literal anywhere in it constrained the slot default — six unrelated `5`s had
to move, one an iTerm window id that dragged two assertions with it. The
guard is only worth its friction when its scope is the thing it guards.
"""
import ast
import json
import os
import threading
from pathlib import Path

import pytest

from dockwright import paths, state
from dockwright.mcp_server import (
    DEFAULT_SLOT_COUNTS,
    acquire_worker_slot_impl,
    register_self_impl,
    release_worker_slot_impl,
)


@pytest.fixture
def fresh_orchestrator_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "ACTIVE", tmp_path / "active")
    monkeypatch.setattr(paths, "QUESTIONS", tmp_path / "questions")
    monkeypatch.setattr(paths, "ANSWERS", tmp_path / "answers")
    monkeypatch.setattr(paths, "DONE", tmp_path / "done")
    monkeypatch.setattr(paths, "CLOSED", tmp_path / "closed")
    monkeypatch.setattr(paths, "SLOTS", tmp_path / "slots")
    monkeypatch.setattr(paths, "ASSIGNMENTS", tmp_path / "assignments")
    monkeypatch.setattr(paths, "ASSIGNMENTS_PENDING", tmp_path / "assignments" / ".pending")
    paths.ensure_dirs()
    yield tmp_path


# --- Worker-slot semaphore -------------------------------------------------

def _register_worker(sid: str, name: str = "w", pid: int | None = None) -> None:
    """Helper: register an active worker so acquire's liveness check passes."""
    register_self_impl(
        claude_sid=sid,
        agent="worker",
        name=name,
        cwd="/tmp",
        iterm_sid="i",
        pid=pid if pid is not None else os.getpid(),
    )


def test_acquire_worker_slot_succeeds_under_cap(fresh_orchestrator_dir):
    _register_worker("sid-A", name="A")
    _register_worker("sid-B", name="B")
    r1 = acquire_worker_slot_impl(claude_sid="sid-A", category="mvn", max_concurrent=3)
    r2 = acquire_worker_slot_impl(claude_sid="sid-B", category="mvn", max_concurrent=3)
    assert "slot_id" in r1 and "slot_id" in r2
    assert r1["slot_id"] != r2["slot_id"]


def test_acquire_worker_slot_blocks_at_cap(fresh_orchestrator_dir):
    for n in ("A", "B", "C"):
        _register_worker(f"sid-{n}", name=n)
        acquire_worker_slot_impl(claude_sid=f"sid-{n}", category="mvn", max_concurrent=3)
    _register_worker("sid-D", name="D")
    with pytest.raises(TimeoutError):
        acquire_worker_slot_impl(
            claude_sid="sid-D", category="mvn", max_concurrent=3, timeout_sec=1
        )


def test_release_worker_slot_frees_one(fresh_orchestrator_dir):
    slot_ids = []
    for n in ("A", "B", "C"):
        _register_worker(f"sid-{n}", name=n)
        slot_ids.append(
            acquire_worker_slot_impl(
                claude_sid=f"sid-{n}", category="mvn", max_concurrent=3
            )["slot_id"]
        )
    release_worker_slot_impl(slot_id=slot_ids[1])
    _register_worker("sid-D", name="D")
    result = acquire_worker_slot_impl(
        claude_sid="sid-D", category="mvn", max_concurrent=3, timeout_sec=2
    )
    assert "slot_id" in result


def test_release_worker_slot_idempotent(fresh_orchestrator_dir):
    _register_worker("sid-A", name="A")
    slot = acquire_worker_slot_impl(claude_sid="sid-A", category="mvn", max_concurrent=3)
    r1 = release_worker_slot_impl(slot_id=slot["slot_id"])
    r2 = release_worker_slot_impl(slot_id=slot["slot_id"])
    assert r1["released"] is True
    assert r2["released"] is True
    # The flags above are the RETURN SHAPE; a no-op release returning
    # {"released": True} satisfies them. This is the release itself.
    assert "sid-A" not in (paths.SLOTS / "mvn.json").read_text()


def test_acquire_evicts_stale_holders(fresh_orchestrator_dir):
    import json
    # Pre-seed a slot file with a holder whose claude_sid has no active record
    # AND whose pid is dead. acquire should evict it and grant.
    (paths.SLOTS).mkdir(parents=True, exist_ok=True)
    (paths.SLOTS / "mvn.json").write_text(json.dumps({
        "max_concurrent": 1,
        "holders": [{
            "slot_id": "stale-1",
            "claude_sid": "ghost-sid",
            "acquired_at": 0.0,
            "pid": 999999,  # almost certainly dead
        }],
    }))
    _register_worker("sid-A", name="A")
    result = acquire_worker_slot_impl(
        claude_sid="sid-A", category="mvn", max_concurrent=1, timeout_sec=2
    )
    assert "slot_id" in result and result["slot_id"] != "stale-1"


def test_env_var_overrides_default_count(fresh_orchestrator_dir, monkeypatch):
    # The env value must DIFFER from DEFAULT_SLOT_COUNTS["mvn"], or this test
    # stops discriminating: with the two equal, an implementation that ignored
    # the env entirely would fall through to the same cap and still pass. It
    # read "5" while the default was 3; when the default became 5 (2026-08-03)
    # that collision would have made it vacuous, so it reads 2 now. This pins
    # the OVERRIDE PATH, never the default's value — do not "sync" it back.
    monkeypatch.setenv("CLAUDE_ORCH_SLOTS_MVN", "2")
    # Acquire 2 with max_concurrent omitted; the env var should set the cap.
    for n in range(2):
        _register_worker(f"sid-{n}", name=f"W{n}")
        acquire_worker_slot_impl(claude_sid=f"sid-{n}", category="mvn")
    _register_worker("sid-X", name="X")
    with pytest.raises(TimeoutError):
        acquire_worker_slot_impl(claude_sid="sid-X", category="mvn", timeout_sec=1)


def test_concurrent_acquires_serialize_safely(fresh_orchestrator_dir):
    import threading
    _register_worker("sid-A", name="A")
    _register_worker("sid-B", name="B")
    results: list = []
    errors: list = []

    def grab(sid):
        try:
            results.append(
                acquire_worker_slot_impl(
                    claude_sid=sid, category="mvn", max_concurrent=2, timeout_sec=6
                )
            )
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=grab, args=("sid-A",))
    t2 = threading.Thread(target=grab, args=("sid-B",))
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert not errors
    assert len(results) == 2
    ids = {r["slot_id"] for r in results}
    assert len(ids) == 2

# --- The default slot cap: path, docs, and the collision class -------------
#
# PR #259's Tier-2 measured that setting DEFAULT_SLOT_COUNTS["mvn"] 5 -> 7 left
# the full suite green (fine — a value pin catches every legitimate change and
# no wrong one), but ALSO that deleting the `if category in
# DEFAULT_SLOT_COUNTS` fallback entirely left it green. The default PATH was
# never executed by any test, so a resolver that raised on every default-path
# caller shipped clean. These three guards pin the path, the docs and the
# collision class — never the value.

def test_default_path_resolves_the_cap_from_the_constant(
    fresh_orchestrator_dir, monkeypatch
):
    """No max_concurrent, no env — the DEFAULT branch must run and yield
    exactly DEFAULT_SLOT_COUNTS['mvn']. Read from the constant, so changing
    the value does not touch this test; removing the fallback does."""
    monkeypatch.delenv("CLAUDE_ORCH_SLOTS_MVN", raising=False)
    cap = DEFAULT_SLOT_COUNTS["mvn"]
    for n in range(cap):
        _register_worker(f"sid-{n}", name=f"W{n}")
        acquire_worker_slot_impl(claude_sid=f"sid-{n}", category="mvn")
    _register_worker("sid-over", name="over")
    with pytest.raises(TimeoutError):
        acquire_worker_slot_impl(
            claude_sid="sid-over", category="mvn", timeout_sec=1)


def test_live_docs_state_the_same_default_as_the_constant():
    """Both live surfaces stating the cap are bound to the constant. Derived,
    never a second hardcoded copy — it fires only when a doc and the code
    disagree, which is the state PR #259 found them in."""
    cap = DEFAULT_SLOT_COUNTS["mvn"]
    repo = Path(__file__).resolve().parents[1]
    from tests.carve_helpers import compose_generic
    worker = compose_generic("worker.md")
    assert f"default {cap} for mvn" in worker, (
        f"the rendered worker.md does not state 'default {cap} for mvn'. It "
        f"is the always-on file every worker reads, so a stale cap there is "
        f"the one that actually misleads someone")
    skill = (repo / "deploy/skills/dockwright-orchestrator-guide"
             / "SKILL.md").read_text()
    assert f"`mvn={cap}`" in skill, (
        f"the orchestrator-guide skill table does not state `mvn={cap}`")


def _slot_test_files() -> list[Path]:
    """Every file containing slot tests, DERIVED. Not `Path(__file__)` — the
    first version hardcoded this module, so a collision planted in a sibling
    file failed open."""
    root = Path(__file__).resolve().parent
    files = [p for p in sorted(root.rglob("test_*.py"))
             if "acquire_worker_slot" in p.read_text()]
    assert files, "derivation found no slot-test files — the parser broke"
    return files


def _all_integer_literals(path: Path) -> set[int]:
    """EVERY integer value anywhere in the file. No filtering by argument
    name, call shape, keyword-vs-positional, or nesting.

    ⚠️ The over-inclusion is the POINT, not a defect. The first version of
    this guard filtered — keyword arg, named `max_concurrent`, value an
    `ast.Constant` int, inside this file — five conditions that all had to
    hold at once. That is a CLASSIFIER, and a classifier fails open on the
    first shape it does not recognise: of eight real collisions planted by
    Tier-2 (positional, dict-splat, variable, parametrize, helper, `2+3`,
    sibling file) it caught ONE, while a `==` meta-assertion over its output
    passed, because the output was exactly the set it knew about.
    `~/.claude/rules/drift-guard-tests.md` names that failure; this now
    enumerates broadly and asserts the property instead. Mis-implementation
    must ADD cases, never drop them.

    ⚠️ STATED BOUNDARY, so nobody reads this as complete. Covered: every
    integer LITERAL, anywhere in these files, in any syntactic position,
    including negation and literal arithmetic (`5`, `-5`, `-(-5)`, `2+3`,
    `10-5`, `1*5`, `10//2`, nested and parenthesised forms — all measured to
    fire). NOT covered: a value produced at RUNTIME. `max_concurrent=len("abcde")`
    evaluates to 5 and this guard does not see it — measured, not assumed.
    (`len([1,2,3,4,5])` DOES fire, but only because the list holds a literal
    5, which is luck rather than coverage.) The folder below is the one place
    this EXTENDS recognition rather than broadening enumeration; its edges are
    probed, and past arithmetic the boundary is real and unbounded, so it is
    stated instead of chased."""
    out: set[int] = set()

    def fold(node):
        """Value of a node built only from int literals, else None. Covers
        `5`, `-5`, `2+3`. `ast.literal_eval` does NOT fold arithmetic and
        neither does CPython's compiler here, both measured."""
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, int) and not isinstance(
                node.value, bool) else None
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            inner = fold(node.operand)
            return None if inner is None else (
                inner if isinstance(node.op, ast.UAdd) else -inner)
        if isinstance(node, ast.BinOp):
            left, right = fold(node.left), fold(node.right)
            if left is None or right is None:
                return None
            for op, fn in ((ast.Add, lambda a, b: a + b),
                           (ast.Sub, lambda a, b: a - b),
                           (ast.Mult, lambda a, b: a * b),
                           (ast.FloorDiv, lambda a, b: a // b if b else None)):
                if isinstance(node.op, op):
                    return fn(left, right)
        return None

    for node in ast.walk(ast.parse(path.read_text())):
        value = fold(node)
        if value is not None:
            out.add(value)
    return out


def test_default_cap_collides_with_no_explicit_literal():
    """The explicit-max_concurrent branch goes vacuous whenever the default
    equals the literal a test passes: the call then yields the same cap with
    or without the argument. Tier-2 measured that at default 3, four tests
    passing max_concurrent=3 were testing nothing — 3 -> 5 repaired it by
    accident. This makes the class explicit instead of that luck."""
    cap = DEFAULT_SLOT_COUNTS["mvn"]
    for path in _slot_test_files():
        literals = _all_integer_literals(path)
        assert literals, f"no integer literals parsed from {path.name}"
        assert cap not in literals, (
            f"DEFAULT_SLOT_COUNTS['mvn'] = {cap} also appears as an integer "
            f"literal in {path.name}. If that literal is an expected cap, the "
            f"call stops discriminating — it yields {cap} whether or not the "
            f"argument or env is honoured, which is how four tests passing "
            f"max_concurrent=3 sat vacuous under the old default of 3.\n"
            f"This check is deliberately over-inclusive, so the hit may be an "
            f"unrelated literal. Move THAT literal, or change the default. Do "
            f"NOT narrow this guard to exclude it: the first exception is "
            f"where the next real collision hides")
