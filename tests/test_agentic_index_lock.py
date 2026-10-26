"""SPEC-0244: an adopted implementer flight clears a stale .git/index.lock before committing."""
from __future__ import annotations

import functools
import os
import subprocess
import threading
import types
from pathlib import Path

import pytest

from test_agentic_adopt import die, start_impl, tick_until, world  # noqa: F401  (fixture)
from test_agentic_e2e import _git, make_repo
from test_agentic_runproc import until
from wt.agentic import gitops as G
from wt.agentic import queue as Q
from wt.agentic import store as S

_KEYS = {"path", "removed", "reason", "holders"}


# ---- index_lock_path ----------------------------------------------------------------------

def test_index_lock_path_main_checkout_and_linked_worktree(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    git_dir = (Path(repo) / ".git").resolve()
    assert G.index_lock_path(repo).resolve() == git_dir / "index.lock"
    wt = tmp_path / "linked"
    G.worktree_add(repo, wt, "feature", "main")
    lock = G.index_lock_path(wt)
    assert lock.is_absolute()
    assert lock.resolve() == git_dir / "worktrees" / "linked" / "index.lock"


# ---- lock_holders -------------------------------------------------------------------------

def _fake_proc(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    return proc


def _pid(proc, n, *, cwd=None, fds=()):
    d = proc / str(n)
    (d / "fd").mkdir(parents=True)
    if cwd is not None:
        (d / "cwd").symlink_to(cwd)
    for i, target in enumerate(fds):
        (d / "fd" / str(i)).symlink_to(target)
    return d


def test_lock_holders_fake_proc(tmp_path):
    wt = tmp_path / "wt"
    (wt / "sub").mkdir(parents=True)
    lock = tmp_path / "gitdir" / "index.lock"
    lock.parent.mkdir()
    lock.write_text("")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    proc = _fake_proc(tmp_path)
    _pid(proc, 11, cwd=elsewhere, fds=[tmp_path / "nothing", lock])     # fd -> the lock
    _pid(proc, 12, cwd=wt / "sub")                                      # cwd inside the worktree
    _pid(proc, 13, cwd=elsewhere)                                       # unrelated
    _pid(proc, 14, cwd=tmp_path / "wt-other")                           # sibling with same prefix
    _pid(proc, os.getpid(), cwd=wt)                                     # ourselves: skipped
    (proc / "15").mkdir()                                               # no cwd/fd: OSError, skipped
    (proc / "self").mkdir()                                             # not a pid
    assert G.lock_holders(lock, wt, proc=proc) == [11, 12]
    assert G.lock_holders(lock, wt, proc=tmp_path / "missing") is None


def test_lock_holders_real_proc_sees_a_process_in_the_worktree(tmp_path):
    if not Path("/proc/self/cwd").exists():
        pytest.skip("no /proc")
    wt = tmp_path / "wt"
    wt.mkdir()
    p = subprocess.Popen(["sleep", "30"], cwd=wt)
    try:
        assert until(lambda: p.pid in (G.lock_holders(tmp_path / "x.lock", wt) or []))
    finally:
        p.kill()
        p.wait()
    assert p.pid not in G.lock_holders(tmp_path / "x.lock", wt)


# ---- clear_stale_index_lock ---------------------------------------------------------------

class FakeClock:
    def __init__(self, on_sleep=None):
        self.now, self.sleeps, self.on_sleep = 0.0, 0, on_sleep

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.now += s
        self.sleeps += 1
        if self.on_sleep:
            self.on_sleep(self.sleeps)


@pytest.fixture
def lockrepo(tmp_path):
    repo = make_repo(tmp_path)
    lock = G.index_lock_path(repo)
    return repo, lock, _fake_proc(tmp_path)


def test_clear_no_lock_is_none(lockrepo):
    repo, lock, proc = lockrepo
    assert G.clear_stale_index_lock(repo, proc=proc, clock=FakeClock()) is None


def test_clear_no_holders_removes(lockrepo):
    repo, lock, proc = lockrepo
    lock.write_text("")
    out = G.clear_stale_index_lock(repo, proc=proc, clock=FakeClock())
    assert out == {"path": str(lock), "removed": True, "reason": None, "holders": []}
    assert not lock.exists()


def test_clear_held_throughout_is_kept(lockrepo):
    repo, lock, proc = lockrepo
    lock.write_text("")
    _pid(proc, 21, cwd=repo)
    clock = FakeClock()
    out = G.clear_stale_index_lock(repo, wait_s=10, proc=proc, clock=clock)
    assert out == {"path": str(lock), "removed": False, "reason": "held", "holders": [21]}
    assert set(out) == _KEYS and lock.exists() and clock.now >= 10


def test_clear_holders_leaving_while_waiting_removes(lockrepo):
    repo, lock, proc = lockrepo
    lock.write_text("")
    holder = _pid(proc, 22, cwd=repo)

    def gone(n):
        if n == 3:
            (holder / "cwd").unlink()
    out = G.clear_stale_index_lock(repo, proc=proc, clock=FakeClock(gone))
    assert out["removed"] is True and out["holders"] == [] and set(out) == _KEYS
    assert not lock.exists()


def test_clear_lock_vanishing_while_waiting_is_none(lockrepo):
    repo, lock, proc = lockrepo
    lock.write_text("")
    _pid(proc, 23, cwd=repo)
    assert G.clear_stale_index_lock(
        repo, proc=proc, clock=FakeClock(lambda n: lock.unlink())) is None


def test_clear_unverifiable_without_proc(lockrepo):
    repo, lock, proc = lockrepo
    lock.write_text("")
    out = G.clear_stale_index_lock(repo, proc=proc / "missing", clock=FakeClock())
    assert out == {"path": str(lock), "removed": False, "reason": "unverifiable", "holders": None}
    assert lock.exists()


def test_clear_unlink_error_is_reported_not_raised(lockrepo, monkeypatch):
    repo, lock, proc = lockrepo
    lock.write_text("")

    def boom(self, *a, **k):
        raise PermissionError("denied")
    monkeypatch.setattr(Path, "unlink", boom)
    out = G.clear_stale_index_lock(repo, proc=proc, clock=FakeClock())
    assert set(out) == _KEYS and out["removed"] is False and out["holders"] == []
    assert out["reason"].startswith("error: ") and "denied" in out["reason"]


# ---- the adopted flight -------------------------------------------------------------------

def _finished_orphan(world):
    """An implementer run that finished with edits in its worktree, its runner gone."""
    pool, run_dir = start_impl(world)
    die(pool)
    world.release()
    assert until((run_dir / "exit.json").exists)
    wt = Path(S.read_state(world.cfg, world.cid)["repo"]["worktree"])
    lock = G.index_lock_path(wt)
    lock.write_text("")                                  # left by the killed runner's git
    return wt, lock


def _notes(world):
    return [e["text"] for e in S.read_ledger(world.cfg, world.cid, types=["note"])]


def test_adopted_flight_removes_stale_lock_and_commits(world):
    wt, lock = _finished_orphan(world)
    Q.tick(world.cfg, runner=world.runner)
    assert not lock.exists()
    commit, = S.read_ledger(world.cfg, world.cid, types=["commit"])
    assert commit["sha"]
    assert any(t.startswith("removed stale ") and t.endswith("index.lock (no process held it; "
                                                             "left by a killed run)")
               for t in _notes(world))


def test_adopted_flight_with_a_held_lock_keeps_it_and_parks(world, monkeypatch):
    wt, lock = _finished_orphan(world)
    monkeypatch.setattr(Q.git, "clear_stale_index_lock",
                        functools.partial(G.clear_stale_index_lock, wait_s=0.6))
    p = subprocess.Popen(["sleep", "60"], cwd=wt)
    try:
        assert until(lambda: p.pid in (G.lock_holders(lock, wt) or []))
        Q.tick(world.cfg, runner=world.runner)           # must not raise
    finally:
        p.kill()
        p.wait()
    assert lock.exists()
    st = S.read_state(world.cfg, world.cid)
    assert st["state"] == "PARKED" and "implement step failed" in st["park"]["detail"]
    assert not S.read_ledger(world.cfg, world.cid, types=["commit"])
    notes = [t for t in _notes(world) if t.startswith("index.lock present and not removed (held; ")]
    assert notes and str(p.pid) in notes[0] and str(lock) in notes[0]


def test_fresh_flight_never_calls_the_helper(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    wt = tmp_path / "wt"
    G.worktree_add(repo, wt, "feature", "main")
    (wt / "f.txt").write_text("x\n")
    G.index_lock_path(wt).write_text("")
    calls = []
    monkeypatch.setattr(Q.git, "clear_stale_index_lock", lambda *a, **k: calls.append(a))
    rr = types.SimpleNamespace(verdict={"summary": "s"}, interrupted=False)
    ctx = types.SimpleNamespace(runner=types.SimpleNamespace(run=lambda req: rr),
                                stop=threading.Event(), project=lambda s: {})
    state = {"id": "C-1", "state": "IMPLEMENTING", "repo": {"worktree": str(wt)}}
    res = Q._do_persona(ctx, state, Q.Step("implement", persona="implementer"), "abc123", None, 1.0)
    assert not calls and res.lock_cleanup is None
    assert res.commit_sha is None and "index.lock" in (res.error or "")
