"""Remote persona jobs with bundle write-back (SPEC-0344).

Worker half (`run_job`, called by the SPEC-0340 job process for a `kind: persona` header): a
branch `nw/job/<job_id>` at the job's commit, the `RunRequest` rebuilt by
`runner.request_from_job`, the worker's own runner (`select_runner`) in a thread with a
`progress` frame every `NW_WORKER_PROGRESS_S` (30 s), then one `done` frame with the
`RunResult` fields, the run-dir files and, for a writing persona, the result bundle: `head`,
`wip` (`git add -A` + `git stash create`) and the bundle of both against the input commit.

Coordinator half (`dispatch`, `run`): `queue._do_persona` asks `dispatch` for a
`runner.RemoteRunner` when a persona-capable worker has a free persona slot and the run is
eligible; `run` submits the job (SPEC-0340), writes a SPEC-0341 record holding `base_head`,
follows the frames, and on `done` writes the result back into the campaign worktree with a
fast-forward plus `git stash apply --index` (`write_back`). A failure before the fast-forward
leaves the worktree untouched; one after it is rolled back with `git reset --hard <base_head>`
plus `git clean -fd` on the paths the `wip` commit adds. A write-back failure comes back as a
failed `RunResult` (`writeback_failed`); `_do_persona` then re-runs the step once locally."""
from __future__ import annotations

import base64
import binascii
import dataclasses
import fcntl
import gzip
import io
import json
import os
import queue as _queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import gitops
from . import remote_jobs as RJ
from . import runner as R
from . import runproc as RP
from . import store
from . import workers as WK
from . import worker as W

KIND = "persona"
PROGRESS_ENV = "NW_WORKER_PROGRESS_S"
DEFAULT_PROGRESS_S = 30.0
RUN_FILES = ("result.json", "stream.jsonl", "request.json", "stderr.log")
MAX_RUN_FILE_BYTES = 16 * 1024 * 1024
RESULT_REFS = "refs/nw/job-result/"         # the worker's result refs, inside its bundle
JOBS_REFS = "refs/nw/jobs/"                 # where the coordinator fetches them
LOCAL_ONLY_PERSONAS = ("conflict_resolver",)
TIMEOUT_MARGIN_S = 600                      # a job's deadline: the run's timeout_s + this + ttl
MIN_PERSONA_SLOTS, MAX_PERSONA_SLOTS = 1, 16
_FALLBACK_IDENT = (("user.name", "nightwright worker"), ("user.email", "nw-worker@localhost"))
_POLL_S = 0.2
BACKOFF_START_S, BACKOFF_MAX_S = 1.0, 30.0


def job_id_for(campaign: str, run_id: str, attempt: int = 1) -> str:
    return f"{campaign}-persona-{run_id}-a{attempt}"


# ---- worker side -----------------------------------------------------------------------------

def _progress_s() -> float:
    return W._float_env(PROGRESS_ENV, DEFAULT_PROGRESS_S)


def _progress(run_dir: Path) -> dict:
    """`{ts, transcript_bytes, last_line_ts}` of a running persona's transcript."""
    p = run_dir / "stream.jsonl"
    try:
        st = p.stat()
        size, last = st.st_size, RJ.iso(st.st_mtime)
    except OSError:
        size, last = 0, None
    return {"ts": store.now_iso(), "transcript_bytes": size, "last_line_ts": last}


def _run_files(run_dir: Path) -> dict[str, str]:
    """The run-dir files of the `done` frame, gzip+base64, each its last 16 MiB at most."""
    out = {}
    for name in RUN_FILES:
        p = run_dir / name
        if not p.is_file():
            continue
        size = p.stat().st_size
        with open(p, "rb") as f:
            f.seek(max(0, size - MAX_RUN_FILE_BYTES))
            out[name] = base64.b64encode(gzip.compress(f.read())).decode("ascii")
    return out


def _result_doc(rr: R.RunResult) -> dict:
    return {"exit_code": rr.exit_code, "timed_out": rr.timed_out, "interrupted": rr.interrupted,
            "duration_s": rr.duration_s, "is_error": rr.is_error, "cost_usd": rr.cost_usd,
            "usage": rr.usage, "session_id": rr.session_id, "verdict": rr.verdict,
            "verdict_ok": rr.verdict_ok, "result_text": rr.result_text}


def _tree_git(tree: Path, *args) -> str:
    return W._git("-C", tree, *args, stage="run").stdout.strip()


def _result_bundle(h: dict, tree: Path, job: Path) -> dict:
    """`{head, wip, bundle_b64}`: the tree's HEAD, a `git add -A` + `git stash create` commit
    of everything left uncommitted (new untracked, non-ignored files included; None when
    `git status --porcelain` is empty) and a bundle of both against the input commit (None
    when it would be empty)."""
    commit = h["commit"]
    head = _tree_git(tree, "rev-parse", "HEAD")
    wip = None
    if _tree_git(tree, "status", "--porcelain"):
        _tree_git(tree, "add", "-A")
        wip = _tree_git(tree, "stash", "create") or None
    refs = []
    for name, sha in (("head", head), ("wip", wip)):
        if sha and sha != commit:
            _tree_git(tree, "update-ref", f"{RESULT_REFS}{name}", sha)
            refs.append(f"{RESULT_REFS}{name}")
    b64 = None
    if refs:
        out = job / "result.bundle"
        _tree_git(tree, "bundle", "create", "-q", str(out), *refs, f"^{commit}")
        b64 = base64.b64encode(out.read_bytes()).decode("ascii")
        out.unlink(missing_ok=True)
    return {"head": head, "wip": wip, "bundle_b64": b64}


