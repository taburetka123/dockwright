"""One-shot state migration: ~/.claude/orchestrator (+ scattered dockwright
state) -> ~/.claude/dockwright, leaving a compat symlink at every old path.

Live-fleet safe: a manager may be polling these dirs right now. Each row is
os.rename + os.symlink back-to-back (µs window vs seconds-scale polls), and
symlink EEXIST (a poller's mkdir re-creating the legacy dir mid-window) is
handled by merging the reborn dir and retrying. Nothing is ever deleted or
overwritten; file collisions abort loudly with both paths intact, and any
aborted row is recoverable by re-running after reconciling.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import config

# (legacy-rel, new-rel) under the claude dir. Root row FIRST — it creates
# dockwright/ that later rows nest under.
ROWS: tuple[tuple[str, str], ...] = (
    ("orchestrator", "dockwright"),
    ("manager-memory", "dockwright/manager-memory"),
    ("selffix-findings", "dockwright/selffix/findings"),
    ("selffix-retry", "dockwright/selffix/retry"),
    ("selffix-trigger.log", "dockwright/selffix/trigger.log"),
    ("selffix-nudges.log", "dockwright/selffix/nudges.log"),
    ("selffix-nudges.log.tmp", "dockwright/selffix/nudges.log.tmp"),
    ("selffix-debug", "dockwright/selffix/debug"),
    ("gardener", "dockwright/gardener"),
    ("bootlite", "dockwright/bootlite"),
    ("worktree-prune", "dockwright/worktree-prune"),
    ("loops-registry.md", "dockwright/loops-registry.md"),
    ("gardener-stop", "dockwright/gardener-stop"),
    ("frontier-stop", "dockwright/frontier-stop"),
    ("bootlite-stop", "dockwright/bootlite-stop"),
    ("worktree-prune-stop", "dockwright/worktree-prune-stop"),
    (".orchestrator-deploy", "dockwright/.deploy-stamp"),
    ("orchestrator-overlay", "dockwright-overlay"),
)

_SYMLINK_RETRIES = 5
_LEGACY_PIN_KEYS = {
    "state_root": config.LEGACY_STATE_ROOT,
    "manager_memory": config.LEGACY_MANAGER_MEMORY,
    "overlay_dir": config.LEGACY_OVERLAY_DIR,
}


class MigrationError(RuntimeError):
    pass


def _assert_no_legacy_toml_pins() -> None:
    """An explicit legacy [paths] pin bypasses the fallback verbatim and would
    break silently when the compat symlinks retire — fail the migration now.
    Compared post-expansion so an absolute-path spelling of the same legacy
    home is caught too."""
    paths_sec = config.load().get("paths")
    if not isinstance(paths_sec, dict):
        return
    for key, legacy_default in _LEGACY_PIN_KEYS.items():
        val = paths_sec.get(key)
        if (isinstance(val, str)
                and config._expand(val.rstrip("/")) == config._expand(legacy_default)):
            raise MigrationError(
                f"dockwright.toml [paths].{key} explicitly pins the legacy "
                f"path {val!r}; update it to the new location (or drop the "
                f"key) before migrating: {config.config_path()}"
            )


def _merge(src: Path, dst: Path) -> None:
    """Move src's children into dst. Recurse on real-dir/real-dir collisions;
    abort loudly on any other collision — never overwrite state. A symlink on
    EITHER side is never followed: a symlink child of src is renamed as-is,
    and a symlink at dst is a collision (following it would silently relocate
    state to wherever it points — worst case back into src)."""
    if (src.is_dir() and not src.is_symlink()
            and dst.is_dir() and not dst.is_symlink()):
        for child in sorted(src.iterdir()):
            _merge(child, dst / child.name)
        try:
            src.rmdir()
        except OSError as e:
            raise MigrationError(f"could not remove emptied {src}: {e}") from e
        return
    if dst.exists() or dst.is_symlink():
        raise MigrationError(
            f"collision migrating {src} -> {dst}: destination already exists; "
            f"reconcile manually (nothing was overwritten)"
        )
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dst)


def _collision_scan(src: Path, dst: Path) -> list[str]:
    """Read-only preview of _merge: the paths a real merge would abort on."""
    if (src.is_dir() and not src.is_symlink()
            and dst.is_dir() and not dst.is_symlink()):
        hits: list[str] = []
        for child in sorted(src.iterdir()):
            hits.extend(_collision_scan(child, dst / child.name))
        return hits
    if dst.exists() or dst.is_symlink():
        return [str(src)]
    return []


def _place_symlink(legacy: Path, target_rel: str, new: Path) -> None:
    for _ in range(_SYMLINK_RETRIES):
        try:
            os.symlink(target_rel, legacy)
            return
        except FileExistsError:
            if legacy.is_symlink():
                break
            # A live poller's mkdir re-created the legacy dir mid-window:
            # fold whatever landed in it into the new home and retry. (If
            # legacy vanished again in between, just retry the symlink.)
            if legacy.exists():
                _merge(legacy, new)
    if not legacy.is_symlink():
        raise MigrationError(
            f"could not place compat symlink at {legacy} after "
            f"{_SYMLINK_RETRIES} attempts (legacy path kept reappearing); "
            f"migrated state is intact at {new} — re-run to retry"
        )


def _verify(legacy: Path, new: Path) -> None:
    if not legacy.is_symlink():
        raise MigrationError(f"verification failed: {legacy} is not a symlink")
    if legacy.resolve() != new.resolve():
        raise MigrationError(
            f"verification failed: {legacy} -> {os.readlink(legacy)} "
            f"does not resolve to {new}"
        )


def _relative_target(legacy: Path, new: Path) -> str:
    return os.path.relpath(new, legacy.parent)


def _migrate_row(claude_dir: Path, legacy_rel: str, new_rel: str,
                 dry_run: bool) -> str:
    legacy = claude_dir / legacy_rel
    new = claude_dir / new_rel
    if legacy.is_symlink():
        if legacy.resolve() == new.resolve():
            return f"ok       {legacy_rel} (already migrated)"
        raise MigrationError(
            f"{legacy} is a symlink to {os.readlink(legacy)}, expected {new}"
        )
    if not legacy.exists():
        if not new.exists():
            return f"absent   {legacy_rel} (skip; created on demand at new home)"
        # Crash residue: a prior run moved the data but died before the
        # symlink landed. Old deployed code hardcodes the legacy path, so a
        # cold row would sit fork-armed until something rebirths it — repair
        # the compat link now. (On a fresh install this also links legacy
        # names to ensure_dirs-precreated new dirs — inert, self-retires.)
        if dry_run:
            return f"would-link {legacy_rel} (crash-repair / compat link)"
        _place_symlink(legacy, _relative_target(legacy, new), new)
        _verify(legacy, new)
        return f"linked   {legacy_rel} (crash-repair / compat link)"
    new_occupied = new.exists() or new.is_symlink()
    if dry_run:
        if new_occupied:
            hits = _collision_scan(legacy, new)
            if hits:
                return (f"would-FAIL {legacy_rel} -> {new_rel} "
                        f"(collisions: {', '.join(hits)})")
            return f"would-merge {legacy_rel} -> {new_rel}"
        return f"would-mv {legacy_rel} -> {new_rel}"
    if new_occupied:
        _merge(legacy, new)
    else:
        new.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(legacy, new)
        except OSError as e:
            # A concurrent ensure_dirs can create new (even non-empty)
            # between the check above and the rename — fold into it. Any
            # other failure (permissions, ...) stays inside the loud
            # MigrationError contract.
            if not (new.exists() or new.is_symlink()):
                raise MigrationError(
                    f"could not move {legacy} -> {new}: {e}") from e
            _merge(legacy, new)
    _place_symlink(legacy, _relative_target(legacy, new), new)
    _verify(legacy, new)
    return f"migrated {legacy_rel} -> {new_rel}"


def run(claude_dir: Path, dry_run: bool = False) -> list[str]:
    _assert_no_legacy_toml_pins()
    return [
        _migrate_row(claude_dir, legacy_rel, new_rel, dry_run)
        for legacy_rel, new_rel in ROWS
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dockwright migrate-state",
        description="Move orchestrator-era state under ~/.claude/dockwright, "
                    "leaving compat symlinks at the old paths.",
    )
    parser.add_argument("--claude-dir", default=os.path.expanduser("~/.claude"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        for line in run(Path(args.claude_dir).expanduser(), dry_run=args.dry_run):
            print(line)
    except MigrationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0
