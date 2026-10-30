"""SPEC-0225: detached persona runs, adopted across runner restarts. A real `ClaudeRunner` runs
the fake `claude` of tests/fixtures/fake_claude.py through the wrapper; a "dead runner" is a
StepPool that is discarded while its runs keep going."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from test_agentic_e2e import _git, added, impl_done, make_cfg, make_repo, review, script, tr_review
from test_agentic_reexec import _code_repo
from test_agentic_runproc import child_pid, pid_alive, until
from wt.agentic import lease as L
from wt.agentic import personas as PS
from wt.agentic import queue as Q
from wt.agentic import runner as R
from wt.agentic import runproc as RP
from wt.agentic import store as S

FAKE = Path(__file__).parent / "fixtures" / "fake_claude.py"


@pytest.fixture
def world(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "claude").symlink_to(FAKE)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    monkeypatch.setenv("WT_RUNPROC_GRACE_S", "1")
    monkeypatch.setattr(RP, "GRACE_S", 1.0)
    monkeypatch.setenv("FAKE_CLAUDE_RELEASE", str(tmp_path / "release"))
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP_S", "60")
    monkeypatch.setenv("FAKE_CLAUDE_VERDICT", json.dumps(impl_done()["verdict"]))
    monkeypatch.setenv("FAKE_CLAUDE_EDITS", json.dumps(impl_done()["edits"]))
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    cfg["agentic"]["runner"] = "claude"
    cid = added(cfg, monkeypatch)
    w = type("World", (), {})()
    w.cfg, w.cid, w.runner, w.tmp = cfg, cid, R.ClaudeRunner(), tmp_path
    w.release = lambda: (tmp_path / "release").write_text("")
    w.pools = []
    yield w
    w.release()
    for pool in w.pools:
        pool.detach_force()
        pool.shutdown()
    for d in (S.runs_dir(cfg, cid)).glob("*"):         # leave no process behind
        RP.terminate_run(d)
        RP.terminate_run(d, kill=True)


def start_impl(w, **kw):
    """Tick until the implementer run is in flight; returns the pool (a 'runner')."""
    pool = Q.StepPool()
    w.pools.append(pool)
    for _ in range(4):
        Q.tick(w.cfg, runner=w.runner, pool=pool, **kw)
        if pool.flights:
            break
    assert pool.flights, S.read_state(w.cfg, w.cid)["state"]
    (_, _, hk), = pool.flights[w.cid].tasks
    assert until(lambda: hk.phase == "wait" and RP.read_json(hk.run_dir / "child.json"))
    assert until(lambda: (Path(S.read_state(w.cfg, w.cid)["repo"]["worktree"]) / "child.pid").exists())
    return pool, hk.run_dir


def die(pool):
    """The runner 'dies': its threads are released, the run keeps going."""
    pool.detach_force()
    pool.shutdown()


def run_ends(w, run_id=None):
    ends = S.read_ledger(w.cfg, w.cid, types=["run.end"])
    return [e for e in ends if run_id in (None, e.get("run_id"))]


def applied_after_end(w, run_id):
    """Ledger events a run's `run.end` led to, up to the next `run.start`. A tick that closes a
    superseded run may start a fresh step at once (its own run.start/run.end), so the tests
    look at what follows the adopted run instead of at the state name."""
    out, seen = [], False
    for ev in S.read_ledger(w.cfg, w.cid):
        if seen and ev["type"] == "run.start":
            break
        if seen:
            out.append(ev["type"])
        seen = seen or (ev["type"] == "run.end" and ev.get("run_id") == run_id)
    return out


def adopt_journal(w):
    return [r for r in Q.read_journal(w.cfg) if r["kind"] == "adopt"]


def tick_until(w, pool, cond, n=60, steps=None):
    for _ in range(n):
        report = Q.tick(w.cfg, runner=w.runner, pool=pool)
        if steps is not None:
            steps.extend(report.steps)
        if cond():
            return True
        time.sleep(0.1)
    return False


def test_adopt_registry_complete():
    # spec_reviewer, conflict_resolver and spec_writer run in-process (no hooks, never detached)
    assert set(PS.PERSONAS) - {"spec_reviewer", "conflict_resolver", "spec_writer"} \
        <= set(PS.ADOPT_REGISTRY)
    assert PS.ADOPT_REGISTRY["review_triager"] == ("review", ("review_triager",))


def test_run_start_records_step_and_state(world):
    pool, run_dir = start_impl(world)
    start, = S.read_ledger(world.cfg, world.cid, types=["run.start"])
    assert start["step"] == {"kind": "implement", "personas": []}
    assert start["state"] == "IMPLEMENTING"


def test_adopt_running_then_finished_once(world):
    pool, run_dir = start_impl(world)
    pid = child_pid(R.RunRequest(*(None,) * 0, **{}) if False else type("r", (), {"cwd": Path(
        S.read_state(world.cfg, world.cid)["repo"]["worktree"])}))
    die(pool)
    assert pid_alive(pid)                                  # the run outlived its runner
    new = Q.StepPool()
    world.pools.append(new)
    Q.tick(world.cfg, runner=world.runner, pool=new)
    assert [r["status"] for r in adopt_journal(world)[-1]["runs"]] == ["running"]
    assert new.flights and not run_ends(world)
    world.release()
    assert tick_until(world, new, lambda: run_ends(world))
    assert len(run_ends(world)) == 1
    assert S.read_state(world.cfg, world.cid)["state"] in ("GATING", "TRIAGING", "REVIEWING",
                                                           "CLOSING", "MERGED")
    assert len(S.read_ledger(world.cfg, world.cid, types=["run.start"])) == 1


def test_detached_run_keeps_its_tmp_dir_until_the_adopted_run_ends(world):
    """SPEC-0253: the runner dying (or detaching) does not remove `run-<id>`; the adopted run's
    `run.end` does."""
    pool, run_dir = start_impl(world)
    tmp = S.child_tmp(world.cfg, world.cid, f"run-{run_dir.name}")
    assert tmp.is_dir()
    (tmp / "scratch").write_text("x")
    die(pool)
    assert (tmp / "scratch").is_file() and not run_ends(world)
    new = Q.StepPool()
    world.pools.append(new)
    Q.tick(world.cfg, runner=world.runner, pool=new)             # adopts the still-running run
    assert (tmp / "scratch").is_file() and not run_ends(world)
    world.release()
    assert tick_until(world, new, lambda: run_ends(world))
    assert not tmp.exists()


def test_queue_tick_adopts_finished(world):
    pool, run_dir = start_impl(world)
    die(pool)
    world.release()
    assert until((run_dir / "exit.json").exists)
    Q.tick(world.cfg, runner=world.runner)                 # single shot, throwaway pool
    assert adopt_journal(world)[-1]["runs"][0]["status"] == "finished"
    assert len(run_ends(world)) == 1
    assert S.read_state(world.cfg, world.cid)["state"] != "IMPLEMENTING"


def test_adopt_after_park_resume_while_no_runner(world):
    """A park + resume done while no runner holds the lease leaves the state name unchanged;
    the ledger-seq check still supersedes the adopted run."""
    pool, run_dir = start_impl(world)
    die(pool)
    Q.apply_park(world.cfg, Q.plan_park(world.cfg, world.cid, "human check"))
    Q.apply_resume(world.cfg, Q.plan_resume(world.cfg, world.cid))
    assert S.read_state(world.cfg, world.cid)["state"] == "IMPLEMENTING"
    world.release()
    assert until((run_dir / "exit.json").exists)
    report = Q.tick(world.cfg, runner=world.runner)
    row = next(s for s in report.steps if s.get("run_id") == run_dir.name)
    assert row["reason"] == "superseded_by_human"
    assert len(run_ends(world, run_dir.name)) == 1
    assert not {"commit", "transition"} & set(applied_after_end(world, run_dir.name))


def test_adopt_after_abandon(world):
    pool, run_dir = start_impl(world)
    die(pool)
    Q.apply_park(world.cfg, Q.plan_park(world.cfg, world.cid, "no longer needed"))
    Q.apply_abandon(world.cfg, Q.plan_abandon(world.cfg, world.cid))
    world.release()
    assert until((run_dir / "exit.json").exists)
    report = Q.tick(world.cfg, runner=world.runner)
    assert len(run_ends(world)) == 1, (report, Q.read_journal(world.cfg)[-3:])
    assert S.read_state(world.cfg, world.cid)["state"] == "ABANDONED"


def _orphan_world(world, monkeypatch, *, launch_json):
    """An old runner's run: finished or not, nobody claimed it. Returns (run_dir, at) where
    `at(age)` sets the patched clock `age` seconds after the grace anchor (the `launch.json`
    mtime, else the `run.start` ts)."""
    pool, run_dir = start_impl(world)
    die(pool)
    world.release()
    assert until((run_dir / "exit.json").exists)
    for name in ("proc.json", "exit.json", "child.json"):
        (run_dir / name).unlink()
    real, offset = time.time, {"s": 0.0}
    monkeypatch.setattr(time, "time", lambda: real() + offset["s"])
    if launch_json:
        (run_dir / "launch.json").write_text("{}")
        anchor = real() - 5
        os.utime(run_dir / "launch.json", (anchor, anchor))
    else:
        (run_dir / "launch.json").unlink()
        anchor = R._epoch(S.read_ledger(world.cfg, world.cid, types=["run.start"])[0]["ts"])

    def at(age):
        offset["s"] = anchor + age - real()

    return run_dir, at


def _orphan_claimed_and_respawned(world, run_dir, new):
    assert json.loads((run_dir / "proc.json").read_text())["by"] == "adopt"
    assert tick_until(world, new, lambda: run_ends(world))
    end = run_ends(world)[0]
    assert end["interrupted"] and end["cost_usd"] is None and end["cost_estimated"]
    assert S.read_state(world.cfg, world.cid)["state"] == "IMPLEMENTING"
    assert tick_until(world, new, lambda: len(S.read_ledger(
        world.cfg, world.cid, types=["run.start"])) == 2)          # the step is respawned
    assert [e["persona"] for e in S.read_ledger(world.cfg, world.cid, types=["run.start"])] \
        == ["implementer", "implementer"]


def test_orphan_claim_after_grace(world, monkeypatch):
    run_dir, at = _orphan_world(world, monkeypatch, launch_json=True)
    at(5)
    new = Q.StepPool()
    world.pools.append(new)
    Q.tick(world.cfg, runner=world.runner, pool=new)
    assert adopt_journal(world)[-1]["runs"][0]["status"] == "running"
    time.sleep(0.6)
    assert not (run_dir / "proc.json").exists()                # 5 s: not claimed
    at(11)
    assert until((run_dir / "proc.json").exists)
    _orphan_claimed_and_respawned(world, run_dir, new)


def test_orphan_claim_without_launch_json_uses_run_start_ts(world, monkeypatch):
    run_dir, at = _orphan_world(world, monkeypatch, launch_json=False)
    at(5)
    new = Q.StepPool()
    world.pools.append(new)
    Q.tick(world.cfg, runner=world.runner, pool=new)
    assert adopt_journal(world)[-1]["runs"][0]["status"] == "running"
    time.sleep(0.6)
    assert not (run_dir / "proc.json").exists()                # 5 s after run.start: not claimed
    at(11)
    assert until((run_dir / "proc.json").exists)
    _orphan_claimed_and_respawned(world, run_dir, new)


def test_legacy_survivor_not_killed(world, monkeypatch):
    pool, run_dir = start_impl(world)
    pid = int((Path(S.read_state(world.cfg, world.cid)["repo"]["worktree"]) / "child.pid").read_text())
    die(pool)
    for name in ("proc.json", "launch.json", "child.json", "request.json"):
        (run_dir / name).unlink()
    ledger = S.runs_dir(world.cfg, world.cid).parent / "ledger.jsonl"
    lines = [json.loads(x) for x in ledger.read_text().splitlines()]
    for rec in lines:
        if rec["type"] == "run.start":
            rec.pop("step"), rec.pop("state")
    ledger.write_text("".join(json.dumps(r) + "\n" for r in lines))
    real, offset = time.time, {"s": 0.0}
    monkeypatch.setattr(time, "time", lambda: real() + offset["s"])
    new = Q.StepPool()
    world.pools.append(new)
    for _ in range(3):
        Q.tick(world.cfg, runner=world.runner, pool=new)
        time.sleep(0.2)
    assert adopt_journal(world)[-1]["runs"][0]["status"] == "legacy"
    assert pid_alive(pid) and not run_ends(world)
    assert len(S.read_ledger(world.cfg, world.cid, types=["run.start"])) == 1
    offset["s"] = 3600 + 61
    assert tick_until(world, new, lambda: run_ends(world))
    end = run_ends(world)[0]
    assert end["is_error"] and "legacy orphan" in (end["result_tail"] or "")
    assert S.read_state(world.cfg, world.cid)["state"] == "IMPLEMENTING"
    assert pid_alive(pid)                                      # never killed


def test_adopt_unknown_persona_collected_with_note(world):
    pool, run_dir = start_impl(world)
    die(pool)
    ledger = S.runs_dir(world.cfg, world.cid).parent / "ledger.jsonl"
    recs = [json.loads(x) for x in ledger.read_text().splitlines()]
    for rec in recs:
        if rec["type"] == "run.start":
            rec["persona"], rec["run_id"] = "resolver", rec["run_id"]
            rec.pop("step")
    ledger.write_text("".join(json.dumps(r) + "\n" for r in recs))
    world.release()
    assert until((run_dir / "exit.json").exists)
    Q.tick(world.cfg, runner=world.runner)
    assert adopt_journal(world)[-1]["runs"][0]["status"] == "unknown"
    assert len(run_ends(world, run_dir.name)) == 1
    notes = S.read_ledger(world.cfg, world.cid, types=["note"])
    assert any("unregistered persona resolver" in n["text"] for n in notes)
    assert not {"commit", "transition"} & set(applied_after_end(world, run_dir.name))


def test_stop_detaches_runs_instead_of_killing(world):
    """SPEC-0229: the interim kill-on-stop is gone; a stop leaves the run running."""
    pool, run_dir = start_impl(world)
    stop = pool.flights[world.cid].ctx.stop
    pid = child_pid(type("r", (), {"cwd": Path(S.read_state(world.cfg, world.cid)["repo"]["worktree"])}))
    stop.set()
    lease = L.acquire(world.cfg, ["wt", "queue", "run"])
    try:
        report = Q._drain(world.cfg, pool, lease, reason="signal", stop=stop)
    finally:
        lease.release()
    assert not pool.flights and pid_alive(pid)
    assert report.detached == [{"campaign": world.cid, "run_ids": ["001-implementer"]}]
    assert not run_ends(world)


def test_adopt_orphan_group_finishing_on_its_own(world):
    """The wrapper died and `claude` then completes: adopted, synthesized, applied once."""
    pool, run_dir = start_impl(world)
    die(pool)
    os.kill(RP.read_json(run_dir / "proc.json")["wrapper_pid"], 9)
    assert until(lambda: RP.run_status(run_dir) == "orphan_group")
    new = Q.StepPool()
    world.pools.append(new)
    Q.tick(world.cfg, runner=world.runner, pool=new)
    assert adopt_journal(world)[-1]["runs"][0]["status"] == "orphan_group"
    time.sleep(0.5)
    world.release()
    assert tick_until(world, new, lambda: run_ends(world), n=150)
    ex = json.loads((run_dir / "exit.json").read_text())
    assert ex["synthesized"] and ex["exit_code"] == 0 and ex["killed_by"] is None
    end, = run_ends(world)
    assert not end["is_error"] and not end["interrupted"]
    assert S.read_state(world.cfg, world.cid)["state"] == "GATING"


def test_adopt_closes_stale_open_runs(world):
    """A dangling older run is classified, listed in `adopt` and closed with no transition; only
    the latest review round forms the group."""
    pool, run_dir = start_impl(world)
    die(pool)
    world.release()
    assert until((run_dir / "exit.json").exists)
    import shutil
    start, = S.read_ledger(world.cfg, world.cid, types=["run.start"])
    stale_id, latest_id = start["run_id"], "002-implementer"     # an older crash-left run, then a newer
    shutil.copytree(run_dir, S.runs_dir(world.cfg, world.cid) / latest_id)
    S.append_event(world.cfg, world.cid, "run.start", {
        **{k: v for k, v in start.items() if k not in ("seq", "ts", "type", "run_id")},
        "run_id": latest_id})
    new = Q.StepPool()
    world.pools.append(new)
    Q.tick(world.cfg, runner=world.runner, pool=new)
    rows = {r["run_id"]: r["status"] for r in adopt_journal(world)[-1]["runs"]}
    assert rows == {stale_id: "stale_finished", latest_id: "finished"}
    assert stale_id in {e["run_id"] for e in run_ends(world)}
    assert tick_until(world, new, lambda: {e["run_id"] for e in run_ends(world)} == {stale_id, latest_id})
    assert S.read_state(world.cfg, world.cid)["state"] == "GATING"


def test_run_finishing_while_stopping_is_not_interrupted(world):
    pool, run_dir = start_impl(world)
    world.release()
    assert until((run_dir / "exit.json").exists)               # exited normally, then stop
    pool.flights[world.cid].ctx.stop.set()
    lease = L.acquire(world.cfg, ["wt", "queue", "run"])
    try:
        Q._drain(world.cfg, pool, lease, reason="signal")
    finally:
        lease.release()
    end, = run_ends(world)
    assert not end["interrupted"]
    assert json.loads((run_dir / "exit.json").read_text())["killed_by"] is None
    commit, = S.read_ledger(world.cfg, world.cid, types=["commit"])     # the work is committed
    wt = Path(S.read_state(world.cfg, world.cid)["repo"]["worktree"])
    assert commit["sha"] and commit["sha"] == _git(wt, "rev-parse", "HEAD")


def test_try_detach_all_phases():
    pool = Q.StepPool()
    from concurrent.futures import Future
    fl = Q._Flight({"id": "C"}, None)
    fut = Future()
    hooks = pool.hooks()
    fl.tasks.append((Q.Step("implement"), fut, hooks))
    fl.runs.append(("001-implementer", 60))
    pool.flights["C"] = fl
    assert pool.try_detach_all() is None                       # spawning
    hooks.spawned()
    hooks.phase = "collect"
    assert pool.try_detach_all() is None                       # collect
    hooks.phase = "wait"
    fl2 = Q._Flight({"id": "D"}, None)
    fl2.tasks.append((Q.Step("gate"), Future(), None))
    pool.flights["D"] = fl2
    assert pool.try_detach_all() is None                       # an in-process gate
    del pool.flights["D"]
    assert pool.try_detach_all() == [{"campaign": "C", "run_ids": ["001-implementer"]}]
    assert hooks.detached() and not pool.flights
    assert hooks.enter_collect() is False                      # atomic with the detach


def _code_world(world, monkeypatch):
    root = _code_repo(world.tmp)
    state = {"changed": False}
    real = Q.code_changes
    monkeypatch.setattr(Q, "code_changes", lambda r, since: (
        ("newsha", ["src/wt/x.py"]) if state["changed"] else real(r, since)))
    return root, state


def test_reexec_does_not_wait_for_persona_run(world, monkeypatch):
    root, state = _code_world(world, monkeypatch)
    seen = {}

    def fake_exec(exe, argv, env):
        wt = Path(S.read_state(world.cfg, world.cid)["repo"]["worktree"])
        seen["alive"] = pid_alive(int((wt / "child.pid").read_text()))
        seen["threads"] = [t.name for t in threading.enumerate() if t.name.startswith("wt-step")
                           and t.is_alive()]
        seen["env"] = env

    monkeypatch.setattr(Q, "_EXEC", fake_exec)

    def on_tick(report):
        wt = Path(S.read_state(world.cfg, world.cid)["repo"]["worktree"])
        if (wt / "child.pid").exists() and RP.read_json(
                S.runs_dir(world.cfg, world.cid) / "001-implementer" / "child.json"):
            state["changed"] = True

    t0 = time.monotonic()
    Q.run(world.cfg, poll_interval_s=0.05, reexec=True, code_root=root, on_tick=on_tick,
          runner=world.runner, max_ticks=200)
    assert time.monotonic() - t0 < 30
    assert seen["alive"] and seen["threads"] == []
    stop = [r for r in Q.read_journal(world.cfg) if r["kind"] == "stop"][-1]
    assert stop["reason"] == "code_changed"
    assert stop["detached"] == [{"campaign": world.cid, "run_ids": ["001-implementer"]}]
    # the exec'd process: takes over the lease, adopts the run, applies it once
    os.environ[L.HANDOVER_ENV] = seen["env"][L.HANDOVER_ENV]
    n = {"ticks": 0}

    def second(report):
        n["ticks"] += 1
        if n["ticks"] == 1:
            world.release()

    state["changed"] = False
    Q.run(world.cfg, poll_interval_s=0.05, runner=world.runner, on_tick=second, max_ticks=40)
    assert adopt_journal(world)[0]["runs"][0]["status"] == "running"
    assert [e["run_id"] for e in run_ends(world)].count("001-implementer") == 1
    assert S.read_state(world.cfg, world.cid)["state"] != "IMPLEMENTING"


def test_stop_after_detach_leaves_runs(world, monkeypatch):
    root, state = _code_world(world, monkeypatch)
    monkeypatch.setattr(Q, "_EXEC", lambda *a: pytest.fail("exec despite a stop"))
    real_detach = Q.StepPool.try_detach_all
    holder = {}

    def detach(self):
        out = real_detach(self)
        if out is not None:
            holder["stop"].set()                   # the signal arrives before the exec
        return out

    monkeypatch.setattr(Q.StepPool, "try_detach_all", detach)
    real_tick = Q.tick

    def tick(cfg, *, stop=None, **kw):
        holder["stop"] = stop
        return real_tick(cfg, stop=stop, **kw)

    monkeypatch.setattr(Q, "tick", tick)

    def on_tick(report):
        if RP.read_json(S.runs_dir(world.cfg, world.cid) / "001-implementer" / "child.json"):
            state["changed"] = True

    Q.run(world.cfg, poll_interval_s=0.05, reexec=True, code_root=root, on_tick=on_tick,
          runner=world.runner, max_ticks=200)
    stop = [r for r in Q.read_journal(world.cfg) if r["kind"] == "stop"][-1]
    assert stop["reason"] == "signal" and stop["detached"][0]["campaign"] == world.cid
    wt = Path(S.read_state(world.cfg, world.cid)["repo"]["worktree"])
    assert pid_alive(int((wt / "child.pid").read_text()))      # left running, adoptable
    assert not run_ends(world)


def test_adopt_vanished_run(world):
    pool, run_dir = start_impl(world)
    die(pool)
    proc, child = RP.read_json(run_dir / "proc.json"), RP.read_json(run_dir / "child.json")
    os.kill(proc["wrapper_pid"], 9)
    os.killpg(child["pgid"], 9)
    assert until(lambda: RP.run_status(run_dir) == "vanished")
    Q.tick(world.cfg, runner=world.runner)
    assert adopt_journal(world)[-1]["runs"][0]["status"] == "vanished"
    end = [e for e in run_ends(world) if e["run_id"] == run_dir.name][0]
    assert end["exit_code"] == -1 and end["is_error"] and end["cost_usd"] is None


def test_unrecorded_commit_only_for_adopted(tmp_path):
    from types import SimpleNamespace
    from test_agentic_e2e import _git, make_repo
    repo = make_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    (repo / "work.txt").write_text("done by the agent\n")        # the agent committed by itself
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "agent commit")
    new_head = _git(repo, "rev-parse", "HEAD")
    result = R.RunResult(exit_code=0, timed_out=False, interrupted=False, duration_s=1.0,
                         is_error=False, cost_usd=0.1, verdict={"summary": "x"}, verdict_ok=True)

    class Runner:
        detachable = True

        def run(self, req, *, hooks=None):
            return result

    ctx = SimpleNamespace(runner=Runner(), stop=threading.Event(),
                          project=lambda st: {"agents": {}})
    state = {"id": "C", "state": "IMPLEMENTING", "repo": {"worktree": str(repo), "head": head}}
    step = Q.Step("implement", persona="implementer")
    fresh = Q._do_persona(ctx, state, step, "001-implementer", None, 4.0)
    assert fresh.commit_sha is None                              # fresh runs: today's behaviour
    late = Q._do_persona(ctx, state, step, "001-implementer", None, 4.0,
                         adopted={"exited_before_start": False})
    assert late.commit_sha is None
    adopted = Q._do_persona(ctx, state, step, "001-implementer", None, 4.0,
                            adopted={"exited_before_start": True})
    assert adopted.commit_sha == new_head
    state["repo"]["head"] = new_head                             # recorded once: no second one
    again = Q._do_persona(ctx, state, step, "001-implementer", None, 4.0,
                          adopted={"exited_before_start": True})
    assert again.commit_sha is None


def test_collect_race_delays_exec(world, monkeypatch):
    root, state = _code_world(world, monkeypatch)
    gate, entered, calls = threading.Event(), threading.Event(), []
    real_commit = Q.git.commit_all

    def slow_commit(*a, **k):
        entered.set()
        assert gate.wait(30)
        return real_commit(*a, **k)

    monkeypatch.setattr(Q.git, "commit_all", slow_commit)
    monkeypatch.setattr(Q, "_EXEC", lambda exe, argv, env: calls.append(entered.is_set()
                                                                          and gate.is_set()))
    armed = {"timer": False}

    def on_tick(report):
        if RP.read_json(S.runs_dir(world.cfg, world.cid) / "001-implementer" / "child.json"):
            world.release()                                       # the run exits ...
        if entered.is_set():                                      # ... the commit is in flight
            state["changed"] = True                               # ... and the code changes
            if not armed["timer"]:
                armed["timer"] = True
                threading.Timer(1.0, gate.set).start()

    Q.run(world.cfg, poll_interval_s=0.05, reexec=True, code_root=root, on_tick=on_tick,
          runner=world.runner, max_ticks=200)
    assert calls == [True]                                        # exec only after the commit
    commits = S.read_ledger(world.cfg, world.cid, types=["commit"])
    assert len(commits) == 1
    stop = [r for r in Q.read_journal(world.cfg) if r["kind"] == "stop"][-1]
    assert stop["reason"] == "code_changed" and not stop.get("detached")


def test_adopt_review_group_applied_once(tmp_path, monkeypatch, world):
    """Two reviewers detached together are adopted and applied as one group (one transition)."""
    from test_agentic_e2e import review
    cfg = world.cfg
    cfg["agentic"]["profiles"]["balanced"]["reviewers"] = ["conformance_reviewer", "test_reviewer"]
    pool, run_dir = start_impl(world)
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP_S", "0")
    world.release()
    assert tick_until(world, pool, lambda: S.read_state(cfg, world.cid)["state"] == "GATING")
    monkeypatch.setenv("FAKE_CLAUDE_VERDICT", json.dumps(review()["verdict"]))
    monkeypatch.setenv("FAKE_CLAUDE_EDITS", "[]")
    assert tick_until(world, pool, lambda: S.read_state(cfg, world.cid)["state"] == "REVIEWING")
    (world.tmp / "release").unlink()
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP_S", "60")
    assert tick_until(world, pool, lambda: pool.flights.get(world.cid) is not None
                      and len(pool.flights[world.cid].tasks) == 2)
    starts = [e for e in S.read_ledger(cfg, world.cid, types=["run.start"])
              if e["persona"] in PS.REVIEWER_PERSONAS]
    assert [e["step"]["personas"] for e in starts] == [["conformance_reviewer", "test_reviewer"]] * 2
    rdirs = [S.runs_dir(cfg, world.cid) / e["run_id"] for e in starts]
    assert until(lambda: all(RP.read_json(d / "child.json") for d in rdirs))
    die(pool)
    world.release()
    assert until(lambda: all((d / "exit.json").exists() for d in rdirs))
    report = Q.tick(cfg, runner=world.runner)
    adopted = adopt_journal(world)[-1]["runs"]
    assert sorted(r["persona"] for r in adopted) == ["conformance_reviewer", "test_reviewer"]
    # each adopted run ends once. The tick may apply the group before its planning pass and then
    # start a fresh step at once (here a test_reviewer retry: the shared fake verdict does not fit
    # its schema), so only the adopted runs are counted, not every reviewer run.end.
    ids = {e["run_id"] for e in starts}
    ends = [e for e in run_ends(world) if e["run_id"] in ids]
    assert len(ends) == 2 and len({e["run_id"] for e in ends}) == 2
    # one group, one applied step (the reviewers' verdicts are routed together)
    rows = [r for r in report.steps if r["campaign"] == world.cid and r["action"] == "review"
            and r.get("run_id") in ids]
    assert len(rows) == 1


def test_adopt_honours_park_request(world):
    pool, run_dir = start_impl(world)
    lease = L.acquire(world.cfg, ["wt", "queue", "run"])         # a loop holds the lease: deferred
    try:
        plan = Q.plan_park(world.cfg, world.cid, "pause")
        assert plan["mode"] == "deferred"
        Q.apply_park(world.cfg, plan)
    finally:
        lease.release()
    die(pool)
    world.release()
    assert until((run_dir / "exit.json").exists)
    Q.tick(world.cfg, runner=world.runner)
    st = S.read_state(world.cfg, world.cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "human"
    assert len(run_ends(world)) == 1


def test_adopt_classification_triager_and_legacy_reviewers():
    # recorded step wins; the registry covers legacy events; the triager is its own group
    assert Q._adopt_class({"persona": "review_triager"}) == ("review", ("review_triager",))
    assert Q._adopt_class({"persona": "review_triager",
                           "step": {"kind": "review", "personas": ["review_triager"]}}) \
        == ("review", ("review_triager",))
    assert Q._adopt_class({"persona": "test_reviewer"}) == ("review", None)
    assert Q._adopt_class({"persona": "implementer", "step": {"kind": "implement",
                                                              "personas": []}}) == ("implement", None)
    assert Q._adopt_class({"persona": "gate_triager"}) == ("triage", None)
    assert Q._adopt_class({"persona": "resolver"}) is None


def test_adopt_classification_recorded_step_beats_registry():
    # the registry says implement / reviewer group; the recorded step says otherwise
    assert Q._adopt_class({"persona": "implementer", "step": {
        "kind": "review", "personas": ["a_reviewer", "b_reviewer"]}}) \
        == ("review", ("a_reviewer", "b_reviewer"))
    assert Q._adopt_class({"persona": "test_reviewer", "step": {
        "kind": "review", "personas": ["test_reviewer", "conformance_reviewer"]}}) \
        == ("review", ("test_reviewer", "conformance_reviewer"))
    assert Q._adopt_class({"persona": "resolver", "step": {"kind": "triage", "personas": []}}) \
        == ("triage", None)                                    # unregistered, but recorded


def test_adopt_uses_recorded_step_end_to_end(world):
    """A persona the registry does not know, with a recorded `implement` step, is applied as
    the implementer's step, not parked as unknown."""
    pool, run_dir = start_impl(world)
    die(pool)
    ledger = S.runs_dir(world.cfg, world.cid).parent / "ledger.jsonl"
    recs = [json.loads(x) for x in ledger.read_text().splitlines()]
    for rec in recs:
        if rec["type"] == "run.start":
            rec["persona"] = "resolver"                        # `step` stays: kind implement
    ledger.write_text("".join(json.dumps(r) + "\n" for r in recs))
    world.release()
    assert until((run_dir / "exit.json").exists)
    Q.tick(world.cfg, runner=world.runner)
    assert adopt_journal(world)[-1]["runs"][0]["status"] == "finished"
    assert len(run_ends(world)) == 1
    assert S.read_state(world.cfg, world.cid)["state"] != "IMPLEMENTING"
    assert not any("unregistered persona" in n["text"]
                   for n in S.read_ledger(world.cfg, world.cid, types=["note"]))


