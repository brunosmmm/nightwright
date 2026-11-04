"""SPEC-0345: persona cap, rate-limit backoff, campaign affinity and cost by worker. Offline:
SPEC-0338's loopback workers (`remote_support.Remote`) and `FakeRunner`."""
import copy
import datetime as dt
import json
import os
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from nightwright.agentic import campaign_export as CE
from nightwright.agentic import events as EV
from nightwright.agentic import persona_gate as PG
from nightwright.agentic import queue as Q
from nightwright.agentic import remote_gates as RG
from nightwright.agentic import remote_jobs as RJ
from nightwright.agentic import remote_personas as RPS
from nightwright.agentic import runner as R
from nightwright.agentic import store as S
from nightwright.agentic import workers as WK

from remote_support import CID, LOCAL_OK, WRITER, _remote, _req, _world, _write_script
from remote_support import _short_runtime_dir  # noqa: F401  (autouse fixture)
from waits import join
from fanout_support import (A, B, PersonaRunner, b_entry, drive, e2e,  # noqa: F401
                            impl_done, review as review_entry, second_reviewer, starts)


# ---- helpers -----------------------------------------------------------------------------------

def _two_workers(w, script):
    """Registry `box`, `box2` (both the loopback, both taking gates and personas)."""
    _remote(w, script)
    recs = json.loads(WK.path(w.cfg).read_text())["workers"]
    recs.append(dict(recs[0], name="box2"))
    WK.path(w.cfg).write_text(json.dumps({"schema": WK.SCHEMA, "workers": recs}))


def _set_registry(w, name, **fields):
    doc = json.loads(WK.path(w.cfg).read_text())
    for r in doc["workers"]:
        if r["name"] == name:
            r.update(fields)
    WK.path(w.cfg).write_text(json.dumps(doc))


def _place(w, state=None, keep=False):
    """One persona dispatch; the worker it landed on (None: local). Releases the slot unless
    `keep`."""
    st = state if state is not None else S.read_state(w.cfg, CID)
    rr = RPS.dispatch(w.cfg, st, "implement", _req(w))
    if rr is None:
        return None, None
    if not keep:
        os.close(rr.slot)
    return rr.worker["name"], rr


def _affine(w, name="box2"):
    st = S.read_state(w.cfg, CID)
    st["remote_affinity"], st["remote_affinity_at"] = name, S.now_iso()
    S.write_state(w.cfg, CID, st)
    return st


@pytest.fixture
def two(tmp_path):
    w = _world(tmp_path)
    _two_workers(w, _write_script(tmp_path / "s.json", WRITER))
    return w


# ---- affinity ----------------------------------------------------------------------------------

def test_first_persona_run_sets_affinity(two):
    assert S.read_state(two.cfg, CID).get("remote_affinity") is None
    assert _place(two)[0] == "box"                           # registry order
    st = S.read_state(two.cfg, CID)
    assert st["remote_affinity"] == "box" and st["remote_affinity_at"]


def test_three_persona_runs_and_a_gate_run_land_on_the_affine_worker(two):
    _affine(two, "box2")
    for _ in range(3):
        assert _place(two)[0] == "box2"
    st = S.read_state(two.cfg, CID)
    pools = RG.pools_for(two.cfg, two.project, st, seq=1)
    assert [p[0] for p in pools][:2] == ["box2", "box"]
    assert S.read_state(two.cfg, CID)["remote_affinity"] == "box2"


def test_full_affine_worker_falls_through_without_clearing(two):
    _affine(two, "box2")
    name, held = _place(two, keep=True)                      # box2's one slot is now taken
    try:
        assert name == "box2"
        assert _place(two)[0] == "box"
        assert S.read_state(two.cfg, CID)["remote_affinity"] == "box2"
    finally:
        os.close(held.slot)
    assert _place(two)[0] == "box2"                          # free again: back to it


@pytest.mark.parametrize("why", ["disabled", "check_failed", "job_lost"])
def test_failing_affine_worker_is_skipped(two, why):
    st = _affine(two, "box2")
    if why == "disabled":
        _set_registry(two, "box2", enabled=False)
    elif why == "check_failed":
        _set_registry(two, "box2", check_ok=False)
    else:
        S.append_event(two.cfg, CID, "worker_job_lost", {"worker": "box2", "job_id": "j",
                                                        "attempt": 1, "reason": "unreachable"})
        assert PG.affinity(two.cfg, st) is None             # a loss clears the affinity
    assert _place(two)[0] == "box"
    if why != "disabled":                                    # a gate run does not prefer it
        pools = RG.pools_for(two.cfg, two.project, S.read_state(two.cfg, CID), seq=1)
        assert pools[0][0] == "box"


def test_failing_affine_worker_first_in_the_registry_is_demoted_for_gates(two):
    _affine(two, "box")                                      # registry order: box first
    _set_registry(two, "box", check_ok=False)
    pools = RG.pools_for(two.cfg, two.project, S.read_state(two.cfg, CID), seq=1)
    assert [p[0] for p in pools][:2] == ["box2", "box"]


def test_affine_worker_that_lost_a_job_of_the_campaign_is_demoted_for_gates(two):
    # the loss voids the affinity; the worker that lost the job still goes last for gate runs,
    # as persona dispatch skips it
    st = _affine(two, "box")                                 # registry order: box first
    S.append_event(two.cfg, CID, "worker_job_lost", {"worker": "box", "job_id": "j",
                                                    "attempt": 1, "reason": "unreachable"})
    assert PG.affinity(two.cfg, st) is None
    pools = RG.pools_for(two.cfg, two.project, S.read_state(two.cfg, CID), seq=1)
    assert [p[0] for p in pools][:2] == ["box2", "box"]
    assert _place(two)[0] == "box2"


def _ago(seconds):
    return PG._iso(PG._now_epoch(None) - seconds)


def test_a_loss_from_before_any_affinity_stops_failing_the_worker(two):
    # a gate job of the campaign was lost on box while no affinity was set; box is the only
    # persona worker. The loss keeps box failing for LOSS_FAILING_S, not for the campaign's life
    _set_registry(two, "box2", enabled=False)
    st = S.read_state(two.cfg, CID)
    assert st.get("remote_affinity") is None and st.get("remote_affinity_at") is None
    box = next(r for r in WK.load(two.cfg) if r["name"] == "box")
    S.append_event(two.cfg, CID, "worker_job_lost", {"worker": "box", "job_id": "j",
                                                    "attempt": 1, "reason": "deadline"})
    assert PG.failing(two.cfg, st, box)
    assert _place(two)[0] is None                           # runs locally while it is fresh
    assert PG.failing(two.cfg, st, box, now=PG._now_epoch(None) + PG.LOSS_FAILING_S + 5) \
        is False
    # the same loss, recorded LOSS_FAILING_S ago: box takes the campaign's persona runs again
    led = S._ledger_path(two.cfg, CID)
    lines = [json.loads(x) for x in led.read_text().splitlines()]
    for e in lines:
        if e["type"] == "worker_job_lost":
            e["ts"] = _ago(PG.LOSS_FAILING_S + 5)
    led.write_text("".join(json.dumps(e) + "\n" for e in lines))
    assert not PG.failing(two.cfg, st, box)
    assert _place(two)[0] == "box"
    assert S.read_state(two.cfg, CID)["remote_affinity"] == "box"


def test_a_loss_after_the_affinity_stops_failing_the_worker_after_the_window(two):
    # the affinity on box was cleared by a loss and box is the only persona worker: the loss
    # keeps box failing (runs go local) only for LOSS_FAILING_S
    st = _affine(two, "box")
    _set_registry(two, "box2", enabled=False)
    box = next(r for r in WK.load(two.cfg) if r["name"] == "box")
    S.append_event(two.cfg, CID, "worker_job_lost", {"worker": "box", "job_id": "j",
                                                    "attempt": 1, "reason": "unreachable"})
    assert PG.affinity(two.cfg, st) is None and PG.failing(two.cfg, st, box)
    later = PG._now_epoch(None) + PG.LOSS_FAILING_S + 5
    assert not PG.failing(two.cfg, st, box, now=later)
    assert PG.affinity(two.cfg, st) is None                 # still cleared: a new run sets it


