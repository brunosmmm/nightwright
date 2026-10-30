"""SPEC-0217: standalone spec reviews in `wt queue watch`, the spec-phase queue events and the
conduct skill's spec-phase park rows."""
import datetime as dt
import json
import re
import threading
from pathlib import Path

from rich.console import Console

from test_agentic_dashboard import _campaign, _ev
from waits import join, until
from test_agentic_e2e import make_cfg, make_repo, run_cli, script
from wt.agentic import cli_queue as CQ
from wt.agentic import events as E
from wt.agentic import queue as Q
from wt.agentic import spec_review as SR
from wt.agentic import store as S

SIZED = {"size": "m", "tone": "high-end", "rationale": "small."}
REVISE = {"verdict": "revise", **SIZED, "suggested_splits": [],
          "findings": [{"severity": "must_fix", "section": "Design", "title": "gap",
                        "suggested_fix": "fill the gap"}]}
SKILL = Path(__file__).resolve().parent.parent / "skills" / "wt-conduct" / "SKILL.md"


def _render(p, width=120):
    console = Console(width=width, record=True, force_terminal=False, color_system=None)
    console.print(CQ._dashboard(p, width))
    return console.export_text()


NOW = dt.datetime(2026, 10, 30, 12, 0, 0, tzinfo=dt.timezone.utc)   # fixed: no wall-clock races


def _iso(delta_s):
    return (NOW - dt.timedelta(seconds=delta_s)).isoformat(timespec="seconds")


def _journal(cfg, *recs):
    p = SR.journal_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps({"schema": SR.RUN_SCHEMA, **r}) + "\n")


def _start(rid, ago_s, timeout_s=600, spec="SPEC-0001"):
    return {"kind": "start", "run_id": rid, "spec_id": spec, "ts": _iso(ago_s),
            "timeout_s": timeout_s}


def _end(rid, ago_s, spec="SPEC-0001", **kw):
    return {"kind": "end", "run_id": rid, "spec_id": spec, "ts": _iso(ago_s),
            "verdict": None, "counts": None, "cost_usd": None, "duration_s": None,
            "error": None, **kw}


def _cfg(tmp_path):
    repo = make_repo(tmp_path)
    return repo, make_cfg(tmp_path, repo, script(tmp_path, []))


def _payload(cfg, **kw):
    return Q.dashboard_payload(cfg, now=NOW, **kw)


def _rows(cfg, **kw):
    return {r["run_id"]: r for r in _payload(cfg, **kw)["spec_reviews"]}


def _row_line(text, needle):
    return next(line for line in text.splitlines() if needle in line)


