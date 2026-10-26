"""SPEC-0224: the runner announces a drain in its lease info, the out-of-process watchdog turns a
long one into `runner_draining`, and the drain keeps the stored backlog readiness fresh so
`ready_not_admitted` still works."""
import copy
import datetime as dt
import json
import os
import threading

from test_agentic_e2e import (_git, _write, added, impl_done, make_cfg, make_repo, review,
                              script, write_review)
from test_agentic_nonblocking import _second_spec
from test_agentic_reexec import _code_repo, _commit
from wt.agentic import events as E
from wt.agentic import lease as L
from wt.agentic import queue as Q
from wt.agentic import store as S
from wt.agentic.runner import select_runner


class GatedRunner:
    """FakeRunner whose runs for a spec block until that spec's event is set."""

    def __init__(self, cfg, *specs):
        self.inner = select_runner(cfg)
        self.gates = {s: threading.Event() for s in specs}

    def run(self, req):
        for spec, ev in self.gates.items():
            if spec in str(req.cwd):
                assert ev.wait(60), f"test never released {spec}"
        return self.inner.run(req)

    def release_all(self):
        for ev in self.gates.values():
            ev.set()


class _Signal:
    """Events set from the runner's own calls (SPEC-0248), so an observer thread waits on what
    the runner did instead of polling files. `watch(name, pred)` arms an event that is set, with
    a copy of the lease info, the first time `pred(info)` holds right after a real
    `Lease.update_info`/`Lease.pop_info`; `backlog_ready(cfg, cid)` arms "B stored ready", set
    when `cid`'s stored `backlog.ready` is true right after a `queue._refresh_backlog`."""

    def __init__(self, monkeypatch):
        self.events, self.preds, self.seen = {}, {}, {}
        real_update, real_pop = L.Lease.update_info, L.Lease.pop_info

        def update_info(lease, **kv):
            real_update(lease, **kv)
            with lease._lock:                        # the heartbeat thread writes info too
                self._check(lease.info)

        def pop_info(lease, *keys):
            real_pop(lease, *keys)
            with lease._lock:
                self._check(lease.info)

        monkeypatch.setattr(L.Lease, "update_info", update_info)
        monkeypatch.setattr(L.Lease, "pop_info", pop_info)
        self.monkeypatch = monkeypatch

    def watch(self, name, pred):
        self.events[name], self.preds[name] = threading.Event(), pred

    def _check(self, info):
        for name, pred in self.preds.items():
            if not self.events[name].is_set() and pred(info):
                self.seen[name] = copy.deepcopy(info)
                self.events[name].set()

    def backlog_ready(self, cfg, cid, name="B stored ready"):
        self.events[name] = threading.Event()
        real = Q._refresh_backlog

        def refresh(c):
            real(c)
            st = S.read_state(cfg, cid)
            if not self.events[name].is_set() and st["backlog"].get("ready"):
                self.seen[name] = st
                self.events[name].set()

        self.monkeypatch.setattr(Q, "_refresh_backlog", refresh)

    def wait(self, name, timeout=60):
        """The info (or state) seen when `name` fired; the timeout is only a hang guard."""
        assert self.events[name].wait(timeout), f"never saw: {name}"
        return self.seen[name]


def _n_waiting(info):
    return len((info.get("draining") or {}).get("waiting_on") or [])


