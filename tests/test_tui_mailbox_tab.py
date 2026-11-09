"""SPEC-0249: the TUI mailbox tab (Textual pilot tests)."""
import asyncio


from test_agentic_dashboard import _fixture
from test_tui_queue_tab import SIZE, _desk, _run, press, settle, spy_titles
from nightwright.agentic import events as EV
from nightwright.agentic import mailbox as MB
from nightwright.tui import agentic_model as AM
from nightwright.tui import mailbox_tab as MT
from waits import pilot_until
import pytest

pytestmark = pytest.mark.tui

STEWARD = "steward:Meta-Tools"


def mv(app):
    return app.query_one("#mailbox-view", MT.MailboxView)


async def open_tab(app, pilot):
    await pilot.press("4")
    await settle(app, pilot)


async def select_inbox(app, pilot, addr):
    v = mv(app)
    keys = [k.key.value for k in v.inboxes_table.ordered_rows]      # by key (SPEC-0250)
    v.inboxes_table.move_cursor(row=keys.index(addr))
    await settle(app, pilot)
    assert v.inbox == addr


def sent(cfg, addr):
    return [m for m in MB.read_inbox(cfg, addr)]


def ask(cfg, frm="human", to="conductor", kind="request", **kw):
    return MB.send(cfg, to=to, frm=frm, text=kw.pop("text", "please look"), kind=kind, **kw)


def flat_titles(titles):
    return [t for t, _ in titles]


# --- tab plumbing and opening ---------------------------------------------------------------

def test_number_key_click_and_tab_option_open_the_mailbox_tab(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg)

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            tabs = app.query_one("#tabs")
            await pilot.press("4")
            await settle(app, pilot)
            assert tabs.active == "tab-mailbox" and app.focused is mv(app).threads_table
            await pilot.press("1")
            await settle(app, pilot)
            assert tabs.active == "tab-ideas"
            await pilot.click("ContentTab#--content-tab-tab-mailbox")
            await settle(app, pilot)
            assert tabs.active == "tab-mailbox" and app.focused is mv(app).threads_table
            rows = {r.address: f"{r.unanswered}/{r.total}" for r in mv(app).inboxes}
            assert list(rows) == MB.known_addresses(cfg)
            assert [k.key.value for k in mv(app).inboxes_table.ordered_rows] == [
                "escalations", *rows]

    _run(drive())
    app2 = _desk(cfg, tab="mailbox")

    async def drive2():
        async with app2.run_test(size=SIZE) as pilot:
            await settle(app2, pilot)
            assert app2.query_one("#tabs").active == "tab-mailbox"
            assert app2.focused is mv(app2).threads_table

    _run(drive2())


def test_opens_on_the_first_inbox_with_unanswered_mail_else_human(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)

    async def opened(app):
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            return mv(app).inbox, [(r.address, r.unanswered, r.total) for r in mv(app).inboxes]

    assert _run(opened(_desk(cfg, tab="mailbox")))[0] == "human"        # nothing yet
    ask(cfg, frm=STEWARD, to="conductor", kind="question")
    ask(cfg, frm="conductor", to="human", kind="request")
    inbox, rows = _run(opened(_desk(cfg, tab="mailbox")))
    assert inbox == "conductor"
    assert rows == [("conductor", 1, 1), ("human", 1, 1), (STEWARD, 0, 0)]


