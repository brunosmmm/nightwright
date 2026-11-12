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
from nightwright.agentic import gate_resources as GR
from nightwright.agentic import gate_slots as GS
from nightwright.agentic import gates as G
from nightwright.agentic import project as P
from nightwright.agentic import queue as Q
from nightwright.agentic import remote_gates as RG
from nightwright.agentic import workers as WK
from nightwright.agentic.cli_queue import _gate_jobs_line, _workers_line
from nightwright.cli import cli

pytestmark = pytest.mark.fast


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
                      "state": state, "keep_changed": keep_changed, "gates_dir": gates_dir,
                      "pcfg_gates": pcfg["gates"]})
        if behaviour["block"] is not None:
            behaviour["block"].wait(30)
            if stop is not None and stop.is_set():
                return G.GateRun(seq, False, (), "", 0.0, interrupted=True)
        if behaviour["raise"]:
            raise RG.RemoteFailure(behaviour.get("stage") or "ssh", behaviour["raise"])
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


def test_slurm_pending_past_max_pending_exits_3_not_4(gw, monkeypatch):
    """SPEC-0513 (GATEJOB-1671): a job still queued in Slurm past `max_pending_s` is a busy box
    (`unavailable`, exit 3), not a transport failure."""
    _workers(gw["cfg"], ("box", 4))
    gw["b"]["stage"] = "slurm_pending"
    gw["b"]["raise"] = "the job was still pending in Slurm (Resources) after 3s"
    r = _invoke(gw["cfg"], monkeypatch, "run", "--worktree", str(gw["repo"]), *CMD, "--json")
    assert r.exit_code == 3
    out = json.loads(r.output)
    assert out["status"] == "unavailable"
    assert out["error"].startswith("box busy on box: the job was still pending in Slurm")


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
                 "retention_days": 7, "wait_s": 1800.0,
                 "adhoc": {"max_cpus": 8, "max_mem": "16G", "licenses": []}}   # SPEC-0571
    bad = {"agentic": {"gate_jobs": {"enabled": "yes", "max_per_requester": 0,
                                     "max_slots": -1, "retention_days": "x", "wait_s": True}}}
    assert J.settings(bad) == s
    for w in ("x", 0, -5, float("inf"), float("nan")):
        assert J.settings({"agentic": {"gate_jobs": {"wait_s": w}}})["wait_s"] == 1800.0
    assert J.settings({"agentic": {"gate_jobs": {"wait_s": 60}}})["wait_s"] == 60


def test_max_slots_full_box_without_campaigns_half_with_one(gw):
    """SPEC-0500: every placeable slot while no campaign holds or waits; half while one does."""
    cfg = gw["cfg"]
    _workers(cfg, ("box", 6), ("b2", 3))
    assert J.max_slots(cfg) == 9 and not J.campaign_active(cfg)
    marker = GS.slots_dir(cfg) / "waiting-C-9.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"campaign": "C-9", "pid": os.getpid(), "since": "x"}))
    assert J.max_slots(cfg) == 4
    marker.write_text(json.dumps({"campaign": "C-9", "pid": 2 ** 22 + 12345, "since": "x"}))
    assert J.max_slots(cfg) == 9                # a dead campaign waiter does not count
    marker.unlink()
    with GS.acquire(cfg, "GATEJOB-0042", pools=[("box", 6)]):
        assert J.max_slots(cfg) == 9            # a standalone job's hold is not a campaign
    with GS.acquire(cfg, "C-1", pools=[("box", 6)]):
        assert J.max_slots(cfg) == 4
    with GS.acquire(cfg, "SENTINEL-Meta-Tools"):  # the local pool (the sentinel, a campaign)
        assert J.max_slots(cfg) == 4
    assert J.max_slots(cfg) == 9
    cfg["agentic"]["gate_jobs"] = {"max_slots": 1}
    assert J.max_slots(cfg) == 1


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


# ---- SPEC-0491: nw gate cadence status ----------------------------------------------------

def _cadence_project(gw, monkeypatch):
    (gw["repo"] / ".nw").mkdir()
    (gw["repo"] / ".nw" / "project.yaml").write_text(
        "schema: nw.project.v1\nproject: demo\nbase: main\n"
        "cadence:\n  - {name: full, cmd: 'true', every_n_merges: 3}\n")
    monkeypatch.setattr(P, "project_config", lambda cfg, name: {
        **P.load_project_config(gw["repo"] / ".nw" / "project.yaml"), "_root": str(gw["repo"])})


