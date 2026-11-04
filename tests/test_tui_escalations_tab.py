"""SPEC-0250: the escalations inbox of the TUI mailbox tab (Textual pilot tests)."""
import pytest


from waits import pilot_until
from test_agentic_dashboard import _campaign, _fixture
from test_tui_queue_tab import SIZE, _desk, _run, _sub, press, settle, spy_titles
from nightwright.agentic import escalations as ESC
from nightwright.agentic import events as EV
from nightwright.agentic import mailbox as MB
from nightwright.agentic import store as S
from nightwright.tui import agentic_model as AM
from nightwright.tui import mailbox_tab as MT

PARK = {"reason": "human_decision", "detail": "d", "resume_to": "IMPLEMENTING",
        "escalated_from": "loop_exhausted"}


def mv(app):
    return app.query_one("#mailbox-view", MT.MailboxView)


def world(tmp_path, n=0, campaign=True, **kw):
    """The dashboard fixture, a notifier that does nothing, and `n` open escalations."""
    repo, cfg, ids = _fixture(tmp_path)
    cfg.setdefault("agentic", {}).setdefault("escalations", {})["notify"] = "none"
    cid = _campaign(cfg, repo, "SPEC-0001-hd", "PARKED", park=PARK) if campaign else None
    for _ in range(n):
        esc(cfg, cid, **kw)
        cid = None                                  # one open record per campaign
    return cfg, ids


def esc(cfg, cid="SPEC-0001-hd", **kw):
    kw.setdefault("question", "ship it?")
    kw.setdefault("options", ["yes", "no"])
    kw.setdefault("recommend", 2 if kw["options"] else None)
    return ESC.apply_escalate(cfg, ESC.plan_escalate(cfg, cid, **kw))


async def select_inbox(app, pilot, key):
    v = mv(app)
    keys = [k.key.value for k in v.inboxes_table.ordered_rows]
    v.inboxes_table.move_cursor(row=keys.index(key))
    await settle(app, pilot)
    assert v.inbox == key


def errors(app):
    return [m for m, k in app.notes if k.get("severity") == "error"]


def notes(app):
    return [m for m, k in app.notes]


def drive_with(cfg, body, **kw):
    app = _desk(cfg, tab="mailbox")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await body(app, pilot)

    _run(drive())
    return app


def rec_view(cfg, esc_id):
    r = ESC.read(cfg, esc_id)
    return {k: r.get(k) for k in ("status", "answer", "answered_by", "campaign", "question")} | {
        "withdrawn": {k: v for k, v in (r.get("withdrawn") or {}).items() if k != "at"}}


# --- opening and the table (AC1, AC2) -------------------------------------------------------

def test_opens_on_escalations_only_when_one_is_open(tmp_path):
    cfg, ids = world(_sub(tmp_path, "a"))

    async def none_open(app, pilot):
        v = mv(app)
        assert v.inbox == "human" and not v.escalations
        first = v.inboxes_table.ordered_rows[0]
        assert first.key.value == "escalations" and v.inboxes_table.get_row_at(0)[1] == "0/0"
        assert app.focused is v.threads_table and not v.esc_table.display
    drive_with(cfg, none_open)

    cfg, ids = world(_sub(tmp_path, "b"), n=1)

    async def open_one(app, pilot):
        v = mv(app)
        assert v.inbox == "escalations" and v.esc_table.display and not v.threads_table.display
        assert v.inboxes_table.get_row_at(0)[1] == "1/1"
        assert v.inboxes_table.get_row_at(0)[0].style == "bold"
        assert app.focused is v.esc_table
        assert "ship it?" in v.pane_text.plain and "override (CLI):" in v.pane_text.plain
    drive_with(cfg, open_one)

    ESC.withdraw(cfg, "ESC-0001", "r", "human")             # none open, one on record

    async def only_closed(app, pilot):
        v = mv(app)
        assert v.inbox == "human" and v.inboxes_table.get_row_at(0)[1] == "0/1"
    drive_with(cfg, only_closed)


