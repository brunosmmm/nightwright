"""SPEC-0338: remote gate dispatch with local fallback. Offline: the SSH transport is a local
script (`ssh_argv`) that drops the destination and runs the remote command through
`SSH_ORIGINAL_COMMAND` and `nw worker ssh-command`, with `NW_DATA_DIR`/`NW_CONFIG_DIR` at a
second temp data dir. The fake project is a temp git repo with tiny shell gates."""
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from nightwright.agentic import gate_slots as GS
from nightwright.agentic import gates as G
from nightwright.agentic import remote_gates as RG
from nightwright.agentic import store as S

from remote_support import (CID, DISK_GUARD_OFF, Remote, World, _DATES, _IDENTITY,  # noqa: F401
                            _cfg, _commit, _gate, _git, _script, no_disk_guard)
from remote_support import _short_runtime_dir  # noqa: F401  (autouse fixture)
from waits import join, until

GOLDEN = Path(__file__).parent / "data" / "remote_gates_golden.json"
_MASK = {"started_at", "duration_s", "slot_wait_s", "ts", "since", "waited_s", "nw_sha"}


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

    def wrapped(worker, project_cfg, state, cfg, seq, job_id, bundle, **kw):
        h = real(worker, project_cfg, state, cfg, seq, job_id, bundle, **kw)
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
    assert set(G.as_payload(run)) - {"worker", "changed"} == set(local)
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


# ---- SPEC-0368: the commit under test, and done.changed on the run ---------------------------

def _stat_dirty(repo):
    """Rewrite a tracked file with identical content and a new mtime. No `git status` or
    `update-index` after it: either would refresh the on-disk index and make the tree clean."""
    p = repo / "a.txt"
    p.write_text(p.read_text())
    os.utime(p, (1_900_000_000, 1_900_000_000))


def _raw_stash_create(repo):
    return subprocess.run(["git", "-C", str(repo), "stash", "create"], capture_output=True,
                          text=True)


@pytest.mark.parametrize("case", ["clean", "content-dirty", "stat-dirty"])
def test_commit_under_test_cases(world, case):
    from nightwright.agentic import remote_gates as RG
    if case == "content-dirty":
        (world.repo / "a.txt").write_text("changed\n")
    elif case == "stat-dirty":
        _stat_dirty(world.repo)
        # the precondition, on a copy: a raw `stash create` exits 1 silently, then refreshes
        # the index it ran in, so it must not run in the fixture itself
        twin = world.tmp / "twin-repo"
        shutil.copytree(world.repo, twin, copy_function=shutil.copy2)
        p = _raw_stash_create(twin)
        assert (p.returncode, p.stdout, p.stderr) == (1, "", "")
    sha = RG.commit_under_test(world.repo)
    if case == "content-dirty":
        assert sha != world.head and _git(world.repo, "rev-parse", f"{sha}^") == world.head
        assert _git(world.repo, "show", f"{sha}:a.txt") == "changed"
    else:
        assert sha == world.head


class _Proc:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _stub_git(monkeypatch, replies):
    """`subprocess.run` answering by git subcommand; the calls are recorded."""
    from nightwright.agentic import remote_gates as RG
    calls = []

    def fake(argv, **kw):
        sub = argv[3:5] if argv[3] == "stash" else argv[3:4]
        calls.append((argv[3:], kw.get("env")))
        reply = replies[" ".join(sub)]
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(RG.subprocess, "run", fake)
    return calls


def test_commit_under_test_silent_exit_1_but_dirty(monkeypatch, tmp_path):
    from nightwright.agentic import remote_gates as RG
    calls = _stub_git(monkeypatch, {"stash create": _Proc(1), "diff": _Proc(1),
                                    "rev-parse": _Proc(0, "deadbeef\n")})
    with pytest.raises(RG.RemoteFailure) as e:
        RG.commit_under_test(tmp_path)
    assert e.value.stage == "bundle" and e.value.cooldown is False
    assert [c[0][0] for c in calls] == ["stash", "diff"]            # HEAD was never consulted
    assert calls[1][0][1:] == ["--quiet", "HEAD", "--"]
    assert calls[1][1]["GIT_OPTIONAL_LOCKS"] == "0"


@pytest.mark.parametrize("reply", [_Proc(128, "", "fatal: bad index"), _Proc(1, "text", ""),
                                   _Proc(1, "", "error: boom"), OSError("no git")])
def test_commit_under_test_git_failure(monkeypatch, tmp_path, reply):
    from nightwright.agentic import remote_gates as RG
    _stub_git(monkeypatch, {"stash create": reply})
    with pytest.raises(RG.RemoteFailure) as e:
        RG.commit_under_test(tmp_path)
    assert e.value.stage == "bundle" and e.value.cooldown is False


def test_commit_under_test_leaves_index_alone(world):
    """`git stash create` itself rewrites the index's stat data when it finds a stat-dirty
    tree (git does this and nothing here can stop it); the helper adds no change of content
    and the stash stack stays untouched."""
    from nightwright.agentic import remote_gates as RG
    _stat_dirty(world.repo)
    entries = _git(world.repo, "ls-files", "-s")
    stashes = _git(world.repo, "stash", "list")
    assert RG.commit_under_test(world.repo) == world.head
    assert _git(world.repo, "ls-files", "-s") == entries
    assert _git(world.repo, "diff", "--quiet", "HEAD", "--") == ""
    assert _git(world.repo, "stash", "list") == stashes == ""


def test_stat_dirty_tree_runs_remotely(world, remote, spy):
    world.project["gates"] = [_gate("ok", "echo hi")]
    _stat_dirty(world.repo)
    entries = _git(world.repo, "ls-files", "-s")
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    assert run.worker == "box" and run.ok
    assert spy[0]["header"]["commit"] == world.head
    # SPEC-0391: the worker's registry slots ride along as the host's slot limit
    assert spy[0]["header"]["payload"]["host_slots"] == remote.record()["slots"] == 1
    assert not world.ledger(["worker_failed"]) and not world.ledger(["finding"])
    assert _git(world.repo, "ls-files", "-s") == entries
    assert _git(world.repo, "stash", "list") == ""


