"""SPEC-0343: `nw workers drain`. Placement skips a draining worker (`gate_pools`, the waiter's
`skip`, the re-check after the take), jobs already on it finish, and the queue tick disables it
once no live job record names it and no slot of its pool is held. Offline: registry and
`remote-jobs` records written directly, slot holds taken in the test process, and SPEC-0338's
loopback worker (`test_remote_gates.Remote`) for the gate runs."""
import json
import threading
from contextlib import ExitStack

import pytest
from click.testing import CliRunner

from nightwright.agentic import events as EV
from nightwright.agentic import gate_slots as GS
from nightwright.agentic import gates as G
from nightwright.agentic import queue as Q
from nightwright.agentic import remote_gates as RG
from nightwright.agentic import remote_jobs as RJ
from nightwright.agentic import workers as WK
from nightwright.cli import cli

from test_remote_gates import CID, Remote, World, _script
from test_remote_gates import _short_runtime_dir  # noqa: F401  (autouse fixture)
from test_remote_job_records import ASYNC_INFO, AW, JOB, _detached, _gate_and_apply, \
    _gate_names_frames
from waits import join, until


@pytest.fixture
def cfg(tmp_path):
    return {"data_dir": str(tmp_path / "data"), "agentic": {"gates": {"slots": 1}}}


def _seed(cfg, *records):
    p = WK.path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema": WK.SCHEMA, "workers": list(records)}))


def _w(name="box", **kw):
    return {"name": name, "ssh": f"me@{name}", "slots": 1, "kinds": ["gate"], "nw": "nw",
            "enabled": True, **kw}


def _raw(cfg, name="box"):
    return next(r for r in json.loads(WK.path(cfg).read_text())["workers"] if r["name"] == name)


def _job(cfg, n, worker="box", state="submitted", campaign="c-1"):
    job_id = RJ.gate_job_id(campaign, n)
    RJ.create(cfg, job_id=job_id, campaign=campaign, worker=worker, attempt=1, seq=n,
              submitted_at=0.0, deadline_at=1e10, tier="loop")
    if state != "submitted":
        RJ.update(cfg, job_id, state=state)
    return job_id


def _invoker(cfg, monkeypatch):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)

    def run(*args):
        return CliRunner().invoke(cli, ["workers", *args])
    return run


@pytest.fixture
def invoke(cfg, monkeypatch):
    return _invoker(cfg, monkeypatch)


def _drained_events(cfg):
    return [e for e in EV.read_since(cfg, 0, types=["worker_drained"])]


# ---- registry, CLI ---------------------------------------------------------------------------

def test_drain_excludes_from_gate_pools(cfg, invoke):
    _seed(cfg, _w("box"), _w("two"))
    assert WK.gate_pools(cfg) == [("box", 1), ("two", 1), (None, 1)]
    r = invoke("drain", "box")
    assert r.exit_code == 0 and "box: draining (0 jobs)" in r.output
    assert _raw(cfg)["draining"] is True and _raw(cfg)["enabled"] is True
    assert WK.gate_pools(cfg) == [("two", 1), (None, 1)]
    assert not WK.placeable_by_name(cfg, "box") and WK.placeable_by_name(cfg, "two")
    assert not WK.placeable_by_name(cfg, "nope")


def test_registry_without_draining_loads_false(cfg, invoke):
    _seed(cfg, _w("old"))                       # a SPEC-0337-era record
    (w,) = WK.load(cfg)
    assert w["draining"] is False and WK.placeable(w)
    assert invoke("add", "box", "--ssh", "me@box", "--no-check").exit_code == 0
    assert "draining" not in _raw(cfg, "box") and "draining" not in _raw(cfg, "old")


