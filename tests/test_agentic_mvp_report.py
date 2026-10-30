"""SPEC-0304: `wt queue mvp-report`, the E1-E6 scorecard over the last N merged campaigns."""
import datetime as dt
import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from test_agentic_events import _campaign as ev_campaign
from test_agentic_events import _set_state
from wt.agentic import conductor_cost as CC
from wt.agentic import escalations as ESC
from wt.agentic import events as E
from wt.agentic import mvp_report as MVP
from wt.agentic import queue as Q
from wt.agentic import quiet
from wt.agentic import schemas as SC
from wt.agentic import store as S
from wt.cli import cli

UTC = dt.timezone.utc
PRICE = {"input": 10.0, "output": 0.0, "cache_read": 0.0, "cache_write_5m": 0.0,
         "cache_write_1h": 0.0}


def T(day, hour, minute=0):
    return f"2026-10-{day:02d}T{hour:02d}:{minute:02d}:00+00:00"


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "data").mkdir()
    return {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "config"),
            "_root": str(tmp_path / "projects"), "_tz": UTC, "timezone": "UTC",
            "specs_dir": str(tmp_path), "outbox_targets": {},
            "agentic": {"lease_ttl_s": 120, "conductor": {"prices": {"claude-x": PRICE}}}}


def camp(cfg, cid, start, end, *, spec=None, project="P", outcome="MERGED", events=(),
         **state):
    """A campaign whose ledger is written verbatim: `open` at `start`, then `events` ((ts, type,
    payload) in order), then, when `end`, a terminal transition. Timestamps are fixed."""
    st = {"id": cid, "spec_id": spec or f"SPEC-X{cid}", "project": project, "kind": "spec",
          "profile": "x", "state": outcome if end else "IMPLEMENTING", "created_at": start,
          "cost": {"total_usd": 0.0}, "counters": {}, **state}
    S.create_campaign(cfg, st)
    rows = [(start, "open", {"spec_id": st["spec_id"], "project": project}), *events]
    if end:
        rows.append((end, "transition", {"from": "CLOSING", "to": outcome, "reason": "x"}))
    recs = [{"schema": SC.LEDGER_SCHEMA, "ts": ts, "seq": i, "campaign": cid, "type": typ,
             "by": "t", **SC.with_defaults(typ, payload)} for i, (ts, typ, payload) in
            enumerate(rows, 1)]
    (S.campaign_dir(cfg, cid) / "ledger.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in recs))
    return cid


def measurement_specs(cfg, end=T(1, 3)):
    """SPEC-0299/0300/0301 merged by `end`."""
    for sid in MVP.MEASUREMENT_SPECS:
        camp(cfg, f"M{sid[-4:]}", T(1, 1), end, spec=sid)


def park_ev(ts, reason="loop_exhausted"):
    return (ts, "park", {"reason": reason, "detail": "d", "resume_to": "IMPLEMENTING"})


def out_of_park(ts):
    return (ts, "transition", {"from": "PARKED", "to": "IMPLEMENTING", "reason": "resume"})


def decide(cfg, seq, cid, ts, by="conductor"):
    path = Path(S.agentic_dir(cfg)) / "conductor" / "decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"schema": "wt.conductor.decision.v1", "seq": seq, "ts": ts, "by": by, "campaign": cid,
           "spec_id": None, "event_id": None, "rule": "parked.loop_exhausted", "kind": "resume",
           "action": None, "rationale": "r"}
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


def asst(msg_id, ts, tokens=0, model="claude-x", brief=False):
    content = [{"type": "tool_use", "name": "Bash",
                "input": {"command": "wt queue conductor-brief"}}] if brief else []
    return {"type": "assistant", "timestamp": ts, "message": {
        "id": msg_id, "model": model, "content": content,
        "usage": {"input_tokens": tokens, "output_tokens": 0, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0}}}


def compact(ts):
    return {"type": "system", "subtype": "compact_boundary", "timestamp": ts}


def conductor(cfg, sid, lines):
    d = Path(cfg["_root"]) / "-slug"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines))
    CC.record_session(cfg, "conductor", {}, source="manual", session_id=sid)


def crit(rep, cid):
    return next(c for c in rep["criteria"] if c["id"] == cid)


