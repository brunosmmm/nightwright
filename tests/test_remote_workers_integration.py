"""SPEC-0346: the SSH-workers integration suite. Whole fake campaigns (`test_agentic_e2e`'s
repo, gates and `FakeRunner`) run through the real queue (`queue.run`) with two loopback SSH
workers (SPEC-0338's loopback script via `remote_support.Remote`, each with its own data and
config dirs) next to the coordinator's own data dir. Gate jobs run tiny shell gates on the
worker; persona jobs run `FakeRunner` there (`NW_AGENTIC_RUNNER=fake`). Failures are real: a
killed process group and a killed coordinator process, never a mocked transport. Waits use
`tests/waits.py`; the kill and restart cases synchronise on sentinel files.

SPEC-0334 adds its pull-worker cases to this file; shared loopback fixtures stay in
`remote_support`."""
import base64
import json
import os
import re
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

pytestmark = pytest.mark.remote

GOLDEN = Path(__file__).parent / "data" / "remote_workers" / "no_workers_golden.json"
GATES_GOLDEN = Path(__file__).parent / "data" / "remote_gates_golden.json"

# keys of the environment a worker job's process may carry from the coordinator's job header:
# the allowlist itself plus the keys the worker sets itself (`runner.ENV_RESET`, the persona
# markers) and the launch environment of the fake `ssh` (what sshd gives a forced command)
# The worker process's own environment (what the fake ssh, standing in for sshd, may pass) plus
# the keys the worker sets for the child (SPEC-0336). Nothing else, and none of the header
# allowlist's complement.
WORKER_OWN = frozenset({"NW_DATA_DIR", "NW_CONFIG_DIR", "NW_WORKTREE", "NW_PERSONA",
                        "NW_PERSONA_RUN", "NW_REBASE_TARGET", "NW_GATE_REPORT", "NW_AGENTIC_RUNNER",
                        "NW_AGENTIC_FAKE_SCRIPT"})
# `PWD`, `SHLVL` and `_` are set by the shell that runs the command, after the worker has
# already handed it only the allowset. They are not inherited from the coordinator.
EXACT_ENV = W.ENV_ALLOWLIST | WORKER_OWN | frozenset({
    "PATH", "HOME", "TMPDIR", "TEMP", "TMP", "NW_WORKER_PROGRESS_S", "PWD", "SHLVL", "_"})
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


def _inspect(name, repo, *, ahead=False):
    """Shell run inside the worker's checkout (a gate's or a persona's cwd): its env and what
    git finds there, written next to the checkout in the job dir (the checkout itself is gone
    when the job ends). The push targets the coordinator repo. `ahead` makes a commit the
    coordinator does not have, so the push is an update and not "everything up-to-date"."""
    prep = "git commit --allow-empty -q -m 'not on main'; " if ahead else ""
    return (f"env > ../{name}-env.txt; git config --get-regexp credential > ../{name}-cred.txt; "
            f"echo $? > ../{name}-cred.rc; git remote -v > ../{name}-remote.txt; "
            f"git config --list > ../{name}-config.txt; {prep}"
            f"GIT_TERMINAL_PROMPT=0 git push {repo} HEAD:refs/heads/main "
            f"> ../{name}-push.txt 2>&1; echo $? > ../{name}-push.rc")


def build(tmp_path, monkeypatch, workers=None, *, blocking=False, lease_s=None, env_files=False,
          persona_blocks=False):
    """A queued fake campaign. `workers`: None (no `workers.json`), "empty" (an empty registry)
    or "two" (two loopback workers, both gate- and persona-capable). `blocking`: the lint gate
    blocks on a sentinel. `env_files`: the gates and the persona write their env to a file in
    the worker's job dir. `persona_blocks`: the worker's `FakeRunner` says it started and blocks
    until `c.go` exists, before it writes anything."""
    c = SimpleNamespace(tmp=tmp_path, go=tmp_path / "GO", started=tmp_path / "STARTED",
                        counter=tmp_path / "counter", remotes=[])
    c.go.unlink(missing_ok=True)
    lint = "true"
    if blocking:
        lint = _blocking_gate(c)
    if env_files:
        lint = _inspect("gate", tmp_path / "repo") + "; " + lint
    c.repo = make_repo(tmp_path, lint_gate=lint)
    GOLD.add_shared_gate(c.repo)
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
            entry["pre_shell"] = [_inspect("persona", c.repo, ahead=True)]
        if persona_blocks:
            # commit something the dead attempt could have merged, then block before `done`
            entry["pre_shell"] = [
                "echo from-dead-attempt > dead-attempt.txt && git add dead-attempt.txt && "
                "git commit -q -m 'dead attempt'; "
                f"touch {c.started}; while [ ! -f {c.go} ]; do sleep 0.05; done"]
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
    # match this campaign's job id, not just "gate-001": when the suite itself runs as a gate job on
    # a worker box, every temp path carries that outer job's own `...-gate-001-a1` name
    return sum(f"/{c.cid}-gate-{seq:03d}-" in ln for ln in c.counter.read_text().splitlines())


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

