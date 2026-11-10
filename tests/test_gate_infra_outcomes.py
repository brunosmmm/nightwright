"""SPEC-0515: OOM kills, Slurm time limits, preemption and node failures are infra outcomes.

Worker: OOM detection from a stand-in cgroup `memory.events` (the classifier, and the real
`nw worker run-job` inside a fake Slurm job), and a TERM from Slurm without a `cancel` file
leaves no final frame. Coordinator: each `slurm_state` of the job's `error` frame mapped to a
gate infra code (async `_Job` and the sync `gate-job` stream), the `attempt` frame of a requeued
job. Queue: `infra_only` / `decide_gate_infra` as pure functions and through `_apply` (boost,
park `gate_oom`, Slurm timeout -> the gate-timeout path, node failure -> re-gate then local),
the triager never started; the local run's placement reason `node_fail`; rendering."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nightwright.agentic import cgroup_oom as CO
from nightwright.agentic import gates as G
from nightwright.agentic import journey as JY
from nightwright.agentic import queue as Q
from nightwright.agentic import remote_gates as RG
from nightwright.agentic import remote_jobs as RJ
from nightwright.agentic import store as S
from nightwright.agentic import timeline as TL
from nightwright.agentic import worker as W
from slurm_fake import SlurmFake
from test_agentic_queue import _apply, _gating, opened  # noqa: F401  (fixture)
from test_executor_slurm import _cfg, _frames, _read, _state, _submit
from test_worker import _bundle_job, _gate, _job_dir, _no_finish_gc, src, wenv  # noqa: F401
from waits import until

pytestmark = pytest.mark.integration

JOB = "C-1-gate-001-a1"


# ---- worker: OOM detection -----------------------------------------------------------------------

def _cgroup(tmp_path, monkeypatch, oom=0):
    """A stand-in Slurm 26.05 job cgroup: the task's dir and the job's dir above it."""
    root = tmp_path / "cg"
    rel = "system.slice/slurmstepd.scope/TOKEN/step_batch/user/task_0"
    task = root / rel
    task.mkdir(parents=True)
    job = root / "system.slice/slurmstepd.scope/TOKEN"
    (job / "memory.events").write_text(f"low 0\nhigh 0\nmax 3\noom 1\noom_kill {oom}\n")
    (task / "memory.events").write_text("low 0\noom_kill 0\n")
    proc = tmp_path / "proc_cgroup"
    proc.write_text(f"0::/{rel}\n")
    monkeypatch.setenv(CO.ROOT_ENV, str(root))
    monkeypatch.setenv(CO.PROC_ENV, str(proc))
    monkeypatch.setenv("SLURM_JOB_ID", "42")
    return job / "memory.events"


def _r(rc, ok=False, interrupted=False):
    return G.GateResult("pytest", True, ok, rc, False, 1.0, "gates/001-pytest.log", "",
                        interrupted=interrupted)


def test_oom_kills_reads_the_job_level_counter(tmp_path, monkeypatch):
    events = _cgroup(tmp_path, monkeypatch, oom=2)
    assert CO.oom_kills() == 2
    events.write_text("oom_kill 5\n")
    assert CO.oom_kills() == 5
    monkeypatch.delenv("SLURM_JOB_ID")
    assert CO.oom_kills() is None                       # outside a Slurm job: never classified


@pytest.mark.parametrize("rc", [-9, 137])
def test_a_sigkill_while_the_counter_rose_is_oom(tmp_path, monkeypatch, rc):
    events = _cgroup(tmp_path, monkeypatch)
    before = CO.oom_kills()
    events.write_text("oom_kill 1\n")
    assert CO.classify(_r(rc), before).infra == "oom"


def test_no_rise_or_another_exit_is_not_oom(tmp_path, monkeypatch):
    events = _cgroup(tmp_path, monkeypatch)
    before = CO.oom_kills()
    assert CO.classify(_r(-9), before).infra is None    # a SIGKILL with no OOM: a red gate
    events.write_text("oom_kill 1\n")
    assert CO.classify(_r(1), before).infra is None     # a plain failure
    assert CO.classify(_r(-9, interrupted=True), before).infra is None
    assert CO.classify(_r(-9), None).infra is None      # nothing read before


@pytest.fixture
def fake(tmp_path, monkeypatch):
    from nightwright.agentic import executor_slurm as ES
    f = SlurmFake(tmp_path / "slurm").install(monkeypatch)
    ES.forget()
    ES._LICENSES.clear()
    ES._LIMITS.clear()
    monkeypatch.setenv(W.SLOT_WAIT_ENV, "3600")
    yield f
    f.close()
    ES.forget()


def test_run_job_reports_an_oom_killed_gate_as_infra(src, tmp_path, wenv, fake, monkeypatch):
    events = _cgroup(tmp_path, monkeypatch)
    # the gate raises the job cgroup's oom_kill counter and dies of SIGKILL, as the kernel does
    h, data = _bundle_job(src, tmp_path, [
        _gate("pytest", f"printf 'oom_kill 1\\n' > '{events}'; kill -9 $$"),
        _gate("lint", "true")])
    cfg = _cfg(wenv)
    assert _submit(cfg, h, data)[0] == 0
    job, jid = _job_dir(wenv, h["job_id"]), fake.ids()[0]
    assert fake.run(jid) == 0
    gates = [f for f in _frames(job) if f["type"] == "gate"]
    assert gates[0]["result"]["infra"] == "oom" and gates[0]["result"]["exit_code"] in (-9, 137)
    assert gates[1]["result"]["infra"] is None
    # the coordinator's view: the run is infra-only, never triaged
    rec = RJ.create({"data_dir": str(tmp_path / "coord")}, job_id=h["job_id"], campaign="C-1",
                    worker="box", attempt=1, seq=1, submitted_at=time.time(),
                    deadline_at=time.time() + 600)
    job_ = RG._Job({"data_dir": str(tmp_path / "coord")}, {"name": "box"},
                   {**rec, "gates": ["pytest", "lint"]}, {"gates": []}, 1)
    job_.gates_dir = tmp_path / "coord-gates"
    run = None
    for f in _frames(job):
        if f["type"] == "done":
            f = {**f, "changed": []}
        run = job_.feed(f) or run
    payload = G.as_payload(run)
    assert Q.infra_only(payload) and payload["results"][0]["infra"] == "oom"
    assert "infra" not in payload["results"][1]


def test_a_term_from_slurm_leaves_no_final_frame(src, tmp_path, wenv, fake, monkeypatch):
    """SPEC-0515: a TERM without the job's `cancel` file (the time limit's early TERM, a
    preemption): the gates end, no `done`, no `result.json`; the reader then writes the frame
    Slurm's end state calls for."""
    monkeypatch.setenv("NW_WORKER_SLURM_END_WAIT_S", "0.2")
    marker = tmp_path / "never"
    h, data = _bundle_job(src, tmp_path, [_gate(
        "wait", f'while [ ! -f "{marker}" ]; do sleep 0.05; done')])
    cfg = _cfg(wenv)
    assert _submit(cfg, h, data)[0] == 0
    job, jid = _job_dir(wenv, h["job_id"]), fake.ids()[0]
    fake.start(jid)
    assert until(lambda: "started" in [f["type"] for f in _frames(job)])
    subprocess.run(["scancel", "--batch", "--signal=TERM", str(jid)], check=True, timeout=30)
    assert fake.finish(jid) == 143
    types = [f["type"] for f in _frames(job)]
    assert "done" not in types and "error" not in types
    assert not (job / "result.json").exists()
    fake.end(jid, "TIMEOUT")                            # Slurm's end state for the time limit
    assert _state(cfg, job) == "error"
    last = _frames(job)[-1]
    assert (last["type"], last["slurm_state"], last["infra"]) == ("error", "TIMEOUT", True)
    assert RG.infra_code(last) == "slurm_timeout"


