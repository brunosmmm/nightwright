"""Remote gate dispatch (SPEC-0338): the coordinator half of a gate run on a registered worker.

`gates.run_all` claims the seq, then waits on `pools_for`'s pool list. A remote hold calls `run`:
it cuts the commit under test (`git stash create` of a dirty tree, else HEAD) and the base into
a git bundle against what the worker already has (`nw worker have`), runs
`ssh <dest> nw worker gate-job` (SPEC-0336), streams the `nw.job.v1` header and the bundle,
reads the frames, writes each gate's files where a local run writes them, and builds the
`GateRun`. A transport failure (SSH, protocol, an `error` frame, EOF, a timeout) returns no run:
the caller re-runs the same gates locally under the same seq. A gate failure is a result."""
from __future__ import annotations

import base64
import collections
import datetime as dt
import hashlib
import json
import math
import os
import queue
import re
import signal
import subprocess
import threading
import time
import zlib
from dataclasses import dataclass, fields, replace
from pathlib import Path

from . import gates as G
from . import store
from . import workers as WK
from . import worker as W

COOLDOWN_S = 600
DEFAULT_SLOT_WAIT_S = 300.0         # `agentic.workers.slot_wait_s`: the wait for `started`
RUN_TIMEOUT_MARGIN_S = 120.0        # from `started`, the kill timer is `timeout_s` + this
MAX_JOB_TIMEOUT_S = 4 * 3600
STOP_GRACE_S = 10.0                 # a stop waits this long for `done` before killing SSH
END_GRACE_S = 10.0                  # after `done` or a failure, the SSH process gets this to exit
HAVE_TIMEOUT_S = 60.0
UPLOAD_MIN_BPS = 256 * 1024         # the header + bundle upload may take `slot_wait_s` + bytes/this
MAX_FILE_BYTES = W.MAX_LOG_BYTES + (1 << 20)   # a gate file's decompressed size, at most
FINDING_GATE = "remote-eligibility"
_POLL_S = 0.2
_CHUNK = 1 << 16
_GATE_FIELDS = tuple(f.name for f in fields(G.GateResult))


class RemoteFailure(Exception):
    """A transport failure (or a coordinator-side git failure): fall back to a local run.
    `cooldown` says whether the worker is put in cooldown for it."""

    def __init__(self, stage: str, error: str, *, cooldown: bool = True):
        super().__init__(error)
        self.stage = stage
        self.error = error
        self.cooldown = cooldown
        self.worker_wait_s = 0.0


class _Stopped(Exception):
    """A stop (SPEC-0229) arrived before the job was sent: nothing ran, nothing falls back."""


@dataclass
class Outcome:
    run: G.GateRun | None           # None: fell back (`failure` says why)
    worker_wait_s: float = 0.0      # the worker's own slot wait, once `started` was seen
    failure: RemoteFailure | None = None


def settings(cfg) -> dict:
    """`{"slot_wait_s": s}` from `agentic.workers.slot_wait_s`; anything but a positive finite
    number falls back to the default."""
    raw = (((cfg.get("agentic") or {}).get("workers")) or {}).get("slot_wait_s")
    ok = isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw) \
        and raw > 0
    return {"slot_wait_s": float(raw) if ok else DEFAULT_SLOT_WAIT_S}


# ---- eligibility -----------------------------------------------------------------------------

