"""SPEC-0345: persona cap, rate-limit backoff, campaign affinity and cost by worker. Offline:
SPEC-0338's loopback workers (`remote_support.Remote`) and `FakeRunner`."""
import datetime as dt
import json
import os
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from nightwright.agentic import campaign_export as CE
from nightwright.agentic import events as EV
from nightwright.agentic import persona_gate as PG
from nightwright.agentic import queue as Q
from nightwright.agentic import remote_gates as RG
from nightwright.agentic import remote_jobs as RJ
from nightwright.agentic import remote_personas as RPS
from nightwright.agentic import runner as R
from nightwright.agentic import store as S
from nightwright.agentic import workers as WK

from remote_support import CID, LOCAL_OK, WRITER, _remote, _req, _world, _write_script
from remote_support import _short_runtime_dir  # noqa: F401  (autouse fixture)
from waits import join
from fanout_support import (A, B, PersonaRunner, b_entry, drive, e2e,  # noqa: F401
                            impl_done, review as review_entry, second_reviewer, starts)


# ---- helpers -----------------------------------------------------------------------------------

def _two_workers(w, script):
    """Registry `box`, `box2` (both the loopback, both taking gates and personas)."""
    _remote(w, script)
    recs = json.loads(WK.path(w.cfg).read_text())["workers"]
    recs.append(dict(recs[0], name="box2"))
    WK.path(w.cfg).write_text(json.dumps({"schema": WK.SCHEMA, "workers": recs}))


def _set_registry(w, name, **fields):
    doc = json.loads(WK.path(w.cfg).read_text())
    for r in doc["workers"]:
        if r["name"] == name:
            r.update(fields)
    WK.path(w.cfg).write_text(json.dumps(doc))


def _place(w, state=None, keep=False):
    """One persona dispatch; the worker it landed on (None: local). Releases the slot unless
    `keep`."""
    st = state if state is not None else S.read_state(w.cfg, CID)
    rr = RPS.dispatch(w.cfg, st, "implement", _req(w))
    if rr is None:
        return None, None
    if not keep:
        os.close(rr.slot)
    return rr.worker["name"], rr


def _affine(w, name="box2"):
    st = S.read_state(w.cfg, CID)
    st["remote_affinity"], st["remote_affinity_at"] = name, S.now_iso()
    S.write_state(w.cfg, CID, st)
    return st


@pytest.fixture
def two(tmp_path):
    w = _world(tmp_path)
    _two_workers(w, _write_script(tmp_path / "s.json", WRITER))
    return w


# ---- affinity ----------------------------------------------------------------------------------

def test_first_persona_run_sets_affinity(two):
    assert S.read_state(two.cfg, CID).get("remote_affinity") is None
    assert _place(two)[0] == "box"                           # registry order
    st = S.read_state(two.cfg, CID)
    assert st["remote_affinity"] == "box" and st["remote_affinity_at"]


def test_three_persona_runs_and_a_gate_run_land_on_the_affine_worker(two):
    _affine(two, "box2")
    for _ in range(3):
        assert _place(two)[0] == "box2"
    st = S.read_state(two.cfg, CID)
    pools = RG.pools_for(two.cfg, two.project, st, seq=1)
    assert [p[0] for p in pools][:2] == ["box2", "box"]
    assert S.read_state(two.cfg, CID)["remote_affinity"] == "box2"


def test_full_affine_worker_falls_through_without_clearing(two):
    _affine(two, "box2")
    name, held = _place(two, keep=True)                      # box2's one slot is now taken
    try:
        assert name == "box2"
        assert _place(two)[0] == "box"
        assert S.read_state(two.cfg, CID)["remote_affinity"] == "box2"
    finally:
        os.close(held.slot)
    assert _place(two)[0] == "box2"                          # free again: back to it


