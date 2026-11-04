"""SPEC-0408: `finding.key` / `finding.run_id` and the `triage` ledger event, over the real
writers (`store.append_event`, `queue._record_run`, `_apply_triager`, the gate-triage branch)."""
import json

import pytest

from nightwright.agentic import followups as FU
from nightwright.agentic import journey as J
from nightwright.agentic import schemas as SC
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S
from test_agentic_e2e import run_cli
from test_agentic_queue import (RV, TG, _apply, _minors, _pending, _reviewing,  # noqa: F401
                                _rr, _triager_in, _tv, _verdict, opened)

SPEC = "SPEC-0001"


def _findings(cfg, cid):
    return S.read_ledger(cfg, cid, types=["finding"])


def _fe(**kw):
    return dict({"persona": "close", "severity": "minor", "gate": None, "title": "t",
                 "detail": "d", "files": ["a.py"]}, **kw)


# ---- store.append_event stamping -------------------------------------------------------------

def test_append_event_stamps_key_and_null_run_id(opened):
    cfg, cid, _ = opened
    rec = S.append_event(cfg, cid, "finding", _fe(title="Some Title"))
    assert rec["key"] == FU.finding_key(SPEC, "Some Title", ["a.py"])
    assert rec["run_id"] is None and "run_id" in rec


def test_explicit_key_and_run_id_are_kept(opened):
    cfg, cid, _ = opened
    rec = S.append_event(cfg, cid, "finding", _fe(key="mine", run_id="001-x"))
    assert (rec["key"], rec["run_id"]) == ("mine", "001-x")


def test_unreadable_state_still_appends_without_key(opened):
    cfg, cid, _ = opened
    S._state_path(cfg, cid).write_text("{not json", encoding="utf-8")
    rec = S.append_event(cfg, cid, "finding", _fe())
    assert "key" not in rec and rec["run_id"] is None
    assert _findings(cfg, cid)[-1]["title"] == "t"


def test_other_event_types_are_not_stamped(opened):
    cfg, cid, _ = opened
    rec = S.append_event(cfg, cid, "note", {"text": "x"})
    assert "key" not in rec and "run_id" not in rec


def test_triage_is_a_known_type_with_required_keys(opened):
    cfg, cid, _ = opened
    assert "triage" in SC.EVENT_KEYS
    good = {"persona": "review_triager", "run_id": "1", "kind": "review",
            "disposition": "none", "waive": []}
    assert SC.ledger_problems(dict(good, schema=SC.LEDGER_SCHEMA, ts="t", seq=1, campaign=cid,
                                   type="triage", by="tick")) == []
    for k in good:
        with pytest.raises(ValueError):
            S.append_event(cfg, cid, "triage", {x: v for x, v in good.items() if x != k})
    assert S.append_event(cfg, cid, "triage", good)["type"] == "triage"


# ---- _record_run -----------------------------------------------------------------------------

def test_record_run_findings_carry_run_id_and_key(opened):
    cfg, cid, _ = opened
    st = S.read_state(cfg, cid)
    verdict = {"status": "blocked", "summary": "s", "blockers": ["needs X"],
               "must_fix": [{"gate": "test", "title": "Broken", "files": ["b.py"]}],
               "findings": [{"severity": "minor", "title": "Nit one", "files": ["c.py"]}]}
    events = Q._record_run(st, "implementer", "007-implementer", _rr(verdict, cost=None), 1.0)
    found = [p for t, p in events if t == "finding"]
    assert len(found) == 4          # unknown cost, must_fix, blocker, reviewer finding
    assert {f["run_id"] for f in found} == {"007-implementer"}
    for etype, payload in events:
        S.append_event(cfg, cid, etype, payload)
    for f in _findings(cfg, cid):
        assert f["run_id"] == "007-implementer"
        assert f["key"] == FU.finding_key(SPEC, f["title"], f["files"])


# ---- review triager --------------------------------------------------------------------------

def _triages(cfg, cid):
    return S.read_ledger(cfg, cid, types=["triage"])


def test_review_triager_writes_one_triage_event(opened):
    cfg, cid, ctx, st = _pending(opened)
    bad = {"waive": [{"id": f"{RV}#0", "reason": "subjective"}, {"id": "nobody#3", "reason": "x"},
                     {"id": f"{RV}#1", "reason": ""}, {"id": f"{RV}#0", "reason": "again"}]}
    _triager_in(ctx, cfg, cid, _rr(bad))
    [ev] = _triages(cfg, cid)
    assert (ev["persona"], ev["kind"], ev["run_id"], ev["disposition"]) == (
        "review_triager", "review", "003-review_triager", "waive")
    [w] = ev["waive"]
    assert w["id"] == f"{RV}#0" and w["reason"] == "subjective" and w["severity"] == "minor"
    assert w["key"] == FU.finding_key(SPEC, "m0", ["a.py"])
    assert {i["id"]: i["why"] for i in ev["ignored"]} == {
        "nobody#3": "not a finding of this review", f"{RV}#1": "empty reason",
        f"{RV}#0": "duplicate"}


