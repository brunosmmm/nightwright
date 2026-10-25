"""SPEC-0224: the runner announces a drain in its lease info, the out-of-process watchdog turns a
long one into `runner_draining`, and the drain keeps the stored backlog readiness fresh so
`ready_not_admitted` still works."""
import datetime as dt
import json
import os
import threading
import time

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
                assert ev.wait(30), f"test never released {spec}"
        return self.inner.run(req)

    def release_all(self):
        for ev in self.gates.values():
            ev.set()


def _poll(fn, what, timeout=20):
    deadline = time.monotonic() + timeout
    while True:
        got = fn()
        if got:
            return got
        assert time.monotonic() < deadline, f"never saw: {what}"
        time.sleep(0.02)


def _draining(cfg):
    return (L.read_lease(cfg) or {}).get("draining")


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

    seen = {"during": [], "exec": None}

    def fake_exec(exe, argv, env):
        seen["exec"] = {"lease": dict(L.read_lease(cfg)), "env": env}

    def observer():
        try:
            seen["during"].append(_poll(lambda: (_draining(cfg) or {}).get("waiting_on")
                                        and _draining(cfg), "draining with both flights"))
            runner.gates["SPEC-0001"].set()
            seen["during"].append(_poll(
                lambda: (d := _draining(cfg)) and len(d["waiting_on"]) == 1 and d,
                "waiting_on shrinks"))
        finally:
            runner.release_all()

    monkeypatch.setattr(Q, "tick", tick)
    monkeypatch.setattr(Q, "_EXEC", fake_exec)
    side = threading.Thread(target=observer, daemon=True)
    side.start()
    Q.run(cfg, max_ticks=50, poll_interval_s=0.05, reexec=True, code_root=root, runner=runner)
    side.join(30)
    first, second = seen["during"]
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

    def observer():
        try:
            _poll(lambda: _draining(cfg), "drain started")
            st = _poll(lambda: (s := S.read_state(cfg, b))["backlog"].get("ready") and s,
                       "B stored ready during the drain")
            seen["since"] = st["backlog"]["since"]
            seen["draining_since"] = _draining(cfg)["since"]
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
    side.join(30)
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

    def boom(now=None):
        calls.append(1)
        raise RuntimeError("dict changed size during iteration")

    lease.heartbeat = boom
    try:
        with Q._heartbeat_while(lease, interval_s=0.02) as hb:
            _poll(lambda: len(calls) >= 3, "several failing beats")
            assert hb._thread.is_alive()
    finally:
        lease.release()
    errs = [r for r in Q.read_journal(cfg) if r["kind"] == "heartbeat_error"]
    assert len(errs) == 1 and "RuntimeError" in errs[0]["error"]    # at most once a minute
