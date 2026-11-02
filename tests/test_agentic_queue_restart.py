"""SPEC-0375: `nw queue restart` — drain, stop, park, resume, start; crash-safe plan file;
and `nw queue watch`'s stale-runner-code line. The runner is a fake lease-holding child
process; nothing here touches a real data dir."""
import json
import os
import socket
import subprocess
import sys

import pytest

from test_agentic_detach import _cfg, _recording_popen, children  # noqa: F401 (fixture)
from test_agentic_e2e import _git, impl_done, make_cfg, make_repo, script
from nightwright.agentic import cli_queue as CQ
from nightwright.agentic import lease as L
from nightwright.agentic import queue as Q
from nightwright.agentic import restart as RS
from nightwright.agentic import store as S

# A fake runner: takes the lease of the data dir in argv[1] (argv[2:] is its "command line",
# containing `queue run`), releases it on SIGTERM.
RUNNER = r"""
import signal, sys, time
from nightwright.agentic import lease as L
cfg = {"data_dir": sys.argv[1], "config_dir": sys.argv[1], "agentic": {"lease_ttl_s": 120}}
lease = L.acquire(cfg, sys.argv[2:])
def _bye(*_):
    lease.release()
    sys.exit(0)
signal.signal(signal.SIGTERM, _bye)
print("holding", flush=True)
while True:
    time.sleep(0.1)
"""


def _runner_argv(cfg, *extra):
    return [sys.executable, "-c", RUNNER, cfg["data_dir"], "queue", "run", *extra]


def _spawn_runner(cfg, children, *, cwd, env_extra=None, extra=("--detach", "--log", "x.log",
                                                                 "--poll", "5")):
    p = subprocess.Popen(_runner_argv(cfg, *extra), stdout=subprocess.PIPE, text=True,
                         cwd=str(cwd), env={**os.environ, **(env_extra or {})})
    children.append(p)
    assert p.stdout.readline().strip() == "holding"
    return p


def _camp(cfg, cid, state, *, priority=None, created="2026-01-01T00:00:00+00:00", **extra):
    S.create_campaign(cfg, {
        "id": cid, "kind": "implement", "spec_id": "SPEC-0001", "project": "Demo",
        "profile": "balanced", "state": state, "created_at": created,
        "repo": {"root": "/nonexistent", "base": "main", "branch": f"nw/{cid}",
                 "worktree": f"/nonexistent/{cid}"},
        **({"priority": priority} if priority is not None else {}), **extra})
    return cid


def _state(cfg, cid):
    return S.read_state(cfg, cid)


@pytest.fixture
def slots(monkeypatch):
    n = {"v": 10}
    monkeypatch.setattr(Q, "slots_for", lambda cfg, profile_name=None: n["v"])
    return n


# ---- the old runner's command --------------------------------------------------------------------

def test_runner_argv_strips_one_shot_and_detach_flags():
    assert RS.runner_argv(["nw", "queue", "run", "--detach", "--log", "/l", "--poll", "5",
                           "--once", "--max-ticks=3", "--until-idle", "--log=/m",
                           "--max-ticks", "4", "--no-reexec"]) == \
        ["nw", "queue", "run", "--poll", "5", "--no-reexec"]
    assert RS.runner_argv(["/py", "-m", "nightwright", "--x", "queue", "run"]) == \
        ["/py", "-m", "nightwright", "--x", "queue", "run"]
    assert RS.runner_argv(["nw", "queue", "tick"]) is None
    assert RS.runner_argv([]) is None


def test_clean_env_drops_handover_and_log():
    env = {"FOO": "1", L.HANDOVER_ENV: "7", Q.RUNNER_LOG_ENV: "/x", "PATH": "/bin"}
    assert RS.clean_env(env) == {"FOO": "1", "PATH": "/bin"}


def test_read_proc(tmp_path):
    me = RS.read_proc(os.getpid())
    assert me["cwd"] == os.getcwd() and me["env"].get("PATH") == os.environ.get("PATH")
    assert me["argv"] and me["argv"][-1] != ""
    assert RS.read_proc(os.getpid(), proc_root=tmp_path) is None


