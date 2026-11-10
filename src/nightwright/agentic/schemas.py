"""Record schemas for the logs the agentic layer saves (SPEC-0192). Every saved log is JSON or
JSONL with a declared, versioned shape; raw third-party output (claude's stdout, a gate's
command output) is kept beside a nightwright-written JSON record, never mixed with nightwright's own text.

- `nw.ledger.event.v1` — one line of `campaigns/<id>/ledger.jsonl`: the envelope
  `{schema, ts, seq, campaign, type, by}` plus the per-type keys below (values may be null).
  SPEC-0408: a `finding` also carries `key` and `run_id` (see `EVENT_KEYS`), and the `triage`
  event records an applied review- or gate-triager verdict with stable finding keys.
- `nw.run.v1` — `runs/<NNN-persona>/run.json`, normalized from the runner's result.
- `nw.gate.v1` — `gates/<NNN-name>.json`, the sidecar of the raw `gates/<NNN-name>.log`. Optional
  `interrupted` (SPEC-0229) and `summary` (SPEC-0284: parsed `{format, passed, failed, errors,
  skipped, failing}` or `{format, error}`; only for gates with `results:`). SPEC-0289: a retry's
  sidecar (`NNN-<name>.r<n>.json`) carries `attempt`; once a retry ran, attempt 0's sidecar holds
  the final result plus `attempts` (`[{attempt, kind: targeted|full|failed-first, ok, exit_code, timed_out,
  interrupted, duration_s, log, failing}]`) and, when the gate ended ok after a targeted pass,
  `flaky` (the ids). SPEC-0358: a `failed_first` prelude is attempt -1 (`NNN-<name>.ff.json`).
- `nw.runner.record.v1` — one line of `<agentic>/runner.jsonl`, written by `nw queue run`.
- `nw.campaign.history.v1` — one derived campaign history document.
- `nw.history.index.v1` — one derived campaign summary row.
- `nw.history.record.v1` — one derived cross-campaign finding or decision record.
"""
from __future__ import annotations
from ..legacy import schema_ok

LEDGER_SCHEMA = "nw.ledger.event.v1"
RUN_SCHEMA = "nw.run.v1"
GATE_SCHEMA = "nw.gate.v1"
RUNNER_SCHEMA = "nw.runner.record.v1"
HISTORY_SCHEMA = "nw.campaign.history.v1"
HISTORY_INDEX_SCHEMA = "nw.history.index.v1"
HISTORY_RECORD_SCHEMA = "nw.history.record.v1"

HISTORY_KEYS = (
    "schema", "campaign", "generated_at", "signature", "header", "phases", "dominant",
    "timeline", "runs", "gates", "records", "gaps",
)
HISTORY_HEADER_KEYS = (
    "campaign", "kind", "spec_id", "idea_id", "title", "project", "profile", "size", "tone",
    "state", "outcome", "priority", "created_at", "closed_at", "wall_s", "active_s", "loops",
    "loop_max", "runs", "cost_usd", "cap_usd", "conductor_usd", "merged_sha", "pr_url",
    "followup_idea", "park",
)
HISTORY_PHASE_KEYS = (
    "phase", "visits", "wall_s", "run_s", "gate_s", "gate_wait_s", "runner_down_s",
    "other_s", "cost_usd", "runs",
)
HISTORY_TIMELINE_KEYS = ("seq", "ts", "kind", "phase", "loop", "by", "summary", "ref")
HISTORY_RUN_KEYS = (
    "run_id", "persona", "phase", "loop", "model", "effort", "started_at", "duration_s",
    "cost_usd", "budget_frac", "exit_code", "status", "verdict_status", "summary", "has_stream",
)
HISTORY_GATE_KEYS = ("gate_seq", "started_at", "duration_s", "ok", "results")
HISTORY_GATE_RESULT_KEYS = ("name", "required", "ok", "exit_code", "retries", "log")
RECORD_KEYS = (
    "schema", "id", "ts", "source", "kind", "agent", "campaign", "spec_id", "project",
    "severity", "title", "text", "status", "links", "linked_by",
)
RECORD_LINK_KEYS = (
    "run_id", "resolved_by", "followup_idea", "escalation", "thread", "campaign", "gate_seq",
)
INDEX_KEYS = (
    "schema", *HISTORY_HEADER_KEYS, "dominant", "phase_wall_s", "phase_cost_usd",
    "findings_by_severity", "records", "signature",
)

