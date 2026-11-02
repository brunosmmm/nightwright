"""`nw queue restart` (SPEC-0375): stop the runner and start a new one with the same command,
parking the in-process campaigns across the restart and resuming them before the new runner
can admit backlog into their slots.

The apply is a sequence of idempotent phases recorded in `<agentic_dir>/restart.json`
(`drain` → `stop` → `park` → `resume` → `start`); a crash leaves the file at its phase and
`nw queue restart --continue --yes` finishes it. The old runner's environment is used for
the new one but never written to disk (it can hold credentials)."""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import lease as L
from . import queue as Q
from . import store

SCHEMA = "nw.queue.restart.v1"
PHASES = ("drain", "stop", "park", "resume", "start")
# in-process steps: the stop ends a gate (its result is discarded, SPEC-0229) and waits for an
# open / close (the close guard); the persona states are adopted losslessly by the next runner
PARK_STATES = frozenset({"OPENING", "GATING", "CLOSING"})
MARK = "nw queue restart"
_DROP_ENV = (L.HANDOVER_ENV, Q.RUNNER_LOG_ENV)
_STRIP_FLAGS = frozenset({"--once", "--until-idle", "--detach"})
_STRIP_VALUE = frozenset({"--max-ticks", "--log"})
DRAIN_POLL_S = 1.0


def plan_path(cfg) -> Path:
    return store.agentic_dir(cfg) / "restart.json"


