"""SPEC-0213: non-blocking ticks. Agent steps run in a pool owned by `run()` and carry across
ticks, so a slow step never delays admission or other campaigns. The rules: one flight per
campaign, in-flight campaigns hold their slot, re-exec drains first, and the heartbeat covers
the loop's whole life."""
import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from test_agentic_e2e import (_git, _write, added, impl_done, make_cfg, make_repo, review,
                              script, write_review)
from test_agentic_reexec import _code_repo, _commit
from waits import join
from wt.agentic import lease as L
from wt.agentic import queue as Q
from wt.agentic import store as S
from wt.agentic.runner import select_runner


class HeldRunner:
    """Delegates to the FakeRunner, but runs whose worktree belongs to a held spec block until
    `release` is set. That makes "A is still running" deterministic."""

    def __init__(self, cfg, *held):
        self.inner = select_runner(cfg)
        self.held = set(held)
        self.release = threading.Event()

    def run(self, req):
        if any(h in str(req.cwd) for h in self.held):
            assert self.release.wait(30), "test never released the held run"
        return self.inner.run(req)


def _second_spec(repo, spec_id="SPEC-0002", rel="docs/specs/0002-second.md"):
    p = _write(repo, rel, (repo / "docs/specs/0001-trivial.md").read_text()
               .replace("SPEC-0001", spec_id))
    write_review({"data_dir": str(repo.parent / "data")}, p, spec_id)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", spec_id)


def _state(cfg, cid):
    return S.read_state(cfg, cid)["state"]


def _starts(cfg, cid):
    return S.read_ledger(cfg, cid, types=["run.start"])


def _settle(pool, cid, timeout=20):
    """Wait until `cid`'s flight is done (not yet harvested)."""
    deadline = time.monotonic() + timeout
    while not pool.flights[cid].done():
        assert time.monotonic() < deadline, f"{cid} never finished"
        time.sleep(0.02)


@pytest.fixture
def pool():
    p = Q.StepPool()
    yield p
    p.shutdown()


def test_slow_run_does_not_block_admission_or_other_campaigns(tmp_path, monkeypatch, pool):
    repo = make_repo(tmp_path)
    _second_spec(repo)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(), review()]),
                   profile={"slots": 2})
    runner = HeldRunner(cfg, "SPEC-0001")
    try:
        a = added(cfg, monkeypatch, "SPEC-0001")
        Q.tick(cfg, runner=runner, pool=pool)                 # admit + open A
        r = Q.tick(cfg, runner=runner, pool=pool)             # A's implementer: in flight
        assert r.started == [{"campaign": a, "action": "implement", "state": "IMPLEMENTING",
                              "run_ids": ["001-implementer"]}]
        assert r.in_flight == [a] and r.idle is False

        b = added(cfg, monkeypatch, "SPEC-0002")              # B becomes ready mid-run
        t0 = time.monotonic()
        r = Q.tick(cfg, runner=runner, pool=pool)
        assert time.monotonic() - t0 < 10                     # did not wait for A
        assert r.admitted == 1 and _state(cfg, b) == "IMPLEMENTING"
        Q.tick(cfg, runner=runner, pool=pool)                 # B's implementer
        _settle(pool, b)
        r = Q.tick(cfg, runner=runner, pool=pool)             # B harvested, gate submitted
        assert {"campaign": b, "from": "IMPLEMENTING", "to": "GATING"}.items() \
            <= next(s for s in r.steps if s["campaign"] == b).items()
        _settle(pool, b)
        Q.tick(cfg, runner=runner, pool=pool)                 # B's gates green → REVIEWING
        assert _state(cfg, b) in ("REVIEWING", "CLOSING")
        assert _state(cfg, a) == "IMPLEMENTING" and a in pool.flights
        assert len(_starts(cfg, a)) == 1                      # one A step through all of it

        runner.release.set()
        _settle(pool, a)
        r = Q.tick(cfg, runner=runner, pool=pool)
        assert any(s["campaign"] == a and s["to"] == "GATING" for s in r.steps)
    finally:
        runner.release.set()


def test_no_double_submission_while_in_flight(tmp_path, monkeypatch, pool):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    runner = HeldRunner(cfg, "SPEC-0001")
    try:
        a = added(cfg, monkeypatch)
        Q.tick(cfg, runner=runner, pool=pool)
        first = Q.tick(cfg, runner=runner, pool=pool)
        assert [s["campaign"] for s in first.started] == [a]
        for _ in range(6):
            r = Q.tick(cfg, runner=runner, pool=pool)
            assert r.started == [] and r.steps == [] and r.in_flight == [a]
        assert len(_starts(cfg, a)) == 1
        assert S.read_state(cfg, a)["counters"]["runs"] == 1
        runner.release.set()
        _settle(pool, a)
        r = Q.tick(cfg, runner=runner, pool=pool)            # harvest + next step, same tick
        assert r.steps[0]["to"] == "GATING" and r.steps[0]["run_id"] == "001-implementer"
        assert [s["action"] for s in r.started] == ["gate"]
        assert len(S.read_ledger(cfg, a, types=["run.end"])) == 1
    finally:
        runner.release.set()


