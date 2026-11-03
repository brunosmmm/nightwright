"""SPEC-0236: `nw campaign export` — flat campaign / run / finding tables."""
import csv
import datetime as dt
import io
import json
import sys
import types
from zoneinfo import ZoneInfo

import click
import pytest
from click.testing import CliRunner

from nightwright.agentic import campaign_export as CE
from nightwright.agentic import runs as RN
from nightwright.agentic import schemas as SC
from nightwright.agentic import store as S
from nightwright.agentic.cli_campaign import _export_dir
from nightwright.cli import cli

TZ = ZoneInfo("America/New_York")
T0 = dt.datetime(2026, 10, 20, 9, 0, 0, tzinfo=TZ)
NOW = dt.datetime(2026, 10, 25, 12, 0, 0, tzinfo=TZ)

SPEC_AT_OPEN = """---
id: SPEC-0001
---
## Context

x

## Acceptance criteria

- [ ] one
- [x] two
* [X] three
      continuation text
- not a box

## Test plan

- [ ] not an ac
"""


def ts(minutes):
    return (T0 + dt.timedelta(minutes=minutes)).isoformat(timespec="seconds")


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "data").mkdir()
    return {"data_dir": str(tmp_path / "data"), "timezone": "America/New_York", "_tz": TZ,
            "agentic": {"default_profile": "balanced"}}


