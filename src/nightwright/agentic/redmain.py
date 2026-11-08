"""Red-main episodes: fix forward, revert, hold, escalate once (SPEC-0420).

A red full-suite sentinel (`sentinel.settle`) opens a **red episode** for the project in
`<agentic>/projects/<project>/red.json`; the queue owns it until a green sentinel closes it. The
runner's tick step (`queue._red_main_step`) asks `step()` what to do next: open a `fixforward`
campaign (priority 1, full suite, a generated brief), localize the culprit merge by a binary search
over the episode's merges running only the failing test ids, open a `revert` campaign of that
merge's commit range, or file **one** escalation. While an episode is open, `close.prepare_close`
holds every non-fix close of the project (`holding`). A green sentinel (`on_green`) closes the
episode, withdraws its escalation and the legacy "Sentinel red on <project>" records, and the
tick abandons the fix campaigns it made moot.

Every read-modify-write of `red.json` holds `red.json.lock` (`events._locked`); the file is
written atomically. `agentic.red_main.enabled: false` turns all of it off: the sentinel escalates
per red sha as before SPEC-0420, nothing is held and no fix campaign opens."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import subprocess
from contextlib import contextmanager
from pathlib import Path

from ..export import _atomic_write
from . import events as E
from . import gitops as git
from . import store

SCHEMA = "nw.red.v1"
STATUS_SCHEMA = "nw.queue.red.v1"
FILE = "red.json"
RED_KINDS = ("fixforward", "revert")
FAILING_MAX = 200
BRIEF_IDS_MAX = 50
MERGES_UNKNOWN_GREEN = 20       # merges considered when the last green sha is unknown
MOOT_STATES = ("QUEUED", "WAITING", "OPENING", "IMPLEMENTING", "GATING", "TRIAGING", "REVIEWING",
               "DIVERGENCE_TRIAGE", "PARKED")
DEFAULTS = {"enabled": True, "fixforward_attempts": 2, "revert": True, "hold_merges": True,
            "escalate_after_s": 14400, "brief_log_lines": 120,
            "probe_cmd": "uv run pytest -q -p no:cacheprovider", "probe_timeout_s": 900}
LEGACY_PREFIX = "Sentinel red on "


# ---- settings / storage ---------------------------------------------------------------------

def settings(cfg) -> dict:
    """`agentic.red_main` over DEFAULTS; a value of the wrong type or out of range falls back to
    its default (pure)."""
    raw = ((cfg or {}).get("agentic") or {}).get("red_main")
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    for key in ("enabled", "revert", "hold_merges"):
        if isinstance(raw.get(key), bool):
            out[key] = raw[key]
    for key in ("fixforward_attempts", "brief_log_lines"):
        v = raw.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            out[key] = v
    for key in ("escalate_after_s", "probe_timeout_s"):
        v = raw.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0:
            out[key] = float(v)
    if isinstance(raw.get("probe_cmd"), str) and raw["probe_cmd"].strip():
        out["probe_cmd"] = raw["probe_cmd"].strip()
    return out


def enabled(cfg) -> bool:
    return settings(cfg)["enabled"]


def _dir(cfg, project) -> Path:
    return store.agentic_dir(cfg) / "projects" / E._safe(str(project))


def path(cfg, project) -> Path:
    return _dir(cfg, project) / FILE


@contextmanager
def locked(cfg, project):
    with E._locked(_dir(cfg, project) / f"{FILE}.lock"):
        yield


def _blank() -> dict:
    return {"schema": SCHEMA, "episode": 0, "open": False, "opened_at": None, "closed_at": None,
            "first_red_sha": None, "last_red_sha": None, "last_green_sha": None, "failing": [],
            "fingerprint": None, "runs": 0, "merges": [], "culprit": None, "localize": None,
            "fixes": [], "resume_credit": 0, "escalation": None, "logs": [], "history": []}


def _load(cfg, project) -> dict | None:
    try:
        rec = json.loads(path(cfg, project).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None             # an unreadable file is treated as "no episode"; a write repairs it
    return rec if isinstance(rec, dict) else None


def _save(cfg, project, rec: dict) -> None:
    p = path(cfg, project)
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(p, json.dumps(rec, indent=2, ensure_ascii=False) + "\n")


def read(cfg, project) -> dict | None:
    """The project's episode state, None when no episode was ever opened."""
    rec = _load(cfg, project)
    if rec is None or not int(rec.get("episode") or 0):
        return None
    return {**_blank(), **rec}


def is_red(cfg, project) -> bool:
    rec = read(cfg, project)
    return bool(rec and rec.get("open"))


def _now(now) -> float:
    return dt.datetime.now().timestamp() if now is None else float(now)


def _iso(now) -> str:
    return store.now_iso(dt.datetime.fromtimestamp(_now(now)).astimezone())


def _ts(value) -> float | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def fingerprint(failing) -> str:
    text = "\n".join(sorted({str(f) for f in failing or ()}))
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _hist(rec: dict, what: str, now, by="queue") -> None:
    rec.setdefault("history", []).append({"at": _iso(now), "what": what, "by": by})
    del rec["history"][:-100]


