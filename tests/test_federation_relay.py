"""SPEC-0530: hub event relay, health, and read-only host views."""
from __future__ import annotations

import ast
import datetime as dt
import json
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from nightwright import report as RP
from nightwright.agentic import brief as BR
from nightwright.agentic import events as E
from nightwright.agentic import federation as F
from nightwright.agentic import federation_host as FH
from nightwright.agentic import hosts as H
from nightwright.agentic import queue as Q
from nightwright.agentic import quiet as QT
from nightwright.cli import cli

import waits
from test_agentic_e2e import make_cfg, make_repo, script
from test_federation_channel import (_add_and_enrol, _cfg, _invoke, _queue_import_violations, _raw,
                                     _serve)

pytestmark = pytest.mark.fast

NOW = dt.datetime(2026, 11, 10, 12, 0, 0, tzinfo=dt.timezone.utc)


def _iso(seconds_ago: float) -> str:
    return (NOW - dt.timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")


def _pair(tmp_path, monkeypatch, name="box"):
    hub, host = _cfg(tmp_path, "hub"), _cfg(tmp_path, "host")
    _add_and_enrol(host, hub, monkeypatch, name=name)
    srv, url = _serve(host)
    H.update(hub, lambda rs: H.find(rs, name).__setitem__("url", url))
    return hub, host, srv, url


def _host_lines(host, *objs, raw=()):
    p = E.events_path(host)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "ab") as f:
        for o in objs:
            f.write((json.dumps(o) + "\n").encode())
        for r in raw:
            f.write(r)


def _ev(i, type_="runner_dead", data=None, **kw):
    return {"schema": "nw.event.v1", "id": i, "ts": "2026-11-09T10:00:00+00:00", "type": type_,
            "campaign": kw.pop("campaign", None), "spec_id": None, "project": None, "by": "nw",
            "data": {"active": 1, "queued": 0} if data is None else data, **kw}


def _hub_events(hub):
    p = E.events_path(hub)
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _rec(hub, name="box"):
    return H.find(H.load(hub), name)


def _origin_ids(hub, name="box"):
    return [e["origin"]["id"] for e in _hub_events(hub) if (e.get("origin") or {}).get("host") == name]


def _add_record(cfg, name, **extra):
    rec = {"name": name, "url": "http://127.0.0.1:1", "token_sha256": "x", "secret_ref": "r",
           "enabled": True, "status_cache": None, **extra}
    H.update(cfg, lambda rs: rs.append(rec))


def _seed_cache(cfg, name, age_s, campaigns, now=NOW):
    cache = {"fetched_at": (now - dt.timedelta(seconds=age_s)).isoformat(timespec="seconds"),
             "projects": ["P"], "campaigns": campaigns}
    H.update(cfg, lambda rs: H.find(rs, name).__setitem__("status_cache", cache))


def _camp(cid, state, project="P"):
    return {"campaign": cid, "project": project, "state": state, "spec_id": "SPEC-9", "kind": "implement"}


