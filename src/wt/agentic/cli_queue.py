"""`wt queue gates` (SPEC-0173), `wt queue add|list|watch|tick|run|park|resume|abandon`
(SPEC-0176), `wt queue backlog|prioritize` (SPEC-0180) and `wt queue gc` (SPEC-0275). `attach`
is called by `wt.agentic.cli.register` with the `queue` group.

Conventions (as elsewhere in wt): destructive commands are dry-run by default and write only
with `--yes`; `--json` emits `{"schema": "wt.queue.<x>.v1", …plan, "written": bool}`; library
`ValueError`s become `click.ClickException`s."""
from __future__ import annotations

import json
import os
import sys
import time

import click
from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .. import completion as C
from .. import render as R
from ..config import load_config
from ..console import console
from ..report import _dump_json
from . import gates as G
from . import lease as L
from . import mailbox as M
from . import project as P
from . import queue as Q
from . import store

_NOT_WRITTEN = "[yellow]not written — re-run with --yes to commit[/]"


def _actor(cfg, by) -> str:
    """SPEC-0273: who a human-command acts as — `--by`, else $WT_MAILBOX_ADDR, else human.
    ValueError on a bad address (callers run it inside their ValueError → ClickException)."""
    return M.validate_address(cfg, by or os.environ.get("WT_MAILBOX_ADDR") or "human")


def _age_txt(age) -> str:
    return f"{age}s" if age is not None and age < 120 else (f"{age // 60}m" if age else "")


_WIDE_COLS = 110        # below this the list table has no room for size / tone / profile columns


def _size_tone(it: dict) -> str:
    """`m/high-end`, or `-` for a campaign without sizing (SPEC-0219)."""
    size, tone = it.get("size"), it.get("tone")
    return f"{size or '?'}/{tone or '?'}" if size or tone else "-"


def _profile_text(it: dict) -> Text:
    """The profile name, then a dim `(flag)`/`(default)` when it was not picked by the review."""
    t = Text(str(it.get("profile") or "-"))
    src = it.get("profile_source")
    if src and src != "review":
        t.append(f" ({src})", style="dim")
    return t


def _source_marker(it: dict) -> Text:
    src = it.get("profile_source")
    return Text(f" ({src})", style="dim") if src and src != "review" else Text()


def _exit_leftover() -> None:
    """Worker threads outlived `Q.run` (SPEC-0229): flush and leave with status 70; executor
    threads are not daemonic, so a normal interpreter exit would join them."""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(70)


def _queue_table(payload: dict, width: int | None = None) -> Group:
    """The human listing: an Active table (the slots) and a Backlog table (admission order).
    Dense enough for an 80-column terminal, ids never cut (they are what a human copies into
    the next command); the park reason rides in the state cell."""
    lease = payload["lease"]
    slots = payload["slots"]
    if lease.get("held"):
        hb = lease.get("heartbeat_age_s")
        stale = " [red]STALE[/]" if lease.get("stale") else ""
        lease_txt = f"lease pid {lease.get('pid')} (heartbeat {hb}s ago){stale}"
    else:
        lease_txt = "no loop running"
    active = [it for it in payload["items"] if it["state"] not in ("QUEUED", "WAITING")]
    waiting = [it for it in payload["items"] if it["state"] == "WAITING"]
    backlog = sorted((it for it in payload["items"] if it["state"] == "QUEUED"),
                     key=lambda it: it["backlog"]["position"])
    table = Table(title=f"Active ({slots['busy']}/{slots['total']}) · {lease_txt}",
                  box=box.SIMPLE_HEAD, pad_edge=False)
    table.add_column("campaign", min_width=24, no_wrap=True)
    table.add_column("project", no_wrap=True, overflow="ellipsis", max_width=18)
    table.add_column("state", no_wrap=True)
    wide = (width or console.width) >= _WIDE_COLS
    if wide:
        table.add_column("size", no_wrap=True)
        table.add_column("tone", no_wrap=True)
        table.add_column("profile", no_wrap=True)
    table.add_column("loops")
    table.add_column("cost")
    table.add_column("age")
    for it in active:
        est = (it.get("cost") or {}).get("estimated_usd") or 0.0
        cost = (f"${it['cost_usd']:.2f}" + (f" (~${est:.2f} est.)" if est > 0 else "")
                + (f"/{it['cap_usd']:.0f}" if it.get("cap_usd") else ""))
        park = it.get("park") or {}
        state = it["state"]
        if it.get("run"):
            state = f"{state} ({it['run']})"           # SPEC-0229: run: detached (pid N)
        if park.get("reason"):
            state = f"{state}:{park['reason']}"
        elif it.get("park_requested"):
            state = f"{state}:park?"
        color = {"PARKED": "yellow", "MERGED": "green", "HANDED_OFF": "green",
                 "ABANDONED": "dim"}.get(it["state"])
        suffix = ""
        if it["state"] == "PARKED" and park.get("owner") != "human":
            color = "dim"           # SPEC-0222: the conductor's, not the human's
            suffix = " [red]![/]" if park.get("overdue") else ""
        campaign, extra = _campaign_cell(it), []
        if wide:
            extra = [it.get("size") or "-", it.get("tone") or "-", _profile_text(it)]
        elif it.get("size") or it.get("tone"):     # no room for columns: a dim line instead
            campaign.append("\n" + _size_tone(it) + " → " + str(it.get("profile") or "-"),
                            style="dim")
            campaign.append_text(_source_marker(it))
        table.add_row(campaign, R.project_text(it.get("project")),
                      f"[{color}]{state}[/]{suffix}" if color else state, *extra,
                      it["loops"], cost, _age_txt(it.get("updated_age_s")))
    parts = [table]
    needs_you = [it for it in active if it["state"] == "PARKED"
                 and (it.get("park") or {}).get("owner") == "human"]
    if needs_you:
        parts.append(_list_needs_you(needs_you))
    if waiting:
        parts.append(_waiting_table(waiting))
    return Group(*parts, _backlog_table(backlog))


def _list_needs_you(items: list) -> Table:
    """SPEC-0222: parks only the human can resolve."""
    table = Table(title=f"Needs you ({len(items)})", box=box.SIMPLE_HEAD, pad_edge=False,
                  title_style="yellow")
    table.add_column("campaign", min_width=24, no_wrap=True)
    table.add_column("reason", style="yellow", no_wrap=True)
    table.add_column("detail", overflow="fold")
    table.add_column("resume to", no_wrap=True)
    for it in items:
        park = it.get("park") or {}
        table.add_row(_campaign_cell(it), str(park.get("reason") or "?"),
                      Text(str(park.get("detail") or ""), style="dim"),
                      Text(str(park.get("resume_to") or ""), style=R.state_style(park.get("resume_to"))))
    return table


