"""`nw queue gate-slots` (SPEC-0309): who holds the host-wide gate slots and who waits for one.
Kept apart from `cli_queue.py` so the pool and the queue's commands can change independently."""
from __future__ import annotations

import datetime as dt

import click
from rich.table import Table

from ..console import console
from ..report import _dump_json
from . import events as E
from . import gate_slots as GS
from .cli_queue import _dur


def _age(since):
    return E._age_s(since, dt.datetime.now().astimezone())


def attach(queue_group):
    """Register `gate-slots` on the `queue` group."""

    @queue_group.command("gate-slots")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.queue.gate_slots.v1 JSON")
    @click.pass_obj
    def queue_gate_slots(cfg, as_json):
        """Show the host-wide gate-slot pool: holders and waiters (SPEC-0309)."""
        occ = GS.occupancy(cfg)
        if as_json:
            _dump_json({"schema": "nw.queue.gate_slots.v1", **occ})
            return
        console.print(f"gate slots: {len(occ['held'])}/{occ['slots']} held, "
                      f"{len(occ['waiting'])} waiting", highlight=False)
        if not occ["held"] and not occ["waiting"]:
            console.print("[dim]no holders, no waiters[/]")
            return
        t = Table(title="Gate slots")
        for col in ("role", "slot", "campaign", "pid", "for"):
            t.add_column(col)
        for h in occ["held"]:
            t.add_row("holds", str(h["slot"]), str(h["campaign"]), str(h["pid"]),
                      _dur(_age(h["since"])))
        for w in occ["waiting"]:
            t.add_row("waits", "—", str(w["campaign"]), str(w["pid"]), _dur(_age(w["since"])))
        console.print(t)

    return queue_group
