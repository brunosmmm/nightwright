"""SPEC-0362: stream-json capture of persona runs and `nw campaign tail`.

Built on real `claude` 2.1.316 stream-json samples (`tests/fixtures/stream_json/`, captured
2026-11-01, paths sanitized). The fake `claude` (`tests/fixtures/fake_claude.py`) replays the
tool-use sample in stream mode; no real agent is ever started."""
import io
import json
import shutil
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.console import Console

from nightwright.agentic import runner as R
from nightwright.agentic import runproc as RP
from nightwright.agentic import store as S
from nightwright.agentic import tail as TL
from nightwright.agentic.personas import IMPLEMENTER_SCHEMA
from nightwright.cli import cli
from waits import join, until

FIX = Path(__file__).parent / "fixtures"
FAKE = FIX / "fake_claude.py"
OK_SAMPLE = FIX / "stream_json" / "ok_text.jsonl"
TOOL_SAMPLE = FIX / "stream_json" / "read_tool_schema.jsonl"
SCHEMA = {"type": "object", "required": ["status", "summary"],
          "properties": {"status": {"type": "string", "enum": ["done", "partial"]},
                         "summary": {"type": "string"}}}
PRICES = {"agentic": {"conductor": {"prices": {"claude-haiku": {
    "input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write_5m": 1.25,
    "cache_write_1h": 2.0}}}}}


@pytest.fixture(autouse=True)
def fake_claude(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "claude").symlink_to(FAKE)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    monkeypatch.setenv("NW_RUNPROC_GRACE_S", "1")
    monkeypatch.setattr(RP, "GRACE_S", 1.0)


def mkreq(tmp_path, name="001-implementer", **env):
    (tmp_path / "nw").mkdir(exist_ok=True)
    return R.RunRequest(
        persona="implementer", prompt="do it", system_append="", cwd=tmp_path / "nw",
        model="m", effort=None, max_budget_usd=1.0, timeout_s=60,
        permission_mode="acceptEdits", read_only=False, allowed_tools=(), add_dirs=(),
        json_schema=IMPLEMENTER_SCHEMA, env=env, run_dir=tmp_path / "c" / "runs" / name)


def lines_of(path):
    return [ln for ln in Path(path).read_text().splitlines() if ln.strip()]


def capture():
    buf = io.StringIO()
    return Console(file=buf, width=200, color_system=None, force_terminal=False), buf


# ---- the real samples ----------------------------------------------------------------------

def test_real_samples_have_the_shape_we_rely_on():
    for path in (OK_SAMPLE, TOOL_SAMPLE):
        evs = [json.loads(ln) for ln in lines_of(path)]
        assert evs[-1]["type"] == "result" and "total_cost_usd" in evs[-1]
        assert any(e["type"] == "system" and e.get("subtype") == "init" for e in evs)
    evs = [json.loads(ln) for ln in lines_of(TOOL_SAMPLE)]
    blocks = [b for e in evs if e["type"] == "assistant" for b in e["message"]["content"]]
    assert [b["name"] for b in blocks if b["type"] == "tool_use"] == ["Read", "StructuredOutput"]
    assert evs[-1]["structured_output"]["status"] == "done"


# ---- argv / launch ---------------------------------------------------------------------------

def test_build_argv_stream_and_json_modes(tmp_path):
    req = mkreq(tmp_path)
    s, j = R.build_argv(req), R.build_argv(req, stream=False)
    assert s[:5] == ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    assert j[:4] == ["claude", "-p", "--output-format", "json"] and "--verbose" not in j
    assert "--no-session-persistence" in s and "--no-session-persistence" in j
    assert s[5:] == j[4:]


def test_select_runner_kill_switch():
    assert R.select_runner({}).stream is True
    assert R.select_runner({"agentic": {"stream_runs": False}}).stream is False