def test_gate_run_never_sets_affinity(two):
    pools = RG.pools_for(two.cfg, two.project, S.read_state(two.cfg, CID), seq=1)
    assert pools[0][0] == "box"
    assert S.read_state(two.cfg, CID).get("remote_affinity") is None


def test_lost_persona_job_clears_affinity(tmp_path):
    w = _world(tmp_path)
    # the worker's FakeRunner raises (persona mismatch): the job is lost and runs locally
    _remote(w, _write_script(tmp_path / "s.json", [dict(WRITER[0], persona="reviewer")]))
    req = _req(w)
    st = S.read_state(w.cfg, CID)
    rr = RPS.dispatch(w.cfg, st, "implement", req,
                      local=R.FakeRunner(_write_script(tmp_path / "ok.json", LOCAL_OK)))
    assert S.read_state(w.cfg, CID)["remote_affinity"] == "box"
    rr.run(req)
    assert S.read_state(w.cfg, CID)["remote_affinity"] is None
    lost = S.read_ledger(w.cfg, CID, types=["worker_job_lost"])
    assert [e["worker"] for e in lost] == ["box"]
    assert lost[0]["run_id"] == "001-implementer"          # the cap restarts the run's clock


class _SpyLocal(R.FakeRunner):
    """The local runner, recording the run dirs it was asked to run."""

    def __init__(self, script):
        super().__init__(script)
        self.calls = []

    def run(self, req, **kw):
        self.calls.append(req.run_dir.name)
        return super().run(req, **kw)


def _backoff_on(cfg, seconds=900):
    PG._write_json(PG.rate_limit_path(cfg), {"until": PG._iso(PG._now_epoch(None) + seconds),
                                             "backoff_s": 60})


def _persona_step(w, req, local):
    """`_do_persona` for the implementer, after the `run.start` `_prepare_run` writes."""
    S.append_event(w.cfg, CID, "run.start", {
        "persona": req.persona, "run_id": req.run_dir.name, "model": req.model,
        "effort": None, "max_budget_usd": req.max_budget_usd, "timeout_s": req.timeout_s,
        "permission_mode": req.permission_mode, "state": "IMPLEMENTING",
        "prompt_sha256": "0" * 64, "step": {"kind": "implement", "personas": []}})
    st = S.read_state(w.cfg, CID)
    st["counters"]["runs"] = int(req.run_dir.name[:3])
    S.write_state(w.cfg, CID, st)
    ctx = Q._Ctx(w.cfg, runner=local)
    ctx._projects[w.state["project"]] = w.project
    return Q._do_persona(ctx, S.read_state(w.cfg, CID),
                         Q.Step("implement", persona="implementer"), req.run_dir.name, req, 1.0)


def _apply_held(w, local, res):
    """Apply a held step's result as the tick does; assert the held run charged nothing."""
    ctx = Q._Ctx(w.cfg, runner=local)
    ctx._projects[w.state["project"]] = w.project
    before = S.read_state(w.cfg, CID)
    Q._apply(ctx, copy.deepcopy(before), res)
    after = S.read_state(w.cfg, CID)
    assert after["cost"]["total_usd"] == before["cost"]["total_usd"]
    persona = res.step.persona
    assert ((after["cost"].get("by_persona") or {}).get(persona, 0.0)
            == (before["cost"].get("by_persona") or {}).get(persona, 0.0))
    end, = [e for e in S.read_ledger(w.cfg, CID, types=["run.end"])
            if e["run_id"] == res.run_id]
    assert end["charged_usd"] == 0.0 and end["cost_estimated"] is False
    assert PG.cost_by_worker(S.read_ledger(w.cfg, CID)).get("local", 0.0) == 0.0


def test_a_job_lost_during_the_backoff_is_not_rerun_locally(tmp_path, monkeypatch):
    # another campaign's 429 started the backoff after this job was placed; the worker then
    # loses it: the run does not go local, it ends interrupted and the gate holds the step
    w = _world(tmp_path)
    _remote(w, _write_script(tmp_path / "s.json", [dict(WRITER[0], persona="reviewer")]))
    local = _SpyLocal(_write_script(tmp_path / "ok.json", LOCAL_OK))
    real = RPS._follow

    def follow(*a, **kw):
        _backoff_on(w.cfg)
        return real(*a, **kw)
    monkeypatch.setattr(RPS, "_follow", follow)
    res = _persona_step(w, _req(w), local)
    assert local.calls == []
    assert res.interrupted and res.run_result.interrupted and res.commit_sha is None
    assert RJ.read(w.cfg, RPS.job_id_for(CID, "001-implementer"))["state"] == "lost"
    _apply_held(w, local, res)                      # SPEC-0345: a held run costs nothing


def test_a_job_lost_with_no_backoff_still_runs_locally(tmp_path):
    w = _world(tmp_path)
    _remote(w, _write_script(tmp_path / "s.json", [dict(WRITER[0], persona="reviewer")]))
    local = _SpyLocal(_write_script(tmp_path / "ok.json", LOCAL_OK))
    res = _persona_step(w, _req(w), local)
    assert local.calls == ["001-implementer"] and not res.interrupted


def test_a_remote_429_whose_write_back_failed_starts_the_backoff_and_holds_the_rerun(
        tmp_path, monkeypatch):
    # the remote run's own 429 is folded in before the re-run, so the re-run is held too
    w = _world(tmp_path)
    _remote(w, _write_script(tmp_path / "w.json", [dict(WRITER[0], api_error={
        "status": 429, "message": "API Error: 429 rate limit"})]))
    monkeypatch.setattr(RPS, "_before_writeback",
                        lambda wt, b: (w.repo / "late.txt").write_text("late\n"))
    local = _SpyLocal(_write_script(tmp_path / "ok.json", LOCAL_OK))
    res = _persona_step(w, _req(w), local)
    assert local.calls == []
    assert PG.read_rate_limit(w.cfg)["backoff_s"] == 60 and PG.backoff_until(w.cfg) is not None
    assert [e["campaign"] for e in _events(w.cfg)] == [CID]
    assert res.run_id == "002-implementer" and res.interrupted and res.run_result.interrupted
    assert res.commit_sha is None
    failed, = S.read_ledger(w.cfg, CID, types=["run.end"])
    assert failed["run_id"] == "001-implementer" and failed["writeback_failed"]
    _apply_held(w, local, res)                      # the held re-run costs nothing


def test_a_write_back_rerun_runs_locally_with_no_backoff(tmp_path, monkeypatch):
    w = _world(tmp_path)
    _remote(w, _write_script(tmp_path / "w.json", WRITER))
    monkeypatch.setattr(RPS, "_before_writeback",
                        lambda wt, b: (w.repo / "late.txt").write_text("late\n"))
    local = _SpyLocal(_write_script(tmp_path / "ok.json", LOCAL_OK))
    res = _persona_step(w, _req(w), local)
    assert local.calls == ["002-implementer"] and not res.interrupted


