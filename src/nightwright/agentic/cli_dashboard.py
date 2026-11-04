"""`nw dashboard snapshot [--json]` (SPEC-0401): the remote dashboard's `nw.dashboard.v1`
document, built in-process. No server, no tailscale, no token; writes nothing.

`nw dashboard install | uninstall | status` (SPEC-0427): the `nw-dashboard.service` systemd user
unit that keeps `nw serve --dashboard` running, and its status report."""
from __future__ import annotations

import json
import sys

import click


@click.group("dashboard")
def dashboard_group():
    """Remote web dashboard (SPEC-0400): the live queue view over Tailscale.

    \b
    nw dashboard snapshot              health, counts, spend and what needs you (text)
    nw dashboard snapshot --json       the full nw.dashboard.v1 document
    nw dashboard install               run the server as a systemd user service (loopback)
    nw dashboard install --dry-run     print the unit and the systemctl commands only
    nw dashboard status                unit, URL, code freshness, linger, log command
    nw dashboard uninstall             stop, disable and remove the unit
    """


def _err(v) -> str | None:
    return v["error"] if isinstance(v, dict) and set(v) == {"error"} else None


def _num(v) -> str:
    return "?" if v is None else f"{v:g}" if isinstance(v, float) else str(v)


def text_lines(doc: dict) -> list[str]:
    """The short text summary: overall health, counts, spend, then what needs the human."""
    out = []
    h = doc.get("health") or {}
    runner, cond = h.get("runner"), h.get("conductor")
    if _err(runner):
        parts = [f"runner: error: {_err(runner)}"]
    else:
        lease = runner.get("lease") or {}
        if lease.get("held"):
            who = f"{lease.get('pid')}@{lease.get('host')}" if lease.get("host") \
                else str(lease.get("pid"))
            parts = [f"runner {who} heartbeat {_num(lease.get('heartbeat_age_s'))}s"]
            if lease.get("stale"):
                parts.append("stale lease")
        else:
            parts = ["runner down"]
        if runner.get("draining"):
            parts.append("draining")
        if (runner.get("runner_code") or {}).get("stale"):
            parts.append("stale code")
    if _err(cond):
        parts.append(f"conductor: error: {_err(cond)}")
    else:
        parts.append(f"conductor {cond.get('pid') or 'down'}")
    out.append(" · ".join(parts))
    totals = doc.get("totals")
    if _err(totals):
        out.append(f"totals: error: {_err(totals)}")
    else:
        out.append(f"active {totals.get('active', 0)} · queued {totals.get('ready', 0)} · "
                   f"parked {totals.get('attention', 0)} · waiting {totals.get('waiting', 0)} · "
                   f"backlog {totals.get('backlog', 0)} · "
                   f"conductor-handling {totals.get('conductor_handling', 0)}")
        out.append(f"spend today ${float(totals.get('spend_today_usd') or 0):.2f}")
    for name in ("stewards", "herdr_watch"):
        if _err(h.get(name)):
            out.append(f"health.{name}: error: {_err(h[name])}")
    for name in ("slots", "gate_pools", "active", "needs_you", "conductor_handling", "waiting",
                 "backlog", "delivered", "recent_events"):
        if _err(doc.get(name)):
            out.append(f"{name}: error: {_err(doc[name])}")
    inbox = doc.get("inbox") or {}
    esc, parks = inbox.get("escalations"), inbox.get("parks")
    needs = 0
    if _err(esc):
        out.append(f"inbox.escalations: error: {_err(esc)}")
    else:
        for e in esc:
            if e.get("error"):
                tail = f"(error: {e['error']})"
            elif not e.get("answerable"):
                tail = f"({e.get('why_not')})"
            else:
                tail = f"[{'/'.join(e.get('forms') or [])}]"
            out.append(f"{e.get('id')} {e.get('campaign') or '-'} {e.get('question')} {tail}")
            needs += 1
    if _err(parks):
        out.append(f"inbox.parks: error: {_err(parks)}")
    else:
        for p in parks:
            park = p.get("park") or {}
            out.append(f"{p.get('campaign')} {park.get('reason')} {p.get('inbox_state')}: "
                       f"{p.get('fix')}")
            needs += 1
    if not needs and not _err(esc) and not _err(parks):
        out.append("nothing needs you")
    return out


