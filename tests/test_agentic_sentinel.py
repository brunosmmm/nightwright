"""SPEC-0359 §4-5: the full-suite sentinel on the base branch and its `gate_reuse.json` state."""
import datetime as dt
import json
import os
import threading
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import waits

from test_agentic_close import _commit, _git_env, _pcfg, world  # noqa: F401
from test_agentic_close_reuse import _declined, rw  # noqa: F401
from nightwright.agentic import close as CL
from nightwright.agentic import escalations as ESC
from nightwright.agentic import events as E
from nightwright.agentic import gate_slots
from nightwright.agentic import gitops as git
from nightwright.agentic import project as P
from nightwright.agentic import queue as Q
from nightwright.agentic import sentinel as SN
from nightwright.agentic import store
from nightwright.cli import cli

pytestmark = pytest.mark.fast

PROJECT = "demo"
SCFG = {"enabled": True, "after_reuses": 3, "max_age_s": 86400}
NOW = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc).timestamp()


def _iso(offset_s):
    return dt.datetime.fromtimestamp(NOW + offset_s, dt.timezone.utc).isoformat()


def _st(**kw):
    return {**SN.empty_state(), **kw}


# ---- due / overdue -------------------------------------------------------------------------

def test_due_by_count_and_age():
    assert not SN.is_due(_st(), SCFG, NOW)
    assert not SN.is_due(_st(reuses_since_green=2, last_green_at=_iso(-60)), SCFG, NOW)
    assert SN.is_due(_st(reuses_since_green=3, last_green_at=_iso(-60)), SCFG, NOW)
    assert SN.is_due(_st(reuses_since_green=1, last_green_at=_iso(-86401)), SCFG, NOW)
    assert not SN.is_due(_st(reuses_since_green=0, last_green_at=_iso(-10 * 86400)), SCFG, NOW)
    # an unknown last_green_at counts as older than max_age_s
    assert SN.is_due(_st(reuses_since_green=1), SCFG, NOW)


def test_overdue_by_due_since_and_by_disabled():
    assert not SN.is_overdue(_st(reuses_since_green=3, due_since=_iso(-3600)), SCFG, NOW)
    assert SN.is_overdue(_st(reuses_since_green=3, due_since=_iso(-86401)), SCFG, NOW)
    off = {**SCFG, "enabled": False}
    assert not SN.is_overdue(_st(reuses_since_green=2), off, NOW)
    assert SN.is_overdue(_st(reuses_since_green=3), off, NOW)


def test_due_since_set_when_due_first_holds_and_cleared_on_green(world):
    cfg, root = world["cfg"], world["root"]
    pcfg = {"sentinel": SCFG}
    SN._write(SN.state_path(cfg, PROJECT), _st(last_green_at=_iso(-60)))
    for i in range(3):
        SN.record_merge(cfg, PROJECT, f"c{i}", f"{i:040x}", root=root, project_cfg=pcfg,
                        now=NOW)
        due_since = SN.read(cfg, PROJECT, project_cfg=pcfg, now=NOW)["due_since"]
        assert (due_since is None) == (i < 2), (i, due_since)
    st = SN.read(cfg, PROJECT, project_cfg=pcfg, now=NOW)
    assert st["due_since"] == SN._iso(NOW)
    sha = git.rev_parse(root)
    SN._write(SN.state_path(cfg, PROJECT), {**st, "suspects": [
        {"campaign": "c0", "merge_sha": sha}]})
    SN.settle(cfg, PROJECT, sha, True, root=root, project_cfg=pcfg, now=NOW + 5)
    st = SN.read(cfg, PROJECT, project_cfg=pcfg)
    assert st["due_since"] is None and st["suspects"] == [] and st["reuses_since_green"] == 0
    assert st["last_green_sha"] == sha and st["force_full_close"] is False


def test_check_due_marks_due_since_and_respects_enabled(world):
    cfg = world["cfg"]
    SN._write(SN.state_path(cfg, PROJECT), _st(reuses_since_green=1, last_green_at=_iso(-60)))
    assert not SN.check_due(cfg, PROJECT, project_cfg={"sentinel": SCFG}, now=NOW)
    assert SN.read(cfg, PROJECT)["due_since"] is None
    assert SN.check_due(cfg, PROJECT, project_cfg={"sentinel": SCFG}, now=NOW + 86400)
    assert SN.read(cfg, PROJECT)["due_since"] == SN._iso(NOW + 86400)
    assert not SN.check_due(cfg, PROJECT, project_cfg={"sentinel": {**SCFG, "enabled": False}},
                            now=NOW + 86400)


# ---- runs ----------------------------------------------------------------------------------

