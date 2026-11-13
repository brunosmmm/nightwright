"""SPEC-0532: exactly-once mail between hub and host."""
from __future__ import annotations

import ast
import inspect
import json
import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from nightwright.agentic import events as E
from nightwright.agentic import federation as F
from nightwright.agentic import federation_host as FH
from nightwright.agentic import hosts as H
from nightwright.agentic import mailbox as M
from nightwright.agentic import queue as Q
from nightwright.agentic import store
from nightwright.cli import cli

import waits
from test_agentic_e2e import make_cfg, make_repo, script
from test_federation_channel import (_add_and_enrol, _cfg, _queue_import_violations, _raw,
                                     _serve)

pytestmark = pytest.mark.fast

UID = "a" * 32


def _mk(tmp_path, name):
    cfg = _cfg(tmp_path, name)
    (Path(cfg["config_dir"]) / "mappings.yaml").write_text("{}\n")
    cfg["outbox_targets"] = {"Demo": {"repo_path": str(tmp_path)}}
    cfg["project_axis"] = "bucket"
    return cfg


def _pair(tmp_path, monkeypatch, name="box", hub=None):
    hub = hub or _mk(tmp_path, "hub")
    host = _mk(tmp_path, f"host-{name}")
    _add_and_enrol(host, hub, monkeypatch, name=name)
    srv, url = _serve(host)
    H.update(hub, lambda rs: H.find(rs, name).__setitem__("url", url))
    return hub, host, srv, url


def _ready(hub, name="box"):
    """The event relay creates the cursor; `relay_mail` adds its three keys on its first pass."""
    F.relay_tick(hub)
    F.relay_mail(hub)
    return H.find(H.load(hub), name)


def _cursor(hub, name="box"):
    return H.find(H.load(hub), name)["cursor"]


def _lines(path):
    p = Path(path)
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _outbox(hub, name="box"):
    return _lines(F.outbox_path(hub, name))


def _acks(hub, name="box"):
    return [o for o in _outbox(hub, name) if "result" in o]


def _inbox(cfg, addr):
    return _lines(M._inbox_path(cfg, addr))


def _hub_events(hub, type_=None):
    evs = _lines(E.events_path(hub))
    return [e for e in evs if type_ is None or e.get("type") == type_]


def _send(hub, text="hi", to="human@box", **kw):
    return F.enqueue_remote(hub, to=to, frm="conductor", text=text, kind=kw.pop("kind", "info"),
                            campaign=None, re=None, needs_reply=None, **kw)


def _raw_outbox(hub, name="box", **fields):
    rec = {"msg_uid": UID, "to": "human", "from": "conductor", "kind": "info", "text": "t",
           "campaign": None, "re": None, "needs_reply": False, **fields}
    p = F.outbox_path(hub, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "ab") as f:
        f.write((json.dumps(rec) + "\n").encode())
    return rec


class _Spy:
    """Wrap `HubClient.request`; `post` / `get` may replace or follow the real call."""

    def __init__(self, monkeypatch, post=None, get=None):
        self.calls = []
        real = F.HubClient.request

        def request(client, method, path, body=b"", allow=(200,)):
            self.calls.append((method, path, body))
            fn = post if method == "POST" else get
            if fn is not None:
                return fn(lambda: real(client, method, path, body, allow), body)
            return real(client, method, path, body, allow)

        monkeypatch.setattr(F.HubClient, "request", request)

    def posts(self):
        return [json.loads(b)["messages"] for m, _, b in self.calls if m == "POST"]


def _stub(status, doc):
    raw = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
    return lambda real, body: (status, raw, {})


def _boom(msg="bad signature"):
    def fn(real, body):
        raise ConnectionError(msg)
    return fn


def _cli(hub, monkeypatch, *args):
    monkeypatch.setenv("NW_DATA_DIR", hub["data_dir"])
    monkeypatch.setenv("NW_CONFIG_DIR", hub["config_dir"])
    return CliRunner().invoke(cli, ["mailbox", "send", *args])


def _send_many(cfg, n):
    for i in range(n):
        _send(cfg, f"m{i}")


def test_concurrent_sends_have_distinct_uids(tmp_path, monkeypatch):
    hub, host, srv, _ = _pair(tmp_path, monkeypatch)
    _ready(hub)
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_send_many, args=(hub, 20)) for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(waits.WAIT_S)
        assert p.exitcode == 0
    lines = _outbox(hub)
    uids = [o["msg_uid"] for o in lines]
    assert len(lines) == 40 and len(set(uids)) == 40
    assert all(len(u) == 32 and u == u.lower() and int(u, 16) >= 0 for u in uids)
    F.relay_mail(hub)
    got = _inbox(host, "human")
    assert sorted(m["msg_uid"] for m in got) == sorted(uids)
    assert len(_acks(hub)) == 40 and F.pending_mail(hub, "box") == []
    # the outbox line is on disk before the fsync, and a failing fsync fails the send
    seen = []
    real = F._fsync_fd
    monkeypatch.setattr(F, "_fsync_fd", lambda fd: (seen.append(len(_outbox(hub))), real(fd))[1])
    r = _cli(hub, monkeypatch, "--to", "human@box", "--from", "conductor", "--json", "j")
    assert r.exit_code == 0, r.output
    assert seen == [81] and json.loads(r.output) == _outbox(hub)[-1]
    r = _cli(hub, monkeypatch, "--to", "human@box", "--from", "conductor", "text")
    assert r.exit_code == 0 and r.output.startswith("queued " + _outbox(hub)[-1]["msg_uid"])
    assert r.output.strip().endswith("→ human@box")

    def raising(fd):
        raise OSError("disk")
    monkeypatch.setattr(F, "_fsync_fd", raising)
    assert _cli(hub, monkeypatch, "--to", "human@box", "--from", "conductor", "x").exit_code != 0
    srv.shutdown()


def test_acks_stop_retry_for_each_result(tmp_path, monkeypatch):
    hub, host, srv, _ = _pair(tmp_path, monkeypatch)
    _ready(hub)
    a, b, c = (_send(hub, t) for t in "abc")

    def results(real, body):
        doc = {"schema": "nw.federation.mail.v1", "results": [
            {"msg_uid": a["msg_uid"], "result": "delivered", "seq": 1},
            {"msg_uid": b["msg_uid"], "result": "dup", "seq": 2},
            {"msg_uid": c["msg_uid"], "result": "rejected", "reason": "bad_address"}]}
        return 200, json.dumps(doc).encode(), {}

    spy = _Spy(monkeypatch, post=results)
    F.relay_mail(hub)
    acks = {x["msg_uid"]: x for x in _acks(hub)}
    assert acks[a["msg_uid"]] == {"msg_uid": a["msg_uid"], "result": "delivered", "seq": 1}
    assert acks[b["msg_uid"]] == {"msg_uid": b["msg_uid"], "result": "dup", "seq": 2}
    assert acks[c["msg_uid"]] == {"msg_uid": c["msg_uid"], "result": "rejected",
                                  "reason": "bad_address"}
    assert len(_hub_events(hub, "host_mail_rejected")) == 1
    d = _send(hub, "d")
    F.relay_mail(hub)
    bodies = spy.posts()
    assert len(bodies) == 2 and [m["msg_uid"] for m in bodies[1]] == [d["msg_uid"]]
    srv.shutdown()