# ---- plan (dry run) ------------------------------------------------------------------------------

def test_plan_without_runner_is_read_only(tmp_path):
    cfg = _cfg(tmp_path)
    _camp(cfg, "low-gate", "GATING", priority=3)
    _camp(cfg, "hi-gate", "GATING", priority=1)
    _camp(cfg, "impl", "IMPLEMENTING", priority=2)
    _camp(cfg, "opening", "OPENING", priority=2, created="2026-01-02T00:00:00+00:00")
    _camp(cfg, "backlog", "QUEUED")
    before = {c: _state(cfg, c) for c in ("low-gate", "hi-gate", "impl", "opening")}
    plan = RS.plan_restart(cfg)
    assert plan["schema"] == "nw.queue.restart.v1" and plan["phase"] == "planned"
    assert plan["runner"]["held"] is False
    assert plan["command"]["source"] == "fallback"
    assert plan["command"]["argv"][-2:] == ["queue", "run"]
    assert [(t["campaign"], t["action"], t["resume_to"]) for t in plan["targets"]] == [
        ("hi-gate", "park", "GATING"), ("impl", "adopt", "IMPLEMENTING"),
        ("opening", "park", "QUEUED"), ("low-gate", "park", "GATING")]
    assert "_env" in plan and "_env" not in RS.public(plan)
    assert not RS.plan_path(cfg).exists()
    assert {c: _state(cfg, c) for c in before} == before


def test_plan_reads_the_runner_command(tmp_path, children):
    cfg = _cfg(tmp_path)
    (tmp_path / "old").mkdir()
    p = _spawn_runner(cfg, children, cwd=tmp_path / "old", env_extra={"FOO": "bar"})
    plan = RS.plan_restart(cfg)
    assert plan["runner"]["held"] and plan["runner"]["pid"] == p.pid
    assert plan["command"]["source"] == "proc"
    assert plan["command"]["argv"] == _runner_argv(cfg, "--poll", "5")
    assert plan["command"]["cwd"] == str(tmp_path / "old")
    assert plan["_env"]["FOO"] == "bar"
    assert "bar" not in json.dumps(RS.public(plan))