@pytest.fixture
def sw(world):
    """`world` with a close config, `_root` set and escalation notifications off and the SPEC-0420
    red-main machinery off (the legacy escalate-per-sha path, AC9)."""
    world["cfg"]["agentic"] = {"escalations": {"notify": "none"}, "red_main": {"enabled": False}}
    pcfg = _pcfg(world)
    pcfg["_root"] = str(world["root"])
    pcfg["gates"] = [
        {"name": "loop", "cmd": "false", "when": "loop", "required": True, "timeout_s": 30},
        {"name": "full", "cmd": "test ! -f broken.txt", "when": "close", "required": True,
         "timeout_s": 30, "paths": ["nothing/**"]},
    ]
    return {**world, "pcfg": pcfg}


def _sentinel_events(cfg):
    return [e for e in E.read_since(cfg, 0, types=["sentinel"]) if e["project"] == PROJECT]


def _merge_on_main(sw, name):
    return _commit(sw["root"], name, f"{name}\n", f"merge {name}")


def test_green_run_settles_only_ancestors_merge_during_run_survives(sw):
    cfg, root = sw["cfg"], sw["root"]
    first = _merge_on_main(sw, "a.txt")
    SN.record_merge(cfg, PROJECT, "c-a", first, root=root, project_cfg=sw["pcfg"])
    late = {}

    def merge_during_run(x):
        assert x == first
        late["sha"] = _merge_on_main(sw, "b.txt")       # lands after the reset to X
        SN.record_merge(cfg, PROJECT, "c-b", late["sha"], root=root, project_cfg=sw["pcfg"])

    out = SN.run(cfg, PROJECT, project_cfg=sw["pcfg"], _hooks={"after_reset": merge_during_run})
    assert out["ok"] and out["sha"] == first and not out["interrupted"]
    assert [r["name"] for r in out["results"]] == ["full"], "close gates only, no paths skip"
    assert out["suspects"] == [{"campaign": "c-a", "merge_sha": first}]
    st = SN.read(cfg, PROJECT)
    assert st["suspects"] == [{"campaign": "c-b", "merge_sha": late["sha"]}]
    assert st["reuses_since_green"] == 1
    assert st["last_green_sha"] == first and st["force_full_close"] is False
    assert st["due_since"] is None, "one survivor after a fresh green is not due yet"
    (ev,) = _sentinel_events(cfg)
    assert ev["data"] == {"project": PROJECT, "ok": True, "sha": first,
                          "suspects": [{"campaign": "c-a", "merge_sha": first}]}
    wt = SN.worktree_path(cfg, PROJECT)
    assert git.rev_parse(wt) == first and git.current_branch(wt) is None


def test_red_run_flags_escalates_names_only_ancestors(sw):
    cfg, root = sw["cfg"], sw["root"]
    first = _merge_on_main(sw, "broken.txt")
    SN.record_merge(cfg, PROJECT, "c-a", first, root=root, project_cfg=sw["pcfg"])
    late = {}

    def merge_during_run(x):
        late["sha"] = _merge_on_main(sw, "b.txt")
        SN.record_merge(cfg, PROJECT, "c-b", late["sha"], root=root, project_cfg=sw["pcfg"])

    out = SN.run(cfg, PROJECT, project_cfg=sw["pcfg"], _hooks={"after_reset": merge_during_run})
    assert not out["ok"] and out["failing"] == ["full"]
    assert out["suspects"] == [{"campaign": "c-a", "merge_sha": first}]
    st = SN.read(cfg, PROJECT)
    assert st["force_full_close"] is True
    assert [s["campaign"] for s in st["suspects"]] == ["c-a", "c-b"], "unchanged on red"
    (ev,) = _sentinel_events(cfg)
    assert ev["data"]["ok"] is False and ev["data"]["failing"] == ["full"]
    assert ev["data"]["suspects"] == [{"campaign": "c-a", "merge_sha": first}]
    (esc,) = ESC.list_all(cfg, "open")
    assert "c-a" in esc["question"] and "c-b" not in esc["question"]
    assert esc["project"] == PROJECT and esc["campaign"] is None
    assert any(first in f for f in esc["facts"])
    assert "force_full_close" in SN.reuse_blocker(cfg, PROJECT)


def test_red_sentinel_makes_the_next_close_decline_with_r1(rw):
    rw["cfg"]["agentic"] = {"escalations": {"notify": "none"}, "red_main": {"enabled": False}}
    SN.settle(rw["cfg"], PROJECT, git.rev_parse(rw["root"]), False, ["t::x"],
              root=str(rw["root"]))
    out = CL.prepare_close(rw["cfg"], rw["state"], rw["pcfg"])
    rule, detail = _declined(out)
    assert rule == "R1" and "force_full_close" in detail and rw["marker"].exists()


