"""SPEC-0401: `nw.dashboard.v1` — `snapshot.build` on the seeded fixture, a failing section,
`rev` stability, the stat-only watermark (including a ledger-only change), `recent_events`
over the tail of a large `events.jsonl`, and `settings`."""
import datetime as dt
import json
import os

import pytest

from fixtures.dashboard_data import ACTIVE, DIRTY, DECISION, seed
from nightwright.agentic import escalations as ESC
from nightwright.agentic import events as E
from nightwright.agentic import lease as L
from nightwright.agentic import queue as Q
from nightwright.agentic import schemas as SC
from nightwright.agentic import store as S
from nightwright.agentic.dashboard import settings
from nightwright.agentic.dashboard import snapshot as SN


@pytest.fixture
def fx(tmp_path):
    return seed(tmp_path)


def check_schema(doc):
    assert set(SN.TOP_KEYS) <= set(doc), set(SN.TOP_KEYS) - set(doc)
    assert doc["schema"] == "nw.dashboard.v1"
    assert set(doc["health"]) == {"runner", "conductor", "stewards", "herdr_watch"}
    assert set(doc["inbox"]) == {"escalations", "parks"}
    assert set(doc["backlog"]) in ({"count", "top"}, {"error"})
    assert isinstance(doc["rev"], str) and len(doc["rev"]) == 16


def ledger_run_end(cfg, cid, cost):
    return S.append_event(cfg, cid, "run.end", SC.with_defaults("run.end", {
        "persona": "implementer", "run_id": "009-implementer", "exit_code": 0,
        "is_error": False, "timed_out": False, "interrupted": False, "cost_usd": cost}))


# ---- build -------------------------------------------------------------------------------

def test_build_has_every_section_and_matches_the_payload(fx):
    cfg = fx["cfg"]
    doc = SN.build(cfg)
    check_schema(doc)
    dp = Q.dashboard_payload(cfg)
    assert doc["totals"]["spend_today_usd"] == dp["totals"]["spend_today_usd"] > 0
    assert [e["id"] for e in doc["inbox"]["escalations"]] == ["ESC-0001"]
    esc = doc["inbox"]["escalations"][0]
    assert esc["question"] == "ship it?" and esc["campaign"] == DECISION and esc["by"] == "conductor"
    assert [p["campaign"] for p in doc["inbox"]["parks"]] == [DIRTY]
    assert {r["campaign"] for r in doc["needs_you"]} == {DECISION, DIRTY}
    assert [r["campaign"] for r in doc["active"]] == [ACTIVE]
    assert doc["health"]["runner"]["lease"]["held"] is False
    assert doc["health"]["conductor"] == {"pid": None, "lag": None, "cursor_age_s": None}
    dt.datetime.fromisoformat(doc["generated_at"])
    assert doc["generated_at"].endswith("+00:00")


def test_one_failing_builder_is_one_error_section(fx, monkeypatch):
    cfg = fx["cfg"]
    good = SN.build(cfg)

    def boom(*a, **k):
        raise OSError("disk on fire")
    monkeypatch.setattr(ESC, "list_all", boom)
    doc = SN.build(cfg)
    check_schema(doc)
    assert doc["inbox"]["escalations"] == {"error": "OSError: disk on fire"}
    assert doc["inbox"]["parks"] == good["inbox"]["parks"]
    for k in ("totals", "slots", "active", "needs_you", "delivered", "recent_events", "backlog"):
        assert SN._strip(doc[k]) == SN._strip(good[k]), k


def test_a_failing_payload_marks_its_sections_only(fx, monkeypatch):
    def boom(*a, **k):
        raise ValueError("bad")
    monkeypatch.setattr(Q, "dashboard_payload", boom)
    doc = SN.build(fx["cfg"])
    check_schema(doc)
    assert doc["active"] == doc["totals"] == doc["inbox"]["parks"] == {"error": "ValueError: bad"}
    assert doc["inbox"]["escalations"][0]["id"] == "ESC-0001" and doc["recent_events"]


# ---- rev ---------------------------------------------------------------------------------

def _payload(age):
    return {"lease": {"held": True, "heartbeat_age_s": age}, "runner_code": None,
            "slots": {"total": 2}, "gate_pools": None, "totals": {"spend_today_usd": 1.0},
            "active": [{"campaign": "C", "state_age_s": age, "age_s": age,
                        "activity": {"kind": "run", "elapsed_s": age},
                        "nested": [{"park_age_s": age, "x": 1}]}],
            "needs_you": [], "conductor_handling": [], "waiting": [], "backlog": [],
            "delivered": [{"campaign": "D", "finished_age_s": age}]}


def test_rev_ignores_ages_and_generated_at(fx, monkeypatch):
    cfg = fx["cfg"]
    monkeypatch.setattr(Q, "dashboard_payload", lambda cfg, now=None: _payload(3))
    a = SN.build(cfg, now=fx["now"])
    monkeypatch.setattr(Q, "dashboard_payload", lambda cfg, now=None: _payload(900))
    b = SN.build(cfg, now=fx["now"] + dt.timedelta(minutes=15))
    assert a["generated_at"] != b["generated_at"]
    assert a["rev"] == b["rev"] == SN.compute_rev(b)


def test_rev_changes_with_cost(fx):
    cfg = fx["cfg"]
    r0 = SN.build(cfg)["rev"]
    assert SN.build(cfg)["rev"] == r0
    st = S.read_state(cfg, ACTIVE)
    st["cost"]["total_usd"] = 3.5
    S.write_state(cfg, ACTIVE, st)
    r1 = SN.build(cfg)["rev"]
    assert r1 != r0
    ledger_run_end(cfg, ACTIVE, 0.75)                 # a ledger-only cost record
    assert SN.build(cfg)["rev"] != r1


