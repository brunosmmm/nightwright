"""SPEC-0580: `nw data usage` is a read-only per-source size/count/retention report that
partitions the agentic dir."""
import json
import os
import time
import types

import pytest
from click.testing import CliRunner

from nightwright.agentic import data_usage as DU
from nightwright.agentic import store
from nightwright.cli import cli

pytestmark = pytest.mark.fast

DAY = 86400
IDS = [s[0] for s in DU.SOURCES]


@pytest.fixture
def cfg(tmp_path):
    return {"data_dir": str(tmp_path)}


def _w(p, n, text=None):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text if text is not None else "x" * n)
    return p


def _fixture(cfg):
    a = store.agentic_dir(cfg)
    for cid, state in (("C-1", "MERGED"), ("C-2", "GATING"), ("C-3", "ABANDONED")):
        d = a / "campaigns" / cid
        _w(d / "state.json", 0, json.dumps({"id": cid, "state": state}))
        _w(d / "ledger.jsonl", 40)
        _w(d / "gates" / "001-gate.json", 30)
        _w(d / "tmp" / "gate-1" / "junk", 20)
        _w(d / "runs" / "001-implementer" / "stream.jsonl", 100)
        _w(d / "runs" / "001-implementer" / "run.json", 10)
    _w(a / "campaigns" / "C-4" / "state.json", 0, "{not json")      # unreadable state
    _w(a / "campaigns" / "x.txt", 7)                                  # loose file -> other
    _w(a / "campaigns" / "C-1" / "runs" / "note.txt", 5)              # loose in runs -> runs
    _w(a / "campaigns" / "C-1" / "runs" / "001-implementer" / "sub" / "stream.jsonl", 3)
    _w(a / "sentinel" / "main.logs" / "001-a.log", 11)
    _w(a / "sentinel" / "main.logs" / "002-b.log", 12)
    _w(a / "sentinel" / "main.json", 13)
    _w(a / "gatejobs" / "GATEJOB-0001" / "job.json", 14)
    _w(a / "gatejobs" / "GATEJOB-0001" / "out.log", 15)
    _w(a / "gatejobs" / ".lock", 0)
    _w(a / "remote-jobs" / "J-1.json", 16)
    _w(a / "job-queue" / "J-2.json", 17)
    _w(a / "job-frames" / "J-1.jsonl", 18)
    _w(a / "runner-jobs" / "r1" / "result.json", 19)
    _w(a / "spec-reviews" / "r1" / "review.json", 21)
    _w(a / "spec-reviews.jsonl", 22)
    _w(a / "events.jsonl", 23)
    _w(a / "events.lock", 0)
    _w(a / "cursors" / "conductor", 4)
    _w(a / "runner.jsonl", 24)
    _w(a / "runner.log", 25)
    _w(a / "conductor" / "decisions.jsonl", 26)
    _w(a / "mailbox" / "steward.jsonl", 27)
    _w(a / "stray.bin", 28)
    _w(a / "workers.json", 29)
    return a


def _rows(doc):
    return {r["id"]: r for r in doc["sources"]}


def _walk_total(root):
    return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(root) for f in fs
               if not os.path.islink(os.path.join(d, f)))


def test_partition_total(cfg):
    a = _fixture(cfg)
    doc = DU.usage(cfg)
    assert doc["schema"] == "nw.data.usage.v1" and doc["root"] == str(a)
    assert doc["total"]["bytes"] == _walk_total(a)
    assert doc["total"]["bytes"] == sum(r["bytes"] for r in doc["sources"])
    nfiles = sum(len(fs) for _, _, fs in os.walk(a))
    assert doc["total"]["files"] == nfiles == sum(r["files"] for r in doc["sources"])
    assert [r["id"] for r in doc["sources"]] == IDS
    assert _rows(doc)["other"]["files"] >= 3                          # stray, workers, x.txt


