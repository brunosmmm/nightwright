"""Pure, defensive read-side derivation for campaign history (SPEC-0406)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections import namedtuple
from pathlib import Path

from . import schemas as SC
from . import store
from .followups import finding_key
from .profiles import resolve_profile

__all__ = (
    "PHASES", "PHASE_OF", "FIX_REASONS", "Visit", "is_fix_loop", "count_loops", "loop_max",
    "time_in_state", "phase_visits", "campaign_history", "history_index", "history_records",
    "filter_index", "campaign_signature",
)

PHASES = {
    "queue": ("QUEUED", "OPENING"),
    "spec": ("SPEC_WRITING", "SPEC_REVIEW"),
    "implement": ("IMPLEMENTING",),
    "gate": ("GATING", "TRIAGING"),
    "review": ("REVIEWING",),
    "close": ("CLOSING",),
    "parked": ("PARKED",),
    "waiting": ("WAITING",),
}
PHASE_OF = {state: phase for phase, states in PHASES.items() for state in states}
FIX_REASONS = frozenset({"triage_fix", "review_fix"})
Visit = namedtuple("Visit", "phase state start end loop")
_TERMINAL = store.TERMINAL_STATES
_LINK_RANK = {"field": 0, "inferred": 1, "text": 2}
_COMPUTE = object()


def _parse_ts(value):
    try:
        return dt.datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _seconds(start, end) -> float:
    if not start or not end:
        return 0.0
    if (start.tzinfo is None) != (end.tzinfo is None):
        if start.tzinfo is None:
            start = start.replace(tzinfo=end.tzinfo)
        else:
            end = end.replace(tzinfo=start.tzinfo)
    return max(0.0, (end - start).total_seconds())


def _iso(value):
    return value.isoformat(timespec="seconds") if isinstance(value, dt.datetime) else value


def _json(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def _jsonl(path: Path, source: str, gaps: list[str]) -> list[tuple[int, dict]]:
    out = []
    try:
        with path.open("rb") as stream:
            line = 0
            while True:
                offset = stream.tell()
                raw = stream.readline()
                if not raw:
                    break
                line += 1
                if not raw.strip():
                    continue
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeError):
                    gaps.append(f"{source}: line {line} unparsable")
                    continue
                if isinstance(value, dict):
                    out.append((offset, value))
                else:
                    gaps.append(f"{source}: line {line} unparsable")
    except OSError:
        gaps.append(f"{source}: missing")
    return out


def _events(cfg, cid, gaps) -> list[dict]:
    return [row for _, row in _jsonl(
        store.campaign_dir(cfg, cid) / "ledger.jsonl", "ledger", gaps
    )]


def is_fix_loop(event) -> bool:
    return bool(event.get("type") == "transition"
                and event.get("to") == "IMPLEMENTING"
                and event.get("reason") in FIX_REASONS)


def count_loops(events) -> int:
    return sum(is_fix_loop(event) for event in events)


def loop_max(cfg, state) -> int | None:
    try:
        return resolve_profile(cfg, state.get("profile")).get("loop_max")
    except (TypeError, ValueError):
        return None


def time_in_state(state, events, start, end) -> dict[str, int]:
    """State seconds using the export's original transition walk."""
    out: dict[str, int] = {}
    cur, cursor = "QUEUED", start
    for event in events:
        if event.get("type") != "transition":
            continue
        stamp = _parse_ts(event.get("ts"))
        frm = event.get("from") or cur
        if cursor and stamp:
            out[frm] = out.get(frm, 0) + int(_seconds(cursor, stamp))
        cur = event.get("to") or cur
        cursor = stamp or cursor
    final = state.get("state")
    if final not in _TERMINAL and cursor and end:
        out[final] = out.get(final, 0) + int(_seconds(cursor, end))
    return out


def phase_visits(state, events, start, end) -> list[Visit]:
    visits, cur, cursor, loop = [], "QUEUED", start, 0
    for event in events:
        if event.get("type") != "transition":
            continue
        stamp = _parse_ts(event.get("ts"))
        frm = event.get("from") or cur
        if cursor and stamp and stamp >= cursor:
            visits.append(Visit(PHASE_OF.get(frm, "other"), frm, cursor, stamp, loop))
        if is_fix_loop(event):
            loop += 1
        cur, cursor = event.get("to") or cur, stamp or cursor
    final = state.get("state")
    if final not in _TERMINAL and cursor and end and end >= cursor:
        visits.append(Visit(PHASE_OF.get(final, "other"), final, cursor, end, loop))
    return visits


def campaign_signature(cfg, cid) -> str:
    root = store.campaign_dir(cfg, cid)
    values = []
    for name in ("ledger.jsonl", "state.json", "runs", "gates"):
        path = root / name
        try:
            stat = path.stat()
            values.append((name, stat.st_size, stat.st_mtime_ns))
        except OSError:
            values.append((name, None, None))
    return hashlib.sha1(repr(tuple(values)).encode("utf-8")).hexdigest()


