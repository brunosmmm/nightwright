"""SPEC-0201: reviewer fan-out in REVIEWING — plan/cap, combiner, decisions, `_apply_reviews`,
failure paths in the tick and end-to-end paths with a test-only second reviewer."""
import copy
import dataclasses
import threading
import time

import pytest

from test_agentic_e2e import (impl_done, make_cfg, make_repo, added, review as review_entry,
                              script)
from wt.agentic import personas as PS
from wt.agentic import queue as Q
from wt.agentic import store as S
from wt.agentic.runner import FakeRunner, RunResult

A, B = "conformance_reviewer", "second_reviewer"
B_SETTINGS = {"model": "sonnet", "effort": "low", "max_budget_usd": 1.0,
              "permission_mode": "dontAsk", "read_only": True}
PROFILE2 = {"slots": 2, "loop_max": 3, "campaign_cap_usd": 10.0, "run_timeout_s": 60,
            "reviewers": [A, B],
            "personas": {"implementer": {"max_budget_usd": 4.0},
                         "gate_triager": {"max_budget_usd": 0.5},
                         A: {"max_budget_usd": 1.5}, B: {"max_budget_usd": 1.0}}}
DELTA = {"reviewers": [A, B], "review": {"triager": False}, "personas": {B: B_SETTINGS}}


@pytest.fixture
def second_reviewer(monkeypatch):
    """A test-only second reviewer: the conformance entry under another name."""
    monkeypatch.setitem(PS.PERSONAS, B, dataclasses.replace(PS.PERSONAS[A], name=B))
    monkeypatch.setattr(PS, "REVIEWER_PERSONAS", (A, B))


def _state(state="REVIEWING", **over):
    st = {"id": "SPEC-0001-20261025-abcd", "spec_id": "SPEC-0001", "state": state,
          "counters": {"loops": 0, "runs": 3, "gate_retries": 0, "triage_retries": 0},
          "cost": {"total_usd": 0.0, "cap_usd": 10.0, "by_persona": {}}, "last_gate": None}
    st.update(over)
    return st


def rv(verdict="pass", *severities, brief="do it", score=90):
    return {"verdict": verdict, "score": score, "ac_assessment": [], "fix_brief": brief,
            "findings": [{"severity": s, "title": f"{s} thing", "suggested_fix": "fix it"}
                         for s in severities]}


def entry(v, loop=0, run="002-x"):
    return dict(v, run=run, loop=loop)


# ---- plan_step / cap ---------------------------------------------------------------------------

def test_plan_step_review_lists_pending_reviewers(second_reviewer):
    step = Q.plan_step(_state(), PROFILE2)
    assert (step.kind, step.persona, step.personas) == ("review", A, (A, B))
    st = _state(last_review={"verdicts": {A: entry(rv())}})            # A valid, B pending
    step = Q.plan_step(st, PROFILE2)
    assert (step.persona, step.personas) == (B, (B,))
    st["last_review"]["verdicts"][B] = entry(rv())                       # none pending: full list
    assert Q.plan_step(st, PROFILE2).personas == (A, B)


def test_plan_step_falls_back_without_reviewers_key():
    profile = {k: v for k, v in PROFILE2.items() if k != "reviewers"}
    step = Q.plan_step(_state(), profile)
    assert (step.persona, step.personas) == (A, (A,))
    assert Q.plan_step(_state(), dict(profile, reviewers=[])).personas == (A,)


def test_stale_loop_and_old_shape_do_not_count(second_reviewer):
    st = _state(last_review={"verdicts": {A: entry(rv(), loop=0)}})
    st["counters"]["loops"] = 1                                          # older loop's entry
    assert Q.plan_step(st, PROFILE2).personas == (A, B)
    old = _state(last_review={"run": "002-x", "verdict": "pass", "loop": 0})   # SPEC-0187 shape
    assert Q.plan_step(old, PROFILE2).personas == (A, B)


@pytest.mark.parametrize("spent,pending,parks", [
    (7.5, (A, B), False),          # 7.5 + 1.5 + 1.0 = 10
    (7.51, (A, B), True),
    (8.51, (A, B), True),
    (8.5, (B,), False),            # a retry pass sums only the pending reviewer
])
def test_cap_sums_pending_budgets(second_reviewer, spent, pending, parks):
    last = {"verdicts": {A: entry(rv())}} if pending == (B,) else None
    st = _state(cost={"total_usd": spent, "cap_usd": 10.0, "by_persona": {}}, last_review=last)
    step = Q.plan_step(st, PROFILE2)
    if parks:
        assert step.kind == "park" and step.reason == "cost_cap" and step.resume_to == "REVIEWING"
        assert A in step.detail and B in step.detail and "$2.50" in step.detail
    else:
        assert step.kind == "review" and step.personas == pending


