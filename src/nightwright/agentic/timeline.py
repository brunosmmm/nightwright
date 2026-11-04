"""The campaign *story* (SPEC-0433): a human-readable, UI-independent view of a campaign ledger.

A ledger is one row per event (`run.start`, `run.end`, `cost`, `finding`, `gate`, ...). The
story groups those events by loop, then by phase visit, and collapses each persona run (its
start, end, cost, findings, triage and verdict) into one line, each gate (and the reuse/slot
noise around it) into one line, each review fan-out into one "review round" line, and each
rebase (conflict finding, resolver run, `rebase_resolved`) into one line. Transitions become
the group header's state path. Every line keeps the `seqs` of the raw events it stands for, so
a UI can always show the raw key=value view of any line.

Pure: it reads nothing but the events (and an optional state dict) it is given, writes
nothing, and imports only the standard library. `story(events)` returns `nw.campaign.story.v1`
(a JSON-ready dict); `text_lines(doc)` renders it as indented plain lines for a terminal.
The TUI campaign screen, the dashboard drilldown and `nw campaign show` all use it, and the
SPEC-0406 history layer (`history.py`) is to build on its phase table and loop predicate
rather than duplicate them.
"""
from __future__ import annotations

import ast
import re
from datetime import datetime

SCHEMA = "nw.campaign.story.v1"

# The phase of each campaign state. The coarse phases are SPEC-0405's `history.PHASES`
# (queue/spec/implement/gate/review/close/parked/waiting); `done` is the terminal visit.
PHASES = {
    "queue": ("QUEUED", "OPENING"),
    "spec": ("SPEC_WRITING", "SPEC_REVIEW"),
    "implement": ("IMPLEMENTING",),
    "gate": ("GATING", "TRIAGING"),
    "review": ("REVIEWING",),
    "close": ("CLOSING",),
    "parked": ("PARKED",),
    "waiting": ("WAITING",),
    "done": ("MERGED", "HANDED_OFF", "ABANDONED"),
}
PHASE_OF = {s: p for p, states in PHASES.items() for s in states}
PHASE_TITLE = {"queue": "Queued", "spec": "Spec writing", "implement": "Implementing",
               "gate": "Gating", "review": "Reviewing", "close": "Closing", "parked": "Parked",
               "waiting": "Waiting", "done": "Closed", "other": "Other"}
TERMINAL = frozenset(PHASES["done"])
# Equal to `campaign_export.FIX_REASONS` (a test pins it): a transition to IMPLEMENTING with
# one of these reasons starts a new loop.
FIX_REASONS = frozenset({"triage_fix", "review_fix"})
SEVERITIES = ("must_fix", "major", "minor", "nit", "info")
# Line categories: the TUI's SPEC-0246 filter groups, kept so `f` still means something.
CATEGORIES = ("transitions", "runs", "gates", "findings", "notes")

_GATE_NOISE = frozenset({"gate.reuse_declined", "gate.reused", "gate.skipped", "gate_waiting",
                         "worker_failed", "worker_job_lost", "worker_job_retried"})
_QUIET_TYPES = frozenset({"idea_link_skipped", "idea_linked", "idea_unlinked", "idea_reconciled",
                          "outbound_pulled", "backlog"})
_SKIP_KEYS = frozenset({"schema", "ts", "seq", "campaign", "type", "by"})
_WAIVED_NOTE = re.compile(r"^waived (\S+?): (.*)$", re.S)
_SCORE_NOTE = re.compile(r"^review score (\d+)/(\d+) (\w+)")
_DISPOSITION = {"retry_gate": "retry", "fix": "fix", "waive": "waive", "escalate": "escalate",
                "flaky": "flaky"}


def is_fix_loop(ev: dict) -> bool:
    return (ev.get("type") == "transition" and ev.get("to") == "IMPLEMENTING"
            and ev.get("reason") in FIX_REASONS)


# --- small formatting helpers -------------------------------------------------------------

def fmt_duration(s) -> str:
    try:
        s = int(round(float(s)))
    except (TypeError, ValueError):
        return "?"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, r = divmod(s, 60)
        return f"{m}m {r}s" if r else f"{m}m"
    h, r = divmod(s, 3600)
    m = r // 60
    return f"{h}h {m}m" if m else f"{h}h"


def fmt_usd(v) -> str:
    try:
        return f"${float(v):.2f}"
    except (TypeError, ValueError):
        return "$?"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts(v):
    try:
        return datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


def _secs(a, b):
    ta, tb = _ts(a), _ts(b)
    if ta is None or tb is None:
        return None
    try:
        return max(0.0, (tb - ta).total_seconds())
    except TypeError:           # naive vs aware
        return None


