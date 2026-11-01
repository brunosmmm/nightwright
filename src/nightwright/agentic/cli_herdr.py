"""`nw herdr watch` (SPEC-0361): the opt-in herdr reporter. The client and watcher modules are
imported inside the command, so loading the CLI never imports `agentic/herdr.py`."""
from __future__ import annotations

import signal

import click


@click.group("herdr")
def herdr_group():
    """herdr integration (SPEC-0361): queue status in herdr's sidebar + notifications.

    \b
    nw herdr watch                    push tokens every 10 s until Ctrl-C (clears them)
    nw herdr watch --once             one push; values expire after 3 intervals
    See docs/herdr.md for the suggested ~/.config/herdr/config.toml snippet.
    """


@herdr_group.command("watch")
@click.option("--interval", "interval_s", type=click.FloatRange(min=1.0), default=10.0,
              show_default=True, help="seconds between token pushes (TTL = 3x)")
@click.option("--conductor-pane", default=None, help="herdr pane id of the conductor "
              "(default: the pane its follower recorded)")
@click.option("--workspace", default=None, help="herdr workspace id for the queue tokens "
              "(default: this pane's workspace + the conductor/steward panes' workspaces)")
@click.option("--once", is_flag=True, help="one event poll + one push, then exit")
@click.pass_obj
def herdr_watch(cfg, interval_s, conductor_pane, workspace, once):
    """Push queue tokens and notifications to herdr. A no-op (exit 0, one line) outside
    herdr or without `herdr` on PATH; only this command ever calls herdr."""
    from . import herdr as H
    reason = H.unavailable()
    if reason:
        click.echo(f"nw herdr: {reason}; nothing to do")
        return
    from . import herdr_watch as HW
    w = HW.Watcher(cfg, H.Client(), interval_s=interval_s, conductor_pane=conductor_pane,
                   workspace=workspace)

    def _term(signum, frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _term)
    except ValueError:                       # not the main thread (tests)
        pass
    try:
        w.run(once=once)
    except KeyboardInterrupt:
        pass
    except ValueError as e:                  # reader lock held by another watcher
        raise click.ClickException(str(e))