def test_adopt_review_triager_is_its_own_group(world, monkeypatch):
    from test_agentic_e2e import review
    cfg = world.cfg
    cfg["agentic"]["profiles"]["balanced"]["review"] = {
        "pass_score": 80, "triager": True,
        "weights": {"must_fix": 100, "major": 15, "minor": 4, "nit": 1}}
    pool, run_dir = start_impl(world)
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP_S", "0")
    world.release()
    assert tick_until(world, pool, lambda: S.read_state(cfg, world.cid)["state"] == "GATING")
    minor = [{"severity": "minor", "title": f"minor {i}", "detail": "d", "suggested_fix": "f", "files": []}
             for i in range(6)]
    verdict = review("pass", findings=minor)["verdict"]
    monkeypatch.setenv("FAKE_CLAUDE_VERDICT", json.dumps(verdict))
    monkeypatch.setenv("FAKE_CLAUDE_EDITS", "[]")
    assert tick_until(world, pool, lambda: S.read_state(cfg, world.cid)["state"] == "REVIEWING")
    assert tick_until(world, pool, lambda: any(
        e["persona"] == "conformance_reviewer" for e in run_ends(world)))
    (world.tmp / "release").unlink()
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP_S", "60")
    monkeypatch.setenv("FAKE_CLAUDE_VERDICT", json.dumps({"waived": [], "kept": []}))
    assert tick_until(world, pool, lambda: any(
        e["persona"] == "review_triager" for e in S.read_ledger(cfg, world.cid, types=["run.start"])))
    start = [e for e in S.read_ledger(cfg, world.cid, types=["run.start"])
             if e["persona"] == "review_triager"][0]
    assert start["step"] == {"kind": "review", "personas": ["review_triager"]}
    trd = S.runs_dir(cfg, world.cid) / start["run_id"]
    assert until(lambda: RP.read_json(trd / "child.json"))
    die(pool)
    world.release()
    assert until((trd / "exit.json").exists)
    report = Q.tick(cfg, runner=world.runner)
    assert adopt_journal(world)[-1]["runs"] == [{"campaign": world.cid, "run_id": start["run_id"],
                                                 "persona": "review_triager", "status": "finished"}]
    assert any(e["run_id"] == start["run_id"] for e in run_ends(world))
    assert [r for r in report.steps if r["campaign"] == world.cid and r["action"] == "review"]


