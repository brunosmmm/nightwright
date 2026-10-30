"""SPEC-0247: divergence detection in the implementation loop — `rounds`, finding matching, the
four signals, the trip rule, settings, and the queue integration (park `diverging`, resume)."""
import json
import types

import pytest

from wt.agentic import divergence as DV
from wt.agentic import queue as Q
from wt.agentic import schemas as SC
from wt.agentic import store as S

from test_agentic_e2e import (added, impl_done, make_cfg, make_repo, review, run_cli, script,
                              triage)

REV = "conformance_reviewer"
F = DV.Finding


def rec(etype, **payload):
    return {"type": etype, **SC.with_defaults(etype, payload)}


def finding(title, severity="must_fix", persona=REV, **kw):
    return rec("finding", persona=persona, severity=severity, title=title, **kw)


def gate(*results):
    return rec("gate", ok=all(ok for _, ok in results),
               results=[{"name": n, "ok": ok} for n, ok in results])


def fix(reason="review_fix"):
    return rec("transition", **{"from": "REVIEWING", "to": "IMPLEMENTING", "reason": reason})


def rnd(kind="review", blocking=(), gates=(), *, minor=(), environmental=False):
    return DV.Round(kind, [F(REV, "must_fix", t, None) for t in blocking]
                    + [F(REV, "minor", t, None) for t in minor], frozenset(gates), environmental)


def run_end(persona="implementer", **kw):
    return rec("run.end", persona=persona, **kw)


def failing_gate(tail):
    return rec("gate", ok=False, results=[{"name": "test", "ok": False, "tail": tail}])


# ---- rounds ----------------------------------------------------------------------------------

def test_rounds_one_per_fix_transition_with_kind_findings_and_failing_gates():
    ledger = [
        rec("open"), gate(("test", False), ("lint", True)),
        finding("gate broke", persona="gate_triager"), fix("triage_fix"),
        gate(("test", True)),
        finding("a defect"), finding("a nit", severity="nit"), finding("info", severity="info"),
        finding("blocker", persona="implementer", severity="major"),
        finding("human", persona="human"), finding("close", persona="close"),
        finding("tri minor", severity="minor", persona="review_triager"), fix("review_fix"),
        rec("transition", **{"from": "X", "to": "GATING", "reason": "implemented"}),
    ]
    first, second = DV.rounds(ledger)
    assert (first.kind, first.failing_gates) == ("gate", frozenset({"test"}))
    assert [f.title for f in first.findings] == ["gate broke"]
    assert (second.kind, second.failing_gates) == ("review", frozenset())
    assert [(f.persona, f.title) for f in second.findings] == [
        (REV, "a defect"), ("review_triager", "tri minor")]
    assert DV.rounds([]) == []


def test_rounds_before_the_resume_of_a_diverging_park_are_dropped():
    ledger = [finding("a"), fix(), finding("b"), fix(),
              rec("park", reason="diverging"), rec("resume", to="IMPLEMENTING"),
              finding("c"), fix()]
    assert [[f.title for f in r.findings] for r in DV.rounds(ledger)] == [["c"]]
    other = [finding("a"), fix(), rec("park", reason="cost_cap"), rec("resume"),
             finding("b"), fix()]
    assert len(DV.rounds(other)) == 2                     # another park resets nothing
    escalated = [finding("a"), fix(), rec("park", reason="diverging"),
                 rec("park", reason="human_decision", escalated_from="diverging"),
                 rec("resume"), finding("b"), fix()]
    assert len(DV.rounds(escalated)) == 1


def test_reset_loops_resume_cuts_rounds():
    """SPEC-0266: `--reset-loops` gives divergence a fresh start, whatever park it ended."""
    def ledger(reset):
        return [finding("a"), fix(), finding("b"), fix(), finding("c"), fix(),
                rec("park", reason="loop_exhausted"),
                rec("resume", to="IMPLEMENTING", loops_reset=reset), finding("d"), fix()]
    assert [[f.title for f in r.findings] for r in DV.rounds(ledger(True))] == [["d"]]
    assert len(DV.rounds(ledger(False))) == 4


