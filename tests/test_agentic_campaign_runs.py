"""SPEC-0199: `nw campaign runs|run` — persona runs without reading run files."""
import json
import shutil
from pathlib import Path

import pytest

from test_agentic_e2e import (_git_env, added, impl_done, make_cfg, make_repo, review, run_cli,
                              script)
from nightwright.agentic import events as EV
from nightwright.agentic import runs as RN
from nightwright.agentic import schemas as SC
from nightwright.agentic import store as S

pytestmark = pytest.mark.fast

LIST_KEYS = {"campaign", "run_id", "persona", "model", "effort", "max_budget_usd", "timeout_s",
             "permission_mode", "started_at", "duration_s", "exit_code", "timed_out",
             "interrupted", "is_error", "cost_usd", "usage", "session_id", "verdict_ok",
             "verdict", "charged_usd", "result_tail", "source", "status", "verdict_status",
             "transient", "runner_started_at"}


@pytest.fixture
def merged(tmp_path, monkeypatch):
    """An e2e campaign (implementer + conformance reviewer) driven to MERGED."""
    monkeypatch.setenv("COLUMNS", "250")
    # COLUMNS is read once, at import. Rich ignores a lone `_width` on a dumb terminal
    # (both dimensions must be set), which is what pytest's capture is.
    monkeypatch.setattr("nightwright.console.console._width", 250)
    monkeypatch.setattr("nightwright.console.console._height", 50)
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(), review()]))
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle", "--json-lines"])
    assert r.exit_code == 0, r.output
    return cfg, cid, monkeypatch


def cli_json(cfg, monkeypatch, *args):
    r = run_cli(cfg, monkeypatch, ["campaign", *args, "--json"])
    assert r.exit_code == 0, r.output
    return json.loads(r.output)


def runs_dir(cfg, cid) -> Path:
    return S.runs_dir(cfg, cid)


def add_run(cfg, cid, run_id, *, start=True, end=None):
    """Append crafted run.start / run.end events for `run_id`."""
    if start:
        S.append_event(cfg, cid, "run.start", SC.with_defaults(
            "run.start", {"persona": "implementer", "run_id": run_id, "model": "m",
                          "effort": "high"}))
    if end is not None:
        S.append_event(cfg, cid, "run.end", SC.with_defaults(
            "run.end", {"persona": "implementer", "run_id": run_id, **end}))


def by_id(cfg, cid):
    return {r["run_id"]: r for r in RN.campaign_runs(cfg, cid)}


def test_runs_json_lists_e2e_runs(merged):
    cfg, cid, mp = merged
    d = cli_json(cfg, mp, "runs", cid)
    assert d["schema"] == "nw.campaign.runs.v1" and d["campaign"] == cid
    rows = d["runs"]
    assert [r["run_id"] for r in rows] == ["001-implementer", "002-conformance_reviewer"]
    for r in rows:
        assert set(r) == LIST_KEYS
        assert r["source"] == "ledger+run.json" and r["status"] == "done"
        assert r["transient"] is False and r["campaign"] == cid
        assert r["started_at"] and r["runner_started_at"]
    assert [r["verdict_status"] for r in rows] == ["done", "pass"]
    assert rows[0]["persona"] == "implementer" and rows[0]["cost_usd"] == 0.5
    assert rows[0]["charged_usd"] == 0.5 and rows[0]["model"]


def test_same_without_run_json(merged):
    cfg, cid, mp = merged
    with_rj = cli_json(cfg, mp, "runs", cid)["runs"]
    for p in runs_dir(cfg, cid).glob("*/run.json"):
        p.unlink()
    without = cli_json(cfg, mp, "runs", cid)["runs"]
    assert len(with_rj) == len(without) == 2
    for a, b in zip(with_rj, without):
        assert b["source"] == "ledger" and b["runner_started_at"] is None
        skip = {"source", "runner_started_at"}
        assert {k: v for k, v in a.items() if k not in skip} == \
            {k: v for k, v in b.items() if k not in skip}


