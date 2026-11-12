import asyncio
import hashlib

from dockwright import mcp_server as ms
from dockwright import notebook_migrate as nm
from dockwright import paths, pipeline, topic
from dockwright.mcp_server import artifact_put_impl, pipeline_start_impl, pipeline_status_impl
from tests.test_pipeline_advance import (_fake_registration, _manager, _setup_schema, _started_at_design,
                                         fresh_orchestrator_dir)

FENCE = "`" * 3
GREEK = "".join(map(chr, (0x3a6, 0x3ac, 0x3c3, 0x3b7)))
NOTEBOOK = (
    "# Notebook — meta\n"
    "Intro line.\n"
    "\n"
    "## [ ] ⏳ Fix the pager route\n"
    "- when: the PR merges\n"
    "- check: `gh pr view 7 --json state`\n"
    "\n"
    "## [x] Fix the pager route today\n"
    "- resolved: done\n"
    "\n"
    "## ⛔⛔ HIS RULINGS — binding\n"
    f"{FENCE}markdown\n"
    "## not a heading\n"
    f"{FENCE}\n"
    "- rule one\n"
    "\n"
    f"## [ ] {GREEK}\n"
    "check: `ls`\n"
    "\n"
    "## [ ] Same title\n"
    "one\n"
    "## [ ] Same title\n"
    "two"
)
T1, T2, T3, T4 = "⏳ Fix the pager route", "Fix the pager route today", "⛔⛔ HIS RULINGS — binding", GREEK


def _sha(text, n):
    return hashlib.sha1(text.encode()).hexdigest()[:n]


KEYS = [f"note-meta-fix-the-pager-route-{_sha(T1, 6)}", f"note-meta-fix-the-pager-route-{_sha(T2, 6)}",
        "note-meta-his-rulings-binding", f"note-meta-h{_sha(T4, 8)}",
        f"note-meta-same-title-{_sha('Same title', 6)}", f"note-meta-same-title-{_sha('Same title', 6)}-2"]


def _write(text=NOTEBOOK, domain="meta"):
    p = paths.ROOT / "notebook" / f"{domain}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _run(capsys, *argv):
    rc = nm.main(list(argv))
    return rc, capsys.readouterr()


def _events_count():
    return sum(len(p.read_text().splitlines()) for p in paths.ARTIFACTS.glob("*/events.jsonl"))


def test_the_split_joins_back_to_the_file():
    header, sections = nm.split(NOTEBOOK)
    assert header + "".join(s.text for s in sections) == NOTEBOOK
    assert header == "# Notebook — meta\nIntro line.\n\n"
    assert [s.title for s in sections] == [T1, T2, T3, T4, "Same title", "Same title"]
    assert [s.closed for s in sections] == [False, True, False, False, False, False]
    assert "## not a heading\n" in sections[2].text
    assert sections[-1].text == "## [ ] Same title\ntwo"


def test_a_fence_needs_a_matching_close_and_inline_code_opens_none():
    text = (f"## A\n{FENCE}ls -la{FENCE} was the old check\n"
            f"## B\n{FENCE}\n## in B\n~~~\n## still in B\n{FENCE}\n"
            f"## C\n{FENCE}`\n{FENCE}\n## in C\n{FENCE}`\n"
            "## D\n")
    header, sections = nm.split(text)
    assert [s.title for s in sections] == ["A", "B", "C", "D"]
    assert header + "".join(s.text for s in sections) == text


def test_keys_are_slugs_with_a_hash_only_where_slugs_collide():
    assert nm.keys_for("meta", nm.split(NOTEBOOK)[1]) == KEYS


def test_a_long_title_fits_the_key_limit():
    sections = nm.split("## " + "abcdefghij" * 4 + " " + "klmnopqrst" * 4 + " x y\n")[1]
    assert len(nm.keys_for("a-long-domain-name", sections)[0]) <= nm.KEY_MAX


