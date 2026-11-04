"""Close-tier reuse bookkeeping and the full-suite sentinel on the base branch (SPEC-0359).

A close gate with `reuse_from` may skip its run when the rebase is clean (`close._reuse_loop_green`).
What that static reasoning can miss is caught here: every reused close that *merges* is recorded
as a suspect in `<agentic>/projects/<project>/gate_reuse.json`, and once enough have piled up (or
one is old) the queue runner runs the project's close gates on the base HEAD in a dedicated
detached worktree. A green run settles the suspects its tested sha contains; a red one sets
`force_full_close` (every later close of the project runs its gates) and opens a red episode
(SPEC-0420, `redmain`: fix forward, revert, hold merges, escalate at most once).

State file: `{reuses_since_green, last_green_sha, last_green_at, due_since, force_full_close,
suspects: [{campaign, merge_sha}]}`. Every read-modify-write holds `gate_reuse.json.lock`. A
missing file is rebuilt from the queue's `sentinel` events and the campaigns' `close` events; an
unreadable one is left in place and declines reuse (R1) until a sentinel run rewrites it.

Runs of one project are serialized by a non-blocking flock on `<agentic>/sentinel/<project>.lock`
(a second run raises `Busy`). Each finished run is noted in `<project>.last.json` so the runner
does not resubmit a red or failed run at the same base sha before `RETRY_S`, nor a green one, nor
one whose red result was cleared by `nw queue sentinel clear`, until the base moves.

SPEC-0379: the sentinel is also due on a clock (`time_due`): the base moved since the last green
run and that run is older than `agentic.sentinel.interval_hours` (default 6; 0 / false / null
turn it off). A run takes a worker-box gate slot whenever a worker is placeable (SPEC-0374's
`workers.gate_pools`) and runs there; the local pool only when none is."""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

from ..export import _atomic_write
from . import events as E
from . import gate_slots
from . import gates as G
from . import gitops as git
from . import project as P
from . import store

STATE_FILE = "gate_reuse.json"
UNREADABLE = f"{STATE_FILE} unreadable"
RETRY_S = 3600          # a red / failed run at an unmoved base is retried after this long
DEFAULT_INTERVAL_HOURS = 6.0    # SPEC-0379: `agentic.sentinel.interval_hours` when unset
_KEYS = ("reuses_since_green", "last_green_sha", "last_green_at", "due_since",
         "force_full_close", "suspects")


class Unreadable(ValueError):
    """`gate_reuse.json` exists but is not valid JSON of the expected shape."""


class Busy(ValueError):
    """Another sentinel run of the project holds its run lock."""


def empty_state() -> dict:
    return {"reuses_since_green": 0, "last_green_sha": None, "last_green_at": None,
            "due_since": None, "force_full_close": False, "suspects": []}


def project_dir(cfg, project) -> Path:
    return store.agentic_dir(cfg) / "projects" / E._safe(str(project))


def state_path(cfg, project) -> Path:
    return project_dir(cfg, project) / STATE_FILE


@contextmanager
def locked(cfg, project):
    """The project lock every read-modify-write of `gate_reuse.json` holds (flock, like
    `events._locked`); not reentrant."""
    with E._locked(project_dir(cfg, project) / f"{STATE_FILE}.lock"):
        yield


def settings(project_cfg: dict | None) -> tuple[dict, dict]:
    """(`close`, `sentinel`) sections with their defaults."""
    pcfg = project_cfg or {}
    return ({**P.PROJECT_DEFAULTS["close"], **(pcfg.get("close") or {})},
            {**P.PROJECT_DEFAULTS["sentinel"], **(pcfg.get("sentinel") or {})})


def _valid(rec) -> bool:
    if not isinstance(rec, dict) or any(k not in rec for k in _KEYS):
        return False
    n = rec["reuses_since_green"]
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        return False
    if not isinstance(rec["force_full_close"], bool) or not isinstance(rec["suspects"], list):
        return False
    return all(isinstance(s, dict) and s.get("merge_sha") for s in rec["suspects"])


