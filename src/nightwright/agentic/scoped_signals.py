"""SPEC-0528: signal handlers scoped to one CLI command. A command that installs a handler
(SIGTERM -> clean shutdown) restores the previous one when it returns, so the same command run
in-process (a test's CliRunner in an xdist worker's main thread) never leaves its handler on
the process that called it."""
from __future__ import annotations

import signal
from contextlib import contextmanager


@contextmanager
def scoped(handlers: dict):
    """Install `{signum: handler}` for the block, then put back what was there. Off the main
    thread `signal.signal` refuses; that signal is then left alone (yields the installed set)."""
    prev = {}
    try:
        for sig, h in handlers.items():
            try:
                prev[sig] = signal.signal(sig, h)
            except ValueError:               # not the main thread (tests)
                pass
        yield set(prev)
    finally:
        for sig, h in prev.items():
            # None: the previous handler was not installed from Python; SIG_DFL is its meaning
            signal.signal(sig, signal.SIG_DFL if h is None else h)
