"""SPEC-0493: the runner starts cadence jobs (`cadence.pick` / `cadence.run`, the
`queue._sentinel_step` scan and harvest, `_red_main_step` for cadence projects) and
`nw gate cadence run`. Real git repos in tmp_path; the worker is faked at `run_detached`."""
import contextlib
import datetime as dt
import json
import threading
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from test_agentic_close import _commit, _git_env, world  # noqa: F401
from nightwright.agentic import cadence as CAD
from nightwright.agentic import events as E
from nightwright.agentic import gate_slots
from nightwright.agentic import gates as G
from nightwright.agentic import gitops as git
from nightwright.agentic import project as P
from nightwright.agentic import queue as Q
from nightwright.agentic import redmain as RM
from nightwright.agentic import remote_gates as RG
from nightwright.agentic import sentinel as SN
from nightwright.agentic import workers as WK
from nightwright.cli import cli

pytestmark = pytest.mark.fast

PROJECT = "demo"
NOW = dt.datetime(2026, 11, 9, 12, 0, tzinfo=dt.timezone.utc).timestamp()
H = 3600


def _job(name, **kw):
    return {"name": name, "cmd": "test ! -f broken.txt", "timeout_s": 30, **kw}


@pytest.fixture
def cw(world, monkeypatch):
    """`world` whose project has two cadence jobs; red_main on, notifications off."""
    cfg = world["cfg"]
    cfg["agentic"] = {"escalations": {"notify": "none"}}
    pcfg = P.load_project_config(world["root"] / ".nw" / "project.yaml")
    pcfg["_root"] = str(world["root"])
    pcfg["cadence"] = [_job("tui", every_n_merges=10, every_hours=24),
                       _job("perf", every_hours=24)]
    monkeypatch.setattr(P, "project_config", lambda c, p: pcfg)
    monkeypatch.setattr(P, "project_root", lambda c, p: world["root"])
    return {**world, "pcfg": pcfg}


def _row(cfg, job):
    return json.loads(CAD.state_path(cfg, PROJECT).read_text())["jobs"][job]


def _events(cfg, type_):
    return [e for e in E.read_since(cfg, 0, types=[type_]) if e["project"] == PROJECT]


def _journal(cfg, kind):
    return [r for r in Q.read_journal(cfg) if r.get("kind") == kind]


# ---- cadence.run ---------------------------------------------------------------------------

def test_busy_before_prepare_and_running_while_a_job_runs(cw, monkeypatch):
    cfg = cw["cfg"]
    prepared = []
    monkeypatch.setattr(SN, "_prepare_worktree", lambda *a: prepared.append(a) or "x")
    with SN.run_lock(cfg, PROJECT):
        with pytest.raises(SN.Busy):
            CAD.run(cfg, PROJECT, "tui", project_cfg=cw["pcfg"])
    assert prepared == []
    seen = []
    real = G.run_gate_with_retries

    def spy(*a, **k):
        seen.append(SN.running(cfg, PROJECT))
        return real(*a, **k)
    monkeypatch.undo()
    monkeypatch.setattr(G, "run_gate_with_retries", spy)
    out = CAD.run(cfg, PROJECT, "tui", project_cfg=cw["pcfg"], now=NOW)
    assert out["ok"] is True and seen == [True]
    assert not SN.running(cfg, PROJECT)
    assert not (SN.worktree_path(cfg, PROJECT).parent / f"{PROJECT}.last.json").exists()
    assert not SN.state_path(cfg, PROJECT).exists(), "no sentinel state is created"


def test_green_local_run_events_logs_and_state(cw):
    cfg = cw["cfg"]
    CAD.force(cfg, PROJECT, "tui")
    out = CAD.run(cfg, PROJECT, "tui", project_cfg=cw["pcfg"], now=NOW, reason="forced")
    sha = git.rev_parse(cw["root"])
    assert set(out) == {"project", "job", "ok", "sha", "interrupted", "error", "deferred",
                        "reason", "failing", "logs"}
    assert out["ok"] is True and out["sha"] == sha and not out["interrupted"]
    wt = SN.worktree_path(cfg, PROJECT)
    logs = wt.parent / f"{wt.name}.logs" / "cadence-tui"
    assert out["logs"] and all(p.startswith(str(logs)) for p in out["logs"])
    row = _row(cfg, "tui")
    assert row["forced"] is False and row["last_ok"] is True and row["last_green_sha"] == sha
    assert "inflight" not in row
    (ev,) = _events(cfg, "cadence")
    assert ev["data"]["job"] == "tui" and ev["data"]["ok"] is True
    assert ev["data"]["reason"] == "forced" and "failing" not in ev["data"]
    assert RM.read(cfg, PROJECT) is None or not RM.read(cfg, PROJECT).get("open")