def test_json_mode_launch_has_no_stream_flag(tmp_path):
    req = mkreq(tmp_path)
    R.ClaudeRunner(stream=False).run(req)
    launch = json.loads((req.run_dir / "launch.json").read_text())
    assert "stream" not in launch and launch["argv"][3] == "json"
    assert not (req.run_dir / "stream.jsonl").exists()
    assert "stream" not in json.loads((req.run_dir / "run.json").read_text())["files"]


# ---- parse_result ----------------------------------------------------------------------------

def test_parse_jsonl_equals_single_json():
    text = TOOL_SAMPLE.read_text()
    result_line = lines_of(TOOL_SAMPLE)[-1]
    a = R.parse_result(text, SCHEMA, exit_code=0)
    b = R.parse_result(result_line, SCHEMA, exit_code=0)
    assert a == b
    assert a["verdict_ok"] and a["verdict"]["status"] == "done" and not a["is_error"]
    assert a["cost_usd"] == pytest.approx(json.loads(result_line)["total_cost_usd"])
    assert a["session_id"] and a["usage"]["output_tokens"] > 0


def test_parse_jsonl_with_garbage_and_truncated_line():
    good = OK_SAMPLE.read_text()
    text = "garbage line\n" + good + '{"type":"assistant","message":{"con'
    p = R.parse_result(text, {}, exit_code=0)
    assert not p["is_error"] and p["result_text"] == "ok"


def test_parse_stream_without_result_is_error():
    text = "".join(ln + "\n" for ln in lines_of(TOOL_SAMPLE)[:-1])
    p = R.parse_result(text, SCHEMA, exit_code=0)
    assert p["is_error"] and p["verdict"] is None and p["cost_usd"] is None
    one_event = lines_of(TOOL_SAMPLE)[0]               # a single non-result event: not a result
    assert R.parse_result(one_event, SCHEMA, exit_code=0)["is_error"]


# ---- the wrapper -----------------------------------------------------------------------------

def test_stream_grows_before_exit_and_result_json_is_the_result_event(tmp_path):
    req = mkreq(tmp_path, FAKE_CLAUDE_SLEEP_S="60", FAKE_CLAUDE_COST="0.31")
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("res", R.ClaudeRunner().run(req)))
    t.start()
    stream = req.run_dir / "stream.jsonl"
    assert until(lambda: stream.is_file() and '"tool_use"' in stream.read_text())
    assert not (req.run_dir / "exit.json").exists()           # still running
    (req.cwd / "release").write_text("")
    join(t)
    res = out["res"]
    launch = json.loads((req.run_dir / "launch.json").read_text())
    assert launch["stream"] is True and launch["argv"][3] == "stream-json"
    last = lines_of(stream)[-1]
    assert json.loads(last)["type"] == "result"
    assert (req.run_dir / "result.json").read_text().strip() == last
    assert res.verdict_ok and res.cost_usd == pytest.approx(0.31) and not res.is_error
    rj = json.loads((req.run_dir / "run.json").read_text())
    assert rj["files"]["stream"] == "stream.jsonl"


def test_stream_run_equals_json_run_of_same_output(tmp_path):
    s_req = mkreq(tmp_path, "001-implementer", FAKE_CLAUDE_COST="0.42")
    R.ClaudeRunner().run(s_req)
    result_line = (s_req.run_dir / "result.json").read_text().strip()
    j_req = mkreq(tmp_path, "002-implementer", FAKE_CLAUDE_STDOUT=result_line)
    R.ClaudeRunner(stream=False).run(j_req)
    a = json.loads((s_req.run_dir / "run.json").read_text())
    b = json.loads((j_req.run_dir / "run.json").read_text())
    for k in ("verdict", "verdict_ok", "cost_usd", "cost_estimated", "usage", "session_id",
              "is_error", "exit_code", "timed_out", "interrupted"):
        assert a[k] == b[k], k
    assert R.collect(s_req).result_text == R.collect(j_req).result_text
    assert a["usage"]["output_tokens"] > 0 and a["cost_usd"] == pytest.approx(0.42)


