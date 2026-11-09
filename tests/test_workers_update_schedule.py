"""SPEC-0386: `nw workers update NAME --when-idle` and the queue tick that performs it. Offline:
the registry is under tmp_path (asserted), ssh is SPEC-0385's stub (`test_workers_update_cmd`),
job records are written directly. NEVER the real registry or box."""
import datetime as dt
import json

import pytest
from click.testing import CliRunner

from nightwright.agentic import events as EV
from nightwright.agentic import queue as Q
from nightwright.agentic import quiet as QT
from nightwright.agentic import remote_jobs as RJ
from nightwright.agentic import workers as WK
from nightwright.cli import cli

from test_workers_update_cmd import world  # noqa: F401  (fixture: tmp registry + stub ssh)
from test_remote_gates import _short_runtime_dir  # noqa: F401  (autouse fixture)
from waits import until

pytestmark = pytest.mark.fast


@pytest.fixture(autouse=True)
def _fresh_state():
    Q._UPDATING.clear()
    Q._UPDATE_RETRY.clear()
    yield
    Q._UPDATING.clear()
    Q._UPDATE_RETRY.clear()


def _raw(world):
    assert str(world.st.parent) in str(WK.path(world.cfg))
    return json.loads(WK.path(world.cfg).read_text())["workers"][0]


def _schedule(world):
    r = CliRunner().invoke(cli, ["workers", "update", "box", "--when-idle"])
    assert r.exit_code == 0, r.output
    return r


def _live_job(world):
    RJ.create(world.cfg, job_id=RJ.gate_job_id("c-1", 1), campaign="c-1", worker="box",
              attempt=1, seq=1, submitted_at=0.0, deadline_at=1e10, tier="loop")


def _ssh_calls(world):
    f = world.st / "argv"
    return f.read_text().splitlines() if f.exists() else []


def _events(world, *types):
    return EV.read_since(world.cfg, 0, types=list(types))


def _tick_until_done(world):
    Q._run_worker_updates(world.cfg)
    assert until(lambda: not Q._UPDATING)


def test_when_idle_marks_draining_and_finish_drains_leaves_it(world):
    r = _schedule(world)
    assert "update scheduled" in r.output
    raw = _raw(world)
    assert raw["draining"] is True and raw["update_pending"]["reason"] == "update"
    assert WK.finish_drains(world.cfg) == []          # idle, but not disabled: it is updated
    assert _raw(world).get("enabled") is not False
    assert not WK.placeable(WK.load(world.cfg)[0])
    j = json.loads(CliRunner().invoke(cli, ["workers", "list", "--json"]).output)
    row = j["workers"][0]
    assert row["draining"] and row["updating"]
    t = CliRunner().invoke(cli, ["workers", "list"]).output
    assert "updating" in t and "0 jobs" in t
    assert "already scheduled" in CliRunner().invoke(
        cli, ["workers", "update", "box", "--when-idle"]).output


def test_when_idle_rejects_force_and_on_cancels(world):
    r = CliRunner().invoke(cli, ["workers", "update", "box", "--when-idle", "--force"])
    assert r.exit_code == 2
    _schedule(world)
    assert CliRunner().invoke(cli, ["workers", "on", "box"]).exit_code == 0
    raw = _raw(world)
    assert "draining" not in raw and "update_pending" not in raw


def test_idle_worker_is_updated_and_returned_to_service(world):
    (world.st / "stale").write_text("")
    (world.st / "scenario").write_text("updated")
    _schedule(world)
    _tick_until_done(world)
    raw = _raw(world)
    assert "draining" not in raw and "update_pending" not in raw
    assert raw.get("enabled") is not False and raw["slots"] == 1
    assert raw["update_last"]["state"] == "updated"
    assert WK.placeable(WK.load(world.cfg)[0])
    upd = [c for c in _ssh_calls(world) if '"update"' in c]
    assert upd and "--force" not in upd[0]                     # never forced
    ev = _events(world, "worker_updated")
    assert len(ev) == 1 and ev[0]["data"]["worker"] == "box"
    assert ev[0]["schema"] == "nw.event.v1"


def test_not_idle_waits_and_sends_nothing(world):
    _schedule(world)
    _live_job(world)
    (world.st / "scenario").write_text("updated")
    _tick_until_done(world)
    assert _raw(world)["draining"] is True and _ssh_calls(world) == []


def test_busy_race_stays_draining_and_backs_off(world):
    (world.st / "scenario").write_text("busy")
    _schedule(world)
    _tick_until_done(world)
    assert _raw(world)["draining"] is True
    n = len(_ssh_calls(world))
    assert n >= 1
    _tick_until_done(world)                                    # inside the back-off: no retry
    assert len(_ssh_calls(world)) == n
    assert _events(world, "worker_update_failed") == []


@pytest.mark.parametrize("scn, reason", [("failed", "failed"), ("notallowed", "old")])
def test_failed_or_old_worker_returns_to_service_and_tells_conductor(world, scn, reason):
    (world.st / "scenario").write_text(scn)
    _schedule(world)
    _tick_until_done(world)
    raw = _raw(world)
    assert "draining" not in raw and "update_pending" not in raw and raw.get("enabled") is not False
    assert raw["update_last"]["state"] == reason and raw["update_last"]["detail"]
    ev = _events(world, "worker_update_failed")
    assert len(ev) == 1 and ev[0]["data"]["reason"] == reason
    calls = len(_ssh_calls(world))
    _tick_until_done(world)                                    # no marker: no tight retry
    assert len(_ssh_calls(world)) == calls


def test_timeout_clears_the_drain_and_never_touches_jobs(world):
    world.cfg["agentic"] = {"workers": {"update_drain_timeout_h": 1}}
    _schedule(world)
    _live_job(world)
    old = (dt.datetime.now().astimezone() - dt.timedelta(hours=2)).isoformat()

    def fn(records):
        records[0]["update_pending"]["since"] = old
    WK.update(world.cfg, fn)
    _tick_until_done(world)
    raw = _raw(world)
    assert "draining" not in raw and raw["update_last"]["state"] == "timeout"
    assert RJ.live(world.cfg) and _ssh_calls(world) == []
    ev = _events(world, "worker_update_failed")
    assert ev[0]["data"]["reason"] == "timeout"


def test_config_defaults_events_and_quiet_rules(world):
    assert WK.update_drain_timeout_h({}) == 6.0
    assert WK.update_drain_timeout_h({"agentic": {"workers": {"update_drain_timeout_h": 2}}}) == 2
    assert WK.update_drain_timeout_h({"agentic": {"workers": {"update_drain_timeout_h": -1}}}) == 6.0
    assert WK.auto_update({}) is False
    assert WK.auto_update({"agentic": {"workers": {"auto_update": True}}}) is True
    EV.validate("worker_updated", {"worker": "box", "state": "updated", "build_id": "b1:x"})
    EV.validate("worker_update_failed", {"worker": "box", "reason": "x", "error": "y"})
    assert QT.RULES["worker_updated"] == "hold"


def test_auto_update_schedules_a_stale_worker_on_check(world):
    world.cfg["agentic"] = {"workers": {"auto_update": True}}
    (world.st / "build").write_text("b1:" + "f" * 64)         # differs from this checkout's
    (world.st / "scenario").write_text("updated")
    r = CliRunner().invoke(cli, ["workers", "check", "box"])
    assert "stale" in r.output, r.output
    assert _raw(world)["update_pending"]["reason"] == "update"
