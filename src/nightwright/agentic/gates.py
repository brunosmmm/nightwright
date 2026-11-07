"""Deterministic gate executor (SPEC-0173).

Gates are the project's own proof commands from `.nw/project.yaml` (SPEC-0170): tests, lint,
anything. They run in the campaign worktree, in order, **all of them every time** (no
short-circuit, so triage sees the full picture), each with a timeout that kills the whole
process group. Output goes to one log per gate under the campaign's `gates/` directory; a
bounded tail travels in the result for prompts and `nw campaign show`. `required` decides
whether a failure blocks; optional failures are recorded and never block."""
from __future__ import annotations

import contextvars
import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .. import render
from . import gate_results
from . import gate_slots
from . import schemas as SC
from . import store
from . import project as P
from . import remote_jobs as RJ
from . import runproc
from .project import expand_cmd

TAIL_LINES = 60
# SPEC-0346: false means `run_gate` starts the child from the env dict it was given, not from
# this process. The worker sets it around its own gates. The call keeps the signature it had
# before that choice, so a stand-in for `run_gate_with_retries` still runs and an in-run disk
# abort can finish the job.
_GATE_INHERIT_ENV = contextvars.ContextVar("nw_gate_inherit_env", default=True)
REPORT_MAX_BYTES = 64 * 1024    # SPEC-0359: a larger selection report is dropped
_KILL_GRACE_S = 5
# SPEC-0392: a gate's timeout is a hard bound. After TERM the tree gets `_KILL_GRACE_S`, after KILL
# `REAP_BOUND_S` to disappear; the whole kill runs under `hard_deadline_s()`, so `run_gate` returns
# by `timeout_s + HARD_DEADLINE_S` even if the kill itself hangs. NW_GATE_GRACE_S / NW_GATE_REAP_S
# shorten the first two (tests; the same pattern as NW_RUNPROC_GRACE_S).
REAP_BOUND_S = 10
HARD_SLACK_S = 5
HARD_DEADLINE_S = 2 * _KILL_GRACE_S + REAP_BOUND_S + HARD_SLACK_S
_LOG_SEQ_RE = re.compile(r"^(\d{3})-")
# SPEC-0289: pytest exits 4 (usage error: ids not found) and 5 (nothing collected) when a targeted
# rerun could not run the ids, which says nothing about whether the failure reproduces.
TARGETED_UNRESOLVED_EXITS = (4, 5)
# shell operators after which appended ids would no longer be arguments of the gate's own command
_SHELL_OPS_RE = re.compile(r"[|&;<>()`\n]|\$\(")

# SPEC-0229: live gate processes, so a stop can end them (gates run in their own sessions, out of
# reach of any signal sent to the runner's group). `_stopping` makes a stop sticky until reset().
_LOCK = threading.Lock()
_LIVE: dict = {}    # SPEC-0392: proc -> {name, campaign, started_at, deadline_at} (epoch seconds)
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
    summary: dict | None = None  # SPEC-0284: parsed counts/failing ids; None without `results:`
    attempts: tuple = ()        # SPEC-0289: one dict per attempt once a retry ran (attempt 0 first)
    flaky: tuple = ()           # SPEC-0289: ids a targeted rerun passed, when the gate ended ok
    report: dict | None = None  # SPEC-0359: the parsed `NW_GATE_REPORT` (< REPORT_MAX_BYTES)
    infra_error: str | None = None  # SPEC-0387: the host could not write the gate's log/sidecar


@dataclass(frozen=True)
class GateRun:
    seq: int
    ok: bool
    results: tuple[GateResult, ...]
    started_at: str
    duration_s: float
    interrupted: bool = False   # SPEC-0229: a gate was ended by a stop, or the run was cut short
    slot_wait_s: float = 0.0    # SPEC-0309: seconds spent waiting for a gate slot (0.0: none)
    worker: str | None = None   # SPEC-0338: the worker the gates ran on; None: locally
    head: str | None = None     # SPEC-0359: worktree HEAD when the run started
    tree: str | None = None     # SPEC-0359: worktree HEAD^{tree} when the run started
    reused: tuple = ()          # SPEC-0359: close gates a green loop gate stood in for
    job_id: str | None = None   # SPEC-0341: the async job's record (`remote_jobs`); None: none
    event_uid: str | None = None    # SPEC-0341: the `gate` event's uid (`<job_id>:<n of done>`)
    changed: tuple | None = None    # SPEC-0368: paths the gates wrote in the worker's checkout;
    #                                 None on a local run


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


def live_deadlines() -> list[dict]:
    """SPEC-0392: the gates running right now, each `{name, campaign, started_at, deadline_at}`
    (epoch seconds; `deadline_at` = start + `timeout_s`). Empty when none run."""
    with _LOCK:
        return [dict(v) for v in _LIVE.values()]


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
    """Environment every gate (and anything it calls) sees: the same nightwright store the loop uses.
    With `seq` the gate run also gets its own `TMPDIR` under the campaign dir (SPEC-0253).
    SPEC-0420: a `full_suite` campaign (a red-base fix) adds `NW_AFFECTED_FULL=1`, so the
    affected-tests selector runs the whole suite in its loop and close gates."""
    repo = state.get("repo") or {}
    tmp = tmp_env(store.child_tmp(cfg, state["id"], f"gate-{seq:03d}")) \
        if seq is not None and state.get("id") else {}
    return {**tmp,
        "NW_CAMPAIGN": str(state.get("id") or ""),
        "NW_DATA_DIR": str(cfg["data_dir"]),
        "NW_CONFIG_DIR": str(cfg.get("config_dir") or ""),
        "NW_SPEC_ID": str(state.get("spec_id") or ""),
        "NW_WORKTREE": str(repo.get("worktree") or ""),
        "NW_BASE": str(repo.get("base") or ""),
        **full_suite_env(state),
    }