def test_review_triager_waive_key_matches_the_followup_key(opened):
    cfg, cid, ctx, st = _pending(opened)
    _triager_in(ctx, cfg, cid, _rr(_tv(f"{RV}#2")))
    [ev] = _triages(cfg, cid)
    assert ev["waive"][0]["key"] == FU.finding_key(SPEC, "m2", ["a.py"])
    # the same finding recorded as a follow-up gets the same key
    assert FU.make_entry(SPEC, {"title": "m2", "files": ["a.py"]}, "waived")["key"] \
        == ev["waive"][0]["key"]


def test_review_triager_with_nothing_accepted_writes_disposition_none(opened):
    cfg, cid, ctx, st = _pending(opened)
    _triager_in(ctx, cfg, cid, _rr(_tv()))
    [ev] = _triages(cfg, cid)
    assert ev["disposition"] == "none" and ev["waive"] == [] and ev["ignored"] == []


@pytest.mark.parametrize("rr_kw", [
    {"verdict": {"garbage": True}, "ok": False},
    {"verdict": None, "ok": False, "exit_code": 1},
])
def test_bad_review_triager_run_writes_no_triage(opened, rr_kw):
    cfg, cid, ctx, st = _pending(opened)
    kw = dict(rr_kw)
    _triager_in(ctx, cfg, cid, _rr(kw.pop("verdict"), **kw))
    assert _triages(cfg, cid) == []


# ---- gate triager ----------------------------------------------------------------------------

def _to_triaging(cfg, cid, *, failing_required=True, counters=None):
    st = S.read_state(cfg, cid)
    st["state"] = "TRIAGING"
    st["last_gate"] = {"gate_seq": 1, "ok": False, "results": [
        {"name": "lint", "required": failing_required, "ok": False}]}
    st["counters"].update(counters or {})
    S.write_state(cfg, cid, st)


def _gate_event(cfg, cid):
    S.append_event(cfg, cid, "gate", {"gate_seq": 1, "ok": False, "started_at": "t",
                                      "duration_s": 0.1, "results": []})


def _triage_in(ctx, cfg, cid, verdict, ok=True):
    return _apply(ctx, cfg, cid, "triage", persona="gate_triager", run_id="002-gate_triager",
                  run_result=_rr(verdict, ok=ok), budget=0.5)


def test_gate_triager_fix_routes_to_implement(opened):
    cfg, cid, ctx = opened
    _to_triaging(cfg, cid)
    v = _verdict("fix", must_fix=[{"gate": "lint", "title": "Fix it", "files": ["z.py"]}],
                 confidence="high", suspected_flaky=False)
    row, _ = _triage_in(ctx, cfg, cid, v)
    assert row["to"] == "IMPLEMENTING"
    [ev] = _triages(cfg, cid)
    assert (ev["persona"], ev["kind"], ev["run_id"], ev["disposition"], ev["routed_to"]) == (
        "gate_triager", "gate", "002-gate_triager", "fix", "implement")
    assert ev["fix_keys"] == [FU.finding_key(SPEC, "Fix it", ["z.py"])]
    assert (ev["confidence"], ev["suspected_flaky"], ev["waive"]) == ("high", False, [])
    ledger = S.read_ledger(cfg, cid)         # written before the transition it caused
    assert ev["seq"] < [e for e in ledger if e["type"] == "transition"][-1]["seq"]


def test_gate_triager_retry_gate_and_past_the_cap(opened):
    cfg, cid, ctx = opened
    _to_triaging(cfg, cid)
    row, _ = _triage_in(ctx, cfg, cid, _verdict("retry_gate"))
    assert row["to"] == "GATING"
    assert _triages(cfg, cid)[-1]["routed_to"] == "gate"
    assert _triages(cfg, cid)[-1]["disposition"] == "retry_gate"
    _to_triaging(cfg, cid, counters={"gate_retries": Q.GATE_RETRY_MAX})
    row, _ = _triage_in(ctx, cfg, cid, _verdict("retry_gate"))
    assert row["to"] == "IMPLEMENTING"
    last = _triages(cfg, cid)[-1]
    assert (last["disposition"], last["routed_to"]) == ("retry_gate", "implement")


def test_gate_triager_escalate_parks(opened):
    cfg, cid, ctx = opened
    _to_triaging(cfg, cid)
    row, _ = _triage_in(ctx, cfg, cid, _verdict("escalate", fix_brief="human"))
    assert row["to"] == "PARKED"
    [ev] = _triages(cfg, cid)
    assert (ev["disposition"], ev["routed_to"]) == ("escalate", "park")


