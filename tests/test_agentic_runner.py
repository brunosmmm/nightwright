"""SPEC-0174: headless persona runner — argv, result parsing, FakeRunner, select_runner.
Never calls the real `claude`: `subprocess.run` is monkeypatched."""
import json
import subprocess
from pathlib import Path

import pytest

from wt.agentic import runner as R
from wt.agentic.personas import GATE_TRIAGER_SCHEMA, IMPLEMENTER_SCHEMA


def _req(tmp_path, **over):
    base = dict(
        persona="implementer", prompt="do the thing", system_append="invariant",
        cwd=tmp_path / "wt", model="sonnet", effort="high", max_budget_usd=4.0,
        timeout_s=30, permission_mode="bypassPermissions", read_only=False,
        allowed_tools=("Bash(uv run wt *)",), add_dirs=(tmp_path / "wt",),
        json_schema=IMPLEMENTER_SCHEMA,
        env={"WT_CAMPAIGN": "C-1", "WT_DATA_DIR": str(tmp_path / "d"),
             "WT_CONFIG_DIR": str(tmp_path / "c"), "WT_SPEC_ID": "SPEC-0001",
             "WT_WORKTREE": str(tmp_path / "wt"), "WT_BASE": "main"},
        run_dir=tmp_path / "runs" / "001-implementer",
    )
    base.update(over)
    (tmp_path / "wt").mkdir(exist_ok=True)
    return R.RunRequest(**base)


def _result_json(**over):
    obj = {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 1200,
           "num_turns": 3, "result": "done", "session_id": "sess-123",
           "total_cost_usd": 0.0421,
           "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0,
                     "cache_creation_input_tokens": 0},
           "structured_output": {"status": "done", "summary": "implemented"}}
    obj.update(over)
    return json.dumps(obj)


def _capture_run(monkeypatch, stdout=None, returncode=0, raise_exc=None):
    calls = {}

    def fake_run(argv, **kw):
        calls["argv"], calls["kw"] = argv, kw
        if raise_exc is not None:
            raise raise_exc
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(R.subprocess, "run", fake_run)
    return calls


# ---- argv --------------------------------------------------------------------------------

def test_claude_runner_argv_and_stdin(tmp_path, monkeypatch):
    calls = _capture_run(monkeypatch, stdout=_result_json())
    req = _req(tmp_path)
    R.ClaudeRunner().run(req)
    argv, kw = calls["argv"], calls["kw"]
    assert argv[:4] == ["claude", "-p", "--output-format", "json"]
    i = argv.index("--json-schema")
    assert json.loads(argv[i + 1]) == IMPLEMENTER_SCHEMA
    for flag, val in [("--model", "sonnet"), ("--effort", "high"), ("--max-budget-usd", "4"),
                      ("--permission-mode", "bypassPermissions"), ("--permission-prompts", "none"),
                      ("--add-dir", str(tmp_path / "wt")), ("--append-system-prompt", "invariant"),
                      ("--allowedTools", "Bash(uv run wt *)")]:
        assert val in argv[argv.index(flag) + 1:argv.index(flag) + 2], flag
    assert "--no-session-persistence" in argv
    assert "--restricted" not in argv
    assert "--max-turns" not in argv
    assert kw["input"] == "do the thing"
    assert kw["cwd"] == str(tmp_path / "wt")
    assert kw["timeout"] == 30
    for k, v in req.env.items():
        assert kw["env"][k] == v


def test_read_only_persona_gets_restricted_and_no_effort_when_none(tmp_path, monkeypatch):
    calls = _capture_run(monkeypatch, stdout=_result_json())
    R.ClaudeRunner().run(_req(tmp_path, persona="gate_triager", read_only=True, effort=None,
                              permission_mode="dontAsk", allowed_tools=(),
                              json_schema=GATE_TRIAGER_SCHEMA))
    argv = calls["argv"]
    assert "--restricted" in argv and "--effort" not in argv and "--allowedTools" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"


# ---- parsing matrix ----------------------------------------------------------------------

def test_parse_structured_output_cost_usage_session(tmp_path, monkeypatch):
    _capture_run(monkeypatch, stdout=_result_json())
    res = R.ClaudeRunner().run(_req(tmp_path))
    assert res.verdict == {"status": "done", "summary": "implemented"}
    assert res.verdict_ok and not res.is_error and not res.timed_out
    assert res.cost_usd == pytest.approx(0.0421)
    assert res.usage["input_tokens"] == 10
    assert res.session_id == "sess-123"
    assert res.exit_code == 0