def test_prepare_failure_does_not_clear_forced(cw, monkeypatch):
    cfg = cw["cfg"]
    CAD.force(cfg, PROJECT, "tui")

    def boom(*a):
        raise git.GitError(["git", "fetch"], 1, "offline")
    monkeypatch.setattr(SN, "_prepare_worktree", boom)
    with pytest.raises(git.GitError):
        CAD.run(cfg, PROJECT, "tui", project_cfg=cw["pcfg"])
    assert _row(cfg, "tui")["forced"] is True and "inflight" not in _row(cfg, "tui")


def test_red_run_opens_a_cadence_episode_with_failing_ids(cw):
    cfg = cw["cfg"]
    _commit(cw["root"], "broken.txt", "x\n", "break")
    out = CAD.run(cfg, PROJECT, "tui", project_cfg=cw["pcfg"], now=NOW)
    assert out["ok"] is False and out["deferred"] is False
    rec = RM.read(cfg, PROJECT)
    assert rec["open"] and rec["source"] == "cadence:tui" and rec["first_red_sha"] == out["sha"]
    (ev,) = _events(cfg, "cadence")
    assert ev["data"]["ok"] is False and "failing" in ev["data"]
    (red,) = _events(cfg, "red_main")
    assert red["data"]["source"] == "cadence:tui" and red["data"]["opened"] is True
    assert not SN.state_path(cfg, PROJECT).exists()


def _slot(pool, interrupted=False):
    @contextlib.contextmanager
    def acquire(cfg, owner, **kw):
        acquire.calls.append((owner, kw))
        yield SimpleNamespace(pool=pool, interrupted=interrupted)
    acquire.calls = []
    return acquire


def _worker(monkeypatch):
    monkeypatch.setattr(WK, "gate_pools", lambda cfg, *a, **k: [("box", 6)])
    monkeypatch.setattr(RG, "ineligible", lambda *a: None)


def test_remote_run_uses_the_one_gate_config_and_the_job_id(cw, monkeypatch):
    cfg = cw["cfg"]
    _worker(monkeypatch)
    refusals = []
    monkeypatch.setattr(RG, "header_refusal",
                        lambda pc, st, c, seq: refusals.append((st["id"],
                                                                [g["name"] for g in pc["gates"]])))
    acq = _slot("box")
    monkeypatch.setattr(gate_slots, "acquire", acq)
    calls = []

    def detached(worker, pcfg, state, c, seq, logs, stop=None):
        calls.append((worker, [g["name"] for g in pcfg["gates"]], state["id"], str(logs), stop))
        res = G.GateResult(name="tui", required=True, ok=True, exit_code=0, timed_out=False,
                           duration_s=1.0, log="001-tui.log", tail="")
        return G.GateRun(seq=seq, ok=True, results=(res,), started_at="", duration_s=1.0,
                         worker=worker)
    monkeypatch.setattr(RG, "run_detached", detached)
    local = []
    monkeypatch.setattr(G, "run_gate_with_retries", lambda *a, **k: local.append(a))
    stop = threading.Event()
    out = CAD.run(cfg, PROJECT, "tui", project_cfg=cw["pcfg"], stop=stop, now=NOW)
    assert out["ok"] is True and local == []
    cid = CAD.campaign_id(PROJECT, "tui")
    assert cid == "sentinel-demo-tui"
    # _placement's check, then the second one with the job's campaign id
    assert refusals[0][1] == ["tui"] and refusals[-1] == (cid, ["tui"])
    assert acq.calls[0][0] == cid and acq.calls[0][1]["stop"] is stop
    assert acq.calls[0][1]["pools"] == [("box", 6)]
    assert calls[0][:3] == ("box", ["tui"], cid) and calls[0][3].endswith("cadence-tui")


def test_campaign_id_with_a_space_is_header_safe():
    from nightwright.agentic import worker as W
    cid = CAD.campaign_id("my proj", "tui")
    assert cid == "sentinel-my_proj-tui" and W.KEY_RE.match(cid)


