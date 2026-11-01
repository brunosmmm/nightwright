"""Deterministic gate executor (SPEC-0173).

Gates are the project's own proof commands from `.nw/project.yaml` (SPEC-0170): tests, lint,
anything. They run in the campaign worktree, in order, **all of them every time** (no
short-circuit, so triage sees the full picture), each with a timeout that kills the whole
process group. Output goes to one log per gate under the campaign's `gates/` directory; a
bounded tail travels in the result for prompts and `nw campaign show`. `required` decides
whether a failure blocks; optional failures are recorded and never block."""
from __future__ import annotations

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

from . import gate_results
from . import gate_slots
from . import schemas as SC
from . import store
from . import project as P
from .project import expand_cmd

TAIL_LINES = 60
_KILL_GRACE_S = 5
_LOG_SEQ_RE = re.compile(r"^(\d{3})-")
# SPEC-0289: pytest exits 4 (usage error: ids not found) and 5 (nothing collected) when a targeted
# rerun could not run the ids, which says nothing about whether the failure reproduces.
TARGETED_UNRESOLVED_EXITS = (4, 5)
# shell operators after which appended ids would no longer be arguments of the gate's own command
_SHELL_OPS_RE = re.compile(r"[|&;<>()`\n]|\$\(")

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
    summary: dict | None = None  # SPEC-0284: parsed counts/failing ids; None without `results:`
    attempts: tuple = ()        # SPEC-0289: one dict per attempt once a retry ran (attempt 0 first)
    flaky: tuple = ()           # SPEC-0289: ids a targeted rerun passed, when the gate ended ok


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
    """Environment every gate (and anything it calls) sees: the same nightwright store the loop uses.
    With `seq` the gate run also gets its own `TMPDIR` under the campaign dir (SPEC-0253)."""
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
             attempt: int | None = None) -> GateResult:
    """Run one gate in `worktree / gate["cwd"]`, combined output to `log_path`, timeout
    `gate["timeout_s"]` (kills the whole process group). `ok` = exit code 0. The log is the
    command's output only; command, cwd, exit, timing and errors go to the JSON sidecar.
    `cmd_override` (SPEC-0289, a targeted rerun) replaces the expanded `cmd`; `attempt` marks
    a retry's sidecar."""
    cmd = cmd_override if cmd_override is not None else expand_cmd(gate["cmd"], **placeholders)
    cwd = Path(worktree) / gate.get("cwd", ".")
    timeout_s = int(gate.get("timeout_s", 1200))
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    child_env = {**os.environ, **env}
    started_at = store.now_iso()
    started = time.monotonic()
    started_ns = time.time_ns()     # SPEC-0284: a junit file older than this is a leftover
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
    summary = None
    if proc is not None and not interrupted:        # SPEC-0284: never decides `ok`
        summary = gate_results.parse(gate.get("results"), log_path, cwd, started_ns)
    result = GateResult(gate["name"], bool(gate.get("required", True)), ok, exit_code,
                        timed_out, duration, log_rel or str(log_path), tail(log_path),
                        cmd=cmd, cwd=str(cwd), interrupted=interrupted, summary=summary)
    _write_sidecar(log_path, result, timeout_s=timeout_s, started_at=started_at, error=error,
                   attempt=attempt)
    return result


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
                          previous: dict | None = None) -> GateResult:
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
                           stop=stop, deadline=deadline)
    ppath = log_path.with_name(f"{log_path.stem}.ff.log")
    prel = f"{Path(log_rel).with_suffix('').as_posix()}.ff.log" if log_rel else str(ppath)
    cmd = targeted_cmd(gate, expand_cmd(gate["cmd"], **placeholders), ids, placeholders)
    pre = run_gate(gate, worktree, env, ppath, placeholders, log_rel=prel, cmd_override=cmd,
                   attempt=-1)
    rec = _attempt(-1, "failed-first", pre)
    if pre.interrupted or (not pre.ok and not pre.timed_out
                           and pre.exit_code not in TARGETED_UNRESOLVED_EXITS):
        return replace(pre, attempts=(rec,))        # reproduced, or cut by a stop (SPEC-0229)
    if _stop_requested(stop):                       # a stop between the prelude and the main run
        return replace(pre, ok=False, interrupted=True, attempts=(rec,))
    final = _run_ladder(gate, worktree, env, log_path, placeholders, log_rel=log_rel,
                        stop=stop, deadline=deadline)
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
                deadline: float | None = None) -> GateResult:
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
    first = run_gate(gate, worktree, env, log_path, placeholders, log_rel=log_rel)
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
                     cmd_override=cmd, attempt=n)
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
    never overwrite each other)."""
    d = store.gates_dir(cfg, campaign_id)
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
            worker: str | None = None, local: bool = False) -> GateRun:
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
    same seq. Without an enabled worker this is the local run it always was."""
    cid = campaign_state["id"]
    project_cfg = {**project_cfg, "gates": P.gates_for(project_cfg, tier)}
    worktree = (campaign_state.get("repo") or {}).get("worktree")
    if not worktree:
        raise ValueError(f"campaign {cid} has no worktree recorded")

    def announce(occ, waited_s):
        store.append_event(cfg, cid, "gate_waiting",
                           {"slots": occ["slots"], "held": occ["held"], "waited_s": waited_s},
                           by="gates")

    seq, owner = _claim_seq(cfg, cid, seq)
    try:                                        # SPEC-0253: only the owner removes its dir
        pools = None
        if not local:
            from . import remote_gates as RG    # lazy: remote_gates imports this module
            pools = RG.pools_for(cfg, project_cfg, campaign_state, seq=seq, worker=worker)
        with gate_slots.acquire(cfg, cid, stop=stop, on_wait=announce, pools=pools) as hold:
            if hold.interrupted:                # SPEC-0229: stopped while waiting, nothing ran (ok=False: never counted green)
                return GateRun(seq, False, (), store.now_iso(), 0.0, interrupted=True,
                               slot_wait_s=hold.waited_s)
            if hold.pool is None:
                run = _run_all_held(project_cfg, campaign_state, cfg, cid, worktree, seq, stop,
                                    tier)
                return replace(run, slot_wait_s=hold.waited_s)
            out = RG.run(hold.pool, project_cfg, campaign_state, cfg, seq, owner, stop)
        waited = round(hold.waited_s + out.worker_wait_s, 1)
        if out.run is not None:
            return replace(out.run, slot_wait_s=waited)
        # SPEC-0338 fallback: the remote slot is released; a local one, under the same claim
        with gate_slots.acquire(cfg, cid, stop=stop, on_wait=announce) as fb:
            waited = round(waited + fb.waited_s, 1)
            if fb.interrupted:
                return GateRun(seq, False, (), store.now_iso(), 0.0, interrupted=True,
                               slot_wait_s=waited)
            run = _run_all_held(project_cfg, campaign_state, cfg, cid, worktree, seq, stop,
                                tier)
        return replace(run, slot_wait_s=waited)
    finally:
        store.clear_child_tmp(cfg, cid, owner)


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
    store.append_event(cfg, cid, "gate", as_payload(run), by=by)
    return run

