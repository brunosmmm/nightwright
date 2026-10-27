"""SPEC-0251: the TUI events tab's view model (no Textual needed)."""
import datetime as dt
import json
import os
import time

import pytest

from test_agentic_events import _campaign, _cfg
from wt.agentic import events as E
from wt.tui import agentic_model as AM


def _append(cfg, type_, campaign=None, **data):
    return E.append(cfg, type_, campaign=campaign, by="test", data=data)


def _failed(cfg, transient, campaign="SPEC-0001-a"):
    return _append(cfg, "run_failed", campaign, persona="implementer", run_id="001",
                   transient=transient, error="x")


def _ping(cfg, **over):
    data = {"blocked": [{"campaign": "SPEC-0002-b", "blocked_for_s": 1500}],
            "parked": [{"campaign": "SPEC-0003-c", "owner": "conductor", "decided": False,
                        "parked_for_s": 400},
                       {"campaign": "SPEC-0004-d", "owner": "human", "decided": False,
                        "parked_for_s": 90},
                       {"campaign": "SPEC-0005-e", "owner": "conductor", "decided": True,
                        "parked_for_s": 90}],
            "stuck": [{"campaign": "SPEC-0006-f", "stuck_s": 1000}], "waiting": [],
            "ready_not_admitted": [{"campaign": "SPEC-0007-g", "ready_for_s": 120}],
            "counts": {"active": 2, "queued": 5, "ready": 3, "blocked": 1, "parked": 3,
                       "undecided_parks": 1, "stuck": 1, "waiting": 0}}
    data.update(over)
    return E.append(cfg, "conductor_ping", by="watchdog", data=data)


def test_load_events_limit_and_after_id(tmp_path):
    cfg = _cfg(tmp_path)
    for i in range(5):
        _append(cfg, "admitted", f"SPEC-000{i}-x")
    rows = AM.load_events(cfg, limit=3)
    assert [r.id for r in rows] == [3, 4, 5]
    assert [r.id for r in AM.load_events(cfg, after_id=3)] == [4, 5]
    assert AM.load_events(cfg, after_id=5) == []
    _append(cfg, "admitted", "SPEC-0009-x")
    assert [r.id for r in AM.load_events(cfg, after_id=5)] == [6]
    assert AM.load_events(_cfg(tmp_path / "none")) == []


def test_row_text_drops_the_id_type_campaign_prefix(tmp_path):
    cfg = _cfg(tmp_path)
    _append(cfg, "parked", "SPEC-0001-a", reason="rate_limited", detail="429", resume_to="X")
    _append(cfg, "admitted")
    _ping(cfg)
    parked, admitted, ping = AM.load_events(cfg)
    assert parked.campaign == "SPEC-0001-a" and parked.type == "parked"
    assert parked.text == "reason=rate_limited detail=429 resume_to=X"
    assert admitted.campaign == "" and admitted.text == ""
    assert ping.text.startswith("blocked=1 parked=3(undecided 1)")


@pytest.mark.parametrize("type_,data,style", [
    ("runner_dead", {"active": 1, "queued": 0}, "red"),
    ("lease_stale", {"pid": 1, "heartbeat_age_s": 99}, "red"),
    ("state_stuck", {"state": "X", "stuck_s": 1}, "red"),
    ("run_failed", {"persona": "p", "run_id": "1", "transient": False, "error": "e"}, "red"),
    ("run_failed", {"persona": "p", "run_id": "1", "transient": True, "error": "e"}, "yellow"),
    ("parked", {"reason": "r", "detail": "d", "resume_to": "X"}, "yellow"),
    ("escalation", {"escalation_id": "E1", "question": "q", "options": ["a"]}, "yellow"),
    ("merged", {"merged_sha": "abc"}, "green"),
    ("handed_off", {"pr_url": "u"}, "green"),
    ("admitted", {}, ""),
    ("resumed", {"to": "X"}, ""),
])
def test_style_per_type(tmp_path, type_, data, style):
    cfg = _cfg(tmp_path)
    E.append(cfg, type_, campaign="SPEC-0001-a", by="test", data=data)
    assert AM.load_events(cfg)[0].style == style