def test_runner_killed_after_popen_is_adopted_running(world, monkeypatch):
    """The runner 'dies' right after `Popen` (before it ever looks at `proc.json`): the wrapper
    still claims the run dir, so the next runner finds a running run, never an orphan."""
    def dead(self, req, wrapper, started):
        raise KeyboardInterrupt("runner died")

    monkeypatch.setattr(R.ClaudeRunner, "_await_claim", dead)
    pool = Q.StepPool()
    world.pools.append(pool)
    for _ in range(3):
        Q.tick(world.cfg, runner=world.runner, pool=pool)
        if pool.flights:
            break
    run_dir = S.runs_dir(world.cfg, world.cid) / "001-implementer"
    assert until(lambda: RP.read_json(run_dir / "child.json"))
    die(pool)
    monkeypatch.undo()
    new = Q.StepPool()
    world.pools.append(new)
    Q.tick(world.cfg, runner=world.runner, pool=new)
    assert adopt_journal(world)[-1]["runs"][0]["status"] == "running"
    assert "orphaned" not in (run_dir / "proc.json").read_text()


# ---- fan-out detach, finished members, orphan_group, unrecorded commit ------------------------

def reviewing_flight(world, monkeypatch, by_argv, prep=None):
    """Drive the campaign to a two-reviewer fan-out in flight; returns (pool, {persona: run_dir}).
    `by_argv` is the fake claude's per-reviewer env override; `prep` runs just before the
    reviewers are spawned."""
    cfg = world.cfg
    cfg["agentic"]["profiles"]["balanced"]["reviewers"] = ["conformance_reviewer", "test_reviewer"]
    pool, _ = start_impl(world)
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP_S", "0")
    world.release()
    assert tick_until(world, pool, lambda: S.read_state(cfg, world.cid)["state"] == "GATING")
    (world.tmp / "release").unlink()
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP_S", "60")
    monkeypatch.setenv("FAKE_CLAUDE_EDITS", "[]")
    monkeypatch.setenv("FAKE_CLAUDE_BY_ARGV", json.dumps(by_argv))
    if prep:
        prep()
    assert tick_until(world, pool, lambda: pool.flights.get(world.cid) is not None
                      and len(pool.flights[world.cid].tasks) == 2)
    starts = [e for e in S.read_ledger(cfg, world.cid, types=["run.start"])
              if e["persona"] in PS.REVIEWER_PERSONAS]
    return pool, {e["persona"]: S.runs_dir(cfg, world.cid) / e["run_id"] for e in starts}