# ---- watermark ---------------------------------------------------------------------------

def _bump(path):
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def test_watermark_is_stable_without_change(fx):
    cfg = fx["cfg"]
    assert SN.watermark(cfg) == SN.watermark(cfg)
    names = {(c, f) for c, f, *_ in SN.watermark(cfg)[3]}
    assert (ACTIVE, "ledger.jsonl") in names and (ACTIVE, "state.json") in names


def test_ledger_append_without_events_moves_watermark(fx):
    cfg = fx["cfg"]
    ev = E.events_path(cfg).stat()
    w0 = SN.watermark(cfg)
    ledger_run_end(cfg, ACTIVE, 0.5)
    w1 = SN.watermark(cfg)
    assert E.events_path(cfg).stat().st_mtime_ns == ev.st_mtime_ns     # projection untouched
    assert w1 != w0 and w1[0] == w0[0] and w1[3] != w0[3]


def test_state_replace_new_campaign_escalation_and_lease_move_watermark(fx):
    cfg = fx["cfg"]
    w = SN.watermark(cfg)
    p = S.campaign_dir(cfg, DIRTY) / "state.json"
    tmp = p.with_name("state.json.tmp")
    tmp.write_text(p.read_text().replace("CLOSING", "CLOSING "))
    os.replace(tmp, p)
    assert SN.watermark(cfg) != w
    w = SN.watermark(cfg)
    (S.campaigns_dir(cfg) / "SPEC-0999-20261103-eeee").mkdir()
    assert SN.watermark(cfg) != w
    w = SN.watermark(cfg)
    ESC._create(cfg, {"by": "conductor", "campaign": None, "question": "q"})
    assert SN.watermark(cfg) != w
    w = SN.watermark(cfg)
    _bump(L.lease_path(cfg))
    assert SN.watermark(cfg) != w
    assert SN.watermark(cfg) == SN.watermark(cfg)


def test_watermark_on_an_empty_data_dir(tmp_path):
    cfg = {"data_dir": str(tmp_path / "nothing")}
    assert SN.watermark(cfg) == (None, (), None, ())
    assert not (tmp_path / "nothing").exists()


# ---- recent events -----------------------------------------------------------------------

class _Spy:
    def __init__(self, f, log):
        self.f, self.log = f, log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.f.close()

    def seek(self, off, whence=0):
        self.log.append(("seek", off, whence))
        return self.f.seek(off, whence)

    def read(self, *a):
        self.log.append(("read", self.f.tell()))
        return self.f.read(*a)


def test_recent_events_reads_only_the_tail(fx, monkeypatch):
    cfg = fx["cfg"]
    path = E.events_path(cfg)
    with open(path, "a") as f:                            # a garbage line, then one more event
        f.write("{not json\n")
        f.write(json.dumps({"id": 10_000, "type": "resumed", "campaign": DIRTY,
                            "spec_id": "SPEC-0903", "data": {"to": "CLOSING"}}) + "\n")
    size = path.stat().st_size
    assert size > SN.RECENT_WINDOW
    log = []
    monkeypatch.setattr(SN, "open", lambda p, mode="r", *a, **k: _Spy(open(p, mode, *a, **k), log),
                        raising=False)
    got = SN.recent_events(cfg)
    floor = size - SN.RECENT_WINDOW
    assert all(off >= floor for kind, off, *rest in log if kind == "seek" and rest == [0])
    assert all(pos >= floor for kind, pos in (x[:2] for x in log) if kind == "read")
    assert any(x[0] == "read" for x in log)
    assert 0 < len(got) <= 20
    assert all(e["type"] in SN.RECENT_TYPES for e in got)
    assert [e["id"] for e in got] == sorted(e["id"] for e in got)
    assert "old0000" not in json.dumps(got) and got[0]["id"] != 1     # the early merge is outside
    assert [e["type"] for e in got] == ["parked", "escalation", "cost_warning", "merged",
                                        "run_failed", "parked", "resumed"]
    assert got[0]["summary"] == "parked SPEC-0903 main_dirty"
    assert set(got[0]) == {"id", "ts", "type", "campaign", "spec_id", "project", "by", "summary"}


def test_recent_events_caps_at_n(fx):
    cfg = fx["cfg"]
    with open(E.events_path(cfg), "a") as f:
        for i in range(30):
            f.write(json.dumps({"id": 20_000 + i, "type": "merged", "campaign": "X",
                                "label": "merge:feat", "data": {}}) + "\n")
    got = SN.recent_events(cfg)
    assert len(got) == 20 and got[-1]["id"] == 20_029 and got[0]["summary"] == "merged merge:feat"


def test_recent_types_are_event_types():
    assert set(SN.RECENT_TYPES) <= set(E.EVENT_TYPES)


# ---- settings ----------------------------------------------------------------------------

def test_settings_defaults_fallback_and_later_keys():
    assert settings({}) == {"bind": "tailscale:8791", "allowed_logins": [], "refresh_s": 5,
                            "max_stale_s": 30}
    got = settings({"agentic": {"dashboard": {
        "refresh_s": "soon", "max_stale_s": 12, "allowed_logins": ["a@b"], "bind": "",
        "max_clients": 4, "tail_poll_s": 2, "push": {"x": 1}}}})
    assert got == {"bind": "tailscale:8791", "allowed_logins": ["a@b"], "refresh_s": 5,
                   "max_stale_s": 12}
    assert settings({"agentic": {"dashboard": {"refresh_s": -1, "max_stale_s": True}}})[
        "refresh_s"] == 5
    assert settings({"agentic": {"dashboard": "junk"}})["max_stale_s"] == 30
