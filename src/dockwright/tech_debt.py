import argparse
import codecs
import hashlib
import json
import os
import re
import stat
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import config, paths
from .sections import Section, split

KINDS = {
    "defect": "A system we change in code behaves wrongly: observed, or suspected with a named mechanism.",
    "trap": ("A measured behaviour or method error that misleads a session and that we do not change in code: "
             "a vendor's, the host's, ours by design, or our own way of reasoning. The cure is knowing it, or a rule."),
    "debt": ("The system behaves correctly, but its shape costs: duplication, a missing test or guard, "
             "stale prose, leftover review findings, cleanup."),
    "idea": "A proposal or an unbuilt capability, with no defect behind it.",
}
UNTAGGED_HINT = "dockwright tech-debt --untagged"
FENCED = "inside a code fence (repair the fence, or indent a quoted heading)"
LOOSE = 'not a "## " entry heading (make it "## ", or "### " inside an entry)'
_LOOSE = re.compile(r"^ {0,3}#{1,2} ")
_TAGS = re.compile(r"^- tags:[ \t]*(.*?)[ \t]*$")
_SPLIT = re.compile(r"[,\s]+")
_SYSTEM = re.compile(r"^[a-z0-9][a-z0-9-]*$")
ATTEMPTS = 3
CHECK_STATUSES = ("ok", "failed", "refused", "timeout", "error", "none")


@dataclass(frozen=True)
class Entry:
    path: Path
    line: int
    title: str
    tags: tuple[str, ...] | None
    reason: str | None
    text: str = field(default="", repr=False, compare=False)
    key: str = ""


def store_dir() -> Path:
    return paths.ROOT / "tech-debt"


def seen_path() -> Path:
    return store_dir() / ".seen.json"


def entry_key(domain: str, heading: str, repeat: int) -> str:
    return f"{domain}\t{heading}" if repeat < 2 else f"{domain}\t{heading}\t{repeat}"


def digest(text: str) -> str:
    return hashlib.sha256(text.rstrip().encode()).hexdigest()[:16]


def _keyed(domain: str, sections: list[Section]) -> list[str]:
    repeats: Counter[str] = Counter()
    keys = []
    for section in sections:
        repeats[section.heading] += 1
        keys.append(entry_key(domain, section.heading, repeats[section.heading]))
    return keys


def systems() -> tuple[dict[str, str], list[str]]:
    valid: dict[str, str] = {}
    rejected: list[str] = []
    for name, definition in config.tech_debt_systems().items():
        if _SYSTEM.match(name) and name not in KINDS:
            valid[name] = definition
        else:
            rejected.append(name)
    return valid, rejected


def tags_of(section_text: str) -> tuple[str, ...] | None:
    lines = section_text.split("\n")
    m = _TAGS.match(lines[1].rstrip("\r")) if len(lines) > 1 else None
    return tuple(tag for tag in _SPLIT.split(m.group(1)) if tag) if m else None


def reason(tags: tuple[str, ...] | None, system_names: dict[str, str]) -> str | None:
    if tags is None:
        return "no tags line"
    for tag in tags:
        if tag not in KINDS and tag not in system_names:
            return f"unknown tag: {tag}"
    if not any(tag in KINDS for tag in tags):
        return "no kind"
    if system_names and not any(tag in system_names for tag in tags):
        return "no system"
    return None