def test_migrate_writes_one_note_per_section_and_the_store_joins_back(fresh_orchestrator_dir, capsys):
    _write()
    rc, out = _run(capsys, "meta")
    assert rc == 0
    assert out.out == ("notebook-migrate meta: 6 notes (new 6, updated 0, unchanged 0, closed 0); decisions: -; "
                       "diff: clean\n")
    for key, section in zip(KEYS, nm.split(NOTEBOOK)[1]):
        got = ms.artifact_get_impl(key, "context", "ask")
        assert got["content"] == section.text and got["writer_sid"] == nm.WRITER and got["scope"] == "output"
    cards = [topic.read(k) for k in KEYS]
    assert [c["status"] for c in cards] == ["open", "closed", "open", "open", "open", "open"]
    assert {(c["kind"], c["domain"], c["priority"]) for c in cards} == {("note", "meta", "P2")}
    assert cards[0]["title"] == T1
    fleet = topic.read("fleet-meta")
    assert (fleet["kind"], fleet["domain"], fleet["status"], fleet["title"]) == ("fleet", "meta", "open",
                                                                                "Notebook — meta")
    assert ms.artifact_get_impl("fleet-meta", "context", "ask")["content"] == "# Notebook — meta\nIntro line.\n\n"
    assert (paths.ROOT / "notebook" / "meta.md").read_text() == NOTEBOOK


def test_a_rerun_of_an_unchanged_file_writes_nothing(fresh_orchestrator_dir, capsys):
    _write()
    _run(capsys, "meta", "--decisions", "HIS RULINGS")
    before = (_events_count(), {k: paths.topic_path(k).stat().st_mtime_ns for k in [*KEYS, "fleet-meta"]})
    rc, out = _run(capsys, "meta")
    assert rc == 0
    assert out.out == ("notebook-migrate meta: 6 notes (new 0, updated 0, unchanged 6, closed 0); "
                       "decisions: unchanged; diff: clean\n")
    assert (_events_count(), {k: paths.topic_path(k).stat().st_mtime_ns for k in [*KEYS, "fleet-meta"]}) == before


def test_an_edited_section_updates_and_a_removed_one_closes(fresh_orchestrator_dir, capsys):
    _write()
    _run(capsys, "meta")
    edited = NOTEBOOK.replace("the PR merges", "the PR closes").replace(f"## [ ] {GREEK}\ncheck: `ls`\n\n", "")
    _write(edited)
    rc, out = _run(capsys, "meta")
    assert rc == 0
    assert out.out == ("notebook-migrate meta: 5 notes (new 0, updated 1, unchanged 4, closed 1); decisions: -; "
                       "diff: clean\n")
    assert topic.read(KEYS[3])["status"] == "closed"
    assert "the PR closes" in ms.artifact_get_impl(KEYS[0], "context", "ask")["content"]
    assert (paths.artifact_ticket_dir(KEYS[0]) / "context.ask.md.prev").exists()


def test_a_heading_with_loose_spacing_keeps_its_card_on_a_rerun(fresh_orchestrator_dir, capsys):
    text = "## [ ] Fix  the\tpager route\n## [ ]  leading space title\n"
    _write(text)
    _run(capsys, "meta")
    rc, out = _run(capsys, "meta")
    assert rc == 0 and "(new 0, updated 0, unchanged 2, closed 0)" in out.out
    keys = nm.keys_for("meta", nm.split(text)[1])
    assert [topic.read(k)["title"] for k in keys] == ["Fix the pager route", "leading space title"]


def test_a_heading_cut_at_a_space_keeps_its_card_on_a_rerun(fresh_orchestrator_dir, capsys):
    text = "## [ ] " + "a" * (topic.TITLE_MAX - 1) + " b\n"
    _write(text)
    _run(capsys, "meta")
    rc, out = _run(capsys, "meta")
    assert rc == 0 and "(new 0, updated 0, unchanged 1, closed 0)" in out.out
    assert topic.read(nm.keys_for("meta", nm.split(text)[1])[0])["title"] == "a" * (topic.TITLE_MAX - 1)


def test_a_ticked_box_closes_the_same_note(fresh_orchestrator_dir, capsys):
    _write()
    _run(capsys, "meta")
    _write(NOTEBOOK.replace("## [ ] ⏳ Fix", "## [x] ⏳ Fix"))
    rc, out = _run(capsys, "meta")
    assert rc == 0 and "(new 0, updated 1, unchanged 5, closed 0)" in out.out
    assert topic.read(KEYS[0])["status"] == "closed"


