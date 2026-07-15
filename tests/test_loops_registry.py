"""Loops-registry teeth: reconcile deploy/loops-registry.md against the machine.

Three layers (arch review IMPORTANT-3 — pytest as the enforcement seam):
1. Schema: every ```loop block carries the full field set, non-empty; five-tuple
   completeness ("none" allowed but explicit); valid status vocabulary.
2. Census bijection: every ~/Library/LaunchAgents/<label_prefix>.*.plist has a block.
3. Reconciliation: each block's intended status matches launchctl/settings/disk
   reality — live ⇒ loaded + program exists; paused ⇒ not loaded; retired ⇒
   plist gone; retiring / pending-install ⇒ transitional, no assertions.

Machine layers skip off this Mac (no LaunchAgents dir / no launchctl) so the
suite stays green in any future CI; the schema layer always runs.

Census glob prefix: the union, deduped, of `config.loop_label_prefix()` (the
product default, blanked to "com.dockwright" by conftest inside the suite), the
live operator `[loops].label_prefix` (LABEL_PREFIX), and — when set — the live
operator `[loops].legacy_label_prefix` (read module-scope straight from
~/.claude/dockwright.toml, default None on a generic clone). A machine's real
installed plists can predate the `[loops].label_prefix` key (they were written
by deploy/scripts/*-install.sh back when the prefix was a literal), so globbing
only the config-resolved prefix would go quietly blind on any machine whose
dockwright.toml hasn't caught up yet — a silent, vacuous pass on exactly the
drift this test exists to catch. The operator pins the historical prefix in
`[loops].legacy_label_prefix` so the census keeps finding those legacy plists;
a generic clone has no such key (None), so only the product/operator prefixes
are globbed. Same "read the live operator state explicitly, not config.*"
pattern as _operator_label_prefix.
"""
import importlib.util
import plistlib
import subprocess
import tomllib
from pathlib import Path

import pytest

from dockwright import config


REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "deploy" / "loops-registry.md"
STATUS_SCRIPT = REPO_ROOT / "deploy" / "scripts" / "loops_status.py"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


# The LIVE operator launchd label-prefix + overlay root, read explicitly from
# ~/.claude (honoring $HOME), NOT via config.* — conftest's autouse
# _dockwright_config_hermetic blanks config to the com.dockwright default inside
# the suite, but the operator's actually-installed plists carry the real prefix
# (~/.claude/dockwright.toml [loops].label_prefix, e.g. com.example). The census /
# reconciliation must expand the core registry's `{prefix}` templates to that
# real prefix to match the installed plists. Same "read the live operator state
# explicitly" pattern as tests/carve_helpers.py; a generic clone (no config,
# HOME=$(mktemp)) falls back to the com.dockwright product default.
def _operator_label_prefix():
    cfg = Path.home() / ".claude" / "dockwright.toml"
    if cfg.is_file():
        data = tomllib.loads(cfg.read_text())
        val = data.get("loops", {}).get("label_prefix")
        if isinstance(val, str) and val:
            return val
    return config.DEFAULT_LOOP_LABEL_PREFIX


def _operator_legacy_label_prefix():
    """The operator's real [loops].legacy_label_prefix, read straight from
    ~/.claude/dockwright.toml — the historical launchd prefix a machine's older
    installed plists still carry. Same shape as _operator_label_prefix but keyed
    on legacy_label_prefix; default None on a generic clone (no legacy plists to
    find), so _label_prefixes drops it from the glob set."""
    cfg = Path.home() / ".claude" / "dockwright.toml"
    if cfg.is_file():
        data = tomllib.loads(cfg.read_text())
        val = data.get("loops", {}).get("legacy_label_prefix")
        if isinstance(val, str) and val:
            return val
    return None


LABEL_PREFIX = _operator_label_prefix()
LEGACY_LABEL_PREFIX = _operator_legacy_label_prefix()
OVERLAY_DIR = Path.home() / ".claude" / "orchestrator-overlay"

# The Gardener-module loops: [modules] gardener=false no-ops all three and the
# installer refuses, so on a module-off machine their plists are absent —
# reconciliation must not demand a `live` block be loaded there.
GARDENER_MODULE_LOOPS = ("selffix", "gardener-gate", "gardener-frontier")


