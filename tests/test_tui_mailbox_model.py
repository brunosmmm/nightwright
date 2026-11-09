"""SPEC-0249: the mailbox tab's view model (no Textual)."""
import json

import pytest

from nightwright.agentic import mailbox as MB
from nightwright.tui import agentic_model as AM

pytestmark = pytest.mark.tui


def _cfg(tmp_path):
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "mappings.yaml").write_text("{}\n")
    return {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "cfg"),
            "outbox_targets": {"Demo": {"repo_path": str(tmp_path)},
                               "Other": {"repo_path": str(tmp_path)}},
            "project_axis": "bucket", "specs_dir": str(tmp_path / "specs")}


def put(cfg, to, seq, frm, ts, text="x", kind="info", re=None, campaign=None, needs=None,
        id=None):
    """Append a hand-written record (controlled ts / id), as another process would."""
    d = MB.mailbox_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    rec = {"schema": MB.SCHEMA, "id": id or f"{to}:{seq}", "seq": seq, "ts": ts, "from": frm,
           "to": to, "kind": kind, "campaign": campaign, "re": re, "text": text,
           "needs_reply": kind in ("question", "request") if needs is None else needs}
    with open(d / f"{MB.E._safe(to)}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def T(n):
    return f"2026-10-25T10:00:{n:02d}+00:00"


def world(tmp_path):
    """q1: steward→conductor question, answered by the human (reply lands in the steward inbox).
    q2: steward→conductor question, open. r1: conductor→human request, open."""
    cfg = _cfg(tmp_path)
    put(cfg, "conductor", 1, "steward:Demo", T(1), "why is the gate red?\nmore", "question",
        campaign="C1")
    put(cfg, "steward:Demo", 1, "human", T(2), "flaky", "answer", re="conductor:1", campaign="C1")
    put(cfg, "conductor", 2, "steward:Other", T(5), "which base?", "question")
    put(cfg, "human", 1, "conductor", T(3), "approve park?", "request", campaign="C2")
    return cfg


def test_load_inboxes_counts_in_known_address_order(tmp_path):
    cfg = world(tmp_path)
    rows = AM.load_inboxes(cfg)
    known = MB.known_addresses(cfg)
    assert [r.address for r in rows] == known
    assert known[:2] == ["conductor", "human"] and {"steward:Demo", "steward:Other"} <= set(known)
    by = {r.address: (r.unanswered, r.total, r.last_ts) for r in rows}
    assert by["conductor"] == (1, 2, T(5)) and by["human"] == (1, 1, T(3))
    assert by["steward:Demo"] == (0, 1, T(2)) and by["steward:Other"] == (0, 0, None)
    assert AM.initial_inbox(rows) == "conductor"
    assert AM.initial_inbox([r for r in rows if r.address != "conductor"]) == "human"
    assert AM.initial_inbox([AM.InboxRow("human", 0, 0, None)]) == "human"


def test_snapshot_results_equal_the_mailbox_functions(tmp_path):
    cfg = world(tmp_path)
    msgs = AM.snapshot(cfg)
    for r in AM.load_inboxes(cfg, msgs):
        assert r.unanswered == len(MB.unanswered(cfg, r.address))
        assert r.total == len(MB.read_inbox(cfg, r.address))
    for root in ("conductor:1", "conductor:2", "human:1"):
        assert [m["id"] for m in AM.thread_of(msgs, root)] == [
            m["id"] for m in MB.thread(cfg, root)]
    for t in AM.load_threads(cfg, "conductor", msgs):
        assert t.count == len(MB.thread(cfg, t.root_id))
        assert t.unanswered == any(m["id"] in {u["id"] for u in MB.unanswered(cfg, "conductor")}
                                   for m in MB.thread(cfg, t.root_id))


def test_thread_of_matches_mailbox_thread_order_on_a_deep_thread(tmp_path):
    cfg = _cfg(tmp_path)
    put(cfg, "conductor", 1, "human", T(1), "a")
    put(cfg, "steward:Demo", 1, "conductor", T(2), "b", re="conductor:1")
    put(cfg, "conductor", 2, "steward:Demo", T(3), "c", re="steward:Demo:1")
    put(cfg, "human", 1, "conductor", T(3), "side", re="conductor:1")
    assert [m["id"] for m in AM.thread_of(AM.snapshot(cfg), "conductor:1")] == [
        m["id"] for m in MB.thread(cfg, "conductor:1")]
    assert AM.thread_of(AM.snapshot(cfg), "nope") == []


def test_thread_root_dangling_and_cycle(tmp_path):
    cfg = _cfg(tmp_path)
    put(cfg, "conductor", 1, "human", T(1), "root")
    put(cfg, "conductor", 2, "human", T(2), "reply", re="conductor:1")
    put(cfg, "conductor", 3, "human", T(3), "orphan", re="gone:9")
    # a hand-written cycle b -> c -> d -> b, with a tail hanging off c
    put(cfg, "steward:Demo", 2, "human", T(4), "b", re="steward:Demo:4")
    put(cfg, "steward:Demo", 3, "human", T(5), "c", re="steward:Demo:2")
    put(cfg, "steward:Demo", 4, "human", T(6), "d", re="steward:Demo:3")
    put(cfg, "steward:Demo", 5, "human", T(7), "tail", re="steward:Demo:3")
    msgs = AM.snapshot(cfg)
    by_id = {m["id"]: m for m in msgs}
    assert AM.thread_root(by_id, by_id["conductor:2"]) == "conductor:1"
    assert AM.thread_root(by_id, by_id["conductor:3"]) == "conductor:3"
    for i in ("2", "3", "4", "5"):
        assert AM.thread_root(by_id, by_id[f"steward:Demo:{i}"]) == "steward:Demo:2"
    rows = AM.load_threads(cfg, "steward:Demo", msgs)
    assert [r.root_id for r in rows] == ["steward:Demo:2"] and rows[0].count == 4
    assert {r.root_id for r in AM.load_threads(cfg, "conductor", msgs)} == {
        "conductor:1", "conductor:3"}
    assert "steward:Demo:2" in str(AM.thread_text(cfg, "steward:Demo:2", msgs))


def test_load_threads_groups_across_inboxes_and_orders(tmp_path):
    cfg = world(tmp_path)
    rows = AM.load_threads(cfg, "conductor")
    # the open question (T5) first although the answered thread has a later reply... then newest
    assert [r.root_id for r in rows] == ["conductor:2", "conductor:1"]
    q2, q1 = rows
    assert q2.unanswered and q2.count == 1 and q2.peer == "steward:Other"
    assert q2.subject == "which base?" and q2.campaign is None
    assert q2.reply_to["id"] == "conductor:2"
    # the reply was stored in steward:Demo's inbox but joins the thread
    assert not q1.unanswered and q1.count == 2 and q1.campaign == "C1"
    assert q1.subject == "why is the gate red?" and q1.last_ts == T(2)
    assert q1.reply_to["id"] == "conductor:1"
    # peer: the latest message touching the conductor is the question; its other party
    assert q1.peer == "steward:Demo"
    # the same thread seen from the steward inbox: the human's answer is the only message there
    (s,) = AM.load_threads(cfg, "steward:Demo")
    assert s.root_id == "conductor:1" and s.reply_to["id"] == "steward:Demo:1"
    assert s.peer == "human" and not s.unanswered
    assert AM.load_threads(cfg, "steward:Other") == []


def test_unanswered_first_then_newest(tmp_path):
    cfg = _cfg(tmp_path)
    put(cfg, "conductor", 1, "human", T(1), "old open", "question")
    put(cfg, "conductor", 2, "human", T(9), "new info", "info")
    put(cfg, "conductor", 3, "human", T(5), "mid open", "request")
    put(cfg, "conductor", 4, "human", T(8), "answered q", "question")
    put(cfg, "human", 1, "conductor", T(9), "done", "answer", re="conductor:4")
    assert [r.subject for r in AM.load_threads(cfg, "conductor")] == [
        "mid open", "old open", "new info", "answered q"]


def test_peer_when_the_latest_message_is_from_human_and_when_none_touches_addr(tmp_path):
    cfg = _cfg(tmp_path)
    put(cfg, "conductor", 1, "steward:Demo", T(1), "q", "question")
    put(cfg, "steward:Demo", 1, "human", T(5), "answer", "answer", re="conductor:1")
    t = AM.load_threads(cfg, "conductor")[0]
    assert t.peer == "steward:Demo"             # human→steward does not touch the conductor
    assert t.last_ts == T(5)
    thread = AM.thread_of(AM.snapshot(cfg), "conductor:1")
    assert AM._peer(thread, "steward:Other") == "human"     # no touching message: latest `from`
    # from the human's own inbox the counterpart is the sender
    put(cfg, "human", 1, "conductor", T(6), "fyi", re="conductor:1")
    assert AM.load_threads(cfg, "human")[0].peer == "conductor"


def test_subject_is_the_first_line_clipped(tmp_path):
    cfg = _cfg(tmp_path)
    put(cfg, "conductor", 1, "human", T(1), "z" * 200 + "\nsecond")
    assert AM.load_threads(cfg, "conductor")[0].subject == "z" * 80


def test_thread_text_header_text_and_markers(tmp_path):
    cfg = world(tmp_path)
    plain = AM.thread_text(cfg, "conductor:1").plain
    lines = plain.splitlines()
    assert lines[0] == f"conductor:1  {T(1)}  steward:Demo → conductor  question  [C1]"
    assert "    why is the gate red?" in lines and "    more" in lines
    assert f"steward:Demo:1  {T(2)}  human → steward:Demo  answer  [C1]" in lines
    assert "needs reply" not in plain           # answered
    t2 = AM.thread_text(cfg, "conductor:2")
    assert "needs reply" in t2.plain
    assert any(s.style == "yellow" and t2.plain[s.start:s.end].strip() == "needs reply"
               for s in t2.spans)
    assert AM.thread_text(cfg, "missing:1").plain == ""


def test_send_plan_refusals_and_defaults(tmp_path):
    cfg = _cfg(tmp_path)
    plan = AM.send_plan(cfg, to="steward:demo", kind="request", text="do it", campaign="C1")
    assert plan == {"to": "steward:Demo", "frm": "human", "kind": "request", "campaign": "C1",
                    "re": None, "text": "do it", "needs_reply": True}
    assert AM.send_plan(cfg, to="conductor", kind="info", text="x")["needs_reply"] is False
    for kw, msg in (({"to": "bob", "kind": "info", "text": "x"}, "unknown address"),
                    ({"to": "conductor", "kind": "bogus", "text": "x"}, "unknown kind"),
                    ({"to": "conductor", "kind": "info", "text": "  \n"}, "empty"),
                    ({"to": "steward:Nope", "kind": "info", "text": "x"}, "unknown steward")):
        with pytest.raises(ValueError, match=msg):
            AM.send_plan(cfg, **kw)
    assert not MB.mailbox_dir(cfg).exists()     # nothing written by planning


def test_send_apply_sends_as_human(tmp_path):
    cfg = _cfg(tmp_path)
    plan = AM.send_plan(cfg, to="conductor", kind="question", text="ok?", campaign="C1",
                        re="x:1")
    rec = AM.send_apply(cfg, plan)
    assert rec == MB.read_inbox(cfg, "conductor")[0]
    assert (rec["from"], rec["kind"], rec["campaign"], rec["re"], rec["needs_reply"]) == (
        "human", "question", "C1", "x:1", True)


def test_mailbox_signature_changes_on_a_send(tmp_path):
    cfg = _cfg(tmp_path)
    assert AM.mailbox_signature(cfg) == ()
    MB.send(cfg, to="conductor", frm="human", text="a")
    first = AM.mailbox_signature(cfg)
    assert [n for n, *_ in first] == ["conductor.jsonl"]
    assert AM.mailbox_signature(cfg) == first
    MB.send(cfg, to="human", frm="conductor", text="b")
    assert AM.mailbox_signature(cfg) != first


def test_reply_target_and_default_kind():
    q = {"id": "conductor:1", "from": "steward:Demo", "to": "conductor", "kind": "question"}
    assert AM.reply_target(q) == ("steward:Demo", "conductor:1")
    own = {"id": "conductor:3", "from": "human", "to": "conductor", "kind": "request",
           "re": "conductor:2"}
    assert AM.reply_target(own) == ("conductor", "conductor:2")
    assert AM.reply_target({**own, "re": None}) == ("conductor", None)
    assert AM.default_reply_kind(q) == "answer"
    assert AM.default_reply_kind({"kind": "request"}) == "answer"
    assert AM.default_reply_kind({"kind": "info"}) == "info"
    assert AM.default_reply_kind({"kind": "answer"}) == "info"


def test_human_follow_up_keeps_the_request_unanswered(tmp_path):
    cfg = _cfg(tmp_path)
    req = put(cfg, "conductor", 1, "human", T(1), "please park C1", "request")
    row = AM.load_threads(cfg, "conductor")[0]
    to, re = AM.reply_target(row.reply_to)
    assert (to, re) == ("conductor", None)
    AM.send_apply(cfg, AM.send_plan(cfg, to=to, kind="info", text="more detail", re=re))
    assert [m["id"] for m in MB.unanswered(cfg, "conductor")] == [req["id"]]


def test_answers_notice(tmp_path):
    cfg = world(tmp_path)
    msgs = AM.snapshot(cfg)
    assert AM.answers_notice(msgs, "conductor:2") == "; this answers it for conductor"
    assert AM.answers_notice(msgs, "conductor:1") == ""     # already answered
    assert AM.answers_notice(msgs, "human:1") == ""         # the human's own inbox
    assert AM.answers_notice(msgs, None) == ""
