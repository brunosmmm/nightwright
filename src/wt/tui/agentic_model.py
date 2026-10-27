"""View model for the TUI queue tab (SPEC-0241) and campaign screen (SPEC-0246). No Textual
import.

Turns `queue.dashboard_payload` and `events.followers` into rows and a header line, computes a
`stat()`-only change signature, and wraps the four human queue actions. `load_campaign` builds
one campaign's header, ledger timeline and review panel. Read-only except `queue_action`, which
goes through the same `plan_*` / `apply_*` functions as the CLI.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

from rich.text import Text

from .. import render as RD
from ..agentic import events as EV
from ..agentic import lease as L
from ..agentic import profiles as PR
from ..agentic import queue as Q
from ..agentic import runs as RN
from ..agentic import spec_review as SR
from ..agentic import store
from ..agentic.cli_queue import _ago, _dur

ACTIONS = ("park", "resume", "prioritize", "abandon")
EMPTY_META = "queue is empty"


@dataclass(frozen=True)
class QueueRow:
    section: str
    campaign: str
    spec: str
    title: str
    project: str
    state: str
    meta: str
    actions: tuple = ()
    priority: int | None = None


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


def _park_meta(it, *, overdue=False) -> str:
    park = it.get("park") or {}
    reason = park.get("reason") or "park requested"
    detail = park.get("detail") or (park.get("decision") or {}).get("text")
    bits = [str(reason), _ago(it.get("park_age_s"))]
    if _first_line(detail):
        bits.append(_first_line(detail))
    return " · ".join(bits) + (" · overdue" if overdue else "")


def _active_meta(it) -> str:
    act = it.get("activity") or {}
    if act.get("kind") == "run":
        head = f"{it['state']} · {act.get('persona')} {_dur(act.get('elapsed_s'))}"
    elif act.get("kind") == "gates":
        head = f"{it['state']} · gates"
    else:
        head = it["state"]
    return f"{head} · loops {it.get('loops')} · ${float(it.get('cost_usd') or 0):.2f}"


def _backlog_meta(it) -> str:
    bits = [f"#{it.get('position')} p{it.get('priority')}"]
    blocked = it.get("blocked_by") or []
    bits.append("ready" if it.get("ready", True) and not blocked
                else f"blocked by {', '.join(str(b) for b in blocked)}")
    if it.get("size"):
        bits.append(f"{it['size']}/{it.get('tone')}")
    return " · ".join(bits)


def _delivered_meta(it) -> str:
    ref = (it.get("merged_sha") or "")[:7] or it.get("pr_url") or ""
    head = f"{it.get('state')} {ref}".strip()
    return f"{head} · ${float(it.get('cost_usd') or 0):.2f} · {_ago(it.get('finished_age_s'))} ago"


def _row(section, it, meta, state=None) -> QueueRow:
    state = state or it.get("state") or ""
    return QueueRow(
        section=section, campaign=it["campaign"],
        spec=str(it.get("spec_id") or it.get("idea_id") or ""),
        title=it.get("spec_title") or "", project=str(it.get("project") or ""),
        state=state, meta=meta,
        actions=allowed_actions(state, bool(it.get("park_requested"))),
        priority=it.get("priority"))


def rows_from_payload(payload) -> list[QueueRow]:
    rows = []
    for it in payload["needs_you"]:
        rows.append(_row("needs you", it, _park_meta(it)))
    for it in payload["conductor_handling"]:
        rows.append(_row("conductor", it, _park_meta(it, overdue=(it.get("park") or {}).get("overdue"))))
    for it in payload["active"]:
        rows.append(_row("active", it, _active_meta(it)))
    for it in payload["waiting"]:
        rows.append(_row("waiting", it, f"waiting {_ago(it.get('waiting_s'))} → {it.get('resume_to')}"))
    for it in payload["backlog"]:
        rows.append(_row("backlog", it, _backlog_meta(it), "QUEUED"))
    for it in payload["delivered"]:
        rows.append(_row("delivered", it, _delivered_meta(it)))
    if not rows:
        rows.append(QueueRow("", "", "", "", "", "", EMPTY_META))
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
    payload = Q.dashboard_payload(cfg, project=project)
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
    if root.is_dir():
        for d in root.iterdir():
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
                     "admitted_after_revision"), "transitions"),
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
    entries = [_entry(ev) for ev in store.read_ledger(cfg, cid)]
    entries.sort(key=lambda e: e.seq)
    return CampaignDetail(
        campaign=cid, state=state, header=header_lines(cfg, state), entries=entries,
        review=review_text(cfg, state),
        actions=allowed_actions(state.get("state"), bool(state.get("park_requested"))),
        signature=signature)


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
