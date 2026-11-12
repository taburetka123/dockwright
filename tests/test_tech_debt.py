import json
from datetime import date

import pytest

from dockwright import config, paths, tech_debt

SYSTEMS_TOML = '[tech_debt.systems]\nengine = "the engine"\ndocs = "the docs"\n'

GENERAL = "\n".join([
    "# Tech debt — general",
    "",
    "Intro.",
    "",
    "## [ ] Engine drops a row",
    "- tags: defect, engine",
    "",
    "Body.",
    "~~~",
    "## Not a heading inside a fence",
    "~~~",
    "",
    "## Docs are stale",
    "- tags: debt docs",
    "",
    "## No line at all",
    "",
    "Body without tags.",
    "",
    "## Bold field",
    "- **tags:** defect, engine",
    "",
    "## Tags below the second line",
    "Body first.",
    "- tags: defect, engine",
    "",
    "## Unknown tag",
    "- tags: defect, engine, mystery",
    "",
    "## Only a system",
    "- tags: engine",
    "",
    "## Only a kind",
    "- tags: idea",
    "",
])

META = "## Meta defect in the docs\n- tags: defect docs\n"


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "dockwright"
    debt = root / "tech-debt"
    (debt / "archive").mkdir(parents=True)
    (debt / ".snapshot").mkdir()
    (debt / "general.md").write_text(GENERAL)
    (debt / "meta.md").write_text(META)
    (debt / "archive" / "general.md").write_text("## [x] Archived\n- tags: defect, engine\n")
    (debt / ".snapshot" / "general.md").write_text("## Hidden copy\n- tags: defect, engine\n")
    (debt / ".dot.md").write_text("## Dot file\n- tags: defect, engine\n")
    (debt / "notes.txt").write_text("## Not markdown\n")
    monkeypatch.setattr(paths, "ROOT", root)
    toml = tmp_path / "dockwright.toml"
    toml.write_text(SYSTEMS_TOML)
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(toml))
    return debt


def _line(text, heading):
    return text.split("\n").index(heading) + 1


def _run(capsys, *argv):
    code = tech_debt.main(list(argv))
    out, err = capsys.readouterr()
    return code, out.splitlines(), err


def _titles(lines):
    return [line.split("  ")[-1] for line in lines[:-1]]


def test_lists_every_entry_in_file_order_with_its_line(store, capsys):
    general, meta = store / "general.md", store / "meta.md"
    code, lines, _ = _run(capsys)
    assert code == 0
    assert lines == [
        f"{general}:{_line(GENERAL, '## [ ] Engine drops a row')}  defect engine  [ ] Engine drops a row",
        f"{general}:{_line(GENERAL, '## Docs are stale')}  debt docs  Docs are stale",
        f"{general}:{_line(GENERAL, '## No line at all')}  untagged  No line at all",
        f"{general}:{_line(GENERAL, '## Bold field')}  untagged  Bold field",
        f"{general}:{_line(GENERAL, '## Tags below the second line')}  untagged  Tags below the second line",
        f"{general}:{_line(GENERAL, '## Unknown tag')}  untagged  Unknown tag",
        f"{general}:{_line(GENERAL, '## Only a system')}  untagged  Only a system",
        f"{general}:{_line(GENERAL, '## Only a kind')}  untagged  Only a kind",
        f"{meta}:1  defect docs  Meta defect in the docs",
        "9 of 9 entries · 6 untagged · 1 hidden — dockwright tech-debt --untagged",
    ]


def test_several_tags_intersect_and_untagged_entries_never_match(store, capsys):
    code, lines, _ = _run(capsys, "--tag", "defect", "--tag", "engine")
    assert code == 0
    assert _titles(lines) == ["[ ] Engine drops a row"]
    assert lines[-1] == "1 of 9 entries · 6 untagged · 1 hidden — dockwright tech-debt --untagged"
    _, lines, _ = _run(capsys, "--tag", "defect")
    assert _titles(lines) == ["[ ] Engine drops a row", "Meta defect in the docs"]


