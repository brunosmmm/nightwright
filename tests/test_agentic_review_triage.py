"""SPEC-0203: `triage_reviews` (pure scorer) and `review_brief` — worked examples, dedup and
canonical ids, `waived` handling, AC cover matching, brief shape."""
import copy

import pytest

from wt.agentic import queue as Q
from wt.agentic.profiles import BUILTIN_PROFILES

A, B = "conformance_reviewer", "test_reviewer"
BAL = {"review": {"pass_score": 80, "weights": {"must_fix": 100, "major": 15, "minor": 4,
                                                 "nit": 1}}}


def f(sev, title=None, **kw):
    return dict({"severity": sev, "title": title or f"{sev} thing"}, **kw)


def v(verdict="pass", *findings, brief="", ac=()):
    return {"verdict": verdict, "score": 50, "fix_brief": brief, "findings": list(findings),
            "ac_assessment": [{"ac": a, "met": m} for a, m in ac]}


def tri(verdicts, profile=BAL, waived=()):
    return Q.triage_reviews(verdicts, profile, waived)


def test_minor_fix_does_not_block():
    out = tri({A: v("fix", f("minor"), f("minor", "other"), brief="x"),
               B: v("pass", f("nit"))})
    assert (out["outcome"], out["score"], out["score_only"]) == ("pass", 91, False)
    assert out["blocking"] == [] and len(out["advisory"]) == 3 and out["fixing"] == []


def test_fix_with_major_blocks_and_score_is_irrelevant():
    out = tri({A: v("fix", f("major"), f("minor"))})
    assert (out["outcome"], out["score"]) == ("fix", 81)
    assert [b["severity"] for b in out["blocking"]] == ["major"] and out["fixing"] == [A]
    assert [a["severity"] for a in out["advisory"]] == ["minor"] and not out["score_only"]


def test_fix_without_findings_adds_the_synthetic_major():
    out = tri({A: v("fix", brief="rewrite " + "x" * 100)})
    [b] = out["blocking"]
    assert (b["id"], b["severity"], b["title"]) == (f"{A}#fix", "major", ("rewrite " + "x" * 100)[:80])
    assert out["outcome"] == "fix" and out["synthetic"] == [f"{A}#fix"]
    [b] = tri({A: v("fix")})["blocking"]
    assert b["title"] == f"{A} returned fix without findings"


def test_score_only_and_waivable_weight():
    verdicts = {A: v("pass", *[f("minor", f"a{i}") for i in range(4)]),
                B: v("pass", *[f("minor", f"b{i}") for i in range(3)])}
    out = tri(verdicts)
    assert (out["outcome"], out["score"], out["score_only"], out["waivable_weight"]) == (
        "fix", 72, True, 28)
    assert len(out["blocking"]) == 7 and out["advisory"] == []
    waive = [f"{A}#0", f"{A}#1", f"{A}#2"]
    out = tri(verdicts, waived=waive)
    assert (out["outcome"], out["score"], out["waivable_weight"]) == ("pass", 84, 16)
    assert tri({A: v("pass", f("minor"))})["waivable_weight"] == 4


def test_must_fix_always_fixes_and_pass_with_must_fix_is_a_fix():
    out = tri({A: v("pass", f("must_fix"))})
    assert out["outcome"] == "fix" and out["fixing"] == [A] and not out["score_only"]
    assert out["score"] == 0 and out["blocking"][0]["severity"] == "must_fix"
    heavy = dict(BAL, review={"pass_score": 0, "weights": dict(BAL["review"]["weights"],
                                                                must_fix=0)})
    assert tri({A: v("pass", f("must_fix"))}, heavy)["outcome"] == "fix"


def test_pass_over_a_major_passes_and_it_is_advisory():
    out = tri({A: v("pass", f("major"))})
    assert (out["outcome"], out["score"]) == ("pass", 85)
    assert out["blocking"] == [] and out["advisory"][0]["severity"] == "major"


def test_profiles_weights_and_pass_score():
    verdicts = {A: v("pass", *[f("minor", f"m{i}") for i in range(6)])}      # score 76
    outs = {n: tri(verdicts, BUILTIN_PROFILES[n])["outcome"] for n in BUILTIN_PROFILES}
    assert outs == {"balanced": "fix", "thorough": "fix", "frugal": "pass"}
    assert tri(verdicts, {})["outcome"] == "fix"                      # no `review`: balanced
    cheap = {"review": {"pass_score": 80, "weights": {"minor": 1}}}  # partial weights: defaults
    assert tri(verdicts, cheap)["score"] == 94


def test_duplicates_across_reviewers_count_once():
    one = f("minor", "Missing  test", files=["b.py", "a.py"])
    two = f("major", "missing test", files=["a.py", "b.py"])
    out = tri({A: v("fix", one, brief="a"), B: v("pass", two)})
    assert out["score"] == 85
    [m] = out["blocking"]
    assert (m["id"], m["severity"], m["personas"]) == (f"{B}#0", "major", [A, B])
    assert out["outcome"] == "fix" and out["fixing"] == [A]           # A survives via the merge
    assert out["advisory"] == []


def test_duplicate_tie_keeps_the_earliest_persona_and_same_reviewer_merges():
    x = f("minor", "same", files=["a.py"])
    out = tri({A: v("pass", x), B: v("pass", dict(x))})
    [m] = out["advisory"]
    assert (m["id"], m["personas"], out["score"]) == (f"{A}#0", [A, B], 96)
    out = tri({A: v("pass", x, dict(x))})
    assert out["score"] == 96 and len(out["advisory"]) == 1
    # no files: never merged
    out = tri({A: v("pass", f("minor", "same"), f("minor", "same"))})
    assert out["score"] == 92


