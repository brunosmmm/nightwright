"""SPEC-0302: parks the queue decides itself (`base_moving`, phantom `cost_cap`): the park
carries `auto`, `_tick` writes the `wait` record outside any item lock, step 0a resumes."""
import pytest

from test_agentic_e2e import ENV, _git, _write
from test_agentic_queue_merge import branch_wt, queue_merge
from test_agentic_queue import PROFILE, _rr, opened  # noqa: F401  (fixture)
from nightwright.agentic import close as CL
from nightwright.agentic import decisions as D
from nightwright.agentic import escalations as ESC
from nightwright.agentic import events as E
from nightwright.agentic import lease as L
from nightwright.agentic import queue as Q
from nightwright.agentic import quiet as QT
from nightwright.agentic import store as S

BASE, CAP = "parked.base_moving", "parked.cost_cap"
LONG_AGO = "2020-01-01T00:00:00+00:00"


class _Done:
    returncode, stdout, stderr = 0, "", ""


def _park(cfg, cid, reason="base_moving", resume_to="CLOSING", budget=None):
    Q._park(cfg, S.read_state(cfg, cid), reason, "d", resume_to, budget=budget)
    return S.read_state(cfg, cid)


def _due(cfg, cid):
    st = S.read_state(cfg, cid)
    st["park"]["auto"]["resume_at"] = LONG_AGO
    S.write_state(cfg, cid, st)


def _step0a(cfg, *, busy=0, slots=2):
    report = Q.TickReport()
    resumed = Q._auto_resume_step(cfg, set(), busy_count=lambda: busy, slots=slots, report=report)
    return report, resumed


def _auto_cfg(cfg, **over):
    cfg.setdefault("agentic", {}).setdefault("conductor", {})["auto"] = over


def test_base_moving_park_auto_decided_and_resumed(opened):
    cfg, cid, ctx = opened
    st = _park(cfg, cid)
    auto = st["park"]["auto"]
    assert auto["rule"] == BASE and auto["n"] == 0 and auto["raise_cap"] is None
    assert S.read_ledger(cfg, cid, types=["park"])[-1]["auto"]["rule"] == BASE
    assert D.read(cfg, campaign=cid) == []                  # `_park` writes no record
    Q._record_auto_waits(cfg)
    recs = D.read(cfg, campaign=cid)
    assert [(r["by"], r["kind"], r["rule"]) for r in recs] == [("queue", "wait", BASE)]
    view = Q.park_view(cfg, S.read_state(cfg, cid), __import__("datetime").datetime.now().astimezone(),
                       E.settings(cfg))
    assert view["decided"]
    Q._record_auto_waits(cfg)                               # idempotent
    assert len(D.read(cfg, campaign=cid)) == 1
    report, _ = _step0a(cfg)                                # not due yet
    assert S.read_state(cfg, cid)["state"] == "PARKED" and not report.steps
    _due(cfg, cid)
    report, resumed = _step0a(cfg)
    now = S.read_state(cfg, cid)
    assert now["state"] == "CLOSING" and now["park"] is None
    assert now["counters"]["auto_resumes"] == {BASE: 1}
    assert [r["action"] for r in report.steps] == ["auto_resume"] and resumed[0]["id"] == cid
    recs = D.read(cfg, campaign=cid)
    assert [(r["by"], r["kind"]) for r in recs] == [("queue", "wait"), ("queue", "resume")]
    assert recs[1]["action"] == "resume to CLOSING"
    ev = S.read_ledger(cfg, cid, types=["resume"])[-1]
    assert "auto-resume 1/3" in ev["note"] and ev["by"] == "queue"
    assert D.park_coverage(cfg)["missing"] == []