def _old_run_dir(tmp_path, result_text, name="001-implementer"):
    """A run dir exactly as the pre-SPEC-0362 code left it."""
    d = tmp_path / "c" / "runs" / name
    d.mkdir(parents=True)
    (d / "prompt.md").write_text("do it")
    RP.write_json_atomic(d / "request.json", {"schema": "nw.run.request.v1",
                         "persona": "implementer", "model": "sonnet", "max_budget_usd": 4.0,
                         "timeout_s": 60, "cwd": str(tmp_path), "json_schema": SCHEMA,
                         "started_at": "2026-10-31T10:00:00-04:00"})
    RP.write_json_atomic(d / "launch.json", {"argv": ["claude", "-p", "--output-format", "json"],
                                             "cwd": str(tmp_path), "env": {}})
    (d / "result.json").write_text(result_text)
    (d / "stderr.log").write_text("warn: one\nwarn: two\n")
    RP.write_json_atomic(d / "exit.json", {"exit_code": 0, "timed_out": False, "killed_by": None,
                                           "duration_s": 12.0})
    return d


def test_old_format_run_collects_exactly_as_before(tmp_path):
    line = lines_of(TOOL_SAMPLE)[-1]
    d = _old_run_dir(tmp_path, line)
    (d / "stream.jsonl").write_text("not consulted\n")       # json mode never reads a stream
    req = R.rebuild_request(d)
    res = R.collect(req)
    expected = R.parse_result(line, SCHEMA, exit_code=0)
    assert res.verdict == expected["verdict"] and res.verdict_ok
    assert res.cost_usd == expected["cost_usd"] and res.session_id == expected["session_id"]
    assert RP.run_output(d) == line


def test_old_format_run_with_unparseable_result_still_errors(tmp_path):
    d = _old_run_dir(tmp_path, "Error: something broke\n")
    res = R.collect(R.rebuild_request(d))
    assert res.is_error and "something broke" in res.result_text


def _stream_run_dir(tmp_path, stream_text, name="001-implementer"):
    d = _old_run_dir(tmp_path, "", name)
    (d / "result.json").unlink()
    launch = json.loads((d / "launch.json").read_text())
    RP.write_json_atomic(d / "launch.json", {**launch, "stream": True})
    (d / "stream.jsonl").write_text(stream_text)
    return d


def test_wrapper_died_result_is_extracted_from_stream(tmp_path):
    d = _stream_run_dir(tmp_path, TOOL_SAMPLE.read_text())
    res = R.collect(R.rebuild_request(d))
    assert (d / "result.json").read_text().strip() == lines_of(TOOL_SAMPLE)[-1]
    assert res.verdict_ok and not res.is_error


def test_killed_mid_stream_is_an_error_with_the_stream_tail(tmp_path):
    partial = "".join(ln + "\n" for ln in lines_of(TOOL_SAMPLE)[:4]) + '{"type":"user","mess'
    d = _stream_run_dir(tmp_path, partial)
    res = R.collect(R.rebuild_request(d))
    assert res.is_error and res.verdict is None
    assert '{"type":"user","mess' in res.result_text
    assert not (d / "result.json").exists()


def test_stream_run_killed_by_timeout(tmp_path):
    req = mkreq(tmp_path, FAKE_CLAUDE_SLEEP_S="60")
    req = R.RunRequest(**{**req.__dict__, "timeout_s": 1})
    res = R.ClaudeRunner().run(req)
    assert res.timed_out and res.is_error
    assert "tool_use" in (req.run_dir / "stream.jsonl").read_text()


# ---- the viewer ------------------------------------------------------------------------------

def _rendered(cfg, run_dir, **kw):
    con, buf = capture()
    assert TL.tail(cfg, run_dir, out=con, **kw) == 0
    return buf.getvalue()


def test_render_real_tool_sample(tmp_path):
    d = _stream_run_dir(tmp_path, TOOL_SAMPLE.read_text())
    out = _rendered(PRICES, d)
    assert "run 001-implementer · persona implementer · model sonnet · budget $4.00" in out
    assert "● session · model claude-haiku" in out
    assert "→ Read /work/proj/notes.txt" in out
    assert "← 4 lines: 1\talpha" in out or "← 4 lines: 1 alpha" in out
    assert "→ StructuredOutput" in out and "Structured output provided successfully" in out
    assert "$ ~" in out and "/ $4.00 budget (est., turn 1)" in out
    assert "■ result: success · cost $0.01 / $4.00 · " in out and "verdict done" in out
    assert "[incomplete last line]" not in out and "[unparseable line]" not in out


