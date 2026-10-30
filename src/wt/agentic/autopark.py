"""Routine parks decided by the queue (SPEC-0302): a `base_moving` park and a `cost_cap` park
whose block is only the phantom charge of transient-failed runs are retried by the queue itself,
after a backoff, instead of waking the conductor. Pure rules: the queue writes the park, the
decision records and the resume. Only `settings` reads the config; `now` is always an argument."""
from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import asdict, dataclass

from . import events as E

DEFAULTS = {"enabled": True, "max_per_campaign": 3, "base_moving_backoff_s": 60,
            "transient_backoff_s": 900, "phantom_max_duration_s": 120}
MAX_RESET_WAIT_S = 6 * 3600          # a reset further out than this is polled at this horizon

RULE_BASE_MOVING = "parked.base_moving"
RULE_COST_CAP = "parked.cost_cap"

_ISO_RE = re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:?\d\d)?")
_EPOCH_RE = re.compile(r"resets?\s+at\s+(\d{9,11})\b", re.IGNORECASE)


def settings(cfg) -> dict:
    """`DEFAULTS` overlaid with `agentic.conductor.auto`; a bad value falls back to its default."""
    raw = (((cfg.get("agentic") or {}).get("conductor") or {}).get("auto")) or {}
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    if isinstance(raw.get("enabled"), bool):
        out["enabled"] = raw["enabled"]
    for key in ("max_per_campaign", "base_moving_backoff_s", "transient_backoff_s",
                "phantom_max_duration_s"):
        v = raw.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0:
            out[key] = int(v) if key == "max_per_campaign" else v
    return out


@dataclass(frozen=True)
class Auto:
    rule: str
    kind: str
    resume_at: str
    raise_cap: float | None
    n: int
    rationale: str

    def park_data(self) -> dict:
        """What `state.park.auto` and the `park` ledger event carry."""
        return {"rule": self.rule, "resume_at": self.resume_at, "raise_cap": self.raise_cap,
                "n": self.n, "rationale": self.rationale}


def _parse(value) -> dt.datetime | None:
    try:
        ts = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ts.astimezone() if ts.tzinfo is None else ts


def _since_seq(ledger) -> int:
    return max((int(e.get("seq") or 0) for e in ledger if e.get("type") in ("open", "resume")),
               default=0)


def _did_no_work(ev: dict, max_duration_s: float) -> bool:
    usage = ev.get("usage")
    out = usage.get("output_tokens") if isinstance(usage, dict) else None
    if not isinstance(out, (int, float)) or isinstance(out, bool) or out <= 0:
        return True
    dur = ev.get("duration_s")
    return isinstance(dur, (int, float)) and not isinstance(dur, bool) and dur <= max_duration_s


def _phantom_runs(ledger, max_duration_s, since_seq=None) -> list[dict]:
    since = _since_seq(ledger) if since_seq is None else since_seq
    return [e for e in ledger
            if e.get("type") == "run.end" and int(e.get("seq") or 0) > since
            and e.get("cost_estimated") and E._is_transient(e.get("result_tail") or "")
            and _did_no_work(e, max_duration_s)]


def phantom_usd(ledger, since_seq=None, max_duration_s=120) -> float:
    """The sum of `charged_usd` of the `run.end` events after `since_seq` (default: the last
    `resume` / `open`) that were charged as an estimate, failed transiently and did no
    measurable work (`usage` without output tokens, or `duration_s` ≤ `max_duration_s`)."""
    return round(sum(float(e.get("charged_usd") or 0.0)
                     for e in _phantom_runs(ledger, max_duration_s, since_seq)), 4)


def reset_time(text, now) -> dt.datetime | None:
    """The reset time a transient error names: an epoch-seconds `resets at …` value or an
    ISO-8601 timestamp. None when there is none or it is not in the future."""
    text = text or ""
    found = None
    m = _EPOCH_RE.search(text)
    if m:
        try:
            found = dt.datetime.fromtimestamp(int(m.group(1)), tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            found = None
    if found is None:
        m = _ISO_RE.search(text)
        found = _parse(m.group(0)) if m else None
    return found if found is not None and found > now else None


def _count(state: dict, rule: str) -> int:
    counts = (state.get("counters") or {}).get("auto_resumes") or {}
    v = counts.get(rule, 0) if isinstance(counts, dict) else 0
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def decide(state: dict, ledger, sett: dict, now, *, budget=None) -> Auto | None:
    """The queue's decision for a new park of `state`, or None (the conductor decides).
    `ledger` holds at least the campaign's `open` / `resume` / `run.end` events; `budget` is the
    cost-capped run's budget (the `cost_cap` step carries it)."""
    park = state.get("park") or {}
    reason = park.get("reason")
    if not sett.get("enabled"):
        return None
    if reason == "base_moving":
        rule = RULE_BASE_MOVING
        n = _count(state, rule)
        if n >= sett["max_per_campaign"]:
            return None
        at = now + dt.timedelta(seconds=sett["base_moving_backoff_s"] * 2 ** n)
        return Auto(rule, "wait", store_iso(at), None, n,
                    f"the base moved during the close; retry in "
                    f"{int(sett['base_moving_backoff_s'] * 2 ** n)}s "
                    f"(automatic resume {n + 1}/{sett['max_per_campaign']})")
    if reason == "cost_cap":
        rule = RULE_COST_CAP
        n = _count(state, rule)
        cost = state.get("cost") or {}
        cap, total = cost.get("cap_usd"), cost.get("total_usd")
        if n >= sett["max_per_campaign"] or budget is None or cap is None or total is None:
            return None
        runs = _phantom_runs(ledger, sett["phantom_max_duration_s"])
        phantom = round(sum(float(e.get("charged_usd") or 0.0) for e in runs), 4)
        if phantom <= 0 or float(total) - phantom + float(budget) > float(cap) + 1e-9:
            return None
        raise_cap = math.ceil((float(cap) + phantom) * 100 - 1e-9) / 100
        wait = dt.timedelta(seconds=sett["transient_backoff_s"])
        reset = reset_time(runs[-1].get("result_tail"), now)
        at = now + wait
        if reset is not None and reset > at:
            at = reset
        at = min(at, now + dt.timedelta(seconds=MAX_RESET_WAIT_S))
        return Auto(rule, "wait", store_iso(at), raise_cap, n,
                    f"the cap block is the ${phantom:.2f} charged for transient-failed runs that "
                    f"did no work; raise the cap to ${raise_cap:.2f} and retry at the reset "
                    f"(automatic resume {n + 1}/{sett['max_per_campaign']})")
    return None


def store_iso(ts: dt.datetime) -> str:
    return ts.isoformat(timespec="seconds")


def due(states, now) -> list[dict]:
    """PARKED states whose `park.auto.resume_at` has passed."""
    out = []
    for st in states:
        auto = (st.get("park") or {}).get("auto")
        if st.get("state") != "PARKED" or not isinstance(auto, dict):
            continue
        at = _parse(auto.get("resume_at"))
        if at is not None and at <= now:
            out.append(st)
    return out


def has_wait_note(ledger, rule) -> bool:
    """True when the ledger holds the queue's `wait` decision note for `rule` after the last
    `park` event."""
    park_seq = max((int(e.get("seq") or 0) for e in ledger if e.get("type") == "park"), default=0)
    return any(e.get("type") == "note" and int(e.get("seq") or 0) > park_seq
               and (e.get("decision") or {}).get("kind") == "wait"
               and (e.get("decision") or {}).get("rule") == rule for e in ledger)


def to_dict(auto: Auto) -> dict:
    return asdict(auto)