def test_pre_epic_golden_reproduces_from_the_pinned_commit(tmp_path):
    """The checked-in fixture is what `NW_RECORD_GOLDEN=1` records in a temp worktree of
    `1f0311c4^`. Replacing the file with output from current code fails this."""
    out = tmp_path / "again.json"
    GOLD.reproduce(out)
    assert out.read_bytes() == GOLDEN.read_bytes()


def test_no_workers_run_matches_the_pre_epic_golden(tmp_path, monkeypatch):
    c = finished(tmp_path, monkeypatch)
    snap = GOLD.snapshot(c.cfg, c.cid, tmp_path)
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    # the fixture is the `1f0311c4^` recording; the comparison keeps review_outcome and the
    # stable head/tree/seq/nw_sha/profile fields rather than dropping them
    assert GOLD.comparable(snap) == GOLD.comparable(GOLD.with_kept_fields(golden))
    assert not any("worker" in e for e in ledger(c))
    assert not WK.path(c.cfg).exists()
    # the gate portion equals SPEC-0338's golden on the masked fields, for the gate both share
    theirs = json.loads(GATES_GOLDEN.read_text(encoding="utf-8"))
    assert GOLD.SHARED_GATE.split("name: ")[1].split(",")[0] == "ok"
    sha = re.compile(r"\b[0-9a-f]{40}\b")
    fields = ("cmd", "name", "required", "ok", "exit_code", "timed_out")

    def result(events, name):
        ev = next(e for e in events if e["type"] == "gate" and e["gate_seq"] == 1)
        return next(r for r in ev["results"] if r["name"] == name)

    mine_r, base_r = result(snap["events"], "ok"), result(theirs["events"], "ok")
    assert {k: mine_r[k] for k in fields} == {k: base_r[k] for k in fields}
    assert mine_r["exit_code"] == 0 and mine_r["cmd"] == "echo hello; git rev-parse HEAD"
    # the tail is `hello` and the head sha, which differs per repo: masked
    assert sha.sub("<SHA>", mine_r["tail"]) == sha.sub("<SHA>", base_r["tail"]) == "hello\n<SHA>"
    assert mine_r["log"] == base_r["log"] == "gates/001-ok.log"      # the `NNN-<name>.log` pattern
    # cwd (campaign worktree versus the golden's repo), duration_s: masked, but present
    assert mine_r["cwd"] and base_r["cwd"] and mine_r["duration_s"] == base_r["duration_s"] == "<X>"
    # the nw.gate.v1 sidecar
    def side(files):                  # the snapshot masks a sidecar's timings in its text: `<X>`
        v = files["001-ok.json"]
        return json.loads(v.replace(": <X>", ': "<X>"')) if isinstance(v, str) else v

    mine_s, base_s = side(snap["files"]), side(theirs["files"])
    sfields = ("schema", "name", "cmd", "required", "ok", "exit_code", "timed_out", "timeout_s",
               "error", "log")
    assert {k: mine_s[k] for k in sfields} == {k: base_s[k] for k in sfields}
    assert mine_s["schema"] == "nw.gate.v1" and mine_s["timeout_s"] == 60 and mine_s["error"] is None
    # SPEC-0504: the pre-epic snapshot drops a sidecar's `resources`; SPEC-0338's golden has it
    assert set(mine_s) == set(base_s) - {"resources"} and mine_s["cwd"] and base_s["cwd"]
    assert mine_s["started_at"] and base_s["started_at"]
    assert mine_s["duration_s"] == base_s["duration_s"] == "<X>"
    assert sha.sub("<SHA>", snap["files"]["001-ok.log"]) == \
        sha.sub("<SHA>", theirs["files"]["001-ok.log"]) == "hello\n<SHA>\n"
    assert {n.split("-", 1)[1].split(".")[0] for n in snap["files"]} == {"lint", "ok", "test"}


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
    # the close re-gate is the coordinator's: it has no worker key, so it is not in this list
    assert [e["type"] for e in on_worker] == ["run.end", "gate", "run.end"]
    assert {e["worker"] for e in on_worker} <= {"wa", "wb"}
    gates = [e for e in ledger(remote) if e["type"] == "gate"]
    assert gates[0].get("worker") in ("wa", "wb") and "worker" not in gates[1]
    # the loop gate and the persona jobs ran on the workers; the close re-gate did not
    jobs = worker_jobs(remote)
    assert sorted(jobs) == sorted([
        f"{remote.cid}-gate-001-a1",
        RPS.job_id_for(remote.cid, "001-implementer"),
        RPS.job_id_for(remote.cid, "002-conformance_reviewer")])


