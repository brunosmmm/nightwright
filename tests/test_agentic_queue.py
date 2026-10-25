"""SPEC-0176: the pure state machine (`plan_step`, `decide_triage`) and `_apply` over synthetic
step results, admission by slots, lease refusal. Repos and runners come from the e2e harness
(`test_agentic_e2e`), personas never run for real."""
import json

import pytest
from click.testing import CliRunner

from test_agentic_e2e import (SPEC_ACCEPTED, added, impl_done, make_cfg, make_repo, run_cli,
                              script, write_review)
from wt.agentic import lease as L
from wt.agentic import queue as Q
from wt.agentic import store as S
from wt.agentic.runner import RunResult
from wt.cli import cli

PROFILE = {"slots": 2, "loop_max": 3, "campaign_cap_usd": 10.0, "run_timeout_s": 60,
           "personas": {"implementer": {"max_budget_usd": 4.0},
                        "gate_triager": {"max_budget_usd": 0.5},
                        "conformance_reviewer": {"max_budget_usd": 1.5}}}


def _state(state="IMPLEMENTING", **over):
    st = {"id": "SPEC-0001-20261025-abcd", "spec_id": "SPEC-0001", "state": state,
          "counters": {"loops": 0, "runs": 0, "gate_retries": 0, "triage_retries": 0},
          "cost": {"total_usd": 0.0, "cap_usd": 10.0, "by_persona": {}}, "last_gate": None}
    st.update(over)
    return st


# ---- plan_step ---------------------------------------------------------------------------

@pytest.mark.parametrize("state,kind,persona", [
    ("QUEUED", "none", None), ("OPENING", "open", None), ("IMPLEMENTING", "implement", "implementer"),
    ("GATING", "gate", None), ("TRIAGING", "triage", "gate_triager"), ("CLOSING", "close", None),
    ("REVIEWING", "review", "conformance_reviewer"),
    ("PARKED", "none", None), ("MERGED", "none", None), ("HANDED_OFF", "none", None),
    ("ABANDONED", "none", None),
])
def test_plan_step_table(state, kind, persona):
    step = Q.plan_step(_state(state), PROFILE)
    assert (step.kind, step.persona) == (kind, persona)


@pytest.mark.parametrize("state,persona,spent,parks", [
    ("IMPLEMENTING", "implementer", 6.0, False),   # 6 + 4 = 10, not over
    ("IMPLEMENTING", "implementer", 6.01, True),   # pessimistic: could cross the cap
    ("TRIAGING", "gate_triager", 9.5, False),
    ("TRIAGING", "gate_triager", 9.51, True),
    ("REVIEWING", "conformance_reviewer", 8.5, False),   # 8.5 + 1.5 = 10, not over
    ("REVIEWING", "conformance_reviewer", 8.51, True),
])
def test_plan_step_cost_cap_is_pessimistic(state, persona, spent, parks):
    st = _state(state, cost={"total_usd": spent, "cap_usd": 10.0, "by_persona": {}})
    step = Q.plan_step(st, PROFILE)
    if parks:
        assert step.kind == "park" and step.reason == "cost_cap" and step.resume_to == state
        assert persona in step.detail
    else:
        assert step.kind in ("implement", "triage", "review")


def test_plan_step_no_cap_never_parks():
    st = _state(cost={"total_usd": 999.0, "cap_usd": None, "by_persona": {}})
    assert Q.plan_step(st, PROFILE).kind == "implement"


@pytest.mark.parametrize("state,resume_to", [
    ("IMPLEMENTING", "IMPLEMENTING"), ("GATING", "GATING"), ("CLOSING", "CLOSING"),
    ("TRIAGING", "GATING"), ("OPENING", "QUEUED"), ("REVIEWING", "REVIEWING"),
])
def test_plan_step_human_park_request_wins(state, resume_to):
    st = _state(state, park_requested={"reason": "human", "detail": "hold"})
    step = Q.plan_step(st, PROFILE)
    assert step.kind == "park" and step.reason == "human" and step.detail == "hold"
    assert step.resume_to == resume_to


