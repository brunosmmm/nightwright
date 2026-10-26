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
