"""SPEC-0319: `nw queue conductor-launch` / `steward-launch`."""
import json
import os
import shlex
from pathlib import Path

import pytest
from click.testing import CliRunner

from nightwright import rules
from nightwright.agentic import conductor_cost as CC
from nightwright.agentic import launch as L
from nightwright.agentic import runner as R
from nightwright.cli import cli

PRICE = {"input": 3.0, "output": 15.0}
KEYS = ("CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS,BASH_MAX_OUTPUT_LENGTH,"
        "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS")


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "nw").mkdir()
    (tmp_path / "foo").mkdir()
    return {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "config"),
            "_root": str(tmp_path / "projects"),
            "agentic": {"conductor": {"prices": {"claude-sonnet-5-5": PRICE}}}}


@pytest.fixture(autouse=True)
def projects(monkeypatch, tmp_path):
    def payload(cfg):
        return {"projects": [
            {"name": "Meta-Tools", "research": {"root": str(tmp_path / "nw")}},
            {"name": "Foo", "research": {"root": str(tmp_path / "foo")}},
            {"name": "Rootless", "research": {"root": None}}]}
    monkeypatch.setattr(rules, "projects_payload", payload)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg_holder[0])


cfg_holder = [None]


@pytest.fixture(autouse=True)
def _wire(cfg, monkeypatch, tmp_path):
    cfg_holder[0] = cfg
    monkeypatch.delenv("CLAUDE_CODE_EFFORT_LEVEL", raising=False)
    monkeypatch.delenv("NW_LAUNCH_KEYS", raising=False)
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
    assert plan["argv"] == ["claude", "-n", "conductor", "--session-id", sid,
                            "--allowedTools", *L.DEFAULTS["allowed_tools"],
                            "--disallowedTools", *L.DEFAULTS["disallowed_tools"],
                            "--model", "claude-sonnet-5-5", "--effort", "medium",
                            "--autocompact", "150000",
                            "Run the /nw-orient skill, then the /nw-conduct skill."]
    assert plan["prompt"] == L.CONDUCTOR_PROMPT
    assert plan["env"] == {"CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS": "120000",
                           "BASH_MAX_OUTPUT_LENGTH": "15000",
                           "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS": "10000",
                           "NW_LAUNCH_KEYS": KEYS}
    assert plan["cwd"] == str(tmp_path / "nw")
    r = invoke("conductor-launch", "--dry-run")
    assert r.exit_code == 0, r.output
    assert f"cwd: {tmp_path / 'nw'}" in r.output and "env:" in r.output
    assert "unset: CLAUDE_CODE_EFFORT_LEVEL" in r.output
    assert f"NW_LAUNCH_KEYS={KEYS}" in r.output
    argv_line = next(l for l in r.output.splitlines() if l.startswith("claude "))
    argv = shlex.split(argv_line)
    assert argv[:3] == ["claude", "-n", "conductor"] and argv[3] == "--session-id"
    assert argv[argv.index("--model"):][:6] == ["--model", "claude-sonnet-5-5", "--effort", "medium",
                          "--autocompact", "150000"]
    assert argv[-1] == L.CONDUCTOR_PROMPT
    assert "allowed_tools: Bash(git push --force-with-lease origin nw/*)" in r.output
    assert "disallowed_tools: Bash(git push * main)" in r.output
    assert sessions_lines(cfg) == []


