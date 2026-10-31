"""SPEC-0171: campaign store — ids, atomic state, locked append-only ledger, listing."""
import json
import os
import threading
from pathlib import Path

import pytest

from waits import join
from nightwright.agentic import store as S
from nightwright.agentic import schemas as SC

def _ev(cfg, cid, etype, payload=None, **kw):
    """append_event with the schema's required keys defaulted (SPEC-0192 fixtures)."""
    return S.append_event(cfg, cid, etype, SC.with_defaults(etype, payload), **kw)


def _cfg(tmp_path):
    return {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "cfg"),
            "agentic": {"profiles": {}}}


def _state(cid, spec="SPEC-0170", state="QUEUED", project="Meta-Tools", created="2026-10-25T10:00:00-04:00"):
    return {"id": cid, "spec_id": spec, "project": project, "profile": "balanced", "state": state,
            "created_at": created, "repo": {"base": "main", "worktree": None, "branch": None},
            "counters": {"loops": 0, "runs": 0}, "cost": {"total_usd": 0.0, "cap_usd": 10.0}}


# ---- ids / create / state ------------------------------------------------------------------

def test_new_campaign_id_shape_and_uniqueness(tmp_path):
    cfg = _cfg(tmp_path)
    cid = S.new_campaign_id(cfg, "SPEC-0170")
    assert cid.startswith("SPEC-0170-") and len(cid.split("-")[-1]) == 4
    S.campaign_dir(cfg, cid).mkdir(parents=True)
    assert S.new_campaign_id(cfg, "SPEC-0170") != cid


def test_create_reads_back_and_refuses_duplicate(tmp_path):
    cfg = _cfg(tmp_path)
    d = S.create_campaign(cfg, _state("C1"))
    assert (d / "state.json").is_file()
    st = S.read_state(cfg, "C1")
    assert st["schema"] == "nw.campaign.v1" and st["state"] == "QUEUED" and st["updated_at"]
    events = S.read_ledger(cfg, "C1")
    assert [e["type"] for e in events] == ["open"] and events[0]["seq"] == 1
    assert events[0]["spec_id"] == "SPEC-0170" and events[0]["by"] == "human"
    with pytest.raises(FileExistsError):
        S.create_campaign(cfg, _state("C1"))


def test_read_state_missing_errors(tmp_path):
    with pytest.raises(ValueError, match="no campaign 'nope'"):
        S.read_state(_cfg(tmp_path), "nope")


def test_write_state_is_atomic(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    S.create_campaign(cfg, _state("C1"))
    before = S.read_state(cfg, "C1")

    def boom(src, dst):
        raise OSError("simulated crash before replace")
    monkeypatch.setattr(S.os, "replace", boom)
    with pytest.raises(OSError):
        S.write_state(cfg, "C1", {**before, "state": "OPENING"})
    monkeypatch.undo()
    assert S.read_state(cfg, "C1")["state"] == "QUEUED"          # old content intact
    assert not list(S.campaign_dir(cfg, "C1").glob(".nw-export-*"))  # temp cleaned up
    S.write_state(cfg, "C1", {**before, "state": "OPENING"})
    assert S.read_state(cfg, "C1")["state"] == "OPENING"


# ---- ledger ----------------------------------------------------------------------------

def test_append_event_envelope_and_seq(tmp_path):
    cfg = _cfg(tmp_path)
    S.create_campaign(cfg, _state("C1"))
    rec = _ev(cfg, "C1", "transition", {"from": "QUEUED", "to": "OPENING"}, by="tick")
    assert rec["seq"] == 2 and rec["campaign"] == "C1" and rec["by"] == "tick"
    assert rec["from"] == "QUEUED" and "ts" in rec
    raw = (S.campaign_dir(cfg, "C1") / "ledger.jsonl").read_text().splitlines()
    assert len(raw) == 2 and json.loads(raw[1]) == rec


def test_concurrent_appends_are_serialized(tmp_path):
    cfg = _cfg(tmp_path)
    S.create_campaign(cfg, _state("C1"))

    def worker(n):
        for i in range(50):
            _ev(cfg, "C1", "note", {"text": f"{n}-{i}"}, by="human")
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    events = S.read_ledger(cfg, "C1")
    assert len(events) == 401                                   # open + 400 notes
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(1, 402))                          # strictly increasing, no gaps
    assert len({e["text"] for e in events if e["type"] == "note"}) == 400