@pytest.mark.parametrize("why", ["disabled", "check_failed", "job_lost"])
def test_failing_affine_worker_is_skipped(two, why):
    st = _affine(two, "box2")
    if why == "disabled":
        _set_registry(two, "box2", enabled=False)
    elif why == "check_failed":
        _set_registry(two, "box2", check_ok=False)
    else:
        S.append_event(two.cfg, CID, "worker_job_lost", {"worker": "box2", "job_id": "j",
                                                        "attempt": 1, "reason": "unreachable"})
        assert PG.affinity(two.cfg, st) is None             # a loss clears the affinity
    assert _place(two)[0] == "box"
    if why != "disabled":                                    # a gate run does not prefer it
        pools = RG.pools_for(two.cfg, two.project, S.read_state(two.cfg, CID), seq=1)
        assert pools[0][0] == "box"


def test_failing_affine_worker_first_in_the_registry_is_demoted_for_gates(two):
    _affine(two, "box")                                      # registry order: box first
    _set_registry(two, "box", check_ok=False)
    pools = RG.pools_for(two.cfg, two.project, S.read_state(two.cfg, CID), seq=1)
    assert [p[0] for p in pools][:2] == ["box2", "box"]


def test_gate_run_never_sets_affinity(two):
    pools = RG.pools_for(two.cfg, two.project, S.read_state(two.cfg, CID), seq=1)
    assert pools[0][0] == "box"
    assert S.read_state(two.cfg, CID).get("remote_affinity") is None


def test_lost_persona_job_clears_affinity(tmp_path):
    w = _world(tmp_path)
    # the worker's FakeRunner raises (persona mismatch): the job is lost and runs locally
    _remote(w, _write_script(tmp_path / "s.json", [dict(WRITER[0], persona="reviewer")]))
    req = _req(w)
    st = S.read_state(w.cfg, CID)
    rr = RPS.dispatch(w.cfg, st, "implement", req,
                      local=R.FakeRunner(_write_script(tmp_path / "ok.json", LOCAL_OK)))
    assert S.read_state(w.cfg, CID)["remote_affinity"] == "box"
    rr.run(req)
    assert S.read_state(w.cfg, CID)["remote_affinity"] is None
    lost = S.read_ledger(w.cfg, CID, types=["worker_job_lost"])
    assert [e["worker"] for e in lost] == ["box"]


def test_workers_check_records_its_outcome(tmp_path, monkeypatch):
    w = _world(tmp_path)
    _remote(w, _write_script(tmp_path / "s.json", WRITER))
    monkeypatch.setattr(WK, "_check", lambda cfg, wk, out: out["errors"].append("down"))
    assert WK.check(w.cfg, WK.load(w.cfg)[0])["ok"] is False
    rec = WK.load(w.cfg)[0]
    assert rec["check_ok"] is False and rec["checked_at"]
    assert PG.failing(w.cfg, S.read_state(w.cfg, CID), rec)


# ---- the dispatch gate: global cap -------------------------------------------------------------

def _open_run(cfg, cid, run_id, persona="implementer"):
    S.append_event(cfg, cid, "run.start", {
        "persona": persona, "run_id": run_id, "model": "m", "effort": None,
        "max_budget_usd": 1.0, "timeout_s": 60, "permission_mode": "x", "prompt_sha256": "0"})


def _cap(cfg, n):
    cfg.setdefault("agentic", {}).setdefault("workers", {})["max_personas"] = n


def test_cap_counts_local_and_remote_runs_and_trims_a_fan_out(tmp_path):
    w = _world(tmp_path)
    _open_run(w.cfg, CID, "001-implementer")                              # a local live run
    RJ.create(w.cfg, job_id="j-1", campaign="other", worker="box", attempt=1, seq=0,
              submitted_at=0.0, kind="persona", deadline_at=9e9)          # a remote live run
    RJ.update(w.cfg, "j-1", run_id="001-reviewer", persona="reviewer")
    _open_run(w.cfg, CID, "002-spec_reviewer", persona="spec_reviewer")   # not counted
    assert PG.live_persona_runs(w.cfg, [CID]) == 2
    st = S.read_state(w.cfg, CID)
    step = Q.Step("review", persona="a", personas=("a", "b", "c"))
    _cap(w.cfg, 2)
    gate = PG.Gate(w.cfg, [CID])
    assert gate.admit(st, step) is None                                   # 2 live, cap 2
    assert gate.holds[CID]["held_by"] == "persona_cap"
    _cap(w.cfg, 3)
    gate = PG.Gate(w.cfg, [CID])
    got = gate.admit(st, step)
    assert got.personas == ("a",) and got.held == ("b", "c")
    assert gate.admit(st, Q.Step("implement", persona="implementer")) is None   # room used
    gate.flush()
    assert PG.read_holds(w.cfg)[CID]["held_by"] == "persona_cap"