def _operator_gardener_enabled():
    """The operator's real [modules] gardener value, read straight from
    ~/.claude/dockwright.toml — the SAME 'read live state, not config.*' pattern
    as _operator_label_prefix (conftest blanks config inside the suite, but the
    installed plists reflect the operator's real config). Default True
    (fail-open), matching config.gardener_module_enabled()."""
    cfg = Path.home() / ".claude" / "dockwright.toml"
    if cfg.is_file():
        try:
            val = tomllib.loads(cfg.read_text()).get("modules", {}).get("gardener")
            if isinstance(val, bool):
                return val
        except (tomllib.TOMLDecodeError, OSError):
            pass
    return True


GARDENER_ENABLED = _operator_gardener_enabled()


def _operator_status_overrides():
    """The operator's real [loops.status_overrides] tables, read straight from
    ~/.claude/dockwright.toml — the SAME 'read live state, not config.*' pattern
    as _operator_label_prefix (conftest blanks config inside the suite, but the
    installed plists reflect the operator's real config). {} on a generic clone
    (fail-open), matching config.loop_status_overrides()."""
    cfg = Path.home() / ".claude" / "dockwright.toml"
    if cfg.is_file():
        try:
            sec = tomllib.loads(cfg.read_text()).get("loops", {}).get("status_overrides", {})
            return {n: v for n, v in sec.items() if isinstance(v, dict)}
        except (tomllib.TOMLDecodeError, OSError):
            pass
    return {}


def _label_prefixes():
    prefixes = {config.loop_label_prefix(), LABEL_PREFIX}
    if LEGACY_LABEL_PREFIX:
        prefixes.add(LEGACY_LABEL_PREFIX)
    return sorted(prefixes)


def _census_plists():
    found = set()
    for prefix in _label_prefixes():
        found.update(LAUNCH_AGENTS.glob(f"{prefix}.*.plist"))
    return sorted(found)