def test_the_pane_shows_the_highlighted_thread_and_follows_the_cursor(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    ask(cfg, frm="conductor", to="human", text="first thread", campaign=ids["ready"])
    ask(cfg, frm=STEWARD, to="human", kind="info", text="second\nthread")
    app = _desk(cfg, tab="mailbox")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v = mv(app)
            assert v.inbox == "human" and [r.unanswered for r in v.threads] == [True, False]
            assert v.root == "human:1" and "first thread" in v.pane_text.plain
            assert "needs reply" in v.pane_text.plain
            await pilot.press("j")
            await settle(app, pilot)
            assert v.root == "human:2" and "    second\n    thread" in v.pane_text.plain
            assert "needs reply" not in v.pane_text.plain

    _run(drive())


# --- reply (AC4) ----------------------------------------------------------------------------

def test_reply_end_to_end(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    req = ask(cfg, frm="conductor", to="human", text="approve?", campaign=ids["ready"])
    app = _desk(cfg, tab="mailbox")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v = mv(app)
            assert v.inbox == "human" and v.threads[0].unanswered
            with spy_titles() as titles:
                await press(app, pilot, ["r", "enter", *"yes", "ctrl+s", "enter"])
                await settle(app, pilot)
            assert flat_titles(titles)[0] == "kind" and titles[0][1][0] == "answer"
            assert flat_titles(titles)[1] == f"send answer to conductor re {req['id']}?"
            (m,) = sent(cfg, "conductor")
            assert (m["from"], m["kind"], m["re"], m["campaign"], m["text"]) == (
                "human", "answer", req["id"], ids["ready"], "yes")
            assert MB.unanswered(cfg, "human") == []
            assert v.inbox == "human" and v.root == req["id"]
            assert not v.threads[0].unanswered and v.threads[0].count == 2
            assert v.threads_table.cursor_row == 0 and app.focused is v.threads_table
            assert "yes" in v.pane_text.plain
            assert not [n for n, k in app.notes if k.get("severity") == "error"]

    _run(drive())


def test_reply_default_kind_is_info_for_an_info_message(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    ask(cfg, frm="conductor", to="human", kind="info", text="fyi")
    app = _desk(cfg, tab="mailbox")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            with spy_titles() as titles:
                await press(app, pilot, ["r", "enter", *"ack", "ctrl+s", "enter"])
                await settle(app, pilot)
            assert flat_titles(titles)[1] == "send info to conductor re human:1?"
            assert sent(cfg, "conductor")[0]["kind"] == "info"

    _run(drive())


def test_follow_up_to_the_humans_own_request_keeps_it_unanswered(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    req = ask(cfg, frm="human", to="conductor", kind="request", text="park it")
    app = _desk(cfg, tab="mailbox")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v = mv(app)
            assert v.inbox == "conductor" and v.threads[0].unanswered
            with spy_titles() as titles:
                await press(app, pilot, ["r", "enter", *"more", "ctrl+s", "enter"])
                await settle(app, pilot)
            assert flat_titles(titles)[1] == "send answer to conductor re -?"
            follow = sent(cfg, "conductor")[1]
            assert (follow["from"], follow["to"], follow["re"]) == ("human", "conductor", None)
            assert [m["id"] for m in MB.unanswered(cfg, "conductor")] == [req["id"]]
            assert v.threads[0].unanswered and v.inbox == "conductor"

    _run(drive())


def test_reply_from_the_conductor_inbox_shows_the_notice_and_answers_the_question(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    q = ask(cfg, frm=STEWARD, to="conductor", kind="question", text="which base?")
    app = _desk(cfg, tab="mailbox")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v = mv(app)
            assert v.inbox == "conductor"
            with spy_titles() as titles:
                await press(app, pilot, ["r", "enter", *"main", "ctrl+s", "enter"])
                await settle(app, pilot)
            assert flat_titles(titles)[1] == (
                f"send answer to {STEWARD} re {q['id']}?; this answers it for conductor")
            (m,) = sent(cfg, STEWARD)
            assert (m["from"], m["re"]) == ("human", q["id"])
            assert MB.unanswered(cfg, "conductor") == []
            assert v.inbox == "conductor" and not v.threads[0].unanswered

    _run(drive())


def test_reply_cancel_and_esc_write_nothing(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    ask(cfg, frm="conductor", to="human")
    app = _desk(cfg, tab="mailbox")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            before = AM.mailbox_signature(cfg)
            for keys in (["r", "escape"],                                   # at the kind
                         ["r", "enter", "escape"],                          # at the body
                         ["r", "enter", *"no", "ctrl+s", "escape"],         # at the confirm
                         ["r", "enter", *"no", "ctrl+s", "j", "enter"]):    # cancel
                await press(app, pilot, keys)
                await settle(app, pilot)
                assert AM.mailbox_signature(cfg) == before and len(app.screen_stack) == 1
            assert not mv(app)._busy

    _run(drive())


def test_empty_reply_text_is_an_error_and_writes_nothing(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    ask(cfg, frm="conductor", to="human")
    app = _desk(cfg, tab="mailbox")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            before = AM.mailbox_signature(cfg)
            await press(app, pilot, ["r", "enter", "ctrl+s"])
            await settle(app, pilot)
            assert AM.mailbox_signature(cfg) == before and not mv(app)._busy
            assert [m for m, k in app.notes if k.get("severity") == "error"] == [
                "empty message text"]

    _run(drive())


# --- new message (AC5) ----------------------------------------------------------------------

def test_new_message_end_to_end_selects_the_target_inbox_and_maps_the_campaign(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    ask(cfg, frm="conductor", to="human", kind="info")
    app = _desk(cfg, tab="mailbox")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v = mv(app)
            assert v.inbox == "human"
            with spy_titles() as titles:
                # to: conductor, steward → the steward; kind: request (default); campaign #2
                await press(app, pilot, ["n", "j", "enter", "enter", "j", "enter",
                                         *"build", "ctrl+s", "enter"])
                await settle(app, pilot)
            assert titles[0][0] == "to" and STEWARD in titles[0][1]
            assert "human" not in titles[0][1]
            assert titles[1][0] == "kind"
            camp_choices = titles[2][1]
            assert camp_choices[0] == "(none)"
            label = camp_choices[1]
            cid = label.split()[0]
            assert label == f"{cid}  {S_spec(cfg, cid)}"
            assert "SPEC-0001-merged" not in [c.split()[0] for c in camp_choices[1:]]
            (m,) = sent(cfg, STEWARD)
            assert (m["from"], m["kind"], m["campaign"], m["re"]) == ("human", "request", cid, None)
            assert v.inbox == STEWARD and v.root == m["id"] and v.threads[0].root_id == m["id"]
            assert v.threads_table.cursor_row == 0
            # (none) → no campaign; from the inboxes table `n` works too
            inbox_tab = v.inboxes_table
            inbox_tab.focus()
            await settle(app, pilot)
            await press(app, pilot, ["n", "enter", "j", "enter", "enter", *"hi", "ctrl+s", "enter"])
            await settle(app, pilot)
            m2 = sent(cfg, "conductor")[-1]
            assert m2["campaign"] is None and m2["kind"] == "info" and v.inbox == "conductor"
            assert v.root == m2["id"]

    _run(drive())


def S_spec(cfg, cid):
    from nightwright.agentic import store
    return store.read_state(cfg, cid).get("spec_id") or ""


def test_new_message_cancel_and_esc_at_each_step_write_nothing(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="mailbox")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            before = AM.mailbox_signature(cfg)
            for keys in (["n", "escape"],
                         ["n", "enter", "escape"],
                         ["n", "enter", "enter", "escape"],
                         ["n", "enter", "enter", "enter", "escape"],
                         ["n", "enter", "enter", "enter", *"x", "ctrl+s", "escape"],
                         ["n", "enter", "enter", "enter", *"x", "ctrl+s", "j", "enter"]):
                await press(app, pilot, keys)
                await settle(app, pilot)
                assert AM.mailbox_signature(cfg) == before and len(app.screen_stack) == 1
            assert not mv(app)._busy and not app.notes

    _run(drive())


def test_new_message_empty_text_is_refused_with_an_error(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="mailbox")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await press(app, pilot, ["n", "enter", "enter", "enter", "ctrl+s"])
            await settle(app, pilot)
            assert AM.mailbox_signature(cfg) == ()
            assert [m for m, k in app.notes if k.get("severity") == "error"] == [
                "empty message text"]

    _run(drive())


def test_new_message_with_an_unknown_address_is_refused(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="mailbox")
    monkeypatch.setattr(MB, "known_addresses", lambda cfg: ["conductor", "human", "steward:Gone"])

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await press(app, pilot, ["n", "j", "enter", "enter", "enter", *"x", "ctrl+s"])
            await settle(app, pilot)
            assert AM.mailbox_signature(cfg) == ()
            errors = [m for m, k in app.notes if k.get("severity") == "error"]
            assert errors and "unknown steward project" in errors[0]

    _run(drive())


# --- live refresh and read-only (AC6) -------------------------------------------------------

def test_an_outside_append_shows_within_a_poll_and_keeps_the_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(MT, "POLL_S", 0.05)
    repo, cfg, ids = _fixture(tmp_path)
    ask(cfg, frm="conductor", to="human", kind="info", text="old")
    ask(cfg, frm=STEWARD, to="human", kind="info", text="older")
    app = _desk(cfg, tab="mailbox")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v = mv(app)
            await pilot.press("j")
            await settle(app, pilot)
            assert v.inbox == "human" and v.root == "human:2"
            ask(cfg, frm="conductor", to="human", kind="question", text="new question")
            for _ in range(40):
                await pilot.pause(0.05)
                if len(v.threads) == 3:
                    break
            await settle(app, pilot)
            assert len(v.threads) == 3 and v.threads[0].subject == "new question"
            assert v.inbox == "human" and v.root == "human:2"       # selection kept
            assert v.threads_table.cursor_row == [r.root_id for r in v.threads].index("human:2")
            assert dict((r.address, r.unanswered) for r in v.inboxes)["human"] == 1
            assert "older" in v.pane_text.plain

    _run(drive())


def test_a_selection_made_during_a_running_load_wins_over_the_stale_snapshot(tmp_path, monkeypatch):
    import threading
    repo, cfg, ids = _fixture(tmp_path)
    ask(cfg, frm="conductor", to="human", kind="info", text="for the human")
    ask(cfg, frm="human", to=STEWARD, kind="info", text="for the steward")
    app = _desk(cfg, tab="mailbox")
    held, release, asked = threading.Event(), threading.Event(), []
    real = AM.load_mailbox

    def slow(cfg_, inbox, *a, **kw):
        asked.append(inbox)
        snap = real(cfg_, inbox, *a, **kw)
        if not held.is_set():
            held.set()
            release.wait(5)
        return snap

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v = mv(app)
            assert v.inbox == "human"
            held.clear(), asked.clear()
            monkeypatch.setattr(AM, "load_mailbox", slow)
            v.reload()                                  # loads "human" and blocks
            for _ in range(40):
                await pilot.pause(0.05)
                if held.is_set():
                    break
            assert held.is_set() and v._loading
            v.inbox, v.root = STEWARD, None             # the user moves on mid-load
            v.reload()
            release.set()
            await settle(app, pilot)
            assert v.inbox == STEWARD and asked[-1] == STEWARD
            assert v.inboxes_table.ordered_rows[v.inboxes_table.cursor_row].key.value == STEWARD
            assert [r.subject for r in v.threads] == ["for the steward"]

    _run(drive())


def test_a_new_thread_in_the_selected_inbox_made_during_a_load_keeps_the_cursor(tmp_path, monkeypatch):
    import threading
    repo, cfg, ids = _fixture(tmp_path)
    ask(cfg, frm="conductor", to="human", kind="info", text="old thread")
    app = _desk(cfg, tab="mailbox")
    held, release = threading.Event(), threading.Event()
    real = AM.load_mailbox

    def slow(cfg_, inbox, *a, **kw):
        snap = real(cfg_, inbox, *a, **kw)             # taken before the new message exists
        if not held.is_set():
            held.set()
            release.wait(5)
        return snap

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v = mv(app)
            assert v.inbox == "human"
            held.clear()
            monkeypatch.setattr(AM, "load_mailbox", slow)
            v.reload()
            for _ in range(40):
                await pilot.pause(0.05)
                if held.is_set():
                    break
            assert held.is_set() and v._loading
            rec = ask(cfg, frm="conductor", to="human", kind="info", text="brand new")
            v.root = rec["id"]                          # as _new_flow does, same inbox
            v.reload()
            release.set()
            await settle(app, pilot)
            assert v.root == rec["id"]
            assert v.current_thread().root_id == rec["id"]

    _run(drive())


def test_agent_written_markup_in_a_subject_or_peer_renders_literally(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    ask(cfg, frm="conductor", to="human", kind="info", text="fix [/x] and [pytest]\nbody")
    ask(cfg, frm=STEWARD, to="human", kind="info", text="[x] done: [/tmp] [link](url)")
    app = _desk(cfg, tab="mailbox")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v = mv(app)
            assert v.inbox == "human"
            await pilot.pause(0.2)
            cells = [str(c.plain if hasattr(c, "plain") else c)
                     for i in range(len(v.threads)) for c in v.threads_table.get_row_at(i)]
            assert "fix [/x] and [pytest]" in cells
            assert "[x] done: [/tmp] [link](url)" in cells

    _run(drive())


def test_the_tab_never_touches_followers(tmp_path, monkeypatch):
    monkeypatch.setattr(MT, "POLL_S", 0.05)
    repo, cfg, ids = _fixture(tmp_path)
    ask(cfg, frm=STEWARD, to="conductor", kind="question")
    ask(cfg, frm="conductor", to="human", kind="request", campaign=ids["ready"])

    def forbidden(name):
        def f(*a, **k):
            raise AssertionError(f"{name} must not be called from the mailbox tab")
        return f
    for mod, names in ((EV, ("sync", "cursor_set", "reader_lock", "follow")), (MB, ("follow",))):
        for n in names:
            monkeypatch.setattr(mod, n, forbidden(f"{mod.__name__}.{n}"))
    signatures = []
    real_signature = AM.mailbox_signature
    monkeypatch.setattr(AM, "mailbox_signature", lambda c: signatures.append(1) or real_signature(c))
    app = _desk(cfg, tab="mailbox")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await select_inbox(app, pilot, "human")
            await press(app, pilot, ["r", "enter", *"ok", "ctrl+s", "enter"])
            await settle(app, pilot)
            await press(app, pilot, ["n", "enter", "enter", "enter", *"hi", "ctrl+s", "enter"])
            await settle(app, pilot)
            polls = len(signatures)
            ask(cfg, frm="conductor", to="human", kind="info")
            assert await pilot_until(pilot, lambda: len(signatures) > polls)    # the tab polled
            await settle(app, pilot)
            assert not [n for n, k in app.notes if k.get("severity") == "error"]
            assert len(sent(cfg, "conductor")) == 3        # the question, the answer, the new one

    _run(drive())


def test_timer_pauses_when_the_tab_is_left(tmp_path, monkeypatch):
    monkeypatch.setattr(MT, "POLL_S", 0.05)
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="mailbox")
    calls = []

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            monkeypatch.setattr(AM, "mailbox_signature", lambda c: calls.append(1) or ())
            assert await pilot_until(pilot, lambda: calls)
            await pilot.press("1")
            await settle(app, pilot)
            calls.clear()
            await pilot.pause(0.3)
            assert not calls

    _run(drive())


# --- enter (AC7) ----------------------------------------------------------------------------

def test_enter_opens_the_campaign_screen_or_focuses_the_pane(tmp_path):
    from nightwright.tui.campaign_detail import CampaignDetail
    repo, cfg, ids = _fixture(tmp_path)
    ask(cfg, frm="conductor", to="human", kind="info", text="no campaign here")
    ask(cfg, frm="conductor", to="human", kind="info", text="with campaign",
        campaign=ids["ready"])
    app = _desk(cfg, tab="mailbox")
    posted = []

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v = mv(app)
            real = v.post_message
            v.post_message = lambda m: posted.append(m) or real(m)
            by_subject = {r.subject: i for i, r in enumerate(v.threads)}
            v.threads_table.move_cursor(row=by_subject["no campaign here"])
            await settle(app, pilot)
            await pilot.press("enter")
            await settle(app, pilot)
            assert app.focused is v.pane and not isinstance(app.screen, CampaignDetail)
            from nightwright.tui.campaign_detail import OpenCampaign
            assert not [m for m in posted if isinstance(m, OpenCampaign)]
            await pilot.press("escape")
            await settle(app, pilot)
            assert app.focused is v.threads_table
            v.threads_table.move_cursor(row=by_subject["with campaign"])
            await settle(app, pilot)
            await pilot.press("enter")
            await settle(app, pilot)
            from nightwright.tui.campaign_detail import OpenCampaign
            sel = [m for m in posted if isinstance(m, OpenCampaign)]
            assert [m.campaign_id for m in sel] == [ids["ready"]]
            assert sel[0].origin is v
            assert isinstance(app.screen, CampaignDetail)

    _run(drive())


def test_tab_moves_between_the_two_tables(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    app = _desk(cfg, tab="mailbox")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            v = mv(app)
            assert app.focused is v.threads_table
            await pilot.press("tab")
            assert app.focused is v.inboxes_table
            await pilot.press("shift+tab")
            assert app.focused is v.threads_table

    _run(drive())


def test_paint_error_notifies(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    ask(cfg, frm="conductor", to="human", kind="info", text="hello")
    app = _desk(cfg, tab="mailbox")

    def boom(self):
        raise TypeError("bad record")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            monkeypatch.setattr(MT.MailboxView, "paint_threads", boom)
            mv(app).reload()
            await settle(app, pilot)
            assert app.is_running
            assert any("mailbox failed" in msg for msg, _ in app.notes)

    _run(drive())


def test_resize_after_paint_error_keeps_the_app_alive(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    ask(cfg, frm="conductor", to="human", kind="info", text="hello")
    app = _desk(cfg, tab="mailbox")

    def boom(self):
        raise TypeError("bad record")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            monkeypatch.setattr(MT.MailboxView, "paint_threads", boom)
            mv(app).reload()
            await settle(app, pilot)
            assert mv(app).threads                  # the bad data stayed in place
            app.notes.clear()
            mv(app).on_resize(None)
            await settle(app, pilot)
            assert app.is_running
            assert any("mailbox failed" in msg for msg, _ in app.notes)

    _run(drive())


def test_age_s_tolerates_out_of_range_naive_timestamps(monkeypatch):
    import datetime as dt
    import time
    monkeypatch.setenv("TZ", "Etc/GMT+12")
    time.tzset()
    try:
        # naive year 1 / year 9999 cannot be converted to local time: no age, no exception
        assert MT.AM.age_s("0001-01-01T00:00:00") is None
        assert MT.AM.age_s("9999-12-31T23:59:59") is None
        assert MT.AM.age_s(None) is None
        assert MT.AM.age_s("2026-01-01T00:00:00") >= 0

        class Overflowing(dt.datetime):
            def astimezone(self, tz=None):
                raise OverflowError("date value out of range")

        class Shim:
            timezone, timedelta = dt.timezone, dt.timedelta
            datetime = Overflowing
        monkeypatch.setattr(MT.AM, "dt", Shim)
        assert MT.AM.age_s("2026-01-01T00:00:00") is None
    finally:
        monkeypatch.undo()
        time.tzset()


def test_send_flow_catch_all_notifies(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    ask(cfg, frm="conductor", to="human", kind="info", text="hello")
    app = _desk(cfg, tab="mailbox")

    def boom(*a, **kw):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(AM, "send_plan", boom)

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await press(app, pilot, ["r", "enter", *"yes", "ctrl+s"])
            await settle(app, pilot)
            assert app.is_running and not mv(app)._busy
            assert any(m.startswith("mailbox action failed") and "RuntimeError" in m
                       for m, k in app.notes if k.get("severity") == "error")

    _run(drive())


def test_a_failed_paint_is_retried_by_the_next_poll(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    ask(cfg, frm="conductor", to="human", kind="info", text="hello")
    app = _desk(cfg, tab="mailbox")
    real = MT.MailboxView.paint_threads
    calls = []

    def boom(self):
        self.threads_table.clear()              # like the real paint: it clears, then fails
        raise TypeError("bad record")

    def counting(self):
        calls.append(1)
        real(self)

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            assert mv(app).threads_table.row_count
            monkeypatch.setattr(MT.MailboxView, "paint_threads", boom)
            mv(app).reload()
            await settle(app, pilot)
            assert any("mailbox failed" in msg for msg, _ in app.notes)
            assert not mv(app).threads_table.row_count
            monkeypatch.setattr(MT.MailboxView, "paint_threads", counting)  # disk unchanged
            mv(app)._poll()
            await settle(app, pilot)
            assert len(calls) >= 1              # without the `_signature = None` reset: 0
            assert mv(app).threads_table.row_count

    _run(drive())


def test_the_same_paint_error_is_notified_once(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    ask(cfg, frm="conductor", to="human", kind="info", text="hello")
    app = _desk(cfg, tab="mailbox")

    def boom(self):
        raise TypeError("bad record")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            monkeypatch.setattr(MT.MailboxView, "paint_threads", boom)
            for _ in range(3):
                mv(app).reload()
                await settle(app, pilot)
            assert sum("mailbox failed" in msg for msg, _ in app.notes) == 1

    _run(drive())
