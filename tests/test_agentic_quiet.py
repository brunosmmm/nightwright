"""SPEC-0300: the quiet conductor follower: classification, the pending digest, counting."""
import datetime as dt
import io
import json
import re
import types
from pathlib import Path

import pytest
from click.testing import CliRunner

from nightwright.agentic import codewatch as CW
from nightwright.agentic import events as E
from nightwright.agentic import quiet as QT
from nightwright.agentic import store
from nightwright.cli import cli

pytestmark = pytest.mark.fast

HOLD = ["admitted", "resumed", "waiting", "abandoned", "cost_warning", "escalation",
        "escalation_withdrawn", "rebase_resolved", "run_failed", "merged", "handed_off"]
WAKE = ["parked", "park_undecided", "runner_dead", "lease_stale", "run_overdue", "message",
        "escalation_answered", "ready_not_admitted", "waiting_not_admitted",
        "runner_journal_silent", "temp_pressure", "runner_config_invalid",
        "code_changed", "brand_new_type"]

DATA = {"merged": {"merged_sha": "s"}, "message": {"msg_id": "1", "from": "x", "kind": "info",
                                                   "text": "t"},
        "admitted": {}, "abandoned": {}, "cost_warning": {"total_usd": 1, "cap_usd": 2}}


def _cfg(tmp_path, quiet=None):
    cfg = {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "config"),
           "agentic": {"lease_ttl_s": 120}, "specs_dir": str(tmp_path)}
    if quiet is not None:
        cfg["agentic"]["conductor"] = {"quiet": quiet}
    return cfg


def _ctx(cfg, lease=None, now=None, closing_waiters=None):
    return QT.Ctx(cfg, "conductor", lease=lease if lease is not None else {"held": False},
                  now=now, closing_waiters=closing_waiters)


def _ev(type_, campaign=None, project=None, by=None, **data):
    ev = {"id": 1, "ts": "2026-10-30T10:00:00+00:00", "type": type_, "campaign": campaign,
          "project": project, "data": data}
    if by is not None:
        ev["by"] = by
    return ev


def _ping(*, stuck=(), blocked=(), runner=None, parked=()):
    return {"blocked": [{"campaign": c} for c in blocked], "parked": list(parked),
            "stuck": [{"campaign": c} for c in stuck], "waiting": [], "ready_not_admitted": [],
            "waiting_not_admitted": [], "runner": runner, "counts": {}}


def _follow(cfg, out, *, events=(), clock=None, **kw):
    kw.setdefault("stop", lambda: True)
    return E.follow(cfg, "conductor", poll_s=0, out=out, code_root=None, quiet=True,
                    now=clock, **kw)


def _lines(out):
    return [json.loads(x) for x in out.getvalue().splitlines()]


class Clock:
    def __init__(self):
        self.t = dt.datetime.now().astimezone()

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += dt.timedelta(seconds=s)


# ---- classification -----------------------------------------------------------------------

@pytest.mark.parametrize("type_", HOLD)
def test_classify_always_hold_and_wake_tables_hold(tmp_path, type_):
    cfg = _cfg(tmp_path)
    assert QT.classify(cfg, _ev(type_), _ctx(cfg)) == "hold"


@pytest.mark.parametrize("type_", WAKE)
def test_classify_always_hold_and_wake_tables_wake(tmp_path, type_):
    cfg = _cfg(tmp_path)
    assert QT.classify(cfg, _ev(type_), _ctx(cfg)) == "wake"


def test_classify_backlog(tmp_path):
    cfg = _cfg(tmp_path)
    c = _ctx(cfg)
    assert QT.classify(cfg, _ev("backlog", ready=True, blocked_by=[]), c) == "hold"
    assert QT.classify(cfg, _ev("backlog", ready=False,
                                blocked_by=["waiting on SPEC-0001 (accepted)"]), c) == "hold"
    assert QT.classify(cfg, _ev("backlog", ready=False,
                                blocked_by=["SPEC-0999 not found"]), c) == "wake"
    for st in ("draft", "proposed", "in-progress"):       # may still become accepted
        assert QT.classify(cfg, _ev("backlog", ready=False,
                                    blocked_by=[f"spec status is {st}"]), c) == "hold"
    for st in ("missing", "rejected", "superseded", "done"):   # can never become ready
        assert QT.classify(cfg, _ev("backlog", ready=False,
                                    blocked_by=[f"spec status is {st}"]), c) == "wake"


LIVE = (("P", "SPEC-0001"), ("P", "SPEC-0007"))         # live_specs: (project, spec_id)


def _blocked_verdict(tmp_path, blocked_by, live=LIVE, project="P"):
    cfg = _cfg(tmp_path)
    ev = _ev("backlog_blocked_long", "c-1", project, blocked_by=blocked_by, blocked_for_s=1300)
    ctx = QT.Ctx(cfg, "conductor", lease={"held": False}, live_specs=frozenset(live))
    return QT.classify(cfg, ev, ctx)


def test_classify_backlog_blocked_long(tmp_path):
    assert QT.RULES["backlog_blocked_long"] == "cond"
    for hold in (["waiting on SPEC-0001 (in-progress)"], ["waiting on SPEC-0001 (proposed)"],
                 ["spec status is draft"], ["spec status is proposed"],
                 ["spec status is in-progress"],
                 ["waiting on SPEC-0001 (superseded; waiting on SPEC-0007)"],
                 ["waiting on SPEC-0001 (accepted)", "spec status is draft"]):
        assert _blocked_verdict(tmp_path, hold) == "hold", hold
    # no live campaign for the dependency, a replacement, or one of two dependencies
    assert _blocked_verdict(tmp_path, ["waiting on SPEC-0001 (proposed)"], live=()) == "wake"
    assert _blocked_verdict(tmp_path, ["waiting on SPEC-0002 (proposed)"]) == "wake"
    assert _blocked_verdict(
        tmp_path, ["waiting on SPEC-0001 (superseded; waiting on SPEC-0007, SPEC-0008)"]) == "wake"
    assert _blocked_verdict(tmp_path, ["waiting on SPEC-0001, SPEC-0002 (proposed)"]) == "wake"
    assert _blocked_verdict(tmp_path, ["waiting on nothing"]) == "wake"      # names no id
    # anything that is not an ordinary dependency wait wakes, even with everything live
    for wake in (["SPEC-0999 not found"], ["branch x missing"], ["idea IDEA-1 not found"],
                 ["spec status is rejected"], ["spec status is superseded"],
                 ["spec status is done"], ["spec status is missing"],
                 ["waiting on SPEC-0001 (proposed)", "SPEC-0999 not found"], [], [7]):
        assert _blocked_verdict(tmp_path, wake) == "wake", wake
    cfg = _cfg(tmp_path)
    ctx = QT.Ctx(cfg, "conductor", lease={"held": False}, live_specs=frozenset(LIVE))
    assert QT.classify(cfg, _ev("backlog_blocked_long", "c-1", "P"), ctx) == "wake"
    assert QT.classify(cfg, _ev("backlog_blocked_long", "c-1", "P", blocked_by="x"), ctx) == "wake"


def test_classify_backlog_blocked_long_is_scoped_to_the_project(tmp_path):
    held = ["waiting on SPEC-0001 (proposed)"]
    assert _blocked_verdict(tmp_path, held) == "hold"
    # the same spec id is live only in another project: nothing builds this project's SPEC-0001
    assert _blocked_verdict(tmp_path, held, live=(("Q", "SPEC-0001"),)) == "wake"
    assert _blocked_verdict(tmp_path, held, project="Q") == "wake"
    # the superseded form checks the replacement in the event's project too
    sup = ["waiting on SPEC-0001 (superseded; waiting on SPEC-0007)"]
    assert _blocked_verdict(tmp_path, sup, live=(("Q", "SPEC-0007"),)) == "wake"
    # a missing project never counts as live, even if a campaign lacks one too
    assert _blocked_verdict(tmp_path, held, project=None) == "wake"
    assert _blocked_verdict(tmp_path, held, live=((None, "SPEC-0001"),), project=None) == "wake"


def test_waited_ids():
    assert QT.waited_ids("waiting on SPEC-0001 (proposed)") == ["SPEC-0001"]
    assert QT.waited_ids("waiting on SPEC-0001 (superseded; waiting on SPEC-0007, SPEC-0008)") \
        == ["SPEC-0007", "SPEC-0008"]
    assert QT.waited_ids("waiting on SPEC-0003, IDEA-5") == ["SPEC-0003", "IDEA-5"]
    assert QT.waited_ids("waiting on someone") == []


def test_ctx_live_specs_reads_store(tmp_path):
    cfg = _cfg(tmp_path)
    for n, state in ((1, "QUEUED"), (2, "IMPLEMENTING"), (3, "MERGED")):
        store.create_campaign(cfg, {"id": f"SPEC-000{n}-20261030-aaaa", "spec_id": f"SPEC-000{n}",
                                    "project": "Meta-Tools", "profile": "balanced",
                                    "state": state, "repo": {"base": "main"}})
    ctx = QT.Ctx(cfg, "conductor", lease={"held": False})
    assert ctx._live_specs is None                          # nothing scanned yet
    assert ctx.live_specs == {("Meta-Tools", "SPEC-0001"), ("Meta-Tools", "SPEC-0002"),
                              ("Meta-Tools", "SPEC-0001-20261030-aaaa"),     # SPEC-0423: `after`
                              ("Meta-Tools", "SPEC-0002-20261030-aaaa")}     # campaign refs
    assert QT.Ctx(cfg, "conductor", lease={"held": False},
                  live_specs={("P", "X-1")}).live_specs == {("P", "X-1")}