def _commit_then_raise(real, body):
    real()
    raise ConnectionError("response lost")


def test_lost_response_acks_dup(tmp_path, monkeypatch):
    hub, host, srv, _ = _pair(tmp_path, monkeypatch)
    _ready(hub)
    line = _send(hub)
    with monkeypatch.context() as mp:
        _Spy(mp, post=_commit_then_raise)
        F.relay_mail(hub)
    assert _acks(hub) == [] and len(_inbox(host, "human")) == 1
    F.relay_mail(hub)
    assert len(_inbox(host, "human")) == 1
    ack = _acks(hub)[0]
    assert ack["msg_uid"] == line["msg_uid"] and ack["result"] == "dup" and ack["seq"] == 1
    srv.shutdown()


def test_lost_response_without_dedupe_would_duplicate(tmp_path, monkeypatch):
    hub, host, srv, _ = _pair(tmp_path, monkeypatch)
    _ready(hub)
    _send(hub)
    with monkeypatch.context() as mp:
        _Spy(mp, post=_commit_then_raise)
        F.relay_mail(hub)
    monkeypatch.setattr(FH, "_read_received", lambda path: [])
    monkeypatch.setattr(FH, "_find_uid", lambda *a: None)
    F.relay_mail(hub)
    assert len(_inbox(host, "human")) == 2
    srv.shutdown()


def test_fsync_500_retries_into_dup(tmp_path, monkeypatch):
    hub, host, srv, _ = _pair(tmp_path, monkeypatch)
    _ready(hub)
    _send(hub, "one")
    real = FH._fsync_fd
    fails = [1]

    def once(fd):
        if fails:
            fails.pop()
            raise OSError("fsync")
        real(fd)

    spy = _Spy(monkeypatch, post=lambda real_, body: _seen(real_))
    seen = []

    def _seen(real_):
        r = real_()
        seen.append((r[0], json.loads(r[1])))
        return r

    monkeypatch.setattr(FH, "_fsync_fd", once)
    F.relay_mail(hub)
    assert seen == [(500, {"error": "commit_failed"})]
    assert len(_inbox(host, "human")) == 1 and _acks(hub) == []   # appended, never acked
    F.relay_mail(hub)
    assert [a["result"] for a in _acks(hub)] == ["dup"] and len(_inbox(host, "human")) == 1
    assert len(spy.posts()) == 2
    # a 500 before any append retries as `delivered`
    _send(hub, "two")
    boom = [1]
    real_read = FH._read_received

    def read_once(path):
        if boom:
            boom.pop()
            raise OSError("no")
        return real_read(path)

    monkeypatch.setattr(FH, "_read_received", read_once)
    F.relay_mail(hub)
    assert len(_inbox(host, "human")) == 1
    F.relay_mail(hub)
    assert [a["result"] for a in _acks(hub)] == ["dup", "delivered"]
    assert len(_inbox(host, "human")) == 2
    srv.shutdown()


def test_verified_4xx_is_terminal(tmp_path, monkeypatch):
    hub, host, srv, url = _pair(tmp_path, monkeypatch)
    fed = F.read_federation(host)
    # the real host: a body that is not {"messages": [...]} is a signed 400 and writes nothing
    st, hdrs, raw = _raw("POST", url, "/v1/mail", fed["token"], fed["secret"],
                         body=b'{"messages": 5}', now=time.time())
    assert st == 400 and json.loads(raw) == {"error": "bad_request"} and "X-NW-Sig" in hdrs
    assert not M._inbox_path(host, "human").exists()
    _ready(hub)
    a, b = _send(hub, "a"), _send(hub, "b")
    with monkeypatch.context() as mp:
        _Spy(mp, post=_stub(400, {"error": "bad_request"}))
        F.relay_mail(hub)
    assert [(x["result"], x["reason"]) for x in _acks(hub)] == [("rejected", "bad_request")] * 2
    assert len(_hub_events(hub, "host_mail_rejected")) == 2
    with monkeypatch.context() as mp:
        spy = _Spy(mp)
        F.relay_mail(hub)
    assert spy.posts() == [] and len(_hub_events(hub, "host_mail_rejected")) == 2
    assert not M._inbox_path(host, "human").exists()
    c = _send(hub, "c")
    with monkeypatch.context() as mp:
        _Spy(mp, post=_stub(404, b""))
        F.relay_mail(hub)
    assert [x["reason"] for x in _acks(hub)][-1] == "http_404"
    d = _send(hub, "d")
    with monkeypatch.context() as mp:                       # an unsigned response acks nothing
        spy = _Spy(mp, post=_boom("missing response signature"))
        F.relay_mail(hub)
    assert all(x["msg_uid"] != d["msg_uid"] for x in _acks(hub))
    with monkeypatch.context() as mp:
        spy = _Spy(mp)
        F.relay_mail(hub)
    assert [m["msg_uid"] for m in spy.posts()[0]] == [d["msg_uid"]]
    assert {a["msg_uid"], b["msg_uid"], c["msg_uid"]} <= {x["msg_uid"] for x in _acks(hub)}
    srv.shutdown()


def test_rejected_address_is_acked_once(tmp_path, monkeypatch):
    hub, host, srv, _ = _pair(tmp_path, monkeypatch)
    _ready(hub)
    _raw_outbox(hub, msg_uid="1" * 32, to="steward:Missing")
    _raw_outbox(hub, msg_uid="2" * 32, kind="nope")
    _raw_outbox(hub, msg_uid="3" * 32, text=" ")
    _raw_outbox(hub, msg_uid="abc")
    F.relay_mail(hub)
    got = {a["msg_uid"]: a["reason"] for a in _acks(hub)}
    assert got == {"1" * 32: "bad_address", "2" * 32: "bad_message", "3" * 32: "bad_message",
                   "abc": "bad_msg_uid"}
    assert not M._inbox_path(host, "human").exists()
    evs = _hub_events(hub, "host_mail_rejected")
    assert sorted(e["data"]["msg_uid"] for e in evs) == sorted(got)
    assert all(e["host"] == "box" and e["campaign"] is None and "origin" not in e
               and e["by"] == "federation" for e in evs)
    with monkeypatch.context() as mp:
        spy = _Spy(mp)
        F.relay_mail(hub)
        F.relay_mail(hub)
    assert spy.posts() == [] and len(_hub_events(hub, "host_mail_rejected")) == 4
    srv.shutdown()


