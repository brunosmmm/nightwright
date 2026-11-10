"""SPEC-0524: the disk sweep removes finished work's leftovers and nothing a live run needs."""
import json
import os
import time

import pytest

pytestmark = pytest.mark.fast

from nightwright.agentic import disk_clean as DC
from nightwright.agentic import store

pytestmark = pytest.mark.fast

DAY = 86400


@pytest.fixture
def cfg(tmp_path):
    return {"data_dir": str(tmp_path)}


def _campaign(cfg, cid, state):
    d = store.campaign_dir(cfg, cid)
    (d / "tmp" / "gate-1").mkdir(parents=True)
    (d / "tmp" / "gate-1" / "junk").write_text("x" * 100)
    run = d / "runs" / "001-implementer"
    run.mkdir(parents=True)
    for n in ("stream.jsonl", "run.json", "result.json", "prompt.md"):
        (run / n).write_text("y" * 50)
    (d / "state.json").write_text(json.dumps({"id": cid, "state": state}))
    return d


def _age(p, days):
    t = time.time() - days * DAY
    os.utime(p, (t, t))


def test_terminal_campaign_loses_tmp_and_old_stream_but_keeps_records(cfg):
    d = _campaign(cfg, "C-1", "MERGED")
    _age(d / "runs/001-implementer/stream.jsonl", 20)
    res = DC.sweep(cfg, force=True)
    assert not (d / "tmp").exists()
    assert not (d / "runs/001-implementer/stream.jsonl").exists()
    for n in ("run.json", "result.json", "prompt.md"):
        assert (d / "runs/001-implementer" / n).exists()
    assert (d / "state.json").exists()
    assert res["tmp_dirs"] == 1 and res["streams"] == 1 and res["bytes"] == 150


def test_recent_stream_and_live_campaign_are_untouched(cfg):
    done = _campaign(cfg, "C-2", "ABANDONED")
    live = _campaign(cfg, "C-3", "GATING")
    _age(live / "runs/001-implementer/stream.jsonl", 90)
    DC.sweep(cfg, force=True)
    assert (done / "runs/001-implementer/stream.jsonl").exists()      # younger than run_days
    assert (live / "runs/001-implementer/stream.jsonl").exists()
    assert (live / "tmp/gate-1/junk").exists()


def test_sentinel_logs_keep_newest_and_recent(cfg):
    logs = store.agentic_dir(cfg) / "sentinel" / "P.logs"
    logs.mkdir(parents=True)
    for i in range(1, DC.KEEP_NEWEST_LOGS + 11):
        f = logs / f"{i:03d}-pytest.log"
        f.write_text("z")
        _age(f, 60 - i * 0.1)
    recent = logs / "900-pytest.log"
    recent.write_text("z")
    DC.sweep(cfg, force=True)
    left = sorted(p.name for p in logs.iterdir())
    assert len(left) == DC.KEEP_NEWEST_LOGS
    assert "900-pytest.log" in left and "050-pytest.log" in left
    assert "001-pytest.log" not in left


def test_rate_limited_and_disableable(cfg):
    d = _campaign(cfg, "C-4", "MERGED")
    assert DC.sweep(cfg)["ran"] is True
    d2 = _campaign(cfg, "C-5", "MERGED")
    assert DC.sweep(cfg)["ran"] is False and (d2 / "tmp").exists()      # inside interval_s
    assert DC.sweep(cfg, now=time.time() + 2 * 3600)["ran"] is True and not (d2 / "tmp").exists()
    off = {"data_dir": cfg["data_dir"], "agentic": {"disk_clean": {"enabled": False}}}
    assert DC.sweep(off)["ran"] is False


def test_bad_settings_fall_back():
    s = DC.settings({"agentic": {"disk_clean": {"run_days": "x", "log_days": -1, "enabled": 3}}})
    assert s == DC.DEFAULTS


def test_pytest_keeps_only_failed_tmp_dirs():
    import tomllib
    from pathlib import Path
    ini = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    opts = ini["tool"]["pytest"]["ini_options"]
    assert opts["tmp_path_retention_policy"] == "failed"
    assert opts["tmp_path_retention_count"] == 1
