"""SPEC-0442: Cursor SDK persona child, cost line, tail, and health check. No network."""
import json
import signal
import sys
import types
from io import StringIO
from types import SimpleNamespace as NS

import pytest

from nightwright.agentic import cursor_exec as CE
from nightwright.agentic import runproc as RP
from nightwright.agentic import runner as R
from nightwright.agentic import tail as TL
from nightwright.agentic import worker as W
from nightwright.agentic.personas import IMPLEMENTER_SCHEMA

VERDICT = {"status": "done", "summary": "implemented"}
KEY = "crsr_test_key_do_not_leak"


def _layout(tmp_path, **over):
    d = tmp_path / "run"
    d.mkdir()
    doc = {"model": "grok-4.7-high", "read_only": False, "cwd": str(tmp_path),
           "persona": "implementer"}
    doc.update(over)
    (d / "request.json").write_text(json.dumps(doc), encoding="utf-8")
    (d / "prompt.md").write_text("do the thing\n\nReply with a single JSON object",
                                 encoding="utf-8")
    return d


def _messages():
    verdict = json.dumps(VERDICT)
    return [
        NS(type="system", subtype="", model=NS(id="grok-4.7-high", params=()),
           tools=("Shell",), agent_id="agent-1", run_id="run-1"),
        NS(type="thinking", text="hidden plan", thinking_duration_ms=4,
           agent_id="agent-1", run_id="run-1"),
        NS(type="tool_call", name="Shell", status="running",
           args={"command": "uv run pytest -q\necho second"}, call_id="c1",
           agent_id="agent-1", run_id="run-1"),
        NS(type="assistant", agent_id="agent-1", run_id="run-1",
           message=NS(content=(NS(type="text", text=verdict),))),
        NS(type="usage", agent_id="agent-1", run_id="run-1",
           usage=NS(input_tokens=10, output_tokens=4, cache_read_tokens=1,
                    cache_write_tokens=2, total_tokens=17, reasoning_tokens=3)),
    ]


class _Run:
    def __init__(self, status="finished"):
        self.status = status
        self.cancelled = False

    def messages(self):
        yield from _messages()

    def wait(self):
        return NS(status=self.status, id="run-1", result=json.dumps(VERDICT),
                  duration_ms=12,
                  usage=NS(input_tokens=10, output_tokens=4, cache_read_tokens=1,
                           cache_write_tokens=2, total_tokens=17),
                  error_code=None, error_message=None)

    def cancel(self):
        self.cancelled = True


class _Agent:
    def __init__(self, options, *, cost=None, usage_error=None, status="finished"):
        self.options = options
        self.agent_id = "agent-1"
        self.run = _Run(status)
        self.closed = False
        self.cost = cost
        self.usage_error = usage_error
        self.prompt = None

    def send(self, prompt):
        self.prompt = prompt
        return self.run

    def get_usage(self):
        if self.usage_error:
            raise RuntimeError(self.usage_error)
        return NS(cost=self.cost)

    def close(self):
        self.closed = True


def _run(tmp_path, *, cost=None, usage_error=None, status="finished", read_only=False,
         argv=None, key=KEY, agent=True):
    d = _layout(tmp_path, read_only=read_only)
    out, err = StringIO(), StringIO()
    seen = {}

    def create(options):
        seen["options"] = options
        if not agent:
            raise AssertionError("agent should not be created")
        return _Agent(options, cost=cost, usage_error=usage_error, status=status)

    code = CE.execute(d, argv=argv, environ={"CURSOR_API_KEY": key} if key is not None else {},
                      out=out, err=err, agent_create=create if agent else None)
    return code, out.getvalue(), err.getvalue(), seen