def _read(path: Path) -> dict | None:
    """The state, None when the file is missing; Unreadable when it is not valid."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        raise Unreadable(f"{UNREADABLE}: {e}")
    try:
        rec = json.loads(text)
    except ValueError as e:
        raise Unreadable(f"{UNREADABLE}: {e}")
    if not _valid(rec):
        raise Unreadable(f"{UNREADABLE}: unexpected shape")
    return rec


def _write(path: Path, st: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps({k: st.get(k) for k in _KEYS}, indent=2) + "\n")


def _ts(value) -> float | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _now(now) -> float:
    return dt.datetime.now().timestamp() if now is None else float(now)


def _iso(now) -> str:
    return store.now_iso(dt.datetime.fromtimestamp(_now(now)).astimezone())


# ---- due / overdue (section 5) ---------------------------------------------------------------

def is_due(st: dict, scfg: dict, now=None) -> bool:
    """Due: `after_reuses` reuses since the last green, or one reuse and no green sentinel within
    `max_age_s` (an unknown `last_green_at` counts as older)."""
    n = int(st.get("reuses_since_green") or 0)
    if n >= int(scfg["after_reuses"]):
        return True
    if n < 1:
        return False
    at = _ts(st.get("last_green_at"))
    return at is None or _now(now) - at > float(scfg["max_age_s"])


def is_overdue(st: dict, scfg: dict, now=None) -> bool:
    """Overdue: due for longer than `max_age_s` (by `due_since`), or the sentinel is disabled
    and `after_reuses` reuses have accumulated."""
    since = _ts(st.get("due_since"))
    if since is not None and _now(now) - since > float(scfg["max_age_s"]):
        return True
    return (not scfg["enabled"]) and int(st.get("reuses_since_green") or 0) \
        >= int(scfg["after_reuses"])


def _mark_due(st: dict, scfg: dict, now=None) -> bool:
    """Set `due_since` when the due condition first holds; True when it changed."""
    if st.get("due_since") is None and is_due(st, scfg, now):
        st["due_since"] = _iso(now)
        return True
    return False


def interval_hours(cfg) -> float:
    """SPEC-0379: `agentic.sentinel.interval_hours` of the nw config: DEFAULT_INTERVAL_HOURS when
    the key is absent (or `true`); 0.0 (off) for 0, `false`, null or a negative number. A value
    that is not a number raises ValueError."""
    sec = (cfg.get("agentic") or {}).get("sentinel")
    if not isinstance(sec, dict) or "interval_hours" not in sec:
        return DEFAULT_INTERVAL_HOURS
    raw = sec["interval_hours"]
    if raw is None or raw is False:
        return 0.0
    if raw is True:
        return DEFAULT_INTERVAL_HOURS
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"agentic.sentinel.interval_hours must be a number of hours, 0 or "
                         f"false to disable; got {raw!r}")
    return hours if hours > 0 and hours == hours else 0.0


def time_due(cfg, project, base_sha, *, root=None, project_cfg=None, now=None) -> bool:
    """SPEC-0379: the clock trigger. True when `interval_hours` is on, the project's sentinel is
    enabled, `base_sha` is not the last green sha, and the last green run is older than the
    interval (or there never was one). An unreadable state is due (a run rewrites it)."""
    hours = interval_hours(cfg)
    scfg = settings(project_cfg)[1]
    if hours <= 0 or not scfg["enabled"]:
        return False
    try:
        st = read(cfg, project, root=root, project_cfg=project_cfg, now=now)
    except Unreadable:
        return True
    if base_sha and st.get("last_green_sha") == base_sha:
        return False
    at = _ts(st.get("last_green_at"))
    return at is None or _now(now) - at >= hours * 3600.0


# ---- rebuild after state loss ------------------------------------------------------------

def _reused_merges(cfg, project) -> list[dict]:
    """`{campaign, merge_sha, ts}` of every merged close of `project` whose gate run reused a
    gate (the `close` ledger event carries `reused`), oldest first."""
    out = []
    for st in store.list_campaigns(cfg, include_terminal=True, project=project):
        try:
            closes = store.read_ledger(cfg, st["id"], types=["close"])
        except (OSError, ValueError):
            continue
        for ev in closes:
            if ev.get("reused") and ev.get("merged_sha"):
                out.append({"campaign": st["id"], "merge_sha": ev["merged_sha"],
                            "ts": ev.get("ts")})
    out.sort(key=lambda m: _ts(m["ts"]) or 0.0)
    return out


def _ancestor(root, rev, tip) -> bool | None:
    """`git merge-base --is-ancestor rev tip`: True (exit 0), False (exit 1), or None when git
    cannot answer (another exit code, a timeout, a missing repo)."""
    if not root or not rev or not tip:
        return None
    try:
        code = git.git(root, "merge-base", "--is-ancestor", str(rev), str(tip),
                       check=False).returncode
    except (git.GitError, ValueError, OSError):
        return None
    return {0: True, 1: False}.get(code)


def _known(root, sha) -> bool:
    """`sha` names a commit git knows (False when git cannot say)."""
    if not root or not sha:
        return False
    try:
        return git.git(root, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0
    except (git.GitError, ValueError, OSError):
        return False


def _contains(root, tested_sha, merge_sha) -> bool:
    """`merge_sha` is `tested_sha` or one of its ancestors (False when git cannot say, so the
    suspect is kept)."""
    if not root or not tested_sha or not merge_sha:
        return False
    return merge_sha == tested_sha or _ancestor(root, merge_sha, tested_sha) is True


def rebuild(cfg, project, *, root=None, scfg=None, now=None) -> dict:
    """The state recomputed from the queue's `sentinel` events and the campaigns' reused merges:
    suspects are the reused merges the last green sentinel's sha does not contain (all of them
    without one; by time when git cannot tell), the green fields come from the last green
    sentinel, and `force_full_close` is set after a red last sentinel, or when reused merges
    exist but no sentinel ever ran."""
    runs = [e for e in E.read_since(cfg, 0, types=["sentinel"]) if e.get("project") == project]
    merges = _reused_merges(cfg, project)
    greens = [e for e in runs if (e.get("data") or {}).get("ok") is True]
    st = empty_state()
    green = greens[-1] if greens else None
    suspects = []
    for m in merges:
        if green is not None:
            sha = (green.get("data") or {}).get("sha")
            if _known(root, sha):                               # the sha is known: by ancestry
                if _contains(root, sha, m["merge_sha"]):
                    continue
            elif (_ts(m["ts"]) or 0.0) <= (_ts(green.get("ts")) or 0.0):
                continue
        suspects.append({"campaign": m["campaign"], "merge_sha": m["merge_sha"]})
    st["suspects"] = suspects
    st["reuses_since_green"] = len(suspects)
    if green is not None:
        st["last_green_sha"] = (green.get("data") or {}).get("sha")
        st["last_green_at"] = green.get("ts")
    if runs:
        st["force_full_close"] = (runs[-1].get("data") or {}).get("ok") is False
    else:
        st["force_full_close"] = bool(merges)
    _mark_due(st, scfg or settings(None)[1], now)
    return st


def _load_locked(cfg, project, *, root=None, scfg=None, now=None,
                 rebuild_unreadable=False) -> dict:
    """Under the lock: the state, rebuilt (and written) when missing. An unreadable file raises
    Unreadable unless `rebuild_unreadable` (a sentinel run rewrites it)."""
    path = state_path(cfg, project)
    try:
        st = _read(path)
    except Unreadable:
        if not rebuild_unreadable:
            raise
        st = None
    if st is None:
        st = rebuild(cfg, project, root=root, scfg=scfg, now=now)
        _write(path, st)
    return st


def read(cfg, project, *, root=None, project_cfg=None, now=None) -> dict:
    """The project's state (rebuilt when missing). Raises Unreadable."""
    scfg = settings(project_cfg)[1]
    with locked(cfg, project):
        return dict(_load_locked(cfg, project, root=root, scfg=scfg, now=now))


