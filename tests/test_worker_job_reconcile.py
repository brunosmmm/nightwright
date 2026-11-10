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

from nightwright.agentic import events as EV
from nightwright.agentic import gate_slots as GS
from nightwright.agentic import queue as Q
from nightwright.agentic import remote_gates as RG
from nightwright.agentic import remote_jobs as RJ
from nightwright.agentic import store as S
from nightwright.agentic import workers as WK
from nightwright.agentic import workers_update as WU
from nightwright.cli import cli
from waits import until

pytestmark = pytest.mark.fast

POLL_FROM = str(1 << 30)


@pytest.fixture(autouse=True)
def _runtime_dir(tmp_path, monkeypatch):
    d = tmp_path / "xdg-runtime"
    d.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(d))


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


def _old_iso(hours=8):
    return (dt.datetime.now().astimezone() - dt.timedelta(hours=hours)).isoformat(
        timespec="seconds")


def _assert_still_occupies_worker(cfg, jid, not_busy):
    """No-answer leaves the record live and still blocking idleness (SPEC-0534)."""
    assert RJ.read(cfg, jid)["state"] in RJ.LIVE
    assert jid not in not_busy
    assert not WK._drained(cfg, "box", not_busy=not_busy)
    assert not any(r.get("via") == "age" for r in _journal(cfg))


def _assert_jobs_argv(argv):
    assert "jobs" in argv and "--json" in argv


def _assert_poll_argv(argv, jid):
    assert "poll" in argv
    assert jid in argv
    i = argv.index("--from")
    assert argv[i + 1] == POLL_FROM
    assert "--renew" not in argv


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
    assert lines[0]["worker"] == "box" and lines[0]["job_id"] == jid
    assert lines[0]["prior_state"] == "attached"
    _assert_jobs_argv(probe.calls[0][0])
    _assert_poll_argv(probe.calls[1][0], jid)
    probe2 = _Probe([(0, b"[]"), (1, b'{"type":"status","state":"unknown"}\n')])
    monkeypatch.setattr(WK, "_reconcile_run", probe2)
    WK.due_updates(cfg)
    assert len(_journal(cfg, "worker_job_reconciled")) == 1
    assert RJ.read(cfg, jid)["state"] == "lost"


def test_two_gone_records_on_one_worker_both_mark_lost(cfg, monkeypatch):
    _seed(cfg, _w())
    jid1 = _job(cfg, job_id="c-1-gate-001-a1", state="attached", accepted_at="t")
    jid2 = _job(cfg, job_id="c-1-gate-002-a1", state="attached", accepted_at="t")
    clock = {"t": 0.0}

    def mono():
        return clock["t"]

    def run(argv, *, timeout, input=None):
        if "jobs" in argv:
            clock["t"] = 15.0
            return subprocess.CompletedProcess(argv, 0, b"[]", b"")
        return subprocess.CompletedProcess(
            argv, 1, b'{"type":"status","state":"unknown"}\n', b"")

    poll_timeouts = []

    def capture_run(argv, *, timeout, input=None):
        if "poll" in argv:
            poll_timeouts.append(timeout)
        return run(argv, timeout=timeout, input=input)

    monkeypatch.setattr(time, "monotonic", mono)
    monkeypatch.setattr(WK, "_reconcile_run", capture_run)
    WK.reconcile_draining(cfg, ["box"])
    assert RJ.read(cfg, jid1)["state"] == "lost"
    assert RJ.read(cfg, jid2)["state"] == "lost"
    assert len(_journal(cfg, "worker_job_reconciled")) == 2
    assert len(poll_timeouts) == 2
    assert poll_timeouts[0] == pytest.approx(3.0)
    assert poll_timeouts[1] == pytest.approx(5.0)


def test_three_gone_records_oldest_polled_after_15s_jobs(cfg, monkeypatch):
    _seed(cfg, _w())
    jid1 = _job(cfg, job_id="c-1-gate-001-a1", state="attached", accepted_at="t")
    jid2 = _job(cfg, job_id="c-1-gate-002-a1", state="attached", accepted_at="t")
    jid3 = _job(cfg, job_id="c-1-gate-003-a1", state="attached", accepted_at="t")
    clock = {"t": 0.0}

    def mono():
        return clock["t"]

    poll_timeouts = []

    def run(argv, *, timeout, input=None):
        if "jobs" in argv:
            clock["t"] = 15.0
            return subprocess.CompletedProcess(argv, 0, b"[]", b"")
        if jid1 in argv:
            poll_timeouts.append(timeout)
            clock["t"] = 20.0
            return subprocess.CompletedProcess(
                argv, 1, b'{"type":"status","state":"unknown"}\n', b"")
        raise AssertionError(f"unexpected poll {argv!r}")

    monkeypatch.setattr(time, "monotonic", mono)
    monkeypatch.setattr(WK, "_reconcile_run", run)
    WK.reconcile_draining(cfg, ["box"])
    assert len(poll_timeouts) == 1
    assert poll_timeouts[0] == pytest.approx(3.0)
    assert RJ.read(cfg, jid1)["state"] == "lost"
    assert RJ.read(cfg, jid2)["state"] == "attached"
    assert RJ.read(cfg, jid3)["state"] == "attached"


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
        if final in ("cancelled", "lost"):
            assert "lost_reason" not in rec
        assert not _journal(c, "worker_job_reconciled")