def test_list_shows_draining_count(cfg, invoke):
    _seed(cfg, _w("box"), _w("two"))
    _job(cfg, 1)
    _job(cfg, 2)
    _job(cfg, 3, state="done")
    _job(cfg, 4, worker="two")
    assert "box: draining (2 jobs)" in invoke("drain", "box").output
    out = invoke("list").output
    assert "draining (2 jobs)" in out
    rows = {w["name"]: w for w in json.loads(invoke("list", "--json").output)["workers"]}
    assert rows["box"]["draining"] is True and rows["box"]["draining_jobs"] == 2
    assert rows["two"]["draining"] is False and rows["two"]["draining_jobs"] is None


def test_drain_unknown_exits_2(cfg, invoke):
    _seed(cfg, _w("box"))
    before = WK.path(cfg).read_bytes()
    r = CliRunner().invoke(cli, ["workers", "drain", "nope"])
    assert r.exit_code == 2 and "no worker named nope" in r.output
    assert WK.path(cfg).read_bytes() == before


def test_drain_disabled_is_noop(cfg, invoke):
    _seed(cfg, _w("box", enabled=False))
    before = WK.path(cfg).read_bytes()
    r = invoke("drain", "box")
    assert r.exit_code == 0 and "box: already disabled; nothing to drain" in r.output
    assert WK.path(cfg).read_bytes() == before


def test_drain_twice_is_noop(cfg, invoke):
    _seed(cfg, _w("box"))
    _job(cfg, 1)
    assert invoke("drain", "box").exit_code == 0
    before = WK.path(cfg).read_bytes()
    r = invoke("drain", "box")
    assert r.exit_code == 0 and "box: already draining (1 jobs)" in r.output
    assert WK.path(cfg).read_bytes() == before


def test_drain_help_mentions_runner(invoke):
    r = invoke("drain", "--help")
    text = " ".join(r.output.split())
    assert r.exit_code == 0
    assert "stays draining until a queue tick sees it idle and disables it" in text
    assert "If no runner is ticking, it stays draining until one runs." in text


def test_on_off_clear_draining(cfg, invoke):
    _seed(cfg, _w("box"))
    assert invoke("drain", "box").exit_code == 0
    assert invoke("on", "box").exit_code == 0
    assert _raw(cfg)["enabled"] is True and "draining" not in _raw(cfg)
    assert invoke("drain", "box").exit_code == 0
    assert invoke("off", "box").exit_code == 0
    assert _raw(cfg)["enabled"] is False and "draining" not in _raw(cfg)


# ---- drain completion ------------------------------------------------------------------------

def test_held_slot_without_record_blocks_completion(cfg, invoke):
    _seed(cfg, _w("box"))
    assert invoke("drain", "box").exit_code == 0
    with GS.acquire(cfg, "c-1", pools=[("box", 1)]) as hold:    # between acquire and submit
        assert hold.pool == "box"
        assert WK.finish_drains(cfg) == []
        assert _raw(cfg)["draining"] is True and _raw(cfg)["enabled"] is True
        assert "draining (0 jobs)" in invoke("list").output     # a held slot is not a job
    Q._finish_worker_drains(cfg)
    assert _raw(cfg)["enabled"] is False and "draining" not in _raw(cfg)
    (ev,) = _drained_events(cfg)
    assert ev["data"] == {"worker": "box"} and ev["by"] == "queue"
    Q._finish_worker_drains(cfg)                # nothing left to drain: no second event
    assert len(_drained_events(cfg)) == 1


def test_live_record_blocks_completion(cfg, invoke):
    _seed(cfg, _w("box"), _w("two"))
    job = _job(cfg, 1)
    _job(cfg, 2, worker="two")                  # another worker's job does not count
    assert invoke("drain", "box").exit_code == 0
    assert WK.finish_drains(cfg) == []
    assert _raw(cfg)["draining"] is True and _raw(cfg)["enabled"] is True
    RJ.mark_done(cfg, job)
    assert WK.finish_drains(cfg) == ["box"]
    assert _raw(cfg)["enabled"] is False and "draining" not in _raw(cfg)
    assert _raw(cfg, "two")["enabled"] is True


