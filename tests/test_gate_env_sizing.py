"""SPEC-0509: every gate, local or remote, sees `NW_GATE_CPUS` / `NW_GATE_MEM_MB` = its job's
request; inside a Slurm job (`SLURM_JOB_ID` set) the allocation (`SLURM_CPUS_ON_NODE`, else
`SLURM_CPUS_PER_TASK`; `SLURM_MEM_PER_NODE`) wins. A worker gate of the local executor sees no
`SLURM_*` key and the request (the slurm executor's side: `tests/test_executor_slurm.py`)."""
import base64
import gzip
import io
import json
import os
import threading

import pytest

from nightwright.agentic import gate_resources as GR
from nightwright.agentic import gates as G
from nightwright.agentic import worker as W
from test_worker import _bundle_job, _gate, _no_finish_gc, src, wenv  # noqa: F401

pytestmark = pytest.mark.integration

PRINT = 'echo "cpus=$NW_GATE_CPUS mem=$NW_GATE_MEM_MB"'


@pytest.fixture(autouse=True)
def _no_slurm(monkeypatch):
    for k in GR.SLURM_GATE_ENV + ("SLURM_SUBMIT_DIR",):
        monkeypatch.delenv(k, raising=False)


def test_sizing_env_takes_the_request_and_slurm_wins():
    req = {"cpus": 3, "mem_mb": 3072}
    assert GR.sizing_env(req, {}) == {"NW_GATE_CPUS": "3", "NW_GATE_MEM_MB": "3072"}
    slurm = {GR.SLURM_JOB_ENV: "9", GR.SLURM_CPUS_ENV: "6", GR.SLURM_MEM_ENV: "8192"}
    assert GR.sizing_env(req, slurm) == {"NW_GATE_CPUS": "6", "NW_GATE_MEM_MB": "8192"}
    # the grant wins over the per-task request: Slurm job 179 asked 3 and got 4 (CR_Core)
    granted = {**slurm, GR.SLURM_CPUS_ENV: "3", GR.SLURM_NODE_CPUS_ENV: "4"}
    assert GR.sizing_env(req, granted)["NW_GATE_CPUS"] == "4"
    assert GR.sizing_env(req, {GR.SLURM_JOB_ENV: "9", GR.SLURM_CPUS_ENV: "junk"}) == {
        "NW_GATE_CPUS": "3", "NW_GATE_MEM_MB": "3072"}
    # outside a Slurm job (no SLURM_JOB_ID) stray SLURM_* keys size nothing
    assert GR.sizing_env(req, {GR.SLURM_CPUS_ENV: "6", GR.SLURM_NODE_CPUS_ENV: "6"}) == {
        "NW_GATE_CPUS": "3", "NW_GATE_MEM_MB": "3072"}
    assert GR.sizing_env(None, {}) == {}


def test_slurm_env_is_the_fixed_allowlist_inside_a_job_only():
    environ = {"SLURM_JOB_ID": "179", "SLURM_JOB_NAME": "GATEJOB-1701", "SLURM_CPUS_ON_NODE": "4",
               "SLURM_CPUS_PER_TASK": "3", "SLURM_MEM_PER_NODE": "3072",
               "SLURM_JOB_PARTITION": "nw-gatejob", "SLURM_SUBMIT_DIR": "/home/nw",
               "SLURM_NODELIST": "box", "SLURM_STEP_ID": "0", "SLURM_CONF": "/etc/slurm.conf",
               "SLURM_JOB_LICENSES": "", "PATH": "/bin"}
    assert GR.slurm_env(environ) == {
        "SLURM_JOB_ID": "179", "SLURM_JOB_NAME": "GATEJOB-1701", "SLURM_CPUS_ON_NODE": "4",
        "SLURM_CPUS_PER_TASK": "3", "SLURM_MEM_PER_NODE": "3072",
        "SLURM_JOB_PARTITION": "nw-gatejob"}
    assert GR.slurm_env({k: v for k, v in environ.items() if k != "SLURM_JOB_ID"}) == {}
    assert all(k.startswith("SLURM_") for k in GR.SLURM_GATE_ENV)