def _conductor_attribution(cfg, now):
    """Campaign id → conductor USD, shared with the flat export."""
    from . import conductor_cost
    return conductor_cost.attribute(cfg, now)


def _interval(start, end):
    return (start, end) if start and end and end > start else None


def _run_intervals(events):
    starts, ends, out = {}, {}, []
    transitions = [_parse_ts(e.get("ts")) for e in events if e.get("type") == "transition"]
    for event in events:
        rid = event.get("run_id")
        if event.get("type") == "run.start" and rid:
            starts[rid] = event
        elif event.get("type") == "run.end" and rid:
            ends[rid] = event
    for rid, start_event in starts.items():
        start = _parse_ts(start_event.get("ts"))
        end_event = ends.get(rid)
        duration = (end_event or {}).get("duration_s")
        if start and isinstance(duration, (int, float)):
            end = start + dt.timedelta(seconds=max(0, duration))
        else:
            end = next((stamp for stamp in transitions if stamp and start and stamp > start), None)
        interval = _interval(start, end)
        if interval:
            out.append(interval)
    return out


def _gate_intervals(events):
    out = []
    for event in events:
        if event.get("type") != "gate":
            continue
        start = _parse_ts(event.get("started_at")) or _parse_ts(event.get("ts"))
        duration = event.get("duration_s")
        if start and isinstance(duration, (int, float)):
            interval = _interval(start, start + dt.timedelta(seconds=max(0, duration)))
            if interval:
                out.append(interval)
    return out


def _gate_wait_intervals(events, visits):
    out = []
    for event in events:
        if event.get("type") == "gate_waiting":
            end, waited = _parse_ts(event.get("ts")), event.get("waited_s")
            if end and isinstance(waited, (int, float)):
                out.append((end - dt.timedelta(seconds=max(0, waited)), end))
    gates = sorted(x[0] for x in _gate_intervals(events))
    for visit in visits:
        if visit.state != "GATING":
            continue
        first = next((stamp for stamp in gates if visit.start <= stamp <= visit.end), None)
        if first and first > visit.start:
            out.append((visit.start, first))
    return out


def _runner_intervals(cfg, gaps):
    path = store.agentic_dir(cfg) / "runner.jsonl"
    rows = _jsonl(path, "runner.jsonl", gaps)
    if not rows:
        return []
    out, down, last_stamp, saw_start = [], None, None, False
    for _, row in rows:
        stamp = _parse_ts(row.get("ts"))
        if not stamp:
            continue
        kind = row.get("kind")
        if kind in ("stop", "crash"):
            down = stamp
        elif kind == "start":
            if down is not None and stamp > down:
                out.append((down, stamp))
            elif saw_start and last_stamp is not None and stamp > last_stamp:
                out.append((last_stamp, stamp))
            saw_start = True
            down = None
        last_stamp = stamp
    return out


def _covers(intervals, start, end):
    return any(a < end and b > start for a, b in intervals)


def _phase_rows(cfg, events, visits, run_rows, gaps):
    intervals = {
        "run": _run_intervals(events),
        "gate": _gate_intervals(events),
        "gate_wait": _gate_wait_intervals(events, visits),
        "runner_down": _runner_intervals(cfg, gaps),
    }
    raw = {phase: {key: 0.0 for key in ("wall", "run", "gate", "gate_wait", "runner_down")}
           for phase in (*PHASES, "other")}
    count = {phase: 0 for phase in raw}
    for visit in visits:
        phase = visit.phase
        if phase not in raw:
            raw[phase] = {key: 0.0 for key in raw["other"]}
            count[phase] = 0
        count[phase] += 1
        raw[phase]["wall"] += _seconds(visit.start, visit.end)
        cuts = {visit.start, visit.end}
        for spans in intervals.values():
            for a, b in spans:
                if a < visit.end and b > visit.start:
                    cuts.update((max(a, visit.start), min(b, visit.end)))
        cuts = sorted(cuts)
        for a, b in zip(cuts, cuts[1:]):
            bucket = next((name for name in ("run", "gate", "gate_wait", "runner_down")
                           if _covers(intervals[name], a, b)), None)
            if bucket:
                raw[phase][bucket] += _seconds(a, b)
    phases = []
    for phase in PHASES:
        values = raw[phase]
        wall = round(values["wall"])
        buckets = {name: round(values[name]) for name in ("run", "gate", "gate_wait", "runner_down")}
        excess = max(0, sum(buckets.values()) - wall)
        for name in ("runner_down", "gate_wait", "gate", "run"):
            take = min(excess, buckets[name])
            buckets[name] -= take
            excess -= take
        other = wall - sum(buckets.values())
        cost = sum(float(run.get("cost_usd") or 0) for run in run_rows if run.get("phase") == phase)
        runs = sum(1 for run in run_rows if run.get("phase") == phase)
        phases.append({
            "phase": phase, "visits": count[phase], "wall_s": wall,
            "run_s": buckets["run"], "gate_s": buckets["gate"],
            "gate_wait_s": buckets["gate_wait"], "runner_down_s": buckets["runner_down"],
            "other_s": other, "cost_usd": round(cost, 4), "runs": runs,
        })
    return phases