def test_inflight_counts_as_busy_for_slots(tmp_path, monkeypatch, pool):
    """slots = 1. While A's step runs, B (QUEUED, ready) is not admitted and C (WAITING) is not
    continued. A human parks A with no lease (immediate); the step still holds the slot until
    it is harvested. Only after the harvest does the slot go to C, which beats the backlog."""
    repo = make_repo(tmp_path)
    _second_spec(repo)
    _second_spec(repo, "SPEC-0003", "docs/specs/0003-third.md")
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    monkeypatch.setattr(Q, "slots_for", lambda cfg, profile_name=None: 1)
    runner = HeldRunner(cfg, "SPEC-0001")
    try:
        a = added(cfg, monkeypatch, "SPEC-0001", "--priority", "1")
        Q.tick(cfg, runner=runner, pool=pool)
        Q.tick(cfg, runner=runner, pool=pool)                 # A in flight
        b = added(cfg, monkeypatch, "SPEC-0002")
        c = added(cfg, monkeypatch, "SPEC-0003")
        S.write_state(cfg, c, {**S.read_state(cfg, c), "state": "WAITING",
                               "waiting": {"resume_to": "IMPLEMENTING", "since": S.now_iso(),
                                           "reason": "test"}})
        r = Q.tick(cfg, runner=runner, pool=pool)
        assert r.admitted == 0 and _state(cfg, b) == "QUEUED" and _state(cfg, c) == "WAITING"

        plan = Q.plan_park(cfg, a, "stop it")                 # no lease held: parks now
        assert plan["mode"] == "immediate"
        Q.apply_park(cfg, plan)
        assert _state(cfg, a) == "PARKED" and a in pool.flights
        r = Q.tick(cfg, runner=runner, pool=pool)             # the step still holds the slot
        assert r.admitted == 0 and _state(cfg, b) == "QUEUED" and _state(cfg, c) == "WAITING"

        runner.release.set()
        _settle(pool, a)
        r = Q.tick(cfg, runner=runner, pool=pool)
        row = next(s for s in r.steps if s["campaign"] == a)
        assert row["reason"] == "superseded_by_human"         # facts kept, move skipped
        assert len(S.read_ledger(cfg, a, types=["run.end"])) == 1
        assert _state(cfg, c) == "IMPLEMENTING" and _state(cfg, b) == "QUEUED"
    finally:
        runner.release.set()


def test_park_requested_mid_flight_applies_after_record(tmp_path, monkeypatch, pool):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    runner = HeldRunner(cfg, "SPEC-0001")
    try:
        a = added(cfg, monkeypatch)
        Q.tick(cfg, runner=runner, pool=pool)
        Q.tick(cfg, runner=runner, pool=pool)                 # A in flight
        with S.item_lock(cfg, a):                             # what a deferred `park` writes
            st = S.read_state(cfg, a)
            st["park_requested"] = {"reason": "human", "detail": "mid-flight"}
            S.write_state(cfg, a, st)
        for _ in range(3):                                    # not planned: no early park
            r = Q.tick(cfg, runner=runner, pool=pool)
            assert r.steps == [] and _state(cfg, a) == "IMPLEMENTING"
        runner.release.set()
        _settle(pool, a)
        r = Q.tick(cfg, runner=runner, pool=pool)
        st = S.read_state(cfg, a)
        assert st["state"] == "PARKED" and st["park"]["detail"] == "mid-flight"
        assert st["park"]["resume_to"] == "GATING"            # the implementer was recorded
        assert r.steps[0]["run_id"] == "001-implementer" and r.started == []
        assert len(S.read_ledger(cfg, a, types=["run.end"])) == 1
    finally:
        runner.release.set()