# ---- window ----------------------------------------------------------------------------------

def test_window_selection(cfg):
    measurement_specs(cfg)
    for i in range(1, 26):
        camp(cfg, f"C{i:02d}", T(2 + i // 2, 1 + i % 2), T(2 + i // 2, 5 + i % 2),
             project="Q" if i % 5 == 0 else "P")
    rep = MVP.report(cfg)
    w = rep["window"]
    assert rep["schema"] == "wt.conductor.mvp.v1"
    assert w["campaigns"] == [f"C{i:02d}" for i in range(6, 26)] and w["n"] == w["requested"] == 20
    assert w["qualifying"] is True and w["qualifying_reason"] is None
    assert w["start"] == "2026-10-05T01:00:00+00:00" and w["end"] == "2026-10-14T06:00:00+00:00"
    assert w["merges"] >= 20 and len(rep["criteria"]) == 6
    # --project keeps one project's campaigns
    only_q = MVP.report(cfg, n=5, project="Q")["window"]["campaigns"]
    assert only_q == ["C05", "C10", "C15", "C20", "C25"]
    # --since keeps campaigns opened at or after it
    since = MVP.report(cfg, n=20, since=dt.datetime(2026, 10, 12, tzinfo=UTC))["window"]
    assert all(c >= "C20" for c in since["campaigns"]) and not since["qualifying"]
    # fewer than N available
    few = MVP.report(cfg, n=30)["window"]
    assert few["n"] == 28 and few["qualifying"] is False


def test_window_ordered_by_terminal_ts(cfg):
    """Open order, id order and terminal order all differ: the last N are by terminal ts."""
    measurement_specs(cfg)
    camp(cfg, "a", T(2, 1), T(12, 1))                  # opened first, merged last
    camp(cfg, "b", T(3, 1), T(5, 1))
    camp(cfg, "c", T(4, 1), T(6, 1))
    camp(cfg, "d", T(3, 5), T(4, 1))
    w = MVP.report(cfg, n=2)["window"]
    assert w["campaigns"] == ["c", "a"]                # by start it would be d, c; by id c, d
    assert w["start"] == T(2, 1) and w["end"] == T(12, 1)


def test_merges_counts_campaigns_ended_in_interval(cfg):
    """merges is neither the window size nor the all-time count: another project's campaign
    ending inside [start, end] counts, campaigns ending outside do not."""
    measurement_specs(cfg)
    camp(cfg, "old1", T(5, 1), T(5, 2))
    camp(cfg, "old2", T(5, 3), T(6, 2))
    camp(cfg, "qold", T(5, 1), T(6, 3), project="Q")
    camp(cfg, "A", T(10, 10), T(10, 14))
    camp(cfg, "B", T(10, 11), T(10, 16))
    camp(cfg, "qin", T(9, 1), T(10, 12), project="Q")          # other project, inside
    camp(cfg, "qlate", T(10, 11), T(10, 20), project="Q")      # ends after the interval
    conductor(cfg, "s1", [asst("a", T(10, 9), 900_000),
                          asst("b", T(10, 11), 100_000), asst("c", T(10, 12), 200_000),
                          asst("d", T(10, 13), 300_000), asst("e", T(10, 17), 900_000)])
    deliveries(cfg, [(T(9, 1), "parked", "wake")] + [(T(10, 11, i), "parked", "wake")
                                                     for i in range(1, 7)])
    rep = MVP.report(cfg, n=2, project="P")
    w = rep["window"]
    assert w["campaigns"] == ["A", "B"] and w["n"] == 2
    assert w["merges"] == 3                            # A, B and qin; not 2, not the 9 all-time
    assert crit(rep, "E1")["value"] == pytest.approx(2.0)    # $6 / 3
    assert crit(rep, "E3")["value"] == pytest.approx(2.0)    # 6 / 3


def test_window_not_enough_campaigns(cfg):
    measurement_specs(cfg)
    for i in range(1, 13):                      # + the 3 measurement specs = 15
        camp(cfg, f"C{i:02d}", T(3, 1, i), T(3, 2, i))
    w = MVP.report(cfg)["window"]
    assert w["n"] == 15 and w["qualifying"] is False
    assert "15 of 20" in w["qualifying_reason"]


def test_window_empty_is_all_no_data(cfg):
    rep = MVP.report(cfg)
    assert rep["window"]["start"] is None and rep["window"]["end"] is None
    for c in rep["criteria"]:
        if c["id"] != "E5":
            assert c["value"] is None and c["status"] == "no_data", c
    assert crit(rep, "E5")["status"] == "fail" and rep["ok"] is False


def test_qualifying_after_measurement_specs(cfg):
    measurement_specs(cfg, end=T(5, 3))
    camp(cfg, "EARLY", T(4, 1), T(6, 1))                       # opened before they merged
    camp(cfg, "LATE", T(5, 4), T(7, 1))
    w = MVP.report(cfg, n=2)["window"]
    assert w["qualifying"] is False and "EARLY" in w["qualifying_reason"]
    assert MVP.report(cfg, n=1)["window"]["qualifying"] is True
    # a measurement spec that is not merged yet is named
    camp(cfg, "LIVE", T(5, 5), None, spec="SPEC-0301")
    for st in S.list_campaigns(cfg, include_terminal=True):
        if st["spec_id"] == "SPEC-0301" and st["state"] == "MERGED":
            S.write_state(cfg, st["id"], {**st, "state": "ABANDONED"})
    import shutil
    shutil.rmtree(S.campaign_dir(cfg, "M0301"))
    w = MVP.report(cfg, n=1)["window"]
    assert w["qualifying"] is False and "SPEC-0301" in w["qualifying_reason"]


def test_bad_targets_fall_back(cfg):
    cfg["agentic"]["conductor"]["mvp"] = {"targets": {"e1": "cheap", "e2": 5, "e3": True}}
    t = MVP.settings(cfg)
    assert t["e1"] == 1.0 and t["e2"] == 5 and t["e3"] == 5


# ---- E1 / E2 ---------------------------------------------------------------------------------

def one_window(cfg):
    camp(cfg, "W", T(10, 10), T(10, 14))


def test_e1_e2_interval(cfg):
    one_window(cfg)
    conductor(cfg, "s1", [asst("a", T(10, 9), 900_000),                 # before
                          asst("b", T(10, 11), 100_000), asst("c", T(10, 12), 200_000),
                          asst("d", T(10, 13), 300_000),
                          asst("e", T(10, 15), 900_000)])               # after
    rep = MVP.report(cfg, n=1)
    e1, e2 = crit(rep, "E1"), crit(rep, "E2")
    assert e1["value"] == pytest.approx(6.0) and e1["status"] == "fail"
    assert e2["value"] == 200_000 and e2["status"] == "fail"           # hand-computed median
    # an unpriced turn inside the interval makes E1 incomplete
    conductor(cfg, "s2", [asst("f", T(10, 12, 30), 1_000, model="mystery")])
    turns = CC.turns_between(cfg, dt.datetime(2026, 10, 10, 10, tzinfo=UTC),
                             dt.datetime(2026, 10, 10, 14, tzinfo=UTC))
    assert [t["usd"] for t in turns if t["model"] == "mystery"] == [None]
    rep = MVP.report(cfg, n=1)
    assert crit(rep, "E1")["status"] == "incomplete"


def test_e1_e2_passing_and_no_turns(cfg):
    one_window(cfg)
    conductor(cfg, "s1", [asst("a", T(10, 11), 50_000), asst("b", T(10, 12), 150_000)])
    rep = MVP.report(cfg, n=1)
    assert crit(rep, "E1")["value"] == pytest.approx(2.0) and crit(rep, "E1")["status"] == "fail"
    assert crit(rep, "E2")["value"] == 100_000 and crit(rep, "E2")["status"] == "pass"
    camp(cfg, "V", T(12, 10), T(12, 14))
    rep = MVP.report(cfg, n=1)                                         # no turns on day 12
    assert crit(rep, "E2")["status"] == "no_data" and crit(rep, "E2")["value"] is None
    assert crit(rep, "E1")["status"] == "no_data"


# ---- E3 --------------------------------------------------------------------------------------

def deliveries(cfg, rows):
    path = quiet.deliveries_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"ts": ts, "reader": "conductor", "event_id": i,
                                         "type": typ, "campaign": None, "action": act}) + "\n"
                            for i, (ts, typ, act) in enumerate(rows, 1)))


