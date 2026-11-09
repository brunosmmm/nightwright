"""SPEC-0406 unit tests for the pure history derivation."""
import datetime as dt
import hashlib
import json

from nightwright.agentic import history as H
from nightwright.agentic import schemas as SC
from nightwright.agentic import spec_review as SR
import pytest

pytestmark = pytest.mark.fast


UTC = dt.timezone.utc


def stamp(second):
    return dt.datetime(2026, 1, 1, tzinfo=UTC) + dt.timedelta(seconds=second)


def transition(second, frm, to, reason="x"):
    return {"type": "transition", "ts": stamp(second).isoformat(), "from": frm, "to": to,
            "reason": reason}


def test_phase_visits_time_and_loop_predicate():
    events = [
        transition(10, "QUEUED", "IMPLEMENTING"),
        transition(20, "IMPLEMENTING", "GATING"),
        transition(30, "GATING", "IMPLEMENTING", "triage_fix"),
        transition(40, "IMPLEMENTING", "REVIEWING"),
        transition(50, "REVIEWING", "IMPLEMENTING", "resume"),
        transition(60, "IMPLEMENTING", "REVIEWING"),
        transition(70, "REVIEWING", "IMPLEMENTING", "review_fix"),
    ]
    state = {"state": "IMPLEMENTING"}
    visits = H.phase_visits(state, events, stamp(0), stamp(80))
    assert [v.loop for v in visits] == [0, 0, 0, 1, 1, 1, 1, 2]
    assert max(v.loop for v in visits) == H.count_loops(events) == 2
    assert H.time_in_state(state, events, stamp(0), stamp(80)) == {
        "QUEUED": 10, "IMPLEMENTING": 40, "GATING": 10, "REVIEWING": 20,
    }


def test_filter_index_ties_dates_and_values():
    rows = [
        {"campaign": "A", "project": "P", "outcome": "merged", "spec_id": "SPEC-1",
         "closed_at": "2026-01-02T00:00:00+00:00", "created_at": "x", "cost_usd": 2},
        {"campaign": "B", "project": "Q", "outcome": None, "spec_id": "SPEC-2",
         "closed_at": None, "created_at": "2026-01-03T00:00:00+00:00", "cost_usd": 1},
    ]
    assert [r["campaign"] for r in H.filter_index(
        rows, project="P", state="merged", spec="SPEC-1", since="2026-01-02",
        until="2026-01-02", min_cost=2,
    )] == ["A"]
    assert [r["campaign"] for r in H.filter_index(
        rows, project=None, state="live", spec=None, since=None, until=None, min_cost=None,
    )] == ["B"]


def test_precedence_split_spill_and_cost_at_start_phase(tmp_path):
    cfg = {"data_dir": str(tmp_path)}
    runner = tmp_path / "agentic" / "runner.jsonl"
    runner.parent.mkdir()
    runner.write_text("\n".join(json.dumps(row) for row in (
        {"ts": stamp(18).isoformat(), "kind": "stop"},
        {"ts": stamp(20).isoformat(), "kind": "start"},
    )) + "\n")
    events = [
        {"type": "run.start", "ts": stamp(5).isoformat(), "run_id": "001", "persona": "p"},
        {"type": "run.end", "ts": stamp(12).isoformat(), "run_id": "001", "persona": "p",
         "duration_s": 7},
        {"type": "gate", "ts": stamp(7).isoformat(), "started_at": stamp(7).isoformat(),
         "duration_s": 9},
        {"type": "gate_waiting", "ts": stamp(18).isoformat(), "waited_s": 12},
    ]
    visits = [
        H.Visit("implement", "IMPLEMENTING", stamp(0), stamp(10), 0),
        H.Visit("gate", "GATING", stamp(10), stamp(20), 0),
    ]
    runs = [{"phase": "implement", "cost_usd": 2}]
    phases = {row["phase"]: row for row in H._phase_rows(cfg, events, visits, runs, [])}
    assert phases["implement"]["run_s"] == 5
    assert phases["implement"]["gate_wait_s"] == 0
    assert phases["implement"]["other_s"] == 5
    assert phases["gate"]["run_s"] == 2
    assert phases["gate"]["gate_s"] == 4
    assert phases["gate"]["gate_wait_s"] == 2
    assert phases["gate"]["runner_down_s"] == 2
    assert phases["implement"]["cost_usd"] == 2
    assert phases["gate"]["cost_usd"] == 0
    for row in phases.values():
        assert sum(row[key] for key in (
            "run_s", "gate_s", "gate_wait_s", "runner_down_s", "other_s"
        )) == row["wall_s"]