def test_domain_limits_the_list_and_the_counts(store, capsys):
    code, lines, _ = _run(capsys, "--domain", "meta")
    assert code == 0
    assert lines == [f"{store / 'meta.md'}:1  defect docs  Meta defect in the docs", "1 of 1 entries · 0 untagged"]


def test_untagged_names_the_first_reason_and_exits_1(store, capsys):
    general = store / "general.md"
    code, lines, _ = _run(capsys, "--untagged")
    assert code == 1
    assert lines == [
        f"{general}:{_line(GENERAL, '## Not a heading inside a fence')}  inside a code fence (repair the fence, or indent a quoted heading)  Not a heading inside a fence",
        f"{general}:{_line(GENERAL, '## No line at all')}  no tags line  No line at all",
        f"{general}:{_line(GENERAL, '## Bold field')}  no tags line  Bold field",
        f"{general}:{_line(GENERAL, '## Tags below the second line')}  no tags line  Tags below the second line",
        f"{general}:{_line(GENERAL, '## Unknown tag')}  unknown tag: mystery  Unknown tag",
        f"{general}:{_line(GENERAL, '## Only a system')}  no kind  Only a system",
        f"{general}:{_line(GENERAL, '## Only a kind')}  no system  Only a kind",
        "6 untagged of 9 · 1 hidden",
    ]


def test_untagged_exits_0_when_every_entry_is_tagged(store, capsys):
    assert _run(capsys, "--untagged", "--domain", "meta") == (0, ["0 untagged of 1"], "")


def test_without_systems_a_kind_alone_tags_an_entry(store, capsys, tmp_path, monkeypatch):
    empty = tmp_path / "empty.toml"
    empty.write_text("")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(empty))
    _, lines, _ = _run(capsys, "--untagged")
    reasons = {line.split("  ")[-1]: line.split("  ")[1] for line in lines[:-1]}
    assert "Only a kind" not in reasons
    assert reasons["Only a system"] == "unknown tag: engine"
    assert reasons["[ ] Engine drops a row"] == "unknown tag: engine"


@pytest.mark.parametrize("argv,needle", [
    (["--tag", "mystery"], "unknown tag: mystery (known: defect, trap, debt, idea, engine, docs)"),
    (["--tag", "Defect"], "unknown tag: Defect"),
    (["--domain", "nope"], "unknown domain: nope (known: general, meta)"),
    (["--untagged", "--tag", "defect"], "not allowed with argument"),
    (["--bogus"], "unrecognized arguments"),
    (["tags", "extra"], "usage: dockwright tech-debt tags"),
])
def test_bad_arguments_exit_2(store, capsys, argv, needle):
    code, lines, err = _run(capsys, *argv)
    assert code == 2
    assert lines == []
    assert needle in err


def test_missing_store_prints_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(paths, "ROOT", tmp_path / "absent")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(tmp_path / "none.toml"))
    assert _run(capsys) == (0, ["0 of 0 entries · 0 untagged"], "")
    assert _run(capsys, "--untagged") == (0, ["0 untagged of 0"], "")


def test_an_unreadable_file_is_named_and_counted(store, capsys):
    (store / "broken.md").write_bytes(b"## Bad \xff bytes\n")
    code, lines, err = _run(capsys)
    assert code == 0
    assert "broken.md" in err
    assert lines[-1] == "9 of 9 entries · 6 untagged · 1 hidden · 1 unreadable — dockwright tech-debt --untagged"


def test_untagged_exits_1_while_a_file_is_unreadable(store, capsys):
    (store / "broken.md").write_bytes(b"## Bad \xff bytes\n")
    code, lines, _ = _run(capsys, "--untagged", "--domain", "meta")
    assert (code, lines) == (0, ["0 untagged of 1"])
    code, lines, _ = _run(capsys, "--untagged", "--domain", "broken")
    assert (code, lines) == (1, ["0 untagged of 0 · 1 unreadable"])


