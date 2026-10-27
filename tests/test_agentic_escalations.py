"""SPEC-0232: escalation records — plan/apply, the parks they cause, withdrawal when the
campaign leaves the park, the notifier and the reminders. Nothing real is notified: the
notifier and `shutil.which` are monkeypatched."""
import datetime as dt
import json

import pytest

from wt.agentic import escalations as ESC
from wt.agentic import events as E
from wt.agentic import lease as L
from wt.agentic import mailbox as M
from wt.agentic import queue as Q
from wt.agentic import store as S

T0 = dt.datetime(2026, 10, 25, 12, 0, 0).astimezone()
CID = "SPEC-0900-20261025-aaaa"


class _Done:
    returncode, stdout, stderr = 0, "", ""


@pytest.fixture(autouse=True)
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(ESC, "_run_notify", lambda argv: seen.append(list(argv)) or _Done())
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/notify-send")
    return seen


def _cfg(tmp_path, **esc):
    cfg = {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "config"),
           "agentic": {"lease_ttl_s": 120}, "outbox_targets": {}, "specs_dir": str(tmp_path)}
    if esc:
        cfg["agentic"]["escalations"] = esc
    return cfg


def _camp(cfg, state="IMPLEMENTING", cid=CID, **over):
    st = {"id": cid, "spec_id": cid[:9], "project": "Meta-Tools", "profile": "balanced",
          "state": state, "repo": {"base": "main"}, "park": None,
          "counters": {"loops": 0, "runs": 0, "parks": 0},
          "cost": {"total_usd": 0.0, "cap_usd": 10.0}}
    st.update(over)
    S.create_campaign(cfg, st)
    return cid


def _parked(cfg, reason="loop_exhausted", resume_to="IMPLEMENTING", **park):
    return _camp(cfg, "PARKED", park={"reason": reason, "detail": "d", "since": S.now_iso(),
                                      "resume_to": resume_to, **park})


def _esc(cfg, cid=CID, **kw):
    kw.setdefault("question", "ship it?")
    kw.setdefault("options", ["yes", "no"])
    kw.setdefault("recommend", 1)
    plan = ESC.plan_escalate(cfg, cid, **kw)
    return ESC.apply_escalate(cfg, plan)


def _state(cfg, cid=CID):
    return S.read_state(cfg, cid)


# ---- records -----------------------------------------------------------------------------

def test_campaignless_plan_writes_nothing_and_apply_writes_every_field(tmp_path):
    cfg = _cfg(tmp_path)
    plan = ESC.plan_escalate(cfg, None, question="runner broken?", options=["restart", "wait"],
                             recommend=2, facts=["dead 10m"], frm="conductor")
    assert plan["park"] is None and ESC.list_all(cfg) == []
    rec = ESC.apply_escalate(cfg, plan)
    assert rec["id"] == "ESC-0001"
    assert rec == ESC.read(cfg, "ESC-0001")
    assert rec["schema"] == "wt.escalation.v1" and rec["status"] == "open"
    assert (rec["campaign"], rec["park"], rec["by"]) == (None, None, "conductor")
    assert rec["options"] == [{"n": 1, "label": "restart"}, {"n": 2, "label": "wait"}]
    assert rec["recommendation"] == 2 and rec["facts"] == ["dead 10m"]
    assert rec["mail_id"] == "human:1" and rec["notified"]["via"] == "notify-send"
    for k in ("answer", "answered_by", "answered_at", "withdrawn", "reminded_at"):
        assert rec[k] is None
    assert _esc(cfg, None, question="two")["id"] == "ESC-0002"
    assert [r["id"] for r in ESC.list_all(cfg)] == ["ESC-0002", "ESC-0001"]


def test_refusals(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError, match="not an option"):
        ESC.plan_escalate(cfg, None, question="q", options=["a"], recommend=2)
    with pytest.raises(ValueError, match="needs at least one"):
        ESC.plan_escalate(cfg, None, question="q", recommend=1)
    with pytest.raises(ValueError, match="unknown address"):
        ESC.plan_escalate(cfg, None, question="q", frm="nobody")
    _camp(cfg, "MERGED")
    with pytest.raises(ValueError, match="MERGED"):
        ESC.plan_escalate(cfg, CID, question="q")
    other = "SPEC-0901-20261025-bbbb"
    _camp(cfg, "QUEUED", cid=other)
    rec = _esc(cfg, other)
    with pytest.raises(ValueError, match=rec["id"]):
        ESC.plan_escalate(cfg, other, question="again")


# ---- parks -------------------------------------------------------------------------------

def test_parked_campaign_is_repark_keeping_resume_to_without_counting(tmp_path):
    cfg = _cfg(tmp_path)
    _parked(cfg, "loop_exhausted", "REVIEWING")
    st = _state(cfg)
    st["counters"]["parks"] = 1
    S.write_state(cfg, CID, st)
    rec = _esc(cfg)
    st = _state(cfg)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "human_decision"
    assert st["park"]["escalated_from"] == "loop_exhausted"
    assert st["park"]["resume_to"] == "REVIEWING"
    assert st["park"]["detail"] == f"{rec['id']}: ship it?"
    assert st["counters"]["parks"] == 1
    assert rec["park"] == {"escalated_from": "loop_exhausted", "resume_to": "REVIEWING",
                           "mode": "immediate"}
    parks = S.read_ledger(cfg, CID, types=["park"])
    assert parks[-1]["reason"] == "human_decision" and parks[-1]["by"] == "conductor"
    assert Q.park_owner(st["park"]["reason"]) == "human"


@pytest.mark.parametrize("state,resume", [("QUEUED", "QUEUED"), ("IMPLEMENTING", "IMPLEMENTING")])
def test_unstarted_or_idle_campaign_parks_immediately(tmp_path, state, resume):
    cfg = _cfg(tmp_path)
    _camp(cfg, state)
    _esc(cfg)
    st = _state(cfg)
    assert st["state"] == "PARKED" and st["park"]["resume_to"] == resume
    assert st["park"]["escalated_from"] is None and st["counters"]["parks"] == 1


def test_waiting_campaign_parks_immediately_to_its_resume_target(tmp_path):
    cfg = _cfg(tmp_path)
    _camp(cfg, "WAITING", waiting={"since": S.now_iso(), "resume_to": "GATING"})
    _esc(cfg)
    st = _state(cfg)
    assert st["state"] == "PARKED" and st["park"]["resume_to"] == "GATING"


def test_repark_after_withdraw_keeps_origin_and_does_not_count(tmp_path):
    cfg = _cfg(tmp_path)
    _parked(cfg, "cost_cap", "GATING")
    first = _esc(cfg)
    ESC.withdraw(cfg, first["id"], "settled", "conductor")
    assert ESC.open_for(cfg, CID) is None
    parks = _state(cfg)["counters"]["parks"]
    second = _esc(cfg, question="follow-up?")
    st = _state(cfg)
    assert second["park"]["escalated_from"] == "cost_cap"
    assert st["park"]["escalated_from"] == "cost_cap" and st["park"]["resume_to"] == "GATING"
    assert st["counters"]["parks"] == parks


