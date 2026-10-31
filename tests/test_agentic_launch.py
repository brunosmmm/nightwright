"""SPEC-0319: `wt queue conductor-launch` / `steward-launch`."""
import json
import os
import shlex
from pathlib import Path

import pytest
from click.testing import CliRunner

from wt import rules
from wt.agentic import conductor_cost as CC
from wt.agentic import launch as L
from wt.agentic import runner as R
from wt.cli import cli

PRICE = {"input": 3.0, "output": 15.0}
KEYS = ("CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS,BASH_MAX_OUTPUT_LENGTH,"
        "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS")


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "wt").mkdir()
    (tmp_path / "foo").mkdir()
    return {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "config"),
            "_root": str(tmp_path / "projects"),
            "agentic": {"conductor": {"prices": {"claude-sonnet-5-5": PRICE}}}}


@pytest.fixture(autouse=True)
def projects(monkeypatch, tmp_path):
    def payload(cfg):
        return {"projects": [
            {"name": "Meta-Tools", "research": {"root": str(tmp_path / "wt")}},
            {"name": "Foo", "research": {"root": str(tmp_path / "foo")}},
            {"name": "Rootless", "research": {"root": None}}]}
    monkeypatch.setattr(rules, "projects_payload", payload)
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg_holder[0])


cfg_holder = [None]


@pytest.fixture(autouse=True)
def _wire(cfg, monkeypatch, tmp_path):
    cfg_holder[0] = cfg
    monkeypatch.delenv("CLAUDE_CODE_EFFORT_LEVEL", raising=False)
    monkeypatch.delenv("WT_LAUNCH_KEYS", raising=False)
    monkeypatch.chdir(tmp_path)


def invoke(*args):
    return CliRunner().invoke(cli, ["queue", *args])


class Exec:
    def __init__(self, cfg, monkeypatch):
        self.calls = []
        self.registered = None

        def fake(file, argv, env):
            self.registered = CC.sessions(cfg, "conductor") + CC.sessions(cfg, "steward:Foo")
            self.calls.append((file, argv, env, os.getcwd()))
        monkeypatch.setattr(os, "execvpe", fake)
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")


@pytest.fixture
def ex(cfg, monkeypatch):
    return Exec(cfg, monkeypatch)


def sessions_lines(cfg):
    p = Path(cfg["data_dir"])
    return [json.loads(l) for f in p.rglob("sessions.jsonl") for l in f.read_text().splitlines()]


def test_default_plan_argv_and_env(cfg, tmp_path):
    plan = L.build_plan(cfg, "conductor")
    sid = plan["session_id"]
    assert plan["argv"] == ["claude", "-n", "conductor", "--session-id", sid, "--model",
                            "claude-sonnet-5-5", "--effort", "medium", "--autocompact",
                            "150000", "Run the /wt-orient skill, then the /wt-conduct skill."]
    assert plan["prompt"] == L.CONDUCTOR_PROMPT
    assert plan["env"] == {"CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS": "120000",
                           "BASH_MAX_OUTPUT_LENGTH": "15000",
                           "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS": "10000",
                           "WT_LAUNCH_KEYS": KEYS}
    assert plan["cwd"] == str(tmp_path / "wt")
    r = invoke("conductor-launch", "--dry-run")
    assert r.exit_code == 0, r.output
    assert f"cwd: {tmp_path / 'wt'}" in r.output and "env:" in r.output
    assert "unset: CLAUDE_CODE_EFFORT_LEVEL" in r.output
    assert f"WT_LAUNCH_KEYS={KEYS}" in r.output
    argv_line = next(l for l in r.output.splitlines() if l.startswith("claude "))
    argv = shlex.split(argv_line)
    assert argv[:3] == ["claude", "-n", "conductor"] and argv[3] == "--session-id"
    assert argv[5:11] == ["--model", "claude-sonnet-5-5", "--effort", "medium",
                          "--autocompact", "150000"]
    assert argv[-1] == L.CONDUCTOR_PROMPT
    assert sessions_lines(cfg) == []