def test_finish_drains_does_not_write_without_a_drain(cfg):
    _seed(cfg, _w("box"))
    before = WK.path(cfg).stat().st_mtime_ns, WK.path(cfg).read_bytes()
    assert WK.finish_drains(cfg) == []
    assert (WK.path(cfg).stat().st_mtime_ns, WK.path(cfg).read_bytes()) == before


def test_unreadable_registry_skips_the_pass(cfg):
    p = WK.path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    Q._finish_worker_drains(cfg)                # journaled, never raised
    assert p.read_text() == "{not json"
    rows = [json.loads(ln) for ln in Q.runner_journal_path(cfg).read_text().splitlines()]
    assert any(r["kind"] == "worker_drain_skipped" for r in rows)


def test_worker_drained_is_a_held_queue_event():
    from nightwright.agentic import quiet as QT
    EV.validate("worker_drained", {"worker": "box"})
    assert QT.RULES["worker_drained"] == "hold"


# ---- placement through gates.run_all (loopback worker) ---------------------------------------

@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.fixture
def remote(world):
    r = Remote(world)
    r.register()
    return r


def _set_draining(cfg, name="box"):
    def fn(records):
        next(r for r in records if r["name"] == name)["draining"] = True
    WK.update(cfg, fn)


def _logging_ssh(world, remote):
    """The loopback ssh, plus a marker file per call: proves nothing was sent."""
    marker = world.tmp / "ssh-called"
    return _script(world.tmp / "ssh-log", f'touch {marker}\nexec {remote.ssh} "$@"'), marker


def test_waiter_started_before_drain_skips_worker(world, remote, monkeypatch):
    """The waiter's pool list has `box` (it started before the drain). Both pools are held;
    once `box` is drained and its slot freed, the waiter still takes only the local pool."""
    ssh, marker = _logging_ssh(world, remote)
    remote.register(ssh=ssh)
    checks = []
    real = WK.placeable_by_name

    def counted(cfg, name):
        checks.append(name)
        return real(cfg, name)
    monkeypatch.setattr(WK, "placeable_by_name", counted)
    monkeypatch.setattr(GS, "POLL_S", 0.05)
    out = {}
    with ExitStack() as box_hold, ExitStack() as local_hold:
        box_hold.enter_context(GS.acquire(world.cfg, "x-box", pools=[("box", 1)]))
        for i in range(2):                      # World's local pool has 2 slots
            local_hold.enter_context(GS.acquire(world.cfg, f"x-l{i}"))
        t = threading.Thread(target=lambda: out.setdefault(
            "run", G.run_all(world.project, world.state, world.cfg, seq=1)), daemon=True)
        t.start()
        assert until(lambda: GS.waiting(world.cfg, CID) is not None)
        _set_draining(world.cfg)
        box_hold.close()                        # box's slot is free, but box is draining
        n = len(checks)
        assert until(lambda: len(checks) >= n + 3)  # the waiter polled box and passed it over
        assert "run" not in out and GS.holding(world.cfg, CID) is None
    join(t)
    run = out["run"]
    assert run.ok and run.worker is None
    assert all(r.cwd == str(world.repo) for r in run.results)
    assert not marker.exists() and not world.ledger(["worker_failed"])


