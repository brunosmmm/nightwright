"""`nw queue conductor-launch` and `steward-launch` (SPEC-0319); logic in `launch.py`."""
from __future__ import annotations

import shlex

import click

from ..console import console
from ..report import _dump_json
from . import launch as L


def _show(plan):
    if plan.get("runner"):
        console.print(f"runner: {plan['runner']}", highlight=False, markup=False)
    console.print(f"cwd: {plan['cwd']}", highlight=False, markup=False)
    console.print("env:", highlight=False, markup=False)
    for k in sorted(plan["env"]):
        console.print(f"  {k}={plan['env'][k]}", highlight=False, markup=False, soft_wrap=True)
    for k in plan["env_remove"]:
        console.print(f"unset: {k}", highlight=False, markup=False)
    for key in ("allowed_tools", "disallowed_tools"):
        console.print(f"{key}: {', '.join(plan[key]) or '(none)'}", highlight=False, markup=False)
    console.print(shlex.join(plan["argv"]), highlight=False, markup=False, soft_wrap=True)
    console.print(f"session: {plan['session_id']} ({'resumed' if plan['resumed'] else 'new'})",
                  highlight=False, markup=False)


def _go(cfg, role, project, dry_run, resume, as_json):
    if as_json and not dry_run:
        raise click.UsageError("--json is accepted only together with --dry-run")
    plan = L.build_plan(cfg, role, project, resume=resume)
    if dry_run:
        if as_json:
            _dump_json(plan)
        else:
            _show(plan)
        L.check_price(plan)
        return
    L.run(cfg, plan)


def attach(queue_group):
    @queue_group.command("conductor-launch")
    @click.option("--dry-run", is_flag=True, help="print the plan; start nothing")
    @click.option("--resume", is_flag=True, help="restart the newest launched conductor session")
    @click.option("--json", "as_json", is_flag=True, help="with --dry-run: emit the plan as JSON")
    @click.pass_obj
    def queue_conductor_launch(cfg, dry_run, resume, as_json):
        """Start the conductor (claude or cursor-agent via resolve_runner) with forced
        context limits, a pinned model and a pre-registered session id."""
        _go(cfg, "conductor", None, dry_run, resume, as_json)

    @queue_group.command("steward-launch")
    @click.argument("project")
    @click.option("--dry-run", is_flag=True, help="print the plan; start nothing")
    @click.option("--resume", is_flag=True, help="restart the newest launched steward session")
    @click.option("--json", "as_json", is_flag=True, help="with --dry-run: emit the plan as JSON")
    @click.pass_obj
    def queue_steward_launch(cfg, project, dry_run, resume, as_json):
        """Start the steward of PROJECT (claude or cursor-agent via resolve_runner)."""
        _go(cfg, "steward", project, dry_run, resume, as_json)
