"""Headless persona runner (SPEC-0174 / SPEC-0436 / SPEC-0442): the only place that knows
`claude -p` and the Cursor SDK child command lines.

A persona run is one fresh `-p` with a structured verdict, a dollar budget, a wall-clock
timeout and an explicit permission mode. The runner saves the prompt and the raw result under
the campaign's `runs/` dir and turns the JSON result into a `RunResult` (verdict, cost, usage,
session id). `FakeRunner` replays a scripted JSON file (edits inside the worktree + canned
verdicts) so the whole loop is testable offline; `select_runner(cfg)` picks one from
`resolve_runner` / the `NW_AGENTIC_RUNNER` env var.

Compared with the older `meetings.refresh_meetings` call: there is a timeout, the exit code is
checked, and the cost is read."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import runproc as RP
from . import schemas as SC

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
    profile: str | None = None                  # SPEC-0238: the resolved profile and its sources
    profile_sources: tuple = ()
    model_alias: str | None = None              # SPEC-0436: profile alias when `model` is mapped


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
    worker: str | None = None                   # SPEC-0344: the worker of a remote run
    writeback_failed: bool = False              # SPEC-0344: its write-back failed
    saved_refs: tuple = ()                      # SPEC-0344: the refs a failed write-back kept


class _Detached:
    """Outcome of `run()` when the pool detached the task: the run lives on, to be adopted."""
    def __repr__(self):
        return "DETACHED"


DETACHED = _Detached()


class RunHooks:
    """The pool's view of one persona task (SPEC-0225): its phase and a set-once detach flag.
    Phases: `spawning` -> `wait` -> `collect` -> `done`; every change happens under `lock`."""

    def __init__(self, lock, run_dir=None, phase: str = "spawning", pool=None):
        self.lock = lock
        self.pool = pool                # SPEC-0229: its `detach_all` flag (crash / stop sweep)
        self.run_dir = Path(run_dir) if run_dir else None
        self.phase = phase
        self.detach = threading.Event()
        self.signalled_at: float | None = None      # interim stop: when TERM was sent
        # adoption of a run with no `proc.json`: ("claim" | "legacy", epoch_s) — wait until
        # then for a wrapper to claim it, then claim it as orphaned / close it as legacy
        self.adopt: tuple | None = None

    def spawned(self) -> None:
        with self.lock:
            if self.phase == "spawning":
                self.phase = "wait"
                if getattr(self.pool, "detach_all", False):
                    self.detach.set()           # spawned after the sweep: leave it for adoption

    def detached(self) -> bool:
        return self.detach.is_set()

    def enter_collect(self) -> bool:
        with self.lock:
            if self.detach.is_set() or getattr(self.pool, "detach_all", False):
                return False
            self.phase = "collect"
            return True

    def restart(self, run_dir) -> bool:
        """SPEC-0344: the same task starts a new local run (a lost remote job, or the re-run
        after a failed write-back): back to `spawning` for `run_dir`, with no adoption mode
        (the new run has its own process to spawn). False when the pool detached meanwhile."""
        with self.lock:
            if self.detach.is_set() or getattr(self.pool, "detach_all", False):
                return False
            self.phase = "spawning"
            self.run_dir = Path(run_dir)
            self.adopt = None
            self.signalled_at = None
            return True

    def done(self) -> None:
        with self.lock:
            self.phase = "done"


class PersonaRunner(Protocol):
    detachable: bool

    def run(self, req: RunRequest, *, hooks: RunHooks | None = None) -> RunResult: ...


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

def build_argv(req: RunRequest, stream: bool = True) -> list[str]:
    """`claude -p` command line for a run; the prompt itself goes on stdin. `stream`
    (SPEC-0362): `--output-format stream-json --verbose` (the wrapper captures the event
    stream); else the single-object `--output-format json` of runs before SPEC-0362."""
    fmt = ["stream-json", "--verbose"] if stream else ["json"]
    argv = ["claude", "-p", "--output-format", *fmt,
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


LIVE_RUNNERS = ("claude", "cursor")
ALL_RUNNERS = ("claude", "cursor", "fake")
CURSOR_SCHEMA_FOOTER = (
    "Reply with a single JSON object matching this schema and nothing else.\n\n{schema}"
)


def _runner_kind_error(kind, *, allow_fake: bool) -> ValueError:
    allowed = ALL_RUNNERS if allow_fake else LIVE_RUNNERS
    return ValueError(f"unknown agentic.runner {kind!r} (expected {' or '.join(allowed)})")


def _check_runner_kind(kind, *, allow_fake: bool = False, fake_ok_host: bool = False) -> str:
    if kind == "fake" and not (allow_fake or fake_ok_host):
        raise ValueError("fake runner is only valid from NW_AGENTIC_RUNNER")
    allowed = ALL_RUNNERS if (allow_fake or fake_ok_host) else LIVE_RUNNERS
    if kind not in allowed:
        raise _runner_kind_error(kind, allow_fake=allow_fake or fake_ok_host)
    return kind


def map_cursor_model(model, effort=None, cfg=None, *, project_root=None, cursor_model=None) -> str:
    """The Cursor model id for a profile `model` / `effort` (SPEC-0436 YAML map)."""
    from . import cursor_map as CM
    return CM.map_model(model, effort, cfg, project_root=project_root, cursor_model=cursor_model)


def mapped_model(cfg, model, effort=None, *, campaign=None, project=None,
                 project_root=None, cursor_model=None) -> tuple[str, str | None]:
    """`(req.model, model_alias)` for this spawn: mapped Cursor id when the runner is cursor."""
    kind = resolve_runner(cfg, campaign=campaign, project=project)
    alias = str(model or "sonnet")
    if kind != "cursor":
        return alias, None
    return map_cursor_model(alias, effort, cfg, project_root=project_root,
                            cursor_model=cursor_model), alias


def resolve_runner(cfg, *, campaign=None, project=None) -> str:
    """The backend kind for this spawn (SPEC-0436). Env wins; then campaign pin, per-project
    overlay, committed `agents.runner`, host `agentic.runner`, else `claude`."""
    env = os.environ.get("NW_AGENTIC_RUNNER")
    if env:
        return _check_runner_kind(env, allow_fake=True)
    pin = (campaign or {}).get("runner") if isinstance(campaign, dict) else None
    if pin:
        return _check_runner_kind(pin)
    project = project or ((campaign or {}).get("project") if isinstance(campaign, dict) else None)
    agentic = (cfg or {}).get("agentic") or {}
    mapping = agentic.get("project_runners") if isinstance(agentic, dict) else None
    if project and isinstance(mapping, dict) and mapping.get(project):
        return _check_runner_kind(mapping[project])
    if project:
        try:
            from . import project as P
            kind = ((P.project_config(cfg, project).get("agents") or {}).get("runner"))
        except (ValueError, KeyError, OSError, TypeError):
            kind = None
        if kind:
            return _check_runner_kind(kind)
    host = agentic.get("runner") if isinstance(agentic, dict) else None
    if host:
        return _check_runner_kind(host, fake_ok_host=True)
    return "claude"


def cursor_needed(cfg, campaigns=None) -> bool:
    """True when the host default, a project overlay, or any given (else live) campaign
    resolves to `cursor`."""
    if resolve_runner(cfg) == "cursor":
        return True
    mapping = ((cfg or {}).get("agentic") or {}).get("project_runners")
    if isinstance(mapping, dict) and any(v == "cursor" for v in mapping.values()):
        return True
    if campaigns is None:
        try:
            from . import store
            campaigns = [st for st in store.list_campaigns(cfg)
                         if st.get("state") not in store.TERMINAL_STATES]
        except (OSError, TypeError, ValueError, KeyError):
            campaigns = ()
    for st in campaigns or ():
        try:
            if resolve_runner(cfg, campaign=st, project=st.get("project")) == "cursor":
                return True
        except ValueError:
            continue
    return False


def cursor_prompt(req: RunRequest) -> str:
    """Prompt file for a Cursor persona (SPEC-0442): prompt + system_append + schema footer.
    Python ``AgentOptions`` has no system prompt, so the footer stays in the user message."""
    parts = [req.prompt or ""]
    if req.system_append:
        parts.append(req.system_append)
    schema = json.dumps(req.json_schema or {}, indent=2)
    parts.append(CURSOR_SCHEMA_FOOTER.format(schema=schema))
    return "\n\n".join(parts)


def build_cursor_argv(req: RunRequest) -> list[str]:
    """Local Cursor SDK child (SPEC-0442). The prompt is ``prompt.md``; the key stays in
    the environment. ``runproc`` supervises this argv the same way it supervises ``claude``."""
    argv = [sys.executable, "-m", "nightwright.agentic.cursor_exec", str(req.run_dir)]
    if req.read_only:
        argv.append("--read-only")
    for d in req.add_dirs:
        argv += ["--add-dir", str(d)]
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


def _last_result_event(text: str):
    """The last `type: result` object of a stream-json (JSONL) text, or None (SPEC-0362)."""
    line = RP._result_line(text or "")
    return json.loads(line) if line is not None else None


def parse_result(stdout: str, schema: dict, *, exit_code: int) -> dict:
    """Fields of a `RunResult` derived from a `--output-format json` stdout, or from a
    `stream-json` JSONL text (its last `result` event, SPEC-0362).

    `verdict` = top-level `structured_output`, else the `result` text parsed as JSON;
    `verdict_ok` = it is an object conforming to `schema`. Unparseable stdout ⇒ error."""
    obj = _loads_lenient(stdout)
    if (obj is None and isinstance(stdout, str) and "\n" in stdout.strip()) or (
            isinstance(obj, dict) and obj.get("type") not in (None, "result")):
        obj = _last_result_event(stdout)    # a stream: never mistake its first event for the result
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


def _save_inputs(req: RunRequest, text: str | None = None) -> None:
    req.run_dir.mkdir(parents=True, exist_ok=True)
    (req.run_dir / "prompt.md").write_text(text if text is not None else req.prompt,
                                           encoding="utf-8")


def _now_iso() -> str:
    import datetime as dt
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def write_run_record(req: RunRequest, result: RunResult, started_at: str) -> RunResult:
    """`runs/<run_id>/run.json` (`nw.run.v1`, SPEC-0192): the run normalized by nightwright, beside
    the raw vendor `result.json` and `stderr.log` it points to. Returns `result`."""
    rec = {
        "schema": SC.RUN_SCHEMA, "campaign": req.run_dir.parent.parent.name,
        "run_id": req.run_dir.name, "persona": req.persona, "model": req.model,
        "effort": req.effort, "max_budget_usd": req.max_budget_usd, "timeout_s": req.timeout_s,
        "permission_mode": req.permission_mode, "started_at": started_at,
        "duration_s": result.duration_s, "exit_code": result.exit_code,
        "timed_out": result.timed_out, "interrupted": result.interrupted,
        "is_error": result.is_error, "cost_usd": result.cost_usd,
        "cost_estimated": result.cost_usd is None, "usage": result.usage,
        "session_id": result.session_id, "verdict_ok": result.verdict_ok,
        "verdict": result.verdict,
        "profile": req.profile, "profile_sources": list(req.profile_sources),
        "worker": result.worker,                                 # SPEC-0344: null when local
        "files": {"prompt": "prompt.md", "raw": "result.json", "stderr": "stderr.log"},
    }
    if result.writeback_failed or result.saved_refs:
        rec["writeback_failed"] = result.writeback_failed
        rec["saved_refs"] = list(result.saved_refs)
    if (req.run_dir / "stream.jsonl").is_file():                 # SPEC-0362
        rec["files"]["stream"] = "stream.jsonl"
    (req.run_dir / "run.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n",
                                          encoding="utf-8")
    return result


def _child_env(req: RunRequest) -> dict:
    """The ambient environment plus `req.env`, minus a launched session's limits (SPEC-0319;
    the same filter the detached wrapper applies, `runproc.child_env`)."""
    return RP.child_env(req.env)


# ---- the real thing ---------------------------------------------------------------------------

def _request_doc(req: RunRequest, started_at: str) -> dict:
    doc = {"schema": "nw.run.request.v1", "persona": req.persona, "model": req.model,
           "effort": req.effort, "max_budget_usd": req.max_budget_usd,
           "timeout_s": req.timeout_s, "permission_mode": req.permission_mode,
           "read_only": req.read_only, "cwd": str(req.cwd), "json_schema": req.json_schema,
           "started_at": started_at}
    if req.model_alias:
        doc["model_alias"] = req.model_alias
    return doc


def rebuild_request(run_dir, *, budget: float | None = None) -> RunRequest:
    """The `RunRequest` of a run already started, from `request.json` + `prompt.md`
    (adoption). Only the fields collecting a result needs are restored."""
    run_dir = Path(run_dir)
    doc = RP.read_json(run_dir / "request.json") or {}
    try:
        prompt = (run_dir / "prompt.md").read_text(encoding="utf-8")
    except OSError:
        prompt = ""
    return RunRequest(
        persona=doc.get("persona") or run_dir.name.split("-", 1)[-1], prompt=prompt,
        system_append="", cwd=Path(doc.get("cwd") or "."), model=doc.get("model") or "",
        effort=doc.get("effort"),
        max_budget_usd=float(budget if budget is not None else doc.get("max_budget_usd") or 0),
        timeout_s=int(doc.get("timeout_s") or 0),
        permission_mode=doc.get("permission_mode") or "", read_only=bool(doc.get("read_only")),
        allowed_tools=(), add_dirs=(), json_schema=doc.get("json_schema") or {}, env={},
        run_dir=run_dir)


def _read_text(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def collect(req: RunRequest) -> RunResult:
    """Turn a finished (or vanished) run's files into the `RunResult` and `run.json`.
    Idempotent: it only reads the run dir and rewrites `run.json`."""
    run_dir = req.run_dir
    ex = RP.read_json(run_dir / "exit.json")
    started_at = (RP.read_json(run_dir / "request.json") or {}).get("started_at") or _now_iso()
    stdout, stderr = RP.run_output(run_dir), _read_text(run_dir / "stderr.log")
    proc = RP.read_json(run_dir / "proc.json") or {}
    ticks = proc.get("wrapper_start_ticks")
    if proc.get("wrapper_pid") and (ticks is None or RP.start_ticks(int(proc["wrapper_pid"])) == ticks):
        try:                                         # reap a zombie wrapper left across an exec
            os.waitpid(int(proc["wrapper_pid"]), os.WNOHANG)
        except (ChildProcessError, OSError):
            pass
    if ex is None:
        code, timed_out, interrupted, duration, killed_by = -1, False, False, 0.0, None
        parsed = parse_result(stdout, req.json_schema, exit_code=-1)
        why = ("run process vanished" if (run_dir / "child.json").is_file()
               else "run process vanished (wrapper died before recording its child)")
        parsed["is_error"] = True
        parsed["result_text"] = _tail((stderr + "\n" + why).strip())
    else:
        code = int(ex.get("exit_code", -1))
        timed_out, killed_by = bool(ex.get("timed_out")), ex.get("killed_by")
        interrupted, duration = killed_by == "signal", float(ex.get("duration_s") or 0.0)
        parsed = parse_result(stdout, req.json_schema, exit_code=code)
        if timed_out or code == -1 or killed_by == "external":
            parsed["is_error"] = True
        if ex.get("error"):
            parsed["result_text"] = _tail(str(ex["error"]))
    return write_run_record(req, RunResult(
        exit_code=code, timed_out=timed_out, interrupted=interrupted,
        duration_s=round(duration, 3), raw_path=run_dir / "result.json", **parsed), started_at)


def _error_result(req: RunRequest, text: str, started_at: str, duration: float) -> RunResult:
    return write_run_record(req, RunResult(
        exit_code=-1, timed_out=False, interrupted=False, duration_s=round(duration, 3),
        is_error=True, cost_usd=None, result_text=_tail(text), raw_path=req.run_dir / "result.json"),
        started_at)


def mark_held(req: RunRequest) -> None:
    """SPEC-0345: claim the run dir of a run the rate-limit backoff held (it never starts) as
    orphaned and `held`. A runner that adopts it later (this one detached before collecting
    it) then closes it as orphaned, and `_synthetic` charges it nothing."""
    try:
        RP.claim(req.run_dir, {"orphaned": True, "held": True, "by": "rate_limit",
                               "ts": _now_iso()})
    except OSError:
        pass


def _synthetic(req: RunRequest, text: str, *, cost_usd: float | None = None) -> RunResult:
    """An interrupted run that never produced output: the state stays, the step respawns.
    `cost_usd=0.0` marks a run known never to have started (SPEC-0345: held by the rate-limit
    backoff), so it is not charged its budget as an estimate; so does a run dir `mark_held`
    claimed, whoever closes it."""
    if cost_usd is None and (RP.read_json(req.run_dir / "proc.json") or {}).get("held"):
        cost_usd = 0.0
    started_at = (RP.read_json(req.run_dir / "request.json") or {}).get("started_at") or _now_iso()
    return write_run_record(req, RunResult(
        exit_code=-1, timed_out=False, interrupted=True, duration_s=0.0, is_error=True,
        cost_usd=cost_usd, result_text=text, raw_path=req.run_dir / "result.json"), started_at)


class ClaudeRunner:
    """Spawn `claude -p` per `build_argv` through the detached wrapper (SPEC-0225): the run
    lives in its own session and records itself in files, so `run` is idempotent per run dir
    (it attaches when `proc.json` exists) and the run can outlive this process."""

    detachable = True

    def __init__(self, spawn_timeout_s: float = 30.0, stream: bool = True):
        self.spawn_timeout_s = spawn_timeout_s
        self.stream = stream                        # SPEC-0362: stream-json capture

    def _launch_argv(self, req: RunRequest) -> list[str]:
        return build_argv(req, stream=self.stream)

    def _prompt_text(self, req: RunRequest) -> str:
        return req.prompt

    def run(self, req: RunRequest, *, hooks: RunHooks | None = None):
        run_dir = req.run_dir
        attach = (run_dir / "proc.json").exists() or bool(hooks and hooks.adopt)
        if not attach:
            _save_inputs(req, text=self._prompt_text(req))
        started_at = _now_iso()
        started = time.monotonic()
        wrapper = None
        try:
            if not attach and not (run_dir / "proc.json").exists():
                RP.write_json_atomic(run_dir / "request.json", _request_doc(req, started_at))
                launch = {"argv": self._launch_argv(req), "cwd": str(req.cwd),
                          "env": dict(req.env)}
                if self.stream:
                    launch["stream"] = True         # fixed at launch (SPEC-0360 invariant 9)
                RP.write_json_atomic(run_dir / "launch.json", launch)
                with open(run_dir / "wrapper.log", "ab") as log:
                    wrapper = subprocess.Popen(
                        [sys.executable, "-m", "nightwright.agentic.runproc", str(run_dir)],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
                err = self._await_claim(req, wrapper, started)
                if err:
                    return _error_result(req, err, started_at, time.monotonic() - started)
            if hooks:
                hooks.spawned()
            return self._wait(req, hooks)
        except BaseException:
            if hooks is None:                       # attached: stopping the caller stops the agent
                self._kill(run_dir)
            raise
        finally:
            if wrapper is not None:
                wrapper.poll()                      # reap it when it is already gone

    def _await_claim(self, req: RunRequest, wrapper, started: float) -> str | None:
        """Wait for the wrapper's `proc.json`. An error text when the launch failed: the
        wrapper exited without claiming, or `spawn_timeout_s` passed (then the runner claims
        the run itself, so a late wrapper can never start `claude`)."""
        run_dir = req.run_dir
        while not (run_dir / "proc.json").exists():
            if wrapper.poll() is not None:
                if (run_dir / "proc.json").exists():
                    break
                return ("launch failed: the wrapper exited without claiming the run\n"
                        + _tail(_read_text(run_dir / "wrapper.log")))
            if time.monotonic() - started > self.spawn_timeout_s:
                if RP.claim(run_dir, {"orphaned": True, "by": "spawn_timeout", "ts": _now_iso()}):
                    return (f"launch failed: no claim within {self.spawn_timeout_s:g}s\n"
                            + _tail(_read_text(run_dir / "wrapper.log")))
                break                               # the wrapper won the claim at the last moment
            time.sleep(0.05)
        return None

    def _poll(self) -> None:
        time.sleep(0.2)

    def _wait(self, req: RunRequest, hooks: RunHooks | None):
        run_dir = req.run_dir
        term_at = kill_at = None
        seen_orphan = False
        while True:
            if hooks and hooks.detached():
                return DETACHED
            status = RP.run_status(run_dir)
            if status == "unclaimed" and hooks and hooks.adopt:
                mode, at = hooks.adopt
                if time.time() >= at:
                    if mode == "legacy":
                        if not hooks.enter_collect():
                            return DETACHED
                        return _synthetic(req, "legacy orphan: started by an earlier runner, "
                                               "no process record")
                    if RP.claim(run_dir, {"orphaned": True, "by": "adopt", "ts": _now_iso()}):
                        if not hooks.enter_collect():
                            return DETACHED
                        return _synthetic(req, "orphaned: no wrapper claimed the run")
                self._poll()
                continue
            if status == "orphaned":
                if hooks and not hooks.enter_collect():
                    return DETACHED
                return _synthetic(req, "orphaned: no wrapper claimed the run")
            if status == "vanished" and (term_at is not None or seen_orphan
                                         or (run_dir / "term.json").exists()):
                # the orphaned group ended (we killed it, or `claude` finished on its own)
                self._orphan_duties(req, term_at, kill_at)
                status = "finished"
            if status in ("finished", "vanished"):
                if hooks and not hooks.enter_collect():
                    return DETACHED
                return collect(req)
            if status == "orphan_group":            # the wrapper died, `claude` did not
                seen_orphan = True
                term_at, kill_at = self._orphan_duties(req, term_at, kill_at)
            self._poll()

    def _orphan_duties(self, req, term_at, kill_at):
        """Enforce the timeout on a group whose wrapper is gone; synthesize `exit.json` once
        the group is empty (the status check then sees it as finished)."""
        run_dir = req.run_dir
        child = RP.read_json(run_dir / "child.json")
        started = (RP.read_json(run_dir / "request.json") or {}).get("started_at")
        now = time.time()
        due = _epoch(started) + req.timeout_s if started and req.timeout_s else None
        if due and now >= due and term_at is None:
            term_at = now
            try:
                os.killpg(int(child["pgid"]), 15)
            except OSError:
                pass
        if term_at and now >= term_at + RP.GRACE_S and kill_at is None:
            kill_at = now
            try:
                os.killpg(int(child["pgid"]), 9)
            except OSError:
                pass
        if not RP.group_alive(child) and not (run_dir / "exit.json").exists():
            stopped = (run_dir / "term.json").exists()
            output = RP.run_output(run_dir)
            parsed = parse_result(output, req.json_schema, exit_code=0)
            ok = bool(_loads_lenient(output) or _last_result_event(output)) and not parsed["is_error"]
            RP.write_json_atomic(run_dir / "exit.json", {
                "exit_code": 0 if ok else -1, "timed_out": term_at is not None,
                "killed_by": "timeout" if term_at else ("signal" if stopped else None),
                "signal": 15 if stopped and not term_at else None,
                "ended_at": _now_iso(), "duration_s": round(now - _epoch(started or _now_iso()), 3),
                "synthesized": True})
        return term_at, kill_at

    @staticmethod
    def _kill(run_dir) -> None:
        child = RP.read_json(run_dir / "child.json")
        proc = RP.read_json(run_dir / "proc.json")
        RP.terminate_run(run_dir)
        RP.kill_group(child)
        if proc and RP.wrapper_alive(proc):
            try:
                os.kill(int(proc["wrapper_pid"]), 9)
            except OSError:
                pass


class CursorRunner(ClaudeRunner):
    """Spawn the local Cursor SDK child per `build_cursor_argv` (SPEC-0442). Same
    detach/adopt wrapper as `ClaudeRunner`. The prompt file is the schema footer
    concatenated onto the persona prompt. Conductor and steward launch stay on
    the `cursor-agent` CLI."""

    def _launch_argv(self, req: RunRequest) -> list[str]:
        return build_cursor_argv(req)

    def _prompt_text(self, req: RunRequest) -> str:
        return cursor_prompt(req)


def _epoch(iso: str) -> float:
    import datetime as dt
    try:
        return dt.datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return time.time()


# ---- remote persona jobs (SPEC-0344) ------------------------------------------------------------

class NotRemotable(ValueError):
    """A request that cannot run on a worker (a path outside the worktree, an env key outside
    the allowlist): the run stays local."""


# passed to the worker verbatim / stripped here and re-set by the worker to its own values
ENV_VERBATIM = frozenset({"NW_CAMPAIGN", "NW_SPEC_ID", "NW_BASE", "NW_PERSONA_RUN",
                          "NW_REBASE_TARGET"})
# `NW_PERSONA` (SPEC-0425) is re-set from the job's `persona`, so a worker on older code that
# would refuse an unknown key still takes the job
ENV_RESET = frozenset({"NW_WORKTREE", "NW_DATA_DIR", "NW_CONFIG_DIR", "TMPDIR", "TEMP", "TMP",
                       "NW_PERSONA"})
TREE_TOKEN = "${NW_TREE}"                     # a worktree-relative path in `allowed_tools`
_ROOT_END = r"""(?=[/\s()*'"`,;|&<>:]|$)"""     # a path separator or the end of the token
_TREE_PATH_RE = re.compile(re.escape(TREE_TOKEN) + r"""([^\s()*'"`,;|&<>]*)""")
_ABS_PATH_RE = re.compile(r"(?<![\w.:/~$}-])/[A-Za-z0-9._~+@%=-][^\s()*'\"`,;|&<>]*")


def _rel_inside(path, roots) -> str | None:
    """`path` relative to the first of `roots` it is inside ("." for the root), else None."""
    for root in roots:
        try:
            rel = Path(os.path.normpath(str(path))).relative_to(root)
        except ValueError:
            continue
        return rel.as_posix() or "."
    return None


def _roots(worktree) -> list[Path]:
    wt = Path(os.path.normpath(str(worktree)))
    real = Path(os.path.realpath(str(worktree)))
    return [wt] if wt == real else [wt, real]


def request_to_job(req: RunRequest, worktree) -> dict:
    """The `request` of a `kind: persona` job (SPEC-0344): every field of `req`, paths made
    relative to `worktree`, worktree-absolute paths in `allowed_tools` replaced by
    `TREE_TOKEN`, the env reduced to `ENV_VERBATIM` (the `ENV_RESET` keys are the worker's own).
    Raises `NotRemotable` for a cwd / `add_dirs` entry / `allowed_tools` path outside the
    worktree or another env key."""
    roots = _roots(worktree)
    cwd = _rel_inside(req.cwd, roots)
    if cwd is None:
        raise NotRemotable(f"cwd {req.cwd} is outside the worktree")
    add_dirs = []
    for d in req.add_dirs:
        rel = _rel_inside(d, roots)
        if rel is None:
            raise NotRemotable(f"add_dirs entry {d} is outside the worktree")
        add_dirs.append(rel)
    tools = []
    for tool in req.allowed_tools:
        out = str(tool)
        for root in sorted({str(r) for r in roots}, key=len, reverse=True):
            # the root only as a whole path component: `<wt>-other` stays and is refused below
            out = re.sub(re.escape(root) + _ROOT_END, lambda _m: TREE_TOKEN, out)
        stray = _ABS_PATH_RE.search(out)
        if stray:
            raise NotRemotable(f"allowed_tools entry {tool!r} names {stray.group(0)}, "
                               "outside the worktree")
        for m in _TREE_PATH_RE.finditer(out):
            # `<wt>/../x` leaves the worktree, as a cwd / `add_dirs` entry would
            if ".." in re.split(r"[/:]", m.group(1)):
                raise NotRemotable(f"allowed_tools entry {tool!r} has a `..` component, "
                                   "outside the worktree")
        tools.append(out)
    env = {}
    for k, v in req.env.items():
        if k in ENV_VERBATIM:
            env[k] = str(v)
        elif k not in ENV_RESET:
            raise NotRemotable(f"env key {k} is outside the remote allowlist")
    doc = {"persona": req.persona, "prompt": req.prompt, "system_append": req.system_append,
           "cwd": cwd, "model": req.model, "effort": req.effort,
           "max_budget_usd": req.max_budget_usd, "timeout_s": req.timeout_s,
           "permission_mode": req.permission_mode, "read_only": req.read_only,
           "allowed_tools": tools, "add_dirs": add_dirs, "json_schema": req.json_schema,
           "env": env, "profile": req.profile, "profile_sources": list(req.profile_sources)}
    if req.model_alias:
        doc["model_alias"] = req.model_alias
    return doc


def request_from_job(doc: dict, tree, run_dir, env: dict | None = None) -> RunRequest:
    """The worker's `RunRequest` from `request_to_job`'s doc: paths anchored at `tree`, the
    worker's own `env` values (`ENV_RESET` keys) over the verbatim ones. Raises `ValueError`
    for a malformed doc or an env key outside `ENV_VERBATIM`."""
    tree = Path(tree)
    extra = sorted(set(doc.get("env") or {}) - ENV_VERBATIM)
    if extra:
        raise ValueError(f"env keys outside the allowlist {sorted(ENV_VERBATIM)}: {extra}")

    def at(rel) -> Path:
        rel = str(rel)
        if rel.startswith("/") or ".." in Path(rel).parts:
            raise ValueError(f"path {rel!r} is not relative to the tree")
        return tree if rel == "." else tree / rel
    return RunRequest(
        persona=str(doc["persona"]), prompt=str(doc.get("prompt") or ""),
        system_append=str(doc.get("system_append") or ""), cwd=at(doc.get("cwd") or "."),
        model=str(doc.get("model") or ""), effort=doc.get("effort"),
        max_budget_usd=float(doc.get("max_budget_usd") or 0), timeout_s=int(doc.get("timeout_s") or 0),
        permission_mode=str(doc.get("permission_mode") or ""),
        read_only=bool(doc.get("read_only")),
        allowed_tools=tuple(str(t).replace(TREE_TOKEN, str(tree))
                            for t in doc.get("allowed_tools") or ()),
        add_dirs=tuple(at(d) for d in doc.get("add_dirs") or ()),
        json_schema=doc.get("json_schema") or {},
        env={**{k: str(v) for k, v in (doc.get("env") or {}).items()}, **(env or {})},
        run_dir=Path(run_dir), profile=doc.get("profile"),
        profile_sources=tuple(doc.get("profile_sources") or ()),
        model_alias=doc.get("model_alias"))


class RemoteRunner:
    """A persona run on a worker (SPEC-0344): a `kind: persona` job through SPEC-0340, its
    result bundle written back into the campaign worktree. Detachable like `ClaudeRunner`: a
    stop leaves the job and its SPEC-0341 record for the next runner to adopt. The logic is in
    `remote_personas`; `local` is the runner a run falls back to before the worker took it.
    `request` is an adopted job's full request, rebuilt from its record (None when fresh, or
    when the record keeps none)."""

    detachable = True

    def __init__(self, cfg, state: dict, worker: dict, *, local=None, slot=None,
                 record: dict | None = None, request: RunRequest | None = None):
        self.cfg, self.state, self.worker = cfg, state, worker
        self.local, self.slot, self.record, self.request = local, slot, record, request
        self.job_id: str | None = None         # the job this runner is attached to
        self.job_lock: int | None = None       # its `job-<id>.lock` fd, held with a slot

    def run(self, req: RunRequest, *, hooks: RunHooks | None = None):
        from . import remote_personas as RPS
        return RPS.run(self, req, hooks)


# ---- offline stand-in ----------------------------------------------------------------------------

class FakeRunner:
    """Replay a JSON script: a list of entries consumed in order, each
    `{persona, edits: [{path, write|append|delete}], verdict, cost_usd, exit_code?, timed_out?,
    malformed?, sleep_s?, pre_shell?, shell?, transcript?}`. Edits apply relative to `req.cwd`; `pre_shell`
    / `shell` are command lists run in `req.cwd` with `req.env` before / after the edits. A persona mismatch raises so a
    test never silently runs the wrong step. The cursor lives in a file next to the campaign's
    runs dir so the script advances across separate `nw` processes."""

    CURSOR_NAME = ".fake-cursor"
    detachable = False

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

    @staticmethod
    def _shell(req: RunRequest, cmds) -> None:
        for cmd in cmds or ():
            subprocess.run(cmd, shell=True, cwd=str(req.cwd), env=_child_env(req),
                           capture_output=True, text=True)

    def run(self, req: RunRequest, *, hooks: RunHooks | None = None) -> RunResult:
        _save_inputs(req)
        entry = self._next_entry(req)
        started_at = _now_iso()
        started = time.monotonic()
        RP.write_json_atomic(req.run_dir / "request.json", _request_doc(req, started_at))
        if entry.get("transcript") is not None:     # SPEC-0344: a scripted transcript
            (req.run_dir / "stream.jsonl").write_text(str(entry["transcript"]), encoding="utf-8")
        if entry.get("sleep_s"):
            time.sleep(float(entry["sleep_s"]))
        self._shell(req, entry.get("pre_shell"))
        self._apply_edits(req.cwd, entry.get("edits"))
        self._shell(req, entry.get("shell"))
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
        api_error = entry.get("api_error")
        if isinstance(api_error, dict):     # SPEC-0345: an API error result (`status`, `message`)
            synthetic.update(is_error=True, structured_output=None,
                             api_error_status=api_error.get("status"),
                             result=str(api_error.get("message") or ""))
        raw_path = req.run_dir / "result.json"
        raw_path.write_text(json.dumps(synthetic, indent=2), encoding="utf-8")
        (req.run_dir / "stderr.log").write_text("", encoding="utf-8")
        parsed = parse_result(json.dumps(synthetic), req.json_schema, exit_code=exit_code)
        if timed_out:
            parsed["is_error"] = True
            parsed["verdict"], parsed["verdict_ok"] = None, False
        return write_run_record(req, RunResult(
            exit_code=-1 if timed_out else exit_code, timed_out=timed_out, interrupted=False,
            duration_s=round(time.monotonic() - started, 3), raw_path=raw_path, **parsed),
            started_at)


def select_runner(cfg, runner=None) -> PersonaRunner:
    """Construct the persona backend. `runner` is an already-resolved kind; env still wins.
    `NW_AGENTIC_RUNNER` / `agentic.runner` == "fake" ⇒ FakeRunner(script); `cursor` ⇒
    CursorRunner; else ClaudeRunner."""
    agentic = (cfg or {}).get("agentic") or {}
    kind = os.environ.get("NW_AGENTIC_RUNNER") or runner or resolve_runner(cfg)
    if kind == "fake":
        script = os.environ.get("NW_AGENTIC_FAKE_SCRIPT") or agentic.get("fake_script")
        if not script:
            raise ValueError("fake runner selected but no script: set agentic.fake_script or "
                             "NW_AGENTIC_FAKE_SCRIPT")
        return FakeRunner(script)
    timeout = float(agentic.get("spawn_timeout_s") or 30.0)
    stream = bool(agentic.get("stream_runs", True))
    if kind == "cursor":
        return CursorRunner(timeout, stream=stream)
    if kind != "claude":
        raise ValueError(f"unknown agentic.runner {kind!r} (expected claude, cursor or fake)")
    return ClaudeRunner(timeout, stream=stream)
