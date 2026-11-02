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
    _seed(cfg, _w("box"), _w("two"), _w("idle"))
    _job(cfg, 1)
    _job(cfg, 2)
    _job(cfg, 3, state="done")
    _job(cfg, 4, worker="two")
    assert "box: draining (2 jobs)" in invoke("drain", "box").output
    assert "idle: draining (0 jobs)" in invoke("drain", "idle").output
    out = invoke("list").output
    assert "draining (2 jobs)" in out and "draining (0 jobs)" in out
    rows = {w["name"]: w for w in json.loads(invoke("list", "--json").output)["workers"]}
    assert rows["box"]["draining"] is True and rows["box"]["draining_jobs"] == 2
    assert rows["idle"]["draining"] is True and rows["idle"]["draining_jobs"] == 0
    assert rows["two"]["draining"] is False and rows["two"]["draining_jobs"] is None


def test_disabled_record_with_stale_draining_lists_disabled(cfg, invoke):
    """A hand-edited `enabled: false, draining: true` record is disabled, not draining."""
    _seed(cfg, _w("box", enabled=False, draining=True))
    _job(cfg, 1)
    assert "draining" not in invoke("list").output
    (row,) = json.loads(invoke("list", "--json").output)["workers"]
    assert row["enabled"] is False and row["draining"] is False
    assert row["draining_jobs"] is None


def test_drain_unknown_exits_2(cfg, invoke):
    _seed(cfg, _w("box"))
    before = WK.path(cfg).read_bytes()
    r = CliRunner().invoke(cli, ["workers", "drain", "nope"])
    assert r.exit_code == 2 and "no worker named nope" in r.stderr
    assert "no worker named nope" not in r.stdout
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


def test_queue_tick_runs_the_drain_pass(cfg, invoke, monkeypatch):
    """The wiring: a real `queue.tick` (not a direct `_finish_worker_drains` call) disables an
    idle draining worker and emits `worker_drained`."""
    _seed(cfg, _w("box"))
    assert invoke("drain", "box").exit_code == 0
    calls = []
    real = Q._finish_worker_drains
    monkeypatch.setattr(Q, "_finish_worker_drains", lambda c: (calls.append(c), real(c)))
    with GS.acquire(cfg, "c-1", pools=[("box", 1)]):
        Q.tick(cfg)                             # the held slot keeps it draining
    assert calls == [cfg]
    assert _raw(cfg)["draining"] is True and _raw(cfg)["enabled"] is True
    Q.tick(cfg)
    assert len(calls) == 2
    assert _raw(cfg)["enabled"] is False and "draining" not in _raw(cfg)
    (ev,) = _drained_events(cfg)
    assert ev["data"] == {"worker": "box"} and ev["by"] == "queue"


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
    seen = {}
    real = RG.run

    def spy(*a, **k):
        seen["out"] = real(*a, **k)
        return seen["out"]
    monkeypatch.setattr(RG, "run", spy)
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    out = seen["out"]
    assert out.run is None and out.failure.stage == "registry"
    assert "draining or disabled" in out.failure.error
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


# ---- review follow-ups: unreadable job records, the locked re-check, the drain race ----------

def _journal_kinds(cfg):
    p = Q.runner_journal_path(cfg)
    return [json.loads(ln)["kind"] for ln in p.read_text().splitlines()] if p.exists() else []


def _assert_skipped(cfg):
    before = WK.path(cfg).read_bytes()
    Q._finish_worker_drains(cfg)
    assert "worker_drain_skipped" in _journal_kinds(cfg)
    assert WK.path(cfg).read_bytes() == before
    assert _raw(cfg)["draining"] is True and _raw(cfg)["enabled"] is True
    assert not _drained_events(cfg)


@pytest.mark.skipif(__import__("os").geteuid() == 0, reason="root reads a mode-000 dir")
def test_unreadable_remote_jobs_dir_skips_the_pass(cfg, invoke):
    _seed(cfg, _w("box"))
    _job(cfg, 1)                                # live, on box; no slot held
    assert invoke("drain", "box").exit_code == 0
    d = RJ.jobs_dir(cfg)
    d.chmod(0)
    try:
        _assert_skipped(cfg)
    finally:
        d.chmod(0o755)
    Q._finish_worker_drains(cfg)                # readable again: the live record still blocks
    assert _raw(cfg)["draining"] is True