def test_events_page_seeks_and_caps(tmp_path, monkeypatch):
    p = tmp_path / "events.jsonl"
    pad = b"".join(b'{"pad":%d}\n' % i for i in range(10000))
    tail = b"".join(b'{"id":%d}\n' % i for i in range(3))
    p.write_bytes(pad + tail)
    decoded = []
    real = json.loads
    monkeypatch.setattr(FH.json, "loads", lambda raw, *a, **k: (decoded.append(raw), real(raw))[1])
    evs, nxt = FH.read_events_page(p, len(pad), 500)
    monkeypatch.undo()
    assert len(decoded) == 3 and [e["id"] for e in evs] == [0, 1, 2]
    assert nxt == len(pad) + len(tail)
    # the first line alone is over the byte cap: still returned, alone
    big = b'{"x":"' + b"a" * F.EVENTS_MAX_BYTES + b'"}\n'
    p.write_bytes(big + b'{"id":1}\n')
    evs, nxt = FH.read_events_page(p, 0, 500)
    assert len(evs) == 1 and nxt == len(big)
    # the byte cap stops a later line; a trailing partial line is not consumed; junk is consumed
    line = b'{"v":"' + b"b" * 400000 + b'"}\n'
    p.write_bytes(line * 3 + b'{"id":1}\n')
    evs, nxt = FH.read_events_page(p, 0, 500)
    assert len(evs) == 2 and nxt == 2 * len(line)
    p.write_bytes(b'not json\n{"id":1}\n{"id":2')
    evs, nxt = FH.read_events_page(p, 0, 500)
    assert [e["id"] for e in evs] == [1] and nxt == len(b'not json\n{"id":1}\n')
    # the limit caps lines; an omitted or invalid limit means 500
    p.write_bytes(b"".join(b'{"id":%d}\n' % i for i in range(600)))
    assert len(FH.read_events_page(p, 0, 7)[0]) == 7
    assert FH._events_args("") == (0, 500)
    assert FH._events_args("offset=5&limit=0") == (5, 500)
    assert FH._events_args("limit=501")[1] == 500 and FH._events_args("limit=x")[1] == 500
    assert FH._events_args("limit=12")[1] == 12
    assert len(FH.read_events_page(p, 0, FH._events_args("limit=0")[1])[0]) == 500
    assert FH.read_events_page(tmp_path / "missing.jsonl", 0, 500) == ([], 0)


def test_events_http_route_is_signed_json_and_post_is_405(tmp_path, monkeypatch):
    hub, host, srv, url = _pair(tmp_path, monkeypatch)
    fed = F.read_federation(host)
    _host_lines(host, _ev(1))
    now = time.time()
    st, hdrs, raw = _raw("GET", url, "/v1/events", fed["token"], fed["secret"], now=now)
    assert st == 200 and hdrs["Content-Type"] == "application/json"
    doc = json.loads(raw)
    assert doc["schema"] == "nw.federation.events.v1" and doc["events"] == [_ev(1)]
    assert F.verify(fed["secret"], "GET", "/v1/events", raw, hdrs["X-NW-Sig"],
                    int(hdrs["X-NW-Sig-Ts"])) is None
    st, hdrs, raw = _raw("POST", url, "/v1/events", fed["token"], fed["secret"], now=now)
    assert st == 405 and json.loads(raw) == {"error": "method_not_allowed"}
    assert "X-NW-Sig" in hdrs
    srv.shutdown()


def test_events_bad_offset_is_409(tmp_path, monkeypatch):
    hub, host, srv, url = _pair(tmp_path, monkeypatch)
    fed = F.read_federation(host)
    now = time.time()

    def get(query):
        path = "/v1/events" + query
        st, hdrs, raw = _raw("GET", url, path, fed["token"], fed["secret"], now=now)
        return st, hdrs, raw, path

    st, hdrs, raw, _ = get("?offset=0")                       # a missing file is an empty page
    assert st == 200 and json.loads(raw)["events"] == [] and json.loads(raw)["next_offset"] == 0
    first = json.dumps(_ev(1)).encode() + b"\n"
    _host_lines(host, _ev(1))
    for q in ("?offset=3", f"?offset={len(first) + 1}", "?offset=-1", "?offset=abc", "?offset="):
        st, hdrs, raw, path = get(q)
        assert st == 409, q
        assert hdrs["Content-Type"] == "application/json" and json.loads(raw) == {"error": "bad_offset"}
        assert F.verify(fed["secret"], "GET", path, raw, hdrs["X-NW-Sig"], int(hdrs["X-NW-Sig-Ts"])) is None
    st, _, raw, _ = get(f"?offset={len(first)}")
    assert st == 200 and json.loads(raw) == {"schema": "nw.federation.events.v1", "events": [],
                                             "next_offset": len(first)}
    srv.shutdown()


