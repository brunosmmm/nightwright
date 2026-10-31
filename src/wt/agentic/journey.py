"""SPEC-0326: the plain-language view of a campaign for `wt queue watch` — its journey `track`
(the compressed sequence of state visits, so fix loops are visible), the `why` sentence for the
current state, what it is `waiting_for` when it is not running, and one short sentence per
ledger event. Every function here is pure: it reads a state dict, a ledger list and (for
`waiting_for`) a gate-slot occupancy snapshot, and never touches the disk."""
from __future__ import annotations

import datetime as dt
import re

SHORT = {"QUEUED": "queued", "OPENING": "open", "SPEC_WRITING": "spec",
         "SPEC_REVIEW": "spec review", "IMPLEMENTING": "impl", "GATING": "gates",
         "TRIAGING": "triage", "REVIEWING": "review", "CLOSING": "close", "PARKED": "parked",
         "WAITING": "waiting", "MERGED": "merged", "HANDED_OFF": "handed off",
         "ABANDONED": "abandoned"}
TRACK_CAP = 8
_SKIP_VISITS = ("QUEUED", "OPENING")      # admission and the worktree open are not the journey
_REVIEW_NOTE = re.compile(r"review score (\d+(?:\.\d+)?)/(\d+(?:\.\d+)?) (\w+)")


def short(state) -> str:
    return SHORT.get(str(state or ""), str(state or "?").lower())


def short_id(cid) -> str:
    """`IDEA-511-20261031-eda6` -> `IDEA-511`; `SPEC-0302-…` -> `SPEC-0302`."""
    parts = str(cid or "?").split("-")
    return "-".join(parts[:2]) if len(parts) > 2 else str(cid or "?")


def _clip(text, n=90) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _sev_counts(events) -> dict:
    c = {"must_fix": 0, "major": 0}
    for e in events:
        if e.get("type") == "finding" and e.get("severity") in c:
            c[e["severity"]] += 1
    return c


def _review_note(events):
    """`(score, pass_score, outcome)` from the queue's `review score S/P outcome` note."""
    for e in reversed(events):
        if e.get("type") == "note":
            m = _REVIEW_NOTE.search(str(e.get("text") or ""))
            if m:
                s, p = float(m.group(1)), float(m.group(2))
                return (int(s) if s.is_integer() else s, int(p) if p.is_integer() else p,
                        m.group(3))
    return None


def _gate_outcome(events) -> tuple:
    """`(ok, outcome)` of the last `gate` event among `events`: `ok`, `timeout`, `N failed`."""
    gate = next((e for e in reversed(events) if e.get("type") == "gate"), None)
    if gate is None:
        return None, None
    if gate.get("ok"):
        return True, "ok"
    bad = [r for r in gate.get("results") or [] if isinstance(r, dict) and not r.get("ok")]
    if any(r.get("timed_out") for r in bad):
        return False, "timeout"
    tests = sum(int((r.get("summary") or {}).get("failed") or 0) for r in bad)
    if tests:
        return False, f"{tests} failed"
    names = [str(r.get("name")) for r in bad]
    return False, (f"{names[0]} failed" if len(names) == 1 else f"{len(names)} gates failed")


_FORWARD = {"implemented", "spec_written", "opened", "closed", "slot_free", "spec_amended",
            "resumed", "config_recovered", "resumed_waiting", "admitted", "spec_accepted",
            "gates_green", "review_pass", "pr_merged"}
_TRIAGE = {"retry_gate": "retry", "triage_fix": "fix", "triage_escalated": "escalate",
           "loop_exhausted": "loop limit", "diverging": "diverging"}