def make(cfg, cid, events=(), *, created=0, opened=None, spec_at_open=SPEC_AT_OPEN, **state):
    """A campaign whose ledger is exactly `events` (`(minute, type, payload)`), written with
    explicit timestamps: an `open` event first, at `opened` (default: `created`)."""
    st = {"id": cid, "spec_id": "SPEC-0001", "project": "P", "kind": "spec",
          "profile": "balanced", "state": "QUEUED", "created_at": ts(created),
          "counters": {"loops": 0, "runs": 0, "gate_runs": 0, "gate_retries": 0,
                       "triage_retries": 0, "parks": 0},
          "cost": {"total_usd": 0.0, "cap_usd": None, "by_persona": {}}}
    st.update(state)
    S.create_campaign(cfg, st)
    lines = [{"schema": SC.LEDGER_SCHEMA, "ts": ts(created if opened is None else opened), "seq": 1,
              "campaign": cid, "type": "open", "by": "t",
              **SC.with_defaults("open", {"spec_id": "SPEC-0001", "project": "P"})}]
    for i, (minute, etype, payload) in enumerate(events, start=2):
        rec = {"schema": SC.LEDGER_SCHEMA, "ts": ts(minute), "seq": i, "campaign": cid,
               "type": etype, "by": "t", **SC.with_defaults(etype, payload)}
        assert not SC.ledger_problems(rec), SC.ledger_problems(rec)
        lines.append(rec)
    (S.campaign_dir(cfg, cid) / "ledger.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in lines))
    if spec_at_open is not None:
        (S.campaign_dir(cfg, cid) / "spec_at_open.md").write_text(spec_at_open)
    return S.campaign_dir(cfg, cid)


def tr(frm, to, reason="x"):
    return {"from": frm, "to": to, "reason": reason}


def run_start(persona, run_id, model="m1", **kw):
    return {"persona": persona, "run_id": run_id, "model": model, "effort": "high",
            "max_budget_usd": 2.0, "timeout_s": 600, **kw}


def run_end(persona, run_id, **kw):
    base = {"exit_code": 0, "is_error": False, "timed_out": False, "interrupted": False,
            "duration_s": 12.5, "cost_usd": 0.5, "charged_usd": 0.5, "verdict_ok": True,
            "verdict": {"status": "done"}, "usage": None, "result_tail": None}
    return {"persona": persona, "run_id": run_id, **base, **kw}


def full_campaign(cfg, cid="C-FULL"):
    """Hand-computed campaign: QUEUED 10m → OPENING 5m → IMPLEMENTING 30m → GATING 10m →
    TRIAGING 5m → (retry_gate) GATING 5m → TRIAGING 5m → IMPLEMENTING (triage_fix) 20m →
    REVIEWING 10m → IMPLEMENTING (review_fix) 15m → PARKED 60m → (resume, loops reset, cap
    raise) REVIEWING 5m → CLOSING 5m → MERGED."""
    events = [
        (10, "transition", tr("QUEUED", "OPENING", "admit")),
        (15, "transition", tr("OPENING", "IMPLEMENTING", "opened")),
        (15, "run.start", run_start("implementer", "001-implementer", "m-impl")),
        (40, "run.end", run_end("implementer", "001-implementer")),
        (45, "transition", tr("IMPLEMENTING", "GATING", "implemented")),
        (55, "gate", {"gate_seq": 1, "ok": False}),
        (55, "transition", tr("GATING", "TRIAGING", "gate_failed")),
        (55, "run.start", run_start("gate_triager", "002-gate_triager", "m-tri")),
        (60, "run.end", run_end("gate_triager", "002-gate_triager", verdict_ok=False,
                                verdict=None)),
        (60, "run.start", run_start("gate_triager", "003-gate_triager", "m-tri2")),
        (60, "run.end", run_end("gate_triager", "003-gate_triager", interrupted=True,
                                verdict_ok=False)),
        (60, "transition", tr("TRIAGING", "GATING", "retry_gate")),
        (65, "gate", {"gate_seq": 2, "ok": False}),
        (65, "transition", tr("GATING", "TRIAGING", "gate_failed")),
        (70, "gate", {"gate_seq": 3, "ok": True}),
        (70, "transition", tr("TRIAGING", "IMPLEMENTING", "triage_fix")),
        (70, "finding", {"persona": "gate_triager", "severity": "must_fix", "gate": "pytest",
                         "title": "t1", "detail": "d" * 600, "files": ["a.py"]}),
        (75, "finding", {"persona": "gate_triager", "severity": "info", "title": "t2"}),
        (90, "transition", tr("IMPLEMENTING", "REVIEWING", "implemented")),
        (95, "finding", {"persona": "conformance_reviewer", "severity": "major", "title": "t3"}),
        (100, "note", {"text": "waived F1: nope", "author": "queue"}),
        (100, "note", {"text": "waived F2: nope", "author": "human"}),
        (100, "note", {"text": "triager ignored F3", "author": "queue"}),
        (100, "transition", tr("REVIEWING", "IMPLEMENTING", "review_fix")),
        (115, "transition", tr("IMPLEMENTING", "PARKED", "loop_exhausted")),
        (115, "park", {"reason": "loop_exhausted", "detail": "d", "resume_to": "REVIEWING"}),
        (175, "resume", {"to": "REVIEWING", "note": None, "cap_usd": 20.0, "loops_reset": True}),
        (175, "transition", tr("PARKED", "REVIEWING", "resume")),
        (180, "resume", {"to": "REVIEWING", "note": None, "cap_usd": None, "loops_reset": False}),
        (180, "run.start", run_start("implementer", "004-implementer", "m-impl2")),
        (185, "transition", tr("REVIEWING", "CLOSING", "approved")),
        (190, "transition", tr("CLOSING", "MERGED", "merged")),
    ]
    return make(cfg, cid, events, state="MERGED", profile="frugal",
                counters={"loops": 0, "runs": 7, "gate_runs": 5, "gate_retries": 0,
                          "triage_retries": 0, "parks": 2},   # != ledger (4, 3, 1): pins the source
                cost={"total_usd": 4.0, "cap_usd": 5.0, "by_persona": {"implementer": 3.0}},
                last_review={"score": 88},
                close={"mode": "merge", "merged_sha": "abc123"},
                sizing={"size": "s", "tone": "mvp", "source": "review",
                        "rationale": "small", "reason": "r"})


def row_of(cfg, cid):
    tables = CE.export_tables(cfg, now=NOW)
    return next(r for r in tables["campaigns"] if r["campaign"] == cid)


# ---- schema ------------------------------------------------------------------------------

def test_columns_and_key_order(cfg):
    assert [n for n, _ in CE.CAMPAIGN_COLUMNS] == [
        "campaign", "kind", "spec_id", "project", "state", "outcome", "created_at", "ended_at",
        "wall_s", "active_s", "size", "tone", "profile", "profile_source", "sizing_rationale",
        "loop_max", "cap_usd", "cap_raises", "models", "ac_count", "spec_lines", "loops",
        "loop_resets", "runs", "runs_by_persona", "gate_runs", "gate_failures", "gate_retries",
        "triage_retries", "parks", "park_reasons", "cost_usd", "cost_by_persona",
        "cost_cap_frac", "time_in_state_s", "review_score", "must_fix_count", "finding_count",
        "waived_count", "close_mode", "merged_sha", "signals", "conductor_usd",
        "cost_with_conductor_usd", "rebase_regenerated", "resolver_dispatches",
        "rebase_resolved", "resolver_overcap_usd",
        "reviewer_runs", "reviewer_findings", "reviewer_blocking", "findings_per_reviewer_run",
        "blocking_per_reviewer_run", "first_review_must_fix", "first_review_major", "close_s",
        "task_active_s"]
    assert [n for n, _ in CE.RUN_COLUMNS] == [
        "campaign", "spec_id", "profile", "size", "tone", "run_id", "persona", "model", "effort",
        "max_budget_usd", "timeout_s", "started_at", "duration_s", "cost_usd", "charged_usd",
        "budget_frac", "exit_code", "is_error", "timed_out", "interrupted", "transient",
        "verdict_status", "status", "input_tokens", "output_tokens", "cache_read_tokens"]
    assert [n for n, _ in CE.FINDING_COLUMNS] == [
        "campaign", "spec_id", "seq", "ts", "state_at", "persona", "severity", "gate", "ac",
        "title", "detail", "files"]
    full_campaign(cfg)
    make(cfg, "C-BARE", [], spec_at_open=None, created=1)
    tables = CE.export_tables(cfg, now=NOW)
    for name, cols in CE.COLUMNS.items():
        assert tables[name], name
        for row in tables[name]:
            assert list(row) == [n for n, _ in cols]


def test_column_types_pinned():
    """Under v1 a column may not be retyped (SPEC-0236 Design): pin every (name, type)."""
    assert CE.CAMPAIGN_COLUMNS == (
        ("campaign", "str"), ("kind", "str"), ("spec_id", "str"), ("project", "str"),
        ("state", "str"), ("outcome", "str"), ("created_at", "str"), ("ended_at", "str"),
        ("wall_s", "int"), ("active_s", "int"),
        ("size", "str"), ("tone", "str"), ("profile", "str"), ("profile_source", "str"),
        ("sizing_rationale", "str"),
        ("loop_max", "int"), ("cap_usd", "float"), ("cap_raises", "int"), ("models", "json"),
        ("ac_count", "int"), ("spec_lines", "int"),
        ("loops", "int"), ("loop_resets", "int"), ("runs", "int"), ("runs_by_persona", "json"),
        ("gate_runs", "int"), ("gate_failures", "int"), ("gate_retries", "int"),
        ("triage_retries", "int"), ("parks", "int"), ("park_reasons", "json"),
        ("cost_usd", "float"), ("cost_by_persona", "json"), ("cost_cap_frac", "float"),
        ("time_in_state_s", "json"), ("review_score", "int"), ("must_fix_count", "int"),
        ("finding_count", "int"), ("waived_count", "int"),
        ("close_mode", "str"), ("merged_sha", "str"), ("signals", "json"),
        ("conductor_usd", "float"), ("cost_with_conductor_usd", "float"),
        ("rebase_regenerated", "int"), ("resolver_dispatches", "int"),
        ("rebase_resolved", "int"), ("resolver_overcap_usd", "float"),
        ("reviewer_runs", "int"), ("reviewer_findings", "int"), ("reviewer_blocking", "int"),
        ("findings_per_reviewer_run", "float"), ("blocking_per_reviewer_run", "float"),
        ("first_review_must_fix", "int"), ("first_review_major", "int"), ("close_s", "int"),
        ("task_active_s", "int"))
    assert len(CE.CAMPAIGN_COLUMNS) == 57 and CE.CAMPAIGN_COLUMNS[47][0] == "resolver_overcap_usd"
    assert CE.EXPORT_SCHEMA == "nw.campaign.export.v1"
    assert CE.RUN_COLUMNS == (
        ("campaign", "str"), ("spec_id", "str"), ("profile", "str"), ("size", "str"),
        ("tone", "str"),
        ("run_id", "str"), ("persona", "str"), ("model", "str"), ("effort", "str"),
        ("max_budget_usd", "float"), ("timeout_s", "int"), ("started_at", "str"),
        ("duration_s", "float"), ("cost_usd", "float"), ("charged_usd", "float"),
        ("budget_frac", "float"),
        ("exit_code", "int"), ("is_error", "bool"), ("timed_out", "bool"),
        ("interrupted", "bool"), ("transient", "bool"), ("verdict_status", "str"),
        ("status", "str"),
        ("input_tokens", "int"), ("output_tokens", "int"), ("cache_read_tokens", "int"))
    assert CE.FINDING_COLUMNS == (
        ("campaign", "str"), ("spec_id", "str"),
        ("seq", "int"), ("ts", "str"), ("state_at", "str"),
        ("persona", "str"), ("severity", "str"), ("gate", "str"), ("ac", "str"),
        ("title", "str"), ("detail", "str"), ("files", "json"))


# ---- the campaign row --------------------------------------------------------------------

def test_hand_computed_campaign_row(cfg):
    full_campaign(cfg)
    r = row_of(cfg, "C-FULL")
    assert r["outcome"] == "MERGED" and r["state"] == "MERGED"
    assert r["ended_at"] == ts(190)
    assert r["wall_s"] == 190 * 60
    tis = r["time_in_state_s"]
    # IMPLEMENTING: 15..45 (30m) + 70..90 (20m) + 100..115 (15m)
    assert tis["IMPLEMENTING"] == 65 * 60
    assert tis["QUEUED"] == 10 * 60 and tis["OPENING"] == 5 * 60
    assert tis["GATING"] == (10 + 5) * 60            # 45..55, 60..65
    assert tis["TRIAGING"] == (5 + 5) * 60           # 55..60, 65..70
    assert tis["REVIEWING"] == (10 + 10) * 60        # 90..100, 175..185
    assert tis["PARKED"] == 60 * 60 and tis["CLOSING"] == 5 * 60
    assert sum(tis.values()) == r["wall_s"]
    assert "MERGED" not in tis                      # a terminal state gets no interval
    assert set(tis) == {"QUEUED", "OPENING", "IMPLEMENTING", "GATING", "TRIAGING", "REVIEWING",
                        "PARKED", "CLOSING"}
    assert r["active_s"] == r["wall_s"] - (10 + 60) * 60      # not QUEUED, not PARKED
    assert r["runs_by_persona"] == {"implementer": 2, "gate_triager": 2}
    assert r["models"] == {"implementer": "m-impl2", "gate_triager": "m-tri2"}
    assert r["gate_failures"] == 2 and r["gate_runs"] == 5
    assert r["park_reasons"] == ["loop_exhausted"] and r["parks"] == 2
    assert r["cost_usd"] == 4.0 and r["cap_usd"] == 5.0 and r["cost_cap_frac"] == 0.8
    assert r["must_fix_count"] == 1 and r["finding_count"] == 2
    assert r["waived_count"] == 1
    assert r["close_mode"] == "merge" and r["merged_sha"] == "abc123"
    assert r["review_score"] == 88 and r["runs"] == 7
    assert r["cost_by_persona"] == {"implementer": 3.0}
    # ledger totals, not the (reset) counters, which are all 0 in state.json
    assert r["loops"] == 2 and r["gate_retries"] == 1 and r["triage_retries"] == 1
    assert r["loop_resets"] == 1 and r["cap_raises"] == 1
    assert r["signals"] == ["loops_exhausted", "cost_near_cap", "many_fix_loops"]


def test_existing_columns_unchanged(cfg):
    """SPEC-0396: the new group leaves every earlier value as test_hand_computed_campaign_row
    pins it (active_s still includes CLOSING; counts still cover every persona)."""
    full_campaign(cfg)
    r = row_of(cfg, "C-FULL")
    assert r["active_s"] == 120 * 60 and r["wall_s"] == 190 * 60
    assert r["must_fix_count"] == 1 and r["finding_count"] == 2
    assert r["signals"] == ["loops_exhausted", "cost_near_cap", "many_fix_loops"]
    # the new group on the same campaign: one reviewer finding (major), no valid reviewer run
    assert (r["reviewer_runs"], r["reviewer_findings"], r["reviewer_blocking"]) == (0, 1, 1)
    assert r["findings_per_reviewer_run"] is None and r["blocking_per_reviewer_run"] is None
    assert (r["first_review_must_fix"], r["first_review_major"]) == (0, 1)
    assert r["close_s"] == 5 * 60 and r["task_active_s"] == 115 * 60


def test_open_campaign_runs_to_now(cfg):
    make(cfg, "C-OPEN", [(10, "transition", tr("QUEUED", "OPENING")),
                         (20, "transition", tr("OPENING", "IMPLEMENTING"))],
         state="IMPLEMENTING")
    r = row_of(cfg, "C-OPEN")
    assert r["outcome"] is None and r["ended_at"] is None
    assert r["wall_s"] == int((NOW - T0).total_seconds())
    assert r["time_in_state_s"]["IMPLEMENTING"] == int((NOW - T0).total_seconds()) - 20 * 60
    assert r["active_s"] == r["wall_s"] - 10 * 60


def test_open_event_later_than_created_at(cfg):
    """QUEUED starts at the `open` event's ts; `wall_s` starts at `created_at`."""
    make(cfg, "C-LATE", [(12, "transition", tr("QUEUED", "OPENING")),
                         (20, "transition", tr("OPENING", "MERGED", "x"))],
         created=0, opened=5, state="MERGED")
    r = row_of(cfg, "C-LATE")
    assert r["time_in_state_s"] == {"QUEUED": 7 * 60, "OPENING": 8 * 60}
    assert r["wall_s"] == 20 * 60 and r["ended_at"] == ts(20)


def test_loops_count_only_fix_transitions_into_implementing(cfg):
    make(cfg, "C-LOOPS", [
        (1, "transition", tr("QUEUED", "OPENING", "admit")),
        (2, "transition", tr("OPENING", "IMPLEMENTING", "opened")),
        (3, "transition", tr("IMPLEMENTING", "GATING", "implemented")),
        (4, "transition", tr("GATING", "PARKED", "triage_fix")),       # fix reason, other target
        (5, "transition", tr("PARKED", "IMPLEMENTING", "resume")),     # IMPLEMENTING, other reason
        (6, "transition", tr("IMPLEMENTING", "REVIEWING", "review_fix")),
        (7, "transition", tr("REVIEWING", "IMPLEMENTING", "review_fix")),    # the only one
    ], state="IMPLEMENTING")
    assert row_of(cfg, "C-LOOPS")["loops"] == 1


def test_terminal_without_terminal_transition(cfg):
    make(cfg, "C-NT", [(10, "transition", tr("QUEUED", "OPENING"))], state="ABANDONED")
    r = row_of(cfg, "C-NT")
    assert r["outcome"] == "ABANDONED" and r["ended_at"] is None and r["wall_s"] is None
    assert "ABANDONED" not in r["time_in_state_s"]
    assert r["signals"] == ["abandoned"]


def test_pre_sizing_and_missing_spec(cfg):
    make(cfg, "C-OLD", [], spec_at_open=None, profile="no-such-profile", state="QUEUED")
    r = row_of(cfg, "C-OLD")
    for k in ("size", "tone", "profile_source", "sizing_rationale", "ac_count", "spec_lines",
              "loop_max", "review_score", "close_mode", "merged_sha"):
        assert r[k] is None, k
    assert "long_for_size" not in r["signals"]


def test_sizing_ac_count_loop_max(cfg):
    full_campaign(cfg)
    r = row_of(cfg, "C-FULL")
    assert (r["size"], r["tone"], r["profile_source"], r["sizing_rationale"]) == (
        "s", "mvp", "review", "small")
    assert r["ac_count"] == 3 and r["spec_lines"] == len(SPEC_AT_OPEN.splitlines())
    assert r["loop_max"] == 2                       # builtin `frugal`


def test_campaign_opened_from_a_revise_review_exports_its_sizing(tmp_path, monkeypatch):  # SPEC-0308
    from test_agentic_e2e import make_cfg, make_repo, script, write_review
    from nightwright.agentic import queue as Q
    repo = make_repo(tmp_path, review=False)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    write_review(cfg, repo / "docs" / "specs" / "0001-trivial.md", "SPEC-0001", "revise",
                 sha="f" * 64, size="s", tone="cost-effective", rationale="small")
    plan = Q.plan_add(cfg, "SPEC-0001")
    Q.apply_add(cfg, plan)
    r = row_of(cfg, plan["campaign"])
    assert (r["size"], r["tone"], r["profile"], r["profile_source"], r["sizing_rationale"]) == (
        "s", "cost-effective", "frugal", "review", "small")


def test_loop_max_follows_current_profile_config(cfg):
    full_campaign(cfg)
    assert row_of(cfg, "C-FULL")["loop_max"] == 2
    cfg["agentic"]["profiles"] = {"frugal": {"loop_max": 7}}
    assert row_of(cfg, "C-FULL")["loop_max"] == 7           # not what was in force during the run


@pytest.mark.parametrize("cap,frac", [(None, None), (0, None), (4.0, 0.5), (3.0, 0.667)])
def test_cost_cap_frac(cfg, cap, frac):
    make(cfg, "C-CAP", [], cost={"total_usd": 2.0, "cap_usd": cap, "by_persona": {}})
    assert row_of(cfg, "C-CAP")["cost_cap_frac"] == frac


# ---- runs, findings ----------------------------------------------------------------------

def test_run_rows(cfg):
    cid = "C-RUNS"
    make(cfg, cid, [
        (1, "run.start", run_start("implementer", "001-implementer")),
        (2, "run.end", run_end("implementer", "001-implementer", cost_usd=1.0, charged_usd=1.0,
                               usage={"input_tokens": 10, "output_tokens": 20,
                                      "cache_read_input_tokens": 30})),
        (3, "run.start", run_start("conformance_reviewer", "002-conformance_reviewer",
                                   max_budget_usd=0)),
        (4, "run.end", run_end("conformance_reviewer", "002-conformance_reviewer")),
        (5, "run.start", run_start("implementer", "003-implementer")),
    ], sizing={"size": "m", "tone": "solid"})
    rows = CE.export_tables(cfg, now=NOW)["runs"]
    by = {r["run_id"]: r for r in rows}
    src = {r["run_id"]: r for r in RN.campaign_runs(cfg, cid)}
    assert set(by) == set(src)
    for rid, row in by.items():
        for k, v in row.items():
            if k in src[rid]:
                assert v == src[rid][k], (rid, k)
        assert (row["campaign"], row["size"], row["tone"], row["profile"]) == (
            cid, "m", "solid", "balanced")
    a = by["001-implementer"]
    assert a["budget_frac"] == 0.5
    assert (a["input_tokens"], a["output_tokens"], a["cache_read_tokens"]) == (10, 20, 30)
    b = by["002-conformance_reviewer"]
    assert b["budget_frac"] is None                     # budget 0
    assert (b["input_tokens"], b["output_tokens"], b["cache_read_tokens"]) == (None,) * 3
    c = by["003-implementer"]
    assert c["status"] == "running" and c["budget_frac"] is None and c["charged_usd"] is None


def test_finding_rows_state_at(cfg):
    full_campaign(cfg)
    rows = CE.export_tables(cfg, now=NOW)["findings"]
    assert [(r["title"], r["state_at"]) for r in rows] == [
        ("t1", "IMPLEMENTING"), ("t2", "IMPLEMENTING"), ("t3", "REVIEWING")]
    assert rows[0]["files"] == ["a.py"] and len(rows[0]["detail"]) == CE.FINDING_DETAIL_MAX
    assert rows[1]["detail"] is None
    assert rows[0]["campaign"] == "C-FULL" and rows[0]["spec_id"] == "SPEC-0001"


def test_off_type_values_are_coerced_to_the_declared_types(cfg, invoke, tmp_path):
    make(cfg, "C-ODD", [
        (1, "finding", {"persona": "p", "severity": "major", "ac": 3, "title": 42,
                        "gate": ["pytest"], "detail": 7}),
    ], last_review={"score": 92.5}, cost={"total_usd": 2, "cap_usd": 4, "by_persona": {}},
        sizing={"size": "s", "tone": "t", "source": 1, "rationale": None})
    t = CE.export_tables(cfg, now=NOW)
    c, f = t["campaigns"][0], t["findings"][0]
    assert c["review_score"] == 92 and c["cost_usd"] == 2.0
    assert c["cap_usd"] == 4.0 and isinstance(c["cap_usd"], float)
    assert c["profile_source"] == "1"
    assert (f["ac"], f["title"], f["gate"], f["detail"]) == ("3", "42", '["pytest"]', "7")
    for name, cols in CE.COLUMNS.items():
        for row in t[name]:
            for col, typ in cols:
                v = row[col]
                ok = {"str": str, "int": int, "float": float, "bool": bool, "json": object}[typ]
                assert v is None or isinstance(v, ok), (name, col, v)
    pq = pytest.importorskip("pyarrow.parquet")
    d = tmp_path / "pq"
    r = invoke("--format", "parquet", "--out", str(d))
    assert r.exit_code == 0, r.output
    assert pq.read_table(d / "campaigns.parquet").to_pylist()[0]["review_score"] == 92


def test_string_numbers_and_bools_do_not_break_the_export(cfg):
    make(cfg, "C-STR", [(1, "finding", {"persona": "p", "severity": "major", "title": "t"})],
         last_review={"score": "92"},
         cost={"total_usd": "4", "cap_usd": "5", "by_persona": {}})
    c = CE.export_tables(cfg, now=NOW)["campaigns"][0]
    assert (c["cost_usd"], c["cap_usd"], c["cost_cap_frac"]) == (4.0, 5.0, 0.8)
    assert c["review_score"] == 92 and "cost_near_cap" in c["signals"]
    make(cfg, "C-BAD", [], cost={"total_usd": "lots", "cap_usd": [], "by_persona": {}})
    bad = {r["campaign"]: r for r in CE.export_tables(cfg, now=NOW)["campaigns"]}["C-BAD"]
    assert bad["cost_usd"] is None and bad["cost_cap_frac"] is None


@pytest.mark.parametrize("value,want", [
    (True, True), (False, False), (1, True), (0, False), ("true", True), ("False", False),
    (" TRUE ", True), ("false", False), ("0", None), ("yes", None), ([], None), ({}, None),
])
def test_bool_coercion(value, want):
    assert CE._coerce(value, "bool") is want


def test_run_row_string_flags_and_budgets(cfg):
    cid = "C-RUNSTR"
    make(cfg, cid, [(1, "run.start", {**run_start("implementer", "001-implementer"),
                                      "max_budget_usd": "2"})])
    d = S.campaign_dir(cfg, cid) / "runs" / "001-implementer"
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.json").write_text(json.dumps({
        "run_id": "001-implementer", "persona": "implementer", "is_error": "false",
        "timed_out": "false", "charged_usd": "1", "max_budget_usd": "2"}))
    rows = CE.export_tables(cfg, now=NOW)["runs"]
    assert rows and all(r["is_error"] is not True and r["timed_out"] is not True for r in rows)


