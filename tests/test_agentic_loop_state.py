"""SPEC-0228: the loop state the runner publishes in its lease — `poll_interval_s`, `tick_at`,
`in_flight`, `slot_freed_at` and the declared long main-thread steps (`busy`: open, close,
drain). A spy on `Lease.update_info` / `pop_info` records every write with the `busy` value
in force at that moment; stamps have second resolution, so ordering is read from the calls."""
import os
import subprocess

import pytest

from test_agentic_e2e import (added, impl_done, make_cfg, make_repo, review, script)
from test_agentic_reexec import _code_repo, _commit
from wt.agentic import close as CL
from wt.agentic import events as E
from wt.agentic import lease as L
from wt.agentic import queue as Q
from wt.agentic import store as S
from wt.agentic.gitops import GitError


@pytest.fixture
def spy(monkeypatch):
    """Every `update_info` call as `(kwargs, busy in force)`; every `pop_info` as `("pop", keys)`,
    every `end_busy` as `("end_busy", restored busy)`."""
    calls = []
    real_update, real_pop, real_end = L.Lease.update_info, L.Lease.pop_info, L.Lease.end_busy

    def update_info(self, **kv):
        calls.append((dict(kv), self.info.get("busy")))
        return real_update(self, **kv)

    def pop_info(self, *keys):
        calls.append(("pop", keys))
        return real_pop(self, *keys)

    def end_busy(self, prev=None):
        calls.append(("end_busy", prev))
        return real_end(self, prev)

    monkeypatch.setattr(L.Lease, "end_busy", end_busy)
    monkeypatch.setattr(L.Lease, "update_info", update_info)
    monkeypatch.setattr(L.Lease, "pop_info", pop_info)
    return calls


def _env(tmp_path, monkeypatch, entries=None, **kw):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, entries or [impl_done(), review()]), **kw)
    return repo, cfg, added(cfg, monkeypatch)


def test_run_stamps_loop_state_before_emit(tmp_path, monkeypatch):
    _repo, cfg, _cid = _env(tmp_path, monkeypatch)
    seen = []
    Q.run(cfg, once=True, poll_interval_s=0.05, on_tick=lambda r: seen.append(L.read_lease(cfg)))
    (info,) = seen
    assert info["poll_interval_s"] == 0.05 and info["tick_at"] and info["slot_freed_at"]
    assert isinstance(info["in_flight"], list)
    assert "busy" not in info


def test_run_stamps_slot_freed_at_on_drop(tmp_path, monkeypatch, spy):
    _repo, cfg, cid = _env(tmp_path, monkeypatch, profile={"slots": 1})   # full -> free (SPEC-0263)
    Q.run(cfg, until_idle=True, poll_interval_s=0.01)
    assert S.read_state(cfg, cid)["state"] == "MERGED"
    assert "slot_freed_at" in spy[0][0] and spy[0][0]["poll_interval_s"] == 0.01   # at start
    ticks = [kw for kw, _ in spy if isinstance(kw, dict) and "tick_at" in kw]
    assert ticks and "slot_freed_at" not in ticks[0]            # admission raises the count
    assert "slot_freed_at" in ticks[-1]                          # the merge tick lowered it
    assert sum("slot_freed_at" in kw for kw in ticks) == 1


class _Lease:
    def __init__(self):
        self.calls = []

    def update_info(self, **kv):
        self.calls.append(kv)


def _slot_watch(monkeypatch, counts, slots=3):
    watch = Q._SlotWatch.__new__(Q._SlotWatch)
    seq = iter(counts)
    watch.cfg, watch.pool = {}, type("P", (), {"ids": lambda self: set()})()
    watch.count = lambda: next(seq)
    watch.last = next(seq)
    monkeypatch.setattr(Q, "slots_for", lambda cfg, name=None: slots)
    return watch


def test_slot_freed_at_only_on_full_to_free(monkeypatch):
    watch, lease = _slot_watch(monkeypatch, [3, 2, 1, 2, 1], slots=3), _Lease()
    for _ in range(4):
        watch.check(lease)
    assert ["slot_freed_at" in kw for kw in lease.calls] == [True, False, False, False]


