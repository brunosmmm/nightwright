"""View model for the TUI queue tab (SPEC-0241), campaign screen (SPEC-0246), events tab
(SPEC-0251), mailbox tab (SPEC-0249) and its escalations inbox (SPEC-0250). No Textual import.

Turns `queue.dashboard_payload` and `events.followers` into rows and a header line, computes a
`stat()`-only change signature, and wraps the four human queue actions. `load_campaign` builds
one campaign's header, ledger timeline and review panel. Read-only except `queue_action`, which
goes through the same `plan_*` / `apply_*` functions as the CLI. The events half (stream,
conditions, ping digest, followers) is read-only throughout: it never syncs the stream or takes
a reader.
"""
import datetime as dt
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.text import Text

from .. import render as RD
from ..agentic import escalations as ESC
from ..agentic import events as EV
from ..agentic import lease as L
from ..agentic import mailbox as MB
from ..agentic import profiles as PR
from ..agentic import queue as Q
from ..agentic import runs as RN
from ..agentic import spec_review as SR
from ..agentic import store
from ..agentic import timeline as TLN
from ..agentic.cli_queue import _ago, _dur

ACTIONS = ("park", "resume", "prioritize", "abandon")
EMPTY_META = "queue is empty"


# SPEC-0410: the queue tab's groups, furthest-along work first. Not `GROUPS` (the campaign
# screen's timeline filters, below).
QUEUE_GROUPS = ("needs you", "conductor", "closing", "reviewing", "gating", "building",
                "waiting", "queued", "delivered")
_SOURCE_GROUP = {"needs_you": "needs you", "conductor_handling": "conductor", "waiting": "waiting",
                 "backlog": "queued", "delivered": "delivered"}
_ACTIVE_GROUP = {"CLOSING": "closing", "REVIEWING": "reviewing", "DIVERGENCE_TRIAGE": "reviewing",
                 "GATING": "gating", "TRIAGING": "gating"}          # anything else active is `building`
_DEFAULT_PRIORITY = 3
_SOURCES = ("needs_you", "conductor_handling", "active", "waiting", "backlog", "delivered")
_REVIEW_GLYPH = {"running": "▶", "pass": "✓", "fix": "✗", "failed": "!", "not_run": "·"}


@dataclass(frozen=True)
class QueueRow:
    group: str
    campaign: str
    spec: str
    title: str
    project: str
    state: str
    actions: tuple = ()
    priority: int | None = None
    kind: str = "campaign"      # "header" | "campaign" | "placeholder" (the empty queue)
    count: int = 0              # header rows: the group's data rows
    pri: str = ""
    loops: str = ""
    cost_usd: float | None = None
    cap_usd: float | None = None
    activity: str = ""
    why: str = ""
    state_since: str | None = None
    time_text: str = ""
    sort_key: tuple = field(default=(), compare=False)


def allowed_actions(state, park_requested=False) -> tuple:
    """Mirror the state guards of the plan functions, so the footer never offers an action the
    plan would refuse. A pending deferred park only overwrites on a second `p`, so `park` is
    withheld; `resume` is unaffected because the state is not PARKED."""
    out = []
    if state not in Q.TERMINAL_STATES and state != "PARKED" and not park_requested:
        out.append("park")
    if state == "PARKED":
        out.append("resume")
    if state in ("QUEUED", "WAITING", "PARKED"):
        out.append("prioritize")
    if state in ("PARKED", "QUEUED", "WAITING"):
        out.append("abandon")
    return tuple(out)


def _first_line(text) -> str:
    lines = str(text or "").strip().splitlines()
    return lines[0].strip() if lines else ""


def group_of(state, source) -> str:
    """The one place that maps a payload list (`source`) plus a state to a queue group."""
    if source == "active":
        return _ACTIVE_GROUP.get(state, "building")
    return _SOURCE_GROUP[source]


def cost_bar(cost, cap, width=8) -> str:
    """`███░░░░░` cells of cost against its cap, clamped to `0..width`; no cap -> ''."""
    try:
        cap = float(cap)
    except (TypeError, ValueError):
        return ""
    if cap <= 0:
        return ""
    full = max(0, min(width, round(float(cost or 0) / cap * width)))
    return "█" * full + "░" * (width - full)


def _wait_label(wf) -> str:
    """The short label of what a campaign waits on (`journey.waiting_for`)."""
    kind = (wf or {}).get("kind")
    if kind == "gate_slot":
        return "waiting for a gate slot"
    if kind == "close_train":
        return "waiting for the close train"
    if kind == "campaign_slot":
        return "waiting for a campaign slot"
    if kind == "dependency":
        return ", ".join(str(d) for d in wf.get("on") or []) or "waiting"
    return ""


def _gate_name(it) -> str:
    for src in ((it.get("gate_hold") or {}).get("progress"), it.get("last_gate")):
        if isinstance(src, dict) and (src.get("gate") or src.get("name")):
            return str(src.get("gate") or src.get("name"))
    return "gates"


def _chips(reviews) -> str:                     # SPEC-0277: glyph + 4-char tag per reviewer
    return " ".join(_REVIEW_GLYPH.get(c["status"], "·") + Q.review_short_name(c["persona"])[:4]
                    for c in reviews)


def activity_text(it) -> str:
    """What the row is doing, appended to its state: an open run, the gate (or the gate-slot
    wait), what a waiting/queued row waits on, and the SPEC-0277 reviewer chips."""
    act = it.get("activity") or {}
    wf = it.get("waiting_for")
    parts = []
    if act.get("kind") == "run":
        parts.append(f"{act.get('persona')} {_dur(act.get('elapsed_s'))}")
    elif wf and wf.get("kind") == "gate_slot":
        parts.append(_wait_label(wf))
    elif act.get("kind") == "gates":
        parts.append(_gate_name(it))
    elif wf:
        parts.append(_wait_label(wf))
    if it.get("reviews"):
        parts.append(_chips(it["reviews"]))
    return " ".join(parts)


_AGE_KEY = {"needs you": "park_age_s", "conductor": "park_age_s", "waiting": "waiting_s",
            "queued": "age_s"}