def test_dominant_tie_zero_and_parked_working():
    def rows(values, costs=None):
        costs = costs or {}
        return [{"phase": phase, "wall_s": values.get(phase, 0),
                 "cost_usd": costs.get(phase, 0)} for phase in H.PHASES]
    assert H._dominant(rows({})) == {"wall": None, "cost": None, "working": None}
    tied = H._dominant(rows({"queue": 10, "spec": 10}, {"queue": 1, "spec": 1}))
    assert tied == {"wall": "queue", "cost": "queue", "working": "spec"}
    parked = H._dominant(rows({"parked": 20, "gate": 12, "review": 10}))
    assert parked == {"wall": "parked", "cost": None, "working": "gate"}


def test_field_inferred_gate_and_triage_links(tmp_path):
    cfg = {"data_dir": str(tmp_path)}
    cid = "C-1"
    run_dir = tmp_path / "agentic" / "campaigns" / cid / "runs" / "003-review_triager"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "verdict": {"waive": [{"id": "reviewer#0", "reason": "ok"}]}
    }))
    events = [
        {"type": "run.end", "seq": 1, "ts": stamp(1).isoformat(), "persona": "reviewer",
         "run_id": "001-reviewer"},
        {"type": "finding", "seq": 2, "ts": stamp(2).isoformat(), "campaign": cid,
         "persona": "reviewer", "severity": "major", "title": "old", "files": ["a.py"]},
        {"type": "gate", "seq": 3, "ts": stamp(3).isoformat(), "gate_seq": 7},
        {"type": "finding", "seq": 4, "ts": stamp(4).isoformat(), "campaign": cid,
         "persona": "gate_triager", "severity": "must_fix", "title": "gate", "files": [],
         "gate": "pytest"},
        {"type": "finding", "seq": 5, "ts": stamp(5).isoformat(), "campaign": cid,
         "persona": "reviewer", "severity": "minor", "title": "new", "files": [],
         "run_id": "002-reviewer", "key": "explicit"},
    ]
    runs = [{"run_id": "003-review_triager", "persona": "review_triager",
             "started_at": stamp(6).isoformat()}]
    records = H._ledger_records(cfg, cid, {"spec_id": "SPEC-1", "state": "MERGED"},
                                events, runs, [])
    findings = [record for record in records if record["kind"] == "finding"]
    assert findings[0]["links"]["run_id"] == "001-reviewer"
    assert findings[0]["linked_by"] == "inferred" and findings[0]["status"] == "waived"
    assert findings[0]["links"]["resolved_by"] == "run:C-1:003-review_triager"
    assert findings[1]["links"]["gate_seq"] == 7 and findings[1]["links"]["run_id"] is None
    assert findings[2]["links"]["key"] == "explicit"
    assert findings[2]["linked_by"] == "field" and findings[2]["status"] == "open"


def test_text_link_never_guesses():
    campaigns = ["C-ONE", "C-TWO"]
    one = H._base_record("decisions", "1", "decision", {
        "text": "context", "rationale": "about C-ONE.",
    })
    two = H._base_record("decisions", "2", "decision", {"text": "C-ONE and C-TWO"})
    assert H._campaign_text_link(one, campaigns) == "text" and one["campaign"] == "C-ONE"
    assert H._campaign_text_link(two, campaigns) is None and two["campaign"] is None


