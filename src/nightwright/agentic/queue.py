"""Queue state machine and loop (SPEC-0176): the engine of the agentic workflow.

`plan_step` is a pure function from campaign state (+ profile + project config) to the one
step the campaign needs next; `tick` executes those steps — agent steps (implement, gate,
triage, review) concurrently in a thread pool whose steps carry across ticks (SPEC-0213),
repository steps (open, close) serially — and applies every result on the main thread under the campaign lock, so `state.json` and `ledger.jsonl`
can never disagree: every transition goes through `_transition`, which records the event in
the same locked section that writes the state.

Invariant (SPEC-0169): Python decides every transition; personas only produce commits and one
structured verdict. Gates are truth; verdicts steer. Before every spawn the campaign cap is
checked pessimistically (`total + persona budget > cap` ⇒ PARKED). Humans act only on
`PARKED` items (`park` / `resume` / `abandon`)."""
from __future__ import annotations

import copy
import errno
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from .. import specs as SP
from ..rules import INTERNAL_PROJECT, registered_projects
from ..spec_scope import idea_project
from ..specmeta import split_frontmatter
from . import autopark as AP
from . import backend_preflight as BP
from . import credentials as CR
from . import runner_jobs as RNJ
from . import cadence as CAD
from . import close as CL
from . import collator as CO
from . import sessions as SS
from . import cost_policy as CP
from . import decisions as DEC
from . import divergence as DV
from . import flaky as FL
from . import followups as FU
from . import gate_slots
from . import gates as G
from . import gitops as git
from . import lease as L
from . import packet as PK
from . import persona_gate as PG
from . import personas as PS
from . import project as P
from . import redmain as RM
from . import remote_jobs as RJ
from . import remote_personas as RPS
from . import sentinel as SN
from . import spec_review as SR
from . import spec_write as SW
from . import schemas as SC
from . import disk_clean
from . import store
from . import tiers as TR
from . import workers
from .codewatch import (_CODE_PATHS, _changed_paths, _git_out, code_changes,  # noqa: F401
                        code_checkout)
from .gitops import GitError
from .profiles import (SEVERITIES, cap_flag, persona_settings, profile_sha256, resolve_profile, review_params,
                       reviewers_of, select_profile, triager_enabled)
from .events import settings as events_settings
from .events import temp_pressure as events_temp_pressure
from . import runproc as RP
from .runner import DETACHED, RunHooks, RunResult, rebuild_request, resolve_runner, select_runner

STATES = ("QUEUED", "WAITING", "OPENING", "SPEC_WRITING", "SPEC_REVIEW", "IMPLEMENTING", "GATING", "TRIAGING",
          "REVIEWING", "DIVERGENCE_TRIAGE", "CLOSING", "PARKED", "MERGED", "HANDED_OFF", "ABANDONED")
# Slots are for campaigns that are open and delivering (ACTIVE_STATES). QUEUED = the backlog,
# never started. WAITING (SPEC-0200) = started (worktree, commits, counters, brief) and ready to
# continue, but no slot was free; it takes the next free slot ahead of the backlog and resumes
# at `waiting.resume_to` without re-opening. PARKED = needs a decision; holds no slot.
TERMINAL_STATES = store.TERMINAL_STATES
ACTIVE_STATES = frozenset({"OPENING", "SPEC_WRITING", "SPEC_REVIEW", "IMPLEMENTING", "GATING", "TRIAGING", "REVIEWING",
                           "DIVERGENCE_TRIAGE", "CLOSING"})
# SPEC-0394: GATING costs no Claude spend (it waits on / runs box gates, whose concurrency is
# bounded by the gate slot pools, gate_slots.py), so it does not count against the profile
# `slots`. Every other active state does.
SLOT_STATES = ACTIVE_STATES - {"GATING"}
RESUMABLE_STATES = ("SPEC_WRITING", "SPEC_REVIEW", "IMPLEMENTING", "GATING", "REVIEWING",
                    "DIVERGENCE_TRIAGE", "CLOSING", "QUEUED")
PARK_REASONS = frozenset({
    "cost_cap", "loop_exhausted", "implementer_blocked", "triage_escalated", "open_failed",
    "rebase_conflict", "main_dirty", "main_not_on_base", "base_moving", "gh_failed", "human",
    "review_escalated", "config_invalid", "spec_writer_unavailable", "spec_write_failed",
    "spec_review_unavailable", "human_decision", "spec_review_exhausted",
    "gates_failed", "worktree_dirty",                  # SPEC-0233: merge campaigns
    "not_docs_only",                                   # SPEC-0234: --docs-only diff left the allowlist
    "diverging",                                       # SPEC-0247: the fix loop is not converging
    "diverging_rework",                               # SPEC-0462: the triager says the spec is the problem
    "regenerate_failed",                               # SPEC-0255: close-time regenerate failed/dirtied
    "gate_timeout",                                    # SPEC-0311: gates keep timing out under load
    "gate_oom",                                        # SPEC-0515: a gate OOMs even with 1.5x memory
    "restart",                                         # SPEC-0375: parked across `nw queue restart`
    "fixforward_weakens", "revert_conflict",           # SPEC-0420: red-base fix campaigns
    "spec_status",                                     # SPEC-0468: only spec-dod failed at close
    "round_budget",                                    # SPEC-0468: cursor review rounds are spent
    "apply_error",                                     # SPEC-0484: applying a result raised
})
# SPEC-0222: who a park waits for. The human only for an off-base main checkout and explicit
# escalations (`human_decision`, SPEC-0232); everything else is the conductor's. SPEC-0547:
# `main_dirty` is the conductor's (it plans and escalates; the human clears the checkout).
PARK_OWNERS = {"main_not_on_base": "human", "human_decision": "human",
               "restart": "human"}            # SPEC-0375: only a crashed restart leaves one


def park_owner(reason) -> str:
    return PARK_OWNERS.get(reason or "", "conductor")


# How many times in a row the implementer may report `blocked` before the campaign parks.
BLOCKED_STREAK_MAX = 2
# SPEC-0214: the parks whose resume refreshes the idea snapshot and the spec-phase retry budgets.
SPEC_PHASE_PARKS = frozenset({"spec_write_failed", "spec_review_exhausted",
                              "spec_review_unavailable"})
# SPEC-0215: rejected spec writes retried before the campaign parks `spec_write_failed`.
SPEC_WRITE_RETRY_MAX = 1
# SPEC-0569: re-reserves of a spec number another worktree or ref took (own counter
# `spec_number_retries`) before the campaign parks `spec_write_failed`.
SPEC_NUMBER_RETRY_MAX = 2
# SPEC-0214: failed spec-review runs retried before the campaign parks `spec_review_exhausted`.
SPEC_REVIEW_RETRY_MAX = 1
# Malformed triage verdicts tolerated per loop before the triage is treated as `escalate`.
TRIAGE_RETRY_MAX = 1
# `retry_gate` dispositions honored per loop before they are treated as `fix`.
GATE_RETRY_MAX = 1
# SPEC-0311: consecutive timeout-only gate runs re-gated (serialized) before `gate_timeout`.
TIMEOUT_REGATE_MAX = 2
# SPEC-0515: an OOM-killed gate run is re-gated once with this much more memory, then parks
# `gate_oom`; a node failure (or a preemption that reached the coordinator) re-gates once on
# the worker, then once locally (SPEC-0338 fallback, placement reason `node_fail`).
OOM_RETRY_MAX = 1
MEM_BOOST = 1.5
NODE_FAIL_REGATE_MAX = 1
# SPEC-0507 + SPEC-0523: the longest an exclusive re-gate marker holds other gate starts,
# counted from its `entered_at`, pending or running: past it a pending one starts as a normal
# gate and a running one no longer counts as exclusive. Exclusivity is advisory: the box is
# shared with other jobs anyway, so "alone" cannot be bought by stalling the queue.
EXCLUSIVE_WAIT_MAX_S = 300
# Malformed conformance-review verdicts tolerated per loop before the review escalates.
REVIEW_RETRY_MAX = 1
# SPEC-0221: failed conflict-resolver runs per entry into CLOSING before the rebase_conflict park.
MAX_RESOLVE_ATTEMPTS = 2
# SPEC-0303: all resolver runs (verified or not) per entry into CLOSING; a verified run that the
# base overtakes four times parks `base_moving`, not `rebase_conflict`.
MAX_RESOLVE_RUNS = 4
_STOP_GRACE_S = 30
# With steps in flight, the loop never waits less than this between ticks (no busy spin).
_MIN_INFLIGHT_WAIT_S = 0.1
PRIORITY_MIN, PRIORITY_MAX, PRIORITY_DEFAULT = 1, 5, 3
# SPEC-0233: a merge request is short and usually unblocks a person, so it goes first.
MERGE_PRIORITY_DEFAULT = 2
# A dependency counts as satisfied once its spec reached one of these in the base checkout.
DEPENDENCY_SATISFIED = frozenset({"done", "superseded"})


# ---- pure planning ---------------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    """What a campaign needs next. `kind` ∈ open | write_spec | spec_review | implement | gate | triage | review |
    close | resolve | park | none; `park` steps carry `reason` / `detail` / `resume_to`."""
    kind: str
    persona: str | None = None
    reason: str | None = None
    detail: str | None = None
    resume_to: str | None = None
    # SPEC-0201: a `review` step names every pending reviewer; `decide_reviews` fills the rest.
    personas: tuple = ()
    missing: tuple = ()
    escalated: tuple = ()
    combined: dict | None = None
    by: str | None = None                  # SPEC-0232: who requested a deferred park
    budget: float | None = None            # SPEC-0302: the run a `cost_cap` park blocked
    held: tuple = ()                       # SPEC-0345: fan-out members the persona cap held


def _resolver_allowance(project_cfg: dict | None, profile: dict) -> float:
    """SPEC-0303: the spend over `cost.cap_usd` a close may put into resolver runs:
    `merge.resolver_allowance_usd`, else the resolver's budget × `MAX_RESOLVE_ATTEMPTS`."""
    configured = ((project_cfg or {}).get("merge") or {}).get("resolver_allowance_usd")
    if configured is not None:
        return float(configured)
    return float(persona_settings(profile, "conflict_resolver").get("max_budget_usd") or 0.0) \
        * MAX_RESOLVE_ATTEMPTS


def _cap_blocks(state: dict, profile: dict, persona, allowance: float = 0.0, reserves=None,
                ceiling=None) -> tuple[str, float] | tuple[None, None]:
    """Cap check before a run. `persona` is a name or a list of names (a fan-out is checked as
    the sum). Returns `(park detail, the blocked runs' full budget)` or `(None, None)`.

    Without `reserves` / `ceiling` it is pessimistic: a run may spend up to its budget, so
    never start one that could cross the campaign cap (plus `allowance`, SPEC-0303: the cap
    itself is never changed). SPEC-0422: the soft check against the cap uses `reserves`
    (persona -> $, the observed median × a margin) and is skipped once the cap is at or above
    `ceiling`; the hard check keeps the full budgets against `max(ceiling, cap)`."""
    cost = state.get("cost") or {}
    cap = cost.get("cap_usd")
    if cap is None:
        return None, None
    soft_cap = float(cap) + allowance
    names = [persona] if isinstance(persona, str) else list(persona)
    budgets = [float(persona_settings(profile, p).get("max_budget_usd") or 0.0) for p in names]
    budget = sum(budgets)
    total = _known_spend(cost)
    who = f"a {names[0]} run" if len(names) == 1 else f"{', '.join(names)} runs"
    close = f" (including a ${allowance:.2f} close allowance)" if allowance else ""
    if ceiling is None or float(cap) < float(ceiling):
        reserve = budget if reserves is None else sum(
            float(reserves.get(p, b)) for p, b in zip(names, budgets))
        if total + reserve > soft_cap:
            return (f"spent ${total:.2f}; {who} may cost ${budget:.2f}; cap ${soft_cap:.2f}"
                    + close + (f" (reserve ${reserve:.2f} of budget ${budget:.2f})"
                               if reserves is not None else ""), budget)
    if ceiling is not None:
        hard = max(float(ceiling), float(cap)) + allowance
        if total + budget > hard:
            return (f"spent ${total:.2f}; {who} may cost ${budget:.2f}; spend ceiling "
                    f"${hard:.2f}" + close, budget)
    return None, None


def _valid_verdicts(state: dict, profile: dict) -> dict:
    """`{persona: stored verdict}` for the reviewers with a valid verdict of the current loop
    (SPEC-0201), in profile order. A stale-loop entry, or an old-shape `last_review` without
    `verdicts`, counts as none."""
    stored = (state.get("last_review") or {}).get("verdicts") or {}
    loop = int((state.get("counters") or {}).get("loops", 0))
    return {p: stored[p] for p in reviewers_of(profile)
            if isinstance(stored.get(p), dict) and stored[p].get("loop") == loop}


def _scoped_verdicts(state: dict, profile: dict) -> dict:
    """`_valid_verdicts`, its findings marked `scope` / `tie` by this verdict set's applied
    collation (SPEC-0555); unchanged with the flag off or without one."""
    valid = _valid_verdicts(state, profile)
    try:
        return CO.scoped(state, valid)
    except Exception:                    # noqa: BLE001 — a bad collation is today's routing
        return valid


def _pending_reviewers(state: dict, profile: dict) -> list:
    """Reviewers without a valid current-loop verdict; the full list when none is pending
    (a review is never skipped)."""
    valid = _valid_verdicts(state, profile)
    return [p for p in reviewers_of(profile) if p not in valid] or reviewers_of(profile)


def campaign_label(state: dict) -> str:
    """What a human sees for a campaign: its spec id, `merge:<branch>` for a merge request
    (SPEC-0233), else its idea id."""
    if state.get("spec_id"):
        return str(state["spec_id"])
    if state.get("kind") == "merge":
        return f"merge:{(state.get('merge') or {}).get('branch') or (state.get('repo') or {}).get('branch') or '?'}"
    if state.get("kind") in RM.RED_KINDS:                       # SPEC-0420
        return f"{state['kind']}:{state.get('project') or '?'}#{state.get('red_episode') or '?'}"
    return str((state.get("idea") or {}).get("id") or "?")


def plan_step(state: dict, profile: dict, project_cfg: dict | None = None, *, reserves=None,
              ceiling=None) -> Step:
    """Pure: the next step for `state`. Never touches disk. `reserves` / `ceiling` (SPEC-0422,
    the tick's cost-cap policy) go to every `_cap_blocks` check; None keeps the old checks."""
    cap_kw = {"reserves": reserves, "ceiling": ceiling}
    st = state.get("state")
    if state.get("park_requested") and st not in TERMINAL_STATES and st != "PARKED":
        req = state["park_requested"]
        return Step("park", reason="human_decision" if req.get("reason") == "human_decision"
                    else "human", detail=req.get("detail") or req.get("reason"),
                    resume_to=_resume_target(st), by=req.get("by"))
    if st in TERMINAL_STATES or st in ("PARKED", "QUEUED"):
        return Step("none")
    if st == "OPENING":
        return Step("open")
    if state.get("kind") in ("merge", "revert") and st != "CLOSING":
        # SPEC-0233: open goes straight to CLOSING; no persona ever runs for a merge request
        # (SPEC-0420: nor for a revert campaign, whose open runs the revert)
        return Step("park", reason="human", detail=f"{state['kind']} campaign in {st}",
                    resume_to="CLOSING")
    if st == "SPEC_WRITING" and state.get("kind") == "split":
        # SPEC-0290: checked before the `spec.id` park below (a split campaign sets `spec.id`)
        parts = (state.get("split") or {}).get("parts") or []
        if parts and all(p.get("spec_id") for p in parts):
            return Step("finalize_split")
        blocked, budget = _cap_blocks(state, profile, "spec_writer", **cap_kw)
        if blocked:
            return Step("park", persona="spec_writer", reason="cost_cap", detail=blocked,
                        resume_to=st, budget=budget)
        return Step("write_spec", persona="spec_writer")
    if st == "SPEC_WRITING":
        # SPEC-0215: the spec writer writes (no spec yet) or revises (findings in the brief)
        if (state.get("spec") or {}).get("id") and not state.get("pending_brief"):
            return Step("park", reason="spec_review_unavailable", resume_to="SPEC_WRITING",
                        detail="nothing to revise; resume with --note to have the writer revise")
        blocked, budget = _cap_blocks(state, profile, "spec_writer", **cap_kw)
        if blocked:
            return Step("park", persona="spec_writer", reason="cost_cap", detail=blocked,
                        resume_to=st, budget=budget)
        return Step("write_spec", persona="spec_writer")
    if st == "SPEC_REVIEW":                 # SPEC-0214
        blocked, budget = _cap_blocks(state, profile, "spec_reviewer", **cap_kw)
        if blocked:
            return Step("park", persona="spec_reviewer", reason="cost_cap", detail=blocked,
                        resume_to=st, budget=budget)
        return Step("spec_review", persona="spec_reviewer")
    if st == "GATING":
        return Step("gate")
    if st == "CLOSING":
        if (state.get("closing") or {}).get("resolve"):         # SPEC-0221
            blocked, budget = _cap_blocks(state, profile, "conflict_resolver",
                                          _resolver_allowance(project_cfg, profile), **cap_kw)
            if blocked:
                return Step("park", persona="conflict_resolver", reason="cost_cap",
                            detail=blocked, resume_to=st, budget=budget)
            return Step("resolve", persona="conflict_resolver")
        return Step("close")
    if st == "REVIEWING":
        reviewers = reviewers_of(profile)
        valid_now = _valid_verdicts(state, profile)
        if len(valid_now) == len(reviewers) and CO.pending(state, valid_now):
            # SPEC-0555: the collation of this verdict set; the cap was checked when it became
            # pending
            return Step("review", persona=CO.NAME, personas=(CO.NAME,))
        if len(valid_now) == len(reviewers) \
                and (state.get("last_review") or {}).get("triage") == "pending":
            # SPEC-0206: the one exception to "a review is never skipped"; the cap was checked
            # when `triage` became pending
            return Step("review", persona="review_triager", personas=("review_triager",))
        if PK.marshal_due(state):           # SPEC-0554: the packet pre-step of this loop
            return Step("review", persona=PK.MARSHAL, personas=(PK.MARSHAL,))
        pending = _pending_reviewers(state, profile)
        blocked, budget = _cap_blocks(state, profile, pending, **cap_kw)
        if blocked:
            return Step("park", persona=pending[0], personas=tuple(pending), reason="cost_cap",
                        detail=blocked, resume_to=st, budget=budget)
        return Step("review", persona=pending[0], personas=tuple(pending))
    if st == "DIVERGENCE_TRIAGE":                    # SPEC-0462
        blocked, budget = _cap_blocks(state, profile, "divergence_triager", **cap_kw)
        if blocked:
            return Step("park", persona="divergence_triager", reason="cost_cap", detail=blocked,
                        resume_to=st, budget=budget)
        return Step("divergence", persona="divergence_triager")
    persona, kind = ("implementer", "implement") if st == "IMPLEMENTING" \
        else ("gate_triager", "triage")
    blocked, budget = _cap_blocks(state, profile, persona, **cap_kw)
    if blocked:
        return Step("park", persona=persona, reason="cost_cap", detail=blocked, resume_to=st,
                    budget=budget)
    return Step(kind, persona=persona)


class _ReviewGroup:
    """One campaign's reviewer fan-out inside a tick (SPEC-0201): the per-persona results
    (prepared runs fill in after the pool finishes), applied once, in profile order."""

    def __init__(self, state: dict, step: Step):
        self.state, self.step = state, step
        self.results: dict = {}          # persona -> StepResult | Exception
        self.meta: dict = {}             # persona -> (run_id, budget) of prepared runs

    def __bool__(self):
        return bool(self.results or self.meta)


def slot_ids(rows, flying=()) -> set:
    """Ids occupying a profile slot (SPEC-0394): campaigns in a SLOT_STATES state plus
    in-flight ids, except those whose campaign is GATING."""
    gating = {r["id"] for r in rows if r.get("state") == "GATING"}
    return {r["id"] for r in rows if r.get("state") in SLOT_STATES} | (set(flying) - gating)


def _active_count(cfg) -> int:
    return sum(1 for s in store.list_campaigns(cfg) if s.get("state") in SLOT_STATES)


def _enter_slot_or_wait(cfg, st: dict, to: str, reason: str, *, by="tick", busy=None,
                        slots=None, extra_events=()) -> dict:
    """Move a started campaign to `to`, or to WAITING when `to` needs a slot and none is free
    (SPEC-0200). `busy`/`slots` default to the live counts."""
    if to in SLOT_STATES:
        busy = _active_count(cfg) if busy is None else busy
        slots = slots_for(cfg) if slots is None else slots
        if busy >= slots:
            st["waiting"] = {"resume_to": to, "since": store.now_iso(), "reason": reason}
            return _transition(cfg, st, "WAITING", f"{reason}_waiting", by=by,
                               extra_events=extra_events)
    st.pop("waiting", None)
    return _transition(cfg, st, to, reason, by=by, extra_events=extra_events)


def _resume_target(st: str) -> str:
    """Where a human park on an active item resumes to: the step that was interrupted
    (OPENING restarts from QUEUED; TRIAGING re-runs its gates; REVIEWING re-runs the review)."""
    return {"OPENING": "QUEUED", "TRIAGING": "GATING"}.get(st, st if st in RESUMABLE_STATES
                                                            else "IMPLEMENTING")


DIVERGENCE_EXTRA_MAX = 1          # SPEC-0462: one extra implement round per campaign
DIVERGENCE_RETRY_MAX = 1
_DIVERGENCE_PICKS = ("extra_round", "rework", "escalate")


def decide_divergence(state: dict, verdict, verdict_ok: bool) -> tuple[str, str]:
    """`(action, detail)` for one divergence-triager verdict (SPEC-0462).

    `action` is `extra_round`, `rework`, `escalate`, or `retry`. A second extra round, and a
    second unusable verdict, are both an escalation. The counter is the caller's."""
    counters = state.get("counters") or {}
    retries = int(counters.get("divergence_retries") or 0)
    extras = int(counters.get("divergence_extras") or 0)
    pick = verdict.get("pick") if isinstance(verdict, dict) else None
    why = str(verdict.get("why") or "").strip() if isinstance(verdict, dict) else ""
    if not verdict_ok or pick not in _DIVERGENCE_PICKS:
        if retries < DIVERGENCE_RETRY_MAX:
            return "retry", "the divergence triager returned no usable verdict"
        return "escalate", "the divergence triager returned no usable verdict twice"
    if pick == "extra_round":
        if extras >= DIVERGENCE_EXTRA_MAX:
            cap = "the one extra implement round was already used"
            return "escalate", f"{why} — {cap}" if why else cap
        return "extra_round", why or "one more implement round"
    if pick == "rework":
        return "rework", why or "the spec is the problem"
    return "escalate", why or "the divergence triager escalated"


def decide_triage(state: dict, profile: dict, verdict: dict | None, verdict_ok: bool) -> Step:
    """Pure: the transition a gate-triager verdict implies, given the loop counters.

    - malformed / missing verdict: one retry per loop (stay in TRIAGING), then escalate;
    - `fix`: loops += 1; beyond `loop_max` ⇒ loop_exhausted, else back to IMPLEMENTING;
    - `retry_gate`: once per loop ⇒ GATING, else treated as `fix`;
    - `waive`: only when every failing gate is optional ⇒ CLOSING, else escalate;
    - `escalate` ⇒ PARKED(triage_escalated)."""
    counters = state.get("counters") or {}
    loop_max = int(profile.get("loop_max") or 3)
    if not verdict_ok or not isinstance(verdict, dict):
        if int(counters.get("triage_retries", 0)) < TRIAGE_RETRY_MAX:
            return Step("none", reason="triage_retry")
        return Step("park", reason="triage_escalated",
                    detail="the gate triager returned no usable verdict twice", resume_to="GATING")
    disp = verdict.get("disposition")
    brief = (verdict.get("fix_brief") or "").strip()
    if disp == "retry_gate":
        if int(counters.get("gate_retries", 0)) < GATE_RETRY_MAX:
            return Step("gate", reason="retry_gate")
        disp = "fix"
    if disp == "waive":
        failing = [r for r in ((state.get("last_gate") or {}).get("results") or [])
                   if not r.get("ok")]
        if failing and all(not r.get("required") for r in failing):
            return Step("close", reason="waived")
        return Step("park", reason="triage_escalated",
                    detail="waive requested but a required gate is failing: " + brief,
                    resume_to="GATING")
    if disp == "escalate":
        return Step("park", reason="triage_escalated", detail=brief or "triager escalated",
                    resume_to="GATING")
    # fix (and anything unexpected the schema let through)
    loops = int(counters.get("loops", 0)) + 1
    if loops > loop_max:
        return Step("park", reason="loop_exhausted",
                    detail=f"{loops - 1}/{loop_max} implement loops used; still failing gates: "
                           + brief, resume_to="IMPLEMENTING")
    return Step("implement", reason="triage_fix")


def timeout_only(run_payload: dict) -> bool:
    """Pure (SPEC-0311): whether a failed gate run failed only because required gates timed out.
    True iff some required result failed, every failing required result `timed_out`, and none
    was `interrupted`; optional failing gates are ignored, as `GateRun.ok` ignores them."""
    failing = [r for r in (run_payload or {}).get("results") or []
               if not r.get("ok") and r.get("required", True)]
    return bool(failing) and all(r.get("timed_out") and not r.get("interrupted") for r in failing)


def infra_only(run_payload: dict) -> bool:
    """Pure (SPEC-0515): whether a failed gate run failed only for infra reasons: some required
    result failed and every failing required result carries an `infra` code (an OOM kill,
    Slurm's time limit, a node failure, a preemption). Such a run is never triaged."""
    failing = [r for r in (run_payload or {}).get("results") or []
               if not r.get("ok") and r.get("required", True)]
    return bool(failing) and all(r.get("infra") in G.INFRA_CODES for r in failing)


def _infra_gates(run_payload: dict, code: str | None = None) -> list:
    return [r for r in (run_payload or {}).get("results") or []
            if not r.get("ok") and r.get("required", True) and r.get("infra")
            and (code is None or r.get("infra") == code)]


def suggested_mem_mb(mem_mb: int) -> int:
    """SPEC-0515: `mem_mb` x `MEM_BOOST`, rounded up to 256 MB (the boosted request, and the
    value a `gate_oom` park suggests after the boosted run OOMed too)."""
    return int(math.ceil(max(1, int(mem_mb)) * MEM_BOOST / 256.0) * 256)


def decide_gate_infra(state: dict, mem_mb: int | None = None) -> Step:
    """Pure (SPEC-0515): the transition for an `infra_only` gate run (`state["last_gate"]`).
    `oom` first: re-gate with `MEM_BOOST` x memory while `oom_retries < OOM_RETRY_MAX`, else
    park `gate_oom` naming the gate, the request it outgrew (`mem_mb`, the boosted request)
    and a suggested value. `slurm_timeout` next: `Step("gate", reason="slurm_timeout")`, which
    the caller hands to `decide_gate_timeout` (nw's own gate-timeout path). `node_fail` /
    `preempted`: re-gate on the worker while `node_fail_regates < NODE_FAIL_REGATE_MAX`, then
    `gate_node_fail_local` (the caller runs the next gate locally)."""
    last = state.get("last_gate") or {}
    counters = state.get("counters") or {}
    oom = _infra_gates(last, "oom")
    if oom:
        if int(counters.get("oom_retries") or 0) < OOM_RETRY_MAX:
            return Step("gate", reason="gate_oom_regate")
        names = ", ".join(str(r.get("name")) for r in oom)
        used = f"> {int(mem_mb)} MB" if mem_mb else "more memory than its request"
        tip = f"suggested: {suggested_mem_mb(mem_mb)}M; " if mem_mb else ""
        return Step("park", reason="gate_oom", resume_to="GATING",
                    detail=f"gate {names} used {used} (OOM-killed again after a "
                           f"{MEM_BOOST:g}x memory re-gate); raise resources.mem in "
                           f".nw/project.yaml ({tip}`nw gate stats` shows measured peaks)")
    if _infra_gates(last, "slurm_timeout"):
        return Step("gate", reason="slurm_timeout")
    if int(counters.get("node_fail_regates") or 0) < NODE_FAIL_REGATE_MAX:
        return Step("gate", reason="gate_node_fail_regate")
    return Step("gate", reason="gate_node_fail_local")


def _timed_out_gates(run_payload: dict) -> list:
    return [r for r in (run_payload or {}).get("results") or []
            if not r.get("ok") and r.get("required", True) and r.get("timed_out")]


def decide_gate_timeout(state: dict, timeouts: dict | None = None) -> Step:
    """Pure (SPEC-0311): the transition for a timeout-only gate run. Re-gate while
    `timeout_regates < TIMEOUT_REGATE_MAX`, else park `gate_timeout` (resume re-gates).
    `timeouts` maps a gate name to its configured `timeout_s` (the payload does not carry it)."""
    used = int((state.get("counters") or {}).get("timeout_regates", 0))
    if used < TIMEOUT_REGATE_MAX:
        return Step("gate", reason="gate_timeout_regate")
    names = ", ".join(_timed_out_names(state.get("last_gate") or {}, timeouts)) or "a gate"
    limit = next(((timeouts or {}).get(r.get("name")) for r in _timed_out_gates(state.get("last_gate") or {})
                  if (timeouts or {}).get(r.get("name"))), None)
    after = f" after {limit}s" if limit else ""
    return Step("park", reason="gate_timeout", resume_to="GATING",
                detail=f"{used} gate timeout re-gates used; {names} still times out{after}; "
                       "likely machine load or a gate whose timeout_s is too low")


def _timed_out_names(run_payload: dict, timeouts: dict | None = None) -> list:
    return [f"{r.get('name')} ({timeouts[r.get('name')]}s)" if (timeouts or {}).get(r.get("name"))
            else str(r.get("name")) for r in _timed_out_gates(run_payload)]


def _gate_mem_mb(ctx, state: dict) -> int | None:
    """SPEC-0515: the memory request (MB) the last loop gate run was sent with: the campaign's
    loop gates' job request, times `state["mem_boost"]` once a re-gate boosted it. None when
    the project config is unreadable."""
    from . import gate_resources as GR
    try:
        pcfg = ctx.project(state)
        worktree = (state.get("repo") or {}).get("worktree")
        if worktree:
            pcfg, _ = P.campaign_gate_config(ctx.cfg, pcfg, worktree)
        mem = GR.job_request(P.gates_for(pcfg, "loop"), ctx.cfg, time_s=0)["mem_mb"]
    except (ValueError, OSError, KeyError, TypeError):
        return None
    return suggested_mem_mb(mem) if state.get("mem_boost") else int(mem)


def _gate_timeouts(ctx, state: dict) -> dict:
    """Configured `timeout_s` of each timed-out gate; {} when the project config is unreadable."""
    try:
        pcfg = ctx.project(state)
        worktree = (state.get("repo") or {}).get("worktree")
        if worktree:                        # SPEC-0316: the limit the gates actually ran under
            pcfg, _ = P.campaign_gate_config(ctx.cfg, pcfg, worktree)
        return {g.get("name"): int(g.get("timeout_s", 1200)) for g in pcfg.get("gates") or []}
    except (ValueError, OSError):
        return {}


def gate_exclusive(state: dict):
    """The exclusive-gate marker of a campaign (SPEC-0311), or None: set after a timeout re-gate
    is decided and on resume from a `gate_timeout` park; cleared when its gate run is applied."""
    return state.get("gate_exclusive") or None


def _exclusive_key(state: dict) -> tuple:
    return ((gate_exclusive(state) or {}).get("entered_at") or "", state["id"])


def select_gate_starts(candidates: list, in_flight: int, exclusive_in_flight: bool) -> list:
    """Pure (SPEC-0311): the campaign ids whose gate step may start this tick. `candidates` are
    the states that want a gate step; `in_flight` counts gate steps (and close re-gates) already
    running. An exclusive gate step starts alone, once nothing is in flight; while any is
    pending, no non-exclusive one starts, so it cannot starve. Exclusive ones go one at a time
    in `(entered_at, id)` order."""
    if exclusive_in_flight:
        return []
    pending = sorted((c for c in candidates if gate_exclusive(c)), key=_exclusive_key)
    if pending:
        return [pending[0]["id"]] if in_flight == 0 else []
    return [c["id"] for c in candidates]


def exclusive_wait_max_s(cfg) -> int:
    """`agentic.gates.exclusive_wait_max_s` (SPEC-0507); anything but a positive number falls
    back to `EXCLUSIVE_WAIT_MAX_S`. SPEC-0523: it may shorten the bound, never lengthen it."""
    raw = (((cfg.get("agentic") or {}).get("gates")) or {}).get("exclusive_wait_max_s")
    ok = isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0
    return min(int(raw), EXCLUSIVE_WAIT_MAX_S) if ok else EXCLUSIVE_WAIT_MAX_S


def exclusive_waited_s(state: dict, now) -> float | None:
    """Seconds the exclusive marker of `state` has been pending at `now` (an aware datetime),
    None without a marker; an unreadable `entered_at` counts as waiting forever (SPEC-0507)."""
    import datetime as dt
    marker = gate_exclusive(state)
    if not marker:
        return None
    try:
        t = dt.datetime.fromisoformat(str(marker.get("entered_at") or ""))
    except ValueError:
        return math.inf
    return (now - t.astimezone()).total_seconds()


def expired_exclusives(candidates: list, now, wait_max_s: float) -> list:
    """Pure (SPEC-0507): ids of the candidates whose exclusive marker has waited longer than
    `wait_max_s`, in `(entered_at, id)` order. The caller clears their marker and starts them as
    normal gates, so a pending exclusive re-gate waits a bounded time."""
    old = [c for c in candidates if (exclusive_waited_s(c, now) or 0) > wait_max_s]
    return [c["id"] for c in sorted(old, key=_exclusive_key)]


def norm_finding_title(title) -> str:
    """Lower-case, whitespace-collapsed title: the identity of a finding across reviewers
    (SPEC-0203; SPEC-0207 reuses it for its follow-up key)."""
    return " ".join(str(title or "").lower().split())


def finding_detail(f: dict):
    """A finding's `detail` with its `trigger` folded in (SPEC-0202), so the ledger event and
    the implementer brief carry the same text. `suggested_fix` / `acceptance` follow (SPEC-0295)."""
    detail, trigger = f.get("detail"), f.get("trigger")
    if detail and trigger:
        detail = f"{detail}\ntrigger: {trigger}"
    elif trigger:
        detail = f"trigger: {trigger}"
    return PS.finding_text(f, detail)


def _covers(finding_ac, entry_ac) -> bool:
    """Does a finding's `ac` cover an `ac_assessment` entry's `ac`? Equal after `norm`, or one
    contains the other with length >= 8; an empty `ac` covers nothing."""
    a, b = norm_finding_title(finding_ac), norm_finding_title(entry_ac)
    if not a or not b:
        return False
    return a == b or (a in b and len(a) >= 8) or (b in a and len(b) >= 8)


def _exact(a, b) -> bool:
    """Equal after `norm_finding_title`; empty text matches nothing (SPEC-0264)."""
    na = norm_finding_title(a)
    return bool(na) and na == norm_finding_title(b)


ADVERSARIAL_REVIEWER = "adversarial_reviewer"
_NO_IMPLEMENT_RESUME = ("IMPLEMENTING", "GATING", "REVIEWING", "DIVERGENCE_TRIAGE")


def must_fix_titles(items) -> list:
    """Sorted unique normalized titles of a must-fix list (dicts or bare strings)."""
    out = []
    for item in items or []:
        if isinstance(item, dict):
            if item.get("severity") not in (None, "must_fix"):
                continue
            title = item.get("title") or ""
        else:
            title = str(item)
        title = norm_finding_title(title)
        if title:
            out.append(title)
    return sorted(set(out))


def note_must_fix_baseline(counters: dict, titles: list) -> None:
    """Record the must-fix set of the first fix round in this loop window (SPEC-0468)."""
    if counters.get("must_fix_baseline_set"):
        return
    counters["must_fix_baseline"] = list(titles)
    counters["must_fix_baseline_set"] = True


def loop_can_auto_resume(counters: dict, titles: list) -> bool:
    """True once per campaign, and only when the must-fix set is strictly smaller."""
    if int(counters.get("loop_auto_resumes") or 0) >= 1:
        return False
    base = counters.get("must_fix_baseline")
    if not isinstance(base, list):
        return False
    return len(titles) < len(base)


def arm_loop_auto_resume(counters: dict) -> None:
    """Clear the loop window and consume the one automatic resume."""
    counters["loops"] = 0
    counters["loop_auto_resumes"] = int(counters.get("loop_auto_resumes") or 0) + 1
    counters.pop("must_fix_baseline", None)
    counters.pop("must_fix_baseline_set", None)


def cursor_review_cap(profile: dict) -> int:
    """Review-driven implement rounds a cursor campaign may spend (SPEC-0468)."""
    return 2 * int((profile or {}).get("loop_max") or 3)


def review_rounds_spent(counters: dict, profile: dict) -> bool:
    return int(counters.get("review_rounds") or 0) >= cursor_review_cap(profile)


def _cursor_campaign(ctx, state: dict) -> bool:
    try:
        from .runner import resolve_runner
        return resolve_runner(getattr(ctx, "cfg", None), campaign=state) == "cursor"
    except Exception:
        return False


def _maybe_loop_auto_resume(state: dict, counters: dict, items) -> bool:
    """Consume the one automatic resume when the must-fix set shrank. False leaves the park."""
    titles = must_fix_titles(items)
    if not loop_can_auto_resume(counters, titles):
        return False
    arm_loop_auto_resume(counters)
    return True


def _usage_holds_review(reviews, state: dict, profile: dict) -> bool:
    """Every reviewer still missing a verdict failed because the account is out of usage."""
    valid = _valid_verdicts(state, profile)
    missing = [r for r in reviews if getattr(getattr(r, "step", None), "persona", None)
               not in valid]
    if not missing:
        return False
    return all(r.run_result is not None and PG.is_usage_exhausted(r.run_result)
               for r in missing)


def _round_budget_park(profile: dict, counters: dict) -> dict:
    cap = cursor_review_cap(profile)
    used = int(counters.get("review_rounds") or 0)
    return {"reason": "round_budget", "resume_to": "SPEC_WRITING",
            "detail": f"cursor review-round budget of {cap} is spent ({used} review fix rounds)"}


def reject_resume_target(campaign: str, reason: str, target: str) -> None:
    """SPEC-0468: some parks cannot go back to implementing."""
    if reason == "diverging_rework" and target in _NO_IMPLEMENT_RESUME:
        raise ValueError(f"{campaign} is parked diverging_rework; resume to SPEC_WRITING, "
                         f"not {target}")
    if reason == "round_budget" and target in _NO_IMPLEMENT_RESUME:
        raise ValueError(f"{campaign} spent its review-round budget; resume to SPEC_WRITING "
                         f"or abandon, not {target}")
    if reason == "spec_status" and target != "CLOSING":
        raise ValueError(f"{campaign} is parked spec_status; resume to CLOSING, not {target}")


def human_overrides_of(state: dict) -> tuple:
    """`(human_waived, attested)` from `state["human_overrides"]` (SPEC-0235): the waive dicts
    and the attested AC texts; both empty when the campaign has none."""
    ov = state.get("human_overrides") if isinstance(state.get("human_overrides"), dict) else {}
    waive = [w for w in ov.get("waive") or [] if isinstance(w, dict)]
    attest = [a["ac"] for a in ov.get("attest") or [] if isinstance(a, dict) and a.get("ac")]
    return waive, attest


def triage_reviews(verdicts: dict, profile: dict, waived=(), human_waived=(),
                   attested=()) -> dict:
    """Pure (SPEC-0203): score and route the reviewers' verdicts `{persona: verdict}` (valid
    verdicts, in profile order) into `{outcome, score, score_only, blocking, advisory,
    waivable_weight, fixing, synthetic}`.

    Normalize (finding ids, synthetic `major`s for an unexplained `fix` and for unmet ACs,
    de-duplication across reviewers) ⇒ waive (`waived` ids, `minor`/`nit` only) ⇒ a `fix`
    verdict survives iff unexplained or backed by a `major`/`must_fix` ⇒ score
    `max(0, 100 - Σ weight)` ⇒ `fix` on a `must_fix`, a surviving `fix` verdict, an unmet AC
    or `score < pass_score`. `fixing` = the personas whose `fix` verdict survived; `synthetic`
    = ids of the synthetic findings.

    SPEC-0235: `human_waived` (stored waive dicts) removes a finding of any severity, synthetic
    included, by id + normalized title, or (real findings) by normalized title + sorted files;
    `attested` ACs (plus every human-waived finding's `resolved_ac` texts) make an unmet
    `ac_assessment` entry met when equal after normalizing (SPEC-0264); synthetic waivers do not
    key real findings. The result also lists `human_waived` ids and `attested` texts
    that applied."""
    pass_score, weights = review_params(profile)
    personas = list(verdicts)
    own = {}                      # persona -> its finding records, in order
    wants_fix, unmet = {}, False
    human_waived = [w for w in human_waived or () if isinstance(w, dict)]
    by_id = {(w.get("id"), norm_finding_title(w.get("title"))) for w in human_waived}
    by_key = {(norm_finding_title(w.get("title")), tuple(sorted(w.get("files") or [])))
              for w in human_waived if not re.search(r"#(fix|ac\d+)$", str(w.get("id") or ""))}

    def human_gone(r):
        return (r["id"], norm_finding_title(r["title"])) in by_id or (
            not r["synthetic"] and (norm_finding_title(r["title"]),
                                    tuple(sorted(r["files"]))) in by_key)

    attest_all = [a for a in attested or () if a] + [
        u for w in human_waived for u in w.get("resolved_ac") or () if u]
    attest_hit = []
    basis_demoted = []
    out_scope = []                # SPEC-0555: findings the collation scoped out
    for p in personas:
        v = verdicts[p]
        raw = [f for f in v.get("findings") or [] if isinstance(f, dict)]
        recs = []
        for i, f in enumerate(raw):
            sev = f.get("severity") if f.get("severity") in SEVERITIES else "minor"
            if f.get("demoted"):    # SPEC-0554: a delta-rule demotion is advisory, never scored
                basis_demoted.append({"id": f"{p}#{i}", "personas": [p], "severity": sev,
                                      "title": f.get("title") or "",
                                      "detail": finding_detail(f),
                                      "files": list(f.get("files") or []), "ac": f.get("ac"),
                                      "category": f.get("category"), "synthetic": False})
                continue
            tie = {"tie": f["tie"]} if f.get("tie") else {}            # SPEC-0555
            if f.get("scope") == "out":     # SPEC-0555: filed as a follow-up, never scored
                out_scope.append({"id": f"{p}#{i}", "personas": [p], "severity": sev,
                                  "title": f.get("title") or "", "detail": finding_detail(f),
                                  "files": list(f.get("files") or []), "ac": f.get("ac"),
                                  "category": f.get("category"), "synthetic": False, **tie})
                continue
            recs.append({"id": f"{p}#{i}", "personas": [p], "severity": sev,
                         "title": f.get("title") or "", "detail": finding_detail(f),
                         "files": list(f.get("files") or []), "ac": f.get("ac"),
                         "category": f.get("category"), "synthetic": False, **tie})
        wants_fix[p] = v.get("verdict") == "fix" or any(r["severity"] == "must_fix"
                                                       for r in recs)
        if v.get("verdict") == "fix" and not raw:
            brief = (v.get("fix_brief") or "").strip()[:80]
            recs.append({"id": f"{p}#fix", "personas": [p], "severity": "major",
                         "title": brief or f"{p} returned fix without findings", "detail": None,
                         "files": [], "ac": None, "category": None, "synthetic": True})
        for j, entry in enumerate(v.get("ac_assessment") or []):
            if not isinstance(entry, dict) or entry.get("met") is not False:
                continue
            hit = next((a for a in attest_all if _exact(a, entry.get("ac"))), None)
            if hit is not None:
                if hit not in attest_hit:
                    attest_hit.append(hit)
                continue
            unmet = True
            # a human-waived finding no longer stands for the AC (SPEC-0286): it gets its own
            if any(not r["synthetic"] and r["severity"] in ("major", "must_fix")
                   and not human_gone(r) and _covers(r["ac"], entry.get("ac")) for r in recs):
                continue
            recs.append({"id": f"{p}#ac{j}", "personas": [p], "severity": "major",
                         "title": str(entry.get("ac") or ""), "detail": entry.get("evidence"),
                         "files": [], "ac": entry.get("ac"), "category": None,
                         "synthetic": True})
        own[p] = recs
    # 1d: identical real findings (same files and title) become one merged finding
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    merged, groups = [], {}                   # all findings in order; key -> merged record
    belongs = {p: [] for p in personas}       # persona -> merged records it reported
    for p in personas:
        for r in own[p]:
            key = (tuple(sorted(r["files"])), norm_finding_title(r["title"]))
            m = groups.get(key) if r["files"] and not r["synthetic"] else None
            if m is None:
                m = r
                merged.append(m)
                if r["files"] and not r["synthetic"]:
                    groups[key] = m
            else:
                if rank[r["severity"]] < rank[m["severity"]]:      # strictly higher wins
                    m.update(id=r["id"], severity=r["severity"], detail=r["detail"],
                             ac=r["ac"], category=r["category"])
                if p not in m["personas"]:
                    m["personas"].append(p)
            if all(m is not x for x in belongs[p]):
                belongs[p].append(m)
    # 2: waive (minor/nit only; synthetic findings never)
    waived = set(waived or ())
    gone = {id(m) for m in merged if m["id"] in waived and not m["synthetic"]
            and m["severity"] in ("minor", "nit")}
    human_ids = [m["id"] for m in merged if human_gone(m)]
    gone |= {id(m) for m in merged if human_gone(m)}
    surviving = [m for m in merged if id(m) not in gone]
    # SPEC-0468: an adversarial major does not block once conformance passed with no
    # must-fix and no unmet AC. It stays advisory so the close files it as a follow-up.
    demoted = []
    conf = verdicts.get("conformance_reviewer")
    if isinstance(conf, dict) and conf.get("verdict") == "pass" and not unmet and not any(
            m["severity"] == "must_fix" for m in surviving):
        kept = []
        for m in surviving:
            if (m["personas"] == [ADVERSARIAL_REVIEWER] and m["severity"] == "major"
                    and not m["synthetic"]):
                demoted.append(m)
            else:
                kept.append(m)
        surviving = kept
    # 3: a fix verdict survives iff backed by a surviving major/must_fix (synthetics are majors)
    fixing = [p for p in personas if wants_fix[p] and any(
        m["severity"] in ("major", "must_fix") for m in surviving if p in m["personas"])]
    # 4/5: score and outcome
    score = max(0, 100 - sum(weights[m["severity"]] for m in surviving))
    must = any(m["severity"] == "must_fix" for m in surviving)
    hard = must or bool(fixing) or unmet
    outcome = "fix" if hard or score < pass_score else "pass"
    score_only = outcome == "fix" and not hard
    # 6: blocking and advisory
    if outcome == "pass":
        blocking = []
    elif score_only:
        blocking = sorted(surviving, key=lambda m: -weights[m["severity"]])
    else:
        blocking = [m for m in surviving if m["severity"] in ("must_fix", "major")]
    basis_demoted = [m for m in basis_demoted if not human_gone(m)]
    advisory = [m for m in surviving if m not in blocking] + demoted + basis_demoted
    keys = ("id", "personas", "severity", "title", "detail", "files", "ac", "category")
    out = lambda ms: [{**{k: m[k] for k in keys}, **({"tie": m["tie"]} if m.get("tie") else {})}
                      for m in ms]
    extra = {"basis_demoted": [m["id"] for m in basis_demoted]} if basis_demoted else {}
    if out_scope:                                                       # SPEC-0555
        extra["out_of_scope"] = out(out_scope)
    return {**extra, "outcome": outcome, "score": score, "score_only": score_only,
            "blocking": out(blocking), "advisory": out(advisory),
            "waivable_weight": sum(weights[m["severity"]] for m in surviving
                                   if m["severity"] in ("minor", "nit")),
            "fixing": fixing, "synthetic": [m["id"] for m in surviving if m["synthetic"]],
            "human_waived": human_ids, "attested": attest_hit}


def _bullet(f: dict) -> str:
    files = ", ".join(f.get("files") or [])
    tie = f" [{f['tie']}]" if f.get("tie") and f["tie"] != "none" else ""     # SPEC-0555
    return (f"- [{f['severity']}]{tie} {f['title']}" + (f" ({files})" if files else "")
            + (f": {f['detail']}" if f.get("detail") else ""))


def review_brief(verdicts: dict, tri: dict, pass_score) -> dict:
    """Pure: the fix brief a `fix` triage implies (SPEC-0203) as `{fix_brief, must_fix,
    personas}` (`personas` only with several reviewers). One reviewer keeps SPEC-0187's shape
    (its own brief verbatim; `must_fix` entries without `id` / `severity` / `personas`)."""
    blocking = tri["blocking"]
    tail = ([f"Review score {tri['score']} is below the pass score {pass_score}; address the "
             f"highest-weight findings first."] if tri["score_only"] else [])
    tail += [_bullet(f) for f in blocking]
    # SPEC-0555: once the collation scoped a finding out, the reviewers' free-text briefs may
    # describe out-of-scope work; the brief is then the in-scope findings only (verbatim)
    scoped_out = bool(tri.get("out_of_scope"))
    if len(verdicts) == 1:
        persona = next(iter(verdicts))
        brief = verdicts[persona].get("fix_brief") if not scoped_out else ""
        if scoped_out or (not (brief or "").strip() and (tri["score_only"] or any(
                f["id"] in tri["synthetic"] for f in blocking))):
            brief = "\n".join(tail)
        return {"fix_brief": brief, "must_fix": [
            _with_category({"ac": f["ac"], "title": f["title"], "detail": f["detail"],
                            "files": f["files"]}, f) for f in blocking]}
    head = "" if scoped_out else "\n\n".join(
        f"[{p}] {(verdicts[p].get('fix_brief') or '').strip()}".rstrip() for p in tri["fixing"])
    reporters = {p for f in blocking for p in f["personas"]} | set(tri["fixing"])
    return {"fix_brief": "\n\n".join(x for x in (head, "\n".join(tail)) if x),
            "personas": [p for p in verdicts if p in reporters],
            "must_fix": [_with_category({"ac": f["ac"], "title": f["title"],
                                         "detail": f["detail"], "files": f["files"],
                                         "id": f["id"], "severity": f["severity"],
                                         "personas": f["personas"]}, f) for f in blocking]}


def _with_category(item: dict, f: dict) -> dict:
    """A brief entry carries the finding's `category` when it has one (SPEC-0202), and its
    collation `tie` (SPEC-0555)."""
    if f.get("category"):
        item["category"] = f["category"]
    if f.get("tie"):
        item["tie"] = f["tie"]
    return item


def _retries_used(counters: dict, persona: str, n_reviewers: int) -> int:
    """Retries `persona` used in this review: its own counter, else (one reviewer only) the
    legacy int `review_retries`, else 0."""
    by = counters.get("review_retries_by_persona") or {}
    if persona in by:
        return int(by[persona])
    return int(counters.get("review_retries", 0)) if n_reviewers == 1 else 0


def needs_triager(result: dict, profile: dict) -> bool:
    """Pure (SPEC-0206): could the review triager change the outcome of `triage_reviews`
    `result`? Only a score-only `fix` that waiving every waivable finding would lift to the pass
    score, with `review.triager` on."""
    if not triager_enabled(profile) or result.get("outcome") != "fix" \
            or not result.get("score_only"):
        return False
    return result["score"] + result["waivable_weight"] >= review_params(profile)[0]


def decide_reviews(state: dict, profile: dict, valid: dict, failed=(), waived=(),
                   human_waived=(), attested=()) -> Step:
    """Pure: the transition the reviewers' verdicts imply (SPEC-0187, SPEC-0201).

    `valid` = ordered `{persona: verdict}` of every reviewer with a valid current-loop
    verdict; `failed` = the personas that produced no usable verdict in this pass.
    - a reviewer without a valid verdict is *missing*: no routing while any is missing;
      each missing one gets one retry (`Step("none", reason="review_retry", missing=…)`);
      if any is out of retries ⇒ PARKED(review_escalated) naming those (`escalated`);
    - all valid: `triage_reviews` (with the `waived` ids, SPEC-0206); `pass` ⇒ CLOSING; `fix` ⇒ loops += 1, beyond `loop_max`
      ⇒ loop_exhausted, else back to IMPLEMENTING. `combined` carries the triage,
      the pass score and (on `fix`) the brief.
    The caller mutates the counters."""
    counters = state.get("counters") or {}
    loop_max = int(profile.get("loop_max") or 3)
    reviewers = reviewers_of(profile)
    missing = tuple(p for p in reviewers if p not in valid)
    if missing:
        out = tuple(p for p in missing
                    if _retries_used(counters, p, len(reviewers)) >= REVIEW_RETRY_MAX)
        if out:
            who = "the conformance reviewer" if out == ("conformance_reviewer",) \
                else ", ".join(out)
            return Step("park", reason="review_escalated", missing=missing, escalated=out,
                        detail=f"{who} returned no usable verdict twice", resume_to="REVIEWING")
        return Step("none", reason="review_retry", missing=missing)
    valid = {p: valid[p] for p in reviewers}
    tri = triage_reviews(valid, profile, waived, human_waived, attested)
    pass_score = review_params(profile)[0]
    combined = {"triage": tri, "pass_score": pass_score, "brief": None}
    if tri["outcome"] == "pass":
        return Step("close", reason="review_pass", combined=combined)
    combined["brief"] = review_brief(valid, tri, pass_score)
    loops = int(counters.get("loops", 0)) + 1
    if loops > loop_max:
        brief = (combined["brief"]["fix_brief"] or "").strip()
        return Step("park", reason="loop_exhausted", combined=combined,
                    detail=f"{loops - 1}/{loop_max} implement loops used; the conformance "
                           f"review still asks for fixes: " + brief, resume_to="IMPLEMENTING")
    return Step("implement", reason="review_fix", combined=combined)


def decide_review(state: dict, profile: dict, verdict: dict | None, verdict_ok: bool) -> Step:
    """Pure: the transition one conformance-reviewer verdict implies (SPEC-0187) — a thin
    one-reviewer wrapper over `decide_reviews`.

    - malformed / missing verdict: one retry per loop (stay in REVIEWING), then
      PARKED(review_escalated);
    - `pass` with no `must_fix` finding ⇒ CLOSING;
    - `fix`, or any `must_fix` finding even under `pass`: loops += 1; beyond `loop_max` ⇒
      loop_exhausted, else back to IMPLEMENTING."""
    solo = dict(profile, reviewers=["conformance_reviewer"])
    ok = bool(verdict_ok) and isinstance(verdict, dict)
    return decide_reviews(state, solo, {"conformance_reviewer": verdict} if ok else {},
                          () if ok else ("conformance_reviewer",))


# ---- backlog: ordering, readiness, admission (SPEC-0180) ----------------------------------

def check_priority(value) -> int:
    """`value` as a priority in 1..5 (1 highest), else ValueError."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = None
    if n is None or isinstance(value, bool) or not PRIORITY_MIN <= n <= PRIORITY_MAX:
        raise ValueError(f"priority must be an integer {PRIORITY_MIN}..{PRIORITY_MAX} "
                         f"(1 highest), got {value!r}")
    return n


def _priority(state: dict) -> int:
    try:
        return check_priority(state.get("priority", PRIORITY_DEFAULT))
    except ValueError:
        return PRIORITY_DEFAULT


def admission_key(state: dict) -> tuple:
    """Admission order: highest priority first, oldest first among equals."""
    return (_priority(state), state.get("created_at") or "", state.get("id") or "")


def pick_admissions(backlog, busy: int, slots: int, readiness) -> list:
    """Pure: the backlog items to admit now, in admission order. Candidates are ready and not
    park-requested; at most `slots - busy` are taken. `readiness(state) -> (ready, blocked_by)`."""
    free = max(0, int(slots) - int(busy))
    picks = []
    for st in sorted(backlog, key=admission_key):
        if len(picks) >= free:
            break
        if st.get("park_requested") or not readiness(st)[0]:
            continue
        picks.append(st)
    return picks


def has_no_spec(st: dict) -> bool:
    """SPEC-0209: an idea campaign whose spec is not written yet. Keys on `kind` first: spec
    campaigns created earlier carry no `spec.id` and keep their old behaviour."""
    return st.get("kind") == "idea" and not (st.get("spec") or {}).get("id")


def idea_is_open(task) -> bool:
    """SPEC-0209: an idea a campaign may still work: IDEA or INCUBATE, or SPECCED with no
    `:SPEC:`."""
    state = task.state or ""
    return state in ("IDEA", "INCUBATE") or (
        state == "SPECCED" and not (task.properties.get("SPEC") or "").strip())


def _idea_task(cfg, idea_id):
    """The idea task for `idea_id` (exact `:ID:` or task id), or None."""
    for t in _ideas(cfg):
        if idea_id in ((t.properties.get("ID") or "").strip(), t.id):
            return t
    want = _canon_id(idea_id)                       # a hand-typed `:ID: idea-071` (SPEC-0276)
    for t in _ideas(cfg):
        if _canon_id(t.properties.get("ID") or "") == want:
            return t
    return None


def _is_request_only(st: dict) -> bool:
    """SPEC-0541: a campaign whose only input is a public request (no org idea)."""
    return bool((st.get("idea") or {}).get("request_only"))


def _request_source(cfg, req_id: str, project: str, *, allow_spec: str | None = None):
    """SPEC-0541: the one reader of the public request for a `request_only` campaign. Returns
    `(agreed, linked_task_or_None)` or raises ValueError. An org idea whose `:REQ:` is `req_id`
    is a drift gate (SPEC-0535's `load_agreed_request`), never a source of text. `allow_spec` is
    the spec the calling campaign owns, so its own cite is not an "already cites" error."""
    from .. import idea_link as IL
    try:
        ideas = _ideas(cfg)                 # not `_ideas_or_empty`: an unreadable file fails closed
    except (OSError, ValueError) as e:
        raise ValueError(f"could not read the ideas file to check {req_id}: "
                         f"{e or type(e).__name__}") from e
    linked = [t for t in ideas if (t.properties.get("REQ") or "").strip() == req_id]
    if len(linked) > 1:
        a, b = (IL._idea_public_id(t) for t in linked[:2])
        raise ValueError(f"{req_id} is claimed by {a} and {b}")
    task = linked[0] if linked else None
    if task is not None:
        agreed = IL.load_agreed_request(cfg, task)
        iid = IL._idea_public_id(task)
        owner = (task.properties.get("PROJECT") or "").strip()
        if owner != project:
            raise ValueError(f"{req_id} is linked to {iid} of project {owner}, not {project}")
        spec = (task.properties.get("SPEC") or "").strip()
        if spec:
            raise ValueError(f"{iid} already has {spec}; queue that spec instead: "
                             f"nw queue add {spec}")
        if not idea_is_open(task):
            raise ValueError(f"{iid} is {task.state}; only open ideas (IDEA, INCUBATE, or "
                             f"SPECCED without a :SPEC:) are queued")
    else:
        agreed = IL.load_public_request(cfg, project, req_id)
    if agreed.status_value not in ("open", "closed"):
        raise ValueError(f"{req_id} file is not a request")
    if agreed.status_value != "open":
        raise ValueError(f"{req_id} is {agreed.status_value}; only open requests are queued")
    if agreed.spec is not None and agreed.spec != allow_spec:
        raise ValueError(f"{req_id} already cites {agreed.spec}")
    return agreed, task


def _request_source_for(cfg, state: dict):
    """`_request_source` for a `request_only` campaign, with the campaign's own spec allowed."""
    idea = state.get("idea") or {}
    own = (state.get("spec") or {}).get("id") or state.get("spec_id") or None
    return _request_source(cfg, idea.get("source_request") or idea.get("id"),
                           state.get("project") or idea.get("project") or "", allow_spec=own)


def _idea_id_of(st: dict) -> str | None:
    return (st.get("idea") or {}).get("id")


def _is_dep_id(dep) -> bool:
    """True for a well-formed dependency id (`SPEC-NNNN`, outbound `PROJ-NNNN`): ASCII, full match."""
    return isinstance(dep, str) and dep.isascii() and _ANY_ID_RE.fullmatch(dep) is not None


def _readiness_dep_ids(fm: dict) -> list[str] | None:
    """Frontmatter dependency ids for `backlog_readiness`, stripped; ``None`` when ``depends_on`` is
    invalid, including an id that is not a well-formed spec id (it would reach a glob)."""
    raw = fm.get("depends_on") if isinstance(fm, dict) else None
    if raw is None:
        return []
    if isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        raw = [raw] if raw.strip() else []
    if isinstance(raw, list):
        out: list[str] = []
        for d in raw:
            if not isinstance(d, str) or not _is_dep_id(d.strip()):
                return None
            out.append(d.strip())
        return out
    return None


def _parse_frontmatter_text(text: str) -> dict | None:
    """Frontmatter mapping of decoded spec text; ``None`` when it cannot be parsed or is not a
    mapping. A pathological spec (invalid YAML, deep nesting, huge) must not raise out of readiness."""
    try:
        fm, _ = split_frontmatter(text)
    except (OSError, ValueError, yaml.YAMLError, RecursionError, MemoryError):
        return None
    return fm if isinstance(fm, dict) else None


def _read_frontmatter_text(path) -> dict | None:
    """Frontmatter of the spec file at `path`: ``None`` when it is unreadable (missing, not a
    regular file, over `MAX_WORKTREE_SPEC_BYTES`, not UTF-8, invalid YAML, too deep); ``{}`` when
    the file has no mapping frontmatter (a present spec with no status)."""
    cap = MAX_WORKTREE_SPEC_BYTES
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return None
        with open(path, "rb") as fh:
            data = fh.read(cap + 1)
        if len(data) > cap:
            return None
        text = data.decode("utf-8").replace("\r\n", "\n")
        fm, _ = split_frontmatter(text)
    except (OSError, ValueError, yaml.YAMLError, RecursionError, MemoryError):
        return None
    return fm if isinstance(fm, dict) else {}


def _read_branch_spec_blob(root: str, branch: str, rel: str) -> bytes | None:
    """Committed spec bytes at ``refs/heads/<branch>:<rel>``; None when missing or over the cap."""
    rev = f"refs/heads/{branch}:{rel}"
    sz = git.git(root, "cat-file", "-s", rev, check=False)
    if sz.returncode != 0:
        return None
    try:
        size = int(str(sz.stdout or "").strip())
    except ValueError:
        return None
    if size > MAX_WORKTREE_SPEC_BYTES:
        return None
    show = git.git(root, "show", rev, check=False, binary=True)
    if show.returncode != 0 or not show.stdout:
        return None
    return show.stdout


def _campaign_spec_text(state: dict) -> tuple[str | None, bool]:
    """The campaign spec body for readiness and whether it came from the branch line (SPEC-0539)."""
    repo = state.get("repo") or {}
    rel = (state.get("spec") or {}).get("rel_path") or ""
    root = repo.get("root") or ""
    branch = repo.get("branch") or ""
    worktree = repo.get("worktree") or ""
    if not root or not branch:
        return None, False
    try:
        if not git.branch_exists(root, branch):
            return None, False
    except GitError:
        return None, False
    try:
        if worktree:
            try:
                elsewhere = git.worktree_for_branch(root, branch)
            except GitError:
                elsewhere = None
            if elsewhere is not None and elsewhere == Path(worktree).resolve():
                data, read_err = _read_worktree_spec_bytes(Path(worktree), rel)
                if data is not None and read_err is None:
                    return _spec_bytes_as_text(data), True
                return None, True
        if not rel:
            return None, True
        data = _read_branch_spec_blob(root, branch, rel)
        if data is None:
            return None, True
        return _spec_bytes_as_text(data), True
    except (GitError, OSError, UnicodeDecodeError, MemoryError, RecursionError):
        return None, True


def backlog_readiness(cfg, state: dict) -> tuple[bool, list[str]]:
    """Can this queued campaign be admitted? Re-reads the campaign spec and its `depends_on`. For a
    non-split campaign, when `repo.branch` exists on `repo.root` the spec comes from that branch's
    worktree (only when the planned `repo.worktree` is that branch's checkout) or from the
    committed blob at `refs/heads/<branch>:<rel>` (SPEC-0539); otherwise from the base checkout.
    Split campaigns always read `split.original_rel` under `repo.root`. Dependency files are always
    resolved in the base specs directory. The spec must still be `accepted` (`in-progress` for a
    `--force` add) and every dependency `done` or `superseded`. Unknown ids block. An idea
    campaign without a spec (SPEC-0209) is ready while its idea is still open. A merge request
    (SPEC-0233) is ready while its branch exists."""
    if state.get("kind") in RM.RED_KINDS:          # SPEC-0420: no spec, opened by the queue
        return True, []
    if state.get("kind") == "merge":
        repo = state.get("repo") or {}
        branch = repo.get("branch") or ""
        if branch and git.branch_exists(repo.get("root") or ".", branch):
            return True, []
        return False, [f"branch {branch} missing"]
    after_blocked = _after_blocked(cfg, state)         # SPEC-0423: any non-merge campaign
    if after_blocked:
        return False, after_blocked
    if has_no_spec(state):
        iid = _idea_id_of(state)
        if _is_request_only(state):                     # SPEC-0541: no org idea to find
            try:
                _request_source_for(cfg, state)
            except ValueError as e:
                return False, [f"request {iid}: {e}"]
            return True, []
        task = _idea_task(cfg, iid)
        if task is None:
            return False, [f"idea {iid} not found"]
        if idea_is_open(task):
            return True, []
        return False, [f"idea {iid} is {task.state}"]
    repo = state.get("repo") or {}
    root_s = repo.get("root") or ""
    root = Path(root_s)
    split = state.get("kind") == "split"
    rel = (state.get("split") or {}).get("original_rel") if split \
        else (state.get("spec") or {}).get("rel_path")
    rel = rel or ""
    if split:
        spec_path = root / rel
        fm = _read_frontmatter_text(spec_path)
        if fm is None:
            return False, ["spec file missing"]
    else:
        text, from_branch = _campaign_spec_text(state)
        if from_branch:
            if text is None:
                return False, ["spec file missing"]
            fm = _parse_frontmatter_text(text)
            if fm is None:
                return False, ["spec file missing"]
        else:
            spec_path = root / rel
            fm = _read_frontmatter_text(spec_path)
            if fm is None:
                return False, ["spec file missing"]
        spec_path = root / ((state.get("spec") or {}).get("rel_path") or "")
    blocked = []
    status = str(fm.get("status") or "")
    allowed = ("accepted", "in-progress") if state.get("force") else ("accepted",)
    if status not in allowed:
        blocked.append(f"spec status is {status or 'missing'}")
    if split:                                   # SPEC-0290: writing spec files needs no dependency
        if _spec_sha(spec_path) != (state.get("split") or {}).get("review_sha"):
            blocked.append("spec changed since the split review")
        return not blocked, blocked
    deps = _readiness_dep_ids(fm)
    if deps is None:
        return False, ["spec file missing"]
    for dep in deps:
        dep = str(dep)
        camp = _dep_campaign(cfg, dep, state.get("project"))
        merged = (camp or {}).get("state") == "MERGED"
        try:
            found = _find_dep_spec(spec_path.parent, dep)
        except ValueError:                      # malformed or unmatched id
            found = None
        dep_fm = _read_frontmatter_text(found) if found is not None else None
        if dep_fm is None:                      # unmatched, oversize, unreadable, pathological
            if not merged:                      # SPEC-0347: the base may predate the export
                blocked.append(f"{dep} not found")
            continue
        met, why = dependency_met(spec_path.parent, dep, dep_fm,
                                  {str(fm["id"])} if fm.get("id") else set())
        if met or merged:                       # SPEC-0347: a merged PR the base lacks yet
            continue
        if (camp or {}).get("state") == "HANDED_OFF":
            why += f"; PR open, campaign {camp.get('id')} handed off"
        blocked.append(f"waiting on {dep} ({why})")
    return not blocked, blocked


def after_entries(state: dict) -> list[dict]:
    """SPEC-0423: the campaign's `after` prerequisites as `[{"ref", "kind"}]` (kind `idea|spec|
    campaign`); [] for a campaign without any (every campaign queued before SPEC-0423)."""
    out = []
    for e in state.get("after") or []:
        if isinstance(e, dict) and e.get("ref"):
            out.append({"ref": str(e["ref"]), "kind": str(e.get("kind") or "spec")})
        elif isinstance(e, str) and e:
            out.append({"ref": e, "kind": "idea" if _IDEA_ID_RE.match(e) else "spec"})
    return out


def _specs_dir_of(state: dict) -> Path:
    """Where `after` spec refs resolve: next to the campaign's own spec, else the project's
    `docs/specs` (an idea campaign has no spec file yet)."""
    root = Path((state.get("repo") or {}).get("root") or "")
    rel = (state.get("spec") or {}).get("rel_path")
    return (root / rel).parent if rel else root / "docs" / "specs"


_NO_CAMPAIGN_AFTER_RE = re.compile(
    r"^waiting on (IDEA-\d+) \(after: no campaign queued for \1\)$")


def _spec_after_met(cfg, state: dict, spec_id: str) -> tuple[bool, str, bool]:
    """`(met, why, live)` for a prerequisite spec id: the spec file through `dependency_met`,
    else a MERGED campaign of it (an outbound PR or a lagging base checkout)."""
    project = state.get("project")
    hits = store.list_campaigns(cfg, include_terminal=True, project=project, spec_id=spec_id)
    camp = hits[0] if hits else None
    live = bool(camp and camp.get("state") not in TERMINAL_STATES)
    try:
        found = _find_dep_spec(_specs_dir_of(state), spec_id)
    except (OSError, ValueError):
        found = None
    dep_fm = _read_frontmatter_text(found) if found is not None else None
    if dep_fm is None:
        if (camp or {}).get("state") == "MERGED":
            return True, "", live
        return False, f"{spec_id} not found", live
    met, why = dependency_met(_specs_dir_of(state), spec_id, dep_fm, set())
    if met or (camp or {}).get("state") == "MERGED":
        return True, "", live
    if live:
        why += f"; campaign {camp['id']} is {camp.get('state')}"
    return False, why, live


def after_met(cfg, state: dict, ref) -> tuple[bool, str]:
    """SPEC-0423: `(met, why)` for one `after` prerequisite (`ref` is an `after` entry or a bare
    id). `why` is the full `blocked_by` text when unmet, "" when met. An ordinary wait starts
    `waiting on <id> (` (the quiet stream holds it while the prerequisite has a live campaign);
    an unknown ref or an abandoned prerequisite is a different text, which wakes the conductor."""
    entry = ref if isinstance(ref, dict) else {
        "ref": str(ref), "kind": "idea" if _IDEA_ID_RE.match(str(ref)) else "spec"}
    rid, kind = entry["ref"], entry.get("kind") or "spec"
    if kind == "campaign":
        try:
            camp = store.read_state(cfg, rid)
        except (OSError, ValueError, KeyError):
            return False, f"after {rid}: campaign not found"
        st = camp.get("state")
        if st == "MERGED":
            return True, ""
        if st == "ABANDONED":
            return False, f"after {rid}: campaign {rid} abandoned"
        return False, f"waiting on {rid} (after: campaign {rid} is {st})"
    if kind == "idea":
        # A live or merged campaign answers the wait by itself. Reading the idea file
        # here reparsed the whole corpus once per queued item on every watch refresh
        # (SPEC-0454). The file is only needed when no campaign can answer.
        hits = [st for st in store.list_campaigns(cfg, include_terminal=True,
                                                  project=state.get("project"))
                if st.get("kind") == "idea" and _canon_id(_idea_id_of(st) or "") == _canon_id(rid)]
        newest = hits[0] if hits else None
        if (newest or {}).get("state") == "MERGED":
            return True, ""
        if newest is not None and newest.get("state") != "ABANDONED":
            spec = str(newest.get("spec_id") or "").strip()
            if spec:
                met, why, _live = _spec_after_met(cfg, state, spec)
                if met:
                    st = newest.get("state")
                    if st == "MERGED":
                        return True, ""
                    return False, (f"waiting on {newest['id']} (after: campaign {newest['id']} "
                                   f"is {st})")
                if why == f"{spec} not found":
                    return False, (f"waiting on {newest['id']} (after: campaign {newest['id']} "
                                   f"is {newest.get('state')})")
                return False, f"waiting on {spec} (after: {rid} -> {why})"
            return False, (f"waiting on {rid} (after: campaign {newest['id']} "
                           f"is {newest.get('state')})")
        task = _idea_task(cfg, rid)
        if task is None:
            return False, f"after {rid}: idea not found"
        spec = (task.properties.get("SPEC") or "").strip()
        if spec:
            met, why, _live = _spec_after_met(cfg, state, spec)
            if met:
                return True, ""
            if (newest or {}).get("state") != "ABANDONED":
                return False, f"waiting on {spec} (after: {rid} -> {why})"
        if newest is None:
            return False, f"waiting on {rid} (after: no campaign queued for {rid})"
        return False, f"after {rid}: campaign {newest['id']} abandoned"
    met, why, _live = _spec_after_met(cfg, state, rid)
    if met:
        return True, ""
    hits = store.list_campaigns(cfg, include_terminal=True, project=state.get("project"),
                                spec_id=rid)
    if hits and hits[0].get("state") == "ABANDONED" and why != f"{rid} not found":
        return False, f"after {rid}: campaign {hits[0]['id']} abandoned"
    if why.endswith("not found"):
        if hits and hits[0].get("state") not in TERMINAL_STATES:
            c = hits[0]
            return False, (f"waiting on {c['id']} (after: campaign {c['id']} is {c.get('state')})")
        return False, f"after {why}"
    return False, f"waiting on {rid} (after: {why})"


def _after_blocked(cfg, state: dict) -> list[str]:
    """The `blocked_by` entries of every unmet `after` prerequisite of `state` (SPEC-0423)."""
    raw_mail = state.get("conflict_mail")
    mailed = set(raw_mail if isinstance(raw_mail, list) else [])
    out = []
    for entry in after_entries(state):
        met, why = after_met(cfg, state, entry)
        if met:
            continue
        m = _NO_CAMPAIGN_AFTER_RE.match(why)
        if m and m.group(1) in mailed:
            why = f"mailed conductor: {m.group(1)} has no campaign ({state['id']})"
        if why not in out:
            out.append(why)
    return out


def resolve_dependency_conflicts(cfg, queued: list[dict]) -> None:
    """SPEC-0463: one conductor mail per waiter blocked on an idea with no campaign; records the
    marker on the QUEUED dicts the tick is about to persist."""
    from . import mailbox as M
    for st in queued:
        if st.get("state") != "QUEUED":
            continue
        raw = st.get("conflict_mail")
        mailed = list(raw if isinstance(raw, list) else [])
        st["conflict_mail"] = mailed
        seen = set(mailed)
        for entry in after_entries(st):
            met, why = after_met(cfg, st, entry)
            if met:
                continue
            m = _NO_CAMPAIGN_AFTER_RE.match(why)
            if not m:
                continue
            idea = m.group(1)
            if idea in seen:
                continue
            text = (f"{st['id']} waits on {idea}. {idea} has no campaign and no spec. "
                    f"Do not queue {idea}.")
            try:
                M.send(cfg, to="conductor", frm="conductor", kind="request", campaign=st["id"],
                       text=text, needs_reply=True)
            except (OSError, ValueError):
                continue
            mailed.append(idea)
            seen.add(idea)
        st["conflict_mail"] = mailed


def _find_dep_spec(specs_dir, dep) -> Path:
    """SPEC-0347: the spec file for dependency `dep` under `specs_dir`. Exported outbound specs
    are filed as `PROJ-NNNN-slug.md` (next to a `.contract.md` sidecar, never the spec), so the
    exact id prefix wins; else `specs._find_spec`'s `NNNN-*.md`. Raises ValueError, also for an id
    that is not a well-formed spec id (it would otherwise reach the glob)."""
    if not _is_dep_id(dep):
        raise ValueError(f"invalid dependency id {dep!r}")
    exact = [p for p in sorted(Path(specs_dir).glob(f"{dep}-*.md"))
             if not p.name.endswith(".contract.md")]
    return exact[0] if exact else SP._find_spec(specs_dir, dep)


def _dep_campaign(cfg, dep, project) -> dict | None:
    """SPEC-0347: the newest campaign (terminal included) of the outbound dependency `dep` in
    `project`, or None (internal ids: their merge fast-forwards the base, so the file is the
    truth there). An outbound PR merges remotely and the human's checkout may lag, so a `MERGED`
    campaign counts as done; `HANDED_OFF` (PR open) does not."""
    if not SP._is_outbound_spec_id(dep) or not (cfg or {}).get("data_dir"):
        return None
    hits = store.list_campaigns(cfg, include_terminal=True, project=project, spec_id=dep)
    return hits[0] if hits else None


def dependency_met(specs_dir, dep_id, dep_fm, seen) -> tuple[bool, str]:
    """(met, why) for a dependency (SPEC-0290). `done` is met. `superseded` is met only when its
    replacements are: the `superseded_by` target plus every spec whose `supersedes` lists it,
    recursively (`seen` guards cycles). With no readable replacement a `superseded` dependency
    stays met, as before. Any other status is unmet; `why` names what is pending."""
    status = str((dep_fm or {}).get("status") or "")
    if status == "done":
        return True, status
    if status not in DEPENDENCY_SATISFIED:
        return False, status or "no status"
    seen = seen | {dep_id}
    repl = {}                                   # id -> front matter
    for f in sorted(Path(specs_dir).glob("[0-9]*.md")):
        rfm = _read_frontmatter_text(f)
        if not rfm or not rfm.get("id") or rfm["id"] in seen:
            continue
        sup = rfm.get("supersedes") or []
        if rfm["id"] == (dep_fm or {}).get("superseded_by") or dep_id in (
                [sup] if isinstance(sup, str) else sup):
            repl[str(rfm["id"])] = rfm
    pending = [rid for rid, rfm in sorted(repl.items())
               if not dependency_met(specs_dir, rid, rfm, seen)[0]]
    if pending:
        return False, "superseded; waiting on " + ", ".join(pending)
    return True, status


def backlog_view(cfg, *, readiness=None) -> list[dict]:
    """Every QUEUED campaign in admission order with its live readiness (`nw queue backlog`,
    the `backlog` object of `nw.queue.v1`)."""
    queued = sorted((s for s in store.list_campaigns(cfg) if s.get("state") == "QUEUED"),
                    key=admission_key)
    rows = []
    for pos, st in enumerate(queued, 1):
        ready, blocked_by = (readiness or (lambda s: backlog_readiness(cfg, s)))(st)
        rows.append({"campaign": st.get("id"), "spec_id": st.get("spec_id"),
                     "project": st.get("project"), "priority": _priority(st), "position": pos, "ready": ready,
                     "blocked_by": blocked_by, "age_s": _age(st.get("created_at"))})
        if st.get("kind") == "idea":
            rows[-1]["idea_id"] = _idea_id_of(st)
        rv = row_view(cfg, st, rows[-1])                                # SPEC-0356
        rows[-1].update(kind=st.get("kind") or "spec", label=campaign_label(st),
                        **{k: rv[k] for k in ("title", "depends_on", "waiting_on",
                                              "park_reason", "cost_usd", "cap_usd")})
        if "after" in rv:                                               # SPEC-0423
            rows[-1]["after"] = rv["after"]
    return rows


def _record_backlog(cfg, st: dict, ready: bool, blocked_by: list) -> None:
    """Write the item's `backlog` object and one `backlog` event, only when it changed."""
    cur = st.get("backlog") or {}
    if "ready" in cur and cur["ready"] == ready and list(cur.get("blocked_by") or []) == blocked_by:
        return
    with store.item_lock(cfg, st["id"]):
        st["backlog"] = {"ready": ready, "blocked_by": blocked_by, "since": store.now_iso()}
        store.append_event(cfg, st["id"], "backlog", {"ready": ready, "blocked_by": blocked_by})
        store.write_state(cfg, st["id"], st)


# ---- state helpers -------------------------------------------------------------------------

def _counters(state: dict) -> dict:
    c = state.setdefault("counters", {})
    for k in ("loops", "runs", "gate_runs", "gate_retries", "timeout_regates", "triage_retries",
              "blocked_streak", "close_attempts", "parks", "review_retries"):
        c.setdefault(k, 0)
    return c


def _cost(state: dict) -> dict:
    c = state.setdefault("cost", {})
    c.setdefault("total_usd", 0.0)
    c.setdefault("by_persona", {})
    return c


def _begin_review(state: dict) -> None:
    """Entering REVIEWING always starts a fresh review (SPEC-0201): clear the previous
    verdicts and per-review counters, and remember the run count so reviewers never see runs
    of this pass-through."""
    counters = _counters(state)
    PK.remember_verdicts(state)                     # SPEC-0554: no-op unless pinned
    PK.forget(state)                                # SPEC-0554: each entry builds its packet
    lr = state.get("last_review") if isinstance(state.get("last_review"), dict) else {}
    for k in ("verdicts", "triage", "triager_run", "waived", "collation"):   # SPEC-0555
        lr.pop(k, None)
    lr["entry_run"] = counters["runs"]
    state["last_review"] = lr
    counters["review_retries"] = 0
    counters.pop("review_retries_by_persona", None)


def _transition(cfg, state: dict, to: str, reason: str, *, by="tick", extra_events=(),
                withdraw_reason=None):
    """Record `extra_events` then the transition, and write the state — all under the
    campaign lock, so the ledger and `state.json` cannot disagree. Mutates and returns
    `state`."""
    cid = state["id"]
    with store.item_lock(cfg, cid):
        for etype, payload in extra_events:
            store.append_event(cfg, cid, etype, payload, by=by)
        frm = state.get("state")
        if to != frm:
            if to == "REVIEWING":
                _begin_review(state)
            if to == "CLOSING":                  # SPEC-0220: the close retry budget restarts
                # SPEC-0260: a fresh entry joins the back of its repository's close train
                state["closing"] = {"attempts": 0, "entered_at": store.now_iso()}
            elif frm == "CLOSING":
                state.pop("closing", None)
            state["prev_state"] = frm
            state["state"] = to
            tr = store.append_event(cfg, cid, "transition",
                                    {"from": frm, "to": to, "reason": reason}, by=by)
        store.write_state(cfg, cid, state)
        if to in TERMINAL_STATES:
            # SPEC-0253/0287: no child of a dead campaign needs it (HANDED_OFF too)
            store.clear_campaign_tmp(cfg, cid)
            # SPEC-0232: nobody will answer a dead campaign
            from . import escalations as ESC
            ESC.withdraw_for_campaign(cfg, cid, withdraw_reason or f"campaign {to}", by)
        if to != frm and to in STEWARD_NOTIFY_STATES:
            _notify_steward(cfg, state, to, reason, tr.get("seq"), by)
    return state


# SPEC-0325: the transitions a project's steward is told about (one `info` mail each)
STEWARD_NOTIFY_STATES = frozenset({"HANDED_OFF", "MERGED", "PARKED", "ABANDONED"})
_STEWARD_NOTIFIED_KEEP = 20


def _steward_notice_text(state: dict, to: str, reason: str) -> str:
    close = state.get("close") or {}
    spec = state.get("spec_id") or (state.get("spec") or {}).get("id") or "-"
    bits = [f"queue: campaign {state['id']} ({spec}) is {to} ({reason})"]
    if close.get("pr_url"):
        bits.append(f"PR {close['pr_url']}")
    if close.get("merged_sha"):
        bits.append(f"merged {str(close['merged_sha'])[:10]}")
    if to == "PARKED" and (state.get("park") or {}).get("detail"):
        bits.append(f"detail: {' '.join(str(state['park']['detail']).split())[:200]}")
    bits.append(f"cost ${float((state.get('cost') or {}).get('total_usd') or 0.0):.2f}")
    return "; ".join(bits)


def _notify_steward(cfg, state: dict, to: str, reason: str, seq, by: str) -> None:
    """SPEC-0325: mail one `info` message (no reply wanted) to `steward:<project>` about this
    transition, only when that steward's mailbox has an armed follower. Called by `_transition`
    under the campaign lock. Idempotent per transition: the ledger `seq` of the transition is
    kept in `state["steward_notified"]`, so a re-run never mails it twice. Never raises: a failed
    send is a ledger `note`, and the transition stands. Sent `from: conductor`, the address the
    queue already mails from (`escalations.withdraw`); the conductor's own wakeups are untouched
    (only conductor-bound mail is mirrored into the event stream)."""
    from . import events as E
    from . import mailbox as M
    try:
        project = (state.get("project") or "").strip()
        if not project:
            return
        key = f"{to}#{seq}"
        done = list(state.get("steward_notified") or [])
        if key in done:
            return
        if E.reader_holder(cfg, f"mailbox:steward:{project}") is None:
            return                        # nobody is listening: no mail, no marker
        addr = M.validate_address(cfg, f"steward:{project}")
    except Exception:                     # noqa: BLE001 — an unknown project has no steward
        return
    try:
        M.send(cfg, to=addr, frm="conductor", kind="info", campaign=state["id"],
               needs_reply=False, text=_steward_notice_text(state, to, reason))
    except Exception as e:                # noqa: BLE001 — a mail failure never undoes the transition
        try:
            store.append_event(cfg, state["id"], "note", {
                "text": f"steward notice to {addr} failed: {e or type(e).__name__}",
                "author": "queue"}, by=by)
        except Exception:                 # noqa: BLE001
            pass
        return
    try:
        state["steward_notified"] = (done + [key])[-_STEWARD_NOTIFIED_KEEP:]
        store.write_state(cfg, state["id"], state)
    except Exception:                     # noqa: BLE001 — the mail went out; the marker is best-effort
        pass


def _park(cfg, state: dict, reason: str, detail: str, resume_to: str, *, by="tick",
          extra_events=(), extra=None, count=True, budget=None, auto_ok=True):
    """Park `state`. `extra` is merged into `state["park"]` (SPEC-0232 `escalated_from`);
    `count=False` keeps a PARKED→PARKED re-park out of `counters.parks`. `budget` is the run a
    `cost_cap` park blocked (SPEC-0302); `auto_ok=False` keeps the queue from deciding it. A park the queue decides itself carries `park.auto`;
    its `wait` decision record is written by the tick, never here: `_park` runs under the
    campaign lock and `decisions.record` would take the decisions lock after it."""
    if count:
        _counters(state)["parks"] += 1
    state["park"] = {"reason": reason, "detail": detail, "since": store.now_iso(),
                     "resume_to": resume_to, **(extra or {})}
    if state.get("kind") in RM.RED_KINDS:
        # SPEC-0420: the queue decides a red-base fix's park (the episode counts it failed)
        state["park"]["red_main"] = state.get("red_episode")
    # SPEC-0318: a deferred park request carries the decision to record once the park applies
    pending = (state.get("park_requested") or {}).get("decision_request")
    if isinstance(pending, dict) and "decision_request" not in state["park"]:
        state["park"]["decision_request"] = pending
    state.pop("park_requested", None)
    extra_events = list(extra_events)
    if reason in ("base_moving", "cost_cap") and by == "tick" and auto_ok:
        import datetime as dt
        try:                  # a bad config value or a bug must never block the park itself
            sett = AP.settings(cfg)
            auto = (AP.decide(state, store.read_ledger(cfg, state["id"],
                                                       types=["open", "resume", "run.end"]),
                              sett, dt.datetime.now().astimezone(), budget=budget)
                    if sett["enabled"] else None)
        except Exception as e:
            auto = None
            extra_events.append(("note", {"text": f"autopark.decide failed ({e!r}); the park is "
                                                  "the conductor's", "author": "queue"}))
        if auto is not None:
            state["park"]["auto"] = auto.park_data()
    ev_park = {k: v for k, v in state["park"].items() if k != "decision_request"}
    events = extra_events + [("park", ev_park)]
    return _transition(cfg, state, "PARKED", reason, by=by, extra_events=events)


def _known_spend(cost: dict) -> float:
    """Dollars a run reported. An unknown cost lives on `estimated_usd` and is not spend
    (SPEC-0461). A state written before that still has the estimate folded into `total_usd`."""
    total = float(cost.get("total_usd") or 0.0)
    if cost.get("estimate_separated"):
        return total
    estimated = float(cost.get("estimated_usd") or 0.0)
    return round(max(0.0, total - estimated), 4)


def _separate_estimate(cost: dict) -> None:
    """Move a folded estimate out of `total_usd` once, so later known charges are not
    reduced again."""
    if cost.get("estimate_separated"):
        return
    estimated = float(cost.get("estimated_usd") or 0.0)
    if estimated:
        cost["total_usd"] = round(max(0.0, float(cost.get("total_usd") or 0.0) - estimated), 4)
    cost["estimate_separated"] = True


def _record_run(state: dict, persona: str, run_id: str, result, budget: float, *,
                interrupted: bool = False) -> list:
    """Cost + run.end + findings for a finished persona run. Returns ledger events; mutates
    the counters and cost in `state`. `interrupted` is the step's view (the loop was told to
    stop while the run was in flight), which the RunResult cannot know.

    A missing cost is not spend (SPEC-0461). The budget is recorded on `estimated_usd` and
    is not added to the total the cap checks."""
    interrupted = bool(interrupted or result.interrupted)
    session_marks = SS.record(state, persona, run_id, result, interrupted=interrupted)  # SPEC-0556
    cost = _cost(state)
    _separate_estimate(cost)
    estimated = result.cost_usd is None
    charged = 0.0 if estimated else float(result.cost_usd)
    cost["total_usd"] = round(float(cost["total_usd"]) + charged, 4)
    if estimated:
        cost["estimated_usd"] = round(float(cost.get("estimated_usd") or 0.0) + float(budget), 4)
    else:
        cost["by_persona"][persona] = round(float(cost["by_persona"].get(persona, 0.0))
                                            + charged, 4)
    events = [("run.end", {
        "persona": persona, "run_id": run_id, "exit_code": result.exit_code,
        "is_error": result.is_error, "timed_out": result.timed_out,
        "interrupted": interrupted, "duration_s": result.duration_s,
        "cost_usd": result.cost_usd, "charged_usd": charged, "cost_estimated": estimated,
        "usage": result.usage,
        "session_id": result.session_id, "verdict_ok": result.verdict_ok,
        "verdict": result.verdict,
        "result_tail": (result.result_text or "")[-400:] if not result.verdict_ok else "",
        **({"worker": result.worker} if getattr(result, "worker", None) else {}),
        **({"writeback_failed": True, "saved_refs": list(result.saved_refs)}
           if getattr(result, "writeback_failed", False) else {}),
        **session_marks,
    }), ("cost", {"persona": persona, "delta_usd": charged, "total_usd": cost["total_usd"],
                  "cap_usd": cost.get("cap_usd"), "estimated": estimated})]
    if estimated:
        reason = ("interrupted" if interrupted else "timed out" if result.timed_out
                  else "no cost reported")
        events.append(("finding", {"persona": persona, "severity": "info", "gate": None,
                                   "title": "run cost unknown; not counted against the cap",
                                   "detail": f"${budget:.2f} not counted ({reason})",
                                   "files": []}))
    verdict = result.verdict if isinstance(result.verdict, dict) else {}
    for item in verdict.get("must_fix") or []:
        if isinstance(item, dict):
            events.append(("finding", {"persona": persona, "severity": "must_fix",
                                       "gate": item.get("gate"), "title": item.get("title"),
                                       "detail": item.get("detail"),
                                       "files": list(item.get("files") or [])}))
    for blocker in verdict.get("blockers") or []:
        events.append(("finding", {"persona": persona, "severity": "major", "gate": None,
                                   "title": "implementer blocker", "detail": str(blocker),
                                   "files": []}))
    for item in verdict.get("findings") or []:        # reviewers (SPEC-0187, SPEC-0202)
        if isinstance(item, dict):
            ev = {"persona": persona, "severity": item.get("severity"),
                  "gate": None, "ac": item.get("ac"),
                  "title": item.get("title"), "detail": finding_detail(item),
                  "files": list(item.get("files") or [])}
            if item.get("category"):
                ev["category"] = item["category"]
            events.append(("finding", ev))
    denials = _raw_permission_denials(result)
    if denials:
        events.append(("finding", {"persona": persona, "severity": "info", "gate": None,
                                   "title": "permission denials during the run",
                                   "detail": str(denials)[:800], "files": []}))
    for i, (etype, payload) in enumerate(events):      # SPEC-0408: the run that produced them
        if etype == "finding":
            events[i] = (etype, {**payload, "run_id": run_id})
    return events


def _raw_permission_denials(result):
    """`permission_denials` from the raw claude result, when the runner saved one."""
    try:
        import json
        if result.raw_path and Path(result.raw_path).is_file():
            obj = json.loads(Path(result.raw_path).read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                return obj.get("permission_denials") or []
    except (OSError, ValueError):
        pass
    return []


def _spec_sha(path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


# ---- step execution --------------------------------------------------------------------

class ProjectConfigError(ValueError):
    """The project's `.nw/project.yaml` could not be loaded (SPEC-0194): parks as
    `config_invalid`, and the campaign resumes by itself once the config loads again."""


@dataclass
class StepResult:
    """What a step produced. Applied on the main thread by `_apply`."""
    campaign: str
    step: Step
    from_state: str
    run_id: str | None = None
    run_result: object = None
    budget: float = 0.0
    commit_sha: str | None = None
    commit_message: str | None = None
    gate_run: object = None
    close_outcome: object = None
    prepared: object = None                          # SPEC-0220: a close's PreparedClose
    resolution: dict | None = None                   # SPEC-0221: {verified, failed, head}
    error: str | None = None
    interrupted: bool = False
    events_before: list = field(default_factory=list)
    config_error: bool = False
    reviews: list = field(default_factory=list)      # per-reviewer StepResults, profile order
    followups: dict | None = None                    # SPEC-0207: the close step's filer summary
    crashed: str | None = None                       # unexpected worker exception caught in-step
    write: dict | None = None                        # SPEC-0215: a write_spec step's outcome
    detached: bool = False                           # SPEC-0225: the pool detached the run
    lock_cleanup: dict | None = None                 # SPEC-0244: an adopted commit's index.lock outcome
    spec_review: dict | None = None                  # SPEC-0214: a spec_review step's outcome
    deferred: str | None = None                      # SPEC-0341: a gate step that did nothing yet
    revert_conflict: str | None = None               # SPEC-0420: a revert open that conflicted
    resumed_to: str | None = None                    # SPEC-0463: adopt into an existing worktree


_PROGRESS_MIN_INTERVAL_S = 1.0      # SPEC-0313: at most one `progress_at` lease write per second


class _Ctx:
    """Per-tick context: cfg, resolved profiles / project configs, the runner, the stop flag."""

    def __init__(self, cfg, *, stop: threading.Event | None = None, runner=None, lease=None):
        self.cfg = cfg
        self.stop = stop or threading.Event()
        self.lease = lease            # SPEC-0228: where `_busy` declares long main-thread steps
        self._injected_runner = runner
        self._runners: dict = {}
        self._profiles: dict = {}
        self._projects: dict = {}
        self._progress_mono: float | None = None

    def progress(self) -> None:
        """SPEC-0313: stamp `progress_at` in the lease info — the loop is alive inside a long
        tick. Throttled to once per `_PROGRESS_MIN_INTERVAL_S`; does nothing without a lease."""
        if self.lease is None:
            return
        mono = time.monotonic()
        if self._progress_mono is not None and mono - self._progress_mono < _PROGRESS_MIN_INTERVAL_S:
            return
        self._progress_mono = mono
        self.lease.update_info(progress_at=store.now_iso())

    @property
    def runner(self):
        return self.runner_for(None)

    def runner_for(self, state=None, *, kind=None):
        """The persona backend for this spawn (SPEC-0436). An injected test runner wins.
        `kind` is the model set's runner for this persona (SPEC-0455)."""
        if self._injected_runner is not None:
            return self._injected_runner
        if not kind:
            kind = resolve_runner(self.cfg, campaign=state,
                                  project=(state or {}).get("project") if state else None)
        if kind not in self._runners:
            self._runners[kind] = select_runner(self.cfg, runner=kind)
        return self._runners[kind]

    def profile(self, state) -> dict:
        name = state.get("profile")
        root = (state.get("repo") or {}).get("root")        # SPEC-0237: the main checkout
        if (name, root) not in self._profiles:
            self._profiles[name, root] = resolve_profile(self.cfg, name, project_root=root)
        return self._profiles[name, root]

    def cost_args(self, profile) -> dict:
        """SPEC-0422: `plan_step`'s `reserves` / `ceiling` for `profile` under the cost-cap
        policy; `{}` when the policy is off. The run-cost statistics are refreshed once per
        tick; a failure there falls back to the full budgets (the old reserve)."""
        if not hasattr(self, "_cost_sett"):
            self._cost_sett = CP.settings(self.cfg)
            self._cost_stats = None
            if self._cost_sett["enabled"]:
                try:
                    self._cost_stats = CP.refresh(self.cfg)
                except (OSError, ValueError) as e:
                    journal(self.cfg, "cost_stats_failed", error=str(e))
        if not self._cost_sett["enabled"]:
            return {}
        return {"reserves": CP.reserves(self.cfg, profile, stats=self._cost_stats or {},
                                        sett=self._cost_sett),
                "ceiling": CP.ceiling(profile, self._cost_sett)}

    def project(self, state) -> dict:
        name = state.get("project")
        if name not in self._projects:
            try:
                self._projects[name] = P.project_config(self.cfg, name)
            except ValueError as e:
                raise ProjectConfigError(str(e)) from e
        return self._projects[name]


@contextmanager
def _busy(lease, kind: str, *, campaign: str | None = None, budget_s: float):
    """Declare a long main-thread step (`open` | `close` | `drain`) in the lease info for the
    out-of-process watchdog (SPEC-0228): the loop cannot tick meanwhile, so a silent journal
    is not a stall within `budget_s`. Cleared in `finally`. Without a lease (one-shot ticks,
    tests) it does nothing. A new long main-thread step must use it."""
    if lease is None:
        yield
        return
    prev = lease.info.get("busy")     # a harvest inside the drain can run a nested close
    lease.update_info(busy={"kind": kind, "campaign": campaign, "since": store.now_iso(),
                            "budget_s": budget_s})
    try:
        yield
    finally:
        lease.end_busy(prev)          # one write: drop `busy` and stamp `busy_ended_at`


def _do_open(ctx: _Ctx, state: dict) -> StepResult:
    """Create the worktree + branch, run the project's setup commands, record base sha and
    spec hash. Serial (touches the main repository). Declared `busy` (SPEC-0228): the setup
    commands (`uv sync`, `npm ci`) run here with no timeout — both call sites of `_tick`."""
    with _busy(ctx.lease, "open", campaign=state["id"],
               budget_s=events_settings(ctx.cfg)["open_budget_s"]):
        return _do_open_unwrapped(ctx, state)


def worktree_resume_state(state: dict, worktree: Path) -> tuple[str | None, str | None]:
    """Target state for resuming into an existing checkout (SPEC-0463): `(state, error)`."""
    spec = state.get("spec") or {}
    rel = spec.get("rel_path")
    admission = state.get("admission")
    file_id = spec.get("id")
    campaign_spec_id = file_id or state.get("spec_id")
    pending = state.get("pending_brief")
    wt_resolved = worktree.resolve()
    spec_path = None
    if rel:
        spec_path = (worktree / rel).resolve()
        if not spec_path.is_relative_to(wt_resolved):
            return None, f"spec path escapes worktree: {rel}"
    if not rel or spec_path is None:
        if has_no_spec(state) or admission == "revise_pending":
            return "SPEC_WRITING", None
        if campaign_spec_id:
            return None, f"spec file missing at {spec_path or rel}"
        return "SPEC_WRITING", None
    data, read_err = _read_worktree_spec_bytes(worktree, rel)
    if read_err is not None:
        return None, f"spec file unreadable at {spec_path}: {read_err}"
    if data is None:
        if has_no_spec(state) or admission == "revise_pending":
            return "SPEC_WRITING", None
        if campaign_spec_id:
            return None, f"spec file missing at {spec_path}"
        return "SPEC_WRITING", None
    try:
        fm, _ = split_frontmatter(_spec_bytes_as_text(data))
    except (ValueError, UnicodeDecodeError, yaml.YAMLError) as e:
        return None, f"spec file unreadable at {spec_path}: {e}"
    if not isinstance(fm, dict):
        return None, f"spec file unreadable at {spec_path}: front matter is not a mapping"
    status = str(fm.get("status") or "").strip()
    if status in ("accepted", "in-progress"):
        if admission == "revise_pending":
            return "SPEC_WRITING", None
        return "IMPLEMENTING", None
    if status == "proposed" and file_id and not pending:
        return "SPEC_REVIEW", None
    if status in ("draft", "proposed") and (not file_id or pending):
        return "SPEC_WRITING", None
    if status == "draft" and file_id and not pending:
        return None, "draft spec has an id and an empty pending_brief"
    return None, f"spec status is {status or 'empty'}"


_WORKTREE_CONFLICT_MAIL_STATUSES = frozenset(
    {"done", "rejected", "superseded", "empty", "unreadable"})


def _worktree_conflict_mail_status(err: str) -> str | None:
    if err.startswith("spec status is "):
        st = err.removeprefix("spec status is ").strip()
        if not st or not st.split():
            return "empty"
        if st.split()[0] in _WORKTREE_CONFLICT_MAIL_STATUSES:
            return st.split()[0]
    if err.startswith("spec file unreadable"):
        return "unreadable"
    return None


def _spec_snapshot_bytes_match_hash(data: bytes, want_hash: str) -> bool:
    """True when ``data`` hashes to ``want_hash``, including legacy LF copies of CRLF opens."""
    if not data:
        return False
    if hashlib.sha256(data).hexdigest() == want_hash:
        return True
    try:
        text = data.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError:
        return False
    crlf = text.replace("\n", "\r\n").encode("utf-8")
    return hashlib.sha256(crlf).hexdigest() == want_hash


def _read_bounded_regular_file(path: Path, *, max_bytes: int) -> tuple[bytes | None, str | None]:
    """Read a local path with ``O_NOFOLLOW`` and a byte cap."""
    try:
        st = os.lstat(path)
    except OSError as e:
        if e.errno == errno.ENOENT:
            return None, None
        return None, str(e)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return None, "not a regular file"
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as e:
        return None, str(e)
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            buf = os.read(fd, 1 << 20)
            if not buf:
                break
            total += len(buf)
            if total > max_bytes:
                return None, f"spec file exceeds {max_bytes} bytes"
            chunks.append(buf)
    except OSError as e:
        return None, str(e)
    finally:
        os.close(fd)
    return b"".join(chunks), None


def _read_spec_at_open_snapshot_bytes(cdir: Path, snap: Path) -> tuple[bytes | None, str | None]:
    """Read ``spec_at_open.md`` when it is a regular file under ``cdir``."""
    try:
        cdir_res = cdir.resolve()
        resolved = snap.resolve()
    except OSError as e:
        return None, str(e)
    if not resolved.is_relative_to(cdir_res):
        return None, "path escapes campaign dir"
    return _read_bounded_regular_file(snap, max_bytes=MAX_WORKTREE_SPEC_BYTES)


def _spec_at_open_snapshot_ok(cdir: Path, snap: Path, want_hash: str) -> bool:
    """True when ``snap`` is a non-empty regular file in ``cdir`` matching ``want_hash``."""
    data, err = _read_spec_at_open_snapshot_bytes(cdir, snap)
    return err is None and data is not None and _spec_snapshot_bytes_match_hash(data, want_hash)


MAX_WORKTREE_SPEC_BYTES = 4 * 1024 * 1024


def _spec_bytes_as_text(data: bytes) -> str:
    """Decode spec snapshot bytes, normalizing CRLF the way ``set_spec_status`` writes them."""
    return data.decode("utf-8").replace("\r\n", "\n")


def _validate_spec_snapshot_bytes(data: bytes) -> str | None:
    """Return an error when ``data`` is not a storable spec snapshot (UTF-8 + YAML front matter)."""
    try:
        text = _spec_bytes_as_text(data)
    except UnicodeDecodeError as e:
        return f"not valid UTF-8: {e}"
    try:
        fm, _ = split_frontmatter(text)
    except (ValueError, yaml.YAMLError) as e:
        return f"front matter: {e}"
    if not isinstance(fm, dict):
        return "front matter is not a mapping"
    return None


def _read_worktree_spec_bytes(worktree: Path, rel: str) -> tuple[bytes | None, str | None]:
    """Read a worktree spec path when it is a regular file inside the worktree (``O_NOFOLLOW``).

    Returns ``(data, None)`` on success, ``(None, None)`` when absent, or ``(None, detail)`` on error."""
    wt_resolved = worktree.resolve()
    raw = worktree / rel
    try:
        resolved = raw.resolve()
    except OSError as e:
        return None, str(e)
    if not resolved.is_relative_to(wt_resolved):
        return None, "path escapes worktree"
    return _read_bounded_regular_file(raw, max_bytes=MAX_WORKTREE_SPEC_BYTES)


def _unlink_spec_at_open_entry(snap: Path) -> None:
    """Remove ``spec_at_open.md`` without following it as a symlink target."""
    try:
        os.lstat(snap)
    except OSError:
        return
    try:
        os.unlink(snap)
    except OSError:
        pass


def _clear_untrusted_spec_at_open_snapshot(cdir: Path, snap: Path, spec: dict) -> None:
    """Drop an untrusted open-time spec copy so close cannot treat it as a drift baseline."""
    want = spec.get("sha256_at_open")
    if want and _spec_at_open_snapshot_ok(cdir, snap, want):
        return
    spec.pop("sha256_at_open", None)
    _unlink_spec_at_open_entry(snap)


def _replace_spec_at_open_snapshot(cdir: Path, snap: Path, data: bytes, spec: dict) -> str | None:
    """Write ``spec_at_open.md`` from ``data`` via a private temp file and ``os.replace``."""
    bad = _validate_spec_snapshot_bytes(data)
    if bad:
        want = spec.get("sha256_at_open")
        if not (want and _spec_at_open_snapshot_ok(cdir, snap, want)):
            spec.pop("sha256_at_open", None)
        return f"spec snapshot not storable: {bad}"
    tmp = cdir / f".{CL.SPEC_AT_OPEN}.{os.getpid()}.{threading.get_ident()}.tmp"
    fd = None
    try:
        cdir.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(tmp, flags, 0o644)
        written = 0
        while written < len(data):
            n = os.write(fd, data[written:])
            if n <= 0:
                raise OSError("short write to spec snapshot temp")
            written += n
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(tmp, snap)
        spec["sha256_at_open"] = hashlib.sha256(data).hexdigest()
        return None
    except (OSError, ValueError):
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            _unlink_spec_at_open_entry(tmp)
        except OSError:
            pass
        want = spec.get("sha256_at_open")
        if not (want and _spec_at_open_snapshot_ok(cdir, snap, want)):
            spec.pop("sha256_at_open", None)
        return f"could not write spec snapshot at {snap}"


def _adopt_spec_open_snapshot(cfg, state: dict, worktree: Path) -> str | None:
    """Create or restore ``spec_at_open.md`` on worktree adopt when missing or untrusted."""
    spec = state.setdefault("spec", {})
    rel_p = spec.get("rel_path")
    cdir = store.campaign_dir(cfg, state["id"])
    snap = cdir / CL.SPEC_AT_OPEN
    want = spec.get("sha256_at_open")
    if want and _spec_at_open_snapshot_ok(cdir, snap, want):
        return None
    admission = state.get("admission")
    if not rel_p:
        if has_no_spec(state) or admission == "revise_pending":
            _clear_untrusted_spec_at_open_snapshot(cdir, snap, spec)
        return None
    data, read_err = _read_worktree_spec_bytes(worktree, rel_p)
    if data is None and read_err is None:
        if has_no_spec(state) or admission == "revise_pending":
            _clear_untrusted_spec_at_open_snapshot(cdir, snap, spec)
            return None
        spec.pop("sha256_at_open", None)
        return f"spec file missing at {worktree / rel_p}"
    if read_err is not None:
        spec.pop("sha256_at_open", None)
        return f"spec file unreadable at {worktree / rel_p}: {read_err}"
    if TR.tiers_on(state):
        # SPEC-0553: this snapshot is the worktree's spec, which in-campaign edits may have
        # touched: its size/tone are not hand values (`_lean_open` reads them as absent)
        spec["snapshot_adopted"] = True
    return _replace_spec_at_open_snapshot(cdir, snap, data, spec)


def _worktree_conflict_mail(cfg, state: dict, worktree: Path, status: str) -> None:
    """One conductor mail when an adopted worktree's spec cannot be resumed (SPEC-0463)."""
    from . import mailbox as M
    marker = f"worktree:{status}"
    raw = state.get("conflict_mail")
    mailed = list(raw if isinstance(raw, list) else [])
    if marker in mailed:
        return
    repo = state.get("repo") or {}
    text = (f"{state['id']} already has worktree {worktree} on {repo.get('branch')}, "
            f"and its spec status is {status}. Not opened.")
    try:
        M.send(cfg, to="conductor", frm="conductor", kind="request", campaign=state["id"],
               text=text, needs_reply=True)
    except (OSError, ValueError):
        return
    mailed.append(marker)
    state["conflict_mail"] = mailed


def _do_open_unwrapped(ctx: _Ctx, state: dict) -> StepResult:
    res = StepResult(state["id"], Step("open"), "OPENING")
    repo = state.setdefault("repo", {})
    if state.get("kind") == "merge":
        return _do_open_merge(ctx, state, res)
    try:
        ctx.profile(state)             # SPEC-0237: a broken profile file fails the open, no worktree
        pcfg = ctx.project(state)
        root = Path(repo["root"])
        base = repo.get("base") or pcfg.get("base") or "main"
        worktree = Path(repo["worktree"])
        branch = repo["branch"]
        kind = state.get("kind")
        if kind not in ("merge", "split", "revert", *RM.RED_KINDS):
            elsewhere = git.worktree_for_branch(root, branch)
            if elsewhere is not None and elsewhere == worktree.resolve():
                if not repo.get("base_sha_at_open"):
                    base_sha = git.rev_parse(root, _open_start(root, pcfg, base))
                    try:
                        _run_worktree_setup(pcfg, worktree)
                    except (GitError, ValueError, OSError) as e:
                        res.error = getattr(e, "tail", None) or str(e)
                        res.config_error = isinstance(e, ProjectConfigError)
                        return res
                    repo["base_sha_at_open"] = base_sha
                repo["head"] = git.rev_parse(worktree)
                if kind not in RM.RED_KINDS and not has_no_spec(state):
                    spec = state.setdefault("spec", {})
                    rel_p = spec.get("rel_path")
                    if rel_p:
                        spec_file = (worktree / rel_p).resolve()
                        if not spec_file.is_relative_to(worktree.resolve()):
                            res.error = f"spec path escapes worktree: {rel_p}"
                            return res
                target, err = worktree_resume_state(state, worktree)
                if err:
                    res.error = err
                    mail_st = _worktree_conflict_mail_status(err)
                    if mail_st:
                        _worktree_conflict_mail(ctx.cfg, state, worktree, mail_st)
                elif target is not None:
                    res.resumed_to = target
                    if kind not in RM.RED_KINDS:
                        if has_no_spec(state):
                            if target in ("IMPLEMENTING", "SPEC_REVIEW"):
                                snap_err = _adopt_spec_open_snapshot(
                                    ctx.cfg, state, worktree)
                                if snap_err:
                                    res.error = snap_err
                                    res.resumed_to = None
                            else:
                                cdir = store.campaign_dir(ctx.cfg, state["id"])
                                _clear_untrusted_spec_at_open_snapshot(
                                    cdir, cdir / CL.SPEC_AT_OPEN,
                                    state.setdefault("spec", {}))
                        else:
                            snap_err = _adopt_spec_open_snapshot(ctx.cfg, state, worktree)
                            if snap_err:
                                res.error = snap_err
                                res.resumed_to = None
                return res
        base_sha = git.rev_parse(root, _open_start(root, pcfg, base))
        # the sha, not `<remote>/<base>`: no upstream is set on the campaign branch
        git.worktree_add(root, worktree, branch, base_sha)
        _run_worktree_setup(pcfg, worktree)
        repo["base_sha_at_open"] = base_sha
        if state.get("kind") == "revert":                       # SPEC-0420 §6
            res.revert_conflict = CL.revert_campaign_range(state, worktree, pcfg,
                                                         res.events_before)
        repo["head"] = git.rev_parse(worktree)
        if state.get("kind") in RM.RED_KINDS:
            pass                                                # SPEC-0420: no spec
        elif not has_no_spec(state):
            spec = state.setdefault("spec", {})
            # SPEC-0186: keep the accepted text so the close step can detect spec drift
            rel_p = spec.get("rel_path") or ""
            if rel_p:
                cdir = store.campaign_dir(ctx.cfg, state["id"])
                snap = cdir / CL.SPEC_AT_OPEN
                want = spec.get("sha256_at_open")
                if want and _spec_at_open_snapshot_ok(cdir, snap, want):
                    pass
                else:
                    data, read_err = _read_worktree_spec_bytes(worktree, rel_p)
                    if read_err is not None:
                        res.error = (f"spec file unreadable at {worktree / rel_p}: {read_err}")
                    elif data is not None:
                        snap_err = _replace_spec_at_open_snapshot(cdir, snap, data, spec)
                        if snap_err:
                            res.error = snap_err
    except (GitError, ValueError, OSError) as e:
        res.error = getattr(e, "tail", None) or str(e)
        res.config_error = isinstance(e, ProjectConfigError)
    return res


def _spec_hand(cfg, state: dict, *, worktree: bool = False) -> dict | None:
    """SPEC-0553: the hand-written `{size, tone}` of the campaign's spec — from the snapshot
    taken at open (the spec as committed on the base branch), or with `worktree` from the
    campaign worktree as it is now. None when the spec has neither key or cannot be read."""
    from .profiles import hand_sizing
    rel = (state.get("spec") or {}).get("rel_path")
    if worktree:
        wt = (state.get("repo") or {}).get("worktree")
        path = Path(wt) / rel if wt and rel else None
    else:
        path = store.campaign_dir(cfg, state["id"]) / CL.SPEC_AT_OPEN
    if path is None or not path.is_file():
        return None
    try:
        fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    except Exception:                  # noqa: BLE001 — unreadable / bad YAML: no hand values
        return None
    return hand_sizing(fm or {})


def _lean_open(ctx: _Ctx, state: dict, events: list) -> None:
    """SPEC-0553, at open with `lean.tiers` pinned: re-apply the cap with the size/tone the
    human wrote into the spec on the base branch (the `spec_at_open` snapshot). A changed pick
    re-resolves the profile (name, sources, sha, cost cap); `raised` marks a campaign above
    the default (it gets the deep close pass). Once per campaign (`sizing.hand` is the mark)."""
    if not TR.tiers_on(state):
        return
    sizing = state.get("sizing") if isinstance(state.get("sizing"), dict) else {}
    if "hand" in sizing:
        return
    untrusted = bool((state.get("spec") or {}).get("snapshot_adopted"))
    hand = None if untrusted else _spec_hand(ctx.cfg, state)
    if untrusted:
        journal(ctx.cfg, "lean_hand_untrusted", campaign=state["id"],
                reason="spec_at_open re-snapshotted from the worktree; size/tone read as absent")
    root = (state.get("repo") or {}).get("root")
    review = ({"size": sizing["size"], "tone": sizing["tone"], "verdict": "approve"}
              if sizing.get("size") and sizing.get("tone") else None)
    pick = select_profile(ctx.cfg, flag=sizing.get("flag"), review=review, project_root=root,
                          hand=hand, lean=True)
    sizing = dict(sizing, hand=hand)
    copy = ["sizing"]
    if hand or pick["profile"] != state.get("profile"):
        prof = resolve_profile(ctx.cfg, pick["profile"], project_root=root)
        state["profile"] = prof["name"]
        state["profile_sources"] = list(prof.get("sources") or ())
        state["profile_sha256"] = profile_sha256(prof)
        state.setdefault("cost", {})["cap_usd"] = prof.get("campaign_cap_usd")
        sizing.update(source=pick["source"], reason=pick["reason"], size=pick["size"],
                      tone=pick["tone"])
        copy += ["profile", "profile_sources", "profile_sha256"]
    state["sizing"] = sizing
    state["raised"] = bool(pick.get("raised"))
    state["_lean_copy"] = tuple(copy)
    events.append(("lean_sizing", {"hand": hand, "profile": state.get("profile"),
                                   "source": sizing.get("source"),
                                   "reason": sizing.get("reason"),
                                   "raised": state["raised"],
                                   **({"hand_untrusted": True} if untrusted else {})}))


def _lean_cap_check(cfg, state: dict, events: list, step: str) -> None:
    """SPEC-0553: a `size`/`tone` added or changed in the spec inside the campaign (the
    writer's or the implementer's commit) never sizes it; report it once per value as a
    `cap_ignored` event."""
    now = _spec_hand(cfg, state, worktree=True)
    hand = (state.get("sizing") or {}).get("hand")
    if (now or None) == (hand or None) or state.get("cap_ignored") == now:
        return
    state["cap_ignored"] = now
    events.append(("cap_ignored", {"spec": now, "hand": hand, "step": step,
                                   "text": "size/tone written inside the campaign is ignored "
                                           "for sizing; only the spec on base at open counts"}))


def _open_start(root: Path, pcfg: dict, base: str) -> str:
    """The ref a new campaign branch starts from (SPEC-0325). `merge` policy: the local `base`
    (nightwright merges into it locally). `pr` / `draft-pr`: `git fetch <remote> <base>`, then
    `<remote>/<base>`, the ref the close step rebases onto and the PR targets; the human's local
    `base` may be stale and is never read or touched. A failed fetch raises (the open parks
    `open_failed`); it never falls back to the local base."""
    mcfg = pcfg.get("merge") or {}
    policy = mcfg.get("policy", "merge")
    if policy == "merge":
        return base
    remote = mcfg.get("remote") or "origin"
    try:
        git.fetch(root, remote, base)
    except GitError as e:
        raise GitError(e.cmd, e.code, f"cannot fetch {remote}/{base} for a {policy} campaign "
                       f"(the branch must start from the remote, not the local {base}): "
                       f"{(e.tail or '').strip()}") from e
    return f"{remote}/{base}"


def _run_worktree_setup(pcfg: dict, worktree: Path) -> bool:
    """Run the project's `worktrees.setup` commands in `worktree`; True when any ran. A failure
    raises GitError whose tail leads with the command and exit code (SPEC-0258), so the park
    detail names the command even when it printed nothing."""
    cmds = (pcfg.get("worktrees") or {}).get("setup") or []
    for cmd in cmds:
        r = subprocess.run(cmd, shell=True, cwd=str(worktree), capture_output=True, text=True)
        if r.returncode != 0:
            out = "\n".join(((r.stdout or "") + (r.stderr or "")).splitlines()[-20:])
            raise GitError([cmd], r.returncode, f"{cmd} exited {r.returncode}:\n{out}")
    return bool(cmds)


def _do_open_merge(ctx: _Ctx, state: dict, res: StepResult) -> StepResult:
    """SPEC-0233: a merge request opens its worktree and nothing else. Both a created and an
    adopted worktree get the project's setup commands (SPEC-0258); an adopted one is otherwise
    the requester's and stays as it is."""
    repo = state["repo"]
    merge = state.get("merge") or {}
    try:
        pcfg = ctx.project(state)
        root, worktree, branch = Path(repo["root"]), Path(repo["worktree"]), repo["branch"]
        base = repo.get("base") or pcfg.get("base") or "main"
        base_sha = git.rev_parse(root, base)
        if merge.get("own_worktree"):
            elsewhere = git.worktree_for_branch(root, branch)
            if elsewhere is not None and elsewhere != worktree.resolve():
                if elsewhere == root.resolve():
                    raise ValueError(f"the main checkout is on {branch}; switch it to {base}")
                if not elsewhere.is_dir():      # fail before `merge`/`repo` change: stay retryable
                    raise ValueError(f"worktree {elsewhere} of {branch} no longer exists "
                                     "(git worktree prune, then resume)")
                # the branch got a worktree since the request: adopt it, never fight it
                merge["own_worktree"] = False
                repo["worktree"] = str(elsewhere)
                worktree = elsewhere
        if merge.get("own_worktree"):
            if git.worktree_for_branch(root, branch) is None:
                git.worktree_add_existing(root, worktree, branch)
            _run_worktree_setup(pcfg, worktree)
        else:
            if worktree.resolve() == root.resolve():
                raise ValueError(f"the main checkout is on {branch}; switch it to {base}")
            if not worktree.is_dir():
                raise ValueError(f"worktree {worktree} no longer exists "
                                 "(git worktree prune if it was removed)")
            if git.current_branch(worktree) != branch:
                raise ValueError(f"worktree {worktree} is not on branch {branch}")
            if not git.is_clean(worktree, untracked=True):
                raise ValueError(f"worktree {worktree} has uncommitted or untracked changes")
            # SPEC-0258: after the clean check (setup writes untracked `.venv`, caches), and
            # before `head` is read so a lockfile rewrite shows up in the close dirty check.
            if _run_worktree_setup(pcfg, worktree):
                res.events_before.append(("finding", {
                    "persona": "queue", "severity": "info", "gate": "setup",
                    "title": "worktree setup ran in adopted worktree",
                    "detail": "\n".join((pcfg.get("worktrees") or {}).get("setup") or []),
                    "files": []}))
        repo["base_sha_at_open"] = base_sha
        repo["head"] = git.rev_parse(worktree)
    except (GitError, ValueError, OSError) as e:
        res.error = getattr(e, "tail", None) or str(e)
        res.config_error = isinstance(e, ProjectConfigError)
    return res


class _RunStarted(OSError):
    """`_prepare_run` failed after `run.start` was written; carries what a matching
    `run.end` needs."""

    def __init__(self, cause, run_id, budget):
        super().__init__(str(cause))
        self.run_id, self.budget = run_id, budget


def _previous_runs(cfg, state: dict, persona: str) -> list:
    """The `run.end` events shown to `persona` as previous verdicts (tail 2). A reviewer never
    sees the runs of its own review pass-through (run number > `last_review.entry_run`)."""
    entry = (state.get("last_review") or {}).get("entry_run")
    if persona == "implementer" and CO.scoped_out(state):
        # SPEC-0555: the review reaches the implementer as the in-scope brief only; the review
        # runs' free-text summaries may describe out-of-scope work
        group = CO.review_group(state)
        ends = [e for e in store.read_ledger(cfg, state["id"], types=["run.end"])
                if e.get("persona") not in group]
        return ends[-2:]
    if persona not in PS.REVIEWER_PERSONAS or entry is None:
        return store.read_ledger(cfg, state["id"], types=["run.end"], tail=2)
    ends = store.read_ledger(cfg, state["id"], types=["run.end"])
    ends = [e for e in ends if int(str(e.get("run_id") or "0-").split("-")[0] or 0) <= entry]
    return ends[-2:]


def _triage_input(state: dict, profile: dict) -> str:
    """The `triage_input` prompt value of the review triager (SPEC-0206): the score, the
    reviewers' verdict and brief, and the waivable (surviving `minor`/`nit`) findings."""
    valid = _scoped_verdicts(state, profile)
    hw, at = human_overrides_of(state)
    tri = triage_reviews(valid, profile, human_waived=hw, attested=at)
    keys = ("id", "personas", "severity", "title", "detail", "files")
    return json.dumps({
        "score": tri["score"], "pass_score": review_params(profile)[0],
        "reviewer_verdicts": {p: {"verdict": v.get("verdict"), "fix_brief": v.get("fix_brief")}
                              for p, v in valid.items()},
        "waivable": [{k: f[k] for k in keys} for f in tri["blocking"] + tri["advisory"]
                     if f["severity"] in ("minor", "nit")],
    }, indent=2, sort_keys=True)


_KIND_OF_PERSONA = {"implementer": "implement", "gate_triager": "triage"}


def _ledger_run_max(cfg, cid: str) -> int:
    """SPEC-0263: the highest run number on the ledger's `run.start` rows (`"012-implementer"` is
    12). A `run_id` without a numeric prefix (adopted or legacy rows) is skipped."""
    top = 0
    for ev in store.read_ledger(cfg, cid, types=["run.start"]):
        head = str(ev.get("run_id") or "").split("-", 1)[0]
        if head.isdigit():
            top = max(top, int(head))
    return top


def _hold_local_backend_before_prepare(pgate, state: dict, persona: str, step: Step | None) -> bool:
    """SPEC-0486: when the backend check fails and no persona slot was reserved at admit, hold
    and skip `run.start` (the reservation is adopted by `dispatch` on the remote path)."""
    personas = (persona,)
    if pgate.backend_fault(state, personas, remote_ok=False, step=step) is None:
        return False
    key = (state["id"], persona)
    if key in pgate._slot_reservations:
        if step is None or step.kind not in ("write_spec", "resolve"):
            return False
        pgate.release_campaign_reservations(state["id"], (persona,))
    pgate._hold(state["id"], "backend_unavailable")
    return True


def _prepare_run(ctx: _Ctx, state: dict, persona: str, step: Step | None = None):
    """Main thread: mint the run id, build the request, append `run.start`, bump `runs` and
    write the state. Returns (run_id, request, budget). Everything that can raise happens
    before the `run.start` append; a failure after it raises `_RunStarted`."""
    cfg = ctx.cfg
    cid = state["id"]
    profile = ctx.profile(state)
    pcfg = ctx.project(state)
    counters = _counters(state)
    # the stored counter can lag the ledger (a failed state write after `run.start`): a run id
    # is never reused, or two runs would share a run directory and a TMPDIR (SPEC-0263)
    run_no = max(counters["runs"], _ledger_run_max(cfg, cid)) + 1
    run_id = f"{run_no:03d}-{persona}"
    run_dir = store.runs_dir(cfg, cid) / run_id
    invariant = None
    if persona == "review_triager":     # its input carries the verdicts already (SPEC-0206)
        previous, extra = [], {"triage_input": _triage_input(state, profile)}
    elif persona == CO.NAME:            # SPEC-0555: its input carries the verdicts
        previous, extra = [], CO.collator_values(cfg, state, _valid_verdicts(state, profile))
    elif persona == "spec_writer":
        previous, extra = [], _prepare_spec_write(ctx, state)
    elif persona == "spec_reviewer":    # SPEC-0214: the campaign's own invariant and context
        worktree = Path((state.get("repo") or {}).get("worktree") or ".")
        previous, extra = [], SR.review_context(
            worktree / (state.get("spec") or {}).get("rel_path", ""), state.get("spec_id"),
            idea_text=json.dumps(state.get("idea") or {}, indent=2, ensure_ascii=False),
            root=worktree)
        invariant = PS.SPEC_REVIEW_CAMPAIGN_INVARIANT
    else:
        previous, extra = _previous_runs(cfg, state, persona), None
    req = PS.build_request(cfg, profile, pcfg, state, persona, run_dir=run_dir, previous=previous,
                           extra=extra, invariant=invariant)
    budget = float(req.max_budget_usd)
    if persona == "conflict_resolver" and (state.get("closing") or {}).get("resolve") is not None:
        # SPEC-0303: remember whether only the close allowance let this run start (plan_step is
        # pure); the apply appends the finding. `cost.cap_usd` is never touched.
        state["closing"]["resolve"]["on_allowance"] = \
            _cap_blocks(state, profile, persona)[0] is not None
    with store.item_lock(cfg, cid):
        store.append_event(cfg, cid, "run.start", {
            "persona": persona, "run_id": run_id, "model": req.model, "effort": req.effort,
            "max_budget_usd": budget, "timeout_s": req.timeout_s,
            "permission_mode": req.permission_mode, "prompt_sha256": PS.prompt_sha256(req.prompt),
            # SPEC-0225: what adoption needs to rebuild the step and its snapshot
            "state": state.get("state"),
            "step": {"kind": step.kind if step else _KIND_OF_PERSONA.get(persona, "implement"),
                     "personas": list(step.personas) if step else []},
            "profile": req.profile,                                    # SPEC-0238
            **({"tier": req.tier} if req.tier else {}),                # SPEC-0553
            **({"effort_clamped_from": req.effort_clamped_from}        # SPEC-0557
               if req.effort_clamped_from else {}),
            **PK.run_marks(state, persona, step.kind if step else None),  # SPEC-0554
        })
        counters["runs"] = run_no
        try:
            store.write_state(cfg, cid, state)
        except (ValueError, OSError) as e:
            raise _RunStarted(e, run_id, budget) from e
    return run_id, req, budget


def _rereserve_spec_number(cfg, state: dict, number: int, specs_dir) -> dict:
    """SPEC-0569: move the campaign off `number`, which another worktree or ref took. `release`
    finds nothing when the reservation was already pruned (the number is committed on a branch):
    `allocate` then returns a fresh number on its own."""
    from .. import spec_numbers as SN
    try:
        SN.release(cfg, number, specs_dir=specs_dir)
    except ValueError:
        pass
    return SN.allocate(cfg, by="queue", campaign=state["id"], specs_dir=specs_dir)


def _collision_text(c: dict) -> str:
    return f"SPEC-{int(c['number']):04d} is already used by {c['file']} on {c['other']}"


def _spec_collision(cfg, worktree, sw: dict, state: dict) -> dict | None:
    """SPEC-0569: the filename collision of the number the writer just used, or None. A
    collision is its own result, never a text problem: the writer is not asked to fix it."""
    from .. import spec_numbers as SN
    for c in SN.collisions(cfg, specs_dir=Path(worktree) / "docs" / "specs", tree=worktree,
                           campaign=state["id"]):
        if c["kind"] == "filename" and c["number"] == sw["number"]:
            return {"number": c["number"], "file": ", ".join(c["other_files"]),
                    "other": ", ".join(c["other"])}
    return None


def _prepare_spec_write(ctx: _Ctx, state: dict) -> dict:
    """Main thread, before the writer's `run.start` (SPEC-0215): resolve the parent epic, fix the
    spec number and path, record `state.spec_write` and return the prompt values. Raises
    ValueError (the tick parks `spec_write_failed`) when nothing may be run."""
    worktree = Path((state.get("repo") or {}).get("worktree") or ".")
    idea = state.get("idea") or {}
    epic = (idea.get("epic") or "").strip() or None
    epic_text = SW.epic_context(worktree, epic) if epic else None
    spec = state.get("spec") or {}
    counters = _counters(state)
    branch = (state.get("repo") or {}).get("branch")
    try:
        # the writer runs with full permissions: never start one on another branch (its rollback
        # would refuse, and its commit would land there)
        if branch and git.current_branch(worktree) != branch:
            raise ValueError(f"HEAD is no longer on {branch}; check the worktree out on it, "
                             "then resume")
        if SW.tracked_dirty(worktree):
            raise ValueError("the worktree has uncommitted changes to tracked files; clean the "
                             "worktree, then resume")
        if state.get("kind") == "split":                        # SPEC-0290, before `amend`/`revise`
            mode, number, path, split_sw = _prepare_split_part(ctx, state, worktree)
        elif state.get("admission") == "revise_pending":        # SPEC-0279, before `revise`
            mode, number, path = "amend", int(str(spec["id"])[5:]), spec["rel_path"]
        elif spec.get("id"):
            mode, number, path = "revise", int(str(spec["id"])[5:]), spec["rel_path"]
        else:
            from .. import idea_link as IL
            if _is_request_only(state):                         # SPEC-0541: before allocating
                agreed, _gate = _request_source_for(ctx.cfg, state)
                idea = dict(state.get("idea") or {})
                idea["summary"] = agreed.summary
                idea["heading"] = agreed.title
                state["idea"] = idea
                task = None
            else:
                task = _idea_task(ctx.cfg, _idea_id_of(state))
            if task is not None:
                agreed = IL.load_agreed_request(ctx.cfg, task)
                if agreed:
                    if agreed.spec:
                        raise ValueError(f"{agreed.req_id} already cites {agreed.spec}")
                    idea = dict(state.get("idea") or {})
                    idea["source_request"] = agreed.req_id
                    idea["summary"] = agreed.summary
                    state["idea"] = idea
            from .. import spec_numbers as SN
            specs_dir = worktree / "docs" / "specs"
            rec = SN.allocate(ctx.cfg, by="queue", campaign=state["id"], specs_dir=specs_dir)
            while taken := SN.number_taken(ctx.cfg, rec["number"], specs_dir=specs_dir,
                                           exclude_campaign=state["id"]):     # SPEC-0569
                counters["spec_number_retries"] = counters.get("spec_number_retries", 0) + 1
                if counters["spec_number_retries"] > SPEC_NUMBER_RETRY_MAX:
                    raise ValueError(f"SPEC-{rec['number']:04d} is already used on "
                                     f"{', '.join(taken)}")
                rec = _rereserve_spec_number(ctx.cfg, state, rec["number"], specs_dir)
            mode, number = "write", rec["number"]
            path = f"docs/specs/{rec['number']:04d}-{SP.slugify(idea.get('heading') or '')}.md"
        sw = SW.snapshot(worktree, mode=mode, number=number, path=path)
        if mode == "write":                                     # SPEC-0423
            sw["must_depend_on"] = _must_depend_on(ctx.cfg, state)
        if mode == "split":
            sw.update(split_sw)
    except GitError as e:
        raise ValueError(e.tail or str(e)) from e
    prior = state.get("spec_write") or {}
    if counters.get("spec_write_retries", 0) and prior.get("untracked_before") is not None:
        sw["untracked_before"] = prior["untracked_before"]      # a failed attempt's leftovers
    if path in sw["untracked_before"]:      # the spec file is never setup output: a leftover of
        sw["untracked_before"].remove(path)                     # an earlier attempt is removed,
        try:                                                    # so it can neither hide the new
            (worktree / path).unlink()                          # write nor survive a rollback
        except OSError as e:
            raise ValueError(f"cannot remove the leftover {path}: {e}") from e
    state["spec_write"] = sw                                    # never join the baseline
    brief = state.get("pending_brief")
    spec_id = SW.spec_id_of(number)
    extra = {}
    if mode == "split":
        extra = {"spec_text": _read_text(worktree / state["split"]["original_rel"]),
                 "idea_text": f"(none: part {sw['split']['k']} of the split of "
                              f"{state['split']['original']})"}
    return {
        "spec_id": spec_id, "spec_rel_path": path,
        "idea_text": json.dumps(idea, indent=2, ensure_ascii=False),
        "template_text": _read_text(worktree / "docs" / "specs" / "TEMPLATE.md"),
        "schema_text": _read_text(worktree / "docs" / "specs" / "SCHEMA.md"),
        "write_problems": SW.write_problems_text(brief),
        "write_rules": SW.write_rules(mode, sw, idea, brief),
        "epic_text": epic_text,
        **extra,
    }


def _must_depend_on(cfg, state: dict) -> list[dict]:
    """SPEC-0423: the spec ids the new spec's `depends_on` must carry, one `{"spec_id", "ref"}`
    per `after` prerequisite: an idea ref -> its `:SPEC:` (else its MERGED campaign's spec id),
    a spec ref -> itself, a campaign ref -> its spec id. A ref with no spec id is skipped."""
    out = []
    for entry in after_entries(state):
        rid, kind, sid = entry["ref"], entry["kind"], None
        if kind == "spec":
            sid = rid
        elif kind == "campaign":
            try:
                c = store.read_state(cfg, rid)
                sid = c.get("spec_id") or (c.get("spec") or {}).get("id")
            except (OSError, ValueError, KeyError):
                sid = None
        else:
            task = _idea_task(cfg, rid)
            sid = ((task.properties.get("SPEC") or "").strip() if task else "") or None
            if not sid:
                for c in store.list_campaigns(cfg, include_terminal=True):
                    if c.get("kind") == "idea" and c.get("state") == "MERGED" and \
                            _canon_id(_idea_id_of(c) or "") == _canon_id(rid):
                        sid = (c.get("spec") or {}).get("id")
                        break
        if sid and all(o["spec_id"] != sid for o in out):
            out.append({"spec_id": str(sid), "ref": rid})
    return out


def _prepare_split_part(ctx: _Ctx, state: dict, worktree: Path) -> tuple:
    """SPEC-0290: the first part without a `spec_id`: allocate its number (one reservation per
    part, keyed `<campaign>#k`), fix its path and the front matter it must carry. Returns
    `(mode, number, path, extra)`; `extra` joins the `spec_write` snapshot."""
    from .. import spec_numbers as SN
    sp = state["split"]
    parts = sp["parts"]
    k = next((i for i, p in enumerate(parts) if not p.get("spec_id")), None)
    if k is None:
        raise ValueError("every part of the split is written")
    try:
        ofm, _ = split_frontmatter((worktree / sp["original_rel"]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"cannot read the original {sp['original_rel']}: {e}") from e
    ofm = ofm if isinstance(ofm, dict) else {}
    rec = SN.allocate(ctx.cfg, by="queue", campaign=f"{state['id']}#{k + 1}",
                      specs_dir=worktree / "docs" / "specs")
    number = rec["number"]
    path = f"docs/specs/{number:04d}-{SP.slugify(parts[k]['title'])}.md"
    if k == 0:
        deps = ofm.get("depends_on") or []
        deps = [str(deps)] if isinstance(deps, str) else [str(d) for d in deps]
    else:
        deps = [parts[k - 1]["spec_id"]]
    expected = {"id": SW.spec_id_of(number), "status": "accepted", "kind": "feature",
                "supersedes": [sp["original"]], "depends_on": deps}
    if ofm.get("parent"):
        expected["parent"] = str(ofm["parent"])
    return "split", number, path, {
        "expected_fm": expected,
        "split": {"k": k + 1, "n": len(parts), "original": sp["original"],
                  "parts": [{"title": p["title"], "scope": p["scope"]} for p in parts],
                  "source_idea": ofm.get("source_idea")}}


def _read_text(path) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def _do_write_spec(ctx: _Ctx, state: dict, step: Step, run_id: str, req,
                   budget: float) -> StepResult:
    """Worker thread (SPEC-0215): run the spec writer, validate what it left, regenerate the
    project's generated files, then commit — or roll the worktree back to where it was."""
    res = StepResult(state["id"], step, state["state"], run_id=run_id, budget=budget)
    worktree = (state.get("repo") or {}).get("worktree")
    sw = dict(state["spec_write"])
    res.write = {"problems": [], "blocked": None, "committed": False, "sha": None,
                 "title": None, "mode": sw["mode"], "rollback_failed": False,
                 "collision": None}
    w = res.write
    branch = (state.get("repo") or {}).get("branch")
    env_interrupt = False
    off_branch: list[str] = []

    def on_branch():
        """A reset must only ever hit the campaign branch: the writer runs with full permissions
        and may have switched or detached HEAD. Otherwise nothing is reset and a human fixes it."""
        if not branch:
            return
        try:
            head = git.current_branch(worktree)
            wrong = head != branch
        except (GitError, OSError) as e:    # an unreadable HEAD is a failed rollback too
            head, wrong = None, True
            why = getattr(e, "tail", None) or str(e)
        else:
            why = f"HEAD is on {head or 'a detached commit'}"
        if wrong:
            w["rollback_failed"] = True
            w["rollback_detail"] = (f"HEAD is no longer on {branch} ({why}); check the worktree "
                                    "out on it, clean it, then resume")
            off_branch.append(w["rollback_detail"])
            raise ValueError(w["rollback_detail"])

    def roll_back():
        on_branch()
        try:
            SW.rollback(worktree, sw)
        except GitError as e:
            w["rollback_failed"] = True
            w["rollback_detail"] = getattr(e, "tail", None) or str(e)
            raise

    try:
        started = time.time()
        res.run_result = rr = _gated_runner(ctx, req, state).run(req)
        if _spec_resume_failed(ctx, req, rr):                          # SPEC-0572
            roll_back()                 # what the failed resume left, before the fresh run
            res.run_id, req, rr = _spec_session_fallback(ctx, state, step, run_id, req, budget,
                                                         rr, started)
            res.run_result = rr
            started = time.time()
        fault = BP.env_fault(rr)
        if fault is not None:
            rr = replace(rr, interrupted=True,
                         cost_usd=0.0 if rr.cost_usd is None else rr.cost_usd)
            res.run_result = rr
            res.interrupted = True
            env_interrupt = True
            if getattr(ctx, "cfg", None) is not None:
                BP.note_fault(ctx.cfg, BP.fault_kind(fault), fault, where="run")
        elif rr.interrupted:
            res.interrupted = True
            if BP.env_fault_text(getattr(rr, "result_text", "") or ""):
                env_interrupt = True
        _note_rate_limit(ctx, state, rr, started, _run_persona(req, step))      # SPEC-0345
        if not res.interrupted:
            res.interrupted = ctx.stop.is_set()
        verdict = rr.verdict if isinstance(rr.verdict, dict) else {}
        if res.interrupted or rr.interrupted:
            roll_back()
        elif rr.verdict_ok and verdict.get("status") == "blocked":
            w["blocked"] = [str(b) for b in verdict.get("blockers") or []] \
                or [str(verdict.get("summary") or "no reason given")]
            roll_back()
        else:
            on_branch()
            problems = SW.verdict_problems(rr, sw)
            if not problems:
                problems = SW.validate_spec_change(worktree, sw, state.get("idea"))
            collision = (_spec_collision(ctx.cfg, worktree, sw, state)      # SPEC-0569
                         if sw["mode"] == "write" else None)
            generated = []
            if not problems and not collision:
                pcfg = ctx.project(state)
                regen = (pcfg.get("merge") or {}).get("regenerate") or []
                problems, generated = SW.regenerate(worktree, regen, sw)
            if collision:
                w["collision"] = collision          # not a text problem: kept out of `problems`
                roll_back()
            elif problems:
                w["problems"] = problems
                roll_back()
            else:
                fm, _ = split_frontmatter((Path(worktree) / sw["path"]).read_text(encoding="utf-8"))
                w["title"] = str(fm.get("title") or verdict.get("title") or "")
                msg = (f"nw({state['id']}): spec {sw['mode']} — "
                       f"{SW.spec_id_of(sw['number'])} {w['title']}")
                trailer = ((ctx.project(state).get("agents") or {}).get("commit_trailer")
                           or "").strip()
                if trailer:
                    msg += f"\n\n{trailer}"
                w["sha"] = SW.commit_staged(worktree, [sw["path"], *generated], msg)
                w["committed"] = True
                w["head"] = git.rev_parse(worktree)
    except Exception as e:                      # nothing may leave the worktree unrolled
        if not env_interrupt:
            res.error = getattr(e, "tail", None) or str(e)
            if not isinstance(e, (GitError, ValueError, OSError)):
                res.error = repr(e)
                res.crashed = repr(e)               # still a report.errors entry
        if not w["rollback_failed"] and not res.interrupted:
            try:
                roll_back()
            except (GitError, ValueError):
                pass                            # roll_back marked rollback_failed
        if w["rollback_failed"] and not env_interrupt:
            res.error = off_branch[0] if off_branch else \
                "rollback failed; clean the worktree, then resume"
    return res


def _spec_review_precheck(ctx: _Ctx, state: dict, step: Step) -> StepResult | None:
    """Main thread, before the reviewer's `run.start` (SPEC-0214): an already committed accept
    (an earlier accept failed after its commit) needs no review, and tracked changes in the
    worktree mean no review may start. Returns the result to apply, or None to run the review."""
    res = StepResult(state["id"], step, state["state"])
    repo, spec = state.get("repo") or {}, state.get("spec") or {}
    worktree = repo.get("worktree")
    try:
        if not git.is_clean(worktree, untracked=False):
            res.spec_review = {"dirty": True}
            return res
        # campaign history, not the spec's status, says whether a review phase may still accept:
        # a plain spec campaign, or one that already has `spec_accepted`, was accepted before
        # this phase and its close-guard baseline must not be reset (whatever the status now)
        current_id = str(spec.get("id") or state.get("spec_id") or "")
        if state.get("kind") != "idea" or (
                current_id and any(e.get("spec_id") == current_id
                                   for e in store.read_ledger(ctx.cfg, state["id"],
                                                              types=["spec_accepted"]))):
            res.spec_review = {"already_accepted": True}
            return res
        head = git.git(worktree, "show", f"HEAD:{spec.get('rel_path')}", check=False)
        if head.returncode == 0:
            fm, _ = split_frontmatter(head.stdout)
            if str((fm or {}).get("status") or "") == "accepted":
                # an accept that committed and then failed (a review of this campaign, no
                # `spec_accepted` yet) is finished here
                resumable = _last_review_event(ctx.cfg, state) is not None
                res.spec_review = {"accepted": True} if resumable else {"already_accepted": True}
                return res
    except (GitError, OSError) as e:
        res.error = getattr(e, "tail", None) or str(e)
        return res
    prior = _prior_revision(ctx.cfg, state)
    if prior is not None:               # SPEC-0230 step 0': one review per campaign
        changed = _spec_sha(Path(worktree or ".") / spec.get("rel_path", "")) != prior["spec_sha256"]
        res.spec_review = {"after_revision" if changed else "unchanged": prior}
        return res
    last = _last_review_event(ctx.cfg, state)
    if last is not None and last.get("verdict") == "approve" \
            and _spec_sha(Path(worktree or ".") / spec.get("rel_path", "")) == last["spec_sha256"]:
        res.spec_review = {"approved": last}    # an accept failed: resume it, no second review
        return res
    return None                         # no review yet, or a human edited the approved spec


def _last_review_event(cfg, state: dict) -> dict | None:
    """This campaign's last `spec_review` ledger event, or None."""
    evs = [e for e in store.read_ledger(cfg, state["id"], types=["spec_review"])
           if e.get("spec_sha256")]
    return evs[-1] if evs else None


def _prior_revision(cfg, state: dict) -> dict | None:
    """SPEC-0230: this campaign's last `spec_review` ledger event when its verdict was `revise`
    or `split` (the one review already ran), else None."""
    last = _last_review_event(cfg, state)
    return last if last and last.get("verdict") in ("revise", "split") else None


def _revision_pending_brief(cfg, state: dict, prior: dict) -> dict:
    """SPEC-0230 accept step 5: the implementer's brief from the stored revise / split review."""
    review = SR.load_review(cfg, (state.get("spec") or {}).get("id"), prior["spec_sha256"])
    return SR.revision_brief(review or {"spec_sha256": prior["spec_sha256"], "findings": []})


def _do_spec_review(ctx: _Ctx, state: dict, step: Step, run_id: str, req,
                    budget: float) -> StepResult:
    """Worker thread (SPEC-0214): run the read-only spec reviewer. If it moved HEAD or changed
    the spec or any tracked file, the worktree is reset to where the run started (the
    precheck guaranteed a clean start; untracked files are left alone)."""
    res = StepResult(state["id"], step, state["state"], run_id=run_id, budget=budget)
    repo = state.get("repo") or {}
    worktree, branch = repo.get("worktree"), repo.get("branch")
    spec_file = Path(worktree or ".") / (state.get("spec") or {}).get("rel_path", "")
    res.spec_review = sr = {"sha": _spec_sha(spec_file), "modified": None}
    env_interrupt = False
    try:
        head = git.rev_parse(worktree)
        try:
            local = _preflight_hold(ctx, state, req, _local_runner(ctx, state, req))
            started = time.time()
            res.run_result = local.run(req)
            if _spec_resume_failed(ctx, req, res.run_result) \
                    and git.rev_parse(worktree) == head and _spec_sha(spec_file) == sr["sha"] \
                    and git.is_clean(worktree, untracked=False):                # SPEC-0572
                res.run_id, req, res.run_result = _spec_session_fallback(
                    ctx, state, step, run_id, req, budget, res.run_result, started)
        except Exception as e:                  # the reset below must still run
            res.error = getattr(e, "tail", None) or str(e)
            if not isinstance(e, (GitError, ValueError, OSError)):
                res.error = repr(e)
        else:
            fault = BP.env_fault(res.run_result)
            if fault is not None:
                rr = res.run_result
                res.run_result = replace(
                    rr, interrupted=True,
                    cost_usd=0.0 if rr.cost_usd is None else rr.cost_usd)
                res.interrupted = True
                env_interrupt = True
                if getattr(ctx, "cfg", None) is not None:
                    BP.note_fault(ctx.cfg, BP.fault_kind(fault), fault, where="run")
            elif res.run_result is not None and res.run_result.interrupted:
                res.interrupted = True
                if BP.env_fault_text(getattr(res.run_result, "result_text", "") or ""):
                    env_interrupt = True
        if not res.interrupted:
            res.interrupted = ctx.stop.is_set()
        moved = False
        try:
            moved = git.rev_parse(worktree) != head
        except GitError:
            if not env_interrupt:
                raise
            moved = True
        dirty = moved or _spec_sha(spec_file) != sr["sha"] \
                or not git.is_clean(worktree, untracked=False)
        if dirty:
            sr["saved"] = _save_worktree_changes(worktree, state["id"], run_id, head)
            if branch and git.current_branch(worktree) != branch:
                git.git(worktree, "checkout", "-f", branch)
            git.git(worktree, "reset", "--hard", head)
            sr["modified"] = "reviewer modified the worktree"
    except (GitError, ValueError, OSError) as e:
        if not env_interrupt:
            res.error = getattr(e, "tail", None) or str(e)
    return res


def _spec_resume_failed(ctx: _Ctx, req, rr) -> bool:
    """SPEC-0572: a resumed spec writer / reviewer run that failed as a resume (as
    `_do_persona` decides it for the code personas, SPEC-0556)."""
    return (getattr(ctx, "cfg", None) is not None and SS.resume_failed(req, rr)
            and BP.env_fault(rr) is None and not PG.is_usage_exhausted(rr)
            and not ctx.stop.is_set())


def _spec_session_fallback(ctx: _Ctx, state: dict, step: Step, run_id: str, req, budget: float,
                           failed, started: float):
    """SPEC-0572: the failed resume is recorded and the step runs once more, in the same step,
    as today's fresh run (`_rerun_locally(fallback=True)`: a `session_fallback` event, a new run
    id). The fallback is never a resume, so it cannot loop. Returns (run_id, req, result)."""
    _note_rate_limit(ctx, state, failed, started, _run_persona(req, step))
    return _rerun_locally(ctx, state, step, run_id, req, budget, failed, None, fallback=True)


def _save_worktree_changes(worktree, cid: str, run_id: str, head: str) -> list[str]:
    """Before `_do_spec_review` resets the worktree: keep what the reset would drop under
    `refs/nw/spec-review-saved/<campaign>/<run>/…` (refs are shared by every worktree of the
    repo and a run id repeats across campaigns), because the change may be a human's, made while the
    review ran. `head` holds a HEAD that moved, `wip` the tracked uncommitted changes (a
    `git stash create` commit; the shared stash stack is untouched). Returns the refs made."""
    saved = []
    try:
        moved = git.rev_parse(worktree)
        wip = git.git(worktree, "stash", "create", check=False).stdout.strip()
        for name, sha in (("head", moved if moved != head else ""), ("wip", wip)):
            if sha:
                ref = f"refs/nw/spec-review-saved/{cid}/{run_id}/{name}"
                git.git(worktree, "update-ref", ref, sha)
                saved.append(ref)
    except (GitError, ValueError, OSError):
        pass                                    # the reset is still the only way to a clean retry
    return saved


def _do_finalize_split(ctx: _Ctx, state: dict) -> StepResult:
    """Worker thread (SPEC-0290): once every part is written, mark the original `superseded` by
    part 1, regenerate the project's generated files and commit. No persona runs. Idempotent: a
    second run changes nothing and commits nothing. A failure leaves the worktree as it was."""
    res = StepResult(state["id"], Step("finalize_split"), state["state"])
    sp = state["split"]
    repo = state.get("repo") or {}
    worktree, branch = repo.get("worktree"), repo.get("branch")
    res.write = {"sha": None, "head": None}
    try:
        if branch and git.current_branch(worktree) != branch:
            raise ValueError(f"HEAD is not on {branch}; check the worktree out on it, then resume")
        if SW.tracked_dirty(worktree):
            raise ValueError("the worktree has uncommitted changes to tracked files; clean the "
                             "worktree, then resume")
        base = {"head_before": git.rev_parse(worktree), "untracked_before": SW.untracked(worktree),
                "path": sp["original_rel"]}
        try:
            SW.supersede(worktree, sp["original_rel"], sp["parts"][0]["spec_id"])
            regen = (ctx.project(state).get("merge") or {}).get("regenerate") or []
            problems, generated = SW.regenerate(worktree, regen, base)
            if problems:
                raise ValueError("; ".join(problems))
            ids = ", ".join(p["spec_id"] for p in sp["parts"])
            msg = f"nw({state['id']}): split {sp['original']} into {ids}"
            trailer = ((ctx.project(state).get("agents") or {}).get("commit_trailer") or "").strip()
            if trailer:
                msg += f"\n\n{trailer}"
            res.write["sha"] = SW.commit_staged(worktree, [sp["original_rel"], *generated], msg)
        except (GitError, ValueError, OSError):
            SW.rollback(worktree, base)
            raise
        res.write["head"] = git.rev_parse(worktree)
    except (GitError, ValueError, OSError) as e:
        res.error = getattr(e, "tail", None) or str(e)
    return res


def _do_persona(ctx: _Ctx, state: dict, step: Step, run_id: str, req, budget: float,
                hooks: RunHooks | None = None, adopted: dict | None = None,
                slot_hold=None) -> StepResult:
    """Worker thread: run the persona; for the implementer, commit afterwards (the triager and
    the reviewer are read-only and commit nothing). With `hooks` (SPEC-0225) the run may be
    detached by the pool: then nothing happens here and the result says so. `adopted` marks a
    run found open by a new runner (`exited_before_start`: its process ended before this
    runner's lease started, so a commit made but never recorded is recorded now)."""
    res = StepResult(state["id"], step, state["state"], run_id=run_id, budget=budget)
    held_slots: dict = {}       # the probe slot fd handed in: `dispatch` consumes it, else closed below
    if slot_hold is not None:
        held_slots[(state["id"], step.persona or req.persona)] = slot_hold
    try:
        # SPEC-0344: a persona-capable worker may take the run (or holds an adopted one).
        # A tick context resolves the persona backend per campaign (SPEC-0436). A caller
        # that only carries `.runner` (the pre-0436 test doubles) is used as-is.
        cfg = getattr(ctx, "cfg", None)
        local = _local_runner(ctx, state, req)
        if adopted is None:                       # SPEC-0484: never in place of a live run
            local = _preflight_hold(ctx, state, req, local)
        reservations = held_slots if slot_hold is not None else getattr(
            ctx, "_slot_reservations", None)
        remote = RPS.dispatch(cfg, state, step.kind, req, adopted=adopted is not None,
                              local=local, reservations=reservations) if cfg is not None else None
        runner = remote or local
        if adopted is None and SS.on(state):      # SPEC-0556: a session lives where it ran
            req = SS.place(req, (getattr(remote, "worker", None) or {}).get("name"),
                           getattr(req, "session_backend", None))
        detachable = bool(getattr(runner, "detachable", False))
        started = time.time() if adopted is None else None     # SPEC-0345: an adopted run's is unknown
        outcome = runner.run(req, hooks=hooks) if (hooks and detachable) else runner.run(req)
        if outcome is DETACHED:
            res.detached = True
            return res
        # an adopted job re-runs from the full request its record keeps, never from the
        # stripped adoption rebuild; without one the failed run is recorded as it is (a
        # transient `run_failed`) and the queue prepares the step again
        rerun_req = req if adopted is None else getattr(remote, "request", None)
        if getattr(outcome, "writeback_failed", False) and rerun_req is not None:
            # SPEC-0345: the remote run's own result counts (a 429 there starts the backoff,
            # which then holds the re-run); the re-run's start is its own
            _note_rate_limit(ctx, state, outcome, started, _run_persona(req, step))
            started = time.time()
            run_id, req, outcome = _rerun_locally(ctx, state, step, run_id, rerun_req, budget,
                                                  outcome, hooks)
            res.run_id = run_id
            detachable = bool(getattr(_local_runner(ctx, state, req), "detachable", False))
            if outcome is DETACHED:
                res.detached = True
                return res
            # a re-run the backoff held never ran: interrupted, whatever the local runner
            detachable = detachable or bool(outcome.interrupted)
        if SS.resume_failed(req, outcome) and BP.env_fault(outcome) is None \
                and not PG.is_usage_exhausted(outcome) and not ctx.stop.is_set():
            if adopted is None:
                # SPEC-0556: once more in the same step, in a fresh session from the packet and
                # the findings history; the fallback run is never resumed, so this cannot loop
                _note_rate_limit(ctx, state, outcome, started, _run_persona(req, step))
                started = time.time()
                run_id, req, outcome = _rerun_locally(ctx, state, step, run_id, req, budget,
                                                      outcome, hooks, fallback=True)
                res.run_id = run_id
                detachable = bool(getattr(_local_runner(ctx, state, req), "detachable", False))
                if outcome is DETACHED:
                    res.detached = True
                    return res
                detachable = detachable or bool(outcome.interrupted)
            else:
                # an adopted resume keeps only a stripped request: no retry is spent, the entry
                # is dropped when it is recorded and the step is prepared again, fresh
                outcome = replace(outcome, interrupted=True)
                detachable = True
        _note_rate_limit(ctx, state, outcome, started, _run_persona(req, step))     # SPEC-0345
        fault = BP.env_fault(outcome)
        if fault is not None:
            # SPEC-0484: a child that could not start for a missing credential / SDK is an
            # environment fault, not a verdict: interrupted (no retry spent), uncharged
            outcome = replace(outcome, interrupted=True,
                              cost_usd=0.0 if outcome.cost_usd is None else outcome.cost_usd)
            detachable = True
            if cfg is not None:
                BP.note_fault(cfg, BP.fault_kind(fault), fault, where="run")
        res.run_result = outcome
        res.interrupted = bool(outcome.interrupted) if detachable else ctx.stop.is_set()
        # an adopted run a human moved past (parked, abandoned) is recorded, never committed
        if step.kind == "implement" and not res.interrupted and not state.get("_superseded"):
            pcfg = ctx.project(state)
            summary = ""
            if isinstance(res.run_result.verdict, dict):
                summary = str(res.run_result.verdict.get("summary") or "").strip().splitlines()
                summary = summary[0] if summary else ""
            msg = f"nw({state['id']}): implementer run {run_id[:3]}"
            if summary:
                msg += f" — {summary[:72]}"
            trailer = ((pcfg.get("agents") or {}).get("commit_trailer") or "").strip()
            if trailer:
                msg += f"\n\n{trailer}"
            res.commit_message = msg
            worktree = (state.get("repo") or {}).get("worktree")
            if adopted:     # a killed runner may have left index.lock; a fresh run never does
                res.lock_cleanup = git.clear_stale_index_lock(worktree)
            res.commit_sha = git.commit_all(worktree, msg)
            if res.commit_sha is None and adopted and adopted.get("exited_before_start"):
                head = git.rev_parse(worktree)
                if head != (state.get("repo") or {}).get("head"):
                    res.commit_sha = head
    except (GitError, ValueError, OSError) as e:
        res.error = getattr(e, "tail", None) or str(e)
    finally:
        RPS.release_reservations(held_slots)      # a hold `dispatch` never reached
        if hooks:
            hooks.done()
    return res


def _local_runner(ctx, state, req):
    """The backend for this request. A model set's runner wins; a test double that
    only accepts the campaign state is left alone."""
    resolve = getattr(ctx, "runner_for", None)
    if not callable(resolve):
        return ctx.runner
    kind = getattr(req, "runner", None)
    if not kind:
        return resolve(state)
    try:
        return resolve(state, kind=kind)
    except TypeError:
        return resolve(state)


def _gated_runner(ctx: _Ctx, req, state=None):
    """SPEC-0345: the runner of a gated `write_spec` / `resolve` run. The tick's gate read the
    backoff before the run was admitted; one begun since (another run's 429) holds it at its
    start too: a `HeldRunner`, so it ends interrupted (no attempt charged) and the next tick's
    gate holds the step. `_do_persona` gets the same check in `remote_personas.dispatch`."""
    cfg = getattr(ctx, "cfg", None)
    if cfg is not None and getattr(req, "persona", None) not in PG.UNGATED_PERSONAS:
        try:
            if PG.usage_until(cfg) is not None or PG.backoff_until(cfg) is not None:
                return RPS.HeldRunner()
        except (OSError, ValueError):
            pass
    return _preflight_hold(ctx, state, req, _local_runner(ctx, state, req))


def _preflight_hold(ctx, state, req, runner):
    """SPEC-0484/0486: `runner`, or a `PreflightHeldRunner` when it is a fresh local Cursor or
    Pi run and that backend fails its preflight (one escalation is filed). A run that already
    has `proc.json` or `launch.json` is left alone (adoption attaches)."""
    from .runner import CursorRunner, PiRunner, PreflightHeldRunner
    rd = getattr(req, "run_dir", None)
    if rd is not None and ((Path(rd) / "proc.json").exists() or (Path(rd) / "launch.json").exists()):
        return runner
    cfg = getattr(ctx, "cfg", None)
    if isinstance(runner, CursorRunner):
        error = BP.check("cursor")
        if error is None:
            return runner
        if cfg is not None:
            BP.note_fault(cfg, "cursor", error, where="spawn")
        return PreflightHeldRunner("cursor", error)
    if isinstance(runner, PiRunner):
        prov = [req.pi_provider] if getattr(req, "pi_provider", None) else []
        error = BP.check("pi", providers=prov)
        if error is None:
            return runner
        if cfg is not None:
            BP.note_fault(cfg, "pi", error, where="spawn")
        return PreflightHeldRunner("pi", error)
    return runner


def _run_persona(req, step: Step) -> str | None:
    """The persona of a run: its request's, else the step's (an adopted or fresh-flight run may
    carry no request)."""
    return getattr(req, "persona", None) or step.persona


def _note_rate_limit(ctx: _Ctx, state: dict, outcome, started, persona=None) -> None:
    """SPEC-0345: fold a finished campaign persona run into the account's rate-limit backoff.
    An ungated persona's run (spec-review) is outside the backoff: it neither starts nor
    resets it. Never fails the step."""
    cfg = getattr(ctx, "cfg", None)
    if cfg is None or outcome is None or outcome is DETACHED or persona in PG.UNGATED_PERSONAS:
        return
    try:
        if PG.is_usage_exhausted(outcome):
            PG.note_usage(cfg)
        PG.note_run(cfg, outcome, started, campaign=state.get("id"))
    except (OSError, ValueError) as e:
        journal(cfg, "rate_limit_note_failed", campaign=state.get("id"),
                error=f"{type(e).__name__}: {e}")


def _rerun_locally(ctx: _Ctx, state: dict, step: Step, run_id: str, req, budget: float, failed,
                   hooks: RunHooks | None = None, *, fallback: bool = False):
    """SPEC-0344: a remote run whose write-back failed. Its `run.end` (`writeback_failed`,
    `result_tail: "write-back: <reason>"`) and cost go on the ledger now, the worker joins the
    step's `remote_excluded`, and the step runs once more, in-process on the local runner,
    under a new run id with its own `run.start` (no autopark). A detachable local runner gets
    the task's `hooks` back in `spawning` for the new run dir, so a stop or restart detaches
    the re-run like any local run (the result is then `DETACHED`; the next runner adopts the
    new run). While the account's rate-limit backoff runs (SPEC-0345) the re-run is not
    started: it ends interrupted and the tick's dispatch gate holds the step. Returns
    (run_id, req, result).

    SPEC-0556 `fallback`: the failed run is a resume that failed. The re-run is the fresh-session
    request (`sessions.as_fresh`), a `session_fallback` event names both runs, the run's
    `run.start` carries `session_fallback: true`, and no worker is excluded."""
    cfg, cid = ctx.cfg, state["id"]
    persona = req.persona
    with store.item_lock(cfg, cid):
        for etype, payload in _record_run(state, persona, run_id, failed, budget):
            store.append_event(cfg, cid, etype, payload)
        start = next((e for e in reversed(store.read_ledger(cfg, cid, types=["run.start"]))
                      if e.get("run_id") == run_id), {})
        current = store.read_state(cfg, cid)
        counters = _counters(state)
        run_no = max(counters["runs"], _counters(current)["runs"], _ledger_run_max(cfg, cid)) + 1
        new_id = f"{run_no:03d}-{persona}"
        env = getattr(req, "env", None)
        tmp_env = None
        if isinstance(env, dict) and "TMPDIR" in env:
            # SPEC-0253: the failed run's `run.end` just removed its private TMPDIR; the re-run
            # gets its own, removed in turn by its own `run.end`. Created before the re-run's
            # `run.start`, so a failure here (a symlinked tmp dir, mkdir) leaves no open run:
            # the step fails with the failed run already closed.
            tmp_env = G.tmp_env(store.child_tmp(cfg, cid, f"run-{new_id}", create=True))
        excluded = list(state.get("remote_excluded") or [])
        if failed.worker and failed.worker not in excluded and not fallback:
            excluded.append(failed.worker)
        payload = {k: v for k, v in start.items()
                   if k not in ("id", "seq", "ts", "type", "campaign", "run_id")}
        payload.update(persona=persona, run_id=new_id, retry_of=run_id,
                       remote_excluded=excluded)
        if fallback:                                                    # SPEC-0556
            fresh = SS.as_fresh(req)
            payload.update(session_fallback=True, prompt_sha256=PS.prompt_sha256(fresh.prompt))
            store.append_event(cfg, cid, "session_fallback", {
                "persona": persona, "run_id": run_id, "retry_run_id": new_id,
                "session_id": req.resume_session, "worker": failed.worker,
                "error": (failed.result_text or "")[-300:]})
        store.append_event(cfg, cid, "run.start", payload)
        counters["runs"] = run_no
        state["remote_excluded"] = excluded
        current.setdefault("counters", {})["runs"] = run_no
        current["remote_excluded"] = excluded
        # the failed run's charge is on the ledger now, so it goes to state.json now too: a
        # re-run that detaches is never applied. `_apply` later copies `state["cost"]` over, so
        # a re-run that completes adds only its own charge.
        current["cost"] = copy.deepcopy(state["cost"])
        store.write_state(cfg, cid, current)
    new_req = replace(SS.as_fresh(req) if fallback else req,
                      run_dir=store.runs_dir(cfg, cid) / new_id)
    if tmp_env is not None:
        new_req = replace(new_req, env={**env, **tmp_env})
    if PG.backoff_until(cfg) is not None:
        from .runner import _save_inputs, _synthetic, mark_held
        _save_inputs(new_req)
        mark_held(new_req)
        what = "session fallback" if fallback else "write-back re-run"
        return new_id, new_req, _synthetic(new_req, f"the {what} was not started "
                                                    "during the rate-limit backoff", cost_usd=0.0)
    local = _preflight_hold(ctx, state, new_req, _local_runner(ctx, state, new_req))
    if hooks is not None and getattr(local, "detachable", False):
        if not hooks.restart(new_req.run_dir):
            return new_id, new_req, DETACHED
        return new_id, new_req, local.run(new_req, hooks=hooks)
    return new_id, new_req, local.run(new_req)


def _commit_resolver_changes(ctx: _Ctx, state: dict, worktree, branch, run_id: str,
                             verdict: dict) -> str | None:
    """SPEC-0252: after a `resolved` verdict commit the tracked and staged changes the resolver
    left in the tree, as the implementer's are. Never over an unattached HEAD, a running
    rebase, unmerged paths or other sequencer state: `git add -u` would hide those from the
    verification. Returns the new sha, None when nothing was committed."""
    if git.head_ref(worktree) != f"refs/heads/{branch}" or git.rebase_in_progress(worktree) \
            or git.git(worktree, "ls-files", "-u").stdout.strip() \
            or git.sequencer_in_progress(worktree):
        return None
    lines = str(verdict.get("summary") or "").strip().splitlines()
    msg = f"nw({state['id']}): conflict resolver run {run_id[:3]}"
    if lines:
        msg += f" — {lines[0][:72]}"
    trailer = ((ctx.project(state).get("agents") or {}).get("commit_trailer") or "").strip()
    if trailer:
        msg += f"\n\n{trailer}"
    return git.commit_tracked(worktree, msg)


def _close_regates(state: dict, pcfg: dict) -> bool:
    """SPEC-0259: whether the close will run the gates on the rebased tree. A `--docs-only` merge
    request (SPEC-0234) returns its `PreparedClose` before `G.run_all`, so a resolver's dropped-
    commit declaration is not honored there; a future gate-skipping close path is excluded here.
    SPEC-0263: nor is it honored when no gate is required — `all()` over nothing passes."""
    if CL._is_merge_request(state) and (state.get("merge") or {}).get("docs_only"):
        return False
    return any(g.get("required", True) for g in P.gates_for(pcfg, "close"))    # SPEC-0328


def _declared_drops(state: dict, verdict: dict, pcfg: dict) -> list:
    if not _close_regates(state, pcfg):
        return []
    return [(str(d["sha"]), str(d.get("reason") or "")) for d in verdict.get("dropped_commits") or []
            if isinstance(d, dict) and d.get("sha")]


def _do_resolve(ctx: _Ctx, state: dict, step: Step, run_id: str, req, budget: float) -> StepResult:
    """Worker thread (SPEC-0221): restore the campaign branch to the recorded `pre_head`, run
    the resolver persona, then verify its work with runner-side git reads (the verdict is not
    trusted). Anything not verified, interrupted or failed is restored to `pre_head`, so a
    discarded attempt leaves the branch as it was and writes nothing outside the worktree."""
    res = StepResult(state["id"], step, state["state"], run_id=run_id, budget=budget)
    repo = state.get("repo") or {}
    worktree, branch = repo.get("worktree"), repo.get("branch")
    rs = (state.get("closing") or {}).get("resolve") or {}
    pre_head = rs.get("pre_head")
    env_interrupt = False
    try:
        git.restore_branch(worktree, branch, pre_head)
        crash = None
        try:
            started = time.time()
            res.run_result = rr = _gated_runner(ctx, req, state).run(req)
            fault = BP.env_fault(rr)
            if fault is not None:
                rr = replace(rr, interrupted=True,
                             cost_usd=0.0 if rr.cost_usd is None else rr.cost_usd)
                res.run_result = rr
                res.interrupted = True
                env_interrupt = True
                if getattr(ctx, "cfg", None) is not None:
                    BP.note_fault(ctx.cfg, BP.fault_kind(fault), fault, where="run")
            elif rr.interrupted:
                res.interrupted = True
                if BP.env_fault_text(getattr(rr, "result_text", "") or ""):
                    env_interrupt = True
            _note_rate_limit(ctx, state, rr, started, _run_persona(req, step))  # SPEC-0345
        except Exception as e:          # a crashing resolver is a failed attempt, not a stuck close
            crash = f"the run raised: {e}"
            res.run_result = rr = _synthetic_failure(crash)
        if not res.interrupted:
            res.interrupted = ctx.stop.is_set()
        verdict = rr.verdict if isinstance(rr.verdict, dict) else {}
        pcfg = ctx.project(state)
        failed = check = commit_sha = None
        if res.interrupted or rr.interrupted:
            failed = "interrupted"
        elif not rr.verdict_ok or rr.is_error or rr.exit_code != 0:
            failed = crash or "the run failed or returned no valid verdict"
        elif verdict.get("status") != "resolved":
            failed = f"the resolver reported {verdict.get('status')}"
        else:
            check = None
            try:
                commit_sha = _commit_resolver_changes(ctx, state, worktree, branch, run_id, verdict)
            except GitError:
                pass                    # e.g. a hook rejected it: check 6 fails the dirty tree
            try:
                check = git.verify_resolution(worktree, branch, rs["target_sha"], rs["fork"],
                                              pre_head, _declared_drops(state, verdict, pcfg))
                failed = check.failed
            except (GitError, KeyError) as e:
                failed = f"verification error: {getattr(e, 'tail', None) or e}"
        res.resolution = {"verified": failed is None, "failed": failed, "head": None,
                          "extra": check.extra if check else [],
                          "absorbed": check.absorbed if check else [],
                          "superseded": check.superseded if check else [], "commit": commit_sha}
        if failed is None:
            git.clean_untracked(worktree)   # residue must not reach the close-time snapshot
            res.resolution["superseded_files"] = git.commit_files(
                worktree, [c["sha"] for c in check.superseded])
            res.resolution["head"] = git.rev_parse(worktree)
        else:
            git.restore_branch(worktree, branch, pre_head)
    except (GitError, ValueError, OSError) as e:
        if not env_interrupt:
            res.error = getattr(e, "tail", None) or str(e)
            res.resolution = None
        if pre_head and worktree and branch:    # a verified result that errored after is undone
            try:
                git.restore_branch(worktree, branch, pre_head)
            except (GitError, ValueError, OSError):
                pass                            # the park detail already names the first error
    return res


def _flaky_events(cfg, state: dict, payload: dict, previous, ok: bool, *, retried: bool) -> list:
    """SPEC-0293: record the flaky ids of a gate run in the cross-campaign record and return the
    ledger events for it. `rerun` = the ids SPEC-0289's targeted rerun passed; `triage_retry` =
    the ids that failed in `previous` when a triager `retry_gate` (`retried`) turned the gates
    green. Best-effort: a failed write is a `note`, never a different transition."""
    sources = {"rerun": [i for r in payload.get("results") or [] for i in r.get("flaky") or []]}
    if ok and retried and previous and not previous.get("ok"):
        sources["triage_retry"] = [i for r in previous.get("results") or [] if not r.get("ok")
                                   for i in (r.get("summary") or {}).get("failing") or []]
    events = []
    for source, ids in sources.items():
        ids = list(dict.fromkeys(ids))
        if not ids:
            continue
        try:
            FL.record(cfg, state.get("project") or "", ids, campaign=state["id"], source=source)
        except (OSError, ValueError, TypeError, AttributeError) as e:
            events.append(("note", {"text": f"could not record flaky tests: {e}",
                                    "author": "queue"}))
        else:
            events.append(("flaky_recorded", {"ids": ids, "source": source}))
    return events


def _do_gate(ctx: _Ctx, state: dict) -> StepResult:
    res = StepResult(state["id"], Step("gate"), "GATING")
    try:
        pcfg = ctx.project(state)
        worktree = (state.get("repo") or {}).get("worktree")
        if worktree:                        # SPEC-0316: the branch may raise a gate's timeout_s
            pcfg, raises = P.campaign_gate_config(ctx.cfg, pcfg, worktree)
            if raises:
                res.events_before.append(("finding", P.gate_limits_finding(raises)))
        # SPEC-0341: a live remote job record (a restart, a detach) is adopted under its own
        # seq; `next_gate_seq` would see the files its applied frames wrote and skip past it.
        # A job submitted for another HEAD or tree is cancelled instead (its results would be
        # for code no longer in the worktree), and the gate runs under a fresh seq.
        live = RJ.live_gate(ctx.cfg, state["id"])
        while live is not None:
            if _gate_committed(state, live):
                # a crash after the transition, before `mark_done`: the job's `gate` event
                # is applied already, so the record is settled, never replayed
                RJ.mark_done(ctx.cfg, live["job_id"])
            else:
                ok = _adoptable(live, worktree, ctx.stop)
                if ok is None:                  # git failed: leave the job for the next tick
                    res.deferred = f"remote job {live['job_id']}: worktree unreadable"
                    return res
                if ok:
                    break
                from . import remote_gates as RG
                for job_id in RG.cancel_campaign(ctx.cfg, state["id"], live["job_id"]):
                    res.events_before.append(("note", {
                        "text": f"cancelled remote job {job_id}: submitted for another commit",
                        "author": "queue"}))
            live = RJ.live_gate(ctx.cfg, state["id"])
        # SPEC-0342: no live record, but the newest one is `lost` (a crash after the give-up,
        # before the next attempt's record): continue that chain on its seq, for the same tree
        lost = None if live is not None else RJ.newest_gate(ctx.cfg, state["id"])
        if lost is not None and (lost.get("state") != "lost" or _gate_committed(state, lost)):
            lost = None
        if lost is not None:
            ok = _adoptable(lost, worktree, ctx.stop)
            if ok is None:
                res.deferred = f"remote job {lost['job_id']}: worktree unreadable"
                return res
            lost = lost if ok else None
        seq = int(live["seq_or_run"]) if live else int(lost["seq_or_run"]) if lost \
            else G.next_gate_seq(ctx.cfg, state["id"])
        # SPEC-0515: after a node failure re-gate failed the same way, one run is local
        local = state.get("gate_local") if live is None and lost is None else None
        res.gate_run = G.run_all(pcfg, state, ctx.cfg, seq=seq, stop=ctx.stop,
                                 tier="loop", adopt=live, lost=lost,     # SPEC-0328
                                 local=bool(local),
                                 local_reason=(local or {}).get("reason"),
                                 local_detail=(local or {}).get("detail"))
    except (ValueError, OSError) as e:
        res.error = str(e)
    return res


_ADOPT_GIT_TRIES = 3                 # `remote_gates.adoptable` retries a failing git this often
_ADOPT_GIT_PAUSE_S = 0.5


def _adoptable(rec: dict, worktree, stop=None) -> bool | None:
    """`remote_gates.adoptable`, retried a few times while git fails; None when it still
    fails (the caller defers, it neither adopts nor cancels)."""
    if not worktree:
        return False
    from . import remote_gates as RG
    for i in range(_ADOPT_GIT_TRIES):
        ok = RG.adoptable(rec, worktree)
        if ok is not None:
            return ok
        if i + 1 < _ADOPT_GIT_TRIES and stop is not None and stop.wait(_ADOPT_GIT_PAUSE_S):
            break
    return None


def _gate_committed(state: dict, rec: dict) -> bool:
    """The campaign's committed state already holds this record's gate (or a later one):
    `last_gate` carries the job's `event_uid`, or a `gate_seq` at or past the record's seq.
    `_transition` writes the ledger first and `state.json` last, so a crash before the state
    write still adopts the job (and the dedupe drops its replayed `gate` event)."""
    lg = state.get("last_gate") or {}
    if str(lg.get("event_uid") or "").startswith(f"{rec['job_id']}:"):
        return True
    gs = lg.get("gate_seq")
    return isinstance(gs, int) and not isinstance(gs, bool) \
        and gs >= int(rec.get("seq_or_run") or 0)


def _do_prepare_close(ctx: _Ctx, state: dict) -> StepResult:
    """The close's worktree phase (SPEC-0220): worker-safe, no main-checkout writes."""
    res = StepResult(state["id"], Step("close"), "CLOSING")
    held = CL.close_hold(ctx.cfg, state, ctx.project(state))       # SPEC-0420: red base
    if held:
        res.prepared = CL.PreparedClose(outcome=CL.CloseOutcome("CLOSING", held=held))
        return res
    # SPEC-0207: file the follow-up ideas first; the filer never raises, and its events and
    # summary reach `_apply` whether or not the close then succeeds.
    if state.get("kind") not in ("merge", "split", *RM.RED_KINDS):  # SPEC-0233/0290/0420
        res.events_before, res.followups = FU.file_followups(ctx.cfg, state, ctx.profile(state))
    try:
        res.prepared = CL.prepare_close(ctx.cfg, state, ctx.project(state), stop=ctx.stop)
    except (GitError, ValueError, OSError) as e:
        res.error = getattr(e, "tail", None) or str(e)
    except Exception as e:                      # nothing may drop the filer's events
        res.error = repr(e)
        res.crashed = repr(e)                   # SPEC-0220: still a report.errors entry
    return res


# ---- applying results (main thread) ---------------------------------------------------------

def _fresh(cfg, state: dict) -> dict | None:
    """Re-read the campaign from disk; None when a human moved it meanwhile (parked, abandoned
    or resumed) — then the step's events are still recorded but the transition is skipped."""
    on_disk = store.read_state(cfg, state["id"])
    if on_disk.get("state") != state.get("state") or state.get("_superseded"):
        return None
    return on_disk


def _apply(ctx: _Ctx, state: dict, res: StepResult) -> dict:
    """Apply one StepResult: events, counters, cost, then the transition. Returns the report
    row for `nw.queue.tick.v1`. SPEC-0484: an exception while applying parks only this
    campaign (`apply_error`, its result's runs closed) and never reaches the loop."""
    try:
        return _apply_step(ctx, state, res)
    except Exception as e:                 # noqa: BLE001 — one campaign's bug must not kill the runner
        return _contain_apply_error(ctx, state, res, e)


APPLY_ERROR_TEXT_MAX = 600


def _result_runs(res) -> list:
    """`(run_id, persona, run_result, budget, interrupted)` of every run a StepResult carries
    (a review fan-out's members, else the step's own run)."""
    out = []
    for r in (getattr(res, "reviews", None) or [res]):
        rid = getattr(r, "run_id", None)
        if rid:
            step = getattr(r, "step", None)
            out.append((rid, getattr(step, "persona", None) or "unknown",
                        getattr(r, "run_result", None), float(getattr(r, "budget", 0.0) or 0.0),
                        bool(getattr(r, "interrupted", False))))
    return out


def _close_result_runs(cfg, current: dict, res, err: str) -> list:
    """`run.end` events for the runs of `res` still open on the ledger, so a re-adopting
    runner finds nothing to re-apply (SPEC-0484). A result that cannot be recorded (it may be
    what broke the apply) is closed as a synthetic failure."""
    open_ids = {e.get("run_id") for e in _open_runs(cfg, current["id"])}
    events = []
    for rid, persona, rr, budget, interrupted in _result_runs(res):
        if rid not in open_ids:
            continue
        try:
            if rr is None:
                raise ValueError("no run result")
            events += _record_run(current, persona, rid, rr, budget, interrupted=interrupted)
        except Exception:                  # noqa: BLE001
            events += _record_run(current, persona, rid,
                                  _synthetic_failure(f"apply_error: {err}"), budget)
    return events


def _contain_apply_error(ctx, state: dict, res, exc: BaseException) -> dict:
    """SPEC-0484: park the campaign `apply_error` (detail = the exception), close the runs
    its result carried, journal `apply_error`. Never raises: a park that fails too is
    journaled `apply_error_uncontained` and the loop goes on."""
    import traceback
    cfg = ctx.cfg
    cid = (state or {}).get("id") or getattr(res, "campaign", None)
    step = getattr(res, "step", None)
    action = getattr(step, "kind", None)
    err = f"{type(exc).__name__}: {exc}"[:APPLY_ERROR_TEXT_MAX]
    run_ids = [r[0] for r in _result_runs(res)]
    row = {"campaign": cid, "from": getattr(res, "from_state", None), "to": None,
           "action": action, "run_id": getattr(res, "run_id", None), "cost_usd": None,
           "reason": "apply_error"}
    journal(cfg, "apply_error", campaign=cid, action=action, from_state=row["from"],
            run_ids=run_ids, error=err, traceback=traceback.format_exc()[-3000:])
    try:
        with store.item_lock(cfg, cid):
            current = store.read_state(cfg, cid)
            events = _close_result_runs(cfg, current, res, err)
            s = current.get("state")
            text = (f"applying the {action} result failed ({err}); the result was not applied"
                    + (f", runs {', '.join(run_ids)} closed" if run_ids else ""))
            if s in TERMINAL_STATES or s == "PARKED":
                events.append(("note", {"text": text + f"; the campaign stays {s}",
                                        "author": "queue"}))
                for etype, payload in events:
                    store.append_event(cfg, cid, etype, payload)
                store.write_state(cfg, cid, current)
                row["to"] = s
                return row
            resume = ((current.get("waiting") or {}).get("resume_to") if s == "WAITING"
                      else _resume_target(s))
            if resume not in RESUMABLE_STATES:
                resume = _resume_target(s)
            events.append(("note", {"text": text, "author": "queue"}))
            _park(cfg, current, "apply_error", f"{action} apply failed: {err}", resume,
                  extra_events=events,
                  extra={"apply_error": {"action": action, "error": err, "run_ids": run_ids,
                                         "from_state": row["from"]}})
            row["to"] = "PARKED"
    except Exception as e2:                # noqa: BLE001
        journal(cfg, "apply_error_uncontained", campaign=cid, action=action, error=err,
                park_error=f"{type(e2).__name__}: {e2}"[:APPLY_ERROR_TEXT_MAX])
        row["reason"] = "apply_error_uncontained"
    return row


def _apply_step(ctx: _Ctx, state: dict, res: StepResult) -> dict:
    """`_apply`'s body (SPEC-0484 keeps the name `_apply` for the contained entry point)."""
    cfg = ctx.cfg
    cid = state["id"]
    if res.step.kind == "review" and not res.reviews and not res.error:
        # one reviewer's bare result (SPEC-0201): the aggregate path is the only code path
        res = StepResult(res.campaign, res.step, res.from_state, reviews=[res])
    row = {"campaign": cid, "from": res.from_state, "to": None, "action": res.step.kind,
           "run_id": res.run_id, "cost_usd": None, "reason": None}
    events: list = list(res.events_before)
    if res.lock_cleanup is not None:       # first: the error/park paths below must carry it too
        lc = res.lock_cleanup
        events.append(("note", {
            "text": (f"removed stale {lc['path']} (no process held it; left by a killed run)"
                     if lc["removed"] else
                     f"index.lock present and not removed ({lc['reason']}; "
                     f"holders {lc['holders']}): {lc['path']}"),
            "author": "queue"}))
    try:
        profile = ctx.profile(state)
    except ValueError:
        if not res.error:
            raise
        profile = {}                   # the step failed on this very profile; the park needs none
    counters = _counters(state)

    def finish(to, reason, *, park=None):
        if res.step.kind in ("implement", "write_spec") and TR.tiers_on(state):
            _lean_cap_check(cfg, state, events, res.step.kind)       # SPEC-0553
        # SPEC-0254: read, decision and write-back are one critical section; `_park` and
        # `_transition` re-take the (reentrant) lock for their own write.
        with store.item_lock(cfg, cid):
            current = _fresh(cfg, state)
            if current is None:
                # A human changed the state while the step ran: keep the facts, not the move.
                with store.item_lock(cfg, cid):
                    for etype, payload in events:
                        store.append_event(cfg, cid, etype, payload)
                    store.append_event(cfg, cid, "note", {
                        "text": f"step {res.step.kind} finished after a human changed the state; "
                                f"transition to {to} skipped", "author": "queue"})
                row["to"], row["reason"] = store.read_state(cfg, cid).get("state"), "superseded_by_human"
                return row
            current.pop("remote_excluded", None)     # SPEC-0344: cleared when the step completes
            for k in ("counters", "cost", "repo", "spec", "last_gate", "last_verdict", "last_review",
                      "pending_brief", "close", "review_carry", "closing", "spec_write", "spec_id",
                      "merge", "admission", "spec_review", "split",   # SPEC-0279: amend flips it
                      "gate_exclusive", "last_green_loop", "close_red", "gate_retry_pending",
                      "divergence", "conflict_mail",
                      "mem_boost", "gate_local",             # SPEC-0515
                      "raised", "deep_close", "cap_ignored",    # SPEC-0553 (lean only)
                      "packet", "packet_prev", "review_heads",   # SPEC-0554 (lean only)
                      "sessions"):                               # SPEC-0556 (lean only)
                if k in state:
                    current[k] = state[k]
            for k in state.pop("_lean_copy", ()):            # SPEC-0553: the open's re-pick
                current[k] = state[k]
            requested = current.get("park_requested")
            escalating = bool(requested) and requested.get("reason") == "human_decision"
            if requested and park is None and to not in TERMINAL_STATES:
                req = requested
                detail = req.get("detail") or req.get("reason") or "parked"
                if escalating:                               # SPEC-0232
                    _park(cfg, current, "human_decision", detail, _resume_target(to),
                          by=req.get("by") or "human", extra={"escalated_from": None},
                          extra_events=events)
                    row["to"], row["reason"] = "PARKED", "human_decision"
                    return row
                _park(cfg, current, "human", detail, _resume_target(to),
                      by=req.get("by") or "human", extra_events=events)
                row["to"], row["reason"] = "PARKED", "human"
                return row
            if park and escalating:
                # the step parked by itself while an escalation was pending: park as the human's,
                # keeping the step's reason as `escalated_from`, so the record matches the park
                req = requested
                _park(cfg, current, "human_decision",
                      f"{req.get('detail')} (step parked: {park['detail']})", park["resume_to"],
                      by=req.get("by") or "human", extra={"escalated_from": park["reason"]},
                      extra_events=events)
                from . import escalations as ESC
                ESC.note_park(cfg, cid, escalated_from=park["reason"], resume_to=park["resume_to"])
                row["to"], row["reason"] = "PARKED", "human_decision"
                return row
            if park:
                extra = {}
                if park.get("escalated_from"):
                    extra["escalated_from"] = park["escalated_from"]
                _park(cfg, current, park["reason"], park["detail"], park["resume_to"],
                      extra_events=events, extra=extra or None, auto_ok=not park.get("no_auto"))
                row["to"], row["reason"] = "PARKED", park["reason"]
                return row
            _transition(cfg, current, to, reason, extra_events=events)
            row["to"], row["reason"] = to, reason
            return row

    kind = res.step.kind
    if kind == "unknown":              # SPEC-0225: an adopted run of an unregistered persona
        rr = res.run_result or _synthetic_failure(res.error or "run produced no result")
        persona = res.step.persona
        events += _record_run(state, persona, res.run_id, rr, res.budget,
                              interrupted=res.interrupted)
        events.append(("note", {"text": f"adopted run of unregistered persona {persona}; "
                                        "result recorded, no transition", "author": "queue"}))
        with store.item_lock(cfg, cid):
            for etype, payload in events:
                store.append_event(cfg, cid, etype, payload)
            current = store.read_state(cfg, cid)
            current["cost"] = state.get("cost", current.get("cost"))
            store.write_state(cfg, cid, current)
        row["cost_usd"] = rr.cost_usd if rr.cost_usd is not None else res.budget
        row["to"], row["reason"] = store.read_state(cfg, cid).get("state"), "unregistered_persona"
        return row
    if res.error and res.config_error:
        resume = "QUEUED" if kind == "open" and res.from_state == "OPENING" else res.from_state
        return finish("PARKED", "config_invalid", park={
            "reason": "config_invalid",
            "detail": res.error if res.error.startswith("profile: ")
            else f"project config: {res.error}",
            "resume_to": resume if resume in RESUMABLE_STATES else "IMPLEMENTING"})
    if res.error and kind != "close":
        reason = {"open": "open_failed", "write_spec": "spec_write_failed",
                  "spec_review": "spec_review_exhausted",
                  "finalize_split": "spec_write_failed"}.get(kind, "open_failed")
        detail = f"{kind} step failed: {res.error}"
        if kind == "write_spec" and (res.write or {}).get("rollback_failed"):
            detail = res.error
        if kind == "write_spec" and res.run_result is not None:     # the run was paid for
            events += _record_run(state, res.step.persona, res.run_id, res.run_result,
                                  res.budget, interrupted=res.interrupted)
        resume = {"open": "QUEUED", "implement": "IMPLEMENTING", "gate": "GATING",
                  "triage": "GATING", "review": "REVIEWING",
                  "divergence": "DIVERGENCE_TRIAGE",
                  "resolve": "CLOSING", "write_spec": "SPEC_WRITING",
                  "spec_review": "SPEC_REVIEW",
                  "finalize_split": "SPEC_WRITING"}.get(kind, "IMPLEMENTING")
        if kind in ("resolve", "spec_review") and res.run_id:    # run.start is on the ledger: close it, run or no run
            rr = res.run_result or replace(_synthetic_failure(str(res.error)), cost_usd=0.0)
            events += _record_run(state, res.step.persona, res.run_id, rr, res.budget,
                                  interrupted=res.interrupted)
        if kind == "open" and res.from_state in ACTIVE_STATES and res.from_state != "OPENING":
            # A profile-file error on an already-open campaign (the tick's planning loop):
            # it has a worktree, so resume where it was instead of re-opening.
            resume = _resume_target(res.from_state)
        return finish("PARKED", reason, park={"reason": reason, "detail": detail,
                                               "resume_to": resume})

    if kind == "open":
        if state.get("kind") == "merge":
            return finish("CLOSING", "opened")
        if state.get("kind") == "revert":                              # SPEC-0420
            if res.revert_conflict:
                return finish("PARKED", "revert_conflict", park={
                    "reason": "revert_conflict", "detail": res.revert_conflict,
                    "resume_to": "QUEUED", "no_auto": True})
            return finish("CLOSING", "opened")
        if state.get("kind") == "split":                               # SPEC-0290
            return finish("SPEC_WRITING", "opened")
        _lean_open(ctx, state, events)                                 # SPEC-0553
        if res.resumed_to:
            events.append(("note", {"text": f"resumed into existing worktree → {res.resumed_to}",
                                    "author": "queue"}))
            return finish(res.resumed_to, "resumed_worktree")
        pending = state.get("admission") == "revise_pending"           # SPEC-0279
        return finish("SPEC_WRITING" if has_no_spec(state) or pending else "IMPLEMENTING",
                      "opened")

    if kind == "review":
        return _apply_reviews(ctx, state, profile, counters, res, row, events, finish)

    if kind == "write_spec":
        return _apply_spec_write(ctx, state, counters, res, row, events, finish)

    if kind == "spec_review":
        return _apply_spec_review(ctx, state, profile, counters, res, row, events, finish)

    if kind == "finalize_split":                                       # SPEC-0290
        sp = state["split"]
        if res.write.get("sha"):
            events.append(("commit", {"sha": res.write["sha"],
                                      "message": f"split {sp['original']}"}))
        state.setdefault("repo", {})["head"] = res.write.get("head") or state["repo"].get("head")
        events.append(("split_finalized", {"original": sp["original"],
                                           "parts": [p["spec_id"] for p in sp["parts"]]}))
        return finish("CLOSING", "split_written")

    if kind in ("implement", "triage", "resolve", "divergence"):
        persona = res.step.persona
        rr = res.run_result
        events += _record_run(state, persona, res.run_id, rr, res.budget,
                              interrupted=res.interrupted)
        row["cost_usd"] = rr.cost_usd if rr.cost_usd is not None else res.budget
        row["cost_estimated"] = rr.cost_usd is None
        state["last_verdict"] = {"persona": persona, "run": res.run_id, "verdict": rr.verdict,
                                 "verdict_ok": rr.verdict_ok}
        if res.interrupted or rr.interrupted:
            if _fresh(cfg, state) is None:               # an adopted run a human moved past
                return finish(state["state"], "interrupted")
            # State unchanged: the next tick spawns a fresh agent (run.end carries interrupted).
            with store.item_lock(cfg, cid):
                for etype, payload in events:
                    store.append_event(cfg, cid, etype, payload)
                store.write_state(cfg, cid, state)
            row["to"], row["reason"] = res.from_state, "interrupted"
            return row
        if kind == "resolve":
            return _apply_resolve(state, counters, res, rr, row, events, finish)
        if kind == "divergence":
            return _apply_divergence(ctx, state, counters, res, rr, events, finish)
        if kind == "implement":
            if res.commit_sha is not None or res.commit_message:
                events.append(("commit", {"sha": res.commit_sha, "message": res.commit_message}))
                if res.commit_sha:
                    state.setdefault("repo", {})["head"] = res.commit_sha
            state["pending_brief"] = None
            status = (rr.verdict or {}).get("status") if isinstance(rr.verdict, dict) else None
            if status == "blocked":
                counters["blocked_streak"] += 1
                if counters["blocked_streak"] >= BLOCKED_STREAK_MAX:
                    blockers = "; ".join(str(b) for b in (rr.verdict.get("blockers") or [])) \
                        or (rr.verdict.get("summary") or "")
                    return finish("PARKED", "implementer_blocked", park={
                        "reason": "implementer_blocked",
                        "detail": f"blocked {counters['blocked_streak']}× in a row: {blockers}",
                        "resume_to": "IMPLEMENTING"})
                elif not res.commit_sha:                  # SPEC-0377: nothing to gate
                    events.append(("note", {
                        "text": "implementer blocked with no new commit; retrying instead of "
                                f"gating (blocked_streak {counters['blocked_streak']}"
                                f"/{BLOCKED_STREAK_MAX})", "author": "queue"}))
                    return finish("IMPLEMENTING", "implementer_blocked_retry")
            else:
                counters["blocked_streak"] = 0
            return finish("GATING", "implemented")
        # triage
        renamed = CL.adopt_renamed_spec(state, (state.get("repo") or {}).get("worktree"))
        if renamed:
            # The spec-changed failure was the old path. The branch renamed the file.
            events.append(("note", {"text": f"spec file was renamed to {renamed}; "
                                            "closing against it", "author": "queue"}))
            return finish("CLOSING", "spec_renamed")
        decision = decide_triage(state, profile, rr.verdict, rr.verdict_ok)
        if decision.kind == "none":                      # malformed verdict, retry allowed
            if rr is not None and PG.is_usage_exhausted(rr):
                try:
                    PG.note_usage(cfg)
                except (OSError, ValueError):
                    pass
                return finish("TRIAGING", "usage_paused")
            counters["triage_retries"] += 1
            return finish("TRIAGING", "triage_retry")
        if rr.verdict_ok:                                # SPEC-0408
            events.append(("triage", _gate_triage_event(cfg, cid, state, res.run_id,
                                                        rr.verdict, decision)))
        if decision.kind == "park":
            if decision.reason == "loop_exhausted" and _maybe_loop_auto_resume(
                    state, counters, list((rr.verdict or {}).get("must_fix") or [])):
                v = rr.verdict or {}
                notes = ((state.get("pending_brief") or {}).get("human_notes") or [])
                state["pending_brief"] = {
                    "source": "gate_triager", "fix_brief": v.get("fix_brief"),
                    "must_fix": list(v.get("must_fix") or []), "human_notes": list(notes)}
                events.append(("note", {"text": "loop_exhausted auto-resume: the must-fix "
                                                "set shrank", "author": "queue"}))
                return _finish_fix(ctx, state, events, finish, "gate", "loop_auto_resume")
            return finish("PARKED", decision.reason, park={
                "reason": decision.reason, "detail": decision.detail,
                "resume_to": decision.resume_to})
        if decision.kind == "gate":
            counters["gate_retries"] += 1
            state["gate_retry_pending"] = True           # SPEC-0293: the next gate run follows it
            return finish("GATING", "retry_gate")
        if decision.kind == "close":
            return finish("CLOSING", "waived")
        # fix
        titles = must_fix_titles(list((rr.verdict or {}).get("must_fix") or []))
        if int(counters.get("loops") or 0) == 0:
            note_must_fix_baseline(counters, titles)
        counters["loops"] += 1
        counters["gate_retries"] = 0
        counters["timeout_regates"] = 0
        counters["triage_retries"] = 0
        v = rr.verdict or {}
        notes = ((state.get("pending_brief") or {}).get("human_notes") or [])
        state["pending_brief"] = {"source": "gate_triager", "fix_brief": v.get("fix_brief"),
                                  "must_fix": list(v.get("must_fix") or []),
                                  "human_notes": list(notes)}
        return _finish_fix(ctx, state, events, finish, "gate", "triage_fix")

    if kind == "gate":
        if res.deferred:
            # SPEC-0341: adoption could not read the worktree; nothing ran, the live job is
            # left alone and the next tick tries again (still GATING). Only the notes of jobs
            # it cancelled are written; the gate step's own events come when it runs.
            notes = [e for e in events if e[0] == "note"]
            if notes:
                with store.item_lock(cfg, cid):
                    for etype, payload in notes:
                        store.append_event(cfg, cid, etype, payload)
            row["to"], row["reason"] = "GATING", "adoption_deferred"
            return row
        run = res.gate_run
        if run is not None and run.interrupted and ctx.stop.is_set():
            # SPEC-0229: a gate a stop killed (or never started) is discarded: no event, no
            # transition, no counters; the next runner re-gates from scratch
            row["to"], row["reason"] = "GATING", "interrupted"
            return row
        counters["gate_runs"] += 1
        payload = G.as_payload(run)
        if run.event_uid:                   # SPEC-0341: a replayed job's `gate` lands once
            payload["event_uid"] = run.event_uid
        if run.job_id:
            # SPEC-0341: the job record goes `done` only once its `gate` event is written
            _finish = finish

            def finish(to, reason, **kw):
                out = _finish(to, reason, **kw)
                RJ.mark_done(cfg, run.job_id)
                return out
        events.append(("gate", payload))
        previous = state.get("last_gate")
        state["last_gate"] = payload
        # SPEC-0359: the green loop result a clean close may reuse; any red loop run clears it
        state["last_green_loop"] = payload if (run.ok and not run.interrupted) else None
        state["gate_exclusive"] = None
        retried = bool(state.get("gate_retry_pending"))   # only the run a `retry_gate` led to
        state["gate_retry_pending"] = False
        events += _flaky_events(cfg, state, payload, previous, run.ok, retried=retried)
        state["gate_local"] = None                       # SPEC-0515: one local run only
        infra = infra_only(payload)                      # SPEC-0515: never triaged
        if not (infra and (_infra_gates(payload, "node_fail")
                           or _infra_gates(payload, "preempted"))):
            counters["node_fail_regates"] = 0
        if run.ok:
            counters["timeout_regates"] = 0
            if state.get("kind") == "fixforward":       # SPEC-0420: full suite + weakening check
                return finish("CLOSING", "gates_green")
            return finish("REVIEWING", "gates_green")
        if infra:
            decision = decide_gate_infra(state, _gate_mem_mb(ctx, state))
            if decision.kind == "park":
                return finish("PARKED", decision.reason, park={
                    "reason": decision.reason, "detail": decision.detail,
                    "resume_to": decision.resume_to})
            codes = sorted({str(r.get("infra")) for r in _infra_gates(payload)})
            if decision.reason == "gate_oom_regate":
                counters["oom_retries"] = int(counters.get("oom_retries") or 0) + 1
                state["mem_boost"] = MEM_BOOST
            elif decision.reason == "gate_node_fail_regate":
                counters["node_fail_regates"] = int(counters.get("node_fail_regates") or 0) + 1
            elif decision.reason == "gate_node_fail_local":
                state["gate_local"] = {"reason": "node_fail",
                                       "detail": f"the worker's Slurm job ended {'/'.join(codes)} "
                                                 f"twice; this run is local"}
            if decision.reason != "slurm_timeout":
                events.append(("note", {
                    "text": f"gate infra outcome {'/'.join(codes)} "
                            f"({', '.join(str(r.get('name')) for r in _infra_gates(payload))}): "
                            f"{decision.reason.replace('_', ' ')}, no triage",
                    "author": "queue"}))
                return finish("GATING", decision.reason)
        if infra or timeout_only(payload):               # SPEC-0311: load, not a code fix
            timeouts = _gate_timeouts(ctx, state)
            decision = decide_gate_timeout(state, timeouts)
            if decision.kind == "park":
                return finish("PARKED", decision.reason, park={
                    "reason": decision.reason, "detail": decision.detail,
                    "resume_to": decision.resume_to})
            counters["timeout_regates"] += 1
            # SPEC-0510: a worker under Slurm guarantees each job its allocation: the re-gate
            # is a normal gate there, no exclusive box
            if not workers.runs_slurm(cfg, run.worker):
                state["gate_exclusive"] = {"entered_at": store.now_iso()}
            names = _timed_out_names(payload, timeouts) or [   # SPEC-0515: Slurm's time limit
                f"{r.get('name')} (Slurm time limit)" for r in _infra_gates(payload, "slurm_timeout")]
            events.append(("note", {
                "text": f"gate timeout: {', '.join(names)}; "
                        f"re-gating alone (timeout_regates "
                        f"{counters['timeout_regates']}/{TIMEOUT_REGATE_MAX})",
                "author": "queue"}))
            return finish("GATING", decision.reason)
        counters["timeout_regates"] = 0
        return finish("TRIAGING", "gates_failed")

    if kind == "close":
        if ctx.stop.is_set() and res.prepared is not None:
            # SPEC-0220: a result that lands during a stop is discarded — a killed re-gate
            # must not send the campaign to TRIAGING; the next run redoes the close
            row["to"], row["reason"] = "CLOSING", "interrupted"
            return row
        held = res.prepared.outcome.held if res.prepared is not None \
            and res.prepared.outcome is not None else None
        if held and not res.error:                       # SPEC-0420: no attempt, no park
            return _apply_close_held(cfg, state, held, events, finish)
        counters["close_attempts"] += 1
        if res.followups is not None:
            fu = res.followups                      # counts and ids, plus the detail lists
            row["followups"] = {"filed": len(fu["filed"]), "unfiled": len(fu["unfiled"]),
                                "capped": len(fu.get("capped") or []),
                                "idea_id": fu.get("idea_id"),
                                "ids": list(dict.fromkeys(f["idea_id"] for f in fu["filed"])),
                                "items": list(fu["filed"]), "unfiled_items": list(fu["unfiled"]),
                                "waived": list(fu["waived"])}
        if res.error:
            return finish("PARKED", "open_failed", park={
                "reason": "open_failed", "detail": f"close step failed: {res.error}",
                "resume_to": "CLOSING"})
        oc = res.close_outcome
        if oc is None and res.prepared.outcome is not None:
            oc = res.prepared.outcome
        absorbed = list(oc.events) if oc is not None else list(res.prepared.events)
        # SPEC-0359: a red full close gate pins that gate to full runs for this campaign
        CL.note_close_gates(state, oc.gate_run if oc is not None
                            else (res.prepared.gate_run if res.prepared is not None else None))
        if oc is None:
            # finish phase (main thread): a park / abandon / resume made during the worktree
            # phase wins over a merge that has not happened yet
            current = _fresh(cfg, state)
            if current is None or current.get("park_requested") or state.get("park_requested"):
                _absorb_close_events(state, counters, events, absorbed)
                return finish("CLOSING", "close_superseded")
            attempts = int((state.get("closing") or {}).get("attempts") or 0) + 1
            state["closing"] = {**(state.get("closing") or {}), "attempts": attempts,
                                "target_sha": res.prepared.target_sha}
            try:
                with _busy(ctx.lease, "close", campaign=cid,
                           budget_s=events_settings(cfg)["gate_budget_s"]):
                    oc = CL.finish_close(cfg, state, ctx.project(state), res.prepared,
                                         attempts=attempts)
            except (GitError, ValueError, OSError) as e:
                _absorb_close_events(state, counters, events, absorbed)
                return finish("PARKED", "open_failed", park={
                    "reason": "open_failed", "resume_to": "CLOSING",
                    "detail": f"close step failed: {getattr(e, 'tail', None) or e}"})
            absorbed += list(oc.events)
        _absorb_close_events(state, counters, events, absorbed)
        if oc.retry:
            events.append(("finding", {
                "persona": "close", "severity": "info", "gate": "base",
                "title": "base moved during re-gate; redoing close", "detail": "", "files": []}))
            return finish("CLOSING", "base_moved")
        if oc.next_state in ("MERGED", "HANDED_OFF"):
            state["close"] = dict(oc.close)
            if res.followups is not None:           # after the assignment replaced the dict
                state["close"]["followups"] = res.followups
            _reconcile_closed(cfg, state, events, oc.next_state)
            _release_idea_link(cfg, state, events)      # SPEC-0292: independent of the reconcile
            if oc.next_state == "MERGED":
                # SPEC-0294: after the release, so a child can link the idea this campaign held;
                # the state is not MERGED on disk yet
                _queue_split_children(cfg, state, events)
            state["park"] = None
            out = finish(oc.next_state, "closed")
            if oc.next_state == "MERGED":
                _red_fix_merged(cfg, state, oc.close)               # SPEC-0420
            return out
        if oc.next_state == "TRIAGING" and state.get("kind") in ("merge", "split", "revert"):
            # SPEC-0233: no gate triager for a merge request (SPEC-0290: nor a split campaign);
            # the conductor relays the failure
            return finish("PARKED", "gates_failed", park={
                "reason": "gates_failed", "detail": oc.gate_detail or "gates failed",
                "resume_to": "CLOSING"})
        if oc.next_state == "TRIAGING":
            if oc.synthetic_gate is not None:
                if oc.synthetic_gate.get("name") == "spec-dod":
                    # SPEC-0468: a spec that is not `done` is not an implement loop.
                    tail = str(oc.synthetic_gate.get("tail") or "spec is not done").strip()
                    return finish("PARKED", "spec_status", park={
                        "reason": "spec_status", "resume_to": "CLOSING",
                        "detail": "only the spec status gate failed; mark the spec done "
                                  "or waive it, then resume close: " + tail[:500]})
                state["last_gate"] = {"gate_seq": None, "ok": False, "started_at": store.now_iso(),
                                      "duration_s": 0.0, "results": [oc.synthetic_gate]}
                return finish("TRIAGING", "spec_dod")
            return finish("TRIAGING", "regate_failed")
        park = oc.park or {"reason": "open_failed", "detail": "close returned PARKED without a reason",
                           "resume_to": "CLOSING"}
        if park["reason"] == "rebase_conflict" and park.get("conflicts"):
            outcome = _dispatch_resolver(ctx, state, res, park, events)
            if outcome == "dispatched":
                return finish("CLOSING", "resolve_dispatched")
            if outcome == "runs_out":       # SPEC-0303: every run verified; the base kept moving
                park = _base_moving_park(state, park)
            else:
                park = _spent_resolver_park(state, park)
        return finish("PARKED", park["reason"], park=park)

    return finish(res.from_state, "noop")


def sweep_handed_off(cfg, *, apply=False, limit=20, budget_s=60, view=None,
                     clock=time.monotonic) -> list[dict]:
    """SPEC-0283: check the PR of each HANDED_OFF campaign with `view` (default
    `gitops.gh_pr_view`) and return one `{campaign, pr_url, pr_state, action, detail}` row per
    campaign checked. Least recently checked first (`close.pr_checked_at`, missing first, ties
    oldest `created_at` first), at most `limit`, stopping once `budget_s` is spent. Nothing is
    written without `apply`. Every write re-reads the state and re-checks its precondition under
    the campaign lock; a campaign that no longer qualifies is reported `skipped`. The one terminal
    edge that may change is HANDED_OFF -> MERGED."""
    view = view or git.gh_pr_view
    cands = [st for st in store.list_campaigns(cfg, include_terminal=True, state="HANDED_OFF")
             if (st.get("close") or {}).get("pr_url")
             and (st["close"].get("pr_state") != "CLOSED")]
    cands.sort(key=lambda st: st.get("created_at") or "")
    cands.sort(key=lambda st: st["close"].get("pr_checked_at") or "")
    start, rows = clock(), []
    for st in cands[:max(0, int(limit))]:
        if clock() - start >= budget_s:
            break
        cid, url = st["id"], st["close"]["pr_url"]
        row = {"campaign": cid, "pr_url": url, "pr_state": None, "action": "error", "detail": ""}
        rows.append(row)
        try:
            pr = view(url)
        except GitError as e:
            row["detail"] = (e.tail or str(e)).strip()
            pr = None
        else:
            action = {"MERGED": "merge", "CLOSED": "closed", "OPEN": "open"}.get(pr["state"])
            if action:
                row["pr_state"], row["action"] = pr["state"], action
            else:
                row["detail"] = f"unexpected PR state {pr['state']!r}"
        if apply:
            _sweep_write(cfg, cid, row, pr)
    return rows


def _sweep_write(cfg, cid: str, row: dict, pr: dict | None) -> None:
    """Apply one sweep row under the campaign lock (see `sweep_handed_off`)."""
    with store.item_lock(cfg, cid):
        st = store.read_state(cfg, cid)
        if st.get("state") != "HANDED_OFF":
            row["action"], row["detail"] = "skipped", f"state is {st.get('state')}"
            return
        close = st["close"] = dict(st.get("close") or {})
        action = row["action"]
        if action == "closed" and close.get("pr_state") == "CLOSED":
            row["action"], row["detail"] = "skipped", "closed PR already recorded"
            return
        close["pr_checked_at"] = store.now_iso()
        if action == "merge":
            close.update(merged_sha=pr["merge_sha"], merged_at=pr["merged_at"], pr_state="MERGED")
            events: list = []
            _reconcile_closed(cfg, st, events, "MERGED")
            _release_idea_link(cfg, st, events)         # SPEC-0292
            _queue_split_children(cfg, st, events)      # SPEC-0294: after the release (children link the idea)
            _transition(cfg, st, "MERGED", "pr_merged", by="sweep", extra_events=events)
            return
        if action == "closed":
            close["pr_state"] = "CLOSED"
            store.append_event(cfg, cid, "finding", {
                "persona": "close", "severity": "major", "gate": "pr",
                "title": "PR closed without merging", "detail": row["pr_url"], "files": []},
                by="sweep")
        store.write_state(cfg, cid, st)


def _reconcile_closed(cfg, state: dict, events: list, outcome: str) -> None:
    """SPEC-0257: after a merge, advance the merged spec's idea by `specs.reconcile_ideas`
    scoped to that spec. Best-effort: the merge is irreversible, so a failure is a minor
    `bookkeeping` finding and never changes the campaign state. Skipped silently without a spec
    (merge-kind campaigns) and with an info finding when the repo root is unknown or nightwright's spec
    store cannot be located. SPEC-0325: a campaign whose repo is not nightwright's spec store (a second
    project) reconciles against its own repo's spec (`_reconcile_project_spec`).
    SPEC-0280: for `HANDED_OFF` the same reconcile runs (an accepted spec gives SPECCED), then
    `:HANDED_OFF: <pr_url>` is set on the spec's sole claimant idea."""
    spec_id = state.get("spec_id") or (state.get("spec") or {}).get("id")
    if not spec_id or state.get("kind") == "split":       # SPEC-0290: no idea tracks the parts
        return
    if SP._is_outbound_spec_id(spec_id):    # SPEC-0297: the outbox, not nightwright's spec store
        return _pull_outbound(cfg, state, events, spec_id, outcome)

    def finding(severity, title, detail=""):
        events.append(("finding", {"persona": "close", "severity": severity,
                                   "gate": "bookkeeping", "title": title, "detail": detail,
                                   "files": []}))

    def snapshot():
        # id -> every (state, SPEC) carrying it: rotated org files can give two ideas one `:ID:`
        snap: dict = {}
        for t in _ideas(cfg):
            snap.setdefault((t.properties.get("ID") or t.id or "").strip(), []).append(
                (t.state, (t.properties.get("SPEC") or "").strip()))
        return {k: sorted(v, key=str) for k, v in snap.items()}

    def reported(idea_ids, before):
        after = snapshot()
        for idea_id in idea_ids:
            was, now = list(before.get(idea_id) or []), list(after.get(idea_id) or [])
            # the idea that moved is the one in only one of the two lists, not a twin's state
            gone = [v for v in was if v not in now] or was
            came = [v for v in now if v not in was] or now
            events.append(("idea_reconciled", {
                "spec_id": spec_id, "idea_id": idea_id,
                "from": (gone[0] if gone else (None,))[0],
                "to": (came[0] if came else (None,))[0]}))

    before = None
    try:
        root = (state.get("repo") or {}).get("root")
        try:
            specs_dir = SP._specs_dir(cfg).resolve()
        except ValueError:                  # no specs dir configured or locatable
            specs_dir = None
        # an unknown repo root is not "the current directory": skip rather than guess
        if not root or specs_dir is None or not specs_dir.is_relative_to(Path(root).resolve()):
            if root and specs_dir is not None and (state.get("spec") or {}).get("rel_path"):
                # SPEC-0325: the spec lives in the campaign's own project repo
                return _reconcile_project_spec(cfg, state, events, spec_id, outcome, finding)
            finding("info", "idea reconcile skipped: spec store is not this repo")
            return
        before = snapshot()
        # backlinks=False: no spec-file write in the main checkout (it would park the next close)
        plan = SP.reconcile_ideas(cfg, apply=True, spec_ids=[spec_id], backlinks=False)
        reported(dict.fromkeys(i for i, _s, a, _d in plan if a in ("link", "advance")), before)
        for idea_id, _s, action, detail in plan:
            if action == "flag-ambiguous":
                finding("minor", "idea reconcile ambiguous", f"{idea_id}: {detail}")
            elif action == "flag-mismatch":
                finding("minor", "idea reconcile mismatch", f"{idea_id}: {detail}")
            elif action == "flag-no-backlink":
                finding("info", "idea reconcile: spec has no source_idea", f"{idea_id}: {detail}")
            elif action == "flag-foreign":      # SPEC-0327
                finding("minor", "idea reconcile: foreign idea", f"{idea_id}: {detail}")
        # an idea campaign's PR only adds the spec (never `done`), so reconcile would never clear it
        if outcome == "HANDED_OFF" and state.get("kind") != "idea":
            _mark_handed_off(cfg, state, events, spec_id, finding)
    except Exception as e:                  # noqa: BLE001 — never block a finished merge
        finding("minor", "idea reconcile failed", str(e) or type(e).__name__)
        if before is not None:              # a half-done write pass: ledger what did land
            try:
                now = snapshot()
                reported([i for i, v in before.items() if now.get(i) != v], before)
            except Exception:               # noqa: BLE001 — the finding above already says it failed
                pass


def _project_spec_fm(state: dict) -> tuple[dict | None, str | None]:
    """SPEC-0325: the frontmatter of a project-repo campaign's spec as merged, read with
    `git show <ref>:<rel_path>` in the project repo (never the working tree, never a fetch).
    Refs tried in order: `close.merged_sha`, `<remote-tracking origin>/<base>`, the campaign
    branch head (`repo.head`: what the PR merged, for a local clone that has not fetched the
    merge yet). Returns `(fm, ref)` or `(None, None)`."""
    repo, close = state.get("repo") or {}, state.get("close") or {}
    rel = (state.get("spec") or {}).get("rel_path") or ""
    base = repo.get("base") or "main"
    for ref in (close.get("merged_sha"), f"origin/{base}", repo.get("head")):
        if not ref:
            continue
        r = git.git(repo["root"], "show", f"{ref}:{rel}", check=False)
        if r.returncode != 0:
            continue
        fm, _body = split_frontmatter(r.stdout or "")
        if fm:
            return fm, ref
    return None, None


def _reconcile_project_spec(cfg, state: dict, events: list, spec_id: str, outcome: str,
                            finding) -> None:
    """SPEC-0325: advance the idea of a spec that lives in the campaign's own project repo (a
    second project, not nightwright's spec store). The spec is read from git (`_project_spec_fm`); the
    bare `SPEC-NNNN` id is never resolved against nightwright's internal specs (it would name another
    spec there, IDEA-533). The idea is the spec's `source_idea`, else the campaign's linked or
    own idea, picked with the SPEC-0267 identity rule. Status `done` ships it (`SHIPPED`: the
    work landed in another repo, as for an outbound spec; also from `PROMOTED`, which is what a
    bare-id collision leaves behind); `accepted`/`in-progress` gives `SPECCED` from IDEA/INCUBATE;
    `superseded`/`rejected` drops a pre-terminal idea. No spec file is ever written. `HANDED_OFF`
    waits: the base does not have the spec yet; the sweep reaches here with `MERGED`. Errors
    propagate to `_reconcile_closed`, which turns them into a minor finding."""
    from .. import org as ORG
    from .. import org_write as OW
    if outcome != "MERGED":
        finding("info", "project idea reconcile waits for the merge",
                f"{spec_id}: reconciled when the PR merges (`nw queue sweep-prs`)")
        return
    fm, ref = _project_spec_fm(state)
    if fm is None:
        finding("minor", "project idea reconcile: spec not readable",
                f"{(state.get('spec') or {}).get('rel_path')} at the merged sha, origin or head")
        return
    status = str(fm.get("status") or "").strip().lower()
    want_id = (str(fm.get("source_idea") or "").strip()
               or str((state.get("idea_link") or {}).get("id") or "").strip()
               or ("" if _is_request_only(state)             # SPEC-0541: `REQ-…` is no idea id
                   else str((state.get("idea") or {}).get("id") or "").strip())).upper()
    if not want_id:
        finding("info", "idea reconcile: spec has no source_idea", f"{spec_id} ({ref})")
        return
    cands = [t for t in _ideas(cfg)
             if (t.properties.get("ID") or t.id or "").strip().upper() == want_id]
    if not cands:
        finding("minor", "project idea reconcile: idea not found", want_id)
        return
    task, bad = SP._pick_idea(cands, SP._as_date(fm.get("created")))
    if bad is not None:
        finding("minor", f"idea reconcile {bad[0].removeprefix('flag-')}", f"{want_id}: {bad[1]}")
        return
    linked = (task.properties.get("SPEC") or "").strip()
    if linked and linked != spec_id:
        finding("minor", "idea reconcile mismatch", f"{want_id} links {linked}, not {spec_id}")
        return
    cur = (task.state or "").upper()
    target = None
    if status == "done" and cur in SP._PRE_TERMINAL | {"PROMOTED"}:
        target = "SHIPPED"
    elif status in ("superseded", "rejected") and cur in SP._PRE_TERMINAL:
        target = "DROPPED"
    elif status in ("accepted", "in-progress") and cur in ("IDEA", "INCUBATE"):
        target = "SPECCED"
    if status != "done":
        finding("minor", f"project spec status is {status or '?'}, not done",
                f"{spec_id} at {str(ref)[:10]}")
    if target is None:
        return
    pin = (task.file, task.heading) if len(cands) > 1 else None
    if not linked:
        OW.set_property(cfg, task, "SPEC", spec_id)
        ORG.invalidate_tasks_cache()
        task = SP._resolve_idea(cfg, want_id, pin)
    if task is not None and target in SP._CLEARS_HANDED_OFF \
            and (task.properties.get("HANDED_OFF") or "").strip():
        OW.clear_property(cfg, task, "HANDED_OFF")
        ORG.invalidate_tasks_cache()
        task = SP._resolve_idea(cfg, want_id, pin)
    if task is not None and OW.ensure_idea_keyword(cfg, task.file, target):
        ORG.invalidate_tasks_cache()
        task = SP._resolve_idea(cfg, want_id, pin)
    if task is None:
        finding("minor", "project idea reconcile: idea moved", f"{want_id}; not advanced")
        return
    OW.set_state(cfg, task, target)
    events.append(("idea_reconciled", {"spec_id": spec_id, "idea_id": want_id,
                                       "from": cur, "to": target}))


def _pull_outbound(cfg, state: dict, events: list, spec_id: str, outcome: str) -> None:
    """SPEC-0297: close the outbound loop. After a merge, run `export.pull_status` so the outbox
    copy mirrors the merged portable file's status and its `EXPORTED` idea becomes `SHIPPED`.
    Best-effort like `_reconcile_closed`: a failure is a minor `bookkeeping` finding, never a
    change to the campaign state. `HANDED_OFF` waits (the base does not have the file yet); the
    SPEC-0283 sweep reaches here with `MERGED`. Only reads the target repo."""
    from .. import export as EX
    from .. import org as ORG
    from ..org_write import resolve_selector

    def finding(severity, title, detail=""):
        events.append(("finding", {"persona": "close", "severity": severity,
                                   "gate": "bookkeeping", "title": title, "detail": detail,
                                   "files": []}))

    if outcome != "MERGED":
        finding("info", "outbound pull-status waits for the merge",
                f"{spec_id}: pulled when the PR merges (`nw queue sweep-prs`)")
        return

    def idea_state():
        # the idea `pull_status` advances: `source_idea` first, `:SPEC:` scan as the fallback
        try:
            selector = EX._resolve_source_idea(cfg, spec_id)
            ORG.invalidate_tasks_cache()        # org writes can land within one mtime tick
            idea = resolve_selector(cfg, selector)
        except ValueError:
            return None, None, None
        return selector, (idea.properties.get("ID") or idea.id), idea.state

    try:
        selector, idea_id, was = idea_state()
        kw = {}
        outbox = EX._find_outbound_spec(cfg, spec_id)
        fm, _body = EX.split_frontmatter(outbox.read_text())
        if not (fm or {}).get("target_spec_path"):      # an old export: the live-config guess
            kw["to"] = (state.get("repo") or {}).get("root")
        path, status = EX.pull_status(cfg, spec_id, **kw)
        after = None
        if selector is not None:
            ORG.invalidate_tasks_cache()
            try:
                after = resolve_selector(cfg, selector).state
            except ValueError:
                after = None
        moved = selector is not None and after != was
        events.append(("outbound_pulled", {
            "spec_id": spec_id, "status": status, "outbox_path": str(path),
            "idea_id": idea_id if moved else None,
            "from": was if moved else None, "to": after if moved else None}))
        if status != "done":
            finding("minor", f"outbound pulled status is {status}, not done",
                    f"run `nw spec pull-status {spec_id}` after pulling the target repo")
        elif (was or "").upper() == "EXPORTED" and (after or "").upper() != "SHIPPED":
            finding("minor", f"outbound pull did not ship idea {idea_id}",
                    f"run `nw spec pull-status {spec_id}`")
    except Exception as e:                  # noqa: BLE001 — never block a finished merge
        finding("minor", "outbound pull-status failed", str(e) or type(e).__name__)


def _queue_split_children(cfg, state: dict, events: list) -> None:
    """SPEC-0294: when a split campaign becomes MERGED, queue its parts as one SPEC-0276 batch
    (dependency order, `skip_invalid`). The campaign is still CLOSING / HANDED_OFF on disk, so
    it is passed as `split_origin`. Best-effort like `_reconcile_closed`: a refusal is a `major`
    `bookkeeping` finding, an exception one more, and the state stays MERGED. `split_queued`
    is always emitted."""
    if state.get("kind") != "split":
        return

    def finding(title, detail=""):
        events.append(("finding", {"persona": "close", "severity": "major",
                                   "gate": "bookkeeping", "title": title, "detail": detail,
                                   "files": []}))

    ids = [p["spec_id"] for p in (state.get("split") or {}).get("parts") or [] if p.get("spec_id")]
    queued, refused = [], []
    try:
        batch = plan_add_batch(cfg, ids, project=state.get("project"),
                               priority=state.get("priority") or PRIORITY_DEFAULT,
                               split_origin=state,
                               lean_request=state.get("lean_request"),   # SPEC-0558: inherit
                               cohort=state.get("cohort"))
        apply_add_batch(cfg, batch, skip_invalid=True, by="queue")
        for it in batch["items"]:
            if it.get("written"):
                queued.append({"spec_id": it["id"], "campaign": it["plan"]["campaign"]})
            else:
                refused.append({"spec_id": it["id"], "error": it.get("error") or "not queued"})
                finding(f"split child {it['id']} not queued", it.get("error") or "")
    except Exception as e:                  # noqa: BLE001 — never block a finished merge
        err = str(e) or type(e).__name__
        try:                                # what exists, so that nothing is queued unrecorded
            queued = [{"spec_id": c.get("spec_id"), "campaign": c["id"]} for c in
                      store.list_campaigns(cfg, include_terminal=True)
                      if c.get("split_origin") == state["id"]]
            have = {q["spec_id"] for q in queued}
            refused = [{"spec_id": i, "error": err} for i in ids if i not in have]
        except Exception:                   # noqa: BLE001 — the finding below already says it failed
            queued, refused = [], []
        named = ", ".join(q["campaign"] for q in queued) or "none"
        finding("split children queue failed", f"{err}; queued before the failure: {named}")
    events.append(("split_queued", {"queued": queued, "refused": refused}))


def _mark_handed_off(cfg, state: dict, events: list, spec_id: str, finding) -> None:
    """SPEC-0280: write `:HANDED_OFF: <pr_url>` on the idea whose `:SPEC:` is `spec_id`. No
    fallback to the campaign's own idea: reconcile only advances (and so clears) ideas that
    point at the spec. A fresh `_ideas` read, because reconcile may just have written `:SPEC:`."""
    from .. import org_write
    pr_url = (state.get("close") or {}).get("pr_url") or ""
    try:
        task = SP._sole_claimant(_ideas(cfg), spec_id, project=state.get("project"), cfg=cfg)
        if task is None or task.state not in ("IDEA", "INCUBATE", "SPECCED"):
            finding("info", "no idea to mark handed off", f"no single open idea claims {spec_id}")
            return
        org_write.set_property(cfg, task, "HANDED_OFF",
                               pr_url or f"branch {(state.get('repo') or {}).get('branch')}")
        events.append(("idea_handed_off", {
            "spec_id": spec_id, "idea_id": task.properties.get("ID") or task.id,
            "pr_url": pr_url}))
    except (ValueError, OSError) as e:
        finding("minor", "idea hand-off mark failed", str(e) or type(e).__name__)


def _clear_handed_off_on_requeue(cfg, spec_id: str, campaign: str, by: str,
                                 project: str | None = None) -> None:
    """SPEC-0280: a new campaign for the spec ends the old "PR open" mark (the PR may have been
    closed unmerged). Best-effort: a failure is a ledger `note`, never a refusal."""
    from .. import org_write
    try:
        task = SP._sole_claimant(_ideas(cfg), spec_id, project=project, cfg=cfg)
        if task is not None and (task.properties.get("HANDED_OFF") or "").strip():
            org_write.clear_property(cfg, task, "HANDED_OFF")
    except Exception as e:                  # noqa: BLE001 — queueing must not depend on the idea
        try:
            store.append_event(cfg, campaign, "note", {
                "text": f"could not clear :HANDED_OFF: for {spec_id}: {e or type(e).__name__}",
                "author": "queue"}, by=by)
        except Exception:                   # noqa: BLE001 — the campaign exists; never refuse the add
            pass


def _ideas(cfg):
    """Every idea task, freshly loaded (org writes can land within one mtime tick)."""
    from .. import org as ORG
    ORG.invalidate_tasks_cache()
    return ORG.filter_tasks(ORG.load_tasks(cfg), is_idea=True)


def _dispatch_resolver(ctx: _Ctx, state: dict, res: StepResult, park: dict, events: list) -> str:
    """SPEC-0221: a close rebase conflicted on non-generated paths. With budget left (and the
    project not opted out) record `closing.resolve` and a finding, and stay CLOSING; the next
    plan is the `resolve` step. Returns `"dispatched"`, or why not: `"failed_out"` (failed
    runs reached `MAX_RESOLVE_ATTEMPTS`), `"runs_out"` (runs reached `MAX_RESOLVE_RUNS`, SPEC-0303)
    or `"no"` (opt-out, missing heads, no merge base). Anything but `"dispatched"` parks."""
    closing = dict(state.get("closing") or {"attempts": 0})
    if int((closing.get("resolve_failed") or {}).get("n") or 0) >= MAX_RESOLVE_ATTEMPTS:
        return "failed_out"
    if int(closing.get("resolve_attempts") or 0) >= MAX_RESOLVE_RUNS:
        return "runs_out" if closing.get("resolve_last_verified") else "failed_out"
    if (ctx.project(state).get("merge") or {}).get("resolver", True) is False:
        return "no"
    repo = state.get("repo") or {}
    pre_head = getattr(res.prepared, "pre_head", "") or ""
    target_sha = park.get("target_sha") or ""
    if not (pre_head and target_sha):
        return "no"
    try:
        fork = git.merge_base(repo["worktree"], pre_head, target_sha)
    except (GitError, KeyError):
        return "no"
    closing["resolve"] = {"target": park.get("target"), "target_sha": target_sha,
                          "conflicts": list(park["conflicts"]), "pre_head": pre_head,
                          "fork": fork, "branch": repo.get("branch")}
    state["closing"] = closing
    events.append(("finding", {
        "persona": "close", "severity": "info", "gate": "rebase",
        "title": "rebase conflict; resolver dispatched",
        "detail": park["detail"], "files": list(park["conflicts"])}))
    return "dispatched"


def _base_moving_park(state: dict, park: dict) -> dict:
    """SPEC-0303: the park after `MAX_RESOLVE_RUNS` resolver runs, the last one verified: nothing
    failed, the base kept moving and conflicting again, so it is `base_moving` (routine,
    SPEC-0302) and resumes to CLOSING. An earlier failed run is named in the detail."""
    closing = state.get("closing") or {}
    failed = closing.get("resolve_failed") or {}
    verified = int(closing.get("resolve_attempts") or 0) - int(failed.get("n") or 0)
    detail = f"{park.get('target') or 'the base'} moved and conflicted again after {verified} " \
             f"verified resolutions"
    if failed.get("n"):
        detail += f"; an earlier attempt failed: {failed.get('last')}"
    return {"reason": "base_moving", "detail": detail, "resume_to": "CLOSING"}


def _spent_resolver_park(state: dict, park: dict) -> dict:
    """The park detail after resolver runs were used up (a real failure, or a refusal): failed
    attempts are reported with the last failure, and a verified last run that the base overtook
    after an earlier failure says so, so the conductor knows which it was."""
    closing = state.get("closing") or {}
    used = int(closing.get("resolve_attempts") or 0)
    if not used:
        return park
    failed = closing.get("resolve_failed") or {}
    if failed.get("n") and closing.get("resolve_last_verified"):
        tail = (f" (resolver: {used} attempts; the last was verified but the base moved and "
                f"conflicted again; an earlier attempt failed: {failed.get('last')})")
    elif failed.get("n"):
        tail = f" (resolver: {used} attempts, last: {failed.get('last')})"
    else:
        return park
    return {**park, "detail": park["detail"] + tail}


def _apply_resolve(state: dict, counters: dict, res: StepResult, rr, row: dict, events: list,
                   finish):
    """SPEC-0221 harvest of a `resolve` step (not interrupted). Verified: clear
    `closing.resolve`, record `rebase_resolved` and stay CLOSING for the normal close.
    Otherwise a `major` finding; stay CLOSING with the same `resolve` while neither the failure
    budget (`MAX_RESOLVE_ATTEMPTS`) nor the run budget (`MAX_RESOLVE_RUNS`, SPEC-0303) is spent,
    else park `rebase_conflict`. A resolve step allowed only by the close allowance appends one
    info finding."""
    closing = dict(state.get("closing") or {"attempts": 0})
    rs = closing.get("resolve") or {}
    info = res.resolution or {}
    verdict = rr.verdict if isinstance(rr.verdict, dict) else {}
    closing["resolve_attempts"] = used = int(closing.get("resolve_attempts") or 0) + 1
    closing["resolve_last_verified"] = bool(info.get("verified"))
    state["closing"] = closing
    if rs.get("on_allowance"):              # SPEC-0303: the plain cap would have refused this run
        events.append(("finding", {
            "persona": "close", "severity": "info", "gate": "rebase",
            "title": "resolver ran on the close allowance",
            "detail": f"run {res.run_id}: total ${float((state.get('cost') or {}).get('total_usd') or 0):.2f}"
                      f" against cap ${float((state.get('cost') or {}).get('cap_usd') or 0):.2f}",
            "files": list(rs.get("conflicts") or [])}))
    if info.get("verified"):
        closing.pop("resolve", None)
        head = info.get("head")
        events.append(("rebase_resolved", {
            "run_id": res.run_id, "target_sha": rs.get("target_sha"),
            "conflicts": list(rs.get("conflicts") or []),
            "resolutions": list(verdict.get("resolutions") or []),
            "summary": verdict.get("summary"), "head": head,
            "extra_commits": list(info.get("extra") or []),
            "absorbed_commits": list(info.get("absorbed") or []),
            "superseded_commits": list(info.get("superseded") or []),
            "resolver_commit": info.get("commit")}))
        if info.get("superseded"):              # SPEC-0259: the drop is the resolver's call; audit it
            events.append(("finding", {
                "persona": "conflict_resolver", "severity": "major", "gate": "rebase",
                "title": "resolver dropped campaign commits",
                "detail": "\n".join(f"- {c['sha'][:10]} ({c['subject']}): {c['reason']}"
                                    for c in info["superseded"]),
                "files": info.get("superseded_files") or []}))
        state.setdefault("repo", {})["head"] = head
        return finish("CLOSING", "rebase_resolved")
    why = str(verdict.get("summary") or "").strip() or str(info.get("failed") or "not resolved")
    if verdict.get("summary") and info.get("failed"):
        why += f" [{info['failed']}]"
    questions = [str(q) for q in verdict.get("questions") or []]
    closing["resolve_failed"] = {"n": int((closing.get("resolve_failed") or {}).get("n") or 0) + 1,
                                 "last": why}
    events.append(("finding", {
        "persona": "conflict_resolver", "severity": "major", "gate": "rebase",
        "title": "conflict resolver did not resolve",
        "detail": why + ("\nQuestions:\n" + "\n".join(f"- {q}" for q in questions)
                        if questions else ""),
        "files": list(rs.get("conflicts") or [])}))
    if int(closing["resolve_failed"]["n"]) < MAX_RESOLVE_ATTEMPTS and used < MAX_RESOLVE_RUNS:
        return finish("CLOSING", "resolve_failed")
    paths = ", ".join(rs.get("conflicts") or [])
    return finish("PARKED", "rebase_conflict", park={
        "reason": "rebase_conflict", "resume_to": "CLOSING",
        "detail": f"rebase onto {rs.get('target')} ({str(rs.get('target_sha'))[:10]}) conflicts: "
                  f"{paths} (resolver: {used} attempts, last: {why})"})


def _apply_spec_write(ctx, state, counters, res, row, events, finish):
    """A `write_spec` result (SPEC-0215): problems retry once then park, `blocked` parks, a
    committed spec is recorded on the campaign and the idea, then goes to its spec review."""
    cfg, cid = ctx.cfg, state["id"]
    rr, w, persona = res.run_result, res.write, res.step.persona
    events += _record_run(state, persona, res.run_id, rr, res.budget, interrupted=res.interrupted)
    row["cost_usd"] = rr.cost_usd if rr.cost_usd is not None else res.budget
    row["cost_estimated"] = rr.cost_usd is None
    state["last_verdict"] = {"persona": persona, "run": res.run_id, "verdict": rr.verdict,
                             "verdict_ok": rr.verdict_ok}
    park_back = {"resume_to": "SPEC_WRITING"}
    if (w or {}).get("rollback_failed"):
        detail = (w or {}).get("rollback_detail") or (
            "rollback failed; clean the worktree, then resume")
        return finish("PARKED", "spec_write_failed", park={
            "reason": "spec_write_failed", "detail": detail, **park_back})
    if res.interrupted or rr.interrupted:
        # rolled back by the worker; no retry counted, the next tick runs a fresh writer
        with store.item_lock(cfg, cid):
            for etype, payload in events:
                store.append_event(cfg, cid, etype, payload)
            store.write_state(cfg, cid, state)
        row["to"], row["reason"] = res.from_state, "interrupted"
        return row
    if not isinstance(w, dict):          # SPEC-0441: no payload must park, never raise
        return finish("PARKED", "spec_write_failed", park={
            "reason": "spec_write_failed", "detail": "spec writer returned no write result",
            **park_back})
    if w["blocked"]:
        return finish("PARKED", "spec_write_failed", park={
            "reason": "spec_write_failed", "detail": "blocked: " + "; ".join(w["blocked"]),
            **park_back})
    if w.get("collision"):                                              # SPEC-0569
        # the number was taken meanwhile: re-reserve and rewrite under the new one; neither the
        # collision nor other problems of this attempt reach the writer's brief
        detail = _collision_text(w["collision"])
        counters["spec_number_retries"] = counters.get("spec_number_retries", 0) + 1
        if counters["spec_number_retries"] <= SPEC_NUMBER_RETRY_MAX:
            try:
                _rereserve_spec_number(cfg, state, w["collision"]["number"],
                                       Path((state.get("repo") or {}).get("worktree") or ".")
                                       / "docs" / "specs")
            except ValueError as e:
                detail = f"{detail}; cannot re-reserve: {e}"
            else:
                return finish("SPEC_WRITING", "spec_number_retry")
        return finish("PARKED", "spec_write_failed", park={
            "reason": "spec_write_failed", "detail": detail, **park_back})
    if w["problems"]:
        for problem in w["problems"]:
            events.append(("finding", {"persona": persona, "severity": "major",
                                       "gate": "spec-write", "title": problem[:120],
                                       "detail": problem, "files": []}))
        brief = state.get("pending_brief") or {"source": "spec_write", "fix_brief": None,
                                               "must_fix": [], "human_notes": []}
        brief["write_problems"] = list(w["problems"])
        state["pending_brief"] = brief
        counters["spec_write_retries"] = counters.get("spec_write_retries", 0) + 1
        if counters["spec_write_retries"] <= SPEC_WRITE_RETRY_MAX:
            return finish("SPEC_WRITING", "spec_write_retry")
        return finish("PARKED", "spec_write_failed", park={
            "reason": "spec_write_failed", "detail": "; ".join(w["problems"]), **park_back})
    sw = state["spec_write"]
    spec_id = SW.spec_id_of(sw["number"])
    if sw["mode"] == "split":                                          # SPEC-0290
        # the part is recorded on `state.split`; the campaign's own spec stays the original
        part = state["split"]["parts"][sw["split"]["k"] - 1]
        part.update(number=sw["number"], rel_path=sw["path"], spec_id=spec_id)
        if w["sha"]:
            events.append(("commit", {"sha": w["sha"], "message": f"spec split — {spec_id}"}))
        state.setdefault("repo", {})["head"] = w.get("head") or w["sha"]
        counters["spec_write_retries"] = counters["spec_number_retries"] = 0
        state["pending_brief"] = None
        events.append(("spec_written", {"spec_id": spec_id, "rel_path": sw["path"],
                                        "title": w["title"], "mode": "split"}))
        return finish("SPEC_WRITING", "split_part_written")
    record = {"id": spec_id, "rel_path": sw["path"], "internal": True}
    if w["sha"]:
        events.append(("commit", {"sha": w["sha"], "message": f"spec {sw['mode']} — {spec_id}"}))
    state.setdefault("repo", {})["head"] = w.get("head") or w["sha"]
    link_error = None
    with store.item_lock(cfg, cid):
        on_disk = store.read_state(cfg, cid)
        on_disk["spec"] = {**(on_disk.get("spec") or {}), **record}
        on_disk["spec_id"] = spec_id
        store.write_state(cfg, cid, on_disk)
        if sw["mode"] == "write" and on_disk.get("state") not in TERMINAL_STATES:
            link_error = _link_idea(cfg, state, spec_id)
    state["spec"] = {**(state.get("spec") or {}), **record}
    state["spec_id"] = spec_id
    counters["spec_write_retries"] = counters["spec_number_retries"] = 0
    if sw["mode"] != "amend":
        state["pending_brief"] = None
    events.append(("spec_written", {"spec_id": spec_id, "rel_path": sw["path"],
                                    "title": w["title"], "mode": sw["mode"]}))
    if sw["mode"] == "amend":                                          # SPEC-0279
        return _finish_amend(cfg, state, sw, finish)
    if link_error:
        # best-effort (SPEC-0256): the spec is committed; the link can be redone with `nw idea`
        events.append(("note", {"text": f"could not link idea to {spec_id}: {link_error}",
                                "author": "queue"}))
        if _is_request_only(state):
            link_error += f"; repair: nw backlog edit {_idea_id_of(state)} --spec {spec_id}"
        events.append(("idea_link_failed", {"spec_id": spec_id, "idea": _idea_id_of(state),
                                            "error": link_error}))
    counters["spec_review_retries"] = 0
    return finish("SPEC_REVIEW", "spec_written")


def _apply_spec_review(ctx, state, profile, counters, res, row, events, finish):
    """A `spec_review` result (SPEC-0214): a dirty worktree parks, an already committed accept
    resumes at its bookkeeping, a failed run retries once then parks, a conforming verdict is
    stored and recorded and routes to the accept (approve) or the writer (revise / split). Per
    SPEC-0230 only one review runs: a revised spec is accepted without a second review, an
    unchanged one parks `spec_review_exhausted`."""
    cfg, cid = ctx.cfg, state["id"]
    sr = res.spec_review or {}
    park_back = {"resume_to": "SPEC_REVIEW"}

    def exhausted(detail):
        return finish("PARKED", "spec_review_exhausted", park={
            "reason": "spec_review_exhausted", "detail": detail, **park_back})

    if sr.get("dirty"):
        return exhausted("commit or discard the worktree changes, then resume")
    if sr.get("already_accepted"):      # accepted before this phase: never re-baseline
        return finish("PARKED", "spec_review_exhausted", park={
            "reason": "spec_review_exhausted", "resume_to": "IMPLEMENTING",
            "detail": "the spec is already accepted; there is nothing to review"})
    if sr.get("accepted"):              # an earlier accept committed, then failed after it
        return _accept_spec(ctx, state, counters, events, finish, committed=True,
                            rnd=(state.get("spec_review") or {}).get("round"),
                            revision=_prior_revision(cfg, state))
    if sr.get("approved"):              # an approve whose accept failed: accept again, no review
        return _accept_spec(ctx, state, counters, events, finish, committed=False,
                            rnd=sr["approved"].get("round"), revision=None)
    if sr.get("after_revision"):        # SPEC-0230: the revision is accepted, no second review
        return _accept_spec(ctx, state, counters, events, finish, committed=False,
                            rnd=sr["after_revision"].get("round"), revision=sr["after_revision"])
    if sr.get("unchanged"):             # the writer cleared the brief; a plain resume needs one
        prior = sr["unchanged"]
        review = SR.load_review(cfg, (state.get("spec") or {}).get("id"), prior["spec_sha256"]) \
            or {"verdict": prior.get("verdict"), "findings": [],
                "rationale": "the reviewer asked for a revision of the spec"}
        state["pending_brief"] = _spec_review_brief(state, review)
        return finish("PARKED", "spec_review_exhausted", park={
            "reason": "spec_review_exhausted", "resume_to": "SPEC_WRITING",
            "detail": "the revision did not change the spec"})
    rr, persona = res.run_result, res.step.persona
    events += _record_run(state, persona, res.run_id, rr, res.budget, interrupted=res.interrupted)
    row["cost_usd"] = rr.cost_usd if rr.cost_usd is not None else res.budget
    row["cost_estimated"] = rr.cost_usd is None
    state["last_verdict"] = {"persona": persona, "run": res.run_id, "verdict": rr.verdict,
                             "verdict_ok": rr.verdict_ok}
    if res.interrupted or rr.interrupted:
        with store.item_lock(cfg, cid):
            for etype, payload in events:
                store.append_event(cfg, cid, etype, payload)
            store.write_state(cfg, cid, state)
        row["to"], row["reason"] = res.from_state, "interrupted"
        return row
    spec = state.get("spec") or {}
    worktree = (state.get("repo") or {}).get("worktree")
    payload = failure = None
    if sr.get("modified"):
        failure = sr["modified"]
    elif "sha" not in sr:       # SPEC-0482: an adopted run never snapshotted the spec at its start
        failure = "adopted review has no start snapshot of the spec"
    elif rr.timed_out:
        failure = "the run timed out"
    elif rr.is_error or rr.exit_code != 0:
        failure = "the run failed"
    elif not rr.verdict_ok:
        failure = "the verdict does not match the schema"
    else:
        try:
            payload = SR.review_payload(
                spec["id"], sr["sha"], model=str(persona_settings(profile, persona).get("model")),
                cost_usd=rr.cost_usd, verdict=rr.verdict, root=worktree,
                spec_path=Path(worktree) / spec["rel_path"], origin="campaign")
        except ValueError as e:
            failure = str(e)
    if failure:
        if sr.get("saved"):             # the reset dropped changes that may be a human's
            saved = ", ".join(sr["saved"])
            failure += f" (worktree changes saved as {saved})"
            events.append(("note", {"text": f"spec review reset the worktree; kept {saved}",
                                    "author": "queue"}))
        counters["spec_review_retries"] = counters.get("spec_review_retries", 0) + 1
        if counters["spec_review_retries"] <= SPEC_REVIEW_RETRY_MAX:
            return finish("SPEC_REVIEW", "spec_review_retry")
        return exhausted(f"reviewer failed: {failure}")
    SR.save_review(cfg, payload)
    rnd = 1 + len(store.read_ledger(cfg, cid, types=["spec_review"]))
    counts = {k: 0 for k in SEVERITIES}
    counts.update({k: v for k, v in SR._counts(payload["findings"]).items() if k in counts})
    events.append(("spec_review", {"verdict": payload["verdict"], "spec_sha256": sr["sha"],
                                   "round": rnd, "counts": counts, "run_id": res.run_id}))
    state["spec_review"] = {"round": rnd, "verdict": payload["verdict"], "sha": sr["sha"]}
    counters["spec_review_retries"] = 0
    if payload["verdict"] == "approve":
        return _accept_spec(ctx, state, counters, events, finish, committed=False, rnd=rnd)
    state["pending_brief"] = _spec_review_brief(state, payload)
    return finish("SPEC_WRITING", "spec_revise")    # SPEC-0230: revised, then accepted unreviewed


def _spec_review_brief(state: dict, review: dict) -> dict:
    """The writer's `pending_brief` from a revise / split review: the summary, the must_fix
    and major findings, the suggested splits of a split, and the human notes kept."""
    must_fix = []
    for f in review.get("findings") or []:
        if f.get("severity") in ("must_fix", "major"):
            title = f"[{f['severity']}] {f.get('title')}"
            if f.get("section"):
                title += f" ({f['section']})"
            must_fix.append({"title": title, "detail": PS.finding_text(f)})
    brief = {"source": "spec_reviewer", "fix_brief": review.get("rationale"),
             "must_fix": must_fix,
             "human_notes": list((state.get("pending_brief") or {}).get("human_notes") or [])}
    if review["verdict"] == "split":
        brief["suggested_splits"] = list(review.get("suggested_splits") or [])
    return brief


def _commit_accept(ctx: _Ctx, state: dict, rnd, *, after_revision: bool = False) -> str:
    """Accept steps 1-3 (SPEC-0214): set the spec `accepted`, regenerate the project's generated
    files, commit exactly those. On any failure the touched paths are restored to HEAD (file,
    index and worktree) and the error is raised."""
    worktree = Path(state["repo"]["worktree"])
    rel = state["spec"]["rel_path"]
    regen = (ctx.project(state).get("merge") or {}).get("regenerate") or []
    declared = [e["path"] for e in regen]
    snap = None
    try:
        snap = {"head_before": git.rev_parse(worktree), "path": rel,
                "untracked_before": SW.untracked(worktree)}
        SP.set_spec_status(worktree / rel, "accepted")
        problems, generated = SW.regenerate(worktree, regen, snap)
        if problems:
            raise ValueError("; ".join(problems))
        msg = f"nw({state['id']}): accept {state['spec']['id']} (round {rnd})"
        if after_revision:
            msg += " (after revision)"
        trailer = ((ctx.project(state).get("agents") or {}).get("commit_trailer") or "").strip()
        if trailer:
            msg += f"\n\n{trailer}"
        if SW.commit_staged(worktree, [rel, *generated], msg) is None:
            raise ValueError("the accept changed nothing")
    except (GitError, ValueError, OSError) as e:
        if snap is not None:            # undeclared changes and files a command created go too
            try:
                SW.rollback(worktree, snap)
            except (GitError, OSError):
                pass
        git.git(worktree, "restore", "--source=HEAD", "--staged", "--worktree", "--", rel,
                check=False)
        for path in declared:
            git.git(worktree, "restore", "--source=HEAD", "--staged", "--worktree", "--", path,
                    check=False)
        raise
    return git.rev_parse(worktree)


def _accept_spec(ctx, state, counters, events, finish, *, committed: bool, rnd, revision=None):
    """Accept the approved spec on the main thread under the campaign lock (SPEC-0214): the
    commit (skipped when `committed`: it landed earlier), then the idempotent bookkeeping. A
    human state change meanwhile skips the accept; `finish` then records `superseded_by_human`."""
    cfg, cid = ctx.cfg, state["id"]
    spec = state["spec"]

    def exhausted(detail):
        return finish("PARKED", "spec_review_exhausted", park={
            "reason": "spec_review_exhausted", "detail": detail, "resume_to": "SPEC_REVIEW"})

    with store.item_lock(cfg, cid):
        if store.read_state(cfg, cid).get("state") != "SPEC_REVIEW":
            return finish("IMPLEMENTING", "spec_accepted")      # superseded: nothing is accepted
        if not committed:
            try:
                head = _commit_accept(ctx, state, rnd, after_revision=revision is not None)
            except (GitError, ValueError, OSError) as e:
                return exhausted(f"accept failed: {getattr(e, 'tail', None) or e}")
            events.append(("commit", {"sha": head, "message": f"accept {spec['id']}"}))
            state["repo"]["head"] = head
        worktree = Path(state["repo"]["worktree"])
        rel = spec["rel_path"]
        data, read_err = _read_worktree_spec_bytes(worktree, rel)
        if read_err or data is None:
            detail = read_err or f"spec file missing at {worktree / rel}"
            return exhausted(f"accept failed after commit: {detail}")
        cdir = store.campaign_dir(cfg, cid)
        snap_err = _replace_spec_at_open_snapshot(cdir, cdir / CL.SPEC_AT_OPEN, data, spec)
        if snap_err:
            return exhausted(f"accept failed after commit: {snap_err}")
        events.append(("spec_accepted", {"spec_id": spec["id"], "round": rnd}))
        state["pending_brief"] = (_revision_pending_brief(cfg, state, revision)
                                  if revision is not None else None)
        return finish("IMPLEMENTING", "spec_accepted")


def _finish_amend(cfg, state: dict, sw: dict, finish):
    """A committed amend (SPEC-0279): the amended text becomes the accepted text (close's drift
    check, SPEC-0186), the campaign is admitted after its one revision and goes straight to
    IMPLEMENTING with the review findings as the implementer's brief."""
    worktree = Path(state["repo"]["worktree"])
    spec = state["spec"]
    rel = sw["path"]
    data, read_err = _read_worktree_spec_bytes(worktree, rel)
    if read_err or data is None:
        detail = read_err or f"spec file missing at {worktree / rel}"
        return finish("PARKED", "spec_write_failed", park={
            "reason": "spec_write_failed", "detail": detail, "resume_to": "SPEC_WRITING"})
    cdir = store.campaign_dir(cfg, state["id"])
    snap_err = _replace_spec_at_open_snapshot(cdir, cdir / CL.SPEC_AT_OPEN, data, spec)
    if snap_err:
        return finish("PARKED", "spec_write_failed", park={
            "reason": "spec_write_failed", "detail": snap_err, "resume_to": "SPEC_WRITING"})
    state["admission"] = "after_revision"       # a resume to SPEC_WRITING never amends twice
    rev = state.get("revision") or {}
    state["pending_brief"] = SR.revision_brief({"spec_sha256": rev.get("review_sha"),
                                                "findings": rev.get("findings") or []})
    return finish("IMPLEMENTING", "spec_amended")


def _link_idea(cfg, state: dict, spec_id: str) -> str | None:
    """Give the campaign's idea `:SPEC:` and SPECCED; cite the public file on write (SPEC-0535).

    Best-effort (SPEC-0256): returns the error text when the ideas file drifted, lacks SPECCED
    or cannot be read, else None; never raises."""
    from .. import idea_link as IL
    from .. import org_write as OW

    def _set_spec_specced(task):
        if (task.properties.get("SPEC") or "").strip() != spec_id:
            OW.set_property(cfg, task, "SPEC", spec_id)
            task = _idea_task(cfg, _idea_id_of(state))
        if task is not None and task.state != "SPECCED":
            OW.set_state(cfg, task, "SPECCED")

    sw = state.get("spec_write") or {}
    if _is_request_only(state):                         # SPEC-0541: the public file only
        return _cite_request(cfg, state, spec_id) if sw.get("mode") == "write" else None
    if sw.get("mode") != "write":
        try:
            task = _idea_task(cfg, _idea_id_of(state))
            if task is None:
                return None
            _set_spec_specced(task)
        except (ValueError, OSError) as e:
            return str(e) or type(e).__name__
        return None

    try:
        with OW.org_write_lock(cfg):
            task = _idea_task(cfg, _idea_id_of(state))
            if task is None:
                return None
            req = (task.properties.get("REQ") or "").strip()
            if not req:
                _set_spec_specced(task)
                return None
            agreed = IL.load_agreed_request(cfg, task)
            if agreed is None:
                _set_spec_specced(task)
                return None
            if agreed.spec and agreed.spec != spec_id:
                return f"{agreed.req_id} already cites {agreed.spec}"
            worktree = Path((state.get("repo") or {}).get("worktree") or ".")
            citing = IL.will_cite(cfg, task, worktree / "docs" / "specs", agreed=agreed)
            cite_sha = cite_old = None
            if agreed.spec != spec_id and citing:
                agreed = IL.agreed_after_backlog_move(cfg, task, agreed, spec_id)
                cite_sha, cite_old = IL.cite_spec(
                    cfg, agreed, spec_id, expected_backlog_tip=agreed.backlog_tip)
            org_snapshot = Path(task.file).read_bytes()
            property_written = False
            try:
                if (task.properties.get("SPEC") or "").strip() != spec_id:
                    OW.set_property(cfg, task, "SPEC", spec_id)
                    property_written = True
                    task = _idea_task(cfg, _idea_id_of(state))
                if task is not None and task.state != "SPECCED":
                    OW.set_state(cfg, task, "SPECCED")
            except (ValueError, OSError) as org_err:
                drift = isinstance(org_err, ValueError) and str(org_err).startswith("drift:")
                if cite_sha:
                    try:
                        IL.restore_cite_backlog_tip_or_bail(
                            agreed.repo, spec_id=spec_id, req_id=agreed.req_id,
                            new_sha=cite_sha, old_tip=cite_old)
                    except ValueError as restore_err:
                        if (
                            "restored refs/heads/nw-backlog" in str(restore_err)
                            and property_written
                            and not drift
                        ):
                            OW._atomic_replace_bytes(task.file, org_snapshot)
                        return str(restore_err)
                if property_written and not drift:
                    OW._atomic_replace_bytes(task.file, org_snapshot)
                return str(org_err) or type(org_err).__name__
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        return str(e) or type(e).__name__
    return None


def _cite_request(cfg, state: dict, spec_id: str) -> str | None:
    """SPEC-0541: write `spec:` on the public request of a `request_only` campaign with one
    compare-and-swap commit. Returns the error text, or None on success and on a repeated call
    (the file already cites `spec_id`). Never raises; touches no org file."""
    from .. import idea_link as IL
    idea = state.get("idea") or {}
    req_id = idea.get("source_request") or idea.get("id")
    try:
        agreed, _gate = _request_source(cfg, req_id, state.get("project") or "",
                                        allow_spec=spec_id)
        if agreed.spec == spec_id:
            return None
        IL.preflight_cite(agreed.repo)
        IL.cite_spec(cfg, agreed, spec_id, expected_backlog_tip=agreed.backlog_tip)
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        return str(e) or type(e).__name__
    return None


def _absorb_close_events(state: dict, counters: dict, events: list, close_events) -> None:
    """Fold a close's ledger events into the apply: gate runs counted, head tracked."""
    for etype, payload in close_events:
        if etype == "park":
            continue                                   # re-emitted by _park
        events.append((etype, payload))
        if etype == "gate":
            counters["gate_runs"] += 1
            state["last_gate"] = payload
        if etype == "commit" and payload.get("sha"):
            state.setdefault("repo", {})["head"] = payload["sha"]


def _synthetic_failure(error: str) -> RunResult:
    """The RunResult recorded for a reviewer whose worker raised after `run.start`."""
    return RunResult(exit_code=-1, timed_out=False, interrupted=False, duration_s=0.0,
                     is_error=True, cost_usd=None, usage={}, session_id=None, verdict=None,
                     verdict_ok=False, result_text=error, raw_path=None)


def _store_verdicts(state: dict, profile: dict, counters: dict, results, enf=None) -> bool:
    """Merge this pass's valid verdicts into `last_review.verdicts` (overwriting a retried
    reviewer's entry) and refresh `run` / `runs` / `loop`. True when any verdict was stored.
    SPEC-0554: `enf` (a delta-mode packet's `packet.enforcement`) makes the stored findings
    the reviewer's still-open prior findings plus its new ones, unsupported ones marked
    `demoted`; the verdict's `prior_status` is kept."""
    lr = state.get("last_review") if isinstance(state.get("last_review"), dict) else {}
    verdicts = dict(lr.get("verdicts") or {})
    stored = False
    for r in results:
        rr = r.run_result
        if rr is None or not rr.verdict_ok or not isinstance(rr.verdict, dict) \
                or r.interrupted or rr.interrupted:
            continue
        v = rr.verdict
        entry = {"verdict": v.get("verdict"), "score": v.get("score"),
                 "fix_brief": v.get("fix_brief"),
                 "findings": [f for f in v.get("findings") or [] if isinstance(f, dict)]}
        if "ac_assessment" in v:
            entry["ac_assessment"] = v["ac_assessment"]
        if "coverage_assessment" in v:
            entry["coverage_assessment"] = v["coverage_assessment"]
        if enf is not None:
            entry["findings"], entry["demoted"] = PK.enforce(
                state.get("spec_id"), r.step.persona, v, enf)
            entry["prior_status"] = [x for x in v.get("prior_status") or []
                                     if isinstance(x, dict)]
        elif v.get("prior_status") and PK.on(state):        # loop 1: kept for the record
            entry["prior_status"] = [x for x in v["prior_status"] if isinstance(x, dict)]
        entry.update(run=r.run_id, loop=counters["loops"])
        verdicts[r.step.persona] = entry
        stored = True
    if stored:
        order = [p for p in reviewers_of(profile) if p in verdicts]
        lr.update(verdicts=verdicts, runs={p: verdicts[p]["run"] for p in order},
                  run=verdicts[order[-1]]["run"], loop=counters["loops"])
        state["last_review"] = lr
    return stored


def _accept_waivers(state: dict, profile: dict, items) -> tuple:
    """`(accepted, ignored)` for the triager's `waive` list (SPEC-0206): accepted = waived
    findings `{id, personas, severity, title, detail, files, ac, category, reason}`; ignored =
    `[(id, why)]`. Only a surviving `minor`/`nit` finding with a non-empty reason, once."""
    hw, at = human_overrides_of(state)
    tri = triage_reviews(_scoped_verdicts(state, profile), profile, human_waived=hw, attested=at)
    found = {f["id"]: f for f in tri["blocking"] + tri["advisory"]}
    accepted, ignored, seen = [], [], set()
    for item in items:
        fid = item.get("id") if isinstance(item, dict) else None
        reason = item.get("reason") if isinstance(item, dict) else None
        if not isinstance(fid, str) or fid not in found:
            ignored.append((fid, "not a finding of this review"))
        elif fid in tri["synthetic"]:
            ignored.append((fid, "synthetic findings cannot be waived"))
        elif found[fid]["severity"] not in ("minor", "nit"):
            ignored.append((fid, f"{found[fid]['severity']} findings cannot be waived"))
        elif not isinstance(reason, str) or not reason.strip():
            ignored.append((fid, "empty reason"))
        elif fid in seen:
            ignored.append((fid, "duplicate"))
        else:
            seen.add(fid)
            accepted.append(dict(found[fid], reason=reason.strip()))
    return accepted, ignored


def _gate_triage_event(cfg, cid: str, state: dict, run_id, verdict, decision) -> dict:
    """SPEC-0408: the `triage` ledger event of an applied gate-triager verdict. `waive[].key` is
    the key of the one open `finding` of that gate in the current gate round (after the last
    `gate` event), `None` when there is none or several (never guessed)."""
    v = verdict if isinstance(verdict, dict) else {}
    spec_id = state.get("spec_id")
    ledger = store.read_ledger(cfg, cid, types=["finding", "gate"])
    last_gate = max((e.get("seq", 0) for e in ledger if e.get("type") == "gate"), default=-1)
    round_findings = [e for e in ledger if e.get("type") == "finding"
                      and e.get("seq", 0) > last_gate]
    waive = []
    for item in v.get("waived") or []:
        if not isinstance(item, dict):
            continue
        gate = item.get("gate")
        keys = {e.get("key") or FU.finding_key(spec_id, e.get("title"), e.get("files"))
                for e in round_findings if gate and e.get("gate") == gate}
        waive.append({"gate": gate, "key": next(iter(keys)) if len(keys) == 1 else None,
                      "reason": item.get("reason")})
    return {"persona": "gate_triager", "run_id": run_id, "kind": "gate",
            "disposition": v.get("disposition"), "routed_to": decision.kind,
            "confidence": v.get("confidence"), "suspected_flaky": v.get("suspected_flaky"),
            "waive": waive,
            "fix_keys": [FU.finding_key(spec_id, i.get("title"), i.get("files"))
                         for i in v.get("must_fix") or [] if isinstance(i, dict)]}


def _apply_triager(ctx, state, profile, counters, res, row, events):
    """Record the review triager's run and accept its waivers (SPEC-0206). The result never
    enters `last_review.verdicts` nor the reviewers' retry logic: a failed, malformed or
    timed-out run only adds a note, and `triage` becomes `done` so it runs once per loop.
    Returns the report row when the run was interrupted (state unchanged: `triage` stays
    `pending`), else None — the caller then routes the review with the accepted waivers."""
    cfg, cid = ctx.cfg, state["id"]
    r = res.reviews[0]
    events += r.events_before
    rr = r.run_result
    lr = state["last_review"]
    if rr is not None:
        events += _record_run(state, "review_triager", r.run_id, rr, r.budget,
                              interrupted=r.interrupted)
        row["run_id"] = r.run_id
        row["cost_usd"] = rr.cost_usd if rr.cost_usd is not None else r.budget
        row["cost_estimated"] = rr.cost_usd is None
        if r.interrupted or rr.interrupted:
            with store.item_lock(cfg, cid):
                for etype, payload in events:
                    store.append_event(cfg, cid, etype, payload)
                store.write_state(cfg, cid, state)
            row["to"], row["reason"] = res.from_state, "interrupted"
            return row
    verdict = rr.verdict if rr is not None and rr.verdict_ok else None
    if not isinstance(verdict, dict) or not isinstance(verdict.get("waive"), list):
        why = (f"review triager run not started: {r.error}" if rr is None else
               "review triager timed out" if rr.timed_out else
               "review triager run failed" if rr.is_error else
               "review triager returned a malformed verdict")
        events.append(("note", {"text": f"{why}; the deterministic review outcome stands",
                                "author": "queue"}))
        accepted = []
    else:
        accepted, ignored = _accept_waivers(state, profile, verdict["waive"])
        events.append(("triage", {      # SPEC-0408: the applied verdict, with stable finding keys
            "persona": "review_triager", "run_id": r.run_id, "kind": "review",
            "disposition": "waive" if accepted else "none",
            "waive": [{"id": w["id"],
                       "key": FU.finding_key(state.get("spec_id"), w.get("title"), w.get("files")),
                       "severity": w.get("severity"), "title": w.get("title"),
                       "reason": w["reason"]} for w in accepted],
            "ignored": [{"id": fid, "why": why} for fid, why in ignored]}))
        for fid, why in ignored:
            events.append(("note", {"text": f"triager ignored {fid}: {why}", "author": "queue"}))
        for w in accepted:
            events.append(("note", {"text": f"waived {w['id']}: {w['reason']}",
                                    "author": "queue"}))
    lr.update(triage="done", waived=accepted, triager_run=r.run_id)
    return None


def _carry_source(state: dict, tri: dict) -> dict:
    """The `{blocking, advisory, waived}` of a review, for `_merge_carry`."""
    return {"blocking": tri["blocking"], "advisory": tri["advisory"],
            "waived": (state.get("last_review") or {}).get("waived") or []}


def _finish_fix(ctx, state, events, finish, kind, reason):
    """The fix outcome of a review or a gate triage (`loops` already counted, `pending_brief`
    set): back to IMPLEMENTING, or into DIVERGENCE_TRIAGE when the fix rounds show no
    convergence (SPEC-0247 detects, SPEC-0462 triages). The brief stays, so an extra round
    continues with it. A campaign already parked `diverging` is left where it is."""
    div = DV.check(ctx.cfg, state, events, kind)
    if div is None:
        return finish("IMPLEMENTING", reason)
    events.append(("loop_divergence", div))
    state["divergence"] = div
    counters = _counters(state)
    counters["divergence_retries"] = 0
    return finish("DIVERGENCE_TRIAGE", "diverging")


def _apply_divergence(ctx, state, counters, res, rr, events, finish):
    """One divergence-triager verdict (SPEC-0462)."""
    action, detail = decide_divergence(state, rr.verdict, rr.verdict_ok)
    if action == "retry":
        counters["divergence_retries"] = int(counters.get("divergence_retries") or 0) + 1
        return finish("DIVERGENCE_TRIAGE", "divergence_retry")
    verdict = rr.verdict if isinstance(rr.verdict, dict) else {}
    if rr.verdict_ok:
        events.append(("triage", {"persona": res.step.persona, "run_id": res.run_id,
                                  "kind": "divergence", "disposition": verdict.get("pick") or action,
                                  "waive": []}))
    events.append(("note", {"text": f"divergence triage {action}: {detail}"[:500],
                            "author": "queue"}))
    counters["divergence_retries"] = 0
    if action == "extra_round":
        counters["divergence_extras"] = int(counters.get("divergence_extras") or 0) + 1
        return finish("IMPLEMENTING", "divergence_extra")
    if action == "rework":
        return finish("PARKED", "diverging_rework", park={
            "reason": "diverging_rework", "detail": detail, "resume_to": "SPEC_WRITING"})
    row = finish("PARKED", "human_decision", park={
        "reason": "human_decision", "detail": detail, "resume_to": "IMPLEMENTING",
        "escalated_from": "diverging"})
    _file_divergence_escalation(ctx.cfg, state["id"], detail, verdict or None)
    return row


def _file_divergence_escalation(cfg, cid, detail, verdict) -> None:
    """A human-only escalation. Topic `scope` does not time out. No recommendation."""
    from . import escalations as ESC
    items = []
    if isinstance(verdict, dict):
        for it in verdict.get("items") or []:
            if isinstance(it, dict) and it.get("title"):
                items.append(f"{it.get('title')}: {it.get('label')}")
    question = (detail or "The fix loop diverged.").strip()[:500]
    try:
        plan = ESC.plan_escalate(
            cfg, cid, frm="conductor", topic="scope", question=question,
            options=("one more implement round", "rework the spec", "stop this campaign"),
            facts=[question, *items][:12])
        ESC.apply_escalate(cfg, plan)
    except (ValueError, OSError) as e:
        store.append_event(cfg, cid, "note", {
            "text": f"divergence escalation was not filed: {e}", "author": "queue"})


def _merge_carry(state: dict, review: dict) -> None:
    """SPEC-0207: on a `fix` outcome merge the review's `advisory` and `waived` findings into
    `state["review_carry"]` (de-duplicated by key, so re-merging the same review adds nothing)
    and drop every key that is blocking in this review (the implementer is told to fix it).
    `review` = `{blocking, advisory, waived}` of the triage."""
    spec_id = state.get("spec_id")
    blocking = {FU.finding_key(spec_id, f.get("title"), f.get("files"))
                for f in review.get("blocking") or []}
    carry = [e for e in state.get("review_carry") or [] if e["key"] not in blocking]
    for e in FU.considered(spec_id, [], review):
        if e["key"] not in blocking:
            FU.merge_entry(carry, e)
    state["review_carry"] = carry


def _apply_reviews(ctx, state, profile, counters, res, row, events, finish):
    """Route the reviewers' results (SPEC-0187, SPEC-0201): record every run, store the valid
    verdicts in `last_review`, then follow `decide_reviews`. `res.reviews` is in profile
    order; `events` is `_apply`'s list, `finish` its closure. The review triager's result
    (SPEC-0206) is handled by `_apply_triager` and then routed the same way."""
    cfg, cid = ctx.cfg, state["id"]
    reviewers = reviewers_of(profile)
    if res.step.persona == PK.MARSHAL:                                # SPEC-0554
        return _apply_marshal(ctx, state, counters, res, row, events, finish)
    if res.step.persona == CO.NAME:                                   # SPEC-0555
        return _apply_collator(ctx, state, profile, counters, res, row, events, finish)
    if res.step.persona == "review_triager":
        out = _apply_triager(ctx, state, profile, counters, res, row, events)
        return out if out is not None else _route_reviews(
            ctx, state, profile, counters, row, events, finish)
    multi = len(reviewers) > 1
    charged, last = 0.0, None
    for r in res.reviews:
        events += r.events_before
        rr = r.run_result
        if rr is None:                      # prepared nothing: no run.start, no cost
            events.append(("note", {"text": f"{r.step.persona}: review run not started: "
                                            f"{r.error}", "author": "queue"}))
            continue
        events += _record_run(state, r.step.persona, r.run_id, rr, r.budget,
                              interrupted=r.interrupted)
        charged += rr.cost_usd if rr.cost_usd is not None else r.budget
        row["cost_estimated"] = bool(row.get("cost_estimated")) or rr.cost_usd is None
        last = r
    if last is not None:
        row["run_id"], row["cost_usd"] = last.run_id, charged
        state["last_verdict"] = {"persona": last.step.persona, "run": last.run_id,
                                 "verdict": last.run_result.verdict,
                                 "verdict_ok": last.run_result.verdict_ok}
    bad = next((r for r in res.reviews if r.config_error), None)
    interrupted = any(r.interrupted or (r.run_result and r.run_result.interrupted)
                      for r in res.reviews) or (multi and ctx.stop.is_set())
    if interrupted:
        # No decision, no retry counted: the next tick re-spawns the reviewers still lacking a
        # valid verdict (one reviewer: state unchanged, as SPEC-0187).
        if multi:
            _store_verdicts(state, profile, counters, res.reviews,
                            PK.enforcement(cfg, state) if PK.on(state) else None)
        with store.item_lock(cfg, cid):
            for etype, payload in events:
                store.append_event(cfg, cid, etype, payload)
            store.write_state(cfg, cid, state)
        row["to"], row["reason"] = res.from_state, "interrupted"
        return row
    if bad is not None:
        return finish("PARKED", "config_invalid", park={
            "reason": "config_invalid", "detail": f"project config: {bad.error}",
            "resume_to": "REVIEWING"})
    _store_verdicts(state, profile, counters, res.reviews,
                    PK.enforcement(cfg, state) if PK.on(state) else None)       # SPEC-0554
    _note_deep_close(state, profile, res, events)                      # SPEC-0553
    if res.step.held:
        # SPEC-0345: the persona cap held part of the fan-out; no routing until every reviewer
        # ran. A dispatched member without a valid verdict (its run failed, or it never started)
        # uses one retry, as in a full pass, and one already out of retries parks
        # review_escalated, as `decide_reviews` would.
        valid = _valid_verdicts(state, profile)
        failed = [r.step.persona for r in res.reviews if r.step.persona not in valid]
        out = tuple(p for p in failed
                    if _retries_used(counters, p, len(reviewers)) >= REVIEW_RETRY_MAX)
        if out:
            who = "the conformance reviewer" if out == ("conformance_reviewer",) \
                else ", ".join(out)
            return finish("PARKED", "review_escalated", park={
                "reason": "review_escalated",
                "detail": f"{who} returned no usable verdict twice",
                "resume_to": "REVIEWING"})
        by = counters.setdefault("review_retries_by_persona", {})
        for p in failed:
            by[p] = by.get(p, 0) + 1
            counters["review_retries"] = int(counters.get("review_retries", 0)) + 1
        events.append(("note", {"text": f"persona cap: {', '.join(res.step.held)} "
                                        "wait for a free persona slot", "author": "queue"}))
        return finish("REVIEWING", "persona_cap")
    if _usage_holds_review(res.reviews, state, profile):
        try:
            PG.note_usage(cfg)
        except (OSError, ValueError):
            pass
        return finish("REVIEWING", "usage_paused")
    return _route_reviews(ctx, state, profile, counters, row, events, finish)


def _deep_close_due(state: dict, profile: dict) -> bool:
    """SPEC-0553: a hand-raised campaign (`raised`, `lean.tiers` pinned) gets one fresh
    deep-tier adversarial pass before it closes; once per campaign, and only when the
    profile's reviewers include the adversarial reviewer."""
    return (TR.tiers_on(state) and state.get("raised") is True
            and not state.get("deep_close")
            and TR.DEEP_PERSONA in reviewers_of(profile))


def _start_deep_close(state: dict, counters: dict, events: list, finish):
    """The review passed: drop the adversarial verdict of this loop so the next review step
    re-runs that reviewer alone, at tier deep (`tiers.persona_tier`). Its verdict then routes
    like any review: a fix is a normal review fix (it counts against `loop_max`)."""
    lr = state.get("last_review") if isinstance(state.get("last_review"), dict) else {}
    verdicts = dict(lr.get("verdicts") or {})
    verdicts.pop(TR.DEEP_PERSONA, None)
    lr["verdicts"] = verdicts
    lr["triage"] = None                 # the new verdict set may need the triager again
    state["last_review"] = lr
    loop = int(counters.get("loops") or 0)
    state["deep_close"] = {"status": "pending", "loop": loop}
    events.append(("deep_close", {"status": "pending", "loop": loop,
                                  "persona": TR.DEEP_PERSONA, "tier": "deep"}))
    return finish("REVIEWING", "deep_close_pass")


def _note_deep_close(state: dict, profile: dict, res, events: list) -> None:
    """Mark the deep close pass done once its adversarial run left a valid verdict."""
    dc = state.get("deep_close")
    if not (isinstance(dc, dict) and dc.get("status") == "pending"):
        return
    if TR.DEEP_PERSONA not in _valid_verdicts(state, profile):
        return
    run = next((r.run_id for r in res.reviews if r.step.persona == TR.DEEP_PERSONA), None)
    dc.update(status="done", run=run)
    verdict = ((state.get("last_review") or {}).get("verdicts") or {}).get(TR.DEEP_PERSONA) or {}
    events.append(("deep_close", {"status": "done", "loop": dc.get("loop"), "run": run,
                                  "verdict": verdict.get("verdict")}))


def _route_reviews(ctx, state, profile, counters, row, events, finish):
    """Follow `decide_reviews` on the stored verdicts and waivers (SPEC-0203, SPEC-0206): retry
    missing reviewers, ask the triager (`triage` pending) or skip it on the cap, then pass, park
    or send the implementer back."""
    reviewers = reviewers_of(profile)
    multi = len(reviewers) > 1
    valid = _valid_verdicts(state, profile)
    if CO.on(state) and len(valid) == len(reviewers):                  # SPEC-0555
        started = _start_collation(ctx, state, profile, valid, events, finish)
        if started is not None:
            return started
        valid = _apply_collation(state, valid)
    lr = state.get("last_review") or {}
    decision = decide_reviews(state, profile, valid, [p for p in reviewers if p not in valid],
                              [w["id"] for w in lr.get("waived") or [] if isinstance(w, dict)],
                              *human_overrides_of(state))
    if decision.kind == "none":                          # missing verdicts, retry allowed
        by = counters.setdefault("review_retries_by_persona", {})
        for p in decision.missing:
            by[p] = by.get(p, 0) + 1
        counters["review_retries"] += 1
        return finish("REVIEWING", "review_retry")
    if decision.combined is not None:
        c = decision.combined
        tri = c["triage"]
        lr = state["last_review"]
        if lr.get("triage") is None and needs_triager(tri, profile):
            if _cap_blocks(state, profile, ["review_triager"])[0] is None:
                lr["triage"] = "pending"
                return finish("REVIEWING", "review_triage")
            lr["triage"] = "skipped"
            events.append(("note", {"text": "triager skipped: cost cap", "author": "queue"}))
        surviving = tri["blocking"] + tri["advisory"]
        lr.update(
            verdict=tri["outcome"], score=tri["score"], findings=len(surviving),
            must_fix=sum(1 for f in surviving if f["severity"] == "must_fix"),
            waived=lr.get("waived") or [], advisory=tri["advisory"], loop=counters["loops"])
        events.append(("review_outcome", {                  # SPEC-0422: the score history
            "loop": counters["loops"], "score": tri["score"], "pass_score": c["pass_score"],
            "outcome": tri["outcome"],
            "major": sum(1 for f in surviving if f["severity"] == "major"),
            "must_fix": sum(1 for f in surviving if f["severity"] == "must_fix"),
            **({"demoted": len(tri.get("basis_demoted") or [])} if PK.on(state) else {}),
            **({"out_of_scope": len(tri.get("out_of_scope") or [])} if CO.on(state) else {})}))
        events.append(("note", {"text": f"review score {tri['score']}/{c['pass_score']} "
                                        f"{tri['outcome']}", "author": "queue"}))
        if tri["human_waived"] or tri["attested"]:
            events.append(("note", {"text": f"human override: waived {len(tri['human_waived'])}, "
                                            f"attested {len(tri['attested'])}",
                                    "author": "queue"}))
        if tri["outcome"] == "pass":
            for f in tri["advisory"]:
                if f["severity"] == "major":
                    events.append(("note", {"text": f"pass over major: {f['id']} {f['title']}",
                                            "author": "queue"}))
    if decision.kind == "park":
        if decision.reason == "loop_exhausted" and decision.combined is not None:
            _merge_carry(state, _carry_source(state, decision.combined["triage"]))
            titles = (decision.combined.get("brief") or {}).get("must_fix") or []
            if _cursor_campaign(ctx, state) and review_rounds_spent(counters, profile):
                return finish("PARKED", "round_budget",
                              park=_round_budget_park(profile, counters))
            if _maybe_loop_auto_resume(state, counters, titles):
                brief = decision.combined["brief"]
                notes = ((state.get("pending_brief") or {}).get("human_notes") or [])
                state["pending_brief"] = {
                    "source": "review" if multi else reviewers[0],
                    "fix_brief": brief["fix_brief"], "must_fix": brief["must_fix"],
                    "human_notes": list(notes)}
                if multi:
                    state["pending_brief"]["personas"] = brief["personas"]
                if _cursor_campaign(ctx, state):
                    counters["review_rounds"] = int(counters.get("review_rounds") or 0) + 1
                events.append(("note", {"text": "loop_exhausted auto-resume: the must-fix "
                                                "set shrank", "author": "queue"}))
                return _finish_fix(ctx, state, events, finish, "review", "loop_auto_resume")
        return finish("PARKED", decision.reason, park={
            "reason": decision.reason, "detail": decision.detail,
            "resume_to": decision.resume_to})
    counters["review_retries"] = 0
    counters.pop("review_retries_by_persona", None)
    if decision.kind == "close":
        if _deep_close_due(state, profile):                           # SPEC-0553
            return _start_deep_close(state, counters, events, finish)
        return finish("CLOSING", "review_pass")
    # fix
    if _cursor_campaign(ctx, state) and review_rounds_spent(counters, profile):
        return finish("PARKED", "round_budget", park=_round_budget_park(profile, counters))
    brief_titles = must_fix_titles((decision.combined["brief"] or {}).get("must_fix"))
    if int(counters.get("loops") or 0) == 0:
        note_must_fix_baseline(counters, brief_titles)
    if _cursor_campaign(ctx, state):
        counters["review_rounds"] = int(counters.get("review_rounds") or 0) + 1
    counters["loops"] += 1
    counters["gate_retries"] = 0
    counters["timeout_regates"] = 0
    counters["triage_retries"] = 0
    _merge_carry(state, _carry_source(state, decision.combined["triage"]))
    notes = ((state.get("pending_brief") or {}).get("human_notes") or [])
    brief = decision.combined["brief"]
    state["pending_brief"] = {
        "source": "review" if multi else reviewers[0], "fix_brief": brief["fix_brief"],
        "must_fix": brief["must_fix"], "human_notes": list(notes)}
    if multi:
        state["pending_brief"]["personas"] = brief["personas"]
    return _finish_fix(ctx, state, events, finish, "review", "review_fix")


# ---- tick / run ------------------------------------------------------------------------------

@dataclass
class TickReport:
    steps: list = field(default_factory=list)
    admitted: int = 0
    idle: bool = True
    errors: list = field(default_factory=list)
    started: list = field(default_factory=list)     # SPEC-0213: steps submitted this tick
    in_flight: list = field(default_factory=list)   # campaigns still running at return
    drain: bool = False                             # the shutdown / re-exec drain report
    detached: list = field(default_factory=list)    # SPEC-0225: runs left running at the exec
    abandoned: list = field(default_factory=list)   # SPEC-0229: spawning / collect tasks left
    admission_paused: str | None = None             # SPEC-0253: why this tick admitted nothing
    blocked: dict | None = None                     # SPEC-0268: ready items waiting on full slots

    def payload(self) -> dict:
        out = {"schema": "nw.queue.tick.v1", "steps": self.steps, "admitted": self.admitted,
               "idle": self.idle, "errors": self.errors, "started": self.started,
               "in_flight": self.in_flight, "drain": self.drain}
        if self.admission_paused:
            out["admission_paused"] = self.admission_paused
        return out


def slots_for(cfg, profile_name=None) -> int:
    try:
        return int(resolve_profile(cfg, profile_name).get("slots") or 1)
    except ValueError:
        return 1


@dataclass
class _Flight:
    """One campaign's in-flight agent step (SPEC-0213): one task for implement / triage /
    gate, one task per reviewer (and the `_ReviewGroup`) for a fan-out. Keeps the `_Ctx` it
    was planned with; its state dict belongs to the workers until it is harvested."""
    state: dict
    ctx: "_Ctx"
    tasks: list = field(default_factory=list)       # [(Step, Future, RunHooks | None)]
    group: object = None
    started_at: str = field(default_factory=lambda: store.now_iso())
    runs: list = field(default_factory=list)        # [(run_id, timeout_s)] of its persona runs

    def done(self) -> bool:
        return all(f.done() for _, f, _h in self.tasks)


class StepPool:
    """The persistent executor and the per-campaign flights (SPEC-0213). `run()` owns one for
    its whole life; `tick()` without one makes a throwaway pool and waits for it. Threads are
    created on demand, so the high cap never runs more than slots × reviewers at once."""

    MAX_WORKERS = 64

    def __init__(self):
        self._pool: ThreadPoolExecutor | None = None
        self.flights: dict = {}                    # campaign id -> _Flight, submission order
        self.lock = threading.RLock()              # SPEC-0225: task phases and detaching
        self.adopted = False                       # the first tick adopts open runs
        self._seen: list = []                      # SPEC-0229: worker threads of ended executors
        self.forced: dict = {}                     # SPEC-0229: campaign -> run ids force-detached
        self.detach_all = False                    # SPEC-0229: set by the stop / crash sweep
        self.deferred_noted: set = set()           # SPEC-0311: exclusive re-gates already journaled
        self.sentinel = None                       # SPEC-0359: (project, future) in flight

    def hooks(self, run_dir=None, phase: str = "spawning") -> RunHooks:
        return RunHooks(self.lock, run_dir, phase, self)

    def try_detach_all(self) -> list | None:
        """Atomically detach every remaining persona run, so the loop can exec. None when any
        unfinished task is in-process (gate / close / FakeRunner) or in `spawning` /
        `collect`; otherwise `pool.flights` is empty afterwards and the result lists
        `{campaign, run_ids}` of the runs left running. SPEC-0341: a gate step attached to an
        async remote job counts as detachable too; it is told to detach and its job record
        stays live for the next runner to adopt."""
        from . import remote_gates as RG

        def remote(cid, step) -> bool:
            return step.kind == "gate" and RG.attached(cid)
        with self.lock:
            claim = set()
            for cid, fl in self.flights.items():
                if fl.done():                   # finished, not harvested yet: harvest, never drop
                    return None
                for step, fut, hk in fl.tasks:
                    if fut.done() or (hk is not None and hk.phase == "wait"):
                        continue
                    if hk is None and remote(cid, step):
                        claim.add(cid)
                        continue
                    return None
            # the claim is what counts, and it is all or none: a step that left its attach loop
            # since (to give up and fall back locally) is not claimed, nor is any other, and the
            # exec waits. Claimed steps return detached runs, never applied.
            if not RG.detach_all_attached(sorted(claim)):
                return None
            out = []
            for cid in list(self.flights):
                fl = self.flights.pop(cid)
                for step, fut, hk in fl.tasks:
                    if not fut.done() and hk is not None:
                        hk.detach.set()
                out.append({"campaign": cid, "run_ids": [r for r, _ in fl.runs]})
            return out

    def force_detach_waiting(self) -> list:
        """Stop / crash sweep (SPEC-0229): under the lock set `detach_all` and the detach flag of
        every `wait` task, so those threads return without collecting; the runs stay adoptable
        (recorded in `forced`, as `detached_view`). Returns the unfinished `spawning` /
        `collect` tasks as `{campaign, run_id, phase}`: what the caller abandons."""
        left = []
        with self.lock:
            self.detach_all = True
            for cid, fl in self.flights.items():
                run_ids = [r for r, _ in fl.runs]
                for i, (_step, fut, hk) in enumerate(fl.tasks):
                    if hk is None or fut.done():
                        continue
                    rid = (hk.run_dir.name if hk.run_dir else
                           (run_ids[i] if i < len(run_ids) else None))
                    if hk.phase == "wait":
                        hk.detach.set()
                        ids = self.forced.setdefault(cid, [])
                        if rid not in ids:
                            ids.append(rid)
                    elif hk.phase in ("spawning", "collect"):
                        left.append({"campaign": cid, "run_id": rid, "phase": hk.phase})
        return left

    def unfinished_in_process(self) -> list:
        """The unfinished hookless tasks (gate / close / resolve / write_spec) as `{campaign,
        run_id, phase: "in_process", step}`: what a stop or crash at the bound leaves behind
        unrecorded otherwise (SPEC-0262)."""
        left = []
        with self.lock:
            for cid, fl in self.flights.items():
                run_ids = [r for r, _ in fl.runs]   # one task per flight, as in force_detach_waiting
                for i, (step, fut, hk) in enumerate(fl.tasks):
                    if hk is None and not fut.done():
                        left.append({"campaign": cid,
                                     "run_id": run_ids[i] if i < len(run_ids) else None,
                                     "phase": "in_process", "step": step.kind})
        return left

    def detached_view(self) -> list:
        """`[{campaign, run_ids}]` of the runs `force_detach_waiting` released."""
        return [{"campaign": c, "run_ids": list(r)} for c, r in self.forced.items()]

    def detach_force(self) -> None:
        """Release every waiting thread; the runs stay adoptable."""
        self.force_detach_waiting()

    def leftover_threads(self) -> list:
        """Worker threads still alive: after `end`, the ones it could not join (SPEC-0229)."""
        live = list(getattr(self._pool, "_threads", ())) if self._pool is not None else []
        return [t for t in self._seen + live if t.is_alive()]

    def end(self, timeout: float = 0.0) -> None:
        """Shut down without waiting, then join the worker threads within `timeout`; threads
        still alive are reported by `leftover_threads` (SPEC-0229)."""
        pool, self._pool = self._pool, None
        if pool is None:
            return
        threads = list(getattr(pool, "_threads", ()))
        self._seen += threads
        pool.shutdown(wait=False, cancel_futures=True)
        deadline = time.monotonic() + timeout
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))

    def submit(self, fn, *args) -> Future:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=self.MAX_WORKERS,
                                            thread_name_prefix="nw-step")
        return self._pool.submit(fn, *args)

    def ids(self) -> set:
        return set(self.flights)

    def any_done(self) -> bool:
        return any(fl.done() for fl in self.flights.values())

    def wait_any(self, timeout=None) -> None:
        pending = [f for fl in self.flights.values() for _, f, _h in fl.tasks if not f.done()]
        if self.any_done():
            return
        if pending:
            wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
        elif timeout:                         # SPEC-0229: nothing to wait on is still a pause
            time.sleep(timeout)

    def wait_all(self) -> None:
        wait([f for fl in self.flights.values() for _, f, _h in fl.tasks])

    def shutdown(self, *, cancel=False) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=cancel)
            self._pool = None


def _harvest(pool: StepPool, report: TickReport, *, guard: bool = False) -> None:
    """Main thread: apply every finished flight, in submission order (SPEC-0213). With `guard`
    (the crash sweep, SPEC-0229) an `_apply` that raises is journaled and skipped."""
    for cid in [c for c, fl in pool.flights.items() if fl.done()]:
        fl = pool.flights.pop(cid)
        st, ctx = fl.state, fl.ctx
        if _left_detached(pool, cid, fl):
            continue
        if fl.group is not None:
            for step, fut, _h in fl.tasks:
                try:
                    fl.group.results[step.persona] = fut.result()
                except Exception as e:          # a bug in a step must park, not kill the loop
                    fl.group.results[step.persona] = e
            _guarded(guard, ctx, st, lambda: report.steps.append(
                _finish_reviews(ctx, report, fl.group)), report, _group_result(fl.group))
            continue
        (_step, fut, _h), = fl.tasks
        try:
            res = fut.result()
        except Exception as e:
            res = StepResult(st["id"], _step, st["state"], error=repr(e))
            report.errors.append(f"{st['id']}: {e!r}")
        if res.crashed:
            report.errors.append(f"{st['id']}: {res.crashed}")
        _guarded(guard, ctx, st, lambda: report.steps.append(_apply(ctx, st, res)), report, res)


def _left_detached(pool: StepPool, cid: str, fl: _Flight) -> bool:
    """A finished flight with a task that returned DETACHED holds a live run, not a result: a
    review group would apply it as a missing verdict. Record its runs in `pool.forced` (the
    `detached_view` the next runner adopts) and say so (SPEC-0287: a run spawned after the
    crash sweep's detach). Only done futures are read, so the answer cannot change. A cancelled
    future is not detached: its `CancelledError` is applied as the step's error by the caller."""
    run_ids = [r for r, _ in fl.runs]
    hit = []
    for i, (_step, fut, hk) in enumerate(fl.tasks):
        if (not fut.cancelled() and fut.exception() is None
                and getattr(fut.result(), "detached", False)):
            hit.append(hk.run_dir.name if hk is not None and hk.run_dir else
                       (run_ids[i] if i < len(run_ids) else None))
    if hit:
        with pool.lock:
            ids = pool.forced.setdefault(cid, [])
            ids.extend(r for r in hit if r not in ids)
    return bool(hit)


def _group_result(group) -> StepResult:
    """A review fan-out as one StepResult carrying its members' runs (SPEC-0484: what a failed
    `_finish_reviews` closes)."""
    st = group.state
    members = []
    for persona, (rid, budget) in (group.meta or {}).items():
        item = group.results.get(persona)
        rr = item.run_result if isinstance(item, StepResult) else None
        members.append(StepResult(st["id"], Step("review", persona=persona), st.get("state"),
                                  run_id=rid, budget=budget, run_result=rr))
    return StepResult(st["id"], group.step, st.get("state"), reviews=members)


def _guarded(guard: bool, ctx: _Ctx, st: dict, fn, report=None, res=None) -> None:
    """Run one harvest apply. SPEC-0484: an exception parks only this campaign
    (`apply_error`), in a normal tick and in the crash sweep (`guard`, SPEC-0229, which also
    journals `harvest_error`); it never propagates to the loop."""
    try:
        fn()
    except Exception as e:           # noqa: BLE001
        if guard:
            journal(ctx.cfg, "harvest_error", campaign=st.get("id"),
                    error=f"{type(e).__name__}: {e}")
        if res is None:
            res = StepResult(st.get("id"), Step("unknown"), st.get("state"))
        try:
            row = _contain_apply_error(ctx, st, res, e)
        except Exception:            # noqa: BLE001 — containment never raises; belt and braces
            return
        if report is not None and not guard:      # the crash sweep's report keeps its shape
            report.steps.append(row)
            report.errors.append(f"{st.get('id')}: apply_error: {type(e).__name__}: {e}")


def _submit(ctx: _Ctx, pool: StepPool, report: TickReport, pending_agent: list,
            groups: list) -> None:
    """Submit the planned agent steps as one flight per campaign."""
    rows = {}
    for st, step, run_id, req, budget, group in pending_agent:
        fl = pool.flights.get(st["id"])
        if fl is None:
            fl = pool.flights[st["id"]] = _Flight(st, ctx, group=group)
            rows[st["id"]] = {"campaign": st["id"], "action": step.kind,
                              "state": st.get("state"), "run_ids": []}
            report.started.append(rows[st["id"]])
        if step.kind == "gate":
            fl.tasks.append((step, pool.submit(_do_gate, ctx, st), None))
        elif step.kind == "finalize_split":
            fl.tasks.append((step, pool.submit(_do_finalize_split, ctx, st), None))
        elif step.kind == "close":
            fl.tasks.append((step, pool.submit(_do_prepare_close, ctx, st), None))
        elif step.kind == "resolve":
            fl.tasks.append((step, pool.submit(_do_resolve, ctx, st, step, run_id, req, budget), None))
            rows[st["id"]]["run_ids"].append(run_id)
            fl.runs.append((run_id, getattr(req, "timeout_s", None)))
        elif step.kind == "write_spec":
            fl.tasks.append((step, pool.submit(_do_write_spec, ctx, st, step, run_id, req,
                                               budget), None))
            rows[st["id"]]["run_ids"].append(run_id)
            fl.runs.append((run_id, getattr(req, "timeout_s", None)))
        elif step.kind == "spec_review":
            fl.tasks.append((step, pool.submit(_do_spec_review, ctx, st, step, run_id, req,
                                               budget), None))
            rows[st["id"]]["run_ids"].append(run_id)
            fl.runs.append((run_id, getattr(req, "timeout_s", None)))
        else:
            hooks = pool.hooks(req.run_dir)
            slot_hold = None
            reservations = getattr(ctx, "_slot_reservations", None)
            if reservations is not None and step.persona:
                slot_hold = reservations.pop((st["id"], step.persona), None)
            fl.tasks.append((step, pool.submit(_do_persona, ctx, st, step, run_id, req, budget,
                                               hooks, None, slot_hold), hooks))
            rows[st["id"]]["run_ids"].append(run_id)
            fl.runs.append((run_id, getattr(req, "timeout_s", None)))
    for group in groups:              # a fan-out whose every prepare failed: nothing to run
        if group.state["id"] not in pool.flights:
            report.steps.append(_finish_reviews(ctx, report, group))


GATE_HOLD_SLACK_S = 600     # SPEC-0522: past its longest gate timeout + this, a step holds nobody


def _gate_hold_s(pcfg) -> float:
    """SPEC-0522: how long one gate step (or re-gating close) may hold other gate starts: the
    project's longest gate `timeout_s` (at least the default) plus `GATE_HOLD_SLACK_S`."""
    outs = [float(g.get("timeout_s") or 0) for g in (pcfg or {}).get("gates") or []]
    return max(outs + [float(P.GATE_DEFAULTS["timeout_s"])]) + GATE_HOLD_SLACK_S


def _flight_age_s(fl, now) -> float | None:
    import datetime as dt
    try:
        return (now - dt.datetime.fromisoformat(fl.started_at)).total_seconds()
    except (TypeError, ValueError):
        return None


def _gate_flights(ctx: _Ctx, pool: StepPool, now=None) -> list:
    """`(campaign id, exclusive)` of each gate step in flight (SPEC-0311). A `close` whose
    project re-gates counts as a gate step: it runs the same full suite. SPEC-0522: a step in
    flight longer than `_gate_hold_s` no longer counts (it keeps running but holds no other
    gate start); `gate_hold_expired` is journaled once per flight. SPEC-0523: a gate step is
    exclusive only while its marker is younger than `exclusive_wait_max_s` (from `entered_at`);
    past that it counts as a normal gate step and `gate_exclusive_lapsed` is journaled once."""
    import datetime as dt
    now = now or dt.datetime.now().astimezone()
    excl_max = exclusive_wait_max_s(ctx.cfg)
    out = []
    for fl in list(pool.flights.values()):
        for step, fut, _hooks in list(fl.tasks):
            if fut.done():
                continue
            if step.kind not in ("close", "gate"):
                continue
            try:
                pcfg = ctx.project(fl.state)
                if step.kind == "close" and not _close_regates(fl.state, pcfg):
                    continue
            except ValueError:
                if step.kind == "close":
                    continue
                pcfg = None
            age, hold = _flight_age_s(fl, now), _gate_hold_s(pcfg)
            if age is not None and age > hold:
                key = ("gate_hold_expired", fl.state.get("id"), fl.started_at)
                if key not in pool.deferred_noted:
                    pool.deferred_noted.add(key)
                    journal(ctx.cfg, "gate_hold_expired", campaign=fl.state.get("id"),
                            step=step.kind, age_s=round(age), bound_s=round(hold),
                            exclusive=bool(gate_exclusive(fl.state)))
                continue
            exclusive = step.kind == "gate" and bool(gate_exclusive(fl.state))
            if exclusive:
                held_s = exclusive_waited_s(fl.state, now)
                if held_s > excl_max:
                    exclusive = False
                    key = ("gate_exclusive_lapsed", fl.state.get("id"), fl.started_at)
                    if key not in pool.deferred_noted:
                        pool.deferred_noted.add(key)
                        journal(ctx.cfg, "gate_exclusive_lapsed", campaign=fl.state.get("id"),
                                age_s=None if held_s == math.inf else round(held_s),
                                bound_s=excl_max)
            out.append((fl.state["id"], exclusive))
    return out


def _gates_in_flight(ctx: _Ctx, pool: StepPool, now=None) -> tuple[int, bool]:
    """(gate steps in flight, whether one is exclusive) (SPEC-0311)."""
    flights = _gate_flights(ctx, pool, now)
    return len(flights), any(ex for _cid, ex in flights)


def _gate_starts(ctx: _Ctx, pool: StepPool, busy: list, admitted_ids: set, flying: set) -> set:
    """Ids of the GATING campaigns allowed to start their gate step this tick (SPEC-0311).
    SPEC-0507: an exclusive marker pending longer than `exclusive_wait_max_s` is cleared (with a
    ledger note) and its gate starts now as a normal one, unless an exclusive gate is running."""
    candidates = [s for s in busy if s.get("state") == "GATING" and s["id"] not in admitted_ids
                  and s["id"] not in flying]
    n, exclusive = _gates_in_flight(ctx, pool)
    if exclusive:
        return set()
    import datetime as dt
    now = dt.datetime.now().astimezone()
    wait_max = exclusive_wait_max_s(ctx.cfg)
    expired = []
    for cid in expired_exclusives(candidates, now, wait_max):
        st = next(c for c in candidates if c["id"] == cid)
        if _expire_exclusive(ctx, pool, st, now, wait_max):
            expired.append(cid)
    rest = [c for c in candidates if c["id"] not in expired]
    return set(expired) | set(select_gate_starts(rest, n, False))


def _flight_list(ids: list) -> str:
    return ", ".join(ids) if ids else "none"


def _expire_exclusive(ctx: _Ctx, pool: StepPool, st: dict, now, wait_max: int) -> bool:
    """Clear a too-old exclusive marker on disk and in `st`, with a ledger note (SPEC-0507).
    False when the campaign moved on meanwhile (the marker on disk is not this one)."""
    cfg, cid, marker = ctx.cfg, st["id"], gate_exclusive(st)
    waited = exclusive_waited_s(st, now)
    flights = [f for f, _ex in _gate_flights(ctx, pool)]
    if not store.campaign_dir(cfg, cid).is_dir():
        return False
    with store.item_lock(cfg, cid):
        try:
            cur = store.read_state(cfg, cid)
        except ValueError:
            return False
        if cur.get("state") != "GATING" or gate_exclusive(cur) != marker:
            return False
        cur["gate_exclusive"] = None
        shown = "?" if waited == math.inf else str(int(waited))
        store.append_event(cfg, cid, "note", {
            "text": f"exclusive re-gate waited {shown}s (max {wait_max}s) for "
                    f"{len(flights)} gate step(s) ({_flight_list(flights)}); "
                    "starting non-exclusively",
            "author": "queue"})
        store.write_state(cfg, cid, cur)
    st["gate_exclusive"] = None
    journal(cfg, "gate_exclusive_expired", campaign=cid,
            waited_s=None if waited == math.inf else int(waited), in_flight=flights)
    return True


def _exclusive_held(ctx: _Ctx, pool: StepPool, busy: list) -> list:
    """Ids of the exclusive re-gates pending or running (SPEC-0507), oldest first: while any,
    a close that re-gates does not start either."""
    held = [s for s in busy if s.get("state") == "GATING" and gate_exclusive(s)]
    running = {cid for cid, ex in _gate_flights(ctx, pool) if ex}
    return sorted({s["id"] for s in held} | running)


def _note_gate_deferred(ctx: _Ctx, pool: StepPool, st: dict, busy: list = ()) -> None:
    """Journal (runner memory de-dupes) that an exclusive re-gate waits, once per campaign per
    `timeout_regates` value (SPEC-0311). SPEC-0507: the first deferral of each marker also
    appends a ledger `note` naming the gate steps in flight and the older exclusive re-gates it
    queues behind (`gate_exclusive.noted` on disk keeps it once across runner restarts)."""
    marker = gate_exclusive(st)
    if not marker:
        return
    key = (st["id"], int((st.get("counters") or {}).get("timeout_regates", 0)),
           marker.get("entered_at"))
    if key not in pool.deferred_noted:
        pool.deferred_noted.add(key)
        journal(ctx.cfg, "gate_timeout_deferred", campaign=st["id"], timeout_regates=key[1])
    if marker.get("noted"):
        return
    cfg, cid = ctx.cfg, st["id"]
    flights = [f for f, _ex in _gate_flights(ctx, pool)]
    ahead = [s["id"] for s in sorted(busy, key=_exclusive_key)
             if s["id"] != cid and s.get("state") == "GATING" and gate_exclusive(s)
             and _exclusive_key(s) < _exclusive_key(st)]
    text = (f"exclusive re-gate waiting: {len(flights)} gate step(s) in flight "
            f"({_flight_list(flights)})")
    if ahead:
        text += f"; queued behind exclusive re-gate(s) {_flight_list(ahead)}"
    if not store.campaign_dir(cfg, cid).is_dir():
        return
    with store.item_lock(cfg, cid):
        try:
            cur = store.read_state(cfg, cid)
        except ValueError:
            return
        if gate_exclusive(cur) != marker:
            return
        cur["gate_exclusive"] = {**marker, "noted": True}
        store.append_event(cfg, cid, "note", {"text": text, "author": "queue"})
        store.write_state(cfg, cid, cur)
    st["gate_exclusive"] = dict(cur["gate_exclusive"])


def _note_close_deferred(ctx: _Ctx, pool: StepPool, st: dict, held: list) -> None:
    """A close that re-gates waits for the exclusive re-gates `held` (SPEC-0507): journal and
    note it once per campaign per head exclusive re-gate (runner memory)."""
    key = ("close", st["id"], held[0])
    if key in pool.deferred_noted:
        return
    pool.deferred_noted.add(key)
    cfg, cid = ctx.cfg, st["id"]
    journal(cfg, "close_deferred_for_exclusive", campaign=cid, exclusive=held)
    if not store.campaign_dir(cfg, cid).is_dir():
        return
    with store.item_lock(cfg, cid):
        store.append_event(cfg, cid, "note", {
            "text": f"close re-gate waits for exclusive re-gate(s) {_flight_list(held)}",
            "author": "queue"})


def _sentinel_busy(pool: StepPool) -> bool:
    """A sentinel run (SPEC-0359) is in flight on the pool."""
    fl = getattr(pool, "sentinel", None)
    return fl is not None and not fl[1].done()


def _sentinel_step(ctx: _Ctx, pool: StepPool, busy: list) -> str | None:
    """SPEC-0359 §4: start one due sentinel on a worker thread, only when no campaign waits for
    a gate slot (a live waiter marker, or a GATING campaign whose gate step did not start) and
    no sentinel is in flight. A project whose run lock is held (a CLI run) is skipped, and one
    `SN.backoff` holds back (green or cleared red at the same base sha, or a red / failed run
    there less than `SN.RETRY_S` ago) waits. SPEC-0379: a project is also due on the clock
    (`SN.time_due`: the base moved since the last green run, which is older than
    `agentic.sentinel.interval_hours`) when it has close gates; the candidates stay the projects
    with sentinel state (every merge records it). Returns the project started, if any.

    SPEC-0493: a project with `cadence:` skips `SN.time_due` (its cadence jobs cover the clock);
    when no sentinel run started, `_cadence_scan` may start one cadence job on the same
    `pool.sentinel` slot (a 3-tuple `(project, future, job)`), harvested here as
    `cadence_done` / `cadence_failed`."""
    cfg = ctx.cfg
    fl = pool.sentinel
    if fl is not None:
        project, fut = fl[0], fl[1]
        if not fut.done():
            return None
        pool.sentinel = None
        if len(fl) > 2:
            _cadence_harvest(cfg, project, fl[2], fut)
        else:
            try:
                out = fut.result()
                journal(cfg, "sentinel_done", project=project, ok=out.get("ok"),
                        sha=out.get("sha"), interrupted=out.get("interrupted"))
            except SN.Busy as e:
                journal(cfg, "sentinel_skipped", project=project, reason=str(e))
            except Exception as e:
                journal(cfg, "sentinel_failed", project=project, error=f"{type(e).__name__}: {e}")
    if ctx.stop.is_set():
        return None
    candidates = SN.projects(cfg)
    flying = pool.ids()
    if any(s.get("state") == "GATING" and s["id"] not in flying for s in busy):
        return None
    if gate_slots.occupancy(cfg).get("waiting"):
        return None
    for project in candidates:
        try:
            pcfg = P.project_config(cfg, project)
            due = SN.check_due(cfg, project, root=pcfg.get("_root"), project_cfg=pcfg)
            if not due and (SN.interval_hours(cfg) <= 0 or not P.gates_for(pcfg, "close")
                            or pcfg.get("cadence")):
                continue                    # the clock is off / nothing to run / cadence owns it
            if SN.running(cfg, project):
                continue                                    # a CLI run holds the lock
            # an unresolvable base (renamed branch, re-cloned repo) is a failure, not a run
            base_sha = git.rev_parse(pcfg.get("_root") or P.project_root(cfg, project),
                                     SN._base(pcfg))
            if not due and not SN.time_due(cfg, project, base_sha, root=pcfg.get("_root"),
                                           project_cfg=pcfg):
                continue
            why = SN.backoff(cfg, project, base_sha)
        except (GitError, ValueError, OSError) as e:         # never out of the tick
            err = " ".join(str(e).split())[:500]
            key = ("sentinel", project, err)
            if key not in pool.deferred_noted:              # once per reason, not per tick
                pool.deferred_noted.add(key)
                journal(cfg, "sentinel_failed", project=project, error=err)
            continue
        if why:
            key = ("sentinel_backoff", project, why)
            if key not in pool.deferred_noted:              # once per reason, not per tick
                pool.deferred_noted.add(key)
                journal(cfg, "sentinel_deferred", project=project, reason=why)
            continue
        pool.sentinel = (project, pool.submit(lambda: SN.run(cfg, project, project_cfg=pcfg, stop=ctx.stop)))
        journal(cfg, "sentinel_started", project=project)
        return project
    return _cadence_scan(ctx, pool)


def _cadence_harvest(cfg, project, job, fut) -> None:
    """A finished cadence run (SPEC-0493): `cadence_done`, or `cadence_failed` when `run`
    raised (a held run lock included). The run itself journalled any `cadence_deferred`."""
    try:
        out = fut.result()
        journal(cfg, "cadence_done", project=project, job=job, ok=out.get("ok"),
                sha=out.get("sha"), interrupted=out.get("interrupted"), error=out.get("error"),
                deferred=out.get("deferred"))
    except Exception as e:
        journal(cfg, "cadence_failed", project=project, job=job,
                error=f"{type(e).__name__}: {e}"[:500])


def _cadence_projects(cfg) -> list:
    """Registered projects whose config loads with a non-empty `cadence:` (load errors skipped)."""
    out = []
    for project in registered_projects(cfg):
        try:
            if P.project_config(cfg, project).get("cadence"):
                out.append(project)
        except (ValueError, OSError):
            continue
    return out


def _cadence_scan(ctx: _Ctx, pool: StepPool) -> str | None:
    """SPEC-0493: start at most one due cadence job, on the first registered project (in order)
    that has one; `CAD.pick` seeds and reconciles the rows before the due check. Called only
    when no sentinel is in flight or started, the tick is not stopping and no campaign waits
    for a gate slot. A load or base-sha failure is journalled `cadence_failed` once per reason."""
    cfg = ctx.cfg
    for project in registered_projects(cfg):
        try:
            root = P.project_root(cfg, project)
            if not P.config_path(root).is_file():
                continue                            # no repo / not a nightwright-run project
        except (ValueError, OSError):
            continue
        try:
            pcfg = P.project_config(cfg, project)
            if not pcfg.get("cadence") or SN.running(cfg, project):
                continue
            base_sha = git.rev_parse(pcfg.get("_root") or P.project_root(cfg, project),
                                     SN._base(pcfg))
            hit = CAD.pick(cfg, project, project_cfg=pcfg, base_sha=base_sha)
        except (GitError, ValueError, OSError) as e:         # never out of the tick
            err = " ".join(str(e).split())[:500]
            key = ("cadence", project, err)
            if key not in pool.deferred_noted:              # once per reason, not per tick
                pool.deferred_noted.add(key)
                journal(cfg, "cadence_failed", project=project, error=err)
            continue
        if hit is None:
            continue
        job, why = hit
        pool.sentinel = (project, pool.submit(
            lambda: CAD.run(cfg, project, job, project_cfg=pcfg, stop=ctx.stop, reason=why)), job)
        journal(cfg, "cadence_started", project=project, job=job, reason=why)
        return project
    return None


def _publish_credentials(lease, creds) -> None:
    """SPEC-0487: `{VAR: status}` (never a value) in the lease info, for `nw doctor`."""
    try:
        if lease is not None and hasattr(lease, "update_info"):
            lease.update_info(credentials=dict(creds or {}))
    except Exception:                                   # noqa: BLE001
        pass


def tick(cfg, *, stop: threading.Event | None = None, runner=None, admit=True,
         pool: StepPool | None = None, lease=None, draining: bool = False) -> TickReport:
    """One bounded iteration: apply finished steps, admit, open, plan, submit agent steps,
    close serially; every result is applied on this thread. The caller holds the lease.

    With `pool` (SPEC-0213, owned by `run()`), agent steps outlive the tick: a campaign with a
    step in flight is skipped (and counts as busy) until a later tick harvests its result.
    Without one, the tick waits for its own steps before the closes (single shot). With `lease`
    (SPEC-0228, passed by `run()`), the long main-thread steps declare themselves `busy`."""
    own = pool is None
    pool = StepPool() if own else pool
    creds = CR.ensure(cfg)  # SPEC-0483: a credential missing so far is retried every tick
    _publish_credentials(lease, creds)
    RNJ.service(cfg, stop=stop)          # SPEC-0487: CLI persona runs, with these credentials
    ctx = _Ctx(cfg, stop=stop, runner=runner, lease=lease)
    report = TickReport()
    try:
        _tick(cfg, ctx, pool, report, admit=admit, own=own, draining=draining and not own)
        _relay_hosts(cfg)
    finally:
        if own:
            pool.shutdown()
    report.in_flight = list(pool.flights)
    remaining = [s for s in store.list_campaigns(cfg) if s.get("state") != "PARKED"]
    report.idle = not remaining and not pool.flights
    return report


def _relay_hosts(cfg) -> None:
    """SPEC-0530: copy enabled federation hosts' events into this hub's log; a failure is
    journalled and never fails the tick. Inert without `hosts.json`."""
    try:
        from . import federation as FED
        FED.relay_tick(cfg)
    except Exception as e:                                       # noqa: BLE001
        journal(cfg, "federation_relay_failed", error=f"{type(e).__name__}: {e}"[:500])


# ---- red episodes (SPEC-0420) -------------------------------------------------------------

def _apply_close_held(cfg, state: dict, held: str, events: list, finish) -> dict:
    """A close the red base holds: the campaign stays CLOSING, no attempt is counted, nothing
    parks; one `close_held` ledger event per campaign per episode."""
    red = RM.read(cfg, state.get("project")) or {}
    episode = red.get("episode")
    closing = dict(state.get("closing") or {})
    if closing.get("held_episode") != episode:
        closing["held_episode"] = episode
        events.append(("close_held", {"episode": episode, "reason": held}))
    state["closing"] = closing
    row = finish("CLOSING", "close_held")
    return row


def _red_fix_merged(cfg, state: dict, close: dict | None) -> None:
    try:
        if RM.record_merge(cfg, state, (close or {}).get("merged_sha")):
            journal(cfg, "red_main_fix_merged", campaign=state["id"],
                    project=state.get("project"), merged_sha=(close or {}).get("merged_sha"))
    except (OSError, ValueError) as e:
        journal(cfg, "red_main_failed", campaign=state["id"], error=str(e)[:500])


def _red_abandon(cfg, cid: str, rule: str, why: str) -> bool:
    """Abandon a fix campaign by the queue: PARKED / QUEUED / WAITING at once; an active one
    gets a park request first (abandoned on a later tick, once parked)."""
    st = store.read_state(cfg, cid)
    s = st.get("state")
    if s in TERMINAL_STATES:
        return False
    if s in ("PARKED", "QUEUED", "WAITING"):
        if (st.get("park") or {}).get("reason") == "human_decision":
            return False                          # the human's escalation decides it
        _record_queue_decision(cfg, cid, "abandon", rule, why, action="abandon")
        apply_abandon(cfg, plan_abandon(cfg, cid, by="queue"))
        return True
    with store.item_lock(cfg, cid):
        cur = store.read_state(cfg, cid)
        if cur.get("state") in TERMINAL_STATES or cur.get("state") in ("PARKED", "CLOSING") \
                or cur.get("park_requested"):
            return False
        cur["park_requested"] = {"reason": "human", "detail": f"{rule}: {why}", "by": "queue",
                                 "at": store.now_iso()}
        store.write_state(cfg, cid, cur)
    return False


def _admit_red_fix(ctx: _Ctx, report: TickReport, st: dict) -> None:
    """SPEC-0420 §3: a fix campaign is admitted at once, one slot over the profile's `slots`
    (a queue full of held closes must not starve it)."""
    _transition(ctx.cfg, st, "OPENING", "red_main_admitted", by="queue")
    report.admitted += 1
    report.steps.append(_apply(ctx, st, _do_open(ctx, st)))


def _red_main_step(ctx: _Ctx, pool: StepPool, report: TickReport, *, own: bool) -> None:
    """Tick step (after the sentinel step): for each project with an open red episode, refresh
    the fix outcomes and act on `redmain.step` (open a fix-forward, localize the culprit, open a
    revert, escalate once). Also adopts a pre-SPEC-0420 red sentinel once, admits queued fix
    campaigns and abandons the fix campaigns a green sentinel made moot."""
    cfg = ctx.cfg
    if not RM.enabled(cfg):
        return
    fl = getattr(pool, "red_localize", None)
    if fl is not None and fl[1].done():
        pool.red_localize = None
        _red_localized(cfg, fl[0], fl[1])
    flying = pool.ids()
    projects = list(SN.projects(cfg))
    projects += [p for p in _cadence_projects(cfg) if p not in projects]   # SPEC-0493
    for project in projects:
        ctx.progress()
        try:
            pcfg = P.project_config(cfg, project)
            try:
                sst = SN.read(cfg, project, root=pcfg.get("_root"), project_cfg=pcfg)
            except SN.Unreadable:
                sst = None
            if RM.adopt_legacy(cfg, project, sst):
                journal(cfg, "red_main_adopted", project=project)
            for st in RM.moot_campaigns(cfg, project):
                if st["id"] in flying:
                    continue
                if _red_abandon(cfg, st["id"], "red_main.moot", "the base is green again; this "
                                "fix is moot"):
                    RM.mark_moot(cfg, project, st["id"])
            red, failed = RM.refresh(cfg, project)
            if not red or not red.get("open"):
                continue
            for cid in failed:
                if cid not in flying:
                    _red_abandon(cfg, cid, "red_main.fix_failed",
                                 f"red-base fix failed (episode {red['episode']}); the episode "
                                 "goes on without it")
            active = store.list_campaigns(cfg, project=project)
            for st in active:
                if st.get("kind") in RM.RED_KINDS and st.get("state") == "QUEUED" \
                        and st["id"] not in flying and not ctx.stop.is_set() \
                        and not st.get("park_requested"):
                    _admit_red_fix(ctx, report, st)
            action, why = RM.step(cfg, project, active=store.list_campaigns(cfg, project=project),
                                  red=red, project_cfg=pcfg)
            _red_act(ctx, pool, report, project, pcfg, red, action, why, own=own)
        except (GitError, ValueError, OSError) as e:
            err = " ".join(str(e).split())[:500]
            key = ("red_main", project, err)
            if key not in pool.deferred_noted:
                pool.deferred_noted.add(key)
                journal(cfg, "red_main_failed", project=project, error=err)


def _red_localized(cfg, project, fut) -> None:
    try:
        out = fut.result()
    except Exception as e:
        out = {"culprit": None, "why": f"localize failed: {type(e).__name__}: {e}"[:500]}
    if out.get("retry"):
        journal(cfg, "red_main_localize_deferred", project=project, reason=out.get("why"))
        return
    rec = RM.set_localized(cfg, project, out)
    c = (rec or {}).get("culprit")
    journal(cfg, "red_main_localized", project=project,
            culprit=(c or {}).get("campaign"), why=out.get("why"), probes=out.get("probes"))
    _record_queue_decision(cfg, None, "other", "red_main.localize",
                           f"{project}: culprit {(c or {}).get('campaign') or 'none'} "
                           f"({out.get('why')})")


def _red_act(ctx, pool, report, project, pcfg, red, action, why, *, own) -> None:
    cfg = ctx.cfg
    if action == "none" or ctx.stop.is_set():
        return
    ep = red["episode"]
    if action in ("open_fixforward", "open_revert"):
        st = (RM.open_fixforward if action == "open_fixforward" else RM.open_revert)(cfg, project)
        journal(cfg, "red_main_fix_opened", project=project, campaign=st["id"],
                kind=st["kind"], episode=ep)
        _record_queue_decision(cfg, st["id"], "other", f"red_main.{action}",
                               f"red episode {ep} of {project}: {why}",
                               action=f"open {st['kind']} {st['id']}")
        report.steps.append({"campaign": st["id"], "from": None, "to": "QUEUED",
                             "action": action, "reason": why, "run_id": None, "cost_usd": None})
        _admit_red_fix(ctx, report, st)
        return
    if action == "localize":
        if getattr(pool, "red_localize", None) is not None or _sentinel_busy(pool):
            return                                  # one at a time, sharing the sentinel's slot
        root = pcfg.get("_root")
        journal(cfg, "red_main_localize_started", project=project, episode=ep)
        if own:
            fut: Future = Future()
            try:
                fut.set_result(RM.localize(cfg, project, root=root, project_cfg=pcfg,
                                           stop=ctx.stop))
            except Exception as e:
                fut.set_exception(e)
            _red_localized(cfg, project, fut)
        else:
            pool.red_localize = (project, pool.submit(
                lambda: RM.localize(cfg, project, root=root, project_cfg=pcfg, stop=ctx.stop)))
        return
    if action == "escalate":
        rec = RM.escalate(cfg, project, why)
        if rec is not None:
            journal(cfg, "red_main_escalated", project=project, escalation=rec["id"], why=why)
            _record_queue_decision(cfg, None, "escalate", "red_main.escalate",
                                   f"red episode {ep} of {project}: {why}",
                                   action=f"escalate {rec['id']}")


# ---- the close train (SPEC-0260) -----------------------------------------------------------

def _train_key(state: dict) -> tuple:
    """(entered CLOSING, id). A missing or unparseable `closing.entered_at` (a campaign that
    entered CLOSING before SPEC-0260) sorts as the earliest possible time."""
    import datetime as dt
    ts = (state.get("closing") or {}).get("entered_at")
    try:
        t = dt.datetime.fromisoformat(ts)
        t = t.astimezone() if t.tzinfo is None else t
    except (TypeError, ValueError):
        t = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    return t, state["id"]


def _repo_root(state: dict) -> str | None:
    return (state.get("repo") or {}).get("root")


def close_train(states, close_flights) -> dict[str, list[str]]:
    """Pure: `{repo root: [campaign ids in close order]}`. Members are the CLOSING campaigns of
    `states` that are not resolving, in flight or not; a campaign with no root is its own train
    (keyed by its id). Order is `(entered_at, id)`; a member in `close_flights` goes first, so a
    running close is always the head."""
    groups: dict[str, list[dict]] = {}
    for st in states:
        if st.get("state") != "CLOSING" or (st.get("closing") or {}).get("resolve"):
            continue
        root = _repo_root(st) or st["id"]
        groups.setdefault(root, []).append(st)
    # SPEC-0420: a red-base fix closes ahead of the (held) campaigns that wait for it
    return {root: [s["id"] for s in sorted(members, key=lambda s: (s["id"] not in close_flights,
                                                                    not _fixes_red_base(s),
                                                                    *_train_key(s)))]
            for root, members in groups.items()}


def _fixes_red_base(state: dict) -> bool:
    return state.get("kind") in RM.RED_KINDS or bool(state.get("fixes_red"))


def _train_wait(cfg, st: dict, head_id: str, position: int) -> None:
    """A train member behind `head_id`: record `closing.queued_behind` and one ledger note,
    only when the head it waits behind changed."""
    if (st.get("closing") or {}).get("queued_behind") == head_id:
        return
    cid = st["id"]
    with store.item_lock(cfg, cid):
        cur = store.read_state(cfg, cid)
        if cur.get("state") != "CLOSING" or (cur.get("closing") or {}).get("queued_behind") == head_id:
            return
        cur["closing"] = {**(cur.get("closing") or {}), "queued_behind": head_id}
        store.append_event(cfg, cid, "note", {
            "text": f"close queued behind {head_id} (position {position})", "author": "queue"})
        store.write_state(cfg, cid, cur)
    st["closing"] = dict(cur["closing"])


def _train_head(cfg, st: dict) -> None:
    """The train's head is about to close: a former waiter drops `queued_behind` and records
    `head_since` (on disk and in `st`, which the close carries forward)."""
    if not (st.get("closing") or {}).get("queued_behind"):
        return
    cid = st["id"]
    with store.item_lock(cfg, cid):
        cur = store.read_state(cfg, cid)
        if cur.get("state") != "CLOSING":
            return
        closing = dict(cur.get("closing") or {})
        closing.pop("queued_behind", None)
        closing["head_since"] = store.now_iso()
        cur["closing"] = closing
        store.write_state(cfg, cid, cur)
    st["closing"] = dict(closing)


# ---- parks the queue decides itself (SPEC-0302) -----------------------------------------

_AUTO_REASON = {AP.RULE_BASE_MOVING: "base_moving", AP.RULE_COST_CAP: "cost_cap"}


def _record_queue_decision(cfg, cid, kind, rule, rationale, *, action=None) -> bool:
    """One `by: queue` decision record. Never called with an item lock held (`record` takes the
    decisions lock first and the item lock inside it). A failure is journaled, not raised."""
    try:
        DEC.record(cfg, campaign=cid, by="queue", kind=kind, rule=rule, action=action,
                   rationale=rationale)
        return True
    except (ValueError, OSError) as e:
        journal(cfg, "decision_record_failed", campaign=cid, rule=rule, error=str(e))
        return False


def settle_decision_requests(cfg, campaign=None) -> int:
    """SPEC-0318: write the decision record of every PARKED campaign whose park still carries a
    `decision_request` (the park command's own, or a crash / deferred park left it), then clear
    the request. `campaign` limits it to one campaign. Never called with an item lock held; a
    failure is journaled (`decision_record_failed`) and retried on the next call. Returns the
    number of records written."""
    done = 0
    for st in store.list_campaigns(cfg, state="PARKED"):
        cid = st["id"]
        req = (st.get("park") or {}).get("decision_request")
        if (campaign is not None and cid != campaign) or not isinstance(req, dict):
            continue
        try:
            DEC.record(cfg, campaign=cid, kind=req.get("kind"), rule=req.get("rule"),
                       rationale=req.get("rationale"), by=req.get("by"),
                       action=f"park: {(st.get('park') or {}).get('detail')}")
        except (ValueError, OSError) as e:
            journal(cfg, "decision_record_failed", campaign=cid, rule=req.get("rule"),
                    error=str(e))
            continue
        done += 1
        with store.item_lock(cfg, cid):
            cur = store.read_state(cfg, cid)
            park = cur.get("park")
            if cur.get("state") == "PARKED" and isinstance(park, dict) \
                    and park.pop("decision_request", None) is not None:
                store.write_state(cfg, cid, cur)
    return done


def _drop_decision_request(cfg, st: dict) -> None:
    """SPEC-0318: a resume or abandon clears the park; a request it still carries is dropped
    (journaled), never recorded late."""
    req = (st.get("park") or {}).get("decision_request")
    if isinstance(req, dict):
        journal(cfg, "decision_request_dropped", campaign=st["id"], rule=req.get("rule"))


def _auto_wait_recorded(cfg, cid, auto: dict) -> bool:
    """True when the campaign's `wait` record exists; writes it when missing (a crash between
    the park and the record, or a failed write, is repaired here)."""
    if AP.has_wait_note(store.read_ledger(cfg, cid, types=["park", "note"]), auto.get("rule")):
        return True
    return _record_queue_decision(
        cfg, cid, "wait", auto["rule"], auto.get("rationale") or "queue-decided park",
        action=f"auto-resume at {auto.get('resume_at')}")


def _record_auto_waits(cfg) -> None:
    if not AP.settings(cfg)["enabled"]:
        return
    for st in store.list_campaigns(cfg, state="PARKED"):
        auto = (st.get("park") or {}).get("auto")
        if isinstance(auto, dict) and auto.get("rule"):
            _auto_wait_recorded(cfg, st["id"], auto)


def _auto_give_up(cfg, cid, rule, error) -> bool:
    """The queue gives up an automatic resume: clear `park.auto`, append `auto_resume_failed`
    (an always-wake event) and record why, so the conductor takes the park over. A campaign
    that is no longer PARKED, or whose park was replaced, only loses a stale `auto`."""
    with store.item_lock(cfg, cid):
        st = store.read_state(cfg, cid)
        park = st.get("park") if st.get("state") == "PARKED" else None
        if not isinstance(park, dict) or not isinstance(park.get("auto"), dict):
            return False
        park.pop("auto")
        store.write_state(cfg, cid, st)
        store.append_event(cfg, cid, "auto_resume_failed", {"rule": rule, "error": error},
                           by="queue")
    _record_queue_decision(cfg, cid, "other", rule, f"auto-resume failed: {error}")
    return True


def _auto_resume_step(cfg, flying, *, busy_count, slots, report) -> list:
    """Tick step 0a. Returns the states it resumed (for the tick's busy / queued lists)."""
    import datetime as dt
    sett = AP.settings(cfg)
    pending = [s for s in store.list_campaigns(cfg, state="PARKED")
               if isinstance((s.get("park") or {}).get("auto"), dict) and s["id"] not in flying]
    if not sett["enabled"]:
        for st in pending:
            _auto_give_up(cfg, st["id"], st["park"]["auto"].get("rule") or "auto_resume_failed",
                          "auto-resume disabled")
        return _cost_policy_parked(cfg, flying, report=report, slots=slots,
                                   busy_count=busy_count)
    resumed = []
    for st in list(pending):
        if not st["park"]["auto"].get("rule"):        # corrupt: nothing to record or resume
            _auto_give_up(cfg, st["id"], "auto_resume_failed", "park.auto has no rule")
            pending.remove(st)
            continue
        _auto_wait_recorded(cfg, st["id"], st["park"]["auto"])
    for st in AP.due(pending, dt.datetime.now().astimezone()):
        cid, auto = st["id"], st["park"]["auto"]
        rule = auto.get("rule")
        cur = store.read_state(cfg, cid)
        cur_park, cur_auto = cur.get("park") or {}, (cur.get("park") or {}).get("auto")
        if cur.get("state") != "PARKED" or not isinstance(cur_auto, dict) \
                or cur_auto.get("rule") != rule:
            continue                    # a human resumed or re-parked it meanwhile
        if not AP.has_wait_note(store.read_ledger(cfg, cid, types=["park", "note"]), rule):
            continue                    # never apply an auto decision without its record
        from . import escalations as ESC
        n = int(((cur.get("counters") or {}).get("auto_resumes") or {}).get(rule, 0))
        note = f"queue: {rule} auto-resume {n + 1}/{sett['max_per_campaign']}"
        error, resume_recorded = None, False
        if cur_park.get("reason") != _AUTO_REASON.get(rule):
            error = f"park reason is now {cur_park.get('reason')}"
        elif ESC.open_for(cfg, cid) is not None:
            error = "an escalation is open"
        else:
            try:
                # no `note`: it would become a persistent pseudo-human brief note; the ledger
                # event carries the bookkeeping text instead
                # the cap was computed at park time: never lower one raised since
                raise_cap = cur_auto.get("raise_cap")
                if raise_cap is not None and \
                        float(raise_cap) <= float((cur.get("cost") or {}).get("cap_usd") or 0.0):
                    raise_cap = None
                plan = plan_resume(cfg, cid, by="queue", raise_cap=raise_cap,
                                   auto_rule=rule, event_note=note)
            except ValueError as e:
                error = str(e)
        if error is None:
            cap = plan.get("raise_cap")
            resume_recorded = _record_queue_decision(
                cfg, cid, "resume", rule, f"scheduled automatic resume reached ({note})",
                action=f"resume to {plan['to']}" + (f" raise cap to ${cap:.2f}" if cap else ""))
            try:
                # states resumed earlier in this step are not in the tick's `busy` yet
                taken = sum(1 for r in resumed if r["state"] in SLOT_STATES)
                after = apply_resume(cfg, plan, busy=busy_count() + taken, slots=slots)
            except ValueError as e:
                error = str(e)
        if error is not None:
            if not _auto_give_up(cfg, cid, rule, error) and resume_recorded:
                # the park was resumed or replaced by someone else after the `resume` record
                _record_queue_decision(cfg, cid, "other", rule,
                                       f"automatic resume not applied: {error}")
            continue
        report.steps.append({"campaign": cid, "from": "PARKED", "to": after["state"],
                             "action": "auto_resume", "reason": rule,
                             "run_id": None, "cost_usd": None})
        resumed.append(after)
    # SPEC-0422: `cost_cap` parks the policy has not decided yet (before the deploy, or while
    # the policy was off) are re-evaluated: resumed (raised when needed) or escalated once
    resumed += _cost_policy_parked(
        cfg, flying, report=report, slots=slots,
        busy_count=lambda: busy_count() + sum(1 for r in resumed if r["state"] in SLOT_STATES))
    return resumed


def _tick(cfg, ctx: _Ctx, pool: StepPool, report: TickReport, *, admit: bool, own: bool,
          draining: bool = False):
    if not pool.adopted:
        _adopt(cfg, ctx, pool, report)
    ctx.progress()                    # SPEC-0313: the loop is alive inside a long tick
    _harvest(pool, report)            # finished steps first: their slots are free this tick
    ctx.progress()
    flying = pool.ids()
    for st in store.list_campaigns(cfg):          # SPEC-0253: residue of a killed runner
        ctx.progress()
        store.sweep_campaign_tmp(cfg, st["id"])
    disk_clean.sweep(cfg)             # SPEC-0524: bounded leftovers of finished work, hourly
    _settle_ended_remote_jobs(cfg, flying)
    _apply_worker_escalations(cfg)
    _finish_worker_drains(cfg, progress=ctx.progress)
    _run_worker_updates(cfg, progress=ctx.progress)
    items = [s for s in store.list_campaigns(cfg) if s.get("state") != "PARKED"]
    slots = slots_for(cfg)
    busy = [s for s in items if s.get("state") in ACTIVE_STATES]
    queued = [s for s in items if s.get("state") == "QUEUED"]

    def n_busy() -> int:              # SPEC-0200 slots; in-flight campaigns always count
        return len(slot_ids(busy, flying))

    # 0. campaigns parked on a bad project config resume once it loads (SPEC-0194) — into a
    #    free slot only, so a recovery never runs more campaigns than the profile allows
    for st in store.list_campaigns(cfg, state="PARKED"):
        ctx.progress()
        park = st.get("park") or {}
        if park.get("reason") != "config_invalid" or st["id"] in flying:
            continue
        try:
            ctx.project(st)
            ctx.profile(st)
        except ValueError:      # ProjectConfigError is one; a broken profile keeps it parked too
            continue
        to = park.get("resume_to") or "QUEUED"
        frm = st.get("state")
        _record_queue_decision(cfg, st["id"], "resume", "parked.config_invalid",
                               "project config loads again", action=f"resume to {to}")
        st["park"] = None
        _enter_slot_or_wait(cfg, st, to, "config_recovered", busy=n_busy(), slots=slots)
        report.steps.append({"campaign": st["id"], "from": frm, "to": st["state"],
                             "action": "recover", "reason": "config_recovered",
                             "run_id": None, "cost_usd": None})
        if st["state"] in ACTIVE_STATES:
            busy.append(st)
        elif st["state"] == "QUEUED":
            queued.append(st)

    # 0a. parks the queue decided itself (SPEC-0302): write their `wait` record, resume the due
    #     ones, hand the rest back to the conductor when the feature is off
    for st in _auto_resume_step(cfg, flying, busy_count=n_busy, slots=slots, report=report):
        if st["state"] in ACTIVE_STATES:
            busy.append(st)
        elif st["state"] == "QUEUED":
            queued.append(st)

    # 0b. started campaigns waiting for a slot go first (SPEC-0200): they continue where they
    #     stopped — no re-open — so their branch does not go stale behind new work
    waiting = sorted((s for s in items if s.get("state") == "WAITING"), key=admission_key)
    for st in waiting:
        ctx.progress()
        if not admit or ctx.stop.is_set() or n_busy() >= slots:
            break
        if st.get("park_requested") or st["id"] in flying:
            continue
        to = (st.get("waiting") or {}).get("resume_to") or "IMPLEMENTING"
        st.pop("waiting", None)
        _transition(cfg, st, to, "slot_free")
        report.steps.append({"campaign": st["id"], "from": "WAITING", "to": to,
                             "action": "continue", "reason": "slot_free",
                             "run_id": None, "cost_usd": None})
        busy.append(st)

    resolve_dependency_conflicts(cfg, queued)

    # 1. admission + opening (serial); an item admitted now waits for the next tick
    ready = {}
    for st in queued:
        ctx.progress()
        ready[st["id"]] = backlog_readiness(cfg, st)
        _record_backlog(cfg, st, *ready[st["id"]])
    admitted_ids = set()
    if admit and not ctx.stop.is_set() and events_temp_pressure(cfg):
        report.admission_paused = "temp_pressure"     # in-flight campaigns carry on (SPEC-0253)
        admit = False
    if admit and not ctx.stop.is_set():
        for st in pick_admissions([s for s in queued if s["id"] not in flying], n_busy(),
                                  slots, lambda s: ready[s["id"]]):
            ctx.progress()
            _transition(cfg, st, "OPENING", "admitted")
            report.admitted += 1
            admitted_ids.add(st["id"])
            report.steps.append(_apply(ctx, st, _do_open(ctx, st)))
            if st.get("state") in ACTIVE_STATES:
                busy.append(st)
    waiting_ready = sorted(s["id"] for s in queued if s["id"] not in admitted_ids
                           and s["id"] not in flying and ready[s["id"]][0]
                           and not s.get("park_requested"))
    holder_rows: dict[str, dict] = {}
    if admit and waiting_ready and n_busy() >= slots:       # SPEC-0268: say why they wait
        state_of = {s["id"]: s.get("state") for s in store.list_campaigns(cfg)}   # flights may be parked
        holder_rows = {cid: {"campaign": cid, "state": state_of.get(cid), "deferred": False}
                       for cid in sorted({s["id"] for s in busy} | flying)}
        report.blocked = {"ready": waiting_ready, "slots": slots, "occupied": n_busy(),
                          "holders": list(holder_rows.values())}

    # 2. plan every active item without a step in flight (never a second step: SPEC-0213)
    pending_agent = []       # (state, step, run_id, req, budget, review group) to submit
    groups = []              # reviewer fan-outs, applied once each when harvested (SPEC-0201)
    pending_close = []
    # SPEC-0260: one close worktree phase per repository root; the others wait in FIFO order.
    # The train covers `items` and every state step 0 brought back (those were PARKED when
    # `items` was read); `busy` holds the newest dict of each id.
    close_roots = {cid: _repo_root(fl.state) for cid, fl in pool.flights.items()
                   if fl.tasks and fl.tasks[0][0].kind == "close"}
    members = {s["id"]: s for s in items}
    members.update((s["id"], s) for s in busy)
    trains = close_train(members.values(), set(close_roots))
    train_pos = {cid: (ids[0], i) for ids in trains.values() for i, cid in enumerate(ids)}
    gate_starts = _gate_starts(ctx, pool, busy, admitted_ids, flying)
    exclusive_held = _exclusive_held(ctx, pool, busy)       # SPEC-0507: closes yield to them
    # SPEC-0345: the dispatch gate. Its live count also reads the campaigns with a step in
    # flight that are not active any more (a human parked one mid-run): the run still runs.
    pgate = PG.Gate(cfg, [s["id"] for s in busy] + sorted(flying - {s["id"] for s in busy}))
    ctx._slot_reservations = pgate._slot_reservations
    ctx._pgate = pgate
    for st in busy:
        ctx.progress()
        if st.get("state") not in ACTIVE_STATES or st["id"] in admitted_ids \
                or st["id"] in flying:
            continue
        try:
            profile = ctx.profile(st)
        except ValueError as e:     # SPEC-0263: parks `config_invalid` and recovers, like the project config
            report.steps.append(_apply(ctx, st, StepResult(
                st["id"], Step("open"), st["state"], error=f"profile: {e}", config_error=True)))
            continue
        try:
            pcfg = ctx.project(st)
        except ValueError as e:
            report.steps.append(_apply(ctx, st, StepResult(
                st["id"], Step("open"), st["state"], error=str(e),
                config_error=isinstance(e, ProjectConfigError))))
            continue
        step = plan_step(st, profile, pcfg, **ctx.cost_args(profile))
        if step.kind == "park" and step.reason == "cost_cap":       # SPEC-0422
            step = _cost_policy_step(ctx, st, profile, pcfg, step, report)
            if step is None:
                continue
        if step.kind == "none":
            continue
        if step.kind == "park":
            report.steps.append(_park_row(cfg, st, step))
            continue
        if step.kind == "open":
            ctx.progress()
            report.steps.append(_apply(ctx, st, _do_open(ctx, st)))
            continue
        if draining and (step.kind in ("gate", "close", "resolve", "write_spec", "finalize_split")
                         or not getattr(ctx.runner, "detachable", False)):
            if st["id"] in holder_rows:
                holder_rows[st["id"]]["deferred"] = True        # SPEC-0268
            continue                 # SPEC-0229: a draining tick starts no in-process work
        if step.kind == "close" and exclusive_held and _close_regates(st, pcfg):
            _note_close_deferred(ctx, pool, st, exclusive_held)      # SPEC-0507
            continue
        if step.kind == "close":
            head_id, position = train_pos.get(st["id"], (None, 0))
            if head_id is None:      # not a train member: never close beside a running close
                root = _repo_root(st)
                head_id = next((cid for cid, r in close_roots.items()
                                if root and r == root and cid != st["id"]), st["id"])
                position = int(head_id != st["id"])
            if position:
                _train_wait(cfg, st, head_id, position)
                continue
            _train_head(cfg, st)
            if ctx.stop.is_set():
                continue
            if own:
                pending_close.append(st)
            else:
                pending_agent.append((st, step, None, None, 0.0, None))
            continue
        if step.kind == "gate" and st["id"] not in gate_starts:
            _note_gate_deferred(ctx, pool, st, busy)   # SPEC-0311: retried on a later tick
            continue
        if step.kind in ("gate", "finalize_split"):
            pending_agent.append((st, step, None, None, 0.0, None))
            continue
        if ctx.stop.is_set():
            continue
        if step.kind in PG.GATED_KINDS:
            # SPEC-0345: the rate-limit backoff and the persona cap hold the step pending (it is
            # retried next tick, never run locally instead); a fan-out may be trimmed to fit
            step = pgate.admit(st, step)
            if step is None:
                continue
        if step.kind == "review":
            group = _prepare_reviews(ctx, pgate, st, step, profile, pending_agent, report)
            if group:
                groups.append(group)
            continue
        if step.kind == "spec_review":
            pre = _spec_review_precheck(ctx, st, step)
            if pre is not None:
                report.steps.append(_apply(ctx, st, pre))
                continue
            fault = pgate.backend_fault(st, ("spec_reviewer",), remote_ok=False)
            if fault is not None:
                pgate._hold(st["id"], "backend_unavailable")
                continue
        if step.kind in PG.GATED_KINDS and _hold_local_backend_before_prepare(
                pgate, st, step.persona, step):
            continue
        try:
            run_id, req, budget = _prepare_run(ctx, st, step.persona, step)
        except (ValueError, OSError) as e:
            started = e if isinstance(e, _RunStarted) else None     # run.start is on the ledger
            report.steps.append(_apply(ctx, st, StepResult(
                st["id"], step, st["state"], error=str(e),
                run_id=started.run_id if started else None,
                budget=started.budget if started else 0.0)))
            continue
        pending_agent.append((st, step, run_id, req, budget, None))

    try:
        pgate.flush()
    except (OSError, ValueError) as e:
        journal(cfg, "persona_holds_failed", error=f"{type(e).__name__}: {e}")

    # 3. agent steps: submitted; a single-shot tick waits for its own and applies them now
    _submit(ctx, pool, report, pending_agent, groups)
    try:
        pgate.release_slot_reservations()
    except Exception:                          # noqa: BLE001
        pass
    ctx.progress()
    if not own and not draining:
        try:                                # SPEC-0359: the full suite on base, when due
            _sentinel_step(ctx, pool, busy)
        except Exception as e:              # never let the sentinel cost the tick
            journal(cfg, "sentinel_failed", error=f"{type(e).__name__}: {e}")
    if not draining:
        try:                                # SPEC-0420: red episodes: fix, revert, escalate
            _red_main_step(ctx, pool, report, own=own)
        except Exception as e:              # never let it cost the tick
            journal(cfg, "red_main_failed", error=f"{type(e).__name__}: {e}")
    if own:
        pool.wait_all()
        _harvest(pool, report)

    # 4. single shot: closes one at a time, worktree phase then finish phase (SPEC-0220); a
    #    moved base redoes the close within the tick until the budget is spent; in train order
    #    (SPEC-0260)
    for st in sorted(pending_close, key=_train_key):
        ctx.progress()
        while not ctx.stop.is_set():
            pool.flights[st["id"]] = fl = _Flight(st, ctx)
            fl.tasks.append((Step("close"), pool.submit(_do_prepare_close, ctx, st), None))
            pool.wait_all()
            _harvest(pool, report)
            if report.steps[-1].get("reason") != "base_moved":
                break
            st = store.read_state(cfg, st["id"])
    _record_auto_waits(cfg)          # SPEC-0302: parks written this tick, outside any item lock
    # SPEC-0318: after the step results (the deferred-park branch included), so a deferred park
    # gets its record in the tick that applies it
    try:
        settle_decision_requests(cfg)
    except Exception as e:           # never let the repair step cost the tick
        journal(cfg, "decision_record_failed", error=str(e))


# ---- adoption of runs left open by an earlier runner (SPEC-0225) -------------------------

_ADOPT_GRACE_S = 10.0              # a wrapper that was just started may still claim its run dir
_LEGACY_EXTRA_S = 60.0             # slack after `timeout_s` before a pre-wrapper run is closed
_HUMAN_EVENTS = frozenset({"transition", "park", "resume", "resumed", "abandon", "abandoned"})
_STATE_OF_KIND = {"implement": "IMPLEMENTING", "triage": "TRIAGING", "review": "REVIEWING",
                  "divergence": "DIVERGENCE_TRIAGE"}


def _open_runs(cfg, cid) -> list:
    """Every `run.start` with no `run.end` for its run id, in ledger order."""
    starts, ended = {}, set()
    for ev in store.read_ledger(cfg, cid, types=["run.start", "run.end"]):
        if ev["type"] == "run.start":
            starts[ev.get("run_id")] = ev
        else:
            ended.add(ev.get("run_id"))
    return [e for rid, e in starts.items() if rid not in ended]


def _adopt_class(ev: dict):
    """(kind, personas) of an open run, from its recorded `step`, else the registry; None for
    a persona nobody knows. Personas None means the campaign's reviewer group."""
    step = ev.get("step")
    if isinstance(step, dict) and step.get("kind"):
        return step["kind"], tuple(step.get("personas") or ()) or None
    entry = PS.ADOPT_REGISTRY.get(ev.get("persona"))
    return (entry[0], entry[1] or None) if entry else None


def _adopt_request(rd: Path, ev: dict):
    req = rebuild_request(rd, budget=ev.get("max_budget_usd"))
    if (rd / "request.json").is_file():
        return req
    import dataclasses
    persona = PS.PERSONAS.get(ev.get("persona"))
    return dataclasses.replace(
        req, persona=ev.get("persona") or req.persona, model=ev.get("model") or "",
        effort=ev.get("effort"), timeout_s=int(ev.get("timeout_s") or 0),
        permission_mode=ev.get("permission_mode") or "",
        json_schema=persona.schema if persona else {})


def _close_stale(cfg, cid: str, ev: dict, ctx: _Ctx) -> str:
    """Close an open run that is not the campaign's latest step (a crash left it behind, the
    loop moved on): `run.end` + cost, no transition. A finished or vanished run keeps its own
    result; anything else is interrupted (a live process is sent TERM, a legacy run with no
    process record is just closed). Returns the status for the `adopt` record."""
    from .runner import _synthetic, collect
    rid, persona = ev["run_id"], ev["persona"]
    rd = store.runs_dir(cfg, cid) / rid
    label = RP.run_status(rd)
    req = _adopt_request(rd, ev)
    budget = float(ev.get("max_budget_usd") or req.max_budget_usd or 0.0)
    if label in ("finished", "vanished"):
        rr = collect(req)
    else:
        if label in ("running", "orphan_group"):
            RP.terminate_run(rd)
        elif label == "unclaimed" and (rd / "launch.json").exists():
            RP.claim(rd, {"orphaned": True, "by": "adopt", "ts": store.now_iso()})
        rr = _synthetic(req, f"stale run ({label}): a newer step superseded it")
    with store.item_lock(cfg, cid):
        current = store.read_state(cfg, cid)
        events = _record_run(current, persona, rid, rr, budget, interrupted=rr.interrupted)
        events.append(("note", {"text": f"closed stale open run {rid} ({persona}, {label}); "
                                        "no transition", "author": "queue"}))
        for etype, payload in events:
            store.append_event(cfg, cid, etype, payload)
        store.write_state(cfg, cid, current)
    return f"stale_{label}"


def _adopted_held(ctx: _Ctx, snap: dict, personas: tuple) -> tuple:
    """SPEC-0345: the reviewers an adopted fan-out's members leave out while `max_personas` is
    set (the cap trimmed it): pending reviewers without a valid verdict. They were held, not
    failed, so applying the adopted members charges them no retry. () without a cap."""
    if PG.max_personas(ctx.cfg) is None or "review_triager" in personas \
            or PK.MARSHAL in personas or CO.NAME in personas:
        return ()
    try:
        prof = ctx.profile(snap)
    except (ValueError, OSError):
        return ()
    valid = _valid_verdicts(snap, prof)
    return tuple(p for p in reviewers_of(prof) if p not in personas and p not in valid)


def _adopt(cfg, ctx: _Ctx, pool: StepPool, report: TickReport) -> None:
    """First tick of a pool: every open run in the campaign ledgers becomes a flight in phase
    `wait`, handled by its files (`ClaudeRunner.run` attaches). Finished runs are applied by the
    harvest that follows; live ones are waited for. Terminal campaigns other than
    MERGED / HANDED_OFF (ABANDONED) are adopted too: their run is recorded, no transition."""
    from .runner import _epoch
    pool.adopted = True
    try:
        detachable = bool(getattr(ctx.runner, "detachable", False))
    except ValueError:
        return
    # SPEC-0344: a remote persona job is adoptable whatever the local runner is
    remote_only = not detachable
    if remote_only and not RPS.any_live(cfg):
        return
    lease_started = (L.read_lease(cfg) or {}).get("started_at")
    now = time.time()
    rows = []
    for st in store.list_campaigns(cfg, include_terminal=True):
        cid = st["id"]
        if st.get("state") in ("MERGED", "HANDED_OFF") or cid in pool.flights:
            continue
        opens = _open_runs(cfg, cid)
        if not opens:
            continue
        last = opens[-1]
        if remote_only and RPS.live_record(cfg, cid, last.get("run_id")) is None:
            continue
        cls = _adopt_class(last)
        if cls and cls[0] == "review":              # the open runs of the latest review round
            key = cls[1]
            members = []                             # the trailing block: stops at a repeat
            for e in reversed(opens):
                c = _adopt_class(e)
                if not (c and c[0] == "review" and c[1] == key) \
                        or any(m["persona"] == e["persona"] for m in members):
                    break
                members.append(e)
            order = key or tuple(p for p in PS.REVIEWER_PERSONAS)
            members.sort(key=lambda e: order.index(e["persona"]) if e["persona"] in order
                         else len(order))
        else:
            members = [last]
        for e in opens:                              # everything else is stale: close it
            if not any(e is m for m in members):
                rows.append({"campaign": cid, "run_id": e["run_id"], "persona": e["persona"],
                             "status": _close_stale(cfg, cid, e, ctx)})
        snap = store.read_state(cfg, cid)
        start_state = members[0].get("state") or (
            _STATE_OF_KIND.get(cls[0]) if cls else snap.get("state"))
        first_seq = min(e.get("seq") or 0 for e in members)
        if snap.get("state") != start_state or any(
                e["type"] in _HUMAN_EVENTS and e.get("by") != "tick"
                and not (e["type"] == "transition" and e.get("from") == e.get("to"))
                and (e.get("seq") or 0) > first_seq for e in store.read_ledger(cfg, cid)):
            snap["_superseded"] = True
        snap["state"] = start_state
        fl = pool.flights[cid] = _Flight(snap, ctx)
        group = None
        if cls and cls[0] == "review":
            personas = tuple(e["persona"] for e in members)
            group = fl.group = _ReviewGroup(snap, Step(
                "review", persona=personas[0], personas=personas,
                held=_adopted_held(ctx, snap, personas)))
        for ev in members:
            rid, persona = ev["run_id"], ev["persona"]
            rd = store.runs_dir(cfg, cid) / rid
            req = _adopt_request(rd, ev)
            budget = float(ev.get("max_budget_usd") or req.max_budget_usd or 0.0)
            hooks = pool.hooks(rd, phase="wait")
            label = RP.run_status(rd)
            if label == "unclaimed":
                legacy = not (rd / "launch.json").exists() and not ev.get("step")
                if legacy:
                    hooks.adopt = ("legacy", _epoch(ev["ts"]) + req.timeout_s + _LEGACY_EXTRA_S)
                    label = "legacy"
                else:
                    try:
                        anchor = (rd / "launch.json").stat().st_mtime
                    except OSError:
                        anchor = _epoch(ev["ts"])
                    hooks.adopt = ("claim", anchor + _ADOPT_GRACE_S)
                    label = "orphaned" if now >= hooks.adopt[1] else "running"
            ex = RP.read_json(rd / "exit.json") or {}
            adopted = {"exited_before_start": bool(
                ex.get("ended_at") and lease_started
                and _epoch(ex["ended_at"]) < _epoch(lease_started))}
            if cls is None:
                step, label = Step("unknown", persona=persona), "unknown"
            elif group is not None:
                step = Step("review", persona=persona, personas=group.step.personas)
                group.meta[persona] = (rid, budget)
            else:
                step = Step(cls[0], persona=persona)
            if step.kind == "write_spec":
                # SPEC-0441: in-process like `_submit`'s; `_do_write_spec` fills `res.write`,
                # which `_do_persona` never does (it would kill the runner at harvest)
                fl.tasks.append((step, pool.submit(_do_write_spec, ctx, snap, step, rid, req,
                                                   budget), None))
            else:
                fl.tasks.append((step, pool.submit(_do_persona, ctx, snap, step, rid, req,
                                                   budget, hooks, adopted), hooks))
            fl.runs.append((rid, req.timeout_s))
            rows.append({"campaign": cid, "run_id": rid, "persona": persona, "status": label})
        report.started.append({"campaign": cid, "action": "adopt", "state": start_state,
                               "run_ids": [e["run_id"] for e in members]})
    if rows:
        journal(cfg, "adopt", runs=rows)


def _prepare_reviews(ctx: _Ctx, pgate: PG.Gate, st: dict, step: Step, profile: dict,
                     pending_agent: list, report: TickReport):
    """Prepare one run per pending reviewer, in profile order (run ids are allocated before any
    task starts). A failure with one reviewer configured is the campaign-wide error of
    SPEC-0187; with several it is that reviewer's missing result and its siblings still run.
    Returns the `_ReviewGroup` to apply when its flight is harvested, or None."""
    # the triager (SPEC-0206) never takes the one-reviewer campaign-wide error path
    multi = len(reviewers_of(profile)) > 1 or step.persona in ("review_triager", PK.MARSHAL,
                                                                 CO.NAME)
    if step.persona == PK.MARSHAL:                                    # SPEC-0554
        return _prepare_marshal(ctx, pgate, st, step, profile, pending_agent, report)
    if step.persona == CO.NAME:                                       # SPEC-0555
        return _prepare_collator(ctx, st, step, pending_agent, report)
    for persona in step.personas:
        sub = Step("review", persona=persona, personas=step.personas)
        if _hold_local_backend_before_prepare(pgate, st, persona, sub):
            pgate.release_campaign_reservations(st["id"], step.personas)
            pgate.release_admit_charge(st["id"])
            pgate._hold(st["id"], "backend_unavailable")
            return None
    group = _ReviewGroup(st, step)
    for persona in step.personas:
        if ctx.stop.is_set():
            break
        sub = Step("review", persona=persona, personas=step.personas)
        try:
            run_id, req, budget = _prepare_run(ctx, st, persona, step)
        except (ValueError, OSError) as e:
            if not multi:
                report.steps.append(_apply(ctx, st, StepResult(
                    st["id"], step, st["state"], error=str(e))))
                return None
            if isinstance(e, _RunStarted):          # run.start is written: it needs a run.end
                report.errors.append(f"{st['id']}: {e!r}")
                group.results[persona] = StepResult(
                    st["id"], sub, st["state"], run_id=e.run_id, budget=e.budget,
                    run_result=_synthetic_failure(repr(e)))
            else:
                group.results[persona] = StepResult(
                    st["id"], sub, st["state"], error=str(e),
                    config_error=isinstance(e, ProjectConfigError))
            continue
        group.meta[persona] = (run_id, budget)
        pending_agent.append((st, sub, run_id, req, budget, group))
    return group


def _prepare_marshal(ctx: _Ctx, pgate: PG.Gate, st: dict, step: Step, profile: dict,
                     pending_agent: list, report: TickReport):
    """SPEC-0554: the packet pre-step of a review loop. Build this loop's packet (code), then
    prepare the `review_marshal` run that adds the AC map. A packet that cannot be built, a
    marshal the cost cap blocks or a marshal run that cannot be prepared is recorded in
    `state.packet` (a `review_packet` event) and the reviewers run next tick: never a park."""
    cfg, cid = ctx.cfg, st["id"]
    cur = PK.current(st)
    events = []
    if cur is None:
        try:
            cur = PK.build(cfg, st)
        except Exception as e:                  # noqa: BLE001 — falls back to today's prompt
            cur = PK.failed(st, f"{type(e).__name__}: {e}")
        st["packet"] = cur
        events.append(("review_packet", {"loop": cur["loop"], "ok": cur["ok"],
                                         "mode": cur.get("mode"), "bytes": cur.get("bytes"),
                                         **({"error": cur["error"]} if cur.get("error") else {})}))
    skip = None
    if cur.get("ok") and cur.get("marshal") == "pending":
        blocked, _budget = _cap_blocks(st, profile, PK.MARSHAL)
        if blocked:
            skip = f"cost cap: {blocked}"
        else:
            try:
                run_id, req, budget = _prepare_run(ctx, st, PK.MARSHAL, step)
            except _RunStarted as e:
                report.errors.append(f"{cid}: {e!r}")
                group = _ReviewGroup(st, step)
                group.meta[PK.MARSHAL] = (e.run_id, e.budget)
                group.results[PK.MARSHAL] = StepResult(
                    cid, Step("review", persona=PK.MARSHAL, personas=step.personas),
                    st["state"], run_id=e.run_id, budget=e.budget,
                    run_result=_synthetic_failure(repr(e)))
                return group
            except (ValueError, OSError) as e:
                skip = f"marshal not started: {e}"
            else:
                with store.item_lock(cfg, cid):
                    for etype, payload in events:
                        store.append_event(cfg, cid, etype, payload)
                group = _ReviewGroup(st, step)
                group.meta[PK.MARSHAL] = (run_id, budget)
                sub = Step("review", persona=PK.MARSHAL, personas=step.personas)
                pending_agent.append((st, sub, run_id, req, budget, group))
                return group
    if skip is not None:
        events.append(("review_packet", PK.apply_marshal(cfg, st, None, error=skip)))
    with store.item_lock(cfg, cid):
        for etype, payload in events:
            store.append_event(cfg, cid, etype, payload)
        store.write_state(cfg, cid, st)
    pgate.release_campaign_reservations(cid, step.personas)
    pgate.release_admit_charge(cid)
    return None


def _apply_marshal(ctx, state, counters, res, row, events, finish):
    """SPEC-0554: record the marshal run and write its AC map into the packet. A failed,
    malformed or over-budget run leaves `ac_map: null` + `ac_map_error`; the reviewers run
    either way. Interrupted: state unchanged but the run, so the next tick runs it again."""
    cfg, cid = ctx.cfg, state["id"]
    r = res.reviews[0]
    events += r.events_before
    rr = r.run_result
    if rr is not None:
        events += _record_run(state, PK.MARSHAL, r.run_id, rr, r.budget,
                              interrupted=r.interrupted)
        row["run_id"] = r.run_id
        row["cost_usd"] = rr.cost_usd if rr.cost_usd is not None else r.budget
        row["cost_estimated"] = rr.cost_usd is None
        if r.interrupted or rr.interrupted:
            with store.item_lock(cfg, cid):
                for etype, payload in events:
                    store.append_event(cfg, cid, etype, payload)
                store.write_state(cfg, cid, state)
            row["to"], row["reason"] = res.from_state, "interrupted"
            return row
    verdict = rr.verdict if rr is not None and rr.verdict_ok else None
    why = None
    if not isinstance(verdict, dict):
        why = (f"marshal run not started: {r.error}" if rr is None else
               "marshal timed out" if rr.timed_out else
               "marshal run failed" if rr.is_error else "marshal returned a malformed verdict")
    events.append(("review_packet", {**PK.apply_marshal(cfg, state, verdict, error=why),
                                     "run_id": r.run_id}))
    return finish("REVIEWING", "review_packet")


def _prepare_collator(ctx: _Ctx, st: dict, step: Step, pending_agent: list,
                      report: TickReport):
    """SPEC-0555: prepare the `findings_collator` run of this verdict set. A run that cannot be
    prepared is a missing result: `_apply_collator` then falls back to today's routing."""
    cid = st["id"]
    group = _ReviewGroup(st, step)
    sub = Step("review", persona=CO.NAME, personas=step.personas)
    try:
        run_id, req, budget = _prepare_run(ctx, st, CO.NAME, step)
    except _RunStarted as e:
        report.errors.append(f"{cid}: {e!r}")
        group.meta[CO.NAME] = (e.run_id, e.budget)
        group.results[CO.NAME] = StepResult(cid, sub, st["state"], run_id=e.run_id,
                                            budget=e.budget,
                                            run_result=_synthetic_failure(repr(e)))
        return group
    except (ValueError, OSError) as e:
        group.results[CO.NAME] = StepResult(cid, sub, st["state"], error=str(e))
        return group
    group.meta[CO.NAME] = (run_id, budget)
    pending_agent.append((st, sub, run_id, req, budget, group))
    return group


def _start_collation(ctx, state, profile, valid, events, finish):
    """SPEC-0555: a complete verdict set without a collation. No finding: nothing to collate
    (`skipped`, the file is still written). The cost cap blocks the collator: fall back. Else
    the collation becomes pending and the next review step runs the collator. Returns the
    finish row, or None when routing goes on now."""
    if not CO.needs_start(state, valid):
        return None
    lr = state["last_review"]
    items = CO.inputs(valid)
    loop = PK.loop_of(state)
    if not items:
        try:
            CO.write(ctx.cfg, state, CO.document(state, loop, "skipped", [], []))
        except OSError:
            pass
        lr["collation"] = {"runs": CO.signature(valid), "loop": loop, "status": "skipped",
                           "scopes": {}}
        events.append(("findings_collated", {"loop": loop, "status": "skipped", "findings": 0,
                                             "out": 0, "scopes": []}))
        return None
    blocked = _cap_blocks(state, profile, [CO.NAME])[0]
    if blocked:
        _collation_failed(ctx.cfg, state, valid, events, f"cost cap: {blocked}")
        return None
    lr["collation"] = {"runs": CO.signature(valid), "loop": loop, "status": "pending"}
    return finish("REVIEWING", "review_collate")


def _apply_collation(state, valid) -> dict:
    """SPEC-0555: the verdicts `decide_reviews` routes: marked `scope` / `tie` by this verdict
    set's applied collation (out-of-scope findings are then never scored), else unchanged
    (pending never reaches here; failed / skipped route every finding, as today)."""
    try:
        return CO.scoped(state, valid)
    except Exception:                    # noqa: BLE001 — a bad collation is today's routing
        return valid


def _collation_failed(cfg, state, valid, events, error, run_id=None) -> None:
    """SPEC-0555: today's routing for this verdict set (every finding in scope), recorded in
    `findings-loop-N.json`, `last_review.collation` and a `collator_failed` event."""
    loop = PK.loop_of(state)
    items = CO.inputs(valid)
    try:
        CO.write(cfg, state, CO.document(state, loop, "failed", CO.fallback_records(items),
                                         items, run_id=run_id, error=error))
    except (OSError, ValueError, TypeError):
        pass
    state["last_review"]["collation"] = {"runs": CO.signature(valid), "loop": loop,
                                         "status": "failed", "error": str(error)[:400],
                                         **({"run": run_id} if run_id else {})}
    events.append(("collator_failed", {"loop": loop, "error": str(error)[:400],
                                       "run_id": run_id}))
    events.append(("note", {"text": f"collator failed ({str(error)[:200]}); every finding "
                                    f"stays in scope this loop", "author": "queue"}))


def _collation_done(ctx, state, profile, valid, verdict, run_id, events) -> None:
    """SPEC-0555: apply the collator's verdict with the code guards, write
    `findings-loop-N.json`, record the scopes in `last_review.collation` and file the
    out-of-scope findings into the campaign's grouped follow-up idea. Raises on a bad verdict
    (the caller falls back)."""
    cfg = ctx.cfg
    loop = PK.loop_of(state)
    items = CO.inputs(valid)
    changed = CO.diff_files(cfg, state)
    records, stats = CO.collate(
        verdict, items, changed=changed, unmet=CO.unmet_acs(valid),
        ac_ids={a["id"] for a in CO.acs_of(cfg, state)},
        worktree=(state.get("repo") or {}).get("worktree"))
    CO.write(cfg, state, CO.document(state, loop, "done", records, items, stats=stats,
                                     run_id=run_id))
    scopes = {i: list(v) for i, v in CO.scope_map(records).items()}
    n_out = sum(1 for r in records if r["scope"] == "out")
    state["last_review"]["collation"] = {"runs": CO.signature(valid), "loop": loop,
                                         "status": "done", "run": run_id, "scopes": scopes,
                                         "out": n_out, "stats": stats}
    spec_id = state.get("spec_id")
    by_id = {f["id"]: f for f in items}
    events.append(("findings_collated", {
        "loop": loop, "status": "done", "run_id": run_id, "findings": len(items),
        "collated": len(records), "out": n_out, "stats": stats,
        "scopes": [{"id": i, "key": FU.finding_key(spec_id, by_id[i]["title"],
                                                   by_id[i]["files"]),
                    "persona": by_id[i]["persona"], "scope": s, "tie": t}
                   for i, (s, t) in scopes.items() if i in by_id]}))
    entries = CO.followup_entries(spec_id, state["id"], loop, records, items)
    if entries:
        fevents, summary = FU.file_out_of_scope(cfg, state, profile, entries)
        events += fevents
        events.append(("note", {"text": f"out of scope: {n_out} finding(s); follow-ups filed "
                                        f"{len(summary['filed'])}, capped "
                                        f"{len(summary['capped'])}, unfiled "
                                        f"{len(summary['unfiled'])}", "author": "queue"}))


def _apply_collator(ctx, state, profile, counters, res, row, events, finish):
    """SPEC-0555: record the collator run, apply its collation (or fall back to today's routing
    on a failed, malformed or unusable run) and route the review. Interrupted: the run is
    recorded, the collation stays pending and the next tick runs it again."""
    cfg, cid = ctx.cfg, state["id"]
    r = res.reviews[0]
    events += r.events_before
    rr = r.run_result
    if rr is not None:
        events += _record_run(state, CO.NAME, r.run_id, rr, r.budget,
                              interrupted=r.interrupted)
        row["run_id"] = r.run_id
        row["cost_usd"] = rr.cost_usd if rr.cost_usd is not None else r.budget
        row["cost_estimated"] = rr.cost_usd is None
        if r.interrupted or rr.interrupted:
            with store.item_lock(cfg, cid):
                for etype, payload in events:
                    store.append_event(cfg, cid, etype, payload)
                store.write_state(cfg, cid, state)
            row["to"], row["reason"] = res.from_state, "interrupted"
            return row
    valid = _valid_verdicts(state, profile)
    verdict = rr.verdict if rr is not None and rr.verdict_ok else None
    why = None
    if not isinstance(verdict, dict):
        why = (f"collator run not started: {r.error}" if rr is None else
               "collator timed out" if rr.timed_out else
               "collator run failed" if rr.is_error else "collator returned a malformed verdict")
    else:
        try:
            _collation_done(ctx, state, profile, valid, verdict, r.run_id, events)
        except Exception as e:           # noqa: BLE001 — never a park: today's routing
            why = f"collation failed: {type(e).__name__}: {e}"
    if why is not None:
        _collation_failed(cfg, state, valid, events, why, r.run_id)
    return _route_reviews(ctx, state, profile, counters, row, events, finish)


def _finish_reviews(ctx: _Ctx, report: TickReport, group: _ReviewGroup) -> dict:
    """Apply a reviewer fan-out once, results in profile order. One reviewer: its bare result
    (or the campaign-wide error of a raising worker), as SPEC-0187. Several: a worker that
    raised or errored after `run.start` becomes a synthetic failed run (a `run.end` and the
    budget charge), so the reviewer is simply missing."""
    st = group.state
    multi = len(reviewers_of(ctx.profile(st))) > 1 \
        or group.step.persona in ("review_triager", PK.MARSHAL, CO.NAME)
    ordered = []
    for persona in group.step.personas:
        item = group.results.get(persona)
        if item is None:                 # never prepared (stop set between two prepares)
            continue
        if not multi:
            if isinstance(item, Exception):
                report.errors.append(f"{st['id']}: {item!r}")
                item = StepResult(st["id"], Step("implement"), st["state"], error=repr(item))
            return _apply(ctx, st, item)
        run_id, budget = group.meta.get(persona, (None, 0.0))
        if isinstance(item, Exception) or (item.error and item.run_result is None
                                           and run_id is not None):
            err = repr(item) if isinstance(item, Exception) else item.error
            report.errors.append(f"{st['id']}: {persona}: {err}")
            item = StepResult(st["id"], Step("review", persona=persona), st["state"],
                              run_id=run_id, budget=budget,
                              run_result=_synthetic_failure(err))
        ordered.append(item)
    return _apply(ctx, st, StepResult(st["id"], group.step, st["state"], reviews=ordered))


# ---- the cost-cap policy (SPEC-0422) ---------------------------------------------------------

_COST_RAISE_LOOPS = 8          # raises in one tick before a still-blocked step parks


def _cost_phantom_first(cfg, state: dict, budget) -> bool:
    """SPEC-0302 keeps precedence: True when `autopark` would decide this cost park itself
    (the block is only the phantom charge of transient-failed runs)."""
    import datetime as dt
    try:
        sett = AP.settings(cfg)
        if not sett["enabled"]:
            return False
        probe = {**state, "park": {"reason": "cost_cap"}}
        led = store.read_ledger(cfg, state["id"], types=["open", "resume", "run.end"])
        return AP.decide(probe, led, sett, dt.datetime.now().astimezone(),
                         budget=budget) is not None
    except Exception:                     # noqa: BLE001 — `_park` reports its own failure
        return False


def _cost_decide(cfg, state: dict, profile: dict, budget):
    """`(cost_policy.decide(...), ledger)`; a failure is `off` (today's park), journaled."""
    try:
        ledger = store.read_ledger(cfg, state["id"], types=CP.LEDGER_TYPES)
        return CP.decide(state, profile, ledger, cfg, blocked_budget=budget), ledger
    except Exception as e:                # noqa: BLE001 — the policy never blocks the park
        journal(cfg, "cost_policy_failed", campaign=state["id"], error=repr(e))
        return CP.Decision("off", why=f"cost policy failed: {e!r}"), []


def _cap_raised_event(decision) -> tuple:
    f = decision.facts
    return ("cap_raised", {"from": f.get("cap_usd"), "to": decision.new_cap,
                           "ceiling": f.get("ceiling_usd"), "base_cap": f.get("base_cap_usd"),
                           "spent": f.get("spent_usd"), "why": decision.why, "rule": CP.RULE})


def _cost_bumps(decision) -> list:
    return [CP.RAISES] + ([CP.RAISES_NO_REVIEW]
                          if decision.facts.get("converging") is None else [])


def _cost_raise_in_place(cfg, state: dict, decision) -> bool:
    """Raise the cap of an active campaign (not in flight) under its lock: `cost.cap_usd`, the
    policy counters, a `cap_raised` ledger event; then the `by: queue` decision record. `state`
    (the tick's dict) gets the new cost and counters. False when the state moved meanwhile."""
    cid = state["id"]
    with store.item_lock(cfg, cid):
        cur = store.read_state(cfg, cid)
        if cur.get("state") != state.get("state") or cur.get("park_requested") \
                or (cur.get("cost") or {}).get("cap_usd") != (state.get("cost") or {}).get("cap_usd"):
            return False
        _cost(cur)["cap_usd"] = float(decision.new_cap)
        for name in _cost_bumps(decision):
            _counters(cur)[name] = int(_counters(cur).get(name) or 0) + 1
        etype, payload = _cap_raised_event(decision)
        store.append_event(cfg, cid, etype, payload, by="queue")
        store.write_state(cfg, cid, cur)
    state["cost"], state["counters"] = cur["cost"], cur["counters"]
    _record_queue_decision(cfg, cid, "resume", CP.RULE, decision.why,
                           action=f"raise cap to ${decision.new_cap:.2f}")
    return True


def _cost_escalated_before(cfg, cid, decision) -> bool:
    """True when a cost escalation was already filed for this campaign in this situation
    (the same spend and cap): the queue files at most one per situation."""
    from . import escalations as ESC
    mark = CP.marker(decision.facts.get("spent_usd"), decision.facts.get("cap_usd"))
    return any(r.get("campaign") == cid and r.get("kind") == "cost"
               and mark in (r.get("facts") or []) for r in ESC.list_all(cfg))


def _cost_escalate(cfg, cid, decision, ledger) -> dict | None:
    """File the policy's human-only escalation (`kind: cost`, no recommendation; `topic` once
    `escalations.plan_escalate` takes one, SPEC-0425), at most once per situation. Never
    called with an item lock held. A failure is journaled; the campaign stays parked."""
    import inspect
    from . import escalations as ESC
    try:
        if ESC.open_for(cfg, cid) is not None or _cost_escalated_before(cfg, cid, decision):
            return None
        st = store.read_state(cfg, cid)
        question, options, facts = CP.escalation_text(st, decision, CP.review_outcomes(ledger),
                                                      CP.settings(cfg))
        kw = {"topic": decision.topic} \
            if "topic" in inspect.signature(ESC.plan_escalate).parameters else {}
        plan = ESC.plan_escalate(cfg, cid, question=question, options=options, recommend=None,
                                 facts=facts, frm="conductor", kind="cost", **kw)
        rec = ESC.apply_escalate(cfg, plan)
    except (ValueError, OSError) as e:
        journal(cfg, "cost_escalation_failed", campaign=cid, error=str(e))
        return None
    _record_queue_decision(cfg, cid, "escalate", CP.RULE, decision.why,
                           action=f"escalate {rec['id']} ({decision.topic})")
    return rec


def _cost_policy_step(ctx, st: dict, profile: dict, pcfg, step: Step, report) -> Step | None:
    """Tick step 2, a `cost_cap` park planned for an active campaign: the phantom-charge rule
    (SPEC-0302) and an open escalation keep today's park; a `raise` raises the cap and
    re-plans in the same tick; an `escalate` parks and files the escalation (returns None)."""
    cfg = ctx.cfg
    from . import escalations as ESC
    for _ in range(_COST_RAISE_LOOPS):
        if step.kind != "park" or step.reason != "cost_cap" or not CP.settings(cfg)["enabled"]:
            return step
        if ESC.open_for(cfg, st["id"]) is not None or _cost_phantom_first(cfg, st, step.budget):
            return step
        decision, ledger = _cost_decide(cfg, st, profile, step.budget)
        if decision.action == "raise":
            frm = (st.get("cost") or {}).get("cap_usd")
            if not _cost_raise_in_place(cfg, st, decision):
                return step
            report.steps.append({"campaign": st["id"], "from": st.get("state"),
                                 "to": st.get("state"), "action": "cap_raised",
                                 "reason": CP.RULE, "run_id": None, "cost_usd": None,
                                 "cap_from": frm, "cap_to": decision.new_cap})
            step = plan_step(st, profile, pcfg, **ctx.cost_args(profile))
            continue
        if decision.action == "escalate":
            report.steps.append(_park_row(cfg, st, step))
            _cost_escalate(cfg, st["id"], decision, ledger)
            return None
        return step
    return step


def _cost_policy_parked(cfg, flying, *, busy_count, slots, report) -> list:
    """Tick step 0a (SPEC-0422): each PARKED `cost_cap` campaign with no `park.auto` and no
    open escalation is planned as if resumed. Not cost-blocked under the new reserve: resumed
    as is. Blocked: `decide` — `raise` resumes it with the raised cap, `escalate` files the
    escalation once. Every resume carries a `by: queue` decision record."""
    sett = CP.settings(cfg)
    if not sett["enabled"]:
        return []
    from . import escalations as ESC
    resumed, stats = [], None
    for st in store.list_campaigns(cfg, state="PARKED"):
        cid, park = st["id"], st.get("park") or {}
        if park.get("reason") != "cost_cap" or isinstance(park.get("auto"), dict) \
                or cid in flying or ESC.open_for(cfg, cid) is not None:
            continue
        try:
            profile = resolve_profile(cfg, st.get("profile"),
                                      project_root=(st.get("repo") or {}).get("root"))
            pcfg = P.project_config(cfg, st.get("project"))
        except ValueError:
            continue                    # a broken profile / config: the park stays the conductor's
        if stats is None:
            try:
                stats = CP.refresh(cfg)
            except (OSError, ValueError):
                stats = {}
        to = park.get("resume_to") or "IMPLEMENTING"
        step = plan_step({**st, "state": to, "park": None}, profile, pcfg,
                         reserves=CP.reserves(cfg, profile, stats=stats, sett=sett),
                         ceiling=CP.ceiling(profile, sett))
        new_cap, bumps, extra = None, [], []
        if step.kind == "park" and step.reason == "cost_cap":
            decision, ledger = _cost_decide(cfg, st, profile, step.budget)
            if decision.action == "escalate":
                _cost_escalate(cfg, cid, decision, ledger)
                continue
            if decision.action != "raise":
                continue
            new_cap, bumps, extra = decision.new_cap, _cost_bumps(decision), [_cap_raised_event(decision)]
            rationale, action = decision.why, f"resume to {to} raise cap to ${new_cap:.2f}"
        elif step.kind == "park":
            continue
        else:
            cap = float((st.get("cost") or {}).get("cap_usd") or 0.0)
            rationale = (f"the observed-cost reserve fits under the ${cap:.2f} cap now "
                         f"(next step: {step.kind}); resume without a raise")
            action = f"resume to {to}"
        try:
            plan = plan_resume(cfg, cid, by="queue", raise_cap=new_cap,
                               event_note=f"queue: {CP.RULE}")
        except ValueError:
            continue
        plan.update(bump_counters=bumps, extra_events=extra)
        if not _record_queue_decision(cfg, cid, "resume", CP.RULE, rationale, action=action):
            continue
        try:
            after = apply_resume(cfg, plan, busy=busy_count() + sum(
                1 for r in resumed if r["state"] in SLOT_STATES), slots=slots)
        except ValueError as e:
            journal(cfg, "cost_policy_resume_failed", campaign=cid, error=str(e))
            continue
        report.steps.append({"campaign": cid, "from": "PARKED", "to": after["state"],
                             "action": "auto_resume", "reason": CP.RULE,
                             "run_id": None, "cost_usd": None})
        resumed.append(after)
    return resumed


def _park_row(cfg, state: dict, step: Step) -> dict:
    frm = state.get("state")
    by = step.by or ("human" if step.reason == "human" else "tick")
    extra = {"escalated_from": None} if step.reason == "human_decision" else None
    _park(cfg, state, step.reason, step.detail or step.reason, step.resume_to or frm, by=by,
          extra=extra, budget=step.budget)
    return {"campaign": state["id"], "from": frm, "to": "PARKED", "action": "park",
            "run_id": None, "cost_usd": None, "reason": step.reason}


def runner_journal_path(cfg) -> Path:
    return store.agentic_dir(cfg) / "runner.jsonl"


def journal(cfg, kind: str, **fields) -> dict:
    """Append one `nw.runner.record.v1` line to `<agentic>/runner.jsonl` (SPEC-0192): a single
    O_APPEND write, so concurrent writers never interleave. Never raises."""
    import json
    rec = {"schema": SC.RUNNER_SCHEMA, "ts": store.now_iso(), "kind": kind, "pid": os.getpid(),
           **fields}
    try:
        path = runner_journal_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (json.dumps(rec, ensure_ascii=False, default=str) + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        pass
    return rec


def read_journal(cfg, *, tail=None) -> list[dict]:
    """`runner.jsonl` records in order (unparseable lines skipped)."""
    import json
    path = runner_journal_path(cfg)
    if not path.is_file():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out[-tail:] if tail else out


def journal_tail_for_pid(cfg, pid, max_bytes: int = 262144) -> dict | None:
    """The newest `runner.jsonl` record written by `pid` within the last `max_bytes` of the
    file (SPEC-0228), else None. A bounded backwards read — never the whole journal; the
    partial first line of the window and unparseable lines are skipped."""
    path = runner_journal_path(cfg)
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read()
    except OSError:
        return None
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]                           # the window began mid-line
    for raw in reversed(lines):
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("pid") == pid:
            return rec
    return None


# ---- code-change re-exec (SPEC-0194) ------------------------------------------------------------
# The runner imports nightwright from a checkout (an editable install of main). When a merge changes
# that code, a long-lived loop would keep running the old code — and reject config the new
# code accepts. The loop checks the checkout's HEAD between ticks; on a change it stops
# submitting, drains the in-flight steps (SPEC-0213), and re-executes itself, handing its lease
# over so the queue is never unowned.

_EXEC = os.execve                      # tests replace this


class _heartbeat_while:
    """Heartbeat the lease from a daemon thread (SPEC-0204): a tick that runs a close's gates,
    or the drain of long persona runs, can outlast the lease TTL, and an alive runner must
    never look stale. `run()` holds it for the loop's whole life (SPEC-0213). Beats every
    ttl/4 seconds (at least 1s)."""

    def __init__(self, lease, interval_s=None):
        self.lease = lease
        ttl = float((lease.info or {}).get("ttl_s") or 120)
        self.interval = interval_s if interval_s is not None else max(1.0, ttl / 4)
        self._stop = threading.Event()
        self._thread = None

    def _beat(self):
        last_logged = None
        while not self._stop.wait(self.interval):
            try:
                self.lease.heartbeat()
            except Exception as e:             # SPEC-0224: this thread never dies silently
                now = time.monotonic()
                if last_logged is None or now - last_logged >= 60:
                    last_logged = now
                    journal(self.lease.cfg, "heartbeat_error",
                            error=f"{type(e).__name__}: {e}")

    def __enter__(self):
        self._thread = threading.Thread(target=self._beat, name="nw-lease-heartbeat",
                                        daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)
        return False


# ---- config reload (SPEC-0226) --------------------------------------------------------------
# The loop re-reads the user config when its file changes (an invalid file never replaces the
# working one), and journals what changed in every live project's `.nw/project.yaml`, which the
# per-tick `_Ctx` already re-reads.

_RUNTIME_PATHS = frozenset({"config_dir", "data_dir", "cache_dir"})
_DIFF_CAP = 50
_MISSING = object()


def _flatten(doc: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in doc.items():
        k = str(k)
        if k.startswith("_") or (not prefix and k in _RUNTIME_PATHS):
            continue
        if isinstance(v, dict) and v:
            out.update(_flatten(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def config_diff(old: dict, new: dict) -> list[str]:
    """Sorted dotted paths whose values differ (added and removed included); mappings are
    flattened, lists compare whole, `_*` and runtime path keys are ignored. Capped at 50 with
    a trailing `"…"`."""
    a, b = _flatten(old or {}), _flatten(new or {})
    keys = sorted(k for k in a.keys() | b.keys()
                  if a.get(k, _MISSING) != b.get(k, _MISSING))
    return keys[:_DIFF_CAP] + ["…"] if len(keys) > _DIFF_CAP else keys


def _positive_number(agentic: dict, key: str) -> None:
    v = agentic.get(key)
    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not v > 0):
        raise ValueError(f"agentic.{key} must be a number > 0, got {v!r}")


def validate_runner_cfg(cfg: dict, campaigns=()) -> None:
    """ValueError when `cfg` cannot drive the runner: the default profile and every profile a
    live campaign names must resolve with `slots` an int >= 1; `poll_interval_s` and
    `lease_ttl_s` must be numbers > 0; `runner` must be claude or fake."""
    agentic = cfg.get("agentic") or {}
    if not isinstance(agentic, dict):
        raise ValueError("agentic must be a mapping")
    for name in [None, *sorted({c["profile"] for c in campaigns if c.get("profile")})]:
        slots = resolve_profile(cfg, name).get("slots")
        if isinstance(slots, bool) or not isinstance(slots, int) or slots < 1:
            raise ValueError(f"profile {name or 'default'!r}: slots must be an int >= 1, "
                             f"got {slots!r}")
    _positive_number(agentic, "poll_interval_s")
    _positive_number(agentic, "lease_ttl_s")
    workers_cfg = agentic.get("workers")
    if isinstance(workers_cfg, dict):                         # SPEC-0345
        PG.check_max_personas(workers_cfg.get("max_personas"))
    if agentic.get("runner") not in (None, "claude", "fake", "cursor", "pi"):
        raise ValueError(f"unknown agentic.runner {agentic['runner']!r} "
                         f"(expected claude, cursor, pi or fake)")
    mapping = agentic.get("project_runners")
    if mapping is not None:
        if not isinstance(mapping, dict):
            raise ValueError("agentic.project_runners must be a mapping")
        for proj, kind in mapping.items():
            if kind not in ("claude", "cursor", "pi"):
                raise ValueError(f"agentic.project_runners[{proj}]: unknown runner {kind!r} "
                                 f"(expected claude, cursor or pi)")
    if agentic.get("cursor") is not None:
        from . import cursor_map as CM
        CM.load_cursor_map(cfg)   # validates agentic.cursor against the YAML schema
    if agentic.get("pi") is not None or agentic.get("runner") == "pi":
        from . import pi_map as PM
        PM.load_pi_map(cfg)


_UNSEEN = object()                                # a stat key no file can have


def _stat_key(path) -> tuple | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _err(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"


class ConfigWatch:
    """Per-tick config change detection for `run()`. Compares `(mtime_ns, size)` of the user
    config and of the `.nw/project.yaml` of every project with a live campaign against the last
    seen; first sight seeds silently. Broken files are reported through the lease info
    (`config_errors`, path -> entry), each file setting and clearing only its own entry."""

    def __init__(self, cfg: dict, loader):
        self.loader = loader
        config_dir = cfg.get("config_dir")
        self.user_path = Path(config_dir) / "config.yaml" if config_dir else None
        self.user_key = _stat_key(self.user_path) if self.user_path else None
        self._projects: dict = {}                 # path -> {"key", "doc" (last good or None)}
        self.workers_path = workers.path(cfg) if cfg.get("data_dir") else None     # SPEC-0337
        self.workers_key = _UNSEEN
        self._errors: dict = {}
        self._first = True
        self._moved = False

    def user_invalid(self) -> bool:
        """True while the user config on disk is broken and the old cfg is still in use."""
        return any(e["scope"] == "user" for e in self._errors.values())

    def check(self, cfg: dict, lease, campaigns) -> dict:
        """The cfg the next tick should use: the freshly loaded user config when it changed and
        is valid, else `cfg`."""
        if self._first:
            self._first = False
            if lease is not None and (lease.info or {}).get("config_errors"):
                lease.pop_info("config_errors")       # left by the image before a re-exec
        if self.user_path is not None:
            key = _stat_key(self.user_path)
            if key != self.user_key:
                self.user_key = key
                cfg = self._reload_user(cfg, lease, campaigns, key)
                if self._moved:                       # rewritten while we read it: retry next tick
                    self._moved = False
                    self.user_key = _UNSEEN
        self._check_projects(cfg, lease, campaigns)
        self._check_workers(lease)
        return cfg

    def _check_workers(self, lease) -> None:
        """SPEC-0337: publish an unreadable `workers.json` as a `config_errors` entry (scope
        `workers`, keyed by its mtime), so the detector emits one `runner_config_invalid` per
        distinct content; clear it once the file reads cleanly or is gone."""
        if self.workers_path is None:
            return
        key = _stat_key(self.workers_path)
        if key == self.workers_key:
            return
        self.workers_key = key
        path = str(self.workers_path)
        try:
            workers.read_file(self.workers_path)
        except (OSError, ValueError) as e:
            self._set_error(lease, path, "workers", _err(e), key)
        else:
            self._clear_error(lease, path)

    def _read_user_file(self) -> None:
        """Raise when the raw file is missing, empty or not a mapping: `load_config` would
        otherwise return pure defaults for it."""
        try:
            text = self.user_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ValueError(f"{self.user_path} is missing")
        if not text.strip():
            raise ValueError(f"{self.user_path} is empty")
        doc = yaml.safe_load(text)
        if not isinstance(doc, dict):
            raise ValueError(f"{self.user_path} is not a mapping (got {type(doc).__name__})")

    def _reload_user(self, cfg, lease, campaigns, key) -> dict:
        path = str(self.user_path)
        try:
            self._read_user_file()
            new = self.loader()
            validate_runner_cfg(new, campaigns)
            if _stat_key(self.user_path) != key:      # truncated or rewritten while we read it
                self._moved = True
                return cfg
        except Exception as e:
            error = _err(e)
            journal(cfg, "config_load_failed", scope="user", path=path, error=error)
            self._set_error(lease, path, "user", error, key)
            return cfg
        self._clear_error(lease, path)
        changed = config_diff(cfg, new)
        if changed:
            journal(cfg, "config_reloaded", scope="user", path=path, changed=changed,
                    restart_required=[k for k in changed if k == "agentic.lease_ttl_s"])
        return new

    def _check_projects(self, cfg, lease, campaigns) -> None:
        live = {}
        for name in sorted({c["project"] for c in campaigns if c.get("project")}):
            try:
                live[name] = P.config_path(P.project_root(cfg, name))
            except Exception:                         # the tick reports an unregistered project
                continue
        wanted = {str(p) for p in live.values()}
        for path in [p for p in self._projects if p not in wanted]:
            del self._projects[path]
            self._clear_error(lease, path)
        for name, p in live.items():
            path, key, scope = str(p), _stat_key(p), f"project:{name}"
            seen = self._projects.get(path)
            if seen is not None and seen["key"] == key:
                continue
            last_good = seen["doc"] if seen else None
            try:
                doc = P.load_project_config(p)
                if doc.get("project") and doc["project"] != name:
                    raise ValueError(f"{p}: project: {doc['project']!r} does not match "
                                     f"{name!r}")
            except Exception as e:
                error = _err(e)
                journal(cfg, "config_load_failed", scope=scope, path=path, error=error)
                self._set_error(lease, path, scope, error, key)
                self._projects[path] = {"key": key, "doc": last_good}
                continue
            self._clear_error(lease, path)
            changed = config_diff(last_good, doc) if last_good is not None else []
            if changed:
                journal(cfg, "config_reloaded", scope=scope, path=path, changed=changed)
            self._projects[path] = {"key": key, "doc": doc}

    def _set_error(self, lease, path, scope, error, key) -> None:
        self._errors[path] = {"scope": scope, "error": error, "since": store.now_iso(),
                              "mtime_ns": key[0] if key else 0}
        self._publish(lease)

    def _clear_error(self, lease, path) -> None:
        if self._errors.pop(path, None) is not None:
            self._publish(lease)

    def _publish(self, lease) -> None:
        if lease is None:
            return
        if self._errors:
            lease.update_info(config_errors=dict(self._errors))   # always a new dict
        else:
            lease.pop_info("config_errors")


def _waiting_on(cfg, pool: StepPool) -> list:
    """The flights that block an exec, as `{campaign, kind, run_ids, timeout_s}` (SPEC-0224):
    in-process work and tasks in `spawning` / `collect`. A detachable persona run in `wait`
    does not block (SPEC-0229), so it is not listed."""
    out = []
    for cid, fl in pool.flights.items():
        if all(f.done() or (hk is not None and hk.phase == "wait") for _s, f, hk in fl.tasks):
            continue
        kind = fl.tasks[0][0].kind if fl.tasks else "implement"
        if kind == "gate":
            timeout = events_settings(cfg)["gate_budget_s"]
        else:
            timeout = max((t for _, t in fl.runs if t is not None), default=None)
        out.append({"campaign": cid, "kind": kind, "run_ids": [r for r, _ in fl.runs],
                    "timeout_s": timeout})
    if _sentinel_busy(pool):                                # SPEC-0359: a full close suite
        fl = pool.sentinel
        if len(fl) > 2:                                     # SPEC-0493: a cadence job
            out.append({"campaign": CAD.campaign_id(fl[0], fl[2]), "kind": "cadence",
                        "run_ids": [], "timeout_s": events_settings(cfg)["gate_budget_s"]})
        else:
            out.append({"campaign": f"sentinel-{fl[0]}", "kind": "sentinel",
                        "run_ids": [], "timeout_s": events_settings(cfg)["gate_budget_s"]})
    return out


def _refresh_backlog(cfg) -> None:
    """Store the readiness of every QUEUED item (what step 1 of `_tick` does, without
    admitting), so a merge that just made one ready is visible to the watchdog."""
    for st in store.list_campaigns(cfg, state="QUEUED"):
        _record_backlog(cfg, st, *backlog_readiness(cfg, st))


class _SlotWatch:
    """The runner's last seen occupied count — the tick's `n_busy`, active-state campaigns
    plus flights (SPEC-0228). `check` stamps `slot_freed_at` in the lease when it drops, so
    the watchdog reads when a slot freed instead of guessing it from state rows."""

    def __init__(self, cfg, pool: StepPool):
        self.cfg, self.pool = cfg, pool
        self.last = self.count()

    def count(self) -> int:
        return len(slot_ids(store.list_campaigns(self.cfg), self.pool.ids()))

    def check(self, lease, **info) -> None:
        """One `update_info` call: `info` plus `in_flight` and, when the runner went from full to
        having a free slot, `slot_freed_at` (SPEC-0263: not on every drop in occupancy), and
        `slots_full_at` (SPEC-0268) while the slots are full."""
        now = self.count()
        try:
            slots = slots_for(self.cfg)
        except (ValueError, OSError):       # a half-written default profile: keep the old stamps
            slots = None
        if slots is not None:
            if now < self.last and self.last >= slots > now:
                info["slot_freed_at"] = store.now_iso()
            # SPEC-0268: when the runner last saw the slots full (None once it sees a free one),
            # so the watchdog never reads a slot freed since as older than this look
            info["slots_full_at"] = store.now_iso() if now >= slots else None
        self.last = now
        lease.update_info(in_flight=sorted(self.pool.ids()), **info)


_STOP_BOUND_S = 30.0               # SPEC-0229: how long a stop or a crash waits for in-process work


def _terminate_children() -> None:
    """End in-process children that a signal to the runner's group never reaches: the gates
    (own sessions). Main loop and crash path only, never a signal handler (SPEC-0229)."""
    G.terminate_all()


def _drain(cfg, pool: StepPool, lease, *, reason: str = "signal",
           interval: float = 20.0, slots: "_SlotWatch | None" = None,
           stop: "threading.Event | None" = None) -> TickReport:
    """Wait for the flights the exit cannot leave behind, applying each as it finishes (main
    thread), before the loop stops or re-execs (SPEC-0213). Persona runs in `wait` are detached
    (`pool.try_detach_all`, SPEC-0225/0229), so this returns once `pool.flights` is empty. While
    it waits, the lease info carries `draining` (SPEC-0224) for the out-of-process watchdog, and
    the stored backlog readiness is refreshed every `interval` seconds. The drain is a declared
    `busy` step (SPEC-0228): the largest flight timeout, from drain start. A harvest refreshes
    `in_flight` (and `slot_freed_at` on a drop).

    With `stop` set (a signal or `ctx.stop`): the gates are ended once (`_terminate_children`),
    and after `_STOP_BOUND_S` every `wait` task is force-detached and the remaining `spawning` /
    `collect` tasks are reported as `abandoned` and left to adoption, as are the unfinished
    in-process tasks (SPEC-0262: recorded, not killed)."""
    report = TickReport(drain=True)
    since = store.now_iso()
    waiting_on = _waiting_on(cfg, pool)
    gate_budget = events_settings(cfg)["gate_budget_s"]
    budget = max((w.get("timeout_s") or gate_budget for w in waiting_on), default=gate_budget)
    lease.update_info(draining={"reason": reason, "since": since, "waiting_on": waiting_on})
    terminated = False
    bound_at = None
    try:
        with _busy(lease, "drain", budget_s=budget):
            next_refresh = time.monotonic()
            while pool.flights or _sentinel_busy(pool):     # SPEC-0359: its gates too
                if time.monotonic() >= next_refresh:
                    _refresh_backlog(cfg)
                    next_refresh = time.monotonic() + interval
                if stop is not None and stop.is_set() and not terminated:
                    terminated = True
                    bound_at = time.monotonic() + _STOP_BOUND_S
                    _terminate_children()
                pool.wait_any(timeout=0.2)
                before = len(pool.flights)
                _harvest(pool, report)
                if not pool.flights and bound_at is not None and time.monotonic() >= bound_at:
                    break                                 # a stopped sentinel past the bound
                if pool.flights and not _sentinel_busy(pool):   # SPEC-0359: not past it
                    detached = pool.try_detach_all()      # SPEC-0225: persona runs outlive us
                    if detached is not None:
                        report.detached = detached
                        break
                if bound_at is not None and pool.flights and time.monotonic() >= bound_at:
                    report.abandoned = pool.force_detach_waiting()
                    report.abandoned += pool.unfinished_in_process()   # SPEC-0262: recorded
                    time.sleep(0.3)                       # the wait threads return in 0.2 s
                    report.detached = pool.detached_view()
                    pool.flights.clear()
                    break
                if len(pool.flights) != before:
                    lease.update_info(draining={"reason": reason, "since": since,
                                                "waiting_on": _waiting_on(cfg, pool)})
                    if slots is not None:
                        slots.check(lease)
                    else:
                        lease.update_info(in_flight=sorted(pool.ids()))
    finally:
        lease.pop_info("draining")
    lease.heartbeat()
    report.idle = not [s for s in store.list_campaigns(cfg) if s.get("state") != "PARKED"]
    return report


def _crash_sweep(cfg, pool: StepPool, bound_s: float) -> tuple:
    """The crash path's `finally` (SPEC-0229), inside one bound: apply what already finished,
    end the gates, detach every `wait` task, re-sweep until no task is in `spawning` / `collect`
    (the ones still there at the bound are abandoned, with the unfinished in-process tasks),
    then apply what finished during the wait (SPEC-0287). Returns `(detached, abandoned)`."""
    deadline = time.monotonic() + bound_s
    abandoned: list = []

    def harvest():
        try:
            rep = TickReport(drain=True)
            _harvest(pool, rep, guard=True)
            if rep.steps or rep.errors:
                journal(cfg, "tick", tick=None, steps=rep.steps, admitted=0, errors=rep.errors,
                        started=[], in_flight=[], drain=True)
        except Exception as e:
            journal(cfg, "harvest_error", campaign=None, error=f"{type(e).__name__}: {e}")

    harvest()
    try:
        _terminate_children()
    except Exception:
        pass
    while True:
        abandoned = pool.force_detach_waiting()
        # SPEC-0262: in-process tasks (a gate just terminated) get the same bound to end
        in_process = pool.unfinished_in_process()
        if not (abandoned or in_process) or time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    # SPEC-0287: apply the tasks that finished during the wait; abandon only the unfinished.
    # A flight holding a force-detached run is not a result: leave it to the next runner's
    # adoption, as `_drain` does (its tasks return DETACHED, a review group would misapply).
    # One whose run spawned during the wait (so detached on `spawned`) is caught by `_harvest`.
    with pool.lock:
        for cid in [c for c in pool.flights if c in pool.forced]:
            pool.flights.pop(cid)
    before = set(pool.flights)
    harvest()
    gone = before - set(pool.flights)
    abandoned = [a for a in abandoned if a["campaign"] not in gone
                 and a["run_id"] not in pool.forced.get(a["campaign"], ())]
    in_process = pool.unfinished_in_process()
    return pool.detached_view(), abandoned + in_process


_LAST_POOL: "StepPool | None" = None


def leftover_threads() -> list:
    """The last `run()`'s pool threads still alive after shutdown (SPEC-0229). Executor threads
    are not daemonic, so interpreter exit would join them: the CLI exits 70 instead."""
    return _LAST_POOL.leftover_threads() if _LAST_POOL is not None else []


# ---- detached start and stop (SPEC-0291) ---------------------------------------------------------
# `nw queue run --detach` re-executes the same entry point as a session leader and confirms the
# child took the lease; `nw queue stop` signals the lease holder, honoring the close guard.

RUNNER_LOG_ENV = "NW_RUNNER_LOG"
_LOG_TAIL = 20


def _started_as_module() -> bool:
    """True for a `python -m nightwright` start: `sys.argv[0]` is then `…/nw/__main__.py`, whose
    relative import fails when it is run again as a plain script."""
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    return getattr(spec, "name", None) == "nightwright.__main__"


def self_argv() -> list:
    """This process's command line as `sys.executable` runs it again (the SPEC-0194 re-exec):
    `python -m nw …` keeps `-m nightwright`, anything else is `python argv[0] …`."""
    if _started_as_module():
        return [sys.executable, "-m", "nightwright", *sys.argv[1:]]
    return [sys.executable, *sys.argv]


def entry_argv() -> list:
    """This process's command line for `--detach` (SPEC-0291): `sys.argv[0]` itself when it is
    an executable file (the console script), else `python -m nightwright` with the same arguments."""
    exe = sys.argv[0] if sys.argv else ""
    if not _started_as_module() and exe and os.path.isfile(exe) and os.access(exe, os.X_OK):
        return list(sys.argv)
    return [sys.executable, "-m", "nightwright", *sys.argv[1:]]


def _strip_detach(argv) -> list:
    """`argv` without `--detach` and `--log PATH` / `--log=PATH`."""
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
        elif a == "--detach" or a.startswith("--log="):
            continue
        elif a == "--log":
            skip = True
        else:
            out.append(a)
    return out


def _log_tail(path, n=_LOG_TAIL) -> str:
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def detach_runner(cfg, argv, log_path, *, timeout_s=15, popen=subprocess.Popen,
                  clock=time.monotonic, env=None, cwd=None, systemctl_run=None,
                  sleep=time.sleep) -> dict:
    """Start `argv` (minus `--detach`/`--log`) as a detached session leader, stdin closed and
    output appended to `log_path`, then wait until the child holds the lease. Returns
    `{pid, log}`. ValueError when the lease is already held, when the child exits first, or
    when it has not taken the lease within `timeout_s` (the child is then left running).
    `env` (default `os.environ`) and `cwd` (default this process's) are the child's
    (SPEC-0375: `nw queue restart` passes the old runner's)."""
    if L.is_held(cfg):
        st = L.lease_status(cfg)
        age = st.get("heartbeat_age_s")
        age_txt = f"heartbeat {age:.0f}s ago" if age is not None else "no heartbeat"
        raise ValueError(f"queue held by pid {st.get('pid', '?')} ({age_txt}); "
                         "stop it with nw queue stop")
    from . import runner_unit as RU
    if RU.installed():
        # SPEC-0483: the unit supervises the runner; `argv`, `env` and `cwd` are the unit's
        return RU.start(cfg, run=systemctl_run, timeout_s=max(float(timeout_s), RU.START_WAIT_S),
                        clock=clock, sleep=sleep)
    log_path = Path(log_path).expanduser().absolute()      # the child's cwd may differ
    child_argv = _strip_detach(argv)
    env = {**(os.environ if env is None else env), RUNNER_LOG_ENV: str(log_path)}
    env.pop(L.HANDOVER_ENV, None)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"--- nw queue run --detach {store.now_iso()} ---\n")
            fh.flush()
            child = popen(child_argv, stdin=subprocess.DEVNULL, stdout=fh,
                          stderr=subprocess.STDOUT, start_new_session=True, env=env,
                          **({"cwd": str(cwd)} if cwd is not None else {}))
    except OSError as e:
        raise ValueError(f"cannot start the runner ({child_argv[0]}, log {log_path}): {e}")
    end = clock() + timeout_s
    while True:
        # the lease file first: the child writes it right after taking the flock, so the flock
        # probe (a brief LOCK_EX of its own) never races the child's non-blocking acquire
        info = L.read_lease(cfg) or {}
        if info.get("pid") == child.pid and L.is_held(cfg):
            return {"pid": child.pid, "log": str(log_path)}
        rc = child.poll()
        if rc is not None:
            raise ValueError(f"runner pid {child.pid} exited with status {rc} before taking "
                             f"the lease; {log_path} ends with:\n{_log_tail(log_path)}")
        if clock() >= end:
            raise ValueError(f"runner pid {child.pid} did not take the lease within "
                             f"{timeout_s:g}s (still running, not killed); {log_path} ends "
                             f"with:\n{_log_tail(log_path)}")
        time.sleep(0.2)


GUARDED_BUSY = ("open", "close")


def close_guard(status: dict, now=None) -> dict | None:
    """The lease's `busy` when a SIGTERM must wait (SPEC-0291): an `open` or `close` step
    within `since + budget_s` (a killed close can leave the main checkout mid-merge, a killed
    open a half-built worktree). An unparseable `since` counts as guarded. Else None."""
    busy = (status or {}).get("busy")
    if not isinstance(busy, dict) or busy.get("kind") not in GUARDED_BUSY:
        return None
    age = _age(busy.get("since"), now)
    try:
        budget = float(busy.get("budget_s"))
    except (TypeError, ValueError):
        return busy
    if age is None or age <= budget:
        return busy
    return None


def _guard_text(busy: dict) -> str:
    return (f"close guard: {busy.get('kind')} of {busy.get('campaign') or '-'} started "
            f"{busy.get('since')}, budget {busy.get('budget_s')}s")


_SETTLE_S = 0.05
_SETTLE_TRIES = 20


def _settled_holder(cfg, sleep=time.sleep) -> dict:
    """`lease_status` once the lease file describes the flock holder: held with a `pid` and a
    `host`, and the same `(pid, host, started_at)` on two reads `_SETTLE_S` apart. The file is
    unlinked just before the flock is dropped and written just after it is taken, so a single
    read can show no holder, or a crashed predecessor's record, while the flock is held.
    Not held ⇒ returned as is. ValueError when it never settles."""
    def key(st):
        return (st.get("pid"), st.get("host"), st.get("started_at"))

    prev = None
    for _ in range(_SETTLE_TRIES):
        st = L.lease_status(cfg)
        if not st["held"]:
            return st
        if st.get("pid") and st.get("host") and prev is not None and key(st) == key(prev):
            return st
        prev = st
        sleep(_SETTLE_S)
    raise ValueError("the queue lease is held but its lease file does not name a settled "
                     f"holder (last: pid {prev.get('pid')} on {prev.get('host')}); retry")


def stop_refusal(st: dict, argv=None, *, wait_guard=False) -> str | None:
    """Why `stop_runner` does not signal the holder of the held lease `st` (a `lease_status`),
    or None: it runs on another host, it is a one-shot `nw queue tick` (`argv`, the lease
    file's), or it is inside the close guard (unless `wait_guard`). The `nw queue stop` dry run
    shows the same refusals."""
    import socket
    if st.get("host") != socket.gethostname():
        return (f"queue held by pid {st.get('pid')} on {st.get('host')}, not this host "
                f"({socket.gethostname()}); stop it there")
    if list(argv or [])[:3] == ["nw", "queue", "tick"]:
        return (f"pid {st.get('pid')} holds the lease for a one-shot `nw queue tick`, not a "
                "runner; it releases the lease when the tick ends")
    busy = close_guard(st)
    if busy is not None and not wait_guard:
        return f"{_guard_text(busy)}; retry later or pass --wait-guard"
    return None


def stop_runner(cfg, *, timeout_s=60, wait_guard=False, guard_wait_s=None, kill=os.kill,
                clock=time.monotonic, sleep=time.sleep, systemctl_run=None) -> dict:
    """SIGTERM the local lease holder and wait until the lease is released (SPEC-0291). Never
    SIGKILLs. Returns `{stopped: False, reason: "no runner"}` when nothing holds the lease,
    else `{stopped: True, pid, waited_s}`. ValueError on a `stop_refusal` (another host, a
    tick, the close guard unless `wait_guard`, which waits up to `guard_wait_s`, default the
    remaining budget + 30 s), or when the lease is still held after `timeout_s`."""
    end = None
    while True:
        st = _settled_holder(cfg, sleep)
        if not st["held"]:
            return {"stopped": False, "reason": "no runner"}
        refusal = stop_refusal(st, (L.read_lease(cfg) or {}).get("argv"), wait_guard=wait_guard)
        if refusal:
            raise ValueError(refusal)
        busy = close_guard(st)
        if busy is None:
            # re-checked right before the signal: an open or close that started since the
            # settle reads (or another holder) is judged again on a fresh settle
            fresh = L.lease_status(cfg)
            if fresh["held"] and fresh.get("pid") == st["pid"] and close_guard(fresh) is None:
                break
            continue
        if end is None:
            if guard_wait_s is None:
                age = _age(busy.get("since")) or 0
                try:
                    guard_wait_s = max(0.0, float(busy.get("budget_s")) - age) + 30
                except (TypeError, ValueError):
                    guard_wait_s = 30
            end = clock() + guard_wait_s
        if clock() >= end:
            raise ValueError(f"{_guard_text(busy)}; still guarded after "
                             f"{guard_wait_s:g}s of waiting")
        sleep(0.2)
    pid = st["pid"]
    start = clock()
    from . import runner_unit as RU
    try:
        if RU.installed() and RU.active(systemctl_run):
            RU.stop(run=systemctl_run)      # SPEC-0483: a deliberate stop; systemd will not restart it
        else:
            kill(pid, signal.SIGTERM)
    except ProcessLookupError:              # it exited (and released) since the lease was read
        return {"stopped": False, "reason": "no runner"}
    except PermissionError:
        raise ValueError(f"pid {pid} holds the lease but cannot be signalled by this user")
    end = start + timeout_s
    while L.is_held(cfg):
        if clock() >= end:
            raise ValueError(f"pid {pid} did not release the lease within {timeout_s:g}s "
                             "(still running); not killed")
        sleep(0.2)
    return {"stopped": True, "pid": pid, "waited_s": round(clock() - start, 1)}


def run(cfg, *, once=False, max_ticks=None, poll_interval_s=None, until_idle=False,
        on_tick=None, runner=None, argv=None, reexec=False, code_root=None,
        config_loader=None) -> int:
    """The loop: take the lease, tick, heartbeat, sleep. Agent steps run in a pool owned by
    the loop and carry across ticks (SPEC-0213); the wait between ticks ends early when one
    finishes. SIGINT/SIGTERM only set a flag (SPEC-0229): the loop then stops admissions, ends
    the gates, detaches persona runs (the next runner adopts them), records the in-process
    steps that finish within a bound, and releases the lease. Every exit drains first.
    Returns the number of ticks performed.

    With `reexec` (SPEC-0194), when a commit to the nightwright checkout (`code_root`, default the one
    this package is imported from) changes nightwright's code, the loop keeps ticking in *draining*
    mode (SPEC-0229: it admits and opens, plans detachable persona steps only, starts no gate,
    close or FakeRunner step) and re-executes the same command, keeping its lease, at the first
    0.2 s check where nothing blocks: persona runs in `wait` are detached, in-process work
    done. `stop` is journaled with reason `code_changed`.

    With `config_loader` (SPEC-0226, a callable returning a fresh cfg), the loop re-reads the
    user config before each tick when its file changed; in-flight steps keep the cfg they were
    planned with. A config that fails to load or validate never replaces the working one."""
    global _LAST_POOL
    agentic = cfg.get("agentic") or {}
    interval = float(poll_interval_s if poll_interval_s is not None
                     else agentic.get("poll_interval_s") or 20)
    stop = threading.Event()
    lease = L.acquire(cfg, argv or ["nw", "queue", "run"])
    previous = {}
    entered = False                     # SPEC-0262: the main `try` owns the lease from there on
    try:
        # SPEC-0291: started by --detach; popped so no descendant inherits it
        runner_log = os.environ.pop(RUNNER_LOG_ENV, None)
        if runner_log:
            lease.update_info(log=runner_log, detached=True)
        G.reset()                                          # SPEC-0229: no stale stop flag
        lease.update_info(poll_interval_s=interval, slot_freed_at=store.now_iso())   # SPEC-0228

        def _handler(signum, _frame):
            stop.set()                      # the main loop does the rest (SPEC-0229)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                previous[sig] = signal.signal(sig, _handler)
            except ValueError:                                  # not the main thread (tests)
                pass
        ticks = 0
        reason = "signal"
        watch_root = (code_root or code_checkout()) if reexec else None
        code_sha = _git_out(watch_root, "rev-parse", "HEAD") if watch_root else None
        # SPEC-0375: the code this process runs, for `nw queue watch`'s stale-code line — with
        # or without re-exec (a --no-reexec runner is the one that goes stale)
        sha_root = watch_root or code_root or code_checkout()
        started_sha = code_sha or (_git_out(sha_root, "rev-parse", "HEAD") if sha_root else None)
        if started_sha:
            lease.update_info(code_sha=started_sha)
        restart = {}
        draining = False                                  # a code change was detected (SPEC-0229)
        detached: list = []
        abandoned: list = []
        deferred = None                                   # head a re-exec was deferred for
        exec_blocked = False                              # SPEC-0226: user config invalid this tick
        pool = StepPool()                                 # SPEC-0213: flights outlive ticks
        _LAST_POOL = pool
        watch = ConfigWatch(cfg, config_loader) if config_loader else None
        slots = _SlotWatch(cfg, pool)
        drain_since = None

        last_blocked_key = None                           # SPEC-0268: dedupe admission_blocked

        def emit(report):
            if report.steps or report.admitted or report.errors or report.started:
                journal(cfg, "tick", tick=ticks, steps=report.steps, admitted=report.admitted,
                        errors=report.errors, started=report.started,
                        in_flight=report.in_flight, drain=report.drain)
            if on_tick:
                on_tick(report)

        def drain_info():
            lease.update_info(draining={"reason": "code_changed", "since": drain_since,
                                        "waiting_on": _waiting_on(cfg, pool)})

        journal(cfg, "start", argv=list(argv or ["nw", "queue", "run"]), interval_s=interval,
                code_sha=started_sha)
        _publish_credentials(lease, CR.ensure(cfg, where="start"))  # SPEC-0483 / SPEC-0487
        BP.startup_check(cfg)                             # SPEC-0484: loud, never fatal
        entered = True
    finally:
        if not entered:
            lease.release()
            for sig, prev in previous.items():
                try:
                    signal.signal(sig, prev)
                except (ValueError, TypeError):
                    pass
    crashed = False
    crash = None
    try:
        # SPEC-0204/0213: one heartbeat thread for the loop's whole life — ticks, waits, the
        # drain — joined before the lease is released or handed over
        with _heartbeat_while(lease):
            while not stop.is_set():
                if watch:
                    live = [c for c in store.list_campaigns(cfg)
                            if c.get("state") not in TERMINAL_STATES]
                    new_cfg = watch.check(cfg, lease, live)
                    if new_cfg is not cfg:
                        cfg = new_cfg
                        if poll_interval_s is None:
                            interval = float((cfg.get("agentic") or {}).get("poll_interval_s")
                                             or 20)
                            lease.update_info(poll_interval_s=interval)   # SPEC-0228
                tick_started = time.monotonic()
                report = tick(cfg, stop=stop, runner=runner, pool=pool, lease=lease,
                              draining=draining)
                tick_s = time.monotonic() - tick_started
                ticks += 1
                lease.heartbeat()
                # SPEC-0228: stamp before `emit`, so a slow `on_tick` never delays it
                slots.check(lease, tick_at=store.now_iso(), last_tick_s=round(tick_s, 1),
                            ticks=ticks)
                emit(report)
                if report.blocked and not report.admitted:
                    holders = report.blocked["holders"]
                    key = (frozenset(report.blocked["ready"]),
                           frozenset(h["campaign"] for h in holders),
                           frozenset(h["campaign"] for h in holders if h["deferred"]))
                    if key != last_blocked_key:
                        last_blocked_key = key
                        journal(cfg, "admission_blocked", tick=ticks, draining=draining,
                                **report.blocked)
                else:
                    last_blocked_key = None
                if once or (max_ticks and ticks >= max_ticks) or (until_idle and report.idle):
                    reason = "once" if once else ("max_ticks" if max_ticks and ticks >= max_ticks
                                                  else "idle")
                    break
                exec_blocked = False
                if code_sha:
                    head, changed = code_changes(watch_root, code_sha)
                    moved = changed is None or bool(changed)   # SPEC-0262: failed diff = change
                    if moved and watch and watch.user_invalid():
                        exec_blocked = True       # draining may go on, but never exec into it
                        # SPEC-0226: the new image would load the broken user config (crash
                        # or pure defaults); keep running on the last good one and keep
                        # `code_sha`, so the re-exec happens once the file is fixed
                        if deferred != head:
                            deferred = head
                            journal(cfg, "reexec_deferred", reason="user_config_invalid",
                                    to_sha=head)
                    elif moved or draining:
                        if moved:                 # a later merge moves the target (SPEC-0229)
                            restart = {"from_sha": code_sha, "to_sha": head,
                                       "paths": (changed or [])[:50]}
                            if changed is None:
                                restart["diff_failed"] = True
                        if not draining:
                            # SPEC-0194 + SPEC-0229: keep admitting; no new in-process work;
                            # exec once nothing blocks it
                            draining, drain_since = True, store.now_iso()
                            journal(cfg, "draining", ticks=ticks, **restart)
                            drain_info()
                    else:
                        code_sha = head or code_sha   # docs-only commits: keep running
                wait_s = max(interval, _MIN_INFLIGHT_WAIT_S) if pool.flights else interval
                deadline = time.monotonic() + wait_s
                exec_now = False
                while not stop.is_set():
                    if draining:
                        # SPEC-0229: poll every 0.2 s (at least once per tick); harvest what
                        # finished, exec as soon as nothing blocks (persona runs in `wait` are
                        # detached)
                        if pool.any_done():
                            before = set(pool.flights)
                            rep = TickReport(drain=True)
                            _harvest(pool, rep)
                            emit(rep)
                            if set(pool.flights) != before:
                                slots.check(lease)
                                drain_info()
                        got = None if exec_blocked else pool.try_detach_all()
                        if got is not None:
                            detached, reason, exec_now = got, "code_changed", True
                            break
                    RNJ.service(cfg, stop=stop)    # SPEC-0487: CLI jobs wait < 0.2 s, not a tick
                    if time.monotonic() >= deadline:
                        break
                    if draining:
                        pool.wait_any(timeout=min(0.2, max(0.0, deadline - time.monotonic())))
                    elif pool.any_done():             # a finished step: apply it now
                        break
                    else:
                        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
                if exec_now:
                    break
            if pool.flights:
                drained = _drain(cfg, pool, lease, reason=reason, interval=interval,
                                 slots=slots, stop=stop)
                emit(drained)
                detached, abandoned = drained.detached, drained.abandoned
            elif stop.is_set():
                _terminate_children()
            pool.end(timeout=0.5 if abandoned else 5.0)    # stuck threads are left behind
            if reason == "code_changed" and stop.is_set():
                reason = "signal"             # signalled before the exec: no exec; runs stay
    except BaseException as e:
        crashed = True
        if not isinstance(e, (KeyboardInterrupt, SystemExit)):
            import traceback
            crash = {"ticks": ticks, "error": f"{type(e).__name__}: {e}",
                     "traceback": traceback.format_exc()[-4000:]}
            reason = "crash"
        raise
    finally:
        try:
            if crashed:
                stop.set()                            # workers see it: runs end as interrupted
                detached, abandoned = _crash_sweep(cfg, pool, _STOP_BOUND_S)
                pool.end(timeout=0.5)
            else:
                pool.end(timeout=0.0)
            exit_fields = {}
            if reason == "code_changed":
                exit_fields.update(restart)
            elif draining and reason in ("max_ticks", "idle") and restart.get("to_sha"):
                exit_fields["restart_pending"] = {"to_sha": restart["to_sha"]}
            if abandoned or crash:
                exit_fields["abandoned"] = abandoned
            if crash:
                journal(cfg, "crash", detached=detached, **crash, **exit_fields)
            else:
                journal(cfg, "stop", ticks=ticks, reason=reason, detached=detached,
                        **exit_fields)
            if draining:
                lease.pop_info("draining")
            handover_env = lease.handover() if reason == "code_changed" else None
            G.reset()
        finally:
            lease.release()
            for sig, prev in previous.items():
                try:
                    signal.signal(sig, prev)
                except (ValueError, TypeError):
                    pass
    if handover_env is not None:
        sys.stdout.flush()
        sys.stderr.flush()
        if lease.info.get("detached") and lease.info.get("log"):   # SPEC-0291: the new image
            handover_env = {**handover_env, RUNNER_LOG_ENV: lease.info["log"]}  # records it too
        _EXEC(sys.executable, self_argv(), {**os.environ, **handover_env})
    return ticks


# ---- listing -----------------------------------------------------------------------------------

def _age(ts: str | None, now=None) -> int | None:
    if not ts:
        return None
    import datetime as dt
    try:
        t = dt.datetime.fromisoformat(ts)
    except (TypeError, ValueError):         # not a string (a hand-edited file), or garbage
        return None
    now = now or dt.datetime.now().astimezone()
    if t.tzinfo is None:
        t = t.astimezone()
    return max(0, int((now - t).total_seconds()))


def park_view(cfg, state: dict, now, sett) -> dict:
    """SPEC-0222: who a PARKED campaign waits for and whether it was decided. Derived from the
    park and its ledger (`park` / `note` events); nothing is stored."""
    park = state.get("park") or {}
    reason = park.get("reason")
    owner = park_owner(reason)
    led = store.read_ledger(cfg, state["id"], types=["park", "note"])
    last = next((e for e in reversed(led) if e.get("type") == "park"), None)
    park_seq = (last or {}).get("seq") or 0
    parked_at = (last or {}).get("ts") or park.get("since")
    note = next((e for e in reversed(led)
                 if e.get("type") == "note" and (e.get("seq") or 0) > park_seq), None)
    decision = ({"text": note.get("text"), "ts": note.get("ts"),
                 "by": note.get("author") or note.get("by")} if note else None)
    decided = decision is not None or reason == "human"
    parked_for = _age(parked_at, now) or 0
    overdue = owner == "conductor" and not decided and parked_for > sett["park_decide_s"]
    return {"owner": owner, "decision": decision, "decided": decided, "overdue": overdue,
            "parked_at": parked_at, "park_seq": park_seq, "parked_for_s": parked_for}


_PARK_VIEW_KEYS = ("owner", "decision", "decided", "overdue", "parked_at")


def _detached_run(cfg, state: dict) -> str | None:
    """`detached (pid N)` when the campaign's open run has a live process and no runner holds
    the lease (SPEC-0229); read from the run dir. None otherwise."""
    if state.get("state") not in ACTIVE_STATES:
        return None
    cid = state.get("id")
    try:
        for ev in _open_runs(cfg, cid):
            rd = store.runs_dir(cfg, cid) / str(ev.get("run_id"))
            if RP.run_status(rd) in ("running", "orphan_group"):
                proc = RP.read_json(rd / "proc.json") or {}
                child = RP.read_json(rd / "child.json") or {}
                return f"detached (pid {child.get('pid') or proc.get('pid')})"
    except OSError:
        return None
    return None


def queue_payload(cfg, *, include_terminal=False, project=None, state=None, now=None) -> dict:
    """`nw.queue.v1`: the lease, the slots and every in-flight item."""
    import datetime as dt
    from . import events as EV
    now = now or dt.datetime.now().astimezone()
    sett = EV.settings(cfg)
    rows = store.list_campaigns(cfg, include_terminal=include_terminal, project=project,
                                state=state)
    slots = slots_for(cfg)
    busy = sum(1 for r in rows if r.get("state") in SLOT_STATES)
    backlog = {b["campaign"]: b for b in backlog_view(cfg)}
    held = L.is_held(cfg)
    holds = PG.read_holds(cfg, now)                  # SPEC-0345
    items = []
    for r in rows:
        counters = r.get("counters") or {}
        cost = r.get("cost") or {}
        try:
            loop_max = resolve_profile(cfg, r.get("profile"), project_root=(
                r.get("repo") or {}).get("root")).get("loop_max")
        except (ValueError, OSError):
            loop_max = None
        repo = r.get("repo") or {}
        sizing = r.get("sizing") or {}
        park = r.get("park")
        if r.get("state") == "PARKED":
            view = park_view(cfg, r, now, sett)
            park = {**(park or {}), **{k: view[k] for k in _PARK_VIEW_KEYS}}
        spec_evs = (store.read_ledger(cfg, r["id"], types=["spec_review"])
                    if r.get("kind") == "idea" else [])
        items.append({
            "campaign": r.get("id"), "spec_id": r.get("spec_id"), "project": r.get("project"),
            "kind": r.get("kind"), "idea_id": _idea_id_of(r), "label": campaign_label(r),
            "merge_branch": (r.get("merge") or {}).get("branch") if r.get("kind") == "merge" else None,
            "profile": r.get("profile"), "state": r.get("state"),
            "size": sizing.get("size"), "tone": sizing.get("tone"),
            "profile_source": sizing.get("source"),
            "loops": f"{counters.get('loops', 0)}/{loop_max if loop_max is not None else '?'}",
            "runs": counters.get("runs", 0),
            "cost_usd": round(float(cost.get("total_usd") or 0.0), 4),
            "cap_usd": cost.get("cap_usd"),
            "cost": {"estimated_usd": round(float(cost.get("estimated_usd") or 0.0), 4)},
            "created_at": r.get("created_at"),          # SPEC-0410: the TUI's stable sort key
            "age_s": _age(r.get("created_at")), "updated_age_s": _age(r.get("updated_at")),
            "park": park, "park_requested": bool(r.get("park_requested")),
            "worktree": repo.get("worktree"), "branch": repo.get("branch"),
            "priority": _priority(r), "waiting": r.get("waiting"),
            "review_score": (r.get("last_review") or {}).get("score"),
            "run": None if held else _detached_run(cfg, r),
            "spec_round": spec_round(r, spec_evs),
        })
        if r.get("kind") == "idea":                 # SPEC-0211: where an idea campaign came from
            last = spec_evs[-1] if spec_evs else None
            items[-1]["idea"] = {"id": _idea_id_of(r), "heading": (r.get("idea") or {}).get("heading")}
            items[-1]["last_spec_review"] = (
                {"round": last.get("round"), "verdict": last.get("verdict")} if last else None)
        b = backlog.get(r.get("id"))
        if b and r.get("state") == "QUEUED":
            items[-1]["backlog"] = {k: b[k] for k in ("position", "priority", "ready", "blocked_by")}
        rv = row_view(cfg, r, b, remote=r.get("state") == "GATING", now=now)   # SPEC-0356
        items[-1].update({k: v for k, v in rv.items() if k not in ("priority", "cost_usd", "cap_usd")})
        hb = PG.held_by(cfg, r, now, holds) if r.get("state") in ACTIVE_STATES else None
        if hb and EV._open_run(cfg, r["id"]):         # a run in flight is not held
            hb = None
        items[-1]["held_by"] = hb                     # SPEC-0345
    rl = PG.read_rate_limit(cfg)
    return {"schema": "nw.queue.v1", "lease": L.lease_status(cfg),
            "slots": {"total": slots, "busy": busy}, "items": items,
            "personas": {"max_personas": PG.max_personas(cfg),                # SPEC-0345
                         "rate_limit": {"until": rl["until"], "backoff_s": rl["backoff_s"],
                                        "active": PG.backoff_until(cfg, now) is not None}}}


# ---- dashboard (nw queue watch, SPEC-0185) -------------------------------------------------------

PHASES = ("OPENING", "SPEC_WRITING", "SPEC_REVIEW", "IMPLEMENTING", "GATING", "TRIAGING",
          "REVIEWING", "DIVERGENCE_TRIAGE", "CLOSING")
_SPEC_PHASES = ("SPEC_WRITING", "SPEC_REVIEW")
_TITLE_CACHE: dict = {}


def phases_for(state: dict) -> tuple:
    """SPEC-0211: the stepper's phases — the spec phases only for an idea campaign."""
    if state.get("kind") == "idea":
        return PHASES
    return tuple(p for p in PHASES if p not in _SPEC_PHASES)


def spec_round(state: dict, ledger: list) -> int | None:
    """SPEC-0211: the spec-review round an idea campaign is at, from its `spec_review` events:
    the round under review in SPEC_REVIEW, the last round while revising or once accepted, None
    before any review (a spec campaign has no such events). PARKED / WAITING follow `resume_to`."""
    evs = [e for e in ledger if e.get("type") == "spec_review"]
    last = (evs[-1].get("round") or len(evs)) if evs else 0
    st = state.get("state")
    if st in ("PARKED", "WAITING"):
        st = ((state.get("park") if st == "PARKED" else state.get("waiting")) or {}).get("resume_to")
    if st == "SPEC_REVIEW":
        return last + 1
    return last or None


def _spec_title(state: dict) -> str | None:
    """The spec's frontmatter title in the project repo (cached on path + mtime); the idea's
    heading for an idea campaign without a spec (SPEC-0209)."""
    return _spec_meta(state)["title"]


def _deps_list(raw) -> list[str]:
    """A frontmatter `depends_on` as a list of str (a bare string is one id)."""
    if not raw:
        return []
    return [str(d) for d in ([raw] if isinstance(raw, str) else raw) if d]


def _spec_meta(state: dict) -> dict:
    """SPEC-0356: `{title, depends_on}` of the campaign's spec, read through one mtime cache.
    A merge campaign's title is its note; an idea without a spec has the idea's heading; neither
    has dependencies."""
    if state.get("kind") == "merge":
        return {"title": (state.get("merge") or {}).get("note") or "merge request",
                "depends_on": []}
    if has_no_spec(state):
        return {"title": (state.get("idea") or {}).get("heading"), "depends_on": []}
    none = {"title": None, "depends_on": []}
    repo = state.get("repo") or {}
    rel = (state.get("spec") or {}).get("rel_path")
    if not repo.get("root") or not rel:
        return none
    bases = [repo.get("root")]
    if state.get("kind") == "idea":     # SPEC-0211: the worktree first; once merged it is gone
        bases.insert(0, repo.get("worktree"))
    for base in bases:
        path = Path(base) / rel if base else None
        try:
            mtime = path.stat().st_mtime if path else None
        except OSError:
            mtime = None
        if mtime is not None:
            break
    else:
        return none
    hit = _TITLE_CACHE.get(str(path))
    if hit and hit[0] == mtime:
        return {"title": hit[1]["title"], "depends_on": list(hit[1]["depends_on"])}
    try:
        fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return none
    fm = fm if isinstance(fm, dict) else {}
    meta = {"title": str(fm.get("title") or "") or None,
            "depends_on": _deps_list(fm.get("depends_on"))}
    _TITLE_CACHE[str(path)] = (mtime, meta)
    return {"title": meta["title"], "depends_on": list(meta["depends_on"])}   # callers own it


# ---- list views: row model, graph, unqueued (SPEC-0356) -----------------------------------------

_WAITING_ON_RE = re.compile(r"waiting on (\S+) \(|^(\S+) not found$")


def waiting_on_ids(blocked_by) -> list[str]:
    """The ids a backlog item waits on, parsed from `backlog_readiness`'s strings
    (`waiting on SPEC-0337 (accepted)`, `PROJ-0012 not found`); order kept, de-duplicated.
    Status / branch / idea blockers carry no id."""
    out = []
    for text in blocked_by or []:
        for m in _WAITING_ON_RE.finditer(str(text)):
            dep = m.group(1) or m.group(2)
            if dep and dep not in out and dep not in ("spec", "idea"):
                out.append(dep)
    return out


def row_view(cfg, st: dict, backlog_entry=None, *, remote=False, now=None) -> dict:
    """SPEC-0356: the display fields every list view (and the TUI) shares for one campaign.
    `backlog_entry` is the campaign's `backlog_view` row (QUEUED readiness); `remote` adds the
    `remote_gate` object (one ledger read; GATING items and `campaign show`)."""
    meta = _spec_meta(st)
    state = st.get("state")
    if state == "QUEUED":
        blocked = list((backlog_entry or {}).get("blocked_by") or [])
    elif state == "WAITING":
        blocked = ["waiting for a slot"]
    else:
        blocked = []
    park = (st.get("park") or {}) if state == "PARKED" else {}
    cost = st.get("cost") or {}
    out = {"title": meta["title"], "priority": _priority(st), "depends_on": meta["depends_on"],
           "blocked_by": blocked, "waiting_on": waiting_on_ids(blocked),
           "park_reason": park.get("reason"), "park_detail": park.get("detail"),
           "cost_usd": round(float(cost.get("total_usd") or 0.0), 4),
           "cap_usd": cost.get("cap_usd")}
    if st.get("after"):                                 # SPEC-0423: only when set
        out["after"] = [e["ref"] for e in after_entries(st)]
    if remote:
        out["remote_gate"] = remote_gate_view(cfg, st, now=now)
    return out


_REMOTE_TYPES = ("gate", "gate_dispatched", "worker_failed")


def remote_gate_view(cfg, st: dict, *, now=None, events=None) -> dict | None:
    """SPEC-0356: the campaign's latest remote gate run from its ledger, or None for a local-only
    campaign. Reads events by `type` string (`gate_dispatched` is not in EVENT_TYPES until the
    SPEC-0338 dispatch emits it): a `gate_dispatched` with no later `gate` of its `gate_seq` is
    running (a later `worker_failed` ends it: the local fallback is not a remote run); a `gate`
    with `worker` finished; a `worker_failed` followed by a local `gate` fell
    back. The job id is the event's `job_id`, else derived `<campaign>-gate-<seq:03d>-a0`."""
    import datetime as dt
    cid = st.get("id")
    evs = events if events is not None else store.read_ledger(cfg, cid, types=list(_REMOTE_TYPES))
    evs = [e for e in evs if e.get("type") in _REMOTE_TYPES]
    gates = [e for e in evs if e.get("type") == "gate"]
    last_gate = gates[-1] if gates else None
    disp = next((e for e in reversed(evs) if e.get("type") == "gate_dispatched"), None)
    now = now or dt.datetime.now().astimezone()

    def job(ev, seq):
        jid = (ev or {}).get("job_id")
        if jid:
            return jid, False
        return (f"{cid}-gate-{int(seq):03d}-a0", True) if isinstance(seq, int) else (None, True)

    if disp is not None and not any(
            (e.get("type") == "gate" and e.get("gate_seq") == disp.get("gate_seq"))
            or e.get("type") == "worker_failed"
            for e in evs if e.get("seq", 0) > disp.get("seq", 0)):
        if last_gate is None or last_gate.get("seq", 0) < disp.get("seq", 0):
            jid, derived = job(disp, disp.get("gate_seq"))
            started = disp.get("started_at") or disp.get("ts")
            return {"gate_seq": disp.get("gate_seq"), "worker": disp.get("worker"),
                    "job_id": jid, "job_id_derived": derived, "status": "running",
                    "started_at": started, "elapsed_s": _age(started, now), "duration_s": None}
    if last_gate is None:
        return None
    seq = last_gate.get("gate_seq")
    base = {"gate_seq": seq, "started_at": last_gate.get("started_at"),
            "elapsed_s": None, "duration_s": last_gate.get("duration_s")}
    if last_gate.get("worker"):
        jid, derived = job(last_gate, seq)
        return {**base, "worker": last_gate.get("worker"), "job_id": jid,
                "job_id_derived": derived, "status": "ok" if last_gate.get("ok") else "failed"}
    prev = [e for e in evs if e.get("seq", 0) < last_gate.get("seq", 0)]
    gate_before = next((e for e in reversed(prev) if e.get("type") == "gate"), None)
    failed = next((e for e in reversed(prev) if e.get("type") == "worker_failed"
                   and (gate_before is None or e.get("seq", 0) > gate_before.get("seq", 0))), None)
    if failed is None:
        return None
    jid, derived = job(failed, seq)
    return {**base, "worker": failed.get("worker"), "job_id": jid, "job_id_derived": derived,
            "status": "fell_back", "stage": failed.get("stage")}


def _ago(s) -> str:
    s = int(s or 0)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s" if s < 600 else f"{s // 60}m"
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def remote_gate_line(rg: dict | None, *, now=None) -> str | None:
    """SPEC-0356: the one `nw campaign show` line for `remote_gate_view`'s object."""
    if not rg:
        return None
    head = f"remote gate #{rg.get('gate_seq')}"
    job = rg.get("job_id") or "?"
    if rg["status"] == "running":
        return f"{head} on worker {rg.get('worker')}  job {job}  running {_ago(rg.get('elapsed_s'))}"
    if rg["status"] == "fell_back":
        return (f"{head} worker {rg.get('worker')} failed (stage {rg.get('stage') or '?'})"
                f" → ran locally")
    dur = rg.get("duration_s")
    took = _ago(dur) if isinstance(dur, (int, float)) else "?"
    ago = ""
    if rg.get("started_at") and isinstance(dur, (int, float)):
        import datetime as dt
        try:
            end = dt.datetime.fromisoformat(rg["started_at"]) + dt.timedelta(seconds=dur)
            ago = f", {_ago(_age(end.isoformat(), now))} ago"
        except (TypeError, ValueError):
            ago = ""
    return f"{head} on worker {rg.get('worker')}  job {job}  {rg['status']} in {took}{ago}"


def _node_key(st: dict) -> str:
    """The graph node a campaign is: its spec id, the idea id without a spec, the campaign id for
    a merge request."""
    if st.get("kind") == "merge":
        return str(st.get("id"))
    return str(st.get("spec_id") or _idea_id_of(st) or st.get("id"))


def _spec_status_of(cfg, dep: str) -> tuple[str, str | None, str | None]:
    """(spec_status, title, project) of a dependency with no campaign, from the specs dir (internal)
    or the outbox (outbound); `missing` when no file is found."""
    try:
        if SP._is_outbound_spec_id(dep):
            for s in SP._outbound_specs(cfg):
                if str((s.get("fm") or {}).get("id") or "") == dep:
                    fm = s["fm"]
                    return str(fm.get("status") or "?"), fm.get("title"), s["project"]
            return "missing", None, None
        fm, _ = split_frontmatter(SP._find_spec(SP._specs_dir(cfg), dep).read_text(encoding="utf-8"))
        fm = fm if isinstance(fm, dict) else {}
        return str(fm.get("status") or "?"), fm.get("title"), None
    except (OSError, ValueError, SystemExit):
        return "missing", None, None


def _pick_campaign(sts: list) -> dict:
    """SPEC-0356: of several campaigns for one key, the newest non-terminal; with only terminal
    ones (`--all`), the newest, preferring MERGED."""
    live = [s for s in sts if s.get("state") not in store.TERMINAL_STATES]
    pool = live or ([s for s in sts if s.get("state") == "MERGED"] or sts)
    return max(pool, key=lambda s: (s.get("created_at") or "", s.get("id") or ""))


def _cycles(nodes: list, deps_of: dict) -> list[list[str]]:
    """Strongly connected components with more than one member (or a self-loop), by an
    iterative Tarjan; each sorted, the list sorted. Never recursive, never infinite."""
    index, low, on, stack, out, counter = {}, {}, set(), [], [], [0]
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(deps_of.get(root, ())))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in deps_of:
                    continue
                if w not in index:
                    index[w] = low[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(deps_of.get(w, ()))))
                    advanced = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1 or v in deps_of.get(v, ()):
                    out.append(sorted(comp))
    return sorted(out)


def graph_view(cfg, project=None, include_terminal=False) -> dict:
    """SPEC-0356 `nw.queue.graph.v1`: campaigns as a dependency DAG keyed by spec id. Every
    `depends_on` id with no node becomes a stub (its spec status read from the specs dir), so an
    unqueued dependency is visible; under `project`, stubs from elsewhere are kept, `external`.
    Edges are `dep → dependent` from `depends_on` only. Pure over `list_campaigns` + spec files."""
    sts = store.list_campaigns(cfg, include_terminal=include_terminal)
    backlog = {b["campaign"]: b for b in backlog_view(cfg)}
    by_key: dict = {}
    for st in sts:
        by_key.setdefault(_node_key(st), []).append(st)
    nodes: dict = {}
    for key, group in by_key.items():
        st = _pick_campaign(group)
        if project and st.get("project") != project:
            continue
        rv = row_view(cfg, st, backlog.get(st.get("id")))
        nodes[key] = {"id": key, "campaign": st.get("id"), "kind": st.get("kind") or "spec",
                      "state": st.get("state"), "spec_status": None, "title": rv["title"],
                      "project": st.get("project"), "priority": rv["priority"],
                      "cost_usd": rv["cost_usd"], "cap_usd": rv["cap_usd"],
                      "park_reason": rv["park_reason"], "blocked_by": rv["blocked_by"],
                      "waiting_on": rv["waiting_on"], "depends_on": rv["depends_on"],
                      "stub": False, "external": False,
                      "_key": admission_key(st)}
    for key in list(nodes):
        for dep in nodes[key]["depends_on"]:
            if dep in nodes:
                continue
            other = by_key.get(dep)
            if other:
                st = _pick_campaign(other)
                status, title, proj = None, _spec_title(st), st.get("project")
                state, camp = st.get("state"), st.get("id")
            else:
                status, title, proj = _spec_status_of(cfg, dep)
                proj = proj or (None if SP._is_outbound_spec_id(dep) else INTERNAL_PROJECT)
                state, camp = None, None
                if status not in ("missing", "done", "superseded"):
                    status = f"{status}, no campaign"
            nodes[dep] = {"id": dep, "campaign": camp, "kind": "spec", "state": state,
                          "spec_status": status, "title": title, "project": proj,
                          "priority": None, "cost_usd": None, "cap_usd": None,
                          "park_reason": None, "blocked_by": [], "waiting_on": [],
                          "depends_on": [], "stub": True,
                          "external": bool(project) and (proj or INTERNAL_PROJECT) != project,
                          "_key": (99, "", dep)}
    edges = sorted({(d, k) for k, n in nodes.items() for d in n["depends_on"] if d in nodes})
    deps_of = {k: [d for d in n["depends_on"] if d in nodes] for k, n in nodes.items()}
    cycles = _cycles(sorted(nodes), deps_of)
    in_cycle = {m for c in cycles for m in c}
    roots = sorted((k for k in nodes if k not in in_cycle
                    and not [d for d in deps_of[k] if d not in in_cycle]),
                   key=lambda k: nodes[k]["_key"])
    out_nodes = []
    for k in sorted(nodes, key=lambda k: nodes[k]["_key"]):
        n = dict(nodes[k])
        n.pop("_key")
        out_nodes.append(n)
    return {"schema": "nw.queue.graph.v1", "nodes": out_nodes,
            "edges": [{"from": a, "to": b} for a, b in edges], "roots": roots,
            "cycles": cycles}


UNQUEUED_STATUSES = ("accepted", "proposed", "draft")
_UNQUEUED_ORDER = ("ready", "deps-unmet", "previous-campaign", "not-accepted", "spec-missing",
                   "no-spec")


def _spec_reasons(cfg, specs_dir, fm: dict, status: str, prev, project=None) -> list[dict]:
    reasons = []
    if status in ("draft", "proposed"):
        reasons.append({"code": "not-accepted", "text": f"spec is {status}"})
    for dep in _deps_list(fm.get("depends_on")):
        # SPEC-0347, as in backlog_readiness: an outbound dependency whose campaign is MERGED is
        # met even when the local spec file lags (or is missing).
        merged = (_dep_campaign(cfg, dep, project) or {}).get("state") == "MERGED"
        try:
            dep_fm, _ = split_frontmatter(_find_dep_spec(specs_dir, dep).read_text(encoding="utf-8"))
        except (OSError, ValueError, SystemExit):
            if not merged:
                reasons.append({"code": "deps-unmet", "text": f"{dep} not found"})
            continue
        met, why = dependency_met(specs_dir, dep, dep_fm or {},
                                  {str(fm["id"])} if fm.get("id") else set())
        if not met and not merged:
            reasons.append({"code": "deps-unmet", "text": f"waiting on {dep} ({why})"})
    if prev:
        day = str(prev.get("updated_at") or "")[:10]
        reasons.append({"code": "previous-campaign",
                        "text": f"campaign {prev.get('id')} {prev.get('state')} {day}".strip()})
    return reasons or [{"code": "ready", "text": "accepted, dependencies met, never queued"}]


def unqueued_view(cfg, project=None, statuses=None) -> dict:
    """SPEC-0356 `nw.queue.unqueued.v1`: accepted/proposed/draft specs (internal and outbound, no
    epics or policies) and SPECCED/INCUBATE ideas that no campaign references in a non-terminal
    state or MERGED, each with the reasons it is not queued and the command that would queue it.
    A SPECCED idea collapses into its spec's row, is omitted when its spec exists but is not a
    candidate, and is listed `spec-missing` only when its spec is not found."""
    statuses = tuple(statuses or UNQUEUED_STATUSES)
    sts = store.list_campaigns(cfg, include_terminal=True)
    by_spec: dict = {}
    by_idea: dict = {}
    for st in sts:
        if st.get("spec_id"):
            by_spec.setdefault(str(st["spec_id"]), []).append(st)
        if _idea_id_of(st):
            by_idea.setdefault(str(_idea_id_of(st)), []).append(st)

    def status_of(group):
        """(queued, previous terminal campaign) for the campaigns of one candidate."""
        group = sorted(group, key=lambda s: (s.get("created_at") or "", s.get("id") or ""))
        if any(s.get("state") not in store.TERMINAL_STATES or s.get("state") == "MERGED"
               for s in group):
            return True, None
        return False, (group[-1] if group else None)

    specs: list = []                                # (kind, fm, project, specs_dir)
    try:
        specs_dir = SP._specs_dir(cfg)
    except (ValueError, OSError):                   # no internal specs dir: no internal rows
        specs_dir = None
    all_ids: dict = {}
    if specs_dir is not None and specs_dir.exists():
        for path in sorted(specs_dir.glob("[0-9]*.md")):
            try:
                fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(fm, dict) and fm.get("id"):
                all_ids[str(fm["id"])] = fm
                specs.append(("spec", fm, INTERNAL_PROJECT, specs_dir))
    try:
        outbound = SP._outbound_specs(cfg)
    except (ValueError, OSError):
        outbound = []
    for s in outbound:
        fm = s.get("fm")
        if isinstance(fm, dict) and fm.get("id"):
            all_ids[str(fm["id"])] = fm
            specs.append(("outbound", fm, s["project"], s["path"].parent))
    rows, listed = [], {}
    for kind, fm, proj, sdir in specs:
        sid, status = str(fm["id"]), str(fm.get("status") or "")
        if status not in statuses or str(fm.get("kind") or "") in ("epic", "policy"):
            continue
        if project and proj != project:
            continue
        idea = str(fm.get("source_idea") or "") or None
        group = by_spec.get(sid, []) + (by_idea.get(idea, []) if idea else [])
        queued, prev = status_of(group)
        if queued:
            listed[sid] = None
            continue
        row = {"kind": kind, "id": sid, "title": fm.get("title"), "status": status,
               "project": proj, "idea_id": None, "depends_on": _deps_list(fm.get("depends_on")),
               "reasons": _spec_reasons(cfg, sdir, fm, status, prev, proj),
               "next": f"nw queue add {sid}",
               "previous_campaign": (prev or {}).get("id")}
        rows.append(row)
        listed[sid] = row
    for t in _ideas_or_empty(cfg):
        state = t.state or ""
        if state not in ("SPECCED", "INCUBATE"):
            continue
        iid = (t.properties.get("ID") or "").strip() or t.id
        iproj = idea_project(t)          # no :PROJECT: = INTERNAL_PROJECT (spec_scope)
        if project and iproj != project:
            continue
        spec_ref = (t.properties.get("SPEC") or "").strip()
        if state == "SPECCED":
            if spec_ref in listed:
                if listed[spec_ref] is not None:
                    listed[spec_ref]["idea_id"] = iid
                continue
            if spec_ref in all_ids or (spec_ref and _spec_exists(cfg, spec_ref)):
                continue
            reasons = [{"code": "spec-missing", "text": "idea is SPECCED but its spec was not found"}]
        else:
            reasons = [{"code": "no-spec", "text": "idea has no spec yet"}]
        queued, prev = status_of(by_idea.get(iid, []))
        if queued:
            continue
        if prev:
            reasons.insert(0, {"code": "previous-campaign",
                               "text": f"campaign {prev.get('id')} {prev.get('state')} "
                                       f"{str(prev.get('updated_at') or '')[:10]}".strip()})
        rows.append({"kind": "idea", "id": iid, "title": t.heading, "state": state,
                     "project": iproj, "idea_id": iid, "depends_on": [], "reasons": reasons,
                     "next": f"nw queue add {iid}", "previous_campaign": (prev or {}).get("id")})

    def order(r):
        codes = [x["code"] for x in r["reasons"]]
        return (min(_UNQUEUED_ORDER.index(c) for c in codes), r["id"])
    return {"schema": "nw.queue.unqueued.v1", "items": sorted(rows, key=order)}


def _spec_exists(cfg, spec_id: str) -> bool:
    try:
        SP._find_spec(SP._specs_dir(cfg), spec_id)
        return True
    except (OSError, ValueError, SystemExit):
        return False


def _ideas_or_empty(cfg) -> list:
    try:
        return list(_ideas(cfg))
    except (OSError, ValueError):
        return []


def _activity(state: dict, ledger: list, now=None) -> dict:
    """What the campaign is doing now: an open persona run, gates, or idle."""
    open_run = None
    for ev in ledger:
        if ev.get("type") == "run.start":
            open_run = ev
        elif ev.get("type") == "run.end" and open_run and ev.get("run_id") == open_run.get("run_id"):
            open_run = None
    if open_run and state.get("state") not in TERMINAL_STATES:
        return {"kind": "run", "persona": open_run.get("persona"), "model": open_run.get("model"),
                "effort": open_run.get("effort"), "run_id": open_run.get("run_id"),
                "elapsed_s": _age(open_run.get("ts"), now), "timeout_s": open_run.get("timeout_s"),
                "max_budget_usd": open_run.get("max_budget_usd")}
    if state.get("state") in ("GATING", "CLOSING"):
        return {"kind": "gates"}
    return {"kind": "idle"}


def _gate_summary(state: dict) -> dict | None:
    g = state.get("last_gate")
    if not g:
        return None
    return {"gate_seq": g.get("gate_seq"), "ok": g.get("ok"),
            "results": [{"name": r.get("name"), "ok": r.get("ok"), "required": r.get("required"),
                         "duration_s": r.get("duration_s"),
                         **({"summary": r["summary"]} if r.get("summary") else {})}
                        for r in g.get("results") or []]}


def _verdict_summary(state: dict) -> dict | None:
    v = state.get("last_verdict")
    if not v:
        return None
    verdict = v.get("verdict") or {}
    summary = str(verdict.get("summary") or "").strip()
    return {"persona": v.get("persona"), "run": v.get("run"), "status": verdict.get("status"),
            "summary": summary.splitlines()[0] if summary else ""}


def review_short_name(persona: str) -> str:
    """`conformance_reviewer` -> `conformance` (SPEC-0277)."""
    return str(persona).removesuffix("_reviewer")


def _review_chips(state: dict, ledger: list, reviewers, now=None) -> list:
    """One chip per reviewer persona for the current loop (SPEC-0277): `{persona, run_id,
    status, verdict, score, counts, elapsed_s}` with `status` running | pass | fix | failed |
    not_run. `reviewers=None` is the broken-profile fallback: the registered reviewer personas
    observed in the current-loop verdicts or the window, in `REVIEWER_PERSONAS` order.

    The window (rules `running` and `failed`) opens at the last transition into REVIEWING and
    exists only while the campaign is in REVIEWING; outside it only a valid current-loop
    verdict can show, so after a fix route (loops incremented) every chip is `not_run`."""
    loop = int((state.get("counters") or {}).get("loops", 0))
    stored = (state.get("last_review") or {}).get("verdicts") or {}
    window = []
    if state.get("state") == "REVIEWING":
        start = max((i for i, ev in enumerate(ledger) if ev.get("type") == "transition"
                     and ev.get("to") == "REVIEWING"), default=-1)
        window = ledger[start + 1:]
    open_runs, ended = {}, set()
    for ev in window:
        if ev.get("type") == "run.start":
            open_runs[ev.get("run_id")] = ev
        elif ev.get("type") == "run.end":
            open_runs.pop(ev.get("run_id"), None)
            ended.add(ev.get("persona"))
    running = {ev.get("persona"): ev for ev in open_runs.values()}
    if reviewers is None:
        seen = {p for p, v in stored.items() if isinstance(v, dict) and v.get("loop") == loop}
        seen |= {ev.get("persona") for ev in window if ev.get("type") == "run.start"}
        reviewers = [p for p in PS.REVIEWER_PERSONAS if p in seen]
    chips = []
    for persona in reviewers:
        chip = {"persona": persona, "run_id": None, "status": "not_run", "verdict": None,
                "score": None, "counts": None, "elapsed_s": None}
        entry = stored.get(persona)
        valid = isinstance(entry, dict) and entry.get("loop") == loop
        if persona in running:
            ev = running[persona]
            chip.update(status="running", run_id=ev.get("run_id"),
                        elapsed_s=_age(ev.get("ts"), now))
        elif valid:
            counts = dict.fromkeys(SEVERITIES, 0)
            for f in entry.get("findings") or []:
                if isinstance(f, dict) and f.get("severity") in counts:
                    counts[f["severity"]] += 1
            chip.update(status=entry.get("verdict") if entry.get("verdict") in ("pass", "fix")
                        else "failed", run_id=entry.get("run"), verdict=entry.get("verdict"),
                        score=entry.get("score"), counts=counts)
        elif persona in ended:
            chip["status"] = "failed"
        chips.append(chip)
    return chips


def _spec_review_rows(cfg, now, recent_hours) -> list[dict]:
    """SPEC-0217: standalone `nw spec review` runs from the SPEC-0210 journal — running ones,
    and ended or failed ones within `recent_hours`; running first (oldest start first), then
    the rest, most recent end first."""
    import datetime as dt
    def _num(v):
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
        return v if ok else None

    def _timeout(v):                                 # malformed or absurd counts as 0
        v = _num(v)
        return v if v is not None and abs(v) <= 10 * 365 * 86400 else 0

    starts, ends = {}, {}
    for rec in SR.read_journal(cfg, tail=2000):
        rid = rec.get("run_id")
        if not rid or not isinstance(rid, str):      # foreign or hand-edited record
            continue
        if rec.get("kind") == "start":
            starts[rid] = rec
        elif rec.get("kind") == "end":
            ends[rid] = rec

    running, done = [], []
    window = dt.timedelta(hours=recent_hours)
    for rid in dict.fromkeys([*starts, *ends]):
        start, end = starts.get(rid), ends.get(rid)
        started = SR._ts(start.get("ts")) if start else None
        if started is None and end is not None:      # a corrupt start ts: fall back to the end's
            started = SR._ts(end.get("ts"))
        if started is None:
            continue
        row = {"run_id": rid, "spec_id": (start or end).get("spec_id"), "status": None,
               "counts": None, "started_at": started.isoformat(timespec="seconds"),
               "duration_s": None, "cost_usd": None, "ended_at": None}
        if end is None:
            deadline = started + dt.timedelta(seconds=_timeout(start.get("timeout_s")))
            if now < deadline:
                row.update(status="running", duration_s=max(0, int((now - started).total_seconds())))
                running.append((started, row))
                continue
            row["status"] = "failed"
            sort_ts = deadline
            row["ended_at"] = deadline.isoformat(timespec="seconds")
        else:
            sort_ts = SR._ts(end.get("ts"))
            if sort_ts is None:
                continue
            verdict = end.get("verdict")
            failed = bool(end.get("error")) or not verdict or not isinstance(verdict, str)
            counts = end.get("counts")
            row.update(status="failed" if failed else verdict,
                       counts=counts if not failed and isinstance(counts, dict) else None,
                       cost_usd=_num(end.get("cost_usd")), duration_s=_num(end.get("duration_s")),
                       ended_at=sort_ts.isoformat(timespec="seconds"))
        if now - sort_ts <= window:
            done.append((sort_ts, row))
    running.sort(key=lambda x: x[0])
    done.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in running] + [r for _, r in done]


def _gate_progress(cfg, cid, hold, pool, ledger, now) -> dict | None:
    """SPEC-0373: percent complete of the campaign's running gate (see gate_progress)."""
    from . import gate_progress as GP
    try:
        return GP.progress(cfg, cid, since=hold.get("since"), elapsed_s=_age(hold.get("since"), now),
                           events=ledger, remote=pool != "local")
    except Exception:                       # informational: never fails the dashboard
        return None


def _with_progress(view, active) -> dict | None:
    """SPEC-0373: put each gating campaign's progress on its `gate_pools` row."""
    if view:
        prog = {a["campaign"]: (a.get("gate_hold") or {}).get("progress") for a in active}
        for c in view["campaigns"]:
            c["progress"] = prog.get(c["campaign"])
    return view


def _gate_pools_view(cfg, occupancy, active_ids) -> dict | None:
    """SPEC-0372: the registered workers and where each GATING campaign gates (local pool, a
    worker's pool, or waiting for a slot), from data the dashboard already read (no probe, no
    subprocess). None when no worker is registered, so the dashboard stays as it was."""
    from . import workers as WK
    if not occupancy:
        return None
    ws = WK.load(cfg)
    if not ws:
        return None
    occ_w = occupancy.get("workers") or {}
    workers = []
    for w in ws:
        held = [h.get("campaign") for h in (occ_w.get(w["name"]) or {}).get("held") or []]
        workers.append({"name": w["name"], "enabled": bool(w["enabled"]),
                        "cooldown": bool(WK.in_cooldown(w)), "draining": bool(w.get("draining")),
                        "updating": bool(WK.update_pending(w)),
                        "slots": w["slots"], "held": len(held), "campaigns": held})
    where = {}
    for h in occupancy.get("held") or []:
        where[h.get("campaign")] = "local"
    for w in workers:
        for c in w["campaigns"]:
            where[c] = w["name"]
    for w in occupancy.get("waiting") or []:
        where.setdefault(w.get("campaign"), "waiting")
    # SPEC-0381: standalone gate jobs (`nw gate run`) hold worker slots with a `job` holder
    jobs = [{"job": h["job"], "by": h.get("by"), "project": h.get("project"),
             "branch": h.get("branch"), "where": name}
            for name, wv in occ_w.items() for h in (wv or {}).get("held") or [] if h.get("job")]
    jobs += [{"job": w["job"], "by": w.get("by"), "project": w.get("project"),
              "branch": w.get("branch"), "where": "waiting"}
             for w in occupancy.get("waiting") or [] if w.get("job")]
    return {"local": {"slots": occupancy.get("slots"), "held": len(occupancy.get("held") or [])},
            "workers": workers,
            "campaigns": [{"campaign": c, "where": where[c]} for c in active_ids if c in where],
            "gate_jobs": jobs}


def _gate_occupancy(cfg) -> dict | None:
    """SPEC-0326: the host-wide gate-slot snapshot for the dashboard, None when unreadable."""
    from . import gate_slots
    try:
        return gate_slots.occupancy(cfg)
    except (OSError, ValueError):
        return None


# ---- SPEC-0375: is the runner running the checkout's code? -----------------------------------

_CODE_MEMO: dict = {}


def _git_watch_files(root) -> list:
    """The files whose stat changes when the checkout's HEAD may have moved: `HEAD`, the ref it
    names and `packed-refs` (a linked worktree's `.git` file and `commondir` are followed)."""
    root = Path(root)
    gitdir = root / ".git"
    if gitdir.is_file():
        txt = gitdir.read_text(encoding="utf-8").strip()
        if txt.startswith("gitdir:"):
            gitdir = root / txt[len("gitdir:"):].strip()
    common = gitdir
    if (gitdir / "commondir").is_file():
        common = gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()
    files = [gitdir / "HEAD", common / "packed-refs"]
    head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
    if head.startswith("ref:"):
        files.append(common / head[len("ref:"):].strip())
    return files


def _stat_fingerprint(paths) -> tuple:
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append((str(p), st.st_ino, st.st_size, st.st_mtime_ns))
        except OSError:
            out.append((str(p), None))
    return tuple(out)


def _checkout_head(root) -> str | None:
    """`git rev-parse HEAD` of `root`, re-run only when the stat fingerprint of its git HEAD
    files changed: a steady-state watch refresh spawns no process."""
    try:
        key = _stat_fingerprint(_git_watch_files(root))
    except OSError:
        key = None
    memo = _CODE_MEMO.get(("head", str(root)))
    if key is not None and memo is not None and memo[0] == key:
        return memo[1]
    head = _git_out(root, "rev-parse", "HEAD")
    _CODE_MEMO[("head", str(root))] = (key, head)
    return head


def _journal_start_sha(cfg, pid, max_bytes: int = 1 << 20) -> str | None:
    """`code_sha` of the newest journal `start` record by `pid` in the last `max_bytes` of
    `runner.jsonl` (a runner that predates the lease's `code_sha`), memoised per pid and lease
    start / re-exec time."""
    info = L.read_lease(cfg) or {}
    key = ("start", pid, info.get("started_at"), info.get("restarted_at"))
    if key in _CODE_MEMO:
        return _CODE_MEMO[key]
    sha = None
    try:
        with open(runner_journal_path(cfg), "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        data = b""
    for raw in reversed(data.split(b"\n")):
        if b'"start"' not in raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("kind") == "start" and rec.get("pid") == pid:
            sha = rec.get("code_sha")
            break
    _CODE_MEMO[key] = sha
    return sha


def runner_code_status(cfg, lease: dict, root=None) -> dict | None:
    """SPEC-0375: `{runner_sha, head_sha, stale, changed, pending_reexec}` for the held lease
    `lease` (a `lease_status`) against the nightwright checkout `root` (default the one this
    package is imported from). `stale` when nightwright code (`_CODE_PATHS`) changed between
    the two, or the diff failed. None when no runner holds the lease, there is no checkout, or
    the runner's sha is unknown (it predates SPEC-0375 and its start record has none)."""
    if not (lease or {}).get("held"):
        return None
    root = root or code_checkout()
    if root is None:
        return None
    runner_sha = lease.get("code_sha") or _journal_start_sha(cfg, lease.get("pid"))
    head = _checkout_head(root) if runner_sha else None
    if not runner_sha or not head:
        return None
    if head == runner_sha:
        changed = []
    else:
        key = ("diff", str(root), runner_sha, head)
        if key not in _CODE_MEMO:
            _CODE_MEMO[key] = _changed_paths(root, runner_sha, head, _CODE_PATHS)
        changed = _CODE_MEMO[key]
    return {"runner_sha": runner_sha, "head_sha": head,
            "stale": changed is None or bool(changed),
            "changed": None if changed is None else len(changed),
            "pending_reexec": ((lease.get("draining") or {}).get("reason") == "code_changed")}


def _runner_code_safe(cfg, lease) -> dict | None:
    try:                                          # a display hint never breaks the dashboard
        return runner_code_status(cfg, lease)
    except Exception:                             # noqa: BLE001
        return None


def dashboard_payload(cfg, *, project=None, recent=5, recent_hours=24, now=None) -> dict:
    """`nw.queue.dashboard.v1`: everything `nw queue watch` shows — the runner, the active
    campaigns with what each is doing now, what needs attention, the backlog, and what was
    recently delivered."""
    import datetime as dt
    from . import journey as JY
    now = now or dt.datetime.now().astimezone()
    states = {s.get("id"): s for s in store.list_campaigns(cfg, include_terminal=True,
                                                            project=project)}
    base = queue_payload(cfg, include_terminal=True, project=project, now=now)
    occupancy = _gate_occupancy(cfg)                  # SPEC-0326: who holds / waits for gates
    slots = {**base["slots"], "over": max(0, base["slots"]["busy"] - base["slots"]["total"])}
    active, needs_you, conductor, waiting, backlog, delivered = [], [], [], [], [], []
    spend_today = 0.0
    reviewers_cache: dict = {}
    for it in base["items"]:
        st = states.get(it["campaign"]) or {}
        title = _spec_title(st)
        updated = st.get("updated_at") or st.get("created_at")
        try:
            if updated and dt.datetime.fromisoformat(updated).astimezone().date() == now.date():
                spend_today += it["cost_usd"]
        except ValueError:
            pass
        state = it["state"]
        if state in TERMINAL_STATES:
            age = _age(updated, now)
            if age is not None and age <= recent_hours * 3600:
                close = st.get("close") or {}
                # SPEC-0410: a stable finish time (the transition into the terminal state),
                # since `updated_at` moves with any later write to the closed campaign
                finished = next((e.get("ts") for e in reversed(store.read_ledger(cfg, it["campaign"]))
                                 if e.get("type") == "transition" and e.get("to") == state), None)
                delivered.append({
                    "campaign": it["campaign"], "spec_id": it["spec_id"], "spec_title": title,
                    "kind": it["kind"], "merge_branch": it["merge_branch"], "label": it["label"],
                    "project": it["project"], "state": state,
                    "merged_sha": close.get("merged_sha"), "pr_url": close.get("pr_url"),
                    "cost_usd": it["cost_usd"],
                    "duration_s": (_age(st.get("created_at"), now) or 0) - age,
                    "finished_age_s": age, "state_since": finished or updated})   # SPEC-0410
            continue
        if state == "WAITING":
            wf = JY.waiting_for(st, [], busy=slots["busy"], total=slots["total"], now=now)
            waiting.append({**it, "spec_title": title,
                            "resume_to": (st.get("waiting") or {}).get("resume_to"),
                            "waiting_s": _age((st.get("waiting") or {}).get("since"), now),
                            "state_since": (st.get("waiting") or {}).get("since"),   # SPEC-0410
                            "waiting_for": wf, "why": JY.why(st, [], wf)})
            continue
        if state == "QUEUED":
            backlog.append({"position": 0, "priority": it["priority"], "ready": True,
                            "blocked_by": [], **(it.get("backlog") or {}),
                            "campaign": it["campaign"], "spec_id": it["spec_id"],
                            "kind": it["kind"], "merge_branch": it["merge_branch"],
                            "label": it["label"], "state_since": it["created_at"],   # SPEC-0410
                            "spec_title": title, "project": it["project"], "age_s": it["age_s"],
                            "size": it["size"], "tone": it["tone"], "profile": it["profile"],
                            "profile_source": it["profile_source"]})
            b = backlog[-1]
            b["waiting_for"] = JY.waiting_for({"state": "QUEUED",
                                               "backlog": {"blocked_by": b["blocked_by"]}}, [])
            b["why"] = JY.why({"state": "QUEUED"}, [], b["waiting_for"]) if b["waiting_for"] \
                else "ready: admitted at the next free slot"
            continue
        ledger = store.read_ledger(cfg, it["campaign"])
        if state == "PARKED" or it["park_requested"]:
            park = dict(it.get("park") or {})
            parked_at = park.get("parked_at")
            if state != "PARKED":
                # SPEC-0222: a pending request is the conductor's, and its detail is the decision
                req = st.get("park_requested") or {}
                parked_at = req.get("at")
                park = {"reason": None, "owner": "conductor", "decided": True, "overdue": False,
                        "decision": {"text": f"park requested: {req.get('detail') or req.get('reason')}",
                                     "ts": req.get("at"), "by": req.get("by") or "human"},
                        "parked_at": parked_at}
            row = {**it, "spec_title": title, "park": park or None,
                   "park_age_s": _age(parked_at, now), "last_verdict": _verdict_summary(st),
                   "state_since": parked_at,   # SPEC-0410
                   "why": JY.why(st, ledger, loops=it["loops"])}
            (needs_you if park.get("owner") == "human" else conductor).append(row)
            if state == "PARKED":
                continue
        key = (st.get("profile"), (st.get("repo") or {}).get("root"))
        if key not in reviewers_cache:
            try:
                reviewers_cache[key] = reviewers_of(resolve_profile(
                    cfg, key[0], project_root=key[1]))
            except (ValueError, OSError):       # SPEC-0265: a broken profile never fails the view
                reviewers_cache[key] = None
        reviews = _review_chips(st, ledger, reviewers_cache[key], now)
        phases = phases_for(st)
        if state != "REVIEWING" and all(c["status"] == "not_run" for c in reviews):
            reviews = []
        visits, elided = JY.track(ledger, state)
        wf = JY.waiting_for(st, ledger, occupancy, now=now)
        hold = next((h for h in (occupancy or {}).get("held") or []
                     if h.get("campaign") == it["campaign"]), None)
        pool = "local" if hold else None
        if not hold:                       # SPEC-0372: a hold in a registered worker's pool
            for wn, wv in ((occupancy or {}).get("workers") or {}).items():
                hold = next((h for h in wv.get("held") or []
                             if h.get("campaign") == it["campaign"]), None)
                if hold:
                    pool = wn
                    break
        state_since = next((e.get("ts") for e in reversed(ledger)
                            if e.get("type") == "transition" and e.get("to") == state), None)
        active.append({**it, "spec_title": title,
                       "phases": list(phases),
                       "phase_index": phases.index(state) if state in phases else None,
                       "activity": _activity(st, ledger, now), "reviews": reviews,
                       "last_gate": _gate_summary(st), "last_verdict": _verdict_summary(st),
                       "recent_events": ledger[-3:],
                       # SPEC-0326: the journey, the why sentence and what it waits on
                       "track": visits, "track_elided": elided, "waiting_for": wf,
                       "why": JY.why(st, ledger, wf, loops=it["loops"]),
                       "gate_hold": ({"slot": hold.get("slot"), "since": hold.get("since"), "pool": pool,
                                      "held_s": _age(hold.get("since"), now),
                                      "progress": _gate_progress(cfg, it["campaign"], hold, pool,
                                                                 ledger, now)} if hold else None),
                       "recent_lines": JY.recent_lines(ledger, 3),
                       "state_age_s": _age(state_since, now),
                       # SPEC-0410: the transition into this state, else creation (never
                       # `updated_at`, which every heartbeat or cost write moves)
                       "state_since": state_since or st.get("created_at")})
    conductor.sort(key=lambda c: (not (c["park"] or {}).get("overdue"),
                                  -(c.get("park_age_s") or 0)))
    needs_you.sort(key=lambda c: -(c.get("park_age_s") or 0))
    attention = needs_you + conductor
    backlog.sort(key=lambda b: b["position"])
    delivered.sort(key=lambda d: d["finished_age_s"])
    delivered = delivered[:recent]
    ready = sum(1 for b in backlog if b["ready"])
    # SPEC-0217: the journal records carry no project, so --project hides the rows
    spec_reviews = [] if project else _spec_review_rows(cfg, now, recent_hours)
    by_project: dict = {}
    waiting.sort(key=lambda w: (w.get("priority") or 3, -(w.get("waiting_s") or 0)))
    for key, rows in (("active", active), ("needs_you", needs_you), ("conductor", conductor),
                      ("waiting", waiting), ("backlog", backlog), ("delivered", delivered)):
        for r in rows:
            c = by_project.setdefault(r.get("project") or "?", {"active": 0, "needs_you": 0,
                                                                 "conductor": 0, "waiting": 0,
                                                                 "backlog": 0, "delivered": 0})
            c[key] += 1
    return {
        "schema": "nw.queue.dashboard.v1", "generated_at": now.isoformat(timespec="seconds"),
        "project": project, "lease": base["lease"], "slots": slots,
        "runner_code": _runner_code_safe(cfg, base["lease"]),
        "gate_slots": ({"total": occupancy.get("slots"),
                        "held": [h.get("campaign") for h in occupancy.get("held") or []],
                        "waiting": [w.get("campaign") for w in occupancy.get("waiting") or []]}
                       if occupancy is not None else None),
        "gate_pools": _with_progress(_gate_pools_view(cfg, occupancy,
                                                      [a["campaign"] for a in active]), active),
        "totals": {"active": len(active), "attention": len(attention), "waiting": len(waiting),
                   "needs_you": len(needs_you), "conductor_handling": len(conductor),
                   "conductor_overdue": sum(1 for c in conductor if c["park"].get("overdue")),
                   "backlog": len(backlog),
                   "reviews_running": sum(c["status"] == "running"
                                          for it in active for c in it["reviews"])
                   + sum(r["status"] == "running" for r in spec_reviews),
                   "ready": ready, "blocked": len(backlog) - ready, "delivered": len(delivered),
                   "spend_today_usd": round(spend_today, 4), "by_project": by_project},
        "active": active, "needs_you": needs_you, "conductor_handling": conductor,
        "attention": attention, "spec_reviews": spec_reviews, "waiting": waiting,
        "backlog": backlog, "delivered": delivered,
    }


# ---- admission (nw queue add) --------------------------------------------------------------------

def _split_origin(cfg, spec_id: str, inflight: dict | None = None,
                  project: str | None = None) -> dict | None:
    """SPEC-0294: the split campaign that wrote `spec_id` as one of its parts. With `inflight`
    (a state dict, not MERGED on disk yet) only that state counts: it must be `kind: "split"`
    and list the id, and there is no disk read. Without it, the durable MERGED split campaign
    that lists the id. With `project` (SPEC-0327: spec ids are per project) the origin must be
    a campaign of that project."""
    def lists(st):
        return (st.get("kind") == "split"
                and any(p.get("spec_id") == spec_id
                        for p in ((st.get("split") or {}).get("parts") or [])))
    if inflight is not None:
        same = project is None or (inflight.get("project") or INTERNAL_PROJECT) == project
        return inflight if same and lists(inflight) else None
    for st in store.list_campaigns(cfg, include_terminal=True, state="MERGED", project=project):
        if lists(st):
            return st
    return None


def resolve_after(cfg, refs, *, project=None, own=None) -> list[dict]:
    """SPEC-0423: canonical `after` entries `[{"ref", "kind"}]` for `--after` refs: a campaign id
    (its directory exists), an idea id or a spec id of `project`. De-duplicated, order kept.
    ValueError `--after X: not found` for a ref that resolves to nothing now, or one that is the
    campaign's own idea/spec (`own`)."""
    out = []
    for raw in refs or ():
        raw = str(raw or "").strip()
        if not raw:
            continue
        ref = _canon_id(raw)
        if store.campaign_dir(cfg, raw).is_dir():
            entry = {"ref": raw, "kind": "campaign"}
        elif _IDEA_ID_RE.match(ref):
            if _idea_task(cfg, ref) is None:
                raise ValueError(f"--after {raw}: not found (no such idea)")
            entry = {"ref": ref, "kind": "idea"}
        elif _ANY_ID_RE.match(ref):
            try:
                _project_spec_path(cfg, ref, project)
            except (ValueError, OSError, KeyError):
                raise ValueError(f"--after {raw}: not found (no such spec)")
            entry = {"ref": ref, "kind": "spec"}
        else:
            raise ValueError(f"--after {raw}: not found")
        if own and _canon_id(own) == entry["ref"]:
            raise ValueError(f"--after {raw}: a campaign cannot wait on itself")
        if entry not in out:
            out.append(entry)
    return out


def _after_status(cfg, after_plan, project, repo_root) -> list[str]:
    """Dry-run text per `after` entry: `IDEA-874 (open, campaign … IMPLEMENTING)` (SPEC-0423)."""
    pseudo = {"project": project, "repo": {"root": str(repo_root)}}
    out = []
    for e in after_plan:
        met, why = after_met(cfg, pseudo, e)
        out.append(f"{e['ref']} ({e['kind']}, " + (
            "met" if met else why.split("(after: ", 1)[-1].rstrip(")")) + ")")
    return out


def _idea_after_refs(task) -> list[str]:
    """The ids in an idea's `:AFTER:` property (space/comma separated)."""
    from ..org_write import parse_after_refs
    return parse_after_refs(task.properties.get("AFTER") if task is not None else "")


def _note_after(cfg, state: dict, by: str) -> None:
    """Record the `after` list on the campaign's ledger (SPEC-0423)."""
    if state.get("after"):
        store.append_event(cfg, state["id"], "note", {
            "text": "after: " + ", ".join(f"{e['ref']} ({e['kind']})" for e in state["after"]),
            "author": "queue"}, by=by)


def _campaign_runner_pin(runner) -> dict:
    """SPEC-0436: `{runner}` when `--runner` was passed; empty when the campaign stays live."""
    if runner is None:
        return {}
    if runner not in ("claude", "cursor", "pi"):
        raise ValueError(f"unknown runner {runner!r} (expected claude, cursor or pi)")
    return {"runner": runner}


def _campaign_lean_pin(lean_request, cohort) -> dict:
    """SPEC-0558: `{lean_request, cohort}` for a campaign queued with `--lean` /
    `--lean-flags` / `--cohort`; empty when none was given (the campaign follows the config,
    exactly as before)."""
    req = None if lean_request is None else TR.lean_request(lean_flags=list(lean_request))
    tag = TR.campaign_cohort(cohort, req)
    return {**({"lean_request": req} if req is not None else {}),
            **({"cohort": tag} if tag else {})}


def plan_add(cfg, spec_id, *, project=None, profile=None, base=None, force=False,
             priority=PRIORITY_DEFAULT, split_origin=None, after=(), runner=None,
             lean_request=None, cohort=None) -> dict:
    """Resolve everything `nw queue add` needs and return the plan (no writes). Raises
    ValueError with the fix when the spec is not admissible. SPEC-0294: a spec with no review
    that a split campaign wrote as a part is admitted as `split_child` (`split_origin` is the
    in-flight split campaign; without it the durable MERGED one)."""
    spec_id = (spec_id or "").strip()
    if not spec_id:
        raise ValueError("spec id is required")
    priority = check_priority(priority)
    outbound = SP._is_outbound_spec_id(spec_id)
    if project and project != INTERNAL_PROJECT and not outbound:
        # SPEC-0327: a second project's bare id lives in that project's repo, not wt's store
        try:
            path = _project_spec_path(cfg, spec_id, project)
        except ValueError as e:
            raise ValueError(f"{spec_id} not found in project {project}: {e}")
    else:
        path = SP.resolve_spec_path(cfg, spec_id)
    fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    fm = fm or {}
    status = str(fm.get("status") or "")
    if status != "accepted" and not (force and status == "in-progress"):
        raise ValueError(f"{spec_id} is {status or 'missing status'!r}; only `accepted` specs are "
                         f"queued (pass --force for in-progress)")
    if outbound:
        project = project or SP.outbound_project(path, fm)
    else:
        project = project or "Meta-Tools"
    pcfg = P.project_config(cfg, project)
    root = Path(pcfg["_root"])
    if outbound:
        target = fm.get("target_spec_path")
        if not target or not Path(target).is_file():
            raise ValueError(f"{spec_id}: portable spec not found at {target!r}; run "
                             f"`nw spec export {spec_id}` first")
        spec_path = Path(target).resolve()
    else:
        spec_path = path.resolve()
    try:
        rel = str(spec_path.relative_to(root.resolve()))
    except ValueError:
        raise ValueError(f"{spec_id}: spec file {spec_path} is not inside the project repo {root}")
    active = store.active_campaign_for_spec(cfg, spec_id, project)
    if active:
        raise ValueError(f"{spec_id} already has an active campaign {active['id']} "
                         f"({active.get('state')}); abandon it first")
    revision = None
    origin = None
    if force:                                                          # SPEC-0188
        review, review_doc = "forced", None
    else:
        try:
            review_doc = SR.check_admissible(cfg, path, spec_id)
        except ValueError:
            if SR.latest_review(cfg, spec_id, path) is not None:
                raise
            origin = _split_origin(cfg, spec_id, split_origin, project)  # SPEC-0294
            if origin is None:
                raise
            review_doc = {"admission": "split_child"}
        review = review_doc["admission"]
        if review == "split_child":
            review_doc = None
        if review in ("after_revision", "revise_pending"):             # SPEC-0230 / SPEC-0279
            revision = {"review_sha": review_doc.get("spec_sha256"),
                        "current_sha": review_doc["current_sha"],
                        "findings": review_doc.get("findings") or []}
            if review == "revise_pending":
                revision["pending"] = True
    split = None
    if review == "split_pending":                                      # SPEC-0290
        split = {"review_sha": review_doc["current_sha"],
                 "parts": [{"title": str(p.get("title") or "").strip() or f"Part {i}",
                            "scope": str(p.get("scope") or "")}
                           for i, p in enumerate(review_doc.get("suggested_splits") or [], 1)]}
        review_doc = None       # the review sizes the unsplit work, not writing the parts
    lean_pin = _campaign_lean_pin(lean_request, cohort)                # SPEC-0558
    lean, lean_off = TR.effective_lean(cfg, project,                   # SPEC-0553
                                       request=lean_pin.get("lean_request"))
    pick = select_profile(cfg, flag=profile, review=review_doc,         # SPEC-0219
                          project_root=root, **({"lean": True} if lean["tiers"] else {}))
    prof = resolve_profile(cfg, pick["profile"], project_root=root)
    base = base or (pcfg.get("merge") or {}).get("base") or pcfg.get("base") or "main"
    cid = store.new_campaign_id(cfg, spec_id)
    worktree = P.worktree_root(pcfg, root, project) / cid
    after_plan = resolve_after(cfg, after, project=project, own=spec_id)
    return {
        **({"after": after_plan, "after_status": _after_status(cfg, after_plan, project, root)}
           if after_plan else {}),
        "campaign": cid, "spec_id": spec_id, "project": project, "profile": prof["name"],
        "priority": priority, "force": bool(force), "review": review, "revision": revision, "base": base,
        "repo_root": str(root), "spec_rel_path": rel, "spec_status": status,
        "internal": not outbound, "worktree": str(worktree), "branch": f"nw/{cid}",
        "cap_usd": prof.get("campaign_cap_usd"), "loop_max": prof.get("loop_max"),
        "slots": prof.get("slots"), "merge_policy": (pcfg.get("merge") or {}).get("policy"),
        "size": pick["size"], "tone": pick["tone"],
        "sizing_rationale": (review_doc or {}).get("rationale") if pick["size"] else None,
        "profile_source": pick["source"], "profile_reason": pick["reason"],
        "review_sha256": (review_doc or {}).get("spec_sha256"),
        "profile_sources": list(prof.get("sources") or ()),             # SPEC-0238
        "profile_sha256": profile_sha256(prof),                         # SPEC-0397
        "spec_fm": {"source_idea": str(fm.get("source_idea") or ""),    # SPEC-0292
                    "created": str(fm.get("created") or "")},
        "split": split, "split_origin": origin["id"] if origin else None,
        **_campaign_runner_pin(runner),
        # SPEC-0553: the flags this campaign pins, and the `--profile` asked for (the open
        # re-applies the cap with the spec's hand-written size/tone)
        **({"lean": lean, "lean_off": lean_off}
           if any(lean.values()) or lean_off or "lean_request" in lean_pin else {}),
        **({"profile_flag": profile, "profile_capped_from": pick.get("capped_from")}
           if lean["tiers"] else {}),
        **lean_pin,                                                     # SPEC-0558
    }


def _pin_profile_flag(state: dict, plan: dict) -> None:
    """SPEC-0553: record the `--profile` asked for (`sizing.flag`, and `capped_from` when the
    add capped it) so `_lean_open` re-applies the cap to the flag, not to the default. Only a
    plan made with `lean.tiers` on carries `profile_flag`; without it nothing is written."""
    if "profile_flag" not in plan:
        return
    sizing = state.setdefault("sizing", {})
    sizing["flag"] = plan["profile_flag"]
    if plan.get("profile_capped_from"):
        sizing["capped_from"] = plan["profile_capped_from"]


def _pin_lean_request(state: dict, plan: dict) -> None:
    """SPEC-0558: carry the plan's opt-in onto the queued state; `store.create_campaign` then
    pins `state.lean` from `lean_request`. Nothing is written without an opt-in."""
    if plan.get("lean_request") is not None:
        state["lean_request"] = list(plan["lean_request"])
    if plan.get("cohort"):
        state["cohort"] = plan["cohort"]


def apply_add(cfg, plan: dict, *, by="human") -> dict:
    """Create the campaign in `QUEUED` from a `plan_add` plan. Re-checks the active-campaign
    guard against live state before writing."""
    state = {
        "schema": "nw.campaign.v1", "id": plan["campaign"], "kind": "implement",
        "spec_id": plan["spec_id"], "project": plan["project"], "profile": plan["profile"],
        "state": "QUEUED", "prev_state": None, "created_at": store.now_iso(),
        "priority": check_priority(plan.get("priority", PRIORITY_DEFAULT)),
        "force": bool(plan.get("force")), "backlog": None,
        "profile_sources": list(plan.get("profile_sources") or ()),    # SPEC-0238
        "profile_sha256": plan.get("profile_sha256"),                  # SPEC-0397
        "repo": {"root": plan["repo_root"], "base": plan["base"], "base_sha_at_open": None,
                 "branch": plan["branch"], "worktree": plan["worktree"], "head": None},
        "spec": {"rel_path": plan["spec_rel_path"], "internal": plan["internal"],
                 "sha256_at_open": None},
        "counters": {"loops": 0, "runs": 0, "gate_runs": 0, "gate_retries": 0, "timeout_regates": 0,
                     "triage_retries": 0, "blocked_streak": 0, "close_attempts": 0, "parks": 0},
        "cost": {"total_usd": 0.0, "cap_usd": plan.get("cap_usd"), "by_persona": {}},
        "last_gate": None, "last_verdict": None,
        "pending_brief": None, "park": None, "close": None,
        "admission": plan.get("review"),                               # SPEC-0243
        "sizing": {"size": plan.get("size"), "tone": plan.get("tone"),
                   "rationale": plan.get("sizing_rationale"),
                   "source": plan.get("profile_source"), "reason": plan.get("profile_reason"),
                   "review_sha256": plan.get("review_sha256")},
    }
    if plan.get("runner"):                                             # SPEC-0436
        state["runner"] = plan["runner"]
    _pin_lean_request(state, plan)                                     # SPEC-0558
    _pin_profile_flag(state, plan)                                     # SPEC-0553
    if plan.get("after"):
        state["after"] = list(plan["after"])                           # SPEC-0423
    if plan.get("split_origin"):
        state["split_origin"] = plan["split_origin"]                   # SPEC-0294
    split = plan.get("split")
    if split:                                                          # SPEC-0290
        state["kind"] = "split"
        state["spec"]["id"] = plan["spec_id"]
        state["split"] = {
            "original": plan["spec_id"], "original_rel": plan["spec_rel_path"],
            "review_sha": split["review_sha"],
            "parts": [{**p, "number": None, "rel_path": None, "spec_id": None}
                      for p in split["parts"]]}
    revision = plan.get("revision")
    pending = bool(revision and revision.get("pending"))
    if revision:                                                       # SPEC-0230
        state["pending_brief"] = SR.revision_brief(
            {"spec_sha256": revision["review_sha"], "findings": revision["findings"]})
    if pending:                                                        # SPEC-0279
        # the spec writer amends the spec first; the apply path reads the findings from here
        state["spec"]["id"] = plan["spec_id"]
        state["revision"] = {"review_sha": revision["review_sha"],
                             "findings": revision["findings"]}
        state["pending_brief"]["source"] = "spec_review_revise"
    with store.add_lock(cfg):                                          # SPEC-0209
        active = store.active_campaign_for_spec(cfg, plan["spec_id"], plan.get("project"))
        if active:
            raise ValueError(f"{plan['spec_id']} already has an active campaign {active['id']}")
        store.create_campaign(cfg, state, by=by)
    if plan.get("split_origin"):                                       # SPEC-0294
        store.append_event(cfg, plan["campaign"], "admitted_split_child",
                           {"origin": plan["split_origin"]}, by=by)
    if split:
        store.append_event(cfg, plan["campaign"], "admitted_split",
                           {"review_sha": split["review_sha"], "parts": split["parts"]}, by=by)
    if pending:
        store.append_event(cfg, plan["campaign"], "admitted_revise_pending",
                           {k: v for k, v in revision.items() if k != "pending"}, by=by)
    elif revision:
        store.append_event(cfg, plan["campaign"], "admitted_after_revision", revision, by=by)
    _note_after(cfg, state, by)                                        # SPEC-0423
    _clear_handed_off_on_requeue(cfg, plan["spec_id"], plan["campaign"], by,
                                 project=plan.get("project"))
    _link_spec_idea(cfg, state, plan.get("spec_fm"), by)
    return state


def _link_spec_idea(cfg, state: dict, spec_fm: dict | None, by: str) -> None:
    """SPEC-0292: set `:CAMPAIGN: <cid>` on the spec's source idea (resolved by the SPEC-0267
    identity rule) and record `state["idea_link"]`. Never overwrites a link held by a live
    campaign. Best-effort: runs after `create_campaign`, so any failure is an
    `idea_link_skipped` event and never refuses the add."""
    from .. import org_write as OW
    cid, spec_id = state["id"], state["spec_id"]
    skipped = None
    try:
        task, reason = SP.source_idea_task(cfg, spec_fm)
        if task is None:
            skipped = None if reason == "no source_idea" else reason
        else:
            held = (task.properties.get("CAMPAIGN") or "").strip()
            if held and held != cid:
                try:
                    live = store.read_state(cfg, held).get("state") not in TERMINAL_STATES
                except ValueError:              # no such campaign: the link is dead
                    live = False
                if live:
                    skipped = f"held by {held}"
            if skipped is None:
                idea_id = (task.properties.get("ID") or task.id or "").strip()
                OW.set_property(cfg, task, "CAMPAIGN", cid)
                with store.item_lock(cfg, cid):
                    on_disk = store.read_state(cfg, cid)
                    on_disk["idea_link"] = {"id": idea_id}
                    store.write_state(cfg, cid, on_disk)
                state["idea_link"] = {"id": idea_id}
                store.append_event(cfg, cid, "idea_linked", {"idea_id": idea_id}, by=by)
    except Exception as e:                  # noqa: BLE001 — the campaign exists; never refuse the add
        skipped = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
    if skipped:
        try:
            store.append_event(cfg, cid, "idea_link_skipped",
                               {"spec_id": spec_id, "reason": skipped}, by=by)
        except Exception:                   # noqa: BLE001 — bookkeeping only
            pass


def _release_idea_link(cfg, state: dict, events: list) -> None:
    """SPEC-0292: clear `:CAMPAIGN:` on every idea that still names this campaign (the equality
    is the ownership check; a reused `:ID:` twin is untouched). Looks ideas up by the property,
    not by id, so a never-persisted `idea_link` and rotated twins are handled. Appends
    `idea_unlinked` events; an error becomes a minor `bookkeeping` finding. Never raises."""
    from .. import org_write as OW
    cid = state["id"]
    failed = 0
    try:
        while True:                         # re-scan: a clear shifts lines below it in the file
            todo = [t for t in _ideas(cfg)
                    if (t.properties.get("CAMPAIGN") or "").strip() == cid]
            if len(todo) <= failed:         # a failed clear stays in the list, ahead of the rest
                return
            task = todo[failed]
            idea_id = (task.properties.get("ID") or task.id or "").strip()
            try:
                OW.clear_property(cfg, task, "CAMPAIGN")
                events.append(("idea_unlinked", {"idea_id": idea_id}))
            except Exception as e:          # noqa: BLE001 — bookkeeping must not change the state
                failed += 1
                events.append(("finding", {
                    "persona": "close", "severity": "minor", "gate": "bookkeeping",
                    "title": f"idea unlink failed: {idea_id}", "detail": str(e) or type(e).__name__,
                    "files": []}))
    except Exception as e:                  # noqa: BLE001 — never block a finished campaign
        events.append(("finding", {
            "persona": "close", "severity": "minor", "gate": "bookkeeping",
            "title": "idea unlink failed", "detail": str(e) or type(e).__name__, "files": []}))


def _campaign_on_branch(cfg, project, branch) -> dict | None:
    """The non-terminal campaign of `project` whose branch is `branch`, or None."""
    for st in store.list_campaigns(cfg):
        if st.get("project") == project and (st.get("repo") or {}).get("branch") == branch:
            return st
    return None


docs_only_violations = git.docs_only_violations      # SPEC-0234 (lives in gitops: close shares it)


def plan_merge(cfg, branch, *, project=None, worktree=None, profile=None,
               priority=MERGE_PRIORITY_DEFAULT, note=None, requested_by="human",
               docs_only=False, fixes_red=False) -> dict:
    """SPEC-0233: resolve everything `nw queue merge` needs and return the plan (no writes).
    Raises ValueError naming the fix when the branch cannot be queued for a merge.
    SPEC-0234: `docs_only` also needs every changed path to be on `merge.docs_only_paths`.
    SPEC-0420: `fixes_red` marks a branch that fixes the project's red base: priority 1, full
    suite at close, exempt from the red-base hold; refused when the project is not red."""
    branch = (branch or "").strip()
    if not branch:
        raise ValueError("branch is required")
    priority = check_priority(priority)
    project = project or P.find_project_for_cwd(cfg) or "Meta-Tools"
    if fixes_red:
        if docs_only:
            raise ValueError("--fixes-red and --docs-only exclude each other")
        if not RM.is_red(cfg, project):
            raise ValueError(f"--fixes-red refused: {project} has no open red episode "
                             f"(nw queue sentinel status --project {project})")
        priority = 1
    pcfg = P.project_config(cfg, project)
    root = Path(pcfg["_root"])
    policy = (pcfg.get("merge") or {}).get("policy")
    if policy != "merge":
        raise ValueError(f"{project} merges with policy {policy!r}; `nw queue merge` needs "
                         f"merge.policy: merge")
    base = (pcfg.get("merge") or {}).get("base") or pcfg.get("base") or "main"
    if not git.branch_exists(root, branch):
        raise ValueError(f"branch {branch!r} does not exist in {root}; it must be a local branch")
    if branch == base:
        raise ValueError(f"{branch!r} is the base branch; nothing to merge")
    if git.current_branch(root) == branch:
        raise ValueError(f"the main checkout {root} is on {branch!r}; switch it to {base} first")
    ahead = int(git.git(root, "rev-list", "--count", f"{base}..{branch}").stdout.strip() or 0)
    if ahead < 1:
        raise ValueError(f"nothing to merge: {branch!r} is not ahead of {base}")
    active = _campaign_on_branch(cfg, project, branch)
    if active:
        raise ValueError(f"{branch!r} already has an active campaign {active['id']} "
                         f"({active.get('state')}); abandon it first")
    allow = list((pcfg.get("merge") or {}).get("docs_only_paths") or [])
    changed = []
    if docs_only:
        if not allow:
            raise ValueError(f"--docs-only is disabled for {project}: set "
                             f"merge.docs_only_paths in .nw/project.yaml")
        changed = git.changed_files(root, base, head=branch, renames=False)
        # SPEC-0255: close regenerates declared generated files, so they never count
        regen = [r["path"] for r in (pcfg.get("merge") or {}).get("regenerate") or []]
        bad = docs_only_violations(changed, allow + regen)
        if bad:
            raise ValueError(f"--docs-only refused: {len(bad)} paths outside "
                             f"merge.docs_only_paths ({', '.join(bad[:5])})")
    existing = git.worktree_for_branch(root, branch)
    cid = store.new_campaign_id(cfg, "MERGE")
    if worktree is not None:
        if existing is None or Path(worktree).resolve() != existing:
            raise ValueError(f"--worktree {worktree} is not the worktree of {branch!r} "
                             f"({existing or 'none'}); drop --worktree to let nightwright choose")
    if existing is not None:
        try:
            clean = git.is_clean(existing, untracked=True)
        except (OSError, GitError) as e:
            raise ValueError(f"worktree {existing} of {branch!r} is unusable "
                             f"({getattr(e, 'tail', None) or e}); run `git worktree prune` "
                             f"in {root}") from e
        if not clean:
            raise ValueError(f"worktree {existing} has uncommitted or untracked changes; "
                             f"commit or stash them before queueing the merge")
        wt_path, own = existing, False
    else:
        wt_path, own = P.worktree_root(pcfg, root, project) / cid, True
    if profile and TR.effective_lean(cfg, project)[0]["tiers"]:        # SPEC-0553 cap
        profile = cap_flag(cfg, profile, project_root=root)
    prof = resolve_profile(cfg, profile)
    return {
        "campaign": cid, "kind": "merge", "branch": branch, "project": project,
        "profile": prof["name"], "priority": priority, "base": base, "repo_root": str(root),
        "worktree": str(wt_path), "own_worktree": own,
        "head_at_request": git.rev_parse(root, branch), "ahead": ahead,
        "requested_by": requested_by, "note": note, "cap_usd": prof.get("campaign_cap_usd"),
        "slots": prof.get("slots"), "merge_policy": policy,
        "profile_sha256": profile_sha256(prof),                         # SPEC-0397
        **({"docs_only": True, "docs_only_paths": allow,
            "gates": f"none (docs-only; {len(changed)} paths within merge.docs_only_paths)"}
           if docs_only else {}),
        **({"fixes_red": True, "full_suite": True,
            "gates": "close gates with the full suite (fixes the red base; not held)"}
           if fixes_red else {}),
    }


def apply_merge(cfg, plan: dict, *, by="human") -> dict:
    """Create the merge campaign in `QUEUED` from a `plan_merge` plan. Re-checks the
    one-campaign-per-branch guard against live state before writing."""
    state = {
        "schema": "nw.campaign.v1", "id": plan["campaign"], "kind": "merge",
        "spec_id": None, "project": plan["project"], "profile": plan["profile"],
        "state": "QUEUED", "prev_state": None, "created_at": store.now_iso(),
        "priority": check_priority(plan.get("priority", MERGE_PRIORITY_DEFAULT)),
        "force": False, "backlog": None,
        "profile_sha256": plan.get("profile_sha256"),                  # SPEC-0397
        "repo": {"root": plan["repo_root"], "base": plan["base"], "base_sha_at_open": None,
                 "branch": plan["branch"], "worktree": plan["worktree"], "head": None},
        "spec": None,
        "merge": {"branch": plan["branch"], "requested_by": plan.get("requested_by") or by,
                  "note": plan.get("note"), "head_at_request": plan["head_at_request"],
                  "own_worktree": bool(plan["own_worktree"]),
                  "docs_only": bool(plan.get("docs_only"))},
        "counters": {"loops": 0, "runs": 0, "gate_runs": 0, "gate_retries": 0, "timeout_regates": 0,
                     "triage_retries": 0, "blocked_streak": 0, "close_attempts": 0, "parks": 0},
        "cost": {"total_usd": 0.0, "cap_usd": plan.get("cap_usd"), "by_persona": {}},
        "last_gate": None, "last_verdict": None,
        "pending_brief": None, "park": None, "close": None,
    }
    if plan.get("fixes_red"):                                          # SPEC-0420
        state.update(fixes_red=True, full_suite=True)
    with store.add_lock(cfg):
        active = _campaign_on_branch(cfg, plan["project"], plan["branch"])
        if active:
            raise ValueError(f"{plan['branch']!r} already has an active campaign {active['id']}")
        store.create_campaign(cfg, state, by=by)
    return state


_IDEA_ID_RE = re.compile(r"^IDEA-\d+$")
_ANY_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")


def _open_idea_campaign(cfg, idea_id) -> dict | None:
    """The non-terminal `kind: idea` campaign for `idea_id`, or None."""
    for st in store.list_campaigns(cfg):
        if st.get("kind") == "idea" and _canon_id(_idea_id_of(st) or "") == _canon_id(idea_id):
            return st
    return None


def plan_add_idea(cfg, idea_id, *, project=None, profile=None, base=None,
                  priority=PRIORITY_DEFAULT, after=(), runner=None, lean_request=None,
                  cohort=None) -> dict:
    """`plan_add` for an idea (SPEC-0209): no writes. Refusals, first match wins: unknown idea,
    idea with a `:SPEC:`, closed idea, open campaign for the idea, unloadable project."""
    idea_id = (idea_id or "").strip()
    priority = check_priority(priority)
    lean_pin = _campaign_lean_pin(lean_request, cohort)                # SPEC-0558
    task = _idea_task(cfg, idea_id)
    if task is None:
        raise ValueError(f"unknown idea {idea_id}")
    spec = (task.properties.get("SPEC") or "").strip()
    if spec:
        raise ValueError(f"{idea_id} already has {spec}; queue that spec instead: "
                         f"nw queue add {spec}")
    if not idea_is_open(task):
        raise ValueError(f"{idea_id} is {task.state}; only open ideas (IDEA, INCUBATE, or "
                         f"SPECCED without a :SPEC:) are queued")
    active = _open_idea_campaign(cfg, idea_id)
    if active:
        raise ValueError(f"{idea_id} already has an active campaign {active['id']} "
                         f"({active.get('state')}); abandon it first")
    from .. import idea_link as IL
    agreed = IL.load_agreed_request(cfg, task)
    if agreed and agreed.spec:
        raise ValueError(f"{agreed.req_id} already cites {agreed.spec}")
    project = project or (task.properties.get("PROJECT") or "").strip() or "Meta-Tools"
    pcfg = P.project_config(cfg, project)
    root = Path(pcfg["_root"])
    lean, lean_off = TR.effective_lean(cfg, project, request=lean_pin.get("lean_request"))
    flag_pin = {}
    if profile and lean["tiers"]:                                      # SPEC-0553 cap
        pick = select_profile(cfg, flag=profile, project_root=root, lean=True)
        # the flag is kept for `_lean_open`, which re-picks from `sizing.flag`
        flag_pin = {"profile_flag": profile, "profile_capped_from": pick.get("capped_from"),
                    "profile_source": pick["source"], "profile_reason": pick["reason"]}
        profile = pick["profile"]
    prof = resolve_profile(cfg, profile, project_root=root)
    base = base or (pcfg.get("merge") or {}).get("base") or pcfg.get("base") or "main"
    cid = store.new_campaign_id(cfg, idea_id)
    worktree = P.worktree_root(pcfg, root, project) / cid
    after_plan = resolve_after(cfg, list(after or ()) + _idea_after_refs(task), project=project,
                               own=idea_id)                            # SPEC-0423: `:AFTER:` too
    return {
        **({"after": after_plan, "after_status": _after_status(cfg, after_plan, project, root)}
           if after_plan else {}),
        "kind": "idea", "idea_id": idea_id, "campaign": cid, "project": project,
        "profile": prof["name"], "priority": priority, "base": base, "repo_root": str(root),
        "worktree": str(worktree), "branch": f"nw/{cid}",
        "cap_usd": prof.get("campaign_cap_usd"), "loop_max": prof.get("loop_max"),
        "slots": prof.get("slots"), "merge_policy": (pcfg.get("merge") or {}).get("policy"),
        "profile_sha256": profile_sha256(prof),                         # SPEC-0397
        **_campaign_runner_pin(runner),
        **({"lean": lean, "lean_off": lean_off}                         # SPEC-0558
           if "lean_request" in lean_pin else {}),
        **flag_pin,                                                     # SPEC-0553
        **lean_pin,
    }


def apply_add_idea(cfg, plan: dict, *, by="human") -> dict:
    """Create the idea campaign in `QUEUED` from a `plan_add_idea` plan and link the idea
    (`:CAMPAIGN:`). The duplicate re-check and the create run under the queue-wide add lock."""
    from .. import explore as EX
    from .. import org_write as OW
    idea_id = plan["idea_id"]
    with store.add_lock(cfg):
        active = _open_idea_campaign(cfg, idea_id)
        if active:
            raise ValueError(f"{idea_id} already has an active campaign {active['id']} "
                             f"({active.get('state')}); abandon it first")
        task = _idea_task(cfg, idea_id)
        if task is None:
            raise ValueError(f"unknown idea {idea_id}")
        from .. import idea_link as IL
        agreed = IL.load_agreed_request(cfg, task)
        if agreed and agreed.spec:
            raise ValueError(f"{agreed.req_id} already cites {agreed.spec}")
        idea = EX.idea_show_payload(cfg, task)
        if agreed:
            idea["source_request"] = agreed.req_id
            idea["summary"] = agreed.summary
        idea["epic"] = task.properties.get("EPIC")
        idea["state_before"] = task.state
        state = {
            "schema": "nw.campaign.v1", "id": plan["campaign"], "kind": "idea",
            "spec_id": None, "idea": idea, "project": plan["project"],
            "profile": plan["profile"], "state": "QUEUED", "prev_state": None,
            "created_at": store.now_iso(),
            "priority": check_priority(plan.get("priority", PRIORITY_DEFAULT)),
            "force": False, "backlog": None,
            "profile_sha256": plan.get("profile_sha256"),              # SPEC-0397
            "repo": {"root": plan["repo_root"], "base": plan["base"], "base_sha_at_open": None,
                     "branch": plan["branch"], "worktree": plan["worktree"], "head": None},
            "spec": {"id": None, "rel_path": None, "internal": True},
            "counters": {"loops": 0, "runs": 0, "gate_runs": 0, "gate_retries": 0, "timeout_regates": 0,
                         "triage_retries": 0, "blocked_streak": 0, "close_attempts": 0,
                         "parks": 0},
            "cost": {"total_usd": 0.0, "cap_usd": plan.get("cap_usd"), "by_persona": {}},
            "last_gate": None, "last_verdict": None,
            "pending_brief": None, "park": None, "close": None,
        }
        if plan.get("runner"):                                         # SPEC-0436
            state["runner"] = plan["runner"]
        _pin_lean_request(state, plan)                                 # SPEC-0558
        if "profile_flag" in plan:                                     # SPEC-0553
            state["sizing"] = {"size": None, "tone": None,
                               "source": plan.get("profile_source"),
                               "reason": plan.get("profile_reason")}
            _pin_profile_flag(state, plan)
        if plan.get("after"):
            state["after"] = list(plan["after"])                       # SPEC-0423
        store.create_campaign(cfg, state, by=by)
        _note_after(cfg, state, by)
        OW.set_property(cfg, task, "CAMPAIGN", plan["campaign"])
    return state


# ---- batch add (SPEC-0276) -----------------------------------------------------------------------

def _project_spec_path(cfg, spec_id, project) -> Path:
    """SPEC-0327: the file of a bare `spec_id` of `project`: a second project's id lives in its
    own repo's `docs/specs`, wt's store only holds wt's (and outbound) ids."""
    if project and project != INTERNAL_PROJECT and not SP._is_outbound_spec_id(spec_id):
        return SP._find_spec(Path(P.project_config(cfg, project)["_root"]) / "docs" / "specs",
                             spec_id)
    return SP.resolve_spec_path(cfg, spec_id)


def _dep_project(spec_id, dep, project) -> str | None:
    """The project a same-namespace dependency of `spec_id` belongs to (None: outbound id)."""
    if SP._is_outbound_spec_id(dep):
        return None
    return project or INTERNAL_PROJECT


def _spec_source(cfg, spec_id, project=None) -> Path:
    """The file `backlog_readiness` reads for the spec: the target repo's copy for an exported
    outbound spec (else its outbox copy), the project's (or wt's) file for a bare one."""
    path = _project_spec_path(cfg, spec_id, project)
    if SP._is_outbound_spec_id(spec_id):
        fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        target = (fm or {}).get("target_spec_path") if isinstance(fm, dict) else None
        if target and Path(target).is_file():
            return Path(target)
    return path


def _depends_on(cfg, spec_id, project=None) -> list[str]:
    """`depends_on` of the spec as a list of ids; [] when the spec is unreadable."""
    try:
        fm, _ = split_frontmatter(_spec_source(cfg, spec_id, project).read_text(
            encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        return []
    deps = (fm.get("depends_on") if isinstance(fm, dict) else None) or []
    return [_canon_id(d) for d in ([deps] if isinstance(deps, str) else deps)]


def _same_namespace(spec_id, dep) -> bool:
    """An outbound spec's `depends_on` names ids of its target repo, so only another outbound id
    can be a spec of the batch (and `SPEC-NNNN` there is not nightwright's own spec of that number)."""
    return SP._is_outbound_spec_id(spec_id) == SP._is_outbound_spec_id(dep)


def _canon_id(raw) -> str:
    """`SPEC-0003` for `spec-0003` (specs resolve by number, so case must not make two ids)."""
    s = str(raw).strip()
    return s.upper() if _ANY_ID_RE.match(s) else s


def expand_epic(cfg, epic_id) -> tuple[list[str], list[dict]]:
    """SPEC-0276: `(ids, skipped)` for `nw queue add --epic`. `ids` are the accepted child specs
    and the open, spec-less `:EPIC:` ideas without an active campaign; everything else under the
    epic goes to `skipped` as `{"id", "reason"}`. Raises ValueError for an unknown epic."""
    epic_id = _canon_id(epic_id or "")
    SP.resolve_spec_path(cfg, epic_id)
    ids: list[str] = []
    skipped: list[dict] = []
    specs_dir = SP._specs_dir(cfg)
    for path in sorted(Path(specs_dir).glob("*.md")):
        try:
            fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(fm, dict) or _canon_id(fm.get("parent") or "") != epic_id:
            continue
        sid, status = _canon_id(fm.get("id") or ""), str(fm.get("status") or "")
        if not sid:
            skipped.append({"id": path.stem, "reason": "no id"})
            continue
        if status != "accepted":
            skipped.append({"id": sid, "reason": f"status {status or 'missing'}"})
            continue
        active = store.active_campaign_for_spec(cfg, sid, INTERNAL_PROJECT)   # wt's own store
        if active:
            skipped.append({"id": sid, "reason": f"already queued ({active['id']})"})
        else:
            ids.append(sid)
    for task in _ideas(cfg):
        if _canon_id(task.properties.get("EPIC") or "") != epic_id:
            continue
        iid = _canon_id(task.properties.get("ID") or "") or task.id
        spec = (task.properties.get("SPEC") or "").strip()
        if spec:
            skipped.append({"id": iid, "reason": f"has {spec}; covered by the spec"})
        elif not idea_is_open(task):
            skipped.append({"id": iid, "reason": f"idea is {task.state}"})
        elif not _IDEA_ID_RE.match(iid):
            skipped.append({"id": iid, "reason": "no IDEA-NNN :ID:; give it one to queue it"})
        else:
            active = _open_idea_campaign(cfg, iid)
            if active:
                skipped.append({"id": iid, "reason": f"already queued ({active['id']})"})
            else:
                ids.append(iid)
    return ids, skipped


def _cycle_path(start, deps_of, nodes) -> list[str] | None:
    """A dependency path `start -> … -> start` through `nodes`, or None when `start` is on no
    cycle."""
    stack, seen = [(start, [start])], set()
    while stack:
        node, path = stack.pop()
        for dep in deps_of.get(node, ()):
            if dep == start:
                return path + [start]
            if dep in nodes and dep not in seen:
                seen.add(dep)
                stack.append((dep, path + [dep]))
    return None


def _order_items(spec_ids, deps_of) -> tuple[list[str], dict]:
    """Kahn's algorithm over the in-batch edges (spec `depends_on`, idea `:AFTER:`; SPEC-0423),
    ties by input position. Returns `(order, cycles)`: `cycles` maps every item on a cycle to
    its error text; the items that are stuck behind a cycle follow in input order."""
    ins = set(spec_ids)
    pending = {s: {d for d in deps_of.get(s, ()) if d in ins} for s in spec_ids}
    order: list[str] = []
    while True:
        ready = [s for s in spec_ids if s in pending and not pending[s]]
        if not ready:
            break
        nxt = ready[0]
        order.append(nxt)
        del pending[nxt]
        for deps in pending.values():
            deps.discard(nxt)
    cycles = {}
    for s in [s for s in spec_ids if s in pending]:
        path = _cycle_path(s, deps_of, set(pending))
        if path:
            cycles[s] = "dependency cycle: " + " -> ".join(path)
    return order + [s for s in spec_ids if s in pending], cycles


_order_specs = _order_items                  # the pre-SPEC-0423 name


def _idea_edges(cfg, idea_ids, batch_ids) -> dict:
    """SPEC-0423: `{idea_id: [in-batch ids it follows]}` from each idea's `:AFTER:`. An idea ref
    that has a `:SPEC:` is the batch's spec when that spec is in the batch."""
    out = {}
    for iid in idea_ids:
        edges = []
        for ref in _idea_after_refs(_idea_task(cfg, iid)):
            ref = _canon_id(ref)
            task = _idea_task(cfg, ref) if _IDEA_ID_RE.match(ref) else None
            spec = ((task.properties.get("SPEC") or "").strip() if task else "")
            for cand in (ref, _canon_id(spec) if spec else None):
                if cand and cand in batch_ids and cand != iid and cand not in edges:
                    edges.append(cand)
        out[iid] = edges
    return out


def _dangling(cfg, spec_id, deps, in_batch, project=None) -> list[str]:
    """The `depends_on` ids of `spec_id` that would block it forever: not satisfied, not in the
    batch and without an active campaign. Dependencies resolve next to the spec file the
    scheduler reads (`backlog_readiness`), so an outbound spec looks in its target repo."""
    try:
        spec_dir = _spec_source(cfg, spec_id, project).parent
    except (OSError, ValueError, KeyError):
        return []
    out = []
    for dep in deps:
        same = _same_namespace(spec_id, dep)
        if same and dep in in_batch:
            continue
        try:
            fm, _ = split_frontmatter(_find_dep_spec(spec_dir, dep).read_text(encoding="utf-8"))
            status = str((fm or {}).get("status") or "")
        except (OSError, ValueError):
            status = None
        if status in DEPENDENCY_SATISFIED or (same and store.active_campaign_for_spec(
                cfg, dep, _dep_project(spec_id, dep, project))):
            continue
        if same and (_dep_campaign(cfg, dep, project) or {}).get("state") in (
                "HANDED_OFF", "MERGED"):            # SPEC-0347: resolves when its PR merges
            continue
        out.append(f"{dep} not found" if status is None
                   else f"{dep} is {status or 'without status'} and not queued")
    return out


def _batch_deps(cfg, spec_ids, project=None) -> tuple[dict, dict]:
    """`(all, edges)`: each spec's `depends_on`, and the part of it that can be a spec of the
    batch (same id namespace)."""
    allv = {i: _depends_on(cfg, i, project) for i in spec_ids}
    return allv, {i: [d for d in ds if _same_namespace(i, d)] for i, ds in allv.items()}


def _batch_totals(items) -> dict:
    plans = [it["plan"] for it in items if "plan" in it]
    return {"ok": len(plans), "errors": sum(1 for it in items if "error" in it),
            "cap_usd_total": sum(p.get("cap_usd") or 0 for p in plans),
            "uncapped": sum(1 for p in plans if p.get("cap_usd") is None)}


def _cascade(items, deps_of) -> None:
    """Give every spec item with an in-batch dependency that has an error `depends on refused
    SPEC-X`, transitively (the error and its plan removal are applied in place)."""
    by_id = {it["id"]: it for it in items}
    changed = True
    while changed:
        changed = False
        for it in items:
            if "error" in it:
                continue
            bad = next((d for d in deps_of.get(it["id"], ())
                        if d in by_id and "error" in by_id[d]), None)
            if bad:
                it["error"] = f"depends on refused {bad}"
                it.pop("plan", None)
                it.pop("would_block", None)
                changed = True


def plan_add_batch(cfg, ids, *, project=None, profile=None, base=None, force=False,
                   priority=PRIORITY_DEFAULT, split_origin=None, after=(), runner=None,
                   lean_request=None, cohort=None) -> dict:
    """SPEC-0276: plan several specs and ideas as one batch (no writes). Each id goes through
    `plan_add` / `plan_add_idea`; on top, specs are ordered by their in-batch `depends_on`
    (ideas follow), cycles and dependents of refused items are refused, and dependencies that
    would block a spec forever are listed in its `would_block`."""
    ids = list(dict.fromkeys(_canon_id(i) for i in ids if i and i.strip()))
    params = {"project": project, "profile": profile, "base": base, "force": bool(force),
              "priority": priority, **_campaign_runner_pin(runner),
              **_campaign_lean_pin(lean_request, cohort)}                # SPEC-0558
    after = [str(a) for a in (after or ())]
    if after:
        params["after"] = after                                 # SPEC-0423
    if split_origin is not None:       # SPEC-0294: kept in the params for apply's re-plan
        params["split_origin"] = {"id": split_origin.get("id"), "kind": split_origin.get("kind"),
                                  "project": split_origin.get("project"),
                                  "split": {"parts": [{"spec_id": p.get("spec_id")} for p in
                                                      (split_origin.get("split") or {}).get("parts") or []]}}
    items = {}
    for i in ids:
        kind = "idea" if _IDEA_ID_RE.match(i) else "spec"
        item = {"id": i, "kind": kind}
        try:
            item["plan"] = (plan_add_idea(cfg, i, project=project, profile=profile, base=base,
                                          priority=priority, after=after, runner=runner,
                                          lean_request=lean_request, cohort=cohort)
                            if kind == "idea" else
                            plan_add(cfg, i, project=project, profile=profile, base=base,
                                     force=force, priority=priority, split_origin=split_origin,
                                     after=after, runner=runner, lean_request=lean_request,
                                     cohort=cohort))
        except ValueError as e:
            item["error"] = str(e)
        items[i] = item
    spec_ids = [i for i in ids if items[i]["kind"] == "spec"]
    all_deps, deps_of = _batch_deps(cfg, spec_ids, project)
    idea_ids = [i for i in ids if items[i]["kind"] == "idea"]
    deps_of = {**deps_of, **_idea_edges(cfg, idea_ids, set(ids))}     # SPEC-0423
    order, cycles = _order_items(spec_ids + idea_ids, deps_of)
    for sid, msg in cycles.items():
        prior = items[sid].get("error")
        items[sid]["error"] = f"{prior}; {msg}" if prior else msg
        items[sid].pop("plan", None)
    ordered = [items[i] for i in order]
    _cascade(ordered, deps_of)
    for n, it in enumerate(ordered, 1):
        it["position"] = n
        if "plan" in it and it["kind"] == "spec":
            it["would_block"] = _dangling(cfg, it["id"], all_deps[it["id"]], set(ids),
                                       project)
    return {"schema": "nw.queue.add.batch.v1", "params": params, "items": ordered,
            "skipped": [], **_batch_totals(ordered)}


def apply_add_batch(cfg, batch: dict, *, skip_invalid=False, by="human") -> dict:
    """SPEC-0276: create the campaigns of a `plan_add_batch` result, in batch order. Raises
    ValueError and writes nothing when an item has an error and `skip_invalid` is false.
    Otherwise each valid item is re-planned (`new_campaign_id` reserves nothing) and applied; a
    failure at write time is a race and lands on that item as `error`, with its dependents."""
    items = batch["items"]
    bad = sum(1 for it in items if "error" in it)
    if bad and not skip_invalid:
        raise ValueError(f"{bad} of {len(items)} items cannot be queued; fix them or pass "
                         f"--skip-invalid")
    prm = batch.get("params") or {}
    _, deps_of = _batch_deps(cfg, [it["id"] for it in items if it["kind"] == "spec"],
                            prm.get("project"))
    deps_of = {**deps_of, **_idea_edges(cfg, [it["id"] for it in items if it["kind"] == "idea"],
                                        {it["id"] for it in items})}   # SPEC-0423
    for it in items:
        if "error" in it:
            continue
        _cascade(items, deps_of)
        if "error" in it:
            continue
        try:
            if it["kind"] == "idea":
                plan = plan_add_idea(cfg, it["id"], project=prm.get("project"),
                                     profile=prm.get("profile"), base=prm.get("base"),
                                     priority=prm.get("priority", PRIORITY_DEFAULT),
                                     after=prm.get("after") or (), runner=prm.get("runner"),
                                     lean_request=prm.get("lean_request"),
                                     cohort=prm.get("cohort"))
                apply_add_idea(cfg, plan, by=by)
            else:
                plan = plan_add(cfg, it["id"], project=prm.get("project"),
                                profile=prm.get("profile"), base=prm.get("base"),
                                force=bool(prm.get("force")),
                                priority=prm.get("priority", PRIORITY_DEFAULT),
                                split_origin=prm.get("split_origin"),
                                after=prm.get("after") or (), runner=prm.get("runner"),
                                lean_request=prm.get("lean_request"),
                                cohort=prm.get("cohort"))
                apply_add(cfg, plan, by=by)
        except ValueError as e:
            it["error"] = str(e)
            it.pop("plan", None)
            it.pop("would_block", None)
            continue
        it["plan"], it["written"] = plan, True
    batch.update(_batch_totals(items))
    return batch


# ---- human commands ------------------------------------------------------------------------------

def plan_after(cfg, campaign, *, add=(), remove=(), by="human") -> dict:
    """SPEC-0423: edit a campaign's `after` list (no writes). Only a QUEUED or PARKED campaign
    can be edited; an added ref must resolve now, a removed one must be present."""
    st = store.read_state(cfg, campaign)
    if st.get("state") not in ("QUEUED", "PARKED"):
        raise ValueError(f"{campaign} is {st.get('state')}; only QUEUED or PARKED campaigns can "
                         f"change their `after`")
    cur = after_entries(st)
    new = resolve_after(cfg, add, project=st.get("project"),
                        own=_idea_id_of(st) or st.get("spec_id"))
    drop = {_canon_id(r) for r in remove or ()} | {str(r) for r in remove or ()}
    missing = [r for r in remove or () if not any(e["ref"] in (r, _canon_id(r)) for e in cur)]
    if missing:
        raise ValueError(f"{campaign} does not wait after {', '.join(missing)}")
    after = [e for e in cur if e["ref"] not in drop]
    after += [e for e in new if e not in after]
    return {"campaign": campaign, "state": st.get("state"), "from": cur, "to": after, "by": by}


def apply_after(cfg, plan: dict) -> dict:
    cid = plan["campaign"]
    with store.item_lock(cfg, cid):
        st = store.read_state(cfg, cid)
        if st.get("state") not in ("QUEUED", "PARKED"):
            raise ValueError(f"{cid} is {st.get('state')}; only QUEUED or PARKED campaigns can "
                             f"change their `after`")
        if plan["to"]:
            st["after"] = list(plan["to"])
        else:
            st.pop("after", None)
        st["backlog"] = None            # re-record readiness against the new list
        text = "after: " + (", ".join(f"{e['ref']} ({e['kind']})" for e in plan["to"]) or "(none)")
        return _transition(cfg, st, st["state"], "after edited", by=plan.get("by") or "human",
                           extra_events=[("note", {"text": text, "author": plan.get("by") or "human"})])


def plan_backend(cfg, campaign, runner, *, by="human") -> dict:
    """SPEC-0436: set or clear `state.runner` on a non-terminal campaign (no writes)."""
    if runner not in ("claude", "cursor", "pi", "default"):
        raise ValueError(f"unknown runner {runner!r} (expected cursor, claude, pi or default)")
    st = store.read_state(cfg, campaign)
    if st.get("state") in TERMINAL_STATES:
        raise ValueError(f"{campaign} is {st.get('state')}; only a non-terminal campaign "
                         "can change backend")
    return {"campaign": campaign, "state": st.get("state"), "from": st.get("runner"),
            "to": None if runner == "default" else runner, "by": by}


def apply_backend(cfg, plan: dict) -> dict:
    """Write or clear `state.runner`. Does not park or SIGTERM the live persona."""
    cid = plan["campaign"]
    with store.item_lock(cfg, cid):
        st = store.read_state(cfg, cid)
        if st.get("state") in TERMINAL_STATES:
            raise ValueError(f"{cid} is {st.get('state')}; only a non-terminal campaign "
                             "can change backend")
        old = st.get("runner")
        new = plan["to"]
        if new is None:
            st.pop("runner", None)
        else:
            st["runner"] = new
        journal(cfg, "runner_changed", campaign=cid, **{"from": old, "to": new})
        text = f"backend: {old or 'default'} → {new or 'default'}"
        return _transition(cfg, st, st["state"], "backend changed",
                           by=plan.get("by") or "human",
                           extra_events=[("note", {"text": text,
                                                   "author": plan.get("by") or "human"})])


def _user_config_path(cfg) -> Path:
    d = cfg.get("config_dir")
    if not d:
        raise ValueError("config_dir is required to change the host backend")
    return Path(d) / "config.yaml"


def _load_user_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def plan_host_backend(cfg, runner, *, by="human") -> dict:
    """SPEC-0439: set or clear user `agentic.runner` (no writes)."""
    if runner not in ("claude", "cursor", "pi", "default"):
        raise ValueError(f"unknown runner {runner!r} (expected cursor, claude, pi or default)")
    path = _user_config_path(cfg)
    current = (_load_user_yaml(path).get("agentic") or {}).get("runner")
    return {"scope": "host", "from": current, "to": None if runner == "default" else runner,
            "path": str(path), "by": by}


def apply_host_backend(cfg, plan: dict) -> dict:
    """Write user `agentic.runner`. Does not SIGTERM a live persona; the loop reloads."""
    from datetime import datetime
    path = Path(plan["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    user = _load_user_yaml(path)
    if path.is_file():
        bak = path.with_name(f"config.yaml.bak.{datetime.now().strftime('%Y%m%d%H%M%S')}")
        bak.write_bytes(path.read_bytes())
    agentic = dict(user.get("agentic") or {})
    new = plan["to"]
    if new is None:
        agentic.pop("runner", None)
    else:
        agentic["runner"] = new
    if agentic:
        user["agentic"] = agentic
    elif "agentic" in user:
        user.pop("agentic")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(user, sort_keys=False, default_flow_style=False),
                   encoding="utf-8")
    os.replace(tmp, path)
    mem = dict(cfg.get("agentic") or {})
    if new is None:
        mem.pop("runner", None)
    else:
        mem["runner"] = new
    cfg["agentic"] = mem
    journal(cfg, "runner_changed", host=True, campaign=None,
            **{"from": plan.get("from"), "to": new})
    return plan


def plan_backend_all(cfg, runner, *, by="human") -> dict:
    """SPEC-0439: pin or clear `state.runner` on every non-terminal campaign."""
    items = []
    for st in store.list_campaigns(cfg):
        item = plan_backend(cfg, st["id"], runner, by=by)
        if item["from"] == item["to"]:
            continue
        items.append(item)
    return {"scope": "all", "runner": runner, "items": items, "by": by}


def apply_backend_all(cfg, plan: dict) -> dict:
    written = [apply_backend(cfg, item) for item in plan.get("items") or ()]
    return {**plan, "written": len(written)}


def plan_prioritize(cfg, campaign, priority, *, by="human") -> dict:
    priority = check_priority(priority)
    st = store.read_state(cfg, campaign)
    if st.get("state") not in ("QUEUED", "WAITING", "PARKED"):
        raise ValueError(f"{campaign} is {st.get('state')}; only QUEUED, WAITING or PARKED "
                         f"campaigns can be re-prioritized")
    return {"campaign": campaign, "state": st.get("state"), "from": _priority(st),
            "to": priority, "by": by}


def apply_prioritize(cfg, plan: dict) -> dict:
    cid = plan["campaign"]
    with store.item_lock(cfg, cid):
        st = store.read_state(cfg, cid)
        if st.get("state") not in ("QUEUED", "WAITING", "PARKED"):
            raise ValueError(f"{cid} is {st.get('state')}; only QUEUED, WAITING or PARKED "
                             f"campaigns can be re-prioritized")
        ev = ("priority", {"from": _priority(st), "to": plan["to"]})
        st["priority"] = plan["to"]
        return _transition(cfg, st, st["state"], "prioritized", by=plan.get("by") or "human",
                           extra_events=[ev])


def _refuse_pending_escalation(cfg, st: dict) -> None:
    """SPEC-0232: a pending `human_decision` park request is the stronger hold; a plain
    `nw queue park` must not overwrite or pop it."""
    if (st.get("park_requested") or {}).get("reason") == "human_decision":
        from . import escalations as ESC
        rec = ESC.open_for(cfg, st["id"])
        which = f"escalation {rec['id']}" if rec else "a human_decision escalation"
        raise ValueError(f"{st['id']} has {which} pending; it parks as human_decision. "
                         f"Withdraw it first (nw escalations withdraw) to park differently")


def plan_park(cfg, campaign, reason, *, by="human", decision=None) -> dict:
    """`decision` is the validated request (`decisions.validate_request`) a non-human actor's
    park carries (SPEC-0318); ValueError when it is needed and missing."""
    if DEC.needs_decision(by) and not decision:
        raise ValueError(f"a park by {by} needs its decision record: pass --decision TEXT "
                         f"--rule ID (and --kind K, default wait)")
    st = store.read_state(cfg, campaign)
    s = st.get("state")
    if s in TERMINAL_STATES or s == "PARKED":
        raise ValueError(f"{campaign} is {s}; nothing to park")
    _refuse_pending_escalation(cfg, st)
    running = L.is_held(cfg) and s in ACTIVE_STATES
    return {"campaign": campaign, "state": s, "reason": reason, "by": by,
            "decision": decision,
            "mode": "deferred" if running else "immediate",
            "note": ("a loop holds the lease: the running step finishes and is recorded, then "
                     "the park applies" if running else "no loop is running: parks now"),
            "resume_to": ((st.get("waiting") or {}).get("resume_to") or "IMPLEMENTING")
            if s == "WAITING" else (_resume_target(s) if s != "QUEUED" else "QUEUED")}


def apply_park(cfg, plan: dict) -> dict:
    cid, by = plan["campaign"], plan.get("by") or "human"
    with store.item_lock(cfg, cid):
        st = store.read_state(cfg, cid)
        if st.get("state") in TERMINAL_STATES or st.get("state") == "PARKED":
            raise ValueError(f"{cid} is {st.get('state')}; nothing to park")
        _refuse_pending_escalation(cfg, st)
        if plan["mode"] == "deferred":
            st["park_requested"] = {"reason": "human", "detail": plan["reason"],
                                    "at": store.now_iso(), "by": by}
            if plan.get("decision"):
                st["park_requested"]["decision_request"] = plan["decision"]
            store.append_event(cfg, cid, "note", {"text": f"park requested: {plan['reason']}",
                                                  "author": by}, by=by)
            store.write_state(cfg, cid, st)
            return st
        out = _park(cfg, st, "human", plan["reason"], plan["resume_to"], by=by,
                    extra={"decision_request": plan["decision"]} if plan.get("decision")
                    else None)
    _cancel_remote_jobs(cfg, cid)               # SPEC-0341: the park ends the gate step
    return out


def _require_decision(cfg, st: dict, by, decision):
    """SPEC-0318: a non-human actor resumes or abandons a PARKED campaign only with a decision
    record at or after the park (or `decision`, the request to write first). Returns the request
    to write, or None. ValueError when neither exists. The queue's own parks (`park.auto`,
    `by: queue`) and a human are exempt."""
    park = st.get("park") or {}
    if st.get("state") != "PARKED" or not DEC.needs_decision(by) or by == "queue" \
            or isinstance(park.get("auto"), dict):
        return None
    if decision:
        return decision
    led = store.read_ledger(cfg, st["id"], types=["park"])
    parked_at = (led[-1].get("ts") if led else None) or park.get("since")
    if DEC.covering(cfg, st["id"], parked_at):
        return None
    raise ValueError(f"{st['id']} is PARKED and has no decision record since its park "
                     f"({parked_at}); record it first (nw queue decision {st['id']} \"why\" "
                     f"--kind K --rule ID) or pass --decision TEXT --rule ID to this command")


def _write_decision(cfg, cid, request) -> None:
    """The record a resume / abandon carries, written before the state change and outside the
    item lock (`decisions.record` takes the decisions lock first)."""
    if request:
        DEC.record(cfg, campaign=cid, **request)


def plan_resume(cfg, campaign, *, to=None, note=None, raise_cap=None, reset_loops=False,
                by="human", auto_rule=None, event_note=None, decision=None) -> dict:
    st = store.read_state(cfg, campaign)
    if st.get("state") != "PARKED":
        raise ValueError(f"{campaign} is {st.get('state')}, not PARKED")
    decision = _require_decision(cfg, st, by, decision)
    park = st.get("park") or {}
    target = to or park.get("resume_to") or "IMPLEMENTING"
    reject_resume_target(campaign, park.get("reason"), target)
    if not to and park.get("reason") == "spec_review_unavailable":
        target = "SPEC_WRITING" if note else "SPEC_REVIEW"     # SPEC-0214: the note is addressed first
    if st.get("kind") == "merge" and target not in ("CLOSING", "QUEUED"):
        raise ValueError(f"a merge campaign resumes to CLOSING or QUEUED, not {target!r}")
    if target not in RESUMABLE_STATES:
        raise ValueError(f"cannot resume to {target!r}; one of {', '.join(RESUMABLE_STATES)}")
    waits = target in SLOT_STATES and _active_count(cfg) >= slots_for(cfg)
    return {"campaign": campaign, "from": "PARKED", "to": target, "note": note,
            "raise_cap": raise_cap, "reset_loops": bool(reset_loops),
            "park": park, "cap_usd": (st.get("cost") or {}).get("cap_usd"),
            "waits_for_slot": waits, "by": by, "auto_rule": auto_rule,
            "event_note": event_note, "decision": decision}


def apply_resume(cfg, plan: dict, *, busy=None, slots=None) -> dict:
    cid, by = plan["campaign"], plan.get("by") or "human"
    _write_decision(cfg, cid, plan.get("decision"))
    with store.item_lock(cfg, cid):
        st = store.read_state(cfg, cid)
        if st.get("state") != "PARKED":
            raise ValueError(f"{cid} is {st.get('state')}, not PARKED")
        if plan.get("auto_rule") and \
                ((st.get("park") or {}).get("auto") or {}).get("rule") != plan["auto_rule"]:
            raise ValueError(f"{cid}: the park changed since the automatic resume was planned")
        if plan.get("note"):
            brief = st.get("pending_brief") or {"source": "human", "fix_brief": None,
                                                "must_fix": [], "human_notes": []}
            brief.setdefault("human_notes", []).append(plan["note"])
            st["pending_brief"] = brief
        if plan.get("raise_cap") is not None:
            _cost(st)["cap_usd"] = float(plan["raise_cap"])
        if (st.get("park") or {}).get("reason") in SPEC_PHASE_PARKS:
            _reset_spec_write(cfg, st)
        if plan.get("reset_loops"):
            c = _counters(st)
            c["loops"] = 0
            c["blocked_streak"] = 0
            c["triage_retries"] = 0
            c["gate_retries"] = 0
            # SPEC-0468: a fresh loop window. The one auto-resume and the cursor
            # review-round budget are not part of the window.
            c.pop("must_fix_baseline", None)
            c.pop("must_fix_baseline_set", None)
        _counters(st)["timeout_regates"] = 0                 # SPEC-0311: a fresh re-gate budget
        if (st.get("park") or {}).get("reason") == "gate_oom":
            # SPEC-0515: the human raised `resources.mem` (or chose to retry): a fresh OOM budget
            _counters(st)["oom_retries"] = 0
            st.pop("mem_boost", None)
        if (st.get("park") or {}).get("reason") == "gate_timeout" and plan["to"] == "GATING":
            st["gate_exclusive"] = {"entered_at": store.now_iso()}   # re-gate alone, first
        if by == "queue" and plan.get("auto_rule"):      # SPEC-0302: bounded automatic resumes
            counts = _counters(st).setdefault("auto_resumes", {})
            counts[plan["auto_rule"]] = int(counts.get(plan["auto_rule"], 0)) + 1
        for name in plan.get("bump_counters") or ():     # SPEC-0422: the cost-cap policy's raises
            _counters(st)[name] = int(_counters(st).get(name) or 0) + 1
        _drop_decision_request(cfg, st)
        st["park"] = None
        st.pop("park_requested", None)
        st.pop("gate_retry_pending", None)          # SPEC-0293: a resumed gate run is no retry
        ev = ("resume", {"to": plan["to"], "note": plan.get("note") or plan.get("event_note"),
                         "cap_usd": plan.get("raise_cap"), "loops_reset": bool(plan.get("reset_loops"))})
        st = _enter_slot_or_wait(cfg, st, plan["to"], "resumed", by=by,
                                 extra_events=[*(plan.get("extra_events") or ()), ev],
                                 busy=busy, slots=slots)
        from . import escalations as ESC            # SPEC-0232: after the transition succeeded
        ESC.withdraw_for_campaign(cfg, cid, f"campaign resumed by {by}", by)
        return st


def _reset_spec_write(cfg, st: dict) -> None:
    """Resuming a spec-phase park (SPEC-0215, SPEC-0214): fresh retry budgets and a fresh idea
    snapshot (its text may have been edited meanwhile), keeping `state_before`."""
    _counters(st)["spec_write_retries"] = 0
    _counters(st)["spec_number_retries"] = 0
    _counters(st)["spec_review_retries"] = 0
    old = st.get("idea") or {}
    if old.get("request_only"):                         # SPEC-0541: refresh from the public file
        try:
            agreed, _gate = _request_source_for(cfg, st)
        except ValueError:
            return                                      # keep the old snapshot
        st["idea"] = {**old, "summary": agreed.summary, "heading": agreed.title}
        return
    task = _idea_task(cfg, old.get("id")) if st.get("kind") == "idea" else None
    if task is not None:
        from .. import explore as EX
        from .. import idea_link as IL
        fresh = EX.idea_show_payload(cfg, task)
        fresh["epic"] = task.properties.get("EPIC")
        fresh["state_before"] = old.get("state_before")
        if (task.properties.get("REQ") or "").strip():
            try:
                agreed = IL.load_agreed_request(cfg, task)
                if agreed:
                    fresh["source_request"] = agreed.req_id
                    fresh["summary"] = agreed.summary
            except ValueError:
                if old.get("source_request"):
                    fresh["source_request"] = old["source_request"]
                if old.get("summary") is not None and "source_request" in old:
                    fresh["summary"] = old["summary"]
        st["idea"] = fresh


def plan_abandon(cfg, campaign, *, keep_worktree=False, by="human", decision=None) -> dict:
    st = store.read_state(cfg, campaign)
    if st.get("state") not in ("PARKED", "QUEUED", "WAITING"):
        raise ValueError(f"{campaign} is {st.get('state')}; only PARKED, QUEUED or WAITING "
                         f"campaigns can be abandoned (park it first)")
    decision = _require_decision(cfg, st, by, decision)
    repo = st.get("repo") or {}
    wt_path = repo.get("worktree")
    present = bool(wt_path and os.path.isdir(wt_path))
    # SPEC-0233: a worktree nightwright did not create belongs to the requester and is never removed
    adopted = st.get("kind") == "merge" and not (st.get("merge") or {}).get("own_worktree")
    return {"campaign": campaign, "from": st.get("state"), "worktree": wt_path,
            "worktree_present": present,
            "remove_worktree": present and not keep_worktree and not adopted,
            "branch": repo.get("branch"), "by": by, "decision": decision}


def _unlink_idea(cfg, st: dict) -> list:
    """Abandoning an idea campaign (SPEC-0209): clear the idea's `:CAMPAIGN:` (and its `:SPEC:`
    when that is this campaign's spec), restore its state when nobody moved it meanwhile, and
    release the campaign's spec-number reservations. Returns ledger events."""
    from .. import org_write as OW
    from .. import spec_numbers as SN
    cid, idea = st["id"], st.get("idea") or {}
    spec_id = (st.get("spec") or {}).get("id")
    events = []
    _release_idea_link(cfg, st, events)       # SPEC-0292: only a `:CAMPAIGN:` naming this campaign
    task = _idea_task(cfg, idea.get("id"))
    if task is not None:
        cur, linked = task.state, (task.properties.get("SPEC") or "").strip()
        restore = cur == idea.get("state_before") or (cur == "SPECCED" and linked == spec_id)
        if spec_id and linked == spec_id:
            OW.clear_property(cfg, task, "SPEC")
            task = _idea_task(cfg, idea.get("id"))
        if restore and idea.get("state_before") and task.state != idea["state_before"]:
            OW.set_state(cfg, task, idea["state_before"])
        elif not restore:
            events.append(("note", {"text": f"idea state kept: {cur}", "author": "queue"}))
    specs_dir = Path((st.get("repo") or {}).get("root") or "") / "docs" / "specs"
    for rec in SN.list_reservations(cfg, specs_dir=specs_dir):
        if rec.get("campaign") == cid:
            try:
                SN.release(cfg, rec["number"], specs_dir=specs_dir)
            except ValueError:
                pass
    return events


def apply_abandon(cfg, plan: dict) -> dict:
    cid, by = plan["campaign"], plan.get("by") or "human"
    _write_decision(cfg, cid, plan.get("decision"))
    with store.item_lock(cfg, cid):
        st = store.read_state(cfg, cid)
        if st.get("state") not in ("PARKED", "QUEUED", "WAITING"):
            raise ValueError(f"{cid} is {st.get('state')}; only PARKED, QUEUED or WAITING "
                             f"campaigns can be abandoned")
        events = []
        # re-derived from live state: never remove a merge request's adopted (requester's) worktree
        adopted = st.get("kind") == "merge" and not (st.get("merge") or {}).get("own_worktree")
        live_wt = (st.get("repo") or {}).get("worktree")
        remove = bool(plan.get("remove_worktree")) and not adopted and bool(live_wt)
        if remove:
            try:
                git.worktree_remove((st.get("repo") or {}).get("root"), live_wt, force=True)
            except GitError as e:
                events.append(("finding", {"persona": "human", "severity": "minor",
                                           "gate": "cleanup", "title": "worktree removal failed",
                                           "detail": e.tail, "files": []}))
        RPS.prune_refs((st.get("repo") or {}).get("root"), cid)    # SPEC-0344: saved refs
        _drop_decision_request(cfg, st)
        st["park"] = None
        st.pop("waiting", None)
        st.pop("park_requested", None)
        if st.get("kind") == "idea":
            events += _unlink_idea(cfg, st)
        else:
            _release_idea_link(cfg, st, events)                        # SPEC-0292
        events.append(("close", {"mode": "abandoned", "merged_sha": None, "pr_url": None,
                                 "gates_seq": None, "worktree_removed": remove}))
        out = _transition(cfg, st, "ABANDONED", "abandoned", by=by, extra_events=events,
                          withdraw_reason=f"campaign abandoned by {by}")
    _cancel_remote_jobs(cfg, cid)
    return out


def _cancel_remote_jobs(cfg, cid, *, note: bool = True) -> list[str]:
    """SPEC-0341: an abandon or a park ends the campaign's remote gate jobs (outside the item
    lock: each cancel waits up to 10 s for the job's `done`). Returns the job ids."""
    if not RJ.live(cfg, cid):
        return []
    from . import remote_gates as RG
    out = RG.cancel_campaign(cfg, cid)
    for job_id in out if note else ():
        store.append_event(cfg, cid, "note", {"text": f"cancelled remote job {job_id}",
                                              "author": "queue"})
    return out


def _apply_worker_escalations(cfg) -> None:
    """SPEC-0342, every tick: carry out answered worker escalations (`off` / `keep`)."""
    from . import workers as WK
    try:
        WK.apply_answered_escalations(cfg)
    except (OSError, ValueError):
        pass                                    # housekeeping: the next tick tries again


def _settle_ended_remote_jobs(cfg, flying) -> None:
    """SPEC-0341, every tick: a campaign that has ended (a terminal state, or gone) has its
    live remote job records cancelled (an abandon whose cancel never ran, a record a crash
    left), and its final records removed, since it never gates again (`max_seq` needs them
    only while it can). Other campaigns' records are known by file name only, not parsed."""
    for cid in sorted(RJ.campaigns(cfg) - set(flying)):
        if not store.campaign_dir(cfg, cid).exists():
            state = None                        # the campaign is gone
        else:
            try:
                state = store.read_state(cfg, cid).get("state")
            except (ValueError, OSError):
                continue                        # unreadable now (EIO, a bad state.json): not gone
            if state not in TERMINAL_STATES:
                continue
        cancelled = _cancel_remote_jobs(cfg, cid, note=state is not None)
        RJ.prune(cfg, cid, keep=cancelled)      # a just-cancelled record stays one tick


_DRAIN_SKIPPED: dict = {}       # runner journal path -> the last `worker_drain_skipped` error


def _finish_worker_drains(cfg, *, progress=None) -> None:
    """SPEC-0343, every tick: disable the draining workers that are idle (`workers.finish_drains`)
    and emit `worker_drained` for each. An unreadable registry, `remote-jobs` dir or record
    skips the pass (journaled `worker_drain_skipped`); nothing is disabled. The same error on
    consecutive ticks is journaled once, not every tick (a bad file stays bad until fixed)."""
    from . import events as E                   # lazy: events imports this module
    key = str(runner_journal_path(cfg))
    try:
        drained = workers.finish_drains(cfg, progress=progress)
    except (OSError, ValueError, TypeError) as e:
        err = f"{type(e).__name__}: {e}"
        if _DRAIN_SKIPPED.get(key) != err:
            journal(cfg, "worker_drain_skipped", error=err)
            _DRAIN_SKIPPED[key] = err
        return
    _DRAIN_SKIPPED.pop(key, None)
    for name in drained:                        # the registry write is the truth; the event
        try:                                    # only reports it, so no failure here undoes it
            E.append(cfg, "worker_drained", by="queue", data={"worker": name})
        except Exception as e:                  # noqa: BLE001 - journaled, the tick goes on
            journal(cfg, "worker_drained", worker=name, error=f"{type(e).__name__}: {e}")


_UPDATING: dict = {}            # (journal path, worker) -> the update thread (SPEC-0386)
_UPDATE_RETRY: dict = {}        # (journal path, worker) -> monotonic time before which not to retry
UPDATE_BUSY_RETRY_S = 60.0


def _emit_worker_event(cfg, type_, data) -> None:
    from . import events as E                   # lazy: events imports this module
    try:
        E.append(cfg, type_, by="queue", data=data)
    except Exception as e:                      # noqa: BLE001 - journaled, never undoes the write
        journal(cfg, type_, error=f"{type(e).__name__}: {e}", **data)


def _end_update_timeout(cfg, name: str, since) -> None:
    hours = workers.update_drain_timeout_h(cfg)
    if workers.end_update(cfg, name, since, "timeout", f"not idle after {hours:g} h"):
        journal(cfg, "worker_update_timeout", worker=name, timeout_h=hours)
        _emit_worker_event(cfg, "worker_update_failed", {
            "worker": name, "reason": "timeout",
            "error": f"still not idle after {hours:g} h; returned to service, not updated"})


def _return_timed_out_update(cfg, name: str, since) -> None:
    """A busy update outcome past `update_drain_timeout_h` since the drain started returns the
    worker to service, like a worker that never went idle (SPEC-0386)."""
    import datetime as dt
    started = workers._parse_ts(since)
    if started is None:
        return                                  # unreadable marker: counts from now
    now = dt.datetime.now().astimezone()
    if now - started > dt.timedelta(hours=workers.update_drain_timeout_h(cfg)):
        _end_update_timeout(cfg, name, since)


def _worker_update_job(cfg, rec: dict, key) -> None:
    """SPEC-0386: one scheduled update, off the tick thread (the wheel build and upload take
    minutes). Never `--force`. updated / unchanged: clear the drain, the worker serves again.
    busy (a race): stay draining, retry after `UPDATE_BUSY_RETRY_S`. failed / old worker:
    clear the drain too (work must not be stranded) and tell the conductor; no retry until
    `nw workers update NAME --when-idle` is run again."""
    from . import workers_update as WU
    name = rec["name"]
    since = (workers.update_pending(rec) or {}).get("since")
    try:
        out = WU.perform(cfg, rec)
    except Exception as e:                      # noqa: BLE001 - a crash is a failed update
        out = WU.Outcome("failed", f"{type(e).__name__}: {e}")
    try:
        if out.kind == "busy":
            _UPDATE_RETRY[key] = time.monotonic() + UPDATE_BUSY_RETRY_S
            journal(cfg, "worker_update_busy", worker=name, live_jobs=out.live_jobs)
            # SPEC-0534: a reconciled `lost` ghost whose process still holds the job lock makes
            # the worker idle here yet busy to the update, so `due_updates` never reports it
            # late; the drain timeout is applied on the busy outcome instead.
            _return_timed_out_update(cfg, name, since)
        elif out.kind in ("updated", "unchanged"):
            wrote = workers.end_update(cfg, name, since, out.kind, out.detail)
            journal(cfg, "worker_updated", worker=name, state=out.kind, build_id=out.build_id,
                    returned_to_service=wrote)
            _emit_worker_event(cfg, "worker_updated",
                               {"worker": name, "state": out.kind, "build_id": out.build_id})
        else:
            wrote = workers.end_update(cfg, name, since, out.kind, out.detail)
            journal(cfg, "worker_update_failed", worker=name, state=out.kind, error=out.detail,
                    returned_to_service=wrote)
            _emit_worker_event(cfg, "worker_update_failed",
                               {"worker": name, "reason": out.kind, "error": out.detail})
    finally:
        _UPDATING.pop(key, None)


def _run_worker_updates(cfg, *, progress=None) -> None:
    """SPEC-0386, every tick: a worker draining for a scheduled update (`update_pending`) that
    is idle gets the SPEC-0385 update in a background thread; one still busy after
    `agentic.workers.update_drain_timeout_h` is returned to service (journal + event; no job
    is touched). An unreadable registry or job record skips the pass (the drain pass journals it)."""
    import threading
    try:
        idle, late = workers.due_updates(cfg, progress=progress)
    except (OSError, ValueError, TypeError):
        return
    jp = str(runner_journal_path(cfg))
    for rec in late:
        name = rec["name"]
        if (jp, name) in _UPDATING:
            continue
        _end_update_timeout(cfg, name, rec["update_pending"].get("since"))
    for rec in idle:
        key = (jp, rec["name"])
        if key in _UPDATING or _UPDATE_RETRY.get(key, 0) > time.monotonic():
            continue
        journal(cfg, "worker_update_started", worker=rec["name"])
        t = threading.Thread(target=_worker_update_job, args=(cfg, rec, key), daemon=True,
                             name=f"worker-update-{rec['name']}")
        _UPDATING[key] = t
        t.start()


# ---- gc (SPEC-0275) -----------------------------------------------------------------------

def _gc_project_cfg(root) -> dict:
    """The repo's `.nw/project.yaml`, or bare defaults when there is none. Raises ValueError
    when it exists but cannot be read or is invalid (its base is then unknown)."""
    path = P.config_path(root)
    if not path.exists():
        return {"base": "main", "merge": {}, "worktrees": {}}
    try:
        return P.load_project_config(path)
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"{path}: unreadable ({e})")
    except ValueError as e:
        raise ValueError(str(e) if str(path) in str(e) else f"{path}: {e}")


def _gc_row(kind, target, st, action, why, root) -> dict:
    return {"kind": kind, ("branch" if kind == "branch" else "path"): str(target),
            "campaign": st.get("id") if st else None,
            "campaign_state": st.get("state") if st else None,
            "project": st.get("project") if st else None,
            "action": action, "why": why, "repo": str(root) if root else None}


def _cwd_worktree(entries) -> Path | None:
    """The innermost listed worktree containing the process cwd, or None."""
    try:
        cwd = Path(os.getcwd()).resolve()
    except OSError:
        return None
    hits = [e["path"] for e in entries if e["path"] == cwd or e["path"] in cwd.parents]
    return max(hits, key=lambda p: len(p.parts)) if hits else None


def _gc_worktree_keep(root, e, st, adopted, main, cwd_wt, base, prune_all=True) -> str | None:
    """Why a terminal campaign's worktree `e` must stay, or None when it may go."""
    path = e["path"]
    if path == main:
        return "main checkout"
    if path == cwd_wt:
        return "current directory"
    if e["locked"]:
        return "locked"
    if e["prunable"]:
        # git also marks a directory whose `.git` file is gone or broken: prune unregisters
        # it, but its files cannot be checked, so the directory itself stays
        if path.exists():
            return ("no .git (git worktree prune unregisters it; directory stays)" if prune_all
                    else "no .git (directory stays)")
        return None
    if adopted and st.get("state") != "MERGED":
        return f"requester's worktree ({st.get('state')} merge request)"
    try:
        if git.rebase_in_progress(path) or git.sequencer_in_progress(path):
            return "rebase/merge in progress"
        if git.bisect_in_progress(path):
            return "bisect in progress"
        if not git.is_clean(path, untracked=True):
            return "dirty"
        if git.hidden_changes(path):
            return "dirty (skip-worktree/assume-unchanged edits)"
    except GitError as e:
        return f"unreadable ({e.tail or e})"
    if adopted and e["branch"] != (st.get("merge") or {}).get("branch"):
        return f"requester moved on (on {e['branch'] or 'detached HEAD'})"
    if adopted and not (e["head"] and git.is_ancestor(root, e["head"], base)):
        return "unmerged"
    # the requester may have synced the same branch past the merge (ff to base, reset, pull):
    # only a HEAD the recorded merge already contains is where the merge left it
    merged_sha = (st.get("close") or {}).get("merged_sha")
    if adopted and not (merged_sha and git.is_ancestor(root, e["head"], merged_sha)):
        return "requester moved on (HEAD past the merge)"
    if e["detached"] and e["head"] and not git.is_ancestor(root, e["head"], base):
        b = (st.get("repo") or {}).get("branch")
        if not (b and git.branch_exists(root, b) and git.is_ancestor(root, e["head"], f"refs/heads/{b}")):
            return "detached, unreachable commits"
    return None


def _gc_request_moved_on(root, b, sts) -> bool:
    """`b` is the branch of a MERGED merge request (the newest one naming it) whose tip the
    recorded merge does not contain: the requester kept using it after the merge."""
    req = next((s for s in sts if s.get("kind") == "merge"
                and (s.get("merge") or {}).get("branch") == b), None)
    if not req or req.get("state") != "MERGED":
        return False
    merged_sha = (req.get("close") or {}).get("merged_sha")
    return not (merged_sha and git.is_ancestor(root, f"refs/heads/{b}", merged_sha))


def _gc_rows(cfg, root, campaigns, base, worktree_roots=(), errors=None, prune_all=True) -> list[dict]:
    """gc rows for one repo: worktrees, orphan dirs, then branches. `campaigns` are all the
    campaigns (any project, any state) whose repo root is `root`, newest first; `base` is the
    project default when a campaign records none. Anything a non-terminal campaign references
    produces no row at all. Unreadable worktree roots are appended to `errors`. `prune_all`:
    `--yes` will run the repo-wide `git worktree prune` (not under a --project filter)."""
    root = Path(root).resolve()
    entries = git.worktree_list(root)
    main = entries[0]["path"] if entries else root
    cwd_wt = _cwd_worktree(entries)
    by_path, by_branch = {}, {}
    for st in campaigns:
        repo = st.get("repo") or {}
        if repo.get("worktree"):
            by_path.setdefault(Path(repo["worktree"]).resolve(), []).append(st)
        for b in {repo.get("branch"), (st.get("merge") or {}).get("branch")} - {None, ""}:
            by_branch.setdefault(b, []).append(st)

    def live(sts):
        return any(s.get("state") not in TERMINAL_STATES for s in sts)

    def base_of(st):
        return (st.get("repo") or {}).get("base") or base

    live_bases = {base_of(s) for s in campaigns if s.get("state") not in TERMINAL_STATES}
    rows, gone, kept_detached = [], set(), {}
    for e in entries:
        if e["bare"]:
            continue
        sts = by_path.get(e["path"]) or []
        if not sts:
            if e["path"] != main:
                why = "no campaign" + (" (prunable: git worktree prune drops it)"
                                       if prune_all and e["prunable"] and not e["locked"] else "")
                rows.append(_gc_row("worktree", e["path"], None, "keep", why, root))
            continue
        if live(sts):
            continue
        st = sts[0]
        adopted = st.get("kind") == "merge" and not (st.get("merge") or {}).get("own_worktree")
        why = _gc_worktree_keep(root, e, st, adopted, main, cwd_wt, base_of(st), prune_all)
        if why is None:
            gone.add(e["path"])
            why = ("missing on disk (unregister only)" if e["prunable"]
                   else f"{st.get('state')} {'merge request, merged' if adopted else 'campaign'}, clean")
            rows.append(_gc_row("worktree", e["path"], st, "remove", why, root))
        else:
            rows.append(_gc_row("worktree", e["path"], st, "keep", why, root))
            # a kept detached worktree (e.g. mid-rebase) still holds its campaign's branch
            b = (st.get("repo") or {}).get("branch")
            if not e["branch"] and b:
                kept_detached.setdefault(b, e["path"])

    listed = {e["path"] for e in entries}
    for wroot in sorted({Path(w).resolve() for w in worktree_roots}):
        try:
            if not wroot.is_dir():
                continue
            dirs = sorted(p for p in wroot.iterdir() if p.is_dir())
        except OSError as e:
            if errors is not None:
                errors.append(f"{wroot}: {e}")
            continue
        for d in dirs:
            if d.resolve() in listed:
                continue
            sts = by_path.get(d.resolve()) or []
            if live(sts):
                continue
            # a dir only a terminal campaign references is just as unknown to git: same row,
            # naming that campaign
            rows.append(_gc_row("orphan", d, sts[0] if sts else None, "keep",
                                (f"orphan dir of {sts[0].get('state')} campaign" if sts else "orphan dir")
                                + ": git does not list it; delete by hand", root))

    # `git worktree prune` (run before the branch rows on --yes) drops every prunable, unlocked entry
    held = {e["branch"]: e["path"] for e in entries
            if e["branch"] and e["path"] not in gone
            and not (prune_all and e["prunable"] and not e["locked"])}
    held = {**kept_detached, **held}
    for b, sts in by_branch.items():
        if live(sts) or not git.branch_exists(root, b):
            continue
        st = sts[0]
        bbase = base_of(st)
        if b == bbase or b in live_bases:
            why = "base branch"
        elif b in held:
            why = f"checked out in {held[b]}"
        elif not git.is_ancestor(root, f"refs/heads/{b}", bbase):
            why = "unmerged"
        elif _gc_request_moved_on(root, b, sts):
            why = "requester moved on (tip past the merge)"
        else:
            rows.append(_gc_row("branch", b, st, "delete", f"merged into {bbase}", root))
            continue
        rows.append(_gc_row("branch", b, st, "keep", why, root))
    return rows


def plan_gc(cfg, project=None) -> dict:
    """SPEC-0275: what `nw queue gc` would remove, per repo root any campaign references.
    `git worktree list` is the source of truth; nothing is written. SPEC-0287: a terminal
    campaign whose `tmp/` survived (pre-SPEC-0287 HANDED_OFF, a killed runner) gets a `tmp` row."""
    by_root, tmp_rows = {}, []
    for st in store.list_campaigns(cfg, include_terminal=True):
        r = (st.get("repo") or {}).get("root")
        if r:
            by_root.setdefault(str(Path(r).resolve()), []).append(st)
        tmp = store.campaign_tmp(cfg, st["id"])
        if (st.get("state") in TERMINAL_STATES and (not project or st.get("project") == project)
                and tmp.exists() and not tmp.is_symlink()):
            tmp_rows.append(_gc_row("tmp", tmp, st, "remove", f"{st.get('state')} campaign temp residue",
                                    Path(r).resolve() if r else None))
    rows, repos, errors = list(tmp_rows), [], []
    for root, sts in sorted(by_root.items()):
        if project and not any(s.get("project") == project for s in sts):
            continue
        if not Path(root).is_dir():
            errors.append(f"{root}: repo root missing")
            continue
        try:
            pcfg = _gc_project_cfg(root)
        except ValueError as e:
            errors.append(f"{root}: {e}")
            continue
        base = (pcfg.get("merge") or {}).get("base") or pcfg.get("base") or "main"
        wroots = [P.worktree_root(pcfg, root, p) for p in sorted({s.get("project") for s in sts} - {None})]
        try:
            got = _gc_rows(cfg, root, sts, base, wroots, errors, prune_all=not project)
        except GitError as e:
            errors.append(f"{root}: {e.tail or e}")
            continue
        if project:
            got = [r for r in got if r["project"] in (project, None)]
        rows += got
        repos.append(root)
    return {"project": project, "repos": repos, "rows": rows, "errors": errors,
            "worktrees_removable": sum(r["kind"] == "worktree" and r["action"] == "remove" for r in rows),
            "branches_removable": sum(r["kind"] == "branch" and r["action"] == "delete" for r in rows),
            "tmp_removable": len(tmp_rows)}


def apply_gc(cfg, plan: dict) -> dict:
    """Apply a `plan_gc` plan: terminal campaigns' `tmp/` residue (SPEC-0287,
    `store.clear_campaign_tmp`), then worktree removals (one `git worktree remove` per row, without
    --force, so git refuses a dirty tree too; it also drops the entry of a directory already
    gone), then `git worktree prune` once per repo unless the plan is filtered by project (the
    prune is repo-wide), then branch deletions (`-d`, never `-D`). A `GitError` lands on its
    row (`error`) and the rest still apply. No ledger events: the returned plan is the record."""
    for row in plan["rows"]:
        if row["kind"] != "tmp" or row["action"] == "keep":
            continue
        try:
            store.clear_campaign_tmp(cfg, row["campaign"])
            if Path(row["path"]).exists():
                raise OSError("still present after removal")
            row["done"] = True
        except (OSError, ValueError) as e:
            row["done"], row["error"] = False, str(e)

    def apply(kind):
        for row in plan["rows"]:
            if row["kind"] != kind or row["action"] == "keep":
                continue
            try:
                if kind == "branch":
                    git.branch_delete(row["repo"], row["branch"])
                else:
                    git.worktree_remove(row["repo"], row["path"], prune=False)
                row["done"] = True
            except GitError as e:
                row["done"], row["error"] = False, e.tail or str(e)

    apply("worktree")
    if not plan.get("project"):
        for root in plan["repos"]:
            try:
                git.git(root, "worktree", "prune")
            except GitError as e:
                plan["errors"].append(f"{root}: worktree prune: {e.tail or e}")
    apply("branch")
    return plan


def tick_row_text(row: dict) -> str:
    extra = f" run {row['run_id']}" if row.get("run_id") else ""
    cost = ""
    if row.get("cost_usd") is not None:
        cost = f" ~${row['cost_usd']:.2f} est." if row.get("cost_estimated") else f" ${row['cost_usd']:.2f}"
    return f"{row['campaign']}: {row['from']} → {row['to']} ({row['action']}; {row['reason']}){extra}{cost}"