def both_fix():
    """conformance exits at once (must_fix); the test reviewer waits for the release."""
    return [["ac_assessment", {"FAKE_CLAUDE_SLEEP_S": "0",
                               "FAKE_CLAUDE_VERDICT": json.dumps(review("fix")["verdict"])}],
            ["coverage_assessment", {"FAKE_CLAUDE_VERDICT": json.dumps(tr_review("fix")["verdict"])}]]


def _group_flight(phases):
    from concurrent.futures import Future
    pool, fl, hooks, futs = Q.StepPool(), Q._Flight({"id": "C"}, None), [], []
    for i, phase in enumerate(phases):
        hk, fut = pool.hooks(phase="wait"), Future()
        hk.phase = phase
        fl.tasks.append((Q.Step("review", persona=f"r{i}", personas=("r0", "r1")), fut, hk))
        fl.runs.append((f"00{i}-r{i}", 60))
        hooks.append(hk), futs.append(fut)
    pool.flights["C"] = fl
    return pool, hooks, futs


def test_fanout_detach_precondition_phases():
    # whichever task is held back (first or second), nothing detaches
    for held in ("spawning", "collect"):
        for order in ((held, "wait"), ("wait", held)):
            pool, hooks, _ = _group_flight(order)
            assert pool.try_detach_all() is None and "C" in pool.flights
            assert not any(h.detached() for h in hooks)
    pool, hooks, futs = _group_flight(("wait", "collect"))
    hooks[1].phase = "wait"                                    # it reached `wait`: both go
    assert pool.try_detach_all() == [{"campaign": "C", "run_ids": ["000-r0", "001-r1"]}]
    assert all(h.detached() for h in hooks) and not pool.flights
    # a finished reviewer next to a waiting one: both run ids (adoption re-collects the first)
    pool, hooks, futs = _group_flight(("done", "wait"))
    futs[0].set_result(None)
    assert pool.try_detach_all() == [{"campaign": "C", "run_ids": ["000-r0", "001-r1"]}]
    assert hooks[1].detached() and not hooks[0].detached()
    # a flight whose tasks are all done is harvested, never detached
    pool, hooks, futs = _group_flight(("done", "done"))
    for f in futs:
        f.set_result(None)
    assert pool.try_detach_all() is None and "C" in pool.flights
    pool, hooks, futs = _group_flight(("wait",))
    futs[0].set_result(None)                                   # an unharvested implementer
    assert pool.try_detach_all() is None