def _waive_setup(cfg, cid, gate_findings):
    _gate_event(cfg, cid)
    for title in gate_findings:
        S.append_event(cfg, cid, "finding", _fe(persona="gate", gate="lint", title=title,
                                                severity="major"))
    _to_triaging(cfg, cid, failing_required=False)


@pytest.mark.parametrize("titles,expected", [(["only one"], "only one"), ([], None),
                                             (["first", "second"], None)])
def test_gate_triager_waive_key_resolution(opened, titles, expected):
    cfg, cid, ctx = opened
    S.append_event(cfg, cid, "finding", _fe(persona="gate", gate="lint", title="old round"))
    _waive_setup(cfg, cid, titles)
    row, _ = _triage_in(ctx, cfg, cid, _verdict("waive", waived=[{"gate": "lint",
                                                                  "reason": "optional"}]))
    assert row["to"] == "CLOSING"
    [ev] = _triages(cfg, cid)
    assert ev["routed_to"] == "close"
    [w] = ev["waive"]
    assert (w["gate"], w["reason"]) == ("lint", "optional")
    assert w["key"] == (FU.finding_key(SPEC, expected, ["a.py"]) if expected else None)


def test_gate_triager_reads_waived_not_waive(opened):
    cfg, cid, ctx = opened
    _waive_setup(cfg, cid, ["only one"])
    _triage_in(ctx, cfg, cid, _verdict("waive", waive=[{"gate": "lint", "reason": "x"}]))
    assert _triages(cfg, cid)[0]["waive"] == []


def test_malformed_gate_triager_verdict_writes_no_triage(opened):
    cfg, cid, ctx = opened
    _to_triaging(cfg, cid)
    row, _ = _triage_in(ctx, cfg, cid, {"garbage": 1}, ok=False)
    assert row["reason"] == "triage_retry" and _triages(cfg, cid) == []


# ---- backward compatibility ------------------------------------------------------------------

def test_old_ledger_lines_stay_and_all_readers_cope(opened, monkeypatch):
    cfg, cid, ctx = opened
    old = {"schema": SC.LEDGER_SCHEMA, "ts": "2026-01-01T00:00:00+00:00", "seq": 9000,
           "campaign": cid, "type": "finding", "by": "tick", "persona": "close",
           "severity": "minor", "gate": None, "title": "old", "detail": "d", "files": []}
    path = S._ledger_path(cfg, cid)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(old) + "\n")
    before = path.read_bytes()
    S.append_event(cfg, cid, "finding", _fe())
    S.append_event(cfg, cid, "triage", {"persona": "review_triager", "run_id": "1",
                                        "kind": "review", "disposition": "none", "waive": []})
    assert path.read_bytes().startswith(before)
    events = S.read_ledger(cfg, cid)
    assert [SC.ledger_problems(e) for e in events] == [[] for _ in events]
    old_back = next(e for e in events if e.get("title") == "old" and e["seq"] == 9000)
    assert "key" not in old_back and "run_id" not in old_back
    r = run_cli(cfg, monkeypatch, ["campaign", "log", cid])
    assert r.exit_code == 0 and "triage" in r.output
    assert "triage" in J.event_sentence(events[-1])


# ---- review round 1 follow-ups ----------------------------------------------------------------

def test_gate_triager_invalid_verdict_past_retry_cap_writes_no_triage(opened):
    cfg, cid, ctx = opened
    _to_triaging(cfg, cid, counters={"triage_retries": Q.TRIAGE_RETRY_MAX})
    row, _ = _triage_in(ctx, cfg, cid, {"garbage": 1}, ok=False)
    assert row["to"] == "PARKED" and row["reason"] == "triage_escalated"
    assert _triages(cfg, cid) == []


def test_finding_key_tolerates_non_string_files():
    assert FU.finding_key(SPEC, "t", ["b.py", "a.py"]) == FU.finding_key(SPEC, "t", ["a.py", "b.py"])
    assert FU.finding_key(SPEC, "t", [None, "a.py"]) == FU.finding_key(SPEC, "t", ["a.py"])
    FU.finding_key(SPEC, "t", [{"path": "a.py"}, 3, None])      # does not raise


def test_malformed_verdict_files_do_not_break_the_append(opened):
    cfg, cid, _ = opened
    st = S.read_state(cfg, cid)
    verdict = {"must_fix": [{"title": "x", "files": [{"path": "a.py", "line": 3}]},
                            {"title": "y", "files": [None, 7]}]}
    events = Q._record_run(st, "implementer", "008-implementer", _rr(verdict), 1.0)
    for etype, payload in events:
        S.append_event(cfg, cid, etype, payload)
    found = _findings(cfg, cid)
    assert [f["title"] for f in found] == ["x", "y"]
    assert all(f["run_id"] == "008-implementer" and isinstance(f["key"], str) for f in found)
