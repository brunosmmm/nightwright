"""`nw worker` (SPEC-0336): the commands a remote gate worker answers over SSH. The logic is in
`worker.py`; `nw worker ssh-command` is the forced command that reaches only these."""
from __future__ import annotations

import json
import sys

import click

from ..report import _dump_json
from . import worker as W
from . import worker_job as WJ


@click.group("worker")
def worker_group():
    """Remote gate worker (SPEC-0336): run gate jobs sent over SSH.

    Runs on the worker host. The coordinator's registry is `nw workers` (SPEC-0337).

    \b
    nw worker info --json              protocol, version, tools, free space
    nw worker have --repo KEY SHA...   which commits the object cache holds
    nw worker gate-job                 one job: header + bundle on stdin, frames on stdout
    nw worker submit                   the same job, detached: prints `accepted` and exits
    nw worker attach JOB --from N      follow a submitted job's frames after N
    nw worker poll JOB --from N        the frames after N once, plus a status line
    nw worker cancel JOB               stop a submitted job
    nw worker jobs --json              the jobs on this worker and their states
    nw worker gc --keep-days 2         age out job dirs and cached objects
    nw worker ssh-command              the forced command for the worker's SSH key
    """


@worker_group.command("info")
@click.option("--json", "as_json", is_flag=True, help="emit nw.worker.info.v1 JSON")
@click.pass_obj
def worker_info(cfg, as_json):
    """What this worker runs: protocol, nightwright version, git, uv, free space."""
    rec = W.info(cfg)
    if as_json:
        _dump_json(rec)
        return
    for k, v in rec.items():
        click.echo(f"{k}: {v}")


@worker_group.command("have")
@click.option("--repo", "repo", required=True, help="the repo_key of the object cache")
@click.argument("shas", nargs=-1, required=True)
@click.pass_obj
def worker_have(cfg, repo, shas):
    """Print the given shas whose commit is in the repo's object cache, one per line."""
    try:
        present = W.have(cfg, repo, list(shas))
    except ValueError as e:
        raise click.UsageError(str(e))
    for sha in present:
        click.echo(sha)


@worker_group.command("gate-job")
@click.pass_obj
def worker_gate_job(cfg):
    """Run one gate job: a nw.job.v1 header line and bundle on stdin, nw.job.frame.v1 frames
    on stdout. Exit 0 after `done`, 3 after `error`; EOF on stdin before `done` cancels."""
    raise SystemExit(W.gate_job(cfg, sys.stdin.buffer, sys.stdout.buffer))


def _stream(fn, *args) -> None:
    """Run a frame-printing command; a dropped connection (closed stdout) or ^C just exits."""
    try:
        code = fn(*args)
    except BrokenPipeError:
        code = 1
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)


@worker_group.command("submit")
@click.pass_obj
def worker_submit(cfg):
    """Submit one job (SPEC-0340): header + bundle on stdin as for gate-job. Starts a detached
    job process and prints `{type: status, state: accepted, n: 0}`. A resubmit with the same
    header prints the current state; a different header exits 3."""
    raise SystemExit(WJ.submit(cfg, sys.stdin.buffer, sys.stdout.buffer))


@worker_group.command("attach")
@click.argument("job_id")
@click.option("--from", "after", type=click.IntRange(min=0), default=0, show_default=True,
              help="print the frames whose n is above this")
@click.pass_obj
def worker_attach(cfg, job_id, after):
    """Print a job's frames after --from and follow them, renewing its lease. Exit 0 after
    `done`, 3 after `error`, 4 when the job is lost. Never cancels the job."""
    _stream(WJ.attach, cfg, job_id, after, sys.stdout.buffer)


@worker_group.command("poll")
@click.argument("job_id")
@click.option("--from", "after", type=click.IntRange(min=0), default=0, show_default=True,
              help="print the frames whose n is above this")
@click.option("--renew", is_flag=True, help="renew the job lease")
@click.pass_obj
def worker_poll(cfg, job_id, after, renew):
    """Print a job's frames after --from once, then a `status` line (state, heartbeat age)."""
    _stream(WJ.poll, cfg, job_id, after, renew, sys.stdout.buffer)


@worker_group.command("cancel")
@click.argument("job_id")
@click.pass_obj
def worker_cancel(cfg, job_id):
    """Cancel a submitted job; a finished one is left as it is, a lost one is killed."""
    _stream(WJ.cancel, cfg, job_id, sys.stdout.buffer)


@worker_group.command("jobs")
@click.option("--json", "as_json", is_flag=True, help="emit a JSON list")
@click.pass_obj
def worker_jobs(cfg, as_json):
    """The job dirs on this worker with their state."""
    rows = WJ.jobs(cfg)
    if as_json:
        _dump_json(rows)
        return
    for r in rows:
        click.echo(f"{r['job_id']}\t{r['state']}")


@worker_group.command("gc")
@click.option("--keep-days", type=click.IntRange(min=0), default=W.DEFAULT_KEEP_DAYS,
              show_default=True, help="keep job dirs and cached commits used this recently")
@click.pass_obj
def worker_gc(cfg, keep_days):
    """Delete old job dirs and `refs/nw/have/*` refs, then prune the caches' objects.
    Exits 1 when a repo's cache failed (listed in `errors`); the other repos still ran."""
    rec = W.gc(cfg, keep_days)
    click.echo(json.dumps(rec))
    if rec["errors"]:
        raise SystemExit(1)


@worker_group.command("ssh-command")
def worker_ssh_command():
    """The SSH forced command: exec `nw worker
    <info|have|gate-job|gc|submit|attach|poll|cancel|jobs>` from SSH_ORIGINAL_COMMAND, refuse
    anything else (exit 2)."""
    raise SystemExit(W.ssh_command())
