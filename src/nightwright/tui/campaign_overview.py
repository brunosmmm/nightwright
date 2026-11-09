"""SPEC-0498: the campaign detail Overview pane as dashboard cards.

Pure Rich: a `nw.campaign.history.v1` payload (plus the optional campaign `state` and spec
title) becomes one `Overview` renderable. It picks its layout from the width Rich hands it
(three, two or one column), so the Textual `Static` that shows it re-lays out on resize with
no widget code. Nothing here imports Textual or touches the filesystem.
"""
from __future__ import annotations

import re
from datetime import datetime

from rich import box
from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from nightwright.agentic.history import PHASE_OF

__all__ = (
    "Overview", "overview", "badge", "reason", "fmt_s", "short_model", "run_glyph",
    "WIDE_MIN", "THREE_COL_MIN", "SIDE_BY_SIDE_MIN",
)

WIDE_MIN = 96            # >= this: banner + two card columns
THREE_COL_MIN = 150      # >= this: banner + three card columns
SIDE_BY_SIDE_MIN = 64    # narrow layout: gates | findings side by side from this width
RUN_ROWS = {3: 18, 2: 14, 1: 10}   # most run rows shown per layout (latest kept)
GATE_ROWS = {3: 10, 2: 10, 1: 6}
IDLE_PHASES = frozenset({"queue", "parked", "waiting"})
RETRY_VERDICTS = frozenset({"fix", "partial", "split", "blocked", "retry_gate", "rework",
                            "request_changes", "changes"})
FAILED_RUNS = frozenset({"error", "timeout", "failed", "killed", "crashed", "aborted"})
_PARTIAL = " ▏▎▍▌▋▊▉"
_GLYPH_RANK = {"✓": 0, "●": 1, "↻": 2, "✗": 3}
CARD_BORDER = "grey50"


def fmt_s(seconds) -> str:
    """42s, 23m, 1h58m, 2d03h."""
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "-"
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts))
    except ValueError:
        return None


def _clock(ts, ref) -> str:
    """HH:MM when on the reference day, else `Nov 07 10:41`."""
    t = _parse(ts)
    if t is None:
        return "-"
    r = _parse(ref)
    if r is not None and t.tzinfo and r.tzinfo:
        r = r.astimezone(t.tzinfo)
    if r is not None and r.date() == t.date():
        return t.strftime("%H:%M")
    return t.strftime("%b %d %H:%M")


def short_model(model) -> str:
    """`grok-4.7-high` -> `grok-high`, `composer-2.5` -> `composer`, medium -> med."""
    if not model:
        return "-"
    out = re.sub(r"-\d+(?:\.\d+)*(?=-|$)", "", str(model))
    return out.replace("medium", "med") or str(model)


def badge(header: dict) -> tuple[str, str]:
    """(badge text, colour) from the outcome, else the live state."""
    outcome = header.get("outcome")
    state = header.get("state") or "?"
    if outcome == "merged":
        return "✓ MERGED", "green"
    if outcome == "handed_off":
        return "⇢ HANDED OFF", "green"
    if outcome == "abandoned":
        return "✗ ABANDONED", "red"
    if state == "PARKED":
        return "⏸ PARKED", "yellow"
    if state == "WAITING":
        return "⧗ WAITING", "yellow"
    return f"● {state}", "cyan"


def _live(header: dict) -> bool:
    return not header.get("outcome") and header.get("state") not in (
        "MERGED", "HANDED_OFF", "ABANDONED")


def _last_failed_gate(gates):
    if not gates or gates[-1].get("ok") is not False:
        return None
    return gates[-1]


def _failed_names(gate) -> list[str]:
    return [str(r.get("name") or "?") for r in (gate.get("results") or [])
            if r.get("ok") is False]