def run_job(cfg, h: dict, job: Path, frames, cancel: threading.Event) -> None:
    """The persona run of a `kind: persona` job, after the checkout: `started`, `progress`
    frames, then `done`. A run that raises is a `WorkerError` (an `error` frame)."""
    from . import gates as G
    payload = h["payload"]
    tree, run_dir, tmp = job / "tree", job / "run", job / "tmp"
    tmp.mkdir(exist_ok=True)
    _tree_git(tree, "checkout", "-q", "-b", f"nw/job/{h['job_id']}")
    for key, value in _FALLBACK_IDENT:          # commits and the wip stash need an identity
        if W._git("-C", tree, "config", key, stage="run", check=False).returncode != 0:
            _tree_git(tree, "config", key, value)
    reset = {"NW_WORKTREE": str(tree), "NW_DATA_DIR": str(cfg["data_dir"]),
             "NW_CONFIG_DIR": str(cfg.get("config_dir") or ""), **G.tmp_env(tmp),
             "NW_PERSONA": str(payload.get("persona") or "")}         # SPEC-0425
    try:
        req = R.request_from_job(payload["request"], tree, run_dir, env=reset)
        runner = R.select_runner(cfg, runner=payload.get("runner"))
    except (KeyError, TypeError, ValueError) as e:
        raise W.WorkerError("run", f"cannot run the persona: {e}")
    if isinstance(runner, R.CursorRunner) and RP.resolve_cursor_agent() is None:
        raise FileNotFoundError(2, "cursor-agent not found on PATH or in the standard install dirs",
                                "cursor-agent")
    if isinstance(runner, R.ClaudeRunner) and not isinstance(runner, R.CursorRunner) \
            and RP.resolve_claude() is None:
        # SPEC-0389: an infrastructure failure, not a persona verdict (no $ estimate charged)
        raise FileNotFoundError(2, "claude not found on PATH or in the standard install dirs",
                                "claude")
    frames.emit("started", slot_wait_s=0.0)
    started_at, t0 = store.now_iso(), time.monotonic()
    box: dict = {}

    def target():
        try:
            box["result"] = runner.run(req)
        except BaseException as e:              # reported as the job's error frame
            box["error"] = e
    th = threading.Thread(target=target, daemon=True)
    th.start()
    every = _progress_s()
    next_at = t0 + every
    while th.is_alive():
        th.join(min(0.25, every))
        if cancel.is_set() and not box.get("killed"):
            box["killed"] = True
            if isinstance(runner, R.ClaudeRunner):
                R.ClaudeRunner._kill(run_dir)
        if th.is_alive() and time.monotonic() >= next_at:
            frames.emit("progress", **_progress(run_dir))
            next_at = time.monotonic() + every
    if "error" in box:
        e = box["error"]
        raise W.WorkerError("run", f"the persona run raised: {type(e).__name__}: {e}")
    rr = box["result"]
    cut = cancel.is_set() or rr.interrupted
    bundle = None if payload["read_only"] or cut else _result_bundle(h, tree, job)
    frames.emit("done", ok=not rr.is_error, interrupted=bool(cut), started_at=started_at,
                duration_s=round(time.monotonic() - t0, 3), result=_result_doc(rr),
                files=_run_files(run_dir), bundle=bundle)


# ---- eligibility and dispatch (coordinator) -------------------------------------------------

def persona_slots(w: dict) -> int:
    v = w.get("persona_slots")
    ok = isinstance(v, int) and not isinstance(v, bool) and MIN_PERSONA_SLOTS <= v <= MAX_PERSONA_SLOTS
    return v if ok else MIN_PERSONA_SLOTS


def persona_workers(cfg, state: dict) -> list[dict]:
    """Enabled workers with `kinds` containing `persona`, the async protocol, out of cooldown
    and not in the step's `remote_excluded`, in registry order."""
    excluded = set(state.get("remote_excluded") or ())
    return [w for w in WK.load(cfg) if w["enabled"] and KIND in w["kinds"]
            and not WK.in_cooldown(w) and RJ.has_async(w) and w["name"] not in excluded]


def _git_out(cwd, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *[str(a) for a in args]],
                          capture_output=True, text=True)


def ineligible(state: dict, step_kind: str | None, req) -> str | None:
    """Why this run must stay local, else None (Design › Eligibility, worker checks aside)."""
    wt = (state.get("repo") or {}).get("worktree")
    if not wt:
        return "no worktree"
    if req.persona in LOCAL_ONLY_PERSONAS or step_kind == "resolve":
        return f"{req.persona} runs mid-rebase"
    try:
        if gitops.rebase_in_progress(wt) or gitops.sequencer_in_progress(wt):
            return "a rebase or sequencer is in progress"
    except (gitops.GitError, OSError) as e:
        return f"git failed: {e}"
    p = _git_out(wt, "ls-files", "--others", "--exclude-standard")
    if p.returncode != 0 or p.stdout.strip():
        return "the worktree has untracked files"
    if not req.read_only:
        p = _git_out(wt, "status", "--porcelain")
        if p.returncode != 0 or p.stdout.strip():
            return "a writing persona needs a clean worktree"
    try:
        R.request_to_job(req, wt)
    except R.NotRemotable as e:
        return str(e)
    return None