def test_no_cap_by_default(tmp_path):
    w = _world(tmp_path)
    for i in range(5):
        _open_run(w.cfg, CID, f"00{i}-implementer")
    step = Q.Step("review", persona="a", personas=("a", "b", "c"))
    assert PG.max_personas(w.cfg) is None
    assert PG.Gate(w.cfg, [CID]).admit(S.read_state(w.cfg, CID), step) is step


@pytest.mark.parametrize("bad", [0, "2", True, -1, 1.5])
def test_invalid_max_personas_is_rejected(tmp_path, bad):
    cfg = {"agentic": {"workers": {"max_personas": bad}}}
    with pytest.raises(ValueError, match="max_personas"):
        Q.validate_runner_cfg(cfg)
    Q.validate_runner_cfg({"agentic": {"workers": {"max_personas": 2}}})
    Q.validate_runner_cfg({"agentic": {"workers": {"max_personas": None}}})


def test_capped_fan_out_runs_members_on_later_ticks(e2e):
    cfg, cid, repo, runner = e2e({"implementer": [impl_done()], A: [review_entry("pass")],
                                  B: [b_entry("pass")]})
    _cap(cfg, 1)
    reports = drive(cfg, runner)
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st
    assert starts(cfg, cid) == ["001-implementer", f"002-{A}", f"003-{B}"]
    # B started only after A's run ended: never two persona runs at once
    led = [(e["type"], e.get("run_id")) for e in S.read_ledger(cfg, cid, types=["run.start", "run.end"])]
    assert led.index(("run.end", f"002-{A}")) < led.index(("run.start", f"003-{B}"))
    notes = [e["text"] for e in S.read_ledger(cfg, cid, types=["note"])]
    assert any(t.startswith(f"persona cap: {B}") for t in notes)
    assert not (st["counters"].get("review_retries_by_persona") or {})   # waiting is no retry
    assert any(r.get("reason") == "persona_cap" for rep in reports for r in rep.steps)


def test_adopted_trimmed_fan_out_keeps_its_held_members(e2e):
    # after a restart the queue adopts the one member the cap let run; the reviewer it held is
    # still held (no retry charged), not a failed member
    cfg, cid, repo, runner = e2e({})
    ctx = Q._Ctx(cfg)
    st = S.read_state(cfg, cid)
    assert Q._adopted_held(ctx, st, (A,)) == ()                       # no cap: as before
    _cap(cfg, 1)
    assert Q._adopted_held(ctx, st, (A,)) == (B,)
    assert Q._adopted_held(ctx, st, (A, B)) == ()
    assert Q._adopted_held(ctx, st, ("review_triager",)) == ()
    loop = int((st.get("counters") or {}).get("loops", 0))
    st["last_review"] = {"verdicts": {B: {"verdict": b_entry("pass")["verdict"], "loop": loop}}}
    assert Q._adopted_held(ctx, st, (A,)) == ()                       # B already has a verdict


def test_no_workers_default_fan_out_runs_all_members_concurrently(e2e):
    barrier = threading.Barrier(2)
    cfg, cid, repo, runner = e2e({"implementer": [impl_done()], A: [review_entry("pass")],
                                  B: [b_entry("pass")]}, profile={"slots": 1}, barrier=barrier)
    drive(cfg, runner)
    assert S.read_state(cfg, cid)["state"] == "MERGED"      # both met at the barrier


def test_capped_fan_out_member_failing_twice_escalates(e2e):
    # room for 1 of 2 reviewers; A keeps failing: its second failure parks review_escalated,
    # as a full pass would, instead of re-running A every tick
    bad = dict(review_entry("pass"), malformed=True)
    cfg, cid, repo, runner = e2e({"implementer": [impl_done()], A: [bad, bad, bad],
                                  B: [b_entry("pass")]})
    _cap(cfg, 1)
    drive(cfg, runner)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "review_escalated", st
    assert "conformance reviewer" in st["park"]["detail"]
    assert runner.calls.count(A) == 2 and runner.calls.count(B) == 0


