"""SPEC-0534: reconcile draining workers' confirmed-gone remote jobs."""
import datetime as dt
import json
import subprocess
import threading
import time
from contextlib import ExitStack
from unittest import mock

import pytest
from click.testing import CliRunner

from nightwright.agentic import gate_slots as GS
from nightwright.agentic import queue as Q
from nightwright.agentic import remote_gates as RG
from nightwright.agentic import remote_jobs as RJ
from nightwright.agentic import workers as WK
from nightwright.agentic import workers_update as WU
from nightwright.cli import cli

from test_remote_gates import _short_runtime_dir  # noqa: F401  (autouse fixture)
from test_workers_update_cmd import world as update_world  # noqa: F401

pytestmark = pytest.mark.fast


@pytest.fixture
def cfg(tmp_path):
    return {"data_dir": str(tmp_path / "data"), "agentic": {"gates": {"slots": 1}}}


def _seed(cfg, *records):
    p = WK.path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema": WK.SCHEMA, "workers": list(records)}))


def _w(name="box", **kw):
    return {"name": name, "ssh": f"me@{name}", "slots": 1, "kinds": ["gate"], "nw": "nw",
            "ssh_argv": ["ssh-stub"], "enabled": True, "draining": True, **kw}


def _job(cfg, job_id=None, worker="box", state="attached", campaign="c-1", **fields):
    job_id = job_id or RJ.gate_job_id(campaign, 1)
    RJ.create(cfg, job_id=job_id, campaign=campaign, worker=worker, attempt=1, seq=1,
              submitted_at=0.0, deadline_at=1e10, tier="loop")
    patch = {"state": state, **fields} if state != "submitted" or fields else fields
    if patch:
        RJ.update(cfg, job_id, **patch)
    return job_id


def _journal(cfg, kind=None):
    rows = Q.read_journal(cfg)
    return [r for r in rows if kind is None or r.get("kind") == kind]


class _Probe:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, argv, *, timeout, input=None):
        self.calls.append((list(argv), timeout))
        if not self.script:
            raise AssertionError(f"unexpected probe {argv!r}")
        step = self.script.pop(0)
        if step == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        if step == "oserror":
            raise OSError("ssh broke")
        rc, stdout = step
        out = stdout if isinstance(stdout, bytes) else stdout.encode()
        return subprocess.CompletedProcess(argv, rc, out, b"")


def _schedule_update(cfg, name="box"):
    since = dt.datetime.now().astimezone().isoformat(timespec="seconds")

    def fn(records):
        for r in records:
            if r.get("name") == name:
                WK.mark_update(r, since)
    WK.update(cfg, fn)


def test_confirmed_gone_marks_lost_once(cfg, monkeypatch):
    _seed(cfg, _w())
    old = (dt.datetime.now().astimezone() - dt.timedelta(hours=7)).isoformat(timespec="seconds")
    jid = _job(cfg, state="attached", accepted_at=old, last_seen_at=old)
    _schedule_update(cfg)
    probe = _Probe([(0, b"[]"), (1, b'{"type":"status","state":"unknown"}\n')])
    monkeypatch.setattr(WK, "_reconcile_run", probe)
    idle, late = WK.due_updates(cfg)
    assert len(idle) == 1
    rec = RJ.read(cfg, jid)
    assert rec["state"] == "lost" and rec.get("lost_reason") == "reconciled"
    lines = _journal(cfg, "worker_job_reconciled")
    assert len(lines) == 1
    assert lines[0]["via"] == "poll" and lines[0]["reported"] == "absent"
    probe2 = _Probe([(0, b"[]"), (1, b'{"type":"status","state":"unknown"}\n')])
    monkeypatch.setattr(WK, "_reconcile_run", probe2)
    WK.due_updates(cfg)
    assert len(_journal(cfg, "worker_job_reconciled")) == 1
    assert RJ.read(cfg, jid)["state"] == "lost"