def test_reread_live_record_prior_state_from_lock(cfg, monkeypatch):
    _seed(cfg, _w())
    jid = _job(cfg, state="attached", accepted_at="t")

    def run(argv, *, timeout, input=None):
        if "jobs" in argv:
            return subprocess.CompletedProcess(argv, 0, b"[]", b"")
        RJ.update(cfg, jid, state="detached")
        return subprocess.CompletedProcess(
            argv, 1, b'{"type":"status","state":"unknown"}\n', b"")

    monkeypatch.setattr(WK, "_reconcile_run", run)
    WK.reconcile_draining(cfg, ["box"])
    rec = RJ.read(cfg, jid)
    assert rec["state"] == "lost" and rec.get("lost_reason") == "reconciled"
    lines = _journal(cfg, "worker_job_reconciled")
    assert len(lines) == 1
    assert lines[0]["prior_state"] == "detached"
    assert lines[0]["via"] == "poll"


def test_unaccepted_submit_stays_submitted(cfg, monkeypatch):
    _seed(cfg, _w())
    jid = _job(cfg, state="submitted")
    probe = _Probe([])
    monkeypatch.setattr(WK, "_reconcile_run", probe)
    WK.reconcile_draining(cfg, ["box"])
    assert RJ.read(cfg, jid)["state"] == "submitted"
    assert probe.calls == []
    jid2 = _job(cfg, job_id="c-1-gate-002-a1", state="attached", accepted_at="t")
    monkeypatch.setattr(WK, "_reconcile_run", _Probe([
        (0, b"[]"), (1, b'{"type":"status","state":"unknown"}\n')]))
    WK.reconcile_draining(cfg, ["box"])
    assert RJ.read(cfg, jid)["state"] == "submitted"
    assert RJ.read(cfg, jid2)["state"] == "lost"
    assert not any(r.get("job_id") == jid for r in _journal(cfg, "worker_job_reconciled"))


def test_unaccepted_cursor_nonzero_is_probed(cfg, monkeypatch):
    _seed(cfg, _w())
    jid = _job(cfg, state="submitted")
    RJ.update(cfg, jid, cursor=1)
    probe = _Probe([(0, b"[]"), (1, b'{"type":"status","state":"unknown"}\n')])
    monkeypatch.setattr(WK, "_reconcile_run", probe)
    WK.reconcile_draining(cfg, ["box"])
    assert RJ.read(cfg, jid)["state"] == "lost"
    assert len(probe.calls) == 2


def test_bad_cursor_does_not_abort(cfg, monkeypatch):
    _seed(cfg, _w())
    jid = _job(cfg, state="attached", accepted_at="t")
    RJ.update(cfg, jid, accepted_at=None, cursor="nope")
    probe = _Probe([(0, b"[]"), (1, b'{"type":"status","state":"unknown"}\n')])
    monkeypatch.setattr(WK, "_reconcile_run", probe)
    WK.reconcile_draining(cfg, ["box"])
    assert RJ.read(cfg, jid)["state"] == "lost"


def test_two_ssh_workers_both_probed(cfg, monkeypatch):
    _seed(cfg, _w("one", update_pending=None), _w("two", ssh_argv=["ssh-2"], update_pending=None))
    j1 = _job(cfg, job_id="c-1-gate-001-a1", worker="one", state="attached", accepted_at="t")
    j2 = _job(cfg, job_id="c-1-gate-002-a1", worker="two", state="attached", accepted_at="t")
    clock = {"t": 0.0}

    def mono():
        return clock["t"]

    monkeypatch.setattr(time, "monotonic", mono)
    calls = []

    def run(argv, *, timeout, input=None):
        calls.append((list(argv), timeout))
        if argv[0] == "ssh-stub":
            if "jobs" in argv:
                left = 20.0 - clock["t"]
                assert timeout == min(15.0, left)
                clock["t"] = 15.0
                return subprocess.CompletedProcess(argv, 0, b"[]", b"")
            left = 20.0 - clock["t"]
            assert timeout <= left
            clock["t"] = 20.0
            return subprocess.CompletedProcess(
                argv, 1, b'{"type":"status","state":"unknown"}\n', b"")
        if "jobs" in argv:
            assert timeout == 15.0
            body = json.dumps([{"job_id": j2, "state": "running"}]).encode()
            return subprocess.CompletedProcess(argv, 0, body, b"")
        pytest.fail("unexpected poll for running job")

    monkeypatch.setattr(WK, "_reconcile_run", run)
    WK.finish_drains(cfg)
    assert clock["t"] == 20.0
    assert len(calls) == 3
    assert RJ.read(cfg, j1)["state"] == "lost"
    assert RJ.read(cfg, j2)["state"] == "attached"
    assert sum(1 for argv, _ in calls if "poll" in argv) == 1