def ineligible(project_cfg: dict, worktree) -> dict | None:
    """Why this run must stay local (`{title, detail, files}`), else None: the project or a
    gate opts out, or the worktree has untracked, non-ignored files (the bundle carries
    commits only)."""
    if project_cfg.get("remote_gates") is False:
        return {"title": "the project sets remote_gates: false; gates run locally",
                "detail": ".nw/project.yaml remote_gates: false", "files": []}
    opted = [g.get("name") for g in project_cfg.get("gates") or [] if g.get("remote") is False]
    if opted:
        return {"title": f"gate {opted[0]} sets remote: false; gates run locally",
                "detail": ", ".join(str(n) for n in opted), "files": []}
    p = subprocess.run(["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard",
                        "-z"], capture_output=True, text=True)
    if p.returncode != 0:
        return {"title": "cannot list the worktree's untracked files; gates run locally",
                "detail": (p.stderr or "").strip()[-500:], "files": []}
    paths = [x for x in p.stdout.split("\0") if x]
    if paths:
        return {"title": f"{len(paths)} untracked file(s) in the worktree; gates run locally",
                "detail": "\n".join(paths[:5]), "files": paths[:5]}
    return None


def _record_ineligible(cfg, campaign_id, why: dict) -> None:
    """One `finding` per campaign: the first ineligible run only (a ledger scan decides).
    `gates`/`info` are outside divergence's counted sets."""
    if any(e.get("gate") == FINDING_GATE
           for e in store.read_ledger(cfg, campaign_id, types=["finding"])):
        return
    store.append_event(cfg, campaign_id, "finding",
                       {"persona": "gates", "severity": "info", "gate": FINDING_GATE,
                        "title": why["title"], "detail": why["detail"], "files": why["files"]},
                       by="gates")


def pools_for(cfg, project_cfg: dict, campaign_state: dict, *, seq: int,
              worker: str | None = None) -> list | None:
    """The `pools` for `gate_slots.acquire`, or None for local only. Eligibility is checked
    only when a remote pool exists, so without an enabled worker nothing extra runs. With
    `worker` (`nw queue gates --worker`): only that worker's pool, its cooldown ignored; an
    unknown or disabled worker, or an ineligible run, raises ValueError."""
    if worker is not None:
        rec = next((w for w in WK.load(cfg) if w["name"] == worker), None)
        if rec is None or not rec["enabled"] or "gate" not in rec["kinds"]:
            raise ValueError(f"worker {worker!r} is unknown or disabled (see `nw workers list`)")
        pools = [(worker, rec["slots"])]
    else:
        pools = WK.gate_pools(cfg)
    if all(name is WK.LOCAL for name, _n in pools):
        return None
    worktree = (campaign_state.get("repo") or {}).get("worktree")
    why = ineligible(project_cfg, worktree)
    if why is None:
        refusal = header_refusal(project_cfg, campaign_state, cfg, seq)
        if refusal is not None:
            why = {"title": "the worker would refuse this job; gates run locally",
                   "detail": refusal, "files": []}
    if why is None:
        return pools
    if worker is not None:
        raise ValueError(f"not eligible for remote gates: {why['title']}")
    _record_ineligible(cfg, campaign_state["id"], why)
    return None


# ---- the commit under test and the bundle ----------------------------------------------------

def _git(cwd, *args, check=True) -> str:
    """A coordinator-side git call; a failure is a `bundle`-stage fallback without cooldown."""
    try:
        p = subprocess.run(["git", "-C", str(cwd), *[str(a) for a in args]],
                           capture_output=True, text=True)
    except OSError as e:
        raise RemoteFailure("bundle", f"git {args[0]} failed to start: {e}", cooldown=False)
    if check and p.returncode != 0:
        raise RemoteFailure("bundle", f"git {' '.join(str(a) for a in args[:2])} failed: "
                                      f"{(p.stderr or p.stdout).strip()[-1000:]}", cooldown=False)
    return p.stdout.strip() if p.returncode == 0 else ""


def _is_ancestor(cwd, a, b) -> bool:
    return subprocess.run(["git", "-C", str(cwd), "merge-base", "--is-ancestor", a, b],
                          capture_output=True).returncode == 0


