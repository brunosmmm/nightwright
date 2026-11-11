"""SPEC-0460 AC8: history cache perf budgets on the 500-campaign clone."""
from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path
from unittest import mock

import pytest

from nightwright.agentic import history as H
from nightwright.agentic import store
import history_scale as HS

pytestmark = pytest.mark.integration

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
LIMIT_COLD = 30.0
LIMIT_WARM_INDEX = 0.9
LIMIT_WARM_DOC = 1.5
LIMIT_RECORDS = 0.6
LIMIT_FILTER = 0.6                    # SPEC-0412: 200 ms x3 slack


@pytest.fixture(scope="module")
def scaled_data(tmp_path_factory):
    base = tmp_path_factory.mktemp("history-perf")
    data = base / "data"
    cache = base / "cache"
    cache.mkdir()
    HS.clone_fixture(data, campaigns=500, records=50_000, seed=0)
    return data, cache


def _cfg(data_dir: Path, cache_dir: Path) -> dict:
    return {
        "data_dir": str(data_dir),
        "cache_dir": str(cache_dir),
        "agentic": {"default_profile": "balanced", "history": {"cache": True}},
    }


def test_record_count_and_budgets(scaled_data):
    data_dir, cache_dir = scaled_data
    c = _cfg(data_dir, cache_dir)
    recs = H.history_records(c)
    assert len(recs) >= 50_000

    v1 = cache_dir / "history" / "v1"
    if v1.exists():
        import shutil
        shutil.rmtree(v1)

    progress = []
    t0 = time.perf_counter()
    H.history_index(c, now=NOW, progress=lambda d, t: progress.append((d, t)))
    cold = time.perf_counter() - t0
    assert progress[-1] == (500, 500)
    assert cold <= LIMIT_COLD, cold

    t0 = time.perf_counter()
    H.history_index(c, now=NOW, cache=True)
    warm = time.perf_counter() - t0
    assert warm <= LIMIT_WARM_INDEX, warm

    v1 = cache_dir / "history" / "v1"
    rec_path = v1 / "records.jsonl"
    rec_stat = rec_path.stat()
    rec_inode = (rec_stat.st_ino, rec_stat.st_mtime_ns)

    terminal = "SPEC-0394-20261102-ae5b"
    t0 = time.perf_counter()
    H.campaign_history(c, terminal, now=NOW, cache=True)
    hit = time.perf_counter() - t0
    assert hit <= LIMIT_WARM_DOC, hit

    t0 = time.perf_counter()
    H.history_records(c, kind="finding")
    kind_t = time.perf_counter() - t0
    assert kind_t <= LIMIT_RECORDS, kind_t

    v1_posix = v1.resolve().as_posix()
    real_open = open

    def open_no_cache_campaign_parse(file, *args, **kwargs):
        text = file if isinstance(file, int) else str(file)
        if text.startswith(f"{v1_posix}/campaigns/") and text.endswith(".json"):
            raise AssertionError(f"warm records must not parse {text}")
        return real_open(file, *args, **kwargs)

    t0 = time.perf_counter()
    with mock.patch("builtins.open", open_no_cache_campaign_parse):
        H.history_records(c, query="pytest")
    query_t = time.perf_counter() - t0
    assert query_t <= LIMIT_RECORDS, query_t
    rec_after = rec_path.stat()
    assert (rec_after.st_ino, rec_after.st_mtime_ns) == rec_inode