def test_capped_fan_out_charges_a_retry_only_to_the_member_that_ran(e2e):
    # room for 1 of 2 reviewers: A runs without a valid verdict (one retry), B is held (none);
    # A's retry then passes, B runs on a later tick, and the review routes to a merge
    bad = dict(review_entry("pass"), malformed=True)
    cfg, cid, repo, runner = e2e({"implementer": [impl_done()], A: [bad, review_entry("pass")],
                                  B: [b_entry("pass")]})
    _cap(cfg, 1)
    for _ in range(12):
        rep = Q.tick(cfg, runner=runner)
        if any(r.get("reason") == "persona_cap" for r in rep.steps):
            break
    st = S.read_state(cfg, cid)
    assert st["state"] == "REVIEWING"
    assert st["counters"]["review_retries_by_persona"] == {A: 1}       # B: held, no retry
    assert runner.calls.count(B) == 0
    drive(cfg, runner)
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st
    assert runner.calls.count(A) == 2 and runner.calls.count(B) == 1
    assert starts(cfg, cid)[-2:] == [f"003-{A}", f"004-{B}"]


def _fill_cap(cfg):
    """`max_personas: 1`, its one slot held by another campaign's live remote persona job, and
    the rate-limit backoff running: every gated dispatch is closed."""
    _cap(cfg, 1)
    RJ.create(cfg, job_id="j-other", campaign="other", worker="box", attempt=1, seq=0,
              submitted_at=0.0, kind="persona", deadline_at=9e9)
    RJ.update(cfg, "j-other", run_id="001-implementer", persona="implementer")
    PG._write_json(PG.rate_limit_path(cfg), {"until": PG._iso(dt.datetime.now().timestamp() + 900),
                                             "backoff_s": 900})
    assert PG.backoff_until(cfg) is not None


def test_spec_review_is_neither_counted_nor_gated(tmp_path, monkeypatch):
    from fanout_support import _git, added, make_cfg, make_repo, script
    w = _world(tmp_path)
    _open_run(w.cfg, CID, "001-spec_reviewer", persona="spec_reviewer")
    assert PG.live_persona_runs(w.cfg, [CID]) == 0                # not counted
    # a live campaign spec-review run starts while the backoff runs and the cap is full
    repo = make_repo(tmp_path)
    v = {"verdict": "approve", "size": "s", "tone": "cost-effective", "rationale": "ok",
         "findings": [], "suggested_splits": []}
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [
        {"persona": "spec_reviewer", "verdict": v, "cost_usd": 0.1}]))
    cid = added(cfg, monkeypatch)
    runner = R.FakeRunner(cfg["agentic"]["fake_script"])
    Q.tick(cfg, runner=R.FakeRunner(script(tmp_path, [], name="none.json")))   # open only
    st = S.read_state(cfg, cid)
    assert st["state"] == "IMPLEMENTING" and not starts(cfg, cid)
    wt = Path(st["repo"]["worktree"])                     # an idea campaign's draft spec
    spec = wt / st["spec"]["rel_path"]
    spec.write_text(spec.read_text().replace("status: accepted", "status: draft"))
    _git(wt, "commit", "-qam", "draft")
    st.update(state="SPEC_REVIEW", kind="idea")
    st["spec"]["id"] = "SPEC-0001"
    S.write_state(cfg, cid, st)
    _fill_cap(cfg)
    rep = Q.tick(cfg, runner=runner)
    assert [r for r in starts(cfg, cid) if r.endswith("-spec_reviewer")], (rep.steps, S.read_state(cfg, cid).get("park"))
    assert cid not in PG.read_holds(cfg)
    # the same closed gate holds a campaign persona step
    st = S.read_state(cfg, cid)
    st["state"] = "IMPLEMENTING"
    S.write_state(cfg, cid, st)
    n = len(starts(cfg, cid))
    Q.tick(cfg, runner=runner)
    assert len(starts(cfg, cid)) == n
    assert PG.read_holds(cfg)[cid]["held_by"] == "rate_limit"