def _load_status_module():
    spec = importlib.util.spec_from_file_location("loops_status_under_test", STATUS_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# .get() everywhere at collection time — a block missing `label` must fail the
# schema test with its message, not KeyError the whole collection.
_status = _load_status_module()
# Core ∪ overlay: product blocks from the repo registry PLUS the operator overlay
# loops (operator-loops.md), with `{prefix}` labels expanded to the live operator
# prefix so the census/reconciliation match the actually-installed plists, and
# the core registry's neutral pending-install statuses replaced by the operator's
# [loops.status_overrides] so reconciliation checks the operator's real intent.
LOOPS = _status.load_all_loops(cli_arg=str(REGISTRY_PATH), overlay_dir=OVERLAY_DIR,
                               prefix=LABEL_PREFIX,
                               status_overrides=_operator_status_overrides())
LOOPS_BY_LABEL = {loop.get("label"): loop for loop in LOOPS
                  if loop.get("label") not in (None, "none")}


def _launchctl_labels():
    try:
        result = subprocess.run(["launchctl", "list"], capture_output=True,
                                timeout=10, check=False, text=True)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return {line.split("\t")[2] for line in result.stdout.splitlines()
            if len(line.split("\t")) == 3}


def _expand(path_str):
    return Path(path_str).expanduser()


# ~/Library/LaunchAgents alone is a weak proxy (it exists on most Macs); the
# orchestrator state root only exists where the fleet actually runs, so a fresh
# clone on a non-fleet Mac skips instead of failing on "gardener not loaded".
FLEET_MACHINE = LAUNCH_AGENTS.is_dir() and (Path.home() / ".claude" / "orchestrator").is_dir()
machine = pytest.mark.skipif(
    not FLEET_MACHINE, reason="not the fleet machine (no LaunchAgents + orchestrator state root)")


# --- Layer 1: schema (always runs) -------------------------------------------

def test_registry_has_loops():
    # Core product floor: the repo registry always ships its 5 product loops
    # (selffix, gardener-gate/-frontier, worktree-prune, bootlite-watchdog). An
    # operator machine adds its overlay loops on top; a generic clone has just
    # the five.
    assert len(LOOPS) >= 5


@pytest.mark.parametrize("loop", LOOPS, ids=lambda l: l.get("name", "?"))
def test_block_has_all_required_fields_non_empty(loop):
    for field in _status.REQUIRED_FIELDS:
        assert loop.get(field), f"{loop.get('name')}: field '{field}' missing or empty"


@pytest.mark.parametrize("loop", LOOPS, ids=lambda l: l.get("name", "?"))
def test_block_status_is_valid(loop):
    assert loop["status"] in _status.VALID_STATUSES


def test_loop_names_and_labels_unique():
    names = [loop.get("name") for loop in LOOPS]
    assert len(names) == len(set(names))
    labels = [loop.get("label") for loop in LOOPS if loop.get("label") != "none"]
    assert len(labels) == len(set(labels))


def test_hook_loops_declare_hook_command():
    for loop in LOOPS:
        if loop.get("label") == "none":
            assert loop.get("hook_command"), \
                f"{loop['name']}: label=none requires hook_command for reconciliation"


@pytest.mark.parametrize("name", GARDENER_MODULE_LOOPS)
def test_gardener_module_loops_document_toggle(name):
    """The [modules] gardener toggle no-ops the whole Gardener pipeline; each
    gardener loop's block must document it so an operator reading the registry
    knows the loop is gated by config, not just by its stop file."""
    loop = next((l for l in LOOPS if l.get("name") == name), None)
    assert loop is not None, f"{name} block missing from the registry"
    blob = " ".join(str(v) for v in loop.values())
    assert "[modules] gardener" in blob, \
        f"{name}: block must document the [modules] gardener toggle"


# --- Layer 2: census bijection (machine only) --------------------------------

@machine
def test_every_labeled_plist_has_a_registry_block():
    unregistered = [p.stem for p in _census_plists() if p.stem not in LOOPS_BY_LABEL]
    assert not unregistered, (
        f"plists without a registry block: {unregistered} — add a ```loop block "
        f"to deploy/loops-registry.md in the same change that ships the loop")


@machine
def test_plist_program_paths_exist_unless_retiring():
    """The ticket-cleanup class: a plist whose program no longer exists fails
    daily and silently. A dead path is only acceptable while the row SAYS the
    loop is being retired/retired."""
    for plist_path in _census_plists():
        loop = LOOPS_BY_LABEL.get(plist_path.stem)
        if loop is None or loop["status"] in ("retiring", "retired"):
            continue
        with plist_path.open("rb") as f:
            program_args = plistlib.load(f).get("ProgramArguments", [])
        for arg in program_args:
            if arg.startswith("/"):
                assert Path(arg).exists(), (
                    f"{plist_path.stem}: ProgramArguments path {arg} does not exist "
                    f"(registry status={loop['status']})")


# --- Layer 3: intended-state reconciliation (machine only) -------------------

@machine
@pytest.mark.parametrize("loop", LOOPS, ids=lambda l: l.get("name", "?"))
def test_status_reconciles_with_machine(loop):
    labels = _launchctl_labels()
    if labels is None:
        pytest.skip("launchctl unavailable")
    status, label, name = loop["status"], loop["label"], loop["name"]

    # [modules] gardener=false: the three gardener loops are legitimately not
    # installed (gardener-install.sh refuses), so a `live` row need not be loaded.
    if name in GARDENER_MODULE_LOOPS and not GARDENER_ENABLED:
        pytest.skip(f"{name}: [modules] gardener disabled — loop intentionally not installed")

    if label != "none":
        plist = LAUNCH_AGENTS / f"{label}.plist"
        if status == "live":
            assert plist.exists(), f"{name}: live but no plist — install it or flip the row"
            assert label in labels, f"{name}: live but not loaded — bootstrap it or flip to paused"
        elif status == "paused":
            assert label not in labels, f"{name}: paused but loaded — flip to live or unload"
        elif status == "retired":
            assert not plist.exists(), f"{name}: retired but plist still present"
            assert label not in labels, f"{name}: retired but still loaded"
        elif status == "pending-install":
            # A loaded label whose row still says pending-install means the
            # installer ran but the operator's override never flipped — force it.
            assert label not in labels, \
                f"{name}: pending-install but loaded — installer ran; add a " \
                f"[loops.status_overrides] entry (or flip the core row)"
        # retiring: transitional, no assertions

    hook_command = loop.get("hook_command")
    if hook_command and SETTINGS_PATH.is_file():
        wired = hook_command in SETTINGS_PATH.read_text()
        if status == "live":
            assert wired, f"{name}: live but hook '{hook_command}' not in settings.json"
        elif status == "paused":
            assert not wired, f"{name}: paused but hook '{hook_command}' is wired"

    if status in ("live", "paused"):
        program = loop["runtime_program_path"]
        if program != "none":
            assert _expand(program).exists(), f"{name}: runtime_program_path missing: {program}"


# --- Parser sanity (always runs) ----------------------------------------------

def test_parser_roundtrips_known_fields():
    bootlite = next(loop for loop in LOOPS if loop["name"] == "bootlite-watchdog")
    # Core product blocks carry the `{prefix}.<name>` template; the union loader
    # expands it to the resolved prefix. Pinned against the live-resolved
    # LABEL_PREFIX (not config.loop_label_prefix(), which conftest blanks to the
    # com.dockwright default inside the suite).
    assert bootlite["label"] == f"{LABEL_PREFIX}.bootlite-watchdog"
    assert bootlite["kill_switch"] == "~/.claude/dockwright/bootlite-stop"
    assert bootlite["max_silence_hours"] == "26"


def test_parser_ignores_prose_outside_blocks():
    text = "# header\nprose key: value\n```loop\nname: x\nstatus: live\n```\nmore prose\n"
    parsed = _status.parse_registry(text)
    assert parsed == [{"name": "x", "status": "live"}]


# --- Union loader: core ∪ overlay + {prefix} label templating -----------------

def test_registry_paths_unions_overlay(tmp_path):
    """load_all_loops unions the core registry with every overlay loops/*.md so
    an operator's out-of-core loop still gets a registry block."""
    core = tmp_path / "loops-registry.md"
    core.write_text(REGISTRY_PATH.read_text())
    ov = tmp_path / "ov" / "loops"
    ov.mkdir(parents=True)
    (ov / "operator-loops.md").write_text(
        "```loop\nname: op-extra\nlabel: com.example.op-extra\nstatus: paused\n"
        "status_why: t\ntrigger: t\ngate: t\nrun_contract: t\npermissions_mode: t\n"
        "ledger_path: t\nkill_switch: t\nruntime_program_path: t\nsource_path: t\n"
        "deploy_mechanism: t\nlog_paths: t\nevent_paths: t\nmax_silence_hours: 24\n"
        "last_verified: 2026-07-03\n```\n")
    loops = _status.load_all_loops(cli_arg=str(core), overlay_dir=tmp_path / "ov")
    names = {loop["name"] for loop in loops}
    assert "op-extra" in names and "gardener-gate" in names


def test_product_labels_expand_prefix():
    """A core product block's `{prefix}.<name>` label is expanded to the resolved
    prefix by the union loader."""
    gate = next(loop for loop in LOOPS if loop["name"] == "gardener-gate")
    assert gate["label"] == f"{LABEL_PREFIX}.gardener-gate"


def test_status_overrides_apply_by_name(tmp_path):
    """An explicit status_overrides dict replaces status/status_why on the
    matching-name block only; untouched blocks keep the core registry value."""
    core = tmp_path / "loops-registry.md"
    core.write_text(REGISTRY_PATH.read_text())
    loops = _status.load_all_loops(cli_arg=str(core), overlay_dir=tmp_path / "no-ov",
                                   status_overrides={"selffix": {"status": "live", "status_why": "op"}})
    selffix = next(l for l in loops if l["name"] == "selffix")
    assert selffix["status"] == "live" and selffix["status_why"] == "op"
    gate = next(l for l in loops if l["name"] == "gardener-gate")
    assert gate["status"] == "pending-install"   # untouched blocks keep core value


def test_deployed_paths_prefer_dockwright_home(tmp_path, monkeypatch):
    claude = tmp_path / ".claude"
    (claude / "dockwright").mkdir(parents=True)
    (claude / "dockwright" / "loops-registry.md").write_text("")
    (claude / "loops-registry.md").write_text("")
    (claude / "dockwright-overlay").mkdir()
    (claude / "orchestrator-overlay").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    mod = _load_status_module()
    assert mod.DEPLOYED_REGISTRY == claude / "dockwright" / "loops-registry.md"
    assert mod.DEFAULT_OVERLAY_DIR == claude / "dockwright-overlay"


def test_deployed_paths_fall_back_to_legacy_home(tmp_path, monkeypatch):
    claude = tmp_path / ".claude"
    claude.mkdir(parents=True)
    (claude / "loops-registry.md").write_text("")
    (claude / "orchestrator-overlay").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    mod = _load_status_module()
    assert mod.DEPLOYED_REGISTRY == claude / "loops-registry.md"
    assert mod.DEFAULT_OVERLAY_DIR == claude / "orchestrator-overlay"
