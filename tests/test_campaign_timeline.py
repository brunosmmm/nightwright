"""SPEC-0433: the campaign story (`agentic/timeline.py`) over synthetic ledgers, plus a sweep over
a copy of a real campaign ledger when the live data dir exists."""
import ast
import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from nightwright.agentic import campaign_export as CE
from nightwright.agentic import timeline as T

pytestmark = pytest.mark.fast

T0 = datetime.fromisoformat("2026-11-04T15:00:00-05:00")


class Ledger:
    def __init__(self):
        self.events = []
        self.t = 0.0

    def ev(self, type_, at=None, by="tick", **kw):
        if at is not None:
            self.t = at
        e = {"schema": "nw.ledger.event.v1", "ts": (T0 + timedelta(seconds=self.t)).isoformat(),
             "seq": len(self.events) + 1, "campaign": "C-1", "type": type_, "by": by, **kw}
        self.events.append(e)
        return e

    def go(self, frm, to, reason, at=None):
        return self.ev("transition", at=at, **{"from": frm, "to": to, "reason": reason})

    def run(self, persona, rid, model, dur, cost, verdict, at=None, start_at=None, **end):
        self.ev("run.start", at=start_at if start_at is not None else at, persona=persona,
                run_id=rid, model=model)
        if start_at is not None and at is not None:
            self.t = at
        self.ev("run.end", persona=persona, run_id=rid, exit_code=0, is_error=False,
                timed_out=False, interrupted=False, duration_s=dur, cost_usd=cost,
                charged_usd=cost, usage={"input_tokens": 2}, session_id="s", verdict_ok=True,
                verdict=verdict, **end)
        self.ev("cost", persona=persona, delta_usd=cost, total_usd=self.total(cost),
                cap_usd=90.0)

    def total(self, add):
        prev = [e["total_usd"] for e in self.events if e["type"] == "cost"]
        return round((prev[-1] if prev else 73.99) + add, 4)

    def story(self, **kw):
        return T.story(self.events, campaign="C-1", **kw)


def lines(doc):
    return [ln for _, _, ln in T.iter_lines(doc)]


def texts(doc):
    return [ln["text"] for ln in lines(doc)]


def covered(doc):
    return {s for ln in lines(doc) for s in ln["seqs"]}


def test_gate_triager_collapses_to_one_line():
    lg = Ledger()
    lg.go("CLOSING", "TRIAGING", "regate_failed")
    lg.run("gate_triager", "092-gate_triager", "sonnet", 8.662, 0.1298214,
           {"disposition": "retry_gate", "confidence": "medium", "suspected_flaky": True,
            "fix_brief": "killed"})
    lg.ev("triage", persona="gate_triager", run_id="092-gate_triager", kind="gate",
          disposition="retry_gate", routed_to="gate", confidence="medium", suspected_flaky=True,
          waive=[], fix_keys=[])
    doc = lg.story()
    line = [ln for ln in lines(doc) if ln["kind"] == "run"][0]
    assert line["text"] == "Gate triager (sonnet, 9s, $0.13): retry — suspected flaky (medium)"
    assert line["seqs"] == [2, 3, 4, 5] and line["ref_seq"] == 3
    assert covered(doc) == {e["seq"] for e in lg.events if e["type"] != "transition"}


def _review_round(lg, start, outcome=True, triage=True):
    lg.go("GATING", "REVIEWING", "gates_green", at=start)
    for p in ("conformance_reviewer", "test_reviewer", "adversarial_reviewer"):
        lg.ev("run.start", at=start, persona=p, run_id=f"0-{p}", model="opus")
    specs = [("conformance_reviewer", 38.0, 0.804877, 90, [("minor", "DoD box", "k1")]),
             ("test_reviewer", 33.5, 0.731958, 90, [("minor", "Tail-429", "k2"),
                                                    ("minor", "Wall-clock", "k3")]),
             ("adversarial_reviewer", 190.0, 1.6465162, 84, [("minor", "Gate reserves", "k4"),
                                                               ("nit", "Holds", "k5")])]
    for p, dur, cost, score, fs in specs:
        lg.ev("run.end", at=start + dur, persona=p, run_id=f"0-{p}", duration_s=dur,
              charged_usd=cost, is_error=False, verdict={"verdict": "pass", "score": score})
        lg.ev("cost", persona=p, delta_usd=cost, total_usd=lg.total(cost), cap_usd=90.0)
        for sev, title, key in fs:
            lg.ev("finding", persona=p, severity=sev, gate=None, title=title, files=["a.py"],
                  run_id=f"0-{p}", key=key)
    lg.t = start + 190
    if triage:
        lg.run("review_triager", "1-review_triager", "sonnet", 10.969, 0.1239905,
               {"waive": [{"id": "test_reviewer#1", "reason": "negligible"}]})
        lg.ev("triage", persona="review_triager", run_id="1-review_triager", kind="review",
              disposition="waive", waive=[{"id": "test_reviewer#1", "key": "k3",
                                           "reason": "negligible"}])
        lg.ev("note", text="waived test_reviewer#1: negligible", author="queue")
    if outcome:
        lg.ev("review_outcome", loop=2, score=92, pass_score=90, outcome="pass", major=0,
              must_fix=0)
    lg.ev("note", text="review score 92/90 pass", author="queue")


