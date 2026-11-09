"""Persona dispatch gate, rate-limit backoff and campaign affinity (SPEC-0345).

- **Dispatch gate.** Every campaign persona step (`GATED_KINDS`; the campaign's spec review is
  not one) passes it in the tick before SPEC-0344's eligibility check. A closed gate leaves the
  step pending (retried on a later tick); nothing falls back to local. It is closed while the
  account's rate-limit backoff runs (`rate-limit.json` `until` in the future) and, with
  `agentic.workers.max_personas` set, when the live campaign persona runs (local and remote)
  leave no room; a reviewer fan-out then dispatches only as many members as fit.
- **Rate limit.** A run whose claude result is an API rate-limit error (`is_rate_limit`) starts
  a backoff episode (60 s, doubled per episode up to 900 s); one `rate_limited` queue event per
  episode. A success from a run started after `until` resets the next episode to 60 s.
- **Holds.** The tick records which campaigns the gate held (`persona-holds.json`, stamped
  `at` on every tick with a hold); the stuck detectors skip them, and only them, while the
  stamp is fresh (`HOLDS_FRESH_S`), and `nw queue list` / `status` shows `held_by`.
- **Affinity.** `state.remote_affinity` (+ `remote_affinity_at`): the worker the campaign's first
  remote persona run went to. Persona runs and gate runs prefer it while it is not full and not
  failing; a `worker_job_lost` for the campaign on it clears it."""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import store

GATED_KINDS = frozenset({"implement", "triage", "review", "write_spec", "resolve", "divergence"})
UNGATED_PERSONAS = frozenset({"spec_reviewer"})          # counted by neither cap nor backoff
FIRST_BACKOFF_S = 60
MAX_BACKOFF_S = 900
# the detectors stop skipping a `persona_cap` hold older than this: a live count inflated by a
# run that never ends (no `run.end`, a persona job record never final) would hold it forever
CAP_MUTE_MAX_S = 7200
# the holds file is the last tick's: a runner ticks every `poll_interval_s` (20 s by default)
# and rewrites it (`at`) on each tick with a hold, so holds not rewritten for this long are
# from a runner that stopped ticking, and the detectors stop skipping them (`read_holds`)
HOLDS_FRESH_S = 600
# a `worker_job_lost` keeps that worker failing for the campaign for at most this long: without
# a bound, one loss before any affinity (or with no other worker to move the affinity to) would
# keep the campaign's persona runs off the worker for the rest of its life
LOSS_FAILING_S = 1800

# whole-token rate-limit markers of an API error message (`events.RATE_LIMIT_MARKERS`)
_RATE_RE = re.compile(r"\b429\b|\b529\b|\brate[ _]limit|\boverloaded\b")
# an account usage/session/weekly limit (its own handling, never the backoff), in the wordings
# the claude CLI uses: "Claude AI usage limit reached|<epoch>", "5-hour limit reached ∙ resets
# 3am", "You've hit your limit · resets 3am", "Opus weekly limit reached". A bare "limit
# reached" or "resets in 30s" is not one: an API rate-limit message says those too, and so is
# "hit your rate limit" / "hit your request limit" (only "hit your [usage|…] limit" is)
_ACCOUNT_LIMIT_RE = re.compile(r"usage[_ ]limit|session limit|weekly limit|\b\d+[- ]hour limit\b"
                               r"|\bhit your (?:(?:usage|session|weekly|daily|monthly|opus"
                               r"|sonnet|\d+[- ]hour) )?limit\b"
                               r"|\bresets (?:at )?\d{1,2}(?::\d\d)?\s*[ap]\.?m\b")
# SPEC-0468: a Cursor account wall ("You're out of usage", "You've hit your usage limit")
# or an error result whose only signal is `feature_unavailable`. Not a rate-limit episode.
_USAGE_RE = re.compile(r"out of usage|usage[_ ]limit|hit your usage limit|feature_unavailable")
USAGE_PAUSE_S = 900


# ---- config -----------------------------------------------------------------------------------