def reason(history: dict, state: dict | None = None):
    """The banner's WHY line as (label, text, colour), or None for a clean/merged run."""
    header = history.get("header") or {}
    state = state or {}
    records = history.get("records") or []
    park = header.get("park") or state.get("park")
    if park:
        why = str(park.get("reason") or "parked")
        if park.get("escalated_from"):
            why += f" (from {park['escalated_from']})"
        detail = str(park.get("detail") or "").strip()
        resume = f" → resume to {park['resume_to']}" if park.get("resume_to") else ""
        return "why parked", f"{why}: {detail}{resume}" if detail else why + resume, "yellow"
    if header.get("outcome") == "abandoned":
        for kinds in (("decision",), ("escalation",)):
            hit = next((r for r in reversed(records) if r.get("kind") in kinds
                        and (kinds != ("decision",) or "abandon" in str(r.get("title") or ""))
                        and r.get("text")), None)
            if hit:
                return "why abandoned", str(hit["text"]).strip(), "red"
        gate = _last_failed_gate(history.get("gates") or [])
        if gate:
            return ("why abandoned", f"gate #{gate.get('gate_seq')} failed: "
                    + ", ".join(_failed_names(gate)), "red")
        return "why abandoned", "no reason recorded", "red"
    requested = state.get("park_requested")
    if requested and _live(header):
        return "park requested", str((requested or {}).get("detail") or "").strip() or "-", \
            "yellow"
    if _live(header):
        gate = _last_failed_gate(history.get("gates") or [])
        if gate:
            return ("attention", f"gate #{gate.get('gate_seq')} failed: "
                    + ", ".join(_failed_names(gate)), "yellow")
    return None


def run_glyph(run: dict) -> tuple[str, str]:
    status = run.get("status")
    if status == "running":
        return "●", "cyan"
    if status in FAILED_RUNS or run.get("exit_code") not in (0, None):
        return "✗", "red"
    if run.get("verdict_status") in RETRY_VERDICTS:
        return "↻", "yellow"
    return "✓", "green"


_GLYPH_STYLE = {"✓": "green", "●": "cyan", "↻": "yellow", "✗": "red"}


def _line(width: int, *parts) -> Text:
    """One cropped, unwrapped line from (text, style) parts."""
    t = Text(no_wrap=True, overflow="ellipsis")
    for text, style in parts:
        t.append(text, style=style or "")
    t.truncate(width, overflow="ellipsis")
    return t


def _flow(width: int, chunks: list[list]) -> list[Text]:
    """Pack chunks (each a list of (text, style)) onto lines, 3 spaces apart; a chunk is
    never split, and one wider than the line is cropped."""
    lines, cur, used = [], [], 0
    for chunk in chunks:
        size = sum(len(text) for text, _ in chunk)
        if cur and used + 3 + size > width:
            lines.append(_line(width, *cur))
            cur, used = [], 0
        if cur:
            cur.append(("   ", ""))
            used += 3
        cur += chunk
        used += size
    if cur:
        lines.append(_line(width, *cur))
    return lines


def _card(title: str, body: list, width: int, border: str = CARD_BORDER,
          title_style: str = "bold") -> Panel:
    return Panel(Group(*body), title=Text(title, style=title_style),
                 title_align="left", box=box.ROUNDED, border_style=border,
                 width=width, padding=(0, 1))


# --- banner -----------------------------------------------------------------------------

