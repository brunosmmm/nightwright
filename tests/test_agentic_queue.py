"""SPEC-0176: the pure state machine (`plan_step`, `decide_triage`) and `_apply` over synthetic
step results, admission by slots, lease refusal. Repos and runners come from the e2e harness
(`test_agentic_e2e`), personas never run for real."""
import dataclasses
import datetime as dt
import json
import re
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from test_agentic_e2e import (SPEC_ACCEPTED, added, impl_done, make_cfg, make_repo, run_cli,
                              script, write_review)
from nightwright.agentic import lease as L
from nightwright.agentic import personas as PS
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S
from nightwright.agentic.runner import RunResult
from nightwright.cli import cli

pytestmark = pytest.mark.integration

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


def test_plan_step_unknown_cost_does_not_fill_the_cap():
    st = _state("IMPLEMENTING", cost={"total_usd": 16.5, "estimated_usd": 16.5, "cap_usd": 15.0,
                                      "by_persona": {}})
    assert Q.plan_step(st, PROFILE).kind == "implement"


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
    entry = {"verdict": "fix", "score": 90, "fix_brief": "do it", "findings": v["findings"],
             "ac_assessment": v["ac_assessment"], "run": "002-conformance_reviewer", "loop": 0}
    advisory = [{"id": "conformance_reviewer#1", "personas": ["conformance_reviewer"],
                 "severity": "minor", "title": "minor thing", "detail": None, "files": [],
                 "ac": None, "category": None}]
    assert st["last_review"] == {"run": "002-conformance_reviewer", "verdict": "fix", "score": 0,
                                 "findings": 2, "must_fix": 1, "loop": 0, "waived": [],
                                 "advisory": advisory,
                                 "runs": {"conformance_reviewer": "002-conformance_reviewer"},
                                 "verdicts": {"conformance_reviewer": entry}}
    notes = [n["text"] for n in S.read_ledger(cfg, cid, types=["note"])]
    assert "review score 0/80 fix" in notes
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
    assert row["to"] == "IMPLEMENTING" and row["reason"] == "implementer_blocked_retry"
    assert st["state"] == "IMPLEMENTING" and st["counters"]["blocked_streak"] == 1
    row, st = _apply(ctx, cfg, cid, "implement", persona="implementer", run_id="002-implementer",
                     run_result=_rr(blocked), budget=4.0)
    assert row["to"] == "PARKED" and st["park"]["reason"] == "implementer_blocked"
    assert "needs X" in st["park"]["detail"] and st["park"]["resume_to"] == "IMPLEMENTING"
    findings = S.read_ledger(cfg, cid, types=["finding"])
    assert sum(1 for f in findings if f["title"] == "implementer blocker") == 2
    assert st["cost"]["total_usd"] == 0.5


def test_apply_implementer_blocked_with_commit_gates(opened):
    cfg, cid, ctx = opened
    blocked = {"status": "blocked", "summary": "partial", "blockers": ["needs X"]}
    row, st = _apply(ctx, cfg, cid, "implement", persona="implementer", run_id="001-implementer",
                     run_result=_rr(blocked), budget=4.0, commit_sha="abc123",
                     commit_message="wip")
    assert row["to"] == "GATING" and st["counters"]["blocked_streak"] == 1


def test_apply_missing_cost_charges_budget_and_notes_it(opened):
    cfg, cid, ctx = opened
    row, st = _apply(ctx, cfg, cid, "implement", persona="implementer", run_id="001-implementer",
                     run_result=_rr({"status": "done", "summary": "ok"}, cost=None), budget=4.0)
    assert row["cost_usd"] == 4.0 and st["cost"]["total_usd"] == 0.0
    assert st["cost"]["estimated_usd"] == 4.0
    assert any("not counted against the cap" in f["title"]
               for f in S.read_ledger(cfg, cid, types=["finding"]))


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
    from nightwright.agentic.gates import GateResult, GateRun
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
    from nightwright.agentic.close import CloseOutcome
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="CLOSING"))
    synthetic = {"name": "spec-dod", "required": True, "ok": False, "tail": "status: accepted"}
    oc = CloseOutcome("TRIAGING", synthetic_gate=synthetic,
                      events=[("finding", {"persona": "close", "severity": "must_fix", "title": "dod",
                                               "gate": "spec-dod", "detail": "", "files": []})])
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert row["to"] == "PARKED" and row["reason"] == "spec_status"
    assert st["park"]["resume_to"] == "CLOSING" and st["counters"]["close_attempts"] == 1
    S.write_state(cfg, cid, dict(st, state="CLOSING"))
    oc = CloseOutcome("PARKED", park={"reason": "rebase_conflict", "detail": "a.txt", "resume_to": "CLOSING"},
                      events=[("park", {"reason": "rebase_conflict"})])
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert row["to"] == "PARKED" and st["park"]["reason"] == "rebase_conflict"
    rebase_parks = [e for e in S.read_ledger(cfg, cid, types=["park"])
                    if e.get("reason") == "rebase_conflict"]
    assert len(rebase_parks) == 1                                              # not duplicated
    S.write_state(cfg, cid, dict(st, state="CLOSING", park=None))
    oc = CloseOutcome("MERGED", close={"mode": "merge", "merged_sha": "abc", "pr_url": None,
                                        "gates_seq": 3, "target_sha": "def"},
                      events=[("close", {"mode": "merge"})])
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert row["to"] == "MERGED" and st["close"]["merged_sha"] == "abc"


def _merge_with_idea(opened, *, spec_status="done", backlink=True):
    """`opened` campaign set to CLOSING, its spec in the main checkout naming a bare idea
    (`backlink=False`: the spec has no `source_idea`; the caller links the idea)."""
    from nightwright import org_write as OW
    from nightwright.agentic.close import CloseOutcome
    from nightwright.org import load_tasks
    cfg, cid, ctx = opened
    OW.add_idea(cfg, "bookkeeping idea")
    iid = [t for t in load_tasks(cfg) if t.is_idea][0].properties["ID"]
    spec = Path(cfg["specs_dir"]) / "0001-trivial.md"
    text = spec.read_text().replace("status: accepted", f"status: {spec_status}")
    # SPEC-0267: the spec may not predate the idea it names; tomorrow clears any timezone skew
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    text = re.sub(r"(?m)^created:.*\n", "", text).replace(
        "owner: t\n", f"owner: t\ncreated: {tomorrow:%Y-%m-%d}\n", 1)
    if backlink:
        text = text.replace("owner: t\n", f"owner: t\nsource_idea: {iid}\n", 1)
    spec.write_text(text)
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="CLOSING"))
    oc = CloseOutcome("MERGED", close={"mode": "merge", "merged_sha": "abc", "pr_url": None,
                                        "gates_seq": 3, "target_sha": "def"},
                      events=[("close", {"mode": "merge"})])
    return iid, oc


def _idea_state(cfg, iid):
    from nightwright import org_write as OW
    return OW.resolve_selector(cfg, iid).state


def test_merge_reconciles_the_spec_idea(opened):
    cfg, cid, ctx = opened
    iid, oc = _merge_with_idea(opened)
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert st["state"] == "MERGED"
    (ev,) = S.read_ledger(cfg, cid, types=["idea_reconciled"])
    assert (ev["spec_id"], ev["idea_id"], ev["from"], ev["to"]) == ("SPEC-0001", iid, "IDEA", "PROMOTED")
    assert _idea_state(cfg, iid) == "PROMOTED"


def test_merge_reconcile_failure_is_a_minor_finding(opened, monkeypatch):
    cfg, cid, ctx = opened
    iid, oc = _merge_with_idea(opened)

    def boom(*a, **k):
        raise RuntimeError("org file drifted")
    monkeypatch.setattr(Q.SP, "reconcile_ideas", boom)
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert st["state"] == "MERGED" and row["to"] == "MERGED"
    (fnd,) = [f for f in S.read_ledger(cfg, cid, types=["finding"]) if f["gate"] == "bookkeeping"]
    assert fnd["severity"] == "minor" and "org file drifted" in fnd["detail"]
    assert not list(S.read_ledger(cfg, cid, types=["idea_reconciled"]))


def test_merge_reconcile_skipped_for_another_repo(opened, tmp_path):
    cfg, cid, ctx = opened
    iid, oc = _merge_with_idea(opened)
    other = tmp_path / "other-project"
    other.mkdir()
    st = S.read_state(cfg, cid)
    S.write_state(cfg, cid, dict(st, repo=dict(st["repo"], root=str(other))))
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert st["state"] == "MERGED"
    # SPEC-0325: another repo's spec is read from that repo's git; here it has none
    (fnd,) = [f for f in S.read_ledger(cfg, cid, types=["finding"]) if f["gate"] == "bookkeeping"]
    assert fnd["severity"] == "minor" and "spec not readable" in fnd["title"]
    assert _idea_state(cfg, iid) == "IDEA" and not list(S.read_ledger(cfg, cid, types=["idea_reconciled"]))


