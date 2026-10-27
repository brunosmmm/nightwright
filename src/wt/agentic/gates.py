"""Deterministic gate executor (SPEC-0173).

Gates are the project's own proof commands from `.wt/project.yaml` (SPEC-0170): tests, lint,
anything. They run in the campaign worktree, in order, **all of them every time** (no
short-circuit, so triage sees the full picture), each with a timeout that kills the whole
process group. Output goes to one log per gate under the campaign's `gates/` directory; a
bounded tail travels in the result for prompts and `wt campaign show`. `required` decides
whether a failure blocks; optional failures are recorded and never block."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import schemas as SC
from . import store
from .project import expand_cmd

TAIL_LINES = 60
_KILL_GRACE_S = 5
_LOG_SEQ_RE = re.compile(r"^(\d{3})-")

# SPEC-0229: live gate processes, so a stop can end them (gates run in their own sessions, out of
# reach of any signal sent to the runner's group). `_stopping` makes a stop sticky until reset().
_LOCK = threading.Lock()
_LIVE: set = set()
_stopping = False


@dataclass(frozen=True)
class GateResult:
    name: str
    required: bool
    ok: bool
    exit_code: int | None
    timed_out: bool
    duration_s: float
    log: str          # path relative to the campaign dir
    tail: str         # last TAIL_LINES lines of the log
    cmd: str = ""     # the expanded command (SPEC-0192; also in the gates/NNN-name.json sidecar)
    cwd: str = ""
    interrupted: bool = False   # SPEC-0229: killed by a stop, or not started because of one


@dataclass(frozen=True)
class GateRun:
    seq: int
    ok: bool
    results: tuple[GateResult, ...]
    started_at: str
    duration_s: float
    interrupted: bool = False   # SPEC-0229: a gate was ended by a stop, or the run was cut short


def terminate_all() -> None:
    """Stop every gate: no further gate starts, and each live gate's process group gets TERM,
    then KILL (SPEC-0229). Called from the main loop only, never from a signal handler."""
    global _stopping
    with _LOCK:
        _stopping = True
        live = list(_LIVE)
    for proc in live:
        _kill_group(proc)


def reset() -> None:
    """Clear the stop flag and the registry (a new loop, a single-shot tick, tests)."""
    global _stopping
    with _LOCK:
        _stopping = False
        _LIVE.clear()


def tail(path, n: int = TAIL_LINES) -> str:
    """Last `n` lines of a text file ("" when missing)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.rstrip("\n").splitlines()[-n:])


def tmp_env(path) -> dict[str, str]:
    """`TMPDIR` & co. pointing at a child's private directory (SPEC-0253): pytest's default
    basetemp follows `tempfile.gettempdir()`, so no gate command needs `--basetemp`."""
    return {"TMPDIR": str(path), "TEMP": str(path), "TMP": str(path)}


def gate_env(cfg, state: dict, seq: int | None = None) -> dict[str, str]:
    """Environment every gate (and anything it calls) sees: the same wt store the loop uses.
    With `seq` the gate run also gets its own `TMPDIR` under the campaign dir (SPEC-0253)."""
    repo = state.get("repo") or {}
    tmp = tmp_env(store.child_tmp(cfg, state["id"], f"gate-{seq:03d}")) \
        if seq is not None and state.get("id") else {}
    return {**tmp,
        "WT_CAMPAIGN": str(state.get("id") or ""),
        "WT_DATA_DIR": str(cfg["data_dir"]),
        "WT_CONFIG_DIR": str(cfg.get("config_dir") or ""),
        "WT_SPEC_ID": str(state.get("spec_id") or ""),
        "WT_WORKTREE": str(repo.get("worktree") or ""),
        "WT_BASE": str(repo.get("base") or ""),
    }


def placeholders(state: dict) -> dict:
    repo = state.get("repo") or {}
    return {"spec_id": state.get("spec_id"), "campaign": state.get("id"),
            "worktree": repo.get("worktree"), "base": repo.get("base")}


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM the gate's process group, wait a grace period, then SIGKILL it."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig, wait_s in ((signal.SIGTERM, _KILL_GRACE_S), (signal.SIGKILL, _KILL_GRACE_S)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait_s)
            return
        except subprocess.TimeoutExpired:
            continue


def _write_sidecar(log_path: Path, result: GateResult, *, timeout_s: int, started_at: str,
                   error: str | None) -> None:
    """`gates/NNN-name.json` (`wt.gate.v1`, SPEC-0192): what wt knows about the run; the
    `.log` beside it holds only the command's own output."""
    rec = {"schema": SC.GATE_SCHEMA, "name": result.name, "cmd": result.cmd, "cwd": result.cwd,
           "required": result.required, "ok": result.ok, "exit_code": result.exit_code,
           "timed_out": result.timed_out, "timeout_s": timeout_s, "started_at": started_at,
           "duration_s": result.duration_s, "log": log_path.name, "error": error}
    if result.interrupted:
        rec["interrupted"] = True
    log_path.with_suffix(".json").write_text(json.dumps(rec, indent=2, ensure_ascii=False)
                                             + "\n", encoding="utf-8")