def test_a_heading_the_splitter_reads_as_fenced_is_counted_and_listed(store, capsys):
    path = store / "fenced.md"
    path.write_text("\n".join([
        "## A",
        "- tags: defect, engine",
        "",
        "1. ```bash",
        "   echo hi",
        "   ```",
        "",
        "## B",
        "- tags: defect, engine",
        "",
        "## C",
        "- tags: debt",
        "",
    ]))
    code, lines, _ = _run(capsys, "--domain", "fenced")
    assert code == 0
    assert lines == [f"{path}:1  defect engine  A",
                     "1 of 1 entries · 0 untagged · 2 hidden — dockwright tech-debt --untagged"]
    code, lines, _ = _run(capsys, "--untagged", "--domain", "fenced")
    assert code == 1
    assert lines == [f"{path}:8  inside a code fence (repair the fence, or indent a quoted heading)  B",
                     f"{path}:11  inside a code fence (repair the fence, or indent a quoted heading)  C",
                     "0 untagged of 1 · 2 hidden"]


def test_a_heading_in_another_form_is_counted_and_listed(store, capsys):
    path = store / "loose.md"
    path.write_text("\n".join([
        "# Loose headings",
        "",
        "## A",
        "- tags: defect, engine",
        "",
        "# B",
        "- tags: idea, docs",
        "",
        "  ## C",
        "- tags: idea, docs",
        "",
        "### A subheading inside A",
        "```bash",
        "# a shell comment",
        "```",
        "",
    ]))
    code, lines, _ = _run(capsys, "--domain", "loose")
    assert code == 0
    assert lines == [f"{path}:3  defect engine  A",
                     "1 of 1 entries · 0 untagged · 2 hidden — dockwright tech-debt --untagged"]
    code, lines, _ = _run(capsys, "--untagged", "--domain", "loose")
    assert code == 1
    assert lines == [f'{path}:6  not a "## " entry heading (make it "## ", or "### " inside an entry)  # B',
                     f'{path}:9  not a "## " entry heading (make it "## ", or "### " inside an entry)  ## C',
                     "0 untagged of 1 · 2 hidden"]


def test_a_byte_order_mark_does_not_hide_the_first_entry(store, capsys):
    path = store / "bom.md"
    path.write_bytes("\ufeff## First\n- tags: idea, docs\n## Second\n- tags: idea, docs\n".encode())
    assert _run(capsys, "--domain", "bom") == (
        0, [f"{path}:1  idea docs  First", f"{path}:3  idea docs  Second", "2 of 2 entries · 0 untagged"], "")


def test_tags_prints_the_rule_the_counts_and_what_falls_outside(store, capsys, tmp_path):
    (tmp_path / "dockwright.toml").write_text(SYSTEMS_TOML + 'Bad_Name = "x"\ndefect = "y"\n')
    code, lines, _ = _run(capsys, "tags")
    assert code == 0
    assert lines[0] == ("An entry's second line, right under its `## ` heading, is `- tags: <tag>, <tag>`: "
                        "every kind its items have and every system it concerns, at least one of each.")
    assert f"  defect  3  {tech_debt.KINDS['defect']}" in lines
    assert f"  trap  0  {tech_debt.KINDS['trap']}" in lines
    assert "  engine  3  the engine" in lines
    assert "  docs  2  the docs" in lines
    assert lines[lines.index("outside the vocabulary:") + 1] == "  mystery  1"
    rejected = lines.index("rejected names in [tech_debt.systems] (not [a-z0-9-], or a kind's name):")
    assert lines[rejected + 1:] == ["  Bad_Name", "  defect"]


def test_tags_without_systems_asks_for_a_kind_only(store, capsys, tmp_path, monkeypatch):
    empty = tmp_path / "empty.toml"
    empty.write_text("")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(empty))
    _, lines, _ = _run(capsys, "tags")
    assert lines[0].endswith(": every kind its items have, at least one.")
    assert "systems: none ([tech_debt.systems] in dockwright.toml is empty or has no string values)" in lines


