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
import hashlib
import json
import math
import os
import re
import shutil
import signal
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
from ..rules import INTERNAL_PROJECT
from ..spec_scope import idea_project
from ..specmeta import split_frontmatter
from . import autopark as AP
from . import close as CL
from . import decisions as DEC
from . import divergence as DV
from . import flaky as FL
from . import followups as FU
from . import gate_slots
from . import gates as G
from . import gitops as git
from . import lease as L
from . import personas as PS
from . import project as P
from . import remote_jobs as RJ
from . import remote_personas as RPS
from . import sentinel as SN
from . import spec_review as SR
from . import spec_write as SW
from . import schemas as SC
from . import store
from . import workers
from .codewatch import (_CODE_PATHS, _changed_paths, _git_out, code_changes,  # noqa: F401
                        code_checkout)
from .gitops import GitError
from .profiles import (SEVERITIES, persona_settings, profile_sha256, resolve_profile, review_params,
                       reviewers_of, select_profile, triager_enabled)
from .events import settings as events_settings
from .events import temp_pressure as events_temp_pressure
from . import runproc as RP
from .runner import DETACHED, RunHooks, RunResult, rebuild_request, select_runner

STATES = ("QUEUED", "WAITING", "OPENING", "SPEC_WRITING", "SPEC_REVIEW", "IMPLEMENTING", "GATING", "TRIAGING", "REVIEWING",
          "CLOSING", "PARKED", "MERGED", "HANDED_OFF", "ABANDONED")
# Slots are for campaigns that are open and delivering (ACTIVE_STATES). QUEUED = the backlog,
# never started. WAITING (SPEC-0200) = started (worktree, commits, counters, brief) and ready to
# continue, but no slot was free; it takes the next free slot ahead of the backlog and resumes
# at `waiting.resume_to` without re-opening. PARKED = needs a decision; holds no slot.
TERMINAL_STATES = store.TERMINAL_STATES
ACTIVE_STATES = frozenset({"OPENING", "SPEC_WRITING", "SPEC_REVIEW", "IMPLEMENTING", "GATING", "TRIAGING", "REVIEWING",
                           "CLOSING"})