def test_cadence_status_unseeded_and_seeded(gw, monkeypatch):
    from nightwright.agentic import cadence as CAD
    _cadence_project(gw, monkeypatch)
    r = _invoke(gw["cfg"], monkeypatch, "cadence", "status", "--project", "demo", "--json")
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["schema"] == "nw.cadence.status.v1" and out["project"] == "demo"
    job = out["jobs"][0]
    assert job["next"] == "not seeded" and job["running"] == "no" and job["merges_since"] == 0
    assert list(job) == list(CAD.STATUS_KEYS) and job["due"] is None
    assert not CAD.state_path(gw["cfg"], "demo").exists()
    pc = P.load_project_config(gw["repo"] / ".nw" / "project.yaml")
    CAD.record_merge(gw["cfg"], "demo", "s1", [], project_cfg=pc)
    r = _invoke(gw["cfg"], monkeypatch, "cadence", "status", "--project", "demo")
    assert r.exit_code == 0 and 'name=full' in r.output and 'merges_since=1' in r.output
    assert 'next="in 2 merges"' in r.output


def test_cadence_status_unloadable_project_exits_2(gw, monkeypatch):
    r = _invoke(gw["cfg"], monkeypatch, "cadence", "status", "--project", "nope")
    assert r.exit_code == 2
    r = _invoke(gw["cfg"], monkeypatch, "cadence", "status")
    assert r.exit_code == 2


# ---- SPEC-0500: the line, the token count, requester, wait_s, messages ------------------------

def _job(cfg, repo, by, n, pid=None):
    t = J.resolve(cfg, worktree=str(repo), cmds=[f"echo {n}"])
    return J.create(cfg, t, by, pid=pid)[0]


def _run(cfg, rec, out, **kw):
    th = threading.Thread(target=lambda: out.setdefault(
        rec["id"], J.execute(cfg, rec["id"], **{"wait_s": 20, "poll_s": 0.02, **kw})))
    th.start()
    return th


def test_share_line_is_first_come_first_served(gw):
    """Waiters started newest first still get the share token oldest first."""
    cfg = gw["cfg"]
    _workers(cfg, ("box", 4))
    cfg["agentic"]["gate_jobs"] = {"max_slots": 1}
    fd = J._take_token(J.share_tokens(cfg), 1)
    a, b, c = (_job(cfg, gw["repo"], by, n) for n, by in enumerate(("alice", "bob", "carol")))
    out, threads, started = {}, [], []
    try:
        for rec in (c, b, a):                   # reverse order: each polls before the older one
            threads.append(_run(cfg, rec, out))
            started.insert(0, rec["id"])
            assert waits.until(lambda: J.line(cfg, "share") == started)
        assert J.line(cfg, "share") == [a["id"], b["id"], c["id"]]
    finally:
        os.close(fd)                            # frees the only share token
    for th in threads:
        waits.join(th)
    assert [x["state"]["id"] for x in gw["calls"]] == [a["id"], b["id"], c["id"]]
    assert all(out[r["id"]]["status"] == "passed" for r in (a, b, c))
    assert J.line(cfg, "share") == [] and not list((J.root(cfg) / "line").glob("*.json"))


def test_dead_or_final_waiter_never_blocks_the_line(gw):
    cfg = gw["cfg"]
    _workers(cfg, ("box", 4))
    cfg["agentic"]["gate_jobs"] = {"max_slots": 1}
    dead = _job(cfg, gw["repo"], "alice", 1, pid=2 ** 22 + 12345)
    J.update(cfg, dead["id"], status="waiting")
    ended = _job(cfg, gw["repo"], "bob", 2)
    J.update(cfg, ended["id"], status="failed")
    line = J.root(cfg) / "line"
    line.mkdir(parents=True, exist_ok=True)
    for r in (dead, ended):
        (line / f"{r['id']}.json").write_text(json.dumps(
            {"id": r["id"], "pool": "share", "created_epoch": r["created_epoch"]}))
    young = _job(cfg, gw["repo"], "carol", 3)
    assert J.execute(cfg, young["id"], wait_s=5, poll_s=0.02)["status"] == "passed"
    assert not list(line.glob("*.json"))


