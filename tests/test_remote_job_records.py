"""SPEC-0341: coordinator re-attach, job records and adoption after a restart. Offline: the
worker is SPEC-0338's loopback (`test_remote_gates.Remote`: a fake `ssh` running
`nw worker ssh-command` against a second data dir), registered with a cached `info` that
lists the `async` feature. The third gate blocks on a `GO` file so a test can cut in mid-job."""
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from nightwright.agentic import events as EV
from nightwright.agentic import gate_slots as GS
from nightwright.agentic import gates as G
from nightwright.agentic import queue as Q
from nightwright.agentic import remote_gates as RG
from nightwright.agentic import remote_jobs as RJ
from nightwright.agentic import store as S
from nightwright.agentic import workers as WK

from test_remote_gates import CID, Remote, World, _commit, _decode, _gate, _script
from test_remote_gates import _short_runtime_dir  # noqa: F401  (autouse fixture)
from waits import join, until

ASYNC_INFO = {"protocol": "nw.job.v1", "features": ["async"], "git": "git version 2"}
JOB = RJ.gate_job_id(CID, 1)


class AW(World):
    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.go = tmp_path / "GO"
        self.project["gates"] = [
            _gate("g1", "echo one"), _gate("g2", "echo two"),
            _gate("g3", f"while [ ! -f {self.go} ]; do sleep 0.05; done; echo three")]

    def release(self):
        self.go.write_text("go\n")


@pytest.fixture
def aw(tmp_path):
    w = AW(tmp_path)
    yield w
    w.release()                                 # never leave a blocked gate behind


@pytest.fixture
def remote(aw):
    r = Remote(aw)
    r.register(info=ASYNC_INFO)
    return r


def _ctx(w, stop=None):
    ctx = Q._Ctx(w.cfg, stop=stop)
    ctx._projects[w.state["project"]] = w.project
    return ctx


def _job_dir(r: Remote, job_id=JOB) -> Path:
    return r.wdata / "agentic" / "worker" / "jobs" / job_id


def _worker_frames(r: Remote, job_id=JOB) -> list[dict]:
    p = _job_dir(r, job_id) / "frames.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.is_file() else []


def _job_state(r: Remote, job_id=JOB):
    return RG.poll(r.record(), job_id)


def _rec(w, job_id=JOB):
    return RJ.read(w.cfg, job_id)


def _gate_names_frames(r):
    return [f["result"]["name"] for f in _worker_frames(r) if f["type"] == "gate"]


def _files_match_frames(w, r):
    """Every gate file equals the worker's frame payload, byte for byte."""
    expect = {}
    for f in _worker_frames(r):
        if f["type"] == "gate":
            for name, data in f["files"].items():
                expect[name] = _decode(data)
    got = {p.name: p.read_bytes() for p in w.gates_dir.iterdir()}
    assert got == expect


def _uids(w):
    return [e["event_uid"] for e in w.ledger() if "event_uid" in e]


def _gate_step(w, stop=None):
    ctx = _ctx(w, stop)
    st = S.read_state(w.cfg, CID)
    res = Q._do_gate(ctx, st)
    return ctx, st, res


def _gate_and_apply(w):
    ctx, st, res = _gate_step(w)
    row = Q._apply(ctx, st, res)
    return res, row


# ---- store.append_event's event_uid dedupe ---------------------------------------------------

def test_append_event_dedupes_event_uid_only_when_present(aw):
    a = S.append_event(aw.cfg, CID, "note", {"text": "x", "author": "t", "event_uid": "j:1"})
    b = S.append_event(aw.cfg, CID, "note", {"text": "y", "author": "t", "event_uid": "j:1"})
    assert b["seq"] == a["seq"] and len(aw.ledger(["note"])) == 1
    S.append_event(aw.cfg, CID, "note", {"text": "z", "author": "t"})
    S.append_event(aw.cfg, CID, "note", {"text": "z", "author": "t"})
    assert len(aw.ledger(["note"])) == 3
    for i in range(S.EVENT_UID_SCAN):           # pushed out of the scan window: appended again
        S.append_event(aw.cfg, CID, "note", {"text": str(i), "author": "t"})
    S.append_event(aw.cfg, CID, "note", {"text": "x", "author": "t", "event_uid": "j:1"})
    assert _uids(aw) == ["j:1", "j:1"]


