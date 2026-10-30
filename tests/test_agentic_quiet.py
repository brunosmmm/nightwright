"""SPEC-0300: the quiet conductor follower: classification, the pending digest, counting."""
import datetime as dt
import io
import json
import re
import types
from pathlib import Path

import pytest
from click.testing import CliRunner

from wt.agentic import codewatch as CW
from wt.agentic import events as E
from wt.agentic import quiet as QT
from wt.cli import cli

HOLD = ["admitted", "resumed", "waiting", "abandoned", "cost_warning", "escalation",
        "escalation_withdrawn", "rebase_resolved", "run_failed", "merged", "handed_off"]
WAKE = ["parked", "park_undecided", "runner_dead", "lease_stale", "run_overdue", "message",
        "escalation_answered", "ready_not_admitted", "waiting_not_admitted",
        "runner_journal_silent", "temp_pressure", "runner_config_invalid",
        "backlog_blocked_long", "code_changed", "brand_new_type"]

DATA = {"merged": {"merged_sha": "s"}, "message": {"msg_id": "1", "from": "x", "kind": "info",
                                                   "text": "t"},
        "admitted": {}, "abandoned": {}, "cost_warning": {"total_usd": 1, "cap_usd": 2}}


def _cfg(tmp_path, quiet=None):
    cfg = {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "config"),
           "agentic": {"lease_ttl_s": 120}, "specs_dir": str(tmp_path)}
    if quiet is not None:
        cfg["agentic"]["conductor"] = {"quiet": quiet}
    return cfg


def _ctx(cfg, lease=None, now=None):
    return QT.Ctx(cfg, "conductor", lease=lease if lease is not None else {"held": False},
                  now=now)


def _ev(type_, campaign=None, **data):
    return {"id": 1, "ts": "2026-10-30T10:00:00+00:00", "type": type_, "campaign": campaign,
            "data": data}


def _ping(*, stuck=(), blocked=(), runner=None, parked=()):
    return {"blocked": [{"campaign": c} for c in blocked], "parked": list(parked),
            "stuck": [{"campaign": c} for c in stuck], "waiting": [], "ready_not_admitted": [],
            "waiting_not_admitted": [], "runner": runner, "counts": {}}


def _follow(cfg, out, *, events=(), clock=None, **kw):
    kw.setdefault("stop", lambda: True)
    return E.follow(cfg, "conductor", poll_s=0, out=out, code_root=None, quiet=True,
                    now=clock, **kw)


def _lines(out):
    return [json.loads(x) for x in out.getvalue().splitlines()]


class Clock:
    def __init__(self):
        self.t = dt.datetime.now().astimezone()

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += dt.timedelta(seconds=s)


# ---- classification -----------------------------------------------------------------------

@pytest.mark.parametrize("type_", HOLD)
def test_classify_always_hold_and_wake_tables_hold(tmp_path, type_):
    cfg = _cfg(tmp_path)
    assert QT.classify(cfg, _ev(type_), _ctx(cfg)) == "hold"


@pytest.mark.parametrize("type_", WAKE)
def test_classify_always_hold_and_wake_tables_wake(tmp_path, type_):
    cfg = _cfg(tmp_path)
    assert QT.classify(cfg, _ev(type_), _ctx(cfg)) == "wake"


def test_classify_backlog(tmp_path):
    cfg = _cfg(tmp_path)
    c = _ctx(cfg)
    assert QT.classify(cfg, _ev("backlog", ready=True, blocked_by=[]), c) == "hold"
    assert QT.classify(cfg, _ev("backlog", ready=False,
                                blocked_by=["waiting on SPEC-0001 (accepted)"]), c) == "hold"
    assert QT.classify(cfg, _ev("backlog", ready=False,
                                blocked_by=["SPEC-0999 not found"]), c) == "wake"
    for st in ("draft", "proposed", "in-progress"):       # may still become accepted
        assert QT.classify(cfg, _ev("backlog", ready=False,
                                    blocked_by=[f"spec status is {st}"]), c) == "hold"
    for st in ("missing", "rejected", "superseded", "done"):   # can never become ready
        assert QT.classify(cfg, _ev("backlog", ready=False,
                                    blocked_by=[f"spec status is {st}"]), c) == "wake"


