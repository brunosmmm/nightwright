"""Deterministic gate executor (SPEC-0173).

Gates are the project's own proof commands from `.wt/project.yaml` (SPEC-0170): tests, lint,
anything. They run in the campaign worktree, in order, **all of them every time** (no
short-circuit, so triage sees the full picture), each with a timeout that kills the whole
process group. Output goes to one log per gate under the campaign's `gates/` directory; a
bounded tail travels in the result for prompts and `wt campaign show`. `required` decides
whether a failure blocks; optional failures are recorded and never block."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import store
from .project import expand_cmd

TAIL_LINES = 60
_KILL_GRACE_S = 5
_LOG_SEQ_RE = re.compile(r"^(\d{3})-")


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


@dataclass(frozen=True)
class GateRun:
    seq: int
    ok: bool
    results: tuple[GateResult, ...]
    started_at: str
    duration_s: float


def tail(path, n: int = TAIL_LINES) -> str:
    """Last `n` lines of a text file ("" when missing)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.rstrip("\n").splitlines()[-n:])


def gate_env(cfg, state: dict) -> dict[str, str]:
    """Environment every gate (and anything it calls) sees: the same wt store the loop uses."""
    repo = state.get("repo") or {}
    return {
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


def run_gate(gate: dict, worktree, env: dict, log_path, placeholders: dict, *,
             log_rel: str | None = None) -> GateResult:
    """Run one gate in `worktree / gate["cwd"]`, combined output to `log_path`, timeout
    `gate["timeout_s"]` (kills the whole process group). `ok` = exit code 0."""
    cmd = expand_cmd(gate["cmd"], **placeholders)
    cwd = Path(worktree) / gate.get("cwd", ".")
    timeout_s = int(gate.get("timeout_s", 1200))
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    child_env = {**os.environ, **env}
    started = time.monotonic()
    timed_out = False
    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"# {gate['name']}: {cmd} (cwd {cwd})\n")
        log.flush()
        try:
            proc = subprocess.Popen(cmd, shell=True, cwd=str(cwd), env=child_env,
                                    stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        except OSError as e:
            log.write(f"# failed to start: {e}\n")
            return GateResult(gate["name"], bool(gate.get("required", True)), False, None,
                              False, round(time.monotonic() - started, 3),
                              log_rel or str(log_path), tail(log_path))
        try:
            exit_code = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            exit_code = proc.returncode
            log.write(f"# timed out after {timeout_s}s; process group killed\n")
    duration = round(time.monotonic() - started, 3)
    ok = (exit_code == 0) and not timed_out
    return GateResult(gate["name"], bool(gate.get("required", True)), ok, exit_code,
                      timed_out, duration, log_rel or str(log_path), tail(log_path))


def next_gate_seq(cfg, campaign_id) -> int:
    """1 + the highest `NNN-` prefix among existing gate logs (so debug runs and loop runs
    never overwrite each other)."""
    d = store.gates_dir(cfg, campaign_id)
    if not d.is_dir():
        return 1
    seqs = [int(m.group(1)) for p in d.iterdir() if (m := _LOG_SEQ_RE.match(p.name))]
    return (max(seqs) + 1) if seqs else 1


def run_all(project_cfg: dict, campaign_state: dict, cfg, *, seq: int) -> GateRun:
    """Run every gate of `project_cfg` in the campaign's worktree; logs land at
    `gates/<seq:03>-<name>.log` under the campaign dir. `ok` = every *required* gate ok."""
    cid = campaign_state["id"]
    worktree = (campaign_state.get("repo") or {}).get("worktree")
    if not worktree:
        raise ValueError(f"campaign {cid} has no worktree recorded")
    env = gate_env(cfg, campaign_state)
    ph = placeholders(campaign_state)
    cdir = store.campaign_dir(cfg, cid)
    started_at = store.now_iso()
    t0 = time.monotonic()
    results = []
    for gate in project_cfg.get("gates") or []:
        rel = f"gates/{seq:03d}-{gate['name']}.log"
        results.append(run_gate(gate, worktree, env, cdir / rel, ph, log_rel=rel))
    ok = all(r.ok for r in results if r.required)
    return GateRun(seq, ok, tuple(results), started_at, round(time.monotonic() - t0, 3))


def blocking_failures(run: GateRun) -> list[GateResult]:
    return [r for r in run.results if r.required and not r.ok]


def as_payload(run: GateRun) -> dict:
    """Shape shared by the `gate` ledger event and `last_gate` in state. The run number is
    `gate_seq`, not `seq`: ledger events carry their own `seq` in the envelope and
    `append_event` merges the payload over it."""
    return {"gate_seq": run.seq, "ok": run.ok, "started_at": run.started_at,
            "duration_s": run.duration_s, "results": [asdict(r) for r in run.results]}


def run_and_log(cfg, campaign_state: dict, project_cfg: dict, *, seq=None, by="tick") -> GateRun:
    """`run_all` + one `gate` ledger event. Does not touch `state.json`; callers that steer
    the queue (SPEC-0176) update `last_gate` themselves."""
    cid = campaign_state["id"]
    seq = seq if seq is not None else next_gate_seq(cfg, cid)
    run = run_all(project_cfg, campaign_state, cfg, seq=seq)
    store.append_event(cfg, cid, "gate", as_payload(run), by=by)
    return run