# ---- decide_triage ------------------------------------------------------------------------

def _verdict(disposition, **kw):
    v = {"disposition": disposition, "fix_brief": "do it", "must_fix": [], "waived": []}
    v.update(kw)
    return v


def test_triage_fix_increments_until_loop_max():
    st = _state("TRIAGING")
    for loops in range(3):
        st["counters"]["loops"] = loops
        step = Q.decide_triage(st, PROFILE, _verdict("fix"), True)
        assert step.kind == "implement" and step.reason == "triage_fix"
    st["counters"]["loops"] = 3
    step = Q.decide_triage(st, PROFILE, _verdict("fix"), True)
    assert step.kind == "park" and step.reason == "loop_exhausted"
    assert step.resume_to == "IMPLEMENTING" and "3/3" in step.detail


def test_triage_retry_gate_once_per_loop_then_fix():
    st = _state("TRIAGING")
    assert Q.decide_triage(st, PROFILE, _verdict("retry_gate"), True).kind == "gate"
    st["counters"]["gate_retries"] = 1
    step = Q.decide_triage(st, PROFILE, _verdict("retry_gate"), True)
    assert step.kind == "implement" and step.reason == "triage_fix"


def test_triage_waive_only_when_every_failing_gate_is_optional():
    optional_fail = {"results": [{"name": "lint", "required": False, "ok": False},
                                 {"name": "test", "required": True, "ok": True}]}
    st = _state("TRIAGING", last_gate=optional_fail)
    assert Q.decide_triage(st, PROFILE, _verdict("waive"), True).kind == "close"
    required_fail = {"results": [{"name": "test", "required": True, "ok": False}]}
    st = _state("TRIAGING", last_gate=required_fail)
    step = Q.decide_triage(st, PROFILE, _verdict("waive"), True)
    assert step.kind == "park" and step.reason == "triage_escalated" and step.resume_to == "GATING"


def test_triage_escalate_parks_with_brief():
    step = Q.decide_triage(_state("TRIAGING"), PROFILE, _verdict("escalate", fix_brief="human call"), True)
    assert step.kind == "park" and step.reason == "triage_escalated" and step.detail == "human call"


def test_triage_malformed_retries_once_then_escalates():
    st = _state("TRIAGING")
    assert Q.decide_triage(st, PROFILE, {"garbage": True}, False).kind == "none"
    assert Q.decide_triage(st, PROFILE, None, False).kind == "none"
    st["counters"]["triage_retries"] = 1
    step = Q.decide_triage(st, PROFILE, None, False)
    assert step.kind == "park" and step.reason == "triage_escalated" and "twice" in step.detail


# ---- decide_review (SPEC-0187) -------------------------------------------------------------

def _review(verdict="pass", *severities, **kw):
    v = {"verdict": verdict, "score": 90, "ac_assessment": [], "fix_brief": "do it",
         "findings": [{"severity": sev, "title": f"{sev} thing"} for sev in severities]}
    v.update(kw)
    return v


@pytest.mark.parametrize("verdict,severities,kind,reason", [
    ("pass", (), "close", "review_pass"),
    ("pass", ("minor", "nit", "major"), "close", "review_pass"),
    ("pass", ("must_fix",), "implement", "review_fix"),            # must-fix overrides a pass
    ("fix", (), "implement", "review_fix"),
    ("fix", ("must_fix", "minor"), "implement", "review_fix"),
])
def test_review_routing(verdict, severities, kind, reason):
    step = Q.decide_review(_state("REVIEWING"), PROFILE, _review(verdict, *severities), True)
    assert (step.kind, step.reason) == (kind, reason)


