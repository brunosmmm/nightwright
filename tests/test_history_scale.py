"""SPEC-0460 AC7: history_scale clone and byte helpers."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from unittest import mock

import pytest

import history_scale as HS

FIXTURE_AGENTIC = HS.FIXTURE_DIR / "agentic"
SOURCES = sorted(p.name for p in (FIXTURE_AGENTIC / "campaigns").iterdir() if p.is_dir())
MINTED_RE = re.compile(r"-\d{8}-[0-9a-f]{4}$")


def _sha_tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_place_bytes_hardlink_or_copy(tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"payload")
    linked = tmp_path / "link.bin"
    HS.place_bytes(src, linked)
    assert linked.read_bytes() == b"payload"
    if hasattr(os, "link"):
        try:
            assert linked.stat().st_ino == src.stat().st_ino
        except OSError:
            pass
    copy_dst = tmp_path / "copy.bin"
    with mock.patch("os.link", side_effect=OSError("nope")):
        HS.place_bytes(src, copy_dst)
    assert copy_dst.read_bytes() == b"payload"
    assert not copy_dst.is_symlink()


def test_retarget_record():
    top = HS.retarget_record({"campaign": "a", "source": {"campaign": "b"}}, "c")
    assert top["campaign"] == "c"
    assert top["source"]["campaign"] == "b"
    nested = HS.retarget_record({"source": {"campaign": "a"}}, "z")
    assert nested["source"]["campaign"] == "z"
    with pytest.raises(ValueError):
        HS.retarget_record({"x": 1}, "c")


def test_shift_timestamps():
    delta = dt.timedelta(days=2)
    stamp = "2026-11-01T12:00:00-05:00"
    micro = "2026-11-01T12:00:00.123456-05:00"
    assert HS.shift_timestamps(stamp, delta) == "2026-11-03T12:00:00-05:00"
    assert "." in HS.shift_timestamps(micro, delta)
    obj = {"ts": stamp, "items": [stamp]}
    out = HS.shift_timestamps(obj, delta)
    assert out["ts"] == "2026-11-03T12:00:00-05:00"
    assert out["items"][0] == out["ts"]


def test_clone_fixture_determinism(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    HS.clone_fixture(a, campaigns=12, records=0, seed=0)
    HS.clone_fixture(b, campaigns=12, records=0, seed=0)
    assert _sha_tree(a) == _sha_tree(b)


def test_clone_fixture_seed_changes_minted_ids(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    HS.clone_fixture(a, campaigns=10, records=0, seed=0)
    HS.clone_fixture(b, campaigns=10, records=0, seed=7)
    ids_a = {p.name for p in (a / "agentic" / "campaigns").iterdir()}
    ids_b = {p.name for p in (b / "agentic" / "campaigns").iterdir()}
    assert set(SOURCES).issubset(ids_a)
    assert ids_a != ids_b


def test_identity_ids_and_inode_isolation(tmp_path):
    root = tmp_path / "clone"
    HS.clone_fixture(root, campaigns=9, records=0, seed=0)
    assert "SPEC-0195-20261025-hdof" in SOURCES
    assert (root / "agentic" / "campaigns" / "SPEC-0195-20261025-hdof").is_dir()
    fixture_inodes = {p.stat().st_ino for p in HS.FIXTURE_DIR.rglob("*") if p.is_file()}
    for path in root.rglob("*"):
        if path.is_file():
            assert path.stat().st_ino not in fixture_inodes
    for cid in SOURCES:
        for rel, meta in _manifest_files_for(cid):
            got = root / rel
            assert hashlib.sha256(got.read_bytes()).hexdigest() == meta["sha256"]


def _manifest_files_for(cid: str) -> list[tuple[str, dict]]:
    manifest = json.loads((HS.FIXTURE_DIR / "MANIFEST.json").read_text())
    prefix = f"agentic/campaigns/{cid}/"
    return [(k, v) for k, v in manifest["files"].items() if k.startswith(prefix)]


def test_minted_ids_and_hdof_pattern(tmp_path):
    root = tmp_path / "clone"
    HS.clone_fixture(root, campaigns=15, records=0, seed=0)
    minted = [p.name for p in (root / "agentic" / "campaigns").iterdir() if p.name not in SOURCES]
    assert len(set(minted)) == len(minted)
    assert all(MINTED_RE.search(name) for name in minted)
    hdof_minted = [n for n in minted if n.startswith("SPEC-0195-20261025-hdof-")]
    assert hdof_minted and all(re.search(r"hdof-20261025-[0-9a-f]{4}$", n) for n in hdof_minted)


def test_fixture_prefix_bytes_and_event_id(tmp_path):
    root = tmp_path / "clone"
    HS.clone_fixture(root, campaigns=10, records=0, seed=0)
    fix_dec = (FIXTURE_AGENTIC / "conductor" / "decisions.jsonl").read_bytes()
    assert (root / "agentic/conductor/decisions.jsonl").read_bytes().startswith(fix_dec)
    fix_run = (FIXTURE_AGENTIC / "runner.jsonl").read_bytes()
    assert (root / "agentic/runner.jsonl").read_bytes() == fix_run
    lines = (root / "agentic/conductor/decisions.jsonl").read_text().splitlines()
    fixture_n = len(fix_dec.decode().splitlines())
    appended = [json.loads(line) for line in lines[fixture_n:] if line.strip()]
    event_ids = {row.get("event_id") for row in appended}
    assert None in event_ids or any(":" in str(e) for e in event_ids if e is not None)
    int_rows = [r for r in appended if isinstance(r.get("event_id"), str) and r["event_id"].split(":")[0].isdigit()]
    assert len({r["event_id"] for r in int_rows}) == len(int_rows)


def _finding_count(ledger: Path) -> int:
    return sum(
        1 for line in ledger.read_text().splitlines()
        if line.strip() and json.loads(line).get("type") == "finding"
    )


def test_one_extra_finding_per_extra_clone(tmp_path):
    root = tmp_path / "clone"
    HS.clone_fixture(root, campaigns=9, records=0, seed=0)
    n = len(SOURCES)
    used: set[str] = set(SOURCES)
    for i in range(n, 9):
        source = SOURCES[i % n]
        clone_id = HS._mint_id(source, i, 0, used)
        camp = root / "agentic" / "campaigns" / clone_id
        src_n = _finding_count(FIXTURE_AGENTIC / "campaigns" / source / "ledger.jsonl")
        got = _finding_count(camp / "ledger.jsonl")
        assert got == src_n + 1, (source, clone_id, src_n, got)


def test_extra_finding_when_source_has_none(tmp_path):
    root = tmp_path / "clone"
    HS.clone_fixture(root, campaigns=9, records=0, seed=0)
    idea_clone = next(
        p for p in (root / "agentic" / "campaigns").iterdir()
        if p.name.startswith("IDEA-606-20261101-0")
    )
    assert _finding_count(idea_clone / "ledger.jsonl") == 1


def test_copy_fixture_unchanged(tmp_path):
    dst = tmp_path / "dst"
    HS.copy_fixture(dst)
    (dst / "marker").write_text("x")
    with pytest.raises(FileExistsError):
        HS.copy_fixture(dst)


def test_clone_fixture_identity_records_one(tmp_path):
    root = tmp_path / "clone"
    HS.clone_fixture(root, campaigns=8, records=1, seed=0)
    for cid in SOURCES:
        src_n = _finding_count(FIXTURE_AGENTIC / "campaigns" / cid / "ledger.jsonl")
        got = _finding_count(root / "agentic" / "campaigns" / cid / "ledger.jsonl")
        assert got == src_n
        src_lines = len((FIXTURE_AGENTIC / "campaigns" / cid / "ledger.jsonl").read_text().splitlines())
        got_lines = len((root / "agentic" / "campaigns" / cid / "ledger.jsonl").read_text().splitlines())
        assert got_lines <= src_lines
    with pytest.raises(ValueError):
        HS.clone_fixture(tmp_path / "too-big", campaigns=8, records=10**9, seed=0)


def test_clone_preserves_nested_held_campaign_in_ledger(tmp_path):
    dst = tmp_path / "clone"
    HS.clone_fixture(dst, campaigns=15, records=0, seed=0)
    other = "IDEA-533-20261031-f316"
    src_cid = "SPEC-0336-20261031-6558"
    minted = None
    for d in (dst / "agentic" / "campaigns").iterdir():
        if d.name.startswith("SPEC-0336-") and d.name != src_cid:
            minted = d
            break
    assert minted is not None
    for line in (minted / "ledger.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("type") == "gate_waiting":
            held = row.get("held") or []
            for slot in held:
                if isinstance(slot, dict) and slot.get("campaign") == other:
                    return
    pytest.fail("gate_waiting held[].campaign for unrelated campaign not preserved")


def test_clone_integer_event_id_rewrite(tmp_path):
    root = tmp_path / "clone"
    HS.clone_fixture(root, campaigns=10, records=0, seed=0)
    lines = (root / "agentic/conductor/decisions.jsonl").read_text().splitlines()
    fixture_n = len((FIXTURE_AGENTIC / "conductor/decisions.jsonl").read_bytes().decode().splitlines())
    for row in [json.loads(line) for line in lines[fixture_n:] if line.strip()]:
        eid = row.get("event_id")
        if eid is None:
            continue
        if isinstance(eid, str) and eid.split(":")[-1].isdigit():
            prefix, idx = eid.rsplit(":", 1)
            assert prefix in ("3885", "3924") or not prefix.isdigit()
            assert int(idx) >= 8


def test_history_scale_does_not_import_nightwright():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "history_scale_isolated",
        Path(__file__).resolve().parent / "history_scale.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "nightwright" not in getattr(mod, "__dict__", {})
