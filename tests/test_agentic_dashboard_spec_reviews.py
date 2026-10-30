"""SPEC-0217: standalone spec reviews in `wt queue watch`, the spec-phase queue events and the
conduct skill's spec-phase park rows."""
import datetime as dt
import json
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
          "findings": [{"severity": "must_fix", "section": "Design", "title": "gap"}]}
SKILL = Path(__file__).resolve().parent.parent / "skills" / "wt-conduct" / "SKILL.md"


def _render(p, width=120):
    console = Console(width=width, record=True, force_terminal=False, color_system=None)
    console.print(CQ._dashboard(p, width))
    return console.export_text()


def _iso(delta_s):
    return (dt.datetime.now().astimezone() - dt.timedelta(seconds=delta_s)).isoformat(
        timespec="seconds")


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


def _rows(cfg, **kw):
    return {r["run_id"]: r for r in Q.dashboard_payload(cfg, **kw)["spec_reviews"]}


def test_cli_review_running_then_ended(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, review=False)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [
        {"persona": "spec_reviewer", "verdict": REVISE, "cost_usd": 0.0, "sleep_s": 2}]))
    out = {}
    t = threading.Thread(target=lambda: out.update(
        r=run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001", "--json"])))
    t.start()
    until(lambda: Q.dashboard_payload(cfg)["spec_reviews"], timeout=10)
    p = Q.dashboard_payload(cfg)
    assert [r["status"] for r in p["spec_reviews"]] == ["running"]
    assert p["totals"]["reviews_running"] == 1
    assert "reviews 1" in _render(p)
    join(t, 15)
    assert out["r"].exit_code == 0, out["r"].output
    p = Q.dashboard_payload(cfg)
    assert p["totals"]["reviews_running"] == 0
    row = p["spec_reviews"][0]
    assert row["status"] == "revise" and row["counts"] == {"must_fix": 1}
    text = _render(p)
    assert "Spec reviews (1)" in text and "revise" in text and "1 must_fix" in text
    assert "$0.00" in text and "reviews 0" not in text and "reviews 1" not in text
    # --project hides the section (and the chip while running)
    assert Q.dashboard_payload(cfg, project="Meta-Tools")["spec_reviews"] == []
    assert "Spec reviews" not in _render(Q.dashboard_payload(cfg, project="Meta-Tools"))


def test_running_hidden_by_project(tmp_path):
    _, cfg = _cfg(tmp_path)
    _journal(cfg, _start("r1", 5))
    p = Q.dashboard_payload(cfg, project="Meta-Tools")
    assert p["spec_reviews"] == [] and p["totals"]["reviews_running"] == 0
    assert "reviews 1" not in _render(p) and "Spec reviews" not in _render(p)
    assert Q.dashboard_payload(cfg)["totals"]["reviews_running"] == 1


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
    # the failed orphan disappears recent_hours after started_at + timeout_s
    _journal(cfg, _start("orphan2", 600 + 2 * h, timeout_s=600))
    assert "orphan2" not in _rows(cfg, recent_hours=1)
    assert "orphan2" in _rows(cfg, recent_hours=3)
    text = _render(Q.dashboard_payload(cfg, recent_hours=1))
    assert "failed" in text and "—" in text and "$0.07" in text


def test_order_running_first_then_recent(tmp_path):
    _, cfg = _cfg(tmp_path)
    _journal(
        cfg,
        _start("e_old", 900), _end("e_old", 800, verdict="approve", counts={}),
        _start("run_new", 20), _start("run_old", 90),
        _start("e_new", 500), _end("e_new", 100, verdict="split", counts={"major": 2}),
        _start("orphan", 1000, timeout_s=300))                      # failed at now-700
    ids = [r["run_id"] for r in Q.dashboard_payload(cfg)["spec_reviews"]]
    assert ids == ["run_old", "run_new", "e_new", "orphan", "e_old"]
    assert "2 major" in _render(Q.dashboard_payload(cfg))


def test_end_without_start_uses_its_ts(tmp_path):
    _, cfg = _cfg(tmp_path)
    _journal(cfg, _end("lonely", 60, verdict="approve", counts={}, cost_usd=0.15))
    row = _rows(cfg)["lonely"]
    assert row["status"] == "approve" and row["started_at"] == _iso(60)
    assert "$0.15" in _render(Q.dashboard_payload(cfg))


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


def test_conduct_skill_rows():
    text = SKILL.read_text(encoding="utf-8")
    for reason in ("spec_writer_unavailable", "spec_review_unavailable", "spec_write_failed",
                   "spec_review_exhausted"):
        assert any(line.startswith("| `parked` `%s`" % reason) for line in text.splitlines()), \
            reason
