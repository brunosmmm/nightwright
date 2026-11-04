"""`nw dashboard snapshot [--json]` (SPEC-0401): the remote dashboard's `nw.dashboard.v1`
document, built in-process. No server, no tailscale, no token; writes nothing."""
from __future__ import annotations

import json
import sys

import click


@click.group("dashboard")
def dashboard_group():
    """Remote web dashboard (SPEC-0400): the live queue view over Tailscale.

    \b
    nw dashboard snapshot              health, counts, spend and what needs you (text)
    nw dashboard snapshot --json       the full nw.dashboard.v1 document
    """


def _err(v) -> str | None:
    return v["error"] if isinstance(v, dict) and set(v) == {"error"} else None


def _num(v) -> str:
    return "?" if v is None else f"{v:g}" if isinstance(v, float) else str(v)


def text_lines(doc: dict) -> list[str]:
    """The short text summary: overall health, counts, spend, then what needs the human."""
    out = []
    h = doc.get("health") or {}
    runner, cond = h.get("runner"), h.get("conductor")
    if _err(runner):
        parts = [f"runner: error: {_err(runner)}"]
    else:
        lease = runner.get("lease") or {}
        if lease.get("held"):
            who = f"{lease.get('pid')}@{lease.get('host')}" if lease.get("host") \
                else str(lease.get("pid"))
            parts = [f"runner {who} heartbeat {_num(lease.get('heartbeat_age_s'))}s"]
            if lease.get("stale"):
                parts.append("stale lease")
        else:
            parts = ["runner down"]
        if runner.get("draining"):
            parts.append("draining")
        if (runner.get("runner_code") or {}).get("stale"):
            parts.append("stale code")
    if _err(cond):
        parts.append(f"conductor: error: {_err(cond)}")
    else:
        parts.append(f"conductor {cond.get('pid') or 'down'}")
    out.append(" · ".join(parts))
    totals = doc.get("totals")
    if _err(totals):
        out.append(f"totals: error: {_err(totals)}")
    else:
        out.append(f"active {totals.get('active', 0)} · queued {totals.get('ready', 0)} · "
                   f"parked {totals.get('attention', 0)} · waiting {totals.get('waiting', 0)} · "
                   f"backlog {totals.get('backlog', 0)} · "
                   f"conductor-handling {totals.get('conductor_handling', 0)}")
        out.append(f"spend today ${float(totals.get('spend_today_usd') or 0):.2f}")
    for name in ("slots", "gate_pools", "active", "waiting", "backlog", "delivered",
                 "recent_events"):
        if _err(doc.get(name)):
            out.append(f"{name}: error: {_err(doc[name])}")
    inbox = doc.get("inbox") or {}
    esc, parks = inbox.get("escalations"), inbox.get("parks")
    needs = 0
    if _err(esc):
        out.append(f"inbox.escalations: error: {_err(esc)}")
    else:
        for e in esc:
            out.append(f"{e.get('id')} {e.get('campaign') or '-'} {e.get('question')}")
            needs += 1
    if _err(parks):
        out.append(f"inbox.parks: error: {_err(parks)}")
    else:
        for p in parks:
            park = p.get("park") or {}
            out.append(f"park {p.get('label') or p.get('campaign')} {p.get('campaign')} "
                       f"{park.get('reason')}: {park.get('detail') or ''}".rstrip(": "))
            needs += 1
    if not needs and not _err(esc) and not _err(parks):
        out.append("nothing needs you")
    return out


@dashboard_group.command("snapshot")
@click.option("--json", "as_json", is_flag=True, help="emit the nw.dashboard.v1 document")
@click.pass_obj
def snapshot_cmd(cfg, as_json):
    """Build the dashboard snapshot in-process and print it (read-only; no server needed).

    Prints the overall health line (runner, conductor), the counts, today's spend and every
    open escalation and human-owned park. A section whose builder failed prints as
    `<section>: error: <message>`; the command still exits 0.
    """
    from .dashboard import snapshot as SN
    try:
        doc = SN.build(cfg)
    except Exception as e:                         # noqa: BLE001 - no config, no data dir
        click.echo(f"nw dashboard snapshot: {type(e).__name__}: {e}", err=True)
        sys.exit(1)
    if as_json:
        click.echo(json.dumps(doc, indent=2, ensure_ascii=False, default=str))
        return
    for line in text_lines(doc):
        click.echo(line)