@pytest.mark.parametrize("bad", ["truncated", "foreign-schema", "mode-000"])
def test_unusable_job_record_blocks_completion(cfg, invoke, bad):
    """A record file `remote_jobs.read` drops (its job may still run on box) never reads as
    "no record": the pass is skipped until the file is readable again."""
    import os
    if bad == "mode-000" and os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    _seed(cfg, _w("box"))
    assert invoke("drain", "box").exit_code == 0
    p = RJ.path(cfg, _job(cfg, 1))
    if bad == "truncated":
        p.write_text(p.read_text()[:20])
    elif bad == "foreign-schema":
        p.write_text(json.dumps({**json.loads(p.read_text()), "schema": "nw.remote_job.v9"}))
    else:
        p.chmod(0)
    try:
        _assert_skipped(cfg)
    finally:
        p.chmod(0o644)
    p.unlink()                                  # the record is gone: the drain completes
    Q._finish_worker_drains(cfg)
    assert _raw(cfg)["enabled"] is False and len(_drained_events(cfg)) == 1


@pytest.mark.parametrize("worker", ["missing", None, 7])
def test_live_record_without_worker_blocks_completion(cfg, invoke, worker):
    """A live record with no string `worker` may be a job on box: the pass is skipped (and
    journaled) rather than disabling box under it. Once it is final, the drain completes."""
    _seed(cfg, _w("box"))
    assert invoke("drain", "box").exit_code == 0
    job = "c-2-gate-001-a1"
    rec = {"schema": RJ.SCHEMA, "job_id": job, "campaign": "c-2", "state": "submitted",
           "seq_or_run": 1, "attempt": 1}
    if worker != "missing":
        rec["worker"] = worker
    RJ.jobs_dir(cfg).mkdir(parents=True, exist_ok=True)
    RJ.path(cfg, job).write_text(json.dumps(rec))
    _assert_skipped(cfg)
    RJ.update(cfg, job, state="done")           # final: no longer a job anywhere
    Q._finish_worker_drains(cfg)
    assert _raw(cfg)["enabled"] is False and len(_drained_events(cfg)) == 1


def test_non_record_entries_do_not_block_completion(cfg, invoke):
    """Only regular, non-hidden `*.json` files are job records: a directory or a dotfile named
    `*.json` (a stray backup) does not stall the drain."""
    _seed(cfg, _w("box"))
    assert invoke("drain", "box").exit_code == 0
    d = RJ.jobs_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    (d / "old.json").mkdir()
    (d / ".backup.json").write_text("not json")
    Q._finish_worker_drains(cfg)
    assert "worker_drain_skipped" not in _journal_kinds(cfg)
    assert _raw(cfg)["enabled"] is False and len(_drained_events(cfg)) == 1


def test_draining_jobs_keys_are_worker_names(cfg):
    _job(cfg, 1)
    RJ.jobs_dir(cfg).joinpath("c-2-gate-001-a1.json").write_text(json.dumps(
        {"schema": RJ.SCHEMA, "job_id": "c-2-gate-001-a1", "campaign": "c-2",
         "state": "submitted", "seq_or_run": 1, "attempt": 1}))    # no worker field
    assert len(RJ.live(cfg)) == 2
    assert WK.draining_jobs(cfg) == {"box": 1}


def test_finish_drains_disables_in_one_update(cfg, invoke, monkeypatch):
    _seed(cfg, _w("box"), _w("two"), _w("three"))
    assert invoke("drain", "box").exit_code == 0
    assert invoke("drain", "two").exit_code == 0
    calls = []
    real = WK.update

    def spy(cfg_, fn):
        calls.append(fn)
        return real(cfg_, fn)
    monkeypatch.setattr(WK, "update", spy)
    assert WK.finish_drains(cfg) == ["box", "two"]
    assert len(calls) == 1
    for n in ("box", "two"):
        assert _raw(cfg, n)["enabled"] is False and "draining" not in _raw(cfg, n)
    assert _raw(cfg, "three")["enabled"] is True


def test_locked_recheck_finding_nothing_does_not_write(cfg, invoke, monkeypatch):
    """A gate takes box's slot between the unlocked pre-check and the update: the re-check
    inside the lock sees the hold, and workers.json is left byte-for-byte untouched."""
    _seed(cfg, _w("box"))
    assert invoke("drain", "box").exit_code == 0
    before = WK.path(cfg).stat().st_mtime_ns, WK.path(cfg).read_bytes()
    with ExitStack() as hold:
        real = WK.update

        def racing(cfg_, fn):
            hold.enter_context(GS.acquire(cfg, "c-1", pools=[("box", 1)]))
            return real(cfg_, fn)
        monkeypatch.setattr(WK, "update", racing)
        assert WK.finish_drains(cfg) == []
        assert GS.held(cfg, "box")
    assert (WK.path(cfg).stat().st_mtime_ns, WK.path(cfg).read_bytes()) == before
    assert _raw(cfg)["draining"] is True


