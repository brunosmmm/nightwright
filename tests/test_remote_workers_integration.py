"""SPEC-0346: the SSH-workers integration suite. Whole fake campaigns (`test_agentic_e2e`'s
repo, gates and `FakeRunner`) run through the real queue (`queue.run`) with two loopback SSH
workers (SPEC-0338's loopback script via `remote_support.Remote`, each with its own data and
config dirs) next to the coordinator's own data dir. Gate jobs run tiny shell gates on the
worker; persona jobs run `FakeRunner` there (`NW_AGENTIC_RUNNER=fake`). Failures are real: a
killed process group and a killed coordinator process, never a mocked transport. Waits use
`tests/waits.py`; the kill and restart cases synchronise on sentinel files.

SPEC-0334 adds its pull-worker cases to this file; shared loopback fixtures stay in
`remote_support`."""
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from nightwright.agentic import events as EV
from nightwright.agentic import queue as Q
from nightwright.agentic import remote_gates as RG
from nightwright.agentic import remote_jobs as RJ
from nightwright.agentic import remote_personas as RPS
from nightwright.agentic import store as S
from nightwright.agentic import worker as W
from nightwright.agentic import workers as WK
from nightwright.cli import cli

import remote_workers_golden as GOLD
from remote_support import (INFO, LOCAL_OK, VERDICT, WRITER, Remote, _remote, _req,  # noqa: F401
                            _script, _world, _write_script)
from remote_support import _short_runtime_dir  # noqa: F401  (autouse fixture)
from test_agentic_e2e import (ENV, _git, _git_env, added, impl_done, make_cfg,  # noqa: F401
                              make_repo, review, run_cli, script)
from waits import join, until

GOLDEN = Path(__file__).parent / "data" / "remote_workers" / "no_workers_golden.json"
GATES_GOLDEN = Path(__file__).parent / "data" / "remote_gates_golden.json"

# keys of the environment a worker job's process may carry from the coordinator's job header:
# the allowlist itself plus the keys the worker sets itself (`runner.ENV_RESET`, the persona
# markers) and the launch environment of the fake `ssh` (what sshd gives a forced command)
WORKER_OWN = frozenset({"NW_DATA_DIR", "NW_CONFIG_DIR", "NW_WORKTREE", "NW_PERSONA",
                        "NW_PERSONA_RUN", "NW_REBASE_TARGET", "NW_GATE_REPORT", "NW_AGENTIC_RUNNER",
                        "NW_AGENTIC_FAKE_SCRIPT"})