def test_second_project_close_leaves_wt_checkout_clean(opened, tmp_path):
    """SPEC-0327: a second project's campaign whose spec id (SPEC-0001) equals a wt spec id merges
    without touching wt's checkout, and its idea is reconciled through the project repo."""
    from nightwright import org_write as OW
    cfg, cid, ctx = opened
    cfg["org_idea_keywords"] = [*cfg["org_idea_keywords"], "SHIPPED"]
    wt_repo = Path(S.read_state(cfg, cid)["repo"]["root"])
    iid, oc = _merge_with_idea(opened, backlink=False)
    p2 = tmp_path / "p2"
    p2.mkdir()
    proj = make_repo(p2, review=False, policy="pr")
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    spec = proj / "docs" / "specs" / "0001-trivial.md"
    spec.write_text(spec.read_text().replace("status: accepted", "status: done").replace(
        "created: 2026-10-25", f"created: {tomorrow:%Y-%m-%d}\nsource_idea: {iid}"))
    subprocess.run(["git", "-C", str(proj), "commit", "-qam", "done"], check=True)
    head = subprocess.run(["git", "-C", str(proj), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()
    OW.set_property(cfg, OW.resolve_selector(cfg, iid), "PROJECT", "comfort-assistant")
    OW.set_property(cfg, OW.resolve_selector(cfg, iid), "SPEC", "SPEC-0001")
    st = S.read_state(cfg, cid)
    S.write_state(cfg, cid, dict(st, project="comfort-assistant",
                                 repo=dict(st["repo"], root=str(proj))))
    oc.close["merged_sha"] = head
    wt_spec = Path(cfg["specs_dir"]) / "0001-trivial.md"
    porcelain = lambda: subprocess.run(["git", "status", "--porcelain"], cwd=wt_repo,  # noqa: E731
                                       capture_output=True, text=True, check=True).stdout
    # start clean (commit the fixture's own edits), so "left clean" is an absolute claim
    subprocess.run(["git", "-C", str(wt_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(wt_repo), "commit", "-qm", "fixture"], check=True)
    before, before_spec = porcelain(), wt_spec.read_bytes()
    assert before == ""
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert st["state"] == "MERGED"
    assert porcelain() == "" and wt_spec.read_bytes() == before_spec
    (ev,) = S.read_ledger(cfg, cid, types=["idea_reconciled"])
    assert (ev["spec_id"], ev["idea_id"], ev["to"]) == ("SPEC-0001", iid, "SHIPPED")
    assert _idea_state(cfg, iid) == "SHIPPED"
    # the global walk (`wt spec reconcile --apply`) must not stamp the idea onto wt's SPEC-0001
    # either: that is the write that dirtied the main checkout
    from nightwright import specs as SPECS
    SPECS.reconcile_ideas(cfg, apply=True)
    assert porcelain() == "" and wt_spec.read_bytes() == before_spec
    assert "source_idea" not in wt_spec.read_text()


@pytest.mark.parametrize("root", [None, ""])
def test_merge_reconcile_skipped_without_a_repo_root(opened, monkeypatch, root):
    """An unknown repo root is not the cwd: even when the queue runs from above the specs dir."""
    cfg, cid, ctx = opened
    iid, oc = _merge_with_idea(opened)
    st = S.read_state(cfg, cid)
    S.write_state(cfg, cid, dict(st, repo=dict(st["repo"], root=root)))
    monkeypatch.chdir("/")
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert st["state"] == "MERGED"
    (fnd,) = [f for f in S.read_ledger(cfg, cid, types=["finding"]) if f["gate"] == "bookkeeping"]
    assert fnd["severity"] == "info" and "not this repo" in fnd["title"]
    assert _idea_state(cfg, iid) == "IDEA"


def test_merge_reconcile_skipped_when_specs_dir_unlocatable(opened, monkeypatch):
    cfg, cid, ctx = opened
    iid, oc = _merge_with_idea(opened)

    def nowhere(c):
        raise ValueError("cannot locate the internal specs directory")
    monkeypatch.setattr(Q.SP, "_specs_dir", nowhere)
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert st["state"] == "MERGED"
    (fnd,) = [f for f in S.read_ledger(cfg, cid, types=["finding"]) if f["gate"] == "bookkeeping"]
    assert fnd["severity"] == "info" and "not this repo" in fnd["title"]


def test_merge_without_a_spec_skips_reconcile_silently(opened, monkeypatch):
    cfg, cid, ctx = opened
    iid, oc = _merge_with_idea(opened)
    st = S.read_state(cfg, cid)
    st.pop("spec_id", None)
    st["kind"], st["spec"] = "merge", None
    S.write_state(cfg, cid, st)
    monkeypatch.setattr(Q.SP, "reconcile_ideas", lambda *a, **k: pytest.fail("must not run"))
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert st["state"] == "MERGED"
    assert not [f for f in S.read_ledger(cfg, cid, types=["finding"]) if f["gate"] == "bookkeeping"]
    assert not list(S.read_ledger(cfg, cid, types=["idea_reconciled"]))


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
    write_review({"data_dir": str(tmp_path / "data")}, repo / "docs" / "specs" / "0002-second.md", "SPEC-0002")
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
    assert r.exit_code == 0 and json.loads(r.output)["schema"] == "nw.queue.tick.v1"


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
    write_review({"data_dir": str(tmp_path / "data")}, spec2, "SPEC-0002")
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


# ---- SPEC-0206: review triager state machine ---------------------------------------------------

RV = "conformance_reviewer"
TG = "review_triager"


def _minors(n):
    return {"verdict": "pass", "score": 90, "fix_brief": "", "ac_assessment": [],
            "findings": [{"severity": "minor", "title": f"m{i}", "detail": "d",
                          "files": ["a.py"]} for i in range(n)]}


def _tv(*ids, reason="subjective", note="n"):
    return {"waive": [{"id": i, "reason": reason} for i in ids], "note": note}


def _reviewing(cfg, cid, **over):
    st = S.read_state(cfg, cid)
    st.update(state="REVIEWING", **over)
    S.write_state(cfg, cid, st)


def _review_in(ctx, cfg, cid, v):
    return _apply(ctx, cfg, cid, "review", persona=RV, run_id="002-conformance_reviewer",
                  run_result=_rr(v), budget=1.5)


def _triager_in(ctx, cfg, cid, rr, run_id="003-review_triager"):
    st = S.read_state(cfg, cid)
    step = Q.Step("review", persona=TG, personas=(TG,))
    row = Q._apply(ctx, st, Q.StepResult(cid, step, st["state"], run_id=run_id, run_result=rr,
                                         budget=0.5))
    return row, S.read_state(cfg, cid)


def _pending(opened, n=6):
    cfg, cid, ctx = opened
    _reviewing(cfg, cid)
    row, st = _review_in(ctx, cfg, cid, _minors(n))
    assert (row["to"], row["reason"]) == ("REVIEWING", "review_triage"), row
    return cfg, cid, ctx, st


def _notes(cfg, cid):
    return [n["text"] for n in S.read_ledger(cfg, cid, types=["note"])]


def test_triage_pending_after_the_last_valid_verdict(opened):
    cfg, cid, ctx, st = _pending(opened)
    lr = st["last_review"]
    assert lr["triage"] == "pending" and lr["verdicts"][RV]["loop"] == 0
    assert "score" not in lr and "verdict" not in lr                  # not evaluated yet
    assert st["state"] == "REVIEWING" and st["counters"]["loops"] == 0
    assert st["counters"]["review_retries"] == 0
    assert not any(n.startswith("review score") for n in _notes(cfg, cid))
    step = Q.plan_step(st, ctx.profile(st))
    assert (step.kind, step.persona, step.personas) == ("review", TG, (TG,))
    assert [e["to"] for e in S.read_ledger(cfg, cid, types=["transition"])][-1] != "REVIEWING"


def test_triager_is_skipped_for_pass_must_fix_and_synthetic(opened):
    cfg, cid, ctx = opened
    fix = {"verdict": "fix", "score": 40, "fix_brief": "x", "ac_assessment": [],
           "findings": _minors(6)["findings"] + [{"severity": "must_fix", "title": "bad"}]}
    for v, to in ((_minors(2), "CLOSING"), (fix, "IMPLEMENTING"),
                  ({"verdict": "fix", "score": 1, "fix_brief": "x", "findings": [],
                    "ac_assessment": []}, "IMPLEMENTING")):
        _reviewing(cfg, cid)
        st = S.read_state(cfg, cid)
        st["counters"]["loops"] = 0
        S.write_state(cfg, cid, st)
        row, st = _review_in(ctx, cfg, cid, v)
        assert row["to"] == to and "triage" not in st["last_review"], row


def test_triager_waives_enough_and_the_campaign_passes(opened):
    cfg, cid, ctx, _ = _pending(opened)
    before = S.read_state(cfg, cid)["cost"]["total_usd"]
    row, st = _triager_in(ctx, cfg, cid, _rr(_tv(f"{RV}#0", f"{RV}#1", f"{RV}#2"), cost=0.1))
    assert (row["to"], row["reason"], row["run_id"]) == ("CLOSING", "review_pass",
                                                         "003-review_triager")
    lr = st["last_review"]
    assert lr["triage"] == "done" and lr["triager_run"] == "003-review_triager"
    assert [w["id"] for w in lr["waived"]] == [f"{RV}#0", f"{RV}#1", f"{RV}#2"]
    assert lr["waived"][0]["reason"] == "subjective" and lr["waived"][0]["title"] == "m0"
    assert (lr["verdict"], lr["score"], lr["findings"]) == ("pass", 88, 3)
    assert [a["title"] for a in lr["advisory"]] == ["m3", "m4", "m5"]
    assert RV in lr["verdicts"] and TG not in lr["verdicts"]
    notes = _notes(cfg, cid)
    assert notes.count("review score 88/80 pass") == 1
    assert [n for n in notes if n.startswith("waived ")] == [f"waived {RV}#{i}: subjective"
                                                            for i in range(3)]
    assert st["cost"]["by_persona"][TG] == 0.1 and st["cost"]["total_usd"] == round(before + 0.1, 4)
    assert not [f for f in S.read_ledger(cfg, cid, types=["finding"]) if f["persona"] == TG]
    assert [e["persona"] for e in S.read_ledger(cfg, cid, types=["run.end"])][-1] == TG


def test_triager_waives_too_little_and_the_review_still_fixes(opened):
    cfg, cid, ctx, _ = _pending(opened, 8)
    row, st = _triager_in(ctx, cfg, cid, _rr(_tv(f"{RV}#0", f"{RV}#1")))
    assert (row["to"], row["reason"]) == ("IMPLEMENTING", "review_fix")
    lr = st["last_review"]
    assert lr["triage"] == "done" and (lr["verdict"], lr["score"]) == ("fix", 76)
    assert st["counters"]["loops"] == 1 and len(st["pending_brief"]["must_fix"]) == 6
    assert "review score 76/80 fix" in _notes(cfg, cid)
    assert [w["id"] for w in lr["waived"]] == [f"{RV}#0", f"{RV}#1"]     # kept for reporting


def test_triager_invalid_waivers_are_ignored_with_notes(opened):
    cfg, cid, ctx, _ = _pending(opened, 8)
    bad = {"waive": [{"id": "nobody#3", "reason": "x"}, {"id": f"{RV}#0", "reason": ""},
                     {"id": f"{RV}#1", "reason": "ok"}], "note": "n"}
    row, st = _triager_in(ctx, cfg, cid, _rr(bad))
    assert [w["id"] for w in st["last_review"]["waived"]] == [f"{RV}#1"]
    notes = _notes(cfg, cid)
    assert "triager ignored nobody#3: not a finding of this review" in notes
    assert f"triager ignored {RV}#0: empty reason" in notes
    assert f"waived {RV}#1: ok" in notes and st["last_review"]["score"] == 72


@pytest.mark.parametrize("rr_kw,why", [
    ({"verdict": {"garbage": True}, "ok": False}, "malformed"),
    ({"verdict": None, "ok": False, "exit_code": 1, "cost": None}, "failed"),
    ({"verdict": None, "ok": False, "exit_code": 1, "timed_out": True}, "timed out"),
])
def test_bad_triager_run_leaves_the_deterministic_outcome(opened, rr_kw, why):
    cfg, cid, ctx, st = _pending(opened)
    before = st["cost"]["total_usd"]
    kw = dict(rr_kw)
    timed_out = kw.pop("timed_out", False)
    rr = dataclasses.replace(_rr(kw.pop("verdict"), **kw), timed_out=timed_out)
    row, st = _triager_in(ctx, cfg, cid, rr)
    assert (row["to"], row["reason"]) == ("IMPLEMENTING", "review_fix")       # score-only fix
    lr = st["last_review"]
    assert lr["triage"] == "done" and lr["waived"] == [] and lr["score"] == 76
    assert st["state"] == "IMPLEMENTING" and st["counters"]["loops"] == 1
    assert st["counters"]["review_retries"] == 0 and "review_retries_by_persona" not in \
        st["counters"]
    assert any(why in n and "deterministic" in n for n in _notes(cfg, cid))
    if rr.cost_usd is None:
        assert st["cost"]["by_persona"].get(TG, 0.0) == 0.0
        assert st["cost"]["total_usd"] == before
        assert st["cost"]["estimated_usd"] == 0.5
    else:
        charged = kw.get("cost", 0.25)
        assert st["cost"]["by_persona"][TG] == charged
        assert st["cost"]["total_usd"] == round(before + charged, 4)


def test_triager_without_a_run_result_counts_as_done(opened):
    cfg, cid, ctx, _ = _pending(opened)
    st = S.read_state(cfg, cid)
    step = Q.Step("review", persona=TG, personas=(TG,))
    res = Q.StepResult(cid, step, "REVIEWING", reviews=[Q.StepResult(cid, step, "REVIEWING",
                                                                      error="boom")])
    row = Q._apply(ctx, st, res)
    st = S.read_state(cfg, cid)
    assert st["last_review"]["triage"] == "done" and row["to"] == "IMPLEMENTING"
    assert any("review triager run not started: boom" in n for n in _notes(cfg, cid))


def test_interrupted_triager_keeps_pending(opened):
    cfg, cid, ctx, _ = _pending(opened)
    row, st = _triager_in(ctx, cfg, cid, _rr(_tv(f"{RV}#0"), interrupted=True))
    assert (row["to"], row["reason"]) == ("REVIEWING", "interrupted")
    assert st["state"] == "REVIEWING" and st["last_review"]["triage"] == "pending"
    assert "waived" not in st["last_review"] and st["cost"]["by_persona"][TG] == 0.25
    step = Q.plan_step(st, ctx.profile(st))
    assert step.personas == (TG,)                       # the next tick re-spawns it
    assert [e for e in S.read_ledger(cfg, cid, types=["run.end"])][-1]["interrupted"] is True


def test_cap_too_small_skips_the_triager(opened):
    cfg, cid, ctx = opened
    _reviewing(cfg, cid)
    st = S.read_state(cfg, cid)
    st["cost"].update(total_usd=9.6, cap_usd=10.0)                     # triager budget is 0.5
    S.write_state(cfg, cid, st)
    row, st = _review_in(ctx, cfg, cid, _minors(6))
    assert (row["to"], row["reason"]) == ("IMPLEMENTING", "review_fix")
    assert st["last_review"]["triage"] == "skipped" and st["last_review"]["score"] == 76
    notes = _notes(cfg, cid)
    assert notes.count("triager skipped: cost cap") == 1
    assert "review score 76/80 fix" in notes
    # terminal: a later evaluation of the same review never re-evaluates the triager
    _reviewing(cfg, cid, counters=dict(st["counters"], loops=0))
    st = S.read_state(cfg, cid)
    st["cost"]["total_usd"] = 0.0
    S.write_state(cfg, cid, st)
    assert Q.plan_step(st, ctx.profile(st)).personas != (TG,)
    row, st = _review_in(ctx, cfg, cid, _minors(6))
    assert st["last_review"]["triage"] == "skipped" and row["reason"] == "review_fix"
    assert notes.count("triager skipped: cost cap") == 1


def test_triage_runs_at_most_once_per_loop_and_clears_on_reentry(opened):
    cfg, cid, ctx, _ = _pending(opened, 8)
    _, st = _triager_in(ctx, cfg, cid, _rr(_tv(f"{RV}#0")))
    assert st["state"] == "IMPLEMENTING" and st["last_review"]["triage"] == "done"
    assert st["last_review"]["waived"]                          # kept after leaving REVIEWING
    st["state"] = "GATING"
    st = Q._transition(cfg, st, "REVIEWING", "gates_green")
    lr = S.read_state(cfg, cid)["last_review"]
    assert not {"triage", "triager_run", "waived", "verdicts"} & set(lr)
    # second loop: the triager is evaluated afresh, and runs again only via a new pending
    row, st = _review_in(ctx, cfg, cid, _minors(6))
    assert st["last_review"]["triage"] == "pending"
    _, st = _triager_in(ctx, cfg, cid, _rr(_tv()), run_id="005-review_triager")
    assert st["last_review"]["triage"] == "done"
    step = Q.plan_step(dict(st, state="REVIEWING"), ctx.profile(st))
    assert step.personas != (TG,)                               # done: never a second run


def test_stored_waivers_survive_a_later_evaluation(opened):
    cfg, cid, ctx, _ = _pending(opened)
    _, st = _triager_in(ctx, cfg, cid, _rr(_tv(f"{RV}#0", f"{RV}#1", f"{RV}#2")))
    assert st["state"] == "CLOSING"
    # a re-evaluation of the same review (a reviewer result arriving again) keeps the waivers
    _reviewing(cfg, cid)
    row, st = _review_in(ctx, cfg, cid, _minors(6))
    assert (row["to"], row["reason"]) == ("CLOSING", "review_pass")
    assert st["last_review"]["score"] == 88 and len(st["last_review"]["waived"]) == 3
    assert st["last_review"]["triage"] == "done"


def test_prepare_run_gives_the_triager_its_input(opened):
    cfg, cid, ctx, st = _pending(opened)
    run_id, req, budget = Q._prepare_run(ctx, st, TG)
    assert run_id.endswith("-review_triager") and budget == 0.5 and req.read_only
    assert f"{RV}#0" in req.prompt and f"{RV}#5" in req.prompt and '"score": 76' in req.prompt
    assert "{{" not in req.prompt and req.json_schema is PS.REVIEW_TRIAGER_SCHEMA
    assert "(none)" in req.prompt                  # previous verdicts blanked


# ---- SPEC-0207: follow-up ideas ---------------------------------------------------------------

from nightwright import org as ORG                                          # noqa: E402
from nightwright.agentic import close as CL                                 # noqa: E402
from nightwright.agentic import followups as FU                             # noqa: E402


def _ideas(cfg):
    ORG.invalidate_tasks_cache()
    return [t for t in ORG.load_tasks(cfg) if t.is_idea]


def _fu_review(verdict, *findings):
    return {"verdict": verdict, "score": 50, "ac_assessment": [], "fix_brief": "do it",
            "findings": [dict({"detail": "d", "files": ["a.py"]}, **f) for f in findings]}


def _review_apply(ctx, cfg, cid, verdict, **counters):
    st = S.read_state(cfg, cid)
    st.update(state="REVIEWING")
    st["counters"].update(counters)
    S.write_state(cfg, cid, st)
    return _apply(ctx, cfg, cid, "review", persona="conformance_reviewer",
                  run_id="002-conformance_reviewer", run_result=_rr(verdict), budget=1.5)


def test_fix_outcome_persists_the_review_carry(opened):               # AC4: finish persists it
    cfg, cid, ctx = opened
    v = _fu_review("fix", {"severity": "must_fix", "title": "broken"},
                   {"severity": "minor", "title": "naming"})
    row, st = _review_apply(ctx, cfg, cid, v)
    assert row["to"] == "IMPLEMENTING"
    assert [(e["title"], e["severity"], e["origin"]) for e in st["review_carry"]] == [
        ("naming", "minor", "advisory")]
    # next loop: "naming" turns blocking (any severity) ⇒ dropped; a new minor is carried
    v = _fu_review("fix", {"severity": "major", "title": "Naming"},
                   {"severity": "nit", "title": "typo", "files": ["b.py"]})
    row, st = _review_apply(ctx, cfg, cid, v)
    assert [e["title"] for e in st["review_carry"]] == ["typo"]


def test_loop_exhausted_park_also_carries(opened):
    cfg, cid, ctx = opened
    v = _fu_review("fix", {"severity": "must_fix", "title": "broken"},
                   {"severity": "minor", "title": "naming"})
    row, st = _review_apply(ctx, cfg, cid, v, loops=3)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "loop_exhausted"
    assert [e["title"] for e in st["review_carry"]] == ["naming"]


def test_pass_does_not_touch_the_carry(opened):
    cfg, cid, ctx = opened
    row, st = _review_apply(ctx, cfg, cid, _fu_review("pass", {"severity": "minor", "title": "n"}))
    assert row["to"] == "CLOSING" and "review_carry" not in st
    assert st["last_review"]["advisory"][0]["title"] == "n"


def _closing(cfg, cid, advisory, carry=None, waived=()):
    st = S.read_state(cfg, cid)
    st.update(state="CLOSING", last_review={"advisory": advisory, "waived": list(waived)})
    if carry is not None:
        st["review_carry"] = carry
    S.write_state(cfg, cid, st)
    return st


def _adv(sev, title, **kw):
    return dict({"id": "x#0", "personas": ["conformance_reviewer"], "severity": sev,
                 "title": title, "detail": "d", "files": ["a.py"], "ac": None,
                 "category": None}, **kw)


def _merged_outcome():
    close = {"mode": "merge", "merged_sha": "abc", "pr_url": None, "gates_seq": 1,
             "target_sha": "def"}
    return CL.CloseOutcome("MERGED", close=close, events=[("close", close)])


def test_close_files_followups_and_state_close_keeps_them(opened, monkeypatch):    # AC5, AC6
    cfg, cid, ctx = opened
    carry = [FU.make_entry("SPEC-0001", _adv("minor", "earlier loop", files=["z.py"]),
                           "advisory")]
    st = _closing(cfg, cid, [_adv("major", "advisory one")], carry=carry,
                  waived=[_adv("nit", "waived nit", files=["w.py"], reason="style")])
    monkeypatch.setattr(CL, "prepare_close", lambda *a, **k: CL.PreparedClose(outcome=_merged_outcome()))
    res = Q._do_prepare_close(ctx, st)
    assert res.error is None and len(res.events_before) == 2
    row = Q._apply(ctx, st, res)
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED" and row["to"] == "MERGED"
    fu = st["close"]["followups"]
    assert [f["title"] for f in fu["filed"]] == ["advisory one", "earlier loop"]     # by severity
    assert fu["unfiled"] == [] and fu["waived"] == [
        {"severity": "nit", "title": "waived nit", "waive_reason": "style"}]
    assert st["close"]["mode"] == "merge"
    [group] = _ideas(cfg)
    assert fu["idea_id"] == group.properties["ID"] and fu["capped"] == []
    assert row["followups"] == {"filed": 2, "unfiled": 0, "capped": 0,
                                "idea_id": group.properties["ID"],
                                "ids": [group.properties["ID"]],        # distinct, not per finding
                                "items": fu["filed"], "unfiled_items": [],
                                "waived": fu["waived"]}
    payload = json.loads(json.dumps(Q.TickReport(steps=[row], idle=False).payload()))
    emitted = payload["steps"][0]["followups"]                      # survives nw.queue.tick.v1
    assert emitted["items"][0]["title"] == "advisory one" and emitted["items"][0]["severity"] == "major"
    assert emitted["waived"] == [{"severity": "nit", "title": "waived nit", "waive_reason": "style"}]
    assert [e["title"] for e in S.read_ledger(cfg, cid, types=["followup"])] == [
        "advisory one", "earlier loop"]
    assert len(_ideas(cfg)) == 1
    [close_ev] = S.read_ledger(cfg, cid, types=["close"])
    assert "followups" not in close_ev                              # the event payload is untouched


def test_close_raising_still_records_the_filer_events_and_parks(opened, monkeypatch):
    cfg, cid, ctx = opened
    st = _closing(cfg, cid, [_adv("minor", "keep me")])

    def boom(*a, **k):
        raise RuntimeError("close exploded")

    monkeypatch.setattr(CL, "prepare_close", boom)
    res = Q._do_prepare_close(ctx, st)
    assert res.error == "RuntimeError('close exploded')" and res.followups["filed"]
    row = Q._apply(ctx, st, res)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "open_failed"
    assert st["park"]["resume_to"] == "CLOSING" and row["followups"]["filed"] == 1
    [ev] = S.read_ledger(cfg, cid, types=["followup"])
    assert ev["title"] == "keep me"
    # the resume re-runs the close step: the finding is reported again, filed once
    monkeypatch.setattr(CL, "prepare_close", lambda *a, **k: CL.PreparedClose(outcome=_merged_outcome()))
    st = S.read_state(cfg, cid)
    st.update(state="CLOSING", park=None)
    S.write_state(cfg, cid, st)
    res = Q._do_prepare_close(ctx, st)
    assert res.events_before == [] and res.followups["filed"][0]["idea_id"] == ev["idea_id"]
    Q._apply(ctx, st, res)
    assert len(_ideas(cfg)) == 1 and len(S.read_ledger(cfg, cid, types=["followup"])) == 1
    assert S.read_state(cfg, cid)["close"]["followups"]["filed"][0]["idea_id"] == ev["idea_id"]


def test_close_row_reports_capped_and_distinct_ids_over_two_groups(opened, monkeypatch):  # AC6
    cfg, cid, ctx = opened
    cfg["agentic"]["profiles"]["balanced"]["review"] = {"followups_max": 1}
    other = FU.OW.add_idea(cfg, FU.group_title("SPEC-0001-20261025-bbbb", "SPEC-0001", 1),
                           kind="improvement")[2]
    key = FU.make_entry("SPEC-0001", _adv("minor", "elsewhere", files=["e.py"]), "advisory")["key"]
    FU.EX.set_summary(cfg, other, f"- [ ] nwfu:{key[:12]} minor x: y")
    carry = [FU.make_entry("SPEC-0001", _adv("minor", "elsewhere", files=["e.py"]), "advisory")]
    st = _closing(cfg, cid, [_adv("major", "kept"), _adv("minor", "cut one"),
                             _adv("minor", "cut two", files=["b.py"])], carry=carry)
    monkeypatch.setattr(CL, "prepare_close", lambda *a, **k: CL.PreparedClose(outcome=_merged_outcome()))
    row = Q._apply(ctx, st, Q._do_prepare_close(ctx, st))
    fu = row["followups"]
    own = S.read_state(cfg, cid)["close"]["followups"]["idea_id"]
    assert own and own != other and fu["capped"] == 2 and fu["idea_id"] == own
    assert fu["filed"] == 2 and fu["ids"] == [other, own] and len(fu["items"]) == 2


def test_set_summary_failure_never_prevents_the_merge(opened, monkeypatch):
    cfg, cid, ctx = opened
    st = _closing(cfg, cid, [_adv("minor", "x")])
    monkeypatch.setattr(FU.EX, "set_summary", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    monkeypatch.setattr(CL, "prepare_close", lambda *a, **k: CL.PreparedClose(outcome=_merged_outcome()))
    row = Q._apply(ctx, st, Q._do_prepare_close(ctx, st))
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED" and row["to"] == "MERGED"
    assert [u["title"] for u in st["close"]["followups"]["unfiled"]] == ["x"]
    assert S.read_ledger(cfg, cid, types=["followup"]) == []
    assert any("follow-ups not filed" in e.get("text", "")
               for e in S.read_ledger(cfg, cid, types=["note"]))


def test_filer_failure_never_prevents_the_close(opened, monkeypatch):
    cfg, cid, ctx = opened
    st = _closing(cfg, cid, [_adv("minor", "x")])
    monkeypatch.setattr(FU.OW, "add_idea", lambda *a, **k: (_ for _ in ()).throw(KeyError("org")))
    monkeypatch.setattr(CL, "prepare_close", lambda *a, **k: CL.PreparedClose(outcome=_merged_outcome()))
    res = Q._do_prepare_close(ctx, st)
    Q._apply(ctx, st, res)
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED"
    assert [u["title"] for u in st["close"]["followups"]["unfiled"]] == ["x"]
    assert S.read_ledger(cfg, cid, types=["followup"]) == []


def test_followups_false_files_nothing(opened, monkeypatch):
    cfg, cid, ctx = opened
    cfg["agentic"]["profiles"]["balanced"]["review"] = {"followups": False}
    ctx = Q._Ctx(cfg)
    st = _closing(cfg, cid, [_adv("major", "x")])
    monkeypatch.setattr(CL, "prepare_close", lambda *a, **k: CL.PreparedClose(outcome=_merged_outcome()))
    Q._apply(ctx, st, Q._do_prepare_close(ctx, st))
    assert S.read_state(cfg, cid)["close"]["followups"]["filed"] == [] and _ideas(cfg) == []


def test_campaign_show_lists_followups_unfiled_and_waivers(opened, monkeypatch):    # AC6
    cfg, cid, ctx = opened
    st = S.read_state(cfg, cid)
    st["close"] = {"mode": "merge", "followups": {
        "filed": [{"idea_id": "IDEA-007", "severity": "minor", "title": "naming", "origin": "advisory"}],
        "unfiled": [{"severity": "major", "title": "big one", "error": "OSError('full')"}],
        "waived": [{"severity": "nit", "title": "tiny", "waive_reason": "style only"}],
        "capped": [{"severity": "minor", "title": "left out"}], "idea_id": "IDEA-009"}}
    S.write_state(cfg, cid, st)
    r = run_cli(cfg, monkeypatch, ["campaign", "show", cid])
    assert r.exit_code == 0, r.output
    assert "follow-up IDEA-007 minor naming" in r.output
    assert "follow-ups IDEA-009 (1 listed, 1 capped)" in r.output
    assert "[capped] minor left out" in r.output
    assert "[unfiled] major big one: OSError('full')" in r.output
    assert "[waived] nit tiny: style only" in r.output


def test_regate_failed_rerun_reports_earlier_filings_without_duplicates(opened, monkeypatch):
    cfg, cid, ctx = opened
    st = _closing(cfg, cid, [_adv("minor", "keep me")])
    monkeypatch.setattr(CL, "prepare_close", lambda *a, **k: CL.PreparedClose(outcome=CL.CloseOutcome("TRIAGING")))
    res = Q._do_prepare_close(ctx, st)
    row = Q._apply(ctx, st, res)
    assert row["to"] == "TRIAGING" and row["reason"] == "regate_failed"
    [ev] = S.read_ledger(cfg, cid, types=["followup"])
    # TRIAGING -> ... -> CLOSING again: the re-run reports the filing and files nothing new
    monkeypatch.setattr(CL, "prepare_close", lambda *a, **k: CL.PreparedClose(outcome=_merged_outcome()))
    st = S.read_state(cfg, cid)
    st.update(state="CLOSING")
    S.write_state(cfg, cid, st)
    res = Q._do_prepare_close(ctx, st)
    assert res.events_before == []
    assert [f["idea_id"] for f in res.followups["filed"]] == [ev["idea_id"]]
    Q._apply(ctx, st, res)
    assert S.read_state(cfg, cid)["close"]["followups"]["filed"][0]["idea_id"] == ev["idea_id"]
    assert len(_ideas(cfg)) == 1 and len(S.read_ledger(cfg, cid, types=["followup"])) == 1


# ---- SPEC-0202: adversarial reviewer ---------------------------------------------------------

def test_finding_detail_three_cases():
    assert Q.finding_detail({"detail": "d", "trigger": "t"}) == "d\ntrigger: t"
    assert Q.finding_detail({"trigger": "t"}) == "trigger: t"
    assert Q.finding_detail({"detail": "d"}) == "d"
    assert Q.finding_detail({}) is None
    assert Q.finding_detail({"detail": "d", "trigger": ""}) == "d"


ADV_FINDINGS = [
    {"severity": "must_fix", "category": "path_traversal", "title": "escape", "detail": "joined",
     "trigger": "when name has ../", "files": ["a.py"], "lines": ["a.py:3"]},
    {"severity": "minor", "category": "other", "title": "meh", "trigger": "rare"},
    {"severity": "nit", "title": "no category, as the conformance reviewer"},
]


def test_record_run_maps_category_and_folds_trigger():
    st = _state("REVIEWING")
    verdict = {"verdict": "fix", "score": 30, "fix_brief": "b", "findings": ADV_FINDINGS}
    events = Q._record_run(st, "adversarial_reviewer", "003-adversarial_reviewer",
                           _rr(verdict), 1.5)
    found = [p for t, p in events if t == "finding"]
    assert found[0] == {"persona": "adversarial_reviewer", "severity": "must_fix", "gate": None,
                        "ac": None, "title": "escape", "files": ["a.py"],
                        "detail": "joined\ntrigger: when name has ../",
                        "category": "path_traversal", "run_id": "003-adversarial_reviewer"}
    assert found[1]["detail"] == "trigger: rare" and found[1]["category"] == "other"
    assert "category" not in found[2] and found[2]["detail"] is None
    assert "trigger" not in found[0] and "lines" not in found[0]


def test_review_brief_must_fix_carries_category_and_trigger():
    """SPEC-0202 on SPEC-0203's triage: brief entries carry `category` and the folded trigger."""
    from nightwright.agentic.profiles import resolve_profile
    prof = resolve_profile({}, "balanced")
    one = {"adversarial_reviewer": {"verdict": "pass", "findings": ADV_FINDINGS, "fix_brief": ""}}
    tri = Q.triage_reviews(one, prof)
    assert tri["outcome"] == "fix"
    out = Q.review_brief(one, tri, 80)
    assert out["must_fix"] == [{"ac": None, "title": "escape", "files": ["a.py"],
                                "detail": "joined\ntrigger: when name has ../",
                                "category": "path_traversal"}]
    both = {"conformance_reviewer": {"verdict": "pass", "findings": [], "fix_brief": ""},
            "adversarial_reviewer": {"verdict": "fix", "findings": ADV_FINDINGS,
                                     "fix_brief": "b"}}
    out = Q.review_brief(both, Q.triage_reviews(both, prof), 80)
    assert out["must_fix"][0]["personas"] == ["adversarial_reviewer"]
    assert out["must_fix"][0]["category"] == "path_traversal"


def test_cap_sum_includes_the_adversarial_reviewer():
    """balanced lists conformance (1.5) + test (1.0) + adversarial (1.5) = 4.0: at 7.5 spent the
    first alone fits the $10 cap but the full set does not."""
    from nightwright.agentic.profiles import resolve_profile
    profile = resolve_profile({}, "balanced")
    assert profile["reviewers"][-1] == "adversarial_reviewer"
    fits = _state("REVIEWING", cost={"total_usd": 5.5, "cap_usd": 10.0, "by_persona": {}})
    step = Q.plan_step(fits, profile)
    assert step.kind == "review" and step.personas == tuple(profile["reviewers"])
    over = _state("REVIEWING", cost={"total_usd": 7.5, "cap_usd": 10.0, "by_persona": {}})
    assert Q.plan_step(over, dict(profile, reviewers=["conformance_reviewer"])).kind == "review"
    step = Q.plan_step(over, profile)
    assert step.kind == "park" and step.reason == "cost_cap" and step.resume_to == "REVIEWING"
    assert "adversarial_reviewer" in step.detail and "$4.00" in step.detail


@pytest.mark.parametrize("total,expect", [
    (1.0, "resolve"),          # total + 2.0 <= cap 4.0: plain cap
    (3.0, "resolve"),          # over the plain cap, within cap + allowance (4.0 + 4.0)
    (6.0, "resolve"),          # 6.0 + 2.0 == 8.0: still within
    (6.5, "park"),             # 8.5 > 8.0: ordinary cost_cap
])
def test_resolve_within_close_allowance(total, expect):         # SPEC-0303
    from nightwright.agentic.profiles import resolve_profile
    profile = resolve_profile({}, "balanced")
    st = _state("CLOSING", closing={"attempts": 0, "resolve": {"target": "main"}},
                cost={"total_usd": total, "cap_usd": 4.0, "by_persona": {}})
    step = Q.plan_step(st, profile)
    assert step.kind == expect
    if expect == "park":
        assert step.reason == "cost_cap" and step.persona == "conflict_resolver"
        assert step.resume_to == "CLOSING" and "close allowance" in step.detail
    assert st["cost"]["cap_usd"] == 4.0                          # the cap itself never changes


def test_allowance_default_and_config():                        # SPEC-0303
    from nightwright.agentic.profiles import persona_settings, resolve_profile
    profile = resolve_profile({}, "balanced")
    budget = float(persona_settings(profile, "conflict_resolver")["max_budget_usd"])
    assert Q._resolver_allowance(None, profile) == budget * Q.MAX_RESOLVE_ATTEMPTS
    assert Q._resolver_allowance({"merge": {"resolver_allowance_usd": None}}, profile) \
        == budget * 2
    assert Q._resolver_allowance({"merge": {"resolver_allowance_usd": 0.5}}, profile) == 0.5
    assert Q._resolver_allowance({"merge": {"resolver_allowance_usd": 0}}, profile) == 0.0
    st = _state("CLOSING", closing={"attempts": 0, "resolve": {"target": "main"}},
                cost={"total_usd": 3.0, "cap_usd": 4.0, "by_persona": {}})
    cfg = {"merge": {"resolver_allowance_usd": 0}}
    assert Q.plan_step(st, profile, cfg).reason == "cost_cap"
    cfg = {"merge": {"resolver_allowance_usd": 5}}
    assert Q.plan_step(st, profile, cfg).kind == "resolve"
    from nightwright.agentic import project as P
    with pytest.raises(ValueError, match="resolver_allowance_usd"):
        P.validate({"schema": P.SCHEMA, "merge": {"resolver_allowance_usd": -1}})
    with pytest.raises(ValueError, match="resolver_allowance_usd"):
        P.validate({"schema": P.SCHEMA, "merge": {"resolver_allowance_usd": "2"}})
    P.validate({"schema": P.SCHEMA, "merge": {"resolver_allowance_usd": 0}})


def test_queue_payload_sizing_fields_null_without_sizing(tmp_path):    # SPEC-0219
    cfg = {"data_dir": str(tmp_path / "data"), "agentic": {}}
    S.create_campaign(cfg, {"id": "A", "spec_id": "SPEC-0001", "profile": "balanced",
                            "state": "IMPLEMENTING"})
    S.create_campaign(cfg, {"id": "B", "spec_id": "SPEC-0002", "profile": "frugal",
                            "state": "IMPLEMENTING",
                            "sizing": {"size": "xs", "tone": "cost-effective", "source": "review"}})
    items = {it["campaign"]: it for it in Q.queue_payload(cfg)["items"]}
    assert (items["A"]["size"], items["A"]["tone"], items["A"]["profile_source"]) == (None,) * 3
    assert (items["B"]["size"], items["B"]["tone"], items["B"]["profile"],
            items["B"]["profile_source"]) == ("xs", "cost-effective", "frugal", "review")


# ---- SPEC-0230: admission after one revision ------------------------------------------------------

REV_FINDINGS = [{"severity": "major", "section": "Design", "title": "gap in step two",
                 "detail": "state the timeout behaviour"},
                {"severity": "nit", "title": "loose wording"}]


def _revised(tmp_path, entries):
    """A repo whose spec has a `revise` review for older text (the revision was applied)."""
    from nightwright.agentic import spec_review as SR
    repo = make_repo(tmp_path, review=False)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, entries))
    spec = repo / "docs" / "specs" / "0001-trivial.md"
    write_review(cfg, spec, "SPEC-0001", "revise", sha="f" * 64, findings=REV_FINDINGS)
    return repo, cfg, SR.spec_sha256(spec)


def test_plan_and_apply_add_after_revision(tmp_path, monkeypatch):
    from nightwright.agentic import schemas as SC
    from nightwright.agentic import spec_review as SR
    repo, cfg, sha = _revised(tmp_path, [])
    plan = Q.plan_add(cfg, "SPEC-0001")
    assert plan["review"] == "after_revision"
    assert plan["revision"] == {"review_sha": "f" * 64, "current_sha": sha,
                                "findings": REV_FINDINGS}
    state = Q.apply_add(cfg, plan)
    assert state["pending_brief"] == SR.revision_brief({"spec_sha256": "f" * 64,
                                                        "findings": REV_FINDINGS})
    events = S.read_ledger(cfg, plan["campaign"])
    assert [e["type"] for e in events] == ["open", "admitted_after_revision"]
    assert all(SC.ledger_problems(e) == [] for e in events)
    assert {k: events[1][k] for k in ("review_sha", "current_sha", "findings")} == plan["revision"]
    assert S.read_state(cfg, plan["campaign"])["pending_brief"] == state["pending_brief"]


def test_queue_add_revise_pending_opens_into_spec_writing(tmp_path, monkeypatch):   # SPEC-0279
    from nightwright.agentic import schemas as SC
    from nightwright.agentic import spec_review as SR
    repo = make_repo(tmp_path, review=False)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    spec = repo / "docs" / "specs" / "0001-trivial.md"
    sha = SR.spec_sha256(spec)
    write_review(cfg, spec, "SPEC-0001", "revise", findings=REV_FINDINGS)
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001"])           # dry run
    assert r.exit_code == 0, r.output
    assert "revision: pending (2 findings), spec_writer amends first" in r.output
    plan = Q.plan_add(cfg, "SPEC-0001")
    assert plan["review"] == "revise_pending"
    assert plan["revision"] == {"review_sha": sha, "current_sha": sha,
                                "findings": REV_FINDINGS, "pending": True}
    cid = added(cfg, monkeypatch)
    st = S.read_state(cfg, cid)
    assert st["admission"] == "revise_pending" and st["spec"]["id"] == "SPEC-0001"
    assert st["revision"] == {"review_sha": sha, "findings": REV_FINDINGS}
    assert st["pending_brief"]["source"] == "spec_review_revise"
    assert [m["title"] for m in st["pending_brief"]["must_fix"]][0] \
        == "[major] gap in step two (Design)"
    events = S.read_ledger(cfg, cid)
    assert [e["type"] for e in events] == ["open", "admitted_revise_pending"]
    assert all(SC.ledger_problems(e) == [] for e in events)
    Q.tick(cfg)                                          # one tick: open
    st = S.read_state(cfg, cid)
    assert st["state"] == "SPEC_WRITING" and st["pending_brief"]["source"] == "spec_review_revise"
    from nightwright.agentic.profiles import resolve_profile
    assert Q.plan_step(st, resolve_profile({}, "balanced")).kind == "write_spec"


@pytest.mark.parametrize("amended", [True, False])
def test_revise_review_sizing_picks_the_matrix_profile(tmp_path, amended):   # SPEC-0308
    from nightwright.agentic import schemas as SC
    from nightwright.agentic import spec_review as SR
    repo = make_repo(tmp_path, review=False)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    spec = repo / "docs" / "specs" / "0001-trivial.md"
    write_review(cfg, spec, "SPEC-0001", "revise", findings=REV_FINDINGS,
                 sha=("f" * 64) if amended else None, size="s", tone="cost-effective",
                 rationale="small")
    plan = Q.plan_add(cfg, "SPEC-0001")
    assert plan["review"] == ("after_revision" if amended else "revise_pending")
    assert (plan["profile"], plan["size"], plan["tone"]) == ("frugal", "s", "cost-effective")
    state = Q.apply_add(cfg, plan)
    want = {"size": "s", "tone": "cost-effective", "source": "review"}
    assert state["profile"] == "frugal"
    assert {k: state["sizing"][k] for k in want} == want and state["sizing"]["rationale"] == "small"
    assert state["sizing"]["reason"].endswith("(revise review, applied without re-review)")
    opened = S.read_ledger(cfg, plan["campaign"])[0]
    assert opened["type"] == "open" and SC.ledger_problems(opened) == []
    assert (opened["profile"], opened["size"], opened["tone"], opened["profile_source"]) \
        == ("frugal", "s", "cost-effective", "review")
    assert SR.would_pick(cfg, SR.load_review(cfg, "SPEC-0001", "f" * 64 if amended
                                             else SR.spec_sha256(spec)))["profile"] == "frugal"


def test_split_review_sizing_stays_unsized(tmp_path, monkeypatch):           # SPEC-0308
    from test_agentic_split import ORIG, _lint_ledger, _spec_md, _write, _git, make_project
    repo = make_project(tmp_path, with_epic=True)
    _write(repo, ORIG, _spec_md(5, None, title="Big thing", status="accepted"))
    _lint_ledger(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "x")
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    write_review(cfg, repo / ORIG, "SPEC-0005", "split", size="s", tone="cost-effective",
                 suggested_splits=[{"title": "A", "scope": "sa"}, {"title": "B", "scope": "sb"}])
    plan = Q.plan_add(cfg, "SPEC-0005")
    assert plan["review"] == "split_pending"
    assert (plan["size"], plan["tone"], plan["profile_source"]) == (None, None, "default")


def test_plan_add_approved_and_forced_have_no_revision(tmp_path):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    plan = Q.plan_add(cfg, "SPEC-0001")
    assert plan["review"] == "approved" and plan["revision"] is None
    state = Q.apply_add(cfg, plan)
    assert state["pending_brief"] is None
    assert [e["type"] for e in S.read_ledger(cfg, plan["campaign"])] == ["open"]


def test_plan_add_forced_on_revise_reviewed_spec_has_no_revision(tmp_path):
    repo, cfg, _ = _revised(tmp_path, [])
    plan = Q.plan_add(cfg, "SPEC-0001", force=True)
    assert plan["review"] == "forced" and plan["revision"] is None
    state = Q.apply_add(cfg, plan)
    assert state["pending_brief"] is None
    assert [e["type"] for e in S.read_ledger(cfg, plan["campaign"])] == ["open"]


def test_first_implementer_prompt_has_findings_second_does_not(tmp_path, monkeypatch):
    entries = [impl_done(), {"persona": "conformance_reviewer", "cost_usd": 0.2, "verdict": {
        "verdict": "fix", "score": 40, "ac_assessment": [], "fix_brief": "rewrite feature.txt",
        "findings": [{"severity": "must_fix", "ac": "feature.txt exists", "title": "wrong text",
                      "detail": "d", "suggested_fix": "f", "files": ["feature.txt"]}]}},
               impl_done(content="better\n"), {"persona": "conformance_reviewer", "cost_usd": 0.2,
                                               "verdict": {"verdict": "pass", "score": 95,
                                                           "findings": [], "ac_assessment": [],
                                                           "fix_brief": ""}}]
    repo, cfg, _ = _revised(tmp_path, entries)
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    first = (S.runs_dir(cfg, cid) / "001-implementer" / "prompt.md").read_text()
    second = (S.runs_dir(cfg, cid) / "003-implementer" / "prompt.md").read_text()
    for title in ("[major] gap in step two (Design)", "[nit] loose wording"):
        assert title in first and title not in second
    assert "state the timeout behaviour" in first and "From: spec_review" in first
    assert "From: spec_review" not in second


# ---- SPEC-0222: park owner / decision / overdue --------------------------------------------

def test_park_owner_table():
    assert Q.PARK_OWNERS == {"main_dirty": "human", "main_not_on_base": "human",
                             "human_decision": "human", "restart": "human"}   # SPEC-0375
    for reason in Q.PARK_REASONS:
        assert Q.park_owner(reason) == ("human" if reason in ("main_dirty", "main_not_on_base",
                                                               "human_decision", "restart")
                                        else "conductor"), reason
    assert Q.park_owner("human_decision") == "human"
    assert "human_decision" in Q.PARK_REASONS                                # SPEC-0232
    assert Q.park_owner(None) == "conductor" and Q.park_owner("whatever") == "conductor"


def _park_items(tmp_path, **cfg_over):
    from test_agentic_dashboard import _campaign, _fixture, ev_at
    repo, cfg, _ = _fixture(tmp_path)
    cfg.update(cfg_over)
    return repo, cfg, _campaign, ev_at


def _mk(cfg, repo, campaign, ev_at, cid, reason, *, ago_s):
    campaign(cfg, repo, cid, "PARKED", park={"reason": reason, "detail": "d",
                                              "resume_to": "GATING"})
    ev_at(cfg, cid, "park", {"reason": reason}, ago_s=ago_s)
    return cid


def _pk(cfg, now=None):
    return {i["campaign"]: i["park"] for i in Q.queue_payload(cfg, now=now)["items"]
            if i["state"] == "PARKED"}


def test_queue_payload_park_keys_decision_rules(tmp_path):
    repo, cfg, campaign, ev_at = _park_items(tmp_path)
    undecided = _mk(cfg, repo, campaign, ev_at, "SPEC-0001-und", "rebase_conflict", ago_s=60)
    decided = _mk(cfg, repo, campaign, ev_at, "SPEC-0001-dec", "rebase_conflict", ago_s=60)
    before = _mk(cfg, repo, campaign, ev_at, "SPEC-0001-bef", "base_moving", ago_s=60)
    repark = _mk(cfg, repo, campaign, ev_at, "SPEC-0001-rep", "base_moving", ago_s=60)
    human = _mk(cfg, repo, campaign, ev_at, "SPEC-0001-hum", "human", ago_s=60)
    dirty = _mk(cfg, repo, campaign, ev_at, "SPEC-0001-dir", "main_dirty", ago_s=60)
    ev_at(cfg, decided, "note", {"text": "first", "author": "conductor"}, ago_s=50,
          by="agent:conductor")
    ev_at(cfg, decided, "note", {"text": "rebased", "author": "conductor"}, ago_s=40,
          by="agent:conductor")
    # a note written before the park does not count: re-order by parking after the note
    ev_at(cfg, before, "note", {"text": "stale", "author": "human"}, ago_s=70, by="human")
    ev_at(cfg, before, "park", {"reason": "base_moving"}, ago_s=60)
    ev_at(cfg, repark, "note", {"text": "handled", "author": "conductor"}, ago_s=50)
    ev_at(cfg, repark, "park", {"reason": "base_moving"}, ago_s=30)
    park = _pk(cfg)
    assert park[undecided]["owner"] == "conductor" and park[undecided]["decision"] is None
    assert park[undecided]["decided"] is False and park[undecided]["overdue"] is False
    assert park[undecided]["parked_at"]
    d = park[decided]["decision"]
    assert d["text"] == "rebased" and d["by"] == "conductor" and d["ts"]
    assert park[decided]["decided"] is True
    assert park[before]["decision"] is None and park[before]["decided"] is False
    assert park[repark]["decision"] is None and park[repark]["decided"] is False
    assert park[human]["decided"] is True and park[human]["overdue"] is False
    assert park[dirty]["owner"] == "human" and park[dirty]["overdue"] is False
    assert park[undecided]["reason"] == "rebase_conflict"          # existing keys survive
    # non-parked items keep their (null) park
    assert all(i["park"] is None for i in Q.queue_payload(cfg)["items"]
               if i["state"] == "IMPLEMENTING")


def test_overdue_threshold_and_setting(tmp_path):
    import datetime as dt
    repo, cfg, campaign, ev_at = _park_items(tmp_path)
    cid = _mk(cfg, repo, campaign, ev_at, "SPEC-0001-ovr", "base_moving", ago_s=0)
    base = dt.datetime.now().astimezone()
    at = lambda s: base + dt.timedelta(seconds=s)          # noqa: E731
    assert _pk(cfg, at(290))[cid]["overdue"] is False
    assert _pk(cfg, at(310))[cid]["overdue"] is True
    cfg["agentic"] = {**cfg.get("agentic", {}), "events": {"park_decide_s": 600}}
    assert _pk(cfg, at(310))[cid]["overdue"] is False
    assert _pk(cfg, at(610))[cid]["overdue"] is True
    ev_at(cfg, cid, "note", {"text": "done", "author": "conductor"}, ago_s=-400)
    assert _pk(cfg, at(610))[cid]["overdue"] is False


# ---- SPEC-0237: the project's profile layer ---------------------------------------------------

PROFILE_SCHEMA = "schema: nw.profile.v1\n"


def _project_profile(root, text, name="balanced"):
    d = root / ".nw" / "profiles"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.yaml").write_text(PROFILE_SCHEMA + text, encoding="utf-8")
    return d / f"{name}.yaml"


def _model(ctx, state):
    return ctx.profile(state)["personas"]["implementer"]["model"]


def test_ctx_profile_uses_the_campaigns_project_layer(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
    _project_profile(a, "personas: {implementer: {model: haiku}}\nslots: 7\n")
    cfg = {"agentic": {"profiles": {}}}
    ctx = Q._Ctx(cfg)
    st_a = {"profile": "balanced", "repo": {"root": str(a)}}
    st_b = {"profile": "balanced", "repo": {"root": str(b)}}
    assert _model(ctx, st_a) == "haiku" and _model(ctx, st_b) == "sonnet"
    assert ctx.profile({"profile": "balanced"})["slots"] == 2        # no repo: no project layer
    assert Q.slots_for(cfg, "balanced") == 2                          # slots ignore project files


def test_ctx_profile_ignores_a_worktree_copy_and_sees_edits_on_the_next_ctx(tmp_path):
    root, wt = tmp_path / "main", tmp_path / "nw"
    root.mkdir()
    wt.mkdir()
    cfg = {"agentic": {"profiles": {}}}
    st = {"profile": "balanced", "repo": {"root": str(root), "worktree": str(wt)}}
    _project_profile(wt, "personas: {implementer: {model: haiku}}\n")
    assert _model(Q._Ctx(cfg), st) == "sonnet"                       # worktree copy is ignored
    _project_profile(root, "personas: {implementer: {model: opus}}\n")
    ctx = Q._Ctx(cfg)
    assert _model(ctx, st) == "opus"
    _project_profile(root, "personas: {implementer: {model: haiku}}\n")
    assert _model(ctx, st) == "opus"                                 # cached for the tick
    assert _model(Q._Ctx(cfg), st) == "haiku"                        # next tick: re-read


def test_invalid_project_profile_parks_an_active_campaign_config_invalid(opened):
    cfg, cid, ctx = opened
    root = S.read_state(cfg, cid)["repo"]["root"]
    f = _project_profile(Path(root), "personas: {implementer: {modle: opus}}\n")
    report = Q.tick(cfg)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "config_invalid"   # SPEC-0263
    assert st["park"]["detail"].startswith("profile:")
    assert str(f) in st["park"]["detail"] and "personas.implementer.modle" in st["park"]["detail"]
    assert report.steps[-1]["to"] == "PARKED"


def test_invalid_profile_on_a_reviewing_campaign_parks_and_resumes_in_place(opened):
    cfg, cid, ctx = opened
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="REVIEWING"))
    root = S.read_state(cfg, cid)["repo"]["root"]
    f = _project_profile(Path(root), "slots: 0\n")
    Q.tick(cfg)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "config_invalid"   # SPEC-0263
    assert st["park"]["resume_to"] == "REVIEWING"
    f.unlink()
    plan = Q.plan_resume(cfg, cid)
    assert plan["to"] == "REVIEWING"
    Q.apply_resume(cfg, plan)
    st = S.read_state(cfg, cid)
    assert st["state"] == "REVIEWING"
    assert not [e for e in S.read_ledger(cfg, cid, types=["transition"])[-2:]
                if e["to"] in ("QUEUED", "OPENING")]


def test_invalid_project_profile_fails_the_open_before_the_worktree(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    cid = added(cfg, monkeypatch)
    f = _project_profile(repo, "slots: 0\n")
    Q.tick(cfg)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "open_failed"
    assert str(f) in st["park"]["detail"] and "slots" in st["park"]["detail"]
    assert not Path(st["repo"]["worktree"]).exists()


def test_queue_add_fails_with_the_profile_file_message(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    f = _project_profile(repo, "bogus: 1\n")
    r = run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--yes", "--json"])
    assert r.exit_code != 0 and f"{f}: bogus: unknown key" in r.output


def test_queue_payload_uses_the_campaigns_project_layer_for_loop_max(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    cid = added(cfg, monkeypatch)
    [item] = [i for i in Q.queue_payload(cfg)["items"] if i["campaign"] == cid]
    assert item["loops"].endswith("/3")
    _project_profile(repo, "loop_max: 9\n")
    [item] = [i for i in Q.queue_payload(cfg)["items"] if i["campaign"] == cid]
    assert item["loops"].endswith("/9")


# ---- SPEC-0232: `_park` extra / count ------------------------------------------------------------

def test_park_extra_merges_and_count_false_skips_counter(opened):
    cfg, cid, _ = opened
    st = S.read_state(cfg, cid)
    before = st["counters"]["parks"]
    Q._park(cfg, st, "human_decision", "ESC-0001: q", "IMPLEMENTING", by="conductor",
            extra={"escalated_from": "loop_exhausted"}, count=False)
    st = S.read_state(cfg, cid)
    assert st["park"]["escalated_from"] == "loop_exhausted"
    assert st["park"]["reason"] == "human_decision"
    assert st["counters"]["parks"] == before
    assert "human_decision" in Q.PARK_REASONS


# ---- SPEC-0232: a pending escalation meets the running step's outcome ------------------------------

@pytest.fixture
def pending_escalation(opened, monkeypatch):
    from nightwright.agentic import escalations as ESC
    monkeypatch.setattr(ESC, "_run_notify", lambda argv: type("R", (), {"returncode": 0})())
    cfg, cid, ctx = opened
    lease = L.acquire(cfg, [cid])
    try:
        plan = ESC.plan_escalate(cfg, cid, question="scope?", options=["a", "b"], frm="conductor")
        rec = ESC.apply_escalate(cfg, plan)
    finally:
        lease.release()
    assert S.read_state(cfg, cid)["park_requested"]["reason"] == "human_decision"
    return cfg, cid, ctx, rec, ESC


def _set(cfg, cid, **kw):
    st = S.read_state(cfg, cid)
    st.update({k: v for k, v in kw.items() if k != "counters"})
    st["counters"].update(kw.get("counters", {}))
    S.write_state(cfg, cid, st)


def test_apply_pending_escalation_parks_as_human_decision(pending_escalation):
    from nightwright.agentic.gates import GateResult, GateRun
    cfg, cid, ctx, rec, ESC = pending_escalation
    _set(cfg, cid, state="GATING")
    good = GateRun(1, True, (GateResult("test", True, True, 0, False, 0.1, "gates/001-test.log", ""),),
                   "2026-10-25T00:00:00", 0.1)
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=good)
    assert row["to"] == "PARKED" and row["reason"] == "human_decision"
    assert st["park"]["reason"] == "human_decision" and "park_requested" not in st
    assert st["park"]["detail"].startswith(rec["id"])
    assert S.read_ledger(cfg, cid, types=["park"])[-1]["by"] == "conductor"
    assert ESC.open_for(cfg, cid)["id"] == rec["id"]


def test_apply_step_park_with_pending_escalation_keeps_the_step_reason(pending_escalation):
    cfg, cid, ctx, rec, ESC = pending_escalation
    _set(cfg, cid, state="TRIAGING", counters={"loops": 3})
    verdict = _verdict("fix", fix_brief="write it", must_fix=[{"gate": "test", "title": "t"}])
    row, st = _apply(ctx, cfg, cid, "triage", persona="gate_triager", run_id="002-gate_triager",
                     run_result=_rr(verdict), budget=0.5)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "human_decision"
    assert st["park"]["escalated_from"] == "loop_exhausted"
    assert "(step parked:" in st["park"]["detail"] and st["park"]["detail"].startswith(rec["id"])
    assert ESC.read(cfg, rec["id"])["park"]["escalated_from"] == "loop_exhausted"
    assert ESC.open_for(cfg, cid)["id"] == rec["id"]


def test_apply_step_that_merges_withdraws_the_pending_escalation(pending_escalation):
    cfg, cid, ctx, rec, ESC = pending_escalation
    from nightwright.agentic.close import CloseOutcome
    _set(cfg, cid, state="CLOSING")
    oc = CloseOutcome("MERGED", close={"mode": "merge", "merged_sha": "abc", "pr_url": None,
                                        "gates_seq": 3, "target_sha": "def"},
                      events=[("close", {"mode": "merge"})])
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert st["state"] == "MERGED"
    got = ESC.read(cfg, rec["id"])
    assert got["status"] == "withdrawn" and got["withdrawn"]["reason"] == "campaign MERGED"


def test_plan_and_apply_add_record_profile_sources(tmp_path):                    # SPEC-0238
    from nightwright.agentic import schemas as SC
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    cfg["agentic"]["profiles"]["balanced"] = {"slots": 3}
    plan = Q.plan_add(cfg, "SPEC-0001")
    want = ["builtin:balanced.yaml", "config.yaml:agentic.profiles.balanced"]
    assert plan["profile_sources"] == want
    state = Q.apply_add(cfg, plan)
    assert state["profile_sources"] == want
    assert S.read_state(cfg, plan["campaign"])["profile_sources"] == want
    (ev,) = S.read_ledger(cfg, plan["campaign"])
    assert ev["profile_sources"] == want and SC.ledger_problems(ev) == []
    assert "profile_sources" not in SC.EVENT_KEYS["open"]


# ---- SPEC-0235: human overrides ----------------------------------------------------------

def _human_override(cfg, cid, **over):
    st = S.read_state(cfg, cid)
    st["human_overrides"] = {"waive": [{"id": f"{RV}#0", "title": "bad", "files": ["a.py"],
                                        "severity": "must_fix", "synthetic": False, "ac": None,
                                        "reason": "r", "escalation": "ESC-0001", "at": "t"}],
                             "attest": [], **over}
    S.write_state(cfg, cid, st)


def test_begin_review_keeps_human_overrides():
    state = {"counters": {"runs": 4}, "last_review": {"verdicts": {"x": 1}},
             "human_overrides": {"waive": [{"id": "a"}], "attest": []}}
    Q._begin_review(state)
    assert state["human_overrides"] == {"waive": [{"id": "a"}], "attest": []}
    assert "verdicts" not in state["last_review"]


def test_review_with_human_overrides_routes_to_closing_with_a_note(opened):
    cfg, cid, ctx = opened
    _reviewing(cfg, cid)
    _human_override(cfg, cid)
    fix = {"verdict": "fix", "score": 20, "fix_brief": "x", "ac_assessment": [],
           "findings": [{"severity": "must_fix", "title": "Bad", "files": ["a.py"]}]}
    row, st = _review_in(ctx, cfg, cid, fix)
    assert (row["to"], row["reason"]) == ("CLOSING", "review_pass"), row
    assert "human override: waived 1, attested 0" in _notes(cfg, cid)
    assert st["human_overrides"]["waive"][0]["id"] == f"{RV}#0"      # on-disk value kept


def test_accept_waivers_ignores_a_human_waived_id(opened):
    cfg, cid, ctx = opened
    _reviewing(cfg, cid)
    _human_override(cfg, cid, waive=[{"id": f"{RV}#0", "title": "m0", "files": ["a.py"],
                                      "severity": "minor", "synthetic": False, "ac": None}])
    st = S.read_state(cfg, cid)
    st["last_review"] = {"verdicts": {RV: dict(_minors(2), loop=0, run="002-x")}}
    got, ignored = Q._accept_waivers(st, ctx.profile(st), [{"id": f"{RV}#0", "reason": "r"}])
    assert got == [] and ignored == [(f"{RV}#0", "not a finding of this review")]


def test_merge_reconcile_ambiguous_is_a_minor_finding(opened):
    cfg, cid, ctx = opened
    iid, oc = _merge_with_idea(opened)
    specs = Path(cfg["specs_dir"])
    other = (specs / "0001-trivial.md").read_text().replace("SPEC-0001", "SPEC-0002")
    (specs / "0002-other.md").write_text(other)               # a second live spec, same idea
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert st["state"] == "MERGED" and row["to"] == "MERGED"
    (fnd,) = [f for f in S.read_ledger(cfg, cid, types=["finding"]) if f["gate"] == "bookkeeping"]
    assert fnd["severity"] == "minor" and fnd["title"] == "idea reconcile ambiguous"
    assert not list(S.read_ledger(cfg, cid, types=["idea_reconciled"]))
    assert _idea_state(cfg, iid) == "IDEA"


def test_merge_reconcile_never_edits_the_main_checkout(opened):
    """An idea linked by hand to a spec without `source_idea`: the close advances the idea but
    must not write the back-link into the (tracked) spec, or the next close parks main_dirty."""
    from nightwright import org_write as OW
    cfg, cid, ctx = opened
    iid, oc = _merge_with_idea(opened, backlink=False)
    OW.set_property(cfg, OW.resolve_selector(cfg, iid), "SPEC", "SPEC-0001")
    repo = Path(S.read_state(cfg, cid)["repo"]["root"])
    spec = Path(cfg["specs_dir"]) / "0001-trivial.md"
    before = spec.read_bytes()
    def dirty():
        return subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo,
                              capture_output=True, text=True, check=True).stdout
    status_before = dirty()             # the fixture's own status edit; the close must add nothing
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert st["state"] == "MERGED" and _idea_state(cfg, iid) == "PROMOTED"
    assert spec.read_bytes() == before and b"source_idea" not in before
    assert dirty() == status_before     # a new tracked edit here would park the next close
    (fnd,) = [f for f in S.read_ledger(cfg, cid, types=["finding"]) if f["gate"] == "bookkeeping"]
    assert fnd["severity"] == "info" and fnd["title"] == "idea reconcile: spec has no source_idea"


def test_merge_reconcile_link_only_idea_emits_an_unchanged_state_event(opened):
    """An `accepted` spec naming an idea already SPECCED: only the `:SPEC:` is written."""
    from nightwright import org_write as OW
    cfg, cid, ctx = opened
    iid, oc = _merge_with_idea(opened, spec_status="accepted")
    OW.set_state_by_selector(cfg, iid, "SPECCED")
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert st["state"] == "MERGED"
    (ev,) = S.read_ledger(cfg, cid, types=["idea_reconciled"])
    assert (ev["idea_id"], ev["from"], ev["to"]) == (iid, "SPECCED", "SPECCED")
    assert (OW.resolve_selector(cfg, iid).properties.get("SPEC") or "").strip() == "SPEC-0001"


def test_merge_reconcile_partial_write_is_ledgered(opened, monkeypatch):
    """The link lands, the state write raises: a minor finding and an event for the link."""
    from nightwright import explore as EX
    from nightwright import org_write as OW
    cfg, cid, ctx = opened
    iid, oc = _merge_with_idea(opened)

    def boom(*a, **k):
        raise RuntimeError("org file drifted")
    monkeypatch.setattr(EX, "set_idea_state", boom)
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert st["state"] == "MERGED"
    (fnd,) = [f for f in S.read_ledger(cfg, cid, types=["finding"]) if f["gate"] == "bookkeeping"]
    assert fnd["severity"] == "minor" and "org file drifted" in fnd["detail"]
    (ev,) = S.read_ledger(cfg, cid, types=["idea_reconciled"])
    assert (ev["idea_id"], ev["from"], ev["to"]) == (iid, "IDEA", "IDEA")
    assert (OW.resolve_selector(cfg, iid).properties.get("SPEC") or "").strip() == "SPEC-0001"


# ---- SPEC-0263: unique run ids, honest profile parks ----------------------------------------

_START = {"persona": "implementer", "model": "m", "effort": "low", "max_budget_usd": 1.0,
          "timeout_s": 60, "permission_mode": "default", "prompt_sha256": "0" * 64}


def test_run_id_not_reused_after_state_write_failure(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    cid = added(cfg, monkeypatch)
    Q.tick(cfg)                                       # admit + open: IMPLEMENTING
    # the stored counter lags the ledger, as after a `write_state` failure following `run.start`
    for n in (1, 2):
        S.append_event(cfg, cid, "run.start", _START | {"run_id": f"{n:03d}-implementer"})
    S.append_event(cfg, cid, "run.start", _START | {"run_id": "legacy"})    # no number: skipped
    st = S.read_state(cfg, cid)
    st["counters"]["runs"] = 1
    S.write_state(cfg, cid, st)
    assert st["state"] == "IMPLEMENTING"
    Q.tick(cfg)
    ids = [e["run_id"] for e in S.read_ledger(cfg, cid, types=["run.start"])]
    assert ids == ["001-implementer", "002-implementer", "legacy", "003-implementer"]
    assert S.read_state(cfg, cid)["counters"]["runs"] == 3
    assert (S.runs_dir(cfg, cid) / "003-implementer").is_dir()
    assert not (S.runs_dir(cfg, cid) / "002-implementer").exists()


def test_broken_profile_parks_config_invalid_and_recovers(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    cid = added(cfg, monkeypatch)
    Q.tick(cfg)                                       # admit + open
    st = S.read_state(cfg, cid)
    assert st["state"] == "IMPLEMENTING"
    st["profile"] = "p"
    S.write_state(cfg, cid, st)
    pfile = Path(cfg["config_dir"]) / "profiles" / "p.yaml"
    pfile.parent.mkdir(parents=True, exist_ok=True)
    good = "schema: nw.profile.v1\nextends: balanced\nslots: 2\n"
    pfile.write_text(good)
    assert Q.resolve_profile(cfg, "p")["slots"] == 2                 # the good file loads
    pfile.write_text("slots: [unclosed\n")
    Q.tick(cfg)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "config_invalid"
    assert st["park"]["resume_to"] == "IMPLEMENTING"
    assert st["park"]["detail"].startswith("profile:")
    def reasons():
        return [e["reason"] for e in S.read_ledger(cfg, cid, types=["transition"])]

    assert reasons().count("config_invalid") == 1
    for _ in range(2):                                # still broken: stays parked, no false recovery
        Q.tick(cfg)
        assert S.read_state(cfg, cid)["state"] == "PARKED"
        assert "config_recovered" not in reasons()
        assert reasons().count("config_invalid") == 1
    pfile.write_text(good)
    Q.tick(cfg)
    assert S.read_state(cfg, cid)["state"] in ("IMPLEMENTING", "WAITING", "GATING")
    assert reasons().count("config_recovered") == 1
    assert S.read_state(cfg, cid)["park"] is None



def test_merge_reconcile_mismatch_is_a_minor_finding(opened):
    """SPEC-0267: a reused source_idea id is reported at close, never linked."""
    from nightwright import org_write as OW
    cfg, cid, ctx = opened
    iid, oc = _merge_with_idea(opened)
    OW.set_property(cfg, OW.resolve_selector(cfg, iid), "CREATED", "[2999-01-01 Fri 09:00]")
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert st["state"] == "MERGED"
    (fnd,) = [f for f in S.read_ledger(cfg, cid, types=["finding"])
              if f["title"] == "idea reconcile mismatch"]
    assert fnd["severity"] == "minor" and iid in fnd["detail"]
    assert _idea_state(cfg, iid) == "IDEA"
    assert not list(S.read_ledger(cfg, cid, types=["idea_reconciled"]))


@pytest.mark.parametrize("twin_file", ["ideas-0000.org", "ideas-zzzz.org"])
def test_merge_reconcile_event_describes_the_moved_idea_not_its_duplicate_id_twin(opened, twin_file):
    """SPEC-0267: a rotated org file repeats the id; the event is the pinned idea's own move."""
    cfg, cid, ctx = opened
    iid, oc = _merge_with_idea(opened)
    twin = Path(cfg["org_ideas_file"]).with_name(twin_file)
    twin.write_text("#+TODO: IDEA INCUBATE SPECCED | PROMOTED EXPORTED SHIPPED DROPPED RESEARCHED\n"
                    f"* SPECCED older twin\n:PROPERTIES:\n:ID: {iid}\n"
                    ":CREATED: [2000-01-01 Sat 09:00]\n:END:\n")
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert st["state"] == "MERGED"
    (ev,) = S.read_ledger(cfg, cid, types=["idea_reconciled"])
    assert (ev["idea_id"], ev["from"], ev["to"]) == (iid, "IDEA", "PROMOTED")
    assert "SPECCED older twin" in twin.read_text()         # the twin is untouched


# ---- SPEC-0280: a HANDED_OFF close marks the idea pending its PR ------------------------------

PR_URL = "https://example.test/pull/7"


def _hand_off_with_idea(opened, *, backlink=True):
    """`_merge_with_idea` for an accepted spec, closing HANDED_OFF with a PR. The fixture idea is
    created today and the spec `created` tomorrow, so the SPEC-0267 identity check passes."""
    from nightwright.agentic.close import CloseOutcome
    iid, _merged = _merge_with_idea(opened, spec_status="accepted", backlink=backlink)
    oc = CloseOutcome("HANDED_OFF", close={"mode": "pr", "merged_sha": None, "pr_url": PR_URL,
                                            "gates_seq": 3, "target_sha": "def"},
                      events=[("close", {"mode": "pr"})])
    return iid, oc


def _idea_prop(cfg, iid, name):
    from nightwright import org_write as OW
    return (OW.resolve_selector(cfg, iid).properties.get(name) or "").strip()


def _bookkeeping(cfg, cid):
    return [f for f in S.read_ledger(cfg, cid, types=["finding"]) if f["gate"] == "bookkeeping"]


def test_handed_off_marks_idea_pending(opened):
    from nightwright.agentic import schemas as SC
    cfg, cid, ctx = opened
    iid, oc = _hand_off_with_idea(opened)
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert st["state"] == "HANDED_OFF"
    assert _idea_state(cfg, iid) == "SPECCED" and _idea_prop(cfg, iid, "HANDED_OFF") == PR_URL
    (ev,) = S.read_ledger(cfg, cid, types=["idea_handed_off"])
    assert (ev["spec_id"], ev["idea_id"], ev["pr_url"]) == ("SPEC-0001", iid, PR_URL)
    assert SC.ledger_problems(ev) == []
    assert not _bookkeeping(cfg, cid)


def test_handed_off_bookkeeping_failure_is_minor(opened, monkeypatch):
    from nightwright import org_write as OW
    cfg, cid, ctx = opened
    iid, oc = _hand_off_with_idea(opened)
    real = OW.set_property

    def flaky(c, task, name, value, **kw):
        if name == "HANDED_OFF":
            raise OSError("disk full")
        return real(c, task, name, value, **kw)
    monkeypatch.setattr(OW, "set_property", flaky)
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert st["state"] == "HANDED_OFF" and row["to"] == "HANDED_OFF"
    (fnd,) = _bookkeeping(cfg, cid)
    assert fnd["severity"] == "minor" and "disk full" in fnd["detail"]
    assert not list(S.read_ledger(cfg, cid, types=["idea_handed_off"]))


def test_handed_off_no_claimant_info_finding(opened):
    """The campaign's own idea is not a fallback: without `:SPEC:` pointing at the spec reconcile
    would never advance it, so its mark could never be cleared."""
    cfg, cid, ctx = opened
    iid, oc = _hand_off_with_idea(opened, backlink=False)
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert st["state"] == "HANDED_OFF"
    assert _idea_prop(cfg, iid, "HANDED_OFF") == ""
    titles = {(f["severity"], f["title"]) for f in _bookkeeping(cfg, cid)}
    assert ("info", "no idea to mark handed off") in titles
    assert not list(S.read_ledger(cfg, cid, types=["idea_handed_off"]))


def test_merged_does_not_write_handed_off(opened):
    cfg, cid, ctx = opened
    iid, oc = _merge_with_idea(opened)
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert st["state"] == "MERGED" and _idea_state(cfg, iid) == "PROMOTED"
    assert not list(S.read_ledger(cfg, cid, types=["idea_handed_off"]))
    assert not [t for t in ORG.filter_tasks(ORG.load_tasks(cfg), is_idea=True)
                if t.properties.get("HANDED_OFF")]


def test_requeue_clears_handed_off(opened):
    from nightwright import org_write as OW
    cfg, cid, ctx = opened
    iid, oc = _hand_off_with_idea(opened)
    _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert _idea_prop(cfg, iid, "HANDED_OFF") == PR_URL
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="ABANDONED"))
    state = Q.apply_add(cfg, _requeue_plan(cfg, cid))
    assert _idea_prop(cfg, iid, "HANDED_OFF") == ""
    assert state["id"] != cid and OW.resolve_selector(cfg, iid).state == "SPECCED"


def test_idea_campaign_handed_off_is_not_marked(opened):
    """An idea campaign's PR only adds the spec, so reconcile would never clear the mark."""
    cfg, cid, ctx = opened
    iid, oc = _hand_off_with_idea(opened)
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), kind="idea"))
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert st["state"] == "HANDED_OFF"
    assert _idea_state(cfg, iid) == "SPECCED" and _idea_prop(cfg, iid, "HANDED_OFF") == ""
    assert not list(S.read_ledger(cfg, cid, types=["idea_handed_off"]))