def test_parse_falls_back_to_result_text(tmp_path, monkeypatch):
    verdict = {"status": "partial", "summary": "half"}
    obj = json.loads(_result_json())
    del obj["structured_output"]
    obj["result"] = "```json\n" + json.dumps(verdict) + "\n```"
    _capture_run(monkeypatch, stdout=json.dumps(obj))
    res = R.ClaudeRunner().run(_req(tmp_path))
    assert res.verdict == verdict and res.verdict_ok


def test_parse_invalid_stdout_is_error(tmp_path, monkeypatch):
    _capture_run(monkeypatch, stdout="not json at all", returncode=0)
    res = R.ClaudeRunner().run(_req(tmp_path))
    assert res.verdict is None and not res.verdict_ok and res.is_error
    assert "not json" in res.result_text


def test_parse_wrong_enum_is_not_ok(tmp_path, monkeypatch):
    _capture_run(monkeypatch, stdout=_result_json(
        structured_output={"status": "maybe", "summary": "x"}))
    res = R.ClaudeRunner().run(_req(tmp_path))
    assert res.verdict == {"status": "maybe", "summary": "x"} and not res.verdict_ok


def test_parse_missing_required_key_is_not_ok(tmp_path, monkeypatch):
    _capture_run(monkeypatch, stdout=_result_json(structured_output={"status": "done"}))
    assert not R.ClaudeRunner().run(_req(tmp_path)).verdict_ok


def test_parse_missing_cost_is_none(tmp_path, monkeypatch):
    obj = json.loads(_result_json())
    del obj["total_cost_usd"]
    _capture_run(monkeypatch, stdout=json.dumps(obj))
    assert R.ClaudeRunner().run(_req(tmp_path)).cost_usd is None


def test_timeout_sets_timed_out(tmp_path, monkeypatch):
    _capture_run(monkeypatch, raise_exc=subprocess.TimeoutExpired(["claude"], 30, output="partial"))
    res = R.ClaudeRunner().run(_req(tmp_path))
    assert res.timed_out and res.is_error and res.exit_code == -1 and res.verdict is None


def test_is_error_flag_from_claude(tmp_path, monkeypatch):
    _capture_run(monkeypatch, stdout=_result_json(is_error=True), returncode=0)
    assert R.ClaudeRunner().run(_req(tmp_path)).is_error


def test_claude_missing_is_error(tmp_path, monkeypatch):
    _capture_run(monkeypatch, raise_exc=FileNotFoundError("claude"))
    res = R.ClaudeRunner().run(_req(tmp_path))
    assert res.is_error and res.exit_code == -1


def test_run_dir_files(tmp_path, monkeypatch):
    _capture_run(monkeypatch, stdout=_result_json())
    req = _req(tmp_path)
    res = R.ClaudeRunner().run(req)
    assert (req.run_dir / "prompt.md").read_text() == "do the thing"
    assert json.loads((req.run_dir / "result.json").read_text())["session_id"] == "sess-123"
    assert (req.run_dir / "stderr.log").exists()
    assert res.raw_path == req.run_dir / "result.json"


def test_conforms_nested_items():
    ok = {"disposition": "fix", "fix_brief": "b", "must_fix": [{"title": "t", "gate": "g"}]}
    bad = {"disposition": "fix", "fix_brief": "b", "must_fix": [{"gate": "g"}]}
    assert R._conforms(ok, GATE_TRIAGER_SCHEMA)
    assert not R._conforms(bad, GATE_TRIAGER_SCHEMA)
    assert not R._conforms({"disposition": "fix", "fix_brief": 3}, GATE_TRIAGER_SCHEMA)


# ---- FakeRunner --------------------------------------------------------------------------

def _script(tmp_path, entries):
    p = tmp_path / "script.json"
    p.write_text(json.dumps(entries))
    return p


