"""`nw gate` (SPEC-0381): standalone remote gate jobs -- gate a worktree or a branch on a worker
box without a campaign. `run` creates (or reuses) a GATEJOB and either runs it in this process
(`--wait`) or in a detached child (`--detach`, the hidden `_exec`); `status`, `logs`, `cancel`
and `list` read and steer the records under `<agentic>/gatejobs/`."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading

import click

from ..console import console
from ..report import _dump_json
from . import gate_jobs as J


@click.group("gate")
def gate_group():
    """Standalone remote gate jobs (SPEC-0381): run a project's gates on a worker box.

    \b
    nw gate run                        gate this worktree's commit on a worker, wait
    nw gate run --branch B --detach    gate a branch tip in the background
    nw gate status GATEJOB-0001        a job's state (--json: nw.gatejob.v1)
    nw gate logs GATEJOB-0001 [GATE]   its gate logs
    nw gate cancel GATEJOB-0001        stop a waiting or running job
    """


def _stop_on_signals() -> threading.Event:
    stop = threading.Event()

    def _handler(_sig, _frm):
        stop.set()

    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(s, _handler)
        except ValueError:                      # not the main thread (tests)
            pass
    return stop


def _report(cfg, rec: dict, as_json: bool, reused: bool = False) -> int:
    out = J.payload(cfg, rec, reused=reused)
    if as_json:
        _dump_json(out)
        return out["exit"] if out["exit"] is not None else 0
    st = out["status"]
    where = f" on {out['worker']}" if out["worker"] else ""
    dur = f" in {out['duration']}s" if out["duration"] is not None else ""
    console.print(f"{out['id']}: {st}{where}{dur}"
                  + (" (reused a running job)" if reused else ""), highlight=False)
    for g in out["gates"]:
        mark = "ok" if g["ok"] else ("FAIL" if g["required"] else "fail (optional)")
        console.print(f"  {g['name']}: {mark}  {g.get('log') or ''}", highlight=False)
    if out["failing"]:
        console.print("failing: " + ", ".join(out["failing"][:20]), highlight=False)
    if out["changed"]:
        console.print("gates changed files on the worker: " + ", ".join(out["changed"][:10]),
                      highlight=False)
    if out["error"] and st not in ("passed", "failed"):
        console.print(out["error"], highlight=False)
    console.print(f"logs: {out['log_dir']}", highlight=False)
    return out["exit"] if out["exit"] is not None else 0


@gate_group.command("run")
@click.option("--project", default=None, help="registered project (default: the cwd's)")
@click.option("--worktree", "worktree", default=None, type=click.Path(file_okay=False),
              help="worktree to gate (default: cwd); HEAD, or a stash commit of a dirty tree")
@click.option("--branch", default=None, help="gate this branch's tip instead of a worktree")
@click.option("--base", default=None, help="base ref for NW_BASE (default: merge.base)")
@click.option("--tier", default="loop", type=click.Choice(J.TIERS), show_default=True,
              help="which project gates run")
@click.option("--cmd", "cmds", multiple=True,
              help="ad-hoc gate command (repeatable; replaces the project gates)")
@click.option("--by", default=None, help="requester (default: $NW_MAILBOX_ADDR, $USER)")
@click.option("--wait/--detach", default=True, help="block for the result (default) or not")
@click.option("--wait-s", type=float, default=J.DEFAULT_WAIT_S, show_default=True,
              help="seconds to wait for a worker slot before exit 3")
@click.option("--timeout-s", "gate_timeout_s", type=int, default=1200, show_default=True,
              help="per-gate timeout for ad-hoc --cmd gates (SPEC-0490)")
@click.option("--json", "as_json", is_flag=True, help="emit nw.gatejob.v1 JSON")
@click.pass_obj
def gate_run(cfg, project, worktree, branch, base, tier, cmds, by, wait, wait_s, gate_timeout_s,
             as_json):
    """Gate a worktree or a branch on a worker box, without a campaign (SPEC-0381).

    Exit 0 green, 1 red, 2 cannot start, 3 box unavailable (no placeable worker or no slot
    within --wait-s), 4 transport failure, 130 cancelled. Never runs gates locally.

    \b
    Example: nw gate run --project Meta-Tools --worktree . --tier close --json"""
    if worktree and branch:
        raise click.UsageError("--worktree and --branch are exclusive")
    if not J.settings(cfg)["enabled"]:
        console.print("standalone gate jobs are disabled (agentic.gate_jobs.enabled: false)")
        sys.exit(2)
    J.gc(cfg)
    try:
        target = J.resolve(cfg, project=project, worktree=worktree, branch=branch, base=base,
                           tier=tier, cmds=cmds, gate_timeout_s=gate_timeout_s)
    except J.JobError as e:
        console.print(f"nw gate run: {e.message}", highlight=False)
        sys.exit(e.code)
    if not J.worker_pools(cfg):
        console.print("box unavailable: no placeable gate worker (nw workers list); "
                      "standalone gates never run locally", highlight=False)
        sys.exit(3)
    rec, reused = J.create(cfg, target, J.requester(by))
    if reused:
        if not wait:
            sys.exit(_report(cfg, rec, as_json, reused=True))
        sys.exit(_report(cfg, J.follow(cfg, rec["id"], stop=_stop_on_signals()), as_json,
                         reused=True))
    if not wait:
        log = J.job_dir(cfg, rec["id"]) / "exec.log"
        with open(log, "ab") as fh:
            p = subprocess.Popen([sys.executable, "-m", "nightwright", "gate", "_exec", rec["id"],
                                  "--wait-s", str(wait_s)], stdin=subprocess.DEVNULL,
                                 stdout=fh, stderr=subprocess.STDOUT, start_new_session=True,
                                 env=os.environ.copy())
        rec = J.update(cfg, rec["id"], pid=p.pid)
        sys.exit(_report(cfg, rec, as_json))
    rec = J.execute(cfg, rec["id"], stop=_stop_on_signals(), wait_s=wait_s)
    sys.exit(_report(cfg, rec, as_json))


@gate_group.command("_exec", hidden=True)
@click.argument("job_id")
@click.option("--wait-s", type=float, default=J.DEFAULT_WAIT_S)
@click.pass_obj
def gate_exec(cfg, job_id, wait_s):
    """Run a recorded job (the `--detach` child)."""
    rec = J.execute(cfg, job_id, stop=_stop_on_signals(), wait_s=wait_s)
    sys.exit(J.EXIT.get(rec.get("status"), 2))


def _get(cfg, job_id) -> dict:
    try:
        J.job_dir(cfg, job_id)
    except ValueError as e:
        raise click.UsageError(str(e))
    rec = J.read(cfg, job_id)
    if rec is None:
        raise click.ClickException(f"no gate job {job_id}")
    return rec


@gate_group.command("status")
@click.argument("job_id")
@click.option("--json", "as_json", is_flag=True, help="emit nw.gatejob.v1 JSON")
@click.pass_obj
def gate_status(cfg, job_id, as_json):
    """A gate job's state; exits with its exit code once it has ended."""
    sys.exit(_report(cfg, _get(cfg, job_id), as_json))