# ---- combine_reviews / decide_reviews ------------------------------------------------------------

def test_combine_reviews_is_gone():
    assert not hasattr(Q, "combine_reviews") and not hasattr(Q, "fixing_reviewers")


def test_decide_reviews_triages_and_builds_the_merged_brief(second_reviewer):
    st = _state()
    step = Q.decide_reviews(st, PROFILE2, {A: rv("fix", "major", brief="do X"),
                                           B: rv("pass", "must_fix", "minor", brief="do Y")})
    assert (step.kind, step.reason) == ("implement", "review_fix")
    brief = step.combined["brief"]
    assert brief["personas"] == [A, B]
    assert brief["fix_brief"] == (f"[{A}] do X\n\n[{B}] do Y\n\n- [major] major thing: Fix: fix it\n"
                                  f"- [must_fix] must_fix thing: Fix: fix it")      # B: pass + must_fix = fix
    assert [m["severity"] for m in brief["must_fix"]] == ["major", "must_fix"]
    both = Q.decide_reviews(st, PROFILE2, {A: rv("pass"), B: rv("pass", "minor")})
    assert both.kind == "close" and both.combined["triage"]["outcome"] == "pass"
    # a fix over only minors does not block (SPEC-0203); score 100 - 4 >= 80
    assert Q.decide_reviews(st, PROFILE2, {A: rv("fix", "minor"), B: rv("pass")}).kind == "close"


def test_decide_reviews_cases(second_reviewer):
    st = _state()
    ok = {A: rv("pass"), B: rv("pass")}
    assert Q.decide_reviews(st, PROFILE2, ok).kind == "close"
    # B missing the first time: retry, sibling kept, counters untouched by the decision
    step = Q.decide_reviews(st, PROFILE2, {A: rv("pass")}, [B])
    assert (step.kind, step.reason, step.missing) == ("none", "review_retry", (B,))
    assert "review_retries" not in st["counters"] and "review_retries_by_persona" not in st["counters"]
    # a valid fix plus a missing sibling does not route
    assert Q.decide_reviews(st, PROFILE2, {A: rv("fix")}, [B]).kind == "none"
    # B out of retries: park names B only, even though A said fix
    st["counters"]["review_retries_by_persona"] = {B: 1}
    step = Q.decide_reviews(st, PROFILE2, {A: rv("fix")}, [B])
    assert step.kind == "park" and step.reason == "review_escalated"
    assert step.escalated == (B,) and B in step.detail and A not in step.detail
    assert step.resume_to == "REVIEWING"
    # mixed: A missing for the second time, B for the first: park naming A only
    st["counters"]["review_retries_by_persona"] = {A: 1}
    step = Q.decide_reviews(st, PROFILE2, {}, [A, B])
    assert step.kind == "park" and step.escalated == (A,) and step.missing == (A, B)
    # loop_exhausted carries the merged brief
    st = _state()
    st["counters"]["loops"] = 3
    step = Q.decide_reviews(st, PROFILE2, {A: rv("fix", "major", brief="bad"), B: rv("pass")})
    assert step.reason == "loop_exhausted" and "[%s] bad" % A in step.detail


def test_legacy_int_applies_to_a_single_reviewer_only(second_reviewer):
    st = _state()
    st["counters"]["review_retries"] = 1
    solo = dict(PROFILE2, reviewers=[A])
    assert Q.decide_reviews(st, solo, {}, [A]).kind == "park"
    assert Q.decide_review(st, PROFILE2, None, False).kind == "park"      # wrapper: one reviewer
    assert Q.decide_reviews(st, PROFILE2, {A: rv()}, [B]).kind == "none"   # ignored with two


# ---- entry into REVIEWING ------------------------------------------------------------------------

@pytest.fixture
def opened2(tmp_path, monkeypatch, second_reviewer):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()] * 6), profile=DELTA)
    cid = added(cfg, monkeypatch)
    ctx = Q._Ctx(cfg, runner=FakeRunner(script(tmp_path, [impl_done()] * 6, "s2.json")))
    Q.tick(cfg, runner=ctx.runner)
    st = S.read_state(cfg, cid)
    assert st["state"] == "IMPLEMENTING"
    return cfg, cid, ctx


def _enter(cfg, cid, frm, **over):
    st = S.read_state(cfg, cid)
    st.update(state=frm, **over)
    st["counters"]["runs"] = 7
    return Q._transition(cfg, st, "REVIEWING", "test")