def test_rounds_marks_environmental_gate_round():
    def env(*records):
        [r] = DV.rounds([*records, fix("triage_fix")])
        return r.environmental
    blocked = {"status": "blocked", "blockers": ["Disk full (ENOSPC) on /tmp"]}
    assert env(run_end(verdict=blocked))                                            # (a)
    assert env(run_end(is_error=True, result_tail="rate limit reached"))            # (b)
    assert env(failing_gate("OSError: [Errno 28] No space left on device"))         # (c)
    assert not env(run_end(verdict={"status": "blocked", "blockers": ["spec is ambiguous"]}))
    assert not env(rec("gate", ok=False, results=[
        {"name": "lint", "ok": False, "required": False, "tail": "ENOSPC"}]))
    assert not env(run_end("gate_triager", is_error=True, result_tail="rate limit reached"))
    assert not env(run_end(is_error=True, result_tail="boom"))
    assert not env(failing_gate("tests/x.py:429: AssertionError"))
    assert not env(failing_gate("529 passed, 1 failed"))
    # the flag is per round
    first, second = DV.rounds([run_end(verdict=blocked), fix("triage_fix"), fix("triage_fix")])
    assert (first.environmental, second.environmental) == (True, False)
    assert DV.Round("review").environmental is False


# ---- matching --------------------------------------------------------------------------------

def test_matching():
    m = DV.matches
    assert m(F(REV, "must_fix", "Missing  test!", None), F("test_reviewer", "major", "missing test", None))
    assert not m(F(REV, "must_fix", "Missing test", "AC1"), F(REV, "must_fix", "Missing test", "AC2"))
    assert not m(F(REV, "must_fix", "Missing test", "AC1"), F(REV, "must_fix", "Missing test", None))
    close = ("the parser drops trailing newline", "parser drops the trailing newline")
    assert m(F(REV, "major", close[0], "AC1"), F(REV, "major", close[1], "AC1"))
    assert m(F(REV, "major", close[0], None), F(REV, "major", close[1], None))
    assert not m(F(REV, "major", close[0], "AC1"), F(REV, "major", close[1], "AC2"))
    assert not m(F(REV, "major", "parser drops newline", None),
                 F(REV, "major", "cache never expires entries", None))
    assert not m(F(REV, "major", "UI", None), F(REV, "major", "a/b", None))    # empty word sets
    assert m(F(REV, "major", "UI", None), F(REV, "major", "ui!", None))        # equal keys
    assert not m(F(REV, "major", "UI", None), F(REV, "major", "UI bug on save", None))


def test_matching_threshold_is_at_or_above_similarity():
    def pair(a, b, **kw):
        return DV.matches(F(REV, "major", a, None), F(REV, "major", b, None), **kw)
    base = "alpha beta gamma delta"
    at = "alpha beta gamma sigma"                  # 3 shared of 5 words: Jaccard 0.6
    below = "alpha beta sigma omega"               # 2 shared of 6 words: Jaccard 1/3
    half = "alpha beta gamma sigma omega"          # 3 shared of 6 words: Jaccard 0.5
    assert pair(base, at)                          # exactly the default 0.6: a match
    assert not pair(base, below)
    assert not pair(base, half)                    # just under the cutoff
    assert not pair(base, "alpha cache omega zeta")    # one word in common is not enough
    assert not pair(base, at, similarity=0.7)      # `similarity` is honoured
    assert pair(base, half, similarity=0.5)
    assert pair(base, below, similarity=0.3)
    assert DV.assess([rnd(blocking=[base]), rnd(blocking=["cache never expires"]), rnd(blocking=[at])],
                     {"similarity": 0.6}) is not None        # recurrence at the cutoff
    assert DV.assess([rnd(blocking=[base]), rnd(blocking=["cache never expires"]), rnd(blocking=[at])],
                     {"similarity": 0.7}) is None


def test_malformed_ledger_findings_and_gates_never_raise():
    ledger = [finding("x", severity=["major"]), finding({"a": 1}, persona=["p"]),
              finding("ok", severity="major", ac=["AC1"]),
              gate(("test", False)), fix(),
              rec("gate", ok=False, results=[{"ok": False}, {"name": "lint", "ok": False}]),
              finding("ok", severity="major"), fix("triage_fix")]
    rs = DV.rounds(ledger)
    assert [[f.title for f in r.findings] for r in rs] == [["ok"], ["ok"]]
    assert rs[1].failing_gates == frozenset({"lint"})
    assert DV.assess(rs) is None