def test_review_fix_beyond_loop_max_parks():
    st = _state("REVIEWING")
    st["counters"]["loops"] = 2
    assert Q.decide_review(st, PROFILE, _review("fix"), True).kind == "implement"   # 3rd loop
    st["counters"]["loops"] = 3
    step = Q.decide_review(st, PROFILE, _review("fix", fix_brief="still wrong"), True)
    assert step.kind == "park" and step.reason == "loop_exhausted"
    assert step.resume_to == "IMPLEMENTING" and "still wrong" in step.detail
    # a pass with a must-fix at the limit parks too
    assert Q.decide_review(st, PROFILE, _review("pass", "must_fix"), True).kind == "park"
    # a clean pass at the limit still closes
    assert Q.decide_review(st, PROFILE, _review("pass"), True).kind == "close"


def test_review_malformed_retries_once_then_escalates():
    st = _state("REVIEWING")
    assert Q.decide_review(st, PROFILE, {"garbage": True}, False).kind == "none"
    assert Q.decide_review(st, PROFILE, None, False).kind == "none"
    st["counters"]["review_retries"] = 1
    step = Q.decide_review(st, PROFILE, None, False)
    assert step.kind == "park" and step.reason == "review_escalated"
    assert step.resume_to == "REVIEWING" and "twice" in step.detail
    assert "review_escalated" in Q.PARK_REASONS


def test_reviewing_is_an_active_resumable_state():
    assert "REVIEWING" in Q.STATES and "REVIEWING" in Q.ACTIVE_STATES
    assert "REVIEWING" in Q.RESUMABLE_STATES


def test_apply_review_fix_sets_brief_and_resets_counters(opened):
    cfg, cid, ctx = opened
    st = S.read_state(cfg, cid)
    st.update(state="REVIEWING", pending_brief={"source": "human", "fix_brief": None,
                                                "must_fix": [], "human_notes": ["be brief"]})
    st["counters"].update(gate_retries=1, triage_retries=1, review_retries=1)
    S.write_state(cfg, cid, st)
    v = _review("fix", "must_fix", "minor")
    v["findings"][0].update(ac="AC one", detail="because", files=["a.py"])
    row, st = _apply(ctx, cfg, cid, "review", persona="conformance_reviewer",
                     run_id="002-conformance_reviewer", run_result=_rr(v), budget=1.5)
    assert row["to"] == "IMPLEMENTING" and row["reason"] == "review_fix"
    assert st["counters"]["loops"] == 1 and st["counters"]["review_retries"] == 0
    assert st["counters"]["gate_retries"] == 0 and st["counters"]["triage_retries"] == 0
    assert st["pending_brief"] == {
        "source": "conformance_reviewer", "fix_brief": "do it", "human_notes": ["be brief"],
        "must_fix": [{"ac": "AC one", "title": "must_fix thing", "detail": "because",
                      "files": ["a.py"]}]}
    assert st["last_review"] == {"run": "002-conformance_reviewer", "verdict": "fix", "score": 90,
                                 "findings": 2, "must_fix": 1, "loop": 0}
    findings = S.read_ledger(cfg, cid, types=["finding"])
    assert [(f["persona"], f["severity"], f["ac"]) for f in findings] == [
        ("conformance_reviewer", "must_fix", "AC one"), ("conformance_reviewer", "minor", None)]


def test_apply_review_malformed_stays_then_escalates(opened):
    cfg, cid, ctx = opened
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="REVIEWING"))
    row, st = _apply(ctx, cfg, cid, "review", persona="conformance_reviewer",
                     run_id="002-conformance_reviewer", run_result=_rr(None, ok=False), budget=1.5)
    assert row["to"] == "REVIEWING" and row["reason"] == "review_retry"
    assert st["counters"]["review_retries"] == 1
    row, st = _apply(ctx, cfg, cid, "review", persona="conformance_reviewer",
                     run_id="003-conformance_reviewer", run_result=_rr(None, ok=False), budget=1.5)
    assert row["to"] == "PARKED" and st["park"]["reason"] == "review_escalated"
    assert st["park"]["resume_to"] == "REVIEWING"


# ---- _apply over synthetic results ------------------------------------------------------------