def test_jobs_timeout_capped_to_budget_left(cfg, monkeypatch):
    _seed(cfg, _w())
    _job(cfg, state="attached", accepted_at="t")
    clock = {"t": 0.0}

    def mono():
        return clock["t"]

    def late_dir(c, w):
        clock["t"] = 11.0

    probe = _Probe([(0, b"[]"), (1, b'{"type":"status","state":"unknown"}\n')])

    def run(argv, *, timeout, input=None):
        if "jobs" in argv:
            assert timeout == 9.0
        return probe(argv, timeout=timeout, input=input)

    monkeypatch.setattr(time, "monotonic", mono)
    monkeypatch.setattr(WK, "ensure_record_ssh_dir", late_dir)
    monkeypatch.setattr(WK, "_reconcile_run", run)
    WK.reconcile_draining(cfg, ["box"])


def test_poll_exit_1_unknown_marks_lost(cfg, monkeypatch, tmp_path):
    assert WK.parse_poll_answer(0, b'{"type":"status","state":"unknown"}\n') == "unknown"
    for exit_rc, poll_line in ((1, "unknown"), (0, "unknown"), (1, "lost"), (0, "lost")):
        c = {"data_dir": str(tmp_path / f"ex{exit_rc}-{poll_line}" / "data"),
             "agentic": {"gates": {"slots": 1}}}
        _seed(c, _w())
        jid = _job(c, state="attached", accepted_at="t")
        poll = json.dumps({"type": "status", "state": poll_line}).encode() + b"\n"
        probe = _Probe([(0, b"[]"), (exit_rc, poll)])
        monkeypatch.setattr(WK, "_reconcile_run", probe)
        WK.reconcile_draining(c, ["box"])
        rec = RJ.read(c, jid)
        assert rec["state"] == "lost" and rec.get("lost_reason") == "reconciled"
        lines = _journal(c, "worker_job_reconciled")
        assert len(lines) == 1
        assert lines[0]["via"] == "poll" and lines[0]["reported"] == "absent"
    for age_old in (False, True):
        stamp = _old_iso() if age_old else "t"
        for step in ("timeout", (2, b""), (0, b"not json\n")):
            tag = f"no-answer-{'old' if age_old else 'young'}-{step!s}"
            c = {"data_dir": str(tmp_path / tag / "data"), "agentic": {"gates": {"slots": 1}}}
            _seed(c, _w())
            jid = _job(c, state="attached", accepted_at=stamp, last_seen_at=stamp)
            def run_no_answer(argv, *, timeout, input=None, _step=step):
                if "jobs" in argv:
                    return subprocess.CompletedProcess(argv, 0, b"[]", b"")
                if _step == "timeout":
                    raise subprocess.TimeoutExpired(argv, timeout)
                rc, stdout = _step
                out = stdout if isinstance(stdout, bytes) else stdout.encode()
                return subprocess.CompletedProcess(argv, rc, out, b"")

            monkeypatch.setattr(WK, "_reconcile_run", run_no_answer)
            not_busy = WK.reconcile_draining(c, ["box"])
            _assert_still_occupies_worker(c, jid, not_busy)
            assert not _journal(c, "worker_job_reconciled")
            _schedule_update(c)
            idle, late = WK.due_updates(c)
            assert idle == [] and late == []
            assert WK.finish_drains(c) == []


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
    probe = _Probe([(0, body)])
    monkeypatch.setattr(WK, "_reconcile_run", probe)
    not_busy = WK.reconcile_draining(cfg, ["box"])
    assert RJ.read(cfg, jid)["state"] == "attached"
    assert jid in not_busy
    assert WK._drained(cfg, "box", not_busy=not_busy)


def test_jobs_error_stays_live_finish_drains(cfg, monkeypatch):
    _seed(cfg, _w(update_pending=None))
    jid = _job(cfg, state="attached", accepted_at="t")
    body = json.dumps([{"job_id": jid, "state": "error"}]).encode()
    monkeypatch.setattr(WK, "_reconcile_run", _Probe([(0, body)]))
    assert WK.finish_drains(cfg) == ["box"]
    assert RJ.read(cfg, jid)["state"] == "attached"
    assert not _journal(cfg, "worker_job_reconciled")


