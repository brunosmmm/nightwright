"""Shared Rich renderers for human-facing output (SPEC-0185).

Every human path renders the common shapes — a campaign event, a queue event, a mailbox
message, a tick step, a plan block, a code/log block, a warning — through here, so colours
and glyphs stay consistent. Each line renderer returns a `rich.text.Text` whose `.plain`
is exactly the line the plain helpers (`events.summary_line`, `mailbox.summary_line`,
`queue.tick_row_text`) produce, so piped output and greps are unchanged."""
from __future__ import annotations

from rich import box
from rich.console import Console, Group
from rich.markup import escape
from rich.table import Table
from rich.text import Text

err_console = Console(stderr=True, soft_wrap=True, highlight=False)

STATE_STYLES = {
    "QUEUED": "dim", "WAITING": "bright_blue", "OPENING": "cyan", "SPEC_WRITING": "cyan", "SPEC_REVIEW": "magenta", "IMPLEMENTING": "cyan", "GATING": "blue",
    "TRIAGING": "magenta", "REVIEWING": "magenta", "CLOSING": "green", "PARKED": "yellow",
    "MERGED": "bold green", "HANDED_OFF": "bold green", "ABANDONED": "dim red",
}

_EVENT_STYLES = {
    "transition": "bold", "open": "cyan", "run.start": "cyan", "run.end": "cyan",
    "cost": "dim", "commit": "green", "park": "yellow", "resume": "cyan", "close": "bold green",
    "note": "italic", "backlog": "dim", "abandon": "red", "triage": "magenta",
    "triage.brief": "magenta", "rebase_resolved": "green",
}

_MESSAGE_STYLES = {"question": "bold yellow", "answer": "bold green", "request": "bold cyan",
                   "info": "dim"}


_PROJECT_PALETTE = ("bright_cyan", "bright_magenta", "bright_green", "bright_yellow",
                    "bright_blue", "orange1", "orchid", "turquoise2")


def state_style(state) -> str:
    return STATE_STYLES.get(str(state or ""), "")


def project_style(project) -> str:
    """A stable colour per project name, so items of one project read as a group when several
    projects are in flight at once."""
    import zlib
    if not project:
        return "dim"
    return _PROJECT_PALETTE[zlib.crc32(str(project).encode()) % len(_PROJECT_PALETTE)]


def project_text(project) -> Text:
    return Text(str(project or "—"), style=project_style(project))


def warn(msg: str) -> None:
    """A warning on stderr (yellow on a terminal), text unchanged, never wrapped."""
    err_console.print(f"[yellow]{escape(msg)}[/]")


def note(msg: str) -> None:
    """A progress/info line on stderr (dim on a terminal), text unchanged."""
    err_console.print(f"[dim]{escape(msg)}[/]")


def print_code(text: str, lexer: str, *, console, title: str | None = None,
               theme: str = "nord") -> None:
    """A highlighted code/log block on a terminal; the exact text when piped (no wrapping,
    no cropping, so agents read the same bytes as before)."""
    if not console.is_terminal:
        if title is not None:
            console.file.write(f"# {title}\n")
        console.file.write(text)
        return
    from rich.syntax import Syntax
    if title is not None:
        console.print(Text(f"# {title}", style="dim"), soft_wrap=True)
    console.print(Syntax(text.rstrip("\n"), lexer, theme=theme, word_wrap=False,
                         background_color="default"), soft_wrap=True)


def _kv(text: Text, key: str, value) -> None:
    text.append(f"{key}=", style="dim")
    style = ""
    if key in ("ok", "verdict_ok"):
        style = "green" if str(value) == "True" else "red"
    elif key in ("from", "to", "resume_to"):
        style = state_style(value)
    text.append(str(value), style=style)


def rebase_resolved_text(ev: dict) -> str:
    """`rebase resolved: 3 paths (both×2, theirs×1)` for a `rebase_resolved` ledger event."""
    counts: dict = {}
    for r in ev.get("resolutions") or []:
        key = (r or {}).get("strategy") or "?"
        counts[key] = counts.get(key, 0) + 1
    paths = len(ev.get("conflicts") or [])
    by = ", ".join(f"{k}\u00d7{n}" for k, n in counts.items())
    text = f"rebase resolved: {paths} paths" + (f" ({by})" if by else "")
    extra, absorbed = len(ev.get("extra_commits") or []), len(ev.get("absorbed_commits") or [])
    if extra or absorbed:                               # SPEC-0252
        text += f"; +{extra} extra, {absorbed} absorbed"
    if ev.get("superseded_commits"):                    # SPEC-0259
        text += f"; {len(ev['superseded_commits'])} superseded"
    return text


