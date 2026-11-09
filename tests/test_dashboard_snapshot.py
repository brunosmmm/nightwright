"""SPEC-0401: `nw.dashboard.v1` — `snapshot.build` on the seeded fixture, a failing section,
`rev` stability, the stat-only watermark (including a ledger-only change), `recent_events`
over the tail of a large `events.jsonl`, and `settings`."""
import datetime as dt
import json
import os
import subprocess
import sys

import pytest

from fixtures.dashboard_data import ACTIVE, DIRTY, DECISION, OFF_BASE, seed
from nightwright.agentic import escalations as ESC
from nightwright.agentic import events as E
from nightwright.agentic import gate_slots as GS
from nightwright.agentic import herdr_autowatch as AW
from nightwright.agentic import lease as L
from nightwright.agentic import mailbox as M
from nightwright.agentic import queue as Q
from nightwright.agentic import schemas as SC
from nightwright.agentic import store as S
from nightwright.agentic.dashboard import settings
from nightwright.agentic.dashboard import snapshot as SN

pytestmark = pytest.mark.fast


@pytest.fixture
def fx(tmp_path):
    return seed(tmp_path)


def check_schema(doc):
    assert set(SN.TOP_KEYS) <= set(doc), set(SN.TOP_KEYS) - set(doc)
    assert doc["schema"] == "nw.dashboard.v1"
    assert set(doc["health"]) == {"runner", "conductor", "stewards", "herdr_watch"}
    assert set(doc["inbox"]) == {"escalations", "parks", "auto_applied"}
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
    # exactly the open records: the answered ESC-0002 and withdrawn ESC-0003 are left out
    assert {r["id"]: r["status"] for r in ESC.list_all(cfg)} == {
        "ESC-0001": "open", "ESC-0002": "answered", "ESC-0003": "withdrawn"}
    assert [e["id"] for e in doc["inbox"]["escalations"]] == ["ESC-0001"]
    esc = doc["inbox"]["escalations"][0]
    assert esc["question"] == "ship it?" and esc["campaign"] == DECISION and esc["by"] == "conductor"
    # SPEC-0403: the full entry; `question` keeps the first line
    assert esc["question_text"] == "ship it?\nmore context"
    assert {"options", "recommendation", "facts", "spec_title", "park", "answerable", "pending",
            "why_not", "forms", "findings", "unmet_acs", "effects"} <= set(esc)
    assert esc["answerable"] is True and esc["forms"] == ["choose", "text", "override"]
    assert esc["unmet_acs"] == ["AC 4 output is sorted"] and esc["findings"]
    # exactly the main_dirty / main_not_on_base parks: the human_decision park is left out
    assert {r["campaign"] for r in doc["needs_you"]} == {DECISION, DIRTY, OFF_BASE}
    assert sorted((p["campaign"], p["park"]["reason"]) for p in doc["inbox"]["parks"]) == [
        (DIRTY, "main_dirty"), (OFF_BASE, "main_not_on_base")]
    assert [r["campaign"] for r in doc["active"]] == [ACTIVE]
    assert doc["health"]["runner"]["lease"]["held"] is False
    assert doc["health"]["conductor"] == {"pid": None, "lag": None, "cursor_age_s": None}
    dt.datetime.fromisoformat(doc["generated_at"])
    assert doc["generated_at"].endswith("+00:00")


def test_one_failing_builder_is_one_error_section(fx, monkeypatch):
    cfg = fx["cfg"]
    good = SN.build(cfg)

    real = ESC.list_all

    def boom(cfg_, status=None):
        # SPEC-0403: the parks read every record (to leave out a campaign with an open one), so
        # only the open-records read of the escalations section fails here
        if status == "open":
            raise OSError("disk on fire")
        return real(cfg_, status)
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


def test_rev_changes_when_an_escalations_options_change(fx):
    cfg = fx["cfg"]
    first = SN.build(cfg)
    ESC._mutate(cfg, "ESC-0001", lambda r: r.update(options=[{"n": 1, "label": "yes"}]))
    again = SN.build(cfg)
    assert again["inbox"]["escalations"][0]["options"] == [{"n": 1, "label": "yes"}]
    assert again["rev"] != first["rev"]


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