def test_ping_fingerprint_hold_and_change(tmp_path):
    cfg = _cfg(tmp_path)

    def verdict(data):
        ev = _ev("conductor_ping", **data)
        ctx = QT.Ctx(cfg, "conductor")
        res = QT.classify(cfg, ev, ctx)
        if res == "wake":
            ctx.digest.note_ping_wake(ev)
            ctx.digest.save()
        return res
    assert verdict(_ping()) == "hold"
    assert verdict(_ping(stuck=["a"])) == "wake"
    assert verdict(_ping(stuck=["a"])) == "hold"
    assert verdict(_ping(stuck=["b"])) == "wake"
    assert verdict(_ping(runner={"pid": 7})) == "wake"
    # a decided or human-owned park is not actionable; an undecided conductor park is
    quiet_park = {"campaign": "p", "owner": "human", "decided": False}
    assert QT.ping_fingerprint(_ping(parked=[quiet_park])) == []
    assert QT.ping_fingerprint(_ping(parked=[{**quiet_park, "owner": "conductor"}])) == \
        [["parked", "p"]]


def test_runner_draining_hold_window(tmp_path):
    cfg = _cfg(tmp_path)
    budget = E.settings(cfg)["gate_budget_s"]
    c = _ctx(cfg)

    def verdict(draining_s, waiting_on):
        return QT.classify(cfg, _ev("runner_draining", reason="r", draining_s=draining_s,
                                    waiting_on=waiting_on, ready=1), c)
    w = [{"campaign": "a", "timeout_s": 100}, {"campaign": "b", "timeout_s": 900}]
    assert verdict(900 + 300, w) == "hold"
    assert verdict(900 + 301, w) == "wake"
    assert verdict(budget + 300, []) == "hold"              # empty: gate_budget_s fallback
    assert verdict(budget + 301, []) == "wake"
    assert verdict(budget + 300, [{"campaign": "a", "timeout_s": None}]) == "hold"
    assert verdict(budget + 301, [{"campaign": "a", "timeout_s": None}]) == "wake"


def test_silent_held_while_in_flight(tmp_path):
    cfg = _cfg(tmp_path)
    now = dt.datetime.now().astimezone()
    for type_ in ("campaign_silent", "state_stuck"):
        ev = _ev(type_, campaign="c1", state="GATING", silent_s=1000, stuck_s=1000)
        held = {"held": True, "in_flight": ["c1"]}
        assert QT.classify(cfg, ev, _ctx(cfg, held)) == "hold"
        assert QT.classify(cfg, ev, _ctx(cfg, {"held": True, "in_flight": ["c2"]})) == "wake"
        assert QT.classify(cfg, ev, _ctx(cfg, {"held": False, "in_flight": ["c1"]})) == "wake"
        fresh = {"held": True, "busy": {"kind": "close", "campaign": "c1",
                                        "since": now.isoformat(), "budget_s": 3600}}
        assert QT.classify(cfg, ev, _ctx(cfg, fresh)) == "hold"
        old = {"held": True, "busy": {"kind": "close", "campaign": "c1", "budget_s": 60,
                                      "since": (now - dt.timedelta(seconds=600)).isoformat()}}
        assert QT.classify(cfg, ev, _ctx(cfg, old)) == "wake"
        other = {"held": True, "busy": {"kind": "close", "campaign": "c9",
                                        "since": now.isoformat(), "budget_s": 3600}}
        assert QT.classify(cfg, ev, _ctx(cfg, other)) == "wake"