def _banner(history, state, title, width) -> Panel:
    h = history.get("header") or {}
    ref = history.get("generated_at")
    inner = max(10, width - 4)
    text, colour = badge(h)
    ident = h.get("spec_id") or h.get("idea_id") or h.get("campaign") or "?"
    name = title or (h.get("title") if h.get("title") != ident else "") or ""
    head = Text(ident, style="bold")
    if name:
        head.append(f" · {name}", style="bold")
    tag = Text(text, style=f"bold {colour}")
    room = width - 6 - tag.cell_len - 3          # Panel: "╭─ " + title + " ─╮"
    head.truncate(max(5, room), overflow="ellipsis")
    gap = max(1, width - 6 - head.cell_len - tag.cell_len - 2)
    title_text = head + Text(" " + "─" * gap + " ", style=colour) + tag

    live = _live(h)
    ident_chunk = [(str(h.get("campaign") or "-"), "bold")]
    for v in (h.get("project"), h.get("idea_id"), h.get("profile")):
        if v:
            ident_chunk.append((f" · {v}", ""))
    meta = [ident_chunk, [("opened ", "dim"), (_clock(h.get("created_at"), ref), "")]]
    if live:
        meta.append([("elapsed ", "dim"), (fmt_s(h.get("wall_s")), "cyan")])
    else:
        meta.append([("closed ", "dim"), (_clock(h.get("closed_at"), ref), "")])

    loops, loop_max = h.get("loops") or 0, h.get("loop_max")
    loop_style = "yellow" if loop_max is not None and loops > loop_max else "bold"
    cost, cap = float(h.get("cost_usd") or 0.0), h.get("cap_usd")
    cost_style = "bold"
    if cap:
        frac = cost / float(cap)
        cost_style = "red" if frac >= 1 else "yellow" if frac >= 0.8 else "bold"
    stats = [[("wall ", "dim"), (fmt_s(h.get("wall_s")), "bold"),
              (" · active ", "dim"), (fmt_s(h.get("active_s")), "bold")],
             [("loops ", "dim"),
              (f"{loops}/{loop_max if loop_max is not None else '?'}", loop_style)],
             [("runs ", "dim"), (str(h.get("runs") or 0), "bold")],
             [("cost ", "dim"), (f"${cost:.2f}", cost_style)]
             + ([(f" / ${float(cap):.2f}", "dim")] if cap else [])]
    if h.get("conductor_usd"):
        stats.append([("conductor ", "dim"), (f"${float(h['conductor_usd']):.2f}", "")])
    if h.get("merged_sha"):
        stats.append([("merged ", "dim"), (str(h["merged_sha"])[:8], "bold green")])
    if h.get("pr_url"):
        stats.append([("PR ", "dim"), (str(h["pr_url"]), "")])
    if h.get("followup_idea"):
        stats.append([("follow-up ", "dim"), (str(h["followup_idea"]), "")])
    repo = (state or {}).get("repo") or {}
    if live and repo.get("branch"):
        stats.append([("branch ", "dim"), (str(repo["branch"]), "")])
    lines = _flow(inner, meta) + _flow(inner, stats)

    why = reason(history, state)
    if why:
        label, body, wcolour = why
        t = Text()
        t.append(f"{label}: ", style=f"bold {wcolour}")
        t.append(body, style=wcolour)
        lines.append(t)          # wraps: the reason is the point, never cropped
    gaps = history.get("gaps") or []
    if gaps:
        lines.append(_line(inner, (f"⚠ {len(gaps)} history gap{'s' * (len(gaps) != 1)}"
                                   " — some runs or gates may be incomplete", "yellow dim")))
    return Panel(Group(*lines), title=title_text, title_align="left", box=box.ROUNDED,
                 border_style=colour, width=width, padding=(0, 1))


# --- cards ------------------------------------------------------------------------------

def _bar(value, top, width) -> tuple[str, str]:
    if top <= 0 or width <= 0:
        return "", "░" * max(0, width)
    cells = max(0.0, float(value)) / top * width
    full = int(cells)
    frac = int(round((cells - full) * 8))
    if frac == 8:
        full, frac = full + 1, 0
    bar = "█" * full + (_PARTIAL[frac] if frac and full < width else "")
    if not bar and value > 0:
        bar = "▏"
    return bar, "░" * max(0, width - len(bar))