@pytest.mark.parametrize("workers", [True, False])
def test_a_backoff_begun_after_the_gate_holds_the_dispatch(tmp_path, workers):
    # the tick's gate admitted the step with no backoff; another run's 429 starts one before
    # this run's dispatch: it is placed neither on the worker nor locally
    w = _world(tmp_path)
    if workers:
        _remote(w, _write_script(tmp_path / "s.json", WRITER))
    local = _SpyLocal(_write_script(tmp_path / "ok.json", LOCAL_OK))
    _backoff_on(w.cfg)
    res = _persona_step(w, _req(w), local)
    assert local.calls == []
    assert res.interrupted and res.run_result.interrupted and res.commit_sha is None
    assert "backoff" in res.run_result.result_text
    assert RJ.read(w.cfg, RPS.job_id_for(CID, "001-implementer")) is None    # never submitted
    assert S.read_state(w.cfg, CID).get("remote_affinity") is None
    _apply_held(w, local, res)                      # the held run costs nothing
    # the campaign's spec review is not gated
    st = S.read_state(w.cfg, CID)
    rr = RPS.dispatch(w.cfg, st, "spec_review", _req(w, "002-spec_reviewer",
                                                     persona="spec_reviewer", read_only=True))
    assert not isinstance(rr, RPS.HeldRunner)
    if rr is not None:
        os.close(rr.slot)


def test_an_uncharged_loss_restarts_the_cap_clock_of_the_local_rerun(tmp_path, monkeypatch):
    # a rotation (uncharged) loses the job late in its run: the local re-run under the same run
    # id keeps its cap slot for its own full timeout, and the worker stays affine and healthy
    w = _world(tmp_path)
    _remote(w, _write_script(tmp_path / "s.json", WRITER))
    req = _req(w)                                                  # timeout_s 600
    t0 = PG._now_epoch(None) - 590
    ttl = RJ.settings(w.cfg)["worker_ttl_s"]
    real_now = S.now_iso
    monkeypatch.setattr(S, "now_iso", lambda: PG._iso(t0))
    S.append_event(w.cfg, CID, "run.start", {
        "persona": "implementer", "run_id": "001-implementer", "model": "m", "effort": None,
        "max_budget_usd": 1.0, "timeout_s": req.timeout_s, "permission_mode": "x",
        "prompt_sha256": "0"})
    monkeypatch.setattr(S, "now_iso", real_now)
    monkeypatch.setattr(RPS, "_follow", lambda rr, rq, hooks, rec: RPS._lost(
        rr, rq, hooks, rec, RG.UNCHARGED[0]))
    monkeypatch.setattr(RG, "_cancel_job", lambda *a: None)
    local = _SpyLocal(_write_script(tmp_path / "ok.json", LOCAL_OK))
    st = S.read_state(w.cfg, CID)
    rr = RPS.dispatch(w.cfg, st, "implement", req, local=local)
    rr.run(req)
    assert local.calls == ["001-implementer"]
    lost, = S.read_ledger(w.cfg, CID, types=["worker_job_lost"])
    assert lost["run_id"] == "001-implementer" and lost["charged"] is False
    loss_t = PG._epoch(lost["ts"])
    late = t0 + req.timeout_s + ttl + 5                    # past the remote run's own deadline
    assert late < loss_t + req.timeout_s + ttl
    assert PG.live_persona_runs(w.cfg, [CID], late) == 1
    assert PG.live_persona_runs(w.cfg, [CID], loss_t + req.timeout_s + ttl + 5) == 0
    # not the worker's fault: no failing worker, the affinity stays
    st = S.read_state(w.cfg, CID)
    assert st["remote_affinity"] == "box" and PG.affinity(w.cfg, st) == "box"
    assert not PG.failing(w.cfg, st, WK.load(w.cfg)[0])


def test_workers_check_records_its_outcome(tmp_path, monkeypatch):
    w = _world(tmp_path)
    _remote(w, _write_script(tmp_path / "s.json", WRITER))
    monkeypatch.setattr(WK, "_check", lambda cfg, wk, out: out["errors"].append("down"))
    assert WK.check(w.cfg, WK.load(w.cfg)[0], record=True)["ok"] is False
    rec = WK.load(w.cfg)[0]
    assert rec["check_ok"] is False and rec["checked_at"]
    assert PG.failing(w.cfg, S.read_state(w.cfg, CID), rec)


def test_an_automatic_check_failure_does_not_make_the_worker_failing(tmp_path, monkeypatch):
    # an update's preflight / post-update probe (no `record`) that fails stores nothing; one
    # that passes clears an earlier `nw workers check` failure
    w = _world(tmp_path)
    _remote(w, _write_script(tmp_path / "s.json", WRITER))
    monkeypatch.setattr(WK, "_check", lambda cfg, wk, out: out["errors"].append("ssh timeout"))
    assert WK.check(w.cfg, WK.load(w.cfg)[0])["ok"] is False
    rec = WK.load(w.cfg)[0]
    assert "check_ok" not in rec
    assert not PG.failing(w.cfg, S.read_state(w.cfg, CID), rec)
    WK.check(w.cfg, rec, record=True)
    assert WK.load(w.cfg)[0]["check_ok"] is False
    monkeypatch.setattr(WK, "_check", lambda cfg, wk, out: None)
    assert WK.check(w.cfg, WK.load(w.cfg)[0])["ok"] is True
    rec = WK.load(w.cfg)[0]
    assert rec["check_ok"] is True
    assert not PG.failing(w.cfg, S.read_state(w.cfg, CID), rec)


def test_an_automatic_pass_keeps_a_claude_check_failure(tmp_path, monkeypatch):
    # `nw workers check` fails on claude: an automatic probe that passes (it never checks claude)
    # leaves the worker failing; only the next `nw workers check` that passes clears it
    from nightwright.cli import cli
    w = _world(tmp_path)
    _remote(w, _write_script(tmp_path / "s.json", WRITER))
    monkeypatch.setattr("nightwright.cli.load_config", lambda: w.cfg)
    monkeypatch.setattr(WK, "_check", lambda cfg, wk, out: None)
    monkeypatch.setattr(RPS, "claude_check", lambda cfg, wk, auth: ["claude: not logged in"])
    run = CliRunner().invoke(cli, ["workers", "check", "box", "--json"])
    assert run.exit_code == 1, run.output
    rec = WK.load(w.cfg)[0]
    assert rec["check_ok"] is False and rec["check_failed"] == "claude"
    assert PG.failing(w.cfg, S.read_state(w.cfg, CID), rec)
    assert WK.check(w.cfg, rec)["ok"] is True                 # e.g. an update's preflight
    rec = WK.load(w.cfg)[0]
    assert rec["check_ok"] is False and rec["check_failed"] == "claude"
    assert PG.failing(w.cfg, S.read_state(w.cfg, CID), rec)
    monkeypatch.setattr(RPS, "claude_check", lambda cfg, wk, auth: [])
    run = CliRunner().invoke(cli, ["workers", "check", "box", "--json"])
    assert run.exit_code == 0, run.output
    rec = WK.load(w.cfg)[0]
    assert rec["check_ok"] is True and "check_failed" not in rec
    assert not PG.failing(w.cfg, S.read_state(w.cfg, CID), rec)
    # an SSH failure recorded by `nw workers check` is cleared by an automatic pass
    monkeypatch.setattr(WK, "_check", lambda cfg, wk, out: out["errors"].append("down"))
    CliRunner().invoke(cli, ["workers", "check", "box", "--json"])
    assert WK.load(w.cfg)[0]["check_failed"] == "probe"
    monkeypatch.setattr(WK, "_check", lambda cfg, wk, out: None)
    WK.check(w.cfg, WK.load(w.cfg)[0])
    assert WK.load(w.cfg)[0]["check_ok"] is True


def test_set_affinity_leaves_the_snapshot_when_the_store_write_fails(tmp_path, monkeypatch):
    w = _world(tmp_path)
    st = S.read_state(w.cfg, CID)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(S, "write_state", boom)
    assert PG.set_affinity(w.cfg, st, "box") is False
    assert st.get("remote_affinity") is None