def _outcome(state: str, reason: str, events: list, to: str) -> tuple:
    """`(ok, outcome)` for a visit to `state` that ended by a transition with `reason`."""
    if state == "GATING" or reason in ("gates_failed", "regate_failed"):
        ok, out = _gate_outcome(events)
        if ok is not None:
            return ok, out
        if reason == "gates_green":
            return True, "ok"
    if state == "REVIEWING":
        note = _review_note(events)
        c = _sev_counts(events)
        if reason == "review_pass":
            return True, (str(note[0]) if note else "pass")
        if reason == "review_fix":
            bits = ([str(note[0])] if note else []) + [
                f"{n} {k.replace('_', '-')}" for k, n in c.items() if n]
            return False, " · ".join(bits) or "fix"
    if state == "TRIAGING" and reason in _TRIAGE:
        return None, _TRIAGE[reason]
    if state == "SPEC_REVIEW":
        if reason == "spec_accepted":
            return True, "accepted"
        if reason == "spec_revise":
            return False, "revise"
    if to == "PARKED":
        return False, "parked"
    if reason in _FORWARD:
        return True, "ok"
    return None, str(reason or "").replace("_", " ") or None


def track(ledger: list, current_state=None, cap: int = TRACK_CAP) -> tuple[list, int]:
    """`(visits, elided)`: the campaign's state visits from its `transition` events, oldest
    first, consecutive duplicates merged, QUEUED/OPENING left out, capped to the last `cap`
    (`elided` = how many earlier visits were cut). Each visit is `{state, reason, at, ok,
    outcome}`: `reason` why it was entered, `ok`/`outcome` how it ended (`None` while it is the
    current visit). A ledger without transitions yields one visit for `current_state`."""
    visits: list = []
    cur = None
    for ev in ledger:
        if ev.get("type") != "transition":
            if cur is not None:
                cur["_events"].append(ev)
            continue
        to, reason = ev.get("to"), ev.get("reason")
        if cur is not None:
            cur["ok"], cur["outcome"] = _outcome(cur["state"], reason, cur["_events"], to)
            cur = None
        if to in _SKIP_VISITS:
            continue
        if visits and visits[-1]["state"] == to and cur is None:
            cur = visits[-1]                      # back where it was: one visit, not two
            cur.update(ok=None, outcome=None)
            continue
        cur = {"state": to, "reason": reason, "at": ev.get("ts"), "ok": None, "outcome": None,
               "_events": []}
        visits.append(cur)
    if not visits and current_state and current_state not in _SKIP_VISITS[:1]:
        visits.append({"state": current_state, "reason": None, "at": None, "ok": None,
                       "outcome": None, "_events": []})
    for v in visits:
        v.pop("_events", None)
    elided = max(0, len(visits) - cap)
    return visits[elided:], elided


def _last_visit(ledger: list, state: str) -> list:
    """The events of the last visit to `state` (between its entering transition and the next)."""
    start = end = None
    for i, ev in enumerate(ledger):
        if ev.get("type") != "transition":
            continue
        if ev.get("to") == state:
            start, end = i, None
        elif start is not None and end is None and ev.get("from") == state:
            end = i
    if start is None:
        return []
    return ledger[start + 1:end if end is not None else len(ledger)]


def waiting_for(state: dict, ledger: list, occupancy=None, *, busy=None, total=None,
                now=None) -> dict | None:
    """What the campaign waits on when it is not running, else None:
    `{"kind": "gate_slot", slots, held: [cid], since, waited_s}` (from the live gate-slot
    `occupancy`, or the last `gate_waiting` ledger entry when no snapshot is given),
    `{"kind": "close_train", behind}`, `{"kind": "campaign_slot", resume_to, busy, total}` or
    `{"kind": "dependency", on: [...]}`."""
    cid, st = state.get("id"), state.get("state")
    if st == "WAITING":
        return {"kind": "campaign_slot", "resume_to": (state.get("waiting") or {}).get("resume_to"),
                "busy": busy, "total": total,
                "since": (state.get("waiting") or {}).get("since")}
    if st == "QUEUED":
        on = list((state.get("backlog") or {}).get("blocked_by") or [])
        return {"kind": "dependency", "on": on} if on else None
    if st == "CLOSING" and (state.get("closing") or {}).get("queued_behind"):
        return {"kind": "close_train", "behind": state["closing"]["queued_behind"]}
    if st not in ("GATING", "CLOSING"):
        return None
    if occupancy is not None:
        w = next((x for x in occupancy.get("waiting") or [] if x.get("campaign") == cid), None)
        if w is None or any(h.get("campaign") == cid for h in occupancy.get("held") or []):
            return None
        return {"kind": "gate_slot", "slots": occupancy.get("slots"),
                "held": [h.get("campaign") for h in occupancy.get("held") or []],
                "since": w.get("since"), "waited_s": _age(w.get("since"), now)}
    idx_t = max((i for i, e in enumerate(ledger) if e.get("type") == "transition"), default=-1)
    idx_w = max((i for i, e in enumerate(ledger) if e.get("type") == "gate_waiting"), default=-1)
    if idx_w <= idx_t or any(e.get("type") in ("gate", "run.start") for e in ledger[idx_w + 1:]):
        return None
    ev = ledger[idx_w]
    held = [h.get("campaign") if isinstance(h, dict) else h for h in ev.get("held") or []]
    return {"kind": "gate_slot", "slots": ev.get("slots"), "held": held, "since": None,
            "waited_s": ev.get("waited_s")}