def test_relay_tags_unknown_and_skips_message(tmp_path, monkeypatch):
    hub, host, srv, url = _pair(tmp_path, monkeypatch)
    _host_lines(host,
                _ev(1, campaign="C-1"), _ev(2, "widget", {"a": 1}), _ev(3, data={"queued": 0}),
                _ev(4, "message", {"msg_id": "m", "from": "x", "kind": "k", "text": "t"}),
                {"type": "runner_dead", "data": {}}, _ev(1, campaign="C-1"))
    paths = []
    real = F.HubClient.request

    def spy(self, method, path, *a, **k):
        paths.append(path)
        return real(self, method, path, *a, **k)

    monkeypatch.setattr(F.HubClient, "request", spy)
    F.relay_tick(hub, now=NOW)
    got = [e for e in _hub_events(hub) if e.get("host")]
    assert [e["type"] for e in got] == ["runner_dead", "host_event", "host_event"]
    first = got[0]
    assert list(first) == ["schema", "id", "ts", "type", "campaign", "spec_id", "project", "by",
                           "data", "host", "origin"]
    assert first["campaign"] == "C-1@box" and first["host"] == "box"
    assert first["origin"] == {"host": "box", "id": 1} and first["data"] == {"active": 1, "queued": 0}
    assert first["ts"] == "2026-11-09T10:00:00+00:00" and first["id"] == 1
    assert got[1]["data"] == {"a": 1, "type": "widget"} and got[1]["origin"]["id"] == 2
    assert got[2]["data"] == {"queued": 0, "type": "runner_dead"}
    assert not [e for e in _hub_events(hub) if e["type"] == "message"]
    assert _origin_ids(hub) == [1, 2, 3]
    cur = _rec(hub)["cursor"]
    assert cur["events_origin_id"] == 4 and cur["events_offset"] > 0
    assert "/v1/events?offset=0&limit=500" in paths
    F.relay_tick(hub, now=NOW)
    assert _origin_ids(hub) == [1, 2, 3]
    assert f"/v1/events?offset={cur['events_offset']}&limit=500" in paths
    assert "mail_seq" not in _rec(hub) and "hub_events_offset" not in _rec(hub)
    assert not [e for e in _hub_events(hub) if e["type"] == "host_mail_rejected"]
    srv.shutdown()


def test_fsync_failure_retries_once(tmp_path, monkeypatch):
    hub, host, srv, url = _pair(tmp_path, monkeypatch)
    F.relay_tick(hub, now=NOW)                                 # initialize the cursor
    before = _rec(hub)["cursor"]
    _host_lines(host, _ev(1), _ev(2))
    real, calls = F._fsync_events, []

    def flaky(cfg):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk")
        real(cfg)

    monkeypatch.setattr(F, "_fsync_events", flaky)
    F.relay_tick(hub, now=NOW)
    assert _rec(hub)["cursor"] == before
    F.relay_tick(hub, now=NOW)
    assert _origin_ids(hub) == [1, 2]
    assert _rec(hub)["cursor"]["events_origin_id"] == 2 and len(calls) == 2
    srv.shutdown()


def test_crash_before_checkpoint_rescans_tail(tmp_path, monkeypatch):
    hub, host, srv, url = _pair(tmp_path, monkeypatch)
    for i in range(1000):
        E.append(hub, "note", data={"text": f"pad{i}"})
    F.relay_tick(hub, now=NOW)                                 # initialize past the padding
    start = _rec(hub)["cursor"]
    assert start["hub_events_offset"] == E.events_path(hub).stat().st_size
    _host_lines(host, _ev(1), _ev(2))
    real, raised = H.update, []

    def once(cfg, fn):
        if not raised:
            raised.append(1)
            raise OSError("crash before the checkpoint")
        return real(cfg, fn)

    monkeypatch.setattr(H, "update", once)
    F.relay_tick(hub, now=NOW)
    monkeypatch.setattr(H, "update", real)
    assert _origin_ids(hub) == [1, 2] and _rec(hub)["cursor"] == start
    tails, scan = [], F._scan_tail
    monkeypatch.setattr(F, "_scan_tail", lambda cfg, off: tails.append(scan(cfg, off)) or tails[-1])
    F.relay_tick(hub, now=NOW)
    assert _origin_ids(hub) == [1, 2]
    assert tails and all("pad" not in json.dumps(ev) for t in tails for ev in t)
    assert _rec(hub)["cursor"]["events_origin_id"] == 2
    srv.shutdown()