def _rr(verdict, *, ok=True, cost=0.25, interrupted=False, exit_code=0):
    return RunResult(exit_code=exit_code, timed_out=False, interrupted=interrupted, duration_s=0.1,
                     is_error=exit_code != 0, cost_usd=cost, usage={}, session_id="s",
                     verdict=verdict, verdict_ok=ok, result_text="", raw_path=None)


@pytest.fixture
def opened(tmp_path, monkeypatch):
    """A real campaign, opened (worktree exists), so `_apply` has somewhere to write."""
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()] * 6))
    cid = added(cfg, monkeypatch)
    run_cli(cfg, monkeypatch, ["queue", "tick"])
    st = S.read_state(cfg, cid)
    assert st["state"] == "IMPLEMENTING"
    return cfg, cid, Q._Ctx(cfg)


def _apply(ctx, cfg, cid, kind, **kw):
    st = S.read_state(cfg, cid)
    res = Q.StepResult(cid, Q.Step(kind, persona=kw.pop("persona", None)), st["state"], **kw)
    row = Q._apply(ctx, st, res)
    return row, S.read_state(cfg, cid)


def test_apply_implementer_blocked_twice_parks(opened):
    cfg, cid, ctx = opened
    blocked = {"status": "blocked", "summary": "no", "blockers": ["needs X"]}
    row, st = _apply(ctx, cfg, cid, "implement", persona="implementer", run_id="001-implementer",
                     run_result=_rr(blocked), budget=4.0)
    assert row["to"] == "GATING" and st["counters"]["blocked_streak"] == 1
    S.write_state(cfg, cid, dict(st, state="IMPLEMENTING"))
    row, st = _apply(ctx, cfg, cid, "implement", persona="implementer", run_id="002-implementer",
                     run_result=_rr(blocked), budget=4.0)
    assert row["to"] == "PARKED" and st["park"]["reason"] == "implementer_blocked"
    assert "needs X" in st["park"]["detail"] and st["park"]["resume_to"] == "IMPLEMENTING"
    findings = S.read_ledger(cfg, cid, types=["finding"])
    assert sum(1 for f in findings if f["title"] == "implementer blocker") == 2
    assert st["cost"]["total_usd"] == 0.5


def test_apply_missing_cost_charges_budget_and_notes_it(opened):
    cfg, cid, ctx = opened
    row, st = _apply(ctx, cfg, cid, "implement", persona="implementer", run_id="001-implementer",
                     run_result=_rr({"status": "done", "summary": "ok"}, cost=None), budget=4.0)
    assert row["cost_usd"] == 4.0 and st["cost"]["total_usd"] == 4.0
    assert any("charged its budget" in f["title"] for f in S.read_ledger(cfg, cid, types=["finding"]))


def test_apply_triage_fix_sets_brief_and_resets_retries(opened):
    cfg, cid, ctx = opened
    st = S.read_state(cfg, cid)
    st.update(state="TRIAGING", pending_brief={"source": "human", "fix_brief": None, "must_fix": [],
                                              "human_notes": ["careful"]})
    st["counters"].update(gate_retries=1, triage_retries=1)
    S.write_state(cfg, cid, st)
    verdict = _verdict("fix", fix_brief="write it", must_fix=[{"gate": "test", "title": "t"}])
    row, st = _apply(ctx, cfg, cid, "triage", persona="gate_triager", run_id="002-gate_triager",
                     run_result=_rr(verdict), budget=0.5)
    assert row["to"] == "IMPLEMENTING" and row["reason"] == "triage_fix"
    assert st["counters"]["loops"] == 1 and st["counters"]["gate_retries"] == 0
    assert st["counters"]["triage_retries"] == 0
    assert st["pending_brief"] == {"source": "gate_triager", "fix_brief": "write it",
                                   "must_fix": [{"gate": "test", "title": "t"}],
                                   "human_notes": ["careful"]}
    assert any(f["severity"] == "must_fix" for f in S.read_ledger(cfg, cid, types=["finding"]))