def test_listing_cases(merged):
    cfg, cid, mp = merged
    add_run(cfg, cid, "010-implementer")                              # lone run.start
    orphan = runs_dir(cfg, cid) / "011-implementer"                   # prompt.md only
    orphan.mkdir()
    (orphan / "prompt.md").write_text("p")
    (runs_dir(cfg, cid) / ".fake-cursor").write_text("3")             # ignored
    rj_only = runs_dir(cfg, cid) / "012-implementer"                  # run.json, no ledger
    rj_only.mkdir()
    (rj_only / "run.json").write_text(json.dumps({
        "schema": SC.RUN_SCHEMA, "run_id": "012-implementer", "persona": "implementer",
        "started_at": "2026-10-25T01:00:00+00:00", "exit_code": 0, "is_error": False,
        "verdict": {"status": "done"}, "verdict_ok": True}))
    shutil.rmtree(runs_dir(cfg, cid) / "002-conformance_reviewer")    # dir gone, ledger stays
    rows = by_id(cfg, cid)
    assert list(rows) == ["001-implementer", "002-conformance_reviewer", "010-implementer",
                          "011-implementer", "012-implementer"]
    assert rows["010-implementer"]["status"] == "running"
    assert rows["010-implementer"]["source"] == "ledger"
    assert rows["011-implementer"]["status"] == "orphan"
    assert rows["011-implementer"]["source"] == "files"
    assert rows["011-implementer"]["persona"] is None
    r12 = rows["012-implementer"]
    assert r12["source"] == "run.json" and r12["status"] == "done"
    assert r12["persona"] == "implementer" and r12["started_at"] == r12["runner_started_at"]
    assert r12["verdict_status"] == "done" and r12["charged_usd"] is None
    assert rows["002-conformance_reviewer"]["source"] == "ledger"
    assert rows["002-conformance_reviewer"]["verdict_status"] == "pass"
    gone = cli_json(cfg, mp, "run", cid, "002")["run"]
    assert gone["paths"] == {}


@pytest.mark.parametrize("end,expected", [
    ({"is_error": True, "result_tail": "API Error: 429 rate limit"}, True),
    ({"is_error": True, "result_tail": "syntax error in foo"}, False),
    ({"timed_out": True, "result_tail": ""}, False),
    ({"timed_out": True, "is_error": True, "result_tail": "overloaded_error"}, True),
    ({"is_error": False, "timed_out": False, "result_tail": "429"}, False),
])
def test_transient_matches_run_failed(merged, end, expected):
    cfg, cid, _ = merged
    add_run(cfg, cid, "020-implementer", end=end)
    row = by_id(cfg, cid)["020-implementer"]
    assert row["transient"] is expected
    ev = S.read_ledger(cfg, cid, types=["run.end"])[-1]
    projected = EV._project_ledger_event(ev, {}, {"cost_warning_ratio": 1.0}, True)
    if end.get("is_error") or end.get("timed_out"):
        assert projected[0] == "run_failed" and projected[1]["transient"] is row["transient"]
    else:
        assert projected is None


@pytest.mark.parametrize("verdict,ok,expected", [
    (None, True, "—"),
    ("a string", True, "invalid"),
    ({"status": "done"}, False, "invalid"),
    ({"disposition": "fix"}, True, "fix"),
    ({"verdict": "pass"}, None, "pass"),
    ({"status": "blocked", "verdict": "pass"}, True, "blocked"),
    ({}, True, "?"),
])
def test_verdict_status(merged, verdict, ok, expected):
    cfg, cid, _ = merged
    add_run(cfg, cid, "030-implementer", end={"verdict": verdict, "verdict_ok": ok})
    assert by_id(cfg, cid)["030-implementer"]["verdict_status"] == expected
    assert RN.verdict_status(verdict, ok) == expected


def test_charged_usd_in_human_view(merged):
    cfg, cid, mp = merged
    add_run(cfg, cid, "040-implementer", end={"cost_usd": None, "charged_usd": 4.0})
    row = by_id(cfg, cid)["040-implementer"]
    assert row["charged_usd"] == 4.0 and row["cost_usd"] is None
    r = run_cli(cfg, mp, ["campaign", "runs", cid])
    assert r.exit_code == 0, r.output
    assert "$4.00*" in r.output and "$0.50" in r.output
    for rid in ("001-implementer", "002-conformance_reviewer", "040-implementer"):
        assert rid in r.output
    assert "done" in r.output and "pass" in r.output