def test_reread_leaves_final_record(cfg, monkeypatch, tmp_path):
    for final in ("done", "local", "lost", "cancelled"):
        c = {"data_dir": str(tmp_path / final / "data"), "agentic": {"gates": {"slots": 1}}}
        _seed(c, _w())
        jid = _job(c, state="attached", accepted_at="t")

        def run(argv, *, timeout, input=None, _final=final, _c=c):
            if "jobs" in argv:
                return subprocess.CompletedProcess(argv, 0, b"[]", b"")
            RJ.update(_c, jid, state=_final)
            return subprocess.CompletedProcess(
                argv, 1, b'{"type":"status","state":"unknown"}\n', b"")

        monkeypatch.setattr(WK, "_reconcile_run", run)
        WK.reconcile_draining(c, ["box"])
        rec = RJ.read(c, jid)
        assert rec["state"] == final
        if final == "cancelled":
            assert "lost_reason" not in rec
        assert not _journal(c, "worker_job_reconciled")


def test_unaccepted_submit_stays_submitted(cfg, monkeypatch):
    _seed(cfg, _w())
    jid = _job(cfg, state="submitted")
    probe = _Probe([(0, b"[]"), (1, b'{"type":"status","state":"unknown"}\n')])
    monkeypatch.setattr(WK, "_reconcile_run", probe)
    WK.reconcile_draining(cfg, ["box"])
    assert RJ.read(cfg, jid)["state"] == "submitted"
    assert probe.calls == []
    jid2 = _job(cfg, job_id="c-1-gate-002-a1", state="attached", accepted_at="t")
    monkeypatch.setattr(WK, "_reconcile_run", _Probe([
        (0, b"[]"), (1, b'{"type":"status","state":"unknown"}\n')]))
    WK.reconcile_draining(cfg, ["box"])
    assert RJ.read(cfg, jid2)["state"] == "lost"


def test_two_ssh_workers_both_probed(cfg, monkeypatch):
    _seed(cfg, _w("one"), _w("two", ssh_argv=["ssh-2"]))
    j1 = _job(cfg, job_id="c-1-gate-001-a1", worker="one", state="attached", accepted_at="t")
    j2 = _job(cfg, job_id="c-1-gate-002-a1", worker="two", state="attached", accepted_at="t")
    clock = {"t": 0.0}

    def mono():
        return clock["t"]

    monkeypatch.setattr(time, "monotonic", mono)
    calls = []

    def run(argv, *, timeout, input=None):
        calls.append((list(argv), timeout))
        worker = "one" if argv[0] == "ssh-stub" else "two"
        if worker == "one":
            assert timeout <= 15
            if "jobs" in argv:
                clock["t"] = 15.0
            return subprocess.CompletedProcess(
                argv, 0,
                b"[]" if "jobs" in argv else b'{"type":"status","state":"unknown"}\n',
                b"")
        assert timeout <= 15
        body = json.dumps([{"job_id": j2, "state": "running"}]).encode()
        return subprocess.CompletedProcess(argv, 0, body, b"")

    monkeypatch.setattr(WK, "_reconcile_run", run)
    WK.reconcile_draining(cfg, ["one", "two"])
    assert len(calls) == 3
    assert calls[0][0][0] == "ssh-stub" and calls[2][0][0] == "ssh-2"
    assert RJ.read(cfg, j1)["state"] == "lost"
    assert RJ.read(cfg, j2)["state"] == "attached"
    assert sum(1 for argv, _ in calls if any(a == "poll" for a in argv)) == 1


def test_poll_exit_1_unknown_marks_lost(cfg, monkeypatch):
    _seed(cfg, _w())
    jid = _job(cfg, state="attached", accepted_at="t")
    for rc, body in ((1, b'{"type":"status","state":"unknown"}\n'),
                     (0, b'{"type":"status","state":"lost"}\n')):
        _seed(cfg, _w())
        jid = _job(cfg, state="attached", accepted_at="t")
        monkeypatch.setattr(WK, "_reconcile_run", _Probe([(0, b"[]"), (rc, body)]))
        WK.reconcile_draining(cfg, ["box"])
        assert RJ.read(cfg, jid)["state"] == "lost"
    old = (dt.datetime.now().astimezone() - dt.timedelta(hours=8)).isoformat()
    for step in ("timeout", (2, b""), (0, b"not json\n")):
        _seed(cfg, _w())
        jid = _job(cfg, state="attached", accepted_at=old, last_seen_at=old)
        if step == "timeout":
            monkeypatch.setattr(WK, "_reconcile_run", _Probe([(0, b"[]"), "timeout"]))
        else:
            monkeypatch.setattr(WK, "_reconcile_run", _Probe([(0, b"[]"), step]))
        WK.reconcile_draining(cfg, ["box"])
        assert RJ.read(cfg, jid)["state"] == "attached"