def test_read_ledger_skips_truncated_line_and_filters(tmp_path):
    cfg = _cfg(tmp_path)
    S.create_campaign(cfg, _state("C1"))
    _ev(cfg, "C1", "note", {"text": "a"})
    _ev(cfg, "C1", "gate", {"ok": True})
    path = S.campaign_dir(cfg, "C1") / "ledger.jsonl"
    with open(path, "a") as f:
        f.write('{"ts": "2026", "seq": 4, "type": "cos')     # truncated last line
    events = S.read_ledger(cfg, "C1")
    assert [e["type"] for e in events] == ["open", "note", "gate"]
    assert [e["type"] for e in S.read_ledger(cfg, "C1", types=["gate"])] == ["gate"]
    assert [e["type"] for e in S.read_ledger(cfg, "C1", tail=1)] == ["gate"]
    # the next append still gets a monotonic seq despite the garbage line
    assert _ev(cfg, "C1", "note", {"text": "b"})["seq"] == 4


def test_read_ledger_missing_is_empty(tmp_path):
    assert S.read_ledger(_cfg(tmp_path), "nope") == []


# ---- locks ------------------------------------------------------------------------------

def test_item_lock_excludes_second_holder(tmp_path):
    cfg = _cfg(tmp_path)
    import fcntl
    with S.item_lock(cfg, "C1"):
        fd = os.open(S.campaign_dir(cfg, "C1") / ".lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
    with S.item_lock(cfg, "C1"):     # released: re-acquirable
        pass


# ---- listing ----------------------------------------------------------------------------

def test_list_campaigns_filters_and_order(tmp_path):
    cfg = _cfg(tmp_path)
    assert S.list_campaigns(cfg) == []
    S.create_campaign(cfg, _state("A", created="2026-10-25T10:00:00-04:00"))
    S.create_campaign(cfg, _state("B", spec="SPEC-0171", state="MERGED", created="2026-10-25T11:00:00-04:00"))
    S.create_campaign(cfg, _state("C", project="acme", state="GATING", created="2026-10-25T12:00:00-04:00"))
    (S.campaigns_dir(cfg) / "stray").mkdir()                     # no state.json: ignored
    assert [s["id"] for s in S.list_campaigns(cfg)] == ["C", "A"]
    assert [s["id"] for s in S.list_campaigns(cfg, include_terminal=True)] == ["C", "B", "A"]
    assert [s["id"] for s in S.list_campaigns(cfg, project="acme")] == ["C"]
    assert [s["id"] for s in S.list_campaigns(cfg, state="GATING")] == ["C"]
    assert [s["id"] for s in S.list_campaigns(cfg, include_terminal=True, spec_id="SPEC-0171")] == ["B"]


def test_active_campaign_for_spec(tmp_path):
    cfg = _cfg(tmp_path)
    S.create_campaign(cfg, _state("A", state="MERGED"))
    assert S.active_campaign_for_spec(cfg, "SPEC-0170") is None
    S.create_campaign(cfg, _state("B", state="GATING"))
    assert S.active_campaign_for_spec(cfg, "SPEC-0170")["id"] == "B"


def test_paths(tmp_path):
    cfg = _cfg(tmp_path)
    assert S.agentic_dir(cfg) == Path(cfg["data_dir"]) / "agentic"
    assert S.runs_dir(cfg, "X") == S.campaign_dir(cfg, "X") / "runs"
    assert S.gates_dir(cfg, "X") == S.campaign_dir(cfg, "X") / "gates"


# ---- SPEC-0176: item_lock is reentrant within the process, exclusive across processes ------------

def test_item_lock_reentrant_and_append_inside(tmp_path):
    import subprocess
    import sys
    import threading
    import time

    cfg = _cfg(tmp_path)
    cid = "SPEC-0001-20261025-lock"
    S.create_campaign(cfg, {"id": cid, "spec_id": "SPEC-0001", "state": "QUEUED"})
    with S.item_lock(cfg, cid):
        with S.item_lock(cfg, cid):                     # nested: must not deadlock
            _ev(cfg, cid, "note", {"text": "inside"})
        _ev(cfg, cid, "note", {"text": "still inside"})
    assert [e["text"] for e in S.read_ledger(cfg, cid, types=["note"])] == ["inside", "still inside"]

    # another thread waits for the lock (RLock is per thread)
    order = []
    with S.item_lock(cfg, cid):
        t = threading.Thread(target=lambda: (_ev(cfg, cid, "note", {"text": "thread"}),
                                             order.append("thread")))
        t.start()
        time.sleep(0.2)
        order.append("main")
    join(t)
    assert order == ["main", "thread"]

    # another process blocks on the flock while we hold it
    code = (
        "import sys, time, fcntl, os\n"
        f"fd = os.open({str(S.campaign_dir(cfg, cid) / '.lock')!r}, os.O_RDWR | os.O_CREAT)\n"
        "try:\n    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\nexcept BlockingIOError:\n"
        "    print('blocked')\nelse:\n    print('acquired')\n"
    )
    with S.item_lock(cfg, cid):
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout
    assert out.strip() == "blocked"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout
    assert out.strip() == "acquired"
