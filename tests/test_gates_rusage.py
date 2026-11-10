"""SPEC-0504: every gate record carries `resources.request` and `resources.used` (CPU seconds
from `wait4` rusage, peak RSS, wall time), local and remote alike."""
import io
import json
import os
import sys
import threading

import pytest

from nightwright.agentic import gates as G
from nightwright.agentic import gate_resources as GR
from nightwright.agentic import remote_gates as RG
from nightwright.agentic import worker as W
from test_worker import _bundle_job, _gate, _no_finish_gc, src, wenv  # noqa: F401

pytestmark = pytest.mark.integration

# ~0.5 s of CPU in the gate's own process, then ~100 MB held in a child it waits for
BURN = (f"{sys.executable} -c \"import time; t=time.process_time()\n"
        f"while time.process_time() - t < 0.5: pass\" && "
        f"{sys.executable} -c \"b = bytearray(100 * 1024 * 1024); b[::4096] = b'x' * len(b[::4096])\"")


def _sidecar(path):
    return json.loads(path.with_suffix(".json").read_text())


def test_a_local_gate_records_cpu_and_peak_memory(tmp_path):
    gate = {"name": "burn", "cmd": BURN, "timeout_s": 60, "required": True,
            "resources": {"cpus": 2, "mem": "1G"}}
    log = tmp_path / "gates" / "001-burn.log"
    r = G.run_gate(gate, tmp_path, {}, log, {})
    assert r.ok, r.tail
    used = r.resources["used"]
    assert used["cpu_s"] >= 0.4
    assert used["max_rss_mb"] >= 90
    assert used["rss_scope"] == "max_process" and used["wall_s"] == r.duration_s
    assert r.resources["request"] == {"cpus": 2, "mem_mb": 1024, "licenses": {}}
    assert _sidecar(log)["resources"] == r.resources
    rec = G.as_payload(G.GateRun(1, True, (r,), "t", 1.0))["results"][0]
    assert rec["resources"] == r.resources                 # the ledger `gate` event


def test_a_timed_out_gate_still_records_its_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("NW_GATE_GRACE_S", "0.3")
    gate = {"name": "slow", "cmd": "sleep 30", "timeout_s": 1, "required": True}
    r = G.run_gate(gate, tmp_path, {}, tmp_path / "001-slow.log", {})
    assert r.timed_out
    used = r.resources["used"]
    assert set(used) == {"cpu_s", "max_rss_mb", "rss_scope", "wall_s"}
    assert used["cpu_s"] is not None and used["max_rss_mb"] is not None


def test_retries_sum_cpu_and_keep_the_peak(tmp_path):
    flag = tmp_path / "once"
    gate = {"name": "flaky", "timeout_s": 60, "required": True, "retries": 1,
            "cmd": f"if [ -f {flag} ]; then exit 0; else touch {flag}; exit 1; fi"}
    r = G.run_gate_with_retries(gate, tmp_path, {}, tmp_path / "001-flaky.log", {})
    assert r.ok and len(r.attempts) == 2
    assert r.resources["used"]["wall_s"] == pytest.approx(
        sum(a["duration_s"] for a in r.attempts), abs=0.01)
    assert _sidecar(tmp_path / "001-flaky.log")["resources"] == r.resources


def test_a_remote_gate_frame_and_record_carry_resources(src, tmp_path, wenv):
    """The worker measures and sends `resources` in the `gate` frame, with the request the
    coordinator put in `payload.resources`; the coordinator's `gates/*.json` and result keep it."""
    gates = [_gate("burn", BURN, resources={"cpus": 2, "mem": "2G"})]
    h, data = _bundle_job(src, tmp_path, [_gate("burn", BURN)])
    h["payload"]["resources"] = GR.job_request(gates, {}, time_s=600)
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
    res = frame["result"]["resources"]
    assert res["request"] == {"cpus": 2, "mem_mb": 2048, "licenses": {}}
    assert res["used"]["cpu_s"] >= 0.4 and res["used"]["max_rss_mb"] >= 90
    gdir = tmp_path / "coord" / "gates"
    taken = RG._take_gate(frame, gdir, 1, {"burn"})
    assert taken.resources == res
    assert json.loads((gdir / "001-burn.json").read_text())["resources"] == res
    assert G.as_payload(G.GateRun(1, True, (taken,), "t", 1.0))["results"][0]["resources"] == res


def test_the_header_carries_the_job_request_and_strips_gate_resources(tmp_path):
    project = {"gates": [{"name": "pytest", "cmd": "x", "results": {"format": "pytest"},
                          "timeout_s": 100, "resources": {"cpus": 5, "mem": "5G"}},
                         {"name": "lint", "cmd": "y", "timeout_s": 50}]}
    state = {"id": "C-1", "spec_id": "SPEC-1", "repo": {"base": "main", "worktree": str(tmp_path)}}
    h = RG._header(project, state, {}, 1, "C-1-gate-001-a0", "repo", "a" * 40, "b" * 40, [], 0)
    assert all("resources" not in g for g in h["payload"]["gates"])     # an older worker refuses it
    res = h["payload"]["resources"]
    assert res["cpus"] == 5 and res["mem_mb"] == 5120 and res["time_s"] > 0
    assert res["gates"]["lint"] == {"cpus": 1, "mem_mb": 1024, "licenses": {}}
    W.validate_header(h)
