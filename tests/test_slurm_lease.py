"""SPEC-0513: Slurm jobs survive a sleeping coordinator.

Worker: `_watch` never cancels on a lapsed lease under a `lease_cancels=False` executor (the
local one still does); `nw worker info` reports `lease:none` under Slurm. Coordinator (fake
clocks): a lease-free job's deadline is `max_pending_s` from the submit until `started`, then
`timeout_s + worker_ttl_s` from the start; a job that ended on the worker is replayed however
late; time spent asleep (wall clock ahead of the monotonic one) does not count against the
worker TTL, so a coordinator waking after > 900 s re-attaches and applies the result."""
import json
import threading
import time

import pytest

from nightwright.agentic import gates as G
from nightwright.agentic import remote_gates as RG
from nightwright.agentic import remote_jobs as RJ
from nightwright.agentic import remote_personas as RPS
from nightwright.agentic import worker as W
from nightwright.agentic import worker_job as WJ
from nightwright.agentic import workers as WK

pytestmark = pytest.mark.fast

JOB = "C-1-gate-001-a1"


# ---- worker ------------------------------------------------------------------------------------

def _watch(tmp_path, lease_cancels):
    job = tmp_path / "job"
    job.mkdir()
    WJ._write_atomic(job / "lease.json", {"lease_until": time.time() - 3600})
    cancel, stop = threading.Event(), threading.Event()
    t = threading.Thread(target=WJ._watch, args=(job, {}, cancel, stop, lease_cancels),
                         daemon=True)
    t.start()
    return cancel, stop, t


def test_an_expired_lease_does_not_cancel_a_lease_free_job(tmp_path):
    cancel, stop, t = _watch(tmp_path, False)
    try:
        # several watch ticks: the lease is long expired, nothing cancels
        assert not cancel.wait(8 * WJ.WATCH_TICK_S)
    finally:
        stop.set()
        t.join(10)
    assert not t.is_alive()


def test_the_local_executor_still_cancels_on_an_expired_lease(tmp_path):
    cancel, stop, t = _watch(tmp_path, True)
    try:
        assert cancel.wait(30)
    finally:
        stop.set()
        t.join(10)
        G.reset()                       # `_watch` called `terminate_all` (a sticky stop)


def test_worker_info_reports_lease_none_under_slurm(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "_version_out", lambda *a: None)
    base = {"data_dir": str(tmp_path)}
    slurm = W.info({**base, "agentic": {"workers": {"executor": "slurm"}}})
    local = W.info(base)
    assert "lease:none" in slurm["features"] and "executor:slurm" in slurm["features"]
    assert "lease:none" not in local["features"]


def test_lease_free_reads_the_cached_worker_info(tmp_path):
    cfg = {"data_dir": str(tmp_path)}
    WK.path(cfg).parent.mkdir(parents=True, exist_ok=True)
    WK.path(cfg).write_text(json.dumps({"schema": WK.SCHEMA, "workers": [
        {"name": "box", "ssh": "nw@box",
         "info": {"features": ["async", "executor:slurm", "lease:none"]}},
        {"name": "old", "ssh": "nw@old", "info": {"features": ["async", "executor:slurm"]}},
        {"name": "bare", "ssh": "nw@bare"}]}))
    assert WK.lease_free(cfg, "box") is True
    assert not any(WK.lease_free(cfg, n) for n in ("old", "bare", "nope", None))


# ---- coordinator: deadlines -----------------------------------------------------------------------

@pytest.fixture
def clock(monkeypatch):
    """`remote_gates._now` and the sleep detector's clocks, driven by the test."""
    c = {"wall": 1_800_000_000.0, "mono": 1000.0}
    monkeypatch.setattr(RG, "_now", lambda: c["wall"])
    monkeypatch.setattr(RJ, "_wall", lambda: c["wall"])
    monkeypatch.setattr(RJ, "_mono", lambda: c["mono"])

    def awake(s):
        c["wall"] += s
        c["mono"] += s

    def asleep(s):                      # suspend: the monotonic clock stands still
        c["wall"] += s
    c["awake"], c["asleep"] = awake, asleep
    return c