def test_tags_names_a_config_that_does_not_parse(store, capsys, tmp_path, monkeypatch):
    broken = tmp_path / "broken.toml"
    broken.write_text("[tech_debt.systems\n")
    monkeypatch.setenv(config.ENV_CONFIG_PATH, str(broken))
    _, lines, _ = _run(capsys, "tags")
    assert any(line.startswith(f"systems: none ({broken} does not parse: ") for line in lines)


ENGINE = "general\t## [ ] Engine drops a row"
STALE = "general\t## Docs are stale"
LATE = "\n## Late entry\n- tags: idea, engine\n"


def _listing(capsys, *argv):
    return json.loads("\n".join(_run(capsys, "--json", *argv)[1]))


def _file(tmp_path, name, rows):
    path = tmp_path / name
    path.write_text(json.dumps(rows))
    return str(path)


def _archive(capsys, tmp_path, *pairs):
    return _run(capsys, "--archive", _file(tmp_path, "targets.json", [{"key": k, "reason": r} for k, r in pairs]))


def test_new_lists_every_entry_without_a_seen_file(store, capsys):
    code, lines, _ = _run(capsys, "--new")
    assert code == 0 and lines[-1].startswith("9 of 9 entries")


def test_seen_hides_recorded_entries_and_a_changed_entry_returns(store, capsys, tmp_path):
    code, lines, _ = _run(capsys, "--seen", _file(tmp_path, "listing.json", _listing(capsys, "--new")))
    assert code == 0 and lines == ["recorded 9 entries as seen"]
    assert _run(capsys, "--new")[1] == ["0 of 9 entries · 6 untagged · 1 hidden — dockwright tech-debt --untagged"]
    (store / "meta.md").write_text(META + "- seen: 2026-10-20\n")
    assert _titles(_run(capsys, "--new")[1]) == ["Meta defect in the docs"]


def test_seen_records_the_digest_the_listing_carries(store, capsys, tmp_path):
    listing = _file(tmp_path, "listing.json", _listing(capsys, "--new", "--domain", "meta"))
    (store / "meta.md").write_text(META + "- seen: 2026-10-20\n" + LATE)
    _run(capsys, "--seen", listing)
    assert _titles(_run(capsys, "--new", "--domain", "meta")[1]) == ["Meta defect in the docs", "Late entry"]


def test_new_json_carries_key_path_line_title_tags_digest(store, capsys):
    assert _listing(capsys, "--new", "--domain", "meta") == [{
        "key": "meta\t## Meta defect in the docs", "path": str(store / "meta.md"), "line": 1,
        "title": "Meta defect in the docs", "tags": ["defect", "docs"], "digest": tech_debt.digest(META)}]


@pytest.mark.parametrize("seen", ["[1, 2]", "{not json", '{"version": 1, "seen": {"k": 3}}'])
def test_new_refuses_a_seen_file_it_cannot_read(store, capsys, seen):
    (store / ".seen.json").write_text(seen)
    code, _, err = _run(capsys, "--new")
    assert code == 2 and str(store / ".seen.json") in err


def test_check_runs_each_entry_and_names_each_result(store, capsys, tmp_path):
    flag = tmp_path / "flag"
    flag.write_text("x")
    (store / "ops.md").write_text(
        f"## Present\n- tags: defect, engine\n- check: `test -e {flag}`\n\n"
        f"## Absent\n- tags: defect, engine\n- check: `test -e {tmp_path / 'gone'}`\n\n"
        "## Refused\n- tags: defect, engine\n- check: `date`\n\n"
        "## Prose\n- tags: idea, engine\n- check: none — an idea\n\n"
        "## Bare\n- tags: idea, engine\n")
    code, lines, _ = _run(capsys, "--check", "--domain", "ops")
    heads = [line for line in lines if not line.startswith("    ")]
    assert code == 0
    assert [head.split("  ")[1] for head in heads[:-1]] == ["ok", "failed", "refused", "none", "none"]
    assert heads[-1] == "5 checked · 1 ok · 1 failed · 1 refused · 0 timeout · 0 error · 2 none"