def _slots_dir(cfg, w: dict) -> Path:
    from . import gate_slots as GS
    return GS.slots_dir(cfg) / f"w-{w['name']}"


def _try_lock(path: Path, *, create: bool = True, block: bool = False) -> int | None:
    """An flocked fd on `path`, else None (held by another fd, or absent and not `create`)."""
    try:
        fd = os.open(path, os.O_RDWR | (os.O_CREAT if create else 0), 0o644)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
    except OSError:
        os.close(fd)
        return None
    return fd


def _held(path: Path) -> bool:
    """Someone holds the flock on `path` (an absent file is not held)."""
    if not path.exists():
        return False
    fd = _try_lock(path, create=False)
    if fd is None:
        return path.exists()
    os.close(fd)
    return False


def job_lock_path(cfg, w: dict, job_id: str) -> Path:
    """Held by the runner attached to `job_id` while it also holds a persona slot."""
    return _slots_dir(cfg, w) / f"job-{job_id}.lock"


def acquire_slot(cfg, w: dict) -> tuple[int, int] | None:
    """A free persona slot of `w` (`gate-slots/w-<W>/persona-<n>.lock`, flocked): `(fd, n)`,
    or None when every slot is held."""
    d = _slots_dir(cfg, w)
    d.mkdir(parents=True, exist_ok=True)
    for n in range(persona_slots(w)):
        fd = _try_lock(d / f"persona-{n}.lock")
        if fd is not None:
            return fd, n
    return None


def busy(cfg, w: dict, mine: int | None = None) -> int:
    """The persona jobs `w` runs or is about to run, besides the caller's slot `mine`: the
    slots other runners hold (an attached job, or one about to be submitted), plus the live
    persona job records nobody holding a slot is attached to (a job left detached by a stop or
    a crash, adopted without a free slot). A slot alone stops being a bound once a job outlives
    its runner; the live records keep counting it."""
    d = _slots_dir(cfg, w)
    held = sum(1 for n in range(persona_slots(w))
               if n != mine and _held(d / f"persona-{n}.lock"))
    loose = sum(1 for rec in RJ.live(cfg)
                if rec.get("kind") == KIND and rec.get("worker") == w["name"]
                and not _held(job_lock_path(cfg, w, rec["job_id"])))
    return held + loose


def live_record(cfg, campaign: str, run_id: str) -> dict | None:
    """The campaign's live persona job record for `run_id`, else None."""
    for rec in RJ.live(cfg, campaign):
        if rec.get("kind") == KIND and rec.get("run_id") == run_id:
            return rec
    return None


def any_live(cfg) -> bool:
    return any(rec.get("kind") == KIND for rec in RJ.live(cfg))


def dispatch(cfg, state: dict, step_kind: str | None, req, *, adopted: bool = False,
             local=None):
    """The `RemoteRunner` for this run, or None (it runs locally). An adopted run is remote
    exactly when it has a live persona job record; a fresh one when a persona worker has a
    free slot and the run is eligible. Without a persona-capable worker nothing else runs."""
    if adopted:
        rec = live_record(cfg, state["id"], Path(req.run_dir).name)
        if rec is None:
            return None
        w = next((x for x in WK.load(cfg) if x["name"] == rec.get("worker")), None)
        if w is None:
            w = WK.normalize(cfg, {"name": rec.get("worker"), "ssh": "-"})
        got = acquire_slot(cfg, w)              # best effort: the job runs on the worker anyway
        return R.RemoteRunner(cfg, state, w, local=local, record=rec,
                              slot=got[0] if got else None,
                              request=adopted_request(cfg, state, rec, req.run_dir))
    from . import persona_gate as PG
    if step_kind in PG.GATED_KINDS and req.persona not in PG.UNGATED_PERSONAS \
            and PG.backoff_until(cfg) is not None:
        # SPEC-0345: the tick's gate read the backoff before this run was admitted; one that
        # started since (another run's 429) holds it too, remote or local
        return HeldRunner()
    workers = persona_workers(cfg, state)
    if not workers or ineligible(state, step_kind, req) is not None:
        return None
    # SPEC-0345: the campaign's affine worker first; a failing worker is skipped, a full one
    # falls through to the next without clearing the affinity
    by_name = {w["name"]: w for w in workers}
    for name in PG.order(cfg, state, list(by_name)):
        w = by_name[name]
        if PG.failing(cfg, state, w):
            continue
        got = acquire_slot(cfg, w)
        if got is None:
            continue
        fd, n = got
        if busy(cfg, w, n) >= persona_slots(w):    # a detached or slot-less adopted job
            os.close(fd)
            continue
        PG.set_affinity(cfg, state, w["name"])
        return R.RemoteRunner(cfg, state, w, local=local, slot=fd)
    return None


class HeldRunner:
    """SPEC-0345: a run admitted by the tick's gate whose dispatch found the rate-limit backoff
    running. It never starts: it ends interrupted (no retry charged) and the next tick's gate
    holds the step until the backoff ends."""

    detachable = True

    def run(self, req, *, hooks=None):
        R._save_inputs(req)
        R.mark_held(req)                 # a runner adopting it after a detach charges nothing
        if hooks is not None and not hooks.enter_collect():
            return R.DETACHED
        return R._synthetic(req, "the run was not started: the rate-limit backoff began after "
                                 "the tick admitted it", cost_usd=0.0)