def test_the_table_lists_open_first_then_newest_and_the_pane_follows_the_cursor(tmp_path):
    cfg, ids = world(tmp_path, n=2, campaign=False)
    ESC.withdraw(cfg, "ESC-0001", "gone", "human")
    esc(cfg, None, question="third")

    async def body(app, pilot):
        v = mv(app)
        assert [(r.id, r.status) for r in v.escalations] == [
            ("ESC-0003", "open"), ("ESC-0002", "open"), ("ESC-0001", "withdrawn")]
        assert v.esc_id == "ESC-0003" and "third" in v.pane_text.plain
        await pilot.press("j")
        await settle(app, pilot)
        assert v.esc_id == "ESC-0002" and "ESC-0002  open" in v.pane_text.plain
        await pilot.press("j")
        await settle(app, pilot)
        assert "withdrawn by human: gone" in v.pane_text.plain
        await select_inbox(app, pilot, "human")
        assert v.threads_table.display and not v.esc_table.display
        await select_inbox(app, pilot, "escalations")
        assert v.esc_id == "ESC-0001"                               # the selection is kept
    drive_with(cfg, body)


def test_the_human_inbox_shows_the_answer_in_escalations_line(tmp_path):
    cfg, ids = world(tmp_path, n=1)

    async def body(app, pilot):
        await select_inbox(app, pilot, "human")
        assert "answer in escalations: ESC-0001" in mv(app).pane_text.plain
    drive_with(cfg, body)


def test_agent_written_markup_in_an_escalation_row_renders_literally(tmp_path):
    cfg, ids = world(tmp_path, n=1, campaign=False, question="[bold]hi[/bold] [red]x")

    async def body(app, pilot):
        v = mv(app)
        assert v.esc_table.get_row_at(0)[3].plain == "[bold]hi[/bold] [red]x"
        assert "[bold]hi[/bold]" in v.pane_text.plain
    drive_with(cfg, body)


# --- answer and withdraw (AC3, AC4) ---------------------------------------------------------

def test_choose_end_to_end_matches_the_cli(tmp_path):
    cfg, ids = world(tmp_path, n=1)
    esc(cfg, None, question="twin", options=["yes", "no"], recommend=2)   # the CLI's twin

    async def body(app, pilot):
        v = mv(app)
        assert v.esc_id == "ESC-0002"
        v.esc_table.move_cursor(row=[r.id for r in v.escalations].index("ESC-0001"))
        await settle(app, pilot)
        with spy_titles() as titles:
            await press(app, pilot, ["c", "enter", "enter"])   # starts on the recommended: 2
            await settle(app, pilot)
        assert titles[0] == ("answer ESC-0001", ["1. yes", "2. no"])
        assert titles[1] == ("answer ESC-0001: 2. no", ["yes", "cancel"])
        assert notes(app) == ["ESC-0001 answered"] and not errors(app)
        assert ESC.read(cfg, "ESC-0001")["status"] == "answered"
        assert v.esc_id == "ESC-0001" and "answered by human" in v.pane_text.plain
        assert "chose 2 (no)" in v.pane_text.plain
    drive_with(cfg, body)
    ESC.apply_answer(cfg, ESC.plan_answer(cfg, "ESC-0002", choose=2, by="human"))
    a, b = rec_view(cfg, "ESC-0001"), rec_view(cfg, "ESC-0002")
    assert a["answer"] == b["answer"] and a["answered_by"] == b["answered_by"] == "human"
    assert a["answer"]["choice"] == 2 and a["answer"]["label"] == "no"