def test_unmet_ac_forces_fix_with_a_synthetic_major():
    out = tri({A: v("pass", f("minor", ac="AC one"), ac=[("AC one", False)])})
    assert (out["outcome"], out["score"], out["score_only"]) == ("fix", 81, False)
    [b] = out["blocking"]
    assert (b["id"], b["severity"], b["title"]) == (f"{A}#ac0", "major", "AC one")
    assert [a["severity"] for a in out["advisory"]] == ["minor"]


def test_unmet_ac_with_a_covering_major_adds_no_synthetic():
    out = tri({A: v("pass", f("major", "real", ac="AC one"), ac=[("AC one", False)])})
    assert out["outcome"] == "fix" and out["synthetic"] == []
    assert [b["title"] for b in out["blocking"]] == ["real"]


@pytest.mark.parametrize("finding_ac,covers", [
    ("AC one", True), ("  ac   ONE ", True),
    ("the cache is invalidated", True),            # containment >= 8
    ("cache is invalidated on write", True),
    ("AC", False), ("ac 1", False),                 # contained but too short / different
    ("something else", False), ("", False), (None, False),
])
def test_ac_cover_matching(finding_ac, covers):
    entry = "the cache is invalidated on write" if "cache" in str(finding_ac) else "AC one"
    kw = {"ac": finding_ac} if finding_ac is not None else {}
    out = tri({A: v("pass", f("major", "real", **kw), ac=[(entry, False)])})
    assert out["outcome"] == "fix"
    assert (out["synthetic"] == []) is covers
    assert len(out["blocking"]) == (1 if covers else 2)


def test_fix_without_findings_and_unmet_ac_gets_both_synthetics():
    out = tri({A: v("fix", brief="b", ac=[("AC one", False)])})
    assert out["synthetic"] == [f"{A}#fix", f"{A}#ac0"]
    assert out["score"] == 70 and len(out["blocking"]) == 2


def test_synthetic_ac_keeps_a_fix_verdict_alive():
    out = tri({A: v("fix", f("minor"), f("minor", "b"), brief="fix it",
                    ac=[("AC one", False)])})
    assert out["fixing"] == [A] and out["synthetic"] == [f"{A}#ac0"]
    brief = Q.review_brief({A: v("fix", brief="fix it")}, out, 80)
    assert brief["fix_brief"] == "fix it"


def test_waived_never_removes_major_must_fix_or_synthetic():
    verdicts = {A: v("fix", f("major"), f("minor")), B: v("fix", brief="b",
                                                       ac=[("AC", False)])}
    ids = [f"{A}#0", f"{A}#1", f"{B}#fix", f"{B}#ac0"]
    out = tri(verdicts, waived=ids)
    surviving = {x["id"] for x in out["blocking"] + out["advisory"]}
    assert surviving == {f"{A}#0", f"{B}#fix", f"{B}#ac0"}           # only the minor went
    assert tri({A: v("pass", f("must_fix"))}, waived=[f"{A}#0"])["outcome"] == "fix"


def test_triage_is_pure():
    verdicts = {A: v("fix", f("major", files=["a"]), f("minor")), B: v("pass", f("minor"))}
    before = copy.deepcopy(verdicts)
    assert tri(verdicts) == tri(verdicts)
    assert verdicts == before


def test_unknown_severity_counts_as_minor():
    assert tri({A: v("pass", f("weird"))})["score"] == 96


# ---- review_brief --------------------------------------------------------------------------------

def test_multi_brief_shape():
    verdicts = {A: v("fix", f("major", "boom", detail="why", files=["a.py", "b.py"]),
                     brief="do X"),
                B: v("pass", f("minor"))}
    out = tri(verdicts)
    brief = Q.review_brief(verdicts, out, 80)
    assert brief["personas"] == [A]
    assert brief["fix_brief"] == f"[{A}] do X\n\n- [major] boom (a.py, b.py): why"
    assert brief["must_fix"] == [{"ac": None, "title": "boom", "detail": "why",
                                  "files": ["a.py", "b.py"], "id": f"{A}#0",
                                  "severity": "major", "personas": [A]}]


def test_score_only_brief_has_the_score_line():
    verdicts = {A: v("pass", *[f("minor", f"a{i}") for i in range(4)]),
                B: v("pass", *[f("minor", f"b{i}") for i in range(3)])}
    brief = Q.review_brief(verdicts, tri(verdicts), 80)
    lines = brief["fix_brief"].splitlines()
    assert lines[0] == ("Review score 72 is below the pass score 80; address the "
                        "highest-weight findings first.")
    assert len(lines) == 8 and lines[1] == "- [minor] a0"
    assert len(brief["must_fix"]) == 7 and brief["personas"] == [A, B]


def test_single_reviewer_brief_keeps_the_0187_shape():
    verdicts = {A: v("fix", f("must_fix", ac="AC", detail="d", files=["x"]), brief="verbatim")}
    brief = Q.review_brief(verdicts, tri(verdicts), 80)
    assert brief == {"fix_brief": "verbatim", "must_fix": [
        {"ac": "AC", "title": "must_fix thing", "detail": "d", "files": ["x"]}]}
    # empty reviewer brief: a synthetic or score-only outcome gets bullets, others stay empty
    solo = {A: v("fix")}
    assert Q.review_brief(solo, tri(solo), 80)["fix_brief"].startswith("- [major] ")
    low = {A: v("pass", *[f("minor", f"m{i}") for i in range(6)])}
    assert Q.review_brief(low, tri(low), 80)["fix_brief"].startswith("Review score 76")
    empty = {A: v("fix", f("must_fix"))}
    assert Q.review_brief(empty, tri(empty), 80)["fix_brief"] == ""