def test_drain_writes_and_clears_lease_draining(tmp_path, monkeypatch):
    root = _code_repo(tmp_path)
    repo = make_repo(tmp_path)
    _second_spec(repo)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(), review()]),
                   profile={"slots": 2})
    a = added(cfg, monkeypatch, "SPEC-0001")
    b = added(cfg, monkeypatch, "SPEC-0002")
    runner = GatedRunner(cfg, "SPEC-0001", "SPEC-0002")
    real_tick = Q.tick

    def tick(*args, **kw):
        report = real_tick(*args, **kw)
        if len(report.started) == 2:                         # code lands mid-run
            _commit(root, "src/wt/x.py", "A = 9\n", "code")
        return report

    seen = {"exec": None}
    sig = _Signal(monkeypatch)
    sig.watch("draining with both flights", lambda i: _n_waiting(i) > 0)
    sig.watch("waiting_on shrinks", lambda i: _n_waiting(i) == 1)
    sig.watch("draining cleared", lambda i: sig.events["draining with both flights"].is_set()
              and "draining" not in i)

    def fake_exec(exe, argv, env):
        seen["exec"] = {"lease": dict(L.read_lease(cfg)), "env": env}

    def observer():
        try:
            sig.wait("draining with both flights")
            runner.gates["SPEC-0001"].set()
            sig.wait("waiting_on shrinks")
        finally:
            runner.release_all()

    monkeypatch.setattr(Q, "tick", tick)
    monkeypatch.setattr(Q, "_EXEC", fake_exec)
    side = threading.Thread(target=observer, daemon=True)
    side.start()
    Q.run(cfg, max_ticks=50, poll_interval_s=0.05, reexec=True, code_root=root, runner=runner)
    side.join(60)
    assert not side.is_alive()
    first = sig.wait("draining with both flights")["draining"]
    second = sig.wait("waiting_on shrinks")["draining"]
    sig.wait("draining cleared")
    assert first["reason"] == "code_changed" and first["since"]
    assert {w["campaign"] for w in first["waiting_on"]} == {a, b}
    for w in first["waiting_on"]:
        assert w["kind"] == "implement" and w["run_ids"] == ["001-implementer"]
        assert isinstance(w["timeout_s"], (int, float)) and w["timeout_s"] > 0
    assert [w["campaign"] for w in second["waiting_on"]] == [b]
    assert second["since"] == first["since"]
    assert seen["exec"] is not None
    assert "draining" not in seen["exec"]["lease"]           # gone by the exec
    os.environ[L.HANDOVER_ENV] = seen["exec"]["env"][L.HANDOVER_ENV]
    lease = L.acquire(cfg, ["t"])
    try:
        assert "draining" not in L.read_lease(cfg) and L.lease_status(cfg)["draining"] is None
    finally:
        lease.release()


def test_handover_drops_draining(tmp_path):
    cfg = make_cfg(tmp_path, make_repo(tmp_path), script(tmp_path, []))
    lease = L.acquire(cfg, ["t"])
    lease.update_info(draining={"reason": "code_changed", "since": S.now_iso(), "waiting_on": []})
    assert L.lease_status(cfg)["draining"]["reason"] == "code_changed"
    env = lease.handover()
    assert "draining" not in L.read_lease(cfg)               # the old image already dropped it
    # a stale file that still has the key (crash in between) is cleaned by the adopter
    info = L.read_lease(cfg)
    info["draining"] = {"reason": "x", "since": S.now_iso(), "waiting_on": []}
    L._atomic_write(L.lease_path(cfg), json.dumps(info))
    os.environ[L.HANDOVER_ENV] = env[L.HANDOVER_ENV]
    adopted = L.acquire(cfg, ["t"])
    try:
        assert "draining" not in adopted.info and "draining" not in L.read_lease(cfg)
        assert L.lease_status(cfg)["draining"] is None
    finally:
        adopted.release()


