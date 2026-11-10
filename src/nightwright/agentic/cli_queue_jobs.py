"""`nw queue jobs` (SPEC-0512): list and govern nw's Slurm jobs by nw id. The logic is in
`queue_jobs.py`; kept apart from `cli_queue.py` so the two can change independently."""
from __future__ import annotations

import click
from rich.table import Table

from ..console import console
from ..report import _dump_json
from . import queue_jobs as QJ

_JOB_HELP = "an nw job id, a campaign id, a GATEJOB-NNNN or a numeric Slurm id"


def _dur(s) -> str:
    if s is None:
        return "-"
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else \
        f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def _mem(mb) -> str:
    return "-" if mb is None else f"{mb / 1024:.1f}G" if mb >= 1024 else f"{int(mb)}M"


def _print(rec: dict) -> None:
    for w in rec["workers"]:
        if not w["ok"]:
            console.print(f"[red]{w['name']}[/]: {w['error']}", highlight=False)
    if not rec["workers"]:
        console.print("no Slurm worker: no SSH worker's cached info reports executor:slurm "
                      "(pass --worker NAME to ask one)", highlight=False)
        return
    if not rec["jobs"]:
        console.print("[dim]no nw Slurm jobs pending or running[/]")
        return
    t = Table(title="nw Slurm jobs")
    t.add_column("nw id", overflow="fold")         # the id is what the verbs take: never cut
    for col in ("class", "state (reason)", "cpus/mem", "node", "waited/ran", "slurm id"):
        t.add_column(col)
    for r in rec["jobs"]:
        state = r["state"] + (f" ({r['reason']})" if r.get("reason") else "")
        if r.get("nice"):
            state += f" nice {r['nice']}"
        t.add_row(r["job_id"], r.get("class") or "-", state,
                  f"{r.get('cpus') or '-'}/{_mem(r.get('mem_mb'))}", r.get("node") or "-",
                  f"{_dur(r.get('waited_s'))}/{_dur(r.get('ran_s'))}",
                  f"{r['slurm_job_id']}" + (f" @{r['worker']}" if len(rec["workers"]) > 1
                                            else ""))
    console.print(t)


def _act(cfg, verb: str, job: str, worker, as_json: bool, **kw) -> None:
    try:
        rec = QJ.govern(cfg, verb, job, worker=worker, **kw)
    except QJ.GovernError as e:
        if as_json:
            _dump_json({"ok": False, "verb": verb, "job": job, "error": str(e)})
            raise SystemExit(1)
        raise click.ClickException(str(e))
    if as_json:
        _dump_json(rec)
        return
    what = {"bump": f"nice {rec.get('nice')}", "class": f"class {rec.get('class')}",
            "cancel": f"cancel via {rec.get('via')}"}.get(verb, verb)
    console.print(f"{rec['job']} (slurm {rec['slurm_id']} on {rec['worker']}): {what}",
                  highlight=False)


def _job_command(group, verb: str, doc: str):
    @group.command(verb, help=doc)
    @click.argument("job", metavar="JOB")
    @click.option("--worker", default=None, metavar="NAME", help="the worker to ask")
    @click.option("--json", "as_json", is_flag=True, help="emit the result as JSON")
    @click.pass_obj
    def cmd(cfg, job, worker, as_json):
        _act(cfg, verb, job, worker, as_json)
    return cmd


def attach(queue_group):
    """Register `jobs` on the `queue` group."""

    @queue_group.group("jobs", invoke_without_command=True)
    @click.option("--json", "as_json", is_flag=True, help="emit nw.queue.jobs.v1 JSON")
    @click.option("--worker", default=None, metavar="NAME",
                  help="ask this worker (default: every worker whose info says executor:slurm)")
    @click.pass_context
    def queue_jobs(ctx, as_json, worker):
        """nw's Slurm jobs (SPEC-0512): list them and govern one by nw id.

        \b
        nw queue jobs [--json]                 pending reason, class, cpus/mem, node, age
        nw queue jobs hold|release JOB         keep a pending job from starting / let it go
        nw queue jobs top JOB                  first among nw's pending jobs of its partition
        nw queue jobs bump JOB [--down N]      lower its priority (Nice=N, default 1000)
        nw queue jobs class JOB CLASS          move a pending job to campaign|gatejob|persona|bulk
        nw queue jobs requeue JOB              put a running job back in the queue
        nw queue jobs cancel JOB               end it (the gate reports interrupted)

        JOB is an nw job id, a campaign id, a GATEJOB-NNNN or a Slurm id. `top` reorders only
        among the same user's jobs in the same partition (SchedulerParameters=enable_user_top);
        raising a priority needs a Slurm operator (slurmdbd, Phase 2)."""
        if ctx.invoked_subcommand is not None:
            return
        cfg = ctx.obj
        try:
            rec = QJ.listing(cfg, worker)
        except QJ.GovernError as e:
            raise click.ClickException(str(e))
        if as_json:
            _dump_json(rec)
            return
        _print(rec)

    for verb, doc in (("hold", f"Hold a pending job (JOB: {_JOB_HELP})."),
                      ("release", f"Release a held job (JOB: {_JOB_HELP})."),
                      ("top", f"Put a pending job first among nw's jobs of its partition "
                              f"(JOB: {_JOB_HELP})."),
                      ("requeue", f"Requeue a running job (JOB: {_JOB_HELP})."),
                      ("cancel", f"Cancel a job; its gate reports interrupted (JOB: {_JOB_HELP}).")):
        _job_command(queue_jobs, verb, doc)

    @queue_jobs.command("bump")
    @click.argument("job", metavar="JOB")
    @click.option("--down", type=int, default=None,
                  help=f"Nice value to set, 0..{QJ.BUMP_DOWN_MAX} (default {QJ.BUMP_DOWN_DEFAULT})")
    @click.option("--worker", default=None, metavar="NAME", help="the worker to ask")
    @click.option("--json", "as_json", is_flag=True, help="emit the result as JSON")
    @click.pass_obj
    def queue_jobs_bump(cfg, job, down, worker, as_json):
        """Lower a job's priority (Slurm Nice). Raising needs a Slurm operator (Phase 2)."""
        _act(cfg, "bump", job, worker, as_json, down=down)

    @queue_jobs.command("class")
    @click.argument("job", metavar="JOB")
    @click.argument("cls", metavar="CLASS", type=click.Choice(list(QJ.CLASSES)))
    @click.option("--worker", default=None, metavar="NAME", help="the worker to ask")
    @click.option("--json", "as_json", is_flag=True, help="emit the result as JSON")
    @click.pass_obj
    def queue_jobs_class(cfg, job, cls, worker, as_json):
        """Move a pending job to another class (its class's partition / QOS)."""
        _act(cfg, "class", job, worker, as_json, cls=cls)

    return queue_group
