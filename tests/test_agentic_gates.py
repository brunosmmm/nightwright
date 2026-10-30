"""SPEC-0173: deterministic gate executor + `wt queue gates`. All gates are `sh -c` scripts in a
tmp worktree; the data dir is a tmp dir."""
import json
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from wt.agentic import gates as G
from wt.agentic import schemas as SC
from wt.agentic import store as S
from wt.cli import cli

CID = "SPEC-0170-20261025-c0de"


def _cfg(tmp_path):
    return {
        "org_files": [str(tmp_path / "org")],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(tmp_path / "data"),
        "config_dir": str(tmp_path / "cfg"),
        "org_capture_file": str(tmp_path / "org" / "inbox.org"),
        "org_ideas_file": str(tmp_path / "org" / "ideas.org"),
        "specs_dir": str(tmp_path / "specs"),
        "outbox_targets": {},
        "agentic": {"profiles": {}},
    }


def _gate(name, cmd, *, required=True, timeout_s=30, cwd="."):
    return {"name": name, "cmd": cmd, "cwd": cwd, "timeout_s": timeout_s, "required": required}


def _state(worktree):
    return {"id": CID, "spec_id": "SPEC-0170", "project": "demo", "profile": "balanced",
            "state": "GATING", "created_at": "2026-10-25T10:00:00-04:00",
            "repo": {"base": "main", "worktree": str(worktree), "branch": f"wt/{CID}"},
            "counters": {"loops": 0, "runs": 0}, "cost": {"total_usd": 0.0, "cap_usd": 10.0},
            "last_gate": None, "park": None}


@pytest.fixture
def world(tmp_path):
    cfg = _cfg(tmp_path)
    wt = tmp_path / "worktree"
    (wt / "sub").mkdir(parents=True)
    st = _state(wt)
    S.create_campaign(cfg, st)
    return cfg, st, wt


# ---- run_gate -----------------------------------------------------------------------------

def test_pass_fail_results_and_log(world, tmp_path):
    cfg, st, wt = world
    env = G.gate_env(cfg, st)
    ok = G.run_gate(_gate("ok", "echo hello; echo world"), wt, env, tmp_path / "ok.log", {})
    bad = G.run_gate(_gate("bad", "echo boom >&2; exit 3", required=False), wt, env,
                     tmp_path / "bad.log", {})
    assert (ok.ok, ok.exit_code, ok.timed_out, ok.required) == (True, 0, False, True)
    assert (bad.ok, bad.exit_code, bad.timed_out, bad.required) == (False, 3, False, False)
    assert ok.tail.splitlines()[-2:] == ["hello", "world"]
    assert "boom" in bad.tail                                 # stderr merged into the log
    assert (tmp_path / "ok.log").read_text() == "hello\nworld\n"   # raw output only (SPEC-0192)
    side = json.loads((tmp_path / "ok.json").read_text())
    assert SC.record_problems(side, SC.GATE_KEYS, SC.GATE_SCHEMA) == []
    assert side["cmd"] == "echo hello; echo world" and side["exit_code"] == 0 and side["ok"]
    assert side["log"] == "ok.log" and side["error"] is None and ok.cmd == side["cmd"]
    assert ok.duration_s >= 0 and ok.log == str(tmp_path / "ok.log")


def test_timeout_kills_the_process_group(world, tmp_path):
    cfg, st, wt = world
    pidfile = tmp_path / "child.pid"
    gate = _gate("slow", f"sleep 30 & echo $! > {pidfile}; wait", timeout_s=1)
    t0 = time.monotonic()
    r = G.run_gate(gate, wt, G.gate_env(cfg, st), tmp_path / "slow.log", {})
    assert r.timed_out and not r.ok
    assert time.monotonic() - t0 < 15
    child = int(pidfile.read_text().strip())
    for _ in range(50):                       # the background `sleep` must be gone too
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail(f"grandchild {child} survived the group kill")
    side = json.loads((tmp_path / "slow.json").read_text())
    assert side["timed_out"] and side["error"] == "timed out after 1s; process group killed"
    assert "timed out" not in (tmp_path / "slow.log").read_text()


