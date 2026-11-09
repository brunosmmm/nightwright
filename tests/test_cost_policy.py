"""SPEC-0422: the cost-cap policy's pure functions — settings, reserves, convergence, decide."""
import json

import pytest

from nightwright.agentic import cost_policy as CP
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S

pytestmark = [pytest.mark.fast, pytest.mark.usefixtures('cost_policy_on')]

PROFILE = {"name": "balanced", "campaign_cap_usd": 15.0, "run_timeout_s": 60,
           "personas": {"implementer": {"model": "sonnet", "max_budget_usd": 4.0},
                        "gate_triager": {"model": "sonnet", "max_budget_usd": 0.5},
                        "conformance_reviewer": {"model": "opus", "max_budget_usd": 1.5}}}


def _st(total=14.0, cap=15.0, **counters):
    return {"id": "X-1", "state": "IMPLEMENTING", "counters": {"loops": 1, **counters},
            "cost": {"total_usd": total, "cap_usd": cap, "by_persona": {}}}


def _out(seq, score, outcome="fix", major=0, must_fix=0, pass_score=80):
    return {"type": "review_outcome", "seq": seq, "loop": seq, "score": score,
            "pass_score": pass_score, "outcome": outcome, "major": major, "must_fix": must_fix}


def _cfg(**pol):
    return {"agentic": {"cost_cap_policy": pol}} if pol else {}


# ---- settings ------------------------------------------------------------------------------

def test_settings_defaults_equal_the_humans_decision():
    s = CP.settings({})
    assert s == {"enabled": True, "ceiling_multiplier": 3.0, "step_fraction": 0.5,
                 "no_review_raises": 1,
                 "reserve": {"multiplier": 1.5, "floor_usd": 0.25, "min_samples": 8, "window": 40}}
    assert CP.ceiling(PROFILE, s) == 45.0
    assert CP.ceiling({**PROFILE, "campaign_cap_usd": 30.0}, s) == 90.0


def test_settings_bad_values_fall_back_per_key():
    s = CP.settings(_cfg(enabled="yes", ceiling_multiplier=0.5, step_fraction=-1,
                         no_review_raises=True,
                         reserve={"multiplier": "x", "floor_usd": float("nan"), "min_samples": 0,
                                  "window": 12}))
    assert s["enabled"] is True and s["ceiling_multiplier"] == 3.0 and s["step_fraction"] == 0.5
    assert s["no_review_raises"] == 1
    assert s["reserve"] == {"multiplier": 1.5, "floor_usd": 0.25, "min_samples": 8, "window": 12}
    assert CP.settings({"agentic": {"cost_cap_policy": "on"}}) == CP.settings({})
    assert CP.settings(_cfg(enabled=False, ceiling_multiplier=2))["ceiling_multiplier"] == 2.0
    assert CP.ceiling(PROFILE, CP.settings(_cfg(enabled=False))) is None


# ---- reserves (AC3) ------------------------------------------------------------------------

def _stats(**runs):
    return {"runs": {CP.key(*k.split("__")): v for k, v in runs.items()}}


def test_reserve_median_floor_budget_and_min_samples():
    sett = CP.settings({})
    stats = _stats(implementer__sonnet=[0.4, 0.5, 0.6, 0.5, 0.52, 0.3, 0.9, 0.55],
                   gate_triager__sonnet=[0.01] * 9,
                   conformance_reviewer__opus=[1.4] * 10)
    rows = CP.reserve_rows(stats, PROFILE, sett)
    assert rows["implementer"]["samples"] == 8
    assert rows["implementer"]["reserve_usd"] == pytest.approx(0.51 * 1.5)
    assert rows["gate_triager"]["reserve_usd"] == 0.25                # the floor
    assert rows["conformance_reviewer"]["reserve_usd"] == 1.5         # clamped to the budget
    few = CP.reserve_rows(_stats(implementer__sonnet=[0.5] * 7), PROFILE, sett)
    assert few["implementer"] == {"reserve_usd": 4.0, "budget_usd": 4.0, "samples": 7,
                                  "median_usd": 0.5}
    other_model = CP.reserve_rows(_stats(implementer__opus=[0.5] * 20), PROFILE, sett)
    assert other_model["implementer"]["reserve_usd"] == 4.0           # keyed by (persona, model)
    windowed = CP.reserve_rows(_stats(implementer__sonnet=[3.0] * 40 + [0.2] * 10), PROFILE,
                               CP.settings(_cfg(reserve={"window": 10})))
    assert windowed["implementer"]["median_usd"] == 0.2