def test_e3_deliveries(cfg):
    one_window(cfg)
    assert crit(MVP.report(cfg, n=1), "E3")["status"] == "no_data"
    deliveries(cfg, [(T(11, 1), "parked", "wake")])                    # only after the window
    assert crit(MVP.report(cfg, n=1), "E3")["status"] == "no_data"
    deliveries(cfg, [(T(9, 1), "parked", "wake"),                       # before: not counted
                     (T(10, 11), "parked", "wake"), (T(10, 11, 5), "merged", "hold"),
                     (T(10, 12), "merged", "hold"), (T(10, 12, 5), "queue_digest", "digest"),
                     (T(10, 13), "code_changed", "wake"), (T(10, 13, 5), "x", "hold"),
                     (T(11, 1), "parked", "wake")])
    e3 = crit(MVP.report(cfg, n=1), "E3")
    assert e3["value"] == 3 and e3["status"] == "pass"
    deliveries(cfg, [(T(9, 1), "parked", "wake")] + [(T(10, 11, i), "parked", "wake")
                                                     for i in range(1, 7)])
    e3 = crit(MVP.report(cfg, n=1), "E3")
    assert e3["value"] == 6 and e3["status"] == "fail"


# ---- E4 --------------------------------------------------------------------------------------

def test_e4_missing_late_covered(cfg):
    camp(cfg, "C", T(10, 10), T(10, 20), events=[
        park_ev(T(10, 10, 10), "a"), out_of_park(T(10, 10, 20)),
        park_ev(T(10, 10, 30), "b"), out_of_park(T(10, 10, 40)),
        park_ev(T(10, 11), "c"), out_of_park(T(10, 11, 10)),
        park_ev(T(10, 12), "d")])
    decide(cfg, 1, "C", T(10, 10, 12))                                  # a: before the exit
    decide(cfg, 2, "C", T(10, 10, 31), by="queue")                      # b: a queue decision
    decide(cfg, 3, "C", T(10, 11, 20))                                  # c: after the resume
    rep = MVP.report(cfg, n=1)                                          # d: never
    e4 = crit(rep, "E4")
    assert e4["value"] == 0.5 and e4["status"] == "fail"
    assert {(p["reason"], p["status"]) for p in rep["parks_undecided"]} == {
        ("c", "late"), ("d", "missing")}
    assert all(p["campaign"] == "C" and p["ts"] for p in rep["parks_undecided"])