# ---- coordinator: mapping the job's end -------------------------------------------------------------

def _job(tmp_path, gates=("g1", "g2")):
    cfg = {"data_dir": str(tmp_path / "coord")}
    rec = RJ.create(cfg, job_id=JOB, campaign="C-1", worker="box", attempt=1, seq=1,
                    submitted_at=time.time(), deadline_at=time.time() + 600)
    rec = RJ.update(cfg, JOB, gates=list(gates))
    job = RG._Job(cfg, {"name": "box"}, rec, {"gates": [{"name": "g2", "required": False}]}, 1)
    return cfg, job


def _frame(n, type_, **kw):
    return {"schema": W.FRAME_SCHEMA, "type": type_, "job_id": JOB, "n": n, **kw}


def _gate_frame(n, name, ok=True):
    res = {"name": name, "required": True, "ok": ok, "exit_code": 0 if ok else 1,
           "timed_out": False, "duration_s": 0.1, "log": "", "tail": ""}
    return _frame(n, "gate", result=res, files={})


def _slurm_error(n, state):
    return _frame(n, "error", stage="slurm", error=f"ended {state}", slurm_state=state,
                  slurm_job_id=7, exit_code=0, signal=None, infra=True)


@pytest.mark.parametrize("state,code", [("OUT_OF_MEMORY", "oom"), ("TIMEOUT", "slurm_timeout"),
                                        ("NODE_FAIL", "node_fail"), ("BOOT_FAIL", "node_fail"),
                                        ("PREEMPTED", "preempted")])
