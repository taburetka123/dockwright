from __future__ import annotations

import os
import re
import subprocess
import time

from .hooks import _awake_seconds

_SYSCTL_SEC = re.compile(r"\bsec\s*=\s*(\d+)")
_PROC_BTIME = re.compile(r"^btime\s+(\d+)\s*$", re.MULTILINE)

PLAUSIBILITY_TOLERANCE_SEC = 600.0
EPOCH_FLOOR = 1_000_000_000


class BootEpochUnavailable(Exception):
    pass


def parse_sysctl_boottime(text: str | None) -> int | None:
    match = _SYSCTL_SEC.search(text or "")
    return int(match.group(1)) if match else None


def parse_proc_stat_btime(text: str | None) -> int | None:
    match = _PROC_BTIME.search(text or "")
    return int(match.group(1)) if match else None


def _read_sysctl() -> str | None:
    try:
        proc = subprocess.run(["sysctl", "-n", "kern.boottime"],
                              capture_output=True, text=True, timeout=5, check=False)
    except Exception:
        return None
    return proc.stdout if proc.returncode == 0 else None


def _read_proc_stat() -> str | None:
    try:
        with open("/proc/stat") as handle:
            return handle.read()
    except OSError:
        return None


def plausible(epoch: int, now: float | None = None, awake: float | None = None) -> bool:
    now = time.time() if now is None else now
    awake = _awake_seconds() if awake is None else awake
    if epoch < EPOCH_FLOOR or epoch > now:
        return False
    return epoch <= now - awake + PLAUSIBILITY_TOLERANCE_SEC


def resolve(env=None, now=None, awake=None,
            sysctl_reader=None, proc_stat_reader=None) -> int:
    env = os.environ if env is None else env
    override = (env.get("DOCKWRIGHT_BOOT_EPOCH") or "").strip()
    if override:
        try:
            return int(float(override))
        except (ValueError, OverflowError):
            raise BootEpochUnavailable(
                f"DOCKWRIGHT_BOOT_EPOCH is not a number: {override!r}")
    sysctl_reader = _read_sysctl if sysctl_reader is None else sysctl_reader
    proc_stat_reader = _read_proc_stat if proc_stat_reader is None else proc_stat_reader
    epoch = parse_sysctl_boottime(sysctl_reader())
    if epoch is None:
        epoch = parse_proc_stat_btime(proc_stat_reader())
    if epoch is None:
        raise BootEpochUnavailable(
            "could not read the host boot time from sysctl or /proc/stat")
    if not plausible(epoch, now=now, awake=awake):
        raise BootEpochUnavailable(
            f"boot epoch {epoch} is not plausible against the current clock and uptime")
    return epoch