def test_e4_still_parked_and_no_parks(cfg):
    camp(cfg, "C", T(10, 10), T(10, 20), events=[park_ev(T(10, 10, 10))])
    decide(cfg, 1, "C", T(10, 12))                                      # no transition out yet
    e4 = crit(MVP.report(cfg, n=1), "E4")
    assert e4["value"] == 1.0 and e4["status"] == "pass"
    camp(cfg, "D", T(11, 10), T(11, 20))
    rep = MVP.report(cfg, n=1)                                          # window: D, no parks
    assert crit(rep, "E4")["status"] == "no_data" and crit(rep, "E4")["value"] is None
    assert rep["parks_undecided"] == []


# ---- E5 --------------------------------------------------------------------------------------

class FakeView:
    def __call__(self, url):
        return {"state": "MERGED", "merged_at": "2026-10-27T10:00:00Z", "merge_sha": "abc"}


def pr_project(cfg, tmp_path, policy="pr"):
    repo = tmp_path / "prrepo"
    (repo / ".wt").mkdir(parents=True, exist_ok=True)
    (repo / ".wt" / "project.yaml").write_text(
        f"schema: wt.project.v1\nproject: PR\nbase: main\nmerge:\n  policy: {policy}\n")
    cfg["outbox_targets"] = {"PR": {"repo_path": str(repo)}}


