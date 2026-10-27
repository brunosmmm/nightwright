"""SPEC-0229: runner exit paths and draining ticks. Gates are real subprocesses that record
their pid and wait for a release file; stops are driven by setting the loop's `stop` event,
exactly what the signal handler does."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from test_agentic_adopt import world  # noqa: F401  (fixture: ClaudeRunner + fake claude)
from test_agentic_adopt import _code_world, start_impl, run_ends
from test_agentic_e2e import added, impl_done, make_cfg, make_repo, review, script
from test_agentic_nonblocking import _second_spec
from test_agentic_runproc import pid_alive, until
from waits import join
from wt.agentic import cli_queue
from wt.agentic import gates as G
from wt.agentic import lease as L
from wt.agentic import queue as Q
from wt.agentic import runner as R
from wt.agentic import runproc as RP
from wt.agentic import store as S


def gate_script(tmp, name="g1"):
    return (f"echo $$ > {tmp}/{name}.pid; while [ ! -f {tmp}/{name}.rel ]; do sleep 0.05; done; "
            "test -f feature.txt")


def gate_world(tmp_path, monkeypatch, entries=None, **kw):
    repo = make_repo(tmp_path, test_gate=gate_script(tmp_path),
                     lint_gate=f"touch {tmp_path}/g2.started", **kw)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, entries or [impl_done(), review()]))
    return repo, cfg, added(cfg, monkeypatch)


def pid_file(tmp, name="g1"):
    p = Path(tmp) / f"{name}.pid"
    return int(p.read_text()) if p.exists() and p.read_text().strip() else None


def run_with_stop(cfg, when, **kw):
    """Q.run; a side thread sets the loop's stop event once `when()` is true."""
    holder = {}
    real_tick = Q.tick

    def tick(cfg_, *, stop=None, **k):
        holder["stop"] = stop
        return real_tick(cfg_, stop=stop, **k)

    finished = threading.Event()

    def side():
        deadline = time.monotonic() + 30
        while not finished.is_set() and time.monotonic() < deadline:
            if "stop" in holder and when():
                holder["stop"].set()
                return
            time.sleep(0.02)

    t = threading.Thread(target=side, daemon=True)
    t.start()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Q, "tick", tick)
        t0 = time.monotonic()
        try:
            n = Q.run(cfg, poll_interval_s=0.05, **kw)
        finally:
            finished.set()
    join(t)
    return n, time.monotonic() - t0


def journal(cfg, kind):
    return [r for r in Q.read_journal(cfg) if r["kind"] == kind]


def test_stop_kills_gate_discards_it_and_next_run_regates(tmp_path, monkeypatch):
    repo, cfg, a = gate_world(tmp_path, monkeypatch)
    n, took = run_with_stop(cfg, lambda: pid_file(tmp_path) is not None)
    pid = pid_file(tmp_path)
    assert not pid_alive(pid) and took < 20
    assert not (tmp_path / "g2.started").exists()            # the second gate never started
    st = S.read_state(cfg, a)
    assert st["state"] == "GATING" and st["counters"]["gate_runs"] == 0
    assert not S.read_ledger(cfg, a, types=["gate"])
    assert st["counters"]["loops"] == 0
    stop, = journal(cfg, "stop")
    assert stop["reason"] == "signal" and stop["detached"] == []
    side = json.loads((S.gates_dir(cfg, a) / "001-test.json").read_text())
    assert side["interrupted"] is True
    # the next runner (same process: `_stopping` was reset) re-gates with the next seq
    (tmp_path / "g1.rel").write_text("")
    Q.run(cfg, until_idle=True, poll_interval_s=0.05)
    assert S.read_state(cfg, a)["state"] == "MERGED"
    gates = S.read_ledger(cfg, a, types=["gate"])
    assert gates[0]["gate_seq"] == 2                          # the close re-gate is seq 3
    assert not json.loads((S.gates_dir(cfg, a) / "002-test.json").read_text()).get("interrupted")


def test_gate_registry_unit(tmp_path):
    G.reset()
    G.terminate_all()
    gate = {"name": "x", "cmd": "true", "timeout_s": 5}
    res = G.run_gate(gate, tmp_path, {}, tmp_path / "x.log", {})
    assert res.interrupted and not res.ok and res.exit_code is None
    assert json.loads((tmp_path / "x.json").read_text())["interrupted"] is True
    G.reset()
    assert G.run_gate(gate, tmp_path, {}, tmp_path / "y.log", {}).ok
    ev = threading.Event()
    ev.set()
    run = G.run_all({"gates": [gate, gate]}, {"id": "c", "repo": {"worktree": str(tmp_path)}},
                    {"data_dir": str(tmp_path / "d")}, seq=1, stop=ev)
    assert run.interrupted and run.results == ()
    assert "interrupted" not in G.as_payload(G.GateRun(1, True, (res.__class__(
        "x", True, True, 0, False, 0.0, "l", ""),), "t", 0.0))["results"][0]


def test_handler_only_sets_stop(tmp_path, monkeypatch):
    repo, cfg, a = gate_world(tmp_path, monkeypatch)
    calls, handlers = [], {}
    real_terminate = G.terminate_all
    monkeypatch.setattr(G, "terminate_all", lambda: (calls.append(time.monotonic()),
                                                     real_terminate()))
    real_signal = signal.signal
    monkeypatch.setattr(signal, "signal", lambda s, h: (handlers.__setitem__(s, h),
                                                        real_signal(s, h))[1])
    seen = {}

    def when():
        if pid_file(tmp_path) is None or "t" in seen:
            return False
        seen["t"] = time.monotonic()
        handlers[signal.SIGTERM](signal.SIGTERM, None)       # a signal with a gate in flight
        seen["returned"] = time.monotonic()
        seen["calls_at_return"] = len(calls)
        return False

    run_with_stop(cfg, when, max_ticks=400)
    assert seen["returned"] - seen["t"] < 0.2 and seen["calls_at_return"] == 0
    assert calls and calls[0] - seen["t"] < 0.5


def test_fakerunner_drain_converges_and_admits(tmp_path, monkeypatch):
    state = {"changed": False}
    repo, cfg, a = gate_world(tmp_path, monkeypatch)
    _second_spec(repo)
    cfg["agentic"]["profiles"]["balanced"]["slots"] = 2
    b = None
    monkeypatch.setattr(Q, "code_changes", lambda r, since: (
        ("newsha", ["src/wt/x.py"]) if state["changed"] else (since, [])))
    calls = []
    monkeypatch.setattr(Q, "_EXEC", lambda exe, argv, env: calls.append(time.monotonic()))
    seen = {}

    def on_tick(report):
        # A is GATING with the held gate: the change lands, B is admitted by a draining tick
        nonlocal b
        if pid_file(tmp_path) and not state["changed"]:
            state["changed"] = True
            seen["gate_pid"] = pid_file(tmp_path)
            b = added(cfg, monkeypatch, "SPEC-0002")
        if b and S.read_state(cfg, b)["state"] != "QUEUED":
            seen["n"] = seen.get("n", 0) + 1
            seen.setdefault("b_state", S.read_state(cfg, b)["state"])
            if seen["n"] >= 3 and "rel" not in seen:      # a few draining ticks went by first
                seen["rel"] = time.monotonic()
                (tmp_path / "g1.rel").write_text("")

    Q.run(cfg, poll_interval_s=0.2, reexec=True, code_root=repo, on_tick=on_tick,
          runner=None, max_ticks=100)
    assert calls and seen["b_state"] in ("IMPLEMENTING", "SPEC_WRITING", "OPENING")
    # FakeRunner is not detachable: B's implementer was never planned while draining
    assert S.read_state(cfg, b)["state"] == "IMPLEMENTING"
    assert not S.read_ledger(cfg, b, types=["run.start"])
    assert len(S.read_ledger(cfg, a, types=["gate"])) == 1       # no gate started after detection
    stop, = journal(cfg, "stop")
    assert stop["reason"] == "code_changed" and "detached" in stop
    assert journal(cfg, "draining")
    assert calls[0] - seen["rel"] < 5