def test_deleted_after_red_restores_force_full_close(rw):
    rw["cfg"]["agentic"] = {"escalations": {"notify": "none"}, "red_main": {"enabled": False}}
    SN.settle(rw["cfg"], PROJECT, git.rev_parse(rw["root"]), False, ["t::x"],
              root=str(rw["root"]))
    SN.state_path(rw["cfg"], PROJECT).unlink()
    out = CL.prepare_close(rw["cfg"], rw["state"], rw["pcfg"])
    assert _declined(out)[0] == "R1"
    assert json.loads(SN.state_path(rw["cfg"], PROJECT).read_text())["force_full_close"] is True


def test_deleted_without_sentinel_history_rebuilt_from_events(world):
    cfg = world["cfg"]
    cid = world["state"]["id"]
    store.write_state(cfg, cid, dict(world["state"], state="MERGED"))
    store.append_event(cfg, cid, "close", {"mode": "merge", "merged_sha": "d" * 40,
                                           "reused": ["pytest"]})
    store.append_event(cfg, cid, "close", {"mode": "merge", "merged_sha": "e" * 40})
    st = SN.read(cfg, PROJECT, root=world["root"])
    assert st["suspects"] == [{"campaign": cid, "merge_sha": "d" * 40}]
    assert st["reuses_since_green"] == 1 and st["last_green_at"] is None
    assert st["force_full_close"] is True, "reused merges but no sentinel ever ran"
    assert SN.is_due(st, SCFG) and st["due_since"] is not None
    assert SN.state_path(cfg, PROJECT).is_file()


def test_rebuild_after_green_drops_contained_merges(sw):
    cfg, root = sw["cfg"], str(sw["root"])
    first = _merge_on_main(sw, "a.txt")
    cid = sw["state"]["id"]
    store.write_state(cfg, cid, dict(sw["state"], state="MERGED"))
    store.append_event(cfg, cid, "close", {"mode": "merge", "merged_sha": first,
                                           "reused": ["pytest"]})
    SN.settle(cfg, PROJECT, first, True, root=root)
    SN.state_path(cfg, PROJECT).unlink()
    st = SN.read(cfg, PROJECT, root=root)
    assert st["suspects"] == [] and st["force_full_close"] is False
    assert st["last_green_sha"] == first


def test_corrupt_file_declines_until_a_sentinel_rewrites_it(sw):
    cfg = sw["cfg"]
    path = SN.state_path(cfg, PROJECT)
    path.parent.mkdir(parents=True)
    path.write_text("{nope")
    assert SN.reuse_blocker(cfg, PROJECT) == SN.UNREADABLE
    assert SN.record_merge(cfg, PROJECT, "c", "f" * 40) is None
    assert path.read_text() == "{nope", "left in place"
    out = SN.run(cfg, PROJECT, project_cfg=sw["pcfg"])
    assert out["ok"]
    assert SN.reuse_blocker(cfg, PROJECT) is None
    assert json.loads(path.read_text())["last_green_sha"] == out["sha"]


def test_interrupted_run_settles_nothing(sw):
    cfg = sw["cfg"]
    stop = threading.Event()
    stop.set()
    out = SN.run(cfg, PROJECT, project_cfg=sw["pcfg"], stop=stop)
    assert out["interrupted"] and _sentinel_events(cfg) == []


# ---- scheduling ----------------------------------------------------------------------------

def _due(cfg):
    SN._write(SN.state_path(cfg, PROJECT), _st(reuses_since_green=3))


@pytest.fixture
def sched(sw, monkeypatch):
    started = []
    monkeypatch.setattr(P, "project_config", lambda cfg, p: sw["pcfg"])
    monkeypatch.setattr(SN, "run", lambda cfg, p, **kw: started.append(p) or {"ok": True})
    ctx = SimpleNamespace(cfg=sw["cfg"], stop=threading.Event())
    return sw, ctx, started


def test_scheduled_when_due_and_no_slot_waiter(sched):
    sw, ctx, started = sched
    pool = Q.StepPool()
    assert Q._sentinel_step(ctx, pool, []) is None          # nothing recorded: not due
    _due(sw["cfg"])
    assert Q._sentinel_step(ctx, pool, []) == PROJECT
    pool.sentinel[1].result()
    assert started == [PROJECT]
    assert Q._sentinel_step(ctx, pool, []) == PROJECT        # harvested, still due: again
    pool.shutdown()


def test_not_scheduled_while_a_campaign_waits_for_a_slot(sched):
    sw, ctx, started = sched
    _due(sw["cfg"])
    marker = gate_slots.slots_dir(sw["cfg"]) / "waiting-X.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"campaign": "X", "pid": os.getpid(), "since": "now"}))
    pool = Q.StepPool()
    assert Q._sentinel_step(ctx, pool, []) is None
    marker.unlink()
    assert Q._sentinel_step(ctx, pool, [{"id": "G1", "state": "GATING"}]) is None
    assert Q._sentinel_step(ctx, pool, []) == PROJECT
    pool.sentinel[1].result()
    pool.shutdown()