# ---- signals -----------------------------------------------------------------------------

SIGNAL_CASES = {
    "loops_exhausted": ({"park_reasons": ["loop_exhausted"]}, {"park_reasons": ["cost_cap"]}),
    "cap_parked": ({"park_reasons": ["cost_cap"]}, {"park_reasons": []}),
    "cost_near_cap": ({"cost_cap_frac": 0.8}, {"cost_cap_frac": 0.799}),
    "many_fix_loops": ({"loops": 2}, {"loops": 1}),
    "escalated": ({"park_reasons": ["review_escalated"]}, {"park_reasons": ["human"]}),
    "gate_churn": ({"gate_failures": 3}, {"gate_failures": 2}),
    "long_for_size": ({"size": "xs", "active_s": 3601}, {"size": "xs", "active_s": 3600}),
    "abandoned": ({"outcome": "ABANDONED"}, {"outcome": "MERGED"}),
}


@pytest.mark.parametrize("name", CE.SIGNAL_ORDER)
def test_signal_on_off(name):
    on, off = SIGNAL_CASES[name]
    assert name in CE.signals(on)
    assert name not in CE.signals(off)
    assert CE.signals({}) == []


@pytest.mark.parametrize("reason", ["triage_escalated", "review_escalated",
                                    "implementer_blocked"])