def test_apply_triage_malformed_stays_then_escalates(opened):
    cfg, cid, ctx = opened
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="TRIAGING"))
    row, st = _apply(ctx, cfg, cid, "triage", persona="gate_triager", run_id="002-gate_triager",
                     run_result=_rr({"garbage": 1}, ok=False), budget=0.5)
    assert row["to"] == "TRIAGING" and row["reason"] == "triage_retry"
    assert st["counters"]["triage_retries"] == 1
    row, st = _apply(ctx, cfg, cid, "triage", persona="gate_triager", run_id="003-gate_triager",
                     run_result=_rr(None, ok=False), budget=0.5)
    assert row["to"] == "PARKED" and st["park"]["reason"] == "triage_escalated"


def test_apply_gate_result_routes(opened):
    cfg, cid, ctx = opened
    from wt.agentic.gates import GateResult, GateRun
    good = GateRun(1, True, (GateResult("test", True, True, 0, False, 0.1, "gates/001-test.log", ""),),
                   "2026-10-25T00:00:00", 0.1)
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="GATING"))
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=good)
    assert row["to"] == "REVIEWING" and row["reason"] == "gates_green"
    assert st["last_gate"]["gate_seq"] == 1 and st["counters"]["gate_runs"] == 1
    bad = GateRun(2, False, (GateResult("test", True, False, 1, False, 0.1, "gates/002-test.log", "x"),),
                  "2026-10-25T00:00:00", 0.1)
    S.write_state(cfg, cid, dict(st, state="GATING"))
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=bad)
    assert row["to"] == "TRIAGING" and st["last_gate"]["ok"] is False


def test_apply_close_outcomes(opened):
    cfg, cid, ctx = opened
    from wt.agentic.close import CloseOutcome
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="CLOSING"))
    synthetic = {"name": "spec-dod", "required": True, "ok": False, "tail": "status: accepted"}
    oc = CloseOutcome("TRIAGING", synthetic_gate=synthetic,
                      events=[("finding", {"persona": "close", "severity": "must_fix", "title": "dod",
                                               "gate": "spec-dod", "detail": "", "files": []})])
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert row["to"] == "TRIAGING" and row["reason"] == "spec_dod"
    assert st["last_gate"]["results"] == [synthetic] and st["counters"]["close_attempts"] == 1
    S.write_state(cfg, cid, dict(st, state="CLOSING"))
    oc = CloseOutcome("PARKED", park={"reason": "rebase_conflict", "detail": "a.txt", "resume_to": "CLOSING"},
                      events=[("park", {"reason": "rebase_conflict"})])
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert row["to"] == "PARKED" and st["park"]["reason"] == "rebase_conflict"
    assert sum(1 for e in S.read_ledger(cfg, cid, types=["park"])) == 1       # not duplicated
    S.write_state(cfg, cid, dict(st, state="CLOSING", park=None))
    oc = CloseOutcome("MERGED", close={"mode": "merge", "merged_sha": "abc", "pr_url": None,
                                        "gates_seq": 3, "target_sha": "def"},
                      events=[("close", {"mode": "merge"})])
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert row["to"] == "MERGED" and st["close"]["merged_sha"] == "abc"


def test_apply_step_error_parks(opened):
    cfg, cid, ctx = opened
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="GATING"))
    row, st = _apply(ctx, cfg, cid, "gate", error="boom")
    assert row["to"] == "PARKED" and st["park"]["reason"] == "open_failed"
    assert "boom" in st["park"]["detail"] and st["park"]["resume_to"] == "GATING"