def test_mail_page_seeks_and_caps(tmp_path, monkeypatch):
    p = tmp_path / "conductor.jsonl"
    pad = b"".join(b'{"pad":%d}\n' % i for i in range(10000))
    tail = b"".join(b'{"id":%d}\n' % i for i in range(3))
    p.write_bytes(pad + tail)
    decoded = []
    real = json.loads
    with monkeypatch.context() as mp:
        mp.setattr(FH.json, "loads", lambda raw, *a, **k: (decoded.append(raw), real(raw))[1])
        msgs, nxt = FH.read_mail_page(p, len(pad), 200)
    assert len(decoded) == 3 and [m["id"] for m in msgs] == [0, 1, 2]
    assert nxt == len(pad) + len(tail)
    big = b'{"x":"' + b"a" * F.MAIL_MAX_BYTES + b'"}\n'
    p.write_bytes(big + b'{"id":1}\n')
    msgs, nxt = FH.read_mail_page(p, 0, 200)
    assert len(msgs) == 1 and nxt == len(big)
    line = b'{"v":"' + b"b" * 400000 + b'"}\n'
    p.write_bytes(line * 3 + b'{"id":1}\n')
    msgs, nxt = FH.read_mail_page(p, 0, 200)
    assert len(msgs) == 2 and nxt == 2 * len(line)
    p.write_bytes(b'not json\n{"id":1}\n{"id":2')
    msgs, nxt = FH.read_mail_page(p, 0, 200)
    assert [m["id"] for m in msgs] == [1] and nxt == len(b'not json\n{"id":1}\n')
    p.write_bytes(b"".join(b'{"id":%d}\n' % i for i in range(300)))
    assert len(FH.read_mail_page(p, 0, 7)[0]) == 7
    assert FH._events_args("", F.MAIL_LIMIT) == (0, 200)
    assert FH._events_args("limit=0", F.MAIL_LIMIT)[1] == 200
    assert FH._events_args("limit=201", F.MAIL_LIMIT)[1] == 200
    assert len(FH.read_mail_page(tmp_path / "missing", 0, 200)[0]) == 0
    assert FH.read_mail_page(tmp_path / "missing", 0, 200) == ([], 0)
    # over HTTP
    hub, host, srv, url = _pair(tmp_path, monkeypatch)
    fed = F.read_federation(host)
    now = time.time()
    st, hdrs, raw = _raw("GET", url, "/v1/mail", fed["token"], fed["secret"], now=now)
    assert st == 200 and hdrs["Content-Type"] == "application/json"
    assert json.loads(raw) == {"schema": "nw.federation.mail.page.v1", "messages": [],
                               "next_offset": 0}
    assert F.verify(fed["secret"], "GET", "/v1/mail", raw, hdrs["X-NW-Sig"],
                    int(hdrs["X-NW-Sig-Ts"]), direction="resp", status=200) is None
    st, hdrs, raw = _raw("PUT", url, "/v1/mail", fed["token"], fed["secret"], now=now)
    assert st == 405 and json.loads(raw) == {"error": "method_not_allowed"} and "X-NW-Sig" in hdrs
    st, hdrs, raw = _raw("POST", url, "/v1/mail", fed["token"], fed["secret"],
                         body=b'{"messages": []}', now=now)
    assert st == 200 and json.loads(raw) == {"schema": "nw.federation.mail.v1", "results": []}
    assert not M._inbox_path(host, "human").exists()
    spy = _Spy(monkeypatch)
    _ready(hub)
    assert ("GET", "/v1/mail?offset=0&limit=200", b"") in spy.calls
    srv.shutdown()


def test_mail_bad_offset_is_409(tmp_path, monkeypatch):
    hub, host, srv, url = _pair(tmp_path, monkeypatch)
    fed = F.read_federation(host)
    now = time.time()

    def get(query):
        path = "/v1/mail" + query
        st, hdrs, raw = _raw("GET", url, path, fed["token"], fed["secret"], now=now)
        return st, hdrs, raw, path

    assert get("?offset=0")[0] == 200
    M.send(host, to="conductor", frm="human", text="one")
    first = len(M._inbox_path(host, "conductor").read_bytes())
    for q in ("?offset=3", f"?offset={first + 1}", "?offset=-1", "?offset=abc", "?offset="):
        st, hdrs, raw, path = get(q)
        assert st == 409, q
        assert hdrs["Content-Type"] == "application/json" and json.loads(raw) == {"error": "bad_offset"}
        assert F.verify(fed["secret"], "GET", path, raw, hdrs["X-NW-Sig"],
                        int(hdrs["X-NW-Sig-Ts"]), direction="resp", status=st) is None
    st, _, raw, _ = get(f"?offset={first}")
    assert st == 200 and json.loads(raw)["messages"] == [] and json.loads(raw)["next_offset"] == first
    srv.shutdown()


def test_mail_409_resets_without_duplicates(tmp_path, monkeypatch):
    hub, host, srv, _ = _pair(tmp_path, monkeypatch)
    _ready(hub)
    m = M.send(host, to="conductor", frm="human", text="once")
    F.relay_mail(hub)
    assert len(_inbox(hub, "conductor")) == 1
    cur = _cursor(hub)
    good = cur["mail_offset"]
    assert good == len(M._inbox_path(host, "conductor").read_bytes())
    H.update(hub, lambda rs: H.find(rs, "box")["cursor"].__setitem__("mail_offset", good - 3))
    keep = {k: _cursor(hub)[k] for k in ("hub_inbox_offset", "hub_mail_events_offset")}
    queued = _send(hub, "posted after a 409")
    F.relay_mail(hub)                                       # a verified 409 still POSTs the outbox
    assert [a["msg_uid"] for a in _acks(hub)] == [queued["msg_uid"]]
    assert len(_inbox(host, "human")) == 1
    after = _cursor(hub)
    assert after["mail_offset"] == 0 and {k: after[k] for k in keep} == keep
    assert not _hub_events(hub, "host_cursor_reset") and len(_inbox(hub, "conductor")) == 1
    F.relay_mail(hub)
    assert _cursor(hub)["mail_offset"] == good and "mail_replay" not in _cursor(hub)
    assert len(_inbox(hub, "conductor")) == 1 and len(_hub_events(hub, "message")) == 1
    assert _inbox(hub, "conductor")[0]["msg_uid"] == m["msg_uid"]
    # an unsigned response leaves the cursor alone and appends nothing
    H.update(hub, lambda rs: H.find(rs, "box")["cursor"].__setitem__("mail_offset", good - 3))
    before = (_inbox(hub, "conductor"), _lines(E.events_path(hub)))
    _Spy(monkeypatch, get=_boom())
    F.relay_mail(hub)
    assert _cursor(hub)["mail_offset"] == good - 3
    assert (_inbox(hub, "conductor"), _lines(E.events_path(hub))) == before
    srv.shutdown()


