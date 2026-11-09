"""Ideas board (SPEC-0490): bucketing, spec-status join, rail counts, scoreboard and activity.

The model half (`nightwright.tui.ideas_board`) imports no Textual, so the rules are tested as
pure functions over hand-built rows. One pilot smoke test boots the real desk on a throwaway
org and checks the tab renders rail + buckets and that a mutation key still lands on the
selected idea.
"""
import asyncio
import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from waits import pilot_until

from nightwright.tui import ideas_board as B
from nightwright.tui import model as M

NOW = dt.datetime(2026, 11, 9, 12, 0)


def _row(iid, state="IDEA", *, project="Meta-Tools", priority="", oq=0, epic=""):
    return M.DeskRow(id=iid, state=state, priority=priority, kind="idea", project=project,
                     epic=epic, age="", heading=f"heading of {iid}", is_done=False,
                     state_style="", open_questions=oq)


def _task(*, spec="", created=None, updated=None, closed=None):
    props = {}
    if spec:
        props["SPEC"] = spec
    if created:
        props["CREATED"] = created
    if updated:
        props["UPDATED"] = updated
    return SimpleNamespace(properties=props, closed=closed)


def _spec(sid, status, **kw):
    return B.SpecInfo(id=sid, status=status, **kw)


# --- bucketing rules ------------------------------------------------------------------------

@pytest.mark.parametrize("state,status,bucket", [
    ("IDEA", None, B.OPEN),
    ("INCUBATE", None, B.OPEN),
    ("RESEARCHED", None, B.OPEN),            # closed as research, never built
    ("SPECCED", None, B.IN_FLIGHT),          # claims a spec but links none: surfaced
    ("PROMOTED", None, B.IN_FLIGHT),
    ("SPECCED", "draft", B.IN_FLIGHT),       # a spec exists, however early
    ("IDEA", "accepted", B.IN_FLIGHT),       # the spec decides, not the idea's state
    ("EXPORTED", "in-progress", B.IN_FLIGHT),
    ("PROMOTED", "missing", B.IN_FLIGHT),    # spec file not found: an anomaly, not BUILT
    ("PROMOTED", "done", B.BUILT),
    ("RESEARCHED", "done", B.BUILT),
    ("SHIPPED", None, B.BUILT),
    ("SHIPPED", "accepted", B.BUILT),        # SHIPPED is the human's word: built
    ("PROMOTED", "rejected", B.DROPPED),
    ("DROPPED", "done", B.DROPPED),          # a dropped idea stays dropped
    ("DROPPED", None, B.DROPPED),
])
def test_classify(state, status, bucket):
    link = None if status is None else B.SpecLink("SPEC-0001", status)
    assert B.classify(state, link) == bucket


def test_supersede_chain_is_followed_to_the_live_spec():
    specs = {"SPEC-0001": _spec("SPEC-0001", "superseded", superseded_by="SPEC-0002"),
             "SPEC-0002": _spec("SPEC-0002", "superseded", superseded_by="SPEC-0003"),
             "SPEC-0003": _spec("SPEC-0003", "done")}
    link = B.resolve_link("SPEC-0001", specs)
    assert (link.status, link.via) == ("done", "SPEC-0003")
    assert link.origin.id == "SPEC-0001"
    assert link.label == "SPEC-0001→SPEC-0003 done"
    assert B.classify("PROMOTED", link) == B.BUILT


def test_supersede_cycle_terminates():
    specs = {"SPEC-0001": _spec("SPEC-0001", "superseded", superseded_by="SPEC-0002"),
             "SPEC-0002": _spec("SPEC-0002", "superseded", superseded_by="SPEC-0001")}
    assert B.resolve_link("SPEC-0001", specs).status == "superseded"


