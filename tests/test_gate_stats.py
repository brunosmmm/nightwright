"""SPEC-0514: `nw gate stats` on synthetic gate records: nearest-rank percentiles, the suggested
request and its rounding, the `over`/`under` flags (and no memory `over` from wait4 numbers),
OOM counts, the window and project filters, `--json`."""
import json
import time
from datetime import datetime, timedelta

import pytest
from click.testing import CliRunner

from nightwright.agentic import gate_stats as GS
from nightwright.agentic.cli_gate import gate_group

pytestmark = pytest.mark.fast


def _cfg(tmp_path):
    return {"data_dir": str(tmp_path)}


def _stamp(days_ago):
    return (datetime.now().astimezone() - timedelta(days=days_ago)).isoformat(timespec="seconds")


def _gate(d, stem, name, wall, cpu, rss, scope="job_cgroup", req=(3, 3072), days_ago=0.1,
          slurm=None):
    d.mkdir(parents=True, exist_ok=True)
    res = {"request": {"cpus": req[0], "mem_mb": req[1], "licenses": {}},
           "used": {"cpu_s": cpu, "max_rss_mb": rss, "rss_scope": scope, "wall_s": wall}}
    if slurm:
        res["slurm"] = slurm
    (d / f"{stem}.json").write_text(json.dumps(
        {"schema": "nw.gate.v1", "name": name, "ok": True, "duration_s": wall,
         "started_at": _stamp(days_ago), "resources": res,
         "summary": {"format": "pytest", "passed": 7, "failed": 1, "errors": 0, "skipped": 2}}))


def _campaign(tmp_path, cid, project):
    c = tmp_path / "agentic" / "campaigns" / cid
    (c / "gates").mkdir(parents=True)
    (c / "state.json").write_text(json.dumps({"project": project}))
    return c / "gates"


def test_percentile_is_nearest_rank():
    assert GS.percentile([], 95) is None
    assert GS.percentile([5], 95) == 5
    assert GS.percentile([1, 2, 3, 4], 50) == 2
    assert GS.percentile(list(range(1, 21)), 95) == 19
    assert GS.percentile(list(range(1, 11)), 95) == 10


def test_suggest_rounds_up_to_256m_and_whole_cpus():
    assert GS.suggest(1.01, 100) == {"cpus": 2, "mem_mb": 256}
    assert GS.suggest(2.0, 1000) == {"cpus": 2, "mem_mb": 1280}     # 1250 -> 1280
    assert GS.suggest(0.2, 1024) == {"cpus": 1, "mem_mb": 1280}
    assert GS.suggest(None, None) == {"cpus": None, "mem_mb": None}


def test_parse_since():
    assert GS.parse_since("14d") == 14 * 86400 and GS.parse_since("90m") == 5400
    with pytest.raises(ValueError):
        GS.parse_since("soon")


def test_summary_p50_p95_suggestion_and_over_flag(tmp_path):
    g = _campaign(tmp_path, "C-1", "P")
    for i, (wall, cpu, rss) in enumerate([(100, 100, 400), (100, 150, 500), (200, 300, 600),
                                          (100, 90, 700)]):
        _gate(g, f"00{i}-pytest", "pytest", wall, cpu, rss, req=(5, 5120))
    rows = GS.collect(_cfg(tmp_path), 86400)
    [s] = GS.summarize(rows)
    assert (s["project"], s["gate"], s["n"]) == ("P", "pytest", 4)
    assert s["wall_s"] == {"p50": 100.0, "p95": 200.0}
    assert s["cpus"]["p95"] == 1.5
    assert s["max_rss_mb"] == {"p50": 500.0, "p95": 700.0}
    assert s["suggested"] == {"cpus": 2, "mem_mb": 1024, "mem_exact": True}   # 875 -> 1024
    assert s["flags"] == ["over"]            # 5 cpus > 1.5 x 2, 5120M > 1.5 x 1024M
    assert rows[0]["tests"] == 10


