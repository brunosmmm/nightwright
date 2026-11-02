"""`nw queue gate-slots` (SPEC-0309): who holds the host-wide gate slots and who waits for one.
Kept apart from `cli_queue.py` so the pool and the queue's commands can change independently."""
from __future__ import annotations

import datetime as dt

import click
from rich.table import Table

from ..console import console
from ..report import _dump_json
from . import events as E
from . import gate_progress as GPG
from . import gate_slots as GS
from . import store
from .cli_queue import _dur


def _age(since):
    return E._age_s(since, dt.datetime.now().astimezone())


def _progress(cfg, h, remote: bool) -> dict | None:
    """SPEC-0373: percent of a held slot's running gate."""
    try:
        return GPG.progress(cfg, h["campaign"], since=h.get("since"), elapsed_s=_age(h.get("since")),
                            events=store.read_ledger(cfg, h["campaign"], types=["gate"]),
                            remote=remote)
    except Exception:
        return None


def _job_label(h) -> str:
    """SPEC-0381: `GATEJOB-0001 (by alice, proj@branch)` for a standalone gate job's slot."""
    return (f"{h['job']} (by {h.get('by') or '?'}, {h.get('project') or '-'}"
            f"@{h.get('branch') or '-'})")


def attach(queue_group):
    """Register `gate-slots` on the `queue` group."""

    @queue_group.command("gate-slots")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.queue.gate_slots.v1 JSON")
    @click.pass_obj
    def queue_gate_slots(cfg, as_json):
        """Show the host-wide gate-slot pool: holders and waiters (SPEC-0309)."""
        occ = GS.occupancy(cfg)
        for h in occ["held"]:
            h["progress"] = _progress(cfg, h, False)
        for wv in (occ.get("workers") or {}).values():
            for h in wv["held"]:
                h["progress"] = _progress(cfg, h, True)
        if as_json:
            _dump_json({"schema": "nw.queue.gate_slots.v1", **occ})
            return
        jobs = sum(1 for wv in (occ.get("workers") or {}).values() for h in wv["held"]
                   if h.get("job"))
        console.print(f"gate slots: {len(occ['held'])}/{occ['slots']} held, "
                      f"{len(occ['waiting'])} waiting"
                      + (f", {jobs} standalone gate job(s) on workers" if jobs else ""),
                      highlight=False)
        if not occ["held"] and not occ["waiting"] and not jobs:
            console.print("[dim]no holders, no waiters[/]")
            return
        t = Table(title="Gate slots")
        for col in ("role", "slot", "campaign", "pid", "for", "progress"):
            t.add_column(col)
        for h in occ["held"]:
            t.add_row("holds", str(h["slot"]), str(h["campaign"]), str(h["pid"]),
                      _dur(_age(h["since"])), GPG.label(h.get("progress")))
        for name, wv in (occ.get("workers") or {}).items():
            for h in wv["held"]:                # SPEC-0381: standalone gate jobs on workers
                if h.get("job"):
                    t.add_row("holds", f"{name}:{h['slot']}", _job_label(h), str(h["pid"]),
                              _dur(_age(h["since"])), GPG.label(h.get("progress")))
        for w in occ["waiting"]:
            t.add_row("waits", "—", _job_label(w) if w.get("job") else str(w["campaign"]),
                      str(w["pid"]), _dur(_age(w["since"])), "")
        console.print(t)

    return queue_group