def check_max_personas(value) -> int | None:
    """`agentic.workers.max_personas`: None or an int >= 1, else ValueError."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"agentic.workers.max_personas must be an int >= 1 or null, "
                         f"got {value!r}")
    return value


def max_personas(cfg) -> int | None:
    """The configured cap; an invalid value (rejected by `validate_runner_cfg`) reads as none."""
    raw = ((cfg or {}).get("agentic") or {}).get("workers")
    try:
        return check_max_personas(raw.get("max_personas") if isinstance(raw, dict) else None)
    except ValueError:
        return None


# ---- small helpers ----------------------------------------------------------------------------

def _iso(epoch_s: float) -> str:
    return dt.datetime.fromtimestamp(epoch_s).astimezone().isoformat(timespec="seconds")


def _epoch(ts) -> float | None:
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return float(ts)
    try:
        d = dt.datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.astimezone()
    return d.timestamp()


def _now_epoch(now) -> float:
    if now is None:
        return time.time()
    if isinstance(now, dt.datetime):
        return now.timestamp()
    return float(now)


def _write_json(path: Path, doc: dict) -> None:
    from ..export import _atomic_write
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(doc, indent=2) + "\n")


def _read_json(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


# ---- rate limit -------------------------------------------------------------------------------

def rate_limit_path(cfg) -> Path:
    return store.agentic_dir(cfg) / "rate-limit.json"


@contextmanager
def _rate_lock(cfg):
    p = store.agentic_dir(cfg) / "rate-limit.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def read_rate_limit(cfg) -> dict:
    """`{until, backoff_s, reset}` (`until` ISO or None)."""
    doc = _read_json(rate_limit_path(cfg))
    b = doc.get("backoff_s")
    ok = isinstance(b, (int, float)) and not isinstance(b, bool) and b > 0
    return {"until": doc.get("until"), "backoff_s": int(b) if ok else None,
            "reset": doc.get("reset") is True}


def backoff_until(cfg, now=None) -> float | None:
    """The backoff's end (epoch) while it runs, else None."""
    until = _epoch(read_rate_limit(cfg).get("until"))
    return until if until is not None and _now_epoch(now) < until else None


def usage_path(cfg) -> Path:
    """SPEC-0468: host-wide pause while the Cursor account is out of usage."""
    return store.agentic_dir(cfg) / "usage-pause.json"


def usage_until(cfg, now=None) -> float | None:
    """The usage pause's end (epoch) while it holds, else None."""
    until = _epoch(_read_json(usage_path(cfg)).get("until"))
    return until if until is not None and _now_epoch(now) < until else None


def note_usage(cfg, now=None) -> None:
    """Start or refresh a `USAGE_PAUSE_S` pause. A later end is left alone."""
    now_e = _now_epoch(now)
    until = now_e + USAGE_PAUSE_S
    with _rate_lock(cfg):
        current = _epoch(_read_json(usage_path(cfg)).get("until"))
        if current is not None and current >= until:
            return
        _write_json(usage_path(cfg), {"until": _iso(until), "reason": "usage_exhausted"})


def is_usage_exhausted(result) -> bool:
    """An error result that is an account usage wall, not a bad verdict (SPEC-0468).

    Reads `result_text` and, when present, the raw result's `error_message` / empty
    `result` plus `cost_error: feature_unavailable`. A successful run is never one,
    even when its cost fetch reports `feature_unavailable`."""
    if result is None or not getattr(result, "is_error", False) \
            or getattr(result, "interrupted", False):
        return False
    parts = [str(getattr(result, "result_text", "") or "")]
    obj = _raw_result(getattr(result, "raw_path", None)) or {}
    parts.append(str(obj.get("error_message") or ""))
    parts.append(str(obj.get("result") or "") if isinstance(obj.get("result"), str) else "")
    blob = "\n".join(parts).lower()
    if obj.get("cost_error") == "feature_unavailable" and not str(obj.get("result") or "").strip():
        return True
    return bool(_USAGE_RE.search(blob))


