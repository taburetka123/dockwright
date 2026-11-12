from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from . import config, paths

HOME_EDIT_DENY = "Edit(~/**)"


def _split_roots(csv: str) -> list[str]:
    return [os.path.expanduser(p.strip()) for p in csv.split(",") if p.strip()]


def _dedupe_nested(dirs: list[str]) -> list[str]:
    out: list[str] = []
    for d in sorted({d.rstrip(os.sep) or os.sep for d in dirs}):
        if not any(d == kept or d.startswith(kept + os.sep) for kept in out):
            out.append(d)
    return out


def headless_additional_dirs() -> list[str]:
    dirs = _split_roots(config.repo_roots()) + _split_roots(config.worktree_roots())
    dirs.append(os.path.expanduser(str(config.worker_home_default())))
    return _dedupe_nested(dirs)


def finalize_headless_settings(path: Path) -> bool:
    data = json.loads(path.read_text())
    perms = data.setdefault("permissions", {})
    if "additionalDirectories" in perms:
        return False
    perms["additionalDirectories"] = headless_additional_dirs()
    path.write_text(json.dumps(data, indent=2) + "\n")
    return True


def add_exports_dir(path: Path) -> bool:
    data = json.loads(path.read_text())
    dirs = data.setdefault("permissions", {}).setdefault("additionalDirectories", [])
    exports = str(paths.EXPORTS)
    if exports in dirs:
        return False
    dirs.append(exports)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return True


def _under_home(path: Path) -> bool:
    home = os.path.realpath(os.path.expanduser("~"))
    real = os.path.realpath(path)
    return real == home or real.startswith(home + os.sep)


def add_topic_folder_rules(path: Path) -> bool:
    data = json.loads(path.read_text())
    perms = data.setdefault("permissions", {})
    wanted = {"allow": [f"Edit(/{paths.EXPORTS / 'topic'}/**)"],
              "deny": [] if _under_home(paths.EXPORTS) else [HOME_EDIT_DENY]}
    added = False
    for key, rules in wanted.items():
        listed = perms.setdefault(key, [])
        for rule in rules:
            if rule not in listed:
                listed.append(rule)
                added = True
    if added:
        path.write_text(json.dumps(data, indent=2) + "\n")
    return added


def _read_json_object(path: Path, source: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as err:
        raise ValueError(f"{source}: cannot read {path}: {err}") from err
    if not isinstance(data, dict):
        raise ValueError(f"{source}: {path} is not a JSON object")
    return data


def derive_preset(source: Path, base_dir: Path, out: Path) -> None:
    from .pipeline import merge_settings
    data = _read_json_object(source, source)
    name = data.pop("extends", None)
    if not isinstance(name, str) or not name or os.sep in name or "/" in name or name in (".", ".."):
        raise ValueError(f"{source}: extends must name a preset file in {base_dir}, got {name!r}")
    base_path = base_dir / name
    if not base_path.is_file():
        raise ValueError(f"{source}: extends names {name}, which is not a file in {base_dir}")
    base = _read_json_object(base_path, source)
    if "extends" in base:
        raise ValueError(f"{source}: its base {base_path} has extends itself")
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=f".{out.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(merge_settings(base, data), indent=2) + "\n")
        os.replace(tmp, out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="dockwright finalize-presets",
        description="Inject operator-absolute permissions.additionalDirectories "
                    "and the artifact export folder into a deployed settings preset.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--file", type=Path, help="settings preset to finalize")
    target.add_argument("--derive", type=Path,
                        help="derived-preset source whose extends names a preset in --base-dir")
    parser.add_argument("--base-dir", type=Path, help="directory of the preset a --derive source extends")
    parser.add_argument("--out", type=Path, help="where --derive writes the resolved preset")
    parser.add_argument("--exports-only", action="store_true", help="add only the artifact export folder")
    parser.add_argument("--topic-folder", action="store_true",
                        help="allow edits only in the topic agents' export folder")
    args = parser.parse_args(argv)
    if args.derive is not None:
        if args.base_dir is None or args.out is None:
            parser.error("--derive needs --base-dir and --out")
        try:
            derive_preset(args.derive, args.base_dir, args.out)
        except ValueError as err:
            print(f"finalize-presets: {err}", file=sys.stderr)
            return 1
        print(f"finalize-presets: derived {args.out} from {args.derive}")
        return 0
    if not args.file.is_file():
        print(f"finalize-presets: no such file: {args.file}", file=sys.stderr)
        return 1
    if not args.exports_only:
        if finalize_headless_settings(args.file):
            print(f"finalize-presets: injected additionalDirectories into {args.file}")
        else:
            print(f"finalize-presets: operator additionalDirectories present, "
                  f"left untouched: {args.file}")
    if add_exports_dir(args.file):
        print(f"finalize-presets: added the export folder {paths.EXPORTS} to {args.file}")
    else:
        print(f"finalize-presets: {args.file} already lists the export folder")
    if args.topic_folder:
        if add_topic_folder_rules(args.file):
            print(f"finalize-presets: limited edits to {paths.EXPORTS / 'topic'} in {args.file}")
        else:
            print(f"finalize-presets: {args.file} already limits edits to the topic folder")
    return 0