def test_steward_plan_cwd_name_prompt(cfg, tmp_path):
    plan = L.build_plan(cfg, "steward", "Foo")
    assert plan["name"] == "steward:Foo" and plan["cwd"] == str(tmp_path / "foo")
    assert plan["argv"][-1] == "Run the /wt-orient skill, then the /wt-steward skill for project Foo."
    assert plan["argv"][2] == "steward:Foo"
    r = invoke("steward-launch", "Foo", "--dry-run")
    assert r.exit_code == 0 and "project Foo." in r.output


def test_steward_unknown_project_lists_known(cfg):
    r = invoke("steward-launch", "Nope", "--dry-run")
    assert r.exit_code != 0 and "Foo" in r.output and "Meta-Tools" in r.output
    r = invoke("steward-launch", "Rootless", "--dry-run")
    assert r.exit_code != 0 and "no repo root" in r.output


@pytest.mark.parametrize("key,value,expect", [
    ("model", "claude-opus-5-5", "claude-opus-5-5"), ("model", "", "claude-sonnet-5-5"),
    ("model", 5, "claude-sonnet-5-5"), ("model", True, "claude-sonnet-5-5"),
    ("effort", "high", "high"), ("effort", "ultra", "medium"), ("effort", True, "medium"),
    ("autocompact", "99k", 150000), ("autocompact", "2m", 150000),
    ("autocompact", "auto", "auto"), ("autocompact", "150k", 150000),
    ("autocompact", 150000, 150000), ("autocompact", "200k", 200000),
    ("autocompact", "1m", 1000000), ("autocompact", True, 150000),
    ("autocompact", -5, 150000), ("autocompact", float("nan"), 150000),
    ("autocompact", "junk", 150000),
    ("idle_compact_min_tokens", 200000, 200000), ("idle_compact_min_tokens", 99999, 120000),
    ("idle_compact_min_tokens", True, 120000), ("idle_compact_min_tokens", "x", 120000),
    ("idle_compact_min_tokens", -1, 120000), ("idle_compact_min_tokens", float("nan"), 120000),
    ("bash_max_output_length", 20000, 20000), ("bash_max_output_length", 999, 15000),
    ("bash_max_output_length", float("inf"), 15000), ("bash_max_output_length", True, 15000),
    ("file_read_max_output_tokens", 5000, 5000), ("file_read_max_output_tokens", -2, 10000),
    ("file_read_max_output_tokens", "5000", 5000), ("file_read_max_output_tokens", False, 10000),
])
def test_settings_overrides_and_bad_values(cfg, key, value, expect):
    cfg["agentic"]["conductor"]["launch"] = {key: value}
    assert L.settings(cfg)[key] == expect
    # a bad value must never leak into the plan
    plan = L.build_plan(cfg, "conductor")
    assert plan["model"] == L.settings(cfg)["model"]


def test_settings_change_argv_and_env_and_steward_override(cfg):
    cfg["agentic"]["conductor"]["prices"]["claude-opus-5-5"] = PRICE
    cfg["agentic"]["conductor"]["launch"] = {
        "model": "claude-opus-5-5", "effort": "high", "autocompact": "200k",
        "idle_compact_min_tokens": 150000, "bash_max_output_length": 8000,
        "file_read_max_output_tokens": 4000, "steward": {"effort": "low", "bash_max_output_length": 9000}}
    c = L.build_plan(cfg, "conductor")
    assert c["argv"][5:11] == ["--model", "claude-opus-5-5", "--effort", "high",
                               "--autocompact", "200000"]
    assert c["env"]["CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS"] == "150000"
    assert c["env"]["BASH_MAX_OUTPUT_LENGTH"] == "8000"
    assert c["env"]["CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS"] == "4000"
    s = L.build_plan(cfg, "steward", "Foo")
    assert s["argv"][5:11] == ["--model", "claude-opus-5-5", "--effort", "low",
                               "--autocompact", "200000"]
    assert s["env"]["BASH_MAX_OUTPUT_LENGTH"] == "9000"
    assert s["env"]["CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS"] == "4000"