def test_not_scheduled_when_disabled(sched):
    sw, ctx, started = sched
    sw["pcfg"]["sentinel"] = {**SCFG, "enabled": False}
    _due(sw["cfg"])
    assert Q._sentinel_step(ctx, Q.StepPool(), []) is None


# ---- CLI -----------------------------------------------------------------------------------

def _cli(cfg, monkeypatch, args):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    return CliRunner().invoke(cli, args)


def test_cli_run_and_clear(sw, monkeypatch):
    cfg = sw["cfg"]
    monkeypatch.setattr(P, "project_config", lambda c, p: sw["pcfg"])
    _merge_on_main(sw, "broken.txt")
    r = _cli(cfg, monkeypatch, ["queue", "sentinel", "run", "--project", PROJECT, "--json"])
    assert r.exit_code == 1, r.output
    out = json.loads(r.output)
    assert out["schema"] == "nw.queue.sentinel.v1" and out["ok"] is False
    assert out["state"]["force_full_close"] is True
    r = _cli(cfg, monkeypatch, ["queue", "sentinel", "clear", "--project", PROJECT])
    assert r.exit_code == 0 and "cleared" in r.output
    assert SN.read(cfg, PROJECT)["force_full_close"] is False
    git.git(sw["root"], "rm", "-q", "broken.txt")
    git.git(sw["root"], "commit", "-q", "-m", "fix")
    r = _cli(cfg, monkeypatch, ["queue", "sentinel", "run", "--project", PROJECT])
    assert r.exit_code == 0 and "green" in r.output, r.output


def test_drain_sees_a_running_sentinel():
    pool = Q.StepPool()
    assert not Q._sentinel_busy(pool)
    gate = threading.Event()
    pool.sentinel = (PROJECT, pool.submit(gate.wait))
    assert Q._sentinel_busy(pool)
    gate.set()
    pool.sentinel[1].result()
    assert not Q._sentinel_busy(pool)
    pool.shutdown()


# ---- retry backoff, run lock, setup marker (review loop 2) ---------------------------------

def _wait_done(pool):
    pool.sentinel[1].result()


@pytest.fixture
def real_sched(sw, monkeypatch):
    """The runner's sentinel step with the real `SN.run`."""
    monkeypatch.setattr(P, "project_config", lambda cfg, p: sw["pcfg"])
    ctx = SimpleNamespace(cfg=sw["cfg"], stop=threading.Event())
    pool = Q.StepPool()
    yield sw, ctx, pool
    pool.shutdown()


def test_red_sentinel_not_resubmitted_at_the_same_sha_and_escalates_once(real_sched,
                                                                         monkeypatch):
    sw, ctx, pool = real_sched
    cfg = sw["cfg"]
    red = _merge_on_main(sw, "broken.txt")
    _due(cfg)
    assert Q._sentinel_step(ctx, pool, []) == PROJECT
    _wait_done(pool)
    assert Q._sentinel_step(ctx, pool, []) is None, "red at an unmoved base: no rerun"
    assert Q._sentinel_step(ctx, pool, []) is None
    assert len(_sentinel_events(cfg)) == 1 and len(ESC.list_all(cfg, "open")) == 1
    assert "red at" in SN.backoff(cfg, PROJECT, red)
    # past the retry interval the same sha runs again, red again, but escalates no more
    monkeypatch.setattr(SN, "RETRY_S", 0)
    assert Q._sentinel_step(ctx, pool, []) == PROJECT
    _wait_done(pool)
    assert len(_sentinel_events(cfg)) == 2 and len(ESC.list_all(cfg, "open")) == 1
    # a cleared red is not undone at the same sha, whatever the age
    SN.clear(cfg, PROJECT)
    assert Q._sentinel_step(ctx, pool, []) is None
    assert SN.read(cfg, PROJECT)["force_full_close"] is False
    assert len(_sentinel_events(cfg)) == 2
    # the base moves (a fix): due again, it runs and goes green
    git.git(sw["root"], "rm", "-q", "broken.txt")
    git.git(sw["root"], "commit", "-q", "-m", "fix")
    assert Q._sentinel_step(ctx, pool, []) == PROJECT
    _wait_done(pool)
    assert _sentinel_events(cfg)[-1]["data"]["ok"] is True
    assert Q._sentinel_step(ctx, pool, []) is None
    assert len(ESC.list_all(cfg, "open")) == 1


