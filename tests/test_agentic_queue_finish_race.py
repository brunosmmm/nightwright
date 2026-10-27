"""SPEC-0254: `_apply.finish` reads, decides and writes the campaign under `item_lock`, and
decides `park_requested` only from the fresh disk copy. The concurrent writers are the real
entry points (`apply_park`, `ESC.apply_escalate`, `ESC.withdraw`), run in a thread that is
started right after `finish`'s `_fresh` read."""
import threading

import pytest

from test_agentic_queue import _rr, opened  # noqa: F401  (fixture)
from wt.agentic import escalations as ESC
from wt.agentic import lease as L
from wt.agentic import queue as Q
from wt.agentic import store as S

PROFILE = {"slots": 2, "loop_max": 3, "campaign_cap_usd": 10.0, "run_timeout_s": 60,
           "personas": {"conformance_reviewer": {"max_budget_usd": 1.5}}}


class _Done:
    returncode, stdout, stderr = 0, "", ""


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(ESC, "_run_notify", lambda argv: _Done())
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/notify-send")
    monkeypatch.setattr(L, "is_held", lambda cfg: True)       # a running loop: parks are deferred


def _park_writer(cfg, cid):
    Q.apply_park(cfg, Q.plan_park(cfg, cid, "stop please"))


def _escalate_writer(cfg, cid):
    ESC.apply_escalate(cfg, ESC.plan_escalate(cfg, cid, question="ship it?", options=["yes", "no"],
                                              recommend=1))


def _reviewing(cfg, cid):
    """A malformed review keeps the campaign in REVIEWING, so the escalation plan stays valid
    across finish's write and the writer's own outcome is the thing under test."""
    st = dict(S.read_state(cfg, cid), state="REVIEWING")
    S.write_state(cfg, cid, st)
    return st


def _step(cid, state):
    return Q.StepResult(cid, Q.Step("review", persona="conformance_reviewer"), state["state"],
                        run_id="002-conformance_reviewer", run_result=_rr(None, ok=False),
                        budget=1.5)


@pytest.mark.parametrize("writer, reason", [(_park_writer, "human"),
                                            (_escalate_writer, "human_decision")])
def test_request_written_during_finish_survives_and_the_next_tick_sees_it(
        opened, monkeypatch, writer, reason):                                   # noqa: F811
    cfg, cid, ctx = opened
    state = _reviewing(cfg, cid)
    real_fresh, seen = Q._fresh, {}

    def shim(c, st):
        current = real_fresh(c, st)
        errors = []

        def run():
            try:
                writer(cfg, cid)
            except BaseException as e:               # surfaced below, not lost in the thread
                errors.append(e)
        t = threading.Thread(target=run)
        t.start()
        t.join(timeout=0.5)
        seen.update(thread=t, errors=errors, alive=t.is_alive())    # alive: blocked on item_lock
        return current

    monkeypatch.setattr(Q, "_fresh", shim)
    row = Q._apply(ctx, state, _step(cid, state))
    seen["thread"].join(timeout=10)
    assert seen["alive"], "the writer completed inside finish: item_lock was not held"
    assert not seen["errors"] and not seen["thread"].is_alive()
    assert row["to"] == "REVIEWING" and row["reason"] == "review_retry"   # this tick missed it
    after = S.read_state(cfg, cid)
    assert after["state"] == "REVIEWING" and after["park_requested"]["reason"] == reason
    assert after["counters"]["review_retries"] == 1     # finish's own write is intact
    assert Q.plan_step(after, PROFILE).kind == "park"   # the next tick parks on it


def test_request_cleared_on_disk_before_the_read_is_not_resurrected(opened):    # noqa: F811
    cfg, cid, ctx = opened
    _reviewing(cfg, cid)
    _escalate_writer(cfg, cid)
    state = S.read_state(cfg, cid)                     # tick-start copy still carries the request
    assert state["park_requested"]["reason"] == "human_decision"
    rec = ESC.open_for(cfg, cid) or ESC._open_records(cfg, cid)[0]
    ESC.withdraw(cfg, rec["id"], "no longer needed", "conductor")
    assert "park_requested" not in S.read_state(cfg, cid)
    row = Q._apply(ctx, state, _step(cid, state))
    assert row["to"] == "REVIEWING" and row["reason"] == "review_retry"
    assert "park_requested" not in S.read_state(cfg, cid)
