"""Every stdout write in the lane files must go through the flushing emit.

The mutation sweep proved 23 guards RED and still missed a site, because a
mutation set is the list of call sites the author already knew about: a site
that was never in the set is unguarded by construction. This guard is derived
from the thing it guards instead — it walks the AST of both lane files and
requires every `print` to be either explicitly `file=sys.stderr` (diagnostics,
which are not the event stream) or replaced by the emit helper.

The site it was written for is real: `stale_monitor._outbox_write`'s fallback
`print(line)`, the one path whose own docstring says losing the line is a true
event loss. It sat unflushed while `main()` went on to commit the ladder and
stamp a heartbeat.

AST rather than grep on purpose. A regex over lines cannot see a `print(...)`
whose `file=sys.stderr` sits on the next line, so it reports ~20 false
positives here and trains the reader to shrug — the coincidence-detector
failure `~/.claude/rules/drift-guard-tests.md` opens with.
"""
import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

def _lane_files():
    """Every module that writes to a manager's event stream, DERIVED.

    A lane file is one that defines or imports an emit helper — that is what
    makes its stdout an event stream. Listing the two I know about was the
    same hand-maintained-set failure this file's own docstring warns about:
    a third lane module would be unguarded by construction.
    """
    found = []
    for path in sorted((REPO / "src" / "dockwright").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        defines = any(isinstance(n, ast.FunctionDef) and n.name in EMIT_NAMES
                      for n in ast.walk(tree))
        imports = any(isinstance(n, ast.ImportFrom)
                      and any(a.name in EMIT_NAMES for a in n.names)
                      for n in ast.walk(tree))
        if defines or imports:
            found.append(path.relative_to(REPO).as_posix())
    return tuple(found)

EMIT_NAMES = {"emit", "_emit"}

# `sys.stdout.write(...)` is the legitimate body of emit()/_emit() in BOTH
# files, so it is the idiom a future author copies — and it is an
# ast.Attribute call, which a `print`-only guard never sees. Allowed only
# inside the two functions whose job it is.
STDOUT_WRITE_ATTRS = {"write", "writelines"}
EMIT_BODY_FUNCTIONS = {"emit", "_emit", "detach_stdout", "_detach_stdout"}


def _stderr_routed(node) -> bool:
    """True only for `file=sys.stderr`.

    Matching any `<x>.stderr` would accept `file=some_object.stderr`, which is
    not the process's error stream — the check has to name what it means.
    """
    for kw in node.keywords:
        if kw.arg != "file":
            continue
        value = kw.value
        return (isinstance(value, ast.Attribute) and value.attr == "stderr"
                and isinstance(value.value, ast.Name) and value.value.id == "sys")
    return False


def _stdout_prints(source: str):
    """Lines that write to stdout without going through the flushing emit.

    Two shapes, because banning only `print` leaves the one that already
    exists in these files: a bare `print(...)`, and a `sys.stdout.write(...)`
    outside the functions whose body it legitimately is.
    """
    tree = ast.parse(source)
    enclosing = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                enclosing.setdefault(id(inner), node.name)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "print":
            if not _stderr_routed(node):
                hits.append(node.lineno)
            continue
        func = node.func
        if (isinstance(func, ast.Attribute)
                and func.attr in STDOUT_WRITE_ATTRS
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "stdout"
                and enclosing.get(id(node)) not in EMIT_BODY_FUNCTIONS):
            hits.append(node.lineno)
    return sorted(hits)


@pytest.mark.parametrize("rel", _lane_files())
def test_no_lane_file_writes_to_stdout_without_flushing(rel):
    hits = _stdout_prints((REPO / rel).read_text(encoding="utf-8"))
    assert hits == [], (
        f"{rel} lines {hits} write to stdout with a bare print. Every event "
        f"line must go through the flushing emit helper, or a dead reader "
        f"loses it while the scan commits its cursor anyway. Diagnostics "
        f"belong on stderr (`file=sys.stderr`).")


@pytest.mark.parametrize("rel", _lane_files())
def test_the_lane_files_actually_emit(rel):
    """A file with no relationship to emit at all would pass vacuously.

    DEFINING the helper counts as well as calling it: `lane_io` defines `emit`
    and never calls it, and it is still a file whose stdout discipline matters
    — it is where the discipline lives.
    """
    tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id in EMIT_NAMES]
    defines = [n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name in EMIT_NAMES]
    assert calls or defines, (
        f"{rel} neither calls nor defines an emit helper — it should not have "
        f"been derived as a lane file, so the derivation is wrong")


def test_the_guard_catches_a_bare_print():
    """ADD-ONE: prove it fires on a site that does not exist yet."""
    assert _stdout_prints("print('an event line')\n") == [1]


def test_the_guard_catches_a_bare_sys_stdout_write():
    """The shape a future author copies from emit() itself."""
    assert _stdout_prints("import sys\nsys.stdout.write('an event line')\n") == [2]


def test_the_guard_allows_sys_stdout_write_inside_the_emit_body():
    """The other direction: emit() must still be able to do its job."""
    source = ("import sys\n"
              "def emit(line):\n"
              "    sys.stdout.write(line)\n"
              "    sys.stdout.flush()\n")
    assert _stdout_prints(source) == []


def test_the_guard_does_not_accept_a_lookalike_stderr():
    """`file=obj.stderr` is not the process's error stream."""
    source = ("class O: stderr = None\n"
              "o = O()\n"
              "print('x', file=o.stderr)\n")
    assert _stdout_prints(source) == [3]


def test_the_guard_accepts_a_stderr_print_split_across_lines():
    """The false-positive direction. A line-based regex reports this as a
    violation, which is how a guard earns a reputation for crying wolf."""
    source = ("print(\n"
              "    'a diagnostic',\n"
              "    file=sys.stderr)\n")
    assert _stdout_prints(source) == []


def test_the_guard_is_not_fooled_by_a_print_inside_a_nested_function():
    """Partial blindness, not just total: an AST walk must reach every scope."""
    source = ("def outer():\n"
              "    def inner():\n"
              "        print('buried event line')\n"
              "    return inner\n")
    assert _stdout_prints(source) == [3]


def test_the_guard_is_not_fooled_by_a_print_in_an_except_handler():
    """The exact shape of the site this was written for — a fallback path."""
    source = ("try:\n"
              "    pass\n"
              "except Exception:\n"
              "    print(line)\n")
    assert _stdout_prints(source) == [4]


def test_the_derived_lane_file_set_finds_the_known_ones():
    """A derivation that returned nothing would make every check above pass
    vacuously — the exact 'green because I found nothing' failure."""
    found = _lane_files()
    assert "src/dockwright/monitor.py" in found
    assert "src/dockwright/stale_monitor.py" in found
    assert "src/dockwright/lane_io.py" in found, (
        "lane_io defines emit() itself and must be checked too")