def test_green_at_the_same_sha_is_not_rerun_and_settles_only_ancestors(real_sched):
    """Green removes only suspects that are X or its ancestors (section 5). A suspect git does
    not know stays silently (git cannot say); one git knows but that is not on the base
    (a rewritten base) stays too and is named on the event as `orphans`."""
    sw, ctx, pool = real_sched
    cfg, root = sw["cfg"], sw["root"]
    first = _merge_on_main(sw, "a.txt")
    SN.record_merge(cfg, PROJECT, "c-a", first, root=root, project_cfg=sw["pcfg"])
    unknown = f"{10:040x}"
    side = git.git(root, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "off main").stdout.strip()
    SN.record_merge(cfg, PROJECT, "c-unknown", unknown, root=root, project_cfg=sw["pcfg"])
    SN.record_merge(cfg, PROJECT, "c-side", side, root=root, project_cfg=sw["pcfg"])
    assert Q._sentinel_step(ctx, pool, []) == PROJECT
    _wait_done(pool)
    st = SN.read(cfg, PROJECT)
    assert st["suspects"] == [{"campaign": "c-unknown", "merge_sha": unknown},
                              {"campaign": "c-side", "merge_sha": side}]
    assert st["reuses_since_green"] == 2 and st["force_full_close"] is False
    ev = _sentinel_events(cfg)[-1]["data"]
    assert ev["suspects"] == [{"campaign": "c-a", "merge_sha": first}]
    assert ev["orphans"] == [{"campaign": "c-side", "merge_sha": side}]
    assert "dropped" not in ev
    # even if something keeps it due, a green base sha is not rerun until the base moves
    _due(cfg)
    assert Q._sentinel_step(ctx, pool, []) is None
    assert "already green" in SN.backoff(cfg, PROJECT, first)
    assert SN.backoff(cfg, PROJECT, "0" * 40) is None


def _git_failing(monkeypatch, *verbs):
    real = git.git

    def flaky(repo, *args, **kw):
        if args and args[0] in verbs:
            raise git.GitError(["git", *args], -1, "timed out after 60s")
        return real(repo, *args, **kw)
    monkeypatch.setattr(git, "git", flaky)


def test_a_git_failure_keeps_the_suspect_and_settle_still_finishes(sw, monkeypatch):
    cfg, root = sw["cfg"], sw["root"]
    first = _merge_on_main(sw, "a.txt")
    later = _merge_on_main(sw, "b.txt")
    SN.record_merge(cfg, PROJECT, "c-a", first, root=root, project_cfg=sw["pcfg"])
    SN.record_merge(cfg, PROJECT, "c-b", later, root=root, project_cfg=sw["pcfg"])
    _git_failing(monkeypatch, "merge-base", "rev-parse", "cat-file")
    settled = SN.settle(cfg, PROJECT, later, True, root=root, project_cfg=sw["pcfg"],
                        base="main")
    assert settled == [{"campaign": "c-b", "merge_sha": later}], "the tested sha itself"
    st = SN.read(cfg, PROJECT)
    assert st["suspects"] == [{"campaign": "c-a", "merge_sha": first}], "kept, not dropped"
    assert st["reuses_since_green"] == 1 and st["last_green_sha"] == later
    (ev,) = _sentinel_events(cfg)
    assert ev["data"]["ok"] is True and "orphans" not in ev["data"]
    assert SN._orphans(root, "main", st["suspects"]) == []


def test_an_unresolvable_base_never_leaves_the_tick(real_sched, monkeypatch):
    """A due project whose `merge.base` no longer resolves (renamed branch, re-cloned repo):
    the runner's tick journals `sentinel_failed` once and goes on; no run is started."""
    sw, ctx, pool = real_sched
    cfg = sw["cfg"]
    sw["pcfg"]["merge"] = {**sw["pcfg"]["merge"], "base": "renamed-away"}
    _due(cfg)
    runs = []
    monkeypatch.setattr(SN, "run", lambda *a, **k: runs.append(a) or {})
    for _ in range(3):
        assert Q._sentinel_step(ctx, pool, []) is None
    assert runs == [] and pool.sentinel is None
    failed = [r for r in Q.read_journal(cfg) if r.get("kind") == "sentinel_failed"]
    assert len(failed) == 1 and failed[0]["project"] == PROJECT
    assert "renamed-away" in failed[0]["error"]
    # the same through a whole tick of the run loop (the step runs only with a pool)
    Q.tick(cfg, pool=pool, stop=ctx.stop)
    assert runs == []