def test_the_merge_happens_on_the_coordinator_after_the_close_regate(tmp_path, monkeypatch):
    c = finished(tmp_path, monkeypatch, "two")
    evs = ledger(c)
    close = next(e for e in evs if e["type"] == "close")
    regate = [e for e in evs if e["type"] == "gate" and e["gate_seq"] == close["gates_seq"]]
    assert regate and evs.index(regate[0]) < evs.index(close)
    # the re-gate is the coordinator's: no worker key, and it follows the worker's gate
    assert "worker" not in regate[0]
    earlier = [e for e in evs if e["type"] == "gate" and e.get("worker")]
    assert earlier and evs.index(earlier[0]) < evs.index(regate[0])
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
        # cost and worker straight from the export's rows (SPEC-0346: `worker` column)
        assert runs[rid]["cost_usd"] == e["cost_usd"] and runs[rid]["worker"] == e["worker"]
        assert runs[rid]["worker"] in ("wa", "wb")
    by = {}
    for e in ends.values():
        by[e["worker"]] = round(by.get(e["worker"], 0.0) + e["cost_usd"], 4)
    assert camp["cost_by_worker"] == by and sum(by.values()) > 0


# ---- duplicate delivery -----------------------------------------------------------------------

def test_a_duplicate_submit_starts_one_process_and_a_duplicate_event_appends_one(
        tmp_path, monkeypatch):
    c = build(tmp_path, monkeypatch, "two")
    install_dup(c)                                          # one gate frame goes out twice
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
    # The worker's final `gate` event is on the ledger, and the transition that would write
    # the state (and `mark_done`) has not run. The restarted queue adopts the same job and
    # appends that event again; `event_uid` keeps a single ledger row.
    real_append = S.append_event
    hit = {"n": 0}

    def crash_after_the_worker_gate(cfg, campaign_id, type, payload=None, *, by="tick"):
        rec = real_append(cfg, campaign_id, type, payload, by=by)
        # every application of this event, including the crash sweep's re-harvest, stops
        # before the transition writes the state or marks the job done
        if type == "gate" and rec.get("worker") and rec.get("event_uid"):
            hit["n"] += 1
            raise RuntimeError("stop after the worker gate event, before the state is written")
        return rec
    monkeypatch.setattr(S, "append_event", crash_after_the_worker_gate)
    # SPEC-0484 contains an apply error (the campaign parks `apply_error`); this test is about
    # a runner that dies mid-apply, so it restores the pre-0484 harvest for the crash
    real_apply, real_guarded = Q._apply, Q._guarded

    def uncontained(guard, ctx, st, fn, report=None, res=None):
        if not guard:
            return fn()
        try:
            fn()
        except Exception as e:
            Q.journal(ctx.cfg, "harvest_error", campaign=st.get("id"), error=repr(e))
    monkeypatch.setattr(Q, "_apply", Q._apply_step)
    monkeypatch.setattr(Q, "_guarded", uncontained)
    with pytest.raises(RuntimeError, match="before the state is written"):
        Q.run(c.cfg, until_idle=True, poll_interval_s=0.01)
    monkeypatch.setattr(S, "append_event", real_append)
    monkeypatch.setattr(Q, "_apply", real_apply)
    monkeypatch.setattr(Q, "_guarded", real_guarded)
    first = [g for g in ledger(c, ["gate"]) if g.get("event_uid")]
    assert len(first) == 1 and first[0].get("worker") in ("wa", "wb")
    uid = first[0]["event_uid"]
    assert S.read_state(c.cfg, c.cid)["state"] == "GATING"
    assert (RJ.read(c.cfg, job_id_of(c)) or {}).get("state") in RJ.LIVE
    assert counted(c, 1) == 1
    assert run_queue(c, monkeypatch)["state"] == "MERGED"
    assert [g["event_uid"] for g in ledger(c, ["gate"]) if g.get("event_uid") == uid] == [uid]
    assert counted(c, 1) == 1                               # adoption did not start a second process
    assert sorted(again) == sorted(worker_jobs(c))          # every remote job went out twice
    assert again                                                    # and there was at least one
    # the same gate frame delivered twice on the worker stream appends one coordinator event
    marks = list(tmp_path.glob("dup-*.mark"))
    assert marks and all(p.read_text().strip() for p in marks)   # the frame was delivered twice
    gates = ledger(c, ["gate"])
    assert [g["gate_seq"] for g in gates] == [1, 2] and len(gates) == 2
    assert gates[0].get("worker") in ("wa", "wb")           # the duplicate did not fail the job


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