def test_jobs_running_and_done_paths(cfg, monkeypatch, tmp_path):
    for st in ("accepted", "running"):
        c = {"data_dir": str(tmp_path / st / "data"), "agentic": {"gates": {"slots": 1}}}
        _seed(c, _w())
        jid = _job(c, state="attached", accepted_at="t")
        body = json.dumps([{"job_id": jid, "state": st}]).encode()
        probe = _Probe([(0, body)])
        monkeypatch.setattr(WK, "_reconcile_run", probe)
        not_busy = WK.reconcile_draining(c, ["box"])
        assert RJ.read(c, jid)["state"] == "attached"
        assert not_busy == set()
        assert len(probe.calls) == 1
        assert not _journal(c, "worker_job_reconciled")
        assert not WK._drained(c, "box", not_busy=not_busy)
    _seed(cfg, _w())
    jid = _job(cfg, state="attached", accepted_at="t")
    body = json.dumps([{"job_id": jid, "state": "done"}]).encode()
    monkeypatch.setattr(WK, "_reconcile_run", _Probe([(0, body)]))
    not_busy = WK.reconcile_draining(cfg, ["box"])
    assert RJ.read(cfg, jid)["state"] == "attached"
    assert jid in not_busy
    assert WK._drained(cfg, "box", not_busy=not_busy)


def test_finish_drains_recheck_uses_not_busy(cfg, monkeypatch):
    _seed(cfg, _w(update_pending=None))
    jid = _job(cfg, state="attached", accepted_at="t")
    body = json.dumps([{"job_id": jid, "state": "error"}]).encode()
    monkeypatch.setattr(WK, "_reconcile_run", _Probe([(0, body)]))
    assert WK.finish_drains(cfg) == ["box"]


def test_held_slot_still_blocks(cfg, monkeypatch):
    _seed(cfg, _w())
    jid = _job(cfg, state="attached", accepted_at="t")
    body = json.dumps([{"job_id": jid, "state": "done"}]).encode()
    monkeypatch.setattr(WK, "_reconcile_run", _Probe([(0, body)]))
    with ExitStack() as hold:
        hold.enter_context(GS.acquire(cfg, "c-1", pools=[("box", 1)]))
        not_busy = WK.reconcile_draining(cfg, ["box"])
        assert jid in not_busy
        assert not WK._drained(cfg, "box", not_busy=not_busy)


def test_poll_wins_over_empty_jobs(cfg, monkeypatch):
    _seed(cfg, _w())
    jid = _job(cfg, state="attached", accepted_at="t")
    for poll_st in ("accepted", "running"):
        monkeypatch.setattr(WK, "_reconcile_run", _Probe([
            (0, b"[]"), (0, json.dumps({"type": "status", "state": poll_st}).encode() + b"\n")]))
        WK.reconcile_draining(cfg, ["box"])
        assert RJ.read(cfg, jid)["state"] == "attached"
    monkeypatch.setattr(WK, "_reconcile_run", _Probe([
        (0, b"[]"), (0, b'{"type":"status","state":"done"}\n')]))
    not_busy = WK.reconcile_draining(cfg, ["box"])
    assert jid in not_busy


