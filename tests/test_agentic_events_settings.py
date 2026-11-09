"""SPEC-0248: `events.settings` coerces every numeric `agentic.events` key, falling back to its
default, so a malformed threshold cannot crash the listing, the dashboard or the watchdog."""
import json

import pytest

from test_agentic_dashboard import _campaign, _fixture, ev_at
from test_agentic_e2e import run_cli
from nightwright.agentic import events as E
from nightwright.agentic import queue as Q

pytestmark = pytest.mark.fast

BAD = ["soon", True, False, -5, None, [1], {"a": 1}, "nan", "inf", float("nan"), 10**400]


def _cfg(events):
    return {"agentic": {"events": events}}


@pytest.mark.parametrize("bad", BAD)
def test_bad_values_fall_back_to_the_default(bad):
    s = E.settings(_cfg({"stuck_after_s": bad, "cost_warning_ratio": bad, "poll_s": bad}))
    assert s["stuck_after_s"] == E.DEFAULTS["stuck_after_s"] == 900          # an int key
    assert s["cost_warning_ratio"] == E.DEFAULTS["cost_warning_ratio"] == 0.8  # a float key
    assert s["poll_s"] == 2.0


@pytest.mark.parametrize("value,want", [(30, 30), ("30", 30), (30.0, 30), ("30.0", 30),
                                        (12.5, 12.5), ("12.5", 12.5), (0, 0)])
def test_good_int_values_are_kept(value, want):
    got = E.settings(_cfg({"stuck_after_s": value}))["stuck_after_s"]
    assert got == want and type(got) is type(want)


@pytest.mark.parametrize("value,want", [(0.5, 0.5), ("0.5", 0.5), (1, 1.0), ("1", 1.0)])
def test_good_float_values_are_kept(value, want):
    got = E.settings(_cfg({"cost_warning_ratio": value}))["cost_warning_ratio"]
    assert got == want and isinstance(got, float)


def test_defaults_and_unknown_keys():
    assert E.settings({}) == E.DEFAULTS
    assert E.settings(_cfg(None)) == E.DEFAULTS
    s = E.settings(_cfg({"custom": "x", "other": -1}))
    assert s["custom"] == "x" and s["other"] == -1          # unknown keys pass through


def _parked(tmp_path, decide):
    repo, cfg, _ = _fixture(tmp_path)
    cid = "SPEC-0001-ovr"
    _campaign(cfg, repo, cid, "PARKED", park={"reason": "base_moving", "detail": "d",
                                               "resume_to": "GATING"})
    ev_at(cfg, cid, "park", {"reason": "base_moving"}, ago_s=310)    # past the 300 s default
    cfg["agentic"] = {**cfg.get("agentic", {}), "events": {"park_decide_s": decide}}
    return cfg, cid


@pytest.mark.parametrize("bad", ["soon", True, -5])
def test_bad_park_decide_s_uses_the_default_everywhere(tmp_path, monkeypatch, bad):
    cfg, cid = _parked(tmp_path, bad)
    assert E.settings(cfg)["park_decide_s"] == 300
    (item,) = [i for i in Q.queue_payload(cfg)["items"] if i["campaign"] == cid]
    assert item["park"]["owner"] == "conductor" and item["park"]["overdue"] is True
    d = Q.dashboard_payload(cfg)
    assert d["schema"] == "nw.queue.dashboard.v1"
    assert E.watchdog_checks(cfg) is not None
    for args in (["queue", "list"], ["queue", "list", "--json"], ["queue", "watch", "--once"]):
        r = run_cli(cfg, monkeypatch, args)
        assert r.exit_code == 0 and r.exception is None, (args, r.output, r.exception)
    r = run_cli(cfg, monkeypatch, ["queue", "list", "--json"])
    (item,) = [i for i in json.loads(r.output)["items"] if i["campaign"] == cid]
    assert item["park"]["overdue"] is True


def test_numeric_string_park_decide_s_is_kept(tmp_path):
    cfg, cid = _parked(tmp_path, "600")
    assert E.settings(cfg)["park_decide_s"] == 600
    (item,) = [i for i in Q.queue_payload(cfg)["items"] if i["campaign"] == cid]
    assert item["park"]["overdue"] is False                  # 310 s < 600 s