def test_409_resets_once_and_skips_known_ids(tmp_path, monkeypatch):
    hub, host, srv, url = _pair(tmp_path, monkeypatch)
    _host_lines(host, _ev(1), _ev(2), _ev(3))
    F.relay_tick(hub, now=NOW)
    assert _origin_ids(hub) == [1, 2, 3]
    H.update(hub, lambda rs: H.find(rs, "box")["cursor"].update(events_offset=5))   # mid-line
    F.relay_tick(hub, now=NOW)
    resets = [e for e in _hub_events(hub) if e["type"] == "host_cursor_reset"]
    assert len(resets) == 1 and resets[0]["data"] == {"host": "box"} and resets[0]["host"] == "box"
    assert resets[0]["by"] == "federation" and "origin" not in resets[0]
    cur = _rec(hub)["cursor"]
    assert cur["events_offset"] == 0 and cur["events_origin_id"] == 3
    _host_lines(host, _ev(4))
    F.relay_tick(hub, now=NOW)
    assert _origin_ids(hub) == [1, 2, 3, 4]
    assert _rec(hub)["cursor"]["events_offset"] > 0
    # a crash between the reset append and its checkpoint does not append a second reset
    H.update(hub, lambda rs: H.find(rs, "box")["cursor"].update(events_offset=5))
    real, raised = H.update, []

    def once(cfg, fn):
        if not raised:
            raised.append(1)
            raise OSError("crash")
        return real(cfg, fn)

    monkeypatch.setattr(H, "update", once)
    F.relay_tick(hub, now=NOW)
    monkeypatch.setattr(H, "update", real)
    assert len([e for e in _hub_events(hub) if e["type"] == "host_cursor_reset"]) == 2
    F.relay_tick(hub, now=NOW)
    assert len([e for e in _hub_events(hub) if e["type"] == "host_cursor_reset"]) == 2
    assert _rec(hub)["cursor"]["events_offset"] == 0
    # an unsigned events response moves nothing and appends nothing
    F.relay_tick(hub, now=NOW)
    cur = _rec(hub)["cursor"]
    n = len(_hub_events(hub))
    _host_lines(host, _ev(5))
    monkeypatch.setattr(F, "response_headers", lambda *a, **k: {"X-NW-Sig-Ts": "1", "X-NW-Sig": "bad"})
    F.relay_tick(hub, now=NOW)
    assert _rec(hub)["cursor"] == cur and len(_hub_events(hub)) == n
    srv.shutdown()


def _three_hosts(hub, disabled=()):
    for n in ("a", "b", "c", *disabled):
        _add_record(hub, n, enabled=n not in disabled)