def test_entering_reviewing_clears_the_previous_review(opened2):
    cfg, cid, _ = opened2
    stale = {"verdicts": {A: entry(rv())}, "triage": {"x": 1}, "waived": [1], "loop": 0}
    st = _enter(cfg, cid, "CLOSING", last_review=stale, counters=dict(
        S.read_state(cfg, cid)["counters"], review_retries=1,
        review_retries_by_persona={A: 1}))
    assert "verdicts" not in st["last_review"] and "triage" not in st["last_review"]
    assert st["last_review"]["entry_run"] == 7 and st["last_review"]["loop"] == 0
    assert st["counters"]["review_retries"] == 0
    assert "review_retries_by_persona" not in st["counters"]
    assert S.read_state(cfg, cid)["last_review"]["entry_run"] == 7


def test_resume_and_waiting_release_clear_verdicts(opened2):
    cfg, cid, ctx = opened2
    stale = {"verdicts": {A: entry(rv())}}
    st = S.read_state(cfg, cid)
    st.update(state="PARKED", last_review=stale, park={"reason": "review_escalated",
                                                       "resume_to": "REVIEWING"})
    S.write_state(cfg, cid, st)
    Q.apply_resume(cfg, Q.plan_resume(cfg, cid))
    st = S.read_state(cfg, cid)
    assert st["state"] == "REVIEWING" and "verdicts" not in st["last_review"]
    assert "entry_run" in st["last_review"]
    # released from WAITING
    st.update(state="WAITING", waiting={"resume_to": "REVIEWING"},
              last_review={"verdicts": {A: entry(rv())}})
    S.write_state(cfg, cid, st)
    Q.tick(cfg, runner=ctx.runner, admit=True)
    assert "verdicts" not in (S.read_state(cfg, cid).get("last_review") or {})


def test_self_transition_keeps_the_review(opened2):
    cfg, cid, _ = opened2
    st = _enter(cfg, cid, "GATING")
    st["last_review"]["verdicts"] = {A: entry(rv())}
    Q._transition(cfg, st, "REVIEWING", "review_retry")
    assert S.read_state(cfg, cid)["last_review"]["verdicts"]


# ---- _apply_reviews ------------------------------------------------------------------------------

def _rr(verdict, *, ok=True, cost=0.25, exit_code=0, interrupted=False, timed_out=False):
    return RunResult(exit_code=exit_code, timed_out=timed_out, interrupted=interrupted,
                     duration_s=0.1, is_error=exit_code != 0, cost_usd=cost, usage={},
                     session_id="s", verdict=verdict, verdict_ok=ok, result_text="", raw_path=None)


def apply_reviews(ctx, cfg, cid, outcomes):
    """outcomes: [(persona, RunResult | None)] in the order the results arrive."""
    st = S.read_state(cfg, cid)
    reviews = [Q.StepResult(cid, Q.Step("review", persona=p), st["state"],
                            run_id=None if rr is None else f"{n:03d}-{p}", run_result=rr,
                            budget=PROFILE2["personas"][p]["max_budget_usd"],
                            error=None if rr is not None else "boom")
               for n, (p, rr) in enumerate(outcomes, 2)]
    res = Q.StepResult(cid, Q.Step("review", persona=outcomes[0][0]), st["state"], reviews=reviews)
    row = Q._apply(ctx, st, res)
    return row, S.read_state(cfg, cid)


def _to_reviewing(cfg, cid, **counters):
    st = S.read_state(cfg, cid)
    st["counters"].update(counters)
    st["state"] = "REVIEWING"
    S.write_state(cfg, cid, st)


def test_apply_reviews_records_in_given_order_and_shapes_last_review(opened2):
    cfg, cid, ctx = opened2
    _to_reviewing(cfg, cid, review_retries=1)
    v = rv("fix", "must_fix", brief="fix A")
    v["findings"][0].update(ac="AC1", detail="d", files=["a.py"])
    row, st = apply_reviews(ctx, cfg, cid, [(A, _rr(v)), (B, _rr(rv("pass", "minor", score=70)))])
    assert row["to"] == "IMPLEMENTING" and row["reason"] == "review_fix"
    assert row["run_id"] == f"003-{B}" and row["cost_usd"] == 0.5
    lr = st["last_review"]
    assert (lr["run"], lr["runs"]) == (f"003-{B}", {A: f"002-{A}", B: f"003-{B}"})
    assert (lr["verdict"], lr["score"], lr["findings"], lr["must_fix"], lr["loop"]) == (
        "fix", 0, 2, 1, 0)
    assert lr["verdicts"][A]["findings"] == v["findings"] and lr["verdicts"][A]["loop"] == 0
    assert st["pending_brief"] == {
        "source": "review", "personas": [A],
        "fix_brief": f"[{A}] fix A\n\n- [must_fix] must_fix thing (a.py): d\nFix: fix it",
        "must_fix": [{"ac": "AC1", "title": "must_fix thing", "detail": "d\nFix: fix it", "files": ["a.py"],
                      "id": f"{A}#0", "severity": "must_fix", "personas": [A]}],
        "human_notes": []}
    assert [a["id"] for a in lr["advisory"]] == [f"{B}#0"] and lr["waived"] == []
    notes = [n["text"] for n in S.read_ledger(cfg, cid, types=["note"])]
    assert "review score 0/80 fix" in notes and not any("pass over major" in n for n in notes)
    assert st["counters"]["loops"] == 1 and st["counters"]["review_retries"] == 0
    assert "review_retries_by_persona" not in st["counters"]
    assert st["last_verdict"]["persona"] == B
    assert [e["persona"] for e in S.read_ledger(cfg, cid, types=["run.end"])] == [A, B]


