"""Live snapshot stream (SPEC-0402): `GET /api/stream` fans the shared `SnapshotCache` out over
Server-Sent Events. One snapshotter thread exists only while a stream client is connected; it
ticks the cache every `refresh_s` and hands a changed snapshot to every subscriber's one-slot
mailbox. Sockets are written by each client's own handler thread, never by the snapshotter, so a
slow client lags (and is dropped by the send timeout) without delaying the others. Read-only;
stdlib only; not imported by any queue-side module."""
from __future__ import annotations

import math
import select
import socket
import sys
import threading
import time

HEARTBEAT_S = 15.0              # a `: hb` comment this often (tests inject a shorter one)
SEND_TIMEOUT_S = 10.0           # a client whose socket stays unwritable this long is dropped
RETRY_MS = 3000
DEFAULT_MAX_CLIENTS = 8
ERROR_LOG_EVERY_S = 60.0        # the same snapshotter error is logged at most this often


def max_clients(cfg) -> int:
    """`agentic.dashboard.max_clients`; anything but a positive number falls back to 8."""
    ag = cfg.get("agentic")
    raw = ag.get("dashboard") if isinstance(ag, dict) else None
    v = raw.get("max_clients") if isinstance(raw, dict) else None
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 1
    return int(v) if ok else DEFAULT_MAX_CLIENTS


# ---- SSE writers -------------------------------------------------------------------------

def write_retry(wfile, ms: int = RETRY_MS) -> None:
    wfile.write(f"retry: {ms}\n\n".encode("ascii"))


def write_event(wfile, event: str, ident: str, data: bytes) -> None:
    """One SSE event. `data` is a single line (JSON without newlines)."""
    wfile.write(b"event: " + event.encode("ascii") + b"\nid: " + ident.encode("ascii")
                + b"\ndata: " + data + b"\n\n")


def write_comment(wfile, text: str = "hb") -> None:
    wfile.write(f": {text}\n\n".encode("ascii"))


def peer_gone(sock: socket.socket) -> bool:
    """True when the client closed or reset its end (a read-ready socket that yields EOF). A
    stream client sends nothing after its request, so any readable byte is not a reason to
    keep waiting either."""
    try:
        if not select.select([sock], [], [], 0)[0]:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


# ---- slots -------------------------------------------------------------------------------

class SlotPool:
    """The `max_clients` counter for every kind of open stream, under one lock."""

    def __init__(self, max_clients: int):
        self.max_clients = max_clients
        self._used = 0
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        with self._lock:
            if self._used >= self.max_clients:
                return False
            self._used += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._used = max(0, self._used - 1)

    @property
    def used(self) -> int:
        with self._lock:
            return self._used


# ---- hub ---------------------------------------------------------------------------------

class Subscriber:
    """A one-slot mailbox: only the latest snapshot not yet written is kept."""

    def __init__(self, known_rev):
        self.rev = known_rev            # the rev last handed to this client (or that it holds)
        self.box = None
        self.offered = False            # the snapshotter has handed over something
        self.closed = False
        self.event = threading.Event()
        self._lock = threading.Lock()

    def offer(self, snap: dict, *, initial: bool = False) -> None:
        """Hand over `snap`. An `initial` one (read from the cache on subscribe) never replaces
        what the snapshotter already delivered: that is at least as new."""
        with self._lock:
            if initial and self.offered:
                return
            self.offered = self.offered or not initial
            if snap["rev"] == self.rev:
                return
            self.rev, self.box = snap["rev"], snap
            self.event.set()

    def close(self) -> None:
        self.closed = True
        self.event.set()

    def take(self, timeout: float):
        """The pending snapshot, or None after `timeout` (or once closed)."""
        self.event.wait(timeout)
        with self._lock:
            snap, self.box = self.box, None
            if self.box is None and not self.closed:
                self.event.clear()
            return snap


class Hub:
    def __init__(self, cache, pool: SlotPool, refresh_s: float | None = None):
        self.cache, self.pool = cache, pool
        self.refresh_s = cache.refresh_s if refresh_s is None else refresh_s
        self.builds_published = 0
        self._subs: list[Subscriber] = []
        self._lock = threading.Lock()
        self._stop = None               # the running snapshotter's own stop event
        self._thread = None
        self._last_rev = None
        self._last_err = (None, 0.0)    # (message, when) of the last logged snapshotter error

    def subscribe(self, known_rev=None):
        """A `Subscriber` whose mailbox already holds the current snapshot unless it is
        `known_rev`; None when the pool is full. Raises when the cache cannot produce one."""
        if not self.pool.acquire():
            return None
        sub = Subscriber(known_rev)
        with self._lock:
            self._subs.append(sub)
            if self._thread is None:
                self._stop = threading.Event()
                self._thread = threading.Thread(target=self._run, args=(self._stop,),
                                                name="nw-dashboard-snapshotter", daemon=True)
                self._last_rev = None
                self._thread.start()
        try:
            sub.offer(self.cache.get(), initial=True)     # not a forced rebuild: the cache decides
        except BaseException:
            self.unsubscribe(sub)
            raise
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            if sub not in self._subs:
                return
            self._subs.remove(sub)
            if not self._subs and self._stop is not None:
                self._stop.set()                # the thread exits at once; a new one may start
                self._stop = self._thread = None
        self.pool.release()

    def close(self) -> None:
        with self._lock:
            subs = list(self._subs)
        for s in subs:
            s.close()

    def _run(self, stop: threading.Event) -> None:
        while not stop.wait(self.refresh_s):
            try:
                snap = self.cache.get()
            except Exception as e:              # noqa: BLE001 - retry next tick, never die
                self._log_error(f"{type(e).__name__}: {e}")
                continue
            with self._lock:
                if stop.is_set():
                    return
                if snap["rev"] != self._last_rev:
                    if self._last_rev is not None:
                        self.builds_published += 1
                    self._last_rev = snap["rev"]
                subs = list(self._subs)
            for s in subs:
                s.offer(snap)

    def _log_error(self, msg: str) -> None:
        """To stderr; a repeat of the same message is held back for `ERROR_LOG_EVERY_S`."""
        now = time.monotonic()
        last_msg, at = self._last_err
        if msg == last_msg and now - at < ERROR_LOG_EVERY_S:
            return
        self._last_err = (msg, now)
        print(f"nw dashboard: snapshot failed: {msg}", file=sys.stderr)

    @property
    def thread_alive(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def stats(self) -> dict:
        with self._lock:
            n = len(self._subs)
        return {"snapshot_clients": n, "builds_published": self.builds_published}