def adopted_request(cfg, state: dict, rec: dict, run_dir):
    """The full `RunRequest` of an adopted job, rebuilt from the `request_to_job` doc its
    record keeps, anchored at the campaign worktree, with the `ENV_RESET` keys set as
    `personas.py` sets them. The queue's adoption rebuild (`runner.rebuild_request`) keeps only
    what collecting a result needs (no `system_append`, `allowed_tools`, `add_dirs` or env), so
    a local run started for an adopted job uses this one. None for a record without the doc:
    such a run is never started locally (see `_local`)."""
    from . import gates as G
    doc = rec.get("request")
    wt = (state.get("repo") or {}).get("worktree")
    if not isinstance(doc, dict) or not wt:
        return None
    env = {"NW_WORKTREE": str(wt), "NW_DATA_DIR": str(cfg.get("data_dir") or ""),
           "NW_CONFIG_DIR": str(cfg.get("config_dir") or ""),
           "NW_PERSONA": str(doc.get("persona") or "")}               # SPEC-0425
    try:
        cid = str(state.get("id") or "")
        if cid:          # SPEC-0253: the run's private TMPDIR
            env.update(G.tmp_env(store.child_tmp(cfg, cid, f"run-{Path(run_dir).name}",
                                                 create=True)))
        return R.request_from_job(doc, wt, run_dir, env=env)
    except (KeyError, TypeError, ValueError, OSError):
        return None


# ---- the job (coordinator) ------------------------------------------------------------------

def _header(cfg, w: dict, state: dict, req, job_id: str, bundle: Path) -> dict:
    from . import gates as G
    from . import remote_gates as RG
    doc = R.request_to_job(req, state["repo"]["worktree"])
    key, sha, base_sha, have, size = RG.cut(w, {}, state, cfg, job_id, bundle)
    cid = state["id"]
    base_name = state["repo"].get("base") or "main"
    spec_id = state.get("spec_id")
    h = {"schema": W.JOB_SCHEMA, "kind": KIND, "job_id": job_id, "repo_key": key,
         "campaign": cid, "spec_id": spec_id if spec_id else "-", "commit": sha,
         "base": {"name": base_name, "sha": base_sha}, "have": have, "bundle_bytes": size,
         "env": {"NW_CAMPAIGN": str(cid), "NW_SPEC_ID": str(spec_id or ""),
                 "NW_BASE": base_name, **G.full_suite_env(state)},            # SPEC-0420
         "timeout_s": int(req.timeout_s) + TIMEOUT_MARGIN_S, "attempt": 1,
         "payload": {"run_id": Path(req.run_dir).name, "persona": req.persona, "request": doc,
                     "json_schema": req.json_schema, "read_only": req.read_only,
                     "timeout_s": req.timeout_s, "max_budget_usd": req.max_budget_usd,
                     "runner": R.resolve_runner(cfg, campaign=state,
                                                project=state.get("project"))}}
    W.validate_header(h)
    return h


def run(rr: R.RemoteRunner, req, hooks=None):
    try:
        if rr.record is not None:
            _hold_job(rr, rr.record["job_id"])
            if hooks:
                hooks.spawned()
            return _follow(rr, req, hooks, rr.record)
        return _fresh(rr, req, hooks)
    finally:
        for name in ("job_lock", "slot"):
            fd = getattr(rr, name)
            if fd is not None:
                os.close(fd)
                setattr(rr, name, None)
        if rr.job_id is not None and (RJ.read(rr.cfg, rr.job_id) or {}).get("state") not in RJ.LIVE:
            job_lock_path(rr.cfg, rr.worker, rr.job_id).unlink(missing_ok=True)


def _hold_job(rr: R.RemoteRunner, job_id: str) -> None:
    """Mark the job attached to a slot holder (`busy` then counts it by the slot alone)."""
    rr.job_id = job_id
    if rr.slot is not None and rr.job_lock is None:
        rr.job_lock = _try_lock(job_lock_path(rr.cfg, rr.worker, job_id), block=True)


def _local(rr: R.RemoteRunner, req, hooks):
    """The run on the local runner (the worker never took it, or lost it). An adopted job runs
    from the full request its record keeps (`adopted_request`), never from the queue's
    stripped rebuild; without one the run ends interrupted and the queue prepares the step
    again. SPEC-0345: while the account's rate-limit backoff runs, the run is not started
    locally either: it ends interrupted and the tick's dispatch gate holds the step."""
    from . import persona_gate as PG
    if rr.record is not None:
        if rr.request is None:
            if hooks is not None and not hooks.enter_collect():
                return R.DETACHED
            return R._synthetic(req, "the remote persona job was lost and its record keeps no "
                                     "request to run it locally")
        req = rr.request
    if req.persona not in PG.UNGATED_PERSONAS and PG.backoff_until(rr.cfg) is not None:
        R.mark_held(req)                 # a runner adopting it after a detach charges nothing
        if hooks is not None and not hooks.enter_collect():
            return R.DETACHED
        return R._synthetic(req, "the run went local during the rate-limit backoff; the step "
                                 "runs again when the backoff ends", cost_usd=0.0)
    local = rr.local if rr.local is not None else R.select_runner(rr.cfg)
    if hooks is not None and getattr(local, "detachable", False):
        # an adopted remote run dir has no `proc.json`, so `_adopt` gave the hooks an adoption
        # mode; the local run spawns its own process instead of waiting for a wrapper
        if not hooks.restart(req.run_dir):
            return R.DETACHED
        return local.run(req, hooks=hooks)
    return local.run(req)


