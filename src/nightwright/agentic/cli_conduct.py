"""`nw queue events` (SPEC-0181) and `nw queue conduct-status` (SPEC-0182): the conductor's
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
from . import quiet as QT
from . import store
from .cli_queue import _dur


def conduct_status(cfg, *, recent=10) -> dict:
    """`nw.conduct.status.v1`: what a freshly woken conductor needs, from facts on disk."""
    E.sync(cfg)
    payload = Q.queue_payload(cfg)
    items = payload["items"]
    try:
        from . import mailbox as M
        unanswered = M.unanswered(cfg, "conductor")
    except (ImportError, AttributeError):
        unanswered = []
    return {
        "schema": "nw.conduct.status.v1",
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
        "parked": [{"campaign": i["campaign"], "spec_id": i["spec_id"] or i["idea_id"],
                    "label": i["label"], "project": i["project"],
                    "park": i["park"], "cost_usd": i["cost_usd"], "loops": i["loops"]}
                   for i in items if i["state"] == "PARKED"],
        "active": [{"campaign": i["campaign"], "spec_id": i["spec_id"] or i["idea_id"],
                    "label": i["label"], "state": i["state"],
                    "loops": i["loops"], "cost_usd": i["cost_usd"],
                    "updated_age_s": i["updated_age_s"]}
                   for i in items if i["state"] in Q.ACTIVE_STATES],
        "unanswered_mail": unanswered,
        "recent_events": E.read_since(cfg, 0)[-recent:],
        "herdr_watch": _herdr_watch_status(cfg),             # SPEC-0365
        "launch": _launch_freshness(cfg),                    # SPEC-0424
    }


def _launch_freshness(cfg) -> list:
    try:
        from . import launch as LA
        return LA.freshness(cfg)
    except Exception:
        return []


def _herdr_supervisor(cfg):
    """SPEC-0365: the conductor follower's `nw herdr watch` supervisor, or None (gated off,
    or anything at all went wrong: the event stream never depends on it)."""
    try:
        from . import herdr_autowatch as AW
        sup = AW.Supervisor(cfg)
        return sup if sup.start() else None
    except Exception:
        return None


def _herdr_watch_status(cfg) -> dict | None:
    try:
        from . import herdr_autowatch as AW
        return AW.status(cfg)
    except Exception:
        return None


def attach(queue_group):
    """Register `events` and `conduct-status` on the `queue` group."""

    @queue_group.command("events")
    @click.option("--follow", "do_follow", is_flag=True,
                  help="stream new events forever (one line each; for Monitor)")
    @click.option("--for", "reader", default=None,
                  help="reader name for --follow (cursor + one live follower per name)")
    @click.option("--since", type=int, default=None, help="events after this id")
    @click.option("--type", "types", multiple=True, help="only these event types (repeatable)")
    @click.option("--campaign", "campaign", default=None,
                  help="only this campaign's events (history mode)")
    @click.option("--tail", type=int, default=None, help="last N events (history mode)")
    @click.option("--text", "as_text", is_flag=True, help="one-line summaries instead of JSON")
    @click.option("--from-start", is_flag=True,
                  help="a new --follow reader replays history instead of starting at now")
    @click.option("--poll", "poll_s", type=float, default=None, help="poll interval seconds")
    @click.option("--no-code-watch", is_flag=True,
                  help="--follow never exits when nightwright's code changes (default: it prints "
                       "`code_changed` and exits 0 so the Monitor owner re-arms)")
    @click.option("--all", "show_all", is_flag=True,
                  help="--follow delivers every event: no quiet mode, no digest (SPEC-0300)")
    @click.pass_obj
    def queue_events(cfg, do_follow, reader, since, types, campaign, tail, as_text, from_start,
                     poll_s, no_code_watch, show_all):
        """The queue's alert stream: parks, merges, failed runs, watchdog signals, mail.

        \b
        nw queue events --tail 20                       recent history
        nw queue events --follow --for conductor        what the conductor's Monitor runs
        """
        if do_follow:
            if not reader:
                raise click.UsageError("--follow needs --for READER (e.g. conductor)")
            sup = _herdr_supervisor(cfg) if reader == "conductor" else None
            try:
                E.follow(cfg, reader, as_json=not as_text, poll_s=poll_s,
                         types=list(types) or None, from_start=from_start,
                         **({"code_root": None} if no_code_watch else {}),
                         **({"stop": sup.as_stop_hook()} if sup is not None else {}),
                         quiet=QT.enabled_for(cfg, reader) and not show_all)
            except ValueError as e:
                raise click.ClickException(str(e))
            except (KeyboardInterrupt, BrokenPipeError):
                pass
            finally:
                if sup is not None:
                    sup.stop()
            return
        E.sync(cfg)
        evs = E.read_since(cfg, since or 0, types=list(types) or None)
        if campaign:
            evs = [e for e in evs if e.get("campaign") == campaign]
        if tail:
            evs = evs[-tail:]
        if as_text:
            for ev in evs:
                console.print(R.queue_event(ev), highlight=False)
            return
        _dump_json({"schema": "nw.events.v1", "events": evs})

    @queue_group.command("conduct-status")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.conduct.status.v1 JSON")
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
        if st.get("herdr_watch"):
            from . import herdr_autowatch as AW
            console.print(AW.status_line(st["herdr_watch"]), highlight=False)
        if st["parked"]:
            t = Table(title="Parked")
            for col in ("campaign", "spec", "reason", "detail", "cost"):
                t.add_column(col)
            for p in st["parked"]:
                park = p["park"] or {}
                t.add_row(p["campaign"], p["label"], str(park.get("reason")),
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

    @queue_group.command("wakeups")
    @click.option("--since", default=None, help="first day, YYYY-MM-DD (inclusive)")
    @click.option("--until", default=None, help="last day, YYYY-MM-DD (inclusive)")
    @click.option("--reader", default="conductor", show_default=True)
    @click.option("--json", "as_json", is_flag=True, help="emit nw.conductor.wakeups.v1 JSON")
    @click.pass_obj
    def queue_wakeups(cfg, since, until, reader, as_json):
        """Conductor wakeups (SPEC-0300): lines delivered vs events held, per merge."""
        try:
            rep = QT.wakeups_report(cfg, since=since, until=until, reader=reader)
        except ValueError as e:
            raise click.UsageError(str(e))
        if as_json:
            _dump_json(rep)
            return
        per = rep["wakeups_per_merge"]
        console.print(f"[bold]Wakeups[/] ({reader}) · delivered {rep['delivered']} "
                      f"(digests {rep['digests']}) · held {rep['held']} · merges {rep['merges']}"
                      f" · per merge {'-' if per is None else per}", highlight=False)
        if rep["by_type"]:
            t = Table(title="By type")
            for col in ("type", "wake", "hold"):
                t.add_column(col)
            for typ, row in sorted(rep["by_type"].items(), key=lambda kv: str(kv[0])):
                t.add_row(str(typ), str(row["wake"]), str(row["hold"]))
            console.print(t)

    @queue_group.command("followers")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.queue.followers.v1 JSON")
    @click.pass_obj
    def queue_followers(cfg, as_json):
        """Who is armed: every event/mailbox reader with its live pid, cursor, lag and age."""
        rows = E.followers(cfg)
        if as_json:
            _dump_json({"schema": "nw.queue.followers.v1", "followers": rows})
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