def _lease_payload(**lease):
    pl = _payload(3)
    pl["lease"] = {"held": True, "pid": 42, "host": "h", "stale": False, "draining": None,
                   "heartbeat_at": "2026-11-03T10:00:00-05:00", "heartbeat_age_s": 1.0,
                   "tick_at": "2026-11-03T10:00:00-05:00", "ticks": 7, "last_tick_s": 0.4,
                   "slot_freed_at": None, "busy_ended_at": None, "slots_full_at": None,
                   "progress_at": None, "busy": False, "in_flight": 1, **lease}
    return pl


def test_rev_ignores_runner_heartbeat_and_tick_stamps(fx, monkeypatch):
    """A live runner rewrites `heartbeat_at` / `tick_at` / `ticks` every tick: same rev."""
    cfg = fx["cfg"]

    def rev(**lease):
        monkeypatch.setattr(Q, "dashboard_payload", lambda cfg, now=None: _lease_payload(**lease))
        return SN.build(cfg, now=fx["now"])["rev"]
    r0 = rev()
    assert rev(heartbeat_at="2026-11-03T10:00:09-05:00", tick_at="2026-11-03T10:00:08-05:00",
               ticks=9, last_tick_s=0.7, slot_freed_at="2026-11-03T10:00:05-05:00",
               busy_ended_at="x", slots_full_at="y", progress_at="z") == r0
    assert rev(held=False) != r0 and rev(pid=43) != r0 and rev(stale=True) != r0
    assert rev(draining={"since": "t"}) != r0 and rev(in_flight=2) != r0


def test_rev_ignores_estimated_gate_percent_only():
    def doc(pct, est):
        prog = {"percent": pct, "estimated": est}
        return {"schema": SN.SCHEMA, "active": [{"campaign": "C", "gate_hold": {
            "progress": prog}}], "gate_pools": {"campaigns": [{"campaign": "C",
                                                                "progress": dict(prog)}]}}
    assert SN.compute_rev(doc(10, True)) == SN.compute_rev(doc(40, True))     # elapsed-time guess
    assert SN.compute_rev(doc(10, False)) != SN.compute_rev(doc(40, False))   # real progress
    assert SN.compute_rev(doc(10, True)) != SN.compute_rev(doc(10, False))


# ---- health ------------------------------------------------------------------------------

def test_health_maps_conductor_and_held_stewards(fx, monkeypatch):
    cfg = fx["cfg"]
    last = SN._last_id(SN._tail_events(cfg))
    E.cursor_set(cfg, "conductor", last - 3)
    E.cursor_set(cfg, "mailbox:steward:Meta-Tools", 0)
    E.cursor_set(cfg, "mailbox:human", 0)                          # not held: left out
    M.send(cfg, to="steward:Meta-Tools", frm="conductor", text="hi")

    def no_full_read(*a, **k):
        raise AssertionError("read_since over the log")
    monkeypatch.setattr(E, "read_since", no_full_read)
    with E.reader_lock(cfg, "conductor"), E.reader_lock(cfg, "mailbox:steward:Meta-Tools"):
        h = SN.build(cfg)["health"]
    assert h["conductor"]["pid"] == os.getpid() and h["conductor"]["lag"] == 3
    assert isinstance(h["conductor"]["cursor_age_s"], int)
    assert h["stewards"] == [{"address": "steward:Meta-Tools", "pid": os.getpid(), "lag": 1}]