def test_bad_jobs_and_skipped_poll(cfg, monkeypatch, tmp_path):
    for step in ((1, b"[]"), "timeout", (0, b"{}"), (0, b"not-json")):
        c = {"data_dir": str(tmp_path / str(step) / "data"), "agentic": {"gates": {"slots": 1}}}
        _seed(c, _w())
        jid = _job(c, state="attached", accepted_at="t")
        if step == "timeout":
            probe = _Probe(["timeout"])
        else:
            probe = _Probe([step])
        monkeypatch.setattr(WK, "_reconcile_run", probe)
        WK.reconcile_draining(c, ["box"])
        assert RJ.read(c, jid)["state"] == "attached"
        assert not _journal(c, "worker_job_reconciled")
    c = {"data_dir": str(tmp_path / "skip-poll" / "data"), "agentic": {"gates": {"slots": 1}}}
    _seed(c, _w())
    jid = _job(c, state="attached", accepted_at="t")
    clock = {"t": 0.0}

    def mono():
        return clock["t"]

    def run(argv, *, timeout, input=None):
        if "jobs" in argv:
            clock["t"] = 19.5
        return subprocess.CompletedProcess(argv, 0, b"[]", b"")

    monkeypatch.setattr(time, "monotonic", mono)
    monkeypatch.setattr(WK, "_reconcile_run", run)
    WK.reconcile_draining(c, ["box"])
    assert RJ.read(c, jid)["state"] == "attached"


def test_attached_campaign_and_pull_skipped(cfg, monkeypatch, tmp_path):
    tok = "a" * 64
    _seed(cfg, _w(), _w("pull", mode="pull", token_sha256=tok, ssh="", nw=""))
    jid = _job(cfg, state="attached", accepted_at="t")
    ev = threading.Event()
    RG._ATTACHED["c-1"] = ev
    try:
        probe = _Probe([])
        monkeypatch.setattr(WK, "_reconcile_run", probe)
        WK.reconcile_draining(cfg, ["box", "pull"])
        assert probe.calls == []
        assert not WK._drained(cfg, "box")
        _job(cfg, job_id="c-2-gate-001-a1", worker="pull", campaign="c-2", state="attached",
             accepted_at="t")
        WK.reconcile_draining(cfg, ["pull"])
        assert probe.calls == []
    finally:
        RG._ATTACHED.pop("c-1", None)
    cp = {"data_dir": str(tmp_path / "persona" / "data"), "agentic": {"gates": {"slots": 1}}}
    _seed(cp, _w())
    jid = _job(cp, job_id="c-3-persona-001-x-a1", campaign="c-3", state="attached",
              accepted_at="t")
    monkeypatch.setattr(WK, "_reconcile_run", _Probe([
        (0, b"[]"), (1, b'{"type":"status","state":"unknown"}\n')]))
    WK.reconcile_draining(cp, ["box"])
    assert RJ.read(cp, jid)["state"] == "lost"


def test_unselected_worker_unchanged(cfg, monkeypatch):
    _seed(cfg, _w("box"), _w("other", draining=False))
    jid = _job(cfg, worker="other", state="attached", accepted_at="t")
    monkeypatch.setattr(WK, "_reconcile_run", _Probe([(0, b"[]")]))
    WK.reconcile_draining(cfg, ["box"])
    assert RJ.read(cfg, jid)["state"] == "attached"


def test_no_give_up_or_side_effects(cfg, monkeypatch):
    _seed(cfg, _w())
    _job(cfg, state="attached", accepted_at="t")
    monkeypatch.setattr(WK, "_reconcile_run", _Probe([
        (0, b"[]"), (1, b'{"type":"status","state":"unknown"}\n')]))
    give_up = mock.Mock(side_effect=AssertionError("give_up"))
    monkeypatch.setattr(RG, "_give_up", give_up)
    WK.reconcile_draining(cfg, ["box"])
    give_up.assert_not_called()


def test_running_past_timeout_still_times_out(cfg, monkeypatch):
    cfg["agentic"]["workers"] = {"update_drain_timeout_h": 1}
    _seed(cfg, _w())
    _schedule_update(cfg)
    jid = _job(cfg, state="attached", accepted_at="t")
    body = json.dumps([{"job_id": jid, "state": "running"}]).encode()
    monkeypatch.setattr(WK, "_reconcile_run", _Probe([(0, body)]))
    old = (dt.datetime.now().astimezone() - dt.timedelta(hours=2)).isoformat()

    def fn(records):
        records[0]["update_pending"]["since"] = old
    WK.update(cfg, fn)
    idle, late = WK.due_updates(cfg)
    assert idle == [] and len(late) == 1