def test_each_source_scope(cfg):
    _fixture(cfg)
    r = _rows(DU.usage(cfg))
    assert (r["streams"]["files"], r["streams"]["bytes"]) == (3, 300)    # nested sub/ not a stream
    assert r["streams"]["count"] == 3
    # runs: 3 x run.json (30) + note.txt (5) + sub/stream.jsonl (3); 3 run dirs
    assert (r["runs"]["files"], r["runs"]["bytes"], r["runs"]["count"]) == (5, 38, 3)
    assert (r["gates"]["files"], r["gates"]["bytes"]) == (3, 90)
    assert (r["campaign-tmp"]["files"], r["campaign-tmp"]["bytes"],
            r["campaign-tmp"]["count"]) == (3, 60, 3)
    # campaigns: 4 state.json (C-4's holds 9 bytes of garbage + 3 json) + 3 ledgers
    assert r["campaigns"]["files"] == 7 and r["campaigns"]["count"] == 4
    assert r["other"]["bytes"] >= 7 + 28 + 29                          # x.txt, stray, workers
    assert (r["sentinel"]["files"], r["sentinel"]["count"]) == (3, 2)   # main.json not a log
    assert (r["sentinel"]["bytes"]) == 11 + 12 + 13
    assert (r["gatejobs"]["files"], r["gatejobs"]["count"]) == (3, 1)
    assert r["remote-jobs"]["files"] == 3 and r["remote-jobs"]["count"] == 1
    assert r["remote-jobs"]["bytes"] == 16 + 17 + 18
    assert (r["runner-jobs"]["files"], r["runner-jobs"]["count"]) == (1, 1)
    assert r["spec-reviews"]["bytes"] == 21 + 22
    assert r["events"]["bytes"] == 23 + 4
    assert r["runner-logs"]["bytes"] == 24 + 25
    assert r["decisions"]["bytes"] == 26 and r["mailbox"]["bytes"] == 27


def test_classify_shapes():
    c = DU.classify
    assert c(("campaigns", "x.txt")) == "other"
    assert c(("campaigns", "C", "runs", "note.txt")) == "runs"
    assert c(("campaigns", "C", "runs", "r", "stream.jsonl")) == "streams"
    assert c(("campaigns", "C", "runs", "r", "sub", "stream.jsonl")) == "runs"
    assert c(("campaigns", "C", "gates", "g.json")) == "gates"
    assert c(("campaigns", "C", "tmp", "a", "b")) == "campaign-tmp"
    assert c(("campaigns", "C", "state.json")) == "campaigns"
    assert c(("brand-new-thing.json",)) == "other"


def test_by_state(cfg):
    _fixture(cfg)
    row = _rows(DU.usage(cfg))["campaigns"]
    bs = row["by_state"]
    assert {k: v["count"] for k, v in bs.items()} == {
        "MERGED": 1, "GATING": 1, "ABANDONED": 1, "UNKNOWN": 1}
    assert sum(v["count"] for v in bs.values()) == row["count"] == 4
    assert bs["UNKNOWN"]["bytes"] == len("{not json")
    assert sum(v["bytes"] for v in bs.values()) == row["bytes"]


def test_oldest_newest(cfg):
    a = _fixture(cfg)
    t_old, t_new = 1_700_000_000, 1_700_000_000 + 5 * DAY
    os.utime(a / "runner.jsonl", (t_old, t_old))
    os.utime(a / "runner.log", (t_new, t_new))
    r = _rows(DU.usage(cfg))
    assert r["runner-logs"]["oldest"] == "2023-11-14T22:13:20Z"
    assert r["runner-logs"]["newest"] == "2023-11-19T22:13:20Z"
    for p in (a / "mailbox").rglob("*"):
        p.unlink()
    r = _rows(DU.usage(cfg))
    assert r["mailbox"]["oldest"] is None and r["mailbox"]["newest"] is None
    assert r["mailbox"]["files"] == 0 and r["mailbox"]["bytes"] == 0


def test_symlinks_and_vanishing(cfg, monkeypatch):
    a = _fixture(cfg)
    before = DU.usage(cfg)
    outside = a.parent / "outside"
    _w(outside / "big", 1000)
    os.symlink(outside / "big", a / "stray-link")
    os.symlink(outside, a / "campaigns" / "linked-dir")
    after = DU.usage(cfg)
    assert after["total"] == before["total"] and after["skipped"] == 0
    real = DU._stat

    def flaky(entry):
        if entry.name == "stray.bin":
            raise FileNotFoundError(entry.path)
        return real(entry)

    monkeypatch.setattr(DU, "_stat", flaky)
    doc = DU.usage(cfg)
    assert doc["skipped"] == 1
    assert doc["total"]["bytes"] == before["total"]["bytes"] - 28


