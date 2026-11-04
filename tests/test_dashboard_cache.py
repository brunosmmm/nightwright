"""SPEC-0401: the lazy shared `SnapshotCache` — no work without callers, one build for many
concurrent callers, the `refresh_s` / `max_stale_s` rules, and a ledger-only change reaching
a new `rev` without any `events.jsonl` line."""
import threading

import pytest

from fixtures.dashboard_data import ACTIVE, seed
from nightwright.agentic import events as E
from nightwright.agentic import schemas as SC
from nightwright.agentic import store as S
from nightwright.agentic.dashboard import snapshot as SN
from waits import join, until


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Counting:
    def __init__(self, rev="r0"):
        self.calls = 0
        self.rev = rev

    def __call__(self, cfg):
        self.calls += 1
        return {"rev": self.rev, "n": self.calls}


class Mark:
    def __init__(self):
        self.value = 0
        self.calls = 0

    def __call__(self, cfg):
        self.calls += 1
        return (self.value,)


CFG = {"data_dir": "/nonexistent-nw-dashboard", "agentic": {}}


def make(**kw):
    clock, builder, mark = Clock(), Counting(), Mark()
    c = SN.SnapshotCache(CFG, clock=clock, builder=builder, watermark_fn=mark, **kw)
    return c, clock, builder, mark


def test_no_get_no_work():
    c, _clock, builder, mark = make()
    assert c.builds == 0 and c.watermark_checks == 0
    assert builder.calls == 0 and mark.calls == 0
    assert (c.refresh_s, c.max_stale_s) == (5, 30)


class GateLock:
    """The cache's lock, instrumented: counts the callers blocked on it."""
    def __init__(self):
        self.inner, self.meta, self.waiting = threading.Lock(), threading.Lock(), 0

    def __enter__(self):
        with self.meta:
            self.waiting += 1
        self.inner.acquire()
        with self.meta:
            self.waiting -= 1
        return self

    def __exit__(self, *a):
        self.inner.release()


def test_concurrent_callers_share_one_build():
    """The first build blocks until the other 19 threads are inside `get()` waiting on the
    lock (or, without the lock, until a second build starts): exactly one build."""
    c, _clock, _builder, _mark = make()
    gate = GateLock()
    c._lock = gate
    calls = []
    start = threading.Barrier(20)

    def slow(cfg):
        calls.append(1)
        assert until(lambda: gate.waiting == 19 or len(calls) > 1)
        return {"rev": "r0"}
    c.builder = slow
    seen = []
    lock = threading.Lock()

    def worker():
        start.wait()
        for _ in range(10):
            got = c.get()
            with lock:
                seen.append(id(got))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        join(t)
    assert len(calls) == 1 and c.builds == 1 and len(seen) == 200 and len(set(seen)) == 1
    assert c.watermark_checks == 1


def test_watermark_seen_only_after_refresh():
    c, clock, _builder, mark = make()
    first = c.get()
    mark.value = 1
    clock.t += 4.9
    assert c.get() is first and c.builds == 1 and c.watermark_checks == 1
    clock.t += 0.2                                       # past refresh_s since the last check
    second = c.get()
    assert second is not first and c.builds == 2 and c.watermark_checks == 2
    clock.t += 5.1                                       # checked again, nothing moved
    assert c.get() is second and c.builds == 2 and c.watermark_checks == 3


def test_max_stale_rebuilds_with_the_same_rev():
    c, clock, _builder, _mark = make()
    first = c.get()
    for _ in range(5):
        clock.t += 5.5
        c.get()
    assert c.builds == 1
    clock.t = 1000.0 + 30
    again = c.get()
    assert c.builds == 2 and again is not first and again["rev"] == first["rev"]


def test_watermark_is_taken_before_the_build():
    """A change that lands during a build is seen at the next check, not lost."""
    c, clock, _builder, mark = make()

    def changing(cfg):
        mark.value += 1                                  # lands while the build runs
        return {"rev": f"r{mark.value}"}
    c.builder = changing
    first = c.get()
    clock.t += c.refresh_s
    second = c.get()
    assert second is not first and c.builds == 2


def test_a_failing_build_keeps_the_move_visible():
    c, clock, builder, mark = make()
    first = c.get()
    mark.value = 1
    clock.t += c.refresh_s
    good = c.builder

    def boom(cfg):
        raise MemoryError("once")
    c.builder = boom
    with pytest.raises(MemoryError):
        c.get()
    c.builder = good
    assert c.get() is first                              # within refresh_s of that check
    clock.t += c.refresh_s
    again = c.get()
    assert again is not first and c.builds == 2 and builder.calls == 2


def test_a_failing_watermark_never_fails_get():
    """A watermark that raises (an unreadable file) counts as a move: `get()` rebuilds at most
    once per `refresh_s` and keeps returning a snapshot."""
    clock, builder, boom = Clock(), Counting(), []

    def mark(cfg):
        if boom:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        return (0,)
    c = SN.SnapshotCache(CFG, clock=clock, builder=builder, watermark_fn=mark,
                         refresh_s=5, max_stale_s=1000)
    first = c.get()
    boom.append(1)
    assert c.get() is first
    clock.t += 5
    second = c.get()
    assert second is not first and builder.calls == 2
    assert c.get() is second
    clock.t += 5
    assert c.get() is not second and builder.calls == 3


def test_settings_feed_the_cache():
    cfg = {"data_dir": "/x", "agentic": {"dashboard": {"refresh_s": 2, "max_stale_s": 9}}}
    c = SN.SnapshotCache(cfg)
    assert (c.refresh_s, c.max_stale_s) == (2, 9)


@pytest.fixture
def fx(tmp_path):
    return seed(tmp_path)


def test_max_stale_real_build_keeps_the_rev(fx):
    """On the real fixture and `build`: nothing moved, `max_stale_s` passes, the rebuild has
    new ages and `generated_at` but the same `rev`."""
    cfg = fx["cfg"]
    clock = Clock()
    c = SN.SnapshotCache(cfg, clock=clock)
    first = c.get()
    clock.t += c.max_stale_s
    again = c.get()
    assert c.builds == 2 and again is not first and again["rev"] == first["rev"]
    assert SN.compute_rev(again) == again["rev"]


def test_ledger_only_change_yields_new_rev(fx):
    cfg = fx["cfg"]
    clock = Clock()
    c = SN.SnapshotCache(cfg, clock=clock)
    r0 = c.get()["rev"]
    ev = E.events_path(cfg).stat()
    S.append_event(cfg, ACTIVE, "run.end", SC.with_defaults("run.end", {
        "persona": "implementer", "run_id": "009-implementer", "exit_code": 0,
        "is_error": False, "timed_out": False, "interrupted": False, "cost_usd": 0.9}))
    assert E.events_path(cfg).stat().st_size == ev.st_size          # no events.jsonl line
    clock.t += 1
    assert c.get()["rev"] == r0                                      # not before refresh_s
    clock.t += c.refresh_s
    r1 = c.get()["rev"]
    assert r1 != r0 and c.builds == 2