FORBIDDEN_ENV = ("GIT_ASKPASS", "SSH_AUTH_SOCK", "GH_TOKEN", "GITHUB_TOKEN")
POISON = {"GH_TOKEN": "poison-gh", "GITHUB_TOKEN": "poison-github", "GIT_ASKPASS": "/poison/askpass",
          "SSH_AUTH_SOCK": "/poison/agent.sock", "GIT_CONFIG_COUNT": "1",
          "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "poison"}


# ---- the fake campaign ------------------------------------------------------------------------

def _blocking_gate(c, name="lint"):
    """A gate that notes its run (one line, the gate run's `TMPDIR`, which names its seq, in `c.counter`),
    says it started, and blocks until the test releases `c.go`."""
    return (f'echo "$TMPDIR" >> {c.counter}; touch {c.started}; '
            f'while [ ! -f {c.go} ]; do sleep 0.05; done')


def _inspect(name):
    """Shell run inside the worker's checkout (a gate's or a persona's cwd): its env and what
    git finds there, written next to the checkout in the job dir (the checkout itself is gone
    when the job ends)."""
    return (f"env > ../{name}-env.txt; git config --get-regexp credential > ../{name}-cred.txt; "
            f"echo $? > ../{name}-cred.rc; git remote -v > ../{name}-remote.txt; "
            f"git config --list > ../{name}-config.txt; "
            f"GIT_TERMINAL_PROMPT=0 git push > ../{name}-push.txt 2>&1; echo $? > ../{name}-push.rc")


def build(tmp_path, monkeypatch, workers=None, *, blocking=False, lease_s=None, env_files=False):
    """A queued fake campaign. `workers`: None (no `workers.json`), "empty" (an empty registry)
    or "two" (two loopback workers, both gate- and persona-capable). `blocking`: the lint gate
    blocks on a sentinel. `env_files`: the gates and the persona write their env to a file in
    the worker's job dir."""
    c = SimpleNamespace(tmp=tmp_path, go=tmp_path / "GO", started=tmp_path / "STARTED",
                        counter=tmp_path / "counter", remotes=[])
    c.go.unlink(missing_ok=True)
    lint = "true"
    if blocking:
        lint = _blocking_gate(c)
    if env_files:
        lint = _inspect("gate") + "; " + lint
    c.repo = make_repo(tmp_path, lint_gate=lint)
    c.cfg = make_cfg(tmp_path, c.repo, script(tmp_path, [impl_done(), review()]))
    if lease_s:
        c.cfg["agentic"]["workers"] = {"job_lease_s": lease_s}
    if workers is not None:
        # a persona job's fake script is entry 0 of the worker's copy (the cursor is per job):
        # one entry whose verdict satisfies the implementer's and the reviewer's schemas
        entry = impl_done()
        entry.pop("persona")
        entry["verdict"].update(review()["verdict"])
        if env_files:
            entry["pre_shell"] = [_inspect("persona")]
        wscript = script(tmp_path, [entry], "worker-script.json")
        shim = SimpleNamespace(tmp=tmp_path, cfg=c.cfg)
        records = []
        for n in ("a", "b"):
            r = Remote(shim, n)
            # sshd hands a forced command a clean environment (HOME, PATH, ...): nothing of the
            # coordinator's reaches the worker but what the protocol sends
            ssh = _script(tmp_path / f"ssh-{n}",
                          f'exec env -i PATH="$PATH" HOME="$HOME" NW_AGENTIC_RUNNER=fake '
                          f'NW_AGENTIC_FAKE_SCRIPT={wscript} NW_WORKER_PROGRESS_S=0.2 '
                          f'{r.ssh} "$@"')
            records.append({"name": f"w{n}", "ssh": f"me@{n}", "slots": 1, "nw": "nw",
                            "ssh_argv": [ssh], "info": INFO, "kinds": ["gate", "persona"]})
            c.remotes.append(r)
        reg = WK.path(c.cfg)
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(json.dumps({"schema": WK.SCHEMA,
                                   "workers": records if workers == "two" else []}))
    c.cid = added(c.cfg, monkeypatch)
    return c


def run_queue(c, monkeypatch):
    r = run_cli(c.cfg, monkeypatch, ["queue", "run", "--until-idle", "--json-lines"])
    assert r.exit_code == 0, r.output
    return S.read_state(c.cfg, c.cid)


def finished(tmp_path, monkeypatch, workers=None, **kw):
    c = build(tmp_path, monkeypatch, workers, **kw)
    assert run_queue(c, monkeypatch)["state"] == "MERGED"
    return c


def sub(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    return d


def ledger(c, types=None):
    return S.read_ledger(c.cfg, c.cid, types=types)


def main_tree(c):
    return _git(c.repo, "rev-parse", "main^{tree}")


def worker_jobs(c):
    """`{job_id: (remote, job dir)}` over the workers' job stores."""
    out = {}
    for r in c.remotes:
        for d in (r.wdata / "agentic" / "worker" / "jobs").glob("*"):
            out[d.name] = (r, d)
    return out


def job_id_of(c, kind="gate", seq=1, attempt=1):
    return f"{c.cid}-gate-{seq:03d}-a{attempt}" if kind == "gate" else \
        RPS.job_id_for(c.cid, "001-implementer", attempt)


def counted(c, seq=1):
    """How many times the blocking gate ran for gate run `seq` (anywhere)."""
    if not c.counter.is_file():
        return 0
    return sum(f"gate-{seq:03d}" in ln for ln in c.counter.read_text().splitlines())


def start_queue(c):
    """The coordinator's queue loop in a thread (what `nw queue run` runs)."""
    out = {}

    def go():
        out["ticks"] = Q.run(c.cfg, until_idle=True, poll_interval_s=0.01)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t, out


@pytest.fixture
def camps():
    """The campaigns a test blocked: their sentinel is released at the end, so no gate is left
    running."""
    out = []
    yield out
    for c in out:
        c.go.write_text("go\n")


def tracked(camps, c):
    camps.append(c)
    return c


# ---- no workers: the pre-epic golden ----------------------------------------------------------

def test_no_workers_run_matches_the_pre_epic_golden(tmp_path, monkeypatch):
    c = finished(tmp_path, monkeypatch)
    snap = GOLD.snapshot(c.cfg, c.cid, tmp_path)
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert GOLD.comparable(snap) == GOLD.comparable(golden)
    assert not any("worker" in e for e in ledger(c))
    assert not WK.path(c.cfg).exists()
    # the gate portion has the shape of SPEC-0338's golden gate event and files
    theirs = json.loads(GATES_GOLDEN.read_text(encoding="utf-8"))
    gate_of = lambda events: next(e for e in events if e["type"] == "gate")  # noqa: E731
    mine, base = GOLD.comparable(gate_of(snap["events"])), GOLD.comparable(gate_of(theirs["events"]))
    assert set(mine) == set(base)
    assert set(mine["results"][0]) == set(base["results"][0])
    assert {n.split("-", 1)[1].split(".")[0] for n in snap["files"]} == {"lint", "test"}


def test_empty_workers_json_equals_the_absent_file(tmp_path, monkeypatch):
    a = finished(sub(tmp_path, "absent"), monkeypatch)
    b = finished(sub(tmp_path, "empty"), monkeypatch, "empty")
    assert WK.path(b.cfg).is_file() and not WK.path(a.cfg).exists()
    # (the prompt text carries the temp path, so `prompt_sha256` is among the dropped keys)
    assert GOLD.comparable(GOLD.snapshot(a.cfg, a.cid, a.tmp)) == \
        GOLD.comparable(GOLD.snapshot(b.cfg, b.cid, b.tmp))


# ---- with workers -----------------------------------------------------------------------------

def test_with_workers_same_tree_and_event_types_plus_worker_keys(tmp_path, monkeypatch):
    local = finished(sub(tmp_path, "local"), monkeypatch)
    remote = finished(sub(tmp_path, "remote"), monkeypatch, "two")
    assert main_tree(remote) == main_tree(local)
    types = lambda c: [e["type"] for e in ledger(c)]  # noqa: E731
    assert types(remote) == types(local)
    assert not any("worker" in e for e in ledger(local))
    on_worker = [e for e in ledger(remote) if e.get("worker")]
    assert [e["type"] for e in on_worker] == ["run.end", "gate", "run.end", "gate"]
    assert {e["worker"] for e in on_worker} <= {"wa", "wb"}
    # gate and persona jobs really ran on the workers (their own data dirs), once each
    jobs = worker_jobs(remote)
    assert sorted(jobs) == sorted([
        f"{remote.cid}-gate-001-a1", f"{remote.cid}-gate-002-a1",
        RPS.job_id_for(remote.cid, "001-implementer"),
        RPS.job_id_for(remote.cid, "002-conformance_reviewer")])


def test_the_merge_happens_on_the_coordinator_after_the_close_regate(tmp_path, monkeypatch):
    c = finished(tmp_path, monkeypatch, "two")
    evs = ledger(c)
    close = next(e for e in evs if e["type"] == "close")
    regate = [e for e in evs if e["type"] == "gate" and e["gate_seq"] == close["gates_seq"]]
    assert regate and evs.index(regate[0]) < evs.index(close)
    st = S.read_state(c.cfg, c.cid)
    # the head the re-gate ran on is the one the coordinator fast-forwarded main to
    assert st["close"]["merged_sha"] == _git(c.repo, "rev-parse", "main")
    assert regate[0]["head"] == st["close"]["merged_sha"]
    # no worker ever wrote a ref of the coordinator's repo, nor holds the merge: its caches
    # have no `main` branch at the merged sha
    for r in c.remotes:
        for cache in (r.wdata / "agentic" / "worker" / "repos").glob("*.git"):
            ref = subprocess.run(["git", "-C", str(cache), "rev-parse", "--verify", "-q",
                                  "refs/heads/main"], capture_output=True, text=True)
            assert ref.stdout.strip() != st["close"]["merged_sha"]


def test_export_lists_cost_per_run_and_per_worker(tmp_path, monkeypatch):
    c = finished(tmp_path, monkeypatch, "two")
    out = tmp_path / "export"
    r = CliRunner().invoke(cli, ["campaign", "export", "--out", str(out), "--json"])
    assert r.exit_code == 0, r.output
    (camp,) = [json.loads(ln) for ln in (out / "campaigns.jsonl").read_text().splitlines()]
    runs = {row["run_id"]: row for row in
            (json.loads(ln) for ln in (out / "runs.jsonl").read_text().splitlines())}
    ends = {e["run_id"]: e for e in ledger(c, ["run.end"])}
    assert set(runs) == set(ends) and len(ends) == 2
    for rid, e in ends.items():
        assert runs[rid]["cost_usd"] == e["cost_usd"] and e["worker"] in ("wa", "wb")
    by = {}
    for e in ends.values():
        by[e["worker"]] = round(by.get(e["worker"], 0.0) + e["cost_usd"], 4)
    assert camp["cost_by_worker"] == by and sum(by.values()) > 0


# ---- duplicate delivery -----------------------------------------------------------------------

def test_a_duplicate_submit_starts_one_process_and_a_duplicate_event_appends_one(
        tmp_path, monkeypatch):
    c = build(tmp_path, monkeypatch, "two")
    c.cfg["agentic"]["gates"] = {"slots": 2}
    real = RG._submit
    again = []

    def twice(worker, header, bundle, cfg):
        real(worker, header, bundle, cfg)
        again.append(header["job_id"])
        real(worker, header, bundle, cfg)           # the same job id, the same bytes
    monkeypatch.setattr(RG, "_submit", twice)
    proj = (c.repo / "gates" / "lint.sh")
    proj.write_text(f'echo "$TMPDIR" >> {c.counter}\n')
    _git(c.repo, "commit", "-qam", "count the lint gate's runs")
    assert run_queue(c, monkeypatch)["state"] == "MERGED"
    assert sorted(again) == sorted(worker_jobs(c))          # every job went to its worker twice
    assert len(again) == 4
    assert counted(c, 1) == 1 and counted(c, 2) == 1      # one worker process per job id
    gates = ledger(c, ["gate"])
    assert [g["gate_seq"] for g in gates] == [1, 2]
    # the same worker event delivered twice: `append_event` drops it by its `event_uid`
    for g in gates:
        S.append_event(c.cfg, c.cid, "gate", {k: v for k, v in g.items()
                                              if k not in ("seq", "ts", "type", "schema")})
    assert len(ledger(c, ["gate"])) == 2


# ---- a killed worker --------------------------------------------------------------------------

def test_a_killed_worker_job_is_retried_and_the_campaign_merges(tmp_path, monkeypatch, camps):
    c = tracked(camps, build(tmp_path, monkeypatch, "two", blocking=True, lease_s=3))
    t, _out = start_queue(c)
    first = job_id_of(c)
    assert until(lambda: c.started.is_file() and first in worker_jobs(c)
                 and (worker_jobs(c)[first][1] / "proc.json").is_file())
    remote, jdir = worker_jobs(c)[first]
    pid = json.loads((jdir / "proc.json").read_text())["pid"]
    os.killpg(os.getpgid(pid), signal.SIGKILL)              # the job process and its gate shell
    c.go.write_text("go\n")                                 # the retry's gate may finish
    join(t, timeout=120)
    st = S.read_state(c.cfg, c.cid)
    assert st["state"] == "MERGED"
    (lost,) = ledger(c, ["worker_job_lost"])
    assert lost["job_id"] == first and lost["attempt"] == 1
    gates = ledger(c, ["gate"])
    first_gate = next(g for g in gates if g["gate_seq"] == 1)
    # the dead attempt's frames never reach the ledger: the one gate event of seq 1 is the
    # retry's, on the other worker or locally, and nothing carries the dead job's uid
    assert len([g for g in gates if g["gate_seq"] == 1]) == 1
    assert first_gate.get("worker") != remote_name(remote, c)
    assert not any(str(e.get("event_uid", "")).startswith(first) for e in ledger(c))
    assert ledger(c, ["worker_job_retried"])
    assert _git(c.repo, "rev-parse", "main") == st["close"]["merged_sha"]


def remote_name(remote, c):
    for rec in WK.load(c.cfg):
        if str(remote.wdata).endswith("wdata" + rec["name"][1:]):
            return rec["name"]
    raise AssertionError("unknown remote")


# ---- a coordinator restart --------------------------------------------------------------------

_COORDINATOR = """import json, sys
from zoneinfo import ZoneInfo
from nightwright.agentic import queue as Q
cfg = json.load(open(sys.argv[1]))
cfg["_tz"] = ZoneInfo(cfg["timezone"])
Q.run(cfg, until_idle=True, poll_interval_s=0.01)
"""


def test_a_coordinator_restart_mid_job_adopts_the_job(tmp_path, monkeypatch, camps):
    c = tracked(camps, build(tmp_path, monkeypatch, "two", blocking=True))
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps({k: v for k, v in c.cfg.items() if k != "_tz"}))
    (tmp_path / "coordinator.py").write_text(_COORDINATOR)
    first = subprocess.Popen([sys.executable, str(tmp_path / "coordinator.py"), str(cfg_file)],
                             start_new_session=True)
    try:
        job = job_id_of(c)
        assert until(lambda: c.started.is_file() and (RJ.read(c.cfg, job) or {}).get("state")
                     in ("submitted", "attached"))
    finally:
        os.killpg(first.pid, signal.SIGKILL)                # the coordinator dies mid-job
        first.wait()
    assert counted(c, 1) == 1
    assert (RJ.read(c.cfg, job) or {})["state"] in RJ.LIVE
    # a new coordinator over the same data dir: the job is adopted via attach/poll...
    t, _out = start_queue(c)
    assert until(lambda: RG.attached(c.cid))
    assert not ledger(c, ["worker_job_lost"])
    c.go.write_text("go\n")                                 # ...and only then finishes
    join(t, timeout=120)
    assert S.read_state(c.cfg, c.cid)["state"] == "MERGED"
    assert counted(c, 1) == 1                               # the worker process ran once
    assert not ledger(c, ["worker_job_lost"]) and not ledger(c, ["worker_failed"])
    gate = next(g for g in ledger(c, ["gate"]) if g["gate_seq"] == 1)
    assert gate["worker"] in ("wa", "wb") and len(ledger(c, ["gate"])) == 2
    assert job in worker_jobs(c) and job_id_of(c, attempt=2) not in worker_jobs(c)
    assert sum((r.wdata / "agentic" / "worker" / "jobs" / job).is_dir() for r in c.remotes) == 1


# ---- no bundle --------------------------------------------------------------------------------

def test_a_persona_job_returning_no_bundle_leaves_the_branch_and_main(tmp_path, monkeypatch):
    script_ = _write_script(tmp_path / "writer.json", WRITER)
    local = _write_script(tmp_path / "local.json", LOCAL_OK)
    w = _world(tmp_path)
    _remote(w, script_)
    branch0 = _git(w.repo, "rev-parse", f"nw/{w.state['id']}")
    main0 = _git(w.repo, "rev-parse", "main")
    seen = []

    def no_bundle(worktree, bundle):
        seen.append(dict(bundle))
        bundle.clear()                                      # the worker sent no bundle
    monkeypatch.setattr(RPS, "_before_writeback", no_bundle)
    from test_remote_personas import _ctx, _start
    req = _req(w)
    _start(w, req)
    res = Q._do_persona(_ctx(w, local), S.read_state(w.cfg, w.state["id"]),
                        Q.Step("implement", persona="implementer"), "001-implementer", req, 1.0)
    assert seen and seen[0].get("head")                     # the worker did commit
    end = w.ledger(["run.end"])[0]
    assert end["writeback_failed"] and end["worker"] == "box"
    assert res.run_result.worker is None                    # re-run locally, which commits nothing
    assert _git(w.repo, "rev-parse", f"nw/{w.state['id']}") == branch0
    assert _git(w.repo, "rev-parse", "main") == main0
    assert not (w.repo / "c.txt").exists()


# ---- forged frames ----------------------------------------------------------------------------

_FORGE = """import json, subprocess, sys
mode, spec = sys.argv[1], sys.argv[2]
p = subprocess.Popen(sys.argv[3:], stdout=subprocess.PIPE)
shift = 0
for line in p.stdout:
    try:
        frame = json.loads(line)
    except ValueError:
        frame = None
    if frame and isinstance(frame.get("n"), int):
        frame["n"] += shift                  # the real frames follow the forged one, renumbered
        line = (json.dumps(frame) + "\\n").encode()
    sys.stdout.buffer.write(line)
    sys.stdout.buffer.flush()
    if frame and frame.get("type") == "started" and not shift:
        forged = {"schema": frame["schema"], "n": frame["n"] + 1, "at": frame.get("at")}
        if mode == "campaign":      # a frame for another campaign's job
            forged.update(type="gate", job_id=spec + "-gate-001-a1")
        else:                       # a worker event of a type no worker may send
            forged.update(type="event", job_id=frame["job_id"], event={"type": spec})
        shift = 1
        sys.stdout.buffer.write((json.dumps(forged) + "\\n").encode())
        sys.stdout.buffer.flush()
sys.exit(p.wait())
"""


@pytest.mark.parametrize("mode,spec", [("campaign", "SPEC-0999-20261101-ffff"), ("type", "close"),
                                       ("type", "park"), ("type", "merge")])
def test_forged_worker_frames_are_refused_and_append_nothing(tmp_path, monkeypatch, mode, spec):
    c = build(tmp_path, monkeypatch, "two")
    forge = tmp_path / "forge.py"
    forge.write_text(_FORGE)
    for rec in WK.load(c.cfg):
        ssh = rec["ssh_argv"][0]
        _script(tmp_path / f"forge-{rec['name']}",
                f'case "$*" in *attach*|*gate-job*|*persona*) exec {sys.executable} {forge} '
                f'{mode} {spec} {ssh} "$@";; esac\nexec {ssh} "$@"')
    reg = json.loads(WK.path(c.cfg).read_text())
    for rec in reg["workers"]:
        rec["ssh_argv"] = [str(tmp_path / f"forge-{rec['name']}")]
    WK.path(c.cfg).write_text(json.dumps(reg))
    assert run_queue(c, monkeypatch)["state"] == "MERGED"
    allowed = {"open", "backlog", "transition", "run.start", "run.end", "cost", "commit", "gate",
               "review_outcome", "note", "close", "worker_failed", "worker_job_lost",
               "worker_job_retried", "finding"}
    evs = ledger(c)
    assert {e["type"] for e in evs} <= allowed, [e["type"] for e in evs]
    # a forged merge/close/park never took effect: one `close`, written by the queue itself
    (close,) = [e for e in evs if e["type"] == "close"]
    assert not str(close.get("by", "")).startswith("worker")
    assert not any(e["type"] == "park" for e in evs)
    assert not any(str(e.get("by", "")).startswith("worker") for e in evs)
    if mode == "campaign":                                   # refused as a protocol failure
        assert any(e["type"] in ("worker_failed", "worker_job_lost") for e in evs)
    else:       # the forged frame sat in a live stream whose jobs still ran on the workers
        assert all(g.get("worker") for g in evs if g["type"] == "gate")
        assert not any(e["type"] in ("worker_failed", "worker_job_lost") for e in evs)


# ---- credentials ------------------------------------------------------------------------------

def _env_keys(path: Path) -> dict:
    return dict(ln.split("=", 1) for ln in path.read_text().splitlines() if "=" in ln)


def _check_env(path: Path):
    env = _env_keys(path)
    for k in env:
        assert k not in FORBIDDEN_ENV and not k.startswith("GIT_CONFIG_"), k
        if k.startswith("NW_"):
            # `NW_WORKER_*`: the worker's own knobs (its job lock fd, progress, gc), set by it
            assert k in W.ENV_ALLOWLIST or k in WORKER_OWN or k.startswith("NW_WORKER_"), k
    assert not any(v.startswith("poison") or "/poison/" in v for v in env.values())
    assert env["NW_CAMPAIGN"]


def test_a_worker_gets_no_git_credentials_and_no_push_path(tmp_path, monkeypatch):
    for k, v in POISON.items():                  # the coordinator's own environment is full
        monkeypatch.setenv(k, v)
    c = finished(tmp_path, monkeypatch, "two", env_files=True)
    jobs = worker_jobs(c)
    _check_env(jobs[job_id_of(c, "gate", 1)][1] / "gate-env.txt")
    _check_env(jobs[job_id_of(c, "persona")][1] / "persona-env.txt")
    for (remote, jdir), name in ((jobs[job_id_of(c, "gate", 1)], "gate"),
                                 (jobs[job_id_of(c, "persona")], "persona")):
        # `git config --get-regexp credential` printed nothing (exit 1: no such key), the
        # checkout has no remote, and `git push` from it failed
        assert (jdir / f"{name}-cred.txt").read_text() == ""
        assert (jdir / f"{name}-cred.rc").read_text().strip() == "1"
        assert (jdir / f"{name}-push.rc").read_text().strip() != "0"
        # the checkout's only remote is the worker's own object cache, and nothing in its git
        # config names the coordinator's repo: it has no way to push there
        wdata = str(remote.wdata)
        urls = {ln.split()[1] for ln in (jdir / f"{name}-remote.txt").read_text().splitlines()}
        assert urls and all(u.startswith(wdata) for u in urls), urls
        assert str(c.repo) not in (jdir / f"{name}-config.txt").read_text()
    assert _git(c.repo, "rev-parse", "main") == S.read_state(c.cfg, c.cid)["close"]["merged_sha"]