def _fresh(rr: R.RemoteRunner, req, hooks):
    from . import remote_gates as RG
    cfg, state, w = rr.cfg, rr.state, rr.worker
    cid, wt = state["id"], state["repo"]["worktree"]
    run_id = Path(req.run_dir).name
    job_id = job_id_for(cid, run_id)
    R._save_inputs(req)
    with tempfile.TemporaryDirectory(prefix="nw-persona-") as tmp:
        bundle = Path(tmp) / "job.bundle"
        try:
            WK.ensure_record_ssh_dir(cfg, w)
            base_head = RG._git(wt, "rev-parse", "HEAD")
            header = _header(cfg, w, state, req, job_id, bundle)
        except RG.RemoteFailure as f:
            _failed(cfg, cid, w, job_id, f)
            return _local(rr, req, hooks)
        except (W.WorkerError, R.NotRemotable, OSError, ValueError):
            return _local(rr, req, hooks)
        now = time.time()
        RJ.create(cfg, job_id=job_id, campaign=cid, worker=w["name"], attempt=1, seq=0,
                  submitted_at=now, kind=KIND, commit=header["commit"], head=base_head,
                  deadline_at=now + header["timeout_s"] + RJ.settings(cfg)["worker_ttl_s"])
        # `request`: an adopted runner rebuilds the full request from it (`adopted_request`)
        rec = RJ.update(cfg, job_id, run_id=run_id, persona=req.persona, base_head=base_head,
                        read_only=req.read_only, started_at=store.now_iso(),
                        step={"state": state.get("state")},
                        request=header["payload"]["request"])
        _hold_job(rr, job_id)
        try:
            RG._submit(w, header, bundle, cfg)
        except RG.RemoteFailure as f:
            _failed(cfg, cid, w, job_id, f)
            RG._cancel_job(cfg, w, job_id)
            RJ.update(cfg, job_id, state="local")
            return _local(rr, req, hooks)
    rec = RJ.update(cfg, job_id, accepted_at=RJ.iso(time.time())) or rec
    if hooks:
        hooks.spawned()
    return _follow(rr, req, hooks, rec)


def _failed(cfg, cid, w: dict, job_id: str, f) -> None:
    """A failure before the worker held the job: `worker_failed`, and the worker's cooldown
    when the failure is its own, as a gate run's fallback records it."""
    from . import remote_gates as RG
    store.append_event(cfg, cid, "worker_failed",
                       {"worker": w["name"], "stage": f.stage, "error": f.error[-2000:],
                        "job_id": job_id, "cooldown": f.cooldown}, by="personas")
    if f.cooldown:
        RG.set_cooldown(cfg, w["name"])


def _reader(stream, lines) -> None:
    try:
        for line in iter(stream.readline, b""):
            lines.put(line)
    except (OSError, ValueError):
        pass
    lines.put(None)


def _attach(rr: R.RemoteRunner, req, hooks, job: dict):
    """One `attach JOB --from seen`: ("done", frame), ("lost", why), ("detached", None) or
    ("dropped", None)."""
    from . import remote_gates as RG
    try:
        proc = subprocess.Popen(RG._argv(rr.worker, "attach", job["id"], "--from", job["seen"]),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return "dropped", None
    lines: _queue.Queue = _queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, lines), daemon=True).start()
    try:
        while True:
            if hooks is not None and hooks.detached():
                return "detached", None
            if _cancelled(rr.cfg, job["id"]):
                return "lost", "cancelled"
            try:
                line = lines.get(timeout=_POLL_S)
            except _queue.Empty:
                if time.time() > job["deadline"]:    # frames already read come first
                    return "lost", "deadline"
                continue
            if line is None or not line.endswith(b"\n"):
                return "dropped", None
            try:
                frame = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(frame, dict):
                continue
            job["last_seen"] = time.time()
            kind = frame.get("type")
            if kind == "status":
                if frame.get("state") in ("lost", "unknown", "error"):
                    return "lost", str(frame.get("state"))
                continue
            if kind == "error":
                return "lost", f"error: {frame.get('stage')}: {frame.get('error')}"
            n = frame.get("n")
            if isinstance(n, int) and not isinstance(n, bool):
                if n <= job["seen"]:
                    continue
                job["seen"] = n
            if kind == "done":
                return "done", frame
            fields = {"cursor": job["seen"], "state": "attached",
                      "last_seen_at": RJ.iso(job["last_seen"])}
            if kind == "progress":
                prog = {k: frame.get(k) for k in ("ts", "transcript_bytes", "last_line_ts")}
                fields["progress"] = prog
                try:
                    RP.write_json_atomic(Path(req.run_dir) / "progress.json", prog)
                except OSError:
                    pass
            RJ.update(rr.cfg, job["id"], **fields)
    finally:
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()
        proc.stdout.close()


def _cancelled(cfg, job_id: str) -> bool:
    return (RJ.read(cfg, job_id) or {}).get("state") == "cancelled"