def run_gate(gate: dict, worktree, env: dict, log_path, placeholders: dict, *,
             log_rel: str | None = None) -> GateResult:
    """Run one gate in `worktree / gate["cwd"]`, combined output to `log_path`, timeout
    `gate["timeout_s"]` (kills the whole process group). `ok` = exit code 0. The log is the
    command's output only; command, cwd, exit, timing and errors go to the JSON sidecar."""
    cmd = expand_cmd(gate["cmd"], **placeholders)
    cwd = Path(worktree) / gate.get("cwd", ".")
    timeout_s = int(gate.get("timeout_s", 1200))
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    child_env = {**os.environ, **env}
    started_at = store.now_iso()
    started = time.monotonic()
    timed_out = False
    error = None
    exit_code = None
    interrupted = False
    with open(log_path, "w", encoding="utf-8") as log:
        proc = None
        try:
            with _LOCK:                 # SPEC-0229: check the stop flag and register atomically
                if _stopping:
                    interrupted = True
                    error = "not started: the runner is stopping"
                else:
                    proc = subprocess.Popen(cmd, shell=True, cwd=str(cwd), env=child_env,
                                            stdout=log, stderr=subprocess.STDOUT,
                                            start_new_session=True)
                    _LIVE.add(proc)
        except OSError as e:
            error = f"failed to start: {e}"
        if proc is not None:
            try:
                try:
                    exit_code = proc.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_group(proc)
                    exit_code = proc.returncode
                    error = f"timed out after {timeout_s}s; process group killed"
            finally:
                with _LOCK:
                    _LIVE.discard(proc)
                    stopping = _stopping
            if stopping and not timed_out and exit_code != 0:
                interrupted = True
                error = "interrupted: the runner is stopping; process group killed"
    duration = round(time.monotonic() - started, 3)
    ok = proc is not None and exit_code == 0 and not timed_out
    result = GateResult(gate["name"], bool(gate.get("required", True)), ok, exit_code,
                        timed_out, duration, log_rel or str(log_path), tail(log_path),
                        cmd=cmd, cwd=str(cwd), interrupted=interrupted)
    _write_sidecar(log_path, result, timeout_s=timeout_s, started_at=started_at, error=error)
    return result


def next_gate_seq(cfg, campaign_id) -> int:
    """1 + the highest `NNN-` prefix among existing gate logs (so debug runs and loop runs
    never overwrite each other)."""
    d = store.gates_dir(cfg, campaign_id)
    if not d.is_dir():
        return 1
    seqs = [int(m.group(1)) for p in d.iterdir() if (m := _LOG_SEQ_RE.match(p.name))]
    return (max(seqs) + 1) if seqs else 1


def run_all(project_cfg: dict, campaign_state: dict, cfg, *, seq: int,
            stop: threading.Event | None = None) -> GateRun:
    """Run every gate of `project_cfg` in the campaign's worktree; logs land at
    `gates/<seq:03>-<name>.log` under the campaign dir. `ok` = every *required* gate ok.
    The returned `GateRun.seq` is the seq actually used (higher than `seq` if it was taken)."""
    cid = campaign_state["id"]
    worktree = (campaign_state.get("repo") or {}).get("worktree")
    if not worktree:
        raise ValueError(f"campaign {cid} has no worktree recorded")
    # SPEC-0253: `next_gate_seq` only sees logs, which appear once the first gate starts, so a
    # concurrent `wt gate run` can pick the same seq. Claim `gate-<seq>` atomically and take
    # the next free seq on a collision, so two runs never share (and wipe) one TMPDIR.
    while True:
        owner = f"gate-{seq:03d}"
        try:
            store.child_tmp(cfg, cid, owner, exclusive=True)
            break
        except FileExistsError:
            seq += 1
    env = gate_env(cfg, campaign_state, seq)
    ph = placeholders(campaign_state)
    cdir = store.campaign_dir(cfg, cid)
    started_at = store.now_iso()
    t0 = time.monotonic()
    results = []
    cut = False
    try:
        for gate in project_cfg.get("gates") or []:
            if stop is not None and stop.is_set():      # SPEC-0229: no next gate after a stop
                cut = True
                break
            rel = f"gates/{seq:03d}-{gate['name']}.log"
            results.append(run_gate(gate, worktree, env, cdir / rel, ph, log_rel=rel))
    finally:                                    # SPEC-0253: only the owner removes its dir
        store.clear_child_tmp(cfg, cid, owner)
    ok = all(r.ok for r in results if r.required)
    return GateRun(seq, ok, tuple(results), started_at, round(time.monotonic() - t0, 3),
                   interrupted=cut or any(r.interrupted for r in results))


def blocking_failures(run: GateRun) -> list[GateResult]:
    return [r for r in run.results if r.required and not r.ok]


def as_payload(run: GateRun) -> dict:
    """Shape shared by the `gate` ledger event and `last_gate` in state. The run number is
    `gate_seq`, not `seq`: ledger events carry their own `seq` in the envelope and
    `append_event` merges the payload over it."""
    results = [asdict(r) for r in run.results]
    for r in results:
        if not r.pop("interrupted"):      # SPEC-0229: only recorded when true
            continue
        r["interrupted"] = True
    return {"gate_seq": run.seq, "ok": run.ok, "started_at": run.started_at,
            "duration_s": run.duration_s, "results": results}


def run_and_log(cfg, campaign_state: dict, project_cfg: dict, *, seq=None, by="tick") -> GateRun:
    """`run_all` + one `gate` ledger event. Does not touch `state.json`; callers that steer
    the queue (SPEC-0176) update `last_gate` themselves."""
    cid = campaign_state["id"]
    seq = seq if seq is not None else next_gate_seq(cfg, cid)
    run = run_all(project_cfg, campaign_state, cfg, seq=seq)
    store.append_event(cfg, cid, "gate", as_payload(run), by=by)
    return run

