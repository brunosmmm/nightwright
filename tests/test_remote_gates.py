"""SPEC-0338: remote gate dispatch with local fallback. Offline: the SSH transport is a local
script (`ssh_argv`) that drops the destination and runs the remote command through
`SSH_ORIGINAL_COMMAND` and `nw worker ssh-command`, with `NW_DATA_DIR`/`NW_CONFIG_DIR` at a
second temp data dir. The fake project is a temp git repo with tiny shell gates."""
import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from nightwright.agentic import gate_slots as GS
from nightwright.agentic import gates as G
from nightwright.agentic import store as S

from waits import join, until

CID = "SPEC-0900-20261101-abcd"
GOLDEN = Path(__file__).parent / "data" / "remote_gates_golden.json"
_MASK = {"started_at", "duration_s", "slot_wait_s", "ts", "since", "waited_s"}


@pytest.fixture(autouse=True)
def _short_runtime_dir(monkeypatch):
    """A long pytest tmp path makes the ssh socket dir fall back to $XDG_RUNTIME_DIR: keep
    those dirs out of the real one."""
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix="nwr")
    monkeypatch.setenv("XDG_RUNTIME_DIR", d)
    G.reset()
    yield
    G.reset()
    shutil.rmtree(d, ignore_errors=True)


_DATES = {"GIT_AUTHOR_DATE": "2026-11-01T10:00:00+00:00",
          "GIT_COMMITTER_DATE": "2026-11-01T10:00:00+00:00"}


def _git(root, *args):
    """git with fixed dates, so the fake repo's shas (and the golden record) are stable."""
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True, env={**os.environ, **_DATES}).stdout.strip()


def _commit(root, name, text):
    (root / name).write_text(text)
    _git(root, "add", name)
    _git(root, "commit", "-q", "-m", f"add {name}")
    return _git(root, "rev-parse", "HEAD")


def _cfg(tmp_path, slots=2):
    return {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "cfg"),
            "agentic": {"gates": {"slots": slots}}}


def _gate(name, cmd, **kw):
    return {"name": name, "cmd": cmd, "cwd": ".", "timeout_s": 60, "required": True,
            "retries": 0, **kw}


