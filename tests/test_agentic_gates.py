"""SPEC-0173: deterministic gate executor + `nw queue gates`. All gates are `sh -c` scripts in a
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

from nightwright.agentic import gates as G
from nightwright.agentic import schemas as SC
from nightwright.agentic import store as S
from nightwright.cli import cli

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
            "repo": {"base": "main", "worktree": str(worktree), "branch": f"nw/{CID}"},
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
    r = G.run_gate(_gate("env", "echo $NW_CAMPAIGN $NW_SPEC_ID $NW_WORKTREE $NW_BASE "
                                "$NW_DATA_DIR $NW_CONFIG_DIR; pwd", cwd="sub"),
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


# ---- nw queue gates -------------------------------------------------------------------------

def _repo_with_project_yaml(tmp_path, gates_yaml):
    root = tmp_path / "repo"
    (root / "src" / "nightwright").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    (root / ".nw").mkdir()
    (root / ".nw" / "project.yaml").write_text(
        "schema: nw.project.v1\nproject: Meta-Tools\nbase: main\ngates:\n" + gates_yaml)
    return root


def _cli(cfg, monkeypatch, args):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    return CliRunner().invoke(cli, args)


def test_cli_gates_logs_event_and_leaves_state_alone(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    root = _repo_with_project_yaml(tmp_path, "  - {name: good, cmd: 'echo fine'}\n"
                                             "  - {name: bad, cmd: 'exit 4', required: false}\n")
    cfg["nw_checkout"] = str(root)                 # Meta-Tools resolves to this repo
    wt = tmp_path / "worktree"
    wt.mkdir()
    st = dict(_state(wt), project="Meta-Tools")
    S.create_campaign(cfg, st)
    before = S.read_state(cfg, CID)

    r = _cli(cfg, monkeypatch, ["queue", "gates", CID, "--json"])
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["schema"] == "nw.campaign.gate.v1" and d["campaign"] == CID
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
    cfg["nw_checkout"] = str(root)
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


def _stop_after_attempt_one(monkeypatch, ev, how):
    """Set the stop once the real attempt 1 returned: it ran to completion (no terminate_all),
    so the ladder sees the stop only at the top of the next iteration."""
    real = G.run_gate

    def wrapped(*a, **kw):
        r = real(*a, **kw)
        if kw.get("attempt") == 1:
            if how == "event":
                ev.set()
            else:
                monkeypatch.setattr(G, "_stopping", True)
        return r
    monkeypatch.setattr(G, "run_gate", wrapped)


@pytest.mark.parametrize("how", ["event", "flag"])
def test_stop_after_completed_retry_is_interrupted(world, tmp_path, monkeypatch, how):
    ev = threading.Event()
    _stop_after_attempt_one(monkeypatch, ev, how)
    g = _fake(tmp_path, ["exit 1", "exit 1", "exit 0", "exit 0"], retries=3, results=False)
    try:
        r = _ladder(world, tmp_path, g, stop=ev)
    finally:
        G.reset()
    assert len(_args(tmp_path)) == 2 and len(r.attempts) == 2
    assert r.interrupted and not r.ok            # the stop cut the ladder: not a failure to triage
    assert r.attempts[-1]["interrupted"] is False
    side = json.loads((tmp_path / "g" / "001-pytest.json").read_text())
    assert side["interrupted"] is True and SC.record_problems(side, SC.GATE_KEYS, SC.GATE_SCHEMA) == []
    assert side["attempts"][0]["interrupted"] is False


@pytest.mark.parametrize("how", ["event", "flag"])
def test_stop_after_completed_retry_run_is_interrupted(world, tmp_path, monkeypatch, how):
    cfg, st, wt = world
    ev = threading.Event()
    _stop_after_attempt_one(monkeypatch, ev, how)
    g = _fake(tmp_path, ["exit 1", "exit 1", "exit 0", "exit 0"], retries=3, results=False)
    try:
        run = G.run_all(_project(g), st, cfg, seq=1, stop=ev)
    finally:
        G.reset()
    (r,) = run.results
    assert len(_args(tmp_path)) == 2 and r.interrupted and not r.ok
    assert run.interrupted and not run.ok


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


def test_targeted_retry_timeout_ends_ladder(world, tmp_path, monkeypatch):
    _patch_attempt(monkeypatch, 1, timed_out=True, ok=False)
    g = _fake(tmp_path, [PY_FAIL, "exit 1", PY_PASS, PY_PASS], retries=3)
    r = _ladder(world, tmp_path, g)
    assert _kinds(r) == ["full", "targeted"] and len(_args(tmp_path)) == 2
    assert r.timed_out and not r.ok and r.flaky == ()
    assert r.exit_code == 1 and r.summary["failed"] == 1 and r.summary["passed"] == 2


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


def _sc(res):
    return res.summary, res.cmd


def test_targeted_ids_eligibility():
    ok = _res(failed=2, failing=["a::1", "a::2"])
    assert G._targeted_ids(PYTEST_GATE, *_sc(ok)) == ["a::1", "a::2"]
    # not pytest results (JUnit, none)
    assert G._targeted_ids({"results": {"format": "junit"}}, *_sc(ok)) == []
    assert G._targeted_ids({}, *_sc(ok)) == []
    # a parse error in the summary
    assert G._targeted_ids(PYTEST_GATE, *_sc(_res(failed=1, failing=["a::1"], error="bad"))) == []
    # the list does not account for every failure or error (either way)
    assert G._targeted_ids(PYTEST_GATE, *_sc(_res(failed=3, failing=["a::1", "a::2"]))) == []
    assert G._targeted_ids(PYTEST_GATE, *_sc(_res(failed=1, failing=["a::1", "a::2"]))) == []
    assert G._targeted_ids(PYTEST_GATE, *_sc(_res(failed=1, errors=1, failing=["a::1"]))) == []
    assert G._targeted_ids(PYTEST_GATE, *_sc(_res(failed=0, failing=[]))) == []


@pytest.mark.parametrize("cmd", ["pytest -q 2>&1 | tee out.txt", "pytest -q > out.txt",
                                 "cd x; pytest", "a && pytest", "pytest $(echo -q)"])
def test_targeted_ids_need_an_appendable_cmd(cmd):
    res = _res(cmd=cmd, failed=1, failing=["a::1"])
    assert G._targeted_ids(PYTEST_GATE, *_sc(res)) == []
    # an explicit retry_cmd takes the ids wherever it wants them
    assert G._targeted_ids({**PYTEST_GATE, "retry_cmd": "make t IDS={failing}"}, *_sc(res)) == ["a::1"]


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


# ---- SPEC-0358: failed_first prelude ---------------------------------------------------------

FF_GATE = {"failed_first": True}


def _prev(failing=("tests/a.py::t1",), **kw):
    """A previous-round result entry as `last_gate["results"]` holds it."""
    n = len(failing)
    d = {"name": "pytest", "ok": False, "exit_code": 1, "timed_out": False, "cmd": "old cmd",
         "summary": {"format": "pytest", "passed": 1, "failed": n, "errors": 0, "skipped": 0,
                     "failing": list(failing)}}
    d.update(kw)
    return d


def _ff(world, tmp_path, behaviours, previous=None, retries=2, **extra):
    g = _fake(tmp_path, behaviours, retries=retries, **{**FF_GATE, **extra})
    r = _ladder(world, tmp_path, g, previous=_prev() if previous is None else previous)
    return g, r


def test_ff_reproduced_ends_the_round_without_main_run(world, tmp_path):
    g, r = _ff(world, tmp_path, [PY_FAIL, "touch main-ran"])
    assert not r.ok and _kinds(r) == ["failed-first"] and r.attempts[0]["attempt"] == -1
    assert _args(tmp_path) == ["tests/a.py::t1"]                 # one run: the ids appended once
    assert r.summary["failing"] == ["tests/a.py::t1"] and r.duration_s == r.attempts[0]["duration_s"]
    assert r.log == "gates/001-pytest.ff.log" and (tmp_path / "g" / "001-pytest.ff.log").exists()
    assert not (tmp_path / "g" / "001-pytest.log").exists()
    assert r.cmd.endswith("tests/a.py::t1")
    side = json.loads((tmp_path / "g" / "001-pytest.ff.json").read_text())
    assert side["attempt"] == -1 and SC.record_problems(side, SC.GATE_KEYS, SC.GATE_SCHEMA) == []


def test_ff_uses_retry_cmd(world, tmp_path):
    g, r = _ff(world, tmp_path, [PY_FAIL],
               retry_cmd=f"sh {tmp_path}/fake/run.sh -k {{failing}}")
    assert _args(tmp_path) == ["-k tests/a.py::t1"] and _kinds(r) == ["failed-first"]


def test_ff_green_prelude_runs_main_and_ladder(world, tmp_path):
    g, r = _ff(world, tmp_path, [PY_PASS, PY_FAIL, PY_PASS, PY_PASS])
    assert _kinds(r) == ["failed-first", "full", "targeted", "full"] and r.ok
    assert r.flaky == ("tests/a.py::t1",)
    assert r.duration_s == round(sum(a["duration_s"] for a in r.attempts), 3)
    assert _args(tmp_path)[0] == "tests/a.py::t1" and _args(tmp_path)[1] == ""
    side = json.loads((tmp_path / "g" / "001-pytest.json").read_text())
    assert [a["kind"] for a in side["attempts"]][0] == "failed-first" and side["ok"] is True
    assert SC.record_problems(side, SC.GATE_KEYS, SC.GATE_SCHEMA) == []


def test_ff_green_prelude_lists_it_even_without_a_ladder_retry(world, tmp_path):
    g, r = _ff(world, tmp_path, [PY_PASS, PY_PASS], retries=0)
    assert r.ok and _kinds(r) == ["failed-first", "full"]
    assert r.duration_s == round(sum(a["duration_s"] for a in r.attempts), 3)
    t2 = tmp_path / "t2"
    t2.mkdir()
    g, r = _ff(world, t2, [PY_PASS, PY_FAIL], retries=0)
    assert not r.ok and _kinds(r) == ["failed-first", "full"]
    assert r.log == "gates/001-pytest.log"


@pytest.mark.parametrize("beh", ["exit 4", "exit 5"])
def test_ff_unresolved_exit_falls_through(world, tmp_path, beh):
    g, r = _ff(world, tmp_path, [beh, PY_PASS])
    assert r.ok and _kinds(r) == ["failed-first", "full"] and r.attempts[0]["exit_code"] in (4, 5)


def test_ff_timed_out_prelude_falls_through(world, tmp_path):
    g, r = _ff(world, tmp_path, ["sleep 10", PY_PASS], timeout_s=1)
    assert _kinds(r) == ["failed-first", "full"] and r.attempts[0]["timed_out"]
    assert r.ok                              # the main run's own verdict


def test_ff_interrupted_prelude_gives_interrupted_without_main_run(world, tmp_path, monkeypatch):
    g = _fake(tmp_path, ["exit 1", "touch main-ran"], retries=0, **FF_GATE)
    real, calls = G.run_gate, []

    def run(*a, **kw):                  # the stop lands as the prelude starts
        calls.append(kw.get("attempt"))
        monkeypatch.setattr(G, "_stopping", True)
        return real(*a, **kw)
    monkeypatch.setattr(G, "run_gate", run)
    r = _ladder(world, tmp_path, g, previous=_prev())
    assert calls == [-1] and r.interrupted and not r.ok and _kinds(r) == ["failed-first"]
    assert not (tmp_path / "main-ran").exists() and len(_args(tmp_path)) == 0


def test_ff_no_prelude_when_a_stop_is_already_requested(world, tmp_path):
    ev = threading.Event()
    ev.set()
    g = _fake(tmp_path, [PY_PASS], retries=0, **FF_GATE)
    r = _ladder(world, tmp_path, g, previous=_prev(), stop=ev)
    assert r.attempts == () and _args(tmp_path) == [""]


def test_ff_stop_after_green_prelude_is_interrupted(world, tmp_path, monkeypatch):
    ev = threading.Event()
    g = _fake(tmp_path, ["echo '=== 1 passed in 0.01s ==='; exit 0", "exit 1"], retries=0, **FF_GATE)
    real = G.run_gate

    def run(*a, **kw):
        res = real(*a, **kw)
        ev.set()
        return res
    monkeypatch.setattr(G, "run_gate", run)
    r = _ladder(world, tmp_path, g, previous=_prev(), stop=ev)
    assert r.interrupted and not r.ok and len(_args(tmp_path)) == 1


@pytest.mark.parametrize("previous", [
    _prev(ok=True),
    _prev(interrupted=True),
    _prev(timed_out=True),
    _prev(summary=None),
    _prev(failing=["a::1", "a::2"], summary={"format": "pytest", "passed": 0, "failed": 3,
                                              "errors": 0, "skipped": 0,
                                              "failing": ["a::1", "a::2"]}),
    _prev(summary={"format": "pytest", "passed": 0, "failed": 1, "errors": 1, "skipped": 0,
                   "failing": ["a::1"]}),
    None,
])
def test_ff_no_prelude_without_a_qualifying_previous(world, tmp_path, previous):
    g = _fake(tmp_path, [PY_PASS], retries=0, **FF_GATE)
    r = _ladder(world, tmp_path, g, previous=previous)
    assert r.ok and r.attempts == () and _args(tmp_path) == [""]


def test_ff_no_prelude_over_max_failing(world, tmp_path):
    ids = [f"t.py::t{i}" for i in range(G.gate_results.MAX_FAILING + 1)]
    g = _fake(tmp_path, [PY_PASS], retries=0, **FF_GATE)
    r = _ladder(world, tmp_path, g, previous=_prev(failing=ids))
    assert r.attempts == () and _args(tmp_path) == [""]


def test_ff_no_prelude_without_the_flag(world, tmp_path):
    g = _fake(tmp_path, [PY_PASS], retries=0)
    r = _ladder(world, tmp_path, g, previous=_prev())
    assert r.attempts == () and _args(tmp_path) == [""]


def test_ff_no_prelude_when_cmd_has_shell_operators(world, tmp_path):
    g = {**_gate("pytest", "echo '=== 1 passed in 0s ===' | cat"), "results": {"format": "pytest"},
         **FF_GATE}
    cfg, st, wt = world
    r = G.run_gate_with_retries(g, wt, G.gate_env(cfg, st), tmp_path / "g" / "001-pytest.log", {},
                                previous=_prev())
    assert r.attempts == () and r.ok


def test_ff_two_reproduced_rounds_in_a_row_never_repeat_the_ids(world, tmp_path):
    g = _fake(tmp_path, [PY_FAIL, PY_FAIL])
    g = {**g, **FF_GATE}
    r1 = _ladder(world, tmp_path, g, previous=_prev())
    # the second round's previous entry is the first prelude's result, as `last_gate` stores it
    stored = G.asdict(r1)
    r2 = _ladder(world, tmp_path, g, previous=stored)
    cmd = f"sh {tmp_path}/fake/run.sh"
    assert r1.cmd == r2.cmd == f"{cmd} tests/a.py::t1"
    assert _args(tmp_path) == ["tests/a.py::t1", "tests/a.py::t1"]


def test_ff_previous_cmd_is_never_used(world, tmp_path):
    g = _fake(tmp_path, [PY_FAIL], **FF_GATE)
    r = _ladder(world, tmp_path, g, previous=_prev(cmd="x | y"))      # operators in the old cmd
    assert _kinds(r) == ["failed-first"]


def test_ff_run_all_hands_previous_only_to_the_loop_tier(world, tmp_path):
    cfg, st, wt = world
    st = {**st, "last_gate": {"results": [_prev()]}}
    g = {**_fake(tmp_path, [PY_FAIL, PY_FAIL, PY_FAIL]), "when": "loop", **FF_GATE}
    for tier, want in (("loop", ["tests/a.py::t1"]), ("close", None), (None, None)):
        (tmp_path / "fake" / "args.log").unlink(missing_ok=True)
        (tmp_path / "fake" / "cnt").unlink(missing_ok=True)
        pg = {**g, "when": "always"} if tier is None else (g if tier == "loop" else {**g, "when": "close"})
        run = G.run_all(_project({**pg, "retries": 0}), st, cfg, seq=1, tier=tier)
        kinds = [a["kind"] for a in run.results[0].attempts]
        assert (kinds == ["failed-first"]) == (want is not None), tier
        if want is None:
            assert _args(tmp_path) == [""]


def test_ff_close_tier_last_gate_gives_no_prelude(world, tmp_path):
    cfg, st, wt = world
    close_prev = {**_prev(), "name": "pytest"}
    st = {**st, "last_gate": {"results": [close_prev]}}
    g = {**_fake(tmp_path, [PY_PASS]), "name": "pytest-affected", "when": "loop", "retries": 0,
         **FF_GATE}
    run = G.run_all(_project(g), st, cfg, seq=1, tier="loop")
    assert run.ok and run.results[0].attempts == () and _args(tmp_path) == [""]


# ---- SPEC-0359: selection report, head/tree, last_green_loop ---------------------------------

def test_gate_report_env_attach_and_cap(world):
    cfg, st, wt = world
    small = "echo '{\"head_sha\": \"abc\", \"tests\": [\"tests/test_a.py\"], \"full\": false}' " \
            "> \"$NW_GATE_REPORT\""
    big = f"python3 -c \"import json,os; open(os.environ['NW_GATE_REPORT'],'w').write(" \
          f"json.dumps({{'tests': ['x' * {G.REPORT_MAX_BYTES}]}}))\""
    run = G.run_all(_project(_gate("small", small), _gate("big", big), _gate("none", "true"),
                             _gate("env", "test -n \"$NW_GATE_REPORT\"")), st, cfg, seq=4)
    small_r, big_r, none_r, env_r = run.results
    assert env_r.ok, "NW_GATE_REPORT is set for every gate"
    assert small_r.report == {"head_sha": "abc", "tests": ["tests/test_a.py"], "full": False}
    assert (S.campaign_dir(cfg, CID) / "gates" / "004-small.report.json").is_file()
    assert big_r.ok and big_r.report is None            # over the cap: dropped
    assert none_r.report is None
    p = G.as_payload(run)
    assert p["results"][0]["report"]["head_sha"] == "abc"
    assert "report" not in p["results"][1] and "report" not in p["results"][2]


def test_stale_report_file_is_not_read(world):
    cfg, st, wt = world
    stale = S.campaign_dir(cfg, CID) / "gates" / "005-g.report.json"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"head_sha": "old"}')
    run = G.run_all(_project(_gate("g", "true")), st, cfg, seq=5)
    assert run.results[0].report is None


def test_head_and_tree_on_payload(world):
    import subprocess
    cfg, st, wt = world
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(["git", "-C", str(wt), *args], check=True, env=env, capture_output=True)
    head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD", "HEAD^{tree}"],
                          capture_output=True, text=True).stdout.split()
    run = G.run_all(_project(_gate("a", "true")), st, cfg, seq=1)
    assert (run.head, run.tree) == tuple(head)
    p = G.as_payload(run)
    assert p["head"] == head[0] and p["tree"] == head[1] and "reused" not in p
    assert G.as_payload(replace(run, reused=("pytest",)))["reused"] == ["pytest"]


def test_no_git_no_head_keys(world):
    cfg, st, wt = world
    p = G.as_payload(G.run_all(_project(_gate("a", "true")), st, cfg, seq=1))
    assert "head" not in p and "tree" not in p



def test_last_green_loop_set_by_green_and_cleared_by_red(tmp_path, monkeypatch):
    import test_agentic_queue as TQ
    cfg, cid, ctx = TQ.opened.__wrapped__(tmp_path, monkeypatch)
    rep = {"head_sha": "h1", "tests": [], "full": False}
    good = G.GateRun(1, True, (G.GateResult("pytest-affected", True, True, 0, False, 0.1,
                                            "gates/001-pytest-affected.log", "", report=rep),),
                     "2026-10-25T00:00:00", 0.1, head="h1", tree="t1")
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="GATING"))
    row, st = TQ._apply(ctx, cfg, cid, "gate", gate_run=good)
    assert row["to"] == "REVIEWING"
    lg = st["last_green_loop"]
    assert lg == st["last_gate"] and lg["head"] == "h1" and lg["tree"] == "t1"
    assert lg["results"][0]["report"] == rep
    bad = replace(good, seq=2, ok=False, results=(replace(good.results[0], ok=False,
                                                           exit_code=1),))
    S.write_state(cfg, cid, dict(st, state="GATING"))
    row, st = TQ._apply(ctx, cfg, cid, "gate", gate_run=bad)
    assert row["to"] == "TRIAGING" and st["last_green_loop"] is None
    S.write_state(cfg, cid, dict(st, state="GATING"))
    row, st = TQ._apply(ctx, cfg, cid, "gate", gate_run=good)
    assert st["last_green_loop"]["gate_seq"] == 1



def test_interrupted_green_loop_run_does_not_set_last_green_loop(tmp_path, monkeypatch):
    """`ok` with `interrupted` (cut short without a stop, so the run is applied) is not a green
    source: `last_green_loop` is cleared, not set to it."""
    import test_agentic_queue as TQ
    cfg, cid, ctx = TQ.opened.__wrapped__(tmp_path, monkeypatch)
    rep = {"head_sha": "h1", "tests": [], "full": False}
    good = G.GateRun(1, True, (G.GateResult("pytest-affected", True, True, 0, False, 0.1,
                                            "gates/001-pytest-affected.log", "", report=rep),),
                     "2026-10-25T00:00:00", 0.1, head="h1", tree="t1")
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="GATING"))
    _, st = TQ._apply(ctx, cfg, cid, "gate", gate_run=good)
    assert st["last_green_loop"]["gate_seq"] == 1
    cut = replace(good, seq=2, interrupted=True)
    assert cut.ok and not ctx.stop.is_set()
    S.write_state(cfg, cid, dict(st, state="GATING"))
    _, st = TQ._apply(ctx, cfg, cid, "gate", gate_run=cut)
    assert st["last_gate"]["gate_seq"] == 2
    assert st["last_green_loop"] is None