def test_each_slurm_end_maps_to_an_infra_code(tmp_path, state, code):
    cfg, job = _job(tmp_path)
    job.feed(_frame(1, "started", slot_wait_s=0.0))
    job.feed(_gate_frame(2, "g1"))
    run = job.feed(_slurm_error(3, state))
    assert run is not None and not run.ok and run.job_id == JOB and run.event_uid == f"{JOB}:3"
    g1, g2 = run.results
    assert g1.ok and g1.infra is None                   # what reported stands
    assert (g2.name, g2.ok, g2.infra, g2.required) == ("g2", False, code, False)
    assert RJ.read(cfg, JOB)["cursor"] == 3
    payload = G.as_payload(dataclass_required(run))
    assert Q.infra_only(payload)


def dataclass_required(run):
    """The same run with every gate required (`infra_only` looks at required gates)."""
    from dataclasses import replace
    return replace(run, results=tuple(replace(r, required=True) for r in run.results))


@pytest.mark.parametrize("state", ["FAILED", "CANCELLED", "COMPLETED", "DEADLINE"])
def test_other_slurm_ends_still_fall_back(tmp_path, state):
    _cfg_, job = _job(tmp_path)
    with pytest.raises(RG.RemoteFailure) as e:
        job.feed(_slurm_error(1, state))
    assert e.value.stage == "slurm"


def test_a_job_whose_gates_all_passed_is_not_blamed_on_them(tmp_path):
    _cfg_, job = _job(tmp_path)
    job.feed(_gate_frame(1, "g1"))
    job.feed(_gate_frame(2, "g2"))
    with pytest.raises(RG.RemoteFailure):
        job.feed(_slurm_error(3, "OUT_OF_MEMORY"))


def test_an_attempt_frame_drops_the_earlier_attempt(tmp_path):
    """SPEC-0515: a requeued job (preemption) writes `attempt`; the coordinator no longer
    refuses it, and the second attempt's results replace the first's."""
    assert "attempt" in W.FRAME_TYPES
    cfg, job = _job(tmp_path)
    job.gates_dir.mkdir(parents=True, exist_ok=True)
    (job.gates_dir / "001-g1.log").write_text("attempt 1\n")
    job.feed(_frame(1, "started", slot_wait_s=0.0))
    job.feed(_gate_frame(2, "g1", ok=False))
    job.feed(_frame(3, "attempt", attempt=2))
    assert job.results == [] and not (job.gates_dir / "001-g1.log").exists()
    job.feed(_frame(4, "started", slot_wait_s=1.0))
    job.feed(_gate_frame(5, "g1"))
    job.feed(_gate_frame(6, "g2"))
    run = job.feed(_frame(7, "done", ok=True, interrupted=False, duration_s=1.0, slot_wait_s=1.0,
                          changed=[]))
    assert run.ok and [r.name for r in run.results] == ["g1", "g2"]