def test_health_rows_from_patched_followers(fx, monkeypatch):
    cfg = fx["cfg"]
    rows = [{"reader": "conductor", "kind": "events", "pid": 11, "held": True, "lag": 4,
             "cursor_age_s": 9},
            {"reader": "mailbox:steward-x", "kind": "mailbox", "pid": 12, "held": True, "lag": 2},
            {"reader": "mailbox:steward-y", "kind": "mailbox", "pid": None, "held": False,
             "lag": 5}]
    monkeypatch.setattr(E, "followers", lambda cfg, **k: rows)
    monkeypatch.setattr(E, "reader_holder", lambda cfg, r: 99)
    h = SN.build(cfg)["health"]
    assert h["conductor"] == {"pid": 11, "lag": 4, "cursor_age_s": 9}
    assert h["stewards"] == [{"address": "steward-x", "pid": 12, "lag": 2}]
    monkeypatch.setattr(E, "followers", lambda cfg, **k: [])
    assert SN.build(cfg)["health"]["conductor"] == {"pid": 99, "lag": None, "cursor_age_s": None}


def test_failing_followers_stewards_and_herdr_are_error_sections(fx, monkeypatch):
    cfg = fx["cfg"]
    monkeypatch.setattr(E, "followers", lambda cfg, **k: [{"kind": "mailbox", "held": True}])
    doc = SN.build(cfg)
    assert doc["health"]["stewards"] == {"error": "KeyError: 'reader'"}
    check_schema(doc)

    def boom(*a, **k):
        raise RuntimeError("no cursors")
    monkeypatch.setattr(E, "followers", boom)

    def hboom(cfg):
        raise RuntimeError("no herdr")
    monkeypatch.setattr(SN, "_herdr_watch", hboom)
    doc = SN.build(cfg)
    check_schema(doc)
    assert doc["health"]["stewards"] == {"error": "RuntimeError: no cursors"}
    assert doc["health"]["conductor"]["lag"] is None
    assert doc["health"]["herdr_watch"] == {"error": "RuntimeError: no herdr"}
    assert doc["inbox"]["escalations"][0]["id"] == "ESC-0001" and doc["active"]


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
    assert SN.watermark(cfg) == (None, (), None, (), ((), (), (), None, ()))
    assert not (tmp_path / "nothing").exists()


def test_follower_gate_mailbox_and_herdr_files_move_watermark(fx):
    cfg = fx["cfg"]
    w = SN.watermark(cfg)
    E.cursor_set(cfg, "conductor", 3)                           # a follower advanced
    assert SN.watermark(cfg) != w
    w = SN.watermark(cfg)
    GS.slots_dir(cfg).mkdir(parents=True)
    (GS.slots_dir(cfg) / "slot-0.lock").write_text(json.dumps({"campaign": ACTIVE, "pid": 1}))
    assert SN.watermark(cfg) != w                               # a gate slot taken
    w = SN.watermark(cfg)
    M.mailbox_dir(cfg).mkdir(parents=True)
    (M.mailbox_dir(cfg) / "steward_P.jsonl").write_text("{}\n")
    assert SN.watermark(cfg) != w                               # steward inbox moved
    w = SN.watermark(cfg)
    AW.state_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    AW.state_path(cfg).write_text(json.dumps({"pid": os.getpid()}))
    assert SN.watermark(cfg) != w                               # herdr watch state
    assert SN.watermark(cfg) == SN.watermark(cfg)


def test_a_dead_follower_moves_watermark_without_any_write(fx, monkeypatch):
    """A follower that dies releases its flock and writes nothing: its pid's liveness moves.
    Liveness is a controlled `_pid_alive`, so no real pid can be reused under the test."""
    cfg = fx["cfg"]
    pid, live = 4_000_001, {4_000_001}
    monkeypatch.setattr(GS, "_pid_alive", lambda p: p in live)
    E.cursors_dir(cfg).mkdir(parents=True, exist_ok=True)
    (E.cursors_dir(cfg) / "conductor.pid").write_text(str(pid))
    w = SN.watermark(cfg)
    assert ("cursors/conductor.pid", str(pid), True) in w[4][4]
    files = {p: p.stat().st_mtime_ns for p in fx["data"].rglob("*") if p.is_file()}
    live.clear()                                    # the follower dies, writing nothing
    w1 = SN.watermark(cfg)
    assert {p: p.stat().st_mtime_ns for p in fx["data"].rglob("*") if p.is_file()} == files
    assert w1 != w and ("cursors/conductor.pid", str(pid), False) in w1[4][4]