def _sleep(seconds: float, hooks) -> bool:
    """Sleep; True when the pool detached meanwhile."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if hooks is not None and hooks.detached():
            return True
        time.sleep(min(_POLL_S, max(0.0, end - time.monotonic())))
    return False


def _follow(rr: R.RemoteRunner, req, hooks, rec: dict):
    """Attach until `done`; a dropped connection polls with backoff and re-attaches. A detach
    leaves the record `detached` (SPEC-0341 adoption); a lost job runs locally. SPEC-0334: a
    pull worker's job is waited for on its record (`pull_jobs.follow_persona`)."""
    from . import remote_gates as RG
    if rr.worker.get("mode") == "pull":
        from . import pull_jobs as PJ
        return PJ.follow_persona(rr, req, hooks, rec)
    cfg = rr.cfg
    job = {"id": rec["job_id"], "seen": int(rec.get("cursor") or 0), "last_seen": time.time(),
           "deadline": RJ.epoch(rec.get("deadline_at")) or time.time() + RG.MAX_JOB_TIMEOUT_S}
    ttl = RJ.settings(cfg)["worker_ttl_s"]
    delay = BACKOFF_START_S
    while True:
        kind, val = _attach(rr, req, hooks, job)
        if kind == "done":
            return _finish(rr, req, hooks, rec, val)
        if kind == "detached":
            RJ.update(cfg, job["id"], state="detached")
            return R.DETACHED
        if kind == "lost":
            return _lost(rr, req, hooks, rec, val)
        while True:
            if time.time() > job["deadline"]:
                return _lost(rr, req, hooks, rec, "deadline")
            if time.time() - job["last_seen"] > ttl:
                return _lost(rr, req, hooks, rec, "unreachable")
            if _sleep(delay, hooks):
                RJ.update(cfg, job["id"], state="detached")
                return R.DETACHED
            delay = min(delay * 2, BACKOFF_MAX_S)
            state = RG.poll(rr.worker, job["id"], job["seen"])
            if state is None:
                continue
            job["last_seen"] = time.time()
            if state in ("lost", "unknown"):
                return _lost(rr, req, hooks, rec, state)
            break


def _lost(rr: R.RemoteRunner, req, hooks, rec: dict, why: str):
    """The worker lost the job (or it was cancelled): a cancel ends the run as interrupted;
    anything else is recorded as a loss on the worker and the run goes local."""
    from . import remote_gates as RG
    cfg, job_id = rr.cfg, rec["job_id"]
    if (RJ.read(cfg, job_id) or rec).get("writeback") == "applied":
        return _applied(rr, req, hooks, job_id)
    if why == "cancelled":
        if hooks is not None and not hooks.enter_collect():
            return R.DETACHED
        return R._synthetic(req, "the remote persona job was cancelled")
    RG._cancel_job(cfg, rr.worker, job_id)
    lost = RJ.update(cfg, job_id, state="lost", lost_reason=why) or rec
    if why in RG.UNCHARGED:                     # SPEC-0334: a rotation is not the worker's fault
        # SPEC-0345: no loss on the worker and no affinity change, but the ledger names the run
        # (`charged: false`) so the cap restarts its clock for the local re-run
        _lost_event(rr, req, job_id, lost, why, charged=False)
        return _local(rr, req, hooks)
    # SPEC-0345: the campaign's ledger says so (as a gate job's loss does), which also clears
    # the campaign's affinity on this worker
    from . import persona_gate as PG
    _lost_event(rr, req, job_id, lost, why)
    PG.clear_affinity(cfg, rr.state, rr.worker["name"])
    try:
        WK.record_loss(cfg, rr.worker["name"], job_id=job_id, reason=why)
    except (OSError, ValueError):
        pass
    return _local(rr, req, hooks)


def _lost_event(rr: R.RemoteRunner, req, job_id: str, lost: dict, why: str, *,
                charged: bool = True) -> None:
    """The persona job's `worker_job_lost` on the campaign ledger, naming its `run_id`: the
    cap restarts the run's clock at the loss (the local re-run gets a full timeout). An
    uncharged loss says `charged: false`: it makes the worker neither failing nor clears the
    affinity (`persona_gate.lost_since`)."""
    data = {"worker": rr.worker["name"], "job_id": job_id, "run_id": Path(req.run_dir).name,
            "attempt": lost.get("attempt"), "reason": why}
    if not charged:
        data["charged"] = False
    try:
        store.append_event(rr.cfg, rr.state["id"], "worker_job_lost", data, by="personas")
    except (OSError, ValueError):
        pass


def _applied(rr: R.RemoteRunner, req, hooks, job_id: str):
    """A record already marked `writeback: applied` (a runner stopped between the write-back
    and the record's deletion) whose job the worker no longer has: the run finished and its
    files are in the run dir, so it is collected from them, never run again."""
    if hooks is not None and not hooks.enter_collect():
        return R.DETACHED
    cur = RJ.read(rr.cfg, job_id) or {}
    run_dir = Path(req.run_dir)
    parsed = R.parse_result(RP.run_output(run_dir), req.json_schema, exit_code=0)
    result = R.write_run_record(req, R.RunResult(
        exit_code=0, timed_out=False, interrupted=False, duration_s=0.0,
        raw_path=run_dir / "result.json", worker=rr.worker["name"], **parsed),
        cur.get("started_at") or store.now_iso())
    RJ.update(rr.cfg, job_id, state="done")
    RJ.path(rr.cfg, job_id).unlink(missing_ok=True)
    return result


def _decode(data: str) -> bytes:
    """A run-dir file of the `done` frame; ValueError past `MAX_RUN_FILE_BYTES` decompressed."""
    with gzip.GzipFile(fileobj=io.BytesIO(base64.b64decode(data))) as f:
        out = f.read(MAX_RUN_FILE_BYTES + 1)
    if len(out) > MAX_RUN_FILE_BYTES:
        raise ValueError(f"run file over {MAX_RUN_FILE_BYTES} bytes")
    return out