def campaign_event(ev: dict) -> Text:
    """`wt campaign show|log` line: seq, ts, type, by, then key=value (values ≤ 60 chars)."""
    skip = {"schema", "ts", "seq", "campaign", "type", "by"}
    etype = ev.get("type", "?")
    style = _EVENT_STYLES.get(etype, "")
    if etype == "gate":
        style = "green" if ev.get("ok") else "red"
    t = Text()
    t.append(f"{ev.get('seq', '?'):>4}", style="dim")
    t.append("  ")
    t.append(str(ev.get("ts", "")), style="dim")
    t.append("  ")
    t.append(f"{etype:<12}", style=style)
    t.append(" ")
    t.append(f"{ev.get('by', ''):<16}", style="dim")
    t.append(" ")
    if etype == "rebase_resolved":                      # SPEC-0221
        t.append(rebase_resolved_text(ev))
        return t
    if etype == "idea_reconciled":                      # SPEC-0257
        t.append(f"idea {ev.get('idea_id')}: {ev.get('from')} -> {ev.get('to')}")
        return t
    if etype == "idea_handed_off":                      # SPEC-0280
        t.append(f"idea {ev.get('idea_id')} handed off → {ev.get('pr_url')}")
        return t
    first = True
    for k, v in ev.items():
        if k in skip or (k == "spec_id" and v is None):   # a merge request has no spec id
            continue
        txt = v if isinstance(v, str) else str(v)
        if len(txt) > 60:
            txt = txt[:57] + "..."
        if not first:
            t.append(" ")
        first = False
        _kv(t, k, txt)
    return t


def queue_event(ev: dict) -> Text:
    """`wt queue events --text` line (same words as `events.summary_line`)."""
    etype = ev.get("type", "?")
    style = ("red" if any(w in etype for w in ("fail", "dead", "stale", "stuck"))
             else "yellow" if "park" in etype
             else "green" if etype in ("merged", "handed_off", "closed") else "cyan")
    t = Text()
    t.append(f"#{ev['id']}", style="dim")
    t.append(" ")
    t.append(etype, style=style)
    t.append(" ")
    t.append(ev.get("campaign") or "-", style="bold")
    bits = [(k, v) for k, v in (ev.get("data") or {}).items() if v not in (None, "", [])]
    for k, v in bits:
        t.append(" ")
        _kv(t, k, v)
    t.rstrip()
    return t


def message(m: dict) -> Text:
    """Mailbox line (same words as `mailbox.summary_line`)."""
    t = Text()
    t.append(m["id"], style="dim")
    t.append(" ")
    t.append(m["kind"], style=_MESSAGE_STYLES.get(m["kind"], ""))
    t.append(" ")
    t.append(f"from={m['from']}", style="dim")
    if m.get("campaign"):
        t.append(" ")
        t.append(f"[{m['campaign']}]", style="bold")
    if m.get("re"):
        t.append(f" re={m['re']}", style="dim")
    t.append(": ")
    t.append(str(m["text"]).replace("\n", " "))
    return t


def tick_row(row: dict) -> Text:
    """`wt queue tick|run` step (same words as `queue.tick_row_text`)."""
    t = Text()
    t.append(f"{row['campaign']}", style="bold")
    t.append(": ")
    t.append(str(row["from"]), style=state_style(row["from"]))
    t.append(" → ")
    t.append(str(row["to"]), style=state_style(row["to"]))
    t.append(f" ({row['action']}; {row['reason']})", style="dim")
    if row.get("run_id"):
        t.append(f" run {row['run_id']}")
    if row.get("cost_usd") is not None:
        if row.get("cost_estimated"):
            t.append(f" ~${row['cost_usd']:.2f} est.", style="dim")
        else:
            t.append(f" ${row['cost_usd']:.2f}", style="green")
    return t


def started_row(row: dict) -> Text:
    """A step `wt queue run` submitted this tick (SPEC-0213); its result row comes later."""
    t = Text()
    t.append(f"{row['campaign']}", style="bold")
    t.append(": started ", style="dim")
    t.append(str(row.get("action")))
    t.append(" in ", style="dim")
    t.append(str(row.get("state")), style=state_style(row.get("state")))
    if row.get("run_ids"):
        t.append(f" (run {', '.join(row['run_ids'])})", style="dim")
    return t


def plan(title: str, mapping: dict) -> Group:
    """A dry-run plan block: `plan  <title>`, then `  key: value` lines with dim keys (empty
    values skipped)."""
    lines = [Text.assemble(("plan", "bold"), "  ", title)]
    for k, v in mapping.items():
        if v in (None, "", [], {}):
            continue
        lines.append(Text.assemble((f"  {k}:", "dim"), " ", str(v)))
    return Group(*lines)


def section_table(title: str, **kw) -> Table:
    """A titled table in wt's house style (left title, simple head)."""
    kw.setdefault("expand", True)
    return Table(title=title, title_justify="left", title_style="bold", box=box.SIMPLE_HEAD,
                 pad_edge=False, **kw)
