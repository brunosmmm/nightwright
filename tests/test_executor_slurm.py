"""SPEC-0510: the Slurm executor, driven against the fake Slurm CLI (`tests/slurm_fake.py`).

Covers the sbatch argv (name, comment JSON, cpus, mem, time clamp, licenses dropped when
undefined, SLURM_CONF), the state table row by row (PENDING -> `slot_wait` with Slurm's reason,
running, an end state without `result.json` -> one final frame, unknown -> lost, a CLI failure
never lost), cancel (TERM to the batch step; `scancel` of a pending job; the backstop), sbatch
failure -> `stage: "spawn"`, the real `nw worker run-job` inside a (fake) Slurm job (frames,
NW_GATE_CPUS from the allocation, SIGTERM -> interrupted, a requeue is attempt 2), and the sync
`gate-job` submit-and-follow incl. stdin EOF -> scancel. `local` stays the default."""
import io
import json
import os
import subprocess
import threading
import time

import pytest

from nightwright.agentic import executor_slurm as ES
from nightwright.agentic import executors as EX
from nightwright.agentic import worker as W
from nightwright.agentic import worker_job as WJ
from nightwright.agentic import workers as WK
from slurm_fake import SlurmFake
from test_worker import (_bundle_job, _gate, _header, _job_dir, _no_finish_gc,  # noqa: F401
                         src, wenv)
from waits import WAIT_S, join, until

pytestmark = pytest.mark.integration


@pytest.fixture
def fake(tmp_path, monkeypatch):
    f = SlurmFake(tmp_path / "slurm").install(monkeypatch)
    ES.forget()
    ES._LICENSES.clear()
    ES._LIMITS.clear()
    monkeypatch.setenv(W.SLOT_WAIT_ENV, "3600")
    yield f
    f.close()
    ES.forget()
    ES._LICENSES.clear()
    ES._LIMITS.clear()


def _cfg(env, slurm=None, **workers):
    return {"data_dir": env["NW_DATA_DIR"], "config_dir": env["NW_CONFIG_DIR"],
            "agentic": {"workers": {"min_free_gb": 0, "abort_free_gb": 0, "executor": "slurm",
                                    **workers},
                        "slurm": {"cache_s": 0, **(slurm or {})}}}


def _line(h, data=b""):
    return io.BytesIO((json.dumps(h) + "\n").encode() + data)


def _read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _frames(job):
    p = job / "frames.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln] if p.exists() else []


def _resources(cpus=3, mem_mb=2048, licenses=None, time_s=600):
    return {"cpus": cpus, "mem_mb": mem_mb, "licenses": licenses or {}, "time_s": time_s,
            "gates": {}}


def _submit(cfg, h, data=b""):
    out = io.BytesIO()
    rc = WJ.submit(cfg, _line(h, data), out)
    return rc, [json.loads(ln) for ln in out.getvalue().splitlines()]


def _plain(job_id, **res):
    h = _header(job_id, "a" * 40, "b" * 40, [_gate("x", "true")], bundle_bytes=0)
    h["payload"]["resources"] = _resources(**res)
    return h


# ---- opt-in ------------------------------------------------------------------------------------

def test_local_stays_the_default_and_slurm_is_opt_in(wenv):
    plain = {"data_dir": wenv["NW_DATA_DIR"], "config_dir": wenv["NW_CONFIG_DIR"]}
    assert EX.configured(plain) == "local" and EX.get(plain).name == "local"
    assert "slurm" in EX.known()
    assert EX.get(_cfg(wenv)).name == "slurm"
    info = W.info(_cfg(wenv))
    assert info["executor"] == "slurm" and "executor:slurm" in info["features"]


# ---- submit ------------------------------------------------------------------------------------