def test_render_unpriced_shows_tokens(tmp_path):
    d = _stream_run_dir(tmp_path, OK_SAMPLE.read_text())
    out = _rendered({}, d)
    assert "  ok" in out
    assert "tokens in 2 · out 4 · cache 23.7k (turn 1; no price for claude-opus-5-5)" in out


def test_render_tool_summaries_and_errors():
    r = TL.Renderer(cwd="/w", budget_usd=2.0)
    ev = {"type": "assistant", "message": {"id": "m1", "model": "x", "usage": {"input_tokens": 1},
          "content": [
              {"type": "tool_use", "id": "a", "name": "Edit",
               "input": {"file_path": "/w/src/a.py", "old_string": "x", "new_string": "y"}},
              {"type": "tool_use", "id": "b", "name": "Bash",
               "input": {"command": "uv run pytest -q\necho second"}},
              {"type": "tool_use", "id": "c", "name": "Read",
               "input": {"file_path": "/w/b.py", "offset": 10, "limit": 5}},
              {"type": "tool_use", "id": "d", "name": "Grep", "input": {"pattern": "def x", "path": "/w/src"}},
              {"type": "thinking", "thinking": "hmm"}]}}
    text = "\n".join(t.plain for t in r.feed(ev))
    assert "→ Edit src/a.py" in text and "→ Bash uv run pytest -q" in text
    assert "echo second" not in text and "→ Read b.py offset=10 limit=5" in text
    assert "→ Grep 'def x' in src" in text and "· thinking (3 chars)" in text
    res = r.feed({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "b", "is_error": True, "content": "Exit code 1\nboom"}]}})
    assert res[0].plain == "  ← error: Exit code 1"
    err = r.feed({"type": "result", "subtype": "error_max_budget_usd", "is_error": True,
                  "total_cost_usd": 2.1, "num_turns": 30, "duration_ms": 190000})
    assert err[0].plain == ("■ result: error_max_budget_usd · cost $2.10 / $2.00 · 30 turns · "
                            "3m10s · verdict —")


def test_unparseable_and_incomplete_lines(tmp_path):
    d = _stream_run_dir(tmp_path, "not json\n" + "".join(
        ln + "\n" for ln in lines_of(TOOL_SAMPLE)[:4]) + '{"type":"user","mess')
    out = _rendered({}, d)
    assert "[unparseable line]" in out and "[incomplete last line]" in out
    assert "■ run ended without a result event" in out


def test_raw_echoes_lines(tmp_path):
    d = _stream_run_dir(tmp_path, TOOL_SAMPLE.read_text())
    out = _rendered({}, d, raw=True)
    assert out.splitlines() == lines_of(TOOL_SAMPLE)


def test_old_run_says_so_and_shows_result(tmp_path):
    d = _old_run_dir(tmp_path, lines_of(TOOL_SAMPLE)[-1])
    out = _rendered({}, d)
    assert "no stream.jsonl: this run was launched without stream capture" in out
    assert "■ result: success" in out and "verdict done" in out
    assert "stderr (last 2 lines):" in out and "warn: two" in out
    assert _rendered({}, d, raw=True).strip() == lines_of(TOOL_SAMPLE)[-1]