def test_reexec_drains_inflight_before_exec(tmp_path, monkeypatch):
    root = _code_repo(tmp_path)
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [dict(impl_done(), sleep_s=1.5), review()]))
    a = added(cfg, monkeypatch)
    real_tick = Q.tick

    def tick(*args, **kw):
        report = real_tick(*args, **kw)
        if report.started:                                   # code lands mid-run
            _commit(root, "src/wt/x.py", "A = 9\n", "code")
        return report

    seen = []

    def fake_exec(exe, argv, env):
        st = S.read_state(cfg, a)
        seen.append({
            "state": st["state"],
            "run_ends": len(S.read_ledger(cfg, a, types=["run.end"])),
            "steps_alive": [t.name for t in threading.enumerate()
                            if t.name.startswith("wt-step")],
            "journal": [(r["kind"], r.get("drain"), r.get("reason")) for r in Q.read_journal(cfg)],
            "handover": env.get(L.HANDOVER_ENV)})

    monkeypatch.setattr(Q, "tick", tick)
    monkeypatch.setattr(Q, "_EXEC", fake_exec)
    ticks = Q.run(cfg, max_ticks=50, poll_interval_s=0.05, reexec=True, code_root=root)
    assert len(seen) == 1, seen
    got = seen[0]
    assert got["run_ends"] == 1 and got["state"] == "GATING"     # recorded and applied
    assert got["steps_alive"] == [] and got["handover"]
    kinds = got["journal"]
    assert kinds[-1] == ("stop", None, "code_changed")
    assert kinds[-2] == ("tick", True, None)                     # the drain, just before
    assert ticks >= 2          # admit/open, then submit; draining ticks follow (SPEC-0229)
    os.environ[L.HANDOVER_ENV] = got["handover"]                 # adopt + release the lease
    L.acquire(cfg, ["t"]).release()


def test_heartbeat_spans_many_ticks(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [dict(impl_done(), sleep_s=3.5), review()]))
    cfg["agentic"]["lease_ttl_s"] = 2
    a = added(cfg, monkeypatch)
    samples, beaters = [], set()
    done = threading.Event()

    def sample():
        while not done.is_set():
            if L.is_held(cfg):
                samples.append(L.lease_status(cfg))
                beaters.update(t.ident for t in threading.enumerate()
                               if t.name == "wt-lease-heartbeat")
            time.sleep(0.2)

    side = threading.Thread(target=sample, daemon=True)
    side.start()
    try:
        ticks = Q.run(cfg, until_idle=True, poll_interval_s=0.05)
    finally:
        done.set()
        join(side)
    assert _state(cfg, a) == "MERGED"
    assert ticks > 10                                         # the run spanned many ticks
    assert len(samples) > 10
    assert not any(s["stale"] for s in samples), [s["heartbeat_age_s"] for s in samples]
    assert max(s["heartbeat_age_s"] for s in samples) < 2
    assert len(beaters) == 1                                  # one thread for the whole loop
    assert not any(t.name == "wt-lease-heartbeat" for t in threading.enumerate())


def test_sigterm_mid_flight_after_several_ticks(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [dict(impl_done(), sleep_s=4)]))
    cid = added(cfg, monkeypatch)
    Q.tick(cfg)
    assert _state(cfg, cid) == "IMPLEMENTING"
    (tmp_path / "cfg" / "config.yaml").write_text(json.dumps({
        "wt_checkout": str(repo), "specs_dir": cfg["specs_dir"], "outbox_dir": cfg["outbox_dir"],
        "org_files": cfg["org_files"], "org_ideas_file": cfg["org_ideas_file"],
        "org_capture_file": cfg["org_capture_file"], "agentic": cfg["agentic"]}))
    env = {**os.environ, "WT_CONFIG_DIR": str(tmp_path / "cfg"), "WT_DATA_DIR": cfg["data_dir"]}
    proc = subprocess.Popen([sys.executable, "-m", "wt.cli", "queue", "run", "--poll-interval",
                             "0.05", "--no-reexec"], env=env, start_new_session=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + 20
        while not _starts(cfg, cid):
            assert time.time() < deadline, proc.communicate(timeout=5)[1]
            time.sleep(0.1)
        time.sleep(1.5)                                       # the loop keeps ticking
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, err
    st = S.read_state(cfg, cid)
    assert st["state"] == "IMPLEMENTING", st
    ends = S.read_ledger(cfg, cid, types=["run.end"])
    assert len(ends) == 1 and ends[0]["interrupted"] is True
    assert st["counters"]["runs"] == 1 and st["cost"]["total_usd"] == 0.5
    assert L.lease_status(cfg)["held"] is False
    recs = Q.read_journal(cfg)
    stop = recs[-1]
    assert stop["kind"] == "stop" and stop["reason"] == "signal" and stop["ticks"] > 3
    drain = recs[-2]
    assert drain["kind"] == "tick" and drain["drain"] is True
    assert drain["steps"][0]["reason"] == "interrupted"
    assert "started implement in IMPLEMENTING" in out