def test_sbatch_argv_names_sizes_and_spools_the_job(wenv, fake):
    cfg = _cfg(wenv, slurm={"conf": "/etc/slurm-x/slurm.conf", "partition": "nw-gatejob"})
    h = _plain("j-argv", licenses={"jerome_target": 1, "undefined_lic": 2})
    rc, out = _submit(cfg, h)
    assert rc == 0 and out[0]["type"] == "status" and out[0]["state"] == "accepted"
    (argv,) = fake.calls("sbatch")
    job = _job_dir(wenv, "j-argv")
    jid = fake.ids()[0]
    opts = {a.split("=", 1)[0]: a.split("=", 1)[1] if "=" in a else True for a in argv}
    assert opts["--parsable"] is True and opts["--job-name"] == "j-argv"
    assert json.loads(opts["--comment"]) == {"campaign": "C-1", "seq": 1, "attempt": 0,
                                            "kind": "gate", "requester": None}
    assert opts["--partition"] == "nw-gatejob"
    assert opts["--cpus-per-task"] == "3" and opts["--mem"] == "2048M"
    assert opts["--time"] == "15"                         # ceil((600 + 300) / 60)
    assert opts["--licenses"] == "jerome_target:1"        # the undefined one is dropped
    assert opts["--chdir"] == str(job) and opts["--output"] == str(job / "slurm.out")
    assert {"--requeue", "--export"} <= set(opts) and opts["--signal"] == "B:TERM@60"
    assert opts["--export"] == "ALL" and opts["--open-mode"] == "append"
    assert "worker run-job" in opts["--wrap"] and str(job) in opts["--wrap"]
    assert "SLURM_RESTART_COUNT" in opts["--wrap"] and WJ.ATTEMPT_ENV in opts["--wrap"]
    env = fake.job(jid)["env"]
    assert env["SLURM_CONF"] == "/etc/slurm-x/slurm.conf"
    assert env["NW_DATA_DIR"] == wenv["NW_DATA_DIR"] and env[W.FINISH_GC_ENV] == "0"
    rec = _read(job / "job.json")
    assert rec["executor"] == "slurm" and rec["slurm_job_id"] == jid
    assert any("undefined_lic" in w for w in rec["slurm_warnings"])
    assert _read(job / "spawn.json")["slurm_job_id"] == jid


def test_the_time_is_clamped_to_the_partition_maximum(wenv, fake):
    cfg = _cfg(wenv, slurm={"max_time_s": 600})
    assert _submit(cfg, _plain("j-clamp", time_s=3600))[0] == 0
    (argv,) = fake.calls("sbatch")
    assert "--time=10" in argv and not any(a.startswith("--partition") for a in argv)
    assert not any(a.startswith("--licenses") for a in argv)
    assert any("clamped" in w for w in _read(_job_dir(wenv, "j-clamp") / "job.json")
               ["slurm_warnings"])


@pytest.mark.parametrize("err,retryable", [
    ("sbatch: error: Batch job submission failed: Unable to contact slurm controller", True),
    ("sbatch: error: Batch job submission failed: Requested node configuration is not "
     "available", False)])
def test_sbatch_failure_unclaims_with_stage_spawn(wenv, fake, err, retryable):
    fake.fail("sbatch", err)
    rc, out = _submit(_cfg(wenv), _plain("j-fail"))
    assert rc == W.EXIT_ERROR
    (frame,) = out
    assert frame["type"] == "error" and frame["stage"] == "spawn" and "Batch job" in frame["error"]
    assert frame.get("retryable", False) is retryable
    assert not _job_dir(wenv, "j-fail").exists()


# ---- state -------------------------------------------------------------------------------------

def _state(cfg, job):
    return WJ.job_state(cfg, job)["state"]


def test_pending_is_accepted_with_slot_wait_frames_carrying_the_reason(wenv, fake, monkeypatch):
    cfg = _cfg(wenv)
    assert _submit(cfg, _plain("j-pend"))[0] == 0
    job, jid = _job_dir(wenv, "j-pend"), fake.ids()[0]
    fake.pending(jid, "Resources")
    assert _state(cfg, job) == "accepted"
    assert _state(cfg, job) == "accepted"               # within the interval: no second frame
    fake.pending(jid, "Priority")                       # a new reason: a new frame
    assert _state(cfg, job) == "accepted"
    waits = [f for f in _frames(job) if f["type"] == "slot_wait"]
    assert [f["reason"] for f in waits] == ["Resources", "Priority"]
    assert [f["n"] for f in waits] == [1, 2] and all(f["job_id"] == "j-pend" for f in waits)
    assert all(f["slurm_job_id"] == jid and f["waited_s"] >= 0 for f in waits)
    # attach shows them and keeps following a pending job
    out = io.BytesIO()
    fake.end(jid, "TIMEOUT")
    assert WJ.attach(cfg, "j-pend", 0, out) == W.EXIT_ERROR
    types = [json.loads(ln)["type"] for ln in out.getvalue().splitlines()]
    assert types == ["slot_wait", "slot_wait", "error"]


