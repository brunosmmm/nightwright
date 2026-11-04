"""`nw queue conductor-cost` and `nw queue conductor-session` (SPEC-0299): what the conductor's
own Claude Code session costs; `nw queue decision`, `decisions` and `conductor-brief`
(SPEC-0301): its decision log and state handoff. Kept apart from `cli_conduct.py` so the cost
report and the alert stream can change independently."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

import click
from rich.table import Table

from .. import render as R
from ..console import console
from ..report import _dump_json
from . import brief as BR
from . import conductor_cost as CC
from . import decisions as D
from . import events as E
from . import mvp_report as MVP
from . import store as S


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


GUARD_MESSAGE = ("conductor never asks the human: use steward:<P> / nw queue escalate "
                 "(SPEC-0382)")


def _hook_session_id(raw):
    """The `session_id` of a PreToolUse hook payload, else `$CLAUDE_CODE_SESSION_ID`."""
    try:
        doc = json.loads(raw) if raw and raw.strip() else {}
    except ValueError:
        doc = {}
    sid = doc.get("session_id") if isinstance(doc, dict) else None
    return sid if isinstance(sid, str) and sid else os.environ.get("CLAUDE_CODE_SESSION_ID")


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
    @click.option("--json", "as_json", is_flag=True, help="emit nw.conductor.cost.v1 JSON")
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
                          "`nw queue conductor-session add ID`[/]")
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

    @queue_group.command("mvp-report")
    @click.option("--window", "n", type=click.IntRange(min=1), default=MVP.DEFAULT_WINDOW,
                  show_default=True, help="the last N merged campaigns")
    @click.option("--since", default=None,
                  help="only campaigns opened at or after this ISO date/datetime")
    @click.option("--project", default=None, help="only campaigns of this project")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.conductor.mvp.v1 JSON")
    @click.option("--check", is_flag=True, help="exit 1 unless the window qualifies and E1-E6 pass")
    @click.pass_obj
    def queue_mvp_report(cfg, n, since, project, as_json, check):
        """SPEC-0298's exit criteria E1-E6 over the last N merged campaigns."""
        try:
            start = D.parse_since(since) if since else None
        except ValueError as e:
            raise click.ClickException(str(e))
        rep = MVP.report(cfg, n=n, since=start, project=project)
        if as_json:
            _dump_json(rep)
        else:
            console.print(MVP.render_text(rep), highlight=False, markup=False, soft_wrap=True)
        if check and not rep["ok"]:
            raise SystemExit(1)

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
            _dump_json({"schema": "nw.conductor.sessions.v1", "sessions": rows})
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

    @queue_group.command("conductor-guard")
    @click.pass_obj
    def conductor_guard(cfg):
        """PreToolUse hook (SPEC-0382): deny the tool call, through the hook's JSON decision on
        stdout, when the calling session is a registered conductor. Always exits 0 (exit 2 is
        what Claude Code treats as a block, and click also uses it for usage errors); stays silent
        for any other session. Reads the hook JSON on stdin; fails open on anything unreadable
        so it can never block a human's session."""
        try:
            sid = _hook_session_id(sys.stdin.read())
            hit = bool(sid) and cfg is not None and any(
                r["session_id"] == sid for r in CC.sessions(cfg, "conductor"))
        except Exception:
            hit = False
        if hit:
            click.echo(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": GUARD_MESSAGE}}))

    @queue_group.command("decision")
    @click.argument("words", nargs=-1, metavar="[CAMPAIGN] RATIONALE")
    @click.option("--kind", required=True, help="one of: " + ", ".join(D.KINDS))
    @click.option("--rule", required=True,
                  help="the decision-table row id (<event> or parked.<reason>)")
    @click.option("--event", "event_id", type=int, default=None, help="the queue event id")
    @click.option("--action", default=None, help="what was done")
    @click.option("--by", default="conductor", show_default=True,
                  help="conductor, queue or steward:<P>")
    @click.option("--no-campaign", is_flag=True,
                  help="do not infer a campaign from --event or the text")
    @click.pass_obj
    def queue_decision(cfg, words, kind, rule, event_id, action, by, no_campaign):
        """Record a conductor or queue decision (applies at once, like `nw campaign note`).
        With CAMPAIGN it also lands as a ledger note; without it, the campaign of --event, or
        the one campaign the text names (SPEC-0431), else a queue-level decision."""
        if not 1 <= len(words) <= 2:
            raise click.UsageError("expected [CAMPAIGN] RATIONALE")
        campaign, rationale = (words[0] if len(words) == 2 else None), words[-1]
        if campaign and no_campaign:
            raise click.UsageError("--no-campaign conflicts with an explicit CAMPAIGN")
        if campaign is None:
            try:
                S.read_state(cfg, rationale)
            except (ValueError, OSError):
                pass
            else:                               # a campaign id alone: the rationale was forgotten
                raise click.ClickException(
                    f"{rationale!r} is a campaign id, not a rationale; "
                    f"pass the rationale after it: nw queue decision {rationale} … RATIONALE")
        campaign_from = None
        if campaign is None and not no_campaign:
            ev = (E.read_since(cfg, event_id - 1)[:1] if event_id is not None and event_id >= 1
                  else [])
            ev_campaign = ev[0].get("campaign") if ev and ev[0].get("id") == event_id else None
            if ev_campaign:                     # a mirrored mail event may carry a bogus campaign
                try:
                    S.read_state(cfg, ev_campaign)
                except (ValueError, OSError, TypeError):    # TypeError: a non-string value
                    ev_campaign = None
            if ev_campaign:
                campaign, campaign_from = ev_campaign, "event"
            else:
                campaign = S.sole_campaign_named(cfg, f"{rationale} {action or ''}")
                campaign_from = "text" if campaign else None
        try:
            rec = D.record(cfg, kind=kind, rule=rule, rationale=rationale, campaign=campaign,
                           event_id=event_id, action=action, by=by, campaign_from=campaign_from)
        except ValueError as e:
            raise click.ClickException(str(e))
        console.print(f"decision #{rec['seq']} {kind} ({rule})"
                      + (f" on {campaign}" if campaign else "")
                      + (f" (campaign inferred from {campaign_from})" if campaign_from else ""),
                      highlight=False, markup=False)

    @queue_group.command("decisions")
    @click.option("--campaign", default=None)
    @click.option("--since", default=None, help="ISO date/datetime, or relative like 2h / 1d")
    @click.option("--tail", type=int, default=None, help="last N records")
    @click.option("--parks", is_flag=True, help="each park with its covering decision, and ratio")
    @click.option("--json", "as_json", is_flag=True, help="emit JSON")
    @click.pass_obj
    def queue_decisions(cfg, campaign, since, tail, parks, as_json):
        """The decision log (nw.conductor.decisions.v1), or with --parks the park coverage."""
        try:
            start = D.parse_since(since) if since else None
        except ValueError as e:
            raise click.ClickException(str(e))
        if parks:
            cov = D.park_coverage(cfg, since=start)
            if campaign:
                cov["items"] = [x for x in cov["items"] if x["campaign"] == campaign]
                cov["missing"] = [x for x in cov["missing"] if x["campaign"] == campaign]
                cov["parks"] = len(cov["items"])
                cov["covered"] = cov["parks"] - len(cov["missing"])
                cov["ratio"] = cov["covered"] / cov["parks"] if cov["parks"] else None
            if as_json:
                _dump_json({"schema": "nw.conductor.park_coverage.v1", **cov})
                return
            for x in cov["items"]:
                d = x["decision"]
                console.print(f"{x['ts']}  {x['campaign']}  {x['reason']}  -> "
                              + (f"#{d['seq']} {d['kind']} ({d['rule']})" if d else "missing"),
                              highlight=False, markup=False)
            ratio = "-" if cov["ratio"] is None else f"{cov['ratio']:.0%}"
            console.print(f"{cov['covered']}/{cov['parks']} parks decided ({ratio})",
                          highlight=False, markup=False)
            return
        rows = D.read(cfg, campaign=campaign, since=start)
        if tail:
            rows = rows[-tail:]
        if as_json:
            _dump_json({"schema": "nw.conductor.decisions.v1", "decisions": rows})
            return
        if not rows:
            console.print("[dim]no decisions[/]")
            return
        for r in rows:
            console.print(f"#{r['seq']} {r['ts']} {r['by']} {r.get('campaign') or '-'} "
                          f"{r['kind']} ({r['rule']}) {r['rationale']}", highlight=False,
                          markup=False, soft_wrap=True)

    @queue_group.command("conductor-brief")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.conductor.brief.v1 JSON")
    @click.pass_obj
    def queue_conductor_brief(cfg, as_json):
        """What a fresh or compacted conductor needs to continue, in a few thousand tokens:
        runner, parked items with their last decision, open dispatches, escalations, mail."""
        brief = BR.conductor_brief(cfg)
        if as_json:
            _dump_json(brief)
            return
        console.print(BR.render_text(brief), highlight=False, markup=False, soft_wrap=True)

    return queue_group