def test_cap_blocks_reserve_and_hard_ceiling():
    st = _st(total=11.94, cap=15.0)
    # old behaviour (AC4): byte-for-byte the same text
    assert Q._cap_blocks(st, PROFILE, "implementer") == (
        "spent $11.94; a implementer run may cost $4.00; cap $15.00", 4.0)
    assert Q._cap_blocks(st, PROFILE, "implementer", reserves=None, ceiling=None) == \
        Q._cap_blocks(st, PROFILE, "implementer")
    assert Q._cap_blocks(st, PROFILE, "implementer", reserves={"implementer": 0.78},
                         ceiling=45.0) == (None, None)
    detail, budget = Q._cap_blocks(_st(14.5, 15.0), PROFILE, "implementer",
                                   reserves={"implementer": 0.78}, ceiling=45.0)
    assert budget == 4.0 and "(reserve $0.78 of budget $4.00)" in detail
    # the hard check: never start a run whose full budget crosses the ceiling
    detail, budget = Q._cap_blocks(_st(41.5, 45.0), PROFILE, "implementer",
                                   reserves={"implementer": 0.78}, ceiling=45.0)
    assert "spend ceiling $45.00" in detail and budget == 4.0
    detail, _ = Q._cap_blocks(_st(41.5, 44.0), PROFILE, "implementer",
                              reserves={"implementer": 0.78}, ceiling=45.0)
    assert "spend ceiling" in detail
    # a human-raised cap above the ceiling: soft check off, hard check against the cap
    assert Q._cap_blocks(_st(50.0, 60.0), PROFILE, "implementer",
                         reserves={"implementer": 0.78}, ceiling=45.0) == (None, None)
    assert Q._cap_blocks(_st(57.0, 60.0), PROFILE, "implementer",
                         reserves={"implementer": 0.78}, ceiling=45.0)[0]
    # plan_step forwards both
    assert Q.plan_step(_st(11.94), PROFILE).reason == "cost_cap"
    assert Q.plan_step(_st(11.94), PROFILE, reserves={"implementer": 0.78},
                       ceiling=45.0).kind == "implement"


# ---- convergence (AC1, AC2) ----------------------------------------------------------------

def test_converging_rules():
    cfg = {}
    assert CP.converging({}, [], cfg) == (None, "no review yet")
    assert CP.converging({}, [_out(1, 85, "pass")], cfg) == (True, "latest review passed 85/80")
    assert CP.converging({}, [_out(1, 50), _out(2, 70)], cfg) == (True, "score rose 50→70")
    conv, why = CP.converging({}, [_out(1, 72), _out(2, 65)], cfg)
    assert conv is False and why == "score fell 72→65"
    conv, why = CP.converging({}, [_out(1, 50), _out(2, 70, major=1)], cfg)
    assert conv is False and why == "1 major"
    conv, why = CP.converging({}, [_out(1, 81, "pass", must_fix=2)], cfg)
    assert conv is False and why == "2 must_fix"
    conv, why = CP.converging({}, [_out(1, 40)], cfg)
    assert conv is False and "first review" in why


def _note(seq, text):
    return {"type": "note", "seq": seq, "author": "queue", "text": text}


def _finding(seq, severity, persona="conformance_reviewer", title="t"):
    return {"type": "finding", "seq": seq, "persona": persona, "severity": severity,
            "title": title, "ac": None}


def test_legacy_note_parser():
    led = [_finding(1, "major"), _finding(2, "minor"), _note(3, "review score 59/80 fix"),
           _note(4, "something else"), _note(5, "review score 81/80 pass"),
           _note(6, "pass over major: c#1 thing"),
           {"type": "note", "seq": 7, "author": "human", "text": "review score 1/80 fix"}]
    outs = CP.review_outcomes(led)
    assert [(o["score"], o["outcome"], o["major"], o["must_fix"], o["source"]) for o in outs] == \
        [(59, "fix", 1, 0, "note"), (81, "pass", 1, 0, "note")]
    conv, why = CP.converging({}, led, {})
    assert conv is False and why == "1 major"
    # once the structured event exists, the notes it duplicates are ignored
    led2 = led[:6] + [_out(8, 90, "pass"), _note(9, "review score 90/80 pass")]
    assert [o["source"] for o in CP.review_outcomes(led2)] == ["note", "note", "event"]
    assert CP.converging({}, led2, {}) == (True, "latest review passed 90/80")


def _fix_round(seq, title):
    return [_finding(seq, "major", title=title),
            {"type": "transition", "seq": seq + 1, "to": "IMPLEMENTING", "reason": "review_fix"}]


def test_divergence_recurrence_blocks_convergence():
    led = []
    for i, title in enumerate(["the parser drops quoted fields", "lock leaks on error",
                               "the parser drops quoted fields"]):
        led += _fix_round(10 * i + 1, title)
    led += [_out(40, 70), _out(41, 85, "pass")]
    conv, why = CP.converging({}, led, {})
    assert conv is False and "diverging: recurrence" in why


# ---- decide ----------------------------------------------------------------------------------

def test_decide_raise_steps_to_the_ceiling():
    led = [_out(1, 85, "pass")]
    d = CP.decide(_st(14.0, 15.0), PROFILE, led, {}, blocked_budget=4.0)
    assert (d.action, d.new_cap, d.topic) == ("raise", 22.5, None)
    caps, cap = [], 15.0
    while True:
        d = CP.decide(_st(cap - 1.0, cap), PROFILE, led, {}, blocked_budget=4.0)
        if d.action != "raise":
            break
        caps.append(d.new_cap)
        cap = d.new_cap
    assert caps == [22.5, 30.0, 37.5, 45.0]
    assert (d.action, d.topic) == ("escalate", CP.TOPIC_CEILING)
    # the blocked budget is the minimum step
    big = {**PROFILE, "personas": {**PROFILE["personas"],
                                   "implementer": {"model": "sonnet", "max_budget_usd": 10.0}}}
    assert CP.decide(_st(14.0, 15.0), big, led, {}, blocked_budget=10.0).new_cap == 25.0