def test_prepare_and_tree_under_test_share_the_helper(world, remote, spy):
    from nightwright.agentic import remote_gates as RG
    world.project["gates"] = [_gate("ok", "echo hi")]
    _stat_dirty(world.repo)
    G.run_all(world.project, world.state, world.cfg, seq=1)
    head, tree = RG.tree_under_test(world.repo)
    assert head == world.head and tree == _git(world.repo, "rev-parse", "HEAD^{tree}")
    assert spy[0]["header"]["commit"] == head
    src = Path(RG.__file__).read_text()
    assert src.count('"stash", "create"') == 1                      # only the helper
    assert src.count("commit_under_test(") >= 3                     # def, prepare, tree_under_test


def test_remote_run_records_changed(world, remote):
    world.project["gates"] = [_gate("ok", "echo ok")]
    run = G.run_and_log(world.cfg, world.state, world.project, by="test")
    assert run.worker == "box" and run.changed == ()
    assert G.as_payload(run)["changed"] == []
    (ev,) = world.ledger(["gate"])
    assert ev["changed"] == []
    local = G.run_and_log(world.cfg, world.state, world.project, by="test", local=True)
    assert local.worker is None and local.changed is None
    assert "changed" not in G.as_payload(local)


def test_a_cut_short_remote_run_records_no_changed(world, remote, tmp_path):
    pidfile = tmp_path / "gate.pid"
    world.project["gates"] = [_gate("slow", f"echo $$ > {pidfile}; exec sleep 600")]
    stop = threading.Event()
    out = {}
    t = threading.Thread(target=lambda: out.update(run=G.run_all(
        world.project, world.state, world.cfg, seq=1, stop=stop)))
    t.start()
    assert until(lambda: pidfile.exists() and pidfile.read_text().strip())
    stop.set()
    join(t)
    assert out["run"].interrupted and out["run"].changed is None
    assert "changed" not in G.as_payload(out["run"])


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
        assert f["files"] == ["new.txt"] and f["detail"] == "new.txt"
    elif case == "project":
        assert f["files"] == [] and "remote_gates: false" in f["detail"]
    else:
        assert f["files"] == [] and f["detail"] == "ok" and "gate ok" in f["title"]
    from nightwright.agentic import divergence as D
    assert f["persona"] not in D.COUNTED_PERSONAS and f["severity"] not in D.COUNTED_SEVERITIES


_NON_UTF8 = b"\xff\xfe"


def test_a_non_utf8_untracked_name_stays_local_with_a_finding(world, remote, spy):
    os.close(os.open(os.fsencode(world.repo) + b"/" + _NON_UTF8, os.O_CREAT | os.O_WRONLY))
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    assert run.worker is None and run.ok and run.results[0].cwd == str(world.repo)
    (f,) = world.ledger(["finding"])
    assert f["gate"] == "remote-eligibility" and f["files"] == ["\ufffd\ufffd"]
    assert not spy and not world.ledger(["worker_failed"])


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
    # the messages carry what a loaded-box failure needs to be diagnosed from the log alone
    why = lambda: (world.ledger(["worker_failed"]), remote.record().get("cooldown_until"),  # noqa: E731
                   sorted(p.name for p in world.gates_dir.iterdir()))
    assert run.worker is None and run.seq == 1 and run.ok, why()
    assert all(r.cwd == str(world.repo) for r in run.results), why()
    (ev,) = world.ledger(["worker_failed"])
    assert ev["worker"] == "box" and ev["job_id"] == f"{CID}-gate-001-a0"
    assert ev["stage"] == stage, ev
    assert (remote.record()["cooldown_until"] is not None) == cooldown, why()
    names = sorted(p.name for p in world.gates_dir.iterdir())
    assert names == sorted([f"001-{n}.{x}" for n in ("ok", "opt", "flaky") for x in ("log", "json")]
                           + ["001-flaky.r1.json", "001-flaky.r1.log"]), why()
    for p in world.gates_dir.glob("*.json"):                     # nothing left from the worker
        assert json.loads(p.read_text())["cwd"] == str(world.repo)
    # both are local state `run_all` cleans up before it returns, so they are direct asserts: a
    # still-running worker job cannot change them, and waiting would hide a lost cleanup
    assert not S.child_tmp(world.cfg, CID, "gate-001").exists()
    assert _git(world.repo, "for-each-ref", "refs/nw/gate-jobs/") == ""


def _run_drop(world, remote, variant):
    """A gate run whose ssh drops after the `gate` frame, and the outcome it must have."""
    world.split_counter()
    remote.register(ssh=remote.variants[variant])
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    _assert_fell_back(world, remote, run, "ssh", cooldown=True)
    assert len(run.results) == 3


@pytest.mark.parametrize("variant,stage", [("unreachable", "have"), ("job255", "ssh"), ("have", "have"),
                                           ("garbage", "protocol"), ("drop", "ssh")])
def test_transport_failures_fall_back_with_cooldown(world, remote, variant, stage):
    if variant == "drop":
        return _run_drop(world, remote, variant)
    remote.register(ssh=remote.variants[variant])
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    _assert_fell_back(world, remote, run, stage, cooldown=True)


def test_drop_ssh_survives_a_slow_worker_side(world, remote):
    """The stage is `ssh` however long the worker side lingers before the connection drops."""
    _run_drop(world, remote, "slowdrop")


def test_a_drop_deletes_the_worker_files_the_local_run_does_not_rewrite(world, remote):
    """The worker's `.r1` files of a gate that failed remotely must go: the local re-run passes
    on its first try and writes no `.r1` of its own."""
    world.project["gates"] = [_gate("g", 'case "$NW_DATA_DIR" in *wdata*) exit 1;; esac; '
                                         'echo ok', retries=1)]
    remote.register(ssh=remote.variants["drop"])
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    assert run.worker is None and run.seq == 1 and run.ok
    (ev,) = world.ledger(["worker_failed"])
    assert ev["stage"] == "ssh" and remote.record()["cooldown_until"] is not None
    (sent,) = [f for f in remote.frames() if f["type"] == "gate"]
    assert "001-g.r1.log" in sent["files"] and "001-g.r1.json" in sent["files"]
    assert sorted(p.name for p in world.gates_dir.iterdir()) == ["001-g.json", "001-g.log"]
    for p in world.gates_dir.glob("*.json"):
        assert json.loads(p.read_text())["cwd"] == str(world.repo)


