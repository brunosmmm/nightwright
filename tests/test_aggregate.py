"""Aggregation math: interval union, the gap/min-block model, and parallelism (SPEC-0001)."""
import datetime as dt
from zoneinfo import ZoneInfo

from nightwright import aggregate
import pytest

pytestmark = pytest.mark.fast


def _dtu(h, m):
    return dt.datetime(2026, 6, 1, h, m, tzinfo=dt.timezone.utc)


# ---- union_secs -------------------------------------------------------------

def test_union_empty():
    assert aggregate.union_secs([]) == 0.0


def test_union_disjoint():
    iv = [(_dtu(10, 0), _dtu(10, 30)), (_dtu(11, 0), _dtu(11, 15))]
    assert aggregate.union_secs(iv) == (30 + 15) * 60


def test_union_overlapping_is_deduped():
    iv = [(_dtu(10, 0), _dtu(10, 40)), (_dtu(10, 20), _dtu(11, 0))]
    assert aggregate.union_secs(iv) == 60 * 60  # 10:00–11:00 merged


def test_union_adjacent_touching():
    iv = [(_dtu(10, 0), _dtu(10, 30)), (_dtu(10, 30), _dtu(11, 0))]
    assert aggregate.union_secs(iv) == 60 * 60


# ---- gap / min-block model --------------------------------------------------

def _cfg():
    return {"gap_minutes": 15, "min_block_seconds": 60, "_tz": ZoneInfo("America/New_York")}


def _ev(ts, kind="prompt"):
    return {"ts": ts, "cwd": "/x", "branch": "main", "session": "S", "uuid": None, "kind": kind}


def _patch(monkeypatch, streams):
    monkeypatch.setattr(aggregate, "load_all_streams", lambda cfg: (streams, set()))
    monkeypatch.setattr(aggregate, "read_meetings", lambda cfg: [])
    monkeypatch.setattr(aggregate, "resolve_topic", lambda ev, ov, rr: ("T", False))


def test_gap_model_credits_gap_then_min_block(monkeypatch):
    # 14:00, 14:05 (5m<=15m gap -> 300s), 14:35 (30m>15m gap -> min_block); last -> min_block
    stream = [_ev(_dtu(14, 0)), _ev(_dtu(14, 5)), _ev(_dtu(14, 35))]
    _patch(monkeypatch, [stream])
    day_topic, _, _, _ = aggregate.build(_cfg(), [], None)
    # 300 (0->1) + 60 (1->2, gap exceeded) + 60 (trailing) = 420
    assert round(sum(day_topic["2026-06-01"].values())) == 420


def test_parallel_streams_bill_independently(monkeypatch):
    # two concurrent streams over the same 10 minutes: billed = 2x, wall = 1x
    s1 = [_ev(_dtu(14, 0)), _ev(_dtu(14, 10))]
    s2 = [_ev(_dtu(14, 0)), _ev(_dtu(14, 10))]
    _patch(monkeypatch, [s1, s2])
    day_topic, day_intervals, _, _ = aggregate.build(_cfg(), [], None)
    billed = sum(day_topic["2026-06-01"].values())
    wall = aggregate.union_secs(day_intervals["2026-06-01"])
    assert round(billed) == 2 * round(wall)  # full credit per concurrent stream