def test_ctx_live_specs_is_lazy_and_cached(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    store.create_campaign(cfg, {"id": "SPEC-0001-20261030-aaaa", "spec_id": "SPEC-0001",
                                "project": "Meta-Tools", "profile": "balanced",
                                "state": "QUEUED", "repo": {"base": "main"}})
    calls = []
    real = store.list_campaigns
    monkeypatch.setattr(QT.store, "list_campaigns",
                        lambda *a, **kw: calls.append(1) or real(*a, **kw))
    ctx = QT.Ctx(cfg, "conductor", lease={"held": False})
    # events that need no liveness never scan campaigns
    assert QT.classify(cfg, _ev("admitted"), ctx) == "hold"
    assert QT.classify(cfg, _ev("conductor_ping", **_ping(stuck=["s"])), ctx) == "wake"
    assert QT.classify(cfg, _ev("conductor_ping", **_ping()), ctx) == "hold"
    assert QT.classify(cfg, _ev("backlog", blocked_by=["waiting on SPEC-0001 (x)"]), ctx) == "hold"
    ctx.digest.add(_ev("conductor_ping", **_ping()), ctx)
    ctx.digest.note_ping_wake(_ev("conductor_ping", **_ping(stuck=["s"])), ctx)
    assert calls == []
    held = _ev("backlog_blocked_long", "c", "Meta-Tools",
               blocked_by=["waiting on SPEC-0001 (proposed)"])
    assert QT.classify(cfg, held, ctx) == "hold"
    assert ("Meta-Tools", "SPEC-0001") in ctx.live_specs
    assert QT.classify(cfg, held, ctx) == "hold"
    assert calls == [1]                                     # read once, then cached


def test_ping_fingerprint_blocked_dependency_waits():
    live = frozenset({("P", "SPEC-0001")})

    def ping(*items, project="P"):
        return {**_ping(), "blocked": [{"campaign": c, "blocked_by": b, "project": project}
                                       for c, b in items]}
    held = ["waiting on SPEC-0001 (proposed)"]
    assert QT.ping_fingerprint(ping(("a", held)), live) == []
    assert QT.ping_fingerprint(ping(("a", held)), frozenset()) == [["blocked", "a"]]
    assert QT.ping_fingerprint(ping(("a", ["SPEC-9 not found"])), live) == [["blocked", "a"]]
    assert QT.ping_fingerprint(ping(("a", held), ("b", [])), live) == [["blocked", "b"]]
    assert QT.ping_fingerprint(_ping(blocked=["old"]), live) == [["blocked", "old"]]
    # a live SPEC-0001 in another project, or an item without a project, does not hold it
    assert QT.ping_fingerprint(ping(("a", held), project="Q"), live) == [["blocked", "a"]]
    assert QT.ping_fingerprint(ping(("a", held), project=None), live) == [["blocked", "a"]]
    other = {**ping(("a", held)), "stuck": [{"campaign": "s"}]}
    assert QT.ping_fingerprint(other, live) == [["stuck", "s"]]


def test_ping_fingerprint_hold_and_change(tmp_path):
    cfg = _cfg(tmp_path)

    def verdict(data):
        ev = _ev("conductor_ping", **data)
        ctx = QT.Ctx(cfg, "conductor")
        res = QT.classify(cfg, ev, ctx)
        if res == "wake":
            ctx.digest.note_ping_wake(ev, ctx)
            ctx.digest.save()
        return res
    assert verdict(_ping()) == "hold"
    assert verdict(_ping(stuck=["a"])) == "wake"
    assert verdict(_ping(stuck=["a"])) == "hold"
    assert verdict(_ping(stuck=["b"])) == "wake"
    assert verdict(_ping(runner={"pid": 7})) == "wake"
    # a decided or human-owned park is not actionable; an undecided conductor park is
    quiet_park = {"campaign": "p", "owner": "human", "decided": False}
    assert QT.ping_fingerprint(_ping(parked=[quiet_park])) == []
    assert QT.ping_fingerprint(_ping(parked=[{**quiet_park, "owner": "conductor"}])) == \
        [["parked", "p"]]
    # SPEC-0306: a blocked item on a dependency being built is held; once its blocker is
    # not an ordinary dependency wait the next ping wakes (and then holds again)
    store.create_campaign(cfg, {"id": "SPEC-0001-20261030-aaaa", "spec_id": "SPEC-0001",
                                "project": "Meta-Tools", "profile": "balanced",
                                "state": "QUEUED", "repo": {"base": "main"}})

    def blocked(by, cid="b", project="Meta-Tools", **more):
        return {**_ping(**more), "blocked": [{"campaign": cid, "blocked_by": by,
                                               "project": project}]}
    assert verdict(blocked(["waiting on SPEC-0001 (proposed)"])) == "hold"
    assert verdict(blocked(["waiting on SPEC-0002 (proposed)"])) == "wake"   # not live
    assert verdict(blocked(["waiting on SPEC-0002 (proposed)"])) == "hold"
    assert verdict(_ping(blocked=["old"])) == "wake"           # legacy item: no blocked_by
    assert verdict(_ping()) == "hold"                          # all clear
    assert verdict(blocked(["waiting on SPEC-0001 (proposed)"], "c2")) == "hold"
    # the same item turns harmless -> not an ordinary dependency wait: the next ping wakes
    assert verdict(blocked(["SPEC-0999 not found"], "c2")) == "wake"
    assert verdict(blocked(["SPEC-0999 not found"], "c2")) == "hold"
    assert verdict(_ping()) == "hold"
    # the stored fingerprint leaves the live-dependency item out, as classify does: a ping that
    # mixes it with an actionable item wakes once, then holds (fails if ctx is dropped)
    live = ["waiting on SPEC-0001 (proposed)"]
    assert verdict(blocked(live, stuck=["s"])) == "wake"
    assert verdict(blocked(live, stuck=["s"])) == "hold"
    assert _stored_fp(cfg) == [["stuck", "s"]]
    # a live SPEC-0001 in another project does not hold this project's item
    assert verdict(blocked(live, "o", project="Other")) == "wake"
    assert verdict(blocked(live, "o", project="Other")) == "hold"
    assert _stored_fp(cfg) == [["blocked", "o"]]


def _stored_fp(cfg):
    return QT.Digest.load(cfg, "conductor").last_ping_fp


def test_follow_holds_blocked_long_on_in_flight_dependency(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    store.create_campaign(cfg, {"id": "SPEC-0001-20261030-aaaa", "spec_id": "SPEC-0001",
                                "project": "Meta-Tools", "profile": "balanced",
                                "state": "IMPLEMENTING", "repo": {"base": "main"}})
    E.cursor_set(cfg, "conductor", 0)
    for cid, by in (("a", "waiting on SPEC-0001 (in-progress)"),
                    ("b", "waiting on SPEC-0002 (proposed)"), ("c", "SPEC-0999 not found")):
        E.append(cfg, "backlog_blocked_long", campaign=cid, project="Meta-Tools", by="watchdog",
                 data={"blocked_by": [by], "blocked_for_s": 1300})
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    assert [e["campaign"] for e in _lines(out) if e["type"] == "backlog_blocked_long"] \
        == ["b", "c"]
    assert E.cursor_get(cfg, "conductor") == E.read_since(cfg, 0)[-1]["id"]
    acts = [(r["campaign"], r["action"]) for r in QT._read_deliveries(cfg, "conductor")
            if r["type"] == "backlog_blocked_long"]
    assert acts == [("a", "hold"), ("b", "wake"), ("c", "wake")]
    assert QT.wakeups_report(cfg)["by_type"]["backlog_blocked_long"] == {"wake": 2, "hold": 1}
    clock.advance(1801)
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    (digest,) = [e for e in _lines(out) if e["type"] == "conductor_digest"]
    assert digest["data"]["held"].get("backlog_blocked_long") == 1
    assert digest["data"]["campaigns"] == {"backlog_blocked_long": ["a"]}
    # the event stays visible without the quiet filter
    assert [e["campaign"] for e in E.read_since(cfg, 0, types=["backlog_blocked_long"])] \
        == ["a", "b", "c"]


def test_follow_ping_with_live_dependency_item(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    store.create_campaign(cfg, {"id": "SPEC-0001-20261030-aaaa", "spec_id": "SPEC-0001",
                                "project": "Meta-Tools", "profile": "balanced",
                                "state": "IMPLEMENTING", "repo": {"base": "main"}})
    E.cursor_set(cfg, "conductor", 0)
    blocked = [{"campaign": "a", "project": "Meta-Tools",
                "blocked_by": ["waiting on SPEC-0001 (proposed)"]},
               {"campaign": "b", "project": "Meta-Tools", "blocked_by": ["SPEC-0999 not found"]},
               {"campaign": "o", "project": "Other",
                "blocked_by": ["waiting on SPEC-0001 (proposed)"]}]
    for _ in range(2):                                      # the identical ping, twice
        E.append(cfg, "conductor_ping", by="watchdog", data={**_ping(), "blocked": blocked})
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    assert [e["type"] for e in _lines(out) if e["type"] == "conductor_ping"] == ["conductor_ping"]
    acts = [r["action"] for r in QT._read_deliveries(cfg, "conductor")
            if r["type"] == "conductor_ping"]
    assert acts == ["wake", "hold"]
    dg = QT.Digest.load(cfg, "conductor")
    # the in-flight item "a" is out of the stored fingerprint and the held digest items
    assert dg.last_ping_fp == [["blocked", "b"], ["blocked", "o"]]
    assert dg.pending["ping_items"] == [["blocked", "b"], ["blocked", "o"]]
    assert dg.pending["pings_held"] == 1
    # a ping that is only the live-dependency item is held and clears the fingerprint
    E.append(cfg, "conductor_ping", by="watchdog", data={**_ping(), "blocked": blocked[:1]})
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    assert [e for e in _lines(out) if e["type"] == "conductor_ping"] == []
    dg = QT.Digest.load(cfg, "conductor")
    assert dg.pending["ping_items"] == [] and dg.last_ping_fp == []


def test_runner_draining_hold_window(tmp_path):
    cfg = _cfg(tmp_path)
    budget = E.settings(cfg)["gate_budget_s"]
    c = _ctx(cfg)

    def verdict(draining_s, waiting_on):
        return QT.classify(cfg, _ev("runner_draining", reason="r", draining_s=draining_s,
                                    waiting_on=waiting_on, ready=1), c)
    w = [{"campaign": "a", "timeout_s": 100}, {"campaign": "b", "timeout_s": 900}]
    assert verdict(900 + 300, w) == "hold"
    assert verdict(900 + 301, w) == "wake"
    assert verdict(budget + 300, []) == "hold"              # empty: gate_budget_s fallback
    assert verdict(budget + 301, []) == "wake"
    assert verdict(budget + 300, [{"campaign": "a", "timeout_s": None}]) == "hold"
    assert verdict(budget + 301, [{"campaign": "a", "timeout_s": None}]) == "wake"


def test_silent_held_while_in_flight(tmp_path):
    cfg = _cfg(tmp_path)
    now = dt.datetime.now().astimezone()
    for type_ in ("campaign_silent", "state_stuck"):
        ev = _ev(type_, campaign="c1", state="GATING", silent_s=1000, stuck_s=1000)
        held = {"held": True, "in_flight": ["c1"]}
        assert QT.classify(cfg, ev, _ctx(cfg, held)) == "hold"
        assert QT.classify(cfg, ev, _ctx(cfg, {"held": True, "in_flight": ["c2"]})) == "wake"
        assert QT.classify(cfg, ev, _ctx(cfg, {"held": False, "in_flight": ["c1"]})) == "wake"
        fresh = {"held": True, "busy": {"kind": "close", "campaign": "c1",
                                        "since": now.isoformat(), "budget_s": 3600}}
        assert QT.classify(cfg, ev, _ctx(cfg, fresh)) == "hold"
        old = {"held": True, "busy": {"kind": "close", "campaign": "c1", "budget_s": 60,
                                      "since": (now - dt.timedelta(seconds=600)).isoformat()}}
        assert QT.classify(cfg, ev, _ctx(cfg, old)) == "wake"
        other = {"held": True, "busy": {"kind": "close", "campaign": "c9",
                                        "since": now.isoformat(), "budget_s": 3600}}
        assert QT.classify(cfg, ev, _ctx(cfg, other)) == "wake"


def test_silent_held_for_train_waiter_behind_live_head(tmp_path):
    cfg = _cfg(tmp_path)
    now = dt.datetime.now().astimezone()
    waiters = {"w": "h"}
    for type_ in ("campaign_silent", "state_stuck"):
        ev = _ev(type_, campaign="w", state="CLOSING", silent_s=1000, stuck_s=1000)

        def verdict(lease):
            return QT.classify(cfg, ev, _ctx(cfg, lease, closing_waiters=waiters))

        assert verdict({"held": True, "in_flight": ["h"]}) == "hold"
        assert verdict({"held": True, "busy": {"kind": "close", "campaign": "h",
                                               "since": now.isoformat(), "budget_s": 3600}}) == "hold"
        assert verdict({"held": True, "in_flight": ["other"]}) == "wake"
        assert verdict({"held": False, "in_flight": ["h"]}) == "wake"
        assert verdict({"held": True, "busy": {
            "kind": "close", "campaign": "h", "budget_s": 60,
            "since": (now - dt.timedelta(seconds=600)).isoformat()}}) == "wake"
        assert verdict({"held": True, "in_flight": ["w"]}) == "hold"       # own campaign


def test_silent_not_held_for_waiter_without_train_entry(tmp_path):
    cfg = _cfg(tmp_path)
    lease = {"held": True, "in_flight": ["h"]}
    ev = _ev("campaign_silent", campaign="w", state="CLOSING", silent_s=1000)
    assert QT.classify(cfg, ev, _ctx(cfg, lease, closing_waiters={})) == "wake"
    assert QT.classify(cfg, ev, _ctx(cfg, lease, closing_waiters={"x": "h"})) == "wake"


def test_closing_waiters_built_from_store(tmp_path):
    cfg = _cfg(tmp_path)
    for cid, state, closing in (
            ("h", "CLOSING", {"attempts": 0}), ("w", "CLOSING", {"queued_behind": "h"}),
            ("m", "MERGED", None), ("stale", "CLOSING", {"queued_behind": "m"}),
            ("gone", "CLOSING", {"queued_behind": "nope"})):
        store.create_campaign(cfg, {"id": cid, "spec_id": "SPEC-0001", "project": "p",
                                    "state": state, "closing": closing})
    assert QT.Ctx(cfg, "conductor", lease={"held": False}).closing_waiters == {"w": "h"}


def _states(**kw):
    """`Ctx.states` for tests: id -> state, or (state, gate_exclusive)."""
    out = {}
    for cid, v in kw.items():
        state, excl = v if isinstance(v, tuple) else (v, False)
        out[cid] = {"state": state, "gate_exclusive": excl, "updated_at": None,
                    "created_at": None}
    return out


def _dead():
    import subprocess
    import sys
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _slot_file(cfg, name, cid, pid, since=None, pool=None):
    from nightwright.agentic import gate_slots as GS
    d = GS.slots_dir(cfg) / (f"w-{pool}" if pool else "")
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps({"campaign": cid, "pid": pid, "since": since}))


def test_silent_held_for_gate_slot_waiter(tmp_path):
    import os
    cfg = _cfg(tmp_path)
    _slot_file(cfg, "waiting-c1.json", "c1", os.getpid(), "2026-10-30T10:00:00+00:00")
    for type_ in ("campaign_silent", "state_stuck"):
        ev = _ev(type_, campaign="c1", state="GATING", silent_s=1000, stuck_s=1000)
        held = {"held": True, "in_flight": []}
        assert QT.classify(cfg, ev, _ctx(cfg, held)) == "hold"
        assert QT.classify(cfg, ev, _ctx(cfg, {"held": False, "in_flight": []})) == "wake"
        other = _ev(type_, campaign="c2", state="GATING")
        assert QT.classify(cfg, other, _ctx(cfg, held)) == "wake"
    _slot_file(cfg, "waiting-c1.json", "c1", _dead())
    assert QT.classify(cfg, ev, _ctx(cfg, held)) == "wake"


def test_silent_held_for_live_gate_hold_within_budget(tmp_path):
    import os
    cfg = _cfg(tmp_path)
    now = dt.datetime.now().astimezone()
    limit = E.settings(cfg)["gate_budget_s"] + QT._GRACE_S
    ev = _ev("campaign_silent", campaign="c1", state="GATING", silent_s=1000)
    lease = {"held": True, "in_flight": []}

    def verdict(since, pid=None, pool=None):
        _slot_file(cfg, "slot-0.lock", "c1", pid or os.getpid(), since, pool)
        return QT.classify(cfg, ev, _ctx(cfg, lease, now=now))

    ago = lambda s: (now - dt.timedelta(seconds=s)).isoformat()   # noqa: E731
    assert verdict(ago(limit - 5)) == "hold"
    assert verdict(ago(limit + 5)) == "wake"
    assert verdict(ago(10), pid=_dead()) == "wake"
    assert verdict(None) == "wake"
    assert verdict("garbage") == "wake"
    import shutil
    from nightwright.agentic import gate_slots as GS
    shutil.rmtree(GS.slots_dir(cfg))
    assert verdict(ago(10), pool="box") == "hold"                 # a worker pool hold
    assert verdict(ago(limit + 5), pool="box") == "wake"


def test_silent_held_for_gate_deferred_by_drain(tmp_path):
    cfg = _cfg(tmp_path)
    now = dt.datetime.now().astimezone()
    budget = E.settings(cfg)["gate_budget_s"]
    states = _states(c1="GATING", c2="IMPLEMENTING")

    def verdict(age, waiting_on=None, cid="c1", in_flight=()):
        lease = {"held": True, "in_flight": list(in_flight), "draining": {
            "since": (now - dt.timedelta(seconds=age)).isoformat(), "waiting_on": waiting_on}}
        ev = _ev("campaign_silent", campaign=cid, state="GATING", silent_s=1000)
        return QT.classify(cfg, ev, QT.Ctx(cfg, "conductor", lease=lease, now=now,
                                           states=states))

    assert verdict(100) == "hold"
    assert verdict(budget + 300 - 5) == "hold"
    assert verdict(budget + 300 + 5) == "wake"
    assert verdict(100, [{"campaign": "a", "timeout_s": 7200}]) == "hold"
    assert verdict(budget + 400, [{"campaign": "a", "timeout_s": 7200}]) == "hold"
    assert verdict(7200 + 305, [{"campaign": "a", "timeout_s": 7200}]) == "wake"
    assert verdict(100, cid="c2") == "wake"                         # not GATING
    assert verdict(100, cid="nope") == "wake"                       # unknown campaign
    assert verdict(100, in_flight=["c1"]) == "hold"                 # in flight: the old rule
    ev = _ev("state_stuck", campaign="c1", state="GATING", stuck_s=1000)
    lease = {"held": True, "in_flight": [], "draining": {"since": now.isoformat()}}
    assert QT.classify(cfg, ev, QT.Ctx(cfg, "conductor", lease=lease, now=now,
                                       states=states)) == "hold"
    lease["draining"] = None
    assert QT.classify(cfg, ev, QT.Ctx(cfg, "conductor", lease=lease, now=now,
                                       states=states)) == "wake"
    lease["draining"] = {"since": now.isoformat()}
    lease["held"] = False
    assert QT.classify(cfg, ev, QT.Ctx(cfg, "conductor", lease=lease, now=now,
                                       states=states)) == "wake"


def test_silent_held_for_gate_deferred_behind_another_gate(tmp_path):
    cfg = _cfg(tmp_path)
    now = dt.datetime.now().astimezone()
    budget = E.settings(cfg)["gate_budget_s"]
    limit = 2 * budget + QT._GRACE_S

    def verdict(states, in_flight, *, age=100, cid="c1", ev_type="campaign_silent"):
        if age is not None:
            for st in states.values():
                st["updated_at"] = (now - dt.timedelta(seconds=age)).isoformat()
        lease = {"held": True, "in_flight": in_flight}
        ev = _ev(ev_type, campaign=cid, state="GATING", silent_s=1000, stuck_s=1000)
        return QT.classify(cfg, ev, QT.Ctx(cfg, "conductor", lease=lease, now=now,
                                           states=states))

    for t in ("campaign_silent", "state_stuck"):
        own = _states(c1=("GATING", True), g="GATING", cl="CLOSING")
        assert verdict(own, ["g", "cl"], ev_type=t) == "hold"       # own exclusive marker
        assert verdict(own, ["cl"], ev_type=t) == "hold"
        other = _states(c1="GATING", g=("GATING", True))
        assert verdict(other, ["g"], ev_type=t) == "hold"           # another's marker
    base = _states(c1="GATING", g=("GATING", True), im="IMPLEMENTING")
    assert verdict(base, ["im"]) == "wake"                          # other state in flight
    assert verdict(base, []) == "wake"
    assert verdict(base, ["c1"]) == "hold"                          # in flight itself: old rule
    assert verdict(_states(c1="CLOSING", g=("GATING", True)), ["g"]) == "wake"
    assert verdict(_states(c1="GATING", g="GATING"), ["g"]) == "wake"        # no marker
    assert verdict(_states(c1="GATING", cl="CLOSING"), ["cl"]) == "wake"
    assert verdict(_states(c1="GATING", cl=("CLOSING", True)), ["cl"]) == "wake"   # non-GATING
    ok = _states(c1="GATING", g=("GATING", True))
    assert verdict(ok, ["g"], age=limit - 5) == "hold"
    assert verdict(ok, ["g"], age=limit + 5) == "wake"              # over age
    assert verdict(_states(c1="GATING", g=("GATING", True)), ["g"], age=None) == "wake"


def test_ctx_states_built_from_store(tmp_path):
    cfg = _cfg(tmp_path)
    for cid, state, extra in (("a", "GATING", {"gate_exclusive": {"entered_at": "x"}}),
                              ("b", "GATING", {}), ("c", "CLOSING", {"closing": {"attempts": 0}})):
        store.create_campaign(cfg, {"id": cid, "spec_id": "SPEC-0001", "project": "p",
                                    "state": state, **extra})
    ctx = QT.Ctx(cfg, "conductor", lease={"held": False})
    st = ctx.states
    assert {k: (v["state"], v["gate_exclusive"]) for k, v in st.items()} == {
        "a": ("GATING", True), "b": ("GATING", False), "c": ("CLOSING", False)}
    assert ctx.closing_waiters == {}
    assert ctx.rows is ctx.rows                                 # one cached scan


def test_settings_validate(tmp_path):
    assert QT.settings(_cfg(tmp_path)) == QT.DEFAULTS
    s = QT.settings(_cfg(tmp_path, {"enabled": "yes", "readers": "x", "digest_interval_s": -1}))
    assert s == QT.DEFAULTS
    assert QT.enabled_for(_cfg(tmp_path), "conductor")
    assert not QT.enabled_for(_cfg(tmp_path), "steward")
    assert not QT.enabled_for(_cfg(tmp_path, {"enabled": False}), "conductor")


def test_settings_custom_readers_and_interval(tmp_path):
    cfg = _cfg(tmp_path, {"readers": ["steward"], "digest_interval_s": 60})
    assert QT.settings(cfg)["readers"] == ["steward"]
    assert QT.settings(cfg)["digest_interval_s"] == 60
    assert QT.enabled_for(cfg, "steward")
    assert not QT.enabled_for(cfg, "conductor")


def test_custom_digest_interval_changes_when_digest_prints(tmp_path):
    cfg, clock = _cfg(tmp_path, {"digest_interval_s": 60}), Clock()
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "admitted", campaign="a")
    out, calls = io.StringIO(), {"n": 0}

    def stop():
        calls["n"] += 1
        if calls["n"] == 1:
            clock.advance(30)                           # inside 60 s: still held
            return False
        if calls["n"] == 2:
            assert out.getvalue() == ""
            clock.advance(31)                           # past 60 s, far inside the 1800 default
            return False
        return True
    _follow(cfg, out, clock=clock, stop=stop)
    (digest,) = _lines(out)
    assert digest["type"] == "conductor_digest"


# ---- the digest -----------------------------------------------------------------------------

def test_digest_emitted_after_interval(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "admitted", campaign="a")
    E.append(cfg, "merged", campaign="m", spec_id="SPEC-0001", data={"merged_sha": "s"})
    out, calls = io.StringIO(), {"n": 0}

    def stop():
        calls["n"] += 1
        if calls["n"] == 1:
            assert out.getvalue() == ""                 # held, interval not reached
            clock.advance(1801)
            return False
        return True
    _follow(cfg, out, clock=clock, stop=stop)
    (digest,) = _lines(out)
    assert digest["type"] == "conductor_digest" and digest["id"] is None
    d = digest["data"]
    assert (d["first_id"], d["last_id"]) == (1, 2)
    assert d["held"] == {"admitted": 1, "merged": 1}
    assert d["merged"] == [{"campaign": "m", "spec_id": "SPEC-0001", "label": "SPEC-0001"}]
    assert E.cursor_get(cfg, "conductor") == 2


def test_digest_text_line(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "merged", campaign="m", spec_id="SPEC-0001", data={"merged_sha": "s"})
    clock.advance(4000)
    out = io.StringIO()
    _follow(cfg, out, clock=clock, as_json=False)
    assert out.getvalue() == "digest #1..#1 held=merged:1 merged=SPEC-0001\n"


def test_digest_never_empty(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "message", by="t", data=DATA["message"])
    clock.advance(10 ** 6)
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    assert [e["type"] for e in _lines(out)] == ["message"]


def _delivery_actions(cfg):
    return [r["action"] for r in QT.read_deliveries(cfg, "conductor")]


def _hold_pings(cfg, clock, n=2):
    for _ in range(n):
        E.append(cfg, "conductor_ping", by="watchdog", data=_ping())
    _follow(cfg, io.StringIO(), clock=clock)            # held, interval not reached


def test_digest_worth_printing():
    def digest(*events):
        d = QT.Digest(Path("unused"))
        for i, (type_, items) in enumerate(events, 1):
            ev = _ev(type_)
            ev["id"] = i
            if type_ == "conductor_ping":
                ev["data"] = {"stuck": [{"campaign": c} for c in items]}
            d.add(ev)
        return d
    assert not digest().worth_printing
    assert not digest(("conductor_ping", ())).worth_printing
    assert not digest(("conductor_ping", ()), ("conductor_ping", ())).worth_printing
    assert digest(("conductor_ping", ("a",))).worth_printing
    assert digest(("conductor_ping", ()), ("admitted", ())).worth_printing
    assert digest(("admitted", ())).worth_printing


def test_digest_skipped_when_only_empty_pings(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)
    _hold_pings(cfg, clock)
    clock.advance(1801)
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    assert out.getvalue() == ""
    dg = QT.Digest.load(cfg, "conductor")
    assert dg.pending["first_id"] is None and dg.last_printed_last_id == 2
    assert "digest" not in _delivery_actions(cfg)
    assert _delivery_actions(cfg).count("hold") == 2
    rep = QT.wakeups_report(cfg)
    assert rep["digests"] == 0 and rep["delivered"] == 0 and rep["held"] == 2


def test_digest_printed_when_pings_and_other_event(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)
    _hold_pings(cfg, clock)
    E.append(cfg, "admitted", campaign="a")
    clock.advance(1801)
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    (digest,) = _lines(out)
    assert digest["data"]["held"] == {"conductor_ping": 2, "admitted": 1}
    assert digest["data"]["pings_held"] == 2 and digest["data"]["ping_items"] == []
    assert "digest" in _delivery_actions(cfg)


def test_digest_printed_when_ping_items_nonempty(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)
    for _ in range(2):                                  # the second one is held
        E.append(cfg, "conductor_ping", by="watchdog", data=_ping(stuck=["a"]))
    _follow(cfg, io.StringIO(), clock=clock)
    clock.advance(1801)
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    (digest,) = _lines(out)
    assert digest["data"]["ping_items"] == [["stuck", "a"]]
    assert digest["data"]["pings_held"] == 1


def test_digest_restarts_after_skipped_digest(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)
    _hold_pings(cfg, clock)
    clock.advance(1801)
    _follow(cfg, io.StringIO(), clock=clock)            # discarded
    ev = E.append(cfg, "admitted", campaign="a")
    clock.t = dt.datetime.fromisoformat(ev["ts"])       # the new digest starts at its event
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    assert out.getvalue() == ""
    assert QT.Digest.load(cfg, "conductor").pending["since"] == ev["ts"]
    clock.advance(1799)
    _follow(cfg, out, clock=clock)
    assert out.getvalue() == ""
    clock.advance(2)
    _follow(cfg, out, clock=clock)
    (digest,) = _lines(out)
    assert digest["data"]["held"] == {"admitted": 1}


def test_skipped_digest_pings_not_readded(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)
    _hold_pings(cfg, clock)
    clock.advance(1801)
    _follow(cfg, io.StringIO(), clock=clock)
    dg = QT.Digest.load(cfg, "conductor")
    for ev in E.read_since(cfg, 0, types=["conductor_ping"]):
        assert dg.add(ev) is False
    assert dg.empty


def test_digest_survives_rearm(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)
    for _ in range(3):
        E.append(cfg, "admitted", campaign="a")
    out1 = io.StringIO()
    _follow(cfg, out1, clock=clock)                       # exits (as on code_changed)
    assert out1.getvalue() == ""
    assert QT.Digest.load(cfg, "conductor").pending["held"] == {"admitted": 3}
    clock.advance(1801)
    out2 = io.StringIO()
    _follow(cfg, out2, clock=clock)
    (digest,) = _lines(out2)
    assert digest["data"]["held"] == {"admitted": 3}
    assert (digest["data"]["first_id"], digest["data"]["last_id"]) == (1, 3)
    out3 = io.StringIO()
    clock.advance(1801)
    _follow(cfg, out3, clock=clock)
    assert out3.getvalue() == ""                          # nothing delivered twice


def test_ping_items_in_digest_and_fp_kept_after_clear(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)

    def ping(**kw):
        E.append(cfg, "conductor_ping", by="watchdog", data=_ping(**kw))
    ping(stuck=["a"])                                     # wakes (new fingerprint)
    ping(stuck=["a"])                                     # held
    ping(stuck=["a"])                                     # held
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    assert [e["type"] for e in _lines(out)] == ["conductor_ping"]
    clock.advance(1801)
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    (digest,) = _lines(out)
    assert digest["data"]["pings_held"] == 2
    assert digest["data"]["ping_items"] == [["stuck", "a"]]
    # the digest was cleared, last_ping_fp is kept: the same ping is still held
    assert QT.Digest.load(cfg, "conductor").empty
    ping(stuck=["a"])
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    assert [e["type"] for e in _lines(out)] == ["conductor_digest"]     # not a ping line
    # an all-clear ping resets it: the same items later wake again
    ping()
    ping(stuck=["a"])
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    assert [e["type"] for e in _lines(out)].count("conductor_ping") == 1


def test_hold_crash_between_digest_and_cursor(tmp_path, monkeypatch):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "admitted", campaign="a")
    real = E.cursor_set

    def boom(*a, **kw):
        monkeypatch.setattr(E, "cursor_set", real)
        raise OSError("killed")
    monkeypatch.setattr(E, "cursor_set", boom)
    with pytest.raises(OSError):
        _follow(cfg, io.StringIO(), clock=clock)
    assert E.cursor_get(cfg, "conductor") == 0            # cursor did not advance
    assert QT.Digest.load(cfg, "conductor").pending["held"] == {"admitted": 1}
    _follow(cfg, io.StringIO(), clock=clock)              # re-arm: re-read, not re-counted
    assert E.cursor_get(cfg, "conductor") == 1
    clock.advance(1801)
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    (digest,) = _lines(out)
    assert digest["data"]["held"] == {"admitted": 1}
    assert QT.wakeups_report(cfg)["held"] == 1            # recorded once


