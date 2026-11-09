"""SPEC-0220: the close's worktree phase (guard, DoD, rebase, re-gate) runs on the StepPool;
only the finish phase (merge, push, PR, cleanup) runs on the main thread. "A is still
re-gating" is deterministic: the project's gate blocks while `hold-<spec>` exists, but only from
its second run on, so the GATING gate passes and the close's re-gate holds."""
import json
import os
import subprocess
import threading
import time
from concurrent import futures
from pathlib import Path

import pytest

from test_agentic_e2e import (SPEC_DONE, _git, fake_gh, impl_done, make_cfg, make_repo,
                              review, run_cli, script)
from test_agentic_nonblocking import _second_spec
from test_agentic_reexec import _code_repo, _commit
from nightwright.agentic import close as CL
from nightwright.agentic import events as E
from nightwright.agentic import gitops as git
from nightwright.agentic import lease as L
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S
from nightwright.agentic.runner import FakeRunner

pytestmark = pytest.mark.integration

SPECS = {"SPEC-0001": ("docs/specs/0001-trivial.md", "feature-a.txt"),
         "SPEC-0002": ("docs/specs/0002-second.md", "feature-b.txt")}


def _no_entry(closing: dict) -> dict:
    """`closing` without its SPEC-0260 `entered_at` stamp, which must be present."""
    assert closing.get("entered_at"), closing
    return {k: v for k, v in closing.items() if k != "entered_at"}


def _gate_cmd(d):
    return f'''ls feature*.txt >/dev/null || exit 1
D={d}
id=$(basename "$PWD" | cut -d- -f1,2)
n=$(cat "$D/n-$id" 2>/dev/null || echo 0)
echo $((n+1)) > "$D/n-$id"
ls > "$D/tree-$id-$n"
if [ "$n" -ge 1 ]; then
  touch "$D/running-$id"
  while [ -e "$D/hold-$id" ]; do sleep 0.05; done
fi
'''


class Dispatch:
    """One FakeRunner per spec (the script holds the spec id), picked by the run's cwd."""

    def __init__(self, runners):
        self.runners = runners

    def run(self, req):
        for spec, r in self.runners.items():
            if spec in str(req.cwd):
                return r.run(req)
        raise AssertionError(f"no runner for {req.cwd}")