def _age_of(group, it, now) -> int | None:
    """Seconds in the current state: the group's own payload age (the park, the wait, the time
    since creation, the last transition), else the age of `state_since`, else None."""
    age = it.get(_AGE_KEY.get(group, "state_age_s"))
    return age if age is not None else Q._age(it.get("state_since"), now)


def _priority(it) -> int:
    p = it.get("priority")
    return _DEFAULT_PRIORITY if p is None else p


def _epoch(ts) -> float:
    """A timestamp as epoch seconds, so keys sort by time and not by string format (`Z` vs
    `+00:00`, fractional seconds, timezone). Missing or unparsable sorts first."""
    if not ts:
        return 0.0
    try:
        t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if t.tzinfo is None:
        t = t.astimezone()
    return t.timestamp()


def _sort_key(group, it) -> tuple:
    """Fixed timestamps, priority and ids only: never an age (SPEC-0410 Decision 2)."""
    cid = it["campaign"]
    if group == "queued":
        return (it.get("position") or 0, cid)
    if group in ("needs you", "conductor"):
        return (_epoch((it.get("park") or {}).get("parked_at") or it.get("state_since")), cid)
    if group == "waiting":
        return (_priority(it),
                _epoch((it.get("waiting") or {}).get("since") or it.get("state_since")), cid)
    if group == "delivered":
        return (_epoch(it.get("state_since")), cid)     # reversed below, ties by id
    return (_priority(it), _epoch(it.get("created_at")), cid)


def _row(group, it, state=None, now=None) -> QueueRow:
    state = state or it.get("state") or ""
    delivered = group == "delivered"
    why = str(it.get("why") or "")
    if (it.get("park") or {}).get("overdue") and group == "conductor":
        why = (why + " · overdue").lstrip(" ·")
    if delivered:
        time_text = f"finished {_ago(it.get('finished_age_s'))} ago"
    else:
        age = _age_of(group, it, now)
        time_text = "" if age is None else _ago(age)
    return QueueRow(
        group=group, campaign=it["campaign"],
        spec=str(it.get("spec_id") or it.get("idea_id") or ""),
        title=it.get("spec_title") or "", project=str(it.get("project") or ""),
        state=state, actions=allowed_actions(state, bool(it.get("park_requested"))),
        priority=it.get("priority"),
        pri="" if delivered or it.get("priority") is None else str(it["priority"]),
        loops="" if delivered else str(it.get("loops") or ""),
        cost_usd=float(it.get("cost_usd") or 0), cap_usd=it.get("cap_usd"),
        activity="" if delivered else activity_text(it), why=why,
        state_since=it.get("state_since"), time_text=time_text, sort_key=_sort_key(group, it))


def rows_from_payload(payload, *, collapsed=frozenset(), now=None) -> list[QueueRow]:
    """Header and data rows in their final order. Sorts by the stable keys of `_sort_key`, never
    by the payload's list order. A group in `collapsed` emits its header only."""
    by_group: dict = {g: [] for g in QUEUE_GROUPS}
    for source in _SOURCES:
        for it in payload.get(source) or []:
            state = "QUEUED" if source == "backlog" else None
            group = group_of(state or it.get("state"), source)
            by_group[group].append(_row(group, it, state, now))
    rows = []
    for group in QUEUE_GROUPS:
        items = by_group[group]
        if not items:
            continue
        items.sort(key=lambda r: r.sort_key)
        if group == "delivered":                # newest first, ties by campaign id
            items.sort(key=lambda r: r.sort_key[0], reverse=True)   # stable: id order kept
        rows.append(QueueRow(group=group, campaign="", spec="", title="", project="", state="",
                             kind="header", count=len(items)))
        if group not in collapsed:
            rows.extend(items)
    if not rows:
        rows.append(QueueRow(group="", campaign="", spec="", title=EMPTY_META, project="",
                             state="", kind="placeholder"))
    return rows


def header_line(payload, followers) -> str:
    lease = payload.get("lease") or {}
    if not lease.get("held"):
        runner = "down"
    elif lease.get("stale"):
        runner = f"stale {lease.get('pid')}"
    else:
        runner = str(lease.get("pid"))
    hb = lease.get("heartbeat_age_s")
    slots = payload.get("slots") or {}
    conductor = next((str(f["pid"]) for f in followers
                      if f.get("reader") == "conductor" and f.get("held")), "none")
    prefix = "mailbox:steward:"
    stewards = sorted(f["reader"][len(prefix):] for f in followers
                      if f.get("reader", "").startswith(prefix) and f.get("held"))
    spend = (payload.get("totals") or {}).get("spend_today_usd") or 0
    line = (f"runner {runner} · hb {'-' if hb is None else f'{hb:g}'}s · "
            f"slots {slots.get('busy', 0)}/{slots.get('total', 0)} · conductor {conductor} · "
            f"stewards {', '.join(stewards) or 'none'} · today ${float(spend):.2f}")
    if lease.get("draining"):
        line += " · draining"
    return line


def load_queue(cfg, *, project=None) -> tuple[list[QueueRow], str]:
    payload = Q.dashboard_payload(cfg, project=project, recent=20)
    return rows_from_payload(payload), header_line(payload, EV.followers(cfg))


def _stat(path):
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def queue_signature(cfg) -> tuple:
    """Cheap change detector, `stat()` calls only. A follower taking or dropping its reader lock
    changes no mtime, so conductor/steward changes in the header wait for the periodic reload."""
    root = store.campaigns_dir(cfg)
    states = []
    try:
        dirs = list(root.iterdir()) if root.is_dir() else []
    except OSError:
        dirs = []
    for d in dirs:
        try:
            states.append((d.name, (d / "state.json").stat().st_mtime_ns))
        except OSError:
            continue
    return (_stat(EV.events_path(cfg)), _stat(L.lease_path(cfg)), tuple(sorted(states)))


def plan_queue_action(cfg, action, campaign, *, reason=None, note=None, priority=None,
                      keep=False) -> dict:
    if action == "park":
        return Q.plan_park(cfg, campaign, reason)
    if action == "resume":
        return Q.plan_resume(cfg, campaign, note=note)
    if action == "prioritize":
        return Q.plan_prioritize(cfg, campaign, priority)
    if action == "abandon":
        return Q.plan_abandon(cfg, campaign, keep_worktree=keep)
    raise ValueError(f"unknown queue action {action!r}")