def test_set_affinity_keeps_an_affinity_stored_from_another_snapshot(tmp_path):
    # two dispatches of one campaign from separate snapshots both see a null affinity: the
    # first writer wins, the second takes its value over and does not move `remote_affinity_at`
    w = _world(tmp_path)
    a, b = S.read_state(w.cfg, CID), S.read_state(w.cfg, CID)
    assert PG.set_affinity(w.cfg, a, "box") is True
    stored = S.read_state(w.cfg, CID)
    assert PG.set_affinity(w.cfg, b, "box2") is False
    after = S.read_state(w.cfg, CID)
    assert after["remote_affinity"] == "box"
    assert after["remote_affinity_at"] == stored["remote_affinity_at"]
    assert (b["remote_affinity"], b["remote_affinity_at"]) == ("box", stored["remote_affinity_at"])
    # a stored affinity voided by a loss is replaced
    S.append_event(w.cfg, CID, "worker_job_lost", {"worker": "box", "job_id": "j",
                                                  "attempt": 1, "reason": "unreachable"})
    c = S.read_state(w.cfg, CID)
    c["remote_affinity"] = None
    assert PG.set_affinity(w.cfg, c, "box2") is True
    assert S.read_state(w.cfg, CID)["remote_affinity"] == "box2"


# ---- the dispatch gate: global cap -------------------------------------------------------------

def _open_run(cfg, cid, run_id, persona="implementer"):
    S.append_event(cfg, cid, "run.start", {
        "persona": persona, "run_id": run_id, "model": "m", "effort": None,
        "max_budget_usd": 1.0, "timeout_s": 60, "permission_mode": "x", "prompt_sha256": "0"})


def _cap(cfg, n):
    cfg.setdefault("agentic", {}).setdefault("workers", {})["max_personas"] = n


def test_cap_counts_local_and_remote_runs_and_trims_a_fan_out(tmp_path):
    w = _world(tmp_path)
    _open_run(w.cfg, CID, "001-implementer")                              # a local live run
    RJ.create(w.cfg, job_id="j-1", campaign="other", worker="box", attempt=1, seq=0,
              submitted_at=0.0, kind="persona", deadline_at=9e9)          # a remote live run
    RJ.update(w.cfg, "j-1", run_id="001-reviewer", persona="reviewer")
    _open_run(w.cfg, CID, "002-spec_reviewer", persona="spec_reviewer")   # not counted
    assert PG.live_persona_runs(w.cfg, [CID]) == 2
    st = S.read_state(w.cfg, CID)
    step = Q.Step("review", persona="a", personas=("a", "b", "c"))
    _cap(w.cfg, 2)
    gate = PG.Gate(w.cfg, [CID])
    assert gate.admit(st, step) is None                                   # 2 live, cap 2
    assert gate.holds[CID]["held_by"] == "persona_cap"
    _cap(w.cfg, 3)
    gate = PG.Gate(w.cfg, [CID])
    got = gate.admit(st, step)
    assert got.personas == ("a",) and got.held == ("b", "c")
    assert gate.admit(st, Q.Step("implement", persona="implementer")) is None   # room used
    gate.flush()
    assert PG.read_holds(w.cfg)[CID]["held_by"] == "persona_cap"


def test_a_run_or_job_record_past_its_deadline_frees_its_cap_slot(tmp_path):
    # the coordinator crashed mid persona job: its `run.start` never gets a `run.end` and the
    # job record stays `detached` on a worker since unregistered. Both count until the run's
    # deadline, then free the slot, so the cap cannot stay full forever
    w = _world(tmp_path)
    _cap(w.cfg, 1)
    S.append_event(w.cfg, CID, "run.start", {
        "persona": "implementer", "run_id": "001-implementer", "model": "m", "effort": None,
        "max_budget_usd": 1.0, "timeout_s": 600, "permission_mode": "x", "prompt_sha256": "0"})
    now = PG._now_epoch(None)
    RJ.create(w.cfg, job_id="j-1", campaign="gone", worker="old", attempt=1, seq=0,
              submitted_at=now, kind="persona", deadline_at=now + 3600)
    RJ.update(w.cfg, "j-1", run_id="001-reviewer", persona="reviewer", state="detached")
    ttl = RJ.settings(w.cfg)["worker_ttl_s"]
    assert PG.live_persona_runs(w.cfg, [CID], now) == 2
    assert PG.live_persona_runs(w.cfg, [CID], now + 600 + ttl + 5) == 1    # the record only
    assert PG.live_persona_runs(w.cfg, [CID], now + 3605) == 0
    st = S.read_state(w.cfg, CID)
    step = Q.Step("implement", persona="implementer")
    assert PG.Gate(w.cfg, [CID], now=now).admit(st, step) is None
    assert PG.Gate(w.cfg, [CID], now=now + 3605).admit(st, step) is step
    # a record with no deadline counts until submitted_at + the longest job timeout + the TTL
    rec = RJ.read(w.cfg, "j-1")
    rec.pop("deadline_at")
    RJ._write(w.cfg, rec)
    late = now + RG.MAX_JOB_TIMEOUT_S + ttl
    assert PG.live_persona_runs(w.cfg, [], late - 5) == 1
    assert PG.live_persona_runs(w.cfg, [], late + 5) == 0


def test_a_local_rerun_after_a_late_loss_keeps_its_cap_slot(tmp_path, monkeypatch):
    # the persona job is lost just before its timeout and the run goes local under the same
    # run id with a full timeout: it counts past the original run.start + timeout + TTL
    w = _world(tmp_path)
    t0 = PG._now_epoch(None)
    ttl = RJ.settings(w.cfg)["worker_ttl_s"]
    monkeypatch.setattr(S, "now_iso", lambda: PG._iso(t0))
    _open_run(w.cfg, CID, "001-implementer")                              # timeout_s 60
    _open_run(w.cfg, CID, "002-reviewer")
    monkeypatch.setattr(S, "now_iso", lambda: PG._iso(t0 + 55))
    S.append_event(w.cfg, CID, "worker_job_lost", {
        "worker": "box", "job_id": "j-1", "run_id": "001-implementer", "attempt": 1,
        "reason": "deadline"}, by="personas")
    S.append_event(w.cfg, CID, "worker_job_lost", {                       # a gate job's loss
        "worker": "box", "job_id": "g-1", "attempt": 1, "reason": "deadline"})
    assert PG.live_persona_runs(w.cfg, [CID], t0 + 30) == 2
    assert PG.live_persona_runs(w.cfg, [CID], t0 + 60 + ttl + 5) == 1     # the re-run only
    assert PG.live_persona_runs(w.cfg, [CID], t0 + 55 + 60 + ttl + 5) == 0


def test_no_cap_by_default(tmp_path):
    w = _world(tmp_path)
    for i in range(5):
        _open_run(w.cfg, CID, f"00{i}-implementer")
    step = Q.Step("review", persona="a", personas=("a", "b", "c"))
    assert PG.max_personas(w.cfg) is None
    assert PG.Gate(w.cfg, [CID]).admit(S.read_state(w.cfg, CID), step) is step


@pytest.mark.parametrize("bad", [0, "2", True, -1, 1.5])
def test_invalid_max_personas_is_rejected(tmp_path, bad):
    cfg = {"agentic": {"workers": {"max_personas": bad}}}
    with pytest.raises(ValueError, match="max_personas"):
        Q.validate_runner_cfg(cfg)
    Q.validate_runner_cfg({"agentic": {"workers": {"max_personas": 2}}})
    Q.validate_runner_cfg({"agentic": {"workers": {"max_personas": None}}})


def test_capped_fan_out_runs_members_on_later_ticks(e2e):
    cfg, cid, repo, runner = e2e({"implementer": [impl_done()], A: [review_entry("pass")],
                                  B: [b_entry("pass")]})
    _cap(cfg, 1)
    reports = drive(cfg, runner)
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st
    assert starts(cfg, cid) == ["001-implementer", f"002-{A}", f"003-{B}"]
    # B started only after A's run ended: never two persona runs at once
    led = [(e["type"], e.get("run_id")) for e in S.read_ledger(cfg, cid, types=["run.start", "run.end"])]
    assert led.index(("run.end", f"002-{A}")) < led.index(("run.start", f"003-{B}"))
    notes = [e["text"] for e in S.read_ledger(cfg, cid, types=["note"])]
    assert any(t.startswith(f"persona cap: {B}") for t in notes)
    assert not (st["counters"].get("review_retries_by_persona") or {})   # waiting is no retry
    assert any(r.get("reason") == "persona_cap" for rep in reports for r in rep.steps)