def test_hold_crash_between_record_and_save_keeps_the_hold(tmp_path, monkeypatch):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "admitted", campaign="a")
    real = QT.record_delivery
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("killed")
        return real(*a, **kw)
    monkeypatch.setattr(QT, "record_delivery", flaky)
    with pytest.raises(OSError):
        _follow(cfg, io.StringIO(), clock=clock)
    assert E.cursor_get(cfg, "conductor") == 0
    _follow(cfg, io.StringIO(), clock=clock)              # re-arm: the event is held again
    assert E.cursor_get(cfg, "conductor") == 1
    assert QT.wakeups_report(cfg)["held"] == 1
    assert QT.Digest.load(cfg, "conductor").pending["held"] == {"admitted": 1}


def test_digest_crash_before_print(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "admitted", campaign="a")
    clock.advance(1801)

    class Flaky(io.StringIO):
        def write(self, s):
            raise BrokenPipeError

    with pytest.raises(BrokenPipeError):
        _follow(cfg, Flaky(), clock=clock)
    assert QT.Digest.load(cfg, "conductor").empty         # cleared and saved first
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    assert out.getvalue() == ""                           # dropped, never duplicated


def test_max_events_counts_delivered_only(tmp_path):
    cfg = _cfg(tmp_path)
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "admitted", campaign="a")
    E.append(cfg, "admitted", campaign="b")
    E.append(cfg, "message", by="t", data=DATA["message"])
    E.append(cfg, "admitted", campaign="c")
    E.append(cfg, "message", by="t", data={**DATA["message"], "msg_id": "2"})
    out = io.StringIO()
    assert _follow(cfg, out, max_events=1) == 1
    assert [e["id"] for e in _lines(out)] == [3]
    assert E.cursor_get(cfg, "conductor") == 3
    assert QT.Digest.load(cfg, "conductor").pending["held"] == {"admitted": 2}