class Det:
    """A detachable stand-in around FakeRunner: its runs sit in `wait` until released."""
    detachable = True

    def __init__(self, cfg):
        self.inner = R.select_runner(cfg)
        self.release = threading.Event()

    def run(self, req, *, hooks=None):
        if hooks:
            hooks.spawned()
        while not self.release.is_set():
            if hooks and hooks.detached():
                return R.DETACHED
            time.sleep(0.05)
        if hooks and not hooks.enter_collect():
            return R.DETACHED
        return self.inner.run(req)


def test_stop_with_run_in_wait_detaches_and_leaves_it_alive(world):
    pool, run_dir = start_impl(world)
    pool.detach_force()
    pool.shutdown()
    holder = {}
    real_tick = Q.tick

    def tick(cfg, *, stop=None, **kw):
        holder["stop"] = stop
        return real_tick(cfg, stop=stop, **kw)

    def on_tick(report):
        if RP_child(world):
            holder["stop"].set()

    import wt.agentic.runproc as RP

    def RP_child(w):
        return RP.read_json(S.runs_dir(w.cfg, w.cid) / "001-implementer" / "child.json")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Q, "tick", tick)
        t0 = time.monotonic()
        Q.run(world.cfg, poll_interval_s=0.05, runner=world.runner, on_tick=on_tick,
              max_ticks=200)
    assert time.monotonic() - t0 < 10
    stop, = journal(world.cfg, "stop")
    assert stop["reason"] == "signal"
    assert stop["detached"] == [{"campaign": world.cid, "run_ids": ["001-implementer"]}]
    wt = Path(S.read_state(world.cfg, world.cid)["repo"]["worktree"])
    assert pid_alive(int((wt / "child.pid").read_text())) and not run_ends(world)


@pytest.mark.parametrize("kw", [{"once": True}, {"max_ticks": 3}])
def test_once_and_max_ticks_detach(world, kw):
    t0 = time.monotonic()
    Q.run(world.cfg, poll_interval_s=0.05, runner=world.runner, **kw)
    assert time.monotonic() - t0 < 10                        # does not wait for the 60 s run
    stop, = journal(world.cfg, "stop")
    assert stop["reason"] == ("once" if "once" in kw else "max_ticks")
    if "once" in kw:                                         # tick 1 only admits and opens
        assert stop["detached"] == []
        return
    assert stop["detached"] == [{"campaign": world.cid, "run_ids": ["001-implementer"]}]
    wt = Path(S.read_state(world.cfg, world.cid)["repo"]["worktree"])
    assert until(lambda: (wt / "child.pid").exists())
    assert pid_alive(int((wt / "child.pid").read_text())) and not run_ends(world)


def test_until_idle_unchanged(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(), review()]))
    a = added(cfg, monkeypatch)
    Q.run(cfg, until_idle=True, poll_interval_s=0.01)
    assert S.read_state(cfg, a)["state"] == "MERGED"
    stop, = journal(cfg, "stop")
    assert stop["reason"] == "idle" and stop["detached"] == [] and "abandoned" not in stop


def test_max_ticks_during_draining_has_restart_pending(tmp_path, monkeypatch):
    repo, cfg, a = gate_world(tmp_path, monkeypatch)
    monkeypatch.setattr(Q, "code_changes", lambda r, since: (
        ("newsha", ["src/wt/x.py"]) if pid_file(tmp_path) else (since, [])))
    monkeypatch.setattr(Q, "_EXEC", lambda *a: pytest.fail("exec despite max_ticks"))
    ticks = []

    def on_tick(report):
        ticks.append(1)
        if len(ticks) >= 8:
            (tmp_path / "g1.rel").write_text("")      # the gate ends; the exit drain harvests it

    Q.run(cfg, max_ticks=8, poll_interval_s=0.01, reexec=True, code_root=repo, on_tick=on_tick)
    stop, = journal(cfg, "stop")
    assert stop["reason"] == "max_ticks" and stop["restart_pending"] == {"to_sha": "newsha"}


def test_until_idle_during_draining_has_restart_pending(tmp_path, monkeypatch):
    """Precedence: `--until-idle` firing on a draining tick is a plain `idle` exit, no exec. The
    campaign cannot go idle by itself while draining (no gate or FakeRunner step is planned), so
    the tick after the detection reports idle by fiat; the exit path is what is under test."""
    repo, cfg, a = gate_world(tmp_path, monkeypatch)
    monkeypatch.setattr(Q, "code_changes", lambda r, since: (
        ("newsha", ["src/wt/x.py"]) if pid_file(tmp_path) else (since, [])))
    monkeypatch.setattr(Q, "_EXEC", lambda *a: pytest.fail("exec despite until_idle"))
    real_tick = Q.tick

    def tick(cfg_, **kw):
        report = real_tick(cfg_, **kw)
        if journal(cfg, "draining"):
            report.idle = True
            (tmp_path / "g1.rel").write_text("")      # the gate ends; the exit drain harvests it
        return report

    monkeypatch.setattr(Q, "tick", tick)
    Q.run(cfg, until_idle=True, poll_interval_s=0.01, reexec=True, code_root=repo)
    stop, = journal(cfg, "stop")
    assert stop["reason"] == "idle" and stop["restart_pending"] == {"to_sha": "newsha"}
    assert journal(cfg, "draining") and len(S.read_ledger(cfg, a, types=["gate"])) == 1


def test_draining_does_not_exec_while_the_user_config_is_invalid(tmp_path, monkeypatch):
    """SPEC-0226 inside the drain: once `reexec_deferred` is journaled, the 0.2 s poll must not
    exec even though nothing blocks it; the exec follows the config fix."""
    repo, cfg, a = gate_world(tmp_path, monkeypatch)
    user = Path(cfg["config_dir"]) / "config.yaml"
    user.write_text("agentic: {}\n")
    monkeypatch.setattr(Q, "code_changes", lambda r, since: (
        ("newsha", ["src/wt/x.py"]) if pid_file(tmp_path) else (since, [])))
    calls, seen = [], {"idle_ticks": 0}
    monkeypatch.setattr(Q, "_EXEC", lambda exe, argv, env: calls.append(1))

    def on_tick(report):
        if "broken" not in seen and journal(cfg, "draining"):
            user.write_text("")                        # broken: seen by the next tick's check
            seen["broken"] = True
        elif "released" not in seen and journal(cfg, "reexec_deferred"):
            (tmp_path / "g1.rel").write_text("")       # the drain's only blocker ends
            seen["released"] = True
        elif "released" in seen and "fixed" not in seen:
            if not Q._LAST_POOL.flights:
                seen["idle_ticks"] += 1                # nothing blocks the exec any more
            if seen["idle_ticks"] >= 4:
                seen["calls_before_fix"] = len(calls)
                user.write_text("agentic: {}\n")
                seen["fixed"] = True

    Q.run(cfg, poll_interval_s=0.05, reexec=True, code_root=repo, on_tick=on_tick,
          config_loader=lambda: dict(cfg), max_ticks=300)
    assert seen.get("fixed") and seen["calls_before_fix"] == 0
    assert len(calls) == 1
    deferred, = journal(cfg, "reexec_deferred")
    assert deferred["reason"] == "user_config_invalid" and deferred["to_sha"] == "newsha"
    stop, = journal(cfg, "stop")
    assert stop["reason"] == "code_changed" and stop["to_sha"] == "newsha"


def test_wait_any_sleeps_when_nothing_is_pending():
    pool = Q.StepPool()
    t0 = time.monotonic()
    pool.wait_any(timeout=0.3)
    assert 0.25 <= time.monotonic() - t0 < 2