def test_under_flag_when_p95_memory_exceeds_the_request(tmp_path):
    g = _campaign(tmp_path, "C-1", "P")
    _gate(g, "001-check", "check", 10, 10, 1500, req=(1, 1024))
    [s] = GS.summarize(GS.collect(_cfg(tmp_path), None))
    assert s["flags"] == ["under"]


def test_wait4_memory_is_approximate_and_never_flags_over(tmp_path):
    g = _campaign(tmp_path, "C-1", "P")
    _gate(g, "001-a", "a", 10, 10, 100, scope="max_process", req=(1, 4096))
    [s] = GS.summarize(GS.collect(_cfg(tmp_path), None))
    assert s["suggested"]["mem_exact"] is False and s["rss_scope"] == "max_process"
    assert s["flags"] == []                     # over on cpus is the only way, and cpus match
    assert GS.summarize(GS.collect(_cfg(tmp_path), None))[0]["suggested"]["cpus"] == 1


def test_cumulative_job_peak_is_an_upper_bound_not_exact(tmp_path):
    g = _campaign(tmp_path, "C-1", "P")
    _gate(g, "001-a", "a", 10, 10, 100, scope="job_cgroup_cumulative", req=(1, 4096))
    [s] = GS.summarize(GS.collect(_cfg(tmp_path), None))
    assert s["rss_scope"] == "job_cgroup_cumulative" and s["suggested"]["mem_exact"] is False
    assert s["flags"] == []


def test_oom_count_window_project_filter_and_gatejob_rows(tmp_path):
    g = _campaign(tmp_path, "C-1", "P")
    _gate(g, "001-a", "a", 10, 10, 100, slurm={"State": "OUT_OF_MEMORY"})
    _gate(g, "002-a", "a", 10, 10, 100, days_ago=40)           # outside the window
    q = _campaign(tmp_path, "C-2", "Q")
    _gate(q, "001-a", "a", 10, 10, 100)
    jd = tmp_path / "agentic" / "gatejobs" / "GATEJOB-0001"
    (jd).mkdir(parents=True)
    (jd / "job.json").write_text(json.dumps({"project": "P", "worker": "box"}))
    _gate(jd / "gates", "001-a", "a", 10, 10, 100)
    cfg = _cfg(tmp_path)
    rows = GS.collect(cfg, 14 * 86400, "P")
    assert [r["source"] for r in rows] == ["campaign", "gatejob"] or len(rows) == 2
    assert {r["worker"] for r in rows} == {None, "box"}
    [s] = GS.summarize(rows)
    assert s["n"] == 2 and s["oom"] == 1
    assert len(GS.collect(cfg, None)) == 4


def test_cli_table_and_json(tmp_path, monkeypatch):
    g = _campaign(tmp_path, "C-1", "P")
    _gate(g, "001-a", "a", 10, 10, 1500, req=(1, 1024))
    r = CliRunner().invoke(gate_group, ["stats", "--since", "7d"], obj=_cfg(tmp_path))
    assert r.exit_code == 0, r.output
    assert "P/a" in r.output and "flags=under" in r.output and "suggest 1c/2G" in r.output
    r = CliRunner().invoke(gate_group, ["stats", "--json", "--project", "P"], obj=_cfg(tmp_path))
    doc = json.loads(r.output)
    assert doc["schema"] == "nw.gate.stats.v1" and doc["gates"][0]["flags"] == ["under"]
    row = doc["rows"][0]
    assert {"start", "end", "worker", "tests", "cpu_s", "max_rss_mb"} <= set(row)
    assert not any(k.startswith("_") for k in row)
    r = CliRunner().invoke(gate_group, ["stats", "--since", "x"], obj=_cfg(tmp_path))
    assert r.exit_code == 2
    r = CliRunner().invoke(gate_group, ["stats", "--project", "none"], obj=_cfg(tmp_path))
    assert "no gate records" in r.output
