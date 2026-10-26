"""SPEC-0225: detached persona runs, adopted across runner restarts. A real `ClaudeRunner` runs
the fake `claude` of tests/fixtures/fake_claude.py through the wrapper; a "dead runner" is a
StepPool that is discarded while its runs keep going."""
import json
import os
import threading
import time
from pathlib import Path

import pytest

from test_agentic_e2e import added, impl_done, make_cfg, make_repo, script
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


def run_ends(w):
    return S.read_ledger(w.cfg, w.cid, types=["run.end"])


def adopt_journal(w):
    return [r for r in Q.read_journal(w.cfg) if r["kind"] == "adopt"]


def tick_until(w, pool, cond, n=60):
    for _ in range(n):
        Q.tick(w.cfg, runner=w.runner, pool=pool)
        if cond():
            return True
        time.sleep(0.1)
    return False


def test_adopt_registry_complete():
    assert set(PS.PERSONAS) - {"spec_reviewer"} <= set(PS.ADOPT_REGISTRY)
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
    assert len(run_ends(world)) == 1
    assert S.read_state(world.cfg, world.cid)["state"] != "GATING"


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


def test_orphan_claim_after_grace(world, monkeypatch):
    pool, run_dir = start_impl(world)
    die(pool)
    world.release()
    assert until((run_dir / "exit.json").exists)
    for name in ("proc.json", "exit.json", "child.json"):      # an old runner's run: no claim
        (run_dir / name).unlink()
    (run_dir / "launch.json").write_text("{}")
    real, offset = time.time, {"s": 0.0}
    monkeypatch.setattr(time, "time", lambda: real() + offset["s"])
    os.utime(run_dir / "launch.json", (real() - 5, real() - 5))
    new = Q.StepPool()
    world.pools.append(new)
    Q.tick(world.cfg, runner=world.runner, pool=new)
    assert adopt_journal(world)[-1]["runs"][0]["status"] == "running"
    time.sleep(0.6)
    assert not (run_dir / "proc.json").exists()                # 5 s: not claimed
    offset["s"] = 6.0
    assert until((run_dir / "proc.json").exists)
    assert json.loads((run_dir / "proc.json").read_text())["by"] == "adopt"
    assert tick_until(world, new, lambda: run_ends(world))
    end = run_ends(world)[0]
    assert end["interrupted"] and end["cost_usd"] is None
    assert S.read_state(world.cfg, world.cid)["state"] in ("IMPLEMENTING",)


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
    assert end["is_error"] and "legacy orphan" in (end["result_tail"] or "") \
        or end["interrupted"]
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
    assert len(run_ends(world)) == 1
    notes = S.read_ledger(world.cfg, world.cid, types=["note"])
    assert any("unregistered persona resolver" in n["text"] for n in notes)
    assert S.read_state(world.cfg, world.cid)["state"] == "IMPLEMENTING"


def test_interim_stop_kills_runs(world):
    pool, run_dir = start_impl(world)
    stop = pool.flights[world.cid].ctx.stop
    pid = child_pid(type("r", (), {"cwd": Path(S.read_state(world.cfg, world.cid)["repo"]["worktree"])}))
    stop.set()
    lease = L.acquire(world.cfg, ["wt", "queue", "run"])
    try:
        t0 = time.monotonic()
        Q._drain(world.cfg, pool, lease, reason="signal")
    finally:
        lease.release()
    assert time.monotonic() - t0 < 20 and not pool.flights
    assert not pid_alive(pid)
    end, = run_ends(world)
    assert end["interrupted"]
    assert json.loads((run_dir / "exit.json").read_text())["killed_by"] == "signal"


def test_interim_stop_stubborn_child_is_killed(world, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_STUBBORN", "1")
    pool, run_dir = start_impl(world)
    pool.flights[world.cid].ctx.stop.set()
    lease = L.acquire(world.cfg, ["wt", "queue", "run"])
    try:
        Q._drain(world.cfg, pool, lease, reason="signal")
    finally:
        lease.release()
    ex = json.loads((run_dir / "exit.json").read_text())
    assert ex["killed_by"] == "signal" and ex["signal"] == 9


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
    ends = [e for e in run_ends(world) if e["persona"] in PS.REVIEWER_PERSONAS]
    assert len(ends) == 2 and len({e["run_id"] for e in ends}) == 2
    # one group, one applied step (the reviewers' verdicts are routed together)
    rows = [r for r in report.steps if r["campaign"] == world.cid and r["action"] == "review"]
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
    minor = [{"severity": "minor", "title": f"minor {i}", "detail": "d", "files": []}
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