def test_poll_done_and_error_stay_live(cfg, monkeypatch, tmp_path):
    for st in ("done", "error"):
        c = {"data_dir": str(tmp_path / st / "data"), "agentic": {"gates": {"slots": 1}}}
        _seed(c, _w(update_pending=None))
        jid = _job(c, state="attached", accepted_at="t")
        probe = _Probe([(0, b"[]"), (0, json.dumps({"type": "status", "state": st}).encode() + b"\n")])
        monkeypatch.setattr(WK, "_reconcile_run", probe)
        assert WK.finish_drains(c) == ["box"]
        assert RJ.read(c, jid)["state"] == "attached"
        assert not _journal(c, "worker_job_reconciled")


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
        for exit_rc in (0, 1):
            poll = json.dumps({"type": "status", "state": poll_st}).encode() + b"\n"
            probe = _Probe([(0, b"[]"), (exit_rc, poll)])
            monkeypatch.setattr(WK, "_reconcile_run", probe)
            not_busy = WK.reconcile_draining(cfg, ["box"])
            assert RJ.read(cfg, jid)["state"] == "attached"
            assert not_busy == set()
            assert not WK._drained(cfg, "box", not_busy=not_busy)
            _assert_poll_argv(probe.calls[1][0], jid)
            _schedule_update(cfg)
            probe2 = _Probe([(0, b"[]"), (exit_rc, poll)])
            monkeypatch.setattr(WK, "_reconcile_run", probe2)
            idle, _ = WK.due_updates(cfg)
            assert idle == []
    for poll_st in ("done", "error"):
        for exit_rc in (0, 1):
            poll = json.dumps({"type": "status", "state": poll_st}).encode() + b"\n"
            probe = _Probe([(0, b"[]"), (exit_rc, poll)])
            monkeypatch.setattr(WK, "_reconcile_run", probe)
            not_busy = WK.reconcile_draining(cfg, ["box"])
            assert RJ.read(cfg, jid)["state"] == "attached"
            assert jid in not_busy
            assert WK._drained(cfg, "box", not_busy=not_busy)
    probe = _Probe([(0, b"[]"), (0, b'{"type":"status","state":"done"}\n')])
    monkeypatch.setattr(WK, "_reconcile_run", probe)
    not_busy = WK.reconcile_draining(cfg, ["box"])
    assert jid in not_busy


def test_bad_jobs_and_skipped_poll(cfg, monkeypatch, tmp_path):
    for age_old in (False, True):
        stamp = _old_iso() if age_old else "t"
        for step in ((1, b"[]"), "timeout", (0, b"{}"), (0, b"not-json")):
            tag = f"{'old' if age_old else 'young'}-{step!s}"
            c = {"data_dir": str(tmp_path / tag / "data"), "agentic": {"gates": {"slots": 1}}}
            _seed(c, _w())
            jid = _job(c, state="attached", accepted_at=stamp, last_seen_at=stamp)
            def run_bad_jobs(argv, *, timeout, input=None, _step=step):
                if "jobs" not in argv:
                    raise AssertionError(argv)
                if _step == "timeout":
                    raise subprocess.TimeoutExpired(argv, timeout)
                rc, stdout = _step
                out = stdout if isinstance(stdout, bytes) else stdout.encode()
                return subprocess.CompletedProcess(argv, rc, out, b"")

            monkeypatch.setattr(WK, "_reconcile_run", run_bad_jobs)
            not_busy = WK.reconcile_draining(c, ["box"])
            _assert_still_occupies_worker(c, jid, not_busy)
            assert not _journal(c, "worker_job_reconciled")
            _schedule_update(c)
            assert WK.due_updates(c)[0] == []
    for at, expect_poll in ((19.5, False), (18.0, True)):
        for age_old in (False, True):
            stamp = _old_iso() if age_old else "t"
            tag = f"budget-{at}-{'old' if age_old else 'young'}"
            c = {"data_dir": str(tmp_path / tag / "data"),
                 "agentic": {"gates": {"slots": 1}}}
            _seed(c, _w())
            jid = _job(c, state="attached", accepted_at=stamp, last_seen_at=stamp)
            clock = {"t": 0.0}
            calls = []

            def mono():
                return clock["t"]

            def run(argv, *, timeout, input=None):
                calls.append((list(argv), timeout))
                if "jobs" in argv:
                    clock["t"] = at
                return subprocess.CompletedProcess(argv, 0, b"[]", b"")

            monkeypatch.setattr(time, "monotonic", mono)
            monkeypatch.setattr(WK, "_reconcile_run", run)
            not_busy = WK.reconcile_draining(c, ["box"])
            _assert_still_occupies_worker(c, jid, not_busy)
            poll_timeouts = [t for a, t in calls if "poll" in a]
            assert (len(poll_timeouts) > 0) == expect_poll
            if expect_poll:
                assert poll_timeouts[0] <= 2.0