def test_optional_failing_gates_do_not_feed_gate_repeat():
    def optional(*results):
        return rec("gate", ok=False, results=[
            {"name": n, "ok": ok, "required": req} for n, ok, req in results])
    ledger = []
    for failing in ("test", "typecheck", "test"):
        ledger += [optional(("lint", False, False), (failing, False, True)),
                   finding("gate broke", persona="gate_triager"), fix("triage_fix")]
    rs = DV.rounds(ledger)
    assert [r.failing_gates for r in rs] == [frozenset({"test"}), frozenset({"typecheck"}),
                                              frozenset({"test"})]
    assert DV._gate_repeat(rs, 3) is None
    assert DV.assess(rs) is None                   # trend alone does not trip


def test_summary_is_one_line():
    div = {"signals": [{"name": "recurrence", "evidence": [
        {"title": "bad\nline\r\n  here " + "x" * 200, "rounds": [1, 3]}]},
        {"name": "gate_repeat", "evidence": {"gates": ["te\nst"]}}], "rounds": 3}
    out = DV.summary(div)
    assert "\n" not in out and "\r" not in out and len(out) < 200
    assert 'recurrence: "bad line here ' in out and "gate_repeat te st" in out


# ---- signals ---------------------------------------------------------------------------------

def names(div):
    return [s["name"] for s in div["signals"]] if div else []


def sigs(rs, **cfg):
    out = {"trend": DV._trend(rs, DV.DEFAULTS["window"], DV.DEFAULTS["similarity"]),
           "churn": DV._churn(rs, DV.DEFAULTS["churn_ratio"], DV.DEFAULTS["similarity"]),
           "recurrence": DV._recurrence(rs, DV.DEFAULTS["similarity"]),
           "gate_repeat": DV._gate_repeat(rs, DV.DEFAULTS["window"])}
    return {k for k, v in out.items() if v}


def test_trend():
    assert sigs([rnd(blocking="a"), rnd(blocking="b"), rnd(blocking="cd")]) >= {"trend"}
    assert "trend" not in sigs([rnd(blocking=["a"]), rnd(blocking=["b"]), rnd(blocking=["c"])])
    assert "trend" not in sigs([rnd(blocking=list("abcd")), rnd(blocking=list("ab")), rnd()])
    assert "trend" not in sigs([rnd(blocking=["a"]), rnd(blocking=["b"])])     # needs `window` rounds
    # a count that dipped and came back with the same blockers is a persisting blocker
    assert "trend" in sigs([rnd(blocking=list("ab")), rnd(blocking=["a"]), rnd(blocking=list("ab"))])


def test_trend_weighs_repeated_over_new():
    def trend(*rounds):
        return DV._trend(list(rounds), 3, DV.DEFAULTS["similarity"])
    assert trend(rnd(blocking=["a"]), rnd(blocking=["b"]), rnd(blocking=["c"])) is None
    div = trend(*[rnd(blocking=["parser drops newline"])] * 3)
    assert div["evidence"] == {"counts": [1, 1, 1], "scores": [0.5, 1.0, 1.0]}
    two = trend(rnd(blocking=["a", "b"]), rnd(blocking=["c", "d"]), rnd(blocking=["e", "f"]))
    assert two["evidence"] == {"counts": [2, 2, 2], "scores": [1.0, 1.0, 1.0]}
    assert DV.summary({"signals": [div], "rounds": 3}) == "trend 0.5→1→1"


def test_assess_drops_environmental_gate_rounds_only():
    def gr(**kw):
        return rnd("gate", blocking=["gate broke"], gates=["test"], **kw)
    assert DV.assess([gr(), gr(environmental=True), gr()]) is None      # 2 kept: no window
    div = DV.assess([gr(), gr(), gr(environmental=True), gr()])
    assert names(div) == ["trend", "gate_repeat"]
    assert div["rounds"] == 3 and div["excluded_rounds"] == 1
    assert "1 environmental round(s) excluded" in DV.summary(div)
    # a review round that hit the environment is kept
    x, y = "parser drops newline", "cache never expires"
    kept = DV.assess([rnd(blocking=[x]), rnd(blocking=[y], environmental=True), rnd(blocking=[x])])
    assert kept is not None and kept["rounds"] == 3 and kept["excluded_rounds"] == 0


