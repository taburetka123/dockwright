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


def _operator_label_prefix():
    cfg = Path.home() / ".claude" / "dockwright.toml"
    if cfg.is_file():
        data = tomllib.loads(cfg.read_text())
        val = data.get("loops", {}).get("label_prefix")
        if isinstance(val, str) and val:
            return val
    return config.DEFAULT_LOOP_LABEL_PREFIX


def _operator_legacy_label_prefix():
    cfg = Path.home() / ".claude" / "dockwright.toml"
    if cfg.is_file():
        data = tomllib.loads(cfg.read_text())
        val = data.get("loops", {}).get("legacy_label_prefix")
        if isinstance(val, str) and val:
            return val
    return None


LABEL_PREFIX = _operator_label_prefix()
LEGACY_LABEL_PREFIX = _operator_legacy_label_prefix()
def _overlay_home() -> Path:
    new = Path.home() / ".claude" / "dockwright-overlay"
    legacy = Path.home() / ".claude" / "orchestrator-overlay"
    return new if new.exists() else legacy


OVERLAY_DIR = _overlay_home()

GARDENER_MODULE_LOOPS = ("selffix", "gardener-gate", "gardener-frontier")


def _operator_gardener_enabled():
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


_status = _load_status_module()
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


FLEET_MACHINE = LAUNCH_AGENTS.is_dir() and any(
    (Path.home() / ".claude" / p).is_dir() for p in ("dockwright", "orchestrator"))
machine = pytest.mark.skipif(
    not FLEET_MACHINE, reason="not the fleet machine (no LaunchAgents + dockwright state root)")


def test_registry_has_loops():
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


@machine
def test_every_labeled_plist_has_a_registry_block():
    unregistered = [p.stem for p in _census_plists() if p.stem not in LOOPS_BY_LABEL]
    assert not unregistered, (
        f"plists without a registry block: {unregistered} — either the loop ships "
        f"and needs a ```loop block in deploy/loops-registry.md, or a release "
        f"removed it and this machine still carries the plist. For a removed loop, "
        f"unload and delete it: launchctl bootout gui/$(id -u)/<label> && "
        f"rm ~/Library/LaunchAgents/<label>.plist")


@machine
def test_plist_program_paths_exist_unless_retiring():
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


@machine
@pytest.mark.parametrize("loop", LOOPS, ids=lambda l: l.get("name", "?"))
def test_status_reconciles_with_machine(loop):
    labels = _launchctl_labels()
    if labels is None:
        pytest.skip("launchctl unavailable")
    status, label, name = loop["status"], loop["label"], loop["name"]

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
            assert label not in labels, \
                f"{name}: pending-install but loaded — installer ran; add a " \
                f"[loops.status_overrides] entry (or flip the core row)"

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


def test_parser_roundtrips_known_fields():
    bootlite = next(loop for loop in LOOPS if loop["name"] == "bootlite-watchdog")
    assert bootlite["label"] == f"{LABEL_PREFIX}.bootlite-watchdog"
    assert bootlite["kill_switch"] == "~/.claude/dockwright/bootlite-stop"
    assert bootlite["max_silence_hours"] == "26"


def test_parser_ignores_prose_outside_blocks():
    text = "# header\nprose key: value\n```loop\nname: x\nstatus: live\n```\nmore prose\n"
    parsed = _status.parse_registry(text)
    assert parsed == [{"name": "x", "status": "live"}]


def test_registry_paths_unions_overlay(tmp_path):
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
    gate = next(loop for loop in LOOPS if loop["name"] == "gardener-gate")
    assert gate["label"] == f"{LABEL_PREFIX}.gardener-gate"


def test_status_overrides_apply_by_name(tmp_path):
    core = tmp_path / "loops-registry.md"
    core.write_text(REGISTRY_PATH.read_text())
    loops = _status.load_all_loops(cli_arg=str(core), overlay_dir=tmp_path / "no-ov",
                                   status_overrides={"selffix": {"status": "live", "status_why": "op"}})
    selffix = next(l for l in loops if l["name"] == "selffix")
    assert selffix["status"] == "live" and selffix["status_why"] == "op"
    gate = next(l for l in loops if l["name"] == "gardener-gate")
    assert gate["status"] == "pending-install"


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


def test_launchctl_states_reads_every_label_not_just_com(monkeypatch):
    mod = _load_status_module()
    listing = "\n".join([
        "PID\tStatus\tLabel",
        "111\t0\tcom.example.pr-review-poller",
        "43587\t0\tco.example.daily-digest",
        "-\t0\torg.example.helper.launcher",
    ])

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=listing, stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    states = mod.launchctl_states()
    assert states["com.example.pr-review-poller"] == "0"
    assert states["co.example.daily-digest"] == "0"
    assert states["org.example.helper.launcher"] == "0"
    assert "Label" not in states
