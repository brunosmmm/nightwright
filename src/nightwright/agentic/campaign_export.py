"""`nw campaign export` (SPEC-0236): three flat tables (campaigns, runs, findings) for after-the-fact
analysis of how well each campaign was sized (SPEC-0219). The schema is `nw.campaign.export.v1`:
the column tuples below are the contract, and under v1 a column may only be appended. Rows are
derived read-only from `state.json`, `ledger.jsonl`, `runs.campaign_runs` and `spec_at_open.md`;
totals come from the ledger because `state.counters` holds per-loop streaks that get reset."""
from __future__ import annotations

import csv
import datetime as dt
import importlib.metadata
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from .. import specmeta
from ..export import _atomic_write
from . import personas as PS
from . import persona_gate as PG
from . import runs as RN
from . import store
from . import history as H

EXPORT_SCHEMA = "nw.campaign.export.v1"
FORMATS = ("jsonl", "csv", "parquet")
TABLES = ("campaigns", "runs", "findings")

CAMPAIGN_COLUMNS = (
    ("campaign", "str"), ("kind", "str"), ("spec_id", "str"), ("project", "str"),
    ("state", "str"), ("outcome", "str"), ("created_at", "str"), ("ended_at", "str"),
    ("wall_s", "int"), ("active_s", "int"),
    ("size", "str"), ("tone", "str"), ("profile", "str"), ("profile_source", "str"),
    ("sizing_rationale", "str"),
    ("loop_max", "int"), ("cap_usd", "float"), ("cap_raises", "int"), ("models", "json"),
    ("ac_count", "int"), ("spec_lines", "int"),
    ("loops", "int"), ("loop_resets", "int"), ("runs", "int"), ("runs_by_persona", "json"),
    ("gate_runs", "int"), ("gate_failures", "int"), ("gate_retries", "int"),
    ("triage_retries", "int"), ("parks", "int"), ("park_reasons", "json"),
    ("cost_usd", "float"), ("cost_by_persona", "json"), ("cost_cap_frac", "float"),
    ("time_in_state_s", "json"), ("review_score", "int"), ("must_fix_count", "int"),
    ("finding_count", "int"), ("waived_count", "int"),
    ("close_mode", "str"), ("merged_sha", "str"), ("signals", "json"),
    ("conductor_usd", "float"), ("cost_with_conductor_usd", "float"),     # SPEC-0299
    ("rebase_regenerated", "int"), ("resolver_dispatches", "int"),         # SPEC-0303
    ("rebase_resolved", "int"), ("resolver_overcap_usd", "float"),
    ("reviewer_runs", "int"), ("reviewer_findings", "int"),                # SPEC-0396
    ("reviewer_blocking", "int"), ("findings_per_reviewer_run", "float"),
    ("blocking_per_reviewer_run", "float"), ("first_review_must_fix", "int"),
    ("first_review_major", "int"), ("close_s", "int"), ("task_active_s", "int"),
    ("nw_sha", "str"), ("profile_sha256", "str"),                          # SPEC-0397
    ("cost_by_worker", "json"),                                            # SPEC-0345
    ("lean", "json"), ("raised", "bool"),                                  # SPEC-0553
    ("demoted_findings", "int"),                                           # SPEC-0554
    ("out_of_scope_findings", "int"), ("collator_fallbacks", "int"),       # SPEC-0555
    ("cohort", "str"), ("lean_request", "json"),                           # SPEC-0558
    ("outside_delta_findings", "int"),                                     # SPEC-0596
)
RUN_COLUMNS = (
    ("campaign", "str"), ("spec_id", "str"), ("profile", "str"), ("size", "str"), ("tone", "str"),
    ("run_id", "str"), ("persona", "str"), ("model", "str"), ("effort", "str"),
    ("max_budget_usd", "float"), ("timeout_s", "int"), ("started_at", "str"),
    ("duration_s", "float"), ("cost_usd", "float"), ("charged_usd", "float"),
    ("budget_frac", "float"),
    ("exit_code", "int"), ("is_error", "bool"), ("timed_out", "bool"), ("interrupted", "bool"),
    ("transient", "bool"), ("verdict_status", "str"), ("status", "str"),
    ("input_tokens", "int"), ("output_tokens", "int"), ("cache_read_tokens", "int"),
    ("worker", "str"),                                                     # SPEC-0346
    ("tier", "str"),                                                       # SPEC-0553
    ("loop", "int"), ("packet", "bool"),                                   # SPEC-0554
    ("effort_clamped_from", "str"),                                        # SPEC-0557
    ("resumed", "bool"), ("session_fallback", "bool"),                     # SPEC-0556
)
FINDING_COLUMNS = (
    ("campaign", "str"), ("spec_id", "str"),
    ("seq", "int"), ("ts", "str"), ("state_at", "str"),
    ("persona", "str"), ("severity", "str"), ("gate", "str"), ("ac", "str"), ("title", "str"),
    ("detail", "str"), ("files", "json"),
    ("scope", "str"), ("tie", "str"),                                      # SPEC-0555
)
COLUMNS = {"campaigns": CAMPAIGN_COLUMNS, "runs": RUN_COLUMNS, "findings": FINDING_COLUMNS}