def _visit_at(visits, stamp):
    if not stamp:
        return None
    return next((v for v in visits if v.start <= stamp < v.end), None)


def _run_rows(cfg, cid, events, visits, gaps):
    root = store.runs_dir(cfg, cid)
    dirs = {p.name: p for p in root.iterdir() if p.is_dir()} if root.is_dir() else {}
    starts = {e.get("run_id"): e for e in events if e.get("type") == "run.start" and e.get("run_id")}
    ends = {e.get("run_id"): e for e in events if e.get("type") == "run.end" and e.get("run_id")}
    rows = []
    for rid in sorted(set(dirs) | set(starts) | set(ends)):
        directory = dirs.get(rid)
        run_json = _json(directory / "run.json") if directory else None
        result_json = _json(directory / "result.json") if directory else None
        if run_json is None and directory:
            gaps.append(f"run {rid}: no run.json")
        if run_json is None and result_json is None:
            gaps.append(f"run {rid}: no run record")
        start, end = starts.get(rid) or {}, ends.get(rid) or {}
        record = run_json or result_json or {}
        source = record or end or start
        stamp = _parse_ts(record.get("started_at") or start.get("ts"))
        visit = _visit_at(visits, _parse_ts(start.get("ts")) or stamp)
        verdict = record.get("verdict", end.get("verdict"))
        timed_out = source.get("timed_out") is True
        interrupted = source.get("interrupted") is True or (bool(start) and not end and bool(record))
        exit_code = source.get("exit_code")
        status = ("running" if start and not end and not record
                  else "timeout" if timed_out else "interrupted" if interrupted
                  else "ok" if exit_code in (None, 0) and source.get("is_error") is not True
                  else "error")
        cost = record.get("cost_usd", end.get("cost_usd"))
        budget = record.get("max_budget_usd", start.get("max_budget_usd"))
        verdict_status = None
        summary = None
        if isinstance(verdict, dict):
            verdict_status = next((verdict.get(k) for k in ("status", "disposition", "verdict")
                                   if verdict.get(k) is not None), None)
            summary = verdict.get("summary")
        rows.append({
            "run_id": rid,
            "persona": record.get("persona", start.get("persona") or end.get("persona")),
            "phase": visit.phase if visit else None, "loop": visit.loop if visit else 0,
            "model": record.get("model", start.get("model")),
            "effort": record.get("effort", start.get("effort")),
            "started_at": _iso(stamp),
            "duration_s": record.get("duration_s", end.get("duration_s")),
            "cost_usd": cost, "budget_frac": (
                round(float(cost) / float(budget), 3) if cost is not None and budget else None),
            "exit_code": exit_code, "status": status, "verdict_status": verdict_status,
            "summary": str(summary) if summary is not None else None,
            "has_stream": bool(directory and (directory / "stream.jsonl").is_file()),
        })
    return rows


def _gate_rows(cfg, cid, events):
    root = store.gates_dir(cfg, cid)
    rows = []
    for event in events:
        if event.get("type") != "gate":
            continue
        seq = event.get("gate_seq")
        results = []
        for result in event.get("results") or []:
            name = str(result.get("name") or result.get("gate") or "")
            prefix = f"{int(seq):03d}-{name}" if isinstance(seq, int) else ""
            retries = len(list(root.glob(f"{prefix}.r*.json"))) if prefix and root.is_dir() else 0
            sidecar = _json(root / f"{prefix}.json") if prefix else None
            source = sidecar or result
            log = source.get("log")
            if log:
                try:
                    log = str(Path(log).relative_to(store.campaign_dir(cfg, cid)))
                except ValueError:
                    log = str(log)
            elif prefix:
                log = f"gates/{prefix}.log"
            results.append({
                "name": name, "required": source.get("required"), "ok": source.get("ok"),
                "exit_code": source.get("exit_code"), "retries": retries, "log": log,
            })
        rows.append({
            "gate_seq": seq, "started_at": event.get("started_at"),
            "duration_s": event.get("duration_s"), "ok": event.get("ok"), "results": results,
        })
    return rows


def _agent(value):
    text = str(value or "")
    return text[6:] if text.startswith("agent:") else text or None


def _one_line(value):
    return " ".join(str(value or "").split())[:200]


def _base_record(source, locator, kind, row, *, campaign=None):
    title = row.get("title") or row.get("question") or row.get("kind") or kind
    text_parts = [str(row[key]) for key in ("text", "detail", "rationale", "summary")
                  if row.get(key) is not None]
    text = "\n".join(dict.fromkeys(text_parts)) if text_parts else None
    return {
        "schema": SC.HISTORY_RECORD_SCHEMA, "id": f"{source}:{locator}", "ts": (
            row.get("ts") or row.get("created") or row.get("reviewed_at") or row.get("at")),
        "source": source, "kind": kind,
        "agent": _agent(row.get("persona") or row.get("by") or row.get("from")),
        "campaign": campaign if campaign is not None else row.get("campaign"),
        "spec_id": row.get("spec_id"), "project": row.get("project"),
        "severity": row.get("severity") if kind == "finding" else None,
        "title": _one_line(title), "text": str(text) if text is not None else None,
        "status": None,
        "links": {key: None for key in SC.RECORD_LINK_KEYS},
        "linked_by": "field",
    }


