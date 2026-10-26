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
    """Every `update_info` call as `(kwargs, busy in force)`; every `pop_info` as `("pop", keys)`."""
    calls = []
    real_update, real_pop = L.Lease.update_info, L.Lease.pop_info

    def update_info(self, **kv):
        calls.append((dict(kv), self.info.get("busy")))
        return real_update(self, **kv)

    def pop_info(self, *keys):
        calls.append(("pop", keys))
        return real_pop(self, *keys)

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
    _repo, cfg, cid = _env(tmp_path, monkeypatch)
    Q.run(cfg, until_idle=True, poll_interval_s=0.01)
    assert S.read_state(cfg, cid)["state"] == "MERGED"
    assert "slot_freed_at" in spy[0][0] and spy[0][0]["poll_interval_s"] == 0.01   # at start
    ticks = [kw for kw, _ in spy if isinstance(kw, dict) and "tick_at" in kw]
    assert ticks and "slot_freed_at" not in ticks[0]            # admission raises the count
    assert "slot_freed_at" in ticks[-1]                          # the merge tick lowered it
    assert sum("slot_freed_at" in kw for kw in ticks) == 1


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
    assert ("pop", ("busy",)) in spy


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
    root = _code_repo(tmp_path)
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [dict(impl_done(), sleep_s=1.0), review()]))
    a = added(cfg, monkeypatch)
    real_tick = Q.tick

    def tick(*args, **kw):
        report = real_tick(*args, **kw)
        if report.started:                         # code lands mid-run; a park is pending
            with S.item_lock(cfg, a):
                st = S.read_state(cfg, a)
                st["park_requested"] = {"reason": "human", "detail": "drain test"}
                S.write_state(cfg, a, st)
            _commit(root, "src/wt/x.py", "A = 9\n", "code")
        return report

    seen = {}

    def fake_exec(exe, argv, env):
        seen["lease"] = dict(L.read_lease(cfg))
        seen["env"] = env

    monkeypatch.setattr(Q, "tick", tick)
    monkeypatch.setattr(Q, "_EXEC", fake_exec)
    Q.run(cfg, max_ticks=50, poll_interval_s=0.05, reexec=True, code_root=root)
    assert S.read_state(cfg, a)["state"] == "PARKED"
    drain_calls = [(kw, b) for kw, b in spy
                   if isinstance(kw, dict) and b and b["kind"] == "drain"]
    assert drain_calls, spy
    harvest = [kw for kw, _ in drain_calls if "in_flight" in kw]
    assert harvest and harvest[-1]["in_flight"] == [] and "slot_freed_at" in harvest[-1]
    assert all("tick_at" not in kw for kw, _ in drain_calls)    # a drain does not tick
    busy = drain_calls[0][1]
    assert busy["budget_s"] > 0 and busy["campaign"] is None
    assert "busy" not in seen["lease"] and "draining" not in seen["lease"]   # gone by the exec
    os.environ[L.HANDOVER_ENV] = seen["env"][L.HANDOVER_ENV]
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