def reuse_blocker(cfg, project, *, root=None, project_cfg=None, now=None) -> str | None:
    """Why the sentinel forbids reuse right now (rule R1), or None: an unreadable state file, a
    red sentinel (`force_full_close`) or an overdue sentinel."""
    scfg = settings(project_cfg)[1]
    try:
        st = read(cfg, project, root=root, project_cfg=project_cfg, now=now)
    except Unreadable:
        return UNREADABLE
    except OSError as e:
        return f"{STATE_FILE}: {e}"
    if st.get("force_full_close"):
        return "sentinel red: force_full_close is set (nw queue sentinel clear)"
    if is_overdue(st, scfg, now):
        return (f"sentinel overdue (due since {st.get('due_since') or '?'}, "
                f"{st.get('reuses_since_green')} reuses since green)")
    return None


def record_merge(cfg, project, campaign, merge_sha, *, root=None, project_cfg=None,
                 now=None) -> dict | None:
    """SPEC-0359 §4: a reused close merged. The only place the counter grows: one more reuse
    since green, `{campaign, merge_sha}` appended (once per merge sha), `due_since` set when
    this makes the sentinel due. None (nothing written) when the file is unreadable."""
    scfg = settings(project_cfg)[1]
    with locked(cfg, project):
        try:
            st = _load_locked(cfg, project, root=root, scfg=scfg, now=now)
        except Unreadable:
            return None
        if any(s.get("merge_sha") == merge_sha for s in st["suspects"]):
            return st                       # already recorded (a rebuild found it)
        st["suspects"].append({"campaign": campaign, "merge_sha": merge_sha})
        st["reuses_since_green"] = int(st["reuses_since_green"]) + 1
        _mark_due(st, scfg, now)
        _write(state_path(cfg, project), st)
        return st