def test_any_sentinel_error_is_journaled_not_raised_by_the_tick(real_sched, monkeypatch):
    sw, ctx, pool = real_sched

    def boom(cfg):
        raise RuntimeError("state dir exploded")
    monkeypatch.setattr(SN, "projects", boom)
    Q.tick(sw["cfg"], pool=pool, stop=ctx.stop)
    failed = [r for r in Q.read_journal(sw["cfg"]) if r.get("kind") == "sentinel_failed"]
    assert failed and "state dir exploded" in failed[-1]["error"]


def test_cli_run_and_clear_report_git_errors_cleanly(sw, monkeypatch):
    cfg = sw["cfg"]
    sw["pcfg"]["merge"] = {**sw["pcfg"]["merge"], "base": "renamed-away"}
    monkeypatch.setattr(P, "project_config", lambda c, p: sw["pcfg"])
    r = _cli(cfg, monkeypatch, ["queue", "sentinel", "run", "--project", PROJECT])
    assert r.exit_code == 1 and "Error:" in r.output, r.output
    assert r.exception is None or isinstance(r.exception, SystemExit), r.exception
    _git_failing(monkeypatch, "merge-base", "rev-parse", "cat-file")
    r = _cli(cfg, monkeypatch, ["queue", "sentinel", "clear", "--project", PROJECT])
    assert r.exception is None or isinstance(r.exception, SystemExit), r.exception


def test_exception_backs_off_and_a_failed_setup_is_rerun(real_sched, tmp_path):
    sw, ctx, pool = real_sched
    cfg = sw["cfg"]
    flag, ran = tmp_path / "setup-may-pass", tmp_path / "setup-ran"
    sw["pcfg"]["worktrees"] = {"setup": [f"test -f {flag}", f"touch {ran}"]}
    _due(cfg)
    assert Q._sentinel_step(ctx, pool, []) == PROJECT
    with pytest.raises(git.GitError):
        _wait_done(pool)
    assert Q._sentinel_step(ctx, pool, []) is None, "harvested; a failure backs off"
    last = SN.last_run(cfg, PROJECT)
    assert last["ok"] is None and "exited 1" in last["error"]
    assert "failed at" in SN.backoff(cfg, PROJECT, last["sha"])
    assert not ran.exists()
    flag.touch()
    out = SN.run(cfg, PROJECT, project_cfg=sw["pcfg"])     # the worktree exists: setup reruns
    assert out["ok"] and ran.exists()
    ran.unlink()
    assert SN.run(cfg, PROJECT, project_cfg=sw["pcfg"])["ok"]
    assert not ran.exists(), "a finished setup is not repeated"


def test_concurrent_cli_run_is_refused_while_the_runner_run_holds_the_lock(real_sched,
                                                                           monkeypatch):
    sw, ctx, pool = real_sched
    cfg = sw["cfg"]
    at_gate, release = threading.Event(), threading.Event()

    def hold(_sha):
        at_gate.set()
        assert release.wait(waits.WAIT_S)

    _due(cfg)
    t = threading.Thread(target=lambda: SN.run(cfg, PROJECT, project_cfg=sw["pcfg"],
                                               _hooks={"after_reset": hold}))
    t.start()
    try:
        assert at_gate.wait(waits.WAIT_S)
        assert SN.running(cfg, PROJECT)
        (SN.worktree_path(cfg, PROJECT) / "suite-output.tmp").write_text("x")
        r = _cli(cfg, monkeypatch, ["queue", "sentinel", "run", "--project", PROJECT])
        assert r.exit_code == 1 and "already in progress" in r.output, r.output
        assert (SN.worktree_path(cfg, PROJECT) / "suite-output.tmp").exists(), "not cleaned"
        assert Q._sentinel_step(ctx, pool, []) is None, "the runner skips a locked project"
        assert pool.sentinel is None
    finally:
        release.set()
        waits.join(t)
    assert not SN.running(cfg, PROJECT)
    assert len(_sentinel_events(cfg)) == 1


def test_drain_waiting_on_lists_a_running_sentinel(sw):
    pool = Q.StepPool()
    assert Q._waiting_on(sw["cfg"], pool) == []
    gate = threading.Event()
    pool.sentinel = (PROJECT, pool.submit(gate.wait))
    (w,) = Q._waiting_on(sw["cfg"], pool)
    assert w["kind"] == "sentinel" and w["campaign"] == f"sentinel-{PROJECT}"
    assert w["timeout_s"]
    gate.set()
    pool.sentinel[1].result()
    pool.shutdown()


# ---- SPEC-0379: the clock trigger and worker placement ------------------------------------------

def _icfg(hours=..., **extra):
    sec = {} if hours is ... else {"interval_hours": hours}
    return {"agentic": {"sentinel": sec, **extra}}


@pytest.mark.parametrize("raw,hours", [(..., 6.0), (True, 6.0), (6, 6.0), (1.5, 1.5), ("12", 12.0),
                                       (0, 0.0), (False, 0.0), (None, 0.0), (-3, 0.0)])
