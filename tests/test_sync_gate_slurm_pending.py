"""SPEC-0513 (GATEJOB-1671): the synchronous `gate-job` path does not count Slurm queue time.

A coordinator `_dispatch` against a real `nw worker gate-job` (the Slurm executor, driven by the
fake Slurm CLI, `tests/slurm_fake.py`): a job PENDING in Slurm longer than
`agentic.workers.slot_wait_s` that then starts succeeds; one still PENDING past
`agentic.slurm.max_pending_s` fails with the distinct `slurm_pending` stage (`nw gate run`
reports it as `unavailable`: `tests/test_gate_jobs.py`); a local worker's `slot_wait` frames (no
Slurm reason) still end at `slot_wait_s`. The limits are seconds, not the 300 s / 6 h defaults."""
import json
import sys
import threading
import time

import pytest

from nightwright.agentic import executor_slurm as ES
from nightwright.agentic import remote_gates as RG
from nightwright.agentic import worker as W
from slurm_fake import SlurmFake
from test_worker import _bundle_job, _gate, _no_finish_gc, src, wenv  # noqa: F401
from waits import join, until

pytestmark = pytest.mark.integration


@pytest.fixture
def fake(tmp_path, monkeypatch):
    f = SlurmFake(tmp_path / "slurm").install(monkeypatch)
    ES.forget()
    ES._LICENSES.clear()
    ES._LIMITS.clear()
    monkeypatch.setenv(W.SLOT_WAIT_ENV, "0.2")      # a `slot_wait` frame every 0.2 s
    monkeypatch.setenv(W.FINISH_GC_ENV, "0")
    yield f
    f.close()
    ES.forget()
    ES._LICENSES.clear()
    ES._LIMITS.clear()


def _worker(tmp_path, wenv):
    """A worker whose `ssh` runs this host's `nw worker gate-job` under the Slurm executor."""
    cfg = {"data_dir": wenv["NW_DATA_DIR"], "config_dir": wenv["NW_CONFIG_DIR"],
           "agentic": {"workers": {"min_free_gb": 0, "abort_free_gb": 0, "executor": "slurm"},
                       "slurm": {"cache_s": 0}}}
    script = tmp_path / "ssh.py"
    script.write_text(
        "import json, sys\n"
        "from nightwright.agentic import worker as W\n"
        f"cfg = json.loads({json.dumps(json.dumps(cfg))})\n"
        "if 'gate-job' not in sys.argv:\n"
        "    print('{}'); sys.exit(0)\n"          # `poll` for accounting: nothing to report
        "sys.exit(W.gate_job(cfg, sys.stdin.buffer, sys.stdout.buffer))\n")
    return {"name": "box", "ssh_argv": [sys.executable, str(script)], "ssh": "box", "nw": "nw"}


def _coordinator(tmp_path, slot_wait_s, max_pending_s):
    return {"data_dir": str(tmp_path / "coord"),
            "agentic": {"workers": {"slot_wait_s": slot_wait_s},
                        "slurm": {"max_pending_s": max_pending_s}}}


def _dispatch(worker, header, bundle, cfg, tmp_path):
    out = {}

    def go():
        try:
            out["o"] = RG._dispatch(worker, header, bundle, cfg, "C-1", 1, None,
                                    gates_dir=tmp_path / "gates", keep_changed=True)
        except RG.RemoteFailure as f:
            out["f"] = f
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t, out


def _job(src, tmp_path):
    h, data = _bundle_job(src, tmp_path, [_gate("hello", "echo hi")])
    bundle = tmp_path / "job.bundle"
    bundle.write_bytes(data)
    return h, bundle


def test_pending_in_slurm_past_slot_wait_then_starting_succeeds(src, tmp_path, wenv, fake):
    h, bundle = _job(src, tmp_path)
    cfg = _coordinator(tmp_path, slot_wait_s=1, max_pending_s=60)
    t, out = _dispatch(_worker(tmp_path, wenv), h, bundle, cfg, tmp_path)
    assert until(lambda: fake.ids())
    jid = fake.ids()[0]
    fake.pending(jid, "Resources")
    t0 = time.monotonic()
    # held PENDING three times `slot_wait_s`: the coordinator must still be waiting
    assert not until(lambda: not t.is_alive(), timeout=3.0)
    assert time.monotonic() - t0 >= 3.0 and "f" not in out
    assert fake.run(jid) == 0
    join(t)
    assert "f" not in out, out.get("f") and out["f"].error
    run = out["o"].run
    assert run.ok and [r.name for r in run.results] == ["hello"]
    assert run.wait_reason == "Resources"


def test_pending_past_max_pending_fails_as_slurm_pending(src, tmp_path, wenv, fake):
    h, bundle = _job(src, tmp_path)
    cfg = _coordinator(tmp_path, slot_wait_s=1, max_pending_s=3)
    t, out = _dispatch(_worker(tmp_path, wenv), h, bundle, cfg, tmp_path)
    assert until(lambda: fake.ids())
    jid = fake.ids()[0]
    fake.pending(jid, "Resources")
    t0 = time.monotonic()
    join(t)
    assert time.monotonic() - t0 >= 2.5          # not the 1 s `slot_wait_s`
    f = out["f"]
    assert f.stage == "slurm_pending" and not f.cooldown
    assert "still pending in Slurm (Resources) after 3s" in f.error
    assert "agentic.slurm.max_pending_s" in f.error
    # giving up closed the stream: the worker scancels the PENDING job
    assert until(lambda: fake.job(jid)["state"] == "CANCELLED")


def _scripted_worker(tmp_path, frames):
    out = tmp_path / "frames.jsonl"
    out.write_text("".join(json.dumps({"schema": W.FRAME_SCHEMA, "job_id": "J", "type": k, **kw})
                           + "\n" for k, kw in frames))
    ssh = tmp_path / "ssh"
    ssh.write_text(f"#!/bin/sh\ncat '{out}'\nexec sleep 60\n")  # stays up, silent
    ssh.chmod(0o755)
    return {"name": "box", "ssh_argv": [str(ssh)], "ssh": "box", "nw": "nw"}


@pytest.mark.parametrize("frames", [
    [("accepted", {}), ("slot_wait", {"waited_s": 0.0})],                 # local: a host slot
    [("accepted", {})],
])
def test_a_local_slot_wait_still_ends_at_slot_wait_s(tmp_path, frames):
    worker = _scripted_worker(tmp_path, frames)
    header = {"job_id": "J", "bundle_bytes": 0, "timeout_s": 60,
              "payload": {"gates": [{"name": "g1", "cmd": "x"}]}}
    cfg = _coordinator(tmp_path, slot_wait_s=1, max_pending_s=600)
    t0 = time.monotonic()
    with pytest.raises(RG.RemoteFailure) as e:
        RG._dispatch(worker, header, tmp_path / "none", cfg, "C-1", 1, None,
                     gates_dir=tmp_path / "gates")
    assert time.monotonic() - t0 < 30
    assert e.value.stage == "slot_wait" and "held no gate slot after 1s" in e.value.error