def read_plan(cfg) -> dict | None:
    try:
        rec = json.loads(plan_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def public(plan: dict) -> dict:
    """`plan` without its private keys (`_env`): what is printed and written."""
    return {k: v for k, v in plan.items() if not k.startswith("_")}


def _save(cfg, plan: dict) -> None:
    """Atomic 0600 write of the public plan."""
    path = plan_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(public(plan), fh, indent=2, ensure_ascii=False, default=str)
        fh.write("\n")
    os.replace(tmp, path)


# ---- the old runner's command --------------------------------------------------------------------

def read_proc(pid, proc_root="/proc") -> dict | None:
    """`{argv, cwd, env}` of `pid` from procfs, or None when it cannot be read."""
    base = Path(proc_root) / str(pid)
    try:
        argv = [a.decode("utf-8", "surrogateescape")
                for a in (base / "cmdline").read_bytes().split(b"\0")]
        while argv and argv[-1] == "":
            argv.pop()
        cwd = os.readlink(base / "cwd")
        env = {}
        for item in (base / "environ").read_bytes().split(b"\0"):
            k, sep, v = item.decode("utf-8", "surrogateescape").partition("=")
            if sep and k:
                env[k] = v
    except OSError:
        return None
    return {"argv": argv, "cwd": cwd, "env": env} if argv else None


def runner_argv(argv) -> list | None:
    """The command that starts the long-lived loop again: `argv` without `--detach`, `--log`,
    `--once`, `--max-ticks` and `--until-idle` after its `queue run`; None when `argv` is not
    a `… queue run …` command."""
    argv = list(argv or [])
    at = next((i for i in range(len(argv) - 1)
               if argv[i] == "queue" and argv[i + 1] == "run"), None)
    if at is None:
        return None
    out, skip = argv[:at + 2], False
    for a in argv[at + 2:]:
        if skip:
            skip = False
        elif a in _STRIP_FLAGS or a.partition("=")[0] in _STRIP_VALUE and "=" in a:
            continue
        elif a in _STRIP_VALUE:
            skip = True
        else:
            out.append(a)
    return out


def fallback_argv() -> list:
    return [sys.executable, "-m", "nightwright", "queue", "run"]


def clean_env(env) -> dict:
    return {k: v for k, v in dict(env).items() if k not in _DROP_ENV}


# ---- plan ----------------------------------------------------------------------------------------

def plan_restart(cfg, *, drain_min=0, only=(), proc_root="/proc") -> dict:
    """The read-only plan (`nw.queue.restart.v1`). ValueError on an unfinished restart, a
    lease another host or a one-shot tick holds, or a bad `--only` campaign."""
    prev = read_plan(cfg)
    if prev and prev.get("phase") != "done":
        raise ValueError(f"restart {prev.get('id')} is unfinished (phase {prev.get('phase')}); "
                         f"finish it with nw queue restart --continue --yes ({plan_path(cfg)})")
    if drain_min and float(drain_min) < 0:
        raise ValueError("--drain takes minutes ≥ 0")
    st = Q._settled_holder(cfg)
    info = (L.read_lease(cfg) or {}) if st["held"] else {}
    if st["held"]:
        refusal = Q.stop_refusal(st, info.get("argv"), wait_guard=True)
        if refusal:
            raise ValueError(refusal)
    proc = read_proc(st["pid"], proc_root) if st["held"] else None
    argv = runner_argv(proc["argv"]) if proc else None
    if argv:
        command = {"argv": argv, "cwd": proc["cwd"], "source": "proc", "env_source": "proc"}
        env = clean_env(proc["env"])
    else:
        command = {"argv": fallback_argv(), "cwd": os.getcwd(), "source": "fallback",
                   "env_source": "current"}
        env = clean_env(os.environ)
    guard = Q.close_guard(st) if st["held"] else None

    live = {s["id"]: s for s in store.list_campaigns(cfg)}
    if only:
        picked = []
        for cid in dict.fromkeys(only):
            s = live.get(cid)
            if s is None:
                try:
                    s = store.read_state(cfg, cid)
                except (OSError, ValueError, KeyError):
                    s = None
                raise ValueError(f"{cid}: " + (f"is {s.get('state')}, not in flight"
                                               if isinstance(s, dict) and s.get("state")
                                               else "no such campaign"))
            if s.get("state") not in Q.ACTIVE_STATES:
                raise ValueError(f"{cid} is {s.get('state')}, not in flight")
            picked.append(s)
    else:
        picked = [s for s in live.values() if s.get("state") in Q.ACTIVE_STATES]
    picked.sort(key=Q.admission_key)
    targets = [{"campaign": s["id"], "state": s["state"], "priority": Q._priority(s),
                "action": "park" if s["state"] in PARK_STATES else "adopt",
                "resume_to": Q._resume_target(s["state"])} for s in picked]
    chosen = {t["campaign"] for t in targets}
    untouched = sorted(s["id"] for s in live.values()
                       if s.get("state") in Q.ACTIVE_STATES and s["id"] not in chosen)
    log = (info.get("log") if st["held"] else None) or str(store.agentic_dir(cfg) / "runner.log")
    return {"schema": SCHEMA, "id": None, "phase": "planned", "done_phases": [],
            "runner": {"held": st["held"], "pid": st.get("pid"), "host": st.get("host"),
                       "argv": info.get("argv"), "detached": bool(info.get("detached")),
                       "guard": guard and Q._guard_text(guard)},
            "command": command, "log": log, "drain_min": float(drain_min or 0),
            "only": list(dict.fromkeys(only)), "targets": targets, "untouched": untouched,
            "_env": env}


# ---- apply ---------------------------------------------------------------------------------------

def _state_of(cfg, cid) -> str | None:
    try:
        return store.read_state(cfg, cid).get("state")
    except (OSError, ValueError, KeyError):
        return None


def _drain(cfg, plan, *, clock, sleep, poll_s):
    mins = float(plan.get("drain_min") or 0)
    watch = [t["campaign"] for t in plan["targets"] if t["state"] == "GATING"]
    if mins <= 0 or not watch or not L.is_held(cfg):
        plan["drain"] = {"skipped": True, "waited_s": 0, "finished": [], "pending": watch
                         if mins > 0 and watch else []}
        return
    start = clock()
    end = start + mins * 60
    while True:
        pending = [c for c in watch if _state_of(cfg, c) == "GATING"]
        if not pending or clock() >= end:
            break
        sleep(poll_s)
    plan["drain"] = {"skipped": False, "waited_s": round(clock() - start, 1),
                     "finished": [c for c in watch if c not in pending], "pending": pending}


def _stop(cfg, plan, *, timeout_s, kill, clock, sleep):
    if not L.is_held(cfg):
        plan["stop"] = {"stopped": False, "reason": "no runner"}
        return
    plan["stop"] = Q.stop_runner(cfg, timeout_s=timeout_s, wait_guard=True, kill=kill,
                                 clock=clock, sleep=sleep)


def _no_runner(cfg, what: str):
    if L.is_held(cfg):
        st = L.lease_status(cfg)
        raise ValueError(f"pid {st.get('pid')} holds the lease again; cannot {what} while a "
                         "runner ticks. Stop it, then nw queue restart --continue --yes")


def _park(cfg, plan, *, by):
    _no_runner(cfg, "park")
    rid = plan["id"]
    parked, skipped = [], []
    for t in plan["targets"]:
        cid = t["campaign"]
        with store.item_lock(cfg, cid):
            st = store.read_state(cfg, cid)
            s, park = st.get("state"), st.get("park") or {}
            if s == "PARKED" and park.get("reason") == "restart" and park.get("restart_id") == rid:
                parked.append({"campaign": cid, "from": park.get("from"),
                               "resume_to": park.get("resume_to")})
                continue
            if s not in PARK_STATES:
                skipped.append({"campaign": cid, "state": s, "why": "adopt" if s in Q.ACTIVE_STATES
                                else "moved"})
                continue
            if st.get("park_requested"):         # a human's park applies on the next tick
                skipped.append({"campaign": cid, "state": s, "why": "park_requested"})
                continue
            to = Q._resume_target(s)
            Q._park(cfg, st, "restart", f"{MARK} {rid}", to, by=by, count=False,
                    extra={"restart_id": rid, "from": s})
            parked.append({"campaign": cid, "from": s, "resume_to": to})
    plan["parked"], plan["skipped"] = parked, skipped


def _resume(cfg, plan, *, by):
    _no_runner(cfg, "resume")
    rid = plan["id"]
    done = {r["campaign"]: r for r in plan.get("resumed") or []}
    for t in plan["targets"]:
        cid = t["campaign"]
        if cid in done:
            continue
        with store.item_lock(cfg, cid):
            st = store.read_state(cfg, cid)
            park = st.get("park") or {}
            if st.get("state") != "PARKED" or park.get("reason") != "restart" \
                    or park.get("restart_id") != rid:
                continue
            to = park.get("resume_to") or "IMPLEMENTING"
            st["park"] = None
            ev = ("resume", {"to": to, "note": f"{MARK} {rid}", "cap_usd": None,
                             "loops_reset": False})
            st = Q._enter_slot_or_wait(cfg, st, to, "resumed", by=by, extra_events=[ev])
            done[cid] = {"campaign": cid, "resume_to": to, "state": st.get("state")}
            plan["resumed"] = list(done.values())
            _save(cfg, plan)                     # per campaign: a crash resumes the rest only
    plan["resumed"] = list(done.values())


def _start(cfg, plan, *, env, popen, clock, timeout_s):
    if L.is_held(cfg):
        st = L.lease_status(cfg)
        plan["start"] = {"started": False, "pid": st.get("pid"),
                         "reason": "lease already held; not starting another"}
        return
    cmd = plan["command"]
    if env is None:
        env, cmd["env_used"] = clean_env(os.environ), "current"
    else:
        cmd["env_used"] = cmd.get("env_source")
    cwd = cmd.get("cwd") if cmd.get("cwd") and Path(cmd["cwd"]).is_dir() else None
    d = Q.detach_runner(cfg, cmd["argv"], plan["log"], timeout_s=timeout_s, popen=popen,
                        clock=clock, env=env, cwd=cwd)
    plan["start"] = {"started": True, **d}


def apply_restart(cfg, plan: dict, *, by="human", timeout_s=60, start_timeout_s=30,
                  popen=subprocess.Popen, kill=os.kill, clock=time.monotonic, sleep=time.sleep,
                  poll_s=DRAIN_POLL_S, fail_after=None) -> dict:
    """Run the phases of `plan` not yet in `done_phases`, saving the plan file before and after
    each. Returns the finished plan. ValueError (the plan file kept at its phase) when a phase
    fails. `fail_after` (tests) raises right after that phase is recorded, as a crash would."""
    plan = dict(plan)
    env = plan.pop("_env", None)
    if not plan.get("id"):
        plan["id"] = "R-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        plan["by"], plan["started_at"] = by, store.now_iso()
    plan.setdefault("done_phases", [])
    plan.pop("error", None)
    _save(cfg, plan)
    steps = {
        "drain": lambda: _drain(cfg, plan, clock=clock, sleep=sleep, poll_s=poll_s),
        "stop": lambda: _stop(cfg, plan, timeout_s=timeout_s, kill=kill, clock=clock,
                              sleep=sleep),
        "park": lambda: _park(cfg, plan, by=by),
        "resume": lambda: _resume(cfg, plan, by=by),
        "start": lambda: _start(cfg, plan, env=env, popen=popen, clock=clock,
                                timeout_s=start_timeout_s),
    }
    for phase in PHASES:
        if phase in plan["done_phases"]:
            continue
        plan["phase"] = phase
        _save(cfg, plan)
        try:
            steps[phase]()
        except Exception as e:                   # noqa: BLE001 — recorded, then re-raised
            plan["error"] = f"{phase}: {e}"
            _save(cfg, plan)
            raise ValueError(f"restart {plan['id']} stopped in phase {phase}: {e}; the plan is "
                             f"kept in {plan_path(cfg)} — finish with nw queue restart "
                             "--continue --yes") from e
        plan["done_phases"].append(phase)
        _save(cfg, plan)
        if fail_after == phase:
            raise RuntimeError(f"injected crash after {phase}")
    plan["phase"], plan["finished_at"] = "done", store.now_iso()
    _save(cfg, plan)
    Q.journal(cfg, "restart", id=plan["id"], phase="done",
              parked=[p["campaign"] for p in plan.get("parked") or []],
              resumed=[r["campaign"] for r in plan.get("resumed") or []],
              old_pid=(plan.get("stop") or {}).get("pid"),
              new_pid=(plan.get("start") or {}).get("pid"))
    return plan


def continue_plan(cfg) -> dict:
    """The unfinished plan `--continue` finishes. ValueError when there is none."""
    plan = read_plan(cfg)
    if not plan:
        raise ValueError(f"no restart to continue ({plan_path(cfg)} absent)")
    if plan.get("phase") == "done":
        raise ValueError(f"restart {plan.get('id')} is already done")
    return plan
