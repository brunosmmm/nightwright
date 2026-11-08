"""SPEC-0472 history cache tests."""
import builtins
import datetime as dt
import hashlib
import json
import os
import shutil
from pathlib import Path
from unittest import mock

import pytest

from nightwright.agentic import conductor_cost as CC
from nightwright.agentic import history as H
from nightwright.agentic import history_cache as HC
from nightwright.agentic import schemas as SC
from nightwright.agentic import store
import history_scale

FIXTURE = Path(__file__).parent / "fixtures" / "agentic_history"
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def _hashes(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def data_dir(tmp_path):
    history_scale.copy_fixture(tmp_path / "data")
    return tmp_path / "data"


@pytest.fixture
def cache_dir(tmp_path, data_dir):
    path = tmp_path / "cache"
    path.mkdir()
    return path


def cfg(data_dir, cache_dir=None, **extra):
    out = {"data_dir": str(data_dir), "agentic": {"default_profile": "balanced"}, **extra}
    if cache_dir is not None:
        out["cache_dir"] = str(cache_dir)
    return out


def _install_live_campaign(data_dir, cid: str) -> None:
    src_id = "SPEC-0180-20261025-f78d"
    src = data_dir / "agentic" / "campaigns" / src_id
    dst = data_dir / "agentic" / "campaigns" / cid
    shutil.copytree(src, dst)
    state = json.loads((dst / "state.json").read_text())
    state["id"] = cid
    state["state"] = "IMPLEMENTING"
    state.pop("close", None)
    state.pop("last_gate", None)
    (dst / "state.json").write_text(json.dumps(state, indent=2) + "\n")
    kept = []
    for line in (dst / "ledger.jsonl").read_text().splitlines():
        row = json.loads(line)
        if row.get("type") == "transition" and row.get("to") in store.TERMINAL_STATES:
            continue
        if "campaign" in row:
            row = {**row, "campaign": cid}
        kept.append(json.dumps(row))
    (dst / "ledger.jsonl").write_text("\n".join(kept) + "\n")


def test_no_cache_dir_no_write_or_gap(data_dir, tmp_path):
    c = cfg(data_dir)
    before = _hashes(data_dir)
    H.campaign_history(c, "SPEC-0394-20261102-ae5b", now=NOW)
    H.history_index(c, now=NOW)
    H.history_records(c)
    assert _hashes(data_dir) == before
    assert not list(tmp_path.rglob("history"))


def test_cache_false_no_write(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir=data_dir / "agentic")
    before = _hashes(data_dir)
    doc = H.campaign_history(c, "SPEC-0394-20261102-ae5b", now=NOW, cache=False)
    assert "cache: not writable" not in doc.get("gaps", [])
    H.history_index(c, now=NOW, cache=False)
    assert _hashes(data_dir) == before
    assert not (cache_dir / "history").exists()


def test_not_writable_gap(data_dir, tmp_path):
    c = cfg(data_dir, cache_dir=tmp_path / "notadir")
    (tmp_path / "notadir").write_text("x")
    doc = H.campaign_history(c, "SPEC-0394-20261102-ae5b", now=NOW)
    assert "cache: not writable" in doc.get("gaps", [])
    idx = H.history_index(c, now=NOW)
    recs = H.history_records(c)
    unc_idx = H.history_index(cfg(data_dir), now=NOW)
    unc_recs = H.history_records(cfg(data_dir))
    assert idx == unc_idx
    assert recs == unc_recs


def test_v1_inside_data_dir_not_writable(data_dir):
    inner = data_dir / "inner-cache"
    inner.mkdir()
    c = cfg(data_dir, cache_dir=inner)
    doc = H.campaign_history(c, "SPEC-0394-20261102-ae5b", now=NOW)
    assert "cache: not writable" in doc.get("gaps", [])


def test_fresh_cache_matches_uncached(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    cid = "SPEC-0394-20261102-ae5b"
    unc_doc = H.campaign_history(c, cid, now=NOW, cache=False)
    H.history_index(c, now=NOW, cache=True)
    doc = H.campaign_history(c, cid, now=NOW, cache=True)
    assert doc == unc_doc
    unc_idx = H.history_index(c, now=NOW, cache=False)
    idx = H.history_index(c, now=NOW, cache=True)
    assert idx == unc_idx
    assert all("start" not in row and "live_end" not in row for row in idx)
    unc_recs = H.history_records(c, cache=False)
    recs = H.history_records(c, cache=True)
    assert recs == unc_recs
    assert all(SC.record_problems_v1(r) == [] for r in recs)
    later = NOW + dt.timedelta(hours=1)
    doc2 = H.campaign_history(c, cid, now=later, cache=True)
    assert doc2["generated_at"] == store.now_iso(later)
    assert doc2["phases"] == doc["phases"]


def test_writes_only_under_v1(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    before = _hashes(data_dir)
    H.history_index(c, now=NOW, cache=True)
    assert _hashes(data_dir) == before
    v1 = cache_dir / "history" / "v1"
    assert v1.is_dir() and (v1 / "sources.json").is_file()


def test_terminal_second_read_uses_cache(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    cid = "SPEC-0394-20261102-ae5b"
    H.history_index(c, now=NOW, cache=True)
    calls = []
    real = H._campaign_history

    def spy(*args, **kwargs):
        calls.append(args[1])
        return real(*args, **kwargs)

    with mock.patch.object(H, "_campaign_history", spy):
        H.campaign_history(c, cid, now=NOW, cache=True)
        H.history_index(c, now=NOW, cache=True)
    assert cid not in calls


def test_rebuild_cache(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    out = H.rebuild_cache(c, now=NOW)
    v1 = cache_dir / "history" / "v1"
    assert out["campaigns"] == len(list((data_dir / "agentic" / "campaigns").iterdir()))
    assert out["records"] == sum(1 for _ in (v1 / "records.jsonl").read_text().splitlines() if _.strip())
    sources = json.loads((v1 / "sources.json").read_text())
    assert sources.get("complete") is True


def test_tmp_files_ignored(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    v1 = cache_dir / "history" / "v1"
    junk = v1 / "index.jsonl.tmp-1-deadbeef"
    junk.write_text('{"campaign":"x"}\n')
    idx = H.history_index(c, now=NOW, cache=True)
    assert all(row["campaign"] != "x" for row in idx)


def test_cache_true_missing_cache_dir_gap(data_dir):
    c = cfg(data_dir)
    c["cache_dir"] = ""
    doc = H.campaign_history(c, "SPEC-0394-20261102-ae5b", now=NOW, cache=True)
    assert "cache: not writable" in doc.get("gaps", [])


def _path_str(path) -> str:
    if isinstance(path, (str, bytes)):
        return os.fsdecode(path)
    return str(path)


def test_warm_index_stat_spy(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    terminal = "SPEC-0394-20261102-ae5b"
    H.history_index(c, now=NOW, cache=True)
    prefix = (data_dir / "agentic" / "campaigns" / terminal).resolve().as_posix() + os.sep
    seen = []
    real_stat = os.stat
    real_open = builtins.open
    real_path_open = Path.open

    def stat_wrap(path, *args, **kwargs):
        text = _path_str(path)
        if text.startswith(prefix):
            seen.append(text)
        return real_stat(path, *args, **kwargs)

    def open_wrap(file, *args, **kwargs):
        text = _path_str(file)
        if text.startswith(prefix):
            seen.append(text)
        return real_open(file, *args, **kwargs)

    def path_open_wrap(self, *args, **kwargs):
        text = self.resolve().as_posix()
        if text.startswith(prefix):
            seen.append(text)
        return real_path_open(self, *args, **kwargs)

    attr_calls = []
    lw_calls = []

    with mock.patch("os.stat", stat_wrap), mock.patch("builtins.open", open_wrap), \
            mock.patch.object(Path, "open", path_open_wrap), \
            mock.patch.object(CC, "attribute", side_effect=lambda *a, **k: attr_calls.append(1)), \
            mock.patch.object(CC, "live_windows", side_effect=lambda *a, **k: lw_calls.append(1) or []):
        H.history_index(c, now=NOW, cache=True)
    assert not seen
    assert not attr_calls and not lw_calls


def test_ledger_append_stale_until_campaign_history(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    cid = "SPEC-0394-20261102-ae5b"
    other = "SPEC-0336-20261031-6558"
    H.history_index(c, now=NOW, cache=True)
    v1 = cache_dir / "history" / "v1"
    other_bytes = (v1 / "campaigns" / f"{other}.json").read_bytes()
    unc_before = H.campaign_history(c, cid, now=NOW, cache=False)
    idx_before = H.history_index(c, now=NOW, cache=False)
    recs_before = H.history_records(c, cache=False)
    ledger = data_dir / "agentic" / "campaigns" / cid / "ledger.jsonl"
    line = json.dumps({
        "schema": "nw.ledger.event.v1", "ts": store.now_iso(NOW), "seq": 9999,
        "campaign": cid, "type": "finding", "by": "tick", "severity": "nit", "title": "t", "detail": "d",
    }) + "\n"
    with ledger.open("a") as stream:
        stream.write(line)
    idx_mid = H.history_index(c, now=NOW, cache=True)
    recs_mid = H.history_records(c, campaign=cid, cache=True)
    row_unc = next(r for r in idx_before if r["campaign"] == cid)
    row_mid = next(r for r in idx_mid if r["campaign"] == cid)
    assert row_mid == row_unc
    assert recs_mid == [r for r in recs_before if r.get("campaign") == cid]
    unc_after = H.campaign_history(c, cid, now=NOW, cache=False)
    fresh = H.campaign_history(c, cid, now=NOW, cache=True)
    assert fresh == unc_after
    assert fresh != unc_before
    assert (v1 / "campaigns" / f"{other}.json").read_bytes() == other_bytes


def test_decision_append_updates_records(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    cid = "SPEC-0336-20261031-6558"
    H.history_index(c, now=NOW, cache=True)
    v1 = cache_dir / "history" / "v1"
    other_doc = (v1 / "campaigns" / "SPEC-0394-20261102-ae5b.json").read_bytes()
    other_line = next(
        line for line in (v1 / "index.jsonl").read_text().splitlines()
        if "SPEC-0394-20261102-ae5b" in line
    )
    before_recs = (v1 / "records.jsonl").read_text()
    dec = data_dir / "agentic" / "conductor" / "decisions.jsonl"
    row = {"schema": "nw.conductor.decision.v1", "seq": 99999, "ts": store.now_iso(NOW),
           "by": "conductor", "campaign": cid, "spec_id": "SPEC-0336", "event_id": "perf-test",
           "rule": "test", "kind": "resume", "action": None, "rationale": "r"}
    with dec.open("a") as stream:
        stream.write(json.dumps(row) + "\n")
    unc_row = next(r for r in H.history_index(c, now=NOW, cache=False) if r["campaign"] == cid)
    cache_row = next(r for r in H.history_index(c, now=NOW, cache=True) if r["campaign"] == cid)
    assert cache_row["records"] == unc_row["records"]
    assert cache_row["findings_by_severity"] == unc_row["findings_by_severity"]
    unc_recs = H.history_records(c, cache=False)
    cache_recs = H.history_records(c, cache=True)
    assert cache_recs == unc_recs
    assert sum(1 for r in cache_recs if r.get("id") == "decisions:perf-test") == 1
    unc_doc = H.campaign_history(c, cid, now=NOW, cache=False)
    cache_doc = H.campaign_history(c, cid, now=NOW, cache=True)
    assert cache_doc == unc_doc
    assert (v1 / "campaigns" / "SPEC-0394-20261102-ae5b.json").read_bytes() == other_doc
    assert other_line in (v1 / "index.jsonl").read_text()
    after = (v1 / "records.jsonl").read_text().count('"source": "decisions"')
    assert after == before_recs.count('"source": "decisions"') + 1


def test_query_matches_title_text_not_json(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    HC._records_mem.clear()
    unc = H.history_records(c, query="decision", cache=False)
    cached = H.history_records(c, query="decision", cache=True)
    assert cached == unc


def test_removed_campaign_pruned(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    cid = "SPEC-0394-20261102-ae5b"
    H.history_index(c, now=NOW, cache=True)
    shutil.rmtree(data_dir / "agentic" / "campaigns" / cid)
    idx = H.history_index(c, now=NOW, cache=True)
    recs = H.history_records(c, cache=True)
    assert all(r["campaign"] != cid for r in idx)
    assert all(r.get("campaign") != cid or r.get("source") not in ("ledger", "run") for r in recs)
    assert idx == H.history_index(c, now=NOW, cache=False)
    assert recs == H.history_records(c, cache=False)


def test_delete_v1_refills(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    v1 = cache_dir / "history" / "v1"
    shutil.rmtree(v1)
    H.history_index(c, now=NOW, cache=True)
    assert (v1 / "sources.json").is_file()


def test_symlink_v1_inside_data_dir_not_writable(data_dir, tmp_path):
    outer = tmp_path / "outer-cache"
    outer.mkdir()
    target = data_dir / "agentic" / "symlinked-v1"
    target.mkdir(parents=True)
    (outer / "history").symlink_to(target, target_is_directory=True)
    before = _hashes(data_dir)
    c = cfg(data_dir, outer)
    doc = H.campaign_history(c, "SPEC-0394-20261102-ae5b", now=NOW, cache=None)
    assert "cache: not writable" in doc.get("gaps", [])
    assert H.history_index(c, now=NOW, cache=None) == H.history_index(cfg(data_dir), now=NOW)
    assert H.history_records(c, cache=None) == H.history_records(cfg(data_dir))
    assert _hashes(data_dir) == before


def test_cached_gaps_include_runner_missing(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    (data_dir / "agentic" / "runner.jsonl").unlink()
    cid = "SPEC-0394-20261102-ae5b"
    unc = H.campaign_history(c, cid, now=NOW, cache=False)
    cached = H.campaign_history(c, cid, now=NOW, cache=True)
    assert cached["gaps"] == unc["gaps"]
    assert "runner.jsonl: missing" in cached["gaps"]


def test_query_matches_search_not_raw_json(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    cid = "SPEC-0394-20261102-ae5b"
    phrase = 'expected "foo"'
    ledger = data_dir / "agentic" / "campaigns" / cid / "ledger.jsonl"
    row = {
        "schema": "nw.ledger.event.v1", "ts": store.now_iso(NOW), "seq": 8888,
        "campaign": cid, "type": "finding", "by": "tick", "severity": "nit",
        "title": phrase, "detail": "quoted title query",
    }
    with ledger.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row) + "\n")
    H.rebuild_cache(c, now=NOW)
    HC._records_mem.clear()
    unc = H.history_records(c, query=phrase, cache=False)
    cached = H.history_records(c, query=phrase, cache=True)
    assert cached == unc
    assert cached


def test_nondirectory_in_campaigns_ignored(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    junk = data_dir / "agentic" / "campaigns" / "notes.txt"
    junk.write_text("not a campaign\n")
    idx = H.history_index(c, now=NOW, cache=True)
    assert idx == H.history_index(c, now=NOW, cache=False)


def test_live_campaign_json_unlinked_and_recomputed(data_dir, cache_dir):
    cid = "LIVE-20261107-abcd"
    _install_live_campaign(data_dir, cid)
    c = cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    v1 = cache_dir / "history" / "v1"
    camp = v1 / "campaigns" / f"{cid}.json"
    assert not camp.exists()
    camp.parent.mkdir(parents=True, exist_ok=True)
    camp.write_text('{"schema":"nw.campaign.history.v1","campaign":"stale"}\n')
    doc = H.campaign_history(c, cid, now=NOW, cache=True)
    unc = H.campaign_history(c, cid, now=NOW, cache=False)
    assert doc["header"]["outcome"] is None
    assert doc == unc
    assert not camp.exists()
    later = NOW + dt.timedelta(hours=3)
    assert H.campaign_history(c, cid, now=later, cache=True)["header"]["wall_s"] == (
        H.campaign_history(c, cid, now=later, cache=False)["header"]["wall_s"]
    )
    calls = []
    real = H._campaign_history

    def spy(*args, **kwargs):
        calls.append(args[1])
        return real(*args, **kwargs)

    terminal = "SPEC-0394-20261102-ae5b"
    with mock.patch.object(H, "_campaign_history", spy):
        H.campaign_history(c, terminal, now=NOW, cache=True)
        H.campaign_history(c, cid, now=later, cache=True)
        H.history_index(c, now=later, cache=True)
    assert terminal not in calls
    assert cid in calls


def test_cold_fill_sources_offset_not_past_parsed_bytes(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    cid = "SPEC-0336-20261031-6558"
    dec = data_dir / "agentic" / "conductor" / "decisions.jsonl"
    runner = data_dir / "agentic" / "runner.jsonl"
    marker = "decisions:cold-fill-cursor-gap"
    appended = []
    real_lw = CC.live_windows
    index_row = next(r for r in H.history_index(cfg(data_dir, cache_dir), now=NOW, cache=False) if r["campaign"] == cid)

    def lw_spy(cfg_in, **kwargs):
        row = {
            "schema": "nw.conductor.decision.v1", "seq": 888888, "ts": store.now_iso(NOW),
            "by": "conductor", "campaign": cid, "spec_id": "SPEC-0336",
            "event_id": "cold-fill-cursor-gap", "rule": "test", "kind": "resume",
            "action": None, "rationale": "r",
        }
        with dec.open("a") as stream:
            stream.write(json.dumps(row) + "\n")
        created = H._parse_ts(index_row["created_at"])
        closed = H._parse_ts(index_row["closed_at"])
        down = store.now_iso(created + dt.timedelta(hours=1))
        up = store.now_iso(closed - dt.timedelta(hours=1))
        with runner.open("a") as stream:
            stream.write(json.dumps({"ts": down, "kind": "stop"}) + "\n")
            stream.write(json.dumps({"ts": up, "kind": "start"}) + "\n")
        appended.append(1)
        return real_lw(cfg_in, **kwargs)

    shutil.rmtree(cache_dir / "history" / "v1", ignore_errors=True)
    size_before = dec.stat().st_size
    run_size_before = runner.stat().st_size
    source_writes = []
    real_write_json = HC._atomic_write_json

    def capture_sources(path, value):
        if path.name == "sources.json":
            source_writes.append(json.loads(json.dumps(value)))
        return real_write_json(path, value)

    with mock.patch.object(HC, "_atomic_write_json", capture_sources):
        with mock.patch.object(CC, "live_windows", lw_spy):
            H.history_index(c, now=NOW, cache=True)
    assert appended
    v1 = cache_dir / "history" / "v1"
    cold_sources = source_writes[0]
    off = cold_sources["logs"]["conductor/decisions.jsonl"]["offset"]
    run_off = cold_sources["logs"]["runner.jsonl"]["offset"]
    assert off <= size_before
    assert run_off <= run_size_before
    HC._records_mem.clear()
    unc_doc = H.campaign_history(c, cid, now=NOW, cache=False)
    cache_doc = H.campaign_history(c, cid, now=NOW, cache=True)
    assert cache_doc["phases"] == unc_doc["phases"]
    unc = H.history_records(c, cache=False)
    cache = H.history_records(c, cache=True)
    assert cache == unc
    assert sum(1 for r in cache if r.get("id") == marker) == 1
    (v1 / "sources.json").unlink()
    HC._records_mem.clear()
    cache2 = H.history_records(c, cache=True)
    assert sum(1 for r in cache2 if r.get("id") == marker) == 1


def test_cold_fill_opens_runner_once(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    shutil.rmtree(cache_dir / "history" / "v1", ignore_errors=True)
    lw_calls = []
    runner_opens = []
    real_lw = CC.live_windows
    real_open = builtins.open
    real_path_open = Path.open

    def lw_spy(cfg_in, **kwargs):
        lw_calls.append(1)
        return real_lw(cfg_in, **kwargs)

    def open_wrap(file, *args, **kwargs):
        text = _path_str(file)
        if text.endswith("/runner.jsonl") or text.endswith("runner.jsonl"):
            runner_opens.append(text)
        return real_open(file, *args, **kwargs)

    def path_open_wrap(self, *args, **kwargs):
        text = self.as_posix()
        if text.endswith("runner.jsonl"):
            runner_opens.append(text)
        return real_path_open(self, *args, **kwargs)

    with mock.patch("builtins.open", open_wrap), mock.patch.object(Path, "open", path_open_wrap), \
            mock.patch.object(CC, "live_windows", lw_spy):
        H.history_index(c, now=NOW, cache=True)
    assert len(runner_opens) == 1
    assert len(lw_calls) == 1


def _install_priced_session(data_dir, cache_dir):
    proj = data_dir / "projects" / "Meta-Tools"
    proj.mkdir(parents=True)
    sid = "priced-cache-session"
    in_window = "2026-11-03T02:00:00+00:00"
    (proj / f"{sid}.jsonl").write_text(json.dumps({
        "type": "assistant", "timestamp": in_window,
        "message": {"id": "m1", "model": "claude", "usage": {"input_tokens": 100000}},
    }) + "\n")
    sess = data_dir / "agentic" / "conductor" / "sessions.jsonl"
    sess.parent.mkdir(parents=True, exist_ok=True)
    sess.write_text(json.dumps({
        "schema": "nw.conductor.session.v1", "reader": "conductor", "session_id": sid,
        "ts": in_window, "project": "Meta-Tools",
    }) + "\n")
    prices = {"claude": {"input": 10.0, "output": 0.0,
                         "cache_read": 0.0, "cache_write_5m": 0.0, "cache_write_1h": 0.0}}
    c = cfg(data_dir, cache_dir, _root=str(data_dir / "projects"),
            config_dir=str(data_dir / "config"))
    c.setdefault("agentic", {})["conductor"] = {"prices": prices}
    return c


def test_priced_session_warm_index_matches_uncached(data_dir, cache_dir):
    c = _install_priced_session(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    terminal = "SPEC-0394-20261102-ae5b"
    prefix = (data_dir / "agentic" / "campaigns" / terminal).resolve().as_posix() + os.sep
    seen = []
    real_stat = os.stat
    real_open = builtins.open
    real_path_open = Path.open
    attr_calls = []
    lw_calls = []

    def stat_wrap(path, *args, **kwargs):
        text = _path_str(path)
        if text.startswith(prefix):
            seen.append(text)
        return real_stat(path, *args, **kwargs)

    def open_wrap(file, *args, **kwargs):
        text = _path_str(file)
        if text.startswith(prefix):
            seen.append(text)
        return real_open(file, *args, **kwargs)

    def path_open_wrap(self, *args, **kwargs):
        text = self.resolve().as_posix()
        if text.startswith(prefix):
            seen.append(text)
        return real_path_open(self, *args, **kwargs)

    with mock.patch("os.stat", stat_wrap), mock.patch("builtins.open", open_wrap), \
            mock.patch.object(Path, "open", path_open_wrap), \
            mock.patch.object(CC, "attribute", side_effect=lambda *a, **k: attr_calls.append(1)), \
            mock.patch.object(CC, "live_windows", side_effect=lambda *a, **k: lw_calls.append(1) or []):
        idx = H.history_index(c, now=NOW, cache=True)
    unc = H.history_index(c, now=NOW, cache=False)
    assert not attr_calls and not lw_calls
    assert not seen
    doc = H.campaign_history(c, terminal, now=NOW, cache=True)
    unc_doc = H.campaign_history(c, terminal, now=NOW, cache=False)
    priced = [r for r in idx if r.get("conductor_usd") is not None]
    assert priced, "expected at least one priced index row"
    assert doc["header"]["conductor_usd"] is not None
    by_c = {r["campaign"]: r for r in idx}
    by_u = {r["campaign"]: r for r in unc}
    for cid, row in by_c.items():
        assert row["conductor_usd"] == by_u[cid]["conductor_usd"]
    assert doc["header"]["conductor_usd"] == unc_doc["header"]["conductor_usd"]


def test_live_ledger_finding_in_history_records_without_campaign_history(data_dir, cache_dir):
    cid = "LIVE-20261107-ef01"
    _install_live_campaign(data_dir, cid)
    c = cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    ledger = data_dir / "agentic" / "campaigns" / cid / "ledger.jsonl"
    marker = "live-finding-without-popup"
    line = json.dumps({
        "schema": "nw.ledger.event.v1", "ts": store.now_iso(NOW), "seq": 4242,
        "campaign": cid, "type": "finding", "by": "tick", "severity": "nit",
        "title": marker, "detail": "d",
    }) + "\n"
    with ledger.open("a") as stream:
        stream.write(line)
    recs = H.history_records(c, campaign=cid, cache=True)
    assert any(marker in (r.get("title") or "") for r in recs)
    unc = H.history_records(c, campaign=cid, cache=False)
    assert recs == unc


def test_live_header_change_refreshes_cached_global_records(data_dir, cache_dir):
    cid = "LIVE-20261107-ef02"
    _install_live_campaign(data_dir, cid)
    dec = data_dir / "agentic" / "conductor" / "decisions.jsonl"
    ev = "live-header-refresh-0460"
    with dec.open("a") as stream:
        stream.write(json.dumps({
            "schema": "nw.conductor.decision.v1", "seq": 88001, "ts": store.now_iso(NOW),
            "by": "conductor", "campaign": cid, "spec_id": "SPEC-0180", "event_id": ev,
            "rule": "test", "kind": "resume", "action": None, "rationale": "r",
        }) + "\n")
    c = cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    state_path = data_dir / "agentic" / "campaigns" / cid / "state.json"
    state = json.loads(state_path.read_text())
    state["project"] = "Renamed-Project"
    state["spec_id"] = "SPEC-9999"
    state_path.write_text(json.dumps(state, indent=2) + "\n")
    HC._records_mem.clear()
    cached = [r for r in H.history_records(c, campaign=cid, cache=True) if r.get("source") == "decisions"]
    unc = [r for r in H.history_records(c, campaign=cid, cache=False) if r.get("source") == "decisions"]
    assert cached == unc
    assert cached and all(r.get("project") == "Renamed-Project" for r in cached)


def test_removed_campaign_deletes_cache_file(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    cid = "SPEC-0394-20261102-ae5b"
    H.history_index(c, now=NOW, cache=True)
    camp = cache_dir / "history" / "v1" / "campaigns" / f"{cid}.json"
    assert camp.is_file()
    shutil.rmtree(data_dir / "agentic" / "campaigns" / cid)
    H.history_index(c, now=NOW, cache=True)
    assert not camp.exists()


def test_atomic_write_uses_os_replace(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    replaces = []
    real = os.replace

    def spy(src, dst):
        replaces.append((Path(src).name, Path(dst).name))
        return real(src, dst)

    with mock.patch("os.replace", spy):
        H.history_index(c, now=NOW, cache=True)
    assert replaces
    assert any(".tmp-" in src and dst == "index.jsonl" for src, dst in replaces)


def test_sessions_partial_tail_does_not_rewrite_records(data_dir, cache_dir):
    c = _install_priced_session(data_dir, cache_dir)
    sess = data_dir / "agentic" / "conductor" / "sessions.jsonl"
    H.history_index(c, now=NOW, cache=True)
    v1 = cache_dir / "history" / "v1"
    before = (v1 / "records.jsonl").read_bytes()
    with sess.open("ab") as stream:
        stream.write(b'{"partial": true')
    HC._records_mem.clear()
    H.history_index(c, now=NOW, cache=True)
    assert (v1 / "records.jsonl").read_bytes() == before


def test_concurrent_live_and_global_cache_writes(data_dir, cache_dir):
    import multiprocessing as mp

    cid = "LIVE-20261107-ef03"
    _install_live_campaign(data_dir, cid)
    terminal = "SPEC-0336-20261031-6558"
    c = cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    dec = data_dir / "agentic" / "conductor" / "decisions.jsonl"
    marker = "decisions:mp-writer-0460"

    def apply_decision():
        cfg_b = cfg(data_dir, cache_dir)
        row = {
            "schema": "nw.conductor.decision.v1", "seq": 777001, "ts": store.now_iso(NOW),
            "by": "conductor", "campaign": terminal, "spec_id": "SPEC-0336",
            "event_id": "mp-writer-0460", "rule": "test", "kind": "resume",
            "action": None, "rationale": "r",
        }
        with dec.open("a") as stream:
            stream.write(json.dumps(row) + "\n")
        HC._records_mem.clear()
        H.history_records(cfg_b, cache=True)

    def refresh_live():
        cfg_b = cfg(data_dir, cache_dir)
        HC._records_mem.clear()
        H.history_index(cfg_b, now=NOW + dt.timedelta(minutes=5), cache=True)

    ctx = mp.get_context("fork")
    p1 = ctx.Process(target=apply_decision)
    p2 = ctx.Process(target=refresh_live)
    p1.start()
    p2.start()
    p1.join()
    p2.join()
    assert p1.exitcode == 0 and p2.exitcode == 0
    HC._records_mem.clear()
    cache_recs = H.history_records(c, cache=True)
    unc_recs = H.history_records(c, cache=False)
    assert cache_recs == unc_recs
    assert sum(1 for r in cache_recs if r.get("id") == marker) == 1


def test_turn_cursor_from_parsed_bytes_not_late_stat(data_dir, cache_dir):
    c = _install_priced_session(data_dir, cache_dir)
    proj = data_dir / "projects" / "Meta-Tools"
    sid = "priced-cache-session"
    transcript = proj / f"{sid}.jsonl"
    sess = data_dir / "agentic" / "conductor" / "sessions.jsonl"
    shutil.rmtree(cache_dir / "history" / "v1", ignore_errors=True)
    pre_t = transcript.stat().st_size
    pre_s = sess.stat().st_size
    appends = []
    real_turns = CC.read_turns
    def turns_spy(path):
        out = real_turns(path)
        if path == transcript:
            with transcript.open("a") as stream:
                stream.write(json.dumps({
                    "type": "assistant", "timestamp": "2026-11-02T11:00:00+00:00",
                    "message": {"id": "m2", "model": "claude",
                                "usage": {"input_tokens": 50000}},
                }) + "\n")
            appends.append("turns")
        return out

    with mock.patch.object(CC, "read_turns", turns_spy):
        H.history_index(c, now=NOW, cache=True)
    assert appends
    v1 = cache_dir / "history" / "v1"
    sources = json.loads((v1 / "sources.json").read_text())
    stored_t = sources["transcripts"][str(transcript.resolve())]
    assert stored_t["size"] == pre_t
    stored_s = sources["logs"]["conductor/sessions.jsonl"]
    assert stored_s["offset"] == pre_s
    HC._records_mem.clear()
    idx = H.history_index(c, now=NOW, cache=True)
    unc = H.history_index(c, now=NOW, cache=False)
    assert idx == unc
    sources2 = json.loads((v1 / "sources.json").read_text())
    stored_t2 = sources2["transcripts"][str(transcript.resolve())]
    assert stored_t2["size"] > pre_t
    read_calls = []
    real_turns2 = CC.read_turns

    def turns_spy2(path):
        read_calls.append(path)
        return real_turns2(path)

    with mock.patch.object(CC, "read_turns", turns_spy2):
        HC._records_mem.clear()
        H.history_index(c, now=NOW, cache=True)
    assert not read_calls


def test_cold_index_progress_once_per_campaign(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    shutil.rmtree(cache_dir / "history" / "v1", ignore_errors=True)
    n = len(list((data_dir / "agentic" / "campaigns").iterdir()))
    progress = []
    H.history_index(c, now=NOW, cache=True, progress=lambda d, t: progress.append((d, t)))
    assert progress[-1] == (n, n)
    assert len(progress) == n
    assert len(progress) == len(set(progress))
    progress.clear()
    H.history_index(c, now=NOW, cache=True, progress=lambda d, t: progress.append((d, t)))
    assert progress[-1] == (n, n)
    assert len(progress) == n


def test_global_gap_change_rewrites_terminal_campaigns(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    dec = data_dir / "agentic" / "conductor" / "decisions.jsonl"
    with dec.open("a", encoding="utf-8") as stream:
        stream.write("not-json\n")
    H.history_index(c, now=NOW, cache=True)
    for cid in (
        "SPEC-0336-20261031-6558",
        "SPEC-0394-20261102-ae5b",
        "IDEA-606-20261101-df50",
    ):
        unc = H.campaign_history(c, cid, now=NOW, cache=False)
        cached = H.campaign_history(c, cid, now=NOW, cache=True)
        assert cached["gaps"] == unc["gaps"]
    H.history_index(c, now=NOW, cache=True)
    cid = "SPEC-0336-20261031-6558"
    unc = H.campaign_history(c, cid, now=NOW, cache=False)
    cached = H.campaign_history(c, cid, now=NOW, cache=True)
    assert cached == unc


def test_runner_missing_gap_on_zero_runner_down_terminal(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    cid = "SPEC-0336-20261031-6558"
    H.history_index(c, now=NOW, cache=True)
    (data_dir / "agentic" / "runner.jsonl").unlink()
    H.history_index(c, now=NOW, cache=True)
    unc = H.campaign_history(c, cid, now=NOW, cache=False)
    cached = H.campaign_history(c, cid, now=NOW, cache=True)
    assert cached["gaps"] == unc["gaps"]
    assert "runner.jsonl: missing" in cached["gaps"]
    cached2 = H.campaign_history(c, cid, now=NOW, cache=True)
    assert cached2["gaps"] == unc["gaps"]


def test_decision_append_leaves_other_terminals_with_runner_gap(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    other = "SPEC-0394-20261102-ae5b"
    target = "SPEC-0336-20261031-6558"
    later = NOW + dt.timedelta(hours=1)
    H.history_index(c, now=NOW, cache=True)
    (data_dir / "agentic" / "runner.jsonl").unlink()
    H.history_index(c, now=later, cache=True)
    v1 = cache_dir / "history" / "v1"
    other_doc = (v1 / "campaigns" / f"{other}.json").read_bytes()
    other_line = next(
        line for line in (v1 / "index.jsonl").read_text().splitlines() if other in line
    )
    dec = data_dir / "agentic" / "conductor" / "decisions.jsonl"
    row = {
        "schema": "nw.conductor.decision.v1", "seq": 99998, "ts": store.now_iso(later),
        "by": "conductor", "campaign": target, "spec_id": "SPEC-0336", "event_id": "gap-isolation",
        "rule": "test", "kind": "resume", "action": None, "rationale": "r",
    }
    with dec.open("a") as stream:
        stream.write(json.dumps(row) + "\n")
    H.history_index(c, now=later, cache=True)
    assert (v1 / "campaigns" / f"{other}.json").read_bytes() == other_doc
    assert other_line in (v1 / "index.jsonl").read_text()
    unc = H.campaign_history(c, other, now=later, cache=False)
    cached = H.campaign_history(c, other, now=later, cache=True)
    assert cached["gaps"] == unc["gaps"]
    assert "runner.jsonl: missing" in cached["gaps"]


def test_decision_shrink_drops_removed_record(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    dec = data_dir / "agentic" / "conductor" / "decisions.jsonl"
    lines = dec.read_text().splitlines()
    dec.write_text("\n".join(lines[:-1]) + "\n")
    HC._records_mem.clear()
    unc = H.history_records(c, cache=False)
    cached = H.history_records(c, cache=True)
    assert cached == unc
    assert len(cached) == len(unc)


def test_write_lock_survives_v1_rebuild(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    v1 = cache_dir / "history" / "v1"
    lock = cache_dir / "history" / ".write.lock"
    assert lock.is_file()
    first_ino = lock.stat().st_ino
    H.rebuild_cache(c, now=NOW)
    assert lock.is_file()
    assert lock.stat().st_ino == first_ino
    assert v1.is_dir()
    assert not (v1 / ".write.lock").exists()