@pytest.mark.parametrize("counts, stamped", [([4, 3], False), ([4, 2], True)])
def test_slot_freed_at_over_capacity(monkeypatch, counts, stamped):
    watch, lease = _slot_watch(monkeypatch, counts, slots=3), _Lease()     # cap 3: 4 -> 3 is still full
    watch.check(lease)
    assert ("slot_freed_at" in lease.calls[0]) is stamped


def test_slots_full_at_published_while_full_and_cleared_on_a_free_slot(monkeypatch):
    watch, lease = _slot_watch(monkeypatch, [3, 3, 2, 3], slots=3), _Lease()
    for _ in range(3):
        watch.check(lease)
    full = [kw["slots_full_at"] is not None for kw in lease.calls]
    assert full == [True, False, True]


def test_slot_watch_survives_slots_for_error(monkeypatch):
    watch, lease = _slot_watch(monkeypatch, [3, 2]), _Lease()

    def boom(cfg, name=None):
        raise OSError("half-written profile")
    monkeypatch.setattr(Q, "slots_for", boom)
    watch.check(lease, tick_at="t")
    (kw,) = lease.calls
    assert kw["tick_at"] == "t" and "slot_freed_at" not in kw and watch.last == 2


def test_run_publishes_real_pool_ids_in_flight(tmp_path, monkeypatch, spy):
    """The watchdog's `_occupied` depends on `in_flight` holding the pool's ids."""
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [dict(impl_done(), sleep_s=0.5), review()]))
    cid = added(cfg, monkeypatch)
    Q.run(cfg, until_idle=True, poll_interval_s=0.05)
    assert S.read_state(cfg, cid)["state"] == "MERGED"
    ticks = [kw["in_flight"] for kw, _ in spy if isinstance(kw, dict) and "tick_at" in kw]
    assert [cid] in ticks                         # the tick while the run was in flight
    assert ticks[-1] == []


def test_close_sets_and_clears_busy(tmp_path, monkeypatch, spy):
    _repo, cfg, cid = _env(tmp_path, monkeypatch)
    seen = []
    real = CL.finish_close

    def finish_close(*a, **kw):
        seen.append(L.read_lease(cfg).get("busy"))
        return real(*a, **kw)

    monkeypatch.setattr(CL, "finish_close", finish_close)
    Q.run(cfg, until_idle=True, poll_interval_s=0.01)
    (busy,) = seen
    assert busy["kind"] == "close" and busy["campaign"] == cid
    assert busy["budget_s"] == E.settings(cfg)["gate_budget_s"] and busy["since"]
    assert ("end_busy", None) in spy


def test_close_busy_cleared_when_close_raises(tmp_path, monkeypatch):
    _repo, cfg, cid = _env(tmp_path, monkeypatch)
    seen = []

    def finish_close(*a, **kw):
        seen.append(L.read_lease(cfg).get("busy"))
        raise GitError(["git"], 1, "boom")

    monkeypatch.setattr(CL, "finish_close", finish_close)
    cleared = []
    Q.run(cfg, until_idle=True, poll_interval_s=0.01,
          on_tick=lambda r: cleared.append("busy" in L.read_lease(cfg)))
    assert seen and seen[0]["kind"] == "close"
    assert not any(cleared)                                     # no tick ever saw a stale busy
    assert S.read_state(cfg, cid)["state"] == "PARKED"


def test_open_sets_and_clears_busy(tmp_path, monkeypatch):
    _repo, cfg, cid = _env(tmp_path, monkeypatch)
    seen = []
    real_run = subprocess.run

    def run(cmd, *a, **kw):
        if cmd == "touch .setup-ran":
            seen.append(L.read_lease(cfg).get("busy"))
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(Q.subprocess, "run", run)
    after = []
    Q.run(cfg, once=True, poll_interval_s=0.01,
          on_tick=lambda r: after.append(L.read_lease(cfg)))
    (busy,) = seen
    assert busy["kind"] == "open" and busy["campaign"] == cid
    assert busy["budget_s"] == E.settings(cfg)["open_budget_s"] == 600
    assert "busy" not in after[0]