def test_unknown_phase_runner_outage_and_mixed_timestamps(tmp_path):
    cfg = {"data_dir": str(tmp_path)}
    runner = tmp_path / "agentic" / "runner.jsonl"
    runner.parent.mkdir()
    runner.write_text("\n".join(json.dumps(row) for row in (
        {"ts": stamp(1).isoformat(), "kind": "stop"},
        {"ts": stamp(2).isoformat(), "kind": "crash"},
        {"ts": stamp(5).isoformat(), "kind": "start"},
    )) + "\n")
    visits = [H.Visit("other", "UNKNOWN_X", stamp(0), stamp(5), 0)]
    phases = H._phase_rows(cfg, [], visits, [], [])
    other = phases[-1]
    assert other["phase"] == "other"
    assert other["wall_s"] == 5
    assert other["runner_down_s"] == 4
    assert sum(other[key] for key in (
        "run_s", "gate_s", "gate_wait_s", "runner_down_s", "other_s"
    )) == other["wall_s"]

    mixed = H.phase_visits(
        {"state": "IMPLEMENTING"},
        [transition(1, "QUEUED", "IMPLEMENTING")],
        dt.datetime(2026, 1, 1), stamp(2),
    )
    assert [visit.wall_s if hasattr(visit, "wall_s") else H._seconds(visit.start, visit.end)
            for visit in mixed] == [1, 1]


def test_terminal_transition_closes_header_without_close_event(tmp_path):
    cid = "C-TERMINAL"
    root = tmp_path / "agentic" / "campaigns" / cid
    root.mkdir(parents=True)
    (root / "state.json").write_text(json.dumps({
        "id": cid, "state": "MERGED", "created_at": "2026-01-01T00:00:00",
        "project": "P", "spec_id": "SPEC-1",
    }))
    (root / "ledger.jsonl").write_text(json.dumps({
        "type": "transition", "seq": 1, "ts": stamp(10).isoformat(),
        "from": "QUEUED", "to": "MERGED", "reason": "closed",
    }) + "\n")
    doc = H.campaign_history({"data_dir": str(tmp_path)}, cid, now=stamp(20))
    assert doc["header"]["closed_at"] == stamp(10).isoformat()
    assert doc["header"]["wall_s"] == 10
    assert sum(row["wall_s"] for row in doc["phases"]) == 10
    assert SC.history_problems(doc) == []


def test_signature_uses_the_documented_five_value_tuple(tmp_path):
    cid = "C-SIG"
    root = tmp_path / "agentic" / "campaigns" / cid
    (root / "runs").mkdir(parents=True)
    (root / "gates").mkdir()
    (root / "ledger.jsonl").write_text("{}\n")
    (root / "state.json").write_text("{}\n")
    ledger = (root / "ledger.jsonl").stat()
    values = (
        ledger.st_size,
        ledger.st_mtime_ns,
        (root / "state.json").stat().st_mtime_ns,
        (root / "runs").stat().st_mtime_ns,
        (root / "gates").stat().st_mtime_ns,
    )
    expected = hashlib.sha1(repr(values).encode("utf-8")).hexdigest()
    assert H.campaign_signature({"data_dir": str(tmp_path)}, cid) == expected


def test_gate_retry_and_log_metadata(tmp_path):
    cid = "C-GATE"
    gates = tmp_path / "agentic" / "campaigns" / cid / "gates"
    gates.mkdir(parents=True)
    (gates / "001-pytest.json").write_text(json.dumps({
        "name": "pytest", "required": True, "ok": True, "exit_code": 0,
        "log": "001-pytest.log",
    }))
    (gates / "001-pytest.r1.json").write_text("{}")
    (gates / "001-pytest.report.json").write_text("{}")
    rows = H._gate_rows({"data_dir": str(tmp_path)}, cid, [{
        "type": "gate", "gate_seq": 1, "started_at": stamp(0).isoformat(),
        "duration_s": 1, "ok": True, "results": [{"name": "pytest"}],
    }], [])
    assert rows[0]["results"][0]["retries"] == 1
    assert rows[0]["results"][0]["log"] == "gates/001-pytest.log"


def _finding(seq, persona, key, run_id):
    return {
        "type": "finding", "seq": seq, "ts": stamp(seq).isoformat(), "persona": persona,
        "severity": "major", "title": key, "files": [], "key": key, "run_id": run_id,
    }


