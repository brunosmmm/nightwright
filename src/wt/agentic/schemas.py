"""Record schemas for the logs the agentic layer saves (SPEC-0192). Every saved log is JSON or
JSONL with a declared, versioned shape; raw third-party output (claude's stdout, a gate's
command output) is kept beside a wt-written JSON record, never mixed with wt's own text.

- `wt.ledger.event.v1` — one line of `campaigns/<id>/ledger.jsonl`: the envelope
  `{schema, ts, seq, campaign, type, by}` plus the per-type keys below (values may be null).
- `wt.run.v1` — `runs/<NNN-persona>/run.json`, normalized from the runner's result.
- `wt.gate.v1` — `gates/<NNN-name>.json`, the sidecar of the raw `gates/<NNN-name>.log`.
- `wt.runner.record.v1` — one line of `<agentic>/runner.jsonl`, written by `wt queue run`.
"""
from __future__ import annotations

LEDGER_SCHEMA = "wt.ledger.event.v1"
RUN_SCHEMA = "wt.run.v1"
GATE_SCHEMA = "wt.gate.v1"
RUNNER_SCHEMA = "wt.runner.record.v1"

ENVELOPE = ("ts", "seq", "campaign", "type", "by")

# Required payload keys per ledger event type. Extra keys are allowed (additive evolution);
# a missing key or an unknown type is an error.
EVENT_KEYS: dict[str, tuple[str, ...]] = {
    "open": ("spec_id", "project", "profile", "base", "base_sha", "worktree", "branch"),
    "transition": ("from", "to", "reason"),
    "run.start": ("persona", "run_id", "model", "effort", "max_budget_usd", "timeout_s",
                  "permission_mode", "prompt_sha256"),
    "run.end": ("persona", "run_id", "exit_code", "is_error", "timed_out", "interrupted",
                "duration_s", "cost_usd", "charged_usd", "usage", "session_id", "verdict_ok",
                "verdict", "result_tail"),
    "cost": ("persona", "delta_usd", "total_usd", "cap_usd"),
    "commit": ("sha", "message"),
    "gate": ("gate_seq", "ok", "started_at", "duration_s", "results"),
    "park": ("reason", "detail", "resume_to"),
    "resume": ("to", "note", "cap_usd", "loops_reset"),
    "close": ("mode",),
    "note": ("text",),
    "backlog": ("ready", "blocked_by"),
    "finding": ("persona", "severity", "gate", "title", "detail", "files"),
    "priority": ("from", "to"),
    "admitted_after_revision": ("review_sha", "current_sha", "findings"),
    "rebase_resolved": ("run_id", "target_sha", "conflicts", "resolutions", "summary", "head"),
    "spec_written": ("spec_id", "rel_path", "title", "mode"),
    "followup": ("persona", "severity", "title", "key", "idea_id", "origin"),
}

RUN_KEYS = ("schema", "campaign", "run_id", "persona", "model", "effort", "max_budget_usd",
            "timeout_s", "permission_mode", "started_at",
            "duration_s", "exit_code", "timed_out", "interrupted", "is_error", "cost_usd",
            "usage", "session_id", "verdict_ok", "verdict", "files")
GATE_KEYS = ("schema", "name", "cmd", "cwd", "required", "ok", "exit_code", "timed_out",
             "timeout_s", "started_at", "duration_s", "log", "error")
RUNNER_KINDS = {"start": ("pid", "argv", "interval_s"),
                "tick": ("tick", "steps", "admitted", "errors"),
                "stop": ("ticks", "reason"),
                "crash": ("ticks", "error"),
                "heartbeat_error": ("error",),
                "config_reloaded": ("scope", "path", "changed"),
                "config_load_failed": ("scope", "path", "error"),
                "reexec_deferred": ("reason", "to_sha"),
                "adopt": ("runs",)}


def with_defaults(etype: str, payload: dict | None = None) -> dict:
    """`payload` with every required key of `etype` present (missing ones as None) — for
    fixtures and tools that only care about some fields."""
    out = {k: None for k in EVENT_KEYS.get(etype, ())}
    out.update(payload or {})
    return out


def ledger_problems(rec) -> list[str]:
    """What is wrong with one ledger record (empty when valid). Records written before
    SPEC-0192 have no `schema` key and are read as v1."""
    if not isinstance(rec, dict):
        return ["not an object"]
    out = [f"missing envelope key {k!r}" for k in ENVELOPE if k not in rec]
    if rec.get("schema", LEDGER_SCHEMA) != LEDGER_SCHEMA:
        out.append(f"unknown schema {rec.get('schema')!r}")
    if "seq" in rec and not isinstance(rec["seq"], int):
        out.append("seq is not an integer")
    etype = rec.get("type")
    keys = EVENT_KEYS.get(etype)
    if keys is None:
        out.append(f"unknown event type {etype!r}")
    else:
        out += [f"{etype}: missing {k!r}" for k in keys if k not in rec]
    return out


def record_problems(rec, keys, schema) -> list[str]:
    """Problems with a run / gate record: wrong schema or a missing key."""
    if not isinstance(rec, dict):
        return ["not an object"]
    out = [] if rec.get("schema") == schema else [f"schema is {rec.get('schema')!r}"]
    return out + [f"missing {k!r}" for k in keys if k not in rec]


def runner_problems(rec) -> list[str]:
    if not isinstance(rec, dict):
        return ["not an object"]
    out = [] if rec.get("schema") == RUNNER_SCHEMA else [f"schema is {rec.get('schema')!r}"]
    out += [f"missing {k!r}" for k in ("ts", "kind") if k not in rec]
    keys = RUNNER_KINDS.get(rec.get("kind"))
    if keys is None:
        return out + [f"unknown kind {rec.get('kind')!r}"]
    return out + [f"{rec['kind']}: missing {k!r}" for k in keys if k not in rec]