def _time_card(history, width) -> Panel:
    h = history.get("header") or {}
    inner = max(10, width - 4)
    phases = [p for p in (history.get("phases") or []) if isinstance(p, dict)]
    current = PHASE_OF.get(h.get("state")) if _live(h) else None
    rows = [p for p in phases if (p.get("wall_s") or 0) > 0 or p.get("phase") == current]
    dominant = (history.get("dominant") or {}).get("wall")
    if not rows:
        return _card("where the time went", [Text("no time recorded yet", style="dim")], width)
    top = max(float(p.get("wall_s") or 0) for p in rows)
    label_w = max(9, max(len(str(p.get("phase"))) for p in rows) + 1)
    tail_w = 7 + 4 + 6           # " 1h58m" + " ×3 " + "● now"
    bar_w = max(4, inner - label_w - tail_w)
    body = []
    for p in rows:
        phase = str(p.get("phase"))
        wall = float(p.get("wall_s") or 0)
        colour = ("yellow" if phase == "parked" else "grey62" if phase in IDLE_PHASES
                  else "cyan")
        is_now = phase == current
        is_dom = phase == dominant
        full, rest = _bar(wall, top, bar_w)
        visits = p.get("visits") or 0
        marker = (("◀ now" if is_dom else "● now", "bold cyan") if is_now
                  else ("◀", "bold") if is_dom else ("", ""))
        body.append(_line(
            inner,
            (f"{phase:<{label_w}}", "bold cyan" if is_now else "bold" if is_dom else ""),
            (full, f"bold {colour}" if is_dom or is_now else colour), (rest, "grey30"),
            (f" {fmt_s(wall):>6}", "bold" if is_dom else ""),
            (f" {'×' + str(visits) if visits > 1 else '':<3} ", "dim"), marker))
    idle = sum(float(p.get("wall_s") or 0) for p in rows if p.get("phase") in IDLE_PHASES)
    share = round(100 * idle / float(h["wall_s"])) if h.get("wall_s") else 0
    if idle >= 60 and share >= 5:
        body.append(_line(inner, (f"idle (queue/parked/waiting) {fmt_s(idle)} · {share}% "
                                  "of wall", "dim")))
    return _card("where the time went", body, width)


def _run_groups(runs):
    """Consecutive same-loop review-phase `*_reviewer` runs fold into one row."""
    out, i = [], 0
    while i < len(runs):
        r = runs[i]
        if r.get("phase") == "review" and str(r.get("persona") or "").endswith("_reviewer"):
            j = i
            while (j + 1 < len(runs) and runs[j + 1].get("phase") == "review"
                   and runs[j + 1].get("loop") == r.get("loop")
                   and str(runs[j + 1].get("persona") or "").endswith("_reviewer")):
                j += 1
            if j > i:
                out.append(runs[i:j + 1])
                i = j + 1
                continue
        out.append([r])
        i += 1
    return out


def _run_seconds(run, ref) -> float | None:
    if run.get("duration_s") is not None:
        try:
            return float(run["duration_s"])
        except (TypeError, ValueError):
            return None
    start, now = _parse(run.get("started_at")), _parse(ref)
    if start and now and run.get("status") == "running":
        return max(0.0, (now - start).total_seconds())
    return None


def _runs_card(history, width, max_rows) -> Panel:
    runs = [r for r in (history.get("runs") or []) if isinstance(r, dict)]
    ref = history.get("generated_at")
    inner = max(10, width - 4)
    if not runs:
        return _card("runs", [Text("no runs yet", style="dim")], width)
    groups = _run_groups(runs)
    rows = []
    for g in groups:
        glyphs = [run_glyph(r) for r in g]
        glyph = max((x[0] for x in glyphs), key=_GLYPH_RANK.get)
        secs = [s for s in (_run_seconds(r, ref) for r in g) if s is not None]
        dur = fmt_s(max(secs)) if secs else "-"
        running = any(r.get("status") == "running" for r in g)
        models = {short_model(r.get("model")) for r in g}
        model = models.pop() if len(models) == 1 else "mixed"
        if len(g) == 1:
            name = str(g[0].get("persona") or "?")
            verdict = str(g[0].get("verdict_status") or g[0].get("status") or "")
        else:
            name = f"{len(g)} reviewers"
            counts: dict[str, int] = {}
            for r in g:
                v = str(r.get("verdict_status") or r.get("status") or "?")
                counts[v] = counts.get(v, 0) + 1
            verdict = " ".join(f"{n}×{v}" if n > 1 else v for v, n in counts.items())
        rows.append((name, model, dur + ("…" if running else ""), glyph, verdict,
                     g[0].get("loop")))
    name_w = min(22, max(len(r[0]) for r in rows) + 1)
    model_w = min(14, max(len(r[1]) for r in rows) + 1)
    hidden = max(0, len(rows) - max_rows)
    body = []
    if hidden:
        body.append(_line(inner, (f"… {hidden} earlier row{'s' * (hidden != 1)} "
                                  f"({sum(len(g) for g in groups[:hidden])} runs)", "dim")))
    last_loop = None
    for name, model, dur, glyph, verdict, loop in rows[hidden:]:
        style = _GLYPH_STYLE[glyph]
        loop_tag = f"L{loop}" if loop is not None and loop != last_loop else ""
        last_loop = loop if loop is not None else last_loop
        body.append(_line(
            inner, (f"{loop_tag:<3}", "dim"), (f"{name:<{name_w}}", ""),
            (f"{model:<{model_w}}", "dim"), (f"{dur:>6} ", ""), (glyph, f"bold {style}"),
            (f" {verdict}", style if glyph != "✓" else "dim")))
    bad = sum(1 for r in runs if run_glyph(r)[0] == "✗")
    retry = sum(1 for r in runs if run_glyph(r)[0] == "↻")
    border = "red" if bad else "yellow" if retry else "green"
    summary = f"runs · {len(runs)}"
    if bad:
        summary += f" · {bad} failed"
    if retry:
        summary += f" · {retry} retried"
    return _card(summary, body, width, border=border)