# Signal thresholds (tunable; the rules are part of the v1 contract, see `signals`).
NEAR_CAP_FRAC = 0.8
MANY_LOOPS = 2
GATE_CHURN = 3
LONG_FOR_SIZE_S = {"xs": 3600, "s": 7200, "m": 14400, "l": 28800}
SIGNAL_ORDER = ("loops_exhausted", "cap_parked", "cost_near_cap", "many_fix_loops", "escalated",
                "gate_churn", "long_for_size", "abandoned")
ESCALATION_REASONS = frozenset({"triage_escalated", "review_escalated", "implementer_blocked"})
FINDING_DETAIL_MAX = 500
FIX_REASONS = H.FIX_REASONS
_REVIEWERS = frozenset(PS.REVIEWER_PERSONAS)
BLOCKING = ("must_fix", "major")         # as divergence.BLOCKING
_AC_LINE = re.compile(r"^\s*[-*] \[[ xX]\]")

NOTES = [
    "loop_max is resolved from the current profile configuration at export time; a campaign "
    "does not store its loop limit, so it may differ from the limit in force during the run.",
    "cap_usd, cost_cap_frac and the cost_near_cap signal use the final cost cap: "
    "`nw queue resume --raise-cap` overwrites the opening cap (cap_raises counts the raises).",
    "conductor_usd is the conductor's share (SPEC-0299): each conductor turn is split equally "
    "among the campaigns live at that moment (open to terminal transition). It is null when no "
    "conductor session is registered or no turn is priced (agentic.conductor.prices); "
    "cost_with_conductor_usd = (cost_usd or 0) + conductor_usd.",
    "rebase_regenerated, resolver_dispatches and rebase_resolved count the close's rebase "
    "findings and events (SPEC-0303); resolver_overcap_usd is the conflict_resolver spend above "
    "the campaign cap (the close allowance), 0 when there was none.",
    "reviewer_runs, reviewer_findings, reviewer_blocking, the *_per_reviewer_run ratios and "
    "first_review_must_fix / first_review_major (SPEC-0396) compare campaigns across profiles. "
    "Only builtin reviewer personas are counted (not reviewers declared by users or projects); a "
    "reviewer run counts only with a valid verdict; blocking is must_fix plus major. "
    "first_review_* counts reviewer findings before the first return to IMPLEMENTING after the "
    "first REVIEWING entry: it does not depend on how many fix rounds the profile allows, but "
    "still scales with the number of reviewer personas in the first round, so compare it within "
    "the same reviewer set or alongside findings_per_reviewer_run. task_active_s = active_s - "
    "close_s excludes PARKED and CLOSING (rebase, resolver, merge), whereas active_s excludes "
    "only PARKED.",
    "nw_sha is the git sha of the nightwright code checkout of the process that admitted the "
    "campaign (SPEC-0397), not of later runs: the runner re-execs on code changes, so a long "
    "campaign can run under several shas. It is null for campaigns opened before SPEC-0397 and "
    "for installs that are not a git checkout. profile_sha256 is the sha256 of the resolved "
    "profile at admission without its `sources`; null for earlier campaigns.",
]