class Env:
    def __init__(self, tmp_path, monkeypatch, *, specs=1, slots=2, policy="merge",
                 with_origin=False):
        self.monkeypatch = monkeypatch
        self.d = tmp_path / "hold"
        self.d.mkdir()
        self.repo = make_repo(tmp_path, test_gate=_gate_cmd(self.d), policy=policy,
                              with_origin=with_origin)
        if specs > 1:
            _second_spec(self.repo)
        runners = {}
        first = None
        for spec in list(SPECS)[:specs]:
            rel, fname = SPECS[spec]
            entry = impl_done(spec, spec_rel=rel)
            entry["edits"][0]["path"] = fname
            p = script(tmp_path, [entry, review()], name=f"{spec}.json")
            first = first or p
            runners[spec] = FakeRunner(p)
        self.cfg = make_cfg(tmp_path, self.repo, first, profile={"slots": slots})
        self.runner = Dispatch(runners)
        self.pool = Q.StepPool()
        self.stop = threading.Event()
        self.reports = []

    def add(self, spec="SPEC-0001"):
        r = run_cli(self.cfg, self.monkeypatch, ["queue", "add", spec, "--yes", "--json"])
        assert r.exit_code == 0, r.output
        return json.loads(r.output)["campaign"]

    def hold(self, spec="SPEC-0001"):
        (self.d / f"hold-{spec}").touch()

    def release(self, spec="SPEC-0001"):
        (self.d / f"hold-{spec}").unlink(missing_ok=True)

    def running(self, spec="SPEC-0001"):
        return (self.d / f"running-{spec}").exists()

    def gate_runs(self, spec="SPEC-0001"):
        f = self.d / f"n-{spec}"
        return int(f.read_text()) if f.exists() else 0

    def release_all(self):
        for p in self.d.glob("hold-*"):
            p.unlink(missing_ok=True)

    def tick(self, **kw):
        # a tick that blocked on a held re-gate would hang until the holds go: the safety timer
        # notes that it fired, then frees them at 15 s, so the regression fails on
        # `guard_fired` (and on the vanished holds) instead of hanging, with no elapsed-time
        # bound (SPEC-0248)
        holds = sorted(self.d.glob("hold-*"))
        self.guard_fired = False

        def fire():
            self.guard_fired = True
            self.release_all()

        guard = threading.Timer(15, fire)
        guard.start()
        try:
            r = Q.tick(self.cfg, runner=self.runner, pool=self.pool, stop=self.stop, **kw)
        finally:
            guard.cancel()
        assert not self.guard_fired, "a tick blocked: the safety timer had to free the holds"
        assert all(p.exists() for p in holds), "a tick blocked: a hold was freed under it"
        self.reports.append(r)
        return r

    def spin(self, cond, max_ticks=400):
        for _ in range(max_ticks):
            if cond():
                return
            self.tick()
            self.pool.wait_any(0.2)
        assert cond(), f"condition never met in {max_ticks} ticks"

    def settle(self, cid, timeout=60):
        fl = self.pool.flights.get(cid)
        if fl is None:
            return
        futures.wait([f for _, f, _h in fl.tasks], timeout=timeout)   # a hang guard only
        assert fl.done(), f"{cid} never finished"

    def st(self, cid):
        return S.read_state(self.cfg, cid)

    def state(self, cid):
        return self.st(cid)["state"]

    def rows(self, cid):
        return [s for r in self.reports for s in r.steps if s["campaign"] == cid]

    def started(self, cid, action="close"):
        return [s for r in self.reports for s in r.started
                if s["campaign"] == cid and s["action"] == action]

    def main_sha(self):
        return _git(self.repo, "rev-parse", "main")

    def move_main(self, name):
        return _commit(self.repo, name, "x\n", name)

    def to_closing(self, cid):
        """Single-shot ticks (no pool) until the campaign is CLOSING; nothing is held yet."""
        for _ in range(30):
            if self.state(cid) == "CLOSING":
                return
            Q.tick(self.cfg, runner=self.runner)
        raise AssertionError("never reached CLOSING")


@pytest.fixture
def mk(tmp_path, monkeypatch):
    envs = []

    def make(**kw):
        env = Env(tmp_path, monkeypatch, **kw)
        envs.append(env)
        return env

    yield make
    for env in envs:
        env.stop.set()
        env.release_all()
        env.pool.shutdown()


def _held_closing(env, spec="SPEC-0001"):
    cid = env.add(spec)
    env.hold(spec)
    env.spin(lambda: env.running(spec) and env.state(cid) == "CLOSING")
    return cid


def test_admission_while_close_regate_runs(mk, tmp_path):
    env = mk(slots=2, specs=2)
    a = _held_closing(env)
    assert len(env.started(a)) == 1
    b = env.add("SPEC-0002")
    env.spin(lambda: env.state(b) not in ("QUEUED", "OPENING", "IMPLEMENTING"))
    assert env.state(a) == "CLOSING" and a in env.pool.flights
    assert len(env.started(a)) == 1
    assert env.state(b) in ("GATING", "REVIEWING", "CLOSING", "MERGED")
    # the runner journal order the manual check looks for: A's `started close`, then B's
    # rows, and A has no result row yet
    first_close = next(i for i, r in enumerate(env.reports)
                       if any(s["campaign"] == a and s["action"] == "close" for s in r.started))
    later = [s for r in env.reports[first_close + 1:] for s in r.steps]
    assert any(s["campaign"] == b for s in later)
    assert not any(s["campaign"] == a and s["to"] == "MERGED" for s in later)
    env.release()
    env.spin(lambda: env.state(a) == "MERGED" and env.state(b) == "MERGED")


