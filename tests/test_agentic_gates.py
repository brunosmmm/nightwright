"""SPEC-0173: deterministic gate executor + `wt queue gates`. All gates are `sh -c` scripts in a
tmp worktree; the data dir is a tmp dir."""
import json
import os
import time
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