def test_adopted_trimmed_fan_out_keeps_its_held_members(e2e):
    # after a restart the queue adopts the one member the cap let run; the reviewer it held is
    # still held (no retry charged), not a failed member
    cfg, cid, repo, runner = e2e({})
    ctx = Q._Ctx(cfg)
    st = S.read_state(cfg, cid)
    assert Q._adopted_held(ctx, st, (A,)) == ()                       # no cap: as before
    _cap(cfg, 1)
    assert Q._adopted_held(ctx, st, (A,)) == (B,)
    assert Q._adopted_held(ctx, st, (A, B)) == ()
    assert Q._adopted_held(ctx, st, ("review_triager",)) == ()
    loop = int((st.get("counters") or {}).get("loops", 0))
    st["last_review"] = {"verdicts": {B: {"verdict": b_entry("pass")["verdict"], "loop": loop}}}
    assert Q._adopted_held(ctx, st, (A,)) == ()                       # B already has a verdict


def test_no_workers_default_fan_out_runs_all_members_concurrently(e2e):
    barrier = threading.Barrier(2)
    cfg, cid, repo, runner = e2e({"implementer": [impl_done()], A: [review_entry("pass")],
                                  B: [b_entry("pass")]}, profile={"slots": 1}, barrier=barrier)
    drive(cfg, runner)
    assert S.read_state(cfg, cid)["state"] == "MERGED"      # both met at the barrier


def test_capped_fan_out_member_failing_twice_escalates(e2e):
    # room for 1 of 2 reviewers; A keeps failing: its second failure parks review_escalated,
    # as a full pass would, instead of re-running A every tick
    bad = dict(review_entry("pass"), malformed=True)
    cfg, cid, repo, runner = e2e({"implementer": [impl_done()], A: [bad, bad, bad],
                                  B: [b_entry("pass")]})
    _cap(cfg, 1)
    drive(cfg, runner)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "review_escalated", st
    assert "conformance reviewer" in st["park"]["detail"]
    assert runner.calls.count(A) == 2 and runner.calls.count(B) == 0


def test_capped_fan_out_member_failing_to_prepare_is_charged_and_escalates(e2e, monkeypatch):
    # room for 1 of 2 reviewers; A's request cannot be built (no run.start) on every attempt:
    # it is charged a retry like a failed run and parks review_escalated, instead of the
    # campaign looping in REVIEWING/persona_cap with B held forever
    cfg, cid, repo, runner = e2e({"implementer": [impl_done()], A: [review_entry("pass")],
                                  B: [b_entry("pass")]})
    _cap(cfg, 1)
    real = Q._prepare_run

    def prepare(ctx, state, persona, step=None):
        if persona == A:
            raise OSError("cannot build the request")
        return real(ctx, state, persona, step)
    monkeypatch.setattr(Q, "_prepare_run", prepare)
    drive(cfg, runner)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "review_escalated", st
    assert runner.calls.count(A) == 0 and runner.calls.count(B) == 0


def test_capped_fan_out_charges_a_retry_only_to_the_member_that_ran(e2e):
    # room for 1 of 2 reviewers: A runs without a valid verdict (one retry), B is held (none);
    # A's retry then passes, B runs on a later tick, and the review routes to a merge
    bad = dict(review_entry("pass"), malformed=True)
    cfg, cid, repo, runner = e2e({"implementer": [impl_done()], A: [bad, review_entry("pass")],
                                  B: [b_entry("pass")]})
    _cap(cfg, 1)
    for _ in range(12):
        rep = Q.tick(cfg, runner=runner)
        if any(r.get("reason") == "persona_cap" for r in rep.steps):
            break
    st = S.read_state(cfg, cid)
    assert st["state"] == "REVIEWING"
    assert st["counters"]["review_retries_by_persona"] == {A: 1}       # B: held, no retry
    assert runner.calls.count(B) == 0
    drive(cfg, runner)
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st
    assert runner.calls.count(A) == 2 and runner.calls.count(B) == 1
    assert starts(cfg, cid)[-2:] == [f"003-{A}", f"004-{B}"]


def _fill_cap(cfg):
    """`max_personas: 1`, its one slot held by another campaign's live remote persona job, and
    the rate-limit backoff running: every gated dispatch is closed."""
    _cap(cfg, 1)
    RJ.create(cfg, job_id="j-other", campaign="other", worker="box", attempt=1, seq=0,
              submitted_at=0.0, kind="persona", deadline_at=9e9)
    RJ.update(cfg, "j-other", run_id="001-implementer", persona="implementer")
    PG._write_json(PG.rate_limit_path(cfg), {"until": PG._iso(dt.datetime.now().timestamp() + 900),
                                             "backoff_s": 900})
    assert PG.backoff_until(cfg) is not None


def test_spec_review_is_neither_counted_nor_gated(tmp_path, monkeypatch):
    from fanout_support import _git, added, make_cfg, make_repo, script
    w = _world(tmp_path)
    _open_run(w.cfg, CID, "001-spec_reviewer", persona="spec_reviewer")
    assert PG.live_persona_runs(w.cfg, [CID]) == 0                # not counted
    # a live campaign spec-review run starts while the backoff runs and the cap is full
    repo = make_repo(tmp_path)
    v = {"verdict": "approve", "size": "s", "tone": "cost-effective", "rationale": "ok",
         "findings": [], "suggested_splits": []}
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [
        {"persona": "spec_reviewer", "verdict": v, "cost_usd": 0.1}]))
    cid = added(cfg, monkeypatch)
    runner = R.FakeRunner(cfg["agentic"]["fake_script"])
    Q.tick(cfg, runner=R.FakeRunner(script(tmp_path, [], name="none.json")))   # open only
    st = S.read_state(cfg, cid)
    assert st["state"] == "IMPLEMENTING" and not starts(cfg, cid)
    wt = Path(st["repo"]["worktree"])                     # an idea campaign's draft spec
    spec = wt / st["spec"]["rel_path"]
    spec.write_text(spec.read_text().replace("status: accepted", "status: draft"))
    _git(wt, "commit", "-qam", "draft")
    st.update(state="SPEC_REVIEW", kind="idea")
    st["spec"]["id"] = "SPEC-0001"
    S.write_state(cfg, cid, st)
    _fill_cap(cfg)
    rep = Q.tick(cfg, runner=runner)
    assert [r for r in starts(cfg, cid) if r.endswith("-spec_reviewer")], (rep.steps, S.read_state(cfg, cid).get("park"))
    assert cid not in PG.read_holds(cfg)
    # the same closed gate holds a campaign persona step
    st = S.read_state(cfg, cid)
    st["state"] = "IMPLEMENTING"
    S.write_state(cfg, cid, st)
    n = len(starts(cfg, cid))
    Q.tick(cfg, runner=runner)
    assert len(starts(cfg, cid)) == n
    assert PG.read_holds(cfg)[cid]["held_by"] == "rate_limit"