def test_recheck_after_take_falls_back(world, remote, monkeypatch):
    """The waiter took `box`'s slot in the race (its `skip` saw it placeable); the worker is
    draining by the time `remote_gates.run` re-reads the record: nothing is sent, the gate runs
    locally, no cooldown."""
    ssh, marker = _logging_ssh(world, remote)
    remote.register(ssh=ssh)
    _set_draining(world.cfg)
    monkeypatch.setattr(RG, "pools_for", lambda *a, **k: [("box", 1), (None, 2)])
    monkeypatch.setattr(WK, "placeable_by_name", lambda cfg, name: True)    # skip -> False
    seen = {}
    real = RG.run

    def spy(*a, **k):
        seen["out"] = real(*a, **k)
        return seen["out"]
    monkeypatch.setattr(RG, "run", spy)
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    out = seen["out"]
    assert out.run is None and out.failure.stage == "registry" and not out.failure.cooldown
    assert "draining or disabled" in out.failure.error
    assert run.ok and run.worker is None
    assert all(r.cwd == str(world.repo) for r in run.results)
    (ev,) = world.ledger(["worker_failed"])
    assert ev["stage"] == "registry" and ev["cooldown"] is False
    assert remote.record()["cooldown_until"] is None
    assert not marker.exists()


def test_off_worker_taken_in_the_race_falls_back(world, remote, monkeypatch):
    """Rollout 2: a waiter that lands on a worker disabled with `off` runs locally too."""
    remote.register(enabled=False)
    monkeypatch.setattr(RG, "pools_for", lambda *a, **k: [("box", 1), (None, 2)])
    monkeypatch.setattr(WK, "placeable_by_name", lambda cfg, name: True)    # the race
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    assert run.ok and run.worker is None
    (ev,) = world.ledger(["worker_failed"])
    assert ev["stage"] == "registry"


def test_queue_gates_worker_refuses_draining(world, remote):
    _set_draining(world.cfg)
    with pytest.raises(ValueError, match="is unknown, disabled or draining"):
        RG.pools_for(world.cfg, world.project, world.state, seq=1, worker="box")


# ---- jobs already on the worker finish there (SPEC-0341 loopback, async) ---------------------

@pytest.fixture
def aw(tmp_path):
    w = AW(tmp_path)
    yield w
    w.release()


@pytest.fixture
def aremote(aw):
    r = Remote(aw)
    r.register(info=ASYNC_INFO)
    aw._remote = r
    return r


def test_running_job_finishes_on_the_draining_worker(aw, aremote):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", _gate_and_apply(aw)), daemon=True)
    t.start()
    try:
        assert until(lambda: _gate_names_frames(aremote) == ["g1", "g2"])
        _set_draining(aw.cfg)
        assert WK.finish_drains(aw.cfg) == []   # a live record and a held slot
        assert _raw(aw.cfg)["draining"] is True
    finally:
        aw.release()
    join(t)
    res, _row = out["r"]
    assert res.gate_run.ok and res.gate_run.worker == "box"
    assert RJ.read(aw.cfg, JOB)["state"] == "done"
    assert WK.finish_drains(aw.cfg) == ["box"]
    assert _raw(aw.cfg)["enabled"] is False


def test_adopted_job_finishes_on_the_draining_worker(aw, aremote):
    """A coordinator restart leaves a detached job on `box`; `box` is drained; the next gate
    step adopts the job there, and the drain completes only after it is done."""
    ctx, st, res = _detached(aw, "stop")
    Q._apply(ctx, st, res)
    assert RJ.read(aw.cfg, JOB)["state"] == "detached"
    _set_draining(aw.cfg)
    assert WK.finish_drains(aw.cfg) == []       # the live record keeps it draining
    G.reset()                                   # the next runner
    aw.release()
    res2, row2 = _gate_and_apply(aw)
    assert res2.gate_run.ok and res2.gate_run.worker == "box" and row2["to"] == "REVIEWING"
    assert [r.name for r in res2.gate_run.results] == ["g1", "g2", "g3"]
    assert RJ.read(aw.cfg, JOB)["state"] == "done"
    assert not aw.ledger(["worker_failed"]) and not aw.ledger(["worker_job_lost"])
    assert _raw(aw.cfg)["draining"] is True     # until the tick sees it idle
    Q._finish_worker_drains(aw.cfg)
    assert _raw(aw.cfg)["enabled"] is False and "draining" not in _raw(aw.cfg)
    (ev,) = _drained_events(aw.cfg)
    assert ev["data"]["worker"] == "box"