def test_escalated_each_reason(reason):
    assert CE.signals({"park_reasons": [reason]}) == ["escalated"]


def test_signals_unknown_size_and_order():
    assert CE.signals({"size": None, "active_s": 10 ** 9}) == []
    assert CE.signals({"size": "xl", "active_s": 10 ** 9}) == []
    assert "long_for_size" in CE.signals({"size": "l", "active_s": 28801})
    row = {"park_reasons": ["cost_cap", "loop_exhausted", "triage_escalated"],
           "cost_cap_frac": 1.0, "loops": 5, "gate_failures": 9, "size": "xs",
           "active_s": 10 ** 6, "outcome": "ABANDONED"}
    assert CE.signals(row) == list(CE.SIGNAL_ORDER)
    assert list(SIGNAL_CASES) == list(CE.SIGNAL_ORDER)


# ---- CLI ---------------------------------------------------------------------------------

@pytest.fixture
def invoke(cfg, monkeypatch):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)

    def run(*args):
        return CliRunner().invoke(cli, ["campaign", "export", *args])
    return run


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_default_dir_and_same_second(cfg, invoke, monkeypatch):
    full_campaign(cfg)

    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 25, 12, 0, 0, tzinfo=tz or TZ)

        def astimezone(self, tz=None):
            return self

    monkeypatch.setattr("nightwright.agentic.cli_campaign.dt", types.SimpleNamespace(datetime=Frozen))
    r1 = invoke("--json")
    assert r1.exit_code == 0, r1.output
    m1 = json.loads(r1.output)
    root = S.agentic_dir(cfg) / "exports"
    assert m1["out"] == str(root / "20261025-120000")
    r2 = invoke("--json")
    assert json.loads(r2.output)["out"] == str(root / "20261025-120000-2")
    d = root / "20261025-120000"
    assert sorted(p.name for p in d.iterdir()) == [
        "campaigns.jsonl", "findings.jsonl", "manifest.json", "runs.jsonl"]
    assert len(read_jsonl(d / "campaigns.jsonl")) == 1
    on_disk = json.loads((d / "manifest.json").read_text())
    assert on_disk["schema"] == "nw.campaign.export.v1" and on_disk["format"] == "jsonl"
    assert on_disk["tables"]["campaigns"] == {
        "file": "campaigns.jsonl", "rows": 1, "columns": [[n, t] for n, t in CE.CAMPAIGN_COLUMNS]}
    assert on_disk["tables"]["runs"]["rows"] == 4
    assert on_disk["tables"]["findings"]["rows"] == 3
    assert on_disk["filters"] == {"project": None, "since": None, "terminal_only": False}
    assert any("loop_max" in n for n in on_disk["notes"])
    assert any("final cost cap" in n for n in on_disk["notes"])
    assert any("first_review_*" in n and "reviewer personas" in n for n in on_disk["notes"])
    assert on_disk["nw_version"] and on_disk["generated_at"]
    assert {k: v for k, v in m1.items() if k != "out"} == on_disk
    assert m1["schema"] == "nw.campaign.export.v1" and m1["notes"] == on_disk["notes"]
    assert any("loop_max" in n for n in m1["notes"])
    assert any("final cost cap" in n for n in m1["notes"])
    assert m1["tables"]["findings"]["rows"] == 3
    assert m1["tables"]["campaigns"]["columns"] == [[n, t] for n, t in CE.CAMPAIGN_COLUMNS]