def test_apply_reviews_pass_resets_counters_and_closes(opened2):
    cfg, cid, ctx = opened2
    _to_reviewing(cfg, cid, review_retries=1, review_retries_by_persona={B: 1})
    row, st = apply_reviews(ctx, cfg, cid, [(A, _rr(rv())), (B, _rr(rv()))])
    assert (row["to"], row["reason"]) == ("CLOSING", "review_pass")
    assert st["counters"]["review_retries"] == 0
    assert "review_retries_by_persona" not in st["counters"]


def test_missing_reviewer_retries_once_keeping_the_sibling(opened2):
    cfg, cid, ctx = opened2
    _to_reviewing(cfg, cid)
    row, st = apply_reviews(ctx, cfg, cid, [(A, _rr(rv("fix"))), (B, _rr(None, ok=False))])
    assert (row["to"], row["reason"]) == ("REVIEWING", "review_retry")
    assert st["counters"]["review_retries"] == 1
    assert st["counters"]["review_retries_by_persona"] == {B: 1}
    assert list(st["last_review"]["verdicts"]) == [A] and "verdict" not in st["last_review"]
    assert st["pending_brief"] is None or st["counters"]["loops"] == 0
    step = Q.plan_step(st, ctx.profile(st))
    assert step.personas == (B,)
    # B valid on the retry pass: one merged brief, A not re-run
    row, st = apply_reviews(ctx, cfg, cid, [(B, _rr(rv("pass")))])
    assert row["reason"] == "review_fix" and st["pending_brief"]["personas"] == [A]
    assert st["last_review"]["run"] == f"002-{B}"
    # then B malformed twice parks naming B
    _to_reviewing(cfg, cid)
    st = S.read_state(cfg, cid)
    st["last_review"] = {"verdicts": {A: entry(rv("fix"), loop=st["counters"]["loops"])}}
    st["counters"]["review_retries_by_persona"] = {B: 1}
    S.write_state(cfg, cid, st)
    row, st = apply_reviews(ctx, cfg, cid, [(B, _rr(None, ok=False))])
    assert row["to"] == "PARKED" and st["park"]["reason"] == "review_escalated"
    assert B in st["park"]["detail"] and A not in st["park"]["detail"]


def test_hard_failure_is_missing_and_sibling_costs_are_recorded(opened2):
    cfg, cid, ctx = opened2
    _to_reviewing(cfg, cid)
    timed_out = _rr(None, ok=False, cost=None, exit_code=-1, timed_out=True)
    row, st = apply_reviews(ctx, cfg, cid, [(A, _rr(rv("pass"))), (B, timed_out)])
    assert row["reason"] == "review_retry"
    costs = {e["persona"]: e["delta_usd"] for e in S.read_ledger(cfg, cid, types=["cost"])}
    assert costs == {A: 0.25, B: 1.0}          # the hard failure is charged its budget
    assert st["cost"]["total_usd"] == 1.25


def test_prepare_failure_leaves_no_run_start_only_a_note(opened2):
    cfg, cid, ctx = opened2
    _to_reviewing(cfg, cid)
    row, st = apply_reviews(ctx, cfg, cid, [(A, _rr(rv("pass"))), (B, None)])
    assert row["reason"] == "review_retry" and st["counters"]["review_retries_by_persona"] == {B: 1}
    assert [e["persona"] for e in S.read_ledger(cfg, cid, types=["run.end"])] == [A]
    notes = S.read_ledger(cfg, cid, types=["note"])
    assert any("boom" in n["text"] and B in n["text"] for n in notes)