def test_interval_hours_default_and_disable_values(raw, hours):
    assert SN.interval_hours(_icfg(raw)) == hours
    assert SN.DEFAULT_INTERVAL_HOURS == 6.0


def test_interval_hours_rejects_a_non_number():
    with pytest.raises(ValueError, match="interval_hours"):
        SN.interval_hours(_icfg("often"))
    assert SN.interval_hours({}) == 6.0                       # no agentic section at all


def _time_due(world, hours=..., *, base="b" * 40, now=NOW, pcfg=None, **st):
    cfg = {**world["cfg"], **_icfg(hours)}
    SN._write(SN.state_path(cfg, PROJECT), _st(**st))
    return SN.time_due(cfg, PROJECT, base, project_cfg=pcfg or {"sentinel": SCFG}, now=now)


def test_time_due_when_the_base_moved_and_the_last_green_is_older_than_the_interval(world):
    old = dict(last_green_sha="a" * 40, last_green_at=_iso(-7 * 3600))
    assert _time_due(world, **old)                                        # 7h > 6h default
    assert not _time_due(world, last_green_sha="a" * 40, last_green_at=_iso(-5 * 3600))   # not yet
    assert _time_due(world, last_green_sha="a" * 40)                      # never timestamped
    assert _time_due(world)                                               # never green at all


def test_time_due_not_when_the_base_is_the_last_green_sha(world):
    assert not _time_due(world, last_green_sha="b" * 40, last_green_at=_iso(-30 * 86400))


@pytest.mark.parametrize("off", [0, False, None, -1])
def test_time_due_disabled_by_the_interval(world, off):
    assert not _time_due(world, off, last_green_sha="a" * 40, last_green_at=_iso(-30 * 86400))


def test_time_due_disabled_with_the_project_sentinel(world):
    assert not _time_due(world, pcfg={"sentinel": {**SCFG, "enabled": False}},
                         last_green_sha="a" * 40, last_green_at=_iso(-30 * 86400))


def test_time_due_follows_an_interval_change(world):
    st = dict(last_green_sha="a" * 40, last_green_at=_iso(-7 * 3600))
    assert _time_due(world, 6, **st)
    assert not _time_due(world, 12, **st)
    assert _time_due(world, 0.5, **st)


