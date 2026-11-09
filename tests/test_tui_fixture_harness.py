"""SPEC-0459: IdeaDesk on the real history fixture, and the queue's 24 h window."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import textual
from textual.events import Click

import history_scale as HS
import tui_harness as TH
from nightwright import report as R
from nightwright.agentic import queue as Q
from nightwright.tui import agentic_model as AM
from nightwright.tui.app import IdeaDesk
from nightwright.tui.campaign_detail import CampaignDetail
from nightwright.tui.queue_tab import QueueTable, QueueView, _ID_W
from waits import pilot_until

pytestmark = pytest.mark.tui

ROOT = Path(__file__).resolve().parents[1]
NEWEST = dt.datetime.fromisoformat("2026-11-03T21:14:52-05:00")


def _cids() -> list[str]:
    manifest = json.loads((HS.FIXTURE_DIR / "MANIFEST.json").read_text(encoding="utf-8"))
    return sorted(entry["cid"] for entry in manifest["campaigns"])


def _parse(text: str) -> dt.datetime:
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


def _anchor(data_dir, cid: str) -> dt.datetime:
    camp = Path(data_dir) / "agentic" / "campaigns" / cid
    state = json.loads((camp / "state.json").read_text(encoding="utf-8"))
    found = None
    ledger = camp / "ledger.jsonl"
    if ledger.is_file():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("type") == "transition" and event.get("to") == state.get("state") and event.get("ts"):
                found = event["ts"]
    if found is None:
        found = state.get("updated_at") or state.get("created_at")
    return _parse(found)


def _ledger_gap(path: Path) -> float:
    stamps = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("ts"):
            stamps.append(_parse(event["ts"]))
    return (stamps[-1] - stamps[0]).total_seconds()


def _global_files(data: Path) -> list[Path]:
    agentic = data / "agentic"
    files = [
        agentic / "conductor" / "decisions.jsonl",
        agentic / "events.jsonl",
        agentic / "spec-reviews.jsonl",
    ]
    files.extend(sorted((agentic / "mailbox").glob("*.jsonl")))
    files.extend(sorted((agentic / "escalations").glob("*.json")))
    return [path for path in files if path.is_file()]


def _locator(obj) -> str | None:
    if not isinstance(obj, dict):
        return None
    if obj.get("campaign"):
        return obj["campaign"]
    source = obj.get("source")
    if isinstance(source, dict) and source.get("campaign"):
        return source["campaign"]
    return None


def _records(path: Path) -> list[tuple[bytes, str | None]]:
    """`(raw, locator)` for each jsonl line, or the whole document for an escalation file."""
    raw = path.read_bytes()
    if path.suffix == ".jsonl":
        out = []
        for line in raw.splitlines(keepends=True):
            body = line.strip()
            locator = None
            if body:
                try:
                    locator = _locator(json.loads(body.decode("utf-8")))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    locator = None
            out.append((line, locator))
        return out
    try:
        locator = _locator(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError):
        locator = None
    return [(raw, locator)]


def _hashes(root: Path) -> dict[str, str]:
    found = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            found[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def _clipped(row) -> str:
    campaign = row.campaign
    if row.spec and campaign.startswith(row.spec + "-"):
        campaign = campaign[len(row.spec) + 1:]
    return R._clip(f"{row.spec} {campaign}".strip(), _ID_W)


def _delivered(app):
    view = app.query_one("#queue-view", QueueView)
    return [row for row in view.rows if row.kind == "campaign" and row.group == "delivered"]


def _run(coro):
    return asyncio.run(coro)


def _under(root: Path, value) -> None:
    path = Path(value).resolve()
    assert path == root or root in path.parents, path


# --- cfg -----------------------------------------------------------------------------------


def test_fixture_cfg_is_isolated(tmp_path, monkeypatch):
    work = tmp_path / "work"
    sentinels = {}
    for key in ("NW_DATA_DIR", "NW_CONFIG_DIR", "HOME"):
        directory = tmp_path / "sentinels" / key
        directory.mkdir(parents=True)
        sentinels[key] = directory
        monkeypatch.setenv(key, str(directory))
    cfg = TH.fixture_cfg(work)
    data = Path(cfg["data_dir"])
    manifest = json.loads((HS.FIXTURE_DIR / "MANIFEST.json").read_text(encoding="utf-8"))
    campaign_files = [rel for rel in manifest["files"] if rel.startswith("agentic/campaigns/")]
    assert campaign_files
    for rel in campaign_files:
        digest = hashlib.sha256((data / rel).read_bytes()).hexdigest()
        assert digest == manifest["files"][rel]["sha256"]
    copy_ids = set()
    for path in data.rglob("*"):
        if not path.is_file():
            continue
        src = HS.FIXTURE_DIR / path.relative_to(data)
        assert src.is_file()
        copied, original = path.stat(), src.stat()
        assert (copied.st_dev, copied.st_ino) != (original.st_dev, original.st_ino)
        copy_ids.add((copied.st_dev, copied.st_ino))
    fixture_ids = {
        (path.stat().st_dev, path.stat().st_ino)
        for path in HS.FIXTURE_DIR.rglob("*") if path.is_file()
    }
    assert copy_ids.isdisjoint(fixture_ids)
    root = work.resolve()
    for key in ("data_dir", "config_dir", "cache_dir", "specs_dir",
                "org_ideas_file", "org_capture_file"):
        _under(root, cfg[key])
    for org in cfg["org_files"]:
        _under(root, org)
    assert cfg["org_todo_keywords"] == ["TODO", "|", "DONE"]
    assert cfg["org_idea_keywords"] == ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"]
    assert cfg["org_question_keywords"] == ["OPEN", "|", "RESOLVED"]
    assert cfg["timezone"] == "America/New_York"
    assert cfg["agentic"]["runner"] == "fake"
    assert Path(cfg["org_ideas_file"]).read_text(encoding="utf-8").splitlines() == [
        "#+TODO: IDEA | PROMOTED"]
    for directory in sentinels.values():
        assert list(directory.rglob("*")) == []


# --- queue window --------------------------------------------------------------------------


def test_queue_window_order_matches_assignment(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    now = dt.datetime(2026, 11, 7, 15, 0, tzinfo=ZoneInfo("America/New_York"))
    cids = _cids()
    n = len(cids)
    assert TH.prepare_queue_window(cfg, now=now) == now
    for i, cid in enumerate(cids):
        anchor = _anchor(cfg["data_dir"], cid)
        target = now - dt.timedelta(hours=(n - i))
        assert abs((anchor - target).total_seconds()) <= 1
    payload = Q.dashboard_payload(cfg, recent=20, now=now)
    delivered = payload["delivered"]
    assert [item["campaign"] for item in delivered] == list(reversed(cids))
    ages = [item["finished_age_s"] for item in delivered]
    assert ages == sorted(ages)
    assert delivered[0]["campaign"] == cids[-1]
    rows = AM.rows_from_payload(payload, now=now)
    data = [row for row in rows if row.kind == "campaign" and row.group == "delivered"]
    headers = [row for row in rows if row.kind == "header" and row.group == "delivered"]
    assert data[0].campaign == cids[-1]
    assert [row.campaign for row in data] == list(reversed(cids))
    assert len(headers) == 1 and headers[0].count == 8


def test_prepare_queue_window_preserves_span_and_runner(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    data = Path(cfg["data_dir"])
    cids = _cids()
    cid = next(c for c in cids if any(
        locator == c for path in _global_files(data) for _, locator in _records(path)))
    other = next(c for c in cids if c != cid and any(
        locator == c for path in _global_files(data) for _, locator in _records(path)))
    ledger = data / "agentic" / "campaigns" / cid / "ledger.jsonl"
    gap = _ledger_gap(ledger)
    runner = (data / "agentic" / "runner.jsonl").read_bytes()
    before_anchor = _anchor(data, cid)
    other_anchor = _anchor(data, other)
    before = {path: _records(path) for path in _global_files(data)}
    named = _first_ts(before, cid)
    other_named = _first_ts(before, other)
    now = dt.datetime(2026, 11, 7, 15, 0, tzinfo=ZoneInfo("America/New_York"))
    TH.prepare_queue_window(cfg, now=now)
    assert abs(_ledger_gap(ledger) - gap) <= 1
    assert (data / "agentic" / "runner.jsonl").read_bytes() == runner
    after = {path: _records(path) for path in _global_files(data)}
    for path, records in before.items():
        assert len(records) == len(after[path])
        for (raw, locator), (new, new_locator) in zip(records, after[path]):
            assert new_locator == locator
            if locator not in cids:
                assert new == raw
    assert _moved_with(_anchor(data, cid) - before_anchor, named, after)
    # A line for a different campaign moves by that campaign's delta, not this one's.
    assert _moved_with(_anchor(data, other) - other_anchor, other_named, after)
    chosen_delta = _anchor(data, cid) - before_anchor
    other_delta = _anchor(data, other) - other_anchor
    assert abs((chosen_delta - other_delta).total_seconds()) > 60


def _first_ts(records_by_path, cid: str):
    for path, records in records_by_path.items():
        for index, (raw, locator) in enumerate(records):
            if locator != cid:
                continue
            try:
                obj = json.loads(raw.strip().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if obj.get("ts"):
                return path, index, _parse(obj["ts"])
    raise AssertionError(f"no global-log ts for {cid}")


def _moved_with(anchor_delta: dt.timedelta, found, after) -> bool:
    path, index, before_ts = found
    raw, _locator = after[path][index]
    obj = json.loads(raw.strip().decode("utf-8"))
    moved = _parse(obj["ts"]) - before_ts
    return abs((moved - anchor_delta).total_seconds()) <= 1


def test_prepare_queue_window_refuses_the_committed_fixture(tmp_path):
    with pytest.raises(ValueError):
        TH.prepare_queue_window({"data_dir": HS.FIXTURE_DIR})
    with pytest.raises(ValueError):
        TH.prepare_queue_window({"data_dir": HS.FIXTURE_DIR / "agentic"})
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError):
        TH.prepare_queue_window({"data_dir": empty})
    # The raises above must not have written the fixture. A missing campaigns dir is the
    # second refusal; the fixture path is the first.
    assert (HS.FIXTURE_DIR / "MANIFEST.json").is_file()


# --- pilot ---------------------------------------------------------------------------------


def test_unshifted_copy_hides_closed_campaigns(tmp_path):
    now = dt.datetime.now().astimezone()
    if now - NEWEST <= dt.timedelta(hours=24):
        pytest.fail(
            f"wall clock {now.isoformat()} is not more than 24h after "
            f"2026-11-03T21:14:52-05:00")
    cfg = TH.fixture_cfg(tmp_path)
    cids = _cids()
    app = IdeaDesk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=TH.SIZE) as pilot:
            def settled():
                view = app.query_one("#queue-view", QueueView)
                text = TH.visible_text(app)
                if "queue failed" in text:
                    return True
                return bool(view.rows) and view.header.startswith("runner ")

            assert await pilot_until(pilot, settled)
            text = TH.visible_text(app)
            assert "queue failed" not in text, text
            for cid in cids:
                assert cid not in text
            assert "delivered (" not in text

    _run(drive())


def test_shifted_queue_lists_every_fixture_campaign(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    cids = _cids()
    app = IdeaDesk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=TH.SIZE) as pilot:
            assert await pilot_until(pilot, lambda: "delivered (8)" in TH.visible_text(app))
            rows = _delivered(app)
            assert rows[0].campaign == cids[-1]
            assert {row.campaign for row in rows} == set(cids)
            assert len(rows) == len(cids) == 8
            text = TH.visible_text(app)
            assert "delivered (8)" in text
            assert _clipped(rows[0]) in text
            assert textual.events.Click is Click

    _run(drive())


def test_double_click_dispatches_to_the_queue_table(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    app = IdeaDesk(cfg, tab="queue")
    seen: list[Click] = []

    def spy(self, event):
        if isinstance(event, Click):
            seen.append(event)

    QueueTable._on_click = spy

    async def drive():
        async with app.run_test(size=TH.SIZE) as pilot:
            assert await pilot_until(pilot, lambda: "delivered (8)" in TH.visible_text(app))
            before = list(app.screen_stack)
            landed = await pilot.double_click("#queue", offset=(2, 2))
            await pilot.pause(0.05)
            assert landed is True
            assert sum(event.chain == 2 for event in seen) == 1
            assert len(app.screen_stack) == len(before) + 1
            assert isinstance(app.screen_stack[-1], CampaignDetail)
            assert list(app.screen_stack)[:-1] == before
            assert isinstance(app, IdeaDesk)

    try:
        _run(drive())
    finally:
        del QueueTable._on_click


def test_pilot_session_does_not_write_the_copy(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    data = Path(cfg["data_dir"])
    cache = Path(cfg["cache_dir"]).resolve()
    assert data.resolve() not in cache.parents and cache != data.resolve()
    before = _hashes(data)
    app = IdeaDesk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=TH.SIZE) as pilot:
            assert await pilot_until(pilot, lambda: "delivered (8)" in TH.visible_text(app))

    _run(drive())
    assert _hashes(data) == before
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "capture_history_fixture.py"), "--check"],
        cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
