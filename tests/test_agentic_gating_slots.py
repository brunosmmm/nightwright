"""SPEC-0395: GATING campaigns do not occupy a profile slot."""
from test_agentic_waiting import _set, _started_parked, two  # noqa: F401  (fixture)
from nightwright.agentic import events as E
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S
import pytest

pytestmark = pytest.mark.fast


def test_gating_is_active_but_not_a_slot_state():
    assert "GATING" in Q.ACTIVE_STATES and "GATING" not in Q.SLOT_STATES
    assert "IMPLEMENTING" in Q.SLOT_STATES


def test_gating_does_not_count_but_implementing_does(two):
    cfg, a, b = two
    _set(cfg, a, state="GATING")
    assert Q._active_count(cfg) == 0
    _set(cfg, a, state="IMPLEMENTING")
    assert Q._active_count(cfg) == 1


def test_resume_to_gating_never_waits_but_implementing_does(two):
    cfg, a, b = two
    _set(cfg, a, state="IMPLEMENTING")
    _started_parked(cfg, b)
    assert Q.plan_resume(cfg, b, to="GATING")["waits_for_slot"] is False
    assert Q.plan_resume(cfg, b, to="IMPLEMENTING")["waits_for_slot"] is True


def test_occupied_ignores_gating_rows_and_flights():
    rows = [{"id": "A", "state": "GATING"}, {"id": "B", "state": "IMPLEMENTING"}]
    assert E._occupied(rows, {"in_flight": ["A", "C"]}) == 2     # B and C
    assert Q.slot_ids(rows, ["A"]) == {"B"}