class World:
    def __init__(self, tmp_path, *, feature=True):
        self.tmp = tmp_path
        self.cfg = _cfg(tmp_path)
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "t")
        (self.repo / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (self.repo / ".git" / "info" / "exclude").write_text("ignored.txt\n")
        self.base = _commit(self.repo, "a.txt", "a\n")
        _git(self.repo, "checkout", "-q", "-b", f"nw/{CID}")
        self.head = _commit(self.repo, "b.txt", "b\n") if feature else self.base
        self.state = {"id": CID, "spec_id": "SPEC-0900", "project": "demo",
                      "profile": "balanced", "state": "GATING",
                      "created_at": "2026-11-01T10:00:00-04:00",
                      "repo": {"base": "main", "worktree": str(self.repo),
                               "branch": f"nw/{CID}"},
                      "counters": {"loops": 0, "runs": 0},
                      "cost": {"total_usd": 0.0, "cap_usd": 10.0}, "last_gate": None,
                      "park": None}
        S.create_campaign(self.cfg, self.state)
        self.counter = tmp_path / "counter"
        self.project = {"project": "demo", "base": "main", "gates": [
            _gate("ok", "echo hello; git rev-parse HEAD"),
            _gate("opt", "echo boom >&2; exit 3", required=False),
            _gate("flaky", f"n=$(cat {self.counter} 2>/dev/null || echo 0); "
                           f"echo $((n+1)) > {self.counter}; echo try $n; [ $n -ge 1 ]",
                  retries=1),
        ]}

    @property
    def gates_dir(self):
        return S.gates_dir(self.cfg, CID)

    def ledger(self, types=None):
        return S.read_ledger(self.cfg, CID, types=types)


def _mask(obj, tmp):
    text = json.dumps(obj, sort_keys=True).replace(str(tmp), "<TMP>")

    def walk(v):
        if isinstance(v, dict):
            return {k: ("<X>" if k in _MASK else walk(x)) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v
    return walk(json.loads(text))


def _snapshot(w: World, run) -> dict:
    files = {}
    for p in sorted(w.gates_dir.iterdir()):
        text = p.read_text(encoding="utf-8")
        files[p.name] = _mask(json.loads(text), w.tmp) if p.suffix == ".json" \
            else text.replace(str(w.tmp), "<TMP>")
    events = [{k: v for k, v in e.items() if k not in ("seq",)} for e in w.ledger()]
    return {"payload": _mask(G.as_payload(run), w.tmp),
            "gate_run": _mask({"seq": run.seq, "ok": run.ok, "interrupted": run.interrupted,
                               "n": len(run.results)}, w.tmp),
            "files": files, "events": _mask(events, w.tmp)}


def _golden_run(tmp_path):
    """The no-worker path, recorded before `run_all` learnt remote dispatch (SPEC-0338 AC1):
    an untracked file and a project opting out of remote gates must change nothing."""
    w = World(tmp_path)
    (w.repo / "untracked.txt").write_text("x\n")
    w.project["remote_gates"] = False
    run = G.run_and_log(w.cfg, w.state, w.project, by="test")
    return w, run


def test_golden_record_or_compare(tmp_path):
    w, run = _golden_run(tmp_path)
    snap = _snapshot(w, run)
    if os.environ.get("NW_RECORD_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert snap == json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.mark.parametrize("registry", ["", '{"schema": "nw.workers.v1", "workers": []}'])
def test_golden_holds_with_an_empty_registry(tmp_path, registry):
    from nightwright.agentic import workers as WK
    p = WK.path(_cfg(tmp_path))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(registry)
    w, run = _golden_run(tmp_path)
    assert run.worker is None
    assert _snapshot(w, run) == json.loads(GOLDEN.read_text(encoding="utf-8"))


# ---- the loopback worker ---------------------------------------------------------------------

def _script(path, body):
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


_DROP_AFTER_GATE = """import subprocess, sys, os
p = subprocess.Popen(sys.argv[1:], stdout=subprocess.PIPE)
for line in p.stdout:
    sys.stdout.buffer.write(line)
    sys.stdout.buffer.flush()
    if b'"type": "gate"' in line:
        os._exit(255)
"""


class Remote:
    """A worker reached through a fake `ssh`: the destination is dropped and the command runs
    as the SSH forced command would, against its own data dir."""

    def __init__(self, w: World):
        self.w = w
        tmp = w.tmp
        self.wdata = tmp / "wdata"
        self.wcfg = {"data_dir": str(self.wdata)}
        self.ssh = _script(tmp / "fake-ssh", (
            f'shift\nexport SSH_ORIGINAL_COMMAND="$*" NW_DATA_DIR={self.wdata} '
            f'NW_CONFIG_DIR={tmp / "wcfgdir"} NW_WORKER_FINISH_GC=0\n'
            f'exec {sys.executable} -m nightwright worker ssh-command'))
        (tmp / "drop.py").write_text(_DROP_AFTER_GATE)
        self.variants = {
            "unreachable": _script(tmp / "ssh-255", "echo 'ssh: connect: refused' >&2\nexit 255"),
            "job255": _script(tmp / "ssh-job255", f'case "$*" in *gate-job*) exit 255;; esac\n'
                                                  f'exec {self.ssh} "$@"'),
            "have": _script(tmp / "ssh-have", f'case "$*" in *" have "*) exit 255;; esac\n'
                                             f'exec {self.ssh} "$@"'),
            "garbage": _script(tmp / "ssh-garbage", f'case "$*" in *gate-job*) echo garbage; '
                                                    f'exec cat >/dev/null;; esac\n'
                                                    f'exec {self.ssh} "$@"'),
            "drop": _script(tmp / "ssh-drop", f'case "$*" in *gate-job*) exec {sys.executable} '
                                              f'{tmp / "drop.py"} {self.ssh} "$@";; esac\n'
                                              f'exec {self.ssh} "$@"'),
        }

    def register(self, ssh=None, **kw):
        from nightwright.agentic import workers as WK
        p = WK.path(self.w.cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"name": "box", "ssh": "me@box", "slots": 1, "nw": "nw",
               "ssh_argv": [ssh or self.ssh], **kw}
        p.write_text(json.dumps({"schema": WK.SCHEMA, "workers": [rec]}))

    def record(self):
        from nightwright.agentic import workers as WK
        return WK.load(self.w.cfg)[0]

    def frames(self, seq=1):
        job = self.wdata / "agentic" / "worker" / "jobs" / f"{CID}-gate-{seq:03d}-a0"
        return [json.loads(ln) for ln in (job / "frames.jsonl").read_text().splitlines()]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.fixture
def remote(world):
    r = Remote(world)
    r.register()
    return r


@pytest.fixture
def spy(monkeypatch):
    """Every job header `prepare` built, with the refs its bundle carries."""
    from nightwright.agentic import remote_gates as RG
    seen = []
    real = RG.prepare

    def wrapped(worker, project_cfg, state, cfg, seq, job_id, bundle):
        h = real(worker, project_cfg, state, cfg, seq, job_id, bundle)
        heads = _git(state["repo"]["worktree"], "bundle", "list-heads", str(bundle)) \
            if h["bundle_bytes"] else ""
        seen.append({"header": h, "heads": heads})
        return h
    monkeypatch.setattr(RG, "prepare", wrapped)
    return seen


def _decode(data):
    import base64
    import gzip
    return gzip.decompress(base64.b64decode(data))


def test_remote_run_matches_a_local_one(world, remote):
    world.project["gates"].append(_gate("mb", "git merge-base $NW_BASE HEAD"))
    world.project["gates"].append(_gate("bad", "exit 4", required=False))
    run = G.run_and_log(world.cfg, world.state, world.project, by="test")
    assert run.worker == "box" and run.ok and run.seq == 1
    assert not world.ledger(["worker_failed"])
    by = {r.name: r for r in run.results}
    assert by["mb"].tail == world.base                          # the base ref rides along
    assert by["ok"].tail.splitlines()[-1] == world.head
    assert not by["bad"].ok and not by["opt"].ok and by["flaky"].ok
    assert [a["log"] for a in by["flaky"].attempts] == ["gates/001-flaky.log",
                                                        "gates/001-flaky.r1.log"]
    assert all(r.log == f"gates/001-{r.name}.log" for r in run.results)
    assert str(world.repo) not in by["ok"].cwd                  # cwd keeps the worker path
    # the coordinator's files are byte for byte what the worker sent
    sent = {}
    for f in remote.frames():
        if f["type"] == "gate":
            sent.update({k: _decode(v) for k, v in f["files"].items()})
    assert "001-flaky.r1.log" in sent and "001-flaky.r1.json" in sent
    assert {p.name: p.read_bytes() for p in world.gates_dir.iterdir()} == sent
    (ev,) = world.ledger(["gate"])
    assert ev["worker"] == "box"
    local = json.loads(GOLDEN.read_text())["payload"]
    assert set(G.as_payload(run)) - {"worker"} == set(local)
    assert set(ev["results"][0]) == set(local["results"][0])
    assert remote.record()["cooldown_until"] is None            # a success writes no registry


def test_a_failing_gate_on_the_worker_is_a_result(world, remote):
    world.project["gates"] = [_gate("red", "echo nope; exit 1")]
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    assert run.worker == "box" and not run.ok and run.results[0].exit_code == 1
    assert not world.ledger(["worker_failed"])
    assert json.loads((world.gates_dir / "001-red.json").read_text())["cwd"] != str(world.repo)


def test_dirty_tracked_tree_gates_the_stash_commit(world, remote):
    (world.repo / "a.txt").write_text("changed\n")
    stashes = _git(world.repo, "stash", "list")
    world.project["gates"] = [_gate("cat", "cat a.txt; git status --porcelain")]
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    assert run.worker == "box"
    assert run.results[0].tail.splitlines()[0] == "changed"
    assert _git(world.repo, "stash", "list") == stashes          # the stash stack is untouched
    assert (world.repo / "a.txt").read_text() == "changed\n"


@pytest.mark.parametrize("case", ["untracked", "project", "gate"])
def test_ineligible_runs_gate_locally_with_one_finding(world, remote, case):
    if case == "untracked":
        (world.repo / "new.txt").write_text("x\n")
        (world.repo / "ignored.txt").write_text("x\n")
    elif case == "project":
        world.project["remote_gates"] = False
    else:
        world.project["gates"][0]["remote"] = False
    for seq in (1, 2):
        run = G.run_all(world.project, world.state, world.cfg, seq=seq)
        assert run.worker is None and run.results[0].cwd == str(world.repo)
    (f,) = world.ledger(["finding"])
    assert (f["persona"], f["severity"], f["gate"]) == ("gates", "info", "remote-eligibility")
    if case == "untracked":
        assert f["files"] == ["new.txt"]
    from nightwright.agentic import divergence as D
    assert f["persona"] not in D.COUNTED_PERSONAS and f["severity"] not in D.COUNTED_SEVERITIES


def test_second_run_bundles_less_and_leaves_the_base_out(world, remote, spy):
    for seq in (1, 2):
        assert G.run_all(world.project, world.state, world.cfg, seq=seq).worker == "box"
    first, second = spy
    assert first["header"]["have"] == [] and "-base" in first["heads"]
    assert second["header"]["have"] == [world.base]
    assert "-base" not in second["heads"] and f"gate-002-a0" in second["heads"]
    assert 0 < second["header"]["bundle_bytes"] < first["header"]["bundle_bytes"]
    assert not world.ledger(["worker_failed"])
    assert _git(world.repo, "for-each-ref", "refs/nw/gate-jobs/") == ""


def test_head_equal_to_base_gates_remotely_twice(tmp_path, spy):
    w = World(tmp_path, feature=False)
    Remote(w).register()
    w.project["gates"].append(_gate("mb", "git merge-base $NW_BASE HEAD"))
    for seq in (1, 2):
        run = G.run_all(w.project, w.state, w.cfg, seq=seq)
        assert run.worker == "box" and run.results[-1].tail == w.base
    assert spy[1]["header"]["bundle_bytes"] == 0 and spy[1]["header"]["have"] == [w.base]
    assert not w.ledger(["worker_failed"])


# ---- transport failures ----------------------------------------------------------------------

def _assert_fell_back(world, remote, run, stage, cooldown):
    assert run.worker is None and run.seq == 1 and run.ok
    assert all(r.cwd == str(world.repo) for r in run.results)
    (ev,) = world.ledger(["worker_failed"])
    assert ev["worker"] == "box" and ev["job_id"] == f"{CID}-gate-001-a0"
    assert ev["stage"] == stage, ev
    assert (remote.record()["cooldown_until"] is not None) == cooldown
    names = sorted(p.name for p in world.gates_dir.iterdir())
    assert names == sorted([f"001-{n}.{x}" for n in ("ok", "opt", "flaky") for x in ("log", "json")]
                           + ["001-flaky.r1.json", "001-flaky.r1.log"])
    for p in world.gates_dir.glob("*.json"):                     # nothing left from the worker
        assert json.loads(p.read_text())["cwd"] == str(world.repo)
    assert not S.child_tmp(world.cfg, CID, "gate-001").exists()
    assert _git(world.repo, "for-each-ref", "refs/nw/gate-jobs/") == ""


@pytest.mark.parametrize("variant,stage", [("unreachable", "have"), ("job255", "ssh"), ("have", "have"),
                                           ("garbage", "protocol"), ("drop", "ssh")])
def test_transport_failures_fall_back_with_cooldown(world, remote, variant, stage):
    remote.register(ssh=remote.variants[variant])
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    _assert_fell_back(world, remote, run, stage, cooldown=True)
    if variant == "drop":
        assert len(run.results) == 3


def test_coordinator_timeout_falls_back_with_cooldown(world, remote, monkeypatch):
    from nightwright.agentic import remote_gates as RG
    monkeypatch.setattr(RG, "job_timeout_s", lambda cfg, gates: 1)
    monkeypatch.setattr(RG, "RUN_TIMEOUT_MARGIN_S", 0.5)
    world.project["gates"][0]["cmd"] = 'case "$NW_DATA_DIR" in *wdata*) sleep 30;; esac; echo ok'
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    _assert_fell_back(world, remote, run, "timeout", cooldown=True)


def test_duplicate_job_id_falls_back_without_cooldown(world, remote):
    (remote.wdata / "agentic" / "worker" / "jobs" / f"{CID}-gate-001-a0").mkdir(parents=True)
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    _assert_fell_back(world, remote, run, "claim", cooldown=False)


def test_failing_bundle_step_falls_back_without_cooldown(world, remote, monkeypatch):
    from nightwright.agentic import remote_gates as RG
    real = RG._git

    def broken(cwd, *args, check=True):
        if args and args[0] == "bundle":
            assert _git(cwd, "for-each-ref", "refs/nw/gate-jobs/")    # the refs exist here
            raise RG.RemoteFailure("bundle", "disk full", cooldown=False)
        return real(cwd, *args, check=check)
    monkeypatch.setattr(RG, "_git", broken)
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    _assert_fell_back(world, remote, run, "bundle", cooldown=False)


def _gate_job_variant(remote, name, body):
    """A fake ssh whose `gate-job` runs `body` (sh) and whose other commands reach the worker."""
    return _script(remote.w.tmp / f"ssh-{name}", f'case "$*" in *gate-job*) {body};; esac\n'
                                                f'exec {remote.ssh} "$@"')


def _error_frame(stage):
    from nightwright.agentic import worker as W
    return json.dumps({"schema": W.FRAME_SCHEMA, "type": "error", "stage": stage,
                       "error": f"{stage} went wrong"})


def test_an_error_frame_falls_back_with_cooldown(world, remote):
    remote.register(ssh=_gate_job_variant(
        remote, "err-checkout", f"echo '{_error_frame('checkout')}'; exec cat >/dev/null"))
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    _assert_fell_back(world, remote, run, "checkout", cooldown=True)
    assert world.ledger(["worker_failed"])[0]["error"] == "checkout went wrong"


def test_a_validate_error_frame_falls_back_without_cooldown(world, remote):
    """The backstop: a header the worker refuses is the coordinator's doing, not the host's."""
    remote.register(ssh=_gate_job_variant(
        remote, "err-validate", f"echo '{_error_frame('validate')}'; exec cat >/dev/null"))
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    _assert_fell_back(world, remote, run, "validate", cooldown=False)


@pytest.mark.parametrize("names", [["unit tests"], ["lint:ruff"], ["py/unit"], ["ok", "ok.r1"]])
def test_gates_the_worker_would_refuse_stay_local(world, remote, spy, invoke, names):
    world.project["gates"] = [_gate(n, "echo hi") for n in names]
    for seq in (1, 2):
        run = G.run_all(world.project, world.state, world.cfg, seq=seq)
        assert run.worker is None and run.ok and run.results[0].cwd == str(world.repo)
    assert spy == []                                        # nothing bundled, nothing sent
    assert not world.ledger(["worker_failed"]) and remote.record()["cooldown_until"] is None
    (f,) = world.ledger(["finding"])
    assert f["gate"] == "remote-eligibility" and "refuse" in f["title"] and f["detail"]
    r = invoke("queue", "gates", CID, "--worker", "box")
    assert r.exit_code != 0 and "not eligible" in r.output


def test_a_base_name_the_worker_would_refuse_stays_local(world, remote):
    from nightwright.agentic import remote_gates as RG
    assert RG.pools_for(world.cfg, world.project, world.state, seq=1) is not None
    state = {**world.state, "repo": {**world.state["repo"], "base": "release/.hidden"}}
    assert RG.header_refusal(world.project, state, world.cfg, 1)
    assert RG.pools_for(world.cfg, world.project, state, seq=1) is None
    assert RG.pools_for(world.cfg, world.project, world.state, seq=1000) is None
    with pytest.raises(ValueError, match="not eligible"):
        RG.pools_for(world.cfg, world.project, state, seq=1, worker="box")
    assert len(world.ledger(["finding"])) == 1


@pytest.mark.parametrize("reply", ["error", "garbage"])
def test_an_error_after_a_stop_is_an_interruption(world, remote, tmp_path, reply):
    """Once a stop closed stdin, whatever the worker answers ends the run as interrupted: no
    `worker_failed`, no cooldown, no local re-run."""
    marker = tmp_path / "job-started"
    line = _error_frame("checkout") if reply == "error" else "garbage"
    remote.register(ssh=_gate_job_variant(
        remote, f"stop-{reply}", f"touch {marker}; cat >/dev/null; echo '{line}'"))
    stop = threading.Event()
    out = {}
    t = threading.Thread(target=lambda: out.update(run=G.run_all(
        world.project, world.state, world.cfg, seq=1, stop=stop)))
    t.start()
    assert until(marker.exists)
    stop.set()
    join(t)
    run = out["run"]
    assert run.interrupted and not run.ok and run.worker == "box" and run.seq == 1
    assert not world.ledger(["worker_failed"]) and remote.record()["cooldown_until"] is None
    assert not world.gates_dir.exists() or not list(world.gates_dir.iterdir())


def _hold_worker_slots(remote, n=2):
    """Hold the worker host's local gate slots (its default pool of 2) from this process."""
    import contextlib
    stack = contextlib.ExitStack()
    for i in range(n):
        stack.enter_context(GS.acquire(remote.wcfg, f"other-{i}"))
    return stack


def _worker_waiting(remote):
    return until(lambda: GS.waiting(remote.wcfg, f"remote:{CID}") is not None)


def test_slot_wait_does_not_count_against_the_run_timeout(world, remote, monkeypatch):
    from nightwright.agentic import remote_gates as RG
    monkeypatch.setattr(RG, "job_timeout_s", lambda cfg, gates: 1)
    monkeypatch.setattr(RG, "RUN_TIMEOUT_MARGIN_S", 2.0)
    world.cfg["agentic"]["workers"] = {"slot_wait_s": 60}
    world.project["gates"] = [_gate("ok", "echo ok")]
    out = {}
    with _hold_worker_slots(remote):
        t = threading.Thread(target=lambda: out.update(run=G.run_all(
            world.project, world.state, world.cfg, seq=1)))
        t.start()
        assert _worker_waiting(remote)
        threading.Event().wait(4.0)             # held past timeout_s + margin (3 s)
    join(t)
    run = out["run"]
    assert run.worker == "box" and run.ok and not world.ledger(["worker_failed"])
    assert run.slot_wait_s >= 3.0


def test_worker_slot_wait_past_the_limit_falls_back_without_cooldown(world, remote):
    world.cfg["agentic"]["workers"] = {"slot_wait_s": 1}
    with _hold_worker_slots(remote):
        run = G.run_all(world.project, world.state, world.cfg, seq=1)
    _assert_fell_back(world, remote, run, "slot_wait", cooldown=False)


def test_stop_during_the_fallback_local_wait(world, remote):
    world.cfg["agentic"]["gates"]["slots"] = 1
    remote.register(ssh=remote.variants["unreachable"])
    stop = threading.Event()
    out = {}
    with GS.acquire(world.cfg, "other"):
        t = threading.Thread(target=lambda: out.update(run=G.run_all(
            world.project, world.state, world.cfg, seq=1, stop=stop)))
        t.start()
        assert until(lambda: GS.waiting(world.cfg, CID) is not None
                     and world.ledger(["worker_failed"]))
        stop.set()
        join(t)
    run = out["run"]
    assert run.interrupted and not run.ok and run.seq == 1 and run.results == ()
    assert not S.child_tmp(world.cfg, CID, "gate-001").exists()


def test_stop_mid_run_ends_the_workers_gate(world, remote, tmp_path):
    pidfile = tmp_path / "gate.pid"
    world.project["gates"] = [_gate("slow", f"echo $$ > {pidfile}; exec sleep 600")]
    stop = threading.Event()
    out = {}
    t = threading.Thread(target=lambda: out.update(run=G.run_all(
        world.project, world.state, world.cfg, seq=1, stop=stop)))
    t.start()
    assert until(lambda: pidfile.exists() and pidfile.read_text().strip())
    pid = int(pidfile.read_text())
    stop.set()
    join(t)
    run = out["run"]
    assert run.interrupted and not run.ok and run.worker == "box"
    assert not world.ledger(["worker_failed"])

    def gone():
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False
    assert until(gone)


# ---- reporting -------------------------------------------------------------------------------

@pytest.fixture
def invoke(world, monkeypatch):
    from click.testing import CliRunner
    from nightwright.agentic import project as P
    from nightwright.cli import cli
    monkeypatch.setattr("nightwright.cli.load_config", lambda: world.cfg)
    monkeypatch.setattr(P, "project_config", lambda cfg, name: dict(world.project))

    def run(*args):
        return CliRunner().invoke(cli, list(args), catch_exceptions=False)
    return run


def test_queue_gates_worker_and_local(world, remote, invoke):
    from nightwright.agentic import remote_gates as RG
    RG.set_cooldown(world.cfg, "box")                       # --worker ignores the cooldown
    r = invoke("queue", "gates", CID, "--worker", "box")
    assert r.exit_code == 0, r.output
    assert "on box" in r.output
    r = invoke("queue", "gates", CID, "--local")
    assert r.exit_code == 0 and "on box" not in r.output
    r = invoke("queue", "gates", CID, "--worker", "nope")
    assert r.exit_code != 0 and "unknown or disabled" in r.output
    (world.repo / "new.txt").write_text("x\n")
    r = invoke("queue", "gates", CID, "--worker", "box")
    assert r.exit_code != 0 and "not eligible" in r.output
    r = invoke("queue", "gates", CID, "--worker", "box", "--local")
    assert r.exit_code != 0
    r = invoke("campaign", "show", CID)
    assert "worker=box" in r.output
    workers = [e.get("worker") for e in world.ledger(["gate"])]
    assert workers == ["box", None]


def test_worker_failed_is_a_held_queue_event():
    from nightwright.agentic import events as E
    from nightwright.agentic import quiet as QT
    ev = {"type": "worker_failed", "worker": "box", "stage": "ssh", "error": "x", "job_id": "j"}
    t, data = E._project_ledger_event(ev, {}, E.settings({}), False)
    assert t == "worker_failed" and data["worker"] == "box"
    E.validate(t, data)
    assert QT.RULES["worker_failed"] == "hold"


def test_project_config_accepts_the_opt_outs(tmp_path):
    from nightwright.agentic import project as P
    p = tmp_path / "project.yaml"
    p.write_text("schema: nw.project.v1\nremote_gates: false\ngates:\n"
                 "  - {name: a, cmd: 'true', remote: false}\n")
    doc = P.load_project_config(p)
    assert doc["remote_gates"] is False and doc["gates"][0]["remote"] is False
    p.write_text("schema: nw.project.v1\ngates:\n  - {name: a, cmd: 'true', remote: 1}\n")
    with pytest.raises(ValueError, match="remote"):
        P.load_project_config(p)
    p.write_text("schema: nw.project.v1\ngates: []\n")
    assert P.load_project_config(p)["remote_gates"] is True
