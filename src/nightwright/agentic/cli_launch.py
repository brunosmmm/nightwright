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
    if plan.get("host_backend"):
        console.print(f"host: {plan['host_backend']}", highlight=False, markup=False)
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


def _git_guard_gap(plan) -> list:
    """SPEC-0550: what is missing for the main-checkout git guard, for a conductor whose
    RESOLVED runner (`plan["runner"]`: --runner, else the stored runner on --resume, else the
    host default) is claude; None for any other plan (cursor has no hooks). Empty when the
    settings entry is present and the script resolves on the launch PATH."""
    from .. import agent_permissions as AP
    if plan["role"] != "conductor" or plan.get("runner") != "claude":
        return None
    gap = []
    target = AP.default_settings_path()
    try:
        if AP.git_guard_hook_missing(target):
            gap.append(f"the {AP.GIT_GUARD_EXECUTABLE} hook entry is missing from {target}")
    except ValueError as e:
        gap.append(str(e))
    if AP.git_guard_executable(L.exec_env(plan)) is None:
        gap.append(f"the {AP.GIT_GUARD_EXECUTABLE} executable is not on PATH")
    return gap


def _go(cfg, role, project, dry_run, resume, as_json, runner=None):
    from . import queue as Q
    if as_json and not dry_run:
        raise click.UsageError("--json is accepted only together with --dry-run")
    plan = L.build_plan(cfg, role, project, resume=resume, runner=runner)
    if role == "conductor" and runner in ("claude", "cursor"):
        plan["host_backend"] = runner
    gap = _git_guard_gap(plan)
    if role == "conductor":
        plan["git_guard_hook"] = None if gap is None else not gap
    if dry_run:
        if as_json:
            _dump_json(plan)
        else:
            _show(plan)
            if gap:
                console.print("warning: git guard not active (a launch would be refused): "
                              + "; ".join(gap), highlight=False, markup=False)
        L.check_price(plan)
        return
    if gap:
        raise click.ClickException(
            "refusing to launch a claude conductor without the git guard (SPEC-0550): "
            + "; ".join(gap) + ". Run `nw skills install` (or reinstall nightwright so the "
            "script is on PATH).")
    if role == "conductor" and runner in ("claude", "cursor"):
        Q.apply_host_backend(cfg, Q.plan_host_backend(cfg, runner))
    L.run(cfg, plan)


def attach(queue_group):
    @queue_group.command("conductor-launch")
    @click.option("--dry-run", is_flag=True, help="print the plan; start nothing")
    @click.option("--resume", is_flag=True, help="restart the newest launched conductor session")
    @click.option("--runner", "runner", default=None, type=click.Choice(["claude", "cursor"]),
                  help="backend for this launch (omit = host default / last --resume)")
    @click.option("--json", "as_json", is_flag=True, help="with --dry-run: emit the plan as JSON")
    @click.pass_obj
    def queue_conductor_launch(cfg, dry_run, resume, as_json, runner):
        """Start the conductor (claude or cursor-agent) with forced context limits, a pinned
        model and a pre-registered session id."""
        _go(cfg, "conductor", None, dry_run, resume, as_json, runner=runner)

    @queue_group.command("steward-launch")
    @click.argument("project")
    @click.option("--dry-run", is_flag=True, help="print the plan; start nothing")
    @click.option("--resume", is_flag=True, help="restart the newest launched steward session")
    @click.option("--runner", "runner", default=None, type=click.Choice(["claude", "cursor"]),
                  help="backend for this launch (omit = project/host default / last --resume)")
    @click.option("--json", "as_json", is_flag=True, help="with --dry-run: emit the plan as JSON")
    @click.pass_obj
    def queue_steward_launch(cfg, project, dry_run, resume, as_json, runner):
        """Start the steward of PROJECT (claude or cursor-agent)."""
        _go(cfg, "steward", project, dry_run, resume, as_json, runner=runner)