def test_requester_cap_does_not_block_other_requesters(gw):
    cfg = gw["cfg"]
    _workers(cfg, ("box", 4))
    cfg["agentic"]["gate_jobs"] = {"max_per_requester": 1, "max_slots": 1}
    alice_fd = J._take_token(J.requester_tokens(cfg, "alice"), 1)
    old = _job(cfg, gw["repo"], "alice", 1)
    out = {}
    th = _run(cfg, old, out)
    try:
        pool = J.requester_tokens(cfg, "alice").name
        assert waits.until(lambda: J.line(cfg, pool) == [old["id"]])
        late = _job(cfg, gw["repo"], "bob", 2)
        assert J.execute(cfg, late["id"], wait_s=5, poll_s=0.02)["status"] == "passed"
        third = _job(cfg, gw["repo"], "alice", 3)
        msg = J.execute(cfg, third["id"], wait_s=0.3, poll_s=0.02)["error"]
        assert msg == ("box unavailable: no requester token within 0.3s: 1 job ahead in line "
                       "(cap 1 per requester alice)")
    finally:
        os.close(alice_fd)
    waits.join(th)
    assert out[old["id"]]["status"] == "passed"


def test_take_token_counts_holders_at_any_index(tmp_path):
    d = tmp_path / "share"
    fds = [J._take_token(d, 3) for _ in range(3)]
    assert None not in fds and J._take_token(d, 3) is None
    os.close(fds[0])                            # slot-0 is free; slot-1 and slot-2 still held
    assert J._take_token(d, 2) is None          # the cap fell to 2: two in flight already
    again = J._take_token(d, 3)
    assert again is not None and J._take_token(d, 3) is None
    for fd in (again, *fds[1:]):
        os.close(fd)