# ---- the async gate step ---------------------------------------------------------------------

def test_async_run_writes_a_record_and_one_gate_event(aw, remote):
    aw.release()
    res, row = _gate_and_apply(aw)
    run = res.gate_run
    assert run.worker == "box" and run.ok and run.seq == 1 and row["to"] == "REVIEWING"
    rec = _rec(aw)
    assert rec["state"] == "done" and rec["seq_or_run"] == 1 and rec["attempt"] == 1
    assert rec["cursor"] == _worker_frames(remote)[-1]["n"]
    assert RJ.epoch(rec["deadline_at"]) > RJ.epoch(rec["submitted_at"])
    (ev,) = aw.ledger(["gate"])
    assert ev["event_uid"] == f"{JOB}:{rec['cursor']}" and ev["worker"] == "box"
    _files_match_frames(aw, remote)


_DROP_ATTACH_ONCE = """import os, subprocess, sys
flag, k = sys.argv[1], int(sys.argv[2])
if os.path.exists(flag):
    os.execv(sys.argv[3], sys.argv[3:])
open(flag, "w").close()
p = subprocess.Popen(sys.argv[3:], stdout=subprocess.PIPE)
for i, line in enumerate(p.stdout, 1):
    sys.stdout.buffer.write(line)
    sys.stdout.buffer.flush()
    if i >= k:
        os._exit(255)
os._exit(255)
"""


def test_drop_reattaches_without_duplicates(aw, remote, monkeypatch):
    """The SSH connection of `attach` is killed after 3 frames: the step backs off,
    `poll --renew`s and re-attaches after its cursor; nothing is applied twice."""
    tmp = aw.tmp
    (tmp / "dropattach.py").write_text(_DROP_ATTACH_ONCE)
    calls = tmp / "calls"
    ssh = _script(tmp / "ssh-dropattach", (
        f'echo "$*" >> {calls}\n'
        f'case "$*" in *" attach "*) exec {sys.executable} {tmp / "dropattach.py"} '
        f'{tmp / "dropped"} 3 {remote.ssh} "$@";; esac\n'
        f'exec {remote.ssh} "$@"'))
    remote.register(ssh=ssh, info=ASYNC_INFO)
    slept = []
    monkeypatch.setattr(RG, "_sleep", lambda s, stop: slept.append(s) or False)
    aw.release()
    res, row = _gate_and_apply(aw)
    assert res.gate_run.ok and res.gate_run.worker == "box" and row["to"] == "REVIEWING"
    assert slept == [RG.BACKOFF_START_S]
    lines = calls.read_text().splitlines()
    attaches = [ln for ln in lines if " attach " in ln]
    assert len(attaches) == 2 and attaches[1].endswith("--from 3")
    assert any(" poll " in ln and ln.endswith("--renew") for ln in lines)
    assert len(aw.ledger(["gate"])) == 1 and len(set(_uids(aw))) == len(_uids(aw))
    assert [r.name for r in res.gate_run.results] == ["g1", "g2", "g3"]
    _files_match_frames(aw, remote)


_STEP = """import json, sys
sys.path.insert(0, {tests!r})
from nightwright.agentic import queue as Q, store as S
cfg, project = json.loads(sys.argv[1]), json.loads(sys.argv[2])
ctx = Q._Ctx(cfg)
ctx._projects[project["project"]] = project
Q._do_gate(ctx, S.read_state(cfg, {cid!r}))
"""


def _step_subprocess(w):
    script = w.tmp / "step.py"
    script.write_text(_STEP.format(tests=str(Path(__file__).parent), cid=CID))
    return subprocess.Popen([sys.executable, str(script), json.dumps(w.cfg),
                             json.dumps(w.project)], start_new_session=True)


