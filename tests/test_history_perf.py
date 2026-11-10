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
