"""SPEC-0224: a run's cost is the real one whenever its output carries it; otherwise the budget
is charged and marked as an estimate everywhere (run.end, cost event, campaign cost, run.json,
tick row, displays)."""
import json
import subprocess
from pathlib import Path

import pytest

from test_agentic_e2e import added, impl_done, make_cfg, make_repo, review, run_cli, script
from wt.agentic import queue as Q
from wt.agentic import runner as R
from wt.agentic import schemas as SC
from wt.agentic import store as S
from wt.agentic.runner import RunResult
from wt import render


def _res(cost, *, timed_out=False, interrupted=False, is_error=False, exit_code=0):
    return RunResult(exit_code=exit_code, timed_out=timed_out, interrupted=interrupted,
                     duration_s=0.1, is_error=is_error, cost_usd=cost, usage={}, session_id="s",
                     verdict=None, verdict_ok=False, result_text="", raw_path=None)


def _by_type(events):
    return {t: p for t, p in events}


def _finding(events):
    return [p for t, p in events if t == "finding"
            and p["title"].startswith("run cost unknown")]


def test_interrupted_run_without_cost_is_estimated():
    state = {}
    events = Q._record_run(state, "implementer", "001-implementer", _res(None, exit_code=-15),
                           4.0, interrupted=True)
    ev = _by_type(events)
    assert ev["run.end"]["cost_usd"] is None and ev["run.end"]["charged_usd"] == 4.0
    assert ev["run.end"]["cost_estimated"] is True and ev["run.end"]["interrupted"] is True
    assert ev["cost"]["estimated"] is True and ev["cost"]["delta_usd"] == 4.0
    assert state["cost"]["total_usd"] == 4.0 and state["cost"]["estimated_usd"] == 4.0
    (f,) = _finding(events)
    assert f["severity"] == "info" and "(interrupted)" in f["detail"] and "$4.00" in f["detail"]
    Q._record_run(state, "implementer", "002-implementer", _res(None), 1.5, interrupted=True)
    assert state["cost"]["estimated_usd"] == 5.5 and state["cost"]["total_usd"] == 5.5


@pytest.mark.parametrize("kw,reason", [({"timed_out": True, "exit_code": -1, "is_error": True},
                                         "timed out"),
                                        ({}, "no cost reported")])
def test_timeout_and_no_cost_reasons(kw, reason):
    state = {}
    events = Q._record_run(state, "implementer", "001-implementer", _res(None, **kw), 2.0)
    ev = _by_type(events)
    assert ev["run.end"]["cost_estimated"] is True and ev["cost"]["estimated"] is True
    assert state["cost"]["estimated_usd"] == 2.0
    (f,) = _finding(events)
    assert f["detail"] == f"$2.00 charged as an upper bound ({reason})"


@pytest.mark.parametrize("kw", [{"timed_out": True, "exit_code": -1, "is_error": True},
                                {"is_error": True, "exit_code": 1}, {}])
def test_reported_cost_is_charged_exactly(kw):
    state = {}
    events = Q._record_run(state, "implementer", "001-implementer", _res(1.23, **kw), 4.0,
                           interrupted=bool(kw.get("timed_out")))
    ev = _by_type(events)
    assert ev["run.end"]["charged_usd"] == 1.23 and ev["run.end"]["cost_estimated"] is False
    assert ev["cost"]["estimated"] is False and _finding(events) == []
    assert state["cost"]["total_usd"] == 1.23 and "estimated_usd" not in state["cost"]


def test_partial_stdout_cost_is_used_on_timeout(tmp_path, monkeypatch):
    out = json.dumps({"type": "result", "is_error": False, "total_cost_usd": 1.23,
                      "result": "", "usage": {}, "session_id": "s"})

    bindir = tmp_path / "bin"                 # SPEC-0225: the fake `claude`, killed at its timeout
    bindir.mkdir()
    (bindir / "claude").symlink_to(Path(__file__).parent / "fixtures" / "fake_claude.py")
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_CLAUDE_STDOUT", out)
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP_S", "30")
    run_dir = tmp_path / "campaign" / "runs" / "001-implementer"
    req = R.RunRequest(persona="implementer", model="m", effort=None, max_budget_usd=4.0,
                       timeout_s=1, permission_mode="acceptEdits", cwd=tmp_path,
                       prompt="p", system_append="", read_only=False, allowed_tools=(),
                       add_dirs=(), json_schema={}, env={}, run_dir=run_dir)
    res = R.ClaudeRunner().run(req)
    assert res.timed_out and res.cost_usd == 1.23
    state = {}
    events = Q._record_run(state, "implementer", "001-implementer", res, 4.0)
    assert _by_type(events)["run.end"]["cost_estimated"] is False
    assert state["cost"]["total_usd"] == 1.23 and _finding(events) == []
    rec = json.loads((run_dir / "run.json").read_text())
    assert rec["cost_usd"] == 1.23 and rec["cost_estimated"] is False