def test_e5_pr_campaign(cfg, tmp_path):
    pr_project(cfg, tmp_path)
    cid = ev_campaign(cfg, "SPEC-0900-20261025-aaaa", state="HANDED_OFF")
    _set_state(cfg, cid, project="PR", created_at=T(25, 10),
               close={"mode": "pr", "merged_sha": None, "pr_url": "https://gh/p/7"})
    S.append_event(cfg, cid, "transition", {"from": "CLOSING", "to": "HANDED_OFF",
                                            "reason": "pr_opened"})
    assert crit(MVP.report(cfg, n=1), "E5")["status"] == "fail"       # handed off, not merged
    (row,) = Q.sweep_handed_off(cfg, apply=True, view=FakeView())
    assert row["action"] == "merge"
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED" and st["close"]["pr_state"] == "MERGED"
    rep = MVP.report(cfg, n=1)
    assert crit(rep, "E5")["status"] == "pass" and crit(rep, "E5")["value"] == 1
    (c5,) = rep["e5_campaigns"]
    assert (c5["campaign"], c5["project"], c5["spec_id"], c5["pr_url"], c5["outcome"]) == (
        cid, "PR", "SPEC-0900", "https://gh/p/7", "MERGED")
    assert c5["touchpoints"] == [] and c5["status"] == "pass"
    # a human-owned main_dirty park is a touchpoint that fails E5; so is an escalation's question
    S.append_event(cfg, cid, "park", {"reason": "main_dirty", "detail": "d",
                                      "resume_to": "CLOSING"})
    ESC._create(cfg, {"campaign": cid, "question": "scope?"})
    rep = MVP.report(cfg, n=1)
    (c5,) = rep["e5_campaigns"]
    assert crit(rep, "E5")["status"] == "fail" and c5["status"] == "fail"
    assert {"kind": "escalation", "id": "ESC-0001", "question": "scope?"} in c5["touchpoints"]
    assert any(t["kind"] == "park" and t["reason"] == "main_dirty" for t in c5["touchpoints"])


def test_e5_human_decision_park_is_allowed_and_policy_matters(cfg, tmp_path):
    pr_project(cfg, tmp_path)
    kw = dict(project="PR", state="MERGED", close={"pr_state": "MERGED", "pr_url": "u"})
    camp(cfg, "A", T(10, 1), T(10, 2), events=[park_ev(T(10, 1, 30), "human_decision")], **kw)
    rep = MVP.report(cfg, n=1)
    assert crit(rep, "E5")["status"] == "pass"
    assert rep["e5_campaigns"][0]["touchpoints"][0]["reason"] == "human_decision"
    pr_project(cfg, tmp_path, policy="merge")                         # not a PR-policy project
    assert crit(MVP.report(cfg, n=1), "E5")["status"] == "fail"
    pr_project(cfg, tmp_path)
    measurement_specs(cfg, end=T(10, 5))                              # A opened before the point
    assert crit(MVP.report(cfg, n=1), "E5")["status"] == "fail"


# ---- E6 --------------------------------------------------------------------------------------

def undecided_event(monkeypatch, ts):
    monkeypatch.setattr(S, "now_iso", lambda now=None: ts)


def test_e6_compaction_and_restart(cfg, monkeypatch):
    one_window(cfg)
    s1 = [asst(f"m{i}", T(10, 9, i), 1) for i in range(3)]              # idx 0-2, before the window
    s1 += [asst(f"n{i}", T(10, 10, 5 + i), 1) for i in range(3)]       # idx 3-5
    s1 += [compact(T(10, 11))]                                          # next turn is idx 6
    s1 += [asst("p0", T(10, 11, 1), 1), asst("p1", T(10, 11, 2), 1, brief=True)]   # brief at 7
    conductor(cfg, "s1", s1)
    conductor(cfg, "s2", [asst("q0", T(10, 12), 1), asst("q1", T(10, 12, 1), 1)])  # restart
    rep = MVP.report(cfg, n=1)
    ev = rep["e6_events"]
    assert [(e["session_id"], e["kind"], e["oriented"], e["clean"]) for e in ev] == [
        ("s1", "compaction", True, True), ("s2", "restart", False, True)]
    assert crit(rep, "E6")["status"] == "fail"
    # a park_undecided 10 minutes after the oriented event makes it not clean
    undecided_event(monkeypatch, T(10, 11, 10))
    E.append(cfg, "park_undecided", campaign="W",
             data={"reason": "loop_exhausted", "parked_for_s": 600, "owner": "conductor"})
    ev = MVP.report(cfg, n=1)["e6_events"]
    assert ev[0]["clean"] is False and ev[1]["clean"] is True