def queue_action(cfg, action, campaign, **kw) -> dict:
    plan = plan_queue_action(cfg, action, campaign, **kw)
    apply = {"park": Q.apply_park, "resume": Q.apply_resume,
             "prioritize": Q.apply_prioritize, "abandon": Q.apply_abandon}[action]
    return apply(cfg, plan)


# --- campaign detail (SPEC-0246) ----------------------------------------------------------

GROUPS = ("transitions", "runs", "gates", "findings", "notes")
_GROUP_OF = {
    **dict.fromkeys(("open", "transition", "park", "resume", "priority", "close",
                     "admitted_after_revision", "admitted_revise_pending"), "transitions"),
    **dict.fromkeys(("run.start", "run.end", "cost"), "runs"),
    "gate": "gates",
    **dict.fromkeys(("finding", "followup"), "findings"),
}
GATE_LOG_LINES = 40
SEVERITIES = ("must_fix", "major", "minor", "nit")


def event_group(etype) -> str:
    return _GROUP_OF.get(etype, "notes")


@dataclass(frozen=True)
class TimelineEntry:
    seq: int
    ts: str
    type: str
    group: str
    text: str
    ref: object = None          # run id (run events) or gate_seq (gate events)
    event: dict = field(default_factory=dict, compare=False, hash=False)


@dataclass
class CampaignDetail:
    campaign: str
    state: dict
    header: list
    entries: list
    review: Text
    actions: tuple
    signature: tuple
    story: dict = field(default_factory=dict)              # SPEC-0433 nw.campaign.story.v1
    events_by_seq: dict = field(default_factory=dict)


def _entry(ev: dict) -> TimelineEntry:
    etype = str(ev.get("type", "?"))
    line = RD.campaign_event(ev)
    # `campaign_event` is `seq  ts  type  by  payload`; the screen has its own seq/ts/type columns
    parts = line.plain.split(None, 3)
    rest = parts[3] if len(parts) > 3 else ""
    ref = None
    if etype in ("run.start", "run.end"):
        ref = ev.get("run_id")
    elif etype == "gate":
        ref = ev.get("gate_seq")
    return TimelineEntry(seq=ev.get("seq") or 0, ts=str(ev.get("ts") or ""), type=etype,
                         group=event_group(etype), text=rest, ref=ref, event=ev)


def _followups_line(state) -> Text | None:
    fu = (state.get("close") or {}).get("followups") or {}
    ids = []
    for f in fu.get("filed") or []:
        i = f.get("idea_id") if isinstance(f, dict) else None
        if i and i not in ids:
            ids.append(i)
    counts = [(k, len(fu.get(k) or [])) for k in ("unfiled", "waived", "capped")]
    counts = [f"{n} {k}" for k, n in counts if n]
    if not ids and not counts:
        return None
    bits = ([f"follow-ups {' '.join(ids)}"] if ids else []) + counts
    return Text(" · ".join(bits))


def header_lines(cfg, state) -> list:
    cost = state.get("cost") or {}
    counters = state.get("counters") or {}
    repo = state.get("repo") or {}
    ident = state.get("spec_id") or (state.get("idea") or {}).get("id") or Q.campaign_label(state)
    title = Q._spec_title(state) or ""
    l1 = Text()
    l1.append(str(state.get("id")), style="bold")
    l1.append(f"  {state.get('state')}  spec {ident} {title}  project {state.get('project') or '-'}")
    try:
        loop_max = PR.resolve_profile(cfg, state.get("profile"),
                                      project_root=repo.get("root")).get("loop_max")
    except ValueError:
        loop_max = None
    total, est, cap = (float(cost.get("total_usd") or 0.0),
                       float(cost.get("estimated_usd") or 0.0), cost.get("cap_usd"))
    l2 = (f"loops {counters.get('loops', 0)}/{loop_max if loop_max is not None else '?'}  "
          f"runs {counters.get('runs', 0)}  cost ${total:.2f}"
          + (f" (~${est:.2f} est.)" if est > 0 else "")
          + (f" / ${float(cap):.2f}" if cap else ""))
    sizing = state.get("sizing") or {}
    if sizing:
        src = f" ({sizing['source']})" if sizing.get("source") else ""
        l2 += (f"  profile {state.get('profile') or '-'}{src}  size {sizing.get('size') or '-'}"
               f"  tone {sizing.get('tone') or '-'}")
    lines = [l1, Text(l2)]
    if repo:
        wt_path = repo.get("worktree")
        missing = "" if wt_path and os.path.isdir(wt_path) else " (missing)"
        lines.append(Text(f"branch {repo.get('branch') or '-'}  worktree {wt_path or '-'}{missing}"))
    park = state.get("park")
    if park:
        lines.append(Text(f"parked {park.get('reason')}: {park.get('detail', '')} "
                          f"→ resume to {park.get('resume_to')}", style="yellow"))
    elif state.get("park_requested"):
        lines.append(Text(f"park requested: {state['park_requested'].get('detail', '')}",
                          style="yellow"))
    fu = _followups_line(state)
    if fu:
        lines.append(fu)
    return lines


def load_campaign(cfg, cid) -> CampaignDetail:
    """Raises ValueError for an unknown campaign (from `store.read_state`)."""
    signature = campaign_signature(cfg, cid)        # first: a write during the load re-triggers
    state = store.read_state(cfg, cid)
    events = store.read_ledger(cfg, cid)
    entries = [_entry(ev) for ev in events]
    entries.sort(key=lambda e: e.seq)
    return CampaignDetail(
        campaign=cid, state=state, header=header_lines(cfg, state), entries=entries,
        review=review_text(cfg, state),
        actions=allowed_actions(state.get("state"), bool(state.get("park_requested"))),
        signature=signature,
        story=TLN.story(events, campaign=cid, state=state, now=store.now_iso()),
        events_by_seq={ev.get("seq"): ev for ev in events if isinstance(ev, dict)})