@pytest.mark.parametrize("role,project", [("conductor", None), ("steward", "Foo")])
def test_conductor_and_steward_launch_during_a_backoff_and_are_not_counted(
        tmp_path, monkeypatch, role, project):
    from nightwright import rules
    from nightwright.agentic import launch as L
    w = _world(tmp_path)
    for d in ("nw", "foo"):
        (tmp_path / d).mkdir()
    monkeypatch.setattr(rules, "projects_payload", lambda cfg: {"projects": [
        {"name": "Meta-Tools", "research": {"root": str(tmp_path / "nw")}},
        {"name": "Foo", "research": {"root": str(tmp_path / "foo")}}]})
    w.cfg["agentic"].setdefault("conductor", {})["prices"] = {
        "claude": {"input": 3.0, "output": 15.0}}
    _fill_cap(w.cfg)
    before = PG.live_persona_runs(w.cfg, [CID, "other"])
    execs = []
    monkeypatch.setattr(os, "execvpe", lambda f, argv, env: execs.append(argv))
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.chdir(tmp_path)
    L.run(w.cfg, L.build_plan(w.cfg, role, project))            # not held by the backoff
    assert len(execs) == 1 and execs[0][0] == "claude"
    assert PG.live_persona_runs(w.cfg, [CID, "other"]) == before  # not counted toward the cap


# ---- rate limit --------------------------------------------------------------------------------

def _result(tmp_path, *, status=None, message="", is_error=True, name="r"):
    d = tmp_path / f"run-{name}"
    d.mkdir(exist_ok=True)
    raw = {"type": "result", "is_error": is_error, "api_error_status": status, "result": message}
    (d / "result.json").write_text(json.dumps(raw))
    return R.RunResult(exit_code=1 if is_error else 0, timed_out=False, interrupted=False,
                       duration_s=1.0, is_error=is_error, cost_usd=0.0, result_text=message,
                       raw_path=d / "result.json")


def _ok(tmp_path):
    return _result(tmp_path, is_error=False, message="{}", name="ok")


def _events(cfg):
    return [e for e in EV.read_since(cfg, 0) if e["type"] == "rate_limited"]


def test_an_ungated_spec_review_run_neither_starts_nor_resets_the_backoff(tmp_path):
    # spec-review is outside the backoff (Design, Scope): its 429 starts no episode and its
    # success resets nothing; a gated persona's 429 still starts one
    w = _world(tmp_path)
    ctx = Q._Ctx(w.cfg, runner=None)
    st = S.read_state(w.cfg, CID)
    limited = _result(tmp_path, status=429, message="API Error: 429 rate limit")
    Q._note_rate_limit(ctx, st, limited, None, "spec_reviewer")
    assert PG.backoff_until(w.cfg) is None and _events(w.cfg) == []
    Q._note_rate_limit(ctx, st, limited, None, "implementer")
    assert PG.backoff_until(w.cfg) is not None and len(_events(w.cfg)) == 1
    doc = PG.read_rate_limit(w.cfg)
    Q._note_rate_limit(ctx, st, _ok(tmp_path), PG._now_epoch(None) + 10_000, "spec_reviewer")
    assert PG.read_rate_limit(w.cfg) == doc


@pytest.mark.parametrize("adopted", [None, {"exited_before_start": True}])
def test_a_persona_run_with_no_request_still_folds_its_429(tmp_path, monkeypatch, adopted):
    # a fresh flight or an adopted run may carry no request: the persona is the step's
    w = _world(tmp_path)
    limited = _result(tmp_path, status=429, message="API Error: 429 rate limit")

    class Local:
        def run(self, req):
            return limited

    monkeypatch.setattr(RPS, "dispatch", lambda *a, **k: None)
    ctx = Q._Ctx(w.cfg, runner=Local())
    res = Q._do_persona(ctx, S.read_state(w.cfg, CID), Q.Step("triage", persona="triager"),
                        "002-triager", None, 1.0, adopted=adopted)
    assert res.run_result is limited and res.error is None
    assert PG.backoff_until(w.cfg) is not None and len(_events(w.cfg)) == 1


@pytest.fixture
def rl(tmp_path):
    w = _world(tmp_path)
    return w.cfg, tmp_path


def test_rate_limit_markers_are_transient():
    assert set(EV.RATE_LIMIT_MARKERS) <= set(EV._TRANSIENT_MARKERS)


@pytest.mark.parametrize("status,message,want", [
    (429, "API Error: 429 {\"type\":\"rate_limit_error\"}", True),
    (529, "API Error: 529 overloaded", True),
    (None, "API Error: 429 Too Many Requests", True),
    (None, "API Error: Repeated 529 Overloaded errors", True),
    (None, "API Error: rate limit exceeded", True),
    (500, "API Error: 500 internal", False),
    (429, "Claude AI usage limit reached|1700000000", False),
    (None, "API Error: usage_limit reached (429)", False),
    (None, "session limit reached", False),
    (429, "5-hour limit reached \u2219 resets 3am", False),
    (429, "You've hit your limit \u00b7 resets 3am (Europe/Lisbon)", False),
    (429, "Opus weekly limit reached", False),
    (None, "API Error: 429 You've hit your usage limit", False),
    # "hit your rate limit" / "hit your request limit" is the API's rate limit, not the account's
    (429, "API Error: 429 You have hit your rate limit for requests per minute", True),
    (None, "API Error: 429 You have hit your rate limit for requests per minute", True),
    (429, "You've hit your request limit", True),
    (429, "You've hit your session limit \u00b7 resets 3am", False),
    (429, "You've hit your Opus limit", False),
    (None, "FAILED tests/x.py::t - x.py:429: AssertionError", False),
    (None, "x.py:429: AssertionError\nAPI Error: 429", False),
    (None, "API Error: 4290 bytes", False),
    # an API rate-limit message may say "limit reached" or "resets in": still a rate limit
    (429, "API Error: Rate limit reached for requests", True),
    (429, "API Error: 429 rate_limit_error: limit resets in 30s", True),
    (None, "API Error: Rate limit reached for requests", True),
    (529, "API Error: Overloaded, capacity resets in 10s", True),
])
def test_is_rate_limit(tmp_path, status, message, want):
    assert PG.is_rate_limit(_result(tmp_path, status=status, message=message)) is want


def test_a_429_status_counts_only_on_an_error_result_that_was_not_interrupted(tmp_path):
    import dataclasses
    hit = _result(tmp_path, status=429, message="API Error: 429 rate limit")
    assert PG.is_rate_limit(hit)                                       # baseline
    assert not PG.is_rate_limit(_result(tmp_path, status=429, message="API Error: 429",
                                        is_error=False, name="ok429"))
    assert not PG.is_rate_limit(dataclasses.replace(hit, is_error=False))
    assert not PG.is_rate_limit(dataclasses.replace(hit, interrupted=True))
    assert not PG.is_rate_limit(None)


def test_rate_limit_episode_doubles_resets_and_caps(rl):
    cfg, tmp = rl
    t0 = 1_000_000.0
    hit = _result(tmp, status=429, message="API Error: 429")
    for i in range(3):                                         # a burst: one episode
        PG.note_run(cfg, hit, t0 - 5, campaign=CID, now=t0 + i * 0.3)
    rl_ = PG.read_rate_limit(cfg)
    assert rl_["backoff_s"] == 60 and len(_events(cfg)) == 1
    assert _events(cfg)[0]["data"]["backoff_s"] == 60 and _events(cfg)[0]["campaign"] == CID
    assert PG.backoff_until(cfg, t0 + 59) is not None and PG.backoff_until(cfg, t0 + 61) is None
    # a success from a run started before `until` does not reset
    assert PG.note_run(cfg, _ok(tmp), t0 + 10, now=t0 + 70) is None
    assert PG.note_run(cfg, hit, t0, now=t0 + 70) == "rate_limited"     # second episode
    assert PG.read_rate_limit(cfg)["backoff_s"] == 120 and len(_events(cfg)) == 2
    t = t0 + 70
    for want in (240, 480, 900, 900):
        t += 1000
        PG.note_run(cfg, hit, None, now=t)
        assert PG.read_rate_limit(cfg)["backoff_s"] == want
    # a success from a run started after `until` resets: the next episode is 60 s again
    until = PG._epoch(PG.read_rate_limit(cfg)["until"])
    assert PG.note_run(cfg, _ok(tmp), until + 1, now=until + 5) == "reset"
    assert PG.read_rate_limit(cfg)["backoff_s"] == 60
    PG.note_run(cfg, hit, None, now=until + 10)
    assert PG.read_rate_limit(cfg)["backoff_s"] == 60