def test_plain_output(cfg, invoke, tmp_path):
    full_campaign(cfg)
    d = tmp_path / "plain"
    r = invoke("--out", str(d))
    assert r.exit_code == 0, r.output
    flat = " ".join(r.output.split())
    assert "exported to" in flat and d.name in flat
    for line in ("campaigns.jsonl 1 rows", "runs.jsonl 4 rows", "findings.jsonl 3 rows"):
        assert line in flat


def test_out_dir_rules(cfg, invoke, tmp_path):
    full_campaign(cfg)
    new = tmp_path / "x" / "new"
    assert invoke("--out", str(new)).exit_code == 0 and (new / "manifest.json").is_file()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert invoke("--out", str(empty)).exit_code == 0
    r = invoke("--out", str(new))
    assert r.exit_code != 0 and f"--out {new} is not empty" in r.output
    assert (new / "campaigns.jsonl").is_file()          # untouched
    f = tmp_path / "a-file"
    f.write_text("x")
    assert invoke("--out", str(f)).exit_code != 0       # click refuses a file before the command
    assert f.read_text() == "x"
    with pytest.raises(click.ClickException, match=f"--out {f} is not empty"):
        _export_dir(cfg, str(f), dt.datetime(2026, 10, 25))
    assert f.read_text() == "x"


def test_filters(cfg, invoke, tmp_path):
    full_campaign(cfg, "C-A")                                              # P, MERGED, day 20
    make(cfg, "C-B", [], project="Q", state="IMPLEMENTING",
         created_at=dt.datetime(2026, 10, 21, 23, 30, tzinfo=TZ).isoformat())
    # 2026-10-22 02:00 UTC is still 2026-10-21 in New York
    make(cfg, "C-C", [], project="P", state="ABANDONED",
         created_at=dt.datetime(2026, 10, 22, 2, 0, tzinfo=dt.timezone.utc).isoformat())

    def ids(*args):
        d = tmp_path / ("o" + str(len(list(tmp_path.iterdir()))))
        r = invoke("--out", str(d), *args)
        assert r.exit_code == 0, r.output
        return [c["campaign"] for c in read_jsonl(d / "campaigns.jsonl")]

    assert ids() == ["C-A", "C-B", "C-C"]
    assert ids("--project", "Q") == ["C-B"]
    assert ids("--terminal-only") == ["C-A", "C-C"]
    assert ids("--since", "2026-10-21") == ["C-B", "C-C"]          # boundary day included (tz)
    assert ids("--since", "2026-10-22") == []
    assert ids("--since", "2026-10-20") == ["C-A", "C-B", "C-C"]
    m = invoke("--json", "--out", str(tmp_path / "f"), "--since", "2026-10-21",
               "--terminal-only", "--project", "P")
    assert json.loads(m.output)["filters"] == {
        "project": "P", "since": "2026-10-21", "terminal_only": True}


def test_since_naive_created_at_is_read_in_the_configured_tz():
    # 23:00 on the 22nd with no offset is the 22nd in cfg["_tz"], whatever the system zone is
    state = {"created_at": "2026-10-22T23:00:00"}
    kiritimati = {"_tz": ZoneInfo("Pacific/Kiritimati")}
    assert CE._created_date(kiritimati, state) == dt.date(2026, 10, 22)
    assert CE._created_date({"_tz": ZoneInfo("Pacific/Pago_Pago")}, state) == dt.date(2026, 10, 22)
    assert CE._created_date({}, state) == dt.date(2026, 10, 22)             # no tz: as written
    aware = {"created_at": "2026-10-22T23:00:00+00:00"}
    assert CE._created_date(kiritimati, aware) == dt.date(2026, 10, 23)     # aware: converted


def test_since_naive_created_at_cli(cfg, invoke, tmp_path):
    make(cfg, "C-NAIVE", [], created_at="2026-10-22T02:00:00")
    d = tmp_path / "n"
    assert invoke("--out", str(d), "--since", "2026-10-22").exit_code == 0
    assert [c["campaign"] for c in read_jsonl(d / "campaigns.jsonl")] == ["C-NAIVE"]
    d2 = tmp_path / "n2"
    assert invoke("--out", str(d2), "--since", "2026-10-23").exit_code == 0
    assert read_jsonl(d2 / "campaigns.jsonl") == []


def test_since_drops_unparseable_created_at(cfg, invoke, tmp_path):
    make(cfg, "C-OK", [])
    make(cfg, "C-NULL", [], created_at=None)
    make(cfg, "C-BAD", [], created_at="not a date")

    def ids(*args):
        d = tmp_path / f"o{len(list(tmp_path.iterdir()))}"
        assert invoke("--out", str(d), *args).exit_code == 0
        return sorted(c["campaign"] for c in read_jsonl(d / "campaigns.jsonl"))

    assert ids() == ["C-BAD", "C-NULL", "C-OK"]
    assert ids("--since", "2026-10-01") == ["C-OK"]


def test_stdout(cfg, invoke, tmp_path):
    full_campaign(cfg)
    r = invoke("--table", "findings", "--stdout")
    assert r.exit_code == 0, r.output
    rows = [json.loads(line) for line in r.output.splitlines()]
    assert [x["title"] for x in rows] == ["t1", "t2", "t3"]
    c = invoke("--table", "campaigns", "--stdout", "--format", "csv")
    got = list(csv.DictReader(io.StringIO(c.output)))
    assert got[0]["campaign"] == "C-FULL"
    assert not (S.agentic_dir(cfg) / "exports").exists()


def test_stdout_ignores_json(cfg, invoke):
    full_campaign(cfg)
    r = invoke("--table", "runs", "--stdout", "--json")
    assert r.exit_code == 0, r.output
    rows = [json.loads(line) for line in r.output.splitlines()]
    assert len(rows) == 4 and all("schema" not in x and "tables" not in x for x in rows)
    assert [n for n, _ in CE.RUN_COLUMNS] == list(rows[0])
    assert not (S.agentic_dir(cfg) / "exports").exists()


@pytest.mark.parametrize("args,msg", [
    (["--stdout"], "--stdout needs --table campaigns|runs|findings"),
    (["--table", "runs"], "--table is only used with --stdout"),
    (["--stdout", "--table", "runs", "--format", "parquet"], "--stdout supports jsonl or csv"),
    (["--stdout", "--table", "runs", "--out", "{out}"], "--out and --stdout are exclusive"),
])
def test_invalid_combinations(cfg, invoke, tmp_path, args, msg):
    out = tmp_path / "never-created"
    r = invoke(*[a.format(out=out) for a in args])
    assert r.exit_code != 0 and msg in r.output
    assert not (S.agentic_dir(cfg) / "exports").exists() and not out.exists()


def test_csv_encoding(cfg, invoke, tmp_path):
    full_campaign(cfg)
    d = tmp_path / "csv"
    assert invoke("--format", "csv", "--out", str(d)).exit_code == 0
    row = next(csv.DictReader((d / "campaigns.csv").open()))
    assert list(row) == [n for n, _ in CE.CAMPAIGN_COLUMNS]
    assert json.loads(row["runs_by_persona"]) == {"implementer": 2, "gate_triager": 2}
    assert json.loads(row["signals"]) == ["loops_exhausted", "cost_near_cap", "many_fix_loops"]
    assert row["loops"] == "2" and row["outcome"] == "MERGED"
    assert row["reviewer_runs"] == "0" and row["findings_per_reviewer_run"] == ""
    assert row["first_review_major"] == "1" and row["task_active_s"] == str(115 * 60)
    runs = list(csv.DictReader((d / "runs.csv").open()))
    assert runs[0]["is_error"] == "false" and runs[0]["input_tokens"] == ""
    assert json.loads((d / "manifest.json").read_text())["format"] == "csv"