def test_stream_result_cost_and_parse(tmp_path):
    code, text, err, seen = _run(
        tmp_path, cost=NS(raw_cost_cents=12.5, charged_cents=10.0))
    assert code == 0 and err == ""
    assert seen["options"]["read_only"] is False
    assert seen["options"]["setting_sources"] == ["project"]
    assert seen["options"]["model"] == {
        "id": "grok-4.7", "params": [{"id": "reasoning_effort", "value": "high"}]}
    assert seen["options"]["api_key"] == KEY
    assert "sandbox" not in seen["options"] and "auto_review" not in seen["options"]
    assert KEY not in text
    lines = [json.loads(ln) for ln in text.split("\n") if ln]
    assert [ln["type"] for ln in lines] == [
        "system", "thinking", "tool_call", "assistant", "usage", "result"]
    assert lines[0]["subtype"] == "init" and lines[0]["model"] == "grok-4.7-high"
    assert lines[0]["tools"] == ["Shell"]
    assert lines[1]["text"] == "hidden plan"
    assert lines[3]["message"]["content"][0]["text"] == json.dumps(VERDICT)
    result = lines[-1]
    assert result["total_cost_usd"] == 0.125
    assert result["cost"] == {"raw_cost_cents": 12.5, "charged_cents": 10.0}
    assert result["session_id"] == "agent-1" and result["sdk_run_id"] == "run-1"
    assert result["usage"]["cache_read_input_tokens"] == 1
    assert result["usage"]["cache_creation_input_tokens"] == 2
    parsed = R.parse_result(text, IMPLEMENTER_SCHEMA, exit_code=0)
    assert parsed["verdict"] == VERDICT and parsed["verdict_ok"]
    assert parsed["cost_usd"] == 0.125 and parsed["session_id"] == "agent-1"
    assert not parsed["is_error"]
    rendered = []
    r = TL.Renderer()
    for ln in text.split("\n"):
        rendered.extend(t.plain for t in r.feed_line(ln))
    blob = "\n".join(rendered)
    assert "hidden plan" not in blob
    assert "→ " in blob and "Shell" in blob and "uv run pytest -q" in blob
    assert "echo second" not in blob
    assert "tokens in 10" in blob
    assert "cost $0.13" in blob or "cost $0.12" in blob


def test_feature_unavailable_leaves_cost_null(tmp_path):
    code, text, _, _ = _run(tmp_path, usage_error="[feature_unavailable] not for this account")
    assert code == 0
    result = json.loads(text.strip().split("\n")[-1])
    assert result["is_error"] is False
    assert result["total_cost_usd"] is None and "cost" not in result
    assert result["cost_error"] == "feature_unavailable"
    parsed = R.parse_result(text, IMPLEMENTER_SCHEMA, exit_code=0)
    assert parsed["cost_usd"] is None and not parsed["is_error"]


def test_charged_cents_when_raw_is_zero(tmp_path):
    _, text, _, _ = _run(tmp_path, cost=NS(raw_cost_cents=0, charged_cents=40))
    result = json.loads(text.strip().split("\n")[-1])
    assert result["total_cost_usd"] == 0.4
    assert result["cost"]["charged_cents"] == 40


def test_reported_zero_is_a_real_zero(tmp_path):
    _, text, _, _ = _run(tmp_path, cost=NS(raw_cost_cents=0, charged_cents=0))
    result = json.loads(text.strip().split("\n")[-1])
    assert result["total_cost_usd"] == 0.0
    assert R.parse_result(text, IMPLEMENTER_SCHEMA, exit_code=0)["cost_usd"] == 0.0


def test_read_only_passes_no_tools(tmp_path):
    _, _, _, seen = _run(tmp_path, read_only=True, argv=["--add-dir", str(tmp_path / "extra")])
    assert seen["options"]["read_only"] is True
    assert seen["options"]["dirs"] == [str(tmp_path / "extra")]


def test_missing_key_does_not_call_the_agent(tmp_path):
    code, text, err, seen = _run(tmp_path, key=None)
    assert code == 1 and "options" not in seen
    assert "CURSOR_API_KEY is not set" in text and "CURSOR_API_KEY is not set" in err
    assert json.loads(text)["is_error"] is True