def test_check_json_carries_the_runs(store, capsys, tmp_path):
    (store / "ops.md").write_text(f"## Present\n- tags: defect, engine\n- check: `test -e {tmp_path}`\n")
    assert _listing(capsys, "--check", "--domain", "ops")[0]["check"] == [
        {"cmd": f"test -e {tmp_path}", "status": "ok", "exit": 0, "output": ""}]


def test_archive_moves_the_block_flips_the_box_and_appends_resolved(store, capsys, tmp_path):
    quoted = "## Not a heading inside a fence"
    (store / "general.md").write_text(GENERAL.replace(quoted, f"    {quoted}"))
    code, _, _ = _archive(capsys, tmp_path, (ENGINE, "fixed by #451"))
    general = (store / "general.md").read_text()
    archived = (store / "archive" / "general.md").read_text()
    assert code == 0
    assert "## [ ] Engine drops a row" not in general and "Body." not in general and "## Docs are stale" in general
    assert archived.startswith("## [x] Archived\n")
    assert "## [x] Engine drops a row\n- tags: defect, engine\n" in archived
    assert archived.rstrip().endswith(f"- resolved: {date.today().isoformat()} — fixed by #451")


def test_archive_flips_a_heading_without_a_box(store, capsys, tmp_path):
    _archive(capsys, tmp_path, ("meta\t## Meta defect in the docs", "dead"))
    assert (store / "archive" / "meta.md").read_text().startswith("## [x] Meta defect in the docs\n")
    assert (store / "meta.md").read_text() == ""


@pytest.mark.parametrize("rows", [
    [{"key": "general\t## Nowhere", "reason": "why"}],
    [{"key": ENGINE, "reason": ""}],
    [{"key": ENGINE, "reason": "two\nlines"}],
    [{"key": ENGINE, "reason": "a"}, {"key": ENGINE, "reason": "b"}],
    [{"key": "archive/general\t## [x] Archived", "reason": "why"}],
    {"key": ENGINE, "reason": "not an array"},
])
def test_archive_refuses_a_bad_target_and_writes_nothing(store, capsys, tmp_path, rows):
    before = {p: p.read_text() for p in store.rglob("*.md")}
    code, _, err = _run(capsys, "--archive", _file(tmp_path, "targets.json", rows))
    assert code == 2 and err
    assert {p: p.read_text() for p in store.rglob("*.md")} == before


def test_repeated_headings_get_distinct_keys_and_archive_picks_the_key(store, capsys, tmp_path):
    (store / "twin.md").write_text("## Same\n- tags: idea, engine\nfirst\n\n## Same\n- tags: idea, engine\nsecond\n")
    assert [row["key"] for row in _listing(capsys, "--new", "--domain", "twin")] == ["twin\t## Same", "twin\t## Same\t2"]
    _archive(capsys, tmp_path, ("twin\t## Same\t2", "dup"))
    assert "first" in (store / "twin.md").read_text() and "second" not in (store / "twin.md").read_text()


def test_archive_retries_when_the_file_changes_while_it_runs(store, capsys, tmp_path, monkeypatch):
    general, real, calls = store / "general.md", tech_debt._read, []
    def racing(path):
        calls.append(path)
        if len(calls) == 1:
            with open(path, "a") as f:
                f.write(LATE)
        return real(path)
    monkeypatch.setattr(tech_debt, "_read", racing)
    code, _, _ = _archive(capsys, tmp_path, (STALE, "done"))
    text = general.read_text()
    assert code == 0 and len(calls) == 2
    assert "## Late entry" in text and "## Docs are stale" not in text
    assert (store / "archive" / "general.md").read_text().count("Docs are stale") == 1