def test_config_error_parks_after_recording_siblings(opened2):
    cfg, cid, ctx = opened2
    _to_reviewing(cfg, cid)
    st = S.read_state(cfg, cid)
    reviews = [Q.StepResult(cid, Q.Step("review", persona=A), "REVIEWING", run_id=f"002-{A}",
                            run_result=_rr(rv("pass")), budget=1.5),
               Q.StepResult(cid, Q.Step("review", persona=B), "REVIEWING", error="bad yaml",
                            config_error=True)]
    row = Q._apply(ctx, st, Q.StepResult(cid, Q.Step("review", persona=A), "REVIEWING",
                                         reviews=reviews))
    st = S.read_state(cfg, cid)
    assert row["to"] == "PARKED" and st["park"]["reason"] == "config_invalid"
    assert st["park"]["resume_to"] == "REVIEWING"
    assert [e["persona"] for e in S.read_ledger(cfg, cid, types=["cost"])] == [A]


def test_interrupted_pass_stores_valid_verdicts_and_decides_nothing(opened2):
    cfg, cid, ctx = opened2
    _to_reviewing(cfg, cid)
    row, st = apply_reviews(ctx, cfg, cid, [(A, _rr(rv("fix"))),
                                            (B, _rr(None, ok=False, interrupted=True))])
    assert (row["to"], row["reason"]) == ("REVIEWING", "interrupted")
    assert list(st["last_review"]["verdicts"]) == [A]
    assert st["counters"]["review_retries"] == 0 and st["counters"]["loops"] == 0
    assert "review_retries_by_persona" not in st["counters"]


def test_single_reviewer_interrupted_leaves_state(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()] * 3))
    cid = added(cfg, monkeypatch)
    Q.tick(cfg)
    ctx = Q._Ctx(cfg)
    st = S.read_state(cfg, cid)
    st["state"] = "REVIEWING"
    S.write_state(cfg, cid, st)
    r = Q.StepResult(cid, Q.Step("review", persona=A), "REVIEWING", run_id=f"002-{A}",
                     run_result=_rr(rv("pass")), budget=1.5, interrupted=True)
    row = Q._apply(ctx, st, r)
    st = S.read_state(cfg, cid)
    assert row["reason"] == "interrupted" and st["state"] == "REVIEWING"
    assert "verdicts" not in (st.get("last_review") or {})


def test_previous_runs_are_filtered_by_entry_run(opened2):
    cfg, cid, _ = opened2
    for n, p in enumerate(["implementer", A, B, "implementer", A, B], 1):
        S.append_event(cfg, cid, "run.end", {
            "persona": p, "run_id": f"{n:03d}-{p}", "exit_code": 0, "is_error": False,
            "timed_out": False, "interrupted": False, "duration_s": 0.0, "cost_usd": 0.0,
            "charged_usd": 0.0, "usage": {}, "session_id": None, "verdict_ok": True,
            "verdict": None, "result_tail": ""})
    st = S.read_state(cfg, cid)
    st["last_review"] = {"entry_run": 4}
    ids = lambda p, s: [e["run_id"] for e in Q._previous_runs(cfg, s, p)]      # noqa: E731
    assert ids(A, st) == [f"003-{B}", "004-implementer"]       # runs 5, 6 are this pass-through
    assert ids("implementer", st) == [f"005-{A}", f"006-{B}"]  # unchanged for non-reviewers
    assert ids(A, dict(st, last_review={})) == [f"005-{A}", f"006-{B}"]   # no entry_run: as today


# ---- end to end (tick loop, scripted per-persona runner) -----------------------------------------

class PersonaRunner(FakeRunner):
    """FakeRunner whose script is a `{persona: [entry, ...]}` map consumed per persona under a
    lock, so concurrent reviewers cannot race on the shared cursor. An entry may carry `raise`
    (the worker raises), `stop` (sets the tick's stop event) and `sleep_s`; `barrier` makes
    every reviewer wait for its siblings."""

    def __init__(self, scripts, barrier=None, stop=None):
        self.scripts = {p: list(v) for p, v in scripts.items()}
        self.lock = threading.Lock()
        self.barrier, self.stop, self.calls, self.done = barrier, stop, [], set()

    def _next_entry(self, req):
        with self.lock:
            self.calls.append(req.persona)
            q = self.scripts.get(req.persona)
            if not q:
                raise ValueError(f"script exhausted for {req.persona}")
            return q.pop(0)

    def run(self, req):
        if self.barrier is not None and req.persona in (A, B):
            self.barrier.wait(timeout=10)
        with self.lock:
            peek = (self.scripts.get(req.persona) or [{}])[0]
        if peek.get("raise"):
            self._next_entry(req)
            raise RuntimeError("worker blew up")
        if peek.get("stop"):             # interrupt once the sibling reviewer has finished
            deadline = time.monotonic() + 10
            while A not in self.done and time.monotonic() < deadline:
                time.sleep(0.01)
            self.stop.set()
        result = super().run(req)
        self.done.add(req.persona)
        return result