# SPEC-0394: GATING costs no Claude spend (it waits on / runs box gates, whose concurrency is
# bounded by the gate slot pools, gate_slots.py), so it does not count against the profile
# `slots`. Every other active state does.
SLOT_STATES = ACTIVE_STATES - {"GATING"}
RESUMABLE_STATES = ("SPEC_WRITING", "SPEC_REVIEW", "IMPLEMENTING", "GATING", "REVIEWING", "CLOSING", "QUEUED")
PARK_REASONS = frozenset({
    "cost_cap", "loop_exhausted", "implementer_blocked", "triage_escalated", "open_failed",
    "rebase_conflict", "main_dirty", "main_not_on_base", "base_moving", "gh_failed", "human",
    "review_escalated", "config_invalid", "spec_writer_unavailable", "spec_write_failed",
    "spec_review_unavailable", "human_decision", "spec_review_exhausted",
    "gates_failed", "worktree_dirty",                  # SPEC-0233: merge campaigns
    "not_docs_only",                                   # SPEC-0234: --docs-only diff left the allowlist
    "diverging",                                       # SPEC-0247: the fix loop is not converging
    "regenerate_failed",                               # SPEC-0255: close-time regenerate failed/dirtied
    "gate_timeout",                                    # SPEC-0311: gates keep timing out under load
    "restart",                                         # SPEC-0375: parked across `nw queue restart`
})
# SPEC-0222: who a park waits for. The human only for their own checkout and explicit
# escalations (`human_decision`, SPEC-0232); everything else is the conductor's.
PARK_OWNERS = {"main_dirty": "human", "main_not_on_base": "human", "human_decision": "human",
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
# SPEC-0214: failed spec-review runs retried before the campaign parks `spec_review_exhausted`.
SPEC_REVIEW_RETRY_MAX = 1
# Malformed triage verdicts tolerated per loop before the triage is treated as `escalate`.
TRIAGE_RETRY_MAX = 1
# `retry_gate` dispositions honored per loop before they are treated as `fix`.
GATE_RETRY_MAX = 1
# SPEC-0311: consecutive timeout-only gate runs re-gated (serialized) before `gate_timeout`.
TIMEOUT_REGATE_MAX = 2
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


def _resolver_allowance(project_cfg: dict | None, profile: dict) -> float:
    """SPEC-0303: the spend over `cost.cap_usd` a close may put into resolver runs:
    `merge.resolver_allowance_usd`, else the resolver's budget × `MAX_RESOLVE_ATTEMPTS`."""
    configured = ((project_cfg or {}).get("merge") or {}).get("resolver_allowance_usd")
    if configured is not None:
        return float(configured)
    return float(persona_settings(profile, "conflict_resolver").get("max_budget_usd") or 0.0) \
        * MAX_RESOLVE_ATTEMPTS


def _cap_blocks(state: dict, profile: dict, persona,
                allowance: float = 0.0) -> tuple[str, float] | tuple[None, None]:
    """Pessimistic cap check: a run may spend up to its budget, so never start one that could
    cross the campaign cap (plus `allowance`, SPEC-0303: the cap itself is never changed).
    `persona` is a name or a list of names (a fan-out is checked as the sum of its budgets).
    Returns `(park detail, the blocked run's budget)` or `(None, None)`."""
    cost = state.get("cost") or {}
    cap = cost.get("cap_usd")
    if cap is None:
        return None, None
    cap = float(cap) + allowance
    names = [persona] if isinstance(persona, str) else list(persona)
    budget = sum(float(persona_settings(profile, p).get("max_budget_usd") or 0.0) for p in names)
    total = float(cost.get("total_usd") or 0.0)
    if total + budget > cap:
        who = f"a {names[0]} run" if len(names) == 1 else f"{', '.join(names)} runs"
        return (f"spent ${total:.2f}; {who} may cost ${budget:.2f}; cap ${cap:.2f}"
                + (f" (including a ${allowance:.2f} close allowance)" if allowance else ""),
                budget)
    return None, None


def _valid_verdicts(state: dict, profile: dict) -> dict:
    """`{persona: stored verdict}` for the reviewers with a valid verdict of the current loop
    (SPEC-0201), in profile order. A stale-loop entry, or an old-shape `last_review` without
    `verdicts`, counts as none."""
    stored = (state.get("last_review") or {}).get("verdicts") or {}
    loop = int((state.get("counters") or {}).get("loops", 0))
    return {p: stored[p] for p in reviewers_of(profile)
            if isinstance(stored.get(p), dict) and stored[p].get("loop") == loop}


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
    return str((state.get("idea") or {}).get("id") or "?")


def plan_step(state: dict, profile: dict, project_cfg: dict | None = None) -> Step:
    """Pure: the next step for `state`. Never touches disk."""
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
    if state.get("kind") == "merge" and st != "CLOSING":
        # SPEC-0233: open goes straight to CLOSING; no persona ever runs for a merge request
        return Step("park", reason="human", detail=f"merge campaign in {st}", resume_to="CLOSING")
    if st == "SPEC_WRITING" and state.get("kind") == "split":
        # SPEC-0290: checked before the `spec.id` park below (a split campaign sets `spec.id`)
        parts = (state.get("split") or {}).get("parts") or []
        if parts and all(p.get("spec_id") for p in parts):
            return Step("finalize_split")
        blocked, budget = _cap_blocks(state, profile, "spec_writer")
        if blocked:
            return Step("park", persona="spec_writer", reason="cost_cap", detail=blocked,
                        resume_to=st, budget=budget)
        return Step("write_spec", persona="spec_writer")
    if st == "SPEC_WRITING":
        # SPEC-0215: the spec writer writes (no spec yet) or revises (findings in the brief)
        if (state.get("spec") or {}).get("id") and not state.get("pending_brief"):
            return Step("park", reason="spec_review_unavailable", resume_to="SPEC_WRITING",
                        detail="nothing to revise; resume with --note to have the writer revise")
        blocked, budget = _cap_blocks(state, profile, "spec_writer")
        if blocked:
            return Step("park", persona="spec_writer", reason="cost_cap", detail=blocked,
                        resume_to=st, budget=budget)
        return Step("write_spec", persona="spec_writer")
    if st == "SPEC_REVIEW":                 # SPEC-0214
        blocked, budget = _cap_blocks(state, profile, "spec_reviewer")
        if blocked:
            return Step("park", persona="spec_reviewer", reason="cost_cap", detail=blocked,
                        resume_to=st, budget=budget)
        return Step("spec_review", persona="spec_reviewer")
    if st == "GATING":
        return Step("gate")
    if st == "CLOSING":
        if (state.get("closing") or {}).get("resolve"):         # SPEC-0221
            blocked, budget = _cap_blocks(state, profile, "conflict_resolver",
                                          _resolver_allowance(project_cfg, profile))
            if blocked:
                return Step("park", persona="conflict_resolver", reason="cost_cap",
                            detail=blocked, resume_to=st, budget=budget)
            return Step("resolve", persona="conflict_resolver")
        return Step("close")
    if st == "REVIEWING":
        reviewers = reviewers_of(profile)
        if len(_valid_verdicts(state, profile)) == len(reviewers) \
                and (state.get("last_review") or {}).get("triage") == "pending":
            # SPEC-0206: the one exception to "a review is never skipped"; the cap was checked
            # when `triage` became pending
            return Step("review", persona="review_triager", personas=("review_triager",))
        pending = _pending_reviewers(state, profile)
        blocked, budget = _cap_blocks(state, profile, pending)
        if blocked:
            return Step("park", persona=pending[0], personas=tuple(pending), reason="cost_cap",
                        detail=blocked, resume_to=st, budget=budget)
        return Step("review", persona=pending[0], personas=tuple(pending))
    persona, kind = ("implementer", "implement") if st == "IMPLEMENTING" \
        else ("gate_triager", "triage")
    blocked, budget = _cap_blocks(state, profile, persona)
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
    for p in personas:
        v = verdicts[p]
        raw = [f for f in v.get("findings") or [] if isinstance(f, dict)]
        recs = []
        for i, f in enumerate(raw):
            sev = f.get("severity") if f.get("severity") in SEVERITIES else "minor"
            recs.append({"id": f"{p}#{i}", "personas": [p], "severity": sev,
                         "title": f.get("title") or "", "detail": finding_detail(f),
                         "files": list(f.get("files") or []), "ac": f.get("ac"),
                         "category": f.get("category"), "synthetic": False})
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
    # 3: a fix verdict survives iff backed by a surviving major/must_fix (synthetics are majors)
    fixing = [p for p in personas if wants_fix[p] and any(
        id(m) not in gone and m["severity"] in ("major", "must_fix") for m in belongs[p])]
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
    advisory = [m for m in surviving if m not in blocking]
    keys = ("id", "personas", "severity", "title", "detail", "files", "ac", "category")
    out = lambda ms: [{k: m[k] for k in keys} for m in ms]
    return {"outcome": outcome, "score": score, "score_only": score_only,
            "blocking": out(blocking), "advisory": out(advisory),
            "waivable_weight": sum(weights[m["severity"]] for m in surviving
                                   if m["severity"] in ("minor", "nit")),
            "fixing": fixing, "synthetic": [m["id"] for m in surviving if m["synthetic"]],
            "human_waived": human_ids, "attested": attest_hit}


def _bullet(f: dict) -> str:
    files = ", ".join(f.get("files") or [])
    return (f"- [{f['severity']}] {f['title']}" + (f" ({files})" if files else "")
            + (f": {f['detail']}" if f.get("detail") else ""))


def review_brief(verdicts: dict, tri: dict, pass_score) -> dict:
    """Pure: the fix brief a `fix` triage implies (SPEC-0203) as `{fix_brief, must_fix,
    personas}` (`personas` only with several reviewers). One reviewer keeps SPEC-0187's shape
    (its own brief verbatim; `must_fix` entries without `id` / `severity` / `personas`)."""
    blocking = tri["blocking"]
    tail = ([f"Review score {tri['score']} is below the pass score {pass_score}; address the "
             f"highest-weight findings first."] if tri["score_only"] else [])
    tail += [_bullet(f) for f in blocking]
    if len(verdicts) == 1:
        persona = next(iter(verdicts))
        brief = verdicts[persona].get("fix_brief")
        if not (brief or "").strip() and (tri["score_only"] or any(
                f["id"] in tri["synthetic"] for f in blocking)):
            brief = "\n".join(tail)
        return {"fix_brief": brief, "must_fix": [
            _with_category({"ac": f["ac"], "title": f["title"], "detail": f["detail"],
                            "files": f["files"]}, f) for f in blocking]}
    head = "\n\n".join(f"[{p}] {(verdicts[p].get('fix_brief') or '').strip()}".rstrip()
                       for p in tri["fixing"])
    reporters = {p for f in blocking for p in f["personas"]} | set(tri["fixing"])
    return {"fix_brief": "\n\n".join(x for x in (head, "\n".join(tail)) if x),
            "personas": [p for p in verdicts if p in reporters],
            "must_fix": [_with_category({"ac": f["ac"], "title": f["title"],
                                         "detail": f["detail"], "files": f["files"],
                                         "id": f["id"], "severity": f["severity"],
                                         "personas": f["personas"]}, f) for f in blocking]}


def _with_category(item: dict, f: dict) -> dict:
    """A brief entry carries the finding's `category` when it has one (SPEC-0202)."""
    if f.get("category"):
        item["category"] = f["category"]
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


def _idea_id_of(st: dict) -> str | None:
    return (st.get("idea") or {}).get("id")


def backlog_readiness(cfg, state: dict) -> tuple[bool, list[str]]:
    """Can this queued campaign be admitted? Re-reads the spec and its `depends_on` from the
    project's base checkout (`repo.root`): the spec must still be `accepted` (`in-progress` for a
    `--force` add) and every dependency `done` or `superseded`. Unknown ids block. An idea
    campaign without a spec (SPEC-0209) is ready while its idea is still open. A merge request
    (SPEC-0233) is ready while its branch exists."""
    if state.get("kind") == "merge":
        repo = state.get("repo") or {}
        branch = repo.get("branch") or ""
        if branch and git.branch_exists(repo.get("root") or ".", branch):
            return True, []
        return False, [f"branch {branch} missing"]
    if has_no_spec(state):
        iid = _idea_id_of(state)
        task = _idea_task(cfg, iid)
        if task is None:
            return False, [f"idea {iid} not found"]
        if idea_is_open(task):
            return True, []
        return False, [f"idea {iid} is {task.state}"]
    root = Path((state.get("repo") or {}).get("root") or "")
    split = state.get("kind") == "split"
    rel = (state.get("split") or {}).get("original_rel") if split \
        else (state.get("spec") or {}).get("rel_path")
    spec_path = root / (rel or "")
    try:
        fm, _ = split_frontmatter(spec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, ["spec file missing"]
    fm = fm if isinstance(fm, dict) else {}
    blocked = []
    status = str(fm.get("status") or "")
    allowed = ("accepted", "in-progress") if state.get("force") else ("accepted",)
    if status not in allowed:
        blocked.append(f"spec status is {status or 'missing'}")
    if split:                                   # SPEC-0290: writing spec files needs no dependency
        if _spec_sha(spec_path) != (state.get("split") or {}).get("review_sha"):
            blocked.append("spec changed since the split review")
        return not blocked, blocked
    deps = fm.get("depends_on") or []
    for dep in [deps] if isinstance(deps, str) else deps:
        dep = str(dep)
        camp = _dep_campaign(cfg, dep, state.get("project"))
        merged = (camp or {}).get("state") == "MERGED"
        try:
            dep_fm, _ = split_frontmatter(_find_dep_spec(spec_path.parent, dep)
                                          .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            if not merged:                      # SPEC-0347: the base may predate the export
                blocked.append(f"{dep} not found")
            continue
        met, why = dependency_met(spec_path.parent, dep, dep_fm or {},
                                  {str(fm["id"])} if fm.get("id") else set())
        if met or merged:                       # SPEC-0347: a merged PR the base lacks yet
            continue
        if (camp or {}).get("state") == "HANDED_OFF":
            why += f"; PR open, campaign {camp.get('id')} handed off"
        blocked.append(f"waiting on {dep} ({why})")
    return not blocked, blocked


def _find_dep_spec(specs_dir, dep) -> Path:
    """SPEC-0347: the spec file for dependency `dep` under `specs_dir`. Exported outbound specs
    are filed as `PROJ-NNNN-slug.md` (next to a `.contract.md` sidecar, never the spec), so the
    exact id prefix wins; else `specs._find_spec`'s `NNNN-*.md`. Raises ValueError."""
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
        try:
            rfm, _ = split_frontmatter(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rfm, dict) or not rfm.get("id") or rfm["id"] in seen:
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
    lr = state.get("last_review") if isinstance(state.get("last_review"), dict) else {}
    for k in ("verdicts", "triage", "triager_run", "waived"):
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


def _record_run(state: dict, persona: str, run_id: str, result, budget: float, *,
                interrupted: bool = False) -> list:
    """Cost + run.end + findings for a finished persona run. Returns ledger events; mutates
    the counters and cost in `state`. `interrupted` is the step's view (the loop was told to
    stop while the run was in flight), which the RunResult cannot know."""
    interrupted = bool(interrupted or result.interrupted)
    cost = _cost(state)
    estimated = result.cost_usd is None
    charged = budget if estimated else result.cost_usd
    cost["total_usd"] = round(float(cost["total_usd"]) + float(charged), 4)
    if estimated:
        cost["estimated_usd"] = round(float(cost.get("estimated_usd") or 0.0) + float(charged), 4)
    cost["by_persona"][persona] = round(float(cost["by_persona"].get(persona, 0.0))
                                        + float(charged), 4)
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
    }), ("cost", {"persona": persona, "delta_usd": charged, "total_usd": cost["total_usd"],
                  "cap_usd": cost.get("cap_usd"), "estimated": estimated})]
    if estimated:
        reason = ("interrupted" if interrupted else "timed out" if result.timed_out
                  else "no cost reported")
        events.append(("finding", {"persona": persona, "severity": "info", "gate": None,
                                   "title": "run cost unknown; charged its budget as an estimate",
                                   "detail": f"${budget:.2f} charged as an upper bound ({reason})",
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


_PROGRESS_MIN_INTERVAL_S = 1.0      # SPEC-0313: at most one `progress_at` lease write per second


class _Ctx:
    """Per-tick context: cfg, resolved profiles / project configs, the runner, the stop flag."""

    def __init__(self, cfg, *, stop: threading.Event | None = None, runner=None, lease=None):
        self.cfg = cfg
        self.stop = stop or threading.Event()
        self.lease = lease            # SPEC-0228: where `_busy` declares long main-thread steps
        self._runner = runner
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
        if self._runner is None:
            self._runner = select_runner(self.cfg)
        return self._runner

    def profile(self, state) -> dict:
        name = state.get("profile")
        root = (state.get("repo") or {}).get("root")        # SPEC-0237: the main checkout
        if (name, root) not in self._profiles:
            self._profiles[name, root] = resolve_profile(self.cfg, name, project_root=root)
        return self._profiles[name, root]

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
        base_sha = git.rev_parse(root, _open_start(root, pcfg, base))
        worktree = Path(repo["worktree"])
        # the sha, not `<remote>/<base>`: no upstream is set on the campaign branch
        git.worktree_add(root, worktree, repo["branch"], base_sha)
        _run_worktree_setup(pcfg, worktree)
        repo["base_sha_at_open"] = base_sha
        repo["head"] = git.rev_parse(worktree)
        if not has_no_spec(state):
            spec = state.setdefault("spec", {})
            spec["sha256_at_open"] = _spec_sha(worktree / spec.get("rel_path", ""))
            # SPEC-0186: keep the accepted text so the close step can detect spec drift
            spec_file = worktree / spec.get("rel_path", "")
            if spec.get("rel_path") and spec_file.is_file():
                store.campaign_dir(ctx.cfg, state["id"]).mkdir(parents=True, exist_ok=True)
                (store.campaign_dir(ctx.cfg, state["id"]) / CL.SPEC_AT_OPEN).write_text(
                    spec_file.read_text(encoding="utf-8"), encoding="utf-8")
    except (GitError, ValueError, OSError) as e:
        res.error = getattr(e, "tail", None) or str(e)
        res.config_error = isinstance(e, ProjectConfigError)
    return res


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
    if persona not in PS.REVIEWER_PERSONAS or entry is None:
        return store.read_ledger(cfg, state["id"], types=["run.end"], tail=2)
    ends = store.read_ledger(cfg, state["id"], types=["run.end"])
    ends = [e for e in ends if int(str(e.get("run_id") or "0-").split("-")[0] or 0) <= entry]
    return ends[-2:]


def _triage_input(state: dict, profile: dict) -> str:
    """The `triage_input` prompt value of the review triager (SPEC-0206): the score, the
    reviewers' verdict and brief, and the waivable (surviving `minor`/`nit`) findings."""
    valid = _valid_verdicts(state, profile)
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
        })
        counters["runs"] = run_no
        try:
            store.write_state(cfg, cid, state)
        except (ValueError, OSError) as e:
            raise _RunStarted(e, run_id, budget) from e
    return run_id, req, budget


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
    try:
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
            from .. import spec_numbers as SN
            rec = SN.allocate(ctx.cfg, by="queue", campaign=state["id"],
                              specs_dir=worktree / "docs" / "specs")
            mode, number = "write", rec["number"]
            path = f"docs/specs/{rec['number']:04d}-{SP.slugify(idea.get('heading') or '')}.md"
        sw = SW.snapshot(worktree, mode=mode, number=number, path=path)
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
                 "title": None, "mode": sw["mode"], "rollback_failed": False}
    w = res.write
    branch = (state.get("repo") or {}).get("branch")

    def on_branch():
        """A reset must only ever hit the campaign branch: the writer runs with full permissions
        and may have switched or detached HEAD. Otherwise nothing is reset and a human fixes it."""
        if branch and git.current_branch(worktree) != branch:
            w["rollback_failed"] = True
            raise ValueError(f"HEAD is no longer on {branch}; check the worktree out on it, "
                             "clean it, then resume")

    def roll_back():
        on_branch()
        try:
            SW.rollback(worktree, sw)
        except GitError:
            w["rollback_failed"] = True
            raise

    try:
        res.run_result = rr = ctx.runner.run(req)
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
            generated = []
            if not problems:
                pcfg = ctx.project(state)
                regen = (pcfg.get("merge") or {}).get("regenerate") or []
                problems, generated = SW.regenerate(worktree, regen, sw)
            if problems:
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
        res.error = getattr(e, "tail", None) or str(e)
        if not isinstance(e, (GitError, ValueError, OSError)):
            res.error = repr(e)
            res.crashed = repr(e)               # still a report.errors entry
        if not w["rollback_failed"] and not res.interrupted:
            try:
                roll_back()
            except (GitError, ValueError):
                pass                            # roll_back marked rollback_failed
        if w["rollback_failed"]:
            res.error = "rollback failed; clean the worktree, then resume"
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
        if state.get("kind") != "idea" or \
                store.read_ledger(ctx.cfg, state["id"], types=["spec_accepted"]):
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
    try:
        head = git.rev_parse(worktree)
        try:
            res.run_result = ctx.runner.run(req)
        except Exception as e:                  # the reset below must still run
            res.error = getattr(e, "tail", None) or str(e)
            if not isinstance(e, (GitError, ValueError, OSError)):
                res.error = repr(e)
        res.interrupted = ctx.stop.is_set()
        if git.rev_parse(worktree) != head or _spec_sha(spec_file) != sr["sha"] \
                or not git.is_clean(worktree, untracked=False):
            sr["saved"] = _save_worktree_changes(worktree, state["id"], run_id, head)
            if branch and git.current_branch(worktree) != branch:
                git.git(worktree, "checkout", "-f", branch)
            git.git(worktree, "reset", "--hard", head)
            sr["modified"] = "reviewer modified the worktree"
    except (GitError, ValueError, OSError) as e:
        res.error = getattr(e, "tail", None) or str(e)
    return res


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
                hooks: RunHooks | None = None, adopted: dict | None = None) -> StepResult:
    """Worker thread: run the persona; for the implementer, commit afterwards (the triager and
    the reviewer are read-only and commit nothing). With `hooks` (SPEC-0225) the run may be
    detached by the pool: then nothing happens here and the result says so. `adopted` marks a
    run found open by a new runner (`exited_before_start`: its process ended before this
    runner's lease started, so a commit made but never recorded is recorded now)."""
    res = StepResult(state["id"], step, state["state"], run_id=run_id, budget=budget)
    try:
        # SPEC-0344: a persona-capable worker may take the run (or holds an adopted one)
        cfg = getattr(ctx, "cfg", None)
        remote = RPS.dispatch(cfg, state, step.kind, req, adopted=adopted is not None,
                              local=ctx.runner) if cfg is not None else None
        runner = remote or ctx.runner
        detachable = bool(getattr(runner, "detachable", False))
        outcome = runner.run(req, hooks=hooks) if (hooks and detachable) else runner.run(req)
        if outcome is DETACHED:
            res.detached = True
            return res
        # an adopted job re-runs from the full request its record keeps, never from the
        # stripped adoption rebuild; without one the failed run is recorded as it is (a
        # transient `run_failed`) and the queue prepares the step again
        rerun_req = req if adopted is None else getattr(remote, "request", None)
        if getattr(outcome, "writeback_failed", False) and rerun_req is not None:
            run_id, req, outcome = _rerun_locally(ctx, state, step, run_id, rerun_req, budget,
                                                  outcome, hooks)
            res.run_id = run_id
            detachable = bool(getattr(ctx.runner, "detachable", False))
            if outcome is DETACHED:
                res.detached = True
                return res
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
        if hooks:
            hooks.done()
    return res


