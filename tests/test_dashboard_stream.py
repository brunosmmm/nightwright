"""SPEC-0402: `GET /api/stream` over real sockets against the seeded data dir: SSE framing, the
load guard (IDEA-008: server work does not grow with the number of clients), backpressure, the
slot cap, auth, and the read-only guard."""
import json
import socket
import threading
import time

import pytest

from fixtures.dashboard_data import ACTIVE, seed
from test_dashboard_readonly import tree
from test_dashboard_snapshot import check_schema, ledger_run_end
from waits import join, until
from nightwright.agentic import queue as Q
from nightwright.agentic.dashboard import server as DS
from nightwright.agentic.dashboard import snapshot as SN
from nightwright.agentic.dashboard import stream as ST

pytestmark = pytest.mark.fast

SNAPSHOTTER = "nw-dashboard-snapshotter"


def snapshotter_alive():
    return any(t.name == SNAPSHOTTER and t.is_alive() for t in threading.enumerate())


class Srv:
    def __init__(self, fx, *, refresh_s=0.05, max_stale_s=3600, builder=None, heartbeat_s=15.0,
                 send_timeout_s=10.0, max_clients=None):
        self.fx, self.cfg = fx, fx["cfg"]
        if max_clients:
            self.cfg.setdefault("agentic", {}).setdefault("dashboard", {})["max_clients"] = \
                max_clients
        kw = {"builder": builder} if builder else {}
        self.cache = SN.SnapshotCache(self.cfg, refresh_s=refresh_s, max_stale_s=max_stale_s,
                                      **kw)
        self.srv = DS.make_server(self.cfg, "127.0.0.1:0", cache=self.cache)
        self.srv.heartbeat_s, self.srv.send_timeout_s = heartbeat_s, send_timeout_s
        self.port = self.srv.server_address[1]
        self.hub = self.srv.hub
        self.t = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05},
                                  daemon=True)
        self.t.start()
        self.clients = []

    def stream(self, rev=None, *, token=None, rcvbuf=None, host=True):
        c = Client(self.port, token, rev, rcvbuf, host)
        self.clients.append(c)
        return c

    def get(self, path="/api/snapshot"):
        c = Client(self.port, None, path=path)
        try:
            return c.status, c.headers, c.read_all()
        finally:
            c.close()

    def close(self):
        for c in self.clients:
            c.close()
        self.srv.shutdown()
        join(self.t, 10)
        self.srv.server_close()


