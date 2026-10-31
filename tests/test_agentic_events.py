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
from wt.agentic import schemas as SC

def _ev(cfg, cid, etype, payload=None, **kw):
    """append_event with the schema's required keys defaulted (SPEC-0192 fixtures)."""
    return store.append_event(cfg, cid, etype, SC.with_defaults(etype, payload), **kw)


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
    _ev(cfg, cid, "transition", {"from": "QUEUED", "to": "OPENING",
                                                "reason": "admitted"})
    _ev(cfg, cid, "run.end", {"persona": "implementer", "run_id": "001",
                                             "is_error": True, "timed_out": False,
                                             "result_tail": "You've hit your session limit"})
    _ev(cfg, cid, "run.end", {"persona": "implementer", "run_id": "002",
                                             "is_error": True, "timed_out": False,
                                             "result_tail": "SyntaxError in foo.py"})
    _ev(cfg, cid, "cost", {"total_usd": 8.5, "cap_usd": 10.0})
    _ev(cfg, cid, "cost", {"total_usd": 9.0, "cap_usd": 10.0})
    _ev(cfg, cid, "park", {"reason": "cost_cap", "detail": "d",
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
    _ev(cfg, cid, "transition", {"from": "CLOSING", "to": "MERGED",
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
    _ev(cfg, a, "run.start", {"persona": "implementer", "run_id": "001",
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


def _marker(cfg, cid, pid):
    from wt.agentic import gate_slots as GS
    GS.slots_dir(cfg).mkdir(parents=True, exist_ok=True)
    (GS.slots_dir(cfg) / f"waiting-{cid}.json").write_text(
        json.dumps({"campaign": cid, "pid": pid, "since": store.now_iso()}))


def _dead_pid():
    import subprocess
    import sys
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


@pytest.mark.parametrize("state", ["GATING", "CLOSING"])
def test_gate_slot_wait_marker_silences_silent_and_stuck(tmp_path, state):
    """SPEC-0309: a live waiter marker keeps `campaign_silent` and `state_stuck` quiet for a
    GATING or CLOSING campaign; a marker with a dead pid does not."""
    import os
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0903-20261025-cccc", state=state)
    later = dt.datetime.now().astimezone() + dt.timedelta(seconds=9000)
    sett = E.settings(cfg)

    def found():
        return {t for _k, t, c, _d in E.watchdog_checks(cfg, now=later) if c == cid}

    assert {"campaign_silent", "state_stuck"} <= found()
    _marker(cfg, cid, _dead_pid())
    assert {"campaign_silent", "state_stuck"} <= found()
    _marker(cfg, cid, os.getpid())
    assert found() == set()
    assert cid not in [i["campaign"] for i in E.stuck_items(cfg, later, sett)]


@pytest.mark.parametrize("state", ["GATING", "CLOSING"])
def test_recorded_gate_wait_is_not_forgiven_twice_and_run_overdue_never_fires(tmp_path, state):
    """The `gate` entry lands after the wait, so the stuck clock taken from it already excludes
    the wait: its `slot_wait_s` does not extend the limit a second time."""
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0904-20261025-dddd", state=state)
    sett = E.settings(cfg)
    limit = max(sett["stuck_after_s"], sett["gate_budget_s"])
    _ev(cfg, cid, "gate", {"gate_seq": 1, "ok": True, "slot_wait_s": 3000.0})
    now = dt.datetime.now().astimezone()
    st = store.read_state(cfg, cid)
    st["updated_at"] = (now - dt.timedelta(seconds=limit + 5000)).isoformat()
    store.write_state(cfg, cid, st)
    at = now + dt.timedelta(seconds=limit - 100)
    assert E.stuck_items(cfg, at, sett) == []
    at = now + dt.timedelta(seconds=limit + 100)         # not limit + 3000
    assert [i["campaign"] for i in E.stuck_items(cfg, at, sett)] == [cid]
    # gate runs write no `run.start`: nothing for `run_overdue` to measure
    assert "run_overdue" not in {t for _k, t, _c, _d in E.watchdog_checks(cfg, now=at)}


@pytest.mark.parametrize("state", ["GATING", "CLOSING"])
def test_finished_wait_in_a_live_hold_extends_the_limit_with_no_gate_entry(tmp_path, state):
    """The wait is over, the marker is gone and the gate is still running, so there is no `gate`
    entry for this run yet: the live hold's `waited_s` carries the extension."""
    import os
    from wt.agentic import gate_slots as GS
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0906-20261025-ffff", state=state)
    sett = E.settings(cfg)
    limit = max(sett["stuck_after_s"], sett["gate_budget_s"])
    now = dt.datetime.now().astimezone()
    at = now + dt.timedelta(seconds=limit + 100)
    assert [i["campaign"] for i in E.stuck_items(cfg, at, sett)] == [cid]    # no hold: stuck
    GS.slots_dir(cfg).mkdir(parents=True, exist_ok=True)
    (GS.slots_dir(cfg) / "slot-0.lock").write_text(json.dumps(
        {"campaign": cid, "pid": os.getpid(), "since": store.now_iso(), "waited_s": 2800.0}))
    assert GS.waiting(cfg, cid) is None                               # the marker is gone
    assert E.stuck_items(cfg, at, sett) == []                         # 2800 s of wait forgiven
    late = now + dt.timedelta(seconds=limit + 3000)
    assert [i["campaign"] for i in E.stuck_items(cfg, late, sett)] == [cid]   # but not forever
    dead = _dead_pid()
    (GS.slots_dir(cfg) / "slot-0.lock").write_text(json.dumps(
        {"campaign": cid, "pid": dead, "since": store.now_iso(), "waited_s": 2800.0}))
    assert GS.holding(cfg, cid) is None                               # a dead holder: no hold
    assert [i["campaign"] for i in E.stuck_items(cfg, at, sett)] == [cid]


@pytest.mark.parametrize("state", ["GATING", "CLOSING"])
def test_finished_wait_in_a_live_hold_is_not_silence(tmp_path, state):
    """The wait is over, the gate runs and the ledger has no new entry: silence starts at the
    later of the newest entry and the hold's start, so the wait is forgiven once, not twice."""
    import os
    from wt.agentic import gate_slots as GS
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0907-20261025-aaab", state=state)
    silent_after = E.settings(cfg)["silent_after_s"]
    later = dt.datetime.now().astimezone() + dt.timedelta(seconds=silent_after + 500)

    def silent():
        return "campaign_silent" in {t for _k, t, c, _d in E.watchdog_checks(cfg, now=later)
                                      if c == cid}

    def hold(held_s, pid=None):
        GS.slots_dir(cfg).mkdir(parents=True, exist_ok=True)
        since = (later - dt.timedelta(seconds=held_s)).isoformat()
        (GS.slots_dir(cfg) / "slot-0.lock").write_text(json.dumps(
            {"campaign": cid, "pid": pid or os.getpid(), "since": since, "waited_s": 900.0}))

    assert silent()                                   # no hold: silent
    hold(silent_after + 100)
    assert silent()                                   # held (gate running) longer than the limit
    hold(100)
    assert GS.waiting(cfg, cid) is None               # the marker is gone
    assert not silent()                               # the gate has run only 100 s
    hold(silent_after + 100, pid=_dead_pid())
    assert silent()                                   # a dead holder forgives nothing


@pytest.mark.parametrize("state", ["GATING", "CLOSING"])
def test_gate_entry_from_an_earlier_state_does_not_extend_the_stuck_limit(tmp_path, state,
                                                                          monkeypatch):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0908-20261025-aaac", state=state)
    sett = E.settings(cfg)
    limit = max(sett["stuck_after_s"], sett["gate_budget_s"])
    _ev(cfg, cid, "gate", {"gate_seq": 1, "ok": False, "slot_wait_s": 1500.0})
    later = dt.datetime.now().astimezone() + dt.timedelta(seconds=10)
    monkeypatch.setattr(store, "now_iso", lambda now=None: later.isoformat(timespec="seconds"))
    _ev(cfg, cid, "transition", {"from": "GATING", "to": state, "reason": "re-entered"})
    at = dt.datetime.now().astimezone() + dt.timedelta(seconds=10 + limit + 1)
    assert [i["campaign"] for i in E.stuck_items(cfg, at, sett)] == [cid]   # not limit + 1500


def test_gate_waiting_is_projected_into_the_event_stream(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0905-20261025-eeee", state="GATING")
    _ev(cfg, cid, "gate_waiting", {"slots": 2, "held": [{"slot": 0}], "waited_s": 31.0})
    E.sync(cfg)
    ev = [e for e in E.read_since(cfg, 0) if e["type"] == "gate_waiting"]
    assert len(ev) == 1 and ev[0]["data"]["waited_s"] == 31.0 and ev[0]["data"]["slots"] == 2


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


def _code_follow(cfg, repo, out, **kw):
    return E.follow(cfg, "conductor", poll_s=0.01, code_root=repo.root, code_check_s=0.01,
                    out=out, **kw)


def test_follow_exits_with_code_changed_and_resumes(tmp_path, code_repo):
    cfg = _cfg(tmp_path)
    E.cursor_set(cfg, "conductor", 0)
    e1 = E.append(cfg, "admitted", campaign="a")
    e2 = E.append(cfg, "admitted", campaign="b")
    out, calls = io.StringIO(), {"n": 0, "late": None}
    start = code_repo.head()

    def stop():
        calls["n"] += 1
        if calls["n"] == 1:
            # an event and a code commit land together: the event must still come first
            calls["late"] = E.append(cfg, "admitted", campaign="late")
            code_repo.commit({"skills/wt-conduct/SKILL.md": "new\n"})
        return False

    assert _code_follow(cfg, code_repo, out, stop=stop) == 3
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    late = calls["late"]
    assert [x["id"] for x in lines] == [e1["id"], e2["id"], late["id"], None]
    cc = lines[-1]
    assert cc["data"]["from_sha"] == start
    assert cc["type"] == "code_changed" and cc["by"] == "follower" and cc["schema"] == E.SCHEMA
    assert cc["campaign"] is None and cc["spec_id"] is None and cc["project"] is None
    assert cc["data"]["reader"] == "conductor"
    assert cc["data"]["skills_changed"] == ["wt-conduct"]
    assert cc["data"]["paths"] == ["skills/wt-conduct/SKILL.md"]
    assert cc["data"]["to_sha"] == code_repo.head()
    assert E.cursor_get(cfg, "conductor") == late["id"]
    assert all(e["type"] != "code_changed" for e in E.read_since(cfg, 0))
    # restart: the next event is delivered once, nothing repeated
    e3 = E.append(cfg, "abandoned", campaign="c")
    out2 = io.StringIO()
    assert _code_follow(cfg, code_repo, out2, max_events=1) == 1
    assert [json.loads(x)["id"] for x in out2.getvalue().splitlines()] == [e3["id"]]


def test_follow_code_changed_text_mode(tmp_path, code_repo):
    cfg = _cfg(tmp_path)
    E.cursor_set(cfg, "conductor", 0)
    out, calls = io.StringIO(), {"n": 0}

    def stop():
        calls["n"] += 1
        if calls["n"] == 1:
            code_repo.commit({"src/wt/x.py": "x = 2\n"})
        return False

    _code_follow(cfg, code_repo, out, stop=stop, as_json=False)
    to = code_repo.head()[:7]
    assert out.getvalue().splitlines()[-1].endswith(f"..{to} skills=- (re-arm)")
    assert out.getvalue().startswith("code_changed ")


def test_follow_without_code_watch_keeps_looping(tmp_path, code_repo):
    cfg = _cfg(tmp_path)
    E.cursor_set(cfg, "conductor", 0)
    out, calls = io.StringIO(), {"n": 0, "commits": 10}

    def stop():
        calls["n"] += 1
        if calls["n"] == 1:
            calls["commits"] += 1
            code_repo.commit({"src/wt/x.py": f"x = {calls['commits']}\n"})
        return calls["n"] >= 5

    assert E.follow(cfg, "conductor", poll_s=0, code_root=None, code_check_s=0.01, out=out,
                    stop=stop) == 0
    assert calls["n"] == 5 and out.getvalue() == ""
    # check_s 0 (agentic.events.code_check_s: 0) disables it too
    calls["n"] = 0
    assert E.follow(cfg, "conductor", poll_s=0, code_root=code_repo.root, code_check_s=0,
                    out=out, stop=stop) == 0
    assert calls["n"] == 5 and out.getvalue() == ""


def _watch_exits(follow_kw, cfg, code_repo, **kw):
    """Run E.follow with a code commit on the first stop(); return (printed lines, stop calls)."""
    out, calls = io.StringIO(), {"n": 0}

    def stop():
        calls["n"] += 1
        if calls["n"] == 1:
            code_repo.commit({"src/wt/x.py": f"x = {code_repo.head()}\n"})
        return calls["n"] >= 6

    E.follow(cfg, "conductor", poll_s=0.01, out=out, stop=stop, **follow_kw, **kw)
    return out.getvalue().splitlines(), calls["n"]


def test_code_check_s_default_and_config_key(tmp_path, code_repo, monkeypatch):
    assert E.DEFAULTS["code_check_s"] == 10
    assert E.settings(_cfg(tmp_path))["code_check_s"] == 10
    monkeypatch.setattr(E.CW, "code_checkout", lambda: code_repo.root)
    cfg = _cfg(tmp_path)
    E.cursor_set(cfg, "conductor", 0)
    # default 10 s: the follower is still running after a few fast iterations
    lines, n = _watch_exits({}, cfg, code_repo)
    assert lines == [] and n == 6
    # the config key is what the follower reads
    cfg["agentic"]["events"] = {"code_check_s": 0.01}
    lines, n = _watch_exits({}, cfg, code_repo)
    assert len(lines) == 1 and json.loads(lines[0])["type"] == "code_changed"
    # 0 turns it off
    cfg["agentic"]["events"] = {"code_check_s": 0}
    lines, n = _watch_exits({}, cfg, code_repo)
    assert lines == [] and n == 6


def test_follow_auto_code_root_uses_code_checkout(tmp_path, code_repo, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg["agentic"]["events"] = {"code_check_s": 0.01}
    E.cursor_set(cfg, "conductor", 0)
    monkeypatch.setattr(E.CW, "code_checkout", lambda: code_repo.root)
    lines, _ = _watch_exits({}, cfg, code_repo)
    assert json.loads(lines[-1])["type"] == "code_changed"
    # a non-checkout install (code_checkout() is None): the follower keeps running
    monkeypatch.setattr(E.CW, "code_checkout", lambda: None)
    lines, n = _watch_exits({}, cfg, code_repo)
    assert lines == [] and n == 6


def test_summary_line_null_id():
    line = E.summary_line({"id": None, "type": "admitted", "campaign": None, "data": {}})
    assert line.startswith("#- admitted")


# ---- CLI ----------------------------------------------------------------------------------

def test_cli_events_follow_no_code_watch(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    seen = []
    monkeypatch.setattr(E, "follow", lambda *a, **kw: seen.append(kw) or 0)
    r = CliRunner().invoke(cli, ["queue", "events", "--follow", "--for", "x"])
    assert r.exit_code == 0, r.output
    assert "code_root" not in seen[-1]
    r = CliRunner().invoke(cli, ["queue", "events", "--follow", "--for", "x",
                                 "--no-code-watch"])
    assert r.exit_code == 0, r.output
    assert seen[-1]["code_root"] is None


def test_cli_events_history_and_conduct_status(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    cid = _campaign(cfg, state="PARKED")
    _set_state(cfg, cid, park={"reason": "cost_cap", "detail": "over",
                               "resume_to": "IMPLEMENTING"})
    _ev(cfg, cid, "park", {"reason": "cost_cap", "detail": "over",
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


# ---- SPEC-0222: park_undecided --------------------------------------------------------------

def _park_at(cfg, cid, reason, ago_s):
    """Park `cid` (state + ledger event) `ago_s` before now."""
    from test_agentic_dashboard import ev_at
    park = {"reason": reason, "detail": "d", "resume_to": "GATING"}
    ev_at(cfg, cid, "park", park, ago_s=ago_s)
    _set_state(cfg, cid, state="PARKED", park={**park, "since": store.now_iso()})


def _undecided(cfg, **kw):
    return [e for e in E.sync(cfg, **kw) if e["type"] == "park_undecided"]


def test_park_undecided_fires_once_per_park_and_stops_on_a_note(tmp_path):
    from test_agentic_dashboard import ev_at
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0910-20261025-aaaa")
    dirty = _campaign(cfg, "SPEC-0911-20261025-aaaa")
    _park_at(cfg, cid, "rebase_conflict", 400)
    _park_at(cfg, dirty, "main_dirty", 400)             # human-owned: never fires
    fired = _undecided(cfg)
    assert len(fired) == 1
    e = fired[0]
    assert e["campaign"] == cid and e["by"] == "watchdog" and e["project"] == "Meta-Tools"
    assert e["data"]["reason"] == "rebase_conflict" and e["data"]["owner"] == "conductor"
    assert e["data"]["parked_for_s"] >= 400
    assert _undecided(cfg) == []                         # a re-sync does not repeat it
    ev_at(cfg, cid, "note", {"text": "decided", "author": "conductor"})
    assert _undecided(cfg) == []                         # a note clears the condition
    _set_state(cfg, cid, state="IMPLEMENTING")           # resume ...
    _park_at(cfg, cid, "rebase_conflict", 400)           # ... and re-park: new seq fires again
    assert len(_undecided(cfg)) == 1


def test_park_undecided_never_fires_for_a_park_that_has_a_note(tmp_path):
    """The note is on the park before the watchdog first sees it overdue, so fire-once cannot
    be what suppresses the event."""
    from test_agentic_dashboard import ev_at
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0916-20261025-aaaa")
    _park_at(cfg, cid, "rebase_conflict", 400)
    ev_at(cfg, cid, "note", {"text": "decided", "author": "conductor"}, ago_s=350)
    assert _undecided(cfg) == []


def test_park_undecided_reparks_fire_again_without_a_note_between(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, "SPEC-0917-20261025-aaaa")
    _park_at(cfg, cid, "rebase_conflict", 400)
    assert len(_undecided(cfg)) == 1
    _set_state(cfg, cid, state="IMPLEMENTING")           # resume (condition clears) ...
    assert _undecided(cfg) == []
    _park_at(cfg, cid, "rebase_conflict", 400)           # ... re-park: the new seq is a new key
    assert len(_undecided(cfg)) == 1


def test_park_undecided_not_before_threshold_or_for_human_reason(tmp_path):
    cfg = _cfg(tmp_path)
    young = _campaign(cfg, "SPEC-0912-20261025-aaaa")
    held = _campaign(cfg, "SPEC-0913-20261025-aaaa")
    _park_at(cfg, young, "base_moving", 100)
    _park_at(cfg, held, "human", 4000)                   # a deliberate hold is decided
    assert _undecided(cfg) == []


def test_ping_digest_owner_overdue_and_counts(tmp_path):
    cfg = _cfg(tmp_path)
    a = _campaign(cfg, "SPEC-0914-20261025-aaaa")
    b = _campaign(cfg, "SPEC-0915-20261025-aaaa")
    _park_at(cfg, a, "base_moving", 400)
    _park_at(cfg, b, "main_dirty", 400)
    d = E.ping_digest(cfg)
    by = {p["campaign"]: p for p in d["parked"]}
    assert by[a]["owner"] == "conductor" and by[a]["overdue"] is True
    assert by[b]["owner"] == "human" and by[b]["overdue"] is False
    assert d["counts"]["needs_you"] == 1 and d["counts"]["undecided_parks"] == 1
    assert d["counts"]["parked"] == 2


def test_rebase_resolved_is_projected(tmp_path):                                   # SPEC-0221
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, state="CLOSING")
    _ev(cfg, cid, "rebase_resolved", {"conflicts": ["src/app.py"], "summary": "merged both",
                                      "resolutions": [{"path": "src/app.py", "strategy": "both",
                                                       "how": "x"}]})
    E.sync(cfg)
    hit = [e for e in E.read_since(cfg, 0) if e["type"] == "rebase_resolved"]
    assert len(hit) == 1 and hit[0]["data"]["conflicts"] == ["src/app.py"]
    E.sync(cfg)
    assert len([e for e in E.read_since(cfg, 0) if e["type"] == "rebase_resolved"]) == 1
