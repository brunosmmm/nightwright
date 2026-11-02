"""SPEC-0342: lost-job retry across workers, the loss window and the worker escalation.
Offline: two loopback workers (SPEC-0338's fake `ssh` running `nw worker ssh-command`, each
against its own temp data dir), both with a cached `info` listing `async`. The third gate
blocks on a `GO` file so a test can kill a job mid-run."""
import json
import os
import signal
import subprocess
import sys
import threading

import pytest

from nightwright.agentic import escalations as ESC
from nightwright.agentic import events as EV
from nightwright.agentic import gate_slots as GS
from nightwright.agentic import gates as G
from nightwright.agentic import queue as Q
from nightwright.agentic import remote_gates as RG
from nightwright.agentic import remote_jobs as RJ
from nightwright.agentic import store as S
from nightwright.agentic import workers as WK

from test_remote_gates import CID, _commit, _script
from test_remote_gates import _short_runtime_dir  # noqa: F401  (autouse fixture)
from test_remote_job_records import ASYNC_INFO, AW
from waits import join, until

CID2 = "SPEC-0901-20261101-ef01"
A1, A2, A3 = (RJ.gate_job_id(CID, 1, n) for n in (1, 2, 3))


class Box:
    """One loopback worker named `name`, with its own data dir."""

    def __init__(self, w, name):
        self.w, self.name = w, name
        self.wdata = w.tmp / f"wdata-{name}"
        self.ssh = _script(w.tmp / f"fake-ssh-{name}", (
            f'shift\nexport SSH_ORIGINAL_COMMAND="$*" NW_DATA_DIR={self.wdata} '
            f'NW_CONFIG_DIR={w.tmp / f"wcfg-{name}"} NW_WORKER_FINISH_GC=0\n'
            f'exec {sys.executable} -m nightwright worker ssh-command'))

    def rec(self):
        return {"name": self.name, "ssh": f"me@{self.name}", "slots": 1, "nw": "nw",
                "ssh_argv": [self.ssh], "info": ASYNC_INFO}

    def job_dir(self, job_id):
        return self.wdata / "agentic" / "worker" / "jobs" / job_id

    def frames(self, job_id):
        p = self.job_dir(job_id) / "frames.jsonl"
        return [json.loads(ln) for ln in p.read_text().splitlines()] if p.is_file() else []

    def running(self, job_id):
        """The job's process is up and has sent g1 and g2 (g3 blocks on GO)."""
        return (self.job_dir(job_id) / "proc.json").is_file() and \
            [f["result"]["name"] for f in self.frames(job_id) if f["type"] == "gate"] \
            == ["g1", "g2"]

    def kill(self, job_id):
        proc = json.loads((self.job_dir(job_id) / "proc.json").read_text())
        os.kill(proc["pid"], signal.SIGKILL)