def test_conductor_delivery_one_event(tmp_path, monkeypatch):
    hub, host, srv, _ = _pair(tmp_path, monkeypatch)
    _ready(hub)
    cp = M._inbox_path(host, "conductor")
    cp.parent.mkdir(parents=True, exist_ok=True)
    with open(cp, "ab") as f:                               # written before federation.json existed
        f.write((json.dumps({"seq": 1, "id": "conductor:1", "from": "human", "kind": "info",
                             "text": "old", "campaign": None, "re": None}) + "\n").encode())
    a = M.send(host, to="conductor", frm="human", text="hello", campaign="SPEC-1")
    b = M.send(host, to="conductor", frm="human", text="plain", kind="question")
    assert len(a["msg_uid"]) == 32 and "msg_uid" in b
    with monkeypatch.context() as mp:
        mp.setattr(M, "send", lambda *a, **k: pytest.fail("send"))
        mp.setattr(M, "validate_address", lambda *a, **k: pytest.fail("validate_address"))
        F.relay_mail(hub)
    recs = _inbox(hub, "conductor")
    assert [r["msg_uid"] for r in recs] == [a["msg_uid"], b["msg_uid"]]
    assert recs[0]["from"] == "human@box" and recs[0]["campaign"] == "SPEC-1@box"
    assert recs[0]["to"] == "conductor" and recs[0]["id"] == "conductor:1"
    assert recs[1]["campaign"] is None and recs[1]["needs_reply"] is True
    evs = _hub_events(hub, "message")
    assert len(evs) == 2 and all(e["host"] == "box" and "origin" not in e for e in evs)
    assert evs[0]["campaign"] == "SPEC-1@box" and evs[1]["campaign"] is None
    assert evs[0]["data"] == {"msg_id": "conductor:1", "from": "human@box", "kind": "info",
                              "text": "hello", "re": None, "msg_uid": a["msg_uid"]}
    assert evs[0]["by"] == "human@box" and evs[0]["spec_id"] is None and evs[0]["project"] is None
    assert _cursor(hub)["mail_offset"] == len(cp.read_bytes())
    F.relay_mail(hub)
    assert len(_inbox(hub, "conductor")) == 2 and len(_hub_events(hub, "message")) == 2
    # a crash that keeps the inbox line and loses the event reuses the line's id
    c = M.send(host, to="conductor", frm="human", text="third")
    mark = E.events_path(hub).stat().st_size
    real = F._fsync_events
    off = _cursor(hub)["mail_offset"]
    monkeypatch.setattr(F, "_fsync_events", lambda cfg: (_ for _ in ()).throw(OSError("x")))
    F.relay_mail(hub)
    assert _cursor(hub)["mail_offset"] == off
    monkeypatch.setattr(F, "_fsync_events", real)
    with open(E.events_path(hub), "r+b") as f:
        f.truncate(mark)
    F.relay_mail(hub)
    got = [r for r in _inbox(hub, "conductor") if r["msg_uid"] == c["msg_uid"]]
    ev = [e for e in _hub_events(hub, "message") if e["data"]["msg_uid"] == c["msg_uid"]]
    assert len(got) == 1 and len(ev) == 1 and ev[0]["data"]["msg_id"] == got[0]["id"]
    srv.shutdown()


class _J:
    """`json` as seen by `federation`: records what `loads` decodes."""

    def __init__(self, seen):
        self.seen = seen

    def loads(self, raw, *a, **k):
        self.seen.append(raw if isinstance(raw, bytes) else raw.encode())
        return json.loads(raw, *a, **k)

    def __getattr__(self, name):
        return getattr(json, name)


def test_delivery_fsync_and_checkpoint(tmp_path, monkeypatch):
    hub, host, srv, _ = _pair(tmp_path, monkeypatch)
    F.relay_tick(hub)
    pad = b"".join(b'{"pad":%d,"msg_uid":"x"}\n' % i for i in range(60))
    assert len(pad) > 1000
    for p in (M._inbox_path(hub, "conductor"), E.events_path(hub)):
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "ab") as f:
            f.write(pad)
    F.relay_mail(hub)                                       # offsets now sit after the padding
    cur = _cursor(hub)
    assert cur["hub_inbox_offset"] == len(pad) and cur["hub_mail_events_offset"] > len(pad) - 1
    a = M.send(host, to="conductor", frm="human", text="a")
    real = F._fsync_fd
    left = [1]

    def once(fd):
        if left:
            left.pop()
            raise OSError("fsync")
        real(fd)

    monkeypatch.setattr(F, "_fsync_fd", once)
    F.relay_mail(hub)
    assert _cursor(hub)["mail_offset"] == 0
    seen = []
    monkeypatch.setattr(F, "json", _J(seen))
    F.relay_mail(hub)
    assert seen and not [r for r in seen if b'"pad"' in r]
    assert [r["msg_uid"] for r in _inbox(hub, "conductor")][-1] == a["msg_uid"]
    assert len([r for r in _inbox(hub, "conductor") if r.get("msg_uid") == a["msg_uid"]]) == 1
    assert len([e for e in _hub_events(hub, "message") if e["data"]["msg_uid"] == a["msg_uid"]]) == 1
    # the checkpoint raises after both fsyncs: the next tick's rescans still leave one of each
    monkeypatch.setattr(F, "json", json)
    b = M.send(host, to="conductor", frm="human", text="b")
    real_update = H.update
    boom = [1]

    def update(cfg, fn):
        if boom:
            boom.pop()
            raise OSError("checkpoint")
        return real_update(cfg, fn)

    monkeypatch.setattr(H, "update", update)
    off = _cursor(hub)["mail_offset"]
    F.relay_mail(hub)
    assert _cursor(hub)["mail_offset"] == off
    seen.clear()
    monkeypatch.setattr(F, "json", _J(seen))
    F.relay_mail(hub)
    assert seen and not [r for r in seen if b'"pad"' in r]
    assert len([r for r in _inbox(hub, "conductor") if r.get("msg_uid") == b["msg_uid"]]) == 1
    assert len([e for e in _hub_events(hub, "message") if e["data"]["msg_uid"] == b["msg_uid"]]) == 1
    srv.shutdown()


def test_post_mail_writes_mail_only(tmp_path, monkeypatch):
    host = _mk(tmp_path, "host")
    store.write_state(host, "C-1", {"campaign": "C-1", "state": "RUNNING"})
    state = store.campaign_dir(host, "C-1")
    before = {p: p.read_bytes() for p in state.rglob("*") if p.is_file()}
    assert before
    msg = {"msg_uid": UID, "to": "steward:demo", "from": "conductor@hub", "kind": "request",
           "text": "park it", "campaign": "C-1", "re": None, "action": {"verb": "park"}}
    assert FH.post_mail(host, [msg]) == [{"msg_uid": UID, "result": "delivered", "seq": 1}]
    rec = _inbox(host, "steward:Demo")[0]
    assert rec["action"] == {"verb": "park"} and rec["to"] == "steward:Demo"
    assert rec["needs_reply"] is True and rec["msg_uid"] == UID and rec["from"] == "conductor@hub"
    assert rec["id"] == "steward:Demo:1"
    assert before == {p: p.read_bytes() for p in state.rglob("*") if p.is_file()}
    names = {n.id for n in ast.walk(ast.parse(inspect.getsource(FH.post_mail)))
             if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(
                 ast.parse(inspect.getsource(FH.post_mail))) if isinstance(n, ast.Attribute)}
    assert not names & {"drain_actions", "plan_park", "apply_park", "plan_resume", "apply_resume"}
    rpath = store.agentic_dir(host) / "federation" / "received.jsonl"
    assert len(_lines(rpath)) == 1
    rpath.unlink()
    assert FH.post_mail(host, [msg]) == [{"msg_uid": UID, "result": "dup", "seq": 1}]
    assert len(_lines(rpath)) == 1 and len(_inbox(host, "steward:Demo")) == 1
    assert _lines(rpath)[0]["inbox_end"] == M._inbox_path(host, "steward:Demo").stat().st_size
    # a dup whose dedupe line exists appends nothing
    assert FH.post_mail(host, [msg])[0]["result"] == "dup"
    assert len(_lines(rpath)) == 1 and len(_inbox(host, "steward:Demo")) == 1


