"""SPEC-0181: the queue event stream — append/validate/ids, projection from campaign ledgers
(exactly once), watchdog signals (once per condition), cursors and the reader lock, and the
`wt queue events` / `wt queue conduct-status` CLI."""
import datetime as dt
import io
import json
import threading

import pytest
from click.testing import CliRunner

from wt.agentic import events as E
from wt.agentic import lease as L
from wt.agentic import store
from wt.cli import cli


def _cfg(tmp_path):
    return {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "config"),
            "agentic": {"lease_ttl_s": 120}, "outbox_targets": {}, "specs_dir": str(tmp_path)}


def _campaign(cfg, cid="SPEC-0900-20261025-aaaa", state="IMPLEMENTING"):
    store.create_campaign(cfg, {"id": cid, "spec_id": cid[:9], "project": "Meta-Tools",
                                "profile": "balanced", "state": state,
                                "repo": {"base": "main"}, "cost": {"total_usd": 0.0,
                                                                   "cap_usd": 10.0}})
    return cid


def _set_state(cfg, cid, **kw):
    st = store.read_state(cfg, cid)
    st.update(kw)
    store.write_state(cfg, cid, st)


# ---- append / read ------------------------------------------------------------------------

def test_append_validates_and_assigns_ids(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError, match="unknown event type"):
        E.append(cfg, "nope", data={})
    with pytest.raises(ValueError, match="missing data keys"):
        E.append(cfg, "parked", data={"reason": "x"})
    a = E.append(cfg, "admitted", campaign="c1")
    b = E.append(cfg, "merged", campaign="c1", data={"merged_sha": "abc"})
    assert (a["id"], b["id"]) == (1, 2)
    assert a["schema"] == "wt.event.v1"
    assert [e["type"] for e in E.read_since(cfg, 1)] == ["merged"]


def test_concurrent_appends_have_unique_increasing_ids(tmp_path):
    cfg = _cfg(tmp_path)

    def worker():
        for _ in range(30):
            E.append(cfg, "admitted", campaign="c")

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = [e["id"] for e in E.read_since(cfg, 0)]
    assert ids == list(range(1, 181))


# ---- projection ---------------------------------------------------------------------------

def test_projection_maps_ledger_events_exactly_once(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg)
    store.append_event(cfg, cid, "transition", {"from": "QUEUED", "to": "OPENING",
                                                "reason": "admitted"})
    store.append_event(cfg, cid, "run.end", {"persona": "implementer", "run_id": "001",
                                             "is_error": True, "timed_out": False,
                                             "result_tail": "You've hit your session limit"})
    store.append_event(cfg, cid, "run.end", {"persona": "implementer", "run_id": "002",
                                             "is_error": True, "timed_out": False,
                                             "result_tail": "SyntaxError in foo.py"})
    store.append_event(cfg, cid, "cost", {"total_usd": 8.5, "cap_usd": 10.0})
    store.append_event(cfg, cid, "cost", {"total_usd": 9.0, "cap_usd": 10.0})
    store.append_event(cfg, cid, "park", {"reason": "cost_cap", "detail": "d",
                                          "resume_to": "IMPLEMENTING"})
    _set_state(cfg, cid, state="PARKED")
    E.sync(cfg)
    E.sync(cfg)                                  # second sync adds nothing
    evs = E.read_since(cfg, 0)
    types = [e["type"] for e in evs]
    assert types.count("admitted") == 1
    assert types.count("cost_warning") == 1      # once per campaign
    assert types.count("parked") == 1
    failed = [e for e in evs if e["type"] == "run_failed"]
    assert [f["data"]["transient"] for f in failed] == [True, False]
    assert all(e["campaign"] == cid and e["spec_id"] == "SPEC-0900" for e in evs
               if e["by"] != "watchdog")


def test_merged_event_carries_sha(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, state="CLOSING")
    _set_state(cfg, cid, state="MERGED", close={"mode": "merge", "merged_sha": "deadbeef"})
    store.append_event(cfg, cid, "transition", {"from": "CLOSING", "to": "MERGED",
                                                "reason": "closed"})
    E.sync(cfg)
    merged = [e for e in E.read_since(cfg, 0) if e["type"] == "merged"]
    assert merged and merged[0]["data"]["merged_sha"] == "deadbeef"


# ---- watchdog -----------------------------------------------------------------------------