def _event(cfg, type_, project, data) -> None:
    try:
        E.append(cfg, type_, project=project, by="queue", data=data)
    except (ValueError, OSError):
        pass


# ---- the due flag (sentinel.mark_due) -------------------------------------------------------

def due_reason(cfg, project) -> str | None:
    """Why the sentinel is due regardless of its reuse count (a fix merged, a legacy red to
    re-check), else None. Only while red_main is enabled."""
    if not enabled(cfg):
        return None
    rec = _load(cfg, project) or {}
    return rec.get("due_reason") or None


def set_due(cfg, project, reason: str, *, now=None) -> None:
    with locked(cfg, project):
        rec = _load(cfg, project) or _blank()
        rec["due_reason"] = str(reason)
        rec["due_at"] = _iso(now)
        _save(cfg, project, rec)


def _clear_due(rec: dict) -> None:
    rec.pop("due_reason", None)
    rec.pop("due_at", None)


def adopt_legacy(cfg, project, sentinel_state: dict | None, *, now=None) -> bool:
    """First tick after SPEC-0420 landed: a project whose sentinel is red (`force_full_close`)
    but that has no `red.json` yet gets its sentinel marked due, so the next run at the current
    base either opens episode 1 (withdrawing the legacy escalations as superseded) or is green
    (withdrawing them as resolved). Never opens a fix campaign for a failure that may already be
    fixed. True when it marked the sentinel due."""
    if not enabled(cfg) or path(cfg, project).exists():
        return False
    if not (sentinel_state or {}).get("force_full_close"):
        return False
    with locked(cfg, project):
        if path(cfg, project).exists():
            return False
        rec = _blank()
        rec["due_reason"] = "legacy red adopted (SPEC-0420): re-check the base"
        rec["due_at"] = _iso(now)
        _hist(rec, "legacy red adopted; sentinel marked due", now)
        _save(cfg, project, rec)
    return True


# ---- merges of an episode -------------------------------------------------------------------

def _is_ancestor(root, rev, tip) -> bool | None:
    if not root or not rev or not tip:
        return None
    if rev == tip:
        return True
    try:
        code = git.git(root, "merge-base", "--is-ancestor", str(rev), str(tip),
                       check=False).returncode
    except (git.GitError, ValueError, OSError):
        return None
    return {0: True, 1: False}.get(code)


def merges_between(cfg, project, root, sha, last_green_sha) -> list[dict]:
    """The merged campaigns of `project` (fix campaigns excluded) whose `close` event's
    `merged_sha` is `sha` or an ancestor of it and not an ancestor of `last_green_sha`, in base
    order (by the merged sha's commit depth; merges in one second tie on time). An unknown `last_green_sha` gives the
    `MERGES_UNKNOWN_GREEN` newest."""
    out = []
    for st in store.list_campaigns(cfg, include_terminal=True, project=project, state="MERGED"):
        if st.get("kind") in RED_KINDS:
            continue
        try:
            closes = [e for e in store.read_ledger(cfg, st["id"], types=["close"])
                      if e.get("merged_sha")]
        except (OSError, ValueError):
            continue
        if not closes:
            continue
        ev = closes[-1]
        merged = ev["merged_sha"]
        if _is_ancestor(root, merged, sha) is not True:
            continue
        if last_green_sha and _is_ancestor(root, merged, last_green_sha) is not False:
            continue
        out.append({"campaign": st["id"], "spec_id": st.get("spec_id"),
                    "kind": st.get("kind"), "target_sha": ev.get("target_sha"),
                    "merged_sha": merged, "ts": ev.get("ts")})
    depth = {}
    for m in out:                       # base order: the merged sha's depth on the base
        try:
            depth[m["merged_sha"]] = int(git.git(root, "rev-list", "--count", m["merged_sha"],
                                                 check=False).stdout.strip() or 0)
        except (git.GitError, ValueError, OSError):
            depth[m["merged_sha"]] = 0
    out.sort(key=lambda m: (depth.get(m["merged_sha"], 0), _ts(m["ts"]) or 0.0, m["campaign"]))
    if not last_green_sha:
        out = out[-MERGES_UNKNOWN_GREEN:]
    return [{k: v for k, v in m.items() if k != "ts"} for m in out]


# ---- sentinel hooks -------------------------------------------------------------------------

def legacy_escalations(cfg, project) -> list[dict]:
    """Open campaign-less escalations the pre-SPEC-0420 sentinel filed for `project`."""
    from . import escalations as ESC
    try:
        recs = ESC.list_all(cfg, "open")
    except (OSError, ValueError):
        return []
    return [r for r in recs if not r.get("campaign")
            and str(r.get("question") or "").startswith(f"{LEGACY_PREFIX}{project} ")]


def _withdraw(cfg, esc_id, reason) -> bool:
    from . import escalations as ESC
    try:
        ESC.withdraw(cfg, esc_id, reason, "queue")
        return True
    except (ValueError, OSError):
        return False