def test_a_killed_persona_job_is_retried_and_the_campaign_merges(tmp_path, monkeypatch, camps):
    c = tracked(camps, build(tmp_path, monkeypatch, "two", persona_blocks=True, lease_s=3))
    late = tmp_path / "late-done.json"
    # once the dead attempt has committed, the next persona frame carries a `done` for another
    # attempt whose bundle is that commit: accepting it would fast-forward the campaign
    install_forge(c, "late-done", str(late), argv_glob="*-persona-*")
    t, _out = start_queue(c)
    first = job_id_of(c, "persona")
    assert first == RPS.job_id_for(c.cid, "001-implementer", 1)
    assert until(lambda: c.started.is_file() and first in worker_jobs(c)
                 and (worker_jobs(c)[first][1] / "proc.json").is_file())
    remote, jdir = worker_jobs(c)[first]
    tree = jdir / "tree"
    assert until(lambda: (tree / "dead-attempt.txt").is_file())
    late.write_text(json.dumps({
        "job_id": RPS.job_id_for(c.cid, "001-implementer", 9),
        "bundle": bundle_from(tree, tmp_path / "dead.bundle")}))
    assert until(lambda: any(tmp_path.glob("forged-*")))    # the stale done was delivered
    pid = json.loads((jdir / "proc.json").read_text())["pid"]
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)          # the persona job and its fake runner
    except ProcessLookupError:
        pass
    c.go.write_text("go\n")                                 # the retry's fake may finish
    join(t, timeout=120)
    st = S.read_state(c.cfg, c.cid)
    assert st["state"] == "MERGED"
    lost = [e for e in ledger(c, ["worker_job_lost"]) if e["job_id"] == first]
    assert len(lost) == 1 and lost[0]["attempt"] == 1
    # a lost persona job is retried by running the persona on the coordinator's own runner (there
    # is no `worker_job_retried`, which is the gate job's event): one implementer run ended, and
    # it is the local one
    ends = [e for e in ledger(c, ["run.end"]) if e["persona"] == "implementer"]
    assert len(ends) == 1 and not ends[0].get("worker")
    # the dead attempt delivered nothing: the worker reaped it as `lost` with no result frame and
    # no `done` frame, and exactly one implementer commit is on the merged branch (the retry's)
    assert json.loads((jdir / "result.json").read_text()) == {
        "state": "lost", "frame": None, "reaped_at": json.loads(
            (jdir / "result.json").read_text())["reaped_at"], "attempt": 1}    # SPEC-0503
    assert '"done"' not in (jdir / "frames.jsonl").read_text()
    merged = _git(c.repo, "rev-parse", "main")
    assert merged == st["close"]["merged_sha"]
    base = _git(c.repo, "rev-list", "--max-parents=0", "main").split()[0]
    commits = _git(c.repo, "log", "--format=%s", f"{base}..main").splitlines()
    assert sum("implementer" in ln for ln in commits) == 1, commits
    assert not any(str(e.get("event_uid", "")).startswith(first) for e in ledger(c))
    # the dead attempt's commit, and the stale `done` that carried its bundle, never landed
    marks = [json.loads(ln) for f in tmp_path.glob("forged-*") for ln in f.read_text().splitlines()]
    assert marks and all(m["job_id"].endswith("-a9") and m["type"] == "done" for m in marks)
    assert "from-stale-persona" not in json.dumps(ledger(c))
    assert "dead attempt" not in _git(c.repo, "log", "--all", "--format=%s")
    assert "dead-attempt.txt" not in _git(c.repo, "ls-tree", "-r", "--name-only", "HEAD")


def remote_name(remote, c):
    for rec in WK.load(c.cfg):
        if str(remote.wdata).endswith("wdata" + rec["name"][1:]):
            return rec["name"]
    raise AssertionError("unknown remote")


