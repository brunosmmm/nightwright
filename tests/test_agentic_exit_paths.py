"""SPEC-0229: runner exit paths and draining ticks. Gates are real subprocesses that record
their pid and wait for a release file; stops are driven by setting the loop's `stop` event,
exactly what the signal handler does."""
import json
import os
import signal
import threading
import time
from pathlib import Path

import pytest

from test_agentic_adopt import world  # noqa: F401  (fixture: ClaudeRunner + fake claude)
from test_agentic_adopt import _code_world, start_impl, run_ends
from test_agentic_e2e import added, impl_done, make_cfg, make_repo, review, script
from test_agentic_nonblocking import _second_spec
from test_agentic_runproc import pid_alive, until
from wt.agentic import cli_queue
from wt.agentic import gates as G
from wt.agentic import lease as L
from wt.agentic import queue as Q
from wt.agentic import runner as R
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

    def side():
        until(lambda: when() and "stop" in holder, timeout=30) if False else None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if "stop" in holder and when():
                holder["stop"].set()
                return
            time.sleep(0.02)

    t = threading.Thread(target=side, daemon=True)
    t.start()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Q, "tick", tick)
        t0 = time.monotonic()
        n = Q.run(cfg, poll_interval_s=0.05, **kw)
    t.join(5)
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
    root, state = None, {"changed": False}
    repo, cfg, a = gate_world(tmp_path, monkeypatch)
    _second_spec(repo)
    cfg["agentic"]["profiles"]["balanced"]["slots"] = 2
    b = None
    real = Q.code_changes
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
    os.environ[L.HANDOVER_ENV] = "x"
    os.environ.pop(L.HANDOVER_ENV)


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


def test_draining_submits_detachable_step_and_execs_promptly(tmp_path, monkeypatch):
    repo, cfg, a = gate_world(tmp_path, monkeypatch, entries=[impl_done(), review()])
    _second_spec(repo)
    cfg["agentic"]["profiles"]["balanced"]["slots"] = 2
    runner = Det(cfg)
    state = {"changed": False}
    monkeypatch.setattr(Q, "code_changes", lambda r, since: (
        ("newsha", ["src/wt/x.py"]) if state["changed"] else (since, [])))
    calls, seen = [], {}
    monkeypatch.setattr(Q, "_EXEC", lambda exe, argv, env: calls.append(time.monotonic()))
    started = []

    def on_tick(report):
        started.extend(report.started)
        if not state["changed"] and pid_file(tmp_path):         # A is in its (held) gate
            b = added(cfg, monkeypatch, "SPEC-0002")
            seen["b"] = b
            runner.release.clear()
            state["changed"] = True
        if seen.get("b") and "b_run" not in seen:
            runs = S.read_ledger(cfg, seen["b"], types=["run.start"])
            if runs:
                seen["b_run"] = runs[0]["run_id"]
                seen["rel"] = time.monotonic()
                (tmp_path / "g1.rel").write_text("")

    runner.release.set()                                         # A's implementer finishes
    Q.run(cfg, poll_interval_s=1, reexec=True, code_root=repo, on_tick=on_tick,
          runner=runner, max_ticks=100)
    runner.release.set()
    assert calls and seen.get("b_run"), (seen, started)
    assert calls[0] - seen["rel"] < 3.5
    stop, = journal(cfg, "stop")
    assert stop["reason"] == "code_changed"
    assert stop["detached"] == [{"campaign": seen["b"], "run_ids": [seen["b_run"]]}]
    assert len(S.read_ledger(cfg, a, types=["gate"])) == 1


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


def test_cli_exits_70_on_leftover_threads(tmp_path, monkeypatch, capsys):
    exits = []
    monkeypatch.setattr(cli_queue.os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(Q, "leftover_threads", lambda: [object()])
    cli_queue._exit_leftover()
    assert exits == [70]


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


def test_cli_prints_traceback_and_exits_70(tmp_path, monkeypatch, capsys):
    from click.testing import CliRunner
    from wt.cli import cli
    exits = []
    monkeypatch.setattr("wt.cli.load_config", lambda: {"data_dir": str(tmp_path)})

    def boom(cfg, **kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(Q, "run", boom)
    monkeypatch.setattr(Q, "leftover_threads", lambda: [object()])
    monkeypatch.setattr(cli_queue.os, "_exit", lambda code: exits.append(code))
    CliRunner().invoke(cli, ["queue", "run", "--once"])
    assert exits == [70]


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
