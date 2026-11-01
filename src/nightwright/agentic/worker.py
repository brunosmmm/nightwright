"""Remote gate worker (SPEC-0336): everything that runs on the worker host.

`gate_job` reads one `nw.job.v1` header line and `bundle_bytes` bytes of git bundle from stdin,
checks the commit out in a private `git clone --shared` of a per-repo object cache, runs the
gate list it was sent through `gates.run_gate_with_retries`, and writes one `nw.job.frame.v1`
JSON frame per line on stdout. stdin stays open: EOF before `done` is a cancel. `ssh_command`
is the SSH forced command: it execs only the `nw worker` subcommands, with a literal grammar.

Threat model: a gate is a command, so whoever may send a job has shell access as the worker
user. Containment is a dedicated unprivileged user (docs/WORKFLOW.md, "Remote gate worker")."""
from __future__ import annotations

import base64
import fcntl
import gzip
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from . import gate_slots
from . import gates as G
from .project import SCHEMA as PROJECT_SCHEMA
from .project import validate as validate_project
from .store import agentic_dir, now_iso

JOB_SCHEMA = "nw.job.v1"
FRAME_SCHEMA = "nw.job.frame.v1"
INFO_SCHEMA = "nw.worker.info.v1"
ENV_ALLOWLIST = frozenset({"NW_CAMPAIGN", "NW_SPEC_ID", "NW_BASE"})
MAX_LOG_BYTES = 16 * 1024 * 1024
MAX_HEADER_BYTES = 4 * 1024 * 1024
DEFAULT_KEEP_DAYS = 2
# `gc --keep-days` beyond this is "forever" (a larger count would overflow the float clock)
MAX_KEEP_DAYS = 100_000
SLOT_WAIT_ENV = "NW_WORKER_SLOT_WAIT_EVENT_S"
EXIT_ERROR = 3
EXIT_REFUSED = 2

# `\Z`, not `$`: `$` also matches before a trailing newline, and these grammars are literal
KEY_RE = re.compile(r"^[A-Za-z0-9._-]+\Z")
SHA_RE = re.compile(r"^[0-9a-f]{40,64}\Z")
DIGITS_RE = re.compile(r"^[0-9]+\Z")
# a branch name: no leading `-` or `/`; `base_name_ok` adds the rest of git's check-ref-format
# rules this charset can break, so a name git would refuse is a `validate` refusal
BASE_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]*\Z")
# a gate name `<other gate>.r<n>` would share files with that gate's retry `<n>`
RETRY_STEM_RE = re.compile(r"^(.+)\.r[0-9]+\Z")
# the worker's slot holds/waiters name a remote job `remote:<campaign>`, so they never read as
# one of the worker host's own local campaigns
SLOT_CAMPAIGN_PREFIX = "remote:"
JOB_REFS = "refs/nw/gate-jobs/"
HAVE_REFS = "refs/nw/have/"
# git variables a caller's environment could carry that would point our git calls elsewhere
_GIT_SCRUB = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
              "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR")


class WorkerError(Exception):
    def __init__(self, stage: str, error: str):
        super().__init__(error)
        self.stage = stage
        self.error = error


# ---- store -----------------------------------------------------------------------------------

def worker_dir(cfg) -> Path:
    return agentic_dir(cfg) / "worker"


def repos_dir(cfg) -> Path:
    return worker_dir(cfg) / "repos"


def jobs_dir(cfg) -> Path:
    return worker_dir(cfg) / "jobs"


def cache_path(cfg, repo_key) -> Path:
    return repos_dir(cfg) / f"{repo_key}.git"


def _have_path(cfg, repo_key) -> Path:
    return repos_dir(cfg) / f"{repo_key}.have.json"