def waiting_text(w: dict) -> str:
    kind = (w or {}).get("kind")
    if kind == "gate_slot":
        held = w.get("held") or []
        who = f": {', '.join(short_id(c) for c in held)}" if held else ""
        return f"waiting for a gate slot ({len(held)}/{w.get('slots')} held{who})"
    if kind == "close_train":
        return f"waiting to close behind {short_id(w.get('behind'))}"
    if kind == "campaign_slot":
        bt = (f" ({w['busy']}/{w['total']} busy)" if w.get("busy") is not None
              and w.get("total") is not None else "")
        return f"waiting for a campaign slot{bt}, then {short(w.get('resume_to'))}"
    if kind == "dependency":
        return f"waiting on {', '.join(w.get('on') or []) or '?'}"
    return "waiting"


def _money(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "?"
    return f"${f:g}" if f.is_integer() else f"${f:.2f}"


def why(state: dict, ledger: list, waiting=None, *, loops=None) -> str:
    """One plain-language sentence for where the campaign is and why, from the latest
    transition, its context and `waiting` (see `waiting_for`): a close-train, campaign-slot or
    dependency wait is the why; a gate-slot wait is not (the dashboard's `now` line shows it,
    the why keeps saying what the gates are for). `loops` is the `N/M` string."""
    st = state.get("state")
    if waiting and waiting.get("kind") != "gate_slot":
        return waiting_text(waiting)      # a gate-slot wait is the `now` line; the why stays
    if st == "PARKED":
        park = state.get("park") or {}
        reason = park.get("reason") or "?"
        if reason == "cost_cap":
            return f"parked: cost cap {_money((state.get('cost') or {}).get('cap_usd'))} reached"
        if reason == "loop_exhausted":
            return f"parked: loop limit reached ({loops})" if loops else "parked: loop limit reached"
        detail = _clip(park.get("detail"), 70)
        return f"parked: {reason.replace('_', ' ')}" + (f" — {detail}" if detail else "")
    last = next((e for e in reversed(ledger) if e.get("type") == "transition"
                 and e.get("to") == st), None)
    reason = (last or {}).get("reason")
    if reason == "review_fix":
        evs = _last_visit(ledger, "REVIEWING")
        note, c = _review_note(evs), _sev_counts(evs)
        counts = ", ".join(f"{n} {k.replace('_', '-')}" for k, n in c.items() if n)
        head = (f"review scored {note[0]} < {note[1]}" if note and note[0] < note[1]
                else "review asked for fixes")
        return f"back to implementing: {head}" + (f" ({counts})" if counts else "")
    if reason == "triage_fix":
        evs = _last_visit(ledger, "TRIAGING")
        f = next((e for e in evs if e.get("type") == "finding"
                  and e.get("severity") == "must_fix"), None)
        return "back to implementing: triage says the gate failure is real" + (
            f" — {_clip(f.get('title'), 60)}" if f else "")
    if reason == "retry_gate":
        return "re-running gates: triage says the failures are flaky"
    if reason in ("gates_failed", "regate_failed"):
        evs = _last_visit(ledger, "GATING") if reason == "gates_failed" else ledger
        ok, out = _gate_outcome(evs)
        where = "gates" if reason == "gates_failed" else "the re-gate before merge"
        return f"triaging: {where} failed" + (f" ({out})" if out and out != "ok" else "")
    if reason == "spec_dod":
        return "triaging: the spec's definition of done is not met at close"
    if reason == "spec_revise":
        sr = next((e for e in reversed(ledger) if e.get("type") == "spec_review"), {})
        c = sr.get("counts") or {}
        counts = ", ".join(f"{c[k]} {k.replace('_', '-')}" for k in ("must_fix", "major")
                           if c.get(k))
        return "revising the spec: review asked for changes" + (f" ({counts})" if counts else "")
    if reason in ("resumed", "config_recovered"):
        res = next((e for e in reversed(ledger) if e.get("type") == "resume"), {})
        who = f" by {last.get('by')}" if last and last.get("by") not in (None, "tick") else ""
        note = _clip(res.get("note"), 60)
        return f"resumed{who} at {short(st)}" + (f": {note}" if note else "")
    if reason == "slot_free":
        return f"continuing at {short(st)}: a slot freed up"
    if reason == "implemented" and st == "GATING":
        return "gating the fix" if any(e.get("type") == "transition" and e.get("to") == "GATING"
                                       for e in ledger[:ledger.index(last)]) \
            else "gating the implementation"
    if reason == "gates_green":
        return "reviewing: gates are green" if st == "REVIEWING" else "closing: gates are green"
    if reason == "review_pass":
        note = _review_note(_last_visit(ledger, "REVIEWING"))
        return "closing: review passed" + (f" ({note[0]})" if note else "") + \
            " — rebase, re-gate, merge"
    if reason == "spec_written":
        return "reviewing the spec"
    if reason == "spec_accepted":
        return "implementing the accepted spec"
    if reason == "spec_amended":
        return "implementing after amending the spec"
    return {"OPENING": "opening the worktree", "SPEC_WRITING": "writing the spec",
            "SPEC_REVIEW": "reviewing the spec", "IMPLEMENTING": "implementing the spec",
            "GATING": "running the gates", "TRIAGING": "triaging the gate failure",
            "REVIEWING": "reviewers are reading the change",
            "CLOSING": "closing: rebase, re-gate, merge"}.get(
        st, f"{short(st)}" + (f": {reason.replace('_', ' ')}" if reason else ""))


def _age(ts, now=None):
    try:
        t = dt.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.astimezone()
    now = now or dt.datetime.now().astimezone()
    return max(0, int((now - t).total_seconds()))


def hhmm(ts) -> str:
    try:
        t = dt.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return "--:--"
    if t.tzinfo is None:
        t = t.astimezone()
    return t.astimezone().strftime("%H:%M")


def _dur(s) -> str:
    try:
        s = int(float(s))
    except (TypeError, ValueError):
        return "?"
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s" if s < 3600 else \
        f"{s // 3600}h{s % 3600 // 60:02d}m"


_COMMIT_PREFIX = re.compile(r"^wt\([^)]*\):\s*")


def _event_text(ev: dict) -> str:
    t = ev.get("type")
    if t == "transition":
        return (f"{short(ev.get('from'))} → {short(ev.get('to'))}: "
                f"{str(ev.get('reason') or '').replace('_', ' ')}")
    if t == "run.start":
        model = "/".join(str(x) for x in (ev.get("model"), ev.get("effort")) if x)
        return f"started {ev.get('persona')}" + (f" ({model})" if model else "")
    if t == "run.end":
        tail = (" · timed out" if ev.get("timed_out") else " · interrupted" if ev.get("interrupted")
                else " · error" if ev.get("is_error") else "")
        cost = ev.get("charged_usd", ev.get("cost_usd"))
        return (f"{ev.get('persona')} finished in {_dur(ev.get('duration_s'))}"
                + (f" · ${float(cost):.2f}" if isinstance(cost, (int, float)) else "") + tail)
    if t == "cost":
        est = " est." if ev.get("estimated") else ""
        return (f"{ev.get('persona')} cost ${float(ev.get('delta_usd') or 0):.2f}{est}, "
                f"total ${float(ev.get('total_usd') or 0):.2f}")
    if t == "commit":
        msg = _COMMIT_PREFIX.sub("", str(ev.get("message") or "").splitlines()[0]
                                 if ev.get("message") else "")
        return f"commit {str(ev.get('sha') or '')[:7]} {_clip(msg, 60)}".rstrip()
    if t == "gate":
        if ev.get("ok"):
            return f"gates #{ev.get('gate_seq')} green in {_dur(ev.get('duration_s'))}"
        bad = [r for r in ev.get("results") or [] if isinstance(r, dict) and not r.get("ok")]
        names = ", ".join(str(r.get("name")) + (" (timeout)" if r.get("timed_out") else
                                                f" ({r['summary']['failed']} failed)"
                                                if (r.get("summary") or {}).get("failed") else "")
                          for r in bad)
        return f"gates #{ev.get('gate_seq')} failed: {names or '?'}"
    if t == "gate_waiting":
        held = [h.get("campaign") if isinstance(h, dict) else h for h in ev.get("held") or []]
        return (f"waiting for a gate slot ({len(held)}/{ev.get('slots')} held: "
                f"{', '.join(short_id(c) for c in held)})")
    if t == "finding":
        return (f"{str(ev.get('severity') or '?').replace('_', '-')} from "
                f"{str(ev.get('persona') or '?').removesuffix('_reviewer')}: "
                f"{_clip(ev.get('title'), 70)}")
    if t == "note":
        author = ev.get("author") or ev.get("by")
        return (f"{author}: " if author and author != "queue" else "") + _clip(ev.get("text"), 80)
    if t == "park":
        detail = _clip(ev.get("detail"), 60)
        return f"parked: {str(ev.get('reason') or '?').replace('_', ' ')}" + (
            f" — {detail}" if detail else "")
    if t == "resume":
        note = _clip(ev.get("note"), 60)
        return f"resumed → {short(ev.get('to'))}" + (f": {note}" if note else "")
    if t == "spec_written":
        return f"spec {ev.get('mode') or 'written'}: {ev.get('spec_id')}"
    if t == "spec_review":
        c = ev.get("counts") or {}
        counts = ", ".join(f"{n} {k.replace('_', '-')}" for k, n in c.items() if n)
        return f"spec review r{ev.get('round')}: {ev.get('verdict')}" + (
            f" ({counts})" if counts else "")
    if t == "spec_accepted":
        return f"spec {ev.get('spec_id')} accepted (round {ev.get('round')})"
    if t == "close":
        ref = ev.get("pr_url") or str(ev.get("merged_sha") or "")[:7]
        return f"closed: {ev.get('mode') or '?'} {ref}".rstrip()
    if t == "open":
        return f"opened {ev.get('branch') or 'the worktree'}"
    if t == "human_decision":
        return f"human decision: {ev.get('choice') or ev.get('label') or ev.get('kind')}" + (
            f" — {_clip(ev.get('note') or ev.get('text'), 50)}" if ev.get("note") or ev.get("text")
            else "")
    if t == "idea_reconciled":
        return f"idea {ev.get('idea_id')}: {ev.get('from')} → {ev.get('to')}"
    skip = {"schema", "ts", "seq", "campaign", "type", "by"}
    bits = []
    for k, v in ev.items():
        if k in skip or v in (None, "", [], {}):
            continue
        bits.append(f"{k}={_clip(v if isinstance(v, str) else str(v), 30)}")
    return _clip(f"{t} " + " ".join(bits), 90)


def event_sentence(ev: dict) -> str:
    """`HH:MM <short sentence>` for one ledger event; raw rows stay in `wt campaign log`."""
    return f"{hhmm(ev.get('ts'))} {_clip(_event_text(ev), 100)}"


QUIET_EVENT_TYPES = frozenset({"cost", "backlog", "priority"})


def recent_lines(ledger: list, n: int = 3) -> list:
    """The last `n` non-bookkeeping events as `{ts, type, text}` (`cost` / `backlog` rows are
    left out: the cost line already shows the total)."""
    picked = [e for e in ledger if e.get("type") not in QUIET_EVENT_TYPES][-n:]
    return [{"ts": e.get("ts"), "type": e.get("type"), "text": event_sentence(e)} for e in picked]