def test_a_late_frame_of_the_dead_attempt_is_ignored(tmp_path, monkeypatch, camps):
    """Attempt 1 is killed and the job retried; a well-formed `gate` frame naming attempt 1 then
    reaches the coordinator on attempt 2's stream. It is refused: nothing carries the dead
    attempt's uid, the campaign still merges, and the one seq-1 gate event is not the forged one."""
    c = tracked(camps, build(tmp_path, monkeypatch, "two", blocking=True, lease_s=3))
    first = job_id_of(c)
    install_forge(c, "stale", first, argv_glob="*-gate-001-a2*")
    # the rejected worker is on cooldown, so the reviewer runs on the coordinator's own runner:
    # its script (the implementer ran on a worker, which never advanced it) starts at the review
    script(tmp_path, [review()])
    t, _out = start_queue(c)
    assert until(lambda: c.started.is_file() and first in worker_jobs(c)
                 and (worker_jobs(c)[first][1] / "proc.json").is_file())
    jdir = worker_jobs(c)[first][1]
    os.killpg(os.getpgid(json.loads((jdir / "proc.json").read_text())["pid"]), signal.SIGKILL)
    c.go.write_text("go\n")
    join(t, timeout=120)
    assert S.read_state(c.cfg, c.cid)["state"] == "MERGED"
    marks = [json.loads(ln) for f in tmp_path.glob("forged-*") for ln in f.read_text().splitlines()]
    assert marks and all(m["job_id"] == first for m in marks)      # the late frame was delivered
    assert [e["job_id"] for e in ledger(c, ["worker_job_lost"])][:1] == [first]
    gates = [g for g in ledger(c, ["gate"]) if g["gate_seq"] == 1]
    assert len(gates) == 1 and "stale" not in json.dumps(gates[0])
    assert not any(str(e.get("event_uid", "")).startswith(first) for e in ledger(c))
    assert any(e["type"] in ("worker_failed", "worker_job_retried") for e in ledger(c))


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
    """A whole queued campaign. The worker's implementer commits, but the stream's `done`
    frame carries no bundle; the local re-run commits nothing, so the branch and main stay."""
    c = build(tmp_path, monkeypatch, "two")
    quiet = impl_done()
    quiet.pop("edits")
    script(tmp_path, [quiet, review()])                     # the coordinator commits nothing
    # the worker does commit (entry 0 of its own script); the stream then drops the bundle
    entry = impl_done()
    entry.pop("persona")
    entry["verdict"].update(review()["verdict"])
    entry["shell"] = ["git add -A && git commit -q -m 'on the worker'"]
    script(tmp_path, [entry], "worker-script.json")
    (c.repo / "gates" / "test.sh").write_text("true\n")     # the coordinator never writes feature.txt
    _git(c.repo, "commit", "-qam", "gates pass with no feature file")
    install_forge(c, "nobundle", "-", argv_glob="*-persona-*")
    main0 = _git(c.repo, "rev-parse", "main")
    run_queue(c, monkeypatch)
    marks = [f.read_text() for f in tmp_path.glob("forged-*")]
    assert any("nobundle" in m for m in marks)              # a done frame left with no bundle
    ends = [e for e in ledger(c, ["run.end"]) if e.get("persona") == "implementer"]
    assert any(e.get("writeback_failed") for e in ends)
    assert _git(c.repo, "rev-parse", "main") == main0
    assert _git(c.repo, "rev-parse", f"nw/{c.cid}") == main0
    assert not (c.repo / "c.txt").exists() and not (c.repo / "feature.txt").exists()


# ---- forged frames ----------------------------------------------------------------------------