def check_due(cfg, project, *, root=None, project_cfg=None, now=None) -> bool:
    """The runner's scheduling check: True when the sentinel is due and enabled; records
    `due_since` the first time the due condition is seen."""
    scfg = settings(project_cfg)[1]
    with locked(cfg, project):
        try:
            st = _load_locked(cfg, project, root=root, scfg=scfg, now=now)
        except Unreadable:
            return bool(scfg["enabled"])    # a run rewrites it
        if _mark_due(st, scfg, now):
            _write(state_path(cfg, project), st)
        due = is_due(st, scfg, now)
    # SPEC-0420: a merged fix (or a legacy red to re-check) makes the sentinel due at once
    return bool(scfg["enabled"]) and (due or bool(_red_due(cfg, project)))


def _red_due(cfg, project) -> str | None:
    from . import redmain as RM               # lazy: redmain imports this module
    try:
        return RM.due_reason(cfg, project)
    except (OSError, ValueError):
        return None


def mark_due(cfg, project, *, reason: str, now=None) -> None:
    """SPEC-0420: make the sentinel due on the next idle tick regardless of its reuse count
    (the `due_reason` lives in `red.json`; the next settled run clears it)."""
    from . import redmain as RM
    RM.set_due(cfg, project, reason, now=now)


def clear(cfg, project, *, root=None, project_cfg=None) -> dict:
    """`nw queue sentinel clear`: `force_full_close` and `due_since` off, suspects kept. A red
    last run is marked cleared, so the runner does not rerun it until the base moves."""
    scfg = settings(project_cfg)[1]
    with locked(cfg, project):
        st = _load_locked(cfg, project, root=root, scfg=scfg, rebuild_unreadable=True)
        st["force_full_close"] = False
        st["due_since"] = None
        _write(state_path(cfg, project), st)
        last = last_run(cfg, project)
        if last is not None and last.get("ok") is not True:
            _write_last(cfg, project, {**last, "cleared": True})
        return dict(st)