def test_churn():
    new = [rnd(blocking=["alpha one", "beta two"]), rnd(blocking=["gamma three", "delta four"]),
           rnd(blocking=["epsilon five", "zeta six"])]
    assert "churn" in sigs(new)
    assert "churn" not in sigs(new[1:])                                        # needs 3 rounds
    repeated = [rnd(blocking=["alpha one", "beta two"])] * 3
    assert "churn" not in sigs(repeated)
    one = [rnd(blocking=["alpha one", "beta two"]), rnd(blocking=["gamma three"]),
           rnd(blocking=["epsilon five", "zeta six"])]
    assert "churn" not in sigs(one)                                            # < 2 findings in a round
    minors = [rnd(minor=["alpha one", "beta two"]), rnd(minor=["gamma three", "delta four"]),
              rnd(minor=["epsilon five", "zeta six"])]
    assert "churn" not in sigs(minors)                                         # blocking only


def test_churn_counts_blocking_only():
    sim = DV.DEFAULTS["similarity"]
    fresh_minors = [rnd(minor=["alpha one", "beta two"]), rnd(minor=["gamma three", "delta four"]),
                    rnd(minor=["epsilon five", "zeta six"])]
    assert DV._churn(fresh_minors, 0.75, sim) is None
    pairs = [rnd(blocking=["alpha one", "beta two"]), rnd(blocking=["gamma three", "delta four"]),
             rnd(blocking=["epsilon five", "zeta six"])]
    assert DV._churn(pairs, 0.75, sim)["evidence"] == {"ratios": [1.0, 1.0]}
    # a blocker raised earlier as a minor is not new
    seen = [rnd(blocking=["alpha one", "beta two"]),
            rnd(blocking=["gamma three", "delta four"], minor=["epsilon five", "zeta six"]),
            rnd(blocking=["epsilon five", "zeta six"])]
    assert DV._churn(seen, 0.75, sim) is None


def test_recurrence():
    x, y = "parser drops newline", "cache never expires"
    assert sigs([rnd(blocking=[x]), rnd(blocking=[y]), rnd(blocking=[x])]) >= {"recurrence"}
    div = DV._recurrence([rnd(blocking=[x]), rnd(blocking=[y]), rnd(blocking=[x])], 0.6)
    assert div["evidence"] == [{"title": x, "rounds": [1, 3]}]
    assert "recurrence" not in sigs([rnd(blocking=[x])] * 3)                   # never addressed
    assert "recurrence" not in sigs([rnd(blocking=[x]), rnd(blocking=[x])])    # needs 3 rounds
    assert "recurrence" not in sigs([rnd(blocking=[x]), rnd(), rnd(minor=[x])])  # only blocking
    # converging: blocking 4 → 2 → 0, findings repeated rather than new
    conv = [rnd(blocking=list("abcd")), rnd(blocking=list("ab")), rnd()]
    assert DV.assess(conv) is None


def test_implementer_blockers_do_not_recur():
    ledger = [finding("implementer blocker", severity="major", persona="implementer"), fix(),
              finding("something else"), fix(),
              finding("implementer blocker", severity="major", persona="implementer"),
              fix()]
    rs = DV.rounds(ledger)
    assert len(rs) == 3 and DV._recurrence(rs, 0.6) is None
    assert DV.assess(rs) is None


def test_gate_repeat_and_its_pairing_with_trend():
    def gr():
        return rnd("gate", blocking=["gate broke"], gates=["test"])
    assert sigs([gr(), gr()]) == set()                       # two failed gate rounds: neither fires
    assert DV.assess([gr(), gr()]) is None
    three = [gr(), gr(), gr()]
    assert {"trend", "gate_repeat"} <= sigs(three)
    div = DV.assess(three)
    assert names(div) == ["trend", "gate_repeat"] and div["rounds"] == 3
    assert div["signals"][1]["evidence"] == {"gates": ["test"]}
    review_between = [gr(), rnd("review", blocking=["x"]), gr()]
    assert "gate_repeat" not in sigs(review_between)         # consecutive gate rounds only
    other = [gr(), rnd("gate", blocking=["g"], gates=["lint"]), gr()]
    assert "gate_repeat" not in sigs(other)