@contextmanager
def repo_lock(cfg, repo_key):
    """The per-repo flock (`repos/<repo_key>.lock`): fetches, `refs/nw/have/*`, `have.json` and
    gc's deletions and pruning all happen under it."""
    repos_dir(cfg).mkdir(parents=True, exist_ok=True)
    fd = os.open(repos_dir(cfg) / f"{repo_key}.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def jobs_lock(cfg):
    """`worker/jobs.lock`: a claim (mkdir, `job.lock`, `job.json`) is atomic against gc's scans
    of `jobs/`, so gc never sees a half-claimed job dir as dead."""
    worker_dir(cfg).mkdir(parents=True, exist_ok=True)
    fd = os.open(worker_dir(cfg) / "jobs.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_have(cfg, repo_key) -> dict:
    try:
        rec = json.loads(_have_path(cfg, repo_key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in rec.items() if isinstance(v, (int, float))} \
        if isinstance(rec, dict) else {}


def _write_have(cfg, repo_key, have: dict) -> None:
    path = _have_path(cfg, repo_key)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(have, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_SCRUB}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(*args, cwd=None, stage="checkout", check=True) -> subprocess.CompletedProcess:
    p = subprocess.run(["git", *[str(a) for a in args]], cwd=cwd, env=_git_env(),
                       capture_output=True, text=True)
    if check and p.returncode != 0:
        raise WorkerError(stage, f"git {' '.join(str(a) for a in args[:2])} failed: "
                                 f"{(p.stderr or p.stdout).strip()[-2000:]}")
    return p


def _has_commit(cache: Path, sha) -> bool:
    return _git("-C", cache, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0


def _set_have(cfg, repo_key, cache: Path, shas) -> None:
    """Point `refs/nw/have/<sha>` at each sha (objects stay reachable after the job refs go)
    and stamp its last-used time. Caller holds the repo lock."""
    have = _read_have(cfg, repo_key)
    now = time.time()
    for sha in shas:
        _git("-C", cache, "update-ref", f"{HAVE_REFS}{sha}", sha)
        have[sha] = now
    _write_have(cfg, repo_key, have)


_GIT_LOCKS = ("HEAD.lock", "config.lock", "packed-refs.lock", "shallow.lock")


def _clear_stale_locks(cache: Path) -> None:
    """Every git that writes the cache runs under the repo lock, so a git lock file found while
    holding it is stale (left by a git killed hard: SIGKILL, OOM) and would fail every later
    ref update on it. Caller holds the repo lock."""
    found = [cache / n for n in _GIT_LOCKS]
    if (cache / "refs").is_dir():
        found += list((cache / "refs").rglob("*.lock"))
    for path in found:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


# git's own background maintenance (`gc --auto` / `maintenance run --auto`, which a fetch
# starts and may detach) would write the cache outside the repo lock, so it is off in every
# cache: the worker's own `gc` is the only one, and it runs under the lock.
_NO_AUTO_GC = (("gc.auto", "0"), ("maintenance.auto", "false"))


def _no_auto_maintenance(cache: Path) -> None:
    """Turn off auto gc/maintenance in the cache config (new and existing caches). Caller
    holds the repo lock."""
    for key, value in _NO_AUTO_GC:
        got = _git("-C", cache, "config", "--get", key, check=False).stdout.strip()
        if got != value:
            _git("-C", cache, "config", key, value)


def _list_refs(cache: Path, prefix: str) -> list[str]:
    p = _git("-C", cache, "for-each-ref", "--format=%(refname)", prefix, check=False)
    return [ln for ln in p.stdout.splitlines() if ln] if p.returncode == 0 else []


# ---- the header ------------------------------------------------------------------------------

def base_name_ok(name) -> bool:
    """`refs/heads/<name>` passes `git check-ref-format`: within BASE_RE's charset that means
    no `..`, no empty component (`a//b`, a trailing `/`), no component starting with `.` or
    ending in `.lock`, and no trailing `.`."""
    if not isinstance(name, str) or not BASE_RE.match(name) or ".." in name \
            or name.endswith("."):
        return False
    return all(c and not c.startswith(".") and not c.endswith(".lock")
               for c in name.split("/"))


def validate_header(h) -> None:
    """Raise `WorkerError("validate", ...)` unless `h` is a `nw.job.v1` gate header this worker
    can run. Nothing runs before this passes."""
    def bad(msg):
        raise WorkerError("validate", msg)
    if not isinstance(h, dict):
        bad("the header is not a JSON object")
    if h.get("schema") != JOB_SCHEMA:
        bad(f"schema {h.get('schema')!r} is not {JOB_SCHEMA}")
    if h.get("kind") != "gate":
        bad(f"kind {h.get('kind')!r} is not 'gate'")
    for key in ("job_id", "repo_key", "campaign", "spec_id"):
        if not isinstance(h.get(key), str) or not KEY_RE.match(h[key]):
            bad(f"{key} must match [A-Za-z0-9._-]+")
    if not isinstance(h.get("commit"), str) or not SHA_RE.match(h["commit"]):
        bad("commit must be a hex sha")
    base = h.get("base")
    if not isinstance(base, dict) or not isinstance(base.get("name"), str) \
            or not base_name_ok(base["name"]) \
            or not isinstance(base.get("sha"), str) or not SHA_RE.match(base["sha"]):
        bad("base must be {name: <branch>, sha: <hex sha>}")
    n = h.get("bundle_bytes")
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        bad("bundle_bytes must be a non-negative int")
    env = h.get("env") or {}
    if not isinstance(env, dict):
        bad("env must be an object")
    extra = sorted(set(env) - ENV_ALLOWLIST)
    if extra:
        bad(f"env keys outside the allowlist {sorted(ENV_ALLOWLIST)}: {extra}")
    if any(not isinstance(v, str) or "\x00" in v for v in env.values()):
        bad("env values must be strings without NUL bytes")
    if "NW_BASE" in env and env["NW_BASE"] != base["name"]:
        bad(f"env NW_BASE {env['NW_BASE']!r} differs from base.name {base['name']!r}")
    payload = h.get("payload")
    if not isinstance(payload, dict):
        bad("payload must be an object")
    seq = payload.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or not 0 <= seq <= 999:
        bad("payload.seq must be an int from 0 to 999")
    budget = payload.get("gate_budget_s")
    if budget is not None and (not isinstance(budget, (int, float)) or isinstance(budget, bool)
                               or not math.isfinite(budget) or budget <= 0):
        bad("payload.gate_budget_s must be a positive finite number")
    gl = payload.get("gates")
    if not isinstance(gl, list):
        bad("payload.gates must be a list")
    for g in gl:
        if not isinstance(g, dict) or not isinstance(g.get("name"), str) \
                or not KEY_RE.match(g["name"]) or not isinstance(g.get("cmd"), str):
            bad("each gate needs a name matching [A-Za-z0-9._-]+ and a string cmd")
        if any(isinstance(v, str) and "\x00" in v for v in g.values()):
            bad(f"gate {g['name']!r}: values must not contain NUL bytes")
    names = {g["name"] for g in gl}
    for name in sorted(names):
        m = RETRY_STEM_RE.match(name)
        if m and m.group(1) in names:
            bad(f"gate name {name!r} collides with the retry files of gate {m.group(1)!r}")
    # every other gate field (timeout_s, retries, cwd, results, ...) and unique names, by the
    # same rules a local `.nw/project.yaml` gate list meets, so nothing fails mid-run
    try:
        validate_project({"schema": PROJECT_SCHEMA, "gates": gl})
    except ValueError as e:
        bad(f"payload.gates: {e}")


# ---- frames ----------------------------------------------------------------------------------

class _Frames:
    """One JSON frame per line on stdout, plus a copy in `frames.jsonl` once the job is
    claimed. A closed stdout (the coordinator went away) is remembered, never raised."""

    def __init__(self, job_id, stream):
        self.job_id = job_id
        self.stream = stream
        self.log: Path | None = None
        self.closed = False
        self.sent: set[str] = set()

    def emit(self, type_, **payload) -> None:
        rec = {"schema": FRAME_SCHEMA, "type": type_, "job_id": self.job_id, "at": now_iso(),
               **payload}
        line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
        self.sent.add(type_)
        if self.log is not None:
            try:
                with open(self.log, "ab") as f:
                    f.write(line)
            except OSError:
                pass
        if self.closed:
            return
        try:
            self.stream.write(line)
            self.stream.flush()
        except (OSError, ValueError):
            self.closed = True


def _read_exact(stream, n: int, dest: Path) -> None:
    with open(dest, "wb") as f:
        left = n
        while left > 0:
            chunk = stream.read(min(left, 1 << 20))
            if not chunk:
                raise WorkerError("read", f"stdin ended {left} bytes short of bundle_bytes={n}")
            f.write(chunk)
            left -= len(chunk)


def _detach_stdin(stream) -> int | None:
    """A private dup of the job stream's fd for the watcher. When the stream is the process's
    fd 0, fd 0 becomes /dev/null: the gates inherit fd 0, and must get EOF on a read (as in a
    local run) rather than block on, or eat from, the coordinator's open job stream."""
    try:
        fd = stream.fileno()
    except (OSError, ValueError):
        return None
    watch = os.dup(fd)
    if fd == 0:
        null = os.open(os.devnull, os.O_RDONLY)
        try:
            os.dup2(null, 0)
        finally:
            os.close(null)
    return watch


def _watch_stdin(fd: int | None, cancel: threading.Event) -> None:
    """EOF on the job stream before `done` is a cancel: stop waiting and kill the live gates.
    It reads the raw fd: a daemon thread blocked inside the buffered reader holds its lock, and
    the interpreter aborts on that lock at exit."""
    try:
        if fd is not None:
            while os.read(fd, 65536):
                pass
    except (OSError, ValueError):
        pass
    cancel.set()
    G.terminate_all()


def _file_payload(path: Path) -> tuple[str, int]:
    """`(gzip+base64 text, truncated_bytes)` of `path`, keeping its last `MAX_LOG_BYTES`."""
    size = path.stat().st_size
    cut = max(0, size - MAX_LOG_BYTES) if path.suffix == ".log" else 0
    with open(path, "rb") as f:
        f.seek(cut)
        data = f.read()
    return base64.b64encode(gzip.compress(data)).decode("ascii"), cut


def gate_files(out: Path, stem: str) -> dict[str, str]:
    """Every file one gate wrote (`<stem>.log`, `<stem>.r<n>.log` and their `.json` sidecars),
    gzip+base64. A log over 16 MiB is cut to its last 16 MiB; its sidecar gets
    `truncated_bytes`."""
    retry = re.compile(rf"^{re.escape(stem)}\.r\d+\.(log|json)$")
    names = sorted(p.name for p in out.iterdir()
                   if p.name in (f"{stem}.log", f"{stem}.json") or retry.match(p.name))
    files, cuts = {}, {}
    for name in names:
        if name.endswith(".log"):
            files[name], cut = _file_payload(out / name)
            if cut:
                cuts[name[:-4] + ".json"] = cut
    for name in names:
        if not name.endswith(".json"):
            continue
        data = (out / name).read_bytes()
        if name in cuts:
            try:
                rec = json.loads(data)
                rec["truncated_bytes"] = cuts[name]
                data = (json.dumps(rec, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            except ValueError:
                pass
        files[name] = base64.b64encode(gzip.compress(data)).decode("ascii")
    return files


# ---- gate-job --------------------------------------------------------------------------------

def _slot_wait_event_s() -> float:
    try:
        v = float(os.environ.get(SLOT_WAIT_ENV, gate_slots.WAIT_EVENT_S))
    except ValueError:
        return gate_slots.WAIT_EVENT_S
    return v if v >= 0 else gate_slots.WAIT_EVENT_S


def _write_job_json(path: Path, rec: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _checkout(cfg, h: dict, job: Path, stdin, stop: "_Stop | None" = None) -> None:
    """Step 3: the bundle into the cache (under the repo lock), then a private clone of it at
    the job's commit with the base branch refs the gates expect. A pending signal (`stop`) is
    raised between the steps, never inside the lock or a running git."""
    stop = stop or _Stop()
    key, commit, base = h["repo_key"], h["commit"], h["base"]
    cache = cache_path(cfg, key)
    bundle = job / "job.bundle"
    if h["bundle_bytes"]:
        with stop.reading():
            _read_exact(stdin, h["bundle_bytes"], bundle)
    stop.check()
    with repo_lock(cfg, key):
        if not (cache / "HEAD").exists():
            _git("init", "-q", "--bare", cache)
        _clear_stale_locks(cache)
        _no_auto_maintenance(cache)
        if h["bundle_bytes"]:
            heads = _git("bundle", "list-heads", bundle).stdout.split()
            job_refs = [r for r in heads if r.startswith(JOB_REFS)]
            (job / "refs.json").write_text(json.dumps(job_refs) + "\n", encoding="utf-8")
            # plain flags only (git < 2.29 lacks --no-auto-maintenance/--no-write-fetch-head);
            # auto gc is off in the cache config (`_no_auto_maintenance`)
            _git("-C", cache, "fetch", "-q", bundle, f"{JOB_REFS}*:{JOB_REFS}*")
            bundle.unlink(missing_ok=True)
        for sha, what in ((commit, "commit"), (base["sha"], "base")):
            if not _has_commit(cache, sha):
                raise WorkerError("checkout", f"the cache has no {what} {sha} "
                                              "(missing from the bundle and not cached)")
        _set_have(cfg, key, cache, sorted({commit, base["sha"]}))
    stop.check()
    tree = job / "tree"
    _git("clone", "-q", "--shared", "--no-checkout", cache, tree)
    _git("-C", tree, "checkout", "-q", "--detach", commit)
    name = base["name"]
    _git("check-ref-format", f"refs/heads/{name}")
    _git("-C", tree, "update-ref", f"refs/heads/{name}", base["sha"])
    if name.startswith("origin/") and len(name) > len("origin/"):
        _git("-C", tree, "update-ref", f"refs/remotes/{name}", base["sha"])
        # `origin/<rest>` now names both refs (same sha); without this every resolve of it
        # warns "refname is ambiguous" on stderr, which a local checkout never does
        _git("-C", tree, "config", "core.warnAmbiguousRefs", "false")
    stop.check()


def _drop_job_refs(cfg, h: dict, job: Path) -> None:
    try:
        refs = json.loads((job / "refs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    cache = cache_path(cfg, h["repo_key"])
    with repo_lock(cfg, h["repo_key"]):
        for ref in refs:
            if isinstance(ref, str) and ref.startswith(JOB_REFS):
                _git("-C", cache, "update-ref", "-d", ref, check=False)
    (job / "refs.json").unlink(missing_ok=True)


def _cleanup(cfg, h: dict, job: Path) -> None:
    """Step 6 after the last frame: the tree and scratch go, the job refs go, `job.json` and
    `frames.jsonl` stay, then a default gc."""
    for sub in ("tree", "tmp", "out"):
        shutil.rmtree(job / sub, ignore_errors=True)
    (job / "job.bundle").unlink(missing_ok=True)
    _drop_job_refs(cfg, h, job)
    gc(cfg)


_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
_PRE_SLOT_STAGES = ("validate", "claim", "checkout")


class _Stop:
    """A stop signal received before the slot stage. The handler never raises at an arbitrary
    point: that could land in a lock's `finally` (the flock leaks and the job's own cleanup
    deadlocks on it) or inside `subprocess.run` (which SIGKILLs the git, leaving its ref
    locks). It raises straight away only while the job is blocked reading stdin (`reading`);
    otherwise the signal waits for the next `check()` between steps."""

    def __init__(self):
        self.name: str | None = None
        self.pending = False                    # received before the slot stage
        self.interruptible = False

    def error(self) -> WorkerError:
        return WorkerError("signal", f"stopped by {self.name}")

    def check(self) -> None:
        if self.pending:
            raise self.error()

    @contextmanager
    def reading(self):
        self.check()
        self.interruptible = True
        try:
            yield
        finally:
            self.interruptible = False


def _install_stop_handler(handler) -> dict:
    """Route SIGTERM/SIGINT/SIGHUP to `handler`; returns the previous handlers, to restore.
    Outside the main thread (an in-process caller) nothing is installed."""
    old = {}
    for sig in _STOP_SIGNALS:
        try:
            old[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):
            pass
    return old


def gate_job(cfg, stdin, stdout) -> int:
    """`nw worker gate-job`: run one job from `stdin` (binary), frames to `stdout` (binary).
    Returns the exit code: 0 after `done` (pass, fail or interrupted), 3 after `error`.

    SIGTERM/SIGINT/SIGHUP cancel like stdin EOF: before the slot stage the job ends with an
    `error` frame (`stage: "signal"`); from then on the slot wait or the live gates stop (their
    process groups are killed) and `done` goes out with `interrupted: true`."""
    frames = _Frames(None, stdout)
    state = {"stage": "validate"}
    stop = _Stop()
    cancel = threading.Event()
    h, job, lock_fd, job_rec = None, None, None, None

    def on_signal(signum, _frame):
        if stop.name:
            return
        stop.name = signal.Signals(signum).name
        cancel.set()
        if state["stage"] in _PRE_SLOT_STAGES:
            stop.pending = True
            if stop.interruptible:              # blocked on stdin: no lock, no git running
                stop.interruptible = False
                raise stop.error()
            return                              # raised at the next stop.check()
        # terminate_all takes the gates lock, which the interrupted main thread may hold
        threading.Thread(target=G.terminate_all, daemon=True).start()

    old_handlers = _install_stop_handler(on_signal)
    try:
        try:
            with stop.reading():
                line = stdin.readline(MAX_HEADER_BYTES)
            try:
                h = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as e:
                raise WorkerError("validate", f"the header is not JSON: {e}")
            if isinstance(h, dict) and isinstance(h.get("job_id"), str) \
                    and KEY_RE.match(h["job_id"]):
                frames.job_id = h["job_id"]
            validate_header(h)
            stop.check()

            state["stage"] = "claim"
            with jobs_lock(cfg):
                jobs_dir(cfg).mkdir(parents=True, exist_ok=True)
                try:
                    (jobs_dir(cfg) / h["job_id"]).mkdir()
                except FileExistsError:
                    raise WorkerError("claim", f"job {h['job_id']} already exists on this worker")
                job = jobs_dir(cfg) / h["job_id"]
                lock_fd = os.open(job / "job.lock", os.O_RDWR | os.O_CREAT, 0o644)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                frames.log = job / "frames.jsonl"
                payload = h["payload"]
                job_rec = {"schema": "nw.worker.job.v1", "job_id": h["job_id"],
                           "kind": h["kind"], "campaign": h["campaign"], "spec_id": h["spec_id"],
                           "repo_key": h["repo_key"], "commit": h["commit"], "base": h["base"],
                           "attempt": h.get("attempt"), "seq": payload["seq"],
                           "tier": payload.get("tier"), "pid": os.getpid(),
                           "claimed_at": now_iso()}
                _write_job_json(job / "job.json", job_rec)
            stop.check()

            state["stage"] = "checkout"
            _checkout(cfg, h, job, stdin, stop)

            state["stage"] = "slot"
            # a signal that came in before the line above is still a pre-slot one (`error`,
            # stage `signal`); from it on, a signal cancels the slot wait (`done` interrupted)
            stop.check()
            threading.Thread(target=_watch_stdin, args=(_detach_stdin(stdin), cancel),
                             daemon=True).start()
            frames.emit("accepted")
            _run_gates(cfg, h, job, frames, cancel)
            job_rec.update(state="done", finished_at=now_iso())
        except WorkerError as e:
            state["stage"] = "finish"               # a late signal no longer raises
            return _fail(cfg, h, job, frames, job_rec, e.stage, e.error)
        except Exception as e:                      # any exception becomes an error frame
            stage, state["stage"] = state["stage"], "finish"
            return _fail(cfg, h, job, frames, job_rec, stage, f"{type(e).__name__}: {e}")
        try:
            _write_job_json(job / "job.json", job_rec)
            _cleanup(cfg, h, job)
        except Exception as e:                      # `done` is out: report, never a second frame
            print(f"nw worker: cleanup after done failed: {e}", file=sys.stderr)
        return 0
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


def _fail(cfg, h, job, frames, job_rec, stage, error) -> int:
    done = "done" in frames.sent                # `done` went out: never a second final frame
    if done:
        print(f"nw worker: after done: {error}", file=sys.stderr)
    else:
        frames.emit("error", stage=stage, error=error)
    if job is not None:
        try:
            if job_rec is not None and not done:
                job_rec.update(state="error", stage=stage, error=error, finished_at=now_iso())
                _write_job_json(job / "job.json", job_rec)
            _cleanup(cfg, h, job)
        except Exception as e:
            print(f"nw worker: cleanup after error failed: {e}", file=sys.stderr)
    return 0 if done else EXIT_ERROR


def _run_gates(cfg, h: dict, job: Path, frames: _Frames, cancel: threading.Event) -> None:
    """Steps 4-5: the worker host's own gate slot, then each gate, one `gate` frame each, then
    `done`. `cancel` (stdin EOF) ends the slot wait or the running gate."""
    payload, base = h["payload"], h["base"]
    seq = payload["seq"]

    def announce(_occ, waited_s):
        frames.emit("slot_wait", waited_s=waited_s)

    with gate_slots.acquire(cfg, SLOT_CAMPAIGN_PREFIX + h["campaign"], stop=cancel, on_wait=announce,
                            wait_event_s=_slot_wait_event_s()) as hold:
        if hold.interrupted:
            frames.emit("done", ok=False, started_at=now_iso(), duration_s=0.0,
                        slot_wait_s=hold.waited_s, interrupted=True)
            return
        frames.emit("started", slot_wait_s=hold.waited_s)
        started_at = now_iso()
        t0 = time.monotonic()
        budget = payload.get("gate_budget_s")
        if budget is None:
            from .events import settings as events_settings
            budget = events_settings(cfg)["gate_budget_s"]
        deadline = t0 + float(budget)
        tree, out, tmp = job / "tree", job / "out", job / "tmp"
        out.mkdir(exist_ok=True)
        tmp.mkdir(exist_ok=True)
        env = {"NW_CAMPAIGN": h["campaign"], "NW_SPEC_ID": h["spec_id"], "NW_BASE": base["name"],
               **(h.get("env") or {}),
               "NW_WORKTREE": str(tree), "NW_DATA_DIR": str(cfg["data_dir"]),
               "NW_CONFIG_DIR": str(cfg.get("config_dir") or ""), **G.tmp_env(tmp)}
        ph = {"worktree": str(tree), "base": base["name"], "spec_id": h["spec_id"],
              "campaign": h["campaign"]}
        results, cut = [], False
        for gate in payload["gates"]:
            if cancel.is_set():
                cut = True
                break
            stem = f"{seq:03d}-{gate['name']}"
            r = G.run_gate_with_retries(gate, tree, env, out / f"{stem}.log", ph,
                                        log_rel=f"gates/{stem}.log", stop=cancel,
                                        deadline=deadline)
            results.append(r)
            frames.emit("gate", result=asdict(r), files=gate_files(out, stem))
        frames.emit("done", ok=all(r.ok for r in results if r.required),
                    started_at=started_at, duration_s=round(time.monotonic() - t0, 3),
                    slot_wait_s=hold.waited_s,
                    interrupted=cut or any(r.interrupted for r in results))


# ---- have / gc / info ------------------------------------------------------------------------

def have(cfg, repo_key, shas) -> list[str]:
    """The given shas whose commit is in the cache. Each reported sha gets (or keeps) its
    `refs/nw/have/<sha>` ref and a fresh last-used time, so it stays until the job arrives."""
    if not KEY_RE.match(repo_key or ""):
        raise ValueError(f"--repo {repo_key!r} must match [A-Za-z0-9._-]+")
    bad = [s for s in shas if not SHA_RE.match(s)]
    if bad:
        raise ValueError(f"not a hex sha: {bad[0]!r}")
    cache = cache_path(cfg, repo_key)
    if not (cache / "HEAD").exists():
        return []
    with repo_lock(cfg, repo_key):
        _clear_stale_locks(cache)
        present = [s for s in shas if _has_commit(cache, s)]
        if present:
            _set_have(cfg, repo_key, cache, present)
    return present


def _job_live(job: Path) -> tuple[bool, int | None]:
    """`(live, fd)`: live when another holder has `job.lock`; otherwise `fd` holds it for us."""
    try:
        fd = os.open(job / "job.lock", os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return True, None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return True, None
    return False, fd


def _live_jobs(cfg, remove_before: float | None = None, removed: list | None = None):
    """`(live_ids, live_shas)` of the job dirs whose `job.lock` another process holds, and the
    commit/base shas their `job.json` names. With `remove_before`, each dead job dir last
    touched before it is deleted (its name appended to `removed`). Caller holds `jobs_lock`."""
    live_ids, live_shas = set(), set()
    jd = jobs_dir(cfg)
    for job in sorted(jd.iterdir()) if jd.is_dir() else []:
        if not job.is_dir():
            continue
        live, fd = _job_live(job)
        if live:
            live_ids.add(job.name)
            try:
                rec = json.loads((job / "job.json").read_text(encoding="utf-8"))
                live_shas.update({rec.get("commit"), (rec.get("base") or {}).get("sha")})
            except (OSError, ValueError, AttributeError):
                pass
            continue
        try:
            if remove_before is not None and job.stat().st_mtime <= remove_before:
                shutil.rmtree(job, ignore_errors=True)
                removed.append(job.name)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    return live_ids, live_shas


def _gc_repo(cfg, key: str, cache: Path, now: float, keep_s: float, out: dict) -> None:
    """`gc`'s repo phase for one cache: under its repo lock, drop aged have refs and orphan job
    refs (the live set read again under the jobs lock), then prune the objects."""
    with repo_lock(cfg, key):
        _clear_stale_locks(cache)
        _no_auto_maintenance(cache)
        with jobs_lock(cfg):
            live_ids, live_shas = _live_jobs(cfg)
        hv = _read_have(cfg, key)
        changed = False
        for ref in _list_refs(cache, HAVE_REFS):
            sha = ref[len(HAVE_REFS):]
            if sha in live_shas:
                continue
            if sha not in hv or now - hv[sha] >= keep_s:
                _git("-C", cache, "update-ref", "-d", ref, check=False)
                hv.pop(sha, None)
                changed = True
                out["refs_removed"] += 1
        for ref in _list_refs(cache, JOB_REFS):
            name = ref[len(JOB_REFS):]
            owner = name[:-len("-base")] if name.endswith("-base") else name
            if name in live_ids or owner in live_ids:
                continue
            _git("-C", cache, "update-ref", "-d", ref, check=False)
            changed = True
            out["refs_removed"] += 1
        have_refs = {r[len(HAVE_REFS):] for r in _list_refs(cache, HAVE_REFS)}
        if changed or set(hv) - have_refs:
            _write_have(cfg, key, {s: t for s, t in hv.items() if s in have_refs})
        if changed:
            if _git("-C", cache, "gc", "--prune=now", "--quiet",
                    check=False).returncode == 0:
                out["pruned"].append(key)


def gc(cfg, keep_days: int = DEFAULT_KEEP_DAYS, now: float | None = None) -> dict:
    """Delete job dirs (except live ones, whose `job.lock` is held) and `refs/nw/have/*` refs
    at least `keep_days` old, plus job refs no live job owns; then `git gc --prune=now` each
    repo whose refs changed, so the objects go too. Shas a live job uses are kept. A cache
    that fails (a git error, an unwritable file) is listed in `errors` and the next repo still
    runs.

    The live set each repo's deletions rely on is read under that repo's lock *and* the jobs
    lock: a job is then either fully claimed (its `job.json` names its shas, its refs are
    kept) or has not fetched yet (it fetches after this gc's prune)."""
    now = time.time() if now is None else now
    keep_s = min(max(0, int(keep_days)), MAX_KEEP_DAYS) * 86400
    out = {"jobs_removed": [], "refs_removed": 0, "pruned": [], "errors": []}
    with jobs_lock(cfg):
        _live_jobs(cfg, remove_before=now - keep_s, removed=out["jobs_removed"])
    rd = repos_dir(cfg)
    for cache in sorted(rd.glob("*.git")) if rd.is_dir() else []:
        key = cache.name[:-4]
        if not KEY_RE.match(key):
            continue
        try:
            _gc_repo(cfg, key, cache, now, keep_s, out)
        except (WorkerError, OSError) as e:     # one broken cache never stops the others
            out["errors"].append({"repo": key, "error": str(e)})
    return out


def _version_out(*cmd) -> str | None:
    try:
        p = subprocess.run(list(cmd), capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.strip() or None if p.returncode == 0 else None


def info(cfg) -> dict:
    from .. import __version__
    pkg = Path(__file__).resolve().parents[1]
    sha = _version_out("git", "-C", str(pkg), "rev-parse", "HEAD")
    wd = worker_dir(cfg)
    probe = wd if wd.is_dir() else Path(cfg["data_dir"])
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return {"schema": INFO_SCHEMA, "protocol": JOB_SCHEMA, "version": __version__,
            "git_sha": sha, "data_dir": str(cfg["data_dir"]),
            "git": _version_out("git", "--version"), "uv": _version_out("uv", "--version"),
            "free_bytes": shutil.disk_usage(probe).free}


# ---- ssh-command -----------------------------------------------------------------------------

def check_ssh_command(original: str | None) -> list[str]:
    """The `nw worker` argv (`[sub, *args]`) `SSH_ORIGINAL_COMMAND` asks for, else raise
    `ValueError(reason)`. The grammar is literal: `info [--json]`;
    `have --repo <key> <sha>...`; `gate-job`; `gc [--keep-days <n>]`."""
    if not original or not original.strip():
        raise ValueError("empty command")
    try:
        words = shlex.split(original)
    except ValueError as e:
        raise ValueError(f"unparsable command: {e}")
    if len(words) < 3 or not (words[0] == "nw" or words[0].endswith("/nw")) \
            or words[1] != "worker":
        raise ValueError("only `nw worker <info|have|gate-job|gc>` is allowed")
    sub, args = words[2], words[3:]
    ok = False
    if sub == "info":
        ok = args in ([], ["--json"])
    elif sub == "have":
        ok = (len(args) >= 3 and args[0] == "--repo" and bool(KEY_RE.match(args[1]))
              and all(SHA_RE.match(a) for a in args[2:]))
    elif sub == "gate-job":
        ok = args == []
    elif sub == "gc":
        ok = args == [] or (len(args) == 2 and args[0] == "--keep-days"
                            and bool(DIGITS_RE.match(args[1])))
    else:
        raise ValueError(f"subcommand {sub!r} is not allowed")
    if not ok:
        raise ValueError(f"arguments {args!r} do not match the `{sub}` grammar")
    return [sub, *args]


def own_nw() -> list[str]:
    """The worker's own `nw` (never a path the client sent): `sys.argv[0]` when it is an `nw`
    executable (the forced command's absolute path), else this interpreter's module."""
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if os.path.basename(argv0) == "nw" and os.access(argv0, os.X_OK):
        return [argv0]
    return [sys.executable, "-m", "nightwright"]


def ssh_command(environ=None) -> int:
    """The forced command: exec the allowed `nw worker` call, or exit 2 having run nothing."""
    environ = os.environ if environ is None else environ
    try:
        argv = check_ssh_command(environ.get("SSH_ORIGINAL_COMMAND"))
    except ValueError as e:
        print(f"refused: {e}", file=sys.stderr)
        return EXIT_REFUSED
    exe = own_nw()
    os.execvp(exe[0], [*exe, "worker", *argv])
    return EXIT_REFUSED                         # not reached unless execvp is patched
