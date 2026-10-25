"""`wt queue gates` (SPEC-0173) and `wt queue add|list|watch|tick|run|park|resume|abandon`
(SPEC-0176). `attach` is called by `wt.agentic.cli.register` with the `queue` group."""
from __future__ import annotations

import os

import click
from rich.table import Table

from .. import completion as C
from ..console import console
from ..report import _dump_json
from . import gates as G
from . import project as P
from . import store


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
            console.print("\n".join(r.tail.splitlines()[-10:]), markup=False, highlight=False)

    return queue_group
