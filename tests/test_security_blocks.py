"""SPEC-0597: a `major` (or `must_fix`) finding in a security category, from any reviewer,
always blocks: not the reviewer's own `pass` verdict, a score at or above the pass score, the
SPEC-0468 adversarial-major relief, the SPEC-0555 collator or the SPEC-0554 delta rule lets it
through. Only a human waiver removes it. Non-security adversarial majors keep the 0468 relief."""
import json

import pytest

from test_agentic_collator import (ADV, CONF, MARSHAL, _drive, _mark_prior_fixed, _Recording,
                                   _setup, adv, collator, marshal)
from test_agentic_e2e import added, impl_done, review
from nightwright.agentic import collator as CO
from nightwright.agentic import personas as PS
from nightwright.agentic import profiles as P
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S

TEST = "test_reviewer"
PROFILE = {"loop_max": 3, "reviewers": [CONF, TEST, ADV], "review": {"pass_score": 80}}
MAC = "Signed 401 doubles as a valid request MAC (MAC reflection)"


def _v(verdict, *findings, score=95):
    return {"verdict": verdict, "score": score, "ac_assessment": [], "fix_brief": "",
            "findings": list(findings)}


def _f(severity, title, category=None, files=("src/hub.py",), **kw):
    f = {"severity": severity, "title": title, "files": list(files), **kw}
    if category:
        f["category"] = category
    return f


def _replay(category="security", **extra):
    """The SPEC-0529 replay shape: conformance passed, the adversarial reviewer says `pass` with
    score 85 (pass score 80) and one major."""
    return {CONF: _v("pass", score=96), TEST: _v("pass", score=92),
            ADV: _v("pass", _f("major", MAC, category, **extra), score=85)}


pytestmark = pytest.mark.fast


# ---- pure: the triage ------------------------------------------------------------------------

@pytest.mark.parametrize("category", list(P.SECURITY_CATEGORIES) + ["Security "])
def test_adversarial_security_major_with_pass_verdict_routes_to_fix(category):
    tri = Q.triage_reviews(_replay(category), PROFILE)
    assert tri["outcome"] == "fix" and not tri["score_only"]
    assert tri["score"] == 85                                   # >= pass score, still fix
    [b] = tri["blocking"]
    assert (b["title"], b["severity"], b["security_block"]) == (MAC, "major", True)
    assert tri["security_block"] == [f"{ADV}#0"] and tri["fixing"] == []
    brief = Q.review_brief(_replay(category), tri, 80)
    assert MAC in brief["fix_brief"] and [m["title"] for m in brief["must_fix"]] == [MAC]


@pytest.mark.parametrize("category", ["bug", "logic", "data_loss", "concurrency", None])
def test_non_security_adversarial_major_keeps_the_0468_relief(category):
    tri = Q.triage_reviews(_replay(category), PROFILE)
    assert tri["outcome"] == "pass" and tri["blocking"] == []
    assert [f["title"] for f in tri["advisory"]] == [MAC]
    assert "security_block" not in tri and "security_block" not in tri["advisory"][0]


@pytest.mark.parametrize("persona", [CONF, TEST])
def test_conformance_or_test_reviewer_security_major_blocks(persona):
    verdicts = {CONF: _v("pass", score=96), TEST: _v("pass", score=92), ADV: _v("pass")}
    verdicts[persona] = _v("pass", _f("major", "token logged in clear", "secrets"), score=90)
    tri = Q.triage_reviews(verdicts, PROFILE)
    assert tri["outcome"] == "fix" and tri["security_block"] == [f"{persona}#0"]
    # the same finding without a category is scored only: 85 >= 80 and its verdict is pass
    verdicts[persona] = _v("pass", _f("major", "token logged in clear"), score=90)
    assert Q.triage_reviews(verdicts, PROFILE)["outcome"] == "pass"


