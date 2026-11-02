"""SPEC-0381: `nw gate run` -- standalone remote gate jobs. The worker dispatch is a stub
(`remote_gates.run_detached` swapped): these tests cover resolution, placement (tokens, campaign
priority, worker-only pools), exit codes, idempotence, records, events and the views."""
import json
import os
import subprocess
import threading

import pytest
from click.testing import CliRunner

import waits
from nightwright.agentic import events as E
from nightwright.agentic import gate_jobs as J
from nightwright.agentic import gate_slots as GS
from nightwright.agentic import gates as G
from nightwright.agentic import project as P
from nightwright.agentic import queue as Q
from nightwright.agentic import remote_gates as RG
from nightwright.agentic import workers as WK
from nightwright.agentic.cli_queue import _gate_jobs_line, _workers_line
from nightwright.cli import cli


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def gw(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "checkout", "-q", "-b", "feat")
    (repo / "a.txt").write_text("b\n")
    _git(repo, "commit", "-q", "-am", "feat")
    cfg = {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "cfg"),
           "agentic": {"gates": {"slots": 2}}}
    monkeypatch.setattr(P, "find_project_for_cwd", lambda c, cwd=None: None)
    monkeypatch.delenv("NW_MAILBOX_ADDR", raising=False)
    calls = []
    behaviour = {"ok": True, "raise": None, "block": None}

    def detached(worker, pcfg, state, c, seq, gates_dir, stop=None, *, keep_changed=False):
        calls.append({"worker": worker, "gates": [g["name"] for g in pcfg["gates"]],
                      "state": state, "keep_changed": keep_changed, "gates_dir": gates_dir})
        if behaviour["block"] is not None:
            behaviour["block"].wait(30)
            if stop is not None and stop.is_set():
                return G.GateRun(seq, False, (), "", 0.0, interrupted=True)
        if behaviour["raise"]:
            raise RG.RemoteFailure("ssh", behaviour["raise"])
        gates_dir.mkdir(parents=True, exist_ok=True)
        res = []
        for g in pcfg["gates"]:
            (gates_dir / f"{seq:03d}-{g['name']}.log").write_text(f"log of {g['name']}\n")
            res.append(G.GateResult(name=g["name"], required=True, ok=behaviour["ok"],
                                    exit_code=0 if behaviour["ok"] else 1, timed_out=False,
                                    duration_s=0.5, log=f"gates/{seq:03d}-{g['name']}.log",
                                    tail="", summary=None if behaviour["ok"]
                                    else {"failing": ["tests/t.py::test_x"]}))
        return G.GateRun(seq, behaviour["ok"], tuple(res), "", 0.5, worker=worker)

    monkeypatch.setattr(RG, "run_detached", detached)
    return {"cfg": cfg, "repo": repo, "calls": calls, "b": behaviour, "tmp": tmp_path}


def _workers(cfg, *ws):
    p = WK.path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema": WK.SCHEMA, "workers": [
        {"name": n, "ssh": f"u@{n}", "slots": s, "kinds": ["gate"], "nw": "nw", "enabled": True}
        for n, s in ws]}))


def _invoke(cfg, monkeypatch, *args):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    return CliRunner().invoke(cli, ["gate", *args], catch_exceptions=False)


CMD = ("--cmd", "python3 -c 'print(1)'")


# ---- run: exit codes 0/1/3/4 and nw.gatejob.v1 --------------------------------------------