# ---- derivations -------------------------------------------------------------------------

def _parse_ts(value):
    try:
        return dt.datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _secs(a, b):
    if not (a and b):
        return None
    if (a.tzinfo is None) != (b.tzinfo is None):    # a naive stamp is read in the other's zone
        a, b = (a.replace(tzinfo=b.tzinfo), b) if a.tzinfo is None else (a, b.replace(tzinfo=a.tzinfo))
    return max(0, int((b - a).total_seconds()))


def _coerce(value, typ):
    """`value` as the declared column type, or null when it cannot be. Values from LLM verdicts
    and config (a float review score, a non-string `ac`) must not break a typed column."""
    if value is None or typ == "json":
        return value
    try:
        if typ == "str":
            return value if isinstance(value, str) else (
                json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list))
                else str(value))
        if typ == "bool":
            if isinstance(value, str):
                return {"true": True, "false": False}.get(value.strip().lower())
            return bool(value) if isinstance(value, (bool, int, float)) else None
        if typ == "float":
            return float(value)
        return value if type(value) is int else int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        return None


def _project(row, columns):
    return {name: _coerce(row.get(name), typ) for name, typ in columns}


def _frac(num, den):
    num, den = _coerce(num, "float"), _coerce(den, "float")
    if num is None or not den:
        return None
    return round(num / den, 3)


_time_in_state = H.time_in_state


def _spec_shape(cfg, cid):
    path = store.campaign_dir(cfg, cid) / "spec_at_open.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, None
    section = next((v for k, v in specmeta.sections(text).items()
                    if k.lower() == "acceptance criteria"), "")
    return sum(1 for line in section.splitlines() if _AC_LINE.match(line)), len(text.splitlines())


_loop_max = H.loop_max


def signals(row: dict) -> list[str]:
    """Named mis-sizing signals for one campaign row, in `SIGNAL_ORDER`."""
    reasons = row.get("park_reasons") or []

    def ge(key, limit):
        return row.get(key) is not None and row[key] >= limit

    size = row.get("size")
    on = {
        "loops_exhausted": "loop_exhausted" in reasons,
        "cap_parked": "cost_cap" in reasons,
        "cost_near_cap": ge("cost_cap_frac", NEAR_CAP_FRAC),
        "many_fix_loops": ge("loops", MANY_LOOPS),
        "escalated": any(r in ESCALATION_REASONS for r in reasons),
        "gate_churn": ge("gate_failures", GATE_CHURN),
        "long_for_size": (size in LONG_FOR_SIZE_S and row.get("active_s") is not None
                          and row["active_s"] > LONG_FOR_SIZE_S[size]),
        "abandoned": row.get("outcome") == "ABANDONED",
    }
    return [name for name in SIGNAL_ORDER if on[name]]


def _resolver_overcap(events: list[dict]) -> float:
    """SPEC-0303: the sum, over `conflict_resolver` `cost` events, of the part of the charge that
    landed above `cap_usd`: `max(0, total - max(cap, total - delta))`."""
    over = 0.0
    for e in events:
        if e.get("type") != "cost" or e.get("persona") != "conflict_resolver":
            continue
        cap, total, delta = e.get("cap_usd"), e.get("total_usd"), e.get("delta_usd")
        if cap is None or total is None or delta is None:
            continue
        over += max(0.0, float(total) - max(float(cap), float(total) - float(delta)))
    return round(over, 4)