def test_missing_foreign_and_claimed_links():
    specs = {"SPEC-0007": _spec("SPEC-0007", "accepted", source_idea="IDEA-9")}
    assert B.resolve_link("SPEC-0404", specs).status == "missing"
    # SPEC-0327: a bare id on another project's idea is that project's spec, not ours …
    assert B.resolve_link("SPEC-0007", specs, idea_id="IDEA-1", internal=False).status \
        == "foreign"
    # … unless our spec explicitly claims the idea.
    assert B.resolve_link("SPEC-0007", specs, idea_id="IDEA-9", internal=False).status \
        == "accepted"
    assert B.resolve_link("", specs) is None


def test_outbound_exported_shows_in_the_label():
    specs = {"JER-0001": _spec("JER-0001", "accepted", outbound=True, exported=True)}
    link = B.resolve_link("JER-0001", specs, idea_id="IDEA-1", internal=False)
    assert link.label == "JER-0001 accepted · exported"


# --- board derivation -----------------------------------------------------------------------

def _board(pairs, specs=None):
    return B.build_board(pairs, specs or {}, now=NOW)


def test_open_bucket_puts_questions_first_then_priority_then_recency():
    pairs = [
        (_row("IDEA-1"), _task(updated="[2026-11-09 Mon 09:00]")),
        (_row("IDEA-2", priority="A"), _task(updated="[2026-11-01 Sun 09:00]")),
        (_row("IDEA-3", oq=2), _task(updated="[2026-10-01 Thu 09:00]")),
        (_row("IDEA-4"), _task(updated="[2026-11-08 Sun 09:00]")),
    ]
    buckets = dict(_board(pairs).buckets("Meta-Tools"))
    assert [i.id for i in buckets[B.OPEN]] == ["IDEA-3", "IDEA-2", "IDEA-1", "IDEA-4"]


def test_built_is_newest_first_and_dropped_only_on_demand():
    specs = {"SPEC-0001": _spec("SPEC-0001", "done", updated=dt.date(2026, 10, 1)),
             "SPEC-0002": _spec("SPEC-0002", "done", updated=dt.date(2026, 11, 5))}
    pairs = [
        (_row("IDEA-1", "PROMOTED"), _task(spec="SPEC-0001")),
        (_row("IDEA-2", "PROMOTED"), _task(spec="SPEC-0002")),
        (_row("IDEA-3", "DROPPED"), _task()),
    ]
    board = _board(pairs, specs)
    shown = dict(board.buckets("Meta-Tools"))
    assert list(shown) == [B.OPEN, B.IN_FLIGHT, B.BUILT]
    assert [i.id for i in shown[B.BUILT]] == ["IDEA-2", "IDEA-1"]
    revealed = dict(board.buckets("Meta-Tools", show_dropped=True))
    assert [i.id for i in revealed[B.DROPPED]] == ["IDEA-3"]


def test_in_flight_rows_carry_the_real_spec_status():
    specs = {"SPEC-0001": _spec("SPEC-0001", "accepted"),
             "SPEC-0002": _spec("SPEC-0002", "in-progress")}
    pairs = [(_row("IDEA-1", "SPECCED"), _task(spec="SPEC-0001")),
             (_row("IDEA-2", "SPECCED"), _task(spec="SPEC-0002")),
             (_row("IDEA-3", "SPECCED"), _task(spec="SPEC-0999"))]
    flight = dict(_board(pairs, specs).buckets("Meta-Tools"))[B.IN_FLIGHT]
    # (Meta-Tools is the internal project, so bare SPEC ids resolve against our specs)
    # furthest-along spec first; a missing spec file last and labelled as such
    assert [(i.id, i.spec_label) for i in flight] == [
        ("IDEA-2", "SPEC-0002 in-progress"), ("IDEA-1", "SPEC-0001 accepted"),
        ("IDEA-3", "SPEC-0999 missing")]


