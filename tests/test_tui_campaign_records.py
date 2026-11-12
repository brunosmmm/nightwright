"""SPEC-0477: campaign detail Findings & decisions pane and Mail pane."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from rich.text import Text
from textual.widgets import DataTable, Input, TabbedContent

from test_tui_campaign_detail import seed
from test_tui_campaign_detail_ac import detail_screen
from test_tui_escalations_tab import esc, errors, notes, world
from test_tui_queue_tab import SIZE, _desk, _run, press, settle
from nightwright.agentic import escalations as ESC
from nightwright.agentic import mailbox as MB
from nightwright.agentic import store as S
from nightwright.tui import agentic_model as AM
from nightwright.tui import campaign_detail as CD
from nightwright.tui import campaign_records as CR
from waits import pilot_until

pytestmark = pytest.mark.tui

CID = "SPEC-0001-hd"
TS = "2026-11-10T10:00:00-05:00"


def rec(rid, *, ts=TS, kind="finding", agent="reviewer", severity=None, status=None, title=None,
        text=None, **links):
    return {"schema": "nw.history.record.v1", "id": rid, "ts": ts, "source": "ledger",
            "kind": kind, "agent": agent, "campaign": CID, "severity": severity,
            "title": title or rid, "text": text, "status": status, "linked_by": "field",
            "links": {"run_id": None, "resolved_by": None, "followup_idea": None,
                      "escalation": None, "thread": None, "campaign": CID, **links}}


def world_cfg(tmp_path):
    cfg, _ = world(tmp_path, n=1)
    seed(cfg, CID)
    return cfg


def patch_history(monkeypatch, mutate):
    """`load_detail` with `mutate(history)` applied: synthetic records on the real campaign."""
    real = AM.load_detail

    def wrapped(cfg, cid):
        d = real(cfg, cid)
        mutate(d.history)
        return d

    monkeypatch.setattr(AM, "load_detail", wrapped)


def drive_popup(cfg, body, *, tab="4"):
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(CID)
            await settle(app, pilot)
            assert isinstance(app.screen, CD.CampaignDetail)
            await press(app, pilot, [tab])
            await settle(app, pilot)
            await body(app, pilot, detail_screen(app))

    _run(drive())
    return app


def table_keys(tbl: DataTable) -> list[str]:
    return [str(tbl.ordered_rows[i].key.value) for i in range(tbl.row_count)]


async def goto(pilot, s, rec_id, *, mail=False):
    tbl = s.mail_table if mail else s.records_table
    tbl.move_cursor(row=table_keys(tbl).index(rec_id))
    await pilot_until(pilot, lambda: (s._selected_thread if mail else s._selected_record_id)
                      == rec_id)


def detail_plain(s) -> str:
    return str(s.query_one("#record-detail").content)


def dir_hash(cfg) -> str:
    h = hashlib.sha256()
    root = S.campaign_dir(cfg, CID)
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


# --- pure model ---------------------------------------------------------------------------


def test_sort_filter_and_thread_model():
    rows = CR.sort_records([rec("b", ts="2026-11-10T10:00:00-05:00"),
                            rec("a", ts="2026-11-10T10:00:00-05:00"),
                            rec("c", ts="2026-11-10T11:00:00-05:00"),
                            rec("a", ts="2026-11-10T10:00:00-05:00")])
    assert [r["id"] for r in rows] == ["c", "a", "b"]
    assert CR.parse_filter("agent:steward:Meta-Tools word bogus:x kind:") == (
        [("agent", "steward:Meta-Tools"), (None, "word")], ["bogus:x", "kind:"])
    items = [rec("x", agent="steward:Meta-Tools", title="Hello World"),
             rec("y", agent="conductor", kind="mail", severity="major", status="fixed")]
    assert [r["id"] for r in CR.filter_records(items, "agent:steward:Meta-Tools")[0]] == ["x"]
    assert [r["id"] for r in CR.filter_records(items, "HELLO")[0]] == ["x"]
    assert [r["id"] for r in CR.filter_records(items, "status:fixed severity:major")[0]] == ["y"]
    assert CR.status_cell(items[1]) == "not re-raised" and CR.status_cell(items[0]) == ""
    assert CR.nearest_index(items, "gone", 5) == 1 and CR.nearest_index([], "x", 0) == -1


def test_mailbox_line_never_raises(tmp_path):
    cfg = world_cfg(tmp_path)
    m = MB.send(cfg, to="conductor", frm="human", text="hi", kind="info", campaign=CID)
    assert CR.mailbox_line(cfg, "mailbox:conductor:0")["id"] == m["id"]
    for bad in ("mailbox:conductor:99999", "mailbox:conductor:5", "mailbox:nope:0",
                "mailbox:../x:0", "mailbox:conductor:x", "garbage", None):
        assert CR.mailbox_line(cfg, bad) is None, bad
    fb = CR.mail_message(cfg, rec("mailbox:conductor:99999", kind="mail", agent="human",
                                  text="from history"))
    assert (fb.frm, fb.to, fb.kind, fb.text) == ("human", "", "", "from history")


# --- AC1: the table --------------------------------------------------------------------------


def test_records_table_order_labels_and_no_placeholder(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    extra = [rec("z:tie-b", ts="2026-11-11T00:00:00-05:00"),
             rec("z:tie-a", ts="2026-11-11T00:00:00-05:00"),
             rec("z:fixed", ts="2026-11-12T00:00:00-05:00", status="fixed")]
    patch_history(monkeypatch, lambda h: h["records"].extend(extra))

    async def body(app, pilot, s):
        keys = table_keys(s.records_table)
        recs = s.view.history["records"]
        by_id = sorted(recs, key=lambda r: r["id"])
        by_id.sort(key=lambda r: CR.H._time_key(r["ts"]), reverse=True)
        assert keys == [r["id"] for r in by_id] and len(keys) == len(recs)
        assert keys[:3] == ["z:fixed", "z:tie-a", "z:tie-b"]
        fixed = s.records_table.get_row("z:fixed")
        assert [c.plain for c in fixed][4] == "not re-raised"
        assert s.query_one("#detail-body", TabbedContent).active == "tab-records"
        for q in ("#campaign-records", "#campaign-mail", "#record-detail"):
            assert "placeholder" not in str(getattr(s.query_one(q), "content", ""))
    drive_popup(cfg, body)


# --- AC2: filter, keys, focus -------------------------------------------------------------------


def test_filter_tokens_keys_and_escape(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    extra = [rec("f:1", agent="steward:Meta-Tools", kind="finding", severity="blocker",
                 status="fixed", title="Alpha", text="Needs Review"),
             rec("f:2", agent="conductor", kind="decision", severity="minor", title="Beta")]
    patch_history(monkeypatch, lambda h: h["records"].extend(extra))

    async def body(app, pilot, s):
        flt = s.query_one("#records-filter", Input)
        status = s.query_one("#records-filter-status")
        total = len(table_keys(s.records_table))

        async def apply(value):
            flt.value = value
            await settle(app, pilot)
            return table_keys(s.records_table)

        assert "f:2" in await apply("agent:conductor") and "f:1" not in table_keys(s.records_table)
        assert all(s.records_table.get_row(k)[1].plain == "conductor"
                   for k in table_keys(s.records_table))
        assert await apply("agent:steward:Meta-Tools") == ["f:1"]
        assert await apply("kind:decision") == ["f:2"]
        assert await apply("severity:blocker") == ["f:1"]
        assert await apply("status:fixed") == ["f:1"]
        assert s.records_table.get_row("f:1")[4].plain == "not re-raised"
        assert await apply("needs review") == ["f:1"]
        assert await apply("BETA") == ["f:2"]
        assert len(await apply("bogus:x")) == total
        assert str(status.content) == "unmatched: bogus:x"
        assert await apply("kind:decision bogus:x") == ["f:2"]
        assert len(await apply("")) == total and str(status.content) == ""

        # keys: j/down/k/up move the focused table, escape in the filter keeps the popup
        s.records_table.focus()
        s.records_table.move_cursor(row=0)
        await settle(app, pilot)
        first = s._selected_record_id
        await press(app, pilot, ["j"])
        second = s._selected_record_id
        assert second != first and s.records_table.cursor_row == 1
        await press(app, pilot, ["down"])
        assert s.records_table.cursor_row == 2
        await press(app, pilot, ["k", "up"])
        assert s._selected_record_id == first
        await press(app, pilot, ["slash"])
        assert app.focused is flt
        await press(app, pilot, ["1"])                       # digits type into the filter
        assert flt.value == "1" and s._active_tab() == "tab-records"
        await press(app, pilot, ["escape"])
        assert isinstance(app.screen, CD.CampaignDetail)
        assert app.focused is s.records_table
    drive_popup(cfg, body)


def test_known_record_is_selected_and_unknown_opens_overview(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    patch_history(monkeypatch, lambda h: h["records"].append(rec("k:known", ts="2020-01-01T00:00:00Z")))
    app = _desk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.open_campaign_detail(CID, focus={"tab": "records", "record": "k:known"})
            await settle(app, pilot)
            s = detail_screen(app)
            assert s._active_tab() == "tab-records"
            assert s._selected_record_id == "k:known"
            assert s.records_table.cursor_row == table_keys(s.records_table).index("k:known")
            await press(app, pilot, ["escape"])
            app.open_campaign_detail(CID, focus={"tab": "records", "record": "nope"})
            await settle(app, pilot)
            assert detail_screen(app)._active_tab() == "tab-overview"
            assert any("not found: nope" in m for m in notes(app))
    _run(drive())


# --- AC3: jumps ----------------------------------------------------------------------------------


def test_jumps_run_decision_followup(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    org = Path(cfg["org_ideas_file"])
    org.write_text("* IDEA Remember the milk\n:PROPERTIES:\n:ID: IDEA-777\n:END:\n")
    extra = [rec("j:run", run_id="001-implementer"), rec("j:badrun", run_id="999-nope"),
             rec("j:dec", resolved_by="j:target"), rec("j:target", kind="human_decision"),
             rec("j:baddec", resolved_by="j:missing"),
             rec("j:idea", followup_idea="IDEA-777"), rec("j:plain")]
    patch_history(monkeypatch, lambda h: h["records"].extend(extra))

    async def body(app, pilot, s):
        await goto(pilot, s, "j:badrun")
        await press(app, pilot, ["r"])
        assert notes(app)[-1] == "no run" and s._active_tab() == "tab-records"
        assert s._selected_record_id == "j:badrun"
        await goto(pilot, s, "j:plain")
        await press(app, pilot, ["r", "d", "i"])
        assert notes(app)[-3:] == ["no run", "no decision", "no follow-up"]
        assert s._selected_record_id == "j:plain" and "follow-up:" not in detail_plain(s)
        await goto(pilot, s, "j:baddec")
        await press(app, pilot, ["d"])
        assert notes(app)[-1] == "no decision" and s._selected_record_id == "j:baddec"
        await goto(pilot, s, "j:dec")
        await press(app, pilot, ["d"])
        assert s._selected_record_id == "j:target"
        await goto(pilot, s, "j:idea")
        assert "follow-up:" not in detail_plain(s)
        await press(app, pilot, ["i"])
        assert await pilot_until(pilot, lambda: "follow-up: IDEA-777" in detail_plain(s))
        assert "Remember the milk" in detail_plain(s) and "followup_idea: IDEA-777" in detail_plain(s)
        await goto(pilot, s, "j:plain")
        assert "follow-up:" not in detail_plain(s)
        await goto(pilot, s, "j:run")
        await press(app, pilot, ["r"])
        await settle(app, pilot)
        assert s._active_tab() == "tab-runs" and s._selected_run_id == "001-implementer"
    drive_popup(cfg, body)


# --- AC4: mail -----------------------------------------------------------------------------------


def test_mail_threads_sort_detail_and_fallback(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    q = MB.send(cfg, to="conductor", frm="human", text="[bold]ask[/bold] me", kind="question",
                campaign=CID)
    MB.send(cfg, to="human", frm="conductor", text="the answer", kind="answer", campaign=CID,
            re=q["id"])
    MB.send(cfg, to="conductor", frm="human", text="other campaign", kind="info",
            campaign="SPEC-0001-ready")
    solo = rec("mailbox:conductor:99999", kind="mail", agent="human", status="open",
               ts="2030-01-01T00:00:00Z", text="solo fallback\nsecond line")
    patch_history(monkeypatch, lambda h: h["records"].append(solo))

    async def body(app, pilot, s):
        recs = [r for r in s.view.history["records"] if r["kind"] == "mail"]
        keys = table_keys(s.mail_table)
        threads = {(r["links"]["thread"] or r["id"]) for r in recs}
        assert set(keys) == threads and len(keys) == len(threads)
        assert keys[0] == "mailbox:conductor:99999"           # null thread, newest ts, own row
        latest = [CR.H._time_key(th.latest["ts"]) for th in s._mail_rows]
        assert latest == sorted(latest, reverse=True)
        assert [c.plain for c in s.mail_table.get_row(keys[0])][1:] == [
            "human", "open", "solo fallback"]
        assert "from human to -" in str(s.query_one("#mail-detail-text").content)
        thread = next(th for th in s._mail_rows if len(th.records) == 2)
        await goto(pilot, s, thread.key, mail=True)
        det = str(s.query_one("#mail-detail-text").content)
        assert "[bold]ask[/bold] me" in det and "the answer" in det
        assert det.index("ask") < det.index("the answer")     # ts ascending
        for r in thread.records:
            line = CR.mailbox_line(cfg, r["id"])
            assert f"from {line['from']} to {line['to']}  {line['kind']}" in det
        assert "other campaign" not in det
        assert not any("other campaign" in str(c) for k in keys
                       for c in s.mail_table.get_row(k))
        assert "placeholder" not in det
        content = s.query_one("#mail-detail-text").content
        assert isinstance(content, Text)
        assert "[bold]ask[/bold] me" in content.plain
        assert content.spans and all(sp.style in ("dim", "bold", "cyan") for sp in content.spans)
    drive_popup(cfg, body, tab="5")


# --- AC5: escalation -----------------------------------------------------------------------------


def test_escalation_choose_text_withdraw_and_refusals(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    esc(cfg, None, options=[], recommend=None)                # ESC-0002: no options, no campaign
    extra = [rec("escalation:ESC-0002", kind="escalation", status="open", escalation="ESC-0002")]
    patch_history(monkeypatch, lambda h: h["records"].extend(extra))
    passed, applied = [], []
    real_answer, real_apply = CD.answer_escalation, ESC.apply_answer

    async def spy(owner, cfg_, row, action):
        passed.append((row, action))
        return await real_answer(owner, cfg_, row, action)

    monkeypatch.setattr(CD, "answer_escalation", spy)
    monkeypatch.setattr(ESC, "apply_answer", lambda c, p: applied.append(p) or real_apply(c, p))

    async def body(app, pilot, s):
        reloads = []
        real_reload = s.reload
        s.reload = lambda **k: reloads.append(1) or real_reload(**k)
        before = ESC.signature(cfg)
        # not an escalation row / no options: nothing planned
        await goto(pilot, s, "ledger:SPEC-0001-hd:7")
        await press(app, pilot, ["c"])
        assert notes(app)[-1] == "not an open escalation" and not passed
        await goto(pilot, s, "escalation:ESC-0002")
        await press(app, pilot, ["c"])
        await settle(app, pilot)
        assert notes(app)[-1] == "ESC-0002 has no options" and not passed
        # cancel, empty text, empty reason write nothing
        await goto(pilot, s, "escalation:ESC-0001")
        await press(app, pilot, ["c", "escape"])
        await settle(app, pilot)
        await press(app, pilot, ["t", "ctrl+s"])
        await settle(app, pilot)
        assert notes(app)[-1] == "empty answer"
        await press(app, pilot, ["w", "enter"])
        await settle(app, pilot)
        assert notes(app)[-1] == "empty reason" and not applied
        assert ESC.signature(cfg) == before and not s._busy
        # t reaches the text prompt
        await press(app, pilot, ["t"])
        assert type(app.screen).__name__ == "BodyPrompt"
        await press(app, pilot, ["escape"])
        # c through confirm
        await press(app, pilot, ["c", "enter", "enter"])
        await settle(app, pilot)
        await pilot_until(pilot, lambda: applied and not s._busy)
        assert [a for _, a in passed] == ["choose", "text", "withdraw", "text", "choose"]
        row, action = passed[-1]
        assert action == "choose" and row.id == "ESC-0001" and type(row).__name__ == "EscRow"
        assert ESC.read(cfg, "ESC-0001")["status"] == "answered" and reloads
        assert notes(app)[-1] == "ESC-0001 answered"
    drive_popup(cfg, body)


def test_escalation_withdraw_keeps_the_parked_suffix_and_errors_are_guarded(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    withdrawn = []
    real_w = ESC.apply_withdraw
    monkeypatch.setattr(ESC, "apply_withdraw", lambda c, p: withdrawn.append(p) or real_w(c, p))

    async def body(app, pilot, s):
        reloads = []
        real_reload = s.reload
        s.reload = lambda **k: reloads.append(1) or real_reload(**k)
        await goto(pilot, s, "escalation:ESC-0001")
        await press(app, pilot, ["w", *"mine", "enter", "enter"])
        await pilot_until(pilot, lambda: withdrawn and not s._busy)
        assert ESC.read(cfg, "ESC-0001")["status"] == "withdrawn" and reloads
        msg = notes(app)[-1]
        assert msg.startswith("ESC-0001 withdrawn; ") and "is still PARKED (human_decision)" in msg
    drive_popup(cfg, body)


def test_escalation_plan_error_is_an_error_notify(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)

    def boom(*a, **k):
        raise ValueError("ESC-0001 is answered")

    monkeypatch.setattr(ESC, "plan_answer", boom)

    async def body(app, pilot, s):
        await goto(pilot, s, "escalation:ESC-0001")
        await press(app, pilot, ["c", "enter"])
        await pilot_until(pilot, lambda: errors(app) and not s._busy)
        assert errors(app) == ["ESC-0001 is answered"]
        assert ESC.read(cfg, "ESC-0001")["status"] == "open"
    drive_popup(cfg, body)


# --- AC6 / AC7 / reload --------------------------------------------------------------------------


def test_markup_in_a_title_and_text_is_literal(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    patch_history(monkeypatch, lambda h: h["records"].append(
        rec("m:1", title="[bold]T[/bold]", text="body [red]x[/red]")))

    async def body(app, pilot, s):
        await goto(pilot, s, "m:1")
        content = s.query_one("#record-detail").content
        assert isinstance(content, Text)
        assert "[bold]T[/bold]" in content.plain and "[red]x[/red]" in content.plain
        assert all(sp.style in ("bold", "dim") for sp in content.spans)
        assert s.records_table.get_row("m:1")[5].plain == "[bold]T[/bold]"
    drive_popup(cfg, body)


def test_read_only_actions_leave_the_campaign_untouched_and_queue_keys_still_work(
        tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    MB.send(cfg, to="conductor", frm="human", text="read only", kind="info", campaign=CID)
    extra = [rec("w:run", run_id="001-implementer"), rec("w:dec", resolved_by="w:target"),
             rec("w:target")]
    patch_history(monkeypatch, lambda h: h["records"].extend(extra))
    acts = []
    monkeypatch.setattr(CD.CampaignDetail, "run_queue_action",
                        lambda self, action, *a, **k: acts.append(action))

    async def body(app, pilot, s):
        before = dir_hash(cfg)
        s.query_one("#records-filter", Input).value = "w:"
        await settle(app, pilot)
        await goto(pilot, s, "w:dec")
        await press(app, pilot, ["d"])
        await goto(pilot, s, "w:run")
        await press(app, pilot, ["r"])
        await settle(app, pilot)
        await press(app, pilot, ["4"])
        s.query_one("#records-filter", Input).value = ""
        await settle(app, pilot)
        await goto(pilot, s, "escalation:ESC-0001")
        await press(app, pilot, ["c", "escape"])
        await settle(app, pilot)
        await press(app, pilot, ["5"])                       # opening Mail writes nothing either
        await settle(app, pilot)
        assert s._active_tab() == "tab-mail" and s.mail_table.row_count
        assert dir_hash(cfg) == before
        await press(app, pilot, ["p", "u", "P", "A"])        # the queue keys on Mail
        assert acts == ["park", "resume", "prioritize", "abandon"]
        await press(app, pilot, ["4"])
        await press(app, pilot, ["p", "u", "P", "A"])        # and on Findings
        assert acts == ["park", "resume", "prioritize", "abandon"] * 2
        assert dir_hash(cfg) == before
    drive_popup(cfg, body)


def test_reload_keeps_tab_filter_and_selections(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    MB.send(cfg, to="conductor", frm="human", text="one", kind="question", campaign=CID)
    MB.send(cfg, to="conductor", frm="human", text="two", kind="info", campaign=CID)
    gone = {"drop": None}

    def mutate(h):
        h["records"].extend(rec(f"r:{i}", ts=f"2026-11-1{i}T00:00:00Z", agent="rr")
                            for i in range(5))
        h["records"][:] = [r for r in h["records"] if r["id"] != gone["drop"]]

    patch_history(monkeypatch, mutate)

    async def body(app, pilot, s):
        flt = s.query_one("#records-filter", Input)
        flt.value = "agent:rr"
        await settle(app, pilot)
        assert table_keys(s.records_table) == [f"r:{i}" for i in (4, 3, 2, 1, 0)]
        await goto(pilot, s, "r:2")
        s.reload()
        await settle(app, pilot)
        assert s._active_tab() == "tab-records" and flt.value == "agent:rr"
        assert s._selected_record_id == "r:2" and s.records_table.cursor_row == 2
        # the selected record disappears: the nearest remaining row (same position) is selected
        gone["drop"] = "r:2"
        s.reload()
        await settle(app, pilot)
        assert table_keys(s.records_table) == ["r:4", "r:3", "r:1", "r:0"]
        assert s._selected_record_id == "r:1" and s.records_table.cursor_row == 2
        await press(app, pilot, ["5"])
        mail_keys = table_keys(s.mail_table)
        assert len(mail_keys) >= 2
        await goto(pilot, s, mail_keys[-1], mail=True)
        s.reload()
        await settle(app, pilot)
        assert s._active_tab() == "tab-mail" and s._selected_thread == mail_keys[-1]
        assert s.mail_table.cursor_row == len(mail_keys) - 1
    drive_popup(cfg, body)


# --- review follow-ups: byte offsets, popup guard, missing idea, Mail keyboard, empty filter ------


def raw_line(cfg, rec_id):
    """The mailbox line a `mailbox:<stem>:<byte offset>` id points at, read without CR."""
    stem, offset = rec_id.split(":")[1], rec_id.rsplit(":", 1)[1]
    with (S.agentic_dir(cfg) / "mailbox" / f"{stem}.jsonl").open("rb") as f:
        f.seek(int(offset))
        return json.loads(f.readline())


def test_mail_second_message_is_read_at_its_byte_offset(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    first = MB.send(cfg, to="conductor", frm="human", text="first line of mail", kind="info",
                    campaign=CID)
    second = MB.send(cfg, to="conductor", frm="human", text="second: in the same file", kind="request",
                     campaign=CID)
    MB.send(cfg, to="steward:Meta-Tools", frm="conductor", text="stem with underscore",
            kind="question", campaign=CID)              # thread id `steward:Meta-Tools:1`
    q = MB.send(cfg, to="conductor", frm="human", text="why a:b:c?", kind="question", campaign=CID)
    MB.send(cfg, to="human", frm="conductor", text="because: colons", kind="answer",
            campaign=CID, re=q["id"])

    async def body(app, pilot, s):
        recs = [r for r in s.view.history["records"] if r["kind"] == "mail"]
        offsets = {r["id"]: int(r["id"].rsplit(":", 1)[1]) for r in recs}
        assert any(o > 0 for o in offsets.values())
        by_text = {raw_line(cfg, r["id"])["text"]: r for r in recs}
        assert by_text["second: in the same file"]["id"].rsplit(":", 1)[1] != "0"
        assert "steward_Meta-Tools" in by_text["stem with underscore"]["id"]
        assert by_text["first line of mail"]["id"] != by_text["second: in the same file"]["id"]
        for r in recs:                                       # every record, against the file
            exp = raw_line(cfg, r["id"])
            m = CR.mail_message(cfg, r)
            assert (m.frm, m.to, m.kind, m.text) == (exp["from"], exp["to"], exp["kind"],
                                                     exp["text"])
        th = next(t for t in s._mail_rows
                  if t.latest["id"] == by_text["second: in the same file"]["id"])
        exp = raw_line(cfg, th.latest["id"])
        assert (exp["from"], exp["to"], exp["kind"]) == ("human", "conductor", "request")
        assert [c.plain for c in s.mail_table.get_row(th.key)][1] == exp["from"]
        await goto(pilot, s, th.key, mail=True)
        det = str(s.query_one("#mail-detail-text").content)
        assert "from human to conductor  request" in det and exp["text"] in det
        assert "first line of mail" not in det             # the other message in the file
        thread2 = next(t for t in s._mail_rows if len(t.records) == 2)
        assert int(thread2.records[0]["id"].rsplit(":", 1)[1]) > 0     # the question: offset > 0
        await goto(pilot, s, thread2.key, mail=True)
        det = str(s.query_one("#mail-detail-text").content)
        for r in thread2.records:
            e = raw_line(cfg, r["id"])
            assert f"from {e['from']} to {e['to']}  {e['kind']}" in det and e["text"] in det
    drive_popup(cfg, body, tab="5")


def test_popup_guard_branches_and_refusals(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    extra = [rec("escalation:ESC-0009", kind="escalation", status="answered", escalation="ESC-0009"),
             rec("escalation:none", kind="escalation", status="open")]       # null links.escalation
    patch_history(monkeypatch, lambda h: h["records"].extend(extra))
    planned = []
    state = {"exc": None}

    def plan(*a, **k):
        planned.append(1)
        raise state["exc"]

    monkeypatch.setattr(ESC, "plan_answer", plan)

    async def body(app, pilot, s):
        before = ESC.signature(cfg)
        for rid in ("escalation:ESC-0009", "escalation:none"):
            await goto(pilot, s, rid)
            await press(app, pilot, ["c", "t", "w"])
            assert notes(app)[-3:] == ["not an open escalation"] * 3 and not s._busy
        assert not planned
        await goto(pilot, s, "escalation:ESC-0001")
        state["exc"] = RuntimeError("kaboom")
        await press(app, pilot, ["c", "enter"])
        await pilot_until(pilot, lambda: errors(app) and not s._busy)
        assert errors(app)[-1].startswith("mailbox action failed: ") and "kaboom" in errors(app)[-1]
        state["exc"] = OSError("disk gone")
        await press(app, pilot, ["c", "enter"])
        await pilot_until(pilot, lambda: len(errors(app)) == 2 and not s._busy)
        assert errors(app)[-1] == "disk gone" and not s._busy
        assert len(planned) == 2 and ESC.signature(cfg) == before
        # the missing file is the natural generic failure: ESC.read itself raises
        monkeypatch.undo()
        real_read = ESC.read

        def boom(c, eid):
            raise RuntimeError("no such esc")

        monkeypatch.setattr(ESC, "read", boom)
        await press(app, pilot, ["w"])
        await pilot_until(pilot, lambda: len(errors(app)) == 3 and not s._busy)
        assert errors(app)[-1].startswith("mailbox action failed: ")
        monkeypatch.setattr(ESC, "read", real_read)
    drive_popup(cfg, body)


def test_escalation_text_answer_runs_through_confirm(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    applied = []
    real_apply = ESC.apply_answer
    monkeypatch.setattr(ESC, "apply_answer", lambda c, p: applied.append(p) or real_apply(c, p))

    async def body(app, pilot, s):
        await goto(pilot, s, "escalation:ESC-0001")
        await press(app, pilot, ["t", *"fine", "ctrl+s", "enter"])
        await pilot_until(pilot, lambda: applied and not s._busy)
        assert applied[0]["answer"]["text"] == "fine"
        assert ESC.read(cfg, "ESC-0001")["status"] == "answered"
        assert notes(app)[-1] == "ESC-0001 answered"
    drive_popup(cfg, body)


def test_followup_without_a_readable_idea_still_shows_the_id(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    Path(cfg["org_ideas_file"]).write_text(
        "* IDEA Remember the milk\n:PROPERTIES:\n:ID: IDEA-777\n:END:\n")
    extra = [rec("n:unknown", followup_idea="IDEA-404"), rec("n:broken", followup_idea="IDEA-777")]
    patch_history(monkeypatch, lambda h: h["records"].extend(extra))
    assert CR.idea_title(cfg, "IDEA-777") .endswith("Remember the milk")
    assert CR.idea_title(cfg, "IDEA-404") is None

    async def body(app, pilot, s):
        await goto(pilot, s, "n:unknown")
        assert "follow-up:" not in detail_plain(s)
        await press(app, pilot, ["i"])
        assert await pilot_until(pilot, lambda: "follow-up: IDEA-404" in detail_plain(s))
        assert detail_plain(s).rstrip().endswith("follow-up: IDEA-404")
        import nightwright.org as org

        def unreadable(c):
            raise OSError("unreadable")

        monkeypatch.setattr(org, "load_tasks", unreadable)
        assert CR.idea_title(cfg, "IDEA-777") is None
        await goto(pilot, s, "n:broken")
        await press(app, pilot, ["i"])
        assert await pilot_until(pilot, lambda: "follow-up: IDEA-777" in detail_plain(s))
        assert detail_plain(s).rstrip().endswith("follow-up: IDEA-777")
        assert "milk" not in detail_plain(s)
    drive_popup(cfg, body)


def test_mail_keyboard_reload_and_empty_filter(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    for n in range(3):
        MB.send(cfg, to="conductor", frm="human", text=f"thread {n}", kind="info", campaign=CID)
    gone = {"key": None}

    def mutate(h):
        h["records"][:] = [r for r in h["records"]
                           if gone["key"] not in (r["id"], r["links"].get("thread"))]

    patch_history(monkeypatch, mutate)

    async def body(app, pilot, s):
        keys = table_keys(s.mail_table)
        assert len(keys) >= 3 and app.focused is s.mail_table
        s.mail_table.move_cursor(row=0)
        await pilot_until(pilot, lambda: s._selected_thread == keys[0])
        for key, row in (("j", 1), ("down", 2), ("k", 1), ("up", 0)):
            await press(app, pilot, [key])
            assert s.mail_table.cursor_row == row and s._selected_thread == keys[row], key
        # the selected thread vanishes on reload: the row at the same position is selected
        await press(app, pilot, ["j"])
        gone["key"] = keys[1]
        s.reload()
        await settle(app, pilot)
        now = table_keys(s.mail_table)
        assert keys[1] not in now and s._selected_thread == now[1] == keys[2]
        assert s.mail_table.cursor_row == 1 and s._active_tab() == "tab-mail"
        gone["key"] = None
        # Findings: enter in the filter returns to the table; a filter matching nothing
        await press(app, pilot, ["4"])
        flt = s.query_one("#records-filter", Input)
        await press(app, pilot, ["slash"])
        assert app.focused is flt
        await press(app, pilot, ["enter"])
        assert app.focused is s.records_table and isinstance(app.screen, CD.CampaignDetail)
        flt.value = "no-such-record-anywhere"
        await settle(app, pilot)
        assert s.records_table.row_count == 0 and s._current_record() is None
        assert str(s.query_one("#record-detail").content) == "no records"
        before = dir_hash(cfg)
        await press(app, pilot, ["r", "d", "i", "c"])
        await settle(app, pilot)
        assert notes(app)[-4:] == ["no run", "no decision", "no follow-up",
                                   "not an open escalation"]
        assert not errors(app) and dir_hash(cfg) == before
    drive_popup(cfg, body, tab="5")


# --- review loop 2: ordering, `r` with two runs, typed letters, keyboard scroll ----------------


def mail_rec(rid, ts, thread, text, agent="human"):
    return rec(rid, ts=ts, kind="mail", agent=agent, text=text, thread=thread, status="answered")


def ordered_mail_input():
    """Hand-built mail records at distinct `ts`, deliberately not in the expected order."""
    return [
        mail_rec("mailbox:x:50", "2026-11-10T10:00:00-05:00", "T:b", "b-first-by-ts"),
        mail_rec("mailbox:x:70", "2026-11-10T12:00:00-05:00", "thread-z", "tie z"),
        mail_rec("mailbox:x:20", "2026-11-10T09:00:00-05:00", "T:old", "old thread"),
        mail_rec("mailbox:x:10", "2026-11-10T11:00:00-05:00", "T:b", "b-last-by-ts"),
        mail_rec("mailbox:x:60", "2026-11-10T12:00:00-05:00", "thread-a", "tie a"),
        mail_rec("mailbox:x:99", "2026-11-10T13:00:00-05:00", None, "no thread"),
    ]


def test_mail_threads_order_model():
    ths = CR.mail_threads(ordered_mail_input() + [rec("not-mail", ts="2030-01-01T00:00:00Z")])
    # latest ts descending; equal latest ts by thread id ascending; a null thread keyed by id
    assert [t.key for t in ths] == ["mailbox:x:99", "thread-a", "thread-z", "T:b", "T:old"]
    assert [t.latest["id"] for t in ths][0] == "mailbox:x:99"
    # ts ascending inside a thread, although ascending record ids say the opposite
    b = next(t for t in ths if t.key == "T:b")
    assert [r["id"] for r in b.records] == ["mailbox:x:50", "mailbox:x:10"]
    assert b.latest["id"] == "mailbox:x:10"
    assert CR.mail_threads([]) == [] and CR.mail_threads(None) == []


def test_mail_pane_orders_by_ts_not_by_id(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    patch_history(monkeypatch, lambda h: h["records"].__setitem__(
        slice(None), [r for r in h["records"] if r["kind"] != "mail"] + ordered_mail_input()))

    async def body(app, pilot, s):
        assert table_keys(s.mail_table) == ["mailbox:x:99", "thread-a", "thread-z", "T:b", "T:old"]
        await goto(pilot, s, "T:b", mail=True)
        det = str(s.query_one("#mail-detail-text").content)
        assert det.index("b-first-by-ts") < det.index("b-last-by-ts")
        assert "tie a" not in det
    drive_popup(cfg, body, tab="5")


def test_r_selects_the_non_default_run_on_the_runs_table(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    extra = [rec("r:one", run_id="001-implementer"), rec("r:two", run_id="002-reviewer")]

    def mutate(h):
        h["records"].extend(extra)
        h["runs"].append({**h["runs"][0], "run_id": "002-reviewer", "persona": "reviewer"})

    patch_history(monkeypatch, mutate)

    async def body(app, pilot, s):
        assert [r["run_id"] for r in s._runs_rows] == ["001-implementer", "002-reviewer"]
        await goto(pilot, s, "r:two")
        await press(app, pilot, ["r"])
        await settle(app, pilot)
        assert s._active_tab() == "tab-runs"
        assert s.runs_table.cursor_row == 1
        assert str(s.runs_table.ordered_rows[1].key.value) == "002-reviewer"
        await press(app, pilot, ["4"])
        await goto(pilot, s, "r:one")
        await press(app, pilot, ["r"])
        await settle(app, pilot)
        assert s._active_tab() == "tab-runs" and s.runs_table.cursor_row == 0
    drive_popup(cfg, body)


def test_letters_type_into_the_filter_without_firing_bindings(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    MB.send(cfg, to="conductor", frm="human", text="read only", kind="info", campaign=CID)
    patch_history(monkeypatch, lambda h: h["records"].append(rec("l:1", title="plain")))
    acts = []
    monkeypatch.setattr(CD.CampaignDetail, "run_queue_action",
                        lambda self, action, *a, **k: acts.append(action))

    async def body(app, pilot, s):
        before = dir_hash(cfg)
        flt = s.query_one("#records-filter", Input)
        await press(app, pilot, ["slash"])
        assert app.focused is flt
        letters = ["p", "u", "q", "j", "k", "r", "d", "i", "c", "t", "w", "g", "G", "P", "A"]
        await press(app, pilot, letters)
        assert flt.value == "puqjkrdictwgGPA"
        assert isinstance(app.screen, CD.CampaignDetail) and s._active_tab() == "tab-records"
        assert acts == [] and not errors(app) and notes(app) == []
        assert dir_hash(cfg) == before
        # `/` is ignored on Mail; `g`/`G` stay timeline-only on Findings and Mail
        await press(app, pilot, ["escape", "5", "slash"])
        assert app.focused is s.mail_table and s._active_tab() == "tab-mail"
        await press(app, pilot, ["g", "G"])
        assert app.focused is s.mail_table and s._active_tab() == "tab-mail"
        assert acts == [] and dir_hash(cfg) == before
    drive_popup(cfg, body)


def test_findings_and_mail_details_scroll_from_the_keyboard(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    long_text = "\n".join(f"body-line-{i}" for i in range(120))
    mails = [mail_rec(f"mailbox:x:{n}", f"2026-11-10T10:0{n}:00-05:00", "T:long", long_text)
             for n in range(3)]
    patch_history(monkeypatch, lambda h: h["records"].__setitem__(
        slice(None), [r for r in h["records"] if r["kind"] != "mail"] + mails
        + [rec("s:long", text=long_text, ts="2030-01-01T00:00:00Z")]))

    async def body(app, pilot, s):
        await goto(pilot, s, "s:long")
        rec_scroll = s.query_one("#records-detail")
        assert rec_scroll.max_scroll_y > 0 and rec_scroll.scroll_y == 0
        await press(app, pilot, ["pagedown"])
        await settle(app, pilot)
        assert rec_scroll.scroll_y > 0
        mid = rec_scroll.scroll_y
        await press(app, pilot, ["pageup"])
        await settle(app, pilot)
        assert rec_scroll.scroll_y < mid
        cur = s._selected_record_id
        await press(app, pilot, ["5"])
        mail_scroll = s.query_one("#mail-detail")
        assert mail_scroll.max_scroll_y > 0 and mail_scroll.scroll_y == 0
        await press(app, pilot, ["pagedown"])
        await settle(app, pilot)
        assert mail_scroll.scroll_y > 0 and s._active_tab() == "tab-mail"
        await press(app, pilot, ["4"])
        assert s._selected_record_id == cur
    drive_popup(cfg, body)


# --- review loop 3: control characters, file order, exact keyed match, selection, guards ----------


def test_mail_threads_same_second_records_keep_file_order():
    ts = "2026-11-10T10:00:00-05:00"
    ths = CR.mail_threads([mail_rec("mailbox:x:100", ts, "T", "answer"),
                           mail_rec("mailbox:x:99", ts, "T", "question")])
    assert [r["id"] for r in ths[0].records] == ["mailbox:x:99", "mailbox:x:100"]
    assert ths[0].latest["id"] == "mailbox:x:100"
    # across inboxes the stem decides, then the numeric offset (not the string offset)
    ths = CR.mail_threads([mail_rec("mailbox:human:1030", ts, "T", "c"),
                           mail_rec("mailbox:human:262", ts, "T", "b"),
                           mail_rec("mailbox:conductor:5", ts, "T", "a")])
    assert [r["id"] for r in ths[0].records] == [
        "mailbox:conductor:5", "mailbox:human:262", "mailbox:human:1030"]


def test_keyed_filter_tokens_match_exactly():
    items = [rec("x", agent="steward:Meta-Tools", kind="finding", severity="major",
                 status="fixed"),
             rec("y", agent="conductor", kind="mail", severity="minor", status="open")]
    for tok in ("agent:steward", "agent:Conductor", "agent:conduct", "kind:find", "kind:Finding",
                "severity:maj", "severity:MAJOR", "status:fix", "status:Fixed", "status:not"):
        assert CR.filter_records(items, tok)[0] == [], tok
    assert [r["id"] for r in CR.filter_records(items, "agent:conductor status:open")[0]] == ["y"]


def test_control_characters_are_stripped_from_the_detail_panes(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    MB.send(cfg, to="conductor", frm="human", text="body \x1b[2Jpasted \x07ok",
            kind="question", campaign=CID)
    patch_history(monkeypatch, lambda h: h["records"].append(
        rec("c:1", title="t\x1b[31mred", text="x\x1b[2Jy", status="o\x1bk",
            followup_idea="IDEA-\x1b9")))

    async def body(app, pilot, s):
        await goto(pilot, s, "c:1")
        await press(app, pilot, ["i"])
        await pilot_until(pilot, lambda: "follow-up:" in detail_plain(s))
        content = s.query_one("#record-detail").content
        assert "\x1b" not in content.plain and "t[31mred" in content.plain
        assert "follow-up: IDEA-9" in content.plain
        await press(app, pilot, ["5"])
        await settle(app, pilot)
        mail = s.query_one("#mail-detail-text").content
        assert "\x1b" not in mail.plain and "[2Jpasted" in mail.plain and "\x07" not in mail.plain
    drive_popup(cfg, body)


def test_selection_survives_a_filter_that_matches_nothing(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    patch_history(monkeypatch, lambda h: h["records"].extend(
        [rec(f"s:{i}", ts=f"2026-11-1{i}T00:00:00Z", agent="conductor") for i in range(4)]
         + [rec("s:other", ts="2026-11-15T00:00:00Z", agent="steward")]))

    async def body(app, pilot, s):
        await goto(pilot, s, "s:1")
        flt = s.query_one("#records-filter", Input)
        for value in ("agent:c", "agent:co", "agent:conductor"):
            flt.value = value
            await settle(app, pilot)
        # the intermediate states matched nothing; the selection is still s:1
        assert s._selected_record_id == "s:1"
        assert table_keys(s.records_table)[s.records_table.cursor_row] == "s:1"
        flt.value = "agent:nobody"
        await settle(app, pilot)
        assert s.records_table.row_count == 0 and s._selected_record_id == "s:1"
        flt.value = ""
        await settle(app, pilot)
        assert table_keys(s.records_table)[s.records_table.cursor_row] == "s:1"
    drive_popup(cfg, body)


def test_followup_title_is_on_the_follow_up_line(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    Path(cfg["org_ideas_file"]).write_text(
        "* IDEA Remember the milk\n:PROPERTIES:\n:ID: IDEA-777\n:END:\n")
    patch_history(monkeypatch, lambda h: h["records"].append(rec("f:1", followup_idea="IDEA-777")))

    async def body(app, pilot, s):
        await goto(pilot, s, "f:1")
        await press(app, pilot, ["i"])
        assert await pilot_until(pilot, lambda: "follow-up: IDEA-777" in detail_plain(s))
        line = next(ln for ln in detail_plain(s).splitlines() if ln.startswith("follow-up:"))
        assert "Remember the milk" in line
    drive_popup(cfg, body)


def test_busy_popup_ignores_an_escalation_key_and_a_settled_file_warns(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    planned = []
    monkeypatch.setattr(ESC, "plan_answer", lambda *a, **k: planned.append(1))
    real_read = ESC.read

    def settled(c, eid):
        return {**real_read(c, eid), "status": "answered"}

    async def body(app, pilot, s):
        await goto(pilot, s, "escalation:ESC-0001")
        s._busy = True
        s.records_escalation("choose")
        await settle(app, pilot)
        assert not planned and s._busy
        s._busy = False
        # history says open, the escalation file is already answered: warn, plan nothing
        monkeypatch.setattr(ESC, "read", settled)
        s.records_escalation("choose")
        await pilot_until(pilot, lambda: notes(app) and notes(app)[-1] == "ESC-0001 is answered"
                          and not s._busy)
        assert not planned
    drive_popup(cfg, body)


# --- SPEC-0563: SPEC-0477 follow-up fixes ---------------------------------------------------


def test_malformed_mailbox_ids_fail_soft(tmp_path):
    cfg = world_cfg(tmp_path)
    MB.send(cfg, to="conductor", frm="human", text="hi", kind="info", campaign=CID)
    for bad in ("mailbox:conductor:99999999999999999999999999", "mailbox:conductor:²",
                "mailbox:conductor:-1", "mailbox:nope:0"):
        assert CR.mailbox_line(cfg, bad) is None, bad
    for bad in ("mailbox:x:²", "mailbox:x:99999999999999999999", "mailbox:x:" + "9" * 5000):
        assert isinstance(CR._id_order(bad), tuple), bad
    assert CR._id_order("mailbox:x:99") < CR._id_order("mailbox:x:100")


def test_popup_with_a_corrupt_mail_id_still_renders(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    bad = rec("mailbox:conductor:99999999999999999999999999", kind="mail", agent="human",
              status="open", text="from history")
    patch_history(monkeypatch, lambda h: h["records"].append(bad))

    async def body(app, pilot, s):
        assert s._load_error is None and s.view is not None
        assert [c.plain for c in s.mail_table.get_row(bad["id"])][1:] == [
            "human", "open", "from history"]
        await goto(pilot, s, bad["id"], mail=True)
        assert "from human to -" in str(s.query_one("#mail-detail-text").content)
    drive_popup(cfg, body, tab="5")


def test_time_cells_are_control_character_cleaned(tmp_path, monkeypatch):
    cfg = world_cfg(tmp_path)
    f = rec("f:1", ts="\x1b[31mnow", title="dirty time")
    m = rec("m:1", ts="\x1b[31mnow", kind="mail", agent="human", text="dirty mail time")
    patch_history(monkeypatch, lambda h: h["records"].extend([f, m]))

    async def body(app, pilot, s):
        assert "\x1b" not in s.records_table.get_row("f:1")[0].plain
        assert "\x1b" not in s.mail_table.get_row("m:1")[0].plain
    drive_popup(cfg, body)
    assert CD._timeline_time(TS) == CD._timeline_time(TS) and "\x1b" not in CD._timeline_time("a\x1bb")


def test_a_null_field_never_matches_a_keyed_token():
    items = [rec("n"), rec("s", severity="None", status="None"),
             rec("h", severity="high", status="fixed")]
    for tok in ("severity:None", "status:None"):
        kept, unmatched = CR.filter_records(items, tok)
        assert [r["id"] for r in kept] == ["s"] and unmatched == [], tok
    assert [r["id"] for r in CR.filter_records(items, "severity:high status:fixed")[0]] == ["h"]


class _Owner:
    def __init__(self):
        self.notes: list[tuple[str, str]] = []
        self._busy = True
        self.app = self

    def notify(self, msg, severity="information"):
        self.notes.append((msg, severity))


@pytest.mark.parametrize("exc", [ValueError("bad"), OSError("gone"), RuntimeError("kaboom")])
def test_mailbox_tab_and_popup_share_one_guard(exc):
    import asyncio

    from nightwright.tui import escalation_flow as EF
    from nightwright.tui import mailbox_tab as MT

    async def flow():
        raise exc

    results = []
    for guard in (EF.guarded, MT.MailboxView._guarded):
        owner = _Owner()
        asyncio.run(guard(owner, flow()))
        assert owner._busy is False
        results.append(owner.notes)
    assert results[0] == results[1] and len(results[0]) == 1 and results[0][0][1] == "error"
    src = Path(MT.__file__).read_text()
    assert "mailbox action failed" not in src
    assert "mailbox action failed" in Path(EF.__file__).read_text()


def test_campaign_detail_docstring_has_no_placeholder():
    assert "placeholder" not in CD.CampaignDetail.__doc__
