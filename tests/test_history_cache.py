"""SPEC-0472 history cache tests."""
import builtins
import collections
import contextlib
import copy
import datetime as dt
import fcntl
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

pytestmark = pytest.mark.fast

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


def cfg(data_dir, cache_dir=None, *, use_cache=True, **extra):
    agentic = {"default_profile": "balanced", **(extra.pop("agentic", None) or {})}
    if use_cache and cache_dir is not None:
        agentic.setdefault("history", {})["cache"] = True  # SPEC-0536 opt-in
    out = {"data_dir": str(data_dir), "agentic": agentic, **extra}
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


def test_default_cfg_is_pure_with_cache_dir_and_writes_nothing(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir, use_cache=False)  # cache_dir set, no opt-in
    before = _hashes(data_dir)
    cid = "SPEC-0394-20261102-ae5b"
    pure = cfg(data_dir)
    doc = H.campaign_history(c, cid, now=NOW)
    assert doc == H.campaign_history(pure, cid, now=NOW, cache=False)
    assert "cache: not writable" not in doc.get("gaps", [])
    assert H.history_index(c, now=NOW) == H.history_index(pure, now=NOW, cache=False)
    assert H.history_records(c) == H.history_records(pure, cache=False)
    assert _hashes(data_dir) == before
    assert list(cache_dir.rglob("*")) == []