def test_base_moving_gives_up_after_max(opened):
    cfg, cid, ctx = opened
    for n in range(3):
        st = _park(cfg, cid)
        assert st["park"]["auto"]["n"] == n
        _due(cfg, cid)
        Q._record_auto_waits(cfg)
        _step0a(cfg)
        assert S.read_state(cfg, cid)["state"] == "CLOSING"
    st = _park(cfg, cid)
    assert "auto" not in st["park"]
    now = __import__("datetime").datetime.now().astimezone()
    assert Q.park_view(cfg, st, now, E.settings(cfg))["decided"] is False
    Q._record_auto_waits(cfg)
    assert D.read(cfg, campaign=cid)[-1]["kind"] == "resume"     # nothing new for this park


def test_transient_phantom_cost_cap_auto_resume(opened):
    cfg, cid, ctx = opened
    st = S.read_state(cfg, cid)
    st["cost"].update(total_usd=8.0, cap_usd=10.0)
    S.write_state(cfg, cid, st)
    for i in (1, 2):                                # two transient runs charged their budget
        S.append_event(cfg, cid, "run.end", {
            "persona": "implementer", "run_id": f"00{i}-implementer", "exit_code": 1,
            "is_error": True, "timed_out": False, "interrupted": False, "duration_s": 3.0,
            "cost_usd": None, "charged_usd": 4.0, "cost_estimated": True, "usage": {},
            "session_id": None, "verdict_ok": False, "verdict": None,
            "result_tail": "API Error: 529 overloaded"})
    step = Q.plan_step(S.read_state(cfg, cid), PROFILE)
    assert step.kind == "park" and step.reason == "cost_cap" and step.budget == 4.0
    Q._park_row(cfg, S.read_state(cfg, cid), step)
    st = S.read_state(cfg, cid)
    assert st["park"]["auto"]["rule"] == CAP and st["park"]["auto"]["raise_cap"] == 18.0
    _due(cfg, cid)
    _step0a(cfg)
    now = S.read_state(cfg, cid)
    assert now["state"] == "IMPLEMENTING" and now["cost"]["cap_usd"] == 18.0
    assert S.read_ledger(cfg, cid, types=["resume"])[-1]["cap_usd"] == 18.0
    assert "raise cap to $18.00" in D.read(cfg, campaign=cid)[-1]["action"]


def test_real_spend_cost_cap_has_no_auto(opened):
    cfg, cid, ctx = opened
    st = S.read_state(cfg, cid)
    st["cost"].update(total_usd=8.0, cap_usd=10.0)
    S.write_state(cfg, cid, st)
    step = Q.plan_step(S.read_state(cfg, cid), PROFILE)
    Q._park_row(cfg, S.read_state(cfg, cid), step)
    assert "auto" not in S.read_state(cfg, cid)["park"]


def test_auto_resume_failure_hands_back(opened, monkeypatch):
    cfg, cid, ctx = opened
    _park(cfg, cid)
    Q._record_auto_waits(cfg)
    _due(cfg, cid)

    def boom(*a, **k):
        raise ValueError("state moved")
    monkeypatch.setattr(Q, "apply_resume", boom)
    _step0a(cfg)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and "auto" not in st["park"]
    last = D.read(cfg, campaign=cid)[-1]
    assert (last["by"], last["kind"], last["rule"]) == ("queue", "other", BASE)
    assert "auto-resume failed: state moved" in last["rationale"]
    ev = S.read_ledger(cfg, cid, types=["auto_resume_failed"])
    assert ev[0]["rule"] == BASE and ev[0]["error"] == "state moved"
    _step0a(cfg)                                            # nothing left to do
    assert len(S.read_ledger(cfg, cid, types=["auto_resume_failed"])) == 1


def _pending_auto(cfg, cid):
    _park(cfg, cid)
    Q._record_auto_waits(cfg)
    _due(cfg, cid)


def test_auto_resume_gives_up_when_the_park_reason_changed(opened):
    cfg, cid, ctx = opened
    _pending_auto(cfg, cid)
    st = S.read_state(cfg, cid)
    st["park"]["reason"] = "loop_exhausted"                 # `auto` survives a rewritten reason
    S.write_state(cfg, cid, st)
    _step0a(cfg)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and "auto" not in st["park"]
    assert "park reason is now loop_exhausted" in \
        S.read_ledger(cfg, cid, types=["auto_resume_failed"])[0]["error"]
    assert [r["kind"] for r in D.read(cfg, campaign=cid)] == ["wait", "other"]   # no `resume`