_FORGE = """import json, os, subprocess, sys
mode, spec, marker = sys.argv[1], sys.argv[2], sys.argv[3]
p = subprocess.Popen(sys.argv[4:], stdout=subprocess.PIPE)
shift = 0

def forged_done(frame, doc):
    # a `done` whose bundle a write-back would fast-forward, naming another job
    return {"schema": frame["schema"], "type": "done", "n": frame["n"] + 1,
            "at": frame.get("at"), "job_id": doc["job_id"], "ok": True, "interrupted": False,
            "started_at": frame.get("at"), "duration_s": 0.0,
            "result": {"exit_code": 0, "timed_out": False, "interrupted": False,
                       "duration_s": 0.0, "is_error": False, "cost_usd": 0.0,
                       "verdict_ok": True, "verdict": {"status": "done", "summary": "stale"},
                       "result_text": "from-stale-persona"},
            "files": {}, "bundle": doc["bundle"]}

def emit(forged):
    global shift
    shift = 1
    sys.stdout.buffer.write((json.dumps(forged) + "\\n").encode())
    sys.stdout.buffer.flush()
    with open(marker, "a") as m:             # proof that a forged frame went onto the stream
        m.write(json.dumps(forged) + "\\n")

for line in p.stdout:
    try:
        frame = json.loads(line)
    except ValueError:
        frame = None
    if mode == "nobundle" and frame and frame.get("type") == "done" and frame.get("bundle"):
        frame["bundle"] = None               # the worker returned no bundle
        line = (json.dumps(frame) + "\\n").encode()
        with open(marker, "a") as m:
            m.write("nobundle\\n")
    if frame and isinstance(frame.get("n"), int):
        frame["n"] += shift                  # the real frames follow the forged one, renumbered
        line = (json.dumps(frame) + "\\n").encode()
    sys.stdout.buffer.write(line)
    sys.stdout.buffer.flush()
    if mode == "nobundle" or not frame or not isinstance(frame.get("n"), int) or shift:
        continue
    forged = None
    if mode == "late-done" and frame.get("type") in ("started", "progress", "accepted") \\
            and os.path.isfile(spec):
        forged = forged_done(frame, json.load(open(spec)))
    elif frame.get("type") == "started":
        forged = {"schema": frame["schema"], "n": frame["n"] + 1, "at": frame.get("at")}
        if mode == "campaign":      # a frame for another campaign's job
            forged.update(type="gate", job_id=spec + "-gate-001-a1")
        elif mode == "stale":       # a late, well-formed gate frame of the dead attempt `spec`
            forged.update(type="gate", job_id=spec, files={}, result={
                "name": "lint", "required": True, "ok": True, "exit_code": 0, "timed_out": False,
                "cmd": "sh gates/lint.sh", "cwd": "/", "duration_s": 0.0, "tail": "stale"})
        elif mode == "persona-gate":   # an allowlisted gate frame naming another job
            forged.update(type="gate", job_id=spec, files={}, result={
                "name": "lint", "required": True, "ok": True, "exit_code": 0, "timed_out": False,
                "cmd": "true", "cwd": "/", "duration_s": 0.0, "tail": "forged-persona-gate"})
        elif mode == "persona-done":   # an allowlisted done frame naming another job
            forged = forged_done(frame, json.load(open(spec)))
        elif mode == "persona-done-nojid":   # allowlisted done, this job, no job_id
            forged = forged_done(frame, json.load(open(spec)))
            forged.pop("job_id", None)
        elif mode == "gate-nojid":   # allowlisted gate frame with no job_id
            forged.update(type="gate")
        elif mode == "persona-no-n":
            forged = forged_done(frame, json.load(open(spec)))
            forged.pop("n", None)
        elif mode == "persona-n-bool":
            forged = forged_done(frame, json.load(open(spec)))
            forged["n"] = True
        elif mode == "persona-n-zero":
            forged = forged_done(frame, json.load(open(spec)))
            forged["n"] = 0
        else:                       # a worker event of a type no worker may send
            forged.update(type="event", job_id=frame["job_id"], event={"type": spec})
    if forged:
        emit(forged)
sys.exit(p.wait())
"""


_DUP = """import json, subprocess, sys
marker = sys.argv[1]
p = subprocess.Popen(sys.argv[2:], stdout=subprocess.PIPE)
sent = False
for line in p.stdout:
    sys.stdout.buffer.write(line)
    sys.stdout.buffer.flush()
    if sent:
        continue
    try:
        frame = json.loads(line)
    except ValueError:
        frame = None
    if frame and frame.get("type") == "gate":
        sys.stdout.buffer.write(line)          # the same frame, the same n, once more
        sys.stdout.buffer.flush()
        sent = True
        with open(marker, "a") as m:
            m.write(line.decode())
sys.exit(p.wait())
"""


def bundle_from(tree: Path, dest: Path) -> dict:
    """A bundle of `tree`'s HEAD against its parent, packed the way a worker's `done` frame
    packs one (`refs/nw/job-result/head` plus the parent as the basis). Write-back
    fast-forwards a worktree sitting on that parent onto HEAD."""
    head = _git(tree, "rev-parse", "HEAD")
    parent = _git(tree, "rev-parse", "HEAD^")
    ref = "refs/nw/job-result/head"
    _git(tree, "update-ref", ref, head)
    _git(tree, "bundle", "create", "-q", str(dest), ref, f"^{parent}")
    return {"head": head, "wip": None,
            "bundle_b64": base64.b64encode(dest.read_bytes()).decode("ascii")}


def stale_result_bundle(repo: Path, tmp: Path) -> dict:
    """A commit on top of `repo` HEAD that adds `stale-persona.txt`, packed as a result bundle."""
    clone = tmp / "stale-src"
    subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True,
                   env={**os.environ, **ENV})
    (clone / "stale-persona.txt").write_text("from-stale-persona\n")
    _git(clone, "add", "stale-persona.txt")
    _git(clone, "commit", "-qm", "stale persona")
    return bundle_from(clone, tmp / "stale.bundle")