def test_settings_validate(tmp_path):
    assert QT.settings(_cfg(tmp_path)) == QT.DEFAULTS
    s = QT.settings(_cfg(tmp_path, {"enabled": "yes", "readers": "x", "digest_interval_s": -1}))
    assert s == QT.DEFAULTS
    assert QT.enabled_for(_cfg(tmp_path), "conductor")
    assert not QT.enabled_for(_cfg(tmp_path), "steward")
    assert not QT.enabled_for(_cfg(tmp_path, {"enabled": False}), "conductor")


def test_settings_custom_readers_and_interval(tmp_path):
    cfg = _cfg(tmp_path, {"readers": ["steward"], "digest_interval_s": 60})
    assert QT.settings(cfg)["readers"] == ["steward"]
    assert QT.settings(cfg)["digest_interval_s"] == 60
    assert QT.enabled_for(cfg, "steward")
    assert not QT.enabled_for(cfg, "conductor")


def test_custom_digest_interval_changes_when_digest_prints(tmp_path):
    cfg, clock = _cfg(tmp_path, {"digest_interval_s": 60}), Clock()
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "admitted", campaign="a")
    out, calls = io.StringIO(), {"n": 0}

    def stop():
        calls["n"] += 1
        if calls["n"] == 1:
            clock.advance(30)                           # inside 60 s: still held
            return False
        if calls["n"] == 2:
            assert out.getvalue() == ""
            clock.advance(31)                           # past 60 s, far inside the 1800 default
            return False
        return True
    _follow(cfg, out, clock=clock, stop=stop)
    (digest,) = _lines(out)
    assert digest["type"] == "conductor_digest"


# ---- the digest -----------------------------------------------------------------------------

def test_digest_emitted_after_interval(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "admitted", campaign="a")
    E.append(cfg, "merged", campaign="m", spec_id="SPEC-0001", data={"merged_sha": "s"})
    out, calls = io.StringIO(), {"n": 0}

    def stop():
        calls["n"] += 1
        if calls["n"] == 1:
            assert out.getvalue() == ""                 # held, interval not reached
            clock.advance(1801)
            return False
        return True
    _follow(cfg, out, clock=clock, stop=stop)
    (digest,) = _lines(out)
    assert digest["type"] == "conductor_digest" and digest["id"] is None
    d = digest["data"]
    assert (d["first_id"], d["last_id"]) == (1, 2)
    assert d["held"] == {"admitted": 1, "merged": 1}
    assert d["merged"] == [{"campaign": "m", "spec_id": "SPEC-0001", "label": "SPEC-0001"}]
    assert E.cursor_get(cfg, "conductor") == 2


def test_digest_text_line(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "merged", campaign="m", spec_id="SPEC-0001", data={"merged_sha": "s"})
    clock.advance(4000)
    out = io.StringIO()
    _follow(cfg, out, clock=clock, as_json=False)
    assert out.getvalue() == "digest #1..#1 held=merged:1 merged=SPEC-0001\n"


def test_digest_never_empty(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "message", by="t", data=DATA["message"])
    clock.advance(10 ** 6)
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    assert [e["type"] for e in _lines(out)] == ["message"]


def test_digest_survives_rearm(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)
    for _ in range(3):
        E.append(cfg, "admitted", campaign="a")
    out1 = io.StringIO()
    _follow(cfg, out1, clock=clock)                       # exits (as on code_changed)
    assert out1.getvalue() == ""
    assert QT.Digest.load(cfg, "conductor").pending["held"] == {"admitted": 3}
    clock.advance(1801)
    out2 = io.StringIO()
    _follow(cfg, out2, clock=clock)
    (digest,) = _lines(out2)
    assert digest["data"]["held"] == {"admitted": 3}
    assert (digest["data"]["first_id"], digest["data"]["last_id"]) == (1, 3)
    out3 = io.StringIO()
    clock.advance(1801)
    _follow(cfg, out3, clock=clock)
    assert out3.getvalue() == ""                          # nothing delivered twice