def test_cli_review_running_then_ended(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, review=False)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [
        {"persona": "spec_reviewer", "verdict": REVISE, "cost_usd": 0.0, "sleep_s": 4}]))
    out, seen = {}, {}
    t = threading.Thread(target=lambda: out.update(
        r=run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001", "--json"])))
    t.start()

    def running():            # sample inside the poll: the running state is what we asserted on
        p = Q.dashboard_payload(cfg)
        if p["spec_reviews"]:
            seen["p"], seen["text"] = p, _render(p)
        return p["spec_reviews"]
    until(running, timeout=10)
    p = seen["p"]
    assert [r["status"] for r in p["spec_reviews"]] == ["running"]
    assert p["spec_reviews"][0]["duration_s"] >= 0 and p["spec_reviews"][0]["ended_at"] is None
    assert p["totals"]["reviews_running"] == 1
    assert "reviews 1" in seen["text"] and "▶" in _row_line(seen["text"], "SPEC-0001")
    join(t, 20)
    assert out["r"].exit_code == 0, out["r"].output
    p = Q.dashboard_payload(cfg)
    assert p["totals"]["reviews_running"] == 0
    row = p["spec_reviews"][0]
    assert row["status"] == "revise" and row["counts"] == {"must_fix": 1}
    assert row["ended_at"] is not None
    text = _render(p)
    assert "Spec reviews (1)" in text and "revise" in text and "1 must_fix" in text
    assert "$0.00" in text and "reviews 0" not in text and "reviews 1" not in text
    line = _row_line(text, "revise")
    assert re.search(r"\b\d+[smhd]\s*$", line.rstrip(" │")) and "▶" not in line
    # --project hides the section (and the chip while running)
    assert Q.dashboard_payload(cfg, project="Meta-Tools")["spec_reviews"] == []
    assert "Spec reviews" not in _render(Q.dashboard_payload(cfg, project="Meta-Tools"))


def test_running_hidden_by_project(tmp_path):
    _, cfg = _cfg(tmp_path)
    _journal(cfg, _start("r1", 5))
    p = _payload(cfg, project="Meta-Tools")
    assert p["spec_reviews"] == [] and p["totals"]["reviews_running"] == 0
    assert "reviews 1" not in _render(p) and "Spec reviews" not in _render(p)
    assert _payload(cfg)["totals"]["reviews_running"] == 1


def test_backdated_failed_and_window(tmp_path):
    _, cfg = _cfg(tmp_path)
    h = 3600
    _journal(
        cfg,
        _start("orphan", 700, timeout_s=600),                       # past its timeout: failed
        _start("orphan_old", 30 * h, timeout_s=600),                # failed, then aged out
        _start("err", 400), _end("err", 300, cost_usd=0.07, duration_s=100,
                                 error="boom", counts={"major": 1}),
        _start("old", 30 * h), _end("old", 30 * h - 60, verdict="approve", counts={}),
        _start("long", int(2.5 * h), timeout_s=3 * h))              # still running
    rows = _rows(cfg, recent_hours=1)
    assert set(rows) == {"orphan", "err", "long"}
    assert rows["orphan"]["status"] == "failed"
    assert rows["err"]["status"] == "failed" and rows["err"]["counts"] is None
    assert rows["err"]["cost_usd"] == 0.07 and rows["err"]["duration_s"] == 100
    assert rows["long"]["status"] == "running"
    assert rows["long"]["duration_s"] == int(2.5 * h) and rows["long"]["ended_at"] is None
    assert rows["orphan"]["ended_at"] == _iso(100)               # started_at + timeout_s
    assert rows["err"]["ended_at"] == _iso(300)
    # the failed orphan disappears recent_hours after started_at + timeout_s
    _journal(cfg, _start("orphan2", 600 + 2 * h, timeout_s=600))
    assert "orphan2" not in _rows(cfg, recent_hours=1)
    assert "orphan2" in _rows(cfg, recent_hours=3)
    text = _render(_payload(cfg, recent_hours=1))
    assert "failed" in text and "—" in text and "$0.07" in text
    assert "▶ 2h30m" in _row_line(text, "▶")
    # when: running shows ▶ elapsed; failed rows age their end (err: 300s -> 5m, orphan: 100s -> 1m)
    lines = [ln for ln in text.splitlines() if "failed" in ln]
    assert any("$0.07" in ln and "5m" in ln for ln in lines)
    assert any("—" in ln and "1m" in ln for ln in lines)
    assert not any("▶" in ln for ln in lines)


def test_order_running_first_then_recent(tmp_path):
    _, cfg = _cfg(tmp_path)
    _journal(
        cfg,
        _start("e_old", 900), _end("e_old", 800, verdict="approve", counts={}),
        _start("run_new", 20), _start("run_old", 90),
        _start("e_new", 500), _end("e_new", 100, verdict="split", counts={"major": 2}),
        _start("orphan", 1000, timeout_s=300))                      # failed at now-700
    ids = [r["run_id"] for r in _payload(cfg)["spec_reviews"]]
    assert ids == ["run_old", "run_new", "e_new", "orphan", "e_old"]
    assert "2 major" in _render(_payload(cfg))


def test_end_without_verdict_or_error_is_failed(tmp_path):
    _, cfg = _cfg(tmp_path)
    _journal(cfg, _start("blank", 200), _end("blank", 100, counts={"major": 3}, cost_usd=0.5),
             _start("badts", 200), _end("badts", 0, ts="not-a-date", verdict="approve"))
    rows = _rows(cfg)
    assert rows["blank"]["status"] == "failed" and rows["blank"]["counts"] is None
    assert rows["blank"]["cost_usd"] == 0.5
    assert "badts" not in rows                    # an end with an unreadable ts is skipped


def test_malformed_records_do_not_break_the_dashboard(tmp_path):
    _, cfg = _cfg(tmp_path)
    _journal(
        cfg,
        {"kind": "start", "run_id": "tstr", "ts": _iso(30), "timeout_s": "30m",
         "spec_id": "SPEC-0002"},
        {"kind": "start", "run_id": ["x"], "ts": _iso(30)},
        {"kind": "end", "run_id": {"a": 1}, "ts": _iso(30), "verdict": "approve"},
        _start("badcounts", 90), _end("badcounts", 60, verdict="approve", counts=["a"],
                                      cost_usd="free", duration_s="long"),
        _start("badverdict", 90), _end("badverdict", 60, verdict=["approve"]))
    p = _payload(cfg)
    rows = {r["run_id"]: r for r in p["spec_reviews"]}
    assert set(rows) == {"tstr", "badcounts", "badverdict"}
    assert rows["tstr"]["status"] == "failed"           # unreadable timeout_s counts as 0
    assert rows["badcounts"]["status"] == "approve" and rows["badcounts"]["counts"] is None
    assert rows["badcounts"]["cost_usd"] is None and rows["badcounts"]["duration_s"] is None
    assert rows["badverdict"]["status"] == "failed"
    assert "Spec reviews (3)" in _render(p)


def test_end_without_start_uses_its_ts(tmp_path):
    _, cfg = _cfg(tmp_path)
    _journal(cfg, _end("lonely", 60, verdict="approve", counts={}, cost_usd=0.15))
    row = _rows(cfg)["lonely"]
    assert row["status"] == "approve" and row["started_at"] == _iso(60)
    assert "$0.15" in _render(_payload(cfg))


def test_boundary_instants(tmp_path):
    _, cfg = _cfg(tmp_path)
    _journal(cfg, _start("exact", 600, timeout_s=600),            # now == started + timeout_s
             _start("inside", 599, timeout_s=600),
             _start("o_edge", 2 * 3600 + 600, timeout_s=600),     # failed at exactly now - 2h
             _start("o_gone", 2 * 3600 + 601, timeout_s=600),
             _start("e_edge", 3700), _end("e_edge", 2 * 3600, verdict="approve", counts={}),
             _start("e_gone", 3700), _end("e_gone", 2 * 3600 + 1, verdict="approve", counts={}))
    rows = _rows(cfg, recent_hours=2)
    assert rows["exact"]["status"] == "failed" and rows["inside"]["status"] == "running"
    assert set(rows) == {"exact", "inside", "o_edge", "e_edge"}


def test_nonfinite_or_huge_timeout_does_not_break_the_dashboard(tmp_path):
    _, cfg = _cfg(tmp_path)
    p = SR.journal_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = _iso(30)
    p.write_text("".join(
        '{"kind":"start","run_id":"%s","ts":"%s","timeout_s":%s}\n' % (rid, ts, v)
        for rid, v in (("inf", "Infinity"), ("nan", "NaN"), ("big", "1e20"))), encoding="utf-8")
    pay = _payload(cfg)
    assert {r["run_id"]: r["status"] for r in pay["spec_reviews"]} == \
        {"inf": "failed", "nan": "failed", "big": "failed"}
    assert "Spec reviews (3)" in _render(pay)


def test_unreadable_journal_does_not_break_the_dashboard(tmp_path, monkeypatch):
    _, cfg = _cfg(tmp_path)
    _journal(cfg, _start("r", 30))

    def boom(path, n):
        raise PermissionError("denied")
    monkeypatch.setattr(SR, "_tail_lines", boom)
    pay = _payload(cfg)
    assert pay["spec_reviews"] == [] and pay["totals"]["reviews_running"] == 0


def test_bad_start_ts_keeps_the_paired_end(tmp_path):
    _, cfg = _cfg(tmp_path)
    _journal(cfg, {**_start("r", 200), "ts": "garbage"},
             _end("r", 100, verdict="revise", counts={"must_fix": 1}, cost_usd=0.25),
             {**_start("lost", 200), "ts": "garbage"})
    rows = _rows(cfg)
    assert rows["r"]["status"] == "revise" and rows["r"]["started_at"] == _iso(100)
    assert rows["r"]["cost_usd"] == 0.25
    assert "lost" not in rows                        # a start with neither a usable ts nor an end


def test_missing_spec_id_renders_a_dash(tmp_path):
    _, cfg = _cfg(tmp_path)
    _journal(cfg, {**_end("r", 60, verdict="approve", counts={}), "spec_id": None})
    line = _row_line(_render(_payload(cfg)), "approve")
    assert "None" not in line and "—" in line and "none" in line


def test_spec_phase_events_projected_once(tmp_path):
    repo, cfg = _cfg(tmp_path)
    cid = _campaign(cfg, repo, "IDEA-078-ev", "SPEC_WRITING", kind="idea", spec_id="SPEC-0001")
    _ev(cfg, cid, "spec_written", {"spec_id": "SPEC-0001", "rel_path": "x.md", "title": "t",
                                   "mode": "write"})
    _ev(cfg, cid, "spec_review", {"verdict": "revise", "round": 1, "spec_sha256": "ab"})
    _ev(cfg, cid, "spec_accepted", {"spec_id": "SPEC-0001", "round": 2})
    S.append_event(cfg, cid, "transition", {"from": "SPEC_REVIEW", "to": "IMPLEMENTING",
                                            "reason": "spec_accepted"})
    E.sync(cfg)
    E.sync(cfg)
    evs = [e for e in E.read_since(cfg, 0) if e["type"].startswith("spec_")]
    assert [e["type"] for e in evs] == ["spec_written", "spec_reviewed", "spec_accepted"]
    assert all(e["data"]["spec_id"] == "SPEC-0001" for e in evs)
    assert evs[1]["data"]["verdict"] == "revise" and evs[1]["data"]["round"] == 1
    assert [e["type"] for e in E.read_since(cfg, 0) if e.get("campaign") == cid] == \
        [e["type"] for e in evs]               # the accept's own transition projects nothing


def test_event_spec_id_prefers_the_ledger_event(tmp_path):
    repo, cfg = _cfg(tmp_path)
    cid = _campaign(cfg, repo, "IDEA-078-pref", "SPEC_WRITING", kind="idea", spec_id="SPEC-0001")
    _ev(cfg, cid, "spec_review", {"spec_id": "SPEC-0099", "verdict": "approve", "round": 1})
    _ev(cfg, cid, "spec_accepted", {"spec_id": "SPEC-0099", "round": 1})
    E.sync(cfg)
    evs = [e for e in E.read_since(cfg, 0) if e["type"].startswith("spec_")]
    assert [e["data"]["spec_id"] for e in evs] == ["SPEC-0099", "SPEC-0099"]


def test_conduct_skill_rows():
    text = SKILL.read_text(encoding="utf-8")
    for reason in ("spec_writer_unavailable", "spec_review_unavailable", "spec_write_failed",
                   "spec_review_exhausted"):
        assert any(line.startswith("| `parked` `%s`" % reason) for line in text.splitlines()), \
            reason