def b_entry(verdict="pass", **kw):
    return dict(review_entry(verdict), persona=B, **kw)


def drive(cfg, runner, *, stop=None, ticks=12):
    reports = []
    for _ in range(ticks):
        rep = Q.tick(cfg, runner=runner, stop=stop)
        reports.append(rep)
        states = {s["state"] for s in S.list_campaigns(cfg)}
        if states <= {"MERGED", "PARKED", "ABANDONED", "HANDED_OFF"}:
            break
    return reports


@pytest.fixture
def e2e(tmp_path, monkeypatch, second_reviewer):
    def build(scripts, profile=None, **kw):
        repo = make_repo(tmp_path)
        delta = dict(DELTA, **(profile or {}))
        cfg = make_cfg(tmp_path, repo, script(tmp_path, []), profile=delta)
        cid = added(cfg, monkeypatch)
        return cfg, cid, repo, PersonaRunner(scripts, **kw)
    return build


def starts(cfg, cid):
    return [e["run_id"] for e in S.read_ledger(cfg, cid, types=["run.start"])]


def test_e2e_a_pass_b_fix_then_both_pass(e2e):
    cfg, cid, repo, runner = e2e({
        "implementer": [impl_done(), impl_done(content="better\n")],
        A: [review_entry("pass"), review_entry("pass")],
        B: [b_entry("fix", sleep_s=0.3), b_entry("pass")]})            # B finishes last
    drive(cfg, runner)
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st
    assert starts(cfg, cid) == ["001-implementer", f"002-{A}", f"003-{B}", "004-implementer",
                                f"005-{A}", f"006-{B}"]
    ends = [e["persona"] for e in S.read_ledger(cfg, cid, types=["run.end"])]
    assert ends == ["implementer", A, B, "implementer", A, B]       # profile order, not finish order
    prompt = (S.runs_dir(cfg, cid) / "004-implementer" / "prompt.md").read_text()
    assert "From: review" in prompt and f"[{B}] rewrite feature.txt" in prompt
    assert st["last_review"]["verdict"] == "pass" and st["last_review"]["loop"] == 1
    # the reviewers of loop 1 saw loop 0's runs but not their own pass-through
    again = (S.runs_dir(cfg, cid) / f"005-{A}" / "prompt.md").read_text()
    assert "004-implementer" in again and f"003-{B}" in again and f"006-{B}" not in again


@pytest.mark.parametrize("slow", [A, B])
def test_e2e_event_order_is_independent_of_finish_order(e2e, slow):
    scripts = {"implementer": [impl_done()], A: [review_entry("pass")], B: [b_entry("pass")]}
    scripts[slow][0]["sleep_s"] = 0.3
    cfg, cid, repo, runner = e2e(scripts)
    drive(cfg, runner)
    assert S.read_state(cfg, cid)["state"] == "MERGED"
    assert [e["run_id"] for e in S.read_ledger(cfg, cid, types=["run.end"])] == [
        "001-implementer", f"002-{A}", f"003-{B}"]
    assert [e["persona"] for e in S.read_ledger(cfg, cid, types=["cost"])][1:] == [A, B]


def test_e2e_reviewers_run_concurrently_with_one_slot(e2e):
    barrier = threading.Barrier(2)
    cfg, cid, repo, runner = e2e({"implementer": [impl_done()], A: [review_entry("pass")],
                                  B: [b_entry("pass")]}, profile={"slots": 1}, barrier=barrier)
    drive(cfg, runner)
    assert S.read_state(cfg, cid)["state"] == "MERGED"      # both met at the barrier: in flight at once


def test_e2e_b_malformed_twice_escalates_without_rerunning_a(e2e):
    bad = b_entry("pass", malformed=True)
    cfg, cid, repo, runner = e2e({"implementer": [impl_done()], A: [review_entry("pass")],
                                  B: [bad, bad]})
    drive(cfg, runner)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "review_escalated"
    assert B in st["park"]["detail"] and A not in st["park"]["detail"]
    assert runner.calls.count(A) == 1 and runner.calls.count(B) == 2
    assert st["counters"]["review_retries_by_persona"] == {B: 1}


def test_e2e_fix_until_loop_max(e2e):
    cfg, cid, repo, runner = e2e({"implementer": [impl_done(), impl_done()],
                                  A: [review_entry("fix"), review_entry("fix")],
                                  B: [b_entry("pass"), b_entry("pass")]}, profile={"loop_max": 1})
    drive(cfg, runner)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "loop_exhausted"
    assert f"[{A}]" in st["park"]["detail"]


