"""SPEC-0422: the cost-cap policy. The queue raises a campaign's cap itself while the campaign is
converging (the latest review passed or its score rose, no surviving major / must_fix, no
divergence signal), in steps of `step_fraction` × the profile cap, up to `ceiling_multiplier` ×
the profile cap. Above the ceiling, or when the campaign diverges, it escalates once to the
human. The pre-run reserve is the observed median run cost × a margin (`reserve`), never above
the persona's `max_budget_usd`; the hard check against the ceiling keeps the full budget.

Pure functions, except `settings` (reads the config) and the run-cost statistics
(`refresh` / `load`, `<agentic>/cost_stats.json`). The queue writes the raise, the ledger
event, the decision record and the escalation."""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import math
import os
import re
import statistics
from dataclasses import asdict, dataclass, field

from . import divergence as DV
from . import personas as PS
from . import store

RULE = "cost_cap_policy"
TOPIC_CEILING, TOPIC_DIVERGING = "spend_ceiling", "spend_diverging"
STATS_SCHEMA = "nw.cost_stats.v1"
BACKFILL_DAYS = 14

RESERVE_DEFAULTS = {"multiplier": 1.5, "floor_usd": 0.25, "min_samples": 8, "window": 40}
DEFAULTS = {"enabled": True, "ceiling_multiplier": 3.0, "step_fraction": 0.5,
            "no_review_raises": 1, "reserve": dict(RESERVE_DEFAULTS)}

# the counters the queue keeps on the campaign state
RAISES = "cost_policy_raises"
RAISES_NO_REVIEW = "cost_policy_raises_no_review"

_REVIEW_NOTE = re.compile(r"^review score (\d+)/(\d+) (\w+)")
_REVIEW_PERSONAS = frozenset(PS.REVIEWER_PERSONAS) | {"review_triager"}
_BLOCKING = frozenset({"major", "must_fix"})


# ---- settings ----------------------------------------------------------------------------------

def _num(v, ok) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and ok(v)


def settings(cfg) -> dict:
    """`DEFAULTS` overlaid with `agentic.cost_cap_policy`; a bad value falls back to its
    default, key by key (as `divergence.settings`)."""
    raw = ((cfg or {}).get("agentic") or {}).get("cost_cap_policy")
    raw = raw if isinstance(raw, dict) else {}
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    if isinstance(raw.get("enabled"), bool):
        out["enabled"] = raw["enabled"]
    if _num(raw.get("ceiling_multiplier"), lambda v: v >= 1):
        out["ceiling_multiplier"] = float(raw["ceiling_multiplier"])
    if _num(raw.get("step_fraction"), lambda v: 0 < v <= 10):
        out["step_fraction"] = float(raw["step_fraction"])
    v = raw.get("no_review_raises")
    if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
        out["no_review_raises"] = v
    res = raw.get("reserve") if isinstance(raw.get("reserve"), dict) else {}
    if _num(res.get("multiplier"), lambda v: v > 0):
        out["reserve"]["multiplier"] = float(res["multiplier"])
    if _num(res.get("floor_usd"), lambda v: v >= 0):
        out["reserve"]["floor_usd"] = float(res["floor_usd"])
    for k in ("min_samples", "window"):
        v = res.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 1:
            out["reserve"][k] = v
    return out


def standing(cfg) -> dict:
    """What the conductor brief shows as the effective policy."""
    s = settings(cfg)
    return {k: s[k] for k in ("enabled", "ceiling_multiplier", "step_fraction", "no_review_raises")}


def base_cap(profile) -> float | None:
    v = (profile or {}).get("campaign_cap_usd")
    return float(v) if _num(v, lambda x: x > 0) else None


def ceiling(profile, sett: dict) -> float | None:
    """`ceiling_multiplier` × the profile's `campaign_cap_usd`; None when the policy is off or
    the profile has no cap."""
    base = base_cap(profile)
    if not sett.get("enabled") or base is None:
        return None
    return round(base * float(sett["ceiling_multiplier"]), 2)


def _ceil_cent(x: float) -> float:
    return math.ceil(round(x * 100, 6)) / 100