def _rerun_locally(ctx: _Ctx, state: dict, step: Step, run_id: str, req, budget: float, failed,
                   hooks: RunHooks | None = None):
    """SPEC-0344: a remote run whose write-back failed. Its `run.end` (`writeback_failed`,
    `result_tail: "write-back: <reason>"`) and cost go on the ledger now, the worker joins the
    step's `remote_excluded`, and the step runs once more, in-process on the local runner,
    under a new run id with its own `run.start` (no autopark). A detachable local runner gets
    the task's `hooks` back in `spawning` for the new run dir, so a stop or restart detaches
    the re-run like any local run (the result is then `DETACHED`; the next runner adopts the
    new run). Returns (run_id, req, result)."""
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
        if failed.worker and failed.worker not in excluded:
            excluded.append(failed.worker)
        payload = {k: v for k, v in start.items()
                   if k not in ("id", "seq", "ts", "type", "campaign", "run_id")}
        payload.update(persona=persona, run_id=new_id, retry_of=run_id,
                       remote_excluded=excluded)
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
    new_req = replace(req, run_dir=store.runs_dir(cfg, cid) / new_id)
    if tmp_env is not None:
        new_req = replace(new_req, env={**env, **tmp_env})
    if hooks is not None and getattr(ctx.runner, "detachable", False):
        if not hooks.restart(new_req.run_dir):
            return new_id, new_req, DETACHED
        return new_id, new_req, ctx.runner.run(new_req, hooks=hooks)
    return new_id, new_req, ctx.runner.run(new_req)


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
    try:
        git.restore_branch(worktree, branch, pre_head)
        crash = None
        try:
            res.run_result = rr = ctx.runner.run(req)
        except Exception as e:          # a crashing resolver is a failed attempt, not a stuck close
            crash = f"the run raised: {e}"
            res.run_result = rr = _synthetic_failure(crash)
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
        res.gate_run = G.run_all(pcfg, state, ctx.cfg, seq=seq, stop=ctx.stop,
                                 tier="loop", adopt=live, lost=lost)     # SPEC-0328
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
    # SPEC-0207: file the follow-up ideas first; the filer never raises, and its events and
    # summary reach `_apply` whether or not the close then succeeds.
    if state.get("kind") not in ("merge", "split"):     # SPEC-0233/0290: no review, no follow-ups
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
    row for `nw.queue.tick.v1`."""
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
                      "gate_exclusive", "last_green_loop", "close_red", "gate_retry_pending"):
                if k in state:
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
                _park(cfg, current, park["reason"], park["detail"], park["resume_to"],
                      extra_events=events, auto_ok=not park.get("no_auto"))
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
        if state.get("kind") == "split":                               # SPEC-0290
            return finish("SPEC_WRITING", "opened")
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

    if kind in ("implement", "triage", "resolve"):
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
        decision = decide_triage(state, profile, rr.verdict, rr.verdict_ok)
        if decision.kind == "none":                      # malformed verdict, retry allowed
            counters["triage_retries"] += 1
            return finish("TRIAGING", "triage_retry")
        if rr.verdict_ok:                                # SPEC-0408
            events.append(("triage", _gate_triage_event(cfg, cid, state, res.run_id,
                                                        rr.verdict, decision)))
        if decision.kind == "park":
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
        if run.ok:
            counters["timeout_regates"] = 0
            return finish("REVIEWING", "gates_green")
        if timeout_only(payload):                        # SPEC-0311: load, not a code fix
            timeouts = _gate_timeouts(ctx, state)
            decision = decide_gate_timeout(state, timeouts)
            if decision.kind == "park":
                return finish("PARKED", decision.reason, park={
                    "reason": decision.reason, "detail": decision.detail,
                    "resume_to": decision.resume_to})
            counters["timeout_regates"] += 1
            state["gate_exclusive"] = {"entered_at": store.now_iso()}
            events.append(("note", {
                "text": f"gate timeout: {', '.join(_timed_out_names(payload, timeouts))}; "
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
            return finish(oc.next_state, "closed")
        if oc.next_state == "TRIAGING" and state.get("kind") in ("merge", "split"):
            # SPEC-0233: no gate triager for a merge request (SPEC-0290: nor a split campaign);
            # the conductor relays the failure
            return finish("PARKED", "gates_failed", park={
                "reason": "gates_failed", "detail": oc.gate_detail or "gates failed",
                "resume_to": "CLOSING"})
        if oc.next_state == "TRIAGING":
            if oc.synthetic_gate is not None:
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
               or str((state.get("idea") or {}).get("id") or "").strip()).upper()
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
                               split_origin=state)
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
    if res.interrupted or rr.interrupted:
        # rolled back by the worker; no retry counted, the next tick runs a fresh writer
        with store.item_lock(cfg, cid):
            for etype, payload in events:
                store.append_event(cfg, cid, etype, payload)
            store.write_state(cfg, cid, state)
        row["to"], row["reason"] = res.from_state, "interrupted"
        return row
    park_back = {"resume_to": "SPEC_WRITING"}
    if w["blocked"]:
        return finish("PARKED", "spec_write_failed", park={
            "reason": "spec_write_failed", "detail": "blocked: " + "; ".join(w["blocked"]),
            **park_back})
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
        counters["spec_write_retries"] = 0
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
    counters["spec_write_retries"] = 0
    state["pending_brief"] = None
    events.append(("spec_written", {"spec_id": spec_id, "rel_path": sw["path"],
                                    "title": w["title"], "mode": sw["mode"]}))
    if sw["mode"] == "amend":                                          # SPEC-0279
        return _finish_amend(cfg, state, sw, finish)
    if link_error:
        # best-effort (SPEC-0256): the spec is committed; the link can be redone with `nw idea`
        events.append(("note", {"text": f"could not link idea to {spec_id}: {link_error}",
                                "author": "queue"}))
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
        try:
            spec_file = Path(state["repo"]["worktree"]) / spec["rel_path"]
            dest = store.campaign_dir(cfg, cid) / CL.SPEC_AT_OPEN
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(spec_file, dest)
            spec["sha256_at_open"] = _spec_sha(spec_file)
            events.append(("spec_accepted", {"spec_id": spec["id"], "round": rnd}))
            state["pending_brief"] = (_revision_pending_brief(cfg, state, revision)
                                      if revision is not None else None)
        except Exception as e:      # the accept is committed; the resume finishes this part
            return exhausted(f"accept failed after commit: {e}")
        return finish("IMPLEMENTING", "spec_accepted")


def _finish_amend(cfg, state: dict, sw: dict, finish):
    """A committed amend (SPEC-0279): the amended text becomes the accepted text (close's drift
    check, SPEC-0186), the campaign is admitted after its one revision and goes straight to
    IMPLEMENTING with the review findings as the implementer's brief."""
    worktree = Path(state["repo"]["worktree"])
    spec = state["spec"]
    spec_file = worktree / sw["path"]
    spec["sha256_at_open"] = _spec_sha(spec_file)
    cdir = store.campaign_dir(cfg, state["id"])
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / CL.SPEC_AT_OPEN).write_text(spec_file.read_text(encoding="utf-8"), encoding="utf-8")
    state["admission"] = "after_revision"       # a resume to SPEC_WRITING never amends twice
    rev = state.get("revision") or {}
    state["pending_brief"] = SR.revision_brief({"spec_sha256": rev.get("review_sha"),
                                                "findings": rev.get("findings") or []})
    return finish("IMPLEMENTING", "spec_amended")


