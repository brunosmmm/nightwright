"""SPEC-0468: usage pause, adversarial follow-ups, resume targets, one loop
auto-resume, and a cursor review-round budget."""
import json
from types import SimpleNamespace

from nightwright.agentic import events as EV
from nightwright.agentic import persona_gate as PG
from nightwright.agentic import queue as Q
from nightwright.agentic import redmain as RM
from nightwright.agentic.runner import parse_result
import pytest

pytestmark = pytest.mark.fast

CONF, TEST, ADV = "conformance_reviewer", "test_reviewer", "adversarial_reviewer"
PROFILE = {"loop_max": 3, "reviewers": [CONF, TEST, ADV],
           "review": {"pass_score": 90}}


def _v(verdict, *findings, score=90):
    return {"verdict": verdict, "score": score, "ac_assessment": [], "fix_brief": "fix",
            "findings": list(findings)}


def _f(severity, title, files=("a.py",)):
    return {"severity": severity, "title": title, "files": list(files)}


def test_usage_error_message_becomes_the_result_tail():
    raw = json.dumps({
        "type": "result", "subtype": "error", "is_error": True, "result": "",
        "error_message": "Increase limits for faster responses You're out of usage.",
    })
    parsed = parse_result(raw, {}, exit_code=1)
    assert "out of usage" in parsed["result_text"]
    assert parsed["is_error"] is True
    assert EV._is_transient(parsed["result_text"])
    assert EV._is_transient("error") is False


def test_usage_pause_holds_the_gate_and_blocks_fixforward(tmp_path, monkeypatch):
    cfg = {"data_dir": str(tmp_path)}
    assert PG.usage_until(cfg) is None
    PG.note_usage(cfg, now=1_000.0)
    assert PG.usage_until(cfg, now=1_000.0) is not None
    assert PG.usage_until(cfg, now=1_000.0 + PG.USAGE_PAUSE_S + 1) is None
    PG.note_usage(cfg, now=1_000.0)
    gate = PG.Gate(cfg, ["c1"], now=1_100.0)
    assert gate.admit({"id": "c1"}, Q.Step("implement", persona="implementer")) is None
    assert gate.holds["c1"]["held_by"] == "usage_pause"
    assert PG.held_by(cfg, {"id": "c1"}, now=1_100.0, holds=gate.holds) == "usage_pause"

    monkeypatch.setattr(PG, "usage_until", lambda cfg, now=None: 5.0)
    action, why = RM.step({"agentic": {}}, "Meta-Tools",
                          red={"open": True, "episode": 4, "fixes": []})
    assert action == "none" and "usage" in why

    ok = SimpleNamespace(is_error=False, interrupted=False, result_text="", raw_path=None)
    assert PG.is_usage_exhausted(ok) is False
    wall = SimpleNamespace(is_error=True, interrupted=False,
                           result_text="You're out of usage.", raw_path=None)
    assert PG.is_usage_exhausted(wall) is True


def test_adversarial_major_becomes_advisory_when_conformance_passed():
    tri = Q.triage_reviews({
        CONF: _v("pass", score=97),
        TEST: _v("pass", _f("minor", "name"), score=88),
        ADV: _v("fix", _f("major", "clip the timeline"), score=54),
    }, PROFILE)
    assert tri["outcome"] == "pass"
    assert ADV not in tri["fixing"]
    assert any(f["title"] == "clip the timeline" and f["severity"] == "major"
               for f in tri["advisory"])
    assert tri["score"] == 96                          # the minor still counts; the major does not


def test_a_test_reviewer_major_still_blocks_and_the_adversarial_one_does_not():
    tri = Q.triage_reviews({
        CONF: _v("pass", score=94),
        TEST: _v("fix", _f("major", "missing pilot", files=("t.py",)), score=70),
        ADV: _v("fix", _f("major", "clip"), score=58),
    }, PROFILE)
    assert tri["outcome"] == "fix"
    assert TEST in tri["fixing"] and ADV not in tri["fixing"]
    assert any(f["title"] == "clip" for f in tri["advisory"])
    assert any(f["title"] == "missing pilot" for f in tri["blocking"])


def test_adversarial_must_fix_and_a_failing_conformance_still_block():
    must = Q.triage_reviews({
        CONF: _v("pass", score=94),
        TEST: _v("pass", score=90),
        ADV: _v("fix", _f("must_fix", "drops the row"), score=40),
    }, PROFILE)
    assert must["outcome"] == "fix"
    assert any(f["severity"] == "must_fix" for f in must["blocking"])

    conf = Q.triage_reviews({
        CONF: _v("fix", _f("major", "ac missing"), score=70),
        TEST: _v("pass", score=90),
        ADV: _v("fix", _f("major", "clip"), score=50),
    }, PROFILE)
    assert conf["outcome"] == "fix"
    assert any(f["title"] == "clip" for f in conf["blocking"])


def test_resume_targets_refuse_another_implement_loop():
    Q.reject_resume_target("c", "diverging_rework", "SPEC_WRITING")
    Q.reject_resume_target("c", "spec_status", "CLOSING")
    Q.reject_resume_target("c", "round_budget", "SPEC_WRITING")
    for reason, target in (("diverging_rework", "IMPLEMENTING"),
                           ("round_budget", "REVIEWING"),
                           ("spec_status", "IMPLEMENTING")):
        try:
            Q.reject_resume_target("c", reason, target)
        except ValueError:
            continue
        raise AssertionError(f"{reason} -> {target} was allowed")


def test_loop_auto_resume_once_only_when_the_must_fix_set_shrinks():
    counters = {}
    Q.note_must_fix_baseline(counters, Q.must_fix_titles([
        {"severity": "must_fix", "title": "A"},
        {"severity": "must_fix", "title": "B"},
    ]))
    assert Q.loop_can_auto_resume(counters, ["a"]) is True
    assert Q.loop_can_auto_resume(counters, ["a", "b"]) is False
    assert Q.loop_can_auto_resume(counters, ["a", "b", "c"]) is False
    assert Q._maybe_loop_auto_resume({}, counters, [{"severity": "must_fix", "title": "A"}])
    assert counters["loop_auto_resumes"] == 1 and counters["loops"] == 0
    assert Q.loop_can_auto_resume(counters, []) is False


def test_cursor_review_round_budget_is_twice_the_loop_max():
    assert Q.cursor_review_cap(PROFILE) == 6
    counters = {"review_rounds": 6}
    assert Q.review_rounds_spent(counters, PROFILE) is True
    assert Q.review_rounds_spent({"review_rounds": 5}, PROFILE) is False
    assert "round_budget" in Q.PARK_REASONS and "spec_status" in Q.PARK_REASONS


