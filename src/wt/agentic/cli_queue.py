"""`wt queue gates` (SPEC-0173) and `wt queue add|list|watch|tick|run|park|resume|abandon`
(SPEC-0176). `attach` is called by `wt.agentic.cli.register` with the `queue` group.

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
from rich.table import Table

from .. import completion as C
from ..console import console
from ..report import _dump_json
from . import gates as G
from . import lease as L
from . import project as P
from . import queue as Q
from . import store

_NOT_WRITTEN = "[yellow]not written — re-run with --yes to commit[/]"


def _queue_table(payload: dict) -> Table:
    """The human listing: dense enough for an 80-column terminal, ids never cut (they are
    what a human copies into the next command); the park reason rides in the state cell."""
    lease = payload["lease"]
    slots = payload["slots"]
    if lease.get("held"):
        hb = lease.get("heartbeat_age_s")
        stale = " [red]STALE[/]" if lease.get("stale") else ""
        lease_txt = f"lease pid {lease.get('pid')} (heartbeat {hb}s ago){stale}"
    else:
        lease_txt = "no loop running"
    table = Table(title=f"Queue · {lease_txt} · slots {slots['busy']}/{slots['total']} · "
                        f"{len(payload['items'])} item(s)", box=box.SIMPLE_HEAD, pad_edge=False)
    table.add_column("campaign", min_width=24, no_wrap=True)
    table.add_column("spec", no_wrap=True)
    table.add_column("state", no_wrap=True)
    table.add_column("loops")
    table.add_column("cost")
    table.add_column("age")
    for it in payload["items"]:
        cost = f"${it['cost_usd']:.2f}" + (f"/{it['cap_usd']:.0f}" if it.get("cap_usd") else "")
        age = it.get("updated_age_s")
        age_txt = f"{age}s" if age is not None and age < 120 else (f"{age // 60}m" if age else "")
        park = it.get("park") or {}
        state = it["state"]
        if park.get("reason"):
            state = f"{state}:{park['reason']}"
        elif it.get("park_requested"):
            state = f"{state}:park?"
        color = {"PARKED": "yellow", "MERGED": "green", "HANDED_OFF": "green",
                 "ABANDONED": "dim"}.get(it["state"])
        table.add_row(it["campaign"], it["spec_id"], f"[{color}]{state}[/]" if color else state,
                      it["loops"], cost, age_txt)
    return table


def _print_plan(title: str, plan: dict) -> None:
    console.print(f"[bold]plan[/]  {title}")
    for k, v in plan.items():
        if v in (None, "", [], {}):
            continue
        console.print(f"  {k}: {v}", markup=False, highlight=False)


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

    # ---- SPEC-0176 -------------------------------------------------------------------------

    @queue_group.command("add")
    @click.argument("spec_id", shell_complete=C.complete_any_spec_id)
    @click.option("--project", shell_complete=C.complete_project,
                  help="project (default: Meta-Tools for SPEC-NNNN, the outbox project otherwise)")
    @click.option("--profile", default=None, help="workflow profile (default: agentic.default_profile)")
    @click.option("--base", default=None, help="base branch (default: the project's merge.base)")
    @click.option("--force", is_flag=True, help="also admit an in-progress spec")
    @click.option("--yes", "do_write", is_flag=True, help="create the campaign (default is dry-run)")
    @click.option("--json", "as_json", is_flag=True, help="emit wt.queue.add.v1 JSON")
    @click.pass_obj
    def queue_add(cfg, spec_id, project, profile, base, force, do_write, as_json):
        """Queue an accepted spec as a new campaign (dry-run unless --yes).

        \b
        wt queue add SPEC-0170            show the plan
        wt queue add SPEC-0170 --yes      create the campaign in QUEUED
        """
        try:
            plan = Q.plan_add(cfg, spec_id, project=project, profile=profile, base=base,
                              force=force)
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

    @queue_group.command("watch")
    @click.option("--interval", type=float, default=2.0, show_default=True,
                  help="refresh interval in seconds")
    @click.option("--iterations", type=int, default=0, hidden=True,
                  help="stop after N refreshes (0 = until Ctrl-C)")
    @click.pass_obj
    def queue_watch(cfg, interval, iterations):
        """Live view of `wt queue list` (Ctrl-C to stop)."""
        from rich.live import Live
        n = 0
        with Live(_queue_table(Q.queue_payload(cfg)), console=console,
                  refresh_per_second=4) as live:
            while True:
                n += 1
                if iterations and n >= iterations:
                    break
                time.sleep(interval)
                live.update(_queue_table(Q.queue_payload(cfg)))

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
            console.print(Q.tick_row_text(row), markup=False, highlight=False)
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
    @click.pass_obj
    def queue_run(cfg, once, max_ticks, poll_interval, until_idle, json_lines):
        """The loop: tick, heartbeat, sleep. One process at a time (lease-guarded).

        SIGINT/SIGTERM stop admissions, let in-flight steps record themselves, and release
        the lease; a run interrupted mid-persona is re-spawned fresh by the next loop.
        """
        def on_tick(report):
            if json_lines:
                sys.stdout.write(json.dumps(report.payload(), ensure_ascii=False) + "\n")
                sys.stdout.flush()
                return
            for row in report.steps:
                console.print(Q.tick_row_text(row), markup=False, highlight=False)

        try:
            ticks = Q.run(cfg, once=once, max_ticks=max_ticks, poll_interval_s=poll_interval,
                          until_idle=until_idle, on_tick=on_tick,
                          argv=["wt", "queue", "run"])
        except ValueError as e:
            raise click.ClickException(str(e))
        if not json_lines:
            console.print(f"[dim]{ticks} tick(s); lease released[/]")

    @queue_group.command("park")
    @click.argument("campaign", shell_complete=C.complete_campaign_id)
    @click.option("--reason", required=True, help="why (recorded on the campaign)")
    @click.option("--yes", "do_write", is_flag=True, help="apply (default is dry-run)")
    @click.option("--json", "as_json", is_flag=True, help="emit wt.queue.park.v1 JSON")
    @click.pass_obj
    def queue_park(cfg, campaign, reason, do_write, as_json):
        """Park a campaign. With a loop running, the current step finishes first."""
        try:
            plan = Q.plan_park(cfg, campaign, reason)
            if do_write:
                Q.apply_park(cfg, plan)
        except ValueError as e:
            raise click.ClickException(str(e))
        if as_json:
            _dump_json({"schema": "wt.queue.park.v1", **plan, "written": bool(do_write)})
            return
        _print_plan(f"park {campaign}", plan)
        console.print(f"[green]✓[/] {campaign} park {plan['mode']}" if do_write else _NOT_WRITTEN)

    @queue_group.command("resume")
    @click.argument("campaign", shell_complete=C.complete_campaign_id)
    @click.option("--to", type=click.Choice(Q.RESUMABLE_STATES), default=None,
                  help="state to resume to (default: where it parked)")
    @click.option("--note", default=None, help="a note for the next implementer run")
    @click.option("--raise-cap", type=float, default=None, help="new campaign cost cap in USD")
    @click.option("--reset-loops", is_flag=True, help="reset the loop counters")
    @click.option("--yes", "do_write", is_flag=True, help="apply (default is dry-run)")
    @click.option("--json", "as_json", is_flag=True, help="emit wt.queue.resume.v1 JSON")
    @click.pass_obj
    def queue_resume(cfg, campaign, to, note, raise_cap, reset_loops, do_write, as_json):
        """Resume a PARKED campaign (optionally with a note, a higher cap, fresh loops)."""
        try:
            plan = Q.plan_resume(cfg, campaign, to=to, note=note, raise_cap=raise_cap,
                                 reset_loops=reset_loops)
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
    @click.option("--yes", "do_write", is_flag=True, help="apply (default is dry-run)")
    @click.option("--json", "as_json", is_flag=True, help="emit wt.queue.abandon.v1 JSON")
    @click.pass_obj
    def queue_abandon(cfg, campaign, keep_worktree, do_write, as_json):
        """Abandon a PARKED or QUEUED campaign (removes its worktree; keeps the branch)."""
        try:
            plan = Q.plan_abandon(cfg, campaign, keep_worktree=keep_worktree)
            if do_write:
                Q.apply_abandon(cfg, plan)
        except ValueError as e:
            raise click.ClickException(str(e))
        if as_json:
            _dump_json({"schema": "wt.queue.abandon.v1", **plan, "written": bool(do_write)})
            return
        _print_plan(f"abandon {campaign}", plan)
        console.print(f"[green]✓[/] {campaign} ABANDONED" if do_write else _NOT_WRITTEN)

    return queue_group