def test_single_reviewer_brief_names_the_security_finding():
    verdicts = {ADV: _v("pass", _f("major", MAC, "security"), score=85)}
    prof = dict(PROFILE, reviewers=[ADV])
    tri = Q.triage_reviews(verdicts, prof)
    assert tri["outcome"] == "fix"
    assert MAC in Q.review_brief(verdicts, tri, 80)["fix_brief"]


def test_security_mark_survives_a_merge_with_an_unmarked_finding():
    """1d merge: the conformance copy (no category, listed first) keeps the record; the mark
    from the adversarial copy still makes it block."""
    verdicts = {CONF: _v("pass", _f("major", MAC), score=96), TEST: _v("pass"),
                ADV: _v("pass", _f("major", MAC, "security"), score=85)}
    tri = Q.triage_reviews(verdicts, PROFILE)
    assert tri["outcome"] == "fix"
    [b] = tri["blocking"]
    assert b["personas"] == [CONF, ADV] and b["security_block"] is True


def test_stored_scope_out_or_delta_demotion_cannot_hide_a_security_major():
    """In-flight campaigns: a collation or a demotion stored before this rule is ignored for a
    security major at the next triage."""
    for mark in ({"scope": "out", "tie": "none"}, {"demoted": True}):
        tri = Q.triage_reviews(_replay("security", **mark), PROFILE)
        assert tri["outcome"] == "fix", mark
        assert "out_of_scope" not in tri and "basis_demoted" not in tri
    # a non-security major keeps both behaviours
    assert Q.triage_reviews(_replay("bug", scope="out"), PROFILE)["out_of_scope"]
    assert Q.triage_reviews(_replay("bug", demoted=True), PROFILE)["basis_demoted"]


def test_minor_security_finding_does_not_force_fix_and_must_fix_still_does():
    minor = {CONF: _v("pass"), TEST: _v("pass"),
             ADV: _v("pass", _f("minor", "weak compare", "security"), score=90)}
    assert Q.triage_reviews(minor, PROFILE)["outcome"] == "pass"
    must = {CONF: _v("pass"), TEST: _v("pass"),
            ADV: _v("pass", _f("must_fix", "drops the row", "data_loss"), score=90)}
    tri = Q.triage_reviews(must, PROFILE)
    assert tri["outcome"] == "fix" and "security_block" not in tri


def test_a_human_waiver_still_removes_a_security_major():
    tri = Q.triage_reviews(_replay("security"), PROFILE, human_waived=[
        {"id": f"{ADV}#0", "title": MAC, "reason": "accepted risk"}])
    assert tri["outcome"] == "pass" and tri["human_waived"] == [f"{ADV}#0"]


def test_decide_reviews_sends_the_replay_shape_back_to_implementing():
    step = Q.decide_reviews({"counters": {"loops": 0}}, PROFILE, _replay("security"))
    assert step.kind == "implement" and step.reason == "review_fix"
    bug = Q.decide_reviews({"counters": {"loops": 0}}, PROFILE, _replay("bug"))
    assert bug.kind == "close"


# ---- pure: the collator ----------------------------------------------------------------------

def test_collator_cannot_scope_out_a_security_major():
    verdicts = {ADV: {"findings": [_f("major", MAC, "security", files=("src/old.py",)),
                                   _f("major", "slow loop", "bug", files=("src/old.py",))]}}
    items = CO.inputs(verdicts)
    recs = [{"id": f"{ADV}#{i}", "merged": [], "tie": "none", "scope": "out", "reason": "r",
             "severity": "major"} for i in range(2)]
    out, stats = CO.collate({"collated": recs}, items, changed={"src/new.py"}, ac_ids={"AC1"})
    assert [(r["scope"], r.get("guard")) for r in out] == [("in", "security"), ("out", None)]
    assert stats["guarded"] == 1


def test_collator_inputs_keep_a_demoted_security_major():
    verdicts = {ADV: {"findings": [_f("major", MAC, "injection", demoted=True),
                                   _f("minor", "nit", "security", demoted=True)]}}
    assert [i["id"] for i in CO.inputs(verdicts)] == [f"{ADV}#0"]


# ---- schemas and prompts ---------------------------------------------------------------------

@pytest.mark.parametrize("schema", [PS.CONFORMANCE_REVIEWER_SCHEMA, PS.TEST_REVIEWER_SCHEMA])
def test_conformance_and_test_schemas_carry_an_optional_security_category(schema):
    item = schema["properties"]["findings"]["items"]
    assert item["properties"]["category"]["enum"] == list(P.SECURITY_CATEGORIES)
    assert "category" not in item["required"]
    assert set(P.SECURITY_CATEGORIES) <= set(PS.ADVERSARIAL_CATEGORIES)
    assert "data_loss" not in P.SECURITY_CATEGORIES


@pytest.mark.parametrize("name", [CONF, TEST])
def test_reviewer_templates_ask_for_the_security_category(name):
    text = PS.PERSONAS[name].template
    assert "`path_traversal`, `secrets`, `subprocess`" in text
    assert "always blocks" in text and '"category": "..."' in text


# ---- the queue path, lean and not ------------------------------------------------------------

SEC = {"severity": "major", "category": "security", "title": MAC,
       "detail": "a signed 401 body is a valid request MAC", "files": ["pyproject.toml"]}
COLLATE_OUT = [{"id": f"{ADV}#0", "merged": [], "tie": "none", "scope": "out",
                "reason": "pyproject.toml is not touched", "severity": "major"}]


def _run(tmp_path, monkeypatch, lean, finding):
    repo, cfg = _setup(tmp_path, lean=lean)
    cid = added(cfg, monkeypatch)
    loop1 = adv("pass", [finding], score=85)
    runner = _Recording({
        "implementer": [impl_done(), impl_done(content="better\n")],
        MARSHAL: [marshal(), marshal()] if lean else [],
        CONF: [review(), review()], ADV: [loop1, adv("pass")],
        CO.NAME: [collator(COLLATE_OUT)] if lean else []})
    if lean:
        _mark_prior_fixed(runner, cfg, cid)
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
    outcomes = [o["outcome"] for o in S.read_ledger(cfg, cid, types=["review_outcome"])]
    impls = [r.prompt for r in runner.reqs if r.persona == "implementer"]
    return cfg, cid, outcomes, impls


@pytest.mark.integration
@pytest.mark.parametrize("lean", [False, True])
def test_queue_security_major_under_pass_verdict_forces_a_fix_loop(tmp_path, monkeypatch, lean):
    cfg, cid, outcomes, impls = _run(tmp_path, monkeypatch, lean, SEC)
    assert outcomes == ["fix", "pass"]
    assert len(impls) == 2 and MAC in impls[1]
    if lean:                    # the collator said `out`; the guard kept it in scope
        doc = json.loads((S.campaign_dir(cfg, cid) / "findings-loop-1.json").read_text())
        assert [(r["scope"], r.get("guard")) for r in doc["collated"]] == [("in", "security")]
        assert not S.read_ledger(cfg, cid, types=["followup"]) or all(
            e["title"] != MAC for e in S.read_ledger(cfg, cid, types=["followup"])
            if e.get("origin") == "out_of_scope")


@pytest.mark.integration
@pytest.mark.parametrize("lean", [False, True])
def test_queue_non_security_major_still_passes_under_0468(tmp_path, monkeypatch, lean):
    repo, cfg = _setup(tmp_path, lean=lean)
    cid = added(cfg, monkeypatch)
    runner = _Recording({
        "implementer": [impl_done()], MARSHAL: [marshal()] if lean else [],
        CONF: [review()], ADV: [adv("pass", [dict(SEC, category="bug")], score=85)],
        CO.NAME: [collator(COLLATE_OUT)] if lean else []})
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
    assert [o["outcome"] for o in S.read_ledger(cfg, cid, types=["review_outcome"])] == ["pass"]
    assert [r.persona for r in runner.reqs].count("implementer") == 1