def test_env_placeholders_and_cwd(world, tmp_path):
    cfg, st, wt = world
    env = G.gate_env(cfg, st)
    r = G.run_gate(_gate("env", "echo $WT_CAMPAIGN $WT_SPEC_ID $WT_WORKTREE $WT_BASE "
                                "$WT_DATA_DIR $WT_CONFIG_DIR; pwd", cwd="sub"),
                   wt, env, tmp_path / "env.log", G.placeholders(st))
    lines = r.tail.splitlines()
    assert lines[-2].split() == [CID, "SPEC-0170", str(wt), "main", cfg["data_dir"],
                                 cfg["config_dir"]]
    assert lines[-1] == str((wt / "sub").resolve())
    r2 = G.run_gate(_gate("ph", "echo {spec_id}/{campaign}/{base} '{other}'"), wt, env,
                    tmp_path / "ph.log", G.placeholders(st))
    assert r2.tail.splitlines()[-1] == f"SPEC-0170/{CID}/main {{other}}"


def test_tail_is_bounded(world, tmp_path):
    cfg, st, wt = world
    r = G.run_gate(_gate("many", "seq 1 200"), wt, G.gate_env(cfg, st), tmp_path / "m.log", {})
    lines = r.tail.splitlines()
    assert len(lines) == G.TAIL_LINES and lines[-1] == "200"
    assert G.tail(tmp_path / "nope.log") == ""


def test_unstartable_cwd_is_a_failed_result(world, tmp_path):
    cfg, st, wt = world
    r = G.run_gate(_gate("nocwd", "true", cwd="missing"), wt, G.gate_env(cfg, st),
                   tmp_path / "n.log", {})
    assert not r.ok and r.exit_code is None and not r.timed_out
    side = json.loads((tmp_path / "n.json").read_text())
    assert side["error"].startswith("failed to start") and side["exit_code"] is None


# ---- run_all ------------------------------------------------------------------------------

def _project(*gates):
    return {"gates": list(gates)}


def test_run_all_runs_everything_and_required_decides(world):
    cfg, st, wt = world
    marker = wt / "ran-last"
    proj = _project(_gate("first", "exit 1"),                       # required failure
                    _gate("opt", "exit 2", required=False),
                    _gate("last", f"touch {marker}"))
    run = G.run_all(proj, st, cfg, seq=1)
    assert marker.exists(), "gates after a required failure still ran"
    assert not run.ok and [r.name for r in run.results] == ["first", "opt", "last"]
    assert [r.name for r in G.blocking_failures(run)] == ["first"]
    cdir = S.campaign_dir(cfg, CID)
    assert [r.log for r in run.results] == ["gates/001-first.log", "gates/001-opt.log",
                                            "gates/001-last.log"]
    assert all((cdir / r.log).is_file() for r in run.results)
    only_optional = G.run_all(_project(_gate("opt", "exit 2", required=False)), st, cfg, seq=2)
    assert only_optional.ok and not only_optional.results[0].ok
    assert G.blocking_failures(only_optional) == []


def test_as_payload_and_next_seq(world):
    cfg, st, wt = world
    assert G.next_gate_seq(cfg, CID) == 1
    run = G.run_all(_project(_gate("a", "true")), st, cfg, seq=G.next_gate_seq(cfg, CID))
    p = G.as_payload(run)
    assert p["gate_seq"] == 1 and p["ok"] is True and p["results"][0]["name"] == "a"
    assert "seq" not in p                                      # never clobber the ledger seq
    assert set(p["results"][0]) == {"name", "required", "ok", "exit_code", "timed_out",
                                    "duration_s", "log", "tail", "cmd", "cwd"}
    json.dumps(p)                                               # ledger-serializable
    assert G.next_gate_seq(cfg, CID) == 2


def test_run_all_without_worktree_errors(world):
    cfg, st, _ = world
    st = dict(st, repo={"base": "main"})
    with pytest.raises(ValueError, match="no worktree"):
        G.run_all(_project(_gate("a", "true")), st, cfg, seq=1)


# ---- wt queue gates -------------------------------------------------------------------------

def _repo_with_project_yaml(tmp_path, gates_yaml):
    root = tmp_path / "repo"
    (root / "src" / "wt").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    (root / ".wt").mkdir()
    (root / ".wt" / "project.yaml").write_text(
        "schema: wt.project.v1\nproject: Meta-Tools\nbase: main\ngates:\n" + gates_yaml)
    return root


def _cli(cfg, monkeypatch, args):
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    return CliRunner().invoke(cli, args)