def _close_start(history):
    for row in history.get("timeline") or []:
        if row.get("kind") == "transition" and row.get("phase") == "close":
            return _parse(row.get("ts"))
    return None


def _gates_card(history, width, max_rows) -> Panel:
    gates = [g for g in (history.get("gates") or []) if isinstance(g, dict)]
    inner = max(10, width - 4)
    if not gates:
        return _card("gates", [Text("no gates yet", style="dim")], width)
    close_at = _close_start(history)
    body = []
    hidden = max(0, len(gates) - max_rows)
    if hidden:
        earlier_failed = sum(1 for g in gates[:hidden] if g.get("ok") is False)
        body.append(_line(inner, (f"… {hidden} earlier", "dim"),
                          (f" ({earlier_failed} failed)" if earlier_failed else "",
                           "red" if earlier_failed else "")))
    for g in gates[hidden:]:
        results = g.get("results") or []
        retries = sum(int(r.get("retries") or 0) for r in results)
        started = _parse(g.get("started_at"))
        is_close = bool(close_at and started and started >= close_at)
        dur = fmt_s(g.get("duration_s"))
        if g.get("ok") is False:
            names = _failed_names(g)
            what = (names[0] if names else "failed") + (f" +{len(names) - 1}"
                                                        if len(names) > 1 else "")
            glyph, style = "✗", "red"
        elif g.get("ok") is None:
            what, glyph, style = "running", "●", "cyan"
        else:
            what = f"{len(results)} check{'s' * (len(results) != 1)}"
            glyph, style = "✓", "green"
        if is_close:
            what = "close · " + what
        tail = f" ↻{retries}" if retries else ""
        name_w = max(4, inner - 4 - 2 - len(tail) - 7)
        body.append(_line(
            inner, (f"#{g.get('gate_seq', '?')!s:<3}", "dim"), (glyph + " ", f"bold {style}"),
            (f"{what[:name_w]:<{name_w}}", style if glyph != "✓" else ""),
            (tail, "yellow"), (f" {dur:>6}", "")))
    failed = sum(1 for g in gates if g.get("ok") is False)
    last_bad = gates[-1].get("ok") is False
    border = "red" if last_bad else "yellow" if failed else "green"
    title = f"gates · {len(gates)}" + (f" · {failed} failed" if failed else "")
    return _card(title, body, width, border=border)


_SEVERITIES = (("must_fix", "must-fix", "bold red"), ("major", "major", "red"),
               ("minor", "minor", "yellow"), ("nit", "nit", ""), ("info", "info", ""))


def _short_test(name: str) -> str:
    path, _, test = name.rpartition("::")
    return f"{path.rsplit('/', 1)[-1]}::{test}" if path else name


