"""`wt queue conductor-brief` (SPEC-0301): what a fresh or compacted conductor needs to carry on,
in a few thousand tokens, built only from files. Every list has a fixed cap (`more` counts the
cut items); the text form is bounded by `TEXT_LIMIT` and drops the least important section
first (`SECTION_ORDER`)."""
from __future__ import annotations

import datetime as dt

from . import conductor_cost as CC
from . import decisions as D
from . import escalations as ESC
from . import events as E
from . import queue as Q
from .. import skills as SK

SCHEMA = "wt.conductor.brief.v1"
TEXT_LIMIT = 12000
COMPACT_AT_DEFAULT = 150000
CAPS = {"parked": 20, "active": 20, "open_dispatches": 10, "escalations_open": 10,
        "unanswered_mail": 10, "recent_decisions": 10}
# Highest priority first; truncation removes items from the end of this list first.
SECTION_ORDER = ("runner", "conductor", "counts", "parked", "open_dispatches",
                 "escalations_open", "unanswered_mail", "active", "recent_decisions", "standing")


def _clip(text, n) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _conductor_conf(cfg) -> dict:
    return ((cfg.get("agentic") or {}).get("conductor")) or {}


def compact_at(cfg) -> int:
    v = _conductor_conf(cfg).get("compact_at_tokens")
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else COMPACT_AT_DEFAULT


def _str_list(v) -> list[str]:
    return [str(x) for x in v] if isinstance(v, (list, tuple)) else []


def _session(cfg) -> tuple[str | None, int | None]:
    """The newest registered conductor session and the context of its last turn; None where
    SPEC-0299 data is absent."""
    rows = CC.sessions(cfg)
    if not rows:
        return None, None
    sid = rows[-1]["session_id"]
    path = CC.find_transcript(cfg, sid)
    turns = CC.read_turns(path) if path else []
    return sid, (max(turns, key=lambda t: t["ts"])["context"] if turns else None)


def _cap(name, rows) -> tuple[list, int]:
    n = CAPS[name]
    return rows[:n], max(0, len(rows) - n)


def conductor_brief(cfg, now=None) -> dict:
    now = now or dt.datetime.now().astimezone()
    payload = Q.queue_payload(cfg, now=now)
    items = payload["items"]
    lease = payload["lease"]
    records = D._read_all(cfg)
    by_campaign: dict = {}
    for r in records:
        by_campaign.setdefault(r.get("campaign"), []).append(r)
    esc_of = {}
    escalations = ESC.open_live(cfg)
    for e in escalations:
        esc_of.setdefault(e.get("campaign"), e.get("id"))
    try:
        from . import mailbox as M
        mail = M.unanswered(cfg, "conductor")
    except (ImportError, AttributeError):
        mail = []

    parked = []
    for i in items:
        if i["state"] != "PARKED":
            continue
        park = i["park"] or {}
        since = D.parse_ts(park.get("parked_at"))
        last = next((r for r in reversed(by_campaign.get(i["campaign"], []))
                     if since is None or (D.parse_ts(r.get("ts")) or since) >= since), None)
        parked.append({
            "campaign": i["campaign"], "label": i["label"], "reason": park.get("reason"),
            "detail": _clip(park.get("detail"), 200),
            "parked_for_s": Q._age(park.get("parked_at"), now), "owner": park.get("owner"),
            "escalation": esc_of.get(i["campaign"]),
            "last_decision": ({"kind": last["kind"], "rule": last["rule"],
                               "rationale": _clip(last.get("rationale"), 200), "ts": last["ts"]}
                              if last else None),
            "decided": bool(park.get("decided")), "overdue": bool(park.get("overdue")),
            "auto": ({"rule": park["auto"].get("rule"), "resume_at": park["auto"].get("resume_at")}
                     if isinstance(park.get("auto"), dict) else None)})
    active = [{"campaign": i["campaign"], "label": i["label"], "state": i["state"],
               "loops": i["loops"], "cost_usd": i["cost_usd"]}
              for i in items if i["state"] in Q.ACTIVE_STATES]
    dispatches = [{"seq": r["seq"], "ts": r["ts"], "campaign": r.get("campaign"),
                   "rule": r["rule"], "action": _clip(r.get("action") or r.get("rationale"), 200)}
                  for r in D.open_dispatches(records)]
    recent = [{"seq": r["seq"], "ts": r["ts"], "campaign": r.get("campaign"), "kind": r["kind"],
               "rule": r["rule"], "rationale": _clip(r.get("rationale"), 200)}
              for r in reversed(records[-CAPS["recent_decisions"]:])]
    escs = [{"id": e.get("id"), "campaign": e.get("campaign"),
             "question": _clip(e.get("question"), 160)} for e in escalations]
    mail_rows = [{"id": m.get("id"), "from": m.get("from"), "kind": m.get("kind"),
                  "text": _clip(m.get("text"), 160)} for m in mail]

    more = {}
    out_lists = {}
    for name, rows in (("parked", parked), ("active", active), ("open_dispatches", dispatches),
                       ("escalations_open", escs), ("unanswered_mail", mail_rows)):
        out_lists[name], more[name] = _cap(name, rows)
    more["recent_decisions"] = max(0, len(records) - CAPS["recent_decisions"])

    sid, context = _session(cfg)
    limit = compact_at(cfg)
    conf = _conductor_conf(cfg)
    draining = lease.get("draining")
    return {
        "schema": SCHEMA,
        "runner": {"held": lease["held"], "pid": lease["pid"],
                   "heartbeat_age_s": lease["heartbeat_age_s"],
                   "draining": ((draining.get("reason") if isinstance(draining, dict) else draining)
                                or None)},
        "conductor": {"pid": E.reader_holder(cfg, "conductor"), "session_id": sid,
                      "context_tokens": context, "compact_at": limit,
                      "compact_now": context is not None and context > limit},
        "counts": {"active": len(active), "queued": sum(1 for i in items if i["state"] == "QUEUED"),
                   "parked": len(parked)},
        **out_lists,
        "recent_decisions": recent,
        "standing": {"skill": str(SK.default_dest() / "wt-conduct" / "SKILL.md"),
                     "memory_paths": _str_list(conf.get("memory_paths")),
                     "standing_rules": _str_list(conf.get("standing_rules"))},
        "more": {k: v for k, v in more.items() if v},
    }