def _link_idea(cfg, state: dict, spec_id: str) -> str | None:
    """Give the campaign's idea `:SPEC:` and SPECCED; both are idempotent. Best-effort
    (SPEC-0256): returns the error text when the ideas file drifted, lacks SPECCED or cannot be
    read, else None; never raises."""
    from .. import org_write as OW
    try:
        task = _idea_task(cfg, _idea_id_of(state))
        if task is None:
            return None
        if (task.properties.get("SPEC") or "").strip() != spec_id:
            OW.set_property(cfg, task, "SPEC", spec_id)
            task = _idea_task(cfg, _idea_id_of(state))
        if task is not None and task.state != "SPECCED":
            OW.set_state(cfg, task, "SPECCED")
    except (ValueError, OSError) as e:
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


def _store_verdicts(state: dict, profile: dict, counters: dict, results) -> bool:
    """Merge this pass's valid verdicts into `last_review.verdicts` (overwriting a retried
    reviewer's entry) and refresh `run` / `runs` / `loop`. True when any verdict was stored."""
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
    tri = triage_reviews(_valid_verdicts(state, profile), profile, human_waived=hw, attested=at)
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
    set): back to IMPLEMENTING, or park `diverging` when the fix rounds show no convergence
    (SPEC-0247). The park keeps the brief, so a resume continues with it."""
    div = DV.check(ctx.cfg, state, events, kind)
    if div is None:
        return finish("IMPLEMENTING", reason)
    events.append(("loop_divergence", div))
    return finish("PARKED", "diverging", park={
        "reason": "diverging", "detail": DV.summary(div), "resume_to": "IMPLEMENTING"})


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
            _store_verdicts(state, profile, counters, res.reviews)
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
    _store_verdicts(state, profile, counters, res.reviews)
    return _route_reviews(ctx, state, profile, counters, row, events, finish)


def _route_reviews(ctx, state, profile, counters, row, events, finish):
    """Follow `decide_reviews` on the stored verdicts and waivers (SPEC-0203, SPEC-0206): retry
    missing reviewers, ask the triager (`triage` pending) or skip it on the cap, then pass, park
    or send the implementer back."""
    reviewers = reviewers_of(profile)
    multi = len(reviewers) > 1
    valid = _valid_verdicts(state, profile)
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
        return finish("PARKED", decision.reason, park={
            "reason": decision.reason, "detail": decision.detail,
            "resume_to": decision.resume_to})
    counters["review_retries"] = 0
    counters.pop("review_retries_by_persona", None)
    if decision.kind == "close":
        return finish("CLOSING", "review_pass")
    # fix
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
                _finish_reviews(ctx, report, fl.group)))
            continue
        (_step, fut, _h), = fl.tasks
        try:
            res = fut.result()
        except Exception as e:
            res = StepResult(st["id"], _step, st["state"], error=repr(e))
            report.errors.append(f"{st['id']}: {e!r}")
        if res.crashed:
            report.errors.append(f"{st['id']}: {res.crashed}")
        _guarded(guard, ctx, st, lambda: report.steps.append(_apply(ctx, st, res)))


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


def _guarded(guard: bool, ctx: _Ctx, st: dict, fn) -> None:
    if not guard:
        fn()
        return
    try:
        fn()
    except Exception as e:           # the campaign is left to the next runner's planning
        journal(ctx.cfg, "harvest_error", campaign=st.get("id"), error=f"{type(e).__name__}: {e}")


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
            fl.tasks.append((step, pool.submit(_do_persona, ctx, st, step, run_id, req, budget,
                                               hooks), hooks))
            rows[st["id"]]["run_ids"].append(run_id)
            fl.runs.append((run_id, getattr(req, "timeout_s", None)))
    for group in groups:              # a fan-out whose every prepare failed: nothing to run
        if group.state["id"] not in pool.flights:
            report.steps.append(_finish_reviews(ctx, report, group))


def _gates_in_flight(ctx: _Ctx, pool: StepPool) -> tuple[int, bool]:
    """(gate steps in flight, whether one is exclusive) (SPEC-0311). A `close` whose project
    re-gates counts as a gate step: it runs the same full suite."""
    n, exclusive = 0, False
    for fl in list(pool.flights.values()):
        for step, fut, _hooks in list(fl.tasks):
            if fut.done():
                continue
            if step.kind == "close":
                try:
                    if not _close_regates(fl.state, ctx.project(fl.state)):
                        continue
                except ValueError:
                    continue
            elif step.kind != "gate":
                continue
            n += 1
            exclusive = exclusive or (step.kind == "gate" and bool(gate_exclusive(fl.state)))
    return n, exclusive


def _gate_starts(ctx: _Ctx, pool: StepPool, busy: list, admitted_ids: set, flying: set) -> set:
    """Ids of the GATING campaigns allowed to start their gate step this tick (SPEC-0311)."""
    candidates = [s for s in busy if s.get("state") == "GATING" and s["id"] not in admitted_ids
                  and s["id"] not in flying]
    n, exclusive = _gates_in_flight(ctx, pool)
    return set(select_gate_starts(candidates, n, exclusive))


def _note_gate_deferred(ctx: _Ctx, pool: StepPool, st: dict) -> None:
    """Journal (runner memory de-dupes; no campaign state or ledger write) that an exclusive
    re-gate waits, once per campaign per `timeout_regates` value (SPEC-0311)."""
    marker = gate_exclusive(st)
    if not marker:
        return
    key = (st["id"], int((st.get("counters") or {}).get("timeout_regates", 0)),
           marker.get("entered_at"))
    if key in pool.deferred_noted:
        return
    pool.deferred_noted.add(key)
    journal(ctx.cfg, "gate_timeout_deferred", campaign=st["id"], timeout_regates=key[1])


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
    with sentinel state (every merge records it). Returns the project started, if any."""
    cfg = ctx.cfg
    fl = pool.sentinel
    if fl is not None:
        project, fut = fl
        if not fut.done():
            return None
        pool.sentinel = None
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
    if not candidates:
        return None
    flying = pool.ids()
    if any(s.get("state") == "GATING" and s["id"] not in flying for s in busy):
        return None
    if gate_slots.occupancy(cfg).get("waiting"):
        return None
    for project in candidates:
        try:
            pcfg = P.project_config(cfg, project)
            due = SN.check_due(cfg, project, root=pcfg.get("_root"), project_cfg=pcfg)
            if not due and (SN.interval_hours(cfg) <= 0 or not P.gates_for(pcfg, "close")):
                continue                                    # the clock is off / nothing to run
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
    return None


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
    ctx = _Ctx(cfg, stop=stop, runner=runner, lease=lease)
    report = TickReport()
    try:
        _tick(cfg, ctx, pool, report, admit=admit, own=own, draining=draining and not own)
    finally:
        if own:
            pool.shutdown()
    report.in_flight = list(pool.flights)
    remaining = [s for s in store.list_campaigns(cfg) if s.get("state") != "PARKED"]
    report.idle = not remaining and not pool.flights
    return report


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
    return {root: [s["id"] for s in sorted(members, key=lambda s: (s["id"] not in close_flights,
                                                                    *_train_key(s)))]
            for root, members in groups.items()}


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
        return []
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
    _settle_ended_remote_jobs(cfg, flying)
    _apply_worker_escalations(cfg)
    _finish_worker_drains(cfg)
    _run_worker_updates(cfg)
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
        step = plan_step(st, profile, pcfg)
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
            _note_gate_deferred(ctx, pool, st)       # SPEC-0311: retried on a later tick
            continue
        if step.kind in ("gate", "finalize_split"):
            pending_agent.append((st, step, None, None, 0.0, None))
            continue
        if ctx.stop.is_set():
            continue
        if step.kind == "review":
            group = _prepare_reviews(ctx, st, step, profile, pending_agent, report)
            if group:
                groups.append(group)
            continue
        if step.kind == "spec_review":
            pre = _spec_review_precheck(ctx, st, step)
            if pre is not None:
                report.steps.append(_apply(ctx, st, pre))
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

    # 3. agent steps: submitted; a single-shot tick waits for its own and applies them now
    _submit(ctx, pool, report, pending_agent, groups)
    ctx.progress()
    if not own and not draining:
        try:                                # SPEC-0359: the full suite on base, when due
            _sentinel_step(ctx, pool, busy)
        except Exception as e:              # never let the sentinel cost the tick
            journal(cfg, "sentinel_failed", error=f"{type(e).__name__}: {e}")
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
_STATE_OF_KIND = {"implement": "IMPLEMENTING", "triage": "TRIAGING", "review": "REVIEWING"}


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
                "review", persona=personas[0], personas=personas))
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
            fl.tasks.append((step, pool.submit(_do_persona, ctx, snap, step, rid, req, budget,
                                               hooks, adopted), hooks))
            fl.runs.append((rid, req.timeout_s))
            rows.append({"campaign": cid, "run_id": rid, "persona": persona, "status": label})
        report.started.append({"campaign": cid, "action": "adopt", "state": start_state,
                               "run_ids": [e["run_id"] for e in members]})
    if rows:
        journal(cfg, "adopt", runs=rows)