def test_latest_clean_round_fixes_reraised_findings_only_after_merge(tmp_path):
    events = [
        {"type": "run.end", "seq": 1, "ts": stamp(1).isoformat(),
         "persona": "reviewer", "run_id": "r1"},
        _finding(2, "reviewer", "same-key", "r1"),
        {"type": "run.end", "seq": 3, "ts": stamp(3).isoformat(),
         "persona": "reviewer", "run_id": "r2"},
        _finding(4, "reviewer", "same-key", "r2"),
        {"type": "run.end", "seq": 5, "ts": stamp(5).isoformat(),
         "persona": "reviewer", "run_id": "r3"},
    ]
    cfg = {"data_dir": str(tmp_path)}
    merged = H._ledger_records(cfg, "C", {"state": "MERGED"}, events, [], [])
    assert [r["status"] for r in merged if r["kind"] == "finding"] == ["fixed", "fixed"]
    live = H._ledger_records(cfg, "C", {"state": "REVIEWING"}, events, [], [])
    assert [r["status"] for r in live if r["kind"] == "finding"] == ["open", "open"]


def test_finding_status_priority_and_link_provenance(tmp_path):
    events = [
        _finding(1, "reviewer", "waived-key", "r1"),
        _finding(2, "reviewer", "filed-key", "r1"),
        _finding(3, "reviewer", "carried-key", "r1"),
        {"type": "followup", "seq": 4, "ts": stamp(4).isoformat(),
         "key": "filed-key", "idea_id": "IDEA-4"},
        {"type": "triage", "seq": 5, "ts": stamp(5).isoformat(), "run_id": "triager",
         "waive": [{"key": "waived-key"}]},
    ]
    rows = H._ledger_records(
        {"data_dir": str(tmp_path)}, "C", {
            "state": "REVIEWING", "review_carry": [{"key": "carried-key"}],
        }, events, [], [])
    findings = {row["links"]["key"]: row for row in rows if row["kind"] == "finding"}
    assert findings["waived-key"]["status"] == "waived"
    assert findings["waived-key"]["links"]["resolved_by"] == "ledger:C:5"
    assert findings["waived-key"]["linked_by"] == "field"
    assert findings["filed-key"]["status"] == "filed"
    assert findings["filed-key"]["links"]["followup_idea"] == "IDEA-4"
    assert findings["filed-key"]["linked_by"] == "inferred"
    assert findings["carried-key"]["status"] == "carried"


def test_legacy_waiver_uses_latest_round_and_human_override_shape(tmp_path):
    cid = "C-WAIVE"
    triager = tmp_path / "agentic" / "campaigns" / cid / "runs" / "triager"
    triager.mkdir(parents=True)
    (triager / "run.json").write_text(json.dumps({
        "verdict": {"waive": [{"id": "reviewer#0"}]},
    }))
    base = [
        {"type": "run.end", "seq": 1, "ts": stamp(1).isoformat(),
         "persona": "reviewer", "run_id": "r1"},
        _finding(2, "reviewer", "old", "r1"),
        {"type": "run.end", "seq": 3, "ts": stamp(3).isoformat(),
         "persona": "reviewer", "run_id": "r2"},
        {"type": "run.start", "seq": 4, "ts": stamp(4).isoformat(),
         "persona": "review_triager", "run_id": "triager"},
    ]
    runs = [{"run_id": "triager", "persona": "review_triager",
             "started_at": stamp(4).isoformat()}]
    rows = H._ledger_records(
        {"data_dir": str(tmp_path)}, cid, {"state": "MERGED"}, base, runs, [])
    assert next(r for r in rows if r["kind"] == "finding")["status"] == "fixed"

    override = base[:2] + [{
        "type": "human_decision", "seq": 3, "ts": stamp(3).isoformat(),
        "kind": "override", "escalation": "ESC-9", "waive": ["reviewer#0"],
    }]
    rows = H._ledger_records(
        {"data_dir": str(tmp_path)}, cid, {"state": "MERGED"}, override, [], [])
    finding = next(r for r in rows if r["kind"] == "finding")
    assert finding["status"] == "waived"
    assert finding["links"]["resolved_by"] == f"ledger:{cid}:3"
    assert finding["linked_by"] == "inferred"