def test_read_only(cfg, invoke, tmp_path):
    full_campaign(cfg, "C-1")
    make(cfg, "C-2", [(5, "run.start", run_start("implementer", "001-implementer"))],
         created=3)
    snap = {}
    root = S.campaigns_dir(cfg)
    for p in root.rglob("*"):
        if p.is_file():
            snap[p] = (p.stat().st_mtime_ns, p.read_bytes())
    assert invoke("--out", str(tmp_path / "ro")).exit_code == 0
    assert invoke("--table", "runs", "--stdout").exit_code == 0
    after = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in root.rglob("*") if p.is_file()}
    assert after == snap
    assert not any(str(tmp_path / "ro") in str(p) for p in after)


# ---- parquet -----------------------------------------------------------------------------

def test_parquet_round_trip(cfg, invoke, tmp_path):
    pq = pytest.importorskip("pyarrow.parquet")
    import pyarrow as pa
    full_campaign(cfg)
    d = tmp_path / "pq"
    r = invoke("--format", "parquet", "--out", str(d))
    assert r.exit_code == 0, r.output
    t = pq.read_table(d / "campaigns.parquet")
    assert t.column_names == [n for n, _ in CE.CAMPAIGN_COLUMNS]
    assert t.schema.field("loops").type == pa.int64()
    assert t.schema.field("cost_usd").type == pa.float64()
    assert t.schema.field("signals").type == pa.string()
    row = t.to_pylist()[0]
    assert row["loops"] == 2 and json.loads(row["signals"])[0] == "loops_exhausted"
    assert t.schema.field("reviewer_runs").type == pa.int64()
    assert t.schema.field("findings_per_reviewer_run").type == pa.float64()
    assert row["findings_per_reviewer_run"] is None and row["close_s"] == 300
    runs = pq.read_table(d / "runs.parquet")
    assert runs.schema.field("is_error").type == pa.bool_() and runs.num_rows == 4
    assert json.loads((d / "manifest.json").read_text())["format"] == "parquet"


def test_parquet_without_pyarrow(cfg, invoke, monkeypatch, tmp_path):
    full_campaign(cfg)
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)
    d = tmp_path / "nope"
    r = invoke("--format", "parquet", "--out", str(d))
    assert r.exit_code != 0
    assert "--format parquet needs pyarrow (pip install pyarrow); use jsonl or csv" in r.output
    assert not d.exists() and not (S.agentic_dir(cfg) / "exports").exists()


# ---- failure handling --------------------------------------------------------------------

def test_non_ascii_written_as_utf8(cfg, invoke, tmp_path, monkeypatch):
    import os
    make(cfg, "C-U", [(1, "finding", {"persona": "p", "severity": "major",
                                      "title": "naïve — café → ok"})])
    real = os.fdopen

    def ascii_locale(fd, mode="r", *a, encoding=None, **kw):
        return real(fd, mode, *a, encoding=encoding or "ascii", **kw)   # the "locale" default

    monkeypatch.setattr(os, "fdopen", ascii_locale)
    for fmt in ("jsonl", "csv"):
        d = tmp_path / fmt
        r = invoke("--format", fmt, "--out", str(d))
        assert r.exit_code == 0, r.output
        assert "naïve — café → ok" in (d / f"findings.{fmt}").read_text(encoding="utf-8")


@pytest.mark.parametrize("existed", [False, True])
def test_failed_write_leaves_no_partial_export(cfg, invoke, tmp_path, monkeypatch, existed):
    full_campaign(cfg)
    real, calls = CE._atomic_write, []

    def flaky(path, content, **kw):
        calls.append(path)
        if len(calls) == 3:
            raise OSError("disk full")
        return real(path, content, **kw)

    monkeypatch.setattr(CE, "_atomic_write", flaky)
    d = tmp_path / "half"
    if existed:
        d.mkdir()
    r = invoke("--out", str(d))
    assert r.exit_code != 0 and "export failed, nothing written: disk full" in r.output
    assert len(calls) == 3
    assert d.is_dir() == existed and (not existed or list(d.iterdir()) == [])
    monkeypatch.setattr(CE, "_atomic_write", real)
    assert invoke("--out", str(d)).exit_code == 0           # the same --out can be retried


def test_parquet_type_error_writes_nothing(cfg, invoke, tmp_path, monkeypatch):
    full_campaign(cfg)
    monkeypatch.setattr(CE, "have_pyarrow", lambda: True)
    written = []
    monkeypatch.setattr(CE, "_write_parquet", lambda *a: written.append(a))

    def table(rows, columns):
        if columns is CE.RUN_COLUMNS:
            raise TypeError("Expected bytes, got int")
        return object()

    monkeypatch.setattr(CE, "_parquet_table", table)
    d = tmp_path / "pqfail"
    r = invoke("--format", "parquet", "--out", str(d))
    assert r.exit_code != 0 and "Expected bytes, got int" in r.output
    assert written == [] and not d.exists()


def test_failed_write_cleanup_cannot_mask_the_error(cfg, invoke, tmp_path, monkeypatch):
    """A foreign subdirectory (a concurrent writer) must neither crash the cleanup nor be removed."""
    full_campaign(cfg)
    real, calls = CE._atomic_write, []

    def flaky(path, content, **kw):
        calls.append(path)
        if len(calls) == 3:
            (path.parent / "foreign").mkdir()
            raise OSError("disk full")
        return real(path, content, **kw)

    monkeypatch.setattr(CE, "_atomic_write", flaky)
    d = tmp_path / "raced"
    r = invoke("--out", str(d))
    assert r.exit_code != 0 and "export failed, nothing written: disk full" in r.output
    assert isinstance(r.exception, SystemExit)               # a ClickException, not a traceback
    assert [p.name for p in d.iterdir()] == ["foreign"]


def test_export_conductor_columns_appended(cfg, tmp_path):
    """SPEC-0299: two columns are the conductor's share and the total with it (SPEC-0303
    appended four more after them, SPEC-0396 nine more)."""
    from nightwright.agentic import conductor_cost as CC
    names = [n for n, _ in CE.CAMPAIGN_COLUMNS]
    assert names[-15:-13] == ["conductor_usd", "cost_with_conductor_usd"]
    make(cfg, "C1", [(10, "transition", tr("CLOSING", "MERGED"))], created=0,
         state="MERGED", cost={"total_usd": 2.5, "cap_usd": None, "by_persona": {}})
    make(cfg, "C2", [], created=600, cost={"total_usd": None, "cap_usd": None, "by_persona": {}})
    cfg["_root"] = str(tmp_path / "projects")
    cfg["agentic"]["conductor"] = {"prices": {"claude-x": {"input": 10.0}}}
    (tmp_path / "projects" / "-s").mkdir(parents=True)

    def transcript(model):
        stamp = (T0 + dt.timedelta(minutes=5)).astimezone(dt.timezone.utc).isoformat()
        (tmp_path / "projects" / "-s" / "sid.jsonl").write_text(json.dumps(
            {"type": "assistant", "timestamp": stamp, "message": {
                "id": "m", "model": model, "usage": {"input_tokens": 100_000}}}) + "\n")

    def rows():
        return {r["campaign"]: r for r in CE.export_tables(cfg, now=NOW)["campaigns"]}

    for r in rows().values():                           # no session registered
        assert (r["conductor_usd"], r["cost_with_conductor_usd"]) == (None, None)
    CC.record_session(cfg, "conductor", {}, source="manual", session_id="sid")
    transcript("unpriced-model")                        # a session, but no turn is priced
    for r in rows().values():
        assert (r["conductor_usd"], r["cost_with_conductor_usd"]) == (None, None)
    transcript("claude-x")
    got = rows()
    assert list(got["C1"])[-15:-13] == ["conductor_usd", "cost_with_conductor_usd"]
    assert got["C1"]["conductor_usd"] == 1.0 and got["C1"]["cost_with_conductor_usd"] == 3.5
    assert got["C2"]["conductor_usd"] == 0.0                     # no attributed turn
    assert got["C2"]["cost_usd"] is None
    assert got["C2"]["cost_with_conductor_usd"] == got["C2"]["conductor_usd"] == 0.0