# ---- trip rule -------------------------------------------------------------------------------

def test_trip_rule():
    x, y = "parser drops newline", "cache never expires"
    only_recurrence = [rnd(blocking=[x, "q", "r"]), rnd(blocking=[y]), rnd(blocking=[x])]
    assert sigs(only_recurrence) == {"recurrence"}           # counts 3→1→1: no trend
    assert names(DV.assess(only_recurrence)) == ["recurrence"]
    trend_churn = [rnd(blocking=["alpha one", "beta two"]), rnd(blocking=["gamma three", "delta four"]),
                   rnd(blocking=["epsilon five", "zeta six"])]
    assert sigs(trend_churn) == {"trend", "churn"}
    assert names(DV.assess(trend_churn)) == ["trend", "churn"]
    only_trend = [rnd(blocking=["a"]), rnd(blocking=["a"]), rnd(blocking=["a"])]
    assert sigs(only_trend) == {"trend"} and DV.assess(only_trend) is None
    only_churn = [rnd(blocking=["alpha one", "beta two", "gamma three", "delta four"]),
                  rnd(blocking=["epsilon five", "zeta six", "eta seven"]),
                  rnd(blocking=["theta eight", "iota nine"])]
    assert sigs(only_churn) == {"churn"} and DV.assess(only_churn) is None
    only_gate = [rnd("gate", gates=["test"])] * 3
    assert sigs(only_gate) == {"gate_repeat"} and DV.assess(only_gate) is None
    # fewer than min_rounds rounds, or disabled, never trip
    assert DV.assess([rnd(blocking=["a"])]) is None
    assert DV.assess(trend_churn, {"enabled": False}) is None
    assert DV.assess(trend_churn, {"min_rounds": 4}) is None


def _spec0251_ledger(*, enospc=True):
    """Seq 5-150 of campaign SPEC-0251-20261025-92e4, titles paraphrased."""
    def minors(*titles):
        return [finding(t, severity="minor") for t in titles]
    pytest_fail = lambda: gate(("pytest", False), ("lint", True))      # noqa: E731
    r3 = [pytest_fail(), finding("gate failed: /tmp is full", persona="gate_triager",
                                 severity="major")]
    if enospc:
        r3.insert(0, run_end(status="blocked", verdict={
            "status": "blocked", "blockers": ["Disk full (ENOSPC) on the volume holding /tmp"]}))
    return (
        [rec("open"), finding("tui events tab filter ignores the kind", severity="major"),
         *minors("help text typo", "missing docstring", "unused import"), fix("review_fix"),
         pytest_fail(), finding("gate failed: tests error", persona="gate_triager",
                                severity="minor"), fix("triage_fix"),
         *r3, fix("triage_fix"),
         finding("wt tui --tab events CLI choice is not tested", persona="test_reviewer",
                 severity="major"),
         *minors("log line wording", "extra blank line", "stale comment"), fix("review_fix"),
         finding("Event text is put into the DataTable as a raw str", severity="major",
                 persona="adversarial_reviewer"),
         *minors("column width is hardcoded", "no empty state", "rename helper")])


def test_spec0251_replay_does_not_trip():
    for enospc in (True, False):
        ledger = _spec0251_ledger(enospc=enospc)
        rs = DV.rounds(ledger + [fix("review_fix")])         # r5 is the pending fix
        assert [r.kind for r in rs] == ["review", "gate", "gate", "review", "review"]
        assert [r.environmental for r in rs] == [False, False, enospc, False, False]
        assert DV.assess(rs) is None


# ---- settings --------------------------------------------------------------------------------

def test_settings_defaults_and_fallbacks():
    assert DV.settings({}) == DV.DEFAULTS
    assert DV.settings({"agentic": {"divergence": "nope"}}) == DV.DEFAULTS
    ok = {"enabled": False, "window": 4, "min_rounds": 3, "churn_ratio": 0.5, "similarity": 1}
    assert DV.settings({"agentic": {"divergence": ok}}) == ok
    bad = {"enabled": "yes", "window": 1, "min_rounds": 2.5, "churn_ratio": 0, "similarity": 1.5}
    assert DV.settings({"agentic": {"divergence": bad}}) == DV.DEFAULTS
    for key, value in (("window", True), ("window", "3"), ("min_rounds", 1), ("churn_ratio", "x"),
                       ("churn_ratio", 1.01), ("similarity", -0.1), ("similarity", None)):
        got = DV.settings({"agentic": {"divergence": {key: value}}})
        assert got[key] == DV.DEFAULTS[key], (key, value)