def on_red(cfg, project, sha, failing, *, root=None, base=None, logs=(), now=None) -> dict:
    """A red sentinel at `sha`: open the project's episode, or update the open one. Appends a
    `red_main` event when the episode opens or its failing set changes. Never escalates; opening
    an episode withdraws the legacy "Sentinel red on <project>" escalations as superseded."""
    from . import sentinel as SN
    failing = [str(f) for f in failing or ()][:FAILING_MAX]
    opened = changed = False
    with locked(cfg, project):
        rec = {**_blank(), **(_load(cfg, project) or {})}
        _clear_due(rec)
        if not rec.get("open"):
            try:
                green = SN.read(cfg, project, root=root).get("last_green_sha")
            except (ValueError, OSError):
                green = None
            prev = int(rec.get("episode") or 0)
            rec = {**_blank(), "episode": prev + 1, "open": True, "opened_at": _iso(now),
                   "first_red_sha": sha, "last_red_sha": sha, "last_green_sha": green,
                   "failing": failing, "fingerprint": fingerprint(failing), "runs": 1,
                   "merges": merges_between(cfg, project, root, sha, green),
                   "logs": [str(p) for p in logs]}
            _hist(rec, f"opened at {str(sha)[:10]}", now)
            opened = True
        else:
            old = rec.get("fingerprint")
            union = list(dict.fromkeys(list(rec.get("failing") or []) + failing))[:FAILING_MAX]
            rec.update(last_red_sha=sha, runs=int(rec.get("runs") or 0) + 1, failing=union,
                       fingerprint=fingerprint(failing), logs=[str(p) for p in logs])
            known = {m["campaign"] for m in rec.get("merges") or []}
            for m in merges_between(cfg, project, root, sha, rec.get("last_green_sha")):
                if m["campaign"] not in known:
                    rec["merges"].append(m)
            # a fix that merged before this run and did not turn the base green failed
            for fx in rec.get("fixes") or []:
                if fx.get("outcome") == "merged" and fx.get("merged_sha") \
                        and _is_ancestor(root, fx["merged_sha"], sha) is True:
                    fx["outcome"] = "failed"
                    fx["why"] = f"merged, base still red at {str(sha)[:10]}"
                    _hist(rec, f"{fx['campaign']} merged but the base stayed red", now)
            changed = rec["fingerprint"] != old
            _hist(rec, f"red again at {str(sha)[:10]}", now)
        _save(cfg, project, rec)
    if opened or changed:
        _event(cfg, "red_main", project, {
            "episode": rec["episode"], "sha": sha, "failing": failing[:20],
            "opened": opened,
            "merges": [{"campaign": m["campaign"], "spec_id": m.get("spec_id")}
                       for m in rec.get("merges") or []]})
    if opened:
        for esc in legacy_escalations(cfg, project):
            _withdraw(cfg, esc["id"], f"superseded by red episode {rec['episode']} of {project} "
                                      "(SPEC-0420: the queue owns the fix)")
    return rec


def on_green(cfg, project, sha, *, now=None) -> dict | None:
    """A green sentinel at `sha`: close the open episode (`red_main_cleared`), withdraw its
    escalation and every open legacy "Sentinel red on <project>" escalation. The fix campaigns it
    made moot are abandoned by the next tick (`sweep_moot`), which runs on the main thread."""
    closed = None
    with locked(cfg, project):
        rec = _load(cfg, project)
        if rec is not None:
            rec = {**_blank(), **rec}
            _clear_due(rec)
            if rec.get("open"):
                rec.update(open=False, closed_at=_iso(now))
                _hist(rec, f"closed: green at {str(sha)[:10]}", now)
                closed = rec
            _save(cfg, project, rec)
    reason = f"sentinel green at {str(sha)[:10]}"
    if closed is not None:
        if closed.get("escalation"):
            _withdraw(cfg, closed["escalation"], reason)
        opened = _ts(closed.get("opened_at")) or _now(now)
        _event(cfg, "red_main_cleared", project, {
            "episode": closed["episode"], "sha": sha,
            "duration_s": max(0, int(_now(now) - opened)),
            "fixes": [{"campaign": f.get("campaign"), "kind": f.get("kind"),
                       "outcome": f.get("outcome")} for f in closed.get("fixes") or []]})
        for fx in closed.get("fixes") or []:
            if fx.get("kind") == "revert" and fx.get("outcome") == "merged":
                _mail_reverted(cfg, project, closed, fx)
    for esc in legacy_escalations(cfg, project):
        _withdraw(cfg, esc["id"], reason)
    return closed


def _mail_reverted(cfg, project, rec, fx) -> None:
    from . import mailbox as M
    c = rec.get("culprit") or {}
    what = c.get("spec_id") or c.get("campaign") or "?"
    try:
        M.send(cfg, to="conductor", frm="conductor", kind="info", campaign=fx.get("campaign"),
               text=f"{what} reverted by {fx.get('campaign')} (red episode {rec.get('episode')} "
                    f"of {project}, campaign {c.get('campaign')}); re-queue it")
    except (ValueError, OSError):
        pass