def test_fanout_detach_precondition(world, monkeypatch):
    """Two reviewers, one wrapper start paused: the fan-out stays attached until it claims."""
    go, real_popen = world.tmp / "go", subprocess.Popen
    code = ("import os, sys, time, runpy\nwhile not os.path.exists(sys.argv[1]): time.sleep(0.05)\n"
            "sys.argv = ['x', sys.argv[2]]\n"
            "runpy.run_module('wt.agentic.runproc', run_name='__main__')")

    def paused(argv, **kw):
        if "wt.agentic.runproc" in argv and "test_reviewer" in argv[-1]:
            argv = [sys.executable, "-c", code, str(go), argv[-1]]
        return real_popen(argv, **kw)

    pool, rdirs = reviewing_flight(world, monkeypatch, both_fix(), prep=lambda: monkeypatch.setattr(
        R.subprocess, "Popen", paused))
    (_, _, h_conf), (_, _, h_test) = pool.flights[world.cid].tasks
    assert until(lambda: h_conf.phase == "wait")
    assert h_test.phase == "spawning"
    assert pool.try_detach_all() is None and world.cid in pool.flights
    go.write_text("")
    assert until(lambda: h_test.phase == "wait" and RP.read_json(rdirs["test_reviewer"] / "child.json"))
    assert pool.try_detach_all() == [{"campaign": world.cid, "run_ids": [
        rdirs[p].name for p in ("conformance_reviewer", "test_reviewer")]}]
    assert h_conf.detached() and h_test.detached() and not pool.flights