class Client:
    """A raw-socket SSE client: parses status, headers and `\\n\\n`-terminated blocks."""

    def __init__(self, port, token, rev=None, rcvbuf=None, host=True, path=None):
        self.sock = socket.socket()
        if rcvbuf:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self.sock.settimeout(30)
        self.sock.connect(("127.0.0.1", port))
        path = path or "/api/stream" + (f"?rev={rev}" if rev else "")
        lines = [f"GET {path} HTTP/1.1", "Connection: close"]
        if host:
            lines.append(f"Host: 127.0.0.1:{port}")
        if token:
            lines.append(f"Authorization: Bearer {token}")
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        self.buf = b""
        head = self._until(b"\r\n\r\n")
        first, *hdrs = head.decode().split("\r\n")
        self.status = int(first.split()[1])
        self.headers = {k.lower(): v.strip() for k, _, v in (h.partition(":") for h in hdrs)}

    def _until(self, sep, timeout=30):
        self.sock.settimeout(timeout)
        while sep not in self.buf:
            d = self.sock.recv(65536)
            if not d:
                raise EOFError
            self.buf += d
        out, _, self.buf = self.buf.partition(sep)
        return out

    def block(self, timeout=30):
        """The next SSE block as a dict (`event`, `id`, `data`, `comment`, `retry`), or None
        on timeout; raises EOFError when the server closed the stream."""
        try:
            raw = self._until(b"\n\n", timeout).decode()
        except socket.timeout:
            return None
        out = {}
        for ln in raw.split("\n"):
            if ln.startswith(":"):
                out["comment"] = ln[1:].strip()
            else:
                k, _, v = ln.partition(": ")
                out[k] = v
        return out

    def event(self, timeout=30):
        """The next `snapshot` event, skipping comments and `retry`."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            b = self.block(max(0.05, end - time.monotonic()))
            if b and b.get("event") == "snapshot":
                return b
        return None

    def read_all(self):
        self.sock.settimeout(10)
        while True:
            d = self.sock.recv(65536)
            if not d:
                break
            self.buf += d
        return self.buf

    def closed_by_server(self, timeout=30):
        try:
            while self.block(timeout) is not None:
                pass
        except (EOFError, OSError):
            return True
        return False

    def close(self):
        self.sock.close()


@pytest.fixture
def mk(tmp_path):
    made = []

    def make(**kw):
        s = Srv(seed(tmp_path / f"s{len(made)}"), **kw)
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()


# ---- framing -----------------------------------------------------------------------------

def test_sse_framing_snapshot_event_and_rev_gating(mk):
    s = mk(heartbeat_s=0.2)
    c = s.stream()
    assert c.status == 200
    assert c.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert c.headers["cache-control"] == "no-store" and c.headers["x-accel-buffering"] == "no"
    assert "content-length" not in c.headers
    assert c.block() == {"retry": "3000"}
    ev = c.event()
    doc = json.loads(ev["data"])
    check_schema(doc)
    assert ev["id"] == doc["rev"] and "\n" not in ev["data"]
    # no change: only heartbeats (no second snapshot) for several ticks
    b = c.block()
    assert b == {"comment": "hb"}
    # a ledger change gives exactly one new event with a new rev
    ledger_run_end(s.cfg, ACTIVE, 1.5)
    ev2 = c.event()
    assert ev2["id"] != ev["id"] and json.loads(ev2["data"])["rev"] == ev2["id"]
    # `?rev=` equal to the current rev: no initial event, heartbeats only
    c2 = s.stream(rev=ev2["id"])
    assert c2.block() == {"retry": "3000"}
    seen = [c2.block(5) for _ in range(2)]
    assert all(b == {"comment": "hb"} for b in seen)
    # a stale `?rev=` gets the current snapshot at once
    c3 = s.stream(rev=ev["id"])
    assert c3.event()["id"] == ev2["id"]


def test_stream_pushes_each_distinct_change_once(mk):
    s = mk()
    c = s.stream()
    first = c.event()["id"]
    ids = [first]
    for cost in (2.0, 3.0):
        ledger_run_end(s.cfg, ACTIVE, cost)
        ids.append(c.event()["id"])
    assert len(set(ids)) == 3


# ---- load guard (IDEA-008) ---------------------------------------------------------------

def test_zero_clients_no_work_no_thread(mk):
    s = mk()
    time.sleep(0.5)                                 # ten ticks' worth; a negative assertion
    assert (s.cache.builds, s.cache.watermark_checks) == (0, 0)
    assert not snapshotter_alive()
    s.get()                                         # polling alone never starts it
    assert not snapshotter_alive()


def test_builds_do_not_grow_with_clients(mk, monkeypatch):
    calls = []
    real = Q.dashboard_payload
    monkeypatch.setattr(Q, "dashboard_payload",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    out = {}
    for n in (1, 8):
        calls.clear()
        s = mk(refresh_s=0.05, max_stale_s=0.5)
        t0 = time.monotonic()
        clients = [s.stream() for _ in range(n)]
        for c in clients:
            assert c.event()
        time.sleep(1.2)
        elapsed = time.monotonic() - t0
        assert s.cache.builds <= int(elapsed / 0.5) + 1, (n, s.cache.builds, elapsed)
        assert len(calls) == s.cache.builds             # only the builder reads the queue
        out[n] = (s.cache.builds, s.cache.watermark_checks)
        for c in clients:
            c.close()
    # the same work for 1 and 8 clients (the windows differ by a tick or two at most)
    assert abs(out[1][0] - out[8][0]) <= 1, out
    assert abs(out[1][1] - out[8][1]) <= 3, out


def test_one_change_is_one_build_and_one_event_per_client(mk):
    s = mk()
    clients = [s.stream() for _ in range(8)]
    for c in clients:
        c.event()
    before = s.cache.builds
    ledger_run_end(s.cfg, ACTIVE, 4.0)
    evs = [c.event() for c in clients]
    assert len({e["id"] for e in evs}) == 1
    time.sleep(0.4)                                 # several more ticks: nothing else arrives
    assert s.cache.builds == before + 1
    for c in clients:
        assert c.event(0.2) is None


def test_thread_is_gone_after_the_last_client_leaves(mk):
    s = mk()
    cs = [s.stream() for _ in range(3)]
    for c in cs:
        c.event()
    assert snapshotter_alive()
    for c in cs:
        c.close()
    assert until(lambda: not snapshotter_alive(), 2 * s.cache.refresh_s + 0.5)
    assert until(lambda: s.hub.stats()["snapshot_clients"] == 0)
    c = s.stream()                                  # and it comes back for the next client
    assert c.event() and snapshotter_alive()


# ---- backpressure ------------------------------------------------------------------------

def test_a_client_that_stops_reading_does_not_delay_others(mk, monkeypatch):
    n = [0]

    def big(cfg):
        n[0] += 1
        return {"schema": "nw.dashboard.v1", "rev": f"{n[0]:016d}", "pad": "x" * 200_000}

    inflight = {}                       # handler thread -> when its current event write began
    real = ST.write_event

    def spy(wfile, event, ident, data):
        tid = threading.get_ident()
        inflight[tid] = time.monotonic()
        try:
            real(wfile, event, ident, data)
        finally:
            inflight.pop(tid, None)
    monkeypatch.setattr(ST, "write_event", spy)

    send_timeout = 0.5
    s = mk(refresh_s=0.05, max_stale_s=0.05, builder=big, send_timeout_s=send_timeout)
    slow = s.stream(rcvbuf=4096)                    # never reads past the headers
    fast = s.stream()
    assert until(lambda: s.hub.stats()["snapshot_clients"] == 2)
    got, last, blocked_since, dropped_at = 0, time.monotonic(), None, None
    gaps = []
    # the reader keeps receiving while the slow client's buffers fill and it is dropped
    while got < 500:
        ev = fast.event(10)
        assert ev is not None
        now = time.monotonic()
        gaps.append(now - last)
        last, got = now, got + 1
        if blocked_since is None:                   # a write stuck for a while = buffers full
            stuck = [t for t in list(inflight.values()) if now - t > 0.2]
            blocked_since = min(stuck) if stuck else None
        if s.hub.pool.used == 1:
            dropped_at = now
            break
    assert dropped_at is not None, "the stalled client was never dropped"
    assert blocked_since is not None, "the slow client was dropped without a blocked write"
    # dropped by the stream's own send timeout, not by the 30 s handler timeout
    assert dropped_at - blocked_since < send_timeout + 2, (dropped_at - blocked_since)
    assert s.hub.stats()["snapshot_clients"] == 1 and max(gaps) < 5
    assert fast.event(10)                           # still being served
    slow.close()


def test_server_close_ends_open_streams_and_frees_their_slots(mk):
    s = mk()
    cs = [s.stream() for _ in range(2)]
    for c in cs:
        c.event()
    s.srv.shutdown()
    join(s.t, 10)
    s.srv.server_close()
    assert all(c.closed_by_server(5) for c in cs)
    assert until(lambda: s.hub.pool.used == 0 and not snapshotter_alive())


def test_a_failing_cache_call_leaves_the_snapshotter_running(mk, capfd):
    calls = [0]

    def flaky(cfg):
        calls[0] += 1
        if 2 <= calls[0] <= 5:
            raise RuntimeError("builder down")
        return {"schema": "nw.dashboard.v1", "rev": "a" if calls[0] == 1 else "b"}

    s = mk(refresh_s=0.05, max_stale_s=0.05, builder=flaky)
    c = s.stream()
    assert c.event()["id"] == "a"
    assert c.event(10)["id"] == "b"                 # after the failed ticks it recovers
    assert calls[0] > 5 and snapshotter_alive()
    err = capfd.readouterr().err
    assert err.count("snapshot failed: RuntimeError: builder down") == 1   # repeats held back


def test_a_builder_that_stays_down_ends_the_streams_with_an_error_event(mk):
    """Heartbeats must not keep a page looking current once no snapshot can be built."""
    down = [False]

    def flaky(cfg):
        if down[0]:
            raise RuntimeError("ledger unreadable")
        return {"schema": "nw.dashboard.v1", "rev": "a"}

    s = mk(refresh_s=0.05, max_stale_s=0.05, builder=flaky, heartbeat_s=0.1)
    s.hub.fail_after_s = 0.3
    c = s.stream()
    assert c.event()["id"] == "a"
    down[0] = True
    assert c.closed_by_server(10)                   # ended, not kept alive by heartbeats
    assert until(lambda: s.hub.pool.used == 0)
    assert s.get()[0] == 503                        # what the page's poll then sees
    assert s.stream().status == 503
    down[0] = False                                 # and it recovers
    assert s.stream().status == 200


# ---- the cap -----------------------------------------------------------------------------

def test_cap_answers_503_and_a_slot_frees_on_any_end(mk):
    s = mk(heartbeat_s=0.1, max_clients=2)
    assert s.hub.pool.max_clients == 2
    a, b = s.stream(), s.stream()
    a.event(), b.event()
    c = s.stream()
    assert c.status == 503 and c.headers["retry-after"] == "5"
    assert "text/event-stream" not in c.headers.get("content-type", "")
    assert s.get()[0] == 200                        # polling still works
    a.close()                                       # a client disconnect frees a slot
    assert until(lambda: s.hub.pool.used == 1)
    d = s.stream()
    assert d.status == 200 and d.event()
    # a server-side auth denial frees a slot as well
    s.srv.stream_auth_ok = lambda h: False
    assert d.closed_by_server() and b.closed_by_server()
    assert until(lambda: s.hub.pool.used == 0)
    s.srv.stream_auth_ok = lambda h: True
    assert s.stream().status == 200


def test_a_failing_cache_answers_503_and_leaks_no_slot(mk):
    def boom(cfg):
        raise RuntimeError("no snapshot")
    s = mk(builder=boom)
    c = s.stream()
    assert c.status == 503
    assert s.hub.pool.used == 0 and until(lambda: not snapshotter_alive())


def test_max_clients_setting():
    assert ST.max_clients({}) == 8
    assert ST.max_clients({"agentic": {"dashboard": {"max_clients": 3}}}) == 3
    for bad in (0, -1, True, "x", float("inf"), None):
        assert ST.max_clients({"agentic": {"dashboard": {"max_clients": bad}}}) == 8


# ---- auth --------------------------------------------------------------------------------

def test_stream_needs_the_same_auth_as_every_call(mk):
    """SPEC-0428: no credential on loopback (an Authorization header is ignored), the same
    Host check as every call; a refusal takes no slot and starts no snapshotter."""
    s = mk()
    assert s.stream(host=False).status == 403
    assert s.hub.pool.used == 0 and not snapshotter_alive()
    assert s.stream(token="junk").status == 200
    assert s.stream().status == 200


def test_heartbeat_reruns_the_auth_check_and_closes_on_denial(mk):
    s = mk(heartbeat_s=0.1)
    calls = []
    s.srv.stream_auth_ok = lambda h: calls.append(1) or len(calls) < 3
    c = s.stream()
    assert c.event()
    assert c.closed_by_server() and len(calls) == 3
    assert until(lambda: s.hub.pool.used == 0)


def test_auth_is_rechecked_even_when_snapshots_keep_arriving(mk):
    n = [0]

    def busy(cfg):
        n[0] += 1
        return {"schema": "nw.dashboard.v1", "rev": f"{n[0]:08d}"}

    # a new rev every tick (0.05 s), far faster than the 0.3 s heartbeat: no hb is ever due
    s = mk(refresh_s=0.05, max_stale_s=0.05, builder=busy, heartbeat_s=0.3)
    calls = []
    s.srv.stream_auth_ok = lambda h: calls.append(1) or len(calls) < 2
    c = s.stream()
    assert c.event()
    t0 = time.monotonic()
    assert c.closed_by_server(10)
    assert len(calls) == 2 and time.monotonic() - t0 < 2
    assert until(lambda: s.hub.pool.used == 0)


# ---- hub units ---------------------------------------------------------------------------

def test_slot_pool():
    p = ST.SlotPool(2)
    assert [p.acquire(), p.acquire(), p.acquire()] == [True, True, False]
    p.release()
    assert p.acquire() and p.used == 2


def test_subscriber_mailbox_keeps_only_the_latest():
    sub = ST.Subscriber(None)
    for r in ("a", "b", "c"):
        sub.offer({"rev": r})
    sub.offer({"rev": "c"})
    assert sub.take(0)["rev"] == "c" and sub.take(0) is None
    assert ST.Subscriber("a").take(0) is None


def test_a_stale_initial_offer_never_replaces_a_newer_delivered_snapshot():
    sub = ST.Subscriber(None)
    sub.offer({"rev": "new"})                       # the snapshotter got there first
    sub.offer({"rev": "old"}, initial=True)         # the subscribe-time cache read, older
    assert sub.take(0)["rev"] == "new" and sub.take(0) is None
    fresh = ST.Subscriber(None)
    fresh.offer({"rev": "x"}, initial=True)         # the normal case: it seeds the mailbox
    assert fresh.take(0)["rev"] == "x"


# ---- read-only ---------------------------------------------------------------------------

def test_streams_leave_the_data_dir_untouched_outside_the_dashboard_dir(mk):
    s = mk()

    def outside():
        return {k: v for k, v in tree(s.fx["data"]).items()
                if not k.startswith("agentic/dashboard")}
    ledger_run_end(s.cfg, ACTIVE, 0.5)
    before = outside()
    c = s.stream()
    c.event()
    s.stream(host=False)                            # a refusal: audited inside the dashboard dir
    s.stream().close()
    assert outside() == before