def test_pid_alive_is_the_real_kill_probe():
    """The real `kill(pid, 0)` behind the liveness signature, without a reaped-pid race."""
    assert GS._pid_alive(os.getpid())
    assert not any(GS._pid_alive(p) for p in (0, -1, True, None, "12"))


def test_unreadable_pid_files_do_not_fail_the_watermark(fx):
    """A torn or binary `.pid` / `waiting-*.json` (invalid UTF-8) counts as no pid."""
    cfg = fx["cfg"]
    E.cursors_dir(cfg).mkdir(parents=True, exist_ok=True)
    (E.cursors_dir(cfg) / "conductor.pid").write_bytes(b"\xff\xfe\x00")
    GS.slots_dir(cfg).mkdir(parents=True, exist_ok=True)
    (GS.slots_dir(cfg) / "waiting-x.json").write_bytes(b"{\"pid\": \xc3\x28}")
    L.lease_path(cfg).write_bytes(b"\x80garbage")
    w = SN.watermark(cfg)
    names = {r[0] for r in w[4][4]}
    assert not names & {"cursors/conductor.pid", "gate-slots/waiting-x.json"}
    assert SN.watermark(cfg) == w


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


def test_recent_events_without_a_log(tmp_path):
    assert SN.recent_events({"data_dir": str(tmp_path / "none")}) == []


def _write_events(cfg, evs, prefix=b""):
    path = E.events_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(prefix + b"".join(json.dumps(e).encode() + b"\n" for e in evs))
    return path


def _ev(i, t="merged"):
    return {"schema": E.SCHEMA, "id": i, "ts": "2026-11-03T00:00:00+00:00", "type": t,
            "campaign": "C", "spec_id": None, "project": "P", "by": "queue", "data": {}}


def test_recent_events_keeps_the_first_line_of_a_small_log(tmp_path):
    cfg = {"data_dir": str(tmp_path / "d")}
    _write_events(cfg, [_ev(1), _ev(2, "note"), _ev(3, "parked")])
    assert [e["id"] for e in SN.recent_events(cfg)] == [1, 3]


def test_recent_events_keeps_a_whole_line_at_the_window_start(tmp_path):
    """A line that starts exactly at `size - 256 KiB` is whole and kept; a cut one is not."""
    cfg = {"data_dir": str(tmp_path / "d")}
    first, last = json.dumps(_ev(2)).encode() + b"\n", json.dumps(_ev(3)).encode() + b"\n"
    pad = b"x" * (SN.RECENT_WINDOW - len(first) - len(last) - 1) + b"\n"
    head = json.dumps(_ev(1)).encode() + b"\n"
    path = E.events_path(cfg)
    path.parent.mkdir(parents=True)
    path.write_bytes(head + first + pad + last)
    assert path.stat().st_size - SN.RECENT_WINDOW == len(head)        # window starts at ev 2
    assert [e["id"] for e in SN.recent_events(cfg)] == [2, 3]
    path.write_bytes(b"y" + head + first + pad + last)                 # now ev 1 is cut
    assert [e["id"] for e in SN.recent_events(cfg)] == [2, 3]
    path.write_bytes(head[:-1] + b"z\n" + first[5:] + pad + last)     # ev 2 cut mid-line
    assert [e["id"] for e in SN.recent_events(cfg)] == [3]


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
    inf = settings({"agentic": {"dashboard": {"refresh_s": float("inf"),
                                              "max_stale_s": float("nan")}}})
    assert (inf["refresh_s"], inf["max_stale_s"]) == (5, 30)


def test_build_without_a_data_dir_raises(tmp_path):
    with pytest.raises(ValueError, match="no data_dir"):
        SN.build({"agentic": {}})
    with pytest.raises(FileNotFoundError, match="does not exist"):
        SN.build({"data_dir": str(tmp_path / "missing"), "agentic": {}})
    assert not (tmp_path / "missing").exists()