def test_e6_pass_and_far_brief(cfg):
    one_window(cfg)
    lines = [compact(T(10, 11))] + [asst(f"x{i}", T(10, 11, i + 1), 1) for i in range(9)]
    lines.append(asst("far", T(10, 11, 30), 1, brief=True))             # the 10th turn after: idx 9
    conductor(cfg, "s1", lines)
    rep = MVP.report(cfg, n=1)
    assert [e["oriented"] for e in rep["e6_events"]] == [True]
    assert crit(rep, "E6")["status"] == "pass"
    conductor(cfg, "s3", [compact(T(10, 12)), *[asst(f"y{i}", T(10, 12, i + 1), 1)
                                                   for i in range(10)], asst("late", T(10, 12, 30), 1, brief=True)])
    flags = {e["session_id"]: e["oriented"] for e in MVP.report(cfg, n=1)["e6_events"]}
    assert flags == {"s1": True, "s3": False}                           # 11th turn: too far


def test_e6_clean_upper_bound(cfg, monkeypatch):
    one_window(cfg)
    conductor(cfg, "s1", [compact(T(10, 11)), asst("p", T(10, 11, 1), 1, brief=True)])
    undecided_event(monkeypatch, T(10, 11, 16))
    E.append(cfg, "park_undecided", campaign="W",
             data={"reason": "loop_exhausted", "parked_for_s": 600, "owner": "conductor"})
    assert [e["clean"] for e in MVP.report(cfg, n=1)["e6_events"]] == [True]   # +16 min: later
    undecided_event(monkeypatch, T(10, 11, 15))
    E.append(cfg, "park_undecided", campaign="W2",
             data={"reason": "loop_exhausted", "parked_for_s": 600, "owner": "conductor"})
    assert [e["clean"] for e in MVP.report(cfg, n=1)["e6_events"]] == [False]  # exactly +15


def test_e6_restart_outside_interval_and_early_brief(cfg):
    one_window(cfg)
    conductor(cfg, "s1", [asst("a", T(10, 8), 1), asst("b", T(10, 9), 1)])
    conductor(cfg, "s2", [asst("c", T(10, 9, 30), 1)])                  # first turn before start
    conductor(cfg, "s3", [asst("d", T(10, 15), 1)])                     # first turn after end
    rep = MVP.report(cfg, n=1)
    assert rep["e6_events"] == [] and crit(rep, "E6")["status"] == "no_data"
    # a conductor-brief call before the event does not orient it
    conductor(cfg, "s4", [asst("e", T(10, 9, 45), 1, brief=True), compact(T(10, 11, 30)),
                          asst("f", T(10, 11, 40), 1)])
    ev = MVP.report(cfg, n=1)["e6_events"]
    assert [(e["session_id"], e["oriented"]) for e in ev] == [("s4", False)]


def test_e6_resumed_session_replay(cfg):
    one_window(cfg)
    hist = [asst("a", T(10, 9), 1), asst("b", T(10, 11), 1), compact(T(10, 11, 1)),
            asst("c", T(10, 11, 2), 1, brief=True)]
    conductor(cfg, "s1", hist)
    # `claude --resume`: the new transcript replays the history under the same ids, with the
    # compact_boundary, then continues; the restart is at its first new turn
    conductor(cfg, "s2", hist + [asst("d", T(10, 12), 1), asst("e", T(10, 12, 1), 1, brief=True)])
    ev = MVP.report(cfg, n=1)["e6_events"]
    assert [(e["session_id"], e["kind"], e["ts"], e["oriented"]) for e in ev] == [
        ("s1", "compaction", T(10, 11, 1), True), ("s2", "restart", T(10, 12), True)]
    # resumed again: only the third session's own first turn is its restart
    conductor(cfg, "s3", hist + [asst("d", T(10, 12), 1), asst("e", T(10, 12, 1), 1, brief=True),
                                 asst("g", T(10, 13), 1)])
    ev = MVP.report(cfg, n=1)["e6_events"]
    assert [(e["session_id"], e["kind"], e["ts"]) for e in ev] == [
        ("s1", "compaction", T(10, 11, 1)), ("s2", "restart", T(10, 12)),
        ("s3", "restart", T(10, 13))]