def test_handed_off_skips_a_terminal_claimant(opened, monkeypatch):
    import nightwright.specs as SPM
    from nightwright import explore as EX
    cfg, cid, ctx = opened
    iid, oc = _hand_off_with_idea(opened)
    orig = SPM.reconcile_ideas

    def then_promote(*a, **kw):             # the claimant is terminal by the time it is marked
        out = orig(*a, **kw)
        EX.set_idea_state(cfg, iid, "PROMOTED")
        return out
    monkeypatch.setattr(SPM, "reconcile_ideas", then_promote)
    row, st = _apply(ctx, cfg, cid, "close", close_outcome=oc)
    assert st["state"] == "HANDED_OFF"
    assert not list(S.read_ledger(cfg, cid, types=["idea_handed_off"]))
    assert ("info", "no idea to mark handed off") in {
        (f["severity"], f["title"]) for f in _bookkeeping(cfg, cid)}


def test_requeue_clear_failure_is_a_ledger_note(opened, monkeypatch):
    from nightwright import org_write as OW
    cfg, cid, ctx = opened
    iid, oc = _hand_off_with_idea(opened)
    _apply(ctx, cfg, cid, "close", close_outcome=oc)
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="ABANDONED"))

    def boom(*a, **kw):
        raise OSError("read-only org file")
    monkeypatch.setattr(OW, "clear_property", boom)
    state = Q.apply_add(cfg, _requeue_plan(cfg, cid))
    assert state["id"] != cid
    assert _idea_prop(cfg, iid, "HANDED_OFF") == PR_URL
    notes = [e for e in S.read_ledger(cfg, state["id"], types=["note"])
             if "could not clear :HANDED_OFF:" in e["text"]]
    assert len(notes) == 1 and "read-only org file" in notes[0]["text"]