def test_text_answer_and_withdraw_match_the_cli(tmp_path):
    cfg, ids = world(tmp_path, n=2, campaign=False)
    esc(cfg, None, question="a")
    esc(cfg, None, question="b")

    async def body(app, pilot):
        v = mv(app)
        assert [r.id for r in v.escalations] == ["ESC-0004", "ESC-0003", "ESC-0002", "ESC-0001"]
        with spy_titles() as titles:
            await press(app, pilot, ["t", *"go", "ctrl+s", "enter"])
            await settle(app, pilot)
        assert titles == [('answer ESC-0004: "go"', ["yes", "cancel"])]
        v.esc_table.move_cursor(row=[r.id for r in v.escalations].index("ESC-0003"))
        await settle(app, pilot)
        with spy_titles() as titles:
            await press(app, pilot, ["w", *"stale", "enter", "enter"])
            await settle(app, pilot)
        assert titles == [("withdraw ESC-0003: stale", ["yes", "cancel"])]
        assert notes(app) == ["ESC-0004 answered", "ESC-0003 withdrawn"] and not errors(app)
    drive_with(cfg, body)
    ESC.apply_answer(cfg, ESC.plan_answer(cfg, "ESC-0001", text="go", by="human"))
    ESC.apply_withdraw(cfg, ESC.plan_withdraw(cfg, "ESC-0002", reason="stale", by="human"))
    for tui, cli in (("ESC-0004", "ESC-0001"), ("ESC-0003", "ESC-0002")):
        x, y = rec_view(cfg, tui), rec_view(cfg, cli)
        assert {**x, "question": None} == {**y, "question": None}
    assert rec_view(cfg, "ESC-0004")["answer"]["text"] == "go"
    assert rec_view(cfg, "ESC-0003")["withdrawn"] == {"reason": "stale", "by": "human"}


def test_withdraw_of_a_campaigns_escalation_shows_the_still_parked_hint(tmp_path):
    cfg, ids = world(tmp_path, n=1)

    async def body(app, pilot):
        await press(app, pilot, ["w", *"moot", "enter", "enter"])
        await settle(app, pilot)
        assert errors(app) == []
        assert notes(app) == ["ESC-0001 withdrawn; SPEC-0001-hd is still PARKED (human_decision); "
                              "resume it from the queue tab (u)"]
        assert S.read_state(cfg, "SPEC-0001-hd")["state"] == "PARKED"
    drive_with(cfg, body)


@pytest.mark.parametrize("how", ["other park reason", "campaign no longer parked"])
def test_withdraw_hint_is_absent_unless_the_park_is_still_human_decision(tmp_path, how):
    cfg, ids = world(tmp_path, n=1)
    st = S.read_state(cfg, "SPEC-0001-hd")
    if how == "other park reason":
        st["park"] = {**st["park"], "reason": "loop_exhausted"}
    else:
        st.update(state="IMPLEMENTING", park=None)
    S.write_state(cfg, "SPEC-0001-hd", st)

    async def body(app, pilot):
        await press(app, pilot, ["w", *"moot", "enter", "enter"])
        await settle(app, pilot)
        assert errors(app) == [] and notes(app) == ["ESC-0001 withdrawn"]
        assert ESC.read(cfg, "ESC-0001")["status"] == "withdrawn"
    drive_with(cfg, body)


# --- cancel, errors, refusals (AC5) ---------------------------------------------------------

def test_cancel_and_esc_at_each_step_write_nothing(tmp_path):
    cfg, ids = world(tmp_path, n=1)

    async def body(app, pilot):
        before = AM.escalations_signature(cfg)
        for keys in (["c", "escape"],                               # at the choice
                     ["c", "enter", "escape"],                      # at the confirm
                     ["c", "enter", "j", "enter"],                  # cancel
                     ["t", "escape"],                               # at the text
                     ["t", *"no", "ctrl+s", "escape"],
                     ["t", *"no", "ctrl+s", "j", "enter"],
                     ["w", "escape"],
                     ["w", *"no", "enter", "escape"],
                     ["w", *"no", "enter", "j", "enter"]):
            await press(app, pilot, keys)
            await settle(app, pilot)
            assert AM.escalations_signature(cfg) == before and len(app.screen_stack) == 1, keys
        assert ESC.read(cfg, "ESC-0001")["status"] == "open" and not mv(app)._busy
        assert notes(app) == []
    drive_with(cfg, body)


