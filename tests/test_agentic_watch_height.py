"""SPEC-0351: queue watch sizes its bottom sections to the terminal height."""
import io

from rich.console import Console

from nightwright.agentic import cli_queue as CQ
import pytest

pytestmark = pytest.mark.fast


def test_allocate_minimums_and_round_robin():
    counts = {"a": 11, "b": 57, "c": 2}
    mins = {"a": 4, "b": 3, "c": 3}
    assert CQ.allocate_rows(counts, mins, 0) == {"a": 4, "b": 3, "c": 2}
    assert CQ.allocate_rows(counts, mins, 4) == {"a": 6, "b": 5, "c": 2}
    assert CQ.allocate_rows(counts, mins, 500) == counts


def test_allocate_shared_lane_costs_one_line():
    counts = {"x": 10, "y": 10}
    got = CQ.allocate_rows(counts, {"x": 2, "y": 2}, 3, lanes=[["x", "y"]])
    assert got == {"x": 5, "y": 5}


def _payload(tmp_path):
    from test_agentic_dashboard import _campaign
    from test_agentic_e2e import impl_done, make_cfg, make_repo, script
    from nightwright.agentic import queue as Q
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    for i in range(11):
        _campaign(cfg, repo, f"SPEC-0001-q{i}", "QUEUED")
    for i in range(11):
        _campaign(cfg, repo, f"SPEC-0001-p{i}", "PARKED",
                  park={"reason": "rate_limited", "detail": "429", "resume_to": "IMPLEMENTING"})
    for i in range(9):
        _campaign(cfg, repo, f"SPEC-0001-m{i}", "MERGED", updated_min=5 + i)
    return Q.dashboard_payload(cfg, recent=9)


def _render(p, width, height, fill=True):
    c = Console(width=width, file=io.StringIO(), force_terminal=False, color_system=None)
    c.print(CQ._dashboard(p, width, 24, height, fill=fill))
    return c.file.getvalue()


def test_render_heights(tmp_path):
    p = _payload(tmp_path)
    small = _render(p, 100, 30)
    tall = _render(p, 100, 120)
    fixed = _render(p, 100, 120, fill=False)
    assert "more" in small
    assert "more" not in tall
    assert "+7 more" in fixed                       # conductor 4 of 11, as before
    assert len(tall.splitlines()) <= 120
    # too short for the minimums: filling adds nothing over the fixed frame
    assert small == _render(p, 100, 30, fill=False)