def test_review_round_is_one_line_with_scores_findings_and_waivers():
    lg = Ledger()
    _review_round(lg, 0)
    doc = lg.story()
    rnd = [ln for ln in lines(doc) if ln["kind"] == "review_round"]
    assert len(rnd) == 1
    rnd = rnd[0]
    assert rnd["text"] == ("Review round — 3 reviewers (3m 10s, $3.18): ✓ conformance 90 ✓ tests "
                           "90 ✓ adversarial 84 → pass 92/90 · 5 findings (4 minor, 1 nit; 1 "
                           "waived by triager)")
    assert rnd["tone"] == "ok"
    waived = [c for c in rnd["children"] if c.get("waived")]
    assert len(waived) == 1 and waived[0]["text"].startswith("minor · tests: Wall-clock (a.py)")
    assert "waived by triager: negligible" in waived[0]["text"]
    assert [c["text"].split(" (")[0] for c in rnd["children"] if c.get("run_id")] == [
        "Conformance reviewer", "Test reviewer", "Adversarial reviewer"]
    tri = [ln for ln in lines(doc) if ln.get("persona") == "review_triager"][0]
    assert tri["text"] == "Review triager (sonnet, 11s, $0.12): waived 1 finding"
    assert covered(doc) == {e["seq"] for e in lg.events if e["type"] != "transition"}


def test_review_round_falls_back_to_the_score_note_and_id_waivers():
    lg = Ledger()
    _review_round(lg, 0, outcome=False)
    for e in lg.events:                         # an older writer: no finding keys
        e.pop("key", None)
        if e["type"] == "triage":
            e["waive"][0].pop("key")
    rnd = [ln for ln in lines(lg.story()) if ln["kind"] == "review_round"][0]
    assert "→ pass 92/90" in rnd["text"] and "1 waived by triager" in rnd["text"]


def test_close_regate_rebase_and_noise():
    lg = Ledger()
    lg.ev("open", by="human", spec_id="SPEC-1", project="P", profile="thorough", base="main")
    lg.go("REVIEWING", "CLOSING", "review_pass", at=10)
    lg.ev("finding", persona="close", severity="info", gate="rebase",
          title="rebase conflict; resolver dispatched", detail="", files=["tests/x.py"])
    lg.run("conflict_resolver", "091-conflict_resolver", "opus", 124.94, 1.0228340000000002,
           {"status": "resolved", "summary": "rebased onto 318652ba"})
    lg.ev("rebase_resolved", run_id="091-conflict_resolver", target_sha="318652ba431f3c6c",
          conflicts=["tests/x.py"], resolutions=[{"path": "tests/x.py", "strategy": "both",
                                                  "how": "kept both"}], summary="ok", head="h")
    lg.ev("gate.reuse_declined", gate="pytest", rule="R3", detail="branch files differ")
    lg.ev("gate", gate_seq=29, ok=False, duration_s=2430.727, worker="box", results=[
        {"name": "spec-lint", "required": True, "ok": True, "exit_code": 0, "duration_s": 8},
        {"name": "pytest", "required": True, "ok": False, "exit_code": 143,
         "duration_s": 2400}])
    lg.ev("gate", gate_seq=30, ok=True, duration_s=660, worker=None, results=[
        {"name": "pytest", "required": True, "ok": True, "duration_s": 650}])
    lg.ev("gate.reused", gate="pytest", from_gate="pytest-affected", from_gate_seq=2)
    doc = lg.story()
    got = texts(doc)
    assert ("Rebase onto main (318652ba) conflicted (tests/x.py) → resolved by conflict_resolver "
            "(opus, 2m 5s, $1.02)") in got
    assert "Re-gate #29 on box — FAILED after 40m 31s (pytest)" in got
    assert "Re-gate #30 — passed (11m)" in got
    gate = [ln for ln in lines(doc) if ln.get("gate_seq") == 29][0]
    assert any("reuse declined (R3" in c["text"] for c in gate["children"])
    assert any(c["text"] == "✗ pytest (40m, exit 143)" for c in gate["children"])
    noise = [ln for ln in lines(doc) if ln["kind"] == "gate_note"]
    assert noise and noise[0]["quiet"] and "reused from pytest-affected #2" in noise[0]["text"]
    assert not any(ln["kind"] == "event" for ln in lines(doc))
    grp = doc["loops"][0]["groups"][-1]
    assert grp["title"] == "Closing (rebase, re-gate, merge)" and grp["states"] == ["CLOSING"]