@dashboard_group.command("snapshot")
@click.option("--json", "as_json", is_flag=True, help="emit the nw.dashboard.v1 document")
@click.pass_obj
def snapshot_cmd(cfg, as_json):
    """Build the dashboard snapshot in-process and print it (read-only; no server needed).

    Prints the overall health line (runner, conductor), the counts, today's spend and every
    open escalation and human-owned park. A section whose builder failed prints as
    `<section>: error: <message>`; the command still exits 0.
    """
    from .dashboard import snapshot as SN
    try:
        doc = SN.build(cfg)
    except Exception as e:                         # noqa: BLE001 - no config, no data dir
        click.echo(f"nw dashboard snapshot: {type(e).__name__}: {e}", err=True)
        sys.exit(1)
    if as_json:
        click.echo(json.dumps(doc, indent=2, ensure_ascii=False, default=str))
        return
    for line in text_lines(doc):
        click.echo(line)


# ---- the systemd user unit (SPEC-0427) ------------------------------------------------------

_unit_opt = click.option("--unit-name", "unit", default=None, hidden=True, metavar="NAME",
                         help="unit name instead of nw-dashboard (tests)")


def _sh(cmd):
    """Run one systemctl / rm step, echoing it; returns the exit code."""
    import subprocess
    from .dashboard import lifecycle as LC
    click.echo(f"$ {LC.shell_line(cmd)}", err=True)
    if cmd[0] == "rm":
        from pathlib import Path
        Path(cmd[-1]).unlink(missing_ok=True)
        return 0
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        click.echo(f"  {e}", err=True)
        return 1
    for line in (p.stdout + p.stderr).splitlines():
        click.echo(f"  {line}", err=True)
    return p.returncode


@dashboard_group.command("install")
@click.option("--bind", "bind", type=click.Choice(["loopback", "tailscale"]), default="loopback",
              show_default=True, help="loopback: 127.0.0.1:PORT; tailscale: this host's own "
                                      "tailnet address (needs `tailscale up`)")
@click.option("--port", type=click.IntRange(1, 65535), default=None,
              help="port (default 8791)")
@click.option("--dry-run", is_flag=True, help="print the unit and the commands; change nothing")
@click.option("--nw-bin", default=None, metavar="PATH",
              help="the nw executable the unit runs (default: the uv tool install)")
@click.option("--code-root", default=None, hidden=True,
              type=click.Path(exists=True, file_okay=False),
              help="pass --code-root to the server (tests)")