def test_rail_counts_and_order():
    specs = {"SPEC-0001": _spec("SPEC-0001", "accepted")}
    pairs = [(_row("IDEA-1", project="beta"), _task()),
             (_row("IDEA-2", project="Alpha"), _task()),
             (_row("IDEA-3", "SPECCED", project="Alpha"), _task(spec="SPEC-0001")),
             (_row("IDEA-4", project=""), _task()),
             (_row("IDEA-5", "DROPPED", project="Alpha"), _task())]
    rail = _board(pairs, specs).rail
    assert [e.label for e in rail] == ["All", "Alpha", "beta", "(no project)"]
    assert [e.counts for e in rail] == ["3·1", "1·1", "1·0", "1·0"]
    assert rail[1].dropped == 1


def test_scoreboard_movement_and_total():
    specs = {"SPEC-0001": _spec("SPEC-0001", "done", created=dt.date(2026, 11, 1),
                                updated=dt.date(2026, 11, 8))}
    pairs = [(_row("IDEA-1"), _task(created="[2026-11-07 Sat 10:00]")),
             (_row("IDEA-2"), _task(created="[2026-10-01 Thu 10:00]")),
             (_row("IDEA-3", "PROMOTED"), _task(spec="SPEC-0001",
                                                created="[2026-11-03 Tue 10:00]")),
             (_row("IDEA-4", project="beta"), _task(created="[2026-09-01 Tue 10:00]"))]
    score = {r.label: r for r in _board(pairs, specs).scoreboard}
    alpha = score["Meta-Tools"]
    assert (alpha.open, alpha.in_flight, alpha.built) == (2, 0, 1)
    assert alpha.movement == "+2 ideas · 1 built"
    assert score["beta"].movement == "—"
    assert score["total"].built == 1 and score["total"].open == 3
    assert list(score)[-1] == "total"


def test_activity_feed_is_newest_first_across_projects():
    specs = {"SPEC-0001": _spec("SPEC-0001", "done", created=dt.date(2026, 11, 2),
                                updated=dt.date(2026, 11, 6))}
    pairs = [(_row("IDEA-1", "PROMOTED"), _task(spec="SPEC-0001",
                                                created="[2026-11-01 Sun 10:00]")),
             (_row("IDEA-2", project="beta"), _task(created="[2026-11-04 Wed 10:00]")),
             (_row("IDEA-3"), _task(updated="[2026-11-03 Tue 10:00]"))]   # no CREATED
    feed = [(a.item.id, a.text) for a in _board(pairs, specs).activity]
    assert feed == [("IDEA-1", "built (SPEC-0001)"), ("IDEA-2", "captured"),
                    ("IDEA-3", "updated"), ("IDEA-1", "became SPEC-0001"),
                    ("IDEA-1", "captured")]


def test_same_day_spec_events_borrow_the_ideas_clock_time():
    specs = {"SPEC-0001": _spec("SPEC-0001", "done", created=dt.date(2026, 11, 9),
                                updated=dt.date(2026, 11, 9))}
    pairs = [(_row("IDEA-1", "PROMOTED"), _task(spec="SPEC-0001",
                                                updated="[2026-11-09 Mon 11:30]")),
             (_row("IDEA-2"), _task(created="[2026-11-09 Mon 08:00]"))]
    feed = [(a.item.id, a.kind) for a in _board(pairs, specs).activity]
    assert feed[0] == ("IDEA-1", "built")         # 11:30, not midnight below the 08:00 capture