def test_pool_overlaps_and_drops_late_results(tmp_path, monkeypatch):
    assert F.POOL_WAIT_S == 2.5
    hub = _cfg(tmp_path, "hub")
    _three_hosts(hub, disabled=("off",))
    starts, ends, applied = {}, {}, []
    called = []
    slow_release, slow_done = threading.Event(), threading.Event()
    page = {"schema": "nw.federation.events.v1", "events": [_ev(1)], "next_offset": 10}

    def fetch_events(rec, cfg, offset):
        n = rec["name"]
        called.append(n)
        starts[n] = time.monotonic()
        if n == "c" and slow:
            slow_release.wait(5)
            slow_done.set()
            return "page", page
        if not slow:
            time.sleep(0.2)
        ends[n] = time.monotonic()
        return "page", page

    def fetch_status(rec, cfg):
        if rec["name"] == "c" and slow:
            slow_release.wait(5)
        return "ok", {"projects": ["P"], "campaigns": [_camp("C", "RUNNING")]}

    real_apply = F._apply_page

    def apply(cfg, name, cursor, doc, now):
        applied.append((name, threading.current_thread() is threading.main_thread(),
                        time.monotonic()))
        return real_apply(cfg, name, cursor, doc, now)

    monkeypatch.setattr(F, "_fetch_events", fetch_events)
    monkeypatch.setattr(F, "_fetch_status", fetch_status)
    monkeypatch.setattr(F, "_apply_page", apply)
    seen = []
    real_wait = F.cf.wait
    monkeypatch.setattr(F.cf, "wait", lambda fs, timeout=None, **k: (seen.append(timeout),
                                                                    real_wait(fs, timeout=timeout, **k))[1])
    slow = False
    F.relay_tick(hub, now=NOW)
    assert seen == [F.POOL_WAIT_S] and sorted(called) == ["a", "b", "c"]
    assert max(starts.values()) - min(starts.values()) < 0.15           # the fetches overlap
    assert [a[0] for a in applied] == ["a", "b", "c"] and all(a[1] for a in applied)
    assert min(a[2] for a in applied) >= max(ends.values())
    assert "cursor" not in _rec(hub, "off") and not _rec(hub, "off")["status_cache"]
    assert _rec(hub, "a")["status_cache"]["campaigns"] == [_camp("C", "RUNNING")]
    # a late call is dropped, and applied by nothing when it finishes
    monkeypatch.setattr(F, "POOL_WAIT_S", 0.05)
    H.update(hub, lambda rs: [H.find(rs, n).pop("status_cache") for n in "abc"])
    before = _rec(hub, "c")["cursor"]
    slow = True
    applied.clear()
    t0 = time.monotonic()
    F.relay_tick(hub, now=NOW)
    assert time.monotonic() - t0 < 2 and not slow_done.is_set()
    assert [a[0] for a in applied] == ["a", "b"]
    assert _rec(hub, "c")["cursor"] == before and "status_cache" not in _rec(hub, "c")
    slow_release.set()
    assert waits.until(slow_done.is_set)
    time.sleep(0.1)
    assert _rec(hub, "c")["cursor"] == before and [a[0] for a in applied] == ["a", "b"]


def test_dropped_status_leaves_cache_and_bad_status_bodies_fail(tmp_path, monkeypatch):
    hub, host, srv, url = _pair(tmp_path, monkeypatch)
    F.relay_tick(hub, now=NOW)
    cache = _rec(hub)["status_cache"]
    assert cache["fetched_at"] == NOW.isoformat(timespec="seconds") and "campaigns" in cache
    later = NOW + dt.timedelta(seconds=30)
    monkeypatch.setattr(FH, "status_document", lambda cfg: [])
    F.relay_tick(hub, now=later)
    assert _rec(hub)["status_cache"] == cache
    monkeypatch.setattr(FH, "status_document", lambda cfg: {"projects": []})
    F.relay_tick(hub, now=later)
    assert _rec(hub)["status_cache"] == cache

    def boom(cfg):
        raise RuntimeError("x")

    monkeypatch.setattr(FH, "status_document", boom)
    F.relay_tick(hub, now=later)
    assert _rec(hub)["status_cache"] == cache
    assert "HTTP 500" in _rec(hub)["health"]["last_error"]
    srv.shutdown()


def test_off_stops_the_relay_and_on_resumes_it(tmp_path, monkeypatch):
    hub, host, srv, url = _pair(tmp_path, monkeypatch)
    F.relay_tick(hub, now=NOW)
    r = _invoke(hub, monkeypatch, "off", "box")
    assert r.exit_code == 0, r.output
    _host_lines(host, _ev(1))
    F.relay_tick(hub, now=NOW)
    assert _origin_ids(hub) == []
    assert _invoke(hub, monkeypatch, "on", "box").exit_code == 0
    F.relay_tick(hub, now=NOW)
    assert _origin_ids(hub) == [1]
    srv.shutdown()