def test_bare_addresses_stay_local(tmp_path, monkeypatch):
    cfg = _mk(tmp_path, "solo")
    assert M.validate_address(cfg, "conductor") == "conductor"
    assert M.validate_address(cfg, "human") == "human"
    assert M.validate_address(cfg, "steward:demo") == "steward:Demo"
    for bad in ("steward:P@host", "human@host", "bob"):
        with pytest.raises(ValueError):
            M.validate_address(cfg, bad)
    src = inspect.getsource(M.validate_address)
    assert not [w for w in ("hosts", "federation", "@", "msg_uid") if w in src]
    tree = ast.parse(inspect.getsource(M))
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                and "federation" in ast.dump(n)]
    r = _cli(cfg, monkeypatch, "--to", "steward:Meta-Tools@box", "--from", "conductor", "x")
    assert r.exit_code != 0
    assert not F.outbox_path(cfg, "box").exists() and not H.path(cfg).exists()
    assert "msg_uid" not in M.send(cfg, to="conductor", frm="human", text="x")
    hub, host, srv, _ = _pair(tmp_path, monkeypatch)
    r = _cli(hub, monkeypatch, "--to", "steward:Meta-Tools", "--from", "conductor", "local")
    assert r.exit_code == 0, r.output
    assert len(_inbox(hub, "steward:Meta-Tools")) == 1 and not F.outbox_path(hub, "box").exists()
    assert "msg_uid" not in M.send(hub, to="conductor", frm="human", text="x")
    assert len(M.send(host, to="conductor", frm="human", text="x")["msg_uid"]) == 32
    assert "msg_uid" not in M.send(host, to="human", frm="conductor", text="x")
    H.update(hub, lambda rs: H.find(rs, "box").__setitem__(
        "status_cache", {"projects": ["Demo"], "campaigns": []}))
    assert _send(hub, to="steward:demo@box")["to"] == "steward:Demo"
    H.update(hub, lambda rs: H.find(rs, "box").__setitem__("status_cache", None))
    assert _send(hub, to="steward: Typed @box")["to"] == "steward:Typed"
    srv.shutdown()


def test_send_refuses_unknown_off_and_missing_project(tmp_path, monkeypatch):
    hub, host, srv, _ = _pair(tmp_path, monkeypatch)
    args = ("--from", "conductor", "x")
    assert _cli(hub, monkeypatch, "--to", "human@nope", *args).exit_code != 0
    H.update(hub, lambda rs: H.find(rs, "box").__setitem__("enabled", False))
    assert _cli(hub, monkeypatch, "--to", "human@box", *args).exit_code != 0
    H.update(hub, lambda rs: (H.find(rs, "box").__setitem__("enabled", True),
                              H.find(rs, "box").__setitem__(
                                  "status_cache", {"projects": ["Other"], "campaigns": []})))
    assert _cli(hub, monkeypatch, "--to", "steward:Demo@box", *args).exit_code != 0
    assert _cli(hub, monkeypatch, "--to", "bob@box", *args).exit_code != 0
    assert not F.outbox_path(hub, "box").exists()
    H.update(hub, lambda rs: H.find(rs, "box").__setitem__("status_cache", None))
    r = _cli(hub, monkeypatch, "--to", "human@box", *args)
    assert r.exit_code == 0 and len(_outbox(hub)) == 1
    spy = _Spy(monkeypatch)
    F.relay_mail(hub)                                       # enabled, but no cursor yet
    assert spy.calls == [] and F.pending_mail(hub, "box")
    F.relay_tick(hub)
    spy.calls.clear()
    H.update(hub, lambda rs: H.find(rs, "box").__setitem__("enabled", False))
    F.relay_mail(hub)
    assert spy.calls == []
    assert "mail_offset" not in _cursor(hub)
    H.update(hub, lambda rs: H.find(rs, "box").__setitem__("enabled", True))
    F.relay_mail(hub)
    assert len(_inbox(host, "human")) == 1 and len(_acks(hub)) == 1
    srv.shutdown()


def test_mail_pool_and_tick_order(tmp_path, monkeypatch):
    assert F.POOL_WAIT_S == 2.5
    hub, host, srv, _ = _pair(tmp_path, monkeypatch)
    hub, host2, srv2, _ = _pair(tmp_path, monkeypatch, name="box2", hub=hub)
    F.relay_tick(hub)
    waited = []
    real_wait = F.cf.wait
    monkeypatch.setattr(F.cf, "wait", lambda fs, **k: (waited.append(k.get("timeout")),
                                                       real_wait(fs, **k))[1])
    # a task past the wait is not applied, even after it commits the POST
    monkeypatch.setattr(F, "POOL_WAIT_S", 0.05)
    release, done = threading.Event(), threading.Event()
    real_task = F._mail_task

    def slow(rec, cfg, offset, pending):
        release.wait(waits.WAIT_S)
        try:
            return real_task(rec, cfg, offset, pending)
        finally:
            done.set()

    monkeypatch.setattr(F, "_mail_task", slow)
    F.relay_mail(hub)                                       # first pass: cursor init only
    _send(hub, "slow")
    M.send(host, to="conductor", frm="human", text="late")
    F.relay_mail(hub)
    assert waited[-1] == 0.05
    release.set()
    assert done.wait(waits.WAIT_S) and waits.until(lambda: len(_inbox(host, "human")) == 1)
    assert _acks(hub) == [] and _cursor(hub)["mail_offset"] == 0
    monkeypatch.setattr(F, "_mail_task", real_task)
    monkeypatch.setattr(F, "POOL_WAIT_S", 2.5)
    order = []
    real_apply = F._apply_post
    monkeypatch.setattr(F, "_apply_post", lambda cfg, name, *a: (order.append(
        (name, threading.current_thread() is threading.main_thread())), real_apply(cfg, name, *a))[1])
    F.relay_mail(hub)
    assert waited[-1] == 2.5 and order == [("box", True), ("box2", True)]
    assert [a["result"] for a in _acks(hub)] == ["dup"] and len(_inbox(host, "human")) == 1
    assert len(_inbox(hub, "conductor")) == 1
    # a GET transport failure never POSTs; a 200 without results leaves the line pending
    _send(hub, "next")
    with monkeypatch.context() as mp:
        spy = _Spy(mp, get=lambda real, body: (_ for _ in ()).throw(OSError("down")))
        F.relay_mail(hub)
        assert spy.posts() == []
    with monkeypatch.context() as mp:
        _Spy(mp, post=_stub(200, {}))
        F.relay_mail(hub)
    assert len(F.pending_mail(hub, "box")) == 1
    # tick order and isolation
    import nightwright.agentic.federation as FED
    (tmp_path / "q").mkdir()
    repo = make_repo(tmp_path / "q")
    cfg = make_cfg(tmp_path / "q", repo, script(tmp_path / "q", []))
    calls = []
    monkeypatch.setattr(FED, "relay_tick", lambda cfg, **k: calls.append("tick"))
    monkeypatch.setattr(FED, "relay_mail", lambda cfg, **k: calls.append("mail"))
    Q.tick(cfg)
    assert calls == ["tick", "mail"]
    monkeypatch.setattr(FED, "relay_tick", lambda cfg, **k: (_ for _ in ()).throw(RuntimeError("t")))
    monkeypatch.setattr(FED, "relay_mail", lambda cfg, **k: (_ for _ in ()).throw(RuntimeError("m")))
    assert Q.tick(cfg) is not None
    srv.shutdown()
    srv2.shutdown()


