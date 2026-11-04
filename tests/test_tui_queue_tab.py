"""SPEC-0241: the TUI queue tab (Textual pilot tests)."""
import asyncio
import json
import subprocess

import pytest

from rich.text import Text

from test_agentic_dashboard import _campaign, _fixture, _iso, _park_fixture
from test_agentic_e2e import run_cli
from nightwright.agentic import events as EV
from nightwright.agentic import lease as L
from nightwright.agentic import mailbox as MB
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S
from nightwright.tui import agentic_model as AM
from nightwright.tui import queue_tab as QT
from waits import pilot_until

SIZE = (150, 40)


def _sub(tmp_path, name):
    (tmp_path / name).mkdir()
    return tmp_path / name


def _desk(cfg, **kw):
    from nightwright.tui.app import IdeaDesk
    app = IdeaDesk(cfg, **kw)
    app.notes = []
    app.notify = lambda msg, **k: app.notes.append((msg, k))
    return app


def _run(coro):
    return asyncio.run(coro)


async def settle(app, pilot, n=3):
    for _ in range(n):
        await pilot.pause(0.05)
        await asyncio.wait_for(app.workers.wait_for_complete(), 10)
    await pilot.pause(0.05)


def view(app):
    from nightwright.tui.queue_tab import QueueView
    return app.query_one("#queue-view", QueueView)


def select(app, group, campaign):
    v = view(app)
    i = next(i for i, r in enumerate(v.rows) if (r.group, r.campaign) == (group, campaign))
    v.table.move_cursor(row=i)
    return i


async def open_queue(app, pilot):
    await pilot.press("3")
    await settle(app, pilot)


def state_of(cfg, cid):
    return S.read_state(cfg, cid)


class spy_titles:
    """Record every ChoiceScreen (title, choices) while the context is open."""
    def __enter__(self):
        from nightwright.tui.app import ChoiceScreen
        self.titles, self.cls = [], ChoiceScreen
        self.real = ChoiceScreen.__init__
        titles, real = self.titles, self.real

        def spy(this, title, choices, current=""):
            titles.append((title, list(choices)))
            real(this, title, choices, current)
        ChoiceScreen.__init__ = spy
        return self.titles

    def __exit__(self, *exc):
        self.cls.__init__ = self.real


def snapshot(cfg, cid):
    st = state_of(cfg, cid)
    park = {k: v for k, v in (st.get("park") or {}).items() if k != "since"}  # ignore timestamps
    return (st["state"], st.get("priority"), json.dumps(park, sort_keys=True),
            bool(st.get("park_requested")), json.dumps(st.get("pending_brief"), sort_keys=True))


# --- tab plumbing -------------------------------------------------------------------------

def test_number_keys_h_and_click_switch_tabs(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg)

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            tabs = app.query_one("#tabs")
            assert tabs.active == "tab-ideas"
            await pilot.press("3")
            await settle(app, pilot)
            assert tabs.active == "tab-queue" and app.focused is view(app).table
            assert view(app).rows and view(app).header.startswith("runner ")
            await pilot.press("1")
            await settle(app, pilot)
            assert tabs.active == "tab-ideas" and app.focused.id == "list"
            await pilot.press("2")
            await settle(app, pilot)
            assert tabs.active == "tab-hub"
            await pilot.press("h")
            await settle(app, pilot)
            assert tabs.active == "tab-ideas"

    _run(drive())


def test_tab_kwarg_opens_the_queue_tab(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            assert app.query_one("#tabs").active == "tab-queue"
            assert app.focused is view(app).table and view(app).rows

    _run(drive())


def test_mouse_click_on_the_queue_tab_loads_and_focuses(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg)

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await pilot.click("ContentTab#--content-tab-tab-queue")
            await settle(app, pilot)
            assert app.query_one("#tabs").active == "tab-queue"
            assert app.focused is view(app).table and view(app).rows

    _run(drive())


def test_idea_keys_do_nothing_on_the_queue_tab(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    assert run_cli(cfg, monkeypatch, ["idea", "keep me untouched"]).exit_code == 0
    files = [p for p in (tmp_path / "org").iterdir() if p.is_file()]
    assert files
    before = {p: p.read_bytes() for p in files}
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            depth = len(app.screen_stack)
            for key in ("x", "i", "X", "l", "c", "t", "m", "s", "a"):
                await pilot.press(key)
                await settle(app, pilot, 1)
                assert len(app.screen_stack) == depth, key
            # the globals still work
            await pilot.press("1")
            await settle(app, pilot)
            assert app.query_one("#tabs").active == "tab-ideas"

    _run(drive())
    assert {p: p.read_bytes() for p in files} == before


# --- live refresh -------------------------------------------------------------------------

def test_state_write_reloads_without_a_key_and_keeps_the_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(QT, "POLL_S", 0.1)
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v = view(app)
            select(app, "queued", ids["blocked"])
            before = v.current_row()
            assert before.pri == "3"
            Q.apply_prioritize(cfg, Q.plan_prioritize(cfg, ids["blocked"], 1))
            await pilot.pause(0.5)
            await settle(app, pilot)
            row = v.current_row()
            assert row.campaign == ids["blocked"] and row.pri == "1"
            assert row.priority == 1

    _run(drive())


def test_status_line_renders_the_header(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            text = str(app.query_one("#queue-status").render())
            assert view(app).header.startswith("runner down · hb -s · slots ")
            assert view(app).header in text

    _run(drive())


def test_no_reload_while_the_signature_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(QT, "POLL_S", 0.05)
    monkeypatch.setattr(QT, "RELOAD_S", 100.0)
    repo, cfg, ids = _fixture(tmp_path)
    calls = []
    real = AM.load_queue
    monkeypatch.setattr(AM, "load_queue", lambda *a, **k: calls.append(1) or real(*a, **k))
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            n = len(calls)
            assert n >= 1
            await pilot.pause(0.5)
            assert len(calls) == n

    _run(drive())


def test_periodic_reload_runs_without_a_write(tmp_path, monkeypatch):
    monkeypatch.setattr(QT, "POLL_S", 100.0)
    monkeypatch.setattr(QT, "RELOAD_S", 0.1)
    repo, cfg, ids = _fixture(tmp_path)
    calls = []
    real = AM.load_queue
    monkeypatch.setattr(AM, "load_queue", lambda *a, **k: calls.append(1) or real(*a, **k))
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            assert await pilot_until(pilot, lambda: len(calls) >= 3)

    _run(drive())


def test_load_error_is_shown_and_rows_are_kept(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v = view(app)
            rows = list(v.rows)

            def boom(*a, **k):
                raise ValueError("disk on fire")
            monkeypatch.setattr(AM, "load_queue", boom)
            await pilot.press("r")
            await settle(app, pilot)
            text = str(app.query_one("#queue-status").render())
            assert "queue failed: disk on fire" in text
            assert v.rows == rows and v.table.row_count == len(rows)

    _run(drive())


def test_timers_pause_when_the_tab_is_left(tmp_path, monkeypatch):
    monkeypatch.setattr(QT, "POLL_S", 0.05)
    monkeypatch.setattr(QT, "RELOAD_S", 0.05)
    repo, cfg, ids = _fixture(tmp_path)
    calls = []
    real = AM.load_queue
    monkeypatch.setattr(AM, "load_queue", lambda *a, **k: calls.append(1) or real(*a, **k))
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await pilot.press("1")
            await settle(app, pilot)
            n = len(calls)
            await pilot.pause(0.4)
            assert len(calls) == n

    _run(drive())


# --- read-only by construction (AC5) -----------------------------------------------------

def test_the_tab_never_touches_followers(tmp_path, monkeypatch):
    monkeypatch.setattr(QT, "POLL_S", 0.05)         # the signature poll runs under the patches too
    repo, cfg, ids = _fixture(tmp_path)

    def forbidden(name):
        def f(*a, **k):
            raise AssertionError(f"{name} must not be called from the queue tab")
        return f
    for mod, names in ((EV, ("sync", "cursor_set", "reader_lock", "follow")), (MB, ("follow",))):
        for n in names:
            monkeypatch.setattr(mod, n, forbidden(f"{mod.__name__}.{n}"))
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await pilot.press("r")
            await settle(app, pilot)
            for action, group, cid, keys in (
                    ("park", "queued", ids["ready"], ["p", *"why", "enter", "enter"]),
                    ("prioritize", "queued", ids["blocked"], ["P", "enter", "escape"]),
                    ("prioritize", "queued", ids["blocked"], ["P", "enter", "enter"]),
                    ("abandon", "queued", ids["blocked"], ["A", "enter"]),
                    ("resume", "conductor", ids["parked"], ["u", "enter", "enter"])):
                await act(app, pilot, group, cid, keys)
            assert not [n for n, k in app.notes if k.get("severity") == "error"]
            assert state_of(cfg, ids["ready"])["state"] == "PARKED"
            assert state_of(cfg, ids["blocked"])["state"] == "ABANDONED"

    _run(drive())


# --- actions (AC6) -------------------------------------------------------------------------

async def press(app, pilot, keys):
    """One key at a time with a pause: a modal pushed by the action worker needs a moment to
    mount before the next key can reach it."""
    for key in keys:
        await pilot.press("space" if key == " " else key)
        await pilot.pause(0.15)


async def act(app, pilot, group, cid, keys):
    select(app, group, cid)
    await press(app, pilot, keys)
    await settle(app, pilot)


def test_park_immediate_confirm_cancel_and_esc(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            before = snapshot(cfg, cid)
            await act(app, pilot, "queued", cid, ["p", "escape"])          # esc at the prompt
            assert snapshot(cfg, cid) == before and len(app.screen_stack) == 1
            await act(app, pilot, "queued", cid, ["p", *"nope", "enter", "escape"])  # esc at confirm
            assert snapshot(cfg, cid) == before
            await act(app, pilot, "queued", cid, ["p", *"nope", "enter", "j", "enter"])  # cancel
            assert snapshot(cfg, cid) == before
            await act(app, pilot, "queued", cid, ["p", "enter"])           # empty reason
            assert snapshot(cfg, cid) == before
            assert any(k.get("severity") == "error" for _, k in app.notes)
            await act(app, pilot, "queued", cid, ["p", *"hold", "enter", "enter"])
            st = state_of(cfg, cid)
            assert st["state"] == "PARKED" and st["park"]["detail"] == "hold"
            assert (f"park {cid} done", {}) in [(m, {}) for m, _ in app.notes]

    _run(drive())


def test_park_confirm_titles_show_the_plan_summary(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    monkeypatch.setattr(L, "is_held", lambda *a, **k: True)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            with spy_titles() as titles:
                await act(app, pilot, "queued", ids["ready"], ["p", *"hold", "enter", "escape"])
                await act(app, pilot, "building", ids["impl"], ["p", *"later", "enter", "escape"])
            ready, impl = (Q.plan_park(cfg, ids[k], "x") for k in ("ready", "impl"))
            assert (ready["mode"], impl["mode"]) == ("immediate", "deferred")
            assert titles == [(f"park {ids['ready']} (immediate): {ready['note']}", ["yes", "cancel"]),
                              (f"park {ids['impl']} (deferred): {impl['note']}", ["yes", "cancel"])]

    _run(drive())


def test_park_matches_the_cli(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(_sub(tmp_path, "a"))
    repo2, cfg2, ids2 = _fixture(_sub(tmp_path, "b"))
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await act(app, pilot, "queued", ids["ready"], ["p", *"same", "enter", "enter"])

    _run(drive())
    assert run_cli(cfg2, monkeypatch, ["queue", "park", ids2["ready"], "--reason", "same",
                                       "--yes"]).exit_code == 0
    assert snapshot(cfg, ids["ready"]) == snapshot(cfg2, ids2["ready"])


def test_deferred_park_notifies_requested_and_the_row_is_listed_twice(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    monkeypatch.setattr(L, "is_held", lambda *a, **k: True)
    cid = ids["impl"]
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await act(app, pilot, "building", cid, ["p", *"later", "enter", "enter"])
            st = state_of(cfg, cid)
            assert st["state"] == "IMPLEMENTING" and st["park_requested"]["detail"] == "later"
            assert any(m.startswith(f"park requested for {cid}") for m, _ in app.notes)
            assert not any(m == f"park {cid} done" for m, _ in app.notes)
            rows = [(r.group, r.campaign) for r in view(app).rows]
            assert ("conductor", cid) in rows and ("building", cid) in rows
            assert len(rows) == len(set(rows))
            # cursor restore: stays on the same (group, campaign) across a reload
            select(app, "building", cid)
            idx = view(app).table.cursor_row
            view(app).reload()
            await settle(app, pilot)
            assert view(app).table.cursor_row == idx
            assert (view(app).current_row().group, view(app).current_row().campaign) == (
                "building", cid)
            # and a second park is not offered
            await act(app, pilot, "building", cid, ["p"])
            assert any("park not available" in m for m, _ in app.notes)
            assert len(app.screen_stack) == 1

    _run(drive())


def test_cursor_falls_back_to_the_campaign_when_its_group_goes(tmp_path):
    repo, cfg, ids = _park_fixture(tmp_path)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            cid = ids["req"]
            select(app, "conductor", cid)
            st = state_of(cfg, cid)
            st.pop("park_requested")
            S.write_state(cfg, cid, st)
            view(app).reload()
            await settle(app, pilot)
            row = view(app).current_row()
            assert row.campaign == cid and row.group == "building"

    _run(drive())


@pytest.mark.parametrize("note,keys", [("", ["u", "enter", "enter"]),
                                       ("try again", ["u", *"try again", "enter", "enter"])])
def test_resume_with_and_without_a_note(tmp_path, monkeypatch, note, keys):
    repo, cfg, ids = _fixture(_sub(tmp_path, "a"))
    repo2, cfg2, ids2 = _fixture(_sub(tmp_path, "b"))
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await act(app, pilot, "conductor", ids["parked"], keys)

    _run(drive())
    cli = ["queue", "resume", ids2["parked"], "--yes"] + (["--note", note] if note else [])
    assert run_cli(cfg2, monkeypatch, cli).exit_code == 0
    mine, theirs = state_of(cfg, ids["parked"]), state_of(cfg2, ids2["parked"])
    assert mine["state"] == theirs["state"] != "PARKED"
    assert mine.get("pending_brief") == theirs.get("pending_brief")
    assert bool(mine.get("pending_brief")) == bool(note)


def test_resume_confirm_title_and_slot_wait(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["parked"]
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            with spy_titles() as titles:
                monkeypatch.setattr(Q, "slots_for", lambda *a, **k: 9)
                assert not Q.plan_resume(cfg, cid)["waits_for_slot"]
                await act(app, pilot, "conductor", cid, ["u", "enter", "escape"])
                monkeypatch.setattr(Q, "slots_for", lambda *a, **k: 1)   # impl + gating fill it
                assert Q.plan_resume(cfg, cid)["waits_for_slot"]
                await act(app, pilot, "conductor", cid, ["u", "enter", "escape"])
            assert [t for t, _ in titles] == [
                f"resume {cid} → IMPLEMENTING",
                f"resume {cid} → IMPLEMENTING (waits for a slot)"]
            assert all(c == ["yes", "cancel"] for _, c in titles)

    _run(drive())


def test_resume_cancel_writes_nothing(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["parked"]
    before = snapshot(cfg, cid)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await act(app, pilot, "conductor", cid, ["u", "escape"])
            await act(app, pilot, "conductor", cid, ["u", "enter", "j", "enter"])
            await act(app, pilot, "conductor", cid, ["u", "enter", "escape"])

    _run(drive())
    assert snapshot(cfg, cid) == before


def test_prioritize_picks_a_priority(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(_sub(tmp_path, "a"))
    repo2, cfg2, ids2 = _fixture(_sub(tmp_path, "b"))
    cid = ids["ready"]
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            before = snapshot(cfg, cid)
            await act(app, pilot, "queued", cid, ["P", "escape"])
            await act(app, pilot, "queued", cid, ["P", "k", "k", "enter", "j", "enter"])  # cancel
            assert snapshot(cfg, cid) == before
            with spy_titles() as titles:
                await act(app, pilot, "queued", cid, ["P", "k", "k", "enter", "enter"])
            assert titles[-1] == ("priority 3 → 1", ["yes", "cancel"])

    _run(drive())
    assert run_cli(cfg2, monkeypatch, ["queue", "prioritize", ids2["ready"], "1",
                                       "--yes"]).exit_code == 0
    assert snapshot(cfg, cid) == snapshot(cfg2, ids2["ready"])
    assert state_of(cfg, cid)["priority"] == 1


def _give_worktree(cfg, cid, path):
    st = state_of(cfg, cid)
    path.parent.mkdir(parents=True, exist_ok=True)          # a real worktree, so removal is visible
    subprocess.run(["git", "-C", st["repo"]["root"], "worktree", "add", "-q", "-b",
                    f"nw/{cid}-tui", str(path)], check=True)
    st["repo"]["worktree"] = str(path)
    S.write_state(cfg, cid, st)


def test_abandon_without_a_worktree_offers_no_keep_choice(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    titles = []
    app = _desk(cfg, tab="queue")
    from nightwright.tui.app import ChoiceScreen
    real_init = ChoiceScreen.__init__

    def spy(self, title, choices, current=""):
        titles.append((title, list(choices)))
        real_init(self, title, choices, current)
    ChoiceScreen.__init__ = spy

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await act(app, pilot, "queued", cid, ["A", "enter"])

    try:
        _run(drive())
    finally:
        ChoiceScreen.__init__ = real_init
    assert titles == [(f"abandon {cid}", ["abandon", "cancel"])]
    assert state_of(cfg, cid)["state"] == "ABANDONED"


@pytest.mark.parametrize("keep", [True, False])
def test_abandon_with_a_worktree_offers_keep(tmp_path, keep):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    wt = tmp_path / "wts" / cid
    _give_worktree(cfg, cid, wt)
    titles = []
    from nightwright.tui.app import ChoiceScreen
    real_init = ChoiceScreen.__init__

    def spy(self, title, choices, current=""):
        titles.append((title, list(choices)))
        real_init(self, title, choices, current)
    ChoiceScreen.__init__ = spy
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await act(app, pilot, "queued", cid, ["A", *(["j"] if keep else []), "enter"])

    try:
        _run(drive())
    finally:
        ChoiceScreen.__init__ = real_init
    assert titles[0] == (f"abandon {cid}, removes worktree {wt}",
                         ["abandon", "abandon, keep worktree", "cancel"])
    assert state_of(cfg, cid)["state"] == "ABANDONED"
    events = [json.loads(line) for line in
              (S.campaign_dir(cfg, cid) / "ledger.jsonl").read_text().splitlines()]
    close = [e for e in events if e["type"] == "close"][-1]
    assert bool(close["worktree_removed"]) is (not keep)
    assert wt.is_dir() is keep


def test_abandon_cancel_writes_nothing(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    before = snapshot(cfg, cid)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await act(app, pilot, "queued", cid, ["A", "j", "enter"])
            await act(app, pilot, "queued", cid, ["A", "escape"])

    _run(drive())
    assert snapshot(cfg, cid) == before


def test_a_plan_value_error_is_an_error_notification_and_writes_nothing(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            select(app, "queued", cid)
            st = state_of(cfg, cid)                       # the row goes stale under the cursor
            st["state"] = "MERGED"
            S.write_state(cfg, cid, st)
            before = snapshot(cfg, cid)
            await press(app, pilot, ["p", *"x", "enter"])
            await settle(app, pilot)
            errors = [m for m, k in app.notes if k.get("severity") == "error"]
            assert errors and "nothing to park" in errors[-1]
            assert snapshot(cfg, cid) == before and len(app.screen_stack) == 1

    _run(drive())


def test_an_apply_value_error_is_an_error_notification(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]

    def boom(cfg, plan):
        raise ValueError("raced")
    monkeypatch.setattr(Q, "apply_prioritize", boom)
    before = snapshot(cfg, cid)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await act(app, pilot, "queued", cid, ["P", "k", "enter", "enter"])
            assert [m for m, k in app.notes if k.get("severity") == "error"] == ["raced"]

    _run(drive())
    assert snapshot(cfg, cid) == before


def test_an_action_not_allowed_notifies_and_opens_no_prompt(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            before = snapshot(cfg, ids["merged"])
            for key, name in (("p", "park"), ("u", "resume"), ("P", "prioritize"),
                              ("A", "abandon")):
                app.notes.clear()
                await act(app, pilot, "delivered", ids["merged"], [key])
                assert app.notes[0][0] == f"{name} not available for MERGED"
                assert len(app.screen_stack) == 1
            app.notes.clear()
            await act(app, pilot, "queued", ids["ready"], ["u"])
            assert app.notes[0][0] == "resume not available for QUEUED"
            assert snapshot(cfg, ids["merged"]) == before

    _run(drive())


def test_enter_posts_campaign_selected_and_opens_the_campaign_screen(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="queue")
    seen = []
    from nightwright.tui.queue_tab import QueueView
    real = QueueView.open_selected

    def spy(self):
        seen.append(self.current_row().campaign)
        real(self)
    QueueView.open_selected = spy

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            select(app, "queued", ids["ready"])
            await press(app, pilot, ["enter"])
            await settle(app, pilot)
            # SPEC-0246 replaced the CLI hint with the campaign screen
            assert app.screen.__class__.__name__ == "CampaignScreen"
            assert app.screen.campaign_id == ids["ready"]

    try:
        _run(drive())
    finally:
        QueueView.open_selected = real
    assert seen == [ids["ready"]]


def test_an_empty_queue_shows_the_placeholder(tmp_path):
    from test_agentic_e2e import impl_done, make_cfg, make_repo, script
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            assert [r.title for r in view(app).rows] == ["queue is empty"]
            await press(app, pilot, ["p", "enter"])     # nothing selectable: no prompt, no crash
            await settle(app, pilot)
            assert len(app.screen_stack) == 1

    _run(drive())


# --- bad data and untrusted text (SPEC-0265) ---------------------------------------------

def _status(app):
    return str(view(app).query_one("#queue-status").content)


def _retitled(title):
    """A `load_queue` that returns the real rows with every title replaced."""
    import dataclasses
    real = AM.load_queue

    def load(cfg):
        rows, header = real(cfg)
        return [dataclasses.replace(r, title=title) for r in rows], header
    return load


def test_paint_error_keeps_the_app_alive(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="queue")
    real = AM.load_queue

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            assert view(app).rows
            monkeypatch.setattr(AM, "load_queue", lambda cfg: {}["missing"])   # KeyError
            view(app).reload()
            await settle(app, pilot)
            assert app.is_running and "queue failed" in _status(app)
            monkeypatch.setattr(AM, "load_queue", _retitled(5))    # paint raises in the worker
            view(app).reload()
            await settle(app, pilot)
            assert app.is_running and "queue failed" in _status(app)
            monkeypatch.setattr(AM, "load_queue", real)           # the next reload recovers
            view(app).reload()
            await settle(app, pilot)
            assert "queue failed" not in _status(app) and view(app).table.row_count

    _run(drive())


def test_signature_oserror_is_contained(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="queue")

    def boom(cfg):
        raise OSError("dir vanished")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            monkeypatch.setattr(AM, "queue_signature", boom)
            view(app)._poll()
            await settle(app, pilot)
            assert app.is_running and "queue failed: dir vanished" in _status(app)

    _run(drive())


def test_queue_signature_survives_an_unlistable_campaigns_dir(tmp_path, monkeypatch):
    import pathlib
    repo, cfg, ids = _fixture(tmp_path)

    def boom(self):
        raise OSError("denied")
    monkeypatch.setattr(pathlib.Path, "iterdir", boom)
    assert AM.queue_signature(cfg)[2] == ()


def test_titles_render_verbatim(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    title = "Fix [x] and [/bold] stray"
    monkeypatch.setattr(AM, "load_queue", _retitled(title))
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            table = view(app).table
            assert app.is_running and table.row_count
            first = next(i for i, r in enumerate(view(app).rows) if r.kind == "campaign")
            cells = table.get_row_at(first)
            assert all(isinstance(c, Text) for c in cells)
            assert cells[2].plain == title

    _run(drive())


def test_an_unexpected_action_error_is_an_error_notification(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]

    def boom(cfg, plan):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(Q, "apply_prioritize", boom)
    before = snapshot(cfg, cid)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await act(app, pilot, "queued", cid, ["P", "k", "enter", "enter"])
            assert app.is_running
            errs = [m for m, k in app.notes if k.get("severity") == "error"]
            assert len(errs) == 1 and errs[0].startswith("queue action failed")
            assert "RuntimeError" in errs[0] and not view(app)._busy

    _run(drive())
    assert snapshot(cfg, cid) == before


def test_a_failed_reload_is_retried_by_the_next_poll(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="queue")
    real = AM.load_queue

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            monkeypatch.setattr(AM, "load_queue", lambda cfg: {}["missing"])
            view(app).reload()
            await settle(app, pilot)
            assert "queue failed" in _status(app)
            monkeypatch.setattr(AM, "load_queue", real)    # nothing changes on disk
            view(app)._poll()
            await settle(app, pilot)
            assert "queue failed" not in _status(app) and view(app).table.row_count

    _run(drive())


def test_a_value_error_notification_is_escaped(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]

    def boom(cfg, plan):
        raise ValueError("bad [/oops] path")
    monkeypatch.setattr(Q, "apply_prioritize", boom)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await act(app, pilot, "queued", cid, ["P", "k", "enter", "enter"])
            assert app.is_running
            errs = [m for m, k in app.notes if k.get("severity") == "error"]
            assert errs == [r"bad \[/oops] path"]

    _run(drive())


def test_resize_and_paint_errors_are_contained(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            assert view(app).rows
            monkeypatch.setattr(QT.QueueView, "paint", lambda self: (_ for _ in ()).throw(TypeError("x")))
            view(app).on_resize(None)
            await settle(app, pilot)
            assert app.is_running and "queue failed" in _status(app)

    _run(drive())
