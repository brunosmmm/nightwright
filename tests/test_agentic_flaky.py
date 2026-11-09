"""SPEC-0293: the cross-campaign flaky-test record (`<agentic_dir>/flaky-tests.json`)."""
import datetime as dt
import json
import multiprocessing
from pathlib import Path

import pytest
from waits import join

from nightwright.agentic import flaky as FL

pytestmark = pytest.mark.fast

T0 = dt.datetime(2026, 10, 1, 12, 0).astimezone()


@pytest.fixture
def cfg(tmp_path):
    return {"data_dir": str(tmp_path / "data")}


def _doc(cfg):
    return json.loads(FL.path(cfg).read_text())


def test_record_creates_and_increments(cfg):
    FL.record(cfg, "P", ["t::a"], campaign="C1", source="rerun", now=T0)
    FL.record(cfg, "P", ["t::a", "t::a"], campaign="C2", source="triage_retry",
              now=T0 + dt.timedelta(days=1))
    doc = _doc(cfg)
    assert doc["schema"] == "nw.flaky.v1"
    e = doc["projects"]["P"]["t::a"]
    assert e["count"] == 2 and e["campaigns"] == ["C1", "C2"]
    assert e["sources"] == {"rerun": 1, "triage_retry": 1}
    assert e["first_seen"] < e["last_seen"]
    assert (FL.path(cfg).parent / "flaky.lock").exists()


def test_campaign_list_capped_at_10(cfg):
    for i in range(14):
        FL.record(cfg, "P", ["t::a"], campaign=f"C{i}", source="rerun", now=T0)
    e = _doc(cfg)["projects"]["P"]["t::a"]
    assert e["count"] == 14 and e["campaigns"] == [f"C{i}" for i in range(4, 14)]


def test_expiry_drops_old_entries(cfg):
    FL.record(cfg, "P", ["t::old"], campaign="C1", source="rerun", now=T0)
    FL.record(cfg, "P", ["t::new"], campaign="C2", source="rerun", now=T0 + dt.timedelta(days=91))
    assert list(_doc(cfg)["projects"]["P"]) == ["t::new"]


def test_project_cap_500_keeps_recent(cfg):
    for lo in range(0, 520, 20):
        FL.record(cfg, "P", [f"t::{i:03d}" for i in range(lo, lo + 20)], campaign="C",
                  source="rerun", now=T0 + dt.timedelta(minutes=lo))
    got = _doc(cfg)["projects"]["P"]
    assert len(got) == 500 and "t::519" in got and "t::000" not in got and "t::019" not in got


def test_corrupt_file_moved_aside(cfg):
    FL.path(cfg).parent.mkdir(parents=True)
    FL.path(cfg).write_text("{not json")
    FL.record(cfg, "P", ["t::a"], campaign="C1", source="rerun", now=T0)
    assert _doc(cfg)["projects"]["P"]["t::a"]["count"] == 1
    bad = list(FL.path(cfg).parent.glob("flaky-tests.json.bad-*"))
    assert len(bad) == 1 and bad[0].read_text() == "{not json"
    FL.path(cfg).write_text(json.dumps({"schema": "other", "projects": {}}))
    FL.record(cfg, "P", ["t::a"], campaign="C1", source="rerun", now=T0)
    assert _doc(cfg)["projects"]["P"]["t::a"]["count"] == 1


def test_malformed_entry_fields_moved_aside_not_crashed(cfg):
    FL.path(cfg).parent.mkdir(parents=True)
    bad = {"schema": FL.SCHEMA, "projects": {"P": {"t::a": {
        "count": "3", "first_seen": T0.isoformat(), "last_seen": T0.isoformat(),
        "sources": [], "campaigns": "C1"}}}}
    FL.path(cfg).write_text(json.dumps(bad))
    assert FL.lookup(cfg, "P", ["t::a"]) == {} and FL.list_entries(cfg) == []
    FL.record(cfg, "P", ["t::a"], campaign="C1", source="rerun", now=T0)
    assert _doc(cfg)["projects"]["P"]["t::a"]["count"] == 1
    assert len(list(FL.path(cfg).parent.glob("flaky-tests.json.bad-*"))) == 1


def test_unreadable_file_is_not_moved_aside(cfg, monkeypatch):
    FL.record(cfg, "P", ["t::a"], campaign="C1", source="rerun", now=T0)
    real = Path.read_text

    def boom(self, *a, **kw):
        if self.name == "flaky-tests.json":
            raise PermissionError("denied")
        return real(self, *a, **kw)
    with monkeypatch.context() as m:
        m.setattr(Path, "read_text", boom)
        with pytest.raises(OSError):
            FL.record(cfg, "P", ["t::a"], campaign="C1", source="rerun", now=T0)
    assert not list(FL.path(cfg).parent.glob("flaky-tests.json.bad-*"))
    assert _doc(cfg)["projects"]["P"]["t::a"]["count"] == 1


def _hammer(data_dir):
    for _ in range(50):
        FL.record({"data_dir": data_dir}, "P", ["t::a"], campaign="C", source="rerun")


def test_concurrent_records_do_not_lose_updates(cfg):
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_hammer, args=(cfg["data_dir"],)) for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        join(p)
        assert p.exitcode == 0
    assert _doc(cfg)["projects"]["P"]["t::a"]["count"] == 100


def test_lookup_and_list_sorted(cfg):
    FL.record(cfg, "P", ["t::a", "t::b"], campaign="C1", source="rerun", now=T0)
    FL.record(cfg, "P", ["t::b"], campaign="C2", source="rerun", now=T0 + dt.timedelta(days=1))
    FL.record(cfg, "Q", ["t::c"], campaign="C3", source="rerun", now=T0 + dt.timedelta(days=2))
    assert set(FL.lookup(cfg, "P", ["t::a", "t::zz"])) == {"t::a"}
    assert [r["id"] for r in FL.list_entries(cfg)] == ["t::b", "t::c", "t::a"]
    assert [r["id"] for r in FL.list_entries(cfg, "P")] == ["t::b", "t::a"]
    assert FL.lookup({"data_dir": cfg["data_dir"] + "x"}, "P", ["t::a"]) == {}
    with pytest.raises(ValueError):
        FL.record(cfg, "P", ["t::a"], campaign="C", source="bogus")