def test_mail_cursor_keys(tmp_path, monkeypatch):
    hub, host, srv, _ = _pair(tmp_path, monkeypatch)
    F.relay_tick(hub)
    rec = H.find(H.load(hub), "box")
    assert "mail_seq" not in rec and set(rec["cursor"]) == {
        "events_offset", "events_origin_id", "hub_events_offset"}
    before = dict(rec["cursor"])
    F.relay_mail(hub)
    cur = _cursor(hub)
    assert {k: cur[k] for k in before} == before
    assert set(cur) - set(before) == {"mail_offset", "hub_inbox_offset", "hub_mail_events_offset"}
    assert "mail_seq" not in H.find(H.load(hub), "box")
    srv.shutdown()


def test_inert_and_no_queue_import(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    F.relay_mail(cfg)
    Q.tick(cfg)
    assert not H.path(cfg).exists() and not F.outbox_path(cfg, "box").exists()
    assert not (store.agentic_dir(cfg) / "mailbox").exists() or not list(
        (store.agentic_dir(cfg) / "mailbox").glob("outbox-*"))
    root = Path(__file__).resolve().parents[1] / "src" / "nightwright" / "agentic"
    for name in ("hosts.py", "federation.py", "cli_hosts.py"):
        assert not _queue_import_violations((root / name).read_text()), name
    assert not _queue_import_violations((root / "federation_host.py").read_text(),
                                        federation_host_payload_only=True)
    assert "subprocess" not in (root / "federation_host.py").read_text()


# ---- review round: signature path, tick isolation, dedupe, caps, replay window ----------------

def _tamper(monkeypatch, method):
    """Real socket, real host: every response to `method` leaves the host with a bad signature."""
    real = F.response_headers

    def headers(secret, m, *a, **k):
        if m == method:
            return {"X-NW-Sig-Ts": "1", "X-NW-Sig": "bad"}
        return real(secret, m, *a, **k)

    monkeypatch.setattr(F, "response_headers", headers)
    return real


def test_unsigned_mail_responses_over_a_real_socket(tmp_path, monkeypatch):
    hub, host, srv, _ = _pair(tmp_path, monkeypatch)
    _ready(hub)
    line = _send(hub, "pending")
    # a signed 401 from the host's body cap, but the signature is bad: not a verified 4xx
    with monkeypatch.context() as mp:
        mp.setattr(FH, "MAX_BODY_BYTES", 10)
        _tamper(mp, "POST")
        F.relay_mail(hub)
    assert _acks(hub) == [] and not _hub_events(hub, "host_mail_rejected")
    assert not M._inbox_path(host, "human").exists()
    # the same 401 with a good signature is verified (and terminal) - this is what the tamper blocked
    with monkeypatch.context() as mp:
        mp.setattr(FH, "MAX_BODY_BYTES", 10)
        spy = _Spy(mp)
        F.relay_mail(hub)
    assert [a["reason"] for a in _acks(hub)] == ["unauthorized"] and len(spy.posts()) == 1
    # an unsigned 409 on the GET: no reset, no POST, nothing appended
    other = _send(hub, "next")
    H.update(hub, lambda rs: H.find(rs, "box")["cursor"].__setitem__("mail_offset", 5))
    before = (_cursor(hub), _lines(E.events_path(hub)))
    with monkeypatch.context() as mp:
        _tamper(mp, "GET")
        spy = _Spy(mp)
        F.relay_mail(hub)
    assert spy.posts() == [] and (_cursor(hub), _lines(E.events_path(hub))) == before
    assert [a["msg_uid"] for a in _acks(hub)] == [line["msg_uid"]]
    assert F.pending_mail(hub, "box")[0]["msg_uid"] == other["msg_uid"]
    srv.shutdown()


def test_tick_runs_mail_once_after_a_raising_relay_tick(tmp_path, monkeypatch):
    import nightwright.agentic.federation as FED
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    calls, journal = [], []
    monkeypatch.setattr(Q, "journal", lambda c, name, **kw: journal.append((name, kw)))
    monkeypatch.setattr(FED, "relay_tick", lambda c, **k: (calls.append("tick"), 1 / 0)[1])
    monkeypatch.setattr(FED, "relay_mail", lambda c, **k: calls.append("mail"))
    assert Q.tick(cfg) is not None
    assert calls == ["tick", "mail"]
    assert [n for n, _ in journal if n.startswith("federation_")] == ["federation_relay_failed"]
    calls.clear()
    journal.clear()
    monkeypatch.setattr(FED, "relay_tick", lambda c, **k: calls.append("tick"))
    monkeypatch.setattr(FED, "relay_mail", lambda c, **k: (calls.append("mail"), 1 / 0)[1])
    assert Q.tick(cfg) is not None
    assert calls == ["tick", "mail"]
    assert [n for n, _ in journal if n.startswith("federation_")] == ["federation_mail_failed"]


def _rejected_line(hub):
    _raw_outbox(hub, msg_uid="1" * 32, to="steward:Missing")
    return "1" * 32


def test_rejected_event_is_not_duplicated_when_the_ack_write_fails(tmp_path, monkeypatch):
    hub, host, srv, _ = _pair(tmp_path, monkeypatch)
    _ready(hub)
    uid = _rejected_line(hub)
    real = F._append_durable
    left = [1]

    def flaky(path, line):
        if left and path == F.outbox_path(hub, "box"):
            left.pop()
            raise OSError("ack write")
        return real(path, line)

    with monkeypatch.context() as mp:
        mp.setattr(F, "_append_durable", flaky)
        F.relay_mail(hub)
    assert _acks(hub) == [] and len(_hub_events(hub, "host_mail_rejected")) == 1
    F.relay_mail(hub)                                       # retried: found in the tail, acked
    assert [a["msg_uid"] for a in _acks(hub)] == [uid]
    assert len(_hub_events(hub, "host_mail_rejected")) == 1
    F.relay_mail(hub)
    assert len(_hub_events(hub, "host_mail_rejected")) == 1 and len(_acks(hub)) == 1
    srv.shutdown()


def test_rejected_event_fsync_failure_does_not_ack(tmp_path, monkeypatch):
    hub, host, srv, _ = _pair(tmp_path, monkeypatch)
    _ready(hub)
    uid = _rejected_line(hub)
    real = F._fsync_events
    left = [1]

    def flaky(cfg):
        if left:
            left.pop()
            raise OSError("events fsync")
        return real(cfg)

    with monkeypatch.context() as mp:
        mp.setattr(F, "_fsync_events", flaky)
        F.relay_mail(hub)
    assert _acks(hub) == [] and [m["msg_uid"] for m in F.pending_mail(hub, "box")] == [uid]
    F.relay_mail(hub)
    assert [a["msg_uid"] for a in _acks(hub)] == [uid]
    assert len(_hub_events(hub, "host_mail_rejected")) == 1
    srv.shutdown()


def test_a_task_that_finishes_during_an_earlier_apply_is_not_applied(tmp_path, monkeypatch):
    hub, host, srv, _ = _pair(tmp_path, monkeypatch)
    hub, host2, srv2, _ = _pair(tmp_path, monkeypatch, name="box2", hub=hub)
    _ready(hub)
    _ready(hub, "box2")
    _send(hub, "to a")
    _send(hub, "to b", to="human@box2")
    M.send(host2, to="conductor", frm="human", text="from b")
    release, finished = threading.Event(), threading.Event()
    real_task = F._mail_task

    def task(rec, cfg, offset, pending):
        if rec["name"] == "box2":
            release.wait(waits.WAIT_S)
        try:
            return real_task(rec, cfg, offset, pending)
        finally:
            if rec["name"] == "box2":
                finished.set()

    real_apply = F._apply_post

    def apply(cfg, name, *a):
        if name == "box":                                   # B finishes while A is being applied
            release.set()
            assert finished.wait(waits.WAIT_S)
        return real_apply(cfg, name, *a)

    monkeypatch.setattr(F, "POOL_WAIT_S", 0.05)
    monkeypatch.setattr(F, "_mail_task", task)
    monkeypatch.setattr(F, "_apply_post", apply)
    F.relay_mail(hub)
    assert len(_acks(hub, "box")) == 1 and _acks(hub, "box2") == []
    assert _cursor(hub, "box2")["mail_offset"] == 0 and len(_inbox(host2, "human")) == 1
    assert [m for m in _inbox(hub, "conductor")] == []
    monkeypatch.setattr(F, "POOL_WAIT_S", 2.5)
    monkeypatch.setattr(F, "_mail_task", real_task)
    F.relay_mail(hub)
    assert [a["result"] for a in _acks(hub, "box2")] == ["dup"]
    assert len(_inbox(host2, "human")) == 1 and len(_inbox(hub, "conductor")) == 1
    srv.shutdown()
    srv2.shutdown()


def test_mail_409_replay_window_spans_every_page(tmp_path, monkeypatch):
    hub, host, srv, _ = _pair(tmp_path, monkeypatch)
    monkeypatch.setattr(F, "MAIL_LIMIT", 20)               # pages of 20 host lines
    _ready(hub)
    sent = [M.send(host, to="conductor", frm="human", text=f"m{i}")["msg_uid"] for i in range(65)]
    F.relay_mail(hub)
    assert len(_inbox(hub, "conductor")) == 20              # the hub-side page limit
    for _ in range(3):
        F.relay_mail(hub)
    end = len(M._inbox_path(host, "conductor").read_bytes())
    assert _cursor(hub)["mail_offset"] == end and len(_inbox(hub, "conductor")) == 65
    H.update(hub, lambda rs: H.find(rs, "box")["cursor"].__setitem__("mail_offset", end - 3))
    keep = {k: _cursor(hub)[k] for k in ("hub_inbox_offset", "hub_mail_events_offset")}
    F.relay_mail(hub)
    cur = _cursor(hub)
    assert cur["mail_offset"] == 0 and cur["mail_replay"] == end - 3
    for _ in range(3):                                      # pages 1-3 of 4: the window stays open
        F.relay_mail(hub)
        cur = _cursor(hub)
        assert cur["mail_replay"] == end - 3 and {k: cur[k] for k in keep} == keep
        assert len(_inbox(hub, "conductor")) == 65 and len(_hub_events(hub, "message")) == 65
    F.relay_mail(hub)                                       # the page that reaches the mark
    cur = _cursor(hub)
    assert cur["mail_offset"] == end and "mail_replay" not in cur
    assert [r["msg_uid"] for r in _inbox(hub, "conductor")] == sent
    assert sorted(e["data"]["msg_uid"] for e in _hub_events(hub, "message")) == sorted(sent)
    srv.shutdown()


def test_mail_409_on_an_emptied_host_inbox_closes_the_window(tmp_path, monkeypatch):
    hub, host, srv, _ = _pair(tmp_path, monkeypatch)
    _ready(hub)
    M.send(host, to="conductor", frm="human", text="x")
    F.relay_mail(hub)
    M._inbox_path(host, "conductor").write_bytes(b"")       # the host log was replaced
    F.relay_mail(hub)                                       # 409: offset past the end
    assert _cursor(hub)["mail_offset"] == 0 and _cursor(hub)["mail_replay"] > 0
    F.relay_mail(hub)                                       # an empty page at 0: nothing to replay
    assert "mail_replay" not in _cursor(hub)
    srv.shutdown()


def test_outbox_batches_are_capped_by_count_and_bytes(tmp_path, monkeypatch):
    hub, host, srv, _ = _pair(tmp_path, monkeypatch)
    _ready(hub)
    uids = [f"{i:032x}" for i in range(205)]
    for u in uids:
        _raw_outbox(hub, msg_uid=u)
    with monkeypatch.context() as mp:
        spy = _Spy(mp)
        F.relay_mail(hub)
        F.relay_mail(hub)
    first, second = spy.posts()
    assert [m["msg_uid"] for m in first] == uids[:200]
    assert [m["msg_uid"] for m in second] == uids[200:]
    assert F.pending_mail(hub, "box") == []
    # bytes: a batch stops before it passes MAIL_POST_BYTES, but one big line always goes alone
    big = "x" * 500000
    path = F.outbox_path(hub, "box")
    path.unlink()
    ids = [f"{i + 1000:032x}" for i in range(7)]
    for u in ids:
        _raw_outbox(hub, msg_uid=u, text=big)
    batch = F.pending_mail(hub, "box")
    assert [m["msg_uid"] for m in batch] == ids[:4]
    assert len(F._mail_body(batch)) <= F.MAIL_POST_BYTES
    path.unlink()
    _raw_outbox(hub, msg_uid=ids[0], text="y" * (F.MAIL_POST_BYTES + 5))
    _raw_outbox(hub, msg_uid=ids[1])
    assert [m["msg_uid"] for m in F.pending_mail(hub, "box")] == [ids[0]]
    srv.shutdown()


def test_ack_reasons_are_truncated_and_odd_results_stay_pending(tmp_path, monkeypatch):
    hub, host, srv, _ = _pair(tmp_path, monkeypatch)
    _ready(hub)
    a, b = _send(hub, "a"), _send(hub, "b")
    with monkeypatch.context() as mp:
        _Spy(mp, post=_stub(400, {"error": "e" * 500}))
        F.relay_mail(hub)
    assert [len(x["reason"]) for x in _acks(hub)] == [200, 200]
    assert all(len(e["data"]["reason"]) == 200 for e in _hub_events(hub, "host_mail_rejected"))
    c, d, e, f, g = (_send(hub, t) for t in "cdefg")
    foreign = "f" * 32
    doc = {"schema": "nw.federation.mail.v1", "results": [
        {"msg_uid": c["msg_uid"], "result": "weird", "seq": 1},                 # unknown result
        {"msg_uid": foreign, "result": "delivered", "seq": 2},                  # not in the POST
        {"msg_uid": d["msg_uid"], "result": "delivered", "seq": 3},
        {"msg_uid": d["msg_uid"], "result": "dup", "seq": 4},                   # duplicate element
        {"msg_uid": e["msg_uid"], "result": "delivered"},                       # no integer seq
        {"msg_uid": f["msg_uid"], "result": "dup", "seq": "7"},
        "junk"]}                                                                # g: missing
    with monkeypatch.context() as mp:
        _Spy(mp, post=_stub(200, doc))
        F.relay_mail(hub)
    got = [x for x in _acks(hub) if x["msg_uid"] in {c["msg_uid"], d["msg_uid"], e["msg_uid"],
                                                     f["msg_uid"], g["msg_uid"], foreign}]
    assert got == [{"msg_uid": d["msg_uid"], "result": "delivered", "seq": 3}]
    assert [m["msg_uid"] for m in F.pending_mail(hub, "box")] == [
        c["msg_uid"], e["msg_uid"], f["msg_uid"], g["msg_uid"]]
    for status, body in ((503, b"<html>down</html>"), (502, b""), (200, b"[]"), (200, b"nope")):
        with monkeypatch.context() as mp:
            _Spy(mp, post=_stub(status, body))
            F.relay_mail(hub)
        assert len(F.pending_mail(hub, "box")) == 4
    srv.shutdown()


def test_non_ascii_text_round_trips_signed(tmp_path, monkeypatch):
    hub, host, srv, _ = _pair(tmp_path, monkeypatch)
    _ready(hub)
    text = "héllo ✓ 日本語"
    _send(hub, text)
    with monkeypatch.context() as mp:
        spy = _Spy(mp)
        F.relay_mail(hub)
    assert text.encode("utf-8") in spy.calls[-1][2] and b"\\u" not in spy.calls[-1][2]
    assert _inbox(host, "human")[0]["text"] == text and len(_acks(hub)) == 1
    srv.shutdown()


def test_post_mail_rejects_malformed_elements(tmp_path):
    host = _mk(tmp_path, "host")
    ok = {"msg_uid": UID, "to": "human", "from": "c", "kind": "info", "text": "t"}
    res = FH.post_mail(host, [{**ok, "from": ""}, {k: v for k, v in ok.items() if k != "from"},
                              {**ok, "from": 5}, 5, "str", None, {**ok, "msg_uid": 7}])
    assert [r["result"] for r in res] == ["rejected"] * 7
    assert [r["reason"] for r in res] == ["bad_message"] * 3 + ["bad_msg_uid"] * 4
    assert [r["msg_uid"] for r in res] == [UID, UID, UID, "", "", "", ""]
    assert not M._inbox_path(host, "human").exists()


def test_host_post_rescans_from_the_last_received_inbox_end(tmp_path, monkeypatch):
    host = _mk(tmp_path, "host")
    ipath = M._inbox_path(host, "human")
    ipath.parent.mkdir(parents=True, exist_ok=True)
    ipath.write_bytes(b"".join(b'{"pad":%d,"seq":%d}\n' % (i, i + 1) for i in range(80)))
    mk = lambda u, t: {"msg_uid": u, "to": "human", "from": "c", "kind": "info", "text": t}
    assert FH.post_mail(host, [mk("1" * 32, "one")])[0]["result"] == "delivered"
    seen = []
    monkeypatch.setattr(FH, "json", _J(seen))
    assert FH.post_mail(host, [mk("2" * 32, "two")])[0] == {
        "msg_uid": "2" * 32, "result": "delivered", "seq": 82}
    assert seen and not [r for r in seen if b'"pad"' in r]


def test_hub_delivery_filter_and_field_copying(tmp_path, monkeypatch):
    hub, host, srv, _ = _pair(tmp_path, monkeypatch)
    _ready(hub)
    cp = M._inbox_path(host, "conductor")
    cp.parent.mkdir(parents=True, exist_ok=True)
    base = {"schema": "nw.message.v1", "from": "human", "kind": "info", "text": "t",
            "campaign": None, "re": None}
    rows = [{**base, "seq": 1, "msg_uid": "1" * 32, "kind": "nope"},        # bad kind
            {**base, "seq": 2, "msg_uid": "2" * 32, "text": "  "},          # empty text
            {**base, "seq": 3, "msg_uid": "3" * 32, "text": {"x": 1}},      # text not a string
            {**base, "seq": 4, "msg_uid": "4" * 32, "from": None, "ts": "2026-01-02T03:04:05+00:00",
             "re": "C-9", "campaign": 5, "needs_reply": True},
            {**base, "seq": 5, "msg_uid": "5" * 32, "ts": 5, "re": 7}]
    cp.write_bytes(b"".join((json.dumps(r) + "\n").encode() for r in rows))
    F.relay_mail(hub)
    recs = _inbox(hub, "conductor")
    assert [r["msg_uid"] for r in recs] == ["4" * 32, "5" * 32]
    assert _cursor(hub)["mail_offset"] == len(cp.read_bytes())
    r4, r5 = recs
    assert r4["from"] == "host@box" and r4["ts"] == "2026-01-02T03:04:05+00:00"
    assert r4["re"] == "C-9" and r4["campaign"] is None and r4["needs_reply"] is True
    assert r5["ts"] != 5 and r5["re"] is None and r5["from"] == "human@box"
    evs = {e["data"]["msg_uid"]: e for e in _hub_events(hub, "message")}
    assert sorted(evs) == ["4" * 32, "5" * 32]
    assert evs["4" * 32]["ts"] == "2026-01-02T03:04:05+00:00" and evs["4" * 32]["data"]["re"] == "C-9"
    assert evs["4" * 32]["data"]["from"] == "host@box" and evs["5" * 32]["data"]["re"] is None
    srv.shutdown()