def test_failing_open_setup_leaves_no_busy(tmp_path, monkeypatch):
    _repo, cfg, cid = _env(tmp_path, monkeypatch)
    real_run = subprocess.run

    def run(cmd, *a, **kw):
        if cmd == "touch .setup-ran":
            return subprocess.CompletedProcess(cmd, 1, "", "setup failed")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(Q.subprocess, "run", run)
    after = []
    Q.run(cfg, once=True, poll_interval_s=0.01,
          on_tick=lambda r: after.append(L.read_lease(cfg)))
    assert S.read_state(cfg, cid)["state"] == "PARKED"
    assert "busy" not in after[0]


def test_reopen_of_opening_campaign_is_busy_too(tmp_path, monkeypatch):
    """The step-2 call site of `_do_open` (an item already in OPENING) is declared as well."""
    _repo, cfg, cid = _env(tmp_path, monkeypatch)
    st = S.read_state(cfg, cid)
    st["state"] = "OPENING"
    S.write_state(cfg, cid, st)
    seen = []
    real_run = subprocess.run

    def run(cmd, *a, **kw):
        if cmd == "touch .setup-ran":
            seen.append(L.read_lease(cfg).get("busy"))
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(Q.subprocess, "run", run)
    Q.run(cfg, once=True, poll_interval_s=0.01)
    (busy,) = seen
    assert busy["kind"] == "open" and busy["campaign"] == cid


def test_busy_without_lease_does_nothing():
    with Q._busy(None, "open", budget_s=1):
        pass


def test_drain_stamps_busy_in_flight_and_slot_freed_at(tmp_path, monkeypatch, spy):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [dict(impl_done(), sleep_s=1.0), review()]),
                   profile={"slots": 1})            # one slot, so the drain's drop is full -> free
    a = added(cfg, monkeypatch)
    real_tick = Q.tick

    def tick(*args, **kw):
        report = real_tick(*args, **kw)
        if report.started:                         # an exit (max_ticks) lands mid-run; a park is pending
            with S.item_lock(cfg, a):
                st = S.read_state(cfg, a)
                st["park_requested"] = {"reason": "human", "detail": "drain test"}
                S.write_state(cfg, a, st)
        return report

    seen = {}

    def fake_exec(exe, argv, env):
        seen["lease"] = dict(L.read_lease(cfg))
        seen["env"] = env

    monkeypatch.setattr(Q, "tick", tick)
    monkeypatch.setattr(Q, "_EXEC", fake_exec)
    Q.run(cfg, max_ticks=2, poll_interval_s=0.05)     # the exit drain, SPEC-0229
    assert S.read_state(cfg, a)["state"] == "PARKED"
    drain_calls = [(kw, b) for kw, b in spy
                   if isinstance(kw, dict) and b and b["kind"] == "drain"]
    assert drain_calls, spy
    harvest = [kw for kw, _ in drain_calls if "in_flight" in kw]
    assert harvest and harvest[-1]["in_flight"] == [] and "slot_freed_at" in harvest[-1]
    listed = [i for i, (kw, _) in enumerate(spy) if isinstance(kw, dict)
              and kw.get("in_flight") == [a]]
    last = max(i for i, (kw, _) in enumerate(spy) if isinstance(kw, dict) and "in_flight" in kw)
    assert listed and listed[0] < last           # the flight was published before it emptied
    assert all("tick_at" not in kw for kw, _ in drain_calls)    # a drain does not tick
    busy = drain_calls[0][1]
    assert busy["budget_s"] > 0 and busy["campaign"] is None
    L.acquire(cfg, ["t"]).release()


def test_runner_journal_silent_suppressed_during_open_and_drain(tmp_path):
    from test_agentic_events import _cfg
    from test_agentic_watchdog_gaps import at, iso
    cfg = _cfg(tmp_path)
    sett = E.settings(cfg)
    lease = L.acquire(cfg, ["test"], ttl_s=10 ** 6)
    try:
        lease.update_info(poll_interval_s=20, tick_at=iso(0))
        for kind in ("open", "drain"):
            lease.update_info(busy={"kind": kind, "campaign": None, "since": iso(0),
                                    "budget_s": 600})
            assert E.runner_silence(cfg, at(300), sett) is None
            assert E.runner_silence(cfg, at(660), sett) is None        # budget + overdue_grace_s
            d = E.runner_silence(cfg, at(661), sett)
            assert d["busy"]["kind"] == kind
    finally:
        lease.release()