def campaign_signature(cfg, cid) -> tuple:
    def stat(path):
        try:
            return path.stat()
        except OSError:
            return None
    st = stat(store.campaign_dir(cfg, cid) / "state.json")
    ledger = stat(store.campaign_dir(cfg, cid) / "ledger.jsonl")
    return (st.st_mtime_ns if st else None, ledger.st_size if ledger else None)


def _field(t: Text, key, value) -> None:
    text = value if isinstance(value, str) else str(value)
    t.append(f"{key}: ", style="dim")
    t.append(text)
    t.append("\n")


def _payload(t: Text, ev: dict) -> None:
    for k, v in ev.items():
        if k in ("schema", "campaign"):
            continue
        _field(t, k, v)


def _file_tail(path: Path, n=GATE_LOG_LINES) -> list:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return None


def _run_detail(t: Text, cfg, cid, ev) -> None:
    try:
        rec = RN.campaign_run(cfg, cid, ev.get("run_id"))
    except ValueError as e:
        t.append(f"(run unavailable: {e})\n", style="yellow")
        _payload(t, ev)
        return
    for k in ("run_id", "persona", "model", "effort", "status", "verdict_status", "duration_s",
              "cost_usd", "charged_usd"):
        _field(t, k, rec.get(k))
    verdict = rec.get("verdict") if isinstance(rec.get("verdict"), dict) else {}
    if verdict.get("summary"):
        t.append("summary\n", style="bold")
        t.append(f"{verdict['summary']}\n")
    for k in ("must_fix", "blockers"):
        if rec.get(k):
            t.append(f"{k}\n", style="bold")
            for item in rec[k]:
                t.append(f"  - {item}\n")
    _field(t, "permission_denials", len(rec.get("permission_denials") or []))
    paths = rec.get("paths") or {}
    if rec.get("stderr_tail"):
        t.append("stderr_tail\n", style="bold")
        t.append("\n".join(rec["stderr_tail"]) + "\n", style="dim")
    t.append("paths\n", style="bold")
    run_dir = store.runs_dir(cfg, cid) / str(rec.get("run_id"))
    if not paths:
        t.append(f"  (missing: {run_dir})\n", style="yellow")
    for k, v in paths.items():
        t.append(f"  {k}: {v}\n")


def _gate_detail(t: Text, cfg, cid, ev) -> None:
    results = [r for r in ev.get("results") or [] if isinstance(r, dict)]
    for key in ("gate_seq", "ok", "started_at", "duration_s"):
        _field(t, key, ev.get(key))
    for r in results:
        t.append(f"{r.get('name')}: ", style="bold")
        t.append(f"{'ok' if r.get('ok') else 'FAIL'}", style="green" if r.get("ok") else "red")
        t.append(f"  required={r.get('required')}  exit={r.get('exit_code')}  "
                 f"duration={r.get('duration_s')}\n")
    for r in results:
        if r.get("ok"):
            continue
        log = r.get("log")
        t.append(f"\n{r.get('name')} log tail\n", style="bold")
        if not log:
            t.append("(missing: no log recorded)\n", style="yellow")
            continue
        path = store.campaign_dir(cfg, cid) / log
        lines = _file_tail(path)
        if lines is None:
            t.append(f"(missing: {path})\n", style="yellow")
        else:
            t.append("\n".join(lines) + "\n", style="dim")


def entry_detail(cfg, cid, entry) -> Text:
    """The detail pane for one timeline entry. Never raises."""
    t = Text()
    ev = entry.event
    try:
        if entry.type in ("run.start", "run.end"):
            _run_detail(t, cfg, cid, ev)
        elif entry.type == "gate":
            _gate_detail(t, cfg, cid, ev)
        else:
            _payload(t, ev)
    except Exception as e:      # the pane must never take the screen down
        t.append(f"(detail unavailable: {e})\n", style="red")
    t.rstrip()
    return t


STORY_TONE = {"ok": "green", "bad": "red", "warn": "yellow", "info": "blue", "dim": "dim"}


def _raw_events(t: Text, detail, seqs) -> None:
    evs = [detail.events_by_seq[s] for s in seqs or [] if s in detail.events_by_seq]
    if not evs:
        return
    t.append(f"\nraw events ({len(evs)})\n", style="bold")
    for ev in evs:
        t.append_text(RD.campaign_event(ev))
        t.append("\n")


def story_detail(cfg, cid, detail, node) -> Text:
    """The detail pane for one SPEC-0433 story node `(kind, data)`: its text, its children,
    the run or gate detail of its main event, then its raw ledger events. Never raises."""
    t = Text()
    try:
        kind, data = node
        if kind == "loop":
            t.append(f"{data.get('header')}\n", style="bold")
            for g in data.get("groups") or []:
                t.append(f"  {g.get('header')}\n")
        elif kind == "group":
            t.append(f"{data.get('header')}\n", style="bold")
            for tr in data.get("transitions") or []:
                t.append(f"  {tr.get('seq')}  {str(tr.get('ts') or '')[11:19]}  "
                         f"{tr.get('from')} → {tr.get('to')}  ({tr.get('reason')}, "
                         f"{tr.get('by')})\n", style="cyan")
            t.append(f"{len(data.get('lines') or [])} lines, {len(data.get('seqs') or [])} events\n",
                     style="dim")
        else:
            t.append(f"{data.get('text')}\n", style="bold " + STORY_TONE.get(data.get("tone"), ""))
            for c in data.get("children") or []:
                t.append(f"  {c.get('text')}\n", style=STORY_TONE.get(c.get("tone"), ""))
            ref = detail.events_by_seq.get(data.get("ref_seq"))
            if kind == "line" and ref and ref.get("type") in ("run.start", "run.end", "gate"):
                t.append("\n")
                t.append_text(entry_detail(cfg, cid, _entry(ref)))
                t.append("\n")
            _raw_events(t, detail, data.get("seqs"))
    except Exception as e:      # the pane must never take the screen down
        t.append(f"(detail unavailable: {e})\n", style="red")
    t.rstrip()
    return t


