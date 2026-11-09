"""SPEC-0387: job-level infra errors (ENOSPC) on a worker. Offline; everything under tmp_path;
the registry and config load are monkeypatched, never the real box."""
import errno
import json
import shutil
from collections import namedtuple

import pytest
from click.testing import CliRunner

from nightwright.agentic import gates as G
from nightwright.agentic import remote_gates as RG
from nightwright.agentic import worker as W
from nightwright.agentic import worker_job as WJ
from nightwright.agentic import workers as WK
from nightwright.cli import cli
from test_worker import _gate, _header

pytestmark = pytest.mark.fast


def _enospc(path="/data/x.log"):
    return OSError(errno.ENOSPC, "No space left on device", path)


# a) the error frame ---------------------------------------------------------------------------

def test_run_job_reports_the_gate_stage_traceback_and_enospc_details(tmp_path, monkeypatch):
    cfg = {"data_dir": str(tmp_path / "data")}
    job = tmp_path / "job"
    for d in ("tmp", "out", "tree"):
        (job / d).mkdir(parents=True)
    (job / "tree" / "f").write_bytes(b"x" * 10)
    h = _header("j-1", "a" * 40, "b" * 40, [_gate("unit", "true")], bundle_bytes=0)
    (job / "header.json").write_text(json.dumps(h))
    (job / "job.json").write_text("{}")
    monkeypatch.setattr(W, "_checkout", lambda *a, **k: None)
    monkeypatch.setattr(W, "_start_gc", lambda cfg: None)
    monkeypatch.setattr(W, "_cleanup", lambda *a, **k: None)

    def boom(*a, **k):
        raise _enospc()
    monkeypatch.setattr(G, "run_gate_with_retries", boom)
    assert WJ.run_job(cfg, job, {}) != 0
    frames = [json.loads(ln) for ln in (job / "frames.jsonl").read_text().splitlines()]
    err = frames[-1]
    assert err["type"] == "error" and err["stage"] == "gate:unit"
    assert err["job_error"] is True and err["filename"] == "/data/x.log"
    assert "Traceback" in err["traceback"] and len(err["traceback"]) <= W.DETAIL_TB_MAX
    assert err["disk"]["free"] >= 0
    assert err["job_bytes"]["tree"] == 10 and err["job_bytes"]["tmp"] == 0


def test_error_detail_skips_disk_usage_for_other_errors(tmp_path):
    d = W.error_detail({"data_dir": str(tmp_path)}, ValueError("x"))
    assert d["job_error"] and "disk" not in d and "filename" not in d


# b) run_gate ----------------------------------------------------------------------------------

def test_run_gate_log_open_failure_is_an_infra_error_result(tmp_path, monkeypatch):
    real = open

    def fake_open(p, *a, **k):
        if str(p).endswith(".log"):
            raise _enospc(str(p))
        return real(p, *a, **k)
    monkeypatch.setattr(G, "open", fake_open, raising=False)
    r = G.run_gate(_gate("g", "true"), tmp_path, {}, tmp_path / "out" / "001-g.log", {})
    assert r.ok is False and "No space" in r.infra_error and r.exit_code is None


def test_run_gate_sidecar_failure_is_an_infra_error_result(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise _enospc()
    monkeypatch.setattr(G, "_write_sidecar", boom)
    r = G.run_gate(_gate("g", "true"), tmp_path, {}, tmp_path / "001-g.log", {})
    assert r.ok is False and "x.log" in r.infra_error


def test_infra_error_survives_the_frame_round_trip():
    assert "infra_error" in RG._GATE_FIELDS


# c) cooldown ----------------------------------------------------------------------------------

def test_job_error_frame_does_not_cool_the_worker_down_but_a_plain_one_does():
    assert RG._error_failure("gate:unit", {"error": "OSError: x", "job_error": True}).cooldown is False
    f = RG._error_failure("gate:unit", {"error": "OSError: x", "job_error": True,
                                        "traceback": "TB"})
    assert f.job_error and "TB" in f.error
    assert RG._error_failure("checkout", {"error": "boom"}).cooldown is True
    assert RG._error_failure("claim", {"error": "dup"}).cooldown is False


def test_failed_job_error_is_given_up_for_a_retry_without_cooldown(monkeypatch):
    calls = []
    monkeypatch.setattr(RG, "_cancelled", lambda *a: None)
    monkeypatch.setattr(RG, "_record_failure", lambda *a: calls.append("record"))
    monkeypatch.setattr(RG, "set_cooldown", lambda *a: calls.append("cooldown"))
    monkeypatch.setattr(RG, "_give_up", lambda cfg, cid, w, rec, seq, reason:
                        calls.append(reason) or "lost")
    f = RG._error_failure("gate:unit", {"error": "e", "job_error": True})
    assert RG._failed({}, "c", {"name": "box"}, {"job_id": "j"}, 1, f) == "lost"
    assert calls == ["record", "job_error"]


# d) admission ---------------------------------------------------------------------------------

def test_admission_refuses_below_min_free_gb(tmp_path, monkeypatch):
    cfg = {"data_dir": str(tmp_path), "agentic": {"workers": {"min_free_gb": 5}}}
    du = namedtuple("du", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: du(100 * 2**30, 98 * 2**30, 2 * 2**30))
    assert "min_free_gb" in W.admission_refusal(cfg)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: du(100 * 2**30, 90 * 2**30, 10 * 2**30))
    assert W.admission_refusal(cfg) is None
    cfg["agentic"]["workers"]["min_free_gb"] = 20
    assert W.admission_refusal(cfg)
    assert W.min_free_gb({}) == 5.0 and W.min_free_gb(
        {"agentic": {"workers": {"min_free_gb": "x"}}}) == 5.0


# e) clear-cooldown ----------------------------------------------------------------------------

def test_clear_cooldown_only_clears_the_cooldown(tmp_path, monkeypatch):
    cfg = {"data_dir": str(tmp_path / "data"), "agentic": {}}
    p = WK.path(cfg)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"schema": WK.SCHEMA, "workers": [
        {"name": "box", "ssh": "nw@box", "slots": 1, "nw": "nw", "enabled": False,
         "draining": True, "cooldown_until": "2999-01-01T00:00:00+00:00"}]}))
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    assert str(tmp_path) in str(p)
    r = CliRunner().invoke(cli, ["workers", "clear-cooldown", "box"])
    assert r.exit_code == 0, r.output
    w = json.loads(p.read_text())["workers"][0]
    assert w["cooldown_until"] is None and w["enabled"] is False and w["draining"] is True
    assert CliRunner().invoke(cli, ["workers", "clear-cooldown", "nope"]).exit_code != 0