def test_ending_a_step_stamps_activity_in_the_same_write(tmp_path, monkeypatch):
    """Between clearing `busy` and the end-of-tick `tick_at` the watchdog must not see a
    quiet loop: `_busy` drops `busy` and stamps `busy_ended_at` together."""
    from test_agentic_events import _cfg
    from test_agentic_watchdog_gaps import at, iso
    cfg = _cfg(tmp_path)
    sett = E.settings(cfg)
    lease = L.acquire(cfg, ["test"], ttl_s=10 ** 6)
    try:
        lease.update_info(poll_interval_s=20, tick_at=iso(-20))
        with Q._busy(lease, "open", campaign="c1", budget_s=600):
            assert E.runner_silence(cfg, at(100), sett) is None      # within the budget
            monkeypatch.setattr(L, "now_iso", lambda now=None: iso(100))
        info = L.read_lease(cfg)
        assert "busy" not in info and info["busy_ended_at"] == iso(100)
        assert L.lease_status(cfg)["busy_ended_at"] == iso(100)
        assert E.runner_silence(cfg, at(101), sett) is None
        assert E.runner_silence(cfg, at(160), sett) is None
        d = E.runner_silence(cfg, at(161), sett)                      # a real stall again
        assert d["busy_ended_at"] == iso(100) and d["silent_s"] == 61
        keys = {k for k, *_ in E.watchdog_checks(cfg, at(161))}
        assert f"runner_journal_silent:{lease.info['pid']}:{iso(100)}" in keys
    finally:
        lease.release()


def test_unparseable_busy_since_is_over_budget(tmp_path):
    from test_agentic_events import _cfg
    from test_agentic_watchdog_gaps import at, iso
    cfg = _cfg(tmp_path)
    lease = L.acquire(cfg, ["test"], ttl_s=10 ** 6)
    try:
        lease.update_info(poll_interval_s=20, tick_at=iso(0),
                          busy={"kind": "open", "campaign": None, "since": "garbage",
                                "budget_s": 600})
        d = E.runner_silence(cfg, at(100), E.settings(cfg))
        assert d is not None and d["busy"]["since"] == "garbage"
    finally:
        lease.release()


def test_nested_busy_restores_the_outer_declaration(tmp_path):
    from test_agentic_events import _cfg
    lease = L.acquire(_cfg(tmp_path), ["test"], ttl_s=10 ** 6)
    try:
        with Q._busy(lease, "drain", budget_s=900):
            with Q._busy(lease, "close", campaign="c1", budget_s=1800):
                assert lease.info["busy"]["kind"] == "close"
            assert lease.info["busy"]["kind"] == "drain" and lease.info["busy"]["budget_s"] == 900
        assert "busy" not in lease.info
    finally:
        lease.release()


def test_drain_keeps_busy_after_a_harvested_close(tmp_path, monkeypatch):
    """A close flight harvested inside the drain runs `_busy("close")` (finish_close); the
    drain's declaration must survive it while another flight is still in flight."""
    from test_agentic_events import _cfg
    cfg = _cfg(tmp_path)
    lease = L.acquire(cfg, ["test"], ttl_s=10 ** 6)

    class Fl:
        tasks, runs = [], []
        ctx = type("C", (), {"stop": type("S", (), {"is_set": staticmethod(lambda: False)})()})()

    class FakePool:
        def __init__(self):
            self.flights = {"close1": Fl(), "impl1": Fl()}

        def wait_any(self, timeout):
            pass

        def ids(self):
            return set(self.flights)

        def try_detach_all(self):                  # SPEC-0229: every drain tries to detach
            return None

    pool, seen = FakePool(), []

    def harvest(p, report):
        if "close1" in p.flights:                  # the close finishes first, impl1 stays
            del p.flights["close1"]
            with Q._busy(lease, "close", campaign="close1", budget_s=1800):
                pass
            seen.append(dict(lease.info["busy"]))
        else:
            p.flights.clear()

    monkeypatch.setattr(Q, "_harvest", harvest)
    monkeypatch.setattr(Q, "_refresh_backlog", lambda cfg: None)
    try:
        Q._drain(cfg, pool, lease)
        assert seen and seen[0]["kind"] == "drain"
        assert "busy" not in lease.info and "draining" not in lease.info
    finally:
        lease.release()