# ---- quiet off ------------------------------------------------------------------------------

def _seed(cfg):
    E.cursor_set(cfg, "conductor", 0)
    for t in ("admitted", "merged", "message", "abandoned"):
        E.append(cfg, t, campaign="c", data=DATA.get(t, {}))


def test_quiet_off_is_byte_identical(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    expected = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in E.read_since(cfg, 0))
    out = io.StringIO()
    E.follow(cfg, "conductor", poll_s=0, out=out, code_root=None, quiet=False,
             stop=lambda: True)
    assert out.getvalue() == expected
    out = io.StringIO()                                   # the default is off too
    E.follow(cfg, "steward", poll_s=0, out=out, code_root=None, from_start=True,
             stop=lambda: True)
    assert out.getvalue() == expected
    assert not (E.cursors_dir(cfg) / "digests" / "conductor.json").exists()
    assert not QT.deliveries_path(cfg).exists()           # quiet off records nothing


def test_quiet_off_text_mode_is_byte_identical(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    expected = "".join(E.summary_line(e) + "\n" for e in E.read_since(cfg, 0))
    out = io.StringIO()
    E.follow(cfg, "conductor", poll_s=0, out=out, code_root=None, quiet=False, as_json=False,
             stop=lambda: True)
    assert out.getvalue() == expected
    assert not (E.cursors_dir(cfg) / "digests" / "conductor.json").exists()
    assert not QT.deliveries_path(cfg).exists()


def test_events_jsonl_unchanged(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    before = E.events_path(cfg).read_bytes()
    clock = Clock()
    clock.advance(4000)
    _follow(cfg, io.StringIO(), clock=clock)
    assert E.events_path(cfg).read_bytes() == before


# ---- counting -------------------------------------------------------------------------------

class _FakeWatcher:
    def __init__(self, *a, **kw):
        pass

    def poll(self, now):
        return {"from_sha": "a", "to_sha": "b", "paths": ["src/nightwright/x.py"], "skills_changed": []}


def test_deliveries_and_wakeups_report(tmp_path, monkeypatch):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)
    for i in range(2):
        E.append(cfg, "merged", campaign=f"m{i}", spec_id=f"SPEC-000{i}",
                 data={"merged_sha": "s"})
    for i in range(8):
        E.append(cfg, "admitted", campaign=f"a{i}")
    for i in range(6):
        E.append(cfg, "message", by="t", data={**DATA["message"], "msg_id": str(i)})
    clock.advance(1801)
    monkeypatch.setattr(CW, "Watcher", _FakeWatcher)
    out = io.StringIO()
    E.follow(cfg, "conductor", poll_s=0, out=out, code_root="x", quiet=True, now=clock)
    types = [e["type"] for e in _lines(out)]
    assert types.count("message") == 6 and types.count("conductor_digest") == 1
    assert types[-1] == "code_changed"
    rep = QT.wakeups_report(cfg)
    assert rep["schema"] == "nw.conductor.wakeups.v1"
    assert (rep["delivered"], rep["held"], rep["digests"], rep["merges"]) == (8, 10, 1, 2)
    assert rep["wakeups_per_merge"] == 4.0
    assert rep["by_type"]["code_changed"] == {"wake": 1, "hold": 0}
    assert rep["by_type"]["merged"] == {"wake": 0, "hold": 2}
    assert len(rep["by_day"]) == 1
    # the window is by day, inclusive
    assert QT.wakeups_report(cfg, since="2999-01-01")["delivered"] == 0
    assert QT.wakeups_report(cfg, reader="steward")["delivered"] == 0


def test_followers_ignores_digest_file(tmp_path):
    cfg = _cfg(tmp_path)
    E.cursor_set(cfg, "conductor", 0)
    QT.Digest.load(cfg, "conductor").save()
    assert [r["reader"] for r in E.followers(cfg)] == ["conductor"]


# ---- CLI ------------------------------------------------------------------------------------

def test_cli_events_all_flag(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(E, "follow", lambda cfg, reader, **kw: seen.append(kw["quiet"]))
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    run = lambda *args: CliRunner().invoke(cli, ["queue", "events", "--follow", *args])
    assert run("--for", "conductor").exit_code == 0
    assert run("--for", "conductor", "--all").exit_code == 0
    assert run("--for", "steward").exit_code == 0
    cfg["agentic"]["conductor"] = {"quiet": {"enabled": False}}
    assert run("--for", "conductor").exit_code == 0
    cfg["agentic"]["conductor"] = {"quiet": {"readers": ["steward"]}}
    assert run("--for", "conductor").exit_code == 0
    assert run("--for", "steward").exit_code == 0
    assert seen == [True, False, False, False, False, True]


def test_cli_events_campaign_filter(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    for cid in ("a", "b"):
        E.append(cfg, "run_failed", campaign=cid, by="t",
                 data={"persona": "x", "run_id": "r", "transient": True, "error": "429"})
    r = CliRunner().invoke(cli, ["queue", "events", "--campaign", "b", "--type", "run_failed"])
    assert r.exit_code == 0, r.output
    assert [e["campaign"] for e in json.loads(r.output)["events"]] == ["b"]


def test_cli_wakeups_json(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    QT.record_delivery(cfg, "conductor", {"id": 1, "type": "message", "campaign": None}, "wake")
    QT.record_delivery(cfg, "conductor", {"id": 2, "type": "admitted", "campaign": "a"}, "hold")
    r = CliRunner().invoke(cli, ["queue", "wakeups", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["schema"] == "nw.conductor.wakeups.v1"
    assert (out["delivered"], out["held"], out["merges"]) == (1, 1, 0)
    assert out["wakeups_per_merge"] is None               # a window with no merges
    E.append(cfg, "merged", campaign="m", data={"merged_sha": "s"})
    out = json.loads(CliRunner().invoke(cli, ["queue", "wakeups", "--json"]).output)
    assert (out["merges"], out["wakeups_per_merge"]) == (1, 1.0)
    r = CliRunner().invoke(cli, ["queue", "wakeups"])
    assert r.exit_code == 0 and "delivered 1" in r.output


def test_wakeups_rejects_malformed_days(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    for args in (["--since", "2026-10-3"], ["--until", "tomorrow"], ["--since", "2026-13-01"]):
        r = CliRunner().invoke(cli, ["queue", "wakeups", "--json", *args])
        assert r.exit_code != 0 and "YYYY-MM-DD" in r.output
    with pytest.raises(ValueError):
        QT.wakeups_report(cfg, since="2026-10-3")
    assert QT.wakeups_report(cfg, since="2026-10-03", until="2026-10-31")["delivered"] == 0


# ---- held drains are re-checked every poll ----------------------------------------------------

def _drain_lease(clock, drained_s, *, since_s=None):
    """A held lease draining since `since_s` before now (default `drained_s`)."""
    since = clock() - dt.timedelta(seconds=drained_s if since_s is None else since_s)
    return {"held": True, "in_flight": [], "busy": None,
            "draining": {"since": since.isoformat(), "reason": "r"}}


def _hold_drain(cfg, clock, lease, monkeypatch):
    # only quiet sees this lease: the watchdog in `events.sync` must not raise its own drain
    monkeypatch.setattr(QT, "L", types.SimpleNamespace(lease_status=lambda cfg, now=None:
                                                       lease["v"]))
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "runner_draining", data={"reason": "r", "draining_s": 61, "ready": 0,
                                           "slots": 1, "waiting_on": [{"campaign": "a",
                                                                       "timeout_s": 100}]})


def test_held_drain_wakes_when_it_outlives_its_window(tmp_path, monkeypatch):
    cfg, clock = _cfg(tmp_path), Clock()
    lease = {"v": _drain_lease(clock, 61)}                  # one drain, `since` never changes
    _hold_drain(cfg, clock, lease, monkeypatch)
    out = io.StringIO()
    step = {"n": 0}

    def stop():
        step["n"] += 1
        if step["n"] == 1:
            assert out.getvalue() == ""                     # 61 s <= 100 + 300: held
            clock.advance(400)                              # the same drain is now 461 s old
            return False
        return True
    _follow(cfg, out, clock=clock, stop=stop)
    (wake,) = _lines(out)
    assert wake["type"] == "runner_draining" and wake["id"] == 1
    assert wake["data"]["draining_s"] == pytest.approx(461, abs=2)
    assert QT.Digest.load(cfg, "conductor").drains == []
    actions = [json.loads(x)["action"] for x in
               QT.deliveries_path(cfg).read_text().splitlines()]
    assert actions == ["hold", "wake"]
    out2 = io.StringIO()
    _follow(cfg, out2, clock=clock)
    assert out2.getvalue() == ""                            # woken once only


def test_held_drain_dropped_when_the_drain_ends_or_changes(tmp_path, monkeypatch):
    for after in ({"held": True, "draining": None}, {"held": False},
                  {"held": True, "draining": {"since": "2000-01-01T00:00:00+00:00"}}):
        cfg, clock = _cfg(tmp_path / str(len(str(after)))), Clock()
        lease = {"v": _drain_lease(clock, 61)}
        _hold_drain(cfg, clock, lease, monkeypatch)
        out = io.StringIO()
        _follow(cfg, out, clock=clock)
        assert len(QT.Digest.load(cfg, "conductor").drains) == 1
        clock.advance(10 ** 4)
        lease["v"] = after
        _follow(cfg, out, clock=clock)
        assert [e["type"] for e in _lines(out)] == ["conductor_digest"]     # no drain wake
        assert QT.Digest.load(cfg, "conductor").drains == []


def test_held_drain_survives_rearm_and_clear(tmp_path, monkeypatch):
    cfg, clock = _cfg(tmp_path), Clock()
    lease = {"v": _drain_lease(clock, 61)}
    _hold_drain(cfg, clock, lease, monkeypatch)
    E.append(cfg, "admitted", campaign="a")
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    clock.advance(1801)
    since = lease["v"]["draining"]["since"]
    lease["v"] = _drain_lease(clock, 0)
    lease["v"]["draining"]["since"] = since
    _follow(cfg, out, clock=clock)
    kinds = [e["type"] for e in _lines(out)]
    assert sorted(kinds) == ["conductor_digest", "runner_draining"]


@pytest.mark.parametrize("rec", [
    {"held": []}, {"held": {"a": 1}, "last_id": "7"}, {"last_ping_fp": "x"},
    {"pings_held": None}, {"last_printed_last_id": "3"}, {"drains": [1]},
    {"drains": [{"since": "s", "bound_s": 1}]}, {"first_id": "a"}, ["list"],
    # SPEC-0324: wrong-typed elements, not just containers
    {"held": {"merged": "x"}}, {"campaigns": {"merged": "x"}}, {"campaigns": {"merged": [1]}},
    {"merged": ["x"]}, {"ping_items": ["x"]}, {"ping_items": [["a"]]},
    {"last_ping_fp": [["a", 1]]}, {"watches": ["x"]},
    {"watches": [{"key": "k", "type": "t", "bound_s": "1", "event": {}}]}])
def test_corrupt_digest_file_falls_back_to_fresh(tmp_path, rec):
    cfg = _cfg(tmp_path)
    path = E.cursors_dir(cfg) / "digests" / "conductor.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec))
    d = QT.Digest.load(cfg, "conductor")
    assert d.empty and d.last_ping_fp == [] and d.last_printed_last_id == 0 and d.drains == []
    assert d.watches == []
    assert d.pending == QT._fresh()                         # every element check, not just first_id
    out = io.StringIO()
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "admitted", campaign="a")
    _follow(cfg, out)                                       # re-arming does not crash
    assert QT.Digest.load(cfg, "conductor").pending["held"] == {"admitted": 1}


# ---- SPEC-0305: the spec-phase events are held ------------------------------------------------

SPEC_PHASE = ("spec_written", "spec_reviewed", "spec_accepted")


def _spec_phase_log(cfg):
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "spec_written", campaign="c", data={"spec_id": "SPEC-0001"})
    E.append(cfg, "spec_reviewed", campaign="c",
             data={"spec_id": "SPEC-0001", "verdict": "revise", "round": 1})
    E.append(cfg, "spec_accepted", campaign="c", data={"spec_id": "SPEC-0001"})
    E.append(cfg, "parked", campaign="c", data={"reason": "spec_review_exhausted", "detail": "d", "resume_to": "SPEC_WRITING"})


def test_spec_phase_events_are_held(tmp_path):
    cfg = _cfg(tmp_path)
    ctx = _ctx(cfg)
    for t in SPEC_PHASE:
        assert QT.RULES[t] == "hold"
    for data in ({}, {"verdict": "accept", "round": 1}, {"verdict": "revise", "round": 2}):
        for t in SPEC_PHASE:
            assert QT.classify(cfg, _ev(t, "c", **data), ctx) == "hold"


def test_gate_waiting_is_held(tmp_path):
    cfg = _cfg(tmp_path)
    assert QT.RULES["gate_waiting"] == "hold"
    data = {"slots": 2, "held": [{"slot": 0}], "waited_s": 31.0}
    assert QT.classify(cfg, _ev("gate_waiting", "c", **data), _ctx(cfg)) == "hold"


def test_follow_holds_spec_phase_events(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    _spec_phase_log(cfg)
    before = E.events_path(cfg).read_bytes()
    clock.advance(1801)
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    assert [e["type"] for e in _lines(out)] == ["parked", "conductor_digest"]
    digest = _lines(out)[-1]["data"]
    assert all(digest["held"][t] == 1 for t in SPEC_PHASE)
    assert E.events_path(cfg).read_bytes() == before
    # --all (quiet off) and a steward reader (quiet is not enabled for it) see every event
    assert not QT.enabled_for(cfg, "steward")
    for reader, quiet in (("conductor", False), ("steward", QT.enabled_for(cfg, "steward"))):
        E.cursor_set(cfg, reader, 0)
        out = io.StringIO()
        E.follow(cfg, reader, poll_s=0, out=out, code_root=None, quiet=quiet, stop=lambda: True)
        assert [e["type"] for e in _lines(out)] == [*SPEC_PHASE, "parked"]


def test_wakeups_report_counts_spec_phase_as_held(tmp_path):
    cfg = _cfg(tmp_path)
    _spec_phase_log(cfg)
    _follow(cfg, io.StringIO(), clock=Clock())
    by_type = QT.wakeups_report(cfg)["by_type"]
    for t in SPEC_PHASE:
        assert by_type[t] == {"wake": 0, "hold": 1}
    assert by_type["parked"] == {"wake": 1, "hold": 0}


# A decision-table row whose action says "none" / "informational" names event types the quiet
# follower must hold unless one is listed here with the reason it must still wake (or is
# synthetic). The audit of the table found none: every informational type is in `QT.RULES`.
INFORMATIONAL_EXEMPT: dict[str, str] = {}

_CONDUCT_SKILL = Path(__file__).resolve().parent.parent / "skills" / "nw-conduct" / "SKILL.md"


def uncovered_informational_types(table_text: str) -> set[str]:
    """Event types named in the first column of informational rows (action `none` /
    `informational`) that are neither in `QT.RULES` nor in `INFORMATIONAL_EXEMPT`."""
    missing = set()
    for ln in table_text.splitlines():
        cells = [c.strip() for c in ln.strip().strip("|").split("|")] if ln.startswith("|") else []
        if len(cells) < 3 or not cells[-1].lower().startswith(("none", "informational")):
            continue
        for t in re.findall(r"`([a-z_]+)`", cells[0]):
            if t not in QT.RULES and t not in INFORMATIONAL_EXEMPT:
                missing.add(t)
    return missing


def test_decision_table_informational_rows_covered_by_rules():
    text = _CONDUCT_SKILL.read_text(encoding="utf-8")
    assert uncovered_informational_types(text) == set()
    assert all(t not in QT.RULES for t in INFORMATIONAL_EXEMPT)
    fake = "| `foo_event` | `foo_event` | none: informational |\n| `merged` | `merged` | none |"
    assert uncovered_informational_types(fake) == {"foo_event"}


def test_parked_auto_is_held(tmp_path):
    """SPEC-0302: a park the queue decided itself is held; any other park, and the queue giving
    up an automatic resume, wake."""
    cfg = _cfg(tmp_path)
    c = _ctx(cfg)
    auto = {"rule": "parked.base_moving", "resume_at": "2026-10-30T10:01:00+00:00"}
    assert QT.RULES["parked"] == "cond" and QT.RULES["auto_resume_failed"] == "wake"
    assert QT.classify(cfg, _ev("parked", reason="base_moving", auto=auto), c) == "hold"
    assert QT.classify(cfg, _ev("parked", reason="base_moving", auto=None), c) == "wake"
    assert QT.classify(cfg, _ev("parked", reason="cost_cap"), c) == "wake"
    assert QT.classify(cfg, _ev("auto_resume_failed", rule="parked.base_moving", error="x"),
                       c) == "wake"


def test_parked_by_conductor_is_held(tmp_path):
    """SPEC-0315: a park the conductor itself made is held, whatever its reason; the rule reads
    only the literal `by`."""
    cfg = _cfg(tmp_path)
    c = _ctx(cfg)
    assert QT.RULES["parked"] == "cond"
    auto = {"rule": "parked.base_moving", "resume_at": "2026-10-30T10:01:00+00:00"}
    for reason in ("human", "human_decision", "loop_exhausted"):
        assert QT.classify(cfg, _ev("parked", reason=reason, by="conductor"), c) == "hold"
    assert QT.classify(cfg, _ev("parked", reason="base_moving", by="conductor", auto=auto),
                       c) == "hold"
    assert QT.classify(cfg, _ev("parked", reason="base_moving", by="tick", auto=auto), c) == "hold"
    for by in ("tick", "queue", "human", "watchdog", "steward:Meta-Tools", "Conductor", "",
               None):
        assert QT.classify(cfg, _ev("parked", reason="human", by=by), c) == "wake", by
    ev = _ev("parked", reason="human")
    assert "by" not in ev and QT.classify(cfg, ev, c) == "wake"


def _append_parks(cfg):
    E.cursor_set(cfg, "conductor", 0)
    for cid, by in (("a", "conductor"), ("b", "tick")):
        E.append(cfg, "parked", campaign=cid, project="Meta-Tools", by=by,
                 data={"reason": "human", "detail": "hold for test", "resume_to": "GATING"})


def test_follow_holds_conductor_parks_only(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    _append_parks(cfg)
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    assert [e["campaign"] for e in _lines(out) if e["type"] == "parked"] == ["b"]
    clock.advance(1801)
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    (digest,) = [e for e in _lines(out) if e["type"] == "conductor_digest"]
    assert digest["data"]["held"]["parked"] == 1
    assert digest["data"]["campaigns"]["parked"] == ["a"]
    # --all and a steward reader still see both; events.jsonl keeps both
    out = io.StringIO()
    E.cursor_set(cfg, "conductor", 0)
    E.follow(cfg, "conductor", poll_s=0, out=out, code_root=None, quiet=False, now=clock,
             stop=lambda: True)
    assert [e["campaign"] for e in _lines(out) if e["type"] == "parked"] == ["a", "b"]
    out = io.StringIO()
    E.cursor_set(cfg, "steward", 0)
    E.follow(cfg, "steward", poll_s=0, out=out, code_root=None,
             quiet=QT.enabled_for(cfg, "steward"), now=clock, stop=lambda: True)
    assert [e["campaign"] for e in _lines(out) if e["type"] == "parked"] == ["a", "b"]
    assert [(e["campaign"], e["by"]) for e in E.read_since(cfg, 0) if e["type"] == "parked"] \
        == [("a", "conductor"), ("b", "tick")]


def test_wakeups_report_counts_conductor_park_as_held(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    _append_parks(cfg)
    _follow(cfg, io.StringIO(), clock=clock)
    assert QT.wakeups_report(cfg)["by_type"]["parked"] == {"wake": 1, "hold": 1}


# ---- SPEC-0324: replay, digest file isolation, bounded silent/stuck holds ----------------------

def _printed_digest(cfg, clock, n=2):
    """Hold `n` admitted events and print their digest: cursor n, last_printed_last_id n."""
    E.cursor_set(cfg, "conductor", 0)
    for _ in range(n):
        E.append(cfg, "admitted", campaign="a")
    clock.advance(1801)
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    assert [e["type"] for e in _lines(out)] == ["conductor_digest"]
    assert QT.Digest.load(cfg, "conductor").last_printed_last_id == n


def _hold_ids(cfg):
    return [json.loads(x)["event_id"] for x in QT.deliveries_path(cfg).read_text().splitlines()
            if json.loads(x)["action"] == "hold"]


def _cursor_file(cfg, reader="conductor"):
    return E.cursors_dir(cfg) / f"{E._safe(reader)}.json"


def test_from_start_replay_keeps_held_events(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    _printed_digest(cfg, clock)
    _cursor_file(cfg).unlink()
    clock.advance(1801)
    out = io.StringIO()
    _follow(cfg, out, clock=clock, from_start=True)
    (digest,) = _lines(out)
    assert digest["data"]["held"] == {"admitted": 2}
    assert (digest["data"]["first_id"], digest["data"]["last_id"]) == (1, 2)
    assert _hold_ids(cfg) == [1, 2, 1, 2]                 # a hold record per re-read event


def test_corrupt_cursor_replays_held_events(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    _printed_digest(cfg, clock)
    _cursor_file(cfg).write_text("{not json")
    clock.advance(1801)
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    (digest,) = _lines(out)
    assert digest["data"]["held"] == {"admitted": 2}
    assert _hold_ids(cfg) == [1, 2, 1, 2]


def test_deleted_cursor_without_from_start_does_not_replay(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    _printed_digest(cfg, clock)
    digest_path = E.cursors_dir(cfg) / "digests" / "conductor.json"
    before = digest_path.read_text()
    _cursor_file(cfg).unlink()
    clock.advance(1801)
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    assert out.getvalue() == ""
    assert E.cursor_get(cfg, "conductor") == 2            # the end of the stream
    assert digest_path.read_text() == before              # not reset
    assert _hold_ids(cfg) == [1, 2]


def test_nonzero_cursor_keeps_crash_dedup(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    _printed_digest(cfg, clock)
    E.cursor_set(cfg, "conductor", 1)                     # a crash left the cursor behind
    clock.advance(1801)
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    assert out.getvalue() == ""                           # event 2 was printed: not added twice
    dg = QT.Digest.load(cfg, "conductor")
    assert dg.empty and dg.last_printed_last_id == 2      # no reset
    assert _hold_ids(cfg) == [1, 2]


def test_replay_does_not_track_or_wake_old_silent_events(tmp_path, monkeypatch):
    cfg, clock = _cfg(tmp_path), Clock()
    lease = {"held": True, "in_flight": ["c1"], "busy": None, "draining": None}
    monkeypatch.setattr(QT, "L", types.SimpleNamespace(lease_status=lambda cfg, now=None: lease))
    _printed_digest(cfg, clock)
    E.append(cfg, "campaign_silent", campaign="c1", data={"state": "GATING", "silent_s": 900})
    clock.advance(5000)                                   # the event is now far past its bound
    _cursor_file(cfg).unlink()
    out = io.StringIO()
    _follow(cfg, out, clock=clock, from_start=True)
    (digest,) = _lines(out)                               # no stale wake line
    assert digest["data"]["held"]["campaign_silent"] == 1
    assert QT.Digest.load(cfg, "conductor").watches == []


def test_replay_does_not_track_or_wake_old_drain(tmp_path, monkeypatch):
    cfg, clock = _cfg(tmp_path), Clock()
    lease = {"v": _drain_lease(clock, 61)}                # the same drain, still draining later
    monkeypatch.setattr(QT, "L", types.SimpleNamespace(lease_status=lambda cfg, now=None:
                                                       lease["v"]))
    _printed_digest(cfg, clock)
    E.append(cfg, "runner_draining", data={"reason": "r", "draining_s": 61, "ready": 0,
                                           "slots": 1, "waiting_on": [{"campaign": "a",
                                                                       "timeout_s": 100}]})
    clock.advance(5000)                                   # far past the drain's bound
    _cursor_file(cfg).unlink()
    out = io.StringIO()
    _follow(cfg, out, clock=clock, from_start=True)
    (digest,) = _lines(out)                               # no stale runner_draining wake line
    assert digest["type"] == "conductor_digest"
    assert digest["data"]["held"]["runner_draining"] == 1
    assert QT.Digest.load(cfg, "conductor").drains == []


def test_corrupt_digest_non_utf8_and_directory(tmp_path, caplog):
    cfg = _cfg(tmp_path / "a")
    path = E.cursors_dir(cfg) / "digests" / "conductor.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe\x00{")
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "admitted", campaign="a")
    _follow(cfg, io.StringIO())
    assert json.loads(path.read_text())["held"] == {"admitted": 1}      # rewritten
    cfg = _cfg(tmp_path / "b")                            # a directory at the digest path
    path = E.cursors_dir(cfg) / "digests" / "conductor.json"
    (path / "x").mkdir(parents=True)
    assert QT.Digest.load(cfg, "conductor").empty
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "admitted", campaign="a")
    with caplog.at_level("WARNING"):
        _follow(cfg, io.StringIO())                       # save() does not raise
    assert path.is_dir() and E.cursor_get(cfg, "conductor") == 1
    assert "not saved" in caplog.text


def test_digest_path_does_not_collide_with_cursor(tmp_path):
    cfg = _cfg(tmp_path)
    E.append(cfg, "admitted", campaign="a")
    for reader in ("conductor", "conductor.digest"):
        E.cursor_set(cfg, reader, 0)
        E.follow(cfg, reader, poll_s=0, out=io.StringIO(), code_root=None, quiet=True,
                 stop=lambda: True)
    for reader in ("conductor", "conductor.digest"):
        assert E.cursor_get(cfg, reader) == 1
        assert QT.Digest.load(cfg, reader).pending["held"] == {"admitted": 1}
        assert (E.cursors_dir(cfg) / "digests" / f"{reader}.json").is_file()
    assert [r["reader"] for r in E.followers(cfg)] == ["conductor", "conductor.digest"]


def test_legacy_digest_file_migrates(tmp_path):
    cfg = _cfg(tmp_path)
    base = E.cursors_dir(cfg)
    base.mkdir(parents=True)
    legacy = base / "conductor.digest.json"
    legacy.write_text(json.dumps({**QT._fresh(), "first_id": 1, "last_id": 2,
                                  "held": {"admitted": 2}, "last_printed_last_id": 0}))
    assert E.followers(cfg) == []                         # an unmigrated digest is no reader
    dg = QT.Digest.load(cfg, "conductor")
    assert dg.pending["held"] == {"admitted": 2}
    dg.save()
    assert (base / "digests" / "conductor.json").is_file() and not legacy.exists()
    assert QT.Digest.load(cfg, "conductor").pending["held"] == {"admitted": 2}
    # a legacy-named file that is a reader's cursor (`after` key) stays, and is not a digest
    legacy.write_text(json.dumps({"reader": "conductor.digest", "after": 3, "ts": "t"}))
    (base / "digests" / "conductor.json").unlink()
    dg = QT.Digest.load(cfg, "conductor")
    assert dg.empty
    dg.save()
    assert json.loads(legacy.read_text())["after"] == 3
    assert [r["reader"] for r in E.followers(cfg)] == ["conductor.digest"]


def _silent_setup(cfg, lease, monkeypatch, type_, clock, campaign="c1"):
    monkeypatch.setattr(QT, "L", types.SimpleNamespace(lease_status=lambda cfg, now=None:
                                                       lease["v"]))
    E.cursor_set(cfg, "conductor", 0)
    field = "silent_s" if type_ == "campaign_silent" else "stuck_s"
    E.append(cfg, type_, campaign=campaign, data={"state": "GATING", field: 3600})   # the watchdog fires near the budget
    return field


@pytest.mark.parametrize("type_", ["campaign_silent", "state_stuck"])
def test_silent_hold_wakes_once_after_bound(tmp_path, monkeypatch, type_):
    cfg, clock = _cfg(tmp_path), Clock()
    lease = {"v": {"held": True, "in_flight": ["c1"], "busy": None, "draining": None}}
    field = _silent_setup(cfg, lease, monkeypatch, type_, clock)
    bound = E.settings(cfg)["gate_budget_s"] + 300
    out, step = io.StringIO(), {"n": 0}

    def stop():
        step["n"] += 1
        if step["n"] == 1:
            assert out.getvalue() == ""                   # held: 3600 s old
            clock.advance(bound - 3600 - 50)               # still short of the bound
        elif step["n"] == 2:
            assert out.getvalue() == ""
            assert len(QT.Digest.load(cfg, "conductor").watches) == 1
            clock.advance(100)                            # past it
        return step["n"] >= 4
    _follow(cfg, out, clock=clock, stop=stop)
    (wake,) = _lines(out)
    assert wake["type"] == type_ and wake["id"] == 1
    assert wake["data"][field] == pytest.approx(bound + 50, abs=3)      # refreshed
    assert QT.Digest.load(cfg, "conductor").watches == []
    actions = [json.loads(x)["action"] for x in QT.deliveries_path(cfg).read_text().splitlines()]
    assert actions == ["hold", "wake"]
    out2 = io.StringIO()
    _follow(cfg, out2, clock=clock)
    assert out2.getvalue() == ""                          # woken once only


@pytest.mark.parametrize("type_", ["campaign_silent", "state_stuck"])
def test_silent_watch_dropped_when_flight_ends(tmp_path, monkeypatch, type_):
    cfg, clock = _cfg(tmp_path), Clock()
    lease = {"v": {"held": True, "in_flight": ["c1"], "busy": None, "draining": None}}
    _silent_setup(cfg, lease, monkeypatch, type_, clock)
    _follow(cfg, io.StringIO(), clock=clock)
    assert len(QT.Digest.load(cfg, "conductor").watches) == 1
    clock.advance(2000)
    lease["v"] = {"held": True, "in_flight": [], "busy": None, "draining": None}
    out = io.StringIO()
    _follow(cfg, out, clock=clock)
    assert [e["type"] for e in _lines(out)] == ["conductor_digest"]     # no silent/stuck wake
    assert QT.Digest.load(cfg, "conductor").watches == []


def test_silent_watch_of_close_train_waiter_is_kept_without_waking(tmp_path, monkeypatch):
    cfg, clock = _cfg(tmp_path), Clock()
    for cid, closing in (("h", {"attempts": 0}), ("w", {"queued_behind": "h"})):
        store.create_campaign(cfg, {"id": cid, "spec_id": "SPEC-0001", "project": "p",
                                    "state": "CLOSING", "closing": closing})
    lease = {"v": {"held": True, "in_flight": ["h"], "busy": None, "draining": None}}
    _silent_setup(cfg, lease, monkeypatch, "campaign_silent", clock, campaign="w")
    out, step = io.StringIO(), {"n": 0}

    def stop():
        step["n"] += 1
        clock.advance(500)                                # well past the bound
        return step["n"] >= 2
    _follow(cfg, out, clock=clock, stop=stop)
    # waiting behind a live head: no silent wake (the real watchdog's runner_dead is unrelated)
    assert "campaign_silent" not in [e["type"] for e in _lines(out)]
    assert len(QT.Digest.load(cfg, "conductor").watches) == 1


def test_silent_hold_through_ctx_lease(tmp_path, monkeypatch):
    cfg, clock = _cfg(tmp_path), Clock()
    busy = {"kind": "close", "campaign": "c1", "since": clock().isoformat(), "budget_s": 3600}
    # the key set `L.lease_status` really returns while held
    lease = {"v": {"held": True, "pid": 1, "in_flight": [], "busy": busy, "draining": None,
                   "stale": False}}
    _silent_setup(cfg, lease, monkeypatch, "campaign_silent", clock)
    out, step = io.StringIO(), {"n": 0}

    def stop():
        step["n"] += 1
        if step["n"] == 1:
            assert out.getvalue() == ""                   # held through Ctx.lease
            clock.advance(400)
        return step["n"] >= 3
    _follow(cfg, out, clock=clock, stop=stop)
    (wake,) = _lines(out)
    assert wake["type"] == "campaign_silent"
    from nightwright.agentic import lease as LS
    with LS.acquire(cfg):
        status = LS.lease_status(cfg)
    assert {"held", "in_flight", "busy"} <= set(status) and status["held"] is True


def test_deliveries_record_fields(tmp_path, monkeypatch):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "admitted", campaign="a")
    E.append(cfg, "message", campaign="m", by="t", data=DATA["message"])
    clock.advance(1801)
    monkeypatch.setattr(CW, "Watcher", _FakeWatcher)
    E.follow(cfg, "conductor", poll_s=0, out=io.StringIO(), code_root="x", quiet=True, now=clock)
    recs = QT.read_deliveries(cfg, "conductor")
    by_action = {r["action"]: r for r in recs}
    assert [r["action"] for r in recs] == ["hold", "wake", "digest", "wake"]
    for r in recs:
        assert r["reader"] == "conductor" and set(r) == {"ts", "reader", "event_id", "type",
                                                        "campaign", "action"}
    assert (by_action["hold"]["event_id"], by_action["hold"]["type"],
            by_action["hold"]["campaign"]) == (1, "admitted", "a")
    assert (recs[1]["event_id"], recs[1]["type"], recs[1]["campaign"]) == (2, "message", "m")
    assert by_action["digest"]["type"] == "conductor_digest"
    assert by_action["digest"]["event_id"] is None and by_action["digest"]["campaign"] is None
    assert recs[3]["type"] == "code_changed"
    assert recs[3]["event_id"] is None and recs[3]["campaign"] is None


def test_digest_counts_toward_max_events(tmp_path):
    cfg, clock = _cfg(tmp_path), Clock()
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "admitted", campaign="a")
    clock.advance(1801)
    out = io.StringIO()
    n = _follow(cfg, out, clock=clock, max_events=1,
                stop=lambda: pytest.fail("max_events not reached by the digest line"))
    assert n == 1 and [e["type"] for e in _lines(out)] == ["conductor_digest"]


def test_drain_wake_counts_toward_max_events(tmp_path, monkeypatch):
    cfg, clock = _cfg(tmp_path), Clock()
    lease = {"v": _drain_lease(clock, 61)}
    _hold_drain(cfg, clock, lease, monkeypatch)
    _follow(cfg, io.StringIO(), clock=clock)              # held
    clock.advance(400)
    out = io.StringIO()
    n = _follow(cfg, out, clock=clock, max_events=1,
                stop=lambda: pytest.fail("max_events not reached by the drain wake"))
    assert n == 1 and [e["type"] for e in _lines(out)] == ["runner_draining"]


def test_silent_wake_counts_toward_max_events(tmp_path, monkeypatch):
    cfg, clock = _cfg(tmp_path), Clock()
    lease = {"v": {"held": True, "in_flight": ["c1"], "busy": None, "draining": None}}
    _silent_setup(cfg, lease, monkeypatch, "state_stuck", clock)
    _follow(cfg, io.StringIO(), clock=clock)
    clock.advance(400)
    out = io.StringIO()
    n = _follow(cfg, out, clock=clock, max_events=1,
                stop=lambda: pytest.fail("max_events not reached by the watch wake"))
    assert n == 1 and [e["type"] for e in _lines(out)] == ["state_stuck"]


def test_ping_fingerprint_all_categories():
    data = {"blocked": [{"campaign": "b"}], "stuck": [{"campaign": "s"}],
            "ready_not_admitted": [{"campaign": "r"}], "waiting_not_admitted": [{}],
            "parked": [{"campaign": "p1", "owner": "conductor", "decided": False},
                       {"campaign": "p2", "owner": "human", "decided": False},
                       {"campaign": "p3", "owner": "conductor", "decided": True}],
            "runner": {"pid": 7}}
    assert QT.ping_fingerprint(data) == [
        ["blocked", "b"], ["parked", "p1"], ["ready_not_admitted", "r"], ["runner", "7"],
        ["stuck", "s"], ["waiting_not_admitted", "None"]]
    assert QT.ping_fingerprint({}) == []


def test_digest_campaign_cap_and_dedup(tmp_path):
    dg = QT.Digest(tmp_path / "d.json")
    for i in range(12):
        assert dg.add({"id": i + 1, "type": "admitted", "campaign": f"c{i}", "ts": "t"})
    assert dg.pending["held"] == {"admitted": 12}
    assert dg.pending["campaigns"]["admitted"] == [f"c{i}" for i in range(10)]
    assert dg.add({"id": 13, "type": "admitted", "campaign": "c0", "ts": "t"})
    assert dg.pending["campaigns"]["admitted"] == [f"c{i}" for i in range(10)]   # no repeat
    other = QT.Digest(tmp_path / "e.json")
    for i in range(3):
        other.add({"id": i + 1, "type": "waiting", "campaign": "same", "ts": "t"})
    assert other.pending["campaigns"]["waiting"] == ["same"]


def test_wakeups_window_filters_merges_and_until_is_inclusive(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    d1, d2, d3 = "2026-10-01", "2026-10-02", "2026-10-03"
    rows = [(d1, "wake"), (d1, "wake"), (d1, "hold"), (d2, "digest"), (d2, "hold"),
            (d2, "hold"), (d3, "wake")]
    path = QT.deliveries_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"ts": f"{d}T12:00:00+00:00", "reader": "conductor",
                                        "event_id": 1, "type": "message", "campaign": None,
                                        "action": a}) + "\n" for d, a in rows))
    merges = [(d1, "merged"), (d2, "handed_off"), (d3, "merged"), (d3, "merged")]
    monkeypatch.setattr(E, "read_since", lambda cfg, after, types=None: [
        {"ts": f"{d}T09:00:00+00:00", "type": t} for d, t in merges])

    def rep(**kw):
        r = QT.wakeups_report(cfg, **kw)
        return r["delivered"], r["held"], r["merges"], [x["day"] for x in r["by_day"]]
    assert rep() == (4, 3, 4, [d1, d2, d3])
    assert rep(since=d2) == (2, 2, 3, [d2, d3])           # earlier days excluded
    assert rep(until=d2) == (3, 3, 2, [d1, d2])           # `until` day included
    assert rep(since=d2, until=d2) == (1, 2, 1, [d2])
    assert QT.wakeups_report(cfg, since=d2, until=d2)["digests"] == 1
    r = QT.wakeups_report(cfg, since=d2, until=d2)
    assert r["by_day"] == [{"day": d2, "delivered": 1, "held": 2, "merges": 1}]


# ---- a digest that cannot be saved (full or read-only disk) ----------------------------------

def _unwritable(monkeypatch, state):
    real = QT._atomic_write

    def write(path, text):
        if state["fail"]:
            raise OSError(28, "No space left on device")
        return real(path, text)
    monkeypatch.setattr(QT, "_atomic_write", write)


def test_failed_save_keeps_held_event_in_memory(tmp_path, monkeypatch):
    cfg, clock = _cfg(tmp_path), Clock()
    state = {"fail": True}
    _unwritable(monkeypatch, state)
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "admitted", campaign="c1", data={})
    out, step = io.StringIO(), {"n": 0}

    def stop():
        step["n"] += 1
        if step["n"] == 2:
            state["fail"] = False                         # the disk recovers
        return step["n"] >= 4
    _follow(cfg, out, clock=clock, stop=stop)
    assert E.cursor_get(cfg, "conductor") == 1
    assert QT.Digest.load(cfg, "conductor").pending["held"] == {"admitted": 1}   # not lost
    assert out.getvalue() == ""


def test_failed_save_does_not_repeat_due_digest(tmp_path, monkeypatch):
    cfg, clock = _cfg(tmp_path), Clock()
    state = {"fail": False}
    _unwritable(monkeypatch, state)
    E.cursor_set(cfg, "conductor", 0)
    E.append(cfg, "admitted", campaign="c1", data={})
    out, step = io.StringIO(), {"n": 0}

    def stop():
        step["n"] += 1
        if step["n"] == 1:
            assert out.getvalue() == ""
            state["fail"] = True
            clock.advance(2000)                           # the digest is due
        return step["n"] >= 5
    _follow(cfg, out, clock=clock, stop=stop)
    assert [e["type"] for e in _lines(out)] == ["conductor_digest"]   # once, over four polls
    actions = [json.loads(x)["action"] for x in QT.deliveries_path(cfg).read_text().splitlines()]
    assert actions == ["hold", "digest"]


@pytest.mark.parametrize("type_", ["campaign_silent", "state_stuck"])
def test_failed_save_does_not_repeat_watch_wake(tmp_path, monkeypatch, type_):
    cfg, clock = _cfg(tmp_path), Clock()
    lease = {"v": {"held": True, "in_flight": ["c1"], "busy": None, "draining": None}}
    _silent_setup(cfg, lease, monkeypatch, type_, clock)
    state = {"fail": True}
    _unwritable(monkeypatch, state)
    out, step = io.StringIO(), {"n": 0}

    def stop():
        step["n"] += 1
        if step["n"] == 1:
            clock.advance(E.settings(cfg)["gate_budget_s"] + 400)
        return step["n"] >= 4
    _follow(cfg, out, clock=clock, stop=stop)
    assert [e["type"] for e in _lines(out) if e["type"] == type_] == [type_]


def test_failed_save_does_not_repeat_drain_wake(tmp_path, monkeypatch):
    cfg, clock = _cfg(tmp_path), Clock()
    since = (clock() - dt.timedelta(seconds=10)).isoformat()
    lease = {"held": True, "in_flight": [], "busy": None,
             "draining": {"since": since}}
    monkeypatch.setattr(QT, "L", types.SimpleNamespace(lease_status=lambda cfg, now=None: lease))
    state = {"fail": True}
    _unwritable(monkeypatch, state)
    d = QT.Digest.load(cfg, "conductor")
    d.drains = [{"since": since, "bound_s": 100,
                 "event": {"id": 1, "ts": since, "type": "runner_draining", "campaign": None,
                           "data": {"draining_s": 5}}}]
    state["fail"] = False
    d.save()
    state["fail"] = True
    E.cursor_set(cfg, "conductor", 1)
    out, step = io.StringIO(), {"n": 0}

    def stop():
        step["n"] += 1
        if step["n"] == 1:
            clock.advance(500)
        return step["n"] >= 4
    _follow(cfg, out, clock=clock, stop=stop)
    assert [e["type"] for e in _lines(out)] == ["runner_draining"]


@pytest.mark.parametrize("event", [{"type": "admitted", "data": {}},
                                   {"type": "campaign_silent", "data": []},
                                   {"type": "campaign_silent", "data": None}])
def test_watch_with_unusable_event_is_ignored(event):
    w = {"key": "k", "type": "campaign_silent", "bound_s": 1.0, "event": event}
    assert not QT._watch_ok(w)
    assert QT._watch_ok({**w, "event": {"type": "state_stuck"}})        # data absent is fine