def _finding_lines(t: Text, findings, *, reason=False) -> None:
    for sev in SEVERITIES:
        for f in findings:
            if (f.get("severity") or "minor") != sev:
                continue
            personas = ",".join(f.get("personas") or [])
            files = ",".join(f.get("files") or [])
            t.append(f"  {f.get('id', '-')} {sev} {personas} {f.get('title', '')}"
                     + (f" ({files})" if files else "") + "\n")
            if reason and f.get("reason"):
                t.append(f"      reason: {f['reason']}\n", style="dim")


def _code_review(t: Text, cfg, state) -> None:
    lr = state.get("last_review") or {}
    if lr.get("verdict") is None:
        t.append("no code review yet\n")
        return
    waived = [w for w in lr.get("waived") or [] if isinstance(w, dict)]
    pass_score, blocking, err = "?", None, None
    try:
        profile = PR.resolve_profile(cfg, state.get("profile"),
                                     project_root=(state.get("repo") or {}).get("root"))
        pass_score = PR.review_params(profile)[0]
        stored = lr.get("verdicts") or {}
        verdicts = {p: stored[p] for p in PR.reviewers_of(profile) if isinstance(stored.get(p), dict)}
        blocking = Q.triage_reviews(verdicts, profile, waived=[w["id"] for w in waived
                                                               if "id" in w])["blocking"]
    except ValueError as e:
        err = e
    t.append(f"score {lr.get('score')} / pass {pass_score}  verdict {lr.get('verdict')}  "
             f"loop {lr.get('loop')}\n", style="bold")
    t.append("blocking:" + ("" if blocking is None else f" {len(blocking)}") + "\n")
    if blocking is None:
        t.append(f"  unavailable ({err})\n", style="yellow")
    else:
        _finding_lines(t, blocking)
    advisory = [f for f in lr.get("advisory") or [] if isinstance(f, dict)]
    t.append(f"advisory: {len(advisory)}\n")
    _finding_lines(t, advisory)
    t.append(f"waived: {len(waived)}\n")
    _finding_lines(t, waived, reason=True)


def _spec_review(t: Text, cfg, state) -> None:
    repo = (state.get("repo") or {}).get("root")
    rel = (state.get("spec") or {}).get("rel_path")
    spec_id = state.get("spec_id")
    review = None
    if spec_id and not Q.has_no_spec(state) and repo and rel:
        review = SR.latest_review(cfg, spec_id, Path(repo) / rel)
    if not review:
        t.append("no spec review\n")
        return
    t.append(f"verdict {review.get('verdict')}  size {review.get('size') or '-'}  "
             f"tone {review.get('tone') or '-'}  cost ${float(review.get('cost_usd') or 0):.2f}"
             f"  reviewed {review.get('reviewed_at')}\n", style="bold")
    if review.get("rationale"):
        t.append(f"rationale: {review['rationale']}\n")
    for f in review.get("findings") or []:
        if isinstance(f, dict):
            t.append(f"  {f.get('severity', '-')} {f.get('title', '')}\n")


def review_text(cfg, state) -> Text:
    """The review panel: the last code review, then the latest spec review. Never raises for
    an absent or unresolvable part."""
    t = Text()
    t.append("Code review\n", style="bold underline")
    _code_review(t, cfg, state)
    t.append("\nSpec review\n", style="bold underline")
    _spec_review(t, cfg, state)
    t.rstrip()
    return t


# --- events tab (SPEC-0251) ---------------------------------------------------------------

ALERT_TYPES = frozenset({
    "runner_dead", "lease_stale", "run_overdue", "campaign_silent", "backlog_blocked_long",
    "state_stuck", "ready_not_admitted", "runner_draining", "runner_config_invalid",
    "run_failed"})
_YELLOW_TYPES = frozenset({"parked", "escalation"})
_GREEN_TYPES = frozenset({"merged", "handed_off"})
EVENT_LIMIT = 2000
LAG_CURSOR_AGE_S = 60
NO_ALERTS = "no alerts"
NO_FOLLOWER_WARNING = " · no live follower: the stream may lag, conditions are live"


def event_style(type_, data=None) -> str:
    if type_ == "run_failed" and (data or {}).get("transient"):
        return "yellow"
    if type_ in ALERT_TYPES:
        return "red"
    if type_ in _YELLOW_TYPES:
        return "yellow"
    if type_ in _GREEN_TYPES:
        return "green"
    return ""


@dataclass(frozen=True)
class EventRow:
    id: int
    ts: str
    type: str
    campaign: str
    text: str
    style: str
    data: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class EventFilter:
    types: frozenset = frozenset()
    campaign: str | None = None
    alerts_only: bool = False


def parse_event_filter(text) -> EventFilter:
    """`type=parked,merged campaign=SPEC-0230 alerts` -> EventFilter; ValueError names a bad
    token so the hint line can say what was wrong."""
    types: set = set()
    campaign = None
    alerts = False
    for token in str(text or "").split():
        key, eq, value = token.partition("=")
        if key == "type" and eq:
            for t in filter(None, value.split(",")):
                if t not in EV.EVENT_TYPES:
                    raise ValueError(f"unknown event type {t!r}")
                types.add(t)
        elif key == "campaign" and eq and value:
            campaign = value
        elif token in ("alerts", "alerts_only") and not eq:
            alerts = True
        else:
            raise ValueError(f"unknown filter token {token!r} (type=… campaign=… alerts)")
    return EventFilter(frozenset(types), campaign, alerts)


def format_event_filter(f) -> str:
    bits = []
    if f.types:
        bits.append("type=" + ",".join(sorted(f.types)))
    if f.campaign:
        bits.append(f"campaign={f.campaign}")
    if f.alerts_only:
        bits.append("alerts")
    return " ".join(bits)


def _event_text(ev) -> str:
    """`events.summary_line` without its `#id type campaign` prefix (the table has columns)."""
    line = EV.summary_line(ev)
    prefix = f"#{ev.get('id')} {ev.get('type')}"
    if not line.startswith(prefix):
        return line
    rest = line[len(prefix):].lstrip()
    if ev.get("type") != "conductor_ping":
        who = ev.get("campaign") or "-"
        if rest.startswith(who):
            rest = rest[len(who):].lstrip()
    return rest