def _kill(proc):
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def test_restart_adopts_the_job_under_its_seq(aw, remote):
    proc = _step_subprocess(aw)
    try:
        assert until(lambda: (_rec(aw) or {}).get("state") in ("submitted", "attached")
                     and _gate_names_frames(remote) == ["g1", "g2"]
                     and (aw.gates_dir / "001-g2.log").is_file())
    finally:
        _kill(proc)
    assert _rec(aw)["state"] in RJ.LIVE
    aw.release()
    res, row = _gate_and_apply(aw)
    run = res.gate_run
    assert run.ok and run.worker == "box" and run.job_id == JOB
    (ev,) = aw.ledger(["gate"])
    assert ev["gate_seq"] == _rec(aw)["seq_or_run"] == 1
    assert not list(aw.gates_dir.glob("002-*"))
    assert _rec(aw)["state"] == "done"
    assert [p.name for p in _job_dir(remote).parent.iterdir()] == [JOB]    # nothing re-ran
    jobs = subprocess.run([sys.executable, "-m", "nightwright", "worker", "jobs", "--json"],
                          capture_output=True, text=True, check=True,
                          env={**os.environ, "NW_DATA_DIR": str(remote.wdata),
                               "NW_CONFIG_DIR": str(aw.tmp / "wcfgdir")}).stdout
    assert JOB in jobs and '"done"' in jobs
    # adoption replay: the frames applied before the kill are in the adopted GateRun
    assert [r.name for r in run.results] == ["g1", "g2", "g3"]
    assert len(set(_uids(aw))) == len(_uids(aw))
    _files_match_frames(aw, remote)


def test_restart_before_apply_writes_one_gate_event(aw, remote):
    """The step read `done` and returned, and the runner died before `_apply`: the record
    stays live; the next tick replays the job and writes exactly one `gate` event."""
    aw.release()
    _ctx_, _st, res = _gate_step(aw)            # no `_apply`: the crash between the two
    assert res.gate_run.ok and _rec(aw)["state"] in RJ.LIVE
    assert not aw.ledger(["gate"])
    res2, row = _gate_and_apply(aw)
    assert res2.gate_run.ok and row["to"] == "REVIEWING"
    (ev,) = aw.ledger(["gate"])
    assert ev["event_uid"] == res.gate_run.event_uid == res2.gate_run.event_uid
    assert _rec(aw)["state"] == "done"
    # the same payload applied again (a duplicate replay) is dropped by its event_uid
    S.append_event(aw.cfg, CID, "gate", {**ev, "seq": None})
    assert len(aw.ledger(["gate"])) == 1


def _kill_job_process(r: Remote):
    proc = json.loads((_job_dir(r) / "proc.json").read_text())
    os.kill(proc["pid"], signal.SIGKILL)


def test_lost_job_gives_up_and_runs_locally(aw, remote):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", _gate_step(aw)), daemon=True)
    t.start()
    assert until(lambda: (_job_dir(remote) / "proc.json").is_file()
                 and _gate_names_frames(remote) == ["g1", "g2"])
    _kill_job_process(remote)
    aw.release()
    join(t)
    _ctx_, _st, res = out["r"]
    assert res.gate_run.worker is None and res.gate_run.ok        # the local run
    (lost,) = aw.ledger(["worker_job_lost"])
    assert lost["reason"] == "lost" and lost["job_id"] == JOB and lost["attempt"] == 1
    assert _rec(aw)["state"] == "local"
    EV.sync(aw.cfg)
    stream = [json.loads(ln) for ln in EV.events_path(aw.cfg).read_text().splitlines()]
    (sev,) = [e for e in stream if e.get("type") == "worker_job_lost"]
    assert sev["campaign"] == CID and sev["data"]["reason"] == "lost"