def _requeue_plan(cfg, cid):
    """A plan for a second campaign on the first one's spec (the first is terminal)."""
    st = S.read_state(cfg, cid)
    return {"campaign": cid + "-b", "spec_id": st["spec_id"], "project": st["project"],
            "profile": st["profile"], "repo_root": st["repo"]["root"], "base": st["repo"]["base"],
            "branch": st["repo"]["branch"] + "-b", "worktree": st["repo"]["worktree"] + "-b",
            "spec_rel_path": st["spec"]["rel_path"], "internal": st["spec"]["internal"]}


def test_render_idea_handed_off_event():
    from nightwright import render as R
    ev = {"seq": 4, "ts": "2026-10-27T10:00:00Z", "type": "idea_handed_off", "by": "queue",
          "spec_id": "SPEC-0001", "idea_id": "IDEA-9", "pr_url": PR_URL}
    out = R.campaign_event(ev).plain
    assert f"idea IDEA-9 handed off → {PR_URL}" in out


def test_plan_add_batch_orders_and_dedupes(tmp_path):                   # SPEC-0276
    from test_agentic_queue_cli import add_spec
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    add_spec(cfg, repo, 2, deps=["SPEC-0001"])
    plan = Q.plan_add_batch(cfg, ["SPEC-0002", "SPEC-0001", " SPEC-0002 "])
    assert [i["id"] for i in plan["items"]] == ["SPEC-0001", "SPEC-0002"]
    assert plan["schema"] == "nw.queue.add.batch.v1" and plan["ok"] == 2
    assert plan["items"][1]["would_block"] == []        # SPEC-0001 is in the batch
    with pytest.raises(ValueError, match="1 of 1 items cannot be queued"):
        Q.apply_add_batch(cfg, {"items": [{"id": "X", "kind": "spec", "error": "e"}]})