def test_loops_groups_paths_and_cost_deltas():
    lg = Ledger()
    lg.go("QUEUED", "OPENING", "admitted", at=0)
    lg.go("OPENING", "IMPLEMENTING", "opened", at=5)
    lg.run("implementer", "001-implementer", "opus", 600, 2.0,
           {"status": "done", "summary": "did it"}, start_at=5, at=605)
    lg.ev("commit", sha="abcdef0123456789", message="nw: implementer run 001")
    lg.go("IMPLEMENTING", "GATING", "implemented", at=606)
    lg.ev("gate", at=900, gate_seq=1, ok=False, duration_s=290, worker="box",
          results=[{"name": "pytest", "ok": False, "required": True, "exit_code": 1}])
    lg.go("GATING", "TRIAGING", "gates_failed", at=900)
    lg.run("gate_triager", "002-gate_triager", "sonnet", 30, 0.2,
           {"disposition": "fix", "confidence": "high", "must_fix": [{"title": "t"}]}, at=930)
    lg.ev("finding", persona="gate_triager", severity="must_fix", gate="pytest", title="t")
    lg.go("TRIAGING", "IMPLEMENTING", "triage_fix", at=931)
    lg.run("implementer", "003-implementer", "opus", 60, 1.0, {"status": "done"}, at=991)
    doc = lg.story(state={"state": "IMPLEMENTING", "cost": {"total_usd": 77.19, "cap_usd": 90}},
                   now=(T0 + timedelta(seconds=1000)).isoformat())
    assert [lp["title"] for lp in doc["loops"]] == ["Loop 1", "Loop 2"]
    g = [x for x in doc["loops"][0]["groups"]]
    assert [x["phase"] for x in g] == ["queue", "implement", "gate"]
    assert g[2]["path"] == "GATING → TRIAGING → IMPLEMENTING"
    assert g[2]["title"] == "Gating & triage"
    assert g[2]["header"] == "Gating & triage · GATING → TRIAGING → IMPLEMENTING · 5m 25s · $75.99 → $76.19 of $90.00"
    assert g[1]["header"].endswith("$73.99 → $75.99 of $90.00")
    assert doc["loops"][1]["groups"][0]["end"] == (T0 + timedelta(seconds=1000)).isoformat()
    assert not any(ln["kind"] == "transition" for ln in lines(doc))
    impl = [ln for ln in lines(doc) if ln.get("run_id") == "001-implementer"][0]
    assert impl["text"] == "Implementer (opus, 10m, $2.00): done — did it"
    assert impl["children"][0]["text"].startswith("commit abcdef01: ")
    tri = [ln for ln in lines(doc) if ln.get("run_id") == "002-gate_triager"][0]
    assert tri["text"] == ("Gate triager (sonnet, 30s, $0.20): fix — 1 must_fix (high) · 1 finding "
                           "(1 must_fix)")
    assert doc["header"]["loops"] == 2 and doc["header"]["cost_usd"] == 77.19
    assert all(e["seq"] in {s for lp in doc["loops"] for g in lp["groups"] for s in g["seqs"]}
               for e in lg.events)