RECORD_SOURCES = frozenset(
    {"ledger", "run", "decisions", "escalation", "mailbox", "spec_review", "flaky", "sentinel"}
)
RECORD_KINDS = frozenset(
    {"finding", "verdict", "triage", "decision", "escalation", "answer", "human_decision",
     "mail", "followup", "spec_review", "close_check", "note", "flaky", "sentinel"}
)
RECORD_STATUSES = frozenset(
    {"open", "fixed", "waived", "filed", "carried", "answered", "withdrawn"}
)
LINKED_BY = frozenset({"field", "inferred", "text"})
OUTCOMES = frozenset({"merged", "handed_off", "abandoned"})
_HISTORY_PHASES = frozenset(
    {"queue", "spec", "implement", "gate", "review", "close", "parked", "waiting", "other"}
)
_RUN_STATUSES = frozenset({"ok", "error", "timeout", "interrupted", "running"})

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
    # SPEC-0493: a finished cadence job run on the project's base (a queue event, `events.py`);
    # `failing` is required too when `ok` is false (see `required_keys`)
    "cadence": ("project", "job", "ok", "sha", "reason", "duration_s"),
    "worker_failed": ("worker", "stage", "error", "job_id"),      # SPEC-0338
    "worker_job_lost": ("worker", "job_id", "attempt", "reason"),  # SPEC-0341
    "worker_job_retried": ("job_id", "from_worker", "to_worker", "attempt"),  # SPEC-0342
    "park": ("reason", "detail", "resume_to"),      # + optional `auto` (SPEC-0302)
    "auto_resume_failed": ("rule", "error"),        # SPEC-0302
    "cap_raised": ("from", "to", "ceiling"),        # SPEC-0422: the cost-cap policy raised the cap
    "review_outcome": ("loop", "score", "pass_score", "outcome", "major", "must_fix"),  # SPEC-0422
    "flaky_recorded": ("ids", "source"),                        # SPEC-0293
    "resume": ("to", "note", "cap_usd", "loops_reset"),
    "close": ("mode",),
    "note": ("text",),
    "backlog": ("ready", "blocked_by"),
    # + optional `key` (`followups.finding_key`) and `run_id` (the producing persona run, null
    # for gate / close / queue-internal findings), stamped by `store.append_event` (SPEC-0408)
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
    # SPEC-0408: one applied triager verdict. Review triager: `waive` = [{id, key, severity,
    # title, reason}], `ignored` = [{id, why}]. Gate triager: `waive` = [{gate, key, reason}],
    # `routed_to` (decide_triage's step kind), `confidence`, `suspected_flaky`, `fix_keys`.
    "triage": ("persona", "run_id", "kind", "disposition", "waive"),
    # SPEC-0420: the close is held while the project's base is red (once per episode)
    "close_held": ("episode", "reason"),
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
                "gate_timeout_deferred": ("campaign", "timeout_regates"),
                "restart": ("id", "phase")}                       # SPEC-0375


def required_keys(etype: str, payload: dict | None = None) -> tuple[str, ...]:
    """The required payload keys of `etype` given the payload: `EVENT_KEYS` plus the
    conditional ones (SPEC-0359 / SPEC-0493: a `sentinel` or `cadence` with `ok` false needs
    `failing`)."""
    keys = EVENT_KEYS.get(etype, ())
    if etype in ("sentinel", "cadence") and (payload or {}).get("ok") is False:
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


def _required(obj, keys, where) -> list[str]:
    if not isinstance(obj, dict):
        return [f"{where} is not an object"]
    return [f"{where}: missing {k!r}" for k in keys if k not in obj]