def test_e2e_fix_plus_malformed_sibling_waits_then_one_brief(e2e):
    cfg, cid, repo, runner = e2e({
        "implementer": [impl_done(), impl_done(content="better\n")],
        A: [review_entry("fix"), review_entry("pass")],
        B: [b_entry("pass", malformed=True), b_entry("pass"), b_entry("pass")]})
    drive(cfg, runner)
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st
    assert runner.calls.count("implementer") == 2
    assert runner.calls.count(A) == 2           # once per review round: never re-run on B's retry
    prompt = (S.runs_dir(cfg, cid) / "005-implementer" / "prompt.md").read_text()
    assert f"[{A}]" in prompt
    # B's retry pass (run 004) did not see the first pass' runs (002, 003)
    retry = (S.runs_dir(cfg, cid) / f"004-{B}" / "prompt.md").read_text()
    assert f"002-{A}" not in retry and f"003-{B}" not in retry


def test_e2e_worker_exception_charges_budget_and_is_retried_alone(e2e):
    cfg, cid, repo, runner = e2e({"implementer": [impl_done()], A: [review_entry("pass")],
                                  B: [b_entry("pass", **{"raise": True}), b_entry("pass")]})
    reports = drive(cfg, runner)
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st
    assert any("worker blew up" in e for r in reports for e in r.errors)
    ends = [e for e in S.read_ledger(cfg, cid, types=["run.end"]) if e["persona"] == B]
    assert len(ends) == 2 and ends[0]["is_error"] and ends[0]["verdict_ok"] is False
    assert ends[0]["charged_usd"] == 1.0 and runner.calls.count(A) == 1


def test_e2e_prepare_failure_for_second_reviewer_leaves_no_trace(e2e, monkeypatch):
    cfg, cid, repo, runner = e2e({"implementer": [impl_done()], A: [review_entry("pass")],
                                  B: [b_entry("pass")]})
    real, fails = Q._prepare_run, []

    def flaky(ctx, state, persona, step=None):
        if persona == B and not fails:
            fails.append(1)
            raise ValueError("cannot build request")
        return real(ctx, state, persona, step)
    monkeypatch.setattr(Q, "_prepare_run", flaky)
    drive(cfg, runner)
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st
    assert any("cannot build request" in n["text"] for n in S.read_ledger(cfg, cid, types=["note"]))
    assert runner.calls.count(A) == 1 and runner.calls.count(B) == 1   # B ran once: the retry
    assert starts(cfg, cid) == ["001-implementer", f"002-{A}", f"003-{B}"]   # no id burnt


def test_e2e_config_error_of_one_reviewer_parks_config_invalid(e2e, monkeypatch):
    cfg, cid, repo, runner = e2e({"implementer": [impl_done()], A: [review_entry("pass")],
                                  B: [b_entry("pass")]})
    real = Q._prepare_run

    def broken(ctx, state, persona, step=None):
        if persona == B:
            raise Q.ProjectConfigError("yaml broke")
        return real(ctx, state, persona, step)
    monkeypatch.setattr(Q, "_prepare_run", broken)
    # the interrupted/prepare precedence: config_error parks, A's cost recorded
    drive(cfg, runner, ticks=4)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "config_invalid"
    assert [e["persona"] for e in S.read_ledger(cfg, cid, types=["cost"])
            if e["persona"] == A] == [A]


def test_e2e_interrupted_pass_keeps_state_and_respawns_only_the_missing(e2e):
    stop = threading.Event()
    cfg, cid, repo, runner = e2e({"implementer": [impl_done()], A: [review_entry("pass")],
                                  B: [b_entry("pass", stop=True), b_entry("pass")]}, stop=stop)
    # the implementer and gate ticks first, then the review tick is interrupted by B's run
    for _ in range(8):
        Q.tick(cfg, runner=runner, stop=stop)
        if S.read_state(cfg, cid)["state"] == "REVIEWING" and stop.is_set():
            break
    st = S.read_state(cfg, cid)
    assert st["state"] == "REVIEWING" and stop.is_set()
    assert list(st["last_review"]["verdicts"]) == [A]
    assert st["counters"]["review_retries"] == 0
    stop.clear()
    drive(cfg, runner)
    assert S.read_state(cfg, cid)["state"] == "MERGED"
    assert runner.calls.count(A) == 1 and runner.calls.count(B) == 2