def _prepare_reviews(ctx: _Ctx, st: dict, step: Step, profile: dict, pending_agent: list,
                     report: TickReport):
    """Prepare one run per pending reviewer, in profile order (run ids are allocated before any
    task starts). A failure with one reviewer configured is the campaign-wide error of
    SPEC-0187; with several it is that reviewer's missing result and its siblings still run.
    Returns the `_ReviewGroup` to apply when its flight is harvested, or None."""
    # the triager (SPEC-0206) never takes the one-reviewer campaign-wide error path
    multi = len(reviewers_of(profile)) > 1 or step.persona == "review_triager"
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


def _finish_reviews(ctx: _Ctx, report: TickReport, group: _ReviewGroup) -> dict:
    """Apply a reviewer fan-out once, results in profile order. One reviewer: its bare result
    (or the campaign-wide error of a raising worker), as SPEC-0187. Several: a worker that
    raised or errored after `run.start` becomes a synthetic failed run (a `run.end` and the
    budget charge), so the reviewer is simply missing."""
    st = group.state
    multi = len(reviewers_of(ctx.profile(st))) > 1 or group.step.persona == "review_triager"
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
    if agentic.get("runner") not in (None, "claude", "fake"):
        raise ValueError(f"unknown agentic.runner {agentic['runner']!r} "
                         f"(expected claude or fake)")


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
        out.append({"campaign": f"sentinel-{pool.sentinel[0]}", "kind": "sentinel",
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
                  clock=time.monotonic, env=None, cwd=None) -> dict:
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
                clock=time.monotonic, sleep=time.sleep) -> dict:
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
    try:
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
    return {"schema": "nw.queue.v1", "lease": L.lease_status(cfg),
            "slots": {"total": slots, "busy": busy}, "items": items}