# ---- hold (close.prepare_close) -------------------------------------------------------------

def holding(cfg, project, state: dict, red: dict | None = None,
            project_cfg: dict | None = None) -> str | None:
    """Why the close of `state` is held, or None: no open episode, the hold is off, the campaign
    fixes the red base (`fixforward`, `revert`, a `--fixes-red` merge), a `--docs-only` merge, or a
    project that does not merge locally (its PRs never land on the base here)."""
    sett = settings(cfg)
    if not sett["enabled"] or not sett["hold_merges"] or not project:
        return None
    if state.get("kind") in RED_KINDS or state.get("fixes_red"):
        return None
    if state.get("kind") == "merge" and (state.get("merge") or {}).get("docs_only"):
        return None
    if project_cfg is not None and ((project_cfg.get("merge") or {}).get("policy")
                                    or "merge") != "merge":
        return None
    red = red if red is not None else read(cfg, project)
    if not red or not red.get("open"):
        return None
    return (f"base of {project} is red (episode {red.get('episode')}, since "
            f"{red.get('opened_at')}): non-fix merges are held until a green sentinel")


# ---- the decision ---------------------------------------------------------------------------

def _state_of(cfg, cid) -> dict | None:
    try:
        return store.read_state(cfg, cid)
    except (ValueError, OSError):
        return None


def _parked_auto(st: dict) -> bool:
    return st.get("state") == "PARKED" and isinstance((st.get("park") or {}).get("auto"), dict)


def refresh(cfg, project, *, now=None) -> tuple[dict | None, list[str]]:
    """Main thread, open episode only: bring each unfinished fix's `outcome` up to date from its
    campaign (MERGED → merged, ABANDONED → failed, PARKED for a reason the queue does not resume
    itself → failed, returned so the tick abandons it). Returns (state, campaigns to abandon)."""
    to_abandon = []
    with locked(cfg, project):
        rec = _load(cfg, project)
        if rec is None or not rec.get("open"):
            return (({**_blank(), **rec}) if rec else None), []
        rec = {**_blank(), **rec}
        dirty = False
        for fx in rec.get("fixes") or []:
            if fx.get("outcome") is not None:
                continue
            st = _state_of(cfg, fx.get("campaign"))
            if st is None:
                fx["outcome"], fx["why"], dirty = "failed", "campaign missing", True
                continue
            s = st.get("state")
            if s == "MERGED":
                fx["outcome"] = "merged"
                fx["merged_sha"] = (st.get("close") or {}).get("merged_sha")
                dirty = True
            elif s == "ABANDONED":
                fx["outcome"], fx["why"], dirty = "failed", "abandoned", True
            elif s == "HANDED_OFF":
                fx["outcome"], fx["why"], dirty = "merged", "handed off (PR)", True
            elif s == "PARKED" and not _parked_auto(st):
                reason = (st.get("park") or {}).get("reason")
                fx["outcome"], fx["why"], dirty = "failed", f"parked {reason}", True
                if (st.get("park") or {}).get("reason") != "human_decision":
                    to_abandon.append(st["id"])
            if dirty and fx.get("outcome"):
                _hist(rec, f"{fx['campaign']} ({fx.get('kind')}): {fx['outcome']}"
                           + (f" ({fx.get('why')})" if fx.get("why") else ""), now)
        if dirty:
            _save(cfg, project, rec)
    return rec, to_abandon


def failed_fixforwards(rec: dict) -> int:
    n = sum(1 for f in rec.get("fixes") or []
            if f.get("kind") == "fixforward" and f.get("outcome") == "failed")
    return max(0, n - int(rec.get("resume_credit") or 0))


def fix_in_flight(rec: dict, active, project) -> str | None:
    """The id of a live fix campaign of the episode (not PARKED, not terminal), or a live
    `--fixes-red` merge of the project, else None."""
    ep = rec.get("episode")
    for st in active or ():
        if st.get("project") != project or st.get("state") in store.TERMINAL_STATES \
                or st.get("state") == "PARKED":
            continue
        if (st.get("kind") in RED_KINDS and st.get("red_episode") == ep) \
                or (st.get("kind") == "merge" and st.get("fixes_red")):
            return st["id"]
    return None


def _pending(rec: dict, active, project) -> str | None:
    """A fix that is neither live nor finished: merged and awaiting the sentinel, or parked by
    the queue with an automatic resume."""
    ep = rec.get("episode")
    for fx in rec.get("fixes") or []:
        if fx.get("outcome") == "merged":
            return fx.get("campaign")
    for st in active or ():
        if st.get("project") == project and st.get("kind") in RED_KINDS \
                and st.get("red_episode") == ep and _parked_auto(st):
            return st["id"]
    return None