def test_two_closes_go_through_the_train(mk):
    """SPEC-0260 narrows SPEC-0220: two closes of one repository no longer re-gate at once, so
    the first merge invalidates nothing."""
    env = mk(slots=2, specs=2)
    a, b = env.add("SPEC-0001"), env.add("SPEC-0002")
    env.hold("SPEC-0001")
    env.hold("SPEC-0002")
    env.spin(lambda: (env.running("SPEC-0001") or env.running("SPEC-0002"))
             and env.state(a) == env.state(b) == "CLOSING")
    assert not (env.running("SPEC-0001") and env.running("SPEC-0002"))
    env.release("SPEC-0001")
    env.release("SPEC-0002")
    env.spin(lambda: env.state(a) == "MERGED" and env.state(b) == "MERGED")
    moved = [r for c in (a, b) for r in env.rows(c) if r["reason"] == "base_moved"]
    assert moved == []
    assert sorted(len(env.started(c)) for c in (a, b)) == [1, 1]
    assert _git(env.repo, "rev-list", "--merges", "main") == ""
    assert (env.repo / "feature-a.txt").exists() and (env.repo / "feature-b.txt").exists()
    assert env.main_sha() == env.st(a)["close"]["merged_sha"] \
        or env.main_sha() == env.st(b)["close"]["merged_sha"]


def test_base_moved_during_regate_redoes_then_merges(mk):
    env = mk()
    a = _held_closing(env)
    new_base = env.move_main("other.txt")
    env.release()
    env.spin(lambda: env.state(a) == "MERGED")
    assert [r["reason"] for r in env.rows(a)].count("base_moved") == 1
    titles = [e["title"] for e in S.read_ledger(env.cfg, a, types=["finding"])]
    assert "base moved during re-gate; redoing close" in titles
    assert len(env.started(a)) == 2
    assert "other.txt" in (env.d / "tree-SPEC-0001-2").read_text()
    st = env.st(a)
    assert st["close"]["target_sha"] == new_base and "closing" not in st
    assert env.main_sha() == st["close"]["merged_sha"]


def test_base_moving_every_attempt_parks(mk):
    env = mk()
    a = _held_closing(env)
    env.move_main("m1.txt")
    env.release()
    env.settle(a)
    env.hold()                       # the redo's gate holds too
    (env.d / "running-SPEC-0001").unlink()
    env.tick()                       # harvest: retry, resubmitted
    st = env.st(a)
    assert st["state"] == "CLOSING" and st["closing"]["attempts"] == 1
    env.spin(lambda: env.running())
    env.move_main("m2.txt")
    env.release()
    env.settle(a)
    env.tick()
    st = env.st(a)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "base_moving"
    assert st["park"]["resume_to"] == "CLOSING" and "closing" not in st
    assert len(env.started(a)) == 2
    r = run_cli(env.cfg, env.monkeypatch, ["queue", "resume", a, "--yes", "--json"])
    assert r.exit_code == 0, r.output
    assert _no_entry(env.st(a)["closing"]) == {"attempts": 0}
    env.spin(lambda: env.state(a) == "MERGED")
    assert "closing" not in env.st(a)


@pytest.mark.parametrize("how", ["dirty", "off_base"])
def test_main_changed_during_regate_parks(mk, how):
    env = mk()
    a = _held_closing(env)
    if how == "dirty":
        (env.repo / "gates" / "lint.sh").write_text("true # edited\n")
    else:
        _git(env.repo, "checkout", "-q", "-b", "elsewhere")
    env.release()
    env.spin(lambda: env.state(a) == "PARKED")
    park = env.st(a)["park"]
    assert park["reason"] == ("main_dirty" if how == "dirty" else "main_not_on_base")
    assert park["resume_to"] == "CLOSING"
    assert not (env.repo / "feature-a.txt").exists()