def test_draining_wait_does_not_busy_spin_with_empty_pool_and_blocked_exec(tmp_path, monkeypatch):
    """The drain's inter-tick wait must pause even when the user config blocks the exec and no
    flight is pending: one 1 s tick makes a handful of polls, not thousands."""
    repo, cfg, a = gate_world(tmp_path, monkeypatch)
    user = Path(cfg["config_dir"]) / "config.yaml"
    user.write_text("agentic: {}\n")
    monkeypatch.setattr(Q, "code_changes", lambda r, since: (
        ("newsha", ["src/wt/x.py"]) if pid_file(tmp_path) else (since, [])))
    monkeypatch.setattr(Q, "_EXEC", lambda exe, argv, env: None)
    polls = {"n": 0}
    real_any_done = Q.StepPool.any_done

    def any_done(self):
        polls["n"] += 1
        return real_any_done(self)

    monkeypatch.setattr(Q.StepPool, "any_done", any_done)
    seen = {}

    def on_tick(report):
        if "broken" not in seen and journal(cfg, "draining"):
            user.write_text("")
            seen["broken"] = True
        elif "released" not in seen and journal(cfg, "reexec_deferred"):
            (tmp_path / "g1.rel").write_text("")
            seen["released"] = True
        elif "released" in seen and not Q._LAST_POOL.flights:
            if "start" not in seen:
                seen["start"] = polls["n"]
            else:
                seen["delta"] = polls["n"] - seen["start"]
                user.write_text("agentic: {}\n")      # let the exec happen and end the run

    Q.run(cfg, poll_interval_s=1, reexec=True, code_root=repo, on_tick=on_tick,
          config_loader=lambda: dict(cfg), max_ticks=40)
    assert seen.get("delta") is not None and seen["delta"] < 30


def test_crash_sweeps_and_journals_after(tmp_path, monkeypatch):
    repo, cfg, a = gate_world(tmp_path, monkeypatch)
    real_tick = Q.tick
    n = {"t": 0}

    def tick(*args, **kw):
        n["t"] += 1
        if n["t"] > 3 and pid_file(tmp_path):
            raise RuntimeError("boom")
        return real_tick(*args, **kw)

    monkeypatch.setattr(Q, "tick", tick)
    t0 = time.monotonic()
    with pytest.raises(RuntimeError):
        Q.run(cfg, poll_interval_s=0.05, max_ticks=400)
    assert time.monotonic() - t0 < 30
    assert not pid_alive(pid_file(tmp_path))                  # the gate group was killed
    crash, = journal(cfg, "crash")
    assert "detached" in crash and crash["abandoned"] == [] and "boom" in crash["error"]
    assert not L.is_held(cfg)
    assert [r["kind"] for r in Q.read_journal(cfg)][-1] == "crash"


def test_force_detach_waiting_and_hooks_honour_detach_all():
    pool = Q.StepPool()
    hk_wait, hk_spawn, hk_collect = (pool.hooks(None, p) for p in ("wait", "spawning", "collect"))
    release = threading.Event()
    futs = [pool.submit(release.wait, 30) for _ in range(3)]
    fl = Q._Flight({"id": "c"}, None)
    for i, (hk, f) in enumerate(zip((hk_wait, hk_spawn, hk_collect), futs)):
        hk.run_dir = Path(f"/x/00{i}-r")
        fl.tasks.append((Q.Step("implement"), f, hk))
        fl.runs.append((f"00{i}-r", 60))
    pool.flights["c"] = fl
    left = pool.force_detach_waiting()
    assert hk_wait.detach.is_set() and not hk_spawn.detach.is_set()
    assert [(x["run_id"], x["phase"]) for x in left] == [("001-r", "spawning"), ("002-r", "collect")]
    assert pool.detached_view() == [{"campaign": "c", "run_ids": ["000-r"]}]
    hk_spawn.spawned()                         # spawned after the sweep: detached, not waiting
    assert hk_spawn.detach.is_set() and hk_spawn.phase == "wait"
    assert hk_collect.enter_collect() is False
    release.set()
    pool.end(timeout=5)
    assert pool.leftover_threads() == []


def test_bound_expiry_abandons_stuck_tasks(tmp_path, monkeypatch):
    monkeypatch.setattr(Q, "_STOP_BOUND_S", 1.0)
    pool = Q.StepPool()
    release = threading.Event()
    hk_stuck, hk_wait = pool.hooks(Path("/x/001-s"), "spawning"), pool.hooks(Path("/x/002-w"), "wait")
    for cid, hk, rid in (("s", hk_stuck, "001-s"), ("w", hk_wait, "002-w")):
        fl = Q._Flight({"id": cid}, None)
        fl.tasks.append((Q.Step("implement"), pool.submit(
            lambda h=hk: R.DETACHED if h.detach.wait(30) else None if release.wait(30) else None), hk))
        fl.runs.append((rid, 60))
        pool.flights[cid] = fl
    # the stuck task is blocked in `spawning` until released; the wait task obeys its flag
    stuck_fl = pool.flights["s"]
    stuck_fl.tasks[0] = (stuck_fl.tasks[0][0], pool.submit(release.wait, 30), hk_stuck)
    cfg = {"data_dir": str(tmp_path / "d")}
    detached, abandoned = Q._crash_sweep(cfg, pool, 1.0)
    assert detached == [{"campaign": "w", "run_ids": ["002-w"]}]
    assert abandoned == [{"campaign": "s", "run_id": "001-s", "phase": "spawning"}]
    pool.end(timeout=0.2)
    assert pool.leftover_threads()                       # the stuck thread is left behind
    release.set()
    time.sleep(0.2)



def _in_process_pool(cid="g"):
    """A pool with one flight whose hookless gate task blocks on an `Event` (SPEC-0262)."""
    pool, release = Q.StepPool(), threading.Event()
    fl = Q._Flight({"id": cid}, None)
    fl.tasks.append((Q.Step("gate"), pool.submit(release.wait, 30), None))
    pool.flights[cid] = fl
    return pool, release


def test_bound_expiry_lists_in_process_flights(tmp_path, monkeypatch):
    """SPEC-0262: at the stop bound an unfinished in-process flight is recorded as abandoned
    (`phase: in_process` with its step), never silently dropped by `flights.clear()`."""
    monkeypatch.setattr(Q, "_STOP_BOUND_S", 0.3)
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {}}
    pool, release = _in_process_pool()
    lease = L.acquire(cfg, ["wt", "queue", "run"])
    stop = threading.Event()
    stop.set()
    try:
        report = Q._drain(cfg, pool, lease, stop=stop)
        assert {"campaign": "g", "run_id": None, "phase": "in_process",
                "step": "gate"} in report.abandoned
        assert not pool.flights
    finally:
        release.set()
        lease.release()
        pool.end(timeout=2)


def test_crash_sweep_lists_in_process_flights(tmp_path):
    """SPEC-0262: the crash path records the unfinished in-process flights the same way."""
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {}}
    pool, release = _in_process_pool()
    try:
        detached, abandoned = Q._crash_sweep(cfg, pool, 0.3)
        assert detached == []
        assert abandoned == [{"campaign": "g", "run_id": None, "phase": "in_process",
                              "step": "gate"}]
    finally:
        release.set()
        pool.end(timeout=2)


def test_crash_sweep_applies_task_finished_during_wait(tmp_path):
    """SPEC-0287: a hookless task that ends inside the bounded wait is harvested after it
    (its park lands on the state and ledger), not dropped and not listed as abandoned."""
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {}}
    cid = "SPEC-0900-20261027-aaaa"
    S.create_campaign(cfg, {"id": cid, "spec_id": "SPEC-0900", "project": "p", "state": "GATING"})
    st = S.read_state(cfg, cid)
    release = threading.Event()
    timer = threading.Timer(0.3, release.set)

    def gate():
        release.wait(30)
        return Q.StepResult(cid, Q.Step("gate"), "GATING", error="boom")

    pool = Q.StepPool()
    fl = Q._Flight(st, Q._Ctx(cfg))
    fl.tasks.append((Q.Step("gate"), pool.submit(gate), None))
    pool.flights[cid] = fl
    timer.start()
    try:
        detached, abandoned = Q._crash_sweep(cfg, pool, 5.0)
        assert (detached, abandoned) == ([], [])
        assert not pool.flights
        after = S.read_state(cfg, cid)
        assert after["state"] == "PARKED" and after["park"]["resume_to"] == "GATING"
        assert "boom" in after["park"]["detail"]
        assert any(e.get("to") == "PARKED"
                   for e in S.read_ledger(cfg, cid, types=["transition"]))
        assert any(r["kind"] == "tick" and r.get("drain") for r in Q.read_journal(cfg))
    finally:
        release.set()
        timer.cancel()
        pool.end(timeout=2)


