"""`wt campaign export` (SPEC-0236): three flat tables (campaigns, runs, findings) for after-the-fact
analysis of how well each campaign was sized (SPEC-0219). The schema is `wt.campaign.export.v1`:
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
import tempfile
from pathlib import Path

from .. import specmeta
from ..export import _atomic_write
from . import runs as RN
from . import store
from .profiles import resolve_profile

EXPORT_SCHEMA = "wt.campaign.export.v1"
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
)
FINDING_COLUMNS = (
    ("campaign", "str"), ("spec_id", "str"),
    ("seq", "int"), ("ts", "str"), ("state_at", "str"),
    ("persona", "str"), ("severity", "str"), ("gate", "str"), ("ac", "str"), ("title", "str"),
    ("detail", "str"), ("files", "json"),
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
FIX_REASONS = frozenset({"triage_fix", "review_fix"})
_AC_LINE = re.compile(r"^\s*[-*] \[[ xX]\]")

NOTES = [
    "loop_max is resolved from the current profile configuration at export time; a campaign "
    "does not store its loop limit, so it may differ from the limit in force during the run.",
    "cap_usd, cost_cap_frac and the cost_near_cap signal use the final cost cap: "
    "`wt queue resume --raise-cap` overwrites the opening cap (cap_raises counts the raises).",
]


# ---- derivations -------------------------------------------------------------------------

def _parse_ts(value):
    try:
        return dt.datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _secs(a, b):
    return max(0, int((b - a).total_seconds())) if a and b else None


def _frac(num, den):
    if num is None or not den:
        return None
    return round(num / den, 3)


def _time_in_state(state, events, start, end):
    """State → seconds, from the transitions. The campaign starts in QUEUED at `start`; each
    transition credits the interval it closes to its `from`; the open interval runs to `end`."""
    out: dict[str, int] = {}
    cur, cursor = "QUEUED", start
    for ev in events:
        if ev.get("type") != "transition":
            continue
        ts = _parse_ts(ev.get("ts"))
        frm = ev.get("from") or cur
        if cursor and ts:
            out[frm] = out.get(frm, 0) + _secs(cursor, ts)
        cur = ev.get("to") or cur
        cursor = ts or cursor
    final = state.get("state")
    if final not in store.TERMINAL_STATES and cursor and end:
        out[final] = out.get(final, 0) + _secs(cursor, end)
    return out


def _spec_shape(cfg, cid):
    path = store.campaign_dir(cfg, cid) / "spec_at_open.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, None
    section = next((v for k, v in specmeta.sections(text).items()
                    if k.lower() == "acceptance criteria"), "")
    return sum(1 for line in section.splitlines() if _AC_LINE.match(line)), len(text.splitlines())


def _loop_max(cfg, state):
    try:
        return resolve_profile(cfg, state.get("profile")).get("loop_max")
    except ValueError:
        return None


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


def campaign_row(cfg, state: dict, events: list[dict], now) -> dict:
    cid = state["id"]
    sizing = state.get("sizing") or {}
    counters = state.get("counters") or {}
    cost = state.get("cost") or {}
    close = state.get("close") or {}
    st = state.get("state")

    opened = next((_parse_ts(e.get("ts")) for e in events if e.get("type") == "open"), None)
    start = opened or _parse_ts(state.get("created_at"))
    ended = next((e for e in reversed(events)
                  if e.get("type") == "transition" and e.get("to") in store.TERMINAL_STATES), None)
    ended_at = ended.get("ts") if ended else None
    end = _parse_ts(ended_at) if ended_at else (None if st in store.TERMINAL_STATES else now)
    tis = _time_in_state(state, events, start, end)

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
        "active_s": sum(v for k, v in tis.items() if k in _active_states()),
        "size": sizing.get("size"), "tone": sizing.get("tone"), "profile": state.get("profile"),
        "profile_source": sizing.get("source"), "sizing_rationale": sizing.get("rationale"),
        "loop_max": _loop_max(cfg, state), "cap_usd": cap,
        "cap_raises": len(of("resume", lambda e: e.get("cap_usd") is not None)),
        "models": models, "ac_count": ac_count, "spec_lines": spec_lines,
        "loops": len(of("transition", lambda e: e.get("to") == "IMPLEMENTING"
                        and e.get("reason") in FIX_REASONS)),
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
        "time_in_state_s": tis,
        "review_score": (state.get("last_review") or {}).get("score"),
        "must_fix_count": sum(1 for e in findings if e.get("severity") == "must_fix"),
        "finding_count": sum(1 for e in findings if e.get("severity") != "info"),
        "waived_count": len(of("note", lambda e: e.get("author") == "queue"
                               and str(e.get("text") or "").startswith("waived "))),
        "close_mode": close.get("mode"), "merged_sha": close.get("merged_sha"),
    }
    row["signals"] = signals(row)
    return {name: row.get(name) for name, _ in CAMPAIGN_COLUMNS}


def _active_states():
    from .queue import ACTIVE_STATES        # local: queue imports a lot
    return ACTIVE_STATES


def run_rows(cfg, crow: dict) -> list[dict]:
    out = []
    for r in RN.campaign_runs(cfg, crow["campaign"]):
        usage = r.get("usage") if isinstance(r.get("usage"), dict) else {}
        row = {**{k: crow.get(k) for k in ("campaign", "spec_id", "profile", "size", "tone")},
               **r, "budget_frac": _frac(r.get("charged_usd"), r.get("max_budget_usd")),
               "input_tokens": usage.get("input_tokens"),
               "output_tokens": usage.get("output_tokens"),
               "cache_read_tokens": usage.get("cache_read_input_tokens")}
        out.append({name: row.get(name) for name, _ in RUN_COLUMNS})
    return out


def finding_rows(crow: dict, events: list[dict]) -> list[dict]:
    out, cur = [], "QUEUED"
    for e in events:
        if e.get("type") == "transition":
            cur = e.get("to") or cur
        elif e.get("type") == "finding":
            row = {**{k: crow.get(k) for k in ("campaign", "spec_id")}, **e, "state_at": cur,
                   "detail": (e.get("detail") or None) and str(e["detail"])[:FINDING_DETAIL_MAX]}
            out.append({name: row.get(name) for name, _ in FINDING_COLUMNS})
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
    for s in states:
        events = store.read_ledger(cfg, s["id"])
        crow = campaign_row(cfg, s, events, now)
        tables["campaigns"].append(crow)
        tables["runs"].extend(run_rows(cfg, crow))
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
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".wt-export-", suffix=".tmp")
    os.close(fd)
    try:
        pq.write_table(table, tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _wt_version():
    try:
        return importlib.metadata.version("wt")
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
                "wt_version": _wt_version(), "format": fmt, "filters": filters,
                "tables": manifest_tables, "notes": list(NOTES)}
    _atomic_write(out_dir / "manifest.json",
                  json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest
