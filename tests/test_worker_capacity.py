"""SPEC-0508: `nw worker info --json` reports the host's capacity and load (from `/proc`, or from
`scontrol show node --json` under the slurm executor) and `nw workers list` renders it from the
cached `info`."""
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from nightwright.agentic import worker as W
from nightwright.agentic import worker_capacity as WC
from nightwright.agentic import workers as WK
from nightwright.cli import cli
from waits import until

pytestmark = pytest.mark.fast

FIXTURE = Path(__file__).parent / "fixtures" / "slurm" / "scontrol_show_node.json"


def _fake_proc(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "meminfo").write_text("MemTotal:       32722284 kB\nMemFree:         5267880 kB\n"
                                  "MemAvailable:   16750632 kB\n")
    (root / "loadavg").write_text("6.10 5.80 5.20 3/1040 274684\n")
    return root


def _cfg(tmp_path, **workers):
    return {"data_dir": str(tmp_path / "data"), "agentic": {"workers": workers}}


def test_proc_source_reads_cpus_memory_and_load(tmp_path):
    cap = WC.capacity(_cfg(tmp_path), root=_fake_proc(tmp_path / "proc"), cpu_count=lambda: 12)
    assert cap["schema"] == "nw.worker.capacity.v1" and cap["source"] == "proc"
    assert cap["cpus"] == 12 and cap["mem_total_mb"] == 31955
    assert cap["mem_available_mb"] == 16358 and cap["loadavg"] == [6.1, 5.8, 5.2]
    assert cap["alloc_cpus"] is None and cap["alloc_mem_mb"] is None and cap["jobs"] == []
    assert WC.ratios(cap) == (0.51, 0.49)


class _Run:
    def __init__(self, rc=0, out="", err=""):
        self.calls, self.rc, self.out, self.err = [], rc, out, err

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.rc, self.out, self.err)


def test_slurm_source_reads_the_recorded_scontrol_node(tmp_path):
    """The fixture is real `scontrol show node morais-Desktop --json` output (Slurm 26.05.5,
    recorded 2026-11-09 on the box)."""
    run = _Run(out=FIXTURE.read_text())
    cap = WC.capacity(_cfg(tmp_path, executor="slurm"), root=_fake_proc(tmp_path / "proc"),
                      run=run, host="morais-Desktop")
    assert run.calls == [["scontrol", "show", "node", "morais-Desktop", "--json"]]
    assert cap["source"] == "slurm" and "error" not in cap
    node = json.loads(FIXTURE.read_text())["nodes"][0]
    assert cap["cpus"] == node["cpus"] == 12
    assert cap["mem_total_mb"] == node["real_memory"] == 31955
    assert cap["alloc_cpus"] == node["alloc_cpus"] and cap["alloc_mem_mb"] == node["alloc_memory"]
    assert cap["mem_available_mb"] == node["real_memory"] - node["alloc_memory"]
    assert cap["free_mem_mb"] == node["free_mem"]["number"]
    assert cap["loadavg"] == [round(node["cpu_load"] / 100, 2)]
    load, mem = WC.ratios(cap)
    assert mem == round(node["alloc_memory"] / node["real_memory"], 2)


@pytest.mark.parametrize("run", [_Run(rc=1, err="slurm_load_node error"), _Run(out="not json")])
def test_a_slurm_failure_falls_back_to_proc_with_an_error(tmp_path, run):
    cap = WC.capacity(_cfg(tmp_path, executor="slurm"), root=_fake_proc(tmp_path / "proc"),
                      run=run, cpu_count=lambda: 4, host="h")
    assert cap["source"] == "proc" and cap["cpus"] == 4 and "scontrol" in cap["error"]


def test_a_running_job_reports_its_cpu_and_memory(tmp_path):
    """A job whose `job.lock` is held and whose `proc.json` names a gate group that burns CPU
    and holds ~50 MB: the real `/proc` sample sees both."""
    cfg = _cfg(tmp_path)
    job = W.jobs_dir(cfg) / "j-busy"
    job.mkdir(parents=True)
    burn = subprocess.Popen(
        [sys.executable, "-c", "b = bytearray(50 * 2**20); b[::4096] = b'x' * len(b[::4096])\n"
                               "while True: pass"], start_new_session=True)
    lock = os.open(job / "job.lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        (job / "proc.json").write_text(json.dumps({"pid": os.getpid(), "pgid": 999999999,
                                                   "gates": [{"pid": burn.pid, "pgid": burn.pid}]}))
        assert until(lambda: WC._rss_mb(Path("/proc"), str(burn.pid)) >= 45)
        (row,) = WC.jobs_usage(cfg, sample_s=0.3)
        assert row["job_id"] == "j-busy" and row["state"] == "running"
        assert row["cpu_pct"] > 10 and row["rss_mb"] >= 45
    finally:
        burn.kill()
        burn.wait()
        os.close(lock)
    assert WC.jobs_usage(cfg, sample_s=0.3) == []          # no lock held: nothing running


def test_info_has_the_capacity_block(tmp_path):
    cap = W.info(_cfg(tmp_path))["capacity"]
    assert cap["source"] == "proc" and cap["cpus"] == os.cpu_count()
    assert cap["mem_total_mb"] > 0 and len(cap["loadavg"]) == 3 and cap["jobs"] == []


def test_workers_list_renders_the_cached_capacity(tmp_path, monkeypatch):
    cfg = {"data_dir": str(tmp_path / "data")}
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    cap = WC.from_proc(_fake_proc(tmp_path / "proc"), cpu_count=lambda: 12)
    slurm = {**cap, "source": "slurm", "alloc_cpus": 3, "alloc_mem_mb": 7989, "jobs": []}
    WK.path(cfg).parent.mkdir(parents=True, exist_ok=True)
    WK.path(cfg).write_text(json.dumps({"schema": "nw.workers.v1", "workers": [
        {"name": "box", "ssh": "nw@box", "slots": 2, "enabled": True,
         "info": {"capacity": {**cap, "jobs": []}}},
        {"name": "big", "ssh": "nw@big", "slots": 2, "enabled": True,
         "info": {"capacity": slurm}},
        {"name": "old", "ssh": "nw@old", "slots": 1, "enabled": True, "info": {}}]}))
    out = CliRunner().invoke(cli, ["workers", "list", "--json"])
    assert out.exit_code == 0, out.output
    rows = {r["name"]: r for r in json.loads(out.output)["workers"]}
    assert rows["box"]["capacity"]["cpus"] == 12 and rows["old"]["capacity"] is None
    text = CliRunner().invoke(cli, ["workers", "list"], terminal_width=200).output
    box = next(ln for ln in text.splitlines() if " box " in ln)
    assert "12" in box and "51%" in box and "49%" in box
    big = next(ln for ln in text.splitlines() if " big " in ln)
    assert "3/12" in big and "25%" in big                   # allocated 7989 of 31955 MB
    old = next(ln for ln in text.splitlines() if " old " in ln)
    assert old.count("-") >= 3