@pytest.mark.parametrize("keys, plan_fn, other", [
    (["c", "enter", "enter"], "plan_answer", "answer"),
    (["t", *"mine", "ctrl+s", "enter"], "plan_answer", "answer"),
    (["w", *"mine", "enter", "enter"], "plan_withdraw", "withdraw"),
])
def test_answered_meanwhile_is_an_error_and_writes_nothing(tmp_path, monkeypatch, keys, plan_fn,
                                                           other):
    cfg, ids = world(tmp_path, n=1)
    real, plan_real = getattr(ESC, plan_fn), ESC.plan_answer

    def planned_then_moved_on(*a, **k):
        plan = real(*a, **k)                    # planned while open; then the CLI gets there first
        if other == "answer":
            ESC.apply_answer(cfg, plan_real(cfg, "ESC-0001", text="cli", by="human"))
        else:
            ESC.withdraw(cfg, "ESC-0001", "cli", "conductor")
        return plan
    monkeypatch.setattr(ESC, plan_fn, planned_then_moved_on)

    async def body(app, pilot):
        await press(app, pilot, keys)
        await settle(app, pilot)
        # SPEC-0269: wait for the outcome (a loaded box can outrun the fixed pauses)
        await pilot_until(pilot, lambda: errors(app) and not mv(app)._busy
                          and len(app.screen_stack) == 1)
        (err,) = errors(app)
        assert "ESC-0001 is" in err and not mv(app)._busy and len(app.screen_stack) == 1
        r = ESC.read(cfg, "ESC-0001")
        if other == "answer":
            assert r["status"] == "answered" and r["answer"]["text"] == "cli"
        else:
            assert r["status"] == "withdrawn" and r["withdrawn"]["reason"] == "cli"
        assert not [n for n in notes(app) if n.endswith(("answered", "withdrawn"))]
    drive_with(cfg, body)


@pytest.mark.parametrize("keys, plan_fn, other", [
    (["c", "enter"], "plan_answer", "answer"),
    (["t", *"mine", "ctrl+s"], "plan_answer", "answer"),
    (["w", *"mine", "enter"], "plan_withdraw", "withdraw"),
])
def test_answered_before_the_plan_is_an_error_and_writes_nothing(tmp_path, monkeypatch, keys,
                                                                 plan_fn, other):
    """The record moves on between the table load and the plan step: the plan itself raises."""
    cfg, ids = world(tmp_path, n=1)
    real, plan_real = getattr(ESC, plan_fn), ESC.plan_answer

    def moved_on_then_planned(*a, **k):
        if other == "answer":
            ESC.apply_answer(cfg, plan_real(cfg, "ESC-0001", text="cli", by="human"))
        else:
            ESC.withdraw(cfg, "ESC-0001", "cli", "conductor")
        return real(*a, **k)
    monkeypatch.setattr(ESC, plan_fn, moved_on_then_planned)

    async def body(app, pilot):
        await press(app, pilot, keys)
        await settle(app, pilot)
        (err,) = errors(app)
        assert "ESC-0001 is" in err and not mv(app)._busy and len(app.screen_stack) == 1
        r = ESC.read(cfg, "ESC-0001")
        assert r["status"] == ("answered" if other == "answer" else "withdrawn")
        assert (r.get("answer") or {}).get("text") in (None, "cli")
        assert notes(app) == errors(app)         # no success notice
    drive_with(cfg, body)


@pytest.mark.parametrize("status", ["withdrawn", "answered"])
def test_keys_on_a_record_that_is_not_open_notify_and_open_nothing(tmp_path, status):
    cfg, ids = world(tmp_path, n=1, campaign=False)
    if status == "withdrawn":
        ESC.withdraw(cfg, "ESC-0001", "r", "human")
    else:
        ESC.apply_answer(cfg, ESC.plan_answer(cfg, "ESC-0001", choose=1, by="human"))

    async def body(app, pilot):
        v = mv(app)
        await select_inbox(app, pilot, "escalations")
        before = AM.escalations_signature(cfg)
        for key in "ctw":
            await press(app, pilot, [key])
            assert len(app.screen_stack) == 1
        assert notes(app) == [f"ESC-0001 is {status}"] * 3
        assert AM.escalations_signature(cfg) == before and not v._busy
    drive_with(cfg, body)


def test_escape_and_tab_in_the_detail_pane_focus_the_visible_escalations_table(tmp_path):
    cfg, ids = world(tmp_path, n=1, campaign=False)

    async def body(app, pilot):
        v = mv(app)
        assert v.in_escalations and not v.threads_table.display
        for key in ("escape", "tab", "shift+tab"):
            v.query_one("#mb-pane", MT.ThreadPane).focus()
            await settle(app, pilot)
            await pilot.press(key)
            await settle(app, pilot)
            assert v.esc_table.has_focus and not v.threads_table.has_focus, key
    drive_with(cfg, body)