def _num(v, cast, default):
    """A `done` frame's numeric field; a malformed one is `default`, not an exception."""
    try:
        return cast(v)
    except (TypeError, ValueError, OverflowError):
        return default


def _finish(rr: R.RemoteRunner, req, hooks, rec: dict, frame: dict):
    """The `done` frame: run-dir files, write-back (once), `run.json`; the record goes."""
    cfg, job_id = rr.cfg, rec["job_id"]
    if hooks is not None and not hooks.enter_collect():
        RJ.update(cfg, job_id, state="detached")
        return R.DETACHED
    run_dir = Path(req.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    for name, data in (frame.get("files") or {}).items():
        if name in RUN_FILES and isinstance(data, str):
            try:
                (run_dir / name).write_bytes(_decode(data))
            except (OSError, ValueError, binascii.Error, EOFError, gzip.BadGzipFile):
                pass
    res = frame.get("result") if isinstance(frame.get("result"), dict) else {}
    cut = bool(frame.get("interrupted") or res.get("interrupted"))
    result = R.RunResult(
        exit_code=_num(res.get("exit_code", -1), int, -1), timed_out=bool(res.get("timed_out")),
        interrupted=cut,
        duration_s=_num(res.get("duration_s") or frame.get("duration_s") or 0, float, 0.0),
        is_error=bool(res.get("is_error", True)) or cut,
        cost_usd=_num(res["cost_usd"], float, None) if res.get("cost_usd") is not None else None,
        usage=res.get("usage") if isinstance(res.get("usage"), dict) else {},
        session_id=res.get("session_id"), verdict=res.get("verdict"),
        verdict_ok=bool(res.get("verdict_ok")), result_text=str(res.get("result_text") or ""),
        raw_path=run_dir / "result.json", worker=rr.worker["name"])
    cur = RJ.read(cfg, job_id) or rec
    if not cur.get("read_only") and not cut and cur.get("writeback") != "applied":
        ok, reason, saved = write_back(cfg, rr.state["repo"]["worktree"], job_id,
                                       cur.get("base_head") or cur.get("head"),
                                       frame.get("bundle") or {})
        if ok:
            RJ.update(cfg, job_id, writeback="applied", writeback_head=gitops.rev_parse(
                rr.state["repo"]["worktree"]))
        else:
            RJ.update(cfg, job_id, writeback="failed", writeback_reason=reason,
                      saved_refs=saved)
            result = dataclasses.replace(result, is_error=True, verdict=None, verdict_ok=False,
                                         result_text=f"write-back: {reason}",
                                         writeback_failed=True, saved_refs=tuple(saved))
    R.write_run_record(req, result, cur.get("started_at") or store.now_iso())
    RJ.update(cfg, job_id, state="done")
    RJ.path(cfg, job_id).unlink(missing_ok=True)
    return result


# ---- write-back -----------------------------------------------------------------------------

def _before_writeback(worktree, bundle: dict) -> None:
    """A seam for tests (a worktree changed after submission, a corrupt bundle)."""


def _worktree_clean(worktree) -> bool:
    p = _git_out(worktree, "status", "--porcelain")
    return p.returncode == 0 and not p.stdout.strip()


def _refs_of(worktree, job_id: str) -> list[str]:
    p = _git_out(worktree, "for-each-ref", "--format=%(refname)", f"{JOBS_REFS}{job_id}/")
    return [ln for ln in p.stdout.splitlines() if ln.strip()]


def prune_refs(repo, campaign: str) -> list[str]:
    """Delete the campaign's saved write-back refs (`refs/nw/jobs/<campaign>-persona-*/`):
    Write-back step 5, when the campaign closes. Returns the refs deleted."""
    if not repo or not Path(repo).is_dir():
        return []
    prefix = f"{JOBS_REFS}{campaign}-persona-"
    p = _git_out(repo, "for-each-ref", "--format=%(refname)", JOBS_REFS)
    out = []
    for ref in (ln.strip() for ln in p.stdout.splitlines()):
        if ref.startswith(prefix) and _git_out(repo, "update-ref", "-d", ref).returncode == 0:
            out.append(ref)
    return out


def write_back(cfg, worktree, job_id: str, base_head: str, bundle: dict) -> tuple:
    """Apply a result bundle to the campaign worktree: `(ok, reason, saved_refs)`. Fetch into
    `refs/nw/jobs/<job_id>/{head,wip}`, check HEAD is `base_head` and the tree clean, `merge
    --ff-only <head>`, `stash apply --index <wip>` (then unstaged, as a local run leaves it),
    drop the refs. A failure keeps the refs (`saved_refs`) and leaves the worktree at
    `base_head` with the status it had before the fast-forward."""
    head, wip, b64 = bundle.get("head"), bundle.get("wip"), bundle.get("bundle_b64")
    saved: list[str] = []

    def fail(reason):
        return False, reason, _refs_of(worktree, job_id) or saved

    try:
        _before_writeback(worktree, bundle)
        head, wip, b64 = bundle.get("head"), bundle.get("wip"), bundle.get("bundle_b64")
        if not isinstance(head, str) or not W.SHA_RE.match(head) \
                or (wip is not None and (not isinstance(wip, str) or not W.SHA_RE.match(wip))):
            return fail("corrupt bundle: bad head/wip")
        if b64:
            with tempfile.TemporaryDirectory(prefix="nw-wb-") as tmp:
                path = Path(tmp) / "result.bundle"
                try:
                    path.write_bytes(base64.b64decode(b64, validate=True))
                except (binascii.Error, ValueError) as e:
                    return fail(f"corrupt bundle: {e}")
                p = _git_out(worktree, "fetch", "-q", path,
                             f"{RESULT_REFS}*:{JOBS_REFS}{job_id}/*")
                if p.returncode != 0:
                    return fail(f"corrupt bundle: {(p.stderr or p.stdout).strip()[-300:]}")
        for name, sha in (("head", head), ("wip", wip)):
            if sha is None:
                continue
            if _git_out(worktree, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
                return fail(f"corrupt bundle: {name} {sha[:12]} is missing")
            _git_out(worktree, "update-ref", f"{JOBS_REFS}{job_id}/{name}", sha)
        saved = _refs_of(worktree, job_id)
        if gitops.rev_parse(worktree) != base_head:
            return fail("head moved")
        if not _worktree_clean(worktree):
            return fail("worktree dirty")
        # a `head` that does not descend from `base_head` (the persona rewound history on the
        # worker) would make the fast-forward a silent no-op and drop the rewind
        if _git_out(worktree, "merge-base", "--is-ancestor", base_head, head).returncode != 0:
            return fail("head not a descendant of base_head")
        pre = _git_out(worktree, "stash", "create").stdout.strip()    # empty: the tree is clean
        p = _git_out(worktree, "merge", "--ff-only", "-q", head)
        if p.returncode != 0:
            _rollback(worktree, base_head, head, None, pre)
            return fail(f"fast-forward failed: {(p.stderr or p.stdout).strip()[-300:]}")
        if gitops.rev_parse(worktree) != head:
            _rollback(worktree, base_head, head, None, pre)
            return fail("fast-forward did not reach head")
        if wip:
            p = _git_out(worktree, "stash", "apply", "--index", "-q", wip)
            if p.returncode != 0:
                _rollback(worktree, base_head, head, wip, pre)
                return fail(f"stash conflict: {(p.stderr or p.stdout).strip()[-300:]}")
            _git_out(worktree, "reset", "-q")
    except (gitops.GitError, OSError) as e:
        return fail(f"git failed: {e}")
    for ref in _refs_of(worktree, job_id):
        _git_out(worktree, "update-ref", "-d", ref)
    return True, None, []


def _rollback(worktree, base_head: str, head: str, wip: str | None, pre: str) -> None:
    """Back to `base_head`: `reset --hard`, `clean -fd` limited to the paths `wip` adds, then
    the tree's own prior changes (`pre`, a `stash create` taken before the fast-forward)."""
    added = []
    if wip:
        # NUL-separated and as bytes: no C-quoting of non-ASCII names, no decoding errors
        out = subprocess.run(["git", "-C", str(worktree), "diff", "--name-only", "-z",
                              "--diff-filter=A", head, wip], capture_output=True).stdout
        added = [os.fsdecode(n) for n in out.split(b"\0") if n]
    _git_out(worktree, "reset", "-q", "--hard", base_head)
    for i in range(0, len(added), 500):        # chunked: a long path list fits in one argv
        # literal pathspecs: a name starting `:` or holding `*` is a path, not pathspec magic
        _git_out(worktree, "--literal-pathspecs", "clean", "-q", "-fd", "--", *added[i:i + 500])
    if pre:
        if _git_out(worktree, "stash", "apply", "--index", "-q", pre).returncode != 0:
            _git_out(worktree, "stash", "apply", "-q", pre)


# ---- `nw workers check --auth` --------------------------------------------------------------

def claude_check(cfg, w: dict, auth: bool) -> list[str]:
    """SPEC-0344: `nw worker claude-check [--auth]` on the worker; the errors (empty: ok)."""
    from . import remote_gates as RG
    args = ["claude-check"] + (["--auth"] if auth else [])
    timeout = W.CLAUDE_AUTH_TIMEOUT_S + 30 if auth else WK.CHECK_TIMEOUT_S
    got = RG._call(w, *args, timeout=timeout)
    if got is None:
        return ["`nw worker claude-check` could not run over SSH"]
    rc, lines, err = got
    rec = next((r for r in lines if r.get("schema") == W.CLAUDE_CHECK_SCHEMA), None)
    if rec is None:
        tail = (err or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        return [f"`nw worker claude-check` failed (exit {rc}): {tail[0]}".rstrip(": ")]
    if not rec.get("ok"):
        return [str(rec.get("error") or "claude check failed")]
    return []


def cursor_check(cfg, w: dict) -> list[str]:
    """SPEC-0436: `nw worker cursor-check` on the worker; the errors (empty: ok)."""
    from . import remote_gates as RG
    got = RG._call(w, "cursor-check", timeout=WK.CHECK_TIMEOUT_S)
    if got is None:
        return ["`nw worker cursor-check` could not run over SSH"]
    rc, lines, err = got
    rec = next((r for r in lines if r.get("schema") == W.CURSOR_CHECK_SCHEMA), None)
    if rec is None:
        tail = (err or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        return [f"`nw worker cursor-check` failed (exit {rc}): {tail[0]}".rstrip(": ")]
    if not rec.get("ok"):
        return [str(rec.get("error") or "cursor check failed")]
    return []