def _campaign_text_link(rec, campaigns):
    if rec.get("campaign"):
        rec["links"]["campaign"] = rec["campaign"]
        return "field"
    text = " ".join(str(rec.get(k) or "") for k in ("title", "text"))
    hits = []
    for cid in campaigns:
        if re.search(re.escape(cid) + r"(?![A-Za-z0-9_-])", text):
            hits.append(cid)
    if len(hits) == 1:
        rec["campaign"] = rec["links"]["campaign"] = hits[0]
        return "text"
    return None


def _ledger_records(cfg, cid, state, events, runs):
    out, findings = [], []
    run_ends = []
    for event in events:
        etype = event.get("type")
        if etype == "run.end":
            run_ends.append(event)
        if etype not in {"finding", "followup", "human_decision", "triage", "note", "close",
                         "spec_review"}:
            continue
        kind = {"close": "close_check"}.get(etype, etype)
        rec = _base_record("ledger", f"{cid}:{event.get('seq')}", kind, event, campaign=cid)
        if etype == "followup":
            rec["status"] = "filed"
            rec["links"]["followup_idea"] = event.get("idea_id")
            rec["links"]["key"] = event.get("key")
        elif etype == "human_decision":
            rec["links"]["escalation"] = event.get("escalation")
        elif etype in ("triage", "spec_review"):
            rec["links"]["run_id"] = event.get("run_id")
        elif etype == "close":
            rec["text"] = _one_line(" ".join(str(value) for value in (
                event.get("mode"), event.get("merged_sha"), event.get("pr_url")
            ) if value is not None))
        elif etype == "finding":
            key = event.get("key")
            run_id = event.get("run_id")
            inferred = False
            if not key:
                key = finding_key(state.get("spec_id"), event.get("title"), event.get("files"))
                inferred = True
            if event.get("gate"):
                gate = next((e for e in reversed(events[:events.index(event)])
                             if e.get("type") == "gate"), None)
                rec["links"]["gate_seq"] = (gate or {}).get("gate_seq")
                inferred = inferred or rec["links"]["gate_seq"] is not None
            elif not run_id:
                prior = [e for e in run_ends if e.get("persona") == event.get("persona")
                         and (e.get("ts") or "") <= (event.get("ts") or "")]
                run_id = prior[-1].get("run_id") if prior else None
                inferred = inferred or run_id is not None
            rec["links"]["run_id"] = run_id
            rec["links"]["key"] = key
            rec["_key"] = key
            rec["linked_by"] = "inferred" if inferred else "field"
            findings.append(rec)
        out.append(rec)
    triages = [e for e in events if e.get("type") == "triage"]
    followups = {e.get("key"): e for e in events if e.get("type") == "followup"}
    waived = {}
    for event in triages:
        for item in event.get("waive") or []:
            if item.get("key"):
                waived[item["key"]] = f"ledger:{cid}:{event.get('seq')}"
    # Pre-SPEC-0408 triagers identified a finding by `persona#index`.
    for run in runs:
        if "triager" not in str(run.get("persona")):
            continue
        value = _json(store.runs_dir(cfg, cid) / run["run_id"] / "run.json") or {}
        verdict = value.get("verdict")
        if not isinstance(verdict, dict):
            continue
        for item in verdict.get("waive") or []:
            match = re.fullmatch(r"([^#]+)#(\d+)", str(item.get("id") or ""))
            if not match:
                continue
            prior = [rec for rec in findings if rec.get("agent") == match.group(1)
                     and (rec.get("ts") or "") <= (run.get("started_at") or "")]
            if prior:
                latest = prior[-1]["links"]
                round_key = (latest.get("run_id"), latest.get("gate_seq"))
                prior = [rec for rec in prior if (
                    rec["links"].get("run_id"), rec["links"].get("gate_seq")
                ) == round_key]
            index = int(match.group(2))
            if index < len(prior):
                key = prior[index].get("_key")
                if key:
                    waived.setdefault(key, f"run:{cid}:{run['run_id']}")
    carry = {(item.get("key") if isinstance(item, dict) else item)
             for item in state.get("review_carry") or []
             if (isinstance(item, dict) and item.get("key")) or isinstance(item, str)}
    human_waives = []
    for event in events:
        if event.get("type") != "human_decision" or event.get("kind") != "waive":
            continue
        escalation_id = event.get("escalation")
        escalation = _json(store.agentic_dir(cfg) / "escalations" / f"{escalation_id}.json") or {}
        facts = " ".join(str(value) for value in escalation.get("facts") or [])
        human_waives.append((event, facts))
    for rec in findings:
        key = rec.pop("_key", None)
        if key in waived:
            rec["status"], rec["links"]["resolved_by"] = "waived", waived[key]
            if waived[key].startswith("run:"):
                rec["linked_by"] = "inferred"
        elif key in followups:
            rec["status"] = "filed"
            rec["links"]["followup_idea"] = followups[key].get("idea_id")
        elif key in carry:
            rec["status"] = "carried"
        elif (hit := next(((event, facts) for event, facts in human_waives
                           if (key and str(key) in facts)
                           or (rec.get("title") and str(rec["title"]) in facts)), None)):
            rec["status"] = "waived"
            rec["links"]["resolved_by"] = f"ledger:{cid}:{hit[0].get('seq')}"
            rec["linked_by"] = "inferred"
        else:
            later = [e for e in run_ends if e.get("persona") == rec.get("agent")
                     and (e.get("ts") or "") > (rec.get("ts") or "")]
            reraised = any(other is not rec and other.get("agent") == rec.get("agent")
                           and other.get("links", {}).get("key") == key
                           and (other.get("ts") or "") > (rec.get("ts") or "")
                           for other in findings)
            rec["status"] = ("fixed" if state.get("state") == "MERGED" and later and not reraised
                             else "open")
    # Old triager verdicts, only if SPEC-0408 did not write a ledger triage for that run.
    ledger_triage_runs = {e.get("run_id") for e in triages}
    for run in runs:
        if run.get("run_id") in ledger_triage_runs or "triager" not in str(run.get("persona")):
            continue
        path = store.runs_dir(cfg, cid) / run["run_id"] / "run.json"
        value = _json(path) or {}
        verdict = value.get("verdict")
        if isinstance(verdict, dict) and verdict.get("waive"):
            out.append(_base_record("run", f"{cid}:{run['run_id']}", "triage",
                                    {**value, "ts": run.get("started_at")}, campaign=cid))
    return out


