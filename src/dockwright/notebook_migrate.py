import argparse
import difflib
import hashlib
import re
import sys
from collections import Counter
from pathlib import Path

from . import mcp_server as ms
from . import paths, state, topic
from .sections import Section, split

WRITER = "cli:notebook-migrate"
KEY_MAX = 64


def keys_for(domain: str, sections: list[Section]) -> list[str]:
    bases = [_fit(f"note-{domain}-{_slug(s.title)}") for s in sections]
    shared = {base for base, n in Counter(bases).items() if n > 1}
    seen: Counter = Counter()
    keys = []
    for section, base in zip(sections, bases):
        if base not in shared:
            keys.append(base)
            continue
        suffix = f"-{_sha(section.title, 6)}"
        seen[base + suffix] += 1
        if seen[base + suffix] > 1:
            suffix += f"-{seen[base + suffix]}"
        keys.append(_fit(base, suffix))
    if len(set(keys)) != len(keys):
        raise ValueError(f"two sections map to one topic key: {sorted(k for k, n in Counter(keys).items() if n > 1)}")
    return keys


def _slug(title: str) -> str:
    words = re.findall(r"[a-z0-9]+", title.lower())[:4]
    return "-".join(words) if words else f"h{_sha(title, 8)}"


def _sha(text: str, n: int) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:n]


def _fit(base: str, suffix: str = "") -> str:
    return base[:KEY_MAX - len(suffix)].rstrip("-") + suffix


def notebook_dir() -> Path:
    return paths.ROOT / "notebook"


def notebook_path(domain: str) -> Path:
    return notebook_dir() / f"{domain}.md"


def read_notebook(source: Path) -> str:
    try:
        return source.read_text()
    except (OSError, UnicodeDecodeError) as err:
        raise ValueError(f"cannot read {source}: {err}") from err


def pending(domain: str) -> int | None:
    source = notebook_path(domain)
    if not source.is_file():
        return None
    header, sections = split(read_notebook(source))
    fleet = topic.fleet_key(domain)
    marker = (topic.read(fleet) or {}).get("decisions_marker")
    registry = _registry(sections, marker) if marker else None
    if marker and registry is None:
        raise ValueError(f"no section heading holds {marker!r} stored in {fleet}; pass --decisions")
    counts, closed, decided, fleet_writes = _writes(domain, header, list(zip(keys_for(domain, sections), sections)),
                                                    None, registry, True)
    return counts["new"] + counts["updated"] + closed + (decided == "written") + fleet_writes


def _registry(sections: list[Section], marker: str) -> str | None:
    picked = [s.text for s in sections if marker in s.heading]
    return "".join(picked) if picked else None


def migrate(domain: str, marker: str | None = None, dry_run: bool = False) -> int:
    if not topic.is_key(domain):
        raise ValueError(f"domain {domain!r} is not a slug; use only [A-Za-z0-9_-]")
    source = notebook_path(domain)
    if not source.is_file():
        raise ValueError(f"no notebook at {source}")
    text = read_notebook(source)
    header, sections = split(text)
    notes = list(zip(keys_for(domain, sections), sections))
    fleet = topic.fleet_key(domain)
    active = (topic.read(fleet) or {}).get("decisions_marker") if marker is None else marker
    registry = _registry(sections, active) if active else None
    if active and registry is None:
        origin = f" stored in {fleet}" if marker is None else ""
        print(f"notebook-migrate {domain}: no section heading holds {active!r}{origin}; nothing written. Pass "
              "--decisions <marker> to re-point the registry, or --decisions '' to stop refreshing it (it stays, and "
              "node footers keep naming it).",
              file=sys.stderr)
        return 1
    counts, closed, decided, _ = _writes(domain, header, notes, marker, registry, dry_run)
    joined = header + "".join(s.text for _, s in notes) if dry_run else _stored(domain, notes)
    diff = list(difflib.unified_diff(text.splitlines(keepends=True), joined.splitlines(keepends=True),
                                     fromfile=str(source), tofile="store"))
    print(f"notebook-migrate {domain}{' (dry run)' if dry_run else ''}: {len(notes)} notes (new {counts['new']}, "
          f"updated {counts['updated']}, unchanged {counts['unchanged']}, closed {closed}); decisions: {decided}; "
          f"diff: {'clean' if not diff else f'{len(diff)} lines'}")
    sys.stdout.writelines(diff)
    return 1 if diff else 0