def test_follow_picks_up_appended_lines_and_ends_with_the_run(tmp_path):
    lines = lines_of(TOOL_SAMPLE)
    d = _stream_run_dir(tmp_path, "")
    (d / "exit.json").unlink()
    RP.write_json_atomic(d / "proc.json", {"wrapper_pid": __import__("os").getpid(),
                                           "wrapper_start_ticks": None})
    con, buf = capture()
    t = threading.Thread(target=lambda: TL.tail({}, d, follow=True, out=con, poll_s=0.02))
    t.start()
    stream = d / "stream.jsonl"
    with open(stream, "a") as f:
        f.write(lines[3] + "\n")                      # the Read tool_use
        f.flush()
        assert until(lambda: "→ Read" in buf.getvalue())
        half = len(lines[5]) // 2                     # the tool_result, written in two parts
        f.write(lines[5][:half])
        f.flush()
        assert until(lambda: buf.getvalue().count("\n") >= 3)
        assert "←" not in buf.getvalue()
        f.write(lines[5][half:] + "\n" + lines[-1] + "\n")
        f.flush()
    assert until(lambda: "■ result: success" in buf.getvalue())
    assert t.is_alive()                               # the run has not ended
    RP.write_json_atomic(d / "exit.json", {"exit_code": 0})
    join(t)
    out = buf.getvalue()
    assert out.index("→ Read") < out.index("← 4 lines") < out.index("■ result")


def test_follow_on_old_running_run_waits_then_shows_result(tmp_path):
    d = _old_run_dir(tmp_path, lines_of(TOOL_SAMPLE)[-1])
    (d / "exit.json").unlink()
    RP.write_json_atomic(d / "proc.json", {"wrapper_pid": __import__("os").getpid(),
                                           "wrapper_start_ticks": None})
    con, buf = capture()
    t = threading.Thread(target=lambda: TL.tail({}, d, follow=True, out=con, poll_s=0.02))
    t.start()
    assert until(lambda: "waiting for the run to end" in buf.getvalue())
    RP.write_json_atomic(d / "exit.json", {"exit_code": 0})
    join(t)
    assert "■ result: success" in buf.getvalue()


def test_stream_stats(tmp_path):
    st = TL.stream_stats(TOOL_SAMPLE, PRICES)
    assert st["turns"] == 2 and st["done"] and st["cost_usd"] > 0 and st["est_usd"] > 0
    partial = tmp_path / "p.jsonl"
    partial.write_text("".join(ln + "\n" for ln in lines_of(TOOL_SAMPLE)[:4]))
    st = TL.stream_stats(partial, {})
    assert st["turns"] == 1 and not st["done"] and st["est_usd"] is None


# ---- CLI ---------------------------------------------------------------------------------------

def _cli_cfg(tmp_path):
    (tmp_path / "data").mkdir(exist_ok=True)
    return {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "cfg")}


def run_cli(cfg, monkeypatch, args):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    return CliRunner().invoke(cli, args)


def test_cli_tail_default_newest_run_and_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    cfg = _cli_cfg(tmp_path)
    cid = "SPEC-0001-20261101-abcd"
    S.create_campaign(cfg, {"id": cid, "spec_id": "SPEC-0001", "state": "IMPLEMENTING"})
    runs = S.runs_dir(cfg, cid)
    r = run_cli(cfg, monkeypatch, ["campaign", "tail", cid])
    assert r.exit_code != 0 and "no runs yet" in r.output
    old = _old_run_dir(tmp_path, lines_of(TOOL_SAMPLE)[-1], "001-implementer")
    new = _stream_run_dir(tmp_path, TOOL_SAMPLE.read_text(), "002-conformance")
    runs.mkdir(parents=True)
    shutil.move(str(old), runs / old.name)
    shutil.move(str(new), runs / new.name)
    r = run_cli(cfg, monkeypatch, ["campaign", "tail", cid])
    assert r.exit_code == 0, r.output
    assert "run 002-conformance" in r.output and "→ Read" in r.output
    r = run_cli(cfg, monkeypatch, ["campaign", "tail", cid, "--run", "001"])
    assert r.exit_code == 0 and "no stream.jsonl" in r.output
    r = run_cli(cfg, monkeypatch, ["campaign", "tail", cid, "--run", "009"])
    assert r.exit_code != 0 and "no run '009'" in r.output
    r = run_cli(cfg, monkeypatch, ["campaign", "tail", "NOPE"])
    assert r.exit_code != 0 and "no campaign" in r.output