def _global_records(cfg, campaigns, gaps):
    root = store.agentic_dir(cfg)
    out = []
    decisions = _jsonl(root / "conductor" / "decisions.jsonl", "decisions.jsonl", gaps)
    for offset, row in decisions:
        out.append(_base_record("decisions", row.get("event_id") or offset, "decision", row))
    escalations = root / "escalations"
    if escalations.is_dir():
        for path in sorted(escalations.glob("ESC-*.json")):
            row = _json(path)
            if not row:
                gaps.append(f"escalation {path.stem}: unparsable")
                continue
            rec = _base_record("escalation", row.get("id") or path.stem, "escalation", row)
            rec["status"] = row.get("status")
            rec["links"]["escalation"] = row.get("id")
            out.append(rec)
            if isinstance(row.get("answer"), dict):
                answer = {**row["answer"], "ts": row.get("answered_at"), "by": row.get("answered_by"),
                          "campaign": row.get("campaign"), "spec_id": row.get("spec_id"),
                          "project": row.get("project"), "title": row.get("answer", {}).get("label")}
                arec = _base_record("escalation", f"{row.get('id')}/answer", "answer", answer)
                arec["status"], arec["links"]["escalation"] = "answered", row.get("id")
                out.append(arec)
    mailbox = root / "mailbox"
    messages = []
    if mailbox.is_dir():
        for path in sorted(mailbox.glob("*.jsonl")):
            for offset, row in _jsonl(path, f"mailbox/{path.name}", gaps):
                messages.append((path.stem, offset, row))
        replies = {str(row.get("re")) for _, _, row in messages if row.get("re") is not None}
        by_id = {str(row.get("id")): row for _, _, row in messages}
        by_id.update({str(row.get("seq")): row for _, _, row in messages if row.get("seq") is not None})
        for inbox, offset, row in messages:
            rec = _base_record("mailbox", f"{inbox}:{offset}", "mail", row)
            if row.get("kind") == "question":
                rec["status"] = "answered" if str(row.get("id")) in replies else "open"
            current, seen_ids = row, set()
            while current.get("re") is not None and str(current["re"]) in by_id:
                parent = str(current["re"])
                if parent in seen_ids:
                    break
                seen_ids.add(parent)
                current = by_id[parent]
            rec["links"]["thread"] = current.get("id") or row.get("id")
            out.append(rec)
    seen, review_at = set(), {}
    reviews = root / "reviews"
    if reviews.is_dir():
        for path in sorted(reviews.glob("*/*.json")):
            row = _json(path)
            if not row:
                continue
            locator = f"{path.parent.name}/{path.stem}"
            seen.add(locator)
            review_at[(row.get("spec_id"), row.get("reviewed_at"))] = locator
            out.append(_base_record("spec_review", locator, "spec_review", row))
    for _, row in _jsonl(root / "spec-reviews.jsonl", "spec-reviews.jsonl", gaps):
        if row.get("kind") != "end":
            continue
        locator = review_at.get((row.get("spec_id"), row.get("ts"))) or (
            f"{row.get('spec_id')}/{row.get('spec_sha256') or row.get('run_id')}")
        if locator not in seen:
            out.append(_base_record("spec_review", locator, "spec_review", row))
            seen.add(locator)
    flaky = _json(root / "flaky-tests.json") or {}
    for project, tests in (flaky.get("projects") or {}).items():
        for test_id, row in (tests or {}).items():
            for cid in row.get("campaigns") or []:
                out.append(_base_record("flaky", test_id, "flaky", {
                    **row, "campaign": cid, "project": project, "title": test_id,
                    "ts": row.get("last_seen"),
                }))
    sentinel = root / "sentinel"
    if sentinel.is_dir():
        for path in sorted(sentinel.glob("*.last.json")):
            row = _json(path)
            if row:
                out.append(_base_record("sentinel", f"{path.name[:-10]}:{row.get('at')}",
                                        "sentinel", {**row, "project": path.name[:-10],
                                                     "title": "sentinel"}))
    for rec in out:
        kind = _campaign_text_link(rec, campaigns)
        if kind:
            rec["linked_by"] = kind
    return out