def test_auto_resume_gives_up_when_plan_resume_raises(opened, monkeypatch):
    cfg, cid, ctx = opened
    _pending_auto(cfg, cid)

    def boom(*a, **k):
        raise ValueError("cannot plan")
    monkeypatch.setattr(Q, "plan_resume", boom)
    _step0a(cfg)
    assert S.read_state(cfg, cid)["state"] == "PARKED"
    assert S.read_ledger(cfg, cid, types=["auto_resume_failed"])[0]["error"] == "cannot plan"
    assert [r["kind"] for r in D.read(cfg, campaign=cid)] == ["wait", "other"]


def test_apply_resume_refuses_when_auto_changed_since_the_plan(opened):
    cfg, cid, ctx = opened
    _pending_auto(cfg, cid)
    plan = Q.plan_resume(cfg, cid, by="queue", auto_rule=BASE)
    st = S.read_state(cfg, cid)
    st["park"].pop("auto")                                  # someone took `auto` away
    S.write_state(cfg, cid, st)
    with pytest.raises(ValueError, match="park changed"):
        Q.apply_resume(cfg, plan)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and not (st.get("counters") or {}).get("auto_resumes")
    st["park"]["auto"] = {"rule": CAP, "resume_at": LONG_AGO}     # or replaced it by another rule
    S.write_state(cfg, cid, st)
    with pytest.raises(ValueError, match="park changed"):
        Q.apply_resume(cfg, plan)
    # a human resume has no `auto_rule` and is not gated by `auto`
    assert Q.apply_resume(cfg, Q.plan_resume(cfg, cid))["state"] != "PARKED"


def test_auto_resume_leaves_no_brief_note_and_orders_record_first(opened):
    cfg, cid, ctx = opened
    _pending_auto(cfg, cid)
    _step0a(cfg)
    st = S.read_state(cfg, cid)
    assert st["state"] == "CLOSING"
    assert not (st.get("pending_brief") or {}).get("human_notes")     # no pseudo-human note
    assert st["counters"]["auto_resumes"] == {BASE: 1}
    ev = S.read_ledger(cfg, cid, types=["resume"])[-1]
    assert ev["note"] == "queue: parked.base_moving auto-resume 1/3"
    note = [e for e in S.read_ledger(cfg, cid, types=["note"])
            if "scheduled automatic resume" in str(e)][-1]
    assert note["seq"] < ev["seq"]                          # the decision record came first


def test_lost_race_after_the_resume_record_is_recorded(opened, monkeypatch):
    cfg, cid, ctx = opened
    _pending_auto(cfg, cid)
    real = Q.apply_resume

    def raced(cfg_, plan, **kw):
        real(cfg_, Q.plan_resume(cfg_, cid))                # a conductor resumed it meanwhile
        return real(cfg_, plan, **kw)
    monkeypatch.setattr(Q, "apply_resume", raced)
    _step0a(cfg)
    recs = D.read(cfg, campaign=cid)
    assert [r["kind"] for r in recs] == ["wait", "resume", "other"]
    assert "not applied" in recs[-1]["rationale"]


def test_tick_plans_the_campaign_it_auto_resumed(opened, monkeypatch):
    """`_tick` adds the step-0a campaign to `busy`, so the same tick plans it."""
    cfg, cid, ctx = opened
    _pending_auto(cfg, cid)
    monkeypatch.setattr(L, "is_held", lambda cfg: True)
    planned, real = [], Q.plan_step

    def spy(st, *a, **k):
        planned.append((st["id"], st["state"]))
        return real(st, *a, **k)
    monkeypatch.setattr(Q, "plan_step", spy)
    report = Q.tick(cfg, admit=False)
    assert any(r["action"] == "auto_resume" and r["campaign"] == cid for r in report.steps)
    assert (cid, "CLOSING") in planned


