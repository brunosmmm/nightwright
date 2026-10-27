"""SPEC-0251: the TUI events tab (Textual pilot tests)."""
import asyncio
import datetime as dt
import json
import os

import pytest

from test_agentic_e2e import make_cfg, make_repo
from test_agentic_events import _campaign
from test_tui_queue_tab import SIZE, _desk, _run, press, settle
from wt import tui as T
from wt.agentic import events as EV
from wt.agentic import mailbox as MB
from wt.tui import agentic_model as AM
from wt.tui import events_tab as ET
from waits import pilot_until
from waits import pilot_until

needs_textual = pytest.mark.skipif(not T.textual_available(),
                                   reason="optional tui extra not installed")
pytestmark = needs_textual


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch):
    monkeypatch.setattr(ET, "POLL_S", 0.05)
    monkeypatch.setattr(ET, "SIDE_S", 0.2)


def _cfg(tmp_path):
    return make_cfg(tmp_path, make_repo(tmp_path), None)


def _add(cfg, type_="admitted", campaign=None, **data):
    return EV.append(cfg, type_, campaign=campaign, by="test", data=data)


def _seed(cfg, n=3):
    for i in range(n):
        _add(cfg, campaign=f"SPEC-00{i:02d}-x")


def view(app):
    return app.query_one("#events-view", ET.EventsView)


def ids(app):
    return [r.id for r in view(app).shown]


def panel(app, name):
    return str(app.query_one(f"#events-{name}").render())


def status(app):
    return str(app.query_one("#events-status").render())


async def ui_until(app, pilot, cond, what="condition", timeout=None):
    if not await pilot_until(pilot, cond, timeout):
        raise AssertionError(f"timed out waiting for {what}")
    await settle(app, pilot, 1)


async def typed(app, pilot, text):
    """Replace the open filter bar's text and press enter (Input has no select-all key)."""
    app.query_one("#events-filter").value = text
    await press(app, pilot, ["enter"])


async def open_events(app, pilot):
    await pilot.press("5")
    await settle(app, pilot)


# --- plumbing and the stream table (AC1) -------------------------------------------------