# ---- queue integration -----------------------------------------------------------------------

X, Y = "feature.txt is wrong", "docs are missing entirely"


def _fix_review(title):
    return review("fix", findings=[{"severity": "must_fix", "ac": "feature.txt exists",
                                    "title": title, "detail": "d", "suggested_fix": "f", "files": ["feature.txt"]}])


def _drive(tmp_path, monkeypatch, entries, **profile):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, entries), profile=profile)
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    return cfg, cid


def test_e2e_recurrence_parks_diverging_then_resume_continues(tmp_path, monkeypatch):
    entries = [impl_done(), _fix_review(X), impl_done(), _fix_review(Y), impl_done(),
               _fix_review(X),
               # after the resume
               impl_done(), _fix_review(Y), impl_done(), review("pass")]
    cfg, cid = _drive(tmp_path, monkeypatch, entries, loop_max=4)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "diverging", st
    assert st["park"]["resume_to"] == "IMPLEMENTING" and st["counters"]["loops"] == 3
    assert st["pending_brief"] and st["pending_brief"]["fix_brief"]
    assert Q.park_owner("diverging") == "conductor"
    assert 'recurrence: "feature.txt is wrong" (r1, r3)' in st["park"]["detail"]
    [div] = S.read_ledger(cfg, cid, types=["loop_divergence"])
    assert "recurrence" in names(div) and div["rounds"] == 3 and div["loop"] == 3
    assert SC.ledger_problems(div) == []
    assert not any(e["to"] == "IMPLEMENTING" and e["reason"] == "review_fix"
                   for e in S.read_ledger(cfg, cid, types=["transition"])[-1:])
    # resume: the implementer runs again and the next fix round does not re-park
    r = run_cli(cfg, monkeypatch, ["queue", "resume", cid, "--yes", "--json"])
    assert r.exit_code == 0, r.output
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st
    assert len(S.read_ledger(cfg, cid, types=["loop_divergence"])) == 1
    assert [p["reason"] for p in S.read_ledger(cfg, cid, types=["park"])] == ["diverging"]


def test_e2e_loop_exhausted_wins_over_diverging(tmp_path, monkeypatch):
    entries = [impl_done(), _fix_review(X), impl_done(), _fix_review(Y), impl_done(),
               _fix_review(X)]
    cfg, cid = _drive(tmp_path, monkeypatch, entries, loop_max=2)
    st = S.read_state(cfg, cid)
    assert st["park"]["reason"] == "loop_exhausted" and st["counters"]["loops"] == 2
    assert S.read_ledger(cfg, cid, types=["loop_divergence"]) == []


def test_e2e_disabled_never_parks_diverging(tmp_path, monkeypatch):
    entries = [impl_done(), _fix_review(X), impl_done(), _fix_review(Y), impl_done(),
               _fix_review(X), impl_done(), review("pass")]
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, entries), profile={"loop_max": 4})
    cfg["agentic"]["divergence"] = {"enabled": False}
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st
    assert S.read_ledger(cfg, cid, types=["loop_divergence"]) == []


def test_e2e_gate_repeat_trips_on_the_third_failed_fix(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, test_gate="exit 1")
    entries = [impl_done(), triage("fix"), impl_done(), triage("fix"), impl_done(), triage("fix")]
    cfg = make_cfg(tmp_path, repo, script(tmp_path, entries), profile={"loop_max": 4})
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["park"]["reason"] == "diverging" and st["counters"]["loops"] == 3, st
    [div] = S.read_ledger(cfg, cid, types=["loop_divergence"])
    assert names(div) == ["trend", "gate_repeat"]
    assert st["park"]["resume_to"] == "IMPLEMENTING"
    assert st["pending_brief"] and st["pending_brief"]["source"] == "gate_triager"
    assert Q.park_owner("diverging") == "conductor"