def _lease_free_job(tmp_path, clock, timeout_s=600, max_pending_s=None):
    cfg = {"data_dir": str(tmp_path / "coord")}
    if max_pending_s:
        cfg["agentic"] = {"slurm": {"max_pending_s": max_pending_s}}
    now = clock["wall"]
    RJ.create(cfg, job_id=JOB, campaign="C-1", worker="box", attempt=1, seq=1,
              submitted_at=now, deadline_at=now + RJ.max_pending_s(cfg))
    rec = RJ.update(cfg, JOB, gates=["g1"], timeout_s=timeout_s, lease="none")
    return cfg, RG._Job(cfg, {"name": "box"}, rec, {"gates": []}, 1)


def _f(n, type_, **kw):
    return {"schema": W.FRAME_SCHEMA, "type": type_, "job_id": JOB, "n": n, **kw}


def test_max_pending_s_defaults_to_six_hours():
    assert RJ.max_pending_s({}) == 6 * 3600
    assert RJ.max_pending_s({"agentic": {"slurm": {"max_pending_s": 60}}}) == 60
    assert RJ.max_pending_s({"agentic": {"slurm": {"max_pending_s": -1}}}) == 6 * 3600


def test_a_pending_job_is_not_given_up_before_max_pending_s(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(RG, "poll", lambda *a, **k: "accepted")
    cfg, job = _lease_free_job(tmp_path, clock, timeout_s=600)
    for n in range(1, 5):               # Slurm keeps it PENDING far past timeout_s + ttl
        clock["awake"](1800)
        job.feed(_f(n, "slot_wait", waited_s=1800.0 * n, reason="Resources"))
        assert not job.past_deadline()
    clock["awake"](6 * 3600)            # past max_pending_s from the submit: given up
    assert job.past_deadline()


def test_started_rearms_the_deadline_from_the_start(tmp_path, clock, monkeypatch):
    polled = []
    monkeypatch.setattr(RG, "poll", lambda *a, **k: polled.append(a) or "running")
    cfg, job = _lease_free_job(tmp_path, clock, timeout_s=600)
    clock["awake"](5 * 3600)            # five hours in Slurm's queue
    job.feed(_f(1, "started", slot_wait_s=5 * 3600.0, at=RJ.iso(clock["wall"])))
    start = clock["wall"]
    ttl = RJ.settings(cfg)["worker_ttl_s"]
    assert job.deadline == pytest.approx(start + 600 + ttl, abs=1)
    assert RJ.epoch(RJ.read(cfg, JOB)["deadline_at"]) == pytest.approx(start + 600 + ttl, abs=1)
    clock["awake"](600 + ttl - 5)
    assert not job.past_deadline() and not polled
    clock["awake"](10)
    assert job.past_deadline() and polled          # still running past it: given up
    # a replayed `started` (adoption from frame 0) does not move it again
    job2 = RG._Job(cfg, {"name": "box"}, RJ.read(cfg, JOB), {"gates": []}, 1)
    job2.feed(_f(1, "started", slot_wait_s=0.0, at=RJ.iso(clock["wall"])))
    assert job2.deadline == pytest.approx(start + 600 + ttl, abs=1)


def test_a_job_that_ended_on_the_worker_is_replayed_however_late(tmp_path, clock, monkeypatch):
    cfg, job = _lease_free_job(tmp_path, clock)
    clock["asleep"](24 * 3600)
    for state in ("done", "error"):
        job.deadline = clock["wall"] - 1
        monkeypatch.setattr(RG, "poll", lambda *a, s=state, **k: s)
        assert not job.past_deadline()


def test_the_local_executor_s_deadline_is_unchanged(tmp_path, clock, monkeypatch):
    """A job on a worker without `lease:none`: `started` does not move the submit deadline."""
    cfg = {"data_dir": str(tmp_path / "coord")}
    now = clock["wall"]
    rec = RJ.create(cfg, job_id=JOB, campaign="C-1", worker="box", attempt=1, seq=1,
                    submitted_at=now, deadline_at=now + 900)
    job = RG._Job(cfg, {"name": "box"}, rec, {"gates": []}, 1)
    clock["awake"](100)
    job.feed(_f(1, "started", slot_wait_s=0.0, at=RJ.iso(clock["wall"])))
    assert job.deadline == pytest.approx(now + 900, abs=1)


def test_run_async_records_a_lease_free_job(tmp_path, clock, monkeypatch):
    cfg = {"data_dir": str(tmp_path / "coord")}
    header = {"commit": "c" * 40, "timeout_s": 600, "payload": {"gates": [{"name": "g1"}]}}
    monkeypatch.setattr(RG, "prepare", lambda *a, **k: dict(header))
    monkeypatch.setattr(RG, "_git", lambda *a, **k: "h" * 40)
    monkeypatch.setattr(RG, "_submit", lambda *a, **k: None)
    monkeypatch.setattr(RG, "_follow", lambda cfg_, w, rec, *a: rec)
    state = {"id": "C-1", "repo": {"worktree": str(tmp_path)}}
    for free, span, lease in ((True, RJ.DEFAULT_MAX_PENDING_S, "none"),
                              (False, 600 + RJ.DEFAULT_WORKER_TTL_S, None)):
        monkeypatch.setattr(WK, "lease_free", lambda *a, f=free: f)
        job_id = f"C-1-gate-00{int(free) + 1}-a1"
        rec = RG._run_async({"name": "box"}, {}, state, cfg, 1, job_id, tmp_path / "b", None)
        assert RJ.epoch(rec["deadline_at"]) == pytest.approx(clock["wall"] + span, abs=1)
        assert rec.get("lease") == lease and rec["timeout_s"] == 600


# ---- coordinator: a coordinator that slept -----------------------------------------------------

def test_sleep_clock_counts_only_suspended_time(clock):
    c = RJ.SleepClock()
    clock["awake"](3600)
    assert c.slept() == 0.0
    clock["asleep"](20)                 # below SLEEP_MIN_S: clock slew, not a sleep
    assert c.slept() == 0.0
    clock["asleep"](1200)
    assert c.slept() == pytest.approx(1200)
    assert c.slept() == 0.0


def test_a_coordinator_waking_after_20_min_reattaches_and_applies_the_result(
        tmp_path, clock, monkeypatch):
    """The laptop sleeps 20 min (> job_lease_s 900, > worker_ttl_s 300) with the job running;
    on wake the attach has dropped. The TTL does not count the sleep, so the follow polls,
    re-attaches and applies the job's `done`: no give-up, no local re-run."""
    cfg, _job = _lease_free_job(tmp_path, clock)
    rec = RJ.read(cfg, JOB)
    attaches = []
    done = G.GateRun(1, True, (), "t", 1.0, worker="box", job_id=JOB)

    def attach(job, stop):
        attaches.append(job.seen)
        if len(attaches) == 1:
            clock["asleep"](20 * 60)    # suspended mid-attach; SSH is dead on wake
            return "dropped", False
        return "done", done
    monkeypatch.setattr(RG, "_attach", attach)
    monkeypatch.setattr(RG, "_sleep", lambda s, stop: clock["awake"](s) or False)
    monkeypatch.setattr(RG, "poll", lambda *a, **k: "done")
    out = RG._follow(cfg, {"name": "box"}, rec, {"gates": []}, 1, None)
    assert out.run is done and out.failure is None and len(attaches) == 2
    assert RJ.read(cfg, JOB)["state"] != "lost"


def test_without_the_sleep_credit_the_same_wake_would_give_up(tmp_path, clock, monkeypatch):
    """The control: the same 20 min spent awake but unreachable is `unreachable`."""
    cfg, _job = _lease_free_job(tmp_path, clock)
    rec = RJ.read(cfg, JOB)

    def attach(job, stop):
        clock["awake"](20 * 60)
        return "dropped", False
    monkeypatch.setattr(RG, "_attach", attach)
    monkeypatch.setattr(RG, "_sleep", lambda s, stop: clock["awake"](s) or False)
    monkeypatch.setattr(RG, "poll", lambda *a, **k: None)
    with pytest.raises(RG._GiveUp) as e:
        RG._follow(cfg, {"name": "box"}, rec, {"gates": []}, 1, None)
    assert e.value.stage == "unreachable"


def test_a_persona_job_past_its_deadline_is_replayed_when_it_ended(monkeypatch):
    rr = type("RR", (), {"worker": {"name": "box"}})()
    job = {"id": "j", "seen": 0, "deadline": time.time() - 1}
    monkeypatch.setattr(RG, "poll", lambda *a, **k: "done")
    assert not RPS._past_deadline(rr, job) and job["deadline"] == float("inf")
    job["deadline"] = time.time() - 1
    monkeypatch.setattr(RG, "poll", lambda *a, **k: "running")
    assert RPS._past_deadline(rr, job)