def test_drain_budget_counts_close_flights_as_gate_budget(tmp_path, monkeypatch):
    from test_agentic_events import _cfg
    cfg = _cfg(tmp_path)
    lease = L.acquire(cfg, ["test"], ttl_s=10 ** 6)
    monkeypatch.setattr(Q, "_waiting_on", lambda c, p: [
        {"campaign": "c", "kind": "close", "run_ids": [], "timeout_s": None},
        {"campaign": "d", "kind": "implement", "run_ids": [], "timeout_s": 60}])
    seen = []

    class FakePool:
        flights = {}

    real = Q._busy

    def busy(lease, kind, **kw):
        seen.append(kw["budget_s"])
        return real(lease, kind, **kw)

    monkeypatch.setattr(Q, "_busy", busy)
    try:
        Q._drain(cfg, FakePool(), lease)
        assert seen == [E.settings(cfg)["gate_budget_s"]]
    finally:
        lease.release()


def test_config_reload_republishes_poll_interval(tmp_path, monkeypatch, spy):
    repo, cfg, _ = _env(tmp_path, monkeypatch)
    cfg["agentic"]["poll_interval_s"] = 20
    new_cfg = dict(cfg, agentic=dict(cfg["agentic"], poll_interval_s=120))

    class Watch:
        n = 0

        def check(self, c, lease, live):
            self.n += 1
            return new_cfg if self.n == 2 else c

    monkeypatch.setattr(Q, "ConfigWatch", lambda *a, **k: Watch())
    Q.run(cfg, max_ticks=3, poll_interval_s=None, config_loader=lambda: new_cfg)
    seen = [kw["poll_interval_s"] for kw, _ in spy if isinstance(kw, dict) and "poll_interval_s" in kw]
    assert seen[:1] == [20.0] and 120.0 in seen


def test_lease_released_when_startup_raises(tmp_path, monkeypatch):
    """SPEC-0262: anything between `L.acquire` and the main loop that raises releases the
    lease, so the next runner takes it at once."""
    _repo, cfg, _cid = _env(tmp_path, monkeypatch)

    def boom(self, *a, **k):
        raise OSError("startup failed")

    monkeypatch.setattr(Q._SlotWatch, "__init__", boom)
    with pytest.raises(OSError, match="startup failed"):
        Q.run(cfg, once=True, poll_interval_s=0.05)
    assert L.lease_status(cfg)["held"] is False
    lease = L.acquire(cfg, ["wt", "queue", "run"])
    lease.release()


# ---- SPEC-0313: progress marks inside a tick -----------------------------------------------

class _ClockLease:
    """A lease double that records every `progress_at` write with the fake monotonic clock."""

    def __init__(self, clock):
        self.clock, self.info, self.writes = clock, {}, []

    def update_info(self, **kv):
        self.info.update(kv)
        if "progress_at" in kv:
            self.writes.append((kv["progress_at"], self.clock[0]))


@pytest.fixture
def fake_clock(monkeypatch):
    """`[now]` driving `Q.time.monotonic` and `store.now_iso` (as a sortable string)."""
    import time as real_time
    clock = [1000.0]

    class _Time:
        monotonic = staticmethod(lambda: clock[0])

        def __getattr__(self, name):
            return getattr(real_time, name)

    monkeypatch.setattr(Q, "time", _Time())
    monkeypatch.setattr(S, "now_iso", lambda now=None: f"{clock[0]:012.1f}")
    return clock


def _gaps(writes):
    return [b[1] - a[1] for a, b in zip(writes, writes[1:])]