def test_missing_dir(cfg):
    doc = DU.usage(cfg)
    assert doc["total"] == {"bytes": 0, "files": 0} and doc["skipped"] == 0
    assert all(r["bytes"] == 0 and r["files"] == 0 and r["oldest"] is None for r in doc["sources"])
    assert [r["id"] for r in doc["sources"]] == IDS
    assert not store.agentic_dir(cfg).exists()


def test_retention_follows_config(cfg):
    r = _rows(DU.usage(cfg))
    rt = {k: v["retention"] for k, v in r.items()}
    assert (rt["streams"]["rule"], rt["streams"]["setting"]) == ("age", "agentic.disk_clean.run_days")
    assert (rt["sentinel"]["rule"], rt["sentinel"]["setting"]) == ("age",
                                                                    "agentic.disk_clean.log_days")
    assert (rt["gatejobs"]["rule"], rt["gatejobs"]["setting"]) == (
        "age", "agentic.gate_jobs.retention_days")
    assert (rt["campaign-tmp"]["rule"], rt["campaign-tmp"]["setting"]) == (
        "on-terminal", "agentic.disk_clean")
    assert rt["runner-jobs"]["setting"] is None and "after 7 d" in rt["runner-jobs"]["text"]
    assert "final records" in rt["remote-jobs"]["text"] and "queued ones kept" in rt["remote-jobs"]["text"]
    assert rt["remote-jobs"]["setting"] == "remote_jobs.prune"
    assert all(rt[k]["setting"] is None for k in ("campaigns", "other"))
    assert "older than 14 d" in r["streams"]["retention"]["text"]
    assert "older than 14 d" in r["sentinel"]["retention"]["text"]
    assert "newest 40 per dir" in r["sentinel"]["retention"]["text"]
    assert "older than 7 d" in r["gatejobs"]["retention"]["text"]
    for sid in ("campaigns", "runs", "gates", "events", "runner-logs", "decisions", "mailbox",
                "spec-reviews", "other"):
        assert r[sid]["retention"]["rule"] == "forever", sid
    assert r["remote-jobs"]["retention"]["rule"] == "on-campaign-end"
    assert r["runner-jobs"]["retention"]["rule"] == "age"        # runner_jobs._prune, 7 d
    cfg2 = {**cfg, "agentic": {"disk_clean": {"run_days": 3, "log_days": 5.5},
                               "gate_jobs": {"retention_days": 2}}}
    r = _rows(DU.usage(cfg2))
    assert "older than 3 d" in r["streams"]["retention"]["text"]
    assert "older than 5.5 d" in r["sentinel"]["retention"]["text"]
    assert "older than 2 d" in r["gatejobs"]["retention"]["text"]
    off = {**cfg, "agentic": {"disk_clean": {"enabled": False}}}
    r = _rows(DU.usage(off))
    for sid in ("streams", "campaign-tmp", "sentinel"):
        assert "sweep disabled" in r[sid]["retention"]["text"], sid
    assert "sweep disabled" not in r["gatejobs"]["retention"]["text"]
    assert r["streams"]["retention"]["setting"] == "agentic.disk_clean.run_days"


def test_read_only(cfg):
    a = _fixture(cfg)

    def snap():
        return {str(p.relative_to(a)): (p.stat().st_mtime_ns, p.stat().st_size if p.is_file() else 0)
                for p in [a, *a.rglob("*")]}
    before = snap()
    time.sleep(0.01)
    DU.usage(cfg)
    CliRunner().invoke(cli, ["data", "usage"], obj=cfg)
    assert snap() == before
    assert not (a / ".disk-clean").exists()


