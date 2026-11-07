"""Detached persona-run wrapper and liveness helpers (SPEC-0225).

`python -m nightwright.agentic.runproc <run_dir>` is started by `ClaudeRunner` in its own session. It
claims the run dir (`proc.json`, created `O_EXCL`), starts `claude -p` in a process group of
its own, streams stdout / stderr into the run dir (stdout to `stream.jsonl` for a stream-json
launch, SPEC-0362, whose last `result` event is then copied to `result.json`), enforces the timeout and writes `exit.json`
atomically. Everything it does is recorded in files, so a runner that restarts can adopt the
run. The helpers below read those files to decide whether a run is alive."""
from __future__ import annotations

import datetime as dt
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

GRACE_S = float(os.environ.get("NW_RUNPROC_GRACE_S") or 10.0)   # TERM -> KILL grace (tests shorten it)


def child_env(extra=None, environ=None) -> dict:
    """The environment of a persona child: `environ` (default `os.environ`) plus `extra`.
    SPEC-0319: inside a launched conductor/steward session the launcher's keys
    (`NW_LAUNCH_KEYS`) and any ambient effort pin are dropped, so a campaign runner gets Claude
    Code's defaults and its own `--effort`. SPEC-0361: every `HERDR_*` key is dropped, so a
    herdr hook inside a headless run cannot report against the pane that started the queue.
    Keys in `extra` always win."""
    env = dict(os.environ if environ is None else environ)
    launch = env.pop("NW_LAUNCH_KEYS", None)
    if launch is not None:
        for key in [k.strip() for k in launch.split(",") if k.strip()] + ["CLAUDE_CODE_EFFORT_LEVEL"]:
            env.pop(key, None)
    for key in [k for k in env if k.startswith("HERDR_")]:
        del env[key]
    return {**env, **(extra or {})}


_CLAUDE_HOMES = (".local/bin", ".claude/local", ".npm-global/bin", ".bun/bin", ".volta/bin")
_CLAUDE_SYSTEM = ("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin")
_AGENT_HOMES = _CLAUDE_HOMES
_AGENT_SYSTEM = _CLAUDE_SYSTEM


def _resolve_binary(name: str, environ=None) -> str | None:
    """`PATH` first, then the standard install dirs a non-login ssh `PATH` lacks."""
    import shutil
    env = os.environ if environ is None else environ
    found = shutil.which(name, path=env.get("PATH"))
    if found:
        return found
    home = Path(env.get("HOME") or os.path.expanduser("~"))
    for d in [home / h for h in _AGENT_HOMES] + [Path(x) for x in _AGENT_SYSTEM]:
        c = d / name
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def resolve_claude(environ=None) -> str | None:
    """SPEC-0389: the one `claude` lookup of a worker (`claude-check` and the job's exec):
    `PATH` first, then the standard install dirs a non-login ssh `PATH` lacks
    (`~/.local/bin`, ...). The absolute path, or None."""
    return _resolve_binary("claude", environ)


def resolve_cursor_agent(environ=None) -> str | None:
    """SPEC-0436: the same lookup as `resolve_claude`, for `cursor-agent`."""
    return _resolve_binary("cursor-agent", environ)


def _with_binary(argv, env, name: str, resolve) -> tuple[list, dict]:
    if not argv or argv[0] != name:
        return list(argv), env
    exe = resolve(env)
    if exe is None:
        raise FileNotFoundError(2, f"{name} not found on PATH or in the standard install dirs",
                                name)
    d = str(Path(exe).parent)
    parts = (env.get("PATH") or "").split(os.pathsep)
    if d not in parts:
        env = {**env, "PATH": os.pathsep.join([*parts, d]) if env.get("PATH") else d}
    return [exe, *argv[1:]], env


def with_claude(argv, env) -> tuple[list, dict]:
    """SPEC-0389: `argv` with a leading `claude` made absolute and its dir put on `env`'s
    `PATH` (claude re-executes itself and its helpers). `FileNotFoundError` when none."""
    return _with_binary(argv, env, "claude", resolve_claude)


def with_cursor_agent(argv, env) -> tuple[list, dict]:
    """SPEC-0436: `argv` with a leading `cursor-agent` made absolute, dir on `PATH`."""
    return _with_binary(argv, env, "cursor-agent", resolve_cursor_agent)


