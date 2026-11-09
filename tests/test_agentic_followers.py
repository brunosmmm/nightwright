"""SPEC-0198: `nw queue followers` and the stewards in `nw queue conduct-status`."""
import json
import os

from click.testing import CliRunner

from nightwright.agentic import events as E
from nightwright.agentic import mailbox as M
from nightwright.cli import cli
import pytest

pytestmark = pytest.mark.fast


def _cfg(tmp_path):
    return {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "config"),
            "agentic": {"lease_ttl_s": 120}, "outbox_targets": {"P": {"repo_path": str(tmp_path)}},
            "project_axis": "bucket", "specs_dir": str(tmp_path)}


def _setup(tmp_path):
    cfg = _cfg(tmp_path)
    for i in range(3):
        E.append(cfg, "message", by="t", data={"msg_id": str(i), "from": "x",
                                                "kind": "info", "text": "t"})
    for _ in range(2):
        M.send(cfg, to="steward:P", frm="conductor", text="hi")
    E.cursor_set(cfg, "conductor", 1)
    E.cursor_set(cfg, "mailbox:steward:P", 2)
    E.cursor_set(cfg, "mailbox:human", 0)
    return cfg


def test_followers_rows_and_lag(tmp_path):
    cfg = _setup(tmp_path)
    with E.reader_lock(cfg, "conductor"), E.reader_lock(cfg, "mailbox:steward:P"):
        rows = {r["reader"]: r for r in E.followers(cfg)}
        c, s, h = rows["conductor"], rows["mailbox:steward:P"], rows["mailbox:human"]
        assert (c["kind"], c["held"], c["pid"], c["after"]) == ("events", True, os.getpid(), 1)
        assert c["lag"] == len([e for e in E.read_since(cfg, 1)]) and c["lag"] >= 2
        assert (s["kind"], s["held"], s["pid"], s["after"], s["lag"]) == \
            ("mailbox", True, os.getpid(), 2, 0)
        assert h["pid"] is None and h["held"] is False and h["lag"] == 0
        assert s["cursor_ts"] and s["cursor_age_s"] is not None
        assert [r["kind"] for r in E.followers(cfg)] == ["events", "mailbox", "mailbox"]
    # lock released -> not held
    assert not any(r["held"] for r in E.followers(cfg))
    E.cursor_set(cfg, "mailbox:steward:P", 0)
    assert {r["reader"]: r for r in E.followers(cfg)}["mailbox:steward:P"]["lag"] == 2


def test_followers_lag_from_last_id_reads_no_log(tmp_path, monkeypatch):
    """SPEC-0401: the dashboard's lag is `last_id - after`, without a `read_since`."""
    cfg = _setup(tmp_path)
    want = {r["reader"]: r["lag"] for r in E.followers(cfg)}
    last = max(e["id"] for e in E.read_since(cfg, 0))

    def no_read(*a, **k):
        raise AssertionError("read_since")
    monkeypatch.setattr(E, "read_since", no_read)
    got = {r["reader"]: r["lag"] for r in E.followers(cfg, last_id=last, lag_from_last_id=True)}
    assert got == want and got["conductor"] == last - 1
    rows = E.followers(cfg, last_id=None, lag_from_last_id=True)
    assert {r["reader"]: r["lag"] for r in rows}["conductor"] is None


def test_reader_lock_retries_a_probe_and_still_refuses_a_holder(tmp_path, monkeypatch):
    """SPEC-0401: a lock busy only for a status probe's instant does not stop a follower."""
    import fcntl
    cfg = _cfg(tmp_path)
    real, busy = fcntl.flock, [2]

    def probed(fd, op):
        if op & fcntl.LOCK_NB and op & fcntl.LOCK_EX and busy[0]:
            busy[0] -= 1
            raise BlockingIOError(11, "probe holds it")
        return real(fd, op)
    monkeypatch.setattr(E.fcntl, "flock", probed)
    with E.reader_lock(cfg, "conductor"):
        assert busy[0] == 0
        monkeypatch.setattr(E.fcntl, "flock", real)
        try:
            with E.reader_lock(cfg, "conductor"):
                raise AssertionError("second follower got the lock")
        except ValueError as e:
            assert "already followed by pid" in str(e)


def test_reader_holder_ignores_another_probe_on_a_dead_follower(tmp_path, monkeypatch):
    """SPEC-0401: a concurrent probe's instant on a dead follower's lock is not a live holder;
    a live recorded pid is returned at once, and a real holder is still reported."""
    import fcntl
    cfg = _cfg(tmp_path)
    with E.reader_lock(cfg, "conductor"):
        assert E.reader_holder(cfg, "conductor") == os.getpid()
    d = E.cursors_dir(cfg)
    (d / "conductor.pid").write_text("4000001")
    monkeypatch.setattr(E.gate_slots, "_pid_alive", lambda p: p == os.getpid())
    real, busy = fcntl.flock, [2]

    def probed(fd, op):
        if op & fcntl.LOCK_NB and op & fcntl.LOCK_EX and busy[0]:
            busy[0] -= 1
            raise BlockingIOError(11, "another probe holds it")
        return real(fd, op)
    monkeypatch.setattr(E.fcntl, "flock", probed)
    monkeypatch.setattr(E, "READER_LOCK_RETRY_S", 0)
    assert E.reader_holder(cfg, "conductor") is None and busy[0] == 0
    busy[0] = 1000                                  # held for good, by a dead pid's record
    assert E.reader_holder(cfg, "conductor") == 4000001
    (d / "conductor.pid").write_text(str(os.getpid()))
    busy[0] = 1000
    assert E.reader_holder(cfg, "conductor") == os.getpid() and busy[0] == 999


def test_followers_empty(tmp_path):
    assert E.followers(_cfg(tmp_path)) == []


def test_cli_followers_and_conduct_status(tmp_path, monkeypatch):
    cfg = _setup(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    with E.reader_lock(cfg, "conductor"), E.reader_lock(cfg, "mailbox:steward:P"):
        r = CliRunner().invoke(cli, ["queue", "followers", "--json"])
        assert r.exit_code == 0, r.output
        out = json.loads(r.output)
        assert out["schema"] == "nw.queue.followers.v1"
        by = {f["reader"]: f for f in out["followers"]}
        assert by["conductor"]["pid"] == os.getpid() and by["conductor"]["held"]
        assert by["mailbox:human"]["pid"] is None and by["mailbox:human"]["held"] is False
        r = CliRunner().invoke(cli, ["queue", "followers"])
        assert r.exit_code == 0 and "not armed" in r.output and "mailbox:steward:P" in r.output
        r = CliRunner().invoke(cli, ["queue", "conduct-status", "--json"])
        st = json.loads(r.output)
        assert st["stewards"] == [{"address": "steward:P", "pid": os.getpid(), "lag": 0}]
        r = CliRunner().invoke(cli, ["queue", "conduct-status"])
        assert f"steward:P (pid {os.getpid()}, lag 0)" in r.output
    r = CliRunner().invoke(cli, ["queue", "conduct-status", "--json"])
    assert json.loads(r.output)["stewards"] == []
    r = CliRunner().invoke(cli, ["queue", "conduct-status"])
    assert "none armed" in r.output