def repo_key(project: str | None, worktree) -> str:
    """The worker cache key: the lowercased project name plus the first 12 hex of sha256 of the
    coordinator repo's realpath (the repo every worktree of it shares)."""
    common = Path(_git(worktree, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = Path(worktree) / common
    root = Path(os.path.realpath(common))
    if root.name == ".git":
        root = root.parent
    name = re.sub(r"[^a-z0-9._-]+", "-", (project or "repo").lower()).strip(".-") or "repo"
    return f"{name}-{hashlib.sha256(os.fsencode(str(root))).hexdigest()[:12]}"


def _check_stop(stop) -> None:
    if G._stop_requested(stop):
        raise _Stopped()


def _have(worker: dict, key: str, shas: list[str], stop=None) -> list[str]:
    """The shas the worker's cache already has. A stop ends the SSH call at once (`_Stopped`)
    instead of waiting out `HAVE_TIMEOUT_S` against a slow host."""
    argv = worker["ssh_argv"] + [worker["ssh"], worker["nw"], "worker", "have", "--repo", key,
                                 *shas]
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError as e:
        raise RemoteFailure("have", f"ssh failed to start: {e}")
    deadline = time.monotonic() + HAVE_TIMEOUT_S
    try:
        while True:
            try:
                out, err = p.communicate(timeout=_POLL_S)
                break
            except subprocess.TimeoutExpired:
                pass
            _check_stop(stop)
            if time.monotonic() >= deadline:
                raise RemoteFailure("have", f"`nw worker have` timed out after "
                                            f"{HAVE_TIMEOUT_S:g}s")
    finally:
        if p.returncode is None:
            _end(p, 0.0)
            _close(p.stdout)
            _close(p.stderr)
    if p.returncode != 0:
        tail = (err or "").strip().splitlines()[-1:] or [""]
        raise RemoteFailure("have", f"`nw worker have` failed (exit {p.returncode}): {tail[0]}")
    got = {ln.strip() for ln in out.splitlines()}
    return [s for s in shas if s in got]


def job_timeout_s(cfg, gates: list) -> int:
    """`gate_budget_s` + the sum over gates of `timeout_s` x (1 + `retries`), capped at 4 h."""
    from .events import settings as events_settings
    total = float(events_settings(cfg)["gate_budget_s"])
    for g in gates:
        total += int(g.get("timeout_s") or 1200) * (1 + int(g.get("retries") or 0))
    return int(min(total, MAX_JOB_TIMEOUT_S))


def prepare(worker: dict, project_cfg: dict, campaign_state: dict, cfg, seq: int, job_id: str,
            bundle: Path, *, stop=None) -> dict:
    """Steps 1-5: the commit under test, the base, `have`, the bundle (written to `bundle`,
    absent when nothing is left to send) and the job header. A stop (`stop` or the gates'
    sticky one) before or during `have` raises `_Stopped`."""
    repo = campaign_state.get("repo") or {}
    wt, base_name = repo["worktree"], repo.get("base") or "main"
    sha = _git(wt, "stash", "create") or _git(wt, "rev-parse", "HEAD")    # stash stack untouched
    base_sha = _git(wt, "rev-parse", "--verify", f"{base_name}^{{commit}}")
    asked = [base_sha]
    mb = _git(wt, "merge-base", "HEAD", base_sha, check=False)
    if mb and mb not in asked:
        asked.append(mb)
    key = repo_key(campaign_state.get("project") or project_cfg.get("project"), wt)
    try:
        WK.ensure_ssh_dir(cfg)
    except OSError as e:
        raise RemoteFailure("have", f"ssh socket directory unusable: {e}")
    _check_stop(stop)
    have = _have(worker, key, asked, stop)
    _check_stop(stop)
    refs = [(f"{W.JOB_REFS}{job_id}", sha), (f"{W.JOB_REFS}{job_id}-base", base_sha)]
    try:
        for ref, tip in refs:
            _git(wt, "update-ref", ref, tip)
        excl = [h for h in have if _is_ancestor(wt, h, sha) or _is_ancestor(wt, h, base_sha)]
        # a ref whose tip the worker has is left out: git would drop it anyway, and refuse an
        # empty bundle; the worker finds both objects through the header's commit/base.sha
        keep = [ref for ref, tip in refs if not any(_is_ancestor(wt, tip, h) for h in excl)]
        if keep:
            _git(wt, "bundle", "create", "-q", bundle, *keep, *[f"^{h}" for h in excl])
            size = bundle.stat().st_size
        else:
            size = 0
    except OSError as e:
        raise RemoteFailure("bundle", f"bundle: {e}", cooldown=False)
    finally:
        for ref, _tip in refs:
            _git(wt, "update-ref", "-d", ref, check=False)
    return _header(project_cfg, campaign_state, cfg, seq, job_id, key, sha, base_sha, have, size)


def _header(project_cfg: dict, campaign_state: dict, cfg, seq: int, job_id: str, key: str,
            sha: str, base_sha: str, have: list, size: int) -> dict:
    """The `nw.job.v1` header (SPEC-0336). The gate list drops the coordinator-only `remote`
    key."""
    from .events import settings as events_settings
    cid = campaign_state["id"]
    base_name = (campaign_state.get("repo") or {}).get("base") or "main"
    gl = [{k: v for k, v in g.items() if k != "remote"} for g in project_cfg.get("gates") or []]
    spec_id = campaign_state.get("spec_id")
    return {"schema": W.JOB_SCHEMA, "kind": "gate", "job_id": job_id, "repo_key": key,
            "campaign": cid, "spec_id": spec_id if spec_id else "-", "commit": sha,
            "base": {"name": base_name, "sha": base_sha}, "have": have, "bundle_bytes": size,
            "env": {"NW_CAMPAIGN": str(cid), "NW_SPEC_ID": str(spec_id or ""),
                    "NW_BASE": base_name},
            "timeout_s": job_timeout_s(cfg, gl), "attempt": 0,
            "payload": {"seq": seq, "gates": gl,
                        "gate_budget_s": float(events_settings(cfg)["gate_budget_s"])}}


def header_refusal(project_cfg: dict, campaign_state: dict, cfg, seq: int) -> str | None:
    """Why the worker's `validate_header` would refuse this run's job, checked on a dry header
    (placeholder shas and cache key) before anything is bundled or sent, else None. Such a
    refusal comes from the coordinator's own config (a gate name outside [A-Za-z0-9._-]+, a
    base branch git refuses, a seq outside 0..999) and would repeat on every run, so it keeps
    the run local instead of costing the worker a cooldown."""
    zero = "0" * 40
    try:
        h = _header(project_cfg, campaign_state, cfg, seq, f"{campaign_state['id']}-gate-"
                    f"{seq:03d}-a0", "repo-000000000000", zero, zero, [], 0)
        W.validate_header(h)
    except W.WorkerError as e:
        return e.error
    except (TypeError, ValueError, KeyError, AttributeError) as e:
        return f"the job header cannot be built: {e}"
    return None


# ---- the gate-job connection -----------------------------------------------------------------

def _writer(fd: int, header: dict, bundle: Path, cancel: threading.Event,
            written: threading.Event) -> None:
    """Header line, then the bundle, on a private dup of SSH's stdin (closed when done, so a
    close of the original by the main thread gives the worker EOF). stdin stays open.
    `written` is set once the writer is done, sent or not."""
    try:
        data = (json.dumps(header, ensure_ascii=False) + "\n").encode("utf-8")
        _write_all(fd, data, cancel)
        if header["bundle_bytes"]:
            with open(bundle, "rb") as f:
                while not cancel.is_set():
                    chunk = f.read(_CHUNK)
                    if not chunk:
                        break
                    _write_all(fd, chunk, cancel)
    except OSError:
        pass                                    # the remote went away: the reader sees EOF
    finally:
        os.close(fd)
        written.set()


def _write_all(fd: int, data: bytes, cancel: threading.Event) -> None:
    view = memoryview(data)
    while view and not cancel.is_set():
        view = view[os.write(fd, view):]


def _reader(stream, lines: queue.Queue) -> None:
    try:
        for line in iter(stream.readline, b""):
            lines.put(line)
    except (OSError, ValueError):
        pass
    lines.put(None)


def _drain(stream, tail: collections.deque) -> None:
    try:
        for line in iter(stream.readline, b""):
            tail.append(line.decode("utf-8", errors="replace").rstrip())
    except (OSError, ValueError):
        pass


def _close(stream) -> None:
    try:
        stream.close()
    except (OSError, ValueError):
        pass


def _end(proc: subprocess.Popen, grace_s: float) -> int | None:
    """Wait `grace_s` for SSH to exit, then TERM and KILL its process group."""
    try:
        return proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        pass
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            break
        try:
            return proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            continue
    return proc.poll()


def _frame(line: bytes, job_id: str) -> dict:
    try:
        rec = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as e:   # deep nesting: Recursion
        raise RemoteFailure("protocol", f"malformed frame: {type(e).__name__}: {e}: "
                                        f"{line[:200]!r}")
    if not isinstance(rec, dict) or rec.get("schema") != W.FRAME_SCHEMA \
            or not isinstance(rec.get("type"), str):
        raise RemoteFailure("protocol", f"not a {W.FRAME_SCHEMA} frame: {line[:200]!r}")
    if rec.get("job_id") not in (job_id, None):
        raise RemoteFailure("protocol", f"frame for job {rec.get('job_id')!r}, not {job_id}")
    return rec


def _num(frame: dict, key: str) -> float:
    """A worker-supplied duration (absent: 0). Anything but a finite, non-negative number is a
    protocol failure, so a bad frame falls back instead of raising out of `run_all`."""
    v = frame.get(key)
    if v is None:
        return 0.0
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        raise RemoteFailure("protocol", f"`{frame['type']}` frame: bad {key} {v!r}")
    return float(v)


def _flag(frame: dict, key: str, *, required: bool) -> bool:
    v = frame.get(key)
    if v is None and not required:
        return False
    if not isinstance(v, bool):
        raise RemoteFailure("protocol", f"`{frame['type']}` frame: bad {key} {v!r}")
    return v


def _check_changed(frame: dict) -> None:
    """`done`'s `changed`: the paths the gates wrote in the worker's checkout. Those writes
    never reach the coordinator's worktree (a regenerated ledger, a formatter's output), so a
    run that made any is re-run locally, without cooldown: the local run leaves the worktree
    as it always has. `null` (git could not tell in the worker's checkout) is treated as a
    change. A worker that does not say is a protocol failure."""
    changed = frame.get("changed")
    if changed is None and "changed" in frame:
        raise RemoteFailure("tree", "the worker could not tell what the gates wrote in its "
                                    "checkout; re-running locally", cooldown=False)
    if not isinstance(changed, list) or not all(isinstance(p, str) for p in changed):
        raise RemoteFailure("protocol", f"`done` frame: bad changed {changed!r} (the worker "
                                        f"cannot tell what the gates wrote)")
    if changed:
        raise RemoteFailure("tree", f"the gates changed {len(changed)} file(s) in the worker's "
                                    f"checkout ({', '.join(changed[:5])}); re-running locally "
                                    f"so the worktree gets those writes", cooldown=False)


def _inflate(name: str, data: str) -> bytes:
    """A gate file's gzip+base64 payload, decoded. A corrupt payload, or one inflating past
    `MAX_FILE_BYTES` (the worker cuts a log to `MAX_LOG_BYTES`), is a protocol failure."""
    try:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = d.decompress(base64.b64decode(data, validate=True), MAX_FILE_BYTES + 1)
    except (ValueError, zlib.error) as e:
        raise RemoteFailure("protocol", f"gate file {name}: {e}")
    if len(raw) > MAX_FILE_BYTES:
        raise RemoteFailure("protocol", f"gate file {name}: over {MAX_FILE_BYTES} bytes")
    if not d.eof:
        raise RemoteFailure("protocol", f"gate file {name}: truncated gzip stream")
    return raw


def _bad_result_field(result: dict) -> str | None:
    """The first field of a worker's gate result whose JSON type a `GateResult` cannot hold,
    else None (absent fields are left to the constructor)."""
    for key, v in result.items():
        if key in ("required", "ok", "timed_out", "interrupted"):
            bad = not isinstance(v, bool)
        elif key == "exit_code":
            bad = v is not None and (isinstance(v, bool) or not isinstance(v, int))
        elif key == "duration_s":
            bad = isinstance(v, bool) or not isinstance(v, (int, float)) \
                or not math.isfinite(v) or v < 0
        elif key in ("cmd", "cwd"):
            bad = not isinstance(v, str)
        elif key == "summary":
            bad = v is not None and not isinstance(v, dict)
        elif key in ("attempts", "flaky"):
            bad = v is not None and not isinstance(v, list)
        else:
            continue
        if bad:
            return key
    return None


def _take_gate(frame: dict, gates_dir: Path, seq: int, names: set) -> G.GateResult:
    """Write one `gate` frame's files under `gates/` and return its `GateResult` with the
    campaign-relative log and a tail recomputed from the local copy. Worker data a
    `GateResult` cannot hold is a protocol failure, never an exception out of `run_all`."""
    result, files = frame.get("result"), frame.get("files")
    if not isinstance(result, dict) or not isinstance(files, dict) \
            or not isinstance(result.get("name"), str) or result["name"] not in names:
        raise RemoteFailure("protocol", "gate frame without a known gate's result and files")
    bad = _bad_result_field(result)
    if bad is not None:
        raise RemoteFailure("protocol", f"gate result: bad {bad} {result[bad]!r:.200}")
    stem = f"{seq:03d}-{result['name']}"
    ok_name = re.compile(rf"^{re.escape(stem)}(\.r[0-9]+)?\.(log|json)\Z")
    gates_dir.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        if not isinstance(name, str) or not ok_name.match(name) or not isinstance(data, str):
            raise RemoteFailure("protocol", f"gate frame names a bad file {name!r}")
        (gates_dir / name).write_bytes(_inflate(name, data))
    log = gates_dir / f"{stem}.log"
    try:
        r = G.GateResult(**{k: result[k] for k in _GATE_FIELDS if k in result})
        return replace(r, log=f"gates/{stem}.log", tail=G.tail(log),
                       attempts=tuple(r.attempts or ()), flaky=tuple(r.flaky or ()))
    except TypeError as e:
        raise RemoteFailure("protocol", f"gate result: {e}")


def _start_io(proc, header: dict, bundle: Path, cancel: threading.Event,
              written: threading.Event, lines: queue.Queue, err_tail: collections.deque) -> None:
    """The writer (on a dup of SSH's stdin), reader and stderr threads. A failure to start them
    is an `ssh` failure on the coordinator's side (no cooldown); the caller's `finally` ends
    SSH."""
    fd = None
    try:
        fd = os.dup(proc.stdin.fileno())
        threading.Thread(target=_writer, args=(fd, header, bundle, cancel, written),
                         daemon=True).start()
        fd = None                               # the writer owns it now
        for target, args in ((_reader, (proc.stdout, lines)), (_drain, (proc.stderr, err_tail))):
            threading.Thread(target=target, args=args, daemon=True).start()
    except (OSError, RuntimeError) as e:
        raise RemoteFailure("ssh", f"cannot start the gate-job I/O: {e}", cooldown=False)
    finally:
        if fd is not None:
            os.close(fd)


def _dispatch(worker: dict, header: dict, bundle: Path, cfg, cid, seq: int, stop) -> Outcome:
    argv = worker["ssh_argv"] + [worker["ssh"], worker["nw"], "worker", "gate-job"]
    slot_wait_s = settings(cfg)["slot_wait_s"]
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
    except OSError as e:
        raise RemoteFailure("ssh", f"ssh failed to start: {e}")
    cancel, written = threading.Event(), threading.Event()
    lines: queue.Queue = queue.Queue()
    err_tail: collections.deque = collections.deque(maxlen=20)
    order = [g["name"] for g in header["payload"]["gates"]]
    names = set(order)
    gates_dir = store.gates_dir(cfg, cid)
    results: list = []
    started, worker_wait = False, 0.0
    t0, started_at = time.monotonic(), store.now_iso()
    # the upload (header + bundle) gets its own bound, scaled to the bundle; the wait for
    # `started` is bounded by `slot_wait_s` from the end of the upload
    upload_s = slot_wait_s + header["bundle_bytes"] / UPLOAD_MIN_BPS
    deadline, upload_seen = t0 + upload_s, False
    stop_at = None
    grace = END_GRACE_S

    def stderr_tail() -> str:
        return f": {err_tail[-1]}" if err_tail else ""

    def interrupted() -> Outcome:
        return Outcome(G.GateRun(seq, False, tuple(results), started_at,
                                 round(time.monotonic() - t0, 3), interrupted=True,
                                 worker=worker["name"]), worker_wait)
    try:
        _start_io(proc, header, bundle, cancel, written, lines, err_tail)
        while True:
            if stop_at is None and G._stop_requested(stop):     # SPEC-0229: cancel, wait, kill
                cancel.set()
                _close(proc.stdin)
                stop_at = time.monotonic() + STOP_GRACE_S
            now = time.monotonic()
            if not upload_seen and written.is_set():
                upload_seen = True
                if not started:
                    deadline = now + slot_wait_s
            if stop_at is not None and now >= stop_at:
                grace = 0.0
                return interrupted()
            if stop_at is None and now >= deadline:
                if not upload_seen and not started:
                    # a link too slow for this bundle fails the same way next run: cooldown
                    raise RemoteFailure("upload", f"the header and {header['bundle_bytes']} B "
                                                  f"bundle were not sent within {upload_s:g}s"
                                                  f"{stderr_tail()}")
                if not started:
                    raise RemoteFailure("slot_wait", f"the worker held no gate slot after "
                                                     f"{slot_wait_s:g}s", cooldown=False)
                raise RemoteFailure("timeout", f"no `done` within {header['timeout_s']}s + "
                                               f"{RUN_TIMEOUT_MARGIN_S:g}s of `started`")
            try:
                line = lines.get(timeout=_POLL_S)
            except queue.Empty:
                continue
            if line is None:                    # EOF before `done`
                if stop_at is not None:
                    return interrupted()
                rc = _end(proc, END_GRACE_S)
                raise RemoteFailure("ssh", f"the connection ended before `done` (exit {rc})"
                                           f"{stderr_tail()}")
            if not line.strip():
                continue
            frame = _frame(line, header["job_id"])
            kind = frame["type"]
            if kind == "error":
                stage = str(frame.get("stage") or "error")
                # `claim` (a duplicate job id) and `validate` (the coordinator's own header)
                # say nothing about the host
                raise RemoteFailure(stage, str(frame.get("error") or ""),
                                    cooldown=stage not in ("claim", "validate"))
            if kind == "started":
                started = True
                worker_wait = _num(frame, "slot_wait_s")
                deadline = time.monotonic() + header["timeout_s"] + RUN_TIMEOUT_MARGIN_S
            elif kind == "gate":
                results.append(_take_gate(frame, gates_dir, seq, names))
            elif kind == "done":
                ok, cut = _flag(frame, "ok", required=True), \
                    _flag(frame, "interrupted", required=False)
                duration = _num(frame, "duration_s")
                if not started:
                    worker_wait = _num(frame, "slot_wait_s")
                if cut and stop_at is None:
                    # the worker cut the run short on its own (SIGTERM/SIGINT/SIGHUP: the host
                    # is going away): a transport failure, never a red gate
                    raise RemoteFailure("interrupted", "the worker interrupted the run (signal)")
                if not cut:
                    _check_changed(frame)
                    if [r.name for r in results] != order:
                        raise RemoteFailure("protocol", f"`done` after results for "
                                                        f"{[r.name for r in results]}, not "
                                                        f"{order}")
                    # the run's verdict is the received results', as a local run computes it
                    ok = all(r.ok for r in results if r.required)
                return Outcome(G.GateRun(seq, ok, tuple(results),
                                         str(frame.get("started_at") or started_at), duration,
                                         interrupted=cut, worker=worker["name"]), worker_wait)
            # `accepted` and `slot_wait` are progress only
    except RemoteFailure as f:
        grace = min(grace, 2.0)
        if stop_at is not None:                 # a stop asked for this end: no fallback
            return interrupted()
        f.worker_wait_s = worker_wait if started else 0.0
        raise
    finally:
        cancel.set()
        _close(proc.stdin)
        _end(proc, grace)


# ---- failure bookkeeping ---------------------------------------------------------------------

def set_cooldown(cfg, name: str, seconds: float = COOLDOWN_S) -> None:
    until = (dt.datetime.now().astimezone() + dt.timedelta(seconds=seconds)) \
        .isoformat(timespec="seconds")

    def fn(records):
        for rec in records:
            if rec.get("name") == name:
                rec["cooldown_until"] = until
    try:
        WK.update(cfg, fn)
    except (OSError, ValueError):
        pass                                    # an unreadable registry is reported elsewhere


def _record_failure(cfg, cid, name: str, job_id: str, seq: int, f: RemoteFailure) -> None:
    for p in store.gates_dir(cfg, cid).glob(f"{seq:03d}-*"):
        p.unlink(missing_ok=True)
    store.append_event(cfg, cid, "worker_failed",
                       {"worker": name, "stage": f.stage, "error": f.error[-2000:],
                        "job_id": job_id, "cooldown": f.cooldown}, by="gates")
    if f.cooldown:
        set_cooldown(cfg, name)


def run(worker: str, project_cfg: dict, campaign_state: dict, cfg, seq: int, owner: str,
        stop: threading.Event | None = None) -> Outcome:
    """One remote gate run under the caller's claim (`owner`) and remote slot. A transport
    failure is recorded (`worker_failed`, the worker's cooldown, this run's `gates/<seq>-*`
    files deleted) and returned with `run=None`, for the caller's local fallback."""
    cid = campaign_state["id"]
    job_id = f"{cid}-gate-{seq:03d}-a0"
    bundle = store.child_tmp(cfg, cid, owner) / "job.bundle"
    started_at, t0 = store.now_iso(), time.monotonic()
    try:
        _check_stop(stop)
        rec = next((w for w in WK.load(cfg) if w["name"] == worker), None)
        if rec is None:
            raise RemoteFailure("registry", f"worker {worker!r} is no longer registered",
                                cooldown=False)
        header = prepare(rec, project_cfg, campaign_state, cfg, seq, job_id, bundle, stop=stop)
        _check_stop(stop)                       # no job is started only to be cancelled
        return _dispatch(rec, header, bundle, cfg, cid, seq, stop)
    except _Stopped:                            # SPEC-0229: nothing was sent, nothing ran
        return Outcome(G.GateRun(seq, False, (), started_at, round(time.monotonic() - t0, 3),
                                 interrupted=True))
    except RemoteFailure as f:
        _record_failure(cfg, cid, worker, job_id, seq, f)
        return Outcome(None, f.worker_wait_s, f)
    finally:
        bundle.unlink(missing_ok=True)