def test_job_timeout_is_the_budget_plus_every_attempt_capped_at_4h():
    from nightwright.agentic import remote_gates as RG
    cfg = {"agentic": {"events": {"gate_budget_s": 1000}}}
    gates = [{"name": "a", "timeout_s": 60}, {"name": "b", "timeout_s": 100, "retries": 2}]
    assert RG.job_timeout_s(cfg, gates) == 1000 + 60 + 100 * 3
    assert RG.job_timeout_s({}, [{"name": "c"}]) == 3600 + 1200    # the gate's own defaults
    huge = [{"name": "d", "timeout_s": 4000, "retries": 3}]
    assert RG.job_timeout_s(cfg, huge) == RG.MAX_JOB_TIMEOUT_S == 4 * 3600


def _frame_line(kind, **kw):
    from nightwright.agentic import worker as W
    return json.dumps({"schema": W.FRAME_SCHEMA, "job_id": f"{CID}-gate-001-a0", "type": kind,
                       **kw})


@pytest.mark.parametrize("how", ["signal", "frame"])
def test_a_worker_side_interruption_falls_back_with_cooldown(world, remote, tmp_path, how):
    """`done` with `interrupted: true` the coordinator never asked for (the worker host got
    SIGTERM) is a transport failure: fall back, not a red gate."""
    if how == "signal":
        ppid = tmp_path / "gate-job.pid"
        world.project["gates"][0]["cmd"] = (f'case "$NW_DATA_DIR" in *wdata*) echo $PPID > {ppid}; '
                                            f'exec sleep 600;; esac; echo ok')
        t = threading.Thread(target=lambda: until(
            lambda: ppid.exists() and ppid.read_text().strip())
            and os.kill(int(ppid.read_text()), 15))
        t.start()
    else:
        lines = [_frame_line("started", slot_wait_s=0.0),
                 _frame_line("done", ok=False, started_at="x", duration_s=0.1, slot_wait_s=0.0,
                             interrupted=True)]
        remote.register(ssh=_gate_job_variant(
            remote, "cut", "; ".join(f"echo '{ln}'" for ln in lines) + "; exec cat >/dev/null"))
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    if how == "signal":
        join(t)
    _assert_fell_back(world, remote, run, "interrupted", cooldown=True)


@pytest.mark.parametrize("frames", [
    [("started", {"slot_wait_s": "abc"})],
    [("started", {"slot_wait_s": float("inf")})],
    [("done", {"ok": "yes", "duration_s": 1.0})],
    [("done", {"ok": False, "duration_s": "x"})],
    [("done", {"ok": True, "duration_s": 1.0, "interrupted": "no"})],
    [("done", {"duration_s": 1.0})],
    [("done", {"ok": True, "duration_s": 1.0})],                      # no `changed`
    [("done", {"ok": True, "duration_s": 1.0, "changed": "a.txt"})],
    [("done", {"ok": True, "duration_s": 1.0, "changed": [1]})],
    # a gate frame whose result builds but whose `attempts` is not iterable
    [("started", {"slot_wait_s": 0.0}),
     ("gate", {"files": {}, "result": {"name": "ok", "required": True, "ok": True,
                                       "exit_code": 0, "timed_out": False, "duration_s": 0.1,
                                       "log": "x", "tail": "", "attempts": 5}})],
])
def test_bad_worker_numbers_and_flags_fall_back(world, remote, frames):
    body = "; ".join(f"echo '{_frame_line(k, **kw)}'" for k, kw in frames)
    remote.register(ssh=_gate_job_variant(remote, "badnum", body + "; exec cat >/dev/null"))
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    _assert_fell_back(world, remote, run, "protocol", cooldown=True)


_EVIL = b"EVIL-PAYLOAD-0338\n"


@pytest.mark.parametrize("gate,files", [
    ("ok", {"../../evil.log": "PAYLOAD"}),        # out of `gates/`
    ("ok", {"001-opt.log": "PAYLOAD"}),           # another gate's stem
    ("ok", {"002-ok.log": "PAYLOAD"}),            # the wrong seq
    ("ok", {"001-ok.log/x": "PAYLOAD"}),          # a path under a gate file
    ("ok", {"001-ok.log": 5}),                    # a non-string payload
    ("nope", {"001-nope.log": "PAYLOAD"}),        # a gate the job does not run
])
def test_a_gate_frame_naming_a_bad_file_falls_back_and_writes_nothing(world, remote, gate, files):
    """The worker names the files the coordinator writes under `gates/`: only
    `<seq>-<gate>(.rN).{log,json}` of one of the job's gates is taken, anything else is a
    protocol failure that writes nothing (followed by a valid `done`, so a missing check would
    end the run remotely instead)."""
    import base64
    import datetime as dt
    import gzip
    payload = base64.b64encode(gzip.compress(_EVIL)).decode()
    files = {k: (payload if v == "PAYLOAD" else v) for k, v in files.items()}
    result = {"name": gate, "required": True, "ok": True, "exit_code": 0, "timed_out": False,
              "duration_s": 0.1, "log": "x", "tail": "", "attempts": []}
    lines = [_frame_line("started", slot_wait_s=0.0), _frame_line("gate", files=files, result=result),
             _frame_line("done", ok=True, started_at="x", duration_s=0.1, slot_wait_s=0.0,
                         changed=[])]
    remote.register(ssh=_gate_job_variant(
        remote, "badfile", "; ".join(f"echo '{ln}'" for ln in lines) + "; exec cat >/dev/null"))
    before = dt.datetime.now().astimezone()
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    _assert_fell_back(world, remote, run, "protocol", cooldown=True)
    until_ = dt.datetime.fromisoformat(remote.record()["cooldown_until"])
    assert abs((until_ - before).total_seconds() - 600) < 60     # COOLDOWN_S from now
    assert not list(world.tmp.rglob("evil.log"))
    assert not (world.gates_dir.parent.parent / "evil.log").exists()
    leaked = [p for p in world.tmp.rglob("*") if p.is_file() and _EVIL in p.read_bytes()]
    assert leaked == []


