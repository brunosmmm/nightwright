"""Record schemas for the logs the agentic layer saves (SPEC-0192). Every saved log is JSON or
JSONL with a declared, versioned shape; raw third-party output (claude's stdout, a gate's
command output) is kept beside a nightwright-written JSON record, never mixed with nightwright's own text.

- `nw.ledger.event.v1` — one line of `campaigns/<id>/ledger.jsonl`: the envelope
  `{schema, ts, seq, campaign, type, by}` plus the per-type keys below (values may be null).
- `nw.run.v1` — `runs/<NNN-persona>/run.json`, normalized from the runner's result.
- `nw.gate.v1` — `gates/<NNN-name>.json`, the sidecar of the raw `gates/<NNN-name>.log`. Optional
  `interrupted` (SPEC-0229) and `summary` (SPEC-0284: parsed `{format, passed, failed, errors,
  skipped, failing}` or `{format, error}`; only for gates with `results:`). SPEC-0289: a retry's
  sidecar (`NNN-<name>.r<n>.json`) carries `attempt`; once a retry ran, attempt 0's sidecar holds
  the final result plus `attempts` (`[{attempt, kind: targeted|full|failed-first, ok, exit_code, timed_out,
  interrupted, duration_s, log, failing}]`) and, when the gate ended ok after a targeted pass,
  `flaky` (the ids). SPEC-0358: a `failed_first` prelude is attempt -1 (`NNN-<name>.ff.json`).
- `nw.runner.record.v1` — one line of `<agentic>/runner.jsonl`, written by `nw queue run`.
"""
from __future__ import annotations
from ..legacy import schema_ok

LEDGER_SCHEMA = "nw.ledger.event.v1"
RUN_SCHEMA = "nw.run.v1"
GATE_SCHEMA = "nw.gate.v1"
RUNNER_SCHEMA = "nw.runner.record.v1"

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
    "gate_waiting": ("slots", "held", "waited_s"),                # SPEC-0309
    "gate.skipped": ("gate", "patterns", "changed_count"),        # SPEC-0350
    # SPEC-0359: a close gate reused the green loop result, or declined to (+ optional
    # `selection_reason`, `full_source` on gate.reused)
    "gate.reused": ("gate", "from_gate", "from_gate_seq", "gated_head", "close_head",
                    "target_sha", "delta_count", "selection_count", "rules"),
    "gate.reuse_declined": ("gate", "rule", "detail"),
    # SPEC-0359: a full-suite sentinel run on the project's base (a queue event, `events.py`);
    # `failing` is required too when `ok` is false (see `required_keys`)
    "sentinel": ("project", "ok", "sha", "suspects"),
    "worker_failed": ("worker", "stage", "error", "job_id"),      # SPEC-0338
    "worker_job_lost": ("worker", "job_id", "attempt", "reason"),  # SPEC-0341
    "park": ("reason", "detail", "resume_to"),      # + optional `auto` (SPEC-0302)
    "auto_resume_failed": ("rule", "error"),        # SPEC-0302
    "resume": ("to", "note", "cap_usd", "loops_reset"),
    "close": ("mode",),
    "note": ("text",),
    "backlog": ("ready", "blocked_by"),
    "finding": ("persona", "severity", "gate", "title", "detail", "files"),
    "priority": ("from", "to"),
    "admitted_after_revision": ("review_sha", "current_sha", "findings"),
    "admitted_revise_pending": ("review_sha", "current_sha", "findings"),     # SPEC-0279
    "admitted_split": ("review_sha", "parts"),                   # SPEC-0290
    "split_finalized": ("original", "parts"),                    # SPEC-0290
    "admitted_split_child": ("origin",),                         # SPEC-0294
    "split_queued": ("queued", "refused"),                       # SPEC-0294
    "rebase_resolved": ("run_id", "target_sha", "conflicts", "resolutions", "summary", "head",
                        "extra_commits", "absorbed_commits", "resolver_commit"),
    "spec_written": ("spec_id", "rel_path", "title", "mode"),
    "idea_link_failed": ("spec_id", "idea", "error"),            # SPEC-0256
    "idea_reconciled": ("spec_id", "idea_id", "from", "to"),     # SPEC-0257
    "outbound_pulled": ("spec_id", "status", "outbox_path", "idea_id", "from", "to"),  # SPEC-0297
    "idea_handed_off": ("spec_id", "idea_id", "pr_url"),         # SPEC-0280
    "idea_linked": ("idea_id",),                                 # SPEC-0292
    "idea_link_skipped": ("spec_id", "reason"),                  # SPEC-0292
    "idea_unlinked": ("idea_id",),                               # SPEC-0292
    "loop_divergence": ("loop", "signals", "rounds"),
    "spec_review": ("verdict", "spec_sha256", "round", "counts", "run_id"),
    "spec_accepted": ("spec_id", "round"),
    "followup": ("persona", "severity", "title", "key", "idea_id", "origin"),
    "human_decision": ("escalation", "kind"),                  # SPEC-0235
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
                "admission_blocked": ("tick", "ready", "slots", "occupied", "holders", "draining"),
                "adopt": ("runs",),
                "gate_timeout_deferred": ("campaign", "timeout_regates")}


def required_keys(etype: str, payload: dict | None = None) -> tuple[str, ...]:
    """The required payload keys of `etype` given the payload: `EVENT_KEYS` plus the
    conditional ones (SPEC-0359: a `sentinel` with `ok` false needs `failing`)."""
    keys = EVENT_KEYS.get(etype, ())
    if etype == "sentinel" and (payload or {}).get("ok") is False:
        keys = (*keys, "failing")
    return keys


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
    if not schema_ok(rec.get("schema", LEDGER_SCHEMA), LEDGER_SCHEMA):
        out.append(f"unknown schema {rec.get('schema')!r}")
    if "seq" in rec and not isinstance(rec["seq"], int):
        out.append("seq is not an integer")
    etype = rec.get("type")
    keys = EVENT_KEYS.get(etype)
    if keys is None:
        out.append(f"unknown event type {etype!r}")
    else:
        out += [f"{etype}: missing {k!r}" for k in required_keys(etype, rec) if k not in rec]
    return out


def record_problems(rec, keys, schema) -> list[str]:
    """Problems with a run / gate record: wrong schema or a missing key."""
    if not isinstance(rec, dict):
        return ["not an object"]
    out = [] if schema_ok(rec.get("schema"), schema) else [f"schema is {rec.get('schema')!r}"]
    return out + [f"missing {k!r}" for k in keys if k not in rec]


def runner_problems(rec) -> list[str]:
    if not isinstance(rec, dict):
        return ["not an object"]
    out = [] if schema_ok(rec.get("schema"), RUNNER_SCHEMA) else [f"schema is {rec.get('schema')!r}"]
    out += [f"missing {k!r}" for k in ("ts", "kind") if k not in rec]
    keys = RUNNER_KINDS.get(rec.get("kind"))
    if keys is None:
        return out + [f"unknown kind {rec.get('kind')!r}"]
    return out + [f"{rec['kind']}: missing {k!r}" for k in keys if k not in rec]