def test_choose_on_a_record_without_options_notifies(tmp_path):
    cfg, ids = world(tmp_path, n=1, campaign=False, options=[])

    async def body(app, pilot):
        await press(app, pilot, ["c"])
        assert notes(app) == ["ESC-0001 has no options"] and len(app.screen_stack) == 1
        await press(app, pilot, ["t", *"free", "ctrl+s", "enter"])      # text still works
        await settle(app, pilot)
        assert ESC.read(cfg, "ESC-0001")["answer"]["text"] == "free"
    drive_with(cfg, body)


def test_empty_text_answer_and_empty_withdraw_reason_are_refused(tmp_path):
    cfg, ids = world(tmp_path, n=1)

    async def body(app, pilot):
        before = AM.escalations_signature(cfg)
        await press(app, pilot, ["t", "ctrl+s"])
        await settle(app, pilot)
        await press(app, pilot, ["t", " ", "ctrl+s"])
        await settle(app, pilot)
        await press(app, pilot, ["w", "enter"])
        await settle(app, pilot)
        assert notes(app) == ["empty answer", "empty answer", "empty reason"]
        assert AM.escalations_signature(cfg) == before and not mv(app)._busy
        assert len(app.screen_stack) == 1
    drive_with(cfg, body)


def test_enter_opens_the_campaign_screen(tmp_path):
    cfg, ids = world(tmp_path, n=1)

    async def body(app, pilot):
        await press(app, pilot, ["enter"])
        await settle(app, pilot)
        from nightwright.tui.campaign_screen import CampaignScreen
        assert isinstance(app.screen, CampaignScreen)
        assert app.screen.campaign_id == "SPEC-0001-hd"
    drive_with(cfg, body)


# --- live refresh and read-only (AC6) -------------------------------------------------------

def test_an_escalation_made_or_answered_elsewhere_shows_within_a_poll(tmp_path, monkeypatch):
    monkeypatch.setattr(MT, "POLL_S", 0.05)
    cfg, ids = world(tmp_path, campaign=False)

    async def body(app, pilot):
        v = mv(app)
        assert v.inbox == "human" and v.inboxes_table.get_row_at(0)[1] == "0/0"
        esc(cfg, None, question="from outside")
        await pilot.pause(0.4)
        await settle(app, pilot)
        assert v.inboxes_table.get_row_at(0)[1] == "1/1" and [r.id for r in v.escalations] == [
            "ESC-0001"]
        await select_inbox(app, pilot, "escalations")
        assert "from outside" in v.pane_text.plain
        ESC.apply_answer(cfg, ESC.plan_answer(cfg, "ESC-0001", text="cli", by="human"))
        await pilot.pause(0.4)
        await settle(app, pilot)
        assert v.escalations[0].status == "answered" and "text: cli" in v.pane_text.plain
        assert v.inbox == "escalations" and v.esc_id == "ESC-0001"
        assert v.inboxes_table.get_row_at(0)[1] == "0/1"
    drive_with(cfg, body)


def test_the_tab_never_touches_followers(tmp_path, monkeypatch):
    monkeypatch.setattr(MT, "POLL_S", 0.05)
    cfg, ids = world(tmp_path, n=1)

    def forbidden(name):
        def f(*a, **k):
            raise AssertionError(f"{name} must not be called from the mailbox tab")
        return f
    for mod, names in ((EV, ("sync", "cursor_set", "reader_lock", "follow")), (MB, ("follow",))):
        for n in names:
            monkeypatch.setattr(mod, n, forbidden(f"{mod.__name__}.{n}"))

    async def body(app, pilot):
        await press(app, pilot, ["c", "enter", "enter"])
        await settle(app, pilot)
        esc(cfg, None, question="more")
        await pilot.pause(0.3)
        await settle(app, pilot)
        assert not errors(app) and ESC.read(cfg, "ESC-0001")["status"] == "answered"
    drive_with(cfg, body)