def test_alert_types_are_event_types():
    assert AM.ALERT_TYPES <= set(E.EVENT_TYPES)


def test_parse_and_format_event_filter_round_trip():
    f = AM.parse_event_filter("type=parked,merged campaign=SPEC-0230 alerts")
    assert f == AM.EventFilter(frozenset({"parked", "merged"}), "SPEC-0230", True)
    assert AM.format_event_filter(f) == "type=merged,parked campaign=SPEC-0230 alerts"
    assert AM.parse_event_filter(AM.format_event_filter(f)) == f
    assert AM.parse_event_filter("") == AM.EventFilter()
    assert AM.format_event_filter(AM.EventFilter()) == ""
    assert AM.parse_event_filter("alerts").alerts_only is True


@pytest.mark.parametrize("text,needle", [("type=parked,bogus", "bogus"), ("type=nope", "nope"),
                                         ("what", "what"), ("campaign=", "campaign=")])
def test_parse_event_filter_errors_name_the_problem(text, needle):
    with pytest.raises(ValueError, match=needle):
        AM.parse_event_filter(text)


def test_matches(tmp_path):
    cfg = _cfg(tmp_path)
    _append(cfg, "admitted", "SPEC-0230-a")
    _append(cfg, "merged", "SPEC-0231-b", merged_sha="abc")
    _append(cfg, "runner_dead", active=1, queued=0)
    admitted, merged, dead = AM.load_events(cfg)
    ok = lambda r, **kw: AM.matches(r, AM.EventFilter(**kw))     # noqa: E731
    assert all(ok(r) for r in (admitted, merged, dead))
    assert ok(merged, types=frozenset({"merged"})) and not ok(admitted, types=frozenset({"merged"}))
    assert ok(admitted, campaign="0230") and not ok(merged, campaign="0230")
    assert not ok(dead, campaign="0230")                 # no campaign never matches a campaign
    assert ok(dead, alerts_only=True) and not ok(merged, alerts_only=True)


def test_event_time_adds_the_date_when_not_today():
    now = dt.datetime(2026, 10, 25, 12, 0, 0).astimezone()
    assert AM.event_time(now.isoformat(), now) == "12:00:00"
    assert AM.event_time((now - dt.timedelta(days=2)).isoformat(), now) == "10-23 12:00:00"
    assert AM.event_time("garbage") == "garbage"


def test_load_conditions_runner_dead_without_a_sync(tmp_path):
    cfg = _cfg(tmp_path)
    assert AM.load_conditions(cfg) == []
    _campaign(cfg)                               # active, and no runner lease is held
    rows = AM.load_conditions(cfg)
    dead = next(r for r in rows if r.type == "runner_dead")
    assert dead.campaign == "" and dead.text == "active=1 queued=0" and dead.style == "red"
    assert not E.events_path(cfg).exists()       # nothing was synced or appended