def test_ambient_effort_env_removed(cfg, ex, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "high")
    monkeypatch.setenv("KEEP_ME", "1")
    r = invoke("conductor-launch")
    assert r.exit_code == 0, r.output
    env = ex.calls[0][2]
    assert "CLAUDE_CODE_EFFORT_LEVEL" not in env and env["KEEP_ME"] == "1"
    assert env["WT_LAUNCH_KEYS"] == KEYS


def _req(tmp_path, env):
    return R.RunRequest(persona="p", prompt="x", system_append="", cwd=tmp_path, model="m",
                        effort="high", max_budget_usd=1.0, timeout_s=1, permission_mode="x",
                        read_only=False, allowed_tools=(), add_dirs=(), json_schema={},
                        env=env, run_dir=tmp_path)


def test_runner_child_env_drops_launch_keys(tmp_path, monkeypatch):
    req = _req(tmp_path, {"EXTRA": "1"})
    monkeypatch.setenv("UNRELATED", "u")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "high")        # kept outside a launched session
    assert R._child_env(req) == {**os.environ, "EXTRA": "1"}      # not a launched session
    assert R._child_env(req)["CLAUDE_CODE_EFFORT_LEVEL"] == "high"
    monkeypatch.setenv("WT_LAUNCH_KEYS", KEYS)
    for k in KEYS.split(","):
        monkeypatch.setenv(k, "9")
    env = R._child_env(req)
    for k in (*KEYS.split(","), "WT_LAUNCH_KEYS", "CLAUDE_CODE_EFFORT_LEVEL"):
        assert k not in env
    assert env["UNRELATED"] == "u" and env["EXTRA"] == "1"
    env = R._child_env(_req(tmp_path, {"BASH_MAX_OUTPUT_LENGTH": "1234"}))   # explicit req.env wins
    assert env["BASH_MAX_OUTPUT_LENGTH"] == "1234"


@pytest.mark.parametrize("launched", [True, False])
def test_runproc_wrapper_child_env(tmp_path, monkeypatch, launched):
    """The production path: the detached wrapper builds the real child's env (SPEC-0319)."""
    import subprocess
    import sys
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    dump = "import json,os;print(json.dumps(dict(os.environ)))"
    (run_dir / "launch.json").write_text(json.dumps({
        "argv": [sys.executable, "-c", dump], "cwd": str(tmp_path),
        "env": {"BASH_MAX_OUTPUT_LENGTH": "1234", "EXTRA": "1"}}))
    (run_dir / "request.json").write_text(json.dumps({"timeout_s": 60}))
    (run_dir / "prompt.md").write_text("x")
    env = {**os.environ, "UNRELATED": "u", "CLAUDE_CODE_EFFORT_LEVEL": "high",
           "CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS": "120000",
           "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS": "10000"}
    env.pop("BASH_MAX_OUTPUT_LENGTH", None)
    if launched:
        env["WT_LAUNCH_KEYS"] = KEYS
    else:
        env.pop("WT_LAUNCH_KEYS", None)
    subprocess.run([sys.executable, "-m", "wt.agentic.runproc", str(run_dir)], env=env,
                   check=True, timeout=60)
    got = json.loads((run_dir / "result.json").read_text())
    assert got["UNRELATED"] == "u" and got["EXTRA"] == "1"
    assert got["BASH_MAX_OUTPUT_LENGTH"] == "1234"               # req.env wins
    if launched:
        for k in ("CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS", "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS",
                  "WT_LAUNCH_KEYS", "CLAUDE_CODE_EFFORT_LEVEL"):
            assert k not in got
    else:
        assert got["CLAUDE_CODE_EFFORT_LEVEL"] == "high"
        assert got["CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS"] == "120000"


def test_launch_registers_once_before_exec(cfg, ex):
    r = invoke("conductor-launch")
    assert r.exit_code == 0, r.output
    _, argv, _, cwd = ex.calls[0]
    sid = argv[argv.index("--session-id") + 1]
    assert [(x["session_id"], x["source"], x["reader"], x["cwd"]) for x in ex.registered] == [
        (sid, "launch", "conductor", cwd)]
    assert cwd == str(Path(cfg["data_dir"]).parent / "wt")
    assert len(sessions_lines(cfg)) == 1
    # resume (transcript exists) adds nothing; neither does the follower's registration
    d = Path(cfg["_root"]) / "-slug"
    d.mkdir(parents=True)
    (d / f"{sid}.jsonl").write_text("{}\n")
    assert invoke("conductor-launch", "--resume").exit_code == 0
    assert CC.record_session(cfg, "conductor", {}, source="follower", session_id=sid) is None
    assert len(sessions_lines(cfg)) == 1