def test_held_lease_defers_and_plan_step_parks_as_human_decision(tmp_path):
    cfg = _cfg(tmp_path)
    _camp(cfg, "IMPLEMENTING")
    lease = L.acquire(cfg, [CID])
    try:
        rec = _esc(cfg, frm="conductor")
    finally:
        lease.release()
    st = _state(cfg)
    assert st["state"] == "IMPLEMENTING" and rec["park"]["mode"] == "deferred"
    req = st["park_requested"]
    assert req["reason"] == "human_decision" and req["by"] == "conductor"
    assert req["detail"] == f"{rec['id']}: ship it?"
    step = Q.plan_step(st, {"personas": {}})
    assert (step.kind, step.reason, step.by) == ("park", "human_decision", "conductor")
    row = Q._park_row(cfg, st, step)
    st = _state(cfg)
    assert row["reason"] == "human_decision" and st["park"]["reason"] == "human_decision"
    assert S.read_ledger(cfg, CID, types=["park"])[-1]["by"] == "conductor"
    assert ESC.open_for(cfg, CID)["id"] == rec["id"]


def test_deferred_escalation_replaces_a_human_park_request(tmp_path):
    cfg = _cfg(tmp_path)
    _camp(cfg, "IMPLEMENTING", park_requested={"reason": "human", "detail": "hold on",
                                               "at": S.now_iso()})
    lease = L.acquire(cfg, [CID])
    try:
        rec = _esc(cfg)
    finally:
        lease.release()
    assert _state(cfg)["park_requested"]["reason"] == "human_decision"
    assert "park requested earlier: hold on" in rec["facts"]


def test_withdrawing_a_replacing_escalation_restores_the_human_request(tmp_path):
    cfg = _cfg(tmp_path)
    earlier = {"reason": "human", "detail": "hold on", "at": S.now_iso()}
    _camp(cfg, "IMPLEMENTING", park_requested=dict(earlier))
    lease = L.acquire(cfg, [CID])
    try:
        rec = _esc(cfg)
    finally:
        lease.release()
    ESC.withdraw(cfg, rec["id"], "resolved", "conductor")
    assert _state(cfg)["park_requested"] == earlier


def test_apply_refuses_when_the_park_request_changed_since_the_plan(tmp_path):
    cfg = _cfg(tmp_path)
    _camp(cfg, "IMPLEMENTING")
    lease = L.acquire(cfg, [CID])
    try:
        plan = ESC.plan_escalate(cfg, CID, question="ship it?")
        st = _state(cfg)
        st["park_requested"] = {"reason": "human", "detail": "hold on", "at": S.now_iso()}
        S.write_state(cfg, CID, st)
        with pytest.raises(ValueError, match="changed since the plan"):
            ESC.apply_escalate(cfg, plan)
    finally:
        lease.release()
    assert ESC.open_for(cfg, CID) is None


def test_withdrawing_a_deferred_escalation_drops_the_pending_request(tmp_path):
    cfg = _cfg(tmp_path)
    _camp(cfg, "IMPLEMENTING")
    lease = L.acquire(cfg, [CID])
    try:
        rec = _esc(cfg)
    finally:
        lease.release()
    ESC.withdraw(cfg, rec["id"], "not needed", "conductor")
    assert "park_requested" not in _state(cfg)


def test_apply_after_the_campaign_changed_leaves_no_record(tmp_path):
    cfg = _cfg(tmp_path)
    _parked(cfg, "loop_exhausted")
    plan = ESC.plan_escalate(cfg, CID, question="q")
    Q.apply_resume(cfg, Q.plan_resume(cfg, CID))
    assert _state(cfg)["state"] != "PARKED"
    with pytest.raises(ValueError, match="changed since the plan"):
        ESC.apply_escalate(cfg, plan)
    assert ESC.list_all(cfg) == [] and not list(ESC.escalations_dir(cfg).glob("ESC-*.json"))