def _raw_result(raw_path) -> dict | None:
    if raw_path is None:
        return None
    from . import runner as R
    try:
        text = Path(raw_path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    obj = R._loads_lenient(text)
    if not isinstance(obj, dict) or obj.get("type") not in (None, "result"):
        obj = R._last_result_event(text)
    return obj if isinstance(obj, dict) else None


def is_rate_limit(result) -> bool:
    """The run failed with an API rate-limit error: an error result whose `api_error_status` is
    429/529, or (no status recorded) whose result message is an `API Error` naming 429, 529, a
    rate limit or an overload as whole tokens. Only the API error message counts, never the
    transcript or test output; an account usage/session/weekly limit never counts, whatever its
    status (the claude CLI reports one with status 429)."""
    if result is None or not getattr(result, "is_error", False) \
            or getattr(result, "interrupted", False):
        return False
    obj = _raw_result(getattr(result, "raw_path", None)) or {}
    msg = obj.get("result") if isinstance(obj.get("result"), str) else \
        str(getattr(result, "result_text", "") or "")
    low = msg.strip().lower()
    if _ACCOUNT_LIMIT_RE.search(low):
        return False
    status = obj.get("api_error_status")
    if isinstance(status, int) and not isinstance(status, bool):
        return status in (429, 529)
    if not low.startswith("api error"):
        return False
    return bool(_RATE_RE.search(low.splitlines()[0] if low else ""))


def note_run(cfg, result, started: float | None, *, campaign=None, now=None) -> str | None:
    """Fold one finished campaign persona run into the backoff (under `rate-limit.lock`).
    Returns `rate_limited` when it started an episode, `reset` when it reset `backoff_s`,
    else None. `started` is the run's start (epoch), None when unknown (an adopted run)."""
    if result is None or getattr(result, "interrupted", False):
        return None
    limited = is_rate_limit(result)
    ok = not result.is_error and int(result.exit_code or 0) == 0
    if not limited and not ok:
        return None
    t = _now_epoch(now)
    with _rate_lock(cfg):
        cur = read_rate_limit(cfg)
        until = _epoch(cur.get("until"))
        if limited:
            if until is not None and t < until:
                return None                     # same episode: no doubling, no event
            prev = cur.get("backoff_s")
            backoff = FIRST_BACKOFF_S if prev is None or cur.get("reset") \
                else min(prev * 2, MAX_BACKOFF_S)
            doc = {"until": _iso(t + backoff), "backoff_s": backoff}
            _write_json(rate_limit_path(cfg), doc)
            from . import events as EV
            st = None
            if campaign:
                try:
                    st = store.read_state(cfg, campaign)
                except (ValueError, OSError):
                    st = None
            EV.append(cfg, "rate_limited", campaign=campaign,
                      spec_id=(st or {}).get("spec_id"), project=(st or {}).get("project"),
                      by="queue", data=dict(doc))
            return "rate_limited"
        if cur.get("backoff_s") is None or cur.get("reset") or started is None \
                or until is None or started <= until:
            return None
        _write_json(rate_limit_path(cfg), {"until": cur.get("until"),
                                           "backoff_s": FIRST_BACKOFF_S, "reset": True})
        return "reset"


# ---- the cap: live campaign persona runs ------------------------------------------------------

def _ttl_s(cfg) -> float:
    from . import remote_jobs as RJ
    try:
        return RJ.settings(cfg)["worker_ttl_s"]
    except ValueError:                  # a bad `max_remote_attempts`: the gate step reports it
        return RJ.DEFAULT_WORKER_TTL_S


def live_persona_runs(cfg, campaign_ids, now=None) -> int:
    """Live campaign persona runs: the open runs (`run.start` without `run.end`) of the given
    campaigns plus the live `kind: persona` remote job records, each `(campaign, run_id)` once.
    Spec-review runs are not counted. A run or record past its deadline is not counted, so one
    that never gets its `run.end` or final state (a coordinator crash, a worker unregistered
    mid-job) cannot hold a cap slot forever: an open run's deadline is its start plus its
    `timeout_s` (absent: the longest job timeout) plus the worker TTL, a remote record's is its
    `deadline_at` (absent: `submitted_at` plus the longest job timeout plus the TTL). By then
    the run is killed by its timeout or the job is lost. A persona job lost to the run's local
    re-run (a `worker_job_lost` naming its `run_id`) restarts the run's clock at the loss: the
    re-run gets a full timeout of its own."""
    from . import remote_gates as RG
    from . import remote_jobs as RJ
    t, ttl = _now_epoch(now), _ttl_s(cfg)
    seen = set()
    for cid in campaign_ids:
        starts, ended, restarted = {}, set(), {}
        for ev in store.read_ledger(cfg, cid, types=["run.start", "run.end", "worker_job_lost"]):
            if ev["type"] == "run.start":
                starts[ev.get("run_id")] = ev
            elif ev["type"] == "run.end":
                ended.add(ev.get("run_id"))
            elif ev.get("run_id") is not None:
                restarted[ev["run_id"]] = ev.get("ts")
        for rid, ev in starts.items():
            if rid in ended or ev.get("persona") in UNGATED_PERSONAS:
                continue
            t0, limit = _epoch(ev.get("ts")), ev.get("timeout_s")
            lost = _epoch(restarted.get(rid))
            if t0 is not None and lost is not None:
                t0 = max(t0, lost)
            if not isinstance(limit, (int, float)) or isinstance(limit, bool) or limit <= 0:
                limit = RG.MAX_JOB_TIMEOUT_S
            if t0 is None or t <= t0 + limit + ttl:
                seen.add((cid, rid))
    for rec in RJ.live(cfg):
        if rec.get("kind") != "persona" or rec.get("persona") in UNGATED_PERSONAS:
            continue
        deadline = RJ.epoch(rec.get("deadline_at"))
        if deadline is None:
            sub = RJ.epoch(rec.get("submitted_at"))
            deadline = None if sub is None else sub + RG.MAX_JOB_TIMEOUT_S + ttl
        if deadline is None or t <= deadline:
            seen.add((rec.get("campaign"), rec.get("run_id") or rec.get("job_id")))
    return len(seen)


class Gate:
    """One tick's dispatch gate. `admit(state, step)` returns the step to dispatch (a fan-out
    possibly trimmed to the room left, its other members in `step.held`), or None when the gate
    holds it; `flush()` writes the tick's holds."""

    def __init__(self, cfg, campaign_ids, now=None):
        self.cfg, self.ids = cfg, list(campaign_ids)
        self.now = _now_epoch(now)
        self.until = backoff_until(cfg, self.now)
        self.usage_until = usage_until(cfg, self.now)
        self.cap = max_personas(cfg)
        self._live = None
        self.taken = 0
        self.holds: dict = {}
        self._backend: dict = {}               # SPEC-0482: kind -> preflight error | None

    def backend_fault(self, state: dict) -> str | None:
        """SPEC-0482: the preflight error of the backend this campaign's steps run on, when no
        persona worker could take them instead; else None. Cached per tick and kind."""
        try:
            from .runner import resolve_runner
            kind = resolve_runner(self.cfg, campaign=state, project=state.get("project"))
        except (ValueError, TypeError, KeyError, OSError):
            return None
        if kind != "cursor":
            return None
        if kind not in self._backend:
            from . import backend_preflight as BP
            self._backend[kind] = BP.check(kind)
            if self._backend[kind] is not None:
                BP.note_fault(self.cfg, kind, self._backend[kind], where="gate")
        if self._backend[kind] is None:
            return None
        try:
            from . import remote_personas as RPS
            if RPS.persona_workers(self.cfg, state):
                return None                    # a worker may run it with its own credential
        except Exception:                      # noqa: BLE001
            pass
        return self._backend[kind]

    def live(self) -> int:
        if self._live is None:
            self._live = live_persona_runs(self.cfg, self.ids, self.now)
        return self._live

    def _hold(self, cid, why: str, waiting=()) -> None:
        self.holds[cid] = {"held_by": why, **({"waiting": list(waiting)} if waiting else {})}

    def admit(self, state: dict, step):
        from dataclasses import replace
        cid = state["id"]
        if self.usage_until is not None:
            self._hold(cid, "usage_pause")
            return None
        if self.until is not None:
            self._hold(cid, "rate_limit")
            return None
        if self.backend_fault(state) is not None:            # SPEC-0482
            self._hold(cid, "backend_unavailable")
            return None
        if self.cap is None:
            return step
        room = self.cap - self.live() - self.taken
        personas = tuple(step.personas or ()) if step.kind == "review" else ()
        if room <= 0:
            self._hold(cid, "persona_cap", personas)
            return None
        if len(personas) > room:
            step = replace(step, personas=personas[:room], held=personas[room:])
            self._hold(cid, "persona_cap", personas[room:])
        self.taken += max(1, min(len(personas), room))
        return step

    def flush(self) -> None:
        """Write the tick's holds, stamped `at` (the tick's time): rewritten on every tick that
        holds something, so `read_holds` can tell a live runner's holds from a dead one's."""
        p = holds_path(self.cfg)
        old = _read_json(p).get("holds") or {}
        if not self.holds and not old:
            return
        since = store.now_iso()
        holds = {cid: {**h, "since": (old.get(cid) or {}).get("since")
                       if (old.get(cid) or {}).get("held_by") == h["held_by"] else since}
                 for cid, h in self.holds.items()}
        for h in holds.values():
            h["since"] = h["since"] or since
        _write_json(p, {"holds": holds, "at": since} if holds else {"holds": {}})


def holds_path(cfg) -> Path:
    return store.agentic_dir(cfg) / "persona-holds.json"


def read_holds(cfg, now=None) -> dict:
    """The last tick's holds, `{}` when that tick is older than `HOLDS_FRESH_S` (or the file
    has no `at`): a runner that stopped ticking holds nothing, so its last holds never mute
    the detectors."""
    doc = _read_json(holds_path(cfg))
    h, at = doc.get("holds"), _epoch(doc.get("at"))
    if not isinstance(h, dict) or at is None or _now_epoch(now) - at > HOLDS_FRESH_S:
        return {}
    return h


def held_by(cfg, state: dict, now=None, holds=None, *, cap_mute_max_s=None) -> str | None:
    """`rate_limit` while the backoff runs for a campaign the last tick's gate held on it,
    `persona_cap` while the last tick held its step on the cap, else None: only a step the
    gate held is held, never a campaign waiting for another reason. The caller skips
    campaigns with a run in flight. With `cap_mute_max_s` (the detectors), a `persona_cap` hold
    held since longer than that is None, so the detectors report it again."""
    entry = (read_holds(cfg, now) if holds is None else holds).get(state.get("id")) or {}
    if entry.get("held_by") == "usage_pause" and usage_until(cfg, now) is not None:
        return "usage_pause"
    if entry.get("held_by") == "rate_limit" and backoff_until(cfg, now) is not None:
        return "rate_limit"
    if entry.get("held_by") == "backend_unavailable":       # SPEC-0482: the escalation covers it
        return "backend_unavailable"
    if entry.get("held_by") == "persona_cap":
        since = _epoch(entry.get("since"))
        if cap_mute_max_s is not None and since is not None \
                and _now_epoch(now) - since > cap_mute_max_s:
            return None
        return "persona_cap"
    return None


# ---- affinity ---------------------------------------------------------------------------------

_AFFINITY_LOCK = threading.Lock()


def _losses(cfg, cid) -> list[dict]:
    try:
        return store.read_ledger(cfg, cid, types=["worker_job_lost"])
    except (ValueError, OSError):
        return []


def lost_since(cfg, state: dict, worker: str, since, *, within=None, now=None) -> bool:
    """A `worker_job_lost` of this campaign on `worker` at or after `since` (any, when None);
    with `within`, only one at most `within` seconds old. An uncharged loss (`charged: false`,
    a rotation) is not the worker's fault and never counts."""
    t0 = _epoch(since)
    cutoff = None if within is None else _now_epoch(now) - within
    for ev in _losses(cfg, state.get("id")):
        if ev.get("worker") != worker or ev.get("charged") is False:
            continue
        t = _epoch(ev.get("ts"))
        if cutoff is not None and (t is None or t < cutoff):
            continue
        if t0 is None or (t is not None and t >= t0):
            return True
    return False


def affinity(cfg, state: dict) -> str | None:
    """The campaign's affine worker; None when unset or cleared by a later `worker_job_lost`."""
    name = state.get("remote_affinity")
    if not name:
        return None
    if lost_since(cfg, state, name, state.get("remote_affinity_at")):
        return None
    return name


def failing(cfg, state: dict, w: dict, now=None) -> bool:
    """The worker is disabled, its last `nw workers check` failed, or it lost a job of this
    campaign since the campaign's affinity was last set and within the last `LOSS_FAILING_S`
    (a loss older than that, or one from before any affinity, stops counting then)."""
    return (not w.get("enabled", True) or w.get("check_ok") is False
            or lost_since(cfg, state, w["name"], state.get("remote_affinity_at"),
                          within=LOSS_FAILING_S, now=now))


def order(cfg, state: dict, names: list) -> list:
    """`names` (worker names, registry order; None is the local pool) with the affine worker
    first when present. Never drops a name."""
    aff = affinity(cfg, state)
    if aff is None or aff not in names:
        return list(names)
    return [aff] + [n for n in names if n != aff]


def set_affinity(cfg, state: dict, worker: str) -> bool:
    """Set the campaign's affinity to `worker` when it has none (persona runs only). Mutates the
    snapshot `state` and the stored state; True when it was set. The stored state decides, under
    the item lock: when another dispatch (another snapshot, another process) set a valid
    affinity since `state` was read, that one stays and `state` takes it over."""
    with _AFFINITY_LOCK:
        if affinity(cfg, state) is not None:
            return False
        at = store.now_iso()
        cid = state.get("id")
        if not cid:
            state["remote_affinity"], state["remote_affinity_at"] = worker, at
            return True
        try:
            with store.item_lock(cfg, cid):
                cur = store.read_state(cfg, cid)
                if affinity(cfg, cur) is not None:
                    state["remote_affinity"] = cur["remote_affinity"]
                    state["remote_affinity_at"] = cur.get("remote_affinity_at")
                    return False
                cur["remote_affinity"], cur["remote_affinity_at"] = worker, at
                store.write_state(cfg, cid, cur)
        except (ValueError, OSError):
            return False                          # not stored: the snapshot stays as it was
        state["remote_affinity"], state["remote_affinity_at"] = worker, at
        return True


def clear_affinity(cfg, state: dict, worker: str) -> None:
    """Drop the affinity on `worker` (its job for this campaign was lost)."""
    with _AFFINITY_LOCK:
        if state.get("remote_affinity") != worker:
            return
        state["remote_affinity"] = None
        cid = state.get("id")
        try:
            with store.item_lock(cfg, cid):
                cur = store.read_state(cfg, cid)
                if cur.get("remote_affinity") == worker:
                    cur["remote_affinity"] = None
                    store.write_state(cfg, cid, cur)
        except (ValueError, OSError):
            pass


# ---- cost per worker --------------------------------------------------------------------------

def cost_by_worker(events) -> dict:
    """`{worker|"local": charged USD}` from the campaign's `run.end` events."""
    out: dict = {}
    for ev in events:
        if ev.get("type") != "run.end":
            continue
        key = ev.get("worker") or "local"
        try:
            charged = float(ev.get("charged_usd") if ev.get("charged_usd") is not None
                            else ev.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            continue
        out[key] = round(out.get(key, 0.0) + charged, 4)
    return out