def test_queue_tick_calls_relay_once(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    calls = []
    monkeypatch.setattr(F, "relay_tick", lambda cfg, **k: calls.append(cfg))
    Q.tick(cfg)
    assert len(calls) == 1
    monkeypatch.setattr(F, "relay_tick", lambda cfg, **k: (_ for _ in ()).throw(RuntimeError("no")))
    assert Q.tick(cfg) is not None
    assert not H.path(cfg).exists()


def test_unreachable_once_across_calls(tmp_path, monkeypatch):
    hub = _cfg(tmp_path, "hub")
    _add_record(hub, "box", url="http://127.0.0.1:1")       # nothing listens: every call fails
    _add_record(hub, "off", enabled=False)
    H.update(hub, lambda rs: H.find(rs, "off").__setitem__(
        "health", {"last_ok": _iso(5000), "last_error": None, "unreachable_reported": False}))

    def set_health(**kw):
        H.update(hub, lambda rs: H.find(rs, "box")["health"].update(kw))

    F.relay_tick(hub, now=NOW)                               # initializes; last_ok is null
    assert _rec(hub)["health"]["last_ok"] is None and _rec(hub)["cursor"]["events_offset"] == 0
    assert not [e for e in _hub_events(hub) if e["type"].startswith("host_")]
    set_health(last_ok=_iso(599))
    F.relay_tick(hub, now=NOW)
    assert not [e for e in _hub_events(hub) if e["type"] == "host_unreachable"]
    assert _rec(hub)["health"]["last_ok"] == _iso(599) and "last_error" in _rec(hub)["health"]
    set_health(last_ok=_iso(600))
    F.relay_tick(hub, now=NOW)
    ev = [e for e in _hub_events(hub) if e["type"] == "host_unreachable"]
    assert len(ev) == 1 and ev[0]["data"] == {"host": "box", "since": _iso(600)}
    assert ev[0]["host"] == "box" and ev[0]["by"] == "federation" and "origin" not in ev[0]
    assert _rec(hub)["health"]["unreachable_reported"] is True
    F.relay_tick(hub, now=NOW)
    assert len([e for e in _hub_events(hub) if e["type"] == "host_unreachable"]) == 1
    assert not [e for e in _hub_events(hub) if e["data"].get("host") == "off"]
    assert "cursor" not in _rec(hub, "off")
    # the host comes back: one host_reachable, flag cleared
    (tmp_path / "x").mkdir()
    hub2, host2, srv, url = _pair(tmp_path / "x", monkeypatch, name="back")
    H.update(hub2, lambda rs: H.find(rs, "back").__setitem__("health", {
        "last_ok": _iso(900), "last_error": None, "unreachable_reported": True}))
    F.relay_tick(hub2, now=NOW)
    reach = [e for e in _hub_events(hub2) if e["type"] == "host_reachable"]
    assert len(reach) == 1 and reach[0]["data"] == {"host": "back"}
    assert _rec(hub2, "back")["health"] == {"last_ok": NOW.isoformat(timespec="seconds"),
                                            "last_error": None, "unreachable_reported": False}
    F.relay_tick(hub2, now=NOW)
    assert len([e for e in _hub_events(hub2) if e["type"] == "host_reachable"]) == 1
    srv.shutdown()


def test_event_types_and_quiet_rules(tmp_path):
    want = {"host_event": ("type",), "host_unreachable": ("host", "since"),
            "host_reachable": ("host",), "host_cursor_reset": ("host",),
            "host_mail_rejected": ("host", "msg_uid", "to", "reason")}
    for t, keys in want.items():
        assert E.EVENT_TYPES[t] == keys and QT.RULES[t] == "wake"
    held = {t for t, v in QT.RULES.items() if v == "hold"}
    cond = {t for t, v in QT.RULES.items() if v == "cond"}
    assert not set(want) & (held | cond)
    skill = (Path(__file__).resolve().parents[1] / "skills" / "nw-conduct" / "SKILL.md").read_text()
    assert not any(t in skill for t in want)
    cfg = _cfg(tmp_path)
    rec = E.append(cfg, "note", data={"text": "x"})
    assert "host" not in rec and "origin" not in rec


def _status_cfg(tmp_path):
    return _cfg(tmp_path, "hub")


def _run_status(cfg, monkeypatch, *args):
    monkeypatch.setenv("NW_DATA_DIR", cfg["data_dir"])
    monkeypatch.setenv("NW_CONFIG_DIR", cfg["config_dir"])
    return CliRunner().invoke(cli, ["queue", "status", *args])


def test_status_all_hosts_filters(tmp_path, monkeypatch):
    hub = _status_cfg(tmp_path)
    plain = _run_status(hub, monkeypatch, "--json")
    assert plain.exit_code == 0, plain.output
    assert json.loads(plain.output) == Q.queue_payload(hub)
    assert not any("host" in i or "stale" in i for i in json.loads(plain.output)["items"])
    empty = _run_status(hub, monkeypatch, "--all-hosts", "--json")
    assert json.loads(empty.output) == json.loads(plain.output)
    _add_record(hub, "box")
    camps = [_camp("C1", "RUNNING"), _camp("C2", "RUNNING"), _camp("C3", "PARKED"),
             _camp("C4", "RUNNING", project="Q")]
    now = dt.datetime.now().astimezone()
    _seed_cache(hub, "box", 600, camps, now=now)
    assert json.loads(_run_status(hub, monkeypatch, "--json").output) == json.loads(plain.output)
    out = json.loads(_run_status(hub, monkeypatch, "--all-hosts", "--json").output)
    assert out["schema"] == "nw.queue.v1"
    rows = [i for i in out["items"] if i.get("host")]
    assert [r["campaign"] for r in rows] == ["C1", "C2", "C3", "C4"]
    assert all(r["stale"] is False and r["host"] == "box" for r in rows)
    assert rows[0] == {"campaign": "C1", "project": "P", "state": "RUNNING", "spec_id": "SPEC-9",
                       "kind": "implement", "host": "box", "stale": False}
    assert all(i["host"] is None and i["stale"] is False for i in out["items"] if not i.get("host"))
    by_p = json.loads(_run_status(hub, monkeypatch, "--all-hosts", "--json", "--project", "P").output)
    assert [r["campaign"] for r in by_p["items"]] == ["C1", "C2", "C3"]
    by_s = json.loads(_run_status(hub, monkeypatch, "--all-hosts", "--json", "--state", "PARKED").output)
    assert [r["campaign"] for r in by_s["items"]] == ["C3"]
    _seed_cache(hub, "box", 590, camps, now=now)
    text = _run_status(hub, monkeypatch, "--all-hosts").output
    assert "box" in text and "stale" not in text
    _seed_cache(hub, "box", 640, camps, now=now)
    out = json.loads(_run_status(hub, monkeypatch, "--all-hosts", "--json").output)
    assert all(r["stale"] is True for r in out["items"])
    text = _run_status(hub, monkeypatch, "--all-hosts").output
    assert "box" in text and "stale" in text
    src = (Path(__file__).resolve().parents[1] / "src/nightwright/agentic/cli_queue.py").read_text()
    assert '_col("host", "host", fixed=True)' in src


def test_view_boundaries_and_lines(tmp_path):
    hub = _cfg(tmp_path, "hub")
    _add_record(hub, "box")
    camps = [_camp("C1", "RUNNING"), _camp("C2", "RUNNING"), _camp("C3", "PARKED"), "junk",
             {"campaign": "X", "state": 3}]
    _seed_cache(hub, "box", 600, camps)
    v = F.host_views(hub, now=NOW)[0]
    assert v["stale"] is False and v["last_contact_s"] == 600 and v["n"] == 3
    _seed_cache(hub, "box", 601, camps)
    assert F.host_views(hub, now=NOW)[0]["stale"] is True
    _seed_cache(hub, "box", 90, camps)
    v = F.host_views(hub, now=NOW)[0]
    assert v == {"host": "box", "enabled": True, "stale": False, "fetched_at": _iso(90),
                 "last_contact_s": 90, "n": 3, "counts": {"PARKED": 1, "RUNNING": 2},
                 "line": "host box: last contact 90s, 3 campaigns (PARKED: 1, RUNNING: 2)"}
    assert F.host_views(hub, now=NOW, state="PARKED")[0]["line"] == \
        "host box: last contact 90s, 1 campaigns (PARKED: 1)"
    assert F.host_views(hub, now=NOW, project="nope")[0]["line"] == \
        "host box: last contact 90s, 0 campaigns"
    _add_record(hub, "new")
    never = F.host_views(hub, now=NOW)[1]
    assert never["line"] == "host new: last contact never, 0 campaigns, stale"
    assert never["last_contact_s"] is None and never["stale"] is True


def test_brief_and_hub_lines(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    assert "federation_hosts" not in BR.conductor_brief(cfg, now=NOW)
    assert "federation_hosts" not in RP.hub_payload(cfg)
    assert "hosts:" not in BR.render_text(BR.conductor_brief(cfg, now=NOW))
    _add_record(cfg, "box")
    camps = [_camp("C1", "RUNNING"), _camp("C2", "RUNNING"), _camp("C3", "PARKED")]
    _seed_cache(cfg, "box", 90, camps)
    socks = []
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: socks.append(a))
    brief = BR.conductor_brief(cfg, now=NOW)
    views = F.host_views(cfg, now=NOW)
    assert brief["federation_hosts"] == views
    line = "host box: last contact 90s, 3 campaigns (PARKED: 1, RUNNING: 2)"
    assert views[0]["line"] == line
    text = BR.render_text(brief)
    assert text.index("counts:") < text.index("hosts:\n- " + line)
    hub = RP.hub_payload(cfg)
    assert hub["federation_hosts"][0]["host"] == "box" and "line" in hub["federation_hosts"][0]
    assert RP.hub_payload(cfg, state="INCUBATE")["federation_hosts"] == hub["federation_hosts"]
    _seed_cache(cfg, "box", 700, camps)
    assert BR.conductor_brief(cfg, now=NOW)["federation_hosts"][0]["line"].endswith(", stale")
    assert not socks


def test_inert_and_no_queue_import(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    F.relay_tick(cfg, now=NOW)
    Q.tick(cfg)
    assert not H.path(cfg).exists()
    root = Path(__file__).resolve().parents[1] / "src" / "nightwright" / "agentic"
    for name in ("hosts.py", "federation.py", "cli_hosts.py"):
        tree = ast.parse((root / name).read_text())
        assert not _queue_import_violations((root / name).read_text()), name
        names = {a.name.split(".")[-1] for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                 for a in n.names} | {n.module.split(".")[-1] for n in ast.walk(tree)
                                      if isinstance(n, ast.ImportFrom) and n.module}
        assert "queue" not in names, name
    (tmp_path / "p").mkdir()
    hub, host, srv, url = _pair(tmp_path / "p", monkeypatch)
    F.relay_tick(hub, now=NOW)
    rec = _rec(hub)
    assert "cursor" in rec and "health" in rec
    assert "mail_seq" not in rec and "hub_inbox_offset" not in rec["cursor"]
    assert set(rec["cursor"]) == {"events_offset", "events_origin_id", "hub_events_offset"}
    srv.shutdown()