def install_dup(c):
    """Deliver one `gate` frame twice on each worker's gate-job stream."""
    tmp_path = c.tmp
    dup = tmp_path / "dup.py"
    dup.write_text(_DUP)
    for rec in WK.load(c.cfg):
        ssh = rec["ssh_argv"][0]
        mark = tmp_path / f"dup-{rec['name']}.mark"
        _script(tmp_path / f"dup-{rec['name']}",
                f'case "$*" in *-gate-*) exec {sys.executable} {dup} {mark} {ssh} "$@";; esac\n'
                f'exec {ssh} "$@"')
    reg = json.loads(WK.path(c.cfg).read_text())
    for rec in reg["workers"]:
        rec["ssh_argv"] = [str(tmp_path / f"dup-{rec['name']}")]
    WK.path(c.cfg).write_text(json.dumps(reg))


def install_forge(c, mode, spec, argv_glob="*attach*|*gate-job*|*persona*"):
    """Put the frame forger (`_FORGE`) in front of every worker's `ssh_argv`: it forges one frame
    after the `started` frame of each stream whose argv matches `argv_glob`, and notes it in
    `<tmp>/forged-<worker>`."""
    tmp_path = c.tmp
    forge = tmp_path / "forge.py"
    forge.write_text(_FORGE)
    for rec in WK.load(c.cfg):
        ssh = rec["ssh_argv"][0]
        mark = tmp_path / f"forged-{rec['name']}"
        _script(tmp_path / f"forge-{rec['name']}",
                f'case "$*" in {argv_glob}) exec {sys.executable} {forge} '
                f'{mode} {json.dumps(spec)} {json.dumps(str(mark))} {ssh} "$@";; esac\n'
                f'exec {ssh} "$@"')
    reg = json.loads(WK.path(c.cfg).read_text())
    for rec in reg["workers"]:
        rec["ssh_argv"] = [str(tmp_path / f"forge-{rec['name']}")]
    WK.path(c.cfg).write_text(json.dumps(reg))


@pytest.mark.parametrize("mode,spec", [("campaign", "SPEC-0999-20261101-ffff"), ("gate-nojid", "-"),
                                       ("type", "close"), ("type", "park"), ("type", "merge")])
def test_forged_worker_frames_are_refused_and_append_nothing(tmp_path, monkeypatch, mode, spec):
    """Gate transport only. A persona stream that accepted the same frame would not satisfy
    this test: the failure's job id is the gate job."""
    c = build(tmp_path, monkeypatch, "two")
    install_forge(c, mode, spec, argv_glob="*-gate-*")
    assert run_queue(c, monkeypatch)["state"] == "MERGED"
    # the forged frame really went onto a worker's stream (a wrapper that never matched the
    # real argv, or a stream without a `started` frame, would leave no marker and test nothing)
    marks = [json.loads(ln) for f in tmp_path.glob("forged-*") for ln in f.read_text().splitlines()]
    want = {"campaign": "gate", "gate-nojid": "gate", "type": "event"}[mode]
    assert marks and all(m["type"] == want for m in marks)
    if mode == "gate-nojid":
        assert all("job_id" not in m for m in marks)
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
    fails = [e for e in evs if e["type"] in ("worker_failed", "worker_job_lost")]
    assert fails and all("gate" in str(e.get("job_id")) for e in fails)
    if mode == "campaign":                                   # refused as a protocol failure
        # ...and nothing landed in the other campaign, where a forged write would go
        other = spec
        with pytest.raises(ValueError):
            S.read_state(c.cfg, other)
        assert S.read_ledger(c.cfg, other) == [] and not S.gates_dir(c.cfg, other).exists()
    elif mode == "gate-nojid":
        assert any(e.get("job_id") == job_id_of(c) and e.get("stage") == "protocol" for e in fails)
    elif spec != "close":
        # a non-allowlisted type is refused: the forged type is not appended
        # (the one `close` is the queue's own, already checked above)
        assert not any(e["type"] == spec for e in evs)


_OTHER = "SPEC-0999-20261101-ffff"


@pytest.mark.parametrize("mode", ["persona-gate", "persona-done", "persona-done-nojid",
                                  "persona-no-n", "persona-n-bool", "persona-n-zero", "type"])