def test_detached_record_jobs_poll_marks_lost(cfg, monkeypatch):
    _seed(cfg, _w())
    jid = _job(cfg, state="detached", accepted_at="t")
    probe = _Probe([(0, b"[]"), (1, b'{"type":"status","state":"unknown"}\n')])
    monkeypatch.setattr(WK, "_reconcile_run", probe)
    not_busy = WK.reconcile_draining(cfg, ["box"])
    rec = RJ.read(cfg, jid)
    assert rec["state"] == "lost" and rec.get("lost_reason") == "reconciled"
    assert not_busy == set()
    lines = _journal(cfg, "worker_job_reconciled")
    assert len(lines) == 1 and lines[0]["prior_state"] == "detached"


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
        ghost = _job(cfg, job_id="c-2-gate-002-a1", campaign="c-2", worker="box",
                     state="attached", accepted_at="t")
        probe2 = _Probe([(0, b"[]"), (1, b'{"type":"status","state":"unknown"}\n')])
        monkeypatch.setattr(WK, "_reconcile_run", probe2)
        WK.reconcile_draining(cfg, ["box"])
        assert RJ.read(cfg, jid)["state"] == "attached"
        assert RJ.read(cfg, ghost)["state"] == "lost"
        assert not WK._drained(cfg, "box")
        assert len(_journal(cfg, "worker_job_reconciled")) == 1
        pj = "c-2-gate-001-a1"
        _job(cfg, job_id=pj, worker="pull", campaign="c-2", state="attached", accepted_at="t")
        before = dict(RJ.read(cfg, pj))
        probe_pull = _Probe([])
        monkeypatch.setattr(WK, "_reconcile_run", probe_pull)
        WK.reconcile_draining(cfg, ["pull"])
        assert probe_pull.calls == []
        assert RJ.read(cfg, pj) == before
    finally:
        RG._ATTACHED.pop("c-1", None)
    cp = {"data_dir": str(tmp_path / "persona" / "data"), "agentic": {"gates": {"slots": 1}}}
    _seed(cp, _w())
    jid = _job(cp, job_id="c-3-persona-001-x-a1", campaign="c-3", state="attached",
              accepted_at="t")
    probe = _Probe([(0, b"[]"), (1, b'{"type":"status","state":"unknown"}\n')])
    monkeypatch.setattr(WK, "_reconcile_run", probe)
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
    gdir = S.gates_dir(cfg, "c-1")
    gdir.mkdir(parents=True)
    gate = gdir / "001-gate.json"
    gate.write_text("{}")
    cool_before = WK.read_strict(cfg)[0].get("cooldown_until")
    monkeypatch.setattr(WK, "_reconcile_run", _Probe([
        (0, b"[]"), (1, b'{"type":"status","state":"unknown"}\n')]))
    give_up = mock.Mock(side_effect=AssertionError("give_up"))
    cool = mock.Mock(side_effect=AssertionError("cooldown"))
    monkeypatch.setattr(RG, "_give_up", give_up)
    monkeypatch.setattr(RG, "set_cooldown", cool)
    WK.reconcile_draining(cfg, ["box"])
    give_up.assert_not_called()
    cool.assert_not_called()
    assert WK.read_strict(cfg)[0].get("cooldown_until") == cool_before
    assert S.read_ledger(cfg, "c-1", types=["worker_job_lost"]) == []
    assert not EV.read_since(cfg, 0, types=["worker_job_lost"])
    assert gate.is_file()


def test_running_past_timeout_still_times_out(cfg, monkeypatch):
    cfg["agentic"]["workers"] = {"update_drain_timeout_h": 1}
    _seed(cfg, _w())
    _schedule_update(cfg)
    jid = _job(cfg, state="attached", accepted_at="t")
    body = json.dumps([{"job_id": jid, "state": "running"}]).encode()

    def run_running(argv, *, timeout, input=None):
        if "jobs" in argv:
            return subprocess.CompletedProcess(argv, 0, body, b"")
        raise AssertionError(argv)

    monkeypatch.setattr(WK, "_reconcile_run", run_running)
    old = (dt.datetime.now().astimezone() - dt.timedelta(hours=2)).isoformat()

    def fn(records):
        records[0]["update_pending"]["since"] = old
    WK.update(cfg, fn)
    idle, late = WK.due_updates(cfg)
    assert idle == [] and len(late) == 1
    perf = mock.Mock(side_effect=AssertionError("perform must not run"))
    monkeypatch.setattr(WU, "perform", perf)
    Q._UPDATING.clear()
    Q._run_worker_updates(cfg, progress=mock.Mock())
    assert perf.call_count == 0
    timeouts = _journal(cfg, "worker_update_timeout")
    assert len(timeouts) == 1 and timeouts[0]["worker"] == "box"
    wrec = next(r for r in WK.read_strict(cfg) if r["name"] == "box")
    assert wrec.get("update_last", {}).get("state") == "timeout"
    assert "draining" not in wrec and "update_pending" not in wrec


def test_finish_drains_after_gone_not_while_running(cfg, monkeypatch):
    _seed(cfg, _w(update_pending=None))
    jid = _job(cfg, state="attached", accepted_at="t")
    probe = _Probe([(0, b"[]"), (1, b'{"type":"status","state":"unknown"}\n')])
    monkeypatch.setattr(WK, "_reconcile_run", probe)
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
    wrec = next(r for r in WK.read_strict(cfg) if r["name"] == "box")
    assert wrec.get("draining") is True and wrec.get("enabled") is not False
    jid = RJ.gate_job_id("c-1", 1)
    probe = _Probe(["oserror"])
    monkeypatch.setattr(WK, "_reconcile_run", probe)
    assert WK.reconcile_draining(cfg, ["box"]) == set()
    assert RJ.read(cfg, jid)["state"] == "attached"
    assert not _journal(cfg, "worker_job_reconciled")