def projects(cfg) -> list[str]:
    """Projects with sentinel state on disk (the runner's candidates)."""
    d = store.agentic_dir(cfg) / "projects"
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if (p / STATE_FILE).exists())


# ---- the run ----------------------------------------------------------------------------

def worktree_path(cfg, project) -> Path:
    return store.agentic_dir(cfg) / "sentinel" / E._safe(str(project))


def _base(project_cfg) -> str:
    return (project_cfg.get("merge") or {}).get("base") or project_cfg.get("base") or "main"


def _side(cfg, project, suffix: str) -> Path:
    wt = worktree_path(cfg, project)
    return wt.parent / f"{wt.name}{suffix}"


# ---- serialization and the last-run note -----------------------------------------------------

def _holder(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


@contextmanager
def run_lock(cfg, project):
    """The per-project run lock (non-blocking flock on `<agentic>/sentinel/<project>.lock`,
    the holder's pid written into it). Raises Busy when another run holds it."""
    path = _side(cfg, project, ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pid = _holder(path)
            raise Busy(f"a sentinel run of {project} is already in progress"
                       + (f" (pid {pid})" if pid else "") + f"; {path} is locked")
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def running(cfg, project) -> bool:
    """A sentinel run of `project` holds the run lock right now (in any process)."""
    try:
        with run_lock(cfg, project):
            return False
    except Busy:
        return True


def last_run(cfg, project) -> dict | None:
    """`{sha, at, ok, error, cleared}` of the last finished run (`ok` None after an exception),
    None when unknown or unreadable."""
    try:
        rec = json.loads(_side(cfg, project, ".last.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def _write_last(cfg, project, rec: dict) -> None:
    path = _side(cfg, project, ".last.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(rec, indent=2) + "\n")


def _note_run(cfg, project, *, sha, ok, error=None, now=None) -> None:
    try:
        _write_last(cfg, project, {"sha": sha, "at": _iso(now), "ok": ok, "error": error,
                                   "cleared": False})
    except OSError:
        pass


def backoff(cfg, project, base_sha, *, now=None) -> str | None:
    """Why the runner must not start a run of `project` at base `base_sha` now, or None. A run
    is pointless at the sha the last one tested when it was green or its red was cleared; a
    red or failed one is retried there only after `RETRY_S`. A moved base always runs."""
    last = last_run(cfg, project)
    if not last:
        return None
    sha = last.get("sha")
    if sha and base_sha and sha != base_sha:
        return None
    at = sha[:10] if sha else "?"
    if sha and last.get("ok") is True:
        return f"already green at {at}; waiting for the base to move"
    if sha and last.get("cleared"):
        return f"red at {at} was cleared; waiting for the base to move"
    t = _ts(last.get("at"))
    if t is not None and _now(now) - t < RETRY_S:
        what = "red" if last.get("ok") is False else "failed"
        return (f"{what} at {at}; retry after "
                f"{_iso(t + RETRY_S)} or when the base moves")
    return None


def _prepare_worktree(root, wt: Path, base: str, project_cfg: dict) -> str:
    """The detached sentinel worktree at the base HEAD (created, with `worktrees.setup`, when
    missing, when the last setup did not finish, or when the setup commands changed: the
    `<project>.setup.json` marker is written only after every setup command succeeded);
    returns the tested sha."""
    sha = git.rev_parse(root, base)
    setup = list((project_cfg.get("worktrees") or {}).get("setup") or [])
    marker = wt.parent / f"{wt.name}.setup.json"
    want = json.dumps({"setup": setup})
    try:
        ready = (wt / ".git").exists() and marker.read_text(encoding="utf-8") == want
    except OSError:
        ready = False
    if not ready:
        marker.unlink(missing_ok=True)
        if wt.exists():
            git.git(root, "worktree", "remove", "--force", str(wt), check=False)
            shutil.rmtree(wt, ignore_errors=True)
        git.git(root, "worktree", "prune", check=False)
        wt.parent.mkdir(parents=True, exist_ok=True)
        git.git(root, "worktree", "add", "-q", "--detach", str(wt), sha)
        for cmd in setup:
            r = subprocess.run(cmd, shell=True, cwd=str(wt), capture_output=True, text=True)
            if r.returncode != 0:
                out = "\n".join(((r.stdout or "") + (r.stderr or "")).splitlines()[-20:])
                raise git.GitError([cmd], r.returncode, f"{cmd} exited {r.returncode}:\n{out}")
        _atomic_write(marker, want)
    git.git(wt, "checkout", "-q", "--detach", "-f", sha)
    git.git(wt, "reset", "-q", "--hard", sha)
    git.git(wt, "clean", "-fdq")            # not -x: ignored setup output (.venv) stays
    return sha


def _failing(results) -> list[str]:
    ids = []
    for r in results:
        if r.required and not r.ok:
            ids += list((r.summary or {}).get("failing") or [])
    return ids or [r.name for r in results if r.required and not r.ok]


def run(cfg, project, *, project_cfg=None, stop=None, now=None, _hooks=None) -> dict:
    """Run the project's close gates (no `paths` skip, no reuse) on the base HEAD in the
    sentinel worktree under a normal gate slot, then settle the state, all under the project's
    run lock (Busy when another run holds it). Returns
    `{project, ok, sha, suspects, failing, interrupted, results}`; an interrupted run settles
    nothing. A finished run (or one that raised) is noted for `backoff`."""
    with run_lock(cfg, project):
        tested = {}
        try:
            out = _run_locked(cfg, project, project_cfg=project_cfg, stop=stop, now=now,
                              hooks=_hooks or {}, tested=tested)
        except Exception as e:
            _note_run(cfg, project, sha=tested.get("sha"), ok=None,
                      error=f"{type(e).__name__}: {e}"[:500], now=now)
            raise
        if not out["interrupted"]:
            _note_run(cfg, project, sha=out["sha"], ok=out["ok"], now=now)
        return out


def _run_locked(cfg, project, *, project_cfg, stop, now, hooks, tested) -> dict:
    pcfg = project_cfg or P.project_config(cfg, project)
    root = pcfg.get("_root")
    if not root:
        root = str(P.project_root(cfg, project))
    wt = worktree_path(cfg, project)
    sha = _prepare_worktree(root, wt, _base(pcfg), pcfg)
    tested["sha"] = sha
    if "after_reset" in hooks:
        hooks["after_reset"](sha)
    owner = f"sentinel-{E._safe(str(project))}"
    logs = wt.parent / f"{wt.name}.logs"
    tmp = wt.parent / f"{wt.name}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    seq = G.next_gate_seq_in(logs)
    env = {**G.tmp_env(tmp), "NW_DATA_DIR": str(cfg["data_dir"]),
           "NW_CONFIG_DIR": str(cfg.get("config_dir") or ""), "NW_CAMPAIGN": "",
           "NW_SPEC_ID": "", "NW_WORKTREE": str(wt), "NW_BASE": _base(pcfg),
           "NW_SENTINEL": str(project)}
    ph = {"spec_id": None, "campaign": owner, "worktree": str(wt), "base": _base(pcfg)}
    results, interrupted = [], False
    pools, repool, state = _placement(cfg, project, pcfg, wt, seq)
    with gate_slots.acquire(cfg, owner, stop=stop, pools=pools, repool=repool) as hold:
        if hold.interrupted:
            interrupted = True
        elif hold.pool is not None:             # SPEC-0379: a worker-box slot, run there
            from . import remote_gates as RG
            try:
                run = RG.run_detached(hold.pool, {**pcfg, "gates": P.gates_for(pcfg, "close")},
                                      state, cfg, seq, logs, stop=stop)
            except RG.RemoteFailure as f:
                raise RuntimeError(f"remote sentinel on {hold.pool} failed "
                                   f"({f.stage}): {f.error}"[:500])
            results = list(run.results) if run is not None else []
            interrupted = run is None or run.interrupted
        else:
            for gate in P.gates_for(pcfg, "close"):
                if stop is not None and stop.is_set():
                    interrupted = True
                    break
                rel = f"{seq:03d}-{gate['name']}.log"
                results.append(G.run_gate_with_retries(gate, wt, env, logs / rel, ph,
                                                       log_rel=rel, stop=stop))
    shutil.rmtree(tmp, ignore_errors=True)
    interrupted = interrupted or any(r.interrupted for r in results)
    out = {"project": project, "sha": sha, "interrupted": interrupted,
           "ok": bool(results) and not interrupted
           and all(r.ok for r in results if r.required),
           "results": [{"name": r.name, "ok": r.ok, "log": str(logs / r.log)} for r in results],
           "suspects": [], "failing": []}
    if not results and not interrupted:
        out["ok"] = True                    # no close gates: nothing can be red
    if interrupted:
        return out
    if "before_settle" in hooks:
        hooks["before_settle"]()
    failing = [] if out["ok"] else _failing(results)
    red_logs = [str(logs / r.log) for r in results if r.required and not r.ok and r.log]
    settled = settle(cfg, project, sha, out["ok"], failing, root=root, project_cfg=pcfg,
                     now=now, base=_base(pcfg), logs=red_logs)
    out.update(suspects=settled, failing=failing)
    return out


def _placement(cfg, project, pcfg, wt, seq):
    """SPEC-0379: `(pools, repool, state)` for the run's slot. Worker pools while a worker is
    placeable (`workers.gate_pools`, SPEC-0374: no local overflow unless configured), and the
    campaign-shaped `state` a remote run reads; `(None, None, state)` (local only) when no worker
    is placeable or the project's close gates cannot run remotely (`remote_gates.ineligible`,
    a header the worker would refuse)."""
    from . import remote_gates as RG
    from . import workers as WK
    safe = "".join(c if c.isalnum() or c in "._-" else "-" for c in str(project)) or "project"
    state = {"id": f"sentinel-{safe}", "project": project, "spec_id": None,
             "repo": {"worktree": str(wt), "base": _base(pcfg)}}
    pools = WK.gate_pools(cfg)
    if all(name is WK.LOCAL for name, _n in pools):
        return None, None, state
    close_cfg = {**pcfg, "gates": P.gates_for(pcfg, "close")}
    if RG.ineligible(close_cfg, str(wt)) is not None \
            or RG.header_refusal(close_cfg, state, cfg, seq) is not None:
        return None, None, state
    return pools, (lambda: WK.gate_pools(cfg)), state


def _orphans(root, base, suspects) -> list:
    """Suspects whose merge sha git knows but which are not on the base branch (rewritten away):
    no sentinel run on the base can contain them. Reported on the `sentinel` event, never
    dropped. Empty when the base cannot be read; a suspect git cannot answer for is not one."""
    if not root or not base or not suspects:
        return []
    try:
        tip = git.rev_parse(root, base)
    except (git.GitError, ValueError, OSError):
        return []
    return [s for s in suspects if s.get("merge_sha") and _known(root, s["merge_sha"])
            and _ancestor(root, s["merge_sha"], tip) is False]


def _repeat_red(cfg, project, sha) -> bool:
    """The project's previous `sentinel` event was already red at `sha` (the legacy path)."""
    try:
        runs = [e for e in E.read_since(cfg, 0, types=["sentinel"])
                if e.get("project") == project]
    except (OSError, ValueError):
        return False
    data = (runs[-1].get("data") or {}) if runs else {}
    return data.get("ok") is False and data.get("sha") == sha


def settle(cfg, project, sha, ok, failing=(), *, root=None, project_cfg=None, now=None,
           base=None, logs=()) -> list:
    """Record a finished sentinel run at tested sha `sha` (section 5 *Outcome*) and announce
    it; returns the suspects it settled (only those that are `sha` or its ancestors; one git
    cannot answer for stays). With `base`, a green run names on its event (`orphans`) the kept
    suspects that are not on the base branch at all; they stay suspects.

    SPEC-0420: a red run opens or updates the project's red episode (`redmain.on_red`, which
    never escalates; `logs` are the failing gates' log paths for the fix brief) and a green one
    closes it (`redmain.on_green`). With `agentic.red_main.enabled: false` a red run escalates
    as before (`_legacy_escalate`), except at the sha the previous red event already reported."""
    from . import redmain as RM               # lazy: redmain imports this module
    scfg = settings(project_cfg)[1]
    orphans = []
    with locked(cfg, project):
        st = _load_locked(cfg, project, root=root, scfg=scfg, now=now, rebuild_unreadable=True)
        settled = [s for s in st["suspects"] if _contains(root, sha, s.get("merge_sha"))]
        if ok:
            keep = [s for s in st["suspects"] if s not in settled]
            orphans = _orphans(root, base, keep)
            st.update(suspects=keep, reuses_since_green=len(keep), last_green_sha=sha,
                      last_green_at=_iso(now), force_full_close=False, due_since=None)
            if keep:
                _mark_due(st, scfg, now)
        else:
            st["force_full_close"] = True
        _write(state_path(cfg, project), st)
    data = {"project": project, "ok": bool(ok), "sha": sha,
            "suspects": [{"campaign": s.get("campaign"), "merge_sha": s.get("merge_sha")}
                         for s in settled]}
    if not ok:
        data["failing"] = list(failing)
    if orphans:
        data["orphans"] = [{"campaign": s.get("campaign"), "merge_sha": s.get("merge_sha")}
                           for s in orphans]
    red_main = RM.enabled(cfg)
    repeat = not ok and not red_main and _repeat_red(cfg, project, sha)
    try:
        E.append(cfg, "sentinel", project=project, by="sentinel", data=data)
    except (ValueError, OSError):
        pass
    if red_main:
        try:
            if ok:
                RM.on_green(cfg, project, sha, now=now)
            else:
                RM.on_red(cfg, project, sha, list(failing), root=root, base=base, logs=logs,
                          now=now)
        except (OSError, ValueError) as e:       # best effort: the sentinel result stands
            _journal(cfg, project, f"redmain.{'on_green' if ok else 'on_red'} failed: {e}")
    elif not ok and not repeat:
        _legacy_escalate(cfg, project, sha, data["suspects"], list(failing))
    return data["suspects"]


def _journal(cfg, project, error) -> None:
    try:
        from .queue import journal
        journal(cfg, "red_main_failed", project=project, error=str(error)[:500])
    except Exception:
        pass


def _legacy_escalate(cfg, project, sha, suspects, failing) -> dict | None:
    """A campaign-less escalation naming the suspect merges (best effort)."""
    from . import escalations as ESC         # lazy: escalations imports the queue
    names = ", ".join(f"{s['campaign']} ({str(s['merge_sha'])[:10]})" for s in suspects) \
        or "none (no reused merge is in the tested sha)"
    question = (f"Sentinel red on {project} at {sha[:10]}: the full close suite fails on the "
                f"base. Suspect reused merges: {names}. Close-tier reuse is off for {project} "
                f"until a green sentinel (or nw queue sentinel clear --project {project}).")
    facts = [f"failing: {', '.join(failing[:20]) or '?'}"] + \
        [f"suspect: {s['campaign']} merged {s['merge_sha']}" for s in suspects]
    try:
        plan = ESC.plan_escalate(cfg, None, question=question, facts=facts,
                                 options=("fix forward on the base",
                                          "revert the suspect merges",
                                          "clear the flag (nw queue sentinel clear)"),
                                 frm="conductor")
        plan["project"] = project
        return ESC.apply_escalate(cfg, plan)
    except (ValueError, OSError):
        return None
