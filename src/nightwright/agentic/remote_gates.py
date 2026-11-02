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

from . import gate_slots
from . import gates as G
from . import remote_jobs as RJ
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
REJECTED_GATE = "worker-rejected"
_POLL_S = 0.2
_CHUNK = 1 << 16
_GATE_FIELDS = tuple(f.name for f in fields(G.GateResult))
# gate keys the worker never sees (SPEC-0376): a tier-filtered list keeps `reuse_from` on a close
# gate but not the loop gate it names, which `validate_header` (here and on the worker) refuses
_COORDINATOR_ONLY = frozenset({"remote", "reuse_from"})


class RemoteFailure(Exception):
    """A transport failure (or a coordinator-side git failure): fall back to a local run.
    `cooldown` says whether the worker is put in cooldown for it. `rejected` (SPEC-0367) marks
    a worker that understood the job but cannot run it (a `validate` error) or speaks another
    dialect (every `protocol` failure): its install is probably stale. `job_error` (SPEC-0387)
    marks an `error` frame a live worker sent for a failure inside the job (ENOSPC, a bug): the
    host is fine, so no cooldown; the job is retried elsewhere (`_failed`)."""

    def __init__(self, stage: str, error: str, *, cooldown: bool = True,
                 rejected: bool = False, job_error: bool = False):
        super().__init__(error)
        self.job_error = job_error
        self.stage = stage
        self.error = error
        self.cooldown = cooldown
        self.rejected = rejected or stage == "protocol"
        self.worker_wait_s = 0.0


def _error_failure(stage: str, frame: dict, *, rejected: bool = False) -> RemoteFailure:
    """The `RemoteFailure` of a worker's `error` frame. SPEC-0387: a `job_error` frame (its
    own traceback appended to the message) leaves the worker out of cooldown."""
    err = str(frame.get("error") or "")
    job_error = frame.get("job_error") is True
    if job_error and isinstance(frame.get("traceback"), str):
        err = f"{err}\n{frame['traceback']}"
    return RemoteFailure(stage, err,
                         cooldown=stage not in ("claim", "validate") and not job_error,
                         rejected=rejected, job_error=job_error)


class _Stopped(Exception):
    """A stop (SPEC-0229) arrived before the job was sent: nothing ran, nothing falls back."""