def test_running_states_read_running(wenv, fake):
    cfg = _cfg(wenv)
    assert _submit(cfg, _plain("j-run"))[0] == 0
    job, jid = _job_dir(wenv, "j-run"), fake.ids()[0]
    for st in ("CONFIGURING", "RUNNING", "COMPLETING", "SUSPENDED"):
        fake._set(jid, state=st)
        assert _state(cfg, job) == "running", st
    assert _frames(job) == []


@pytest.mark.parametrize("slurm_state", ["OUT_OF_MEMORY", "TIMEOUT", "NODE_FAIL", "PREEMPTED",
                                         "FAILED", "BOOT_FAIL", "DEADLINE", "CANCELLED",
                                         "COMPLETED"])
def test_an_end_without_result_writes_one_error_frame(wenv, fake, slurm_state):
    cfg = _cfg(wenv)
    assert _submit(cfg, _plain("j-end"))[0] == 0
    job, jid = _job_dir(wenv, "j-end"), fake.ids()[0]
    fake.end(jid, slurm_state, exit_code=137 if slurm_state == "FAILED" else 0)
    assert _state(cfg, job) == "error"
    assert _state(cfg, job) == "error"                  # read again: still one frame
    (frame,) = _frames(job)
    assert frame["type"] == "error" and frame["stage"] == "slurm"
    assert frame["slurm_state"] == slurm_state and frame["infra"] is True
    assert frame["slurm_job_id"] == jid and "exit_code" in frame
    res = _read(job / "result.json")
    assert res["state"] == "error" and res["slurm_state"] == slurm_state
    assert _read(job / "job.json")["state"] == "error"
    out = io.BytesIO()                                  # attach ends normally on it
    assert WJ.attach(cfg, "j-end", 0, out) == W.EXIT_ERROR


def test_a_job_still_holding_its_lock_is_not_finished_by_a_reader(wenv, fake):
    cfg = _cfg(wenv)
    assert _submit(cfg, _plain("j-lock"))[0] == 0
    job, jid = _job_dir(wenv, "j-lock"), fake.ids()[0]
    fake.end(jid, "COMPLETED")
    fd = WJ._take_lock_nb(job, 1.0)                     # the job process, still writing
    try:
        assert _state(cfg, job) == "running" and _frames(job) == []
    finally:
        os.close(fd)
    assert _state(cfg, job) == "error"


def test_unknown_to_slurm_is_lost_and_a_cli_failure_never_is(wenv, fake):
    cfg = _cfg(wenv)
    assert _submit(cfg, _plain("j-lost"))[0] == 0
    job, jid = _job_dir(wenv, "j-lost"), fake.ids()[0]
    fake.fail("squeue", "slurm_load_jobs error: Unable to contact slurm controller")
    assert _state(cfg, job) == "accepted"
    fake.heal("squeue")
    fake.forget(jid)                                    # past MinJobAge, no accounting
    assert _state(cfg, job) == "lost"
    assert ["--json", "-j", str(jid)] in fake.calls("sacct")


# ---- cancel ------------------------------------------------------------------------------------

def test_cancel_terms_a_running_job_and_scancels_a_pending_one(wenv, fake):
    cfg = _cfg(wenv)
    assert _submit(cfg, _plain("j-c1"))[0] == 0
    assert _submit(cfg, _plain("j-c2"))[0] == 0
    run_id, pend_id = fake.ids()
    fake._set(run_id, state="RUNNING")
    fake.pending(pend_id, "Priority")
    for job_id in ("j-c1", "j-c2"):
        out = io.BytesIO()
        assert WJ.cancel(cfg, job_id, out) == 0
        assert json.loads(out.getvalue())["cancel"] == "requested"
        assert (_job_dir(wenv, job_id) / "cancel").exists()
    assert fake.calls("scancel") == [["--batch", "--signal=TERM", str(run_id)], [str(pend_id)]]
    # the pending one is CANCELLED with a cancel file: `done` interrupted, not an error
    job = _job_dir(wenv, "j-c2")
    assert _state(cfg, job) == "done"
    frames = _frames(job)
    assert [f["type"] for f in frames] == ["slot_wait", "done"]
    assert frames[-1]["interrupted"] is True and frames[-1]["ok"] is False