@gate_group.command("logs")
@click.argument("job_id")
@click.argument("gate", required=False)
@click.option("--tail", "tail_n", type=int, default=0, help="only the last N lines of each log")
@click.pass_obj
def gate_logs(cfg, job_id, gate, tail_n):
    """Print a job's gate logs (one gate's with GATE)."""
    rec = _get(cfg, job_id)
    d = J.job_dir(cfg, rec["id"]) / "gates"
    logs = sorted(p for p in d.glob("*.log")) if d.is_dir() else []
    if gate:
        logs = [p for p in logs if p.stem.split("-", 1)[-1] == gate]
        if not logs:
            raise click.ClickException(f"no log for gate {gate} in {d}")
    if not logs:
        console.print(f"no gate logs yet in {d}", highlight=False)
        return
    for p in logs:
        text = p.read_text(encoding="utf-8", errors="replace")
        if tail_n > 0:
            text = "\n".join(text.splitlines()[-tail_n:])
        click.echo(f"==> {p} <==")
        click.echo(text)


@gate_group.command("cancel")
@click.argument("job_id")
@click.pass_obj
def gate_cancel(cfg, job_id):
    """Stop a waiting or running job (SIGTERM to its process)."""
    rec = _get(cfg, job_id)
    st = J.status_of(rec)
    if st in J.FINAL or st == "lost":
        console.print(f"{job_id} already ended: {st}", highlight=False)
        return
    try:
        os.kill(int(rec["pid"]), signal.SIGTERM)
    except (OSError, TypeError, ValueError) as e:
        raise click.ClickException(f"cannot signal {job_id}'s process {rec.get('pid')}: {e}")
    console.print(f"{job_id}: cancel sent to pid {rec['pid']}", highlight=False)


@gate_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="emit a list of nw.gatejob.v1 JSON")
@click.pass_obj
def gate_list(cfg, as_json):
    """Recent gate jobs, newest last."""
    rows = [J.payload(cfg, r) for r in J.list_jobs(cfg)]
    if as_json:
        _dump_json({"schema": "nw.gatejob.list.v1", "jobs": rows})
        return
    if not rows:
        console.print("[dim]no gate jobs[/]")
        return
    for r in rows:
        console.print(f"{r['id']}  {r['status']:<15} {r['project'] or '-'}@{r['branch'] or '-'}"
                      f"  by {r['requester']}  {r['worker'] or ''}", highlight=False)


@gate_group.group("cadence")
def cadence_group():
    """Cadence jobs (SPEC-0491): when a project's heavy tests are due."""


@cadence_group.command("status")
@click.option("--project", default=None, help="registered project (default: the cwd's)")
@click.option("--json", "as_json", is_flag=True, help="emit nw.cadence.status.v1 JSON")
@click.pass_obj
def cadence_status(cfg, project, as_json):
    """Per-job merge counters, last run and what is due (read-only; never seeds the state)."""
    from . import cadence as CAD
    from . import gitops as git
    from . import project as P
    name = project or P.find_project_for_cwd(cfg, os.getcwd())
    try:
        if not name:
            raise ValueError("no registered project here; pass --project")
        pcfg = P.project_config(cfg, name)
    except ValueError as e:
        console.print(f"nw gate cadence status: {e}", highlight=False)
        sys.exit(2)
    base = (pcfg.get("merge") or {}).get("base") or pcfg.get("base") or "main"
    try:
        base_sha = git.rev_parse(pcfg["_root"], base)
    except Exception:
        base_sha = None
    rows = CAD.status_rows(cfg, name, project_cfg=pcfg, base_sha=base_sha)
    if as_json:
        _dump_json({"schema": CAD.STATUS_SCHEMA, "project": name, "jobs": rows})
        return
    if not rows:
        console.print(f"{name}: no cadence jobs", highlight=False)
        return
    for r in rows:
        console.print("  ".join(f"{k}={json.dumps(r[k]) if k != 'name' else r[k]}"
                                for k in CAD.STATUS_KEYS), highlight=False, soft_wrap=True)