def test_on_fail_warn_goes_to_reviewing(opened, tmp_path):
    """SPEC-0289: `on_fail: warn` is `required: false` end to end."""
    from nightwright.agentic import gates as G
    from nightwright.agentic import project as P
    cfg, cid, ctx = opened
    p = tmp_path / "project.yaml"
    p.write_text("schema: nw.project.v1\ngates:\n  - {name: soft, cmd: 'exit 1', on_fail: warn}\n")
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="GATING"))
    run = G.run_all(P.load_project_config(p), S.read_state(cfg, cid), cfg, seq=1)
    assert run.ok and not run.results[0].ok
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=run)
    assert row["to"] == "REVIEWING" and st["last_gate"]["ok"] is True


def test_stop_during_gate_retry_not_triaged(opened, monkeypatch):
    """SPEC-0289 x SPEC-0229: a stop landing in a retry is an interrupted run, not a failure."""
    from nightwright.agentic import gates as G
    cfg, cid, ctx = opened
    real = G.run_gate

    def wrapped(*a, **kw):
        if kw.get("attempt") == 1:
            ctx.stop.set()
            G.terminate_all()
        return real(*a, **kw)
    monkeypatch.setattr(G, "run_gate", wrapped)
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="GATING"))
    proj = {"gates": [{"name": "t", "cmd": "exit 1", "cwd": ".", "timeout_s": 30,
                       "required": True, "retries": 2}]}
    try:
        run = G.run_all(proj, S.read_state(cfg, cid), cfg, seq=1, stop=ctx.stop)
        assert run.interrupted and not run.ok
        row, st = _apply(ctx, cfg, cid, "gate", gate_run=run)
    finally:
        G.reset()
    assert row["to"] != "TRIAGING" and st["state"] == "GATING"