def test_crash_sweep_leaves_force_detached_review_to_adoption(tmp_path):
    """SPEC-0287: the post-wait harvest skips a review flight whose run was force-detached (its
    task returns DETACHED); the run is left for adoption, not applied as a missing verdict."""
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {}}
    rid, gid = "SPEC-0900-20261027-aaaa", "SPEC-0901-20261027-bbbb"
    S.create_campaign(cfg, {"id": rid, "spec_id": "SPEC-0900", "project": "p",
                            "state": "REVIEWING"})
    S.create_campaign(cfg, {"id": gid, "spec_id": "SPEC-0901", "project": "p", "state": "GATING"})
    st, before = S.read_state(cfg, rid), S.read_ledger(cfg, rid)
    pool = Q.StepPool()
    step = Q.Step("review", persona="reviewer", personas=("reviewer",))
    hk = pool.hooks(Path("/x/002-reviewer"), "wait")

    def reviewer():                             # `_do_persona` in `wait`: returns once detached
        hk.detach.wait(30)
        return Q.StepResult(rid, step, "REVIEWING", run_id="002-reviewer", detached=True)

    fl = Q._Flight(st, Q._Ctx(cfg), group=Q._ReviewGroup(st, step))
    fl.tasks.append((Q.Step("review", persona="reviewer"), pool.submit(reviewer), hk))
    fl.runs.append(("002-reviewer", 60))
    pool.flights[rid] = fl
    release = threading.Event()
    timer = threading.Timer(0.5, release.set)   # keeps the wait loop going past the detach

    def gate():
        release.wait(30)
        return Q.StepResult(gid, Q.Step("gate"), "GATING", error="boom")

    gl = Q._Flight(S.read_state(cfg, gid), Q._Ctx(cfg))
    gl.tasks.append((Q.Step("gate"), pool.submit(gate), None))
    pool.flights[gid] = gl
    timer.start()
    try:
        detached, abandoned = Q._crash_sweep(cfg, pool, 5.0)
        assert detached == [{"campaign": rid, "run_ids": ["002-reviewer"]}] and abandoned == []
        assert S.read_state(cfg, rid) == st and S.read_ledger(cfg, rid) == before
        assert S.read_state(cfg, gid)["state"] == "PARKED"     # the gate still lands
        assert not pool.flights
    finally:
        release.set()
        timer.cancel()
        hk.detach.set()
        pool.end(timeout=2)


def test_crash_sweep_leaves_review_spawned_during_wait_to_adoption(tmp_path):
    """SPEC-0287: a reviewer still `spawning` when the sweep starts claims during the wait, so
    `spawned` detaches it and it returns DETACHED without ever entering `forced` through
    `force_detach_waiting`. The post-wait harvest records it for adoption, never applies it."""
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {}}
    rid, gid = "SPEC-0900-20261027-aaaa", "SPEC-0901-20261027-bbbb"
    S.create_campaign(cfg, {"id": rid, "spec_id": "SPEC-0900", "project": "p",
                            "state": "REVIEWING"})
    S.create_campaign(cfg, {"id": gid, "spec_id": "SPEC-0901", "project": "p", "state": "GATING"})
    st, before = S.read_state(cfg, rid), S.read_ledger(cfg, rid)
    pool = Q.StepPool()
    step = Q.Step("review", persona="reviewer", personas=("reviewer",))
    hk = pool.hooks(Path("/x/002-reviewer"), "spawning")
    claimed = threading.Event()
    claim = threading.Timer(0.3, claimed.set)

    def reviewer():                             # `_do_persona`: the wrapper claims, then `_wait`
        claimed.wait(30)
        hk.spawned()
        assert hk.detached()                    # the sweep's `detach_all` caught the spawn
        return Q.StepResult(rid, step, "REVIEWING", run_id="002-reviewer", detached=True)

    fl = Q._Flight(st, Q._Ctx(cfg), group=Q._ReviewGroup(st, step))
    fl.tasks.append((Q.Step("review", persona="reviewer"), pool.submit(reviewer), hk))
    fl.runs.append(("002-reviewer", 60))
    pool.flights[rid] = fl
    release = threading.Event()
    timer = threading.Timer(0.6, release.set)   # keeps the wait loop going past the spawn

    def gate():
        release.wait(30)
        return Q.StepResult(gid, Q.Step("gate"), "GATING", error="boom")

    gl = Q._Flight(S.read_state(cfg, gid), Q._Ctx(cfg))
    gl.tasks.append((Q.Step("gate"), pool.submit(gate), None))
    pool.flights[gid] = gl
    claim.start()
    timer.start()
    try:
        detached, abandoned = Q._crash_sweep(cfg, pool, 5.0)
        assert detached == [{"campaign": rid, "run_ids": ["002-reviewer"]}] and abandoned == []
        assert S.read_state(cfg, rid) == st and S.read_ledger(cfg, rid) == before
        assert S.read_state(cfg, gid)["state"] == "PARKED"     # the gate still lands
        assert not pool.flights
    finally:
        claimed.set()
        release.set()
        claim.cancel()
        timer.cancel()
        pool.end(timeout=2)


def test_waiting_on_lists_only_blockers(tmp_path):
    pool = Q.StepPool()
    f_wait, f_gate = pool.submit(time.sleep, 0.3), pool.submit(time.sleep, 0.3)
    fl = Q._Flight({"id": "p"}, None)
    fl.tasks.append((Q.Step("implement"), f_wait, pool.hooks(None, "wait")))
    gl = Q._Flight({"id": "g"}, None)
    gl.tasks.append((Q.Step("gate"), f_gate, None))
    pool.flights.update(p=fl, g=gl)
    cfg = {"data_dir": str(tmp_path), "agentic": {}}
    assert [w["campaign"] for w in Q._waiting_on(cfg, pool)] == ["g"]
    pool.end(timeout=2)


def test_list_marks_detached_run(world):
    pool, run_dir = start_impl(world)
    pool.detach_force()
    pool.shutdown()
    payload = Q.queue_payload(world.cfg)
    (item,) = [i for i in payload["items"] if i["campaign"] == world.cid]
    assert item["run"].startswith("detached (pid ")


class Stuck:
    """A detachable runner whose spawn never returns (a hung `Popen`)."""
    detachable = True

    def __init__(self):
        self.release = threading.Event()

    def run(self, req, *, hooks=None):
        self.release.wait(60)
        return R.DETACHED


@pytest.mark.parametrize("how", ["stop", "crash"])
def test_bound_expiry_through_run(tmp_path, monkeypatch, how):
    monkeypatch.setattr(Q, "_STOP_BOUND_S", 1.0)
    repo, cfg, a = gate_world(tmp_path, monkeypatch)
    runner = Stuck()
    started = lambda: bool(S.read_ledger(cfg, a, types=["run.start"]))     # noqa: E731
    try:
        t0 = time.monotonic()
        if how == "stop":
            run_with_stop(cfg, started, runner=runner)
        else:
            real_tick = Q.tick

            def tick(*args, **kw):
                if started():
                    raise RuntimeError("boom")
                return real_tick(*args, **kw)

            monkeypatch.setattr(Q, "tick", tick)
            with pytest.raises(RuntimeError):
                Q.run(cfg, poll_interval_s=0.05, runner=runner, max_ticks=200)
        assert time.monotonic() - t0 < 15
        rec, = journal(cfg, "stop" if how == "stop" else "crash")
        assert rec["abandoned"] == [{"campaign": a, "run_id": rec["abandoned"][0]["run_id"],
                                     "phase": "spawning"}]
        assert rec["detached"] == [] and not L.is_held(cfg)
        assert Q.leftover_threads()                          # the lease did not wait for it
    finally:
        runner.release.set()
        time.sleep(0.2)


def _cli_queue_run(tmp_path, monkeypatch, run):
    from click.testing import CliRunner
    from wt.cli import cli
    exits = []
    monkeypatch.setattr("wt.cli.load_config", lambda: {"data_dir": str(tmp_path)})
    monkeypatch.setattr(Q, "run", run)
    monkeypatch.setattr(Q, "leftover_threads", lambda: [object()])
    monkeypatch.setattr(cli_queue.os, "_exit", lambda code: exits.append(code))
    return CliRunner().invoke(cli, ["queue", "run", "--once"]), exits