def test_adopt_review_group_with_finished_member(world, monkeypatch):
    """One reviewer exits before the detach (its task is done, never harvested) and one is
    still running: the new pool applies both, once, as one group."""
    pool, rdirs = reviewing_flight(world, monkeypatch, both_fix())
    (_, f_conf, _), (_, f_test, _) = pool.flights[world.cid].tasks
    assert until(lambda: f_conf.done() and (rdirs["conformance_reviewer"] / "run.json").exists())
    assert not f_test.done() and not (rdirs["test_reviewer"] / "exit.json").exists()
    detached = pool.try_detach_all()                           # the real detach, not detach_force
    assert detached == [{"campaign": world.cid, "run_ids": [
        rdirs[p].name for p in ("conformance_reviewer", "test_reviewer")]}]
    pool.shutdown()
    new, steps = Q.StepPool(), []
    world.pools.append(new)
    Q.tick(world.cfg, runner=world.runner, pool=new)
    adopted = {r["persona"]: r["status"] for r in adopt_journal(world)[-1]["runs"]}
    assert adopted == {"conformance_reviewer": "finished", "test_reviewer": "running"}
    assert not [e for e in run_ends(world) if e["persona"] in PS.REVIEWER_PERSONAS]
    world.release()
    assert tick_until(world, new, lambda: any(
        s["action"] == "review" for s in steps), steps=steps)
    ends = [e for e in run_ends(world) if e["persona"] in PS.REVIEWER_PERSONAS]
    assert sorted(e["persona"] for e in ends) == ["conformance_reviewer", "test_reviewer"]
    rows = [s for s in steps if s["campaign"] == world.cid and s["action"] == "review"]
    assert len(rows) == 1 and rows[0]["reason"] == "review_fix"      # both verdicts were valid
    fixes = [t for t in S.read_ledger(world.cfg, world.cid, types=["transition"])
             if t.get("reason") == "review_fix"]
    assert len(fixes) == 1
    brief = S.read_state(world.cfg, world.cid)["pending_brief"]
    assert sorted(brief["personas"]) == ["conformance_reviewer", "test_reviewer"]