# ---- dashboard (nw queue watch, SPEC-0185) -------------------------------------------------------

PHASES = ("OPENING", "SPEC_WRITING", "SPEC_REVIEW", "IMPLEMENTING", "GATING", "TRIAGING",
          "REVIEWING", "CLOSING")
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


def plan_add(cfg, spec_id, *, project=None, profile=None, base=None, force=False,
             priority=PRIORITY_DEFAULT, split_origin=None) -> dict:
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
    pick = select_profile(cfg, flag=profile, review=review_doc,         # SPEC-0219
                          project_root=root)
    prof = resolve_profile(cfg, pick["profile"], project_root=root)
    base = base or (pcfg.get("merge") or {}).get("base") or pcfg.get("base") or "main"
    cid = store.new_campaign_id(cfg, spec_id)
    worktree = P.worktree_root(pcfg, root, project) / cid
    return {
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
    }


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
               docs_only=False) -> dict:
    """SPEC-0233: resolve everything `nw queue merge` needs and return the plan (no writes).
    Raises ValueError naming the fix when the branch cannot be queued for a merge.
    SPEC-0234: `docs_only` also needs every changed path to be on `merge.docs_only_paths`."""
    branch = (branch or "").strip()
    if not branch:
        raise ValueError("branch is required")
    priority = check_priority(priority)
    project = project or P.find_project_for_cwd(cfg) or "Meta-Tools"
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
                  priority=PRIORITY_DEFAULT) -> dict:
    """`plan_add` for an idea (SPEC-0209): no writes. Refusals, first match wins: unknown idea,
    idea with a `:SPEC:`, closed idea, open campaign for the idea, unloadable project."""
    idea_id = (idea_id or "").strip()
    priority = check_priority(priority)
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
    project = project or (task.properties.get("PROJECT") or "").strip() or "Meta-Tools"
    pcfg = P.project_config(cfg, project)
    root = Path(pcfg["_root"])
    prof = resolve_profile(cfg, profile, project_root=root)
    base = base or (pcfg.get("merge") or {}).get("base") or pcfg.get("base") or "main"
    cid = store.new_campaign_id(cfg, idea_id)
    worktree = P.worktree_root(pcfg, root, project) / cid
    return {
        "kind": "idea", "idea_id": idea_id, "campaign": cid, "project": project,
        "profile": prof["name"], "priority": priority, "base": base, "repo_root": str(root),
        "worktree": str(worktree), "branch": f"nw/{cid}",
        "cap_usd": prof.get("campaign_cap_usd"), "loop_max": prof.get("loop_max"),
        "slots": prof.get("slots"), "merge_policy": (pcfg.get("merge") or {}).get("policy"),
        "profile_sha256": profile_sha256(prof),                         # SPEC-0397
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
        idea = EX.idea_show_payload(cfg, task)
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
        store.create_campaign(cfg, state, by=by)
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


def _order_specs(spec_ids, deps_of) -> tuple[list[str], dict]:
    """Kahn's algorithm over the in-batch `depends_on` edges, ties by input position. Returns
    `(order, cycles)`: `cycles` maps every spec on a cycle to its error text; the specs that are
    stuck behind a cycle follow in input order."""
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
            if "error" in it or it["kind"] != "spec":
                continue
            bad = next((d for d in deps_of.get(it["id"], ())
                        if d in by_id and "error" in by_id[d]), None)
            if bad:
                it["error"] = f"depends on refused {bad}"
                it.pop("plan", None)
                it.pop("would_block", None)
                changed = True


def plan_add_batch(cfg, ids, *, project=None, profile=None, base=None, force=False,
                   priority=PRIORITY_DEFAULT, split_origin=None) -> dict:
    """SPEC-0276: plan several specs and ideas as one batch (no writes). Each id goes through
    `plan_add` / `plan_add_idea`; on top, specs are ordered by their in-batch `depends_on`
    (ideas follow), cycles and dependents of refused items are refused, and dependencies that
    would block a spec forever are listed in its `would_block`."""
    ids = list(dict.fromkeys(_canon_id(i) for i in ids if i and i.strip()))
    params = {"project": project, "profile": profile, "base": base, "force": bool(force),
              "priority": priority}
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
                                          priority=priority) if kind == "idea" else
                            plan_add(cfg, i, project=project, profile=profile, base=base,
                                     force=force, priority=priority, split_origin=split_origin))
        except ValueError as e:
            item["error"] = str(e)
        items[i] = item
    spec_ids = [i for i in ids if items[i]["kind"] == "spec"]
    all_deps, deps_of = _batch_deps(cfg, spec_ids, project)
    order, cycles = _order_specs(spec_ids, deps_of)
    for sid, msg in cycles.items():
        prior = items[sid].get("error")
        items[sid]["error"] = f"{prior}; {msg}" if prior else msg
        items[sid].pop("plan", None)
    ordered = [items[i] for i in order] + [items[i] for i in ids if items[i]["kind"] == "idea"]
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
                                     priority=prm.get("priority", PRIORITY_DEFAULT))
                apply_add_idea(cfg, plan, by=by)
            else:
                plan = plan_add(cfg, it["id"], project=prm.get("project"),
                                profile=prm.get("profile"), base=prm.get("base"),
                                force=bool(prm.get("force")),
                                priority=prm.get("priority", PRIORITY_DEFAULT),
                                split_origin=prm.get("split_origin"))
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
        _counters(st)["timeout_regates"] = 0                 # SPEC-0311: a fresh re-gate budget
        if (st.get("park") or {}).get("reason") == "gate_timeout" and plan["to"] == "GATING":
            st["gate_exclusive"] = {"entered_at": store.now_iso()}   # re-gate alone, first
        if by == "queue" and plan.get("auto_rule"):      # SPEC-0302: bounded automatic resumes
            counts = _counters(st).setdefault("auto_resumes", {})
            counts[plan["auto_rule"]] = int(counts.get(plan["auto_rule"], 0)) + 1
        _drop_decision_request(cfg, st)
        st["park"] = None
        st.pop("park_requested", None)
        st.pop("gate_retry_pending", None)          # SPEC-0293: a resumed gate run is no retry
        ev = ("resume", {"to": plan["to"], "note": plan.get("note") or plan.get("event_note"),
                         "cap_usd": plan.get("raise_cap"), "loops_reset": bool(plan.get("reset_loops"))})
        st = _enter_slot_or_wait(cfg, st, plan["to"], "resumed", by=by, extra_events=[ev],
                                 busy=busy, slots=slots)
        from . import escalations as ESC            # SPEC-0232: after the transition succeeded
        ESC.withdraw_for_campaign(cfg, cid, f"campaign resumed by {by}", by)
        return st