def test_unreachable_backs_off_and_gives_up(aw, remote, monkeypatch):
    calls = aw.tmp / "calls"
    ssh = _script(aw.tmp / "ssh-down", (
        f'echo "$*" >> {calls}\n'
        f'case "$*" in *" attach "*|*" poll "*|*" cancel "*) exit 255;; esac\n'
        f'exec {remote.ssh} "$@"'))
    remote.register(ssh=ssh, info=ASYNC_INFO)
    clock = [RG._now()]
    slept = []

    def fake_sleep(s, stop):
        slept.append(s)
        clock[0] += s
        return False
    monkeypatch.setattr(RG, "_now", lambda: clock[0])
    monkeypatch.setattr(RG, "_sleep", fake_sleep)
    aw.release()
    _ctx_, _st, res = _gate_step(aw)
    assert slept == [2, 4, 8, 16, 32, 60, 60, 60, 60]            # 302 s > worker_ttl_s 300
    lines = calls.read_text().splitlines()
    assert len([ln for ln in lines if " attach " in ln]) == 1
    polls = [ln for ln in lines if " poll " in ln]
    assert len(polls) == len(slept) and all(ln.endswith("--renew") for ln in polls)
    (lost,) = aw.ledger(["worker_job_lost"])
    assert lost["reason"] == "unreachable"
    assert res.gate_run.worker is None and res.gate_run.ok
    assert WK.in_cooldown(remote.record())


def _detached(aw, how):
    """A gate step cut mid-job by a runner exit: `stop` (a signal / `ctx.stop`, with the gates'
    sticky stop SPEC-0229 sets) or `code_changed` (SPEC-0231: the pool detaches every
    detachable task so the runner can exec). Returns the step's result."""
    stop = threading.Event()
    ctx = _ctx(aw, stop)
    st = S.read_state(aw.cfg, CID)
    pool = Q.StepPool()
    fut = pool.submit(Q._do_gate, ctx, st)
    pool.flights[CID] = Q._Flight(st, ctx, tasks=[(Q.Step("gate"), fut, None)])
    try:
        assert until(lambda: _gate_names_frames(remote_of(aw)) == ["g1", "g2"])
        if how == "stop":
            G.terminate_all()
            stop.set()
        else:
            assert until(lambda: RG.attached(CID))
            assert pool.try_detach_all() == [{"campaign": CID, "run_ids": []}]
            assert not pool.flights
        res = fut.result(timeout=60)
    finally:
        pool.end(timeout=5.0)
    return ctx, st, res


def test_code_change_waits_for_a_submit_but_not_an_attach(aw, remote, monkeypatch):
    """Before the job is submitted the gate step is in-process work the exec must wait for."""
    gate = threading.Event()
    real = RG._submit

    def slow_submit(*a, **k):
        gate.wait(30)
        return real(*a, **k)
    monkeypatch.setattr(RG, "_submit", slow_submit)
    ctx = _ctx(aw)
    st = S.read_state(aw.cfg, CID)
    pool = Q.StepPool()
    fut = pool.submit(Q._do_gate, ctx, st)
    pool.flights[CID] = Q._Flight(st, ctx, tasks=[(Q.Step("gate"), fut, None)])
    try:
        assert pool.try_detach_all() is None
        gate.set()
        assert until(lambda: RG.attached(CID))
        assert pool.try_detach_all() is not None
        assert fut.result(timeout=60).gate_run.interrupted
    finally:
        aw.release()
        pool.end(timeout=5.0)
    assert _rec(aw)["state"] == "detached"


def remote_of(aw):
    return aw._remote


@pytest.mark.parametrize("how", ["stop", "code_changed"])
def test_runner_exit_detaches_and_next_runner_adopts(aw, remote, how):
    aw._remote = remote
    ctx, st, res = _detached(aw, how)
    assert res.gate_run.interrupted and res.gate_run.job_id == JOB
    if how == "stop":                           # a code-change exit never applies the step
        row = Q._apply(ctx, st, res)
        assert row["reason"] == "interrupted"
    assert not aw.ledger(["gate"])
    assert _rec(aw)["state"] == "detached"
    assert _job_state(remote) == "running"                        # not cancelled
    G.reset()                                   # the next runner
    aw.release()
    res2, row2 = _gate_and_apply(aw)
    assert res2.gate_run.ok and res2.gate_run.worker == "box" and row2["to"] == "REVIEWING"
    assert [r.name for r in res2.gate_run.results] == ["g1", "g2", "g3"]
    assert len(aw.ledger(["gate"])) == 1 and _rec(aw)["state"] == "done"
    assert not aw.ledger(["worker_job_lost"]) and not aw.ledger(["worker_failed"])


