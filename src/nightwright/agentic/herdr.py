"""The one herdr client (SPEC-0361, epic SPEC-0360 invariant 2): best-effort calls to the
`herdr` CLI. argv only (no shell), a 2 s timeout, output discarded, any failure -> False, never
raised; a circuit breaker stops spawning after repeated failures.

Only `herdr_watch` (`nw herdr watch`) imports this module. The queue loop, the runner, `runproc`
and the event stream never do: that is the structural guarantee that herdr can neither fail nor
slow the queue (a test asserts it)."""
from __future__ import annotations

import json
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
        return self._run(args, capture=False) is not None

    def call_json(self, args) -> dict | None:
        """Run `herdr ARGS` and return the `result` object of its JSON stdout (SPEC-0364: the
        ids `tab create` / `pane split` hand back, `pane get`, `pane layout`, ...); None on any
        failure, including a reply that is not `{"result": {...}}`. Same timeout, breaker and
        gating as `call`. Never raises."""
        out = self._run(args, capture=True)
        if not out:
            return None
        try:
            res = json.loads(out).get("result")
        except (ValueError, AttributeError):
            self.last_error = f"not a JSON reply: {out[:200]!r}"
            return None
        return res if isinstance(res, dict) else None

    def _run(self, args, *, capture) -> str | None:
        """The one process spawn: stdout text when `capture` (else ""), None on failure."""
        try:
            if self.breaker_open():
                return None
            env = os.environ if self.env is None else self.env
            if unavailable(env) is not None:
                return None
            exe = shutil.which("herdr", path=env.get("PATH"))
            self.calls += 1
            out = ""
            try:
                proc = subprocess.run([exe, *[str(a) for a in args]], timeout=self.timeout_s,
                                      stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                                      stderr=subprocess.PIPE, env=dict(env))
                ok = proc.returncode == 0
                if ok and capture:
                    out = (proc.stdout or b"").decode(errors="replace")
                if not ok:
                    detail = (proc.stderr or b"").decode(errors="replace").strip()
                    self.last_error = (f"exit {proc.returncode}: {detail}"[:300]
                                       if detail else f"exit {proc.returncode}")
            except Exception as e:
                ok = False
                self.last_error = f"{type(e).__name__}: {e}"[:300]
            if ok:
                self.fails, self.open_until = 0, None
                return out
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
            return None
        except Exception:
            return None


_default: Client | None = None


def call(args) -> bool:
    """The module-level entry point (epic invariant 2): the default client's `call`."""
    global _default
    if _default is None:
        _default = Client()
    return _default.call(args)


# ---- argv builders (pure) -----------------------------------------------------------------

def metadata_args(kind, target, tokens, *, seq, ttl_ms, source=SOURCE,
                  display_agent=None) -> list[str]:
    """`<kind> report-metadata` for an explicit pane/workspace id; empty token values are
    cleared. `display_agent` (panes only, SPEC-0363) replaces the pane's agent label in herdr's
    default sidebar rows, with no config."""
    if kind not in ("pane", "workspace"):
        raise ValueError(kind)
    if display_agent is not None and kind != "pane":
        raise ValueError("display_agent is a pane option")
    # herdr's parser needs the positional id right after the subcommand: with options first
    # it rejects the call ("unknown option: nw", exit 2) although --help lists the id last.
    args = [kind, "report-metadata", str(target), "--source", source, "--seq", str(int(seq)),
            "--ttl-ms", str(int(ttl_ms))]
    if display_agent is not None:
        args += ["--display-agent", str(display_agent)]
    for name, value in tokens.items():
        if value:
            args += ["--token", f"{name}={value}"]
    for name, value in tokens.items():
        if not value:
            args += ["--clear-token", name]
    return args


def clear_args(kind, target, names, *, seq, source=SOURCE, clear_display_agent=False) -> list[str]:
    if kind not in ("pane", "workspace"):
        raise ValueError(kind)
    if clear_display_agent and kind != "pane":
        raise ValueError("clear_display_agent is a pane option")
    args = [kind, "report-metadata", str(target), "--source", source, "--seq", str(int(seq))]
    if clear_display_agent:
        args.append("--clear-display-agent")
    for name in names:
        args += ["--clear-token", name]
    return args


def notify_args(title, body, sound) -> list[str]:
    if sound not in SOUNDS:
        raise ValueError(sound)
    return ["notification", "show", str(title), "--body", str(body or ""), "--sound", sound]


# ---- run panes (SPEC-0364): every shape checked against herdr 0.9.5, the id right after the
# subcommand (its parser rejects options before the positional id)

RUN_SOURCE = "nw-run"
AGENT_STATES = ("idle", "working", "blocked", "unknown")


def tab_create_args(workspace, label, cwd) -> list[str]:
    """-> `.result.tab.tab_id`, `.result.root_pane.pane_id`; the tab is not focused."""
    return ["tab", "create", "--workspace", str(workspace), "--label", str(label),
            "--cwd", str(cwd), "--no-focus"]


def tab_rename_args(tab, label) -> list[str]:
    return ["tab", "rename", str(tab), str(label)]


def pane_split_args(pane, direction, cwd) -> list[str]:
    """-> `.result.pane.pane_id`; focus stays where it was."""
    if direction not in ("right", "down"):
        raise ValueError(direction)
    return ["pane", "split", str(pane), "--direction", direction, "--cwd", str(cwd), "--no-focus"]


def pane_layout_args(pane) -> list[str]:
    """-> `.result.layout.panes[] {pane_id, rect {x, y, width, height}}` of the pane's tab."""
    return ["pane", "layout", "--pane", str(pane)]


def pane_get_args(pane) -> list[str]:
    """-> `.result.pane {pane_id, tab_id, label?}`; exit 1 (`pane_not_found`) once closed."""
    return ["pane", "get", str(pane)]


def pane_process_info_args(pane) -> list[str]:
    """-> `.result.process_info {shell_pid, foreground_process_group_id, ...}`."""
    return ["pane", "process-info", "--pane", str(pane)]


def pane_rename_args(pane, label) -> list[str]:
    return ["pane", "rename", str(pane), str(label)]


def pane_run_args(pane, command) -> list[str]:
    """Types COMMAND + Enter into the pane's shell (the herdr server's shell and env)."""
    return ["pane", "run", str(pane), str(command)]


def pane_close_args(pane) -> list[str]:
    """Closing a tab's last pane closes the tab."""
    return ["pane", "close", str(pane)]


def report_agent_args(pane, state, *, seq, source=RUN_SOURCE, agent=RUN_SOURCE) -> list[str]:
    """herdr ignores (exit 0) a report whose --seq is not above the last one it took."""
    if state not in AGENT_STATES:
        raise ValueError(state)
    return ["pane", "report-agent", str(pane), "--source", source, "--agent", agent,
            "--state", state, "--seq", str(int(seq))]
