"""`wt queue events` (SPEC-0181) and `wt queue conduct-status` (SPEC-0182): the conductor's
alert stream and its one-call situation report. Kept apart from `cli_queue.py` so the queue's
own commands and these can change independently."""
from __future__ import annotations

import click
from rich.table import Table

from .. import render as R
from ..console import console
from ..report import _dump_json
from . import events as E
from . import queue as Q
from . import store
from .cli_queue import _dur


def conduct_status(cfg, *, recent=10) -> dict:
    """`wt.conduct.status.v1`: what a freshly woken conductor needs, from facts on disk."""
    E.sync(cfg)
    payload = Q.queue_payload(cfg)
    items = payload["items"]
    try:
        from . import mailbox as M
        unanswered = M.unanswered(cfg, "conductor")
    except (ImportError, AttributeError):
        unanswered = []
    return {
        "schema": "wt.conduct.status.v1",
        "lease": payload["lease"],
        "slots": payload["slots"],
        "conductor_pid": E.reader_holder(cfg, "conductor"),
        "stewards": [{"address": f["reader"][len("mailbox:"):], "pid": f["pid"],
                      "lag": f["lag"]}
                     for f in E.followers(cfg) if f["kind"] == "mailbox" and f["held"]],
        "counts": {
            "active": sum(1 for i in items if i["state"] in Q.ACTIVE_STATES),
            "queued": sum(1 for i in items if i["state"] == "QUEUED"),
            "parked": sum(1 for i in items if i["state"] == "PARKED"),
        },
        "parked": [{"campaign": i["campaign"], "spec_id": i["spec_id"], "project": i["project"],
                    "park": i["park"], "cost_usd": i["cost_usd"], "loops": i["loops"]}
                   for i in items if i["state"] == "PARKED"],
        "active": [{"campaign": i["campaign"], "spec_id": i["spec_id"], "state": i["state"],
                    "loops": i["loops"], "cost_usd": i["cost_usd"],
                    "updated_age_s": i["updated_age_s"]}
                   for i in items if i["state"] in Q.ACTIVE_STATES],
        "unanswered_mail": unanswered,
        "recent_events": E.read_since(cfg, 0)[-recent:],
    }


def attach(queue_group):
    """Register `events` and `conduct-status` on the `queue` group."""

    @queue_group.command("events")
    @click.option("--follow", "do_follow", is_flag=True,
                  help="stream new events forever (one line each; for Monitor)")
    @click.option("--for", "reader", default=None,
                  help="reader name for --follow (cursor + one live follower per name)")
    @click.option("--since", type=int, default=None, help="events after this id")
    @click.option("--type", "types", multiple=True, help="only these event types (repeatable)")
    @click.option("--tail", type=int, default=None, help="last N events (history mode)")
    @click.option("--text", "as_text", is_flag=True, help="one-line summaries instead of JSON")
    @click.option("--from-start", is_flag=True,
                  help="a new --follow reader replays history instead of starting at now")
    @click.option("--poll", "poll_s", type=float, default=None, help="poll interval seconds")
    @click.pass_obj
    def queue_events(cfg, do_follow, reader, since, types, tail, as_text, from_start, poll_s):
        """The queue's alert stream: parks, merges, failed runs, watchdog signals, mail.

        \b
        wt queue events --tail 20                       recent history
        wt queue events --follow --for conductor        what the conductor's Monitor runs
        """
        if do_follow:
            if not reader:
                raise click.UsageError("--follow needs --for READER (e.g. conductor)")
            try:
                E.follow(cfg, reader, as_json=not as_text, poll_s=poll_s,
                         types=list(types) or None, from_start=from_start)
            except ValueError as e:
                raise click.ClickException(str(e))
            except (KeyboardInterrupt, BrokenPipeError):
                pass
            return
        E.sync(cfg)
        evs = E.read_since(cfg, since or 0, types=list(types) or None)
        if tail:
            evs = evs[-tail:]
        if as_text:
            for ev in evs:
                console.print(R.queue_event(ev), highlight=False)
            return
        _dump_json({"schema": "wt.events.v1", "events": evs})

    @queue_group.command("conduct-status")
    @click.option("--json", "as_json", is_flag=True, help="emit wt.conduct.status.v1 JSON")
    @click.pass_obj
    def queue_conduct_status(cfg, as_json):
        """One-call situation report for a woken conductor: lease, slots, parked items,
        active work, unanswered conductor mail, recent events."""
        st = conduct_status(cfg)
        if as_json:
            _dump_json(st)
            return
        lease = st["lease"]
        lease_txt = (f"runner pid {lease['pid']} (heartbeat {lease['heartbeat_age_s']}s)"
                     if lease["held"] else "[red]runner not running[/]")
        cond = f"conductor pid {st['conductor_pid']}" if st["conductor_pid"] else \
            "[yellow]no conductor armed[/]"
        c = st["counts"]
        console.print(f"[bold]Conduct status[/] · {lease_txt} · {cond} · "
                      f"active {c['active']} · queued {c['queued']} · parked {c['parked']}")
        if st["stewards"]:
            console.print("stewards: " + ", ".join(
                f"{s['address']} (pid {s['pid']}, lag {s['lag']})" for s in st["stewards"]),
                highlight=False)
        else:
            console.print("stewards: [dim]none armed[/]")
        if st["parked"]:
            t = Table(title="Parked")
            for col in ("campaign", "spec", "reason", "detail", "cost"):
                t.add_column(col)
            for p in st["parked"]:
                park = p["park"] or {}
                t.add_row(p["campaign"], p["spec_id"], str(park.get("reason")),
                          str(park.get("detail") or "")[:60], f"${p['cost_usd']:.2f}")
            console.print(t)
        if st["unanswered_mail"]:
            t = Table(title="Unanswered conductor mail")
            for col in ("id", "from", "kind", "text"):
                t.add_column(col)
            for m in st["unanswered_mail"]:
                t.add_row(str(m.get("id")), str(m.get("from")), str(m.get("kind")),
                          str(m.get("text") or "")[:70])
            console.print(t)
        if st["recent_events"]:
            t = Table(title="Recent events")
            for col in ("#", "type", "campaign", "data"):
                t.add_column(col)
            for ev in st["recent_events"]:
                data = " ".join(f"{k}={v}" for k, v in (ev.get("data") or {}).items()
                                if v not in (None, "", []))
                t.add_row(str(ev["id"]), ev["type"], str(ev.get("campaign") or "-"), data[:70])
            console.print(t)

    @queue_group.command("followers")
    @click.option("--json", "as_json", is_flag=True, help="emit wt.queue.followers.v1 JSON")
    @click.pass_obj
    def queue_followers(cfg, as_json):
        """Who is armed: every event/mailbox reader with its live pid, cursor, lag and age."""
        rows = E.followers(cfg)
        if as_json:
            _dump_json({"schema": "wt.queue.followers.v1", "followers": rows})
            return
        if not rows:
            console.print("[dim]no followers[/]")
            return
        t = Table(title="Followers")
        for col in ("reader", "kind", "pid", "cursor", "lag", "cursor age"):
            t.add_column(col)
        for r in rows:
            lag = f"[yellow]{r['lag']}[/]" if r["lag"] > 0 else str(r["lag"])
            t.add_row(r["reader"], r["kind"], str(r["pid"]) if r["held"] else "[dim]not armed[/]",
                      str(r["after"]), lag, _dur(r["cursor_age_s"]))
        console.print(t)

    return queue_group
