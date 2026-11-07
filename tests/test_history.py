"""SPEC-0406 unit tests for the pure history derivation."""
import datetime as dt
import json

from nightwright.agentic import history as H


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
    events = [
        {"type": "run.start", "ts": stamp(5).isoformat(), "run_id": "001", "persona": "p"},
        {"type": "run.end", "ts": stamp(15).isoformat(), "run_id": "001", "persona": "p",
         "duration_s": 10},
        {"type": "gate", "ts": stamp(7).isoformat(), "started_at": stamp(7).isoformat(),
         "duration_s": 8},
        {"type": "gate_waiting", "ts": stamp(12).isoformat(), "waited_s": 8},
    ]
    visits = [
        H.Visit("implement", "IMPLEMENTING", stamp(0), stamp(10), 0),
        H.Visit("gate", "GATING", stamp(10), stamp(20), 0),
    ]
    runs = [{"phase": "implement", "cost_usd": 2}]
    phases = {row["phase"]: row for row in H._phase_rows(cfg, events, visits, runs, [])}
    assert phases["implement"]["run_s"] == 5
    assert phases["gate"]["run_s"] == 5
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
                                events, runs)
    findings = [record for record in records if record["kind"] == "finding"]
    assert findings[0]["links"]["run_id"] == "001-reviewer"
    assert findings[0]["linked_by"] == "inferred" and findings[0]["status"] == "open"
    assert findings[1]["links"]["gate_seq"] == 7 and findings[1]["links"]["run_id"] is None
    assert findings[2]["links"]["key"] == "explicit"
    assert findings[2]["linked_by"] == "field" and findings[2]["status"] == "waived"


def test_text_link_never_guesses():
    campaigns = ["C-ONE", "C-TWO"]
    one = H._base_record("decisions", "1", "decision", {
        "text": "context", "rationale": "about C-ONE.",
    })
    two = H._base_record("decisions", "2", "decision", {"text": "C-ONE and C-TWO"})
    assert H._campaign_text_link(one, campaigns) == "text" and one["campaign"] == "C-ONE"
    assert H._campaign_text_link(two, campaigns) is None and two["campaign"] is None