def test_cli_prints_traceback_and_exits_70(tmp_path, monkeypatch):
    def boom(cfg, **kw):
        raise RuntimeError("kaboom")

    res, exits = _cli_queue_run(tmp_path, monkeypatch, boom)
    assert exits == [70]
    assert "Traceback" in res.output and "kaboom" in res.output     # stderr (mixed by CliRunner)
    assert isinstance(res.exception, RuntimeError)                  # then it re-raises


def test_cli_exits_70_when_run_raises_value_error_with_leftover_threads(tmp_path, monkeypatch):
    def boom(cfg, **kw):
        raise ValueError("bad json")

    res, exits = _cli_queue_run(tmp_path, monkeypatch, boom)
    assert exits == [70]
    assert "Traceback" in res.output and "bad json" in res.output


def test_cli_value_error_without_leftover_threads_is_a_click_exception(tmp_path, monkeypatch):
    def boom(cfg, **kw):
        raise ValueError("bad json")

    monkeypatch.setattr(Q, "leftover_threads", lambda: [])
    exits = []
    monkeypatch.setattr("wt.cli.load_config", lambda: {"data_dir": str(tmp_path)})
    monkeypatch.setattr(Q, "run", boom)
    monkeypatch.setattr(cli_queue.os, "_exit", lambda code: exits.append(code))
    from click.testing import CliRunner
    from wt.cli import cli
    res = CliRunner().invoke(cli, ["queue", "run", "--once"])
    assert exits == [] and res.exit_code == 1
    assert "bad json" in res.output and "Traceback" not in res.output


def test_cli_exits_70_when_run_returns_with_leftover_threads(tmp_path, monkeypatch):
    res, exits = _cli_queue_run(tmp_path, monkeypatch, lambda cfg, **kw: 3)
    assert exits == [70] and res.exception is None and "Traceback" not in res.output


def test_draining_starts_no_gate_for_a_harvested_campaign(tmp_path, monkeypatch):
    """B's implementer is harvested while A's gate still blocks the exec: B stays GATING."""
    repo, cfg, a = gate_world(tmp_path, monkeypatch)
    _second_spec(repo)
    cfg["agentic"]["profiles"]["balanced"]["slots"] = 2
    runner = Det(cfg)
    state, seen, calls = {"changed": False}, {"after": 0}, []
    monkeypatch.setattr(Q, "code_changes", lambda r, since: (
        ("newsha", ["src/wt/x.py"]) if state["changed"] else (since, [])))
    monkeypatch.setattr(Q, "_EXEC", lambda exe, argv, env: calls.append(1))

    def on_tick(report):
        if not state["changed"] and pid_file(tmp_path):
            seen["b"] = added(cfg, monkeypatch, "SPEC-0002")
            runner.release.clear()
            state["changed"] = True
        b = seen.get("b")
        if b and S.read_ledger(cfg, b, types=["run.start"]) and not runner.release.is_set():
            runner.release.set()                                 # B's implementer finishes
        if b and S.read_state(cfg, b)["state"] == "GATING":
            seen["after"] += 1
            if seen["after"] >= 3:
                (tmp_path / "g1.rel").write_text("")

    runner.release.set()
    Q.run(cfg, poll_interval_s=0.3, reexec=True, code_root=repo, on_tick=on_tick,
          runner=runner, max_ticks=100)
    b = seen["b"]
    assert calls and S.read_state(cfg, b)["state"] == "GATING"
    assert not S.read_ledger(cfg, b, types=["gate"]) and not S.gates_dir(cfg, b).exists()


def test_draining_tick_admits_while_a_gate_blocks_exec(tmp_path, monkeypatch):
    """SPEC-0268 / SPEC-0229: A's gate blocks the exec; a ready QUEUED B is still admitted and
    opened by the draining ticks, and no re-exec is journaled before the gate ends."""
    repo, cfg, a = gate_world(tmp_path, monkeypatch)
    _second_spec(repo)
    cfg["agentic"]["profiles"]["balanced"]["slots"] = 2
    runner = Det(cfg)
    runner.release.set()
    seen, calls = {}, []
    monkeypatch.setattr(Q, "code_changes", lambda r, since: (
        ("newsha", ["src/wt/x.py"]) if "changed" in seen else (since, [])))
    monkeypatch.setattr(Q, "_EXEC", lambda exe, argv, env: calls.append(1))

    def on_tick(report):
        if "changed" not in seen and pid_file(tmp_path):         # A is in its held gate
            seen["changed"] = True
            seen["b"] = added(cfg, monkeypatch, "SPEC-0002")
        b = seen.get("b")
        if b and "b_state" not in seen and journal(cfg, "draining") \
                and S.read_state(cfg, b)["state"] != "QUEUED":
            seen["b_state"] = S.read_state(cfg, b)["state"]
            seen["stops"] = journal(cfg, "stop")
            seen["gate_blocked"] = not (tmp_path / "g1.rel").exists()
            (tmp_path / "g1.rel").write_text("")

    Q.run(cfg, poll_interval_s=0.1, reexec=True, code_root=repo, on_tick=on_tick,
          runner=runner, max_ticks=200)
    assert seen["b_state"] != "QUEUED"
    assert seen["gate_blocked"] and seen["stops"] == [] and calls
    stop, = journal(cfg, "stop")
    assert stop["reason"] == "code_changed"


def test_admission_blocked_journaled_once_per_change(tmp_path, monkeypatch):
    """SPEC-0268: full slots with a ready item waiting give one `admission_blocked` record per
    distinct (ready, holders, deferred) set; a draining tick's skipped close is `deferred`."""
    repo, cfg, a = gate_world(tmp_path, monkeypatch)
    for i in (2, 3, 4):
        _second_spec(repo, f"SPEC-000{i}", f"docs/specs/000{i}-x.md")
    cfg["agentic"]["profiles"]["balanced"]["slots"] = 2
    runner = Det(cfg)
    runner.release.set()
    seen, calls = {"n": 0}, []
    monkeypatch.setattr(Q, "code_changes", lambda r, since: (
        ("newsha", ["src/wt/x.py"]) if "changed" in seen else (since, [])))
    monkeypatch.setattr(Q, "_EXEC", lambda exe, argv, env: calls.append(1))

    def hold(spec):                        # a CLOSING holder, made before the next tick
        cid = added(cfg, monkeypatch, spec)
        st = S.read_state(cfg, cid)
        st["state"] = "CLOSING"
        S.write_state(cfg, cid, st)
        return cid

    def on_tick(report):
        if "changed" not in seen and pid_file(tmp_path):         # A is in its held gate
            seen["changed"] = True
            seen["b"] = hold("SPEC-0002")
            seen["c"] = added(cfg, monkeypatch, "SPEC-0003")     # ready, no slot
            return
        if "changed" not in seen:
            return
        seen["n"] += 1
        if seen["n"] == 8:
            seen["first"] = journal(cfg, "admission_blocked")
            st = S.read_state(cfg, seen["b"])
            st["state"] = "PARKED"                               # frees B's slot ...
            S.write_state(cfg, seen["b"], st)
            seen["d"] = hold("SPEC-0004")                        # ... which D takes at once
        if seen["n"] == 16:
            (tmp_path / "g1.rel").write_text("")

    Q.run(cfg, poll_interval_s=0.05, reexec=True, code_root=repo, on_tick=on_tick,
          runner=runner, max_ticks=300)
    first, second = seen["first"], journal(cfg, "admission_blocked")
    (rec,) = first
    assert rec["ready"] == [seen["c"]] and rec["slots"] == 2 and rec["occupied"] == 2
    assert rec["draining"] is True
    held = {h["campaign"]: h for h in rec["holders"]}
    assert set(held) == {a, seen["b"]} and held[seen["b"]]["state"] == "CLOSING"
    assert held[seen["b"]]["deferred"] is True and held[a]["deferred"] is False
    assert len(second) == 2 and {h["campaign"] for h in second[1]["holders"]} == {a, seen["d"]}