def _nonnegative(obj, keys, where) -> list[str]:
    out = []
    for key in keys:
        value = obj.get(key) if isinstance(obj, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0:
            out.append(f"{where}.{key} is negative")
    return out


def _array(obj, key, where, out):
    value = obj.get(key) if isinstance(obj, dict) else None
    if value is None:
        return []
    if not isinstance(value, list):
        out.append(f"{where} is not an array")
        return []
    return value


def record_problems_v1(rec) -> list[str]:
    """Problems with one additive `nw.history.record.v1` record."""
    out = _required(rec, RECORD_KEYS, "record")
    if not isinstance(rec, dict):
        return out
    if not schema_ok(rec.get("schema"), HISTORY_RECORD_SCHEMA):
        out.append(f"schema is {rec.get('schema')!r}")
    for key, allowed in (
        ("source", RECORD_SOURCES), ("kind", RECORD_KINDS), ("status", RECORD_STATUSES),
        ("linked_by", LINKED_BY),
    ):
        if rec.get(key) is not None and rec.get(key) not in allowed:
            out.append(f"unknown {key} {rec.get(key)!r}")
    out += _required(rec.get("links"), RECORD_LINK_KEYS, "links")
    out += _nonnegative(rec.get("links"), ("gate_seq",), "links")
    return out


def history_problems(doc) -> list[str]:
    """Problems with one additive `nw.campaign.history.v1` document."""
    out = _required(doc, HISTORY_KEYS, "history")
    if not isinstance(doc, dict):
        return out
    if not schema_ok(doc.get("schema"), HISTORY_SCHEMA):
        out.append(f"schema is {doc.get('schema')!r}")
    header = doc.get("header")
    out += _required(header, HISTORY_HEADER_KEYS, "header")
    out += _nonnegative(
        header, ("wall_s", "active_s", "loops", "loop_max", "runs", "cost_usd", "cap_usd",
                 "conductor_usd"), "header",
    )
    if isinstance(header, dict) and header.get("outcome") is not None \
            and header.get("outcome") not in OUTCOMES:
        out.append(f"unknown outcome {header.get('outcome')!r}")
    dominant = doc.get("dominant")
    out += _required(dominant, ("wall", "cost", "working"), "dominant")
    if isinstance(dominant, dict):
        for key in ("wall", "cost", "working"):
            if dominant.get(key) is not None and dominant[key] not in _HISTORY_PHASES:
                out.append(f"dominant.{key}: unknown phase {dominant[key]!r}")
    for i, phase in enumerate(_array(doc, "phases", "phases", out)):
        where = f"phases[{i}]"
        out += _required(phase, HISTORY_PHASE_KEYS, where)
        out += _nonnegative(
            phase, ("visits", "wall_s", "run_s", "gate_s", "gate_wait_s", "runner_down_s",
                    "other_s", "cost_usd", "runs"), where,
        )
        if isinstance(phase, dict) and phase.get("phase") not in _HISTORY_PHASES:
            out.append(f"{where}: unknown phase {phase.get('phase')!r}")
    for i, row in enumerate(_array(doc, "timeline", "timeline", out)):
        where = f"timeline[{i}]"
        out += _required(row, HISTORY_TIMELINE_KEYS, where)
        out += _nonnegative(row, ("seq", "loop"), where)
        if isinstance(row, dict) and row.get("phase") is not None \
                and row.get("phase") not in _HISTORY_PHASES:
            out.append(f"{where}: unknown phase {row.get('phase')!r}")
    for i, run in enumerate(_array(doc, "runs", "runs", out)):
        where = f"runs[{i}]"
        out += _required(run, HISTORY_RUN_KEYS, where)
        out += _nonnegative(run, ("loop", "duration_s", "cost_usd", "budget_frac"), where)
        if isinstance(run, dict) and run.get("status") not in _RUN_STATUSES:
            out.append(f"{where}: unknown status {run.get('status')!r}")
        if isinstance(run, dict) and run.get("phase") is not None \
                and run.get("phase") not in _HISTORY_PHASES:
            out.append(f"{where}: unknown phase {run.get('phase')!r}")
    for i, gate in enumerate(_array(doc, "gates", "gates", out)):
        where = f"gates[{i}]"
        out += _required(gate, HISTORY_GATE_KEYS, where)
        out += _nonnegative(gate, ("gate_seq", "duration_s"), where)
        results = _array(gate, "results", f"{where}.results", out)
        for j, result in enumerate(results):
            rwhere = f"{where}.results[{j}]"
            out += _required(result, HISTORY_GATE_RESULT_KEYS, rwhere)
            out += _nonnegative(result, ("retries",), rwhere)
    for rec in _array(doc, "records", "records", out):
        out += record_problems_v1(rec)
    gaps = doc.get("gaps")
    if gaps is not None and not isinstance(gaps, list):
        out.append("gaps is not an array")
    return out


def index_problems(row) -> list[str]:
    """Problems with one additive `nw.history.index.v1` row."""
    out = _required(row, INDEX_KEYS, "index")
    if not isinstance(row, dict):
        return out
    if not schema_ok(row.get("schema"), HISTORY_INDEX_SCHEMA):
        out.append(f"schema is {row.get('schema')!r}")
    out += _nonnegative(
        row, ("wall_s", "active_s", "loops", "loop_max", "runs", "cost_usd", "cap_usd",
              "conductor_usd", "records"), "index",
    )
    if row.get("outcome") is not None and row.get("outcome") not in OUTCOMES:
        out.append(f"unknown outcome {row.get('outcome')!r}")
    out += _required(row.get("dominant"), ("wall", "cost", "working"), "dominant")
    if isinstance(row.get("dominant"), dict):
        for key in ("wall", "cost", "working"):
            value = row["dominant"].get(key)
            if value is not None and value not in _HISTORY_PHASES:
                out.append(f"dominant.{key}: unknown phase {value!r}")
    for key in ("phase_wall_s", "phase_cost_usd", "findings_by_severity"):
        value = row.get(key)
        if not isinstance(value, dict):
            out.append(f"{key} is not an object")
        elif any(isinstance(v, (int, float)) and not isinstance(v, bool) and v < 0
                 for v in value.values()):
            out.append(f"{key} has a negative value")
    return out