@pytest.mark.parametrize("change", ["removed", "disabled", "drained"])
def test_drain_changed_under_the_lock_writes_nothing(cfg, monkeypatch, change):
    """`drain`'s first read is stale (box enabled, not draining); the locked re-check sees the
    registry as it is now and writes nothing (`_DrainNoop`)."""
    from nightwright.agentic import cli_workers as CW
    now = {"removed": [_w("two")], "disabled": [_w("box", enabled=False)],
           "drained": [_w("box", draining=True)]}[change]
    _seed(cfg, *now)
    before = WK.path(cfg).read_bytes()
    monkeypatch.setattr(CW, "_load_or_fail", lambda cfg_: [_w("box")])
    r = _invoker(cfg, monkeypatch)("drain", "box")
    expect = {"removed": (2, "no worker named box"),
              "disabled": (0, "box: already disabled; nothing to drain"),
              "drained": (0, "box: already draining (0 jobs)")}[change]
    assert (r.exit_code, expect[1] in r.output) == (expect[0], True)
    assert WK.path(cfg).read_bytes() == before


def test_forced_worker_waiter_drained_mid_wait_falls_back(world, remote, monkeypatch):
    """`nw queue gates --worker box` has only box's pool, so it gets no `skip`. box is drained
    while it waits; it takes the freed slot, the re-check refuses it before anything is sent,
    and the gate runs locally (it never hangs)."""
    ssh, marker = _logging_ssh(world, remote)
    remote.register(ssh=ssh)
    monkeypatch.setattr(GS, "POLL_S", 0.05)
    out = {}
    with ExitStack() as box_hold:
        box_hold.enter_context(GS.acquire(world.cfg, "x-box", pools=[("box", 1)]))
        t = threading.Thread(target=lambda: out.setdefault("run", G.run_all(
            world.project, world.state, world.cfg, seq=1, worker="box")), daemon=True)
        t.start()
        assert until(lambda: GS.waiting(world.cfg, CID) is not None)
        _set_draining(world.cfg)
    join(t)
    run = out["run"]
    assert run.ok and run.worker is None
    assert all(r.cwd == str(world.repo) for r in run.results)
    (ev,) = world.ledger(["worker_failed"])
    assert ev["stage"] == "registry" and ev["cooldown"] is False
    assert not marker.exists()


# ---- review follow-ups (2026-11-02, loop 2) -------------------------------------------------

def test_worker_drained_append_failure_keeps_the_write(cfg, invoke, monkeypatch):
    """`worker_drained` is best-effort: any failure to append it (not only OSError/ValueError)
    is journaled, nothing escapes the tick, and the registry write stands."""
    _seed(cfg, _w("box"))
    assert invoke("drain", "box").exit_code == 0

    def boom(*a, **k):
        raise RuntimeError("events down")
    monkeypatch.setattr(EV, "append", boom)
    Q._finish_worker_drains(cfg)                # never raises
    assert _raw(cfg)["enabled"] is False and "draining" not in _raw(cfg)
    rows = [json.loads(ln) for ln in Q.runner_journal_path(cfg).read_text().splitlines()]
    (row,) = [r for r in rows if r["kind"] == "worker_drained"]
    assert row["worker"] == "box" and row["error"] == "RuntimeError: events down"
    monkeypatch.undo()
    assert not _drained_events(cfg)


def test_worker_without_gate_kind_is_not_placeable(cfg):
    _seed(cfg, _w("box", kinds=["review"]), _w("two"))
    box, two = WK.load(cfg)
    assert box["enabled"] and not box["draining"]
    assert not WK.placeable(box) and WK.placeable(two)
    assert not WK.placeable_by_name(cfg, "box") and WK.placeable_by_name(cfg, "two")
    assert not WK.placeable_by_name(cfg, "nope")
    assert [p for p, _ in WK.gate_pools(cfg)] == ["two", WK.LOCAL]


def test_recheck_refuses_a_worker_without_gate_kind(world, remote, monkeypatch):
    """Step 3: a worker that is enabled and not draining but takes no gates is not placeable
    either: the re-check after the take sends nothing and the gate runs locally."""
    ssh, marker = _logging_ssh(world, remote)
    remote.register(ssh=ssh, kinds=["review"])
    monkeypatch.setattr(RG, "pools_for", lambda *a, **k: [("box", 1), (None, 2)])
    monkeypatch.setattr(WK, "placeable_by_name", lambda cfg, name: True)    # the race
    seen = {}
    real = RG.run

    def spy(*a, **k):
        seen["out"] = real(*a, **k)
        return seen["out"]
    monkeypatch.setattr(RG, "run", spy)
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    out = seen["out"]
    assert out.run is None and out.failure.stage == "registry"
    assert "takes no gates" in out.failure.error and "'gate' in kinds" in out.failure.error
    assert "draining" not in out.failure.error
    assert run.ok and run.worker is None
    (ev,) = world.ledger(["worker_failed"])
    assert ev["stage"] == "registry" and ev["cooldown"] is False
    assert not marker.exists()