def test_header_refusal_runs_locally(cw, monkeypatch):
    cfg = cw["cfg"]
    _worker(monkeypatch)
    seen = []
    monkeypatch.setattr(RG, "header_refusal",
                        lambda pc, st, c, seq: None if st["id"].startswith("sentinel-demo") and
                        not st["id"].endswith("-tui") else "bad header")
    monkeypatch.setattr(gate_slots, "acquire",
                        lambda c, o, **kw: seen.append(kw) or _slot(None)(c, o, **kw))
    monkeypatch.setattr(RG, "run_detached", lambda *a, **k: pytest.fail("not remote"))
    out = CAD.run(cfg, PROJECT, "tui", project_cfg=cw["pcfg"], now=NOW)
    assert out["ok"] is True and seen[0]["pools"] is None


def test_remote_failure_is_a_failed_run_and_never_local(cw, monkeypatch):
    cfg = cw["cfg"]
    _worker(monkeypatch)
    monkeypatch.setattr(RG, "header_refusal", lambda *a: None)
    monkeypatch.setattr(gate_slots, "acquire", _slot("box"))

    def boom(*a, **k):
        raise RG.RemoteFailure("transport", "ssh died")
    monkeypatch.setattr(RG, "run_detached", boom)
    local = []
    monkeypatch.setattr(G, "run_gate_with_retries", lambda *a, **k: local.append(a))
    hooks = []
    monkeypatch.setattr(RM, "on_red", lambda *a, **k: hooks.append("red"))
    monkeypatch.setattr(RM, "on_green", lambda *a, **k: hooks.append("green"))
    out = CAD.run(cfg, PROJECT, "tui", project_cfg=cw["pcfg"], now=NOW)
    assert out["ok"] is None and "ssh died" in out["error"] and local == [] and hooks == []
    assert _row(cfg, "tui")["last_ok"] is None and _row(cfg, "tui")["last_error"]
    (ev,) = _events(cfg, "cadence")
    assert ev["data"]["ok"] is None and "ssh died" in ev["data"]["error"]


def test_interrupt_in_the_slot_wait_restores_forced(cw, monkeypatch):
    cfg = cw["cfg"]
    CAD.force(cfg, PROJECT, "tui")
    monkeypatch.setattr(gate_slots, "acquire", _slot(None, interrupted=True))
    hooks = []
    monkeypatch.setattr(RM, "on_red", lambda *a, **k: hooks.append("red"))
    monkeypatch.setattr(RM, "on_green", lambda *a, **k: hooks.append("green"))
    out = CAD.run(cfg, PROJECT, "tui", project_cfg=cw["pcfg"], now=NOW)
    assert out["interrupted"] is True and out["ok"] is None and hooks == []
    row = _row(cfg, "tui")
    assert row["forced"] is True and row["last_run_at"] is None and "inflight" not in row
    assert _events(cfg, "cadence") == []


def test_a_stop_during_the_gate_is_an_interrupt(cw, monkeypatch):
    cfg = cw["cfg"]
    CAD.force(cfg, PROJECT, "tui")
    _worker(monkeypatch)
    monkeypatch.setattr(RG, "header_refusal", lambda *a: None)
    monkeypatch.setattr(gate_slots, "acquire", _slot("box"))
    monkeypatch.setattr(RG, "run_detached", lambda *a, **k: None)   # stopped mid-run
    hooks = []
    monkeypatch.setattr(RM, "on_red", lambda *a, **k: hooks.append("red"))
    monkeypatch.setattr(RM, "on_green", lambda *a, **k: hooks.append("green"))
    out = CAD.run(cfg, PROJECT, "tui", project_cfg=cw["pcfg"], now=NOW)
    assert out["interrupted"] is True and hooks == [] and _row(cfg, "tui")["forced"] is True
    assert _events(cfg, "cadence") == []


def test_an_exception_after_begin_finishes_failed_without_hooks(cw, monkeypatch):
    cfg = cw["cfg"]

    def boom(*a, **k):
        raise RuntimeError("gate runner exploded")
    monkeypatch.setattr(G, "run_gate_with_retries", boom)
    hooks = []
    monkeypatch.setattr(RM, "on_red", lambda *a, **k: hooks.append("red"))
    monkeypatch.setattr(RM, "on_green", lambda *a, **k: hooks.append("green"))
    out = CAD.run(cfg, PROJECT, "tui", project_cfg=cw["pcfg"], now=NOW)
    assert out["ok"] is None and "exploded" in out["error"] and hooks == []
    assert _row(cfg, "tui")["last_ok"] is None and "inflight" not in _row(cfg, "tui")