def _enrich_records(records, headers):
    for rec in records:
        cid = rec.get("campaign")
        if cid in headers:
            rec["spec_id"] = headers[cid].get("spec_id")
            rec["project"] = headers[cid].get("project")
            rec["links"]["campaign"] = cid
        kinds = [rec.get("linked_by")]
        if rec["links"].get("campaign") and not rec.get("campaign"):
            kinds.append("text")
        rec["linked_by"] = max((k for k in kinds if k), key=lambda k: _LINK_RANK[k],
                               default="field")
    return records


def _records_all(cfg, headers=None, gaps=None):
    gaps = gaps if gaps is not None else []
    campaigns_root = store.campaigns_dir(cfg)
    cids = sorted(p.name for p in campaigns_root.iterdir() if p.is_dir()) \
        if campaigns_root.is_dir() else []
    headers = headers or {}
    out = []
    for cid in cids:
        state = _json(store.campaign_dir(cfg, cid) / "state.json") or {}
        events = _events(cfg, cid, gaps)
        start = _parse_ts(state.get("created_at") or (events[0].get("ts") if events else None))
        now = dt.datetime.now().astimezone()
        visits = phase_visits(state, events, start, now)
        runs = _run_rows(cfg, cid, events, visits, gaps)
        out.extend(_ledger_records(cfg, cid, state, events, runs))
        for run in runs:
            directory = store.runs_dir(cfg, cid) / run["run_id"]
            value = _json(directory / "run.json") or {}
            if isinstance(value.get("verdict"), dict):
                row = {**value, "ts": run.get("started_at"), "title": "verdict",
                       "text": value["verdict"].get("summary"), "persona": run.get("persona")}
                out.append(_base_record("run", f"{cid}:{run['run_id']}", "verdict", row,
                                        campaign=cid))
        if cid not in headers:
            headers[cid] = {"spec_id": state.get("spec_id"), "project": state.get("project")}
    out.extend(_global_records(cfg, cids, gaps))
    return _enrich_records(out, headers)


def _header(cfg, cid, state, events, phases, runs, now, conductor_shares=_COMPUTE):
    first_ts = next((_parse_ts(e.get("ts")) for e in events if _parse_ts(e.get("ts"))), None)
    created = _parse_ts(state.get("created_at")) or first_ts
    close = next((e for e in reversed(events) if e.get("type") == "close"), None)
    closed_at = (close or {}).get("ts") or (state.get("close") or {}).get("ts")
    if state.get("state") not in _TERMINAL:
        closed_at = None
    end = _parse_ts(closed_at) or (now if state.get("state") not in _TERMINAL else None)
    wall = round(_seconds(created, end)) if created and end else 0
    idle = sum(p["wall_s"] for p in phases if p["phase"] in ("queue", "parked", "waiting"))
    cost = state.get("cost") or {}
    total = cost.get("total_usd")
    if total is None:
        total = sum(float(e.get("charged_usd") or 0) for e in events if e.get("type") == "run.end")
    outcome = {"MERGED": "merged", "HANDED_OFF": "handed_off",
               "ABANDONED": "abandoned"}.get(state.get("state"))
    conductor = None
    if conductor_shares is _COMPUTE:
        try:
            conductor_shares = _conductor_attribution(cfg, now)
        except Exception:
            conductor_shares = None
    if conductor_shares is not None:
        conductor = conductor_shares.get(cid, 0.0)
    followup = next((e.get("idea_id") for e in reversed(events)
                     if e.get("type") == "followup"), None)
    sizing, close_state = state.get("sizing") or {}, state.get("close") or {}
    start_ids = {e.get("run_id") for e in events
                 if e.get("type") == "run.start" and e.get("run_id")}
    end_ids = {e.get("run_id") for e in events
               if e.get("type") == "run.end" and e.get("run_id")}
    return {
        "campaign": cid, "kind": state.get("kind"), "spec_id": state.get("spec_id"),
        "idea_id": (state.get("idea") or {}).get("id"), "title": (
            state.get("title") or (state.get("spec") or {}).get("title") or state.get("spec_id")
            or (state.get("idea") or {}).get("heading")),
        "project": state.get("project"), "profile": state.get("profile"),
        "size": sizing.get("size"), "tone": sizing.get("tone"), "state": state.get("state"),
        "outcome": outcome, "priority": state.get("priority"), "created_at": _iso(created),
        "closed_at": closed_at, "wall_s": wall, "active_s": max(0, wall - idle),
        "loops": count_loops(events), "loop_max": loop_max(cfg, state),
        "runs": len(end_ids) + len(start_ids - end_ids),
        "cost_usd": round(float(total or 0), 4),
        "cap_usd": cost.get("cap_usd"), "conductor_usd": conductor,
        "merged_sha": close_state.get("merged_sha"),
        "pr_url": close_state.get("pr_url") or close_state.get("pr"),
        "followup_idea": followup, "park": state.get("park"),
    }


