"""SPEC-0302: the pure rules that let the queue decide routine parks (`autopark`)."""
import datetime as dt

import pytest

from nightwright.agentic import autopark as AP

NOW = dt.datetime(2026, 10, 30, 12, 0, 0, tzinfo=dt.timezone.utc)
SETT = dict(AP.DEFAULTS)


def _state(reason="base_moving", counts=None, total=8.0, cap=10.0):
    return {"id": "C1", "park": {"reason": reason}, "counters": {"auto_resumes": counts or {}},
            "cost": {"total_usd": total, "cap_usd": cap}}


def _end(seq, charged=4.0, *, estimated=True, tail="API Error: 529 overloaded", duration=1.0,
         usage=None):
    return {"type": "run.end", "seq": seq, "charged_usd": charged, "cost_estimated": estimated,
            "result_tail": tail, "duration_s": duration, "usage": usage or {}}


def _secs(auto):
    return (dt.datetime.fromisoformat(auto.resume_at) - NOW).total_seconds()


@pytest.mark.parametrize("n,secs", [(0, 60), (1, 120), (2, 240)])
def test_decide_base_moving_backoff(n, secs):
    auto = AP.decide(_state(counts={"parked.base_moving": n}), [], SETT, NOW)
    assert auto.rule == "parked.base_moving" and auto.kind == "wait" and auto.n == n
    assert _secs(auto) == secs and auto.raise_cap is None


def test_decide_base_moving_limit_and_other_rule_counts():
    assert AP.decide(_state(counts={"parked.base_moving": 3}), [], SETT, NOW) is None
    auto = AP.decide(_state(counts={"parked.cost_cap": 2}), [], SETT, NOW)
    assert auto.n == 0 and _secs(auto) == 60


def test_base_moving_backoff_is_clamped_to_six_hours():
    auto = AP.decide(_state(counts={"parked.base_moving": 2}), [],
                     {**SETT, "base_moving_backoff_s": 20000, "max_per_campaign": 5}, NOW)
    assert _secs(auto) == AP.MAX_RESET_WAIT_S == 6 * 3600
    auto = AP.decide(_state(counts={"parked.base_moving": 12}), [],
                     {**SETT, "max_per_campaign": 20}, NOW)
    assert _secs(auto) == 6 * 3600


def test_phantom_usd():
    ledger = [_end(1, 3.0),                                    # before the resume
              {"type": "resume", "seq": 2},
              _end(3, 4.0), _end(4, 4.0, tail="usage_limit reached"),
              _end(5, 4.0, tail="tests failed"),                # not transient
              _end(6, 4.0, estimated=False),                    # reported a cost
              _end(7, 4.0, duration=1200, usage={"output_tokens": 900})]   # did work
    assert AP.phantom_usd(ledger) == 8.0
    assert AP.phantom_usd(ledger, since_seq=0) == 11.0
    # long with zero output tokens is not phantom either: the duration decides
    assert AP.phantom_usd([_end(1, 2.0, duration=1200, usage={"output_tokens": 0})]) == 0.0
    # tokens but short: below `phantom_max_duration_s`
    assert AP.phantom_usd([_end(1, 2.0, duration=30, usage={"output_tokens": 5})]) == 2.0
    # the `phantom_max_duration_s` boundary is inclusive; no duration is not phantom
    assert AP.phantom_usd([_end(1, 2.0, duration=120)]) == 2.0
    assert AP.phantom_usd([_end(1, 2.0, duration=120.5)]) == 0.0
    assert AP.phantom_usd([_end(1, 2.0, duration=None)]) == 0.0


def test_long_crashed_run_is_not_phantom():
    """What the runner really writes for a 40-minute run killed by a 529: no usage, no cost."""
    end = {"type": "run.end", "seq": 1, "usage": {}, "cost_usd": None, "cost_estimated": True,
           "charged_usd": 4.0, "duration_s": 2400, "result_tail": "API Error: 529 overloaded"}
    assert AP.phantom_usd([end]) == 0.0
    assert AP.decide(_state("cost_cap", total=8.0), [end], SETT, NOW, budget=4.0) is None
    short = dict(end, duration_s=3)
    assert AP.phantom_usd([short]) == 4.0