def _reset_spec_write(cfg, st: dict) -> None:
    """Resuming a spec-phase park (SPEC-0215, SPEC-0214): fresh retry budgets and a fresh idea
    snapshot (its text may have been edited meanwhile), keeping `state_before`."""
    _counters(st)["spec_write_retries"] = 0
    _counters(st)["spec_review_retries"] = 0
    old = st.get("idea") or {}
    task = _idea_task(cfg, old.get("id")) if st.get("kind") == "idea" else None
    if task is not None:
        from .. import explore as EX
        fresh = EX.idea_show_payload(cfg, task)
        fresh["epic"] = task.properties.get("EPIC")
        fresh["state_before"] = old.get("state_before")
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


def _finish_worker_drains(cfg) -> None:
    """SPEC-0343, every tick: disable the draining workers that are idle (`workers.finish_drains`)
    and emit `worker_drained` for each. An unreadable registry, `remote-jobs` dir or record
    skips the pass (journaled `worker_drain_skipped`); nothing is disabled. The same error on
    consecutive ticks is journaled once, not every tick (a bad file stays bad until fixed)."""
    from . import events as E                   # lazy: events imports this module
    key = str(runner_journal_path(cfg))
    try:
        drained = workers.finish_drains(cfg)
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


def _run_worker_updates(cfg) -> None:
    """SPEC-0386, every tick: a worker draining for a scheduled update (`update_pending`) that
    is idle gets the SPEC-0385 update in a background thread; one still busy after
    `agentic.workers.update_drain_timeout_h` is returned to service (journal + event; no job
    is touched). An unreadable registry or job record skips the pass (the drain pass journals it)."""
    import threading
    try:
        idle, late = workers.due_updates(cfg)
    except (OSError, ValueError, TypeError):
        return
    jp = str(runner_journal_path(cfg))
    for rec in late:
        name = rec["name"]
        if (jp, name) in _UPDATING:
            continue
        hours = workers.update_drain_timeout_h(cfg)
        since = rec["update_pending"].get("since")
        if workers.end_update(cfg, name, since, "timeout", f"not idle after {hours:g} h"):
            journal(cfg, "worker_update_timeout", worker=name, timeout_h=hours)
            _emit_worker_event(cfg, "worker_update_failed", {
                "worker": name, "reason": "timeout",
                "error": f"still not idle after {hours:g} h; returned to service, not updated"})
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