def test_the_backstop_scancels_a_job_that_ignored_its_term(wenv, fake):
    cfg = _cfg(wenv, slurm={"cancel_grace_s": 0})
    assert _submit(cfg, _plain("j-stuck"))[0] == 0
    job, jid = _job_dir(wenv, "j-stuck"), fake.ids()[0]
    fake._set(jid, state="RUNNING")
    assert WJ.cancel(cfg, "j-stuck", io.BytesIO()) == 0
    assert _state(cfg, job) == "running"                # the backstop fires on this read
    assert fake.calls("scancel")[-1] == [str(jid)]
    assert _state(cfg, job) == "done"                   # CANCELLED + cancel file
    assert len(fake.calls("scancel")) == 2              # once


# ---- the job body inside a (fake) Slurm job ----------------------------------------------------

def test_a_gate_job_runs_inside_slurm_with_the_allocation_sizing_it(src, tmp_path, wenv, fake):
    seen = tmp_path / "seen"
    h, data = _bundle_job(src, tmp_path, [_gate("hello", f'echo "$NW_GATE_CPUS" > "{seen}"')])
    h["payload"]["resources"] = _resources(cpus=3)
    cfg = _cfg(wenv)
    assert _submit(cfg, h, data)[0] == 0
    job, jid = _job_dir(wenv, h["job_id"]), fake.ids()[0]
    fake._set(jid, cpus=2)                              # Slurm granted 2: that is what wins
    assert fake.run(jid) == 0
    assert seen.read_text().strip() == "2"
    types = [f["type"] for f in _frames(job)]
    assert types == ["accepted", "started", "gate", "done"]
    done = _frames(job)[-1]
    assert done["ok"] is True and done["interrupted"] is False
    # the worker wait is the time the job sat in Slurm's queue (sbatch -> its process started)
    assert 0.0 <= done["slot_wait_s"] < WAIT_S
    assert _state(cfg, job) == "done" and _read(job / "result.json")["attempt"] == 1


def test_sigterm_to_the_batch_step_ends_the_job_interrupted(src, tmp_path, wenv, fake):
    marker = tmp_path / "never"
    h, data = _bundle_job(src, tmp_path, [_gate(
        "wait", f'while [ ! -f "{marker}" ]; do sleep 0.05; done')])
    cfg = _cfg(wenv)
    assert _submit(cfg, h, data)[0] == 0
    job, jid = _job_dir(wenv, h["job_id"]), fake.ids()[0]
    fake.start(jid)
    assert until(lambda: "started" in [f["type"] for f in _frames(job)])
    # scancel --batch --signal=TERM, without a cancel file: the TERM alone cancels
    subprocess.run(["scancel", "--batch", "--signal=TERM", str(jid)], check=True, timeout=30)
    assert fake.finish(jid) == 0
    done = _frames(job)[-1]
    assert done["type"] == "done" and done["interrupted"] is True
    assert _read(job / "result.json")["state"] == "done"


def test_a_requeued_job_reruns_as_attempt_two(src, tmp_path, wenv, fake):
    h, data = _bundle_job(src, tmp_path, [_gate("hello", "echo hi")])
    cfg = _cfg(wenv)
    assert _submit(cfg, h, data)[0] == 0
    job, jid = _job_dir(wenv, h["job_id"]), fake.ids()[0]
    assert fake.run(jid) == 0
    fake.requeue(jid)                                   # SLURM_RESTART_COUNT=1 next run
    assert fake.run(jid) == 0
    assert _read(job / "result.json")["attempt"] == 2
    assert "attempt" in [f["type"] for f in _frames(job)]


# ---- sync gate-job -----------------------------------------------------------------------------

