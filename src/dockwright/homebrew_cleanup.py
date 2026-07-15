"""Surgically remove a Homebrew system-python editable install of a distribution.

The canonical install is the tool's own venv. A duplicate editable install in Homebrew's
externally-managed system python (PEP 668) puts a second console script on PATH and a
global .pth that a worktree `pip install -e` can hijack. This removes ONLY the named
distribution's own artifacts (pip uninstall by name uses the package's RECORD) — never
unrelated Homebrew packages.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class CleanupError(RuntimeError):
    pass


@dataclass
class BrewEditable:
    python_bin: Path
    site_packages: Path
    artifacts: list


def _under(dist_name: str) -> str:
    return dist_name.replace("-", "_")


def find_brew_editable(brew_prefix, dist_name: str) -> list:
    """Homebrew interpreters with an editable install of dist_name.

    Detection key: `__editable__.<dist>-*.pth` OR `<dist>-*.dist-info`. The
    `__editable___<dist>_*finder.py` form is collected if present, never required.
    """
    brew_prefix = Path(brew_prefix)
    dist = _under(dist_name)
    out = []
    lib = brew_prefix / "lib"
    if not lib.is_dir():
        return out
    for site in sorted(lib.glob("python3.*/site-packages")):
        pths = list(site.glob(f"__editable__.{dist}-*.pth"))
        distinfos = list(site.glob(f"{dist}-*.dist-info"))
        if not pths and not distinfos:
            continue
        artifacts = pths + distinfos + list(site.glob(f"__editable___{dist}_[0-9]*finder.py"))
        python_bin = brew_prefix / "bin" / site.parent.name  # .../python3.14/site-packages -> python3.14
        out.append(BrewEditable(python_bin=python_bin, site_packages=site, artifacts=artifacts))
    return out


def find_stray_console_script(bin_dir, console_script: str, brew_prefix) -> Path | None:
    """Return bin_dir/console_script iff it exists and its shebang points into brew_prefix."""
    path = Path(bin_dir) / console_script
    if not path.is_file():
        return None
    try:
        first = path.read_text(errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return None
    return path if first.startswith("#!") and str(brew_prefix) in first else None


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink()


def clean(brew_prefix, dist_name: str, console_script: str, *, bin_dir=None,
          run=subprocess.run, dry_run: bool = False) -> dict:
    brew_prefix = Path(brew_prefix)
    bin_dir = Path(bin_dir) if bin_dir else brew_prefix / "bin"
    found = find_brew_editable(brew_prefix, dist_name)
    stray = find_stray_console_script(bin_dir, console_script, brew_prefix)
    report = {"uninstalled": [], "removed_scripts": [], "dry_run": dry_run}

    if dry_run:
        report["would_uninstall"] = [str(e.python_bin) for e in found]
        report["would_remove_script"] = str(stray) if stray else None
        return report

    for e in found:
        if e.python_bin.exists():
            run([str(e.python_bin), "-m", "pip", "uninstall", "-y",
                 "--break-system-packages", dist_name], check=False)
            report["uninstalled"].append(str(e.python_bin))
        for art in e.artifacts:    # remove anything pip didn't (e.g. a hijacked orphan .pth)
            _remove(art)
    if stray and stray.exists():
        stray.unlink()
        report["removed_scripts"].append(str(stray))

    residual_import = []
    for e in found:
        if e.python_bin.exists():
            r = run([str(e.python_bin), "-c", f"import {_under(dist_name)}"],
                    capture_output=True, check=False)
            if getattr(r, "returncode", 1) == 0:
                residual_import.append(str(e.python_bin))
    residual_art = [str(a) for e in find_brew_editable(brew_prefix, dist_name) for a in e.artifacts]
    residual_script = find_stray_console_script(bin_dir, console_script, brew_prefix)
    if residual_import or residual_art or residual_script:
        raise CleanupError(
            f"residual after cleanup: import={residual_import} artifacts={residual_art} script={residual_script}")
    return report


def _default_brew_prefix() -> Path:
    try:
        r = subprocess.run(["brew", "--prefix"], capture_output=True, text=True, check=True)
        return Path(r.stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        return Path("/opt/homebrew")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Surgically remove a Homebrew editable install of a distribution.")
    p.add_argument("--brew-prefix", type=Path, default=None)
    p.add_argument("--dist-name", required=True)
    p.add_argument("--console-script", required=True)
    p.add_argument("--bin-dir", type=Path, default=None)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    brew_prefix = args.brew_prefix or _default_brew_prefix()
    bin_dir = args.bin_dir or (brew_prefix / "bin")
    if not find_brew_editable(brew_prefix, args.dist_name) and not find_stray_console_script(
            bin_dir, args.console_script, brew_prefix):
        print(f"clean-homebrew: no Homebrew editable install of {args.dist_name} — nothing to do")
        return 0
    try:
        report = clean(brew_prefix, args.dist_name, args.console_script,
                       bin_dir=args.bin_dir, dry_run=args.dry_run)
    except CleanupError as e:
        print(f"clean-homebrew: FAILED — {e}", file=sys.stderr)
        return 1
    print(f"clean-homebrew: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