def test_steward_plan_cwd_name_prompt(cfg, tmp_path):
    plan = L.build_plan(cfg, "steward", "Foo")
    assert plan["name"] == "steward:Foo" and plan["cwd"] == str(tmp_path / "foo")
    assert plan["argv"][-1] == "Run the /nw-orient skill, then the /nw-steward skill for project Foo."
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
    # inclusive floors / ceiling (review: a > vs >= slip must show)
    ("autocompact", "100k", 100000), ("autocompact", 100000, 100000),
    ("autocompact", 1000000, 1000000), ("autocompact", 1000001, 150000),
    ("idle_compact_min_tokens", 100000, 100000),
    ("bash_max_output_length", 1000, 1000), ("file_read_max_output_tokens", 1000, 1000),
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
    assert c["argv"][c["argv"].index("--model"):][:6] == ["--model", "claude-opus-5-5", "--effort", "high",
                               "--autocompact", "200000"]
    assert c["env"]["CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS"] == "150000"
    assert c["env"]["BASH_MAX_OUTPUT_LENGTH"] == "8000"
    assert c["env"]["CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS"] == "4000"
    s = L.build_plan(cfg, "steward", "Foo")
    assert s["argv"][s["argv"].index("--model"):][:6] == ["--model", "claude-opus-5-5", "--effort", "low",
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
    assert env["NW_LAUNCH_KEYS"] == KEYS


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
    monkeypatch.setenv("NW_LAUNCH_KEYS", KEYS)
    for k in KEYS.split(","):
        monkeypatch.setenv(k, "9")
    env = R._child_env(req)
    for k in (*KEYS.split(","), "NW_LAUNCH_KEYS", "CLAUDE_CODE_EFFORT_LEVEL"):
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
        env["NW_LAUNCH_KEYS"] = KEYS
    else:
        env.pop("NW_LAUNCH_KEYS", None)
    subprocess.run([sys.executable, "-m", "nightwright.agentic.runproc", str(run_dir)], env=env,
                   check=True, timeout=60)
    got = json.loads((run_dir / "result.json").read_text())
    assert got["UNRELATED"] == "u" and got["EXTRA"] == "1"
    assert got["BASH_MAX_OUTPUT_LENGTH"] == "1234"               # req.env wins
    if launched:
        for k in ("CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS", "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS",
                  "NW_LAUNCH_KEYS", "CLAUDE_CODE_EFFORT_LEVEL"):
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
    assert cwd == str(Path(cfg["data_dir"]).parent / "nw")
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
    assert "nw queue conductor-launch" in (root / "nw-conduct" / "SKILL.md").read_text()
    assert "nw queue steward-launch" in (root / "nw-steward" / "SKILL.md").read_text()


def test_steward_bad_override_falls_back_to_default(cfg):
    cfg["agentic"]["conductor"]["launch"] = {"effort": "high", "steward": {"effort": "ultra",
                                                                            "model": "claude-x"}}
    st = L.settings(cfg, "steward")
    assert st["effort"] == "medium" and st["model"] == "claude-x"
    assert L.settings(cfg)["effort"] == "high"


def test_exec_argv_and_env_equal_plan(cfg, ex, monkeypatch):
    monkeypatch.setenv("KEEP_ME", "1")
    seen = {}
    real = L.build_plan
    monkeypatch.setattr(L, "build_plan", lambda *a, **k: seen.setdefault("plan", real(*a, **k)))
    r = invoke("conductor-launch")
    assert r.exit_code == 0, r.output
    _, argv, env, _ = ex.calls[0]
    plan = seen["plan"]
    assert argv == plan["argv"]
    assert env == L.exec_env(plan) and env["KEEP_ME"] == "1"
    assert all(env[k] == v for k, v in plan["env"].items()) and len(plan["env"]) == 4


def test_registration_failure_is_click_error_without_exec(cfg, ex, monkeypatch):
    def boom(*a, **k):
        raise OSError("read-only data dir")
    monkeypatch.setattr(CC, "record_session", boom)
    r = invoke("conductor-launch")
    assert r.exit_code == 1 and "cannot register" in r.output and ex.calls == []


def test_exec_failure_is_click_error(cfg, monkeypatch):
    def bad_exec(*a):
        raise OSError(8, "Exec format error")
    monkeypatch.setattr(os, "execvpe", bad_exec)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    r = invoke("conductor-launch")
    assert r.exit_code == 1 and "cannot start claude" in r.output
    assert not isinstance(r.exception, OSError)


def _perm_args(argv, flag):
    i = argv.index(flag) + 1
    out = []
    while i < len(argv) and not argv[i].startswith("--") and argv[i] != argv[-1]:
        out.append(argv[i])
        i += 1
    return out


def test_default_permission_rules():
    a, d = L.DEFAULTS["allowed_tools"], L.DEFAULTS["disallowed_tools"]
    assert "Bash(git push origin nw/*)" in a and "Bash(gh pr create *)" in a
    assert not any(x.startswith("Bash(gh pr view") or "pr list" in x for x in a)
    assert "Bash(gh pr merge *)" in d and "Bash(git push * main)" in d


def test_plan_has_permission_flags_and_prompt_stays_last(cfg):
    plan = L.build_plan(cfg, "conductor")
    assert _perm_args(plan["argv"], "--allowedTools") == L.DEFAULTS["allowed_tools"]
    assert _perm_args(plan["argv"], "--disallowedTools") == L.DEFAULTS["disallowed_tools"]
    assert plan["argv"][-1] == L.CONDUCTOR_PROMPT
    assert plan["allowed_tools"] == L.DEFAULTS["allowed_tools"]
    r = invoke("conductor-launch", "--dry-run", "--json")
    data = json.loads(r.output)
    assert data["allowed_tools"] == L.DEFAULTS["allowed_tools"]
    assert data["disallowed_tools"] == L.DEFAULTS["disallowed_tools"]


def test_permission_config_replaces_defaults_and_steward_inherits(cfg):
    cfg["agentic"]["conductor"]["launch"] = {"allowed_tools": ["Bash(ls *)"],
                                             "disallowed_tools": ["Bash(rm *)"]}
    for plan in (L.build_plan(cfg, "conductor"), L.build_plan(cfg, "steward", "Foo")):
        assert _perm_args(plan["argv"], "--allowedTools") == ["Bash(ls *)"]
        assert _perm_args(plan["argv"], "--disallowedTools") == ["Bash(rm *)"]


def test_empty_permission_lists_add_no_flags(cfg):
    cfg["agentic"]["conductor"]["launch"] = {"allowed_tools": [], "disallowed_tools": []}
    plan = L.build_plan(cfg, "conductor")
    assert "--allowedTools" not in plan["argv"] and "--disallowedTools" not in plan["argv"]
    cfg["agentic"]["conductor"]["launch"] = {"allowed_tools": []}
    plan = L.build_plan(cfg, "conductor")
    assert "--allowedTools" not in plan["argv"] and "--disallowedTools" in plan["argv"]


@pytest.mark.parametrize("bad", ["Bash(ls)", [1], [""], None, {"a": 1}])
def test_bad_permission_values_fall_back(cfg, bad):
    cfg["agentic"]["conductor"]["launch"] = {"allowed_tools": bad}
    assert L.settings(cfg)["allowed_tools"] == L.DEFAULTS["allowed_tools"]


def test_resume_applies_permission_rules(cfg):
    _launch_record(cfg, "old")
    plan = L.build_plan(cfg, "conductor", resume=True)
    assert plan["argv"][3:5] == ["--resume", "old"]
    assert _perm_args(plan["argv"], "--allowedTools") == L.DEFAULTS["allowed_tools"]
    assert _perm_args(plan["argv"], "--disallowedTools") == L.DEFAULTS["disallowed_tools"]