def test_ensure_ssh_dir_failure_aborts_without_disable(cfg, monkeypatch):
    _seed(cfg, _w("one", update_pending=None), _w("two", ssh_argv=["ssh-2"], update_pending=None))
    _job(cfg, job_id="c-1-gate-001-a1", worker="one", state="attached", accepted_at="t")
    j2 = _job(cfg, job_id="c-1-gate-002-a1", worker="two", state="attached", accepted_at="t")
    calls = []

    def ensure(cfg_, w):
        if w["name"] == "one":
            raise OSError("ssh dir unusable")

    def run(argv, *, timeout, input=None):
        calls.append(list(argv))
        if "jobs" in argv:
            return subprocess.CompletedProcess(argv, 0, b"[]", b"")
        return subprocess.CompletedProcess(
            argv, 1, b'{"type":"status","state":"unknown"}\n', b"")

    monkeypatch.setattr(WK, "ensure_record_ssh_dir", ensure)
    monkeypatch.setattr(WK, "_reconcile_run", run)
    done = WK.finish_drains(cfg)
    assert "one" not in done
    rec1 = next(r for r in WK.read_strict(cfg) if r["name"] == "one")
    assert rec1.get("draining") is True and rec1.get("enabled") is not False
    assert RJ.read(cfg, "c-1-gate-001-a1")["state"] == "attached"
    assert RJ.read(cfg, j2)["state"] == "lost"
    assert any(j2 in c for c in calls)
    failed = _journal(cfg, "worker_reconcile_probe_failed")
    assert len(failed) == 1 and failed[0]["worker"] == "one"
    _seed(cfg, _w("one"), _w("two", ssh_argv=["ssh-2"]))
    old = (dt.datetime.now().astimezone() - dt.timedelta(hours=7)).isoformat(timespec="seconds")
    _schedule_update(cfg, "one")

    def stamp_old(records):
        for r in records:
            if r["name"] == "one":
                r["update_pending"]["since"] = old
    WK.update(cfg, stamp_old)
    _job(cfg, job_id="c-1-gate-003-a1", worker="one", state="attached", accepted_at=old,
         last_seen_at=old)
    idle, late = WK.due_updates(cfg)
    assert idle == [] and len(late) == 1 and late[0]["name"] == "one"


def test_reported_jobs_state_when_list_agrees(cfg, monkeypatch, tmp_path):
    for st in ("lost", "unknown"):
        c = {"data_dir": str(tmp_path / st / "data"), "agentic": {"gates": {"slots": 1}}}
        _seed(c, _w())
        jid = _job(c, state="attached", accepted_at="t")
        body = json.dumps([{"job_id": jid, "state": st}]).encode()
        poll = b'{"type":"status","state":"unknown"}\n'
        probe = _Probe([(0, body), (1, poll)])
        monkeypatch.setattr(WK, "_reconcile_run", probe)
        WK.reconcile_draining(c, ["box"])
        rec = RJ.read(c, jid)
        assert rec["state"] == "lost" and rec.get("lost_reason") == "reconciled"
        lines = _journal(c, "worker_job_reconciled")
        assert len(lines) == 1
        assert lines[0]["reported"] == st
        assert lines[0]["via"] == "poll"
        _assert_poll_argv(probe.calls[1][0], jid)
    for jobs_st, poll_st, exit_rc in (("lost", "unknown", 1), ("lost", "lost", 0),
                                        ("lost", "lost", 1), ("unknown", "lost", 0),
                                        ("unknown", "lost", 1)):
        tag = f"{jobs_st}-{poll_st}-ex{exit_rc}"
        c = {"data_dir": str(tmp_path / tag / "data"), "agentic": {"gates": {"slots": 1}}}
        _seed(c, _w())
        jid = _job(c, state="attached", accepted_at="t")
        body = json.dumps([{"job_id": jid, "state": jobs_st}]).encode()
        poll_line = json.dumps({"type": "status", "state": poll_st}).encode() + b"\n"
        probe = _Probe([(0, body), (exit_rc, poll_line)])
        monkeypatch.setattr(WK, "_reconcile_run", probe)
        WK.reconcile_draining(c, ["box"])
        rec = RJ.read(c, jid)
        assert rec["state"] == "lost" and rec.get("lost_reason") == "reconciled"
        lines = _journal(c, "worker_job_reconciled")
        assert len(lines) == 1
        assert lines[0]["reported"] == jobs_st
        assert lines[0]["via"] == "poll"
    c = {"data_dir": str(tmp_path / "disagree" / "data"), "agentic": {"gates": {"slots": 1}}}
    _seed(c, _w())
    jid = _job(c, state="attached", accepted_at="t")
    body = json.dumps([{"job_id": jid, "state": "lost"}]).encode()
    monkeypatch.setattr(WK, "_reconcile_run", _Probe([
        (0, body), (0, b'{"type":"status","state":"running"}\n')]))
    WK.reconcile_draining(c, ["box"])
    assert RJ.read(c, jid)["state"] == "attached"
    assert not _journal(c, "worker_job_reconciled")


def test_reconcile_stamps_progress(cfg, monkeypatch):
    _seed(cfg, _w("one"), _w("two", ssh_argv=["ssh-2"]))
    _job(cfg, job_id="c-1-gate-001-a1", worker="one", state="attached", accepted_at="t")
    _job(cfg, job_id="c-1-gate-002-a1", worker="two", state="attached", accepted_at="t")
    order = []

    def progress():
        order.append("p")

    probe = _Probe([(0, b"[]"), (1, b'{"type":"status","state":"unknown"}\n'),
                    (0, b"[]"), (1, b'{"type":"status","state":"unknown"}\n')])

    def run(argv, *, timeout, input=None):
        order.append("s")
        return probe(argv, timeout=timeout, input=input)

    monkeypatch.setattr(WK, "_reconcile_run", run)
    WK.reconcile_draining(cfg, ["one", "two"], progress=progress)
    assert order.count("p") >= 2
    assert "s" in order
    assert order.index("p") < order.index("s")