# ---- run-cost statistics -------------------------------------------------------------------------

def stats_path(cfg):
    return store.agentic_dir(cfg) / "cost_stats.json"


def _empty() -> dict:
    return {"schema": STATS_SCHEMA, "runs": {}, "cursors": {}, "pending": {}}


def load(cfg) -> dict | None:
    """The stored statistics, or None when there are none (or they are unreadable)."""
    try:
        doc = json.loads(stats_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("schema") != STATS_SCHEMA:
        return None
    for k in ("runs", "cursors", "pending"):
        if not isinstance(doc.get(k), dict):
            doc[k] = {}
    return doc


def key(persona, model) -> str:
    return f"{persona}|{model or '?'}"


def counts_run(ev: dict) -> bool:
    """A `run.end` whose cost says what a run of its persona costs: reported (not estimated),
    positive, and not an error, timeout, interruption or transient failure."""
    from .events import _is_transient
    cost = ev.get("cost_usd")
    if not _num(cost, lambda v: v > 0) or ev.get("cost_estimated"):
        return False
    if ev.get("is_error") or ev.get("timed_out") or ev.get("interrupted"):
        return False
    return not _is_transient(ev.get("result_tail") or "")


def _parse_ts(value):
    try:
        ts = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ts.astimezone() if ts.tzinfo is None else ts


def _scan(cfg, doc: dict, window: int, now) -> bool:
    """Read each recent campaign ledger from its byte cursor on; fold new `run.end` costs into
    `doc`. Returns True when `doc` changed."""
    cutoff = now - dt.timedelta(days=BACKFILL_DAYS)
    cursors, pending, runs = doc["cursors"], doc["pending"], doc["runs"]
    changed, seen = False, set()
    for st in store.list_campaigns(cfg, include_terminal=True):
        cid = st.get("id")
        if not cid:
            continue
        if cid not in cursors:
            ts = _parse_ts(st.get("updated_at") or st.get("created_at"))
            if ts is not None and ts < cutoff:
                continue
        seen.add(cid)
        path = store._ledger_path(cfg, cid)
        offset = int(cursors.get(cid) or 0)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size < offset:                 # never re-count a rewritten ledger
            cursors[cid], changed = size, True
            continue
        if size == offset:
            if cid not in cursors:
                cursors[cid], changed = offset, True
            continue
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read(size - offset)
        except OSError:
            continue
        end = chunk.rfind(b"\n")
        if end < 0:
            continue
        starts = pending.setdefault(cid, {})
        for raw in chunk[:end + 1].splitlines():
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == "run.start" and ev.get("run_id"):
                starts[str(ev["run_id"])] = ev.get("model")
            elif ev.get("type") == "run.end":
                model = starts.pop(str(ev.get("run_id")), None)
                if isinstance(ev.get("persona"), str) and counts_run(ev):
                    lst = runs.setdefault(key(ev["persona"], model), [])
                    lst.append(round(float(ev["cost_usd"]), 4))
                    del lst[:-window]
        if not starts:
            pending.pop(cid, None)
        cursors[cid] = offset + end + 1
        changed = True
    for cid in [c for c in cursors if c not in seen]:
        cursors.pop(cid)
        pending.pop(cid, None)
        changed = True
    return changed


def refresh(cfg, *, write: bool = True, now=None) -> dict:
    """The run-cost statistics, brought up to date from the ledgers. The first call (no file)
    backfills from the campaigns of the last `BACKFILL_DAYS` days. `write=False` (the read-only
    `explain`) computes the same in memory and writes nothing."""
    now = now or dt.datetime.now().astimezone()
    window = settings(cfg)["reserve"]["window"]
    if not write:
        doc = load(cfg) or _empty()
        _scan(cfg, doc, window, now)
        return doc
    lock = stats_path(cfg).with_name("cost_stats.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        doc = load(cfg) or _empty()
        if _scan(cfg, doc, window, now) or not stats_path(cfg).is_file():
            from ..export import _atomic_write
            doc["updated_at"] = store.now_iso()
            _atomic_write(stats_path(cfg), json.dumps(doc, ensure_ascii=False) + "\n")
        return doc
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def reserve_rows(stats: dict | None, profile: dict, sett: dict) -> dict:
    """`{persona: {reserve_usd, budget_usd, samples, median_usd}}` for every persona of
    `profile`: `clamp(median × multiplier, floor_usd, max_budget_usd)` over the newest `window`
    runs of the persona's (persona, model), or `max_budget_usd` below `min_samples`."""
    r = sett["reserve"]
    runs = (stats or {}).get("runs") or {}
    out = {}
    for persona, ps in ((profile or {}).get("personas") or {}).items():
        budget = float((ps or {}).get("max_budget_usd") or 0.0)
        costs = [c for c in runs.get(key(persona, (ps or {}).get("model")), [])
                 if _num(c, lambda v: v > 0)][-r["window"]:]
        median = round(statistics.median(costs), 4) if costs else None
        if len(costs) < r["min_samples"]:
            reserve = budget
        else:
            reserve = min(max(median * r["multiplier"], r["floor_usd"]), budget)
        out[persona] = {"reserve_usd": round(reserve, 4), "budget_usd": budget,
                        "samples": len(costs), "median_usd": median}
    return out


def reserves(cfg, profile, *, stats=None, sett=None) -> dict:
    """`{persona: reserve $}` (see `reserve_rows`). `stats` defaults to the stored statistics."""
    sett = sett or settings(cfg)
    stats = stats if stats is not None else load(cfg)
    return {p: row["reserve_usd"] for p, row in reserve_rows(stats, profile, sett).items()}


# ---- convergence ---------------------------------------------------------------------------------

LEDGER_TYPES = ["review_outcome", "note", "finding", "gate", "transition", "park", "resume",
                "run.end"]


def review_outcomes(ledger) -> list[dict]:
    """`[{seq, loop, score, pass_score, outcome, major, must_fix, source}]` in ledger order:
    the structured `review_outcome` events, and before the first of them the queue's legacy
    `review score S/P outcome` notes. A legacy pass counts its `pass over major:` notes as
    majors; a legacy fix counts the reviewers' major / must_fix findings of its round."""
    ledger = sorted(ledger, key=lambda e: int(e.get("seq") or 0))
    first_new = next((int(e.get("seq") or 0) for e in ledger
                      if e.get("type") == "review_outcome"), None)
    out, round_findings = [], {"major": 0, "must_fix": 0}
    for e in ledger:
        t, seq = e.get("type"), int(e.get("seq") or 0)
        if t == "review_outcome":
            out.append({"seq": seq, "loop": e.get("loop"), "score": e.get("score"),
                        "pass_score": e.get("pass_score"), "outcome": e.get("outcome"),
                        "major": int(e.get("major") or 0), "must_fix": int(e.get("must_fix") or 0),
                        "source": "event"})
            continue
        if first_new is not None and seq >= first_new:
            continue
        if t == "finding" and e.get("persona") in _REVIEW_PERSONAS \
                and e.get("severity") in _BLOCKING:
            round_findings[e["severity"]] += 1
        elif t == "note" and e.get("author") == "queue":
            text = str(e.get("text") or "")
            m = _REVIEW_NOTE.match(text)
            if m:
                score, pass_score, outcome = int(m.group(1)), int(m.group(2)), m.group(3)
                legacy = {"seq": seq, "loop": None, "score": score, "pass_score": pass_score,
                          "outcome": outcome, "major": 0, "must_fix": 0, "source": "note"}
                if outcome != "pass":
                    legacy.update(round_findings)
                out.append(legacy)
                round_findings = {"major": 0, "must_fix": 0}
            elif text.startswith("pass over major:") and out and out[-1]["source"] == "note" \
                    and out[-1]["outcome"] == "pass":
                out[-1]["major"] += 1
    return out


def converging(state: dict, ledger, cfg) -> tuple:
    """`(True | False | None, why)`. True when the latest review passed or its score is above
    the previous one, it has no major or must_fix, and `divergence.assess` finds nothing. None
    when there is no review outcome yet."""
    outs = review_outcomes(ledger)
    if not outs:
        return None, "no review yet"
    last, prev = outs[-1], (outs[-2] if len(outs) > 1 else None)
    s, p = last.get("score"), last.get("pass_score")
    passed = last.get("outcome") == "pass"
    rising = prev is not None and _num(s, lambda v: True) and _num(prev.get("score"), lambda v: True) \
        and s > prev["score"]
    fails = []
    if not (passed or rising):
        if prev is None:
            fails.append(f"first review {last.get('outcome')} {s}/{p}")
        elif s == prev.get("score"):
            fails.append(f"score flat {prev.get('score')}→{s} ({last.get('outcome')})")
        else:
            fails.append(f"score fell {prev.get('score')}→{s}")
    if last["major"]:
        fails.append(f"{last['major']} major")
    if last["must_fix"]:
        fails.append(f"{last['must_fix']} must_fix")
    div = DV.assess(DV.rounds(ledger), DV.settings(cfg))
    if div:
        fails.append("diverging: " + ", ".join(x["name"] for x in div["signals"]))
    if fails:
        return False, "; ".join(fails)
    if passed:
        return True, f"latest review passed {s}/{p}"
    return True, f"score rose {prev.get('score')}→{s}"


# ---- the decision ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    action: str                       # "raise" | "escalate" | "off"
    new_cap: float | None = None
    topic: str | None = None
    why: str = ""
    facts: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def decide(state: dict, profile: dict, ledger, cfg, *, blocked_budget, sett=None) -> Decision:
    """What the queue does with a campaign its cap blocks (`blocked_budget`: the blocked run's
    full `max_budget_usd`). Checks, in order: policy off; the hard ceiling (spend + the run's
    budget above `ceiling`) → escalate `spend_ceiling`; not converging → escalate
    `spend_diverging`; no review yet after `no_review_raises` raises → escalate
    `spend_diverging`; else raise to `min(ceiling, cap + max(step × base, budget))`."""
    s = sett or settings(cfg)
    cost = state.get("cost") or {}
    cap, total = cost.get("cap_usd"), float(cost.get("total_usd") or 0.0)
    base = base_cap(profile)
    ceil = ceiling(profile, s)
    budget = float(blocked_budget or 0.0)
    facts = {"spent_usd": round(total, 4), "cap_usd": cap, "base_cap_usd": base,
             "ceiling_usd": ceil, "blocked_budget_usd": budget}
    if not s["enabled"]:
        return Decision("off", why="agentic.cost_cap_policy.enabled is false", facts=facts)
    if cap is None or ceil is None:
        return Decision("off", why="no campaign cap or no profile cap", facts=facts)
    cap = float(cap)
    if total + budget > ceil + 1e-9:
        return Decision("escalate", topic=TOPIC_CEILING, facts=facts, why=(
            f"spent ${total:.2f} + a ${budget:.2f} run would cross the ${ceil:.2f} ceiling "
            f"({s['ceiling_multiplier']:g}× the ${base:.2f} profile cap)"))
    if cap >= ceil - 1e-9:
        return Decision("escalate", topic=TOPIC_CEILING, facts=facts, why=(
            f"the cap ${cap:.2f} is already at or above the ${ceil:.2f} ceiling"))
    conv, why = converging(state, ledger, cfg)
    facts.update(converging=conv, converging_why=why)
    if conv is False:
        return Decision("escalate", topic=TOPIC_DIVERGING, why=f"not converging: {why}",
                        facts=facts)
    counters = state.get("counters") or {}
    if conv is None:
        n = int(counters.get(RAISES_NO_REVIEW) or 0)
        if n >= s["no_review_raises"]:
            return Decision("escalate", topic=TOPIC_DIVERGING, facts=facts,
                            why=f"no review yet after {n} raises")
    new = min(ceil, _ceil_cent(cap + max(s["step_fraction"] * base, budget)))
    if new <= cap + 1e-9:
        return Decision("escalate", topic=TOPIC_CEILING, facts=facts,
                        why=f"no raise left below the ${ceil:.2f} ceiling")
    return Decision("raise", new_cap=new, facts=facts, why=(
        f"{why}; raise ${cap:.2f} → ${new:.2f} (ceiling ${ceil:.2f}, spent ${total:.2f})"))


def escalation_text(state: dict, decision: Decision, outcomes, sett: dict) -> tuple:
    """`(question, options, facts)` of the human-only escalation a `decide` escalation files."""
    f = decision.facts
    cap, ceil, base = f.get("cap_usd") or 0.0, f.get("ceiling_usd") or 0.0, f.get("base_cap_usd") or 0.0
    step = float(sett["step_fraction"]) * base
    to = _ceil_cent(max(float(cap), float(ceil)) + max(step, f.get("blocked_budget_usd") or 0.0)) \
        if decision.topic == TOPIC_CEILING \
        else min(float(ceil), _ceil_cent(float(cap) + max(step, f.get("blocked_budget_usd") or 0.0)))
    label = state.get("spec_id") or (state.get("idea") or {}).get("id") or state.get("id")
    what = "would cross its spend ceiling" if decision.topic == TOPIC_CEILING else "is not converging"
    question = (f"{label} is parked at its cost cap (${f.get('spent_usd', 0):.2f} of "
                f"${float(cap):.2f}) and {what}: {decision.why}. The cost-cap policy "
                f"(SPEC-0422) does not raise it; this is your call.")
    options = (f"raise the cap to ${to:.2f} once",
               "let it finish at the current cap (no more runs)",
               "abandon",
               "rework the spec (split / narrow)")
    scores = ", ".join(f"{o.get('score')}/{o.get('pass_score')} {o.get('outcome')}"
                       for o in outcomes[-3:]) or "none"
    last = outcomes[-1] if outcomes else {}
    facts = [marker(f.get("spent_usd"), cap),
             f"topic: {decision.topic}",
             f"spent ${f.get('spent_usd', 0):.2f}, cap ${float(cap):.2f}, ceiling ${float(ceil):.2f} "
             f"({sett['ceiling_multiplier']:g}× the ${float(base):.2f} profile cap)",
             f"last reviews: {scores}",
             f"latest review majors {last.get('major', 0)}, must_fix {last.get('must_fix', 0)}",
             f"converging: {f.get('converging')} ({f.get('converging_why') or '-'})"]
    return question, options, facts


def marker(spent, cap) -> str:
    """The fact that ties a cost escalation to one situation (spent, cap): the queue files at
    most one per situation."""
    return f"cost_cap_policy: spent ${float(spent or 0):.2f} cap ${float(cap or 0):.2f}"


# ---- explain (read-only) -----------------------------------------------------------------------------

EXPLAIN_SCHEMA = "nw.queue.cost_policy.v1"
_MAY_COST = re.compile(r"may cost \$([\d.]+)")


def _as_of(st: dict, ledger, seq: int) -> tuple:
    """`(state, ledger, park event or None)` as the campaign stood at ledger `seq`: spend and
    cap from the newest `cost` / raising `resume` / `cap_raised` event up to it."""
    led = [e for e in ledger if int(e.get("seq") or 0) <= seq]
    total, cap = 0.0, (st.get("cost") or {}).get("cap_usd")
    first_cap = None
    for e in led:
        t = e.get("type")
        if t == "cost":
            total = float(e.get("total_usd") or 0.0)
            if e.get("cap_usd") is not None:
                cap = e["cap_usd"]
                first_cap = cap if first_cap is None else first_cap
        elif t == "resume" and e.get("cap_usd") is not None:
            cap = e["cap_usd"]
        elif t == "cap_raised" and e.get("to") is not None:
            cap = e["to"]
    at = next((e for e in led if int(e.get("seq") or 0) == seq), None)
    park = at if at is not None and at.get("type") == "park" else None
    state = {**st, "cost": {**(st.get("cost") or {}), "total_usd": total, "cap_usd": cap},
             "counters": {**(st.get("counters") or {}), RAISES: 0, RAISES_NO_REVIEW: 0},
             "state": (park or {}).get("resume_to") or st.get("state"), "park": None}
    return state, led, park


def _default_ids(cfg) -> list[str]:
    from . import queue as Q
    out = []
    for st in store.list_campaigns(cfg):
        park = st.get("park") or {}
        if st.get("state") in Q.ACTIVE_STATES or (st.get("state") == "PARKED" and "cost_cap" in (
                park.get("reason"), park.get("escalated_from"))):
            out.append(st["id"])
    return out


def explain(cfg, campaigns=(), *, at_seq=None) -> dict:
    """`nw.queue.cost_policy.v1`: per campaign (default: every active one and every one parked
    on its cost cap) the spend, cap, base cap, ceiling, reserves with sample counts, the review
    outcomes used, `converging` and the decision `decide` takes at its next cost block.
    `at_seq` replays one campaign as it stood at that ledger seq. Writes nothing."""
    from . import project as P
    from . import queue as Q
    from .profiles import persona_settings, resolve_profile
    sett = settings(cfg)
    stats = refresh(cfg, write=False)
    ids = list(campaigns) or _default_ids(cfg)
    rows = []
    for cid in ids:
        try:
            st = store.read_state(cfg, cid)
            profile = resolve_profile(cfg, st.get("profile"),
                                      project_root=(st.get("repo") or {}).get("root"))
            ledger = store.read_ledger(cfg, cid)
        except (ValueError, OSError) as e:
            rows.append({"campaign": cid, "error": str(e)})
            continue
        park_ev = None
        if at_seq is not None:
            probe, ledger, park_ev = _as_of(st, ledger, int(at_seq))
        else:
            probe = dict(st)
            if st.get("state") == "PARKED":
                probe.update(state=(st.get("park") or {}).get("resume_to") or "IMPLEMENTING",
                             park=None)
        try:
            pcfg = P.project_config(cfg, st.get("project"))
        except ValueError:
            pcfg = None
        rrows = reserve_rows(stats, profile, sett)
        ceil = ceiling(profile, sett)
        on = sett["enabled"]
        step = Q.plan_step(probe, profile, pcfg,
                           reserves={p: r["reserve_usd"] for p, r in rrows.items()} if on else None,
                           ceiling=ceil)
        blocked = step.kind == "park" and step.reason == "cost_cap"
        detail = step.detail if blocked else None
        if park_ev is not None and park_ev.get("reason") == "cost_cap":
            m = _MAY_COST.search(str(park_ev.get("detail") or ""))
            budget = float(m.group(1)) if m else None
            detail = f"(then) {park_ev.get('detail')}"
        else:
            budget = None
        if budget is None:
            names = list(step.personas) or ([step.persona] if step.persona else ["implementer"])
            try:
                budget = sum(float(persona_settings(profile, p).get("max_budget_usd") or 0.0)
                             for p in names)
            except ValueError:
                budget = 0.0
        decision = decide(probe, profile, ledger, cfg, blocked_budget=budget, sett=sett)
        conv, why = converging(probe, ledger, cfg)
        cost = probe.get("cost") or {}
        rows.append({
            "campaign": cid, "label": Q.campaign_label(st), "state": st.get("state"),
            "park_reason": (st.get("park") or {}).get("reason"), "profile": st.get("profile"),
            "at_seq": at_seq,
            "spent_usd": round(float(cost.get("total_usd") or 0.0), 4),
            "cap_usd": cost.get("cap_usd"), "base_cap_usd": base_cap(profile),
            "ceiling_usd": ceil,
            "reserves": rrows,
            "review_outcomes": review_outcomes(ledger)[-5:],
            "converging": conv, "converging_why": why,
            "next_step": step.kind if not blocked else f"park cost_cap ({step.persona})",
            "blocked_now": blocked or (park_ev or {}).get("reason") == "cost_cap",
            "blocked_detail": detail,
            "next_budget_usd": budget,
            "raises": int((probe.get("counters") or {}).get(RAISES) or 0),
            "decision": decision.to_dict(),
        })
    return {"schema": EXPLAIN_SCHEMA, "policy": sett, "campaigns": rows}