def test_decisions_join_the_marked_sections_verbatim_and_the_marker_sticks(fresh_orchestrator_dir, capsys):
    _write()
    rc, out = _run(capsys, "meta", "--decisions", "HIS RULINGS")
    assert rc == 0 and "decisions: written; diff: clean" in out.out
    rulings = nm.split(NOTEBOOK)[1][2].text
    got = ms.artifact_get_impl("fleet-meta", topic.DECISIONS_PHASE, "meta")
    assert got["content"] == rulings and got["writer_sid"] == nm.WRITER
    assert topic.read("fleet-meta")["decisions_marker"] == "HIS RULINGS"
    _write(NOTEBOOK.replace("- rule one", "- rule two"))
    rc, out = _run(capsys, "meta")
    assert rc == 0 and "decisions: written" in out.out
    assert "- rule two" in ms.artifact_get_impl("fleet-meta", topic.DECISIONS_PHASE, "meta")["content"]
    rc, out = _run(capsys, "meta")
    assert "decisions: unchanged" in out.out


def test_a_marker_that_matches_nothing_exits_1_before_any_write(fresh_orchestrator_dir, capsys):
    _write()
    rc, out = _run(capsys, "meta", "--decisions", "NOPE")
    assert rc == 1 and "no section heading holds 'NOPE'" in out.err
    assert list(paths.ARTIFACTS.iterdir()) == []


def test_a_stale_stored_marker_names_its_remedies_and_an_empty_flag_clears_it(fresh_orchestrator_dir, capsys):
    _write()
    _run(capsys, "meta", "--decisions", "HIS RULINGS")
    _write(NOTEBOOK.replace("HIS RULINGS", "THE RULES"))
    rc, out = _run(capsys, "meta")
    assert rc == 1
    assert "'HIS RULINGS' stored in fleet-meta" in out.err and "--decisions ''" in out.err
    rc, out = _run(capsys, "meta", "--decisions", "THE RULES")
    assert rc == 0 and "decisions: written" in out.out
    assert topic.read("fleet-meta")["decisions_marker"] == "THE RULES"
    rc, out = _run(capsys, "meta", "--decisions", "")
    assert rc == 0 and "decisions: -" in out.out
    assert "decisions_marker" not in topic.read("fleet-meta")
    rc, out = _run(capsys, "meta")
    assert rc == 0 and "(new 0, updated 0, unchanged 6, closed 0); decisions: -" in out.out


def test_a_dry_run_writes_nothing(fresh_orchestrator_dir, capsys):
    _write()
    rc, out = _run(capsys, "meta", "--decisions", "HIS RULINGS", "--dry-run")
    assert rc == 0
    assert out.out == ("notebook-migrate meta (dry run): 6 notes (new 6, updated 0, unchanged 0, closed 0); "
                       "decisions: written; diff: clean\n")
    assert list(paths.ARTIFACTS.iterdir()) == []


def test_a_store_that_differs_from_the_file_prints_the_diff_and_exits_1(fresh_orchestrator_dir, capsys, monkeypatch):
    _write()
    _run(capsys, "meta")
    real = ms.artifact_get_impl

    def drifted(task_key, phase, name, reader_sid=None):
        got = real(task_key, phase, name, reader_sid)
        return {**got, "content": got["content"] + "drift\n"} if task_key == KEYS[2] else got

    monkeypatch.setattr(ms, "artifact_get_impl", drifted)
    rc, out = _run(capsys, "meta")
    assert rc == 1 and "diff: clean" not in out.out and "+drift\n" in out.out


def test_a_bad_domain_or_a_missing_notebook_exits_2(fresh_orchestrator_dir, capsys):
    assert _run(capsys, "a/b")[0] == 2
    rc, out = _run(capsys, "nope")
    assert rc == 2 and "no notebook" in out.err


def test_notes_of_another_domain_stay_open(fresh_orchestrator_dir, capsys):
    _write(domain="meta-x")
    _run(capsys, "meta-x")
    _write("# only a header\n")
    rc, out = _run(capsys, "meta")
    assert rc == 0 and "closed 0" in out.out
    assert all(topic.read(k)["status"] == ("closed" if i == 1 else "open")
               for i, k in enumerate(nm.keys_for("meta-x", nm.split(NOTEBOOK)[1])))


def _first_brief(monkeypatch, tmp_path):
    _setup_schema(monkeypatch, tmp_path)
    _manager()
    calls = _fake_registration(monkeypatch, ["registered"])
    asyncio.run(pipeline_start_impl("m1", "K", "code-ticket", "/w", "ask"))
    return calls[0]["initial_prompt"]