def test_usage_limit_and_test_output_429_do_not_set_the_backoff(rl):
    cfg, tmp = rl
    PG.note_run(cfg, _result(tmp, status=429, message="Claude AI usage limit reached"), None)
    PG.note_run(cfg, _result(tmp, message="x.py:429: AssertionError", name="t"), None)
    assert not PG.rate_limit_path(cfg).exists() and _events(cfg) == []


def test_concurrent_rate_limit_failures_make_one_episode(rl):
    cfg, tmp = rl
    hit = _result(tmp, status=429, message="API Error: 429")
    threads = [threading.Thread(target=PG.note_run, args=(cfg, hit, None)) for _ in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        join(th)
    assert PG.read_rate_limit(cfg)["backoff_s"] == 60 and len(_events(cfg)) == 1


def test_backoff_holds_every_persona_dispatch_and_shows_held_by(tmp_path, monkeypatch):
    from fanout_support import added, make_cfg, make_repo, script
    repo = make_repo(tmp_path)
    entries = [dict(impl_done(), api_error={"status": 429, "message": "API Error: 429 rate limit"}),
               review_entry("pass")]
    cfg = make_cfg(tmp_path, repo, script(tmp_path, entries))
    cid = added(cfg, monkeypatch)
    runner = R.FakeRunner(cfg["agentic"]["fake_script"])
    for _ in range(3):
        Q.tick(cfg, runner=runner)
        if PG.rate_limit_path(cfg).exists():
            break
    assert PG.read_rate_limit(cfg)["backoff_s"] == 60
    assert len(_events(cfg)) == 1
    n = len(starts(cfg, cid))
    for _ in range(3):                    # the gate step runs; the reviewer step is held
        Q.tick(cfg, runner=runner)
    assert S.read_state(cfg, cid)["state"] == "REVIEWING"
    assert len(starts(cfg, cid)) == n     # nothing started, locally or remotely
    item = next(i for i in Q.queue_payload(cfg)["items"] if i["campaign"] == cid)
    assert item["held_by"] == "rate_limit"
    assert PG.read_holds(cfg)[cid]["held_by"] == "rate_limit"
    # the backoff over (the clock moved past `until`), dispatch resumes
    doc = json.loads(PG.rate_limit_path(cfg).read_text())
    doc["until"] = PG._iso(PG._epoch(doc["until"]) - 3600)
    PG.rate_limit_path(cfg).write_text(json.dumps(doc))
    Q.tick(cfg, runner=runner)
    assert len(starts(cfg, cid)) == n + 1
    assert cid not in PG.read_holds(cfg)


# ---- detectors ---------------------------------------------------------------------------------

def _silent_world(tmp_path):
    w = _world(tmp_path)                                 # IMPLEMENTING, no run in flight
    later = dt.datetime.now().astimezone() + dt.timedelta(seconds=5000)
    return w, later


def _kinds(cfg, now):
    return {t for _k, t, cid, _d in EV.watchdog_checks(cfg, now) if cid == CID}


def test_backoff_hold_raises_no_campaign_silent(tmp_path):
    w, later = _silent_world(tmp_path)
    assert {"campaign_silent", "state_stuck"} <= _kinds(w.cfg, later)        # baseline
    PG._write_json(PG.rate_limit_path(w.cfg),
                   {"until": PG._iso(later.timestamp() + 900), "backoff_s": 900})
    assert not {"campaign_silent", "state_stuck"} & _kinds(w.cfg, later)
    assert CID not in [s["campaign"] for s in EV.ping_digest(w.cfg, later)["stuck"]]
    item = next(i for i in Q.queue_payload(w.cfg, now=later)["items"] if i["campaign"] == CID)
    assert item["held_by"] == "rate_limit"


def test_cap_hold_raises_no_campaign_silent(tmp_path):
    w, later = _silent_world(tmp_path)
    PG._write_json(PG.holds_path(w.cfg), {"holds": {CID: {"held_by": "persona_cap",
                                                          "since": S.now_iso()}}})
    assert not {"campaign_silent", "state_stuck"} & _kinds(w.cfg, later)
    item = next(i for i in Q.queue_payload(w.cfg, now=later)["items"] if i["campaign"] == CID)
    assert item["held_by"] == "persona_cap"


def test_a_cap_hold_older_than_the_mute_limit_is_reported_again(tmp_path):
    # a live count inflated by a run that never ends must not mute the detectors forever
    w, later = _silent_world(tmp_path)
    old = later - dt.timedelta(seconds=PG.CAP_MUTE_MAX_S + 60)
    PG._write_json(PG.holds_path(w.cfg), {"holds": {CID: {"held_by": "persona_cap",
                                                          "since": old.isoformat()}}})
    assert {"campaign_silent", "state_stuck"} <= _kinds(w.cfg, later)
    item = next(i for i in Q.queue_payload(w.cfg, now=later)["items"] if i["campaign"] == CID)
    assert item["held_by"] == "persona_cap"                  # still shown as held


def test_flush_keeps_since_while_the_hold_lasts_and_resets_it_on_a_change(tmp_path,
                                                                         monkeypatch):
    # rewriting `since` on every tick would silently disable CAP_MUTE_MAX_S
    w = _world(tmp_path)
    clock = iter(f"2026-11-01T10:{m:02d}:00+00:00" for m in range(0, 60, 5))
    monkeypatch.setattr(S, "now_iso", lambda: next(clock))

    def tick(why):
        gate = PG.Gate(w.cfg, [CID])
        if why:
            gate._hold(CID, why)
        gate.flush()
        return (PG.read_holds(w.cfg).get(CID) or {}).get("since")

    assert tick("persona_cap") == "2026-11-01T10:00:00+00:00"
    assert tick("persona_cap") == "2026-11-01T10:00:00+00:00"           # the same hold: kept
    assert tick("rate_limit") == "2026-11-01T10:10:00+00:00"            # another hold: reset
    assert tick(None) is None                                           # released
    assert PG.read_holds(w.cfg) == {}
    assert tick("persona_cap") == "2026-11-01T10:20:00+00:00"           # a new hold


def test_the_cap_counts_the_open_run_of_a_campaign_parked_mid_run(tmp_path, monkeypatch):
    # a human parked X while its local implementer runs: the run is still in the pool, and the
    # tick's gate counts it
    w = _world(tmp_path)
    _cap(w.cfg, 2)
    x = dict(w.state, id="SPEC-0901-20261101-abcd", spec_id="SPEC-0901", state="PARKED",
             park={"reason": "human", "detail": "parked by a human"})
    S.create_campaign(w.cfg, x)
    _open_run(w.cfg, x["id"], "001-implementer")
    seen = []

    class Spy(PG.Gate):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            seen.append(self)

    monkeypatch.setattr(PG, "Gate", Spy)
    pool = Q.StepPool()
    pool.adopted = True
    monkeypatch.setattr(pool, "ids", lambda: {x["id"]})
    Q.tick(w.cfg, runner=R.FakeRunner(_write_script(tmp_path / "l.json", LOCAL_OK)), pool=pool,
           admit=False)
    gate, = seen
    assert x["id"] in gate.ids and gate.live() == 1


def test_queue_status_is_list(tmp_path, monkeypatch):
    from nightwright.cli import cli
    w = _world(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: w.cfg)
    r = CliRunner().invoke(cli, ["queue", "status", "--json"])
    assert r.exit_code == 0, r.output
    doc = json.loads(r.output)
    assert doc["schema"] == "nw.queue.v1" and "held_by" in doc["items"][0]
    assert doc["personas"]["max_personas"] is None


# ---- cost by worker ----------------------------------------------------------------------------

def _charge(state, worker, cost):
    rr = R.RunResult(exit_code=0, timed_out=False, interrupted=False, duration_s=1.0,
                     is_error=False, cost_usd=cost, verdict={"summary": "x"}, verdict_ok=True,
                     worker=worker)
    return Q._record_run(state, "implementer", "001-implementer", rr, 4.0)


def test_remote_cost_counts_like_local_cost():
    profile = {"personas": {"implementer": {"max_budget_usd": 4.0}}}
    remote = {"id": "x", "cost": {"total_usd": 0.0, "cap_usd": 10.0, "by_persona": {}}}
    local = {"id": "y", "cost": {"total_usd": 0.0, "cap_usd": 10.0, "by_persona": {}}}
    _charge(remote, "box", 6.5)
    _charge(local, None, 6.5)
    assert remote["cost"]["total_usd"] == local["cost"]["total_usd"] == 6.5
    assert Q._cap_blocks(remote, profile, "implementer")[0] is not None     # 6.5 + 4 > 10
    assert Q._cap_blocks(remote, profile, "implementer") == Q._cap_blocks(local, profile,
                                                                          "implementer")


def test_cost_by_worker_in_campaign_show_and_export(tmp_path, monkeypatch):
    from nightwright.cli import cli
    w = _world(tmp_path)
    st = S.read_state(w.cfg, CID)
    st["cost"] = {"total_usd": 0.0, "cap_usd": 10.0, "by_persona": {}}
    for worker, cost, rid in (("box", 1.25, "001"), (None, 0.5, "002"), ("box", 0.25, "003")):
        rr = R.RunResult(exit_code=0, timed_out=False, interrupted=False, duration_s=1.0,
                         is_error=False, cost_usd=cost, worker=worker)
        for etype, payload in Q._record_run(st, "implementer", f"{rid}-implementer", rr, 4.0):
            S.append_event(w.cfg, CID, etype, payload)
    S.write_state(w.cfg, CID, st)
    want = {"box": 1.5, "local": 0.5}
    monkeypatch.setattr("nightwright.cli.load_config", lambda: w.cfg)
    r = CliRunner().invoke(cli, ["campaign", "show", CID, "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["cost_by_worker"] == want
    r = CliRunner().invoke(cli, ["campaign", "show", CID])
    assert "cost by worker" in r.output and "box $1.50" in r.output
    row = CE.campaign_row(w.cfg, S.read_state(w.cfg, CID), S.read_ledger(w.cfg, CID),
                          dt.datetime.now().astimezone())
    assert row["cost_by_worker"] == want and row["cost_usd"] == 2.0


# ---- queue-driven: persona runs on loopback workers through `Q.tick` --------------------------

def _loopback(cfg, tmp_path, entries, names=("box",)):
    """Loopback persona workers `names` for the e2e harness's `cfg`; each worker job runs
    `entries` with the worker's FakeRunner."""
    from types import SimpleNamespace
    d = tmp_path / "lb"
    d.mkdir(exist_ok=True)
    w = SimpleNamespace(tmp=d, cfg=cfg)
    _remote(w, _write_script(d / "worker.json", entries))
    recs = json.loads(WK.path(cfg).read_text())["workers"]
    recs += [dict(recs[0], name=n) for n in names[1:]]
    WK.path(cfg).write_text(json.dumps({"schema": WK.SCHEMA, "workers": recs}))


def _ends(cfg, cid, persona=None):
    return [e for e in S.read_ledger(cfg, cid, types=["run.end"])
            if persona is None or e.get("persona") == persona]


def _tick_until(cfg, runner, pred, ticks=12):
    for _ in range(ticks):
        if pred():
            return
        Q.tick(cfg, runner=runner)
    assert pred()


def test_a_tick_places_the_implementer_remotely_and_stores_the_affinity(e2e, tmp_path):
    # the local runner has no implementer entry: a local run would fail the step
    cfg, cid, repo, runner = e2e({A: [review_entry("pass")], B: [b_entry("pass")]})
    _loopback(cfg, tmp_path, [impl_done()])
    _tick_until(cfg, runner, lambda: _ends(cfg, cid, "implementer"))
    end, = _ends(cfg, cid, "implementer")
    assert end["worker"] == "box" and not end["is_error"], end
    st = S.read_state(cfg, cid)
    assert st["remote_affinity"] == "box" and st["remote_affinity_at"]


def test_a_remote_rate_limit_starts_the_backoff_and_holds_the_next_step(e2e, tmp_path):
    cfg, cid, repo, runner = e2e({A: [review_entry("pass")], B: [b_entry("pass")]})
    _loopback(cfg, tmp_path, [dict(impl_done(), api_error={
        "status": 429, "message": "API Error: 429 rate limit"})])
    _tick_until(cfg, runner, lambda: _ends(cfg, cid, "implementer"))
    end, = _ends(cfg, cid, "implementer")
    assert end["worker"] == "box" and end["is_error"], end
    assert PG.read_rate_limit(cfg)["backoff_s"] == 60 and PG.backoff_until(cfg) is not None
    assert [e["campaign"] for e in _events(cfg)] == [cid]
    n = len(starts(cfg, cid))
    for _ in range(3):
        Q.tick(cfg, runner=runner)
    assert len(starts(cfg, cid)) == n                 # held: no new run, locally or remotely
    assert PG.read_holds(cfg)[cid]["held_by"] == "rate_limit"


C = "third_reviewer"


def test_cap_of_two_across_two_workers_and_local_runs_two_at_once(e2e, tmp_path, monkeypatch):
    import dataclasses
    from nightwright.agentic import personas as PS
    monkeypatch.setitem(PS.PERSONAS, C, dataclasses.replace(PS.PERSONAS[A], name=C))
    monkeypatch.setattr(PS, "REVIEWER_PERSONAS", (A, B, C))
    from fanout_support import B_SETTINGS
    cfg, cid, repo, runner = e2e({"implementer": [impl_done()], A: [review_entry("pass")],
                                  B: [b_entry("pass")], C: [dict(review_entry("pass"), persona=C)]},
                                 profile={"reviewers": [A, B, C],
                                          "personas": {B: B_SETTINGS, C: B_SETTINGS}})
    _cap(cfg, 2)
    _tick_until(cfg, runner, lambda: _ends(cfg, cid, "implementer"))   # local: no worker yet
    assert "worker" not in _ends(cfg, cid, "implementer")[0]
    reviewer = {k: v for k, v in review_entry("pass").items() if k != "persona"}
    _loopback(cfg, tmp_path, [reviewer], names=("box", "box2"))
    _tick_until(cfg, runner, lambda: S.read_state(cfg, cid)["state"] in ("MERGED", "PARKED"),
                ticks=20)
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st.get("park")
    led = [(e["type"], e.get("persona"), e.get("worker"))
           for e in S.read_ledger(cfg, cid, types=["run.start", "run.end"])]
    live = peak = 0
    for kind, persona, _w in led:
        if persona in (A, B, C):
            live += 1 if kind == "run.start" else -1
            peak = max(peak, live)
    assert peak == 2                                   # never three reviewers at once
    workers = {p: w for k, p, w in led if k == "run.end" and p in (A, B, C)}
    first_two = [p for k, p, _w in led if k == "run.start" and p in (A, B, C)][:2]
    assert {workers[p] for p in first_two} == {"box", "box2"}   # box full: falls to box2
    third = [p for k, p, _w in led if k == "run.start" and p in (A, B, C)][2]
    # the two reviewers race to set the affinity; the third one, dispatched alone, follows it
    assert st["remote_affinity"] in ("box", "box2") and workers[third] == st["remote_affinity"]
    notes = [e["text"] for e in S.read_ledger(cfg, cid, types=["note"])]
    assert any(t.startswith("persona cap:") for t in notes)       # the third one waited