def test_epic_rollup_from_child_specs_then_child_ideas():
    specs = {"SPEC-0010": _spec("SPEC-0010", "in-progress", kind="epic"),
             "SPEC-0011": _spec("SPEC-0011", "done", parent="SPEC-0010"),
             "SPEC-0012": _spec("SPEC-0012", "accepted", parent="SPEC-0010"),
             "SPEC-0013": _spec("SPEC-0013", "superseded", parent="SPEC-0010"),
             "JER-0001": _spec("JER-0001", "accepted", kind="epic", outbound=True)}
    pairs = [(_row("IDEA-1", "SPECCED"), _task(spec="SPEC-0010")),
             (_row("IDEA-2", "PROMOTED"), _task(spec="SPEC-0011")),
             (_row("IDEA-3", "EXPORTED", project="jerome"), _task(spec="JER-0001")),
             (_row("IDEA-4", "SHIPPED", project="jerome", epic="JER-0001"), _task()),
             (_row("IDEA-5", project="jerome", epic="JER-0001"), _task()),
             (_row("IDEA-6", "DROPPED", project="jerome", epic="JER-0001"), _task())]
    board = _board(pairs, specs)
    assert board.by_id["IDEA-1"].rollup_label == "1/2 built"     # superseded child excluded
    assert board.by_id["IDEA-3"].rollup_label == "1/2 built"     # dropped child idea excluded
    assert board.by_id["IDEA-2"].rollup is None


# --- loaders --------------------------------------------------------------------------------

def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    specs = tmp_path / "specs"
    specs.mkdir()
    (tmp_path / "data").mkdir()
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED",
                              "SHIPPED", "DROPPED", "RESEARCHED"],
        "org_question_keywords": ["OPEN", "|", "RESOLVED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(tmp_path / "data"),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(specs),
        "config_dir": str(tmp_path / "config"),
    }


def _write_corpus(cfg):
    specs = Path(cfg["specs_dir"])
    for num, status, extra in ((1, "accepted", "kind: epic\n"),
                               (2, "done", "parent: SPEC-0001\n")):
        (specs / f"{num:04d}-x.md").write_text(
            f"---\nid: SPEC-{num:04d}\ntitle: \"s{num}\"\nstatus: {status}\nowner: b\n"
            f"created: 2026-11-01\nupdated: 2026-11-05\n{extra}---\n\n## Context\n\nx\n")
    outbox = Path(cfg["data_dir"]) / "outbox" / "jerome"
    outbox.mkdir(parents=True)
    (outbox / "JEROME-0001-x.md").write_text(
        "---\nid: JEROME-0001\ntitle: \"j\"\nstatus: accepted\nowner: b\n"
        "created: 2026-11-02\n---\n\nbody\n")
    (outbox / "PROVENANCE.md").write_text("JEROME-0001 exported to ../jerome\n")
    Path(cfg["org_ideas_file"]).write_text(
        "#+TODO: IDEA INCUBATE SPECCED | PROMOTED EXPORTED SHIPPED DROPPED RESEARCHED\n\n"
        "* SPECCED the epic\n  :PROPERTIES:\n  :ID: IDEA-001\n  :PROJECT: Meta-Tools\n"
        "  :SPEC: SPEC-0001\n  :CREATED: [2026-11-01 Sun 10:00]\n"
        "  :UPDATED: [2026-11-05 Thu 10:00]\n  :END:\n"
        "* PROMOTED the child\n  :PROPERTIES:\n  :ID: IDEA-002\n  :PROJECT: Meta-Tools\n"
        "  :SPEC: SPEC-0002\n  :UPDATED: [2026-11-05 Thu 11:00]\n  :END:\n"
        "* IDEA a loose idea with a question\n  :PROPERTIES:\n  :ID: IDEA-003\n"
        "  :PROJECT: Meta-Tools\n  :UPDATED: [2026-11-06 Fri 09:00]\n  :END:\n"
        "** Open questions\n*** OPEN why?\n"
        "* EXPORTED handed to jerome\n  :PROPERTIES:\n  :ID: IDEA-004\n  :PROJECT: jerome\n"
        "  :SPEC: JEROME-0001\n  :UPDATED: [2026-11-02 Mon 09:00]\n  :END:\n"
        "* DROPPED a dead one\n  :PROPERTIES:\n  :ID: IDEA-005\n  :PROJECT: jerome\n"
        "  :UPDATED: [2026-11-03 Tue 09:00]\n  :END:\n")