# ---- the runner scan -----------------------------------------------------------------------

@pytest.fixture
def scan(cw, monkeypatch):
    started = []
    monkeypatch.setattr(Q, "registered_projects", lambda cfg: [PROJECT])
    monkeypatch.setattr(SN, "projects", lambda cfg: [])
    monkeypatch.setattr(CAD, "run", lambda cfg, p, job, **kw: started.append(
        (p, job, kw.get("reason"))) or {"project": p, "job": job, "ok": True, "sha": "s",
                                        "interrupted": False, "error": None,
                                        "deferred": False})
    ctx = SimpleNamespace(cfg=cw["cfg"], stop=threading.Event())
    pool = Q.StepPool()
    yield cw, ctx, pool, started
    pool.shutdown()


def _tick(ctx, pool, busy=()):
    got = Q._sentinel_step(ctx, pool, list(busy))
    if pool.sentinel is not None:
        pool.sentinel[1].exception()
    return got


def _set_rows(cfg, **rows):
    CAD.ensure(cfg, PROJECT, project_cfg={"cadence": [{"name": n} for n in rows]}, now=NOW)
    doc = json.loads(CAD.state_path(cfg, PROJECT).read_text())
    for n, kw in rows.items():
        doc["jobs"][n].update(kw)
    CAD.state_path(cfg, PROJECT).write_text(json.dumps(doc))


def test_scan_seeds_rows_and_starts_nothing_until_due(scan, monkeypatch):
    cw, ctx, pool, started = scan
    monkeypatch.setattr(CAD.time, "time", lambda: NOW)
    assert _tick(ctx, pool) is None and started == []
    doc = json.loads(CAD.state_path(cw["cfg"], PROJECT).read_text())
    assert set(doc["jobs"]) == {"tui", "perf"}
    seeded = doc["jobs"]["perf"]["seeded_at"]
    monkeypatch.setattr(CAD.time, "time", lambda: NOW + 25 * H)
    assert _tick(ctx, pool) == PROJECT
    assert started == [(PROJECT, "tui", "hours")], "both due by hours: list order on a tie"
    assert json.loads(CAD.state_path(cw["cfg"], PROJECT).read_text())["jobs"]["perf"][
        "seeded_at"] == seeded, "seeded_at is set once"


def test_forced_beats_an_older_due_job_and_oldest_due_wins(scan, monkeypatch):
    cw, ctx, pool, started = scan
    cfg = cw["cfg"]
    monkeypatch.setattr(CAD.time, "time", lambda: NOW + 100 * H)
    old = dt.datetime.fromtimestamp(NOW - 50 * H, dt.timezone.utc).isoformat()
    newer = dt.datetime.fromtimestamp(NOW, dt.timezone.utc).isoformat()
    _set_rows(cfg, tui={"last_run_at": newer, "last_run_sha": "a"},
              perf={"last_run_at": old, "last_run_sha": "a"})
    assert _tick(ctx, pool) == PROJECT and started[-1][1] == "perf", "oldest due"
    _set_rows(cfg, tui={"last_run_at": newer, "last_run_sha": "a"},
              perf={"last_run_at": old, "last_run_sha": "a"})
    CAD.force(cfg, PROJECT, "tui")
    assert _tick(ctx, pool) == PROJECT and started[-1] == (PROJECT, "tui", "forced")


def test_forced_runs_inside_retry_and_on_a_green_unchanged_sha(scan, monkeypatch):
    cw, ctx, pool, started = scan
    cfg = cw["cfg"]
    sha = git.rev_parse(cw["root"])
    monkeypatch.setattr(CAD.time, "time", lambda: NOW)
    just = dt.datetime.fromtimestamp(NOW - 5, dt.timezone.utc).isoformat()
    _set_rows(cfg, tui={"last_run_at": just, "last_run_sha": sha, "last_ok": True},
              perf={"last_run_at": just, "last_run_sha": sha, "last_ok": False})
    assert _tick(ctx, pool) is None
    CAD.force(cfg, PROJECT, "tui")
    assert _tick(ctx, pool) == PROJECT and started == [(PROJECT, "tui", "forced")]