def with_agent(argv, env) -> tuple[list, dict]:
    """Resolve a leading `claude` or `cursor-agent` the way the job's exec does.
    A leading `node` (Pi child) is left unchanged (SPEC-0451)."""
    if argv and argv[0] == "node":
        return list(argv), env
    if argv and argv[0] == "cursor-agent":
        return with_cursor_agent(argv, env)
    return with_claude(argv, env)


def _pi_exec_script() -> Path:
    return Path(__file__).resolve().parent / "pi_exec.mjs"


def _pi_probe_secret_values(environ) -> list[str]:
    env = os.environ if environ is None else environ
    return [v for v in env.values() if isinstance(v, str) and v]


def _redact_secrets(text: str, secrets: list[str]) -> str:
    out = text or ""
    for s in secrets:
        out = out.replace(s, "[redacted]")
    return out


def probe_pi(providers, environ=None, *, run=None) -> dict:
    """SPEC-0451: `{ok, error, missing}` from `pi_exec.mjs --probe` (no key text)."""
    run = subprocess.run if run is None else run
    env = os.environ if environ is None else environ
    providers = [str(p).strip() for p in (providers or []) if str(p).strip()]
    out = {"ok": False, "error": None, "missing": list(providers)}
    node = _resolve_binary("node", env)
    if not node:
        out["error"] = "node not found on PATH"
        return out
    script = _pi_exec_script()
    if not script.is_file():
        out["error"] = f"pi_exec.mjs missing at {script}"
        return out
    try:
        rec = run([node, str(script), "--probe", *providers],
                  capture_output=True, text=True, timeout=30.0, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        out["error"] = f"pi probe failed: {e}"
        return out
    secrets = _pi_probe_secret_values(env)
    text = (rec.stdout or "").strip().splitlines()
    line = _redact_secrets(text[-1] if text else "", secrets)
    try:
        payload = json.loads(line) if line else {}
    except json.JSONDecodeError:
        out["error"] = (rec.stderr or rec.stdout or "").strip() or f"probe exit {rec.returncode}"
        return out
    if isinstance(payload, dict):
        out.update({k: payload.get(k) for k in ("ok", "error", "missing") if k in payload})
        out["ok"] = bool(payload.get("ok"))
        out["missing"] = list(payload.get("missing") or [])
        err = payload.get("error")
        out["error"] = _redact_secrets(err, secrets) if isinstance(err, str) else err
    if out["ok"]:
        out["missing"] = []
    elif not out["error"]:
        out["error"] = "one or more providers lack a credential"
    return out


def ensure_pi_persona(pi_provider, environ=None) -> dict:
    """Raise `FileNotFoundError` when the request's Pi provider has no credential."""
    prov = str(pi_provider or "").strip()
    if not prov:
        raise FileNotFoundError(2, "pi_provider is missing", "pi")
    rec = probe_pi([prov], environ)
    if not rec.get("ok"):
        detail = rec.get("error") or f"no credential for provider {prov!r}"
        raise FileNotFoundError(2, detail, "pi")
    return rec


def create_cursor_chat(exe=None, run=None, timeout=30.0) -> str:
    """SPEC-0437: `cursor-agent create-chat` stdout is a UUID (the chat id to `--resume`)."""
    run = subprocess.run if run is None else run
    exe = exe or resolve_cursor_agent()
    if not exe:
        raise FileNotFoundError(2, "cursor-agent not found on PATH or in the standard install dirs",
                                "cursor-agent")
    rec = run([exe, "create-chat"], capture_output=True, text=True, timeout=timeout)
    stdout = (getattr(rec, "stdout", None) or "")
    stderr = (getattr(rec, "stderr", None) or "")
    if getattr(rec, "returncode", 1):
        tail = (stderr or stdout).strip().splitlines()
        raise RuntimeError(tail[-1] if tail else f"create-chat exit {rec.returncode}")
    sid = stdout.strip().splitlines()[-1].strip() if stdout.strip() else ""
    if not sid:
        raise RuntimeError("cursor-agent create-chat produced no id")
    return sid


_CURSOR_LOGIN_FAIL = ("not logged", "not authenticated", "please log in", "login required",
                      "unauthorized")


def probe_cursor_sdk(environ=None) -> dict:
    """SPEC-0442: `{ok, version, login, error}` for a Cursor persona host.
    The package must import and `CURSOR_API_KEY` must be set. The key is never copied
    into the record. This is not a billed request."""
    env = os.environ if environ is None else environ
    out = {"ok": False, "version": None, "login": None, "error": None}
    try:
        import cursor_sdk  # noqa: F401
    except ImportError as e:
        out["error"] = (f"cursor-sdk is not installed ({e}). "
                        "Install the nightwright cursor extra.")
        return out
    try:
        from importlib.metadata import version
        out["version"] = version("cursor-sdk")
    except Exception:
        out["version"] = "unknown"
    if not (env.get("CURSOR_API_KEY") or "").strip():
        out["error"] = "CURSOR_API_KEY is not set"
        return out
    out["ok"] = True
    out["login"] = "CURSOR_API_KEY is set"
    return out


def ensure_cursor_persona(environ=None) -> dict:
    """Raise `FileNotFoundError` when a Cursor persona cannot start (SPEC-0442)."""
    rec = probe_cursor_sdk(environ)
    if not rec["ok"]:
        raise FileNotFoundError(2, rec["error"] or "cursor-sdk unavailable", "cursor-sdk")
    return rec


def probe_cursor_agent(environ=None, run=None, timeout=10.0) -> dict:
    """SPEC-0436: `{ok, exe, version, login, error}` from `--version` plus `status`/`whoami`.
    Not a paid `-p`."""
    run = subprocess.run if run is None else run
    exe = resolve_cursor_agent(environ)
    out = {"ok": False, "exe": exe, "version": None, "login": None, "error": None}
    if not exe:
        out["error"] = "cursor-agent not found on PATH or in the standard install dirs"
        return out
    try:
        p = run([exe, "--version"], capture_output=True, text=True, timeout=timeout)
        out["version"] = (p.stdout or p.stderr or "").strip() or None if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired) as e:
        out["error"] = f"`cursor-agent --version` failed: {e}"
        return out
    if not out["version"]:
        out["error"] = "`cursor-agent --version` failed"
        return out
    for args in ([exe, "status"], [exe, "whoami"]):
        try:
            p = run(args, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            out["error"] = f"`cursor-agent {args[1]}` failed: {e}"
            continue
        text = ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
        low = text.lower()
        if p.returncode == 0 and not any(s in low for s in _CURSOR_LOGIN_FAIL):
            out["ok"] = True
            out["login"] = text.splitlines()[0] if text else "logged in"
            out["error"] = None
            return out
        if any(s in low for s in _CURSOR_LOGIN_FAIL):
            out["error"] = f"`cursor-agent {args[1]}`: not logged in"
            return out
    out["error"] = out["error"] or "cursor-agent status/whoami did not show a logged-in session"
    return out


def _now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json_atomic(path, data) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def start_ticks(pid: int) -> int | None:
    """Field 22 of `/proc/<pid>/stat` (process start time), None off Linux or when gone."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return int(raw.rsplit(")", 1)[1].split()[19])


def _proc_state(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return raw.rsplit(")", 1)[1].split()[0]


def claim(run_dir, payload: dict) -> bool:
    """Create `proc.json` exclusively; False when someone else holds the claim. The payload is
    written to a temp file first and hard-linked into place, so `proc.json` never exists empty
    or partial (`link` fails with FileExistsError when the claim is taken)."""
    run_dir = Path(run_dir)
    path = run_dir / "proc.json"
    tmp = run_dir / f".proc.json.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
    finally:
        tmp.unlink(missing_ok=True)
    dfd = os.open(run_dir, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return True


def wrapper_alive(proc: dict | None) -> bool:
    if not proc or not proc.get("wrapper_pid"):
        return False
    pid = int(proc["wrapper_pid"])
    ticks = proc.get("wrapper_start_ticks")
    state = _proc_state(pid)
    if state is None:
        if os.path.isdir("/proc/self"):
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    if state == "Z":
        return False
    return ticks is None or start_ticks(pid) == ticks


def group_alive(child: dict | None) -> bool:
    if not child or not child.get("pgid"):
        return False
    pid, ticks = child.get("pid"), child.get("start_ticks")
    if pid and ticks is not None:
        now = start_ticks(int(pid))
        if now is not None and now != ticks:       # the leader's pid was reused
            return False
    try:
        os.killpg(int(child["pgid"]), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not _group_all_zombies(int(child["pgid"]))


def _group_all_zombies(pgid: int) -> bool:
    """True when every process of the group is a zombie (`/proc` only; else False)."""
    if not os.path.isdir("/proc/self"):
        return False
    seen = False
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            raw = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue
        fields = raw.rsplit(")", 1)[1].split()
        if int(fields[2]) == pgid:
            seen = True
            if fields[0] != "Z":
                return False
    return seen


def run_status(run_dir) -> str:
    """`finished` | `running` | `orphan_group` | `vanished` | `orphaned` | `unclaimed`."""
    run_dir = Path(run_dir)
    if (run_dir / "exit.json").is_file():
        return "finished"
    proc = read_json(run_dir / "proc.json")
    if proc is None:
        return "unclaimed"
    if proc.get("orphaned"):
        return "orphaned"
    if wrapper_alive(proc):
        return "running"
    if group_alive(read_json(run_dir / "child.json")):
        return "orphan_group"
    return "vanished"


def terminate_run(run_dir, *, kill: bool = False) -> bool:
    """Send TERM (or KILL) to a live run: the wrapper forwards TERM to the child's group.
    Returns True when something was signalled."""
    run_dir = Path(run_dir)
    if (run_dir / "exit.json").is_file():
        return False
    sig = signal.SIGKILL if kill else signal.SIGTERM
    sent = False
    child = read_json(run_dir / "child.json")
    if kill and group_alive(child):
        try:
            os.killpg(int(child["pgid"]), sig)
            sent = True
        except OSError:
            pass
    proc = read_json(run_dir / "proc.json")
    if proc and wrapper_alive(proc) and not kill:
        try:
            os.kill(int(proc["wrapper_pid"]), sig)
            sent = True
        except OSError:
            pass
    elif proc and not kill and not proc.get("orphaned") and group_alive(child):
        # the wrapper is gone: TERM the group ourselves, and leave a mark so the synthesized
        # `exit.json` says `killed_by: "signal"` (the run is interrupted, not an error)
        try:
            write_json_atomic(run_dir / "term.json", {"ts": time.time()})
            os.killpg(int(child["pgid"]), sig)
            sent = True
        except OSError:
            pass
    return sent


def kill_group(child: dict | None, *, grace: float = GRACE_S) -> None:
    """TERM the child's group, wait up to `grace`, then KILL."""
    if not child or not child.get("pgid"):
        return
    pgid = int(child["pgid"])
    for sig, wait_s in ((signal.SIGTERM, grace), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(pgid, sig)
        except OSError:
            return
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            if not group_alive(child):
                return
            time.sleep(0.05)


# ---- stream capture (SPEC-0362) -------------------------------------------------------------

def is_stream(run_dir) -> bool:
    """True when the run was launched in stream-json mode (`"stream": true` in `launch.json`).
    Fixed at launch: a run started before SPEC-0362 has no flag and is read as before."""
    return bool((read_json(Path(run_dir) / "launch.json") or {}).get("stream"))


def physical_lines(text: str) -> list[str]:
    """Split on ``\\n`` only. ``str.splitlines`` also breaks on U+2028/U+2029, which
    appear inside JSON strings (a tool result) and must stay one event (SPEC-0442)."""
    if text.endswith("\n"):
        text = text[:-1]
    if not text:
        return []
    return [ln[:-1] if ln.endswith("\r") else ln for ln in text.split("\n")]


def _result_line(text: str) -> str | None:
    """The last line of a stream-json text that is a `type: result` object, verbatim."""
    for line in reversed(physical_lines(text)):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            return line
    return None


def extract_result(run_dir) -> bool:
    """Copy the last `result` event of `stream.jsonl` to `result.json` (atomically). False,
    and no `result.json`, when the stream has none. Never raises on a bad or missing file."""
    run_dir = Path(run_dir)
    try:
        text = (run_dir / "stream.jsonl").read_text(encoding="utf-8", errors="replace")
        line = _result_line(text)
        if line is None:
            return False
        tmp = run_dir / "result.json.tmp"
        tmp.write_text(line + "\n", encoding="utf-8")
        os.replace(tmp, run_dir / "result.json")
        return True
    except OSError:
        return False


def _text(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def run_output(run_dir) -> str:
    """The text to parse for a run's result. A json-mode run (no stream flag): `result.json`,
    as before SPEC-0362. A stream run: `result.json`, extracted from `stream.jsonl` first when
    it is missing (the wrapper died before writing it); with no result event at all, the
    whole stream (it parses as an error carrying the stream's tail)."""
    run_dir = Path(run_dir)
    if not is_stream(run_dir):
        return _text(run_dir / "result.json")
    raw = _text(run_dir / "result.json")
    if not raw.strip() and extract_result(run_dir):
        raw = _text(run_dir / "result.json")
    return raw if raw.strip() else _text(run_dir / "stream.jsonl")


# ---- the wrapper process ------------------------------------------------------------------

def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    run_dir = Path(argv[0])
    state = {"term": False, "child": None, "term_at": None}

    def on_term(_signum, _frame):
        state["term"] = True
        state["term_at"] = state["term_at"] or time.monotonic()
        child = state["child"]
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except OSError:
                pass

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    me = os.getpid()
    if not claim(run_dir, {"wrapper_pid": me, "wrapper_start_ticks": start_ticks(me),
                           "host": socket.gethostname(), "started_at": _now_iso()}):
        os.write(2, f"run dir already claimed: {run_dir}\n".encode())
        return 3
    began = time.monotonic()
    exit_path = run_dir / "exit.json"

    def finish(code, *, timed_out=False, killed_by=None, sig=None, **extra):
        write_json_atomic(exit_path, {
            "exit_code": code, "timed_out": timed_out, "killed_by": killed_by, "signal": sig,
            "ended_at": _now_iso(), "duration_s": round(time.monotonic() - began, 3),
            "synthesized": False, **extra})

    try:
        launch = json.loads((run_dir / "launch.json").read_text(encoding="utf-8"))
        req = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
        timeout_s = float(req.get("timeout_s") or 0) or None
        env = child_env(launch.get("env"))
        stream = bool(launch.get("stream"))
        state["stream"] = stream
        if state["term"]:
            finish(-1, killed_by="signal", sig=signal.SIGTERM)
            return 0
        try:
            with open(run_dir / "prompt.md", "rb") as stdin, \
                    open(run_dir / ("stream.jsonl" if stream else "result.json"), "wb") as out, \
                    open(run_dir / "stderr.log", "wb") as err:
                argv, env = with_agent(launch["argv"], env)     # SPEC-0389 / SPEC-0436
                child = subprocess.Popen(argv, stdin=stdin, stdout=out, stderr=err,
                                         cwd=launch.get("cwd"), env=env, process_group=0)
        except OSError as e:
            (run_dir / "stderr.log").write_text(str(e), encoding="utf-8")
            finish(-1, error=str(e))
            return 0
        state["child"] = child
        write_json_atomic(run_dir / "child.json", {
            "pid": child.pid, "pgid": child.pid, "start_ticks": start_ticks(child.pid)})
        if state["term"]:
            on_term(signal.SIGTERM, None)
        killed_by, timed_out, kill_at, kill_sent = None, False, None, False
        deadline = began + timeout_s if timeout_s else None
        while child.poll() is None:
            now = time.monotonic()
            if deadline and now >= deadline and not timed_out:
                timed_out, killed_by, kill_at = True, "timeout", now + GRACE_S
                _killpg(child.pid, signal.SIGTERM)
            if state["term_at"] and kill_at is None:
                kill_at = state["term_at"] + GRACE_S
            if kill_at and now >= kill_at and not kill_sent:
                kill_sent = True
                _killpg(child.pid, signal.SIGKILL)
            time.sleep(0.05)
        rc = child.returncode
        _killpg(child.pid, signal.SIGKILL) if (timed_out or state["term"]) else None
        sig = -rc if rc < 0 else None
        if killed_by is None and state["term"]:     # we forwarded a TERM: however the child ended
            killed_by = "signal"
            sig = sig or signal.SIGTERM
        elif killed_by is None and sig is not None:
            killed_by = "external"
        if stream:
            extract_result(run_dir)
        finish(-1 if rc < 0 else rc, timed_out=timed_out, killed_by=killed_by, sig=sig)
    except BaseException as e:                       # leave a diagnosis, never a silent death
        import traceback
        traceback.print_exc()
        if not exit_path.exists():
            error = f"wrapper failed: {e!r}"
            child = state.get("child")
            if child is not None and child.poll() is None:
                # SPEC-0262: never leave `claude` running behind a written `exit.json`
                _killpg(child.pid, signal.SIGTERM)
                end = time.monotonic() + GRACE_S
                while child.poll() is None and time.monotonic() < end:
                    time.sleep(0.05)
                _killpg(child.pid, signal.SIGKILL)
                error += "; child process group killed"
            if state.get("stream"):
                extract_result(run_dir)
            finish(-1, error=error)
        return 1
    return 0


def _killpg(pgid, sig) -> None:
    try:
        os.killpg(pgid, sig)
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