def test_auto_resume_skips_a_campaign_in_flight(opened):
    cfg, cid, ctx = opened
    _pending_auto(cfg, cid)
    report = Q.TickReport()
    assert Q._auto_resume_step(cfg, {cid}, busy_count=lambda: 0, slots=2, report=report) == []
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["auto"]["rule"] == BASE


def test_missing_auto_rule_falls_back_to_a_table_rule_when_disabled(opened):
    cfg, cid, ctx = opened
    _park(cfg, cid)
    st = S.read_state(cfg, cid)
    st["park"]["auto"].pop("rule")
    S.write_state(cfg, cid, st)
    _auto_cfg(cfg, enabled=False)
    _step0a(cfg)
    assert D.read(cfg, campaign=cid)[-1]["rule"] == "auto_resume_failed"
    assert S.read_ledger(cfg, cid, types=["auto_resume_failed"])[0]["rule"] == "auto_resume_failed"


def test_escalation_drops_auto(opened, monkeypatch):
    cfg, cid, ctx = opened
    monkeypatch.setattr(ESC, "_run_notify", lambda argv: _Done())
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/notify-send")
    _park(cfg, cid)
    Q._record_auto_waits(cfg)
    ESC.apply_escalate(cfg, ESC.plan_escalate(cfg, cid, question="ship?", options=["y", "n"],
                                              recommend=1))
    st = S.read_state(cfg, cid)
    assert st["park"]["reason"] == "human_decision" and "auto" not in st["park"]
    _step0a(cfg)
    assert S.read_state(cfg, cid)["state"] == "PARKED"
    assert ESC.open_for(cfg, cid) is not None
    assert not S.read_ledger(cfg, cid, types=["auto_resume_failed"])


def test_open_escalation_on_auto_park_gives_up(opened, monkeypatch):
    cfg, cid, ctx = opened
    _park(cfg, cid)
    Q._record_auto_waits(cfg)
    _due(cfg, cid)
    monkeypatch.setattr(ESC, "open_for", lambda c, i: {"id": "ESC-1"})
    _step0a(cfg)
    assert S.read_state(cfg, cid)["state"] == "PARKED"
    assert "an escalation is open" in S.read_ledger(cfg, cid, types=["auto_resume_failed"])[0]["error"]


def test_wait_record_outside_item_lock(opened, monkeypatch):
    cfg, cid, ctx = opened
    depth = {"n": 0}
    real_lock, real_record = S.item_lock, D.record

    class _Tracked:
        def __init__(self, *a, **k):
            self.inner = real_lock(*a, **k)

        def __enter__(self):
            depth["n"] += 1
            return self.inner.__enter__()

        def __exit__(self, *exc):
            depth["n"] -= 1
            return self.inner.__exit__(*exc)

    held, fail = [], {"left": 1}

    def record(*a, **k):
        held.append(depth["n"])
        if fail["left"]:
            fail["left"] -= 1
            raise OSError("disk full")
        return real_record(*a, **k)
    monkeypatch.setattr(S, "item_lock", _Tracked)
    monkeypatch.setattr(D, "record", record)
    _park(cfg, cid)
    assert held == []                                       # the park itself never records
    _due(cfg, cid)
    _step0a(cfg)                                            # the record fails: not resumed
    assert S.read_state(cfg, cid)["state"] == "PARKED" and held == [0]
    _step0a(cfg)                                            # repaired, then resumed
    assert S.read_state(cfg, cid)["state"] == "CLOSING"
    assert held and set(held) == {0}


def test_tick_writes_wait_record(opened, monkeypatch):
    cfg, cid, ctx = opened
    monkeypatch.setattr(L, "is_held", lambda cfg: True)
    _park(cfg, cid)
    Q.tick(cfg, admit=False)
    assert [r["kind"] for r in D.read(cfg, campaign=cid)] == ["wait"]
    assert S.read_state(cfg, cid)["state"] == "PARKED"