def test_one_in_flight_no_scan_and_harvest_journals_cadence_done(scan, monkeypatch):
    cw, ctx, pool, started = scan
    cfg = cw["cfg"]
    CAD.force(cfg, PROJECT, "tui")
    gate = threading.Event()
    monkeypatch.setattr(CAD, "run", lambda cfg, p, job, **kw: gate.wait(5) and {
        "ok": False, "sha": "s", "interrupted": False, "error": None, "deferred": True})
    assert Q._sentinel_step(ctx, pool, []) == PROJECT
    assert len(pool.sentinel) == 3 and pool.sentinel[2] == "tui"
    (fl,) = Q._waiting_on(cfg, pool)
    assert fl["campaign"] == "sentinel-demo-tui" and fl["kind"] == "cadence"
    CAD.force(cfg, PROJECT, "perf")
    assert Q._sentinel_step(ctx, pool, []) is None, "a future in flight: no scan"
    gate.set()
    pool.sentinel[1].result()
    Q._sentinel_step(ctx, pool, [])
    done = _journal(cfg, "cadence_done")
    assert done and done[0]["job"] == "tui" and done[0]["ok"] is False
    assert done[0]["deferred"] is True
    assert _journal(cfg, "sentinel_done") == []
    # the faked run never clears `forced`, so the harvesting tick starts tui again
    assert [r["job"] for r in _journal(cfg, "cadence_started")][:1] == ["tui"]


def test_exception_in_run_journals_cadence_failed_only(scan, monkeypatch):
    cw, ctx, pool, started = scan
    CAD.force(cw["cfg"], PROJECT, "tui")

    def busy(*a, **k):
        raise SN.Busy("held")
    monkeypatch.setattr(CAD, "run", busy)
    assert _tick(ctx, pool) == PROJECT
    monkeypatch.setattr(Q, "registered_projects", lambda cfg: [])
    Q._sentinel_step(ctx, pool, [])
    assert [r["job"] for r in _journal(cw["cfg"], "cadence_failed")] == ["tui"]
    assert _journal(cw["cfg"], "cadence_done") == []
    assert _journal(cw["cfg"], "sentinel_failed") == []


def test_campaigns_first_and_running_projects_skipped(scan):
    cw, ctx, pool, started = scan
    cfg = cw["cfg"]
    CAD.force(cfg, PROJECT, "tui")
    assert _tick(ctx, pool, [{"id": "G1", "state": "GATING"}]) is None
    with SN.run_lock(cfg, PROJECT):
        assert _tick(ctx, pool) is None
    ctx.stop.set()
    assert _tick(ctx, pool) is None
    ctx.stop.clear()
    assert _tick(ctx, pool) == PROJECT and started == [(PROJECT, "tui", "forced")]


def test_a_crash_left_inflight_is_consumed_before_due(scan, monkeypatch):
    cw, ctx, pool, started = scan
    cfg = cw["cfg"]
    sha = git.rev_parse(cw["root"])
    monkeypatch.setattr(CAD.time, "time", lambda: NOW)
    _set_rows(cfg, tui={"inflight": {"merges": 0, "paths": [], "sha": sha, "forced": False},
                        "merges_since": 12})
    assert _tick(ctx, pool) is None, "the crash counts as a failed run at the base: RETRY_S"
    row = _row(cfg, "tui")
    assert "inflight" not in row and row["last_ok"] is None and row["last_run_sha"] == sha


def test_load_failure_journalled_once_and_skipped(scan, monkeypatch):
    cw, ctx, pool, started = scan

    def bad(c, p):
        raise ValueError("bad yaml at cadence[0]")
    monkeypatch.setattr(P, "project_config", bad)
    for _ in range(3):
        assert _tick(ctx, pool) is None
    rows = _journal(cw["cfg"], "cadence_failed")
    assert len(rows) == 1 and "bad yaml" in rows[0]["error"]