def test_escalation_links_and_note_agent_normalization(tmp_path):
    events = [{
        "type": "note", "seq": 1, "ts": stamp(1).isoformat(), "by": "tick",
        "author": "queue", "text": "parked for ESC-0003",
    }, {
        "type": "note", "seq": 2, "ts": stamp(2).isoformat(), "by": "agent:conductor",
        "text": "conductor note",
    }]
    rows = H._ledger_records(
        {"data_dir": str(tmp_path)}, "C", {"state": "PARKED"}, events, [], [])
    assert rows[0]["agent"] == "queue"
    assert rows[0]["links"]["escalation"] == "ESC-0003"
    assert rows[0]["linked_by"] == "text"
    assert rows[1]["agent"] == "conductor"

    decision = H._base_record("decisions", "d", "decision", {
        "rationale": "because ESC-0003 was answered",
    })
    esc, linked_by = H._single_escalation({"rationale": decision["text"]})
    assert (esc, linked_by) == ("ESC-0003", "text")


def test_mixed_offset_timeline_and_records_are_chronological(tmp_path):
    visits = [H.Visit("queue", "QUEUED", stamp(-7200), stamp(7200), 0)]
    rows = H._timeline([
        {"type": "note", "seq": 2, "ts": "2026-01-01T00:30:00+00:00"},
        {"type": "note", "seq": 1, "ts": "2026-01-01T01:00:00+02:00"},
    ], visits, [])
    assert [row["seq"] for row in rows] == [1, 2]

    decisions = tmp_path / "agentic" / "conductor" / "decisions.jsonl"
    decisions.parent.mkdir(parents=True)
    decisions.write_text("\n".join(json.dumps(row) for row in (
        {"event_id": "later", "ts": "2026-01-01T00:30:00+00:00", "text": "later"},
        {"event_id": "earlier", "ts": "2026-01-01T01:00:00+02:00", "text": "earlier"},
    )) + "\n")
    records = H.history_records({"data_dir": str(tmp_path)})
    assert [row["id"] for row in records if row["source"] == "decisions"] == [
        "decisions:later", "decisions:earlier",
    ]


def test_partial_json_structures_produce_gaps_without_exceptions(tmp_path):
    cfg = {"data_dir": str(tmp_path)}
    cid = "C-PARTIAL"
    run = tmp_path / "agentic" / "campaigns" / cid / "runs" / "r1"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({
        "started_at": stamp(1).isoformat(), "cost_usd": {"bad": 1},
        "duration_s": "bad", "verdict": [],
    }))
    gaps = []
    rows = H._run_rows(cfg, cid, [], [], gaps)
    assert rows and any("cost_usd malformed" in gap for gap in gaps)
    assert any("duration_s malformed" in gap for gap in gaps)
    assert any("verdict malformed" in gap for gap in gaps)

    gate_gaps = []
    assert H._gate_rows(cfg, cid, [{
        "type": "gate", "gate_seq": 1, "duration_s": 1, "results": [None],
    }], gate_gaps)[0]["results"] == []
    assert "gate 1: result 0 malformed" in gate_gaps

    triage_gaps = []
    H._ledger_records(cfg, cid, {"state": "REVIEWING"}, [{
        "type": "triage", "seq": 1, "ts": stamp(1).isoformat(), "waive": ["bad"],
    }], [], triage_gaps)
    assert "triage 1: waive item malformed" in triage_gaps

    agentic = tmp_path / "agentic"
    review = agentic / "reviews" / "SPEC-1" / "abc.json"
    review.parent.mkdir(parents=True)
    review.write_text(json.dumps({"spec_id": "SPEC-1"}))
    sentinel = agentic / "sentinel" / "P.last.json"
    sentinel.parent.mkdir()
    sentinel.write_text(json.dumps({"at": stamp(1).isoformat()}))
    (agentic / "flaky-tests.json").write_text(json.dumps({"projects": []}))
    global_gaps = []
    H._global_records(cfg, [], global_gaps)
    assert "review SPEC-1/abc.json: partial" in global_gaps
    assert "sentinel/P.last.json: unparsable or partial" in global_gaps
    assert "flaky-tests.json: projects malformed" in global_gaps