def test_run_json_and_tick_row_mark_estimate(tmp_path):
    run_dir = tmp_path / "campaign" / "runs" / "001-implementer"
    run_dir.mkdir(parents=True)
    req = R.RunRequest(persona="implementer", model="m", effort=None, max_budget_usd=4.0,
                       timeout_s=1, permission_mode="acceptEdits", cwd=tmp_path, prompt="p",
                       system_append="", read_only=False, allowed_tools=(), add_dirs=(),
                       json_schema={}, env={}, run_dir=run_dir)
    R.write_run_record(req, _res(None, timed_out=True), "t")
    assert json.loads((run_dir / "run.json").read_text())["cost_estimated"] is True
    row = {"campaign": "c", "from": "IMPLEMENTING", "to": "IMPLEMENTING", "action": "implement",
           "reason": "interrupted", "run_id": "001-implementer", "cost_usd": 4.0,
           "cost_estimated": True}
    text = Q.tick_row_text(row)
    assert "~$4.00" in text and "est." in text
    assert "~$4.00 est." in render.tick_row(row).plain
    plain = Q.tick_row_text({**row, "cost_estimated": False})
    assert "$4.00" in plain and "~$" not in plain and "est." not in plain
    assert "~" not in render.tick_row({**row, "cost_estimated": False}).plain


def test_queue_payload_estimated_usd_and_legacy(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [dict(impl_done(), cost_usd=None)]))
    cid = added(cfg, monkeypatch)
    st = S.read_state(cfg, cid)
    assert "estimated_usd" not in st["cost"]                      # legacy shape
    (item,) = Q.queue_payload(cfg)["items"]
    assert item["cost"] == {"estimated_usd": 0.0}
    st["cost"]["estimated_usd"] = 4.0
    S.write_state(cfg, cid, st)
    assert Q.queue_payload(cfg)["items"][0]["cost"] == {"estimated_usd": 4.0}


def test_legacy_ledger_without_estimate_keys_validates():
    for etype, payload in (("run.end", {}), ("cost", {})):
        rec = {**dict.fromkeys(SC.ENVELOPE, "x"), "schema": SC.LEDGER_SCHEMA, "seq": 1, "type": etype,
               **SC.with_defaults(etype, payload)}
        assert SC.ledger_problems(rec) == [], etype
        assert "cost_estimated" not in rec and "estimated" not in rec
    assert "cost_estimated" not in SC.EVENT_KEYS["run.end"]
    assert "estimated" not in SC.EVENT_KEYS["cost"]


def test_e2e_null_cost_reaches_the_displays(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [dict(impl_done(), cost_usd=None), review()]))
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle", "--json-lines"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    end = S.read_ledger(cfg, cid, types=["run.end"])[0]
    assert end["cost_usd"] is None and end["cost_estimated"] is True
    est = st["cost"]["estimated_usd"]
    assert est == end["charged_usd"] > 0
    assert st["cost"]["total_usd"] == round(est + 0.2, 4)
    rj = json.loads((S.runs_dir(cfg, cid) / "001-implementer" / "run.json").read_text())
    assert rj["cost_estimated"] is True
    rows = [json.loads(ln) for ln in r.output.splitlines() if ln.startswith("{")]
    assert any(s.get("cost_estimated") for row in rows for s in row.get("steps", []))
    show = run_cli(cfg, monkeypatch, ["campaign", "show", cid])
    assert show.exit_code == 0, show.output
    assert f"~${est:.2f} est." in show.output


def _rendered(renderable) -> str:
    from io import StringIO
    from rich.console import Console
    buf = StringIO()
    Console(file=buf, width=240, color_system=None).print(renderable)
    return buf.getvalue()


@pytest.mark.parametrize("cost,shown", [({"estimated_usd": 4.0}, True),
                                         ({"estimated_usd": 0.0}, False), (None, False)])
def test_queue_watch_shows_estimate_only_when_above_zero(tmp_path, monkeypatch, cost, shown):
    from wt.agentic import cli_queue
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    added(cfg, monkeypatch)
    (item,) = Q.queue_payload(cfg)["items"]
    item = {**item, "state": "IMPLEMENTING", "cost_usd": 4.2}
    if cost is None:
        item.pop("cost", None)
    else:
        item["cost"] = cost
    payload = {**Q.queue_payload(cfg), "items": [item]}
    table = _rendered(cli_queue._queue_table(payload))
    panel = _rendered(cli_queue._active_panel(item))
    assert "$4.20" in table and "$4.20" in panel
    assert ("~$4.00 est." in table) is shown
    assert ("~$4.00 est." in panel) is shown
    if not shown:
        assert "est." not in table and "est." not in panel


def test_campaign_show_has_no_estimate_mark_without_estimate(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(), review()]))
    cid = added(cfg, monkeypatch)
    run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle", "--json-lines"])
    assert "est." not in run_cli(cfg, monkeypatch, ["campaign", "show", cid]).output
    st = S.read_state(cfg, cid)
    st["cost"].pop("estimated_usd", None)                         # legacy state
    S.write_state(cfg, cid, st)
    assert "est." not in run_cli(cfg, monkeypatch, ["campaign", "show", cid]).output