def full_suite_env(state: dict) -> dict[str, str]:
    """SPEC-0420: `{"NW_AFFECTED_FULL": "1"}` for a `full_suite` campaign, else `{}`; the remote
    gate and persona job headers carry the same key."""
    return {"NW_AFFECTED_FULL": "1"} if (state or {}).get("full_suite") else {}


def placeholders(state: dict) -> dict:
    repo = state.get("repo") or {}
    return {"spec_id": state.get("spec_id"), "campaign": state.get("id"),
            "worktree": repo.get("worktree"), "base": repo.get("base")}


def _env_s(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return float(default)


def _grace_s() -> float:
    return _env_s("NW_GATE_GRACE_S", _KILL_GRACE_S)


def _reap_s() -> float:
    return _env_s("NW_GATE_REAP_S", REAP_BOUND_S)


def hard_deadline_s() -> float:
    """Longest a timeout kill may take before `run_gate` stops waiting for it (SPEC-0392)."""
    return 2 * _grace_s() + _reap_s() + HARD_SLACK_S


def _stat(pid: int):
    """`(state, ppid, pgrp, start_ticks)` from `/proc/<pid>/stat`; None when gone / off Linux."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        f = raw.rsplit(")", 1)[1].split()
        return f[0], int(f[1]), int(f[2]), int(f[19])
    except (OSError, IndexError, ValueError):
        return None


def _proc_table() -> dict:
    table = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return table
    for n in names:
        if n.isdigit() and (st := _stat(int(n))) is not None:
            table[int(n)] = st
    return table


def _pgid(proc: subprocess.Popen) -> int:
    try:
        return os.getpgid(proc.pid)
    except OSError:
        return proc.pid         # the shell leads its own session, so its pid is the group


def _tree_pids(proc: subprocess.Popen) -> set:
    """Snapshot of the gate's process tree as `{(pid, start_ticks)}`: the shell's process group
    united with every descendant reachable through parent links, so a child that called `setsid`
    is found while its parent lives. A descendant that left the group *and* lost its ancestor
    chain before the snapshot (a double-fork daemon, reparented to init) is not found: a known
    limit, left to a cgroup backend. Off Linux it degrades to the shell alone (the group kill)."""
    table = _proc_table()
    if not table:
        return {(proc.pid, None)}
    pgid = _pgid(proc)
    kids: dict = {}
    for pid, (_s, ppid, _g, _t) in table.items():
        kids.setdefault(ppid, []).append(pid)
    todo = [pid for pid, (_s, _p, pg, _t) in table.items() if pg == pgid or pid == proc.pid]
    seen = set(todo)
    while todo:
        for kid in kids.get(todo.pop(), ()):
            if kid not in seen:
                seen.add(kid)
                todo.append(kid)
    return {(pid, table[pid][3]) for pid in seen}


def _present(pid: int, ticks) -> bool:
    """The snapshotted process is still there: same start ticks (a recycled pid is someone
    else's) and not a zombie (dead, only unreaped)."""
    if ticks is None:
        return False
    st = _stat(pid)
    return st is not None and st[0] != "Z" and runproc.start_ticks(pid) == ticks


def _signal_tree(snap: set, pgid: int, sig: int) -> None:
    members = [pid for pid, ticks in snap if _present(pid, ticks)]
    if not members:
        return
    if any((st := _stat(pid)) is not None and st[2] == pgid for pid in members):
        try:
            os.killpg(pgid, sig)
        except OSError:
            pass
    for pid in members:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def _survivors(proc: subprocess.Popen, snap: set) -> list[int]:
    proc.poll()                                     # reap the shell so it stops counting
    return sorted(pid for pid, ticks in snap if _present(pid, ticks))


def _wait_gone(proc: subprocess.Popen, snap: set, bound_s: float) -> list[int]:
    end = time.monotonic() + bound_s
    while True:
        left = _survivors(proc, snap)
        if not left or time.monotonic() >= end:
            return left
        time.sleep(0.05)


def _kill_tree(proc: subprocess.Popen) -> list[int]:
    """SPEC-0392: end the gate's whole process tree. TERM it (group and every snapshotted
    pid), wait for *all* of it, not just the shell, then KILL whatever is left, even when the
    shell already exited, and wait a bounded time for it to go. Returns the pids that outlived
    that (empty normally); they are never waited on again."""
    pgid = _pgid(proc)
    snap = _tree_pids(proc)
    if snap == {(proc.pid, None)}:                  # off Linux: the group kill, as before
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except OSError:
                break
            try:
                proc.wait(timeout=_grace_s())
                break
            except subprocess.TimeoutExpired:
                pass
        return []
    _signal_tree(snap, pgid, signal.SIGTERM)
    if _wait_gone(proc, snap, _grace_s()):
        snap |= _tree_pids(proc)                    # children spawned during the grace
        _signal_tree(snap, pgid, signal.SIGKILL)
        left = _wait_gone(proc, snap, _reap_s())
    else:
        left = []
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    return left


def _kill_group(proc: subprocess.Popen) -> list[int]:
    """Kill the gate's process tree (name kept for `terminate_all` and SPEC-0229's stop path)."""
    return _kill_tree(proc)


def _wait_deadline(proc: subprocess.Popen, deadline: float) -> int | None:
    """Exit code, or None once the monotonic `deadline` passes. Short slices, so no single
    blocking call stands between the gate and its deadline."""
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return proc.poll()
        try:
            return proc.wait(timeout=min(1.0, left))
        except subprocess.TimeoutExpired:
            pass


def _timeout_kill(proc: subprocess.Popen, timeout_s: int) -> tuple[int | None, str]:
    """Kill an overrun gate under `hard_deadline_s()`: the kill runs in a daemon thread and
    `run_gate` stops waiting for it at the bound (SPEC-0392). -> (exit code, sidecar error)."""
    out: list = []
    th = threading.Thread(target=lambda: out.append(_kill_tree(proc)), daemon=True,
                          name="gate-kill")
    th.start()
    bound = hard_deadline_s()
    th.join(bound)
    if th.is_alive():
        return None, f"timed out after {timeout_s}s; kill did not complete within {bound:g}s"
    error = f"timed out after {timeout_s}s; process group killed"
    if out and out[0]:
        error += f"; survivors: {' '.join(map(str, out[0]))}"
    return proc.returncode, error


def _write_sidecar(log_path: Path, result: GateResult, *, timeout_s: int, started_at: str,
                   error: str | None, attempt: int | None = None) -> None:
    """`gates/NNN-name.json` (`nw.gate.v1`, SPEC-0192): what nightwright knows about the run; the
    `.log` beside it holds only the command's own output."""
    rec = {"schema": SC.GATE_SCHEMA, "name": result.name, "cmd": result.cmd, "cwd": result.cwd,
           "required": result.required, "ok": result.ok, "exit_code": result.exit_code,
           "timed_out": result.timed_out, "timeout_s": timeout_s, "started_at": started_at,
           "duration_s": result.duration_s, "log": log_path.name, "error": error}
    if result.interrupted:
        rec["interrupted"] = True
    if result.summary is not None:
        rec["summary"] = result.summary
    if attempt is not None:                 # SPEC-0289: a retry's own sidecar
        rec["attempt"] = attempt
    if result.attempts:
        rec["attempts"] = list(result.attempts)
    if result.flaky:
        rec["flaky"] = list(result.flaky)
    log_path.with_suffix(".json").write_text(json.dumps(rec, indent=2, ensure_ascii=False)
                                             + "\n", encoding="utf-8")


def run_gate(gate: dict, worktree, env: dict, log_path, placeholders: dict, *,
             log_rel: str | None = None, cmd_override: str | None = None,
             attempt: int | None = None, inherit_env: bool = True) -> GateResult:
    """Run one gate in `worktree / gate["cwd"]`, combined output to `log_path`, timeout
    `gate["timeout_s"]` (kills the whole process group). `ok` = exit code 0. The log is the
    command's output only; command, cwd, exit, timing and errors go to the JSON sidecar.
    `cmd_override` (SPEC-0289, a targeted rerun) replaces the expanded `cmd`; `attempt` marks
    a retry's sidecar."""
    cmd = cmd_override if cmd_override is not None else expand_cmd(gate["cmd"], **placeholders)
    cwd = Path(worktree) / gate.get("cwd", ".")
    timeout_s = int(gate.get("timeout_s", 1200))
    log_path = Path(log_path)
    started_at = store.now_iso()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        report_path = report_path_for(log_path)         # SPEC-0359
        report_path.unlink(missing_ok=True)             # a leftover never stands for this run
        log = open(log_path, "w", encoding="utf-8")
    except OSError as e:                            # SPEC-0387: a full disk is the host's, not a crash
        return _infra_result(gate, cmd, cwd, log_path, log_rel, e, timeout_s, started_at,
                             attempt)
    # SPEC-0346: a worker gate sets `_GATE_INHERIT_ENV` false (or passes `inherit_env=False`).
    # The env dict is already the allowset (PATH, HOME, the header keys, the worker's own).
    # Inheriting the process env would hand the gate sshd's and the job lock's keys.
    base = os.environ if inherit_env and _GATE_INHERIT_ENV.get() else {}
    child_env = {**base, **env, "NW_GATE_REPORT": str(report_path)}
    started = time.monotonic()
    started_ns = time.time_ns()     # SPEC-0284: a junit file older than this is a leftover
    timed_out = False
    error = None
    exit_code = None
    interrupted = False
    with log:
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
                    t_wall = time.time()
                    _LIVE[proc] = {"name": gate["name"], "campaign": env.get("NW_CAMPAIGN") or None,
                                   "started_at": t_wall, "deadline_at": t_wall + timeout_s}
        except OSError as e:
            error = f"failed to start: {e}"
        if proc is not None:
            try:
                exit_code = _wait_deadline(proc, started + timeout_s)
                if exit_code is None:
                    timed_out = True
                    exit_code, error = _timeout_kill(proc, timeout_s)
            finally:
                with _LOCK:
                    _LIVE.pop(proc, None)
                    stopping = _stopping
            if stopping and not timed_out and exit_code != 0:
                interrupted = True
                error = "interrupted: the runner is stopping; process group killed"
    duration = round(time.monotonic() - started, 3)
    ok = proc is not None and exit_code == 0 and not timed_out
    summary = None
    if proc is not None and not interrupted:        # SPEC-0284: never decides `ok`
        summary = gate_results.parse(gate.get("results"), log_path, cwd, started_ns)
    result = GateResult(gate["name"], bool(gate.get("required", True)), ok, exit_code,
                        timed_out, duration, log_rel or str(log_path), tail(log_path),
                        cmd=cmd, cwd=str(cwd), interrupted=interrupted, summary=summary,
                        report=read_report(report_path))
    try:
        _write_sidecar(log_path, result, timeout_s=timeout_s, started_at=started_at, error=error,
                       attempt=attempt)
    except OSError as e:                            # SPEC-0387: the run happened; say it is unrecorded
        result = replace(result, ok=False, infra_error=f"could not write {e.filename or 'the sidecar'}: {e}")
    return result


def _infra_result(gate, cmd, cwd, log_path, log_rel, e: OSError, timeout_s, started_at,
                  attempt) -> GateResult:
    """SPEC-0387: the gate never ran because its log could not be created (typically ENOSPC):
    a failed result carrying `infra_error`, so the caller still emits its frame and `done`."""
    result = GateResult(gate["name"], bool(gate.get("required", True)), False, None, False, 0.0,
                        log_rel or str(log_path), "", cmd=cmd, cwd=str(cwd),
                        infra_error=f"could not create {e.filename or log_path}: {e}")
    try:
        _write_sidecar(log_path, result, timeout_s=timeout_s, started_at=started_at,
                       error=result.infra_error, attempt=attempt)
    except OSError:
        pass                                        # the disk is still full: the frame has it
    return result


def report_path_for(log_path) -> Path:
    """SPEC-0359: `gates/<seq:03>-<name>.report.json` beside the gate's log."""
    log_path = Path(log_path)
    return log_path.with_name(f"{log_path.stem}.report.json")


def read_report(path) -> dict | None:
    """The gate's selection report (SPEC-0357 `NW_GATE_REPORT`), parsed; None when absent,
    unreadable, not a JSON object or `REPORT_MAX_BYTES` or larger (SPEC-0359)."""
    try:
        path = Path(path)
        if path.stat().st_size >= REPORT_MAX_BYTES:
            return None
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def head_tree(worktree) -> tuple[str | None, str | None]:
    """SPEC-0359: the worktree's `HEAD` and `HEAD^{tree}` (None, None when git cannot say)."""
    try:
        r = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD", "HEAD^{tree}"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    out = r.stdout.split()
    if r.returncode != 0 or len(out) != 2:
        return None, None
    return out[0], out[1]


def targeted_cmd(gate: dict, cmd: str, failing, placeholders: dict | None = None) -> str:
    """SPEC-0289: the command that re-runs only `failing`: the gate's `retry_cmd` with
    `{failing}` replaced, else the expanded `cmd` followed by the ids. Ids are shell-quoted."""
    ids = " ".join(shlex.quote(i) for i in failing)
    rc = gate.get("retry_cmd")
    if rc:
        return expand_cmd(rc, **(placeholders or {})).replace("{failing}", ids)
    return f"{cmd} {ids}"


def _targeted_ids(gate: dict, summary: dict | None, cmd: str) -> list[str]:
    """The failing ids a targeted rerun may use: pytest results, 1..MAX_FAILING ids, and the
    list accounts for every failure and error (so it is complete). Without a `retry_cmd` the
    ids are appended to the expanded `cmd` (the current round's, never a previous result's),
    which only works when it has no shell operators (a pipe or redirect would hand the ids to
    the wrong program). SPEC-0358: `summary` may come from a previous round's result."""
    if (gate.get("results") or {}).get("format") != "pytest":
        return []
    if not gate.get("retry_cmd") and _SHELL_OPS_RE.search(cmd):
        return []
    s = summary
    if not isinstance(s, dict) or s.get("error"):
        return []
    failing = list(s.get("failing") or [])
    if not 0 < len(failing) <= gate_results.MAX_FAILING \
            or (s.get("failed") or 0) + (s.get("errors") or 0) != len(failing):
        return []
    return failing


def _attempt(n: int, kind: str, r: GateResult) -> dict:
    failing = list((r.summary or {}).get("failing") or [])
    rec = {"attempt": n, "kind": kind, "ok": r.ok, "exit_code": r.exit_code,
           "timed_out": r.timed_out, "interrupted": r.interrupted,
           "duration_s": r.duration_s, "log": r.log, "failing": failing}
    return rec


def _stop_requested(stop) -> bool:
    return (stop is not None and stop.is_set()) or _stopping


def _previous_ids(gate: dict, previous: dict | None, cmd: str) -> list[str]:
    """SPEC-0358: the ids a `failed_first` prelude runs: those of the previous round's result for
    this gate when it was red (not interrupted, not timed out) with a complete pytest list.
    Reads only the previous entry's `summary`; `cmd` is the current round's expanded command."""
    if not gate.get("failed_first") or not isinstance(previous, dict):
        return []
    if previous.get("ok") or previous.get("interrupted") or previous.get("timed_out"):
        return []
    return _targeted_ids(gate, previous.get("summary"), cmd)


def run_gate_with_retries(gate: dict, worktree, env: dict, log_path, placeholders: dict, *,
                          log_rel: str | None = None, stop: threading.Event | None = None,
                          deadline: float | None = None,
                          previous: dict | None = None,
                          inherit_env: bool = True) -> GateResult:
    """`run_gate` with the SPEC-0358 `failed_first` prelude, then the SPEC-0289 ladder.

    With `gate["failed_first"]` and a qualifying `previous` result (see `_previous_ids`) the round
    starts by running the previous failing ids (attempt kind `failed-first`, log `*.ff.log`).
    A red prelude whose exit code is not in TARGETED_UNRESOLVED_EXITS reproduced the failure and
    is the gate's result: no main run, no ladder. A stop during it gives an interrupted result.
    Otherwise (green, timed out, ids not runnable) the normal run and ladder follow, with the
    prelude first in `attempts`.
    """
    log_path = Path(log_path)
    ids = _previous_ids(gate, previous, expand_cmd(gate["cmd"], **placeholders))
    if not ids or _stop_requested(stop):
        return _run_ladder(gate, worktree, env, log_path, placeholders, log_rel=log_rel,
                           stop=stop, deadline=deadline, inherit_env=inherit_env)
    ppath = log_path.with_name(f"{log_path.stem}.ff.log")
    prel = f"{Path(log_rel).with_suffix('').as_posix()}.ff.log" if log_rel else str(ppath)
    cmd = targeted_cmd(gate, expand_cmd(gate["cmd"], **placeholders), ids, placeholders)
    pre = run_gate(gate, worktree, env, ppath, placeholders, log_rel=prel, cmd_override=cmd,
                   attempt=-1, inherit_env=inherit_env)
    rec = _attempt(-1, "failed-first", pre)
    if pre.interrupted or (not pre.ok and not pre.timed_out
                           and pre.exit_code not in TARGETED_UNRESOLVED_EXITS):
        return replace(pre, attempts=(rec,))        # reproduced, or cut by a stop (SPEC-0229)
    if _stop_requested(stop):                       # a stop between the prelude and the main run
        return replace(pre, ok=False, interrupted=True, attempts=(rec,))
    final = _run_ladder(gate, worktree, env, log_path, placeholders, log_rel=log_rel,
                        stop=stop, deadline=deadline, inherit_env=inherit_env)
    attempts = (rec, *(final.attempts or (_attempt(0, "full", final),)))
    final = replace(final, attempts=attempts,
                    duration_s=round(final.duration_s + pre.duration_s, 3))
    try:
        error = json.loads(log_path.with_suffix(".json").read_text(encoding="utf-8")).get("error")
    except (OSError, ValueError):
        error = None
    _rewrite_sidecar(log_path, final, error)
    return final


def _run_ladder(gate: dict, worktree, env: dict, log_path, placeholders: dict, *,
                log_rel: str | None = None, stop: threading.Event | None = None,
                deadline: float | None = None, inherit_env: bool = True) -> GateResult:
    """`run_gate` plus the SPEC-0289 retry ladder (`gate["retries"]`).

    A failing pytest gate with a complete failing-id list first re-runs only those ids: a
    failure there is reproduced and ends the ladder; a pass proves nothing about the rest of the
    suite, so only a *full* rerun that passes can turn the gate green. Timed-out, interrupted
    and stopped gates are never retried. The result is attempt 0's with the verdict (last full
    attempt) `ok`/`exit_code`/`tail`/`summary`, the last attempt's `interrupted`/`timed_out`,
    the summed `duration_s`, plus `attempts` and `flaky`. `deadline` (a `time.monotonic()` value)
    keeps the ladder inside the runner's busy budget: a full rerun does not start when it
    would, going by attempt 0's duration, end after it. A targeted rerun that exits with a
    pytest usage/no-tests code never ran the ids, so it is not "reproduced": the ladder goes on."""
    first = run_gate(gate, worktree, env, log_path, placeholders, log_rel=log_rel,
                     inherit_env=inherit_env)
    retries = int(gate.get("retries") or 0)
    if first.ok or first.interrupted or first.timed_out or retries <= 0:
        return first
    ids = _targeted_ids(gate, first.summary, first.cmd)
    attempts = [_attempt(0, "full", first)]
    verdict = last = first
    targeted_passed = stopped = False
    for n in range(1, retries + 1):
        if _stop_requested(stop):
            stopped = True
            break
        kind = "targeted" if (n == 1 and ids) else "full"
        if kind == "full" and deadline is not None \
                and time.monotonic() + first.duration_s > deadline:
            break
        rpath = log_path.with_name(f"{log_path.stem}.r{n}.log")
        rrel = (f"{Path(log_rel).with_suffix('').as_posix()}.r{n}.log" if log_rel
                else str(rpath))
        cmd = targeted_cmd(gate, first.cmd, ids, placeholders) if kind == "targeted" else None
        r = run_gate(gate, worktree, env, rpath, placeholders, log_rel=rrel,
                     cmd_override=cmd, attempt=n, inherit_env=inherit_env)
        attempts.append(_attempt(n, kind, r))
        last = r
        if r.interrupted or r.timed_out:
            if kind == "full":
                verdict = r
            break
        if kind == "targeted":
            if r.exit_code in TARGETED_UNRESOLVED_EXITS:    # the ids never ran: inconclusive
                continue
            if not r.ok:                # reproduced: the failure is real
                break
            targeted_passed = True
            continue
        verdict = r
        if r.ok:
            break
    if len(attempts) == 1:              # no retry started
        if not stopped:
            return first
        # a stop cut the ladder: the gate is cut by it, not a failure to triage (SPEC-0229)
        final = replace(first, interrupted=True)
        _rewrite_sidecar(log_path, final, "interrupted: the runner is stopping")
        return final
    flaky = tuple(ids) if (verdict.ok and targeted_passed) else ()
    final = replace(first, ok=verdict.ok, exit_code=verdict.exit_code, tail=verdict.tail,
                    summary=verdict.summary, interrupted=last.interrupted or stopped,
                    timed_out=last.timed_out,
                    duration_s=round(sum(a["duration_s"] for a in attempts), 3),
                    attempts=tuple(attempts), flaky=flaky)
    # the error of the verdict attempt, or of a later retry that was cut short
    err_attempt = last if (last.interrupted or last.timed_out) else verdict
    n_err = next(a["attempt"] for a in reversed(attempts) if a["log"] == err_attempt.log)
    err_path = log_path if n_err == 0 else log_path.with_name(f"{log_path.stem}.r{n_err}.log")
    try:
        error = json.loads(err_path.with_suffix(".json").read_text(encoding="utf-8")).get("error")
    except (OSError, ValueError):
        error = None
    _rewrite_sidecar(log_path, final, error)
    return final


def _rewrite_sidecar(log_path: Path, final: GateResult, error: str | None) -> None:
    """Attempt 0's sidecar becomes the gate's final result (the same values as the `gate`
    ledger event); attempt 0's raw values live on in `attempts[0]`. Best effort: an unreadable
    sidecar is left as it is, and the rewrite goes through a temp file so it is never torn."""
    side = log_path.with_suffix(".json")
    try:
        rec = json.loads(side.read_text(encoding="utf-8"))
        rec.update(ok=final.ok, exit_code=final.exit_code, timed_out=final.timed_out,
                   duration_s=final.duration_s, error=error)
        for key, val in (("interrupted", final.interrupted or None),
                         ("summary", final.summary), ("attempts", list(final.attempts) or None),
                         ("flaky", list(final.flaky) or None)):
            if val is None:
                rec.pop(key, None)
            else:
                rec[key] = val
        tmp = side.with_name(side.name + ".tmp")
        tmp.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, side)
    except (OSError, ValueError):
        pass


def next_gate_seq(cfg, campaign_id) -> int:
    """1 + the highest `NNN-` prefix among existing gate logs (so debug runs and loop runs
    never overwrite each other). SPEC-0341: also past every remote job record of the campaign
    (final ones included), so a job id the worker may still hold is never reused."""
    return max(next_gate_seq_in(store.gates_dir(cfg, campaign_id)),
               RJ.max_seq(cfg, campaign_id) + 1)


def next_gate_seq_in(d) -> int:
    """1 + the highest `NNN-` prefix among the files of directory `d` (1 when none)."""
    d = Path(d)
    if not d.is_dir():
        return 1
    seqs = [int(m.group(1)) for p in d.iterdir() if (m := _LOG_SEQ_RE.match(p.name))]
    return (max(seqs) + 1) if seqs else 1


def _claim_seq(cfg, cid, seq) -> tuple[int, str]:
    """SPEC-0253: `next_gate_seq` only sees logs, which appear once the first gate starts, so a
    concurrent `nightwright gate run` can pick the same seq. Claim `gate-<seq>` atomically and take
    the next free seq on a collision, so two runs never share (and wipe) one TMPDIR."""
    while True:
        owner = f"gate-{seq:03d}"
        try:
            store.child_tmp(cfg, cid, owner, exclusive=True)
            return seq, owner
        except FileExistsError:
            seq += 1


def run_all(project_cfg: dict, campaign_state: dict, cfg, *, seq: int,
            stop: threading.Event | None = None, tier: str | None = None,
            worker: str | None = None, local: bool = False,
            adopt: dict | None = None, lost: dict | None = None) -> GateRun:
    """Run every gate of `project_cfg` in the campaign's worktree; logs land at
    `gates/<seq:03>-<name>.log` under the campaign dir. `ok` = every *required* gate ok.
    The returned `GateRun.seq` is the seq actually used (higher than `seq` if it was taken).
    SPEC-0309: the whole run holds one host-wide gate slot; the budget, the gates' timeouts and
    `GateRun.duration_s` count from the moment the slot is held.
    SPEC-0328: `tier` (`loop` | `close`) runs only that tier's gates plus `always` ones;
    None runs every gate.
    SPEC-0338: the seq is claimed before the slot, and the slot comes from the registered
    workers' pools first (`remote_gates.pools_for`; `worker` names one, `local` uses none). A
    remote hold runs on the worker; a transport failure re-runs the gates locally under the
    same seq. Without an enabled worker this is the local run it always was.
    SPEC-0341: `adopt` is the campaign's live remote job record (`seq` is its seq): the run
    re-enters that job (`remote_gates.adopt`) instead of starting one; giving up on it falls
    back locally as a transport failure does.
    SPEC-0342: a give-up retries the job (`_retry`): attempt + 1 on a worker no earlier attempt
    was lost on, local once `max_remote_attempts` is used up. `lost` is a `lost` record whose
    next attempt a crash never wrote (`seq` is its seq): the run continues that chain."""
    cid = campaign_state["id"]
    project_cfg = {**project_cfg, "gates": P.gates_for(project_cfg, tier)}
    worktree = (campaign_state.get("repo") or {}).get("worktree")
    if not worktree:
        raise ValueError(f"campaign {cid} has no worktree recorded")

    def announce(occ, waited_s):
        store.append_event(cfg, cid, "gate_waiting",
                           {"slots": occ["slots"], "held": occ["held"], "waited_s": waited_s},
                           by="gates")

    if adopt is None and lost is None:
        seq, owner = _claim_seq(cfg, cid, seq)
    else:                                       # the dead runner's claim may still be there
        owner = f"gate-{seq:03d}"
        store.child_tmp(cfg, cid, owner, create=True)
    retry = dict(project_cfg=project_cfg, campaign_state=campaign_state, cfg=cfg, cid=cid,
                 worktree=worktree, seq=seq, owner=owner, stop=stop, tier=tier,
                 announce=announce, worker=worker)
    try:                                        # SPEC-0253: only the owner removes its dir
        pools = skip = repool = None
        if adopt is not None or lost is not None:
            from . import remote_gates as RG
            RJ.settings(cfg)                    # SPEC-0342: a bad max_remote_attempts raises
            head, tree = head_tree(worktree)    # SPEC-0359: adoption requires the same tree
            if lost is not None:                # SPEC-0342: the crash left no next attempt
                for p in store.gates_dir(cfg, cid).glob(f"{seq:03d}-*"):
                    p.unlink(missing_ok=True)
                return _retry(lost, 0.0, head=head, tree=tree, **retry)
            out, held_s = RG.adopt(adopt, project_cfg, campaign_state, cfg, seq, stop, announce)
            waited = round(held_s + out.worker_wait_s, 1)
            if out.run is not None:
                return replace(out.run, slot_wait_s=waited, head=head, tree=tree)
            if out.lost is not None:
                return _retry(out.lost, waited, head=head, tree=tree, **retry)
            return _fallback(project_cfg, campaign_state, cfg, cid, worktree, seq, stop, tier,
                             announce, waited, head=head, tree=tree)
        if not local:
            from . import remote_gates as RG    # lazy: remote_gates imports this module
            pools = RG.pools_for(cfg, project_cfg, campaign_state, seq=seq, worker=worker)
            if pools is not None:
                RJ.settings(cfg)                # SPEC-0342: a bad max_remote_attempts raises
            if worker is None:                  # SPEC-0343: a worker drained mid-wait is passed
                from . import workers as WK     # over (`--worker` has no other pool to take)
                skip = lambda p: not WK.placeable_by_name(cfg, p)   # noqa: E731
                if pools is not None:           # SPEC-0374: re-read placement while waiting
                    repool = lambda: WK.gate_pools(cfg)                 # noqa: E731
        with gate_slots.acquire(cfg, cid, stop=stop, on_wait=announce, pools=pools,
                                skip=skip, repool=repool) as hold:
            if hold.interrupted:                # SPEC-0229: stopped while waiting, nothing ran (ok=False: never counted green)
                return GateRun(seq, False, (), store.now_iso(), 0.0, interrupted=True,
                               slot_wait_s=hold.waited_s)
            head, tree = head_tree(worktree)        # SPEC-0359: the tree this run gates
            if hold.pool is None:
                run = _run_all_held(project_cfg, campaign_state, cfg, cid, worktree, seq, stop,
                                    tier)
                return replace(run, slot_wait_s=hold.waited_s, head=head, tree=tree)
            out = RG.run(hold.pool, project_cfg, campaign_state, cfg, seq, owner, stop,
                         tier=tier)
        waited = round(hold.waited_s + out.worker_wait_s, 1)
        if out.run is not None:
            return replace(out.run, slot_wait_s=waited, head=head, tree=tree)
        if out.lost is not None:
            return _retry(out.lost, waited, head=head, tree=tree, **retry)
        if out.failure is not None and out.failure.rejected:    # SPEC-0367: say it loudly
            render.warn(RG.rejection_notice(cfg, hold.pool, out.failure))
        return _fallback(project_cfg, campaign_state, cfg, cid, worktree, seq, stop, tier,
                         announce, waited, head=head, tree=tree)
    finally:
        store.clear_child_tmp(cfg, cid, owner)


def _retry(lost, waited, *, project_cfg, campaign_state, cfg, cid, worktree, seq, owner, stop,
           tier, announce, worker, head, tree) -> GateRun:
    """SPEC-0342: the lost job's next attempts (the remote slot is already released). Each
    gets `attempt + 1` and `excluded_workers` = the lost record's plus its worker, and acquires
    from `remote_gates.retry_pools` (the eligible pools without them; SPEC-0374: local only
    when no other worker is placeable, or last with `agentic.gates.local_overflow`), and
    re-reads that placement while it waits. Past `max_remote_attempts`, or with no remote pool left, it
    runs locally. Every new attempt appends `worker_job_retried` (`to_worker` None: local); a
    remote one appends it only once the job is about to be sent (`on_attempt`), so a stop
    before that leaves the record `lost` with no event and the next step continues the chain.
    A retry sent to a worker without `async` (no record, its `-a0` id in the event) sets the lost
    record `local` before it is sent: it is the chain's last attempt.
    A local run, including a transport failure's fallback (SPEC-0338, no further retry), sets
    the lost record `local` first, so a crash during it never restarts remote attempts."""
    from . import remote_gates as RG
    limit = RJ.settings(cfg)["max_remote_attempts"]
    while True:
        attempt = int(lost.get("attempt") or 1) + 1
        # SPEC-0334: a `rotated` loss is not charged (no cooldown, no recorded loss), but its
        # worker is still excluded (it exits on its next 401 until re-enrolled) and the
        # attempt still counts: the SPEC-0342 retry budget is the same for every reason
        excluded = list(dict.fromkeys(
            [w for w in [*(lost.get("excluded_workers") or []), lost.get("worker")] if w]))
        pools = None
        if attempt <= limit:
            pools = RG.retry_pools(cfg, project_cfg, campaign_state, seq=seq, excluded=excluded,
                                   worker=worker)
        if pools is None:
            RJ.update(cfg, lost["job_id"], state="local")
            RG.retried(cfg, cid, lost, attempt, None)
            return _fallback(project_cfg, campaign_state, cfg, cid, worktree, seq, stop, tier,
                             announce, waited, head=head, tree=tree)
        repool = None
        if worker is None:                      # SPEC-0374: re-read placement while waiting
            from . import workers as WK
            repool = lambda ex=tuple(excluded): WK.gate_pools(cfg, exclude=ex)  # noqa: E731
        with gate_slots.acquire(cfg, cid, stop=stop, on_wait=announce, pools=pools,
                                repool=repool) as hold:
            waited = round(waited + hold.waited_s, 1)
            if hold.interrupted:                # the record stays `lost`: the next step retries
                return GateRun(seq, False, (), store.now_iso(), 0.0, interrupted=True,
                               slot_wait_s=waited)
            if hold.pool is None:               # no eligible worker left (or overflow): local
                RJ.update(cfg, lost["job_id"], state="local")
                RG.retried(cfg, cid, lost, attempt, None)
                run = _run_all_held(project_cfg, campaign_state, cfg, cid, worktree, seq, stop,
                                    tier)
                return replace(run, slot_wait_s=waited, head=head, tree=tree)
            sent = []

            def on_attempt(job_id, recorded, lost=lost, attempt=attempt, to=hold.pool):
                sent.append(to)
                if not recorded:                # a `gate-job` worker: no record of its own
                    # and never retried (its failure is a transport failure), so the chain
                    # ends here: a stop during it starts afresh instead of re-sending
                    RJ.update(cfg, lost["job_id"], state="local")
                RG.retried(cfg, cid, lost, attempt, to, job_id=job_id)
            out = RG.run(hold.pool, project_cfg, campaign_state, cfg, seq, owner, stop,
                         tier=tier, attempt=attempt, excluded=excluded, on_attempt=on_attempt)
        waited = round(waited + out.worker_wait_s, 1)
        if out.run is not None:
            return replace(out.run, slot_wait_s=waited, head=head, tree=tree)
        if out.lost is None:                    # a transport failure: local, no further retry
            RJ.update(cfg, lost["job_id"], state="local")
            if not sent:                        # nothing was sent: this local run is the attempt
                RG.retried(cfg, cid, lost, attempt, None)
            if out.failure is not None and out.failure.rejected:    # SPEC-0367
                render.warn(RG.rejection_notice(cfg, hold.pool, out.failure))
            return _fallback(project_cfg, campaign_state, cfg, cid, worktree, seq, stop, tier,
                             announce, waited, head=head, tree=tree)
        lost = out.lost


def _fallback(project_cfg, campaign_state, cfg, cid, worktree, seq, stop, tier, announce,
              waited, *, head=None, tree=None) -> GateRun:
    """SPEC-0338 fallback: the remote slot is released; a local one, under the same claim."""
    with gate_slots.acquire(cfg, cid, stop=stop, on_wait=announce) as fb:
        waited = round(waited + fb.waited_s, 1)
        if fb.interrupted:
            return GateRun(seq, False, (), store.now_iso(), 0.0, interrupted=True,
                           slot_wait_s=waited)
        run = _run_all_held(project_cfg, campaign_state, cfg, cid, worktree, seq, stop, tier)
    return replace(run, slot_wait_s=waited, head=head, tree=tree)


def _run_all_held(project_cfg, campaign_state, cfg, cid, worktree, seq, stop, tier) -> GateRun:
    """The local run under a held slot and the caller's `gate-<seq>` claim."""
    env = gate_env(cfg, campaign_state, seq)
    ph = placeholders(campaign_state)
    cdir = store.campaign_dir(cfg, cid)
    started_at = store.now_iso()
    t0 = time.monotonic()
    results = []
    cut = False
    from .events import settings as events_settings     # lazy: events imports the queue lazily
    deadline = t0 + float(events_settings(cfg)["gate_budget_s"])   # SPEC-0289: retries stay in it
    # SPEC-0358: the loop tier hands each `failed_first` gate its previous same-name result
    # (`last_gate` is the latest run of either tier; a close run's entries have other names)
    previous = {}
    if tier == "loop":
        for r in (campaign_state.get("last_gate") or {}).get("results") or []:
            if isinstance(r, dict) and r.get("name"):
                previous[r["name"]] = r
    for gate in project_cfg.get("gates") or []:
        if stop is not None and stop.is_set():      # SPEC-0229: no next gate after a stop
            cut = True
            break
        rel = f"gates/{seq:03d}-{gate['name']}.log"
        results.append(run_gate_with_retries(gate, worktree, env, cdir / rel, ph,
                                             log_rel=rel, stop=stop, deadline=deadline,
                                             previous=(previous.get(gate["name"])
                                                       if gate.get("failed_first") else None)))
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
        if r["summary"] is None:          # SPEC-0284: only recorded with `results:`
            del r["summary"]
        if r["infra_error"] is None:      # SPEC-0387: only recorded when the host failed
            del r["infra_error"]
        if r["report"] is None:           # SPEC-0359: only recorded when the gate wrote one
            del r["report"]
        for key in ("attempts", "flaky"):     # SPEC-0289: only recorded when a retry ran
            if not r[key]:
                del r[key]
            else:
                r[key] = list(r[key])
        if not r.pop("interrupted"):      # SPEC-0229: only recorded when true
            continue
        r["interrupted"] = True
    out = {"gate_seq": run.seq, "ok": run.ok, "started_at": run.started_at,
           "duration_s": run.duration_s, "slot_wait_s": run.slot_wait_s}
    if run.worker:                          # SPEC-0338: a local run's payload is unchanged
        out["worker"] = run.worker
    for key in ("head", "tree"):            # SPEC-0359: the tree the run gated
        if getattr(run, key):
            out[key] = getattr(run, key)
    if run.changed is not None:             # SPEC-0368: a remote run's; a local payload is unchanged
        out["changed"] = list(run.changed)
    if run.reused:                          # SPEC-0359: close gates a loop result stood in for
        out["reused"] = list(run.reused)
    out["results"] = results
    return out


def run_and_log(cfg, campaign_state: dict, project_cfg: dict, *, seq=None, by="tick",
                tier: str | None = None, worker: str | None = None,
                local: bool = False) -> GateRun:
    """`run_all` + one `gate` ledger event. Does not touch `state.json`; callers that steer
    the queue (SPEC-0176) update `last_gate` themselves."""
    cid = campaign_state["id"]
    seq = seq if seq is not None else next_gate_seq(cfg, cid)
    run = run_all(project_cfg, campaign_state, cfg, seq=seq, tier=tier, worker=worker,
                  local=local)
    store.append_event(cfg, cid, "gate", job_payload(run), by=by)
    settle_job(cfg, cid, run)       # SPEC-0341: a debug run is never adopted by the loop
    return run


def job_payload(run: GateRun) -> dict:
    """`as_payload`, plus the async job's `event_uid` (SPEC-0341) when there is one."""
    payload = as_payload(run)
    if run.event_uid:
        payload["event_uid"] = run.event_uid
    return payload


def settle_job(cfg, cid, run: GateRun) -> None:
    """SPEC-0341: a run outside the loop's gate step (a debug run, a close re-gate) has its
    `GateRun` in hand: its job record goes final now, and a job a stop detached is cancelled,
    so nothing is left live for a later gate step to stumble on."""
    if not run.job_id:
        return
    if run.interrupted:
        from . import remote_gates as RG    # lazy: remote_gates imports this module
        RG.cancel_campaign(cfg, cid, run.job_id)
    RJ.mark_done(cfg, run.job_id)