def test_forged_persona_frames_are_refused_and_append_nothing(tmp_path, monkeypatch, mode):
    """Persona transport only. An allowlisted `gate` or `done` for another job is refused
    before the cursor moves, and the `done`'s bundle is not written back."""
    c = build(tmp_path, monkeypatch, "two")
    _THIS_JOB_FORGES = ("persona-done-nojid", "persona-no-n", "persona-n-bool", "persona-n-zero")
    if mode in ("persona-done", *_THIS_JOB_FORGES):
        jid = (f"{_OTHER}-persona-001-implementer-a1" if mode == "persona-done"
               else job_id_of(c, "persona"))
        doc = {"job_id": jid, "bundle": stale_result_bundle(c.repo, tmp_path)}
        spec = tmp_path / f"persona-{mode}.json"
        spec.write_text(json.dumps(doc))
        spec_arg = str(spec)
    elif mode == "persona-gate":
        spec_arg = f"{_OTHER}-gate-001-a1"
    else:
        spec_arg = "close"
    install_forge(c, mode, spec_arg, argv_glob="*-persona-*")
    # close prunes a final job record; keep it so the cursor assertion can read it
    monkeypatch.setattr(RJ, "prune", lambda *a, **k: [])
    assert run_queue(c, monkeypatch)["state"] == "MERGED"
    marks = [json.loads(ln) for f in tmp_path.glob("forged-*") for ln in f.read_text().splitlines()]
    want = {"persona-gate": "gate", "persona-done": "done", "persona-done-nojid": "done",
            "persona-no-n": "done", "persona-n-bool": "done", "persona-n-zero": "done",
            "type": "event"}[mode]
    assert marks and all(m["type"] == want for m in marks)
    evs = ledger(c)
    fails = [e for e in evs if e["type"] in ("worker_failed", "worker_job_lost")]
    impl = job_id_of(c, "persona")
    impl_lost = [e for e in fails if e.get("job_id") == impl]
    assert impl_lost and all("protocol" in (e.get("reason") or "") for e in impl_lost)
    assert fails and all("persona" in str(e.get("job_id")) for e in fails)
    if mode == "type":
        assert not any(e["type"] == "close" and str(e.get("by", "")).startswith("worker")
                       for e in evs)
        return
    live = RJ.read(c.cfg, job_id_of(c, "persona"))
    assert live and live.get("state") == "lost"
    forged_ns = {m["n"] for m in marks if isinstance(m.get("n"), int)}
    assert live.get("cursor") not in forged_ns
    assert "forged-persona-gate" not in json.dumps(evs)
    assert "from-stale-persona" not in json.dumps(evs)
    assert "stale persona" not in _git(c.repo, "log", "--all", "--format=%s")
    assert "stale-persona.txt" not in _git(c.repo, "ls-tree", "-r", "--name-only", "HEAD")
    if mode in _THIS_JOB_FORGES:
        if mode == "persona-done-nojid":
            assert all("job_id" not in m for m in marks)
        return
    foreign = marks[0]["job_id"]
    assert all(m["job_id"] == foreign for m in marks)
    assert all(foreign in (e.get("reason") or "") for e in impl_lost)
    assert all(e.get("job_id") != foreign for e in evs)
    with pytest.raises(ValueError):
        S.read_state(c.cfg, _OTHER)
    assert S.read_ledger(c.cfg, _OTHER) == [] and not S.gates_dir(c.cfg, _OTHER).exists()


# ---- credentials ------------------------------------------------------------------------------

def _env_keys(path: Path) -> dict:
    return dict(ln.split("=", 1) for ln in path.read_text().splitlines() if "=" in ln)


def _check_env(path: Path):
    env = _env_keys(path)
    assert set(env) <= EXACT_ENV, sorted(set(env) - EXACT_ENV)
    for k in FORBIDDEN_ENV:
        assert k not in env
    assert not any(k.startswith("GIT_CONFIG_") for k in env)
    assert not any(v.startswith("poison") or "/poison/" in v for v in env.values())
    assert env["NW_CAMPAIGN"] and "NW_SPEC_ID" in env and "NW_BASE" in env


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
        push = (jdir / f"{name}-push.txt").read_text()
        # git's refusal names the branch as `HEAD -> main`; a push that landed would
        # not say it failed
        assert "failed to push" in push or "refusing to update" in push
        # the checkout's only remote is the worker's own object cache, and nothing in its git
        # config names the coordinator's repo: it has no way to push there
        wdata = str(remote.wdata)
        urls = {ln.split()[1] for ln in (jdir / f"{name}-remote.txt").read_text().splitlines()}
        assert urls and all(u.startswith(wdata) for u in urls), urls
        assert str(c.repo) not in (jdir / f"{name}-config.txt").read_text()
    assert _git(c.repo, "rev-parse", "main") == S.read_state(c.cfg, c.cid)["close"]["merged_sha"]