def test_archive_keeps_an_edit_of_the_target_made_while_it_runs(store, capsys, tmp_path, monkeypatch):
    general, real, calls = store / "general.md", tech_debt._read, []
    def editing(path):
        calls.append(path)
        if len(calls) == 1:
            general.write_text(general.read_text().replace("- tags: debt docs\n", "- tags: debt docs\n- seen: 2026-10-20\n"))
        return real(path)
    monkeypatch.setattr(tech_debt, "_read", editing)
    code, _, _ = _archive(capsys, tmp_path, (STALE, "done"))
    assert code == 0 and "2026-10-20" in (store / "archive" / "general.md").read_text()
    assert "## Docs are stale" not in general.read_text()


def test_archive_keeps_an_append_that_lands_in_the_old_file(store, capsys, tmp_path, monkeypatch):
    real = tech_debt._replace
    def racing(src, dst):
        with open(dst, "a") as f:
            f.write(LATE)
        real(src, dst)
    monkeypatch.setattr(tech_debt, "_replace", racing)
    code, _, _ = _archive(capsys, tmp_path, (STALE, "done"))
    text = (store / "general.md").read_text()
    assert code == 0 and "## Late entry" in text and "## Docs are stale" not in text


def test_new_flags_refuse_bad_company(store, capsys, tmp_path):
    listing = _file(tmp_path, "listing.json", _listing(capsys, "--new"))
    targets = _file(tmp_path, "targets.json", [{"key": STALE, "reason": "done"}])
    for argv in (["--untagged", "--new"], ["--untagged", "--json"], ["--seen", listing, "--new"],
                 ["--archive", targets, "--tag", "defect"], ["--seen", listing, "--archive", targets]):
        assert _run(capsys, *argv)[0] == 2, argv
    assert not (store / ".seen.json").exists() and "## Docs are stale" in (store / "general.md").read_text()


@pytest.mark.parametrize("body,line", [
    ("```bash\n## Hidden live entry\n- tags: defect, engine\n", 4),
    ("# Hidden live entry\n- tags: defect, engine\n", 3),
])
def test_archive_refuses_a_target_that_hides_another_heading(store, capsys, tmp_path, body, line):
    ops = store / "ops.md"
    ops.write_text("## Dead\n- tags: defect, engine\n" + body)
    before = ops.read_text()
    code, _, err = _archive(capsys, tmp_path, ("ops\t## Dead", "dead"))
    assert code == 2 and f"{ops}:{line}" in err
    assert ops.read_text() == before and not (store / "archive" / "ops.md").exists()


def test_archive_names_what_it_left_in_both_files_and_a_rerun_files_it_once(store, capsys, tmp_path, monkeypatch):
    real = tech_debt._read
    def racing(path):
        with open(path, "a") as f:
            f.write(LATE)
        return real(path)
    monkeypatch.setattr(tech_debt, "_read", racing)
    code, _, err = _archive(capsys, tmp_path, (STALE, "done"))
    assert code == 1 and json.dumps(STALE) in err
    monkeypatch.setattr(tech_debt, "_read", real)
    assert _archive(capsys, tmp_path, (STALE, "done"))[0] == 0
    assert (store / "archive" / "general.md").read_text().count("## [x] Docs are stale") == 1
    assert "## Docs are stale" not in (store / "general.md").read_text()


def test_archive_names_a_target_renamed_after_it_was_filed(store, capsys, tmp_path, monkeypatch):
    general, real, calls = store / "general.md", tech_debt._read, []
    def renaming(path):
        calls.append(path)
        if len(calls) == 1:
            general.write_text(general.read_text().replace("## Docs are stale\n", "## Docs are stale again\n"))
        return real(path)
    monkeypatch.setattr(tech_debt, "_read", renaming)
    code, _, err = _archive(capsys, tmp_path, (STALE, "done"))
    assert code == 1 and json.dumps(STALE) in err
    assert "## Docs are stale again" in general.read_text()
