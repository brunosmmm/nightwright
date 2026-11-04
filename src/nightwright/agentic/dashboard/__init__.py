"""The remote web dashboard (SPEC-0400). SPEC-0401 ships the data layer: `nw.dashboard.v1`
(`snapshot.build`), the lazy shared `snapshot.SnapshotCache` and `nw dashboard snapshot`.

Read-only by contract (SPEC-0400 invariant 1): nothing here writes under the data dir except
`dashboard_dir`, which only the server (a later sibling spec) calls. No queue-side module
imports this package (invariant 2, `tests/test_dashboard_imports.py`)."""
from __future__ import annotations

import os
from pathlib import Path

DEFAULTS = {"bind": "tailscale:8791", "allowed_logins": [], "refresh_s": 5, "max_stale_s": 30}


def _positive(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


_VALID = {
    "bind": lambda v: isinstance(v, str) and bool(v.strip()),
    "allowed_logins": lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v),
    "refresh_s": _positive,
    "max_stale_s": _positive,
}


def settings(cfg) -> dict:
    """`agentic.dashboard` with per-key defaults: a malformed value falls back to its default.
    Keys of later children (`max_clients`, `tail_poll_s`, `push`) are accepted and ignored."""
    ag = cfg.get("agentic")
    raw = ag.get("dashboard") if isinstance(ag, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for k, default in DEFAULTS.items():
        v = raw.get(k, default)
        out[k] = list(v) if k == "allowed_logins" and _VALID[k](v) else \
            v if _VALID[k](v) else (list(default) if isinstance(default, list) else default)
    return out


def dashboard_dir(cfg) -> Path:
    """`<data_dir>/agentic/dashboard/` (mode 0700), created on call. The one directory the
    dashboard may write; only the server calls it."""
    from .. import store
    d = store.agentic_dir(cfg) / "dashboard"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d