@pytest.mark.parametrize("role,project", [("conductor", None), ("steward", "Foo")])
def test_conductor_and_steward_launch_during_a_backoff_and_are_not_counted(
        tmp_path, monkeypatch, role, project):
    from nightwright import rules
    from nightwright.agentic import launch as L
    w = _world(tmp_path)
    for d in ("nw", "foo"):
        (tmp_path / d).mkdir()
    monkeypatch.setattr(rules, "projects_payload", lambda cfg: {"projects": [
        {"name": "Meta-Tools", "research": {"root": str(tmp_path / "nw")}},
        {"name": "Foo", "research": {"root": str(tmp_path / "foo")}}]})
    w.cfg["agentic"].setdefault("conductor", {})["prices"] = {
        "claude": {"input": 3.0, "output": 15.0}}
    _fill_cap(w.cfg)
    before = PG.live_persona_runs(w.cfg, [CID, "other"])
    execs = []
    monkeypatch.setattr(os, "execvpe", lambda f, argv, env: execs.append(argv))
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.chdir(tmp_path)
    L.run(w.cfg, L.build_plan(w.cfg, role, project))            # not held by the backoff
    assert len(execs) == 1 and execs[0][0] == "claude"
    assert PG.live_persona_runs(w.cfg, [CID, "other"]) == before  # not counted toward the cap


# ---- rate limit --------------------------------------------------------------------------------

def _result(tmp_path, *, status=None, message="", is_error=True, name="r"):
    d = tmp_path / f"run-{name}"
    d.mkdir(exist_ok=True)
    raw = {"type": "result", "is_error": is_error, "api_error_status": status, "result": message}
    (d / "result.json").write_text(json.dumps(raw))
    return R.RunResult(exit_code=1 if is_error else 0, timed_out=False, interrupted=False,
                       duration_s=1.0, is_error=is_error, cost_usd=0.0, result_text=message,
                       raw_path=d / "result.json")


def _ok(tmp_path):
    return _result(tmp_path, is_error=False, message="{}", name="ok")


def _events(cfg):
    return [e for e in EV.read_since(cfg, 0) if e["type"] == "rate_limited"]


@pytest.fixture
def rl(tmp_path):
    w = _world(tmp_path)
    return w.cfg, tmp_path


def test_rate_limit_markers_are_transient():
    assert set(EV.RATE_LIMIT_MARKERS) <= set(EV._TRANSIENT_MARKERS)


@pytest.mark.parametrize("status,message,want", [
    (429, "API Error: 429 {\"type\":\"rate_limit_error\"}", True),
    (529, "API Error: 529 overloaded", True),
    (None, "API Error: 429 Too Many Requests", True),
    (None, "API Error: Repeated 529 Overloaded errors", True),
    (None, "API Error: rate limit exceeded", True),
    (500, "API Error: 500 internal", False),
    (429, "Claude AI usage limit reached|1700000000", False),
    (None, "API Error: usage_limit reached (429)", False),
    (None, "session limit reached", False),
    (None, "FAILED tests/x.py::t - x.py:429: AssertionError", False),
    (None, "x.py:429: AssertionError\nAPI Error: 429", False),
    (None, "API Error: 4290 bytes", False),
])
def test_is_rate_limit(tmp_path, status, message, want):
    assert PG.is_rate_limit(_result(tmp_path, status=status, message=message)) is want