def _reviewer_stats(events: list[dict]) -> dict:
    """SPEC-0396: reviewer-only run/finding counts and the first review round's blockers. The
    first round ends at the first transition to IMPLEMENTING after the first entry into REVIEWING,
    whatever its reason (`review_fix`, or the resume after a `loop_exhausted` park)."""
    runs = findings = blocking = first_must = first_major = 0
    reviewed = first_round_over = False
    discarded: set = set()  # reviewer personas whose latest run.end was malformed or interrupted
    for e in events:
        kind = e.get("type")
        if kind == "transition":
            if e.get("to") == "REVIEWING":
                reviewed = True
            elif e.get("to") == "IMPLEMENTING" and reviewed:
                first_round_over = True
        elif kind == "run.end" and e.get("persona") in _REVIEWERS:
            if e.get("verdict_ok") is True and not e.get("interrupted"):
                runs += 1
            # `_record_run` writes a run's findings right after its run.end, even when the verdict
            # was non-conforming (the retry then repeats them); drop those with the run. A missing
            # verdict_ok (older ledger) is unknown, so its findings still count.
            if e.get("verdict_ok") is False or e.get("interrupted"):
                discarded.add(e.get("persona"))
            else:
                discarded.discard(e.get("persona"))
        elif kind == "finding" and e.get("persona") in _REVIEWERS:
            if e.get("persona") in discarded:
                continue
            sev = e.get("severity")
            if sev == "info":
                continue
            findings += 1
            if sev in BLOCKING:  # a non-string or unknown severity matches nothing
                blocking += 1
                if not first_round_over:
                    first_must += sev == "must_fix"
                    first_major += sev == "major"
    return {"reviewer_runs": runs, "reviewer_findings": findings, "reviewer_blocking": blocking,
            "findings_per_reviewer_run": _frac(findings, runs),
            "blocking_per_reviewer_run": _frac(blocking, runs),
            "first_review_must_fix": first_must, "first_review_major": first_major}


def _demoted(events, key="demoted") -> int:
    """SPEC-0554: new findings the delta rule demoted to advisory, summed over the review
    loops (the last `review_outcome` of each loop counts: a re-route repeats the number).
    SPEC-0596: `key="outside_delta"` sums the majors / must-fixes kept blocking instead."""
    per_loop = {}
    for e in events:
        if e.get("type") == "review_outcome" and isinstance(e.get(key), int):
            per_loop[e.get("loop")] = e[key]
    return sum(per_loop.values())


def _collation_stats(events) -> tuple:
    """SPEC-0555: `(out_of_scope_findings, collator_fallbacks)`: the distinct finding keys any
    loop's last collation scoped out, and the `collator_failed` events."""
    per_loop = {}
    for e in events:
        if e.get("type") == "findings_collated" and isinstance(e.get("scopes"), list):
            per_loop[e.get("loop")] = e["scopes"]
    out = {s.get("key") or s.get("id") for scopes in per_loop.values() for s in scopes
           if isinstance(s, dict) and s.get("scope") == "out"}
    return len(out), sum(1 for e in events if e.get("type") == "collator_failed")


