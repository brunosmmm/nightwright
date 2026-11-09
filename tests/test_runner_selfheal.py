"""SPEC-0484: one campaign's apply error parks only that campaign; a backend fault (a missing
credential or SDK) holds the step instead of being read as a verdict."""
from __future__ import annotations

import io
import threading
from concurrent.futures import Future
from types import SimpleNamespace

from test_agentic_e2e import added, impl_done, make_cfg, make_repo, review, run_cli, script
from nightwright.agentic import backend_preflight as BP
from nightwright.agentic import escalations as ESC
from nightwright.agentic import events as E
from nightwright.agentic import persona_gate as PG
from nightwright.agentic import queue as Q
from nightwright.agentic import runner as R
from nightwright.agentic import runproc as RP
from nightwright.agentic import store as S
import pytest

pytestmark = pytest.mark.fast


def journal(cfg, kind):
    return [r for r in Q.read_journal(cfg) if r.get("kind") == kind]


def _raise_on(kind):
    real = Q._apply_step

    def apply_step(ctx, st, res):
        if res.step.kind == kind:
            raise KeyError("sha")
        return real(ctx, st, res)
    return apply_step


# ---- part 1: containment ---------------------------------------------------------------------

def test_apply_error_parks_only_that_campaign_and_closes_its_run(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(), review()]))
    cid = added(cfg, monkeypatch)
    monkeypatch.setattr(Q, "_apply_step", _raise_on("implement"))
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "apply_error", st
    assert "KeyError" in st["park"]["detail"] and "implement" in st["park"]["detail"]
    assert st["park"]["resume_to"] == "IMPLEMENTING"
    assert st["park"]["apply_error"]["action"] == "implement"
    assert Q.park_owner("apply_error") == "conductor"
    rec, = journal(cfg, "apply_error")
    assert rec["campaign"] == cid and "KeyError" in rec["error"] and rec["traceback"]
    assert not journal(cfg, "crash")
    assert Q._open_runs(cfg, cid) == []          # nothing for a restart to re-adopt
    ends = S.read_ledger(cfg, cid, types=["run.end"])
    assert [e["run_id"] for e in ends] == rec["run_ids"]
    parked = [e for e in E.sync(cfg) if e["type"] == "parked" and e.get("campaign") == cid]
    assert parked and parked[-1]["data"]["reason"] == "apply_error"
    # a later runner leaves it parked and does not crash
    n = len(S.read_ledger(cfg, cid))
    Q.run(cfg, max_ticks=2, poll_interval_s=0.01)
    assert S.read_state(cfg, cid)["state"] == "PARKED" and len(S.read_ledger(cfg, cid)) == n
    assert not journal(cfg, "crash")


def test_contain_leaves_a_parked_campaign_and_never_raises(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(), review()]))
    cid = added(cfg, monkeypatch)
    st = S.read_state(cfg, cid)
    Q._park(cfg, st, "human", "by hand", "QUEUED")
    ctx = SimpleNamespace(cfg=cfg)
    res = Q.StepResult(cid, Q.Step("implement"), "IMPLEMENTING")
    row = Q._contain_apply_error(ctx, st, res, KeyError("sha"))
    after = S.read_state(cfg, cid)
    assert row["to"] == "PARKED" and after["park"]["reason"] == "human"
    note = S.read_ledger(cfg, cid, types=["note"])[-1]
    assert "KeyError" in note["text"] and "stays PARKED" in note["text"]
    row = Q._contain_apply_error(ctx, {"id": "nope"}, Q.StepResult("nope", Q.Step("x"), "X"),
                                 ValueError("boom"))
    assert row["reason"] == "apply_error_uncontained"
    assert journal(cfg, "apply_error_uncontained")[0]["campaign"] == "nope"


def test_harvest_contains_finish_reviews_error(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(), review()]))
    bad, good = added(cfg, monkeypatch), "good"
    st = S.read_state(cfg, bad)
    st["state"] = "REVIEWING"
    S.write_state(cfg, bad, st)
    monkeypatch.setattr(Q, "_finish_reviews",
                        lambda ctx, rep, group: (_ for _ in ()).throw(RuntimeError("fan-out")))
    applied = []
    monkeypatch.setattr(Q, "_apply", lambda ctx, s, res: applied.append(s["id"]) or {"campaign": s["id"]})
    pool, report = Q.StepPool(), Q.TickReport()
    ctx = SimpleNamespace(cfg=cfg)
    fl = Q._Flight(st, ctx, group=Q._ReviewGroup(st, Q.Step("review", personas=("conformance_reviewer",))))
    fut = Future()
    fut.set_result(Q.StepResult(bad, Q.Step("review"), "REVIEWING"))
    fl.tasks.append((Q.Step("review", persona="conformance_reviewer"), fut, None))
    pool.flights[bad] = fl
    fut2 = Future()
    fut2.set_result(Q.StepResult(good, Q.Step("implement"), "IMPLEMENTING"))
    fl2 = Q._Flight({"id": good, "state": "IMPLEMENTING"}, ctx)
    fl2.tasks.append((Q.Step("implement"), fut2, None))
    pool.flights[good] = fl2
    Q._harvest(pool, report)                       # a normal tick: no guard
    assert applied == [good]
    assert S.read_state(cfg, bad)["park"]["reason"] == "apply_error"
    assert any("apply_error" in e for e in report.errors)