def test_plan_refusals(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _camp(cfg, "q", "QUEUED")
    _camp(cfg, "g", "GATING")
    with pytest.raises(ValueError, match="no such campaign"):
        RS.plan_restart(cfg, only=["nope"])
    with pytest.raises(ValueError, match="q is QUEUED, not in flight"):
        RS.plan_restart(cfg, only=["q"])
    with pytest.raises(ValueError, match="--drain"):
        RS.plan_restart(cfg, drain_min=-1)
    plan = RS.plan_restart(cfg, only=["g"])
    assert [t["campaign"] for t in plan["targets"]] == ["g"] and plan["untouched"] == []

    RS._save(cfg, {"id": "R-1", "phase": "park"})
    with pytest.raises(ValueError, match="R-1 is unfinished .*--continue --yes"):
        RS.plan_restart(cfg)
    assert oct(RS.plan_path(cfg).stat().st_mode & 0o777) == "0o600"
    RS.plan_path(cfg).unlink()

    monkeypatch.setattr(Q, "_settled_holder",
                        lambda cfg: {"held": True, "pid": 42, "host": "elsewhere"})
    monkeypatch.setattr(L, "read_lease", lambda cfg: {"argv": ["nw", "queue", "run"]})
    with pytest.raises(ValueError, match="not this host"):
        RS.plan_restart(cfg)
    monkeypatch.setattr(Q, "_settled_holder",
                        lambda cfg: {"held": True, "pid": 42, "host": socket.gethostname()})
    monkeypatch.setattr(L, "read_lease", lambda cfg: {"argv": ["nw", "queue", "tick"]})
    with pytest.raises(ValueError, match="one-shot"):
        RS.plan_restart(cfg)


# ---- apply ---------------------------------------------------------------------------------------

def test_apply_restarts_the_runner(tmp_path, children, slots):
    cfg = _cfg(tmp_path)
    (tmp_path / "old").mkdir()
    old = _spawn_runner(cfg, children, cwd=tmp_path / "old", env_extra={"FOO": "bar"})
    _camp(cfg, "low-gate", "GATING", priority=3)
    _camp(cfg, "hi-close", "CLOSING", priority=1)
    _camp(cfg, "impl", "IMPLEMENTING", priority=2)
    slots["v"] = 2                       # impl + hi-close fit; low-gate waits for a slot
    plan = RS.plan_restart(cfg)
    done = RS.apply_restart(cfg, plan, by="tester", popen=_recording_popen(children))

    assert old.wait(30) == 0                                   # SIGTERM'd, released, exited
    assert done["phase"] == "done" and done["done_phases"] == list(RS.PHASES)
    assert done["stop"]["stopped"] and done["stop"]["pid"] == old.pid
    assert [p["campaign"] for p in done["parked"]] == ["hi-close", "low-gate"]
    assert [r["campaign"] for r in done["resumed"]] == ["hi-close", "low-gate"]
    assert _state(cfg, "hi-close")["state"] == "CLOSING"
    low = _state(cfg, "low-gate")
    assert low["state"] == "WAITING" and low["waiting"]["resume_to"] == "GATING"
    assert low.get("park") is None
    impl = _state(cfg, "impl")
    assert impl["state"] == "IMPLEMENTING"
    assert not [e for e in S.read_ledger(cfg, "impl") if e["type"] in ("park", "resume")]
    park = [e for e in S.read_ledger(cfg, "hi-close") if e["type"] == "park"][-1]
    assert park["reason"] == "restart" and park["by"] == "tester"
    assert (_state(cfg, "hi-close").get("counters") or {}).get("parks", 0) == 0

    new = children[-1]
    assert new.pid == done["start"]["pid"] != old.pid
    assert L.lease_status(cfg)["pid"] == new.pid
    proc = RS.read_proc(new.pid)
    assert proc["argv"] == _runner_argv(cfg, "--poll", "5")
    assert proc["cwd"] == str(tmp_path / "old") and proc["env"].get("FOO") == "bar"
    assert L.HANDOVER_ENV not in proc["env"]
    assert done["command"]["env_used"] == "proc"

    rec = RS.read_plan(cfg)
    assert rec["phase"] == "done" and "_env" not in rec and "bar" not in json.dumps(rec)
    j = [r for r in Q.read_journal(cfg) if r["kind"] == "restart"][-1]
    assert j["id"] == done["id"] and j["old_pid"] == old.pid and j["new_pid"] == new.pid
    assert j["parked"] == ["hi-close", "low-gate"]
    # a done plan does not block the next one
    assert RS.plan_restart(cfg)["phase"] == "planned"


def test_apply_only_parks_the_chosen(tmp_path, children, slots):
    cfg = _cfg(tmp_path)
    _spawn_runner(cfg, children, cwd=tmp_path)
    _camp(cfg, "a", "GATING")
    _camp(cfg, "b", "GATING")
    plan = RS.plan_restart(cfg, only=["b"])
    assert plan["untouched"] == ["a"]
    done = RS.apply_restart(cfg, plan, popen=_recording_popen(children))
    assert [p["campaign"] for p in done["parked"]] == ["b"]
    assert not [e for e in S.read_ledger(cfg, "a") if e["type"] == "park"]
    assert _state(cfg, "a")["state"] == "GATING"     # the new runner re-gates it


def test_park_skips_park_requested_and_moved(tmp_path, slots):
    cfg = _cfg(tmp_path)
    _camp(cfg, "req", "GATING", park_requested={"reason": "human", "by": "human"})
    _camp(cfg, "moved", "GATING")
    plan = RS.plan_restart(cfg)
    st = _state(cfg, "moved")
    st["state"] = "TRIAGING"
    S.write_state(cfg, "moved", st)
    plan["id"] = "R-x"
    RS._park(cfg, plan, by="human")
    assert plan["parked"] == []
    assert {s["campaign"]: s["why"] for s in plan["skipped"]} == {"req": "park_requested",
                                                                   "moved": "adopt"}
    assert _state(cfg, "req")["state"] == "GATING"


def test_crash_after_park_then_continue(tmp_path, children, slots):
    cfg = _cfg(tmp_path)
    old = _spawn_runner(cfg, children, cwd=tmp_path)
    _camp(cfg, "g", "GATING")
    _camp(cfg, "o", "OPENING")
    plan = RS.plan_restart(cfg)
    with pytest.raises(RuntimeError, match="after park"):
        RS.apply_restart(cfg, plan, popen=_recording_popen(children), fail_after="park")
    assert old.wait(30) == 0 and not L.is_held(cfg)
    rec = RS.read_plan(cfg)
    assert rec["phase"] == "park" and rec["done_phases"] == ["drain", "stop", "park"]
    g = _state(cfg, "g")
    assert g["state"] == "PARKED" and g["park"]["reason"] == "restart"
    assert g["park"]["restart_id"] == rec["id"] and Q.park_owner("restart") == "human"
    with pytest.raises(ValueError, match="unfinished"):
        RS.plan_restart(cfg)

    again = RS.continue_plan(cfg)
    done = RS.apply_restart(cfg, again, popen=_recording_popen(children))
    assert done["id"] == rec["id"] and done["phase"] == "done"
    assert done["stop"]["stopped"] is True                     # recorded once, not re-run
    assert _state(cfg, "g")["state"] == "GATING"
    assert _state(cfg, "o")["state"] == "QUEUED"
    assert done["command"]["env_used"] == "current"            # the env never hit the disk
    assert L.lease_status(cfg)["pid"] == done["start"]["pid"] == children[-1].pid
    assert len(children) == 2
    with pytest.raises(ValueError, match="already done"):
        RS.continue_plan(cfg)


def test_continue_does_not_start_a_second_runner(tmp_path, children, slots):
    cfg = _cfg(tmp_path)
    _spawn_runner(cfg, children, cwd=tmp_path)
    _camp(cfg, "g", "GATING")
    with pytest.raises(RuntimeError):
        RS.apply_restart(cfg, RS.plan_restart(cfg), popen=_recording_popen(children),
                         fail_after="resume")
    other = _spawn_runner(cfg, children, cwd=tmp_path)    # someone started one meanwhile
    done = RS.apply_restart(cfg, RS.continue_plan(cfg),
                            popen=lambda *a, **k: pytest.fail("must not start a runner"))
    assert done["start"] == {"started": False, "pid": other.pid,
                             "reason": "lease already held; not starting another"}


def test_park_refused_while_a_runner_ticks(tmp_path, slots):
    cfg = _cfg(tmp_path)
    _camp(cfg, "g", "GATING")
    plan = RS.plan_restart(cfg)
    plan["done_phases"] = ["drain", "stop"]
    with L.acquire(cfg):
        with pytest.raises(ValueError, match="stopped in phase park.*holds the lease again"):
            RS.apply_restart(cfg, plan, popen=lambda *a, **k: pytest.fail("no start"))
    rec = RS.read_plan(cfg)
    assert rec["phase"] == "park" and rec["error"].startswith("park:")
    assert _state(cfg, "g")["state"] == "GATING"


def test_drain_waits_for_gates_then_the_deadline(tmp_path):
    cfg = _cfg(tmp_path)
    _camp(cfg, "g1", "GATING")
    _camp(cfg, "g2", "GATING")
    _camp(cfg, "impl", "IMPLEMENTING")
    t = {"now": 0.0, "sleeps": 0}

    def clock():
        return t["now"]

    def sleep_then_finish_g1(s):
        t["now"] += s
        t["sleeps"] += 1
        if t["sleeps"] == 2:
            st = _state(cfg, "g1")
            st["state"] = "TRIAGING"
            S.write_state(cfg, "g1", st)

    with L.acquire(cfg):
        plan = RS.plan_restart(cfg, drain_min=1)
        RS._drain(cfg, plan, clock=clock, sleep=sleep_then_finish_g1, poll_s=10)
        assert plan["drain"] == {"skipped": False, "waited_s": 60.0, "finished": ["g1"],
                                 "pending": ["g2"]}
        st = _state(cfg, "g2")
        st["state"] = "CLOSING"
        S.write_state(cfg, "g2", st)
        plan = RS.plan_restart(cfg, drain_min=1, only=["g2"])
        t["now"] = 0.0
        RS._drain(cfg, plan, clock=clock, sleep=lambda s: pytest.fail("no wait"), poll_s=10)
        assert plan["drain"]["finished"] == [] and plan["drain"]["pending"] == []
    plan = RS.plan_restart(cfg, drain_min=1)
    RS._drain(cfg, plan, clock=clock, sleep=lambda s: pytest.fail("no runner"), poll_s=10)
    assert plan["drain"]["skipped"] is True


# ---- CLI -----------------------------------------------------------------------------------------

def _cli(tmp_path, monkeypatch, cfg, args):
    from click.testing import CliRunner
    from nightwright.cli import cli
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    return CliRunner().invoke(cli, ["queue", "restart", *args])


def test_cli_dry_run_and_json(tmp_path, monkeypatch, children):
    cfg = _cfg(tmp_path)
    p = _spawn_runner(cfg, children, cwd=tmp_path, env_extra={"SECRET_TOKEN": "s3cr3t"})
    _camp(cfg, "g", "GATING")
    _camp(cfg, "impl", "IMPLEMENTING")
    r = _cli(tmp_path, monkeypatch, cfg, [])
    assert r.exit_code == 0, r.output
    assert f"pid {p.pid}" in r.output and "g  GATING" in r.output
    assert "park restart → resume GATING" in r.output and "adopts its persona run" in r.output
    assert "s3cr3t" not in r.output
    r = _cli(tmp_path, monkeypatch, cfg, ["--json"])
    assert r.exit_code == 0, r.output
    rec = json.loads(r.output)
    assert rec["written"] is False and rec["schema"] == "nw.queue.restart.v1"
    assert "_env" not in rec and "s3cr3t" not in r.output
    assert not RS.plan_path(cfg).exists() and L.lease_status(cfg)["pid"] == p.pid
    assert p.poll() is None
    r = _cli(tmp_path, monkeypatch, cfg, ["--continue", "--drain", "5"])
    assert r.exit_code != 0 and "drop --drain" in r.output
    r = _cli(tmp_path, monkeypatch, cfg, ["--continue"])
    assert r.exit_code != 0 and "no restart to continue" in r.output


# ---- stale runner code (queue watch) -------------------------------------------------------------

def _code_repo(tmp_path):
    repo = tmp_path / "nwrepo"
    (repo / "src" / "nightwright").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "src" / "nightwright" / "x.py").write_text("a = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a")
    return repo