# ---- SPEC-0311: gate timeouts are re-gated, serialized, not triaged -----------------------

def _res(name="test", *, ok=False, timed_out=False, required=True, interrupted=False):
    r = {"name": name, "required": required, "ok": ok, "timed_out": timed_out}
    if interrupted:
        r["interrupted"] = True
    return r


@pytest.mark.parametrize("results,expect", [
    ([_res(timed_out=True)], True),
    ([_res("a", timed_out=True), _res("b", timed_out=True)], True),
    ([_res("a", timed_out=True), _res("b", ok=True)], True),
    ([_res("a", timed_out=True), _res("lint", required=False)], True),    # optional failure ignored
    ([_res("a", timed_out=True), _res("b")], False),                      # mixed: a real failure
    ([_res()], False),
    ([_res(timed_out=True, interrupted=True)], False),
    ([_res(required=False, timed_out=True)], False),                      # only optional failed
    ([_res(ok=True)], False),                                             # green
    ([], False),
])
def test_timeout_only_classification(results, expect):
    assert Q.timeout_only({"results": results}) is expect


def test_gate_timeout_regates_then_parks():
    gate = {"results": [_res("test", timed_out=True)]}
    st = _state("GATING", last_gate=gate)
    for used in (0, 1):
        st["counters"]["timeout_regates"] = used
        step = Q.decide_gate_timeout(st)
        assert (step.kind, step.reason) == ("gate", "gate_timeout_regate")
    st["counters"]["timeout_regates"] = Q.TIMEOUT_REGATE_MAX
    step = Q.decide_gate_timeout(st, {"test": 1200})
    assert (step.kind, step.reason, step.resume_to) == ("park", "gate_timeout", "GATING")
    assert "test (1200s)" in step.detail and "2 gate timeout re-gates" in step.detail
    assert "gate_timeout" in Q.PARK_REASONS


