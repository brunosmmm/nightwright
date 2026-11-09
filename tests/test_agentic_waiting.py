"""SPEC-0200: WAITING — a started campaign that needs a slot waits ahead of the not-started
backlog, and resume never runs more campaigns than the profile's slots."""
import json

import pytest
from rich.console import Console

from test_agentic_e2e import (_git, _write, added, impl_done, make_cfg, make_repo, run_cli, script,
                              write_review)
from nightwright.agentic import cli_queue as CQ
from nightwright.agentic import events as E
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S

pytestmark = pytest.mark.fast


@pytest.fixture
def two(tmp_path, monkeypatch):
    """slots = 1; SPEC-0001 and SPEC-0002 queued."""
    repo = make_repo(tmp_path)
    second = _write(repo, "docs/specs/0002-second.md",
                    (repo / "docs/specs/0001-trivial.md").read_text().replace("SPEC-0001", "SPEC-0002"))
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    write_review(cfg, second, "SPEC-0002")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")
    monkeypatch.setattr(Q, "slots_for", lambda cfg, profile_name=None: 1)
    a = added(cfg, monkeypatch, "SPEC-0001")
    b = added(cfg, monkeypatch, "SPEC-0002")
    return cfg, a, b


def _set(cfg, cid, **fields):
    S.write_state(cfg, cid, {**S.read_state(cfg, cid), **fields})


def _started_parked(cfg, cid):
    """A campaign that was opened and worked on, then parked by a human."""
    _set(cfg, cid, state="PARKED",
         park={"reason": "human", "detail": "pause", "resume_to": "IMPLEMENTING"},
         counters={"loops": 1, "runs": 2, "gate_runs": 1, "gate_retries": 0,
                   "triage_retries": 0, "blocked_streak": 0, "close_attempts": 0, "parks": 1},
         pending_brief={"source": "triage", "fix_brief": "fix it", "must_fix": [],
                        "human_notes": []},
         repo={**S.read_state(cfg, cid)["repo"], "head": "abc123"})


def test_resume_with_full_slots_waits_and_keeps_its_work(two, monkeypatch):
    cfg, a, b = two
    _set(cfg, a, state="IMPLEMENTING")                    # the one slot is busy
    _started_parked(cfg, b)
    plan = Q.plan_resume(cfg, b, note="go on")
    assert plan["waits_for_slot"] is True
    Q.apply_resume(cfg, plan)
    st = S.read_state(cfg, b)
    assert st["state"] == "WAITING" and st["waiting"]["resume_to"] == "IMPLEMENTING"
    assert st["park"] is None and st["counters"]["loops"] == 1 and st["repo"]["head"] == "abc123"
    assert st["pending_brief"]["human_notes"] == ["go on"]
    trans = S.read_ledger(cfg, b, types=["transition"])[-1]
    assert (trans["from"], trans["to"], trans["reason"]) == ("PARKED", "WAITING", "resumed_waiting")
    assert sum(1 for s in S.list_campaigns(cfg) if s["state"] in Q.ACTIVE_STATES) == 1


def test_resume_with_a_free_slot_goes_straight_to_its_target(two):
    cfg, a, b = two
    _started_parked(cfg, b)
    plan = Q.plan_resume(cfg, b)
    assert plan["waits_for_slot"] is False
    Q.apply_resume(cfg, plan)
    assert S.read_state(cfg, b)["state"] == "IMPLEMENTING"


def test_resume_to_queued_never_waits(two):
    cfg, a, b = two
    _set(cfg, a, state="IMPLEMENTING")
    _set(cfg, b, state="PARKED", park={"reason": "human", "detail": "x", "resume_to": "QUEUED"})
    Q.apply_resume(cfg, Q.plan_resume(cfg, b))
    assert S.read_state(cfg, b)["state"] == "QUEUED"


def test_waiting_takes_the_free_slot_before_the_backlog_without_reopening(two):
    cfg, a, b = two                                      # a is QUEUED (older), b waits
    _started_parked(cfg, b)
    _set(cfg, b, state="WAITING", park=None,
         waiting={"resume_to": "GATING", "since": S.now_iso(), "reason": "resumed"})
    report = Q.tick(cfg, admit=True)
    steps = {(r["campaign"], r["from"], r["to"]) for r in report.steps}
    assert (b, "WAITING", "GATING") in steps
    # SPEC-0395: the WAITING item resumed into GATING, which holds no profile slot, so the
    # backlog is admitted in the same tick
    assert S.read_state(cfg, a)["state"] != "QUEUED"
    assert "waiting" not in S.read_state(cfg, b)
    assert not any(e["to"] == "OPENING" for e in S.read_ledger(cfg, b, types=["transition"])
                   if e["from"] == "WAITING")


def test_waiting_order_is_priority_then_age(tmp_path, monkeypatch, two):
    cfg, a, b = two
    for cid, pri in ((a, 3), (b, 1)):
        _set(cfg, cid, state="WAITING", priority=pri,
             waiting={"resume_to": "IMPLEMENTING", "since": S.now_iso(), "reason": "resumed"})
    Q.tick(cfg)
    assert S.read_state(cfg, b)["state"] not in ("WAITING", "QUEUED")
    assert S.read_state(cfg, a)["state"] == "WAITING"


def test_no_continue_when_admissions_are_stopped(two):
    cfg, a, b = two
    _set(cfg, b, state="WAITING", waiting={"resume_to": "IMPLEMENTING", "since": S.now_iso(),
                                           "reason": "resumed"})
    Q.tick(cfg, admit=False)
    assert S.read_state(cfg, b)["state"] == "WAITING"


def test_waiting_can_be_parked_abandoned_and_prioritized(two):
    cfg, a, b = two
    _set(cfg, b, state="WAITING", waiting={"resume_to": "GATING", "since": S.now_iso(),
                                           "reason": "resumed"})
    Q.apply_prioritize(cfg, Q.plan_prioritize(cfg, b, 1))
    assert S.read_state(cfg, b)["priority"] == 1
    plan = Q.plan_park(cfg, b, "hold")
    assert plan["mode"] == "immediate" and plan["resume_to"] == "GATING"
    Q.apply_park(cfg, plan)
    assert S.read_state(cfg, b)["state"] == "PARKED"
    _set(cfg, a, state="WAITING", waiting={"resume_to": "IMPLEMENTING", "since": S.now_iso(),
                                           "reason": "resumed"})
    Q.plan_abandon(cfg, a)                               # allowed (no ValueError)


def test_events_listing_and_dashboard_show_waiting(two, monkeypatch):
    cfg, a, b = two
    _set(cfg, a, state="IMPLEMENTING")
    _started_parked(cfg, b)
    Q.apply_resume(cfg, Q.plan_resume(cfg, b))
    E.sync(cfg)
    types = [ev["type"] for ev in E.read_since(cfg, 0)]
    assert "waiting" in types
    r = run_cli(cfg, monkeypatch, ["queue", "list"])
    assert "Waiting for a slot (1)" in r.output and b in r.output, r.output
    p = Q.dashboard_payload(cfg)
    assert [w["campaign"] for w in p["waiting"]] == [b] and p["totals"]["waiting"] == 1
    c = Console(width=120, record=True)
    c.print(CQ._dashboard(p, 120))
    assert "Waiting for a slot (1)" in c.export_text()
    _set(cfg, a, state="MERGED")
    Q.tick(cfg)
    E.sync(cfg)
    admitted = [ev for ev in E.read_since(cfg, 0) if ev["type"] == "admitted"
                and ev["campaign"] == b]
    assert admitted and admitted[-1]["data"].get("from") == "WAITING"