def test_deadline_gives_up(aw, remote):
    aw._remote = remote
    ctx, st, res = _detached(aw, "stop")
    Q._apply(ctx, st, res)
    RJ.update(aw.cfg, JOB, deadline_at=RJ.iso(RG._now() - 1))
    G.reset()
    aw.release()
    res2, _row = _gate_and_apply(aw)
    (lost,) = aw.ledger(["worker_job_lost"])
    assert lost["reason"] == "deadline" and res2.gate_run.worker is None
    assert _rec(aw)["state"] == "local" and not WK.in_cooldown(remote.record())


@pytest.mark.parametrize("ending", ["done", "lost"])
def test_slot_wait_renews_the_lease_and_ends_on_done_or_lost(aw, remote, ending):
    aw._remote = remote
    ctx, st, res = _detached(aw, "stop")
    Q._apply(ctx, st, res)
    G.reset()
    calls = aw.tmp / "calls"
    ssh = _script(aw.tmp / "ssh-log", f'echo "$(date +%s.%N) $*" >> {calls}\n'
                                      f'exec {remote.ssh} "$@"')
    remote.register(ssh=ssh, info=ASYNC_INFO)
    aw.cfg.setdefault("agentic", {})["workers"] = {"job_lease_s": 1.5}    # a poll every 0.5 s
    with GS.acquire(aw.cfg, "someone-else", pools=[("box", 1)]) as held:
        assert held.pool == "box"
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", _gate_and_apply(aw)),
                             daemon=True)
        t.start()
        assert until(lambda: calls.is_file() and len([ln for ln in calls.read_text()
                                                      .splitlines() if " poll " in ln]) >= 3)
        if ending == "done":
            aw.release()
        else:
            _kill_job_process(remote)
            aw.release()                        # the local fallback runs g3 too
        join(t)                                 # ends while the slot is still held
    res2, _row = out["r"]
    polls = [float(ln.split()[0]) for ln in calls.read_text().splitlines() if " poll " in ln]
    gaps = [b - a for a, b in zip(polls, polls[1:])]
    assert all(0.4 <= g for g in gaps[:2]), gaps
    if ending == "done":
        assert res2.gate_run.worker == "box" and res2.gate_run.ok
        assert _rec(aw)["state"] == "done" and not aw.ledger(["worker_job_lost"])
    else:
        (lost,) = aw.ledger(["worker_job_lost"])
        assert lost["reason"] == "lost" and res2.gate_run.worker is None


@pytest.mark.parametrize("how", ["abandon", "park"])
def test_abandon_and_park_cancel_the_job(aw, remote, how):
    aw._remote = remote
    ctx, st, res = _detached(aw, "stop")
    Q._apply(ctx, st, res)
    G.reset()
    assert _job_state(remote) == "running"
    if how == "park":
        Q.apply_park(aw.cfg, Q.plan_park(aw.cfg, CID, "testing"))
    else:                                       # parked without a cancel (a deferred park)
        st = S.read_state(aw.cfg, CID)
        st["state"] = "PARKED"
        st["park"] = {"reason": "human", "detail": "t", "resume_to": "GATING"}
        S.write_state(aw.cfg, CID, st)
        assert RJ.live(aw.cfg, CID)
        Q.apply_abandon(aw.cfg, {"campaign": CID, "by": "human"})
        assert S.read_state(aw.cfg, CID)["state"] == "ABANDONED"
    assert _rec(aw)["state"] == "cancelled"
    assert until(lambda: _job_state(remote) == "done")
    done = [f for f in _worker_frames(remote) if f["type"] == "done"]
    assert done and done[-1].get("interrupted") is True
    pid = json.loads((_job_dir(remote) / "proc.json").read_text())["pid"]
    assert until(lambda: not Path(f"/proc/{pid}").exists()
                 or "Z" in Path(f"/proc/{pid}/stat").read_text().split()[2])