def test_ping_items_in_digest_and_fp_kept_after_clear(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)

    def ping(**kw):
        E.append(cfg, "conductor_ping", by="watchdog", data=_ping(**kw))
    ping(stuck=["a"])                                     # wakes (new fingerprint)
    ping(stuck=["a"])                                     # held
    ping(stuck=["a"])                                     # held
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    assert [e["type"] for e in _lines(out)] == ["conductor_ping"]
    clock.advance(1801)
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    (digest,) = _lines(out)
    assert digest["data"]["pings_held"] == 2
    assert digest["data"]["ping_items"] == [["stuck", "a"]]
    # the digest was cleared, last_ping_fp is kept: the same ping is still held
    assert QT.Digest.load(cfg, "conductor").empty
    ping(stuck=["a"])
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    assert [e["type"] for e in _lines(out)] == ["conductor_digest"]     # not a ping line
    # an all-clear ping resets it: the same items later wake again
    ping()
    ping(stuck=["a"])
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    assert [e["type"] for e in _lines(out)].count("conductor_ping") == 1


def test_hold_crash_between_digest_and_cursor(tmp_path, monkeypatch):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "admitted", campaign="a")
    real = E.cursor_set

    def boom(*a, **kw):
        monkeypatch.setattr(E, "cursor_set", real)
        raise OSError("killed")
    monkeypatch.setattr(E, "cursor_set", boom)
    with pytest.raises(OSError):
        _follow(cfg, io.StringIO(), clock=clock)
    assert E.cursor_get(cfg, "conductor") == 0            # cursor did not advance
    assert QT.Digest.load(cfg, "conductor").pending["held"] == {"admitted": 1}
    _follow(cfg, io.StringIO(), clock=clock)              # re-arm: re-read, not re-counted
    assert E.cursor_get(cfg, "conductor") == 1
    clock.advance(1801)
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    (digest,) = _lines(out)
    assert digest["data"]["held"] == {"admitted": 1}
    assert QT.wakeups_report(cfg)["held"] == 1            # recorded once


def test_hold_crash_between_record_and_save_keeps_the_hold(tmp_path, monkeypatch):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "admitted", campaign="a")
    real = QT.record_delivery
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("killed")
        return real(*a, **kw)
    monkeypatch.setattr(QT, "record_delivery", flaky)
    with pytest.raises(OSError):
        _follow(cfg, io.StringIO(), clock=clock)
    assert E.cursor_get(cfg, "conductor") == 0
    _follow(cfg, io.StringIO(), clock=clock)              # re-arm: the event is held again
    assert E.cursor_get(cfg, "conductor") == 1
    assert QT.wakeups_report(cfg)["held"] == 1
    assert QT.Digest.load(cfg, "conductor").pending["held"] == {"admitted": 1}


def test_digest_crash_before_print(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "admitted", campaign="a")
    clock.advance(1801)

    class Flaky(io.StringIO):
        def write(self, s):
            raise BrokenPipeError

    with pytest.raises(BrokenPipeError):
        _follow(cfg, Flaky(), clock=clock)
    assert QT.Digest.load(cfg, "conductor").empty         # cleared and saved first
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    assert out.getvalue() == ""                           # dropped, never duplicated


def test_max_events_counts_delivered_only(tmp_path):
    cfg = _cfg(tmp_path)
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "admitted", campaign="a")
    E.append(cfg, "admitted", campaign="b")
    E.append(cfg, "message", by="t", data=DATA["message"])
    E.append(cfg, "admitted", campaign="c")
    E.append(cfg, "message", by="t", data={**DATA["message"], "msg_id": "2"})
    out = io.StringIO()
    assert _follow(cfg, out, max_events=1) == 1
    assert [e["id"] for e in _lines(out)] == [3]
    assert E.cursor_get(cfg, "conductor") == 3
    assert QT.Digest.load(cfg, "conductor").pending["held"] == {"admitted": 2}