def test_admission_blocked_journaled_again_when_the_condition_recurs(tmp_path, monkeypatch):
    """SPEC-0268: a tick without the blocked condition resets the dedupe key, so the identical
    (ready, holders, deferred) set coming back is journaled a second time."""
    repo, cfg, a = gate_world(tmp_path, monkeypatch)
    for i in (2, 3):
        _second_spec(repo, f"SPEC-000{i}", f"docs/specs/000{i}-x.md")
    cfg["agentic"]["profiles"]["balanced"]["slots"] = 2
    runner = Det(cfg)
    runner.release.set()
    seen, calls = {"n": 0}, []
    monkeypatch.setattr(Q, "code_changes", lambda r, since: (
        ("newsha", ["src/wt/x.py"]) if "changed" in seen else (since, [])))
    monkeypatch.setattr(Q, "_EXEC", lambda exe, argv, env: calls.append(1))

    def set_state(cid, state):
        st = S.read_state(cfg, cid)
        st["state"] = state
        S.write_state(cfg, cid, st)

    def on_tick(report):
        if "changed" not in seen and pid_file(tmp_path):         # A is in its held gate
            seen["changed"] = True
            seen["b"] = added(cfg, monkeypatch, "SPEC-0002")
            set_state(seen["b"], "CLOSING")
            seen["c"] = added(cfg, monkeypatch, "SPEC-0003")     # ready, no slot
            return
        if "changed" not in seen:
            return
        seen["n"] += 1
        if seen["n"] == 8:
            seen["first"] = journal(cfg, "admission_blocked")
            set_state(seen["c"], "PARKED")                       # nothing is waiting any more
        if seen["n"] == 14:
            seen["mid"] = journal(cfg, "admission_blocked")
            set_state(seen["c"], "QUEUED")                       # the same condition returns
        if seen["n"] == 22:
            (tmp_path / "g1.rel").write_text("")

    Q.run(cfg, poll_interval_s=0.05, reexec=True, code_root=repo, on_tick=on_tick,
          runner=runner, max_ticks=300)
    assert len(seen["first"]) == 1 and len(seen["mid"]) == 1
    first, second = journal(cfg, "admission_blocked")[0], journal(cfg, "admission_blocked")
    assert len(second) == 2
    for k in ("ready", "slots", "occupied", "holders"):
        assert second[1][k] == first[k]
    assert second[1]["tick"] > first["tick"]


def test_admission_blocked_non_draining_has_no_deferred_holders(tmp_path, monkeypatch):
    """SPEC-0268: outside a drain the record says `draining: False` and defers no holder."""
    repo, cfg, a = gate_world(tmp_path, monkeypatch)
    _second_spec(repo)
    cfg["agentic"]["profiles"]["balanced"]["slots"] = 1
    runner = Det(cfg)
    runner.release.set()
    seen = {"n": 0}

    def on_tick(report):
        if "b" not in seen and pid_file(tmp_path):               # A holds the only slot
            seen["b"] = added(cfg, monkeypatch, "SPEC-0002")
        if "b" in seen:
            seen["n"] += 1
            if seen["n"] == 6:
                (tmp_path / "g1.rel").write_text("")

    Q.run(cfg, poll_interval_s=0.05, on_tick=on_tick, runner=runner, max_ticks=100)
    rec = journal(cfg, "admission_blocked")[0]
    assert rec["draining"] is False and rec["ready"] == [seen["b"]]
    assert rec["holders"] and all(h["deferred"] is False for h in rec["holders"])


def test_signal_during_draining_clears_restart(tmp_path, monkeypatch):
    """Precedence: a human stop during draining ticks is a plain `signal` exit: no exec, no
    `restart_pending`."""
    repo, cfg, a = gate_world(tmp_path, monkeypatch)
    monkeypatch.setattr(Q, "code_changes", lambda r, since: (
        ("newsha", ["src/wt/x.py"]) if pid_file(tmp_path) else (since, [])))
    monkeypatch.setattr(Q, "_EXEC", lambda *a: pytest.fail("exec despite a signal"))
    run_with_stop(cfg, lambda: bool(journal(cfg, "draining")), reexec=True, code_root=repo)
    stop, = journal(cfg, "stop")
    assert stop["reason"] == "signal" and "restart_pending" not in stop
    assert "to_sha" not in stop and not pid_alive(pid_file(tmp_path))


def test_crash_sweep_journals_harvest_error_and_applies_the_rest(tmp_path, monkeypatch):
    from concurrent.futures import Future
    from types import SimpleNamespace
    cfg = {"data_dir": str(tmp_path / "d")}
    pool = Q.StepPool()
    applied = []

    def apply(ctx, st, res):
        if st["id"] == "bad":
            raise ValueError("cannot apply")
        applied.append(st["id"])
        return {"campaign": st["id"]}

    monkeypatch.setattr(Q, "_apply", apply)
    for cid in ("bad", "good"):
        fut = Future()
        fut.set_result(Q.StepResult(cid, Q.Step("implement"), "IMPLEMENTING"))
        fl = Q._Flight({"id": cid}, SimpleNamespace(cfg=cfg))
        fl.tasks.append((Q.Step("implement"), fut, pool.hooks(None, "wait")))
        pool.flights[cid] = fl
    detached, abandoned = Q._crash_sweep(cfg, pool, 1.0)
    assert applied == ["good"] and not pool.flights and not abandoned
    err, = journal(cfg, "harvest_error")
    assert err["campaign"] == "bad" and "cannot apply" in err["error"]
    assert [r for r in journal(cfg, "tick") if r["drain"]][0]["steps"] == [{"campaign": "good"}]


# ---- real detached runs: ClaudeRunner + fake claude, several campaigns ---------------------------

@pytest.fixture
def reap(world):
    """Campaign ids whose runs are terminated at teardown (the `world` fixture reaps its own)."""
    cids = []
    yield cids
    for cid in cids:
        for d in S.runs_dir(world.cfg, cid).glob("*"):
            RP.terminate_run(d)
            RP.terminate_run(d, kill=True)


def multi_world(world, monkeypatch, reap, n):
    """`world` with SPEC-0001..n queued, a held two-gate project (test gate waits for g1.rel,
    lint gate touches g2.started), n slots, and a release file per worktree."""
    repo = world.tmp / "repo"
    monkeypatch.delenv("FAKE_CLAUDE_RELEASE")
    (repo / "gates" / "test.sh").write_text(gate_script(world.tmp) + "\n")
    (repo / "gates" / "lint.sh").write_text(f"touch {world.tmp}/g2.started\n")
    world.cfg["agentic"]["profiles"]["balanced"]["slots"] = n
    world.cids = [world.cid]
    reap.append(world.cid)
    for i in range(2, n + 1):
        _second_spec(repo, f"SPEC-000{i}", f"docs/specs/000{i}-x.md")
        world.cids.append(added(world.cfg, monkeypatch, f"SPEC-000{i}"))
        reap.append(world.cids[-1])
    return repo


def wt_of(world, cid):
    return Path(S.read_state(world.cfg, cid)["repo"]["worktree"])


def child_of(world, cid):
    try:
        f = wt_of(world, cid) / "child.pid"
        return int(f.read_text()) if f.exists() and f.read_text().strip() else None
    except (KeyError, TypeError, ValueError):               # not opened yet
        return None


def release_run(world, cid):
    (wt_of(world, cid) / "release").write_text("")


def adopted(world):
    return [r for rec in journal(world.cfg, "adopt") for r in rec["runs"]]