def test_a_node_brief_names_the_domain_registry_right_after_its_inputs(fresh_orchestrator_dir, tmp_path, monkeypatch):
    artifact_put_impl("fleet-general", topic.DECISIONS_PHASE, "general", "## ⛔ rule\n", "complete", nm.WRITER,
                      scope="output")
    lines = _first_brief(monkeypatch, tmp_path).splitlines()
    i = next(n for n, line in enumerate(lines) if line.startswith("Inputs (read ONLY these"))
    assert lines[i + 1] == pipeline._rulings_line("fleet-general", "general")


def test_no_registry_no_rulings_line(fresh_orchestrator_dir, tmp_path, monkeypatch):
    assert "Domain rulings" not in _first_brief(monkeypatch, tmp_path)


def test_a_node_that_pins_the_registry_reads_it_without_a_flag(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    artifact_put_impl("K", "design", "hld", "x", "complete", sid, scope="output",
                      read_set=[{"name": "decisions.general", "task_key": "fleet-general", "written_at": 1.0},
                                {"name": "decisions.general", "written_at": 1.0}])

    def reads():
        return [line for line in pipeline_status_impl("K").splitlines() if "decisions.general" in line]

    assert len(reads()) == 2 and all("OUTSIDE INPUTS" in line for line in reads())
    artifact_put_impl("fleet-general", topic.DECISIONS_PHASE, "general", "## ⛔ rule\n", "complete", nm.WRITER,
                      scope="output")
    assert len(reads()) == 2 and not any("OUTSIDE INPUTS" in line for line in reads())


def test_a_read_of_the_works_own_decisions_artifact_stays_flagged(fresh_orchestrator_dir, tmp_path, monkeypatch):
    sid, _ = _started_at_design(monkeypatch, tmp_path)
    artifact_put_impl("K", topic.DECISIONS_PHASE, "general", "x", "complete", sid)
    ms.artifact_get_impl("K", topic.DECISIONS_PHASE, "general", reader_sid=sid)
    artifact_put_impl("fleet-general", topic.DECISIONS_PHASE, "general", "## ⛔ rule\n", "complete", nm.WRITER,
                      scope="output")
    reads = [line for line in pipeline_status_impl("K").splitlines() if ") read decisions.general" in line]
    assert len(reads) == 1 and "OUTSIDE INPUTS" in reads[0]


def test_pending_counts_what_a_rerun_would_write_and_writes_nothing(fresh_orchestrator_dir, capsys):
    assert nm.pending("meta") is None
    path = _write()
    assert _run(capsys, "meta")[0] == 0
    events = _events_count()
    assert nm.pending("meta") == 0
    path.write_text(NOTEBOOK.replace("- when: the PR merges\n", "- when: the PR merges twice\n"))
    assert nm.pending("meta") == 1
    path.write_text(NOTEBOOK.replace(f"## [ ] {GREEK}\ncheck: `ls`\n\n", ""))
    assert nm.pending("meta") == 1
    path.write_text("# Notebook — meta, renamed\n" + NOTEBOOK.split("\n", 1)[1])
    assert nm.pending("meta") == 1
    assert _events_count() == events
    assert nm.notebook_path("meta") == paths.ROOT / "notebook" / "meta.md"


def test_pending_counts_an_edit_that_changes_only_the_rulings(fresh_orchestrator_dir, capsys):
    retired = "## [x] HIS RULINGS, retired\n- rule zero\n\n"
    path = _write(NOTEBOOK.replace("## ⛔⛔ HIS RULINGS", retired + "## ⛔⛔ HIS RULINGS"))
    assert _run(capsys, "meta", "--decisions", "HIS RULINGS")[0] == 0
    assert nm.pending("meta") == 0
    path.write_text(NOTEBOOK)
    assert "decisions: written" in _run(capsys, "meta", "--dry-run")[1].out
    assert nm.pending("meta") == 1
    _run(capsys, "meta")
    assert nm.pending("meta") == 0


def test_an_unreadable_notebook_is_a_value_error(fresh_orchestrator_dir, capsys):
    _write()
    _run(capsys, "meta")
    nm.notebook_path("meta").write_bytes(b"## [ ] ok\n\xff\xfe")
    try:
        nm.pending("meta")
    except ValueError as err:
        assert "meta.md" in str(err)
    else:
        raise AssertionError("pending read a notebook that is not UTF-8")
    rc, out = _run(capsys, "meta")
    assert rc == 2 and "meta.md" in out.err