def test_resume_slot_accounting(opened):
    cfg, cid, ctx = opened
    _park(cfg, cid)
    Q._record_auto_waits(cfg)
    plan = Q.plan_resume(cfg, cid, by="queue", auto_rule=BASE)
    assert plan["auto_rule"] == BASE
    _due(cfg, cid)
    report, resumed = _step0a(cfg, busy=2, slots=2)
    assert S.read_state(cfg, cid)["state"] == "WAITING" and resumed[0]["state"] == "WAITING"
    assert report.steps[0]["to"] == "WAITING"


def test_resume_slot_accounting_within_one_step(opened):
    """Two auto resumes due in one tick share the free slots: the second sees the first."""
    cfg, cid, ctx = opened
    other = "C2-" + cid
    S.create_campaign(cfg, dict(S.read_state(cfg, cid), id=other))
    for c in (cid, other):
        _park(cfg, c)
        _due(cfg, c)
    Q._record_auto_waits(cfg)
    report, resumed = _step0a(cfg, busy=1, slots=2)         # one other campaign is active
    states = sorted(S.read_state(cfg, c)["state"] for c in (cid, other))
    assert states == ["CLOSING", "WAITING"]
    assert sorted(r["state"] for r in resumed) == states


def test_decide_failure_never_blocks_the_park(opened, monkeypatch):
    cfg, cid, ctx = opened

    def boom(*a, **k):
        raise RuntimeError("bad config")
    monkeypatch.setattr(Q.AP, "decide", boom)
    st = _park(cfg, cid)
    assert st["state"] == "PARKED" and "auto" not in st["park"]
    assert any("autopark.decide failed" in n["text"] for n in S.read_ledger(cfg, cid, types=["note"]))


def test_park_auto_ok_false_is_not_auto(opened):
    cfg, cid, ctx = opened
    Q._park(cfg, S.read_state(cfg, cid), "base_moving", "d", "CLOSING", auto_ok=False)
    assert "auto" not in S.read_state(cfg, cid)["park"]


@pytest.mark.parametrize("by", ["human", "conductor"])
def test_only_tick_parks_are_auto_decided(opened, by):
    cfg, cid, ctx = opened
    Q._park(cfg, S.read_state(cfg, cid), "base_moving", "d", "CLOSING", by=by)
    assert "auto" not in S.read_state(cfg, cid)["park"]
    assert S.read_ledger(cfg, cid, types=["park"])[-1].get("auto") is None
    Q._record_auto_waits(cfg)
    assert D.read(cfg, campaign=cid) == []


def test_missing_auto_rule_is_handed_back_when_enabled(opened):
    cfg, cid, ctx = opened
    _park(cfg, cid)
    st = S.read_state(cfg, cid)
    st["park"]["auto"].pop("rule")
    S.write_state(cfg, cid, st)
    Q._record_auto_waits(cfg)                                    # must not raise
    _due(cfg, cid)
    report, resumed = _step0a(cfg)                               # must not raise
    now = S.read_state(cfg, cid)
    assert now["state"] == "PARKED" and "auto" not in now["park"] and not resumed
    assert D.read(cfg, campaign=cid)[-1]["rule"] == "auto_resume_failed"
    assert len(S.read_ledger(cfg, cid, types=["auto_resume_failed"])) == 1


def test_auto_resume_never_lowers_a_cap_raised_meanwhile(opened):
    cfg, cid, ctx = opened
    st = S.read_state(cfg, cid)
    st["cost"].update(total_usd=8.0, cap_usd=10.0)
    S.write_state(cfg, cid, st)
    st = _park(cfg, cid, reason="cost_cap", resume_to="IMPLEMENTING")
    st["park"]["auto"] = {"rule": CAP, "resume_at": LONG_AGO, "raise_cap": 18.0, "n": 0,
                          "rationale": "phantom"}
    S.write_state(cfg, cid, st)
    Q._record_auto_waits(cfg)
    st = S.read_state(cfg, cid)
    st["cost"]["cap_usd"] = 25.0                                 # raised by some other path
    S.write_state(cfg, cid, st)
    _step0a(cfg)
    now = S.read_state(cfg, cid)
    assert now["state"] == "IMPLEMENTING" and now["cost"]["cap_usd"] == 25.0


