"""The one herdr client (SPEC-0361, epic SPEC-0360 invariant 2): best-effort calls to the
`herdr` CLI. argv only (no shell), a 2 s timeout, output discarded, any failure -> False, never
raised; a circuit breaker stops spawning after repeated failures.

Only `herdr_watch` (`nw herdr watch`) imports this module. The queue loop, the runner, `runproc`
and the event stream never do: that is the structural guarantee that herdr can neither fail nor
slow the queue (a test asserts it)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

TIMEOUT_S = 2.0
FAIL_MAX = 5
COOLDOWN_S = 60.0
SOURCE = "nw"
SOUNDS = ("none", "done", "request")


def unavailable(env=None) -> str | None:
    """Why herdr calls are off (one line), or None when they may be made: the process runs
    inside herdr (`HERDR_ENV=1`) and `herdr` is on PATH."""
    env = os.environ if env is None else env
    if env.get("HERDR_ENV") != "1":
        return "not inside a herdr pane (HERDR_ENV is not 1)"
    if shutil.which("herdr", path=env.get("PATH")) is None:
        return "`herdr` is not on PATH"
    return None


class Client:
    """Best-effort `herdr` caller with a circuit breaker and a monotonic `--seq` source."""

    def __init__(self, *, timeout_s=TIMEOUT_S, fail_max=FAIL_MAX, cooldown_s=COOLDOWN_S,
                 clock=time.monotonic, err=None, env=None):
        self.timeout_s = float(timeout_s)
        self.fail_max = int(fail_max)
        self.cooldown_s = float(cooldown_s)
        self.clock = clock
        self.err = err
        self.env = env
        self.fails = 0
        self.open_until = None
        self.last_error = None               # last failure's stderr / exception, for the breaker line
        self.calls = 0                       # processes spawned (for tests / diagnostics)
        self._seq = 0

    def next_seq(self) -> int:
        """Strictly increasing, seeded from epoch milliseconds so a restarted watcher never
        sends a lower seq than the one it replaces."""
        self._seq = max(self._seq + 1, time.time_ns() // 1_000_000)
        return self._seq

    def breaker_open(self) -> bool:
        return self.open_until is not None and self.clock() < self.open_until

    def call(self, args) -> bool:
        """Run `herdr ARGS`; True only on exit 0 within the timeout. Never raises."""
        try:
            if self.breaker_open():
                return False
            env = os.environ if self.env is None else self.env
            if unavailable(env) is not None:
                return False
            exe = shutil.which("herdr", path=env.get("PATH"))
            self.calls += 1
            try:
                proc = subprocess.run([exe, *[str(a) for a in args]], timeout=self.timeout_s,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE, env=dict(env))
                ok = proc.returncode == 0
                if not ok:
                    detail = (proc.stderr or b"").decode(errors="replace").strip()
                    self.last_error = (f"exit {proc.returncode}: {detail}"[:300]
                                       if detail else f"exit {proc.returncode}")
            except Exception as e:
                ok = False
                self.last_error = f"{type(e).__name__}: {e}"[:300]
            if ok:
                self.fails, self.open_until = 0, None
                return True
            self.fails += 1
            if self.fails >= self.fail_max:
                self.open_until = self.clock() + self.cooldown_s
                self.fails = self.fail_max - 1   # after the cool-down, one failure re-opens
                try:
                    (self.err or sys.stderr).write(
                        f"nw herdr: {self.fail_max} consecutive herdr failures; pausing herdr "
                        f"calls for {self.cooldown_s:g}s (last: {' '.join(map(str, args[:2]))}"
                        f" -> {self.last_error})\n")
                    (self.err or sys.stderr).flush()
                except Exception:
                    pass
            return False
        except Exception:
            return False


_default: Client | None = None


def call(args) -> bool:
    """The module-level entry point (epic invariant 2): the default client's `call`."""
    global _default
    if _default is None:
        _default = Client()
    return _default.call(args)


# ---- argv builders (pure) -----------------------------------------------------------------

def metadata_args(kind, target, tokens, *, seq, ttl_ms, source=SOURCE) -> list[str]:
    """`<kind> report-metadata` for an explicit pane/workspace id; empty values are cleared."""
    if kind not in ("pane", "workspace"):
        raise ValueError(kind)
    # herdr's parser needs the positional id right after the subcommand: with options first
    # it rejects the call ("unknown option: nw", exit 2) although --help lists the id last.
    args = [kind, "report-metadata", str(target), "--source", source, "--seq", str(int(seq)),
            "--ttl-ms", str(int(ttl_ms))]
    for name, value in tokens.items():
        if value:
            args += ["--token", f"{name}={value}"]
    for name, value in tokens.items():
        if not value:
            args += ["--clear-token", name]
    return args


def clear_args(kind, target, names, *, seq, source=SOURCE) -> list[str]:
    if kind not in ("pane", "workspace"):
        raise ValueError(kind)
    args = [kind, "report-metadata", str(target), "--source", source, "--seq", str(int(seq))]
    for name in names:
        args += ["--clear-token", name]
    return args


def notify_args(title, body, sound) -> list[str]:
    if sound not in SOUNDS:
        raise ValueError(sound)
    return ["notification", "show", str(title), "--body", str(body or ""), "--sound", sound]