def domains(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return [p.stem for p in sorted(root.glob("*.md")) if p.is_file() and not p.name.startswith(".")]


def _starts(header: str, sections: list[Section]) -> list[int]:
    line = 1 + header.count("\n")
    starts = []
    for section in sections:
        starts.append(line)
        line += section.text.count("\n")
    return starts


def _loose(lines: list[str], starts: set[int]) -> list[int]:
    marked = [f"## {raw}" if _LOOSE.match(raw) and not raw.startswith("## ") else raw for raw in lines]
    extra = [line for line in _starts(*split("\n".join(marked))) if line not in starts]
    return extra[1:] if extra and extra[0] < min(starts, default=extra[0] + 1) else extra


def entries(root: Path, only: str | None,
            system_names: dict[str, str]) -> tuple[list[Entry], list[Entry], int]:
    found: list[Entry] = []
    hidden: list[Entry] = []
    unreadable = 0
    for domain in domains(root):
        if only is not None and domain != only:
            continue
        path = root / f"{domain}.md"
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as err:
            print(f"cannot read {path}: {err}", file=sys.stderr)
            unreadable += 1
            continue
        header, sections = split(text)
        starts = _starts(header, sections)
        for section, line, key in zip(sections, starts, _keyed(domain, sections)):
            tags = tags_of(section.text)
            found.append(Entry(path, line, section.heading[3:], tags, reason(tags, system_names),
                               section.text, key))
        lines = text.split("\n")
        known = set(starts)
        hidden += [Entry(path, number, raw[3:], None, FENCED)
                   for number, raw in enumerate(lines, 1)
                   if raw.startswith("## ") and number not in known]
        hidden += [Entry(path, number, lines[number - 1].strip(), None, LOOSE)
                   for number in _loose(lines, known)]
    return found, hidden, unreadable


def _vocabulary() -> int:
    system_names, rejected = systems()
    found, _, _ = entries(store_dir(), None, system_names)
    counts = Counter(tag for entry in found for tag in set(entry.tags or ()))
    need = ("every kind its items have and every system it concerns, at least one of each" if system_names
            else "every kind its items have, at least one")
    print(f"An entry's second line, right under its `## ` heading, is `- tags: <tag>, <tag>`: {need}.")
    print("kinds:")
    for name, definition in KINDS.items():
        print(f"  {name}  {counts[name]}  {definition}")
    if system_names:
        print("systems:")
    elif config.load_error():
        print(f"systems: none ({config.config_path()} does not parse: {config.load_error()})")
    else:
        print("systems: none ([tech_debt.systems] in dockwright.toml is empty or has no string values)")
    for name, definition in system_names.items():
        print(f"  {name}  {counts[name]}  {definition}")
    outside = sorted(tag for tag in counts if tag not in KINDS and tag not in system_names)
    if outside:
        print("outside the vocabulary:")
        for tag in outside:
            print(f"  {tag}  {counts[tag]}")
    if rejected:
        print("rejected names in [tech_debt.systems] (not [a-z0-9-], or a kind's name):")
        for name in rejected:
            print(f"  {name}")
    return 0


def _q(key: str) -> str:
    return json.dumps(key, ensure_ascii=False)


def load_seen(path: Path) -> dict[str, str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as err:
        raise ValueError(f"cannot read {path}: {err}") from err
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        raise ValueError(f"{path} is not JSON: {err}") from err
    seen = data.get("seen") if isinstance(data, dict) and data.get("version") == 1 else None
    if not isinstance(seen, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in seen.items()):
        raise ValueError(f'{path} is not {{"version": 1, "seen": {{<key>: <digest>}}}}')
    return seen


def record_seen(rows: list[dict], path: Path) -> int:
    seen = load_seen(path)
    seen.update((row["key"], row["digest"]) for row in rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps({"version": 1, "seen": seen}, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)
    return len(rows)


def check_runs(entry: Entry) -> list[dict]:
    from . import note_check
    return [note_check.run_segment(segment, None) for segment in note_check.commands_of(entry.text)]


def _check_status(runs: list[dict]) -> str:
    return next((run["status"] for run in runs if run["status"] != "ok"), "ok") if runs else "none"


def _row(entry: Entry, runs: list[dict] | None = None) -> dict:
    row = {"key": entry.key, "path": str(entry.path), "line": entry.line, "title": entry.title,
           "tags": None if entry.tags is None else list(entry.tags), "digest": digest(entry.text)}
    return row if runs is None else {**row, "check": runs}


def _print_json(rows: list[dict]) -> None:
    print(json.dumps(rows, ensure_ascii=False, indent=1))


def _checked(shown: list[Entry], as_json: bool) -> int:
    if as_json:
        _print_json([_row(entry, check_runs(entry)) for entry in shown])
        return 0
    counts: Counter[str] = Counter()
    for entry in shown:
        runs = check_runs(entry)
        status = _check_status(runs)
        counts[status] += 1
        print(f"{entry.path}:{entry.line}  {status}  {entry.title}")
        for run in runs:
            print(f"    {run['status']} {'-' if run['exit'] is None else run['exit']}  {run['cmd']}")
            for line in run["output"].splitlines():
                print(f"    | {line}")
    print(f"{len(shown)} checked · " + " · ".join(f"{counts[status]} {status}" for status in CHECK_STATUSES))
    return 0


def _rows(source: str, fields: tuple[str, ...]) -> list[dict]:
    try:
        rows = json.loads(Path(source).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ValueError(f"cannot read {source}: {err}") from err
    if not isinstance(rows, list) or not all(
            isinstance(row, dict) and all(isinstance(row.get(name), str) for name in fields) for row in rows):
        raise ValueError(f"{source} is not a JSON array of objects with a string "
                         + " and a string ".join(fields))
    return rows


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8-sig")


def _replace(src: Path, dst: Path) -> None:
    os.replace(src, dst)


def _file_away(dest: Path, section: Section, why: str, today: str) -> None:
    body = section.text.split("\n", 1)[1].rstrip() if "\n" in section.text else ""
    block = f"## [x] {section.title}\n" + (f"{body}\n" if body else "") + f"- resolved: {today} — {why}\n\n"
    dest.parent.mkdir(exist_ok=True)
    with open(dest, "a+b") as out:
        out.seek(0)
        held = out.read()
        if held.startswith(block.encode()) or f"\n{block}".encode() in held:
            return
        gap = b"" if not held or held.endswith(b"\n\n") else b"\n" if held.endswith(b"\n") else b"\n\n"
        out.write(gap + block.encode())


def _archive_file(path: Path, dest: Path, wanted: dict[str, str], today: str) -> tuple[int, bool]:
    domain, wanted, filed, left = path.stem, dict(wanted), set(), False
    held = open(path, "rb")
    try:
        for _ in range(ATTEMPTS):
            orig = held.read()
            text = orig.decode("utf-8-sig")
            header, sections = split(text)
            keys = _keyed(domain, sections)
            for key in [key for key in wanted if key not in keys]:
                if key in filed:
                    print(f"{_q(key)} left {path} after this call filed it to {dest}: "
                          "check whether it lives on under another heading", file=sys.stderr)
                    left = True
                else:
                    print(f"already gone from {path}: {_q(key)}")
                del wanted[key]
            targeted = [(key, section) for key, section in zip(keys, sections) if key in wanted]
            if not targeted:
                return 0, not left
            for key, section in targeted:
                _file_away(dest, section, wanted[key], today)
                filed.add(key)
            if _read(path) != text:
                held.close()
                held = open(path, "rb")
                continue
            bom = codecs.BOM_UTF8 if orig.startswith(codecs.BOM_UTF8) else b""
            rebuilt = header + "".join(section.text for key, section in zip(keys, sections) if key not in wanted)
            tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
            tmp.write_bytes(bom + rebuilt.encode())
            os.chmod(tmp, stat.S_IMODE(os.fstat(held.fileno()).st_mode))
            _replace(tmp, path)
            held.seek(0)
            after = held.read()
            if len(after) > len(orig) and after.startswith(orig):
                with open(path, "ab") as out:
                    out.write(after[len(orig):])
            starts = dict(zip(keys, _starts(header, sections)))
            for key, section in targeted:
                print(f"archived {path}:{starts[key]}  {section.heading[3:]}")
            return len(targeted), not left
        for key, section in targeted:
            print(f"{path} kept changing: {section.heading[3:]} is in both it and {dest} (key {_q(key)})",
                  file=sys.stderr)
        return 0, False
    finally:
        held.close()


def archive(root: Path, targets: list[tuple[str, str]], today: str) -> int:
    system_names, _ = systems()
    found, hidden, _ = entries(root, None, system_names)
    by_key = {entry.key: entry for entry in found}
    errors = []
    for i, (key, why) in enumerate(targets):
        entry = by_key.get(key)
        if entry is None:
            errors.append(f"no entry has the key {_q(key)}")
        else:
            end = min((other.line for other in found if other.path == entry.path and other.line > entry.line),
                      default=None)
            errors += [f"{inner.path}:{inner.line}  {inner.reason}  {inner.title} — inside {_q(key)}, "
                       "which would move it too" for inner in hidden
                       if inner.path == entry.path and entry.line < inner.line and (end is None or inner.line < end)]
        if not why.strip() or "\n" in why or "\r" in why:
            errors.append(f"the reason for {_q(key)} is not one non-empty line")
        if any(key == other for other, _ in targets[:i]):
            errors.append(f"the key {_q(key)} repeats")
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 2
    wanted: dict[Path, dict[str, str]] = {}
    for key, why in targets:
        wanted.setdefault(by_key[key].path, {})[key] = why
    moved, code = 0, 0
    for path in sorted(wanted):
        count, ok = _archive_file(path, root / "archive" / path.name, wanted[path], today)
        moved += count
        code = code if ok else 1
    print(f"archived {moved} entries to {root / 'archive'}")
    return code


def _record(listing: str) -> int:
    try:
        count = record_seen(_rows(listing, ("key", "digest")), seen_path())
    except ValueError as err:
        print(err, file=sys.stderr)
        return 2
    print(f"recorded {count} entries as seen")
    return 0


def _archive_targets(source: str) -> int:
    try:
        rows = _rows(source, ("key", "reason"))
    except ValueError as err:
        print(err, file=sys.stderr)
        return 2
    return archive(store_dir(), [(row["key"], row["reason"]) for row in rows], date.today().isoformat())


def main(argv: list[str]) -> int:
    if argv[:1] == ["tags"]:
        if len(argv) > 1:
            print("usage: dockwright tech-debt tags", file=sys.stderr)
            return 2
        return _vocabulary()
    parser = argparse.ArgumentParser(prog="dockwright tech-debt")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--tag", action="append", default=[])
    group.add_argument("--untagged", action="store_true")
    parser.add_argument("--domain")
    parser.add_argument("--new", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--seen", metavar="FILE")
    parser.add_argument("--archive", metavar="FILE")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    alone = [flag for flag, value in (("--seen", args.seen), ("--archive", args.archive)) if value is not None]
    if alone and (len(alone) > 1 or args.tag or args.untagged or args.domain is not None
                  or args.new or args.check or args.json):
        print(f"{alone[0]} stands alone: it takes no other flag", file=sys.stderr)
        return 2
    if args.untagged and (args.new or args.check or args.json):
        print("--untagged takes only --domain", file=sys.stderr)
        return 2
    if args.seen is not None:
        return _record(args.seen)
    if args.archive is not None:
        return _archive_targets(args.archive)
    system_names, _ = systems()
    root = store_dir()
    known = domains(root)
    if args.domain is not None and args.domain not in known:
        print(f"unknown domain: {args.domain} (known: {', '.join(known) or 'none'})", file=sys.stderr)
        return 2
    vocabulary = [*KINDS, *system_names]
    for tag in args.tag:
        if tag not in vocabulary:
            print(f"unknown tag: {tag} (known: {', '.join(vocabulary)})", file=sys.stderr)
            return 2
    found, hidden, unreadable = entries(root, args.domain, system_names)
    untagged = [entry for entry in found if entry.reason is not None]
    suffix = ((f" · {len(hidden)} hidden" if hidden else "")
              + (f" · {unreadable} unreadable" if unreadable else ""))
    if args.untagged:
        for entry in sorted(untagged + hidden, key=lambda entry: (entry.path, entry.line)):
            print(f"{entry.path}:{entry.line}  {entry.reason}  {entry.title}")
        print(f"{len(untagged)} untagged of {len(found)}{suffix}")
        return 1 if untagged or hidden or unreadable else 0
    wanted = set(args.tag)
    shown = [entry for entry in found
             if not wanted or (entry.reason is None and wanted <= set(entry.tags or ()))]
    if args.new:
        try:
            seen = load_seen(seen_path())
        except ValueError as err:
            print(err, file=sys.stderr)
            return 2
        shown = [entry for entry in shown if seen.get(entry.key) != digest(entry.text)]
    if args.check:
        return _checked(shown, args.json)
    if args.json:
        _print_json([_row(entry) for entry in shown])
        return 0
    for entry in shown:
        label = " ".join(entry.tags or ()) if entry.reason is None else "untagged"
        print(f"{entry.path}:{entry.line}  {label}  {entry.title}")
    hint = f" — {UNTAGGED_HINT}" if untagged or hidden else ""
    print(f"{len(shown)} of {len(found)} entries · {len(untagged)} untagged{suffix}{hint}")
    return 0