def test_disable_hands_back_pending_auto(opened):
    cfg, cid, ctx = opened
    _park(cfg, cid)
    Q._record_auto_waits(cfg)
    _auto_cfg(cfg, enabled=False)
    _due(cfg, cid)
    _step0a(cfg)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and "auto" not in st["park"]
    assert "auto-resume disabled" in D.read(cfg, campaign=cid)[-1]["rationale"]
    assert D.read(cfg, campaign=cid)[-1]["kind"] == "other"
    assert len(S.read_ledger(cfg, cid, types=["auto_resume_failed"])) == 1


def test_auto_disabled_is_today(opened):
    cfg, cid, ctx = opened
    _auto_cfg(cfg, enabled=False)
    st = _park(cfg, cid)
    assert "auto" not in st["park"]
    Q._record_auto_waits(cfg)
    assert D.read(cfg, campaign=cid) == []
    ev = E._project_ledger_event(S.read_ledger(cfg, cid, types=["park"])[-1], st,
                                 E.settings(cfg), False)
    assert QT.classify(cfg, {"type": ev[0], "data": ev[1]}, None) == "wake"


def test_config_invalid_recovery_records_decision(opened, monkeypatch):
    cfg, cid, ctx = opened
    monkeypatch.setattr(L, "is_held", lambda cfg: True)
    _park(cfg, cid, reason="config_invalid", resume_to="IMPLEMENTING")
    Q.tick(cfg, admit=False)
    recs = [r for r in D.read(cfg, campaign=cid) if r["rule"] == "parked.config_invalid"]
    assert [(r["by"], r["kind"]) for r in recs] == [("queue", "resume")]
    assert S.read_state(cfg, cid)["state"] != "PARKED"


def test_auto_park_projects_auto_and_is_held(opened):
    cfg, cid, ctx = opened
    st = _park(cfg, cid)
    ev = E._project_ledger_event(S.read_ledger(cfg, cid, types=["park"])[-1], st,
                                 E.settings(cfg), False)
    assert ev[0] == "parked" and ev[1]["auto"]["rule"] == BASE
    assert QT.classify(cfg, {"type": ev[0], "data": ev[1]}, None) == "hold"


def _closing_world(tmp_path, monkeypatch):
    """A merge campaign in CLOSING in a real temp repo: (cfg, repo, cid, run_cli)."""
    from test_agentic_e2e import make_cfg, make_repo, run_cli, script
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("NW_AGENTIC_RUNNER", raising=False)
    monkeypatch.delenv("NW_AGENTIC_FAKE_SCRIPT", raising=False)
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    branch_wt(tmp_path, repo)
    cid = queue_merge(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "tick"])            # admit + open
    assert r.exit_code == 0, r.output
    assert S.read_state(cfg, cid)["state"] == "CLOSING"
    return cfg, repo, cid, run_cli