def step(cfg, project, *, now=None, active=(), red=None, project_cfg=None) -> tuple[str, str]:
    """The tick's decision for the project's open episode: (`none` | `open_fixforward` |
    `localize` | `open_revert` | `escalate`, reason). Pure over `red` and `active`."""
    sett = settings(cfg)
    rec = red if red is not None else read(cfg, project)
    if not sett["enabled"] or not rec or not rec.get("open"):
        return "none", "no open episode"
    live = fix_in_flight(rec, active, project)
    if live:
        return "none", f"fix in flight: {live}"
    escalated = bool(rec.get("escalation"))
    age = _now(now) - (_ts(rec.get("opened_at")) or _now(now))
    pending = _pending(rec, active, project)
    if pending:
        if not escalated and age > sett["escalate_after_s"]:
            return "escalate", (f"red for {int(age)} s with no fix in flight ({pending} "
                                "awaits the sentinel)")
        return "none", f"{pending} awaits the next sentinel"
    from . import persona_gate as PG
    if (cfg or {}).get("data_dir") and PG.usage_until(cfg) is not None:
        return "none", "usage exhausted; fix-forward waits"  # SPEC-0468
    failed = failed_fixforwards(rec)
    if failed < sett["fixforward_attempts"]:
        return "open_fixforward", f"attempt {failed + 1} of {sett['fixforward_attempts']}"
    policy = ((project_cfg or {}).get("merge") or {}).get("policy") or "merge"
    reverts = [f for f in rec.get("fixes") or [] if f.get("kind") == "revert"]
    loc = rec.get("localize") or {}
    if sett["revert"] and policy == "merge" and not reverts:
        if rec.get("culprit"):
            return "open_revert", f"revert {rec['culprit'].get('campaign')}"
        if not loc.get("done"):
            return "localize", f"{failed} fix-forward attempts failed"
    if escalated:
        return "none", f"escalated ({rec['escalation']}); waiting for the answer"
    if reverts:
        why = f"the revert {reverts[-1].get('campaign')} {reverts[-1].get('outcome')}"
    elif sett["revert"] and policy == "merge":
        why = f"no culprit merge localized ({loc.get('why') or '?'})"
    else:
        why = "fix-forward exhausted; revert is off for this project"
    return "escalate", f"automatic fixing exhausted: {why}"


# ---- fix campaigns --------------------------------------------------------------------------

def brief(cfg, rec: dict, *, root=None, project=None) -> str:
    """The fix-forward brief: failing ids, sentinel gate-log tails, the merges since the last
    green (spec id, diffstat), the culprit, and the rules."""
    sett = settings(cfg)
    ids = list(rec.get("failing") or [])
    lines = [f"The base branch of {project or '?'} is RED (red episode {rec.get('episode')}, "
             f"first red at {str(rec.get('first_red_sha') or '?')[:10]}, last red at "
             f"{str(rec.get('last_red_sha') or '?')[:10]}). The full test suite fails on the base.",
             "", f"Failing tests ({len(ids)}):"]
    lines += [f"- {i}" for i in ids[:BRIEF_IDS_MAX]]
    if len(ids) > BRIEF_IDS_MAX:
        lines.append(f"- … and {len(ids) - BRIEF_IDS_MAX} more")
    merges = rec.get("merges") or []
    lines += ["", f"Merges since the last green sentinel ({len(merges)}):"]
    for m in merges:
        stat = ""
        if root and m.get("target_sha") and m.get("merged_sha"):
            try:
                stat = git.git(root, "diff", "--stat", f"{m['target_sha']}..{m['merged_sha']}",
                               check=False).stdout.strip()
            except (git.GitError, OSError):
                stat = ""
        lines.append(f"- {m.get('campaign')} ({m.get('spec_id') or m.get('kind') or '-'}): "
                     f"{str(m.get('target_sha') or '?')[:10]}..{str(m.get('merged_sha'))[:10]}")
        lines += [f"    {s}" for s in stat.splitlines()[-15:]]
    if not merges:
        lines.append("- none recorded (the failure may come from a direct commit on the base)")
    if rec.get("culprit"):
        lines += ["", f"Localized culprit: {rec['culprit'].get('campaign')} "
                      f"({rec['culprit'].get('spec_id') or '-'})"]
    n = sett["brief_log_lines"]
    for p in rec.get("logs") or []:
        try:
            tail = Path(p).read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        except OSError:
            continue
        if n and tail:
            lines += ["", f"Sentinel gate log {Path(p).name} (last {len(tail)} lines):", "```"]
            lines += tail + ["```"]
    lines += ["", "Rules:",
              "- Make every failing test above pass on the base. Run them by id while you work.",
              "- Never delete, skip, xfail or importorskip a test; never add collect_ignore; never "
              "weaken a guard test or what it scans. The close refuses such a diff.",
              "- Prefer fixing the newly merged code, or the newly merged test that violates a "
              "guard, over changing older code.",
              "- Change nothing unrelated. There is no spec: do not edit docs/specs/ or the ledger."]
    return "\n".join(lines)


