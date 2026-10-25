"""SPEC-0220: the close's worktree phase (guard, DoD, rebase, re-gate) runs on the StepPool;
only the finish phase (merge, push, PR, cleanup) runs on the main thread. "A is still
re-gating" is deterministic: the project's gate blocks while `hold-<spec>` exists, but only from
its second run on, so the GATING gate passes and the close's re-gate holds."""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from test_agentic_e2e import (SPEC_DONE, _git, fake_gh, impl_done, make_cfg, make_repo,
                              review, run_cli, script)
from test_agentic_nonblocking import _second_spec
from test_agentic_reexec import _code_repo, _commit
from wt.agentic import close as CL
from wt.agentic import events as E
from wt.agentic import gitops as git
from wt.agentic import lease as L
from wt.agentic import queue as Q
from wt.agentic import store as S
from wt.agentic.runner import FakeRunner

SPECS = {"SPEC-0001": ("docs/specs/0001-trivial.md", "feature-a.txt"),
         "SPEC-0002": ("docs/specs/0002-second.md", "feature-b.txt")}


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

    def tick(self, **kw):
        t0 = time.monotonic()
        r = Q.tick(self.cfg, runner=self.runner, pool=self.pool, stop=self.stop, **kw)
        assert time.monotonic() - t0 < 5, "a tick blocked"
        self.reports.append(r)
        return r

    def spin(self, cond, timeout=40):
        end = time.monotonic() + timeout
        while not cond():
            assert time.monotonic() < end, "condition never met"
            self.tick()
            time.sleep(0.03)

    def settle(self, cid, timeout=20):
        end = time.monotonic() + timeout
        while cid in self.pool.flights and not self.pool.flights[cid].done():
            assert time.monotonic() < end, f"{cid} never finished"
            time.sleep(0.02)

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
        for p in env.d.glob("hold-*"):
            p.unlink()
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
    env.release()
    env.spin(lambda: env.state(a) == "MERGED" and env.state(b) == "MERGED")


def test_two_worktree_phases_concurrent_merges_serial(mk):
    env = mk(slots=2, specs=2)
    a, b = env.add("SPEC-0001"), env.add("SPEC-0002")
    env.hold("SPEC-0001")
    env.hold("SPEC-0002")
    env.spin(lambda: env.running("SPEC-0001") and env.running("SPEC-0002"))
    assert env.state(a) == env.state(b) == "CLOSING"
    env.release("SPEC-0001")
    env.release("SPEC-0002")
    env.spin(lambda: env.state(a) == "MERGED" and env.state(b) == "MERGED")
    moved = [r for c in (a, b) for r in env.rows(c) if r["reason"] == "base_moved"]
    assert len(moved) == 1
    assert sorted(len(env.started(c)) for c in (a, b)) == [1, 2]
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
    assert env.st(a)["closing"] == {"attempts": 0}
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
    assert env.st(a)["closing"] == {"attempts": 0}
    with S.item_lock(env.cfg, a):                 # legacy state, then a WAITING detour
        st = env.st(a)
        st.pop("closing")
        st["state"] = "WAITING"
        st["waiting"] = {"resume_to": "CLOSING", "since": S.now_iso(), "reason": "x"}
        S.write_state(env.cfg, a, st)
    env.hold()
    env.spin(lambda: env.running())
    assert env.st(a)["closing"] == {"attempts": 0} and env.state(a) == "CLOSING"
    env.release()
    env.spin(lambda: env.state(a) == "MERGED")
    st = env.st(a)
    assert "closing" not in st and st["counters"]["close_attempts"] == 1


def test_legacy_closing_without_key(mk):
    env = mk()
    a = env.add()
    env.to_closing(a)
    with S.item_lock(env.cfg, a):
        st = env.st(a)
        st.pop("closing")
        S.write_state(env.cfg, a, st)
    Q.tick(env.cfg, runner=env.runner)
    assert env.state(a) == "MERGED"


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
    assert _git(wt, "rev-parse", "--git-path", "rebase-merge")
    assert Path(wt, _git(wt, "rev-parse", "--git-path", "rebase-merge")).exists() \
        or Path(_git(wt, "rev-parse", "--git-path", "rebase-merge")).exists()
    Q.tick(env.cfg, runner=env.runner)
    assert env.state(a) == "MERGED" and (env.repo / "feature-a.txt").exists()


def test_watchdog_quiet_while_closing(mk):
    env = mk(slots=2, specs=2)
    a = _held_closing(env)
    b = env.add("SPEC-0002")
    lease = L.acquire(env.cfg, ["t"])
    try:
        sett = dict(E.settings(env.cfg), ready_grace_s=0)
        env.tick(admit=False)                      # B is ready but not admitted
        time.sleep(0.05)
        items = E.ready_not_admitted_items(env.cfg, _now(), sett)
        assert [i["campaign"] for i in items] == [b] and items[0]["runner_busy"] is True
        env.tick()                                 # the real runner admits it mid-close
        assert env.state(b) != "QUEUED" and env.state(a) == "CLOSING"
        assert E.ready_not_admitted_items(env.cfg, _now(), sett) == []
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
            _commit(root, "src/wt/x.py", "A = 9\n", "code")
            threading.Timer(0.6, env.release).start()
        return report

    seen = []

    def fake_exec(exe, argv, envv):
        seen.append({
            "state": S.read_state(env.cfg, a)["state"],
            "alive": [t.name for t in threading.enumerate() if t.name.startswith("wt-step")],
            "journal": [(r["kind"], r.get("drain"), r.get("reason"))
                        for r in Q.read_journal(env.cfg)],
            "handover": envv.get(L.HANDOVER_ENV)})

    monkeypatch.setattr(Q, "tick", tick)
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