# ---- text form -----------------------------------------------------------------------------

def _sections(b) -> dict:
    """Section name -> (header line, item lines, items cut by the caps)."""
    r, c, n = b["runner"], b["conductor"], b["counts"]
    more = b.get("more") or {}
    s = {
        "runner": (f"runner: {'held pid ' + str(r['pid']) if r['held'] else 'NOT RUNNING'}"
                   f", heartbeat {r['heartbeat_age_s']}s, draining {r['draining']}", [], 0),
        "conductor": (f"conductor: pid {c['pid']} session {c['session_id']} context "
                      f"{c['context_tokens']} / compact_at {c['compact_at']} "
                      f"compact_now={str(c['compact_now']).lower()}", [], 0),
        "counts": (f"counts: active {n['active']} queued {n['queued']} parked {n['parked']}", [], 0),
    }
    s["parked"] = ("parked:", [
        f"- {p['campaign']} {p['label']} {p['reason']} owner={p['owner']} "
        f"for={p['parked_for_s']}s decided={str(p['decided']).lower()} "
        f"overdue={str(p['overdue']).lower()} esc={p['escalation']}"
        + (f" auto={p['auto']['rule']}@{p['auto']['resume_at']}" if p.get("auto") else "")
        + f" | {p['detail']} | last: "
        + (f"{p['last_decision']['kind']}/{p['last_decision']['rule']} "
           f"{p['last_decision']['ts']} {p['last_decision']['rationale']}"
           if p["last_decision"] else "none") for p in b["parked"]], more.get("parked", 0))
    s["open_dispatches"] = ("open dispatches:", [
        f"- #{d['seq']} {d['campaign'] or '-'} {d['rule']} {d['action']}"
        for d in b["open_dispatches"]], more.get("open_dispatches", 0))
    s["escalations_open"] = ("open escalations:", [
        f"- {e['id']} {e['campaign'] or '-'} {e['question']}"
        for e in b["escalations_open"]], more.get("escalations_open", 0))
    s["unanswered_mail"] = ("unanswered mail:", [
        f"- {m['id']} from {m['from']} ({m['kind']}) {m['text']}"
        for m in b["unanswered_mail"]], more.get("unanswered_mail", 0))
    s["active"] = ("active:", [
        f"- {a['campaign']} {a['label']} {a['state']} loops {a['loops']} ${a['cost_usd']}"
        for a in b["active"]], more.get("active", 0))
    s["recent_decisions"] = ("recent decisions:", [
        f"- #{d['seq']} {d['ts']} {d['campaign'] or '-'} {d['kind']}/{d['rule']} {d['rationale']}"
        for d in b["recent_decisions"]], more.get("recent_decisions", 0))
    st = b["standing"]
    s["standing"] = ("standing:", [f"- skill {st['skill']}"]
                     + [f"- memory {p}" for p in st["memory_paths"]]
                     + [f"- rule {x}" for x in st["standing_rules"]], 0)
    return s


def render_text(brief, limit=TEXT_LIMIT) -> str:
    """The brief as text of at most `limit` characters. Items are removed from the lowest-
    priority section first (the first parked item is kept to the end); a section that lost items
    ends with `+N more`."""
    secs = _sections(brief)
    keep = {k: len(v[1]) for k, v in secs.items()}

    def build() -> str:
        lines = []
        for name in SECTION_ORDER:
            head, rows, capped = secs[name]
            if not rows and not capped and name not in ("runner", "conductor", "counts"):
                continue
            lines.append(head)
            lines.extend(rows[:keep[name]])
            cut = len(rows) - keep[name] + capped
            if cut:
                lines.append(f"  +{cut} more")
        return "\n".join(lines)

    text = build()
    while len(text) > limit:
        floor = {"parked": 1}
        name = next((n for n in reversed(SECTION_ORDER) if keep[n] > floor.get(n, 0)), None)
        if name is None:
            return text[:limit - 1] + "…"
        keep[name] -= 1
        text = build()
    return text
