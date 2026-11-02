"""SPEC-0373: gate progress percent."""
import os

from nightwright.agentic import gate_progress as GP
from nightwright.agentic import store


def test_parse_percent_takes_the_last_marker():
    assert GP.parse_percent("..... [ 12%]\n....F. [ 42%]\n") == 42
    assert GP.parse_percent("....... [100%]") == 100
    assert GP.parse_percent("no markers here") is None and GP.parse_percent("") is None


def test_bar_rendering():
    assert GP.bar(50) == "▕████░░░░▏ 50%"
    assert GP.bar(42, estimated=True) == "▕███░░░░░▏ ~42%"
    assert GP.bar(0) == "▕░░░░░░░░▏ 0%" and GP.bar(100).startswith("▕████████▏")
    assert GP.label(None) == ""


def test_live_percent_then_estimate_fallback(tmp_path):
    cfg = {"data_dir": str(tmp_path)}
    cid = "SPEC-0001-a"
    gd = store.gates_dir(cfg, cid)
    gd.mkdir(parents=True)
    (gd / "001-tests.log").write_text("..... [ 30%]\n..... [ 61%]\n")
    ev = [{"type": "gate", "duration_s": 100.0}]
    p = GP.progress(cfg, cid, since=None, elapsed_s=10, events=ev)
    assert p == {"percent": 61, "estimated": False}
    # a remote gate has no live output: elapsed / previous duration, flagged as an estimate
    p = GP.progress(cfg, cid, since=None, elapsed_s=40, events=ev, remote=True)
    assert p == {"percent": 40, "estimated": True}
    assert GP.progress(cfg, cid, since=None, elapsed_s=40, events=[], remote=True) is None
    assert GP.estimate(500, 100) == 99
