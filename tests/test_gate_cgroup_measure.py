"""SPEC-0514: the job-cgroup measurement of a gate (`gate_cgroup`) on a fake cgroup dir, the
`run_gate` wiring (cgroup numbers replace the `wait4` ones, fallback otherwise) and the
`scope` of merged attempts."""
import os
from pathlib import Path

import pytest

from nightwright.agentic import gate_cgroup as GC
from nightwright.agentic import gates as G

pytestmark = pytest.mark.fast

MB = 2**20


def _cg(root: Path, rel="system.slice/slurmstepd.scope/job_7/step_batch/user/task_0",
        peak=900 * MB, usec=1_000_000):
    d = root / rel
    d.mkdir(parents=True)
    (d / "memory.peak").write_text(f"{peak}\n")
    (d / "cpu.stat").write_text(f"usage_usec {usec}\nuser_usec 1\nsystem_usec 1\n")
    return d


def _proc(tmp_path, rel):
    f = tmp_path / "cgroup"
    f.write_text(f"0::/{rel}\n")
    return str(f)


def test_job_cgroup_walks_up_to_job_dir(tmp_path):
    rel = "system.slice/slurmstepd.scope/job_7/step_batch/user/task_0"
    _cg(tmp_path / "root", rel)
    job = tmp_path / "root/system.slice/slurmstepd.scope/job_7"
    (job / "memory.peak").write_text("1\n")
    (job / "cpu.stat").write_text("usage_usec 5\n")
    got = GC.job_cgroup({"SLURM_JOB_ID": "7"}, str(tmp_path / "root"), _proc(tmp_path, rel))
    assert got == job


def test_job_cgroup_without_job_dir_uses_own_cgroup(tmp_path):
    """The box's layout has no `job_<id>` directory: the process's own cgroup is used."""
    rel = "user.slice/slurm-7.scope/task"
    d = _cg(tmp_path / "root", rel)
    assert GC.job_cgroup({"SLURM_JOB_ID": "7"}, str(tmp_path / "root"),
                         _proc(tmp_path, rel)) == d


def test_job_cgroup_on_the_boxs_layout_is_the_dir_above_step(tmp_path):
    """Real box (slurm 26.05): `.../slurmstepd.scope/<random id>/step_batch/user/task_0`."""
    rel = "system.slice/slurmstepd.scope/s0HNM71ZQH7300/step_batch/user/task_0"
    _cg(tmp_path / "root", rel)
    job = tmp_path / "root/system.slice/slurmstepd.scope/s0HNM71ZQH7300"
    (job / "memory.peak").write_text("1\n")
    (job / "cpu.stat").write_text("usage_usec 5\n")
    assert GC.job_cgroup({"SLURM_JOB_ID": "7"}, str(tmp_path / "root"),
                         _proc(tmp_path, rel)) == job


def test_no_slurm_job_or_missing_files_means_none(tmp_path):
    rel = "a/b"
    _cg(tmp_path / "root", rel)
    assert GC.job_cgroup({}, str(tmp_path / "root"), _proc(tmp_path, rel)) is None
    assert GC.job_cgroup({"SLURM_JOB_ID": "7"}, str(tmp_path / "nope"),
                         _proc(tmp_path, rel)) is None
    assert GC.job_cgroup({"SLURM_JOB_ID": "7"}, str(tmp_path / "root"),
                         str(tmp_path / "no-proc-file")) is None


def test_meter_resets_peak_before_and_reads_after(tmp_path):
    d = _cg(tmp_path, peak=1, usec=1_000_000)
    m = GC.Meter(d)
    m.begin()
    assert (d / "memory.peak").read_text().startswith("0")  # the reset write came first
    (d / "memory.peak").write_text(f"{300 * MB}\n")      # what the gate reached
    (d / "cpu.stat").write_text("usage_usec 3500000\n")
    got = m.end(4.2)
    assert got == {"cpu_s": 2.5, "max_rss_mb": 300.0, "rss_scope": "job_cgroup", "wall_s": 4.2}


def test_meter_refused_reset_reports_cumulative_scope(tmp_path, monkeypatch):
    d = _cg(tmp_path)
    m = GC.Meter(d)
    real_open = open

    class _RO:
        def __init__(self, f): self.f = f
        def write(self, _): raise OSError("refused")
        def flush(self): pass
        def seek(self, n): self.f.seek(n)
        def read(self): return self.f.read()
        def close(self): self.f.close()

    monkeypatch.setattr("builtins.open", lambda p, mode="r", *a, **k:
                        _RO(real_open(p)) if str(p).endswith("memory.peak") else real_open(p, mode, *a, **k))
    m.begin()
    got = m.end(1.0)
    assert got["rss_scope"] == "job_cgroup_cumulative" and got["max_rss_mb"] == 900.0
    assert got["rss_bound"] == "upper"          # the gate did not exceed the job's earlier peak
    m = GC.Meter(d)
    m.begin()
    (d / "memory.peak").write_text(f"{1200 * MB}\n")
    got = m.end(1.0)
    assert got["max_rss_mb"] == 1200.0 and "rss_bound" not in got   # raised by this gate


def test_meter_fallback_when_nothing_to_read(tmp_path):
    assert GC.Meter(None).end(1.0) is None
    m = GC.Meter(None)
    m.begin()
    assert m.end(1.0) is None
    d = _cg(tmp_path)
    m = GC.Meter(d)
    m.begin()
    (d / "cpu.stat").unlink()
    assert m.end(1.0) is None


def _gate(cmd="true"):
    return {"name": "g", "cmd": cmd, "timeout_s": 30, "required": True}


def test_run_gate_records_cgroup_numbers_inside_a_slurm_job(tmp_path, monkeypatch):
    rel = "x/job_9/step"
    root = tmp_path / "root"
    d = _cg(root, rel, peak=0, usec=100)
    monkeypatch.setenv("SLURM_JOB_ID", "9")
    monkeypatch.setattr(GC, "CGROUP_ROOT", str(root))
    monkeypatch.setattr(GC, "own_cgroup", lambda *_a: "/" + rel)
    # the "gate" itself plays the part of the cgroup's accounting while it runs
    cmd = (f"printf '%s\\n' {512 * MB} > {d}/memory.peak; "
           f"printf 'usage_usec 2000100\\n' > {d}/cpu.stat")
    r = G.run_gate(_gate(cmd), tmp_path, {}, tmp_path / "g.log", {})
    used = r.resources["used"]
    assert used["rss_scope"] == "job_cgroup"
    assert used["max_rss_mb"] == 512.0 and used["cpu_s"] == 2.0


def test_run_gate_keeps_wait4_numbers_outside_slurm(tmp_path, monkeypatch):
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    r = G.run_gate(_gate(), tmp_path, {}, tmp_path / "g.log", {})
    assert r.resources["used"]["rss_scope"] == "max_process"


def test_merged_attempts_keep_the_weakest_scope():
    a = {"cpu_s": 1.0, "max_rss_mb": 10.0, "rss_scope": "job_cgroup", "wall_s": 1.0}
    b = {"cpu_s": 1.0, "max_rss_mb": 20.0, "rss_scope": "max_process", "wall_s": 1.0}
    assert G.merge_used([a, a])["rss_scope"] == "job_cgroup"
    assert G.merge_used([a, b])["rss_scope"] == "max_process"
    assert G.merge_used([a, b])["max_rss_mb"] == 20.0