def campaign_row(cfg, state: dict, events: list[dict], now) -> dict:
    cid = state["id"]
    sizing = state.get("sizing") or {}
    counters = state.get("counters") or {}
    cost = state.get("cost") or {}
    close = state.get("close") or {}
    st = state.get("state")

    open_ev = next((e for e in events if e.get("type") == "open"), None) or {}
    opened = _parse_ts(open_ev.get("ts"))
    start = opened or _parse_ts(state.get("created_at"))
    ended = next((e for e in reversed(events)
                  if e.get("type") == "transition" and e.get("to") in store.TERMINAL_STATES), None)
    ended_at = ended.get("ts") if ended else None
    end = _parse_ts(ended_at) if ended_at else (None if st in store.TERMINAL_STATES else now)
    tis = _time_in_state(state, events, start, end)
    active_s = sum(v for k, v in tis.items() if k in _active_states())
    close_s = tis.get("CLOSING", 0)

    def of(t, pred=lambda e: True):
        return [e for e in events if e.get("type") == t and pred(e)]

    starts = of("run.start")
    by_persona: dict[str, int] = {}
    models: dict[str, str] = {}
    for e in starts:
        p = e.get("persona")
        by_persona[p] = by_persona.get(p, 0) + 1
        models[p] = e.get("model")
    findings = of("finding")
    cap = cost.get("cap_usd")
    ac_count, spec_lines = _spec_shape(cfg, cid)
    total = cost.get("total_usd")

    row = {
        "campaign": cid, "kind": state.get("kind"), "spec_id": state.get("spec_id"),
        "project": state.get("project"), "state": st,
        "outcome": st if st in store.TERMINAL_STATES else None,
        "created_at": state.get("created_at"), "ended_at": ended_at,
        "wall_s": _secs(_parse_ts(state.get("created_at")), end),
        "active_s": active_s,
        "size": sizing.get("size"), "tone": sizing.get("tone"), "profile": state.get("profile"),
        "profile_source": sizing.get("source"), "sizing_rationale": sizing.get("rationale"),
        "loop_max": _loop_max(cfg, state), "cap_usd": cap,
        "cap_raises": len(of("resume", lambda e: e.get("cap_usd") is not None)),
        "models": models, "ac_count": ac_count, "spec_lines": spec_lines,
        "loops": H.count_loops(events),
        "loop_resets": len(of("resume", lambda e: e.get("loops_reset") is True)),
        "runs": counters.get("runs"), "runs_by_persona": by_persona,
        "gate_runs": counters.get("gate_runs"),
        "gate_failures": len(of("gate", lambda e: e.get("ok") is False)),
        "gate_retries": len(of("transition", lambda e: e.get("reason") == "retry_gate")),
        "triage_retries": len(of("run.end", lambda e: e.get("persona") == "gate_triager"
                                 and e.get("verdict_ok") is False and not e.get("interrupted"))),
        "parks": counters.get("parks"),
        "park_reasons": [e.get("reason") for e in of("park")],
        "cost_usd": total, "cost_by_persona": cost.get("by_persona") or {},
        "cost_cap_frac": _frac(total, cap),
        "cost_by_worker": PG.cost_by_worker(events),
        "time_in_state_s": tis,
        "review_score": (state.get("last_review") or {}).get("score"),
        "must_fix_count": sum(1 for e in findings if e.get("severity") == "must_fix"),
        "finding_count": sum(1 for e in findings if e.get("severity") != "info"),
        "waived_count": len(of("note", lambda e: e.get("author") == "queue"
                               and str(e.get("text") or "").startswith("waived "))),
        "close_mode": close.get("mode"), "merged_sha": close.get("merged_sha"),
        "rebase_regenerated": sum(1 for e in findings
                                  if e.get("title") == "generated files regenerated during rebase"),
        "resolver_dispatches": sum(1 for e in findings
                                   if e.get("title") == "rebase conflict; resolver dispatched"),
        "rebase_resolved": len(of("rebase_resolved")),
        "resolver_overcap_usd": _resolver_overcap(events),
        **_reviewer_stats(events), "close_s": close_s,
        "task_active_s": max(0, active_s - close_s),
        "nw_sha": open_ev.get("nw_sha"), "profile_sha256": open_ev.get("profile_sha256"),
        "lean": state.get("lean"), "raised": bool(state.get("raised")),    # SPEC-0553
        "demoted_findings": _demoted(events),                              # SPEC-0554
        "out_of_scope_findings": _collation_stats(events)[0],              # SPEC-0555
        "collator_fallbacks": _collation_stats(events)[1],
        "cohort": state.get("cohort"), "lean_request": state.get("lean_request"),  # SPEC-0558
        "outside_delta_findings": _demoted(events, "outside_delta"),       # SPEC-0596
    }
    out = _project(row, CAMPAIGN_COLUMNS)       # signals read the coerced values
    out["signals"] = signals(out)
    return out


def _active_states():
    from .queue import ACTIVE_STATES        # local: queue imports a lot
    return ACTIVE_STATES