def _writes(domain: str, header: str, notes: list[tuple[str, Section]], marker: str | None, registry: str | None,
            dry_run: bool) -> tuple[Counter, int, str, int]:
    counts: Counter = Counter()
    for key, section in notes:
        body = _put(key, "context", "ask", section.text, dry_run)
        card = _card(key, lambda current, s=section: _note_card(current, domain, s), dry_run)
        counts[body if body != "unchanged" else ("updated" if card else "unchanged")] += 1
    closed = _close_missing(domain, {key for key, _ in notes}, dry_run)
    decided, fleet_writes = _fleet(domain, header, marker, registry, dry_run)
    return counts, closed, decided, fleet_writes


def _stored_body(path) -> str | None:
    try:
        return state.parse_artifact(path.read_text())[1]
    except (OSError, ValueError):
        return None


def _put(task_key: str, phase: str, name: str, content: str, dry_run: bool) -> str:
    path = paths.artifact_path(task_key, phase, name)
    exists = path.is_file()
    if exists and _stored_body(path) == content:
        return "unchanged"
    if not dry_run:
        ms.artifact_put_impl(task_key, phase, name, content, "complete", WRITER, overwrite=exists, scope="output")
    return "updated" if exists else "new"


def _card(task_key: str, change, dry_run: bool) -> bool:
    if dry_run:
        return change(topic.read(task_key)) is not None
    changed = [False]

    def apply(current):
        new = change(current)
        changed[0] = new is not None
        return new

    topic.update(task_key, apply)
    return changed[0]


def _note_card(current, domain: str, section: Section) -> dict | None:
    title, status = topic._one_line(section.title), "closed" if section.closed else "open"
    if current is None:
        return topic.new_card("note", domain, title, status=status)
    if current.get("title") == title and current.get("status") == status:
        return None
    return {**current, "title": title, "status": status}


def _closing(current, domain: str) -> dict | None:
    if (not current or current.get("kind") != "note" or current.get("domain") != domain
            or current.get("status") == "closed"):
        return None
    return {**current, "status": "closed"}


def _close_missing(domain: str, keys: set[str], dry_run: bool) -> int:
    if not paths.ARTIFACTS.is_dir():
        return 0
    prefix = f"note-{domain}-"
    closed = 0
    for d in sorted(paths.ARTIFACTS.iterdir()):
        if d.is_dir() and d.name.startswith(prefix) and d.name not in keys:
            closed += _card(d.name, lambda current: _closing(current, domain), dry_run)
    return closed


def _fleet(domain: str, header: str, marker: str | None, registry: str | None, dry_run: bool) -> tuple[str, int]:
    fleet = topic.fleet_key(domain)

    def change(current):
        new = current or topic.new_card("fleet", domain, topic.title_of(header) or f"{domain} notebook")
        if marker:
            new = {**new, "decisions_marker": marker}
        elif marker == "":
            new = {k: v for k, v in new.items() if k != "decisions_marker"}
        return None if new == current else new

    writes = _card(fleet, change, dry_run) + (_put(fleet, "context", "ask", header, dry_run) != "unchanged")
    if registry is None:
        return "-", writes
    unchanged = _put(fleet, topic.DECISIONS_PHASE, domain, registry, dry_run) == "unchanged"
    return "unchanged" if unchanged else "written", writes


def _stored(domain: str, notes: list[tuple[str, Section]]) -> str:
    keys = [topic.fleet_key(domain), *(key for key, _ in notes)]
    return "".join(ms.artifact_get_impl(key, "context", "ask")["content"] for key in keys)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dockwright notebook-migrate",
                                     description="Split notebook/<domain>.md into note topics. The file stays.")
    parser.add_argument("domain", help="The notebook's domain: <state_root>/notebook/<domain>.md.")
    parser.add_argument("--decisions", metavar="MARKER",
                        help="Also join the sections whose heading holds MARKER into decisions.<domain> under "
                             "fleet-<domain>; later runs reuse the marker.")
    parser.add_argument("--dry-run", action="store_true", help="Split and compare in memory; write nothing.")
    args = parser.parse_args(argv)
    try:
        return migrate(args.domain, args.decisions, args.dry_run)
    except ValueError as err:
        print(f"notebook-migrate: {err}", file=sys.stderr)
        return 2
