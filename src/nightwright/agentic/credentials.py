"""Agent credentials from the human's own store (SPEC-0483): `agentic.credentials`.

    agentic:
      credentials:
        CURSOR_API_KEY: pass:cursor/sdk     # `pass show cursor/sdk`, first line
        OPENAI_API_KEY: env                 # inherit only (the default for any other var)

The runner calls `ensure(cfg)` at start and at the top of every tick. A var already set in the
process environment wins and is never re-read. A missing `pass:` var is resolved with
`pass show <entry>` run non-interactively (stdin closed, no TTY, `GPG_TTY` removed, gpg told
`--batch --pinentry-mode error` through `PASSWORD_STORE_GPG_OPTS`, a short timeout), and the
first line of its output goes into `os.environ`, so the persona children inherit it.

A failed resolution (typically a locked gpg-agent after a reboot) never raises: the var stays
unset, the cursor backend preflight then holds the steps that need it (SPEC-0484), one
escalation names the var and the fix, and the next tick tries again; a later success
withdraws that escalation. No value is ever written, logged, journaled or emitted: records
carry the var name, the entry name and a status."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading

ESC_KIND = "credential_unresolved"
TIMEOUT_S = 5.0
STATUSES = ("set", "resolved", "locked", "missing", "unconfigured")
_state: dict = {}                 # var -> last status this process journaled
_lock = threading.Lock()


def settings(cfg) -> dict:
    """`{VAR: source}` from `agentic.credentials`; invalid entries dropped. A source is
    `env` or `pass:<entry>`."""
    raw = ((cfg or {}).get("agentic") or {}).get("credentials") if isinstance(cfg, dict) else None
    out = {}
    if not isinstance(raw, dict):
        return out
    for var, src in raw.items():
        if not isinstance(var, str) or not var or not var.replace("_", "").isalnum():
            continue
        src = str(src or "").strip()
        if src == "env" or (src.startswith("pass:") and src[len("pass:"):].strip()):
            out[var] = src
    return out


def _pass_env(environ) -> dict:
    env = {k: v for k, v in environ.items() if k != "GPG_TTY"}
    opts = (env.get("PASSWORD_STORE_GPG_OPTS") or "").strip()
    env["PASSWORD_STORE_GPG_OPTS"] = (opts + " --batch --pinentry-mode error").strip()
    return env


def _first_line(text: str) -> str:
    return (text or "").split("\n", 1)[0].rstrip("\r")


def fetch(source: str, *, environ=None, run=None, timeout=TIMEOUT_S, which=shutil.which):
    """`(value | None, status, why)` for one source; never raises. `why` never holds the
    value (it is the tool's last stderr line, or a fixed text)."""
    environ = os.environ if environ is None else environ
    if source == "env":
        return None, "missing", "not set in the environment (source: env)"
    entry = source[len("pass:"):].strip()
    exe = which("pass", path=environ.get("PATH"))
    if not exe:
        return None, "missing", "`pass` not found on PATH"
    run = run or subprocess.run
    try:
        p = run([exe, "show", entry], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=timeout, env=_pass_env(environ), start_new_session=True)
    except subprocess.TimeoutExpired:
        return None, "locked", f"`pass show {entry}` timed out after {timeout:g}s"
    except OSError as e:
        return None, "missing", f"`pass` could not run: {e.strerror or e}"
    value = _first_line(p.stdout) if p.returncode == 0 else ""
    if p.returncode == 0 and value:
        return value, "resolved", ""
    tail = ((p.stderr or "").strip().splitlines() or [f"exit {p.returncode}"])[-1][:200]
    if p.returncode == 0:                   # stdout (the secret) is never quoted
        tail = "the entry's first line is empty"
    return None, "locked", f"`pass show {entry}` failed: {tail}"


def status(cfg, var: str, *, environ=None, run=None) -> dict:
    """`{var, source, status, why}` without changing anything (`nw doctor`)."""
    environ = os.environ if environ is None else environ
    src = settings(cfg).get(var)
    if (environ.get(var) or "").strip():
        return {"var": var, "source": src or "env", "status": "set", "why": ""}
    if src is None:
        return {"var": var, "source": None, "status": "unconfigured",
                "why": "not set and no agentic.credentials entry"}
    _v, st, why = fetch(src, environ=environ, run=run)
    return {"var": var, "source": src, "status": st, "why": why}


def fix_text(var: str, src: str | None) -> str:
    if src and src.startswith("pass:"):
        entry = src[len("pass:"):].strip()
        return (f"unlock gpg: run keys-setup or `pass show {entry}` once in a terminal; the "
                "runner picks it up on its next tick (no restart)")
    return (f"set {var} in the runner's environment, or map it in agentic.credentials "
            "(e.g. pass:<entry>), then restart the runner")


def ensure(cfg, *, environ=None, run=None, where="tick") -> dict:
    """Resolve every configured var missing from `environ` (default `os.environ`, which it
    fills). Returns `{var: status}`. Journals status changes, files / withdraws the single
    escalation per var. Never raises."""
    environ = os.environ if environ is None else environ
    out = {}
    try:
        conf = settings(cfg)
    except Exception:                                   # noqa: BLE001
        return out
    for var, src in conf.items():
        try:
            if (environ.get(var) or "").strip():
                out[var] = "set"
                _transition(cfg, var, src, "set", "", where)
                continue
            if src == "env":
                out[var] = "missing"
                _transition(cfg, var, src, "missing", "not set (source: env)", where)
                continue
            value, st, why = fetch(src, environ=environ, run=run)
            if value is not None:
                environ[var] = value
                del value
            out[var] = st
            _transition(cfg, var, src, st, why, where)
        except Exception:                               # noqa: BLE001
            out[var] = "missing"
    return out


def _transition(cfg, var, src, st, why, where) -> None:
    with _lock:
        before = _state.get(var)
        _state[var] = st
    ask = src.startswith("pass:")                       # an `env` var is inherit-only: no escalation
    if before == st:
        if st in ("locked", "missing") and ask:
            _escalate(cfg, var, src, why)               # deduped by the open record
        return
    from .queue import journal
    ok = st in ("set", "resolved")
    if ok and before is None and st == "set":
        return                                          # the normal case: nothing to say
    journal(cfg, "credential_resolved" if ok else "credential_unresolved", var=var,
            source=src, status=st, **({} if ok else {"why": why}), where=where)
    if ok:
        _withdraw(cfg, var)
    elif ask:
        _escalate(cfg, var, src, why)


def _open(cfg, var):
    from . import escalations as ESC
    return [r for r in ESC.list_all(cfg, "open")
            if r.get("kind") == ESC_KIND and (r.get("facts") or [""])[0] == f"var: {var}"]


def _escalate(cfg, var, src, why) -> dict | None:
    try:
        if _open(cfg, var):
            return None
        from . import escalations as ESC
        plan = ESC.plan_escalate(
            cfg, None, frm="conductor", kind=ESC_KIND, topic="human_settings",
            question=(f"The queue runner cannot resolve {var} ({src}): {why}. Persona steps "
                      "that need it are held (no retry spent); the runner retries every tick."),
            options=(fix_text(var, src), "switch agentic.runner to claude"),
            facts=[f"var: {var}", f"source: {src}", f"status: {why}"])
        return ESC.apply_escalate(cfg, plan)
    except Exception:                                   # noqa: BLE001
        return None


def _withdraw(cfg, var) -> None:
    try:
        from . import escalations as ESC
        for rec in _open(cfg, var):
            ESC.withdraw(cfg, rec["id"], f"{var} resolved", "nw")
    except Exception:                                   # noqa: BLE001
        pass


def reset() -> None:
    """Tests: forget the journaled statuses."""
    with _lock:
        _state.clear()