# ---- quiet off ------------------------------------------------------------------------------

def _seed(cfg):
    E.cursor_set(cfg, "conductor", 0)
    for t in ("admitted", "merged", "message", "abandoned"):
        E.append(cfg, t, campaign="c", data=DATA.get(t, {}))


def test_quiet_off_is_byte_identical(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    expected = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in E.read_since(cfg, 0))
    out = io.StringIO()
    E.follow(cfg, "conductor", poll_s=0, out=out, code_root=None, quiet=False,
             stop=lambda: True)
    assert out.getvalue() == expected
    out = io.StringIO()                                   # the default is off too
    E.follow(cfg, "steward", poll_s=0, out=out, code_root=None, from_start=True,
             stop=lambda: True)
    assert out.getvalue() == expected
    assert not (E.cursors_dir(cfg) / "conductor.digest.json").exists()
    assert not QT.deliveries_path(cfg).exists()           # quiet off records nothing


def test_quiet_off_text_mode_is_byte_identical(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    expected = "".join(E.summary_line(e) + "\n" for e in E.read_since(cfg, 0))
    out = io.StringIO()
    E.follow(cfg, "conductor", poll_s=0, out=out, code_root=None, quiet=False, as_json=False,
             stop=lambda: True)
    assert out.getvalue() == expected
    assert not (E.cursors_dir(cfg) / "conductor.digest.json").exists()
    assert not QT.deliveries_path(cfg).exists()


def test_events_jsonl_unchanged(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    before = E.events_path(cfg).read_bytes()
    clock = Clock()
    clock.advance(4000)
    _follow(cfg, io.StringIO(), clock=clock)
    assert E.events_path(cfg).read_bytes() == before


# ---- counting -------------------------------------------------------------------------------

class _FakeWatcher:
    def __init__(self, *a, **kw):
        pass

    def poll(self, now):
        return {"from_sha": "a", "to_sha": "b", "paths": ["src/wt/x.py"], "skills_changed": []}


def test_deliveries_and_wakeups_report(tmp_path, monkeypatch):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)
    for i in range(2):
        E.append(cfg, "merged", campaign=f"m{i}", spec_id=f"SPEC-000{i}",
                 data={"merged_sha": "s"})
    for i in range(8):
        E.append(cfg, "admitted", campaign=f"a{i}")
    for i in range(6):
        E.append(cfg, "message", by="t", data={**DATA["message"], "msg_id": str(i)})
    clock.advance(1801)
    monkeypatch.setattr(CW, "Watcher", _FakeWatcher)
    out = io.StringIO()
    E.follow(cfg, "conductor", poll_s=0, out=out, code_root="x", quiet=True, now=clock)
    types = [e["type"] for e in _lines(out)]
    assert types.count("message") == 6 and types.count("conductor_digest") == 1
    assert types[-1] == "code_changed"
    rep = QT.wakeups_report(cfg)
    assert rep["schema"] == "wt.conductor.wakeups.v1"
    assert (rep["delivered"], rep["held"], rep["digests"], rep["merges"]) == (8, 10, 1, 2)
    assert rep["wakeups_per_merge"] == 4.0
    assert rep["by_type"]["code_changed"] == {"wake": 1, "hold": 0}
    assert rep["by_type"]["merged"] == {"wake": 0, "hold": 2}
    assert len(rep["by_day"]) == 1
    # the window is by day, inclusive
    assert QT.wakeups_report(cfg, since="2999-01-01")["delivered"] == 0
    assert QT.wakeups_report(cfg, reader="steward")["delivered"] == 0


def test_followers_ignores_digest_file(tmp_path):
    cfg = _cfg(tmp_path)
    E.cursor_set(cfg, "conductor", 0)
    QT.Digest.load(cfg, "conductor").save()
    assert [r["reader"] for r in E.followers(cfg)] == ["conductor"]


# ---- CLI ------------------------------------------------------------------------------------

def test_cli_events_all_flag(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(E, "follow", lambda cfg, reader, **kw: seen.append(kw["quiet"]))
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    run = lambda *args: CliRunner().invoke(cli, ["queue", "events", "--follow", *args])
    assert run("--for", "conductor").exit_code == 0
    assert run("--for", "conductor", "--all").exit_code == 0
    assert run("--for", "steward").exit_code == 0
    cfg["agentic"]["conductor"] = {"quiet": {"enabled": False}}
    assert run("--for", "conductor").exit_code == 0
    cfg["agentic"]["conductor"] = {"quiet": {"readers": ["steward"]}}
    assert run("--for", "conductor").exit_code == 0
    assert run("--for", "steward").exit_code == 0
    assert seen == [True, False, False, False, False, True]


def test_cli_events_campaign_filter(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    for cid in ("a", "b"):
        E.append(cfg, "run_failed", campaign=cid, by="t",
                 data={"persona": "x", "run_id": "r", "transient": True, "error": "429"})
    r = CliRunner().invoke(cli, ["queue", "events", "--campaign", "b", "--type", "run_failed"])
    assert r.exit_code == 0, r.output
    assert [e["campaign"] for e in json.loads(r.output)["events"]] == ["b"]


def test_cli_wakeups_json(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    QT.record_delivery(cfg, "conductor", {"id": 1, "type": "message", "campaign": None}, "wake")
    QT.record_delivery(cfg, "conductor", {"id": 2, "type": "admitted", "campaign": "a"}, "hold")
    r = CliRunner().invoke(cli, ["queue", "wakeups", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["schema"] == "wt.conductor.wakeups.v1"
    assert (out["delivered"], out["held"], out["merges"]) == (1, 1, 0)
    assert out["wakeups_per_merge"] is None               # a window with no merges
    E.append(cfg, "merged", campaign="m", data={"merged_sha": "s"})
    out = json.loads(CliRunner().invoke(cli, ["queue", "wakeups", "--json"]).output)
    assert (out["merges"], out["wakeups_per_merge"]) == (1, 1.0)
    r = CliRunner().invoke(cli, ["queue", "wakeups"])
    assert r.exit_code == 0 and "delivered 1" in r.output


def test_wakeups_rejects_malformed_days(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    for args in (["--since", "2026-10-3"], ["--until", "tomorrow"], ["--since", "2026-13-01"]):
        r = CliRunner().invoke(cli, ["queue", "wakeups", "--json", *args])
        assert r.exit_code != 0 and "YYYY-MM-DD" in r.output
    with pytest.raises(ValueError):
        QT.wakeups_report(cfg, since="2026-10-3")
    assert QT.wakeups_report(cfg, since="2026-10-03", until="2026-10-31")["delivered"] == 0


# ---- held drains are re-checked every poll ----------------------------------------------------

def _drain_lease(clock, drained_s, *, since_s=None):
    """A held lease draining since `since_s` before now (default `drained_s`)."""
    since = clock() - dt.timedelta(seconds=drained_s if since_s is None else since_s)
    return {"held": True, "in_flight": [], "busy": None,
            "draining": {"since": since.isoformat(), "reason": "r"}}


def _hold_drain(cfg, clock, lease, monkeypatch):
    # only quiet sees this lease: the watchdog in `events.sync` must not raise its own drain
    monkeypatch.setattr(QT, "L", types.SimpleNamespace(lease_status=lambda cfg, now=None:
                                                       lease["v"]))
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "runner_draining", data={"reason": "r", "draining_s": 61, "ready": 0,
                                           "slots": 1, "waiting_on": [{"campaign": "a",
                                                                       "timeout_s": 100}]})


def test_held_drain_wakes_when_it_outlives_its_window(tmp_path, monkeypatch):
    cfg, clock = _cfg(tmp_path), Clock()
    lease = {"v": _drain_lease(clock, 61)}                  # one drain, `since` never changes
    _hold_drain(cfg, clock, lease, monkeypatch)
    out = io.StringIO()
    step = {"n": 0}

    def stop():
        step["n"] += 1
        if step["n"] == 1:
            assert out.getvalue() == ""                     # 61 s <= 100 + 300: held
            clock.advance(400)                              # the same drain is now 461 s old
            return False
        return True
    _follow(cfg, out, clock=clock, stop=stop)
    (wake,) = _lines(out)
    assert wake["type"] == "runner_draining" and wake["id"] == 1
    assert wake["data"]["draining_s"] == pytest.approx(461, abs=2)
    assert QT.Digest.load(cfg, "conductor").drains == []
    actions = [json.loads(x)["action"] for x in
               QT.deliveries_path(cfg).read_text().splitlines()]
    assert actions == ["hold", "wake"]
    out2 = io.StringIO()
    _follow(cfg, out2, clock=clock)
    assert out2.getvalue() == ""                            # woken once only


def test_held_drain_dropped_when_the_drain_ends_or_changes(tmp_path, monkeypatch):
    for after in ({"held": True, "draining": None}, {"held": False},
                  {"held": True, "draining": {"since": "2000-01-01T00:00:00+00:00"}}):
        cfg, clock = _cfg(tmp_path / str(len(str(after)))), Clock()
        lease = {"v": _drain_lease(clock, 61)}
        _hold_drain(cfg, clock, lease, monkeypatch)
        out = io.StringIO()
        _follow(cfg, out, clock=clock)
        assert len(QT.Digest.load(cfg, "conductor").drains) == 1
        clock.advance(10 ** 4)
        lease["v"] = after
        _follow(cfg, out, clock=clock)
        assert [e["type"] for e in _lines(out)] == ["conductor_digest"]     # no drain wake
        assert QT.Digest.load(cfg, "conductor").drains == []


def test_held_drain_survives_rearm_and_clear(tmp_path, monkeypatch):
    cfg, clock = _cfg(tmp_path), Clock()
    lease = {"v": _drain_lease(clock, 61)}
    _hold_drain(cfg, clock, lease, monkeypatch)
    E.append(cfg, "admitted", campaign="a")
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    clock.advance(1801)
    since = lease["v"]["draining"]["since"]
    lease["v"] = _drain_lease(clock, 0)
    lease["v"]["draining"]["since"] = since
    _follow(cfg, out, clock=clock)
    kinds = [e["type"] for e in _lines(out)]
    assert sorted(kinds) == ["conductor_digest", "runner_draining"]


@pytest.mark.parametrize("rec", [
    {"held": []}, {"held": {"a": 1}, "last_id": "7"}, {"last_ping_fp": "x"},
    {"pings_held": None}, {"last_printed_last_id": "3"}, {"drains": [1]},
    {"drains": [{"since": "s", "bound_s": 1}]}, {"first_id": "a"}, ["list"]])
def test_corrupt_digest_file_falls_back_to_fresh(tmp_path, rec):
    cfg = _cfg(tmp_path)
    path = E.cursors_dir(cfg) / "conductor.digest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec))
    d = QT.Digest.load(cfg, "conductor")
    assert d.empty and d.last_ping_fp == [] and d.last_printed_last_id == 0 and d.drains == []
    out = io.StringIO()
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "admitted", campaign="a")
    _follow(cfg, out)                                       # re-arming does not crash
    assert QT.Digest.load(cfg, "conductor").pending["held"] == {"admitted": 1}


# ---- SPEC-0305: the spec-phase events are held ------------------------------------------------

SPEC_PHASE = ("spec_written", "spec_reviewed", "spec_accepted")


def _spec_phase_log(cfg):
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "spec_written", campaign="c", data={"spec_id": "SPEC-0001"})
    E.append(cfg, "spec_reviewed", campaign="c",
             data={"spec_id": "SPEC-0001", "verdict": "revise", "round": 1})
    E.append(cfg, "spec_accepted", campaign="c", data={"spec_id": "SPEC-0001"})
    E.append(cfg, "parked", campaign="c", data={"reason": "spec_review_exhausted", "detail": "d", "resume_to": "SPEC_WRITING"})


def test_spec_phase_events_are_held(tmp_path):
    cfg = _cfg(tmp_path)
    ctx = _ctx(cfg)
    for t in SPEC_PHASE:
        assert QT.RULES[t] == "hold"
    for data in ({}, {"verdict": "accept", "round": 1}, {"verdict": "revise", "round": 2}):
        for t in SPEC_PHASE:
            assert QT.classify(cfg, _ev(t, "c", **data), ctx) == "hold"


def test_follow_holds_spec_phase_events(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    _spec_phase_log(cfg)
    before = E.events_path(cfg).read_bytes()
    clock.advance(1801)
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    assert [e["type"] for e in _lines(out)] == ["parked", "conductor_digest"]
    digest = _lines(out)[-1]["data"]
    assert all(digest["held"][t] == 1 for t in SPEC_PHASE)
    assert E.events_path(cfg).read_bytes() == before
    # --all (quiet off) and a steward reader (quiet is not enabled for it) see every event
    assert not QT.enabled_for(cfg, "steward")
    for reader, quiet in (("conductor", False), ("steward", QT.enabled_for(cfg, "steward"))):
        E.cursor_set(cfg, reader, 0)
        out = io.StringIO()
        E.follow(cfg, reader, poll_s=0, out=out, code_root=None, quiet=quiet, stop=lambda: True)
        assert [e["type"] for e in _lines(out)] == [*SPEC_PHASE, "parked"]


def test_wakeups_report_counts_spec_phase_as_held(tmp_path):
    cfg = _cfg(tmp_path)
    _spec_phase_log(cfg)
    _follow(cfg, io.StringIO(), clock=Clock())
    by_type = QT.wakeups_report(cfg)["by_type"]
    for t in SPEC_PHASE:
        assert by_type[t] == {"wake": 0, "hold": 1}
    assert by_type["parked"] == {"wake": 1, "hold": 0}


# A decision-table row whose action says "none" / "informational" names event types the quiet
# follower must hold unless one is listed here with the reason it must still wake (or is
# synthetic). The audit of the table found none: every informational type is in `QT.RULES`.
INFORMATIONAL_EXEMPT: dict[str, str] = {}

_CONDUCT_SKILL = Path(__file__).resolve().parent.parent / "skills" / "wt-conduct" / "SKILL.md"


def uncovered_informational_types(table_text: str) -> set[str]:
    """Event types named in the first column of informational rows (action `none` /
    `informational`) that are neither in `QT.RULES` nor in `INFORMATIONAL_EXEMPT`."""
    missing = set()
    for ln in table_text.splitlines():
        cells = [c.strip() for c in ln.strip().strip("|").split("|")] if ln.startswith("|") else []
        if len(cells) < 3 or not cells[-1].lower().startswith(("none", "informational")):
            continue
        for t in re.findall(r"`([a-z_]+)`", cells[0]):
            if t not in QT.RULES and t not in INFORMATIONAL_EXEMPT:
                missing.add(t)
    return missing


def test_decision_table_informational_rows_covered_by_rules():
    text = _CONDUCT_SKILL.read_text(encoding="utf-8")
    assert uncovered_informational_types(text) == set()
    assert all(t not in QT.RULES for t in INFORMATIONAL_EXEMPT)
    fake = "| `foo_event` | `foo_event` | none: informational |\n| `merged` | `merged` | none |"
    assert uncovered_informational_types(fake) == {"foo_event"}