def _findings_card(history, width) -> Panel:
    records = [r for r in (history.get("records") or []) if isinstance(r, dict)]
    inner = max(10, width - 4)
    counts = {key: 0 for key, _, _ in _SEVERITIES}
    for r in records:
        if r.get("kind") == "finding" and r.get("severity") in counts:
            counts[r["severity"]] += 1
    flaky = [str(r.get("title") or "?") for r in records if r.get("kind") == "flaky"]
    triage = [str(r.get("title") or "?") for r in records if r.get("kind") == "triage"]
    escal = [r for r in records if r.get("kind") == "escalation"]
    parts = []
    for key, label, style in _SEVERITIES:
        n = counts[key]
        parts.append((f"{n} {label}", (style or "") if n else "dim"))
    body: list = []
    line: list = []
    used = 0
    for text, style in parts:      # severity counts flow onto as few lines as fit
        need = len(text) + (2 if line else 0)
        if line and used + need > inner:
            body.append(_line(inner, *line))
            line, used = [], 0
            need = len(text)
        if line:
            line.append(("  ", ""))
        line.append((text, style))
        used += need
    if line:
        body.append(_line(inner, *line))
    if flaky:
        body.append(_line(inner, (f"{len(flaky)} flaky", "bold yellow")))
        for name in flaky[:3]:
            body.append(_line(inner, ("  " + _short_test(name), "yellow")))
        if len(flaky) > 3:
            body.append(_line(inner, (f"  … {len(flaky) - 3} more", "dim")))
    if triage:
        body.append(_line(inner, (f"{len(triage)} triage: ", "yellow"),
                          (", ".join(triage), "yellow")))
    if escal:
        body.append(_line(inner, (f"{len(escal)} escalation{'s' * (len(escal) != 1)}",
                                  "bold red")))
    blocking = counts["must_fix"] + counts["major"]
    if not blocking and not flaky and not escal:
        body.append(_line(inner, ("✓ nothing blocking", "green")))
    border = ("red" if counts["must_fix"] or escal else
              "yellow" if counts["major"] or flaky else "green")
    return _card("findings", body, width, border=border)


STORY_KINDS = frozenset({"decision", "escalation", "human_decision", "answer", "note",
                         "close_check"})
STORY_ROWS = {3: 10, 2: 8, 1: 5}
_NOTE_PREFIX = re.compile(r"^decision \S+ \([^)]*\): ")
_STORY_STYLE = {"escalation": "red", "human_decision": "yellow", "answer": "yellow",
                "close_check": "green"}


def _story_items(history) -> list[tuple]:
    """(when, who, text, style) for notes/decisions/escalations and run summaries, oldest
    first, with repeated texts dropped."""
    items = []
    for r in history.get("records") or []:
        if not isinstance(r, dict) or r.get("kind") not in STORY_KINDS:
            continue
        text = _NOTE_PREFIX.sub("", " ".join(str(r.get("text") or "").split()))
        if r.get("kind") == "close_check":
            text = f"close: {text}" if text else ""
        if text:
            items.append((str(r.get("ts") or ""), str(r.get("agent") or r.get("kind")),
                          text, _STORY_STYLE.get(r.get("kind"), "")))
    for run in history.get("runs") or []:
        if isinstance(run, dict) and run.get("summary"):
            items.append((str(run.get("started_at") or ""), str(run.get("persona") or "run"),
                          " ".join(str(run["summary"]).split()), "dim"))
    items.sort(key=lambda it: _parse(it[0]).timestamp() if _parse(it[0]) else 0.0)
    out, seen = [], set()
    for it in items:
        key = it[2]
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _story_card(history, width, max_rows) -> Panel | None:
    items = _story_items(history)
    if not items:
        return None
    inner = max(10, width - 4)
    ref = history.get("generated_at")
    who_w = min(22, max(len(it[1]) for it in items) + 1)
    body = []
    hidden = max(0, len(items) - max_rows)
    if hidden:
        body.append(_line(inner, (f"… {hidden} earlier", "dim")))
    clocks = [_clock(it[0], ref) for it in items[hidden:]]
    clock_w = max(len(c) for c in clocks)
    for clock, (when, who, text, style) in zip(clocks, items[hidden:]):
        body.append(_line(inner, (f"{clock:>{clock_w}} ", "dim"), (f"{who:<{who_w}}", "bold"),
                          (text, style)))
    return _card("what happened", body, width)


