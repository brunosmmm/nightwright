"""Close-tier reuse bookkeeping and the full-suite sentinel on the base branch (SPEC-0359).

A close gate with `reuse_from` may skip its run when the rebase is clean (`close._reuse_loop_green`).
What that static reasoning can miss is caught here: every reused close that *merges* is recorded
as a suspect in `<agentic>/projects/<project>/gate_reuse.json`, and once enough have piled up (or
one is old) the queue runner runs the project's close gates on the base HEAD in a dedicated
detached worktree. A green run settles the suspects its tested sha contains; a red one sets
`force_full_close` (every later close of the project runs its gates) and escalates.

State file: `{reuses_since_green, last_green_sha, last_green_at, due_since, force_full_close,
suspects: [{campaign, merge_sha}]}`. Every read-modify-write holds `gate_reuse.json.lock`. A
missing file is rebuilt from the queue's `sentinel` events and the campaigns' `close` events; an
unreadable one is left in place and declines reuse (R1) until a sentinel run rewrites it."""
from __future__ import annotations

import datetime as dt
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
_KEYS = ("reuses_since_green", "last_green_sha", "last_green_at", "due_since",
         "force_full_close", "suspects")


class Unreadable(ValueError):
    """`gate_reuse.json` exists but is not valid JSON of the expected shape."""


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


def _contains(root, tested_sha, merge_sha) -> bool:
    """`merge_sha` is `tested_sha` or one of its ancestors (False when git cannot say)."""
    if not root or not tested_sha or not merge_sha:
        return False
    return merge_sha == tested_sha or git.is_ancestor(root, merge_sha, tested_sha)


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
            if root and git.is_ancestor(root, sha, sha):        # the sha is known: by ancestry
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
        return bool(scfg["enabled"]) and is_due(st, scfg, now)


def clear(cfg, project, *, root=None, project_cfg=None) -> dict:
    """`nw queue sentinel clear`: `force_full_close` and `due_since` off, suspects kept."""
    scfg = settings(project_cfg)[1]
    with locked(cfg, project):
        st = _load_locked(cfg, project, root=root, scfg=scfg, rebuild_unreadable=True)
        st["force_full_close"] = False
        st["due_since"] = None
        _write(state_path(cfg, project), st)
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


def _prepare_worktree(root, wt: Path, base: str, project_cfg: dict) -> str:
    """The detached sentinel worktree at the base HEAD (created, with `worktrees.setup`, when
    missing); returns the tested sha."""
    sha = git.rev_parse(root, base)
    if not (wt / ".git").exists():
        if wt.exists():
            shutil.rmtree(wt)
        git.git(root, "worktree", "prune", check=False)
        wt.parent.mkdir(parents=True, exist_ok=True)
        git.git(root, "worktree", "add", "-q", "--detach", str(wt), sha)
        for cmd in (project_cfg.get("worktrees") or {}).get("setup") or []:
            r = subprocess.run(cmd, shell=True, cwd=str(wt), capture_output=True, text=True)
            if r.returncode != 0:
                out = "\n".join(((r.stdout or "") + (r.stderr or "")).splitlines()[-20:])
                raise git.GitError([cmd], r.returncode, f"{cmd} exited {r.returncode}:\n{out}")
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
    sentinel worktree under a normal gate slot, then settle the state. Returns
    `{project, ok, sha, suspects, failing, interrupted, results}`; an interrupted run settles
    nothing."""
    hooks = _hooks or {}
    pcfg = project_cfg or P.project_config(cfg, project)
    root = pcfg.get("_root")
    if not root:
        root = str(P.project_root(cfg, project))
    wt = worktree_path(cfg, project)
    sha = _prepare_worktree(root, wt, _base(pcfg), pcfg)
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
    with gate_slots.acquire(cfg, owner, stop=stop) as hold:
        if hold.interrupted:
            interrupted = True
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
    settled = settle(cfg, project, sha, out["ok"], failing, root=root, project_cfg=pcfg,
                     now=now)
    out.update(suspects=settled, failing=failing)
    return out


def settle(cfg, project, sha, ok, failing=(), *, root=None, project_cfg=None, now=None) -> list:
    """Record a finished sentinel run at tested sha `sha` (section 5 *Outcome*) and announce
    it; returns the suspects it settled (those that are `sha` or its ancestors)."""
    scfg = settings(project_cfg)[1]
    with locked(cfg, project):
        st = _load_locked(cfg, project, root=root, scfg=scfg, now=now, rebuild_unreadable=True)
        settled = [s for s in st["suspects"] if _contains(root, sha, s.get("merge_sha"))]
        if ok:
            keep = [s for s in st["suspects"] if s not in settled]
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
    try:
        E.append(cfg, "sentinel", project=project, by="sentinel", data=data)
    except (ValueError, OSError):
        pass
    if not ok:
        _escalate(cfg, project, sha, data["suspects"], list(failing))
    return data["suspects"]


def _escalate(cfg, project, sha, suspects, failing) -> dict | None:
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