class _Stdin:
    """A pipe for `gate_job`'s stdin: the payload written at once, EOF only on `close`."""

    def __init__(self, payload: bytes):
        self.r, self.w = os.pipe()
        threading.Thread(target=os.write, args=(self.w, payload), daemon=True).start()
        self.stream = os.fdopen(self.r, "rb")

    def close(self):
        try:
            os.close(self.w)
        except OSError:
            pass


def _gate_job(cfg, payload):
    stdin = _Stdin(payload)
    out = io.BytesIO()
    res = {}
    t = threading.Thread(target=lambda: res.setdefault("rc", W.gate_job(cfg, stdin.stream, out)),
                         daemon=True)
    t.start()
    return t, stdin, out, res


def test_sync_gate_job_submits_and_follows(src, tmp_path, wenv, fake):
    h, data = _bundle_job(src, tmp_path, [_gate("hello", "echo hi")])
    cfg = _cfg(wenv)
    t, stdin, out, res = _gate_job(cfg, (json.dumps(h) + "\n").encode() + data)
    try:
        assert until(lambda: fake.ids())
        assert fake.run(fake.ids()[0]) == 0
        join(t)
    finally:
        stdin.close()
        stdin.stream.close()
    assert res["rc"] == 0
    types = [json.loads(ln)["type"] for ln in out.getvalue().splitlines()]
    assert types == ["accepted", "started", "gate", "done"]     # as under `local`


def test_sync_gate_job_stdin_eof_scancels_the_pending_job(src, tmp_path, wenv, fake):
    h, data = _bundle_job(src, tmp_path, [_gate("hello", "echo hi")])
    cfg = _cfg(wenv)
    t, stdin, out, res = _gate_job(cfg, (json.dumps(h) + "\n").encode() + data)
    try:
        assert until(lambda: fake.ids())
        jid = fake.ids()[0]
        fake.pending(jid, "Resources")
        assert until(lambda: b"slot_wait" in out.getvalue())
        stdin.close()                                   # the coordinator went away
        join(t)
    finally:
        stdin.close()
        stdin.stream.close()
    assert fake.calls("scancel") == [[str(jid)]] and fake.job(jid)["state"] == "CANCELLED"
    frames = [json.loads(ln) for ln in out.getvalue().splitlines()]
    assert frames[0]["type"] == "slot_wait" and frames[0]["reason"] == "Resources"
    assert frames[-1]["type"] == "done" and frames[-1]["interrupted"] is True
    assert res["rc"] == 0


def test_sync_gate_job_refuses_a_persona_and_an_existing_job(wenv, fake):
    cfg = _cfg(wenv)
    h = _plain("j-exists")
    assert _submit(cfg, h)[0] == 0
    out = io.BytesIO()
    assert W.gate_job(cfg, _line(h), out) == W.EXIT_ERROR
    assert json.loads(out.getvalue())["stage"] == "claim"
    p = dict(_plain("j-persona"), kind="persona")
    out = io.BytesIO()
    assert W.gate_job(cfg, _line(p), out) == W.EXIT_ERROR
    assert json.loads(out.getvalue())["stage"] == "validate"
    assert len(fake.calls("sbatch")) == 1


# ---- coordinator side --------------------------------------------------------------------------

def test_runs_slurm_reads_the_cached_worker_info(tmp_path):
    cfg = {"data_dir": str(tmp_path)}
    WK.path(cfg).parent.mkdir(parents=True, exist_ok=True)
    WK.path(cfg).write_text(json.dumps({"schema": WK.SCHEMA, "workers": [
        {"name": "box", "ssh": "nw@box", "info": {"features": ["async", "executor:slurm"]}},
        {"name": "old", "ssh": "nw@old", "info": {"features": ["async", "executor:local"]}},
        {"name": "bare", "ssh": "nw@bare"}]}))
    assert WK.runs_slurm(cfg, "box") is True
    assert not any(WK.runs_slurm(cfg, n) for n in ("old", "bare", "nope", None))


def test_host_slot_is_a_no_op(wenv, fake):
    ex = ES.SlurmExecutor()
    cancel = threading.Event()
    with ex.host_slot(_cfg(wenv), {}, cancel, lambda *a: None) as hold:
        assert hold.interrupted is False and hold.waited_s == 0.0
    cancel.set()
    with ex.host_slot(_cfg(wenv), {}, cancel, lambda *a: None) as hold:
        assert hold.interrupted is True
    assert ex.lease_cancels is False and ex.term_cancels is True