# --- layout -----------------------------------------------------------------------------

def _columns(cells: list[tuple[list, int]]) -> Table:
    grid = Table.grid(padding=(0, 1), expand=False)
    for _, w in cells:
        grid.add_column(width=w, no_wrap=True)
    grid.add_row(*[Group(*cards) for cards, _ in cells])
    return grid


def _clean(history) -> dict:
    """A shallow copy with `header`/`dominant` as dicts and list sections holding only dicts,
    so a partial or malformed payload renders instead of raising."""
    h = dict(history) if isinstance(history, dict) else {}
    for key in ("header", "dominant"):
        if not isinstance(h.get(key), dict):
            h[key] = {}
    for key in ("phases", "runs", "gates", "records", "timeline"):
        rows = h.get(key)
        h[key] = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    if not isinstance(h.get("gaps"), list):
        h["gaps"] = []
    return h


def _safe(build, title, width, *args):
    try:
        return build(*args)
    except Exception as e:          # one bad card must not blank the pane
        return _card(title, [Text(f"unavailable: {e}", style="yellow")], width)


def _layout(width: int) -> int:
    return 3 if width >= THREE_COL_MIN else 2 if width >= WIDE_MIN else 1


class Overview:
    """Rich renderable: banner + cards, laid out for the width it is rendered at."""

    def __init__(self, history: dict, *, state: dict | None = None, title: str | None = None):
        self.history = _clean(history)
        self.state = state if isinstance(state, dict) else {}
        self.title = title

    def renderables(self, width: int) -> list:
        width = max(30, int(width))
        cols = _layout(width)
        h = self.history

        def time(w):
            return _safe(_time_card, "where the time went", w, h, w)

        def runs(w):
            return _safe(_runs_card, "runs", w, h, w, RUN_ROWS[cols])

        def gates(w):
            return _safe(_gates_card, "gates", w, h, w, GATE_ROWS[cols])

        def findings(w):
            return _safe(_findings_card, "findings", w, h, w)

        out = [_safe(_banner, "overview", width, h, self.state, self.title, width)]
        if cols == 3:
            w1 = (width - 2) * 38 // 100
            w2 = (width - 2) * 34 // 100
            w3 = width - 2 - w1 - w2
            out.append(_columns([([time(w1)], w1), ([runs(w2)], w2),
                                 ([gates(w3), findings(w3)], w3)]))
        elif cols == 2:
            w1 = (width - 1) * 58 // 100
            w2 = width - 1 - w1
            out.append(_columns([([time(w1), runs(w1)], w1), ([gates(w2), findings(w2)], w2)]))
        else:
            out += [time(width), runs(width)]
            if width >= SIDE_BY_SIDE_MIN:
                w1 = (width - 1) // 2
                w2 = width - 1 - w1
                out.append(_columns([([gates(w1)], w1), ([findings(w2)], w2)]))
            else:
                out += [gates(width), findings(width)]
        story = _safe(_story_card, "what happened", width, h, width, STORY_ROWS[cols])
        if story is not None:
            out.append(story)
        return out

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield from self.renderables(options.max_width)

    def plain(self, width: int = 120) -> str:
        console = Console(width=width, record=True, color_system=None, file=_Null(),
                          legacy_windows=False)
        console.print(self)
        return console.export_text(styles=False)

    def __str__(self) -> str:
        return self.plain()


class _Null:
    def write(self, _):
        return 0

    def flush(self):
        pass


def overview(detail) -> Overview:
    """The Overview renderable for an `agentic_model.DetailView`."""
    title = None
    state = getattr(detail, "state", None) or {}
    try:
        from nightwright.agentic import queue as Q
        title = Q._spec_title(state) if state else None
    except Exception:
        title = None
    return Overview(getattr(detail, "history", None) or {}, state=state, title=title)