@_unit_opt
@click.pass_obj
def install_cmd(cfg, bind, port, dry_run, nw_bin, code_root, unit):
    """Write ~/.config/systemd/user/nw-dashboard.service, then `daemon-reload` and
    `enable --now` it (a running unit is restarted onto the new unit text).

    The unit runs `nw serve --dashboard` with `Restart=always`: the server's exit 0 on a
    nightwright code change becomes a restart onto the new code. Exit 2 (a refused bind) stops
    it. With `--bind tailscale` the server waits up to 60 s for a tailnet address before exiting
    3, so a Tailscale outage is one retry a minute and the dashboard is back within seconds of
    Tailscale coming up. Logs go to the journal: `journalctl --user -u nw-dashboard -f`.
    """
    import os
    import shutil
    from pathlib import Path
    from .dashboard import lifecycle as LC
    try:
        name = LC.unit_name(unit)
        nw = LC.resolve_nw_bin(nw_bin)
        b = LC.unit_bind(bind, port)
    except ValueError as e:
        raise click.UsageError(str(e))
    after = bind == "tailscale" and LC.tailscaled_unit_exists()
    text = LC.render_unit(nw_bin=nw, bind=b, env=LC.env_overrides(), after_tailscaled=after,
                          code_root=os.path.abspath(code_root) if code_root else None,
                          name=name)
    path = LC.unit_path(name)
    show = LC.systemctl_show(name) if not dry_run else None
    cmds = LC.install_commands(name, restart=bool(show and show.get("ActiveState") == "active"))
    if dry_run:
        click.echo(f"# {path}")
        click.echo(text, nl=False)
        click.echo("# then:")
        for c in cmds:
            click.echo(LC.shell_line(c))
        click.echo(f"# (a unit that is already active is also restarted: "
                   f"systemctl --user restart {name}.service)")
        return
    if not shutil.which("systemctl"):
        raise click.ClickException("systemctl not found: a systemd user session is required")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(f"{path}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    click.echo(f"wrote {path}", err=True)
    for c in cmds:
        if _sh(c) != 0:
            raise click.ClickException(f"`{LC.shell_line(c)}` failed; see "
                                       f"`{LC.logs_command(name)}`")
    _print_status(_settled_status(cfg, name))


def _settled_status(cfg, name, wait_s=15.0):
    """The status once the server has written server.json, or the unit stopped being active
    (failed, auto-restart), or `wait_s` passed: right after `enable --now` the server is still
    starting."""
    import time
    from .dashboard import lifecycle as LC
    deadline = time.monotonic() + wait_s
    while True:
        st = LC.status(cfg, name=name, query_unit=True)
        if st["running"] or not st["unit"]["active"] or time.monotonic() >= deadline:
            return st
        time.sleep(0.5)


@dashboard_group.command("uninstall")
@click.option("--dry-run", is_flag=True, help="print the commands; change nothing")
@_unit_opt
def uninstall_cmd(dry_run, unit):
    """Stop, disable and remove the nw-dashboard user unit, then `daemon-reload`."""
    from .dashboard import lifecycle as LC
    try:
        name = LC.unit_name(unit)
    except ValueError as e:
        raise click.UsageError(str(e))
    cmds = LC.uninstall_commands(name)
    if dry_run:
        for c in cmds:
            click.echo(LC.shell_line(c))
        return
    path = LC.unit_path(name)
    if not path.is_file():
        click.echo(f"{name}.service is not installed ({path} does not exist)")
        return
    rc = 0
    for c in cmds:
        if c[2] == "reset-failed":                 # nothing to reset is not an error: quiet
            import subprocess
            try:
                subprocess.run(c, capture_output=True, timeout=60, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
            continue
        rc = rc or _sh(c)
    if path.exists():
        raise click.ClickException(f"{path} is still there")
    click.echo(f"removed {name}.service")
    if rc:
        sys.exit(rc)


def _yn(v) -> str:
    return "?" if v is None else "yes" if v else "no"


def _print_status(st):
    u, srv, code = st["unit"], st["server"], st["code"]
    if u["installed"]:
        bits = [f"installed ({u['path']})", f"enabled {_yn(u['enabled'])}",
                f"{u['state'] or '?'}" + (f" ({u['sub_state']})" if u["sub_state"] else "")]
        if u["main_pid"]:
            bits.append(f"pid {u['main_pid']}")
        if u["n_restarts"] is not None:
            bits.append(f"restarts {u['n_restarts']}")
        if u["exit_status"]:
            bits.append(f"last exit {u['exit_status']}")
        click.echo(f"unit     {u['name']}: " + " · ".join(bits))
    else:
        click.echo(f"unit     {u['name']}: not installed (nw dashboard install)")
    if srv:
        click.echo(f"server   {srv['url']} · pid {srv['pid']} · bind {srv['bind']} · since "
                   f"{srv['started_at']} · clients {srv['clients']}")
    else:
        click.echo("server   not running (no live server.json)")
    if srv and not code["watched"]:
        click.echo("code     not watched (will not restart itself on a code change)")
    elif code["current"] is True:
        click.echo(f"code     current ({str(code['running_sha'])[:7]} = checkout HEAD)")
    elif code["current"] is False:
        click.echo(f"code     STALE: running {str(code['running_sha'])[:7]}, checkout HEAD "
                   f"{str(code['head_sha'])[:7]}")
    elif srv:
        click.echo("code     unknown (checkout HEAD unreadable)")
    if st["linger"] is not None:
        click.echo(f"linger   {_yn(st['linger'])}" + (
            f" (the unit stops when you log out; run: {st['linger_fix']})"
            if st["linger"] is False else ""))
    click.echo(f"logs     {st['logs']}")
    for p in st["problems"]:
        click.echo(f"problem: {p}")
    for w in st["warnings"]:
        click.echo(f"note: {w}")


@dashboard_group.command("status")
@click.option("--json", "as_json", is_flag=True, help="emit nw.dashboard.status.v1")
@_unit_opt
@click.pass_obj
def status_cmd(cfg, as_json, unit):
    """Is the dashboard service installed, enabled and active; its pid and URL (server.json);
    does it run the checkout's current code; is linger on; where are the logs.

    Read-only: it never runs `loginctl enable-linger` (that changes your system config) but
    prints the exact command when linger is off. Exit 1 when there is a problem (installed
    but not active, active without a live server, stale code), else 0.
    """
    from .dashboard import lifecycle as LC
    try:
        st = LC.status(cfg, name=unit, query_unit=True)
    except ValueError as e:
        raise click.UsageError(str(e))
    if as_json:
        click.echo(json.dumps(st, indent=2, ensure_ascii=False, default=str))
    else:
        _print_status(st)
    if st["problems"]:
        sys.exit(1)