def test_gate_timeout_park_detail_uses_timed_out_gates_limit():
    gate = {"results": [_res("lint"), _res("test", timed_out=True)]}
    st = _state("GATING", last_gate=gate)
    st["counters"]["timeout_regates"] = Q.TIMEOUT_REGATE_MAX
    step = Q.decide_gate_timeout(st, {"lint": 60, "test": 1200})
    assert "test (1200s)" in step.detail and "after 1200s" in step.detail
    assert "lint" not in step.detail


def _gate_run(seq, *results):
    from nightwright.agentic.gates import GateResult, GateRun
    rs = tuple(GateResult(n, req, ok, None if to else (0 if ok else 1), to, 0.1,
                          f"gates/{seq:03d}-{n}.log", "") for n, req, ok, to in results)
    return GateRun(seq, all(r.ok or not r.required for r in rs), rs, "2026-10-25T00:00:00", 0.1)


def _gating(cfg, cid, **counters):
    st = S.read_state(cfg, cid)
    st["state"] = "GATING"
    st["counters"].update(counters)
    S.write_state(cfg, cid, st)


def test_mixed_timeout_and_failure_goes_to_triage(opened):
    cfg, cid, ctx = opened
    _gating(cfg, cid, timeout_regates=1)
    run = _gate_run(1, ("test", True, False, True), ("lint", True, False, False))
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=run)
    assert row["to"] == "TRIAGING" and row["reason"] == "gates_failed"
    assert st["counters"]["timeout_regates"] == 0 and st["counters"]["loops"] == 0


def test_timeout_only_run_skips_triager(opened):
    cfg, cid, ctx = opened
    _gating(cfg, cid)
    run = _gate_run(1, ("test", True, False, True))
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=run)
    assert row["to"] == "GATING" and row["reason"] == "gate_timeout_regate"
    assert all(n["author"] == "queue" for n in S.read_ledger(cfg, cid, types=["note"])
               if "gate timeout" in n["text"])
    c = st["counters"]
    assert (c["loops"], c["gate_retries"], c["triage_retries"], c["timeout_regates"]) == (0, 0, 0, 1)
    assert st["state"] == "GATING" and Q.gate_exclusive(st)
    assert not S.read_ledger(cfg, cid, types=["run.start"]) or all(
        e.get("persona") != "gate_triager" for e in S.read_ledger(cfg, cid, types=["run.start"]))
    gate, = S.read_ledger(cfg, cid, types=["gate"])
    assert gate["results"][0]["timed_out"]
    note = [n for n in S.read_ledger(cfg, cid, types=["note"]) if "gate timeout" in n["text"]]
    assert len(note) == 1 and "test" in note[0]["text"] and "timeout_regates 1/2" in note[0]["text"]
    assert Q.plan_step(st, PROFILE).kind == "gate"


def test_timeout_budget_exhausted_parks_gate_timeout(opened):
    cfg, cid, ctx = opened
    _gating(cfg, cid)
    for n in (1, 2):
        row, st = _apply(ctx, cfg, cid, "gate", gate_run=_gate_run(n, ("test", True, False, True)))
        assert row["to"] == "GATING"
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=_gate_run(3, ("test", True, False, True)))
    assert row["to"] == "PARKED" and row["reason"] == "gate_timeout"
    assert st["park"]["reason"] == "gate_timeout" and st["park"]["resume_to"] == "GATING"
    assert "test" in st["park"]["detail"] and st["counters"]["loops"] == 0
    assert st["park"]["reason"] not in ("loop_exhausted", "triage_escalated")


def test_timeout_regates_reset(opened):
    cfg, cid, ctx = opened
    # green
    _gating(cfg, cid, timeout_regates=2)
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=_gate_run(1, ("test", True, True, False)))
    assert row["to"] == "REVIEWING" and st["counters"]["timeout_regates"] == 0
    assert not Q.gate_exclusive(st)
    # non-timeout failure
    _gating(cfg, cid, timeout_regates=1)
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=_gate_run(2, ("test", True, False, False)))
    assert row["to"] == "TRIAGING" and st["counters"]["timeout_regates"] == 0
    # fix triage
    st = S.read_state(cfg, cid)
    st.update(state="TRIAGING")
    st["counters"]["timeout_regates"] = 1
    S.write_state(cfg, cid, st)
    row, st = _apply(ctx, cfg, cid, "triage", persona="gate_triager", run_id="002-gate_triager",
                     run_result=_rr(_verdict("fix", fix_brief="x")), budget=0.5)
    assert row["to"] == "IMPLEMENTING" and st["counters"]["timeout_regates"] == 0
    # resume from the gate_timeout park: fresh budget, exclusive first gate
    _gating(cfg, cid)
    for n in (3, 4, 5):
        row, st = _apply(ctx, cfg, cid, "gate", gate_run=_gate_run(n, ("test", True, False, True)))
    assert st["state"] == "PARKED" and st["counters"]["timeout_regates"] == 2
    Q.apply_resume(cfg, Q.plan_resume(cfg, cid))
    st = S.read_state(cfg, cid)
    assert st["state"] == "GATING" and st["counters"]["timeout_regates"] == 0
    assert Q.gate_exclusive(st)
    # resume from any other park also starts with a fresh budget
    st = S.read_state(cfg, cid)
    S.write_state(cfg, cid, dict(st, state="PARKED", gate_exclusive=None,
                                 counters=dict(st["counters"], timeout_regates=1),
                                 park={"reason": "loop_exhausted", "detail": "x", "resume_to": "GATING"}))
    Q.apply_resume(cfg, Q.plan_resume(cfg, cid))
    st = S.read_state(cfg, cid)
    assert st["state"] == "GATING" and st["counters"]["timeout_regates"] == 0


def _gs(cid, entered=None, **over):
    st = _state("GATING", id=cid)
    if entered:
        st["gate_exclusive"] = {"entered_at": entered}
    st.update(over)
    return st


def test_timeout_regate_is_serialized():
    a, b = _gs("A", "2026-10-30T10:00:00"), _gs("B")
    # nothing in flight: only the exclusive step starts, the other waits for it
    assert Q.select_gate_starts([b, a], 0, False) == ["A"]
    # a gate in flight: the exclusive step is deferred, and so is everything else
    assert Q.select_gate_starts([b, a], 1, False) == []
    # an exclusive step in flight: no other gate starts
    assert Q.select_gate_starts([b], 1, True) == []
    assert Q.select_gate_starts([b], 0, True) == []
    # no exclusive step anywhere: gates start as before (slot admission is the only limit)
    assert Q.select_gate_starts([b, _gs("C")], 2, False) == ["B", "C"]
    # a deferral changes no campaign state
    before = dict(a)
    Q.select_gate_starts([a], 1, False)
    assert a == before


def test_exclusive_regate_not_starved():
    a, c = _gs("A", "2026-10-30T10:00:00"), _gs("C")
    # B's gate is in flight, A is deferred exclusive, C enters GATING: C may not jump the queue
    assert Q.select_gate_starts([a, c], 1, False) == []
    # B finished: A starts first and alone; C waits behind it
    assert Q.select_gate_starts([a, c], 0, False) == ["A"]
    assert Q.select_gate_starts([c], 1, True) == []
    # two pending exclusives start one at a time in (entered_at, id) order
    late, tie_y, tie_z = (_gs("X", "2026-10-30T10:05:00"), _gs("Y", "2026-10-30T10:00:00"),
                          _gs("Z", "2026-10-30T10:00:00"))
    assert Q.select_gate_starts([tie_z, late, tie_y], 0, False) == ["Y"]
    assert Q.select_gate_starts([tie_z, late], 0, False) == ["Z"]
    assert Q.select_gate_starts([late], 0, False) == ["X"]


def test_gate_starts_counts_flights_and_close_regates(opened):
    from concurrent.futures import Future
    cfg, cid, ctx = opened
    pool = Q.StepPool()

    def fly(state, kind, done=False):
        fut = Future()
        if done:
            fut.set_result(None)
        fl = Q._Flight(state, ctx)
        fl.tasks.append((Q.Step(kind), fut, None))
        pool.flights[state["id"]] = fl

    a, c = _gs("A", "2026-10-30T10:00:00"), _gs("C")
    fly(_state("GATING", id="B"), "gate")
    assert Q._gates_in_flight(ctx, pool) == (1, False)
    assert Q._gate_starts(ctx, pool, [a, c], set(), {"B"}) == set()      # A deferred, C blocked
    pool.flights.clear()
    fly(_state("IMPLEMENTING", id="B"), "implement")                     # non-gate steps ignored
    assert Q._gate_starts(ctx, pool, [a, c], set(), {"B"}) == {"A"}
    pool.flights.clear()
    st = S.read_state(cfg, cid)                                          # a close that re-gates
    fly(dict(st, id="D"), "close")
    assert Q._gates_in_flight(ctx, pool)[0] == 1
    assert Q._gate_starts(ctx, pool, [a], set(), {"D"}) == set()
    pool.flights.clear()
    fly(_state("GATING", id="B", gate_exclusive={"entered_at": "x"}), "gate")
    assert Q._gates_in_flight(ctx, pool) == (1, True)
    pool.flights.clear()
    fly(_state("GATING", id="B"), "gate", done=True)
    assert Q._gates_in_flight(ctx, pool) == (0, False)
    # the deferral note is journaled once per campaign per timeout_regates value
    calls = []
    import nightwright.agentic.queue as QQ
    orig = QQ.journal
    QQ.journal = lambda cfg_, kind, **kw: calls.append((kind, kw))
    try:
        for _ in range(3):
            Q._note_gate_deferred(ctx, pool, a)
        Q._note_gate_deferred(ctx, pool, c)                              # not exclusive: silent
        a["counters"]["timeout_regates"] = 1
        Q._note_gate_deferred(ctx, pool, a)
    finally:
        QQ.journal = orig
    assert [k for k, _ in calls] == ["gate_timeout_deferred"] * 2


