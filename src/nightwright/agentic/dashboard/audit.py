"""Dashboard audit log (SPEC-0414, SPEC-0429): `agentic/dashboard/audit.jsonl`, one
`nw.dashboard.audit.v1` line per refused request and per escalation plan / answer (`action`). Appends under a module lock, rotates once at 5 MiB (one backup) and
never raises into a request. Never records a token, `Authorization` header or query string."""
from __future__ import annotations

import json
import os
import threading

from .. import store
from . import dashboard_dir

SCHEMA = "nw.dashboard.audit.v1"
MAX_BYTES = 5 * 1024 * 1024
FIELDS = ("reason", "status", "method", "path", "login", "node", "node_ip", "user_agent",
          "detail", "esc_id", "form", "summary", "result")
ACTIONS = ("refused", "plan", "answer")
_lock = threading.Lock()


def _cut(v, n=200):
    return v[:n] if isinstance(v, str) else v


def record(**fields) -> dict:
    """The audit line for `fields` (unknown keys are dropped; a path loses its query)."""
    action = fields.get("action")
    rec = {"schema": SCHEMA, "ts": store.now_iso(),
           "action": action if action in ACTIONS else "refused"}
    for k in FIELDS:
        rec[k] = fields.get(k)
    if isinstance(rec["path"], str):
        rec["path"] = rec["path"].split("?", 1)[0].split("#", 1)[0]
    for k in ("path", "user_agent", "detail", "summary"):
        rec[k] = _cut(rec[k], 400 if k == "summary" else 200)
    return rec


def audit(cfg, **fields) -> None:
    try:
        line = (json.dumps(record(**fields)) + "\n").encode("utf-8")
        with _lock:
            p = dashboard_dir(cfg) / "audit.jsonl"
            try:
                if p.stat().st_size + len(line) > MAX_BYTES:
                    os.replace(p, p.with_name("audit.jsonl.1"))
            except FileNotFoundError:
                pass
            fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
    except Exception:                       # noqa: BLE001 - an audit failure never fails a request
        pass