def test_load_conditions_orders_campaign_conditions_after_runner(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(E, "watchdog_checks", lambda cfg: [
        ("k1", "state_stuck", "SPEC-0001-a", {"state": "IMPLEMENTING", "stuck_s": 5}),
        ("k2", "runner_dead", None, {"active": 1, "queued": 0}),
        ("k3", "park_undecided", "SPEC-0002-b", {"reason": "r"})])
    rows = AM.load_conditions(cfg)
    assert [r.type for r in rows] == ["runner_dead", "park_undecided", "state_stuck"]
    assert rows[1].style == "yellow"


def test_ping_text_with_and_without_a_ping(tmp_path):
    cfg = _cfg(tmp_path)
    cfg["agentic"]["events"] = {"ping_interval_s": 42}
    assert AM.ping_text([], cfg).plain == "no conductor_ping yet (interval 42s)"
    assert "interval 600s" in AM.ping_text([]).plain
    _ping(cfg, counts={"active": 1, "queued": 1, "ready": 1, "blocked": 0, "parked": 0,
                       "undecided_parks": 0, "stuck": 0, "waiting": 0})
    newest = _ping(cfg)
    rows = AM.load_events(cfg)
    text = AM.ping_text(rows, cfg).plain.splitlines()
    assert text[0] == ("active 2 · queued 5 · ready 3 · blocked 1 · parked 3 (undecided 1) · "
                       "stuck 1 · waiting 0")
    assert text[1:-1] == ["blocked SPEC-0002-b 25m", "stuck SPEC-0006-f 16m",
                          "ready_not_admitted SPEC-0007-g 2m", "parked SPEC-0003-c 6m"]
    assert text[-1].startswith("ping ") and text[-1].endswith("s ago")
    assert newest["id"] == rows[-1].id


def test_load_followers_flags(tmp_path):
    cfg = _cfg(tmp_path)
    for i in range(3):
        _append(cfg, "admitted", f"SPEC-000{i}-x")
    E.cursor_set(cfg, "dead", 0)
    E.cursor_set(cfg, "lagging", 1)
    E.cursor_set(cfg, "fresh", 1)
    E.cursor_set(cfg, "caught-up", 3)
    E.cursor_set(cfg, "mailbox:steward:X", 0)
    stale = (dt.datetime.now().astimezone() - dt.timedelta(seconds=300)).isoformat(
        timespec="seconds")
    for name in ("dead", "lagging", "caught-up"):         # cursors untouched for five minutes
        path = E.cursors_dir(cfg) / f"{name}.json"
        rec = json.loads(path.read_text())
        path.write_text(json.dumps({**rec, "ts": stale}))
    with E.reader_lock(cfg, "lagging"), E.reader_lock(cfg, "fresh"), \
            E.reader_lock(cfg, "caught-up"):
        by = {r.reader: r for r in AM.load_followers(cfg)}
    assert by["dead"].flag == "dead" and by["dead"].pid == "-" and not by["dead"].held
    assert by["dead"].lag == 3
    assert by["lagging"].flag == "lagging" and by["lagging"].pid == str(os.getpid())
    assert by["lagging"].lag == 2 and by["lagging"].cursor_age_s >= 300
    assert by["fresh"].flag == "" and by["fresh"].lag == 2          # lag, but cursor is recent
    assert by["caught-up"].flag == "" and by["caught-up"].lag == 0
    assert by["mailbox:steward:X"].kind == "mailbox" and by["mailbox:steward:X"].flag == "dead"


def test_stream_health_with_and_without_a_held_events_reader(tmp_path):
    cfg = _cfg(tmp_path)
    assert AM.stream_health(cfg, []).startswith("stream never synced")
    E.sync(cfg)                                    # the test, not the tab, writes the projector
    E.cursor_set(cfg, "conductor", 0)
    line = AM.stream_health(cfg, AM.load_followers(cfg))
    assert line.startswith("stream synced ") and line.count(" ago") == 1
    assert line.endswith(AM.NO_FOLLOWER_WARNING)
    old = time.time() - 300
    os.utime(E._projector_path(cfg), (old, old))
    with E.reader_lock(cfg, "conductor"):
        line = AM.stream_health(cfg, AM.load_followers(cfg))
    assert line == "stream synced 5m ago"
    with E.reader_lock(cfg, "mailbox:steward:X"):      # a mailbox follower is not the stream's
        E.cursor_set(cfg, "mailbox:steward:X", 0)
        assert AM.stream_health(cfg, [r for r in AM.load_followers(cfg)
                                      if r.kind == "mailbox"]).endswith(AM.NO_FOLLOWER_WARNING)


def test_events_signature_tracks_the_file(tmp_path):
    cfg = _cfg(tmp_path)
    assert AM.events_signature(cfg) is None
    _append(cfg, "admitted", "SPEC-0001-a")
    first = AM.events_signature(cfg)
    assert first[0] > 0
    _append(cfg, "admitted", "SPEC-0002-a")
    assert AM.events_signature(cfg)[0] > first[0]
