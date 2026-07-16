"""PATH-prepend `stat` shims emulating the two platform personalities.

GNU coreutils (verified Ubuntu 25.10): `stat -f %m <file>` prints a multi-line
filesystem-info block to STDOUT, then exits 1 — inside `$(a || b)` the garbage
concatenates with the fallback's output and poisons the result (E2E N-1).
BSD/macOS: `stat -f %m` is the correct file-mtime form; `-c` is an illegal
option (rc=1, empty stdout).
"""
from pathlib import Path

_PY_MTIME = (
    "python3 -c 'import os,sys;print(int(os.stat(sys.argv[1]).st_mtime))' \"$3\""
)

GNU_STAT_SHIM = f"""#!/usr/bin/env bash
if [ "$1" = "-f" ] && [ "$2" = "%m" ]; then
  printf '  File: "%s"\\n    ID: 63b1412d5ada6273 Namelen: 255     Type: tmpfs\\nBlock size: 4096       Fundamental block size: 4096\\nBlocks: Total: 498604     Free: 496740     Available: 496740\\nInodes: Total: 1048576    Free: 1047912\\n' "$3"
  exit 1
fi
if [ "$1" = "-c" ] && [ "$2" = "%Y" ]; then
  {_PY_MTIME}
  exit 0
fi
echo "stat-shim(gnu): unexpected args: $*" >&2
exit 2
"""

BSD_STAT_SHIM = f"""#!/usr/bin/env bash
if [ "$1" = "-f" ] && [ "$2" = "%m" ]; then
  {_PY_MTIME}
  exit 0
fi
case "$1" in
  -c*) echo "stat: illegal option -- c" >&2; exit 1;;
esac
echo "stat-shim(bsd): unexpected args: $*" >&2
exit 2
"""


def _write(dir_path: Path, body: str) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    shim = dir_path / "stat"
    shim.write_text(body)
    shim.chmod(0o755)
    return dir_path


def write_gnu_stat_shim(dir_path: Path) -> Path:
    return _write(dir_path, GNU_STAT_SHIM)


def write_bsd_stat_shim(dir_path: Path) -> Path:
    return _write(dir_path, BSD_STAT_SHIM)