def _dominant(phases):
    def winner(key, allowed=None):
        rows = [p for p in phases if allowed is None or p["phase"] in allowed]
        value = max((p[key] for p in rows), default=0)
        return next((p["phase"] for p in rows if p[key] == value), None) if value > 0 else None
    wall = winner("wall_s")
    working = winner("wall_s", set(PHASES) - {"queue", "parked", "waiting"}) \
        if wall in {"queue", "parked", "waiting"} else None
    return {"wall": wall, "cost": winner("cost_usd"), "working": working}


def _timeline(events, visits, records):
    rows, seen_runs = [], set()
    ledger_refs = {int(rec["id"].rsplit(":", 1)[-1]): rec["id"] for rec in records
                   if rec.get("source") == "ledger"
                   and rec.get("id", "").rsplit(":", 1)[-1].isdigit()}
    for event in events:
        kind = event.get("type")
        rid = event.get("run_id")
        if kind in ("run.start", "run.end"):
            if rid in seen_runs or kind == "run.end" and any(
                    e.get("type") == "run.start" and e.get("run_id") == rid for e in events):
                continue
            seen_runs.add(rid)
            kind = "run"
        stamp = _parse_ts(event.get("ts"))
        visit = _visit_at(visits, stamp)
        summary = event.get("title") or event.get("text") or event.get("reason") or kind
        rows.append({
            "seq": event.get("seq"), "ts": event.get("ts"), "kind": kind,
            "phase": visit.phase if visit else None, "loop": visit.loop if visit else 0,
            "by": _agent(event.get("persona") or event.get("by")), "summary": _one_line(summary),
            "ref": rid or ledger_refs.get(
                event.get("seq") if isinstance(event.get("seq"), int) else None),
        })
    ledger_ids = {f"ledger:{e.get('campaign')}:{e.get('seq')}" for e in events}
    for rec in records:
        if rec["source"] == "ledger" and rec["id"] in ledger_ids:
            continue
        if rec["source"] not in {"decisions", "mailbox", "escalation", "spec_review"}:
            continue
        stamp = _parse_ts(rec.get("ts"))
        visit = _visit_at(visits, stamp)
        rows.append({
            "seq": None, "ts": rec.get("ts"), "kind": rec.get("kind"),
            "phase": visit.phase if visit else None, "loop": visit.loop if visit else 0,
            "by": rec.get("agent"), "summary": _one_line(rec.get("title") or rec.get("text")),
            "ref": rec.get("id"),
        })
    rows.sort(key=lambda row: (row.get("ts") or "", row.get("seq") if row.get("seq") is not None
                               else 10 ** 12))
    return rows


def _campaign_history(cfg, cid, now, conductor_shares):
    root = store.campaign_dir(cfg, cid)
    if not root.is_dir():
        raise ValueError(f"campaign {cid} not found")
    gaps = []
    state = _json(root / "state.json")
    if state is None:
        state = {"id": cid, "state": None}
        gaps.append("state.json: missing or unparsable")
    events = _events(cfg, cid, gaps)
    now = now or dt.datetime.now().astimezone()
    created = _parse_ts(state.get("created_at")) or next(
        (_parse_ts(e.get("ts")) for e in events if _parse_ts(e.get("ts"))), now)
    closed = next((_parse_ts(e.get("ts")) for e in reversed(events)
                   if e.get("type") == "close"), None)
    end = closed if state.get("state") in _TERMINAL else now
    visits = phase_visits(state, events, created, end)
    if state.get("state") not in PHASE_OF and state.get("state") not in _TERMINAL:
        gaps.append(f"state {state.get('state')}: no phase")
    for visit in visits:
        if visit.phase == "other":
            gaps.append(f"state {visit.state}: no phase")
    runs = _run_rows(cfg, cid, events, visits, gaps)
    phases = _phase_rows(cfg, events, visits, runs, gaps)
    header = _header(cfg, cid, state, events, phases, runs, now, conductor_shares)
    headers = {cid: header}
    records = _ledger_records(cfg, cid, state, events, runs)
    for run in runs:
        value = _json(store.runs_dir(cfg, cid) / run["run_id"] / "run.json") or {}
        if isinstance(value.get("verdict"), dict):
            row = {**value, "ts": run.get("started_at"), "title": "verdict",
                   "text": value["verdict"].get("summary"), "persona": run.get("persona")}
            records.append(_base_record("run", f"{cid}:{run['run_id']}", "verdict", row,
                                        campaign=cid))
    campaigns_root = store.campaigns_dir(cfg)
    cids = sorted(p.name for p in campaigns_root.iterdir() if p.is_dir()) \
        if campaigns_root.is_dir() else [cid]
    records.extend(_global_records(cfg, cids, gaps))
    records = [r for r in _enrich_records(records, headers) if r.get("campaign") == cid]
    return {
        "schema": SC.HISTORY_SCHEMA, "campaign": cid, "generated_at": store.now_iso(now),
        "signature": campaign_signature(cfg, cid), "header": header, "phases": phases,
        "dominant": _dominant(phases), "timeline": _timeline(events, visits, records),
        "runs": runs, "gates": _gate_rows(cfg, cid, events), "records": records,
        "gaps": sorted(set(gaps)),
    }