def test_key_five_opens_the_tab_and_other_keys_leave_it(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    app = _desk(cfg)

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            tabs = app.query_one("#tabs")
            await open_events(app, pilot)
            assert tabs.active == "tab-events" and app.focused is view(app).table
            assert ids(app) == [1, 2, 3]
            await pilot.press("3")
            await settle(app, pilot)
            assert tabs.active == "tab-queue"
            await pilot.press("5")
            await settle(app, pilot)
            await pilot.press("1")
            await settle(app, pilot)
            assert tabs.active == "tab-ideas" and app.focused.id == "list"

    _run(drive())


def test_tab_kwarg_opens_the_events_tab(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            assert app.query_one("#tabs").active == "tab-events"
            assert app.focused is view(app).table and ids(app) == [1, 2, 3]

    _run(drive())


def test_click_on_the_events_tab_loads_and_focuses(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    app = _desk(cfg)

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            from textual.widgets import Tabs
            tabs = app.query_one("#tabs").query_one(Tabs)
            tab = next(t for t in tabs.query("Tab") if "events" in str(t.label))
            await pilot.click(tab)
            await settle(app, pilot)
            assert app.query_one("#tabs").active == "tab-events"
            assert ids(app) == [1, 2, 3] and app.focused is view(app).table

    _run(drive())


def test_rows_have_the_documented_columns_and_styles(tmp_path):
    cfg = _cfg(tmp_path)
    _add(cfg, "admitted", "SPEC-0001-a")
    _add(cfg, "runner_dead", active=1, queued=0)
    _add(cfg, "run_failed", "SPEC-0001-a", persona="p", run_id="1", transient=True, error="429")
    _add(cfg, "run_failed", "SPEC-0001-a", persona="p", run_id="2", transient=False, error="boom")
    _add(cfg, "parked", "SPEC-0001-a", reason="r", detail="d", resume_to="X")
    _add(cfg, "merged", "SPEC-0001-a", merged_sha="abc")
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            table = view(app).table
            assert [str(c.label) for c in table.columns.values()] == \
                ["#id", "time", "type", "campaign", "text"]
            rows = [[c.plain for c in table.get_row_at(i)] for i in range(table.row_count)]
            styles = [str(table.get_row_at(i)[2].style) for i in range(table.row_count)]
            assert [r[0] for r in rows] == ["1", "2", "3", "4", "5", "6"]
            assert len(rows[0][1]) == 8 and rows[0][1][2] == ":"        # HH:MM:SS
            assert styles == ["", "red", "yellow", "red", "yellow", "green"]
            assert rows[0][3] == "SPEC-0001-a" and rows[1][3] == ""
            assert rows[1][4] == "active=1 queued=0"
            assert table.cursor_row == 5                             # opens on the tail

    _run(drive())


def test_untrusted_event_text_is_never_parsed_as_markup(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg, 1)
    err = "[/tmp/x][@click=app.quit]q[/]"
    _fail = dict(persona="p", run_id="1", transient=False)
    _add(cfg, "run_failed", "SPEC-0001-[/a]", error=err, **_fail)    # error first: survives the clip
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            table = view(app).table
            row = view(app).shown[-1]
            assert row.text.startswith(f"error={err}")
            cells = table.get_row_at(table.row_count - 1)
            assert all(hasattr(c, "plain") for c in cells)
            assert cells[3].plain == "SPEC-0001-[/a]"
            assert cells[4].plain.startswith("error=[/tmp/x][@click")
            _add(cfg, "run_failed", "SPEC-0002-b", error=err, **_fail)   # the incremental append
            await ui_until(app, pilot, lambda: len(ids(app)) == 3, "event 3")
            assert app.is_running
            assert table.get_row_at(2)[4].plain.startswith("error=[/tmp/x][@click")

    _run(drive())


def test_the_table_keeps_the_newest_events_only(tmp_path, monkeypatch):
    monkeypatch.setattr(AM, "EVENT_LIMIT", 4)
    cfg = _cfg(tmp_path)
    _seed(cfg, 6)
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            assert ids(app) == [3, 4, 5, 6]
            _add(cfg, campaign="SPEC-0099-x")
            await ui_until(app, pilot, lambda: ids(app)[-1] == 7, "event 7")
            assert ids(app) == [4, 5, 6, 7] and view(app).table.row_count == 4
            assert "4/4" in status(app)

    _run(drive())


def test_the_cursor_keeps_its_event_when_an_append_trims_the_table(tmp_path, monkeypatch):
    monkeypatch.setattr(AM, "EVENT_LIMIT", 6)
    cfg = _cfg(tmp_path)
    _seed(cfg, 6)
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await pilot.press("k", "k")                       # off the tail: event 4
            assert view(app).current_row().id == 4
            _add(cfg, campaign="SPEC-0099-x")
            await ui_until(app, pilot, lambda: ids(app)[-1] == 7, "event 7")
            assert ids(app) == [2, 3, 4, 5, 6, 7]
            assert view(app).current_row().id == 4
            _add(cfg, campaign="SPEC-0098-x")
            await ui_until(app, pilot, lambda: ids(app)[-1] == 8, "event 8")
            assert view(app).current_row().id == 4
            for _ in range(3):                                # trims event 4 away: tail
                _add(cfg, campaign="SPEC-0097-x")
            await ui_until(app, pilot, lambda: ids(app)[-1] == 11, "event 11")
            assert view(app).table.cursor_row == 5

    _run(drive())


def test_a_malformed_record_does_not_break_the_stream(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg, 2)
    with open(EV.events_path(cfg), "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": 50, "type": None, "data": "x", "campaign": 7}) + "\n")
        f.write(json.dumps({"id": 51, "type": "parked", "data": [1, 2]}) + "\n")
    rows = AM.load_events(cfg)
    assert [r.id for r in rows][:2] == [1, 2]
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            assert ids(app)[:2] == [1, 2]
            _add(cfg, campaign="SPEC-0010-x")
            await ui_until(app, pilot, lambda: len(ids(app)) > len(rows), "new event")

    _run(drive())


# --- live updates (AC3) ------------------------------------------------------------------

def test_an_appended_event_appears_and_the_tail_is_followed(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            table = view(app).table
            assert table.cursor_row == 2
            _add(cfg, campaign="SPEC-0010-x")                 # another process appends
            await ui_until(app, pilot, lambda: ids(app) == [1, 2, 3, 4], "event 4")
            assert table.cursor_row == 3                      # was on the tail: follows it

    _run(drive())


def test_the_cursor_stays_put_when_it_was_not_on_the_tail(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            table = view(app).table
            await pilot.press("k", "k")
            assert table.cursor_row == 0
            _add(cfg, campaign="SPEC-0010-x")
            await ui_until(app, pilot, lambda: ids(app) == [1, 2, 3, 4], "event 4")
            assert table.cursor_row == 0
            await pilot.press("G")                            # G: back to the tail
            assert table.cursor_row == 3
            _add(cfg, campaign="SPEC-0011-x")
            await ui_until(app, pilot, lambda: ids(app)[-1] == 5, "event 5")
            assert table.cursor_row == 4

    _run(drive())


def test_a_truncated_or_replaced_file_reloads_in_full(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg, 4)
    app = _desk(cfg, tab="events")
    path = EV.events_path(cfg)

    def record(i, text):
        return json.dumps({"schema": EV.SCHEMA, "id": i, "ts": "2026-10-25T10:00:00+00:00",
                           "type": "admitted", "campaign": text, "data": {}}) + "\n"

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            assert ids(app) == [1, 2, 3, 4]
            path.write_text(record(1, "SPEC-0001-new"))                   # truncated
            await ui_until(app, pilot, lambda: ids(app) == [1], "truncation reload")
            assert view(app).table.get_row_at(0)[3].plain == "SPEC-0001-new"
            other = path.with_name("events.new")
            other.write_text("".join(record(i, "SPEC-0002-longer-campaign-name-" + "y" * 40)
                                     for i in (1, 2)))
            other.replace(path)                                           # replaced, bigger
            await ui_until(app, pilot, lambda: ids(app) == [1, 2], "replacement reload")
            assert view(app).table.get_row_at(0)[3].plain.startswith("SPEC-0002-longer")
            path.unlink()                                                 # removed
            await ui_until(app, pilot, lambda: ids(app) == [], "empty reload")
            assert not [n for n, k in app.notes if k.get("severity") == "error"]
            assert "events failed" not in status(app)
            _add(cfg, campaign="SPEC-0003-x")
            await ui_until(app, pilot, lambda: ids(app) == [1], "new stream")

    _run(drive())



def test_an_in_place_rewrite_with_older_ids_reloads_in_full(tmp_path):
    """Same inode, bigger file, but every id is below the last loaded one: the incremental
    read finds nothing, and the fallback re-reads the file from 0."""
    cfg = _cfg(tmp_path)
    _seed(cfg, 4)
    app = _desk(cfg, tab="events")
    path = EV.events_path(cfg)

    def record(i):
        return json.dumps({"schema": EV.SCHEMA, "id": i, "ts": "2026-10-25T10:00:00+00:00",
                           "type": "admitted", "campaign": "SPEC-0002-rewritten-" + "z" * 400,
                           "data": {}}) + "\n"

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            assert ids(app) == [1, 2, 3, 4]
            before = os.stat(path)
            with open(path, "w", encoding="utf-8") as f:                  # in place: same inode
                f.write(record(1) + record(2))
            after = os.stat(path)
            assert after.st_ino == before.st_ino and after.st_size > before.st_size
            await ui_until(app, pilot, lambda: ids(app) == [1, 2], "fallback reload")
            assert view(app).table.get_row_at(0)[3].plain.startswith("SPEC-0002-rewritten")
            assert "events failed" not in status(app)

    _run(drive())


def test_a_record_without_a_type_is_skipped_not_fatal(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg, 2)
    with open(EV.events_path(cfg), "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": 7, "campaign": "X"}) + "\n")
        f.write(json.dumps({"campaign": "Y"}) + "\n")
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            assert ids(app) == [1, 2]
            assert "events failed" not in status(app)
            EV.append(cfg, "admitted", campaign="SPEC-0010-x", by="test", data={})
            await ui_until(app, pilot, lambda: len(ids(app)) == 3, "event after the bad lines")

    _run(drive())


def test_a_malformed_ping_does_not_end_the_desk(tmp_path):
    cfg = _cfg(tmp_path)
    EV.events_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    with open(EV.events_path(cfg), "a", encoding="utf-8") as f:     # hand-edited records
        for i, data in enumerate(({"blocked": ["SPEC-1"], "stuck": [3, None], "parked": ["p"],
                                   "ready_not_admitted": [["a"]], "counts": {}},
                                  {"blocked": {"campaign": "A"}, "counts": [1, 2]}), 1):
            f.write(json.dumps({"schema": EV.SCHEMA, "id": i, "type": "conductor_ping",
                                "ts": "2026-10-25T10:00:00+00:00", "data": data}) + "\n")
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            assert panel(app, "ping").startswith("active 0")
            assert app.is_running
            assert not [n for n, k in app.notes if k.get("severity") == "error"]
            assert ids(app) == [1]              # the record summary_line cannot read is skipped

    _run(drive())


# --- filters (AC2) -----------------------------------------------------------------------

def _filter_fixture(cfg):
    _add(cfg, "admitted", "SPEC-0230-a")
    _add(cfg, "parked", "SPEC-0230-a", reason="r", detail="d", resume_to="X")
    _add(cfg, "merged", "SPEC-0231-b", merged_sha="abc")
    _add(cfg, "runner_dead", active=1, queued=0)
    _add(cfg, "parked", "SPEC-0231-b", reason="r", detail="d", resume_to="X")


def test_filter_bar_applies_reports_errors_and_closes_on_escape(tmp_path):
    cfg = _cfg(tmp_path)
    _filter_fixture(cfg)
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v = view(app)
            box = app.query_one("#events-filter")
            assert "5/5" in status(app) and "filter: none" in status(app)
            await press(app, pilot, ["f"])
            assert box.has_class("visible") and app.focused is box
            await press(app, pilot, [*"type=parked", "enter"])
            assert not box.has_class("visible") and app.focused is v.table
            assert ids(app) == [2, 5]
            assert "filter: type=parked" in status(app) and "2/5" in status(app)
            # an invalid filter: hint line, old filter kept, bar stays open
            await press(app, pilot, ["f"])
            assert box.value == "type=parked"
            await typed(app, pilot, "type=zzz")
            assert "zzz" in str(app.query_one("#hint").render())
            assert box.has_class("visible") and ids(app) == [2, 5]
            assert v.event_filter == AM.EventFilter(frozenset({"parked"}))
            await press(app, pilot, ["escape"])
            assert not box.has_class("visible") and app.focused is v.table
            assert ids(app) == [2, 5] and "filter: type=parked" in status(app)
            # a combined filter, and clearing it
            await press(app, pilot, ["f"])
            await typed(app, pilot, "campaign=0231 alerts")
            assert ids(app) == [] and "0/5" in status(app)
            assert str(app.query_one("#hint").render()).strip() == ""
            await press(app, pilot, ["f"])
            await typed(app, pilot, "campaign=0231")
            assert ids(app) == [3, 5]
            await press(app, pilot, ["f"])
            await typed(app, pilot, "")
            assert ids(app) == [1, 2, 3, 4, 5] and "filter: none" in status(app)

    _run(drive())


def test_x_toggles_alerts_only_and_c_toggles_the_campaign(tmp_path):
    cfg = _cfg(tmp_path)
    _filter_fixture(cfg)
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v = view(app)
            await press(app, pilot, ["x"])
            assert ids(app) == [4] and v.event_filter.alerts_only and "1/5" in status(app)
            await press(app, pilot, ["x"])
            assert ids(app) == [1, 2, 3, 4, 5] and not v.event_filter.alerts_only
            v.table.move_cursor(row=2)                       # merged, SPEC-0231-b
            await press(app, pilot, ["c"])
            assert v.event_filter.campaign == "SPEC-0231-b" and ids(app) == [3, 5]
            assert "campaign=SPEC-0231-b" in status(app) and "2/5" in status(app)
            await press(app, pilot, ["c"])
            assert v.event_filter.campaign is None and ids(app) == [1, 2, 3, 4, 5]
            v.table.move_cursor(row=3)                       # runner_dead: no campaign
            await press(app, pilot, ["c"])
            assert v.event_filter.campaign is None
            assert ("no campaign on this event", {}) in app.notes

    _run(drive())


def test_new_events_respect_the_active_filter(tmp_path):
    cfg = _cfg(tmp_path)
    _filter_fixture(cfg)
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await press(app, pilot, ["x"])
            _add(cfg, "admitted", "SPEC-0232-c")
            _add(cfg, "lease_stale", pid=1, heartbeat_age_s=99)
            await ui_until(app, pilot, lambda: ids(app) == [4, 7], "alert 7")
            assert "2/7" in status(app)

    _run(drive())


# --- side panels (AC4-AC6) ---------------------------------------------------------------

def test_the_side_panels_are_populated_without_any_sync(tmp_path):
    cfg = _cfg(tmp_path)
    _campaign(cfg, "SPEC-0900-20261025-aaaa", "IMPLEMENTING")        # active, no lease held
    _add(cfg, "admitted", "SPEC-0900-20261025-aaaa")
    EV.cursor_set(cfg, "conductor", 0)
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await ui_until(app, pilot, lambda: "runner_dead" in panel(app, "conditions"), "alerts")
            assert "runner_dead - active=1 queued=0" in panel(app, "conditions")
            assert panel(app, "ping") == "no conductor_ping yet (interval 600s)"
            assert "conductor events pid - lag 1" in panel(app, "followers")
            assert "dead" in panel(app, "followers")
            assert "stream never synced" in status(app)
            assert "no live follower: the stream may lag, conditions are live" in status(app)
            assert not EV.events_path(cfg).read_text().count("runner_dead")   # nothing appended

    _run(drive())


def test_no_alerts_and_a_ping_digest_and_a_live_follower(tmp_path):
    cfg = _cfg(tmp_path)
    EV.append(cfg, "conductor_ping", by="watchdog", data={
        "blocked": [{"campaign": "SPEC-0002-b", "blocked_for_s": 1500}], "parked": [],
        "stuck": [], "waiting": [], "ready_not_admitted": [],
        "counts": {"active": 1, "queued": 2, "ready": 1, "blocked": 1, "parked": 0,
                   "undecided_parks": 0, "stuck": 0, "waiting": 0}})
    EV.sync(cfg)                       # the test plays the follower; the tab never would
    EV.cursor_set(cfg, "conductor", 1)
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            with EV.reader_lock(cfg, "conductor"):
                await settle(app, pilot)
                await ui_until(app, pilot, lambda: "no alerts" in panel(app, "conditions"))
                ping = panel(app, "ping")
                assert ping.splitlines()[0].startswith("active 1 · queued 2 · ready 1 · blocked 1")
                assert "blocked SPEC-0002-b 25m" in ping and "ago" in ping.splitlines()[-1]
                assert "pid " in panel(app, "followers")
                assert "dead" not in panel(app, "followers")
                assert "stream synced" in status(app) and "no live follower" not in status(app)

    _run(drive())


def test_a_held_follower_with_a_stale_cursor_is_flagged_lagging(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg, 3)
    EV.sync(cfg)                       # the test plays the follower
    EV.cursor_set(cfg, "conductor", 1)
    EV.cursor_set(cfg, "steward", 3)
    stale = (dt.datetime.now().astimezone() - dt.timedelta(seconds=300)).isoformat(
        timespec="seconds")
    path = EV.cursors_dir(cfg) / "conductor.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "ts": stale}))
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            with EV.reader_lock(cfg, "conductor"):
                await settle(app, pilot)
                await ui_until(app, pilot, lambda: "conductor" in panel(app, "followers"))
                lines = {ln.split()[0]: ln for ln in panel(app, "followers").splitlines()}
                assert "lag 2" in lines["conductor"] and lines["conductor"].endswith("lagging")
                assert "dead" not in lines["conductor"]
                assert lines["steward"].endswith("dead")        # not held
                assert "no live follower" not in status(app)

    _run(drive())


def test_a_new_ping_updates_the_panel(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg, 1)
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            assert panel(app, "ping").startswith("no conductor_ping yet")
            EV.append(cfg, "conductor_ping", by="watchdog", data={
                "blocked": [], "parked": [], "stuck": [], "waiting": [],
                "ready_not_admitted": [],
                "counts": {"active": 7, "queued": 0, "ready": 0, "blocked": 0, "parked": 0,
                           "undecided_parks": 0, "stuck": 0, "waiting": 0}})
            await ui_until(app, pilot, lambda: panel(app, "ping").startswith("active 7"), "ping")

    _run(drive())


# --- enter and read-only (AC7) -----------------------------------------------------------

def test_enter_posts_campaign_selected_only_with_a_campaign(tmp_path):
    cfg = _cfg(tmp_path)
    _add(cfg, "admitted", "SPEC-0001-a")
    _add(cfg, "runner_dead", active=1, queued=0)
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v = view(app)
            v.table.move_cursor(row=1)
            await press(app, pilot, ["enter"])
            await settle(app, pilot)
            assert app.notes == []
            assert app.screen.__class__.__name__ != "CampaignScreen"
            v.table.move_cursor(row=0)
            await press(app, pilot, ["enter"])
            await settle(app, pilot)
            # SPEC-0246 replaced the CLI hint with the campaign screen
            assert app.screen.__class__.__name__ == "CampaignScreen"
            assert app.screen.campaign_id == "SPEC-0001-a"

    _run(drive())


def test_the_tab_never_syncs_or_takes_a_cursor_or_reader(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _campaign(cfg, "SPEC-0900-20261025-aaaa", "IMPLEMENTING")
    _filter_fixture(cfg)
    EV.cursor_set(cfg, "conductor", 0)

    def forbidden(name):
        def f(*a, **k):
            raise AssertionError(f"{name} must not be called from the events tab")
        return f
    for mod, names in ((EV, ("sync", "cursor_set", "reader_lock", "follow")), (MB, ("follow",))):
        for n in names:
            monkeypatch.setattr(mod, n, forbidden(f"{mod.__name__}.{n}"))
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await ui_until(app, pilot, lambda: "runner_dead" in panel(app, "conditions"), "alerts")
            # the followers path swallows exceptions into "followers failed": it must not have
            await ui_until(app, pilot, lambda: "conductor" in panel(app, "followers"), "followers")
            assert "failed" not in panel(app, "followers")
            assert "failed" not in panel(app, "conditions")
            assert "stream never synced" in status(app)
            _add(cfg, "admitted", "SPEC-0300-x")
            await ui_until(app, pilot, lambda: ids(app)[-1] == 6, "event 6")
            await press(app, pilot, ["x", "x", "c", "c", "G", "f", *"type=parked", "enter",
                                     "f", "escape", "enter"])
            await pilot.press("1")
            await pilot.press("5")
            await settle(app, pilot)
            assert not [n for n, k in app.notes if k.get("severity") == "error"]
            assert "events failed" not in status(app)
            assert "failed" not in panel(app, "followers")
            assert "stream never synced" in status(app)

    _run(drive())


def test_timers_pause_when_the_tab_is_left(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed(cfg, 1)
    calls = []
    real = AM.events_signature
    monkeypatch.setattr(AM, "events_signature", lambda c: calls.append(1) or real(c))
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await pilot.press("1")
            await settle(app, pilot)
            n = len(calls)
            await pilot.pause(0.4)
            assert len(calls) == n
            await pilot.press("5")
            await settle(app, pilot)
            assert await pilot_until(pilot, lambda: len(calls) > n)

    _run(drive())


# --- the 5 s side-panel refresh (no key press after the tab opens) --------------------------

def test_side_panels_refresh_on_their_own_after_the_tab_opens(tmp_path):
    cfg = _cfg(tmp_path)
    EV.cursor_set(cfg, "conductor", 0)             # a follower row that starts out not held
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await ui_until(app, pilot, lambda: "no alerts" in panel(app, "conditions"), "no alerts")
            await ui_until(app, pilot, lambda: "dead" in panel(app, "followers"), "dead follower")
            assert "no live follower" in status(app)
            # State changes after the tab opened; no key press from here on.
            _campaign(cfg, "SPEC-0900-20261025-aaaa", "IMPLEMENTING")   # active, no lease
            await ui_until(app, pilot, lambda: "runner_dead" in panel(app, "conditions"),
                        "runner_dead")
            with EV.reader_lock(cfg, "conductor"):
                await ui_until(app, pilot, lambda: "dead" not in panel(app, "followers"),
                            "follower no longer dead")
                await ui_until(app, pilot, lambda: "no live follower" not in status(app),
                            "warning gone")
            await ui_until(app, pilot, lambda: "dead" in panel(app, "followers"),
                        "dead again once released")
            assert not EV.events_path(cfg).exists() or "runner_dead" not in \
                EV.events_path(cfg).read_text()

    _run(drive())


def test_the_side_timer_pauses_off_tab_and_resumes_on_five(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed(cfg, 1)
    calls = []
    real = AM.load_conditions
    monkeypatch.setattr(AM, "load_conditions", lambda c: calls.append(1) or real(c))
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await ui_until(app, pilot, lambda: len(calls) >= 3, "side timer ticking")
            await pilot.press("1")
            await settle(app, pilot)
            await pilot.pause(0.3)                  # let an in-flight reload finish
            n = len(calls)
            await pilot.pause(0.8)                  # four SIDE_S periods
            assert len(calls) == n
            await pilot.press("5")
            await ui_until(app, pilot, lambda: len(calls) >= n + 3, "side timer resumed")

    _run(drive())


def test_a_failing_load_is_shown_and_the_next_poll_recovers(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed(cfg, 2)
    broken = {"on": True}
    real_events, real_conditions = AM.load_events, AM.load_conditions

    def flaky_events(*a, **k):
        if broken["on"]:
            raise OSError("disk gone")
        return real_events(*a, **k)

    def flaky_conditions(*a, **k):
        if broken["on"]:
            raise OSError("ledger gone")
        return real_conditions(*a, **k)

    monkeypatch.setattr(AM, "load_events", flaky_events)
    monkeypatch.setattr(AM, "load_conditions", flaky_conditions)
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await ui_until(app, pilot, lambda: "conditions failed: ledger gone"
                        in panel(app, "conditions"), "conditions failure shown")
            await ui_until(app, pilot, lambda: "events failed: disk gone" in status(app),
                        "events failure shown")
            assert ids(app) == []                  # the desk is still up and responsive
            broken["on"] = False
            await ui_until(app, pilot, lambda: "no alerts" in panel(app, "conditions"),
                        "conditions recovered")
            await ui_until(app, pilot, lambda: ids(app) == [1, 2]
                        and "events failed" not in status(app), "events recovered")
            assert not [n for n, k in app.notes if k.get("severity") == "error"]

    _run(drive())


# --- bad data (SPEC-0265) ----------------------------------------------------------------

def _raw(cfg, *records):
    with open(EV.events_path(cfg), "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps({"type": "admitted", "ts": "2026-10-27T10:00:00+00:00",
                                "campaign": None, "by": "t", "data": {}, **rec}) + "\n")


def test_duplicate_ids_and_bad_ts_do_not_crash(tmp_path):
    cfg = _cfg(tmp_path)
    _raw(cfg, {"id": 4, "campaign": "SPEC-0004-x"}, {"id": 5, "campaign": "SPEC-0005-a"},
         {"id": 5, "campaign": "SPEC-0005-b"},
         {"id": 6, "campaign": "SPEC-0006-x", "ts": "9999-12-31T23:59:59-14:00"},
         {"id": 7, "campaign": "SPEC-0007-x"})
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v = view(app)
            assert app.is_running
            assert ids(app) == [4, 5, 6, 7]
            assert v.table.row_count == len(v.shown) == 4
            assert "1 duplicate ids skipped" in status(app)
            v.table.move_cursor(row=2)                 # the row after the duplicate
            assert v.current_row().id == 6 and v.table.get_row_at(2)[3].plain == "SPEC-0006-x"
            _raw(cfg, {"id": 8, "campaign": "SPEC-0008-x"}, {"id": 8, "campaign": "SPEC-0008-y"})
            await until(app, pilot, lambda: ids(app) == [4, 5, 6, 7, 8], "event 8")
            assert v.table.row_count == len(v.shown) == 5
            assert "2 duplicate ids skipped" in status(app)

    _run(drive())


def test_append_failure_is_retried(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed(cfg, 3)
    real = ET.EventsView._repaint
    calls, at_retry = [], []

    def flaky(self, **kw):
        calls.append(1)
        at_retry.append((self._last_id, self._signature))   # what the failed load left behind
        if len(calls) == 1:
            self.table.add_row("x", "x", "x", "x", "x", key="partial")    # a half-painted table
            raise TypeError("paint failed")
        return real(self, **kw)

    monkeypatch.setattr(ET.EventsView, "_repaint", flaky)
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause(0.1)
            v = view(app)
            await until(app, pilot, lambda: ids(app) == [1, 2, 3], "retried rows")
            assert app.is_running and len(calls) >= 2
            assert at_retry[1] == (0, None)             # the failed append consumed nothing
            assert v._last_id == 3 and v._signature is not None
            assert v.table.row_count == 3 and "events failed" not in status(app)

    _run(drive())


def test_append_failure_after_a_reset_restores_the_old_rows_and_retries(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed(cfg, 3)
    app = _desk(cfg, tab="events")
    path = EV.events_path(cfg)
    real_repaint, real_load = ET.EventsView._repaint, AM.load_events
    armed, seen = [], []

    def flaky(self, **kw):
        if armed:
            armed.clear()
            self.table.add_row("x", "x", "x", "x", "x", key="partial")
            raise TypeError("paint failed")
        return real_repaint(self, **kw)

    def spy(cfg, since, limit):
        v = view(app)
        seen.append((since, len(v.loaded), v._dirty, v._last_id))
        return real_load(cfg, since, limit)

    monkeypatch.setattr(ET.EventsView, "_repaint", flaky)

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            assert ids(app) == [1, 2, 3]
            monkeypatch.setattr(AM, "load_events", spy)
            armed.append(1)
            path.write_text(json.dumps({"schema": EV.SCHEMA, "id": 1, "type": "admitted",
                                        "ts": "2026-10-25T10:00:00+00:00",
                                        "campaign": "SPEC-0001-new", "data": {}}) + "\n")
            await until(app, pilot, lambda: len(seen) >= 2, "retry load")
            assert seen[0][0] == 0                              # the reset load
            assert seen[1][1:] == (3, True, 3)                  # old rows back, rebuild pending
            await until(app, pilot, lambda: ids(app) == [1], "rebuilt rows")
            v = view(app)
            assert v.table.row_count == 1 and v.table.get_row_at(0)[3].plain == "SPEC-0001-new"
            assert "events failed" not in status(app)

    _run(drive())


def test_a_trailing_lower_duplicate_id_does_not_inflate_the_count(tmp_path):
    cfg = _cfg(tmp_path)
    _raw(cfg, *({"id": i, "campaign": f"SPEC-{i:04d}-x"} for i in range(1, 6)),
         {"id": 1, "campaign": "SPEC-0001-dup"})
    app = _desk(cfg, tab="events")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v = view(app)
            assert ids(app) == [1, 2, 3, 4, 5] and v._last_id == 5
            assert "1 duplicate ids skipped" in status(app)
            _raw(cfg, {"id": 6, "campaign": "SPEC-0006-x"})
            await until(app, pilot, lambda: ids(app) == [1, 2, 3, 4, 5, 6], "event 6")
            assert "1 duplicate ids skipped" in status(app)

    _run(drive())