def test_rate_limit_episode_doubles_resets_and_caps(rl):
    cfg, tmp = rl
    t0 = 1_000_000.0
    hit = _result(tmp, status=429, message="API Error: 429")
    for i in range(3):                                         # a burst: one episode
        PG.note_run(cfg, hit, t0 - 5, campaign=CID, now=t0 + i * 0.3)
    rl_ = PG.read_rate_limit(cfg)
    assert rl_["backoff_s"] == 60 and len(_events(cfg)) == 1
    assert _events(cfg)[0]["data"]["backoff_s"] == 60 and _events(cfg)[0]["campaign"] == CID
    assert PG.backoff_until(cfg, t0 + 59) is not None and PG.backoff_until(cfg, t0 + 61) is None
    # a success from a run started before `until` does not reset
    assert PG.note_run(cfg, _ok(tmp), t0 + 10, now=t0 + 70) is None
    assert PG.note_run(cfg, hit, t0, now=t0 + 70) == "rate_limited"     # second episode
    assert PG.read_rate_limit(cfg)["backoff_s"] == 120 and len(_events(cfg)) == 2
    t = t0 + 70
    for want in (240, 480, 900, 900):
        t += 1000
        PG.note_run(cfg, hit, None, now=t)
        assert PG.read_rate_limit(cfg)["backoff_s"] == want
    # a success from a run started after `until` resets: the next episode is 60 s again
    until = PG._epoch(PG.read_rate_limit(cfg)["until"])
    assert PG.note_run(cfg, _ok(tmp), until + 1, now=until + 5) == "reset"
    assert PG.read_rate_limit(cfg)["backoff_s"] == 60
    PG.note_run(cfg, hit, None, now=until + 10)
    assert PG.read_rate_limit(cfg)["backoff_s"] == 60


def test_usage_limit_and_test_output_429_do_not_set_the_backoff(rl):
    cfg, tmp = rl
    PG.note_run(cfg, _result(tmp, status=429, message="Claude AI usage limit reached"), None)
    PG.note_run(cfg, _result(tmp, message="x.py:429: AssertionError", name="t"), None)
    assert not PG.rate_limit_path(cfg).exists() and _events(cfg) == []