@pytest.mark.parametrize("ledger,total,expect", [
    ([_end(1), _end(2)], 8.0, 18.0),                           # all phantom
    ([_end(1)], 8.0, 14.0),                                    # phantom 4: 8-4+4 fits the cap
    ([_end(1)], 12.0, None),                                   # phantom does not explain the block
    ([_end(1, 4.0, estimated=False)], 8.0, None),              # real spend
    ([], 8.0, None),
])
def test_decide_cost_cap_phantom_only(ledger, total, expect):
    auto = AP.decide(_state("cost_cap", total=total), ledger, SETT, NOW, budget=4.0)
    assert (auto.raise_cap if auto else None) == expect
    if auto:
        assert auto.rule == "parked.cost_cap" and _secs(auto) == 900


def test_decide_cost_cap_needs_budget_and_limit():
    led = [_end(1), _end(2)]
    assert AP.decide(_state("cost_cap"), led, SETT, NOW) is None
    st = _state("cost_cap", counts={"parked.cost_cap": 3})
    assert AP.decide(st, led, SETT, NOW, budget=4.0) is None


def test_decide_other_reason_is_none():
    assert AP.decide(_state("loop_exhausted"), [], SETT, NOW, budget=4.0) is None


def test_reset_time_parsing():
    epoch = int((NOW + dt.timedelta(hours=2)).timestamp())
    assert AP.reset_time(f"limit reached, resets at {epoch}", NOW) == NOW + dt.timedelta(hours=2)
    iso = (NOW + dt.timedelta(hours=1)).isoformat()
    assert AP.reset_time(f"retry after {iso}", NOW) == NOW + dt.timedelta(hours=1)
    assert AP.reset_time("overloaded", NOW) is None
    assert AP.reset_time(f"error at {iso} while writing the cache", NOW) is None   # not a reset
    assert AP.reset_time(f"resets at {epoch - 86400}", NOW) is None       # already past
    led = [_end(1, tail=f"usage_limit: resets at {epoch}")]
    auto = AP.decide(_state("cost_cap", total=4.0), led, SETT, NOW, budget=4.0)
    assert _secs(auto) == 7200
    far = int((NOW + dt.timedelta(hours=10)).timestamp())
    led = [_end(1, tail=f"usage_limit: resets at {far}")]
    assert _secs(AP.decide(_state("cost_cap", total=4.0), led, SETT, NOW, budget=4.0)) == 6 * 3600
    led = [_end(1, tail="overloaded, resets at soon")]
    assert _secs(AP.decide(_state("cost_cap", total=4.0), led, SETT, NOW, budget=4.0)) == 900


def test_settings_fallback():
    cfg = {"agentic": {"conductor": {"auto": {
        "enabled": "yes", "max_per_campaign": -1, "base_moving_backoff_s": True,
        "transient_backoff_s": 30, "phantom_max_duration_s": "x"}}}}
    s = AP.settings(cfg)
    assert s["enabled"] is True and s["max_per_campaign"] == 3
    assert s["base_moving_backoff_s"] == 60 and s["transient_backoff_s"] == 30
    assert s["phantom_max_duration_s"] == 120
    off = AP.settings({"agentic": {"conductor": {"auto": {"enabled": False}}}})
    assert AP.decide(_state(), [], off, NOW) is None
    assert AP.settings({}) == AP.DEFAULTS


def test_due_and_has_wait_note():
    past = (NOW - dt.timedelta(seconds=5)).isoformat()
    future = (NOW + dt.timedelta(seconds=5)).isoformat()
    mk = lambda at, state="PARKED": {"state": state, "park": {"auto": {"resume_at": at}}}  # noqa: E731
    got = AP.due([mk(past), mk(future), mk(past, "CLOSING"), {"state": "PARKED", "park": {}}], NOW)
    assert got == [mk(past)]
    note = {"type": "note", "seq": 3, "decision": {"kind": "wait", "rule": "parked.base_moving"}}
    assert AP.has_wait_note([{"type": "park", "seq": 2}, note], "parked.base_moving")
    assert not AP.has_wait_note([note, {"type": "park", "seq": 4}], "parked.base_moving")
    assert not AP.has_wait_note([{"type": "park", "seq": 2}, note], "parked.cost_cap")
