"""`nw worker` (SPEC-0336): the commands a remote gate worker answers over SSH. The logic is in
`worker.py`; `nw worker ssh-command` is the forced command that reaches only these."""
from __future__ import annotations

import json
import sys

import click

from ..report import _dump_json
from . import worker as W
from . import worker_job as WJ
from . import worker_update as WU


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
    nw worker update [--force]         install the wheel on stdin (SPEC-0369)
    nw worker ssh-command              the forced command for the worker's SSH key
    nw worker serve --enrol BLOB       a pull worker: dial the coordinator (SPEC-0334)
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


@worker_group.command("claude-check")
@click.option("--auth", is_flag=True, help="also run a 1-token `claude -p` auth probe (costs money)")
@click.pass_obj
def worker_claude_check(cfg, auth):
    """SPEC-0344: whether this worker's `claude` works (and, with --auth, is logged in): one
    nw.worker.claude_check.v1 JSON line; exit 1 when it fails."""
    rec = W.claude_check(auth)
    click.echo(json.dumps(rec))
    if not rec["ok"]:
        raise SystemExit(1)


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


def _human_bytes(n) -> str:
    if not isinstance(n, int):
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


@worker_group.command("jobs")
@click.option("--json", "as_json", is_flag=True, help="emit a JSON list")
@click.pass_obj
def worker_jobs(cfg, as_json):
    """The job dirs on this worker with their state and on-disk size (SPEC-0388)."""
    rows = WJ.jobs(cfg)
    if as_json:
        _dump_json(rows)
        return
    for r in rows:
        click.echo(f"{r['job_id']}\t{r['state']}\t{_human_bytes(r.get('size_bytes'))}")


@worker_group.command("gc")
@click.option("--keep-days", type=click.IntRange(min=0), default=W.DEFAULT_KEEP_DAYS,
              show_default=True, help="keep job dirs and cached commits used this recently")
@click.pass_obj
def worker_gc(cfg, keep_days):
    """Delete old job dirs and `refs/nw/have/*` refs, then prune the caches' objects.
    Exits 1 when a repo's cache, or the reap of a lost job, failed (listed in `errors`);
    the rest still ran."""
    rec = W.gc(cfg, keep_days)
    click.echo(json.dumps(rec))
    if rec["errors"]:
        raise SystemExit(1)


@worker_group.command("update")
@click.option("--force", is_flag=True, help="install even while jobs are live (never kills one)")
@click.pass_obj
def worker_update(cfg, force):
    """SPEC-0369: install a wheel with `uv tool install --reinstall`, as this user, no sudo.
    stdin: one nw.worker.update.v1 JSON line, then `size` wheel bytes. stdout: one
    nw.worker.update.result.v1 JSON line. Exit 0 updated/unchanged, 3 failed, 5 busy (live
    jobs without --force, or another update running)."""
    raise SystemExit(WU.update(cfg, force, sys.stdin.buffer, sys.stdout.buffer))


@worker_group.command("ssh-command")
def worker_ssh_command():
    """The SSH forced command: exec `nw worker
    <info|have|gate-job|gc|submit|attach|poll|cancel|jobs|claude-check|update>` from SSH_ORIGINAL_COMMAND, refuse
    anything else (exit 2)."""
    raise SystemExit(W.ssh_command())


@worker_group.command("serve")
@click.option("--enrol", "blob", default=None, metavar="BLOB",
              help="the one-time blob `nw workers add --pull` printed; stored in "
                   "<config_dir>/coordinator.json (0600)")
@click.option("--config", "conf", default=None, type=click.Path(dir_okay=False),
              help="a coordinator.json to use (default <config_dir>/coordinator.json)")
@click.option("--kinds", default="gate,persona", show_default=True, metavar="KIND[,KIND]",
              help="job kinds this worker leases")
@click.option("--slots", type=click.IntRange(1, 64), default=1, show_default=True,
              help="jobs run at once")