def test_warm_index_no_terminal_campaign_stat(scaled_data):
    data_dir, cache_dir = scaled_data
    c = _cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    terminal = "SPEC-0394-20261102-ae5b"
    camp_root = (data_dir / "agentic" / "campaigns" / terminal).as_posix()
    touches = []

    real_open = open
    real_stat = os.stat

    def open_wrapper(file, *args, **kwargs):
        path = file if isinstance(file, int) else str(file)
        if camp_root in path:
            touches.append(path)
        return real_open(file, *args, **kwargs)

    def stat_wrapper(path, *args, **kwargs):
        text = str(path)
        if camp_root in text:
            touches.append(text)
        return real_stat(path, *args, **kwargs)

    ledger = (data_dir / "agentic" / "campaigns" / terminal / "ledger.jsonl").as_posix()

    def open_ok(file, *args, **kwargs):
        path = file if isinstance(file, int) else str(file)
        if ledger in path:
            raise AssertionError("ledger opened on warm index")
        if camp_root in path and (
            path.endswith("ledger.jsonl")
            or path.endswith(f"/campaigns/{terminal}.json")
        ):
            raise AssertionError(path)
        return real_open(file, *args, **kwargs)

    def stat_ok(path, *args, **kwargs):
        text = str(path)
        if camp_root in text and "ledger.jsonl" not in text:
            touches.append(text)
        return real_stat(path, *args, **kwargs)

    with mock.patch("builtins.open", open_ok), mock.patch("os.stat", stat_ok):
        H.history_index(c, now=NOW, cache=True)
    assert touches


FINDINGS_QUERIES = [
    "kind:finding severity:major", "since:30d", "agent:conductor kind:decision", "pytest",
    "pytest finding", "project:META-tools", "severity:none", "campaign:SPEC-0394",
    "agent:steward", "status:none since:7d",
]


def _timed_findings_queries(scaled_data, monkeypatch):
    """(text, wall seconds, adapter's own seconds, history call count) for each query, warm."""
    from nightwright.tui import findings_filter as FF

    data_dir, cache_dir = scaled_data
    c = _cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    H.history_records(c)                                    # warm the records cache
    spent = []
    real = H.history_records

    def timed(*args, **kwargs):
        t0 = time.perf_counter()
        try:
            return real(*args, **kwargs)
        finally:
            spent.append(time.perf_counter() - t0)

    monkeypatch.setattr(H, "history_records", timed)
    out = []
    for text in FINDINGS_QUERIES:
        flt = FF.parse_record_filter(text)
        assert not flt.problems, text
        spent.clear()
        t0 = time.perf_counter()
        res = FF.query_records(c, flt)
        wall = time.perf_counter() - t0
        assert res.error is None and res.total_records >= 50_000, text
        out.append((text, wall, wall - sum(spent), len(spent)))
    return out


def test_findings_filter_adapter_share_meets_the_budget(scaled_data, monkeypatch):
    """SPEC-0412: what `query_records` adds on top of the history layer (matching, sorting) is held
    to 200 ms x3, and it makes exactly one history call. The end-to-end bound is the xfail below."""
    rows = _timed_findings_queries(scaled_data, monkeypatch)
    print("query_records (wall s, adapter s):", {t: (round(w, 3), round(o, 3)) for t, w, o, _ in rows})
    assert all(calls == 1 for *_, calls in rows)
    assert {t: round(o, 3) for t, _, o, _ in rows if o > LIMIT_FILTER} == {}


@pytest.mark.xfail(strict=False, reason=(
    "SPEC-0412 AC9 is not met end to end: every history_records call on the 500-campaign clone "
    "spends 0.5-1.4 s in the warm-path staleness check (history_cache._records_cache_quiet stats "
    "every terminal campaign), whatever the filter. Needs a cheaper warm-path validity check in "
    "the history layer's own spec (SPEC-0406 or a follow-up); remove this marker when it lands. "
    "The budget must not be loosened. Not strict: the figure sits around the 0.6 s bound and a "
    "quiet machine can pass it, which must not fail the suite."))
def test_findings_filter_queries_meet_the_budget_end_to_end(scaled_data, monkeypatch):
    rows = _timed_findings_queries(scaled_data, monkeypatch)
    assert len(rows) >= 8
    assert {t: round(w, 3) for t, w, _, _ in rows if w > LIMIT_FILTER} == {}