def test_the_sync_stream_maps_a_slurm_end_and_an_attempt(tmp_path):
    """The sync `gate-job` stream (`_dispatch`): an `attempt` frame resets the results, an
    infra `error` frame ends the run as infra, no fallback."""
    frames = [_frame(None, "accepted"), _frame(None, "started", slot_wait_s=0.0),
              _gate_frame(None, "g1", ok=False), _frame(None, "attempt", attempt=2),
              _frame(None, "started", slot_wait_s=0.0), _gate_frame(None, "g1"),
              _slurm_error(None, "OUT_OF_MEMORY")]
    out = tmp_path / "frames.jsonl"
    out.write_text("".join(json.dumps({k: v for k, v in f.items() if v is not None}) + "\n"
                           for f in frames))
    ssh = tmp_path / "ssh"
    ssh.write_text(f"#!/bin/sh\ncat '{out}'\nexec cat >/dev/null\n")
    ssh.chmod(0o755)
    worker = {"name": "box", "ssh_argv": [str(ssh)], "ssh": "box", "nw": "nw"}
    header = {"job_id": JOB, "bundle_bytes": 0, "timeout_s": 60,
              "payload": {"gates": [{"name": "g1", "cmd": "x"}, {"name": "g2", "cmd": "y"}]}}
    o = RG._dispatch(worker, header, tmp_path / "none", {"data_dir": str(tmp_path / "c")}, "C-1",
                     1, None, gates_dir=tmp_path / "gates")
    assert o.failure is None and o.run is not None and o.run.worker == "box"
    g1, g2 = o.run.results
    assert g1.ok and (g2.infra, g2.ok) == ("oom", False)


# ---- queue -------------------------------------------------------------------------------------

def _res(name, ok=False, infra=None, required=True, timed_out=False):
    r = {"name": name, "ok": ok, "required": required, "timed_out": timed_out}
    if infra:
        r["infra"] = infra
    return r


@pytest.mark.parametrize("results,expect", [
    ([_res("a", infra="oom")], True),
    ([_res("a", infra="oom"), _res("b", ok=True)], True),
    ([_res("a", infra="oom"), _res("b")], False),          # a real failure next to it
    ([_res("a", infra="oom"), _res("b", required=False)], True),
    ([_res("a", ok=True)], False),
    ([_res("a", infra="bogus")], False),
])
def test_infra_only(results, expect):
    assert Q.infra_only({"results": results}) is expect


def _st(results, **counters):
    return {"last_gate": {"results": results}, "counters": counters}


def test_decide_gate_infra_policy():
    oom = [_res("pytest", infra="oom")]
    assert Q.decide_gate_infra(_st(oom)).reason == "gate_oom_regate"
    step = Q.decide_gate_infra(_st(oom, oom_retries=1), 1536)
    assert (step.kind, step.reason, step.resume_to) == ("park", "gate_oom", "GATING")
    assert "pytest" in step.detail and "> 1536 MB" in step.detail and "2304M" in step.detail
    assert "resources.mem" in step.detail and ".nw/project.yaml" in step.detail
    assert Q.decide_gate_infra(_st([_res("a", infra="slurm_timeout")])).reason == "slurm_timeout"
    nf = [_res("a", infra="node_fail")]
    assert Q.decide_gate_infra(_st(nf)).reason == "gate_node_fail_regate"
    assert Q.decide_gate_infra(_st(nf, node_fail_regates=1)).reason == "gate_node_fail_local"
    assert Q.decide_gate_infra(_st([_res("a", infra="preempted")])).reason == \
        "gate_node_fail_regate"
    assert Q.suggested_mem_mb(1024) == 1536 and Q.suggested_mem_mb(1000) == 1536


def _infra_run(seq, code, name="test"):
    r = G.GateResult(name, True, False, None, False, 0.0, "", "", infra=code)
    return G.GateRun(seq, False, (r,), "2026-11-10T00:00:00", 0.1, worker="box")


def _no_triager(cfg, cid):
    return all(e.get("persona") != "gate_triager" for e in S.read_ledger(cfg, cid,
                                                                       types=["run.start"]))


