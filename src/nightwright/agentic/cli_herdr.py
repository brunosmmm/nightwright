"""`nw herdr watch` (SPEC-0361) and `nw herdr status` (SPEC-0365). The client and watcher modules
are imported inside the commands, so loading the CLI never imports `agentic/herdr.py`."""
from __future__ import annotations

import signal

import click


@click.group("herdr")
def herdr_group():
    """herdr integration (SPEC-0361/0363/0365): queue status in herdr's sidebar + notifications.

    \b
    The conductor's event follower starts the watcher by itself inside herdr (SPEC-0365;
    agentic.herdr.auto_watch: false turns that off). By hand:
    nw herdr watch                    push every 10 s until Ctrl-C (clears what it set)
    nw herdr watch --once             one push; values expire after 3 intervals
    nw herdr watch --run-panes        plus a pane per active campaign tailing its run
    nw herdr status                   is a watcher running? pid, mode, log path
    The conductor/steward panes need no config; see docs/herdr.md for the optional
    workspace-row snippet.
    """


@herdr_group.command("watch")
@click.option("--interval", "interval_s", type=click.FloatRange(min=1.0), default=10.0,
              show_default=True, help="seconds between pushes (TTL = 3x)")
@click.option("--conductor-pane", default=None, help="herdr pane id of the conductor "
              "(default: the pane its follower recorded)")
@click.option("--workspace", default=None, help="herdr workspace id for the queue totals "
              "(default: the conductor pane's workspace)")
@click.option("--once", is_flag=True, help="one event poll + one push, then exit")
@click.option("--run-panes/--no-run-panes", "run_panes", default=None,
              help="one herdr pane per active campaign running `nw campaign tail --follow`, in "
                   "an 'nw runs' tab (SPEC-0364; default: agentic.herdr.run_panes, off)")
@click.option("--run-pane-linger", type=click.FloatRange(min=0.0), default=300.0,
              show_default=True, help="seconds a run pane stays after its campaign stops")
@click.option("--parent", "parent", default=None, hidden=True,
              help="PID:TICKS of the supervising follower (SPEC-0365): exit once it is gone")
@click.pass_obj
def herdr_watch(cfg, interval_s, conductor_pane, workspace, once, run_panes, run_pane_linger,
                parent):
    """Push queue tokens and notifications to herdr. A no-op (exit 0, one line) outside
    herdr or without `herdr` on PATH; only this command ever calls herdr."""
    from . import herdr as H
    from . import herdr_autowatch as AW
    reason = H.unavailable()
    if reason:
        click.echo(f"nw herdr: {reason}; nothing to do")
        return
    from . import herdr_watch as HW
    par = None
    if parent is not None:
        par = AW.parse_parent(parent)
        if par is None:
            raise click.BadParameter("expected PID:TICKS", param_hint="--parent")
    if run_panes is None:
        run_panes = bool((((cfg or {}).get("agentic") or {}).get("herdr") or {}).get("run_panes"))
    w = HW.Watcher(cfg, H.Client(), interval_s=interval_s, conductor_pane=conductor_pane,
                   workspace=workspace, run_panes=run_panes, run_pane_linger=run_pane_linger)

    def _term(signum, frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _term)
    except ValueError:                       # not the main thread (tests)
        pass

    def record_state():
        AW.write_state(cfg, "auto" if par else "manual", par)

    def parent_gone():
        if par is not None and not AW.alive(*par):
            click.echo(f"nw herdr: supervising follower pid {par[0]} is gone; exiting")
            return True
        return False
    try:
        w.run(once=once, stop=parent_gone if par else None,
              on_locked=None if once else record_state)
    except KeyboardInterrupt:
        pass
    except ValueError as e:                  # reader lock held by another watcher
        raise click.ClickException(str(e))
    finally:
        if not once:
            AW.clear_state(cfg)


@herdr_group.command("status")
@click.option("--json", "as_json", is_flag=True, help="emit nw.herdr.watch.v1 JSON")
@click.pass_obj
def herdr_status(cfg, as_json):
    """Whether a `nw herdr watch` runs (pid, auto|manual, started), and its log path."""
    from . import herdr_autowatch as AW
    st = AW.status(cfg)
    if as_json:
        from ..report import _dump_json
        _dump_json(st)
        return
    from ..console import console
    console.print(AW.status_line(st), highlight=False)