def test_finish_drains_after_gone_not_while_running(cfg, monkeypatch):
    _seed(cfg, _w(update_pending=None))
    jid = _job(cfg, state="attached", accepted_at="t")
    monkeypatch.setattr(WK, "_reconcile_run", _Probe([
        (0, b"[]"), (1, b'{"type":"status","state":"unknown"}\n')]))
    assert WK.finish_drains(cfg) == ["box"]
    _seed(cfg, _w(update_pending=None))
    jid = _job(cfg, state="attached", accepted_at="t")
    body = json.dumps([{"job_id": jid, "state": "running"}]).encode()
    monkeypatch.setattr(WK, "_reconcile_run", _Probe([(0, body)]))
    assert WK.finish_drains(cfg) == []


def test_unreadable_registry_raises(cfg, monkeypatch):
    _seed(cfg, _w())
    _job(cfg, state="attached", accepted_at="t")
    bad = RJ.jobs_dir(cfg) / "c-1-gate-099-a1.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        WK.finish_drains(cfg)
    probe = _Probe(["oserror"])
    monkeypatch.setattr(WK, "_reconcile_run", probe)
    assert WK.reconcile_draining(cfg, ["box"]) == set()


def test_reported_jobs_state_when_list_agrees(cfg, monkeypatch, tmp_path):
    for st in ("lost", "unknown"):
        c = {"data_dir": str(tmp_path / st / "data"), "agentic": {"gates": {"slots": 1}}}
        _seed(c, _w())
        jid = _job(c, state="attached", accepted_at="t")
        body = json.dumps([{"job_id": jid, "state": st}]).encode()
        poll = b'{"type":"status","state":"unknown"}\n' if st == "unknown" else \
            b'{"type":"status","state":"lost"}\n'
        monkeypatch.setattr(WK, "_reconcile_run", _Probe([(0, body), (1, poll)]))
        WK.reconcile_draining(c, ["box"])
        line = _journal(c, "worker_job_reconciled")[-1]
        assert line["reported"] == st


def test_due_updates_idle_starts_update_force_false(update_world, monkeypatch):
    w = update_world
    _schedule_update(w.cfg)
    old = (dt.datetime.now().astimezone() - dt.timedelta(hours=7)).isoformat(timespec="seconds")
    jid = _job(w.cfg, state="attached", accepted_at=old, last_seen_at=old)
    monkeypatch.setattr(WK, "_reconcile_run", _Probe([
        (0, b"[]"), (1, b'{"type":"status","state":"unknown"}\n')]))
    perf = mock.Mock(return_value=WU.Outcome("updated", "", build_id="b"))
    monkeypatch.setattr(WU, "perform", perf)
    idle, _ = WK.due_updates(w.cfg)
    assert len(idle) == 1
    Q._run_worker_updates(w.cfg)
    assert perf.call_count == 1
    assert RJ.read(w.cfg, jid)["state"] == "lost"
    assert not _journal(w.cfg, "worker_update_timeout")


def test_cli_help_mentions_reconcile(monkeypatch, cfg):
    _seed(cfg, _w(draining=False))
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["workers", "update", "--help"])
    assert r.exit_code == 0
    assert "reconcil" in r.output.lower() and "poll" in r.output.lower()
    assert "stale" not in r.output.lower() or "stale-job" not in r.output.lower()


def test_workers_reference_row_mentions_reconcile():
    text = open("docs/product/reference/index.md", encoding="utf-8").read()
    row = [ln for ln in text.splitlines() if ln.startswith("| `workers`")][0]
    assert "reconcil" in row.lower() and "lost" in row
    assert "stale <worker>" not in row


def test_parse_jobs_and_poll_units():
    assert WK.parse_jobs_answer(0, b"[]") == []
    assert WK.parse_jobs_answer(1, b"[]") is None
    assert WK.parse_poll_answer(1, b'{"type":"status","state":"unknown"}\n') == "unknown"
    assert WK.parse_poll_answer(2, b'{"type":"status","state":"unknown"}\n') is None