def test_draining_ticks_admit_exec_promptly_and_new_process_adopts(world, monkeypatch, reap):
    repo = multi_world(world, monkeypatch, reap, 1)
    _second_spec(repo)                                  # SPEC-0002: queued later, during the drain
    world.cfg["agentic"]["profiles"]["balanced"]["slots"] = 2
    cfg, a = world.cfg, world.cid
    seen, calls = {}, []
    monkeypatch.setattr(Q, "code_changes", lambda r, since: (
        (since, []) if "changed" not in seen else
        ("sha2", ["src/wt/y.py"]) if "second" in seen else ("sha1", ["src/wt/x.py"])))
    monkeypatch.setattr(Q, "_EXEC", lambda exe, argv, env: (calls.append(time.monotonic()),
                                                           seen.update(env=env)))
    real_apply = Q._apply

    def apply(ctx, st, res):
        out = real_apply(ctx, st, res)
        if res.step.kind == "gate":
            seen["harvest"] = time.monotonic()
        return out

    monkeypatch.setattr(Q, "_apply", apply)

    def on_tick(report):
        if "a_released" not in seen and child_of(world, a):
            seen["a_released"] = True
            release_run(world, a)                       # A's implementer ends; A reaches its gate
        if "changed" not in seen and pid_file(world.tmp):
            seen["changed"] = True                      # A is in its held gate
            seen["b"] = added(cfg, monkeypatch, "SPEC-0002")
            reap.append(seen["b"])
        b = seen.get("b")
        if b and "second" not in seen and child_of(world, b):
            seen["second"] = True                       # a later merge moves the target
            info = L.read_lease(cfg)["draining"]
            seen["waiting_on"] = [w["campaign"] for w in info["waiting_on"]]
            (world.tmp / "g1.rel").write_text("")       # the gate ends, B's run is in `wait`

    Q.run(cfg, poll_interval_s=1, reexec=True, code_root=repo, on_tick=on_tick,
          runner=world.runner, max_ticks=100)
    b = seen["b"]
    assert calls and "harvest" in seen
    assert calls[0] - seen["harvest"] < 0.5             # exec at the first 0.2 s check
    assert seen["waiting_on"] == [a]                    # the gate blocks; B's run in `wait` does not
    stop, = journal(cfg, "stop")
    b_run = S.read_ledger(cfg, b, types=["run.start"])[0]["run_id"]
    assert stop["reason"] == "code_changed" and stop["to_sha"] == "sha2"
    assert stop["paths"] == ["src/wt/y.py"]
    assert stop["detached"] == [{"campaign": b, "run_ids": [b_run]}]
    assert len(S.read_ledger(cfg, a, types=["gate"])) == 1            # no gate after detection
    drain_ticks = [r for r in journal(cfg, "tick") if r.get("drain")]
    assert any(s["campaign"] == a and s["action"] == "gate"
               for r in drain_ticks for s in r["steps"])              # the harvest was journaled
    assert drain_ticks[0]["ts"] <= stop["ts"]
    pid = child_of(world, b)
    assert pid_alive(pid)
    # the new process (the same, here: it inherits the handed-over lease) adopts B's implementer
    for k in (L.HANDOVER_ENV,):
        monkeypatch.setenv(k, seen["env"][k])
    Q.run(cfg, max_ticks=2, poll_interval_s=0.1, runner=world.runner)
    assert [r["status"] for r in adopted(world) if r["campaign"] == b] == ["running"]
    assert pid_alive(pid) and not [e for e in S.read_ledger(cfg, b, types=["run.end"])]


def test_stop_with_wait_run_and_gate_then_next_runner_adopts_and_regates(world, monkeypatch, reap):
    multi_world(world, monkeypatch, reap, 2)
    cfg, (a, b) = world.cfg, world.cids
    seen = {}

    def when():
        if "a_rel" not in seen and child_of(world, a):
            seen["a_rel"] = True
            release_run(world, a)                       # A's implementer ends; A starts its gate
        return pid_file(world.tmp) is not None and child_of(world, b) is not None

    n, took = run_with_stop(cfg, when, runner=world.runner)
    gate_pid, b_pid = pid_file(world.tmp), child_of(world, b)
    assert not pid_alive(gate_pid) and pid_alive(b_pid) and took < 20
    assert not (world.tmp / "g2.started").exists()                # the second gate never started
    st = S.read_state(cfg, a)
    assert st["state"] == "GATING" and st["counters"]["gate_runs"] == 0
    assert st["counters"]["loops"] == 0 and not S.read_ledger(cfg, a, types=["gate"])
    rows = [s for r in journal(cfg, "tick") for s in r["steps"]
            if s["campaign"] == a and s["action"] == "gate"]
    assert [r["reason"] for r in rows] == ["interrupted"]
    b_run = S.read_ledger(cfg, b, types=["run.start"])[0]["run_id"]
    stop, = journal(cfg, "stop")
    assert stop["reason"] == "signal" and stop["detached"] == [{"campaign": b, "run_ids": [b_run]}]
    assert not S.read_ledger(cfg, b, types=["run.end"])

    # a new run() adopts B's run and applies it, and re-gates A with the next seq
    (world.tmp / "g1.rel").write_text("")
    release_run(world, b)
    run_with_stop(cfg, lambda: S.read_ledger(cfg, b, types=["run.end"])
                  and S.read_ledger(cfg, a, types=["gate"]), runner=world.runner)
    assert [r["status"] for r in adopted(world) if r["campaign"] == b] == ["running"]
    assert len(S.read_ledger(cfg, b, types=["run.end"])) == 1
    assert S.read_ledger(cfg, a, types=["gate"])[0]["gate_seq"] == 2


def test_crash_with_gate_wait_spawning_and_finished_flight(world, monkeypatch, reap):
    multi_world(world, monkeypatch, reap, 4)
    monkeypatch.setattr(Q, "_STOP_BOUND_S", 2.0)
    cfg, (a, b, c, d) = world.cfg, world.cids
    go, real_popen, seen = world.tmp / "go", subprocess.Popen, {}
    code = ("import os, sys, time, runpy\nwhile not os.path.exists(sys.argv[1]): time.sleep(0.05)\n"
            "sys.argv = ['x', sys.argv[2]]\n"
            "runpy.run_module('wt.agentic.runproc', run_name='__main__')")

    def paused(argv, **kw):                                     # C's wrapper start is held
        if "wt.agentic.runproc" in argv and c in argv[-1]:
            proc = real_popen([sys.executable, "-c", code, str(go), argv[-1]], **kw)
            seen["c_pid"] = proc.pid
            return proc
        return real_popen(argv, **kw)

    monkeypatch.setattr(R.subprocess, "Popen", paused)
    real_tick = Q.tick

    def tick(*args, pool=None, **kw):
        if "a_rel" not in seen and child_of(world, a):
            seen["a_rel"] = True
            release_run(world, a)                       # A's implementer ends; A starts its gate
        if (pid_file(world.tmp) and child_of(world, b) and child_of(world, d)
                and c in pool.flights and pool.flights[c].tasks[0][2].phase == "spawning"):
            release_run(world, d)                       # D finishes: a done, unharvested flight
            assert until(lambda: pool.flights[d].done())
            seen["pool"] = pool
            raise RuntimeError("boom")
        return real_tick(*args, pool=pool, **kw)

    monkeypatch.setattr(Q, "tick", tick)
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="boom"):
        Q.run(cfg, poll_interval_s=0.05, runner=world.runner)
    assert time.monotonic() - t0 < 30 and not L.is_held(cfg)
    runs = {cid: S.read_ledger(cfg, cid, types=["run.start"])[0]["run_id"] for cid in (b, c, d)}
    assert not pid_alive(pid_file(world.tmp))                   # the gate group was killed
    assert [e["run_id"] for e in S.read_ledger(cfg, d, types=["run.end"])] == [runs[d]]
    assert S.read_state(cfg, d)["state"] != "IMPLEMENTING"      # the finished flight was applied
    assert not journal(cfg, "harvest_error")
    assert S.read_state(cfg, a)["state"] == "GATING" and not S.read_ledger(cfg, a, types=["gate"])
    crash, = journal(cfg, "crash")
    assert crash["detached"] == [{"campaign": b, "run_ids": [runs[b]]}]
    assert crash["abandoned"] == [{"campaign": c, "run_id": runs[c], "phase": "spawning"}]
    assert [r["kind"] for r in Q.read_journal(cfg)][-1] == "crash"
    b_pid = child_of(world, b)
    assert pid_alive(b_pid) and pid_alive(seen["c_pid"])        # both runs outlived the crash
    # the next runner adopts both
    go.write_text("")
    assert until(lambda: (S.runs_dir(cfg, c) / runs[c] / "proc.json").exists())
    (world.tmp / "g1.rel").write_text("")
    monkeypatch.setattr(Q, "tick", real_tick)
    Q.run(cfg, max_ticks=2, poll_interval_s=0.1, runner=world.runner)
    assert {r["campaign"]: r["status"] for r in adopted(world)
            if r["campaign"] in (b, c)} == {b: "running", c: "running"}
    assert pid_alive(b_pid)


@pytest.mark.parametrize("how", ["stop", "crash"])
def test_bound_expiry_abandons_collect_and_next_runner_commits_once(world, monkeypatch, reap, how):
    """A's `collect` thread commits, then hangs (the process 'dies' there); B waits. At the
    bound B is detached anyway and A is abandoned; the next runner adopts A as finished and
    records the commit it already made exactly once."""
    multi_world(world, monkeypatch, reap, 2)
    monkeypatch.setattr(Q, "_STOP_BOUND_S", 1.0)
    cfg, (a, b) = world.cfg, world.cids
    reached, hang, seen = threading.Event(), threading.Event(), {}
    real_commit = Q.git.commit_all

    def commit_all(worktree, message, env=None):
        sha = real_commit(worktree, message, env)
        if not reached.is_set():
            reached.set()
            hang.wait(60)
            raise RuntimeError("the runner died while collecting")
        return sha

    monkeypatch.setattr(Q.git, "commit_all", commit_all)

    def ready():
        if "a_rel" not in seen and child_of(world, a):
            seen["a_rel"] = True
            release_run(world, a)
        return reached.is_set() and child_of(world, b) is not None

    real_tick = Q.tick
    try:
        t0 = time.monotonic()
        if how == "stop":
            run_with_stop(cfg, ready, runner=world.runner)
        else:
            def tick(*args, **kw):
                if ready():
                    raise RuntimeError("boom")
                return real_tick(*args, **kw)

            monkeypatch.setattr(Q, "tick", tick)
            with pytest.raises(RuntimeError, match="boom"):
                Q.run(cfg, poll_interval_s=0.05, runner=world.runner)
            monkeypatch.setattr(Q, "tick", real_tick)
        assert time.monotonic() - t0 < 20 and not L.is_held(cfg)
        a_run = S.read_ledger(cfg, a, types=["run.start"])[0]["run_id"]
        b_run = S.read_ledger(cfg, b, types=["run.start"])[0]["run_id"]
        rec, = journal(cfg, "stop" if how == "stop" else "crash")
        assert rec["abandoned"] == [{"campaign": a, "run_id": a_run, "phase": "collect"}]
        assert rec["detached"] == [{"campaign": b, "run_ids": [b_run]}]
        assert Q.leftover_threads()                             # the lease did not wait for it
        a_dir = S.runs_dir(cfg, a) / a_run
        assert (a_dir / "exit.json").exists() and not S.read_ledger(cfg, a, types=["run.end"])
        # the next runner: A is `finished` (collected again), B is still `running`
        run_with_stop(cfg, lambda: S.read_ledger(cfg, a, types=["run.end"]), runner=world.runner)
        assert {r["campaign"]: r["status"] for r in adopted(world)} == {a: "finished",
                                                                        b: "running"}
        assert len(S.read_ledger(cfg, a, types=["run.end"])) == 1
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt_of(world, a), text=True,
                              capture_output=True, check=True).stdout.strip()
        subjects = subprocess.run(["git", "log", "--format=%s"], cwd=wt_of(world, a), text=True,
                                  capture_output=True, check=True).stdout.splitlines()
        assert len([x for x in subjects if x.startswith(f"wt({a}): implementer run")]) == 1
        commits = S.read_ledger(cfg, a, types=["commit"])
        assert [c["sha"] for c in commits] == [head]            # recorded once
    finally:
        hang.set()
        time.sleep(0.2)


# ---- FakeRunner steps in flight at detection; close steps while draining ---------------------------

def test_fakerunner_step_in_flight_is_waited_for_and_never_detached(tmp_path, monkeypatch):
    """AC2: A's non-detachable implementer is mid-sleep when the change lands. It is neither
    detached nor abandoned: the exec waits for its run.end. B is admitted but gets no step."""
    repo = make_repo(tmp_path)
    _second_spec(repo)
    cfg = make_cfg(tmp_path, repo,
                   script(tmp_path, [dict(impl_done(), sleep_s=1), review()]))
    cfg["agentic"]["profiles"]["balanced"]["slots"] = 2
    a = added(cfg, monkeypatch)
    state, seen, calls = {"changed": False}, {}, []
    monkeypatch.setattr(Q, "code_changes", lambda r, since: (
        ("newsha", ["src/wt/x.py"]) if state["changed"] else (since, [])))

    def fake_exec(exe, argv, env):
        calls.append(1)
        seen["a_ends"] = len(S.read_ledger(cfg, a, types=["run.end"]))
        seen["stop"] = [r for r in journal(cfg, "stop")]

    monkeypatch.setattr(Q, "_EXEC", fake_exec)

    def on_tick(report):
        if not state["changed"] and a in Q._LAST_POOL.ids():
            assert not S.read_ledger(cfg, a, types=["run.end"])   # the implementer is in flight
            seen["b"] = added(cfg, monkeypatch, "SPEC-0002")
            state["changed"] = True
        if "b" in seen and S.read_state(cfg, seen["b"])["state"] != "QUEUED":
            seen["b_admitted"] = True

    Q.run(cfg, poll_interval_s=0.2, reexec=True, code_root=repo, on_tick=on_tick, max_ticks=100)
    b = seen["b"]
    assert calls and seen["a_ends"] == 1                  # the exec followed A's harvest
    stop, = seen["stop"]
    assert stop["reason"] == "code_changed"
    assert stop["detached"] == [] and not stop.get("abandoned")
    assert seen.get("b_admitted")
    assert not S.read_ledger(cfg, b, types=["run.start"])  # B ran no persona step while draining


def test_close_step_is_not_started_while_draining(tmp_path, monkeypatch):
    """AC1: A reaches CLOSING during draining ticks while B's held gate blocks the exec. With a
    detachable runner, only the draining skip of "close" keeps `prepare_close` from starting."""
    repo = make_repo(tmp_path,
                     test_gate=(f"if grep -qs \"$(basename \"$PWD\")\" {tmp_path}/holdid; then "
                                f"echo $$ > {tmp_path}/g1.pid; "
                                f"while [ ! -f {tmp_path}/g1.rel ]; do sleep 0.05; done; fi; "
                                "test -f feature.txt"))
    _second_spec(repo)
    cfg = make_cfg(tmp_path, repo,
                   script(tmp_path, [impl_done(), review()]))
    cfg["agentic"]["profiles"]["balanced"]["slots"] = 2
    cfg_script = json.loads((tmp_path / "script.json").read_text())
    cfg_script[1]["sleep_s"] = 1                          # A's review is in flight at detection
    (tmp_path / "script.json").write_text(json.dumps(cfg_script))
    a = added(cfg, monkeypatch)
    b = added(cfg, monkeypatch, "SPEC-0002")
    (tmp_path / "holdid").write_text(b)                  # only B's gate is held
    runner = Det(cfg)
    runner.release.set()
    state, seen, calls, closes = {"changed": False}, {"closing": 0}, [], []
    monkeypatch.setattr(Q, "code_changes", lambda r, since: (
        ("newsha", ["src/wt/x.py"]) if state["changed"] else (since, [])))
    monkeypatch.setattr(Q, "_EXEC", lambda exe, argv, env: calls.append(1))
    real_prepare = Q.CL.prepare_close
    monkeypatch.setattr(Q.CL, "prepare_close", lambda *a_, **k: (closes.append(1),
                                                                   real_prepare(*a_, **k))[1])

    def on_tick(report):
        if not state["changed"] and pid_file(tmp_path) and a in Q._LAST_POOL.ids() \
                and S.read_state(cfg, a)["state"] == "REVIEWING":
            state["changed"] = True                       # B's gate held, A's review in flight
        if state["changed"] and S.read_state(cfg, a)["state"] == "CLOSING":
            seen["closing"] += 1
            if seen["closing"] >= 3:                      # several draining ticks went by
                (tmp_path / "g1.rel").write_text("")

    Q.run(cfg, poll_interval_s=0.2, reexec=True, code_root=repo, on_tick=on_tick,
          runner=runner, max_ticks=200)
    assert calls and seen["closing"] >= 3
    assert S.read_state(cfg, a)["state"] == "CLOSING"     # left for the new process
    assert not closes                                      # no prepare_close before the exec
    assert not S.read_ledger(cfg, a, types=["close"])
    stop, = journal(cfg, "stop")
    assert stop["reason"] == "code_changed"