def test_a_park_that_raises_removes_the_record(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _parked(cfg, "loop_exhausted")
    plan = ESC.plan_escalate(cfg, CID, question="q")

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(Q, "_park", boom)
    with pytest.raises(OSError):
        ESC.apply_escalate(cfg, plan)
    assert list(ESC.escalations_dir(cfg).glob("ESC-*.json")) == []
    assert ESC.open_for(cfg, CID) is None


# ---- event, mail --------------------------------------------------------------------------

def test_event_and_human_mail(tmp_path):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    rec = _esc(cfg, facts=["f1"])
    ev = E.read_since(cfg, 0, types=["escalation"])
    assert len(ev) == 1 and ev[0]["campaign"] == CID and ev[0]["by"] == "conductor"
    assert ev[0]["data"]["escalation_id"] == rec["id"] and ev[0]["data"]["recommendation"] == 1
    [msg] = M.read_inbox(cfg, "human")
    assert msg["kind"] == "question" and msg["needs_reply"] is False
    assert msg["id"] == rec["mail_id"] and msg["campaign"] == CID
    assert "1. yes (recommended)" in msg["text"] and "2. no" in msg["text"]
    assert f"answer: wt escalations show {rec['id']}" in msg["text"]
    assert M.unanswered(cfg, "human") == []


# ---- leaving the park ---------------------------------------------------------------------

def test_resume_withdraws_the_open_record_but_not_an_answered_one(tmp_path):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    rec = _esc(cfg)
    Q.apply_resume(cfg, Q.plan_resume(cfg, CID))
    got = ESC.read(cfg, rec["id"])
    assert got["status"] == "withdrawn" and got["withdrawn"]["by"] == "human"
    assert got["withdrawn"]["reason"] == "campaign resumed by human"
    assert [e["type"] for e in E.read_since(cfg, 0, types=["escalation_withdrawn"])]
    notes = [e["text"] for e in S.read_ledger(cfg, CID, types=["note"])]
    assert any(f"escalation {rec['id']} withdrawn" in t for t in notes)
    # an answered record is left alone
    Q.apply_park(cfg, Q.plan_park(cfg, CID, "again"))
    second = _esc(cfg)
    ESC._mutate(cfg, second["id"], lambda r: r.update(status="answered", answer={"n": 1}))
    Q.apply_resume(cfg, Q.plan_resume(cfg, CID))
    assert ESC.read(cfg, second["id"])["status"] == "answered"


def test_abandon_withdraws_the_open_record(tmp_path):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    rec = _esc(cfg)
    Q.apply_abandon(cfg, Q.plan_abandon(cfg, CID))
    got = ESC.read(cfg, rec["id"])
    assert got["status"] == "withdrawn"
    assert got["withdrawn"]["reason"] == "campaign abandoned by human"


def test_terminal_transition_withdraws_the_open_record(tmp_path):
    cfg = _cfg(tmp_path)
    _camp(cfg, "CLOSING")
    lease = L.acquire(cfg, [CID])
    try:
        rec = _esc(cfg)
    finally:
        lease.release()
    Q._transition(cfg, _state(cfg), "MERGED", "merged")
    got = ESC.read(cfg, rec["id"])
    assert got["status"] == "withdrawn" and got["withdrawn"]["reason"] == "campaign MERGED"


def test_hand_edited_state_is_skipped_and_remind_due_withdraws(tmp_path, calls):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    rec = _esc(cfg)
    st = _state(cfg)
    st["park"] = {**st["park"], "reason": "cost_cap"}
    S.write_state(cfg, CID, st)
    assert ESC.open_for(cfg, CID) is None
    assert E.ping_digest(cfg, T0)["escalations_open"] == []
    n = len(calls)
    assert ESC.remind_due(cfg, T0 + dt.timedelta(hours=3)) == []
    assert len(calls) == n
    got = ESC.read(cfg, rec["id"])
    assert got["status"] == "withdrawn" and got["withdrawn"]["reason"] == "campaign left the park"


# ---- notifier -----------------------------------------------------------------------------

def test_notify_auto_runs_notify_send(tmp_path, calls):
    cfg = _cfg(tmp_path)
    rec = _esc(cfg, None)
    assert calls == [["/usr/bin/notify-send", "--app-name=wt", "--urgency=critical",
                      "--", f"wt: {rec['id']} needs you", "ship it?"]]
    assert rec["notified"]["via"] == "notify-send" and rec["notified"]["ok"] is True


def test_notify_auto_without_notify_send_does_nothing(tmp_path, calls, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    rec = _esc(_cfg(tmp_path), None)
    assert calls == [] and rec["notified"]["via"] == "none"


def test_notify_none_never_runs(tmp_path, calls):
    rec = _esc(_cfg(tmp_path, notify="none"), None)
    assert calls == [] and rec["notified"] == {**rec["notified"], "via": "none", "ok": True}


def test_notify_custom_argv_substitutes(tmp_path, calls):
    cfg = _cfg(tmp_path, notify=["say", "{id} {campaign} {question} rec={recommendation} {nope}"])
    _parked(cfg)
    rec = _esc(cfg)
    assert calls == [["say", f"{rec['id']} {CID} ship it? rec=1 {{nope}}"]]
    assert rec["notified"]["via"] == "command"


def test_failing_notifier_is_recorded_and_the_escalation_succeeds(tmp_path, monkeypatch):
    def boom(argv):
        raise FileNotFoundError("no such command")
    monkeypatch.setattr(ESC, "_run_notify", boom)
    rec = _esc(_cfg(tmp_path), None)
    assert rec["status"] == "open"
    assert rec["notified"]["ok"] is False and "no such command" in rec["notified"]["error"]

    class Bad:
        returncode, stdout, stderr = 3, "", "nope"
    monkeypatch.setattr(ESC, "_run_notify", lambda argv: Bad())
    rec = _esc(_cfg(tmp_path), None, question="two")
    assert rec["notified"]["ok"] is False and "exit 3" in rec["notified"]["error"]


# ---- reminders ----------------------------------------------------------------------------

def _aged(cfg, esc_id, **fields):
    ESC._mutate(cfg, esc_id, lambda r: r.update(fields))


def test_remind_due_once_per_interval_and_never_for_withdrawn(tmp_path, calls):
    cfg = _cfg(tmp_path, remind_s=600)
    rec = _esc(cfg, None)
    done = _esc(cfg, None, question="gone")
    ESC.withdraw(cfg, done["id"], "x", "conductor")
    created = dt.datetime.fromisoformat(rec["created"])
    calls.clear()
    assert ESC.remind_due(cfg, created + dt.timedelta(seconds=599)) == []
    due = created + dt.timedelta(seconds=601)
    out = ESC.remind_due(cfg, due)
    assert [o["id"] for o in out] == [rec["id"]]
    assert calls[0][-2] == f"wt: {rec['id']} still needs you"
    assert ESC.read(cfg, rec["id"])["reminded_at"]
    assert ESC.remind_due(cfg, due) == []                 # a second caller at the same time
    assert len(calls) == 1
    assert len(ESC.remind_due(cfg, due + dt.timedelta(seconds=601))) == 1


def test_remind_due_off_at_zero(tmp_path, calls):
    cfg = _cfg(tmp_path, remind_s=0)
    _esc(cfg, None)
    calls.clear()
    assert ESC.remind_due(cfg, T0 + dt.timedelta(days=30)) == []
    assert calls == []


def test_sync_ping_reminds(tmp_path, calls):
    cfg = _cfg(tmp_path, remind_s=60)
    rec = _esc(cfg, None)
    calls.clear()
    E.sync(cfg, now=dt.datetime.fromisoformat(rec["created"]) + dt.timedelta(seconds=61),
           ping=True)
    assert len(calls) == 1


def test_ping_digest_lists_open_escalations(tmp_path):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    rec = _esc(cfg)
    created = dt.datetime.fromisoformat(rec["created"])
    d = E.ping_digest(cfg, created + dt.timedelta(seconds=90))
    assert d["escalations_open"] == [{"id": rec["id"], "campaign": CID, "age_s": 90}]
    assert d["counts"]["escalations_open"] == 1 and d["counts"]["needs_you"] == 1
    json.dumps(d)


# ---- review hardening ---------------------------------------------------------------------

def _infos(cfg):
    return [m for m in M.read_inbox(cfg, "human") if m["kind"] == "info"]


def test_two_plans_before_a_deferred_apply_make_one_record(tmp_path):
    cfg = _cfg(tmp_path)
    _camp(cfg, "IMPLEMENTING")
    lease = L.acquire(cfg, [CID])
    try:
        a = ESC.plan_escalate(cfg, CID, question="a")
        b = ESC.plan_escalate(cfg, CID, question="b")
        ESC.apply_escalate(cfg, a)
        with pytest.raises(ValueError, match="changed since the plan"):
            ESC.apply_escalate(cfg, b)
    finally:
        lease.release()
    assert len(list(ESC.escalations_dir(cfg).glob("ESC-*.json"))) == 1


def test_park_reason_changed_between_plan_and_apply_is_refused(tmp_path):
    cfg = _cfg(tmp_path)
    _parked(cfg, "loop_exhausted")
    plan = ESC.plan_escalate(cfg, CID, question="q")
    st = _state(cfg)
    st["park"]["reason"] = "cost_cap"
    S.write_state(cfg, CID, st)
    with pytest.raises(ValueError, match="changed since the plan"):
        ESC.apply_escalate(cfg, plan)
    assert not list(ESC.escalations_dir(cfg).glob("ESC-*.json"))


def test_withdraw_mails_human_info_for_manual_and_automatic_withdrawals(tmp_path):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    rec = _esc(cfg)
    ESC.withdraw(cfg, rec["id"], "not needed", "conductor")
    [m] = _infos(cfg)
    assert rec["id"] in m["text"] and m["from"] == "conductor"
    rec2 = _esc(cfg)
    Q.apply_resume(cfg, Q.plan_resume(cfg, CID))
    assert [rec["id"], rec2["id"]] == [r["id"] for r in reversed(ESC.list_all(cfg))]
    assert len(_infos(cfg)) == 2 and rec2["id"] in _infos(cfg)[-1]["text"]
    _parked_again = _camp(cfg, "QUEUED", cid="SPEC-0901-20261025-bbbb")
    rec3 = _esc(cfg, _parked_again)
    Q._transition(cfg, _state(cfg, _parked_again), "ABANDONED", "x", by="tick")
    last = _infos(cfg)[-1]
    assert rec3["id"] in last["text"] and last["from"] == "conductor"


@pytest.mark.parametrize("leased", [True, False])
def test_park_does_not_cancel_a_pending_escalation(tmp_path, leased):
    cfg = _cfg(tmp_path)
    _camp(cfg, "IMPLEMENTING")
    lease = L.acquire(cfg, [CID])
    try:
        rec = _esc(cfg)
    finally:
        lease.release()
    lease = L.acquire(cfg, [CID]) if leased else None
    try:
        with pytest.raises(ValueError, match=rec["id"]):
            Q.plan_park(cfg, CID, "x")
        plan = {"campaign": CID, "state": "IMPLEMENTING", "reason": "x", "resume_to": "IMPLEMENTING",
                "mode": "deferred" if leased else "immediate"}
        with pytest.raises(ValueError, match=rec["id"]):
            Q.apply_park(cfg, plan)
    finally:
        if lease:
            lease.release()
    st = _state(cfg)
    assert st["state"] == "IMPLEMENTING" and st["park_requested"]["reason"] == "human_decision"
    assert ESC.open_for(cfg, CID)["id"] == rec["id"]


def test_a_failing_mail_does_not_fail_the_escalation(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(M, "send", boom)
    rec = _esc(cfg, None)
    assert rec["mail_id"] is None and "mail: disk full" in rec["notified"]["error"]
    assert ESC.read(cfg, rec["id"])["status"] == "open"


# ---- review round 3 ------------------------------------------------------------------------

def test_escalating_again_withdraws_a_stale_open_record(tmp_path):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    old = _esc(cfg)
    st = _state(cfg)
    st["park"] = {**st["park"], "reason": "cost_cap"}      # hand edit: the escalation is lost
    S.write_state(cfg, CID, st)
    new = _esc(cfg, question="again")
    assert ESC.read(cfg, old["id"])["status"] == "withdrawn"
    assert [r["id"] for r in ESC.list_all(cfg, "open")] == [new["id"]]
    assert ESC.open_for(cfg, CID)["id"] == new["id"]
    assert [r["id"] for r in ESC.open_live(cfg)] == [new["id"]]


def test_withdraw_for_campaign_closes_every_open_record(tmp_path):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    a = _esc(cfg)
    ESC._create(cfg, {"by": "conductor", "campaign": CID, "spec_id": None, "project": None,
                      "question": "dup", "options": [], "recommendation": None,
                      "facts": [], "park": None})
    ESC.withdraw_for_campaign(cfg, CID, "done", "conductor")
    assert ESC.list_all(cfg, "open") == [] and ESC.read(cfg, a["id"])["status"] == "withdrawn"


def test_remind_due_claims_under_the_escalations_lock(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, remind_s=60)
    rec = _esc(cfg, None)
    held = []
    real_locked, real_write = E._locked, ESC._write

    class _Tracked:
        def __init__(self, path):
            self.cm = real_locked(path)

        def __enter__(self):
            self.cm.__enter__()
            held.append(1)

        def __exit__(self, *exc):
            held.pop()
            return self.cm.__exit__(*exc)
    writes = []

    def write(c, r):
        writes.append(bool(held))
        real_write(c, r)
    monkeypatch.setattr(E, "_locked", _Tracked)
    monkeypatch.setattr(ESC, "_write", write)
    ESC.remind_due(cfg, dt.datetime.fromisoformat(rec["created"]) + dt.timedelta(seconds=61))
    assert writes and all(writes)            # the claim (and the outcome) write under the lock


def test_remind_due_skips_a_record_that_raises(tmp_path, calls, monkeypatch):
    cfg = _cfg(tmp_path, remind_s=60)
    bad = _esc(cfg, None, question="bad")
    good = _esc(cfg, None, question="good")
    real = ESC._claim

    def claim(c, esc_id, *a):
        if esc_id == bad["id"]:
            raise OSError("disk gone")
        return real(c, esc_id, *a)
    monkeypatch.setattr(ESC, "_claim", claim)
    calls.clear()
    out = ESC.remind_due(cfg, dt.datetime.fromisoformat(good["created"])
                         + dt.timedelta(seconds=61))
    assert [o["id"] for o in out] == [good["id"]] and len(calls) == 1


def test_withdraw_still_mails_when_the_event_append_fails(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    rec = _esc(cfg)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(E, "append", boom)
    ESC.withdraw(cfg, rec["id"], "not needed", "conductor")
    assert ESC.read(cfg, rec["id"])["status"] == "withdrawn"
    [m] = _infos(cfg)
    assert rec["id"] in m["text"]


def test_malformed_escalations_config_uses_defaults(tmp_path, calls):
    cfg = _cfg(tmp_path)
    cfg["agentic"]["escalations"] = ["notify-send"]
    _parked(cfg)
    rec = _esc(cfg)
    assert rec["mail_id"] and rec["notified"]["ok"] and rec["notified"]["via"] == "notify-send"
    assert ESC.settings(cfg) == ESC.DEFAULTS
    cfg["agentic"]["escalations"] = {"remind_s": "soon", "notify": 5}
    ESC.remind_due(cfg, T0 + dt.timedelta(days=3))        # a bad remind_s falls back; no raise
    assert ESC.notify(cfg, rec)["ok"] is False


def test_campaign_record_and_event_carry_spec_id_and_project(tmp_path):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    rec = _esc(cfg)
    assert (rec["spec_id"], rec["project"]) == (CID[:9], "Meta-Tools")
    ev = [e for e in E.read_since(cfg, 0) if e["type"] == "escalation"][-1]
    assert (ev["campaign"], ev["spec_id"], ev["project"]) == (CID, CID[:9], "Meta-Tools")


# ---- SPEC-0235: answers ------------------------------------------------------------------

RV = "conformance_reviewer"
AC4 = "AC 4 output is sorted"


def _reviewed(cfg, extra_unmet=()):
    """A campaign parked `human_decision` after a review with a major, a minor and an unmet
    AC; returns its open escalation."""
    verdict = {"verdict": "fix", "score": 40, "fix_brief": "x", "loop": 0, "run": "002-r",
               "findings": [{"severity": "major", "title": "Wrong order", "files": ["a.py"]},
                            {"severity": "minor", "title": "Typo", "files": ["b.py"]}],
               "ac_assessment": [{"ac": AC4, "met": False}, {"ac": "AC 5 other", "met": True},
                                 *[{"ac": a, "met": False} for a in extra_unmet]]}
    _camp(cfg, "PARKED", last_review={"verdicts": {RV: verdict}},
          park={"reason": "loop_exhausted", "detail": "d", "since": S.now_iso(),
                "resume_to": "IMPLEMENTING"})
    return _esc(cfg, question="waive?", options=["yes", "no"])


def _answer(cfg, esc_id="ESC-0001", **kw):
    plan = ESC.plan_answer(cfg, esc_id, **kw)
    return plan, ESC.apply_answer(cfg, plan)


def test_choose_by_number_and_by_label(tmp_path):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    _esc(cfg, options=["Resume now", "Abandon"])
    plan = ESC.plan_answer(cfg, "ESC-0001", choose="2")
    assert plan["answer"]["choice"] == 2 and ESC.read(cfg, "ESC-0001")["status"] == "open"
    _, rec = _answer(cfg, choose="  resume NOW ")
    assert rec["status"] == "answered" and rec["answered_by"] == "human" and rec["answered_at"]
    assert rec["answer"] == {"kind": "choose", "choice": 1, "label": "Resume now", "text": None,
                             "waive": [], "attest": [], "reason": None, "note": None}
    assert _state(cfg)["state"] == "PARKED" and _state(cfg)["park"]["reason"] == "human_decision"
    notes = [n["text"] for n in S.read_ledger(cfg, CID, types=["note"])]
    assert notes[-1] == "human: ESC-0001 chose 1 (Resume now)"
    [hd] = S.read_ledger(cfg, CID, types=["human_decision"])
    assert hd["escalation"] == "ESC-0001" and hd["kind"] == "choose"


def test_text_answer_by_steward_and_pinned_notes(tmp_path):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    _esc(cfg)
    long = "line one\n  line two " + "x" * 300
    plan, rec = _answer(cfg, text=long, note="n" * 250, by="steward:Meta-Tools")
    assert rec["answered_by"] == "steward:Meta-Tools"
    one = " ".join(long.split())[:200] + "…"
    assert plan["summary"] == f"text: {one} — note: {'n' * 200}…"
    assert [n["text"] for n in S.read_ledger(cfg, CID, types=["note"])][-1] == (
        f"steward:Meta-Tools: ESC-0001 {plan['summary']}")


def test_override_stores_overrides_and_wakes_once(tmp_path):
    cfg = _cfg(tmp_path)
    _reviewed(cfg)
    plan, rec = _answer(cfg, waive=[f"{RV}#0"], attest=[f"{RV}#ac0"], reason="checked by hand",
                        note="see PR")
    assert plan["summary"] == (f'waived {RV}#0; attested "{AC4}"; reason: checked by hand'
                               ' — note: see PR')
    st = _state(cfg)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "human_decision"
    ov = st["human_overrides"]
    [w] = ov["waive"]
    assert (w["id"], w["title"], w["files"], w["severity"], w["synthetic"], w["ac"]) == (
        f"{RV}#0", "Wrong order", ["a.py"], "major", False, None)
    assert (w["reason"], w["escalation"]) == ("checked by hand", "ESC-0001") and w["at"]
    [a] = ov["attest"]
    assert (a["ac"], a["reason"], a["escalation"]) == (AC4, "checked by hand", "ESC-0001")
    assert len(S.read_ledger(cfg, CID, types=["human_decision"])) == 1
    assert S.read_ledger(cfg, CID, types=["note"])[-1]["text"] == f"human: ESC-0001 {plan['summary']}"
    got = [e for e in E.read_since(cfg, 0) if e["type"] == "escalation_answered"]
    assert len(got) == 1 and got[0]["data"]["escalation_id"] == "ESC-0001"
    assert got[0]["data"]["kind"] == "override" and got[0]["data"]["summary"] == plan["summary"]
    before = len(E.read_since(cfg, 0))
    E.sync(cfg)
    assert [e["type"] for e in E.read_since(cfg, 0)[before:]].count("escalation_answered") == 0
    assert all(e["type"] != "human_decision" for e in E.read_since(cfg, 0))


def test_override_note_cuts_long_ac_text(tmp_path):
    cfg = _cfg(tmp_path)
    long_ac = "AC 9 " + "y" * 100
    _reviewed(cfg, extra_unmet=(long_ac,))
    plan = ESC.plan_answer(cfg, "ESC-0001", attest=[long_ac], reason="r")
    assert plan["summary"] == f'attested "{long_ac[:60]}…"; reason: r'
    plan = ESC.plan_answer(cfg, "ESC-0001", waive=[f"{RV}#1", f"{RV}#0"], reason="r")
    assert plan["summary"] == f"waived {RV}#1, {RV}#0; reason: r"


def test_waiving_a_real_finding_lists_its_ac_as_implicitly_attested(tmp_path):
    cfg = _cfg(tmp_path)
    _reviewed(cfg)
    st = _state(cfg)
    st["last_review"]["verdicts"][RV]["findings"][0]["ac"] = AC4
    S.write_state(cfg, CID, st)
    plan = ESC.plan_answer(cfg, "ESC-0001", waive=[f"{RV}#0"], reason="r")
    assert plan["override"]["implicitly_attested"] == [AC4]


def test_attest_by_ac_id_still_works_for_an_already_attested_ac(tmp_path):
    cfg = _cfg(tmp_path)
    _reviewed(cfg)
    st = _state(cfg)
    st["human_overrides"] = {"attest": [{"ac": AC4, "reason": "earlier"}]}
    S.write_state(cfg, CID, st)
    plan = ESC.plan_answer(cfg, "ESC-0001", attest=[f"{RV}#ac0"], reason="r")
    assert plan["answer"]["attest"] == [AC4]
    assert plan["override"]["reach"] == {AC4: [AC4]}


@pytest.mark.parametrize("kw,msg", [
    (dict(choose="3"), "not an option"),
    (dict(choose="maybe"), "not an option"),
    (dict(choose="1", text="x"), "exactly one"),
    (dict(), "exactly one"),
    (dict(text="x", waive=["a#0"], reason="r"), "exactly one"),
    (dict(choose="1", reason="r"), "--reason belongs"),
    (dict(waive=[f"{RV}#0"]), "non-empty --reason"),
    (dict(waive=[f"{RV}#0"], reason="   "), "non-empty --reason"),
    (dict(waive=["nobody#9"], reason="r"), "not a finding"),
    (dict(attest=["no such criterion at all"], reason="r"), "matches no unmet AC"),
    (dict(attest=[f"{RV}#ac7"], reason="r"), "not an AC finding"),
    (dict(text="  "), "empty --text"),
    (dict(text="x", by="not an address"), "address"),
])
def test_answer_refusals(tmp_path, kw, msg):
    cfg = _cfg(tmp_path)
    _reviewed(cfg)
    with pytest.raises(ValueError, match=msg):
        ESC.plan_answer(cfg, "ESC-0001", **kw)
    assert ESC.read(cfg, "ESC-0001")["status"] == "open"


def test_attest_text_covering_two_acs_is_refused_listing_both(tmp_path):
    cfg = _cfg(tmp_path)
    _reviewed(cfg, extra_unmet=("AC 4 output is sorted and stable",))
    with pytest.raises(ValueError) as e:
        ESC.plan_answer(cfg, "ESC-0001", attest=["output is sorted"], reason="r")
    assert AC4 in str(e.value) and "AC 4 output is sorted and stable" in str(e.value)
    plan = ESC.plan_answer(cfg, "ESC-0001", attest=["AC 4 output is sorted and stable"],
                           reason="r")                     # an exact text is precise enough
    assert plan["answer"]["attest"] == ["AC 4 output is sorted and stable"]
    assert plan["override"]["reach"]["AC 4 output is sorted and stable"] == [
        "AC 4 output is sorted and stable"]                # only the exact AC (SPEC-0286)


def test_choose_needs_options_and_overrides_need_a_campaign(tmp_path):
    cfg = _cfg(tmp_path)
    _esc(cfg, None, options=[], recommend=None)
    with pytest.raises(ValueError, match="no options"):
        ESC.plan_answer(cfg, "ESC-0001", choose="1")
    with pytest.raises(ValueError, match="no campaign"):
        ESC.plan_answer(cfg, "ESC-0001", waive=["a#0"], reason="r")
    plan, rec = _answer(cfg, text="go on")                  # campaign-less: record + event only
    assert rec["status"] == "answered"
    assert [e["type"] for e in E.read_since(cfg, 0)].count("escalation_answered") == 1


def test_answered_and_withdrawn_records_are_refused(tmp_path):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    _esc(cfg)
    _answer(cfg, choose="1")
    with pytest.raises(ValueError, match="answered since"):
        ESC.plan_answer(cfg, "ESC-0001", choose="2")
    # a fresh record, withdrawn
    new = _esc(cfg)["id"]
    ESC.withdraw(cfg, new, "gone", "conductor")
    with pytest.raises(ValueError, match="withdrawn since"):
        ESC.plan_answer(cfg, new, choose="1")


def test_apply_after_a_withdrawal_is_refused_and_writes_nothing(tmp_path):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    _esc(cfg)
    plan = ESC.plan_answer(cfg, "ESC-0001", choose="1")
    ESC.withdraw(cfg, "ESC-0001", "gone", "conductor")
    n_events = len(E.read_since(cfg, 0))
    with pytest.raises(ValueError, match="withdrawn since"):
        ESC.apply_answer(cfg, plan)
    assert ESC.read(cfg, "ESC-0001")["status"] == "withdrawn"
    assert S.read_ledger(cfg, CID, types=["human_decision"]) == []
    assert not [n for n in S.read_ledger(cfg, CID, types=["note"]) if "ESC-0001" in n["text"]
                and "chose" in n["text"]]
    assert len(E.read_since(cfg, 0)) == n_events


def test_apply_takes_item_lock_then_the_escalations_lock_nested(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    _esc(cfg)
    events = []
    real_item, real_lock = S.item_lock, E._locked
    import contextlib

    @contextlib.contextmanager
    def item(c, cid):
        events.append("item+")
        try:
            with real_item(c, cid):
                yield
        finally:
            events.append("item-")

    @contextlib.contextmanager
    def locked(path):
        name = "esc" if str(path).endswith("escalations/.lock") else "other"
        events.append(name + "+")
        try:
            with real_lock(path):
                yield
        finally:
            events.append(name + "-")
    monkeypatch.setattr(S, "item_lock", item)
    monkeypatch.setattr(E, "_locked", locked)
    plan = ESC.plan_answer(cfg, "ESC-0001", choose="1")
    events.clear()
    ESC.apply_answer(cfg, plan)
    # the escalations lock is entered and left while the item lock is held
    # (the ledger/state writes nest further, reentrant item locks inside the escalations lock)
    last_item_out = len(events) - 1 - events[::-1].index("item-")
    assert events.index("item+") < events.index("esc+") < events.index("esc-") < last_item_out
    held = 0
    for ev in events:
        held += (ev == "item+") - (ev == "item-")
        if ev == "esc+":
            assert held >= 1


def test_escalate_again_after_an_answered_text(tmp_path):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    _esc(cfg)
    _answer(cfg, text="not sure, look at the logs")
    assert _state(cfg)["park"]["reason"] == "human_decision"
    plan = ESC.plan_escalate(cfg, CID, question="which logs?", options=["a", "b"])
    rec = ESC.apply_escalate(cfg, plan)
    assert rec["id"] == "ESC-0002" and rec["status"] == "open"
    assert _state(cfg)["park"]["escalated_from"] == "loop_exhausted"
    assert ESC.read(cfg, "ESC-0001")["status"] == "answered"


def test_waiving_a_synthetic_finding_is_accepted_and_stored_as_synthetic(tmp_path):
    cfg = _cfg(tmp_path)
    _reviewed(cfg)
    plan, _ = _answer(cfg, waive=[f"{RV}#ac0"], reason="r")
    assert plan["override"]["waive"][0]["synthetic"] is True
    [w] = _state(cfg)["human_overrides"]["waive"]
    assert w["id"] == f"{RV}#ac0" and w["synthetic"] is True and w["ac"] == AC4


def test_waiving_the_synthetic_fix_finding_is_stored_as_synthetic(tmp_path):
    cfg = _cfg(tmp_path)
    _reviewed(cfg)
    st = _state(cfg)
    v = st["last_review"]["verdicts"][RV]
    v["findings"] = []                      # `fix` with no finding ⇒ a synthetic `#fix`
    v["ac_assessment"] = []
    v["fix_brief"] = "please fix the ordering"
    S.write_state(cfg, CID, st)
    _answer(cfg, waive=[f"{RV}#fix"], reason="r")
    [w] = _state(cfg)["human_overrides"]["waive"]
    assert w["id"] == f"{RV}#fix" and w["synthetic"] is True and w["severity"] == "major"


def test_waiving_a_merged_finding_by_its_merged_id(tmp_path):
    cfg = _cfg(tmp_path)
    _reviewed(cfg)
    st = _state(cfg)
    shared = {"severity": "major", "title": "Wrong order", "files": ["a.py"]}
    base = {"verdict": "fix", "score": 40, "fix_brief": "x", "loop": 0, "run": "003-t",
            "findings": [dict(shared)], "ac_assessment": []}
    st["last_review"]["verdicts"][RV] = {**base, "run": "002-r"}
    st["last_review"]["verdicts"]["test_reviewer"] = base
    S.write_state(cfg, CID, st)
    from wt.agentic import profiles as PR
    tri = Q.triage_reviews(st["last_review"]["verdicts"], PR.resolve_profile(cfg, "balanced"))
    [merged] = [f for f in tri["blocking"] if f["title"] == "Wrong order"]
    assert sorted(merged["personas"]) == ["conformance_reviewer", "test_reviewer"]
    plan, _ = _answer(cfg, waive=[merged["id"]], reason="r")
    assert plan["answer"]["waive"] == [merged["id"]]
    [w] = _state(cfg)["human_overrides"]["waive"]
    assert (w["id"], w["title"], w["files"], w["synthetic"]) == (
        merged["id"], "Wrong order", ["a.py"], False)


def test_a_deferred_escalation_is_refused_until_the_campaign_parks(tmp_path):
    cfg = _cfg(tmp_path)
    _camp(cfg, "IMPLEMENTING")
    lease = L.acquire(cfg, [CID])
    try:
        rec = _esc(cfg, frm="conductor")
    finally:
        lease.release()
    assert rec["park"]["mode"] == "deferred"
    with pytest.raises(ValueError, match="pending: the campaign has not parked yet"):
        ESC.plan_answer(cfg, rec["id"], choose="1")
    # the campaign changes under a plan made before: apply re-checks under the lock
    st = _state(cfg)
    before = dict(st)
    # no plan is possible now, so build one by hand
    plan = {"id": rec["id"], "campaign": CID, "by": "human", "summary": "chose 1 (yes)",
            "answer": {"kind": "choose", "choice": 1, "label": "yes", "text": None, "waive": [],
                       "attest": [], "reason": None, "note": None}, "override": None}
    n_events = len(E.read_since(cfg, 0))
    with pytest.raises(ValueError, match="pending"):
        ESC.apply_answer(cfg, plan)
    assert ESC.read(cfg, rec["id"])["status"] == "open"
    assert _state(cfg) == before
    assert S.read_ledger(cfg, CID, types=["human_decision"]) == []
    assert len(E.read_since(cfg, 0)) == n_events
    # once the park lands, the answer is accepted
    step = Q.plan_step(_state(cfg), {"personas": {}})
    Q._park_row(cfg, _state(cfg), step)
    _, done = _answer(cfg, rec["id"], choose="1")
    assert done["status"] == "answered"


def test_two_overrides_accumulate(tmp_path):
    cfg = _cfg(tmp_path)
    _reviewed(cfg)
    _answer(cfg, waive=[f"{RV}#0"], reason="first")
    _esc(cfg, question="again?")
    _answer(cfg, "ESC-0002", waive=[f"{RV}#1"], attest=[AC4], reason="second")
    ov = _state(cfg)["human_overrides"]
    assert [w["id"] for w in ov["waive"]] == [f"{RV}#0", f"{RV}#1"]
    assert [a["ac"] for a in ov["attest"]] == [AC4]
    assert [w["escalation"] for w in ov["waive"]] == ["ESC-0001", "ESC-0002"]



def test_apply_leaves_the_whole_park_unchanged_for_every_form(tmp_path):
    cfg = _cfg(tmp_path)
    _reviewed(cfg)
    for n, kw in enumerate((dict(choose="1"), dict(text="go on"),
                            dict(waive=[f"{RV}#0"], attest=[f"{RV}#ac0"], reason="r")), 1):
        if n > 1:
            _esc(cfg)                                    # a fresh record re-parks (own detail)
        before = _state(cfg)["park"]
        assert set(before) >= {"reason", "detail", "since", "resume_to"}
        _answer(cfg, f"ESC-{n:04d}", **kw)
        st = _state(cfg)
        assert st["park"] == before and st["state"] == "PARKED"


def test_a_repeated_waive_id_is_stored_once(tmp_path):
    cfg = _cfg(tmp_path)
    _reviewed(cfg)
    plan, _ = _answer(cfg, waive=[f"{RV}#0", f"{RV}#0"], reason="r")
    assert plan["answer"]["waive"] == [f"{RV}#0"]
    assert [w["id"] for w in _state(cfg)["human_overrides"]["waive"]] == [f"{RV}#0"]
    assert plan["summary"] == f"waived {RV}#0; reason: r"


def test_mixed_and_reasonless_forms_raise_the_form_error(tmp_path):
    cfg = _cfg(tmp_path)
    _reviewed(cfg)
    for kw in (dict(choose="1", text="x"), dict(), dict(choose="1", reason="r"),
               dict(waive=[f"{RV}#0"])):
        with pytest.raises(ESC.AnswerFormError):
            ESC.plan_answer(cfg, "ESC-0001", **kw)
    with pytest.raises(ValueError) as e:                  # other refusals are plain errors
        ESC.plan_answer(cfg, "ESC-0001", choose="9")
    assert not isinstance(e.value, ESC.AnswerFormError)


def test_an_unreadable_campaign_state_refuses_every_answer_form(tmp_path):
    cfg = _cfg(tmp_path)
    _reviewed(cfg)
    plan = ESC.plan_answer(cfg, "ESC-0001", choose="1")
    S._state_path(cfg, CID).write_text("{not json")
    for kw in (dict(choose="1"), dict(text="x"), dict(waive=[f"{RV}#0"], reason="r")):
        with pytest.raises(ValueError, match="missing or unreadable"):
            ESC.plan_answer(cfg, "ESC-0001", **kw)
    n_events = len(E.read_since(cfg, 0))
    with pytest.raises(ValueError, match="missing or unreadable"):
        ESC.apply_answer(cfg, plan)
    assert ESC.read(cfg, "ESC-0001")["status"] == "open"
    assert len(E.read_since(cfg, 0)) == n_events


def test_waiving_a_finding_whose_ac_covers_two_unmet_acs_is_refused(tmp_path):
    cfg = _cfg(tmp_path)
    _reviewed(cfg, extra_unmet=("AC 4 output is sorted and stable",))
    st = _state(cfg)
    st["last_review"]["verdicts"][RV]["findings"][0]["ac"] = "output is sorted"
    S.write_state(cfg, CID, st)
    with pytest.raises(ValueError) as e:
        ESC.plan_answer(cfg, "ESC-0001", waive=[f"{RV}#0"], reason="r")
    assert "covers 2 ACs" in str(e.value) and AC4 in str(e.value)
    assert ESC.read(cfg, "ESC-0001")["status"] == "open"


def test_plan_override_stores_resolved_ac_text(tmp_path):
    cfg = _cfg(tmp_path)
    _reviewed(cfg)
    plan = ESC.plan_answer(cfg, "ESC-0001", attest=["output is sorted"], reason="r")
    assert plan["override"]["attest"] == [AC4]             # the full AC, not the typed text
    assert plan["override"]["reach"][AC4] == [AC4]
    st = _state(cfg)
    st["last_review"]["verdicts"][RV]["findings"][0]["ac"] = "output is sorted"
    S.write_state(cfg, CID, st)
    plan = ESC.plan_answer(cfg, "ESC-0001", waive=[f"{RV}#0"], reason="r")
    [w] = plan["override"]["waive"]
    assert w["ac"] == "output is sorted" and w["resolved_ac"] == [AC4]
    assert plan["override"]["implicitly_attested"] == [AC4]


def test_answer_state_write_failure_keeps_record_open(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _reviewed(cfg)
    plan = ESC.plan_answer(cfg, "ESC-0001", attest=[f"{RV}#ac0"], reason="r")
    real = S.write_state
    boom = {"n": 0}

    def flaky(*a, **kw):
        boom["n"] += 1
        if boom["n"] == 1:
            raise OSError("disk full")
        return real(*a, **kw)
    monkeypatch.setattr(S, "write_state", flaky)
    with pytest.raises(OSError, match="disk full"):
        ESC.apply_answer(cfg, plan)
    assert ESC.read(cfg, "ESC-0001")["status"] == "open"
    assert "human_overrides" not in _state(cfg)
    rec = ESC.apply_answer(cfg, plan)                       # the retry goes through
    assert rec["status"] == "answered" and _state(cfg)["human_overrides"]["attest"][0]["ac"] == AC4
    assert [e for e in E.read_since(cfg, 0) if e["type"] == "escalation_answered"]


def test_apply_answer_returns_event_error_without_persisting_it(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    _esc(cfg)
    plan = ESC.plan_answer(cfg, "ESC-0001", choose="1")
    monkeypatch.setattr(E, "append", lambda *a, **kw: (_ for _ in ()).throw(OSError("nope")))
    rec = ESC.apply_answer(cfg, plan)
    assert rec["status"] == "answered" and rec["event_error"] == "nope"
    assert "event_error" not in ESC.read(cfg, "ESC-0001")


def test_waived_partial_ac_finding_clears_full_ac_on_next_triage(tmp_path):
    from wt.agentic import queue as Q
    cfg = _cfg(tmp_path)
    _reviewed(cfg)
    st = _state(cfg)
    st["last_review"]["verdicts"][RV]["findings"][0]["ac"] = "output is sorted"
    S.write_state(cfg, CID, st)
    _answer(cfg, waive=[f"{RV}#0"], reason="r")
    st = _state(cfg)
    hw, at = Q.human_overrides_of(st)
    out = Q.triage_reviews(st["last_review"]["verdicts"], {"name": "balanced"}, (), hw, at)
    assert out["attested"] == [AC4] and f"{RV}#ac0" not in out["synthetic"]


# ---- SPEC-0286: overrides are idempotent, withdrawable and exact ------------------------------

def _half_answered(cfg, monkeypatch):
    """ESC-0001 answered with one waive and one attest whose record write fails once: the
    overrides are stored and the record stays open."""
    plan = ESC.plan_answer(cfg, "ESC-0001", waive=[f"{RV}#1"], attest=[f"{RV}#ac0"], reason="r")
    real = ESC._write
    boom = {"n": 0}

    def flaky(cfg_, rec):
        if rec.get("status") == "answered" and not boom["n"]:
            boom["n"] += 1
            raise OSError("disk full")
        return real(cfg_, rec)
    monkeypatch.setattr(ESC, "_write", flaky)
    with pytest.raises(OSError, match="disk full"):
        ESC.apply_answer(cfg, plan)
    assert ESC.read(cfg, "ESC-0001")["status"] == "open"
    return plan


def _overrides(cfg, esc_id):
    h = _state(cfg).get("human_overrides") or {}
    return ([w for w in h.get("waive") or [] if w.get("escalation") == esc_id],
            [a for a in h.get("attest") or [] if a.get("escalation") == esc_id])


def test_answer_retry_after_record_write_failure_does_not_duplicate_overrides(tmp_path,
                                                                                monkeypatch):
    cfg = _cfg(tmp_path)
    _reviewed(cfg)
    plan = _half_answered(cfg, monkeypatch)
    waive, attest = _overrides(cfg, "ESC-0001")
    assert len(waive) == len(attest) == 1                 # stored before the record closed
    rec = ESC.apply_answer(cfg, plan)                      # the retry
    assert rec["status"] == "answered"
    waive, attest = _overrides(cfg, "ESC-0001")
    assert len(waive) == len(attest) == 1
    assert waive[0]["id"] == f"{RV}#1" and attest[0]["ac"] == AC4


def test_withdraw_strips_that_escalations_overrides(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _reviewed(cfg)
    st = _state(cfg)                                       # an earlier, answered ESC-0000
    st["human_overrides"] = {
        "waive": [{"id": "x#0", "title": "t", "files": [], "severity": "minor", "ac": None,
                   "resolved_ac": [], "synthetic": False, "reason": "old",
                   "escalation": "ESC-0000", "at": S.now_iso()}],
        "attest": [{"ac": "AC 1 old", "reason": "old", "escalation": "ESC-0000",
                    "at": S.now_iso()}]}
    S.write_state(cfg, CID, st)
    _half_answered(cfg, monkeypatch)
    assert _overrides(cfg, "ESC-0001") != ([], [])
    ESC.apply_withdraw(cfg, ESC.plan_withdraw(cfg, "ESC-0001", reason="gave up", by="human"))
    assert ESC.read(cfg, "ESC-0001")["status"] == "withdrawn"
    assert _overrides(cfg, "ESC-0001") == ([], [])
    old_w, old_a = _overrides(cfg, "ESC-0000")
    assert [w["id"] for w in old_w] == ["x#0"] and [a["ac"] for a in old_a] == ["AC 1 old"]
    h = _state(cfg)["human_overrides"]
    assert len(h["waive"]) == len(h["attest"]) == 1
    assert _state(cfg)["state"] == "PARKED"                # never resumes the campaign
    note = S.read_ledger(cfg, CID, types=["note"])[-1]["text"]
    assert note == "escalation ESC-0001 withdrawn: gave up; 2 override(s) removed"


def test_withdraw_without_overrides_keeps_the_plain_note(tmp_path):
    cfg = _cfg(tmp_path)
    _reviewed(cfg)
    ESC.apply_withdraw(cfg, ESC.plan_withdraw(cfg, "ESC-0001", reason="gave up", by="human"))
    note = S.read_ledger(cfg, CID, types=["note"])[-1]["text"]
    assert note == "escalation ESC-0001 withdrawn: gave up"


def test_waiver_resolved_ac_keeps_only_the_exact_ac(tmp_path):
    cfg = _cfg(tmp_path)
    short, long_ = "pytest passes", "pytest passes with the new regression tests"
    _reviewed(cfg)
    st = _state(cfg)
    v = st["last_review"]["verdicts"][RV]
    v["ac_assessment"] = [{"ac": short, "met": False}, {"ac": long_, "met": False}]
    v["findings"][0]["ac"] = short
    S.write_state(cfg, CID, st)
    plan, _ = _answer(cfg, waive=[f"{RV}#0"], reason="r")
    assert plan["override"]["waive"][0]["resolved_ac"] == [short]
    st = _state(cfg)
    hw, at = Q.human_overrides_of(st)
    out = Q.triage_reviews(st["last_review"]["verdicts"], {"name": "balanced"}, (), hw, at)
    assert out["attested"] == [short] and out["outcome"] == "fix"   # the longer AC stays unmet
