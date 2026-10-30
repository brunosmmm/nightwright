"""`wt queue conductor-cost` and `wt queue conductor-session` (SPEC-0299): what the conductor's
own Claude Code session costs. Kept apart from `cli_conduct.py` so the cost report and the
alert stream can change independently."""
from __future__ import annotations

import datetime as dt

import click
from rich.table import Table

from .. import render as R
from ..console import console
from ..report import _dump_json
from . import conductor_cost as CC


def _date(ctx, param, value):
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise click.BadParameter("expected YYYY-MM-DD")


def _usd(v) -> str:
    return "-" if v is None else f"${v:,.2f}"


def _k(v) -> str:
    return "-" if v is None else f"{v / 1000:,.0f}k"


def summary_line(rep) -> str:
    t, c = rep["totals"], rep["context"]
    line = (f"{t['turns']} turns · {_usd(t['usd'])} · median context {_k(c['median'])} · "
            f"{_usd(rep['usd_per_merge'])} per merge ({rep['merges']} merges)")
    if not t["usd_complete"]:
        line += f" (partial: {t['unpriced_turns']} unpriced turns)"
    return line


def attach(queue_group):
    """Register `conductor-cost` and `conductor-session` on the `queue` group."""

    @queue_group.command("conductor-cost")
    @click.option("--since", callback=_date, default=None, help="first local day, YYYY-MM-DD")
    @click.option("--until", callback=_date, default=None,
                  help="last local day, YYYY-MM-DD (inclusive)")
    @click.option("--session", "session_ids", multiple=True,
                  help="session id (repeatable); overrides the registry")
    @click.option("--include-subagents", is_flag=True,
                  help="also report sub-agent transcripts, separately")
    @click.option("--json", "as_json", is_flag=True, help="emit wt.conductor.cost.v1 JSON")
    @click.pass_obj
    def queue_conductor_cost(cfg, since, until, session_ids, include_subagents, as_json):
        """The conductor's tokens and dollars (at agentic.conductor.prices), per day and per
        merged campaign, plus the context size per turn."""
        rep = CC.cost_report(cfg, since=since, until=until, session_ids=list(session_ids) or None,
                             include_subagents=include_subagents)
        if as_json:
            _dump_json(rep)
            return
        if not rep["sessions"]:
            console.print("[dim]no conductor session registered; arm the conductor or run "
                          "`wt queue conductor-session add ID`[/]")
            return
        for s in rep["sessions"]:
            if not s["found"]:
                console.print(f"[yellow]no transcript for session {s['session_id']}[/]")
        console.print(summary_line(rep), highlight=False)
        if rep["unpriced_models"]:
            console.print("[yellow]no price for: " + ", ".join(rep["unpriced_models"])
                          + " (set agentic.conductor.prices)[/]")
        if rep["by_day"]:
            t = Table(title="By day")
            for col in ("date", "turns", "usd", "cache read", "merges"):
                t.add_column(col)
            for d in rep["by_day"]:
                t.add_row(d["date"], str(d["turns"]), _usd(d["usd"]), f"{d['cache_read']:,}",
                          str(d["merges"]))
            console.print(t)
        if rep["campaigns"]:
            t = Table(title=f"Top {CC.TOP_CAMPAIGNS} campaigns by conductor share")
            for col in ("campaign", "spec", "outcome", "conductor", "personas"):
                t.add_column(col)
            for c in rep["campaigns"][:CC.TOP_CAMPAIGNS]:
                t.add_row(c["campaign"], str(c["spec_id"] or "-"), str(c["outcome"] or "live"),
                          _usd(c["conductor_usd"]), _usd(c["persona_usd"]))
            console.print(t)
        console.print(f"idle (no live campaign): {_usd(rep['idle_usd'])} · "
                      f"personas in window: {_usd(rep['persona_usd'])}", highlight=False)
        if rep["subagents"]:
            s = rep["subagents"]
            console.print(f"sub-agents (not conductor context): {s['turns']} turns · "
                          f"{_usd(s['usd'])}", highlight=False)

    @queue_group.group("conductor-session")
    def conductor_session():
        """The registry of Claude Code sessions that run a reader (the conductor)."""

    @conductor_session.command("add")
    @click.argument("session_id")
    @click.option("--reader", default="conductor", show_default=True)
    @click.pass_obj
    def session_add(cfg, session_id, reader):
        """Backfill a session by hand (source: manual)."""
        if CC.find_transcript(cfg, session_id) is None:
            R.warn(f"warning: no transcript found for session {session_id}")
        rec = CC.record_session(cfg, reader, {}, source="manual", session_id=session_id)
        if rec is None:
            console.print(f"[dim]{reader}/{session_id} is already registered[/]", highlight=False)
        else:
            console.print(f"registered {reader}/{session_id}", highlight=False)

    @conductor_session.command("list")
    @click.option("--reader", default="conductor", show_default=True)
    @click.option("--json", "as_json", is_flag=True, help="emit the registry as JSON")
    @click.pass_obj
    def session_list(cfg, reader, as_json):
        """Show the registered sessions."""
        rows = CC.sessions(cfg, reader)
        if as_json:
            _dump_json({"schema": "wt.conductor.sessions.v1", "sessions": rows})
            return
        if not rows:
            console.print("[dim]no sessions registered[/]")
            return
        t = Table(title=f"Sessions of {reader}")
        for col in ("session", "since", "source", "pid", "cwd"):
            t.add_column(col)
        for r in rows:
            t.add_row(r["session_id"], str(r.get("ts")), str(r.get("source")),
                      str(r.get("claude_pid") or "-"), str(r.get("cwd") or "-"))
        console.print(t)

    return queue_group
