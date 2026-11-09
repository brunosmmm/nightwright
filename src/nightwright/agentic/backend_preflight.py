"""Persona backend preflight (SPEC-0484 part 3): a missing credential or SDK is an environment
fault, not a verdict.

- `check(kind)` is the cheap, in-process probe of a backend (no billed request, no
  subprocess): today only `cursor` has one (`cursor-sdk` imports, `CURSOR_API_KEY` set).
- `env_fault(result)` recognises the startup failure a Cursor child reports for the same
  faults (`cursor_exec._fail(..., code="startup")`), e.g. a run started by a runner that had no
  key and adopted by one that has.
- `note_fault(cfg, kind, error)` journals `backend_preflight_failed` once per process and
  fault, and files one queue-wide escalation (`kind: backend_unavailable`) while none is open
  for that backend. Never raises; never writes a credential value (the error texts name the
  variable, never its value)."""
from __future__ import annotations

import re
import sys
import threading

from . import runproc as RP

ESC_KIND = "backend_unavailable"
HOLD = "backend_unavailable"
# the texts `cursor_exec` writes for a missing key / SDK (code "startup")
_ENV_FAULT_RE = re.compile(r"CURSOR_API_KEY is not set|cursor-sdk is not installed")
_NOTED: set = set()
_NOTED_LOCK = threading.Lock()


def check(kind, environ=None) -> str | None:
    """The preflight error of backend `kind` in `environ` (default this process's), else
    None. Backends without a cheap in-process probe (`claude`, `pi`, `fake`) pass."""
    if kind != "cursor":
        return None
    try:
        rec = RP.probe_cursor_sdk(environ)
    except Exception:                        # noqa: BLE001 — a probe bug is not a fault
        return None
    return None if rec.get("ok") else (rec.get("error") or "cursor backend unavailable")


def env_fault(result) -> str | None:
    """The environment fault a finished, failed persona run reports, else None."""
    if result is None or not getattr(result, "is_error", False) \
            or getattr(result, "interrupted", False):
        return None
    m = _ENV_FAULT_RE.search(str(getattr(result, "result_text", "") or ""))
    return m.group(0) if m else None


def fault_kind(error: str) -> str:
    return "cursor" if "CURSOR" in error.upper() else "unknown"


def _question(kind: str, error: str, cfg) -> tuple[str, list, list]:
    q = (f"The {kind} persona backend cannot start on this host: {error}. Persona steps that "
         "need it are held (no triage or review retry is spent) until it is fixed.")
    opts = ["map the credential in agentic.credentials (e.g. CURSOR_API_KEY: pass:cursor/sdk), "
            "or install the missing SDK, then restart the runner: nw queue restart --yes",
            "switch agentic.runner (or the project's runner) to claude"]
    facts = [f"backend: {kind}", f"preflight: {error}"]
    return q, opts, facts


def note_fault(cfg, kind: str, error: str, *, where: str = "tick") -> dict | None:
    """Journal the fault once per process and file one escalation while none is open for
    `kind`. Returns the new escalation record, else None. Never raises."""
    key = (kind, error)
    with _NOTED_LOCK:
        first = key not in _NOTED
        _NOTED.add(key)
    try:
        from . import escalations as ESC
        from .queue import journal
        if first:
            journal(cfg, "backend_preflight_failed", backend=kind, error=error, where=where)
        from . import credentials as CR
        if any(var in error for var in CR.settings(cfg)):
            return None             # SPEC-0483: the credential's own escalation names the fix
        for rec in ESC.list_all(cfg, "open"):
            if rec.get("kind") == ESC_KIND and (rec.get("facts") or [""])[0] == f"backend: {kind}":
                return None
        q, opts, facts = _question(kind, error, cfg)
        plan = ESC.plan_escalate(cfg, None, question=q, options=opts, facts=facts,
                                 frm="conductor", kind=ESC_KIND, topic="human_settings")
        return ESC.apply_escalate(cfg, plan)
    except Exception:                        # noqa: BLE001 — a hold must never fail on this
        return None


def startup_check(cfg, *, err=None) -> str | None:
    """Runner start: warn loudly (stderr, the runner journal, one escalation) when a backend
    this host's config needs fails its preflight. Returns the error, else None."""
    try:
        from .runner import cursor_needed
        if not cursor_needed(cfg):
            return None
    except Exception:                        # noqa: BLE001
        return None
    error = check("cursor")
    if error is None:
        return None
    out = err or sys.stderr
    try:
        out.write(f"WARNING: the cursor persona backend fails its preflight: {error}. Persona "
                  "steps that need it are held until it is fixed (see the escalation).\n")
        out.flush()
    except (OSError, ValueError):
        pass
    note_fault(cfg, "cursor", error, where="start")
    return error


def reset_noted() -> None:
    """Tests: forget which faults this process journaled."""
    with _NOTED_LOCK:
        _NOTED.clear()