def test_steward_registers_under_steward_reader(cfg, ex):
    assert invoke("steward-launch", "Foo").exit_code == 0
    assert [x["reader"] for x in sessions_lines(cfg)] == ["steward:Foo"]


def test_unpriced_model_refused(cfg, ex):
    cfg["agentic"]["conductor"]["prices"] = {}
    for args in (["conductor-launch"], ["steward-launch", "Foo"]):
        r = invoke(*args)
        assert r.exit_code != 0 and "agentic.conductor.prices" in r.output
    assert sessions_lines(cfg) == [] and ex.calls == []


def test_unpriced_model_dry_run_prints_then_fails(cfg, ex):
    cfg["agentic"]["conductor"]["prices"] = {}
    r = invoke("conductor-launch", "--dry-run")
    assert r.exit_code == 1
    assert r.output.index("claude -n conductor") < r.output.index("no price for model")
    assert sessions_lines(cfg) == [] and ex.calls == []


def _launch_record(cfg, sid, source="launch", reader="conductor", transcript=True):
    CC.record_session(cfg, reader, {}, source=source, session_id=sid)
    if transcript:
        d = Path(cfg["_root"]) / "-slug"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{sid}.jsonl").write_text("{}\n")


def test_resume_selects_latest_launch_record(cfg, ex):
    _launch_record(cfg, "old")
    _launch_record(cfg, "new")
    plan = L.build_plan(cfg, "conductor", resume=True)
    assert plan["session_id"] == "new" and plan["resumed"]
    assert plan["argv"][3:5] == ["--resume", "new"] and "--session-id" not in plan["argv"]
    r = invoke("conductor-launch", "--resume", "--dry-run")
    assert "session: new (resumed)" in r.output


def test_resume_ignores_follower_and_manual(cfg):
    _launch_record(cfg, "f", source="follower")
    _launch_record(cfg, "m", source="manual")
    r = invoke("conductor-launch", "--resume")
    assert r.exit_code != 0 and "no launched session to resume" in r.output


def test_resume_missing_transcript_fails(cfg):
    _launch_record(cfg, "gone", transcript=False)
    r = invoke("conductor-launch", "--resume")
    assert r.exit_code != 0 and "no launched session to resume" in r.output


def test_exec_chdir_and_env_overlay(cfg, ex, tmp_path):
    r = invoke("steward-launch", "Foo")
    assert r.exit_code == 0, r.output
    file, argv, env, cwd = ex.calls[0]
    assert file == "claude" and argv[0] == "claude" and cwd == str(tmp_path / "foo")
    assert env["BASH_MAX_OUTPUT_LENGTH"] == "15000" and argv[2] == "steward:Foo"


def test_missing_claude_binary_fails_before_register(cfg, ex, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    r = invoke("conductor-launch")
    assert r.exit_code != 0 and "claude" in r.output
    assert sessions_lines(cfg) == [] and ex.calls == []


def test_json_requires_dry_run(cfg, ex):
    r = invoke("conductor-launch", "--json")
    assert r.exit_code != 0 and ex.calls == []
    r = invoke("conductor-launch", "--dry-run", "--json")
    assert r.exit_code == 0
    plan = json.loads(r.output)
    assert plan["name"] == "conductor" and plan["env_remove"] == ["CLAUDE_CODE_EFFORT_LEVEL"]


def test_skills_mention_launcher():
    root = Path(__file__).resolve().parent.parent / "skills"
    assert "wt queue conductor-launch" in (root / "wt-conduct" / "SKILL.md").read_text()
    assert "wt queue steward-launch" in (root / "wt-steward" / "SKILL.md").read_text()