def run_rows(cfg, crow: dict, events=None) -> list[dict]:
    """One row per run. `worker` is the run.end event's worker id, `local` for a run that ran on
    the coordinator (as `cost_by_worker` labels it), null for a run with no run.end (SPEC-0346).
    The run records and that worker map are both taken from `events` when it is passed, so a
    ledger that grows between two reads cannot split them."""
    if events is None:
        events = store.read_ledger(cfg, crow["campaign"], types=["run.start", "run.end"])
    workers = {e.get("run_id"): e.get("worker") or "local"
               for e in events if e.get("type") == "run.end" and e.get("run_id")}
    starts = {e.get("run_id"): e for e in events
              if e.get("type") == "run.start" and e.get("run_id")}
    tiers = {k: e.get("tier") for k, e in starts.items()}                # SPEC-0553
    ends = {e.get("run_id"): e for e in events
            if e.get("type") == "run.end" and e.get("run_id")}
    out = []
    for r in RN.campaign_runs(cfg, crow["campaign"], events):
        usage = r.get("usage") if isinstance(r.get("usage"), dict) else {}
        row = {**{k: crow.get(k) for k in ("campaign", "spec_id", "profile", "size", "tone")},
               **r, "budget_frac": _frac(r.get("charged_usd"), r.get("max_budget_usd")),
               "tier": tiers.get(r.get("run_id")),
               "effort_clamped_from": (starts.get(r.get("run_id")) or {})
               .get("effort_clamped_from"),                                 # SPEC-0557
               "loop": (starts.get(r.get("run_id")) or {}).get("loop"),     # SPEC-0554
               "packet": bool((starts.get(r.get("run_id")) or {}).get("packet")),
               "resumed": bool(ends.get(r.get("run_id"), {}).get("resumed")),   # SPEC-0556
               "session_fallback": bool((starts.get(r.get("run_id")) or {})
                                        .get("session_fallback")),
               "input_tokens": usage.get("input_tokens"),
               "output_tokens": usage.get("output_tokens"),
               "cache_read_tokens": usage.get("cache_read_input_tokens"),
               "worker": workers.get(r.get("run_id"))}
        out.append(_project(row, RUN_COLUMNS))
    return out


def finding_rows(crow: dict, events: list[dict]) -> list[dict]:
    """One row per `finding` event. SPEC-0555: a reviewer finding gets the `scope` / `tie` of
    the first collation after it that lists its key (null without one)."""
    from .followups import finding_key
    out, cur, open_rows = [], "QUEUED", []
    for e in events:
        if e.get("type") == "transition":
            cur = e.get("to") or cur
        elif e.get("type") == "finding":
            row = {**{k: crow.get(k) for k in ("campaign", "spec_id")}, **e, "state_at": cur,
                   "detail": (e.get("detail") or None) and str(e["detail"])[:FINDING_DETAIL_MAX]}
            row.pop("scope", None)
            row.pop("tie", None)
            out.append(_project(row, FINDING_COLUMNS))
            if e.get("persona") in _REVIEWERS:
                open_rows.append((out[-1], finding_key(crow.get("spec_id"), e.get("title"),
                                                       e.get("files"))))
        elif e.get("type") == "findings_collated" and isinstance(e.get("scopes"), list):
            hits = {s.get("key"): s for s in e["scopes"] if isinstance(s, dict) and s.get("key")}
            left = []
            for row, key in open_rows:
                if key in hits:
                    row["scope"], row["tie"] = hits[key].get("scope"), hits[key].get("tie")
                else:
                    left.append((row, key))
            open_rows = left
    return out


def _created_date(cfg, state):
    ts = _parse_ts(state.get("created_at"))
    if ts is None:
        return None
    tz = (cfg or {}).get("_tz")
    if tz is None:
        return ts.date()
    return (ts.replace(tzinfo=tz) if ts.tzinfo is None else ts.astimezone(tz)).date()