def _register(w, *boxes):
    p = WK.path(w.cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema": WK.SCHEMA, "workers": [b.rec() for b in boxes]}))


@pytest.fixture
def aw(tmp_path):
    w = AW(tmp_path)
    w.cfg["agentic"]["escalations"] = {"notify": "none"}
    yield w
    w.release()


@pytest.fixture
def boxes(aw):
    b1, b2 = Box(aw, "b1"), Box(aw, "b2")
    _register(aw, b1, b2)
    return b1, b2


def _ctx(w, stop=None):
    ctx = Q._Ctx(w.cfg, stop=stop)
    ctx._projects[w.state["project"]] = w.project
    return ctx


def _gate_step(w, stop=None):
    return Q._do_gate(_ctx(w, stop), S.read_state(w.cfg, CID))


def _thread(fn):
    out = {}

    def body():
        try:
            out["r"] = fn()
        except BaseException as e:              # noqa: BLE001 - a test's crash, re-checked
            out["e"] = e
    t = threading.Thread(target=body, daemon=True)
    t.start()
    return t, out


def _retried(w, cid=CID):
    return [{k: e[k] for k in ("job_id", "from_worker", "to_worker", "attempt")}
            for e in S.read_ledger(w.cfg, cid, types=["worker_job_retried"])]


def _clear_cooldowns(w):
    def fn(records):
        for r in records:
            r.pop("cooldown_until", None)
    WK.update(w.cfg, fn)


# ---- retry -----------------------------------------------------------------------------------

@pytest.mark.parametrize("limit", [None, 3])
def test_lost_job_retries_on_the_second_worker_then_runs_locally(aw, boxes, monkeypatch, limit):
    """Attempt 1 on b1 is killed: attempt 2 on b2. Attempt 2 is killed too: with the default
    limit (2) the attempts are used up, with 3 both workers are excluded; either way local.
    Each abandoned attempt's `gates/` files are gone before the next attempt starts."""
    b1, b2 = boxes
    if limit is not None:
        aw.cfg["agentic"]["workers"] = {"max_remote_attempts": limit}
    seen = []
    real = RG.retried

    def spy(cfg, cid, lost, attempt, to_worker, **kw):
        seen.append((attempt, to_worker, sorted(p.name for p in aw.gates_dir.glob("001-*"))))
        return real(cfg, cid, lost, attempt, to_worker, **kw)
    monkeypatch.setattr(RG, "retried", spy)
    t, out = _thread(lambda: _gate_step(aw))
    assert until(lambda: b1.running(A1))
    b1.kill(A1)
    assert until(lambda: b2.running(A2))
    assert (rec := RJ.read(aw.cfg, A2))["excluded_workers"] == ["b1"] and rec["attempt"] == 2
    assert rec["seq_or_run"] == 1 and RJ.read(aw.cfg, A1)["state"] == "lost"
    b2.kill(A2)
    aw.release()
    join(t)
    run = out["r"].gate_run
    assert run.worker is None and run.ok and run.seq == 1          # the local run
    lost = S.read_ledger(aw.cfg, CID, types=["worker_job_lost"])
    assert [(e["worker"], e["job_id"], e["attempt"]) for e in lost] == [("b1", A1, 1),
                                                                         ("b2", A2, 2)]
    assert _retried(aw) == [
        {"job_id": A2, "from_worker": "b1", "to_worker": "b2", "attempt": 2},
        {"job_id": A3, "from_worker": "b2", "to_worker": None, "attempt": 3}]
    assert seen == [(2, "b2", []), (3, None, [])]
    assert RJ.read(aw.cfg, A1)["state"] == "lost" and RJ.read(aw.cfg, A2)["state"] == "local"
    assert RJ.read(aw.cfg, A3) is None


def test_busy_second_worker_runs_the_retry_locally_at_once(aw, boxes, monkeypatch):
    b1, b2 = boxes
    pools_seen = []
    real = GS.acquire

    def acquire(cfg, cid, **kw):
        pools_seen.append(kw.get("pools"))
        return real(cfg, cid, **kw)
    monkeypatch.setattr(GS, "acquire", acquire)
    with real(aw.cfg, "OTHER", pools=[("b2", 1)]) as hold:      # b2's only slot is held
        assert hold.pool == "b2"
        t, out = _thread(lambda: _gate_step(aw))
        assert until(lambda: b1.running(A1))
        b1.kill(A1)
        aw.release()
        join(t)
    run = out["r"].gate_run
    assert run.worker is None and run.ok
    assert _retried(aw) == [{"job_id": A2, "from_worker": "b1", "to_worker": None,
                             "attempt": 2}]
    assert RJ.read(aw.cfg, A2) is None and RJ.read(aw.cfg, A1)["state"] == "local"
    assert not (b2.wdata / "agentic" / "worker" / "jobs").exists()
    # the retry's pools: gate_pools without b1 (in cooldown now, excluded anyway), local last
    assert pools_seen[-1] == [p for p in WK.gate_pools(aw.cfg) if p[0] != "b1"] \
        == [("b2", 1), (None, 2)]


def test_retry_pools_drop_the_excluded_workers_and_keep_local_last(aw, boxes):
    st = S.read_state(aw.cfg, CID)
    assert WK.gate_pools(aw.cfg) == [("b1", 1), ("b2", 1), (None, 2)]
    assert RG.retry_pools(aw.cfg, aw.project, st, seq=1, excluded=["b1"]) == [("b2", 1),
                                                                             (None, 2)]
    assert RG.retry_pools(aw.cfg, aw.project, st, seq=1, excluded=["b2"]) == [("b1", 1),
                                                                             (None, 2)]
    assert RG.retry_pools(aw.cfg, aw.project, st, seq=1, excluded=["b1", "b2"]) is None
    assert RG.retry_pools(aw.cfg, aw.project, st, seq=1, excluded=["b1"], worker="b1") is None



def test_retry_pools_with_an_unregistered_or_disabled_pinned_worker_is_local(aw, boxes):
    """`--worker` names a worker `pools_for` refuses now (removed, or disabled since): the
    retry runs locally instead of failing the step."""
    st = S.read_state(aw.cfg, CID)
    assert RG.retry_pools(aw.cfg, aw.project, st, seq=1, excluded=["b1"], worker="gone") is None

    def off(records):
        for r in records:
            if r["name"] == "b2":
                r["enabled"] = False
    WK.update(aw.cfg, off)
    assert RG.retry_pools(aw.cfg, aw.project, st, seq=1, excluded=["b1"], worker="b2") is None


def test_worker_job_retried_is_projected_into_the_global_events(aw, boxes):
    b1, b2 = boxes
    t, out = _thread(lambda: _gate_step(aw))
    assert until(lambda: b1.running(A1))
    b1.kill(A1)
    aw.release()
    join(t)
    assert out["r"].gate_run.worker == "b2" and out["r"].gate_run.ok
    EV.sync(aw.cfg)
    (ev,) = EV.read_since(aw.cfg, types=["worker_job_retried"])
    assert ev["campaign"] == CID
    assert ev["data"] == {"job_id": A2, "from_worker": "b1", "to_worker": "b2", "attempt": 2}


# ---- a stop or a transport failure during the retry ------------------------------------------

def test_stop_while_the_retry_waits_for_a_slot_leaves_the_record_lost(aw, boxes, monkeypatch):
    """Every pool of the retry (b2 and both local slots) is held: the retry waits, a stop
    ends the step with nothing sent. The record stays `lost` with no `worker_job_retried`, and
    the next step continues the chain (one event, attempt 2 on b2)."""
    b1, b2 = boxes
    real = GS.acquire
    waiting = threading.Event()

    def acquire(cfg, cid, **kw):
        if kw.get("pools") == [("b2", 1), (None, 2)]:
            waiting.set()
        return real(cfg, cid, **kw)
    monkeypatch.setattr(GS, "acquire", acquire)
    stop = threading.Event()
    with real(aw.cfg, "O1", pools=[("b2", 1)]), real(aw.cfg, "O2"), real(aw.cfg, "O3"):
        t, out = _thread(lambda: _gate_step(aw, stop))
        assert until(lambda: b1.running(A1))
        b1.kill(A1)
        assert waiting.wait(30)
        stop.set()
        join(t)
    run = out["r"].gate_run
    assert run.interrupted and not run.ok and run.seq == 1
    assert RJ.read(aw.cfg, A1)["state"] == "lost" and RJ.read(aw.cfg, A2) is None
    assert not _retried(aw)
    assert not (b2.wdata / "agentic" / "worker" / "jobs").exists()
    monkeypatch.undo()
    G.reset()
    aw.release()
    res = _gate_step(aw)
    assert res.gate_run.ok and res.gate_run.worker == "b2" and res.gate_run.seq == 1
    assert RJ.read(aw.cfg, A2)["excluded_workers"] == ["b1"]
    assert _retried(aw) == [{"job_id": A2, "from_worker": "b1", "to_worker": "b2",
                             "attempt": 2}]


def test_stop_before_the_retry_is_sent_appends_no_event(aw, boxes, monkeypatch):
    """A stop right after the retry took b2's slot, before submit: no record, no event; the
    next step's continuation appends the only `worker_job_retried`."""
    b1, b2 = boxes
    real = RG.prepare
    stop = threading.Event()

    def prepare(worker, project_cfg, campaign_state, cfg, seq, job_id, *a, **k):
        out = real(worker, project_cfg, campaign_state, cfg, seq, job_id, *a, **k)
        if job_id == A2:
            stop.set()
        return out
    monkeypatch.setattr(RG, "prepare", prepare)
    t, out = _thread(lambda: _gate_step(aw, stop))
    assert until(lambda: b1.running(A1))
    b1.kill(A1)
    join(t)
    assert out["r"].gate_run.interrupted
    assert RJ.read(aw.cfg, A1)["state"] == "lost" and RJ.read(aw.cfg, A2) is None
    assert not _retried(aw)
    monkeypatch.undo()
    G.reset()
    aw.release()
    res = _gate_step(aw)
    assert res.gate_run.ok and res.gate_run.worker == "b2"
    assert _retried(aw) == [{"job_id": A2, "from_worker": "b1", "to_worker": "b2",
                             "attempt": 2}]


@pytest.mark.parametrize("crash", [False, True])
def test_transport_failure_of_a_retry_sets_the_lost_record_local(aw, boxes, monkeypatch,
                                                                   crash):
    """The retry to b2 fails before anything is sent: the lost record goes `local`, the event
    says `to_worker: null`, and the run is local with no further retry. A crash during that
    local run does not restart remote attempts on the next step."""
    b1, b2 = boxes
    real = RG.prepare

    def prepare(worker, project_cfg, campaign_state, cfg, seq, job_id, *a, **k):
        if job_id == A2:
            raise RG.RemoteFailure("bundle", "prepare failed")
        return real(worker, project_cfg, campaign_state, cfg, seq, job_id, *a, **k)
    monkeypatch.setattr(RG, "prepare", prepare)

    class Crash(BaseException):
        pass

    if crash:
        def boom(*a, **k):
            raise Crash()
        monkeypatch.setattr(G, "_fallback", boom)
    t, out = _thread(lambda: _gate_step(aw))
    assert until(lambda: b1.running(A1))
    b1.kill(A1)
    aw.release()
    join(t)
    assert RJ.read(aw.cfg, A1)["state"] == "local" and RJ.read(aw.cfg, A2) is None
    assert _retried(aw) == [{"job_id": A2, "from_worker": "b1", "to_worker": None,
                             "attempt": 2}]
    assert [e["worker"] for e in S.read_ledger(aw.cfg, CID, types=["worker_failed"])] == ["b2"]
    if not crash:
        assert out["r"].gate_run.ok and out["r"].gate_run.worker is None
        return
    assert isinstance(out.get("e"), Crash)
    monkeypatch.undo()
    G.reset()
    res = _gate_step(aw)                        # no `lost` record to continue: a fresh run
    assert res.gate_run.ok and res.gate_run.seq != 1
    assert RJ.read(aw.cfg, A2) is None and len(_retried(aw)) == 1

def _sync(w, name):
    """Worker `name` without the `async` feature: it keeps the synchronous `gate-job`."""
    def fn(records):
        for r in records:
            if r["name"] == name:
                r["info"] = {**ASYNC_INFO, "features": []}
    WK.update(w.cfg, fn)


@pytest.mark.parametrize("stopped", [False, True])
def test_retry_to_a_sync_worker_ends_the_chain(aw, boxes, monkeypatch, stopped):
    """b2 has no `async`: the retry is a `gate-job` with no record of its own. The event names
    the id it was sent with (`-a0`) and the lost record goes `local` before it is sent, so a
    stop during that dispatch starts afresh on the next step: no second `worker_job_retried`."""
    b1, b2 = boxes
    _sync(aw, "b2")
    sync_id = f"{CID}-gate-001-a0"
    stop = threading.Event()
    if stopped:
        def dispatch(rec, header, bundle, cfg, cid, seq, stop_):
            assert RJ.read(cfg, A1)["state"] == "local" and _retried(aw)
            stop.set()
            return RG.Outcome(G.GateRun(seq, False, (), S.now_iso(), 0.0, interrupted=True))
        monkeypatch.setattr(RG, "_dispatch", dispatch)
    t, out = _thread(lambda: _gate_step(aw, stop))
    assert until(lambda: b1.running(A1))
    b1.kill(A1)
    aw.release()
    join(t)
    assert RJ.read(aw.cfg, A1)["state"] == "local" and RJ.read(aw.cfg, A2) is None
    assert _retried(aw) == [{"job_id": sync_id, "from_worker": "b1", "to_worker": "b2",
                             "attempt": 2}]
    if not stopped:
        assert out["r"].gate_run.ok and out["r"].gate_run.worker == "b2"
        return
    assert out["r"].gate_run.interrupted
    monkeypatch.undo()
    G.reset()
    _clear_cooldowns(aw)
    res = _gate_step(aw)                        # the chain ended: a fresh attempt 1
    assert res.gate_run.ok and res.gate_run.seq != 1
    assert RJ.read(aw.cfg, A2) is None and len(_retried(aw)) == 1


def test_a_loss_file_that_cannot_be_written_is_a_note(aw, boxes, monkeypatch):
    """`record_loss` itself fails (the loss file's directory cannot be made): the give-up
    appends a campaign `note` and the retry still runs; the gate step does not fail."""
    b1, b2 = boxes
    blocker = aw.tmp / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(WK, "losses_path", lambda cfg: blocker / "worker-losses.json")
    t, out = _thread(lambda: _gate_step(aw))
    assert until(lambda: b1.running(A1))
    b1.kill(A1)
    aw.release()
    join(t)
    assert "e" not in out, out.get("e")
    assert out["r"].gate_run.ok and out["r"].gate_run.worker == "b2"
    notes = [e["text"] for e in S.read_ledger(aw.cfg, CID, types=["note"])]
    assert any("worker b1: loss not recorded" in n for n in notes), notes
    assert not blocker.with_name("worker-losses.json").exists()


# ---- max_remote_attempts ---------------------------------------------------------------------

def test_max_remote_attempts_setting():
    assert RJ.settings({})["max_remote_attempts"] == 2
    assert RJ.settings({"agentic": {"workers": {"max_remote_attempts": 1}}})[
        "max_remote_attempts"] == 1
    for bad in (0, -1, "2", True, 1.5, None):
        with pytest.raises(ValueError, match="agentic.workers.max_remote_attempts"):
            RJ.settings({"agentic": {"workers": {"max_remote_attempts": bad}}})


def test_max_remote_attempts_one_runs_locally_without_a_retry(aw, boxes):
    b1, b2 = boxes
    aw.cfg["agentic"]["workers"] = {"max_remote_attempts": 1}
    t, out = _thread(lambda: _gate_step(aw))
    assert until(lambda: b1.running(A1))
    b1.kill(A1)
    aw.release()
    join(t)
    assert out["r"].gate_run.worker is None and out["r"].gate_run.ok
    assert _retried(aw) == [{"job_id": A2, "from_worker": "b1", "to_worker": None,
                             "attempt": 2}]
    assert not (b2.wdata / "agentic" / "worker" / "jobs").exists()


@pytest.mark.parametrize("bad", [0, -1, "2", True])
def test_bad_max_remote_attempts_is_a_gate_step_config_error(aw, boxes, bad):
    aw.cfg["agentic"]["workers"] = {"max_remote_attempts": bad}
    aw.release()
    res = _gate_step(aw)
    assert res.gate_run is None and "agentic.workers.max_remote_attempts" in res.error
    assert "config error" in res.error
    assert not RJ.jobs_dir(aw.cfg).exists() or not list(RJ.jobs_dir(aw.cfg).glob("*.json"))


def test_bad_max_remote_attempts_without_workers_changes_nothing(aw):
    aw.cfg["agentic"]["workers"] = {"max_remote_attempts": 0}
    aw.release()
    res = _gate_step(aw)
    assert res.error is None and res.gate_run.ok and res.gate_run.worker is None


# ---- several workers -------------------------------------------------------------------------

def test_two_campaigns_use_both_workers(aw, boxes):
    b1, b2 = boxes
    st2 = {**aw.state, "id": CID2, "spec_id": "SPEC-0901"}
    S.create_campaign(aw.cfg, st2)
    runs = {}
    threads = []
    for cid in (CID, CID2):
        st = S.read_state(aw.cfg, cid)
        t, out = _thread(lambda st=st: G.run_all(aw.project, st, aw.cfg, seq=1, tier="loop"))
        threads.append(t)
        runs[cid] = out
    jobs = lambda b: list((b.wdata / "agentic" / "worker" / "jobs").glob("*-gate-*"))  # noqa
    assert until(lambda: len(jobs(b1)) == 1 and len(jobs(b2)) == 1)
    aw.release()
    for t in threads:
        join(t)
    assert {runs[c]["r"].worker for c in (CID, CID2)} == {"b1", "b2"}
    assert all(runs[c]["r"].ok for c in (CID, CID2))


# ---- adoption and the crash between records -------------------------------------------------

def test_adoption_of_an_attempt_2_record_keeps_its_exclusions(aw, boxes):
    b1, b2 = boxes
    aw.cfg["agentic"]["workers"] = {"max_remote_attempts": 3}
    t, out = _thread(lambda: _gate_step(aw))
    assert until(lambda: b1.running(A1))
    b1.kill(A1)
    assert until(lambda: b2.running(A2) and RG.attached(CID))
    assert RG.detach_attached(CID)              # a code-change exit: the record stays live
    join(t)
    assert out["r"].gate_run.interrupted and RJ.read(aw.cfg, A2)["state"] == "detached"
    G.reset()
    _clear_cooldowns(aw)                        # b1 would be eligible but for the exclusion
    t, out = _thread(lambda: _gate_step(aw))    # the next runner adopts attempt 2
    assert until(lambda: RJ.read(aw.cfg, A2)["state"] == "attached" and RG.attached(CID))
    assert RJ.read(aw.cfg, A2)["excluded_workers"] == ["b1"]
    b2.kill(A2)
    aw.release()
    join(t)
    assert out["r"].gate_run.worker is None and out["r"].gate_run.ok
    assert _retried(aw)[-1] == {"job_id": A3, "from_worker": "b2", "to_worker": None,
                                "attempt": 3}
    assert RJ.read(aw.cfg, A3) is None


def _lost_record(w, worker="b1"):
    head, tree = RG.tree_under_test(w.repo)
    rec = RJ.create(w.cfg, job_id=A1, campaign=CID, worker=worker, attempt=1, seq=1,
                    submitted_at=RG._now(), deadline_at=RG._now() + 600, tier="loop",
                    commit=head, head=head, tree=tree)
    RJ.update(w.cfg, A1, state="lost", lost_reason="lost")
    return rec


def test_crash_between_records_continues_the_chain(aw, boxes, monkeypatch):
    """A crash after attempt 1's record went `lost`, before attempt 2's was written: here the
    step dies in `_retry`, then the next tick finds no live record and starts attempt 2."""
    b1, b2 = boxes

    class Crash(BaseException):
        pass

    def crash(*a, **k):
        raise Crash()
    monkeypatch.setattr(G, "_retry", crash)
    t, out = _thread(lambda: _gate_step(aw))
    assert until(lambda: b1.running(A1))
    b1.kill(A1)
    join(t)
    assert isinstance(out.get("e"), Crash)
    assert RJ.read(aw.cfg, A1)["state"] == "lost" and RJ.live_gate(aw.cfg, CID) is None
    monkeypatch.undo()
    _clear_cooldowns(aw)
    aw.release()
    res = _gate_step(aw)
    assert res.gate_run.ok and res.gate_run.worker == "b2" and res.gate_run.seq == 1
    rec = RJ.read(aw.cfg, A2)
    assert rec["excluded_workers"] == ["b1"] and rec["seq_or_run"] == 1
    assert _retried(aw) == [{"job_id": A2, "from_worker": "b1", "to_worker": "b2",
                             "attempt": 2}]
    assert not (b1.job_dir(A2)).exists()


def test_crash_between_records_with_another_commit_starts_over(aw, boxes):
    _lost_record(aw)
    _commit(aw.repo, "c.txt", "c\n")            # the commit under test moved on
    aw.release()
    res = _gate_step(aw)
    assert res.gate_run.ok and res.gate_run.seq == 2 and res.gate_run.worker == "b1"
    assert RJ.read(aw.cfg, RJ.gate_job_id(CID, 2, 1)) is not None
    assert RJ.read(aw.cfg, A2) is None and not _retried(aw)


def test_crash_between_records_from_the_lost_record_alone(aw, boxes):
    _lost_record(aw, worker="b2")
    aw.release()
    res = _gate_step(aw)
    assert res.gate_run.ok and res.gate_run.seq == 1 and res.gate_run.worker == "b1"
    assert RJ.read(aw.cfg, A2)["excluded_workers"] == ["b2"]



def test_adopting_a_record_of_a_removed_worker_records_no_loss(two):
    """`nw workers rm W` while records on W are live: each adoption gives up (`unreachable`)
    but counts no loss against W, so no escalation is raised about a worker that is gone."""
    aw = two
    for i, cid in enumerate((CID, CID2, CID)):
        seq = i + 1
        rec = RJ.create(aw.cfg, job_id=RJ.gate_job_id(cid, seq, 1), campaign=cid,
                        worker="gone", attempt=1, seq=seq, submitted_at=RG._now(),
                        deadline_at=RG._now() + 600, tier="loop")
        out, _ = RG.adopt(rec, aw.project, S.read_state(aw.cfg, cid), aw.cfg, seq, None)
        assert out.lost["state"] == "lost" and out.lost["lost_reason"] == "unreachable"
    assert "gone" not in (WK.read_losses(aw.cfg).get("workers") or {})
    assert not _worker_escalations(aw)
    assert len(S.read_ledger(aw.cfg, CID, types=["worker_job_lost"])) == 2

# ---- the loss window -------------------------------------------------------------------------

T0 = 1_800_000_000.0


def _lose(w, cid, n, *, at, worker="b1", seq=None):
    """`n` give-ups of `worker` jobs of campaign `cid` (no worker reachable: no cancel)."""
    out = []
    for i in range(n):
        s = seq if seq is not None else RJ.max_seq(w.cfg, cid) + 1
        job_id = RJ.gate_job_id(cid, s, 1)
        rec = RJ.create(w.cfg, job_id=job_id, campaign=cid, worker=worker, attempt=1, seq=s,
                        submitted_at=at, deadline_at=at + 600, tier="loop")
        WK._now = lambda at=at: at
        try:
            out.append(RG._give_up(w.cfg, cid, None, rec, s, "lost"))
        finally:
            WK._now = __import__("time").time
    return out


def _worker_escalations(w):
    return [r for r in ESC.list_all(w.cfg) if r.get("kind") == "worker_lost"]


@pytest.fixture
def two(aw, boxes):
    st2 = {**aw.state, "id": CID2, "spec_id": "SPEC-0901"}
    S.create_campaign(aw.cfg, st2)
    return aw


def test_three_losses_from_two_campaigns_raise_one_escalation(two):
    aw = two
    _lose(aw, CID, 1, at=T0)
    _lose(aw, CID2, 1, at=T0 + 60)
    assert not _worker_escalations(aw)
    _lose(aw, CID, 1, at=T0 + 120)
    (esc,) = _worker_escalations(aw)
    assert esc["campaign"] is None and esc["by"] == "conductor" and esc["status"] == "open"
    assert [o["label"] for o in esc["options"]] == ["off", "keep"] and esc["recommendation"] == 1
    assert esc["facts"][0] == "worker=b1" and len(esc["facts"]) == 4
    assert esc["question"] == "Worker b1 lost 3 jobs in the last hour. Disable it?"
    for cid in (CID, CID2):                    # no campaign parked
        assert S.read_state(aw.cfg, cid)["state"] == "GATING"
    doc = WK.read_losses(aw.cfg)["workers"]["b1"]
    assert doc["escalation"] == esc["id"] and doc["escalated_at"] and len(doc["losses"]) == 3
    _lose(aw, CID2, 1, at=T0 + 180)            # a fourth while it is open: none
    assert len(_worker_escalations(aw)) == 1
    EV.sync(aw.cfg)
    assert [e["data"]["escalation_id"] for e in EV.read_since(aw.cfg, types=["escalation"])] \
        == [esc["id"]]


def test_old_losses_are_pruned(two):
    aw = two
    _lose(aw, CID, 1, at=T0)
    _lose(aw, CID, 2, at=T0 + 3700)
    assert not _worker_escalations(aw)
    assert len(WK.read_losses(aw.cfg)["workers"]["b1"]["losses"]) == 2


def test_after_an_answer_and_an_hour_three_new_losses_raise_a_second(two):
    aw = two
    _lose(aw, CID, 3, at=T0)
    (esc,) = _worker_escalations(aw)
    ESC.apply_answer(aw.cfg, ESC.plan_answer(aw.cfg, esc["id"], choose="keep"))
    _lose(aw, CID, 3, at=T0 + 1800)            # answered, but within the hour of escalated_at
    assert len(_worker_escalations(aw)) == 1
    _lose(aw, CID2, 3, at=T0 + 3700)           # past the hour: a second one
    assert len(_worker_escalations(aw)) == 2


def test_a_fourth_loss_while_the_escalation_is_open_raises_none_on_its_own(two):
    """The open-escalation check alone: `escalated_at` is cleared, so only the open record
    for the worker stops a second escalation."""
    aw = two
    _lose(aw, CID, 3, at=T0)
    (esc,) = _worker_escalations(aw)
    p = WK.losses_path(aw.cfg)
    doc = json.loads(p.read_text())
    doc["workers"]["b1"]["escalated_at"] = None
    p.write_text(json.dumps(doc))
    _lose(aw, CID2, 1, at=T0 + 180)            # a fourth while it is open: none
    assert [r["id"] for r in _worker_escalations(aw)] == [esc["id"]]
    w = WK.read_losses(aw.cfg)["workers"]["b1"]
    assert len(w["losses"]) == 4 and w["escalated_at"] is None
    assert WK.record_loss(aw.cfg, "b1", job_id="x", reason="lost",
                          now=T0 + 200)["escalation"] is None
    assert len(_worker_escalations(aw)) == 1


def test_an_open_escalation_blocks_a_new_one_after_the_window_has_elapsed(two):
    """An hour after `escalated_at`, with three losses in the window, the still-open record
    stops a second escalation; once it is answered, the next loss raises one."""
    aw = two
    _lose(aw, CID, 3, at=T0)
    (esc,) = _worker_escalations(aw)
    _lose(aw, CID2, 3, at=T0 + 3700)           # window elapsed, three losses, still open
    assert [r["id"] for r in _worker_escalations(aw)] == [esc["id"]]
    w = WK.read_losses(aw.cfg)["workers"]["b1"]
    assert len(w["losses"]) == 3 and w["escalation"] == esc["id"]
    ESC.apply_answer(aw.cfg, ESC.plan_answer(aw.cfg, esc["id"], choose="keep"))
    _lose(aw, CID, 1, at=T0 + 3710)            # same window, now answered: a second one
    escs = _worker_escalations(aw)
    assert len(escs) == 2
    assert WK.read_losses(aw.cfg)["workers"]["b1"]["escalation"] != esc["id"]


def test_a_failed_raise_is_a_note_and_the_next_loss_retries(two, monkeypatch):
    aw = two
    real = ESC.apply_escalate

    def boom(cfg, plan):
        raise OSError("mailbox down")
    monkeypatch.setattr(ESC, "apply_escalate", boom)
    _lose(aw, CID, 3, at=T0)
    assert not _worker_escalations(aw)
    assert WK.read_losses(aw.cfg)["workers"]["b1"]["escalated_at"] is None
    assert any("mailbox down" in e["text"] for e in S.read_ledger(aw.cfg, CID, types=["note"]))
    monkeypatch.setattr(ESC, "apply_escalate", real)
    _lose(aw, CID, 1, at=T0 + 10)
    assert len(_worker_escalations(aw)) == 1


def test_a_failed_mail_is_a_failed_raise(two, monkeypatch):
    """The mail to `human` fails (the mailbox is unwritable): the record is withdrawn,
    `escalated_at` stays null, the losing campaign gets a `note`, and the next loss raises."""
    aw = two

    def boom(*a, **k):
        raise OSError("mailbox unwritable")
    monkeypatch.setattr(ESC.M, "send", boom)
    _lose(aw, CID, 3, at=T0)
    assert [r["status"] for r in _worker_escalations(aw)] == ["withdrawn"]
    w = WK.read_losses(aw.cfg)["workers"]["b1"]
    assert w["escalated_at"] is None and w["escalation"] is None
    notes = [e["text"] for e in S.read_ledger(aw.cfg, CID, types=["note"])]
    assert any("not raised" in n and "mailbox unwritable" in n for n in notes), notes
    monkeypatch.undo()
    _lose(aw, CID, 1, at=T0 + 10)
    opened = [r for r in _worker_escalations(aw) if r["status"] == "open"]
    assert len(opened) == 1 and opened[0]["mail_id"]
    assert WK.read_losses(aw.cfg)["workers"]["b1"]["escalation"] == opened[0]["id"]


def test_the_loss_count_survives_a_restart(two):
    aw = two
    _lose(aw, CID, 2, at=T0)
    code = ("import json, sys; from nightwright.agentic import workers as WK; "
            "print(json.dumps(WK.record_loss(json.loads(sys.argv[1]), 'b1', job_id='x', "
            "reason='lost', now=float(sys.argv[2]))))")
    p = subprocess.run([sys.executable, "-c", code, json.dumps(aw.cfg), str(T0 + 30)],
                       capture_output=True, text=True, check=True)
    got = json.loads(p.stdout)
    assert got["losses"] == 3 and got["escalation"]
    assert [r["id"] for r in _worker_escalations(aw)] == [got["escalation"]]


# ---- carrying out the answer -----------------------------------------------------------------

def _answered(aw, choose):
    _lose(aw, CID, 3, at=T0)
    (esc,) = _worker_escalations(aw)
    ESC.apply_answer(aw.cfg, ESC.plan_answer(aw.cfg, esc["id"], choose=choose))
    return esc["id"]


def _tick(aw):
    Q.tick(aw.cfg, admit=False)


def test_off_disables_the_worker_once(two):
    aw = two
    esc_id = _answered(aw, "off")
    _tick(aw)
    (b1,) = [w for w in WK.load(aw.cfg) if w["name"] == "b1"]
    assert b1["enabled"] is False
    assert all(w["enabled"] for w in WK.load(aw.cfg) if w["name"] != "b1")
    rec = ESC.read(aw.cfg, esc_id)
    assert rec["applied"] == "off" and rec["applied_at"]
    before = WK.path(aw.cfg).read_bytes()
    mtime = WK.path(aw.cfg).stat().st_mtime_ns
    _tick(aw)                                   # applied once: the registry is not rewritten
    assert WK.path(aw.cfg).read_bytes() == before and WK.path(aw.cfg).stat().st_mtime_ns == mtime


def test_keep_leaves_the_registry_byte_identical(two):
    aw = two
    esc_id = _answered(aw, "keep")
    before = WK.path(aw.cfg).read_bytes()
    mtime = WK.path(aw.cfg).stat().st_mtime_ns
    _tick(aw)
    assert WK.path(aw.cfg).read_bytes() == before and WK.path(aw.cfg).stat().st_mtime_ns == mtime
    assert ESC.read(aw.cfg, esc_id)["applied"] == "keep"


def test_an_unreadable_registry_leaves_the_answer_unapplied(two):
    """The registry cannot be parsed at the tick: the record keeps no `applied_at` and the
    file is not touched; once it reads again, the next tick applies the answer."""
    aw = two
    esc_id = _answered(aw, "off")
    good = WK.path(aw.cfg).read_bytes()
    WK.path(aw.cfg).write_text("{not json")
    _tick(aw)
    rec = ESC.read(aw.cfg, esc_id)
    assert "applied_at" not in rec or rec["applied_at"] is None
    assert WK.path(aw.cfg).read_text() == "{not json"
    WK.path(aw.cfg).write_bytes(good)
    _tick(aw)
    assert ESC.read(aw.cfg, esc_id)["applied"] == "off"
    assert [w["enabled"] for w in WK.load(aw.cfg) if w["name"] == "b1"] == [False]


def test_removed_worker_is_skipped_with_a_note(two, monkeypatch):
    from click.testing import CliRunner
    from nightwright.cli import cli
    aw = two
    esc_id = _answered(aw, "off")
    monkeypatch.setattr("nightwright.cli.load_config", lambda: aw.cfg)
    r = CliRunner().invoke(cli, ["workers", "rm", "b1"])
    assert r.exit_code == 0, r.output
    before = WK.path(aw.cfg).read_bytes()
    _tick(aw)
    assert WK.path(aw.cfg).read_bytes() == before
    rec = ESC.read(aw.cfg, esc_id)
    assert rec["applied"] == "skipped" and rec["applied_at"]
    notes = EV.read_since(aw.cfg, types=["note"])
    assert any(esc_id in n["data"]["text"] and "no longer registered" in n["data"]["text"]
               for n in notes)



def test_free_text_answer_is_skipped_with_a_note(two):
    aw = two
    _lose(aw, CID, 3, at=T0)
    (esc,) = _worker_escalations(aw)
    ESC.apply_answer(aw.cfg, ESC.plan_answer(aw.cfg, esc["id"], text="look at it tomorrow"))
    before = WK.path(aw.cfg).read_bytes()
    mtime = WK.path(aw.cfg).stat().st_mtime_ns
    _tick(aw)
    assert WK.path(aw.cfg).read_bytes() == before and WK.path(aw.cfg).stat().st_mtime_ns == mtime
    rec = ESC.read(aw.cfg, esc["id"])
    assert rec["applied"] == "skipped" and rec["applied_at"]
    notes = [n for n in EV.read_since(aw.cfg, types=["note"]) if esc["id"] in n["data"]["text"]]
    assert len(notes) == 1 and "not one of its options" in notes[0]["data"]["text"]
    _tick(aw)                                   # applied once: no second note
    assert len([n for n in EV.read_since(aw.cfg, types=["note"])
                if esc["id"] in n["data"]["text"]]) == 1