def load_events(cfg, after_id=0, limit=EVENT_LIMIT) -> list[EventRow]:
    rows = []
    for ev in EV.read_since(cfg, after_id)[-limit:]:
        try:
            rows.append(EventRow(
                id=ev["id"], ts=str(ev.get("ts") or ""), type=str(ev.get("type") or ""),
                campaign=str(ev.get("campaign") or ""), text=_event_text(ev),
                style=event_style(ev.get("type"), ev.get("data")),
                data=ev.get("data") if isinstance(ev.get("data"), dict) else {}))
        except Exception:                   # a malformed record (even a missing key) is skipped
            continue
    return rows


def matches(row, f) -> bool:
    if f.types and row.type not in f.types:
        return False
    if f.campaign and f.campaign not in row.campaign:
        return False
    return not (f.alerts_only and row.type not in ALERT_TYPES)


def event_time(ts, now=None) -> str:
    """HH:MM:SS local, with the date when it is not today."""
    try:
        when = dt.datetime.fromisoformat(ts).astimezone()
    except (TypeError, ValueError, OverflowError):
        return str(ts or "")[:19]
    today = (now or dt.datetime.now().astimezone()).date()
    return when.strftime("%H:%M:%S" if when.date() == today else "%m-%d %H:%M:%S")


@dataclass(frozen=True)
class ConditionRow:
    type: str
    campaign: str
    text: str
    style: str


def _kv(data) -> str:
    return " ".join(f"{k}={v}" for k, v in (data or {}).items() if v not in (None, "", []))


def load_conditions(cfg) -> list[ConditionRow]:
    """The watchdog conditions true now (`events.watchdog_checks`, pure), runner-level first."""
    rows = [ConditionRow(type_, cid or "", _kv(data),
                         "red" if type_ in ALERT_TYPES else "yellow")
            for _key, type_, cid, data in EV.watchdog_checks(cfg)]
    return sorted(rows, key=lambda r: (bool(r.campaign), r.type, r.campaign))


def _entries(data, key) -> list[dict]:
    """The dict entries of a ping list; anything else in a hand-edited record is ignored."""
    items = data.get(key)
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _ping_lines(data) -> list[tuple]:
    out = []
    for item in _entries(data, "blocked"):
        out.append(("blocked", item.get("campaign"), item.get("blocked_for_s")))
    for item in _entries(data, "stuck"):
        out.append(("stuck", item.get("campaign"), item.get("stuck_s")))
    for item in _entries(data, "ready_not_admitted"):
        out.append(("ready_not_admitted", item.get("campaign"), item.get("ready_for_s")))
    for item in _entries(data, "parked"):
        if item.get("owner") == "conductor" and not item.get("decided"):
            out.append(("parked", item.get("campaign"), item.get("parked_for_s")))
    return out


def ping_text(rows, cfg=None, now=None) -> Text:
    """The newest `conductor_ping` among the loaded rows, as a panel body."""
    ping = next((r for r in reversed(rows) if r.type == "conductor_ping"), None)
    if ping is None:
        interval = (EV.settings(cfg) if cfg is not None else EV.DEFAULTS)["ping_interval_s"]
        return Text(f"no conductor_ping yet (interval {interval}s)", style="dim")
    c = ping.data.get("counts")
    c = c if isinstance(c, dict) else {}
    out = Text(f"active {c.get('active', 0)} · queued {c.get('queued', 0)} · "
               f"ready {c.get('ready', 0)} · blocked {c.get('blocked', 0)} · "
               f"parked {c.get('parked', 0)} (undecided {c.get('undecided_parks', 0)}) · "
               f"stuck {c.get('stuck', 0)} · waiting {c.get('waiting', 0)}")
    for kind, campaign, age in _ping_lines(ping.data):
        out.append(f"\n{kind} {campaign or '-'} {_ago(age)}", style="yellow")
    age = EV._age_s(ping.ts, now or dt.datetime.now().astimezone())
    out.append(f"\nping {_ago(age)} ago", style="dim")
    return out


@dataclass(frozen=True)
class FollowerRow:
    reader: str
    kind: str
    pid: str
    held: bool
    lag: int
    cursor_age_s: int | None
    flag: str = ""          # "dead" (red) | "lagging" (yellow) | ""


def load_followers(cfg) -> list[FollowerRow]:
    rows = []
    for f in EV.followers(cfg):
        age = f.get("cursor_age_s")
        if not f["held"]:
            flag = "dead"
        elif f["lag"] > 0 and age is not None and age > LAG_CURSOR_AGE_S:
            flag = "lagging"
        else:
            flag = ""
        rows.append(FollowerRow(f["reader"], f["kind"], "-" if f["pid"] is None else str(f["pid"]),
                                f["held"], f["lag"], age, flag))
    return rows


def stream_health(cfg, followers) -> str:
    """How fresh the stream is: the projector file is rewritten by every `sync`. `followers`
    are `load_followers` rows."""
    try:
        age = max(0, int(time.time() - EV._projector_path(cfg).stat().st_mtime))
        line = f"stream synced {_ago(age)} ago"
    except OSError:
        line = "stream never synced"
    if not any(f.kind == "events" and f.held for f in followers):
        line += NO_FOLLOWER_WARNING
    return line


def events_signature(cfg):
    return _stat(EV.events_path(cfg))


def events_stat(cfg):
    """`(size, mtime_ns, inode)` from one `stat()`, so the inode matches the size."""
    try:
        st = EV.events_path(cfg).stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns, st.st_ino)


# --- mailbox (SPEC-0249) ------------------------------------------------------------------
# Every function reads one `mailbox._all_messages` snapshot (`snapshot`, or the `msgs` a caller
# passes in), so the rows of one reload cannot disagree and the cost is one read of the inbox
# files, not one per thread. The rules mirror `mailbox.unanswered` and `mailbox.thread`.

HUMAN = "human"
ESCALATIONS = "escalations"                 # the pseudo-inbox row (SPEC-0250)
SUBJECT_W = 80
FINDINGS_SHOWN = 15