def _commit(repo, rel, text):
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", rel)
    return Q._git_out(repo, "rev-parse", "HEAD")


def test_runner_code_status(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    repo = _code_repo(tmp_path)
    a = Q._git_out(repo, "rev-parse", "HEAD")
    Q._CODE_MEMO.clear()
    lease = {"held": True, "pid": 123, "code_sha": a}
    assert Q.runner_code_status(cfg, lease, root=repo) == {
        "runner_sha": a, "head_sha": a, "stale": False, "changed": 0, "pending_reexec": False}
    docs = _commit(repo, "docs/readme.md", "x\n")
    rc = Q.runner_code_status(cfg, lease, root=repo)
    assert rc["head_sha"] == docs and rc["stale"] is False and rc["changed"] == 0
    b = _commit(repo, "src/nightwright/x.py", "a = 2\n")
    rc = Q.runner_code_status(cfg, lease, root=repo)
    assert rc["head_sha"] == b and rc["stale"] is True and rc["changed"] == 1
    drain = Q.runner_code_status(cfg, {**lease, "draining": {"reason": "code_changed"}},
                                 root=repo)
    assert drain["pending_reexec"] is True
    assert Q.runner_code_status(cfg, {"held": False}, root=repo) is None
    assert Q.runner_code_status(cfg, {"held": True, "pid": 999999}, root=repo) is None

    # steady state: no git process once HEAD's files are unchanged
    calls = []
    real = Q._git_out
    monkeypatch.setattr(Q, "_git_out", lambda *a: calls.append(a) or real(*a))
    monkeypatch.setattr(Q, "_changed_paths", lambda *a: calls.append(a) or [])
    for _ in range(3):
        assert Q.runner_code_status(cfg, lease, root=repo)["stale"] is True
    assert calls == []


def test_runner_code_status_falls_back_to_the_journal(tmp_path):
    cfg = _cfg(tmp_path)
    repo = _code_repo(tmp_path)
    a = Q._git_out(repo, "rev-parse", "HEAD")
    Q._CODE_MEMO.clear()
    Q.journal(cfg, "start", argv=["nw", "queue", "run"], interval_s=5, code_sha=a)
    pid = [r for r in Q.read_journal(cfg) if r["kind"] == "start"][-1]["pid"]
    b = _commit(repo, "pyproject.toml", "[project]\n")
    rc = Q.runner_code_status(cfg, {"held": True, "pid": pid}, root=repo)
    assert rc["runner_sha"] == a and rc["head_sha"] == b and rc["stale"] is True


def test_runner_code_line():
    assert CQ._runner_code_line(None) is None
    assert CQ._runner_code_line({"stale": False}) is None
    line = CQ._runner_code_line({"stale": True, "runner_sha": "a" * 40, "head_sha": "b" * 40,
                                 "changed": 3, "pending_reexec": False})
    assert line.plain == "⚠ runner code stale: aaaaaaa → bbbbbbb (3 code files)  · nw queue restart"
    line = CQ._runner_code_line({"stale": True, "runner_sha": "a" * 40, "head_sha": "b" * 40,
                                 "changed": None, "pending_reexec": True})
    assert "(diff unknown)" in line.plain and "re-exec pending (draining)" in line.plain


def test_dashboard_carries_runner_code(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    seen = []
    monkeypatch.setattr(Q, "runner_code_status",
                        lambda cfg, lease, root=None: seen.append(lease) or {"stale": True})
    with L.acquire(cfg):
        p = Q.dashboard_payload(cfg)
    assert p["runner_code"] == {"stale": True} and seen[0]["held"] is True
    monkeypatch.setattr(Q, "runner_code_status", lambda *a, **k: 1 / 0)
    assert Q.dashboard_payload(cfg)["runner_code"] is None      # never breaks the watch


def test_run_records_its_code_sha(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    head = Q._git_out(repo, "rev-parse", "HEAD")
    seen = []
    real = L.Lease.update_info

    def update_info(self, **kv):
        seen.append(kv)
        return real(self, **kv)

    monkeypatch.setattr(L.Lease, "update_info", update_info)
    Q.run(cfg, once=True, code_root=repo)
    assert {"code_sha": head} in seen
    start = [r for r in Q.read_journal(cfg) if r["kind"] == "start"][-1]
    assert start["code_sha"] == head


def test_journal_line_for_a_restart():
    g = CQ._journal_lines({"ts": "2026-11-02T10:00:00", "pid": 5, "kind": "restart",
                           "id": "R-1", "phase": "done", "parked": ["a", "b"],
                           "resumed": ["a", "b"], "old_pid": 5, "new_pid": 9})
    assert "⟲ restart R-1  runner 5 → 9; parked 2, resumed 2" in g.renderables[0].plain