def _waiting_table(items: list) -> Table:
    """Started campaigns that need a slot (SPEC-0200): next in line, ahead of the backlog."""
    table = Table(title=f"Waiting for a slot ({len(items)})", box=box.SIMPLE_HEAD, pad_edge=False)
    table.add_column("campaign", min_width=24, no_wrap=True)
    table.add_column("project", no_wrap=True, overflow="ellipsis", max_width=18)
    table.add_column("continues at", no_wrap=True)
    table.add_column("size", no_wrap=True)
    table.add_column("pri", justify="right")
    table.add_column("loops")
    table.add_column("waiting", justify="right")
    for it in items:
        w = it.get("waiting") or {}
        table.add_row(_campaign_cell(it), R.project_text(it.get("project")),
                      Text(str(w.get("resume_to") or "?"), style=R.state_style(w.get("resume_to"))),
                      _size_tone(it), str(it.get("priority")), it["loops"], _age_txt(it.get("updated_age_s")))
    return table


def _backlog_table(items: list) -> Table:
    """The backlog section: `items` are `wt.queue.v1` queued items (each has `backlog`)."""
    table = Table(title=f"Backlog ({len(items)})", box=box.SIMPLE_HEAD, pad_edge=False)
    table.add_column("#", justify="right")
    table.add_column("campaign", min_width=24, no_wrap=True)
    table.add_column("project", no_wrap=True, overflow="ellipsis", max_width=18)
    table.add_column("size", no_wrap=True)
    table.add_column("pri", justify="right")
    table.add_column("waiting on")
    table.add_column("age")
    for it in items:
        b = it["backlog"]
        waiting = "[green]ready[/]" if b["ready"] else "; ".join(b["blocked_by"])
        table.add_row(str(b["position"]), _campaign_cell(it), R.project_text(it.get("project")),
                      _size_tone(it), str(b["priority"]),
                      waiting, _age_txt(it.get("age_s")))
    return table


# ---- dashboard (wt queue watch, SPEC-0185) ------------------------------------------------------