def test_adopt_orphan_group_waited_and_killed(world):
    """The wrapper died, `claude` did not: adoption waits (no respawn), kills the group at the
    timeout, synthesizes `exit.json`, and records exactly one `run.end`."""
    pool, run_dir = start_impl(world)
    pid = int((Path(S.read_state(world.cfg, world.cid)["repo"]["worktree"]) / "child.pid").read_text())
    die(pool)
    os.kill(RP.read_json(run_dir / "proc.json")["wrapper_pid"], 9)
    assert until(lambda: RP.run_status(run_dir) == "orphan_group")
    req = json.loads((run_dir / "request.json").read_text())
    req["timeout_s"] = 3
    (run_dir / "request.json").write_text(json.dumps(req))
    new = Q.StepPool()
    world.pools.append(new)
    Q.tick(world.cfg, runner=world.runner, pool=new)
    assert adopt_journal(world)[-1]["runs"][0]["status"] == "orphan_group"
    for _ in range(4):                                           # the group lives: nothing happens
        Q.tick(world.cfg, runner=world.runner, pool=new)
        time.sleep(0.2)
    assert pid_alive(pid) and not run_ends(world)
    assert len(S.read_ledger(world.cfg, world.cid, types=["run.start"])) == 1
    assert tick_until(world, new, lambda: run_ends(world), n=150)
    ex = json.loads((run_dir / "exit.json").read_text())
    assert ex["synthesized"] and ex["killed_by"] == "timeout"
    assert not pid_alive(pid)
    assert len(run_ends(world)) == 1


