"""View model for the TUI queue tab (SPEC-0241). No Textual import.

Turns `queue.dashboard_payload` and `events.followers` into rows and a header line, computes a
`stat()`-only change signature, and wraps the four human queue actions. Read-only except
`queue_action`, which goes through the same `plan_*` / `apply_*` functions as the CLI.
"""
from dataclasses import dataclass

from ..agentic import events as EV
from ..agentic import lease as L
from ..agentic import queue as Q
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
