"""Headless persona runner (SPEC-0174): the only place that knows `claude`'s command line.

A persona run is one fresh `claude -p` with a structured verdict (`--json-schema`), a dollar
budget, a wall-clock timeout and an explicit permission mode. The runner saves the prompt and
the raw result under the campaign's `runs/` dir and turns the JSON result into a `RunResult`
(verdict, cost, usage, session id). `FakeRunner` replays a scripted JSON file (edits inside the
worktree + canned verdicts) so the whole loop is testable offline; `select_runner(cfg)` picks
one from config or the `WT_AGENTIC_RUNNER` env var.

Compared with the older `meetings.refresh_meetings` call: there is a timeout, the exit code is
checked, and the cost is read."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n\s*```\s*$", re.DOTALL)
_TAIL_CHARS = 2000


@dataclass(frozen=True)
class RunRequest:
    persona: str
    prompt: str
    system_append: str
    cwd: Path
    model: str
    effort: str | None
    max_budget_usd: float
    timeout_s: int
    permission_mode: str
    read_only: bool
    allowed_tools: tuple[str, ...]
    add_dirs: tuple[Path, ...]
    json_schema: dict
    env: dict[str, str]
    run_dir: Path


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    timed_out: bool
    interrupted: bool
    duration_s: float
    is_error: bool
    cost_usd: float | None
    usage: dict = field(default_factory=dict)
    session_id: str | None = None
    verdict: dict | None = None
    verdict_ok: bool = False
    result_text: str = ""
    raw_path: Path | None = None


class PersonaRunner(Protocol):
    def run(self, req: RunRequest) -> RunResult: ...


# ---- schema conformance (no jsonschema at runtime) --------------------------------------------

_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool,
          "number": (int, float), "integer": int}


def _conforms(value, schema) -> bool:
    """Small subset of JSON Schema: `type`, `required`, `enum`, `properties`, `items`. Enough to
    reject a verdict with a missing key or an out-of-vocabulary disposition."""
    if not isinstance(schema, dict):
        return True
    typ = schema.get("type")
    if typ in _TYPES and not isinstance(value, _TYPES[typ]):
        return False
    if typ == "integer" and isinstance(value, bool):
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if isinstance(value, dict):
        for key in schema.get("required") or ():
            if key not in value:
                return False
        for key, sub in (schema.get("properties") or {}).items():
            if key in value and not _conforms(value[key], sub):
                return False
    if isinstance(value, list) and "items" in schema:
        return all(_conforms(item, schema["items"]) for item in value)
    return True


# ---- argv + parsing -----------------------------------------------------------------------

def build_argv(req: RunRequest) -> list[str]:
    """`claude -p` command line for a run; the prompt itself goes on stdin."""
    argv = ["claude", "-p", "--output-format", "json",
            "--json-schema", json.dumps(req.json_schema, separators=(",", ":")),
            "--model", req.model]
    if req.effort:
        argv += ["--effort", req.effort]
    argv += ["--max-budget-usd", f"{req.max_budget_usd:g}",
             "--permission-mode", req.permission_mode,
             "--permission-prompts", "none",
             "--no-session-persistence"]
    for d in req.add_dirs:
        argv += ["--add-dir", str(d)]
    argv += ["--append-system-prompt", req.system_append]
    for tool in req.allowed_tools:
        argv += ["--allowedTools", tool]
    if req.read_only:
        argv.append("--restricted")
    return argv


def _tail(text: str, n: int = _TAIL_CHARS) -> str:
    text = text or ""
    return text[-n:]


def _loads_lenient(text):
    """Parse JSON that may be wrapped in a ``` fence; None when it is not JSON."""
    if not isinstance(text, str) or not text.strip():
        return None
    m = _FENCE_RE.match(text)
    candidate = m.group(1) if m else text
    try:
        return json.loads(candidate)
    except ValueError:
        return None


def parse_result(stdout: str, schema: dict, *, exit_code: int) -> dict:
    """Fields of a `RunResult` derived from a `--output-format json` stdout.

    `verdict` = top-level `structured_output`, else the `result` text parsed as JSON;
    `verdict_ok` = it is an object conforming to `schema`. Unparseable stdout ⇒ error."""
    obj = _loads_lenient(stdout)
    if isinstance(obj, list):          # defensive: a stream-json dump — take the result event
        obj = next((e for e in reversed(obj) if isinstance(e, dict) and e.get("type") == "result"),
                   None)
    if not isinstance(obj, dict):
        return {"verdict": None, "verdict_ok": False, "cost_usd": None, "usage": {},
                "session_id": None, "is_error": True, "result_text": _tail(stdout)}
    verdict = obj.get("structured_output")
    if verdict is None:
        verdict = _loads_lenient(obj.get("result"))
    if not isinstance(verdict, dict):
        verdict = None
    cost = obj.get("total_cost_usd")
    try:
        cost = float(cost) if cost is not None else None
    except (TypeError, ValueError):
        cost = None
    result_text = obj.get("result") if isinstance(obj.get("result"), str) else ""
    return {
        "verdict": verdict,
        "verdict_ok": verdict is not None and _conforms(verdict, schema),
        "cost_usd": cost,
        "usage": obj.get("usage") if isinstance(obj.get("usage"), dict) else {},
        "session_id": obj.get("session_id"),
        "is_error": bool(obj.get("is_error", exit_code != 0)),
        "result_text": _tail(result_text),
    }


def _save_inputs(req: RunRequest) -> None:
    req.run_dir.mkdir(parents=True, exist_ok=True)
    (req.run_dir / "prompt.md").write_text(req.prompt, encoding="utf-8")


def _child_env(req: RunRequest) -> dict:
    return {**os.environ, **req.env}


# ---- the real thing ---------------------------------------------------------------------------

class ClaudeRunner:
    """Spawn `claude -p` per `build_argv`; prompt on stdin; `subprocess.run(timeout=…)`."""

    def run(self, req: RunRequest) -> RunResult:
        _save_inputs(req)
        argv = build_argv(req)
        raw_path = req.run_dir / "result.json"
        started = time.monotonic()
        timed_out = False
        try:
            r = subprocess.run(argv, input=req.prompt, capture_output=True, text=True,
                               cwd=str(req.cwd), env=_child_env(req), timeout=req.timeout_s)
            stdout, stderr, code = r.stdout or "", r.stderr or "", r.returncode
        except subprocess.TimeoutExpired as e:  # subprocess.run already killed the child
            timed_out = True
            stdout = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = (e.stderr or b"").decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
            code = -1
        except OSError as e:               # claude not installed, etc.
            stdout, stderr, code = "", str(e), -1
        duration = time.monotonic() - started
        raw_path.write_text(stdout, encoding="utf-8")
        (req.run_dir / "stderr.log").write_text(stderr, encoding="utf-8")
        parsed = parse_result(stdout, req.json_schema, exit_code=code)
        if timed_out or code == -1:
            parsed["is_error"] = True
        return RunResult(exit_code=code, timed_out=timed_out, interrupted=False,
                         duration_s=round(duration, 3), raw_path=raw_path, **parsed)


# ---- offline stand-in ----------------------------------------------------------------------------

class FakeRunner:
    """Replay a JSON script: a list of entries consumed in order, each
    `{persona, edits: [{path, write|append|delete}], verdict, cost_usd, exit_code?, timed_out?,
    malformed?, sleep_s?}`. Edits apply relative to `req.cwd`. A persona mismatch raises so a
    test never silently runs the wrong step. The cursor lives in a file next to the campaign's
    runs dir so the script advances across separate `wt` processes."""

    CURSOR_NAME = ".fake-cursor"

    def __init__(self, script_path):
        self.script_path = Path(script_path)
        if not self.script_path.is_file():
            raise ValueError(f"fake runner script not found: {self.script_path}")
        self.script = json.loads(self.script_path.read_text(encoding="utf-8"))
        if not isinstance(self.script, list):
            raise ValueError(f"fake runner script must be a JSON list: {self.script_path}")

    def _cursor_path(self, req: RunRequest) -> Path:
        return req.run_dir.parent / self.CURSOR_NAME

    def _next_entry(self, req: RunRequest) -> dict:
        cur = self._cursor_path(req)
        idx = int(cur.read_text().strip() or 0) if cur.is_file() else 0
        if idx >= len(self.script):
            raise ValueError(f"fake runner script exhausted after {idx} run(s)")
        entry = self.script[idx]
        cur.parent.mkdir(parents=True, exist_ok=True)
        cur.write_text(str(idx + 1))
        want = entry.get("persona")
        if want and want != req.persona:
            raise ValueError(f"fake runner script entry {idx} is for {want!r}, "
                             f"but the queue asked for {req.persona!r}")
        return entry

    @staticmethod
    def _apply_edits(cwd: Path, edits) -> None:
        for edit in edits or ():
            target = Path(cwd) / edit["path"]
            if edit.get("delete"):
                if target.exists():
                    target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if "append" in edit:
                with open(target, "a", encoding="utf-8") as f:
                    f.write(edit["append"])
            else:
                target.write_text(edit.get("write", ""), encoding="utf-8")

    def run(self, req: RunRequest) -> RunResult:
        _save_inputs(req)
        entry = self._next_entry(req)
        started = time.monotonic()
        if entry.get("sleep_s"):
            time.sleep(float(entry["sleep_s"]))
        self._apply_edits(req.cwd, entry.get("edits"))
        verdict = entry.get("verdict")
        if entry.get("malformed"):
            verdict = {"garbage": True}
        exit_code = int(entry.get("exit_code", 0))
        timed_out = bool(entry.get("timed_out", False))
        cost = entry.get("cost_usd", 0.0)
        cost = float(cost) if cost is not None else None
        synthetic = {"type": "result", "subtype": "fake", "is_error": exit_code != 0,
                     "structured_output": verdict, "total_cost_usd": cost,
                     "usage": entry.get("usage") or {}, "session_id": f"fake-{req.persona}",
                     "result": json.dumps(verdict) if verdict is not None else ""}
        raw_path = req.run_dir / "result.json"
        raw_path.write_text(json.dumps(synthetic, indent=2), encoding="utf-8")
        (req.run_dir / "stderr.log").write_text("", encoding="utf-8")
        parsed = parse_result(json.dumps(synthetic), req.json_schema, exit_code=exit_code)
        if timed_out:
            parsed["is_error"] = True
            parsed["verdict"], parsed["verdict_ok"] = None, False
        return RunResult(exit_code=-1 if timed_out else exit_code, timed_out=timed_out,
                         interrupted=False, duration_s=round(time.monotonic() - started, 3),
                         raw_path=raw_path, **parsed)


def select_runner(cfg) -> PersonaRunner:
    """`WT_AGENTIC_RUNNER` / `agentic.runner` == "fake" ⇒ FakeRunner(script), else ClaudeRunner."""
    agentic = (cfg or {}).get("agentic") or {}
    kind = os.environ.get("WT_AGENTIC_RUNNER") or agentic.get("runner") or "claude"
    if kind == "fake":
        script = os.environ.get("WT_AGENTIC_FAKE_SCRIPT") or agentic.get("fake_script")
        if not script:
            raise ValueError("fake runner selected but no script: set agentic.fake_script or "
                             "WT_AGENTIC_FAKE_SCRIPT")
        return FakeRunner(script)
    if kind != "claude":
        raise ValueError(f"unknown agentic.runner {kind!r} (expected claude or fake)")
    return ClaudeRunner()