def test_missing_package(tmp_path, monkeypatch):
    real = __import__

    def boom(name, *args, **kwargs):
        if name == "cursor_sdk":
            raise ImportError("no module named cursor_sdk")
        return real(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", boom)
    d = _layout(tmp_path)
    out, err = StringIO(), StringIO()
    code = CE.execute(d, environ={"CURSOR_API_KEY": KEY}, out=out, err=err)
    assert code == 1
    assert "not installed" in out.getvalue() and KEY not in out.getvalue()


def test_sdk_error_scrubs_the_key(tmp_path):
    d = _layout(tmp_path)
    out, err = StringIO(), StringIO()

    class Boom:
        def send(self, prompt):
            raise RuntimeError(f"rejected {KEY}")

        def close(self):
            self.closed = True

    agent = Boom()
    code = CE.execute(d, environ={"CURSOR_API_KEY": KEY}, out=out, err=err,
                      agent_create=lambda options: agent)
    assert code == 1 and agent.closed
    assert KEY not in out.getvalue() and "[redacted]" in out.getvalue()
    assert KEY not in err.getvalue()


def test_read_lines_and_result_line_keep_line_separators(tmp_path):
    tool = json.dumps({"type": "tool_call", "name": "Read", "status": "completed",
                       "args": {"path": "a.py"}, "result": "one\u2028two"})
    result = json.dumps({"type": "result", "is_error": False, "result": "ok"})
    text = tool + "\n" + result + "\n"
    path = tmp_path / "stream.jsonl"
    path.write_text(text, encoding="utf-8")
    lines, _ = TL.read_lines(path, 0)
    assert len(lines) == 2
    assert json.loads(lines[0])["type"] == "tool_call"
    assert "unparseable" not in TL.Renderer().feed_line(lines[0])[0].plain
    assert json.loads(RP._result_line(text))["type"] == "result"


def test_tail_tool_call_thinking_and_tool_count():
    r = TL.Renderer()
    assert r.feed({"type": "thinking", "text": "secret plan"}) == []
    running = r.feed({"type": "tool_call", "name": "Shell", "status": "running",
                      "args": {"command": "uv run pytest -q\necho second"}})
    assert "Shell" in running[0].plain and "uv run pytest -q" in running[0].plain
    assert "echo second" not in running[0].plain
    done = r.feed({"type": "tool_call", "name": "Read", "status": "completed",
                   "result": {"success": {"content": "SECRET\n" * 30}}})
    assert "ok" in done[0].plain and "SECRET" not in done[0].plain
    err = r.feed({"type": "tool_call", "name": "Shell", "status": "error",
                  "result": {"exitCode": 1, "stderr": "boom"}})
    assert "error" in err[0].plain and "exit 1" in err[0].plain
    bare = r.feed({"type": "system", "subtype": "init", "model": "gpt"})[0].plain
    assert "gpt" in bare and "0 tools" not in bare
    counted = r.feed({"type": "system", "subtype": "init", "model": {"id": "gpt"},
                      "tools": ["a", "b"]})[0].plain
    assert "2 tools" in counted and "gpt" in counted


def test_sdk_tool_results_render_the_value_not_the_json():
    r = TL.Renderer()
    edit = r.feed({"type": "tool_call", "name": "edit", "status": "completed", "result": {
        "status": "success", "value": {
            "linesAdded": 1, "linesRemoved": 1,
            "diffString": "--- a//home/bruno/secret/file.md\n+++ b//home/bruno/secret/file.md\n"
                          "@@ -1 +1 @@\n-status: accepted\n+status: in-progress\n"}}})
    plain = [t.plain for t in edit]
    assert plain[0] == "  ← +1 −1"
    assert "    -status: accepted" in plain and "    +status: in-progress" in plain
    assert "secret" not in "\n".join(plain) and "diffString" not in "\n".join(plain)
    huge = "\n".join(f"+line {i}" for i in range(40))
    clipped = r.feed({"type": "tool_call", "name": "edit", "status": "completed", "result": {
        "status": "success", "value": {"linesAdded": 40, "linesRemoved": 0, "diffString": huge}}})
    assert any(t.plain.startswith("    … ") and t.plain.endswith("more lines") for t in clipped)
    shell = r.feed({"type": "tool_call", "name": "shell", "status": "completed", "result": {
        "status": "success", "value": {"exitCode": 1, "stdout": "", "stderr": "Error: nw: refusing\n"}}})
    assert shell[0].plain == "  ← exit 1"
    assert "refusing" in shell[1].plain and "exitCode" not in shell[1].plain
    read = r.feed({"type": "tool_call", "name": "read", "status": "completed", "result": {
        "status": "success", "value": {"content": "SECRET", "totalLines": 347, "fileSize": 20}}})
    assert read[0].plain == "  ← 347 lines · 20 bytes" and "SECRET" not in read[0].plain
    glob = r.feed({"type": "tool_call", "name": "glob", "status": "completed", "result": {
        "status": "success", "value": {"files": ["tests/history_scale.py"], "totalFiles": 1}}})
    assert glob[0].plain == "  ← 1 file: tests/history_scale.py"
    grep = r.feed({"type": "tool_call", "name": "grep", "status": "completed", "result": {
        "status": "success", "value": {"workspaceResults": {"/work": {"output": {"matches": [
            {"file": "./src/nightwright/agentic/tail.py"}]}}}}}})
    assert grep[0].plain == "  ← 1 file: src/nightwright/agentic/tail.py"
    todos = r.feed({"type": "tool_call", "name": "updateTodos", "status": "running", "args": {
        "todos": [{"content": "Read the spec", "status": "inProgress"}]}})
    assert "1 items" in todos[0].plain and "todos" not in todos[0].plain.split("updateTodos", 1)[-1]


def test_shell_output_deltas_are_kept_and_shown_once(tmp_path):
    delta = NS(type="shell-output-delta", event={"type": "stdout", "data": "collected 3 items\n"})
    frame = NS(sdk_message=None, interaction_update=delta)
    skipped = NS(sdk_message=None, interaction_update=NS(type="text-delta", text="I'll"))
    assert CE.frame_dicts(frame) == [
        {"type": "shell_output", "event": {"type": "stdout", "data": "collected 3 items\n"}}]
    assert CE.frame_dicts(skipped) == []

    class _Live:
        def events(self):
            yield frame
            yield NS(type="tool_call", name="shell", status="completed", call_id="c",
                     args={"command": "uv run pytest -q"},
                     result={"status": "success", "value": {
                         "exitCode": 0, "stdout": "collected 3 items\n", "stderr": ""}})

        def wait(self):
            return NS(status="finished", id="run-1", result="ok", duration_ms=1,
                      usage=None, error_code=None, error_message=None)

        def cancel(self):
            pass

    class _Agent2(_Agent):
        def send(self, prompt):
            self.prompt = prompt
            self.run = _Live()
            return self.run

    d = _layout(tmp_path)
    out, err = StringIO(), StringIO()

    def create(options):
        return _Agent2(options)

    assert CE.execute(d, environ={"CURSOR_API_KEY": KEY}, out=out, err=err,
                      agent_create=create) == 0
    kinds = [json.loads(ln)["type"] for ln in out.getvalue().splitlines() if ln]
    assert "shell_output" in kinds and "text-delta" not in kinds
    r = TL.Renderer()
    shown = []
    for ln in out.getvalue().splitlines():
        if ln:
            shown.extend(t.plain for t in r.feed_line(ln))
    assert "  │ collected 3 items" in shown
    assert shown.count("  ← exit 0") == 1
    assert not any("collected 3 items" in ln and ln.strip().startswith("←") for ln in shown)
    fresh = TL.Renderer()
    body = fresh.feed({"type": "tool_call", "name": "shell", "status": "completed",
                       "result": {"status": "success", "value": {
                           "exitCode": 1, "stdout": "", "stderr": "boom"}}})
    assert [t.plain for t in body] == ["  ← exit 1", "    boom"]


def test_probe_hides_the_key_and_worker_check(monkeypatch):
    monkeypatch.setitem(sys.modules, "cursor_sdk", types.ModuleType("cursor_sdk"))
    monkeypatch.setenv("CURSOR_API_KEY", KEY)
    rec = RP.probe_cursor_sdk()
    assert rec["ok"] and KEY not in json.dumps(rec)
    check = W.cursor_check()
    assert check["ok"] and KEY not in json.dumps(check)
    monkeypatch.delenv("CURSOR_API_KEY")
    missing = RP.probe_cursor_sdk()
    assert not missing["ok"] and "CURSOR_API_KEY" in missing["error"]
    with pytest.raises(FileNotFoundError, match="CURSOR_API_KEY"):
        RP.ensure_cursor_persona()


def test_main_usage_without_a_run_dir():
    prev = signal.getsignal(signal.SIGTERM)
    try:
        assert CE.main([]) == 2
    finally:
        signal.signal(signal.SIGTERM, prev)