@dataclass(frozen=True)
class InboxRow:
    address: str
    unanswered: int
    total: int
    last_ts: str | None


@dataclass(frozen=True)
class ThreadRow:
    root_id: str
    subject: str
    peer: str
    campaign: str | None
    count: int
    last_ts: str | None
    unanswered: bool
    reply_to: dict


@dataclass
class MailboxSnapshot:
    msgs: list
    inboxes: list
    inbox: str
    threads: list
    escalations: list = field(default_factory=list)     # [EscRow], the escalations inbox


def snapshot(cfg) -> list[dict]:
    return MB._all_messages(cfg)


def replied_ids(msgs) -> set:
    return {m["re"] for m in msgs if m.get("re")}


def thread_of(msgs, root_id) -> list[dict]:
    """`mailbox.thread` over a snapshot: the root, then replies in reply order."""
    by_id = {m["id"]: m for m in msgs}
    if root_id not in by_id:
        return []
    want, out = {root_id}, [by_id[root_id]]
    changed = True
    while changed:
        changed = False
        for m in msgs:
            if m.get("re") in want and m["id"] not in want:
                want.add(m["id"])
                out.append(m)
                changed = True
    return out


def thread_root(msgs_by_id, msg) -> str:
    """Follow `re` up to a message without `re` or with an unknown one. On a cycle the root is
    the smallest id of the cycle, so every member (and what hangs off it) groups together."""
    seen: list = []
    cur = msg
    while True:
        if cur["id"] in seen:
            return min(seen[seen.index(cur["id"]):])
        seen.append(cur["id"])
        parent = msgs_by_id.get(cur.get("re"))
        if parent is None:
            return cur["id"]
        cur = parent


def _needs_reply(m, replied) -> bool:
    return bool(m.get("needs_reply")) and m["id"] not in replied


def load_inboxes(cfg, msgs=None) -> list[InboxRow]:
    msgs = snapshot(cfg) if msgs is None else msgs
    replied = replied_ids(msgs)
    out = []
    for addr in MB.known_addresses(cfg):
        mine = [m for m in msgs if m.get("to") == addr]
        out.append(InboxRow(addr, sum(1 for m in mine if _needs_reply(m, replied)), len(mine),
                            max((m.get("ts") or "" for m in mine), default=None) or None))
    return out


def _latest(thread):
    """Newest message of a thread: by `ts`, ties by reply order."""
    return max(enumerate(thread), key=lambda p: (p[1].get("ts") or "", p[0]))[1]


def _peer(thread, addr) -> str:
    mine = [m for m in thread if addr in (m.get("from"), m.get("to"))]
    if not mine:
        return _latest(thread).get("from") or ""
    m = _latest(mine)
    return m["to"] if m.get("from") == addr else m["from"]


def load_threads(cfg, addr, msgs=None) -> list[ThreadRow]:
    msgs = snapshot(cfg) if msgs is None else msgs
    by_id = {m["id"]: m for m in msgs}
    replied = replied_ids(msgs)
    roots: dict = {}
    for m in msgs:
        if m.get("to") == addr:
            roots.setdefault(thread_root(by_id, m), None)
    rows = []
    for root_id in roots:
        thread = thread_of(msgs, root_id)
        mine = [m for m in thread if m.get("to") == addr]
        root = by_id[root_id]
        rows.append(ThreadRow(
            root_id=root_id, subject=_first_line(root.get("text"))[:SUBJECT_W],
            peer=_peer(thread, addr),
            campaign=next((m["campaign"] for m in thread if m.get("campaign")), None),
            count=len(thread), last_ts=_latest(thread).get("ts"),
            unanswered=any(_needs_reply(m, replied) for m in mine),
            reply_to=_latest(mine) if mine else root))
    rows.sort(key=lambda r: r.last_ts or "", reverse=True)
    rows.sort(key=lambda r: not r.unanswered)           # stable: unanswered first, then newest
    return rows


def initial_inbox(inboxes, escalations=()) -> str:
    """An open escalation parks a campaign until a human acts, so it outranks mail."""
    if any(r.status == "open" for r in escalations):
        return ESCALATIONS
    return next((r.address for r in inboxes if r.unanswered), HUMAN)


def load_mailbox(cfg, inbox=None) -> MailboxSnapshot:
    """One snapshot for a whole reload: inbox counts, the escalation rows and the threads of
    `inbox` (`escalations` when one is open, else the first inbox with unanswered mail, else
    `human`, when None). The escalations inbox has no threads."""
    msgs = snapshot(cfg)
    inboxes = load_inboxes(cfg, msgs)
    escs = load_escalations(cfg)
    inbox = inbox or initial_inbox(inboxes, escs)
    threads = [] if inbox == ESCALATIONS else load_threads(cfg, inbox, msgs)
    return MailboxSnapshot(msgs, inboxes, inbox, threads, escs)


_ESC_MAIL_RE = re.compile(r"nw escalations show (ESC-\d{4,})")


def thread_text(cfg, root_id, msgs=None) -> Text:
    msgs = snapshot(cfg) if msgs is None else msgs
    replied = replied_ids(msgs)
    t = Text()
    for m in thread_of(msgs, root_id):
        t.append(f"{m['id']}  {m.get('ts') or '-'}  {m.get('from')} → {m.get('to')}  "
                 f"{m.get('kind')}", style="bold")
        if m.get("campaign"):
            t.append(f"  [{m['campaign']}]", style="dim")
        if _needs_reply(m, replied):
            t.append("  needs reply", style="yellow")
        t.append("\n")
        for line in str(m.get("text") or "").splitlines() or [""]:
            t.append(f"    {line}\n")
        hit = _ESC_MAIL_RE.search(str(m.get("text") or ""))
        if hit:                                 # a plain reply is mail, not the record's answer
            t.append(f"    answer in escalations: {hit.group(1)}\n", style="yellow")
        t.append("\n")
    return t


def mailbox_signature(cfg) -> tuple:
    """Sorted (name, size, mtime_ns) of the inbox files; `stat()` calls only."""
    d = MB.mailbox_dir(cfg)
    out = []
    if d.is_dir():
        for p in d.glob("*.jsonl"):
            try:
                st = p.stat()
            except OSError:
                continue
            out.append((p.name, st.st_size, st.st_mtime_ns))
    return tuple(sorted(out))