def test_watchdog_runner_draining_after_grace(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    cid = added(cfg, monkeypatch)                            # QUEUED and ready
    now = dt.datetime.now().astimezone().replace(microsecond=0)
    lease = L.acquire(cfg, ["t"])

    def drains(at):
        return [e for e in E.sync(cfg, now=at) if e["type"] == "runner_draining"]

    try:
        assert drains(now) == []                             # no drain recorded
        since = (now - dt.timedelta(seconds=30)).isoformat(timespec="seconds")
        waiting = [{"campaign": "SPEC-0009-x", "kind": "implement", "run_ids": ["001-implementer"],
                    "timeout_s": 1800}]
        lease.update_info(draining={"reason": "code_changed", "since": since,
                                    "waiting_on": waiting})
        assert drains(now) == []                             # 30 s < drain_grace_s
        out = drains(now + dt.timedelta(seconds=60))         # 90 s old
        assert len(out) == 1
        d = out[0]["data"]
        assert d["reason"] == "code_changed" and d["draining_s"] == 90
        assert d["waiting_on"] == waiting and d["ready"] == 1 and d["slots"] >= 1
        assert drains(now + dt.timedelta(seconds=61)) == []  # exactly once per drain
        lease.pop_info("draining")
        assert drains(now + dt.timedelta(seconds=62)) == []
        lease.update_info(draining={"reason": "code_changed", "since": since,
                                    "waiting_on": waiting})
        assert len(drains(now + dt.timedelta(seconds=63))) == 1   # the condition had cleared
    finally:
        lease.release()
    assert cid and E.EVENT_TYPES["runner_draining"] == ("reason", "draining_s", "waiting_on",
                                                        "ready")
    assert E.settings(cfg)["drain_grace_s"] == 60


def test_ready_not_admitted_fires_during_drain(tmp_path, monkeypatch):
    root = _code_repo(tmp_path)
    repo = make_repo(tmp_path)
    _second_spec(repo, "SPEC-0002", "docs/specs/0002-second.md")
    dep = repo / "docs/specs/0003-dep.md"
    _write(repo, "docs/specs/0003-dep.md", (repo / "docs/specs/0001-trivial.md").read_text()
           .replace("SPEC-0001", "SPEC-0003"))
    spec2 = repo / "docs/specs/0002-second.md"
    spec2.write_text(spec2.read_text().replace("status: accepted",
                                               "status: accepted\ndepends_on: [SPEC-0003]"))
    write_review({"data_dir": str(tmp_path / "data")}, spec2, "SPEC-0002")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "specs")
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(), review()]),
                   profile={"slots": 3})
    cfg["agentic"]["events"] = {"ready_grace_s": 0}
    c = added(cfg, monkeypatch, "SPEC-0001")                 # implements, held
    b = added(cfg, monkeypatch, "SPEC-0002")                 # waits on SPEC-0003
    assert S.read_state(cfg, b)["state"] == "QUEUED"
    runner = GatedRunner(cfg, "SPEC-0001")
    real_tick = Q.tick

    def tick(*args, **kw):
        report = real_tick(*args, **kw)
        if report.started:                                   # "A merged", and code landed
            dep.write_text(dep.read_text().replace("status: accepted", "status: done"))
            _commit(root, "src/wt/x.py", "A = 9\n", "code")
        return report

    seen = {}
    sig = _Signal(monkeypatch)
    sig.watch("drain started", lambda i: bool(i.get("draining")))
    sig.backlog_ready(cfg, b)

    def observer():
        try:
            seen["draining_since"] = sig.wait("drain started")["draining"]["since"]
            seen["since"] = sig.wait("B stored ready")["backlog"]["since"]
            later = dt.datetime.now().astimezone() + dt.timedelta(seconds=2)
            seen["items"] = E.ready_not_admitted_items(cfg, later, E.settings(cfg))
        finally:
            runner.release_all()

    calls = []
    monkeypatch.setattr(Q, "tick", tick)
    monkeypatch.setattr(Q, "_EXEC", lambda exe, argv, env: calls.append(env))
    side = threading.Thread(target=observer, daemon=True)
    side.start()
    Q.run(cfg, max_ticks=50, poll_interval_s=0.05, reexec=True, code_root=root, runner=runner)
    side.join(60)
    assert not side.is_alive()
    assert S.read_state(cfg, c)["state"] != "QUEUED" and len(calls) == 1
    assert seen["since"] >= seen["draining_since"]            # stored during the drain
    (item,) = seen["items"]
    assert item["campaign"] == b and item["runner_draining"] is True
    os.environ[L.HANDOVER_ENV] = calls[0][L.HANDOVER_ENV]
    L.acquire(cfg, ["t"]).release()


def test_heartbeat_thread_survives_errors_and_journals_once(tmp_path):
    cfg = make_cfg(tmp_path, make_repo(tmp_path), script(tmp_path, []))
    lease = L.acquire(cfg, ["t"])
    calls = []
    several = threading.Event()

    def boom(now=None):
        calls.append(1)
        if len(calls) >= 3:
            several.set()
        raise RuntimeError("dict changed size during iteration")

    lease.heartbeat = boom
    try:
        with Q._heartbeat_while(lease, interval_s=0.02) as hb:
            assert several.wait(60), "never saw: several failing beats"
            assert hb._thread.is_alive()
    finally:
        lease.release()
    errs = [r for r in Q.read_journal(cfg) if r["kind"] == "heartbeat_error"]
    assert len(errs) == 1 and "RuntimeError" in errs[0]["error"]    # at most once a minute