def test_apply_skips_transition_when_human_moved_the_item(opened):
    cfg, cid, ctx = opened
    st = S.read_state(cfg, cid)
    # a human abandons while the (synthetic) step runs
    S.write_state(cfg, cid, dict(st, state="ABANDONED"))
    row = Q._apply(ctx, st, Q.StepResult(cid, Q.Step("implement", persona="implementer"), "IMPLEMENTING",
                                          run_id="001-implementer",
                                          run_result=_rr({"status": "done", "summary": "ok"}), budget=4.0))
    assert row["to"] == "ABANDONED" and row["reason"] == "superseded_by_human"
    assert S.read_state(cfg, cid)["state"] == "ABANDONED"
    types = [e["type"] for e in S.read_ledger(cfg, cid)]
    assert "run.end" in types and "note" in types


# ---- admission, slots, lease ---------------------------------------------------------------------

def test_admission_is_fifo_and_bounded_by_slots(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    (repo / "docs" / "specs" / "0002-second.md").write_text(
        (repo / "docs" / "specs" / "0001-trivial.md").read_text().replace("SPEC-0001", "SPEC-0002"))
    write_review(repo / "docs" / "specs" / "0002-second.md", "SPEC-0002")
    import subprocess
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "second spec"], check=True)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()] * 4), profile={"slots": 1})
    first = added(cfg, monkeypatch, "SPEC-0001")
    second = added(cfg, monkeypatch, "SPEC-0002")
    report = Q.tick(cfg)
    assert report.admitted == 1
    assert S.read_state(cfg, first)["state"] == "IMPLEMENTING"
    assert S.read_state(cfg, second)["state"] == "QUEUED"
    payload = Q.queue_payload(cfg)
    assert payload["slots"] == {"total": 1, "busy": 1}
    report = Q.tick(cfg)                       # first still busy → second waits
    assert report.admitted == 0 and S.read_state(cfg, second)["state"] == "QUEUED"