@click.pass_obj
def worker_serve(cfg, blob, conf, kinds, slots):
    """Pull worker (SPEC-0334): lease jobs from `nw serve --jobs`, run them with the same job
    process as `submit`, and post their frames back. Runs until revoked or interrupted."""
    from . import pull_worker as PW
    try:
        doc = PW.load_conf(cfg, blob, conf)
    except ValueError as e:
        raise click.UsageError(str(e))
    ks = [k.strip() for k in kinds.split(",") if k.strip()]
    sys.exit(PW.Loop(cfg, doc, kinds=ks, slots=slots).run())


@click.command("serve")
@click.option("--jobs", "jobs", is_flag=True,
              help="serve the pull-worker job API (default bind 127.0.0.1:8790)")
@click.option("--dashboard", "dashboard", is_flag=True,
              help="serve the read-only dashboard (SPEC-0414; default bind 127.0.0.1:8791, "
                   "loopback only, bearer token in <data_dir>/agentic/dashboard/local.token)")
@click.option("--bind", default=None, metavar="ADDR:PORT",
              help="--jobs: loopback or a Tailscale address without TLS; --dashboard: loopback "
                   "only")
@click.option("--tls-cert", default=None, type=click.Path(exists=True, dir_okay=False))
@click.option("--tls-key", default=None, type=click.Path(exists=True, dir_okay=False))
@click.option("--client-ca", default=None, type=click.Path(exists=True, dir_okay=False),
              help="require client certificates signed by this CA, CN = the worker's name")
@click.pass_obj
def serve_cmd(cfg, jobs, dashboard, bind, tls_cert, tls_key, client_ca):
    """Serve jobs to pull workers (--jobs, SPEC-0334) or the read-only dashboard (--dashboard,
    SPEC-0414) over HTTP(S). Exactly one mode flag is required."""
    if jobs == dashboard:
        raise click.UsageError("give exactly one of --jobs and --dashboard")
    if dashboard:
        return _serve_dashboard(cfg, bind, tls_cert, tls_key, client_ca)
    from . import pull_jobs as PJ
    try:
        srv = PJ.make_server(cfg, bind or PJ.DEFAULT_BIND, tls_cert=tls_cert, tls_key=tls_key,
                             client_ca=client_ca)
    except (ValueError, OSError) as e:
        raise click.ClickException(str(e))
    host, port = srv.server_address[:2]
    click.echo(f"nw serve: job API on {'https' if tls_cert else 'http'}://{host}:{port}",
               err=True)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.stopping.set()
        srv.server_close()


class _TailnetMissingExit(click.ClickException):
    """No tailnet address on this host: exit 3 (SPEC-0416), distinct from a usage error (2)."""
    exit_code = 3


def _serve_dashboard(cfg, bind, tls_cert, tls_key, client_ca):
    """`nw serve --dashboard` (SPEC-0414, SPEC-0416). Exit 2: `BindRefused` or a TLS flag; exit 3:
    `TailnetMissing`; exit 1: an OSError from binding."""
    import signal
    import threading

    from .dashboard import auth as DA
    from .dashboard import server as DS
    try:
        host, port = DA.resolve_bind(bind or DS.DEFAULT_BIND)
    except DA.BindRefused as e:
        raise click.UsageError(str(e))
    except DA.TailnetMissing as e:
        raise _TailnetMissingExit(str(e))
    if tls_cert or tls_key or client_ca:
        raise click.UsageError("TLS is not supported with --dashboard")
    try:
        srv = DS.make_server(cfg, f"[{host}]:{port}" if ":" in host else f"{host}:{port}")
    except DA.BindRefused as e:
        raise click.UsageError(str(e))
    except DA.TailnetMissing as e:
        raise _TailnetMissingExit(str(e))
    except OSError as e:
        raise click.ClickException(str(e))
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):     # before server.json, so it is always cleaned up
        signal.signal(sig, lambda *_: stop.set())
    try:
        rec = DS.write_server_json(cfg, srv)
        click.echo(f"nw serve: dashboard on {rec['url']} (token auth, token in "
                   f"{DS.dashboard_dir(cfg) / 'local.token'})", err=True)
        t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2},
                             daemon=True)
        t.start()
        stop.wait()
        srv.shutdown()
        t.join()
    finally:
        DS.remove_server_json(cfg)
        srv.server_close()