def test_e6_replayed_history_before_interval_is_no_restart(cfg):
    one_window(cfg)
    hist = [asst("a", T(10, 8), 1), asst("b", T(10, 9), 1)]
    conductor(cfg, "s1", hist)
    conductor(cfg, "s2", hist + [asst("c", T(10, 12), 1), asst("d", T(10, 12, 1), 1, brief=True)])
    ev = MVP.report(cfg, n=1)["e6_events"]
    assert [(e["session_id"], e["kind"], e["ts"], e["oriented"]) for e in ev] == [
        ("s2", "restart", T(10, 12), True)]


def test_e6_no_events(cfg):
    one_window(cfg)
    conductor(cfg, "s1", [asst("a", T(10, 11), 1)])
    c6 = crit(MVP.report(cfg, n=1), "E6")
    assert c6["status"] == "no_data"


# ---- CLI -------------------------------------------------------------------------------------

@pytest.fixture
def run(cfg, monkeypatch):
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    return lambda *args: CliRunner().invoke(cli, ["queue", "mvp-report", *args])


def all_pass(cfg, tmp_path):
    pr_project(cfg, tmp_path)
    measurement_specs(cfg)
    camp(cfg, "PRC", T(2, 1), T(2, 2), project="PR", state="MERGED",
         close={"pr_state": "MERGED", "pr_url": "u"})
    camp(cfg, "W", T(10, 10), T(10, 14), events=[park_ev(T(10, 11)), out_of_park(T(10, 11, 30))])
    decide(cfg, 1, "W", T(10, 11, 10))
    conductor(cfg, "s1", [asst("a", T(10, 12), 10_000), compact(T(10, 12, 1)),
                          asst("b", T(10, 12, 2), 10_000, brief=True)])
    deliveries(cfg, [(T(9, 1), "parked", "wake"), (T(10, 12), "parked", "wake")])


def test_cli_text_and_check(cfg, tmp_path, run):
    all_pass(cfg, tmp_path)
    r = run("--window", "1", "--check")
    assert r.exit_code == 0, r.output
    rows = [l for l in r.output.splitlines() if l[:2] in ("E1", "E2", "E3", "E4", "E5", "E6")
            and " pass" in l]
    assert len(rows) == 6, r.output
    j = json.loads(run("--window", "1", "--json", "--check").output)
    assert j["ok"] is True and j["window"]["qualifying"] is True
    assert {c["status"] for c in j["criteria"]} == {"pass"}
    # a failing fixture: --check exits 1, without it the command exits 0
    decide(cfg, 2, "W", T(10, 11, 20))
    camp(cfg, "W2", T(10, 15), T(10, 18), events=[park_ev(T(10, 16))])
    r = run("--window", "2", "--check")
    assert r.exit_code == 1 and "fail" in r.output
    assert run("--window", "2").exit_code == 0
    assert run("--since", "nonsense").exit_code != 0


def test_cli_empty_store_and_since(cfg, run):
    r = run("--check")
    assert r.exit_code == 1 and "no_data" in r.output
    r = run()
    assert r.exit_code == 0, r.output
    j = json.loads(run("--json").output)
    assert j["window"]["start"] is None and j["ok"] is False
    for i, day in enumerate((3, 6, 9), 1):
        camp(cfg, f"C{i}", T(day, 1), T(day, 2))
    j = json.loads(run("--window", "3", "--since", "2026-10-05", "--json").output)
    assert j["window"]["campaigns"] == ["C2", "C3"]


def test_check_needs_a_qualifying_window(cfg, tmp_path, run):
    """Six passing criteria on a window that does not qualify: `ok` is false, `--check` exits 1."""
    all_pass(cfg, tmp_path)
    shutil.rmtree(S.campaign_dir(cfg, "M0301"))  # SPEC-0301 no longer merged
    rep = MVP.report(cfg, n=1)
    assert {c["status"] for c in rep["criteria"]} == {"pass"}
    assert rep["window"]["qualifying"] is False and rep["ok"] is False
    r = run("--window", "1", "--check")
    assert r.exit_code == 1, r.output
    j = json.loads(run("--window", "1", "--json").output)
    assert j["ok"] is False and {c["status"] for c in j["criteria"]} == {"pass"}