def test_tick_refuses_while_lease_held(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    added(cfg, monkeypatch)
    lease = L.acquire(cfg, ["other"])
    try:
        r = run_cli(cfg, monkeypatch, ["queue", "tick"])
        assert r.exit_code != 0 and f"pid {lease.info['pid']}" in r.output
        r = run_cli(cfg, monkeypatch, ["queue", "run", "--once"])
        assert r.exit_code != 0 and "queue held" in r.output
    finally:
        lease.release()
    r = run_cli(cfg, monkeypatch, ["queue", "tick", "--json"])
    assert r.exit_code == 0 and json.loads(r.output)["schema"] == "wt.queue.tick.v1"


# ---- SPEC-0180: backlog admission ----------------------------------------------------------------

def _q(cid, priority=3, created="2026-10-25T10:00:00", **over):
    return {"id": cid, "state": "QUEUED", "priority": priority, "created_at": created, **over}


@pytest.mark.parametrize("backlog,busy,slots,not_ready,expect", [
    # priority first, then age, then id
    ([_q("a", 3, "2026-10-25T10:00:00"), _q("b", 1, "2026-10-25T11:00:00"),
      _q("c", 3, "2026-10-25T09:00:00")], 0, 3, (), ["b", "c", "a"]),
    ([_q("a", 2), _q("b", 2)], 0, 2, (), ["a", "b"]),
    # never more than the free slots
    ([_q("a"), _q("b"), _q("c")], 1, 2, (), ["a"]),
    ([_q("a"), _q("b")], 2, 2, (), []),
    ([_q("a"), _q("b")], 5, 2, (), []),
    # not-ready items are skipped, a lower-priority ready one takes the slot
    ([_q("a", 1), _q("b", 5)], 0, 1, ("a",), ["b"]),
    # park-requested items are skipped
    ([_q("a", 1, park_requested={"reason": "human"}), _q("b", 4)], 0, 1, (), ["b"]),
    # a missing priority defaults to 3
    ([{"id": "a", "created_at": "2026-10-25T10:00:00"}, _q("b", 2)], 0, 2, (), ["b", "a"]),
    ([], 0, 3, (), []),
])
def test_pick_admissions(backlog, busy, slots, not_ready, expect):
    picks = Q.pick_admissions(backlog, busy, slots,
                              lambda s: (s["id"] not in not_ready, ["x"] if s["id"] in not_ready else []))
    assert [p["id"] for p in picks] == expect


@pytest.mark.parametrize("value,ok", [(1, True), (5, True), ("3", True), (0, False), (6, False),
                                      ("x", False), (None, False), (True, False)])
def test_check_priority(value, ok):
    if ok:
        assert Q.check_priority(value) == int(value)
    else:
        with pytest.raises(ValueError):
            Q.check_priority(value)


def _spec_file(root, num, status, deps=None):
    d = root / "docs" / "specs"
    d.mkdir(parents=True, exist_ok=True)
    extra = f"depends_on: [{', '.join(deps)}]\n" if deps else ""
    p = d / f"{num:04d}-s.md"
    p.write_text(f"---\nid: SPEC-{num:04d}\nstatus: {status}\n{extra}---\n\nbody\n")
    return p


def _queued(root, num, **over):
    return _q(f"SPEC-{num:04d}-x", repo={"root": str(root)},
              spec={"rel_path": f"docs/specs/{num:04d}-s.md"}, **over)


def test_backlog_readiness_reasons(tmp_path):
    _spec_file(tmp_path, 1, "accepted")
    _spec_file(tmp_path, 2, "done")
    _spec_file(tmp_path, 3, "superseded")
    _spec_file(tmp_path, 4, "accepted", deps=["SPEC-0002", "SPEC-0003"])
    _spec_file(tmp_path, 5, "accepted", deps=["SPEC-0001", "SPEC-0099", "SPEC-0002"])
    _spec_file(tmp_path, 6, "draft")
    _spec_file(tmp_path, 7, "in-progress")
    assert Q.backlog_readiness({}, _queued(tmp_path, 1)) == (True, [])
    assert Q.backlog_readiness({}, _queued(tmp_path, 4)) == (True, [])
    assert Q.backlog_readiness({}, _queued(tmp_path, 5)) == (
        False, ["waiting on SPEC-0001 (accepted)", "SPEC-0099 not found"])
    assert Q.backlog_readiness({}, _queued(tmp_path, 6)) == (False, ["spec status is draft"])
    assert Q.backlog_readiness({}, _queued(tmp_path, 7)) == (False, ["spec status is in-progress"])
    assert Q.backlog_readiness({}, _queued(tmp_path, 7, force=True)) == (True, [])
    assert Q.backlog_readiness({}, _queued(tmp_path, 9)) == (False, ["spec file missing"])


def test_tick_records_one_backlog_event_per_change(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    spec2 = repo / "docs" / "specs" / "0002-second.md"
    spec2.write_text(SPEC_ACCEPTED.format(id="SPEC-0002", extra="depends_on: [SPEC-0099]\n"))
    write_review(spec2, "SPEC-0002")
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    cid = added(cfg, monkeypatch, "SPEC-0002")

    def events():
        return [(e["ready"], e["blocked_by"]) for e in S.read_ledger(cfg, cid, types=["backlog"])]

    Q.tick(cfg)
    Q.tick(cfg)
    assert events() == [(False, ["SPEC-0099 not found"])]
    assert S.read_state(cfg, cid)["state"] == "QUEUED"
    assert S.read_state(cfg, cid)["backlog"]["blocked_by"] == ["SPEC-0099 not found"]
    spec2.write_text(spec2.read_text().replace("status: accepted", "status: draft"))
    Q.tick(cfg)
    Q.tick(cfg)
    assert events()[-1] == (False, ["spec status is draft", "SPEC-0099 not found"])
    assert len(events()) == 2
    spec2.write_text(spec2.read_text().replace("status: draft", "status: accepted")
                     .replace("depends_on: [SPEC-0099]\n", ""))
    report = Q.tick(cfg)
    assert report.admitted == 1 and events()[-1] == (True, [])
    assert len(events()) == 3
    reasons = [e["reason"] for e in S.read_ledger(cfg, cid, types=["transition"])]
    assert reasons[0] == "admitted"