def test_record_turning_unusable_under_the_lock_blocks_completion(cfg, invoke, monkeypatch):
    """A job record is readable at the unlocked pre-check but unusable by the time the update
    re-checks inside the registry lock: the pass is skipped and nothing is written."""
    _seed(cfg, _w("box"))
    assert invoke("drain", "box").exit_code == 0
    job = _job(cfg, 1)
    RJ.mark_done(cfg, job)                      # final: the pre-check sees an idle worker
    p = RJ.path(cfg, job)
    real = WK.update

    def racing(cfg_, fn):
        p.write_text(json.dumps({**json.loads(p.read_text()), "state": "running",
                                 "schema": "nw.remote_job.v9"}))
        return real(cfg_, fn)
    monkeypatch.setattr(WK, "update", racing)
    _assert_skipped(cfg)


# ---- review loop 4: malformed records, unknown states, repeated skips --------------------------

def _raw_job(cfg, job, **kw):
    rec = {"schema": RJ.SCHEMA, "job_id": job, "campaign": "c-2", "state": "submitted",
           "worker": "box", "seq_or_run": 1, "attempt": 1, **kw}
    RJ.jobs_dir(cfg).mkdir(parents=True, exist_ok=True)
    RJ.path(cfg, job).write_text(json.dumps(rec))
    return rec


@pytest.mark.parametrize("field", ["seq_or_run", "attempt"])
@pytest.mark.parametrize("value", [[1], {"n": 1}, "x"])
def test_malformed_sort_field_never_escapes_the_tick(cfg, invoke, field, value):
    """A live record with a list, dict or non-numeric string in `seq_or_run` / `attempt` does
    not raise out of the tick, `list` or `drain`: it is still a live job on box, so the drain
    waits for it."""
    _seed(cfg, _w("box"))
    _job(cfg, 1, state="done")
    _raw_job(cfg, "c-2-gate-001-a1", **{field: value})
    r = invoke("drain", "box")
    assert r.exit_code == 0 and "draining (1 jobs)" in r.output
    assert "draining (1 jobs)" in invoke("list").output
    assert json.loads(invoke("list", "--json").output)["workers"][0]["draining_jobs"] == 1
    Q._finish_worker_drains(cfg)                # the tick's pass: nothing escapes
    assert "worker_drain_skipped" not in _journal_kinds(cfg)
    assert _raw(cfg)["draining"] is True and not _drained_events(cfg)
    RJ.update(cfg, "c-2-gate-001-a1", state="done")
    Q._finish_worker_drains(cfg)
    assert _raw(cfg)["enabled"] is False and len(_drained_events(cfg)) == 1


def test_type_error_from_the_pass_is_journaled(cfg, invoke, monkeypatch):
    _seed(cfg, _w("box"))
    assert invoke("drain", "box").exit_code == 0

    def boom(cfg_):
        raise TypeError("bad record")
    monkeypatch.setattr(WK, "finish_drains", boom)
    Q._finish_worker_drains(cfg)                # journaled, never raised
    assert "worker_drain_skipped" in _journal_kinds(cfg)


@pytest.mark.parametrize("state", ["missing", None, "running", 3])
def test_record_with_unknown_state_blocks_completion(cfg, invoke, state):
    """A record whose `state` is missing, null or outside `remote_jobs.STATES` cannot be
    trusted to be final: the pass is skipped rather than disabling box under it."""
    _seed(cfg, _w("box"))
    assert invoke("drain", "box").exit_code == 0
    job = "c-2-gate-001-a1"
    rec = _raw_job(cfg, job)
    if state == "missing":
        rec.pop("state")
    else:
        rec["state"] = state
    RJ.path(cfg, job).write_text(json.dumps(rec))
    _assert_skipped(cfg)
    RJ.update(cfg, job, state="lost")           # a known final state: the drain completes
    Q._finish_worker_drains(cfg)
    assert _raw(cfg)["enabled"] is False and len(_drained_events(cfg)) == 1


def test_repeated_skip_is_journaled_once(cfg):
    """An unreadable registry (reported by ConfigWatch too) journals one
    `worker_drain_skipped` row, not one per tick; a new error, or the same one after a
    clean pass, is journaled again."""
    p = WK.path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    for _ in range(3):
        Q._finish_worker_drains(cfg)
    assert _journal_kinds(cfg).count("worker_drain_skipped") == 1
    p.write_text("[also not a registry")       # a different error: journaled
    Q._finish_worker_drains(cfg)
    assert _journal_kinds(cfg).count("worker_drain_skipped") == 2
    _seed(cfg, _w("box"))                       # a clean pass resets it
    Q._finish_worker_drains(cfg)
    p.write_text("[also not a registry")
    Q._finish_worker_drains(cfg)
    assert _journal_kinds(cfg).count("worker_drain_skipped") == 3