def test_cli_json_and_table(cfg, monkeypatch):
    _fixture(cfg)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["data", "usage", "--json"])
    assert r.exit_code == 0, r.output
    doc = json.loads(r.output)
    assert list(doc) == ["schema", "root", "generated_at", "skipped", "total", "sources"]
    assert doc["generated_at"].endswith("Z")
    assert [s["id"] for s in doc["sources"]] == IDS
    keys = ["id", "path", "bytes", "files", "count", "count_unit", "oldest", "newest", "retention"]
    for s in doc["sources"]:
        assert [k for k in s if k != "by_state"] == keys
        assert isinstance(s["path"], list) and s["path"] and all(isinstance(p, str) for p in s["path"])
        assert list(s["retention"]) == ["rule", "setting", "text"]
        assert isinstance(s["count"], int) and isinstance(s["files"], int)
    rows = _rows(doc)
    assert rows["remote-jobs"]["path"] == ["remote-jobs/**", "job-queue/**", "job-uploads/**",
                                           "job-frames/**"]
    assert rows["streams"]["path"] == ["campaigns/*/runs/*/stream.jsonl"]
    assert "by_state" in rows["campaigns"]

    r = CliRunner().invoke(cli, ["data", "usage"])
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert lines[-1].startswith("total") and "KiB" in lines[-1]
    first = {i: next(n for n, ln in enumerate(lines) if ln.strip().startswith(i)) for i in IDS
             if rows[i]["files"]}
    order = sorted(first, key=first.get)
    sizes = [rows[i]["bytes"] for i in order]
    assert sizes == sorted(sizes, reverse=True)

    assert "usage" in CliRunner().invoke(cli, ["data", "--help"]).output
    assert "retention" in CliRunner().invoke(cli, ["data", "usage", "--help"]).output
    root_help = CliRunner().invoke(cli, ["--help"]).output
    assert any(ln.split()[:1] == ["data"] for ln in root_help.splitlines())
    assert "nw data usage" in CliRunner().invoke(cli, ["queue", "disk-clean", "--help"]).output


def test_scale(cfg):
    a = store.agentic_dir(cfg)
    for c in range(200):
        d = a / "campaigns" / f"C-{c}"
        _w(d / "state.json", 0, json.dumps({"state": "MERGED"}))
        for r in range(5):
            _w(d / "runs" / f"{r:03d}-impl" / "stream.jsonl", 10)
            _w(d / "runs" / f"{r:03d}-impl" / "run.json", 2)
    t0 = time.monotonic()
    doc = DU.usage(cfg)
    assert time.monotonic() - t0 < 30
    r = _rows(doc)
    assert r["runs"]["count"] == 1000 and r["streams"]["files"] == 1000


def test_cli_missing_dir(cfg, monkeypatch):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["data", "usage", "--json"])
    assert r.exit_code == 0, r.output
    doc = json.loads(r.output)
    assert doc["total"] == {"bytes": 0, "files": 0}
    r = CliRunner().invoke(cli, ["data", "usage"])
    assert r.exit_code == 0, r.output
    assert any(ln.startswith("total") and "0 files" in ln for ln in r.output.splitlines())
    assert not store.agentic_dir(cfg).exists()


def test_wild_mtime_and_markup_do_not_crash(tmp_path, monkeypatch):
    cfg = {"data_dir": str(tmp_path / "[/x]data")}
    a = store.agentic_dir(cfg)
    _w(a / "runner.log", 5)
    real = DU._stat

    def wild(entry):                        # the filesystem would clamp a huge utime
        st = real(entry)
        if entry.name == "runner.log":
            return types.SimpleNamespace(st_size=st.st_size, st_mtime=4_000_000_000_000)
        return st

    monkeypatch.setattr(DU, "_stat", wild)
    _w(a / "campaigns" / "C-1" / "state.json", 0, json.dumps({"state": "[/oops]"}))
    doc = DU.usage(cfg)
    assert _rows(doc)["runner-logs"]["newest"] == "9999-12-31T23:59:59Z"
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["data", "usage"])
    assert r.exit_code == 0, r.output
    assert "[/oops] 1" in r.output and "[/x]data" in r.output


def test_iso_clamps_out_of_range():
    assert DU._iso(4_000_000_000_000) == "9999-12-31T23:59:59Z"
    assert DU._iso(-1e13) == "0001-01-01T00:00:00Z"
    assert DU._iso(None) is None