def _new_state(cfg, project, kind, rec, *, now=None) -> dict:
    from . import project as P
    from .profiles import profile_sha256, resolve_profile
    pcfg = P.project_config(cfg, project)
    root = Path(pcfg["_root"])
    base = (pcfg.get("merge") or {}).get("base") or pcfg.get("base") or "main"
    prof = resolve_profile(cfg, None, project_root=root)
    prefix = "FIX" if kind == "fixforward" else "REVERT"
    cid = store.new_campaign_id(cfg, f"{prefix}-{E._safe(str(project))}")
    n = sum(1 for f in rec.get("fixes") or [] if f.get("kind") == kind) + 1
    return {
        "schema": "nw.campaign.v1", "id": cid, "kind": kind, "spec_id": None,
        "project": project, "profile": prof["name"], "state": "QUEUED", "prev_state": None,
        "created_at": store.now_iso(), "priority": 1, "force": False, "backlog": None,
        "full_suite": True, "red_episode": rec["episode"], "red_attempt": n,
        "profile_sha256": profile_sha256(prof),
        "repo": {"root": str(root), "base": base, "base_sha_at_open": None,
                 "branch": f"nw/{cid}", "worktree": str(P.worktree_root(pcfg, root, project) / cid),
                 "head": None},
        "spec": None,
        "counters": {"loops": 0, "runs": 0, "gate_runs": 0, "gate_retries": 0,
                     "timeout_regates": 0, "triage_retries": 0, "blocked_streak": 0,
                     "close_attempts": 0, "parks": 0},
        "cost": {"total_usd": 0.0, "cap_usd": prof.get("campaign_cap_usd"), "by_persona": {}},
        "last_gate": None, "last_verdict": None, "pending_brief": None, "park": None,
        "close": None,
    }


def _open(cfg, project, kind, *, now=None, extra=None) -> dict:
    """Create a fix campaign of `kind` in QUEUED and record it in the episode's `fixes`."""
    with locked(cfg, project):
        rec = {**_blank(), **(_load(cfg, project) or {})}
        if not rec.get("open"):
            raise ValueError(f"{project} has no open red episode")
        st = _new_state(cfg, project, kind, rec, now=now)
        st.update(extra(rec, st) if extra else {})
        store.create_campaign(cfg, st, by="queue")
        rec.setdefault("fixes", []).append({"campaign": st["id"], "kind": kind,
                                            "opened_at": _iso(now), "outcome": None})
        _hist(rec, f"opened {kind} {st['id']}", now)
        _save(cfg, project, rec)
    _event(cfg, "red_main_fix_opened", project, {"campaign": st["id"], "kind": kind,
                                                 "attempt": st["red_attempt"],
                                                 "episode": rec["episode"]})
    return st


def open_fixforward(cfg, project, *, now=None) -> dict:
    def extra(rec, st):
        text = brief(cfg, rec, root=st["repo"]["root"], project=project)
        return {"red_brief": text,
                "pending_brief": {"source": "red_main", "fix_brief": text,
                                  "must_fix": list(rec.get("failing") or [])[:BRIEF_IDS_MAX],
                                  "human_notes": []}}
    return _open(cfg, project, "fixforward", now=now, extra=extra)


def open_revert(cfg, project, *, now=None) -> dict:
    def extra(rec, st):
        c = rec.get("culprit") or {}
        if not c.get("target_sha") or not c.get("merged_sha"):
            raise ValueError("no culprit merge with a commit range to revert")
        return {"revert": {"campaign": c.get("campaign"), "spec_id": c.get("spec_id"),
                           "target_sha": c["target_sha"], "merged_sha": c["merged_sha"]}}
    return _open(cfg, project, "revert", now=now, extra=extra)


def record_merge(cfg, state: dict, merged_sha, *, now=None) -> bool:
    """A campaign merged: a fix of the open episode (`fixforward`, `revert`, `--fixes-red` merge)
    gets `outcome: merged` (a `--fixes-red` merge is appended to `fixes` as `kind: merge`), and
    the sentinel is marked due so the green confirmation runs on the next idle tick. True when it
    was a fix."""
    project = state.get("project")
    if not project or not enabled(cfg):
        return False
    if state.get("kind") not in RED_KINDS and not state.get("fixes_red"):
        return False
    with locked(cfg, project):
        rec = _load(cfg, project)
        if rec is None or not rec.get("open"):
            return False
        rec = {**_blank(), **rec}
        hit = next((f for f in rec["fixes"] if f.get("campaign") == state["id"]), None)
        if hit is None:
            hit = {"campaign": state["id"], "kind": "merge" if state.get("kind") == "merge"
                   else state.get("kind"), "opened_at": _iso(now), "outcome": None}
            rec["fixes"].append(hit)
        hit.update(outcome="merged", merged_sha=merged_sha)
        rec["due_reason"] = f"fix merged ({state['id']})"
        rec["due_at"] = _iso(now)
        _hist(rec, f"{state['id']} merged {str(merged_sha)[:10]}; sentinel due", now)
        _save(cfg, project, rec)
    return True