@dataclass
class Outcome:
    run: G.GateRun | None           # None: fell back (`failure` says why)
    worker_wait_s: float = 0.0      # the worker's own slot wait, once `started` was seen
    failure: RemoteFailure | None = None
    lost: dict | None = None        # SPEC-0342: the give-up's record (`lost`), for the retry


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
                        "-z"], capture_output=True, encoding="utf-8", errors="replace")
    # errors="replace": a non-UTF-8 file name is still an untracked file, not an exception
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
    unknown, disabled or draining worker, or an ineligible run, raises ValueError."""
    if worker is not None:
        rec = next((w for w in WK.load(cfg) if w["name"] == worker), None)
        if rec is None or not rec["enabled"] or "gate" not in rec["kinds"]:
            raise ValueError(f"worker {worker!r} is unknown or disabled (see `nw workers list`)")
        if rec["draining"]:                     # SPEC-0343
            raise ValueError(f"worker {worker!r} is unknown, disabled or draining "
                             "(see `nw workers list`)")
        pools = [(worker, rec["slots"])]
    else:
        pools = _affine_first(cfg, campaign_state, WK.gate_pools(cfg))
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


def _affine_first(cfg, campaign_state: dict, pools: list) -> list:
    """SPEC-0345: the campaign's affine worker's pool first, unless it is failing (a full one
    falls through to the next pool). A failing worker (disabled, last check failed, or it lost
    a job of this campaign since the affinity was last set, which also voids the affinity) goes
    after the other remote pools, even when the registry order put it first. Gate runs only
    read the affinity, never set it."""
    from . import persona_gate as PG
    by_name = {name: (name, n) for name, n in pools}
    names = PG.order(cfg, campaign_state, [name for name, _n in pools])
    recs = {w["name"]: w for w in WK.load(cfg)}
    aff = PG.affinity(cfg, campaign_state)

    def bad(name) -> bool:
        if name is WK.LOCAL:
            return False
        rec = recs.get(name)
        return (name == aff) if rec is None else PG.failing(cfg, campaign_state, rec)

    ok = [n for n in names if n is not WK.LOCAL and not bad(n)]
    worse = [n for n in names if bad(n)]
    local = [n for n in names if n is WK.LOCAL]
    return [by_name[n] for n in ok + worse + local]


def retry_pools(cfg, project_cfg: dict, campaign_state: dict, *, seq: int, excluded,
                worker: str | None = None) -> list | None:
    """SPEC-0342: the `pools` of a lost job's next attempt: `pools_for`'s list (its
    eligibility checks first) without the `excluded` workers (SPEC-0374: worker pools only,
    unless `agentic.gates.local_overflow` appends the local pool); None (local only) when no
    remote pool is left."""
    try:
        pools = pools_for(cfg, project_cfg, campaign_state, seq=seq, worker=worker)
    except ValueError:                          # `--worker`'s worker was disabled since
        return None
    if pools is None:
        return None
    pools = [p for p in pools if p[0] not in set(excluded)]
    if all(name is WK.LOCAL for name, _n in pools):
        return None
    return pools


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


def commit_under_test(worktree) -> str:
    """SPEC-0368: the commit the gates run: `git stash create` of a dirty tree, else HEAD.
    `stash create` exits 1 with no output when stat information is stale but no content
    differs (a touched file, a rebase that rewrote mtimes); that is HEAD only when
    `git diff --quiet HEAD --` agrees (`GIT_OPTIONAL_LOCKS=0`: it never writes the index).
    Anything else is a `bundle`-stage failure without cooldown. The stash stack stays untouched."""
    wt = str(worktree)
    try:
        p = subprocess.run(["git", "-C", wt, "stash", "create"], capture_output=True, text=True)
    except OSError as e:
        raise RemoteFailure("bundle", f"git stash failed to start: {e}", cooldown=False)
    out, err = (p.stdout or "").strip(), (p.stderr or "").strip()
    if p.returncode == 0:
        return out or _git(wt, "rev-parse", "HEAD")
    if p.returncode == 1 and not out and not err:
        try:
            d = subprocess.run(["git", "-C", wt, "diff", "--quiet", "HEAD", "--"],
                               capture_output=True, text=True,
                               env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        except OSError as e:
            raise RemoteFailure("bundle", f"git diff failed to start: {e}", cooldown=False)
        if d.returncode == 0:
            return _git(wt, "rev-parse", "HEAD")
        raise RemoteFailure("bundle", "stash create found changes but made no commit",
                            cooldown=False)
    raise RemoteFailure("bundle", f"git stash create failed: {(err or out)[-1000:]}",
                        cooldown=False)


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
    key, sha, base_sha, have, size = cut(worker, project_cfg, campaign_state, cfg, job_id,
                                         bundle, stop=stop)
    return _header(project_cfg, campaign_state, cfg, seq, job_id, key, sha, base_sha, have, size,
                   host_slots=worker.get("slots"))


def cut(worker: dict, project_cfg: dict, campaign_state: dict, cfg, job_id: str, bundle: Path,
        *, stop=None) -> tuple:
    """Steps 1-4 of `prepare` (shared with SPEC-0344's persona jobs): `(repo_key, commit under
    test, base sha, have, bundle bytes)`, the bundle written to `bundle`."""
    repo = campaign_state.get("repo") or {}
    wt, base_name = repo["worktree"], repo.get("base") or "main"
    # SPEC-0381: a standalone gate job names its commit (a branch tip); campaigns never do
    sha = repo.get("commit") or commit_under_test(wt)   # stash stack untouched
    base_sha = _git(wt, "rev-parse", "--verify", f"{base_name}^{{commit}}")
    asked = [base_sha]
    mb = _git(wt, "merge-base", sha, base_sha, check=False)
    if mb and mb not in asked:
        asked.append(mb)
    key = repo_key(campaign_state.get("project") or project_cfg.get("project"), wt)
    if worker.get("mode") != "pull":            # SPEC-0334: a pull worker gets everything
        try:
            WK.ensure_record_ssh_dir(cfg, worker)
        except OSError as e:
            # the coordinator's own directory, not the worker's fault: no cooldown
            raise RemoteFailure("ssh", f"ssh socket directory unusable: {e}", cooldown=False)
    _check_stop(stop)
    have = [] if worker.get("mode") == "pull" else _have(worker, key, asked, stop)
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
    return key, sha, base_sha, have, size


def _header(project_cfg: dict, campaign_state: dict, cfg, seq: int, job_id: str, key: str,
            sha: str, base_sha: str, have: list, size: int, host_slots=None) -> dict:
    """The `nw.job.v1` header (SPEC-0336). The gate list drops the coordinator-only keys
    (`_COORDINATOR_ONLY`): `remote`, and `reuse_from` (SPEC-0376), whose reuse decision the
    close made before dispatch and whose loop gate a close-tier list never carries.
    SPEC-0391: `host_slots` (the worker's registry `slots`) goes in as `payload.host_slots`,
    the worker host's slot limit for this job."""
    from .events import settings as events_settings
    cid = campaign_state["id"]
    base_name = (campaign_state.get("repo") or {}).get("base") or "main"
    gl = [{k: v for k, v in g.items() if k not in _COORDINATOR_ONLY}
          for g in project_cfg.get("gates") or []]
    spec_id = campaign_state.get("spec_id")
    payload = {"seq": seq, "gates": gl,
               "gate_budget_s": float(events_settings(cfg)["gate_budget_s"])}
    if host_slots is not None:
        payload["host_slots"] = host_slots
    return {"schema": W.JOB_SCHEMA, "kind": "gate", "job_id": job_id, "repo_key": key,
            "campaign": cid, "spec_id": spec_id if spec_id else "-", "commit": sha,
            "base": {"name": base_name, "sha": base_sha}, "have": have, "bundle_bytes": size,
            "env": {"NW_CAMPAIGN": str(cid), "NW_SPEC_ID": str(spec_id or ""),
                    "NW_BASE": base_name, **G.full_suite_env(campaign_state)},   # SPEC-0420
            "timeout_s": job_timeout_s(cfg, gl), "attempt": 0, "payload": payload}


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


def _check_changed(frame: dict) -> tuple:
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
    return tuple(changed)


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


def _take_gate(frame: dict, gates_dir: Path, seq: int, names: set,
               write: bool = True) -> G.GateResult:
    """Write one `gate` frame's files under `gates/` and return its `GateResult` with the
    campaign-relative log and a tail recomputed from the local copy. Worker data a
    `GateResult` cannot hold is a protocol failure, never an exception out of `run_all`.
    `write=False` (SPEC-0341: a frame at or below the record's cursor, already applied before
    a restart) checks the frame and rebuilds the result without rewriting the files."""
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
        raw = _inflate(name, data)
        if write:
            (gates_dir / name).write_bytes(raw)
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


def _kept_changed(frame: dict):
    """SPEC-0381: `done`'s `changed` for a run with no local re-run (a standalone gate job):
    recorded, never a fallback. `null` stays None; a malformed value is a protocol failure."""
    changed = frame.get("changed")
    if changed is None:
        return None
    if not isinstance(changed, list) or not all(isinstance(p, str) for p in changed):
        raise RemoteFailure("protocol", f"`done` frame: bad changed {changed!r}")
    return tuple(changed)


def _dispatch(worker: dict, header: dict, bundle: Path, cfg, cid, seq: int, stop, *,
              gates_dir: Path | None = None, keep_changed: bool = False) -> Outcome:
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
    gates_dir = store.gates_dir(cfg, cid) if gates_dir is None else Path(gates_dir)
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
                raise _error_failure(stage, frame, rejected=stage == "validate")
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
                changed = None
                if not cut:
                    changed = _kept_changed(frame) if keep_changed else _check_changed(frame)
                    if [r.name for r in results] != order:
                        raise RemoteFailure("protocol", f"`done` after results for "
                                                        f"{[r.name for r in results]}, not "
                                                        f"{order}")
                    # the run's verdict is the received results', as a local run computes it
                    ok = all(r.ok for r in results if r.required)
                return Outcome(G.GateRun(seq, ok, tuple(results),
                                         str(frame.get("started_at") or started_at), duration,
                                         interrupted=cut, worker=worker["name"],
                                         changed=changed), worker_wait)
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


def _worker_ssh(cfg, name: str) -> str:
    try:
        return next((w.get("ssh") or "USER@HOST" for w in WK.load(cfg) if w["name"] == name),
                    "USER@HOST")
    except (OSError, ValueError):
        return "USER@HOST"


def rejection_notice(cfg, name: str, f: RemoteFailure) -> str:
    """The stderr line for a rejected remote gate job (SPEC-0367), printed on every one."""
    return (f"nw: worker {name} rejected the remote gate job ({f.stage}: {f.error[:200]}); "
            f"running gates locally. The worker is probably stale: nw workers bootstrap "
            f"{name} --ssh {_worker_ssh(cfg, name)} --reinstall")


def _record_rejection(cfg, cid, name: str, f: RemoteFailure) -> None:
    """One `finding` per (campaign, worker): the first rejection only (a ledger scan decides).
    `gates`/`info` are outside divergence's counted sets."""
    title = f"worker {name} rejected the gate job; its install may be stale"
    if any(e.get("gate") == REJECTED_GATE and e.get("title") == title
           for e in store.read_ledger(cfg, cid, types=["finding"])):
        return
    ssh = _worker_ssh(cfg, name)
    store.append_event(cfg, cid, "finding",
                       {"persona": "gates", "severity": "info", "gate": REJECTED_GATE,
                        "title": title,
                        "detail": f"{f.stage}: {f.error[:500]}\nrun nw workers check {name}, "
                                  f"then nw workers bootstrap {name} --ssh {ssh} --reinstall",
                        "files": []}, by="gates")


def _record_failure(cfg, cid, name: str, job_id: str, seq: int, f: RemoteFailure) -> None:
    for p in store.gates_dir(cfg, cid).glob(f"{seq:03d}-*"):
        p.unlink(missing_ok=True)
    store.append_event(cfg, cid, "worker_failed",
                       {"worker": name, "stage": f.stage, "error": f.error[-2000:],
                        "job_id": job_id, "cooldown": f.cooldown,
                        **({"rejected": True} if f.rejected else {})}, by="gates")
    if f.rejected:
        _record_rejection(cfg, cid, name, f)
    if f.cooldown:
        set_cooldown(cfg, name)


def run(worker: str, project_cfg: dict, campaign_state: dict, cfg, seq: int, owner: str,
        stop: threading.Event | None = None, *, tier: str | None = None, attempt: int = 1,
        excluded=(), on_attempt=None) -> Outcome:
    """One remote gate run under the caller's claim (`owner`) and remote slot. A transport
    failure is recorded (`worker_failed`, the worker's cooldown, this run's `gates/<seq>-*`
    files deleted) and returned with `run=None`, for the caller's local fallback. SPEC-0341: a
    worker whose cached `info` lists `async` gets `submit` + `attach` with a job record
    (`_run_async`); any other keeps the synchronous `gate-job`. A seq that already has a job
    record (an explicit seq; `gates.next_gate_seq` allocates past every record) also keeps
    `gate-job`: the worker may still hold that job id. `tier` is written to the record.
    SPEC-0342: `attempt` and `excluded` (the workers earlier attempts were lost on) are a lost
    job's retry: the job id carries the attempt and the record its exclusions.
    `on_attempt(job_id, recorded)` is called once the job is about to be sent (its record
    written, for an async worker; `recorded` False for a `gate-job` one, which has none and
    keeps its `-a0` id), so a stop or a transport failure before that leaves no trace of it.
    SPEC-0343: the record is re-read after the slot is taken; a worker that is no longer
    `placeable` (draining, disabled, or without the `gate` kind) gets nothing sent, as an
    unregistered one."""
    cid = campaign_state["id"]
    job_id = f"{cid}-gate-{seq:03d}-a0"
    bundle = store.child_tmp(cfg, cid, owner) / "job.bundle"
    started_at, t0 = store.now_iso(), time.monotonic()
    rec = job = None
    try:
        _check_stop(stop)
        rec = next((w for w in WK.load(cfg) if w["name"] == worker), None)
        if rec is None:
            raise RemoteFailure("registry", f"worker {worker!r} is no longer registered",
                                cooldown=False)
        if not WK.placeable(rec):               # SPEC-0343: drained or disabled since the take
            why = "takes no gates (no 'gate' in kinds)" \
                if rec["enabled"] and not rec["draining"] else "is draining or disabled"
            raise RemoteFailure("registry", f"worker {worker} {why}", cooldown=False)
        if RJ.has_async(rec) and RJ.read(cfg, RJ.gate_job_id(cid, seq, attempt)) is None:
            job_id = RJ.gate_job_id(cid, seq, attempt)
            return _run_async(rec, project_cfg, campaign_state, cfg, seq, job_id, bundle, stop,
                              tier, attempt=attempt, excluded=excluded, on_attempt=on_attempt)
        header = prepare(rec, project_cfg, campaign_state, cfg, seq, job_id, bundle, stop=stop)
        _check_stop(stop)                       # no job is started only to be cancelled
        if on_attempt is not None:
            on_attempt(job_id, False)
        return _dispatch(rec, header, bundle, cfg, cid, seq, stop)
    except _Stopped:                            # SPEC-0229: nothing was sent, nothing ran
        return Outcome(G.GateRun(seq, False, (), started_at, round(time.monotonic() - t0, 3),
                                 interrupted=True))
    except _GiveUp as g:
        return _give_up(cfg, cid, g.worker, g.rec, seq, g.stage)
    except RemoteFailure as f:
        job = RJ.read(cfg, job_id) if job_id != f"{cid}-gate-{seq:03d}-a0" else None
        if job is not None:                     # submitted: cancel it, the record goes `local`
            return _failed(cfg, cid, rec, job, seq, f)
        _record_failure(cfg, cid, worker, job_id, seq, f)
        return Outcome(None, f.worker_wait_s, f)
    finally:
        bundle.unlink(missing_ok=True)


def run_detached(worker: str, project_cfg: dict, state: dict, cfg, seq: int, gates_dir: Path,
                 stop: threading.Event | None = None, *, keep_changed: bool = False) -> G.GateRun:
    """SPEC-0379: one gate run on `worker` for a caller that is not a campaign (the base-branch
    sentinel): the synchronous `gate-job` path only (no `remote_jobs` record, so nothing for a
    runner restart to adopt), logs written to `gates_dir`, and no campaign ledger. `state` is
    the campaign-shaped `{id, project, spec_id, repo: {worktree, base}}` `prepare` reads; its
    `id` names the job. A transport failure sets the worker's cooldown (as `_record_failure`
    does) and is raised as RemoteFailure; a stop returns an interrupted run. SPEC-0381:
    `keep_changed` records the files the gates wrote in the worker's checkout instead of
    failing with `tree` (a standalone job has no local re-run)."""
    cid = state["id"]
    job_id = f"{cid}-gate-{seq:03d}-t{int(time.time())}"
    gates_dir = Path(gates_dir)
    gates_dir.mkdir(parents=True, exist_ok=True)
    bundle = gates_dir / f".{job_id}.bundle"
    started_at, t0 = store.now_iso(), time.monotonic()
    try:
        _check_stop(stop)
        rec = next((w for w in WK.load(cfg) if w["name"] == worker), None)
        if rec is None or not WK.placeable(rec):
            raise RemoteFailure("registry", f"worker {worker!r} is not placeable", cooldown=False)
        header = prepare(rec, project_cfg, state, cfg, seq, job_id, bundle, stop=stop)
        _check_stop(stop)
        out = _dispatch(rec, header, bundle, cfg, cid, seq, stop, gates_dir=gates_dir,
                        keep_changed=keep_changed)
        return out.run
    except _Stopped:
        return G.GateRun(seq, False, (), started_at, round(time.monotonic() - t0, 3),
                         interrupted=True)
    except RemoteFailure as f:
        for q in gates_dir.glob(f"{seq:03d}-*"):
            q.unlink(missing_ok=True)
        if f.cooldown:
            set_cooldown(cfg, worker)
        raise
    finally:
        bundle.unlink(missing_ok=True)


# ---- async jobs: submit, attach, re-attach, adoption, cancel (SPEC-0341) --------------------

BACKOFF_START_S = 2.0               # re-attach backoff: 2, 4, 8, ... capped at BACKOFF_MAX_S
BACKOFF_MAX_S = 60.0
CANCEL_GRACE_S = 10.0               # a cancel attaches this long for the job's `done`
CALL_TIMEOUT_S = 60.0               # one `poll` / `cancel` call
_now = time.time                    # the wall clock of `last_seen_at` / `deadline_at` (tests swap it)


def _sleep(seconds: float, stop) -> bool:
    """Wait `seconds`; True when a stop (SPEC-0229) cut the wait short. Tests swap it."""
    end = time.monotonic() + seconds
    while not G._stop_requested(stop):
        left = end - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(_POLL_S, left))
    return True


class _Either:
    """`stop` or a code-change detach (`detach_attached`): what the attach loop checks."""

    def __init__(self, *events):
        self.events = [e for e in events if e is not None]

    def is_set(self) -> bool:
        return any(e.is_set() for e in self.events)


_ATTACHED: dict = {}                # campaign -> the detach Event of its step's attach loop
_ATTACHED_LOCK = threading.Lock()


def attached(cid) -> bool:
    """A gate step of `cid` is in its attach loop: the job lives on the worker, so the runner
    may exec without waiting for it (SPEC-0231 code-change restart)."""
    with _ATTACHED_LOCK:
        return cid in _ATTACHED


def detach_attached(cid) -> bool:
    """Claim `cid`'s attached step for a detach (its record stays live); False when none is
    attached. The claim and the step's own exit (`_leave`) are serialized: once this returns
    True the step returns a detached run, even if it was about to give up and fall back."""
    with _ATTACHED_LOCK:
        ev = _ATTACHED.pop(cid, None)
        if ev is None:
            return False
        ev.set()
        return True


def detach_all_attached(cids) -> bool:
    """Claim every step of `cids` for a detach, all or none: False (nothing claimed) when any
    of them is no longer attached, e.g. it left its attach loop to give up."""
    with _ATTACHED_LOCK:
        if not all(cid in _ATTACHED for cid in cids):
            return False
        for cid in cids:
            _ATTACHED.pop(cid).set()
        return True


def _leave(cid, ev: threading.Event) -> bool:
    """The step leaves its attach loop; True when a detach claimed it first."""
    with _ATTACHED_LOCK:
        if _ATTACHED.get(cid) is ev:
            del _ATTACHED[cid]
        return ev.is_set()


# the give-up reasons that blame the worker (its cooldown): not `deadline`, and not
# `unaccepted` (a job the worker never accepted: the coordinator died during its submit)
_BLAME = ("unreachable", "lost")
# SPEC-0334: the give-up reasons that charge the worker nothing: no cooldown and no recorded
# loss (`rotated`: `nw workers token --rotate` released a pull worker's live jobs). The retry
# still excludes the worker and counts the attempt against `max_remote_attempts` (SPEC-0342)
UNCHARGED = ("rotated",)


class _GiveUp(RemoteFailure):
    """The coordinator gives up on an async job (`stage` is the reason: `unreachable`, `lost`,
    `unaccepted`, `deadline`): `worker_job_lost`, then the local fallback."""

    def __init__(self, reason: str, worker: dict | None, rec: dict):
        super().__init__(reason, f"remote job {rec['job_id']}: {reason}",
                         cooldown=reason in _BLAME)
        self.worker, self.rec = worker, rec


def _gone(cfg, rec: dict, state) -> str:
    """The give-up reason for a job the worker reports `lost` or `unknown`. An `unknown` job
    whose record never saw the worker accept it (no `accepted_at`, cursor 0: the coordinator
    died while `submit` was still uploading) is `unaccepted`, which does not blame the worker;
    anything else is `lost`."""
    cur = RJ.read(cfg, rec["job_id"]) or rec
    if state == "unknown" and not cur.get("accepted_at") and not int(cur.get("cursor") or 0):
        return "unaccepted"
    return "lost"


def _argv(worker: dict, *args) -> list[str]:
    return worker["ssh_argv"] + [worker["ssh"], worker["nw"], "worker", *[str(a) for a in args]]


def _lines(raw: bytes) -> list[dict]:
    out = []
    for ln in raw.splitlines():
        try:
            rec = json.loads(ln.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _call(worker: dict, *args, timeout: float = CALL_TIMEOUT_S, input: bytes | None = None):
    """One short SSH call: `(returncode, JSON lines)`, or None when SSH could not run or timed
    out (and always for a pull worker, SPEC-0334: nothing reaches it over SSH)."""
    if worker.get("mode") == "pull":
        return None
    try:
        p = subprocess.run(_argv(worker, *args), input=input, capture_output=True,
                           timeout=timeout, start_new_session=True,
                           stdin=None if input is not None else subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.returncode, _lines(p.stdout), p.stderr


def _status_of(lines: list[dict]) -> str | None:
    st = [r for r in lines if r.get("type") == "status"]
    return str(st[-1].get("state")) if st else None


def poll(worker: dict, job_id: str, after: int = 0) -> str | None:
    """`poll JOB --from N --renew`: the job's state (renewing its lease), None when the worker
    could not be reached."""
    got = _call(worker, "poll", job_id, "--from", int(after), "--renew")
    if got is None or got[0] not in (0, 1):     # 1: an unknown job, with its status line
        return None
    return _status_of(got[1])


def _submit(worker: dict, header: dict, bundle: Path, cfg) -> None:
    """`nw worker submit` with the header line and the bundle on stdin; returns once the worker
    printed `accepted` (or the current state of an identical earlier submit). SPEC-0334: a pull
    worker's job is queued for its lease instead."""
    if worker.get("mode") == "pull":
        from . import pull_jobs as PJ
        try:
            return PJ.submit(cfg, worker, header, bundle)
        except OSError as e:
            raise RemoteFailure("queue", f"could not queue the job: {e}", cooldown=False)
    data = (json.dumps(header, ensure_ascii=False) + "\n").encode("utf-8")
    if header["bundle_bytes"]:
        data += bundle.read_bytes()
    upload_s = settings(cfg)["slot_wait_s"] + header["bundle_bytes"] / UPLOAD_MIN_BPS
    try:
        p = subprocess.run(_argv(worker, "submit"), input=data, capture_output=True,
                           timeout=upload_s, start_new_session=True)
    except subprocess.TimeoutExpired:
        raise RemoteFailure("upload", f"`nw worker submit` did not answer within {upload_s:g}s")
    except OSError as e:
        raise RemoteFailure("ssh", f"ssh failed to start: {e}")
    lines = _lines(p.stdout)
    err = next((r for r in lines if r.get("type") == "error"), None)
    if err is not None:
        stage = str(err.get("stage") or "error")
        raise RemoteFailure(stage, str(err.get("error") or ""),
                            cooldown=stage not in ("claim", "validate"))
    state = _status_of(lines)
    if p.returncode != 0 or state not in ("accepted", "running", "done"):
        tail = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        raise RemoteFailure("ssh", f"`nw worker submit` failed (exit {p.returncode}, state "
                                   f"{state!r}): {tail[0]}")


def tree_under_test(worktree) -> tuple[str, str]:
    """`(HEAD, tree of the commit under test)` of `worktree`, as `prepare` cuts it: a dirty
    tree's `git stash create` commit is new on every call, but its tree is not. (A stat-dirty,
    content-clean tree is HEAD's tree; a real git failure raises `RemoteFailure`: SPEC-0368.)"""
    sha = commit_under_test(worktree)
    return _git(worktree, "rev-parse", "HEAD"), _git(worktree, "rev-parse", f"{sha}^{{tree}}")


def adoptable(rec: dict, worktree) -> bool | None:
    """SPEC-0341: a live record is adopted only when the worktree still holds what the job
    was submitted for (same HEAD and same tree under test); otherwise its replayed results
    would be for code no longer there. A record without them is never adopted. None: git
    failed (a held lock, an I/O error), so nobody knows yet; the caller must not cancel."""
    if not rec.get("head") or not rec.get("tree"):
        return False
    try:
        return tree_under_test(worktree) == (rec["head"], rec["tree"])
    except RemoteFailure:
        return None


def _run_async(worker: dict, project_cfg: dict, campaign_state: dict, cfg, seq: int,
               job_id: str, bundle: Path, stop, tier: str | None = None, *, attempt: int = 1,
               excluded=(), on_attempt=None) -> Outcome:
    """Prepare, submit (`attempt`), write the job record, then follow the job."""
    cid = campaign_state["id"]
    header = prepare(worker, project_cfg, campaign_state, cfg, seq, job_id, bundle, stop=stop)
    header["attempt"] = attempt
    _check_stop(stop)
    wt = (campaign_state.get("repo") or {})["worktree"]
    head = _git(wt, "rev-parse", "HEAD")
    tree = _git(wt, "rev-parse", f"{header['commit']}^{{tree}}")
    submitted = _now()
    # the record comes first: a submit the worker accepted but whose reply never arrived (or a
    # crash right after it) still has a record, so the job is cancelled (`_failed`) or adopted
    rec = RJ.create(cfg, job_id=job_id, campaign=cid, worker=worker["name"], attempt=attempt,
                    seq=seq, submitted_at=submitted,
                    deadline_at=submitted + header["timeout_s"]
                    + RJ.settings(cfg)["worker_ttl_s"],
                    tier=tier, commit=header["commit"], head=head, tree=tree,
                    excluded_workers=excluded)
    rec = RJ.update(cfg, job_id, gates=[g["name"] for g in header["payload"]["gates"]]) or rec
    if on_attempt is not None:
        on_attempt(job_id, True)
    _submit(worker, header, bundle, cfg)
    # the worker holds the job now: an `unknown` from here on is a job it lost
    rec = RJ.update(cfg, job_id, accepted_at=RJ.iso(_now())) or rec
    return _follow(cfg, worker, rec, project_cfg, seq, stop)


class _Job:
    """The frames of one job, applied in `n` order. `seen` is the last `n` taken in this
    process (a re-attach resumes after it); `cursor` the last one whose files were written
    (the record's; frames at or below it only rebuild their `GateResult`). `last_seen` starts
    now: an adopted job's TTL counts from the adoption, not from the dead runner's last frame."""

    def __init__(self, cfg, worker: dict, rec: dict, project_cfg: dict, seq: int):
        self.cfg, self.worker, self.rec, self.seq = cfg, worker, rec, seq
        self.job_id = rec["job_id"]
        self.order = list(rec.get("gates") or [g["name"] for g in project_cfg.get("gates") or []])
        self.names = set(self.order)
        self.gates_dir = store.gates_dir(cfg, rec["campaign"])
        self.cursor = int(rec.get("cursor") or 0)
        self.seen = 0
        self.results: list = []
        self.started, self.worker_wait = False, 0.0
        self.started_at, self.t0 = store.now_iso(), time.monotonic()
        self.last_seen = _now()
        self.deadline = RJ.epoch(rec.get("deadline_at")) or (_now() + MAX_JOB_TIMEOUT_S)

    def cancelled(self) -> bool:
        """An abandon or a park cancelled the job (`cancel_campaign`)."""
        return (RJ.read(self.cfg, self.job_id) or {}).get("state") == "cancelled"

    def past_deadline(self) -> bool:
        """The record's `deadline_at` has passed and the job is not finished on the worker. A
        job `done` there is only replayed, however late: that costs nothing, and the deadline
        bounds waiting, not results already there."""
        if _now() < self.deadline:
            return False
        if poll(self.worker, self.job_id, self.seen) == "done":
            self.deadline = math.inf
            return False
        return True

    def saw_worker(self, **fields) -> None:
        self.last_seen = _now()
        RJ.update(self.cfg, self.job_id, last_seen_at=RJ.iso(self.last_seen), **fields)

    def interrupted(self) -> G.GateRun:
        return G.GateRun(self.seq, False, tuple(self.results), self.started_at,
                         round(time.monotonic() - self.t0, 3), interrupted=True,
                         worker=self.worker["name"], job_id=self.job_id)

    def feed(self, frame: dict) -> G.GateRun | None:
        """Apply one frame: files first, then the record's cursor. Returns the `GateRun` on
        `done`. A repeated frame (`n` at or below `seen`) is skipped."""
        n = frame.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise RemoteFailure("protocol", f"`{frame['type']}` frame without a frame number")
        if n <= self.seen:
            return None
        self.seen = n
        kind, out = frame["type"], None
        if kind == "error":
            stage = str(frame.get("stage") or "error")
            raise _error_failure(stage, frame)
        if kind == "started":
            self.started = True
            self.worker_wait = _num(frame, "slot_wait_s")
        elif kind == "gate":
            self.results.append(_take_gate(frame, self.gates_dir, self.seq, self.names,
                                           write=n > self.cursor))
        elif kind == "done":
            out = self._done(frame, n)
        if n > self.cursor:
            self.cursor = n
            self.saw_worker(cursor=n, state="attached")
        return out

    def _done(self, frame: dict, n: int) -> G.GateRun:
        ok, cut = _flag(frame, "ok", required=True), _flag(frame, "interrupted", required=False)
        duration = _num(frame, "duration_s")
        if not self.started:
            self.worker_wait = _num(frame, "slot_wait_s")
        if cut:
            if self.cancelled():
                return self.interrupted()       # an abandon or a park cancelled it: no fallback
            raise RemoteFailure("interrupted", "the worker interrupted the run (signal or "
                                               "lease)")
        changed = _check_changed(frame)
        if [r.name for r in self.results] != self.order:
            raise RemoteFailure("protocol", f"`done` after results for "
                                            f"{[r.name for r in self.results]}, not {self.order}")
        ok = all(r.ok for r in self.results if r.required)
        return G.GateRun(self.seq, ok, tuple(self.results),
                         str(frame.get("started_at") or self.started_at), duration,
                         worker=self.worker["name"], job_id=self.job_id,
                         event_uid=f"{self.job_id}:{n}", changed=changed)


def _attach(job: _Job, stop):
    """One `attach JOB --from seen`: `("done", run)`, `("lost", "lost" | "unknown")`,
    `("stopped", None)`, `("deadline", None)` or `("dropped", got_frames)` (the connection
    ended without `done`)."""
    try:
        proc = subprocess.Popen(_argv(job.worker, "attach", job.job_id, "--from", job.seen),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
    except OSError:
        return "dropped", False
    lines: queue.Queue = queue.Queue()
    got = False
    try:
        for target, stream in ((_reader, proc.stdout), (_drain, proc.stderr)):
            threading.Thread(target=target,
                             args=(stream, lines if target is _reader
                                   else collections.deque(maxlen=5)), daemon=True).start()
        while True:
            if G._stop_requested(stop):
                return "stopped", None
            if job.past_deadline():
                return "deadline", None
            try:
                line = lines.get(timeout=_POLL_S)
            except queue.Empty:
                continue
            if line is None:
                return "dropped", got
            if not line.endswith(b"\n"):       # the frame cut off at EOF: SSH died mid-write
                return "dropped", got
            if not line.strip():
                continue
            frame = _frame(line, job.job_id)
            job.last_seen = _now()              # the worker answered (a replayed frame too)
            if frame["type"] == "status":
                state = frame.get("state")
                if state in ("lost", "unknown"):
                    return "lost", state
                if state == "error":
                    raise RemoteFailure("error", "the job ended in `error` before this attach")
                continue                        # `done` past our cursor cannot happen: seen < n
            run = job.feed(frame)
            got = True
            if run is not None:
                return "done", run
    finally:
        _end(proc, 0.0)                         # attach never cancels the job
        _close(proc.stdout)
        _close(proc.stderr)


def _follow(cfg, worker: dict, rec: dict, project_cfg: dict, seq: int, stop) -> Outcome:
    """Attach until `done`. A dropped connection waits with backoff, then `poll --renew`s and
    re-attaches after `seen`. Gives up (`_GiveUp`) when the worker reports the job lost, stays
    unreachable past `worker_ttl_s` since `last_seen_at`, or the record's `deadline_at`
    passes. A stop detaches: the record stays live for the next runner to adopt.
    SPEC-0334: a pull worker's job is waited for on its record (`pull_jobs.follow_gate`)."""
    if worker.get("mode") == "pull":
        from . import pull_jobs as PJ
        return PJ.follow_gate(cfg, worker, rec, project_cfg, seq, stop)
    job = _Job(cfg, worker, rec, project_cfg, seq)
    detach_ev = threading.Event()
    with _ATTACHED_LOCK:
        _ATTACHED[rec["campaign"]] = detach_ev
    stop = _Either(stop, detach_ev)
    ttl = RJ.settings(cfg)["worker_ttl_s"]
    delay = BACKOFF_START_S

    def detach() -> Outcome:
        RJ.update(cfg, job.job_id, state="detached")
        return Outcome(job.interrupted(), job.worker_wait)
    try:
        while True:
            kind, val = _attach(job, stop)
            if kind == "done":
                return Outcome(val, job.worker_wait)
            if kind == "stopped":
                return detach()
            if kind == "deadline":
                raise _GiveUp(kind, worker, rec)
            if kind == "lost":
                raise _GiveUp(_gone(cfg, rec, val), worker, rec)
            if val:                             # frames arrived: the next drop starts over
                delay = BACKOFF_START_S
            while True:
                if job.past_deadline():
                    raise _GiveUp("deadline", worker, rec)
                if _now() - job.last_seen > ttl:
                    raise _GiveUp("unreachable", worker, rec)
                if _sleep(delay, stop):
                    return detach()
                if job.cancelled():             # an abandon or a park whose own SSH failed
                    return Outcome(job.interrupted(), job.worker_wait)
                delay = min(delay * 2, BACKOFF_MAX_S)
                state = poll(worker, job.job_id, job.seen)
                if state is None:
                    continue
                job.saw_worker()
                if state in ("lost", "unknown"):
                    raise _GiveUp(_gone(cfg, rec, state), worker, rec)
                break
    except RemoteFailure as f:
        if _leave(rec["campaign"], detach_ev):  # a code-change exec claimed the step first
            return detach()
        f.worker_wait_s = job.worker_wait if job.started else 0.0
        raise
    finally:
        _leave(rec["campaign"], detach_ev)


def _cancel(worker: dict, job_id: str) -> None:
    """Best effort: `cancel JOB`."""
    _call(worker, "cancel", job_id, timeout=CANCEL_GRACE_S)


def _cancel_job(cfg, worker: dict, job_id: str) -> None:
    """`_cancel`; SPEC-0334: a pull worker's job gets `cancel_requested` instead (the server
    answers its worker's next request with `cancel: true`)."""
    if worker.get("mode") == "pull":
        from . import pull_jobs as PJ
        PJ.request_cancel(cfg, job_id)
        return
    _cancel(worker, job_id)


def _cancelled(cfg, worker: dict | None, rec: dict, seq: int) -> Outcome | None:
    """The record was `cancelled` (an abandon or a park) while the step still ran: an
    interrupted run, never a fallback. None when it was not."""
    if (RJ.read(cfg, rec["job_id"]) or {}).get("state") != "cancelled":
        return None
    return Outcome(G.GateRun(seq, False, (), store.now_iso(), 0.0, interrupted=True,
                             worker=(worker or {}).get("name"), job_id=rec["job_id"]))


def _give_up(cfg, cid, worker: dict | None, rec: dict, seq: int, reason: str, *,
             count: bool = True) -> Outcome:
    """The record goes `lost` first, then `worker_job_lost`, a best-effort cancel, this run's
    `gates/<seq>-*` files deleted and the loss recorded (SPEC-0342's window). The record stays
    `lost` and is returned as `Outcome.lost`: the caller retries the job on another worker, or
    sets it `local` and runs the gates locally. A record a cancel already ended is left alone
    (an interrupted run). `count=False` records no loss: the worker is no longer registered,
    so a loss window (and an escalation) about it would only be noise."""
    if (out := _cancelled(cfg, worker, rec, seq)) is not None:
        return out
    job_id = rec["job_id"]
    pull = rec.get("pull") or (RJ.read(cfg, job_id) or {}).get("pull")
    if pull:                                    # SPEC-0334: the API server writes this job's
        from . import pull_jobs as PJ           # gate files under the job's lock. `lost` is
        with PJ.job_lock(cfg, job_id):          # written under it, so a frames post after it
            lost = RJ.update(cfg, job_id, state="lost", lost_reason=reason) or rec   # gets 410
    else:                                       # and the files deleted below stay deleted
        lost = RJ.update(cfg, job_id, state="lost", lost_reason=reason) or rec
    store.append_event(cfg, cid, "worker_job_lost",
                       {"worker": rec["worker"], "job_id": job_id,
                        "attempt": rec.get("attempt"), "reason": reason}, by="gates")
    if worker is not None:
        _cancel_job(cfg, worker, job_id)
    if pull:                                    # the queue entry and bundle, even when the
        PJ.unqueue(cfg, job_id)                 # worker is no longer registered
    for p in store.gates_dir(cfg, cid).glob(f"{seq:03d}-*"):
        p.unlink(missing_ok=True)
    if reason in _BLAME:
        set_cooldown(cfg, rec["worker"])
    if count and reason not in UNCHARGED:
        _record_loss(cfg, cid, rec["worker"], job_id, reason)
    return Outcome(None, 0.0, RemoteFailure(reason, f"remote job {job_id}: {reason}"),
                   lost=lost)


def _record_loss(cfg, cid, name: str, job_id: str, reason: str) -> None:
    """SPEC-0342: `workers.record_loss`; a failure (to write the file, or to raise the
    escalation) is a `note` on the losing campaign, never a failed gate step."""
    try:
        err = WK.record_loss(cfg, name, job_id=job_id, reason=reason)["error"]
    except (OSError, ValueError) as e:
        err = f"worker {name}: loss not recorded: {e}"
    if err:
        store.append_event(cfg, cid, "note", {"text": err, "author": "gates"}, by="gates")


def retried(cfg, cid, lost: dict, attempt: int, to_worker: str | None, *,
            job_id: str | None = None) -> None:
    """SPEC-0342: `worker_job_retried` for a lost job's next attempt (`to_worker` None: it runs
    locally). `job_id` is the id the attempt was sent with; by default the attempt's
    `<cid>-gate-<seq>-a<attempt>`."""
    seq = int(lost.get("seq_or_run") or 0)
    store.append_event(cfg, cid, "worker_job_retried",
                       {"job_id": job_id or RJ.gate_job_id(cid, seq, attempt),
                        "from_worker": lost.get("worker"), "to_worker": to_worker,
                        "attempt": attempt}, by="gates")


def _failed(cfg, cid, worker: dict, rec: dict, seq: int, f: RemoteFailure) -> Outcome:
    """A transport or protocol failure of a recorded job: the record set `local` first (as
    `_give_up` sets `lost` first, so a crash past this point never adopts a job whose files
    are gone), then SPEC-0338's fallback and a best-effort cancel. A record a cancel already
    ended is left alone (an interrupted run)."""
    if (out := _cancelled(cfg, worker, rec, seq)) is not None:
        return out
    if f.job_error:                             # SPEC-0387: not the host's fault: retry the job
        _record_failure(cfg, cid, worker["name"], rec["job_id"], seq, f)     # no cooldown
        return _give_up(cfg, cid, worker, rec, seq, "job_error")
    RJ.update(cfg, rec["job_id"], state="local")
    _record_failure(cfg, cid, worker["name"], rec["job_id"], seq, f)
    _cancel_job(cfg, worker, rec["job_id"])
    return Outcome(None, f.worker_wait_s, f)


def adopt(rec: dict, project_cfg: dict, campaign_state: dict, cfg, seq: int, stop,
          on_wait=None) -> tuple[Outcome, float]:
    """Re-enter a live record's job after a restart: re-acquire a slot in its worker's pool
    (renewing the job lease with `poll --renew` every `job_lease_s / 3` while waiting), then
    attach from frame 0, rebuilding every `GateResult` and writing only the files past the
    record's cursor. A `done` poll ends the wait with a replay (no slot: nothing new runs); a
    `lost` one gives up. Returns the outcome and the slot wait."""
    cid = campaign_state["id"]
    worker = next((w for w in WK.load(cfg) if w["name"] == rec.get("worker")), None)
    if worker is None:                          # unregistered since: nobody to attach to
        return _give_up(cfg, cid, None, rec, seq, "unreachable", count=False), 0.0
    quit_, wait_stop = threading.Event(), threading.Event()
    verdict: dict = {}
    every = RJ.settings(cfg)["job_lease_s"] / 3

    def watch():
        next_at = time.monotonic() + every
        while not quit_.is_set():
            if G._stop_requested(stop):
                wait_stop.set()
                return
            if time.monotonic() >= next_at:
                state = poll(worker, rec["job_id"], int(rec.get("cursor") or 0))
                next_at = time.monotonic() + every
                if state is not None:           # the worker answered: not unreachable
                    rec["last_seen_at"] = RJ.iso(_now())
                    RJ.update(cfg, rec["job_id"], last_seen_at=rec["last_seen_at"])
                if state in ("done", "lost", "unknown"):
                    verdict["state"] = state
                    wait_stop.set()
                    return
            quit_.wait(min(_POLL_S, every))
    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        with gate_slots.acquire(cfg, cid, stop=wait_stop, on_wait=on_wait,
                                pools=[(worker["name"], worker["slots"])]) as hold:
            quit_.set()
            watcher.join()
            state = verdict.get("state")
            if hold.interrupted and state is None:      # a stop: stay live for the next runner
                RJ.update(cfg, rec["job_id"], state="detached")
                run = G.GateRun(seq, False, (), store.now_iso(), 0.0, interrupted=True,
                                worker=worker["name"], job_id=rec["job_id"])
                return Outcome(run), hold.waited_s
            if hold.interrupted and state in ("lost", "unknown"):
                raise _GiveUp(_gone(cfg, rec, state), worker, rec)
            return _follow(cfg, worker, rec, project_cfg, seq, stop), hold.waited_s
    except _GiveUp as g:
        return _give_up(cfg, cid, worker, rec, seq, g.stage), 0.0
    except RemoteFailure as f:
        return _failed(cfg, cid, worker, rec, seq, f), 0.0
    finally:
        quit_.set()


def cancel_campaign(cfg, cid, job_id: str | None = None) -> list[str]:
    """SPEC-0341: an abandon or a park ends the campaign's remote jobs (only `job_id`, when
    given): `cancel`, attach until `done` (`CANCEL_GRACE_S`), then the record's state
    `cancelled`. Returns the job ids."""
    out = []
    workers = {w["name"]: w for w in WK.load(cfg)}
    for rec in RJ.live(cfg, cid):
        if job_id is not None and rec["job_id"] != job_id:
            continue
        RJ.update(cfg, rec["job_id"], state="cancelled")    # first: a live step reads it
        worker = workers.get(rec.get("worker"))
        if worker is not None:
            _cancel_job(cfg, worker, rec["job_id"])
            _call(worker, "attach", rec["job_id"], "--from", int(rec.get("cursor") or 0),
                  timeout=CANCEL_GRACE_S)
        out.append(rec["job_id"])
    return out