def test_mail_questions_require_a_later_scoped_reply(tmp_path):
    mailbox = tmp_path / "agentic" / "mailbox"
    mailbox.mkdir(parents=True)
    (mailbox / "steward_P.jsonl").write_text("\n".join(json.dumps(row) for row in (
        {"id": "steward:P:1", "seq": 1, "ts": stamp(2).isoformat(), "from": "conductor",
         "to": "steward:P", "kind": "question", "text": "first"},
        {"id": "steward:P:2", "seq": 2, "ts": stamp(5).isoformat(), "from": "conductor",
         "to": "steward:P", "kind": "question", "text": "second"},
    )) + "\n")
    (mailbox / "conductor.jsonl").write_text("\n".join(json.dumps(row) for row in (
        {"id": "conductor:0", "seq": 0, "ts": stamp(1).isoformat(), "from": "steward:P",
         "to": "conductor", "kind": "answer", "re": "1", "text": "too early"},
        {"id": "conductor:1", "seq": 1, "ts": stamp(3).isoformat(), "from": "steward:Q",
         "to": "conductor", "kind": "answer", "re": "1", "text": "wrong steward"},
        {"id": "conductor:2", "seq": 2, "ts": stamp(4).isoformat(), "from": "steward:P",
         "to": "conductor", "kind": "answer", "re": "1", "text": "correct"},
    )) + "\n")
    rows = H._global_records({"data_dir": str(tmp_path)}, [], [])
    questions = {row["text"]: row for row in rows if row["source"] == "mailbox"
                 and row["title"] == "question"}
    assert questions["first"]["status"] == "answered"
    assert questions["second"]["status"] == "open"


def test_decision_escalation_link_and_spec_review_dedup_by_sha(tmp_path):
    agentic = tmp_path / "agentic"
    decisions = agentic / "conductor" / "decisions.jsonl"
    decisions.parent.mkdir(parents=True)
    decisions.write_text(json.dumps({
        "event_id": "d1", "ts": stamp(1).isoformat(),
        "rationale": "ESC-0003 resolved this park",
    }) + "\n")
    review = agentic / "reviews" / "SPEC-1" / "abc.json"
    review.parent.mkdir(parents=True)
    review.write_text(json.dumps({
        "spec_id": "SPEC-1", "spec_sha256": "abc", "reviewed_at": stamp(2).isoformat(),
        "verdict": "approve",
    }))
    (agentic / "spec-reviews.jsonl").write_text(json.dumps({
        "kind": "end", "run_id": "run-1", "spec_id": "SPEC-1", "spec_sha256": "abc",
        "ts": stamp(3).isoformat(), "verdict": "approve",
    }) + "\n")
    rows = H._global_records({"data_dir": str(tmp_path)}, [], [])
    decision = next(row for row in rows if row["source"] == "decisions")
    assert decision["links"]["escalation"] == "ESC-0003"
    assert decision["linked_by"] == "text"
    reviews = [row for row in rows if row["source"] == "spec_review"]
    assert len(reviews) == 1
    assert reviews[0]["id"] == "spec_review:SPEC-1/abc"


def test_spec_review_journal_end_carries_stable_sha(tmp_path):
    cfg = {"data_dir": str(tmp_path)}
    SR.journal_end(cfg, "run-1", "SPEC-1", verdict="approve", spec_sha256="abc")
    row = json.loads(SR.journal_path(cfg).read_text())
    assert row["spec_sha256"] == "abc"