def test_green_run_on_the_worker_emits_gatejob_json(gw, monkeypatch):
    _workers(gw["cfg"], ("box", 4))
    r = _invoke(gw["cfg"], monkeypatch, "run", "--worktree", str(gw["repo"]), *CMD,
                "--by", "alice", "--json")
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["schema"] == "nw.gatejob.v1" and out["status"] == "passed" and out["exit"] == 0
    assert out["id"] == "GATEJOB-0001" and out["worker"] == "box" and out["failing"] == []
    assert {"id", "status", "exit", "gates", "failing", "log_dir", "worker", "duration"} <= set(out)
    assert out["gates"][0]["name"] == "cmd-1" and os.path.isfile(out["gates"][0]["log"])
    call = gw["calls"][0]
    assert call["keep_changed"] and call["state"]["id"] == "GATEJOB-0001"
    head = subprocess.run(["git", "-C", str(gw["repo"]), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    assert call["state"]["repo"] == {"worktree": str(gw["repo"]), "base": "main", "commit": head}
    rec = J.read(gw["cfg"], "GATEJOB-0001")
    assert rec["requester"] == "alice" and rec["branch"] == "feat"
    assert rec["gate_cfg"][0]["results"] == {"format": "pytest"}
    evs = [e for e in E.read_since(gw["cfg"], 0, types=["gate_job"])]
    assert [e["data"]["status"] for e in evs] == ["running", "passed"]


def test_red_run_exits_1_with_failing_ids_and_log_path(gw, monkeypatch):
    _workers(gw["cfg"], ("box", 4))
    gw["b"]["ok"] = False
    r = _invoke(gw["cfg"], monkeypatch, "run", "--worktree", str(gw["repo"]), *CMD)
    assert r.exit_code == 1, r.output
    assert "tests/t.py::test_x" in r.output and "gatejobs/GATEJOB-0001/gates" in r.output


def test_no_placeable_worker_exits_3_and_never_runs_locally(gw, monkeypatch):
    r = _invoke(gw["cfg"], monkeypatch, "run", "--worktree", str(gw["repo"]), *CMD)
    assert r.exit_code == 3 and "box unavailable" in r.output
    assert gw["calls"] == []
    _workers(gw["cfg"], ("box", 2))
    monkeypatch.setattr(WK, "gate_pools", lambda c, *a, **k: [(WK.LOCAL, 2)])
    assert J.worker_pools(gw["cfg"]) == []


def test_transport_failure_exits_4(gw, monkeypatch):
    _workers(gw["cfg"], ("box", 4))
    gw["b"]["raise"] = "ssh: connection refused"
    r = _invoke(gw["cfg"], monkeypatch, "run", "--worktree", str(gw["repo"]), *CMD, "--json")
    assert r.exit_code == 4
    out = json.loads(r.output)
    assert out["status"] == "transport_error" and "connection refused" in out["error"]


def test_slot_wait_timeout_exits_3(gw, monkeypatch):
    _workers(gw["cfg"], ("box", 1))
    with GS.acquire(gw["cfg"], "C-1", pools=[("box", 1)]):
        r = _invoke(gw["cfg"], monkeypatch, "run", "--worktree", str(gw["repo"]), *CMD,
                    "--wait-s", "0.3", "--json")
    assert r.exit_code == 3
    out = json.loads(r.output)
    assert out["status"] == "unavailable" and "box unavailable" in out["error"]
    assert gw["calls"] == []


# ---- resolution: preconditions exit 2 -------------------------------------------------------

def test_project_gates_drop_remote_false_and_respect_remote_gates_false(gw, monkeypatch):
    _workers(gw["cfg"], ("box", 4))
    pcfg = {"_root": str(gw["repo"]), "merge": {"base": "main"}, "gates": [
        {"name": "unit", "cmd": "pytest", "when": "loop"},
        {"name": "full", "cmd": "pytest", "when": "close"},
        {"name": "gpu", "cmd": "x", "when": "loop", "remote": False}]}
    monkeypatch.setattr(P, "project_config", lambda c, p: pcfg)
    t = J.resolve(gw["cfg"], project="demo", worktree=str(gw["repo"]))
    assert [g["name"] for g in t["gates"]] == ["unit"] and t["skipped"] == ["gpu"]
    pcfg["remote_gates"] = False
    with pytest.raises(J.JobError) as e:
        J.resolve(gw["cfg"], project="demo", worktree=str(gw["repo"]))
    assert e.value.code == 2 and "remote_gates: false" in e.value.message


def test_no_project_and_no_cmd_exits_2(gw, monkeypatch):
    _workers(gw["cfg"], ("box", 4))
    r = _invoke(gw["cfg"], monkeypatch, "run", "--worktree", str(gw["repo"]))
    assert r.exit_code == 2 and "--cmd" in r.output


def test_untracked_files_exit_2_and_a_dirty_tree_gates_a_stash_commit(gw, monkeypatch):
    (gw["repo"] / "new.txt").write_text("x")
    with pytest.raises(J.JobError) as e:
        J.resolve(gw["cfg"], worktree=str(gw["repo"]), cmds=["true"])
    assert e.value.code == 2 and "new.txt" in e.value.message
    (gw["repo"] / "new.txt").unlink()
    (gw["repo"] / "a.txt").write_text("dirty\n")
    t = J.resolve(gw["cfg"], worktree=str(gw["repo"]), cmds=["true"])
    head = subprocess.run(["git", "-C", str(gw["repo"]), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    assert t["commit"] != head
    assert subprocess.run(["git", "-C", str(gw["repo"]), "stash", "list"], capture_output=True,
                          text=True).stdout == ""


def test_branch_mode_gates_the_branch_tip_without_a_worktree(gw, monkeypatch):
    _git(gw["repo"], "checkout", "-q", "main")
    (gw["repo"] / "junk.txt").write_text("untracked is fine in branch mode")
    t = J.resolve(gw["cfg"], worktree=str(gw["repo"]), branch="feat", cmds=["true"])
    tip = subprocess.run(["git", "-C", str(gw["repo"]), "rev-parse", "feat"],
                         capture_output=True, text=True).stdout.strip()
    assert t["commit"] == tip and t["branch"] == "feat" and t["worktree"] is None
    with pytest.raises(J.JobError):
        J.resolve(gw["cfg"], worktree=str(gw["repo"]), branch="nope", cmds=["true"])
    with pytest.raises(J.JobError):
        J.resolve(gw["cfg"], worktree=str(gw["repo"]), base="nope", cmds=["true"])


def test_disabled_exits_2(gw, monkeypatch):
    gw["cfg"]["agentic"]["gate_jobs"] = {"enabled": False}
    r = _invoke(gw["cfg"], monkeypatch, "run", "--worktree", str(gw["repo"]), *CMD)
    assert r.exit_code == 2 and "disabled" in r.output


# ---- idempotence -----------------------------------------------------------------------------

def test_same_commit_and_project_returns_the_running_job(gw, monkeypatch):
    _workers(gw["cfg"], ("box", 4))
    gw["b"]["block"] = threading.Event()
    t = J.resolve(gw["cfg"], worktree=str(gw["repo"]), cmds=["true"])
    rec, reused = J.create(gw["cfg"], t, "alice")
    assert not reused
    th = threading.Thread(target=J.execute, args=(gw["cfg"], rec["id"]), kwargs={"wait_s": 5})
    th.start()
    try:
        assert waits.until(lambda: J.read(gw["cfg"], rec["id"])["status"] == "running")
        again, reused = J.create(gw["cfg"], J.resolve(gw["cfg"], worktree=str(gw["repo"]),
                                                      cmds=["true"]), "bob")
        assert reused and again["id"] == rec["id"]
        other, reused = J.create(gw["cfg"], J.resolve(gw["cfg"], worktree=str(gw["repo"]),
                                                      cmds=["false"]), "bob")
        assert not reused and other["id"] != rec["id"]
    finally:
        gw["b"]["block"].set()
        waits.join(th)
    assert J.read(gw["cfg"], rec["id"])["status"] == "passed"
    fresh, reused = J.create(gw["cfg"], t, "alice")
    assert not reused and fresh["id"] not in (rec["id"], other["id"])


# ---- fair sharing ----------------------------------------------------------------------------

def test_settings_defaults_and_bad_values():
    s = J.settings({})
    assert s == {"enabled": True, "max_per_requester": 2, "max_slots": None,
                 "retention_days": 7}
    bad = {"agentic": {"gate_jobs": {"enabled": "yes", "max_per_requester": 0,
                                     "max_slots": -1, "retention_days": "x"}}}
    assert J.settings(bad) == s


def test_max_slots_defaults_to_half_the_worker_slots(gw):
    _workers(gw["cfg"], ("box", 6), ("b2", 3))
    assert J.max_slots(gw["cfg"]) == 4
    gw["cfg"]["agentic"]["gate_jobs"] = {"max_slots": 1}
    assert J.max_slots(gw["cfg"]) == 1


def test_per_requester_cap_holds_a_third_job(gw, monkeypatch):
    _workers(gw["cfg"], ("box", 8))
    gw["cfg"]["agentic"]["gate_jobs"] = {"max_per_requester": 1}
    gw["b"]["block"] = threading.Event()
    t1 = J.resolve(gw["cfg"], worktree=str(gw["repo"]), cmds=["true"])
    t2 = J.resolve(gw["cfg"], worktree=str(gw["repo"]), cmds=["echo 2"])
    a, _ = J.create(gw["cfg"], t1, "alice")
    b, _ = J.create(gw["cfg"], t2, "alice")
    th = threading.Thread(target=J.execute, args=(gw["cfg"], a["id"]), kwargs={"wait_s": 10})
    th.start()
    try:
        assert waits.until(lambda: J.read(gw["cfg"], a["id"])["status"] == "running")
        out = J.execute(gw["cfg"], b["id"], wait_s=0.3, poll_s=0.05)
        assert out["status"] == "unavailable" and "token" in out["error"]
    finally:
        gw["b"]["block"].set()
        waits.join(th)


def test_campaign_waiter_wins_slot_contention(gw):
    cfg = gw["cfg"]
    _workers(cfg, ("box", 1))
    held = GS.acquire(cfg, "C-1", pools=[("box", 1)])
    held.__enter__()
    t = J.resolve(cfg, worktree=str(gw["repo"]), cmds=["true"])
    rec, _ = J.create(cfg, t, "alice")
    got = {}
    camp_stop = threading.Event()

    def campaign():
        with GS.acquire(cfg, "C-2", pools=[("box", 1)], poll_s=0.05, stop=camp_stop) as h:
            got["campaign"] = h.pool
            waits.until(lambda: J.read(cfg, rec["id"])["status"] == "waiting", timeout=5)
            got["job_status_while_campaign_holds"] = J.read(cfg, rec["id"])["status"]

    job = threading.Thread(target=lambda: got.setdefault(
        "job", J.execute(cfg, rec["id"], wait_s=20, poll_s=0.05)))
    job.start()
    assert waits.until(lambda: GS.waiting(cfg, rec["id"]) is not None)
    camp = threading.Thread(target=campaign)
    camp.start()
    assert waits.until(lambda: GS.waiting(cfg, "C-2") is not None)
    assert waits.until(lambda: any(w.get("job") == rec["id"] and w.get("by") == "alice"
                                   for w in GS.occupancy(cfg)["waiting"]))
    held.__exit__(None, None, None)
    waits.join(camp)
    waits.join(job)
    assert got["campaign"] == "box"
    assert got["job_status_while_campaign_holds"] == "waiting"
    assert got["job"]["status"] == "passed"


# ---- status, logs, cancel, gc, views ---------------------------------------------------------

def test_status_logs_and_list(gw, monkeypatch):
    _workers(gw["cfg"], ("box", 4))
    assert _invoke(gw["cfg"], monkeypatch, "run", "--worktree", str(gw["repo"]), *CMD).exit_code == 0
    r = _invoke(gw["cfg"], monkeypatch, "status", "GATEJOB-0001", "--json")
    assert r.exit_code == 0 and json.loads(r.output)["status"] == "passed"
    r = _invoke(gw["cfg"], monkeypatch, "logs", "GATEJOB-0001", "cmd-1")
    assert "log of cmd-1" in r.output
    r = _invoke(gw["cfg"], monkeypatch, "list", "--json")
    assert [j["id"] for j in json.loads(r.output)["jobs"]] == ["GATEJOB-0001"]
    r = CliRunner().invoke(cli, ["gate", "status", "nope"])
    assert r.exit_code == 2 and "GATEJOB-NNNN" in r.output


def test_cancel_stops_a_running_job(gw, monkeypatch):
    _workers(gw["cfg"], ("box", 4))
    gw["b"]["block"] = threading.Event()
    t = J.resolve(gw["cfg"], worktree=str(gw["repo"]), cmds=["true"])
    rec, _ = J.create(gw["cfg"], t, "alice")
    stop = threading.Event()
    out = {}
    th = threading.Thread(target=lambda: out.setdefault(
        "rec", J.execute(gw["cfg"], rec["id"], stop=stop, wait_s=5)))
    th.start()
    assert waits.until(lambda: J.read(gw["cfg"], rec["id"])["status"] == "running")
    stop.set()                                  # what `nw gate cancel`'s SIGTERM handler does
    gw["b"]["block"].set()
    waits.join(th)
    assert out["rec"]["status"] == "cancelled" and out["rec"]["exit"] == 130
    r = _invoke(gw["cfg"], monkeypatch, "cancel", rec["id"])
    assert r.exit_code == 0 and "already ended" in r.output


def test_dead_process_reads_lost_and_gc_drops_old_final_jobs(gw):
    t = J.resolve(gw["cfg"], worktree=str(gw["repo"]), cmds=["true"])
    rec, _ = J.create(gw["cfg"], t, "alice", pid=2 ** 22 + 12345)
    assert J.status_of(J.read(gw["cfg"], rec["id"])) == "lost"
    again, reused = J.create(gw["cfg"], t, "alice")
    assert not reused
    J.update(gw["cfg"], again["id"], status="passed", finished_at="2026-01-01T00:00:00+00:00")
    import time
    assert set(J.gc(gw["cfg"], now=time.time() + 8 * 86400)) == {rec["id"], again["id"]}
    assert J.list_jobs(gw["cfg"]) == []


def test_dashboard_and_gate_slots_show_the_job(gw, monkeypatch):
    cfg = gw["cfg"]
    _workers(cfg, ("box", 2))
    holder = {"job": "GATEJOB-0007", "by": "alice", "project": "demo", "branch": "feat"}
    with GS.acquire(cfg, "GATEJOB-0007", pools=[("box", 2)], holder=holder):
        occ = GS.occupancy(cfg)
        view = Q._gate_pools_view(cfg, occ, [])
        assert view["gate_jobs"] == [{**holder, "where": "box"}]
        line = _gate_jobs_line({"gate_pools": view})
        assert "GATEJOB-0007 by alice (demo@feat)" in line.plain
        monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
        r = CliRunner().invoke(cli, ["queue", "gate-slots"])
        assert r.exit_code == 0 and "GATEJOB-0007" in r.output and "alice" in r.output


def test_gate_job_event_is_registered_and_held():
    from nightwright.agentic import quiet
    assert E.EVENT_TYPES["gate_job"] == ("job_id", "status")
    assert quiet.RULES["gate_job"] == "hold"


def test_detach_spawns_the_exec_child_and_records_its_pid(gw, monkeypatch):
    from nightwright.agentic import cli_gate
    _workers(gw["cfg"], ("box", 4))
    spawned = []

    real = subprocess.Popen

    def fake_popen(argv, **kw):
        if "_exec" not in argv:                 # git and friends still run for real
            return real(argv, **kw)
        spawned.append((argv, kw))
        return type("P", (), {"pid": os.getpid()})()   # alive: `queued`, not `lost`

    monkeypatch.setattr(cli_gate.subprocess, "Popen", fake_popen)
    r = _invoke(gw["cfg"], monkeypatch, "run", "--worktree", str(gw["repo"]), *CMD,
                "--detach", "--wait-s", "7", "--json")
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["status"] == "queued" and gw["calls"] == []
    argv, kw = spawned[0]
    assert argv[-4:] == ["_exec", out["id"], "--wait-s", "7.0"] and kw["start_new_session"]
    r = _invoke(gw["cfg"], monkeypatch, "run", "--worktree", str(gw["repo"]), *CMD,
                "--detach", "--json")
    assert json.loads(r.output)["id"] == out["id"] and json.loads(r.output)["reused"]
    assert len(spawned) == 1


def test_local_overflow_never_offers_the_local_pool(gw):
    _workers(gw["cfg"], ("box", 2))
    gw["cfg"]["agentic"]["gates"]["local_overflow"] = True
    assert WK.gate_pools(gw["cfg"])[-1][0] is WK.LOCAL
    assert J.worker_pools(gw["cfg"]) == [("box", 2)]


def test_live_campaign_marker_blocks_a_free_slot_until_wait_s(gw):
    cfg = gw["cfg"]
    _workers(cfg, ("box", 2))
    marker = GS.slots_dir(cfg) / "waiting-C-9.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"campaign": "C-9", "pid": os.getpid(), "since": "x"}))
    rec, _ = J.create(cfg, J.resolve(cfg, worktree=str(gw["repo"]), cmds=["true"]), "alice")
    out = J.execute(cfg, rec["id"], wait_s=0.3, poll_s=0.05)
    assert out["status"] == "unavailable" and out["exit"] == 3 and gw["calls"] == []
    marker.unlink()
    rec2, _ = J.create(cfg, J.resolve(cfg, worktree=str(gw["repo"]), cmds=["true"]), "alice")
    assert J.execute(cfg, rec2["id"], wait_s=5, poll_s=0.05)["status"] == "passed"


def test_max_slots_cap_holds_a_job_over_it(gw):
    cfg = gw["cfg"]
    _workers(cfg, ("box", 8))
    cfg["agentic"]["gate_jobs"] = {"max_slots": 1}
    fd = J._take_token(J.share_tokens(cfg), 1)  # another job holds the only share token
    try:
        rec, _ = J.create(cfg, J.resolve(cfg, worktree=str(gw["repo"]), cmds=["true"]), "bob")
        out = J.execute(cfg, rec["id"], wait_s=0.3, poll_s=0.05)
        assert out["status"] == "unavailable" and "token" in out["error"]
    finally:
        os.close(fd)


def test_cancel_while_waiting_for_a_slot(gw):
    cfg = gw["cfg"]
    _workers(cfg, ("box", 1))
    rec, _ = J.create(cfg, J.resolve(cfg, worktree=str(gw["repo"]), cmds=["true"]), "alice")
    stop, out = threading.Event(), {}
    with GS.acquire(cfg, "C-1", pools=[("box", 1)]):
        th = threading.Thread(target=lambda: out.setdefault(
            "rec", J.execute(cfg, rec["id"], stop=stop, wait_s=30, poll_s=0.05)))
        th.start()
        assert waits.until(lambda: GS.waiting(cfg, rec["id"]) is not None)
        stop.set()
        waits.join(th)
    assert out["rec"]["status"] == "cancelled" and gw["calls"] == []


def test_detached_child_waits_and_cancel_sigterms_it(gw, monkeypatch):
    """A real `--detach` child (`python -m nightwright gate _exec`) on a tmp config: it waits on
    a held worker slot, and `nw gate cancel` ends it as `cancelled`. It never dispatches."""
    from nightwright.config import load_config
    tmp = gw["tmp"]
    monkeypatch.setenv("NW_CONFIG_DIR", str(tmp / "cfg"))
    monkeypatch.setenv("NW_DATA_DIR", str(tmp / "data"))
    monkeypatch.setenv("NW_CACHE_DIR", str(tmp / "cache"))
    cfg = load_config()
    _workers(cfg, ("box", 1))
    monkeypatch.chdir(gw["repo"])
    with GS.acquire(cfg, "C-1", pools=[("box", 1)]):
        r = CliRunner().invoke(cli, ["gate", "run", *CMD, "--detach", "--json"],
                               catch_exceptions=False)
        assert r.exit_code == 0, r.output
        job = json.loads(r.output)["id"]
        assert waits.until(lambda: GS.waiting(cfg, job) is not None)
        r = CliRunner().invoke(cli, ["gate", "cancel", job])
        assert r.exit_code == 0 and "cancel sent" in r.output
        assert waits.until(lambda: J.read(cfg, job)["status"] == "cancelled")
    assert J.read(cfg, job)["exit"] == 130


def test_gate_jobs_line_survives_a_long_campaign_marker():
    """SPEC-0384: a campaign marker that truncates the workers line must not hide a job."""
    import io
    from rich.console import Console
    from nightwright.agentic import cli_queue as CQ
    gp = {"local": {"slots": 2, "held": 1},
          "workers": [{"name": "box", "enabled": True, "cooldown": False, "draining": False,
                       "slots": 6, "held": 2, "campaigns": []}],
          "campaigns": [{"campaign": "SPEC-0001-" + "x" * 80, "where": "box"}],
          "gate_jobs": [{"job": "GATEJOB-0002", "by": "bruno", "project": "jerome",
                         "branch": "main", "where": "box"}]}
    p = {"gate_pools": gp}
    c = Console(width=80, file=io.StringIO(), force_terminal=False, color_system=None)
    c.print(CQ._workers_line(p))
    assert "GATEJOB-0002" not in c.file.getvalue()
    c.print(CQ._gate_jobs_line(p))
    out = c.file.getvalue()
    assert "GATEJOB-0002 by bruno (jerome@main)" in out
    gp["gate_jobs"] = []
    assert CQ._gate_jobs_line(p) is None
