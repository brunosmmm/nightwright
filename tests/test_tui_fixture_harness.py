"""SPEC-0459: IdeaDesk on the real history fixture, and the queue's 24 h window."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import inspect
import json
import os
import re
import shutil
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
from nightwright.tui.campaign_detail import CampaignDetail, DetailOpener
from nightwright.tui.queue_tab import QueueView, _ID_W
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


def _assert_size(app) -> None:
    """The pilot runs at 160x48: the 30-column ident and `delivered (8)` depend on it."""
    assert (app.size.width, app.size.height) == (160, 48)


# Written apart from `tui_harness._ISO` on purpose: group 1 is the offset, if any.
_TOKEN = re.compile(
    rb"(?<!\d)\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(Z|[+-]\d{2}:\d{2})?(?!\d)")


def _token_time(raw: bytes) -> dt.datetime:
    text = raw.decode("ascii")
    return dt.datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)


def _check_unit(before: bytes, after: bytes, delta: dt.timedelta) -> int:
    """Every ISO token in `before` moved by `delta` within 1 s in `after`, with its offset kept
    and everything outside tokens identical. Returns the token count."""
    old = [m for m in _TOKEN.finditer(before)]
    new = [m for m in _TOKEN.finditer(after)]
    assert len(old) == len(new), (before, after)
    for a, b in zip(old, new):
        assert a.group(1) == b.group(1), (a.group(0), b.group(0))
        moved = _token_time(b.group(0)) - _token_time(a.group(0))
        assert abs((moved - delta).total_seconds()) <= 1, (a.group(0), b.group(0), delta)
    assert _TOKEN.sub(b"<ts>", before) == _TOKEN.sub(b"<ts>", after)
    return len(old)


def _snap(path: Path):
    """What an outside target holds: bytes, a hash map for a directory, or None when missing."""
    if path.is_dir():
        return _hashes(path)
    if path.is_file():
        return path.read_bytes()
    return None


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
    fixture_before = _hashes(HS.FIXTURE_DIR)
    assert "MANIFEST.json" in fixture_before
    with pytest.raises(ValueError, match="inside the committed fixture"):
        TH.prepare_queue_window({"data_dir": HS.FIXTURE_DIR})
    with pytest.raises(ValueError, match="inside the committed fixture"):
        TH.prepare_queue_window({"data_dir": HS.FIXTURE_DIR / "agentic"})
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="does not contain agentic/campaigns"):
        TH.prepare_queue_window({"data_dir": empty})
    assert list(empty.iterdir()) == []
    assert _hashes(HS.FIXTURE_DIR) == fixture_before
    # A manifest campaign with no directory refuses before any shift: the copy is untouched.
    cfg = TH.fixture_cfg(tmp_path / "missing")
    data = Path(cfg["data_dir"])
    shutil.rmtree(data / "agentic" / "campaigns" / _cids()[3])
    copy_before = _hashes(data)
    with pytest.raises(ValueError, match="has no directory under"):
        TH.prepare_queue_window(cfg)
    assert _hashes(data) == copy_before


def _link_file(data: Path, outside: Path):
    camp = data / "agentic" / "campaigns" / _cids()[0]
    victim = camp / "state.json"
    outside_file = outside / "target.json"
    outside_file.write_bytes(victim.read_bytes())   # holds the ISO timestamps a write would move
    victim.unlink()
    victim.symlink_to(outside_file)
    return victim, outside_file


def _link_hard(data: Path, outside: Path):
    victim = data / "agentic" / "campaigns" / _cids()[1] / "state.json"
    outside_file = outside / "target.json"
    outside_file.write_bytes(victim.read_bytes())
    victim.unlink()
    os.link(outside_file, victim)
    return victim, outside_file


def _link_dir(data: Path, outside: Path):
    victim = data / "agentic" / "campaigns" / _cids()[2]
    moved = outside / "moved-campaign"
    shutil.move(str(victim), str(moved))
    victim.symlink_to(moved, target_is_directory=True)
    return victim, moved


def _link_global(data: Path, outside: Path):
    victim = data / "agentic" / "events.jsonl"
    outside_file = outside / "events.jsonl"
    outside_file.write_bytes(victim.read_bytes())
    victim.unlink()
    victim.symlink_to(outside_file)
    return victim, outside_file


def _link_inside(data: Path, outside: Path):
    camp = data / "agentic" / "campaigns" / _cids()[3]
    victim = camp / "alias.json"
    victim.symlink_to(camp / "state.json")
    return victim, camp / "state.json"


def _link_dangling_global(data: Path, outside: Path):
    victim = data / "agentic" / "mailbox" / "dangling.jsonl"
    victim.parent.mkdir(exist_ok=True)
    outside_file = outside / "never-created.jsonl"
    victim.symlink_to(outside_file)
    return victim, outside_file


@pytest.mark.parametrize("make", [
    _link_file, _link_hard, _link_dir, _link_global, _link_inside, _link_dangling_global])
def test_prepare_queue_window_refuses_links(tmp_path, make):
    cfg = TH.fixture_cfg(tmp_path / "copy")
    data = Path(cfg["data_dir"]).resolve()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim, target = make(data, outside)   # a link that cannot be made fails the test
    before, outside_before = _hashes(data), _snap(outside)
    target_before = _snap(target)
    with pytest.raises(ValueError, match=re.escape(str(victim))):
        TH.prepare_queue_window(cfg)
    assert _hashes(data) == before
    assert _snap(outside) == outside_before
    assert _snap(target) == target_before


def test_every_matching_token_moves_by_its_campaign_delta(tmp_path):
    cfg = TH.fixture_cfg(tmp_path)
    data = Path(cfg["data_dir"])
    cids = _cids()
    camps = {cid: data / "agentic" / "campaigns" / cid for cid in cids}
    anchors = {cid: _anchor(data, cid) for cid in cids}
    files = {cid: {p: p.read_bytes() for p in sorted(camps[cid].rglob("*")) if p.is_file()}
             for cid in cids}
    globs = {path: _records(path) for path in _global_files(data)}
    TH.prepare_queue_window(cfg)
    checked = 0
    for cid in cids:
        delta = _anchor(data, cid) - anchors[cid]
        assert abs(delta.total_seconds()) > 60
        assert files[cid]
        tokens = 0
        for path, raw in files[cid].items():
            tokens += _check_unit(raw, path.read_bytes(), delta)
        assert tokens > 0, cid
        # for .jsonl the unit is the line; for escalations/*.json it is the whole file
        for path, records in globs.items():
            after = _records(path)
            assert len(after) == len(records)
            for (raw, locator), (new, _) in zip(records, after):
                if locator == cid:
                    checked += _check_unit(raw, new, delta)
    assert checked > 0
    for path, records in globs.items():
        for (raw, locator), (new, _) in zip(records, _records(path)):
            if locator not in cids:
                assert new == raw


def test_shift_datetimes_grammar():
    delta = dt.timedelta(hours=1, minutes=30)
    raw = (
        b'{"z": "2026-11-03T10:00:00Z", "zf": "2026-11-03T10:00:00.5Z",'
        b' "p": "2026-11-03T10:00:00+05:30", "m": "2026-11-03T10:00:00-05:00",'
        b' "frac": "2026-11-03T10:00:00.987654+00:00", "naive": "2026-11-03T10:00:00",'
        b' "midnight": "2026-11-03T23:00:00Z", "month": "2026-11-30T23:00:00Z",'
        b' "date": "2026-11-03", "cid": "SPEC-0100-20261103-ab12",'
        b' "digit_before": "12026-11-03T01:02:03", "digit_after": "2026-11-03T01:02:033",'
        b' "bad": "2026-13-45T00:00:00", "duration_s": 3600.5, "n": 20261103}')
    expected = (
        b'{"z": "2026-11-03T11:30:00Z", "zf": "2026-11-03T11:30:00Z",'
        b' "p": "2026-11-03T11:30:00+05:30", "m": "2026-11-03T11:30:00-05:00",'
        b' "frac": "2026-11-03T11:30:00+00:00", "naive": "2026-11-03T11:30:00",'
        b' "midnight": "2026-11-04T00:30:00Z", "month": "2026-12-01T00:30:00Z",'
        b' "date": "2026-11-03", "cid": "SPEC-0100-20261103-ab12",'
        b' "digit_before": "12026-11-03T01:02:03", "digit_after": "2026-11-03T01:02:033",'
        b' "bad": "2026-13-45T00:00:00", "duration_s": 3600.5, "n": 20261103}')
    assert TH._shift_datetimes(raw, delta) == expected


def test_size_is_160_by_48():
    assert TH.SIZE == (160, 48)


# --- pilot ---------------------------------------------------------------------------------


def test_unshifted_copy_hides_closed_campaigns(tmp_path):
    now = dt.datetime.now().astimezone()
    if now - NEWEST <= dt.timedelta(hours=24):
        pytest.fail(
            f"wall clock {now.isoformat()} is not more than 24h after "
            f"2026-11-03T21:14:52-05:00")
    cfg = TH.fixture_cfg(tmp_path)
    cids = _cids()
    # The idents the screen paints for these campaigns come from the model's own rows on a
    # shifted copy, so the clip rule is not written a second time here.
    shifted = TH.fixture_cfg(tmp_path / "shifted")
    now = TH.prepare_queue_window(shifted)
    rows = AM.rows_from_payload(Q.dashboard_payload(shifted, recent=20, now=now), now=now)
    idents = [_clipped(row) for row in rows if row.kind == "campaign" and row.group == "delivered"]
    assert len(idents) == len(cids) == 8 and all(idents)
    app = IdeaDesk(cfg, tab="queue")

    async def drive():
        async with app.run_test(size=TH.SIZE) as pilot:
            _assert_size(app)

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
            for ident in idents:
                assert ident not in text
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
            _assert_size(app)
            assert _ID_W == 30
            rows = _delivered(app)
            assert rows[0].campaign == cids[-1]
            assert {row.campaign for row in rows} == set(cids)
            assert len(rows) == len(cids) == 8
            text = TH.visible_text(app)
            assert "delivered (8)" in text
            assert _clipped(rows[0]) in text
            assert textual.events.Click is Click

    _run(drive())


def test_double_click_dispatches_to_the_queue_table(tmp_path, monkeypatch):
    cfg = TH.fixture_cfg(tmp_path)
    TH.prepare_queue_window(cfg)
    app = IdeaDesk(cfg, tab="queue")
    seen: list[Click] = []
    # `QueueTable` has no click handler of its own: its `_on_click` is DataTable's and stays
    # untouched. The open-detail handler is `DetailOpener.on_click`; wrap it, calling the original.
    original = DetailOpener.on_click

    async def wrapper(self, event):
        seen.append(event)
        result = original(self, event)
        if inspect.isawaitable(result):
            await result

    monkeypatch.setattr(DetailOpener, "on_click", wrapper)

    async def drive():
        async with app.run_test(size=TH.SIZE) as pilot:
            assert await pilot_until(pilot, lambda: "delivered (8)" in TH.visible_text(app))
            _assert_size(app)
            before = list(app.screen_stack)
            landed = await pilot.double_click("#queue", offset=(2, 2))
            assert landed is True
            assert sum(event.chain == 2 for event in seen) == 1
            assert await pilot_until(pilot, lambda: len(app.screen_stack) == len(before) + 1)
            assert isinstance(app.screen_stack[-1], CampaignDetail)
            assert list(app.screen_stack)[:-1] == before
            assert isinstance(app, IdeaDesk)

    _run(drive())


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