def test_oom_regates_with_more_memory_then_parks(opened):
    cfg, cid, ctx = opened
    _gating(cfg, cid)
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=_infra_run(1, "oom"))
    assert (row["to"], row["reason"]) == ("GATING", "gate_oom_regate")
    assert st["mem_boost"] == Q.MEM_BOOST and st["counters"]["oom_retries"] == 1
    assert st["counters"]["loops"] == 0 and _no_triager(cfg, cid)
    # the re-gate's header asks for 1.5x memory
    header = RG._header({"gates": [{"name": "test", "cmd": "x"}]}, st, cfg, 2, "j", "k", "s",
                        "b", [], 0)
    assert header["payload"]["resources"]["mem_mb"] == 1536          # 1G default x 1.5
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=_infra_run(2, "oom"))
    assert (row["to"], row["reason"]) == ("PARKED", "gate_oom")
    assert "test" in st["park"]["detail"] and "resources.mem" in st["park"]["detail"]
    assert _no_triager(cfg, cid)
    gate = S.read_ledger(cfg, cid, types=["gate"])[-1]
    assert gate["results"][0]["infra"] == "oom"


def test_a_slurm_timeout_follows_the_gate_timeout_path(opened):
    cfg, cid, ctx = opened
    _gating(cfg, cid)
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=_infra_run(1, "slurm_timeout"))
    assert (row["to"], row["reason"]) == ("GATING", "gate_timeout_regate")
    assert st["counters"]["timeout_regates"] == 1 and _no_triager(cfg, cid)
    note = [n["text"] for n in S.read_ledger(cfg, cid, types=["note"]) if "gate timeout" in n["text"]]
    assert note and "Slurm time limit" in note[-1]


def test_a_node_failure_regates_then_runs_locally(opened, monkeypatch):
    cfg, cid, ctx = opened
    _gating(cfg, cid)
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=_infra_run(1, "node_fail"))
    assert (row["to"], row["reason"]) == ("GATING", "gate_node_fail_regate")
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=_infra_run(2, "node_fail"))
    assert (row["to"], row["reason"]) == ("GATING", "gate_node_fail_local")
    assert st["gate_local"]["reason"] == "node_fail" and _no_triager(cfg, cid)
    # the gate step runs that one gate locally, with SPEC-0505's reason
    seen = {}

    def run_all(*a, **kw):
        seen.update(kw)
        return G.GateRun(3, True, (), "t", 0.0)
    monkeypatch.setattr(G, "run_all", run_all)
    Q._do_gate(ctx, st)
    assert seen["local"] is True and seen["local_reason"] == "node_fail"
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=G.GateRun(
        3, True, (), "t", 0.0, placement=G.local_placement("node_fail", "x")))
    assert row["to"] == "REVIEWING" and st["gate_local"] is None
    assert st["counters"]["node_fail_regates"] == 0
    assert S.read_ledger(cfg, cid, types=["gate"])[-1]["placement"]["reason"] == "node_fail"


def test_non_infra_failures_are_unchanged(opened):
    cfg, cid, ctx = opened
    _gating(cfg, cid)
    run = G.GateRun(1, False, (G.GateResult("t", True, False, 1, False, 0.1, "", ""),), "t", 0.1)
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=run)
    assert (row["to"], row["reason"]) == ("TRIAGING", "gates_failed")
    mixed = G.GateRun(2, False, (G.GateResult("t", True, False, 1, False, 0.1, "", ""),
                                 G.GateResult("u", True, False, None, False, 0, "", "",
                                              infra="oom")), "t", 0.1)
    _gating(cfg, cid)
    row, st = _apply(ctx, cfg, cid, "gate", gate_run=mixed)
    assert row["to"] == "TRIAGING"


def test_local_placement_node_fail_and_rendering():
    assert G.local_placement("node_fail", "x")["reason"] == "node_fail"
    ev = {"type": "gate", "ok": False, "gate_seq": 3,
          "results": [{"name": "pytest", "ok": False, "infra": "oom"},
                      {"name": "lint", "ok": False, "infra": "slurm_timeout"}]}
    text = JY._event_text(ev)
    assert "pytest (oom)" in text and "lint (slurm timeout)" in text
    assert TL.INFRA_LABELS["slurm_timeout"] == "slurm timeout"