def test_runner_dead_fires_once_and_again_after_clearing(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg)
    E.sync(cfg)
    E.sync(cfg)
    dead = [e for e in E.read_since(cfg, 0) if e["type"] == "runner_dead"]
    assert len(dead) == 1 and dead[0]["by"] == "watchdog"
    lease = L.acquire(cfg, ["test"])            # runner alive → condition clears
    try:
        E.sync(cfg)
    finally:
        lease.release()
    E.sync(cfg)                                  # dead again → fires again
    assert len([e for e in E.read_since(cfg, 0) if e["type"] == "runner_dead"]) == 2
    _set_state(cfg, cid, state="MERGED")         # nothing pending → no more alarms
    E.sync(cfg)
    assert len([e for e in E.read_since(cfg, 0) if e["type"] == "runner_dead"]) == 2


def test_run_overdue_and_silent(tmp_path):
    cfg = _cfg(tmp_path)
    a = _campaign(cfg, "SPEC-0901-20261025-aaaa")
    b = _campaign(cfg, "SPEC-0902-20261025-bbbb", state="GATING")
    store.append_event(cfg, a, "run.start", {"persona": "implementer", "run_id": "001",
                                             "timeout_s": 60})
    lease = L.acquire(cfg, ["test"])
    try:
        later = dt.datetime.now().astimezone() + dt.timedelta(seconds=2000)
        found = {t: (c, d) for _k, t, c, d in E.watchdog_checks(cfg, now=later)}
        assert found["run_overdue"][0] == a
        assert found["run_overdue"][1]["run_id"] == "001"
        assert found["campaign_silent"][0] == b
        # in-flight run within budget: lease not stale even with an old heartbeat
        soon = dt.datetime.now().astimezone() + dt.timedelta(seconds=30)
        assert "run_overdue" not in {t for _k, t, _c, _d in E.watchdog_checks(cfg, now=soon)}
    finally:
        lease.release()


# ---- cursors, reader lock, follow -----------------------------------------------------------

def test_follow_resumes_after_cursor_and_new_reader_starts_at_now(tmp_path):
    cfg = _cfg(tmp_path)
    E.append(cfg, "admitted", campaign="old")       # history before the reader exists
    out = io.StringIO()
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        if calls["n"] == 1:
            E.append(cfg, "merged", campaign="c", data={"merged_sha": "s1"})
            return False
        return True

    E.follow(cfg, "conductor", poll_s=0, out=out, stop=stop)
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [e["type"] for e in lines] == ["merged"]   # not the pre-existing history
    E.append(cfg, "abandoned", campaign="c")
    out2 = io.StringIO()
    E.follow(cfg, "conductor", poll_s=0, out=out2, max_events=1)
    assert json.loads(out2.getvalue())["type"] == "abandoned"


def test_reader_lock_allows_one_follower(tmp_path):
    cfg = _cfg(tmp_path)
    with E.reader_lock(cfg, "conductor"):
        assert E.reader_holder(cfg, "conductor") is not None
        with pytest.raises(ValueError, match="already followed"):
            with E.reader_lock(cfg, "conductor"):
                pass
    assert E.reader_holder(cfg, "conductor") is None


# ---- CLI ----------------------------------------------------------------------------------

def test_cli_events_history_and_conduct_status(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    cid = _campaign(cfg, state="PARKED")
    _set_state(cfg, cid, park={"reason": "cost_cap", "detail": "over",
                               "resume_to": "IMPLEMENTING"})
    store.append_event(cfg, cid, "park", {"reason": "cost_cap", "detail": "over",
                                          "resume_to": "IMPLEMENTING"})
    r = CliRunner().invoke(cli, ["queue", "events"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["schema"] == "wt.events.v1"
    assert "parked" in [e["type"] for e in payload["events"]]
    r = CliRunner().invoke(cli, ["queue", "conduct-status", "--json"])
    assert r.exit_code == 0, r.output
    st = json.loads(r.output)
    assert st["schema"] == "wt.conduct.status.v1"
    assert st["counts"]["parked"] == 1
    assert st["parked"][0]["park"]["reason"] == "cost_cap"
    r = CliRunner().invoke(cli, ["queue", "events", "--follow"])
    assert r.exit_code != 0 and "--for" in r.output


def test_lease_not_stale_while_gates_run(tmp_path):
    """A GATING/CLOSING item runs gates in the loop's process; its stale heartbeat is not a
    hung runner within the gate budget (false positive seen on the first live conductor)."""
    cfg = _cfg(tmp_path)
    _campaign(cfg, state="CLOSING")
    lease = L.acquire(cfg, ["test"])
    try:
        soon = dt.datetime.now().astimezone() + dt.timedelta(seconds=300)
        assert "lease_stale" not in {t for _k, t, _c, _d in E.watchdog_checks(cfg, now=soon)}
        late = dt.datetime.now().astimezone() + dt.timedelta(seconds=4000)
        assert "lease_stale" in {t for _k, t, _c, _d in E.watchdog_checks(cfg, now=late)}
    finally:
        lease.release()