def test_a_cadence_project_skips_the_sentinel_clock(cw, monkeypatch):
    cfg = cw["cfg"]
    cfg["agentic"]["sentinel"] = {"interval_hours": 6}
    cw["pcfg"]["gates"] = [{"name": "full", "cmd": "true", "when": "close", "required": True,
                            "timeout_s": 30}]
    monkeypatch.setattr(SN, "projects", lambda c: [PROJECT])
    monkeypatch.setattr(Q, "registered_projects", lambda c: [])
    clock = []
    monkeypatch.setattr(SN, "time_due", lambda *a, **k: clock.append(a) or True)
    sentinel = []
    monkeypatch.setattr(SN, "run", lambda *a, **k: sentinel.append(a) or {"ok": True})
    ctx = SimpleNamespace(cfg=cfg, stop=threading.Event())
    pool = Q.StepPool()
    assert _tick(ctx, pool) is None and clock == [] and sentinel == []
    # a reuse-suspect trigger (SPEC-0359) still starts a sentinel run, without NW_ALL_TIERS
    SN._write(SN.state_path(cfg, PROJECT), {**SN.empty_state(), "reuses_since_green": 3})
    cw["pcfg"]["sentinel"] = {"enabled": True, "after_reuses": 3, "max_age_s": 86400}
    assert _tick(ctx, pool) == PROJECT and len(sentinel) == 1
    pool.shutdown()
    state = {"id": f"sentinel-{PROJECT}", "project": PROJECT,
             "repo": {"worktree": str(cw["root"])}}
    (cw["root"] / ".nw" / "project.yaml").write_text(
        (cw["root"] / ".nw" / "project.yaml").read_text()
        + "cadence:\n  - {name: tui, cmd: 'true', every_hours: 24}\n")
    assert G.all_tiers_env(cfg, state, header=True) == {}


def test_red_main_step_visits_a_cadence_project_without_sentinel_state(cw, monkeypatch):
    cfg = cw["cfg"]
    monkeypatch.setattr(Q, "registered_projects", lambda c: [PROJECT])
    monkeypatch.setattr(SN, "projects", lambda c: [])
    _commit(cw["root"], "broken.txt", "x\n", "break")
    out = CAD.run(cfg, PROJECT, "tui", project_cfg=cw["pcfg"], now=NOW)
    assert out["ok"] is False and RM.read(cfg, PROJECT)["open"]
    acts = []
    monkeypatch.setattr(Q, "_red_act", lambda ctx, pool, report, project, pcfg, red, action,
                        why, own: acts.append((project, action)))
    ctx = SimpleNamespace(cfg=cfg, stop=threading.Event(), progress=lambda: None)
    Q._red_main_step(ctx, Q.StepPool(), Q.TickReport(), own=True)
    assert acts == [(PROJECT, "open_fixforward")]
    # a project whose config fails to load is skipped
    monkeypatch.setattr(P, "project_config", lambda c, p: (_ for _ in ()).throw(ValueError("x")))
    acts.clear()
    Q._red_main_step(ctx, Q.StepPool(), Q.TickReport(), own=True)
    assert acts == []


# ---- CLI -------------------------------------------------------------------------------------

def _cli(cfg, monkeypatch, args):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    return CliRunner().invoke(cli, args)


def test_cli_cadence_run_forces_with_and_without_a_runner(cw, monkeypatch):
    from nightwright.agentic import lease as L
    cfg = cw["cfg"]
    monkeypatch.setattr(L, "is_held", lambda c: False)
    r = _cli(cfg, monkeypatch, ["gate", "cadence", "run", "perf", "--project", PROJECT])
    assert r.exit_code == 0, r.output
    assert "no runner is up" in r.output and _row(cfg, "perf")["forced"] is True
    monkeypatch.setattr(L, "is_held", lambda c: True)
    r = _cli(cfg, monkeypatch, ["gate", "cadence", "run", "tui", "--project", PROJECT])
    assert r.exit_code == 0 and "queued" in r.output and _row(cfg, "tui")["forced"] is True
    r = _cli(cfg, monkeypatch, ["gate", "cadence", "run", "nope", "--project", PROJECT])
    assert r.exit_code == 2 and "no cadence job" in r.output
    monkeypatch.setattr(P, "find_project_for_cwd", lambda c, cwd=None: PROJECT)
    r = _cli(cfg, monkeypatch, ["gate", "cadence", "run", "perf"])
    assert r.exit_code == 0, r.output


def test_cli_status_running_column(cw, monkeypatch):
    cfg = cw["cfg"]
    CAD.ensure(cfg, PROJECT, project_cfg=cw["pcfg"])
    r = _cli(cfg, monkeypatch, ["gate", "cadence", "status", "--project", PROJECT, "--json"])
    assert {j["running"] for j in json.loads(r.output)["jobs"]} == {"no"}
    with SN.run_lock(cfg, PROJECT):
        r = _cli(cfg, monkeypatch, ["gate", "cadence", "status", "--project", PROJECT, "--json"])
    assert {j["running"] for j in json.loads(r.output)["jobs"]} == {"yes"}