def test_export_conflict_pressure_columns(cfg):                    # SPEC-0303
    def finding(title):
        return {"persona": "close", "severity": "info", "gate": "rebase", "title": title,
                "detail": "", "files": []}

    def cost(delta, total, cap, persona="conflict_resolver"):
        return {"persona": persona, "delta_usd": delta, "total_usd": total, "cap_usd": cap,
                "estimated": False}

    make(cfg, "C-PRESSURE", [
        (10, "finding", finding("generated files regenerated during rebase")),
        (11, "finding", finding("generated files regenerated during rebase")),
        (12, "finding", finding("rebase conflict; resolver dispatched")),
        (13, "finding", finding("resolver ran on the close allowance")),
        (14, "rebase_resolved", {"run_id": "003-conflict_resolver", "target_sha": "abc"}),
        (15, "cost", cost(0.5, 1.5, 2.0, persona="implementer")),     # not the resolver
        (16, "cost", cost(0.6, 2.4, 2.0)),       # 0.4 of the charge landed above the cap
        (17, "cost", cost(0.5, 2.9, 2.0)),       # all of it above
        (18, "cost", cost(0.3, 1.8, 2.0)),       # below the cap
    ], cost={"total_usd": 2.9, "cap_usd": 2.0, "by_persona": {}})
    make(cfg, "C-QUIET", [], created=1, cost={"total_usd": 1.0, "cap_usd": None, "by_persona": {}})
    rows = {r["campaign"]: r for r in CE.export_tables(cfg, now=NOW)["campaigns"]}
    row = rows["C-PRESSURE"]
    assert (row["rebase_regenerated"], row["resolver_dispatches"], row["rebase_resolved"]) \
        == (2, 1, 1)
    assert row["resolver_overcap_usd"] == pytest.approx(0.9)
    quiet = rows["C-QUIET"]
    assert (quiet["rebase_regenerated"], quiet["resolver_dispatches"], quiet["rebase_resolved"],
            quiet["resolver_overcap_usd"]) == (0, 0, 0, 0.0)
    assert [n for n, _ in CE.CAMPAIGN_COLUMNS][:42] == list(row)[:42]


# ---- SPEC-0321: conductor measurement hardening --------------------------------------------

def test_live_window_agrees_with_campaign_row_for_terminal_without_transition(cfg):
    from nightwright.agentic import conductor_cost as CC
    make(cfg, "C-NT", [(10, "transition", tr("QUEUED", "OPENING"))], state="ABANDONED")
    r = row_of(cfg, "C-NT")
    assert r["ended_at"] is None and r["wall_s"] is None          # not measured to `now`
    (w,) = CC.live_windows(cfg, now=NOW)
    assert w["end"] is None and w["live_end"] is not None
    assert w["live_end"] == dt.datetime.fromisoformat(ts(10))     # the last transition


def _conductor_setup(cfg, tmp_path, *, tokens=100_000, model="claude-x", sid="sid"):
    from nightwright.agentic import conductor_cost as CC
    make(cfg, "C1", [(10, "transition", tr("CLOSING", "MERGED"))], created=0,
         state="MERGED", cost={"total_usd": 2.5, "cap_usd": None, "by_persona": {}})
    cfg["_root"] = str(tmp_path / "projects")
    cfg["agentic"]["conductor"] = {"prices": {"claude-x": {"input": 10.0}}}
    (tmp_path / "projects" / "-s").mkdir(parents=True)
    stamp = (T0 + dt.timedelta(minutes=5)).astimezone(dt.timezone.utc).isoformat()
    (tmp_path / "projects" / "-s" / f"{sid}.jsonl").write_text(json.dumps(
        {"type": "assistant", "timestamp": stamp, "message": {
            "id": "m", "model": model, "usage": {"input_tokens": tokens}}}) + "\n")
    CC.record_session(cfg, "conductor", {}, source="manual", session_id=sid)


def test_export_conductor_usd_with_unpriced_turn_in_window(cfg, tmp_path):
    _conductor_setup(cfg, tmp_path)
    stamp = (T0 + dt.timedelta(minutes=6)).astimezone(dt.timezone.utc).isoformat()
    with open(tmp_path / "projects" / "-s" / "sid.jsonl", "a") as f:
        f.write(json.dumps({"type": "assistant", "timestamp": stamp, "message": {
            "id": "u", "model": "other-y", "usage": {"input_tokens": 500_000}}}) + "\n")
    (row,) = CE.export_tables(cfg, now=NOW)["campaigns"]
    assert row["conductor_usd"] == 1.0                            # the priced turn only
    assert row["cost_with_conductor_usd"] == 3.5