def test_tick_defers_gate_steps_around_exclusive_regate(opened, monkeypatch):
    """`_tick` itself applies the serialization (SPEC-0311): with B's gate in flight, the
    exclusive A and the later C are not submitted and their state and ledger stay untouched, while
    D's implementer still is; once B is done only A starts, and C after it."""
    import threading

    from waits import until
    cfg, d_id, _ctx = opened                      # the opened campaign is D (IMPLEMENTING)
    base = S.read_state(cfg, d_id)
    ids = {"D": d_id}
    started = []
    gates = {}

    def clone(name, **over):
        ids[name] = f"{d_id}-{name}"
        S.write_state(cfg, ids[name], dict(base, id=ids[name], state="GATING",
                                           counters=dict(base["counters"]), **over))

    def fake_gate(ctx, state):
        name = next(k for k, v in ids.items() if v == state["id"])
        started.append(name)
        gates[name].wait(10)
        return Q.StepResult(state["id"], Q.Step("gate"), "GATING",
                            gate_run=_gate_run(1, ("test", True, True, False)))

    def fake_persona(ctx, state, step, *a):
        started.append(f"{next(k for k, v in ids.items() if v == state['id'])}:{step.kind}")
        gates["D"].wait(10)
        return Q.StepResult(state["id"], step, state["state"], error="faked")

    def finish(name):
        gates[name].set()
        pool.flights[ids[name]].tasks[0][1].result(10)

    def gate_starts():
        return [x for x in started if ":" not in x]

    def snapshot(*names):
        return {n: (S.read_state(cfg, ids[n]), S.read_ledger(cfg, ids[n])) for n in names}

    gates.update({k: threading.Event() for k in "ABCD"})
    monkeypatch.setattr(Q, "_do_gate", fake_gate)
    monkeypatch.setattr(Q, "_do_persona", fake_persona)
    S.write_state(cfg, d_id, dict(base, state="PARKED", park={"reason": "x", "detail": "",
                                                                "resume_to": "IMPLEMENTING"}))
    pool = Q.StepPool()
    try:
        clone("B")
        Q.tick(cfg, pool=pool)
        assert until(lambda: started == ["B"])       # a step records itself on its worker thread
        clone("A", gate_exclusive={"entered_at": "2026-10-30T10:00:00"})
        clone("C")
        S.write_state(cfg, d_id, base)            # D implements: a non-gate step
        before = snapshot("A", "C")
        Q.tick(cfg, pool=pool)
        # (d) D goes ahead; (a)(b) no gate for A or C
        assert until(lambda: started == ["B", "D:implement"])
        assert set(pool.flights) == {ids["B"], ids["D"]}
        Q.tick(cfg, pool=pool)
        assert started == ["B", "D:implement"] and snapshot("A", "C") == before
        finish("B")
        Q.tick(cfg, pool=pool)                    # (c) B harvested: only the exclusive A starts
        assert until(lambda: gate_starts() == ["B", "A"]) and ids["C"] not in pool.flights
        assert S.read_state(cfg, ids["B"])["state"] == "REVIEWING"
        assert snapshot("C") == {"C": before["C"]}
        Q.tick(cfg, pool=pool)                    # A in flight and exclusive: C still waits
        assert gate_starts() == ["B", "A"]
        finish("A")
        Q.tick(cfg, pool=pool)
        assert until(lambda: gate_starts() == ["B", "A", "C"])
    finally:
        for e in gates.values():
            e.set()
        pool.shutdown()


def test_six_campaigns_timeout_under_concurrency():
    """Fake gate: times out when more than one gate runs at once, passes alone. One round =
    the gates started in a tick run to completion together."""
    states = [_gs(f"C{i}") for i in range(6)]
    clock = iter(f"2026-10-30T10:{m:02d}:00" for m in range(60))
    for _ in range(40):
        gating = [s for s in states if s["state"] == "GATING"]
        if not gating:
            break
        started = set(Q.select_gate_starts(gating, 0, False))
        for s in gating:
            if s["id"] not in started:
                continue
            s["gate_exclusive"] = None
            if len(started) == 1:
                s["state"] = "REVIEWING"
                continue
            s["last_gate"] = {"results": [_res("test", timed_out=True)]}
            assert Q.timeout_only(s["last_gate"])
            step = Q.decide_gate_timeout(s)
            assert step.kind == "gate", step
            s["counters"]["timeout_regates"] = s["counters"].get("timeout_regates", 0) + 1
            s["gate_exclusive"] = {"entered_at": next(clock)}
    assert [s["state"] for s in states] == ["REVIEWING"] * 6
    assert all(s["counters"]["loops"] == 0 for s in states)


def test_e2e_gate_timeout_regates_then_parks_and_resume_regates(tmp_path, monkeypatch):
    """The manual verification of SPEC-0311: a gate that always times out is re-gated twice
    (never triaged, `loops` stays 0), parks `gate_timeout`, and a resume re-gates twice more."""
    repo = make_repo(tmp_path, test_gate="sleep 3")
    yml = repo / ".nw" / "project.yaml"
    yml.write_text(yml.read_text().replace("name: test, cmd: 'sh gates/test.sh', timeout_s: 30",
                                           "name: test, cmd: 'sh gates/test.sh', timeout_s: 1"))
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "short timeout"], check=True)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "gate_timeout", st
    assert st["counters"]["loops"] == 0 and st["counters"]["timeout_regates"] == 2
    assert "test (1s)" in st["park"]["detail"]
    types = [e["type"] for e in S.read_ledger(cfg, cid)]
    assert types.count("gate") == 3
    assert not any(e.get("persona") == "gate_triager" for e in S.read_ledger(cfg, cid, types=["run.start"]))
    assert sum("gate timeout" in n["text"] for n in S.read_ledger(cfg, cid, types=["note"])) == 2
    r = run_cli(cfg, monkeypatch, ["queue", "resume", cid, "--yes"])
    assert r.exit_code == 0, r.output
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "gate_timeout"
    assert st["counters"]["loops"] == 0
    assert [e["type"] for e in S.read_ledger(cfg, cid)].count("gate") == 6


def test_backlog_readiness_outbound_dependency(tmp_path):
    """SPEC-0347: an outbound `PROJ-NNNN` dependency resolves to its `PROJ-NNNN-slug.md` file
    (not the `.contract.md` sidecar); `HANDED_OFF` blocks, `MERGED` or `done` admits."""
    root = tmp_path / "target"
    d = root / "docs" / "specs"
    d.mkdir(parents=True)
    (d / "ACME-0002-x.md").write_text(
        "---\nid: ACME-0002\nstatus: accepted\ndepends_on: [ACME-0003, ACME-0099]\n---\n")
    dep = d / "ACME-0003-y.md"
    dep.write_text("---\nid: ACME-0003\nstatus: accepted\n---\n")
    (d / "ACME-0003-y.contract.md").write_text("---\nstatus: done\n---\n")   # never the spec
    cfg = {"data_dir": str(tmp_path / "data")}
    st = _q("ACME-0002-x", project="acme", repo={"root": str(root)},
            spec={"rel_path": "docs/specs/ACME-0002-x.md"})
    assert Q.backlog_readiness(cfg, st) == (
        False, ["waiting on ACME-0003 (accepted)", "ACME-0099 not found"])
    S.create_campaign(cfg, {"id": "ACME-0003-c1", "spec_id": "ACME-0003", "project": "acme",
                            "state": "HANDED_OFF", "created_at": "2026-11-01T00:00:00"})
    S.create_campaign(cfg, {"id": "ACME-0003-o", "spec_id": "ACME-0003", "project": "other",
                            "state": "MERGED", "created_at": "2026-11-02T00:00:00"})
    assert Q.backlog_readiness(cfg, st) == (False, [
        "waiting on ACME-0003 (accepted; PR open, campaign ACME-0003-c1 handed off)",
        "ACME-0099 not found"])
    S.create_campaign(cfg, {"id": "ACME-0099-c1", "spec_id": "ACME-0099", "project": "acme",
                            "state": "MERGED", "created_at": "2026-11-01T00:00:00"})
    st2 = S.read_state(cfg, "ACME-0003-c1")
    st2["state"] = "MERGED"                                    # its PR merged; the base lags
    S.write_state(cfg, "ACME-0003-c1", st2)
    assert Q.backlog_readiness(cfg, st) == (True, [])
    dep.write_text("---\nid: ACME-0003\nstatus: done\n---\n")
    assert Q.backlog_readiness({}, _q("ACME-0002-x", repo={"root": str(root)},
                                      spec={"rel_path": "docs/specs/ACME-0002-x.md"})) == (
        False, ["ACME-0099 not found"])                         # no campaigns: the file decides


# ---- SPEC-0293: the cross-campaign flaky record ---------------------------------------------------

def _flaky_gate_run(seq, ok, *, failing=(), flaky=()):
    from nightwright.agentic.gates import GateResult, GateRun
    r = GateResult("test", True, ok, 0 if ok else 1, False, 0.1, f"gates/{seq:03d}-test.log", "",
                   summary={"failing": list(failing)} if failing else None, flaky=tuple(flaky))
    return GateRun(seq, ok, (r,), "2026-10-25T00:00:00", 0.1)


def _flaky_doc(cfg):
    from nightwright.agentic import flaky as FL
    return FL._read(cfg)["projects"].get("Meta-Tools", {})


def test_gate_flaky_ids_recorded(opened):
    cfg, cid, ctx = opened
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="GATING"))
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=_flaky_gate_run(1, True, flaky=["t::a"]))
    assert row["to"] == "REVIEWING"
    assert _flaky_doc(cfg)["t::a"]["sources"] == {"rerun": 1}
    (ev,) = S.read_ledger(cfg, cid, types=["flaky_recorded"])
    assert ev["ids"] == ["t::a"] and ev["source"] == "rerun"
    from nightwright.agentic import schemas as SC
    assert SC.ledger_problems(ev) == []


def test_triage_retry_green_records_previous_failing(opened):
    cfg, cid, ctx = opened
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="GATING"))
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=_flaky_gate_run(1, False, failing=["t::b"]))
    assert row["to"] == "TRIAGING" and not _flaky_doc(cfg)
    row, st = _apply(ctx, cfg, cid, "triage", persona="gate_triager", run_id="002-gate_triager",
                     run_result=_rr(_verdict("retry_gate", suspected_flaky=True)), budget=0.5)
    assert row["to"] == "GATING" and st["counters"]["gate_retries"] == 1
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=_flaky_gate_run(2, True))
    assert row["to"] == "REVIEWING"
    assert _flaky_doc(cfg)["t::b"]["sources"] == {"triage_retry": 1}
    (ev,) = S.read_ledger(cfg, cid, types=["flaky_recorded"])
    assert ev["ids"] == ["t::b"] and ev["source"] == "triage_retry"


def _triage(ctx, cfg, cid, kind, n):
    return _apply(ctx, cfg, cid, "triage", persona="gate_triager", run_id=f"{n:03d}-gate_triager",
                  run_result=_rr(_verdict(kind, suspected_flaky=True, fix_brief="human call")),
                  budget=0.5)


def test_retry_gate_still_failing_records_nothing(opened):
    cfg, cid, ctx = opened
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="GATING"))
    _apply(ctx, cfg, cid, "gate", gate_run=_flaky_gate_run(1, False, failing=["t::b"]))
    row, st = _triage(ctx, cfg, cid, "retry_gate", 2)
    assert row["to"] == "GATING"
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=_flaky_gate_run(2, False, failing=["t::b"]))
    assert row["to"] == "TRIAGING" and st["state"] == "TRIAGING"
    assert "t::b" not in _flaky_doc(cfg)
    assert not S.read_ledger(cfg, cid, types=["flaky_recorded"])


def test_resume_after_park_green_is_not_a_triage_retry(opened):
    cfg, cid, ctx = opened
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="GATING"))
    _apply(ctx, cfg, cid, "gate", gate_run=_flaky_gate_run(1, False, failing=["t::b"]))
    _triage(ctx, cfg, cid, "retry_gate", 2)
    _apply(ctx, cfg, cid, "gate", gate_run=_flaky_gate_run(2, False, failing=["t::b"]))
    row, st = _triage(ctx, cfg, cid, "escalate", 3)
    assert st["state"] == "PARKED" and st["park"]["resume_to"] == "GATING"
    assert st["counters"]["gate_retries"] == 1                # the stale counter must not matter
    Q.apply_resume(cfg, Q.plan_resume(cfg, cid))
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=_flaky_gate_run(3, True))
    assert row["to"] == "REVIEWING"
    assert not _flaky_doc(cfg) and not S.read_ledger(cfg, cid, types=["flaky_recorded"])


def test_green_without_retry_records_nothing(opened):
    cfg, cid, ctx = opened
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="GATING"))
    _apply(ctx, cfg, cid, "gate", gate_run=_flaky_gate_run(1, False, failing=["t::b"]))
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="GATING"))
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=_flaky_gate_run(2, True))
    assert row["to"] == "REVIEWING"
    assert not _flaky_doc(cfg) and not S.read_ledger(cfg, cid, types=["flaky_recorded"])


def test_record_failure_is_note_only(opened, monkeypatch):
    from nightwright.agentic import flaky as FL

    def boom(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(FL, "record", boom)
    cfg, cid, ctx = opened
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="GATING"))
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=_flaky_gate_run(1, True, flaky=["t::a"]))
    assert row["to"] == "REVIEWING" and st["state"] == "REVIEWING"
    assert any("could not record flaky tests: disk full" in n["text"]
               for n in S.read_ledger(cfg, cid, types=["note"]))
    assert not S.read_ledger(cfg, cid, types=["flaky_recorded"])


def test_apply_add_open_event_has_code_and_profile_hashes(tmp_path, monkeypatch):    # SPEC-0397
    from nightwright.agentic import codewatch as CW
    from nightwright.agentic.profiles import profile_sha256, resolve_profile
    monkeypatch.setattr(CW, "code_sha", lambda: "c" * 40)
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    with pytest.raises(ValueError, match="nope"):     # the plan's profile error is unchanged
        Q.plan_add(cfg, "SPEC-0001", profile="nope")
    plan = Q.plan_add(cfg, "SPEC-0001")
    want = profile_sha256(resolve_profile(cfg, plan["profile"], project_root=plan["repo_root"]))
    assert plan["profile_sha256"] == want
    Q.apply_add(cfg, plan)
    ev = S.read_ledger(cfg, plan["campaign"], types=["open"])[0]
    assert ev["nw_sha"] == "c" * 40 and ev["profile_sha256"] == want and len(want) == 64