def test_decide_escalations_and_off():
    led = [_out(1, 85, "pass")]
    d = CP.decide(_st(42.0, 44.0), PROFILE, led, {}, blocked_budget=4.0)
    assert (d.action, d.topic) == ("escalate", "spend_ceiling") and "ceiling" in d.why
    d = CP.decide(_st(50.0, 60.0), PROFILE, led, {}, blocked_budget=4.0)   # human-raised cap
    assert (d.action, d.topic) == ("escalate", "spend_ceiling")
    d = CP.decide(_st(14.0), PROFILE, [_out(1, 72), _out(2, 65)], {}, blocked_budget=4.0)
    assert (d.action, d.topic) == ("escalate", "spend_diverging") and "72→65" in d.why
    d = CP.decide(_st(14.0), PROFILE, [], {}, blocked_budget=4.0)
    assert d.action == "raise" and d.facts["converging"] is None
    d = CP.decide(_st(14.0, **{CP.RAISES_NO_REVIEW: 1}), PROFILE, [], {}, blocked_budget=4.0)
    assert (d.action, d.topic) == ("escalate", "spend_diverging")
    assert d.why == "no review yet after 1 raises"
    assert CP.decide(_st(14.0), PROFILE, led, _cfg(enabled=False), blocked_budget=4.0).action == "off"
    assert CP.decide(_st(14.0, None), PROFILE, led, {}, blocked_budget=4.0).action == "off"


def test_escalation_text_has_options_facts_and_marker():
    d = CP.decide(_st(14.0), PROFILE, [_out(1, 72), _out(2, 65, major=1)], {}, blocked_budget=4.0)
    q, opts, facts = CP.escalation_text(_st(14.0), d, CP.review_outcomes(
        [_out(1, 72), _out(2, 65, major=1)]), CP.settings({}))
    assert "this is your call" in q and len(opts) == 4
    assert opts[0] == "raise the cap to $22.50 once" and opts[2] == "abandon"
    assert CP.marker(14.0, 15.0) in facts and "topic: spend_diverging" in facts
    assert any("72/80 fix, 65/80 fix" in f for f in facts)


# ---- run-cost statistics ---------------------------------------------------------------------

def _ledger(cfg, cid, lines):
    d = S.campaign_dir(cfg, cid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps({"id": cid, "state": "IMPLEMENTING",
                                              "created_at": S.now_iso(),
                                              "updated_at": S.now_iso()}))
    with open(d / "ledger.jsonl", "a") as f:
        for ln in lines:
            f.write(json.dumps(ln) + "\n")


def _run(seq, rid, cost, persona="implementer", model="sonnet", **kw):
    return [{"type": "run.start", "seq": seq, "run_id": rid, "persona": persona, "model": model},
            {"type": "run.end", "seq": seq + 1, "run_id": rid, "persona": persona,
             "cost_usd": cost, "is_error": False, "timed_out": False, "interrupted": False,
             "cost_estimated": False, "result_tail": "", **kw}]


def test_refresh_backfills_skips_bad_runs_and_is_incremental(tmp_path):
    cfg = {"data_dir": str(tmp_path)}
    S.agentic_dir(cfg).mkdir(parents=True, exist_ok=True)
    lines = (_run(1, "r1", 0.5) + _run(3, "r2", 0.7) + _run(5, "r3", None, cost_estimated=True)
             + _run(7, "r4", 0.9, is_error=True) + _run(9, "r5", 0.0)
             + _run(11, "r6", 0.8, result_tail="API Error: 529 overloaded")
             + _run(13, "r7", 1.2, persona="conformance_reviewer", model="opus"))
    _ledger(cfg, "C-1", lines)
    ro = CP.refresh(cfg, write=False)
    assert not CP.stats_path(cfg).exists()                       # read-only: nothing written
    assert ro["runs"] == {"implementer|sonnet": [0.5, 0.7], "conformance_reviewer|opus": [1.2]}
    doc = CP.refresh(cfg)
    assert doc["runs"] == ro["runs"] and CP.stats_path(cfg).is_file()
    _ledger(cfg, "C-1", _run(15, "r8", 0.6)[:1])                 # a run.start, no end yet
    doc = CP.refresh(cfg)
    assert doc["runs"]["implementer|sonnet"] == [0.5, 0.7] and doc["pending"]["C-1"] == {"r8": "sonnet"}
    _ledger(cfg, "C-1", _run(15, "r8", 0.6)[1:])
    doc = CP.refresh(cfg)
    assert doc["runs"]["implementer|sonnet"] == [0.5, 0.7, 0.6] and "C-1" not in doc["pending"]
    assert CP.refresh(cfg)["runs"]["implementer|sonnet"] == [0.5, 0.7, 0.6]   # no double count