def campaign_history(cfg, cid, *, now=None) -> dict:
    return _campaign_history(cfg, cid, now, _COMPUTE)


def history_index(cfg, *, now=None, progress=None) -> list[dict]:
    now = now or dt.datetime.now().astimezone()
    try:
        conductor_shares = _conductor_attribution(cfg, now)
    except Exception:
        conductor_shares = None
    root = store.campaigns_dir(cfg)
    cids = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    rows = []
    for done, cid in enumerate(cids, 1):
        doc = _campaign_history(cfg, cid, now, conductor_shares)
        counts = {}
        for rec in doc["records"]:
            if rec["kind"] == "finding" and rec.get("severity"):
                counts[rec["severity"]] = counts.get(rec["severity"], 0) + 1
        rows.append({
            "schema": SC.HISTORY_INDEX_SCHEMA, **doc["header"], "dominant": doc["dominant"],
            "phase_wall_s": {p["phase"]: p["wall_s"] for p in doc["phases"]},
            "phase_cost_usd": {p["phase"]: p["cost_usd"] for p in doc["phases"]},
            "findings_by_severity": counts, "records": len(doc["records"]),
            "signature": doc["signature"],
        })
        if progress:
            progress(done, len(cids))
    return sorted(rows, key=lambda row: (row.get("closed_at") or row.get("created_at") or "",
                                         row.get("campaign") or ""), reverse=True)


def _set(value):
    if value is None:
        return None
    return set(value) if isinstance(value, (list, tuple, set, frozenset)) else {value}


def _date_bound(value, *, until=False):
    if value is None or isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        stamp = dt.datetime.combine(value, dt.time.max if until else dt.time.min)
        return stamp
    text = str(value)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            day = dt.date.fromisoformat(text)
            return dt.datetime.combine(day, dt.time.max if until else dt.time.min)
        return dt.datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"invalid date {value!r}")


def _in_dates(value, since, until):
    stamp = _parse_ts(value)
    if not stamp:
        return since is None and until is None
    for bound, lower in ((since, True), (until, False)):
        if bound is None:
            continue
        if (stamp.tzinfo is None) != (bound.tzinfo is None):
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=bound.tzinfo)
            else:
                bound = bound.replace(tzinfo=stamp.tzinfo)
        if lower and stamp < bound or not lower and stamp > bound:
            return False
    return True


def filter_index(rows, *, project=None, state=None, spec=None, since=None, until=None,
                 min_cost=None) -> list[dict]:
    since, until = _date_bound(since), _date_bound(until, until=True)
    out = []
    for row in rows:
        outcome = row.get("outcome") or "live"
        if project and row.get("project") != project:
            continue
        if state and state != "all" and outcome != state:
            continue
        if spec and row.get("spec_id") != spec:
            continue
        if min_cost is not None and float(row.get("cost_usd") or 0) < float(min_cost):
            continue
        if not _in_dates(row.get("closed_at") or row.get("created_at"), since, until):
            continue
        out.append(row)
    return out


def history_records(cfg, *, campaign=None, agent=None, kind=None, project=None, severity=None,
                    status=None, since=None, until=None, query=None, limit=None) -> list[dict]:
    records = _records_all(cfg)
    values = {name: _set(value) for name, value in (
        ("campaign", campaign), ("agent", agent), ("kind", kind), ("project", project),
        ("severity", severity), ("status", status),
    )}
    since, until = _date_bound(since), _date_bound(until, until=True)
    needle = str(query or "").casefold()
    out = []
    for rec in records:
        if any(wanted is not None and rec.get(name) not in wanted for name, wanted in values.items()):
            continue
        if not _in_dates(rec.get("ts"), since, until):
            continue
        if needle and needle not in f"{rec.get('title') or ''} {rec.get('text') or ''}".casefold():
            continue
        out.append(rec)
    out.sort(key=lambda rec: (rec.get("ts") or "", rec.get("id") or ""), reverse=True)
    return out[:limit] if limit is not None else out