def test_tick_marks_progress_between_campaigns(tmp_path, monkeypatch, fake_clock):
    _repo, cfg, _cid = _env(tmp_path, monkeypatch)
    for i in range(6):
        S.create_campaign(cfg, {"id": f"SPEC-09{i:02d}-20261025-aaaa", "spec_id": f"SPEC-09{i:02d}",
                                "project": "Meta-Tools", "profile": "balanced",
                                "state": "IMPLEMENTING", "repo": {"base": "main"},
                                "cost": {"total_usd": 0.0, "cap_usd": 10.0}})

    def plan(st, profile, pcfg):
        fake_clock[0] += 5.0                  # each busy campaign takes 5 s to plan
        return Q.Step("none")

    monkeypatch.setattr(Q, "plan_step", plan)
    lease = _ClockLease(fake_clock)
    Q.tick(cfg, lease=lease)
    stamps = [w[0] for w in lease.writes]
    assert len(stamps) > 1 and stamps == sorted(set(stamps))          # strictly increasing
    assert all(g >= Q._PROGRESS_MIN_INTERVAL_S for g in _gaps(lease.writes))
    assert len([w for w in lease.writes if w[1] > 1000.0]) >= 6      # advances during the tick


def test_tick_marks_progress_in_sweep_and_readiness_loops(tmp_path, monkeypatch, fake_clock):
    _repo, cfg, _cid = _env(tmp_path, monkeypatch)
    for i in range(8):
        S.create_campaign(cfg, {"id": f"SPEC-08{i:02d}-20261025-aaaa", "spec_id": f"SPEC-08{i:02d}",
                                "project": "Meta-Tools", "profile": "balanced",
                                "state": "QUEUED",
                                "repo": {"base": "main"},
                                "cost": {"total_usd": 0.0, "cap_usd": 10.0}})
    for i in range(4):
        S.create_campaign(cfg, {"id": f"SPEC-07{i:02d}-20261025-aaaa", "spec_id": f"SPEC-07{i:02d}",
                                "project": "Meta-Tools", "profile": "balanced",
                                "state": "MERGED", "repo": {"base": "main"},
                                "cost": {"total_usd": 0.0, "cap_usd": 10.0}})
    real_sweep = S.sweep_campaign_tmp

    def sweep(cfg_, cid):
        fake_clock[0] += 5.0
        return real_sweep(cfg_, cid)

    def readiness(cfg_, st):
        fake_clock[0] += 5.0
        return False, ["blocked"]

    monkeypatch.setattr(S, "sweep_campaign_tmp", sweep)
    monkeypatch.setattr(Q, "backlog_readiness", readiness)
    lease = _ClockLease(fake_clock)
    Q.tick(cfg, lease=lease)
    elapsed = fake_clock[0] - 1000.0
    assert elapsed > 60.0                                   # the loops alone outlast the threshold
    assert len(lease.writes) >= elapsed / 5.0               # a mark per swept and per queued item
    assert max(_gaps(lease.writes)) < 60.0                 # 3 x 20 s: no gap the watchdog would flag


def test_ctx_progress_is_noop_without_lease(tmp_path):
    ctx = Q._Ctx({}, lease=None)
    ctx.progress()                                           # does not raise, writes nothing


def test_ctx_progress_is_throttled(fake_clock):
    lease = _ClockLease(fake_clock)
    ctx = Q._Ctx({}, lease=lease)
    for _ in range(5):
        ctx.progress()
    assert len(lease.writes) == 1                            # same instant: one write
    fake_clock[0] += Q._PROGRESS_MIN_INTERVAL_S - 0.1
    ctx.progress()
    assert len(lease.writes) == 1
    fake_clock[0] += 0.1
    ctx.progress()
    assert len(lease.writes) == 2


def test_run_stamps_last_tick_s(tmp_path, monkeypatch, fake_clock):
    _repo, cfg, _cid = _env(tmp_path, monkeypatch)
    real_tick = Q.tick

    def slow_tick(*a, **kw):
        report = real_tick(*a, **kw)
        fake_clock[0] += 7.34                                 # the tick took 7.34 s
        return report

    monkeypatch.setattr(Q, "tick", slow_tick)
    seen = []
    Q.run(cfg, once=True, poll_interval_s=0.05, on_tick=lambda r: seen.append(L.read_lease(cfg)))
    (info,) = seen
    assert info["last_tick_s"] == 7.3 and info["tick_at"]