def _clip(text, n=100) -> str:
    text = " ".join(str(text if text is not None else "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _dict(v) -> dict:
    """Ledger payloads are dicts; tolerate a repr string from very old writers."""
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.startswith("{"):
        try:
            got = ast.literal_eval(v)
            return got if isinstance(got, dict) else {}
        except (ValueError, SyntaxError):
            return {}
    return {}


def _list(v) -> list:
    return v if isinstance(v, list) else []


def persona_title(p) -> str:
    p = str(p or "run")
    return p.replace("_", " ").capitalize()


def _reviewer_short(p) -> str:
    short = str(p or "").removesuffix("_reviewer")
    return {"test": "tests"}.get(short, short) or "reviewer"


def is_reviewer(p) -> bool:
    return str(p or "").endswith("_reviewer") and p != "spec_reviewer"


def raw_text(ev: dict, width: int = 60) -> str:
    """One event as `type by k=v k=v` (values clipped): the fallback and the raw view."""
    parts = []
    for k, v in ev.items():
        if k in _SKIP_KEYS:
            continue
        txt = v if isinstance(v, str) else str(v)
        txt = " ".join(txt.split())
        if len(txt) > width:
            txt = txt[: width - 3] + "..."
        parts.append(f"{k}={txt}")
    return " ".join(parts)


def findings_summary(children) -> str:
    """`3 findings (1 major, 2 minor; 1 waived by triager)` over finding children, or ''."""
    fs = [c for c in children if c.get("severity")]
    if not fs:
        return ""
    by = {}
    for f in fs:
        by[f["severity"]] = by.get(f["severity"], 0) + 1
    sev = ", ".join(f"{by[s]} {s}" for s in SEVERITIES if by.get(s))
    others = ", ".join(f"{n} {s}" for s, n in by.items() if s not in SEVERITIES)
    waived = sum(1 for f in fs if f.get("waived"))
    inner = ", ".join(x for x in (sev, others) if x)
    if waived:
        inner += f"; {waived} waived by triager"
    return f"{len(fs)} finding{'s' if len(fs) != 1 else ''} ({inner})"


# --- the builder ---------------------------------------------------------------------------

class _Builder:
    def __init__(self, campaign, state, now):
        self.campaign = campaign
        self.state = state or {}
        self.now = now
        self.loops: list[dict] = []
        self.group: dict | None = None
        self.loop = 0
        self.cur = "QUEUED"
        self.total = 0.0
        self.cap = None
        self.base = None
        self.runs: dict[str, dict] = {}       # run id -> line (or round member)
        self.findings_by_key: dict[str, dict] = {}
        self.unknown: dict[str, int] = {}
        self.last_ts = None
        self.n = 0
        self._last_ended = None
        self._last_ended_line = None
        self.total_known = False

    # --- groups --------------------------------------------------------------------------

    def _new_group(self, ts, state):
        phase = PHASE_OF.get(state, "other")
        if not self.loops or self.loops[-1]["loop"] != self.loop:
            self.loops.append({"id": f"loop{self.loop}", "loop": self.loop,
                               "title": f"Loop {self.loop + 1}", "groups": []})
        self.group = {"id": None, "phase": phase, "loop": self.loop, "states": [state],
                      "transitions": [], "start": ts, "end": None,
                      "cost_from": round(self.total, 4), "cost_to": None, "cap_usd": self.cap,
                      "seqs": [], "lines": [], "_pending_noise": [], "_pending_decisions": [],
                      "_round": None, "_rebase": None, "_followups": None, "_backlog": None}
        self.loops[-1]["groups"].append(self.group)

    def _close_group(self, ts):
        g = self.group
        if g is None:
            return
        for ev in g.pop("_pending_noise"):
            self._line(self._noise_text(ev), "gates", [ev["seq"]], quiet=True, kind="gate_note")
        for ev, text in g.pop("_pending_decisions"):
            self._line(text, "transitions", [ev["seq"]], kind="decision")
        for k in ("_round", "_rebase", "_followups", "_backlog"):
            g.pop(k, None)
        g["end"] = ts
        g["cost_to"] = round(self.total, 4)
        g["cap_usd"] = self.cap if self.cap is not None else g["cap_usd"]
        g["id"] = f"g{g['seqs'][0]}" if g["seqs"] else f"g-{len(self.loops)}-{g['start']}"
        self.group = None

    def _line(self, text, category, seqs, *, kind="event", tone="", quiet=False, ref=None,
              children=None, **extra) -> dict:
        line = {"id": f"l{seqs[0]}" if seqs else f"l-{self.n}", "kind": kind,
                "category": category, "text": text, "tone": tone, "quiet": quiet,
                "seqs": list(seqs), "ref_seq": ref if ref is not None else (seqs[0] if seqs else None),
                "children": children or []}
        line.update(extra)
        self.group["lines"].append(line)
        return line

    # --- walk ----------------------------------------------------------------------------

    def feed(self, ev: dict) -> None:
        self.n += 1
        seq = ev.get("seq")
        ts = ev.get("ts")
        self.last_ts = ts or self.last_ts
        if self.group is None:
            self._new_group(ts, self.cur)
        self.group["seqs"].append(seq)
        etype = str(ev.get("type") or "?")
        handler = getattr(self, "_ev_" + etype.replace(".", "_"), None)
        if etype in _GATE_NOISE:
            self.group["_pending_noise"].append(ev)
            return
        if etype != "backlog":
            self.group["_backlog"] = None
        if etype != "followup":
            self.group["_followups"] = None
        if handler is None:
            self.unknown[etype] = self.unknown.get(etype, 0) + 1
            self._line(f"{etype} ({ev.get('by') or '?'}): {_clip(raw_text(ev), 160)}", "notes",
                       [seq], kind="event")
            return
        handler(ev)

    def finish(self) -> None:
        live = self.cur not in TERMINAL
        end = self.now if (live and self.now) else self.last_ts
        self._close_group(end)
        for lp in self.loops:
            gs = lp["groups"]
            lp["start"] = gs[0]["start"] if gs else None
            lp["end"] = gs[-1]["end"] if gs else None
            lp["wall_s"] = _secs(lp["start"], lp["end"])
            lp["cost_from"] = gs[0]["cost_from"] if gs else None
            lp["cost_to"] = gs[-1]["cost_to"] if gs else None
            lp["cap_usd"] = gs[-1]["cap_usd"] if gs else None
            lp["phases"] = [g["phase"] for g in gs]
            lp["header"] = loop_header(lp)
            for g in gs:
                g["wall_s"] = _secs(g["start"], g["end"])
                for line in g["lines"]:
                    self._finalize(line)
                g["path"] = " → ".join(g["states"])
                g["title"] = group_title(g)
                g["header"] = group_header(g)

    # --- transitions and parks -------------------------------------------------------------

    def _ev_transition(self, ev):
        g = self.group
        to = ev.get("to") or "?"
        g["transitions"].append({"seq": ev.get("seq"), "from": ev.get("from"), "to": to,
                                 "reason": ev.get("reason"), "by": ev.get("by"),
                                 "ts": ev.get("ts")})
        g["states"].append(to)
        new_loop = is_fix_loop(ev)
        self.cur = to
        if new_loop or PHASE_OF.get(to, "other") != g["phase"]:
            self._close_group(ev.get("ts"))
            if new_loop:
                self.loop += 1
            self._new_group(ev.get("ts"), to)

    def _ev_open(self, ev):
        self.base = ev.get("base") or self.base
        idea = ev.get("idea") if isinstance(ev.get("idea"), dict) else {}
        bits = [ev.get("spec_id") or idea.get("id") or ev.get("merge_branch"), ev.get("project"),
                ev.get("profile") and f"profile {ev.get('profile')}",
                ev.get("size") and f"size {ev.get('size')}",
                ev.get("kind") and f"kind {ev.get('kind')}"]
        self._line(f"Opened by {ev.get('by') or '?'}: " + " · ".join(str(b) for b in bits if b),
                   "transitions", [ev["seq"]], kind="open")

    def _decisions_into(self, line):
        g = self.group
        for dev, text in g["_pending_decisions"]:
            line["seqs"].append(dev["seq"])
            line["children"].append({"text": text, "tone": "", "seqs": [dev["seq"]]})
        g["_pending_decisions"] = []

    def _ev_park(self, ev):
        reason = ev.get("reason") or "?"
        escal = reason.endswith("_escalated") or bool(ev.get("escalated_from")) \
            or reason == "human_decision"
        kind = "escalation" if escal else "park"
        frm = f" (from {ev['escalated_from']})" if ev.get("escalated_from") else ""
        text = f"Parked by {ev.get('by') or '?'}: {reason}{frm} — {_clip(ev.get('detail'), 140)}"
        if ev.get("resume_to"):
            text += f" (resume to {ev['resume_to']})"
        line = self._line(text, "transitions", [ev["seq"]], kind=kind,
                          tone="bad" if escal else "warn",
                          children=[{"text": f"detail: {ev.get('detail')}", "tone": "",
                                     "seqs": [ev["seq"]]}] if len(str(ev.get("detail") or "")) > 140 else [])
        self._decisions_into(line)

    def _ev_resume(self, ev):
        text = f"Resumed by {ev.get('by') or '?'} → {ev.get('to') or '?'}"
        if ev.get("cap_usd") is not None:
            text += f", cap {fmt_usd(ev['cap_usd'])}"
            self.cap = _num(ev["cap_usd"]) or self.cap
        if ev.get("loops_reset"):
            text += ", loops reset"
        if ev.get("note"):
            text += f": {_clip(ev['note'], 140)}"
        line = self._line(text, "transitions", [ev["seq"]], kind="resume", tone="ok")
        self._decisions_into(line)

    def _ev_human_decision(self, ev):
        what = ev.get("label") or ev.get("text") or ev.get("choice")
        text = f"Human answered {ev.get('escalation') or 'escalation'} ({ev.get('kind') or '?'}): " \
               f"{_clip(what, 140)}"
        kids = [{"text": f"{k}: {ev[k]}", "tone": "", "seqs": [ev["seq"]]}
                for k in ("note", "reason", "waive", "attest") if ev.get(k)]
        self._line(text, "transitions", [ev["seq"]], kind="answer", tone="ok", children=kids)

    def _ev_cap_raised(self, ev):
        self.cap = _num(ev.get("to")) or self.cap
        why = ev.get("why") or ""
        short = why.split(";")[0] if why else ""
        line = self._line(f"Cost cap raised {fmt_usd(ev.get('from'))} → {fmt_usd(ev.get('to'))}"
                          + (f" ({short})" if short else "")
                          + (f" by {ev.get('rule')}" if ev.get("rule") else ""),
                          "transitions", [ev["seq"]], kind="cost_raise", tone="warn")
        self._decisions_into(line)

    def _ev_priority(self, ev):
        self._line(f"Priority {ev.get('from')} → {ev.get('to')} ({ev.get('by') or '?'})",
                   "transitions", [ev["seq"]], kind="priority", quiet=True)

    def _ev_close(self, ev):
        mode = ev.get("mode") or "?"
        if mode == "merge":
            text = f"Merged {str(ev.get('merged_sha') or '?')[:8]}"
            if ev.get("target_sha"):
                text += f" onto {str(ev['target_sha'])[:8]}"
            if ev.get("gates_seq") is not None:
                text += f" (gate #{ev['gates_seq']})"
            tone = "ok"
        elif mode == "abandoned":
            text, tone = f"Abandoned by {ev.get('by') or '?'}", "bad"
        else:
            text = f"Closed ({mode})" + (f": {ev['pr_url']}" if ev.get("pr_url") else "")
            tone = "ok"
        self._line(text, "transitions", [ev["seq"]], kind="close", tone=tone)

    def _ev_backlog(self, ev):
        g = self.group
        line = g["_backlog"]
        if line is None:
            line = self._line("", "notes", [ev["seq"]], kind="backlog", quiet=True, _bl=[])
            g["_backlog"] = line
        else:
            line["seqs"].append(ev["seq"])
        line["_bl"].append(ev)

    def _ev_admitted_revise_pending(self, ev):
        n = len(_list(ev.get("findings")))
        self._line(f"Admitted with the spec review's {n} finding{'s' if n != 1 else ''} pending "
                   f"revision ({ev.get('by') or '?'})", "transitions", [ev["seq"]], kind="admit",
                   children=self._spec_findings(ev))

    def _ev_admitted_after_revision(self, ev):
        n = len(_list(ev.get("findings")))
        self._line(f"Admitted after revision ({n} finding{'s' if n != 1 else ''} addressed, "
                   f"{ev.get('by') or '?'})", "transitions", [ev["seq"]], kind="admit",
                   children=self._spec_findings(ev))

    def _ev_admitted_split(self, ev):
        parts = _list(ev.get("parts"))
        self._line(f"Admitted as a split into {len(parts)} parts ({ev.get('by') or '?'})",
                   "transitions", [ev["seq"]], kind="admit",
                   children=[{"text": _clip((p or {}).get("title") if isinstance(p, dict) else p, 120),
                              "tone": "", "seqs": [ev["seq"]]} for p in parts])

    def _ev_split_finalized(self, ev):
        self._line(f"Split of {ev.get('original')} finalized: "
                   f"{', '.join(str(p) for p in _list(ev.get('parts')))}", "transitions",
                   [ev["seq"]], kind="split")

    def _spec_findings(self, ev):
        out = []
        for f in _list(ev.get("findings")):
            if isinstance(f, dict):
                out.append({"text": f"{f.get('severity') or '?'} · {_clip(f.get('title'), 120)}",
                            "severity": f.get("severity"), "tone": "", "seqs": [ev["seq"]]})
        return out

    # --- quiet bookkeeping events ----------------------------------------------------------

    def _quiet(self, ev, text):
        self._line(text, "notes", [ev["seq"]], kind="bookkeeping", quiet=True)

    def _ev_idea_link_skipped(self, ev):
        self._quiet(ev, f"Idea link skipped for {ev.get('spec_id')}: {ev.get('reason')}")

    def _ev_idea_linked(self, ev):
        self._quiet(ev, f"Linked idea {ev.get('idea_id')}")

    def _ev_idea_unlinked(self, ev):
        self._quiet(ev, f"Unlinked idea {ev.get('idea_id')}")

    def _ev_idea_reconciled(self, ev):
        self._quiet(ev, f"Idea {ev.get('idea_id')}: {ev.get('from')} → {ev.get('to')}")

    def _ev_outbound_pulled(self, ev):
        self._quiet(ev, f"Outbox {ev.get('spec_id')} → {ev.get('status')}; idea "
                        f"{ev.get('idea_id')}: {ev.get('from')} → {ev.get('to')}")

    def _ev_idea_handed_off(self, ev):
        self._line(f"Idea {ev.get('idea_id')} handed off → {ev.get('pr_url')}", "transitions",
                   [ev["seq"]], kind="close", tone="ok")

    def _ev_loop_divergence(self, ev):
        names = [s.get("name") for s in _list(ev.get("signals")) if isinstance(s, dict)]
        self._line(f"Loop divergence at loop {ev.get('loop')} over {ev.get('rounds')} rounds: "
                   f"{', '.join(str(n) for n in names) or '?'}", "notes", [ev["seq"]],
                   kind="divergence", tone="warn")

    def _ev_followup(self, ev):
        g = self.group
        line = g["_followups"]
        if line is None:
            line = self._line("", "findings", [ev["seq"]], kind="followups", _ideas=[])
            g["_followups"] = line
        else:
            line["seqs"].append(ev["seq"])
        if ev.get("idea_id") and ev["idea_id"] not in line["_ideas"]:
            line["_ideas"].append(ev["idea_id"])
        origin = f" ({ev['origin']})" if ev.get("origin") else ""
        line["children"].append({"text": f"{ev.get('severity') or '?'} · "
                                         f"{_clip(ev.get('title'), 110)}{origin}",
                                 "tone": "", "seqs": [ev["seq"]]})

    def _ev_flaky_recorded(self, ev):
        ids = [str(i) for i in _list(ev.get("ids"))]
        gate = self._last_line("gate")
        kid = {"text": f"flaky ({ev.get('source') or '?'}): {', '.join(ids)}", "tone": "warn",
               "seqs": [ev["seq"]]}
        if gate is not None:
            gate["seqs"].append(ev["seq"])
            gate["children"].append(kid)
            gate["_flaky"] = gate.get("_flaky", 0) + len(ids)
        else:
            self._line(f"Recorded {len(ids)} flaky test{'s' if len(ids) != 1 else ''}", "gates",
                       [ev["seq"]], kind="flaky", children=[kid])

    def _ev_commit(self, ev):
        kid = {"text": f"commit {str(ev.get('sha') or '?')[:8]}: {_clip(ev.get('message'), 100)}",
               "tone": "", "seqs": [ev["seq"]]}
        run = self._last_line("run", ended=True)
        if run is not None:
            run["seqs"].append(ev["seq"])
            run["children"].append(kid)
        else:
            self._line(kid["text"].capitalize(), "notes", [ev["seq"]], kind="commit", quiet=True)

    def _ev_note(self, ev):
        text = str(ev.get("text") or "")
        g = self.group
        m = _WAIVED_NOTE.match(text)
        if m:
            target = self._last_line("run", persona_like="triager", ended=True) or \
                self._last_line("review_round")
            if target is not None:
                target["seqs"].append(ev["seq"])
                self._mark_waived(m.group(1), m.group(2), ev["seq"])
                return
        m = _SCORE_NOTE.match(text)
        if m:
            rnd = self._last_line("review_round")
            if rnd is not None:
                rnd["seqs"].append(ev["seq"])
                rnd.setdefault("_outcome", {"score": int(m.group(1)),
                                            "pass_score": int(m.group(2)),
                                            "outcome": m.group(3)})
                return
        if ev.get("decision") is not None:
            d = _dict(ev.get("decision"))
            who = ev.get("author") or str(ev.get("by") or "?").removeprefix("agent:")
            body = text if text.startswith("decision") else f"decision {d.get('kind') or '?'}: {text}"
            g["_pending_decisions"].append((ev, f"{who.capitalize()} {_clip(body, 160)}"))
            return
        author = ev.get("author") or ev.get("by") or "?"
        self._line(f"Note ({author}): {_clip(text, 160)}", "notes", [ev["seq"]], kind="note",
                   children=[{"text": text, "tone": "", "seqs": [ev["seq"]]}]
                   if len(text) > 160 else [])

    # --- spec writing ----------------------------------------------------------------------

    def _ev_spec_written(self, ev):
        run = self._last_line("run", persona_like="spec_writer")
        if run is not None:
            run["seqs"].append(ev["seq"])
            run["_spec"] = ev
            return
        self._line(f"Spec {ev.get('spec_id')} written ({ev.get('mode') or 'new'}): "
                   f"{_clip(ev.get('title'), 100)}", "runs", [ev["seq"]], kind="spec")

    def _ev_spec_review(self, ev):
        run = self.runs.get(str(ev.get("run_id")))
        if run is not None:
            run["seqs"].append(ev["seq"])
            run["_spec_review"] = ev
            return
        self._line(f"Spec review round {ev.get('round')}: {ev.get('verdict')}"
                   + _counts(_dict(ev.get("counts"))), "runs", [ev["seq"]], kind="spec_review")

    def _ev_spec_accepted(self, ev):
        self._line(f"Spec {ev.get('spec_id') or ''} accepted (round {ev.get('round')})".replace("  ", " "),
                   "transitions", [ev["seq"]], kind="spec_accepted", tone="ok")

    # --- runs ------------------------------------------------------------------------------

    def _last_line(self, kind, *, persona_like=None, ended=False):
        g = self.group
        for line in reversed(g["lines"]):
            if line["kind"] != kind:
                continue
            if persona_like and persona_like not in str(line.get("persona") or ""):
                continue
            if ended and not line.get("_end"):
                continue
            return line
        return None

    def _ev_run_start(self, ev):
        g = self.group
        rid = str(ev.get("run_id") or f"?{ev['seq']}")
        persona = ev.get("persona")
        member = {"persona": persona, "run_id": rid, "model": ev.get("model"),
                  "_start": ev, "_end": None, "_cost": None, "seqs": [ev["seq"]],
                  "children": []}
        if is_reviewer(persona) and g["phase"] == "review":
            rnd = g["_round"]
            if rnd is None or rnd["_closed"] or any(m["persona"] == persona for m in rnd["_members"]):
                rnd = self._line("", "runs", [], kind="review_round", _members=[], _closed=False)
                g["_round"] = rnd
            rnd["_members"].append(member)
            rnd["seqs"].append(ev["seq"])
            self.runs[rid] = rnd
            member["_round"] = rnd
            self.runs[rid + "#member"] = member
            return
        if persona == "conflict_resolver" and g["_rebase"] is not None and g["_rebase"].get("_run") is None:
            line = g["_rebase"]
            line["seqs"].append(ev["seq"])
            line["_run"] = member
            self.runs[rid] = line
            self.runs[rid + "#member"] = member
            return
        if persona == "conflict_resolver":
            line = self._line("", "runs", [ev["seq"]], kind="rebase", _run=member)
            g["_rebase"] = line
            self.runs[rid] = line
            self.runs[rid + "#member"] = member
            return
        line = self._line("", "runs", [ev["seq"]], kind="run", persona=persona, run_id=rid,
                          model=ev.get("model"), _start=ev, _end=None, _cost=None)
        if g["_round"] is not None:
            g["_round"]["_closed"] = True
        self.runs[rid] = line

    def _member(self, rid):
        return self.runs.get(rid + "#member") or self.runs.get(rid)

    def _ev_run_end(self, ev):
        rid = str(ev.get("run_id") or "")
        line = self.runs.get(rid)
        if line is None:                           # an end with no start (or a pruned start)
            line = self._line("", "runs", [], kind="run", persona=ev.get("persona"), run_id=rid,
                              model=ev.get("model"), _start=None, _end=None, _cost=None)
            self.runs[rid] = line
        line["seqs"].append(ev["seq"])
        m = self._member(rid)
        m["_end"] = ev
        if line["kind"] == "run":
            line["ref_seq"] = ev["seq"]
        self._last_ended = m
        self._last_ended_line = line

    def _ev_cost(self, ev):
        total = _num(ev.get("total_usd"))
        if total is not None and not self.total_known:
            # the first total names what was spent before it (a ledger may not start at $0)
            base = round(total - (_num(ev.get("delta_usd")) or 0.0), 4)
            for lp in self.loops:
                for g in lp["groups"]:
                    g["cost_from"] = base
                    if g is not self.group:
                        g["cost_to"] = base
            self.total_known = True
        self.total = total if total is not None else self.total
        if ev.get("cap_usd") is not None:
            self.cap = _num(ev.get("cap_usd"))
        m, line = self._last_ended, self._last_ended_line
        if m is not None and m["_cost"] is None and m.get("persona") == ev.get("persona"):
            m["_cost"] = ev
            line["seqs"].append(ev["seq"])
            return
        self._line(f"Cost +{fmt_usd(ev.get('delta_usd'))} ({ev.get('persona') or '?'}) → "
                   f"{fmt_usd(ev.get('total_usd'))}", "runs", [ev["seq"]], kind="cost", quiet=True)

    def _finding_child(self, ev, prefix=""):
        sev = ev.get("severity") or "minor"
        files = [str(f) for f in _list(ev.get("files"))]
        text = f"{sev} · {prefix}{_clip(ev.get('title'), 120)}"
        if files:
            text += f" ({', '.join(files[:3])}{'…' if len(files) > 3 else ''})"
        kid = {"text": text, "severity": sev, "tone": "", "seqs": [ev["seq"]],
               "persona": ev.get("persona"), "key": ev.get("key"), "waived": False}
        if ev.get("key"):
            self.findings_by_key[str(ev["key"])] = kid
        return kid

    def _ev_finding(self, ev):
        g = self.group
        rid = ev.get("run_id")
        persona = ev.get("persona")
        target = None
        if rid and str(rid) in self.runs:
            target = self.runs[str(rid)]
        elif ev.get("gate") == "rebase":
            title = str(ev.get("title") or "")
            cur = g["_rebase"]
            if "regenerated" in title:
                self._line("", "runs", [ev["seq"]], kind="rebase", _run=None, _regen=ev)
            elif cur is not None and "dispatched" not in title and not cur.get("_failed"):
                cur["seqs"].append(ev["seq"])
                cur["_failed"] = ev
            else:
                g["_rebase"] = self._line("", "runs", [ev["seq"]], kind="rebase", _run=None,
                                          _conflict=ev)
            return
        else:
            m = self._last_ended
            if m is not None and m.get("persona") == persona and persona:
                target = self.runs.get(str(m.get("run_id")))
        if target is None:
            sev = ev.get("severity") or "info"
            where = ev.get("gate") or persona or "?"
            self._line(f"{str(where).capitalize()}: {_clip(ev.get('title'), 140)}"
                       + (f" ({sev})" if sev != "info" else ""), "findings", [ev["seq"]],
                       kind="finding", tone="warn",
                       children=[{"text": str(ev.get("detail")), "tone": "",
                                  "seqs": [ev["seq"]]}] if ev.get("detail") else [])
            return
        target["seqs"].append(ev["seq"])
        prefix = f"{_reviewer_short(persona)}: " if target["kind"] == "review_round" else ""
        target["children"].append(self._finding_child(ev, prefix))

    def _mark_waived(self, wid, reason, seq):
        """`wid` is `persona#i` (the i-th finding of that persona in the last round) or a key."""
        kid = self.findings_by_key.get(str(wid))
        if kid is None and "#" in str(wid):
            persona, _, idx = str(wid).partition("#")
            rnd = self._last_line("review_round")
            if rnd is not None and idx.isdigit():
                mine = [c for c in rnd["children"] if c.get("persona") == persona]
                if int(idx) < len(mine):
                    kid = mine[int(idx)]
        if kid is not None and not kid.get("_reason_seq"):
            kid["waived"] = True
            kid["text"] += " — waived by triager" + (f": {_clip(reason, 140)}" if reason else "")
            kid["seqs"].append(seq)
            kid["_reason_seq"] = seq

    def _ev_triage(self, ev):
        rid = str(ev.get("run_id") or "")
        line = self.runs.get(rid)
        if line is not None:
            line["seqs"].append(ev["seq"])
            m = self._member(rid)
            m["_triage"] = ev
        for w in _list(ev.get("waive")):
            if not isinstance(w, dict):
                continue
            kid = self.findings_by_key.get(str(w.get("key"))) if w.get("key") else None
            if kid is not None and not kid.get("waived"):
                kid["waived"] = True
                kid["text"] += " — waived by triager" + (
                    f": {_clip(w.get('reason'), 140)}" if w.get("reason") else "")
                kid["_reason_seq"] = ev["seq"]
            elif kid is None and w.get("id"):
                self._mark_waived(w["id"], w.get("reason"), ev["seq"])
        if line is None:
            self._line(f"Triage ({ev.get('kind') or '?'}): {ev.get('disposition')}", "runs",
                       [ev["seq"]], kind="triage")

    def _ev_review_outcome(self, ev):
        rnd = self._last_line("review_round")
        if rnd is None:
            self._line(f"Review outcome: {ev.get('outcome')} {ev.get('score')}/{ev.get('pass_score')}",
                       "runs", [ev["seq"]], kind="review_outcome")
            return
        rnd["seqs"].append(ev["seq"])
        rnd["_outcome"] = {"score": ev.get("score"), "pass_score": ev.get("pass_score"),
                           "outcome": ev.get("outcome")}

    def _ev_rebase_resolved(self, ev):
        rid = str(ev.get("run_id") or "")
        line = self.runs.get(rid)
        if line is None or line["kind"] != "rebase":
            line = self._line("", "runs", [], kind="rebase", _run=None)
        line["seqs"].append(ev["seq"])
        line["_resolved"] = ev

    # --- gates -----------------------------------------------------------------------------

    def _noise_text(self, ev):
        t = ev.get("type")
        if t == "gate.reuse_declined":
            return f"{ev.get('gate')}: reuse declined ({ev.get('rule')}: {_clip(ev.get('detail'), 100)})"
        if t == "gate.reused":
            return (f"{ev.get('gate')}: reused from {ev.get('from_gate')} #{ev.get('from_gate_seq')}"
                    + (f" ({_clip(ev.get('selection_reason'), 80)})" if ev.get("selection_reason") else ""))
        if t == "gate.skipped":
            return f"{ev.get('gate')}: skipped ({ev.get('changed_count')} changed files match none of its patterns)"
        if t == "gate_waiting":
            return f"waited {fmt_duration(ev.get('waited_s'))} for a gate slot ({ev.get('slots')} slots)"
        if t == "worker_failed":
            return f"worker {ev.get('worker')} failed at {ev.get('stage')}: {_clip(ev.get('error'), 100)}"
        if t == "worker_job_lost":
            return f"worker {ev.get('worker')} lost job {ev.get('job_id')} ({ev.get('reason')})"
        if t == "worker_job_retried":
            return f"job retried: attempt {ev.get('attempt')} (from {ev.get('from_worker')})"
        return f"{t}: {_clip(raw_text(ev), 120)}"

    def _ev_gate(self, ev):
        g = self.group
        results = [r for r in _list(ev.get("results")) if isinstance(r, dict)]
        ok = bool(ev.get("ok"))
        label = "Re-gate" if g["phase"] == "close" else "Gate"
        where = f" on {ev['worker']}" if ev.get("worker") else ""
        dur = fmt_duration(ev.get("duration_s"))
        if ok:
            text = f"{label} #{ev.get('gate_seq')}{where} — passed ({dur})"
        else:
            failed = [r for r in results if not r.get("ok") and r.get("required", True)] or \
                     [r for r in results if not r.get("ok")]
            names = ", ".join(str(r.get("name")) for r in failed) or "?"
            text = f"{label} #{ev.get('gate_seq')}{where} — FAILED after {dur} ({names})"
        kids = []
        for r in results:
            mark = "✓" if r.get("ok") else "✗"
            extra = "" if r.get("ok") else f", exit {r.get('exit_code')}"
            opt = "" if r.get("required", True) else ", optional"
            kids.append({"text": f"{mark} {r.get('name')} ({fmt_duration(r.get('duration_s'))}"
                                 f"{extra}{opt})", "tone": "ok" if r.get("ok") else "bad",
                         "seqs": [ev["seq"]]})
        if _num(ev.get("slot_wait_s")):
            kids.append({"text": f"waited {fmt_duration(ev['slot_wait_s'])} for a slot",
                         "tone": "", "seqs": [ev["seq"]]})
        seqs = [ev["seq"]]
        for nev in g["_pending_noise"]:
            seqs.insert(0, nev["seq"])
            kids.append({"text": self._noise_text(nev), "tone": "", "seqs": [nev["seq"]]})
        g["_pending_noise"] = []
        self._line(text, "gates", sorted(seqs), kind="gate", tone="ok" if ok else "bad",
                   ref=ev["seq"], children=kids, gate_seq=ev.get("gate_seq"))

    # --- finalize --------------------------------------------------------------------------

    def _finalize(self, line):
        kind = line["kind"]
        if kind == "run":
            line["text"], line["tone"] = run_text(line)
            _move_findings_first(line)
        elif kind == "review_round":
            line["text"], line["tone"] = round_text(line)
            _move_findings_first(line)
        elif kind == "rebase":
            line["text"], line["tone"] = rebase_text(line, self.base)
        elif kind == "backlog":
            bl = line.pop("_bl", [])
            last = bl[-1] if bl else {}
            blocked = [str(b) for b in _list(last.get("blocked_by"))]
            if last.get("ready"):
                first_block = next((_list(b.get("blocked_by")) for b in bl
                                    if _list(b.get("blocked_by"))), [])
                line["text"] = "Backlog: ready" + (f" (was blocked: {_clip(', '.join(map(str, first_block)), 100)})"
                                                    if first_block else "")
            else:
                line["text"] = f"Backlog: blocked ({_clip(', '.join(blocked), 120) or '?'})"
        elif kind == "followups":
            ideas = line.pop("_ideas", [])
            n = len(line["children"])
            line["text"] = (f"Follow-ups: {n} finding{'s' if n != 1 else ''} filed"
                            + (f" as {', '.join(ideas)}" if ideas else ""))
        # drop private bookkeeping so the doc is JSON-clean
        for k in [k for k in line if k.startswith("_")]:
            line.pop(k)
        for c in line["children"]:
            for k in [k for k in c if k.startswith("_")]:
                c.pop(k)
        line["seqs"] = sorted(set(s for s in line["seqs"] if s is not None))
        if line["ref_seq"] is None and line["seqs"]:
            line["ref_seq"] = line["seqs"][0]
        if line["id"].startswith("l-") and line["seqs"]:
            line["id"] = f"l{line['seqs'][0]}"


def _move_findings_first(line):
    kids = line["children"]
    sev = [c for c in kids if c.get("severity")]
    order = {s: i for i, s in enumerate(SEVERITIES)}
    sev.sort(key=lambda c: (bool(c.get("waived")), order.get(c.get("severity"), 99)))
    line["children"] = sev + [c for c in kids if not c.get("severity")]


def _counts(counts: dict) -> str:
    bits = [f"{counts[s]} {s}" for s in SEVERITIES if counts.get(s)]
    return f" ({', '.join(bits)})" if bits else ""


def _run_head(persona, model, start, end, cost_ev):
    bits = [model] if model else []
    if end is not None:
        bits.append(fmt_duration(end.get("duration_s")))
        charged = end.get("charged_usd")
        if charged is None and cost_ev is not None:
            charged = cost_ev.get("delta_usd")
        if charged is not None:
            bits.append(fmt_usd(charged) + ("~" if end.get("cost_estimated") else ""))
    head = persona_title(persona)
    return head + (f" ({', '.join(bits)})" if bits else "")


def _failure(end) -> str | None:
    if end.get("timed_out"):
        return "timed out"
    if end.get("interrupted"):
        return "interrupted"
    if end.get("is_error"):
        tail = _clip(end.get("result_tail") or f"exit {end.get('exit_code')}", 90)
        return f"error — {tail}"
    return None


def run_text(line) -> tuple[str, str]:
    start, end = line.get("_start"), line.get("_end")
    persona = line.get("persona") or (end or {}).get("persona") or (start or {}).get("persona")
    head = _run_head(persona, line.get("model"), start, end, line.get("_cost"))
    if end is None:
        since = str((start or {}).get("ts") or "")[11:16]
        return f"{head}: running" + (f" since {since}" if since else ""), "info"
    fail = _failure(end)
    verdict = _dict(end.get("verdict"))
    triage = line.get("_triage") or {}
    tone = ""
    if fail:
        out, tone = fail, "bad"
    elif "triager" in str(persona) and (verdict.get("disposition") or triage.get("disposition")
                                        or "waive" in verdict):
        disp = verdict.get("disposition") or triage.get("disposition")
        if disp is None and "waive" in verdict:
            disp = "waive"
        conf = verdict.get("confidence") or triage.get("confidence")
        out = _DISPOSITION.get(disp, str(disp))
        if disp == "waive" or (disp is None and verdict.get("waive")):
            n = len(_list(verdict.get("waive")) or _list(triage.get("waive")))
            ign = len(_list(verdict.get("ignored")) or _list(triage.get("ignored")))
            out = f"waived {n} finding{'s' if n != 1 else ''}" + (f", {ign} ignored" if ign else "")
        elif disp == "retry_gate" and (verdict.get("suspected_flaky") or triage.get("suspected_flaky")):
            out += " — suspected flaky"
        elif disp == "fix":
            nfix = len(_list(verdict.get("must_fix")))
            if nfix:
                out += f" — {nfix} must_fix"
        if conf:
            out += f" ({conf})"
        tone = {"fix": "warn", "escalate": "bad"}.get(disp, "")
    elif persona == "spec_writer":
        spec = line.get("_spec") or {}
        status = verdict.get("status") or "?"
        title = spec.get("title") or verdict.get("title")
        out = f"{status}" + (f" — {spec.get('spec_id') + ' ' if spec.get('spec_id') else ''}"
                             f"{_clip(title, 90)}" if title else "")
    elif line.get("_spec_review") is not None or persona == "spec_reviewer":
        sr = line.get("_spec_review") or {}
        v = sr.get("verdict") or verdict.get("verdict") or verdict.get("status") or "?"
        out = f"{v}" + (f" — round {sr['round']}" if sr.get("round") is not None else "") \
            + _counts(_dict(sr.get("counts")))
        tone = "ok" if v in ("accept", "accepted", "pass") else "warn"
    elif is_reviewer(persona):
        out = f"{verdict.get('verdict') or '?'} {verdict.get('score') if verdict.get('score') is not None else ''}".strip()
        tone = "ok" if verdict.get("verdict") == "pass" else "warn"
    else:
        status = verdict.get("status") or ("ok" if end.get("verdict_ok") else "no verdict")
        summary = verdict.get("summary")
        out = status + (f" — {_clip(summary, 100)}" if summary else "")
        tone = {"done": "ok", "partial": "warn", "blocked": "bad"}.get(status, "")
    fs = findings_summary(line["children"])
    if fs:
        out += f" · {fs}"
    return f"{head}: {out}", tone


def round_text(line) -> tuple[str, str]:
    members = line.get("_members") or []
    starts = [m["_start"].get("ts") for m in members if m.get("_start")]
    ends = [m["_end"].get("ts") for m in members if m.get("_end")]
    done = all(m.get("_end") for m in members)
    wall = _secs(min(starts), max(ends)) if starts and ends else None
    cost = 0.0
    for m in members:
        e = m.get("_end") or {}
        c = _num(e.get("charged_usd"))
        if c is None and m.get("_cost"):
            c = _num(m["_cost"].get("delta_usd"))
        cost += c or 0.0
    bits = []
    if wall is not None:
        bits.append(fmt_duration(wall))
    if ends:
        bits.append(fmt_usd(cost))
    head = f"Review round — {len(members)} reviewer{'s' if len(members) != 1 else ''}" \
        + (f" ({', '.join(bits)})" if bits else "")
    marks = []
    for m in members:
        e = m.get("_end")
        if e is None:
            marks.append(f"… {_reviewer_short(m['persona'])}")
            continue
        fail = _failure(e)
        v = _dict(e.get("verdict"))
        if fail:
            marks.append(f"✗ {_reviewer_short(m['persona'])} {fail.split(' — ')[0]}")
        else:
            ok = v.get("verdict") == "pass"
            score = v.get("score")
            marks.append(f"{'✓' if ok else '✗'} {_reviewer_short(m['persona'])}"
                         + (f" {score}" if score is not None else ""))
    text = f"{head}: {' '.join(marks)}"
    oc = line.get("_outcome")
    tone = "info" if not done else ""
    if oc:
        text += f" → {oc.get('outcome')} {oc.get('score')}/{oc.get('pass_score')}"
        tone = "ok" if oc.get("outcome") == "pass" else "warn"
    elif not done:
        text += " (running)"
    fs = findings_summary(line["children"])
    if fs:
        text += f" · {fs}"
    for m in members:                      # each reviewer's run is a detail child
        e = m.get("_end")
        line["children"].append({"text": run_text({**m, "children": [], "kind": "run"})[0],
                                 "tone": "", "seqs": [s for s in (m["_start"].get("seq") if m.get("_start") else None,
                                                                  e.get("seq") if e else None,
                                                                  m["_cost"].get("seq") if m.get("_cost") else None)
                                                      if s is not None],
                                 "run_id": m.get("run_id")})
    return text, tone


def rebase_text(line, base) -> tuple[str, str]:
    run = line.get("_run")
    res = line.get("_resolved")
    conflict = line.get("_conflict")
    files = [str(f) for f in _list((res or {}).get("conflicts"))]
    if not files and conflict is not None:
        files = [str(f) for f in _list(conflict.get("files"))]
    onto = base or "base"
    if res and res.get("target_sha"):
        onto += f" ({str(res['target_sha'])[:8]})"
    regen = line.get("_regen")
    if regen is not None and run is None and res is None:
        files = [str(f) for f in _list(regen.get("files"))]
        return (f"Rebase onto {onto}: regenerated generated files"
                + (f" ({', '.join(files)})" if files else ""), "")
    text = f"Rebase onto {onto} conflicted" + (f" ({', '.join(files)})" if files else "")
    tone = "warn"
    failed = line.get("_failed")
    if run is not None:
        head = _run_head("conflict_resolver", run.get("model"), run.get("_start"), run.get("_end"),
                         run.get("_cost"))
        head = head.replace("Conflict resolver", "conflict_resolver", 1)
        end = run.get("_end")
        if failed is not None:
            text += f" → {head}: {failed.get('title')}"
            tone = "bad"
        elif end is None:
            text += f" → {head} running"
            tone = "info"
        elif res is not None:
            text += f" → resolved by {head}"
            tone = "ok"
        else:
            fail = _failure(end)
            v = _dict(end.get("verdict"))
            text += f" → {head}: {fail or v.get('status') or 'not resolved'}"
            tone = "bad"
        v = _dict((end or {}).get("verdict"))
        if v.get("summary"):
            line["children"].append({"text": f"summary: {_clip(v['summary'], 200)}", "tone": "",
                                     "seqs": [end["seq"]]})
    elif res is not None:
        text += " → resolved"
        tone = "ok"
    if failed is not None:
        if run is None:
            text += f" → {failed.get('title')}"
            tone = "bad"
        if failed.get("detail"):
            line["children"].append({"text": f"detail: {_clip(failed['detail'], 200)}",
                                     "tone": "", "seqs": [failed["seq"]]})
    for r in _list((res or {}).get("resolutions")):
        if isinstance(r, dict):
            line["children"].append({"text": f"{r.get('path')}: {r.get('strategy')} — "
                                             f"{_clip(r.get('how'), 140)}", "tone": "",
                                     "seqs": [res["seq"]]})
    return text, tone


def group_title(g) -> str:
    title = PHASE_TITLE.get(g["phase"], g["phase"])
    if g["phase"] == "gate" and "TRIAGING" in g["states"]:
        title = "Gating & triage"
    if g["phase"] == "spec" and "SPEC_REVIEW" in g["states"]:
        title = "Spec writing & review"
    if g["phase"] == "close" and any(line["kind"] == "rebase" for line in g["lines"]):
        title = "Closing (rebase, re-gate, merge)"
    return title


def _cost_delta(a, b, cap) -> str:
    if a is None and b is None:
        return ""
    if a is None or b is None or abs((a or 0) - (b or 0)) < 0.005:
        s = fmt_usd(b if b is not None else a)
    else:
        s = f"{fmt_usd(a)} → {fmt_usd(b)}"
    return s + (f" of {fmt_usd(cap)}" if cap else "")


def group_header(g) -> str:
    bits = [g["title"], g["path"]]
    if g.get("wall_s") is not None:
        bits.append(fmt_duration(g["wall_s"]))
    cd = _cost_delta(g.get("cost_from"), g.get("cost_to"), g.get("cap_usd"))
    if cd:
        bits.append(cd)
    return " · ".join(bits)


def loop_header(lp) -> str:
    bits = [lp["title"]]
    if lp.get("wall_s") is not None:
        bits.append(fmt_duration(lp["wall_s"]))
    cd = _cost_delta(lp.get("cost_from"), lp.get("cost_to"), lp.get("cap_usd"))
    if cd:
        bits.append(cd)
    return " · ".join(bits)


def story(events, *, campaign=None, state=None, now=None) -> dict:
    """`nw.campaign.story.v1` for a campaign's ledger events (any order; sorted by seq).

    `state` (the campaign's state.json, optional) only supplies the header; `now` (an ISO
    timestamp) closes the open group of a live campaign. Never raises on odd events: an event
    it does not know becomes a short `type (by): k=v ...` line."""
    evs = sorted((e for e in events if isinstance(e, dict)),
                 key=lambda e: (e.get("seq") if isinstance(e.get("seq"), int) else 0))
    b = _Builder(campaign, state, now)
    for ev in evs:
        if ev.get("seq") is None:
            ev = {**ev, "seq": -(b.n + 1)}
        try:
            b.feed(ev)
        except Exception as e:             # noqa: BLE001 - one odd event never loses the story
            if b.group is None:
                b._new_group(ev.get("ts"), b.cur)
            b._line(f"{ev.get('type')}: (unreadable: {type(e).__name__}) {_clip(raw_text(ev), 120)}",
                    "notes", [ev["seq"]], kind="event", tone="warn")
    if b.group is None and not b.loops:
        return {"schema": SCHEMA, "campaign": campaign, "header": _header(state, b, evs),
                "loops": [], "unknown_types": {}, "events": 0, "lines": 0}
    b.finish()
    n_lines = sum(len(g["lines"]) for lp in b.loops for g in lp["groups"])
    return {"schema": SCHEMA, "campaign": campaign or (evs[0].get("campaign") if evs else None),
            "header": _header(state, b, evs), "loops": b.loops, "unknown_types": b.unknown,
            "events": len(evs), "lines": n_lines}


def _header(state, b, evs) -> dict:
    st = state or {}
    cost = st.get("cost") or {}
    return {"state": st.get("state") or b.cur, "loops": b.loop + 1 if evs else 0,
            "cost_usd": _num(cost.get("total_usd")) if cost.get("total_usd") is not None else round(b.total, 4),
            "cap_usd": _num(cost.get("cap_usd")) if cost.get("cap_usd") is not None else b.cap,
            "first_ts": evs[0].get("ts") if evs else None,
            "last_ts": evs[-1].get("ts") if evs else None}


def iter_lines(doc):
    """`(loop, group, line)` for every line of a story doc, in order."""
    for lp in doc.get("loops") or []:
        for g in lp.get("groups") or []:
            for line in g.get("lines") or []:
                yield lp, g, line


def text_lines(doc, *, expand=False, quiet=None) -> list[tuple[int, str, str]]:
    """The story as `(indent, text, tone)` lines for a terminal. Collapsed by default: loop
    and group headers and the non-quiet lines (a `[+N]` marks lines with hidden children).
    `expand` also prints every line's children and the quiet lines."""
    show_quiet = expand if quiet is None else quiet
    out = []
    for lp in doc.get("loops") or []:
        out.append((0, lp.get("header") or lp.get("title"), "head"))
        for g in lp.get("groups") or []:
            out.append((1, g.get("header") or g.get("title"), "group"))
            hidden = 0
            for line in g.get("lines") or []:
                if line.get("quiet") and not show_quiet:
                    hidden += 1
                    continue
                kids = line.get("children") or []
                more = f"  [+{len(kids)}]" if kids and not expand else ""
                out.append((2, f"{line['text']}{more}", line.get("tone") or ""))
                if expand:
                    for c in kids:
                        out.append((3, c.get("text") or "", c.get("tone") or ""))
            if hidden:
                out.append((2, f"({hidden} quiet detail{'s' if hidden != 1 else ''})", "dim"))
    return out
