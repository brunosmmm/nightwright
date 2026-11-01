"""SPEC-0340: a loopback "SSH" transport for worker tests. It runs `nw worker ssh-command` locally
with `SSH_ORIGINAL_COMMAND` set (the forced command's own path), in place of `ssh worker ...`,
and relays its stdout line by line. `kill_after=k` drops the "connection" after k lines: the
remote command gets SIGHUP and its stdout is closed, as when sshd loses the client.

Usable as a script too: `python remote_loopback.py [--kill-after K] -- nw worker attach J`."""
import os
import signal
import subprocess
import sys

TIMEOUT_S = float(os.environ.get("NW_TEST_WAIT_S", "60"))


class Result:
    def __init__(self, lines, returncode, stderr, dropped):
        self.lines = lines              # raw stdout lines (bytes, newline stripped)
        self.returncode = returncode
        self.stderr = stderr
        self.dropped = dropped          # True when kill_after cut the connection


def start(env, command, input=None):
    """The remote side of one connection, as a Popen (stdin a pipe, stdout a pipe)."""
    proc = subprocess.Popen([sys.executable, "-m", "nightwright", "worker", "ssh-command"],
                            env={**env, "SSH_ORIGINAL_COMMAND": command},
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    if input is not None:
        try:
            proc.stdin.write(input)
            proc.stdin.close()
        except BrokenPipeError:
            pass
    return proc


def run(env, command, input=None, kill_after=None, timeout=None):
    """Run `command` over the loopback; `kill_after` drops the connection after that many
    stdout lines. Returns a `Result`."""
    proc = start(env, command, b"" if input is None else input)
    lines, dropped = [], False
    for raw in proc.stdout:
        lines.append(raw.rstrip(b"\n"))
        if kill_after is not None and len(lines) >= kill_after:
            dropped = True
            try:
                proc.send_signal(signal.SIGHUP)
            except ProcessLookupError:
                pass
            break
    proc.stdout.close()
    try:
        rc = proc.wait(timeout=TIMEOUT_S if timeout is None else timeout)
    finally:
        if proc.poll() is None:
            proc.kill()
    err = proc.stderr.read().decode(errors="replace")
    proc.stderr.close()
    return Result(lines, rc, err, dropped)


if __name__ == "__main__":
    args = sys.argv[1:]
    k = None
    if args[:1] == ["--kill-after"]:
        k, args = int(args[1]), args[2:]
    if args[:1] == ["--"]:
        args = args[1:]
    r = run(dict(os.environ), " ".join(args), sys.stdin.buffer.read(), kill_after=k)
    for ln in r.lines:
        sys.stdout.buffer.write(ln + b"\n")
    sys.stderr.write(r.stderr)
    sys.exit(255 if r.dropped else r.returncode)