def test_close_park_inside_tick_is_decided_and_recorded(tmp_path, monkeypatch):
    """The real close → finish_close → `_park` path: the park happens during the tick, so only
    the end-of-tick `_record_auto_waits` can have written the `wait` record by then."""
    cfg, repo, cid, run_cli = _closing_world(tmp_path, monkeypatch)
    moves = {"n": 0}

    def move_base():
        moves["n"] += 1
        _write(repo, f"m{moves['n']}.txt", "x\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"move {moves['n']}")

    real = CL.finish_close
    monkeypatch.setattr(CL, "finish_close",
                        lambda *a, **k: real(*a, **k, _hooks={"before_merge": move_base}))
    r = run_cli(cfg, monkeypatch, ["queue", "tick"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "base_moving" and moves["n"] == 2
    assert st["park"]["auto"]["rule"] == BASE
    recs = D.read(cfg, campaign=cid)
    assert [(r["by"], r["kind"], r["rule"]) for r in recs] == [("queue", "wait", BASE)]


def test_post_merge_base_moving_from_finish_close_is_never_auto(tmp_path, monkeypatch):
    """The 'merged != gated HEAD' park comes out of the real close with `no_auto`, and the tick
    (`_apply`) keeps it away from the queue: the merge already happened, a blind retry is not a
    mechanical decision."""
    cfg, repo, cid, run_cli = _closing_world(tmp_path, monkeypatch)
    monkeypatch.setattr(CL.git, "ff_merge", lambda root, branch: "f" * 40)
    r = run_cli(cfg, monkeypatch, ["queue", "tick"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "base_moving"
    assert "!= gated HEAD" in st["park"]["detail"]
    assert "auto" not in st["park"]
    assert S.read_ledger(cfg, cid, types=["park"])[-1].get("auto") is None
    assert D.read(cfg, campaign=cid) == []                      # no `wait` record either
    assert not S.read_ledger(cfg, cid, types=["auto_resume_failed"])
    r = run_cli(cfg, monkeypatch, ["queue", "tick"])            # and no tick resumes it
    assert r.exit_code == 0, r.output
    assert S.read_state(cfg, cid)["state"] == "PARKED"


# ---- SPEC-0422: the cost-cap policy next to the phantom-charge rule ------------------------------

def _phantom(cfg, cid, total=8.0, cap=10.0):
    st = S.read_state(cfg, cid)
    st["cost"].update(total_usd=total, cap_usd=cap)
    S.write_state(cfg, cid, st)
    for i in (1, 2):
        S.append_event(cfg, cid, "run.end", {
            "persona": "implementer", "run_id": f"00{i}-implementer", "exit_code": 1,
            "is_error": True, "timed_out": False, "interrupted": False, "duration_s": 3.0,
            "cost_usd": None, "charged_usd": 4.0, "cost_estimated": True, "usage": {},
            "session_id": None, "verdict_ok": False, "verdict": None,
            "result_tail": "API Error: 529 overloaded"})


def test_cost_policy_leaves_the_phantom_charge_rule_first(opened, cost_policy_on):
    cfg, cid, ctx = opened
    _phantom(cfg, cid)
    st = S.read_state(cfg, cid)
    step = Q.plan_step(st, PROFILE)
    assert Q._cost_phantom_first(cfg, st, step.budget)
    out = Q._cost_policy_step(ctx, st, ctx.profile(st), None, step, Q.TickReport())
    assert out is step                                   # today's park; autopark decides it
    assert S.read_ledger(cfg, cid, types=["cap_raised"]) == []


def test_parked_cost_cap_is_reevaluated_and_resumed_raised(opened, cost_policy_on):
    cfg, cid, ctx = opened
    st = S.read_state(cfg, cid)
    base = float(ctx.profile(st)["campaign_cap_usd"])
    st["cost"].update(total_usd=base - 1.0, cap_usd=base)
    S.write_state(cfg, cid, st)
    Q._park(cfg, S.read_state(cfg, cid), "cost_cap", "d", "IMPLEMENTING", auto_ok=False)
    report, resumed = _step0a(cfg)
    now = S.read_state(cfg, cid)
    assert now["state"] == "IMPLEMENTING" and now["cost"]["cap_usd"] > base
    assert [r["reason"] for r in report.steps] == ["cost_cap_policy"] and resumed
    rec = [r for r in D.read(cfg, campaign=cid) if r["rule"] == "cost_cap_policy"]
    assert [(r["by"], r["kind"]) for r in rec] == [("queue", "resume")]
    report, _ = _step0a(cfg)                             # idempotent: nothing parked any more
    assert report.steps == [] and len(S.read_ledger(cfg, cid, types=["cap_raised"])) == 1


def test_parked_cost_cap_untouched_with_the_policy_off(opened):
    cfg, cid, ctx = opened
    st = S.read_state(cfg, cid)
    st["cost"].update(total_usd=9.0, cap_usd=10.0)
    S.write_state(cfg, cid, st)
    Q._park(cfg, S.read_state(cfg, cid), "cost_cap", "d", "IMPLEMENTING", auto_ok=False)
    report, resumed = _step0a(cfg)
    assert S.read_state(cfg, cid)["state"] == "PARKED" and not report.steps and not resumed