def test_requester_defaults_to_user_at_project(gw, monkeypatch):
    monkeypatch.setenv("USER", "bruno")
    assert J.requester(None, "P") == "bruno@P"
    assert J.requester(None, None) == "bruno"
    assert J.requester("agent:x", "P") == "agent:x"
    monkeypatch.setenv("NW_MAILBOX_ADDR", "steward:P")
    assert J.requester(None, "P") == "steward:P"
    monkeypatch.delenv("NW_MAILBOX_ADDR")
    _workers(gw["cfg"], ("box", 4))
    pcfg = {"_root": str(gw["repo"]), "merge": {"base": "main"},
            "gates": [{"name": "unit", "cmd": "pytest", "when": "loop"}]}
    monkeypatch.setattr(P, "project_config", lambda c, p: pcfg)
    r = _invoke(gw["cfg"], monkeypatch, "run", "--project", "demo", "--worktree",
                str(gw["repo"]), "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["requester"] == "bruno@demo"


def test_wait_s_setting_is_the_cli_default(gw, monkeypatch):
    from nightwright.agentic import cli_gate
    _workers(gw["cfg"], ("box", 4))
    spawned = []
    real = subprocess.Popen

    def fake_popen(argv, **kw):
        if "_exec" not in argv:
            return real(argv, **kw)
        spawned.append(argv)
        return type("P", (), {"pid": os.getpid()})()

    monkeypatch.setattr(cli_gate.subprocess, "Popen", fake_popen)
    r = _invoke(gw["cfg"], monkeypatch, "run", "--worktree", str(gw["repo"]),
                "--cmd", "echo 1", "--detach", "--json")
    assert r.exit_code == 0, r.output
    assert spawned[-1][-2:] == ["--wait-s", "1800.0"]
    gw["cfg"]["agentic"]["gate_jobs"] = {"wait_s": 60}
    r = _invoke(gw["cfg"], monkeypatch, "run", "--worktree", str(gw["repo"]),
                "--cmd", "echo 2", "--detach", "--json")
    assert spawned[-1][-2:] == ["--wait-s", "60.0"]
    r = _invoke(gw["cfg"], monkeypatch, "run", "--worktree", str(gw["repo"]),
                "--cmd", "echo 3", "--detach", "--wait-s", "5", "--json")
    assert spawned[-1][-2:] == ["--wait-s", "5.0"]
    assert J.DEFAULT_WAIT_S == 1800.0


def test_unavailable_message_names_line_and_cap(gw):
    cfg = gw["cfg"]
    _workers(cfg, ("box", 4))
    cfg["agentic"]["gate_jobs"] = {"max_slots": 1}
    fd = J._take_token(J.share_tokens(cfg), 1)
    first = _job(cfg, gw["repo"], "alice", 1)
    stop, out = threading.Event(), {}
    th = _run(cfg, first, out, stop=stop)
    try:
        assert waits.until(lambda: J.line(cfg, "share") == [first["id"]])
        second = _job(cfg, gw["repo"], "bob", 2)
        rec = J.execute(cfg, second["id"], wait_s=0.3, poll_s=0.02)
        assert rec["status"] == "unavailable" and rec["exit"] == 3
        assert rec["error"] == ("box unavailable: no share token within 0.3s: 1 job ahead in "
                                "line (cap 1 standalone jobs box-wide)")
        assert "no a " not in rec["error"]
    finally:
        stop.set()
        waits.join(th)
        os.close(fd)
    assert out[first["id"]]["status"] == "cancelled"
    assert J.line(cfg, "share") == []


# ---- SPEC-0571: --cpus / --mem / --licenses on --cmd gates, capped by agentic.gate_jobs.adhoc --

def _adhoc_run(gw, monkeypatch, *flags, cmd=CMD):
    return _invoke(gw["cfg"], monkeypatch, "run", "--worktree", str(gw["repo"]), *cmd, *flags,
                   "--json")


def _no_cut(gw, monkeypatch):
    """Make a commit cut or a worker dispatch fail the test: a refusal must precede both."""
    def boom(*a, **k):
        raise AssertionError("refusal came after the commit was cut / worker contacted")
    monkeypatch.setattr(RG, "commit_under_test", boom)
    monkeypatch.setattr(RG, "run_detached", boom)


def _no_job_dir(gw):
    assert not list((J.root(gw["cfg"])).glob("GATEJOB-*")) and gw["calls"] == []


def test_flags_become_the_gate_resources_and_the_request(gw):
    t = J.resolve(gw["cfg"], worktree=str(gw["repo"]), cmds=("a", "b"), cpus=8, mem="12G",
                  licenses=())
    assert [g["resources"] for g in t["gates"]] == [{"cpus": 8, "mem": "12G"}] * 2
    gw["cfg"]["agentic"]["gate_jobs"] = {"adhoc": {"licenses": ["lic"]}}
    t = J.resolve(gw["cfg"], worktree=str(gw["repo"]), cmds=("a",), cpus=8, mem="12G",
                  licenses=("lic:2",))
    g = t["gates"][0]
    assert g["resources"] == {"cpus": 8, "mem": "12G", "licenses": ["lic:2"]}
    req = GR.job_request(t["gates"], gw["cfg"], time_s=60)
    assert (req["cpus"], req["mem_mb"], req["licenses"]) == (8, 12288, {"lic": 2})


def test_no_flags_leave_resources_off_and_the_default_request(gw):
    t = J.resolve(gw["cfg"], worktree=str(gw["repo"]), cmds=("a",))
    assert "resources" not in t["gates"][0]
    req = GR.job_request(t["gates"], gw["cfg"], time_s=60)
    assert (req["cpus"], req["mem_mb"]) == (3, 3072)


def test_partial_flags_keep_the_other_defaults(gw):
    t = J.resolve(gw["cfg"], worktree=str(gw["repo"]), cmds=("a",), cpus=8)
    req = GR.job_request(t["gates"], gw["cfg"], time_s=60)
    assert (req["cpus"], req["mem_mb"]) == (8, 3072)
    t = J.resolve(gw["cfg"], worktree=str(gw["repo"]), cmds=("a",), mem="8G")
    req = GR.job_request(t["gates"], gw["cfg"], time_s=60)
    assert (req["cpus"], req["mem_mb"]) == (3, 8192)


def test_a_flagged_run_reaches_the_worker_with_a_valid_payload(gw, monkeypatch):
    _workers(gw["cfg"], ("box", 4))
    r = _adhoc_run(gw, monkeypatch, "--cpus", "8", "--mem", "12G")
    assert r.exit_code == 0, r.output
    rec = J.read(gw["cfg"], "GATEJOB-0001")
    assert rec["gate_cfg"][0]["resources"] == {"cpus": 8, "mem": "12G"}
    req = GR.job_request(rec["gate_cfg"], gw["cfg"], time_s=60)
    assert GR.check_payload(req) is None and (req["cpus"], req["mem_mb"]) == (8, 12288)
    call = gw["calls"][0]
    assert call["state"]["id"] == "GATEJOB-0001"
    sent = call["pcfg_gates"]                    # the gates the dispatch actually received
    assert sent and all(g["resources"] == {"cpus": 8, "mem": "12G"} for g in sent)
    sent_req = GR.job_request(sent, gw["cfg"], time_s=60)
    assert GR.check_payload(sent_req) is None
    assert (sent_req["cpus"], sent_req["mem_mb"]) == (8, 12288)


@pytest.mark.parametrize("flags", [("--cpus", "2"), ("--mem", "1G"), ("--licenses", "lic")])
def test_flags_without_cmd_exit_2_with_a_usage_message(gw, monkeypatch, flags):
    _workers(gw["cfg"], ("box", 4))
    _no_cut(gw, monkeypatch)
    r = _invoke(gw["cfg"], monkeypatch, "run", "--worktree", str(gw["repo"]), *flags)
    assert r.exit_code == 2 and "--cmd" in r.output
    _no_job_dir(gw)


@pytest.mark.parametrize("flags, needle", [
    (("--cpus", "0"), "cpus: must be an integer >= 1"),
    (("--mem", "lots"), "mem: must be a size"),
    (("--mem", "8"), "mem: must be a size"),
    (("--licenses", "Bad Name"), "licenses: must be a list"),
])
def test_invalid_flag_values_exit_2_before_any_cut(gw, monkeypatch, flags, needle):
    _workers(gw["cfg"], ("box", 4))
    _no_cut(gw, monkeypatch)
    r = _adhoc_run(gw, monkeypatch, *flags)
    assert r.exit_code == 2 and needle in r.output, r.output
    _no_job_dir(gw)


@pytest.mark.parametrize("flags, needle", [
    (("--cpus", "9"), ("--cpus 9", "cap of 8", "agentic.gate_jobs.adhoc.max_cpus")),
    (("--mem", "17G"), ("--mem 17G", "cap of 16G", "agentic.gate_jobs.adhoc.max_mem")),
    (("--mem", "16385M"), ("--mem 16385M", "cap of 16G", "agentic.gate_jobs.adhoc.max_mem")),
    (("--licenses", "jerome_target"), ("--licenses jerome_target", "jerome_target",
                                       "allowed: none", "agentic.gate_jobs.adhoc.licenses")),
    (("--licenses", "jerome_target:2"), ("--licenses jerome_target:2", "allowed: none",
                                         "agentic.gate_jobs.adhoc.licenses")),
])
def test_default_caps_refuse_before_any_cut(gw, monkeypatch, flags, needle):
    _workers(gw["cfg"], ("box", 4))
    _no_cut(gw, monkeypatch)
    r = _adhoc_run(gw, monkeypatch, *flags)
    assert r.exit_code == 2, r.output
    assert all(n in r.output for n in needle), r.output
    _no_job_dir(gw)


def test_values_at_the_default_caps_are_accepted(gw, monkeypatch):
    _workers(gw["cfg"], ("box", 4))
    r = _adhoc_run(gw, monkeypatch, "--cpus", "8", "--mem", "16G")
    assert r.exit_code == 0, r.output


@pytest.mark.parametrize("mem", ["512M", "16384M"])   # compared in MB, not as strings
def test_the_mem_cap_compares_across_units(gw, monkeypatch, mem):
    _workers(gw["cfg"], ("box", 4))
    assert _adhoc_run(gw, monkeypatch, "--mem", mem).exit_code == 0


def test_one_disallowed_license_among_several_refuses_the_run(gw, monkeypatch):
    _workers(gw["cfg"], ("box", 4))
    gw["cfg"]["agentic"]["gate_jobs"] = {"adhoc": {"licenses": ["lic"]}}
    ok = _adhoc_run(gw, monkeypatch, "--licenses", "lic:3", "--licenses", "lic")   # count over bare name
    assert ok.exit_code == 0, ok.output
    _no_cut(gw, monkeypatch)
    n = len(list(J.root(gw["cfg"]).glob("GATEJOB-*")))
    r = _adhoc_run(gw, monkeypatch, "--licenses", "lic", "--licenses", "other:1")
    assert r.exit_code == 2 and "'other'" in r.output and "allowed: lic" in r.output, r.output
    assert len(list(J.root(gw["cfg"]).glob("GATEJOB-*"))) == n


def test_configured_caps_raise_the_limits(gw, monkeypatch):
    _workers(gw["cfg"], ("box", 4))
    gw["cfg"]["agentic"]["gate_jobs"] = {"adhoc": {"max_cpus": 16, "max_mem": "64G",
                                                   "licenses": ["jerome_target"]}}
    r = _adhoc_run(gw, monkeypatch, "--cpus", "16", "--mem", "64G", "--licenses",
                   "jerome_target:2")
    assert r.exit_code == 0, r.output


@pytest.mark.parametrize("bad", [
    {"max_cpus": 0}, {"max_cpus": "9"}, {"max_cpus": True}, {"max_mem": "lots"}, {"max_mem": 16},
    {"licenses": "jerome_target"}, {"licenses": ["Bad Name"]}, {"licenses": ["lic:2"]}, "x"])
def test_a_malformed_cap_falls_back_to_the_default_per_key(gw, bad):
    gw["cfg"]["agentic"]["gate_jobs"] = {"adhoc": bad}
    assert J.settings(gw["cfg"])["adhoc"] == J.ADHOC_DEFAULTS
    gw["cfg"]["agentic"]["gate_jobs"] = {"adhoc": {**(bad if isinstance(bad, dict) else {}),
                                                   "max_cpus": 4}}
    a = J.settings(gw["cfg"])["adhoc"]
    assert a["max_cpus"] == 4 and a["max_mem"] == "16G" and a["licenses"] == []


def test_a_cap_below_the_default_request_only_checks_the_flags_set(gw, monkeypatch):
    _workers(gw["cfg"], ("box", 4))
    gw["cfg"]["agentic"]["gate_jobs"] = {"adhoc": {"max_cpus": 2}}
    assert _adhoc_run(gw, monkeypatch).exit_code == 0               # no flag: 3 cpus not refused
    assert _adhoc_run(gw, monkeypatch, "--mem", "8G").exit_code == 0  # cpus stays the default 3
    flagless, mem_only = (GR.job_request(c["pcfg_gates"], gw["cfg"], time_s=60)
                          for c in gw["calls"])
    assert (flagless["cpus"], flagless["mem_mb"]) == (3, 3072)
    assert (mem_only["cpus"], mem_only["mem_mb"]) == (3, 8192)
    _no_cut(gw, monkeypatch)
    r = _adhoc_run(gw, monkeypatch, "--cpus", "3")
    assert r.exit_code == 2, r.output
    assert all(n in r.output for n in ("--cpus 3", "cap of 2", "agentic.gate_jobs.adhoc.max_cpus"))


def test_resources_are_part_of_the_dedup_key(gw, monkeypatch):
    _workers(gw["cfg"], ("box", 4))
    gw["b"]["block"] = threading.Event()
    args = ("run", "--worktree", str(gw["repo"]), *CMD, "--detach", "--json")
    a = json.loads(_invoke(gw["cfg"], monkeypatch, *args, "--cpus", "4").output)
    b = json.loads(_invoke(gw["cfg"], monkeypatch, *args, "--cpus", "5").output)
    c = json.loads(_invoke(gw["cfg"], monkeypatch, *args, "--cpus", "4").output)
    assert a["id"] != b["id"] and c["id"] == a["id"]
    gw["b"]["block"].set()


def test_help_lists_the_resource_flags(gw, monkeypatch):
    r = _invoke(gw["cfg"], monkeypatch, "run", "--help")
    assert r.exit_code == 0
    for opt in ("--cpus", "--mem", "--licenses", "agentic.gate_jobs.adhoc"):
        assert opt in r.output


def test_docs_name_the_flags_and_the_cap_key():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in ("skills/nw-orient/SKILL.md", "docs/product/reference/index.md"):
        text = open(os.path.join(root, rel)).read()
        for needle in ("--cpus", "--mem", "--licenses", "agentic.gate_jobs.adhoc"):
            assert needle in text, (rel, needle)