def test_fake_runner_applies_edits_in_order_and_advances_across_instances(tmp_path):
    script = _script(tmp_path, [
        {"persona": "implementer", "edits": [{"path": "src/feat.py", "write": "X = 1\n"}],
         "verdict": {"status": "done", "summary": "wrote feat"}, "cost_usd": 0.5},
        {"persona": "gate_triager", "edits": [{"path": "src/feat.py", "append": "Y = 2\n"},
                                               {"path": "gone.txt", "delete": True}],
         "verdict": {"disposition": "fix", "fix_brief": "do it"}, "cost_usd": 0.1},
    ])
    (tmp_path / "wt").mkdir()
    (tmp_path / "wt" / "gone.txt").write_text("bye")
    r1 = R.FakeRunner(script).run(_req(tmp_path))
    assert r1.verdict_ok and r1.cost_usd == 0.5 and r1.verdict["summary"] == "wrote feat"
    assert (tmp_path / "wt" / "src" / "feat.py").read_text() == "X = 1\n"
    assert (tmp_path / "runs" / "001-implementer" / "prompt.md").exists()
    # a fresh instance (another process) continues from the cursor
    r2 = R.FakeRunner(script).run(_req(tmp_path, persona="gate_triager", read_only=True,
                                       json_schema=GATE_TRIAGER_SCHEMA,
                                       run_dir=tmp_path / "runs" / "002-gate_triager"))
    assert r2.verdict_ok and r2.verdict["disposition"] == "fix"
    assert (tmp_path / "wt" / "src" / "feat.py").read_text() == "X = 1\nY = 2\n"
    assert not (tmp_path / "wt" / "gone.txt").exists()
    with pytest.raises(ValueError, match="exhausted"):
        R.FakeRunner(script).run(_req(tmp_path, run_dir=tmp_path / "runs" / "003-implementer"))


def test_fake_runner_persona_mismatch_raises(tmp_path):
    script = _script(tmp_path, [{"persona": "gate_triager", "verdict": {}}])
    with pytest.raises(ValueError, match="asked for 'implementer'"):
        R.FakeRunner(script).run(_req(tmp_path))


def test_fake_runner_malformed_exit_code_timed_out(tmp_path):
    script = _script(tmp_path, [
        {"persona": "implementer", "verdict": {"status": "done", "summary": "s"}, "malformed": True},
        {"persona": "implementer", "verdict": {"status": "done", "summary": "s"}, "exit_code": 2},
        {"persona": "implementer", "verdict": {"status": "done", "summary": "s"}, "timed_out": True,
         "cost_usd": None},
    ])
    fr = R.FakeRunner(script)
    r1 = fr.run(_req(tmp_path, run_dir=tmp_path / "runs" / "001-implementer"))
    assert r1.verdict == {"garbage": True} and not r1.verdict_ok
    r2 = fr.run(_req(tmp_path, run_dir=tmp_path / "runs" / "002-implementer"))
    assert r2.exit_code == 2 and r2.is_error and r2.verdict_ok
    r3 = fr.run(_req(tmp_path, run_dir=tmp_path / "runs" / "003-implementer"))
    assert r3.timed_out and r3.is_error and r3.verdict is None and r3.cost_usd is None


def test_fake_runner_rejects_missing_or_non_list_script(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        R.FakeRunner(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    with pytest.raises(ValueError, match="JSON list"):
        R.FakeRunner(bad)


# ---- select_runner -----------------------------------------------------------------------

def test_select_runner(tmp_path, monkeypatch):
    script = _script(tmp_path, [])
    monkeypatch.delenv("WT_AGENTIC_RUNNER", raising=False)
    monkeypatch.delenv("WT_AGENTIC_FAKE_SCRIPT", raising=False)
    assert isinstance(R.select_runner({"agentic": {"runner": "claude"}}), R.ClaudeRunner)
    assert isinstance(R.select_runner({}), R.ClaudeRunner)
    fake = R.select_runner({"agentic": {"runner": "fake", "fake_script": str(script)}})
    assert isinstance(fake, R.FakeRunner)
    monkeypatch.setenv("WT_AGENTIC_RUNNER", "fake")
    monkeypatch.setenv("WT_AGENTIC_FAKE_SCRIPT", str(script))
    assert isinstance(R.select_runner({"agentic": {"runner": "claude"}}), R.FakeRunner)
    monkeypatch.delenv("WT_AGENTIC_FAKE_SCRIPT")
    with pytest.raises(ValueError, match="no script"):
        R.select_runner({"agentic": {"runner": "fake"}})
    monkeypatch.setenv("WT_AGENTIC_RUNNER", "bogus")
    with pytest.raises(ValueError, match="unknown agentic.runner"):
        R.select_runner({})


def test_fake_runner_pre_shell_and_shell_run_in_cwd_with_env(tmp_path):      # SPEC-0221
    (tmp_path / "wt").mkdir()
    script = _script(tmp_path, [
        {"persona": "implementer", "pre_shell": ["echo pre-$WT_BASE > order.txt"],
         "edits": [{"path": "edit.txt", "write": "x"}],
         "shell": ["echo post >> order.txt", "test -f edit.txt && echo saw-edit >> order.txt"],
         "verdict": {"status": "done", "summary": "s"}, "cost_usd": 0.1}])
    r = R.FakeRunner(script).run(_req(tmp_path))
    assert r.verdict_ok
    assert (tmp_path / "wt" / "order.txt").read_text().split() == ["pre-main", "post", "saw-edit"]