def test_tick_forwards_progress_to_reconcile(cfg, monkeypatch):
    _seed(cfg, _w(update_pending=None))
    _job(cfg, state="attached", accepted_at="t")
    captured = {}
    real = WK.reconcile_draining

    def wrap(cfg_, names, *, progress=None):
        captured["progress"] = progress
        return real(cfg_, names, progress=progress)

    monkeypatch.setattr(WK, "reconcile_draining", wrap)
    jid = RJ.gate_job_id("c-1", 1)
    body = json.dumps([{"job_id": jid, "state": "running"}]).encode()
    monkeypatch.setattr(WK, "_reconcile_run", _Probe([(0, body)]))
    tick_cb = mock.Mock()
    Q._finish_worker_drains(cfg, progress=tick_cb)
    assert captured.get("progress") is tick_cb


def test_tick_forwards_progress_to_due_updates(cfg, monkeypatch):
    _seed(cfg, _w())
    _schedule_update(cfg)
    _job(cfg, state="attached", accepted_at="t")
    captured = {}
    real = WK.due_updates

    def wrap(cfg_, *a, progress=None, **kw):
        captured["progress"] = progress
        return real(cfg_, *a, progress=progress, **kw)

    monkeypatch.setattr(WK, "due_updates", wrap)
    jid = RJ.gate_job_id("c-1", 1)
    body = json.dumps([{"job_id": jid, "state": "running"}]).encode()
    monkeypatch.setattr(WK, "_reconcile_run", _Probe([(0, body)]))
    tick_cb = mock.Mock()
    Q._run_worker_updates(cfg, progress=tick_cb)
    assert captured.get("progress") is tick_cb


def test_due_updates_idle_starts_update_force_false(cfg, monkeypatch, tmp_path):
    _seed(cfg, _w())
    _schedule_update(cfg)
    old = (dt.datetime.now().astimezone() - dt.timedelta(hours=7)).isoformat(timespec="seconds")
    jid = _job(cfg, state="attached", accepted_at=old, last_seen_at=old)
    monkeypatch.setattr(WK, "_reconcile_run", _Probe([
        (0, b"[]"), (1, b'{"type":"status","state":"unknown"}\n')]))
    send = mock.Mock(side_effect=AssertionError("send should not run"))
    perf = mock.Mock(return_value=WU.Outcome("updated", "", build_id="b"))
    monkeypatch.setattr(WU, "send", send)
    monkeypatch.setattr(WU, "perform", perf)
    idle, _ = WK.due_updates(cfg)
    assert len(idle) == 1
    Q._run_worker_updates(cfg, progress=mock.Mock())
    assert until(lambda: not Q._UPDATING)
    assert perf.call_count == 1
    assert RJ.read(cfg, jid)["state"] == "lost"
    assert not _journal(cfg, "worker_update_timeout")


def test_cli_help_mentions_reconcile(monkeypatch, cfg):
    _seed(cfg, _w(draining=False))
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["workers", "update", "--help"])
    text = r.output.lower()
    assert r.exit_code == 0
    for needle in ("reconcil", "poll", "lost", "non-renewing", "jobs"):
        assert needle in text, needle
    assert "stale-job" not in text
    assert "update_drain_grace" not in text
    assert "via: age" not in text


def test_workers_reference_row_mentions_reconcile():
    text = open("docs/product/reference/index.md", encoding="utf-8").read()
    row = [ln for ln in text.splitlines() if ln.startswith("| `workers`")][0]
    low = row.lower()
    assert "reconcil" in low
    assert "non-renewing" in low
    assert "marks jobs those agree are gone as `lost`" in row
    assert "stale <worker>" not in row
    assert "update_drain_grace" not in low
    assert "via: age" not in low


def test_mark_reconciled_lost_skips_on_missing_or_write_error(cfg, monkeypatch):
    _seed(cfg, _w())
    jid = _job(cfg, state="attached", accepted_at="t")

    def run(argv, *, timeout, input=None):
        if "jobs" in argv:
            return subprocess.CompletedProcess(argv, 0, b"[]", b"")
        path = RJ.path(cfg, jid)
        path.unlink()
        return subprocess.CompletedProcess(
            argv, 1, b'{"type":"status","state":"unknown"}\n', b"")

    monkeypatch.setattr(WK, "_reconcile_run", run)
    WK.reconcile_draining(cfg, ["box"])
    assert not _journal(cfg, "worker_job_reconciled")
    assert RJ.read(cfg, jid) is None
    _seed(cfg, _w())
    jid = _job(cfg, state="attached", accepted_at="t")
    probe = _Probe([(0, b"[]"), (1, b'{"type":"status","state":"unknown"}\n')])
    real_write = RJ._write

    def boom(c, rec):
        raise OSError("disk full")

    monkeypatch.setattr(WK, "_reconcile_run", probe)
    monkeypatch.setattr(RJ, "_write", boom)
    with pytest.raises(OSError):
        WK.reconcile_draining(cfg, ["box"])
    assert not _journal(cfg, "worker_job_reconciled")
    assert RJ.read(cfg, jid)["state"] == "attached"
    assert not WK._drained(cfg, "box")
    wrec = next(r for r in WK.read_strict(cfg) if r["name"] == "box")
    assert wrec.get("draining") is True and wrec.get("enabled") is not False
    monkeypatch.setattr(RJ, "_write", real_write)