def _local_run(tmp_path, gates):
    data = tmp_path / "data"
    wt = tmp_path / "wt"
    wt.mkdir()
    cfg = {"data_dir": str(data), "config_dir": str(tmp_path / "conf")}
    state = {"id": "C-1", "spec_id": "SPEC-1", "repo": {"worktree": str(wt), "base": "main"}}
    (data / "agentic" / "campaigns" / "C-1").mkdir(parents=True)
    run = G._run_all_held({"gates": gates}, state, cfg, "C-1", str(wt), 1, None, None)
    return run, data / "agentic" / "campaigns" / "C-1" / "gates"


def test_a_local_gate_sees_its_jobs_request(tmp_path):
    gates = [{"name": "print", "cmd": PRINT, "timeout_s": 60},
             {"name": "big", "cmd": "true", "timeout_s": 60,
              "resources": {"cpus": 4, "mem": "6G"}}]
    run, gdir = _local_run(tmp_path, gates)
    assert run.ok
    # the job runs both gates in sequence: its request is their max, and every gate sees it
    assert (gdir / "001-print.log").read_text().strip() == "cpus=4 mem=6144"


def test_a_local_gate_under_slurm_sees_the_allocation(tmp_path, monkeypatch):
    monkeypatch.setenv(GR.SLURM_JOB_ENV, "77")         # a campaign run inside a Slurm job
    monkeypatch.setenv(GR.SLURM_CPUS_ENV, "7")
    monkeypatch.setenv(GR.SLURM_MEM_ENV, "7000")
    run, gdir = _local_run(tmp_path, [{"name": "print", "cmd": PRINT, "timeout_s": 60}])
    assert (gdir / "001-print.log").read_text().strip() == "cpus=7 mem=7000"


def _remote_log(src, tmp_path, wenv, resources, cmd=PRINT):
    h, data = _bundle_job(src, tmp_path, [_gate("print", cmd)])
    if resources is not None:
        h["payload"]["resources"] = resources
    cfg = {"data_dir": wenv["NW_DATA_DIR"], "config_dir": wenv["NW_CONFIG_DIR"],
           "agentic": {"workers": {"min_free_gb": 0, "abort_free_gb": 0}}}
    out = io.BytesIO()
    r, w = os.pipe()                            # stdin stays open (EOF would be a cancel)
    payload = (json.dumps(h) + "\n").encode() + data
    threading.Thread(target=lambda: os.write(w, payload), daemon=True).start()
    try:
        with os.fdopen(r, "rb") as stdin:
            assert W.gate_job(cfg, stdin, out) == 0
    finally:
        os.close(w)
    (frame,) = [f for f in map(json.loads, out.getvalue().splitlines()) if f["type"] == "gate"]
    return gzip.decompress(base64.b64decode(frame["files"]["001-print.log"])).decode().strip()


def test_a_remote_gate_sees_the_headers_request(src, tmp_path, wenv):
    req = GR.job_request([{"name": "print", "cmd": "x", "resources": {"cpus": 5, "mem": "5G"}}],
                         {}, time_s=60)
    assert _remote_log(src, tmp_path, wenv, req) == "cpus=5 mem=5120"


def test_a_remote_gate_from_an_older_coordinator_gets_the_defaults(src, tmp_path, wenv):
    assert _remote_log(src, tmp_path, wenv, None) == "cpus=1 mem=1024"


def test_a_local_executor_worker_gate_sees_no_slurm_and_the_request(src, tmp_path, wenv,
                                                                    monkeypatch):
    # stray Slurm keys in the worker's own env (a local job is never inside Slurm) pass
    # neither into the gate nor into its sizing
    for k, v in {GR.SLURM_JOB_ENV: "5", GR.SLURM_CPUS_ENV: "2", GR.SLURM_NODE_CPUS_ENV: "2",
                 GR.SLURM_MEM_ENV: "2500"}.items():
        monkeypatch.setenv(k, v)
    req = GR.job_request([{"name": "print", "cmd": "x"}], {}, time_s=60)
    cmd = PRINT[:-1] + ' job=${SLURM_JOB_ID:-none}"'
    assert _remote_log(src, tmp_path, wenv, {**req, "cpus": 9}, cmd) == "cpus=9 mem=1024 job=none"