@pytest.mark.parametrize("mode,recorded", [("earlier", True), ("later", False), ("handover", False)])
def test_adopt_exited_before_start_from_ended_at(world, monkeypatch, mode, recorded):
    """An unrecorded worktree commit is recorded only when the run's `exit.json.ended_at` is
    earlier than the lease `started_at`; a handover keeps the lease `started_at`."""
    import datetime as dt
    cfg = world.cfg
    lease = L.acquire(cfg, ["wt", "queue", "run"]) if mode == "handover" else None
    pool, run_dir = start_impl(world)
    die(pool)
    world.release()
    assert until((run_dir / "exit.json").exists)
    wt = Path(S.read_state(cfg, world.cid)["repo"]["worktree"])
    _git(wt, "add", "-A")                                        # the crash-during-collect case:
    _git(wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "agent commit")
    head = _git(wt, "rev-parse", "HEAD")
    if mode == "handover":
        monkeypatch.setenv(L.HANDOVER_ENV, lease.handover()[L.HANDOVER_ENV])
        started = L.read_lease(cfg)["started_at"]
        lease = L.acquire(cfg, ["wt", "queue", "run"])
        assert L.read_lease(cfg)["started_at"] == started
        assert dt.datetime.fromisoformat(json.loads(
            (run_dir / "exit.json").read_text())["ended_at"]) >= dt.datetime.fromisoformat(started)
    else:
        lease = L.acquire(cfg, ["wt", "queue", "run"])
        started = dt.datetime.fromisoformat(L.read_lease(cfg)["started_at"])
        ex = json.loads((run_dir / "exit.json").read_text())
        delta = dt.timedelta(seconds=-5 if mode == "earlier" else 5)
        ex["ended_at"] = (started + delta).isoformat(timespec="seconds")
        (run_dir / "exit.json").write_text(json.dumps(ex))
    try:
        Q.tick(cfg, runner=world.runner)
    finally:
        lease.release()
    commit, = S.read_ledger(cfg, world.cid, types=["commit"])
    assert commit["sha"] == (head if recorded else None)
    assert len(run_ends(world)) == 1


def test_try_detach_all_never_drops_a_finished_implementer(world, monkeypatch):
    """The implementer's task completes after `_harvest` built its list but before the detach:
    the flight is not detached, the next iteration harvests it, and its commit is recorded."""
    import concurrent.futures as cf
    pool, run_dir = start_impl(world)
    (_, fut, _), = pool.flights[world.cid].tasks
    real, calls = Q._harvest, []

    def late(p, report):
        real(p, report)
        if not calls:
            calls.append(1)
            world.release()
            cf.wait([fut], timeout=30)
            assert fut.done()

    monkeypatch.setattr(Q, "_harvest", late)
    lease = L.acquire(world.cfg, ["wt", "queue", "run"])
    try:
        report = Q._drain(world.cfg, pool, lease, reason="code_changed")
    finally:
        lease.release()
    assert not report.detached and not pool.flights
    commit, = S.read_ledger(world.cfg, world.cid, types=["commit"])
    assert commit["sha"]
    assert len(run_ends(world)) == 1
    assert S.read_state(world.cfg, world.cid)["repo"]["head"] == commit["sha"]