# ---- requests that can never be scheduled are clamped ------------------------------------------

def _argv_opts(argv):
    return {a.split("=", 1)[0]: a.split("=", 1)[1] for a in argv if "=" in a}


def test_a_request_over_the_partition_and_node_limits_is_clamped(wenv, fake):
    """The box's real `scontrol show partition/node --json`: nw-gatejob MaxCPUsPerNode=8, the
    node 10 usable cpus (CoreSpecCount=1) and 31955-6000 MB. jerome's whole-box wish (12 cpus,
    30G) would stay PENDING forever: it is clamped and the clamp recorded."""
    rc, _ = _submit(_cfg(wenv), _plain("j-big", cpus=12, mem_mb=30 * 1024, time_s=6 * 3600))
    assert rc == 0
    opts = _argv_opts(fake.calls("sbatch")[0])
    assert opts["--cpus-per-task"] == "8" and opts["--mem"] == "25955M"
    assert opts["--time"] == "240"                      # the partition's MaxTime
    warns = _read(_job_dir(wenv, "j-big") / "job.json")["slurm_warnings"]
    assert any("cpus 12 clamped to 8" in w and "nw-gatejob" in w for w in warns)
    assert any("mem 30720M clamped to 25955M" in w for w in warns)


def test_the_named_partition_s_limits_apply(wenv, fake):
    cfg = _cfg(wenv, slurm={"partition": "nw-persona"})
    assert _submit(cfg, _plain("j-persona-part", cpus=6, mem_mb=8192))[0] == 0
    opts = _argv_opts(fake.calls("sbatch")[0])
    assert opts["--cpus-per-task"] == "4" and opts["--mem"] == "8192M"


def test_a_jerome_sized_request_fits_unclamped(wenv, fake):
    """JEROME-0022's declared request (6 cpus, 8G, jerome_target) fits nw-gatejob as is."""
    assert _submit(_cfg(wenv), _plain("j-jerome", cpus=6, mem_mb=8192,
                                      licenses={"jerome_target": 1}))[0] == 0
    opts = _argv_opts(fake.calls("sbatch")[0])
    assert (opts["--cpus-per-task"], opts["--mem"], opts["--licenses"]) == \
        ("6", "8192M", "jerome_target:1")
    assert "slurm_warnings" not in _read(_job_dir(wenv, "j-jerome") / "job.json")


def test_unreadable_limits_clamp_nothing(wenv, fake):
    fake.fixture("partition", None)
    assert _submit(_cfg(wenv), _plain("j-nolim", cpus=12))[0] == 0
    assert _argv_opts(fake.calls("sbatch")[0])["--cpus-per-task"] == "12"


# ---- the pending reason reaches the coordinator --------------------------------------------------

def test_the_pending_reason_reaches_the_job_record_and_the_gate_record(tmp_path):
    from nightwright.agentic import gates as G
    from nightwright.agentic import remote_gates as RG
    from nightwright.agentic import remote_jobs as RJ
    cfg = {"data_dir": str(tmp_path / "coord")}
    rec = RJ.create(cfg, job_id="C-1-g1-a1", campaign="C-1", worker="box", attempt=1, seq=1,
                    submitted_at=time.time(), deadline_at=time.time() + 600)
    job = RG._Job(cfg, {"name": "box"}, rec, {"gates": []}, 1)
    job.feed({"type": "slot_wait", "job_id": "C-1-g1-a1", "n": 1, "waited_s": 3.0,
              "reason": "Resources"})
    assert RJ.read(cfg, "C-1-g1-a1")["wait_reason"] == "Resources"
    run = job.feed({"type": "done", "job_id": "C-1-g1-a1", "n": 2, "ok": True,
                    "interrupted": False, "duration_s": 1.0, "slot_wait_s": 0.0, "changed": []})
    assert run.wait_reason == "Resources"
    assert G.as_payload(run)["wait_reason"] == "Resources"
    assert "wait_reason" not in G.as_payload(G.GateRun(1, True, (), "t", 0.0))