# ---- part 3: backend preflight -----------------------------------------------------------------

def _no_key(monkeypatch):
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.delenv("NW_AGENTIC_RUNNER", raising=False)
    monkeypatch.setattr(RP, "probe_cursor_sdk", lambda environ=None: (
        {"ok": True, "error": None} if (environ or __import__("os").environ).get("CURSOR_API_KEY")
        else {"ok": False, "error": "CURSOR_API_KEY is not set"}))


def _cursor_cfg(tmp_path):
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {"runner": "cursor"}}
    return cfg


def test_gate_holds_backend_unavailable_and_escalates_once(tmp_path, monkeypatch):
    _no_key(monkeypatch)
    cfg = _cursor_cfg(tmp_path)
    st = {"id": "c1", "state": "GATING"}
    g = PG.Gate(cfg, ["c1"])
    assert g.admit(st, Q.Step("triage", persona="gate_triager")) is None
    assert g.holds["c1"]["held_by"] == "backend_unavailable"
    g.flush()
    assert PG.held_by(cfg, st) == "backend_unavailable"
    PG.Gate(cfg, ["c1"]).admit(st, Q.Step("triage", persona="gate_triager"))
    open_ = [r for r in ESC.list_all(cfg, "open") if r.get("kind") == "backend_unavailable"]
    assert len(open_) == 1 and "CURSOR_API_KEY is not set" in open_[0]["question"]
    assert len(journal(cfg, "backend_preflight_failed")) == 1
    monkeypatch.setenv("CURSOR_API_KEY", "k-test-value")
    step = Q.Step("triage", persona="gate_triager")
    assert PG.Gate(cfg, ["c1"]).admit(st, step) is step


def test_preflight_held_runner_is_interrupted_and_uncharged(tmp_path, monkeypatch):
    _no_key(monkeypatch)
    cfg = _cursor_cfg(tmp_path)
    rd = tmp_path / "c1" / "runs" / "001-gate_triager"
    rd.mkdir(parents=True)
    req = R.RunRequest(persona="gate_triager", prompt="p", system_append="", cwd=tmp_path,
                       model="m", effort=None, max_budget_usd=1.0, timeout_s=10,
                       permission_mode="default", read_only=True, allowed_tools=(), add_dirs=(),
                       json_schema={}, env={}, run_dir=rd)
    monkeypatch.setattr(R, "_save_inputs", lambda req, text=None: None)
    held = Q._preflight_hold(SimpleNamespace(cfg=cfg), {"id": "c1"}, req, R.CursorRunner())
    assert isinstance(held, R.PreflightHeldRunner)
    rr = held.run(req)
    assert rr.interrupted and rr.cost_usd == 0.0 and "preflight" in rr.result_text
    # an adopted run dir (a launch file exists) is never replaced
    (rd / "launch.json").write_text("{}")
    cur = R.CursorRunner()
    assert Q._preflight_hold(SimpleNamespace(cfg=cfg), {"id": "c1"}, req, cur) is cur
    # a claude runner is never held
    cl = R.ClaudeRunner()
    assert Q._preflight_hold(SimpleNamespace(cfg=cfg), {"id": "c1"}, req, cl) is cl


def test_do_persona_turns_cursor_startup_fault_into_interrupted(tmp_path, monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "k")             # this runner has it; the run did not
    cfg = {"data_dir": str(tmp_path / "d")}
    failed = R.RunResult(exit_code=1, timed_out=False, interrupted=False, duration_s=0.1,
                         is_error=True, cost_usd=None, result_text="CURSOR_API_KEY is not set")

    class Fake:
        detachable = False

        def run(self, req, hooks=None):
            return failed
    ctx = SimpleNamespace(cfg=cfg, runner=Fake(), stop=threading.Event())
    req = SimpleNamespace(run_dir=tmp_path / "r", persona="gate_triager", runner=None)
    res = Q._do_persona(ctx, {"id": "c1", "state": "TRIAGING"},
                        Q.Step("triage", persona="gate_triager"), "001-x", req, 1.0)
    assert res.interrupted and res.run_result.interrupted and res.run_result.cost_usd == 0.0
    assert [r for r in ESC.list_all(cfg, "open") if r.get("kind") == "backend_unavailable"]


def test_startup_check_warns_and_journals(tmp_path, monkeypatch):
    _no_key(monkeypatch)
    cfg = _cursor_cfg(tmp_path)
    err = io.StringIO()
    assert BP.startup_check(cfg, err=err) == "CURSOR_API_KEY is not set"
    assert err.getvalue().startswith("WARNING:")
    assert journal(cfg, "backend_preflight_failed")[0]["where"] == "start"
    assert BP.startup_check({"data_dir": str(tmp_path / "e")}, err=err) is None   # claude host