def test_parse_jobs_and_poll_units():
    assert WK.parse_jobs_answer(0, b"[]") == []
    assert WK.parse_jobs_answer(1, b"[]") is None
    pretty = json.dumps([{"job_id": "j", "state": "running"}], indent=2).encode()
    assert WK.parse_jobs_answer(0, pretty) == [{"job_id": "j", "state": "running"}]
    assert WK.parse_poll_answer(0, b'{"type":"status","state":"unknown"}\n') == "unknown"
    assert WK.parse_poll_answer(0, b'{"type":"status","state":"lost"}\n') == "lost"
    assert WK.parse_poll_answer(1, b'{"type":"status","state":"unknown"}\n') == "unknown"
    assert WK.parse_poll_answer(2, b'{"type":"status","state":"unknown"}\n') is None
    buf = (b'garbage\n{"type":"status","state":"running"}\n'
           b'{"type":"status","state":"unknown"}\n')
    assert WK.parse_poll_answer(1, buf) == "unknown"


def test_one_worker_two_eligible_records(cfg, monkeypatch):
    _seed(cfg, _w())
    j1 = _job(cfg, job_id=RJ.gate_job_id("c-1", 1), state="attached", accepted_at="t")
    j2 = _job(cfg, job_id=RJ.gate_job_id("c-1", 2), state="attached", accepted_at="t")
    calls = []

    def run(argv, *, timeout, input=None):
        calls.append((list(argv), timeout))
        if "jobs" in argv:
            return subprocess.CompletedProcess(argv, 0, b"[]", b"")
        if j1 in argv:
            assert timeout == pytest.approx(15.0)
            return "timeout"
        if j2 in argv:
            return subprocess.CompletedProcess(
                argv, 1, b'{"type":"status","state":"unknown"}\n', b"")
        raise AssertionError(argv)

    def reconcile_run(argv, *, timeout, input=None):
        out = run(argv, timeout=timeout, input=input)
        if out == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        return out

    monkeypatch.setattr(WK, "_reconcile_run", reconcile_run)
    WK.reconcile_draining(cfg, ["box"])
    assert len([c for c in calls if "poll" in c[0]]) == 2
    assert RJ.read(cfg, j1)["state"] == "attached"
    assert RJ.read(cfg, j2)["state"] == "lost"


def test_running_sibling_ghost_lost_same_worker(cfg, monkeypatch):
    _seed(cfg, _w())
    j1 = _job(cfg, job_id=RJ.gate_job_id("c-1", 1), state="attached", accepted_at="t")
    j2 = _job(cfg, job_id=RJ.gate_job_id("c-1", 2), state="attached", accepted_at="t")
    body = json.dumps([{"job_id": j1, "state": "running"}]).encode()
    probe = _Probe([(0, body), (1, b'{"type":"status","state":"unknown"}\n')])
    monkeypatch.setattr(WK, "_reconcile_run", probe)
    not_busy = WK.reconcile_draining(cfg, ["box"])
    assert RJ.read(cfg, j1)["state"] == "attached"
    assert RJ.read(cfg, j2)["state"] == "lost"
    assert not_busy == set()
    assert not WK._drained(cfg, "box", not_busy=not_busy)
    assert sum(1 for a, _ in probe.calls if "poll" in a) == 1


@pytest.mark.parametrize("poll_st", ("submitted", "detached", "cancelled", "foo"))
def test_poll_unrecognized_state_stays_live(cfg, monkeypatch, tmp_path, poll_st):
    c = {"data_dir": str(tmp_path / poll_st / "data"), "agentic": {"gates": {"slots": 1}}}
    _seed(c, _w())
    jid = _job(c, state="attached", accepted_at="t")
    poll = json.dumps({"type": "status", "state": poll_st}).encode() + b"\n"
    probe = _Probe([(0, b"[]"), (1, poll)])
    monkeypatch.setattr(WK, "_reconcile_run", probe)
    not_busy = WK.reconcile_draining(c, ["box"])
    _assert_still_occupies_worker(c, jid, not_busy)
    assert not _journal(c, "worker_job_reconciled")


def test_live_record_missing_job_id_skips_reconcile_pass(cfg, monkeypatch):
    _seed(cfg, _w())
    _schedule_update(cfg)
    jid = _job(cfg, state="attached", accepted_at="t")
    path = RJ.path(cfg, jid)
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec.pop("job_id", None)
    path.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    probe = _Probe([(0, b"[]"), (1, b'{"type":"status","state":"unknown"}\n')])
    monkeypatch.setattr(WK, "_reconcile_run", probe)
    Q._run_worker_updates(cfg, progress=mock.Mock())
    assert probe.calls == []
    assert RJ.read(cfg, jid)["state"] == "attached"