def test_config_opt_in_uses_cache(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    assert c["agentic"]["history"]["cache"] is True
    H.history_index(c, now=NOW)
    assert (cache_dir / "history" / "v1" / "version").read_bytes() == b"1\n"


def test_cache_dir_default_builds_v1_and_matches_pure(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    before = _hashes(data_dir)
    cid = "SPEC-0394-20261102-ae5b"
    pure_idx = H.history_index(cfg(data_dir), now=NOW, cache=False)
    H.history_index(c, now=NOW)
    v1 = cache_dir / "history" / "v1"
    assert (v1 / "version").read_bytes() == b"1\n"
    assert H.history_index(c, now=NOW) == pure_idx
    pure_doc = H.campaign_history(cfg(data_dir), cid, now=NOW, cache=False)
    cached = H.campaign_history(c, cid, now=NOW)
    assert {k: v for k, v in cached.items() if k != "generated_at"} == {
        k: v for k, v in pure_doc.items() if k != "generated_at"
    }
    assert _hashes(data_dir) == before


def test_cache_dir_file_unwritable_gap(data_dir, tmp_path):
    c = cfg(data_dir, cache_dir=tmp_path / "notadir")
    (tmp_path / "notadir").write_text("x")
    cid = "SPEC-0394-20261102-ae5b"
    doc = H.campaign_history(c, cid, now=NOW)
    assert "cache: not writable" in doc.get("gaps", [])


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
    doc = H.campaign_history(c, "SPEC-0394-20261102-ae5b", now=NOW, cache=True)
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
    doc = H.campaign_history(c, "SPEC-0394-20261102-ae5b", now=NOW, cache=True)
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
    assert doc2["generated_at"] == doc["generated_at"]
    unc_later = H.campaign_history(c, cid, now=later, cache=False)
    d2 = dict(doc2)
    d2.pop("generated_at", None)
    u2 = dict(unc_later)
    u2.pop("generated_at", None)
    assert d2 == u2
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
    rec_ino = (v1 / "records.jsonl").stat().st_ino
    idx = H.history_index(c, now=NOW, cache=True)
    assert junk.is_file()
    assert (v1 / "records.jsonl").stat().st_ino == rec_ino
    assert all(row["campaign"] != "x" for row in idx)
    H.rebuild_cache(c, now=NOW)
    assert not junk.exists()
    assert H.history_index(c, now=NOW, cache=True) == H.history_index(c, now=NOW, cache=False)


def test_campaign_id_with_tmp_substring_not_orphan(cache_dir):
    fake_id = "IDEA.tmp-1015-20261108-faf2"
    v1 = cache_dir / "history" / "v1"
    path = v1 / "campaigns" / f"{fake_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")
    assert not HC._is_tmp(path.name)
    HC._unlink_orphan_temps(v1)
    assert path.is_file()


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
    v1 = (cache_dir / "history" / "v1").resolve().as_posix()
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

    ledger = (data_dir / "agentic" / "campaigns" / terminal / "ledger.jsonl").resolve().as_posix()

    def stat_ok(path, *args, **kwargs):
        text = _path_str(path)
        if text == ledger or text.startswith(prefix) and "ledger.jsonl" in text:
            return real_stat(path, *args, **kwargs)
        if text.startswith(prefix):
            seen.append(text)
        return real_stat(path, *args, **kwargs)

    def open_wrap(file, *args, **kwargs):
        text = _path_str(file)
        camp_prefix = f"{v1}/campaigns/"
        if text.startswith(camp_prefix) and text.endswith(".json"):
            raise AssertionError(f"cache warm index must not open {text}")
        if text == f"{v1}/records.jsonl":
            raise AssertionError(f"cache warm index must not open {text}")
        if ledger in text or text.endswith("ledger.jsonl") and text.startswith(prefix):
            raise AssertionError("ledger opened on warm index")
        if text.startswith(prefix) and (
            text.endswith("ledger.jsonl")
            or "/campaigns/" in text and text.endswith(".json")
            or text.endswith("records.jsonl")
        ):
            raise AssertionError(f"unexpected open {text}")
        return real_open(file, *args, **kwargs)

    def path_open_wrap(self, *args, **kwargs):
        text = self.resolve().as_posix()
        camp_prefix = f"{v1}/campaigns/"
        if text.startswith(camp_prefix) and text.endswith(".json"):
            raise AssertionError(f"cache warm index must not open {text}")
        if text == f"{v1}/records.jsonl":
            raise AssertionError(f"cache warm index must not open {text}")
        if text.startswith(prefix):
            seen.append(text)
        return real_path_open(self, *args, **kwargs)

    with mock.patch("os.stat", stat_ok), mock.patch("builtins.open", open_wrap), \
            mock.patch.object(Path, "open", path_open_wrap), \
            mock.patch.object(CC, "attribute", side_effect=lambda *a, **k: attr_calls.append(1)), \
            mock.patch.object(CC, "live_windows", side_effect=lambda *a, **k: lw_calls.append(1) or []):
        H.history_index(c, now=NOW, cache=True)
    assert seen
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
    row_unc = next(r for r in H.history_index(c, now=NOW, cache=False) if r["campaign"] == cid)
    row_mid = next(r for r in idx_mid if r["campaign"] == cid)
    assert row_mid == row_unc
    recs_unc = [r for r in H.history_records(c, campaign=cid, cache=False)]
    assert recs_mid == recs_unc
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
    doc = H.campaign_history(c, "SPEC-0394-20261102-ae5b", now=NOW, cache=True)
    assert "cache: not writable" in doc.get("gaps", [])
    assert H.history_index(c, now=NOW, cache=True) == H.history_index(cfg(data_dir), now=NOW)
    assert H.history_records(c, cache=True) == H.history_records(cfg(data_dir))
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

    ledger = (data_dir / "agentic" / "campaigns" / terminal / "ledger.jsonl").resolve().as_posix()

    def stat_ok(path, *args, **kwargs):
        text = _path_str(path)
        if text.startswith(prefix) and "ledger.jsonl" not in text:
            seen.append(text)
        return real_stat(path, *args, **kwargs)

    def open_ok(file, *args, **kwargs):
        text = _path_str(file)
        if text.startswith(prefix) and (
            text.endswith("ledger.jsonl") or f"/campaigns/{terminal}.json" in text
        ):
            raise AssertionError(f"unexpected open {text}")
        return real_open(file, *args, **kwargs)

    with mock.patch("os.stat", stat_ok), mock.patch("builtins.open", open_ok), \
            mock.patch.object(CC, "attribute", side_effect=lambda *a, **k: attr_calls.append(1)), \
            mock.patch.object(CC, "live_windows", side_effect=lambda *a, **k: lw_calls.append(1) or []):
        idx = H.history_index(c, now=NOW, cache=True)
    unc = H.history_index(c, now=NOW, cache=False)
    assert not attr_calls and not lw_calls
    assert seen
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
    H.rebuild_cache(c, now=NOW)
    assert (v1 / ".lock").is_file()
    assert v1.is_dir()
    assert not (cache_dir / "history" / ".write.lock").exists()


def test_terminal_ledger_append_updates_records_without_index(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    cid = "SPEC-0394-20261102-ae5b"
    H.history_index(c, now=NOW, cache=True)
    ledger = data_dir / "agentic" / "campaigns" / cid / "ledger.jsonl"
    marker = "quiet-records-ledger-append"
    line = json.dumps({
        "schema": "nw.ledger.event.v1", "ts": store.now_iso(NOW), "seq": 7777,
        "campaign": cid, "type": "finding", "by": "tick", "severity": "nit",
        "title": marker, "detail": "d",
    }) + "\n"
    with ledger.open("a") as stream:
        stream.write(line)
    recs = H.history_records(c, campaign=cid, cache=True)
    unc = H.history_records(c, campaign=cid, cache=False)
    assert recs == unc
    assert any(marker in (r.get("title") or "") for r in recs)


def test_removed_campaign_pending_write_prunes_records(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    cid = "SPEC-0394-20261102-ae5b"
    H.history_index(c, now=NOW, cache=True)
    v1 = cache_dir / "history" / "v1"
    idx_before = next(r for r in H.history_index(c, now=NOW, cache=False) if r["campaign"] == cid)
    shutil.rmtree(data_dir / "agentic" / "campaigns" / cid)
    (v1 / "campaigns" / f"{cid}.json").write_text(json.dumps({"stale": True}) + "\n")
    with (v1 / "index.writes.jsonl").open("a") as stream:
        stream.write(json.dumps(idx_before, ensure_ascii=False) + "\n")
    H.history_index(c, now=NOW, cache=True)
    assert not (v1 / "campaigns" / f"{cid}.json").exists()
    idx = H.history_index(c, now=NOW, cache=True)
    assert all(r["campaign"] != cid for r in idx)
    recs = H.history_records(c, cache=True)
    assert all(
        not (r.get("campaign") == cid and r.get("source") in ("ledger", "run"))
        for r in recs
    )
    with (v1 / "index.writes.jsonl").open("a") as stream:
        stream.write(json.dumps(idx_before, ensure_ascii=False) + "\n")
    H.history_index(c, now=NOW, cache=True)
    assert all(r["campaign"] != cid for r in H.history_index(c, now=NOW, cache=True))
    assert recs == H.history_records(c, cache=False)
    dec = data_dir / "agentic" / "conductor" / "decisions.jsonl"
    with dec.open("a") as stream:
        stream.write(json.dumps({
            "schema": "nw.conductor.decision.v1", "seq": 880880, "ts": store.now_iso(NOW),
            "by": "conductor", "campaign": cid, "spec_id": "SPEC-x", "event_id": "gone-campaign",
            "rule": "t", "kind": "resume", "action": None, "rationale": "r",
        }) + "\n")
    assert next(
        r for r in H.history_records(c, cache=True) if r.get("id") == "decisions:gone-campaign"
    ) == next(
        r for r in H.history_records(c, cache=False) if r.get("id") == "decisions:gone-campaign"
    )


def test_decision_append_tail_seeks_prior_offset(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    v1 = cache_dir / "history" / "v1"
    sources = json.loads((v1 / "sources.json").read_text())
    old_off = sources["logs"]["conductor/decisions.jsonl"]["offset"]
    dec = data_dir / "agentic" / "conductor" / "decisions.jsonl"
    row = {
        "schema": "nw.conductor.decision.v1", "seq": 881881, "ts": store.now_iso(NOW),
        "by": "conductor", "campaign": "SPEC-0336-20261031-6558", "spec_id": "SPEC-0336",
        "event_id": "tail-seek-test", "rule": "t", "kind": "resume", "action": None, "rationale": "r",
    }
    tail_starts = []
    real_dec = HC._decision_records

    def dec_spy(H, cfg, cids, path, gaps, **kwargs):
        if kwargs.get("start"):
            tail_starts.append(kwargs["start"])
        return real_dec(H, cfg, cids, path, gaps, **kwargs)

    with dec.open("a") as stream:
        stream.write(json.dumps(row) + "\n")
    with mock.patch.object(HC, "_decision_records", dec_spy):
        HC._records_mem.clear()
        H.history_records(c, cache=True)
    assert tail_starts == [old_off]
    sources2 = json.loads((v1 / "sources.json").read_text())
    assert sources2["logs"]["conductor/decisions.jsonl"]["offset"] == dec.stat().st_size
    assert H.history_records(c, cache=True) == H.history_records(c, cache=False)


def test_sessions_partial_tail_conductor_usd(data_dir, cache_dir):
    c = _install_priced_session(data_dir, cache_dir)
    sess = data_dir / "agentic" / "conductor" / "sessions.jsonl"
    terminal = "SPEC-0394-20261102-ae5b"
    H.history_index(c, now=NOW, cache=True)
    before = H.campaign_history(c, terminal, now=NOW, cache=False)["header"]["conductor_usd"]
    extra = json.dumps({
        "schema": "nw.conductor.session.v1", "reader": "conductor",
        "session_id": "extra-priced", "ts": "2026-11-03T03:00:00+00:00",
        "project": "Meta-Tools",
    }).encode()
    with sess.open("ab") as stream:
        stream.write(extra)
    doc = H.campaign_history(c, terminal, now=NOW, cache=True)
    assert doc["header"]["conductor_usd"] == before
    with sess.open("ab") as stream:
        stream.write(b"\n")
    doc2 = H.campaign_history(c, terminal, now=NOW, cache=True)
    unc = H.campaign_history(c, terminal, now=NOW, cache=False)
    assert doc2["header"]["conductor_usd"] == unc["header"]["conductor_usd"]


def test_runner_append_then_run_json_edit_stays_fresh(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    other = "SPEC-0336-20261031-6558"
    target = "SPEC-0394-20261102-ae5b"
    H.history_index(c, now=NOW, cache=True)
    runner = data_dir / "agentic" / "runner.jsonl"
    with runner.open("a") as stream:
        stream.write(json.dumps({"ts": store.now_iso(NOW + dt.timedelta(days=30)), "kind": "note"}) + "\n")
    run_root = data_dir / "agentic" / "campaigns" / target / "runs"
    run_dirs = [p for p in run_root.iterdir() if p.is_dir()]
    assert run_dirs
    run_json = run_dirs[0] / "run.json"
    payload = json.loads(run_json.read_text())
    payload["verdict"] = "stale-cache-check"
    run_json.write_text(json.dumps(payload, indent=2) + "\n")
    unc = H.campaign_history(c, other, now=NOW, cache=False)
    cached = H.campaign_history(c, other, now=NOW, cache=True)
    u = {k: v for k, v in unc.items() if k != "generated_at"}
    d = {k: v for k, v in cached.items() if k != "generated_at"}
    assert d == u


def test_warm_index_ledger_and_run_json_records_match_pure(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    cid = "SPEC-0394-20261102-ae5b"
    H.history_index(c, now=NOW, cache=True)
    HC._records_mem.clear()
    ledger = data_dir / "agentic" / "campaigns" / cid / "ledger.jsonl"
    marker = "quiet-records-run-json"
    with ledger.open("a") as stream:
        stream.write(json.dumps({
            "schema": "nw.ledger.event.v1", "ts": store.now_iso(NOW), "seq": 7788,
            "campaign": cid, "type": "finding", "by": "tick", "severity": "nit",
            "title": marker, "detail": "d",
        }) + "\n")
    run_root = data_dir / "agentic" / "campaigns" / cid / "runs"
    run_dirs = [p for p in run_root.iterdir() if p.is_dir()]
    assert run_dirs
    run_json = run_dirs[0] / "run.json"
    payload = json.loads(run_json.read_text())
    payload["note"] = "in-place-run-json"
    run_json.write_text(json.dumps(payload, indent=2) + "\n")
    HC._records_mem.clear()
    cached = H.history_records(c, campaign=cid, cache=True)
    unc = H.history_records(c, campaign=cid, cache=False)
    assert cached == unc
    assert any(marker in (r.get("title") or "") for r in cached)


def test_escalation_write_invalidates_records(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    HC._records_mem.clear()
    esc_dir = data_dir / "agentic" / "escalations"
    esc_path = esc_dir / "ESC-99001.json"
    esc_path.write_text(json.dumps({
        "schema": "nw.escalation.v1", "id": "ESC-99001", "ts": store.now_iso(NOW),
        "status": "open", "title": "cache-invalidation", "text": "esc test",
        "campaign": "SPEC-0394-20261102-ae5b",
    }) + "\n")
    HC._records_mem.clear()
    cached = H.history_records(c, cache=True)
    unc = H.history_records(c, cache=False)
    assert cached == unc
    assert any(r.get("id") == "escalation:ESC-99001" for r in cached)


def test_warm_index_then_decision_records_without_reindex(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    cid = "SPEC-0336-20261031-6558"
    H.history_index(c, now=NOW, cache=True)
    H.history_index(c, now=NOW, cache=True)
    dec = data_dir / "agentic" / "conductor" / "decisions.jsonl"
    marker = "warm-quiet-records-decision"
    row = {
        "schema": "nw.conductor.decision.v1", "seq": 99997, "ts": store.now_iso(NOW),
        "by": "conductor", "campaign": cid, "spec_id": "SPEC-0336", "event_id": marker,
        "rule": "test", "kind": "resume", "action": None, "rationale": "r",
    }
    with dec.open("a") as stream:
        stream.write(json.dumps(row) + "\n")
    HC._records_mem.clear()
    cached = H.history_records(c, cache=True)
    unc = H.history_records(c, cache=False)
    assert cached == unc


def test_warm_index_then_run_json_records_without_reindex(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    cid = "SPEC-0394-20261102-ae5b"
    H.history_index(c, now=NOW, cache=True)
    H.history_index(c, now=NOW, cache=True)
    run_root = data_dir / "agentic" / "campaigns" / cid / "runs"
    run_json = next(p for p in run_root.iterdir() if p.is_dir()) / "run.json"
    payload = json.loads(run_json.read_text())
    payload["note"] = "warm-quiet-run-json-only"
    run_json.write_text(json.dumps(payload, indent=2) + "\n")
    HC._records_mem.clear()
    cached = H.history_records(c, campaign=cid, cache=True)
    unc = H.history_records(c, campaign=cid, cache=False)
    assert cached == unc


def test_runner_recreated_after_missing_updates_terminal(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    cid = "SPEC-0336-20261031-6558"
    H.history_index(c, now=NOW, cache=True)
    runner = data_dir / "agentic" / "runner.jsonl"
    runner.unlink()
    H.history_index(c, now=NOW, cache=True)
    unc_missing = H.campaign_history(c, cid, now=NOW, cache=False)
    cached_missing = H.campaign_history(c, cid, now=NOW, cache=True)
    assert "runner.jsonl: missing" in cached_missing["gaps"]
    doc = cached_missing
    created = H._parse_ts(doc["header"]["created_at"])
    closed = H._parse_ts(doc["header"]["closed_at"])
    assert created and closed
    down_start = created - dt.timedelta(minutes=5)
    down_end = closed + dt.timedelta(minutes=5)
    with runner.open("w") as stream:
        stream.write(json.dumps({"ts": store.now_iso(down_start), "kind": "down"}) + "\n")
        stream.write(json.dumps({"ts": store.now_iso(down_end), "kind": "up"}) + "\n")
    HC._records_mem.clear()
    unc = H.campaign_history(c, cid, now=NOW, cache=False)
    cached = H.campaign_history(c, cid, now=NOW, cache=True)
    u = {k: v for k, v in unc.items() if k != "generated_at"}
    d = {k: v for k, v in cached.items() if k != "generated_at"}
    assert d == u
    assert "runner.jsonl: missing" not in cached["gaps"]
    def _runner_down_total(doc):
        phases = doc.get("phases") or []
        rows = phases.values() if isinstance(phases, dict) else phases
        return sum(int(p.get("runner_down_s") or 0) for p in rows)
    assert _runner_down_total(cached) == _runner_down_total(unc)


def test_review_inplace_refreshes_records(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    review_dir = data_dir / "agentic" / "reviews" / "SPEC-0394"
    review = next(review_dir.glob("*.json"))
    body = json.loads(review.read_text())
    body["title"] = "cache-review-inplace-title"
    review.write_text(json.dumps(body, indent=2) + "\n")
    HC._records_mem.clear()
    cached = H.history_records(c, query="cache-review-inplace-title", cache=True)
    unc = H.history_records(c, query="cache-review-inplace-title", cache=False)
    assert cached == unc
    assert cached


def test_decision_unparsable_line_number_after_global_sync(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    cid = "SPEC-0394-20261102-ae5b"
    H.history_index(c, now=NOW, cache=True)
    flaky = data_dir / "agentic" / "flaky-tests.json"
    body = json.loads(flaky.read_text())
    body["note"] = "touch-flaky-for-lines-preserve"
    flaky.write_text(json.dumps(body) + "\n")
    H.history_index(c, now=NOW, cache=True)
    dec = data_dir / "agentic" / "conductor" / "decisions.jsonl"
    with dec.open("a") as stream:
        stream.write("not-json-decision-line\n")
    unc = H.campaign_history(c, cid, now=NOW, cache=False)
    cached = H.campaign_history(c, cid, now=NOW, cache=True)
    u = {k: v for k, v in unc.items() if k != "generated_at"}
    d = {k: v for k, v in cached.items() if k != "generated_at"}
    assert d == u


def test_corrupt_campaign_json_rebuilds_and_removes_tmp(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    cid = "SPEC-0394-20261102-ae5b"
    H.history_index(c, now=NOW, cache=True)
    v1 = cache_dir / "history" / "v1"
    camp = v1 / "campaigns" / f"{cid}.json"
    tmp = v1 / "campaigns" / "orphan.tmp"
    tmp.write_text("leftover\n")
    camp.write_text("{")
    HC._records_mem.clear()
    unc = H.history_records(c, cache=False)
    cached = H.history_records(c, cache=True)
    assert cached == unc
    assert (v1 / "version").read_bytes() == b"1\n"
    assert not tmp.exists()
    assert json.loads(camp.read_text())["campaign"] == cid


def test_decision_append_keeps_stored_unparsable_gaps(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    cid = "SPEC-0394-20261102-ae5b"
    H.history_index(c, now=NOW, cache=True)
    dec = data_dir / "agentic" / "conductor" / "decisions.jsonl"
    with dec.open("a") as stream:
        stream.write("not-json-before-append\n")
    H.history_index(c, now=NOW, cache=True)
    unc_before = H.campaign_history(c, cid, now=NOW, cache=False)
    gap_prefix = "decisions.jsonl: line"
    assert any(g.startswith(gap_prefix) for g in unc_before.get("gaps", []))
    row = {
        "schema": "nw.conductor.decision.v1", "seq": 88887, "ts": store.now_iso(NOW),
        "by": "conductor", "campaign": cid, "event_id": "gap-keep-test",
        "rule": "test", "kind": "resume", "action": None, "rationale": "r",
    }
    with dec.open("a") as stream:
        stream.write(json.dumps(row) + "\n")
    unc = H.campaign_history(c, cid, now=NOW, cache=False)
    cached = H.campaign_history(c, cid, now=NOW, cache=True)
    assert cached["gaps"] == unc["gaps"]
    assert any(g.startswith(gap_prefix) for g in cached.get("gaps", []))


def test_runner_same_size_mtime_resets_log_offset(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    v1 = cache_dir / "history" / "v1"
    old_sources = json.loads((v1 / "sources.json").read_text())
    old_off = old_sources["logs"]["runner.jsonl"]["offset"]
    assert old_off > 0
    run_path = data_dir / "agentic" / "runner.jsonl"
    st = run_path.stat()
    os.utime(run_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    fresh = HC._scan_sources(c, prev=old_sources)
    assert fresh["logs"]["runner.jsonl"]["offset"] == 0
    HC._records_mem.clear()
    cid = "SPEC-0394-20261102-ae5b"
    unc = H.campaign_history(c, cid, now=NOW, cache=False)
    cached = H.campaign_history(c, cid, now=NOW, cache=True)
    assert {k: v for k, v in cached.items() if k != "generated_at"} == {
        k: v for k, v in unc.items() if k != "generated_at"
    }


def test_decisions_unlink_matches_pure_terminal(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    cid = "SPEC-0394-20261102-ae5b"
    H.history_index(c, now=NOW, cache=True)
    (data_dir / "agentic" / "conductor" / "decisions.jsonl").unlink()
    HC._records_mem.clear()
    unc = H.campaign_history(c, cid, now=NOW, cache=False)
    cached = H.campaign_history(c, cid, now=NOW, cache=True)
    assert cached["gaps"] == unc["gaps"]
    assert "decisions.jsonl: missing" in cached["gaps"]
    assert H.history_records(c, cache=True) == H.history_records(c, cache=False)


def test_lock_ex_before_campaign_listing_on_warm_miss(data_dir, cache_dir, monkeypatch):
    import fcntl

    c = cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    v1 = cache_dir / "history" / "v1"
    sample = json.loads((v1 / "index.jsonl").read_text().splitlines()[0])
    (v1 / "index.writes.jsonl").write_text(json.dumps(sample) + "\n")
    HC._CAMPAIGN_LISTING.clear()
    events: list = []
    real_flock = fcntl.flock

    def track_flock(fd, op):
        if op == fcntl.LOCK_EX:
            events.append("lock_ex")
        return real_flock(fd, op)

    real_scandir = os.scandir

    def track_scandir(path, *a, **kw):
        events.append(("scandir", str(path)))
        return real_scandir(path, *a, **kw)

    monkeypatch.setattr(fcntl, "flock", track_flock)
    monkeypatch.setattr(os, "scandir", track_scandir)
    H.history_index(c, now=NOW, cache=True)
    camp_dir = str(store.campaigns_dir(c))
    lock_i = events.index("lock_ex")
    scandir_i = next(
        i for i, e in enumerate(events) if e[0] == "scandir" and e[1] == camp_dir
    )
    assert lock_i < scandir_i


def test_corrupt_version_rebuilds_to_one(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    v1 = cache_dir / "history" / "v1"
    (v1 / "version").write_text("999\n")
    HC._records_mem.clear()
    idx = H.history_index(c, now=NOW, cache=True)
    assert (v1 / "version").read_bytes() == b"1\n"
    assert idx == H.history_index(cfg(data_dir), now=NOW, cache=False)


def test_pending_prune_relinks_survivor_mail(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    a = "SPEC-0394-20261102-ae5b"
    b = "SPEC-0336-20261031-6558"
    mail = data_dir / "agentic" / "mailbox" / "human.jsonl"
    marker = "dual-link-mail-marker"
    with mail.open("a") as stream:
        stream.write(json.dumps({
            "schema": "nw.message.v1", "id": "dual-link-mail", "seq": 99999,
            "ts": store.now_iso(NOW), "from": "conductor", "to": "human",
            "kind": "note", "re": None, "text": f"{marker} {a} {b}",
        }) + "\n")
    H.history_index(c, now=NOW, cache=True)
    v1 = cache_dir / "history" / "v1"
    idx_a = next(r for r in H.history_index(c, now=NOW, cache=False) if r["campaign"] == a)
    shutil.rmtree(data_dir / "agentic" / "campaigns" / a)
    (v1 / "campaigns" / f"{a}.json").write_text(json.dumps({"stale": True}) + "\n")
    (v1 / "index.writes.jsonl").write_text(json.dumps(idx_a, ensure_ascii=False) + "\n")
    H.history_index(c, now=NOW, cache=True)
    rec_ino = (v1 / "records.jsonl").stat().st_ino
    pure_b = H.campaign_history(c, b, now=NOW, cache=False)
    cached_b = H.campaign_history(c, b, now=NOW, cache=True)
    assert {k: v for k, v in cached_b.items() if k != "generated_at"} == {
        k: v for k, v in pure_b.items() if k != "generated_at"
    }
    mail_rec = next(r for r in H.history_records(c, cache=True) if marker in (r.get("text") or ""))
    assert mail_rec.get("campaign") == next(
        r for r in H.history_records(c, cache=False) if r.get("id") == mail_rec["id"]
    ).get("campaign")
    row_b = next(r for r in H.history_index(c, now=NOW, cache=True) if r["campaign"] == b)
    pure_row = next(r for r in H.history_index(c, now=NOW, cache=False) if r["campaign"] == b)
    assert row_b["records"] == pure_row["records"]
    H.history_index(c, now=NOW, cache=True)
    assert (v1 / "records.jsonl").stat().st_ino == rec_ino


def test_sentinel_dir_appears_updates_terminal_gaps(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    sent = data_dir / "agentic" / "sentinel"
    if sent.is_dir():
        shutil.rmtree(sent)
    cid = "SPEC-0394-20261102-ae5b"
    H.history_index(c, now=NOW, cache=True)
    cached = H.campaign_history(c, cid, now=NOW, cache=True)
    assert "sentinel/: missing" in cached["gaps"]
    sent.mkdir()
    (sent / "Meta-Tools.lock").write_text("held\n")
    HC._records_mem.clear()
    v1 = cache_dir / "history" / "v1"
    H.history_index(c, now=NOW, cache=True)
    sources = json.loads((v1 / "sources.json").read_text())
    assert sources.get("dir_present", {}).get("sentinel") is True
    assert "sentinel/: missing" not in (sources.get("global_gaps") or [])
    for row in H.history_index(c, now=NOW, cache=False):
        if row.get("outcome") not in ("merged", "handed_off", "abandoned"):
            continue
        tid = row["campaign"]
        stored = json.loads((v1 / "campaigns" / f"{tid}.json").read_text())
        pure = H.campaign_history(c, tid, now=NOW, cache=False)
        assert {k: v for k, v in stored.items() if k != "generated_at"} == {
            k: v for k, v in pure.items() if k != "generated_at"
        }
        assert "sentinel/: missing" not in stored.get("gaps", [])
    rec_ino = (v1 / "records.jsonl").stat().st_ino
    prefix = (data_dir / "agentic" / "campaigns" / cid).resolve().as_posix() + os.sep
    ledger = (data_dir / "agentic" / "campaigns" / cid / "ledger.jsonl").resolve().as_posix()
    camp_json = (v1 / "campaigns" / f"{cid}.json").resolve().as_posix()
    real_open = builtins.open

    def open_ok(file, *args, **kwargs):
        text = _path_str(file)
        if text == ledger or text == camp_json or (
            text.startswith(prefix) and "ledger.jsonl" in text
        ):
            raise AssertionError(f"unexpected open {text}")
        return real_open(file, *args, **kwargs)

    with mock.patch("builtins.open", open_ok):
        H.campaign_history(c, cid, now=NOW, cache=True)
        H.history_index(c, now=NOW, cache=True)
    assert (v1 / "records.jsonl").stat().st_ino == rec_ino
    cached2 = H.campaign_history(c, cid, now=NOW, cache=True)
    pure = H.campaign_history(c, cid, now=NOW, cache=False)
    assert "sentinel/: missing" not in cached2["gaps"]
    assert cached2["gaps"] == pure["gaps"]


def test_runner_null_line_gap_matches_pure(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    run = data_dir / "agentic" / "runner.jsonl"
    with run.open("a") as stream:
        stream.write("null\n")
    cid = "SPEC-0394-20261102-ae5b"
    H.history_index(c, now=NOW, cache=True)
    cached = H.campaign_history(c, cid, now=NOW, cache=True)
    pure = H.campaign_history(c, cid, now=NOW, cache=False)
    assert cached["gaps"] == pure["gaps"]


# --- SPEC-0567: a live-id set change invalidates stored terminals ---------------------------

_A = "SPEC-0394-20261102-ae5b"
_B = "SPEC-0336-20261031-6558"
_X = "LIVE-20261107-ef02"
_MAIL_ID = "live-set-mail"


def _v1(cache_dir) -> Path:
    return cache_dir / "history" / "v1"


def _add_mail(data_dir, text: str, mail_id: str = _MAIL_ID) -> None:
    mail = data_dir / "agentic" / "mailbox" / "human.jsonl"
    with mail.open("a") as stream:
        stream.write(json.dumps({
            "schema": "nw.message.v1", "id": mail_id, "seq": 99998,
            "ts": store.now_iso(NOW), "from": "conductor", "to": "human",
            "kind": "note", "re": None, "text": text,
        }) + "\n")


def _install_terminal_campaign(data_dir, cid: str) -> None:
    src = data_dir / "agentic" / "campaigns" / _A
    dst = data_dir / "agentic" / "campaigns" / cid
    shutil.copytree(src, dst)
    state = json.loads((dst / "state.json").read_text())
    state["id"] = cid
    (dst / "state.json").write_text(json.dumps(state, indent=2) + "\n")
    kept = []
    for line in (dst / "ledger.jsonl").read_text().splitlines():
        row = json.loads(line)
        if "campaign" in row:
            row = {**row, "campaign": cid}
        kept.append(json.dumps(row))
    (dst / "ledger.jsonl").write_text("\n".join(kept) + "\n")


def _strip(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k != "generated_at"}


def _mail_campaign(c, cache: bool):
    rec = next(r for r in H.history_records(c, cache=cache)
               if "live-set-marker" in (r.get("text") or ""))
    return rec.get("campaign")


def _assert_cache_equals_pure(c, cache_dir) -> None:
    """Stored terminals and records.jsonl first (a read must not be what heals them)."""
    H.history_index(c, now=NOW, cache=True)
    cached_records = H.history_records(c, cache=True)
    v1 = _v1(cache_dir)
    for row in HC._read_index(v1):
        if row.get("outcome") not in HC._TERMINAL_OUTCOMES:
            continue
        stored = json.loads((v1 / "campaigns" / f"{row['campaign']}.json").read_text())
        assert _strip(stored) == _strip(
            H.campaign_history(c, row["campaign"], now=NOW, cache=False)), row["campaign"]
    assert cached_records == H.history_records(c, cache=False)
    assert {r["id"] for r in HC._read_records(v1)} == {r["id"] for r in cached_records}
    for row in H.history_index(c, now=NOW, cache=False):
        assert _strip(H.campaign_history(c, row["campaign"], now=NOW, cache=True)) == _strip(
            H.campaign_history(c, row["campaign"], now=NOW, cache=False)), row["campaign"]


def _mail_rid(c) -> str:
    return next(r["id"] for r in H.history_records(c, cache=False)
                if "live-set-marker" in (r.get("text") or ""))


def _global_pairs(records):
    return sorted(
        (r["id"], r.get("campaign")) for r in records
        if r.get("source") not in HC._LOCAL_SOURCES
    )


@pytest.mark.parametrize("case", ["add", "remove", "add+remove"])
def test_live_set_change_stored_terminals_equal_pure(data_dir, cache_dir, case):
    c = cfg(data_dir, cache_dir)
    camps = data_dir / "agentic" / "campaigns"
    _add_mail(data_dir, f"live-set-marker {_A} {_X}")
    if case != "add":
        _install_live_campaign(data_dir, _X)
    H.history_index(c, now=NOW, cache=True)
    H.history_records(c, cache=True)
    if case == "add":
        _install_live_campaign(data_dir, _X)
    elif case == "remove":
        shutil.rmtree(camps / _X)
    else:
        shutil.rmtree(camps / _X)
        _install_live_campaign(data_dir, "LIVE-20261107-ef03")
    _assert_cache_equals_pure(c, cache_dir)
    assert _mail_campaign(c, True) == _mail_campaign(c, False)


def test_live_set_add_unlinks_text_link(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    _add_mail(data_dir, f"live-set-marker {_A} {_X}")
    H.history_index(c, now=NOW, cache=True)
    assert _mail_campaign(c, True) == _A
    _install_live_campaign(data_dir, _X)
    H.history_index(c, now=NOW, cache=True)
    assert _mail_campaign(c, True) is None
    v1 = _v1(cache_dir)
    stored = json.loads((v1 / "campaigns" / f"{_A}.json").read_text())
    rid = _mail_rid(c)
    assert all(r["id"] != rid for r in stored["records"])
    assert all(r["id"] != rid or r.get("campaign") is None for r in HC._read_records(v1))


def test_live_set_remove_relinks_text_link(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    _add_mail(data_dir, f"live-set-marker {_A} {_X}")
    _install_live_campaign(data_dir, _X)
    H.history_index(c, now=NOW, cache=True)
    assert _mail_campaign(c, True) is None
    shutil.rmtree(data_dir / "agentic" / "campaigns" / _X)
    H.history_index(c, now=NOW, cache=True)
    stored = json.loads((_v1(cache_dir) / "campaigns" / f"{_A}.json").read_text())
    assert any(r["id"] == _mail_rid(c) for r in stored["records"])
    assert _strip(stored) == _strip(H.campaign_history(c, _A, now=NOW, cache=False))


def test_gone_id_leaves_no_trace(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    v1 = _v1(cache_dir)
    assert (v1 / "campaigns" / f"{_A}.json").is_file()
    shutil.rmtree(data_dir / "agentic" / "campaigns" / _A)
    H.history_index(c, now=NOW, cache=True)
    assert all(r["campaign"] != _A for r in HC._read_index(v1))
    assert not (v1 / "campaigns" / f"{_A}.json").exists()
    recs = HC._read_records(v1)
    assert not [r for r in recs if r.get("campaign") == _A and r.get("source") in HC._LOCAL_SOURCES]
    sources = json.loads((v1 / "sources.json").read_text())
    assert _A not in sources["campaign_files"]
    assert _A not in sources.get("campaign_rg_dirs", {})
    # the flaky record names the gone campaign by field and is still returned
    flaky = [r for r in H.history_records(c, cache=True, kind="flaky") if r.get("campaign") == _A]
    assert flaky and flaky == [
        r for r in H.history_records(c, cache=False, kind="flaky") if r.get("campaign") == _A]


def test_moved_global_record_not_dropped(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    _add_mail(data_dir, f"live-set-marker {_A} {_X}")
    H.history_index(c, now=NOW, cache=True)
    assert _mail_campaign(c, True) == _A
    shutil.rmtree(data_dir / "agentic" / "campaigns" / _A)
    _install_live_campaign(data_dir, _X)
    H.history_index(c, now=NOW, cache=True)
    rid = _mail_rid(c)
    mine = [r for r in HC._read_records(_v1(cache_dir)) if r["id"] == rid]
    assert len(mine) == 1 and mine[0]["campaign"] == _X
    assert _global_pairs(HC._read_records(_v1(cache_dir))) == _global_pairs(
        H.history_records(c, cache=False))
    assert H.history_records(c, cache=True) == H.history_records(c, cache=False)
    src_root = Path(HC.__file__).resolve().parent.parent
    assert not [p for p in src_root.rglob("*.py") if "drop_campaign_all" in p.read_text()]


def _owned_globals(records, cid):
    return sorted(
        (r["id"], r.get("campaign"), r.get("spec_id"), r.get("project"))
        for r in records if r.get("source") not in HC._LOCAL_SOURCES and r.get("campaign") == cid
    )


def test_refresh_live_keeps_owned_globals(data_dir, cache_dir):
    """A refreshed campaign that owns a text-linked global keeps it exactly once."""
    c = cfg(data_dir, cache_dir)
    _add_mail(data_dir, f"live-set-marker {_X}")
    _install_live_campaign(data_dir, _X)
    H.history_index(c, now=NOW, cache=True)
    root = _v1(cache_dir)
    pure = H.history_records(c, cache=False)
    assert _owned_globals(pure, _X)
    assert _owned_globals(HC._read_records(root), _X) == _owned_globals(pure, _X)
    ledger = data_dir / "agentic" / "campaigns" / _X / "ledger.jsonl"
    with ledger.open("a") as stream:
        stream.write(json.dumps({"ts": store.now_iso(NOW), "type": "note", "campaign": _X}) + "\n")
    HC._refresh_live(c, root, NOW, HC._read_index(root), HC._read_records(root))
    recs = HC._read_records(root)
    assert _owned_globals(recs, _X) == _owned_globals(pure, _X)
    assert [r["id"] for r in recs].count(_mail_rid(c)) == 1
    assert H.history_records(c, cache=True) == H.history_records(c, cache=False)


def test_merge_recomputed_keeps_globals_and_follows_header():
    glob = {"id": "g1", "source": "mailbox", "campaign": "C1", "links": {"campaign": "C1"},
            "linked_by": "text", "spec_id": "OLD", "project": "old"}
    local = {"id": "l-old", "source": next(iter(HC._LOCAL_SOURCES)), "campaign": "C1"}
    other = {"id": "g2", "source": "mailbox", "campaign": "C2", "spec_id": "S2", "project": "p2"}
    new_local = {"id": "l-new", "source": local["source"], "campaign": "C1"}
    doc = {"header": {"spec_id": "SPEC-9", "project": "proj"}, "records": [new_local]}
    start = [glob, local, other]
    out = HC._merge_recomputed(start, doc, "C1")
    assert sorted(r["id"] for r in out) == ["g1", "g2", "l-new"]
    g1 = next(r for r in out if r["id"] == "g1")
    assert (g1["spec_id"], g1["project"]) == ("SPEC-9", "proj")
    assert next(r for r in out if r["id"] == "g2")["spec_id"] == "S2"
    assert glob["spec_id"] == "OLD"  # the input snapshot's dicts are not mutated


def test_refresh_live_leaves_globals_equal_pure(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    _add_mail(data_dir, f"live-set-marker {_A}")
    _install_live_campaign(data_dir, _X)
    H.history_index(c, now=NOW, cache=True)
    ledger = data_dir / "agentic" / "campaigns" / _X / "ledger.jsonl"
    with ledger.open("a") as stream:
        stream.write(json.dumps({"ts": store.now_iso(NOW), "type": "note", "campaign": _X}) + "\n")
    v1 = _v1(cache_dir)
    root = v1
    HC._refresh_live(c, root, NOW, HC._read_index(root), HC._read_records(root))
    assert _global_pairs(HC._read_records(root)) == _global_pairs(H.history_records(c, cache=False))
    assert H.history_records(c, cache=True) == H.history_records(c, cache=False)


def test_recompute_touches_only_differing_terminals(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    _add_mail(data_dir, f"live-set-marker {_A} {_X}")
    H.history_index(c, now=NOW, cache=True)
    v1 = _v1(cache_dir)
    mtimes = {p.name: p.stat().st_mtime_ns for p in (v1 / "campaigns").glob("*.json")}
    _install_live_campaign(data_dir, _X)
    calls: list[str] = []
    real = H._campaign_history

    def spy(cfg_, cid, *a, **kw):
        calls.append(cid)
        return real(cfg_, cid, *a, **kw)

    with mock.patch.object(H, "_campaign_history", spy):
        H.history_index(c, now=NOW, cache=True)
    assert calls.count(_A) == 1
    untouched = [cid for cid in calls if cid not in (_A, _X)]
    assert untouched == []
    for name, mtime in mtimes.items():
        if name != f"{_A}.json":
            assert (v1 / "campaigns" / name).stat().st_mtime_ns == mtime, name
    assert (v1 / "campaigns" / f"{_A}.json").stat().st_mtime_ns != mtimes[f"{_A}.json"]


def test_reconcile_persist_contract(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    v1 = _v1(cache_dir)
    new_id = "SPEC-0394-20261112-aaaa"
    _install_terminal_campaign(data_dir, new_id)
    names = ("index.jsonl", "records.jsonl", "sources.json")
    before = {n: (v1 / n).read_bytes() for n in names}
    persist_calls: list = []
    real = HC._persist_index_records

    def spy(*a, **kw):
        persist_calls.append(1)
        return real(*a, **kw)

    def load():
        return HC._read_index(v1), HC._read_records(v1), json.loads((v1 / "sources.json").read_text())

    with mock.patch.object(HC, "_persist_index_records", spy):
        idx, recs, src = load()
        out = HC._reconcile_live_set(c, v1, NOW, idx, recs, src, persist=False)
        assert persist_calls == [] and new_id in out[3]
        assert {n: (v1 / n).read_bytes() for n in names} == before
        idx, recs, src = load()
        HC._reconcile_live_set(c, v1, NOW, idx, recs, src, persist=True)
        assert persist_calls == [1]
    assert any(r["campaign"] == new_id for r in HC._read_index(v1))
    seen: list[str] = []
    real_ch = H._campaign_history
    with mock.patch.object(H, "_campaign_history",
                           lambda cfg_, cid, *a, **kw: seen.append(cid) or real_ch(cfg_, cid, *a, **kw)):
        doc = H.campaign_history(c, new_id, now=NOW, cache=True)
    assert seen == [] and doc["campaign"] == new_id


def _delete_on_first_recompute(data_dir, pick, *, after: bool):
    real = HC._recompute_campaign
    state = {"n": 0, "victim": None}

    def wrapper(cfg_, cid, *a, **kw):
        state["n"] += 1
        if state["n"] == 1:
            state["victim"] = pick(cid)
        if not after and state["n"] == 1 and state["victim"]:
            shutil.rmtree(data_dir / "agentic" / "campaigns" / state["victim"])
        out = real(cfg_, cid, *a, **kw)
        if after and state["n"] == 1:
            shutil.rmtree(data_dir / "agentic" / "campaigns" / state["victim"])
        return out

    return wrapper, state


def _spy_cold_fill_writes():
    calls = {"recompute": [], "pending": [], "file": []}
    real_re, real_pending, real_file = (
        HC._recompute_campaign, HC._append_pending_index_write, HC._write_campaign_file)

    def recompute(cfg_, cid, *a, **kw):
        calls["recompute"].append(cid)
        return real_re(cfg_, cid, *a, **kw)

    def pending(root, row):
        calls["pending"].append(row.get("campaign"))
        return real_pending(root, row)

    def wfile(root, cid, doc):
        calls["file"].append(cid)
        return real_file(root, cid, doc)

    return calls, (recompute, pending, wfile)


def test_cold_fill_skips_gone_id(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    ids = HC._campaign_ids(c)
    victim = ids[1]
    removed = {"done": False}
    calls, (recompute, pending, wfile) = _spy_cold_fill_writes()

    def deleting(cfg_, cid, *a, **kw):
        if not removed["done"]:
            removed["done"] = True
            shutil.rmtree(data_dir / "agentic" / "campaigns" / victim)
        return recompute(cfg_, cid, *a, **kw)

    seen: list[tuple[int, int]] = []
    with mock.patch.object(HC, "_recompute_campaign", deleting), \
            mock.patch.object(HC, "_append_pending_index_write", pending), \
            mock.patch.object(HC, "_write_campaign_file", wfile):
        HC.rebuild_cache(c, now=NOW, progress=lambda d, t: seen.append((d, t)))
    v1 = _v1(cache_dir)
    assert seen[-1][0] == seen[-1][1] == len(ids)
    # the skip itself: the gone id never reaches a recompute, a pending write or a file write
    assert victim not in calls["recompute"]
    assert victim not in calls["pending"]
    assert victim not in calls["file"]
    assert sorted(calls["recompute"]) == sorted(set(ids) - {victim})
    # the spy is the assertion: ``_cold_fill`` always unlinks index.writes.jsonl, so the
    # absence of that file would pass whether or not a pending write was recorded
    assert sorted(calls["pending"]) == sorted(set(ids) - {victim})
    assert not (v1 / "campaigns" / f"{victim}.json").exists()
    assert all(r["campaign"] != victim for r in HC._read_index(v1))
    assert all(r.get("campaign") != victim or r.get("source") not in HC._LOCAL_SOURCES
               for r in HC._read_records(v1))
    assert victim not in json.loads((v1 / "sources.json").read_text())["campaign_files"]
    assert H.history_records(c, cache=True) == H.history_records(c, cache=False)


def test_cold_fill_skipped_gone_id_relinks_globals(data_dir, cache_dir):
    """A skipped gone id still changes the live set the globals were resolved with."""
    c = cfg(data_dir, cache_dir)
    gone = "SPEC-9999-20261112-zzzz"
    _install_terminal_campaign(data_dir, gone)
    _add_mail(data_dir, f"live-set-marker {_A} {gone}")
    assert HC._campaign_ids(c)[-1] == gone
    removed = {"done": False}
    real = HC._recompute_campaign

    def deleting(cfg_, cid, *a, **kw):
        if not removed["done"]:
            removed["done"] = True
            shutil.rmtree(data_dir / "agentic" / "campaigns" / gone)
        return real(cfg_, cid, *a, **kw)

    with mock.patch.object(HC, "_recompute_campaign", deleting):
        HC.rebuild_cache(c, now=NOW)
    assert _mail_campaign(c, False) == _A
    _assert_cache_equals_pure(c, cache_dir)
    assert _mail_campaign(c, True) == _A
    assert any(r["id"] == _mail_rid(c) for r in json.loads(
        (_v1(cache_dir) / "campaigns" / f"{_A}.json").read_text())["records"])


def test_cold_fill_dir_removed_during_its_recompute(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    ids = HC._campaign_ids(c)
    wrapper, state = _delete_on_first_recompute(data_dir, lambda cid: cid, after=True)
    with mock.patch.object(HC, "_recompute_campaign", wrapper):
        HC.rebuild_cache(c, now=NOW)
    victim = state["victim"]
    v1 = _v1(cache_dir)
    assert victim == ids[0]
    assert not (v1 / "campaigns" / f"{victim}.json").exists()
    assert all(r["campaign"] != victim for r in HC._read_index(v1))
    assert all(r.get("campaign") != victim or r.get("source") not in HC._LOCAL_SOURCES
               for r in HC._read_records(v1))
    assert victim not in json.loads((v1 / "sources.json").read_text())["campaign_files"]
    assert H.history_records(c, cache=True) == H.history_records(c, cache=False)


def test_cold_fill_live_set_added_mid_fill(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    new_id = "SPEC-0394-20261112-bbbb"
    _add_mail(data_dir, f"live-set-marker {_A} {new_id}")
    real = HC._recompute_campaign
    n = {"v": 0}

    def wrapper(cfg_, cid, *a, **kw):
        n["v"] += 1
        if n["v"] == 1:
            _install_terminal_campaign(data_dir, new_id)
        return real(cfg_, cid, *a, **kw)

    with mock.patch.object(HC, "_recompute_campaign", wrapper):
        HC.rebuild_cache(c, now=NOW)
    v1 = _v1(cache_dir)
    assert any(r["campaign"] == new_id for r in HC._read_index(v1))
    assert new_id in json.loads((v1 / "sources.json").read_text())["campaign_files"]
    assert not (v1 / "index.writes.jsonl").exists()
    assert _mail_campaign(c, True) == _mail_campaign(c, False) is None
    _assert_cache_equals_pure(c, cache_dir)
    seen: list[str] = []
    real_ch = H._campaign_history
    with mock.patch.object(H, "_campaign_history",
                           lambda cfg_, cid, *a, **kw: seen.append(cid) or real_ch(cfg_, cid, *a, **kw)):
        H.campaign_history(c, new_id, now=NOW, cache=True)
    assert seen == []


def test_unchanged_live_set_skips_reconcile(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    v1 = _v1(cache_dir)
    idx, recs = HC._read_index(v1), HC._read_records(v1)
    names = ("index.jsonl", "records.jsonl", "sources.json")
    before = {n: (v1 / n).read_bytes() for n in names}
    writers = ("_append_pending_index_write", "_write_campaign_file", "_write_records",
               "_write_index", "_atomic_write_json", "_safe_unlink_campaign_file",
               "_recompute_campaign")
    with mock.patch.object(H, "_global_records") as gl, \
            mock.patch.object(HC, "_persist_index_records") as persist, \
            contextlib.ExitStack() as stack:
        spies = {n: stack.enter_context(mock.patch.object(HC, n)) for n in writers}
        out = HC._reconcile_live_set(c, v1, NOW, idx, recs, None, persist=True)
    gl.assert_not_called()
    persist.assert_not_called()
    for name, spy in spies.items():
        assert not spy.called, name
    assert not (v1 / "index.writes.jsonl").exists()
    assert {n: (v1 / n).read_bytes() for n in names} == before
    assert out[0] is idx and out[1] is recs and out[3] == set()
    seen: list[str] = []
    real_ch = H._campaign_history
    with mock.patch.object(H, "_campaign_history",
                           lambda cfg_, cid, *a, **kw: seen.append(cid) or real_ch(cfg_, cid, *a, **kw)):
        H.campaign_history(c, _A, now=NOW, cache=True)
    assert seen == []


# --- SPEC-0575: SPEC-0567 review follow-ups --------------------------------------------

_N = "SPEC-0394-20261112-aaaa"


def _lock_held(v1: Path) -> bool:
    """True while the cache's flock is held: a second descriptor conflicts with the first."""
    fd = os.open(v1 / ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _load(v1: Path):
    return HC._read_index(v1), HC._read_records(v1), json.loads((v1 / "sources.json").read_text())


def _spy_reconcile(v1: Path, log: list):
    real = HC._reconcile_live_set

    def spy(*a, **kw):
        log.append({"kw": dict(kw), "locked": _lock_held(v1)})
        return real(*a, **kw)

    return spy


def test_cold_fill_skipped_gone_id_forces_reconcile(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    gone = "SPEC-9999-20261112-zzzz"
    _install_terminal_campaign(data_dir, gone)
    _add_mail(data_dir, f"live-set-marker {_A} {gone}")
    removed = {"done": False}
    real = HC._recompute_campaign

    def deleting(cfg_, cid, *a, **kw):
        if not removed["done"]:
            removed["done"] = True
            shutil.rmtree(data_dir / "agentic" / "campaigns" / gone)
        return real(cfg_, cid, *a, **kw)

    log: list = []
    with mock.patch.object(HC, "_recompute_campaign", deleting), \
            mock.patch.object(HC, "_reconcile_live_set", _spy_reconcile(_v1(cache_dir), log)):
        HC.rebuild_cache(c, now=NOW)
    assert len(log) == 1
    assert log[0]["kw"]["force"] is True and log[0]["kw"]["persist"] is False
    assert _mail_campaign(c, False) == _A
    _assert_cache_equals_pure(c, cache_dir)
    assert _mail_campaign(c, True) == _A


def _change_live_set(data_dir, case: str) -> None:
    camps = data_dir / "agentic" / "campaigns"
    if case == "add":
        _install_terminal_campaign(data_dir, _N)
    else:
        shutil.rmtree(camps / _N)


def _warm_for(data_dir, c, case: str) -> None:
    if case == "remove":
        _install_terminal_campaign(data_dir, _N)
    _add_mail(data_dir, f"live-set-marker {_A} {_N}")
    H.history_index(c, now=NOW, cache=True)
    H.history_records(c, cache=True)


@pytest.mark.parametrize("case", ["add", "remove"])
def test_first_campaign_history_after_live_set_change_reconciles_under_lock(
        data_dir, cache_dir, case):
    c = cfg(data_dir, cache_dir)
    _warm_for(data_dir, c, case)
    _change_live_set(data_dir, case)
    log: list = []
    with mock.patch.object(HC, "_sync_globals", lambda *a, **kw: set()), \
            mock.patch.object(HC, "_reconcile_live_set", _spy_reconcile(_v1(cache_dir), log)):
        doc = H.campaign_history(c, _A, now=NOW, cache=True)
    assert _strip(doc) == _strip(H.campaign_history(c, _A, now=NOW, cache=False))
    assert len(log) == 1
    assert log[0]["kw"]["persist"] is True and log[0]["kw"]["_hold_guard"] is True
    assert log[0]["locked"] is True


@pytest.mark.parametrize("case", ["add", "remove"])
def test_first_history_records_after_live_set_change_reconciles_under_lock(
        data_dir, cache_dir, case):
    c = cfg(data_dir, cache_dir)
    _warm_for(data_dir, c, case)
    _change_live_set(data_dir, case)
    log: list = []
    with mock.patch.object(HC, "_sync_globals", lambda *a, **kw: set()), \
            mock.patch.object(HC, "_reconcile_live_set", _spy_reconcile(_v1(cache_dir), log)):
        got = H.history_records(c, cache=True)
    assert got == H.history_records(c, cache=False)
    assert len(log) == 1
    assert log[0]["kw"]["persist"] is True and log[0]["kw"]["_hold_guard"] is True
    assert log[0]["locked"] is True


@pytest.mark.parametrize("entry", ["history_index", "campaign_history"])
def test_each_id_recomputed_once_per_live_set_change(data_dir, cache_dir, entry):
    c = cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    v1 = _v1(cache_dir)
    other = next(r["campaign"] for r in HC._read_index(v1)
                 if r["campaign"] != _A and r.get("outcome") in HC._TERMINAL_OUTCOMES)
    # one change: a new id, and (a global source) two mails each naming one existing id, so the
    # two terminals are in ``affected`` (global source) and in the reconcile's ``differing``
    _install_terminal_campaign(data_dir, _N)
    _add_mail(data_dir, f"live-set-marker {_A}", "mail-one")
    _add_mail(data_dir, f"live-set-marker {other}", "mail-two")

    def owned(doc):
        return sorted(HC._global_tuple(r) for r in doc["records"]
                      if r.get("source") not in HC._LOCAL_SOURCES)

    differing = {
        r["campaign"] for r in HC._read_index(v1)
        if r.get("outcome") in HC._TERMINAL_OUTCOMES
        and owned(json.loads((v1 / "campaigns" / f"{r['campaign']}.json").read_text()))
        != owned(H.campaign_history(c, r["campaign"], now=NOW, cache=False))
    }
    assert {_A, other} <= differing
    before_records = HC._read_records(v1)
    counts: collections.Counter = collections.Counter()
    real = H._campaign_history

    def spy(cfg_, cid, *a, **kw):
        counts[cid] += 1
        return real(cfg_, cid, *a, **kw)

    with mock.patch.object(H, "_campaign_history", spy):
        if entry == "history_index":
            H.history_index(c, now=NOW, cache=True)
        else:
            H.campaign_history(c, _A, now=NOW, cache=True)
    # a changed global source (the mailbox) also puts every campaign that owns a mailbox
    # record into ``affected``; those are recomputed too, but each still exactly once
    mailbox_owners = {r["campaign"] for r in before_records
                      if r.get("source") == "mailbox" and r.get("campaign")}
    expected = differing | {_N} | mailbox_owners
    assert set(counts) == expected, counts
    assert set(counts.values()) == {1}, counts


def test_no_drop_campaign_all_in_src():
    src_root = Path(__file__).resolve().parents[1] / "src" / "nightwright"
    assert src_root.is_dir()
    assert (src_root / "agentic" / "history_cache.py").is_file()
    files = list(src_root.rglob("*.py"))
    assert files
    assert [p for p in files if "drop_campaign_all" in p.read_text()] == []


def _persist_calls_spy():
    calls: list = []
    real = HC._persist_index_records

    def spy(*a, **kw):
        calls.append({**kw, "sources": copy.deepcopy(kw.get("sources"))})
        return real(*a, **kw)

    return calls, spy


@pytest.mark.parametrize("persist", [False, True])
def test_reconcile_persist_contract_recompute_path(data_dir, cache_dir, persist):
    c = cfg(data_dir, cache_dir)
    _add_mail(data_dir, f"live-set-marker {_A} {_N}")
    H.history_index(c, now=NOW, cache=True)
    v1 = _v1(cache_dir)
    _install_terminal_campaign(data_dir, _N)
    names = ("index.jsonl", "records.jsonl", "sources.json")
    before = {n: (v1 / n).read_bytes() for n in names}
    a_before = (v1 / "campaigns" / f"{_A}.json").read_bytes()
    calls, spy = _persist_calls_spy()
    idx, recs, src = _load(v1)
    idx_on_disk = HC._read_index(v1)
    with mock.patch.object(HC, "_persist_index_records", spy):
        out = HC._reconcile_live_set(c, v1, NOW, idx, recs, src, persist=persist)
    assert {_A, _N} <= out[3]
    assert (v1 / "campaigns" / f"{_A}.json").read_bytes() != a_before
    if not persist:
        assert calls == []
        assert {n: (v1 / n).read_bytes() for n in names} == before
        n_file = v1 / "campaigns" / f"{_N}.json"
        assert n_file.is_file()
        assert json.loads(n_file.read_text())["campaign"] == _N
        assert any(r.get("campaign") == _N for r in out[0])
        assert any(r.get("campaign") == _N and r.get("source") in HC._LOCAL_SOURCES
                   for r in out[1])
        assert _N in out[2]["campaign_files"]
        assert HC._read_index(v1) == idx_on_disk
        return
    assert len(calls) == 1
    seen: list[str] = []
    real_ch = H._campaign_history
    with mock.patch.object(H, "_campaign_history",
                           lambda cfg_, cid, *a, **kw: seen.append(cid) or real_ch(cfg_, cid, *a, **kw)):
        for cid in (_A, _N):
            assert H.campaign_history(c, cid, now=NOW, cache=True)["campaign"] == cid
    assert seen == []


def test_reconcile_persist_sources_none(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    H.history_index(c, now=NOW, cache=True)
    v1 = _v1(cache_dir)
    _install_terminal_campaign(data_dir, _N)
    names = ("index.jsonl", "records.jsonl", "sources.json")
    before = {n: (v1 / n).read_bytes() for n in names}
    idx, recs, _ = _load(v1)
    HC._reconcile_live_set(c, v1, NOW, idx, recs, None, persist=False)
    assert {n: (v1 / n).read_bytes() for n in names} == before
    on_disk = json.loads(before["sources.json"])
    idx, recs, _ = _load(v1)
    calls, spy = _persist_calls_spy()
    with mock.patch.object(HC, "_persist_index_records", spy):
        HC._reconcile_live_set(c, v1, NOW, idx, recs, None, persist=True)
    assert len(calls) == 1
    got = calls[0]["sources"]
    assert isinstance(got, dict) and _N not in on_disk["campaign_files"]
    assert {k: v for k, v in got.items() if k not in ("campaign_files", "campaign_rg_dirs")} == {
        k: v for k, v in on_disk.items() if k not in ("campaign_files", "campaign_rg_dirs")}
    assert {k: v for k, v in got["campaign_files"].items() if k != _N} == on_disk["campaign_files"]
    assert _N in got["campaign_files"]
    assert _N in json.loads((v1 / "sources.json").read_text())["campaign_files"]


def test_cold_filled_branch_reconciles_under_lock(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    v1 = _v1(cache_dir)
    _add_mail(data_dir, f"live-set-marker {_A} {_N}")  # links to _A until _N is live
    real_ensure = HC._ensure_writable_cache
    state = {"inside": False}
    log: list = []
    recorded = {"file": [], "pending": [], "persist": []}

    def ensure(*a, **kw):
        out = real_ensure(*a, **kw)
        if out and not state.get("installed"):
            state["installed"] = True
            _install_terminal_campaign(data_dir, _N)
        return out

    real_re = HC._reconcile_live_set

    def reconcile(*a, **kw):
        log.append(_lock_held(v1))
        state["inside"] = True
        try:
            return real_re(*a, **kw)
        finally:
            state["inside"] = False

    def recording(name, real):
        def spy(*a, **kw):
            if state["inside"]:
                recorded[name].append(_lock_held(v1))
            return real(*a, **kw)
        return spy

    with mock.patch.object(HC, "_ensure_writable_cache", ensure), \
            mock.patch.object(HC, "_reconcile_live_set", reconcile), \
            mock.patch.object(HC, "_write_campaign_file",
                              recording("file", HC._write_campaign_file)), \
            mock.patch.object(HC, "_append_pending_index_write",
                              recording("pending", HC._append_pending_index_write)), \
            mock.patch.object(HC, "_persist_index_records",
                              recording("persist", HC._persist_index_records)):
        rows = H.history_index(c, now=NOW, cache=True)
    assert log == [True]
    for name, seen in recorded.items():
        assert seen, f"no {name} write recorded inside the reconcile"
        assert all(seen), name
    assert rows == H.history_index(c, now=NOW, cache=False)
    _assert_cache_equals_pure(c, cache_dir)


def test_reconcile_one_global_resolve_and_snapshot(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    _add_mail(data_dir, f"live-set-marker {_A} {_N}")
    H.history_index(c, now=NOW, cache=True)
    v1 = _v1(cache_dir)
    # a complete unparsable line makes the global sources report a gap, so the recomputed
    # document's ``gaps`` can only match the pure path if ``extra_gaps`` reaches it
    with (data_dir / "agentic" / "mailbox" / "human.jsonl").open("a") as stream:
        stream.write("not json\n")
    _install_terminal_campaign(data_dir, _N)
    live_at_entry = sorted(HC._live_campaign_ids(c))
    glob_calls: list = []
    live_calls: list = []
    real_g, real_l = H._global_records, HC._live_campaign_ids

    def glob(cfg_, cids, gaps, *a, **kw):
        glob_calls.append(list(cids))
        return real_g(cfg_, cids, gaps, *a, **kw)

    def live(*a, **kw):
        live_calls.append(1)
        return real_l(*a, **kw)

    idx, recs, src = _load(v1)
    with mock.patch.object(H, "_global_records", glob), \
            mock.patch.object(HC, "_live_campaign_ids", live):
        out = HC._reconcile_live_set(c, v1, NOW, idx, recs, src, persist=False)
    assert {_A, _N} <= out[3]
    assert glob_calls == [live_at_entry]
    assert live_calls == [1]
    stored = json.loads((v1 / "campaigns" / f"{_A}.json").read_text())
    pure_gaps = H.campaign_history(c, _A, now=NOW, cache=False)["gaps"]
    assert any("unparsable" in g for g in pure_gaps), pure_gaps
    assert any("unparsable" in g for g in stored["gaps"]), stored["gaps"]
    assert stored["gaps"] == pure_gaps


def test_reconcile_directory_added_mid_call_picked_up_next_call(data_dir, cache_dir):
    c = cfg(data_dir, cache_dir)
    late = "SPEC-0394-20261112-cccc"
    _add_mail(data_dir, f"live-set-marker {_A} {_N} {late}")
    H.history_index(c, now=NOW, cache=True)
    v1 = _v1(cache_dir)
    _install_terminal_campaign(data_dir, _N)
    real = HC._recompute_campaign
    state = {"n": 0}

    def wrapper(cfg_, cid, *a, **kw):
        state["n"] += 1
        if state["n"] == 1:
            _install_terminal_campaign(data_dir, late)
        return real(cfg_, cid, *a, **kw)

    glob_calls: list = []
    real_g = H._global_records

    def glob(cfg_, cids, gaps, *a, **kw):
        glob_calls.append(list(cids))
        return real_g(cfg_, cids, gaps, *a, **kw)

    idx, recs, src = _load(v1)
    with mock.patch.object(HC, "_recompute_campaign", wrapper), \
            mock.patch.object(H, "_global_records", glob):
        idx, recs, src, touched = HC._reconcile_live_set(
            c, v1, NOW, idx, recs, src, persist=True)
    # one snapshot: the single global parse never saw ``late`` and every stored/returned
    # record resolved against the entry set
    assert len(glob_calls) == 1
    assert late not in glob_calls[0] and {_A, _N} <= set(glob_calls[0])
    for cid in (_A, _N):
        doc = json.loads((v1 / "campaigns" / f"{cid}.json").read_text())
        assert all(r.get("campaign") != late for r in doc["records"])
    assert all(r.get("campaign") != late for r in recs)
    stored_a = json.loads((v1 / "campaigns" / f"{_A}.json").read_text())
    assert (sorted(HC._global_tuple(r) for r in recs if r.get("campaign") == _A
                   and r.get("source") not in HC._LOCAL_SOURCES)
            == sorted(HC._global_tuple(r) for r in stored_a["records"]
                      if r.get("source") not in HC._LOCAL_SOURCES))
    assert late not in touched and all(r["campaign"] != late for r in idx)
    assert all(r.get("campaign") != late for r in recs if r.get("source") in HC._LOCAL_SOURCES)
    idx2, recs2, src2 = _load(v1)
    assert all(r["campaign"] != late for r in idx2)
    _, _, _, touched2 = HC._reconcile_live_set(c, v1, NOW, idx2, recs2, src2, persist=True)
    assert late in touched2
    assert any(r["campaign"] == late for r in HC._read_index(v1))
    _assert_cache_equals_pure(c, cache_dir)