def test_parks_escalations_answers_and_cost_raises():
    lg = Ledger()
    lg.go("REVIEWING", "PARKED", "loop_exhausted")
    lg.ev("park", reason="triage_escalated", detail="needs a human", resume_to="GATING")
    lg.ev("human_decision", by="human", escalation="ESC-0029", kind="choose", choice=1,
          label="Defer to IDEA-972", text=None, waive=[], attest=[], note=None)
    lg.ev("note", by="agent:conductor", author="conductor", text="decision resume (x): go",
          decision={"seq": 1, "kind": "resume", "rule": "x"})
    lg.ev("resume", by="conductor", to="IMPLEMENTING", note="answered", cap_usd=60.0,
          loops_reset=True)
    lg.ev("cap_raised", by="queue", **{"from": 75.0, "to": 90.0}, why="score rose 72→88; raise",
          rule="cost_cap_policy")
    got = {ln["kind"]: ln for ln in lines(lg.story())}
    assert got["escalation"]["text"].startswith("Parked by tick: triage_escalated — needs a human")
    assert got["escalation"]["tone"] == "bad"
    assert got["answer"]["text"] == "Human answered ESC-0029 (choose): Defer to IDEA-972"
    assert got["resume"]["text"] == "Resumed by conductor → IMPLEMENTING, cap $60.00, loops reset: answered"
    assert got["resume"]["children"][0]["text"] == "Conductor decision resume (x): go"
    assert got["cost_raise"]["text"] == "Cost cap raised $75.00 → $90.00 (score rose 72→88) by cost_cap_policy"


def test_unknown_and_malformed_events_never_raise_and_never_hide():
    lg = Ledger()
    lg.ev("frobnicate", a=1, b="x" * 200)
    lg.ev("run.end", run_id=None, verdict="not a dict", duration_s="??")
    lg.ev("gate", results="nope", ok=None)
    lg.ev("finding", severity=None, title=None, files="x")
    lg.ev("cost", total_usd="bad")
    lg.events.append({"type": "note", "text": "no seq"})
    lg.events.append("not an event")
    doc = lg.story()
    json.dumps(doc)
    assert doc["unknown_types"] == {"frobnicate": 1}
    fro = [ln for ln in lines(doc) if ln["kind"] == "event"][0]
    assert fro["text"].startswith("frobnicate (tick): a=1 b=xxx") and len(fro["text"]) < 200
    assert {1, 2, 3, 4, 5} <= covered(doc)
    assert T.story([])["loops"] == []


def test_text_lines_collapse_quiet_and_children_and_expand_shows_them():
    lg = Ledger()
    lg.ev("backlog", ready=False, blocked_by=["waiting on SPEC-9"])
    lg.ev("backlog", ready=True, blocked_by=[])
    lg.ev("gate", gate_seq=1, ok=True, duration_s=60, results=[{"name": "a", "ok": True}])
    doc = lg.story()
    short = T.text_lines(doc)
    assert (2, "Gate #1 — passed (1m)  [+1]", "ok") in short
    assert (2, "(1 quiet detail)", "dim") in short
    full = [t for _, t, _ in T.text_lines(doc, expand=True)]
    assert "Backlog: ready (was blocked: waiting on SPEC-9)" in full and "✓ a (?)" in full


def test_fix_loop_predicate_matches_the_export():
    assert T.FIX_REASONS == CE.FIX_REASONS
    assert T.is_fix_loop({"type": "transition", "to": "IMPLEMENTING", "reason": "review_fix"})
    assert not T.is_fix_loop({"type": "transition", "to": "IMPLEMENTING", "reason": "resumed"})


def test_the_module_imports_only_the_standard_library():
    tree = ast.parse(Path(T.__file__).read_text())
    mods = {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    mods |= {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
             for a in n.names}
    mods.discard("__future__")
    assert mods and all(m in sys.stdlib_module_names for m in mods), mods


REAL = Path.home() / ".local/share/nightwright/agentic/campaigns/SPEC-0345-20261101-e596"


@pytest.mark.skipif(not (REAL / "ledger.jsonl").is_file(), reason="no live data dir here")
def test_a_copied_real_ledger_tells_the_story(tmp_path):
    led = tmp_path / "ledger.jsonl"
    shutil.copyfile(REAL / "ledger.jsonl", led)            # read a copy, never the live file
    events = [json.loads(x) for x in led.read_text().splitlines() if x.strip()]
    doc = T.story(events, campaign=REAL.name)
    json.dumps(doc)
    assert doc["unknown_types"] == {} and doc["lines"] < len(events) / 3
    trans = {e["seq"] for e in events if e["type"] == "transition"}
    assert covered(doc) | trans == {e["seq"] for e in events}
    got = texts(doc)
    assert "Re-gate #29 on box — FAILED after 40m 31s (pytest)" in got
    assert ("Rebase onto main (318652ba) conflicted (tests/test_remote_gates.py) → resolved by "
            "conflict_resolver (opus, 2m 5s, $1.02)") in got
    assert "Gate triager (sonnet, 9s, $0.13): retry — suspected flaky (medium)" in got
    assert any(t.startswith("Review round — 3 reviewers (3m 10s, $3.18): ✓ conformance 90 ✓ "
                            "tests 90 ✓ adversarial 84 → pass 92/90") for t in got)