def test_cli_gates_logs_event_and_leaves_state_alone(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    root = _repo_with_project_yaml(tmp_path, "  - {name: good, cmd: 'echo fine'}\n"
                                             "  - {name: bad, cmd: 'exit 4', required: false}\n")
    cfg["wt_checkout"] = str(root)                 # Meta-Tools resolves to this repo
    wt = tmp_path / "worktree"
    wt.mkdir()
    st = dict(_state(wt), project="Meta-Tools")
    S.create_campaign(cfg, st)
    before = S.read_state(cfg, CID)

    r = _cli(cfg, monkeypatch, ["queue", "gates", CID, "--json"])
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["schema"] == "wt.campaign.gate.v1" and d["campaign"] == CID
    assert d["ok"] is True and d["gate_seq"] == 1
    assert [(x["name"], x["ok"], x["required"]) for x in d["results"]] == [
        ("good", True, True), ("bad", False, False)]

    events = S.read_ledger(cfg, CID, types=["gate"])
    assert len(events) == 1 and events[0]["by"] == "human"
    assert events[0]["seq"] == 2 and events[0]["gate_seq"] == 1   # envelope seq intact
    after = S.read_state(cfg, CID)
    assert after["state"] == before["state"] == "GATING"
    assert after["last_gate"] is None                          # debug runs never steer

    r2 = _cli(cfg, monkeypatch, ["queue", "gates", CID])
    assert r2.exit_code == 0, r2.output
    assert "good" in r2.output and "exit 4" in r2.output
    assert len(S.read_ledger(cfg, CID, types=["gate"])) == 2
    assert json.loads(_cli(cfg, monkeypatch, ["queue", "gates", CID, "--json"]).output)["gate_seq"] == 3


def test_cli_gates_errors(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    r = _cli(cfg, monkeypatch, ["queue", "gates", "SPEC-0000-00000000-dead"])
    assert r.exit_code != 0 and "no campaign" in r.output
    root = _repo_with_project_yaml(tmp_path, "  - {name: good, cmd: 'true'}\n")
    cfg["wt_checkout"] = str(root)
    st = dict(_state(tmp_path / "gone"), project="Meta-Tools")
    S.create_campaign(cfg, st)
    r = _cli(cfg, monkeypatch, ["queue", "gates", CID])
    assert r.exit_code != 0 and "no worktree on disk" in r.output
    assert S.read_ledger(cfg, CID, types=["gate"]) == []


# ---- SPEC-0284: result summaries ----------------------------------------------------------

def _res_gate(name, cmd, results):
    return {**_gate(name, cmd), "results": results}


def test_run_gate_records_summary(world, tmp_path):
    cfg, st, wt = world
    g = _res_gate("pytest", "echo 'FAILED t.py::test_a - boom'; "
                  "echo '=== 1 failed, 2 passed in 0.10s ==='; exit 1", {"format": "pytest"})
    r = G.run_gate(g, wt, G.gate_env(cfg, st), tmp_path / "p.log", {})
    assert r.ok is False and r.exit_code == 1
    assert r.summary["failed"] == 1 and r.summary["failing"] == ["t.py::test_a"]
    side = json.loads((tmp_path / "p.json").read_text())
    assert side["summary"]["failed"] == 1 and side["ok"] is False
    assert SC.record_problems(side, SC.GATE_KEYS, SC.GATE_SCHEMA) == []
    run = G.GateRun(1, False, (r,), "t", 0.1)
    payload = G.as_payload(run)
    assert payload["results"][0]["summary"]["failed"] == 1
    ev = {"schema": SC.LEDGER_SCHEMA, "ts": "t", "seq": 1, "campaign": CID, "type": "gate",
          "by": "tick", **payload}
    assert SC.ledger_problems(ev) == []


def test_run_gate_parse_error_keeps_ok(world, tmp_path):
    cfg, st, wt = world
    g = _res_gate("pytest", "echo garbage", {"format": "pytest"})
    r = G.run_gate(g, wt, G.gate_env(cfg, st), tmp_path / "p.log", {})
    assert r.ok is True and r.exit_code == 0
    assert r.summary["format"] == "pytest" and r.summary["error"]
    side = json.loads((tmp_path / "p.json").read_text())
    assert side["ok"] is True and side["summary"]["error"]


def test_junit_gate_reads_path_relative_to_cwd(world, tmp_path):
    cfg, st, wt = world
    cmd = ("printf '<testsuite><testcase classname=\"c\" name=\"t\"><failure/></testcase>"
           "</testsuite>' > r.xml")
    g = {**_res_gate("j", cmd, {"format": "junit", "path": "r.xml"}), "cwd": "sub"}
    r = G.run_gate(g, wt, G.gate_env(cfg, st), tmp_path / "j.log", {})
    assert r.ok and r.summary["failed"] == 1 and r.summary["failing"] == ["c::t"]


def test_run_gate_nonzero_exit_with_all_passed_summary_stays_failed(world, tmp_path):
    cfg, st, wt = world
    g = _res_gate("pytest", "echo '=== 3 passed in 0.1s ==='; exit 1", {"format": "pytest"})
    r = G.run_gate(g, wt, G.gate_env(cfg, st), tmp_path / "p.log", {})
    assert r.ok is False and r.exit_code == 1 and r.summary["failed"] == 0


def test_junit_gate_ignores_file_left_by_an_earlier_round(world, tmp_path):
    cfg, st, wt = world
    stale = wt / "r.xml"
    stale.write_text("<testsuite><testcase classname='c' name='old'><failure/></testcase>"
                     "</testsuite>")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    g = _res_gate("j", "exit 3", {"format": "junit", "path": "r.xml"})
    r = G.run_gate(g, wt, G.gate_env(cfg, st), tmp_path / "j.log", {})
    assert r.ok is False and r.exit_code == 3
    assert "stale" in r.summary["error"] and "failing" not in r.summary


def test_stopped_gate_gets_no_summary(world, tmp_path):
    """SPEC-0229 x SPEC-0284: a gate that never started because of a stop is not parsed."""
    cfg, st, wt = world
    G.terminate_all()
    try:
        g = _res_gate("pytest", "echo '=== 1 passed in 0.1s ==='", {"format": "pytest"})
        r = G.run_gate(g, wt, G.gate_env(cfg, st), tmp_path / "p.log", {})
    finally:
        G.reset()
    assert r.interrupted and r.summary is None
    assert "summary" not in json.loads((tmp_path / "p.json").read_text())


def test_no_results_no_summary_key(world, tmp_path):
    cfg, st, wt = world
    r = G.run_gate(_gate("ok", "echo hi"), wt, G.gate_env(cfg, st), tmp_path / "ok.log", {})
    assert r.summary is None
    assert "summary" not in json.loads((tmp_path / "ok.json").read_text())
    payload = G.as_payload(G.GateRun(1, True, (r,), "t", 0.1))
    assert "summary" not in payload["results"][0]


# ---- SPEC-0289: retry ladder ----------------------------------------------------------------

PY_FAIL = ("echo 'FAILED tests/a.py::t1 - boom'; "
           "echo '=== 1 failed, 2 passed in 0.10s ==='; exit 1")
PY_PASS = "echo '=== 1 passed in 0.01s ==='; exit 0"
PY_COLLECT = ("echo 'ERROR tests/x.py - ImportError'; "
              "echo '!!! Interrupted: 1 error during collection !!!'; "
              "echo '=== 1 error in 0.10s ==='; exit 2")


def _fake(tmp_path, behaviours, *, retries=2, results=True, **extra):
    """A gate whose Nth invocation runs `behaviours[N-1]` (a shell snippet). Every call
    appends its argv to `args.log`."""
    d = tmp_path / "fake"
    d.mkdir(exist_ok=True)
    for i, snippet in enumerate(behaviours, 1):
        (d / f"beh.{i}").write_text(snippet + "\n")
    script = d / "run.sh"
    script.write_text(f'n=$(cat "{d}/cnt" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "{d}/cnt"\n'
                      f'echo "$@" >> "{d}/args.log"; sh "{d}/beh.$n" "$@"\n')
    g = {**_gate("pytest", f"sh {script}"), "retries": retries, **extra}
    if results:
        g["results"] = {"format": "pytest"}
    return g


def _args(tmp_path):
    p = tmp_path / "fake" / "args.log"
    return p.read_text().splitlines() if p.exists() else []


def _ladder(world, tmp_path, gate, **kw):
    cfg, st, wt = world
    out = tmp_path / "g"
    return G.run_gate_with_retries(gate, wt, G.gate_env(cfg, st), out / "001-pytest.log", {},
                                   log_rel="gates/001-pytest.log", **kw)


def _kinds(r):
    return [a["kind"] for a in r.attempts]


def test_full_retry_passes_second_time(world, tmp_path):
    g = _fake(tmp_path, ["echo boom; exit 1", "echo fine"], results=False)
    r = _ladder(world, tmp_path, g)
    assert r.ok and len(r.attempts) == 2 and _kinds(r) == ["full", "full"]
    assert (tmp_path / "g" / "001-pytest.r1.log").is_file()
    assert r.attempts[1]["log"] == "gates/001-pytest.r1.log" and r.log == "gates/001-pytest.log"


def test_full_retry_exhausted(world, tmp_path):
    g = _fake(tmp_path, ["exit 1", "exit 2", "exit 3"], results=False)
    r = _ladder(world, tmp_path, g)
    assert not r.ok and len(r.attempts) == 3 and r.exit_code == 3
    assert [a["exit_code"] for a in r.attempts] == [1, 2, 3] and r.flaky == ()


def test_targeted_rerun_reproduces(world, tmp_path):
    g = _fake(tmp_path, [PY_FAIL, "echo again; exit 1", PY_PASS, PY_PASS], retries=3)
    r = _ladder(world, tmp_path, g)
    assert not r.ok and _kinds(r) == ["full", "targeted"] and len(_args(tmp_path)) == 2
    assert _args(tmp_path)[1] == "tests/a.py::t1" and r.exit_code == 1
    assert r.attempts[0]["failing"] == ["tests/a.py::t1"]


def test_targeted_rerun_flaky_ok(world, tmp_path):
    g = _fake(tmp_path, [PY_FAIL, PY_PASS, PY_PASS])
    r = _ladder(world, tmp_path, g)
    assert r.ok and _kinds(r) == ["full", "targeted", "full"]
    assert r.flaky == ("tests/a.py::t1",) and _args(tmp_path)[2] == ""


def test_targeted_then_full_after_early_stop(world, tmp_path):
    stop_fail = ("echo 'FAILED tests/a.py::t1 - boom'; echo '!!! stopping after 1 failures !!!'; "
                 "echo '=== 1 failed, 2 passed in 0.10s ==='; exit 1")
    r = _ladder(world, tmp_path, _fake(tmp_path, [stop_fail, PY_PASS, PY_PASS]))
    assert r.ok
    assert _kinds(r) == ["full", "targeted", "full"]
    t2 = tmp_path / "t2"
    t2.mkdir()
    r = _ladder(world, t2, _fake(t2, [stop_fail, PY_PASS, PY_PASS], retries=1))
    assert not r.ok and r.flaky == () and r.exit_code == 1
    assert _kinds(r) == ["full", "targeted"] and r.attempts[1]["ok"] is True


def test_targeted_then_full_fails(world, tmp_path):
    r = _ladder(world, tmp_path, _fake(tmp_path, [PY_FAIL, PY_PASS, "exit 1"]))
    assert not r.ok and r.flaky == () and _kinds(r) == ["full", "targeted", "full"]
    assert r.attempts[1]["ok"] is True
    t2 = tmp_path / "t2"
    t2.mkdir()
    r = _ladder(world, t2, _fake(t2, [PY_FAIL, PY_PASS, "exit 1", PY_PASS], retries=3))
    assert r.ok and r.flaky == ("tests/a.py::t1",)


def test_targeted_after_collection_error_runs_full(world, tmp_path):
    r = _ladder(world, tmp_path, _fake(tmp_path, [PY_COLLECT, PY_PASS, PY_PASS]))
    assert r.attempts[0]["failing"] == ["tests/x.py"] and _args(tmp_path)[1] == "tests/x.py"
    assert r.ok and _kinds(r) == ["full", "targeted", "full"]
    t2 = tmp_path / "t2"
    t2.mkdir()
    r = _ladder(world, t2, _fake(t2, [PY_COLLECT, PY_PASS, "exit 1"]))
    assert not r.ok and r.flaky == ()


def test_retry_cmd_template_used(world, tmp_path):
    g = _fake(tmp_path, [PY_FAIL, PY_PASS, PY_PASS], retry_cmd=f"sh {tmp_path}/fake/run.sh -k {{failing}}")
    r = _ladder(world, tmp_path, g)
    assert r.ok and _args(tmp_path)[1] == "-k tests/a.py::t1"
    cmd = G.targeted_cmd({"retry_cmd": "make t IDS={failing}"}, "make", ["a b::t[x y]", "c::d"])
    assert cmd == "make t IDS='a b::t[x y]' c::d"
    assert G.targeted_cmd({}, "uv run pytest", ["a b"]) == "uv run pytest 'a b'"


def test_incomplete_failing_list_falls_back_to_full(world, tmp_path):
    many = ("for i in $(seq 1 50); do echo \"FAILED tests/a.py::t$i\"; done; "
            "echo '=== 60 failed, 1 passed in 1s ==='; exit 1")
    r = _ladder(world, tmp_path, _fake(tmp_path, [many, PY_PASS]))
    assert r.ok and _kinds(r) == ["full", "full"] and r.flaky == ()
    assert _args(tmp_path)[1] == ""


@pytest.mark.parametrize("how", ["event", "flag"])
def test_no_retry_when_stopping(world, tmp_path, monkeypatch, how):
    ev = threading.Event()
    real = G.run_gate

    def wrapped(*a, **kw):
        r = real(*a, **kw)
        if how == "event":
            ev.set()
        else:
            monkeypatch.setattr(G, "_stopping", True)
        return r
    monkeypatch.setattr(G, "run_gate", wrapped)
    g = _fake(tmp_path, ["exit 1", "exit 0"], results=False)
    try:
        r = _ladder(world, tmp_path, g, stop=ev)
    finally:
        G.reset()
    assert r.attempts == () and not r.ok and len(_args(tmp_path)) == 1
    assert r.interrupted           # the stop cut the ladder: not a failure to triage (SPEC-0229)
    side = json.loads((tmp_path / "g" / "001-pytest.json").read_text())
    assert side["interrupted"] is True and "stopping" in side["error"]


def test_stop_during_retry_is_interrupted(world, tmp_path, monkeypatch):
    cfg, st, wt = world
    ev = threading.Event()
    real = G.run_gate

    def wrapped(*a, **kw):
        if kw.get("attempt") == 1:
            ev.set()
            G.terminate_all()
        return real(*a, **kw)
    monkeypatch.setattr(G, "run_gate", wrapped)
    g = _fake(tmp_path, ["exit 1", "exit 0", "exit 0"], retries=3, results=False)
    try:
        run = G.run_all(_project(g), st, cfg, seq=1, stop=ev)
    finally:
        G.reset()
    (r,) = run.results
    assert r.interrupted and not r.ok and len(r.attempts) == 2 and r.attempts[-1]["interrupted"]
    assert run.interrupted and not run.ok
    side = json.loads((tmp_path / "data").rglob("001-pytest.json").__next__().read_text())
    assert side["interrupted"] is True and SC.record_problems(side, SC.GATE_KEYS, SC.GATE_SCHEMA) == []
    assert "stopping" in side["error"]      # the cut retry's error, not attempt 0's


def _patch_attempt(monkeypatch, n, **changes):
    """Make `run_gate` report attempt `n` (0 = the first run) with `changes` applied."""
    real = G.run_gate

    def wrapped(*a, **kw):
        r = real(*a, **kw)
        return replace(r, **changes) if kw.get("attempt") == (n or None) else r
    monkeypatch.setattr(G, "run_gate", wrapped)


def test_timed_out_attempt_zero_not_retried(world, tmp_path, monkeypatch):
    _patch_attempt(monkeypatch, 0, timed_out=True, ok=False)
    r = _ladder(world, tmp_path, _fake(tmp_path, ["exit 1", "exit 0"], results=False))
    assert r.timed_out and not r.ok and r.attempts == () and len(_args(tmp_path)) == 1


def test_interrupted_attempt_zero_not_retried(world, tmp_path, monkeypatch):
    _patch_attempt(monkeypatch, 0, interrupted=True)
    r = _ladder(world, tmp_path, _fake(tmp_path, ["exit 1", "exit 0"], results=False))
    assert r.interrupted and not r.ok and r.attempts == () and len(_args(tmp_path)) == 1


def test_retry_timeout_ends_ladder(world, tmp_path, monkeypatch):
    _patch_attempt(monkeypatch, 1, timed_out=True, ok=False)
    g = _fake(tmp_path, ["exit 1", "exit 1", "exit 0"], retries=3, results=False)
    r = _ladder(world, tmp_path, g)
    assert len(r.attempts) == 2 and r.timed_out and not r.ok and len(_args(tmp_path)) == 2


def test_result_fields_after_ladder(world, tmp_path):
    r = _ladder(world, tmp_path, _fake(tmp_path, ["exit 1", "exit 0"], results=False))
    assert r.ok and r.exit_code == 0 and not r.timed_out and not r.interrupted
    assert r.duration_s == pytest.approx(sum(a["duration_s"] for a in r.attempts), abs=0.002)
    assert r.log == "gates/001-pytest.log"
    t2 = tmp_path / "t2"
    t2.mkdir()
    r = _ladder(world, t2, _fake(t2, [PY_FAIL, PY_PASS], retries=1))
    assert r.summary["failed"] == 1 and r.summary["passed"] == 2 and not r.ok


def test_tail_and_summary_come_from_the_last_full_attempt(world, tmp_path):
    full_fail = "echo 'distinct full tail'; echo '=== 2 failed in 0.20s ==='; exit 1"
    r = _ladder(world, tmp_path, _fake(tmp_path, [PY_FAIL, PY_PASS, full_fail]))
    assert not r.ok and "distinct full tail" in r.tail and "boom" not in r.tail
    assert r.summary["failed"] == 2 and r.summary["passed"] == 0
    t2 = tmp_path / "t2"
    t2.mkdir()
    full_ok = "echo 'full ok tail'; echo '=== 5 passed in 0.20s ==='"
    r = _ladder(world, t2, _fake(t2, [PY_FAIL, PY_PASS, full_ok]))
    assert r.ok and "full ok tail" in r.tail and r.summary["passed"] == 5
    assert r.summary["failed"] == 0
    t3 = tmp_path / "t3"
    t3.mkdir()
    r = _ladder(world, t3, _fake(t3, ["echo boom; exit 1", "echo fine"], results=False))
    assert "fine" in r.tail and "boom" not in r.tail


def test_attempt0_sidecar_rewritten(world, tmp_path):
    r = _ladder(world, tmp_path, _fake(tmp_path, [PY_FAIL, PY_PASS, PY_PASS]))
    side = json.loads((tmp_path / "g" / "001-pytest.json").read_text())
    assert side["ok"] is True and side["exit_code"] == 0 and side["flaky"] == ["tests/a.py::t1"]
    assert side["attempts"][0]["ok"] is False and side["attempts"][0]["exit_code"] == 1
    assert [a["kind"] for a in side["attempts"]] == ["full", "targeted", "full"]
    assert side["duration_s"] == r.duration_s
    assert SC.record_problems(side, SC.GATE_KEYS, SC.GATE_SCHEMA) == []


def test_payload_omits_empty_attempts(world, tmp_path):
    cfg, st, wt = world
    run = G.run_all(_project(_gate("plain", "exit 1")), st, cfg, seq=1)
    (res,) = G.as_payload(run)["results"]
    assert "attempts" not in res and "flaky" not in res
    side = json.loads((S.campaign_dir(cfg, CID) / "gates" / "001-plain.json").read_text())
    assert "attempts" not in side and "flaky" not in side and "attempt" not in side
    assert not list((S.campaign_dir(cfg, CID) / "gates").glob("*.r1.*"))


def test_retry_sidecars_validate(world, tmp_path):
    cfg, st, wt = world
    g = _fake(tmp_path, [PY_FAIL, PY_PASS, PY_PASS])
    run = G.run_all(_project(g), st, cfg, seq=1)
    (res,) = run.results
    assert res.ok and res.flaky == ("tests/a.py::t1",)
    gdir = S.campaign_dir(cfg, CID) / "gates"
    sides = sorted(gdir.glob("001-pytest.r*.json"))
    assert [p.name for p in sides] == ["001-pytest.r1.json", "001-pytest.r2.json"]
    for n, p in enumerate(sides, 1):
        rec = json.loads(p.read_text())
        assert rec["attempt"] == n and SC.record_problems(rec, SC.GATE_KEYS, SC.GATE_SCHEMA) == []
        assert rec["ok"] is True and rec["exit_code"] == 0
        assert "attempts" not in rec and "flaky" not in rec
    r1, r2 = (json.loads(p.read_text()) for p in sides)
    assert r1["summary"]["passed"] == 1 and r1["cmd"].endswith("tests/a.py::t1")
    assert r2["cmd"] == res.cmd
    payload = G.as_payload(run)
    assert payload["results"][0]["flaky"] == ["tests/a.py::t1"]
    ev = {"schema": SC.LEDGER_SCHEMA, "ts": "t", "seq": 1, "campaign": CID, "type": "gate",
          "by": "tick", **payload}
    assert SC.ledger_problems(ev) == []


def _res(cmd="pytest -q", **summary):
    base = {"passed": 1, "failed": 0, "errors": 0, "skipped": 0, "failing": []}
    return G.GateResult("pytest", True, False, 1, False, 0.1, "l", "", cmd=cmd,
                        summary={**base, **summary})


PYTEST_GATE = {"results": {"format": "pytest"}}


def test_targeted_ids_eligibility():
    ok = _res(failed=2, failing=["a::1", "a::2"])
    assert G._targeted_ids(PYTEST_GATE, ok) == ["a::1", "a::2"]
    # not pytest results (JUnit, none)
    assert G._targeted_ids({"results": {"format": "junit"}}, ok) == []
    assert G._targeted_ids({}, ok) == []
    # a parse error in the summary
    assert G._targeted_ids(PYTEST_GATE, _res(failed=1, failing=["a::1"], error="bad")) == []
    # the list does not account for every failure or error (either way)
    assert G._targeted_ids(PYTEST_GATE, _res(failed=3, failing=["a::1", "a::2"])) == []
    assert G._targeted_ids(PYTEST_GATE, _res(failed=1, failing=["a::1", "a::2"])) == []
    assert G._targeted_ids(PYTEST_GATE, _res(failed=1, errors=1, failing=["a::1"])) == []
    assert G._targeted_ids(PYTEST_GATE, _res(failed=0, failing=[])) == []


@pytest.mark.parametrize("cmd", ["pytest -q 2>&1 | tee out.txt", "pytest -q > out.txt",
                                 "cd x; pytest", "a && pytest", "pytest $(echo -q)"])
def test_targeted_ids_need_an_appendable_cmd(cmd):
    res = _res(cmd=cmd, failed=1, failing=["a::1"])
    assert G._targeted_ids(PYTEST_GATE, res) == []
    # an explicit retry_cmd takes the ids wherever it wants them
    assert G._targeted_ids({**PYTEST_GATE, "retry_cmd": "make t IDS={failing}"}, res) == ["a::1"]


def test_pytest_usage_error_on_targeted_rerun_is_not_reproduced(world, tmp_path):
    g = _fake(tmp_path, [PY_FAIL, "echo 'ERROR: not found'; exit 4", PY_PASS])
    r = _ladder(world, tmp_path, g)
    assert r.ok and _kinds(r) == ["full", "targeted", "full"]
    assert r.flaky == ()                # the ids never ran: no flake evidence
    t2 = tmp_path / "t2"
    t2.mkdir()
    r = _ladder(world, t2, _fake(t2, [PY_FAIL, "exit 5", "exit 1"]))
    assert not r.ok and len(r.attempts) == 3


def test_deadline_skips_a_full_rerun_that_would_overrun(world, tmp_path):
    g = _fake(tmp_path, ["exit 1", "exit 0"], results=False)
    r = _ladder(world, tmp_path, g, deadline=time.monotonic() - 1)
    assert not r.ok and r.attempts == () and len(_args(tmp_path)) == 1
    t2 = tmp_path / "t2"
    t2.mkdir()
    g = _fake(t2, [PY_FAIL, PY_PASS, PY_PASS])      # the targeted rung is not budgeted
    r = _ladder(world, t2, g, deadline=time.monotonic() - 1)
    assert _kinds(r) == ["full", "targeted"] and not r.ok


def test_rewrite_sidecar_error_and_clearing(world, tmp_path):
    g = _fake(tmp_path, [PY_FAIL, PY_PASS, PY_PASS])
    first = _ladder(world, tmp_path, g)
    side = tmp_path / "g" / "001-pytest.json"
    assert json.loads(side.read_text())["summary"] is not None
    final = replace(first, summary=None, interrupted=False, flaky=(), attempts=first.attempts)
    G._rewrite_sidecar(tmp_path / "g" / "001-pytest.log", final, "boom")
    rec = json.loads(side.read_text())
    assert rec["error"] == "boom"
    assert "summary" not in rec and "interrupted" not in rec and "flaky" not in rec
    assert not side.with_name(side.name + ".tmp").exists()
    final = replace(final, interrupted=True, flaky=("x",))
    G._rewrite_sidecar(tmp_path / "g" / "001-pytest.log", final, None)
    rec = json.loads(side.read_text())
    assert rec["interrupted"] is True and rec["flaky"] == ["x"] and rec["error"] is None


def test_rewrite_sidecar_survives_an_unreadable_sidecar(world, tmp_path):
    d = tmp_path / "g"
    d.mkdir()
    (d / "001-pytest.json").write_text("{trunc")
    G._rewrite_sidecar(d / "001-pytest.log", G.GateResult("p", True, True, 0, False, 0.1, "l", ""),
                       None)                # no raise; the file is left as it was
    assert (d / "001-pytest.json").read_text() == "{trunc"