# ---- jobs the loop must not adopt (review round 1) ------------------------------------------

def test_close_regate_job_is_never_adopted_by_the_next_gate_step(aw, remote):
    """A close re-gate (tier `close`) on an async worker: `settle_job` closes its record, and
    even one a crash left live is ignored by `live_gate`. The next GATING runs a fresh job under
    a new seq and gets its own results, not the close run's."""
    aw.release()
    st = S.read_state(aw.cfg, CID)
    run = G.run_all(aw.project, st, aw.cfg, seq=G.next_gate_seq(aw.cfg, CID), tier="close")
    assert run.worker == "box" and run.job_id == JOB and run.event_uid
    rec = _rec(aw)
    assert rec["tier"] == "close" and rec["state"] in RJ.LIVE      # a crash before settling
    assert RJ.live_gate(aw.cfg, CID) is None
    assert G.job_payload(run)["event_uid"] == run.event_uid
    res, _row = _gate_and_apply(aw)
    assert res.gate_run.seq == 2 and res.gate_run.job_id == RJ.gate_job_id(CID, 2)
    assert res.gate_run.worker == "box" and res.gate_run.ok
    (ev,) = aw.ledger(["gate"])
    assert ev["gate_seq"] == 2 and ev["event_uid"].startswith(RJ.gate_job_id(CID, 2))
    assert _rec(aw)["state"] in RJ.LIVE                            # untouched by the loop
    assert not aw.ledger(["worker_failed"]) and not aw.ledger(["worker_job_lost"])
    G.settle_job(aw.cfg, CID, run)                                 # what the close step does
    assert _rec(aw)["state"] == "done"


def test_adoption_after_head_moved_cancels_the_old_job(aw, remote):
    """A detached job whose worktree got a new commit since its submit is cancelled, not
    adopted: the gate runs a new job under a new seq, for the current HEAD."""
    aw._remote = remote
    ctx, st, res = _detached(aw, "stop")
    Q._apply(ctx, st, res)
    G.reset()
    rec = _rec(aw)
    assert rec["state"] in RJ.LIVE and rec["head"] == aw.head and rec["tier"] == "loop"
    head = _commit(aw.repo, "c.txt", "c\n")
    aw.release()
    res2, _row = _gate_and_apply(aw)
    run = res2.gate_run
    assert _rec(aw)["state"] == "cancelled"
    assert run.seq == 2 and run.job_id == RJ.gate_job_id(CID, 2) and run.worker == "box"
    assert _rec(aw, RJ.gate_job_id(CID, 2))["head"] == head
    assert _rec(aw, RJ.gate_job_id(CID, 2))["state"] == "done"
    (ev,) = aw.ledger(["gate"])
    assert ev["gate_seq"] == 2
    assert any("submitted for another commit" in n["text"] for n in aw.ledger(["note"]))
    assert not aw.ledger(["worker_failed"]) and not aw.ledger(["worker_job_lost"])


def test_same_head_still_adopts(aw, remote):
    aw._remote = remote
    ctx, st, res = _detached(aw, "stop")
    Q._apply(ctx, st, res)
    G.reset()
    assert RG.adoptable(_rec(aw), aw.repo)
    (aw.repo / "a.txt").write_text("dirty\n")                      # another tree under test
    assert not RG.adoptable(_rec(aw), aw.repo)
    (aw.repo / "a.txt").write_text("a\n")
    assert RG.adoptable(_rec(aw), aw.repo)
    assert not RG.adoptable({**_rec(aw), "tree": None}, aw.repo)