def set_localized(cfg, project, result: dict, *, now=None) -> dict | None:
    with locked(cfg, project):
        rec = _load(cfg, project)
        if rec is None or not rec.get("open"):
            return None
        rec = {**_blank(), **rec}
        rec["localize"] = {"done": True, "at": _iso(now), "why": result.get("why"),
                           "probes": result.get("probes")}
        rec["culprit"] = result.get("culprit")
        c = rec["culprit"]
        _hist(rec, f"localized: {c.get('campaign') if c else 'no culprit'} "
                   f"({result.get('why')})", now)
        _save(cfg, project, rec)
        return rec


def set_escalation(cfg, project, esc_id, *, now=None) -> None:
    with locked(cfg, project):
        rec = {**_blank(), **(_load(cfg, project) or {})}
        rec["escalation"] = esc_id
        _hist(rec, f"escalated {esc_id}", now)
        _save(cfg, project, rec)


def escalation_plan(cfg, project, rec: dict, why: str) -> dict:
    """`escalations.plan_escalate` for the episode: kind `red_main`, topic `recovery` when
    supported (SPEC-0425), recommendation 1."""
    import inspect
    from . import escalations as ESC
    c = rec.get("culprit") or {}
    merges = rec.get("merges") or []
    target = c.get("campaign") or (merges[-1]["campaign"] if merges else "the newest suspect")
    question = (f"Red base on {project} (episode {rec.get('episode')}, since "
                f"{rec.get('opened_at')}): {why}. Non-fix merges are held until it is green.")
    facts = [f"failing: {', '.join((rec.get('failing') or [])[:20]) or '?'}",
             f"culprit: {c.get('campaign') or 'not localized'}"
             + (f" ({c.get('spec_id')})" if c.get("spec_id") else "")]
    facts += [f"fix {f.get('campaign')} ({f.get('kind')}): {f.get('outcome') or 'open'}"
              + (f" - {f['why']}" if f.get("why") else "") for f in rec.get("fixes") or []]
    kw = {}
    if "topic" in inspect.signature(ESC.plan_escalate).parameters:
        kw["topic"] = "recovery"
    plan = ESC.plan_escalate(
        cfg, None, question=question, facts=facts, recommend=1, frm="conductor",
        kind="red_main",
        options=("open another fix-forward attempt (resume the episode: nw queue sentinel "
                 f"resume-fix --project {project} --yes)",
                 f"revert {target} by hand-resolved revert",
                 f"accept red for now: nw queue sentinel clear --project {project}"), **kw)
    plan["project"] = project
    return plan


def escalate(cfg, project, why, *, now=None) -> dict | None:
    """File the episode's single escalation (best effort); None when it already has one."""
    from . import escalations as ESC
    rec = read(cfg, project)
    if not rec or not rec.get("open") or rec.get("escalation"):
        return None
    out = ESC.apply_escalate(cfg, escalation_plan(cfg, project, rec, why))
    set_escalation(cfg, project, out["id"], now=now)
    return out


def resume_fix(cfg, project, *, by="conductor", now=None) -> dict:
    """Escalation choice 1: one more fix-forward attempt (the failed count drops by one)."""
    with locked(cfg, project):
        rec = _load(cfg, project)
        if rec is None or not rec.get("open"):
            raise ValueError(f"{project} has no open red episode")
        rec = {**_blank(), **rec}
        rec["resume_credit"] = int(rec.get("resume_credit") or 0) + 1
        _hist(rec, "resume-fix: one more fix-forward attempt", now, by=by)
        _save(cfg, project, rec)
        return rec


def clear(cfg, project, *, by="human", now=None) -> dict | None:
    """`nw queue sentinel clear`: close an open episode (held merges are released)."""
    with locked(cfg, project):
        rec = _load(cfg, project)
        if rec is None or not rec.get("open"):
            return None
        rec = {**_blank(), **rec}
        _clear_due(rec)
        rec.update(open=False, closed_at=_iso(now))
        _hist(rec, "cleared", now, by=by)
        _save(cfg, project, rec)
    if rec.get("escalation"):
        _withdraw(cfg, rec["escalation"], f"red episode cleared by {by}")
    _event(cfg, "red_main_cleared", project, {"episode": rec["episode"], "cleared_by": by,
                                              "fixes": [{"campaign": f.get("campaign"),
                                                         "kind": f.get("kind"),
                                                         "outcome": f.get("outcome")}
                                                        for f in rec.get("fixes") or []]})
    return rec


def moot_campaigns(cfg, project) -> list[dict]:
    """Fix campaigns of `project` whose episode is no longer open and that have not reached
    CLOSING (a CLOSING fix finishes: its merge is gated and harmless)."""
    rec = _load(cfg, project) or {}
    live_ep = rec.get("episode") if rec.get("open") else None
    return [st for st in store.list_campaigns(cfg, project=project)
            if st.get("kind") in RED_KINDS and st.get("red_episode") != live_ep
            and st.get("state") in MOOT_STATES]


def mark_moot(cfg, project, cid, *, now=None) -> None:
    with locked(cfg, project):
        rec = _load(cfg, project)
        if rec is None:
            return
        for fx in rec.get("fixes") or []:
            if fx.get("campaign") == cid and fx.get("outcome") is None:
                fx["outcome"] = "moot"
                _hist(rec, f"{cid} moot", now)
        _save(cfg, project, rec)