def test_run_detail(merged):
    cfg, cid, mp = merged
    d = runs_dir(cfg, cid) / "001-implementer"
    (d / "stderr.log").write_text("".join(f"line {i}\n" for i in range(100)))
    (d / "result.json").write_text(json.dumps(
        {"permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}]}))
    full = cli_json(cfg, mp, "run", cid, "001-implementer")
    prefix = cli_json(cfg, mp, "run", cid, "001")
    assert full == prefix
    assert set(full) == {"schema", "campaign", "run"}
    assert full["schema"] == "nw.campaign.run.v1" and full["campaign"] == cid
    run = full["run"]
    assert run["run_id"] == "001-implementer"
    assert run["stderr_tail"] == [f"line {i}" for i in range(60, 100)]
    assert run["permission_denials"][0]["tool_name"] == "Bash"
    assert set(run["paths"]) == {"dir", "prompt", "raw", "stderr", "run_json"}
    assert all(Path(p).is_absolute() and Path(p).exists() for p in run["paths"].values())
    assert run["must_fix"] == [] and run["blockers"] == []
    assert LIST_KEYS <= set(run)
    # empty / unparseable raw results and a missing stderr.log
    (d / "result.json").write_text("")
    (d / "stderr.log").unlink()
    run = cli_json(cfg, mp, "run", cid, "001")["run"]
    assert run["permission_denials"] == [] and run["stderr_tail"] == []
    assert "stderr" not in run["paths"]
    (d / "result.json").write_text("{not json")
    assert cli_json(cfg, mp, "run", cid, "001")["run"]["permission_denials"] == []
    (d / "result.json").unlink()
    run = cli_json(cfg, mp, "run", cid, "001")["run"]
    assert run["permission_denials"] == [] and "raw" not in run["paths"]


def test_run_detail_must_fix_and_blockers(merged):
    cfg, cid, mp = merged
    add_run(cfg, cid, "050-implementer", end={
        "verdict": {"status": "blocked", "summary": "stuck", "must_fix": [{"title": "t"}],
                    "blockers": ["no network"]}, "verdict_ok": True})
    run = cli_json(cfg, mp, "run", cid, "050")["run"]
    assert run["must_fix"] == [{"title": "t"}] and run["blockers"] == ["no network"]
    r = run_cli(cfg, mp, ["campaign", "run", cid, "050"])
    assert r.exit_code == 0, r.output
    assert "050-implementer" in r.output and "blocked" in r.output
    assert "stuck" in r.output and "no network" in r.output


def test_errors(merged):
    cfg, cid, mp = merged
    r = run_cli(cfg, mp, ["campaign", "runs", "SPEC-9999-nope"])
    assert r.exit_code != 0 and "no campaign" in r.output
    r = run_cli(cfg, mp, ["campaign", "run", "SPEC-9999-nope", "001"])
    assert r.exit_code != 0 and "no campaign" in r.output
    r = run_cli(cfg, mp, ["campaign", "run", cid, "009"])
    assert r.exit_code != 0 and "no run '009'" in r.output and "001-implementer" in r.output
    add_run(cfg, cid, "001-extra")
    r = run_cli(cfg, mp, ["campaign", "run", cid, "001"])
    assert r.exit_code != 0 and "ambiguous" in r.output
    assert "001-implementer" in r.output and "001-extra" in r.output
    r = run_cli(cfg, mp, ["campaign", "run", cid, "001-implementer"])      # exact beats prefix
    assert r.exit_code == 0, r.output


def test_human_views_render(merged):
    cfg, cid, mp = merged
    r = run_cli(cfg, mp, ["campaign", "runs", cid])
    assert r.exit_code == 0, r.output
    assert "001-implementer" in r.output and "002-conformance_reviewer" in r.output
    r = run_cli(cfg, mp, ["campaign", "run", cid, "002"])
    assert r.exit_code == 0, r.output
    assert "002-conformance_reviewer" in r.output and "pass" in r.output


def test_empty_campaign_prints_no_runs(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["campaign", "runs", cid])
    assert r.exit_code == 0 and "no runs yet" in r.output
    assert cli_json(cfg, monkeypatch, "runs", cid)["runs"] == []


def test_conduct_skill_names_runs():
    skill = Path(__file__).resolve().parent.parent / "skills" / "nw-conduct" / "SKILL.md"
    assert "nw campaign runs" in skill.read_text(encoding="utf-8")