def test_time_due_on_an_unreadable_state(world):
    cfg = world["cfg"]
    path = SN.state_path(cfg, PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{nope")
    assert SN.time_due(cfg, PROJECT, "b" * 40, project_cfg={"sentinel": SCFG}, now=NOW)


def _stale_green(sw, sha="a" * 40):
    SN._write(SN.state_path(sw["cfg"], PROJECT),
              _st(last_green_sha=sha, last_green_at=_iso(-30 * 86400)))


def test_clock_trigger_schedules_a_run_on_a_moved_base(sched):
    sw, ctx, started = sched
    _stale_green(sw)                                     # not due by count, due on the clock
    assert not SN.check_due(sw["cfg"], PROJECT, project_cfg=sw["pcfg"])
    pool = Q.StepPool()
    assert Q._sentinel_step(ctx, pool, []) == PROJECT
    pool.sentinel[1].result()
    assert started == [PROJECT]
    pool.shutdown()


def test_clock_trigger_off_unmoved_base_or_no_close_gates(sched):
    sw, ctx, started = sched
    _stale_green(sw, git.rev_parse(sw["root"]))          # green at the current base
    assert Q._sentinel_step(ctx, Q.StepPool(), []) is None
    _stale_green(sw)
    sw["cfg"]["agentic"]["sentinel"] = {"interval_hours": 0}
    assert Q._sentinel_step(ctx, Q.StepPool(), []) is None
    sw["cfg"]["agentic"]["sentinel"] = {"interval_hours": 6}
    sw["pcfg"]["gates"] = [g for g in sw["pcfg"]["gates"] if g.get("when") != "close"]
    assert Q._sentinel_step(ctx, Q.StepPool(), []) is None
    assert started == []


def test_placement_prefers_worker_pools_and_never_local_while_one_is_placeable(sw, monkeypatch):
    from nightwright.agentic import remote_gates as RG
    from nightwright.agentic import workers as WK
    wt = SN.worktree_path(sw["cfg"], PROJECT)
    monkeypatch.setattr(WK, "gate_pools", lambda cfg, *a, **k: [("box", 6)])
    monkeypatch.setattr(RG, "ineligible", lambda *a: None)
    monkeypatch.setattr(RG, "header_refusal", lambda *a: None)
    pools, repool, state = SN._placement(sw["cfg"], PROJECT, sw["pcfg"], wt, 1)
    assert pools == [("box", 6)] and repool() == [("box", 6)]
    assert state["id"] == f"sentinel-{PROJECT}" and state["spec_id"] is None
    assert state["id"].startswith("sentinel-")           # affected_tests.py: full suite
    assert state["repo"] == {"worktree": str(wt), "base": SN._base(sw["pcfg"])}
    monkeypatch.setattr(RG, "ineligible", lambda *a: {"reason": "untracked"})
    assert SN._placement(sw["cfg"], PROJECT, sw["pcfg"], wt, 1)[:2] == (None, None)
    monkeypatch.setattr(RG, "ineligible", lambda *a: None)
    monkeypatch.setattr(WK, "gate_pools", lambda cfg, *a, **k: [(WK.LOCAL, 2)])
    assert SN._placement(sw["cfg"], PROJECT, sw["pcfg"], wt, 1)[:2] == (None, None)


def _fake_slot(pool):
    import contextlib

    @contextlib.contextmanager
    def acquire(cfg, owner, **kw):
        acquire.kw = kw
        yield SimpleNamespace(pool=pool, interrupted=False)
    return acquire


def test_run_on_a_worker_slot_runs_the_close_gates_there(sw, monkeypatch):
    from nightwright.agentic import gates as G
    from nightwright.agentic import remote_gates as RG
    from nightwright.agentic import workers as WK
    monkeypatch.setattr(WK, "gate_pools", lambda cfg, *a, **k: [("box", 6)])
    monkeypatch.setattr(RG, "ineligible", lambda *a: None)
    monkeypatch.setattr(RG, "header_refusal", lambda *a: None)
    acq = _fake_slot("box")
    monkeypatch.setattr(gate_slots, "acquire", acq)
    calls = []

    def detached(worker, pcfg, state, cfg, seq, gates_dir, stop=None):
        calls.append((worker, [g["name"] for g in pcfg["gates"]], state["id"]))
        res = G.GateResult(name="full", required=True, ok=True, exit_code=0, timed_out=False,
                           duration_s=1.0, log="001-full.log", tail="")
        return G.GateRun(seq=seq, ok=True, results=(res,), started_at="", duration_s=1.0,
                         worker=worker)
    monkeypatch.setattr(RG, "run_detached", detached)
    out = SN.run(sw["cfg"], PROJECT, project_cfg=sw["pcfg"])
    assert out["ok"] and calls == [("box", ["full"], f"sentinel-{PROJECT}")]
    assert acq.kw["pools"] == [("box", 6)] and acq.kw["repool"] is not None
    assert SN.read(sw["cfg"], PROJECT)["last_green_sha"] == out["sha"]


def test_remote_transport_failure_is_a_failed_run_not_a_local_fallback(sw, monkeypatch):
    from nightwright.agentic import remote_gates as RG
    from nightwright.agentic import workers as WK
    monkeypatch.setattr(WK, "gate_pools", lambda cfg, *a, **k: [("box", 6)])
    monkeypatch.setattr(RG, "ineligible", lambda *a: None)
    monkeypatch.setattr(RG, "header_refusal", lambda *a: None)
    monkeypatch.setattr(gate_slots, "acquire", _fake_slot("box"))

    def boom(*a, **k):
        raise RG.RemoteFailure("transport", "ssh died")
    monkeypatch.setattr(RG, "run_detached", boom)
    ran_local = []
    monkeypatch.setattr(SN.G, "run_gate_with_retries", lambda *a, **k: ran_local.append(a))
    with pytest.raises(RuntimeError, match="remote sentinel on box failed"):
        SN.run(sw["cfg"], PROJECT, project_cfg=sw["pcfg"])
    assert ran_local == []


# ---- SPEC-0420: a red settle opens a red episode and never escalates ------------------------

def test_red_settle_opens_an_episode_and_never_escalates(world, monkeypatch):
    from nightwright.agentic import redmain as RM
    cfg, root = world["cfg"], world["root"]
    cfg["agentic"] = {"escalations": {"notify": "none"}}         # red_main on (the default)

    def boom(*a, **k):
        raise AssertionError("plan_escalate must not be called on a red sentinel")
    monkeypatch.setattr(ESC, "plan_escalate", boom)
    sha = git.rev_parse(root)
    for _ in range(2):                                   # same sha twice: still no escalation
        SN.settle(cfg, PROJECT, sha, False, ["t.py::test_x"], root=str(root))
    rec = RM.read(cfg, PROJECT)
    assert rec["open"] and rec["episode"] == 1 and rec["runs"] == 2
    assert ESC.list_all(cfg) == []
    assert len(E.read_since(cfg, 0, types=["red_main"])) == 1
    SN.settle(cfg, PROJECT, sha, True, root=str(root))
    assert not RM.is_red(cfg, PROJECT)