def test_concurrent_rate_limit_failures_make_one_episode(rl):
    cfg, tmp = rl
    hit = _result(tmp, status=429, message="API Error: 429")
    threads = [threading.Thread(target=PG.note_run, args=(cfg, hit, None)) for _ in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        join(th)
    assert PG.read_rate_limit(cfg)["backoff_s"] == 60 and len(_events(cfg)) == 1


def test_backoff_holds_every_persona_dispatch_and_shows_held_by(tmp_path, monkeypatch):
    from fanout_support import added, make_cfg, make_repo, script
    repo = make_repo(tmp_path)
    entries = [dict(impl_done(), api_error={"status": 429, "message": "API Error: 429 rate limit"}),
               review_entry("pass")]
    cfg = make_cfg(tmp_path, repo, script(tmp_path, entries))
    cid = added(cfg, monkeypatch)
    runner = R.FakeRunner(cfg["agentic"]["fake_script"])
    for _ in range(3):
        Q.tick(cfg, runner=runner)
        if PG.rate_limit_path(cfg).exists():
            break
    assert PG.read_rate_limit(cfg)["backoff_s"] == 60
    assert len(_events(cfg)) == 1
    n = len(starts(cfg, cid))
    for _ in range(3):                    # the gate step runs; the reviewer step is held
        Q.tick(cfg, runner=runner)
    assert S.read_state(cfg, cid)["state"] == "REVIEWING"
    assert len(starts(cfg, cid)) == n     # nothing started, locally or remotely
    item = next(i for i in Q.queue_payload(cfg)["items"] if i["campaign"] == cid)
    assert item["held_by"] == "rate_limit"
    assert PG.read_holds(cfg)[cid]["held_by"] == "rate_limit"
    # the backoff over (the clock moved past `until`), dispatch resumes
    doc = json.loads(PG.rate_limit_path(cfg).read_text())
    doc["until"] = PG._iso(PG._epoch(doc["until"]) - 3600)
    PG.rate_limit_path(cfg).write_text(json.dumps(doc))
    Q.tick(cfg, runner=runner)
    assert len(starts(cfg, cid)) == n + 1
    assert cid not in PG.read_holds(cfg)


# ---- detectors ---------------------------------------------------------------------------------

def _silent_world(tmp_path):
    w = _world(tmp_path)                                 # IMPLEMENTING, no run in flight
    later = dt.datetime.now().astimezone() + dt.timedelta(seconds=5000)
    return w, later


def _kinds(cfg, now):
    return {t for _k, t, cid, _d in EV.watchdog_checks(cfg, now) if cid == CID}


def test_backoff_hold_raises_no_campaign_silent(tmp_path):
    w, later = _silent_world(tmp_path)
    assert {"campaign_silent", "state_stuck"} <= _kinds(w.cfg, later)        # baseline
    PG._write_json(PG.rate_limit_path(w.cfg),
                   {"until": PG._iso(later.timestamp() + 900), "backoff_s": 900})
    assert not {"campaign_silent", "state_stuck"} & _kinds(w.cfg, later)
    assert CID not in [s["campaign"] for s in EV.ping_digest(w.cfg, later)["stuck"]]
    item = next(i for i in Q.queue_payload(w.cfg, now=later)["items"] if i["campaign"] == CID)
    assert item["held_by"] == "rate_limit"


def test_cap_hold_raises_no_campaign_silent(tmp_path):
    w, later = _silent_world(tmp_path)
    PG._write_json(PG.holds_path(w.cfg), {"holds": {CID: {"held_by": "persona_cap",
                                                          "since": S.now_iso()}}})
    assert not {"campaign_silent", "state_stuck"} & _kinds(w.cfg, later)
    item = next(i for i in Q.queue_payload(w.cfg, now=later)["items"] if i["campaign"] == CID)
    assert item["held_by"] == "persona_cap"


def test_queue_status_is_list(tmp_path, monkeypatch):
    from nightwright.cli import cli
    w = _world(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: w.cfg)
    r = CliRunner().invoke(cli, ["queue", "status", "--json"])
    assert r.exit_code == 0, r.output
    doc = json.loads(r.output)
    assert doc["schema"] == "nw.queue.v1" and "held_by" in doc["items"][0]
    assert doc["personas"]["max_personas"] is None


# ---- cost by worker ----------------------------------------------------------------------------

def _charge(state, worker, cost):
    rr = R.RunResult(exit_code=0, timed_out=False, interrupted=False, duration_s=1.0,
                     is_error=False, cost_usd=cost, verdict={"summary": "x"}, verdict_ok=True,
                     worker=worker)
    return Q._record_run(state, "implementer", "001-implementer", rr, 4.0)


def test_remote_cost_counts_like_local_cost():
    profile = {"personas": {"implementer": {"max_budget_usd": 4.0}}}
    remote = {"id": "x", "cost": {"total_usd": 0.0, "cap_usd": 10.0, "by_persona": {}}}
    local = {"id": "y", "cost": {"total_usd": 0.0, "cap_usd": 10.0, "by_persona": {}}}
    _charge(remote, "box", 6.5)
    _charge(local, None, 6.5)
    assert remote["cost"]["total_usd"] == local["cost"]["total_usd"] == 6.5
    assert Q._cap_blocks(remote, profile, "implementer")[0] is not None     # 6.5 + 4 > 10
    assert Q._cap_blocks(remote, profile, "implementer") == Q._cap_blocks(local, profile,
                                                                          "implementer")


def test_cost_by_worker_in_campaign_show_and_export(tmp_path, monkeypatch):
    from nightwright.cli import cli
    w = _world(tmp_path)
    st = S.read_state(w.cfg, CID)
    st["cost"] = {"total_usd": 0.0, "cap_usd": 10.0, "by_persona": {}}
    for worker, cost, rid in (("box", 1.25, "001"), (None, 0.5, "002"), ("box", 0.25, "003")):
        rr = R.RunResult(exit_code=0, timed_out=False, interrupted=False, duration_s=1.0,
                         is_error=False, cost_usd=cost, worker=worker)
        for etype, payload in Q._record_run(st, "implementer", f"{rid}-implementer", rr, 4.0):
            S.append_event(w.cfg, CID, etype, payload)
    S.write_state(w.cfg, CID, st)
    want = {"box": 1.5, "local": 0.5}
    monkeypatch.setattr("nightwright.cli.load_config", lambda: w.cfg)
    r = CliRunner().invoke(cli, ["campaign", "show", CID, "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["cost_by_worker"] == want
    r = CliRunner().invoke(cli, ["campaign", "show", CID])
    assert "cost by worker" in r.output and "box $1.50" in r.output
    row = CE.campaign_row(w.cfg, S.read_state(w.cfg, CID), S.read_ledger(w.cfg, CID),
                          dt.datetime.now().astimezone())
    assert row["cost_by_worker"] == want and row["cost_usd"] == 2.0