def test_header_derivations_are_independently_pinned(tmp_path):
    events = [
        {"type": "open", "seq": 1, "ts": stamp(0).isoformat()},
        {"type": "run.start", "seq": 2, "ts": stamp(1).isoformat(), "run_id": "r1"},
        {"type": "run.end", "seq": 3, "ts": stamp(2).isoformat(), "run_id": "r1",
         "charged_usd": 1.25},
        {"type": "run.start", "seq": 4, "ts": stamp(3).isoformat(), "run_id": "r2"},
        transition(4, "REVIEWING", "IMPLEMENTING", "review_fix"),
        {"type": "followup", "seq": 6, "ts": stamp(5).isoformat(), "idea_id": "IDEA-1"},
        {"type": "transition", "seq": 7, "ts": stamp(10).isoformat(),
         "from": "CLOSING", "to": "MERGED", "reason": "closed"},
    ]
    phases = [{"phase": phase, "wall_s": 2 if phase == "queue" else 0}
              for phase in H.PHASES]
    header = H._header(
        {"data_dir": str(tmp_path)}, "C", {
            "state": "MERGED", "project": "P", "spec_id": "SPEC-1",
            "sizing": {"size": "m", "tone": "high-end"}, "cost": {},
        }, events, phases, [], stamp(20), {"C": 0.5}, [],
    )
    assert header["created_at"] == stamp(0).isoformat()
    assert header["closed_at"] == stamp(10).isoformat()
    assert header["outcome"] == "merged"
    assert header["wall_s"] == 10 and header["active_s"] == 8
    assert header["loops"] == 1 and header["runs"] == 2
    assert header["cost_usd"] == 1.25 and header["conductor_usd"] == 0.5
    assert header["followup_idea"] == "IDEA-1"
    assert header["tone"] == "high-end"


def test_run_source_priority_fallback_status_budget_and_stream(tmp_path):
    cfg = {"data_dir": str(tmp_path)}
    cid = "C-RUNS"
    root = tmp_path / "agentic" / "campaigns" / cid / "runs"
    first = root / "r1"
    first.mkdir(parents=True)
    (first / "run.json").write_text(json.dumps({
        "persona": "reviewer", "model": "preferred", "effort": "high",
        "started_at": stamp(1).isoformat(), "duration_s": 2, "cost_usd": 2,
        "max_budget_usd": 4, "exit_code": 0, "verdict": {"status": "pass"},
    }))
    (first / "result.json").write_text(json.dumps({"model": "wrong", "cost_usd": 99}))
    (first / "stream.jsonl").write_text("{}\n")
    second = root / "r2"
    second.mkdir()
    (second / "result.json").write_text(json.dumps({
        "persona": "tester", "started_at": stamp(4).isoformat(), "duration_s": 1,
        "cost_usd": 1, "max_budget_usd": 2, "timed_out": True, "exit_code": 1,
    }))
    events = [
        {"type": "run.start", "ts": stamp(1).isoformat(), "run_id": "r1"},
        {"type": "run.end", "ts": stamp(3).isoformat(), "run_id": "r1", "duration_s": 2},
        {"type": "run.start", "ts": stamp(4).isoformat(), "run_id": "r2"},
        {"type": "run.end", "ts": stamp(5).isoformat(), "run_id": "r2", "duration_s": 1},
        {"type": "run.start", "ts": stamp(6).isoformat(), "run_id": "r3"},
    ]
    visits = [H.Visit("review", "REVIEWING", stamp(0), stamp(10), 0)]
    gaps = []
    rows = {row["run_id"]: row for row in H._run_rows(cfg, cid, events, visits, gaps)}
    assert rows["r1"]["model"] == "preferred"
    assert rows["r1"]["budget_frac"] == 0.5
    assert rows["r1"]["status"] == "ok" and rows["r1"]["has_stream"] is True
    assert rows["r2"]["status"] == "timeout"
    assert rows["r3"]["status"] == "running"
    assert "run r2: no run.json" in gaps
    assert "run r3: no run record" in gaps


def test_timeline_merges_run_events_and_caps_plain_summary():
    visits = [H.Visit("review", "REVIEWING", stamp(0), stamp(10), 2)]
    rows = H._timeline([
        {"type": "run.start", "seq": 1, "ts": stamp(1).isoformat(),
         "run_id": "r1", "persona": "agent:reviewer", "title": "x" * 250},
        {"type": "run.end", "seq": 2, "ts": stamp(2).isoformat(),
         "run_id": "r1", "persona": "agent:reviewer"},
    ], visits, [])
    assert len(rows) == 1
    assert rows[0]["kind"] == "run" and rows[0]["ref"] == "r1"
    assert rows[0]["phase"] == "review" and rows[0]["loop"] == 2
    assert rows[0]["by"] == "reviewer" and len(rows[0]["summary"]) == 200