def test_e2e_single_reviewer_error_still_parks_open_failed(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    cid = added(cfg, monkeypatch)
    runner = PersonaRunner({"implementer": [impl_done()], A: [review_entry("pass", )]})
    runner.scripts[A][0]["raise"] = True
    drive(cfg, runner)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "open_failed"
    assert not [e for e in S.read_ledger(cfg, cid, types=["run.end"]) if e["persona"] == A]


def test_precedence_interrupted_beats_config_error_beats_missing(opened2):
    cfg, cid, ctx = opened2
    _to_reviewing(cfg, cid)
    st = S.read_state(cfg, cid)
    bad = Q.StepResult(cid, Q.Step("review", persona=B), "REVIEWING", error="x", config_error=True)
    ok = Q.StepResult(cid, Q.Step("review", persona=A), "REVIEWING", run_id=f"002-{A}",
                      run_result=_rr(rv("pass"), interrupted=True), budget=1.5)
    row = Q._apply(ctx, st, Q.StepResult(cid, Q.Step("review", persona=A), "REVIEWING",
                                         reviews=[ok, bad]))
    assert row["reason"] == "interrupted" and S.read_state(cfg, cid)["state"] == "REVIEWING"


# ---- SPEC-0203: score notes and routing through _apply_reviews --------------------------------

def _notes(cfg, cid):
    return [n["text"] for n in S.read_ledger(cfg, cid, types=["note"])]


def test_apply_reviews_pass_over_major_notes_and_records_advisory(opened2):
    cfg, cid, ctx = opened2
    _to_reviewing(cfg, cid)
    row, st = apply_reviews(ctx, cfg, cid, [(A, _rr(rv("pass", "major"))), (B, _rr(rv("pass")))])
    assert (row["to"], row["reason"]) == ("CLOSING", "review_pass")
    lr = st["last_review"]
    assert (lr["verdict"], lr["score"], lr["findings"], lr["must_fix"]) == ("pass", 85, 1, 0)
    assert [(a["id"], a["severity"]) for a in lr["advisory"]] == [(f"{A}#0", "major")]
    notes = _notes(cfg, cid)
    assert "review score 85/80 pass" in notes
    assert f"pass over major: {A}#0 major thing" in notes


def test_apply_reviews_minor_fix_passes_without_a_loop(opened2):
    cfg, cid, ctx = opened2
    _to_reviewing(cfg, cid)
    row, st = apply_reviews(ctx, cfg, cid, [(A, _rr(rv("fix", "minor"))), (B, _rr(rv("pass")))])
    assert (row["to"], row["reason"]) == ("CLOSING", "review_pass")
    assert st["counters"]["loops"] == 0
    assert not any(n.startswith("pass over major") for n in _notes(cfg, cid))


def test_apply_reviews_score_only_fix_brief(opened2):
    cfg, cid, ctx = opened2
    _to_reviewing(cfg, cid)
    row, st = apply_reviews(ctx, cfg, cid, [(A, _rr(rv("pass", *["minor"] * 6))),
                                            (B, _rr(rv("pass")))])
    assert (row["to"], row["reason"]) == ("IMPLEMENTING", "review_fix")
    assert st["last_review"]["score"] == 76 and "review score 76/80 fix" in _notes(cfg, cid)
    pb = st["pending_brief"]
    assert pb["source"] == "review" and pb["personas"] == [A]
    assert pb["fix_brief"].startswith("Review score 76 is below the pass score 80")
    assert len(pb["must_fix"]) == 6 and st["counters"]["loops"] == 1


def test_apply_reviews_loop_exhausted_still_notes_the_score(opened2):
    cfg, cid, ctx = opened2
    _to_reviewing(cfg, cid, loops=3)
    row, st = apply_reviews(ctx, cfg, cid, [(A, _rr(rv("fix", "major", brief="bad"))),
                                            (B, _rr(rv("pass")))])
    assert st["state"] == "PARKED" and st["park"]["reason"] == "loop_exhausted"
    assert "bad" in st["park"]["detail"]
    assert "review score 85/80 fix" in _notes(cfg, cid)


def test_no_score_note_for_a_retry_or_escalation(opened2):
    cfg, cid, ctx = opened2
    _to_reviewing(cfg, cid)
    apply_reviews(ctx, cfg, cid, [(A, _rr(rv("pass"))), (B, _rr(None, ok=False))])
    assert not any(n.startswith("review score") for n in _notes(cfg, cid))
    st = S.read_state(cfg, cid)
    st["counters"]["review_retries_by_persona"] = {B: 1}
    S.write_state(cfg, cid, st)
    row, st = apply_reviews(ctx, cfg, cid, [(B, _rr(None, ok=False))])
    assert st["park"]["reason"] == "review_escalated"
    assert not any(n.startswith("review score") for n in _notes(cfg, cid))