def age_s(ts) -> float | None:
    try:
        t = dt.datetime.fromisoformat(ts)
        if t.tzinfo is None:
            t = t.astimezone()
        return max(0.0, (dt.datetime.now().astimezone() - t).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def reply_target(reply_to) -> tuple[str, str | None]:
    """(address, re) of a human reply to `reply_to`. A follow-up to the human's own message goes
    to its addressee with the parent's `re`, so it never marks the human's message answered."""
    if reply_to.get("from") == HUMAN:
        return reply_to["to"], reply_to.get("re")
    return reply_to["from"], reply_to["id"]


def default_reply_kind(reply_to) -> str:
    return "answer" if reply_to.get("kind") in ("question", "request") else "info"


def answers_notice(msgs, re) -> str:
    """`; this answers it for <inbox>` when `re` names a message that still needs a reply and
    sits in an inbox other than the human's (the human stepping in), else ''."""
    by_id = {m["id"]: m for m in msgs}
    m = by_id.get(re)
    if m and m.get("to") != HUMAN and _needs_reply(m, replied_ids(msgs)):
        return f"; this answers it for {m['to']}"
    return ""


def send_plan(cfg, *, to, kind, text, campaign=None, re=None) -> dict:
    """Validate a human message without writing it."""
    to = MB.validate_address(cfg, to)
    if kind not in MB.KINDS:
        raise ValueError(f"unknown kind {kind!r} ({' | '.join(MB.KINDS)})")
    if not (text or "").strip():
        raise ValueError("empty message text")
    return {"to": to, "frm": HUMAN, "kind": kind, "campaign": campaign, "re": re, "text": text,
            "needs_reply": kind in ("question", "request")}


def send_apply(cfg, plan) -> dict:
    return MB.send(cfg, to=plan["to"], frm=HUMAN, text=plan["text"], kind=plan["kind"],
                   campaign=plan["campaign"], re=plan["re"])


# --- escalations inbox (SPEC-0250) --------------------------------------------------------
# Answers and withdrawals go through `escalations.plan_answer` / `apply_answer` and
# `plan_withdraw` / `apply_withdraw`, always as `human`.

@dataclass(frozen=True)
class EscRow:
    id: str
    status: str
    campaign: str | None
    spec_id: str | None
    question: str
    recommendation: int | None
    age_s: float | None
    options_n: int


def load_escalations(cfg) -> list[EscRow]:
    """Every record, `open` first, then newest `created` first."""
    recs = ESC.list_all(cfg)
    recs.sort(key=lambda r: r.get("created") or "", reverse=True)
    recs.sort(key=lambda r: r.get("status") != "open")          # stable
    return [EscRow(id=r["id"], status=str(r.get("status") or "?"), campaign=r.get("campaign"),
                   spec_id=r.get("spec_id"), question=_first_line(r.get("question"))[:SUBJECT_W],
                   recommendation=r.get("recommendation"), age_s=age_s(r.get("created")),
                   options_n=len(r.get("options") or []))
            for r in recs if r.get("id")]


def _esc_campaign_line(cfg, t: Text, rec) -> None:
    cid = rec.get("campaign")
    if not cid:
        t.append("no campaign\n", style="dim")
        return
    try:
        state = store.read_state(cfg, cid).get("state") or "?"
    except (ValueError, OSError):
        state = "(campaign gone)"
    t.append(f"campaign {cid}  {rec.get('spec_id') or '-'}  {state}\n")


def _override_block(cfg, t: Text, rec) -> None:
    t.append("\noverride (CLI):\n", style="bold")
    t.append(f"  nw escalations answer {rec['id']} --waive <finding-id> --attest <ac> "
             f"--reason \"<why>\" --yes\n")
    try:
        findings = ESC.answerable_findings(cfg, store.read_state(cfg, rec["campaign"]),
                                           rec["id"])
    except (ValueError, OSError, TypeError, KeyError):
        findings = []
    for f in findings[:FINDINGS_SHOWN]:
        t.append(f"  {f['id']} {f['severity']} {f['title']}\n", style="dim")
    if len(findings) > FINDINGS_SHOWN:
        t.append(f"  +{len(findings) - FINDINGS_SHOWN} more\n", style="dim")


def escalation_text(cfg, rec) -> Text:
    t = Text()
    t.append(f"{rec.get('id')}  {rec.get('status')}  {rec.get('created') or '-'}  "
             f"{rec.get('by') or '-'}\n", style="bold")
    _esc_campaign_line(cfg, t, rec)
    t.append(f"\n{rec.get('question') or ''}\n\n")
    for o in rec.get("options") or []:
        mark = " (recommended)" if o.get("n") == rec.get("recommendation") else ""
        t.append(f"  {o.get('n')}. {o.get('label')}{mark}\n")
    if rec.get("facts"):
        t.append("\nfacts:\n", style="bold")
        for f in rec["facts"]:
            t.append(f"  - {f}\n")
    park = rec.get("park")
    if park:
        t.append(f"\npark: {park.get('escalated_from') or '(none)'} → "
                 f"{park.get('resume_to')} ({park.get('mode')})\n")
    n = rec.get("notified")
    if n:
        t.append("\nnotified via " + str(n.get("via")) + (" ok" if n.get("ok") else " failed")
                 + (f" {n['error']}" if n.get("error") else "") + "\n", style="dim")
    t.append(f"reminded {rec.get('reminded_at') or 'never'}\n", style="dim")
    status = rec.get("status")
    if status == "answered":
        t.append(f"\nanswered by {rec.get('answered_by')} at {rec.get('answered_at')}\n",
                 style="bold green")
        t.append(f"{ESC.answer_summary(rec)}\n")
    elif status == "withdrawn":
        w = rec.get("withdrawn") or {}
        t.append(f"\nwithdrawn by {w.get('by')}: {w.get('reason')}\n", style="bold")
    elif status == "open" and rec.get("campaign"):
        _override_block(cfg, t, rec)
    return t