def export_tables(cfg, *, project=None, since=None, terminal_only=False, now=None) -> dict:
    """`{"campaigns": [...], "runs": [...], "findings": [...]}`, campaigns oldest first.
    `since` is a `datetime.date`, compared with the campaign's local date in `cfg["_tz"]`."""
    now = now or dt.datetime.now().astimezone()
    states = store.list_campaigns(cfg, include_terminal=True, project=project)
    if terminal_only:
        states = [s for s in states if s.get("state") in store.TERMINAL_STATES]
    if since is not None:
        states = [s for s in states if (d := _created_date(cfg, s)) is not None and d >= since]
    states.sort(key=lambda s: (s.get("created_at") or "", s.get("id") or ""))
    tables: dict[str, list] = {"campaigns": [], "runs": [], "findings": []}
    try:
        shares = H._conductor_attribution(cfg, now)  # SPEC-0299: campaign id -> conductor USD
    except Exception as e:      # measurement is an extra column set; it must not fail an export
        shares = None
        print(f"conductor columns skipped: {type(e).__name__}", file=sys.stderr)
    for s in states:
        events = store.read_ledger(cfg, s["id"])
        crow = campaign_row(cfg, s, events, now)
        if shares is not None:
            share = shares.get(s["id"], 0.0)
            crow["conductor_usd"] = _coerce(share, "float")
            crow["cost_with_conductor_usd"] = (crow["cost_usd"] or 0) + share
        tables["campaigns"].append(crow)
        tables["runs"].extend(run_rows(cfg, crow, events))
        tables["findings"].extend(finding_rows(crow, events))
    return tables


# ---- writing -----------------------------------------------------------------------------

def _csv_cell(value, typ):
    if value is None:
        return ""
    if typ == "json":
        return json.dumps(value, ensure_ascii=False)
    if typ == "bool":
        return "true" if value else "false"
    return value


def render(rows, columns, fmt) -> str:
    """One table as `jsonl` or `csv` text."""
    if fmt == "jsonl":
        return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    if fmt != "csv":
        raise ValueError(f"cannot render {fmt!r} as text")
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=[n for n, _ in columns], lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({n: _csv_cell(r.get(n), t) for n, t in columns})
    return buf.getvalue()


def have_pyarrow() -> bool:
    try:
        import pyarrow  # noqa: F401
        import pyarrow.parquet  # noqa: F401
    except ImportError:
        return False
    return True


def _parquet_table(rows, columns):
    import pyarrow as pa

    kinds = {"str": pa.string(), "int": pa.int64(), "float": pa.float64(), "bool": pa.bool_(),
             "json": pa.string()}
    schema = pa.schema([(n, kinds[t]) for n, t in columns])
    cells = [{n: (None if r.get(n) is None else json.dumps(r[n], ensure_ascii=False) if t == "json"
                  else r[n]) for n, t in columns} for r in rows]
    return pa.Table.from_pylist(cells, schema=schema)


def _write_parquet(path: Path, table) -> None:
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".nw-export-", suffix=".tmp")
    os.close(fd)
    try:
        pq.write_table(table, tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _nw_version():
    try:
        return importlib.metadata.version("nightwright")
    except importlib.metadata.PackageNotFoundError:
        return None


def write_export(tables, out_dir, fmt, *, filters) -> dict:
    """Write the three tables and `manifest.json` into `out_dir`; returns the manifest."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}")
    out_dir = Path(out_dir)
    manifest_tables = {}
    # Build every Parquet table before writing one: a value that does not fit its column type
    # (a hand-edited ledger) must fail before the directory holds a partial export.
    arrow = ({name: _parquet_table(tables[name], COLUMNS[name]) for name in TABLES}
             if fmt == "parquet" else {})
    for name in TABLES:
        columns, rows = COLUMNS[name], tables[name]
        fname = f"{name}.{fmt}"
        if fmt == "parquet":
            _write_parquet(out_dir / fname, arrow[name])
        else:
            _atomic_write(out_dir / fname, render(rows, columns, fmt), encoding="utf-8")
        manifest_tables[name] = {"file": fname, "rows": len(rows),
                                 "columns": [[n, t] for n, t in columns]}
    manifest = {"schema": EXPORT_SCHEMA, "generated_at": store.now_iso(),
                "nw_version": _nw_version(), "format": fmt, "filters": filters,
                "tables": manifest_tables, "notes": list(NOTES)}
    _atomic_write(out_dir / "manifest.json",
                  json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest
