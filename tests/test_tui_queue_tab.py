"""SPEC-0241: the TUI queue tab (Textual pilot tests)."""
import asyncio
import json

import pytest

from test_agentic_dashboard import _campaign, _fixture, _iso, _park_fixture
from test_agentic_e2e import run_cli
from wt import tui as T
from wt.agentic import events as EV
from wt.agentic import lease as L
from wt.agentic import mailbox as MB
from wt.agentic import queue as Q
from wt.agentic import store as S
from wt.tui import agentic_model as AM
from wt.tui import queue_tab as QT

needs_textual = pytest.mark.skipif(not T.textual_available(),
                                   reason="optional tui extra not installed")
pytestmark = needs_textual
SIZE = (150, 40)


def _sub(tmp_path, name):
    (tmp_path / name).mkdir()
    return tmp_path / name


def _desk(cfg, **kw):
    from wt.tui.app import IdeaDesk
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
    from wt.tui.queue_tab import QueueView
    return app.query_one("#queue-view", QueueView)


def select(app, section, campaign):
    v = view(app)
    i = next(i for i, r in enumerate(v.rows) if (r.section, r.campaign) == (section, campaign))
    v.table.move_cursor(row=i)
    return i


async def open_queue(app, pilot):
    await pilot.press("3")
    await settle(app, pilot)


def state_of(cfg, cid):
    return S.read_state(cfg, cid)


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
            select(app, "backlog", ids["blocked"])
            before = v.current_row()
            assert "p3" in before.meta
            Q.apply_prioritize(cfg, Q.plan_prioritize(cfg, ids["blocked"], 1))
            await pilot.pause(0.5)
            await settle(app, pilot)
            row = v.current_row()
            assert row.campaign == ids["blocked"] and "p1" in row.meta
            assert row.priority == 1

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
            await pilot.pause(0.5)
            assert len(calls) >= 3

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
            for action, section, cid, keys in (
                    ("park", "backlog", ids["ready"], ["p", *"why", "enter", "enter"]),
                    ("prioritize", "backlog", ids["blocked"], ["P", "enter", "enter"]),
                    ("abandon", "backlog", ids["blocked"], ["A", "enter"]),
                    ("resume", "conductor", ids["parked"], ["u", "enter", "enter"])):
                await act(app, pilot, section, cid, keys)
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


async def act(app, pilot, section, cid, keys):
    select(app, section, cid)
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
            await act(app, pilot, "backlog", cid, ["p", "escape"])          # esc at the prompt
            assert snapshot(cfg, cid) == before and len(app.screen_stack) == 1
            await act(app, pilot, "backlog", cid, ["p", *"nope", "enter", "escape"])  # esc at confirm
            assert snapshot(cfg, cid) == before
            await act(app, pilot, "backlog", cid, ["p", *"nope", "enter", "j", "enter"])  # cancel
            assert snapshot(cfg, cid) == before
            await act(app, pilot, "backlog", cid, ["p", "enter"])           # empty reason
            assert snapshot(cfg, cid) == before
            assert any(k.get("severity") == "error" for _, k in app.notes)
            await act(app, pilot, "backlog", cid, ["p", *"hold", "enter", "enter"])
            st = state_of(cfg, cid)
            assert st["state"] == "PARKED" and st["park"]["detail"] == "hold"
            assert (f"park {cid} done", {}) in [(m, {}) for m, _ in app.notes]

    _run(drive())


def test_park_matches_the_cli(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(_sub(tmp_path, "a"))
    repo2, cfg2, ids2 = _fixture(_sub(tmp_path, "b"))
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await act(app, pilot, "backlog", ids["ready"], ["p", *"same", "enter", "enter"])

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
            await act(app, pilot, "active", cid, ["p", *"later", "enter", "enter"])
            st = state_of(cfg, cid)
            assert st["state"] == "IMPLEMENTING" and st["park_requested"]["detail"] == "later"
            assert any(m.startswith(f"park requested for {cid}") for m, _ in app.notes)
            assert not any(m == f"park {cid} done" for m, _ in app.notes)
            rows = [(r.section, r.campaign) for r in view(app).rows]
            assert ("conductor", cid) in rows and ("active", cid) in rows
            assert len(rows) == len(set(rows))
            # cursor restore: stays on the same (section, campaign) across a reload
            select(app, "active", cid)
            idx = view(app).table.cursor_row
            view(app).reload()
            await settle(app, pilot)
            assert view(app).table.cursor_row == idx
            assert (view(app).current_row().section, view(app).current_row().campaign) == (
                "active", cid)
            # and a second park is not offered
            await act(app, pilot, "active", cid, ["p"])
            assert any("park not available" in m for m, _ in app.notes)
            assert len(app.screen_stack) == 1

    _run(drive())


def test_cursor_falls_back_to_the_campaign_when_its_section_goes(tmp_path):
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
            assert row.campaign == cid and row.section == "active"

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
            await act(app, pilot, "backlog", cid, ["P", "escape"])
            await act(app, pilot, "backlog", cid, ["P", "k", "k", "enter", "j", "enter"])  # cancel
            assert snapshot(cfg, cid) == before
            await act(app, pilot, "backlog", cid, ["P", "k", "k", "enter", "enter"])

    _run(drive())
    assert run_cli(cfg2, monkeypatch, ["queue", "prioritize", ids2["ready"], "1",
                                       "--yes"]).exit_code == 0
    assert snapshot(cfg, cid) == snapshot(cfg2, ids2["ready"])
    assert state_of(cfg, cid)["priority"] == 1


def _give_worktree(cfg, cid, path):
    path.mkdir(parents=True)
    st = state_of(cfg, cid)
    st["repo"]["worktree"] = str(path)
    S.write_state(cfg, cid, st)


def test_abandon_without_a_worktree_offers_no_keep_choice(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    titles = []
    app = _desk(cfg, tab="queue")
    from wt.tui.app import ChoiceScreen
    real_init = ChoiceScreen.__init__

    def spy(self, title, choices, current=""):
        titles.append((title, list(choices)))
        real_init(self, title, choices, current)
    ChoiceScreen.__init__ = spy

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await act(app, pilot, "backlog", cid, ["A", "enter"])

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
    from wt.tui.app import ChoiceScreen
    real_init = ChoiceScreen.__init__

    def spy(self, title, choices, current=""):
        titles.append((title, list(choices)))
        real_init(self, title, choices, current)
    ChoiceScreen.__init__ = spy
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await act(app, pilot, "backlog", cid, ["A", *(["j"] if keep else []), "enter"])

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
    if keep:
        assert wt.is_dir()


def test_abandon_cancel_writes_nothing(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    before = snapshot(cfg, cid)
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await act(app, pilot, "backlog", cid, ["A", "j", "enter"])
            await act(app, pilot, "backlog", cid, ["A", "escape"])

    _run(drive())
    assert snapshot(cfg, cid) == before


def test_a_plan_value_error_is_an_error_notification_and_writes_nothing(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    cid = ids["ready"]
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            select(app, "backlog", cid)
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
            await act(app, pilot, "backlog", cid, ["P", "k", "enter", "enter"])
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
            await act(app, pilot, "backlog", ids["ready"], ["u"])
            assert app.notes[0][0] == "resume not available for QUEUED"
            assert snapshot(cfg, ids["merged"]) == before

    _run(drive())


def test_enter_posts_campaign_selected_and_shows_the_cli_hint(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="queue")
    seen = []
    from wt.tui.queue_tab import QueueView
    real = QueueView.open_selected

    def spy(self):
        seen.append(self.current_row().campaign)
        real(self)
    QueueView.open_selected = spy

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            select(app, "backlog", ids["ready"])
            await press(app, pilot, ["enter"])
            await settle(app, pilot)
            assert (f"wt campaign show {ids['ready']}", {"title": "run this in a shell"}) in \
                app.notes

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
            assert [r.meta for r in view(app).rows] == ["queue is empty"]
            await press(app, pilot, ["p", "enter"])     # nothing selectable: no prompt, no crash
            await settle(app, pilot)
            assert len(app.screen_stack) == 1

    _run(drive())