def _dur(s) -> str:
    """Compact duration: 45s, 4m12s, 2h05m, 3d4h."""
    if s is None:
        return "—"
    s = int(float(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 86400}d{s % 86400 // 3600}h"


def _bar(frac: float, width: int = 12, style: str = "green") -> Text:
    frac = max(0.0, min(1.0, frac))
    full = round(frac * width)
    if frac >= 0.9:
        style = "red"
    elif frac >= 0.6:
        style = "yellow"
    return Text.assemble(("━" * full, style), ("━" * (width - full), "grey30"))


def _spec_cell(spec_id, title) -> Text:
    t = Text(str(spec_id or "?"), style="bold")
    if title:
        t.append("  ")
        t.append(title, style="dim")
    return t


def _campaign_cell(it: dict) -> Text:
    """The campaign id; a merge request (SPEC-0233) also shows its `merge:<branch>` label."""
    t = Text(it["campaign"])
    if it.get("kind") == "merge" and it.get("label"):
        t.append("\n" + it["label"], style="dim")
    return t


def _title_cell(title) -> Text:
    return Text(title or "—", style="" if title else "dim")


def _ago(s) -> str:
    """Coarse age for tables: 45s, 11m, 3h, 2d."""
    if s is None:
        return "—"
    s = int(s)
    return (f"{s}s" if s < 60 else f"{s // 60}m" if s < 3600 else f"{s // 3600}h" if s < 86400
            else f"{s // 86400}d")


def _owner_chip(label: str, n, style: str) -> Text:
    """SPEC-0222: label first (`needs you 2`), so it reads as a sentence."""
    if not n:
        return Text(f"{label} 0", style="dim")
    return Text.assemble((label, style), " ", (str(n), f"bold {style}"))


def _conductor_chip(tot: dict) -> Text:
    n, overdue = tot.get("conductor_handling", 0), tot.get("conductor_overdue", 0)
    chip = Text(f"conductor {n}", style="dim")
    if overdue:
        chip.append(f" ({overdue} overdue)", style="red")
    return chip


def _chip(label: str, n, style: str) -> Text:
    return Text.assemble((f" {n} ", f"bold reverse {style}" if n else "dim reverse"),
                         (f" {label}", style if n else "dim"))


def _dash_header(p: dict) -> Panel:
    lease, slots, tot = p["lease"], p["slots"], p["totals"]
    if lease.get("held"):
        runner = Text.assemble(("● ", "red" if lease.get("stale") else "green"),
                               f"runner pid {lease.get('pid')}",
                               (f"  heartbeat {_dur(lease.get('heartbeat_age_s'))} ago", "dim"))
        if lease.get("stale"):
            runner.append("  STALE", style="bold red")
    else:
        runner = Text.assemble(("○ ", "red"), ("no loop running", "bold red"),
                               ("  (wt queue run)", "dim"))
    busy, total = slots.get("busy", 0), slots.get("total", 0) or 1
    slot_txt = Text.assemble(("slots ", "dim"), _bar(busy / total, width=max(total * 3, 6),
                                                    style="cyan"), f" {busy}/{slots.get('total')}")
    chips = Text(" ").join([
        _chip("active", tot["active"], "cyan"), _chip("waiting", tot.get("waiting", 0), "bright_blue"),
        _owner_chip("needs you", tot.get("needs_you", 0), "yellow"), _conductor_chip(tot),
        _chip("ready", tot["ready"], "green"), _chip("blocked", tot["blocked"], "magenta"),
        _chip("delivered", tot["delivered"], "green")])
    if tot.get("reviews_running"):                         # SPEC-0277
        chips.append(f" · reviews {tot['reviews_running']}", style="cyan")
    grid = Table.grid(expand=True, padding=(0, 2))
    grid.add_column(ratio=1)
    grid.add_column(justify="right", no_wrap=True)
    slot_txt.append_text(Text.assemble(("  today ", "dim"),
                                       (f"${tot['spend_today_usd']:.2f}", "bold")))
    grid.add_row(runner, slot_txt)
    rows = [grid, chips]
    by_project = tot.get("by_project") or {}
    if len(by_project) > 1 or p.get("project"):
        line = Text(" ")
        for i, (name, c) in enumerate(sorted(by_project.items())):
            if i:
                line.append("   ")
            line.append_text(R.project_text(name))
            bits = [f"{c[k]} {k.replace('_', ' ')}" for k in ("active", "waiting", "needs_you", "conductor", "backlog",
                                         "delivered")
                    if c.get(k)]
            line.append(" ▸ " + (" · ".join(bits) or "idle"), style="dim")
        rows.append(line)
    body = Group(*rows)
    when = (p.get("generated_at") or "")[11:19]
    scope = f" · {p['project']}" if p.get("project") else ""
    return Panel(body, title=Text(f"wt queue{scope}", style="bold"),
                 subtitle=Text(when, style="dim"), title_align="left",
                 subtitle_align="right", border_style="grey50", box=box.ROUNDED)


def _stepper(state: str, phase_index) -> Text:
    t = Text()
    for i, name in enumerate(Q.PHASES):
        if i:
            t.append(" › ", style="grey50")
        label = name.lower()
        if phase_index is None:
            t.append(label, style="dim")
        elif i < phase_index:
            t.append("✓ " + label, style="green")
        elif i == phase_index:
            t.append("● " + label, style=f"bold {R.state_style(state) or 'cyan'}")
        else:
            t.append(label, style="grey50")
    return t


def _activity_text(a: dict, state: str) -> Text:
    kind = (a or {}).get("kind")
    if kind == "run":
        t = Text.assemble(("▶ ", "bold cyan"), (str(a.get("persona")), "bold"))
        model = "/".join(x for x in (a.get("model"), a.get("effort")) if x)
        if model:
            t.append(f" · {model}", style="cyan")
        t.append(f" · run {a.get('run_id')}", style="dim")
        el, to = a.get("elapsed_s"), a.get("timeout_s")
        t.append(f" · {_dur(el)}")
        if to:
            t.append(f" / {_dur(to)}", style="dim")
            t.append(" ")
            t.append_text(_bar((el or 0) / float(to), width=8, style="cyan"))
        if a.get("max_budget_usd") is not None:
            t.append(f" · budget ${float(a['max_budget_usd']):.2f}", style="dim")
        return t
    if kind == "gates":
        return Text.assemble(("⚙ ", "bold blue"), ("running gates" if state == "GATING"
                                                    else "closing (re-gate, rebase, merge)"))
    return Text("· idle (waiting for the next tick)", style="dim")


def _gate_text(g) -> Text:
    if not g:
        return Text("no gate run yet", style="dim")
    t = Text(f"#{g.get('gate_seq')} ", style="dim")
    for i, r in enumerate(g.get("results") or []):
        if i:
            t.append("  ")
        ok = r.get("ok")
        t.append("✓ " if ok else "✗ ", style="green" if ok else "red")
        t.append(str(r.get("name")), style="" if ok else "bold red")
        t.append(f" {_dur(r.get('duration_s'))}", style="dim")
    return t


def _verdict_text(v) -> Text:
    if not v:
        return Text("no verdict yet", style="dim")
    status = str(v.get("status") or "?")
    style = {"done": "green", "blocked": "red", "needs_info": "yellow"}.get(status, "magenta")
    t = Text.assemble((status, f"bold {style}"), (f" ({v.get('persona')})", "dim"))
    if v.get("summary"):
        t.append("  ")
        t.append(v["summary"])
    return t


def _reviews_text(chips) -> Text:
    """SPEC-0277: one chip per reviewer persona — short name, then `▶ 3m`, `✓ 92`,
    `✗ 2mf/1mj` (must_fix/major counts), `! failed` or `·`."""
    t = Text()
    for i, c in enumerate(chips or []):
        if i:
            t.append("  ")
        t.append(Q.review_short_name(c["persona"]) + " ", style="dim")
        status, counts = c["status"], c.get("counts") or {}
        if status == "running":
            t.append(f"▶ {_dur(c.get('elapsed_s'))}", style="cyan")
        elif status == "pass":
            t.append("✓" + (f" {c['score']}" if c.get("score") is not None else ""), style="green")
        elif status == "fix":
            t.append(f"✗ {counts.get('must_fix', 0)}mf/{counts.get('major', 0)}mj", style="red")
        elif status == "failed":
            t.append("! failed", style="red")
        else:
            t.append("·", style="dim")
    return t


def _active_panel(it: dict) -> Panel:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="dim", no_wrap=True, justify="right")
    grid.add_column(overflow="fold")
    grid.add_row("spec", _spec_cell(it.get("label") or it["spec_id"] or it.get("idea_id"),
                                    it.get("spec_title")))
    where = R.project_text(it.get("project"))
    where.append(f" · {_size_tone(it)} → {it.get('profile')}", style="dim")
    where.append_text(_source_marker(it))
    where.append(f" · {it.get('branch') or 'no branch yet'}", style="dim")
    grid.add_row("project", where)
    grid.add_row("phase", _stepper(it["state"], it.get("phase_index")))
    grid.add_row("now", _activity_text(it.get("activity"), it["state"]))
    cap = it.get("cap_usd")
    cost = Text(f"${it['cost_usd']:.2f}", style="bold")
    est = (it.get("cost") or {}).get("estimated_usd") or 0.0
    if est > 0:
        cost.append(f" (~${est:.2f} est.)", style="dim")
    if cap:
        cost.append(f" / ${float(cap):.2f} ", style="dim")
        cost.append_text(_bar(it["cost_usd"] / float(cap)))
    cost.append(f"   loops {it['loops']} · runs {it['runs']} · age {_dur(it.get('age_s'))}",
                style="dim")
    grid.add_row("cost", cost)
    grid.add_row("gates", _gate_text(it.get("last_gate")))
    grid.add_row("verdict", _verdict_text(it.get("last_verdict")))
    if it.get("reviews"):
        grid.add_row("reviews", _reviews_text(it["reviews"]))
    events = it.get("recent_events") or []
    if events:
        grid.add_row("events", Group(*[R.campaign_event(ev) for ev in events]))
    state = it["state"]
    if it.get("park_requested"):
        state += " · park requested"
    return Panel(grid, title=Text.assemble((it["campaign"], "bold"), "  ",
                                           R.project_text(it.get("project")), "  ",
                                           (state, R.state_style(it["state"]))),
                 title_align="left", border_style=R.state_style(it["state"]) or "cyan",
                 box=box.ROUNDED)


def _needs_you_table(items: list) -> Table:
    t = R.section_table(f"Needs you ({len(items)})", border_style="yellow")
    t.add_column("campaign", no_wrap=True, style="bold")
    t.add_column("project", no_wrap=True, overflow="ellipsis", max_width=18)
    t.add_column("title", no_wrap=True, overflow="ellipsis", ratio=1, min_width=8)
    t.add_column("reason", style="yellow", no_wrap=True)
    t.add_column("detail", overflow="fold", ratio=3)
    t.add_column("resume to", no_wrap=True)
    t.add_column("for", justify="right", no_wrap=True)
    for it in items:
        park = it.get("park") or {}
        reason = park.get("reason") or "?"
        t.add_row(_campaign_cell(it), R.project_text(it.get("project")),
                  _title_cell(it.get("spec_title")), reason,
                  Text(str(park.get("detail") or ""), style="dim"),
                  Text(str(park.get("resume_to") or ""), style=R.state_style(park.get("resume_to"))),
                  _ago(it.get("park_age_s")))
    return t


def _decision_cell(it: dict) -> Text:
    """SPEC-0222: the conductor's latest note and its age, `overdue`, or `pending`."""
    park = it.get("park") or {}
    decision = park.get("decision")
    if decision:
        text = " ".join(str(decision.get("text") or "").split())
        if len(text) > 120:
            text = text[:119] + "…"
        t = Text(text, style="dim")
        ago = _ago(_note_age(decision.get("ts")))
        t.append(f" · {ago} ago", style="dim")
        return t
    age = _dur(it.get("park_age_s"))
    if park.get("overdue"):
        return Text(f"overdue {age}", style="bold red")
    return Text(f"pending {age}", style="dim")


def _note_age(ts):
    import datetime as dt
    try:
        t = dt.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.astimezone()
    return max(0, int((dt.datetime.now().astimezone() - t).total_seconds()))


def _conductor_table(items: list) -> Table:
    t = R.section_table(f"Conductor handling ({len(items)})", border_style="grey50")
    t.add_column("campaign", no_wrap=True, style="dim")
    t.add_column("project", no_wrap=True, overflow="ellipsis", max_width=18)
    t.add_column("title", no_wrap=True, overflow="ellipsis", ratio=1, min_width=8, style="dim")
    t.add_column("reason", no_wrap=True, style="dim")
    t.add_column("decision", overflow="fold", ratio=3)
    t.add_column("for", justify="right", no_wrap=True, style="dim")
    for it in items:
        park = it.get("park") or {}
        reason = park.get("reason") or "park requested"
        t.add_row(_campaign_cell(it), R.project_text(it.get("project")),
                  _title_cell(it.get("spec_title")), reason, _decision_cell(it),
                  _ago(it.get("park_age_s")))
    return t


def _dash_waiting(items: list) -> Table:
    t = R.section_table(f"Waiting for a slot ({len(items)})", border_style="bright_blue")
    t.add_column("campaign", no_wrap=True)
    t.add_column("project", no_wrap=True, overflow="ellipsis", max_width=18)
    t.add_column("title", no_wrap=True, overflow="ellipsis", ratio=1, min_width=8)
    t.add_column("size", no_wrap=True)
    t.add_column("continues at", no_wrap=True)
    t.add_column("pri", justify="right")
    t.add_column("cost", justify="right", no_wrap=True)
    t.add_column("for", justify="right", no_wrap=True)
    for it in items:
        t.add_row(_campaign_cell(it), R.project_text(it.get("project")), _title_cell(it.get("spec_title")),
                  _size_tone(it), Text(str(it.get("resume_to") or "?"), style=R.state_style(it.get("resume_to"))),
                  str(it.get("priority")), f"${it['cost_usd']:.2f}", _ago(it.get("waiting_s")))
    return t


def _dash_backlog(items: list):
    if not items:
        return Group(Text("Backlog (0)", style="bold"),
                     Text("  empty — wt queue add SPEC-NNNN --yes", style="dim"))
    t = R.section_table(f"Backlog ({len(items)})")
    t.add_column("#", justify="right", style="dim")
    t.add_column("campaign", no_wrap=True)
    t.add_column("project", no_wrap=True, overflow="ellipsis", max_width=18)
    t.add_column("title", no_wrap=True, overflow="ellipsis", ratio=1, min_width=8)
    t.add_column("size", no_wrap=True)
    t.add_column("pri", justify="right")
    t.add_column("status", overflow="fold", ratio=2, min_width=14)
    t.add_column("waiting", justify="right", no_wrap=True)
    for b in items:
        status = (Text("● ready", style="green") if b.get("ready")
                  else Text.assemble(("◌ waiting on ", "yellow"),
                                     ("; ".join(b.get("blocked_by") or []), "bold yellow")))
        pri = b.get("priority")
        t.add_row(str(b.get("position")), _campaign_cell(b), R.project_text(b.get("project")),
                  _title_cell(b.get("spec_title")), _size_tone(b),
                  Text(str(pri), style="bold" if pri is not None and pri <= 2 else "dim"),
                  status, _ago(b.get("age_s")))
    return t


def _dash_delivered(items: list, hours: int, width: int = 120):
    wide = width >= 110
    t = R.section_table(f"Recently delivered ({len(items)}, last {hours}h)")
    t.add_column("ago", justify="right", no_wrap=True, style="dim")
    t.add_column("campaign", no_wrap=True)
    t.add_column("project", no_wrap=True, overflow="ellipsis", max_width=18)
    t.add_column("title", no_wrap=True, overflow="ellipsis", ratio=1, min_width=8)
    t.add_column("outcome", no_wrap=True)
    if wide:
        t.add_column("ref", no_wrap=True, overflow="ellipsis")
    t.add_column("cost", justify="right", no_wrap=True)
    if wide:
        t.add_column("took", justify="right", no_wrap=True)
    glyph = {"MERGED": "✓ merged", "HANDED_OFF": "↗ handed off", "ABANDONED": "✗ abandoned"}
    for d in items:
        ref = d.get("pr_url") or ((d.get("merged_sha") or "")[:7])
        outcome = Text(glyph.get(d["state"], d["state"]), style=R.state_style(d["state"]))
        cells = [_ago(d.get("finished_age_s")), d["campaign"], R.project_text(d.get("project")),
                 _title_cell(d.get("spec_title")),
                 outcome] + ([ref] if wide else []) + [f"${d['cost_usd']:.2f}"]
        t.add_row(*cells + ([_dur(d.get("duration_s"))] if wide else []))
    if not items:
        return Group(t, Text("  nothing in this window", style="dim"))
    return t


def _dashboard(p: dict, width: int = 120, recent_hours: int = 24) -> Group:
    """The `wt queue watch` frame for a `wt.queue.dashboard.v1` payload."""
    parts = [_dash_header(p)]
    active = [_active_panel(it) for it in p["active"]]
    parts.append(Text.assemble((f"Active ({len(active)})", "bold")))
    if not active:
        parts.append(Text("  No active campaigns", style="dim"))
    elif width >= 140 and len(active) > 1:
        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        for i in range(0, len(active), 2):
            grid.add_row(*(active[i:i + 2] + ([Text("")] if i + 1 >= len(active) else [])))
        parts.append(grid)
    else:
        parts.extend(active)
    if p.get("needs_you"):
        parts.append(_needs_you_table(p["needs_you"]))
    if p.get("conductor_handling"):
        parts.append(_conductor_table(p["conductor_handling"]))
    if p.get("waiting"):
        parts.append(_dash_waiting(p["waiting"]))
    parts.append(_dash_backlog(p["backlog"]))
    parts.append(_dash_delivered(p["delivered"], recent_hours, width))
    return Group(*parts)


def _journal_lines(rec: dict) -> Group:
    """One runner-journal record: a dim timestamp + pid, then what happened."""
    head = Text.assemble((str(rec.get("ts", ""))[:19], "dim"), " ",
                         (f"pid {rec.get('pid')}", "dim"), " ")
    kind = rec.get("kind")
    if kind == "start":
        head.append("▶ start", style="bold green")
        head.append(f"  every {rec.get('interval_s')}s", style="dim")
        return Group(head)
    if kind == "stop":
        head.append("■ stop", style="bold")
        head.append(f"  {rec.get('reason')} after {rec.get('ticks')} tick(s)", style="dim")
        return Group(head)
    if kind == "crash":
        head.append("✗ crash", style="bold red")
        head.append(f"  {rec.get('error')}", style="red")
        return Group(head)
    if kind == "config_reloaded":                          # SPEC-0226
        changed = list(rec.get("changed") or [])
        keys = [k for k in changed if k != "…"]
        more = len(keys) > 6 or len(keys) < len(changed)
        head.append(f"⟳ config reloaded ({rec.get('scope')}): ", style="bold cyan")
        head.append(", ".join(keys[:6]) + (", …" if more else ""))
        if rec.get("restart_required"):
            head.append(f"  restart required: {', '.join(rec['restart_required'])}",
                        style="yellow")
        return Group(head)
    if kind == "config_load_failed":
        head.append(f"✗ config load failed ({rec.get('scope')}): ", style="bold red")
        head.append(f"{rec.get('error')}", style="red")
        return Group(head)
    if kind == "reexec_deferred":                          # SPEC-0226
        head.append("⏸ re-exec deferred", style="bold yellow")
        head.append(f"  {rec.get('reason')} (code at {str(rec.get('to_sha'))[:8]})")
        return Group(head)
    if kind == "admission_blocked":                        # SPEC-0268
        held = ", ".join(f"{h.get('campaign')} {h.get('state')}"
                         + (" deferred" if h.get("deferred") else "")
                         for h in rec.get("holders") or [] if isinstance(h, dict))
        head.append(f"⏳ admission blocked: {len(rec.get('ready') or [])} ready, slots "
                    f"{rec.get('occupied')}/{rec.get('slots')} (held: {held})",
                    style="yellow")
        return Group(head)
    if rec.get("drain") and rec.get("tick") is not None:
        label = f"tick {rec['tick']} (drain)"
    else:
        label = "drain" if rec.get("drain") else f"tick {rec.get('tick')}"
    head.append(label, style="cyan")
    if rec.get("admitted"):
        head.append(f"  admitted {rec['admitted']}", style="green")
    rows = [head] + [Text("  ") + R.tick_row(row) for row in rec.get("steps") or []]
    rows += [Text("  ") + R.started_row(row) for row in rec.get("started") or []]
    rows += [Text(f"  ! {e}", style="red") for e in rec.get("errors") or []]
    return Group(*rows)


def _gc_table(rows: list) -> Table:
    """SPEC-0275: one row per worktree / orphan dir / branch and what gc does with it."""
    table = Table(title=f"gc ({len(rows)})", box=box.SIMPLE_HEAD, pad_edge=False)
    table.add_column("kind", no_wrap=True)
    table.add_column("target", overflow="fold")
    table.add_column("campaign", no_wrap=True)
    table.add_column("action", no_wrap=True)
    table.add_column("why", overflow="fold")
    styles = {"remove": "green", "delete": "green", "keep": "dim"}
    for r in rows:
        why = r["why"] + (f" — FAILED: {r['error']}" if r.get("error") else "")
        table.add_row(r["kind"], r.get("path") or r.get("branch") or "",
                      f"{r['campaign']} ({r['campaign_state']})" if r["campaign"] else "",
                      Text(r["action"], style=styles.get(r["action"], "")),
                      Text(why, style="red" if r.get("error") else ""))
    return table


def _print_plan(title: str, plan: dict) -> None:
    rev = plan.get("revision")
    if isinstance(rev, dict) and rev.get("pending"):                   # SPEC-0279
        plan = {**plan, "revision": (f"pending ({len(rev.get('findings') or [])} findings), "
                                     "spec_writer amends first")}
    elif isinstance(rev, dict):                                        # SPEC-0230
        plan = {**plan, "revision": (f"review {str(rev.get('review_sha'))[:12]} -> spec "
                                     f"{str(rev.get('current_sha'))[:12]}, "
                                     f"{len(rev.get('findings') or [])} findings")}
    console.print(R.plan(title, plan), highlight=False)


def attach(queue_group):
    """Register the queue subcommands on `queue_group` (SPEC-0173 / SPEC-0176)."""

    @queue_group.command("gates")
    @click.argument("campaign", shell_complete=C.complete_campaign_id)
    @click.option("--json", "as_json", is_flag=True, help="emit wt.campaign.gate.v1 JSON")
    @click.pass_obj
    def queue_gates(cfg, campaign, as_json):
        """Run the project's gates on a campaign's worktree now (debugging a parked item).

        Logs one `gate` event; never changes the campaign's state or `last_gate`, so a debug
        run cannot steer the queue.
        """
        try:
            state = store.read_state(cfg, campaign)
            project_cfg = P.project_config(cfg, state.get("project") or "")
        except ValueError as e:
            raise click.ClickException(str(e))
        worktree = (state.get("repo") or {}).get("worktree")
        if not worktree or not os.path.isdir(worktree):
            raise click.ClickException(
                f"campaign {campaign} has no worktree on disk ({worktree or 'none recorded'})")
        try:
            run = G.run_and_log(cfg, state, project_cfg, by="human")
        except ValueError as e:
            raise click.ClickException(str(e))
        if as_json:
            _dump_json({"schema": "wt.campaign.gate.v1", "campaign": campaign,
                        **G.as_payload(run)})
            return
        table = Table(title=f"gates #{run.seq} · {campaign} · {'ok' if run.ok else 'FAILED'}")
        for col in ("gate", "required", "result", "duration", "log"):
            table.add_column(col)
        for r in run.results:
            if r.timed_out:
                res = "[red]timeout[/]"
            elif r.ok:
                res = "[green]ok[/]"
            else:
                res = f"[red]exit {r.exit_code}[/]"
            table.add_row(r.name, "yes" if r.required else "no", res, f"{r.duration_s:.1f}s",
                          r.log)
        console.print(table)
        for r in G.blocking_failures(run):
            console.print(f"[red]✗[/] {r.name} — last lines:", highlight=False)
            R.print_code("\n".join(r.tail.splitlines()[-10:]) + "\n", "text", console=console)

    # ---- SPEC-0176 -------------------------------------------------------------------------

    @queue_group.command("add")
    @click.argument("spec_id", shell_complete=C.complete_any_spec_id)
    @click.option("--project", shell_complete=C.complete_project,
                  help="project (default: Meta-Tools for SPEC-NNNN, the outbox project otherwise; "
                       "the idea's :PROJECT: for IDEA-NNN)")
    @click.option("--profile", default=None, help="workflow profile (default: agentic.default_profile)")
    @click.option("--base", default=None, help="base branch (default: the project's merge.base)")
    @click.option("--force", is_flag=True, help="also admit an in-progress spec")
    @click.option("--priority", type=int, default=Q.PRIORITY_DEFAULT, show_default=True,
                  help="backlog priority 1..5 (1 = admitted first)")
    @click.option("--yes", "do_write", is_flag=True, help="create the campaign (default is dry-run)")
    @click.option("--json", "as_json", is_flag=True, help="emit wt.queue.add.v1 JSON")
    @click.pass_obj
    def queue_add(cfg, spec_id, project, profile, base, force, priority, do_write, as_json):
        """Queue an accepted spec, or an idea, as a new campaign (dry-run unless --yes).

        \b
        wt queue add SPEC-0170            show the plan
        wt queue add SPEC-0170 --yes      create the campaign in QUEUED
        wt queue add IDEA-063 --yes       queue an idea: its campaign opens into SPEC_WRITING
        """
        try:
            if Q._IDEA_ID_RE.match((spec_id or "").strip()):
                plan = Q.plan_add_idea(cfg, spec_id, project=project, profile=profile,
                                       base=base, priority=priority)
                if do_write:
                    Q.apply_add_idea(cfg, plan)
            else:
                plan = Q.plan_add(cfg, spec_id, project=project, profile=profile, base=base,
                                  force=force, priority=priority)
                if do_write:
                    Q.apply_add(cfg, plan)
        except ValueError as e:
            raise click.ClickException(str(e))
        if as_json:
            _dump_json({"schema": "wt.queue.add.v1", **plan, "written": bool(do_write)})
            return
        _print_plan(f"queue {spec_id}", plan)
        if do_write:
            console.print(f"[green]✓[/] {plan['campaign']}  QUEUED  [dim]{plan['worktree']}[/]")
        else:
            console.print(_NOT_WRITTEN)

    @queue_group.command("merge")
    @click.argument("branch")
    @click.option("--project", shell_complete=C.complete_project,
                  help="project (default: the one whose repo the cwd is in, else Meta-Tools)")
    @click.option("--worktree", default=None, type=click.Path(),
                  help="the branch's worktree (default: its existing worktree, else a new one)")
    @click.option("--profile", default=None, help="workflow profile (sizes the cap and slots)")
    @click.option("--priority", type=int, default=Q.MERGE_PRIORITY_DEFAULT, show_default=True,
                  help="backlog priority 1..5 (1 = admitted first)")
    @click.option("--note", default=None, help="why this merge (shown in the queue)")
    @click.option("--by", "requested_by", default="human", show_default=True,
                  help="who asks: human, conductor, steward:<Project>")
    @click.option("--docs-only", "docs_only", is_flag=True,
                  help="skip the gates; only if every changed path is in merge.docs_only_paths")
    @click.option("--yes", "do_write", is_flag=True, help="create the campaign (default is dry-run)")
    @click.option("--json", "as_json", is_flag=True, help="emit wt.queue.merge.v1 JSON")
    @click.pass_obj
    def queue_merge(cfg, branch, project, worktree, profile, priority, note, requested_by,
                    docs_only, do_write, as_json):
        """Queue a local branch for a rebase + re-gate + ff-only merge (dry-run unless --yes).

        \b
        wt queue merge fix/typo           show the plan
        wt queue merge fix/typo --yes     create the merge campaign in QUEUED
        wt queue merge fix/doc --docs-only    gate-free, for allowlisted docs paths only
        """
        try:
            plan = Q.plan_merge(cfg, branch, project=project, worktree=worktree, profile=profile,
                                priority=priority, note=note, requested_by=requested_by,
                                docs_only=docs_only)
            if do_write:
                Q.apply_merge(cfg, plan, by=requested_by)
        except ValueError as e:
            raise click.ClickException(str(e))
        if as_json:
            _dump_json({"schema": "wt.queue.merge.v1", **plan, "written": bool(do_write)})
            return
        _print_plan(f"queue merge {branch}", plan)
        if do_write:
            console.print(f"[green]✓[/] {plan['campaign']}  QUEUED  [dim]{plan['worktree']}[/]")
        else:
            console.print(_NOT_WRITTEN)

    @queue_group.command("list")
    @click.option("--all", "include_terminal", is_flag=True, help="include terminal campaigns")
    @click.option("--project", shell_complete=C.complete_project, help="filter by project")
    @click.option("--state", shell_complete=C.complete_campaign_state, help="filter by state")
    @click.option("--json", "as_json", is_flag=True, help="emit wt.queue.v1 JSON")
    @click.pass_obj
    def queue_list(cfg, include_terminal, project, state, as_json):
        """What is in flight, in which state, at what cost — plus the lease and the slots."""
        payload = Q.queue_payload(cfg, include_terminal=include_terminal, project=project,
                                  state=state)
        if as_json:
            _dump_json(payload)
            return
        console.print(_queue_table(payload))

    @queue_group.command("backlog")
    @click.option("--json", "as_json", is_flag=True, help="emit wt.queue.backlog.v1 JSON")
    @click.pass_obj
    def queue_backlog(cfg, as_json):
        """The inactive backlog in admission order: priority, then age; what each item waits on."""
        items = Q.backlog_view(cfg)
        if as_json:
            _dump_json({"schema": "wt.queue.backlog.v1", "items": items})
            return
        table = _backlog_table([{"campaign": b["campaign"], "spec_id": b["spec_id"] or b.get("idea_id"),
                                 "kind": b.get("kind"), "label": b.get("label"),
                                 "project": b.get("project"),
                                 "age_s": b["age_s"], "backlog": b} for b in items])
        console.print(table)

    @queue_group.command("prioritize")
    @click.argument("campaign", shell_complete=C.complete_campaign_id)
    @click.argument("priority", type=int)
    @click.option("--by", default=None,
                  help="who acts (default: $WT_MAILBOX_ADDR, then human)")
    @click.option("--yes", "do_write", is_flag=True, help="apply (default is dry-run)")
    @click.option("--json", "as_json", is_flag=True, help="emit wt.queue.prioritize.v1 JSON")
    @click.pass_obj
    def queue_prioritize(cfg, campaign, priority, by, do_write, as_json):
        """Set a QUEUED or PARKED campaign's backlog priority (1..5, 1 = admitted first)."""
        try:
            plan = Q.plan_prioritize(cfg, campaign, priority, by=_actor(cfg, by))
            if do_write:
                Q.apply_prioritize(cfg, plan)
        except ValueError as e:
            raise click.ClickException(str(e))
        if as_json:
            _dump_json({"schema": "wt.queue.prioritize.v1", **plan, "written": bool(do_write)})
            return
        _print_plan(f"prioritize {campaign}", plan)
        console.print(f"[green]✓[/] {campaign} priority {plan['from']} → {plan['to']}"
                      if do_write else _NOT_WRITTEN)

    @queue_group.command("watch")
    @click.option("--interval", type=float, default=2.0, show_default=True,
                  help="refresh interval in seconds")
    @click.option("--project", shell_complete=C.complete_project, help="only this project")
    @click.option("--recent", type=int, default=5, show_default=True,
                  help="how many recently delivered campaigns to show")
    @click.option("--hours", "recent_hours", type=int, default=24, show_default=True,
                  help="the 'recently delivered' window")
    @click.option("--once", is_flag=True, help="print one frame and exit (no live refresh)")
    @click.option("--json", "as_json", is_flag=True, help="emit wt.queue.dashboard.v1 JSON once")
    @click.option("--iterations", type=int, default=0, hidden=True,
                  help="stop after N refreshes (0 = until Ctrl-C)")
    @click.pass_obj
    def queue_watch(cfg, interval, project, recent, recent_hours, once, as_json, iterations):
        """Live dashboard: the runner, active campaigns (what each is doing now, gates, verdict,
        per-reviewer review chips, cost), what needs attention, the backlog and recently
        delivered work (Ctrl-C to stop).

        Review chips: `▶ 3m` running, `✓ 92` pass with score, `✗ 2mf/1mj` fix with
        must_fix/major counts, `! failed` no valid verdict, `·` not run."""
        def payload():
            return Q.dashboard_payload(cfg, project=project, recent=recent,
                                       recent_hours=recent_hours)

        if as_json:
            _dump_json(payload())
            return
        if once:
            console.print(_dashboard(payload(), console.width, recent_hours))
            return
        from rich.live import Live
        n = 0
        # the alternate screen redraws in place (no scroll-back ghosts) and restores the
        # terminal on exit; `--once` is the frame to keep
        with Live(_dashboard(payload(), console.width, recent_hours), console=console,
                  refresh_per_second=4, screen=console.is_terminal) as live:
            while True:
                n += 1
                if iterations and n >= iterations:
                    break
                time.sleep(interval)
                live.update(_dashboard(payload(), console.width, recent_hours))

    @queue_group.command("tick")
    @click.option("--json", "as_json", is_flag=True, help="emit wt.queue.tick.v1 JSON")
    @click.pass_obj
    def queue_tick(cfg, as_json):
        """One iteration of the loop under the lease (admit, open, run steps, close)."""
        try:
            lease = L.acquire(cfg, ["wt", "queue", "tick"])
        except ValueError as e:
            raise click.ClickException(str(e))
        try:
            report = Q.tick(cfg)
        finally:
            lease.release()
        if as_json:
            _dump_json(report.payload())
            return
        for row in report.steps:
            console.print(R.tick_row(row), highlight=False)
        if not report.steps:
            console.print("[dim]nothing to do[/]" if report.idle else "[dim]no step this tick[/]")

    @queue_group.command("run")
    @click.option("--once", is_flag=True, help="one tick, then exit")
    @click.option("--max-ticks", type=int, default=None, help="exit after N ticks")
    @click.option("--poll-interval", type=float, default=None,
                  help="seconds between ticks (default: agentic.poll_interval_s)")
    @click.option("--until-idle", is_flag=True, help="exit when nothing is in flight")
    @click.option("--json-lines", "json_lines", is_flag=True,
                  help="one wt.queue.tick.v1 object per tick on stdout")
    @click.option("--no-reexec", "no_reexec", is_flag=True,
                  help="keep running old code when a merge changes wt (default: re-exec)")
    @click.pass_obj
    def queue_run(cfg, once, max_ticks, poll_interval, until_idle, json_lines, no_reexec):
        """The loop: tick, heartbeat, sleep. One process at a time (lease-guarded).

        Agent steps carry across ticks (SPEC-0213): a tick starts steps and applies the ones
        that finished, so a slow step never holds up admission or other campaigns.
        SIGINT/SIGTERM stop admissions, end running gates, and return. Persona runs keep
        running and the next `wt queue run` adopts them (SPEC-0225/0229). If worker threads
        are still stuck after the 30 s bound, the traceback (if any) is printed to stderr and
        the process exits with status 70. --once / --max-ticks: persona runs still running at
        exit are left running; the next `wt queue run` collects them. When a commit changes
        wt's own code, the loop keeps admitting while it drains (no new gate or close), then
        re-executes itself, keeping the lease (SPEC-0194/0229).
        """
        def on_tick(report):
            if json_lines:
                sys.stdout.write(json.dumps(report.payload(), ensure_ascii=False) + "\n")
                sys.stdout.flush()
                return
            for row in report.steps:
                console.print(R.tick_row(row), highlight=False)
            for row in report.started:            # SPEC-0213: its result lands in a later tick
                console.print(R.started_row(row), highlight=False)

        try:
            ticks = Q.run(cfg, once=once, max_ticks=max_ticks, poll_interval_s=poll_interval,
                          until_idle=until_idle, on_tick=on_tick,
                          argv=["wt", "queue", "run"], reexec=not (once or no_reexec),
                          config_loader=load_config)
        except ValueError as e:
            if Q.leftover_threads():       # SPEC-0229: a stuck thread would hang the exit
                import traceback
                traceback.print_exc()
                _exit_leftover()
            raise click.ClickException(str(e))
        except BaseException:
            if Q.leftover_threads():       # SPEC-0229: no stale thread outlives the lease
                import traceback
                traceback.print_exc()
                _exit_leftover()
            raise
        if Q.leftover_threads():
            _exit_leftover()
        if not json_lines:
            console.print(f"[dim]{ticks} tick(s); lease released[/]")

    @queue_group.command("runner-log")
    @click.option("--tail", type=int, default=30, show_default=True,
                  help="last N records (0 = all)")
    @click.option("--json", "as_json", is_flag=True, help="emit wt.runner.log.v1 JSON")
    @click.pass_obj
    def queue_runner_log(cfg, tail, as_json):
        """The runner's own journal (`runner.jsonl`, SPEC-0192): starts, stops, crashes and
        every tick that did something — what `wt queue run` did, without reading free text."""
        recs = Q.read_journal(cfg, tail=tail or None)
        if as_json:
            _dump_json({"schema": "wt.runner.log.v1", "path": str(Q.runner_journal_path(cfg)),
                        "records": recs})
            return
        if not recs:
            console.print(f"[dim]no runner records yet ({Q.runner_journal_path(cfg)})[/]")
            return
        for rec in recs:
            console.print(_journal_lines(rec), highlight=False)

    @queue_group.command("park")
    @click.argument("campaign", shell_complete=C.complete_campaign_id)
    @click.option("--reason", required=True, help="why (recorded on the campaign)")
    @click.option("--by", default=None,
                  help="who acts (default: $WT_MAILBOX_ADDR, then human)")
    @click.option("--yes", "do_write", is_flag=True, help="apply (default is dry-run)")
    @click.option("--json", "as_json", is_flag=True, help="emit wt.queue.park.v1 JSON")
    @click.pass_obj
    def queue_park(cfg, campaign, reason, by, do_write, as_json):
        """Park a campaign. With a loop running, the current step finishes first."""
        try:
            plan = Q.plan_park(cfg, campaign, reason, by=_actor(cfg, by))
            if do_write:
                Q.apply_park(cfg, plan)
        except ValueError as e:
            raise click.ClickException(str(e))
        if as_json:
            _dump_json({"schema": "wt.queue.park.v1", **plan, "written": bool(do_write)})
            return
        _print_plan(f"park {campaign}", plan)
        console.print(f"[green]✓[/] {campaign} park {plan['mode']}" if do_write else _NOT_WRITTEN)

    @queue_group.command("escalate")
    @click.argument("campaign", required=False, shell_complete=C.complete_campaign_id)
    @click.option("--question", required=True, help="what the human must decide")
    @click.option("--option", "options", multiple=True, help="a numbered choice (repeatable)")
    @click.option("--recommend", type=int, default=None, help="the recommended option, 1-based")
    @click.option("--fact", "facts", multiple=True, help="a fact the decision rests on")
    @click.option("--from", "frm", default=None,
                  help="who escalates (default: $WT_MAILBOX_ADDR, then conductor)")
    @click.option("--yes", "do_write", is_flag=True, help="apply (default is dry-run)")
    @click.option("--json", "as_json", is_flag=True, help="emit wt.queue.escalate.v1 JSON")
    @click.pass_obj
    def queue_escalate(cfg, campaign, question, options, recommend, facts, frm, do_write,
                       as_json):
        """Escalate a decision to the human (SPEC-0232): an ESC record, the campaign parked as
        `human_decision`, mail to `human` and a desktop notification. Without CAMPAIGN the
        escalation is about the runner or the queue as a whole and parks nothing."""
        from . import escalations as ESC
        try:
            plan = ESC.plan_escalate(cfg, campaign, question=question, options=options,
                                     recommend=recommend, facts=facts, frm=frm)
            rec = ESC.apply_escalate(cfg, plan) if do_write else None
        except (ValueError, OSError) as e:
            raise click.ClickException(str(e))
        if as_json:
            _dump_json({"schema": "wt.queue.escalate.v1", **plan, "written": bool(do_write),
                        "escalation": rec})
            return
        shown = {**plan, "options": "; ".join(f"{o['n']}. {o['label']}" for o in plan["options"]),
                 "park": plan["park"] and f"{plan['park']['mode']} → human_decision"}
        _print_plan("escalate" + (f" {campaign}" if campaign else ""), shown)
        console.print(f"[green]✓[/] {rec['id']} opened" if do_write else _NOT_WRITTEN)

    @queue_group.command("resume")
    @click.argument("campaign", shell_complete=C.complete_campaign_id)
    @click.option("--to", type=click.Choice(Q.RESUMABLE_STATES), default=None,
                  help="state to resume to (default: where it parked)")
    @click.option("--note", default=None, help="a note for the next implementer run")
    @click.option("--raise-cap", type=float, default=None, help="new campaign cost cap in USD")
    @click.option("--reset-loops", is_flag=True, help="reset the loop counters")
    @click.option("--by", default=None,
                  help="who acts (default: $WT_MAILBOX_ADDR, then human)")
    @click.option("--yes", "do_write", is_flag=True, help="apply (default is dry-run)")
    @click.option("--json", "as_json", is_flag=True, help="emit wt.queue.resume.v1 JSON")
    @click.pass_obj
    def queue_resume(cfg, campaign, to, note, raise_cap, reset_loops, by, do_write, as_json):
        """Resume a PARKED campaign (optionally with a note, a higher cap, fresh loops)."""
        try:
            plan = Q.plan_resume(cfg, campaign, to=to, note=note, raise_cap=raise_cap,
                                 reset_loops=reset_loops, by=_actor(cfg, by))
            if do_write:
                Q.apply_resume(cfg, plan)
        except ValueError as e:
            raise click.ClickException(str(e))
        if as_json:
            _dump_json({"schema": "wt.queue.resume.v1", **plan, "written": bool(do_write)})
            return
        _print_plan(f"resume {campaign}", plan)
        console.print(f"[green]✓[/] {campaign} → {plan['to']}" if do_write else _NOT_WRITTEN)

    @queue_group.command("abandon")
    @click.argument("campaign", shell_complete=C.complete_campaign_id)
    @click.option("--keep-worktree", is_flag=True, help="leave the worktree on disk")
    @click.option("--by", default=None,
                  help="who acts (default: $WT_MAILBOX_ADDR, then human)")
    @click.option("--yes", "do_write", is_flag=True, help="apply (default is dry-run)")
    @click.option("--json", "as_json", is_flag=True, help="emit wt.queue.abandon.v1 JSON")
    @click.pass_obj
    def queue_abandon(cfg, campaign, keep_worktree, by, do_write, as_json):
        """Abandon a PARKED or QUEUED campaign (removes its worktree; keeps the branch)."""
        try:
            plan = Q.plan_abandon(cfg, campaign, keep_worktree=keep_worktree,
                                  by=_actor(cfg, by))
            if do_write:
                Q.apply_abandon(cfg, plan)
        except ValueError as e:
            raise click.ClickException(str(e))
        if as_json:
            _dump_json({"schema": "wt.queue.abandon.v1", **plan, "written": bool(do_write)})
            return
        _print_plan(f"abandon {campaign}", plan)
        console.print(f"[green]✓[/] {campaign} ABANDONED" if do_write else _NOT_WRITTEN)

    @queue_group.command("gc")
    @click.option("--project", shell_complete=C.complete_project, help="filter by project")
    @click.option("--yes", "do_write", is_flag=True, help="apply (default is dry-run)")
    @click.option("--json", "as_json", is_flag=True, help="emit wt.queue.gc.v1 JSON")
    @click.pass_context
    def queue_gc(ctx, project, do_write, as_json):
        """Garbage-collect worktrees and merged branches of finished campaigns (SPEC-0275).
        Never touches dirty worktrees, unmerged branches, live campaigns, the main checkout
        or the cwd's worktree. Exits 1 when a --yes row or repo failed."""
        cfg = ctx.obj
        try:
            plan = Q.plan_gc(cfg, project=project)
            if do_write:
                Q.apply_gc(cfg, plan)
        except ValueError as e:
            raise click.ClickException(str(e))
        failed = sum(r.get("done") is False for r in plan["rows"]) if do_write else 0
        if as_json:
            _dump_json({"schema": "wt.queue.gc.v1", **plan, "written": bool(do_write)})
            if failed or (do_write and plan["errors"]):
                ctx.exit(1)
            return
        console.print(_gc_table(plan["rows"]), highlight=False)
        for err in plan["errors"]:
            console.print(f"[red]![/] {err}", highlight=False)
        summary = (f"{plan['worktrees_removable']} worktrees, {plan['branches_removable']} "
                   f"branches removable")
        if not do_write:
            console.print(f"{summary} (dry-run; --yes to apply)")
            return
        console.print(f"[green]✓[/] {summary}; applied" + (f", [red]{failed} failed[/]" if failed else ""))
        if failed or plan["errors"]:
            ctx.exit(1)

    return queue_group