def test_load_spec_index_reads_internal_and_outbound(tmp_path):
    cfg = _cfg(tmp_path)
    _write_corpus(cfg)
    specs = B.load_spec_index(cfg)
    assert specs["SPEC-0001"].kind == "epic" and specs["SPEC-0002"].parent == "SPEC-0001"
    assert specs["SPEC-0002"].updated == dt.date(2026, 11, 5)
    jer = specs["JEROME-0001"]
    assert jer.outbound and jer.exported and jer.status == "accepted"


def test_load_board_joins_real_org_and_specs(tmp_path):
    from nightwright.explore import open_question_count
    from nightwright.org import load_tasks

    cfg = _cfg(tmp_path)
    _write_corpus(cfg)
    board = B.load_board(cfg, now=NOW)
    got = {i.id: (i.bucket, i.spec_label) for i in board.items}
    assert got == {
        "IDEA-001": (B.IN_FLIGHT, "SPEC-0001 accepted"),
        "IDEA-002": (B.BUILT, "SPEC-0002 done"),
        "IDEA-003": (B.OPEN, ""),
        "IDEA-004": (B.IN_FLIGHT, "JEROME-0001 accepted · exported"),
        "IDEA-005": (B.DROPPED, ""),
    }
    assert board.by_id["IDEA-001"].rollup_label == "1/1 built"
    # the batched question count agrees with the per-idea reader
    for task in load_tasks(cfg):
        iid = task.properties.get("ID")
        if iid in board.by_id:
            assert board.by_id[iid].row.open_questions == open_question_count(cfg, task)
    assert board.by_id["IDEA-003"].row.open_questions == 1


# --- pilot smoke ----------------------------------------------------------------------------

def test_tab_renders_rail_and_buckets_and_mutations_target_the_selection(tmp_path):
    from test_tui_desk import _grid

    from nightwright.tui.app import IdeaDesk

    cfg = _cfg(tmp_path)
    _write_corpus(cfg)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(180, 40)) as pilot:
            assert await pilot_until(pilot, lambda: app._pairs)
            screen = "\n".join(_grid(app))
            for text in ("All", "Meta-Tools", "jerome", "RECENT ACTIVITY", "IN FLIGHT"):
                assert text in screen, f"{text!r} missing from the All view:\n{screen}"

            app.select_project("Meta-Tools")
            await pilot.pause()
            tree = app.query_one("#board")
            heads = {str(n.label).split("  ")[0]: n for n in tree.root.children}
            assert list(heads) == ["OPEN", "IN FLIGHT", "BUILT"]
            assert not heads["BUILT"].is_expanded                  # folded by default
            assert heads["OPEN"].is_expanded and heads["IN FLIGHT"].is_expanded
            screen = "\n".join(_grid(app))
            assert "SPEC-0001 accepted" in screen and "1/1 built" in screen

            # the selection is the first OPEN idea; `l` logs onto exactly that idea
            assert app.selected_id == "IDEA-003"
            app.jump_to("IDEA-001")
            assert app.selected_id == "IDEA-001"
            assert "spec SPEC-0001" in "\n".join(_grid(app))     # detail names the spec
            await pilot.press("l")
            await pilot.pause()
            for ch in "noted":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause()

            def logged():
                from nightwright.org_write import resolve_selector
                return any("noted" in e for e in
                           M.load_detail(cfg, resolve_selector(cfg, "IDEA-001")).log)
            assert await pilot_until(pilot, logged)
            assert not any("noted" in e for e in
                           M.load_detail(cfg, app._resolve_fresh("IDEA-003")).log)
            assert await pilot_until(pilot, lambda: app.selected_id == "IDEA-001")

            # search jumps across projects into a folded / hidden bucket
            app.jump_to("IDEA-005")
            assert app.project == "jerome" and app.show_dropped
            assert app.selected_id == "IDEA-005"

            # the rail itself: down from jerome selects the next project
            app.query_one("#rail").focus()
            await pilot.press("down")
            assert await pilot_until(pilot, lambda: app.project == "Meta-Tools")

    asyncio.run(drive())