def test_park_before_the_first_frame_then_resume_uses_a_new_seq(aw, remote):
    """A park lands before any gate frame (no `gates/001-*` file): the next gate step
    allocates past the cancelled record's seq, so it never resubmits a job id the worker
    still holds, and runs remotely with no `worker_failed` and no cooldown."""
    go = aw.tmp / "GO1"
    aw.project["gates"][0] = _gate("g1", f"while [ ! -f {go} ]; do sleep 0.05; done; echo one")
    stop = threading.Event()
    ctx = _ctx(aw, stop)
    st = S.read_state(aw.cfg, CID)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("res", Q._do_gate(ctx, st)), daemon=True)
    t.start()
    try:
        assert until(lambda: RG.attached(CID) and _job_state(remote) == "running")
        G.terminate_all()
        stop.set()
        join(t)
    finally:
        go.write_text("go\n")
    Q._apply(ctx, st, out["res"])
    G.reset()
    assert not list(aw.gates_dir.glob("001-*")) and _rec(aw)["state"] in RJ.LIVE
    Q.apply_park(aw.cfg, Q.plan_park(aw.cfg, CID, "testing"))
    assert _rec(aw)["state"] == "cancelled"
    assert G.next_gate_seq(aw.cfg, CID) == 2
    st = S.read_state(aw.cfg, CID)                                  # resume
    st["state"], st["park"] = "GATING", None
    S.write_state(aw.cfg, CID, st)
    aw.release()
    res, _row = _gate_and_apply(aw)
    run = res.gate_run
    assert run.seq == 2 and run.worker == "box" and run.ok
    assert run.job_id == RJ.gate_job_id(CID, 2) and _rec(aw, run.job_id)["state"] == "done"
    assert not aw.ledger(["worker_failed"]) and not WK.in_cooldown(remote.record())


def test_watcher_poll_refreshes_last_seen(aw, remote):
    """While adoption waits for a slot, a successful poll refreshes `last_seen_at`: an old one
    (a restart long after the last frame) cannot cause an instant `unreachable` give-up."""
    aw._remote = remote
    ctx, st, res = _detached(aw, "stop")
    Q._apply(ctx, st, res)
    G.reset()
    stale = RJ.iso(RG._now() - 10 * RJ.DEFAULT_WORKER_TTL_S)
    RJ.update(aw.cfg, JOB, last_seen_at=stale)
    aw.cfg.setdefault("agentic", {})["workers"] = {"job_lease_s": 0.6}
    with GS.acquire(aw.cfg, "someone-else", pools=[("box", 1)]):
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", _gate_and_apply(aw)),
                             daemon=True)
        t.start()
        assert until(lambda: _rec(aw)["last_seen_at"] != stale)
    aw.release()
    join(t)
    assert out["r"][0].gate_run.worker == "box" and not aw.ledger(["worker_job_lost"])


# ---- old workers -----------------------------------------------------------------------------

@pytest.mark.parametrize("info", [None, {"protocol": "nw.job.v1", "git": "git"}])
def test_old_worker_gates_through_gate_job(aw, info):
    r = Remote(aw)
    if info is None:
        r.register()
    else:
        r.register(info=info)
    aw.release()
    res, _row = _gate_and_apply(aw)
    assert res.gate_run.worker == "box" and res.gate_run.ok and res.gate_run.job_id is None
    assert (_job_dir(r, f"{CID}-gate-001-a0") / "frames.jsonl").is_file()   # gate-job's id
    assert not RJ.jobs_dir(aw.cfg).is_dir() or not list(RJ.jobs_dir(aw.cfg).glob("*.json"))


def test_check_warns_without_async_and_caches_info(aw, monkeypatch):
    r = Remote(aw)
    r.register()
    res = WK.check(aw.cfg, r.record())
    assert res["ok"]
    assert not any("async" in w for w in res["warnings"])      # this checkout's worker has it
    assert RJ.has_async(r.record())                             # cached by the check

    import nightwright.agentic.worker as W
    info = W.info(aw.cfg)
    info.pop("features", None)
    out = {"name": "box", "ok": False, "errors": [], "warnings": [], "info": None}
    monkeypatch.setattr(WK.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, json.dumps(info), ""))
    WK._check(aw.cfg, WK.normalize(aw.cfg, r.record()), out)
    assert any("lacks the `async` feature" in w for w in out["warnings"])