def status(cfg, project, *, now=None) -> dict:
    """`nw queue sentinel status` (`nw.queue.red.v1`)."""
    rec = read(cfg, project)
    out = {"schema": STATUS_SCHEMA, "project": project, "enabled": enabled(cfg),
           "due_reason": due_reason(cfg, project), "episode": rec}
    if rec:
        start = _ts(rec.get("opened_at"))
        end = _ts(rec.get("closed_at")) if not rec.get("open") else _now(now)
        out["age_s"] = int(end - start) if start and end else None
        out["failed_fixforwards"] = failed_fixforwards(rec)
    return out


# ---- localize -------------------------------------------------------------------------------

def _test_ids(failing) -> list[str]:
    return [f for f in failing or () if "::" in str(f) or str(f).endswith(".py")]


def _probe(wt: Path, ids, cmd: str, timeout_s: float) -> bool:
    """True when every id passes at the checked-out tree (an id whose file does not exist there
    cannot fail). RuntimeError when pytest errors out (exit code not 0 or 1)."""
    present = [i for i in ids if (wt / str(i).split("::", 1)[0]).is_file()]
    if not present:
        return True
    r = subprocess.run(f"{cmd} " + " ".join(_quote(i) for i in present), shell=True,
                       cwd=str(wt), capture_output=True, text=True, timeout=timeout_s)
    if r.returncode in (0, 1):
        return r.returncode == 0
    tail = "\n".join(((r.stdout or "") + (r.stderr or "")).splitlines()[-10:])
    raise RuntimeError(f"probe exited {r.returncode}: {tail}")


def _quote(s) -> str:
    import shlex
    return shlex.quote(str(s))


def bisect(merges: list, fails) -> tuple[dict | None, str, int]:
    """Pure over `fails(sha) -> bool` (cached): the first merge whose `merged_sha` fails while
    its `target_sha` passes. Returns (culprit, why, probes)."""
    cache: dict = {}

    def f(sha):
        if sha not in cache:
            cache[sha] = fails(sha)
        return cache[sha]

    n = len(merges)
    if n == 0:
        return None, "no merges in the episode's range", 0
    if n == 1:
        return merges[0], "the only merge since the last green", 0
    lo, hi = 0, n - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if f(merges[mid]["merged_sha"]):
            hi = mid
        else:
            lo = mid + 1
    cand = merges[lo]
    if not f(cand["merged_sha"]):
        return None, "no merge of the episode fails the ids (a direct commit?)", len(cache)
    if cand.get("target_sha") and f(cand["target_sha"]):
        return None, (f"{cand['campaign']}'s base already fails the ids "
                      "(the failure predates the episode's merges)"), len(cache)
    return cand, f"first failing merge of {n} ({len(cache)} probes)", len(cache)


def localize(cfg, project, *, root=None, project_cfg=None, stop=None) -> dict:
    """Bisect the episode's merges in the sentinel worktree, running only the failing ids
    (`agentic.red_main.probe_cmd`), under the sentinel run lock and a local gate slot. Returns
    `{culprit, why, probes}`; `{retry: True}` when a sentinel run holds the lock or a stop came."""
    from . import gate_slots
    from . import project as P
    from . import sentinel as SN
    rec = read(cfg, project)
    if not rec or not rec.get("open"):
        return {"culprit": None, "why": "no open episode", "probes": 0, "retry": True}
    merges = [m for m in rec.get("merges") or [] if m.get("merged_sha")]
    ids = _test_ids(rec.get("failing"))
    if len(merges) <= 1 or not ids:
        if len(merges) > 1:
            return {"culprit": None, "why": "no failing test ids to probe", "probes": 0}
        c, why, n = bisect(merges, lambda sha: True)
        return {"culprit": c, "why": why, "probes": n}
    sett = settings(cfg)
    pcfg = project_cfg or P.project_config(cfg, project)
    root = root or pcfg.get("_root") or str(P.project_root(cfg, project))
    wt = SN.worktree_path(cfg, project)
    try:
        with SN.run_lock(cfg, project):
            with gate_slots.acquire(cfg, f"redmain-{E._safe(str(project))}", stop=stop) as hold:
                if hold.interrupted:
                    return {"culprit": None, "why": "interrupted", "probes": 0, "retry": True}

                def fails(sha):
                    SN._prepare_worktree(root, wt, sha, pcfg)
                    return not _probe(wt, ids, sett["probe_cmd"], sett["probe_timeout_s"])

                try:
                    c, why, n = bisect(merges, fails)
                except (RuntimeError, subprocess.TimeoutExpired, git.GitError, OSError) as e:
                    return {"culprit": None, "why": f"probe failed: {e}"[:500], "probes": 0}
    except SN.Busy as e:
        return {"culprit": None, "why": str(e), "probes": 0, "retry": True}
    return {"culprit": c, "why": why, "probes": n}