def test_export_survives_attribute_raising(cfg, tmp_path, monkeypatch, capsys):
    from nightwright.agentic import conductor_cost as CC
    _conductor_setup(cfg, tmp_path)
    real = CC.attribute

    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(CC, "attribute", boom)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    out = tmp_path / "out"
    r = CliRunner().invoke(cli, ["campaign", "export", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert (r.output + capsys.readouterr().err).count(
        "conductor columns skipped: RuntimeError") == 1
    (row,) = read_jsonl(next(out.rglob("campaigns.jsonl")))
    assert row["conductor_usd"] is None and row["cost_with_conductor_usd"] is None
    monkeypatch.setattr(CC, "attribute", real)
    (row,) = CE.export_tables(cfg, now=NOW)["campaigns"]
    assert row["conductor_usd"] == 1.0


def test_export_tolerates_malformed_conductor_config_and_bad_registry_byte(
        cfg, tmp_path, capsys):
    from nightwright.agentic import conductor_cost as CC
    _conductor_setup(cfg, tmp_path)
    cfg["agentic"]["conductor"] = [1]
    (row,) = CE.export_tables(cfg, now=NOW)["campaigns"]
    assert row["conductor_usd"] is None and row["cost_with_conductor_usd"] is None
    cfg["agentic"]["conductor"] = {"prices": {"claude-x": {"input": 10.0}}}
    reg = CC.conductor_dir(cfg) / "sessions.jsonl"
    reg.write_bytes(b"\xff\xfe\n" + reg.read_bytes())
    (row,) = CE.export_tables(cfg, now=NOW)["campaigns"]
    assert row["conductor_usd"] == 1.0 and row["cost_with_conductor_usd"] == 3.5
    assert capsys.readouterr().err == ""


# ---- treatment-aware difficulty metrics (SPEC-0396) ---------------------------------------

def finding(persona, severity, title="f"):
    return {"persona": persona, "severity": severity, "title": title}


def test_reviewer_run_counts(cfg):
    make(cfg, "C-RR", [
        (1, "run.end", run_end("conformance_reviewer", "001")),
        (2, "run.end", run_end("adversarial_reviewer", "002")),
        (3, "run.end", run_end("test_reviewer", "003", interrupted=True)),
        (4, "run.end", run_end("test_reviewer", "004", verdict_ok=False, verdict=None)),
        (5, "run.end", run_end("implementer", "005")),
        (6, "run.end", run_end("declared_reviewer", "006")),
    ])
    assert row_of(cfg, "C-RR")["reviewer_runs"] == 2


def test_reviewer_findings_exclude_other_personas(cfg):
    make(cfg, "C-RF", [
        (1, "finding", finding("conformance_reviewer", "must_fix")),
        (1, "finding", finding("adversarial_reviewer", "major")),
        (1, "finding", finding("test_reviewer", "minor")),
        (1, "finding", finding("test_reviewer", "info")),
        (1, "finding", finding("close", "must_fix")),
        (1, "finding", finding("implementer", "major")),
        (1, "finding", finding("gate_triager", "must_fix")),
    ])
    r = row_of(cfg, "C-RF")
    assert (r["reviewer_findings"], r["reviewer_blocking"]) == (3, 2)
    assert r["must_fix_count"] == 3 and r["finding_count"] == 6      # old columns: every persona


def test_per_run_ratios_and_null_denominator(cfg):
    make(cfg, "C-RATIO", [
        (1, "run.end", run_end("conformance_reviewer", "001")),
        (1, "run.end", run_end("test_reviewer", "002")),
        (1, "run.end", run_end("adversarial_reviewer", "003")),
        (2, "finding", finding("conformance_reviewer", "must_fix")),
        (2, "finding", finding("test_reviewer", "minor")),
        (2, "finding", finding("test_reviewer", "minor")),
        (2, "finding", finding("adversarial_reviewer", "minor")),
    ])
    r = row_of(cfg, "C-RATIO")
    assert r["findings_per_reviewer_run"] == 1.333 and r["blocking_per_reviewer_run"] == 0.333
    make(cfg, "C-ZERO", [])
    z = row_of(cfg, "C-ZERO")
    assert z["findings_per_reviewer_run"] is None and z["blocking_per_reviewer_run"] is None
    assert [z[k] for k in ("reviewer_runs", "reviewer_findings", "reviewer_blocking",
                           "first_review_must_fix", "first_review_major")] == [0] * 5


def _round(first_to, mid, second_tail=()):
    """OPENING→IMPLEMENTING→REVIEWING with round-1 findings, then `mid` transitions, then
    findings that belong to a later round (1 must_fix, 1 major), then `second_tail`."""
    return [
        (1, "transition", tr("QUEUED", "OPENING", "admit")),
        (2, "transition", tr("OPENING", "IMPLEMENTING", "opened")),
        *first_to,
        (10, "transition", tr("IMPLEMENTING", "REVIEWING", "implemented")),
        (11, "finding", finding("conformance_reviewer", "must_fix")),
        (11, "finding", finding("test_reviewer", "major")),
        (11, "finding", finding("test_reviewer", "major")),
        (11, "finding", finding("test_reviewer", "minor")),
        *mid,
        (30, "finding", finding("conformance_reviewer", "must_fix")),
        (30, "finding", finding("conformance_reviewer", "major")),
        *second_tail,
    ]


def test_first_review_round_boundary(cfg):
    fix = [(20, "transition", tr("REVIEWING", "IMPLEMENTING", "review_fix")),
           (25, "transition", tr("IMPLEMENTING", "REVIEWING", "implemented"))]
    make(cfg, "C-B1", _round([], fix))
    r = row_of(cfg, "C-B1")
    assert (r["first_review_must_fix"], r["first_review_major"]) == (1, 2)
    assert r["reviewer_blocking"] == 5

    # a triage_fix IMPLEMENTING entry before the first REVIEWING entry does not end the round
    early = [(5, "transition", tr("IMPLEMENTING", "GATING", "implemented")),
             (6, "transition", tr("GATING", "IMPLEMENTING", "triage_fix"))]
    make(cfg, "C-B2", _round(early, fix))
    r = row_of(cfg, "C-B2")
    assert (r["first_review_must_fix"], r["first_review_major"]) == (1, 2)

    # park/resume back into REVIEWING does not end the round: everything counts
    park = [(15, "transition", tr("REVIEWING", "PARKED", "review_escalated")),
            (18, "transition", tr("PARKED", "REVIEWING", "resume"))]
    make(cfg, "C-B3", _round([], park))
    r = row_of(cfg, "C-B3")
    assert (r["first_review_must_fix"], r["first_review_major"]) == (2, 3)

    # loop_exhausted park, then the resume goes to IMPLEMENTING (not review_fix)
    exhausted = [(15, "transition", tr("REVIEWING", "PARKED", "loop_exhausted")),
                 (18, "transition", tr("PARKED", "IMPLEMENTING", "resume")),
                 (22, "transition", tr("IMPLEMENTING", "REVIEWING", "implemented"))]
    make(cfg, "C-B4", _round([], exhausted))
    r = row_of(cfg, "C-B4")
    assert (r["first_review_must_fix"], r["first_review_major"]) == (1, 2)

    # never back to IMPLEMENTING: all reviewer findings count
    make(cfg, "C-B5", _round([], []))
    r = row_of(cfg, "C-B5")
    assert (r["first_review_must_fix"], r["first_review_major"]) == (2, 3)


def test_treatment_independence(cfg):
    round1 = [
        (1, "transition", tr("QUEUED", "OPENING", "admit")),
        (2, "transition", tr("OPENING", "IMPLEMENTING", "opened")),
        (10, "transition", tr("IMPLEMENTING", "REVIEWING", "implemented")),
        (11, "run.end", run_end("conformance_reviewer", "001")),
        (11, "run.end", run_end("test_reviewer", "002")),
        (11, "finding", finding("conformance_reviewer", "must_fix")),
        (11, "finding", finding("test_reviewer", "major")),
    ]
    round2 = [
        (20, "transition", tr("REVIEWING", "IMPLEMENTING", "review_fix")),
        (25, "transition", tr("IMPLEMENTING", "REVIEWING", "implemented")),
        (26, "run.end", run_end("conformance_reviewer", "003")),
        (26, "run.end", run_end("test_reviewer", "004")),
        (26, "finding", finding("conformance_reviewer", "must_fix")),
        (26, "finding", finding("test_reviewer", "major")),
        (26, "finding", finding("test_reviewer", "minor")),
    ]
    make(cfg, "C-A", round1)
    make(cfg, "C-BB", round1 + round2)
    a, b = row_of(cfg, "C-A"), row_of(cfg, "C-BB")
    assert (a["first_review_must_fix"], a["first_review_major"]) == (1, 1)
    assert (b["first_review_must_fix"], b["first_review_major"]) == (1, 1)
    assert b["reviewer_runs"] > a["reviewer_runs"]
    assert b["reviewer_findings"] > a["reviewer_findings"]


def test_task_active_excludes_closing_and_parked(cfg):
    make(cfg, "C-TA", [
        (10, "transition", tr("QUEUED", "OPENING", "admit")),
        (15, "transition", tr("OPENING", "IMPLEMENTING", "opened")),
        (45, "transition", tr("IMPLEMENTING", "CLOSING", "approved")),
        (50, "transition", tr("CLOSING", "PARKED", "rebase_conflict")),
        (110, "transition", tr("PARKED", "CLOSING", "resume")),
        (115, "transition", tr("CLOSING", "MERGED", "merged")),
    ], state="MERGED")
    r = row_of(cfg, "C-TA")
    assert r["close_s"] == 10 * 60                       # 45..50 and 110..115
    assert r["active_s"] == (5 + 30 + 10) * 60
    assert r["task_active_s"] == (5 + 30) * 60 == r["active_s"] - r["close_s"]
    assert r["time_in_state_s"]["PARKED"] == 60 * 60     # in neither column
    make(cfg, "C-NC", [(10, "transition", tr("QUEUED", "OPENING", "admit")),
                       (20, "transition", tr("OPENING", "ABANDONED", "x"))], state="ABANDONED")
    n = row_of(cfg, "C-NC")
    assert n["close_s"] == 0 and n["task_active_s"] == n["active_s"] == 10 * 60


def test_unknown_severity_not_counted_as_blocking(cfg):
    # `make` rejects events that fail ledger_problems, so call campaign_row on raw events.
    events = [
        {"type": "transition", "ts": ts(1), "from": "QUEUED", "to": "REVIEWING"},
        {"type": "finding", "ts": ts(2), "persona": "test_reviewer", "severity": ["must_fix"]},
        {"type": "finding", "ts": ts(2), "persona": "test_reviewer", "severity": "catastrophic"},
        {"type": "finding", "ts": ts(2), "persona": "test_reviewer", "severity": None},
        {"type": "finding", "ts": ts(2), "persona": "test_reviewer", "severity": "major"},
    ]
    row = CE.campaign_row(cfg, {"id": "C-SEV", "state": "REVIEWING", "created_at": ts(0)},
                          events, NOW)
    assert row["reviewer_findings"] == 4 and row["reviewer_blocking"] == 1
    assert (row["first_review_must_fix"], row["first_review_major"]) == (0, 1)