def test_worktree_commit_after_regate_redoes(mk):
    env = mk()
    a = _held_closing(env)
    wt = env.st(a)["repo"]["worktree"]
    env.release()
    env.settle(a)
    Path(wt, "late.txt").write_text("late\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "late")
    head = _git(wt, "rev-parse", "HEAD")
    env.spin(lambda: env.state(a) == "MERGED")
    assert [r["reason"] for r in env.rows(a)].count("base_moved") == 1
    assert env.st(a)["close"]["merged_sha"] == head
    assert (env.repo / "late.txt").exists()
    assert env.gate_runs() == 3


def test_branch_tip_moved_after_regate_redoes(mk):
    """Only the branch ref moves (the worktree stays on the gated commit, detached)."""
    env = mk()
    a = _held_closing(env)
    st = env.st(a)
    wt, branch = st["repo"]["worktree"], st["repo"]["branch"]
    env.release()
    env.settle(a)
    before = env.main_sha()
    _git(wt, "checkout", "-q", "--detach")
    _git(wt, "update-ref", f"refs/heads/{branch}", before)    # the branch tip is no longer HEAD
    env.hold()                                                # keep the redo's re-gate busy
    env.tick()
    assert env.rows(a)[-1]["reason"] == "base_moved"
    assert env.main_sha() == before and not (env.repo / "feature-a.txt").exists()
    assert env.st(a)["closing"]["attempts"] == 1


def test_pr_policy_worktree_commit_after_regate_redoes(mk, monkeypatch):
    env = mk(policy="pr", with_origin=True)
    calls = {"push": 0, "pr": 0}

    def push(*args, **kw):
        calls["push"] += 1

    def pr(*args, **kw):
        calls["pr"] += 1
        return "https://example.test/pr/9"

    monkeypatch.setattr(git, "push_branch", push)
    monkeypatch.setattr(git, "gh_pr_create", pr)
    a = _held_closing(env)
    wt = env.st(a)["repo"]["worktree"]
    env.release()
    env.settle(a)
    Path(wt, "late.txt").write_text("late\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "late")
    env.tick()                                    # harvest: the gated tree is stale
    assert env.rows(a)[-1]["reason"] == "base_moved" and env.state(a) == "CLOSING"
    assert calls == {"push": 0, "pr": 0}
    env.spin(lambda: env.state(a) == "HANDED_OFF")
    assert calls == {"push": 1, "pr": 1} and env.gate_runs() == 3


def test_park_requested_during_worktree_phase_blocks_merge(mk):
    env = mk()
    a = _held_closing(env)
    before = env.main_sha()
    lease = L.acquire(env.cfg, ["t"])
    try:
        plan = Q.plan_park(env.cfg, a, "hold on")
        assert plan["mode"] == "deferred"
        Q.apply_park(env.cfg, plan)
    finally:
        lease.release()
    env.release()
    env.spin(lambda: env.state(a) == "PARKED")
    st = env.st(a)
    assert st["park"]["reason"] == "human" and st["park"]["resume_to"] == "CLOSING"
    assert env.main_sha() == before and not (env.repo / "feature-a.txt").exists()
    assert len(S.read_ledger(env.cfg, a, types=["gate"])) == 2


@pytest.mark.parametrize("to", ["ABANDONED", "PARKED"])
def test_abandon_during_worktree_phase_blocks_merge(mk, monkeypatch, to):
    env = mk()
    a = _held_closing(env)
    before = env.main_sha()
    calls = []
    monkeypatch.setattr(CL, "finish_close", lambda *a_, **k: calls.append(1))
    with S.item_lock(env.cfg, a):
        st = env.st(a)
        st["state"] = to
        S.write_state(env.cfg, a, st)
    env.release()
    env.settle(a)
    env.tick()
    assert calls == []
    assert [r["reason"] for r in env.rows(a)][-1] == "superseded_by_human"
    assert env.state(a) == to and env.main_sha() == before


def test_closing_reset_on_resume_and_waiting(mk):
    env = mk(slots=1)
    a = env.add()
    env.to_closing(a)
    assert _no_entry(env.st(a)["closing"]) == {"attempts": 0}
    with S.item_lock(env.cfg, a):                 # legacy state, then a WAITING detour
        st = env.st(a)
        st.pop("closing")
        st["state"] = "WAITING"
        st["waiting"] = {"resume_to": "CLOSING", "since": S.now_iso(), "reason": "x"}
        S.write_state(env.cfg, a, st)
    env.hold()
    env.spin(lambda: env.running())
    assert _no_entry(env.st(a)["closing"]) == {"attempts": 0} and env.state(a) == "CLOSING"
    env.release()
    env.spin(lambda: env.state(a) == "MERGED")
    st = env.st(a)
    assert "closing" not in st and st["counters"]["close_attempts"] == 1


def _moving_base(env, monkeypatch, times):
    """Wrap `prepare_close` so main gets a new commit after each of the first `times` worktree
    phases (so the finish phase finds a moved base). Returns the per-call `closing` log."""
    real = CL.prepare_close
    log = []

    def wrapped(cfg, state, *args, **kw):
        prep = real(cfg, state, *args, **kw)
        log.append((S.read_state(cfg, state["id"]).get("closing") or {}).get("attempts"))
        if len(log) <= times:
            env.move_main(f"moved{len(log)}.txt")
        return prep

    monkeypatch.setattr(CL, "prepare_close", wrapped)
    return log


def test_legacy_closing_without_key(mk, monkeypatch):
    env = mk()
    a = env.add()
    env.to_closing(a)
    with S.item_lock(env.cfg, a):
        st = env.st(a)
        st.pop("closing")
        S.write_state(env.cfg, a, st)
    log = _moving_base(env, monkeypatch, 1)
    r = Q.tick(env.cfg, runner=env.runner)
    assert env.state(a) == "MERGED"
    assert log == [None, 1]                       # no key = budget 0; the first retry makes it 1
    assert [s["reason"] for s in r.steps if s["campaign"] == a] == ["base_moved", "closed"]


def test_single_shot_tick_redoes_moved_base(mk, monkeypatch):
    """No pool: one `nw queue tick` loops the close until merged (the old in-call loop)."""
    env = mk()
    a = env.add()
    env.to_closing(a)
    log = _moving_base(env, monkeypatch, 1)
    r = Q.tick(env.cfg, runner=env.runner)
    rows = [s for s in r.steps if s["campaign"] == a]
    assert [s["reason"] for s in rows] == ["base_moved", "closed"]
    st = env.st(a)
    assert st["state"] == "MERGED" and log == [0, 1] and "closing" not in st
    assert st["close"]["merged_sha"] == env.main_sha()
    assert st["counters"]["close_attempts"] == 2


def test_single_shot_tick_parks_when_base_always_moves(mk, monkeypatch):
    env = mk()
    a = env.add()
    env.to_closing(a)
    log = _moving_base(env, monkeypatch, 99)
    before = env.main_sha()
    r = Q.tick(env.cfg, runner=env.runner)
    rows = [s for s in r.steps if s["campaign"] == a]
    assert [s["reason"] for s in rows] == ["base_moved", "base_moving"]
    st = env.st(a)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "base_moving"
    assert st["park"]["resume_to"] == "CLOSING" and "closing" not in st
    assert log == [0, 1]                          # exactly MAX_CLOSE_ATTEMPTS worktree phases
    assert env.main_sha() != before and not (env.repo / "feature-a.txt").exists()


def test_close_worker_exception_parks_open_failed(mk, monkeypatch):
    env = mk()
    a = env.add()
    env.to_closing(a)
    before = env.st(a)["counters"]

    def boom(*args, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(CL, "prepare_close", boom)
    r = Q.tick(env.cfg, runner=env.runner)
    st = env.st(a)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "open_failed"
    assert st["park"]["resume_to"] == "CLOSING" and "close step failed" in st["park"]["detail"]
    assert any("boom" in e for e in r.errors)
    assert st["counters"] == dict(before, close_attempts=1, parks=1)


@pytest.mark.parametrize("exc", [
    lambda: git.GitError(["git", "merge"], 1, "tail of git"),
    lambda: ValueError("bad config"),
    lambda: OSError("disk gone")])
def test_finish_close_exception_parks_open_failed(mk, monkeypatch, exc):
    env = mk()
    a = env.add()
    env.to_closing(a)

    def boom(*args, **kw):
        raise exc()

    monkeypatch.setattr(CL, "finish_close", boom)
    before = env.main_sha()
    Q.tick(env.cfg, runner=env.runner)
    st = env.st(a)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "open_failed"
    assert st["park"]["resume_to"] == "CLOSING" and "close step failed" in st["park"]["detail"]
    assert env.main_sha() == before
    assert len(S.read_ledger(env.cfg, a, types=["gate"])) == 2     # the worktree phase's gate kept


def test_no_close_submitted_while_stopping(mk):
    env = mk()
    a = env.add()
    env.to_closing(a)
    env.stop.set()
    env.tick()
    assert env.state(a) == "CLOSING" and env.started(a) == [] and a not in env.pool.flights
    env.stop.clear()
    env.spin(lambda: env.state(a) == "MERGED")
    assert len(env.started(a)) == 1


def test_stop_during_worktree_phase_leaves_closing(mk):
    env = mk()
    a = _held_closing(env)
    env.stop.set()
    env.release()
    env.settle(a)
    env.tick()
    st = env.st(a)
    assert st["state"] == "CLOSING" and env.rows(a)[-1]["reason"] == "interrupted"
    assert len(S.read_ledger(env.cfg, a, types=["gate"])) == 1       # only the GATING gate
    assert st["counters"]["close_attempts"] == 0
    env.stop.clear()
    env.spin(lambda: env.state(a) == "MERGED")
    assert env.st(a)["close"]["merged_sha"] == env.main_sha()


def test_pr_policy_push_in_finish_phase(mk, monkeypatch):
    env = mk(policy="pr", with_origin=True)
    calls = {"push": [], "pr": []}
    main = threading.current_thread()

    def push(*args, **kw):
        calls["push"].append(threading.current_thread())

    def pr(*args, **kw):
        calls["pr"].append(threading.current_thread())
        return "https://example.test/pr/9"

    monkeypatch.setattr(git, "push_branch", push)
    monkeypatch.setattr(git, "gh_pr_create", pr)
    a = _held_closing(env)
    env.stop.set()
    env.release()
    env.settle(a)
    env.tick()
    assert env.state(a) == "CLOSING" and calls == {"push": [], "pr": []}
    env.stop.clear()
    env.spin(lambda: env.state(a) == "HANDED_OFF")
    assert calls["push"] == [main] and calls["pr"] == [main]
    assert env.st(a)["close"]["pr_url"] == "https://example.test/pr/9"


def test_redo_aborts_leftover_rebase(mk):
    env = mk()
    a = env.add()
    env.to_closing(a)
    wt = env.st(a)["repo"]["worktree"]
    subprocess.run(["git", "-C", wt, "rebase", "-i", "main"], check=True, capture_output=True,
                   env={**os.environ, "GIT_SEQUENCE_EDITOR": "sed -i s/^pick/edit/"})
    gitdir = Path(wt, _git(wt, "rev-parse", "--git-path", "rebase-merge"))
    assert gitdir.exists()                                   # the rebase is really in progress
    Q.tick(env.cfg, runner=env.runner)
    assert env.state(a) == "MERGED" and (env.repo / "feature-a.txt").exists()


def test_watchdog_quiet_while_closing(mk, monkeypatch):
    env = mk(slots=2, specs=2)
    a = _held_closing(env)
    b = env.add("SPEC-0002")
    lease = L.acquire(env.cfg, ["t"])
    try:
        sett = dict(E.settings(env.cfg), ready_grace_s=0, stuck_after_s=0)
        monkeypatch.setattr(E, "settings", lambda cfg: sett)
        env.tick(admit=False)                      # B is ready but not admitted
        time.sleep(0.05)
        items = E.ready_not_admitted_items(env.cfg, _now(), sett)
        assert [i["campaign"] for i in items] == [b] and items[0]["runner_busy"] is True
        digest = E.ping_digest(env.cfg)
        assert [i["runner_busy"] for i in digest["ready_not_admitted"]] == [True]
        env.tick()                                 # the real runner admits it mid-close
        assert env.state(b) != "QUEUED" and env.state(a) == "CLOSING"
        assert E.ready_not_admitted_items(env.cfg, _now(), sett) == []
        # the held re-gate is inside the gate budget: busy, not stuck
        assert a not in [i["campaign"] for i in E.stuck_items(env.cfg, _now(), sett)]
        digest = E.ping_digest(env.cfg)
        assert digest["ready_not_admitted"] == []
        assert a not in [i["campaign"] for i in digest["stuck"]]
    finally:
        lease.release()


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def test_reexec_drains_inflight_close(tmp_path, monkeypatch):
    root = _code_repo(tmp_path)
    env = Env(tmp_path, monkeypatch)
    a = env.add()
    env.hold()
    real_tick = Q.tick

    def tick(*args, **kw):
        report = real_tick(*args, **kw)
        if any(s["action"] == "close" for s in report.started):   # code lands mid-close
            _commit(root, "src/nightwright/x.py", "A = 9\n", "code")
        return report

    real_journal = Q.journal

    def journal(cfg, kind, **fields):             # the held re-gate is freed once draining
        if kind == "draining":
            env.release()
        return real_journal(cfg, kind, **fields)

    seen = []

    def fake_exec(exe, argv, envv):
        seen.append({
            "state": S.read_state(env.cfg, a)["state"],
            "alive": [t.name for t in threading.enumerate() if t.name.startswith("nw-step")],
            "journal": [(r["kind"], r.get("drain"), r.get("reason"))
                        for r in Q.read_journal(env.cfg)],
            "handover": envv.get(L.HANDOVER_ENV)})

    monkeypatch.setattr(Q, "tick", tick)
    monkeypatch.setattr(Q, "journal", journal)
    monkeypatch.setattr(Q, "_EXEC", fake_exec)
    try:
        Q.run(env.cfg, runner=env.runner, max_ticks=80, poll_interval_s=0.05, reexec=True,
              code_root=root)
    finally:
        env.release()
    assert len(seen) == 1, seen
    got = seen[0]
    assert got["state"] == "MERGED" and got["alive"] == [] and got["handover"]
    assert got["journal"][-1] == ("stop", None, "code_changed")
    assert got["journal"][-2] == ("tick", True, None)
    assert len([r for r in Q.read_journal(env.cfg)
                if r["kind"] == "tick" for s in r.get("started", []) if s["action"] == "close"]) == 1
    os.environ[L.HANDOVER_ENV] = got["handover"]
    L.acquire(env.cfg, ["t"]).release()


def test_finish_rechecks_base_sha_itself(mk):
    """The base check is not left to the ff-only refusal: a stale target sha means no merge
    even when the branch could fast-forward."""
    from dataclasses import replace
    env = mk()
    a = env.add()
    env.to_closing(a)
    st = env.st(a)
    pcfg = Q._Ctx(env.cfg).project(st)
    prep = CL.prepare_close(env.cfg, st, pcfg)
    assert prep.outcome is None
    before = env.main_sha()
    oc = CL.finish_close(env.cfg, st, pcfg, replace(prep, target_sha="0" * 40), attempts=1)
    assert oc.retry and oc.next_state == "CLOSING" and env.main_sha() == before
    oc = CL.finish_close(env.cfg, st, pcfg, replace(prep, target_sha="0" * 40), attempts=2)
    assert oc.next_state == "PARKED" and oc.park["reason"] == "base_moving"
    assert env.main_sha() == before
    oc = CL.finish_close(env.cfg, st, pcfg, prep, attempts=1)
    assert oc.next_state == "MERGED" and env.main_sha() == oc.close["merged_sha"]