def test_check_read_error_notes_and_returns_none(monkeypatch):
    def boom(*a, **kw):
        raise OSError("disk gone")
    monkeypatch.setattr(DV, "store", types.SimpleNamespace(read_ledger=boom))
    pending = []
    assert DV.check({}, {"id": "C-1"}, pending) is None
    assert pending == [("note", {"text": "divergence check skipped: disk gone",
                                 "author": "queue"})]


def test_e2e_ledger_read_error_still_transitions_to_implementing(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise OSError("disk gone")
    monkeypatch.setattr(DV, "store", types.SimpleNamespace(read_ledger=boom))
    entries = [impl_done(), _fix_review(X), impl_done(), review("pass")]
    cfg, cid = _drive(tmp_path, monkeypatch, entries)
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st
    notes = [e["text"] for e in S.read_ledger(cfg, cid, types=["note"])]
    assert "divergence check skipped: disk gone" in notes
    trans = [(e["from"], e["to"], e["reason"]) for e in S.read_ledger(cfg, cid, types=["transition"])]
    assert ("REVIEWING", "IMPLEMENTING", "review_fix") in trans


# ---- environmental rounds through `check` and the edge rules --------------------------------

def test_check_reads_run_end_and_reports_excluded_rounds(monkeypatch):
    title = "pytest fails on the parser"
    enospc = run_end(status="blocked", verdict={"status": "blocked",
                                                 "blockers": ["Disk full (ENOSPC) on /tmp"]})

    def gate_round(*extra):
        return [*extra, gate(("test", False)), finding(title, persona="gate_triager"),
                fix("triage_fix")]
    ledger = [rec("open"), *gate_round(), *gate_round(enospc), *gate_round()]

    def read_ledger(cfg, cid, types=None):
        return [r for r in ledger if types is None or r["type"] in types]
    monkeypatch.setattr(DV, "store", types.SimpleNamespace(read_ledger=read_ledger))
    pending = [("gate", {"ok": False, "results": [{"name": "test", "ok": False}]}),
               ("finding", {"persona": "gate_triager", "severity": "must_fix", "title": title})]
    div = DV.check({}, {"id": "C-1", "state": "TRIAGING", "counters": {"loops": 4}}, pending)
    assert div is not None and div["loop"] == 4
    assert div["rounds"] == 3 and div["excluded_rounds"] == 1
    assert names(div) == ["trend", "gate_repeat"]
    assert pending[0][0] == "gate" and len(pending) == 2                 # no note: the read worked


def test_recurrence_evidence_uses_kept_round_numbers():
    ledger = [finding(X), fix(),
              failing_gate("OSError: [Errno 28] No space left on device"), fix("triage_fix"),
              finding(Y), fix(),
              finding(X), fix()]
    rs = DV.rounds(ledger)
    assert [r.kind for r in rs] == ["review", "gate", "review", "review"]
    assert rs[1].environmental
    div = DV.assess(rs)
    [rcr] = [s for s in div["signals"] if s["name"] == "recurrence"]
    assert [h["rounds"] for h in rcr["evidence"]] == [[1, 3]]
    assert div["rounds"] == 3 and div["excluded_rounds"] == 1
    assert "(r1, r3)" in DV.summary(div)


def test_min_rounds_counts_kept_rounds_only():
    same = [rnd(blocking=[X]), rnd("gate", blocking=[Y], gates=["test"]), rnd(blocking=[X])]
    assert "recurrence" in names(DV.assess(same))                          # 3 rounds recur
    env = [same[0], rnd("gate", blocking=[Y], gates=["test"], environmental=True), same[2]]
    assert DV.assess(env) is None                                        # only 2 kept


def test_environmental_rule_edges():
    def env(*records):
        [r] = DV.rounds([*records, fix("triage_fix")])
        return r.environmental
    # (a) matches on the verdict summary alone
    assert env(run_end(status="blocked", verdict={"status": "blocked", "summary": "ENOSPC on /tmp"}))
    assert not env(run_end(status="blocked", verdict={"status": "blocked", "summary": "unclear"}))
    # (b) a timeout with no result_tail falls back to "timed out", which is not transient
    assert not env(run_end(timed_out=True))
    assert not env(run_end(is_error=True))
    assert env(run_end(timed_out=True, result_tail="API Error: 529 overloaded"))
