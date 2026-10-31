"""`nw queue conductor-launch` / `steward-launch` (SPEC-0319): start the conductor or a steward
as a Claude Code session with forced context limits, a pinned model and a session id nightwright
registered (SPEC-0299) before `claude` starts.

`settings` and `build_plan` are pure; `run` is the only function with process side effects."""
from __future__ import annotations

import math
import os
import re
import shutil
import uuid

import click

from . import conductor_cost as CC

CONDUCTOR_PROMPT = "Run the /nw-orient skill, then the /nw-conduct skill."
STEWARD_PROMPT = "Run the /nw-orient skill, then the /nw-steward skill for project {project}."

DEFAULTS = {"model": "claude-sonnet-5-5", "effort": "medium", "autocompact": "150k",
            "idle_compact_min_tokens": 120000, "bash_max_output_length": 15000,
            "file_read_max_output_tokens": 10000}
EFFORTS = ("low", "medium", "high", "xhigh", "max")
AUTOCOMPACT_MIN, AUTOCOMPACT_MAX = 100_000, 1_000_000
# settings key -> env var, in the order NW_LAUNCH_KEYS lists them
ENV_KEYS = {"idle_compact_min_tokens": "CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS",
            "bash_max_output_length": "BASH_MAX_OUTPUT_LENGTH",
            "file_read_max_output_tokens": "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS"}
ENV_REMOVE = ["CLAUDE_CODE_EFFORT_LEVEL"]
LAUNCH_KEYS_VAR = "NW_LAUNCH_KEYS"
_COUNT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*$")


def _number(v):
    """`v` as a finite non-negative float, or None (bool, non-numeric, NaN/inf, negative)."""
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) and f >= 0 else None


def _tokens(v):
    """A token count from an int, a numeric string, or an `Nk`/`Nm` string; else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = _number(v)
        return int(f) if f is not None and f.is_integer() else None
    m = _COUNT_RE.match(v) if isinstance(v, str) else None
    if not m:
        return None
    f = float(m.group(1)) * {"": 1, "k": 1000, "m": 1_000_000}[m.group(2).lower()]
    return int(f) if f.is_integer() else None


def _good(key, v):
    """The cleaned value of setting `key`, or None when `v` is a bad value."""
    if key == "model":
        return v.strip() if isinstance(v, str) and v.strip() else None
    if key == "effort":
        return v if v in EFFORTS else None
    if key == "autocompact":
        if isinstance(v, str) and v.strip().lower() == "auto":
            return "auto"
        n = _tokens(v)
        return n if n is not None and AUTOCOMPACT_MIN <= n <= AUTOCOMPACT_MAX else None
    n = _tokens(v)
    floor = 100_000 if key == "idle_compact_min_tokens" else 1000
    return n if n is not None and n >= floor else None


def settings(cfg, role="conductor") -> dict:
    """`DEFAULTS` overlaid with `agentic.conductor.launch` and, for a steward, its `steward`
    sub-mapping. A bad value falls back to the default; `autocompact` becomes a token count
    (or `auto`)."""
    block = (((cfg.get("agentic") or {}).get("conductor") or {}).get("launch")) or {}
    block = block if isinstance(block, dict) else {}
    steward = block.get("steward") if role == "steward" else None
    steward = steward if isinstance(steward, dict) else {}
    out = {}
    for key, default in DEFAULTS.items():
        out[key] = _good(key, default)
        for layer in (block, steward):
            if key in layer:
                good = _good(key, layer[key])
                out[key] = good if good is not None else _good(key, default)
    return out


def _project_root(cfg, project):
    from ..rules import projects_payload
    projects = projects_payload(cfg)["projects"]
    for p in projects:
        if p["name"] == project:
            root = (p.get("research") or {}).get("root")
            if root:
                return root
            raise click.ClickException(
                f"project {project!r} has no repo root; known projects: "
                + ", ".join(q["name"] for q in projects) + " (see `nw projects --json`)")
    raise click.ClickException(
        f"unknown project {project!r}; known projects: "
        + ", ".join(q["name"] for q in projects) + " (see `nw projects --json`)")


def reader_for(role, project=None) -> str:
    return "conductor" if role == "conductor" else f"steward:{project}"


def build_plan(cfg, role, project=None, *, resume=False, session_id=None) -> dict:
    """The launch plan; touches nothing. With `resume`, the id is the newest launched session
    of the reader (a ClickException when there is none or its transcript is gone)."""
    from ..rules import INTERNAL_PROJECT
    if role not in ("conductor", "steward"):
        raise ValueError(role)
    sett = settings(cfg, role)
    name = reader_for(role, project)
    cwd = _project_root(cfg, INTERNAL_PROJECT if role == "conductor" else project)
    if resume:
        launched = [r for r in CC.sessions(cfg, name) if r.get("source") == "launch"]
        sid = launched[-1]["session_id"] if launched else None
        if not sid or CC.find_transcript(cfg, sid) is None:
            raise click.ClickException("no launched session to resume; run without --resume")
    else:
        sid = session_id or str(uuid.uuid4())
    prompt = (CONDUCTOR_PROMPT if role == "conductor"
              else STEWARD_PROMPT.format(project=project))
    argv = ["claude", "-n", name, "--resume" if resume else "--session-id", sid,
            "--model", sett["model"], "--effort", sett["effort"],
            "--autocompact", str(sett["autocompact"]), prompt]
    env = {var: str(sett[key]) for key, var in ENV_KEYS.items()}
    env[LAUNCH_KEYS_VAR] = ",".join(ENV_KEYS.values())
    return {"role": role, "name": name, "cwd": str(cwd), "session_id": sid, "resumed": resume,
            "argv": argv, "env": env, "env_remove": list(ENV_REMOVE), "prompt": prompt,
            "model": sett["model"], "price": CC.price_for(cfg, sett["model"])}


def check_price(plan):
    if plan["price"] is None:
        raise click.ClickException(
            f"no price for model {plan['model']}; set agentic.conductor.prices.<model-prefix> "
            "(SPEC-0299)")


def exec_env(plan, base=None) -> dict:
    """`base` (default `os.environ`) without the `env_remove` keys, overlaid with the plan's env."""
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if k not in plan.get("env_remove", ())}
    env.update(plan["env"])
    return env


def register(cfg, plan):
    """Pre-register the session in the SPEC-0299 registry (idempotent per reader and id)."""
    try:
        CC.record_session(cfg, plan["name"], {}, source="launch", session_id=plan["session_id"])
    except OSError as e:
        raise click.ClickException(f"cannot register the session: {e}")


def run(cfg, plan):
    """Check the price and the binary, chdir to the plan's cwd, register, exec `claude`."""
    check_price(plan)
    if shutil.which("claude") is None:
        raise click.ClickException("`claude` not found on PATH")
    try:
        os.chdir(plan["cwd"])
    except OSError as e:
        raise click.ClickException(f"cannot enter {plan['cwd']}: {e}")
    register(cfg, plan)
    try:
        os.execvpe("claude", plan["argv"], exec_env(plan))
    except OSError as e:   # e.g. a broken `claude` wrapper: an error, not a traceback
        raise click.ClickException(f"cannot start claude: {e}; the registered launch record "
                                   "has no session, so start fresh (without --resume)")