def _fake_gate_job(remote, name, lines):
    remote.register(ssh=_gate_job_variant(
        remote, name, "; ".join(f"echo '{ln}'" for ln in lines) + "; exec cat >/dev/null"))


def _gate_result(name, ok=True):
    return {"name": name, "required": True, "ok": ok, "exit_code": 0 if ok else 1,
            "timed_out": False, "duration_s": 0.1, "log": "x", "tail": "", "attempts": []}


def _done(**kw):
    return _frame_line("done", **{"ok": True, "started_at": "x", "duration_s": 0.1,
                                  "slot_wait_s": 0.0, "changed": [], **kw})


@pytest.mark.parametrize("payload", ["corrupt", "truncated", "oversize"])
def test_a_bad_gate_file_payload_falls_back(world, remote, monkeypatch, payload):
    """A corrupt deflate stream (`zlib.error`, not an OSError), a cut one, or one inflating past
    `MAX_FILE_BYTES` is a protocol failure, never an exception out of `run_all` or an
    unbounded allocation."""
    import base64
    import gzip
    from nightwright.agentic import remote_gates as RG
    raw = gzip.compress(os.urandom(4000))
    if payload == "corrupt":
        raw = raw[:10] + bytes(b ^ 0xFF for b in raw[10:200]) + raw[200:]
    elif payload == "truncated":
        raw = raw[:len(raw) // 2]
    else:
        monkeypatch.setattr(RG, "MAX_FILE_BYTES", 1000)
    data = base64.b64encode(raw).decode()
    _fake_gate_job(remote, f"bad-{payload}", [
        _frame_line("started", slot_wait_s=0.0),
        _frame_line("gate", files={"001-ok.log": data}, result=_gate_result("ok")), _done()])
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    _assert_fell_back(world, remote, run, "protocol", cooldown=True)


def test_done_needs_one_result_per_gate(world, remote):
    _fake_gate_job(remote, "short", [
        _frame_line("started", slot_wait_s=0.0),
        _frame_line("gate", files={}, result=_gate_result("ok")), _done()])
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    _assert_fell_back(world, remote, run, "protocol", cooldown=True)


def test_the_remote_verdict_is_computed_from_the_results(world, remote):
    """A `done` saying ok over a failed required gate is still a red run."""
    world.project["gates"] = [_gate("ok", "echo ok")]
    _fake_gate_job(remote, "liar", [
        _frame_line("started", slot_wait_s=0.0),
        _frame_line("gate", files={}, result=_gate_result("ok", ok=False)), _done(ok=True)])
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    assert run.worker == "box" and not run.ok and not world.ledger(["worker_failed"])


def test_changed_null_falls_back_without_cooldown(world, remote):
    """The worker could not tell what the gates wrote: re-run locally, the host is fine."""
    world.project["gates"] = [_gate("ok", "echo ok")]
    _fake_gate_job(remote, "nullchanged", [
        _frame_line("started", slot_wait_s=0.0),
        _frame_line("gate", files={}, result=_gate_result("ok")), _done(changed=None)])
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    assert run.worker is None and run.ok and run.seq == 1
    (ev,) = world.ledger(["worker_failed"])
    assert ev["stage"] == "tree" and not ev["cooldown"]
    assert remote.record()["cooldown_until"] is None


def test_an_io_setup_failure_falls_back_and_ends_ssh(world, remote, monkeypatch):
    """`os.dup` of SSH's stdin failing (fd exhaustion) is a coordinator-side failure: fall
    back without cooldown, and the started SSH process is still ended."""
    import types
    from nightwright.agentic import remote_gates as RG
    procs = []
    real_popen = RG.subprocess.Popen

    def popen(*a, **kw):
        procs.append(real_popen(*a, **kw))
        return procs[-1]

    def dup(fd):
        raise OSError(24, "Too many open files")
    monkeypatch.setattr(RG, "os", types.SimpleNamespace(**{**vars(os), "dup": dup}))
    monkeypatch.setattr(RG.subprocess, "Popen", popen)
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    monkeypatch.undo()
    _assert_fell_back(world, remote, run, "ssh", cooldown=False)
    assert procs and all(p.poll() is not None for p in procs)


def _big_commit(world, mib=2):
    (world.repo / "big.bin").write_bytes(os.urandom(mib << 20))
    _git(world.repo, "add", "big.bin")
    _git(world.repo, "commit", "-q", "-m", "big")


def test_an_upload_longer_than_the_slot_wait_stays_remote(world, remote, spy):
    """The upload has its own bound (`slot_wait_s` + bytes / `UPLOAD_MIN_BPS`): a bundle that
    takes longer than `slot_wait_s` to send does not fall back as a slot wait."""
    _big_commit(world)
    # the slot wait restarts when the upload ends, so it must cover the worker's own work on the
    # bundle (a slow box needs >1s); the upload outlasts it by one second
    world.cfg["agentic"]["workers"] = {"slot_wait_s": 4}
    remote.register(ssh=_gate_job_variant(remote, "slowup", f'sleep 5; exec {remote.ssh} "$@"'))
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    assert spy[0]["header"]["bundle_bytes"] > 2 << 20
    assert run.worker == "box" and run.ok and not world.ledger(["worker_failed"])


def test_an_upload_past_its_bound_falls_back_with_cooldown(world, remote, monkeypatch):
    """A link too slow for the bundle fails the same way on the next run: `upload`, with a
    cooldown, so later runs stop paying the wait."""
    from nightwright.agentic import remote_gates as RG
    _big_commit(world)
    monkeypatch.setattr(RG, "UPLOAD_MIN_BPS", 1 << 40)     # the bound is ~`slot_wait_s`
    world.cfg["agentic"]["workers"] = {"slot_wait_s": 1}
    remote.register(ssh=_gate_job_variant(remote, "stuckup", "exec sleep 600"))
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    _assert_fell_back(world, remote, run, "upload", cooldown=True)


def test_repo_key_is_the_project_and_12_hex_of_the_shared_repo(world):
    import re
    from nightwright.agentic import remote_gates as RG
    other = world.tmp / "wt2"
    _git(world.repo, "worktree", "add", "-q", "-b", "other", str(other))
    key = RG.repo_key("My Demo", world.repo)
    assert re.fullmatch(r"my-demo-[0-9a-f]{12}", key), key
    assert RG.repo_key("My Demo", other) == key                 # one repo, one cache
    assert RG.repo_key(None, world.repo).startswith("repo-")
    (world.tmp / "elsewhere").mkdir()
    twin = World(world.tmp / "elsewhere")
    assert RG.repo_key("My Demo", twin.repo) != key             # another repo, another key


def _tree(repo):
    status = _git(repo, "status", "--porcelain", "--untracked-files=all")
    return status, {p.name: p.read_text() for p in sorted(repo.iterdir()) if p.is_file()}


def test_a_gate_that_writes_the_tree_is_rerun_locally(world, remote, tmp_path):
    """The worker's writes never reach the coordinator's worktree (`--write-ledger`, a
    formatter): a run whose gates changed its checkout is re-run locally, without cooldown, so
    the worktree ends as a local run leaves it and no tree passes only on the worker."""
    gates = [_gate("regen", "printf 'regen\\n' > a.txt; printf 'new\\n' > gen.txt"),
             _gate("check", "grep -q regen a.txt")]
    (tmp_path / "twin").mkdir()
    twin = World(tmp_path / "twin")                     # the reference: no worker at all
    twin.project["gates"] = gates
    ref = G.run_all(twin.project, twin.state, twin.cfg, seq=1)
    assert ref.ok and ref.worker is None
    world.project["gates"] = gates
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    assert run.worker is None and run.seq == 1 and run.ok
    (ev,) = world.ledger(["worker_failed"])
    assert ev["stage"] == "tree" and ev["cooldown"] is False and "a.txt" in ev["error"]
    assert remote.record()["cooldown_until"] is None
    assert remote.frames()[-1]["changed"] == ["a.txt", "gen.txt"]
    assert _tree(world.repo) == _tree(twin.repo)
    assert (world.repo / "a.txt").read_text() == "regen\n"
    assert sorted(p.name for p in world.gates_dir.iterdir()) == sorted(
        f"001-{n}.{x}" for n in ("regen", "check") for x in ("log", "json"))
    for p in world.gates_dir.glob("*.json"):
        assert json.loads(p.read_text())["cwd"] == str(world.repo)


def test_a_gate_that_rewrites_the_tree_unchanged_stays_remote(world, remote):
    """A generator whose output is already current (a fresh ledger) writes nothing new."""
    world.project["gates"] = [_gate("regen", "printf 'a\\n' > a.txt")]
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    assert run.worker == "box" and run.ok
    assert not world.ledger(["worker_failed"])
    assert remote.frames()[-1]["changed"] == []


def test_worker_tree_changes_lists_renames_once(tmp_path):
    from nightwright.agentic import worker as W
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _commit(repo, "a.txt", "a\n")
    assert W.tree_changes(repo) == []
    _git(repo, "mv", "a.txt", "b.txt")
    (repo / "c d.txt").write_text("x")
    assert W.tree_changes(repo) == ["b.txt", "c d.txt"]
    assert W.tree_changes(tmp_path / "nope") is None


def test_worker_tree_changes_survives_a_non_utf8_name(tmp_path):
    from nightwright.agentic import worker as W
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _commit(repo, "a.txt", "a\n")
    os.close(os.open(os.fsencode(repo) + b"/" + _NON_UTF8, os.O_CREAT | os.O_WRONLY))
    assert W.tree_changes(repo) == ["\ufffd\ufffd"]


def test_a_gate_writing_a_non_utf8_name_falls_back_as_tree(world, remote):
    world.project["gates"] = [_gate("gen", "printf x > \"$(printf '\\377\\376')\"")]
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    assert run.worker is None and run.seq == 1 and run.ok
    assert run.results[0].cwd == str(world.repo)
    (ev,) = world.ledger(["worker_failed"])
    assert ev["stage"] == "tree" and ev["cooldown"] is False
    assert remote.record()["cooldown_until"] is None
    assert remote.frames()[-1]["changed"] == ["\ufffd\ufffd"]


def test_an_unusable_ssh_socket_dir_falls_back_without_cooldown(world, remote, monkeypatch,
                                                                tmp_path):
    """The ControlPath dir is the coordinator's (a symlink, as a shared /tmp path could be):
    stage `ssh`, no cooldown, and no SSH call at all."""
    from nightwright.agentic import workers as WK
    (tmp_path / "elsewhere").mkdir()
    link = tmp_path / "sshdir"
    link.symlink_to(tmp_path / "elsewhere")
    monkeypatch.setattr(WK, "ssh_dir", lambda cfg, extra=0: link)
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    _assert_fell_back(world, remote, run, "ssh", cooldown=False)
    assert "ssh socket directory" in world.ledger(["worker_failed"])[0]["error"]
    assert not (remote.wdata / "agentic" / "worker" / "jobs").exists()


def test_an_identity_worker_gets_its_own_controlpath_dir_created(world, remote, monkeypatch,
                                                                 request):
    """SPEC-0354: an `identity` record's socket name is `TAG_LEN` bytes longer, so a 41..49
    byte first candidate fits a plain record but not it, and its ControlPath goes to the next
    candidate. The gate path must create and check that dir, not the plain record's: a fake
    `ssh` fails as OpenSSH's ControlMaster bind does (exit 255) when the dir is missing."""
    import shutil
    import tempfile
    from nightwright.agentic import workers as WK
    xdg = Path(tempfile.mkdtemp(prefix="nwi", dir="/tmp"))     # short, whatever TMPDIR is
    request.addfinalizer(lambda: shutil.rmtree(xdg, ignore_errors=True))
    first = xdg / ("p" * (45 - len(os.fsencode(str(xdg))) - 1))
    second = xdg / "nw-ssh-second"
    assert len(os.fsencode(str(first))) == 45
    monkeypatch.setattr(WK, "_ssh_dir_candidates", lambda cfg: [first, second])
    assert WK.ssh_dir(world.cfg) == first and WK.ssh_dir(world.cfg, WK.TAG_LEN) == second
    log = world.tmp / "controlpaths"
    bindir = world.tmp / "bin"
    bindir.mkdir()
    _script(bindir / "ssh", (
        'for a in "$@"; do case "$a" in ControlPath=*) d=$(dirname "${a#ControlPath=}")\n'
        f'  echo "$d" >> {log}\n'
        '  [ -d "$d" ] || { echo "unix_listener: cannot bind: No such file" >&2; exit 255; };;\n'
        'esac; done\n'
        'while [ $# -gt 0 ]; do case "$1" in -o|-i) shift 2;; -*) shift;; *) break;; esac; done\n'
        f'exec {remote.ssh} "$@"'))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    p = WK.path(world.cfg)
    p.write_text(json.dumps({"schema": WK.SCHEMA, "workers": [
        {"name": "box", "ssh": "me@box", "slots": 1, "nw": "nw",
         "identity": str(world.tmp / "nw_worker_ed25519")}]}))
    argv = remote.record()["ssh_argv"]
    assert f"ControlPath={second}/%C-{WK.identity_tag(str(world.tmp / 'nw_worker_ed25519'))}" \
        in argv
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    assert run.worker == "box" and run.ok, world.ledger(["worker_failed"])
    assert set(log.read_text().split()) == {str(second)}
    st = second.lstat()
    assert stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o700
    assert not first.exists()
    # the dir it uses is the one checked: a symlink there is refused, with no ssh call
    (xdg / "elsewhere").mkdir()
    second.rmdir()
    second.symlink_to(xdg / "elsewhere")
    log.unlink()
    G.reset()
    run = G.run_all(world.project, world.state, world.cfg, seq=2)
    assert run.worker is None and run.ok
    ev = world.ledger(["worker_failed"])[-1]
    assert ev["stage"] == "ssh" and "ssh socket directory" in ev["error"]
    assert not log.exists()


def test_a_no_cooldown_fallback_waits_for_a_local_slot(world, remote):
    """The worker is not cooled down after a `claim` error, so a fallback acquire over every
    pool would take its free slot again; it must wait for the local one."""
    world.cfg["agentic"]["gates"]["slots"] = 1
    (remote.wdata / "agentic" / "worker" / "jobs" / f"{CID}-gate-001-a0").mkdir(parents=True)
    out = {}
    with GS.acquire(world.cfg, "other"):
        t = threading.Thread(target=lambda: out.update(run=G.run_all(
            world.project, world.state, world.cfg, seq=1)))
        t.start()
        assert until(lambda: world.ledger(["worker_failed"])
                     and GS.waiting(world.cfg, CID) is not None)
        threading.Event().wait(1.5)
        assert "run" not in out                 # still waiting for the local slot
    join(t)
    run = out["run"]
    _assert_fell_back(world, remote, run, "claim", cooldown=False)
    assert run.slot_wait_s >= 1.5


def test_a_disabled_worker_is_no_worker(world, remote, invoke):
    remote.register(enabled=False)
    (world.repo / "new.txt").write_text("x\n")                 # ineligible, but never asked
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    assert run.worker is None and run.results[0].cwd == str(world.repo)
    assert not world.ledger(["finding"]) and not world.ledger(["worker_failed"])
    (world.repo / "new.txt").unlink()
    r = invoke("queue", "gates", CID, "--worker", "box")
    assert r.exit_code != 0 and "unknown or disabled" in r.output


def test_a_fallback_run_counts_the_fallback_wait(world, remote):
    world.cfg["agentic"]["gates"]["slots"] = 1
    remote.register(ssh=remote.variants["unreachable"])
    out = {}
    with GS.acquire(world.cfg, "other"):
        t = threading.Thread(target=lambda: out.update(run=G.run_all(
            world.project, world.state, world.cfg, seq=1)))
        t.start()
        assert until(lambda: GS.waiting(world.cfg, CID) is not None
                     and world.ledger(["worker_failed"]))
        threading.Event().wait(1.5)             # the fallback waits for the local slot
    join(t)
    run = out["run"]
    assert run.worker is None and run.ok and run.seq == 1
    assert run.slot_wait_s >= 1.5


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


def _reuse_tiers(world):
    """SPEC-0376: the repo's own shape: a loop gate and a close gate that `reuse_from`s it."""
    world.project["gates"] = [_gate("fast", "echo fast", when="loop"),
                              _gate("full", "echo full; git rev-parse HEAD", when="close",
                                    reuse_from="fast"),
                              _gate("lint", "echo lint")]


def test_a_close_tier_run_with_reuse_from_is_remote_eligible(world, remote, spy):
    """SPEC-0376 (IDEA-782): the close tier's filtered gate list carries `reuse_from` without
    its loop gate; the header drops the key, so the run is eligible and runs on the worker."""
    from nightwright.agentic import project as P
    from nightwright.agentic import remote_gates as RG
    _reuse_tiers(world)
    close_cfg = {**world.project, "gates": P.gates_for(world.project, "close")}
    assert [g["name"] for g in close_cfg["gates"]] == ["full", "lint"]
    assert RG.header_refusal(close_cfg, world.state, world.cfg, 1) is None
    assert RG.pools_for(world.cfg, close_cfg, world.state, seq=1) is not None
    run = G.run_all(world.project, world.state, world.cfg, seq=1, tier="close")
    assert run.worker == "box" and run.ok
    assert [r.name for r in run.results] == ["full", "lint"]
    (seen,) = spy
    assert all("reuse_from" not in g and "remote" not in g
               for g in seen["header"]["payload"]["gates"])
    assert not [f for f in world.ledger(["finding"]) if f["gate"] == RG.FINDING_GATE]


def test_eligibility_is_re_evaluated_at_every_gate_start(world, remote):
    """SPEC-0376: no verdict persists: a run refused once is eligible again as soon as the
    cause is gone, though the one-per-campaign finding stays the only record."""
    from nightwright.agentic import remote_gates as RG
    bad = {**world.state, "repo": {**world.state["repo"], "base": "release/.hidden"}}
    assert RG.pools_for(world.cfg, world.project, bad, seq=1) is None
    assert RG.pools_for(world.cfg, world.project, world.state, seq=2) is not None
    run = G.run_all(world.project, world.state, world.cfg, seq=2)
    assert run.worker == "box"
    assert len([f for f in world.ledger(["finding"]) if f["gate"] == RG.FINDING_GATE]) == 1


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


def test_registry_slots_raise_the_worker_hosts_own_limit(world, remote):
    """SPEC-0391 (IDEA-813): a worker registered with 3 slots runs a third job while its host's
    own default pool of 2 is full; the header carries the registry value."""
    # Generous wait: a loaded box must not expire it and fall back to local (flaky); with the
    # worker change reverted the job waits out the full 20 s and falls back, so this still fails.
    world.cfg["agentic"]["workers"] = {"slot_wait_s": 20}
    remote.register(slots=3)
    with _hold_worker_slots(remote):
        run = G.run_all(world.project, world.state, world.cfg, seq=1)
    assert run.worker == "box" and run.ok and not world.ledger(["worker_failed"])
    assert run.slot_wait_s < 20


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



def test_a_worker_removed_between_slot_and_dispatch_falls_back_without_cooldown(
        world, remote, monkeypatch):
    """`nw workers rm box` lands after the remote slot is held: `stage: registry`, no cooldown
    (there is no record left to cool), the gates run locally under the same seq."""
    from nightwright.agentic import workers as WK
    real = GS.acquire
    seen = []

    def acquire(cfg, cid, **kw):
        cm = real(cfg, cid, **kw)
        if kw.get("pools") is None:
            return cm
        import contextlib

        @contextlib.contextmanager
        def held():
            with cm as hold:
                seen.append(hold.pool)
                WK.path(world.cfg).write_text(json.dumps({"schema": WK.SCHEMA, "workers": []}))
                yield hold
        return held()
    monkeypatch.setattr(GS, "acquire", acquire)
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    assert seen == ["box"]
    assert run.worker is None and run.ok and run.seq == 1 and len(run.results) == 3
    (ev,) = world.ledger(["worker_failed"])
    assert ev["stage"] == "registry" and ev["cooldown"] is False and ev["worker"] == "box"
    assert WK.load(world.cfg) == []
    assert all(r.cwd == str(world.repo) for r in run.results)
    assert not (remote.wdata / "agentic" / "worker" / "jobs").exists()
    assert not S.child_tmp(world.cfg, CID, "gate-001").exists()


_DEEP = "import sys; n = 300000; sys.stdout.write('[' * n + ']' * n + chr(10))"


@pytest.mark.parametrize("bad", [
    {"name": ["ok"]}, {"name": {"a": 1}}, {"name": None},          # unhashable / not a str
    {"duration_s": "1.0"}, {"duration_s": float("inf")}, {"exit_code": "0"},
    {"ok": "yes"}, {"required": 1}, {"cmd": 5}, {"summary": [1]}, {"flaky": "x"},
    "deep",                                                         # RecursionError in json
])
def test_a_malformed_gate_frame_falls_back_as_protocol(world, remote, bad):
    """Worker data a `GateResult` cannot hold (or a line json cannot parse without recursing
    out) is a `protocol` fallback with cooldown, never an exception out of `run_all`. One
    gate, so the `done` that follows would end a run that took the result."""
    world.project["gates"] = [_gate("ok", "echo ok")]
    if bad == "deep":
        body = f"{sys.executable} -c \"{_DEEP}\"; exec cat >/dev/null"
        remote.register(ssh=_gate_job_variant(remote, "deep", body))
    else:
        result = {**_gate_result("ok"), **bad}                      # inf -> `Infinity`
        _fake_gate_job(remote, "badresult", [
            _frame_line("started", slot_wait_s=0.0),
            _frame_line("gate", files={}, result=result), _done()])
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    assert run.worker is None and run.ok and run.seq == 1
    assert [r.cwd for r in run.results] == [str(world.repo)]
    (ev,) = world.ledger(["worker_failed"])
    assert ev["stage"] == "protocol" and ev["cooldown"] is True
    assert remote.record()["cooldown_until"] is not None
    assert sorted(p.name for p in world.gates_dir.iterdir()) == ["001-ok.json", "001-ok.log"]


def test_frame_parsing_turns_deep_nesting_into_a_protocol_failure():
    from nightwright.agentic import remote_gates as RG
    n = 300000
    with pytest.raises(RG.RemoteFailure) as e:
        RG._frame(("[" * n + "]" * n).encode(), "job")
    assert e.value.stage == "protocol"


def test_frame_job_id_is_required_except_on_error_frames():
    from nightwright.agentic import remote_gates as RG
    from nightwright.agentic import worker as W
    jid = "SPEC-gate-001-a1"
    gate = json.dumps({"schema": W.FRAME_SCHEMA, "type": "gate", "n": 2, "files": {},
                       "result": {"name": "ok", "required": True, "ok": True, "exit_code": 0,
                                  "timed_out": False, "cmd": "true", "cwd": "/", "duration_s": 0.0,
                                  "tail": "x"}})
    with pytest.raises(RG.RemoteFailure) as e:
        RG._frame((gate + "\n").encode(), jid)
    assert e.value.stage == "protocol"
    err = json.dumps({"schema": W.FRAME_SCHEMA, "type": "error", "stage": "validate",
                      "error": "bad header"})
    assert RG._frame((err + "\n").encode(), jid)["stage"] == "validate"


def test_a_stop_during_have_ends_it_at_once_and_sends_no_job(world, remote, tmp_path,
                                                            monkeypatch):
    """A stop while `nw worker have` hangs on a slow host ends that SSH call at once (not after
    `HAVE_TIMEOUT_S`), starts no `gate-job`, and returns an interrupted run: no
    `worker_failed`, no cooldown, no local re-run."""
    from nightwright.agentic import remote_gates as RG
    monkeypatch.setattr(RG, "HAVE_TIMEOUT_S", 600.0)
    have, job = tmp_path / "have.pid", tmp_path / "job-started"
    remote.register(ssh=_script(tmp_path / "ssh-slowhave",
                                f'case "$*" in *" have "*) echo $$ > {have}; exec sleep 600;; '
                                f'*gate-job*) touch {job};; esac\nexec {remote.ssh} "$@"'))
    stop = threading.Event()
    out = {}
    t = threading.Thread(target=lambda: out.update(run=G.run_all(
        world.project, world.state, world.cfg, seq=1, stop=stop)))
    t.start()
    assert until(lambda: have.exists() and have.read_text().strip())
    pid = int(have.read_text())
    stop.set()
    join(t, timeout=15)
    run = out["run"]
    assert run.interrupted and not run.ok and run.seq == 1 and run.results == ()
    assert not job.exists()
    assert not world.ledger(["worker_failed"]) and remote.record()["cooldown_until"] is None
    assert not world.gates_dir.exists() or not list(world.gates_dir.iterdir())
    assert not S.child_tmp(world.cfg, CID, "gate-001").exists()
    assert _git(world.repo, "for-each-ref", "refs/nw/gate-jobs/") == ""

    def gone():
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False
    assert until(gone, timeout=10)                # the hung `have` was ended, not waited out


@pytest.mark.parametrize("sticky", [False, True])
def test_a_stop_after_prepare_starts_no_job(world, remote, tmp_path, monkeypatch, sticky):
    """A stop (the caller's event, or the runner's sticky `terminate_all`) that lands once the
    bundle is cut starts no `gate-job` only to cancel it."""
    from nightwright.agentic import remote_gates as RG
    job = tmp_path / "job-started"
    remote.register(ssh=_gate_job_variant(remote, "late-job",
                                          f'touch {job}; exec {remote.ssh} "$@"'))
    stop = threading.Event()
    real = RG.prepare

    def prepare(*a, **kw):
        h = real(*a, **kw)
        if sticky:
            G.terminate_all()
        else:
            stop.set()
        return h
    monkeypatch.setattr(RG, "prepare", prepare)
    run = G.run_all(world.project, world.state, world.cfg, seq=1, stop=stop)
    assert run.interrupted and not run.ok and run.results == () and run.worker is None
    assert not job.exists()
    assert not world.ledger(["worker_failed"]) and remote.record()["cooldown_until"] is None
    assert not (remote.wdata / "agentic" / "worker" / "jobs").exists()

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
    r = invoke("campaign", "show", CID, "--raw")
    assert "worker=box" in r.output
    workers = [e.get("worker") for e in world.ledger(["gate"])]
    assert workers == ["box", None]


def test_queue_gates_json_shows_changed_for_a_remote_run(world, remote, invoke):
    r = invoke("queue", "gates", CID, "--worker", "box", "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["changed"] == []
    r = invoke("queue", "gates", CID, "--local", "--json")
    assert r.exit_code == 0, r.output
    assert "changed" not in json.loads(r.output)


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


# ---- worker-side rejection (SPEC-0367) ---------------------------------------------------------

def _rejected_findings(world):
    return [e for e in world.ledger(["finding"]) if e.get("gate") == "worker-rejected"]


def _local_results(run):
    return [(r.name, r.ok) for r in run.results]


def _reject_once(world, remote, capfd):
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    return run, capfd.readouterr().err


def test_a_validate_rejection_is_loud_and_deduplicated(world, remote, capfd):
    frame = json.dumps({"schema": "nw.job.frame.v1", "type": "error", "stage": "validate",
                        "error": "unknown gate key warm"})
    remote.register(ssh=_gate_job_variant(
        remote, "rej-validate", f"echo '{frame}'; exec cat >/dev/null"))
    run, err = _reject_once(world, remote, capfd)
    assert run.worker is None and run.ok
    (ev,) = world.ledger(["worker_failed"])
    assert ev["stage"] == "validate" and ev["rejected"] is True and not ev["cooldown"]
    (f,) = _rejected_findings(world)
    assert f["persona"] == "gates" and f["severity"] == "info"
    assert "unknown gate key" in f["detail"] and "nw workers bootstrap box --ssh me@box" in f["detail"]
    assert "nw: worker box rejected the remote gate job (validate: unknown gate key" in err
    assert "nw workers bootstrap box --ssh me@box --reinstall" in err
    run2 = G.run_all(world.project, world.state, world.cfg, seq=2)
    err2 = capfd.readouterr().err
    assert "rejected the remote gate job" in err2                # the line prints every time
    assert len(_rejected_findings(world)) == 1                   # the finding does not repeat
    assert [e["rejected"] for e in world.ledger(["worker_failed"])] == [True, True]
    assert _local_results(run2) == _local_results(run)


def test_a_done_without_changed_is_a_rejection(world, remote, capfd):
    world.project["gates"] = [_gate("ok", "echo ok")]
    old = {k: v for k, v in json.loads(_done()).items() if k != "changed"}
    _fake_gate_job(remote, "nochanged", [
        _frame_line("started", slot_wait_s=0.0),
        _frame_line("gate", files={}, result=_gate_result("ok")), json.dumps(old)])
    run, err = _reject_once(world, remote, capfd)
    assert run.worker is None and run.ok
    (ev,) = world.ledger(["worker_failed"])
    assert ev["stage"] == "protocol" and ev["rejected"] is True and ev["cooldown"]
    assert len(_rejected_findings(world)) == 1
    assert "worker box rejected the remote gate job (protocol:" in err


@pytest.mark.parametrize("variant", ["unreachable", "job255", "drop"])
def test_transport_failures_are_not_rejections(world, remote, capfd, variant):
    remote.register(ssh=remote.variants[variant])
    run, err = _reject_once(world, remote, capfd)
    (ev,) = world.ledger(["worker_failed"])
    assert "rejected" not in ev
    assert _rejected_findings(world) == [] and "rejected the remote gate job" not in err


def test_only_validate_and_protocol_failures_are_rejections():
    for stage in ("upload", "timeout", "ssh", "slot_wait", "interrupted", "have", "bundle"):
        assert RG.RemoteFailure(stage, "x").rejected is False
    assert RG.RemoteFailure("validate", "x", rejected=True).rejected is True
    assert RG.RemoteFailure("protocol", "x").rejected is True
